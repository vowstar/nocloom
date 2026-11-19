"""Compare the clock and reset of every emitted state after renaming endpoints."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess
import tempfile

from address_selectors import definition as wide_selectors
from log_clock import definition


def renamed(text, names):
    return re.sub(r"\b[a-z][a-z_0-9]*\b", lambda key: names.get(key[0], key[0]), text)


def signatures(report, names):
    signals = defaultdict(list)
    for index, port in report["inputs"].items():
        signals[index].append("port/" + port)
    states = report["states"]
    anchors = defaultdict(list)
    for key, location in report["bindings"].items():
        location = str(location)
        if (key.startswith("domain/") and key.endswith((".clock", ".reset", ".reset_port")) or
                key.startswith("gate/") and key.endswith(".clock")):
            signals[location].append(renamed(key, names))
        if location in states:
            anchors[location].append(renamed(key, names))
    for key, location in report["storage"].items():
        location = str(location)
        if location in states and not anchors[location]:
            anchors[location].append("storage/" + renamed(key, names))

    def signal(index):
        keys = signals[str(index)]
        assert keys, f"Clock or reset without a domain binding: {index}"
        return tuple(sorted(keys))

    named, all_states = Counter(), Counter()
    for index, (width, clock, reset) in states.items():
        assert anchors[index], f"Register without a binding or storage owner: {index}"
        binding = width, signal(clock), (signal(reset[0]), reset[1]) if reset else ()
        all_states[binding] += 1
        for key in anchors[index]:
            named[key, binding] += 1
    return named, all_states


def fifo_resets(cuts, bindings, reset_all):
    expected = {}

    def add(refs, domain, value=0):
        signal = bindings[f"domain/{domain}.reset"]
        for ref in refs:
            assert ref.startswith("value/"), ref
            key = ref.removeprefix("value/")
            assert key not in expected, (key, domain)
            expected[key] = [signal, value] if value is not None else None

    for cut in cuts:
        if cut.get("kind", "async") != "async":
            continue
        send, receive = cut["from_domain"], cut["to_domain"]
        handshake = cut.get("handshake")
        add([cut["write_index"], *cut["read_count_sync"]], send)
        add([cut["read_index"], cut["receive_valid"], *cut["write_count_sync"]], receive)
        for key, domain in (("write_count", send), ("read_count", receive)):
            add([cut[key]], domain, None if handshake else 0)
        for field in cut["fields"]:
            add(field["storage"], send, 0 if reset_all else None)
            add([field["received"]], receive, 0 if reset_all else None)
        if handshake:
            for role, domain in (("sender", send), ("receiver", receive)):
                side = handshake[role]
                add([side["announce"], side["on"], *side["announce_sync"],
                     *side["acknowledge_sync"]], domain)
                add([side["wait"]], domain, (1 << cut["synchronizer_stages"]) - 1)
    return expected


def check_owners(report, mapping):
    network, definition = mapping["network"], mapping["definition"]
    cdc = network.get("cdc", {})
    bindings, gates = report["bindings"], report["gates"]
    domains = [domain["name"] for domain in definition.get("clock_domains", [])]
    if not domains:
        key, = (name for name in bindings if name.startswith("domain/") and name.endswith(".clock"))
        stem = key.removesuffix(".clock")
        domains = [stem.removeprefix("domain/")]
        assert report["inputs"][str(bindings[key])] == definition["clock_port"]
        assert report["inputs"][str(bindings[stem + ".reset_port"])] == definition["reset_port"]
    objects = cdc.get("objects", {})
    resources = {entry["name"]: entry.get("clock_domain", domains[0])
                 for entry in network["resources"]}
    cuts = {entry["segment"]: entry for entry in cdc.get("cuts", [])}
    logs = definition.get("error_logs", {})
    region_gates = {value for name, value in bindings.items()
                    if name.startswith("gate/") and name.endswith(".clock")}
    labels, stores = defaultdict(list), defaultdict(list)
    for name, index in bindings.items():
        labels[index].append(name)
    for name, index in report["storage"].items():
        stores[index].append(name.rsplit("/", 2)[0])
    fifo = fifo_resets(cdc.get("cuts", []), bindings, report["reset_all"])
    async_stores = {cut["name"] + suffix
                    for cut in cdc.get("cuts", []) if cut.get("kind", "async") == "async"
                    for suffix in ("", "/receive")}

    def object_domain(name):
        return objects[name] if objects else domains[0]

    def bank_domain(name):
        request = cuts.get(f"service/{name}/request") or cuts.get(f"service/{name}/0/request")
        return request["to_domain"] if request else object_domain("target/" + name)

    def owner(name):
        parts = name.split("/")
        if name.startswith("observe/"):
            log, component = parts[1:3]
            if log not in logs:
                return object_domain("target/" + log), False, False
            watch = logs[log]["watch"]
            domain = bank_domain(logs[log]["registers"])
            if component.startswith("sender"):
                return object_domain("target/" + watch[int(component[6:])]), False, False
            if component.startswith("probe"):
                k = int(component[5:].split(".")[0])
                cut = cuts.get(f"observe/{log}/{watch[k]}")
                if cut:
                    if component.endswith(".tracking"):
                        return cut["from_domain"], True, False
                    return cut["to_domain"], True, True
            boundary = component == "logger.probe_started"
            return domain, boundary, boundary
        if name.startswith("service/bank/"):
            return bank_domain(parts[2]), False, False
        if name.startswith("domain/"):
            domain = parts[1].split(".")[0]
            if "/init" in name or name.endswith(".reset"):
                return domain, True, True
            return domain, True, False
        if name.startswith("gate/"):
            region = name[5:].split(".")[0]
            return object_domain(region.removesuffix("/protocol")), False, False
        if name.startswith("burst_service/"):
            return object_domain("target/" + parts[1]), False, False
        if name.startswith("source/"):
            return object_domain("source/" + parts[1]), False, False
        if name.startswith("graph/"):
            return object_domain("/".join(parts[1:3]).split(">")[0]), False, False
        if name.startswith("level_hint/"):
            return object_domain("/".join(parts[1:3]).split(".")[0]), False, False
        return None

    for key, (_, clock, reset) in report["states"].items():
        index = int(key)
        names, storage = labels[index], stores[index]
        explicit = [owner(n) for n in names if n.startswith(("observe/", "service/bank/", "domain/"))]
        if explicit:
            expected = set(explicit)
        elif storage:
            expected = {(resources[n], False, False) for n in storage}
        else:
            expected = {domain for n in names if (domain := owner(n)) is not None}
        assert len(expected) == 1, ("ambiguous planned owner", names, storage, expected)
        domain, root, raw_reset = expected.pop()
        planned = bindings[f"domain/{domain}.clock"]
        if root:
            while str(planned) in gates:
                planned = gates[str(planned)]
        actual = gates[str(clock)] if clock in region_gates else clock
        assert actual == planned, ("planned clock", names, storage, domain, actual, planned)
        if async_stores.intersection(storage):
            assert key in fifo, ("missing FIFO reset role", names, storage, key)
            assert reset == fifo[key], ("planned FIFO reset", names, storage, reset, fifo[key])
            continue
        assert reset, ("planned reset missing", names, storage, domain)
        stem = f"domain/{domain}"
        match = bindings[stem + (".reset_port" if raw_reset else ".reset")]
        if any("/ahb." in n for n in names):
            # The AHB address phase can use the port reset.
            assert reset[0] in (match, bindings[stem + ".reset_port"]), (names, domain, reset)
        else:
            assert reset[0] == match, ("planned reset", names, storage, domain, reset, match)


def compare_definitions(fixture, cli, directory, shape, variants):
    expected = None
    for i, (source, names) in enumerate(variants):
        config = directory / f"{shape}-{i}.yaml"
        config.write_text(source)
        report = json.loads(subprocess.check_output([fixture, config], text=True, timeout=20))
        out = directory / f"{shape}-{i}"
        subprocess.run([cli, "generate", config, "--out-dir", out], check=True, timeout=20,
                       capture_output=True)
        map_file, = out.glob("*.map.json")
        mapping = json.loads(map_file.read_text())
        check_owners(report, mapping)
        if shape == "related_ahb":
            domains = {d["name"]: d["period_ps"] for d in mapping["definition"]["clock_domains"]}
            assert domains == {"main": 10000, "slow": 30000}, domains
        observed = signatures(report, names)
        definition = mapping["definition"]
        ports, selectors = definition.get("address_inputs", []), definition.get("address_selectors", [])
        modes = Counter(("input", i, renamed(port["name"], names), port["width"])
                        for i, port in enumerate(ports))
        modes.update(("selector", i, renamed(selector["name"], names),
                      tuple(sorted((renamed(port, names), value)
                                   for port, value in selector["equals"].items())))
                     for i, selector in enumerate(selectors))
        observed += (modes,)
        if shape.startswith("wide_modes_"):
            observed += (Counter((name, values["possible"], values["exact"])
                                 for name, values in definition["address_selection_holes"].items()),
                         Counter((field["name"], field["width"], field["lsb"])
                                 for field in definition["target_ids"]["record_fields"]))
        if expected is None:
            expected = observed
        else:
            for old, new in zip(expected, observed):
                assert old == new, (config, list((old - new).items())[:8],
                                    list((new - old).items())[:8])
    print(f"{shape}: {sum(expected[1].values())} state words, "
          f"{sum(expected[0].values())} named bindings in {len(variants)} renamings")


def compare(fixture, cli, directory, shape, source, names, variants):
    renamed_sources = [(renamed(source, dict(zip(names, variant))), dict(zip(variant, names)))
                       for variant in (names, *variants)]
    compare_definitions(fixture, cli, directory, shape, renamed_sources)


def mode_variants(source):
    # The example uses rom as both a selector and a target.
    ports = {"boot": "a_boot", "bank": "z_bank"}
    selectors = {"rom": "a_boot_region", "high": "z_high_region"}
    targets = {"ram": "z_mem", "rom": "a_mem", "dev": "m_mem"}

    def rename_mode(text, inputs, flags, destinations):
        text = renamed(text, inputs)
        start, end = text.index("address_selectors:"), text.index("initiators:")
        text = text[:start] + renamed(text[start:end], flags) + text[end:]
        text = re.sub(r"when: \{[^}]+\}", lambda m: renamed(m[0], flags), text)
        start, end = text.index("targets:"), text.index("connectivity:")
        targets_text = re.sub(r"(?m)^  - \{name: [a-z_0-9]+",
                              lambda m: renamed(m[0], destinations), text[start:end])
        return text[:start] + targets_text + text[end:]

    return [(rename_mode(source, inputs, flags, destinations),
             {value: key for names in (inputs, flags, destinations)
              for key, value in names.items()})
            for inputs, flags, destinations in (({}, {}, {}), (ports, {}, {}),
                                                ({}, selectors, {}), ({}, {}, targets),
                                                (ports, selectors, targets))]


def designs():
    for protocol, layout, gated in (("apb4", "binding", False), ("axi4", "to_main", True),
                                    ("apb4", "to_slow", False), ("apb4", "explicit_direct", True)):
        design = definition(protocol, "ram", True, layout)
        if layout == "to_slow":
            design["output"]["register_reset"] = "synchronous"
        yield f"{protocol}-{layout}", design, gated

    chain = definition("apb4", "ram", False)
    logs = chain["error_logging"]["logs"]
    logs[0]["loggers"] = [dict(pass_on="always") for _ in range(9)]
    logs.append(dict(name="spare_log", watch=["ram"]))
    yield "bank_chain", chain, True

    shaper = definition("apb4", "ram")
    shaper["initiators"][1]["qos"] = dict(shaper=dict(bytes_per_1024_cycles=64, burst_bytes=64))
    yield "shaper", shaper, False

    scheduler = definition("apb4", "ram", False)
    window = scheduler.pop("error_logging")["registers"]
    scheduler["targets"].append(dict(name=window["name"], clock_domain="main", protocol="apb4",
                                     data_width=32, attributes="amba",
                                     mappings=[dict(base=window["base"], size=window["size"])]))
    scheduler["arbitration"] = dict(policy="level_round_robin")
    scheduler["initiators"][1]["qos"] = dict(
        shaper=dict(bytes_per_1024_cycles=64, burst_bytes=64),
        scheduler=dict(under_level=1, over_level=0, threshold_percent=50))
    yield "scheduler", scheduler, False

    reservations = definition("apb4", "ram", False)
    reservations["initiators"][1].update(protocol="axi4", id_width=2, len_width=4)
    reservations["targets"][1].update(protocol="axi4", id_width=4, len_width=4, exclusive="network")
    yield "reservations", reservations, False

    related_ahb = definition("apb4", "ram", False, "qualified")
    related_ahb["targets"][1]["protocol"] = "ahb_lite"
    related_ahb["clock_roots"][0]["period_ps"] = 10000
    related_ahb["clock_domains"][1]["period_ps"] = 30000
    yield "related_ahb", related_ahb, False


def check_bindings(args, directory):
    endpoints = ("s", "slow_src", "csr", "logged", "ram", "data_src")
    variants = (("z_cfg", "a_spare", "u_win", "y_mem", "a_mem", "w_data"),
                ("a_cfg", "z_spare", "z_win", "b_mem", "m_mem", "v_data"),
                ("m_cfg", "m_spare", "n_win", "x_mem", "v_mem", "a_data"))
    for shape, design, gated in designs():
        if gated:
            design["clock_gating"] = True
            design["test_ports"] = [dict(name="test")]
            for domain in design["clock_domains"]:
                domain["test_port"] = "test"
        compare(args.fixture, args.cli, directory, shape, json.dumps(design), endpoints, variants)
    clocked = args.clocked_example.read_text().replace(
        "to: branch, transport_word_bytes: 4}",
        "to: branch, transport_word_bytes: 4, pipeline: {slice: full}}")
    compare(args.fixture, args.cli, directory, "clocked_links", clocked,
            ("s0", "s1", "s2", "t0", "t1", "t2"),
            (("z_src", "x_src", "y_src", "z_mem", "x_mem", "y_mem"),))
    split = clocked.replace(
        "reset_ports: [{name: reset_n}]", "reset_ports: [{name: reset_n}, {name: middle_reset_n}]"
    ).replace("clock_port: clk_middle, reset_port: reset_n",
              "clock_port: clk_middle, reset_port: middle_reset_n")
    compare(args.fixture, args.cli, directory, "clocked_links_split_reset", split,
            ("s0", "s1", "s2", "t0", "t1", "t2"),
            (("z_src", "x_src", "y_src", "z_mem", "x_mem", "y_mem"),))
    mode = args.mode_example.read_text()
    compare_definitions(args.fixture, args.cli, directory, "mode_remap", mode_variants(mode))
    compare_definitions(args.fixture, args.cli, directory, "mode_multi_input",
                        mode_variants(mode.replace("equals: {bank: 2}", "equals: {bank: 2, boot: 1}")))


def check_wide_modes(args, directory, counts):
    for count in counts:
        selectors = wide_selectors(count)
        flag_names = tuple(flag["name"] for flag in selectors["address_selectors"])
        port_names = tuple(port["name"] for port in selectors["address_inputs"])
        compare(args.fixture, args.cli, directory, f"wide_modes_{count}", json.dumps(selectors),
                (*flag_names, *port_names, "ram", "regs"),
                ((*reversed(flag_names), *reversed(port_names), "z_ram", "a_regs"),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--clocked-example", type=Path, required=True)
    parser.add_argument("--mode-example", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cases", choices=("all", "boundaries", "interior"), default="all")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    if args.cases != "interior":
        check_bindings(args, directory)
    counts = {"all": range(17, 33), "boundaries": (17, 32), "interior": range(18, 32)}
    check_wide_modes(args, directory, counts[args.cases])


if __name__ == "__main__":
    main()
