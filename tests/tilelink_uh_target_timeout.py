"""Watchdog idle state, gated sleep and request wakeup at TileLink targets."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import sim_engine
from tilelink_uh_target_ordering import bench, configuration


def timer_probe(args, work):
    work.mkdir()
    subprocess.run([str(args.fixture.resolve()), str(work), "contexts", "64",
                    str(args.capacity), str((args.capacity-1).bit_length())],
                   check=True, timeout=60)
    ports = [line.split() for line in (work / "ports.tsv").read_text().splitlines()]
    declarations, connections = [], []
    for name, width, incoming in ports:
        declarations.append(f"{'reg' if incoming == '1' else 'wire'} [{int(width)-1}:0] {name}"
                            + ("=0;" if incoming == "1" else ";"))
        connections.append(f".{name}({name})")
    pending = [name for name, _, _ in ports if name.endswith("timeout_x_pending")]
    assert len(pending) == args.capacity, pending
    checks = "\n".join(f'if({name} !== 0) $fatal(1,"CHECK_FAIL TIMER_IDLE {name}");'
                       for name in pending)
    (work / "tb.sv").write_text("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
        + "\nnocloom_tl_uh_outlet_probe dut(" + ",".join(connections) + ");\n"
        + "always #5 nl_clk=~nl_clk;\ninitial begin repeat(160) @(negedge nl_clk); nl_reset_n=1;"
        + "repeat(20) @(negedge nl_clk);\n" + checks
        + '\n$display("UH_PASS watchdog idle"); $finish; end\nendmodule\n')
    result = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                    success=("UH_PASS",)).run()
    print(result.stdout, work, flush=True)
    return result.returncode


def gated_probe(args, work):
    work.mkdir()
    cfg = configuration(args.capacity)
    cfg["clock_gating"] = True
    cfg["targets"][0].update(clock_gating=dict(protocol=True, transport=True), timeout=dict(cycles=16, completion=False))
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                    "--out-dir", str(generated)], check=True, timeout=60)
    mapping = json.loads((generated / "nocloom_uh_target.map.json").read_text())
    gate = next(g for g in mapping["network"]["clock_gates"]
                if g.get("target") == "t" and g["zone"] == "protocol")
    wire = lambda value: "nl_internal_v" + value.split("/")[1]
    rtl = sim_engine.generated_rtl(generated / "nocloom_uh_target.v")
    clock = re.search(r"^nocloom_clock_gate .*?\.enable\(" + wire(gate["enable"])
                      + r"\).*?\.clock_out\((\w+)\)", rtl, re.M)[1]
    body = Path(__file__).with_suffix(".sv").read_text()
    body = body.replace("@REMAINING@", "dut." + wire(gate["remaining"]))
    body = body.replace("@CLOCK@", "dut." + clock)
    (work / "tb.sv").write_text(bench(mapping, body))
    (work / "network.v").write_text(rtl)
    result = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                    success=("UH_PASS",)).run()
    print(result.stdout, work, flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--capacity", required=True, type=int)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="timeout-", dir=args.work)).resolve()
    # Check timer state and generated protocol gate behavior.
    gated = gated_probe(args, work / "gated")
    timer = timer_probe(args, work / "timer")
    return gated or timer


if __name__ == "__main__":
    raise SystemExit(main())
