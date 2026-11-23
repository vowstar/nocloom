"""Check which physical APB words execute and which AXI response they produce."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from sim_engine import Simulation, generated_rtl

BENCH = r"""
`timescale 1ns/1ps
module tb;
reg clk=0, reset_n=0;
always #5 clk=~clk;
reg awvalid=0, wvalid=0;
reg [31:0] awaddr=0, wdata=0;
reg [3:0] wstrb=0;
reg wlast=0;
wire awready, wready, bvalid;
wire [1:0] bresp;
wire psel, penable, pwrite;
wire [31:0] paddr, pwdata;
integer packets=0, accesses=0, seen, word_id;
integer first, missing;
reg [3:0] hole;
reg live=0;

nocloom_apb_write_contract dut(
 .clk_main(clk), .reset_n(reset_n), .test(1'b0),
 .nl_i_s0_awaddr(awaddr), .nl_i_s0_awlen(4'd3), .nl_i_s0_awsize(3'd2),
 .nl_i_s0_awburst(2'd1), .nl_i_s0_awid(4'd0), .nl_i_s0_awlock(1'b0),
 .nl_i_s0_awcache(4'd0), .nl_i_s0_awprot(3'd0),
 .nl_i_s0_awvalid(awvalid), .nl_i_s0_awready(awready),
 .nl_i_s0_wdata(wdata), .nl_i_s0_wstrb(wstrb), .nl_i_s0_wlast(wlast),
 .nl_i_s0_wvalid(wvalid), .nl_i_s0_wready(wready),
 .nl_i_s0_bvalid(bvalid), .nl_i_s0_bresp(bresp), .nl_i_s0_bready(1'b1),
 .nl_i_s0_arvalid(1'b0), .nl_i_s0_rready(1'b1),
 .nl_t_t0_psel(psel), .nl_t_t0_penable(penable), .nl_t_t0_paddr(paddr),
 .nl_t_t0_pwrite(pwrite), .nl_t_t0_pwdata(pwdata), .nl_t_t0_prdata(32'd0)@READY@
);

always @(posedge clk) begin
 if (reset_n && psel && penable) begin
  if (seen==missing) seen=seen+1;
  if (!live || seen>=4 || pwrite!==1'b1 || paddr!==first+4*seen || pwdata!==32'h76543210+seen)
   $fatal(1,"ASSERT_FAIL:APB_WRITE_ACCESS packet=%0d word=%0d address=%h data=%h",
          packets,seen,paddr,pwdata);
  seen=seen+1;
  accesses=accesses+1;
 end
 if (reset_n && bvalid) begin
  if (seen==missing) seen=seen+1;
  if (!live || seen!=4 || bresp!==(hole==0 || missing<0 ? 2'd0 : 2'd2))
   $fatal(1,"ASSERT_FAIL:APB_WRITE_RESULT packet=%0d words=%0d response=%h",packets,seen,bresp);
  live=0;
  packets=packets+1;
 end
end

task write_packet(input integer skip, input [3:0] strobe);
 begin
  @(negedge clk);
  seen=0; missing=skip; hole=strobe; live=1;
  first=16*packets; awaddr=32'h1000+first; awvalid=1;
  @(posedge clk); while (!awready) @(posedge clk);
  @(negedge clk); awvalid=0;
  for (word_id=0; word_id<4; word_id=word_id+1) begin
   wvalid=1; wdata=32'h76543210+word_id;
   wstrb=word_id==missing ? hole : 4'hf; wlast=word_id==3;
   @(posedge clk); while (!wready) @(posedge clk);
   @(negedge clk);
  end
  wvalid=0;
  while (live) @(negedge clk);
 end
endtask

initial begin
 repeat (4) @(negedge clk);
 reset_n=1;
 write_packet(0,4'h3);
 write_packet(1,4'h5);
 write_packet(3,4'h8);
 write_packet(1,4'h0);
 write_packet(-1,4'hf);
 if (packets!=5 || accesses!=16) $fatal(1,"ASSERT_FAIL:APB_WRITE_TOTAL");
 $display("NOCLOOM_PASS apb-write-contract packets=%0d accesses=%0d",packets,accesses);
 $finish;
end
initial begin
 repeat (512) @(negedge clk);
 $fatal(1,"ASSERT_FAIL:APB_WRITE_PROGRESS");
end
endmodule
"""


TOP = "nocloom_apb_write_contract"


def definition(dialect):
    return dict(schema_version=10, name="apb_write_contract", address_width=32,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                                    test_port="test")],
                initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32)],
                targets=[dict(name="t0", clock_domain="main", protocol=dialect, data_width=32,
                              address_width=32, mappings=[dict(base="0x1000", size="0x1000")])],
                connectivity="all", transport=dict(transport_word_bytes=4),
                output=dict(backend="verilog", language="verilog_2001"))


def simulate(work, rtl, bench):
    (work / "network.v").write_text(rtl)
    (work / "tb.sv").write_text(bench)
    return Simulation(work, "network.v").run()


def generate(cli, work, dialect):
    cfg = work / "config.yaml"
    cfg.write_text(json.dumps(definition(dialect)))
    out = work / "generated"
    subprocess.run([cli, "generate", cfg, "--out-dir", out], timeout=20, check=True)
    ready_ports = ", .nl_t_t0_pready(1'b1), .nl_t_t0_pslverr(1'b0)" if dialect == "apb3" else ""
    return out / f"{TOP}.v", BENCH.replace("@READY@", ready_ports)


def scoreboard(work, generated, bench, dialect):
    result = simulate(work, generated_rtl(generated), bench)
    assert result.returncode == 0, result.stdout
    assert "NOCLOOM_PASS apb-write-contract packets=5 accesses=16" in result.stdout, result.stdout

    mapping = json.loads(generated.with_suffix(".map.json").read_text())
    # The scoreboard above checks these four claims against the target
    # accesses and packet responses, including when the first word fails.
    assert mapping["definition"]["contract"]["dedicated_dialects"][dialect]["write"] == (
        "full_word_strobe_written_zero_strobe_skipped_without_error_"
        "partial_word_skipped_and_contributes_SLVERR_later_full_words_execute"
    )


def fault(work, generated, bench):
    # Suppress the first packet's accesses. The target sees no writes
    # following the rejected word, but the outlet still returns its response.
    bad, count = re.subn(r"(assign nl_t_t0_psel = )([^;]+);",
                         r"\1(\2) & (nl_t_t0_paddr[31:4] != 28'd0);", generated_rtl(generated))
    assert count == 1
    result = simulate(work, bad, bench)
    assert result.returncode != 0 and "ASSERT_FAIL:APB_WRITE_RESULT packet=0" in result.stdout, (
        result.stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--dialect", choices=("apb3", "apb2"), required=True)
    # Each check compiles its own simulation, so CTest runs every dialect and
    # check as a separate test.
    parser.add_argument("--check", choices=("scoreboard", "fault"), required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix=f"{args.dialect}-{args.check}-", dir=args.work)).resolve()
    generated, bench = generate(args.cli.resolve(), work, args.dialect)
    if args.check == "scoreboard":
        scoreboard(work, generated, bench, args.dialect)
    else:
        fault(work, generated, bench)
    print(f"{args.dialect} {args.check} {time.monotonic() - began:.3f}s")
