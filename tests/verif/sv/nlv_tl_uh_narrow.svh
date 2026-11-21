    begin : narrow
      integer base, offset, sz, source, first_ar, held;
      reg [31:0] word, lanes, data;
      reg [3:0] d_source;
      reg [3:0] d_size;
      // Distinct bytes make every lane of a returned word identifiable.
      for(integer b=0;b<4096;b=b+1) memory[b]=(b*37+5) & 255;
      for(integer n=0;n<12;n=n+1) begin
        base=n<6 ? 512:1540;
        sz=n%6<4 ? 0:1;
        offset=sz==0 ? n%6 : 2*(n%6-4);
        source=(5*n+3)%16;
        word=0;
        for(integer b=0;b<4;b=b+1) word[b*8+:8]=memory[base+b];
        lanes=(sz==0 ? 32'hff:32'hffff) << (8*offset);
        first_ar=ar_count;
        s_d_ready=0; read_size=sz;
        expect_d(1,sz,source,1,0);
        expected_word[source]=word; expected_lanes[source]=lanes;
        send_a(4,sz,source,base+offset,(sz==0 ? 1:3) << offset,0);
        held=0;
        while(!s_d_valid && held<60) begin @(negedge clk); held=held+1; end
        if(!s_d_valid) $fatal(1,"ASSERT_FAIL:UH.NARROW.D_TIMEOUT get=%0d",n);
        if(ar_count!=first_ar+1 || r_addresses[first_ar]!=base+offset ||
            r_sizes[first_ar]!=sz || r_lengths[first_ar]!=0)
          $fatal(1,"ASSERT_FAIL:UH.NARROW.AR get=%0d address=%h size=%0d len=%0d",
                 n,r_addresses[first_ar],r_sizes[first_ar],r_lengths[first_ar]);
        if(s_d_opcode!=1 || s_d_source!=source || s_d_size!=sz || s_d_denied || s_d_corrupt)
          $fatal(1,"ASSERT_FAIL:UH.NARROW.D_IDENTITY get=%0d source=%0d size=%0d",
                 n,s_d_source,s_d_size);
        for(integer b=0;b<4;b=b+1)
          if(lanes[b*8] && s_d_data[b*8+:8]!=word[b*8+:8])
            $fatal(1,"ASSERT_FAIL:UH.NARROW.BYTE get=%0d lane=%0d got=%h expected=%h",
                   n,b,s_d_data[b*8+:8],word[b*8+:8]);
        // D holds its message while the initiator stalls it.
        data=s_d_data; d_source=s_d_source; d_size=s_d_size;
        repeat(3) begin
          @(negedge clk);
          if(!s_d_valid || s_d_data!=data || s_d_source!=d_source || s_d_size!=d_size ||
              s_d_opcode!=1)
            $fatal(1,"ASSERT_FAIL:UH.NARROW.D_STALL get=%0d",n);
        end
        s_d_ready=1;
        @(negedge clk); s_d_ready=0;
        if(busy[source]) $fatal(1,"ASSERT_FAIL:UH.NARROW.D_RETIRE get=%0d",n);
      end
      read_size=2;
      $display("UH_NARROW gets=12 ar=%0d",ar_count);
      drain;
    end
