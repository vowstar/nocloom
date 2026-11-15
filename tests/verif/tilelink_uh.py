"""Raw TileLink messages through generated RTL with byte and ownership checks."""
import argparse
import json
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine



def bench(mapping, body):
    declarations, connections = [], []
    for port in mapping["ports"]:
        name = port["name"]
        width = port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_t_", "t_")
        connections.append(f".{name}({alias})")
        declarations.append(f"{'reg' if port['direction']=='input' else 'wire'} [{width-1}:0] {alias}"
                            + ("=0;" if port["direction"]=="input" else ";"))
    source_width = next((p["width"] for p in mapping["ports"] if p["name"]=="nl_i_s_a_source"), 0)
    if not source_width:
        declarations += ["reg s_a_source=0;", "wire s_d_source=0;"]
    return ("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
            + "\nnocloom_uh_ingress dut(" + ",".join(connections) + ");\n"
            + f"localparam MAX_SIZE={int(mapping['definition']['tilelink']['initiators']['s']['max_transfer_bytes']).bit_length()-1}, SOURCE_BITS={source_width};\n"
            + body + "\nendmodule\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--maximum", type=int, default=1024)
    parser.add_argument("--source-width", type=int, default=4)
    parser.add_argument("--slots", type=int, default=4)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    case = runpy.run_path(str(args.case))
    cfg = case["configuration"](args.maximum, args.source_width, args.slots)
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True, timeout=60)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stderr)
        return 1
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    (work / "tb.sv").write_text(bench(mapping, args.bench.read_text()))
    checker = Path(__file__).parent / "sv/nlv_tl_checker.sv"
    (work / "network.v").write_text(sim_engine.generated_rtl(generated / "nocloom_uh_ingress.v")
                                     + checker.read_text())
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=180, run_timeout=90, success=("UH_PASS",))
    result = sim.run()
    print(result.stdout)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
