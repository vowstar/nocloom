  always #5 clk=~clk;
  always #8.5 clk_far=~clk_far;
  integer cycle=0, aw_count=0, ar_count=0, w_count=0, r_count=0;
  integer b_write=0, b_read=0, write_address=0, write_id=0;
  integer write_len=0, write_size=0, write_beat=0;
  integer r_write=0, r_read=0, read_beat=0;
  integer b_ids[0:2047], r_ids[0:63], r_addresses[0:63], r_lengths[0:63];
  integer d_count[0:15], d_cycle[0:15], left[0:15];
  integer opcode[0:15], size[0:15], denied[0:15];
  reg busy[0:15];
  reg [7:0] memory[0:4095];
  reg writing=0, hold_r=0;
  reg [3:0] seen_strobe[0:2047];
  reg [31:0] read_word;
  integer address, lane;
  integer active_d=-1;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $fatal(1,"ASSERT_FAIL:%s %s %0d",rule,name,detail);
  endtask
  always @(posedge clk) begin
    cycle=cycle+1;
    if(cycle>10000) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.TIMEOUT");
    if(reset_n && s_d_valid && s_d_ready) begin
      if(active_d>=0 && s_d_source!=active_d)
        $fatal(1,"ASSERT_FAIL:UH.REGRESSION.D_OWNER");
      if(!busy[s_d_source] || s_d_opcode!=opcode[s_d_source] ||
          s_d_size!=size[s_d_source] || s_d_denied!=denied[s_d_source] ||
          s_d_param!=0 || s_d_corrupt!=(denied[s_d_source] && s_d_opcode==1))
        $fatal(1,"ASSERT_FAIL:UH.REGRESSION.D_IDENTITY source=%0d",s_d_source);
      if(s_d_opcode==1 && s_d_data!=(s_d_denied ? 32'b0:32'h11111111))
        $fatal(1,"ASSERT_FAIL:UH.REGRESSION.D_DATA");
      left[s_d_source]=left[s_d_source]-1;
      d_count[s_d_source]=d_count[s_d_source]+1;
      d_cycle[s_d_source]=cycle;
      if(left[s_d_source]==0) begin busy[s_d_source]=0; active_d=-1; end
      else active_d=s_d_source;
    end
  end
  // The target writes exactly the lanes in WSTRB, including malformed masks.
  always @(posedge clk) begin
    if(!reset_n) begin
      t_awready<=0; t_wready<=0; t_arready<=0; t_bvalid<=0; t_rvalid<=0;
    end else begin
      t_awready<=!writing; t_wready<=writing; t_arready<=1;
      if(t_awvalid && t_awready) begin
        if(writing || t_awburst!=1) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.AW");
        writing=1; t_awready<=0;
        write_address=t_awaddr; write_id=t_awid;
        write_size=t_awsize; write_len=t_awlen; write_beat=0;
        aw_count=aw_count+1;
      end
      if(t_wvalid && t_wready) begin
        if(!writing || t_wlast!=(write_beat==write_len))
          $fatal(1,"ASSERT_FAIL:UH.REGRESSION.WLAST");
        address=(write_address+(write_beat<<write_size)) & ~3;
        seen_strobe[w_count]=t_wstrb; w_count=w_count+1;
        for(lane=0;lane<4;lane=lane+1)
          if(t_wstrb[lane]) memory[address+lane]=t_wdata[lane*8+:8];
        write_beat=write_beat+1;
        if(t_wlast) begin
          writing=0; t_wready<=0;
          b_ids[b_write]=write_id; b_write=b_write+1;
        end
      end
      if(t_bvalid && t_bready) t_bvalid<=0;
      if(!t_bvalid && b_read<b_write) begin
        t_bvalid<=1; t_bid<=b_ids[b_read]; t_bresp<=0; b_read=b_read+1;
      end
      if(t_arvalid && t_arready) begin
        if(t_arburst!=1 || t_arsize!=2) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.AR");
        r_ids[r_write]=t_arid; r_addresses[r_write]=t_araddr;
        r_lengths[r_write]=t_arlen; r_write=r_write+1; ar_count=ar_count+1;
      end
      if(t_rvalid && t_rready) begin
        t_rvalid<=0; r_count=r_count+1;
        if(read_beat==r_lengths[r_read]) begin r_read=r_read+1; read_beat=0; end
        else read_beat=read_beat+1;
      end
      if(!t_rvalid && r_read<r_write && !hold_r) begin
        address=r_addresses[r_read]+4*read_beat; read_word=0;
        for(lane=0;lane<4;lane=lane+1) read_word[lane*8+:8]=memory[address+lane];
        t_rvalid<=1; t_rid<=r_ids[r_read]; t_rdata<=read_word;
        t_rresp<=0; t_rlast<=read_beat==r_lengths[r_read];
      end
    end
  end
  task expect_d(input integer op, sz, source, beats, error);
    if(busy[source]) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.SOURCE");
    busy[source]=1; opcode[source]=op; size[source]=sz;
    left[source]=beats; denied[source]=error;
  endtask
  task send_a(input integer op, sz, source, addr, mask, data);
    integer wait_cycles;
    begin
      @(negedge clk);
      s_a_opcode=op; s_a_param=0; s_a_size=sz; s_a_source=source;
      s_a_address=addr; s_a_mask=mask; s_a_data=data; s_a_corrupt=0; s_a_valid=1;
      wait_cycles=0;
      @(posedge clk);
      while(!s_a_ready && wait_cycles<512) begin @(posedge clk); wait_cycles=wait_cycles+1; end
      if(!s_a_ready) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.A_TIMEOUT");
      @(negedge clk); s_a_valid=0;
    end
  endtask
  task drain;
    integer remaining, waits;
    begin
      waits=0; remaining=1;
      while(remaining && waits<512) begin
        @(negedge clk); waits=waits+1; remaining=0;
        for(integer s=0;s<16;s=s+1) remaining=remaining+busy[s];
      end
      if(remaining) $fatal(1,"ASSERT_FAIL:UH.REGRESSION.D_TIMEOUT");
    end
  endtask
  initial begin
    for(integer s=0;s<16;s=s+1) begin busy[s]=0; d_count[s]=0; d_cycle[s]=0; end
    for(integer b=0;b<4096;b=b+1) memory[b]=8'h11;
    repeat(110) @(negedge clk); reset_n=1;
    repeat(24) @(negedge clk);
    $display("UH_PASS regression");
    $finish;
  end
