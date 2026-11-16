"""Read progress and write response order through the generated target."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import sim_engine


def configuration(capacity):
    return dict(schema_version=10, name="uh_target", address_width=32,
                clock_gating=False, reset_ports=[dict(name="reset_n")],
                test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk",
                                    reset_port="reset_n", test_port="test")],
                initiators=[dict(name="s", clock_domain="main", protocol="axi4",
                                 data_width=32, id_width=4, len_width=8,
                                 max_outstanding=8)],
                targets=[dict(name="t", clock_domain="main", protocol="tl_uh",
                              data_width=32, size_width=4, max_transfer_bytes=64,
                              max_outstanding=capacity,
                              mappings=[dict(base="0x0", size="0x10000")])],
                connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def bench(mapping, body):
    declarations, connections = [], []
    for port in mapping["ports"]:
        name, width = port["name"], port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_t_", "t_")
        connections.append(f".{name}({alias})")
        incoming = port["direction"] == "input"
        declarations.append(f"{'reg' if incoming else 'wire'} [{width-1}:0] {alias}"
                            + ("=0;" if incoming else ";"))
    capacity = mapping["definition"]["tilelink"]["targets"]["t"]["max_outstanding"]
    if capacity == 1:
        declarations += ["wire t_a_source=0;", "reg t_d_source=0;"]
    storage = next(s for s in mapping["network"]["storage"]
                   if s["resource"] == "burst_service/t/tilelink")
    wire = lambda value: "dut.nl_internal_v" + value.split("/")[1]
    monitor = ("reg release_overlap=0;\nalways @(posedge clk) if(reset_n && "
               + wire(storage["acquire"]) + " && " + wire(storage["release"])
               + ") release_overlap<=1;\n")
    return ("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
            + f"\nnocloom_uh_target dut({','.join(connections)});\n"
            + f"localparam K={capacity}, DB=4, LDB=2;\n" + monitor + body + "\nendmodule\n")


def run(args):
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ordering-", dir=args.work)).resolve()
    capacity = 3 if args.case == "write_random" else 2
    (work / "input.yaml").write_text(json.dumps(configuration(capacity)))
    generated = work / "generated"
    subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                    "--out-dir", str(generated)], check=True, timeout=60)
    mapping = json.loads((generated / "nocloom_uh_target.map.json").read_text())
    body = Path(__file__).with_suffix(".sv").read_text()
    (work / "tb.sv").write_text(bench(mapping, body))
    (work / "network.v").write_text(sim_engine.generated_rtl(generated / "nocloom_uh_target.v")
                                   + (Path(__file__).parent / "verif/sv/nlv_tl_checker.sv").read_text())
    simulation = sim_engine.Simulation(work, "network.v", engine=args.sim,
                                       config=args.sim_config, compile_timeout=180,
                                       run_timeout=60, success=("UH_PASS",))
    cases = (["+MODE=1", "+DET=1", "+DMAX=0", "+RP=100", "+D=6"]
             if args.case == "read_release" else
             ["+seed=5", "+NR=40", "+NW=0", "+IDS=1", "+GAP=40"])
    if args.case == "write_random":
        cases = ["+seed=8", "+IDS=1", "+RIDS=8", "+RBASE=8", "+DEN=1",
                 "+STALL=20", "+LMAX=15"]
    result = simulation.run(cases)
    print(result.stdout, work, flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--case", required=True, choices=("read_release", "read_random", "write_random"))
    sim_engine.add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
