// A packet keeps its grant through the accepted tail, including stalls.
// Waiting higher levels compete at the next packet boundary.
// Link pipes must retain the scheduling hint with buffered work.
`timescale 1ps/1ps
module tb;
reg Clk=0, RstN=0, test_mode=0, Clk2=0;
always #625 Clk=~Clk;
integer cyc=0;
always @(posedge Clk) cyc <= cyc+1;
// Source requests encode pause, ID, length and address.
reg [45:0] RROM[0:3071], WROM[0:3071];
@STIMULUS@
reg [31:0] araddr[0:2], awaddr[0:2], wdata[0:2];
reg [7:0] arlen[0:2], awlen[0:2];
reg [1:0] arid[0:2], awid[0:2];
reg [2:0] arvalid=0, awvalid=0, wvalid=0, wlast=0;
wire [2:0] arready, awready, wready, bvalid, rvalid, rlast;
wire [1:0] bid[0:2], bresp[0:2], rid[0:2], rresp[0:2];
wire [31:0] rdata[0:2];
wire [31:0] t_araddr, t_awaddr;
wire [3:0] t_arid, t_awid; wire [7:0] t_arlen, t_awlen;
wire t_arvalid, t_awvalid, t_wvalid, t_wlast, t_bready, t_rready;
wire [31:0] t_wdata;
reg t_arready, t_awready, t_wready, t_bvalid=0, t_rvalid=0, t_rlast=0;
reg [3:0] t_bid=0, t_rid=0; reg [31:0] t_rdata=0;
nocloom_qos_registered_chain_network dut(.clk(Clk),
 .reset_n(RstN),
 .test(test_mode),
 .nl_i_s0_araddr(araddr[0]),
 .nl_i_s0_arburst(2'b01),
 .nl_i_s0_arcache(4'b0011),
 .nl_i_s0_arid(arid[0]),
 .nl_i_s0_arlen(arlen[0]),
 .nl_i_s0_arlock(1'b0),
 .nl_i_s0_arprot(3'b000),
 .nl_i_s0_arready(arready[0]),
 .nl_i_s0_arsize(3'd2),
 .nl_i_s0_arvalid(arvalid[0]),
 .nl_i_s0_awaddr(awaddr[0]),
 .nl_i_s0_awburst(2'b01),
 .nl_i_s0_awcache(4'b0011),
 .nl_i_s0_awid(awid[0]),
 .nl_i_s0_awlen(awlen[0]),
 .nl_i_s0_awlock(1'b0),
 .nl_i_s0_awprot(3'b000),
 .nl_i_s0_awready(awready[0]),
 .nl_i_s0_awsize(3'd2),
 .nl_i_s0_awvalid(awvalid[0]),
 .nl_i_s0_bid(bid[0]),
 .nl_i_s0_bready(1'b1),
 .nl_i_s0_bresp(bresp[0]),
 .nl_i_s0_bvalid(bvalid[0]),
 .nl_i_s0_rdata(rdata[0]),
 .nl_i_s0_rid(rid[0]),
 .nl_i_s0_rlast(rlast[0]),
 .nl_i_s0_rready(1'b1),
 .nl_i_s0_rresp(rresp[0]),
 .nl_i_s0_rvalid(rvalid[0]),
 .nl_i_s0_wdata(wdata[0]),
 .nl_i_s0_wlast(wlast[0]),
 .nl_i_s0_wready(wready[0]),
 .nl_i_s0_wstrb(4'hf),
 .nl_i_s0_wvalid(wvalid[0]),
 .nl_i_s1_araddr(araddr[1]),
 .nl_i_s1_arburst(2'b01),
 .nl_i_s1_arcache(4'b0011),
 .nl_i_s1_arid(arid[1]),
 .nl_i_s1_arlen(arlen[1]),
 .nl_i_s1_arlock(1'b0),
 .nl_i_s1_arprot(3'b000),
 .nl_i_s1_arready(arready[1]),
 .nl_i_s1_arsize(3'd2),
 .nl_i_s1_arvalid(arvalid[1]),
 .nl_i_s1_awaddr(awaddr[1]),
 .nl_i_s1_awburst(2'b01),
 .nl_i_s1_awcache(4'b0011),
 .nl_i_s1_awid(awid[1]),
 .nl_i_s1_awlen(awlen[1]),
 .nl_i_s1_awlock(1'b0),
 .nl_i_s1_awprot(3'b000),
 .nl_i_s1_awready(awready[1]),
 .nl_i_s1_awsize(3'd2),
 .nl_i_s1_awvalid(awvalid[1]),
 .nl_i_s1_bid(bid[1]),
 .nl_i_s1_bready(1'b1),
 .nl_i_s1_bresp(bresp[1]),
 .nl_i_s1_bvalid(bvalid[1]),
 .nl_i_s1_rdata(rdata[1]),
 .nl_i_s1_rid(rid[1]),
 .nl_i_s1_rlast(rlast[1]),
 .nl_i_s1_rready(1'b1),
 .nl_i_s1_rresp(rresp[1]),
 .nl_i_s1_rvalid(rvalid[1]),
 .nl_i_s1_wdata(wdata[1]),
 .nl_i_s1_wlast(wlast[1]),
 .nl_i_s1_wready(wready[1]),
 .nl_i_s1_wstrb(4'hf),
 .nl_i_s1_wvalid(wvalid[1]),
 .nl_i_s2_araddr(araddr[2]),
 .nl_i_s2_arburst(2'b01),
 .nl_i_s2_arcache(4'b0011),
 .nl_i_s2_arid(arid[2]),
 .nl_i_s2_arlen(arlen[2]),
 .nl_i_s2_arlock(1'b0),
 .nl_i_s2_arprot(3'b000),
 .nl_i_s2_arready(arready[2]),
 .nl_i_s2_arsize(3'd2),
 .nl_i_s2_arvalid(arvalid[2]),
 .nl_i_s2_awaddr(awaddr[2]),
 .nl_i_s2_awburst(2'b01),
 .nl_i_s2_awcache(4'b0011),
 .nl_i_s2_awid(awid[2]),
 .nl_i_s2_awlen(awlen[2]),
 .nl_i_s2_awlock(1'b0),
 .nl_i_s2_awprot(3'b000),
 .nl_i_s2_awready(awready[2]),
 .nl_i_s2_awsize(3'd2),
 .nl_i_s2_awvalid(awvalid[2]),
 .nl_i_s2_bid(bid[2]),
 .nl_i_s2_bready(1'b1),
 .nl_i_s2_bresp(bresp[2]),
 .nl_i_s2_bvalid(bvalid[2]),
 .nl_i_s2_rdata(rdata[2]),
 .nl_i_s2_rid(rid[2]),
 .nl_i_s2_rlast(rlast[2]),
 .nl_i_s2_rready(1'b1),
 .nl_i_s2_rresp(rresp[2]),
 .nl_i_s2_rvalid(rvalid[2]),
 .nl_i_s2_wdata(wdata[2]),
 .nl_i_s2_wlast(wlast[2]),
 .nl_i_s2_wready(wready[2]),
 .nl_i_s2_wstrb(4'hf),
 .nl_i_s2_wvalid(wvalid[2]),
 .nl_t_t0_araddr(t_araddr),
 .nl_t_t0_arburst(),
 .nl_t_t0_arcache(),
 .nl_t_t0_arid(t_arid),
 .nl_t_t0_arlen(t_arlen),
 .nl_t_t0_arlock(),
 .nl_t_t0_arprot(),
 .nl_t_t0_arready(t_arready),
 .nl_t_t0_arsize(),
 .nl_t_t0_arvalid(t_arvalid),
 .nl_t_t0_awaddr(t_awaddr),
 .nl_t_t0_awburst(),
 .nl_t_t0_awcache(),
 .nl_t_t0_awid(t_awid),
 .nl_t_t0_awlen(t_awlen),
 .nl_t_t0_awlock(),
 .nl_t_t0_awprot(),
 .nl_t_t0_awready(t_awready),
 .nl_t_t0_awsize(),
 .nl_t_t0_awvalid(t_awvalid),
 .nl_t_t0_bid(t_bid),
 .nl_t_t0_bready(t_bready),
 .nl_t_t0_bresp(2'b00),
 .nl_t_t0_bvalid(t_bvalid),
 .nl_t_t0_rdata(t_rdata),
 .nl_t_t0_rid(t_rid),
 .nl_t_t0_rlast(t_rlast),
 .nl_t_t0_rready(t_rready),
 .nl_t_t0_rresp(2'b00),
 .nl_t_t0_rvalid(t_rvalid),
 .nl_t_t0_wdata(t_wdata),
 .nl_t_t0_wlast(t_wlast),
 .nl_t_t0_wready(t_wready),
 .nl_t_t0_wstrb(),
 .nl_t_t0_wvalid(t_wvalid));
integer k;
initial begin
  for(k=0;k<3;k=k+1) begin araddr[k]=0; awaddr[k]=0; wdata[k]=0; arlen[k]=0; awlen[k]=0; arid[k]=0; awid[k]=0; end
  #10; RstN=1; test_mode=1; #10; RstN=0;
  repeat(20) @(negedge Clk); RstN=1;
end
// Each accepted AW queues its address and length until all W beats transfer.
reg [39:0] wq[0:2][0:63]; integer wq_w[0:2], wq_r[0:2];
integer rdone[0:2], wdone[0:2], rbeat[0:2];
reg [2:0] fin_r=0, fin_w=0, fin_wd=0;
initial for(k=0;k<3;k=k+1) begin wq_w[k]=0; wq_r[k]=0; rdone[k]=0; wdone[k]=0; rbeat[k]=0; end
task automatic issue_ar(input integer s);
integer n; reg [45:0] e;
begin
  wait(cyc>=24 && RstN==1); repeat(16) @(negedge Clk);
  for(n=0;n<300 && cyc<200000;n=n+1) begin
    e = RROM[s*1024+n];
    repeat(e[45:42]) @(negedge Clk);
    araddr[s]=e[31:0]; arlen[s]=e[39:32]; arid[s]=e[41:40]; arvalid[s]=1;
    @(posedge Clk); while(arready[s]!==1'b1) @(posedge Clk);
    @(negedge Clk); arvalid[s]=0;
  end
  fin_r[s]=1;
end
endtask
task automatic issue_aw(input integer s);
integer n; reg [45:0] e;
begin
  wait(cyc>=24 && RstN==1); repeat(16) @(negedge Clk);
  for(n=0;n<300 && cyc<200000;n=n+1) begin
    e = WROM[s*1024+n];
    repeat(e[45:42]) @(negedge Clk);
    awaddr[s]=e[31:0]; awlen[s]=e[39:32]; awid[s]=e[41:40]; awvalid[s]=1;
    wq[s][wq_w[s]%64] = e[39:0]; wq_w[s]=wq_w[s]+1;
    @(posedge Clk); while(awready[s]!==1'b1) @(posedge Clk);
    @(negedge Clk); awvalid[s]=0;
  end
  fin_w[s]=1;
end
endtask
task automatic drive_w(input integer s);
integer b; reg [39:0] e;
begin
  wait(cyc>=24 && RstN==1);
  forever begin
    @(negedge Clk);
    if(wq_r[s] < wq_w[s]) begin
      e = wq[s][wq_r[s]%64];
      for(b=0;b<=e[39:32];b=b+1) begin
        wdata[s] = e[31:0] + 4*b ^ 32'h5a000000; wlast[s] = (b==e[39:32]); wvalid[s]=1;
        @(posedge Clk); while(wready[s]!==1'b1) @(posedge Clk);
        @(negedge Clk); wvalid[s]=0; wlast[s]=0;
      end
      wq_r[s]=wq_r[s]+1;
    end
  end
end
endtask
initial issue_ar(0);
initial issue_aw(0);
initial drive_w(0);
initial issue_ar(1);
initial issue_aw(1);
initial drive_w(1);
initial issue_ar(2);
initial issue_aw(2);
initial drive_w(2);

// Read data carries the address so each source can check response order per ID.
integer s_i;
always @(posedge Clk) if(RstN) for(s_i=0;s_i<3;s_i=s_i+1) begin
  if(rvalid[s_i]) begin
    if(rresp[s_i]!==2'b00 || rdata[s_i][31:24]!==8'ha5) $fatal(1,"bad read s=%0d data=%h resp=%b", s_i, rdata[s_i], rresp[s_i]);
    if(rlast[s_i]) begin rdone[s_i]=rdone[s_i]+1; $display("DONE %0d R %0d %h", cyc, s_i, rdata[s_i]); end
  end
  if(bvalid[s_i]) begin
    if(bresp[s_i]!==2'b00) $fatal(1,"bad write response s=%0d", s_i);
    wdone[s_i]=wdone[s_i]+1; $display("DONE %0d B %0d %0d", cyc, s_i, bid[s_i]);
  end
end
// The target answers reads in AR order and pairs W and B with accepted AW.
reg [43:0] tq[0:255]; integer tq_w=0, tq_r=0, tbeat=0, tlat=0;
reg [43:0] twq[0:255]; integer twq_w=0, twq_r=0, twl=0;
reg [3:0] tbq[0:255]; integer tbq_w=0, tbq_r=0;
always @(posedge Clk) begin
  begin t_arready <= (cyc%7 != 3); t_awready <= (cyc%9 != 5); t_wready <= (cyc%5 != 2); end
end
always @(posedge Clk) if(RstN) begin
  if(t_arvalid && t_arready) begin tq[tq_w%256] = {t_arid, t_arlen, t_araddr}; tq_w=tq_w+1; $display("TAR %0d %h %0d", cyc, t_araddr[15:0], t_arlen); end
  if(t_awvalid && t_awready) begin twq[twq_w%256] = {t_awid, t_awlen, t_awaddr}; twq_w=twq_w+1; $display("TAW %0d %h %0d", cyc, t_awaddr[15:0], t_awlen); end
  if(t_wvalid && t_wready) begin
    if(t_wdata[31:24] !== 8'h5a) $fatal(1,"bad write data at target %h", t_wdata);
    if(t_wlast) twl=twl+1;
  end
  if(twq_r < twq_w && twq_r < twl) begin tbq[tbq_w%256] = twq[twq_r%256][43:40]; tbq_w=tbq_w+1; twq_r=twq_r+1; end
  // Hold B until its handshake.
  if(t_bvalid && t_bready) t_bvalid <= 0;
  if((!t_bvalid || t_bready) && tbq_r < tbq_w && !(t_bvalid && !t_bready)) begin
    begin t_bvalid <= 1; t_bid <= tbq[tbq_r%256]; tbq_r=tbq_r+1; end
  end
  // Hold each R beat until its handshake, then advance the burst.
  if(t_rvalid && t_rready) begin
    if(t_rlast) begin t_rvalid <= 0; tq_r=tq_r+1; tbeat=0; tlat=0; end else tbeat=tbeat+1;
  end
  if(tq_r < tq_w && !(t_rvalid && t_rready && t_rlast)) begin
    if(tlat < 2) tlat=tlat+1;
    else begin
      t_rvalid <= 1; t_rid <= tq[tq_r%256][43:40]; t_rlast <= (tbeat == tq[tq_r%256][39:32]);
      t_rdata <= 32'ha5000000 | ((tq[tq_r%256][15:0] + 4*tbeat) & 16'hffff);
    end
  end
end
always @(posedge Clk) if(RstN) begin
@GD_OBSERVE@
@GA_OBSERVE@
end
initial begin
  wait(fin_r==3'b111 && fin_w==3'b111);
  for(k=0;k<3;k=k+1) wait(rdone[k] >= 300 && wdone[k] >= 300);
  repeat(100) @(posedge Clk);
  $display("CHECK_PASS pressure %0d", cyc); $finish;
end
initial begin #1000000000; $fatal(1,"overall timeout"); end
endmodule
