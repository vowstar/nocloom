integer deny_child=-1, next_read_message=0, next_write_message=0;
integer cycle=0, target_cycle=0, messages=0, max_size=0;
integer put_left=0, put_cursor=0, put_size=0, put_source=0;
integer d_left=0, d_cursor=0, d_size=0, d_source=0, d_kind=0;
integer reads[0:65535], expected_reads[0:65535];
reg [7:0] memory[0:65535], expected_memory[0:65535];
integer read_words=0, write_words=0, read_bytes=0;
reg d_fired=0, drain=0;
always #5 clk=~clk;
always #8.5 clk_far=~clk_far;
wire target_clk=CDC ? clk_far : clk;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d target_cycle=%0d",rule_name,who,value,cycle,target_cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"large",0);
endtask
function [7:0] pattern(input integer address);
  pattern=(address*37+(address/16)*11+93)%256;
endfunction
always @(posedge clk) if(reset_n) begin
  cycle=cycle+1; check(cycle<300000,"PROGRESS");
end
always @(posedge target_clk) begin
  if(!reset_n) begin
    d_fired=0; put_left=0; d_left=0;
  end else begin
    target_cycle=target_cycle+1;
    d_fired=t_d_valid && t_d_ready;
    if(d_fired) begin d_left=d_left-1; d_cursor=d_cursor+4; end
    if(t_a_valid && t_a_ready) begin
      if(put_left==0) begin
        check(d_left==0,"ONE_MODEL_MESSAGE");
        messages=messages+1;
        if(t_a_size>max_size) max_size=t_a_size;
        if(t_a_opcode==4) begin
          check(t_a_address==next_read_message,"READ_CHILD_ORDER");next_read_message=next_read_message+(1<<t_a_size);
          d_left=t_a_size<=2 ? 1 : (1<<t_a_size)/4;
          d_cursor=t_a_address & ~3; d_size=t_a_size; d_source=t_a_source; d_kind=1;
          for(integer b=0;b<(1<<t_a_size);b=b+1) begin
            check(t_a_address+b<65536,"READ_RANGE");
            reads[t_a_address+b]=reads[t_a_address+b]+1; read_bytes=read_bytes+1;
          end
        end else begin
          check(t_a_opcode==1,"PACKET_PUT_PARTIAL");
          check(t_a_address==next_write_message,"WRITE_CHILD_ORDER");next_write_message=next_write_message+(1<<t_a_size);
          put_left=t_a_size<=2 ? 1 : (1<<t_a_size)/4;
          put_cursor=t_a_address & ~3; put_size=t_a_size; put_source=t_a_source;
        end
      end
      if(t_a_opcode!=4) begin
        for(integer b=0;b<4;b=b+1) if(t_a_mask[b]) begin
          check(put_cursor+b<65536,"WRITE_RANGE");
          memory[put_cursor+b]=t_a_data[b*8+:8];
        end
        write_words=write_words+1; put_cursor=put_cursor+4; put_left=put_left-1;
        if(put_left==0) begin
          d_left=1; d_kind=0; d_size=put_size; d_source=put_source; d_cursor=0;
        end
      end
    end
  end
end
always @(negedge target_clk) begin
  if(!reset_n) begin t_a_ready=0; t_d_valid=0; end
  else begin
    t_a_ready=target_cycle%7!=1;
    if(d_fired) t_d_valid=0;
    if(!t_d_valid && d_left!=0 && target_cycle%5!=2) begin
      t_d_valid=1; t_d_opcode=d_kind; t_d_size=d_size; t_d_source=d_source;
      t_d_denied=d_kind==0 && ((messages-1)%4)==deny_child; t_d_corrupt=0; t_d_param=0; t_d_data=0;
      if(d_kind==1) for(integer b=0;b<4;b=b+1) t_d_data[b*8+:8]=memory[d_cursor+b];
    end
  end
end

always @(negedge clk) s_d_ready=drain && cycle%5!=1;
task put(input integer address,input integer denied_child);
 integer prior;
 begin
  prior=messages;deny_child=denied_child;next_write_message=address;
  for(integer k=0;k<1024;k=k+1) begin
   @(negedge clk);s_a_valid=1;s_a_opcode=1;s_a_param=0;s_a_size=12;s_a_source=16'h8001;s_a_address=address;s_a_corrupt=0;
   for(integer b=0;b<4;b=b+1) begin
    s_a_data[b*8+:8]=pattern(address+4*k+b);s_a_mask[b]=(k+b)%3!=1;
    if(s_a_mask[b]) expected_memory[address+4*k+b]=pattern(address+4*k+b);
   end
   @(posedge clk);while(!s_a_ready) @(posedge clk);
   @(negedge clk);s_a_valid=0;
   if(k%23==4) repeat(3) @(negedge clk);
  end
  repeat(15) @(negedge clk);drain=1;
  @(posedge clk);while(!s_d_valid || !s_d_ready) @(posedge clk);
  check(s_d_opcode==0 && s_d_source==16'h8001 && s_d_size==12 && s_d_denied==(deny_child>=0) && !s_d_corrupt,"BRIDGE_WRITE_ACK");
  check(messages-prior==4 && max_size==10,"FOUR_WRITE_CHILDREN");
  @(negedge clk);drain=0;deny_child=-1;
  for(integer b=0;b<65536;b=b+1) check(memory[b]==expected_memory[b],"BRIDGE_WRITE_BYTES");
 end
endtask
task get(input integer address);
 integer prior;
 begin
  prior=messages;read_bytes=0;next_read_message=address;
  for(integer b=0;b<65536;b=b+1) reads[b]=0;
  @(negedge clk);s_a_valid=1;s_a_opcode=4;s_a_param=0;s_a_size=12;s_a_source=16'h8001;s_a_address=address;s_a_mask=15;s_a_corrupt=0;
  @(posedge clk);while(!s_a_ready) @(posedge clk);
  @(negedge clk);s_a_valid=0;
  repeat(24) @(negedge clk);drain=1;
  for(integer k=0;k<1024;k=k+1) begin
   @(posedge clk);while(!s_d_valid || !s_d_ready) @(posedge clk);
   check(s_d_opcode==1 && s_d_source==16'h8001 && s_d_size==12 && !s_d_denied && !s_d_corrupt,"BRIDGE_READ_FRAME");
   for(integer b=0;b<4;b=b+1) check(s_d_data[b*8+:8]==expected_memory[address+4*k+b],"BRIDGE_READ_BYTES");
   @(negedge clk);
  end
  drain=0;
  check(messages-prior==4 && max_size==10,"FOUR_READ_CHILDREN");
  for(integer b=0;b<65536;b=b+1) check(reads[b]==(b>=address && b<address+4096),"BRIDGE_EXACT_READ_BYTES");
 end
endtask
initial begin
 for(integer b=0;b<65536;b=b+1) begin memory[b]=8'h5a;expected_memory[b]=8'h5a;end
 repeat(160) @(negedge clk);reset_n=1;repeat(40) @(negedge clk);
 put(4096,-1);get(4096);put(8192,0);put(12288,3);
 repeat(80) @(negedge clk);
 $display("UH_LARGE_PASS bridge source_width=16 original_size=12 children=4");$finish;
end
initial begin #3000000;nlv_fail_report("BRIDGE_PROGRESS","timeout",0);end
