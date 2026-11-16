integer cycle=0, messages=0, rwords=0, bwords=0;
integer sources[0:2], addresses[0:2], kinds[0:2];
reg drain=0;
always #5 nl_clk=~nl_clk;
always @(negedge nl_clk) begin nl_rrsp_ready=drain; nl_wrsp_ready=drain; end
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"response_owner",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    check(nl_tl_a_size==2,"ONE_D_WORD");
    sources[messages]=nl_tl_a_source; addresses[messages]=nl_tl_a_address;
    kinds[messages]=nl_tl_a_opcode; messages=messages+1;
  end
  check(!(nl_rrsp_valid && nl_wrsp_valid),"ONE_RESPONSE_FACE");
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    check(bwords==1,"RESPONSE_DIRECTION_TURN");
    check(nl_rrsp_id==3 && nl_rrsp_tag==3 && nl_rrsp_source==0,"READ_IDENTITY");
    check(nl_rrsp_address==4096+rwords && nl_rrsp_count==1,"READ_ADDRESS_COUNT");
    check(nl_rrsp_data==(32'h44332211 & (32'hff << (8*rwords))),"READ_BYTE_DATA");
    check(nl_rrsp_first==(rwords==0) && nl_rrsp_last==(rwords==3),"READ_FRAMING");
    check(!nl_probe_cut,"ONE_MESSAGE_SEGMENT"); rwords=rwords+1;
  end
  if(nl_wrsp_valid && nl_wrsp_id==2 && rwords!=0 && rwords!=4)
    nlv_fail_report("WRITE_WITHIN_READ_SEGMENT","response_owner",rwords);
  if(nl_wrsp_valid && nl_wrsp_ready) begin
    check(nl_wrsp_source==nl_wrsp_id && nl_wrsp_tag==nl_wrsp_id,"WRITE_IDENTITY");
    check(nl_wrsp_id==(bwords==0 ? 1 : 2),"WRITE_RESPONSE_ONCE");
    check(nl_wrsp_result==(bwords==0 ? 0 : 2),"WRITE_ERROR_IDENTITY"); bwords=bwords+1;
  end
end
task write_word(input integer id);
  begin
    @(negedge nl_clk); nl_wreq_address=8192+id*4; nl_wreq_start_address=nl_wreq_address;
    nl_wreq_length=0; nl_wreq_size=2; nl_wreq_burst=1; nl_wreq_count=4; nl_wreq_be=15;
    nl_wreq_id=id; nl_wreq_source=id; nl_wreq_tag=id;
    nl_wreq_first=1; nl_wreq_last=1; nl_wreq_data=32'h22000000+id; nl_wreq_valid=1;
    @(posedge nl_clk); while(!nl_wreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_wreq_valid=0;
  end
endtask
task respond(input integer index, input denied);
  begin
    @(negedge nl_clk); nl_tl_d_source=sources[index]; nl_tl_d_size=2;
    nl_tl_d_opcode=kinds[index]==4 ? 1 : 0; nl_tl_d_data=32'h44332211;
    nl_tl_d_denied=denied; nl_tl_d_valid=1;
    @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_tl_d_valid=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(2),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(3)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid),.d_ready(nl_tl_d_ready));
initial begin
  repeat(160) @(negedge nl_clk); nl_reset_n=1; nl_tl_a_ready=1;
  nl_rreq_address=4096; nl_rreq_start_address=4096; nl_rreq_length=3;
  nl_rreq_size=0; nl_rreq_burst=1; nl_rreq_count=1; nl_rreq_be=1;
  nl_rreq_id=3; nl_rreq_source=0; nl_rreq_tag=3;
  nl_rreq_first=1; nl_rreq_last=1; nl_rreq_valid=1;
  @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_rreq_valid=0;
  write_word(1); wait(messages==2); respond(1,0);
  write_word(2); wait(messages==3); wait(nl_wrsp_valid);
  check(nl_wrsp_id==1,"FIRST_B_OFFER"); respond(0,0); respond(2,1);
  repeat(8) begin
    @(posedge nl_clk);
    check(nl_wrsp_valid && nl_wrsp_id==1 && !nl_rrsp_valid,"RESPONSE_DIRECTION_HOLD");
  end
  @(negedge nl_clk); drain=1;
  wait(rwords==4 && bwords==2);
  repeat(8) @(negedge nl_clk);
  $display("UH_CONTEXTS_PASS response_owner read_words=%0d write_responses=%0d",rwords,bwords); $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
