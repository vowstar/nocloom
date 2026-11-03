`timescale 1ns/1ps
// A logger and its bank in a clock domain of their own, behind crossings of the serial path and of
// the record, reached by two APB4 sources. Two free-running clocks; +SLOW_NS sets the bank's period.
// The bank's offsets arrive as macros from the map.
module tb;
  reg clk = 0, clk_slow = 0;
  real slow_ns = 33.0;
  initial begin
    if ($value$plusargs("SLOW_NS=%f", slow_ns)) begin end
    fork
      forever #5 clk = ~clk;
      forever #(slow_ns / 2) clk_slow = ~clk_slow;
    join_none
  end
  reg rst_n = 0;
  integer failures = 0, checks = 0, took_a = 0, took_b = 0;
  reg [31:0] a_addr = 0, a_wdata = 0, b_addr = 0, b_wdata = 0, d_addr = 0;
  reg [3:0] a_strb = 0, b_strb = 0;
  reg a_sel = 0, a_en = 0, a_wr = 0, b_sel = 0, b_en = 0, b_wr = 0, d_sel = 0, d_en = 0, d_wr = 0;
  reg r_err = 1;
  wire [31:0] a_rdata, b_rdata, d_rdata;
  wire a_ready, a_err, b_ready, b_err, d_ready, d_err, fault;
  localparam [31:0] WINDOW = 32'h00100000, BANK = WINDOW + 32'h300;
  nocloom_crossed dut (.clk(clk), .clk_slow(clk_slow), .reset_n(rst_n), .test(1'b0),
    .nl_o_errors_alarm(fault),
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
    .nl_t_ram_prdata(32'h12345678), .nl_t_ram_pslverr(r_err));

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
        if (took_a > 2000) begin
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
        if (took_b > 2000) begin
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
    end
  endtask
  // Long enough for a record and for the fault in any clock of the bench.
  task settle;
    begin
      repeat (40) @(negedge clk_slow);
      repeat (40) @(negedge clk);
    end
  endtask
  reg [31:0] v, w;
  reg e, f;
  integer k;
  initial begin
    repeat (5) @(negedge clk_slow);
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (6) @(negedge clk_slow);
    repeat (6) @(negedge clk);
    // Reads and writes reach the bank across the crossing, once each; the other source sees them.
    csr_a(1, BANK + `ALARM_ENABLE, 1, v, e);
    check(!e, "write_ok");
    csr_b(0, BANK + `ALARM_ENABLE, 0, v, e);
    check(v == 1 && !e, "control_kept");
    // An address of no bank fails; the word stays out.
    csr_a(0, WINDOW + 32'h500, 0, v, e);
    check(e && v == 0, "outside_fails");
    // Both sources at once, each gets its own word, at every distance.
    csr_a(1, BANK + `HOLD_ENABLE, 0, v, e);
    for (k = 0; k < 12; k = k + 1) begin
      fork
        csr_a(0, BANK + `ALARM_ENABLE, 0, v, e);
        begin
          repeat (k) @(negedge clk);
          csr_b(0, BANK + `HOLD_ENABLE, 0, w, f);
        end
      join
      check(v == 1 && w == 0 && !e && !f, "own_data");
    end
    // An error is logged on the bank's side and its fault follows there.
    check(fault === 1'b0, "fault_quiet");
    data(0, 32'h40);
    settle;
    csr_a(0, BANK + `RECORD_VALID, 0, v, e); v = v[`RECORD_VALID_lsb];
    check(v == 1, "record_logged");
    check(fault === 1'b1, "fault_follows");
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h40, "record_address");
    // A later error does not replace the first.
    data(0, 32'h44);
    settle;
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h40, "kept_first");
    // A clear crosses too and the fault falls.
    csr_b(1, BANK + `RECORD_CLEAR, 1, v, e);
    settle;
    csr_b(0, BANK + `RECORD_VALID, 0, v, e); v = v[`RECORD_VALID_lsb];
    check(v == 0, "cleared");
    check(fault === 1'b0, "fault_falls");
    // Stall holds the next record behind a full slot, back through the crossing; errors pile on
    // the probe, the data path goes on, and the clear lets the held record in.
    csr_a(1, BANK + `HOLD_ENABLE, 1, v, e);
    data(0, 32'h40);
    settle;
    data(0, 32'h44);
    settle;
    for (k = 0; k < 6; k = k + 1) begin
      data(0, 32'h48 + 4 * k);
      settle;
    end
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h40, "stall_holds");
    csr_a(1, BANK + `RECORD_CLEAR, 1, v, e);
    settle;
    csr_a(0, BANK + `RECORD_VALID, 0, v, e); v = v[`RECORD_VALID_lsb];
    check(v == 1, "released");
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h44, "released_address");
    // The record behind it waited on its probe and comes next, whole.
    csr_a(1, BANK + `RECORD_CLEAR, 1, v, e);
    settle;
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h48, "third_record");
    // The next waited on its probe for the crossing's room; later ones found the probe taken.
    csr_a(1, BANK + `RECORD_CLEAR, 1, v, e);
    settle;
    csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
    check(v == 32'h4c, "fourth_record");
    if (`SERIAL_WORD_BITS == 32) begin
      // Five FIFO slots, its output slot and the prefix hold three frames.
      // The probe retains one further frame while that storage is full.
      csr_a(1, BANK + `RECORD_CLEAR, 1, v, e);
      settle;
      csr_a(0, BANK + `RECORD_ADDRESS, 0, v, e);
      check(v == 32'h50, "fifth_record");
    end
    csr_a(1, BANK + `HOLD_ENABLE, 0, v, e);
    csr_a(1, BANK + `RECORD_CLEAR, 1, v, e);
    settle;
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #40000000;
    $display("CHECK_FAIL timeout global");
    $finish;
  end
endmodule
