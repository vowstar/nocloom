"""Check internal packet counts, payloads and boundaries under backpressure."""
import argparse
from pathlib import Path
import re
import subprocess

from sim_engine import PASS_MARKERS, Simulation


BENCH = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@MODULE@ dut(@CONN@);
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
localparam UP=@UP@, DOWN=@DOWN@, WDOWN=@WDOWN@, US=@US@, DS=@DS@, WS=@WS@;
localparam PIECE=@PIECE@, WPIECE=@WPIECE@, PIECES=DOWN/PIECE;
integer cycle=0, wi=0, wo=0, ro=0, ri=0, heads=0, bs=0, bi=0;
reg responding=0, write_taken=0, response_taken=0;
function [127:0] payload;
  input integer index, size;
  integer j;
  begin
    payload=0;
    if(size==4) begin
      for(j=0;j<4;j=j+1) payload[32*j+:32]=32'h5a000000+4*index+j;
    end else payload= (128'h5a000000+index) << (32*(index % @LANES@));
  end
endfunction
task check;
  input ok;
  input [255:0] label;
  begin
    if(!ok) begin
      $display("ASSERT_FAIL:PACKET_LENGTHS.%0s cycle=%0d wi=%0d wo=%0d ri=%0d ro=%0d",
               label,cycle,wi,wo,ri,ro);
      $fatal(1);
    end
  end
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  write_taken=nl_up_wreq_valid && nl_up_wreq_ready;
  response_taken=nl_down_rrsp_valid && nl_down_rrsp_ready;
  cycle=cycle+1;
  check(cycle<30000,"PROGRESS");
  if(nl_up_wreq_valid && nl_up_wreq_ready) wi=wi+1;
  if(nl_down_wreq_valid && nl_down_wreq_ready) begin
    check(wo<WDOWN,"WRITE_COUNT");
    check(nl_down_wreq_length==PIECE-1,"WRITE_LENGTH");
    check(nl_down_wreq_first==(wo%WPIECE==0),"WRITE_FIRST");
    check(nl_down_wreq_last==(wo%WPIECE==WPIECE-1),"WRITE_LAST");
    check(nl_down_wreq_address==wo*(1<<WS),"WRITE_ADDRESS");
    check(nl_down_wreq_count==(1<<WS),"WRITE_OCCUPANCY");
    check(@WDATA@,"WRITE_DATA");
    check(@WBE@,"WRITE_STROBES");
    wo=wo+1;
  end
  if(nl_up_wrsp_valid && nl_up_wrsp_ready) begin
    check(wo==WDOWN && nl_up_wrsp_result==0,"WRITE_RESPONSE");
    bs=bs+1;
  end
  if(nl_down_wrsp_valid && nl_down_wrsp_ready) bi=bi+1;
  if(nl_down_rreq_valid && nl_down_rreq_ready) begin
    check(nl_down_rreq_length==PIECE-1,"READ_LENGTH");
    check(nl_down_rreq_size==DS,"READ_SIZE");
    heads=heads+1;
  end
  if(nl_down_rrsp_valid && nl_down_rrsp_ready) ri=ri+1;
  if(nl_up_rrsp_valid && nl_up_rrsp_ready) begin
    check(ro<UP,"READ_COUNT");
    check(nl_up_rrsp_first==(ro==0),"READ_FIRST");
    check(nl_up_rrsp_last==(ro==UP-1),"READ_LAST");
    check(nl_up_rrsp_result==0,"READ_RESULT");
    check(@RDATA@,"READ_DATA");
    ro=ro+1;
  end
end
always @(negedge nl_clk) if(nl_reset_n) begin
  nl_down_wreq_ready=(cycle%7!=3);
  nl_up_wrsp_ready=(cycle%5!=2);
  nl_down_rreq_ready=(cycle%7!=4);
  nl_up_rrsp_ready=(cycle%11!=5 && cycle%11!=6);
  if(write_taken || !nl_up_wreq_valid)
    nl_up_wreq_valid=(wi<UP && cycle%5!=1);
  nl_up_wreq_first=(wi==0);
  nl_up_wreq_last=(wi==UP-1);
  nl_up_wreq_address=wi*(1<<US);
  nl_up_wreq_data=payload(wi,US);
  nl_up_wreq_ends=@ENDS@;
  @BE@
  nl_down_wrsp_valid=(wo==WDOWN && bi<PIECES);
  if(response_taken || !nl_down_rrsp_valid)
    nl_down_rrsp_valid=(responding && ri<DOWN && cycle%7!=2);
  nl_down_rrsp_first=(ri%PIECE==0);
  nl_down_rrsp_last=(ri%PIECE==PIECE-1);
  nl_down_rrsp_data=payload(ri,DS);
  nl_down_rrsp_address=ri*(1<<DS);
end
initial begin
@INIT@
  nl_reset_n=0;
  repeat(4) @(negedge nl_clk);
  nl_up_wreq_write=1; nl_up_wreq_length=UP-1; nl_up_wreq_size=US;
  nl_up_wreq_burst=1; nl_up_wreq_cache=3; nl_up_wreq_count=1<<US;
  nl_down_wrsp_first=1; nl_down_wrsp_last=1;
  nl_up_rreq_length=UP-1; nl_up_rreq_size=US; nl_up_rreq_burst=1;
  nl_up_rreq_cache=3; nl_up_rreq_count=1<<US;
  nl_up_rreq_first=1; nl_up_rreq_last=1;
  nl_down_rrsp_count=1<<DS;
  nl_reset_n=1;
  wait(bs==1);
  @(negedge nl_clk); nl_up_rreq_valid=1;
  @(posedge nl_clk);
  while(!nl_up_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_up_rreq_valid=0;
  wait(heads==PIECES);
  @(negedge nl_clk); responding=1;
  wait(ro==UP);
  repeat(10) @(negedge nl_clk);
  check(wi==UP && wo==WDOWN && ri==DOWN && heads==PIECES && bi==PIECES && bs==1,"TOTALS");
  $display("PACKET_LENGTHS_PASS up=%0d down=%0d",UP,DOWN);
  $finish;
end
endmodule
'''


def bench(work, kind, words):
    ports = [(n, int(w), int(i)) for n, w, i in
             (line.split() for line in (work / "ports.tsv").read_text().splitlines())]
    up_size = 4 if kind in ("cut", "cut_axi", "clamp") else 2
    down_size = 4 if kind == "upsize" else 2
    up = words // 4 if up_size == 4 else words
    down = words // 4 if down_size == 4 else words
    module = re.search(r"module (\w+)", (work / "network.v").read_text())[1]
    lanes = 4 if kind in ("clamp", "upsize") else 1
    be = ("nl_up_wreq_be=16'hf << (4*(wi%4));" if kind == "upsize" else
          "nl_up_wreq_be='1;")
    write_size = 4 if kind == "clamp" else down_size
    wdata = "nl_down_wreq_data==payload(wo,WS)"
    wbe = "nl_down_wreq_be==" + ("16'hffff" if write_size == 4 else "4'hf")
    rdata = ("((nl_up_rrsp_data >> (32*(ro%4))) & 32'hffffffff)==32'h5a000000+ro"
             if kind == "upsize" else "nl_up_rrsp_data==payload(ro,US)")
    write_words = words // 4 if write_size == 4 else words
    values = dict(
        DECL="\n".join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};"
                       for n, w, i in ports),
        CONN=", ".join(f".{n}({n})" for n, _, _ in ports),
        INIT="\n".join(f"  {n}=0;" for n, _, i in ports if i and n != "nl_clk"),
        MODULE=module, UP=up, DOWN=down, US=up_size, DS=down_size, LANES=lanes, BE=be,
        WDATA=wdata, WBE=wbe, RDATA=rdata, WS=write_size,
        WDOWN=write_words, PIECE=256 if kind == "cut_axi" else down,
        WPIECE=256 if kind == "cut_axi" else write_words,
        ENDS="(wi%4==3)" if kind == "upsize" else "1")
    text = BENCH
    for key, value in values.items():
        text = text.replace(f"@{key}@", str(value))
    (work / "tb.sv").write_text(text)


def simulate(work, engine):
    command = None
    if engine == "verilator":
        command = ["verilator", "--binary", "--timing", "-Wno-fatal",
                   "--top-module", "tb", "--build-jobs", "2", "-o", "sim",
                   "tb.sv", "network.v"]
    sim = Simulation(work, "network.v", engine=engine, compile_argv=command,
                     compile_timeout=240, run_timeout=60, success=PASS_MARKERS)
    result = sim.run(())
    if result.returncode:
        raise RuntimeError(f"{work.name} failed:\n{result.stdout}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--engine", default="iverilog")
    args = parser.parse_args()
    for bits in (9, 10):
        work = args.work.resolve() / f"markers-{bits}"
        subprocess.run([str(args.fixture.resolve()), str(work), "markers", str(bits)], check=True)
        (work / "tb.sv").write_text(f'''
module tb;
reg [{bits-1}:0] length;
reg [127:0] data;
reg [31:0] address;
wire [127:0] encoded;
wire [{bits-1}:0] decoded;
wire wrap_end;
integer n, g, k;
nocloom_packet_length_markers dut(.nl_test_length(length), .nl_test_data(data),
  .nl_test_address(address), .nl_test_encoded(encoded), .nl_test_decoded(decoded),
  .nl_test_wrap_end(wrap_end));
initial begin
  for(n=0;n<(1<<{bits});n=n+1) begin
    for(g=0;g<4;g=g+1) begin
      length=n; data=0; data[32*g+:32]=n; address=4*n;
      #1;
      if(decoded!=n || !wrap_end) $fatal(1,"ASSERT_FAIL:MARKERS.DECODE");
      for(k=0;k<4;k=k+1)
        if(encoded[32*k+:32]!=n) $fatal(1,"ASSERT_FAIL:MARKERS.ENCODE");
      address=0;
      #1;
      if(wrap_end!=(n==0)) $fatal(1,"ASSERT_FAIL:MARKERS.WRAP_END");
    end
  end
  $display("PACKET_LENGTHS_PASS markers");
  $finish;
end
endmodule
''')
        simulate(work, args.engine)
        print(f"PASS markers-{bits}", flush=True)
        for kind in ("packetizer", "cut", "cut_axi", "clamp", "upsize"):
            sizes = ([n for n in (255, 256, 257, 511, 512, 513, 1023, 1024)
                      if n <= 1 << bits] if kind == "packetizer" else [1 << bits])
            for words in sizes:
                work = args.work.resolve() / f"{kind}-{bits}-{words}"
                subprocess.run([str(args.fixture.resolve()), str(work), kind, str(bits)], check=True)
                bench(work, kind, words)
                simulate(work, args.engine)
                print(f"PASS {kind}-{bits}-{words}", flush=True)


if __name__ == "__main__":
    main()
