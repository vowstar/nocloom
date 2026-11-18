localparam CYCLES = 16;
integer scenario, cycle = 0, waited = 0, completed = 0, answers = 0;
integer first = 0, last = 0, sent_last = 0;
reg expired = 0;
reg block_target = 0;
wire eligible, waiting, done, target_write;
wire [31:0] target_data;

`ifdef TARGET_APB
  assign eligible = t_psel && t_penable;
  assign waiting = eligible && !t_pready;
  assign done = eligible && t_pready;
  assign target_write = t_pwrite;
  assign target_data = t_pwdata;
  always_comb t_pready = !block_target;
`elsif TARGET_WB_CLASSIC
  assign eligible = t_cyc && t_stb;
  assign waiting = eligible && !t_ack;
  assign done = eligible && t_ack;
  assign target_write = t_we;
  assign target_data = t_dat_w;
  always_comb t_ack = eligible && !block_target;
`elsif TARGET_WB_PIPELINED
  reg accepted = 0;
  assign eligible = accepted;
  assign waiting = (t_cyc && t_stb && t_stall) || (accepted && !t_ack);
  assign done = accepted && t_ack;
  assign target_write = t_we;
  assign target_data = t_dat_w;
  always_comb begin
    t_stall = scenario == 5 && block_target;
    t_ack = accepted && !block_target;
  end
  always @(posedge clk) begin
    if (!reset_n || done) accepted <= 0;
    else if (t_cyc && t_stb && !t_stall) accepted <= 1;
  end
`endif

always #5 clk = ~clk;
always @(posedge clk) if (reset_n) begin
  cycle = cycle + 1;
  if (s_wvalid && s_wready && s_wlast) sent_last = cycle;
  if (done) begin
    if (target_write !== 1'b1 || target_data !== (32'h5a003000 + completed))
      $fatal(1, "CHECK_FAIL TARGET_WRITE beat=%0d data=%h", completed, target_data);
    if (completed == 0) first = cycle;
    last = cycle;
    completed = completed + 1;
  end
  if (s_bvalid && s_bready) begin
    if (s_bresp !== (expired ? 2'b10 : 2'b00) || (expired && !COMPLETION))
      $fatal(1, "CHECK_FAIL WRITE_RESPONSE code=%0d", s_bresp);
    answers = answers + 1;
  end
  waited = waiting ? waited + 1 : 0;
  if (waited == CYCLES) expired = 1;
  #1;
  if (scenario != 0) begin
    if (t_timed_out && !expired) $fatal(1, "CHECK_FAIL EARLY_EXPIRY cycle=%0d", cycle);
    if (!t_timed_out && expired) $fatal(1, "CHECK_FAIL MISSING_EXPIRY cycle=%0d", cycle);
  end
end

task beat(input integer index, input integer beats);
  begin
    s_wvalid = 1;
    s_wdata = 32'h5a003000 + index;
    s_wstrb = 4'hf;
    s_wlast = index == beats - 1;
    @(posedge clk); while (!s_wready) @(posedge clk);
    @(negedge clk); s_wvalid = 0;
  end
endtask

initial begin
  if (!$value$plusargs("SCENARIO=%d", scenario)) scenario = 0;
  repeat (8) @(negedge clk);
  reset_n = 1; s_bready = 1;
  repeat (8) @(negedge clk);
  s_awaddr = 'h100; s_awlen = 3; s_awsize = 2; s_awburst = 1; s_awvalid = 1;
  @(posedge clk); while (!s_awready) @(posedge clk);
  @(negedge clk); s_awvalid = 0;
  if (scenario == 2 || scenario == 3 || scenario == 4 || scenario == 5)
    block_target = 1;
  beat(0, 4);
  if (scenario == 3 || scenario == 4) begin
    wait (waited == CYCLES - 1);
    @(negedge clk); block_target = 0;
  end
  if (scenario == 1 || scenario == 4) begin
    wait (completed == 1);
    repeat (2 * CYCLES) @(negedge clk);
  end
  if (scenario == 4) block_target = 1;
  beat(1, 4); beat(2, 4); beat(3, 4);
  repeat (4 * CYCLES) @(negedge clk);
  if (scenario == 2 || scenario == 4 || scenario == 5) begin
    if (!expired || answers != COMPLETION || completed != (scenario == 4 ? 1 : 0))
      $fatal(1, "CHECK_FAIL HUNG_RESULT completed=%0d answers=%0d", completed, answers);
  end else if (expired || completed != 4 || answers != 1)
    $fatal(1, "CHECK_FAIL NORMAL_RESULT completed=%0d answers=%0d", completed, answers);
  $display("WATCHDOG_PASS scenario=%0d completion=%0d first=%0d last=%0d sent_last=%0d",
           scenario, COMPLETION, first, last, sent_last);
  $finish;
end

initial begin
  repeat (512) @(negedge clk);
  $fatal(1, "CHECK_FAIL CYCLE_LIMIT");
end
