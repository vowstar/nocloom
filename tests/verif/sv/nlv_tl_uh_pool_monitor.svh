  // Shared request pool of the initiator, checked every cycle after reset.
  // Admission and release must keep the count equal to the units admitted,
  // at most the capacity, and zero with no message in flight. The bursts the
  // target holds are at most the count, plus one for a request packet the
  // network entry offers: the target can take its address before admission
  // counts the packet. A source stays in flight until its final D.
  localparam POOL_CAPACITY=@POOL_CAPACITY@;
  integer pool_admitted=0, pool_admits=0, pool_reads=0, pool_writes=0;
  integer pool_flight=0, pool_a_left=0;
  integer pool_full=0, pool_mixed=0, pool_d_stall=0, pool_both=0;
  integer pool_d_left[0:255];
  initial for(integer s=0;s<256;s=s+1) pool_d_left[s]=0;
  always @(posedge clk) if(reset_n) begin
    if(probe_pool>POOL_CAPACITY) $fatal(1,"ASSERT_FAIL:UH.POOL.BOUND pool=%0d",probe_pool);
    if(probe_release && probe_pool==0) $fatal(1,"ASSERT_FAIL:UH.POOL.RELEASE");
    if(probe_pool!=pool_admitted)
      $fatal(1,"ASSERT_FAIL:UH.POOL.CONSERVE pool=%0d admitted=%0d",probe_pool,pool_admitted);
    if(pool_reads+pool_writes>probe_pool+probe_offer ||
        pool_reads+pool_writes>POOL_CAPACITY)
      $fatal(1,"ASSERT_FAIL:UH.POOL.HOLDS pool=%0d reads=%0d writes=%0d",probe_pool,pool_reads,pool_writes);
    if(pool_flight==0 && probe_pool!=0) $fatal(1,"ASSERT_FAIL:UH.POOL.IDLE pool=%0d",probe_pool);
    if(probe_pool==POOL_CAPACITY) pool_full=pool_full+1;
    if(probe_pool==POOL_CAPACITY && pool_reads>0 && pool_writes>0) pool_mixed=pool_mixed+1;
    if(probe_pool==POOL_CAPACITY && s_d_valid && !s_d_ready) pool_d_stall=pool_d_stall+1;
    if(probe_admit && probe_release) pool_both=pool_both+1;
    pool_admitted=pool_admitted+probe_admit-probe_release;
    pool_admits=pool_admits+probe_admit;
    pool_reads=pool_reads+(t_arvalid && t_arready)-(t_rvalid && t_rready && t_rlast);
    pool_writes=pool_writes+(t_awvalid && t_awready)-(t_bvalid && t_bready);
    if(s_d_valid && s_d_ready) begin
      if(pool_d_left[s_d_source]==0) $fatal(1,"ASSERT_FAIL:UH.POOL.D_SOURCE source=%0d",s_d_source);
      pool_d_left[s_d_source]=pool_d_left[s_d_source]-1;
      if(pool_d_left[s_d_source]==0) pool_flight=pool_flight-1;
    end
    if(s_a_valid && s_a_ready) begin
      if(pool_a_left==0) begin
        if(pool_d_left[s_a_source]!=0) $fatal(1,"ASSERT_FAIL:UH.POOL.SOURCE_HELD source=%0d",s_a_source);
        pool_flight=pool_flight+1;
        pool_d_left[s_a_source]=(s_a_opcode==4 || s_a_opcode==2 || s_a_opcode==3) && s_a_size>2 ?
                                1<<(s_a_size-2) : 1;
        pool_a_left=s_a_opcode<4 && s_a_size>2 ? 1<<(s_a_size-2) : 1;
      end
      pool_a_left=pool_a_left-1;
    end
  end
