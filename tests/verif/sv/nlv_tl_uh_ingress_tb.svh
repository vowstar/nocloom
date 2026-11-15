  always #5 clk=~clk;
  always #8.5 clk_far=~clk_far;
  // The generator supplies BUS_BITS and SIZE_BITS. Some simulators evaluate a
  // width query of a net to zero inside a parameter value.
  localparam BUS_BYTES=BUS_BITS/8;
  reg [BUS_BITS-1:0] bus_word;
  reg hold_w=0, defer_drain=0;
  integer actual_reads[0:65535], expected_reads[0:65535];
  integer rejecting_a_left=0, reject_source=0, retractions=0;

  integer cycle=0, aw_count=0, w_count=0, ar_count=0, r_count=0, b_count=0;
  integer b_write=0, b_read=0, write_beat=0, read_beat=0;
  integer write_len=0, read_len=0, write_size=0, read_size=0;
  integer write_id=0, read_id=0, write_address=0, read_address=0;
  integer pending=0, completions=0, d_beats=0, stall_count=0;
  integer max_issued=0, fail_b=-1, fail_r=-1;
  reg writing=0, reading=0, hold_b=0, hold_r=0, hold_d=0;
  reg [7:0] memory[0:65535], expected[0:65535];
  integer b_ids[0:4095], b_errors[0:4095];
  integer exp_opcode[0:65535], exp_size[0:65535], exp_left[0:65535];
  integer exp_total[0:65535], exp_address[0:65535], exp_denied[0:65535];
  reg exp_busy[0:65535];
  integer byte_address, beat_address, lane, src, k, n, size, param, op, before_aw, before_ar;
  reg [31:0] word_value, active_bits;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $fatal(1,"ASSERT_FAIL:%s %s %0d",rule,name,detail);
  endtask
  nlv_tl_checker #(.NAME("uh"), .SOURCE_W(SOURCE_BITS>0 ? SOURCE_BITS:1), .SIZE_W(SIZE_BITS), .UH(1)) check_tl (
    .clk(clk), .rst_n(reset_n), .allow_reject(1'b1),
    .a_opcode(s_a_opcode), .a_param(s_a_param), .a_size(s_a_size), .a_source(s_a_source),
    .a_address(s_a_address), .a_mask(s_a_mask), .a_data(s_a_data), .a_corrupt(s_a_corrupt),
    .a_valid(s_a_valid), .a_ready(s_a_ready),
    .d_opcode(s_d_opcode), .d_param(s_d_param), .d_size(s_d_size), .d_source(s_d_source),
    .d_data(s_d_data), .d_corrupt(s_d_corrupt), .d_denied(s_d_denied),
    .d_valid(s_d_valid), .d_ready(s_d_ready));
  always @(posedge clk) begin
    cycle=cycle+1;
    if(cycle>2000000) $fatal(1,"ASSERT_FAIL:UH.BOUNDED_PROGRESS");
    if(reset_n) begin
      s_d_ready <= !hold_d && cycle%4!=0;
      if(s_a_valid && !s_a_ready) stall_count=stall_count+1;
      if(s_a_valid && s_a_ready && rejecting_a_left>0) rejecting_a_left=rejecting_a_left-1;
      if(s_d_valid && s_d_source==reject_source && rejecting_a_left>0) $fatal(1,"ASSERT_FAIL:UH.LOCAL.EARLY_D");
      if(s_d_valid && s_d_ready) begin
        src=s_d_source;
        if(s_d_sink!=0) $fatal(1,"ASSERT_FAIL:UH.SINK.ZERO");
        if(!exp_busy[src] || s_d_opcode!=exp_opcode[src] || s_d_size!=exp_size[src] ||
            s_d_param!=0 || s_d_denied!=exp_denied[src])
          $fatal(1,"ASSERT_FAIL:UH.D.IDENTITY source=%0d opcode=%0d size=%0d denied=%0d",src,s_d_opcode,s_d_size,s_d_denied);
        beat_address=(exp_address[src]+4*(exp_total[src]-exp_left[src])) & ~3;
        if(s_d_opcode==1) begin
          if(s_d_corrupt!=(exp_denied[src] || ((beat_address & ~(BUS_BYTES-1))==fail_r || (MISS_LAST && beat_address>=7168 && beat_address<8192))))
            $fatal(1,"ASSERT_FAIL:UH.D.CORRUPT");
          word_value=0;
          if(!exp_denied[src] && !((beat_address & ~(BUS_BYTES-1))==fail_r || (MISS_LAST && beat_address>=7168 && beat_address<8192)))
            for(lane=0;lane<4;lane=lane+1) word_value[lane*8+:8]=expected[beat_address+lane];
          active_bits=32'hffffffff;
          if(exp_size[src]<2 && !exp_denied[src])
            active_bits=((32'h1<<(8*(1<<exp_size[src])))-1) << (8*(exp_address[src]%4));
          if((s_d_data & active_bits)!=(word_value & active_bits))
            $fatal(1,"ASSERT_FAIL:UH.D.DATA source=%0d address=%h got=%h expected=%h",src,beat_address,s_d_data,word_value);
        end else if(s_d_corrupt) $fatal(1,"ASSERT_FAIL:UH.ACK.CORRUPT");
        exp_left[src]=exp_left[src]-1; d_beats=d_beats+1;
        if(exp_left[src]==0) begin exp_busy[src]=0; pending=pending-1; completions=completions+1; end
      end
    end
  end
  always @(posedge target_clk) begin
    if(!reset_n) begin
      t_awready<=0; t_wready<=0; t_arready<=0; t_bvalid<=0; t_rvalid<=0;
      writing=0; reading=0; b_read=b_write;
    end else begin
      t_awready <= !writing && cycle%7!=0;
      t_wready <= writing && !hold_w && cycle%5!=0;
      t_arready <= !reading && cycle%7!=0;
      if(t_awvalid && t_awready) begin
        if(writing || t_awburst!=1 || t_awsize>$clog2(BUS_BYTES) || t_awlen>255)
          $fatal(1,"ASSERT_FAIL:UH.AXI.AW");
        t_awready<=0; writing=1; write_address=t_awaddr; write_len=t_awlen;
        write_size=t_awsize; write_id=t_awid; write_beat=0; aw_count=aw_count+1;
        if(aw_count-b_count>max_issued) max_issued=aw_count-b_count;
      end
      if(t_wvalid && t_wready) begin
        if(!writing || t_wlast!=(write_beat==write_len)) $fatal(1,"ASSERT_FAIL:UH.AXI.WLAST writing=%0d beat=%0d len=%0d last=%0d",writing,write_beat,write_len,t_wlast);
        beat_address=(write_address+(write_beat<<write_size)) & ~(BUS_BYTES-1);
        for(lane=0;lane<BUS_BYTES;lane=lane+1)
          if(t_wstrb[lane]) memory[beat_address+lane]=t_wdata[lane*8+:8];
        w_count=w_count+1;
        if(write_beat==write_len) begin
          t_wready<=0; writing=0; b_ids[b_write]=write_id; b_errors[b_write]=(aw_count-1==fail_b ? 2:0);
          b_write=b_write+1;
        end
        write_beat=write_beat+1;
      end
      if(t_bvalid && t_bready) begin t_bvalid<=0; b_count=b_count+1; end
      if(!t_bvalid && b_read<b_write && !hold_b) begin
        t_bvalid<=1; t_bid<=b_ids[b_read]; t_bresp<=b_errors[b_read]; b_read=b_read+1;
      end
      if(t_arvalid && t_arready) begin
        if(reading || t_arburst!=1 || t_arsize>$clog2(BUS_BYTES) || t_arlen>255)
          $fatal(1,"ASSERT_FAIL:UH.AXI.AR");
        t_arready<=0; reading=1; read_address=t_araddr; read_len=t_arlen;
        read_size=t_arsize; read_id=t_arid; read_beat=0; ar_count=ar_count+1;
      end
      if(t_rvalid && t_rready) begin
        for(integer b=0;b<(1<<read_size);b=b+1)
          actual_reads[read_address+(read_beat<<read_size)+b]=
            actual_reads[read_address+(read_beat<<read_size)+b]+1;
        t_rvalid<=0; r_count=r_count+1;
        if(read_beat==read_len) reading=0;
        read_beat=read_beat+1;
      end
      if(reading && !t_rvalid && !hold_r) begin
        beat_address=(read_address+(read_beat<<read_size)) & ~(BUS_BYTES-1);
        bus_word=0;
        for(lane=0;lane<BUS_BYTES;lane=lane+1) bus_word[lane*8+:8]=memory[beat_address+lane];
        t_rvalid<=1; t_rid<=read_id; t_rdata<=bus_word;
        t_rresp<=((beat_address & ~(BUS_BYTES-1))==fail_r || (MISS_LAST && beat_address>=7168 && beat_address<8192)) ? 2:0; t_rlast<=read_beat==read_len;
      end
    end
  end
  task expect_message(input integer opcode, sz, source, address, denied);
    source=(source & ((1<<SOURCE_BITS)-1)) | (SOURCE_BITS==16 ? 32768:0);
    if(opcode==1 && !denied)
      for(integer b=0;b<(1<<sz);b=b+1)
        if(!MISS_LAST || address+b<7168) expected_reads[address+b]=expected_reads[address+b]+1;
    if(exp_busy[source]) $fatal(1,"ASSERT_FAIL:UH.TEST.SOURCE");
    exp_busy[source]=1; exp_opcode[source]=opcode; exp_size[source]=sz;
    exp_address[source]=address; exp_denied[source]=denied;
    exp_left[source]=(opcode==1 && sz>2) ? (1<<(sz-2)):1;
    exp_total[source]=exp_left[source]; pending=pending+1;
  endtask
  task send_a(input integer opcode, prm, sz, source, address, mask, data, corrupt);
    reg retracted;
    begin
      retracted=0;
      @(negedge clk);
      s_a_opcode=opcode; s_a_param=prm; s_a_size=sz; s_a_source=(source & ((1<<SOURCE_BITS)-1)) | (SOURCE_BITS==16 ? 32768:0);
      s_a_address=address; s_a_mask=mask; s_a_data=data; s_a_corrupt=corrupt; s_a_valid=1;
      @(posedge clk);
      while(!s_a_ready) begin
        if(!retracted) begin
          @(negedge clk); s_a_valid=0; s_a_data=~data; s_a_mask=0;
          @(negedge clk); s_a_data=data; s_a_mask=mask; s_a_valid=1;
          retractions=retractions+1; retracted=1;
        end
        @(posedge clk);
      end
      @(negedge clk); s_a_valid=0;
    end
  endtask
  task drain;
    begin
      @(negedge clk);
      while(pending!=0) @(negedge clk);
      repeat(8) @(negedge clk);
    end
  endtask
  task put(input integer opcode, sz, source, address, corrupt_at);
    integer j, lanes, count, mask;
    reg [31:0] data;
    begin
      count=sz>2 ? 1<<(sz-2):1;
      expect_message(0,sz,source,address,(MISS_LAST && address+(1<<sz)>7168) || corrupt_at>=0 || (fail_b>=aw_count && fail_b<aw_count+(count+255)/256));
      for(j=0;j<count;j=j+1) begin
        data=32'h9e370000 ^ (j*32'h10203) ^ sz;
        if(j==corrupt_at) data=data ^ 32'hfedcba98;
        lanes=sz>=2 ? 15:((1<<(1<<sz))-1) << (address%4);
        mask=opcode==0 ? lanes:((j*7+sz)&15)&lanes;
        send_a(opcode,0,sz,source,address,mask,data,j==corrupt_at);
        if(j!=corrupt_at)
          for(integer b=0;b<4;b=b+1)
            if(mask & (1<<b)) expected[(address & ~3)+j*4+b]=data[b*8+:8];
      end
      if(!defer_drain) drain;
    end
  endtask
  task get(input integer sz, source, address);
    begin
      expect_message(1,sz,source,address,0);
      send_a(4,0,sz,source,address,sz>=2 ? 15:((1<<(1<<sz))-1) << (address%4),0,0);
      drain;
    end
  endtask
  initial begin
    if($bits(t_wdata)!=BUS_BITS || $bits(t_rdata)!=BUS_BITS || $bits(s_a_size)!=SIZE_BITS ||
        $bits(s_a_source)!=(SOURCE_BITS>0 ? SOURCE_BITS:1))
      $fatal(1,"ASSERT_FAIL:UH.TEST.WIDTHS");
    for(integer j=0;j<65536;j=j+1) begin memory[j]=0; expected[j]=0; exp_busy[j]=0; actual_reads[j]=0; expected_reads[j]=0; end
    repeat(110) @(negedge clk);
    reset_n=1;
    repeat(24) @(negedge clk);
    for(size=0;size<=MAX_SIZE;size=size+1) begin
      put(0,size,1,4096,-1); get(size,2,4096);
      put(1,size,1,4096,-1); get(size,2,4096);
      put(1,size,1,4096,(size>2 ? 1<<(size-3):0)); get(size,2,4096);
    end
    if(MAX_SIZE==12 && BUS_BYTES==4 && WINDOWS==0 && TARGET_AXI) begin
      before_aw=aw_count; before_ar=w_count;
      hold_b=1; max_issued=0;
      fork
        begin put(0,12,1,4096,-1); end
        begin
          wait(w_count==before_ar+1024);
          repeat(8) @(negedge clk);
          if(aw_count!=before_aw+4 || max_issued!=4)
            $fatal(1,"ASSERT_FAIL:UH.FOUR_CHILDREN");
          hold_b=0;
        end
      join
      fail_b=aw_count; put(0,12,1,4096,-1);
      fail_b=aw_count+3; put(0,12,1,4096,-1);
      fail_b=-1; get(12,2,4096);
    end
    for(size=0;size<2;size=size+1)
      for(k=0;k<4;k=k+(1<<size)) begin
        put(0,size,1,4096+k,-1); get(size,2,4096+k);
      end
    before_aw=aw_count; before_ar=ar_count;
    for(size=0;size<=MAX_SIZE;size=size+1)
      for(param=0;param<8;param=param+1) begin
        expect_message(2,size,3,0,param>1);
        send_a(5,param,size,3,0,size>=2 ? 15:(1<<(1<<size))-1,0,0); drain;
      end
    for(op=2;op<=3;op=op+1)
      for(size=0;size<=(SIZE_BITS==2 ? 3:6);size=size+1)
        for(param=0;param<8;param=param+1) begin
          expect_message(1,size,3,0,1);
          n=size>2 ? 1<<(size-2):1;
          rejecting_a_left=n; reject_source=(3 & ((1<<SOURCE_BITS)-1)) | (SOURCE_BITS==16 ? 32768:0);
          for(k=0;k<n;k=k+1) send_a(op,param,size,3,0,size>=2 ? 15:(1<<(1<<size))-1,k,0);
          drain;
        end
    if(aw_count!=before_aw || ar_count!=before_ar) $fatal(1,"ASSERT_FAIL:UH.LOCAL.FOOTPRINT");
    get(2,2,4096);
    if(MAX_SIZE>2) begin : reuse_source
      integer early_source, wait_cycles;
      early_source=(2 & ((1<<SOURCE_BITS)-1)) | (SOURCE_BITS==16 ? 32768:0);
      expect_message(1,MAX_SIZE,2,4096,0);
      send_a(4,0,MAX_SIZE,2,4096,15,0,0);
      while(exp_left[early_source]==exp_total[early_source]) @(negedge clk);
      @(negedge clk);
      s_a_opcode=5; s_a_param=0; s_a_size=2; s_a_source=early_source;
      s_a_address=0; s_a_mask=15; s_a_data=0; s_a_corrupt=0; s_a_valid=1;
      wait_cycles=0;
      @(posedge clk);
      while(!s_a_ready && wait_cycles<20000) begin @(posedge clk); wait_cycles=wait_cycles+1; end
      if(!s_a_ready || exp_busy[early_source]) $fatal(1,"ASSERT_FAIL:UH.SOURCE.REUSE");
      expect_message(2,2,2,0,0);
      @(negedge clk); s_a_valid=0;
      drain;
      $display("UH_SOURCE_REUSE bits=%0d waited=%0d",SOURCE_BITS,wait_cycles);
    end
    if(SOURCE_BITS>=3) begin
      hold_d=1;
      for(k=0;k<MESSAGE_SLOTS;k=k+1) begin
        expect_message(2,2,k,0,0); send_a(5,0,2,k,0,15,0,0);
      end
      expect_message(2,2,MESSAGE_SLOTS,0,0);
      fork
        begin send_a(5,0,2,MESSAGE_SLOTS,0,15,0,0); end
        begin repeat(30) @(negedge clk); hold_d=0; end
      join
      drain;
    end
    if(stall_count==0) $fatal(1,"ASSERT_FAIL:UH.TEST.NO_BACKPRESSURE");
    for(integer b=0;b<65536;b=b+1)
      if(actual_reads[b]!=expected_reads[b])
        $fatal(1,"ASSERT_FAIL:UH.READ.FOOTPRINT address=%h actual=%0d expected=%0d",b,actual_reads[b],expected_reads[b]);
    $display("UH_PASS completions=%0d A_stalls=%0d AW=%0d AR=%0d D_beats=%0d",completions,stall_count,aw_count,ar_count,d_beats);
    $finish;
  end
