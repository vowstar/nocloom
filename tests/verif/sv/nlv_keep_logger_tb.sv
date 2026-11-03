`timescale 1ns/1ps
// An error logger keeping whole addresses and user flags, on an AXI4 target of several bursts: an
// APB4 source reads the logger's bank through a service window, an AXI4 source makes errors. The
// target answers in its own order, so a record shows which burst's fields a response found. The
// bank's offsets arrive as macros from the network's map.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0, returned_reads = 0, reads_before = 0;
  reg [31:0] c_addr = 0, c_wdata = 0;
  reg [3:0] c_strb = 0;
  reg c_sel = 0, c_en = 0, c_wr = 0;
  wire [31:0] c_rdata;
  wire c_ready, c_err, fault;
  reg ar_valid = 0, aw_valid = 0, w_valid = 0, w_last = 0, ar_lock = 0, aw_lock = 0;
  reg [47:0] ar_addr = 0, aw_addr = 0;
  reg [7:0] ar_len = 0, aw_len = 0;
  reg [2:0] ar_size = 2, aw_size = 2;
  reg [1:0] ar_burst = 1, aw_burst = 1;
  reg ar_id = 0, aw_id = 0;
  reg [43:0] ar_user = 0, aw_user = 0;
  wire ar_ready, aw_ready, w_ready, b_valid, r_valid, r_last;
  wire [1:0] b_resp, r_resp;
  wire t_ar_valid, t_aw_valid, t_w_valid, t_w_last;
  wire [47:0] t_ar_addr, t_aw_addr;
  wire [7:0] t_ar_len, t_aw_len;
  wire [1:0] t_ar_id, t_aw_id;
  reg t_b_valid = 0, t_r_valid = 0, t_r_last = 0;
  reg [1:0] t_b_id = 0, t_r_id = 0, t_b_resp = 0, t_r_resp = 0;
  wire t_r_ready, t_b_ready;
  localparam [31:0] BANK = 32'h00100000;
  nocloom_kept dut (.clk(clk), .reset_n(rst_n), .test(1'b0), .nl_o_errors_alarm(fault),
    .nl_i_cfg_psel(c_sel), .nl_i_cfg_penable(c_en), .nl_i_cfg_pwrite(c_wr), .nl_i_cfg_paddr(c_addr),
    .nl_i_cfg_pwdata(c_wdata), .nl_i_cfg_pstrb(c_strb), .nl_i_cfg_pprot(3'b0),
    .nl_i_cfg_pready(c_ready), .nl_i_cfg_prdata(c_rdata), .nl_i_cfg_pslverr(c_err),
    .nl_i_data_arvalid(ar_valid), .nl_i_data_arready(ar_ready), .nl_i_data_araddr(ar_addr),
    .nl_i_data_arlen(ar_len), .nl_i_data_arsize(ar_size), .nl_i_data_arburst(ar_burst),
    .nl_i_data_arlock(ar_lock), .nl_i_data_arcache(4'h3), .nl_i_data_arprot(3'h2),
    .nl_i_data_arid(ar_id), .nl_i_data_aruser(ar_user),
    .nl_i_data_awvalid(aw_valid), .nl_i_data_awready(aw_ready), .nl_i_data_awaddr(aw_addr),
    .nl_i_data_awlen(aw_len), .nl_i_data_awsize(aw_size), .nl_i_data_awburst(aw_burst),
    .nl_i_data_awlock(aw_lock), .nl_i_data_awcache(4'h3), .nl_i_data_awprot(3'h2),
    .nl_i_data_awid(aw_id), .nl_i_data_awuser(aw_user),
    .nl_i_data_wvalid(w_valid), .nl_i_data_wready(w_ready), .nl_i_data_wdata(32'h1),
    .nl_i_data_wstrb(4'hf), .nl_i_data_wlast(w_last),
    .nl_i_data_bvalid(b_valid), .nl_i_data_bready(1'b1), .nl_i_data_bresp(b_resp), .nl_i_data_bid(),
    .nl_i_data_rvalid(r_valid), .nl_i_data_rready(1'b1), .nl_i_data_rdata(), .nl_i_data_rresp(r_resp),
    .nl_i_data_rlast(r_last), .nl_i_data_rid(),
    .nl_t_ram_arvalid(t_ar_valid), .nl_t_ram_arready(1'b1), .nl_t_ram_araddr(t_ar_addr),
    .nl_t_ram_arlen(t_ar_len), .nl_t_ram_arsize(), .nl_t_ram_arburst(), .nl_t_ram_arlock(),
    .nl_t_ram_arcache(), .nl_t_ram_arprot(), .nl_t_ram_arid(t_ar_id), .nl_t_ram_aruser(),
    .nl_t_ram_awvalid(t_aw_valid), .nl_t_ram_awready(1'b1), .nl_t_ram_awaddr(t_aw_addr),
    .nl_t_ram_awlen(t_aw_len), .nl_t_ram_awsize(), .nl_t_ram_awburst(), .nl_t_ram_awlock(),
    .nl_t_ram_awcache(), .nl_t_ram_awprot(), .nl_t_ram_awid(t_aw_id), .nl_t_ram_awuser(),
    .nl_t_ram_wvalid(t_w_valid), .nl_t_ram_wready(1'b1), .nl_t_ram_wdata(), .nl_t_ram_wstrb(),
    .nl_t_ram_wlast(t_w_last),
    .nl_t_ram_bvalid(t_b_valid), .nl_t_ram_bready(t_b_ready), .nl_t_ram_bresp(t_b_resp),
    .nl_t_ram_bid(t_b_id),
    .nl_t_ram_rvalid(t_r_valid), .nl_t_ram_rready(t_r_ready), .nl_t_ram_rdata(32'h0),
    .nl_t_ram_rresp(t_r_resp), .nl_t_ram_rlast(t_r_last), .nl_t_ram_rid(t_r_id));
  always @(posedge clk) cycle <= cycle + 1;

  // The target holds the requests it took; the scenario answers them by hand.
  reg [1:0] ar_ids[0:63];
  reg [7:0] ar_lens[0:63];
  integer ar_count = 0;
  reg [1:0] aw_ids[0:63];
  integer aw_count = 0;
  always @(posedge clk) begin
    if (t_ar_valid) begin
      ar_ids[ar_count] <= t_ar_id;
      ar_lens[ar_count] <= t_ar_len;
      ar_count <= ar_count + 1;
    end
    if (t_aw_valid) begin
      aw_ids[aw_count] <= t_aw_id;
      aw_count <= aw_count + 1;
    end
  end

  always @(posedge clk) if (rst_n && r_valid) returned_reads <= returned_reads + 1;

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
  task idle(input integer n);
    repeat (n) @(negedge clk);
  endtask
  task send_ar(input [47:0] a, input [7:0] l, input [2:0] s, input [1:0] b, input i, input [43:0] u,
               input lock);
    integer n;
    begin
      @(negedge clk);
      ar_addr = a; ar_len = l; ar_size = s; ar_burst = b; ar_id = i; ar_user = u; ar_lock = lock;
      ar_valid = 1; n = 0;
      @(posedge clk);
      while (!ar_ready) begin
        n = n + 1;
        if (n > 200) begin
          $display("CHECK_FAIL timeout ar");
          $finish;
        end
        @(posedge clk);
      end
      @(negedge clk);
      ar_valid = 0;
    end
  endtask
  task send_aw(input [47:0] a, input [7:0] l, input i, input [43:0] u, input lock);
    integer n, k;
    begin
      @(negedge clk);
      aw_addr = a; aw_len = l; aw_id = i; aw_user = u; aw_lock = lock; aw_valid = 1;
      w_valid = 1; w_last = l == 0; n = 0; k = 0;
      while (aw_valid || w_valid) begin
        @(posedge clk);
        n = n + 1;
        if (n > 400) begin
          $display("CHECK_FAIL timeout aw");
          $finish;
        end
        begin : step
          reg aw_done, w_done;
          aw_done = aw_ready;
          w_done = w_ready;
          @(negedge clk);
          if (aw_done) aw_valid = 0;
          if (w_done) begin
            if (k == l) w_valid = 0;
            else begin
              k = k + 1;
              w_last = k == l;
            end
          end
        end
      end
    end
  endtask
  // One read word from the target: the burst of the given ID.
  task word(input [1:0] i, input [1:0] resp, input last);
    integer waited;
    begin
      @(negedge clk);
      t_r_valid = 1; t_r_id = i; t_r_resp = resp; t_r_last = last;
      @(posedge clk);
      waited = 0;
      while (!t_r_ready) begin
        waited = waited + 1;
        if (waited > 40) begin
          $display("CHECK_FAIL data_goes_on target_rready");
          $finish;
        end
        @(posedge clk);
      end
      @(negedge clk);
      t_r_valid = 0; t_r_last = 0;
    end
  endtask
  // Words given one a cycle, as a burst whose words touch.
  task words(input [1:0] i, input [1:0] resp, input integer n);
    integer k;
    begin
      @(negedge clk);
      for (k = 0; k < n; k = k + 1) begin
        t_r_valid = 1; t_r_id = i; t_r_resp = resp; t_r_last = k == n - 1;
        @(posedge clk);
        while (!t_r_ready) @(posedge clk);
        @(negedge clk);
      end
      t_r_valid = 0; t_r_last = 0;
    end
  endtask
  task answer_b(input [1:0] i, input [1:0] resp);
    begin
      @(negedge clk);
      t_b_valid = 1; t_b_id = i; t_b_resp = resp;
      @(posedge clk);
      while (!t_b_ready) @(posedge clk);
      @(negedge clk);
      t_b_valid = 0;
    end
  endtask
  reg [31:0] head, route, a_lo, a_hi, u_lo, u_hi, ignored, valid;
  task read_record(input [8*24-1:0] tag, input expect_valid);
    begin
      idle(40);
      csr(0, `RECORD_VALID, 0, valid); valid = valid[`RECORD_VALID_lsb];
      csr(0, `RECORD_HEAD, 0, head);
      csr(0, `RECORD_ROUTE, 0, route);
      csr(0, `RECORD_ADDRESS_LOW, 0, a_lo);
      csr(0, `RECORD_ADDRESS_HIGH, 0, a_hi);
      csr(0, `RECORD_USER_LOW, 0, u_lo);
      csr(0, `RECORD_USER_HIGH, 0, u_hi);
      check(valid[0] == expect_valid, tag);
    end
  endtask
  task clear;
    begin
      csr(1, `RECORD_CLEAR, 1, ignored);
      idle(3);
    end
  endtask
  function [47:0] address_of(input [31:0] hi, input [31:0] lo);
    address_of = {hi[15:0], lo};
  endfunction
  localparam [47:0] A0 = 48'h1a34_5678_0040, A1 = 48'h1b00_0000_0100, HOLE = 48'hC123_0000_0104;
  localparam [43:0] U0 = 44'h5_80_0000_0001, U1 = 44'hA_01_2345_6789;
  integer seen, aw_seen, fixed_case;
  reg [47:0] fixed_address;
  reg [31:0] v;

  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    idle(5);
    if (!`TRIMMED_RECORD) begin
    // A failing write: its whole address and every flag, and the fault.
    fork
      send_aw(A1, 1, 1'b1, U1, 1'b0);
      begin
        wait (aw_count == 1);
        idle(3);
        answer_b(aw_ids[0], 2'b10);
      end
    join
    read_record("whole_write", 1'b1);
    check(route == 32'h3a, "whole_route_write");
    check(address_of(a_hi, a_lo) == A1, "whole_address_write");
    check({u_hi[7:0], u_lo} == U1[39:0], "whole_user_write");
    csr(0, `RECORD_SECURITY, 0, v);
    check(v == 32'hA, "whole_security_write");
    check(head[`RECORD_WRITE_lsb], "opcode_write");
    clear;
    // Two reads in flight, the second answers first: the record is that second one's.
    seen = ar_count;
    send_ar(A0, 3, 2, 1, 1'b0, U0, 1'b0);
    send_ar(A1, 0, 2, 1, 1'b1, U1, 1'b0);
    wait (ar_count == seen + 2);
    idle(2);
    word(ar_ids[seen + 1], 2'b10, 1'b1);
    idle(4);
    words(ar_ids[seen], 2'b00, 4);
    read_record("newest_answers", 1'b1);
    check(route == 32'h1a, "slot_route_read");
    check(address_of(a_hi, a_lo) == A1, "slot_address");
    check({u_hi[7:0], u_lo} == U1[39:0], "slot_user");
    csr(0, `RECORD_SECURITY, 0, v);
    check(v == 32'hA, "slot_security");
    clear;
    // Two reads of one ID: the first response word is the older one's.
    seen = ar_count;
    send_ar(A1, 0, 2, 1, 1'b0, U1, 1'b0);
    send_ar(A0, 0, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 2);
    idle(2);
    word(ar_ids[seen], 2'b10, 1'b1);
    idle(2);
    word(ar_ids[seen + 1], 2'b00, 1'b1);
    read_record("oldest_of_key", 1'b1);
    check(address_of(a_hi, a_lo) == A1, "oldest_address");
    clear;
    // Contexts come free: the next read's record is its own.
    seen = ar_count;
    send_ar(A0, 0, 2, 1, 1'b1, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    word(ar_ids[seen], 2'b10, 1'b1);
    read_record("context_free", 1'b1);
    check(address_of(a_hi, a_lo) == A0, "context_free_address");
    check({u_hi[7:0], u_lo} == U0[39:0], "context_free_user");
    clear;
    // A word error after touching words stays with the head.
    seen = ar_count;
    send_ar(A0, 2, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    @(negedge clk);
    t_r_valid = 1; t_r_id = ar_ids[seen]; t_r_resp = 2'b00;
    @(negedge clk);
    t_r_resp = 2'b10;
    @(negedge clk);
    t_r_resp = 2'b00; t_r_last = 1;
    @(negedge clk);
    t_r_valid = 0; t_r_last = 0;
    read_record("touching_word", 1'b1);
    check(address_of(a_hi, a_lo) == A0, "touching_word_address");
    clear;
    // A word after a gap of two cycles heads a packet: the marker and its low address bits.
    seen = ar_count;
    send_ar(A0, 1, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    idle(2);
    word(ar_ids[seen], 2'b10, 1'b1);
    read_record("gap_word", 1'b1);
    check(a_lo[7:0] == 8'h44, "gap_low_bits");
    check(head[`RECORD_PARTIAL_lsb] && a_lo[31:8] == 0 && a_hi == 0, "gap_marker");
    clear;
    // A word after a one-cycle gap does not.
    seen = ar_count;
    send_ar(A0, 1, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    word(ar_ids[seen], 2'b10, 1'b1);
    read_record("short_gap", 1'b1);
    check(address_of(a_hi, a_lo) == A0, "short_gap_address");
    clear;
    // A packet started after a gap records the low bits of its first failed word.
    seen = ar_count;
    send_ar(A0, 3, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    word(ar_ids[seen], 2'b10, 1'b0);
    word(ar_ids[seen], 2'b00, 1'b1);
    read_record("gap_failed_word", 1'b1);
    check(a_lo[7:0] == 8'h48 && a_lo[31:8] == 0 && a_hi == 0, "gap_failed_word_address");
    check(head[`RECORD_PARTIAL_lsb], "gap_failed_word_partial");
    clear;
    // In a FIXED burst every word has the burst's address.
    seen = ar_count;
    send_ar(A0 + 48'hb0, 3, 2, 0, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    idle(2);
    word(ar_ids[seen], 2'b00, 1'b0);
    word(ar_ids[seen], 2'b10, 1'b0);
    word(ar_ids[seen], 2'b00, 1'b1);
    read_record("gap_fixed_word", 1'b1);
    check(a_lo[7:0] == 8'hf0 && a_lo[31:8] == 0 && a_hi == 0, "gap_fixed_word_address");
    clear;
    // A failed write response between the words of a clean read is the write's record.
    seen = ar_count;
    send_ar(A0, 3, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    aw_seen = aw_count;
    fork
      send_aw(A1, 0, 1'b1, U1, 1'b0);
      begin
        wait (aw_count == aw_seen + 1);
        idle(2);
        word(ar_ids[seen], 2'b00, 1'b0);
        answer_b(aw_ids[aw_seen], 2'b10);
        word(ar_ids[seen], 2'b00, 1'b0);
        word(ar_ids[seen], 2'b00, 1'b0);
        word(ar_ids[seen], 2'b00, 1'b1);
      end
    join
    read_record("write_inside_read", 1'b1);
    check(head[`RECORD_WRITE_lsb] && address_of(a_hi, a_lo) == A1, "write_inside_read");
    clear;
    // A narrow burst's bytes and a FIXED burst's words come from the source's own fields.
    seen = ar_count;
    send_ar(A0 + 1, 3, 0, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    idle(2);
    words(ar_ids[seen], 2'b10, ar_lens[seen] + 1);
    read_record("narrow", 1'b1);
    check(head[`RECORD_BYTES_MINUS_ONE_lsb +: 15] == 3, "narrow_length");
    clear;
    end
    for (fixed_case = 0; fixed_case < 4; fixed_case = fixed_case + 1) begin
      fixed_address = A0 + (fixed_case == 0 ? 1 : fixed_case == 1 ? 8 : fixed_case == 2 ? 128 : 252);
      seen = ar_count;
      send_ar(fixed_address, 3, 0, 0, 1'b0, U0, 1'b0);
      wait (ar_count == seen + 1);
      idle(2);
      words(ar_ids[seen], 2'b10, ar_lens[seen] + 1);
      read_record("fixed", 1'b1);
      check(head[`RECORD_BYTES_MINUS_ONE_lsb +: 15] == 15, "fixed_length");
      check(address_of(a_hi, a_lo) == (`TRIMMED_RECORD ? fixed_address & 48'hff : fixed_address), "fixed_address");
      check(head[`RECORD_PARTIAL_lsb] == `TRIMMED_RECORD, "fixed_partial");
      clear;
    end
    aw_burst = 0;
    seen = aw_count;
    fork
      send_aw(A0 + 12, 3, 1'b0, U0, 1'b0);
      begin
        wait (aw_count == seen + 1);
        idle(4);
        answer_b(aw_ids[seen], 2'b10);
      end
    join
    read_record("fixed_write", 1'b1);
    check(address_of(a_hi, a_lo) == (`TRIMMED_RECORD ? (A0 + 12) & 48'hff : A0 + 12), "fixed_write_address");
    check(head[`RECORD_PARTIAL_lsb] == `TRIMMED_RECORD, "fixed_write_partial");
    clear;
    aw_burst = 1;
    if (!`TRIMMED_RECORD) begin
    // A decode miss: the window's bits of its address, the source's length and lock.
    send_ar(HOLE, 3, 2, 1, 1'b0, U1, 1'b1);
    idle(20);
    read_record("miss", 1'b1);
    check(address_of(a_hi, a_lo) == (HOLE & 48'h3fff_ffff_ffff), "miss_address");
    check({u_hi[7:0], u_lo} == U1[39:0], "miss_user");
    check(head[`RECORD_BYTES_MINUS_ONE_lsb +: 15] == 15, "miss_length");
    check(head[`RECORD_EXCLUSIVE_lsb], "miss_lock");
    clear;
    // A full held logger cannot stall a target response or its return to the source.
    seen = ar_count;
    send_ar(A0, 0, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    word(ar_ids[seen], 2'b10, 1'b1);
    read_record("hold_first", 1'b1);
    csr(1, `HOLD_ENABLE, 1, ignored);
    reads_before = returned_reads;
    seen = ar_count;
    send_ar(A0 + 4, 0, 2, 1, 1'b0, U0, 1'b0);
    wait (ar_count == seen + 1);
    word(ar_ids[seen], 2'b10, 1'b1);
    idle(20);
    check(returned_reads == reads_before + 1, "data_goes_on");
    csr(1, `HOLD_ENABLE, 0, ignored);
    clear;
    end
    if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
    $finish;
  end
  initial begin
    #2000000;
    $display("CHECK_FAIL timeout global");
    $finish;
  end
endmodule
