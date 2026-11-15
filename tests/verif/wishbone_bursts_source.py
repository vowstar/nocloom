"""Wishbone source sequences held within one cycle, with error recovery."""

import argparse
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

HERE = Path(__file__).resolve().parent
BODY = r"""
  always #5 clk_main=~clk_main;
  integer ticks=0, accepted=0, writes=0, reads=0, groups=0, addresses=0;
  integer write_address=0, write_id=0, before_words;
  reg writing=0, error_enable=1;
  reg [31:0] memory[0:1023];
  always @(posedge clk_main) begin
    ticks=ticks+1;
    if(ticks>4000) $fatal(1,"CHECK_FAIL:WB_SOURCE_PROGRESS");
    if(reset_n) begin
      if((s0_ack || s0_err) && (!s0_cyc || !s0_stb)) $fatal(1,"CHECK_FAIL:WB_SOURCE_ORPHAN");
      if(s0_ack && s0_err) $fatal(1,"CHECK_FAIL:WB_SOURCE_TERMINATIONS");
      if(s0_cyc && s0_stb && (s0_ack || s0_err)) accepted=accepted+1;
    end
    if(!reset_n) begin t0_bvalid<=0; t0_rvalid<=0; writing=0; end
    else begin
      if(t0_bvalid && t0_bready) t0_bvalid<=0;
      if(t0_rvalid && t0_rready) t0_rvalid<=0;
      if(t0_awvalid && t0_awready) begin
        if(writing || t0_awlen!=0 || t0_awsize!=2) $fatal(1,"CHECK_FAIL:WB_SOURCE_AW");
        addresses=addresses+1; writing=1; write_address=t0_awaddr; write_id=t0_awid;
      end
      if(t0_wvalid && t0_wready) begin
        if(!writing || !t0_wlast || t0_wstrb!=15) $fatal(1,"CHECK_FAIL:WB_SOURCE_W");
        writing=0; writes=writes+1; memory[write_address/4]=t0_wdata;
        t0_bid<=write_id; t0_bvalid<=1;
        t0_bresp<=(error_enable && write_address==32'h284) ? 2:0;
      end
      if(t0_arvalid && t0_arready) begin
        if(t0_arlen!=0 || t0_arsize!=2) $fatal(1,"CHECK_FAIL:WB_SOURCE_AR");
        reads=reads+1; t0_rid<=t0_arid; t0_rvalid<=1; t0_rlast<=1;
        t0_rresp<=(error_enable && t0_araddr==32'h284) ? 2:0;
        t0_rdata<=memory[t0_araddr/4];
      end
    end
  end
  task word(input integer write_request, address, tag, error_response, input [31:0] value);
    integer waits;
    begin
      @(negedge clk_main);
      s0_cyc=1; s0_stb=1; s0_we=write_request;
      s0_adr=@WORD@ ? address/4:address; s0_cti=tag; s0_bte=0;
      s0_sel=15; s0_dat_w=value; waits=0;
      @(posedge clk_main);
      while(!(s0_ack || s0_err) && waits<256) begin @(posedge clk_main); waits=waits+1; end
      if(waits==256 || s0_err!=error_response || s0_ack==error_response)
        $fatal(1,"CHECK_FAIL:WB_SOURCE_RESPONSE address=%h",address);
      if(!write_request && s0_dat_r!=(error_response ? 32'b0:value))
        $fatal(1,"CHECK_FAIL:WB_SOURCE_READ_DATA address=%h",address);
      @(negedge clk_main); s0_stb=0;
      // Keep CYC high across an STB gap, including after ERR.
      repeat(3) begin
        @(negedge clk_main);
        if(!s0_cyc) $fatal(1,"CHECK_FAIL:WB_SOURCE_CYCLE_GAP");
      end
    end
  endtask
  task end_cycle(input integer count);
    begin
      if(accepted-before_words!=count) $fatal(1,"CHECK_FAIL:WB_SOURCE_CYCLE_WORDS");
      @(negedge clk_main); s0_cyc=0;
      repeat(8) @(negedge clk_main);
      if(accepted-before_words!=count) $fatal(1,"CHECK_FAIL:WB_SOURCE_EXTRA_RESPONSE");
      before_words=accepted; groups=groups+1;
    end
  endtask
  initial begin
    for(integer j=0;j<1024;j=j+1) memory[j]=0;
    t0_awready=1; t0_wready=1; t0_arready=1;
    repeat(8) @(negedge clk_main); reset_n=1;
    repeat(12) @(negedge clk_main); before_words=accepted;
    // A source can end its tagged cycle with a classic transfer.
    word(1,32'h1240,2,0,32'h12345678);
    word(1,32'h1244,0,0,32'h87654321);
    end_cycle(2);
    word(1,32'h1280,2,0,32'h55110000);
    word(1,32'h1284,2,1,32'h55110001);
    word(1,32'h1288,7,0,32'h55110002);
    end_cycle(3);
    word(0,32'h1280,2,0,32'h55110000);
    word(0,32'h1284,2,1,0);
    word(0,32'h1288,7,0,32'h55110002);
    end_cycle(3);
    error_enable=0;
    word(1,32'h1284,0,0,32'hcafed00d);
    word(0,32'h1284,0,0,32'hcafed00d);
    end_cycle(2);
    if(groups!=4 || accepted!=10 || writes!=6 || reads!=4 || addresses!=6 || writing)
      $fatal(1,"CHECK_FAIL:WB_SOURCE_FOOTPRINT");
    $display("WB_SOURCE_PASS groups=%0d responses=%0d writes=%0d reads=%0d",groups,accepted,writes,reads);
    $finish;
  end
"""


def direct(args, root, unit, configuration):
    work = root / unit
    work.mkdir()
    cfg = configuration("wishbone", "axi4", unit)
    cfg["initiators"][0]["burst_tags"] = True
    config = work / "input.yaml"
    config.write_text(json.dumps(cfg))
    out = work / "generated"
    subprocess.run(
        [str(args.cli.resolve()), "generate", str(config), "--out-dir", str(out)],
        check=True,
    )
    mapping = json.loads(next(out.glob("*.map.json")).read_text())
    declarations, connections = [], []
    for port in mapping["ports"]:
        name = port["name"]
        alias = name.replace("nl_i_", "").replace("nl_t_", "")
        variable = port["direction"] == "input"
        declarations.append(
            f"{'reg' if variable else 'wire'} [{port['width'] - 1}:0] {alias}"
            + ("=0;" if variable else ";")
        )
        connections.append(f".{name}({alias})")
    tb = (
        "module tb;\n"
        + "\n".join(declarations)
        + "\nnocloom_wb_classic dut("
        + ",".join(connections)
        + ");\n"
        + BODY.replace("@WORD@", str(int(unit == "word")))
        + "\nendmodule\n"
    )
    (work / "tb.sv").write_text(tb)
    (work / "network.v").write_text(
        sim_engine.generated_rtl(out / "nocloom_wb_classic.v")
    )
    result = sim_engine.Simulation(
        work,
        "network.v",
        engine=args.sim,
        config=args.sim_config,
        success=("WB_SOURCE_PASS",),
    ).run([])
    print(result.stdout, flush=True)
    if result.returncode:
        raise RuntimeError(f"direct source simulation failed in {work}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cli", "case", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    sim_args = []
    if args.sim:
        sim_args += ["--sim", args.sim]
    if args.sim_config:
        sim_args += ["--sim-config", args.sim_config]
    subprocess.run(
        [
            sys.executable,
            str(HERE / "nlverif/run.py"),
            "--case",
            str(args.case),
            "--cli",
            str(args.cli.resolve()),
            "--param",
            "family=source",
            "--work",
            str(args.work / "network"),
            *sim_args,
        ],
        check=True,
    )
    case = runpy.run_path(str(args.case))
    root = Path(tempfile.mkdtemp(prefix="direct-", dir=args.work)).resolve()
    for unit in ("byte", "word"):
        direct(args, root, unit, case["configuration"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
