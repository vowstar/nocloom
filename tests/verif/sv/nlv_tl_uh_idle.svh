    begin : idle_checks
      integer checks, before_b;
      s_d_ready=0;
      repeat(20) @(negedge clk);
      if(!probe_idle || probe_enable) $fatal(1,"ASSERT_FAIL:UH.IDLE.SLEEP");
      expect_d(0,4,1,1,0); send_a(0,4,1,256,15,32'h11111111);
      repeat(20) begin
        @(negedge clk);
        if(!probe_collecting || probe_idle) $fatal(1,"ASSERT_FAIL:UH.IDLE.A_TAIL");
      end
      for(checks=0;checks<3;checks=checks+1) send_a(0,4,1,256,15,32'h11111111);
      wait(b_count==1); @(negedge clk);
      if(!busy[1] || probe_idle) $fatal(1,"ASSERT_FAIL:UH.IDLE.B_CAPTURED");
      wait(s_d_valid); @(negedge clk);
      repeat(20) begin
        @(negedge clk);
        if(!s_d_valid || probe_idle) $fatal(1,"ASSERT_FAIL:UH.IDLE.ACK_HELD");
      end
      s_d_ready=1; drain;
      repeat(20) @(negedge clk);
      if(!probe_idle || probe_enable) $fatal(1,"ASSERT_FAIL:UH.IDLE.DRAIN_WRITE");
      expect_d(1,4,2,4,0); send_a(4,4,2,256,15,0);
      wait(d_count[2]==1); @(negedge clk); s_d_ready=0;
      repeat(20) begin
        @(negedge clk);
        if(probe_idle || !busy[2]) $fatal(1,"ASSERT_FAIL:UH.IDLE.D_TAIL");
      end
      s_d_ready=1; drain;
      repeat(20) @(negedge clk);
      if(!probe_idle || probe_enable) $fatal(1,"ASSERT_FAIL:UH.IDLE.DRAIN_READ");
      expect_d(2,2,3,1,0); send_a(5,2,3,0,15,0); drain;
      repeat(20) @(negedge clk);
      if(!probe_idle || probe_enable) $fatal(1,"ASSERT_FAIL:UH.IDLE.REWAKE");
      $display("UH_IDLE partial_a=1 held_d=1 captured_b=1 wake=3");
    end
