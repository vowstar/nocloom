    begin : concurrent
      integer base_ar, base_aw, saved, attempts;
      // Hold W while a second Put is offered, then keep both B replies pending.
      s_d_ready=0; hold_w=1; hold_b=1;
      expect_d(0,2,1,1,0); send_a(0,2,1,256,15,32'h11111111);
      expect_d(0,2,2,1,0);
      s_a_opcode=0; s_a_size=2; s_a_source=2; s_a_address=512;
      s_a_mask=15; s_a_data=32'h22222222; s_a_valid=1;
      repeat(1) begin
        @(posedge clk);
        if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.WRITER_GATE");
      end
      @(negedge clk); hold_w=0;
      @(posedge clk); while(!s_a_ready) @(posedge clk);
      @(negedge clk); s_a_valid=0;
      wait(b_write==2); @(negedge clk);
      if(!busy[1] || !busy[2] || aw_count!=2) $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.PUT_REACH");
      hold_b=0; s_d_ready=1; drain;

      // A pending AR descriptor must not be overwritten by a second Get.
      hold_ar=1; hold_r=1;
      repeat(3) @(negedge clk);
      base_ar=ar_count;
      expect_d(1,2,3,1,0); send_a(4,2,3,256,15,0);
      expect_d(1,2,4,1,0); expected_word[4]=32'h22222222;
      s_a_opcode=4; s_a_size=2; s_a_source=4; s_a_address=512; s_a_valid=1;
      repeat(1) begin
        @(posedge clk);
        if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.AR_PENDING_GATE");
      end
      @(negedge clk); hold_ar=0;
      @(posedge clk); while(!s_a_ready) @(posedge clk);
      @(negedge clk); s_a_valid=0;
      wait(ar_count==base_ar+2); @(negedge clk);
      if(!busy[3] || !busy[4] || r_ids[r_read]==r_ids[r_read+1])
        $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.GET_REACH");
      // Deliver the second AXI ID first, with distinct data at each address.
      saved=r_ids[r_read]; r_ids[r_read]=r_ids[r_read+1]; r_ids[r_read+1]=saved;
      saved=r_addresses[r_read]; r_addresses[r_read]=r_addresses[r_read+1]; r_addresses[r_read+1]=saved;
      saved=r_lengths[r_read]; r_lengths[r_read]=r_lengths[r_read+1]; r_lengths[r_read+1]=saved;
      hold_r=0; drain;
      if(d_cycle[4]>=d_cycle[3]) $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.REORDER_UNREACHED");

      // A long Get owns slot zero. The failing write must complete in another slot.
      hold_r=1; base_ar=ar_count; base_aw=aw_count; fail_b=aw_count;
      expect_d(1,6,5,16,0); send_a(4,6,5,1024,15,0);
      expect_d(0,2,6,1,1); send_a(0,2,6,768,15,32'h11111111);
      wait(b_count==3); @(negedge clk);
      if(r_ids[r_read]!=0 || b_ids[b_write-1]==0 || aw_count!=base_aw+1)
        $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.NONZERO_SLOT");
      // A new read waits until the multibeat read has retired.
      s_a_opcode=4; s_a_size=2; s_a_source=7; s_a_address=256; s_a_valid=1;
      repeat(12) begin
        @(posedge clk);
        if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.LONG_READ_GATE");
      end
      @(negedge clk); s_a_valid=0; hold_r=0;
      drain;
      if(d_count[5]!=16 || d_count[6]!=1 || ar_count!=base_ar+1)
        $fatal(1,"ASSERT_FAIL:UH.CONCURRENT.COMPLETIONS");
      fail_b=-1;
      expect_d(1,2,6,1,0); send_a(4,2,6,768,15,0); drain;
      $display("UH_CONCURRENT puts=2 gets=2 reordered=1 nonzero_error=1");
    end
