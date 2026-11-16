integer early=0, put_active=0, denied=0;
integer cycle=0, issued=0, returned=0;
integer request_source[0:16], occupied[0:15], answered[0:16];
integer logical_id;
always #5 clk=~clk;
function [31:0] value_at(input integer index);
  value_at=32'hda000000+index;
endfunction
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"capacity",0);
endtask
always @(posedge clk) if(reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(t_a_valid && t_a_ready) begin
    check(t_a_opcode==4 && t_a_size==2,"GET_SHAPE");
    logical_id=(t_a_address-4096)/256;
    check(logical_id>=0 && logical_id<=CAPACITY && t_a_address==4096+logical_id*256,"GET_ADDRESS");
    check(t_a_source<CAPACITY && !occupied[t_a_source],"CAPACITY_SOURCE_UNIQUE");
    request_source[logical_id]=t_a_source; occupied[t_a_source]=1;
    issued=issued+1;
  end
  if(s_rvalid && s_rready) begin
    logical_id=s_rid;
    check(logical_id>=0 && logical_id<=CAPACITY && !answered[logical_id],"RETURN_ONCE");
    check(s_rresp==0 && s_rlast && s_rdata==value_at(logical_id),"RETURN_DATA");
    answered[logical_id]=1; returned=returned+1;
  end
  check(!s_bvalid,"NO_WRITE_RESPONSE");
end
task send_read(input integer index);
  begin
    @(negedge clk); s_araddr=4096+index*256; s_arid=index;
    s_arlen=0; s_arsize=2; s_arburst=1; s_arvalid=1;
    @(posedge clk); while(!s_arready) @(posedge clk);
    @(negedge clk); s_arvalid=0;
  end
endtask
task respond(input integer index);
  begin
    @(negedge clk); t_d_source=request_source[index]; t_d_size=2; t_d_opcode=1;
    t_d_data=value_at(index); t_d_valid=1;
    @(posedge clk); while(!t_d_ready) @(posedge clk);
    occupied[request_source[index]]=0;
    @(negedge clk); t_d_valid=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(SOURCE_WIDTH),.SIZE_W(SIZE_WIDTH),.UH(1),.SOURCE_LIMIT(CAPACITY)) checker_t (
  .clk(clk),.rst_n(reset_n),.allow_reject(1'b0),
  .a_opcode(t_a_opcode),.a_param(t_a_param),.a_size(t_a_size),.a_source(t_a_source),
  .a_address(t_a_address),.a_mask(t_a_mask),.a_data(t_a_data),.a_corrupt(t_a_corrupt),
  .a_valid(t_a_valid),.a_ready(t_a_ready),
  .d_opcode(t_d_opcode),.d_param(t_d_param),.d_size(t_d_size),.d_source(t_d_source),
  .d_data(t_d_data),.d_corrupt(t_d_corrupt),.d_denied(t_d_denied),
  .d_valid(t_d_valid),.d_ready(t_d_ready));
nlv_tl_uh_axi_check #(.NAME("source"),.ID_W(5)) checker_s (
  .clk(clk),.rst_n(reset_n),
  .awaddr(s_awaddr),.araddr(s_araddr),.awid(s_awid),.arid(s_arid),.bid(s_bid),.rid(s_rid),
  .awlen(s_awlen),.arlen(s_arlen),.awsize(s_awsize),.arsize(s_arsize),
  .awprot(s_awprot),.arprot(s_arprot),.awburst(s_awburst),.arburst(s_arburst),
  .bresp(s_bresp),.rresp(s_rresp),.awcache(s_awcache),.arcache(s_arcache),
  .awlock(s_awlock),.arlock(s_arlock),.awvalid(s_awvalid),.awready(s_awready),
  .arvalid(s_arvalid),.arready(s_arready),.wdata(s_wdata),.rdata(s_rdata),
  .wstrb(s_wstrb),.wlast(s_wlast),.wvalid(s_wvalid),.wready(s_wready),
  .bvalid(s_bvalid),.bready(s_bready),.rlast(s_rlast),.rvalid(s_rvalid),.rready(s_rready));
initial begin
  for(integer k=0;k<16;k=k+1) occupied[k]=0;
  for(integer k=0;k<17;k=k+1) begin request_source[k]=-1; answered[k]=0; end
  repeat(160) @(negedge clk); reset_n=1; t_a_ready=1;
  for(integer k=0;k<CAPACITY;k=k+1) send_read(k);
  wait(issued==CAPACITY);
  send_read(CAPACITY);
  repeat(32) @(negedge clk);
  check(issued==CAPACITY,"FULL_POOL_NO_EXTRA_GET");
  check(t_timed_out,"TIMEOUT_ONLY_REPORTS");
  for(integer k=CAPACITY-1;k>=0;k=k-1) respond(k);
  wait(issued==CAPACITY+1); respond(CAPACITY);
  s_rready=1; wait(returned==CAPACITY+1);
  repeat(8) @(negedge clk);
  check(issued==CAPACITY+1 && returned==CAPACITY+1,"FULL_POOL_RETURNED");
  $display("UH_CONTEXTS_PASS network_capacity=%0d issued=%0d returned=%0d",CAPACITY,issued,returned); $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
