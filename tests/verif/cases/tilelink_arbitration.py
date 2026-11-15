"""TL-UL response grants have bounded fairness and serve a lone waiter immediately."""
import argparse
import itertools
import json
from pathlib import Path
import sys
import tempfile

from tilelink_gate import configuration, run

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import sim_engine

CASE = dict(name="tilelink_arbitration", contracts=["TL.ACCESS"])


def bench(mapped):
    stores = {s["resource"]: s for s in mapped["network"]["storage"]}
    probes = {}
    for direction in ("read", "write"):
        for event in ("acquire", "release"):
            value = stores[f"source/s/{direction}/return"][event]
            assert value.startswith("value/"), value
            probes[f"@{direction.upper()}_{event.upper()}@"] = (
                "dut.nl_internal_v" + value.split("/")[1])
    text = r'''
`timescale 1ns/1ps
module tb;
  reg clk=0, reset_n=0, a_valid=0;
  always #5 clk=~clk;
  reg [2:0] a_opcode=0;
  reg [3:0] a_source=0;
  reg [31:0] a_address=32'h1000;
  wire a_ready, d_valid, d_denied, d_corrupt;
  wire [2:0] d_opcode;
  wire [1:0] d_param, d_size;
  wire [3:0] d_source;
  wire [31:0] d_data;
  wire awvalid, wvalid, arvalid, bready, rready, wlast;
  wire [31:0] awaddr, araddr, wdata;
  wire [3:0] awid, arid, wstrb;
  reg bvalid=0, rvalid=0, aw_pending=0, w_pending=0;
  reg [3:0] bid=0, rid=0;
  reg [31:0] rdata=0;
  nocloom_tilelink_arbitration dut(
    .clk_main(clk), .reset_n(reset_n), .test(1'b0),
    .nl_i_s_a_valid(a_valid), .nl_i_s_a_ready(a_ready),
    .nl_i_s_a_opcode(a_opcode), .nl_i_s_a_param(3'd0), .nl_i_s_a_size(2'd2),
    .nl_i_s_a_source(a_source), .nl_i_s_a_address(a_address), .nl_i_s_a_mask(4'hf),
    .nl_i_s_a_data(32'h11223344), .nl_i_s_a_corrupt(1'b0),
    .nl_i_s_d_ready(1'b1), .nl_i_s_d_valid(d_valid), .nl_i_s_d_opcode(d_opcode),
    .nl_i_s_d_param(d_param), .nl_i_s_d_size(d_size), .nl_i_s_d_source(d_source),
    .nl_i_s_d_data(d_data), .nl_i_s_d_denied(d_denied), .nl_i_s_d_corrupt(d_corrupt),
    .nl_t_t_awready(1'b1), .nl_t_t_wready(1'b1), .nl_t_t_arready(1'b1),
    .nl_t_t_awvalid(awvalid), .nl_t_t_awaddr(awaddr), .nl_t_t_awid(awid),
    .nl_t_t_wvalid(wvalid), .nl_t_t_wdata(wdata), .nl_t_t_wstrb(wstrb),
    .nl_t_t_wlast(wlast), .nl_t_t_bvalid(bvalid), .nl_t_t_bid(bid),
    .nl_t_t_bresp(2'b00), .nl_t_t_bready(bready),
    .nl_t_t_arvalid(arvalid), .nl_t_t_araddr(araddr), .nl_t_t_arid(arid),
    .nl_t_t_rvalid(rvalid), .nl_t_t_rid(rid), .nl_t_t_rdata(rdata),
    .nl_t_t_rresp(2'b00), .nl_t_t_rlast(1'b1), .nl_t_t_rready(rready));
  wire r_acquire=@READ_ACQUIRE@, r_release=@READ_RELEASE@;
  wire b_acquire=@WRITE_ACQUIRE@, b_release=@WRITE_RELEASE@;
  integer cycle=0, read_head=0, read_tail=0, max_queued=0, b_due=0;
  integer r_run=0, max_r_run=0;
  integer r_waiting=0, b_waiting=0, b_age=0, bypassed=0;
  integer contended=0, lone_r=0, lone_b=0, max_b_age=0, b_grants=0;
  integer delay_cycles=0, flood=0, received=0, sent=0, j, source, limit;
  reg busy[0:15];
  reg [2:0] kind[0:15];
  reg [31:0] expected_data[0:15];
  reg [3:0] read_ids[0:1023];
  reg [31:0] read_data[0:1023];
  // AXI peer with an independent queue. Consecutive R handshakes need no bubble.
  always @(posedge clk) begin
    cycle<=cycle+1;
    if (!reset_n) begin
      bvalid<=0; rvalid<=0; aw_pending<=0; w_pending<=0;
      read_head=0; read_tail=0; max_queued=0; b_due=0;
      r_run=0; max_r_run=0;
    end else begin
      if (awvalid) begin
        if (aw_pending) $fatal(1,"TL_AXI_WRITE_OVERLAP");
        aw_pending<=1; bid<=awid; b_due=cycle+delay_cycles;
        if (awaddr!==32'h800) $fatal(1,"TL_AXI_WRITE_ADDRESS");
      end
      if (wvalid) begin
        w_pending<=1;
        if (wdata!==32'h11223344 || wstrb!==15 || !wlast)
          $fatal(1,"TL_AXI_WRITE_DATA");
      end
      if (aw_pending && w_pending && !bvalid && cycle>=b_due) begin
        bvalid<=1; aw_pending<=0; w_pending<=0;
      end else if (bvalid && bready) bvalid<=0;
      if (rvalid && rready) begin
        read_head=read_head+1; r_run=r_run+1;
        if (r_run>max_r_run) max_r_run=r_run;
      end else r_run=0;
      if (arvalid) begin
        if (read_tail>=1024) $fatal(1,"TL_AXI_QUEUE_OVERFLOW");
        read_ids[read_tail]=arid;
        read_data[read_tail]=(araddr+32'h1000) ^ 32'h5a5a0000;
        read_tail=read_tail+1;
      end
      if (read_tail-read_head>max_queued) max_queued=read_tail-read_head;
      if (!rvalid || rready) begin
        // Accumulate several in-flight AXI reads before starting the flood.
        rvalid<=read_head<read_tail && (!flood || cycle>=160);
        if (read_head<read_tail) begin
          rid<=read_ids[read_head]; rdata<=read_data[read_head];
        end
      end
    end
  end
  // Count stored kernel responses from map events, without reading grant policy.
  always @(posedge clk) begin
    if (!reset_n) begin r_waiting<=0; b_waiting<=0; end
    else begin
      r_waiting<=r_waiting+r_acquire-r_release;
      b_waiting<=b_waiting+b_acquire-b_release;
    end
  end
  always @(posedge clk) if (reset_n) begin
    if (r_waiting<0 || b_waiting<0) $fatal(1,"TL_WAIT_UNDERFLOW");
    if (r_release && b_release) $fatal(1,"TL_DOUBLE_GRANT");
    if (b_waiting>0) begin
      if (r_waiting>0) contended=contended+1;
      if (r_release) bypassed=bypassed+1;
      if (bypassed>1) $fatal(1,"TL_B_R_BOUND reads=%0d",bypassed);
      // With D ready and no local rejection, an occupied D register drains in
      // one cycle. One competing read can take the next slot and drain in one
      // more cycle. B must be taken by the fourth observed waiting edge.
      if (!b_release && b_age>=3) $fatal(1,"TL_B_CYCLE_BOUND age=%0d",b_age);
      if (b_age>max_b_age) max_b_age=b_age;
      if (b_release) begin b_age=0; bypassed=0; b_grants=b_grants+1; end
      else b_age=b_age+1;
    end else begin b_age=0; bypassed=0; end
    if (!flood && !d_valid && r_waiting>0 && b_waiting==0) begin
      lone_r=lone_r+1;
      if (!r_release) $fatal(1,"TL_LONE_R_FIRST_EDGE");
    end
    if (!flood && !d_valid && b_waiting>0 && r_waiting==0) begin
      lone_b=lone_b+1;
      if (!b_release) $fatal(1,"TL_LONE_B_FIRST_EDGE");
    end
    if (d_valid) begin
      if (!busy[d_source]) $fatal(1,"TL_D_UNEXPECTED_SOURCE");
      if (d_opcode!==(kind[d_source]==4 ? 3'd1 : 3'd0) ||
          d_data!==expected_data[d_source] || d_size!==2 || d_param!==0 ||
          d_denied!==0 || d_corrupt!==0) $fatal(1,"TL_D_RESULT");
      busy[d_source]=0; received=received+1;
    end
  end
  task send(input [2:0] opcode, input [3:0] src, input [31:0] address);
    begin
      @(negedge clk);
      if (busy[src]) $fatal(1,"TL_SOURCE_REUSE");
      busy[src]=1; kind[src]=opcode;
      expected_data[src]=opcode==4 ? address ^ 32'h5a5a0000 : 0;
      a_valid=1; a_opcode=opcode; a_source=src; a_address=address;
      limit=0;
      @(posedge clk);
      while (!a_ready && limit<100) begin @(posedge clk); limit=limit+1; end
      if (!a_ready) $fatal(1,"TL_A_TIMEOUT");
      sent=sent+1;
      @(negedge clk); a_valid=0;
    end
  endtask
  task complete;
    begin
      limit=0;
      while (received<sent && limit<2000) begin @(negedge clk); limit=limit+1; end
      if (received!=sent) $fatal(1,"TL_D_TIMEOUT");
    end
  endtask
  initial begin
    flood=$test$plusargs("flood");
    if (!$value$plusargs("delay=%d",delay_cycles)) delay_cycles=0;
    for (j=0;j<16;j=j+1) busy[j]=0;
    repeat (100) @(negedge clk);
    reset_n=1;
    if (flood) begin
      send(0,0,32'h1800);
      for (j=0;j<512;j=j+1) begin
        source=1+j%15;
        while (busy[source]) @(negedge clk);
        send(4,source,32'h1000+4*(j%128));
      end
      complete;
      if (contended==0 || max_queued<2 || max_r_run<2 || b_grants!=1)
        $fatal(1,"TL_FLOOD_COVERAGE");
    end else begin
      // Repeat each direction to exercise a lone waiter after either grant.
      send(4,1,32'h1000); complete;
      repeat (20) @(negedge clk);
      send(4,2,32'h1004); complete;
      send(0,3,32'h1800); complete;
      repeat (20) @(negedge clk);
      send(0,4,32'h1800); complete;
      if (lone_r!=2 || lone_b!=2 || b_grants!=2) $fatal(1,"TL_LONE_COVERAGE");
    end
    $display("CHECK_PASS flood=%0d contention=%0d max_b_age=%0d queued=%0d consecutive=%0d lone_r=%0d lone_b=%0d",
             flood,contended,max_b_age,max_queued,max_r_run,lone_r,lone_b);
    $finish;
  end
  initial begin #100000; $fatal(1,"TL_ARBITRATION_TIMEOUT"); end
endmodule
'''
    for key, value in probes.items():
        text = text.replace(key, value)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--scenario", choices=("single", "flood"), required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    for capacity, mode in itertools.product((8, 16), ("independent", "combined")):
        directory = work / f"{capacity}-{mode}"
        directory.mkdir()
        config = configuration(capacity, mode)
        config["name"] = "tilelink_arbitration"
        config["targets"][0]["max_outstanding"] = capacity
        source = directory / "config.yaml"
        source.write_text(json.dumps(config))
        generated = directory / "generated"
        run([args.cli, "generate", source, "--out-dir", generated], directory, "generate")
        mapped = json.loads((generated / "nocloom_tilelink_arbitration.map.json").read_text())
        (directory / "network.v").write_text(
            sim_engine.generated_rtl(generated / "nocloom_tilelink_arbitration.v"))
        (directory / "tb.sv").write_text(bench(mapped))
        run(["iverilog", "-g2001", "-s", "nocloom_tilelink_arbitration", "-o", "syntax.vvp",
             "network.v"], directory, "syntax")
        sim = sim_engine.Simulation(
            directory, "network.v", engine=args.sim, config=args.sim_config,
            compile_argv=["verilator", "--binary", "--timing", "--top-module", "tb",
                          "-Wno-fatal", "--build-jobs", "2", "-o", "sim", "tb.sv", "network.v"],
            success=("CHECK_PASS",))
        options = (["+flood", "+delay=200"], ["+flood", "+delay=201"]) if (
            args.scenario == "flood") else ([],)
        for argv in options:
            result = sim.run(argv)
            (directory / ("result-" + (argv[-1] if argv else "single") + ".log")).write_text(
                result.stdout)
            if result.returncode or "CHECK_PASS" not in result.stdout:
                raise RuntimeError(f"simulation failed in {directory}:\n{result.stdout[-4000:]}")
            print(directory.name + ": " + next(line for line in result.stdout.splitlines()
                                              if "CHECK_PASS" in line), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
