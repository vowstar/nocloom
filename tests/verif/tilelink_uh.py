"""Raw TileLink messages through generated RTL with byte and ownership checks."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine
from nlverif.config import ENDPOINT_GATE_SETTINGS


def target_mux(pins, count):
    lines = ["integer w_target=0, read_target=0, b_target=0;",
             "integer b_targets[0:4095];"]
    for channel in ("aw", "ar"):
        choice = "0"
        for k in range(count-1,-1,-1):
            choice = f"t{k}_{channel}valid ? {k} : ({choice})"
        lines.append(f"wire [1:0] {channel}_select={choice};")
    for port in pins:
        if port["endpoint"] != "target:t0":
            continue
        field, width = port["signal"], port["width"]
        if field in ("awaddr", "araddr"):
            width = 32
        if port["direction"] == "input":
            lines.append(f"reg [{width-1}:0] t_{field}=0;")
            for k in range(count):
                value = f"t_{field}"
                owner = ({"awready": "aw_select", "wready": "w_target", "arready": "ar_select",
                          "bvalid": "b_target", "rvalid": "read_target"}).get(field)
                if owner:
                    value += f" && {owner}=={k}"
                lines.append(f"assign t{k}_{field}={value};")
        else:
            if field in ("awvalid", "arvalid"):
                value = " | ".join(f"t{k}_{field}" for k in range(count))
            else:
                owner = ("aw_select" if field.startswith("aw") else "ar_select" if field.startswith("ar")
                         else "w_target" if field.startswith("w") else "b_target" if field.startswith("b")
                         else "read_target")
                value = "0"
                for k in range(count-1,-1,-1):
                    value = f"{owner}=={k} ? t{k}_{field} : ({value})"
                if field in ("awaddr", "araddr"):
                    value = f"({value}) + 4096 + ({owner} * 1024)"
            lines.append(f"wire [{width-1}:0] t_{field}={value};")
    return "\n".join(lines)

def axi_checks(pins, cfg):
    text = []
    fields = "awaddr araddr awid arid bid rid awlen arlen awsize arsize awprot arprot awburst arburst bresp rresp awcache arcache awlock arlock awvalid awready arvalid arready wdata rdata wstrb wlast wvalid wready bvalid bready rlast rvalid rready".split()
    for target in cfg["targets"]:
        name = target["name"]
        own = {p["signal"]: p for p in pins if p["endpoint"]=="target:"+name}
        params = dict(ADDR_W=own["awaddr"]["width"], DATA_W=own["wdata"]["width"],
                      ID_W=own["awid"]["width"], LEN_W=own["awlen"]["width"])
        parameters = ",".join(f".{k}({v})" for k,v in params.items())
        ports = ",".join(f".{field}({name}_{field})" for field in fields)
        text.append(f'nlv_tl_uh_axi_check #(.NAME("{name}"),{parameters}) check_{name} '
                    f'(.clk(target_clk),.rst_n(reset_n),{ports});')
    return "\n".join(text)

def ul_target(body):
    start = body.index("  always @(posedge target_clk)")
    end = body.index("  task expect_message", start)
    model = r"""
  always @(posedge target_clk) begin
    if(!reset_n) begin t_a_ready<=0; t_d_valid<=0; end
    else begin
      t_a_ready<=!t_d_valid && cycle%5!=0;
      if(t_d_valid && t_d_ready) begin
        t_d_valid<=0;
        if(t_d_opcode==1) r_count=r_count+1;
        else b_count=b_count+1;
      end
      if(t_a_valid && t_a_ready) begin
        t_a_ready<=0; t_d_valid<=1; t_d_size<=t_a_size;
        t_d_param<=0; t_d_denied<=0; t_d_corrupt<=0; t_d_data<=0;
        beat_address=t_a_address & ~3;
        if(t_a_opcode==4) begin
          ar_count=ar_count+1; t_d_opcode<=1; bus_word=0;
          for(integer b=0;b<4;b=b+1) bus_word[b*8+:8]=memory[beat_address+b];
          t_d_data<=bus_word;
          for(integer b=0;b<(1<<t_a_size);b=b+1)
            actual_reads[t_a_address+b]=actual_reads[t_a_address+b]+1;
        end else begin
          aw_count=aw_count+1; w_count=w_count+1; t_d_opcode<=0;
          for(integer b=0;b<4;b=b+1)
            if(t_a_mask[b]) memory[beat_address+b]=t_a_data[b*8+:8];
        end
      end
    end
  end
  nlv_tl_checker #(.NAME("ul_target"), .SOURCE_W(1), .SIZE_W(2)) check_ul (
    .clk(target_clk), .rst_n(reset_n), .allow_reject(1'b0),
    .a_opcode(t_a_opcode), .a_param(t_a_param), .a_size(t_a_size), .a_source(1'b0),
    .a_address(t_a_address), .a_mask(t_a_mask), .a_data(t_a_data), .a_corrupt(t_a_corrupt),
    .a_valid(t_a_valid), .a_ready(t_a_ready),
    .d_opcode(t_d_opcode), .d_param(t_d_param), .d_size(t_d_size), .d_source(1'b0),
    .d_data(t_d_data), .d_corrupt(t_d_corrupt), .d_denied(t_d_denied),
    .d_valid(t_d_valid), .d_ready(t_d_ready));
"""
    return body[:start] + model + body[end:]

def bench(mapping, body, cfg, windows, miss_last, bus_model=None):
    axi = cfg["targets"][0]["protocol"]=="axi4"
    declarations, connections = [], []
    pins = mapping["ports"]
    for port in pins:
        name, width = port["name"], port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_", "")
        connections.append(f".{name}({alias})")
        variable = port["direction"]=="input" and not (windows and port["endpoint"].startswith("target:"))
        declarations.append(f"{'reg' if variable else 'wire'} [{width-1}:0] {alias}"
                            + ("=0;" if variable else ";"))
    source_width = next((p["width"] for p in pins if p["name"]=="nl_i_s_a_source"), 0)
    size_width = next(p["width"] for p in pins if p["name"]=="nl_i_s_a_size")
    bus_width = next(p["width"] for p in pins if p["signal"]=="wdata") if axi else 32
    if not any(p["name"]=="nl_i_s_d_sink" for p in pins):
        declarations.append("wire s_d_sink=0;")
    if not source_width:
        declarations += ["reg s_a_source=0;", "wire s_d_source=0;"]
    if not any(p["name"]=="clk_far" for p in pins):
        declarations += ["reg clk_far=0;"]
    if not axi:
        declarations += ["wire [31:0] t_wdata=0;", "wire [31:0] t_rdata=0;"]
        body = (ul_target(body) if cfg["targets"][0]["protocol"]=="tl_ul" else
                runpy.run_path(str(bus_model))["replace_model"](body,cfg["targets"][0]["protocol"]))
    if windows:
        declarations.append(target_mux(pins, len(cfg["targets"])))
        body = body.replace("t_awready<=0; writing=1;", "t_awready<=0; w_target=aw_select; writing=1;")
        body = body.replace("t_arready<=0; reading=1;", "t_arready<=0; read_target=ar_select; reading=1;")
        body = body.replace("writing=0; b_ids[b_write]", "writing=0; b_targets[b_write]=w_target; b_ids[b_write]")
        body = body.replace("t_bvalid<=1; t_bid", "b_target<=b_targets[b_read]; t_bvalid<=1; t_bid")
    maximum = mapping["definition"]["tilelink"]["initiators"]["s"]["max_transfer_bytes"]
    target_clock = "clk_far" if cfg["targets"][0]["clock_domain"]=="far" else "clk"
    return ("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
            + "\nnocloom_uh_ingress dut(" + ",".join(connections) + ");\n"
            + f"localparam TARGET_AXI={int(axi)}, MAX_SIZE={maximum.bit_length()-1}, WINDOWS={windows}, MISS_LAST={int(miss_last)}, SOURCE_BITS={source_width}, MESSAGE_SLOTS={cfg['initiators'][0]['max_outstanding']};\n"
            + f"localparam BUS_BITS={bus_width}, SIZE_BITS={size_width};\n"
            + f"wire target_clk={target_clock};\n" + (axi_checks(pins, cfg) if axi else "") + "\n" + body + "\nendmodule\n")


def run(args, maximum):
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    case = runpy.run_path(str(args.case))
    cfg = case["configuration"](maximum, args.source_width, args.slots)
    cfg["initiators"][0]["sink_width"] = args.sink_width
    if args.gating:
        cfg["initiators"][0]["clock_gating"] = ENDPOINT_GATE_SETTINGS[args.gating]
    cfg["targets"][0].update(data_width=args.target_width, read_side_effects=True,
                              max_outstanding=max(4, args.slots))
    if args.all_maxima and not args.oversize:
        cfg["initiators"][0]["size_width"] = 2 if maximum<=8 else 3 if maximum<=128 else 4
    if args.target_protocol=="tl_ul":
        target = cfg["targets"][0]
        for field in ("attributes", "id_width", "len_width"):
            target.pop(field)
        target.update(protocol="tl_ul", max_outstanding=1)
    if args.target_protocol in ("apb4", "ahb_lite", "wishbone"):
        target=cfg["targets"][0]
        for field in ("id_width", "len_width", "read_side_effects"):
            target.pop(field,None)
        target.update(protocol=args.target_protocol,max_outstanding=1)
        if args.target_protocol=="wishbone":
            target.pop("attributes")
            target["address_unit"]="byte"
    if args.windows:
        template = cfg["targets"][0]
        cfg["targets"] = []
        for k in range(3 if args.miss_last else 4):
            target = deepcopy(template)
            target.update(name=f"t{k}", mappings=[dict(base=hex(4096+1024*k), size="0x400")])
            cfg["targets"].append(target)
    if args.cdc:
        cfg["clock_domains"][0]["period_ps"] = 10000
        cfg["clock_domains"].append(dict(name="far", clock_port="clk_far", reset_port="reset_n",
                                          test_port="test", period_ps=17000))
        cfg["targets"][0]["clock_domain"] = "far"
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint="s", node="q")], exits=[dict(endpoint="t", node="q")]),
            response=dict(nodes=[dict(name="r", clock_domain="far")], links=[],
                          entries=[dict(endpoint="t", node="r")], exits=[dict(endpoint="s", node="r")]))
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True, timeout=60)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        print(result.stderr)
        return result.returncode
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    profile = mapping["definition"]["tilelink"]["initiators"]["s"]
    assert profile["max_outstanding"] == args.slots
    assert profile["internal_transaction_capacity"] == min(64, max(4, args.slots))
    assert mapping["definition"]["outstanding"]["initiators"]["s"] == args.slots
    fixture_rtl = None
    if args.gating:
        probe_dir = work / "probes"
        subprocess.run([str(args.fixture.resolve()), "tilelink_uh_probe", str(probe_dir),
                        "--param", f"config={work / 'input.yaml'}", "--param", "observe=true",
                        "--param", f"gate={args.gating}"], check=True)
        ports = json.loads((probe_dir / "ports.json").read_text())[0]["ports"]
        for port in ports:
            if port["endpoint"] == "probe":
                mapping["ports"].append(dict(port, direction="input" if port["input"] else "output"))
        fixture_rtl = (probe_dir / "network.v").read_text() + (probe_dir / "cells.v").read_text()
    body = args.bench.read_text()
    if args.extra:
        body = body.replace('    $display("UH_PASS', args.extra.read_text() + '\n    $display("UH_PASS')
    (work / "tb.sv").write_text(bench(mapping, body, cfg, args.windows, args.miss_last, args.bus_model))
    checker = Path(__file__).parent / "sv/nlv_tl_checker.sv"
    rtl = ((fixture_rtl if fixture_rtl is not None else sim_engine.generated_rtl(generated / "nocloom_uh_ingress.v"))
           + checker.read_text() + args.axi_check.read_text())
    (work / "network.v").write_text(rtl)
    compile_argv = None
    if sim_engine.selection(args.sim)=="verilator":
        compile_argv = sim_engine.verilator_command("network.v")
        compile_argv[compile_argv.index("--build-jobs")+1] = "6"
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                compile_argv=compile_argv, compile_timeout=180,
                                run_timeout=120, success=("UH_PASS",))
    result = sim.run(["+UH_BACK_TO_BACK"] if args.back_to_back else [])
    print(json.dumps(dict(maximum=maximum, source_width=args.source_width, slots=args.slots,
                          target_width=args.target_width, cdc=args.cdc,
                          rtl_sha256=hashlib.sha256(rtl.encode()).hexdigest())))
    print(result.stdout)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--extra", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--bus-model", type=Path)
    parser.add_argument("--gating", choices=("independent", "combined"))
    parser.add_argument("--axi-check", type=Path,
                        default=Path(__file__).parent / "sv/nlv_tl_uh_axi_check.sv")
    parser.add_argument("--maximum", type=int, default=1024)
    parser.add_argument("--source-width", type=int, default=4)
    parser.add_argument("--sink-width", type=int, default=0)
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--target-protocol", choices=("axi4", "tl_ul", "apb4", "ahb_lite", "wishbone"), default="axi4")
    parser.add_argument("--target-width", type=int, default=32)
    parser.add_argument("--cdc", action="store_true")
    parser.add_argument("--windows", type=int, choices=(0,4), default=0)
    parser.add_argument("--miss-last", action="store_true")
    parser.add_argument("--all-maxima", action="store_true")
    parser.add_argument("--oversize", action="store_true")
    parser.add_argument("--back-to-back", action="store_true")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    for maximum in ([1<<n for n in range(2,13)] if args.all_maxima else [args.maximum]):
        result = run(args, maximum)
        if result:
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
