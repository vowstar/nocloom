integer scenario, cycle = 0, waited = 0, completed = 0, answers = 0;
integer first = 0, last = 0, sent_last = 0;
localparam READ_WORD = 32'h6b001111;
reg expired = 0;
reg block_target = 0;
wire eligible, waiting, done, target_write;
wire [31:0] target_data;
integer reads = 0;

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
`elsif TARGET_AHB_LITE
  reg data_phase = 0, writing = 0;
  assign eligible = data_phase || (t_hsel && t_htrans[1]);
  assign waiting = eligible && !t_hreadyout;
  assign done = data_phase && t_hreadyout;
  assign target_write = writing;
  assign target_data = t_hwdata;
  always_comb t_hreadyout = !block_target || (scenario != 5 && !data_phase);
  always @(posedge clk) begin
    if (!reset_n) data_phase <= 0;
    else if (t_hreadyout) begin
      data_phase <= t_hsel && t_htrans[1];
      writing <= t_hwrite;
    end
  end
`elsif TARGET_TL_UL
  reg accepted = 0, writing = 0;
  reg [31:0] payload = 0;
  assign eligible = accepted;
  assign waiting = (t_a_valid && !t_a_ready) || (accepted && !t_d_valid);
  assign done = t_d_valid && t_d_ready;
  assign target_write = writing;
  assign target_data = payload;
  always_comb begin
    t_a_ready = !accepted && (scenario != 5 || !block_target);
    t_d_valid = accepted && !block_target;
    t_d_opcode = writing ? 0 : 1;
    t_d_size = 2;
  end
  always @(posedge clk) begin
    if (!reset_n || done) accepted <= 0;
    else if (t_a_valid && t_a_ready) begin
      accepted <= 1;
      payload <= t_a_data;
      writing <= t_a_opcode != 4;
    end
  end
`elsif TARGET_AXI4
  reg aw_taken = 0, last_taken = 0, read_pending = 0;
  reg [3:0] read_left = 0;
  integer read_gap = 0, target_reads = 0, aw_waited = 0, b_waited = 0;
  assign eligible = aw_taken && last_taken;
  assign waiting = (t_awvalid && !t_awready) || (t_wvalid && !t_wready)
                   || (eligible && !t_bvalid) || (t_arvalid && !t_arready && !t_rvalid)
                   || (read_pending && !t_rvalid);
  assign done = t_wvalid && t_wready;
  assign target_write = 1;
  assign target_data = t_wdata;
  always_comb begin
    t_awready = !aw_taken && scenario != 10 && (scenario != 5 || !block_target);
    t_wready = !last_taken && (!block_target || scenario == 2 || scenario == 5 ||
                              (scenario == 9 && !t_wlast));
    t_bvalid = eligible && (scenario != 2 || !block_target) && (scenario != 11 || !read_pending);
    t_arready = !read_pending && (scenario != 6 || !block_target);
    t_rvalid = read_pending && read_gap == 0 && (scenario != 7 || !block_target) &&
               (scenario != 12 || !eligible);
    t_rdata = READ_WORD;
    t_rlast = read_left == 0;
    if (scenario == 13) begin
      t_awready = !block_target;
      t_wready = !last_taken || aw_taken;
      t_bvalid = 0;
    end
  end
  always @(posedge clk) begin
    if (!reset_n) begin
      t_bid <= 0;
      t_rid <= 0;
    end else begin
      if (t_awvalid && t_awready) t_bid <= t_awid;
      if (t_arvalid && t_arready) t_rid <= t_arid;
    end
    if (!reset_n || (t_bvalid && t_bready)) begin
      aw_taken <= 0;
      last_taken <= 0;
    end else begin
      if (t_awvalid && t_awready) aw_taken <= 1;
      if (done && t_wlast) last_taken <= 1;
    end
    if (read_gap != 0) read_gap <= read_gap - 1;
    if (!reset_n) read_pending <= 0;
    else if (t_rvalid && t_rready) begin
      target_reads <= target_reads + 1;
      if (scenario == 10) read_gap <= 15;
      if (t_rlast) read_pending <= 0;
      else read_left <= read_left - 1;
    end else if (t_arvalid && t_arready) begin
      if (scenario == 10) read_gap <= 14;
      read_pending <= 1;
      read_left <= t_arlen;
    end
  end
  always @(posedge clk) if (reset_n && scenario == 10) begin
    aw_waited = t_awvalid && !t_awready ? aw_waited + 1 : 0;
    #1;
    if (aw_waited < CYCLES && t_timed_out) $fatal(1, "CHECK_FAIL AW_EARLY_EXPIRY");
    if (aw_waited == CYCLES) begin
      if (target_reads == 0 || completed != 1) $fatal(1, "CHECK_FAIL AW_MISSING_OVERLAP");
      if (!t_timed_out) $fatal(1, "CHECK_FAIL AW_MISSING_EXPIRY");
    end
  end
  always @(posedge clk) if (reset_n && scenario == 13) begin
    b_waited = eligible && !t_bvalid ? b_waited + 1 : 0;
    #1;
    if (b_waited < CYCLES && t_timed_out) $fatal(1, "CHECK_FAIL B_EARLY_EXPIRY");
    if (b_waited == CYCLES) begin
      if (completed != 2) $fatal(1, "CHECK_FAIL B_MISSING_OVERLAP");
      if (!t_timed_out) $fatal(1, "CHECK_FAIL B_MISSING_EXPIRY");
    end
  end
`elsif TARGET_AXI4_LITE
  reg aw_taken = 0, w_taken = 0, read_pending = 0;
  reg [31:0] payload = 0;
  assign eligible = aw_taken && w_taken;
  assign waiting = (t_awvalid && !t_awready) || (t_wvalid && !t_wready)
                   || (eligible && !t_bvalid) || (t_arvalid && !t_arready && !t_rvalid)
                   || (read_pending && !t_rvalid);
  assign done = t_bvalid && t_bready;
  assign target_write = 1;
  assign target_data = payload;
  always_comb begin
    t_awready = !aw_taken && (scenario != 5 || !block_target);
    t_wready = !w_taken && (!block_target || scenario == 2 || scenario == 5);
    t_bvalid = eligible && (scenario != 2 || !block_target);
    t_arready = !read_pending && (scenario != 6 || !block_target);
    t_rvalid = read_pending && (scenario != 7 || !block_target);
    t_rdata = READ_WORD;
  end
  always @(posedge clk) begin
    if (!reset_n || done) begin
      aw_taken <= 0;
      w_taken <= 0;
    end else begin
      if (t_awvalid && t_awready) aw_taken <= 1;
      if (t_wvalid && t_wready) begin
        w_taken <= 1;
        payload <= t_wdata;
      end
    end
    if (!reset_n || (t_rvalid && t_rready)) read_pending <= 0;
    else if (t_arvalid && t_arready) read_pending <= 1;
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
    if (s_bresp !== (expired ? 2'b10 : 2'b00) || s_bid !== s_awid || (expired && !COMPLETION))
      $fatal(1, "CHECK_FAIL WRITE_RESPONSE code=%0d", s_bresp);
    answers = answers + 1;
  end
  if (s_rvalid && s_rready) begin
    if (s_rid !== s_arid) $fatal(1, "CHECK_FAIL READ_ID");
    if (scenario == 8 || scenario >= 10) begin
      if (s_rresp !== 2'b00 || s_rdata !== READ_WORD ||
          s_rlast !== (scenario == 8 ? reads >= 7 : reads == s_arlen))
        $fatal(1, "CHECK_FAIL BACKPRESSURE_READ beat=%0d", reads);
    end else if (!expired || !COMPLETION || s_rresp !== 2'b10 || s_rdata !== 0 || !s_rlast)
      $fatal(1, "CHECK_FAIL READ_RESPONSE code=%0d data=%h", s_rresp, s_rdata);
    reads = reads + 1;
  end
  waited = waiting ? waited + 1 : 0;
  if (scenario < 10 && waited == CYCLES) expired = 1;
  #1;
  if (scenario > 0 && scenario < 10) begin
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

task read_request(input [31:0] address, input integer beats);
  begin
    s_araddr = address; s_arsize = 2; s_arburst = 1; s_arlen = beats - 1;
    s_arid = 3; s_arvalid = 1;
    @(posedge clk); while (!s_arready) @(posedge clk);
    @(negedge clk); s_arvalid = 0;
  end
endtask

task read_hang;
  begin
    block_target = 1;
    s_rready = 1;
    read_request('h180, 1);
    repeat (4 * CYCLES) @(negedge clk);
    if (!expired || reads != COMPLETION || answers != 0)
      $fatal(1, "CHECK_FAIL HUNG_READ_RESULT reads=%0d", reads);
  end
endtask

`ifdef TARGET_AXI4
`define AXI_READ_BACKPRESSURE
`elsif TARGET_AXI4_LITE
`define AXI_READ_BACKPRESSURE
`endif
`ifdef AXI_READ_BACKPRESSURE
task read_backpressure;
  integer expected;
  begin
    read_request('h180, 8);
`ifdef TARGET_AXI4
    read_request('h1c0, 1);
    expected = 9;
`else
    expected = 8;
`endif
    wait (t_arvalid && !t_arready && t_rvalid && !t_rready);
    repeat (2 * CYCLES) @(negedge clk);
    if (t_timed_out || !t_arvalid || t_arready || !t_rvalid || t_rready)
      $fatal(1, "CHECK_FAIL BACKPRESSURE_HOLD");
    s_rready = 1;
    repeat (4 * CYCLES) @(negedge clk);
    if (expired || reads != expected)
      $fatal(1, "CHECK_FAIL BACKPRESSURE_RESULT reads=%0d", reads);
  end
endtask
`endif

`ifdef TARGET_AXI4
task single_write(input integer index);
  begin
    s_awaddr = 'h100 + 4 * index; s_awlen = 0; s_awsize = 2; s_awburst = 1; s_awid = 2;
    s_awvalid = 1;
    fork
      begin
        @(posedge clk); while (!s_awready) @(posedge clk);
        @(negedge clk); s_awvalid = 0;
      end
      beat(index, index + 1);
    join
  end
endtask

task held_aw;
  begin
    s_rready = 1;
    fork
      single_write(0);
      read_request('h180, 16);
    join
    wait (aw_waited == CYCLES);
    @(negedge clk);
  end
endtask

task held_b_after_next_w;
  begin
    block_target = 1;
    single_write(0);
    wait (completed == 1);
    @(negedge clk);
    fork
      single_write(1);
      begin
        wait (t_wvalid && !t_wready);
        @(negedge clk); block_target = 0;
      end
    join
    wait (b_waited == CYCLES);
    @(negedge clk);
  end
endtask

task shared_response(input bit hold_read);
  integer writes, read_beats;
  begin
    if (hold_read) begin
      read_beats = 16;
      writes = 1;
      read_request('h180, read_beats);
      single_write(0);
      wait (eligible && t_rvalid && !t_rready);
    end else begin
      s_bready = 0;
      s_rready = 1;
      read_beats = 1;
      writes = 0;
      while (writes < 4 && !(t_bvalid && !t_bready)) begin
        single_write(writes);
        writes = writes + 1;
        wait (completed == writes);
        repeat (4) @(negedge clk);
      end
      read_request('h180, read_beats);
      wait (read_pending && t_bvalid && !t_bready);
    end
    repeat (2 * CYCLES) begin
      @(negedge clk);
      if (t_timed_out) $fatal(1, "CHECK_FAIL SHARED_RESPONSE_EXPIRY");
      if (hold_read && (!eligible || !t_rvalid || t_rready || t_bvalid))
        $fatal(1, "CHECK_FAIL SHARED_B_NOT_BLOCKED");
      if (!hold_read && (!read_pending || !t_bvalid || t_bready || t_rvalid))
        $fatal(1, "CHECK_FAIL SHARED_R_NOT_BLOCKED");
    end
    s_bready = 1; s_rready = 1;
    repeat (4 * CYCLES) @(negedge clk);
    if (t_timed_out || reads != read_beats || answers != writes || completed != writes)
      $fatal(1, "CHECK_FAIL SHARED_RESPONSE_RESULT reads=%0d writes=%0d", reads, answers);
  end
endtask
`endif

task write_burst;
  integer expected;
  begin
    s_awaddr = 'h100; s_awlen = 3; s_awsize = 2; s_awburst = 1; s_awid = 2; s_awvalid = 1;
    @(posedge clk); while (!s_awready) @(posedge clk);
    @(negedge clk); s_awvalid = 0;
    if (scenario == 2 || scenario == 3 || scenario == 4 || scenario == 5 || scenario == 9)
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
    if (scenario == 9) begin
      wait (waited == CYCLES - 1);
      @(negedge clk); block_target = 0;
    end
    repeat (4 * CYCLES) @(negedge clk);
    if (scenario == 2 || scenario == 4 || scenario == 5) begin
      expected = scenario == 4 ? 1 : 0;
`ifdef TARGET_AXI4
      if (scenario == 2 || scenario == 5) expected = 4;
`endif
      if (!expired || answers != COMPLETION || completed != expected)
        $fatal(1, "CHECK_FAIL HUNG_RESULT completed=%0d answers=%0d", completed, answers);
    end else if (expired || completed != 4 || answers != 1)
      $fatal(1, "CHECK_FAIL NORMAL_RESULT completed=%0d answers=%0d", completed, answers);
  end
endtask

initial begin
  if (!$value$plusargs("SCENARIO=%d", scenario)) scenario = 0;
  repeat (8) @(negedge clk);
  reset_n = 1; s_bready = 1;
  repeat (8) @(negedge clk);
  if (scenario == 6 || scenario == 7) read_hang();
`ifdef AXI_READ_BACKPRESSURE
  else if (scenario == 8) read_backpressure();
`endif
`ifdef TARGET_AXI4
  else if (scenario == 10) held_aw();
  else if (scenario == 11 || scenario == 12) shared_response(scenario == 11);
  else if (scenario == 13) held_b_after_next_w();
`endif
  else write_burst();
  $display("CHECK_PASS watchdog scenario=%0d completion=%0d first=%0d last=%0d sent_last=%0d",
           scenario, COMPLETION, first, last, sent_last);
  $finish;
end

initial begin
  repeat (512) @(negedge clk);
  $fatal(1, "CHECK_FAIL CYCLE_LIMIT");
end
