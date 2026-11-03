`timescale 1ns/1ps
// Several loggers on one service target over APB4: each bank has its own place in the window, its
// own controls and its own record. Reads and writes reach one bank; an address of no bank answers
// with an error, and a hole in a bank reads zero. BANKS covers two error logs. CHAIN and FORWARD
// cover two loggers in one chain. The bank's offsets arrive as macros from the map.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0, took = 0, data_done = 0, data_before = 0;
  reg [31:0] c_addr = 0, c_wdata = 0, d_addr = 0;
  reg [3:0] c_strb = 0;
  reg c_sel = 0, c_en = 0, c_wr = 0, d_sel = 0, d_en = 0, d_wr = 0, c_failed = 0;
  wire [31:0] c_rdata, d_rdata;
  wire c_ready, c_err, d_ready, d_err, fault0, fault1;
  localparam [31:0] WINDOW = 32'h00100000, BANK0 = WINDOW + 32'h300, BANK1 = WINDOW + 32'h100;
  nocloom_banked dut (.clk(clk), .reset_n(rst_n), .test(1'b0), .nl_o_errors_alarm(fault0),
`ifdef BANKS
    .nl_o_others_alarm(fault1),
`else
    .nl_o_errors_alarm1(fault1),
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
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);
`ifdef CHAIN
    csr(1, BANK0 + `ALARM_ENABLE, 1, v);
    csr(0, BANK0 + `ALARM_ENABLE, 0, v);
    check(v == 1, "bank0_control");
    // Past the first logger's bank a read takes one cycle more than through one bank.
    csr(0, BANK1 + `ALARM_ENABLE, 0, v);
    check(v == 0 && took == 9, "control_apart");
    // A target code: the first logger logs and deletes it.
    data(0, 32'h40);
    csr(0, BANK0 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "first_logs");
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "deleted");
    // A decode code: the first logger drops it, the next takes it.
    data(0, 32'h00090000);
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "forwarded");
    csr(0, BANK0 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "first_record_kept");
    // Behind a full last slot with stall a forwarded record holds the serial path; a record the
    // first logger deletes behind it finds the path taken, and is lost. The data path goes on.
    csr(1, BANK1 + `HOLD_ENABLE, 1, v);
    data_before = data_done;
    data(0, 32'h00090100);
    data(0, 32'h50);
    check(data_done == data_before + 2, "data_goes_on");
    csr(1, BANK0 + `RECORD_CLEAR, 1, v);
    repeat (12) @(negedge clk);
    csr(0, BANK0 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "stall_holds");
    csr(1, BANK1 + `RECORD_CLEAR, 1, v);
    repeat (12) @(negedge clk);
    csr(0, BANK1 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h00000100, "released");
    csr(1, BANK1 + `HOLD_ENABLE, 0, v);
    csr(1, BANK1 + `RECORD_CLEAR, 1, v);
    repeat (3) @(negedge clk);
    // Once the sequence is over the first logger deletes again.
    data(0, 32'h60);
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "deleted_again");
`elsif FORWARD
    data(0, 32'h40);
    csr(0, BANK0 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "first_logs");
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "both_logged");
    csr(0, BANK1 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "same_record");
    // Hold the first full bank while the next bank has room.
    csr(1, BANK0 + `HOLD_ENABLE, 1, v);
    csr(1, BANK1 + `RECORD_CLEAR, 1, v);
    data(0, 32'h44);
    repeat (20) @(negedge clk);
    csr(0, BANK1 + `RECORD_VALID, 0, v);
    check(v[`RECORD_VALID_lsb] == 0, "chain_waits_at_first");
    csr(1, BANK0 + `RECORD_CLEAR, 1, v);
    repeat (20) @(negedge clk);
    csr(0, BANK0 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h44, "chain_released_first");
    csr(0, BANK1 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h44, "chain_released_next");
`else
    // A word goes to its bank and to no other; a read of it takes one more cycle than through one
    // bank's plain link.
    csr(1, BANK0 + `ALARM_ENABLE, 1, v);
    csr(0, BANK0 + `ALARM_ENABLE, 0, v);
    check(v == 1, "bank0_control");
    check(took == 9, "read_cycles");
    csr(0, BANK1 + `ALARM_ENABLE, 0, v);
    check(v == 0, "control_apart");
    csr(1, BANK1 + `HOLD_ENABLE, 1, v);
    csr(0, BANK0 + `HOLD_ENABLE, 0, v);
    check(v == 0, "stall_apart");
    csr(0, BANK1 + `HOLD_ENABLE, 0, v);
    check(v == 1, "bank1_control");
    csr(1, BANK1 + `HOLD_ENABLE, 0, v);
    // No bank here, and an empty word of a bank.
    csr(0, WINDOW + 32'h500, 0, v);
    check(c_failed && v == 0, "outside");
    csr(0, BANK0 + 32'hfc, 0, v);
    check(!c_failed && v == 0, "hole");
    // An error stays in the error log of its target.
    data(0, 32'h40);
    csr(0, BANK0 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "ram_logged");
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "ram_apart");
    check(fault0 === 1'b1 && fault1 === 1'b0, "fault_apart");
    data(1, 32'h10044);
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "rom_logged");
    csr(0, BANK1 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h44, "rom_record");
    csr(0, BANK0 + `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "ram_record");
    // A clear reaches one logger.
    csr(1, BANK0 + `RECORD_CLEAR, 1, v);
    csr(0, BANK0 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "cleared");
    csr(0, BANK1 + `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "clear_apart");
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
