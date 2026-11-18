"""Check target watchdog timing from external handshakes."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import sim_engine

DRIVEN = {"t_pready", "t_ack", "t_stall", "t_hreadyout", "t_a_ready", "t_d_valid", "t_d_opcode",
          "t_d_size", "t_awready", "t_wready", "t_bvalid", "t_arready", "t_rvalid",
          "t_rdata", "t_rlast"}


def configuration(kind, completion):
    target = dict(name="t", clock_domain="main", protocol=kind, data_width=32,
                  mappings=[dict(base="0x0", size="0x1000")],
                  timeout=dict(cycles=32 if kind == "tl_ul" else 16, completion=completion))
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
                            + ("=0;" if incoming and alias not in DRIVEN else ";"))
    if kind.startswith("apb"):
        kind = "apb"
    cycles = 32 if kind == "tl_ul" else 16
    body = Path(__file__).with_suffix(".sv").read_text()
    return (f"`timescale 1ns/1ps\n`define TARGET_{kind.upper()}\nmodule tb;\n"
            + "\n".join(declarations)
            + f"\nnocloom_target_watchdog dut({','.join(connections)});\n"
            + f"localparam COMPLETION={int(completion)}, CYCLES={cycles};\n" + body + "\nendmodule\n")


def faulted(rtl, fixture, config, work):
    probe = work / "probe"
    subprocess.run([str(fixture.resolve()), "tilelink_uh_probe", str(probe),
                    "--param", f"config={config}", "--list-bindings"], check=True, timeout=30)
    cells = probe / "cells.v"
    probe_rtl = (probe / "network.v").read_text() + (cells.read_text() if cells.is_file() else "")
    if probe_rtl != rtl:
        raise RuntimeError("Probe RTL differs from generated RTL")
    bindings = json.loads((probe / "bindings.json").read_text())["dut"]
    key, = (key for key in bindings if key.endswith("/timeout.waiting"))
    net = f"nl_internal_v{bindings[key]['index']}"
    rtl, count = re.subn(rf"^assign {net} = (.*);$",
                         rf"assign {net} = watchdog_fault==1 ? 1'b0 : "
                         rf"(\1) | (watchdog_fault==2 && partial_write && !nl_i_s_wvalid);",
                         rtl, flags=re.M)
    if count != 1:
        raise RuntimeError(f"Expected one wait expression, found {count}")
    start = rtl.index("\n", rtl.index(");", rtl.index("module nocloom_target_watchdog")))
    return (rtl[:start] + """
integer watchdog_fault;
reg partial_write;
initial if (!$value$plusargs("FAULT=%d", watchdog_fault)) watchdog_fault = 0;
always @(posedge clk or negedge reset_n)
  if (!reset_n) partial_write <= 0;
  else if (nl_i_s_wvalid && nl_i_s_wready) partial_write <= !nl_i_s_wlast;
""" + rtl[start:])


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
        rtl = faulted(rtl, args.fixture, config, work)
    (work / "tb.sv").write_text(bench(mapping, args.kind, completion))
    (work / "network.v").write_text(rtl)
    simulation = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                       compile_timeout=30, run_timeout=10, success=("WATCHDOG_PASS",))
    scenarios = (0, 1) if args.baseline else (0, 1, 2, 3, 4)
    if args.kind in ("wb_pipelined", "ahb_lite", "tl_ul") and not args.baseline:
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
        for fault, scenario, check in ((1, 2, "MISSING_EXPIRY"), (2, 1, "EARLY_EXPIRY")):
            result = simulation.run([f"+SCENARIO={scenario}", f"+FAULT={fault}"])
            print(result.stdout, end="", flush=True)
            if result.returncode == 0 or f"CHECK_FAIL {check}" not in result.stdout:
                return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--fixture", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true")
    group.add_argument("--negative", action="store_true")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    if args.negative and not args.fixture:
        parser.error("--negative requires --fixture")
    args.work.mkdir(parents=True, exist_ok=True)
    if args.kind == "tl_ul":
        return run(args, False)
    return run(args, True) or (0 if args.baseline or args.negative else run(args, False))


if __name__ == "__main__":
    raise SystemExit(main())
