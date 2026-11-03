`timescale 1ns/1ps
// Several loggers on one service target over APB4: each bank has its own place in the window, its
// own controls and its own record. Reads and writes reach one bank; an address of no bank answers
// with an error, and a hole in a bank reads zero. BANKS covers two error logs.
// CHAIN and PASS_ALWAYS cover two loggers in one chain. The bank's size, offsets and bit places arrive as macros from
// the map. took counts the clock edges with PENABLE before the edge with PREADY: five for a word
// of one bank on a plain link, one more behind a switch, one more for each further bank of a
// chain, and one for an address of no bank, which the window answers by itself.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0, took = 0, data_done = 0, data_before = 0;
  reg [31:0] c_addr = 0, c_wdata = 0, d_addr = 0;
  reg [3:0] c_strb = 0;
  reg c_sel = 0, c_en = 0, c_wr = 0, d_sel = 0, d_en = 0, d_wr = 0, c_failed = 0;
  wire [31:0] c_rdata, d_rdata;
  wire c_ready, c_err, d_ready, d_err, alarm0, alarm1;
  localparam [31:0] WINDOW = 32'h00100000, BANK0 = WINDOW + 32'h300, BANK1 = WINDOW + 32'h100;
  // STATE: the status bits in byte 0, the rw bits in byte 1. A write of this bench strobes
  // every byte, so it names the rw bits it keeps and clears only the status bits it names.
  localparam [31:0] STATE0 = BANK0 + `OFFSET_STATE, STATE1 = BANK1 + `OFFSET_STATE,
                    HELD = 32'h1 << `RECORD_HELD_lsb, OVERFLOW = 32'h1 << `RECORD_OVERFLOW_lsb,
                    ALARM = 32'h1 << `ALARM_ENABLE_lsb, WAIT = 32'h1 << `WAIT_WHEN_FULL_lsb;
`ifndef BANKS
  // The classes of an error.
  localparam [3:0] CLASS_DECODE = 4'h1, CLASS_TARGET = 4'h2;
  function [3:0] class_of(input [31:0] state_word);
    class_of = state_word[`RECORD_CLASS_lsb +: 4];
  endfunction
`endif
  nocloom_banked dut (.clk(clk), .reset_n(rst_n), .test(1'b0), .nl_o_errors_alarm(alarm0),
`ifdef BANKS
    .nl_o_others_alarm(alarm1),
`else
    .nl_o_errors_alarm1(alarm1),
`endif
    .nl_i_cfg_psel(c_sel), .nl_i_cfg_penable(c_en), .nl_i_cfg_pwrite(c_wr), .nl_i_cfg_paddr(c_addr),
    .nl_i_cfg_pwdata(c_wdata), .nl_i_cfg_pstrb(c_strb), .nl_i_cfg_pprot(3'b0),
    .nl_i_cfg_pready(c_ready), .nl_i_cfg_prdata(c_rdata), .nl_i_cfg_pslverr(c_err),
    .nl_i_data_psel(d_sel), .nl_i_data_penable(d_en), .nl_i_data_pwrite(d_wr),
    .nl_i_data_paddr(d_addr), .nl_i_data_pwdata(32'b0), .nl_i_data_pstrb(4'hf),
    .nl_i_data_pprot(3'b0), .nl_i_data_pready(d_ready), .nl_i_data_prdata(d_rdata),
    .nl_i_data_pslverr(d_err),
    .nl_t_ram_psel(), .nl_t_ram_penable(), .nl_t_ram_paddr(), .nl_t_ram_pwrite(),
    .nl_t_ram_pwdata(), .nl_t_ram_pstrb(), .nl_t_ram_pprot(), .nl_t_ram_pready(1'b1),
    .nl_t_ram_prdata(32'h12345678), .nl_t_ram_pslverr(1'b1),
    .nl_t_rom_psel(), .nl_t_rom_penable(), .nl_t_rom_paddr(), .nl_t_rom_pwrite(),
    .nl_t_rom_pwdata(), .nl_t_rom_pstrb(), .nl_t_rom_pprot(), .nl_t_rom_pready(1'b1),
    .nl_t_rom_prdata(32'h12345678), .nl_t_rom_pslverr(1'b1));
  always @(posedge clk) begin
    cycle <= cycle + 1;
    if (rst_n && d_sel && d_en && d_ready) data_done <= data_done + 1;
  end

  task check(input ok, input [8*24-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s cycle=%0d", what, cycle);
      end
    end
  endtask
  task csr(input w, input [31:0] address, input [31:0] d, output [31:0] value);
    begin
      @(negedge clk);
      c_addr = address; c_wr = w; c_wdata = d; c_strb = w ? 4'hf : 4'h0; c_sel = 1; c_en = 0;
      @(negedge clk);
      c_en = 1; took = 0;
      @(posedge clk);
      while (!c_ready) begin
        took = took + 1;
        if (took > 40) begin
          $display("CHECK_FAIL timeout csr");
          $finish;
        end
        @(posedge clk);
      end
      value = c_rdata; c_failed = c_err;
      @(negedge clk);
      c_sel = 0; c_en = 0; c_strb = 0;
    end
  endtask
  task data(input w, input [31:0] a);
    integer n;
    begin
      @(negedge clk);
      d_addr = a; d_wr = w; d_sel = 1; d_en = 0;
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
      repeat (12) @(negedge clk);
    end
  endtask
  reg [31:0] v;
`ifdef PASS_ALWAYS
  // The event model of the second logger: a record that arrives takes the logger when it is
  // empty and in the cycle of a clear of its record_held.
  reg expected_held = 0;
  wire arrival_takes = `LOG1_OFFERED && (!expected_held || `LOG1_CLEAR);
  // What a window saw at the second logger: the record arrive before the clear, in its cycle or
  // after it, and the alarm fall.
  reg watching = 0, cleared = 0, arrived_early = 0, arrived_tied = 0, arrived_late = 0;
  reg alarm_before = 0, alarm_fell = 0;
  always @(posedge clk) begin
    alarm_before <= alarm1;
    if (!rst_n) expected_held <= 0;
    else begin
      check(`LOG1_VALID === expected_held, "chain_arrival_model");
      expected_held <= arrival_takes || (expected_held && !`LOG1_CLEAR);
    end
    if (!watching) begin
      cleared <= 0; arrived_early <= 0; arrived_tied <= 0; arrived_late <= 0; alarm_fell <= 0;
    end else begin
      if (alarm_before && !alarm1) alarm_fell <= 1;
      if (`LOG1_CLEAR) cleared <= 1;
      if (`LOG1_OFFERED && `LOG1_CLEAR) arrived_tied <= 1;
      else if (`LOG1_OFFERED && cleared) arrived_late <= 1;
      else if (`LOG1_OFFERED) arrived_early <= 1;
    end
  end
  integer d, ties;
  reg [31:0] kept, arriving;
`endif
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);
`ifdef CHAIN
    csr(1, STATE0, ALARM, v);
    check(took == 7, "write_cycles");
    csr(0, STATE0, 0, v);
    check(v == ALARM, "bank0_control");
    // A word of either bank takes one cycle more than through one bank: the answer of the first
    // logger's bank passes the next bank, and a frame for the next bank passes the first.
    check(took == 3, "read_cycles");
    csr(0, STATE1, 0, v);
    check(v == 0 && took == 3, "control_apart");
    // A record of the target class: the first logger logs and deletes it.
    data(0, 32'h40);
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb], "first_logs");
    check(class_of(v) == CLASS_TARGET, "first_class");
    csr(0, STATE1, 0, v);
    check(!v[`RECORD_HELD_lsb], "deleted");
    // A record of the decode class: the first logger skips it, the next takes it. The first
    // logger is full, but a record it skips is no overflow there.
    data(0, 32'h00090000);
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb], "next_record_held");
    check(class_of(v) == CLASS_DECODE, "next_record_class");
    csr(0, BANK0 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "first_record_kept");
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb] && !v[`RECORD_OVERFLOW_lsb], "skipped_no_overflow");
    // A record passed to a full last logger can hold the serial path.
    // The first logger drops the next record if the occupied path blocks it.
    // Data traffic continues.
    csr(1, STATE1, WAIT, v);
    data_before = data_done;
    data(0, 32'h00090100);
    data(0, 32'h50);
    check(data_done == data_before + 2, "data_goes_on");
    csr(1, STATE0, HELD | ALARM, v);
    repeat (12) @(negedge clk);
    csr(0, STATE0, 0, v);
    check(!v[`RECORD_HELD_lsb], "wait_holds");
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb] && !v[`RECORD_OVERFLOW_lsb], "wait_no_overflow");
    csr(1, STATE1, HELD | WAIT, v);
    repeat (12) @(negedge clk);
    csr(0, BANK1 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h00000100, "released");
    csr(1, STATE1, HELD, v);
    repeat (3) @(negedge clk);
    // Once the sequence is over the first logger deletes again.
    data(0, 32'h60);
    csr(0, STATE1, 0, v);
    check(!v[`RECORD_HELD_lsb], "deleted_again");
    check(!v[`RECORD_OVERFLOW_lsb], "wait_no_overflow");
`elsif PASS_ALWAYS
    data(0, 32'h40);
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb], "first_logs");
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb], "both_logged");
    csr(0, BANK1 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "same_record");
    // The first full bank makes the record wait while the next bank has room.
    csr(1, STATE0, WAIT, v);
    csr(1, STATE1, HELD, v);
    data(0, 32'h44);
    repeat (20) @(negedge clk);
    csr(0, STATE1, 0, v);
    check(!v[`RECORD_HELD_lsb], "chain_waits_at_first");
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb] && !v[`RECORD_OVERFLOW_lsb], "wait_no_overflow");
    csr(1, STATE0, HELD | WAIT, v);
    repeat (20) @(negedge clk);
    csr(0, BANK0 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h44, "chain_released_first");
    csr(0, BANK1 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h44, "chain_released_next");
    // A record that arrives at the second logger in the cycle of a clear of its record_held takes
    // that logger, and nothing is dropped there. Both loggers hold a write. The clear of the second
    // and a read error come at every distance, and exactly one distance puts the arrival in the
    // cycle of the clear. Before that distance the record is lost at the second logger. After it
    // the record finds that logger empty. The first logger keeps its record and counts a loss.
    ties = 0;
    for (d = 0; d < 12; d = d + 1) begin
      csr(1, STATE0, HELD | OVERFLOW, v);
      csr(1, STATE1, HELD | OVERFLOW | ALARM, v);
      data(1, 32'h80);
      arriving = 32'h40 + 4 * d;
      watching = 0;
      @(negedge clk);
      watching = 1;
      fork
        csr(1, STATE1, HELD | ALARM, v);
        begin
          repeat (d) @(negedge clk);
          data(0, arriving);
        end
      join
      csr(0, STATE1, 0, v);
      csr(0, BANK1 + `OFFSET_ADDRESS, 0, kept);
      check(arrived_early + arrived_tied + arrived_late == 1, "chain_arrival_window");
      check(v[`RECORD_HELD_lsb] == !arrived_early, "chain_arrival_held");
      check(v[`RECORD_OVERFLOW_lsb] == arrived_early, "chain_arrival_overflow");
      check(kept == (arrived_early ? 32'h80 : arriving), "chain_arrival_record");
      check(v[`RECORD_WRITE_lsb] == arrived_early, "chain_arrival_direction");
      // No record is lost without a trace.
      check(v[`RECORD_HELD_lsb] || v[`RECORD_OVERFLOW_lsb], "chain_arrival_trace");
      if (arrived_tied) begin
        ties = ties + 1;
        check(v[`RECORD_HELD_lsb] && !v[`RECORD_OVERFLOW_lsb], "chain_arrival_held");
        check(kept == arriving, "chain_arrival_record");
        // The alarm of the held record stays up through the cycle of the clear.
        check(!alarm_fell, "chain_arrival_alarm");
      end
      check(alarm1 === v[`RECORD_HELD_lsb], "chain_arrival_alarm");
      csr(0, STATE0, 0, v);
      check(v[`RECORD_HELD_lsb] && v[`RECORD_OVERFLOW_lsb], "chain_first_apart");
      csr(0, BANK0 + `OFFSET_ADDRESS, 0, v);
      check(v == 32'h80, "chain_first_apart");
    end
    check(ties == 1, "chain_arrival_overlap");
`else
    // A word goes to its bank and to no other; a read of it takes one more cycle than through one
    // bank's plain link.
    csr(1, STATE0, ALARM, v);
    check(took == 7, "write_cycles");
    csr(0, STATE0, 0, v);
    check(v == ALARM, "bank0_control");
    check(took == 3, "read_cycles");
    csr(0, STATE1, 0, v);
    check(v == 0, "control_apart");
    check(took == 3, "read_cycles");
    csr(1, STATE1, WAIT, v);
    check(took == 7, "write_cycles");
    csr(0, STATE0, 0, v);
    check(v == ALARM, "wait_apart");
    csr(0, STATE1, 0, v);
    check(v == WAIT, "bank1_control");
    csr(1, STATE1, 0, v);
    // No bank here, the first address past a bank, and an empty word of a bank.
    csr(0, WINDOW + 32'h500, 0, v);
    check(c_failed && v == 0, "outside");
    check(took == 1, "outside_cycles");
    csr(0, BANK0 + `BANK_BYTES, 0, v);
    check(c_failed && v == 0, "past_bank");
    check(took == 1, "outside_cycles");
    csr(0, BANK0 + `BANK_BYTES - 4, 0, v);
    check(!c_failed && v == 0, "hole");
    // An error stays in the error log of its target.
    data(0, 32'h40);
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb], "ram_logged");
    csr(0, STATE1, 0, v);
    check(!v[`RECORD_HELD_lsb], "ram_apart");
    check(alarm0 === 1'b1 && alarm1 === 1'b0, "alarm_apart");
    data(1, 32'h10044);
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb], "rom_logged");
    csr(0, BANK1 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h44, "rom_record");
    csr(0, BANK0 + `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "ram_record");
    // An overflow stays in the logger that dropped the record.
    data(0, 32'h48);
    csr(0, STATE0, 0, v);
    check(v[`RECORD_HELD_lsb] && v[`RECORD_OVERFLOW_lsb], "overflow_sets");
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb] && !v[`RECORD_OVERFLOW_lsb], "overflow_apart");
    // A clear reaches one logger and one status bit of it.
    csr(1, STATE0, HELD | ALARM, v);
    csr(0, STATE0, 0, v);
    check(!v[`RECORD_HELD_lsb], "cleared");
    check(v[`RECORD_OVERFLOW_lsb], "held_clear_alone");
    csr(0, STATE1, 0, v);
    check(v[`RECORD_HELD_lsb], "clear_apart");
`endif
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #2000000;
    $display("CHECK_FAIL timeout");
    $finish;
  end
endmodule
