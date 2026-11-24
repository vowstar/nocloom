`timescale 1ns/1ps
// Two error logs and their banks in a clock domain of their own, behind one crossing of the serial
// path each way, reached by two APB4 sources. A request word carries its log's branch across the
// crossing, and the switch past it hands the word to that log's bank. Two free-running clocks;
// +SLOW_NS sets the banks' period, and the bench can stop the banks' clock low. The banks'
// offsets and bit places arrive as macros from the map.
module tb;
  reg clk = 0, clk_slow = 0, slow_run = 1;
  real slow_ns = 33.0;
  initial begin
    if ($value$plusargs("SLOW_NS=%f", slow_ns)) begin end
    fork
      forever #5 clk = ~clk;
      forever #(slow_ns / 2) if (slow_run || clk_slow) clk_slow = ~clk_slow;
    join_none
  end
  reg rst_n = 0;
  integer failures = 0, checks = 0, took_a = 0, took_b = 0;
  reg [31:0] a_addr = 0, a_wdata = 0, b_addr = 0, b_wdata = 0, d_addr = 0;
  reg [3:0] a_strb = 0, b_strb = 0;
  reg a_sel = 0, a_en = 0, a_wr = 0, b_sel = 0, b_en = 0, b_wr = 0, d_sel = 0, d_en = 0, d_wr = 0;
  wire [31:0] a_rdata, b_rdata, d_rdata;
  wire a_ready, a_err, b_ready, b_err, d_ready, d_err, alarm0, alarm1;
  localparam [31:0] WINDOW = 32'h00100000, BANK0 = WINDOW + 32'h300, BANK1 = WINDOW + 32'h100;
  // STATE: the status bits in byte 0, the rw bits in byte 1. A write of this bench strobes
  // every byte, so it names the rw bits it keeps and clears only the status bits it names.
  localparam [31:0] STATE0 = BANK0 + `OFFSET_STATE, STATE1 = BANK1 + `OFFSET_STATE,
                    ADDRESS0 = BANK0 + `OFFSET_ADDRESS, ADDRESS1 = BANK1 + `OFFSET_ADDRESS,
                    HELD = 32'h1 << `RECORD_HELD_lsb, OVERFLOW = 32'h1 << `RECORD_OVERFLOW_lsb,
                    ALARM = 32'h1 << `ALARM_ENABLE_lsb, WAIT = 32'h1 << `WAIT_WHEN_FULL_lsb,
                    // The first word of a bank: its layout and its log's place in the window.
                    ID0 = 32'h2 << `LAYOUT_lsb, ID1 = ID0 | 32'h1 << `LOG_INDEX_lsb;
  // The status and rw bits of STATE. The record's bits beside them stay after a clear.
  localparam [31:0] CONTROL = HELD | OVERFLOW | ALARM | WAIT;
  nocloom_shared dut (.clk(clk), .clk_slow(clk_slow), .reset_n(rst_n), .test(1'b0),
    .nl_o_errors_alarm(alarm0), .nl_o_others_alarm(alarm1),
    .nl_i_cfg_psel(a_sel), .nl_i_cfg_penable(a_en), .nl_i_cfg_pwrite(a_wr), .nl_i_cfg_paddr(a_addr),
    .nl_i_cfg_pwdata(a_wdata), .nl_i_cfg_pstrb(a_strb), .nl_i_cfg_pprot(3'b0),
    .nl_i_cfg_pready(a_ready), .nl_i_cfg_prdata(a_rdata), .nl_i_cfg_pslverr(a_err),
    .nl_i_cfg2_psel(b_sel), .nl_i_cfg2_penable(b_en), .nl_i_cfg2_pwrite(b_wr),
    .nl_i_cfg2_paddr(b_addr), .nl_i_cfg2_pwdata(b_wdata), .nl_i_cfg2_pstrb(b_strb),
    .nl_i_cfg2_pprot(3'b0), .nl_i_cfg2_pready(b_ready), .nl_i_cfg2_prdata(b_rdata),
    .nl_i_cfg2_pslverr(b_err),
    .nl_i_data_psel(d_sel), .nl_i_data_penable(d_en), .nl_i_data_pwrite(d_wr),
    .nl_i_data_paddr(d_addr), .nl_i_data_pwdata(32'b0), .nl_i_data_pstrb(4'hf),
    .nl_i_data_pprot(3'b0), .nl_i_data_pready(d_ready), .nl_i_data_prdata(d_rdata),
    .nl_i_data_pslverr(d_err),
    .nl_t_ram_psel(), .nl_t_ram_penable(), .nl_t_ram_paddr(), .nl_t_ram_pwrite(),
    .nl_t_ram_pwdata(), .nl_t_ram_pstrb(), .nl_t_ram_pprot(), .nl_t_ram_pready(1'b1),
    .nl_t_ram_prdata(32'h12345678), .nl_t_ram_pslverr(1'b1),
    .nl_t_rom_psel(), .nl_t_rom_penable(), .nl_t_rom_paddr(), .nl_t_rom_pwrite(),
    .nl_t_rom_pwdata(), .nl_t_rom_pstrb(), .nl_t_rom_pprot(), .nl_t_rom_pready(1'b1),
    .nl_t_rom_prdata(32'h9abcdef0), .nl_t_rom_pslverr(1'b1));

  task check(input ok, input [8*24-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s", what);
      end
    end
  endtask
  task csr_a(input w, input [31:0] address, input [31:0] d, output [31:0] value, output failed);
    begin
      @(negedge clk);
      a_addr = address; a_wr = w; a_wdata = d; a_strb = w ? 4'hf : 4'h0; a_sel = 1; a_en = 0;
      @(negedge clk);
      a_en = 1; took_a = 0;
      @(posedge clk);
      while (!a_ready) begin
        took_a = took_a + 1;
        if (took_a > 4000) begin
          $display("CHECK_FAIL timeout csr");
          $finish;
        end
        @(posedge clk);
      end
      value = a_rdata; failed = a_err;
      @(negedge clk);
      a_sel = 0; a_en = 0; a_strb = 0;
    end
  endtask
  task csr_b(input w, input [31:0] address, input [31:0] d, output [31:0] value, output failed);
    begin
      @(negedge clk);
      b_addr = address; b_wr = w; b_wdata = d; b_strb = w ? 4'hf : 4'h0; b_sel = 1; b_en = 0;
      @(negedge clk);
      b_en = 1; took_b = 0;
      @(posedge clk);
      while (!b_ready) begin
        took_b = took_b + 1;
        if (took_b > 4000) begin
          $display("CHECK_FAIL timeout csr");
          $finish;
        end
        @(posedge clk);
      end
      value = b_rdata; failed = b_err;
      @(negedge clk);
      b_sel = 0; b_en = 0; b_strb = 0;
    end
  endtask
  task data(input [31:0] a);
    integer n;
    begin
      @(negedge clk);
      d_addr = a; d_wr = 0; d_sel = 1; d_en = 0;
      @(negedge clk);
      d_en = 1; n = 0;
      @(posedge clk);
      while (!d_ready) begin
        n = n + 1;
        if (n > 40) begin
          $display("CHECK_FAIL timeout data");
          $finish;
        end
        @(posedge clk);
      end
      @(negedge clk);
      d_sel = 0; d_en = 0;
    end
  endtask
  // Long enough for a record and for the alarm in any clock of the bench.
  task settle;
    begin
      repeat (40) @(negedge clk_slow);
      repeat (40) @(negedge clk);
    end
  endtask
  task restart;
    begin
      repeat (6) @(negedge clk_slow);
      repeat (6) @(negedge clk);
    end
  endtask
  // A word the serial path holds back: the request crossing's output waits for the switch, or
  // the answer waits at the response crossing's input.
  integer request_waits = 0, answer_words = 0;
  always @(posedge clk_slow)
    if (rst_n) begin
      if (`PATH_REQ_VALID && !`PATH_REQ_READY) request_waits = request_waits + 1;
      if (`PATH_RSP_VALID && `PATH_RSP_READY) answer_words = answer_words + 1;
    end
  reg [31:0] v, w;
  reg e, f, done_a;
  integer k, n;
  // The banks' clock stops while an access waits in the request crossing. The source waits, and
  // the access ends as it would have once the clock runs again.
  task stalled(input write, input [31:0] address, input [31:0] value, output [31:0] got);
    begin
      @(negedge clk_slow);
      slow_run = 0;
      done_a = 0;
      fork
        begin
          csr_a(write, address, value, got, e);
          done_a = 1;
        end
        begin
          repeat (80) @(negedge clk);
          check(!done_a, "stall_holds");
          slow_run = 1;
        end
      join
      check(!e, "stall_ok");
    end
  endtask
  initial begin
    repeat (5) @(negedge clk_slow);
    repeat (5) @(negedge clk);
    rst_n = 1;
    restart;
    // Each bank answers its own reads and writes through the shared crossings.
    csr_a(0, BANK0 + `OFFSET_ID, 0, v, e);
    check(v == ID0 && !e, "id_apart");
    csr_a(0, BANK1 + `OFFSET_ID, 0, v, e);
    check(v == ID1 && !e, "id_apart");
    csr_a(1, STATE0, ALARM | WAIT, v, e);
    check(!e, "write_ok");
    csr_b(1, STATE1, ALARM, w, f);
    check(!f, "write_ok");
    csr_b(0, STATE0, 0, w, f);
    check(w == (ALARM | WAIT) && !f, "control_apart");
    csr_a(0, STATE1, 0, v, e);
    check(v == ALARM && !e, "control_apart");
    // An address of no bank fails at the window; the next access still reaches its bank.
    csr_a(0, WINDOW + 32'h500, 0, v, e);
    check(e && v == 0, "outside_fails");
    csr_a(0, BANK1 + `OFFSET_ID, 0, v, e);
    check(v == ID1 && !e, "id_apart");
    // Both sources at once, one bank each, at every distance: each source gets its own answer, in
    // the order the window takes the accesses. A write to one bank and a read of the other, too.
    for (k = 0; k < 16; k = k + 1) begin
      fork
        csr_a(0, STATE0, 0, v, e);
        begin
          repeat (k) @(negedge clk);
          csr_b(0, BANK1 + `OFFSET_ID, 0, w, f);
        end
      join
      check(v == (ALARM | WAIT) && w == ID1 && !e && !f, "own_data_apart");
      fork
        csr_a(1, STATE1, k[0] ? ALARM | WAIT : 0, v, e);
        begin
          repeat (k) @(negedge clk);
          csr_b(0, BANK0 + `OFFSET_ID, 0, w, f);
        end
      join
      check(w == ID0 && !e && !f, "own_data_apart");
      csr_b(0, STATE1, 0, w, f);
      check(w == (k[0] ? ALARM | WAIT : 0) && !f, "write_apart");
      csr_b(0, STATE0, 0, w, f);
      check(w == (ALARM | WAIT) && !f, "write_apart");
    end
    // Back-to-back accesses of one source alternate between the banks.
    for (k = 0; k < 8; k = k + 1) begin
      csr_a(0, k[0] ? BANK1 + `OFFSET_ID : BANK0 + `OFFSET_ID, 0, v, e);
      check(v == (k[0] ? ID1 : ID0) && !e, "repeated_apart");
    end
    // An error at each watched target lands in its own log's bank and raises that log's alarm.
    csr_a(1, STATE0, ALARM, v, e);
    csr_a(1, STATE1, ALARM, v, e);
    data(32'h40);
    data(32'h10044);
    settle;
    csr_a(0, STATE0, 0, v, e);
    csr_b(0, STATE1, 0, w, f);
    check(v[`RECORD_HELD_lsb] && w[`RECORD_HELD_lsb], "record_apart");
    csr_a(0, ADDRESS0, 0, v, e);
    csr_b(0, ADDRESS1, 0, w, f);
    check(v == 32'h40 && w == 32'h44, "record_apart");
    check(alarm0 === 1'b1 && alarm1 === 1'b1, "alarm_apart");
    // A clear reaches its own bank only.
    csr_a(1, STATE0, HELD | ALARM, v, e);
    settle;
    csr_a(0, STATE0, 0, v, e);
    csr_b(0, STATE1, 0, w, f);
    check(!v[`RECORD_HELD_lsb] && w[`RECORD_HELD_lsb], "clear_apart");
    check(alarm0 === 1'b0 && alarm1 === 1'b1, "clear_apart");
    csr_b(1, STATE1, HELD | ALARM, w, f);
    settle;
    csr_a(0, STATE1, 0, v, e);
    check(!v[`RECORD_HELD_lsb] && alarm1 === 1'b0, "cleared");
    // The banks' clock stops with a write, a read and a clear in the request crossing.
    stalled(1, STATE1, ALARM | WAIT, v);
    csr_b(0, STATE1, 0, w, f);
    check((w & CONTROL) == (ALARM | WAIT), "stall_write");
    stalled(0, BANK1 + `OFFSET_ID, 0, v);
    check(v == ID1, "stall_read");
    data(32'h48);
    settle;
    stalled(1, STATE0, HELD | ALARM, v);
    csr_b(0, STATE0, 0, w, f);
    check(!w[`RECORD_HELD_lsb] && w[`ALARM_ENABLE_lsb], "stall_clear");
    // The banks' clock stops as the first word of a read's answer enters the response crossing:
    // the rest of the answer waits in the banks' domain and follows once the clock runs again.
    n = answer_words;
    done_a = 0;
    fork
      begin
        csr_a(0, BANK0 + `OFFSET_ID, 0, v, e);
        done_a = 1;
      end
      begin
        while (answer_words <= n) @(posedge clk_slow);
        @(negedge clk_slow);
        slow_run = 0;
        repeat (80) @(negedge clk);
        check(!done_a && answer_words < n + 5, "stall_answer_holds");
        slow_run = 1;
      end
    join
    check(v == ID0 && !e, "stall_answer");
    check(answer_words == n + 5, "answer_words");
    // The banks of one domain wait on one another: the request crossing's output never waits.
    check(request_waits == 0, "request_never_waits");
    // A reset at every distance into an access: nothing of the access is left in either domain.
    // k[0] picks a read or a write and k[1] the bank. With k[2] the banks' clock stops as the
    // reset comes and runs again while it holds. The distance is k / 8 cycles.
    for (k = 0; k < 384; k = k + 1) begin
      csr_a(1, STATE0, ALARM, v, e);
      csr_a(1, STATE1, 0, v, e);
      fork : attempt
        csr_a(!k[0], k[1] ? STATE0 : STATE1, ALARM | WAIT, v, e);
        begin
          repeat (k / 8) @(negedge clk);
          disable attempt;
        end
      join
      a_sel = 0; a_en = 0; a_strb = 0;
      if (k[2]) begin
        @(negedge clk_slow);
        slow_run = 0;
      end
      rst_n = 0;
      repeat (6) @(negedge clk);
      slow_run = 1;
      repeat (6) @(negedge clk_slow);
      rst_n = 1;
      restart;
      csr_a(0, STATE0, 0, v, e);
      csr_b(0, STATE1, 0, w, f);
      check(v == 0 && w == 0 && !e && !f, "reset_clean");
      csr_b(0, BANK1 + `OFFSET_ID, 0, w, f);
      check(w == ID1 && !f, "reset_fresh");
      csr_a(1, STATE0, WAIT, v, e);
      csr_b(0, STATE0, 0, w, f);
      check(w == WAIT && !e && !f, "reset_fresh");
      csr_a(0, BANK0 + `OFFSET_ID, 0, v, e);
      check(v == ID0 && !e, "reset_fresh");
    end
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #20000000;
    $display("CHECK_FAIL timeout");
    $finish;
  end
endmodule
