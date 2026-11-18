"""Check target watchdog timing from external handshakes."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import sim_engine


def configuration(kind, completion):
    target = dict(name="t", clock_domain="main", protocol=kind, data_width=32,
                  mappings=[dict(base="0x0", size="0x1000")],
                  timeout=dict(cycles=16, completion=completion))
    if kind.startswith("wb_"):
        target.update(protocol="wishbone", cycles=kind[3:], address_unit="byte")
    if kind not in ("apb3", "wb_classic", "wb_pipelined", "tl_ul"):
        target["attributes"] = "amba"
    return dict(schema_version=10, name="target_watchdog", clock_gating=False,
                address_width=32, reset_ports=[dict(name="reset_n")],
                clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n")],
                initiators=[dict(name="s", clock_domain="main", protocol="axi4",
                                 data_width=32, len_width=8)],
                targets=[target], connectivity="all",
                output=dict(backend="verilog", language="verilog_2001"))


def bench(mapping, kind, completion):
    declarations, connections = [], []
    for port in mapping["ports"]:
        name, width = port["name"], port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_t_", "t_")
        connections.append(f".{name}({alias})")
        incoming = port["direction"] == "input"
        declarations.append(f"{'reg' if incoming else 'wire'} [{width - 1}:0] {alias}"
                            + ("=0;" if incoming else ";"))
    family = "apb" if kind.startswith("apb") else kind
    body = Path(__file__).with_suffix(".sv").read_text()
    return (f"`timescale 1ns/1ps\n`define TARGET_{family.upper()}\nmodule tb;\n"
            + "\n".join(declarations)
            + f"\nnocloom_target_watchdog dut({','.join(connections)});\n"
            + f"localparam COMPLETION={int(completion)};\n" + body + "\nendmodule\n")


def run(args, completion):
    work = Path(tempfile.mkdtemp(prefix="watchdog-", dir=args.work)).resolve()
    config = work / "input.yaml"
    config.write_text(json.dumps(configuration(args.kind, completion)))
    generated = work / "generated"
    subprocess.run([str(args.cli.resolve()), "generate", str(config), "--out-dir", str(generated)],
                   check=True, timeout=30)
    mapping = json.loads((generated / "nocloom_target_watchdog.map.json").read_text())
    rtl = sim_engine.generated_rtl(generated / "nocloom_target_watchdog.v")
    if args.negative:
        rtl, count = re.subn(r"^assign nl_t_t_timed_out = (.*);$",
                             r"assign nl_t_t_timed_out = watchdog_fault ? 1'b0 : \1;", rtl,
                             flags=re.M)
        if count != 1:
            raise RuntimeError(f"Expected one timer output, found {count}")
        start = rtl.index("\n", rtl.index(");", rtl.index("module nocloom_target_watchdog")))
        rtl = (rtl[:start] + "\nreg watchdog_fault;\n"
               "initial watchdog_fault = $test$plusargs(\"FAULT\");\n" + rtl[start:])
    (work / "tb.sv").write_text(bench(mapping, args.kind, completion))
    (work / "network.v").write_text(rtl)
    simulation = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                       compile_timeout=30, run_timeout=10, success=("WATCHDOG_PASS",))
    scenarios = (0, 1) if args.baseline else (0, 1, 2, 3, 4)
    if args.kind == "wb_pipelined" and not args.baseline:
        scenarios += (5,)
    for scenario in scenarios:
        result = simulation.run([f"+SCENARIO={scenario}"])
        print(result.stdout, end="", flush=True)
        if args.baseline and scenario == 1:
            if result.returncode == 0 or "CHECK_FAIL EARLY_EXPIRY" not in result.stdout:
                return 1
        elif result.returncode:
            return result.returncode
    if args.negative:
        result = simulation.run(["+SCENARIO=2", "+FAULT"])
        print(result.stdout, end="", flush=True)
        if result.returncode == 0 or "CHECK_FAIL MISSING_EXPIRY" not in result.stdout:
            return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--kind", required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true")
    group.add_argument("--negative", action="store_true")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    return run(args, True) or (0 if args.baseline or args.negative else run(args, False))


if __name__ == "__main__":
    raise SystemExit(main())
