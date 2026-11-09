"""Check crossing delay coverage against the register cones in emitted RTL."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess
import tempfile

from log_clock import definition, TOP


def designs():
    for ratio in (2, 3, 5, 8, None):
        for protocol in ("apb4", "axi4"):
            for layout in ("to_main", "to_slow"):
                design = definition(protocol, "ram", False, layout)
                if ratio is None:
                    del design["clock_domains"][1]["ratio"]
                    design["clock_domains"][1]["enable_port"] = "qualify"
                    design["enable_ports"] = [{"name": "qualify"}]
                else:
                    design["clock_domains"][1]["ratio"] = ratio
                design["clock_roots"][0]["period_ps"] = 10000
                design["reset_ports"].append({"name": "slow_reset_n"})
                design["clock_domains"][1]["reset_port"] = "slow_reset_n"
                yield f"{ratio}-{protocol}-{layout}", design

    design = definition("apb4", "ram", False, "to_slow")
    design["clock_roots"][0]["period_ps"] = 10000
    design["output"]["register_reset"] = "synchronous"
    yield "synchronous-reset", design

    yield "async", definition("apb4", "ram", False, "explicit_direct")
    design = definition("apb4", "ram", False, "explicit_direct")
    design.pop("error_logging")
    design["initiators"] = [endpoint for endpoint in design["initiators"] if endpoint["name"] == "s"]
    design["targets"] = [endpoint for endpoint in design["targets"] if endpoint["name"] == "ram"]
    design["connectivity"] = {"s": ["ram"]}
    for graph in design["topology"].values():
        for key in ("entries", "exits"):
            graph[key] = [entry for entry in graph[key] if entry["endpoint"] in ("s", "ram")]
        a, = graph["entries"]
        z, = graph["exits"]
        graph["links"] = [{"from": a["node"], "to": z["node"]}]
    for kind, fast in (("async", False), ("related", False), ("related", True),
                       ("edge_aligned", False)):
        fixture = json.loads(json.dumps(design))
        if kind == "async":
            fixture["reset_ports"].append({"name": "slow_reset_n"})
            fixture["clock_domains"][1]["reset_port"] = "slow_reset_n"
        else:
            fixture["clock_domains"] = definition("apb4", "ram")["clock_domains"]
            fixture["clock_roots"] = [{"name": "root", "clock_port": "clk", "period_ps": 10000}]
        overrides = [{"from_domain": a, "to_domain": z, "kind": kind}
                     for a, z in (("main", "slow"), ("slow", "main"))]
        for override in overrides:
            if fast:
                override.update(depth=2, fast=True)
            elif kind == "async":
                override["reset_handshake"] = True
        fixture["clock_crossings"] = overrides
        yield f"{kind}-{fast}", fixture


def register_cells(mapping, net):
    # Map each Q bit to the cell pattern used for its register in the SDC.
    names = net["netnames"]
    by_bit = {}
    for register in mapping["network"]["cdc"]["registers"]:
        if "rtl" in register:
            name = register["rtl"]
            by_bit.update((bit, name + "_reg*") for bit in names[name]["bits"])
        else:
            sync = register["synchronizer"]
            stage = sync["stage"]
            for instance in sync["instances"]:
                bit = names[instance + ".stage"]["bits"][stage]
                by_bit[bit] = f"{instance}/stage_reg[{stage}]"
    return by_bit


def register_paths(net, names, clocks):
    launches = {}
    captures = []
    comb = {}
    for cell in net["cells"].values():
        pins = cell["connections"]
        if "dff" in cell["type"]:
            clock = clocks.get(tuple(pins["CLK"]))
            if clock is None:
                continue
            for bit in pins["Q"]:
                launches[bit] = (names[bit], clock)
            for pin in ("D", "ARST", "SRST"):
                if pin in pins:
                    captures.append((pins[pin], (names[pins["Q"][0]], clock), pin))
        elif "latch" not in cell["type"]:
            inputs = [bit for port, bits in pins.items()
                      if cell["port_directions"][port] == "input" for bit in bits]
            for port, bits in pins.items():
                if cell["port_directions"][port] == "output":
                    comb.update((bit, inputs) for bit in bits)

    for inputs, capture, pin in captures:
        todo, seen = list(inputs), set()
        while todo:
            bit = todo.pop()
            if bit in seen:
                continue
            seen.add(bit)
            if bit in launches:
                source = launches[bit]
                if source[1] != capture[1]:
                    yield source, capture, "async" if pin == "ARST" else "data"
            else:
                todo.extend(comb.get(bit, ()))


DELAY = re.compile(r"^set_(min|max)_delay (.+?) -ignore_clock_latency "
                   r"-from \[get_(cells|clocks) \{([^}]+)\}\] -to (.+)$", re.M)
PIN_SELECT = re.compile(r"\[remove_from_collection \$nocloom_(data|async)_pins "
                        r"\[remove_from_collection \$nocloom_\1_pins "
                        r"\[get_pins -of_objects \[get_cells \{([^}]+)\}\]\]\]\]")


def delay_coverage(sdc):
    result = defaultdict(set)
    for sign, bound, source_kind, sources, destinations in DELAY.findall(sdc):
        if pins := PIN_SELECT.fullmatch(destinations):
            pin, cells = pins.groups()
            result[sign].update((source_kind, a, z, pin, bound)
                                for a in sources.split() for z in cells.split())
        elif clocks := re.fullmatch(r"\[get_clocks \{([^}]+)\}\]", destinations):
            result[sign].update((source_kind, a, clocks[1], "clock", bound)
                                for a in sources.split())
    return result


def check_paths(mapping, net, sdc):
    definition = mapping["definition"]
    clock_bits = {tuple(net["ports"][d["clock_port"]]["bits"]): d["name"]
                  for d in definition["clock_domains"] if "edge_parent" not in d}
    divided = [d["name"] for d in definition["clock_domains"] if "edge_parent" in d]
    if divided:
        gated, = (v["bits"] for n, v in net["netnames"].items() if n.endswith("_gate.clock_out"))
        clock_bits[tuple(gated)], = divided
    coverage = delay_coverage(sdc)
    paths = set(register_paths(net, register_cells(mapping, net), clock_bits))
    cuts = mapping["network"]["cdc"]["cuts"]
    pairs = {(cut.get("kind", "async"), frozenset((cut["from_domain"], cut["to_domain"])))
             for cut in cuts}
    checked = Counter()
    if any(kind == "related_log" for kind, _ in pairs):
        assert "set nocloom_data_pins [all_registers -data_pins]" in sdc
        assert "set nocloom_async_pins [all_registers -async_pins]" in sdc
    clock_delays = {sign: {(kind, a, z) for kind, a, z, pin, _ in rows if pin == "clock"}
                    for sign, rows in coverage.items()}
    for kind, domains in pairs:
        for (a, a_domain), (z, z_domain), pin in paths:
            if {a_domain, z_domain} != domains:
                continue
            if kind == "related_log":
                # The handover requires both bounds on the correct pin type.
                assert ("cells", a, z, pin, "0") in coverage["min"], (kind, a, z, pin, "min")
                assert any(("cells", a, z, pin, bound) in coverage["max"]
                           for bound in ("10.0", "10.000", "10")), (kind, a, z, pin, "max")
                checked[pin] += 1
                continue
            bounds = {sign for sign, endpoints in clock_delays.items()
                      if ("cells", a, z_domain) in endpoints or
                      ("clocks", a_domain, z_domain) in endpoints}
            if kind == "async":
                assert pin == "data" and bounds == {"min", "max"}, (kind, a, z, pin, bounds)
            else:
                # Ordinary setup/hold or recovery/removal times related paths
                # outside the generated transfer exceptions.
                assert "set_clock_groups -asynchronous" not in sdc
                assert "set_false_path" not in sdc
                assert not bounds or bounds == {"min", "max"}, (kind, a, z, pin, bounds)
            checked[kind + (" bounded" if bounds else " clock timed")] += 1
    assert checked, "No crossing paths reached"
    return checked


def run(cli, yosys, directory, label, design):
    work = directory / label
    work.mkdir()
    config = work / "design.yaml"
    config.write_text(json.dumps(design))
    subprocess.run([cli, "generate", config, "--out-dir", work / "rtl", "--cdc-constraints"],
                   check=True, timeout=20, stdout=subprocess.PIPE)
    mapping = json.loads((work / "rtl" / f"{TOP}.map.json").read_text())
    sdc = (work / "rtl" / f"{TOP}.cdc.sdc").read_text()
    sources = " ".join(str(path.relative_to(work)) for path in sorted((work / "rtl").glob("*.v")))
    subprocess.run([yosys, "-Q", "-q", "-p", f"read_verilog {sources}; hierarchy -top {TOP}; "
                    "proc; flatten; write_json net.json"], cwd=work, check=True, timeout=20)
    net = json.loads((work / "net.json").read_text())["modules"][TOP]
    print(label, dict(check_paths(mapping, net, sdc)), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--yosys", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    for label, design in designs():
        run(args.cli, args.yosys, directory, label, design)


if __name__ == "__main__":
    main()
