`timescale 1ns/1ps
module nlv_tl_uh_axi_check #(
  parameter NAME="axi", ADDR_W=32, DATA_W=32, ID_W=4, LEN_W=8
)(
  input wire clk, rst_n,
  input wire [ADDR_W-1:0] awaddr, araddr,
  input wire [ID_W-1:0] awid, arid, bid, rid,
  input wire [LEN_W-1:0] awlen, arlen,
  input wire [2:0] awsize, arsize, awprot, arprot,
  input wire [1:0] awburst, arburst, bresp, rresp,
  input wire [3:0] awcache, arcache,
  input wire awlock, arlock,
  input wire awvalid, awready, arvalid, arready,
  input wire [DATA_W-1:0] wdata, rdata,
  input wire [DATA_W/8-1:0] wstrb,
  input wire wlast, wvalid, wready, bvalid, bready, rlast, rvalid, rready
);
  localparam A_BITS=ADDR_W+ID_W+LEN_W+13;
  localparam W_BITS=DATA_W+DATA_W/8+1, B_BITS=ID_W+2, R_BITS=ID_W+DATA_W+3;
  wire [A_BITS-1:0] aw={awaddr,awid,awlen,awsize,awprot,awburst,awcache,awlock};
  wire [A_BITS-1:0] ar={araddr,arid,arlen,arsize,arprot,arburst,arcache,arlock};
  wire [W_BITS-1:0] w={wdata,wstrb,wlast};
  wire [B_BITS-1:0] b={bid,bresp};
  wire [R_BITS-1:0] r={rid,rdata,rresp,rlast};
  reg aw_wait=0, ar_wait=0, w_wait=0, b_wait=0, r_wait=0;
  reg [A_BITS-1:0] held_aw, held_ar;
  reg [W_BITS-1:0] held_w;
  reg [B_BITS-1:0] held_b;
  reg [R_BITS-1:0] held_r;
  always @(posedge clk) begin
    if(!rst_n) begin
      aw_wait=0; ar_wait=0; w_wait=0; b_wait=0; r_wait=0;
    end else begin
      if(aw_wait && (!awvalid || aw!=held_aw)) tb.nlv_fail_report("AXI.AW.HOLD",NAME,0);
      if(ar_wait && (!arvalid || ar!=held_ar)) tb.nlv_fail_report("AXI.AR.HOLD",NAME,0);
      if(w_wait && (!wvalid || w!=held_w)) tb.nlv_fail_report("AXI.W.HOLD",NAME,0);
      if(b_wait && (!bvalid || b!=held_b)) tb.nlv_fail_report("AXI.B.HOLD",NAME,0);
      if(r_wait && (!rvalid || r!=held_r)) tb.nlv_fail_report("AXI.R.HOLD",NAME,0);
      aw_wait=awvalid && !awready; held_aw=aw;
      ar_wait=arvalid && !arready; held_ar=ar;
      w_wait=wvalid && !wready; held_w=w;
      b_wait=bvalid && !bready; held_b=b;
      r_wait=rvalid && !rready; held_r=r;
    end
  end
endmodule
