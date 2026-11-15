"""Runtime TileLink UH product faults with a fault-disabled trace comparison."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile

import tilelink_uh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

HERE = Path(__file__).resolve().parent


def substitute(rtl, pattern, replacement):
    result, count = re.subn(pattern, replacement, rtl, flags=re.M)
    if count != 1:
        raise RuntimeError(f"fault anchor matched {count} times: {pattern}")
    return result


def faulted(rtl, bindings):
    def net(name):
        return "nl_internal_v" + str(bindings["source/s/tl." + name]["index"])

    def output(name, selector, value):
        nonlocal rtl
        rtl = substitute(
            rtl,
            rf"^(assign {name} = )([^;]+);$",
            lambda m: (
                f"{m[1]}(nlv_fault_sel=={selector} ? ({value(m[2])}) : ({m[2]}));"
            ),
        )

    output("nl_i_s_d_source", 1, lambda v: f"{v} ^ (nlv_d_beat!=0 ? 4'd1:4'd0)")
    output("nl_i_s_d_denied", 2, lambda v: f"{v} && nlv_d_beat==0")
    for slot in range(8):
        name = net(f"slot{slot}.a_done")
        rtl = substitute(
            rtl,
            rf"^(\s*{name} <= )(nl_internal_v\d+);$",
            lambda m: f"{m[1]}(nlv_fault_sel==3 ? 1'b1 : {m[2]});",
        )
    output("nl_t_t_arlen", 4, lambda v: f"{v} + 8'd1")
    output(
        "nl_t_t_arvalid",
        5,
        lambda v: f"{v} || (nl_i_s_a_valid && nl_i_s_a_ready && nl_i_s_a_opcode==5)",
    )
    output("nl_t_t_bready", 6, lambda v: f"{v} && !nl_i_s_d_valid")
    for queue in range(2):
        name = net(f"q{queue}.mask")
        rtl = substitute(
            rtl,
            rf"^(\s*{name} <= )(nl_internal_v\d+);$",
            lambda m: (
                f"{m[1]}(nlv_fault_sel==7 && {net('collecting')} && !{net('collect_local')} ? 4'hf : {m[2]});"
            ),
        )
    name = net("w_push")
    rtl = substitute(
        rtl,
        rf"^(assign {name} = )([^;]+);$",
        lambda m: (
            f"{m[1]}({m[2]} || (nlv_fault_sel==8 && {net('collecting')} && nl_i_s_a_valid && nl_i_s_a_ready));"
        ),
    )
    end = rtl.index("\n", rtl.index(");", rtl.index("module nocloom_uh_ingress")))
    controls = """
integer nlv_fault_sel=0, nlv_d_beat=0;
initial if (!$value$plusargs("FAULT=%d",nlv_fault_sel)) nlv_fault_sel=0;
always @(posedge clk) begin
  if(!reset_n) nlv_d_beat<=0;
  else if(nl_i_s_d_valid && nl_i_s_d_ready) begin
    if(nl_i_s_d_opcode!=1 || nl_i_s_d_size<=2 || nlv_d_beat+1==(1<<(nl_i_s_d_size-2))) nlv_d_beat<=0;
    else nlv_d_beat<=nlv_d_beat+1;
  end
end
"""
    return rtl[:end] + controls + rtl[end:]


def trace_hash(output):
    events = [line for line in output.splitlines() if line.startswith("UH_EVENT")]
    if not events:
        raise RuntimeError("no protocol events")
    return hashlib.sha256("\n".join(events).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cli", "fixture", "work", "bench", "case"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--extra", type=Path)
    parser.add_argument("--maximum", type=int, default=4096)
    parser.add_argument("--fault", type=int, action="append", metavar="SELECTOR")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    case = runpy.run_path(str(args.case))
    faults = case["FAULTS"]
    if args.fault:
        unknown = set(args.fault).difference(faults)
        if unknown:
            parser.error(f"unknown fault selectors: {sorted(unknown)}")
        faults = {selector: faults[selector] for selector in args.fault}
    cfg = case["configuration"](args.maximum, 4, 8)
    cfg["initiators"][0]["sink_width"] = 0
    cfg["targets"][0].update(max_outstanding=8, read_side_effects=True)
    config = root / "input.yaml"
    config.write_text(json.dumps(cfg))
    generated = root / "generated"
    subprocess.run(
        [str(args.cli.resolve()), "generate", str(config), "--out-dir", str(generated)],
        check=True,
    )
    fixture = root / "fixture"
    subprocess.run(
        [
            str(args.fixture.resolve()),
            "tilelink_uh_probe",
            str(fixture),
            "--param",
            f"config={config}",
            "--list-bindings",
        ],
        check=True,
    )
    original = (generated / "nocloom_uh_ingress.v").read_text()
    if original != (fixture / "network.v").read_text():
        raise RuntimeError("fixture differs from the CLI RTL")
    bindings = json.loads((fixture / "bindings.json").read_text())["dut"]
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    body = args.bench.read_text()
    if args.extra:
        prefix = '    $display("UH_PASS'
        body = body.replace(prefix, args.extra.read_text() + "\n" + prefix)
    body += """
  always @(posedge clk) if(reset_n) begin
    if(s_a_valid && s_a_ready) $display("UH_EVENT A %d %d %d %d %h %h",cycle,s_a_source,s_a_opcode,s_a_size,s_a_mask,s_a_data);
    if(s_d_valid && s_d_ready) $display("UH_EVENT D %d %d %d %d %d %d %h",cycle,s_d_source,s_d_opcode,s_d_size,s_d_denied,s_d_corrupt,s_d_data);
    if(t_awvalid && t_awready) $display("UH_EVENT AW %d %h %h %h",cycle,t_awaddr,t_awid,t_awlen);
    if(t_arvalid && t_arready) $display("UH_EVENT AR %d %h %h %h",cycle,t_araddr,t_arid,t_arlen);
    if(t_wvalid && t_wready) $display("UH_EVENT W %d %h %h %h",cycle,t_wdata,t_wstrb,t_wlast);
    if(t_bvalid && t_bready) $display("UH_EVENT B %d %h %h",cycle,t_bid,t_bresp);
    if(t_rvalid && t_rready) $display("UH_EVENT R %d %h %h %h",cycle,t_rid,t_rdata,t_rlast);
  end
"""
    tb = tilelink_uh.bench(mapping, body, cfg, 0, False)
    checks = (
        (fixture / "cells.v").read_text()
        + (HERE / "sv/nlv_tl_checker.sv").read_text()
        + (HERE / "sv/nlv_tl_uh_axi_check.sv").read_text()
    )
    normal = root / "normal"
    normal.mkdir()
    (normal / "tb.sv").write_text(tb)
    (normal / "network.v").write_text(original + checks)
    base = sim_engine.Simulation(
        normal,
        "network.v",
        engine=args.sim,
        config=args.sim_config,
        success=("UH_PASS",),
        compile_timeout=180,
        run_timeout=120,
    ).run([])
    if base.returncode:
        raise RuntimeError(f"normal RTL failed: {base.stdout[-2000:]}")
    baseline_hash = trace_hash(base.stdout)
    work = root / "faults"
    work.mkdir()
    (work / "tb.sv").write_text(tb)
    (work / "network.v").write_text(faulted(original, bindings) + checks)
    sim = sim_engine.Simulation(
        work,
        "network.v",
        engine=args.sim,
        config=args.sim_config,
        success=("UH_PASS",),
        compile_timeout=180,
        run_timeout=120,
    )
    disabled = sim.run(["+FAULT=0"])
    if disabled.returncode or trace_hash(disabled.stdout) != baseline_hash:
        raise RuntimeError("fault-disabled trace differs")
    rows = [dict(selector=0, status="PASS", trace_sha256=baseline_hash)]
    for selector, (name, rules) in faults.items():
        result = sim.run([f"+FAULT={selector}"])
        caught = (
            result.returncode != 0
            and "ASSERT_FAIL:" in result.stdout
            and any(rule in result.stdout for rule in rules)
        )
        row = dict(
            selector=selector,
            fault=name,
            status="CAUGHT" if caught else "SURVIVED",
            assertion=next(
                (line for line in result.stdout.splitlines() if "ASSERT_FAIL:" in line),
                "",
            ),
        )
        rows.append(row)
        (work / f"fault-{selector}.log").write_text(result.stdout)
        print(json.dumps(row), flush=True)
    (root / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["status"] in ("PASS", "CAUGHT") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
