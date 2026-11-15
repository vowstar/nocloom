    begin : fairness
      integer start_cycle, accepted, candidate, fast_op, completed_before;
      reg took;
      // Eight slots and single-beat D messages give at most eight owners,
      // each with one acquire, load and handshake cycle: 24 cycles.
      localparam COMPLETION_BOUND=24;
      for(integer pass=0;pass<2;pass=pass+1) begin
        fast_op=pass==0 ? 5:0;
        s_d_ready=0;
        for(integer s=0;s<4;s=s+1) begin
          expect_d(2,2,s,1,0); send_a(5,2,s,0,15,0);
        end
        completed_before=d_count[9];
        expect_d(1,2,9,1,0); send_a(4,2,9,256,15,0);
        repeat(60) @(negedge clk);
        if(ar_count!=pass+1 || r_count!=pass+1 || !s_d_valid || s_d_source!=0)
          $fatal(1,"ASSERT_FAIL:UH.FAIR.SETUP");
        start_cycle=cycle; s_d_ready=1; accepted=0;
        candidate=4;
        expect_d(fast_op==5 ? 2:0,2,candidate,1,0);
        s_a_opcode=fast_op; s_a_param=0; s_a_size=2; s_a_source=candidate;
        s_a_address=1024; s_a_mask=15; s_a_data=32'h12345678; s_a_valid=1;
        repeat(256) begin
          @(posedge clk); took=s_a_valid && s_a_ready;
          @(negedge clk);
          if(cycle-start_cycle==COMPLETION_BOUND && d_count[9]!=completed_before+1)
            $fatal(1,"ASSERT_FAIL:UH.FAIR.BOUND op=%0d bound=%0d",fast_op,COMPLETION_BOUND);
          if(took) begin
            accepted=accepted+1; candidate=-1;
            for(integer s=15;s>=0;s=s-1) if(!busy[s] && s!=9) candidate=s;
            if(candidate<0) $fatal(1,"ASSERT_FAIL:UH.FAIR.FREE_SOURCE");
            expect_d(fast_op==5 ? 2:0,2,candidate,1,0);
            s_a_source=candidate;
          end
        end
        // The final offer has not handshaken and can be withdrawn.
        busy[candidate]=0; s_a_valid=0;
        if(accepted<32) $fatal(1,"ASSERT_FAIL:UH.FAIR.LOAD");
        $display("UH_FAIR op=%0d latency=%0d bound=%0d accepted=%0d",
                 fast_op,d_cycle[9]-start_cycle,COMPLETION_BOUND,accepted);
        drain;
      end
    end
