    begin : b_independence
      integer b_base, deadline;
      b_base=b_count;
      hold_b=1; defer_drain=1;
      put(0,2,1,4096,-1);
      defer_drain=0;
      hold_d=1;
      expect_message(1,2,2,4100,0);
      send_a(4,0,2,2,4100,15,0,0);
      deadline=0;
      while(!s_d_valid && deadline<64) begin @(negedge clk); deadline=deadline+1; end
      if(!s_d_valid || s_d_source!=2 || s_d_opcode!=1 || s_d_ready)
        $fatal(1,"ASSERT_FAIL:UH.OWNER.START");
      hold_b=0;
      deadline=0;
      while(b_count==b_base && deadline<64) begin @(negedge clk); deadline=deadline+1; end
      if(b_count!=b_base+1)
        $fatal(1,"ASSERT_FAIL:UH.OWNER.B b=%0d base=%0d",b_count,b_base);
      if(!s_d_valid || s_d_source!=2 || s_d_opcode!=1 || s_d_ready)
        $fatal(1,"ASSERT_FAIL:UH.D.HELD_OWNER");
      hold_d=0;
      drain;
    end
