  always #5 clk=~clk;
  integer cycle=0, messages=0, early_acks=0, same_acks=0, max_size=0;
  integer puts=0, gets=0, a_beat=0, d_beat=0, total=0, bytes=0;
  integer msg_addr=0, read_addr=0, read_size=0, read_beats=0;
  integer read_pending=0, put_active=0, ack_pending=0;
  reg [SOURCE_WIDTH-1:0] read_source=0, ack_source=0;
  integer early=0, denied=0, corrupt_at=-1, hold_a=0, hold_d=0;
  integer error_lo=-1, error_hi=-1;
  reg d_fired=0;
  integer actual_reads[0:65535], expected_reads[0:65535];
  reg [7:0] memory[0:65535];
  reg [7:0] expected_memory[0:65535];
  integer read_order[0:65535], write_order[0:65535];
  integer read_events=0, write_events=0;
  reg [31:0] trace_address[0:4095];
  integer trace_size[0:4095];
  task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
    begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
  endtask
  task default_miss;
    integer prior;
    begin
      prior=messages;
      @(negedge clk); s_araddr=ADDRESS_BASE==0 ? 32'h10000 : 0;
      s_arlen=7; s_arsize=2; s_arburst=1; s_arvalid=1;
      @(posedge clk); while(!s_arready) @(posedge clk);
      @(negedge clk); s_arvalid=0; s_rready=1;
      for(integer k=0;k<8;k=k+1) begin
        @(posedge clk); while(!s_rvalid) @(posedge clk);
        check(s_rresp==3 && s_rdata==0 && s_rlast==(k==7),"DEFAULT_MISS");
        @(negedge clk);
      end
      s_rready=0;
      s_awaddr=ADDRESS_BASE==0 ? 32'h10000 : 0; s_awid=7;
      s_awlen=7; s_awsize=2; s_awburst=1; s_awvalid=1;
      @(posedge clk); while(!s_awready) @(posedge clk);
      @(negedge clk); s_awvalid=0;
      for(integer k=0;k<8;k=k+1) begin
        s_wdata=32'hcafef00d; s_wstrb=15; s_wlast=k==7; s_wvalid=1;
        @(posedge clk); while(!s_wready) @(posedge clk);
        @(negedge clk); s_wvalid=0;
      end
      s_bready=1;
      @(posedge clk); while(!s_bvalid) @(posedge clk);
      check(s_bresp==3 && s_bid==7,"DEFAULT_MISS_WRITE");
      @(negedge clk); s_bready=0;
      check(messages==prior,"DEFAULT_MISS_NO_ACCESS");
    end
  endtask
  task check(input ok, input [255:0] label);
    if(!ok) nlv_fail_report(label,"target",0);
  endtask
  integer other_reads=0;
  always @(negedge clk) q_arready=reset_n && !q_rvalid;
  always @(posedge clk) if(reset_n) begin
    if(q_rvalid && q_rready) q_rvalid<=0;
    if(q_arvalid && q_arready) begin
      check(q_arlen==0 && q_arsize==2,"OTHER_REQUEST");
      q_rvalid<=1; q_rdata<=32'hc001d00d; q_rid<=q_arid; q_rlast<=1;
      other_reads=other_reads+1;
    end
  end
  task other_target;
    integer prior;
    begin
      prior=messages;
      @(negedge clk); s_araddr=32'h20000;
      s_arlen=0; s_arsize=2; s_arburst=1; s_arvalid=1;
      @(posedge clk); while(!s_arready) @(posedge clk);
      @(negedge clk); s_arvalid=0;
      repeat(8) @(negedge clk);
      s_rready=1;
      @(posedge clk); while(!s_rvalid) @(posedge clk);
      check(s_rresp==0 && s_rdata==32'hc001d00d && s_rlast,"OTHER_RESPONSE");
      @(negedge clk); s_rready=0;
      check(other_reads==1 && messages==prior,"OTHER_ISOLATION");
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
  nlv_tl_uh_axi_check #(.NAME("source")) checker_s (
    .clk(clk),.rst_n(reset_n),
    .awaddr(s_awaddr),.araddr(s_araddr),.awid(s_awid),.arid(s_arid),.bid(s_bid),.rid(s_rid),
    .awlen(s_awlen),.arlen(s_arlen),.awsize(s_awsize),.arsize(s_arsize),
    .awprot(s_awprot),.arprot(s_arprot),.awburst(s_awburst),.arburst(s_arburst),
    .bresp(s_bresp),.rresp(s_rresp),.awcache(s_awcache),.arcache(s_arcache),
    .awlock(s_awlock),.arlock(s_arlock),.awvalid(s_awvalid),.awready(s_awready),
    .arvalid(s_arvalid),.arready(s_arready),.wdata(s_wdata),.rdata(s_rdata),
    .wstrb(s_wstrb),.wlast(s_wlast),.wvalid(s_wvalid),.wready(s_wready),
    .bvalid(s_bvalid),.bready(s_bready),.rlast(s_rlast),.rvalid(s_rvalid),.rready(s_rready));
  nlv_tl_uh_axi_check #(.NAME("other")) checker_q (
    .clk(clk),.rst_n(reset_n),
    .awaddr(q_awaddr),.araddr(q_araddr),.awid(q_awid),.arid(q_arid),.bid(q_bid),.rid(q_rid),
    .awlen(q_awlen),.arlen(q_arlen),.awsize(q_awsize),.arsize(q_arsize),
    .awprot(q_awprot),.arprot(q_arprot),.awburst(q_awburst),.arburst(q_arburst),
    .bresp(q_bresp),.rresp(q_rresp),.awcache(q_awcache),.arcache(q_arcache),
    .awlock(q_awlock),.arlock(q_arlock),.awvalid(q_awvalid),.awready(q_awready),
    .arvalid(q_arvalid),.arready(q_arready),.wdata(q_wdata),.rdata(q_rdata),
    .wstrb(q_wstrb),.wlast(q_wlast),.wvalid(q_wvalid),.wready(q_wready),
    .bvalid(q_bvalid),.bready(q_bready),.rlast(q_rlast),.rvalid(q_rvalid),.rready(q_rready));
  always @(negedge clk) if(reset_n) begin
    t_a_ready=!hold_a && !ack_pending && cycle%5!=1;
    if(!t_d_valid || d_fired) begin
      t_d_valid=0;
      if(ack_pending) begin
        t_d_valid=1; t_d_opcode=0; t_d_size=read_size; t_d_source=ack_source;
        t_d_data=0; t_d_denied=denied; t_d_corrupt=0;
      end else if(read_pending && !hold_d && cycle%7!=2) begin
        t_d_valid=1; t_d_opcode=1; t_d_size=read_size; t_d_source=read_source;
        t_d_data=0; t_d_denied=denied; t_d_corrupt=denied || d_beat==corrupt_at;
        for(integer b=0;b<4;b=b+1) t_d_data[b*8+:8]=memory[(read_addr & ~3)+b];
      end
    end
    d_fired=0;
  end
  always @(posedge clk) if(reset_n) begin
    cycle=cycle+1;
    check(cycle<100000,"PROGRESS");
    if(same_ack) check(t_d_ready,"EARLY_ACK_READY");
    if(t_d_valid && t_d_ready) begin
      d_fired=1;
      if(t_d_opcode==0) begin
        ack_pending=0;
        if(same_ack) same_acks=same_acks+1;
        if(put_active) early_acks=early_acks+1;
      end else begin
        read_addr=read_addr+4; d_beat=d_beat+1;
        if(d_beat==read_beats) read_pending=0;
      end
    end
    if(t_a_valid && t_a_ready) begin
      if(!put_active) begin
        trace_address[messages]=t_a_address; trace_size[messages]=t_a_size;
        messages=messages+1;
        if(t_a_size>max_size) max_size=t_a_size;
        msg_addr=t_a_address; bytes=1<<t_a_size;
        total=bytes<4 ? 1 : bytes/4; a_beat=0;
        read_size=t_a_size;
        if(t_a_opcode==4) begin
          check(!read_pending,"ONE_GET");
          read_source=t_a_source;
          gets=gets+1; read_addr=t_a_address; read_beats=total;
          read_pending=1; d_beat=0;
          for(integer b=0;b<bytes;b=b+1) begin
            actual_reads[t_a_address+b]=actual_reads[t_a_address+b]+1;
            read_order[read_events]=t_a_address+b; read_events=read_events+1;
          end
        end else begin
          ack_source=t_a_source;
          puts=puts+1; put_active=1;
          if(early==1) ack_pending=1;
        end
      end
      if(t_a_opcode!=4) begin
        for(integer b=0;b<4;b=b+1) begin
          if(t_a_mask[b]) begin
            if(!denied) memory[(msg_addr & ~3)+4*a_beat+b]=t_a_data[b*8+:8];
            write_order[write_events]=(msg_addr & ~3)+4*a_beat+b; write_events=write_events+1;
          end
          else check(t_a_data[b*8+:8]==0,"UNSELECTED_ZERO");
        end
        a_beat=a_beat+1;
        if(a_beat==total) begin put_active=0; if(!early) ack_pending=1; end
      end
    end
  end
  function integer address_of(input integer start, input integer beat, input integer size, input integer count, input integer burst);
    integer a,span;
    begin
      span=count*(1<<size); a=(start & ~((1<<size)-1))+beat*(1<<size);
      address_of=burst==0 ? start : burst==2 ? (start/span)*span+(a%span) : beat==0 ? start : a;
    end
  endfunction
  task concurrent_read_write;
    integer aw_done, w_done, ar_done, b_done, r_done, read_index, write_index, prior;
    begin
      aw_done=0; w_done=0; ar_done=0; b_done=0; r_done=0;
      read_index=read_events; write_index=write_events; prior=messages;
      for(integer b=0;b<65536;b=b+1) expected_reads[b]=actual_reads[b];
      for(integer b=16388;b<16392;b=b+1) expected_reads[b]=expected_reads[b]+1;
      @(negedge clk);
      s_awaddr=ADDRESS_BASE+16384; s_awid=5; s_awlen=0; s_awsize=2; s_awburst=1;
      s_wdata=32'hbeefcafe; s_wstrb=15; s_wlast=1;
      s_araddr=ADDRESS_BASE+16388; s_arid=6; s_arlen=0; s_arsize=2; s_arburst=1;
      s_awvalid=1; s_wvalid=1; s_arvalid=1; s_bready=1; s_rready=1;
      while(!b_done || !r_done) begin
        @(posedge clk);
        if(s_awvalid && s_awready) aw_done=1;
        if(s_wvalid && s_wready) w_done=1;
        if(s_arvalid && s_arready) ar_done=1;
        if(s_bvalid && s_bready) begin
          check(s_bid==5 && s_bresp==0,"CONCURRENT_B"); b_done=1;
        end
        if(s_rvalid && s_rready) begin
          check(s_rid==6 && s_rlast && s_rresp==0 && s_rdata==32'h07060504,"CONCURRENT_R");
          r_done=1;
        end
        @(negedge clk);
        s_awvalid=!aw_done; s_wvalid=!w_done; s_arvalid=!ar_done;
        s_bready=!b_done; s_rready=!r_done;
      end
      check(aw_done && w_done && ar_done && messages==prior+2,"CONCURRENT_COMPLETE");
      check(read_events==read_index+4 && write_events==write_index+4,"CONCURRENT_BYTES");
      for(integer b=0;b<4;b=b+1) begin
        check(read_order[read_index+b]==16388+b && write_order[write_index+b]==16384+b,"CONCURRENT_ORDER");
        expected_memory[16384+b]=32'hbeefcafe >> (b*8);
      end
      for(integer b=0;b<65536;b=b+1) begin
        check(actual_reads[b]==expected_reads[b],"CONCURRENT_FOOTPRINT");
        check(memory[b]==expected_memory[b],"CONCURRENT_MEMORY");
      end
    end
  endtask
  task reset_pending_read;
    begin
      hold_d=1;
      @(negedge clk); s_araddr=ADDRESS_BASE+7168; s_arlen=15;
      s_arsize=2; s_arburst=1; s_arvalid=1;
      @(posedge clk); while(!s_arready) @(posedge clk);
      @(negedge clk); s_arvalid=0;
      while(!read_pending) @(negedge clk);
      repeat(3) @(negedge clk);
      reset_n=0; read_pending=0; put_active=0; ack_pending=0;
      t_d_valid=0; d_fired=0; hold_d=0; a_beat=0; d_beat=0;
      repeat(160) @(negedge clk); reset_n=1;
      repeat(20) @(negedge clk);
      check(!s_rvalid && !s_bvalid && !t_a_valid && !t_timed_out,"RESET_CLEARED");
    end
  endtask
  task transaction(input integer start, input integer count, input integer sz, input integer burst, input integer sparse);
    integer addr, end_byte, write_index, read_index, error_here;
    reg [31:0] expected_word;
    begin
      write_index=write_events; read_index=read_events;
      s_awid=(start+count+sz)%16;
      @(negedge clk); s_awaddr=ADDRESS_BASE+start; s_awlen=count-1; s_awsize=sz; s_awburst=burst; s_awvalid=1;
      @(posedge clk); while(!s_awready) @(posedge clk);
      @(negedge clk); s_awvalid=0;
      for(integer k=0;k<count;k=k+1) begin
        addr=address_of(start,k,sz,count,burst); end_byte=(addr & ~((1<<sz)-1))+(1<<sz);
        s_wvalid=1; s_wlast=k==count-1; s_wdata=0; s_wstrb=0;
        for(integer b=addr;b<end_byte;b=b+1) begin
          s_wdata[(b%4)*8+:8]=(b+k+cycle)%256;
          s_wstrb[b%4]=!sparse || ((b+k)%3!=0);
        end
        @(posedge clk); while(!s_wready) @(posedge clk);
        for(integer b=addr;b<end_byte;b=b+1) if(s_wstrb[b%4]) begin
          if(!denied) expected_memory[b]=s_wdata[(b%4)*8+:8];
        end
        @(negedge clk); s_wvalid=0;
      end
      s_bready=1;
      @(posedge clk); while(!s_bvalid) @(posedge clk);
      check(s_bresp==(denied ? 2 : 0),"BRESP");
      check(s_bid==s_awid,"BID");
      for(integer k=0;k<count;k=k+1) begin
        addr=address_of(start,k,sz,count,burst); end_byte=(addr & ~((1<<sz)-1))+(1<<sz);
        for(integer b=addr;b<end_byte;b=b+1) if(!sparse || ((b+k)%3!=0)) begin
          check(write_order[write_index]==b,"WRITE_ORDER"); write_index=write_index+1;
        end
      end
      check(write_index==write_events,"WRITE_EVENT_COUNT");
      for(integer b=0;b<65536;b=b+1) check(memory[b]==expected_memory[b],"WRITE_BYTES");
      @(negedge clk); s_bready=0;
      for(integer b=0;b<65536;b=b+1) expected_reads[b]=actual_reads[b];
      for(integer k=0;k<count;k=k+1) begin
        addr=address_of(start,k,sz,count,burst); end_byte=(addr & ~((1<<sz)-1))+(1<<sz);
        for(integer b=addr;b<end_byte;b=b+1) expected_reads[b]=expected_reads[b]+1;
      end
      s_arid=(start+count+sz+1)%16;
      s_araddr=ADDRESS_BASE+start; s_arlen=count-1; s_arsize=sz; s_arburst=burst; s_arvalid=1;
      @(posedge clk); while(!s_arready) @(posedge clk);
      @(negedge clk); s_arvalid=0;
      for(integer k=0;k<count;k=k+1) begin
        repeat(k%4) @(negedge clk);
        s_rready=1;
        @(posedge clk); while(!s_rvalid) @(posedge clk);
        addr=address_of(start,k,sz,count,burst); end_byte=(addr & ~((1<<sz)-1))+(1<<sz);
        expected_word=0;
        for(integer b=addr;b<end_byte;b=b+1) expected_word[(b%4)*8+:8]=memory[b];
        error_here=denied || (error_lo>=0 ? addr<error_hi && end_byte>error_lo : k==corrupt_at);
        check(s_rlast==(k==count-1),"RLAST");
        check(s_rid==s_arid,"RID");
        check(s_rresp==(error_here ? 2 : 0),"RRESP");
        for(integer b=addr;b<end_byte;b=b+1) begin
          if(s_rdata[(b%4)*8+:8]!=(error_here ? 0 : expected_word[(b%4)*8+:8]))
            $display("DATA start=%h count=%0d size=%0d burst=%0d k=%0d actual=%h expected=%h",start,count,sz,burst,k,s_rdata,expected_word);
          check(s_rdata[(b%4)*8+:8]==(error_here ? 0 : expected_word[(b%4)*8+:8]),"RDATA");
        end
        @(negedge clk); s_rready=0;
      end
      for(integer b=0;b<65536;b=b+1) check(actual_reads[b]==expected_reads[b],"EXACT_READ_BYTES");
      for(integer k=0;k<count;k=k+1) begin
        addr=address_of(start,k,sz,count,burst); end_byte=(addr & ~((1<<sz)-1))+(1<<sz);
        for(integer b=addr;b<end_byte;b=b+1) begin
          check(read_order[read_index]==b,"READ_ORDER"); read_index=read_index+1;
        end
      end
      check(read_index==read_events,"READ_EVENT_COUNT");
    end
  endtask
  initial begin
    for(integer b=0;b<65536;b=b+1) begin
      memory[b]=b%256; expected_memory[b]=b%256; actual_reads[b]=0; expected_reads[b]=0;
    end
    repeat(160) @(negedge clk); reset_n=1;
    repeat(20) @(negedge clk);
    early=2;
    hold_a=1;
    fork
      begin
        repeat(80) @(negedge clk);
        check(t_timed_out,"WATCHDOG_EXPIRED"); hold_a=0;
      end
      transaction(0,256,2,1,0);
    join
    check(max_size==$clog2(MAX_BYTES),"MAXIMUM_BLOCK");
    check(puts==1024/MAX_BYTES && gets==1024/MAX_BYTES,"MAXIMUM_MESSAGE_COUNT");
    early=1;
    for(integer sz=0;sz<3;sz=sz+1) begin
      for(integer off=0;off<4;off=off+1) begin
        transaction(1024+off,3,sz,1,1);
        transaction(1152+off,7,sz,1,0);
      end
      transaction(1280,17,sz,1,1);
      transaction(8193,1,sz,1,0);
      transaction(8193,2,sz,1,1);
      transaction(8193,5,sz,1,0);
      transaction(8193,15,sz,1,1);
      transaction(8193,16,sz,1,0);
      transaction(8193,255,sz,1,1);
    end
    for(integer sz=0;sz<3;sz=sz+1)
      for(integer n=2;n<=16;n=n*2)
        for(integer start=0;start<n;start=start+1)
          transaction(2048+(start<<sz),n,sz,2,1);
    transaction(3073,4,2,0,1);
    denied=1; transaction(4096,16,2,1,0); denied=0;
    if(MAX_BYTES>=64) begin corrupt_at=3; transaction(4096,16,2,1,0); corrupt_at=-1; end
    if(MAX_BYTES>=16) begin
      corrupt_at=1; error_lo=4356; error_hi=4360;
      transaction(4352,16,0,1,0);
      transaction(4352,8,1,1,0);
      corrupt_at=-1; error_lo=-1; error_hi=-1;
    end
    transaction(4224,3,2,1,0);
    transaction(65532,1,2,1,0);
    transaction(65524,3,2,1,0);
    transaction(65533,1,2,0,0);
    default_miss;
    other_target;
    transaction(4224,1,2,1,0);
    concurrent_read_write;
    if(MAX_BYTES>=8) check(early_acks>0,"EARLY_ACK_REACHED");
    check(same_acks>0,"SAME_CYCLE_ACK_REACHED");
    check(t_timed_out,"WATCHDOG_LATCHED");
    reset_pending_read;
    transaction(7300,3,2,1,0);
    $display("UH_PASS messages=%0d gets=%0d puts=%0d early=%0d",messages,gets,puts,early_acks);
    $finish;
  end
