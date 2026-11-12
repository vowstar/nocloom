"""Plan wide selector maps under a fixed budget and check their generated APB decode."""

import argparse
import json
from pathlib import Path
import re
import resource
import subprocess
import tempfile

from sim_engine import Simulation, add_arguments, generated_rtl


def mapping(base, local, when=None, size=0x1000):
    row = dict(base=hex(base), size=hex(size), local_address=hex(local))
    if when:
        row["when"] = when
    return row


def definition(count, independent=False):
    names = [f"sel{i:02}" for i in range(count)]
    ports = [f"mode{i:02}" for i in range(count)]
    if independent:
        windows = [mapping((i + 1) * 0x1000, (i + 1) * 0x2000, {name: True})
                   for i, name in enumerate(names)]
    else:
        windows = [mapping(0, 0, {names[0]: False}, size=0x100000)]
        groups = [names[i:i + 4] for i in range(0, count, 4)]
        for i, group in enumerate(groups):
            windows.append(mapping((i + 1) * 0x1000, (i + 1) * 0x2000,
                                   dict.fromkeys([names[0], *group], True)))
        windows += [mapping(len(groups) * 0x1000, 0x14000,
                            {names[0]: True, names[-1]: False}),
                    mapping(0x10000, 0x18000, dict.fromkeys(names, True))]
        for i in range(16 - len(windows)):
            windows.append(mapping(0x30000 + 0x2000 * i, 0, windows[1]["when"]))
    apb = dict(clock_domain="main", protocol="apb4", data_width=32)
    return dict(schema_version=10, name="selectors", address_width=24,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n",
                                    test_port="test")],
                address_inputs=[dict(name=port, width=1, clock_domain="main") for port in ports],
                address_selectors=[dict(name=name, equals={port: 1})
                                   for name, port in zip(names, ports)],
                initiators=[dict(name="cpu", address_width=20, **apb),
                            dict(name="cfg", address_width=24, **apb)],
                targets=[dict(name="ram", address_width=20, attributes="amba", mappings=windows,
                              **apb),
                         dict(name="regs", clock_domain="main", protocol="axi4", data_width=32,
                              id_width=2, len_width=4, address_width=12, attributes="amba",
                              mappings=[mapping(0x200000, 0)])],
                connectivity=dict(cpu=["ram"], cfg=["regs"]),
                output=dict(backend="verilog", language="verilog_2001"))


def covered_definition(conditional):
    config = definition(32, independent=True)
    ram, regs = config["targets"]
    if conditional:
        # Each page is mapped for either value of its own selector. The first
        # 17 selectors already yield more than 65,536 distinct visible sets.
        config["initiators"][0]["address_width"] = 17
        ram["mappings"] = [mapping(i * 0x1000, value * 0x1000, {f"sel{i:02}": value})
                           for i in range(32) for value in (False, True)]
    else:
        regs["address_width"] = ram["address_width"]
        regs["mappings"] = ram["mappings"]
        for window in regs["mappings"]:
            window["base"] = hex(int(window["base"], 16) + 0x200000)
        ram["mappings"] = [mapping(0, 0, size=1 << config["initiators"][0]["address_width"])]
    return config


def memory_limit():
    cap = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def generate(cli, root, config, *, budget=False):
    root.mkdir()
    (root / "network.yaml").write_text(json.dumps(config))
    run = subprocess.run([str(cli), "generate", "network.yaml", "--out-dir", "gen",
                          "--address-table", "--c-header", "--ipxact"],
                         cwd=root, capture_output=True, text=True, timeout=15,
                         preexec_fn=memory_limit if budget else None)
    (root / "generation.log").write_text(run.stdout + run.stderr)
    return run


def check_region(cli, root, config, *, width, possible, exact):
    run = generate(cli, root, config, budget=True)
    assert run.returncode == 0, run.stderr
    artifacts = root / "gen"
    data = json.loads((artifacts / "nocloom_selectors.map.json").read_text())
    fields = data["definition"]["target_ids"]["record_fields"]
    assert fields[0] == dict(name="region_index", width=width, lsb=0), fields
    assert fields[1]["lsb"] == width, fields
    coverage = data["definition"]["address_selection_holes"]
    assert coverage == dict(cpu=dict(possible=possible, exact=exact),
                            cfg=dict(possible=True, exact=True)), coverage
    return data, artifacts


def scale(cli, work):
    for count in (28, 32):
        data, artifacts = check_region(cli, work / str(count), definition(count),
                                       width=5, possible=True, exact=True)
        assert len(data["network"]["address_selectors"]) == count
        files = [next(artifacts.glob(suffix)) for suffix in
                 ("*.address_table.csv", "*.cpu.addresses.h", "*.ipxact.xml")]
        for path in files:
            assert f"sel{count - 1:02}" in path.read_text(), path.name
    check_region(cli, work / "independent", definition(32, independent=True),
                 width=6, possible=True, exact=True)
    check_region(cli, work / "unconditional_cover", covered_definition(conditional=False),
                 width=6, possible=False, exact=True)
    # 64 reportable blocks fit in six bits. Seven bits also fit the reserved
    # miss code 64 and place the next field above it.
    check_region(cli, work / "conditional_cover", covered_definition(conditional=True),
                 width=7, possible=True, exact=False)
    print("SCALE_CLI_PASS")


def simulate(args, work):
    for count, independent in ((17, False), (32, False), (32, True)):
        root = work / ("independent" if independent else str(count))
        run = generate(args.cli, root, definition(count, independent), budget=True)
        assert run.returncode == 0, run.stderr
        path = root / "gen/nocloom_selectors.v"
        rtl = generated_rtl(path)
        ports = "".join(f".mode{i:02}(modes[{i}]),\n" for i in range(count))
        bench = (args.bench.read_text().replace("SELECTOR_INPUTS", ports)
                 .replace("SELECTOR_COUNT", str(count))
                 .replace("INDEPENDENT_WINDOWS", str(int(independent))))
        (root / "tb.sv").write_text(bench)
        (root / "network.v").write_text(rtl)
        sim = Simulation(root, "network.v", engine=args.sim, config=args.sim_config)
        result = sim.run()
        assert result.returncode == 0, result.stdout
        print(next(line for line in result.stdout.splitlines() if "ADDRESS_PASS" in line))
        if count == 32:
            # Hold the highest selector low. A window that requires it must fail.
            map_file = path.with_suffix(".map.json")
            net = json.loads(map_file.read_text())["network"]["address_selectors"]["sel31"]
            wire = "nl_internal_v" + net.split("/")[1]
            changed, edits = re.subn(rf"^(assign {wire} = )[^;]+;", r"\g<1>1'b0;",
                                     rtl, flags=re.MULTILINE)
            assert edits == 1, wire
            fault = root / "selector_low"
            fault.mkdir()
            (fault / "tb.sv").write_text(bench)
            (fault / "network.v").write_text(changed)
            sim = Simulation(fault, "network.v", engine=args.sim, config=args.sim_config)
            result = sim.run()
            label = "last_independent" if independent else "all_selectors"
            assert result.returncode != 0 and f"CHECK_FAIL {label}" in result.stdout, result.stdout
            print("SELECTOR_FAULT_CAUGHT")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--bench", type=Path)
    add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=args.work))
    if args.bench:
        simulate(args, work)
    else:
        scale(args.cli, work)


if __name__ == "__main__":
    main()
