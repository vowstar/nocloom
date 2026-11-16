"""Fill the target packet pool through AXI and return D messages in reverse order."""
import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

import tilelink_uh_target
import sim_engine


def run(args, capacity):
    work = Path(tempfile.mkdtemp(prefix="capacity-", dir=args.work)).resolve()
    cfg = runpy.run_path(str(args.case))["configuration"](64)
    cfg["targets"] = cfg["targets"][:1]
    cfg["targets"][0]["max_outstanding"] = capacity
    cfg["initiators"][0].update(max_outstanding=capacity+1, id_width=5)
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True, timeout=60)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stderr, work)
        return result.returncode
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    (work / "tb.sv").write_text(tilelink_uh_target.bench(mapping, args.bench.read_text(), 0))
    sv = Path(__file__).parent / "sv"
    rtl = sim_engine.generated_rtl(generated / "nocloom_uh_target.v")
    rtl += (sv / "nlv_tl_checker.sv").read_text() + (sv / "nlv_tl_uh_axi_check.sv").read_text()
    (work / "network.v").write_text(rtl)
    result = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
        compile_timeout=180, run_timeout=120, success=("UH_CONTEXTS_PASS",)).run()
    print(result.stdout, work, flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    for capacity in (3, 16):
        result = run(args, capacity)
        if result:
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
