"""Exercise an error log beside a target on a divided clock."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from log_bank_access import ports
import sim_engine


NAME = "log_clock"
TOP = "nocloom_" + NAME
WINDOW = 0x100000


def definition(protocol, slow_name, timeout=True, layout="binding"):
    main, slow = "main", "slow"
    roles = {"s": main, "slow_src": slow, "csr": main, "logged": main, slow_name: slow}
    links = {"s": ["csr", "logged"], "slow_src": [slow_name]}
    log = dict(name="errors", watch=["logged"])
    reset = dict(clock_port="clk", reset_port="reset_n", clock_root="root")
    clocks = [dict(reset, name=main), dict(reset, name=slow, ratio=2, edge_parent=main)]
    if layout in ("to_main", "to_slow", "in_slow", "qualified"):
        config_domain = slow if layout in ("to_slow", "in_slow", "qualified") else main
        target_domain = main if layout in ("to_slow", "qualified") else slow
        roles.update(s=config_domain, csr=config_domain, data_src=target_domain, logged=target_domain)
        links["s"] = ["csr"]
        links["data_src"] = ["logged"]
    if layout == "in_slow":
        roles.update(slow_src=main)
        roles[slow_name] = main
    if layout == "qualified":
        clocks[1].pop("ratio")
        clocks[1]["enable_port"] = "slow_enable"
    if layout == "explicit_direct":
        clocks = [dict(name=main, clock_port="clk", reset_port="reset_n", period_ps=10000),
                  dict(name=slow, clock_port="clk_slow", reset_port="reset_n", period_ps=14000)]
        log["clock_domain"] = slow
    if layout in ("all_main", "minimal"):
        roles = dict.fromkeys(roles, main)
        clocks = [dict(name=main, clock_port="clk", reset_port="reset_n")]
    endpoints = {name: dict(name=name, protocol="apb4", data_width=32, clock_domain=domain)
                 for name, domain in roles.items()}
    for name, base in ((slow_name, 0), ("logged", 0x2000)):
        endpoints[name].update(attributes="amba", mappings=[dict(base=hex(base), size="0x1000")])
    if timeout:
        endpoints[slow_name]["timeout"] = dict(cycles=16, completion=True)
    if protocol == "axi4":
        endpoints["logged"].update(protocol=protocol, id_width=2, len_width=4)
    sources = tuple(links)
    targets = ("csr", "logged", slow_name)
    nodes = [dict(name=clock["name"], clock_domain=clock["name"]) for clock in clocks]

    def attachments(names):
        return [dict(endpoint=name, node=roles[name]) for name in names]

    def graph(entries, exits):
        return dict(nodes=nodes, links=[], entries=attachments(entries), exits=attachments(exits))

    config = dict(schema_version=10, name=NAME, address_width=32, clock_gating=False,
                  reset_ports=[dict(name="reset_n")], clock_domains=clocks,
                  initiators=[endpoints[name] for name in sources],
                  targets=[endpoints["logged"], endpoints[slow_name]], connectivity=links,
                  topology=dict(request=graph(sources, targets), response=graph(targets, sources)),
                  error_logging=dict(registers=dict(name="csr", base=hex(WINDOW), size="0x1000",
                                                    clock_domain=roles["csr"]), logs=[log]),
                  output=dict(backend="verilog", language="verilog_2001"))
    if "clock_root" in clocks[0]:
        config["clock_roots"] = [dict(name="root", clock_port="clk")]
    if layout == "qualified":
        config["enable_ports"] = [dict(name="slow_enable")]
    if layout == "minimal":
        config.pop("topology")
        config["initiators"] = [endpoints["s"]]
        config["targets"] = [endpoints["logged"]]
        config["connectivity"] = "all"
    return config


def connections(dut, protocol, slow_name, timeout, design):
    bound = dict(clk="clk", reset_n="reset_n", nl_o_errors_alarm="alarm")
    if len(design["reset_ports"]) == 2:
        bound["reset_log_n"] = "reset_log_n"
    if "enable_ports" in design:
        bound["slow_enable"] = "qualify"
    if any(c["clock_port"] == "clk_slow" for c in design["clock_domains"]):
        bound["clk_slow"] = "clk_slow"
    sources = {i["name"] for i in design["initiators"]}
    for index, source in enumerate(("s", "slow_src", "data_src")):
        if source not in sources:
            continue
        for name in "psel penable pwrite paddr pwdata pstrb prdata pready pslverr".split():
            bound[f"nl_i_{source}_{name}"] = f"{name}[{index}]"
    if "slow_src" in sources:
        bound[f"nl_t_{slow_name}_pready"] = "1'b0" if timeout else "1'b1"
    if timeout:
        bound[f"nl_t_{slow_name}_timed_out"] = "timed_out"
    if protocol == "apb4":
        bound.update(nl_t_logged_pready="1'b1", nl_t_logged_pslverr="1'b1",
                     nl_t_logged_prdata="32'hcafe1234")
        signals = "psel penable pwrite paddr pwdata pstrb"
    else:
        bound.update(nl_t_logged_awready="1'b1", nl_t_logged_wready="1'b1",
                     nl_t_logged_arready="1'b1", nl_t_logged_rresp="2'b10",
                     nl_t_logged_rlast="1'b1", nl_t_logged_bresp="2'b10",
                     nl_t_logged_rdata="32'hcafe1234")
        signals = ("awvalid awid awaddr awlen wvalid wdata wstrb wlast bvalid bready bid "
                   "arvalid arid araddr arlen rvalid rready rid")
    bound.update({f"nl_t_logged_{name}": f"t_{name}" for name in signals.split()})
    lines = []
    for direction, width, name in ports(dut, TOP):
        idle = "" if direction == "output" else f"{width}'d0"
        lines.append(f"    .{name}({bound.pop(name, idle)})")
    assert not bound, bound
    return ",\n".join(lines)


def bench_text(bench, dut, protocol, slow_name, timeout, design, layout):
    gates = re.findall(r"^nocloom_clock_gate .*\.clock_out\((\w+)\)\);$", dut, re.M)
    if "clock_roots" in design:
        slow_clock, = gates
        slow_clock = "dut." + slow_clock
    else:
        slow_clock = "clk_slow" if layout == "explicit_direct" else "clk"
    clock = {"main": "clk", "slow": slow_clock}
    sources = {i["name"]: clock[i["clock_domain"]] for i in design["initiators"]}
    data_index = 2 if "data_src" in sources else 0
    data_source = "data_src" if data_index else "s"
    watch_clock = clock[design["targets"][0]["clock_domain"]]
    paths = (sorted(t["name"] for t in design["targets"] + [dict(name="csr")]).index("logged") << 16
             | sorted(sources).index(data_source))
    miss_mask = "ffffffff" if data_index else "fff"
    periods = 8 if layout in ("to_main", "to_slow", "in_slow", "qualified") else 2
    values = dict(PORTS=connections(dut, protocol, slow_name, timeout, design),
                  WATCH_CLOCK=watch_clock, CFG_CLOCK=sources["s"], DATA_INDEX=str(data_index),
                  SPARE_CLOCK=sources.get("slow_src", "clk"), USER_CLOCK=sources[data_source],
                  PHASE_COUNT=str(periods), PATHS=f"32'h{paths:x}", MISS_MASK=f"32'h{miss_mask}")
    for key, val in values.items():
        bench = bench.replace(f"@{key}@", val)
    defines = ("`define AXI_TARGET\n" if protocol == "axi4" else "")
    defines += ("`define RUN_TIMEOUT\n" if timeout else "")
    return defines + bench


def run(args, work, protocol, slow_name, timeout, layout="binding", overflow=False):
    work.mkdir()
    config = work / "network.yaml"
    design = definition(protocol, slow_name, timeout, layout)
    config.write_text(json.dumps(design))
    rtl = work / "rtl"
    subprocess.run([args.cli, "generate", str(config), "--out-dir", str(rtl)],
                   check=True, timeout=30, stdout=subprocess.PIPE)
    dut = sim_engine.generated_rtl(rtl / (TOP + ".v"))
    (work / "dut.v").write_text(dut)
    bench = bench_text(args.bench.read_text(), dut, protocol, slow_name, timeout, design, layout)
    if overflow:
        bench = "`define WAIT_OVERFLOW\n" + bench
    (work / "tb.sv").write_text(bench)
    simulation = sim_engine.Simulation(work, "dut.v", engine=args.sim, config=args.sim_config)
    result = simulation.run()
    print(result.stdout, end="")
    assert result.returncode == 0, work


def check_rejections(cli, directory):
    base = definition("apb4", "ram")
    for case, code in (("separate_divided_log", "NCL_UNSUPPORTED_TOPOLOGY"),
                       ("watched_timeout", "NCL_UNSUPPORTED_TOPOLOGY"),
                       ("missing_mapping", "NCL_SCHEMA_MISSING_FIELD"),
                       ("unused_target", "NCL_CONNECTIVITY_UNUSED_TARGET")):
        design = json.loads(json.dumps(base))
        if case == "separate_divided_log":
            design["clock_roots"][0]["period_ps"] = 10000
            design["error_logging"]["logs"][0]["clock_domain"] = "slow"
        elif case == "watched_timeout":
            design["targets"][0]["timeout"] = dict(cycles=16, completion=True)
        elif case == "missing_mapping":
            design["targets"][1].pop("mappings")
        else:
            design["initiators"].pop()
            design["connectivity"].pop("slow_src")
            design["topology"]["request"]["entries"].pop()
            design["topology"]["response"]["exits"].pop()
        config = directory / f"{case}.yaml"
        config.write_text(json.dumps(design))
        result = subprocess.run([cli, "generate", config, "--out-dir", directory / case],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 1 and code in result.stderr, (case, result.stderr)


def check_split_reset(args, work, ratio):
    work.mkdir()
    layout = "to_slow" if ratio else "qualified"
    design = definition("apb4", "ram", False, layout)
    design["clock_roots"][0]["period_ps"] = 10000
    slow = design["clock_domains"][1]
    if ratio:
        slow["ratio"] = ratio
    slow["reset_port"] = "reset_log_n"
    design["reset_ports"].append(dict(name="reset_log_n"))
    config, rtl = work / "network.yaml", work / "rtl"
    config.write_text(json.dumps(design))
    subprocess.run([args.cli, "generate", config, "--out-dir", rtl, "--cdc-constraints"],
                   check=True, timeout=30, capture_output=True)
    mapping = json.loads((rtl / f"{TOP}.map.json").read_text())
    cut, = mapping["network"]["cdc"]["cuts"]
    assert (cut["kind"], cut["from_domain"], cut["to_domain"]) == ("related_log", "main", "slow")
    assert all((path["sources"] or path["input_ports"]) and path["destinations"]
               for path in cut["paths"])
    delays = re.findall(r"^set_max_delay ([0-9.]+) ",
                        (rtl / f"{TOP}.cdc.sdc").read_text(), re.M)
    assert delays and all(float(delay) == 10 for delay in delays), delays
    dut = sim_engine.generated_rtl(rtl / f"{TOP}.v")
    (work / "dut.v").write_text(dut)
    bench = bench_text(args.bench.read_text(), dut, "apb4", "ram", False, design, layout)
    for key, value in (("EVENT", cut["event"]), ("LOG_RESET", cut["reset"]),
                       ("TARGET_RESET", cut["target_reset"])):
        bench = bench.replace(f"@{key}@", "dut.nl_internal_v" + value.split("/")[1])
    (work / "tb.sv").write_text("`define SPLIT_RESET\n" + bench)
    sim = sim_engine.Simulation(work, "dut.v", engine=args.sim, config=args.sim_config)
    phases = set()
    for first, shifts in ((1, range(1, 23)), (0, (0, 1, 3, 5, 8))):
        for shift in shifts:
            result = sim.run((f"+target_first={first}", f"+shift={shift}"))
            reached = re.search(r"CHECK_PASS event=(\d+) log_start=(\d+) spacing=(\d+)",
                                result.stdout)
            assert result.returncode == 0 and reached, (ratio, first, shift, result.stdout)
            event, start, spacing = map(int, reached.groups())
            assert all(float(delay) <= spacing for delay in delays), (ratio, delays, spacing)
            if first:
                phases.add((event > start) - (event < start))
    assert phases == {-1, 0, 1}, (ratio, phases)
    print(f"{ratio or 'qualified'}: 27 reset trials, event phases {sorted(phases)}")


# The bindings of a log beside a target on a divided clock, one network each: the protocol of the
# watched target, the name of the divided target and whether that target has a timeout.
BINDINGS = {f"{protocol}_{slow_name}": (protocol, slow_name, True)
            for protocol in ("apb4", "axi4") for slow_name in ("a_ram", "ram")}
BINDINGS["no_timeout"] = ("apb4", "ram", False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--related", action="store_true")
    parser.add_argument("--split-reset", action="store_true")
    parser.add_argument("--wait-overflow", action="store_true")
    parser.add_argument("--binding", choices=BINDINGS)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    if sum(map(bool, (args.related, args.split_reset, args.wait_overflow, args.binding))) != 1:
        parser.error("give one of --binding, --related, --split-reset and --wait-overflow")
    args.work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    if args.wait_overflow:
        run(args, root / "overflow", "apb4", "ram", False, "qualified", overflow=True)
        return
    if args.split_reset:
        for ratio in (2, 3, 5, 8, 0):
            check_split_reset(args, root / f"split-{ratio}", ratio)
        return
    if args.related:
        for layout in ("to_main", "to_slow", "in_slow"):
            for protocol in ("apb4", "axi4"):
                for slow_name in ("a_ram", "ram"):
                    run(args, root / f"{layout}-{protocol}-{slow_name}", protocol, slow_name,
                        True, layout)
        for layout in ("all_main", "minimal", "explicit_direct", "qualified"):
            run(args, root / layout, "apb4", "ram", layout not in ("minimal", "qualified"), layout)
        check_rejections(args.cli, root)
        return
    run(args, root / args.binding, *BINDINGS[args.binding])


if __name__ == "__main__":
    main()
