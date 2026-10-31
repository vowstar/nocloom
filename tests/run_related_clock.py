"""Transaction, stall and common-reset checks for related-clock adapters."""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

import related_reset_bench
from sim_engine import Simulation

PASS = "RELATED_CLOCK_PASS"


def bench(meta):
    edge_aligned = meta["kind"] == "edge_aligned"
    depth = meta["depth"]
    fault = meta["fault"]
    return f"""`timescale 1ns/1ps
module tb;
  localparam integer DEPTH={depth};
  reg clk=0, reset_n=0, valid=0, out_ready=0, slow_enable=0;
  always #5 clk=~clk;
  integer ratio_a=2, ratio_b=3, count_a=0, count_b=0;
  reg enable_a=0, enable_b=0, clk_a=0, clk_b=0;
  always #(5*ratio_a) clk_a=~clk_a;
  always #(5*ratio_b) clk_b=~clk_b;
  always @(posedge clk or negedge reset_n) begin
    if (!reset_n) begin count_a<=0; count_b<=0; enable_a<=1; enable_b<=1; end
    else begin
      enable_a <= count_a==0; enable_b <= count_b==0;
      count_a <= count_a==0 ? ratio_a-1 : count_a-1;
      count_b <= count_b==0 ? ratio_b-1 : count_b-1;
    end
  end
  reg [31:0] in_f0=0; reg [2:0] in_f1=0; reg [1:0] in_f2=0;
  reg [3:0] in_f3=0; reg in_f4=0,in_f5=0; reg [2:0] in_f6=0;
  wire ready,out_valid;
  wire [31:0] out_f0; wire [2:0] out_f1; wire [1:0] out_f2;
  wire [3:0] out_f3; wire out_f4,out_f5; wire [2:0] out_f6;
  {meta['module']} dut(
    .nl_clk_0(clk_a),.nl_clk_1(clk_b),.nl_reset_n_0(reset_n),.nl_reset_n_1(reset_n),
    {'.nl_slow_enable(slow_enable),' if edge_aligned else ''}
    .nl_stream_valid(valid),.nl_stream_ready(ready),
    .nl_stream_out_ready(out_ready),.nl_stream_out_valid(out_valid),
    .nl_stream_in_f0(in_f0),.nl_stream_in_f1(in_f1),.nl_stream_in_f2(in_f2),
    .nl_stream_in_f3(in_f3),.nl_stream_in_f4(in_f4),.nl_stream_in_f5(in_f5),
    .nl_stream_in_f6(in_f6),.nl_stream_out_f0(out_f0),.nl_stream_out_f1(out_f1),
    .nl_stream_out_f2(out_f2),.nl_stream_out_f3(out_f3),.nl_stream_out_f4(out_f4),
    .nl_stream_out_f5(out_f5),.nl_stream_out_f6(out_f6));
  wire [45:0] payload={{in_f4,in_f5,in_f6,in_f3,in_f2,in_f1,in_f0}};
  wire [45:0] out_payload={{out_f4,out_f5,out_f6,out_f3,out_f2,out_f1,out_f0}};
  reg [45:0] expected[0:4095]; integer q_head=0,q_tail=0,seqno=0,accepted=0,delivered=0,sent=0;
  integer words=48,pvalid=75,pready=65,seed=1,cycles=0,reset_at=110;
  integer stall_seen=0,reset_seen=0,full_early_seen=0,margin_seen=0;
  reg previous_stall=0; reg [45:0] previous_payload=0;
  reg source_advanced=0;
  task fail(input [8*64-1:0] label); begin
    $display("ASSERT_FAIL:%0s q=%0d/%0d t=%0t",label,q_head,q_tail,$time); $finish;
  end endtask
  task launch; begin
    valid<=1; in_f0<=32'h1000+seqno; in_f1<=seqno[2:0]; in_f2<=seqno[1:0];
    in_f3<=seqno[3:0]; in_f4<=seqno%3==0; in_f5<=seqno%3==2;
    in_f6<=seqno[2:0];
    expected[q_tail]={{seqno%3==0,seqno%3==2,seqno[2:0],seqno[3:0],seqno[1:0],seqno[2:0],32'h1000+seqno}};
    seqno=seqno+1;
  end endtask
  always @(posedge clk_a) begin
    if (!reset_n) begin sent=0; source_advanced<=0; end
    else begin
      source_advanced<=valid && ready;
      if (valid && ready) sent=sent+1;
    end
  end
  always @(negedge clk_a) begin
    if (!reset_n) begin valid<=0; accepted=0; q_tail=0; end
    else if (accepted<words && (!valid || source_advanced)) begin
      if (($urandom()%100)<pvalid) begin
        launch; accepted=accepted+1; q_tail=q_tail+1;
      end else valid<=0;
    end else if (valid && source_advanced) valid<=0;
  end
  always @(negedge clk_b) if (!reset_n) out_ready<=0;
    else out_ready<=($urandom()%100)<pready;
  always @(negedge clk_a) if (reset_n && DEPTH>1) begin
    if (sent-q_head>=DEPTH-1 && ready) margin_seen=1;
    if (sent-q_head>=DEPTH-1 && !ready) full_early_seen=1;
    if ("{fault}"=="full_early" && sent-q_head>=DEPTH-1 && ready)
      fail("FULL_EARLY_NOT_DETECTED");
  end
  {'''reg model_done=0;
  always @(posedge clk_b) begin
    if (!reset_n) model_done<=0;
    else model_done<=(model_done || (valid && !model_done && out_ready)) && !slow_enable;
  end
  wire model_ready=out_ready | model_done;
  wire model_valid_out=valid && !model_done;
  wire [45:0] model_payload=payload;
  ''' if edge_aligned and meta['slow_from_sender'] == 1 and meta['slow_pipeline'] == 0 else ''}
  {'''reg model_mask=0;
  always @(posedge clk_a) begin
    if (!reset_n) model_mask<=0;
    else model_mask<=(model_mask || !valid) && !slow_enable;
  end
  wire model_ready=out_ready && !model_mask && slow_enable;
  wire model_valid_out=valid && !model_mask;
  wire [45:0] model_payload=payload;
  ''' if edge_aligned and meta['slow_from_sender'] == 0 and meta['slow_pipeline'] == 0 else ''}
  {'''reg model_done=0,model_spare=0,model_full=0;
  reg [45:0] model_spare_data=0,model_full_data=0;
  wire model_inner=!model_full || out_ready;
  wire model_mid_valid=model_spare | valid;
  wire [45:0] model_mid_data=model_spare ? model_spare_data : payload;
  wire model_adapter_valid=valid;
  always @(posedge clk_b) begin
    if (!reset_n) begin model_spare<=0; model_full<=0; end
    else begin
      if (valid && !model_spare) model_spare_data<=payload;
      model_spare<=model_mid_valid && !model_inner;
      if (model_mid_valid && model_inner) begin model_full<=1; model_full_data<=model_mid_data; end
      else if (model_full && out_ready) model_full<=0;
    end
  end
  wire model_ready=slow_enable && !model_spare;
  wire model_valid_out=model_full;
  wire [45:0] model_payload=model_full_data;
  ''' if edge_aligned and meta['slow_from_sender'] == 0 and meta['slow_pipeline'] == 1 else ''}
  {'''reg model_done=0,model_spare=0,model_full=0;
  reg [45:0] model_spare_data=0,model_full_data=0;
  wire model_inner=!model_full || (out_ready | model_done);
  wire model_mid_valid=model_spare | valid;
  wire [45:0] model_mid_data=model_spare ? model_spare_data : payload;
  wire model_adapter_valid=model_full && !model_done;
  always @(posedge clk_a) begin
    if (!reset_n) begin model_spare<=0; model_full<=0; end
    else begin
      if (valid && !model_spare) model_spare_data<=payload;
      model_spare<=model_mid_valid && !model_inner;
      if (model_mid_valid && model_inner) begin model_full<=1; model_full_data<=model_mid_data; end
      else if (model_full && (out_ready | model_done)) model_full<=0;
    end
  end
  always @(posedge clk_b) begin
    if (!reset_n) model_done<=0;
    else model_done<=(model_done || (model_adapter_valid && out_ready)) && !slow_enable;
  end
  wire model_ready=!model_spare;
  wire model_valid_out=model_adapter_valid;
  wire [45:0] model_payload=model_full_data;
  ''' if edge_aligned and meta['slow_from_sender'] == 1 and meta['slow_pipeline'] == 1 else ''}
  {'''always @(posedge clk) begin
    if (!reset_n) previous_stall=0;
    else begin
      if (out_valid && !out_ready) stall_seen=stall_seen+1;
      if (ready!==model_ready || out_valid!==model_valid_out ||
          (out_valid && out_payload!==model_payload)) begin
        $display("MODEL ready=%b/%b valid=%b/%b payload=%h/%h",model_ready,ready,model_valid_out,out_valid,model_payload,out_payload);
        fail("EDGE_ALIGNED_MODEL");
      end
      if (out_valid && out_ready) delivered=delivered+1;
    end
  end''' if edge_aligned else '''always @(posedge clk_b) begin
    if (!reset_n) begin q_head=0; previous_stall=0; end
    else begin
      if (previous_stall && (!out_valid || out_payload!==previous_payload)) fail("STABLE");
      previous_stall=out_valid&&!out_ready; previous_payload=out_payload;
      if (previous_stall) stall_seen=stall_seen+1;
      if (out_valid && out_ready) begin
        if (q_head>=q_tail) fail("PHANTOM");
        if (out_payload!==expected[q_head]) begin
          $display("EXPECTED %h GOT %h",expected[q_head],out_payload); fail("ORDER_OR_FIELD");
        end
        q_head=q_head+1; delivered=delivered+1;
      end
    end
  end'''}
  always @(negedge clk) begin
    cycles=cycles+1;
    if (cycles==reset_at) begin reset_n<=0; valid<=0; out_ready<=0; reset_seen=reset_seen+1; end
    if (cycles==reset_at+3) reset_n<=1;
    if (cycles==1) reset_n<=0;
    if (cycles==8) reset_n<=1;
    if (cycles>40000) fail("TIMEOUT");
    if (reset_seen && accepted==words && {'' if edge_aligned else 'q_head==q_tail &&'} !valid && cycles>reset_at+30) begin
      if (!stall_seen) fail("NO_BACKPRESSURE_COVERAGE");
      if ("{fault}"=="full_early" && !full_early_seen) fail("FULL_EARLY_NO_COVERAGE");
      if ("{fault}"=="none" && DEPTH>1 && !margin_seen) fail("NO_FULL_MARGIN_COVERAGE");
      $display("FAULT_CAUGHT:FULL_EARLY");
      $display("{PASS} accepted=%0d delivered=%0d stalls=%0d resets=%0d",accepted,delivered,stall_seen,reset_seen);
      $finish;
    end
  end
  initial begin
    if (!$value$plusargs("ratio_a=%d",ratio_a)) ratio_a=2;
    if (!$value$plusargs("ratio_b=%d",ratio_b)) ratio_b=3;
    void'($value$plusargs("pvalid=%d",pvalid)); void'($value$plusargs("pready=%d",pready));
    void'($value$plusargs("seed=%d",seed)); void'($value$plusargs("reset_at=%d",reset_at));
    void'($urandom(seed));
    if ({1 if edge_aligned else 0}) slow_enable=0;
  end
  {'''always @(negedge clk) begin
    if (ratio_a>ratio_b) slow_enable <= ratio_a>1 ? enable_a : 1'b1;
    else if (ratio_b>ratio_a) slow_enable <= ratio_b>1 ? enable_b : 1'b1;
    else slow_enable <= 1'b1;
  end''' if edge_aligned else ''}
endmodule
"""


def run_case(fixture, root, kind, args, ratios, fault="none", extra=None):
    name = f"{kind}_{'_'.join(map(str,args))}_{ratios[0]}to{ratios[1]}_{fault}"
    work = root / name
    work.mkdir(parents=True, exist_ok=True)
    argv = [str(fixture), str(work), kind, *map(str,args), fault]
    generated = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    (work / "generate.log").write_text(generated.stdout + generated.stderr)
    if generated.returncode:
        raise AssertionError(f"fixture failed: {argv}: {generated.stderr}")
    meta = json.loads((work / "bindings.json").read_text())
    (work / "network.v").write_text((work / "generated.v").read_text())
    (work / "tb.sv").write_text(bench(meta))
    options = [f"+ratio_a={ratios[0]}", f"+ratio_b={ratios[1]}", "+seed=17",
               "+pvalid=85", "+pready=55", "+reset_at=137"]
    options += extra or []
    result = Simulation(work, "network.v", compile_timeout=300, run_timeout=120,
                        success=[PASS]).run(options)
    log = (work / "simulation.log").read_text(errors="replace")
    if fault == "none":
        passed = result.returncode == 0 and PASS in log and "ASSERT_FAIL" not in log
    elif fault == "full_early":
        passed = "FAULT_CAUGHT:FULL_EARLY" in log and "FULL_EARLY_NOT_DETECTED" not in log
    else:
        passed = "ASSERT_FAIL" in log or result.returncode != 0
    row = dict(name=name, expected="PASS" if fault == "none" else "CAUGHT", passed=passed,
               exit=result.returncode, log=str(work / "simulation.log"))
    print(("PASS" if passed else "FAIL"), name, flush=True)
    if not passed:
        print(log[-5000:], flush=True)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--nocloom", required=True, type=Path)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    rows = []
    for depth, fast, ratios in [(2,0,(2,3)),(2,0,(3,2)),(3,0,(2,3)),(4,0,(3,2)),
                                (2,1,(2,3)),(2,1,(3,2))]:
        rows.append(run_case(args.fixture,args.work,"sync",[depth,fast],ratios))
    rows.append(run_case(args.fixture,args.work,"sync",[3,0],(2,3),"full_early"))
    for slow,pipe,ratios in [(1,1,(2,1)),(1,0,(2,1)),(0,1,(1,2)),(0,0,(1,2)),
                             (1,0,(1,1)),(0,0,(1,1))]:
        rows.append(run_case(args.fixture,args.work,"edge_aligned",[slow,pipe],ratios))
    rows.append(run_case(args.fixture,args.work,"edge_aligned",[1,0],(2,1),"missing_admission_open"))
    rows.append(run_case(args.fixture,args.work,"edge_aligned",[0,0],(1,2),"missing_admission_open"))
    rows += related_reset_bench.case_rows(args.nocloom, args.work)
    (args.work / "results.json").write_text(json.dumps(rows,indent=2)+"\n")
    if not all(row["passed"] for row in rows):
        raise SystemExit(1)
    print(f"RELATED_CLOCK_SUITE_PASS {len(rows)}/{len(rows)}")


if __name__ == "__main__":
    main()
