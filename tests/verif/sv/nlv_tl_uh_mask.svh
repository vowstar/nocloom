    begin : narrow_mask
      integer request_size[0:9], request_address[0:9], word_address[0:9];
      reg [3:0] request_mask[0:9], expected_strobe[0:9];
      reg [31:0] expected_word[0:9], actual_word;
      integer before_w, before_aw, waits;
      // Literal lane and memory expectations are independent of the DUT.
      request_size[0]=0; request_address[0]=32'h304; word_address[0]=32'h304;
      request_size[1]=0; request_address[1]=32'h301; word_address[1]=32'h300;
      request_size[2]=0; request_address[2]=32'h30a; word_address[2]=32'h308;
      request_size[3]=0; request_address[3]=32'h30f; word_address[3]=32'h30c;
      request_size[4]=1; request_address[4]=32'h310; word_address[4]=32'h310;
      request_size[5]=1; request_address[5]=32'h316; word_address[5]=32'h314;
      request_size[6]=1; request_address[6]=32'h318; word_address[6]=32'h318;
      request_size[7]=1; request_address[7]=32'h31e; word_address[7]=32'h31c;
      request_size[8]=0; request_address[8]=32'h321; word_address[8]=32'h320;
      request_size[9]=1; request_address[9]=32'h326; word_address[9]=32'h324;
      expected_strobe[0]=1; expected_word[0]=32'h111111ef;
      expected_strobe[1]=2; expected_word[1]=32'h1111be11;
      expected_strobe[2]=4; expected_word[2]=32'h11ad1111;
      expected_strobe[3]=8; expected_word[3]=32'hde111111;
      expected_strobe[4]=3; expected_word[4]=32'h1111beef;
      expected_strobe[5]=12; expected_word[5]=32'hdead1111;
      expected_strobe[6]=2; expected_word[6]=32'h1111be11;
      expected_strobe[7]=4; expected_word[7]=32'h11ad1111;
      expected_strobe[8]=2; expected_word[8]=32'h1111be11;
      expected_strobe[9]=0; expected_word[9]=32'h11111111;
      for(integer v=0;v<10;v=v+1) begin
        request_mask[v]=v==6 ? 14:v==7 ? 5:15;
        s_d_ready=1; before_w=w_count; before_aw=aw_count;
        expect_d(0,request_size[v],3,1,v==9);
        // Inactive mask bits deliberately violate the external A contract.
        if(v==9) begin
          @(negedge clk);
          s_a_opcode=1; s_a_param=0; s_a_size=request_size[v]; s_a_source=3;
          s_a_address=request_address[v]; s_a_mask=request_mask[v];
          s_a_data=32'hdeadbeef; s_a_corrupt=1; s_a_valid=1;
          waits=0;
          @(posedge clk);
          while(!s_a_ready && waits<512) begin @(posedge clk); waits=waits+1; end
          if(!s_a_ready) $fatal(1,"ASSERT_FAIL:UH.MASK.CORRUPT_TIMEOUT");
          @(negedge clk); s_a_valid=0; s_a_corrupt=0;
        end else
          send_a(v==8 ? 0:1,request_size[v],3,request_address[v],request_mask[v],32'hdeadbeef);
        drain;
        if(aw_count!=before_aw+1 || write_address!=request_address[v] ||
            write_size!=request_size[v] || write_len!=0 || w_count!=before_w+1)
          $fatal(1,"ASSERT_FAIL:UH.MASK.DESCRIPTOR vector=%0d",v);
        if(seen_strobe[before_w]!=expected_strobe[v])
          $fatal(1,"ASSERT_FAIL:UH.MASK.STROBE vector=%0d got=%h expected=%h",
                 v,seen_strobe[before_w],expected_strobe[v]);
        actual_word={memory[word_address[v]+3],memory[word_address[v]+2],
                     memory[word_address[v]+1],memory[word_address[v]]};
        if(actual_word!=expected_word[v])
          $fatal(1,"ASSERT_FAIL:UH.MASK.MEMORY vector=%0d got=%h expected=%h",
                 v,actual_word,expected_word[v]);
      end
      $display("UH_MASK vectors=10");
    end
