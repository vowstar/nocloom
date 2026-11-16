    begin : fairness_wrap
      integer waits, start_cycle;
      localparam COMPLETION_BOUND=9;
      if(MESSAGE_SLOTS!=3) $fatal(1,"ASSERT_FAIL:UH.WRAP.CAPACITY");
      s_d_ready=0;
      // Fill slots 0, 1 and 2 before any response retires.
      for(integer s=0;s<3;s=s+1) begin
        expect_d(2,2,s,1,0); send_a(5,2,s,0,15,0);
      end
      waits=0;
      while(!s_d_valid && waits<60) begin @(negedge clk); waits=waits+1; end
      if(!s_d_valid || s_d_source!=0) $fatal(1,"ASSERT_FAIL:UH.WRAP.FIRST");
      s_d_ready=1;
      @(negedge clk); s_d_ready=0;
      if(d_count[0]!=1) $fatal(1,"ASSERT_FAIL:UH.WRAP.RETIRE_ZERO");
      // The Get takes free slot 0 while slots 1 and 2 still hold their Hints.
      expect_d(1,2,9,1,0); send_a(4,2,9,256,15,0);
      waits=0;
      while((!s_d_valid || s_d_source!=1) && waits<60) begin
        @(negedge clk); waits=waits+1;
      end
      if(!s_d_valid || s_d_source!=1) $fatal(1,"ASSERT_FAIL:UH.WRAP.SECOND");
      s_d_ready=1;
      @(negedge clk); s_d_ready=0;
      if(d_count[1]!=1) $fatal(1,"ASSERT_FAIL:UH.WRAP.RETIRE_ONE");
      repeat(60) @(negedge clk);
      if(!s_d_valid || s_d_source!=2 || ar_count!=1 || r_count!=1 || d_count[9]!=0)
        $fatal(1,"ASSERT_FAIL:UH.WRAP.SETUP");
      start_cycle=cycle; s_d_ready=1;
      repeat(COMPLETION_BOUND) @(negedge clk);
      if(d_count[2]!=1 || d_count[9]!=1)
        $fatal(1,"ASSERT_FAIL:UH.WRAP.BOUND bound=%0d",COMPLETION_BOUND);
      $display("UH_WRAP slots=3 previous=2 candidate=0 latency=%0d bound=%0d",
               d_cycle[9]-start_cycle,COMPLETION_BOUND);
      drain;
    end
