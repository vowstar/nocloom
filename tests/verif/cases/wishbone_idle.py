"""Wishbone source occupancy and gate rest during completion and abort."""
import argparse
import itertools
import json
from pathlib import Path
import subprocess
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import sim_engine
from verif.nlverif.config import ENDPOINT_GATE_SETTINGS

CASE = dict(name="wishbone_idle", contracts=["AXI.ROUTE.LOCAL_OFFSET"])


def configuration(unit, mode, gate):
    source = dict(name="s0", clock_domain="main", protocol="wishbone", data_width=32,
                  address_unit=unit, clock_gating=ENDPOINT_GATE_SETTINGS[gate])
    if mode == "pipelined":
        source.update(cycles="pipelined", max_outstanding=3)
    return dict(schema_version=10, name="wb_idle", address_width=16,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                                    test_port="test")], initiators=[source], connectivity="all",
                targets=[dict(name="t0", clock_domain="main", protocol="wishbone", data_width=32,
                              address_unit=unit, mappings=[dict(base="0x0", size="0x10000")])],
                output=dict(backend="verilog", language="verilog_2001"))


def bench(unit, mode):
    extra = ".nl_i_s0_stall(stall), .probe_count(count)," if mode == "pipelined" else ""
    return r'''
`timescale 1ns/1ps
module tb;
  reg clk_main=0, reset_n=0, test=0;
  always #5 clk_main=~clk_main;
  reg cyc=0, stb=0, we=0, allow=0, fail=0;
  wire ack, err, tc, ts, tw;
  wire [31:0] data, read_data;
  wire [3:0] sel;
  wire [@ADDR@:0] addr;
  wire idle, enable, busy, raw_ack, raw_err, aw, w, ar, abandoned;
  wire [1:0] remaining;
  wire [6:0] count;
  wire stall;
  @UNUSED@
  integer words=0, writes=0, reads=0, responses=0;
  integer request_edges=0, ack_edges=0, err_edges=0, count_zero_response=0;
  reg [31:0] memory=0;
  nocloom_wb_idle dut(
    .clk_main(clk_main), .reset_n(reset_n), .test(test),
    .nl_i_s0_cyc(cyc), .nl_i_s0_stb(stb), .nl_i_s0_we(we),
    .nl_i_s0_adr(@ABITS@'d0), .nl_i_s0_sel(4'hf), .nl_i_s0_dat_w(32'h12345678),
    .nl_i_s0_dat_r(read_data), .nl_i_s0_ack(ack), .nl_i_s0_err(err),
    @EXTRA@
    .nl_t_t0_cyc(tc), .nl_t_t0_stb(ts), .nl_t_t0_we(tw),
    .nl_t_t0_adr(addr), .nl_t_t0_sel(sel), .nl_t_t0_dat_w(data),
    .nl_t_t0_dat_r(memory), .nl_t_t0_ack(tc && ts && allow && !fail),
    .nl_t_t0_err(tc && ts && allow && fail),
    .probe_idle(idle), .probe_enable(enable), .probe_remaining(remaining),
    .probe_busy(busy), .probe_ack(raw_ack), .probe_err(raw_err),
    .probe_aw(aw), .probe_w(w), .probe_ar(ar), .probe_abandoned(abandoned));

  always @(posedge clk_main) if (reset_n) begin
    if (tc && ts && allow) begin
      words=words+1;
      if (addr != 0 || sel != 15) $fatal(1,"TARGET_ADDRESS_SEL");
      if (tw) begin
        if (data != 32'h12345678) $fatal(1,"TARGET_WRITE_DATA");
        writes=writes+1;
        if (!fail) memory=data;
      end else reads=reads+1;
    end
    if (ack || err) responses=responses+1;
  end

  // Observe the new register values on each acceptance and response edge.
  always @(posedge clk_main) begin
    #1;
    if (reset_n) begin
      if ((busy || raw_ack || raw_err || aw || w || ar @COUNT_BUSY@) && idle)
        $fatal(1,"WB_IDLE_OCCUPIED busy=%0d ack=%0d err=%0d aw=%0d w=%0d ar=%0d count=%0d",
               busy,raw_ack,raw_err,aw,w,ar,count);
      if (aw || w || ar) request_edges=request_edges+1;
      if (raw_ack) ack_edges=ack_edges+1;
      if (raw_err) err_edges=err_edges+1;
      if (@PIPE@ && count == 0 && (raw_ack || raw_err))
        count_zero_response=count_zero_response+1;
    end
  end

  task quiet;
    begin
      repeat (16) @(negedge clk_main);
      if (busy || raw_ack || raw_err || aw || w || ar || !idle || enable || remaining != 0)
        $fatal(1,"WB_IDLE_DRAIN busy=%0d idle=%0d enable=%0d abandoned=%0d",
               busy,idle,enable,abandoned);
    end
  endtask

  task transfer(input integer write_request, input integer error_response, input integer abort_mode);
    integer before_words, before_responses, n;
    begin
      before_words=words;
      before_responses=responses;
      @(negedge clk_main);
      cyc=1; stb=1; we=(write_request != 0); allow=0; fail=(error_response != 0);
      @(posedge clk_main); #2;
      if (!busy || (write_request && !(aw && w)) || (!write_request && !ar))
        $fatal(1,"WB_REQUEST_NOT_CAPTURED");
      @(negedge clk_main);
      if (@PIPE@ || abort_mode == 1) stb=0;
      if (abort_mode == 2) begin cyc=0; stb=0; end
      // Delay target completion through the request wait and abort checks.
      repeat (20) begin
        @(negedge clk_main);
        if (ack || err) $fatal(1,"WB_EARLY_RESPONSE");
        if (!busy || idle) $fatal(1,"WB_IDLE_PENDING");
      end
      allow=1;
      n=0;
      if (abort_mode == 0) begin
        while (!(ack || err) && n < 120) begin @(negedge clk_main); n=n+1; end
        if (n == 120) $fatal(1,"WB_RESPONSE_TIMEOUT");
        if (err != error_response || ack == error_response) $fatal(1,"WB_RESPONSE_KIND");
        if (!write_request && read_data != (error_response ? 32'd0 : 32'h12345678))
          $fatal(1,"WB_READ_DATA");
        @(negedge clk_main);
        cyc=0; stb=0;
      end else begin
        while (busy && n < 120) begin
          @(negedge clk_main); n=n+1;
          if (ack || err) $fatal(1,"WB_ABORT_RESPONSE_LEAK");
        end
        if (n == 120) $fatal(1,"WB_ABORT_DRAIN_TIMEOUT");
      end
      quiet();
      if (words != before_words+1) $fatal(1,"WB_TARGET_COMPLETION_COUNT");
      if (responses != before_responses+(abort_mode == 0 ? 1 : 0))
        $fatal(1,"WB_SOURCE_COMPLETION_COUNT");
      if (abort_mode && !@PIPE@ && !abandoned) $fatal(1,"WB_ABORT_NOT_REACHED");
      @(negedge clk_main); cyc=0; stb=0;
    end
  endtask

  initial begin
    repeat (4) @(negedge clk_main);
    reset_n=1;
    repeat (12) @(negedge clk_main);
    if (!idle || enable || remaining != 0) $fatal(1,"WB_GATE_NOT_CLOSED");
    transfer(1,0,0);
    transfer(0,0,0);
    transfer(1,1,0);
    transfer(0,1,0);
    if (!@PIPE@) begin
      transfer(1,0,1);
      transfer(0,0,1);
    end
    transfer(1,0,2);
    transfer(0,0,2);
    transfer(0,0,0);
    if (request_edges == 0 || ack_edges == 0 || err_edges == 0 ||
        (@PIPE@ && count_zero_response == 0)) $fatal(1,"WB_IDLE_COVERAGE");
    $display("CHECK_PASS words=%0d writes=%0d reads=%0d requests=%0d ack=%0d err=%0d tail=%0d",
             words,writes,reads,request_edges,ack_edges,err_edges,count_zero_response);
    $finish;
  end
  initial begin #20000; $fatal(1,"WB_TEST_TIMEOUT"); end
endmodule
'''.replace("@ADDR@", "13" if unit == "word" else "15").replace(
        "@ABITS@", "14" if unit == "word" else "16").replace("@EXTRA@", extra).replace(
        "@PIPE@", "1" if mode == "pipelined" else "0").replace(
        "@COUNT_BUSY@", " || count != 0" if mode == "pipelined" else "").replace(
        "@UNUSED@", "" if mode == "pipelined" else "assign count=0; assign stall=0;")


def run(command, directory, name):
    result = subprocess.run([str(v) for v in command], cwd=directory, capture_output=True,
                            text=True, timeout=90)
    output = result.stdout + result.stderr
    (directory / (name + ".log")).write_text(output)
    if result.returncode or "FATAL" in output or "%Error" in output:
        raise RuntimeError(f"{name} failed in {directory}:\n{output[-4000:]}")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    for unit, mode, gate in itertools.product(("byte", "word"), ("classic", "pipelined"),
                                              ("independent", "combined")):
        directory = work / f"{unit}-{mode}-{gate}"
        directory.mkdir()
        config = directory / "config.yaml"
        config.write_text(json.dumps(configuration(unit, mode, gate)))
        run([args.fixture, "wishbone_idle", directory / "generated", "--param", f"config={config}",
             "--param", f"gate={gate}"], directory, "generate")
        tb = directory / "tb.sv"
        tb.write_text(bench(unit, mode))
        generated = directory / "generated"
        rtl = (generated / "network.v").read_text() + (generated / "cells.v").read_text()
        (directory / "network.v").write_text(rtl)
        run(["iverilog", "-g2001", "-s", "nocloom_wb_idle", "-o", directory / "syntax.vvp",
             directory / "network.v"], directory, "syntax")
        compile_argv = ["verilator", "--binary", "--timing", "--top-module", "tb", "-Wno-fatal",
                        "--build-jobs", "2", "-o", "sim", "tb.sv", "network.v"]
        sim = sim_engine.Simulation(directory, "network.v", engine=args.sim, config=args.sim_config,
                                    compile_argv=compile_argv, success=("CHECK_PASS",))
        result = sim.run([])
        output = result.stdout
        if result.returncode or "CHECK_PASS" not in output or "FATAL" in output:
            raise RuntimeError(f"simulation failed in {directory}:\n{output[-4000:]}")
        print(directory.name + ": " + next(v for v in output.splitlines() if "CHECK_PASS" in v),
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
