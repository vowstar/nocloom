`timescale 1ns/1ps
// An error log in a clock domain of its own watches an APB4 target. The bench stops the log's clock
// low while the target fails, so the crossing fills and the target's sender keeps a record and
// drops later errors. With AXI_SOURCE, an AXI4 source reads bursts whose later transfer fails.
// +EXPECT gives the records that fit the crossing and the sender: depth / words + 1.
module tb;
  reg clk = 0, clk_log = 0, reset_n = 0, log_run = 1;
  always #5 clk = ~clk;
  always #10 if (log_run || clk_log) clk_log = ~clk_log;
  integer failures = 0, checks = 0, expect_n = 0;
  task check(input ok, input [8*24-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s", what);
      end
    end
  endtask

  // The APB4 source of the register window and of single errors.
  reg c_psel = 0, c_penable = 0, c_pwrite = 0;
  reg [31:0] c_paddr = 0, c_pwdata = 0;
  wire c_pready, c_pslverr, alarm;
  wire [31:0] c_prdata;
  // The APB4 target. Every transfer fails while t_all is set. Otherwise transfer t_fail after
  // t_base fails, and transfer t_hold after t_base waits until t_release.
  wire t_psel, t_penable, t_pwrite;
  wire [31:0] t_paddr;
  reg t_all = 1, t_release = 0;
  integer t_count = 0, t_base = 0, t_fail = -1, t_hold = -1;
  wire t_access = t_psel && t_penable;
  wire t_pready = !(t_access && t_count - t_base == t_hold && !t_release);
  wire t_pslverr = t_all || t_count - t_base == t_fail;
  always @(posedge clk) if (t_access && t_pready) t_count <= t_count + 1;
`ifdef AXI_SOURCE
  // The AXI4 source reads only.
  reg m_arvalid = 0;
  reg [1:0] m_arid = 0;
  reg [31:0] m_araddr = 0;
  reg [3:0] m_arlen = 0;
  wire m_arready, m_rvalid, m_rlast;
  wire [1:0] m_rresp, m_rid;
  wire [31:0] m_rdata;
`endif

  nocloom_probe_drops dut(
@PORTS@
  );

  localparam [31:0] STATE = 32'h100004, ADDRESS = 32'h100008, EXTENT = 32'h10000c;
  localparam [31:0] WAIT = 32'h200, HELD = 32'h1, OVERFLOW = 32'h2;
  task apb(input w, input [31:0] a, input [31:0] d, output [31:0] r, output e);
    integer n;
    begin
      @(negedge clk);
      c_psel = 1; c_pwrite = w; c_paddr = a; c_pwdata = d;
      @(negedge clk);
      c_penable = 1; n = 0;
      @(posedge clk);
      while (!c_pready) begin
        n = n + 1;
        if (n > 4000) begin
          $display("CHECK_FAIL timeout apb");
          $finish;
        end
        @(posedge clk);
      end
      r = c_prdata; e = c_pslverr;
      @(negedge clk);
      c_psel = 0; c_penable = 0;
    end
  endtask
  reg [31:0] v, a, x;
  reg e;
  integer k, polls;
  // A failing read of the target from the APB4 source.
  task error_read(input [31:0] at);
    begin
      apb(0, at, 0, v, e);
      check(e, "data_error");
    end
  endtask
  task settle;
    repeat (60) @(negedge clk);
  endtask
  // The records the logger takes one at a time behind wait_when_full. Each record's clear also
  // clears record_overflow, so a record's overflow bit tells its own drops.
  integer got = 0, idle = 0;
  reg [31:0] got_addr[0:31];
  reg got_flag[0:31];
  task drain;
    begin
      got = 0; idle = 0;
      while (idle < 2 && got < 32) begin
        apb(0, STATE, 0, v, e);
        if (v[0]) begin
          apb(0, ADDRESS, 0, a, e);
          got_addr[got] = a;
          got_flag[got] = v[1];
          got = got + 1;
          idle = 0;
          apb(1, STATE, WAIT | HELD | OVERFLOW, x, e);
        end else begin
          check(!v[1], "overflow_alone");
          idle = idle + 1;
        end
        settle;
      end
    end
  endtask
  // From an empty logger, with the log's clock stopped: n failing reads fill the crossing and the
  // sender keeps the last one.
  task fill(input [31:0] base);
    begin
      apb(1, STATE, WAIT | HELD | OVERFLOW, v, e);
      settle;
      @(negedge clk_log);
      log_run = 0;
      for (k = 0; k < expect_n; k = k + 1) error_read(base + 4 * k);
    end
  endtask
`ifdef AXI_SOURCE
  reg [15:0] beat_errors;
  integer beats;
  // A read burst of len + 1 transfers. beat_errors gets a bit for each beat with an error response.
  task burst(input [1:0] id, input [31:0] at, input [3:0] len);
    integer n;
    begin
      beat_errors = 0; beats = 0; n = 0;
      @(negedge clk);
      m_arvalid = 1; m_arid = id; m_araddr = at; m_arlen = len;
      @(posedge clk);
      while (!m_arready) @(posedge clk);
      @(negedge clk);
      m_arvalid = 0;
      while (beats <= len) begin
        @(posedge clk);
        n = n + 1;
        if (n > 4000) begin
          $display("CHECK_FAIL timeout burst");
          $finish;
        end
        if (m_rvalid) begin
          check(m_rid == id && m_rlast == (beats == len), "burst_beat");
          if (m_rresp != 0) beat_errors[beats] = 1;
          beats = beats + 1;
        end
      end
    end
  endtask
`endif

  initial begin
    if (!$value$plusargs("EXPECT=%d", expect_n)) $fatal(1, "missing EXPECT");
    repeat (4) @(negedge clk);
    reset_n = 1;
    repeat (16) @(negedge clk);
`ifndef ONLY_RECORD
    // The reported sequence: twelve failing reads while the log's clock stops, then polls. The
    // records come in order and the last one carries the drops. record_overflow stays set through
    // clears of record_held, and its own written one clears it.
    apb(1, STATE, WAIT, v, e);
    @(negedge clk_log);
    log_run = 0;
    for (k = 0; k < 12; k = k + 1) error_read(4 * k);
    log_run = 1;
    repeat (30) @(negedge clk);
    got = 0;
    for (polls = 0; polls < 16; polls = polls + 1) begin
      apb(0, STATE, 0, v, e);
      if (v[0]) begin
        apb(0, ADDRESS, 0, a, e);
        check(a == 4 * got, "stop_order");
        got = got + 1;
        apb(1, STATE, WAIT | HELD, x, e);
      end
      check(v[1] == (got >= expect_n), "stop_overflow");
      repeat (15) @(negedge clk);
    end
    check(got == expect_n, "stop_records");
    apb(1, STATE, WAIT | OVERFLOW, x, e);
    apb(0, STATE, 0, v, e);
    check(v[1:0] == 0, "overflow_clears");
`endif
`ifdef AXI_SOURCE
`ifndef ONLY_RECORD
    // The sender keeps a record. A burst's first transfer succeeds and its second fails: the
    // sender drops that error and marks the kept record.
    fill(32'h100);
    t_all = 0; t_base = t_count; t_fail = 1;
    burst(2, 32'h200, 3);
    check(beat_errors == 16'h2, "burst_response");
    t_all = 1;
    log_run = 1;
    settle;
    drain;
    check(got == expect_n, "burst_records");
    for (k = 0; k < got; k = k + 1) begin
      check(got_addr[k] == ((32'h100 + 4 * k) & 32'hff), "burst_order");
      check(got_flag[k] == (k == got - 1), "burst_drop_reported");
    end
    // A burst starts while the sender keeps a record and its second transfer waits. The kept
    // record leaves before that transfer fails: the sender takes the error then.
    fill(32'h140);
    t_all = 0; t_base = t_count; t_fail = 1; t_hold = 1; t_release = 0;
    fork
      burst(1, 32'h300, 3);
      begin
        while (!(t_access && t_count - t_base == 1)) @(negedge clk);
        log_run = 1;
        settle;
        drain;
        check(got == expect_n, "late_records");
        for (k = 0; k < got; k = k + 1)
          check(got_addr[k] == ((32'h140 + 4 * k) & 32'hff) && !got_flag[k], "late_order");
        t_release = 1;
      end
    join
    check(beat_errors == 16'h2, "burst_response");
    t_all = 1; t_hold = -1;
    settle;
    drain;
    check(got == 1 && got_addr[0] == 32'h0 && !got_flag[0], "late_taken");
`endif
    // A burst whose third transfer fails while the sender is free: its record.
    apb(1, STATE, WAIT | HELD | OVERFLOW, v, e);
    settle;
    t_all = 0; t_base = t_count; t_fail = 2;
    burst(3, 32'h4c0, 3);
    check(beat_errors == 16'h4, "burst_response");
    t_all = 1;
    settle;
    apb(0, STATE, 0, v, e);
    apb(0, ADDRESS, 0, a, e);
    apb(0, EXTENT, 0, x, e);
    $display("BURST_RECORD state=%h address=%h extent=%h", v, a, x);
    // Held with wait_when_full set, a read of class target from AxID 3 with its low address byte,
    // 16 bytes.
    check(v == 32'h00038221 && a == 32'hc0 && x == 32'h00100000, "burst_record");
`endif
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #3000000 $display("CHECK_FAIL timeout global");
    $finish;
  end
endmodule
