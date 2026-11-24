"""Bounded checks of the shared request pool behind a TL-UH initiator.

The DUT is the generated network of one TL-UH initiator with four message
slots and one AXI4 target, without clock gates. The fixture exposes the pool
count, a unit entering the pool (admission), a unit leaving it (release, before
the counter stops at zero) and the request packet offered at the network
entry.

Definitions:
  C          internal_transaction_capacity of the initiator, here 4.
  admitted   admissions minus releases since reset, counted by the harness.
  held       AXI bursts the target accepted and has not answered: AR minus
             final R, plus AW minus B.
  in flight  a source whose first A beat was taken and whose final D
             handshake has not happened.
  offered    the request packet at the network entry is valid. The target
             can take its address before the packet leaves the initiator,
             and admission counts the packet when it leaves.

Assertions, each checked by bounded model checking in every cycle up to
PROVE_DEPTH cycles after reset:
  POOL_BOUND        the pool count is at most C.
  POOL_RELEASE      a release happens only while the count is above zero.
  POOL_CONSERVE     the pool count equals admitted.
  POOL_HOLDS        held is at most the pool count plus offered, so no unit
                    leaves before its burst is answered.
  POOL_TARGET       held is at most C.
  POOL_IDLE         with no source in flight, the count is zero.
  SOURCE_HELD       D answers only a source in flight, and only its final D
                    beat ends it.
  TARGET_CAPACITY   the target holds at most 4 reads and 4 writes.

Covers, each reached within COVER_DEPTH cycles:
  full pool, a Get and a Put held at the target together with the pool full,
  D stalled while the pool is full, admission and release in one cycle, and a
  source used again after its final D.

Assumptions: one reset cycle, then no reset. The initiator sends Get,
PutFullData and PutPartialData with param 0, no corrupt, legal masks, aligned
addresses below 64, sizes 0 to 3, a source that is not in flight, and the
beats of a 2-beat Put back to back with the same fields. A waits until taken
with stable fields. D ready is free. The AXI4 target answers reads and writes
in order of acceptance, holds R and B until taken, and answers OKAY or SLVERR.
Ready inputs and data are free.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

CAPACITY = 4
PROVE_DEPTH = 10
COVER_DEPTH = 16


def configuration():
    return dict(
        schema_version=10, name="uh_pool", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")],
        initiators=[dict(name="s", clock_domain="main", protocol="tl_uh", data_width=32,
                         source_width=2, size_width=2, max_transfer_bytes=8,
                         max_outstanding=CAPACITY)],
        targets=[dict(name="t", clock_domain="main", protocol="axi4", data_width=32,
                      id_width=4, len_width=4, max_outstanding=4, attributes="amba",
                      mappings=[dict(base="0x0", size="0x1000")])],
        connectivity="all", clock_gating=False,
        output=dict(backend="verilog", language="verilog_2001"))


HARNESS = r"""
module formal_top(input clk);
reg started=0;
always @(posedge clk) started<=1;
wire reset_n=started;
(* anyseq *) reg [2:0] a_opcode; (* anyseq *) reg [1:0] a_size;
(* anyseq *) reg [31:0] a_address; (* anyseq *) reg [3:0] a_mask;
(* anyseq *) reg [31:0] a_data; (* anyseq *) reg a_valid; (* anyseq *) reg [1:0] a_source;
(* anyseq *) reg d_ready;
(* anyseq *) reg t_wready, t_awready, t_arready, t_bvalid, t_rvalid;
(* anyseq *) reg [1:0] t_bresp, t_rresp; (* anyseq *) reg [3:0] t_bid, t_rid;
(* anyseq *) reg t_rlast; (* anyseq *) reg [31:0] t_rdata;
wire a_ready, d_corrupt, d_denied, d_valid, t_awvalid, t_wvalid, t_wlast, t_bready;
wire t_arvalid, t_rready, t_awlock, t_arlock, unit_free, admit, offer;
wire [31:0] d_data, t_awaddr, t_wdata, t_araddr;
wire [2:0] d_opcode, t_awsize, t_arsize, t_awprot, t_arprot, pool;
wire [1:0] d_size, d_source, d_param, t_awburst, t_arburst;
wire [3:0] t_awlen, t_arlen, t_awcache, t_arcache, t_awid, t_arid, t_wstrb;
MODULE dut(.clk(clk), .reset_n(reset_n), .test(1'b0),
  .nl_i_s_a_opcode(a_opcode), .nl_i_s_a_param(3'd0), .nl_i_s_a_size(a_size),
  .nl_i_s_a_address(a_address), .nl_i_s_a_mask(a_mask), .nl_i_s_a_data(a_data),
  .nl_i_s_a_corrupt(1'b0), .nl_i_s_a_valid(a_valid), .nl_i_s_a_source(a_source),
  .nl_i_s_d_ready(d_ready), .nl_i_s_a_ready(a_ready), .nl_i_s_d_corrupt(d_corrupt),
  .nl_i_s_d_data(d_data), .nl_i_s_d_denied(d_denied), .nl_i_s_d_opcode(d_opcode),
  .nl_i_s_d_size(d_size), .nl_i_s_d_source(d_source), .nl_i_s_d_valid(d_valid),
  .nl_i_s_d_param(d_param),
  .nl_t_t_wready(t_wready), .nl_t_t_awready(t_awready), .nl_t_t_bresp(t_bresp),
  .nl_t_t_bid(t_bid), .nl_t_t_bvalid(t_bvalid), .nl_t_t_arready(t_arready),
  .nl_t_t_rid(t_rid), .nl_t_t_rvalid(t_rvalid), .nl_t_t_rlast(t_rlast),
  .nl_t_t_rresp(t_rresp), .nl_t_t_rdata(t_rdata),
  .nl_t_t_awvalid(t_awvalid), .nl_t_t_awaddr(t_awaddr), .nl_t_t_awlen(t_awlen),
  .nl_t_t_awsize(t_awsize), .nl_t_t_awburst(t_awburst), .nl_t_t_awcache(t_awcache),
  .nl_t_t_awprot(t_awprot), .nl_t_t_awlock(t_awlock), .nl_t_t_awid(t_awid),
  .nl_t_t_wvalid(t_wvalid), .nl_t_t_wdata(t_wdata), .nl_t_t_wstrb(t_wstrb),
  .nl_t_t_wlast(t_wlast), .nl_t_t_bready(t_bready),
  .nl_t_t_arvalid(t_arvalid), .nl_t_t_araddr(t_araddr), .nl_t_t_arlen(t_arlen),
  .nl_t_t_arsize(t_arsize), .nl_t_t_arburst(t_arburst), .nl_t_t_arcache(t_arcache),
  .nl_t_t_arprot(t_arprot), .nl_t_t_arlock(t_arlock), .nl_t_t_arid(t_arid),
  .nl_t_t_rready(t_rready),
  .probe_pool(pool), .probe_admit(admit), .probe_release(unit_free),
  .probe_offer(offer));

// Initiator: legal TL-UH Gets and Puts.
wire a_take=a_valid && a_ready, d_take=d_valid && d_ready;
reg a_more=0; reg [1:0] a_more_source=0; reg [2:0] a_more_opcode=0; reg [31:0] a_more_address=0;
reg [3:0] in_flight=0; reg [1:0] d_left[0:3]; reg [3:0] reused=0, retired=0;
wire [3:0] lanes=a_size>=2 ? 4'hf : a_size==1 ? (a_address[1] ? 4'hc : 4'h3) : 4'h1<<a_address[1:0];
wire a_first=a_take && !a_more;
always @* if(reset_n) begin
  assume(a_opcode==4 || a_opcode==0 || a_opcode==1);
  assume(a_address<64);
  assume(a_size<=1 || a_address[1:0]==0);
  assume(a_size!=1 || !a_address[0]);
  assume(a_size!=3 || !a_address[2]);
  assume(a_opcode==1 ? (a_mask & ~lanes)==0 : a_mask==lanes);
  if(a_more) assume(a_valid && a_source==a_more_source && a_opcode==a_more_opcode &&
                    a_size==3 && a_address==a_more_address);
  else if(a_valid) assume(!in_flight[a_source]);
end else assume(!a_valid);
always @(posedge clk) if(reset_n && $past(reset_n) && $past(a_valid && !a_ready)) begin
  assume(a_valid && a_opcode==$past(a_opcode) && a_size==$past(a_size));
  assume(a_address==$past(a_address) && a_source==$past(a_source));
  assume(a_mask==$past(a_mask) && a_data==$past(a_data));
end
integer k;
always @(posedge clk) if(reset_n) begin
  if(a_take) begin
    a_more<=!a_more && a_opcode!=4 && a_size==3;
    a_more_source<=a_source; a_more_opcode<=a_opcode; a_more_address<=a_address;
  end
  for(k=0;k<4;k=k+1) begin
    if(a_first && a_source==k) begin
      in_flight[k]<=1; d_left[k]<=a_opcode==4 && a_size==3 ? 2 : 1;
      if(retired[k]) reused[k]<=1;
    end
    if(d_take && d_source==k) begin
      d_left[k]<=d_left[k]-1;
      if(d_left[k]==1) begin in_flight[k]<=0; retired[k]<=1; end
    end
  end
end

// Target: in-order AXI4 responses, held until taken.
reg [3:0] r_id[0:7], r_len[0:7], b_id[0:7];
reg [2:0] r_head=0, r_tail=0, b_head=0, b_tail=0;
reg [3:0] r_count=0, aw_count=0, b_count=0, w_lasts=0, r_beat=0;
wire ar_take=t_arvalid && t_arready, r_take=t_rvalid && t_rready;
wire aw_take=t_awvalid && t_awready, w_take=t_wvalid && t_wready, b_take=t_bvalid && t_bready;
wire r_final=r_take && t_rlast;
always @* if(reset_n) begin
  if(t_rvalid) assume(r_count!=0 && t_rid==r_id[r_head] && t_rlast==(r_beat==r_len[r_head]));
  if(t_bvalid) assume(b_count!=0 && w_lasts!=0 && t_bid==b_id[b_head]);
  assume(t_rresp==0 || t_rresp==2);
  assume(t_bresp==0 || t_bresp==2);
end else assume(!t_rvalid && !t_bvalid);
always @(posedge clk) if(reset_n && $past(reset_n)) begin
  if($past(t_rvalid && !t_rready))
    assume(t_rvalid && t_rid==$past(t_rid) && t_rdata==$past(t_rdata) && t_rresp==$past(t_rresp));
  if($past(t_bvalid && !t_bready)) assume(t_bvalid && t_bid==$past(t_bid) && t_bresp==$past(t_bresp));
end
always @(posedge clk) if(reset_n) begin
  if(ar_take) begin r_id[r_tail]<=t_arid; r_len[r_tail]<=t_arlen; r_tail<=r_tail+1; end
  if(r_take) begin
    r_beat<=t_rlast ? 0 : r_beat+1;
    if(t_rlast) r_head<=r_head+1;
  end
  r_count<=r_count+ar_take-r_final;
  if(aw_take) begin b_id[b_tail]<=t_awid; b_tail<=b_tail+1; end
  if(b_take) b_head<=b_head+1;
  aw_count<=aw_count+aw_take-b_take;
  w_lasts<=w_lasts+(w_take && t_wlast)-b_take;
end
wire [3:0] held=r_count+aw_count;

// Pool accounting.
reg [7:0] admitted=0;
always @(posedge clk) if(reset_n) admitted<=admitted+admit-unit_free;
always @(posedge clk) if(reset_n) begin
  POOL_BOUND: assert(pool<=CAPACITY);
  POOL_RELEASE: assert(!unit_free || pool!=0);
  POOL_CONSERVE: assert(admitted==pool);
  POOL_HOLDS: assert(held<=pool+offer);
  POOL_TARGET: assert(held<=CAPACITY);
  POOL_IDLE: assert(in_flight!=0 || pool==0);
  SOURCE_HELD: assert(!d_take || in_flight[d_source]);
  TARGET_CAPACITY: assert(r_count<=4 && aw_count<=4);
`ifdef POOL_COVER
  COVER_FULL: cover(pool==CAPACITY);
  COVER_MIXED: cover(pool==CAPACITY && r_count!=0 && aw_count!=0);
  COVER_D_STALL: cover(pool==CAPACITY && d_valid && !d_ready);
  COVER_ADMIT_RELEASE: cover(admit && unit_free);
  COVER_REUSE: cover(reused!=0);
`endif
end
endmodule
"""


def harness(module):
    return HARNESS.replace("MODULE", module).replace("CAPACITY", str(CAPACITY))


def run(args):
    work = Path(tempfile.mkdtemp(prefix="pool-", dir=args.work)).resolve()
    (work / "input.json").write_text(json.dumps(configuration()))
    subprocess.run([str(args.fixture.resolve()), "tilelink_uh_probe", str(work / "gen"),
                    "--param", f"config={work / 'input.json'}", "--param", "observe=pool"],
                   check=True, timeout=120)
    component = json.loads((work / "gen/ports.json").read_text())[0]
    (work / "network.v").write_text((work / "gen/network.v").read_text()
                                    + (work / "gen/cells.v").read_text())
    (work / "formal_top.sv").write_text(harness(component["module"]))
    tasks = ["prove", "cover"]
    (work / "pool.sby").write_text(f"""[tasks]
prove
cover
[options]
prove: mode bmc
prove: depth {args.prove_depth}
cover: mode cover
cover: depth {args.cover_depth}
timeout {args.timeout}
[engines]
smtbmc z3
[script]
read_verilog -formal network.v
prove: read_verilog -formal -sv formal_top.sv
cover: read_verilog -formal -sv -D POOL_COVER formal_top.sv
prep -top formal_top -flatten
[files]
network.v
formal_top.sv
""")
    with subprocess.Popen(["sby", "-j", "2", "pool.sby"], cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, start_new_session=True) as process:
        try:
            output, _ = process.communicate(timeout=args.timeout + 120)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            output, _ = process.communicate()
            print(output, work, flush=True)
            return 1
    print(output, work, flush=True)
    status = {task: (work / f"pool_{task}/status").read_text().split()[0]
              for task in tasks if (work / f"pool_{task}/status").exists()}
    print(json.dumps(dict(capacity=CAPACITY, prove_depth=args.prove_depth, cover_depth=args.cover_depth,
                          status=status)))
    return 0 if process.returncode == 0 and all(status.get(t) == "PASS" for t in tasks) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--prove-depth", type=int, default=PROVE_DEPTH)
    parser.add_argument("--cover-depth", type=int, default=COVER_DEPTH)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
