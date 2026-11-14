`timescale 1ns/1ps
// TileLink 1.8.1 single-beat peer with independent protocol checks.
module nlv_tl_master #(parameter NAME="tl", ADDR_W=32, DATA_W=32, ID_W=4, SIZE_W=2, SINK_W=1, MEM_BYTES=65536, SALT=0)(
    input wire clk, input wire rst_n,
    output reg [3-1:0] a_opcode,
    output reg [3-1:0] a_param,
    output reg [SIZE_W-1:0] a_size,
    output reg [ID_W-1:0] a_source,
    output reg [ADDR_W-1:0] a_address,
    output reg [4-1:0] a_mask,
    output reg [32-1:0] a_data,
    output reg a_corrupt,
    output reg a_valid,
    input wire a_ready,
    input wire [3-1:0] d_opcode,
    input wire [2-1:0] d_param,
    input wire [SIZE_W-1:0] d_size,
    input wire [ID_W-1:0] d_source,
    input wire [SINK_W-1:0] d_sink,
    input wire [32-1:0] d_data,
    input wire d_corrupt,
    input wire d_denied,
    input wire d_valid,
    output reg d_ready
);
  wire [ID_W-1:0] x_awid;
  wire [ADDR_W-1:0] x_awaddr;
  wire [8-1:0] x_awlen;
  wire [3-1:0] x_awsize;
  wire [2-1:0] x_awburst;
  wire x_awlock;
  wire [4-1:0] x_awcache;
  wire [3-1:0] x_awprot;
  wire [4-1:0] x_awqos;
  wire [4-1:0] x_awregion;
  wire x_awvalid;
  wire x_awready;
  wire [ID_W-1:0] x_wid;
  wire [DATA_W-1:0] x_wdata;
  wire [4-1:0] x_wstrb;
  wire x_wlast;
  wire x_wvalid;
  wire x_wready;
  wire [ID_W-1:0] x_bid;
  wire [2-1:0] x_bresp;
  wire x_buser;
  wire x_bvalid;
  wire x_bready;
  wire [ID_W-1:0] x_arid;
  wire [ADDR_W-1:0] x_araddr;
  wire [8-1:0] x_arlen;
  wire [3-1:0] x_arsize;
  wire [2-1:0] x_arburst;
  wire x_arlock;
  wire [4-1:0] x_arcache;
  wire [3-1:0] x_arprot;
  wire [4-1:0] x_arqos;
  wire [4-1:0] x_arregion;
  wire x_arvalid;
  wire x_arready;
  wire [ID_W-1:0] x_rid;
  wire [DATA_W-1:0] x_rdata;
  wire [2-1:0] x_rresp;
  wire x_ruser;
  wire x_rlast;
  wire x_rvalid;
  wire x_rready;
  nlv_axi_master #(.NAME(NAME), .ADDR_W(ADDR_W), .DATA_W(32), .ID_W(ID_W), .LEN_W(8)) peer (
    .clk(clk), .rst_n(rst_n),
    .awid(x_awid),
    .awaddr(x_awaddr),
    .awlen(x_awlen),
    .awsize(x_awsize),
    .awburst(x_awburst),
    .awlock(x_awlock),
    .awcache(x_awcache),
    .awprot(x_awprot),
    .awqos(x_awqos),
    .awregion(x_awregion),
    .awvalid(x_awvalid),
    .awready(x_awready),
    .wid(x_wid),
    .wdata(x_wdata),
    .wstrb(x_wstrb),
    .wlast(x_wlast),
    .wvalid(x_wvalid),
    .wready(x_wready),
    .bid(x_bid),
    .bresp(x_bresp),
    .buser(x_buser),
    .bvalid(x_bvalid),
    .bready(x_bready),
    .arid(x_arid),
    .araddr(x_araddr),
    .arlen(x_arlen),
    .arsize(x_arsize),
    .arburst(x_arburst),
    .arlock(x_arlock),
    .arcache(x_arcache),
    .arprot(x_arprot),
    .arqos(x_arqos),
    .arregion(x_arregion),
    .arvalid(x_arvalid),
    .arready(x_arready),
    .rid(x_rid),
    .rdata(x_rdata),
    .rresp(x_rresp),
    .ruser(x_ruser),
    .rlast(x_rlast),
    .rvalid(x_rvalid),
    .rready(x_rready)
  );
  integer raw_opcode=8, raw_param=0, raw_corrupt=0, raw_size=0, repeat_source=0, withdraw=0, retracted=0;
  integer read_denied=0, read_corrupt=0, write_denied=0, d_stalls=0;
  function [63:0] counter(input integer index); counter = index==21 ? check.peak : index==22 ? retracted : index==23 ? read_denied : index==24 ? read_corrupt : index==25 ? write_denied : index==26 ? d_stalls : peer.counter(index); endfunction
  task set_ctl(input integer index, input [31:0] value);
    case(index)
      96: raw_opcode=value;
      97: raw_corrupt=value;
      98: withdraw=value;
      99: repeat_source=value;
      100: raw_size=value;
      101: raw_param=value;
      default: peer.set_ctl(index, value);
    endcase
  endtask
  task push_write(input [63:0] addr, input [7:0] len, input [2:0] size, input [1:0] burst, input [31:0] id, input lock, input [3:0] cache, input [2:0] prot, input [3:0] qos, input [7:0] strb_mode, input [31:0] salt, input [31:0] tag); peer.push_write(addr,len,size,burst,id,lock,cache,prot,qos,strb_mode,salt,tag); endtask
  task push_read(input [63:0] addr, input [7:0] len, input [2:0] size, input [1:0] burst, input [31:0] id, input lock, input [3:0] cache, input [2:0] prot, input [3:0] qos, input [31:0] tag); peer.push_read(addr,len,size,burst,id,lock,cache,prot,qos,tag); endtask
  task stall_report(input integer min_hold); peer.stall_report(min_hold); endtask
  reg awfull=0, wfull=0, arfull=0;
  reg [ADDR_W-1:0] wa, ra;
  reg [ID_W-1:0] wi, ri;
  reg [2:0] ws, rs;
  reg [31:0] wd;
  reg [3:0] wm;
  integer write_left=0;
  reg choose=0;
  integer read_left[0:(1<<ID_W)-1];
  assign x_awready = !awfull;
  assign x_wready = !wfull;
  assign x_arready = !arfull;
  assign x_bvalid = d_valid && d_opcode != 1;
  assign x_rvalid = d_valid && d_opcode == 1;
  assign x_bid = d_source;
  assign x_rid = d_source;
  assign x_bresp = d_denied ? 2 : 0;
  assign x_rresp = d_denied || d_corrupt ? 2 : 0;
  assign x_rdata = d_data;
  assign x_rlast = read_left[d_source] == 0;
  assign x_buser = 0;
  assign x_ruser = 0;
  always @* d_ready = d_opcode != 1 ? x_bready : x_rready;
  initial begin
    a_valid=0; a_opcode=0; a_param=0; a_size=0; a_source=0;
    a_address=0; a_mask=0; a_data=0; a_corrupt=0;
  end
  always @(posedge clk) begin
    if (!rst_n) begin awfull<=0; wfull<=0; arfull<=0; a_valid<=0; choose<=0; end
    else begin
      if (d_valid && !d_ready) d_stalls<=d_stalls+1;
      if (d_valid && d_ready) begin
        if (d_opcode==1 && d_denied) read_denied<=read_denied+1;
        if (d_opcode==1 && d_corrupt) read_corrupt<=read_corrupt+1;
        if (d_opcode==0 && d_denied) write_denied<=write_denied+1;
      end
      if (x_awvalid && x_awready) begin awfull<=1; wa<=x_awaddr; wi<=x_awid; ws<=x_awsize; write_left<=x_awlen; end
      if (x_wvalid && x_wready) begin wfull<=1; wd<=x_wdata; wm<=x_wstrb; end
      if (d_valid && d_ready && d_opcode==1) read_left[d_source]<=read_left[d_source]-1;
      if (x_arvalid && x_arready) begin read_left[x_arid]<=x_arlen; arfull<=1; ra<=x_araddr; ri<=x_arid; rs<=x_arsize; end
      if (a_valid && a_ready) a_valid<=0;
      if (!a_valid && retracted!=1) begin
        if (awfull && wfull && (!arfull || !choose)) begin
          a_valid<=1; a_opcode<=raw_opcode<8 ? raw_opcode : wm==15 ? 0 : 1; a_size<=raw_size!=0 ? raw_size-1 : ws; a_source<=repeat_source ? 0 : wi; a_corrupt<=raw_corrupt;
          a_address<=wa; a_param<=raw_param; a_mask<=wm; a_data<=wd; awfull<=write_left!=0; write_left<=write_left-1; wfull<=0; choose<=1;
        end else if (arfull) begin
          a_valid<=1; a_opcode<=raw_opcode<8 ? raw_opcode : 4; a_size<=raw_size!=0 ? raw_size-1 : rs; a_source<=repeat_source ? 0 : ri; a_corrupt<=0;
          a_address<=ra; a_param<=raw_param; a_mask<=((1 << (1 << rs))-1) << (ra % 4);
          a_data<=0; arfull<=0; choose<=0;
        end
      end
      if (retracted==1 && !a_valid) begin a_valid<=1; retracted<=2; end
      if (withdraw!=0 && retracted==0 && a_valid && !a_ready) begin a_valid<=0; retracted<=1; end
    end
  end
  nlv_tl_checker #(.NAME(NAME), .ADDR_W(ADDR_W), .SOURCE_W(ID_W), .SIZE_W(SIZE_W)) check (
    .clk(clk), .rst_n(rst_n), .allow_reject(raw_opcode<8 || raw_corrupt || raw_size!=0),
    .a_opcode(a_opcode),
    .a_param(a_param),
    .a_size(a_size),
    .a_source(a_source),
    .a_address(a_address),
    .a_mask(a_mask),
    .a_data(a_data),
    .a_corrupt(a_corrupt),
    .a_valid(a_valid),
    .a_ready(a_ready),
    .d_opcode(d_opcode),
    .d_param(d_param),
    .d_size(d_size),
    .d_source(d_source),
    .d_data(d_data),
    .d_corrupt(d_corrupt),
    .d_denied(d_denied),
    .d_valid(d_valid),
    .d_ready(d_ready)
  );
endmodule
