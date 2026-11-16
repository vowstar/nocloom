localparam LANES=DATA_WIDTH/8;
integer cycle=0, q_done=0, p_done=0, b_done=0, w_beats=0, ack_seen=0;
integer p1_source=-1,p2_source=-1,q_source=-1,w_source=-1,w_gap=0;
reg manual_valid=0;
reg [31:0] manual_data=0;
reg [3:0] manual_size=0;
reg [15:0] manual_source=0;
wire first_put=t_a_valid && t_a_ready && t_a_opcode!=4 && w_beats==0;
wire target_clk=clk;
always #5 clk=~clk;
always @* begin
 t_a_ready=reset_n && (w_gap==0);
 t_d_valid=first_put || manual_valid;
 t_d_opcode=first_put ? 0 : 1;
 t_d_size=first_put ? t_a_size : manual_size;
 t_d_source=first_put ? t_a_source : manual_source;
 t_d_data=manual_data; t_d_denied=first_put; t_d_corrupt=0; t_d_param=0;
 s_rready=reset_n && cycle%5!=1; s_bready=reset_n && cycle%7!=3;
end
function [7:0] pattern(input integer address);
 pattern=(address*37+(address/16)*11+93)%256;
endfunction
function [31:0] word_at(input integer address);
 for(integer b=0;b<4;b=b+1) word_at[b*8+:8]=pattern(address+b);
endfunction
task nlv_fail_report(input [255:0] rule_name,input [255:0] who,input [63:0] value);
 begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle);$fatal(1);end
endtask
task check(input ok,input [255:0] label);
 if(!ok) nlv_fail_report(label,"cut",0);
endtask
always @(posedge clk) if(reset_n) begin
 cycle=cycle+1; check(cycle<20000,"CUT_PROGRESS");
 if(w_gap>0) w_gap=w_gap-1;
 if(t_a_valid && t_a_ready) begin
  if(t_a_opcode==4) begin
   case(t_a_address)
    4100:begin check(t_a_size==2 && p1_source<0,"P1_FOUR_BYTES");p1_source=t_a_source;end
    4104:begin check(t_a_size==3 && p2_source<0,"P2_EIGHT_BYTES");p2_source=t_a_source;end
    8192:begin check(t_a_size==5 && q_source<0,"Q_EIGHT_WORDS");q_source=t_a_source;end
    default:check(0,"READ_MESSAGE_ADDRESS");
   endcase
  end else begin
   check(t_a_size==4 && t_a_opcode==1,"WRITE_MESSAGE");
   if(w_beats==0) begin w_source=t_a_source; w_gap=24; end
   check(t_a_source==w_source && t_a_data==word_at(32768+4*w_beats) && t_a_mask==15,"WRITE_BYTES");
   w_beats=w_beats+1;
  end
 end
 if(first_put) begin check(t_d_ready,"SAME_CYCLE_EARLY_ACK"); ack_seen=ack_seen+1;end
 if(s_bvalid && s_bready) begin
  check(s_bid==7 && s_bresp==2 && w_beats==4 && ack_seen==1,"WRITE_DENIED_AFTER_TAIL");b_done=b_done+1;
 end
 if(s_rvalid && s_rready) begin
  check(s_rresp==0,"CUT_READ_RESPONSE");
  if(s_rid==4) begin
   check(q_done==2 && s_rlast && p_done==0,"Q_BEFORE_P_COMPLETION");
   for(integer b=4;b<16;b=b+1) check(s_rdata[b*8+:8]==pattern(4096+b),"P_PARTIAL_BYTE_DATA");
   p_done=p_done+1;
  end else begin
   check(s_rid==5 && q_done<2 && s_rlast==(q_done==1),"Q_RESPONSE_ID");
   for(integer b=0;b<16;b=b+1) check(s_rdata[b*8+:8]==pattern(8192+q_done*16+b),"Q_BYTE_DATA");
   q_done=q_done+1;
  end
 end
end
task ar(input integer address,input integer count,input integer id);
 begin
  @(negedge clk);s_araddr=address;s_arlen=count-1;s_arsize=4;s_arburst=1;s_arid=id;s_arvalid=1;
  @(posedge clk);while(!s_arready) @(posedge clk);
  @(negedge clk);s_arvalid=0;
 end
endtask
task send_d(input integer source,input integer size,input integer address,input integer count);
 begin
  for(integer k=0;k<count;k=k+1) begin
   @(negedge clk);manual_valid=1;manual_source=source;manual_size=size;manual_data=word_at(address+4*k);
   @(posedge clk);while(!t_d_ready) @(posedge clk);
   @(negedge clk);manual_valid=0;
   if(k%3==0) repeat(3) @(negedge clk);
  end
 end
endtask
initial begin
 repeat(160) @(negedge clk);reset_n=1;
 repeat(40) @(negedge clk);
 ar(4100,1,4);ar(8192,2,5);
 @(negedge clk);s_awaddr=32768;s_awlen=0;s_awsize=4;s_awburst=1;s_awid=7;s_awvalid=1;
 @(posedge clk);while(!s_awready) @(posedge clk);
 @(negedge clk);s_awvalid=0;s_wvalid=1;s_wlast=1;s_wstrb='1;
 for(integer b=0;b<16;b=b+1) s_wdata[b*8+:8]=pattern(32768+b);
 @(posedge clk);while(!s_wready) @(posedge clk);
 @(negedge clk);s_wvalid=0;
 wait(p1_source>=0 && q_source>=0 && ack_seen==1);
 send_d(p1_source,2,4100,1);
 wait(p2_source>=0);
 check(p1_source==p2_source,"SOURCE_HELD_ACROSS_CUT");
 send_d(q_source,5,8192,8);
 wait(q_done==2);
 check(p_done==0,"Q_DRAINED_WITH_P2_HELD");
 repeat(24) @(negedge clk);
 send_d(p2_source,3,4104,2);
 wait(p_done==1 && b_done==1);
 repeat(40) @(negedge clk);
 $display("UH_LARGE_PASS full_network_cut q_words=8 early_ack=%0d",ack_seen);$finish;
end
initial begin #400000;nlv_fail_report("CUT_PROGRESS","timeout",0);end
