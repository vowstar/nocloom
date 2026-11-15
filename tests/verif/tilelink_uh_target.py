"""AXI byte footprints and TileLink ownership through generated RTL."""
import argparse
import json
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine


def bench(mapping, body, address_base):
    declarations, connections, assignments = ["wire same_ack;"], [], []
    for port in mapping["ports"]:
        name, width = port["name"], port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_t_", "t_").replace("nl_t_q_", "q_")
        connections.append(f".{name}({alias})")
        variable = port["direction"] == "input"
        held = "model_" + alias if alias.startswith("t_d_") and variable else alias
        declarations.append(f"{'reg' if variable else 'wire'} [{width-1}:0] {held}"
                            + ("=0;" if variable else ";"))
        if held != alias:
            direct = {"t_d_valid": "1'b1", "t_d_opcode": "3'd0",
                      "t_d_size": "t_a_size", "t_d_denied": "denied"}.get(alias, "0")
            declarations.append(f"wire [{width-1}:0] {alias};")
            assignments.append(f"assign {alias}=same_ack ? {direct} : {held};")
            body = body.replace(alias + "=", held + "=")
            body = body.replace(alias + "[b", held + "[b")
    assignments.append("assign same_ack=early==2 && !put_active && t_a_valid && t_a_ready && t_a_opcode!=4;")
    maximum = mapping["definition"]["tilelink"]["targets"]["t"]["max_transfer_bytes"]
    source_width = mapping["definition"]["tilelink"]["targets"]["t"]["source_width"]
    size_width = mapping["definition"]["tilelink"]["targets"]["t"]["size_width"]
    if source_width == 0:
        declarations.extend(["wire t_a_source=0;", "wire t_d_source=0;"])
    return ("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
            + "\nnocloom_uh_target dut(" + ",".join(connections) + ");\n"
            + f"localparam MAX_BYTES={maximum}, SOURCE_WIDTH={max(1, source_width)}, SIZE_WIDTH={size_width}, ADDRESS_BASE=32'h{address_base:x};\n" + body + "\n"
            + "\n".join(assignments) + "\nendmodule\n")


def run(args):
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    cfg = runpy.run_path(str(args.case))["configuration"](args.maximum)
    if args.source_width is not None:
        cfg["targets"][0]["source_width"] = args.source_width
    if args.size_width is not None:
        cfg["targets"][0]["size_width"] = args.size_width
    elif args.all_maxima:
        cfg["targets"][0]["size_width"] = max(2, (args.maximum.bit_length()-1).bit_length())
    address_base = 0xffff0000 if args.high_address else 0
    cfg["targets"][0]["mappings"][0]["base"] = hex(address_base)
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True, timeout=60)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stderr)
        return result.returncode
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    sv = Path(__file__).parent / "sv"
    (work / "tb.sv").write_text(bench(mapping, args.bench.read_text(), address_base))
    rtl = sim_engine.generated_rtl(generated / "nocloom_uh_target.v")
    rtl += (sv / "nlv_tl_checker.sv").read_text()
    rtl += (sv / "nlv_tl_uh_axi_check.sv").read_text()
    (work / "network.v").write_text(rtl)
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=180, run_timeout=120, success=("UH_PASS",))
    result = sim.run()
    print(result.stdout)
    print(work)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--maximum", type=int, default=1024)
    parser.add_argument("--source-width", type=int)
    parser.add_argument("--size-width", type=int)
    parser.add_argument("--high-address", action="store_true")
    sim_engine.add_arguments(parser)
    parser.add_argument("--all-maxima", action="store_true")
    args = parser.parse_args()
    for maximum in ([1 << n for n in range(2, 11)] if args.all_maxima else [args.maximum]):
        args.maximum = maximum
        result = run(args)
        if result:
            return result
    if args.all_maxima and args.source_width is None:
        args.source_width = 16
        return run(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
