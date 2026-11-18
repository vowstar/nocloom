`timescale 1ns/1ps
module tb;
  localparam ZERO_RATE = @ZERO@;
  localparam SCHEDULER = @SCHEDULER@;
  localparam [31:0] HIT = 32'h20, MISS = 32'h2000, DATA = 32'h12345678;
  reg clk = 0, reset_n = 0;
  reg psel = 0, penable = 0;
  reg [31:0] address = 0;
  wire ready, error, target_sel, target_enable;
  wire [31:0] data, target_address;
  integer target_accesses = 0;
  always #5 clk = ~clk;

  nocloom_source_miss_fallback dut(
    .clk(clk), .reset_n(reset_n),
    .nl_i_s_psel(psel), .nl_i_s_penable(penable), .nl_i_s_paddr(address),
    .nl_i_s_pwrite(1'b0), .nl_i_s_pwdata(32'b0), .nl_i_s_pstrb(4'b0), .nl_i_s_pprot(3'b0),
    .nl_i_s_pready(ready), .nl_i_s_pslverr(error), .nl_i_s_prdata(data),
    .nl_t_t_pready(1'b1), .nl_t_t_pslverr(1'b0), .nl_t_t_prdata(DATA),
    .nl_t_t_psel(target_sel), .nl_t_t_penable(target_enable), .nl_t_t_paddr(target_address)
  );

  task fail(input string rule);
    $display("ASSERT_FAIL:SOURCE_MISS_RATE.%s", rule);
    $fatal(1);
  endtask

  always @(posedge clk) begin
    if (reset_n && target_sel && target_enable) begin
      if (target_address !== HIT) fail("ADDRESS");
      target_accesses <= target_accesses + 1;
    end
    if (reset_n && SCHEDULER && (@CHARGE@ !== (psel && penable && ready)))
      fail("COMPLETION_CHARGE");
  end

  task access(input [31:0] addr, input miss, input integer earliest, input integer limit);
    integer elapsed;
    begin
      @(negedge clk);
      address = addr;
      psel = 1;
      @(negedge clk);
      penable = 1;
      @(posedge clk);
      elapsed = 0;
      while (ready !== 1'b1 && elapsed < limit) begin
        @(posedge clk);
        elapsed = elapsed + 1;
      end
      if (ready === 1'b1) begin
        if (elapsed < earliest || (ZERO_RATE && !miss)) fail("EARLY_ADMISSION");
        if (error !== miss || data !== (miss ? 32'b0 : DATA)) fail("RESPONSE");
      end else if (!ZERO_RATE || miss) begin
        fail("MISSING_RESPONSE");
      end
      $display("ACCESS %h cycles=%0d ready=%b", addr, elapsed, ready);
      @(negedge clk);
      if (elapsed < limit || !ZERO_RATE || miss) begin
        psel = 0;
        penable = 0;
      end
    end
  endtask

  initial begin
    repeat (8) @(negedge clk);
    reset_n = 1;
    repeat (4) @(negedge clk);
    access(MISS, 1'b1, 0, 32);
    if (SCHEDULER && @COUNTER@ == 0) fail("MISSING_DEBT");
    if (!ZERO_RATE && !SCHEDULER) begin
      // Four charged bytes drain at one byte per 16 cycles. Admission opens
      // below one byte. Include the gap between accesses and response latency.
      access(MISS + 4, 1'b1, 32, 80);
    end
    if (target_accesses != 0) fail("MISS_TARGET_ACCESS");
    access(HIT, 1'b0, (ZERO_RATE || SCHEDULER) ? 0 : 16, ZERO_RATE ? 80 : 320);
    if (target_accesses != (ZERO_RATE ? 0 : 1)) fail("TARGET_ACCESS_COUNT");
    $display("CHECK_PASS source_miss_rate");
    $finish;
  end
endmodule
