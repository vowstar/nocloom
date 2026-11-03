`timescale 1ns/1ps
module tb;
  reg clk = 0, clk_slow = 0, reset_n = 1, qualify = 0;
`ifdef SPLIT_RESET
  reg reset_log_n = 1;
`endif
  always #5 clk = ~clk;
  always #7 clk_slow = ~clk_slow;
  reg [2:0] psel = 0, penable = 0, pwrite = 0;
  reg [31:0] paddr[0:2], pwdata[0:2];
  reg [3:0] pstrb[0:2];
  wire [2:0] pready, pslverr;
  wire [31:0] prdata[0:2];
  wire alarm, timed_out;
  wire watched_clk = @WATCH_CLOCK@;
  wire [31:0] t_paddr, t_pwdata, t_awaddr, t_araddr, t_wdata;
  wire [3:0] t_pstrb, t_wstrb, t_awlen, t_arlen;
  wire t_psel, t_penable, t_pwrite;
  wire t_awvalid, t_wvalid, t_wlast, t_bready, t_arvalid, t_rready;
  wire [1:0] t_awid, t_arid;
  reg [1:0] t_bid = 0, t_rid = 0, awid = 0;
  reg t_bvalid = 0, t_rvalid = 0, have_aw = 0, have_w = 0;
  localparam [31:0] DATA = 32'hdeadbeef, WINDOW = 32'h100000;
  localparam int DATA_SOURCE = @DATA_INDEX@, PHASE_COUNT = @PHASE_COUNT@;
  reg [31:0] expected_target_addr;
  bit expected_write;
  integer writes = 0, reads = 0, aw = 0, wd = 0, alarms = 0, cycle = 0;
  integer cfg_read_cycles = 0, cfg_write_cycles = 0;
  always @(posedge clk) cycle <= cycle + 1;
`ifdef WAIT_OVERFLOW
  reg log_running = 1;
  always @(negedge clk) qualify <= log_running;
`else
  always @(negedge clk) qualify <= cycle % 4 != 2;
`endif
  always @(posedge alarm) alarms++;

  nocloom_log_clock dut(
@PORTS@
  );

  always @(posedge watched_clk) begin
`ifdef AXI_TARGET
    if (t_arvalid) begin
      if (expected_write || t_araddr !== expected_target_addr || t_arlen !== 0)
        $fatal(1, "unexpected target AR");
      t_rid <= t_arid;
      t_rvalid <= 1;
    end else if (t_rready) t_rvalid <= 0;
    if (t_rvalid && t_rready) reads++;
    if (t_bvalid && t_bready) begin
      t_bvalid <= 0;
      writes++;
    end
    if (t_awvalid) begin
      if (!expected_write || t_awaddr !== expected_target_addr || t_awlen !== 0)
        $fatal(1, "unexpected target AW");
      awid <= t_awid;
      have_aw <= 1;
      aw++;
    end
    if (t_wvalid) begin
      if (!expected_write || !t_wlast || t_wdata !== DATA || t_wstrb !== 4'hf)
        $fatal(1, "unexpected target W");
      have_w <= 1;
      wd++;
    end
    if (have_aw && have_w && !t_bvalid) begin
      t_bid <= awid;
      t_bvalid <= 1;
      have_aw <= 0;
      have_w <= 0;
    end
`else
    if (t_psel && t_penable) begin
      if (t_paddr !== expected_target_addr || t_pwrite !== expected_write)
        $fatal(1, "unexpected target APB request");
      if (t_pwrite) begin
        if (t_pwdata !== DATA || t_pstrb !== 4'hf) $fatal(1, "unexpected APB write data");
        writes++;
      end else reads++;
    end
`endif
  end

  wire [2:0] source_clock = {@USER_CLOCK@, @SPARE_CLOCK@, @CFG_CLOCK@};
  task apb(input int source, input bit write_access, input [31:0] addr,
           input [31:0] data, input [3:0] strobe, input bit expect_error,
           output [31:0] result);
    int cycles;
    begin
      @(negedge source_clock[source]);
      #1;
      psel[source] = 1;
      pwrite[source] = write_access;
      paddr[source] = addr;
      pwdata[source] = data;
      pstrb[source] = write_access ? strobe : 0;
      @(negedge source_clock[source]);
      #1 penable[source] = 1;
      @(posedge source_clock[source]);
      cycles = 1;
      while (pready[source] !== 1'b1) begin
        @(posedge source_clock[source]);
        cycles++;
      end
      result = prdata[source];
      if (pslverr[source] !== expect_error)
        $fatal(1, "bus response addr=%h error=%b expected=%b", addr, pslverr[source], expect_error);
      if (addr == WINDOW + 4) begin
        if (write_access) cfg_write_cycles = cycles;
        else cfg_read_cycles = cycles;
      end
      @(negedge source_clock[source]);
      #1;
      psel[source] = 0;
      penable[source] = 0;
    end
  endtask

  task check_word(input [31:0] offset, input [31:0] expected);
    reg [31:0] value;
    begin
      apb(0, 0, WINDOW + offset, 0, 0, 0, value);
      if (value !== expected)
        $fatal(1, "record offset=%h value=%h expected=%h", offset, value, expected);
    end
  endtask

  task check_error(input int phase, input bit write_access, input bit miss, input [31:0] addr);
    reg [31:0] value, status;
    int alarm_count, rcount, wcount, awcount, wdcount;
    begin
      alarm_count = alarms;
      rcount = reads;
      wcount = writes;
      awcount = aw;
      wdcount = wd;
      expected_target_addr = addr - 32'h2000;
      expected_write = write_access;
      while (cycle % 8 != phase) @(negedge clk);
      apb(DATA_SOURCE, write_access, addr, DATA, 4'hf, 1, value);
      if (!write_access && value !== 32'h0)
        $fatal(1, "unexpected data in error response: %h", value);
      repeat (24) @(negedge clk);
      status = 32'h101 | (write_access ? 32'h1000 : 0) | (miss ? 32'h10 : 32'h8020);
      check_word(4, status);
      check_word(8, addr & (miss ? @MISS_MASK@ : 32'hff));
      check_word(12, 32'h40000);
      check_word(16, @PATHS@);
      if (alarm !== 1'b1 || alarms != alarm_count + 1)
        $fatal(1, "expected one alarm");
      if (reads != rcount + int'(!miss && !write_access) ||
          writes != wcount + int'(!miss && write_access))
        $fatal(1, "target response count");
`ifdef AXI_TARGET
      if (aw != awcount + int'(!miss && write_access) || wd != wdcount + int'(!miss && write_access))
        $fatal(1, "target write request count");
`endif
      apb(0, 1, WINDOW + 4, 3, 4'h1, 0, value);
      repeat (8) @(negedge clk);
      if (alarm !== 1'b0) $fatal(1, "alarm still on after clear");
      check_word(4, status & ~32'h3);
    end
  endtask

  reg [31:0] value;
`ifdef SPLIT_RESET
  time event_time = 0, log_start = 0, target_start = 0;
  time last_log_edge = 0, min_spacing = 100000;
  integer shift, target_first;
  always @(posedge @CFG_CLOCK@) begin
    last_log_edge = $time;
    if (log_start == 0) begin
      #1;
      if (@LOG_RESET@) log_start = $time - 1;
    end
  end
  always @(posedge @TARGET_RESET@) target_start = $time;
  always @(posedge clk) begin
    if (@EVENT@) begin
      if (event_time != 0) $fatal(1, "extra target error event");
      event_time = $time;
    end
  end
  always @(posedge clk) begin
    #2;
    if (last_log_edge != 0 && last_log_edge < $time - 2 && $time - 2 - last_log_edge < min_spacing)
      min_spacing = $time - 2 - last_log_edge;
  end
  initial begin
    if (!$value$plusargs("shift=%d", shift) || !$value$plusargs("target_first=%d", target_first))
      $fatal(1, "missing reset timing");
    #1 reset_n = 0; reset_log_n = 0;
    repeat (12) @(negedge clk);
    fork
      begin
        if (target_first == 0) repeat (shift) @(negedge clk);
        #1 reset_n = 1;
        repeat (12) @(negedge clk);
        expected_target_addr = 32'h34;
        expected_write = 0;
        apb(DATA_SOURCE, 0, 32'h2034, 0, 0, 1, value);
      end
      begin
        if (target_first != 0) repeat (shift) @(negedge clk);
        #1 reset_log_n = 1;
      end
    join
    repeat (24) @(negedge clk);
    if (reads != 1 || event_time <= target_start || log_start == 0)
      $fatal(1, "reset sweep did not reach one live target event");
    if (event_time >= log_start) begin
      check_word(4, 32'h8021);
      check_word(8, 32'h34);
    end else begin
      check_word(4, 0);
    end
    $display("CHECK_PASS event=%0d log_start=%0d spacing=%0d", event_time, log_start, min_spacing);
    $finish;
  end
`elsif WAIT_OVERFLOW
  initial begin
    #1 reset_n = 0;
    repeat (8) @(negedge clk);
    #1 reset_n = 1;
    repeat (8) @(negedge clk);
    apb(0, 1, WINDOW + 4, 32'h100, 4'h2, 0, value);
    log_running = 0;
    repeat (4) @(negedge clk);
    expected_target_addr = 32'h34;
    apb(DATA_SOURCE, 0, 32'h2034, 0, 0, 1, value);
    expected_target_addr = 32'h78;
    apb(DATA_SOURCE, 0, 32'h2078, 0, 0, 1, value);
    if (reads != 2 || alarm !== 0) $fatal(1, "target completions or alarm during clock stop");
    log_running = 1;
    repeat (8) @(negedge clk);
    check_word(4, 32'h8123);
    check_word(8, 32'h34);
    if (alarm !== 1 || alarms != 1) $fatal(1, "expected a single alarm after resume");
    apb(0, 1, WINDOW + 4, 3, 4'h1, 0, value);
    check_word(4, 32'h8120);
    $display("CHECK_PASS waiting record and overflow");
    $finish;
  end
`else
  initial begin
    #1 reset_n = 0;
    repeat (8) @(negedge clk);
    #1 reset_n = 1;
    repeat (8) @(negedge clk);
    apb(0, 1, WINDOW + 4, 32'h100, 4'h2, 0, value);
    check_word(4, 32'h100);
`ifdef RUN_TIMEOUT
    apb(1, 0, 0, 0, 0, 1, value);
    if (timed_out !== 1'b1) $fatal(1, "missing timeout");
`endif
    for (int phase = 0; phase < PHASE_COUNT; phase++) begin
      check_error(phase, 0, 0, 32'h2034);
      check_error(phase, 1, 0, 32'h2078);
      check_error(phase, 0, 1, 32'h4000);
      check_error(phase, 1, 1, 32'h4004);
    end
    $display("CHECK_PASS records=%0d cfg_read=%0d cfg_write=%0d", alarms,
             cfg_read_cycles, cfg_write_cycles);
    $finish;
  end
`endif
  initial begin
    #100000 $fatal(1, "timeout");
  end
endmodule
