    begin : pool
      integer saturated, waits;
      if(POOL_CAPACITY!=4 || MAX_SIZE!=12) $fatal(1,"ASSERT_FAIL:UH.POOL.SETUP");
      // A 4096-byte Put, a 1024-byte Put and a 4096-byte Get need nine units
      // beyond the capacity of four. B and D stay stalled first, so the first
      // Put keeps its four units and the second Put waits for one of them.
      // The target takes eight bursts, so the pool is the limit.
      saturated=0; s_d_ready=0; hold_b=1;
      fork
        begin
          expect_d(0,12,1,1,0);
          for(integer j=0;j<1024;j=j+1) send_a(0,12,1,4096,15,j);
          expect_d(0,10,3,1,0);
          for(integer j=0;j<256;j=j+1) send_a(0,10,3,8192,15,j);
          expect_d(1,12,0,1024,0); send_a(4,12,0,0,15,0);
        end
        begin
          waits=0;
          while(pool_admits<9 && waits<9000) begin
            @(negedge clk); waits=waits+1;
            if(probe_pool==POOL_CAPACITY && pool_admits<9) saturated=saturated+1;
            // Once the second Put waits at a full pool, free one B at a time.
            hold_b=!(saturated>900 && cycle%40==0);
            s_d_ready=saturated>900 && cycle%3==0;
          end
          hold_b=0; s_d_ready=1;
        end
      join
      waits=0;
      while((busy[0] || busy[1] || busy[3]) && waits<4000) begin @(negedge clk); waits=waits+1; end
      if(busy[0] || busy[1] || busy[3]) $fatal(1,"ASSERT_FAIL:UH.POOL.D_TIMEOUT");
      // Single-word Gets and Puts from four sources overlap admissions with
      // releases.
      s_d_ready=1;
      for(integer n=0;n<48;n=n+1) begin
        waits=0;
        while(busy[2+n%4] && waits<200) begin @(negedge clk); waits=waits+1; end
        expect_d(n%3==0 ? 0:1,2,2+n%4,1,0);
        send_a(n%3==0 ? 0:4,2,2+n%4,(n%3==0 ? 512:256)+4*(n%4),15,n);
      end
      waits=0;
      while(pool_flight!=0 && waits<400) begin @(negedge clk); waits=waits+1; end
      if(pool_admits!=57 || saturated==0 || pool_mixed==0 || pool_d_stall==0 || pool_both==0)
        $fatal(1,"ASSERT_FAIL:UH.POOL.REACH admits=%0d saturated=%0d mixed=%0d stalled=%0d both=%0d",
               pool_admits,saturated,pool_mixed,pool_d_stall,pool_both);
      $display("UH_POOL admits=%0d saturated=%0d full=%0d mixed=%0d stalled=%0d both=%0d",
               pool_admits,saturated,pool_full,pool_mixed,pool_d_stall,pool_both);
    end
