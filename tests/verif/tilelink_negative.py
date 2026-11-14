"""TileLink source negative control and direct peer byte-mask transactions."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sim_engine  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args, network = parser.parse_known_args()
    simulation = []
    if args.sim:
        simulation += ["--sim", args.sim]
    if args.sim_config:
        simulation += ["--sim-config", args.sim_config]
    result = subprocess.run([sys.executable, str(HERE / "nlverif/run.py"), *network,
                             *simulation, "--work", str(args.work / "network")])
    if result.returncode:
        return result.returncode
    work = Path(tempfile.mkdtemp(prefix="peer-", dir=args.work))
    (work / "tb.sv").write_text(args.peer.read_text())
    sv = HERE / "sv"
    common = (sv / "nlv_common.svh").read_text()
    rtl = "".join((sv / name).read_text().replace('`include "nlv_common.svh"', common)
                  for name in ("nlv_tl_checker.sv", "nlv_ahb_slave.sv", "nlv_tl_slave.sv"))
    (work / "peer.sv").write_text(rtl)
    sim = sim_engine.Simulation(work, "peer.sv", engine=args.sim, config=args.sim_config,
                                compile_timeout=120, run_timeout=60, success=("NLV_DONE",))
    result = sim.run([f"+NLV_EVENTS={work.resolve() / 'events.jsonl'}"])
    if result.returncode:
        return 1
    events = [json.loads(line) for line in (work / "events.jsonl").read_text().splitlines()]
    writes = [e for e in events if e["k"] == "ahb_data" and e["w"]]
    assert len(writes) == 39 and all("s" in e for e in writes)
    assert sum(int(e["s"], 16) == 0 for e in writes) == 7
    print("TileLink peer masks: 39 writes, including 7 zero masks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
