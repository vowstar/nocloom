`timescale 1ns/1ps
// Service target over APB4 at PSEL, PENABLE and PREADY. SINGLE: one source and a data path beside
// it. TWO: two sources through forward pipes, so both hold a request at once. Offsets are those of
// the error logger's bank through the service window; the beat counts run from PENABLE.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0;
`ifdef TWO
  localparam N = 2;
`else
  localparam N = 1;
`endif
  reg [31:0] addr[0:N-1], wdata[0:N-1];
  reg [3:0] strb[0:N-1];
  reg sel[0:N-1], en[0:N-1], wr[0:N-1];
  wire [31:0] rdata[0:N-1];
  wire ready[0:N-1], err[0:N-1];
  localparam [31:0] BANK = 32'h00100000, CONTROL = BANK + `OFFSET_CONTROL, HOLD = BANK + `OFFSET_HOLD,
                    STATUS = BANK + `OFFSET_STATUS, RECORD_HEAD = BANK + `OFFSET_RECORD_HEAD,
                    HOLE = BANK + 252, OUTSIDE = BANK + 256;
`ifdef TWO
  nocloom_service dut (.clk(clk), .reset_n(rst_n), .test(1'b0),
    .nl_i_m0_psel(sel[0]), .nl_i_m0_penable(en[0]), .nl_i_m0_pwrite(wr[0]), .nl_i_m0_paddr(addr[0]),
    .nl_i_m0_pwdata(wdata[0]), .nl_i_m0_pstrb(strb[0]), .nl_i_m0_pprot(3'b0),
    .nl_i_m0_pready(ready[0]), .nl_i_m0_prdata(rdata[0]), .nl_i_m0_pslverr(err[0]),
    .nl_i_m1_psel(sel[1]), .nl_i_m1_penable(en[1]), .nl_i_m1_pwrite(wr[1]), .nl_i_m1_paddr(addr[1]),
    .nl_i_m1_pwdata(wdata[1]), .nl_i_m1_pstrb(strb[1]), .nl_i_m1_pprot(3'b0),
    .nl_i_m1_pready(ready[1]), .nl_i_m1_prdata(rdata[1]), .nl_i_m1_pslverr(err[1]));
`else
  reg data_sel = 0, data_en = 0;
  reg [31:0] data_addr = 0;
  wire data_ready, data_err;
  wire [31:0] data_rdata;
  wire ram_sel, ram_en;
  wire [15:0] ram_addr;
  nocloom_service dut (.clk(clk), .reset_n(rst_n), .test(1'b0),
    .nl_i_m0_psel(sel[0]), .nl_i_m0_penable(en[0]), .nl_i_m0_pwrite(wr[0]), .nl_i_m0_paddr(addr[0]),
    .nl_i_m0_pwdata(wdata[0]), .nl_i_m0_pstrb(strb[0]), .nl_i_m0_pprot(3'b0),
    .nl_i_m0_pready(ready[0]), .nl_i_m0_prdata(rdata[0]), .nl_i_m0_pslverr(err[0]),
    .nl_i_data_psel(data_sel), .nl_i_data_penable(data_en), .nl_i_data_pwrite(1'b0),
    .nl_i_data_paddr(data_addr), .nl_i_data_pwdata(32'b0), .nl_i_data_pstrb(4'b0),
    .nl_i_data_pprot(3'b0), .nl_i_data_pready(data_ready), .nl_i_data_prdata(data_rdata),
    .nl_i_data_pslverr(data_err), .nl_t_ram_psel(ram_sel), .nl_t_ram_penable(ram_en),
    .nl_t_ram_paddr(ram_addr), .nl_t_ram_pwrite(), .nl_t_ram_pwdata(), .nl_t_ram_pstrb(),
    .nl_t_ram_pprot(), .nl_t_ram_pready(1'b1), .nl_t_ram_prdata(32'h12345678),
    .nl_t_ram_pslverr(1'b0));
`endif
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
  // One transfer of source m: SETUP, ACCESS, then the edge with PREADY; beats counts from PENABLE.
  task automatic xact(input integer m, input w, input [31:0] a, input [31:0] d, input [3:0] s,
            output [31:0] value, output failed, output integer beats);
    begin
      @(negedge clk);
      addr[m] = a; wr[m] = w; wdata[m] = d; strb[m] = w ? s : 4'h0; sel[m] = 1; en[m] = 0;
      @(negedge clk);
      en[m] = 1;
      beats = 0;
      @(posedge clk);
      beats = 1;
      while (!ready[m]) begin
        @(posedge clk);
        beats = beats + 1;
        if (beats > 2000) begin
          $display("CHECK_FAIL timeout source=%0d", m);
          $finish;
        end
      end
      value = rdata[m];
      failed = err[m];
      @(negedge clk);
      sel[m] = 0; en[m] = 0; strb[m] = 0;
    end
  endtask
  integer b, k;
  reg [31:0] v;
  reg f;
  task read_is(input [31:0] a, input [31:0] expected);
    begin
      xact(0, 0, a, 0, 0, v, f, b);
      check(b == 9, "read_beats");
      check(!f, "read_ok");
      check(v === expected, "read_value");
      if (v !== expected) $display("READ a=%h got=%h want=%h", a, v, expected);
    end
  endtask
  task write_ok(input [31:0] a, input [31:0] d, input [3:0] s);
    begin
      xact(0, 1, a, d, s, v, f, b);
      check(b == 9, "write_beats");
      check(!f, "write_ok");
    end
  endtask
`ifdef TWO
  reg [31:0] seen[0:1];
  reg seen_err[0:1];
  integer done[0:1];
  reg both_waiting = 0;
  always @(posedge clk)
    if (rst_n && sel[0] && en[0] && !ready[0] && sel[1] && en[1] && !ready[1])
      both_waiting <= 1;
  task automatic reader(input integer m, input integer count);
    reg [31:0] value;
    reg failed;
    integer beats, n;
    begin
      for (n = 0; n < count; n = n + 1) begin
        xact(m, 0, m ? HOLD : CONTROL, 0, 0, value, failed, beats);
        check(value === (m ? 32'h1 : 32'h0), "owner_data");
        check(!failed, "owner_ok");
        done[m] = done[m] + 1;
      end
    end
  endtask
`endif
  initial begin
    for (k = 0; k < N; k = k + 1) begin
      addr[k] = 0; wdata[k] = 0; strb[k] = 0; sel[k] = 0; en[k] = 0; wr[k] = 0;
    end
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);
`ifdef TWO
    xact(0, 1, HOLD, 32'h1, 4'hf, v, f, b);
    check(!f, "write_ok");
    done[0] = 0; done[1] = 0;
    fork
      reader(0, 8);
      reader(1, 8);
    join
    check(both_waiting, "both_pending");
    check(done[0] == 8 && done[1] == 8, "owner_count");
`else
    read_is(CONTROL, 32'h0);
    read_is(STATUS, 32'h0);
    read_is(HOLE, 32'h0);
    read_is(RECORD_HEAD, 32'h0);
    // The identity's type code stands in its top byte.
    xact(0, 0, BANK + `OFFSET_IDENTITY, 0, 0, v, f, b);
    check(v[31:24] === 8'h01, "read_value");
    // A write lands in the fields under its strobes and answers once.
    write_ok(CONTROL, 32'h1, 4'h1);
    read_is(CONTROL, 32'h1);
    check(v === 32'h1, "write_stored");
    write_ok(CONTROL, 32'h0, 4'he);
    read_is(CONTROL, 32'h1);
    check(v === 32'h1, "write_stored");
    write_ok(CONTROL, 32'h0, 4'hf);
    read_is(CONTROL, 32'h0);
    check(v === 32'h0, "write_stored");
    // Status and holes ignore writes without an error.
    write_ok(STATUS, 32'hfffffffe, 4'hf);
    read_is(STATUS, 32'h0);
    write_ok(HOLE, 32'hffffffff, 4'hf);
    read_is(HOLE, 32'h0);
    // Outside the bank: an error after five beats, nothing changes.
    xact(0, 0, OUTSIDE, 0, 0, v, f, b);
    check(b == 5, "outside_beats");
    check(f === 1'b1, "outside_error");
    check(v === 32'h0, "outside_zero");
    xact(0, 1, OUTSIDE + 8, 32'h1, 4'hf, v, f, b);
    check(b == 5, "outside_beats");
    check(f === 1'b1, "outside_error");
    read_is(CONTROL, 32'h0);
    // A decode miss of the source does not reach the bank.
    xact(0, 0, 32'h00300000, 0, 0, v, f, b);
    check(b == 3, "miss_beats");
    check(f === 1'b1, "miss_error");
    xact(0, 1, 32'h00000040, 32'h1, 4'hf, v, f, b);
    check(b == 3, "miss_beats");
    check(f === 1'b1, "miss_error");
    read_is(CONTROL, 32'h0);
    // The data path beside a read keeps its own beats.
    fork
      read_is(CONTROL, 32'h0);
      begin
        @(negedge clk);
        data_addr = 32'h40; data_sel = 1;
        @(negedge clk);
        data_en = 1;
        @(posedge clk);
        k = 1;
        while (!data_ready && k < 100) begin
          @(posedge clk);
          k = k + 1;
        end
        check(k == 4, "data_beats");
        check(data_rdata === 32'h12345678 && !data_err, "data_value");
        @(negedge clk);
        data_sel = 0; data_en = 0;
      end
    join
`endif
    repeat (4) @(negedge clk);
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #5000000;
    $display("CHECK_FAIL timeout");
    $finish;
  end
endmodule
