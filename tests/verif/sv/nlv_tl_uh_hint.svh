    begin : hint_competition
      s_d_ready=1;
      expect_d(1,6,1,16,0); send_a(4,6,1,1024,15,0);
      wait(d_count[1]==1); @(negedge clk); s_d_ready=0;
      expect_d(2,2,2,1,0);
      s_a_opcode=5; s_a_param=0; s_a_size=2; s_a_source=2;
      s_a_address=0; s_a_mask=15; s_a_data=0; s_a_valid=1;
      repeat(20) begin
        @(posedge clk);
        if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.HINT.D_OWNER_ADMISSION");
      end
      @(negedge clk); s_d_ready=1;
      @(posedge clk); while(!s_a_ready) @(posedge clk);
      @(negedge clk); s_a_valid=0;
      drain;
      if(ar_count!=1 || aw_count!=0 || d_count[1]!=16 || d_count[2]!=1 ||
          d_cycle[2]<=d_cycle[1]) $fatal(1,"ASSERT_FAIL:UH.HINT.COMPETITION");
      $display("UH_HINT competing=1 network_reads=1");
    end
