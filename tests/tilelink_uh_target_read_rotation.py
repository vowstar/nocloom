"""Check read response rotation among three TileLink UH target contexts."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import sim_engine


CONFIG = """schema_version: 10
name: uh_read_rotation
address_width: 16
reset_ports: [{name: reset_n}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, gated: false}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 4,
     len_width: 4, max_outstanding: 16}
targets:
  - {name: t0, clock_domain: main, protocol: tl_uh, data_width: 32, max_outstanding: 3,
     mappings: [{base: '0x0', size: '0x8000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
"""


def tie_read_turn(rtl, bindings):
    turn = bindings["dut"]["burst_service/t0/tl.read_turn"]
    if turn["op"] != "State":
        raise RuntimeError("Read turn binding is not a register")
    net = f"nl_internal_v{turn['index']}"
    # Leave the reset assignment in place and replace the single live update.
    faulted, count = re.subn(rf"^( +{net} <= )nl_internal_v\d+;", rf"\g<1>{turn['width']}'d0;",
                             rtl, flags=re.M)
    if count != 1:
        raise RuntimeError(f"Expected one read turn update, found {count}")
    return faulted


def simulate(work, rtl, args):
    work.mkdir()
    (work / "network.v").write_text(rtl)
    (work / "tb.sv").write_bytes(Path(__file__).with_suffix(".sv").read_bytes())
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=25, run_timeout=5, success=("UH_PASS",))
    result = sim.run()
    print(result.stdout, end="", flush=True)
    return result, sim.record["raw_exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rotation-", dir=args.work)).resolve()
    print(f"Work directory: {work}", flush=True)
    definition = work / "input.yaml"
    definition.write_text(CONFIG)
    generated = work / "generated"
    subprocess.run([args.cli.resolve(), "generate", definition, "--out-dir", generated],
                   check=True, timeout=10)
    network = generated / "nocloom_uh_read_rotation.v"
    rtl = sim_engine.generated_rtl(network)
    baseline, _ = simulate(work / "baseline", rtl, args)
    if baseline.returncode:
        return 1

    probe = work / "probe"
    subprocess.run([args.fixture.resolve(), "tilelink_uh_probe", probe, "--param",
                    f"config={definition}", "--list-bindings"], check=True, timeout=10)
    if (probe / "network.v").read_bytes() != network.read_bytes():
        raise RuntimeError("Fixture RTL differs from CLI RTL")
    bindings = json.loads((probe / "bindings.json").read_text())
    control, exit_code = simulate(work / "turn_zero", tie_read_turn(rtl, bindings), args)
    if (exit_code != 0 or control.returncode == 0 or
            "CHECK_FAIL READ_ROTATION got=0,13,12,14" not in control.stdout):
        raise RuntimeError("Tying the read turn to zero did not produce the expected order")
    print("READ_ROTATION_CONTROL_CAUGHT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
