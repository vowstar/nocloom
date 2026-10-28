localparam SEED = @SEED@;
localparam WIDE = @WIDE@;
localparam COUNT = 48;
integer phase=0, k;
integer rdone[0:2], wdone[0:2];
reg [39:0] wq[0:2][0:63];
integer wq_w[0:2], wq_r[0:2];
// Each request has its own reproducible word. Timing never consumes random state.
function automatic [49:0] request(input integer s, input integer n, input integer write_dir);
  reg [31:0] x;
  reg [7:0] len;
  reg [3:0] pause;
  begin
    x=SEED ^ (s*97+n*73+write_dir*193);
    x=x ^ (x<<13); x=x ^ (x>>17); x=x ^ (x<<5);
    len=(n<8) ? 0 : (x%8);
    pause=(n<8 || n>=32) ? (x%4) : 0;
    request={pause,4'd0,2'(n%4),len,32'(s*16384+n*64)};
  end
endfunction
initial begin
  for(k=0;k<3;k=k+1) begin
    araddr[k]=0; awaddr[k]=0; wdata[k]=0; arlen[k]=0; awlen[k]=0;
    arid[k]=0; awid[k]=0; arqos[k]=0; awqos[k]=0;
    wq_w[k]=0; wq_r[k]=0; rdone[k]=0; wdone[k]=0;
  end
  #10; RstN=1; #10; RstN=0;
  repeat(20) @(negedge Clk); RstN=1;
  // Phase zero covers short bursts before pressure builds.
  for(k=0;k<3;k=k+1) wait(rdone[k]>=8 && wdone[k]>=8);
  @(negedge Clk); phase=1;
  $display("PHASE %0d 1",cyc);
  // Phase one queues all sources behind bounded target stalls.
  for(k=0;k<3;k=k+1) wait(rdone[k]>=32 && wdone[k]>=32);
  @(negedge Clk); phase=2;
  $display("PHASE %0d 2",cyc);
  // Phase two checks draining and renewed service after contention.
  for(k=0;k<3;k=k+1) wait(rdone[k]==COUNT && wdone[k]==COUNT);
  repeat(20) @(negedge Clk);
  $display("CHECK_PASS pressure %0d",cyc); $finish;
end

task automatic issue_ar(input integer s);
  integer n; reg [49:0] e;
  begin
    wait(RstN && cyc>=24);
    for(n=0;n<COUNT;n=n+1) begin
      wait(phase >= ((n<8) ? 0 : (n<32) ? 1 : 2));
      e=request(s,n,0);
      repeat(e[49:46]+1) @(negedge Clk);
      araddr[s]=e[31:0]; arlen[s]=e[39:32]; arid[s]=e[41:40]; arvalid[s]=1;
      @(posedge Clk); while(!arready[s]) @(posedge Clk);
      $display("SRC %0d R %0d %0d %h %0d",cyc,s,arid[s],araddr[s],arlen[s]);
      @(negedge Clk); arvalid[s]=0;
    end
  end
endtask

task automatic issue_aw(input integer s);
  integer n; reg [49:0] e;
  begin
    wait(RstN && cyc>=24);
    for(n=0;n<COUNT;n=n+1) begin
      wait(phase >= ((n<8) ? 0 : (n<32) ? 1 : 2));
      e=request(s,n,1);
      repeat(e[49:46]+1) @(negedge Clk);
      awaddr[s]=e[31:0]; awlen[s]=e[39:32]; awid[s]=e[41:40]; awvalid[s]=1;
      wq[s][wq_w[s]%64]=e[39:0]; wq_w[s]=wq_w[s]+1;
      @(posedge Clk); while(!awready[s]) @(posedge Clk);
      $display("SRC %0d W %0d %0d %h %0d",cyc,s,awid[s],awaddr[s],awlen[s]);
      @(negedge Clk); awvalid[s]=0;
    end
  end
endtask

task automatic drive_w(input integer s);
  integer n,b; reg [39:0] e;
  begin
    wait(RstN && cyc>=24);
    for(n=0;n<COUNT;n=n+1) begin
      wait(wq_r[s]<wq_w[s]);
      e=wq[s][wq_r[s]%64];
      for(b=0;b<=e[39:32];b=b+1) begin
        @(negedge Clk);
        if(WIDE && s==0)
          wdata[s]={32'((e[31:0]+8*b+32'd4)^32'h5a000000),32'((e[31:0]+8*b)^32'h5a000000)};
        else wdata[s]=(e[31:0]+4*b)^32'h5a000000;
        wlast[s]=(b==e[39:32]); wvalid[s]=1;
        @(posedge Clk); while(!wready[s]) @(posedge Clk);
        @(negedge Clk); wvalid[s]=0; wlast[s]=0;
      end
      wq_r[s]=wq_r[s]+1;
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
integer si;
always @(posedge Clk) if(RstN) for(si=0;si<3;si=si+1) begin
  if(rvalid[si]) begin
    $display("RET %0d R %0d %0d %h %0d %0d",cyc,si,rid[si],rdata[si],rlast[si],rresp[si]);
    if(rlast[si]) rdone[si]=rdone[si]+1;
  end
  if(bvalid[si]) begin
    $display("RET %0d W %0d %0d 0 1 %0d",cyc,si,bid[si],bresp[si]);
    wdone[si]=wdone[si]+1;
  end
end
// Target delays use accepted transaction counts. Each stall has at most three edges.
reg [43:0] tq[0:255], twq[0:255];
integer tq_w=0,tq_r=0,tbeat=0,tlat=0,twq_w=0,twq_r=0,twbeat=0;
integer ar_wait=0,aw_wait=0,w_wait=0;
reg [3:0] tbq[0:255]; integer tbq_w=0,tbq_r=0;
reg [32:0] tdq[0:2047]; integer tdq_w=0,tdq_r=0;
always @(negedge Clk) begin
  if(!RstN) begin t_arready=0; t_awready=0; t_wready=0; end
  else begin
    t_arready=(ar_wait==0); t_awready=(aw_wait==0); t_wready=(w_wait==0);
    if(ar_wait>0) ar_wait=ar_wait-1;
    if(aw_wait>0) aw_wait=aw_wait-1;
    if(w_wait>0) w_wait=w_wait-1;
  end
end
always @(posedge Clk) if(RstN) begin
  if(t_arvalid && t_arready) begin
    tq[tq_w%256]={t_arid,t_arlen,t_araddr}; tq_w=tq_w+1; ar_wait=1+(tq_w+SEED)%3;
    $display("TARGET %0d R %0d %h %0d",cyc,t_arid,t_araddr,t_arlen);
  end
  if(t_awvalid && t_awready) begin
    twq[twq_w%256]={t_awid,t_awlen,t_awaddr}; twq_w=twq_w+1; aw_wait=1+(twq_w+SEED)%2;
    $display("TARGET %0d W %0d %h %0d",cyc,t_awid,t_awaddr,t_awlen);
  end
  if(t_wvalid && t_wready) begin
    if(t_wstrb!==4'hf) $fatal(1,"TARGET_WRITE_STROBE");
    tdq[tdq_w%2048]={t_wlast,t_wdata}; tdq_w=tdq_w+1; w_wait=tdq_w%2;
    $display("WRITE %0d %h %0d",cyc,t_wdata,t_wlast);
  end
  // AXI permits W before AW. Pair accepted data only after its address is known.
  if(twq_r<twq_w && tdq_r<tdq_w) begin
    if(tdq[tdq_r%2048][31:0]!==((twq[twq_r%256][31:0]+4*twbeat)^32'h5a000000)
       || tdq[tdq_r%2048][32]!==(twbeat==twq[twq_r%256][39:32]))
      $fatal(1,"TARGET_WRITE_BYTES");
    if(tdq[tdq_r%2048][32]) begin
      tbq[tbq_w%256]=twq[twq_r%256][43:40]; tbq_w=tbq_w+1; twq_r=twq_r+1; twbeat=0;
    end else twbeat=twbeat+1;
    tdq_r=tdq_r+1;
  end
  if(t_bvalid && t_bready) t_bvalid<=0;
  if((!t_bvalid || t_bready) && tbq_r<tbq_w) begin
    t_bvalid<=1; t_bid<=tbq[tbq_r%256]; tbq_r=tbq_r+1;
  end
  if(t_rvalid && t_rready) begin
    if(t_rlast) begin t_rvalid<=0; tq_r=tq_r+1; tbeat=0; tlat=0; end
    else tbeat=tbeat+1;
  end
  if(tq_r<tq_w && !(t_rvalid && t_rready && t_rlast)) begin
    if(tlat<2) tlat=tlat+1;
    else if(!t_rvalid || t_rready) begin
      t_rvalid<=1; t_rid<=tq[tq_r%256][43:40]; t_rlast<=(tbeat==tq[tq_r%256][39:32]);
      t_rdata<=32'ha5000000|((tq[tq_r%256][31:0]+4*tbeat)&16'hffff);
    end
  end
end
initial begin #100000000; $fatal(1,"overall timeout"); end

// Monitor every active target request field on each destination clock edge.
wire [56:0] ar_fields={t_arid,t_arlen,t_araddr,t_arcache,t_arprot,t_arburst,t_arlock,t_arsize};
wire [56:0] aw_fields={t_awid,t_awlen,t_awaddr,t_awcache,t_awprot,t_awburst,t_awlock,t_awsize};
wire [36:0] w_fields={t_wdata,t_wstrb,t_wlast};
reg ar_stalled=0,aw_stalled=0,w_stalled=0;
reg [56:0] ar_kept,aw_kept;
reg [36:0] w_kept;
always @(posedge Clk) begin
  if(RstN) begin
    if(ar_stalled && (!t_arvalid || ar_fields!==ar_kept)) $fatal(1,"AXI_AR_STABLE");
    if(aw_stalled && (!t_awvalid || aw_fields!==aw_kept)) $fatal(1,"AXI_AW_STABLE");
    if(w_stalled && (!t_wvalid || w_fields!==w_kept)) $fatal(1,"AXI_W_STABLE");
    if(t_arvalid && (^ar_fields===1'bx)) $fatal(1,"AXI_AR_UNKNOWN");
    if(t_awvalid && (^aw_fields===1'bx)) $fatal(1,"AXI_AW_UNKNOWN");
    if(t_wvalid && (^w_fields===1'bx)) $fatal(1,"AXI_W_UNKNOWN");
  end
  ar_stalled=RstN && t_arvalid && !t_arready;
  aw_stalled=RstN && t_awvalid && !t_awready;
  w_stalled=RstN && t_wvalid && !t_wready;
  ar_kept=ar_fields; aw_kept=aw_fields; w_kept=w_fields;
end
