`timescale 1ns/1ps
// An error logger over APB4 at PSEL, PENABLE and PREADY: a configuration source reads and writes
// the logger's bank through a service window; a data source makes errors at an observed target. The
// bank's offsets arrive as macros from the network's map.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0;
  reg [31:0] c_addr = 0, c_wdata = 0, d_addr = 0;
  reg [3:0] c_strb = 0;
  reg c_sel = 0, c_en = 0, c_wr = 0, d_sel = 0, d_en = 0, d_wr = 0;
  wire [31:0] c_rdata, d_rdata;
  wire c_ready, c_err, d_ready, d_err, fault;
  wire ram_sel, ram_en;
  wire [15:0] ram_addr;
  reg ram_err = 0;
  reg [2:0] d_prot = 0;
  localparam [31:0] BANK = 32'h00100000;
  nocloom_logged dut (.clk(clk), .reset_n(rst_n), .test(1'b0), .nl_o_errors_alarm(fault),
    .nl_i_cfg_psel(c_sel), .nl_i_cfg_penable(c_en), .nl_i_cfg_pwrite(c_wr), .nl_i_cfg_paddr(c_addr),
    .nl_i_cfg_pwdata(c_wdata), .nl_i_cfg_pstrb(c_strb), .nl_i_cfg_pprot(3'b0),
    .nl_i_cfg_pready(c_ready), .nl_i_cfg_prdata(c_rdata), .nl_i_cfg_pslverr(c_err),
    .nl_i_data_psel(d_sel), .nl_i_data_penable(d_en), .nl_i_data_pwrite(d_wr),
    .nl_i_data_paddr(d_addr), .nl_i_data_pwdata(32'b0), .nl_i_data_pstrb(4'hf),
    .nl_i_data_pprot(d_prot), .nl_i_data_pready(d_ready), .nl_i_data_prdata(d_rdata),
    .nl_i_data_pslverr(d_err), .nl_t_ram_psel(ram_sel), .nl_t_ram_penable(ram_en),
    .nl_t_ram_paddr(ram_addr), .nl_t_ram_pwrite(), .nl_t_ram_pwdata(), .nl_t_ram_pstrb(),
    .nl_t_ram_pprot(), .nl_t_ram_pready(1'b1), .nl_t_ram_prdata(32'h12345678),
    .nl_t_ram_pslverr(ram_err));
  always @(posedge clk) cycle <= cycle + 1;

  task check(input ok, input [8*24-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s cycle=%0d", what, cycle);
      end
    end
  endtask
  task csr(input w, input [31:0] offset, input [31:0] d, output [31:0] value);
    integer n;
    begin
      @(negedge clk);
      c_addr = BANK + offset; c_wr = w; c_wdata = d; c_strb = w ? 4'hf : 4'h0; c_sel = 1; c_en = 0;
      @(negedge clk);
      c_en = 1; n = 0;
      @(posedge clk);
      while (!c_ready) begin
        n = n + 1;
        if (n > 40) begin
          $display("CHECK_FAIL timeout csr");
          $finish;
        end
        @(posedge clk);
      end
      value = c_rdata;
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
    end
  endtask
  reg [31:0] v;
  task clear;
    begin
      csr(1, `RECORD_CLEAR, 1, v);
      repeat (2) @(negedge clk);
    end
  endtask
  task settle;
    repeat (12) @(negedge clk);
  endtask
  task reset_network;
    begin
      @(negedge clk);
      rst_n = 0;
      repeat (5) @(negedge clk);
      rst_n = 1;
      repeat (3) @(negedge clk);
    end
  endtask
  // Cycle of the target's access edge and of the next rise of the fault output.
  integer access_at = 0, fault_at = 0;
  reg fault_before = 0;
  always @(posedge clk) begin
    if (ram_sel && ram_en) access_at <= cycle;
    if (fault && !fault_before) fault_at <= cycle;
    fault_before <= fault;
  end
`ifdef PLAIN
  reg previous_valid = 0;
  reg check_fault = 0, expected_valid = 0, saw_clear_capture = 0;
  always @(posedge clk) begin
    if (rst_n && check_fault) check(fault === previous_valid, "fault_cycle");
    if (!rst_n) expected_valid <= 0;
    else begin
      check(`LOG_VALID === expected_valid, "clear_capture");
      expected_valid <= !`LOG_CLEAR && (`LOG_CAPTURE || expected_valid);
      if (`LOG_CLEAR && `LOG_CAPTURE) saw_clear_capture <= 1;
    end
    previous_valid <= `LOG_VALID && `LOG_ALARM;
    check_fault <= rst_n;
  end
`endif
  integer d;
  reg [31:0] kept;
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);
    csr(1, `ALARM_ENABLE, 1, v);
    data(0, 32'h40);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "clean_read");
    ram_err = 1;
`ifdef PLAIN
    data(0, 32'h40);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "first_record");
    check(fault === 1'b1, "fault_on");
    check(fault_at > access_at, "fault_after_event");
    // A record: its length and partial bit, sub-range and numbers, kept low bits.
    csr(0, `RECORD_HEAD, 0, v);
    check(v == (32'h3 << `RECORD_BYTES_MINUS_ONE_lsb | 32'h1 << `RECORD_PARTIAL_lsb), "first_head");
    csr(0, `RECORD_ROUTE, 0, v);
    check(v == 6, "first_route");
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "first_address");
    data(1, 32'h1c8);
    settle;
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "kept_first");
    csr(1, `ALARM_ENABLE, 0, v);
    repeat (3) @(negedge clk);
    check(fault === 1'b0, "fault_follows_enable");
    csr(1, `ALARM_ENABLE, 1, v);
    repeat (3) @(negedge clk);
    check(fault === 1'b1, "fault_follows_enable");
    clear;
    check(fault === 1'b0, "fault_follows_clear");
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "cleared");
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h40, "clear_keeps_record");
    // A write and a miss of this source: the miss keeps its own address and is not partial.
    data(1, 32'h1c8);
    settle;
    csr(0, `RECORD_HEAD, 0, v);
    check(v == (32'h3 << `RECORD_BYTES_MINUS_ONE_lsb | 32'h1 << `RECORD_PARTIAL_lsb
                | 32'h1 << `RECORD_WRITE_lsb), "write_opcode");
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'hc8, "write_address");
    clear;
    data(0, 32'h00090104);
    settle;
    csr(0, `RECORD_ROUTE, 0, v);
    check(v == 7, "miss_route");
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h00090104, "miss_address");
    csr(0, `RECORD_HEAD, 0, v);
    check(!v[`RECORD_PARTIAL_lsb], "miss_whole");
    clear;
    // Stall: the second error waits on the path and enters after the clear; the third is gone.
    csr(1, `HOLD_ENABLE, 1, v);
    data(0, 32'h200);
    settle;
    data(0, 32'h204);
    settle;
    data(0, 32'h208);
    settle;
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h0, "stall_first");
    clear;
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "stall_kept");
    csr(0, `RECORD_ADDRESS, 0, v);
    check(v == 32'h4, "stall_kept");
    clear;
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "stall_lost");
    csr(1, `HOLD_ENABLE, 0, v);
    // Clear wins on a simultaneous capture. The event model checks valid at each edge.
    for (d = 0; d < 8; d = d + 1) begin
      reset_network;
      fork
        csr(1, `RECORD_CLEAR, 1, v);
        begin
          repeat (d) @(negedge clk);
          data(0, 32'h40 + 4 * d);
        end
      join
      settle;
      csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
      check(v == expected_valid, "clear_capture");
      csr(0, `RECORD_ADDRESS, 0, kept);
      check(kept == 32'h40 + 4 * d, "clear_capture_record");
    end
    check(saw_clear_capture, "clear_capture_overlap");
`elsif SECURITY
    // Zeta is PPROT[0], Alpha PPROT[1] inverted, mid PPROT[2]: a record's word stands in that order,
    // and the logger takes only Alpha one and zeta zero.
    data(0, 32'h40);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "filter_accepts");
    csr(0, `RECORD_SECURITY, 0, v);
    check(v == 32'h2, "security_record");
    clear;
    d_prot = 3'b100;
    data(1, 32'h44);
    d_prot = 3'b001;
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "filter_accepts");
    csr(0, `RECORD_SECURITY, 0, v);
    check(v == 32'h6, "security_order");
    clear;
    d_prot = 3'b001;
    data(0, 32'h48);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "filter_rejects_one");
    d_prot = 3'b010;
    data(1, 32'h4c);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "filter_rejects_zero");
    // Stall does not look at the filter: behind a full slot a dropped record still waits.
    d_prot = 3'b000;
    data(0, 32'h50);
    settle;
    csr(1, `HOLD_ENABLE, 1, v);
    d_prot = 3'b010;
    data(0, 32'h54);
    settle;
    d_prot = 3'b000;
    data(0, 32'h58);
    settle;
    clear;
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "filter_stalls");
`else
    // Decode codes are ignored: they stall like any error and are dropped as the logger takes them.
    data(0, 32'h00090000);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "ignored_dropped");
    data(0, 32'h40);
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 1, "target_code_logged");
    csr(1, `HOLD_ENABLE, 1, v);
    data(0, 32'h00090000);
    settle;
    data(0, 32'h80);
    settle;
    clear;
    settle;
    csr(0, `RECORD_VALID, 0, v); v = v[`RECORD_VALID_lsb];
    check(v == 0, "ignored_stalls");
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
