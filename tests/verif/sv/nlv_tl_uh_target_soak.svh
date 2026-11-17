// Long-run retirement scenario. The masters keep more requests in flight than
// the target has contexts. The environment stalls every channel in turn, each
// stall ends within LONG cycles, and the slave lets at most F+K-1 other
// messages pass a waiting one. Under this fair environment every accepted
// request must retire, within the latency bound and the starvation bound of
// the environment include. A finite run gives bounded evidence only. It is
// not a proof that every request retires in every fair run.
integer prof=0, contended=0, base[0:4], alone[0:4], combined[0:4];
integer accepted_sum=0, pending_sum=0;
always @(posedge clk) if(reset_n && started) begin
  if(accepted_now>=K) contended=contended+1;
  accepted_sum=accepted_sum+accepted_now; pending_sum=pending_sum+pending;
end
task counters(input integer mode);
  integer c, now[0:4];
  begin
    now[0]=cov_long_a; now[1]=cov_long_d; now[2]=cov_long_r; now[3]=cov_long_b; now[4]=cov_long_w;
    for(c=0;c<5;c=c+1) begin
      if(mode==c+1) alone[c]=alone[c]+now[c]-base[c];
      if(mode==6) combined[c]=combined[c]+now[c]-base[c];
      base[c]=now[c];
    end
  end
endtask
// Profile 0 is light. Profiles 1 to 5 stall one channel alone: A, D, R, B
// and W. Profile 6 stalls all five. Profile 7 mixes random limits. Profiles
// 8 and 9 make A or the read responses the narrow point with short stalls,
// so that several contexts wait for one grant at the same time.
task profile(input integer p);
  begin
    case(p)
      0: set_stalls(1,1,1,1,1,48);
      1: set_stalls(LONG,0,0,0,0,96);
      2: set_stalls(0,LONG,0,0,0,96);
      3: set_stalls(0,0,LONG,0,0,96);
      4: set_stalls(0,0,0,LONG,0,96);
      5: set_stalls(0,0,0,0,LONG,96);
      6: set_stalls(LONG,LONG,LONG,LONG,LONG,96);
      7: set_stalls(rc(LONG/2),rc(LONG/2),rc(LONG/2),rc(LONG/2),rc(LONG/2),16+rc(200));
      8: set_stalls(6,0,0,0,0,255);
      default: set_stalls(0,0,6,0,0,255);
    endcase
    d_policy=rc(3); k_len_max=1+rc(8); k_long_pct=rc(4)==0 ? 3 : 0; k_gap=rc(3);
    early_pct=rc(60); k_unaligned_pct=rc(30);
    // The network carries reads and writes to the target in one stream, and
    // a write holds that stream until its last word. An initiator splits a
    // narrow burst into single beats that follow each other, and it keeps
    // fewer long bursts in flight than short ones. Three profiles of four
    // therefore send short full-width reads with separate IDs, and 60/(K+1)
    // percent of their bursts are writes, so that the reads between two writes
    // fill the pool. The fourth mixes writes, long and narrow bursts and
    // shared IDs.
    if(rc(4)!=0) begin
      k_ids=16; k_wr_pct=60/(K+1); k_narrow_pct=0; k_unaligned_pct=0; k_long_pct=0;
      k_len_max=DW>32 ? 1 : 1+rc(4);
    end else begin k_ids=rc(2) ? 3 : 16; k_wr_pct=30+rc(40); k_narrow_pct=rc(40); end
    // Independent short reads keep every context a candidate for the grant.
    if(p>=8) begin k_wr_pct=0; k_ids=16; k_long_pct=0; k_len_max=4; end
  end
endtask
initial begin : scenario
  integer m, c, s, slots, missing;
  wait(ready_go);
  for(c=0;c<5;c=c+1) begin base[c]=0; alone[c]=0; combined[c]=0; end
  long_mark=(LONG*3)/4;
  do_reset;
  full_size;
  // The bounds use the longest stall of the run from the first request on.
  set_stalls(LONG,LONG,LONG,LONG,LONG,0);
  mon_fair=1; k_inflight=24;
  for(m=0;m<NS;m=m+1) budget[m]=-1;
  // The run lasts GOAL cycles of both clocks. It goes on, within a limit,
  // until every channel has met a long stall alone and with the others.
  missing=1;
  while(mcycle<GOAL || fcycle<GOAL || (missing && prof<400)) begin
    profile(prof%10);
    // The profiles that slow the target side last three times as long. They
    // keep packets in their contexts.
    #((25+rc(17))*LONG*slow_ps*((prof%10)==1 || (prof%10)==2 || (prof%10)==6 ? 3 : 1));
    counters(prof%10);
    prof=prof+1;
    missing=0;
    for(c=0;c<5;c=c+1) if(alone[c]==0 || combined[c]==0) missing=1;
  end
  for(m=0;m<NS;m=m+1) budget[m]=0;
  profile(0);
  drain;
  quiet_check;
  pool_probe;
  quiet_check;

  report_common;
  slots=0;
  for(s=0;s<K;s=s+1) if(src_msgs[s]>0) slots=slots+1;
  $display("UHG_STAT mean_accepted_x100=%0d mean_pending_x100=%0d",accepted_sum/(mcycle/100),pending_sum/(mcycle/100));
  $display("UHG_COVER profiles=%0d contended_cycles=%0d done_oldest=%0d done_newest=%0d served_oldest=%0d served_newest=%0d grant_wraps=%0d",
           prof,contended,cov_done_oldest,cov_done_newest,cov_served_oldest,cov_served_newest,cov_rr_wrap);
  $display("UHG_COVER long_a_alone=%0d long_d_alone=%0d long_r_alone=%0d long_b_alone=%0d long_w_alone=%0d long_a_combined=%0d long_d_combined=%0d long_r_combined=%0d long_b_combined=%0d long_w_combined=%0d",
           alone[0],alone[1],alone[2],alone[3],alone[4],combined[0],combined[1],combined[2],
           combined[3],combined[4]);
  check(mcycle>=GOAL && fcycle>=GOAL,"RUN_LENGTH");
  if(slots!=K) nlv_fail_report("COVERAGE","every_context_slot_used",slots);
  // At least three contexts of four hold a packet in most target cycles, and
  // every context holds one in a quarter of them or more.
  if(POOL_PROBE ? 2*pool_high_cycles<fcycle : 2*contended<mcycle)
    nlv_fail_report("COVERAGE","pool_mostly_full",pool_high_cycles);
  if(POOL_PROBE && 4*pool_full_cycles<fcycle)
    nlv_fail_report("COVERAGE","pool_often_all_full",pool_full_cycles);
  need(cov_pool_full,"pool_full");
  need(cov_done_oldest,"oldest_request_retires");
  if(K>1) begin
    need(cov_served_oldest,"oldest_context_served");
    need(cov_served_newest,"newest_context_served");
    need(cov_rr_wrap,"grant_wraps");
  end
  for(c=0;c<5;c=c+1) begin
    need(alone[c],"long_stall_alone");
    need(combined[c],"long_stall_combined");
  end
  $display("UH_PASS soak seed=%0d pm=%0d pf=%0d reads=%0d writes=%0d messages=%0d",
           SEED,PM,PF,n_reads,n_writes,n_msgs);
  $finish;
end
