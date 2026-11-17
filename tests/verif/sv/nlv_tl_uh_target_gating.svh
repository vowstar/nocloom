// Clock gating scenario. Bursts of traffic alternate with idle gaps, so every
// gate closes and wakes many times. Directed steps then place requests around
// the instant a gate closes, return responses while the initiator side
// sleeps, fill the context pool across a wake, expire the watchdog and assert
// the common reset with traffic in flight.
//
// WAKES is the least number of sleep and wake pairs of each focus gate in one
// run. The directed steps alone place a request at 8 edge offsets around the
// close of the initiator gate and at 12 half-cycle offsets around the close
// of the target gate, and each needs its own sleep. 16 leaves no run that
// passes on the directed steps of one gate only.
localparam WAKES=16;
integer cov_reset_inflight=0, cov_snipe=0;
real sn_quiet=0, sn_launch=0, sn_tp, sn_rel, sn_off;
time last_mpos=0;
always @(posedge clk) last_mpos=$time;
task random_knobs;
  begin
    set_stalls(rc(5),rc(9),rc(5),rc(5),rc(4),32+rc(160));
    d_policy=rc(3); k_wr_pct=20+rc(60); k_len_max=1+rc(12); k_long_pct=rc(8)==0 ? 10 : 0;
    k_gap=rc(6); k_ids=1+rc(16); k_narrow_pct=rc(50); k_unaligned_pct=rc(40); early_pct=rc(60);
  end
endtask
task burst(input integer most);
  integer m;
  begin
    random_knobs;
    for(m=0;m<NS;m=m+1) budget[m]=(m>0 && rc(3)==0) ? 0 : 1+rc(most);
    drain;
  end
endtask
task wait_outstanding(input integer count, input [255:0] label);
  integer n;
  begin
    n=0;
    while(outstanding<count && n<8192) begin @(posedge fclk); n=n+1; end
    if(outstanding<count) report_stuck(label);
  end
endtask
initial begin : scenario
  integer i, k, g, n, d, pass, gap;
  time t0;
  wait(ready_go);
  do_reset;
  quiet_check;
  full_size;

  // Bursts and idle gaps. A short gap ends near the close of a gate.
  for(i=0;i<BURSTS;i=i+1) begin
    burst(10);
    gap=rc(4)==0 ? rc(8) : 8+rc(40);
    repeat(gap) @(posedge clk);
    if(i%4==3) quiet_check;
  end

  // A bus request at each edge offset around the close of the initiator gate.
  set_stalls(0,0,0,0,0,0); d_gap=0; d_policy=1;
  for(pass=0;pass<2;pass=pass+1) for(d=0;d<8;d=d+1) begin
    @(posedge clk); force_txn(0,pass,d,4096*d+512,1+d%3,LSZ,g);
    drain;
    n=0;
    @(negedge clk);
    while(gq[IG]!=1 && n<256) begin @(negedge clk); n=n+1; end
    repeat(d) @(negedge clk);
    @(posedge clk); force_txn(0,1-pass,d+8,4096*d+1024,1,LSZ,g);
    drain;
  end

  // A network request at each half-cycle offset around the close of the
  // target gate. Two measurements place it: the time from the release of a
  // held response to the first quiet edge of that gate, and the time from a
  // request at the master to the first active edge of that gate.
  repeat(2) begin
    quiet_check;
    hold_d=1; wd_pause=1;
    @(posedge clk); force_txn(0,0,1,8192+512,1,2,g);
    wait_outstanding(1,"DIRECTED_READ_NOT_SEEN");
    #(8*slow_ps);
    t0=$time; hold_d=0; wd_pause=0; last_progress=$time;
    drain;
    n=0;
    @(negedge fclk);
    while(gq[TG]<1 && n<256) begin @(negedge fclk); n=n+1; end
    sn_quiet=g_tq1[TG]-t0;
    #(16*slow_ps);
    @(posedge clk); t0=$time; force_txn(0,0,2,8192+1024,1,2,g);
    n=0;
    while(g_tact[TG]<=t0 && n<512) begin @(negedge fclk); n=n+1; end
    sn_launch=g_tact[TG]-t0;
    drain;
  end
  for(pass=0;pass<3 && !(cov_exact[TG]>=2 && cov_one[TG]>=2);pass=pass+1)
    for(k=-4;k<=7;k=k+1) begin
      drain;
      #(12*slow_ps);
      hold_d=1; wd_pause=1; d_gap=0;
      @(posedge clk); force_txn(0,0,3,12288+512,1,2,g);
      wait_outstanding(1,"DIRECTED_READ_NOT_SEEN");
      #(6*slow_ps);
      // The second request must reach the gate two target cycles after its
      // first quiet edge, moved by the swept offset.
      sn_off=k*(PF/2.0);
      sn_tp=last_mpos+PM;
      sn_rel=sn_tp+sn_launch-sn_quiet-2.0*PF-sn_off;
      while(sn_rel<$time+PF || sn_tp-PM/2.0<$time+1) begin
        sn_tp=sn_tp+PM; sn_rel=sn_rel+PM;
      end
      fork
        begin #(sn_rel-$time); hold_d=0; wd_pause=0; last_progress=$time; end
        begin
          #(sn_tp-PM/2.0-$time);
          @(posedge clk); force_txn(0,0,4,16384+512,1,2,g);
        end
      join
      cov_snipe=cov_snipe+1;
      drain;
    end

  // The slave holds every response until the pool is full and the initiator
  // side sleeps. The responses then cross while that side wakes.
  quiet_check;
  hold_d=1; wd_pause=1;
  for(k=0;k<K+2;k=k+1) begin
    @(posedge clk); force_txn(k%NS,0,k%16,(k%16)*4096+1024+64*(k/16),2,LSZ,g);
  end
  wait_outstanding(K,"POOL_FULL_NOT_REACHED");
  n=0;
  while(g_en[IG] && n<256) begin @(negedge clk); n=n+1; end
  #(8*slow_ps);
  hold_d=0; wd_pause=0; last_progress=$time;
  drain;
  burst(6);

  // A response and a request that wait longer than the watchdog limit.
  if(TIMEOUT>0) begin
    quiet_check;
    check(!t_timed_out || max_wait_hi>=TIMEOUT-2,"TIMEOUT_EARLY");
    set_stalls(0,0,0,0,0,0); d_gap=0;
    hold_d=1; wd_pause=1;
    @(posedge clk); force_txn(0,0,7,20480,4,LSZ,g);
    wait_outstanding(1,"DIRECTED_READ_NOT_SEEN");
    repeat(TIMEOUT+12) @(posedge fclk);
    check(t_timed_out,"TIMEOUT_EXPIRY");
    hold_d=0; wd_pause=0; last_progress=$time;
    drain;
    // Later work completes. The gates close again and the status stays high.
    burst(6);
    quiet_check;
    check(t_timed_out,"TIMEOUT_STATUS_HELD");
    hold_a=1; wd_pause=1;
    @(posedge clk); force_txn(0,1,9,24576,4,LSZ,g);
    repeat(TIMEOUT+12) @(posedge fclk);
    hold_a=0; wd_pause=0; last_progress=$time;
    drain;
  end

  // Common reset with requests and responses in flight.
  random_knobs; k_len_max=16; k_long_pct=20; k_gap=0;
  for(k=0;k<NS;k=k+1) budget[k]=12;
  n=0;
  while(!(accepted_now>=2 && outstanding>=1) && n<4000) begin @(posedge clk); n=n+1; end
  check(accepted_now>=1,"RESET_WITHOUT_TRAFFIC");
  cov_reset_inflight=accepted_now;
  #(rc(PM));
  do_reset;
  quiet_check;
  for(i=0;i<4;i=i+1) begin
    burst(10);
    repeat(rc(24)) @(posedge clk);
  end
  quiet_check;
  pool_probe;
  quiet_check;

  report_common;
  $display("UHG_COVER focus_initiator_wakes=%0d focus_target_wakes=%0d initiator_exact_close=%0d initiator_one_edge=%0d target_exact_close=%0d target_one_edge=%0d",
           cov_wake[IG],cov_wake[TG],cov_exact[IG],cov_one[IG],cov_exact[TG],cov_one[TG]);
  $display("UHG_COVER d_while_initiator_gated=%0d response_at_wake=%0d sleep_with_pending=%0d full_pool_wakes=%0d timeout_expired=%0d gate_closed_after_timeout=%0d reset_in_flight=%0d directed_offsets=%0d",
           cov_d_init_closed,cov_resp_wake,cov_sleep_pending,cov_full_wake,cov_timeout,
           cov_gate_after_timeout,cov_reset_inflight,cov_snipe);
  if(cov_wake[IG]<WAKES) nlv_fail_report("COVERAGE","initiator_gate_wakes",cov_wake[IG]);
  if(cov_wake[TG]<WAKES) nlv_fail_report("COVERAGE","target_gate_wakes",cov_wake[TG]);
  for(i=0;i<NG;i=i+1) need(cov_wake[i],"every_gate_wakes");
  need(cov_exact[IG]+cov_one[IG],"request_at_initiator_gate_close");
  need(cov_exact[TG]+cov_one[TG],"request_at_target_gate_close");
  need(cov_d_init_closed,"d_while_initiator_gated");
  need(cov_full_wake,"full_pool_across_wake");
  need(cov_pool_full,"pool_full");
  need(cov_reset_inflight,"reset_in_flight");
  if(TIMEOUT>0) begin
    need(cov_timeout,"timeout_expired");
    need(cov_gate_after_timeout,"gate_closed_after_timeout");
  end
  $display("UH_PASS gating seed=%0d pm=%0d pf=%0d reads=%0d writes=%0d messages=%0d",
           SEED,PM,PF,n_reads,n_writes,n_msgs);
  $finish;
end
