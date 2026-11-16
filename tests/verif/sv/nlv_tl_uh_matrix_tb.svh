localparam LANES=DATA_WIDTH/8;
integer cycle=0, target_cycle=0, messages=0, max_size=0;
integer put_left=0, put_cursor=0, put_size=0, put_source=0;
integer d_left=0, d_cursor=0, d_size=0, d_source=0, d_kind=0;
integer reads[0:65535], expected_reads[0:65535];
reg [7:0] memory[0:65535], expected_memory[0:65535];
integer read_words=0, write_words=0, read_bytes=0;
reg d_fired=0, drain=0;
integer corrupt_word=-1, message_word=0;
reg deny_read=0, deny_write=0;
integer response_error, stall_stage=0, ready_pause=0;
integer write_sequence[0:8191], read_sequence[0:8191];
integer expected_write_sequence[0:8191], expected_read_sequence[0:8191];
integer write_events=0, read_events=0, expected_write_events=0, expected_read_events=0;
wire target_gate_open=GATE_PROBE;

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
    if(d_left!=0 || put_left!=0) check(target_gate_open,"GATE_BUSY_RETAINED");
    d_fired=t_d_valid && t_d_ready;
    if(d_fired) begin d_left=d_left-1; d_cursor=d_cursor+4; message_word=message_word+1; end
    if(t_a_valid && t_a_ready) begin
      if(put_left==0) begin
        check(d_left==0,"ONE_MODEL_MESSAGE");
        messages=messages+1;
        if(t_a_size>max_size) max_size=t_a_size;
        if(t_a_opcode==4) begin
          d_left=t_a_size<=2 ? 1 : (1<<t_a_size)/4;
          message_word=0; d_cursor=t_a_address & ~3; d_size=t_a_size; d_source=t_a_source; d_kind=1;
          for(integer b=0;b<(1<<t_a_size);b=b+1) begin
            check(t_a_address+b<65536,"READ_RANGE");
            reads[t_a_address+b]=reads[t_a_address+b]+1; read_bytes=read_bytes+1;
            check(read_events<8192,"READ_EVENT_BOUND");
            read_sequence[read_events]=t_a_address+b;read_events=read_events+1;
          end
        end else begin
          check(t_a_opcode==1 || (t_a_opcode==0 && t_a_size<=2),"PACKET_PUT_PARTIAL");
          put_left=t_a_size<=2 ? 1 : (1<<t_a_size)/4;
          put_cursor=t_a_address & ~3; put_size=t_a_size; put_source=t_a_source;
        end
      end
      if(t_a_opcode!=4) begin
        for(integer b=0;b<4;b=b+1) if(t_a_mask[b]) begin
          check(put_cursor+b<65536,"WRITE_RANGE");
          memory[put_cursor+b]=t_a_data[b*8+:8];
          check(write_events<8192,"WRITE_EVENT_BOUND");
          write_sequence[write_events]=put_cursor+b;write_events=write_events+1;
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
      t_d_denied=d_kind==1 ? deny_read : deny_write; t_d_corrupt=d_kind==1 && (deny_read || message_word==corrupt_word); t_d_param=0; t_d_data=0;
      if(d_kind==1) for(integer b=0;b<4;b=b+1) t_d_data[b*8+:8]=memory[d_cursor+b];
    end
  end
end
always @(negedge clk) begin
  if(ready_pause>0) ready_pause=ready_pause-1;
  if(d_kind==1 && drain && ((stall_stage==0 && message_word>=255) || (stall_stage==1 && message_word>=256) || (stall_stage==2 && message_word>=1022) || (stall_stage==3 && message_word>=1023))) begin
    ready_pause=24;stall_stage=stall_stage+1;
  end
  s_rready=drain && ready_pause==0 && cycle%5!=1;
  s_bready=drain && cycle%7!=3;
end
task write_packet(input integer address, input integer count, input sparse, input miss);
  integer current, base, lane, prior;
  begin
    prior=messages;write_events=0;expected_write_events=0;
    @(negedge clk); s_awaddr=address; s_awlen=count-1; s_awsize=$clog2(LANES);
    s_awburst=1; s_awid=5; s_awvalid=1;
    @(posedge clk); while(!s_awready) @(posedge clk);
    @(negedge clk); s_awvalid=0;
    for(integer k=0;k<count;k=k+1) begin
      current=k==0 ? address : (address & ~(LANES-1))+k*LANES;
      base=current & ~(LANES-1); s_wdata=0; s_wstrb=0;
      for(integer b=0;b<LANES;b=b+1) begin
        s_wdata[b*8+:8]=pattern(base+b);
        s_wstrb[b]=base+b>=current && (!sparse || (base+b)%3!=1);
        if(s_wstrb[b] && !miss) begin
          expected_memory[base+b]=pattern(base+b);
          expected_write_sequence[expected_write_events]=base+b;expected_write_events=expected_write_events+1;
        end
      end
      s_wvalid=1; s_wlast=k==count-1;
      @(posedge clk); while(!s_wready) @(posedge clk);
      @(negedge clk); s_wvalid=0;
      if(k%17==3) repeat(3) @(negedge clk);
    end
    repeat(12) @(negedge clk); drain=1;
    @(posedge clk); while(!s_bvalid || !s_bready) @(posedge clk);
    check(s_bid==5 && s_bresp==(miss ? 3 : deny_write ? 2 : 0),"WRITE_RESPONSE");
    @(negedge clk); drain=0;
    if(miss) check(messages==prior,"MISS_WRITE_NO_TARGET");
    else if(EXACT_BLOCK) check(messages-prior==1 && put_size==$clog2(count*LANES),"ONE_FULL_SIZE_PUT");
    check(write_events==expected_write_events,"WRITE_ACCESS_COUNT");
    for(integer b=0;b<write_events;b=b+1) check(write_sequence[b]==expected_write_sequence[b],"WRITE_ACCESS_ORDER");
    for(integer b=0;b<65536;b=b+1) check(memory[b]==expected_memory[b],"WRITE_BYTE_ORACLE");
  end
endtask
task read_packet(input integer address, input integer count, input miss);
  integer current, base, prior;
  begin
    prior=messages;read_events=0;expected_read_events=0; read_words=0; read_bytes=0; stall_stage=0;ready_pause=0;
    for(integer b=0;b<65536;b=b+1) begin reads[b]=0; expected_reads[b]=0; end
    if(!miss) for(integer k=0;k<count;k=k+1) begin
      current=k==0 ? address : (address & ~(LANES-1))+k*LANES;
      base=current & ~(LANES-1);
      for(integer b=0;b<LANES;b=b+1) if(base+b>=current) begin
        expected_reads[base+b]=expected_reads[base+b]+1;
        expected_read_sequence[expected_read_events]=base+b;expected_read_events=expected_read_events+1;
      end
    end
    @(negedge clk); s_araddr=address; s_arlen=count-1; s_arsize=$clog2(LANES);
    s_arburst=1; s_arid=9; s_arvalid=1;
    @(posedge clk); while(!s_arready) @(posedge clk);
    @(negedge clk); s_arvalid=0;
    repeat(24) @(negedge clk); drain=1;
    for(integer k=0;k<count;k=k+1) begin
      @(posedge clk); while(!s_rvalid || !s_rready) @(posedge clk);
      response_error=miss ? 3 : deny_read ? 2 : 0;
      if(!miss && corrupt_word>=0) begin
        current=k==0 ? address : (address & ~(LANES-1))+k*LANES;
        if(corrupt_word*4>=current-address && corrupt_word*4<(current & ~(LANES-1))+LANES-address) response_error=2;
      end
      check(s_rid==9 && s_rresp==response_error && s_rlast==(k==count-1),"READ_RESPONSE_FRAME");
      current=k==0 ? address : (address & ~(LANES-1))+k*LANES;
      base=current & ~(LANES-1);
      for(integer b=0;b<LANES;b=b+1) if(base+b>=current)
        check(s_rdata[b*8+:8]==(response_error!=0 ? 0 : expected_memory[base+b]),"READ_BYTE_DATA");
      read_words=read_words+1;
      @(negedge clk);
    end
    drain=0;
    if(miss) check(messages==prior,"MISS_READ_NO_TARGET");
    else begin
      if(EXACT_BLOCK) check(messages-prior==1 && d_size==$clog2(count*LANES),"ONE_FULL_SIZE_GET");
      for(integer b=0;b<65536;b=b+1) check(reads[b]==expected_reads[b],"EXACT_READ_BYTE_ORACLE");
    end
    check(read_events==expected_read_events,"READ_ACCESS_COUNT");
    for(integer b=0;b<read_events;b=b+1) check(read_sequence[b]==expected_read_sequence[b],"READ_ACCESS_ORDER");
    $display("LARGE_READ width=%0d address=%0d axi_beats=%0d physical_words=%0d messages=%0d",DATA_WIDTH,address,count,(count*LANES-address%LANES+3)/4,messages-prior);
  end
endtask
initial begin
  for(integer b=0;b<65536;b=b+1) begin memory[b]=8'h5a; expected_memory[b]=8'h5a; end
  repeat(160) @(negedge clk); reset_n=1;
  repeat(40) @(negedge clk);
  SCENARIOS
  repeat(160) @(negedge clk);
  GATE_IDLE
  corrupt_word=-1;deny_read=0;deny_write=0;
  read_packet(4096,1,0);
  repeat(160) @(negedge clk);
  GATE_IDLE
  $display("UH_LARGE_PASS width=%0d capacity=%0d maximum=%0d messages=%0d max_size=%0d",DATA_WIDTH,CAPACITY,MAXIMUM,messages,max_size);
  $finish;
end
initial begin #3000000; nlv_fail_report("PROGRESS","timeout",0); end
