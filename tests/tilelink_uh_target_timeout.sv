always #5 clk=~clk;
integer pulses=0, gets=0, responses=0;
always @(posedge @CLOCK@) if(reset_n) pulses=pulses+1;
always @(posedge clk) if(reset_n) begin
  if(t_a_valid && t_a_ready) begin
    if(t_a_opcode!=4 || t_a_size!=2 || t_a_address!=(gets==0 ? 32'h100 : 32'h200))
      $fatal(1,"CHECK_FAIL GET");
    gets=gets+1;
  end
  if(s_rvalid && s_rready) begin
    if(s_rdata!=32'h12345678 || s_rresp!=0 || !s_rlast)
      $fatal(1,"CHECK_FAIL RESPONSE");
    responses=responses+1;
  end
end
task sleep_check;
  begin
    repeat(40) @(negedge clk);
    @(posedge clk); #1;
    if(@REMAINING@ !== 0 || @CLOCK@ !== 0) $fatal(1,"CHECK_FAIL GATE_SLEEP");
  end
endtask
task read_word(input integer address);
  integer before_pulses;
  begin
    before_pulses=pulses;
    @(negedge clk); s_araddr=address; s_arlen=0; s_arsize=2; s_arburst=1; s_arvalid=1;
    @(posedge clk); while(!s_arready) @(posedge clk);
    @(negedge clk); s_arvalid=0;
    wait(t_a_valid);
    t_d_source=t_a_source;
    @(posedge clk); while(!t_a_ready) @(posedge clk);
    repeat(24) @(negedge clk);
    if(!t_timed_out) $fatal(1,"CHECK_FAIL WATCHDOG_EXPIRY");
    t_d_valid=1; t_d_opcode=1; t_d_size=2; t_d_data=32'h12345678;
    @(posedge clk); while(!t_d_ready) @(posedge clk);
    @(negedge clk); t_d_valid=0;
    wait(responses==gets);
    if(pulses<=before_pulses) $fatal(1,"CHECK_FAIL GATE_WAKE");
  end
endtask
initial begin
  repeat(160) @(negedge clk); reset_n=1;
  t_a_ready=1; s_rready=1;
  sleep_check;
  read_word(32'h100);
  sleep_check;
  read_word(32'h200);
  sleep_check;
  if(gets!=2 || responses!=2) $fatal(1,"CHECK_FAIL TRANSACTIONS");
  $display("UH_PASS gated timeout K=%0d", K); $finish;
end
initial begin repeat(2000) @(negedge clk); $fatal(1,"CHECK_FAIL CYCLE_LIMIT"); end
