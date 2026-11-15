    begin : admission
      integer deadline, before_count, taken;
      for(integer op=0;op<2;op=op+1) begin
        s_d_ready=0;
        expect_d(op==0 ? 0:2,3,1,1,0);
        send_a(op==0 ? 0:5,3,1,1280,15,1);
        if(op==0) send_a(0,3,1,1280,15,2);
        deadline=0;
        while(!s_d_valid && deadline<128) begin @(negedge clk); deadline=deadline+1; end
        if(!s_d_valid || s_d_source!=1) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.SETUP");
        expect_d(2,2,2,1,0);
        s_a_opcode=5; s_a_param=0; s_a_size=2; s_a_source=2;
        s_a_address=0; s_a_mask=15; s_a_data=0; s_a_valid=1; taken=0;
        repeat(20) begin
          @(posedge clk);
          if(s_a_valid && s_a_ready) taken=taken+1;
          @(negedge clk);
          if(taken) s_a_valid=0;
          if(!s_d_valid || s_d_source!=1 || s_d_opcode!=(op==0 ? 0:2))
            $fatal(1,"ASSERT_FAIL:UH.ADMISSION.HELD_ACK");
        end
        if(taken!=1) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.SINGLE_ACK op=%0d",op);
        s_d_ready=1; drain;
      end
      // A network read and a local denied atomic both have two D beats.
      for(integer local_read=0;local_read<2;local_read=local_read+1) begin
        s_d_ready=0; before_count=d_count[3];
        expect_d(1,3,3,2,local_read);
        send_a(local_read ? 2:4,3,3,1536,15,0);
        if(local_read) send_a(2,3,3,1536,15,0);
        deadline=0;
        while(!s_d_valid && deadline<128) begin @(negedge clk); deadline=deadline+1; end
        if(!s_d_valid || s_d_source!=3) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.MULTI_SETUP");
        s_a_opcode=5; s_a_param=0; s_a_size=2; s_a_source=4;
        s_a_address=0; s_a_mask=15; s_a_data=0; s_a_valid=1;
        repeat(20) begin
          @(posedge clk);
          if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.MULTI_FIRST");
        end
        @(negedge clk); s_a_valid=0; s_d_ready=1;
        @(posedge clk);
        @(negedge clk); s_d_ready=0;
        if(d_count[3]!=before_count+1) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.FIRST_D");
        // Reusing this source after its first D is legal. It still waits
        // for the old slot to retire at the last D handshake.
        s_a_source=3; s_a_valid=1;
        repeat(20) begin
          @(posedge clk);
          if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.SOURCE_REUSE");
        end
        // Even with only one D beat left, a different source stays blocked.
        @(negedge clk); s_a_source=4;
        repeat(20) begin
          @(posedge clk);
          if(s_a_ready) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.MULTI_LAST");
        end
        @(negedge clk); s_a_source=3; s_d_ready=1; taken=0;
        deadline=0;
        while(!taken && deadline<128) begin
          @(posedge clk);
          if(s_a_ready) taken=1;
          @(negedge clk); deadline=deadline+1;
        end
        if(!taken || busy[3]) $fatal(1,"ASSERT_FAIL:UH.ADMISSION.RETIRE");
        expect_d(2,2,3,1,0); s_a_valid=0;
        drain;
      end
      $display("UH_ADMISSION single_ack=2 multibeat=2");
    end
