"""TL-UL D responses keep their cycle and contents when clock gates sleep."""
import argparse
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import sim_engine
from verif.nlverif.config import ENDPOINT_GATE_SETTINGS

CASE = dict(name="tilelink_gate", contracts=["TL.ACCESS", "AXI.CLOCK.GATE"])


def configuration(capacity, mode):
    return dict(
        schema_version=10, name="tilelink_gate", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                            test_port="test")],
        initiators=[dict(name="s", clock_domain="main", protocol="tl_ul", data_width=32,
                         max_outstanding=capacity, clock_gating=ENDPOINT_GATE_SETTINGS[mode])],
        targets=[dict(name="t", clock_domain="main", protocol="axi4", data_width=32,
                      attributes="amba", id_width=4, len_width=8,
                      mappings=[dict(base="0x1000", size="0x1000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def bench(mapped, capacity, mode):
    def net(value):
        assert value.startswith("value/"), value
        return "dut.nl_internal_v" + value.split("/")[1]

    stores = {s["resource"]: s for s in mapped["network"]["storage"]}
    probes = {}
    for direction in ("read", "write"):
        store = stores[f"source/s/{direction}/return"]
        for event in ("acquire", "release"):
            probes[f"@{direction.upper()}_{event.upper()}@"] = net(store[event])
    gate = next((g for g in mapped["network"]["clock_gates"]
                 if g.get("initiator") == "s" and g["zone"] in ("protocol", "shared")), None)
    probes["@CLOSED@"] = "!" + net(gate["enable"]) if gate else "1'b0"
    probes["@GATED@"] = str(int(ENDPOINT_GATE_SETTINGS[mode]["protocol"]))
    probes["@MULTI@"] = "1" if capacity > 1 else "0"
    text = r'''
`timescale 1ns/1ps
module peer(input clk, reset_n, test, a_valid, d_ready, track,
            input [2:0] a_opcode, input [1:0] a_size,
            input [3:0] a_source, input [31:0] a_address,
            output a_ready, d_valid, output [45:0] d,
            output closed, output reg [7:0] reads=0, writes=0,
            output reg [7:0] r_waiting=0, b_waiting=0);
  wire [2:0] d_opcode;
  wire [1:0] d_param, d_size;
  wire [3:0] d_source;
  wire [31:0] d_data;
  wire d_denied, d_corrupt;
  assign d={1'b0,d_opcode,d_param,d_size,d_source,d_data,d_denied,d_corrupt};
  wire awvalid, wvalid, arvalid, bready, rready, wlast;
  wire [31:0] awaddr, araddr, wdata;
  wire [3:0] awid, arid, wstrb;
  reg bvalid=0, rvalid=0, aw_pending=0, w_pending=0;
  reg [3:0] bid=0, rid=0;
  reg [1:0] bresp=0, rresp=0;
  nocloom_tilelink_gate dut(
    .clk_main(clk), .reset_n(reset_n), .test(test),
    .nl_i_s_a_valid(a_valid), .nl_i_s_a_ready(a_ready),
    .nl_i_s_a_opcode(a_opcode), .nl_i_s_a_param(3'd0), .nl_i_s_a_size(a_size),
    .nl_i_s_a_source(a_source), .nl_i_s_a_address(a_address), .nl_i_s_a_mask(4'hf),
    .nl_i_s_a_data(32'h11223344), .nl_i_s_a_corrupt(1'b0),
    .nl_i_s_d_ready(d_ready), .nl_i_s_d_valid(d_valid), .nl_i_s_d_opcode(d_opcode),
    .nl_i_s_d_param(d_param), .nl_i_s_d_size(d_size), .nl_i_s_d_source(d_source),
    .nl_i_s_d_data(d_data), .nl_i_s_d_denied(d_denied), .nl_i_s_d_corrupt(d_corrupt),
    .nl_t_t_awready(1'b1), .nl_t_t_wready(1'b1), .nl_t_t_arready(1'b1),
    .nl_t_t_awvalid(awvalid), .nl_t_t_awaddr(awaddr), .nl_t_t_awid(awid),
    .nl_t_t_wvalid(wvalid), .nl_t_t_wdata(wdata), .nl_t_t_wstrb(wstrb),
    .nl_t_t_wlast(wlast), .nl_t_t_bvalid(bvalid), .nl_t_t_bid(bid),
    .nl_t_t_bresp(bresp), .nl_t_t_bready(bready),
    .nl_t_t_arvalid(arvalid), .nl_t_t_araddr(araddr), .nl_t_t_arid(arid),
    .nl_t_t_rvalid(rvalid), .nl_t_t_rid(rid), .nl_t_t_rdata(32'h5a5a0000),
    .nl_t_t_rresp(rresp), .nl_t_t_rlast(1'b1), .nl_t_t_rready(rready));
  assign closed=@CLOSED@;
  // Independent AXI peer. Each response is registered after its request.
  always @(posedge clk) begin
    if (!reset_n) begin
      aw_pending<=0; w_pending<=0; bvalid<=0; rvalid<=0;
      reads<=0; writes<=0; r_waiting<=0; b_waiting<=0;
    end else begin
      if (awvalid) begin aw_pending<=1; bid<=awid; bresp<=awaddr[2] ? 2 : 0; end
      if (wvalid) begin
        w_pending<=1;
        if (wdata !== 32'h11223344 || wstrb !== 15 || !wlast)
          $fatal(1,"TL_TARGET_WRITE");
      end
      if (aw_pending && w_pending && !bvalid) begin
        bvalid<=1; aw_pending<=0; w_pending<=0; writes<=writes+1;
      end else if (bvalid && bready) bvalid<=0;
      if (arvalid) begin
        if (rvalid) $fatal(1,"TL_TARGET_READ_OVERLAP");
        rvalid<=1; rid<=arid; rresp<=araddr[2] ? 2 : 0; reads<=reads+1;
      end else if (rvalid && rready) rvalid<=0;
      // Count network responses only during the fairness window, which has no local errors.
      if (!track) begin r_waiting<=0; b_waiting<=0; end
      else begin
        case ({@READ_ACQUIRE@,@READ_RELEASE@})
          2'b10: r_waiting<=r_waiting+1;
          2'b01: begin
            if (r_waiting==0) $fatal(1,"TL_READ_WAIT_UNDERFLOW");
            r_waiting<=r_waiting-1;
          end
        endcase
        case ({@WRITE_ACQUIRE@,@WRITE_RELEASE@})
          2'b10: b_waiting<=b_waiting+1;
          2'b01: begin
            if (b_waiting==0) $fatal(1,"TL_WRITE_WAIT_UNDERFLOW");
            b_waiting<=b_waiting-1;
          end
        endcase
      end
    end
  end
endmodule

module tb;
  reg clk=0, reset_n=0, a_valid=0, d_ready=1, track=0;
  always #5 clk=~clk;
  reg [2:0] a_opcode=0;
  reg [1:0] a_size=2;
  reg [3:0] a_source=0;
  reg [31:0] a_address=0;
  wire [1:0] a_ready, d_valid, closed;
  wire [45:0] d[0:1];
  wire [7:0] reads[0:1], writes[0:1], r_waiting[0:1], b_waiting[0:1];
  genvar k;
  generate for (k=0;k<2;k=k+1) begin: run
    peer p(clk,reset_n,k!=0,a_valid,d_ready,track,a_opcode,a_size,a_source,a_address,
           a_ready[k],d_valid[k],d[k],closed[k],reads[k],writes[k],r_waiting[k],b_waiting[k]);
  end endgenerate
  integer cycle=0, received[0:15], j, gap, n, slept=0, fairness=0;
  reg stalled=0;
  reg [45:0] held;
  reg [45:0] expected;
  always @(posedge clk) begin
    cycle=cycle+1;
    if (!reset_n) stalled=0;
    else begin
      if (a_valid && a_ready[0] !== a_ready[1]) $fatal(1,"TL_GATE_A_CYCLE");
      if (d_valid[0] !== d_valid[1])
        $fatal(1,"TL_GATE_D_CYCLE cycle=%0d gated=%b open=%b",cycle,d_valid[0],d_valid[1]);
      if (d_valid[0] && d[0] !== d[1]) $fatal(1,"TL_GATE_D_CONTENTS");
      if (stalled && (!d_valid[0] || d[0] !== held)) $fatal(1,"TL_D_STABLE");
      stalled=d_valid[0] && !d_ready;
      held=d[0];
      if (d_valid[0]) begin
        // Literal protocol results, independent of arbitration and gate logic.
        case (d[0][37:34])
          0,8,9: expected={1'b0,3'd1,2'd0,2'd2,d[0][37:34],32'h5a5a0000,2'b00};
          1,10: expected={1'b0,3'd0,2'd0,2'd2,d[0][37:34],32'd0,2'b00};
          2,4: expected={1'b0,3'd1,2'd0,2'd2,d[0][37:34],32'd0,2'b01};
          3: expected={1'b0,3'd0,2'd0,2'd2,4'd3,32'd0,2'b10};
          5: expected={1'b0,3'd1,2'd0,2'd2,4'd5,32'd0,2'b11};
          6: expected={1'b0,3'd1,2'd0,2'd3,4'd6,32'd0,2'b11};
          default: $fatal(1,"TL_D_UNEXPECTED_SOURCE");
        endcase
        if (d[0] !== expected) $fatal(1,"TL_D_RESULT source=%0d",d[0][37:34]);
        if (d_ready) begin
          j=d[0][37:34]; received[j]=received[j]+1;
          if (received[j] > (j==6 ? 2 : 1)) $fatal(1,"TL_D_DUPLICATE");
          $display("D cycle=%0d source=%0d value=%h",cycle,j,d[0]);
        end
      end
    end
  end
  task send(input [2:0] opcode, input [3:0] source,
            input [31:0] address, input [1:0] size);
    integer limit;
    begin
      @(negedge clk); a_opcode=opcode; a_source=source; a_address=address; a_size=size; a_valid=1;
      limit=0;
      @(posedge clk);
      while (!a_ready[0] && limit<80) begin @(posedge clk); limit=limit+1; end
      if (!a_ready[0]) $fatal(1,"TL_A_TIMEOUT");
      @(negedge clk); a_valid=0;
    end
  endtask
  task complete(input integer source, input integer beats);
    integer limit;
    begin
      limit=0;
      while (received[source]!=beats && limit<100) begin @(negedge clk); limit=limit+1; end
      if (received[source]!=beats) $fatal(1,"TL_D_TIMEOUT source=%0d",source);
    end
  endtask
  task quiet(input integer count);
    begin
      repeat (count) @(negedge clk);
      if (@GATED@ && !closed[0]) $fatal(1,"TL_GATE_DID_NOT_SLEEP");
      if (@GATED@) slept=slept+1;
    end
  endtask
  initial begin
    for (gap=20;gap<=23;gap=gap+1) begin
      reset_n=0; a_valid=0; d_ready=1; track=0;
      for (j=0;j<16;j=j+1) received[j]=0;
      repeat (100) @(negedge clk);
      reset_n=1;
      quiet(gap);
      send(4,0,32'h1000,2); complete(0,1);
      quiet(gap);
      send(0,1,32'h1000,2); complete(1,1);
      quiet(gap);
      d_ready=0;
      send(4,2,32'h1004,2);
      n=0;
      while (!d_valid[0] && n<100) begin @(negedge clk); n=n+1; end
      if (!d_valid[0]) $fatal(1,"TL_STALL_TIMEOUT");
      repeat (9) @(negedge clk);
      d_ready=1; complete(2,1);
      quiet(gap);
      send(0,3,32'h1004,2); complete(3,1);
      quiet(gap);
      send(4,4,32'h3000,2); complete(4,1);
      quiet(gap);
      send(2,5,32'h1000,2); complete(5,1);
      quiet(gap);
      send(4,6,32'h1000,3); complete(6,2);
      quiet(gap);
      if (@MULTI@) begin
        d_ready=0; track=1;
        send(4,8,32'h1000,2);
        n=0;
        while (!d_valid[0] && n<100) begin @(negedge clk); n=n+1; end
        if (!d_valid[0]) $fatal(1,"TL_FAIR_SEED_TIMEOUT");
        send(4,9,32'h1000,2);
        send(0,10,32'h1000,2);
        n=0;
        while (!(r_waiting[0]>0 && b_waiting[0]>0) && n<100) begin
          @(negedge clk); n=n+1;
        end
        if (!(r_waiting[0]>0 && b_waiting[0]>0)) $fatal(1,"TL_BOTH_WAITING_NOT_REACHED");
        if (r_waiting[0]!=r_waiting[1] || b_waiting[0]!=b_waiting[1])
          $fatal(1,"TL_GATE_WAITING");
        repeat (12) @(negedge clk);
        if (!(r_waiting[0]>0 && b_waiting[0]>0)) $fatal(1,"TL_WAITING_NOT_HELD");
        d_ready=1;
        repeat (12) @(negedge clk);
        if (r_waiting[0]!=0 || b_waiting[0]!=0 || received[9]!=1 || received[10]!=1)
          $fatal(1,"TL_R_B_FAIR_BOUND");
        fairness=fairness+1;
        quiet(gap);
      end
      if (reads[0]!=(@MULTI@ ? 4 : 2) || writes[0]!=(@MULTI@ ? 3 : 2) ||
          reads[0]!=reads[1] || writes[0]!=writes[1]) $fatal(1,"TL_TARGET_COUNTS");
    end
    if ((@GATED@ && slept==0) || (@MULTI@ && fairness!=4)) $fatal(1,"TL_GATE_COVERAGE");
    $display("CHECK_PASS slept=%0d fairness=%0d",slept,fairness);
    $finish;
  end
  initial begin #100000; $fatal(1,"TL_GATE_TIMEOUT"); end
endmodule
'''
    for key, value in probes.items():
        text = text.replace(key, value)
    return text


def run(command, directory, name):
    result = subprocess.run(list(map(str, command)), cwd=directory, capture_output=True,
                            text=True, timeout=90)
    (directory / (name + ".log")).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{name} failed in {directory}:\n{result.stdout}{result.stderr}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    for capacity, mode in itertools.product((1, 4), ("independent", "combined", "transport")):
        directory = work / f"{capacity}-{mode}"
        directory.mkdir()
        config = directory / "config.yaml"
        config.write_text(json.dumps(configuration(capacity, mode)))
        generated = directory / "generated"
        run([args.cli, "generate", config, "--out-dir", generated], directory, "generate")
        mapped = json.loads((generated / "nocloom_tilelink_gate.map.json").read_text())
        (directory / "network.v").write_text(
            sim_engine.generated_rtl(generated / "nocloom_tilelink_gate.v"))
        (directory / "tb.sv").write_text(bench(mapped, capacity, mode))
        run(["iverilog", "-g2001", "-s", "nocloom_tilelink_gate", "-o", "syntax.vvp",
             "network.v"], directory, "syntax")
        sim = sim_engine.Simulation(
            directory, "network.v", engine=args.sim, config=args.sim_config,
            compile_argv=["verilator", "--binary", "--timing", "--top-module", "tb",
                          "-Wno-fatal", "--build-jobs", "2", "-o", "sim", "tb.sv", "network.v"],
            success=("CHECK_PASS",))
        result = sim.run([])
        if result.returncode or "CHECK_PASS" not in result.stdout:
            raise RuntimeError(f"simulation failed in {directory}:\n{result.stdout[-4000:]}")
        print(directory.name + ": " + next(line for line in result.stdout.splitlines()
                                          if "CHECK_PASS" in line), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
