    begin : stress
      integer w_base, b_base, a_base, r_base, source2, source3, checks, deadline;
      integer boundaries[0:4];
      source2=2 | (SOURCE_BITS==16 ? 32768:0);
      source3=3 | (SOURCE_BITS==16 ? 32768:0);
      boundaries[0]=254; boundaries[1]=255; boundaries[2]=256;
      boundaries[3]=1022; boundaries[4]=1023;
      if(MAX_SIZE==12 && BUS_BYTES==4) begin
        w_base=w_count;
        fork
          begin put(0,12,1,4096,-1); end
          begin
            for(checks=0;checks<5;checks=checks+1) begin
              wait(w_count>=w_base+boundaries[checks]);
              @(negedge clk); hold_w=1;
              repeat(16) @(negedge clk);
              $display("UH_BOUNDARY W=%0d",w_count-w_base);
              hold_w=0;
            end
          end
        join
        expect_message(1,12,2,4096,0);
        send_a(4,0,12,2,4096,15,0,0);
        for(checks=0;checks<5;checks=checks+1) begin
          wait(1024-exp_left[source2]>=boundaries[checks]);
          @(negedge clk); hold_d=1;
          repeat(16) @(negedge clk);
          $display("UH_BOUNDARY D=%0d",1024-exp_left[source2]);
          hold_d=0;
        end
        drain;
        put(1,12,1,4096,0); put(1,12,1,4096,1023); get(12,2,4096);
      end
      if(SOURCE_BITS>=2 && MESSAGE_SLOTS>=8) begin
        hold_b=1; hold_r=1; defer_drain=1;
        b_base=b_count;
        put(0,12,1,4096,-1);
        defer_drain=0;
        r_base=r_count;
        expect_message(1,12,2,4096,0); send_a(4,0,12,2,4096,15,0,0);
        expect_message(1,6,3,0,1); rejecting_a_left=16; reject_source=source3;
        send_a(2,0,6,3,0,15,0,0);
        hold_r=0;
        fork
          begin : pause_r
            integer wait_r;
            wait_r=0;
            while(r_count<r_base+256 && wait_r<4000) begin @(negedge target_clk); wait_r=wait_r+1; end
            if(r_count<r_base+256) $fatal(1,"ASSERT_FAIL:UH.OWNER.RLAST");
            hold_r=1;
          end
          begin : pause_d
            integer wait_d;
            wait_d=0;
            while(exp_left[source2]>769 && wait_d<4000) begin @(negedge clk); wait_d=wait_d+1; end
            if(exp_left[source2]>769) $fatal(1,"ASSERT_FAIL:UH.OWNER.START");
            hold_d=1; s_d_ready=0;
          end
        join
        deadline=0;
        while(!s_d_valid && deadline<4000) begin @(negedge clk); deadline=deadline+1; end
        if(!s_d_valid || exp_left[source2]!=769) $fatal(1,"ASSERT_FAIL:UH.OWNER.LAST_D_HELD");
        $display("UH_READ_BOUNDARY R=%0d base=%0d D_left=%0d",r_count,r_base,exp_left[source2]);
        for(k=1;k<16;k=k+1) send_a(2,0,6,3,0,15,k,0);
        hold_b=0;
        deadline=0;
        while(b_count<b_base+4 && deadline<4000) begin @(negedge clk); deadline=deadline+1; end
        if(b_count!=b_base+4) $fatal(1,"ASSERT_FAIL:UH.OWNER.B b=%0d base=%0d aw=%0d",b_count,b_base,aw_count);
        repeat(24) @(negedge clk);
        if(!s_d_valid || s_d_source!=source2 || s_d_opcode!=1 || s_d_size!=12)
          $fatal(1,"ASSERT_FAIL:UH.D.HELD_OWNER");
        s_a_opcode=5; s_a_param=0; s_a_size=2; s_a_source=source2;
        s_a_address=0; s_a_mask=15; s_a_data=0; s_a_corrupt=0; s_a_valid=1;
        repeat(24) begin
          @(negedge clk);
          if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.SOURCE.EARLY_RETIRE");
        end
        hold_d=0; hold_r=0;
        deadline=0;
        @(posedge clk);
        while(!s_a_ready && deadline<20000) begin @(posedge clk); deadline=deadline+1; end
        if(!s_a_ready) $fatal(1,"ASSERT_FAIL:UH.OWNER.PROGRESS");
        if(exp_busy[source2]) $fatal(1,"ASSERT_FAIL:UH.SOURCE.LIFETIME");
        expect_message(2,2,2,0,0);
        @(negedge clk); s_a_valid=0;
        drain;
        $display("UH_OWNER B_absorbed=%0d source_wait=%0d",b_count-b_base,deadline);
      end
      for(checks=0;checks<3;checks=checks+1) begin
        fail_r=4096+(checks==0 ? 0:checks==1 ? 512:4096-BUS_BYTES);
        get(12,2,4096);
      end
      fail_r=-1;
      a_base=aw_count; r_base=ar_count;
      for(op=2;op<=3;op=op+1) begin
        expect_message(1,15,3,0,1); rejecting_a_left=8192; reject_source=source3;
        for(k=0;k<8192;k=k+1) send_a(op,7,15,3,0,15,k,0);
        drain;
      end
      expect_message(1,15,3,0,1); send_a(4,0,15,3,0,15,0,0); drain;
      expect_message(0,15,3,0,1);
      for(k=0;k<8192;k=k+1) send_a(1,0,15,3,0,15,k,0);
      drain;
      expect_message(2,15,3,0,1); send_a(5,0,15,3,0,15,0,0); drain;
      for(op=6;op<=7;op=op+1) begin
        expect_message(0,15,3,0,1); send_a(op,0,15,3,0,15,0,0); drain;
      end
      if(aw_count!=a_base || ar_count!=r_base) $fatal(1,"ASSERT_FAIL:UH.REJECT.FOOTPRINT");
      // Common reset cancels a Put whose tail has not arrived.
      send_a(1,0,6,1,8192,15,32'h13572468,0);
      repeat(4) @(negedge clk);
      reset_n=0;
      repeat(110) @(negedge clk);
      reset_n=1;
      repeat(24) @(negedge clk);
      get(2,2,4096);
    end
