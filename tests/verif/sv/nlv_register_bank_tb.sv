`timescale 1ns/1ps
// Register banks driven at their serial request port. The offsets and constants come from the
// register_map the fixture names (the defines the driver puts above this file). Every write byte
// lands in its own cycle, byte 3 first; the scoreboard checks each bus beat against the request.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, beats = 0, cycle = 0;

  // Request and response wires, one bench driver per bank through the selector.
  reg bank = 0; // 0 custom, 1 logger
  reg r_valid = 0, r_head = 0, r_tail = 0, s_ready = 1;
  reg [9:0] r_data = 0;
  wire [1:0] rq_ready, rs_valid, rs_head, rs_tail,
             access_write_active, access_read_active, access_complete;
  wire [9:0] rs_data[0:1];
  wire [17:0] access_word_index[0:1];
  wire [31:0] access_write_mask[0:1], access_write_value[0:1], access_read_value[0:1];
  wire [3:0] lanes[0:1];
  wire [31:0] control32;
  wire [7:0] control8;
  wire [3:0] pulse4;
  wire option_a, option_b, alarm_enable, serial_hold_when_full, record_clear_valid;
  reg [15:0] status16 = 0;
  reg record_valid = 0;
  reg record_write = 0, record_exclusive = 0, record_wrap = 0, record_address_partial = 0;
  reg [1:0] record_bytes_minus_one = 0;
  reg [2:0] record_route = 0;
  reg [31:0] record_address = 0;

  nocloom_register_bank_custom custom (.nl_clk(clk), .nl_reset_n(rst_n),
    .nl_request_valid(r_valid & ~bank), .nl_request_data(r_data), .nl_request_head(r_head),
    .nl_request_tail(r_tail), .nl_request_ready(rq_ready[0]), .nl_response_ready(s_ready),
    .nl_response_valid(rs_valid[0]), .nl_response_data(rs_data[0]), .nl_response_head(rs_head[0]),
    .nl_response_tail(rs_tail[0]), .nl_field_option_a(option_a),
    .nl_field_option_b(option_b), .nl_field_status16(status16),
    .nl_field_control32(control32), .nl_field_control8(control8), .nl_field_pulse4(pulse4),
    .nl_access_word_index(access_word_index[0]),
    .nl_access_write_active(access_write_active[0]), .nl_access_read_active(access_read_active[0]),
    .nl_access_complete(access_complete[0]), .nl_access_write_mask(access_write_mask[0]),
    .nl_access_write_value(access_write_value[0]),
    .nl_access_read_value(access_read_value[0]), .nl_bus_lanes(lanes[0]));
  nocloom_register_bank_logger logger (.nl_clk(clk), .nl_reset_n(rst_n),
    .nl_request_valid(r_valid & bank), .nl_request_data(r_data), .nl_request_head(r_head),
    .nl_request_tail(r_tail), .nl_request_ready(rq_ready[1]), .nl_response_ready(s_ready),
    .nl_response_valid(rs_valid[1]), .nl_response_data(rs_data[1]), .nl_response_head(rs_head[1]),
    .nl_response_tail(rs_tail[1]),
    .nl_field_alarm_enable(alarm_enable), .nl_field_record_valid(record_valid),
    .nl_field_record_clear_valid(record_clear_valid),
    .nl_field_record_write(record_write), .nl_field_record_exclusive(record_exclusive),
    .nl_field_record_wrap(record_wrap), .nl_field_record_address_partial(record_address_partial),
    .nl_field_record_bytes_minus_one(record_bytes_minus_one),
    .nl_field_record_route_low(record_route), .nl_field_record_address_low(record_address),
    .nl_field_serial_hold_when_full(serial_hold_when_full),
    .nl_access_word_index(access_word_index[1]),
    .nl_access_write_active(access_write_active[1]), .nl_access_read_active(access_read_active[1]),
    .nl_access_complete(access_complete[1]), .nl_access_write_mask(access_write_mask[1]),
    .nl_access_write_value(access_write_value[1]),
    .nl_access_read_value(access_read_value[1]), .nl_bus_lanes(lanes[1]));

  task check(input ok, input [8*48-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s cycle=%0d", what, cycle);
      end
    end
  endtask

  // ---- Scoreboard: the expected write beats, in order. ----
  reg [17:0] want_addr[0:15];
  reg [31:0] want_be[0:15];
  reg [7:0] want_byte[0:15];
  reg [3:0] want_lane[0:15];
  reg want_last[0:15];
  integer want_in = 0, want_out = 0;
  // Response words in arrival order, with the cycle each was taken.
  reg [9:0] got_data[0:63];
  reg got_head[0:63], got_tail[0:63];
  integer got_cycle[0:63];
  integer got_n = 0;
  integer started = 0; // cycle of the first word of the last request
  reg prev_offer = 0, prev_ready = 1;
  reg [9:0] prev_word = 0;
  integer pulses[0:3];
  integer clears = 0;
  initial begin
    pulses[0] = 0; pulses[1] = 0; pulses[2] = 0; pulses[3] = 0;
  end
  always @(posedge clk) begin
    cycle = cycle + 1;
    if (rst_n) begin
      if (r_valid && r_head && rq_ready[bank]) started = cycle;
      if (access_write_active[bank] && r_valid) begin
        beats = beats + 1;
        $display("WBEAT c=%0d addr=%h be=%h byte=%h last=%b lane=%b", cycle,
                 access_word_index[bank], access_write_mask[bank], access_write_value[bank][7:0],
                 access_complete[bank], lanes[bank]);
        if (want_out == want_in) check(0, "beat_unexpected");
        else begin
          check(access_word_index[bank] === want_addr[want_out % 16], "beat_addr");
          check(access_write_mask[bank] === want_be[want_out % 16], "beat_strobe");
          check(access_write_value[bank][7:0] === want_byte[want_out % 16], "beat_byte");
          check(lanes[bank] === want_lane[want_out % 16], "beat_lane");
          check(access_complete[bank] === want_last[want_out % 16], "beat_last");
          want_out = want_out + 1;
        end
      end
      if (rs_valid[bank] && s_ready) begin
        got_data[got_n] = rs_data[bank];
        got_head[got_n] = rs_head[bank];
        got_tail[got_n] = rs_tail[bank];
        got_cycle[got_n] = cycle;
        got_n = got_n + 1;
      end
      if (prev_offer && !prev_ready)
        check(rs_valid[bank] === 1'b1 && rs_data[bank] === prev_word, "response_hold");
      if (!bank) begin : pulse_count
        integer k;
        for (k = 0; k < 4; k = k + 1)
          if (pulse4[k]) pulses[k] = pulses[k] + 1;
      end
      if (record_clear_valid) clears = clears + 1;
      prev_offer = rs_valid[bank];
      prev_ready = s_ready;
      prev_word = rs_data[bank];
    end else begin
      prev_offer = 0;
    end
  end

  // ---- Request driver. ----
  
  task word(input [9:0] data, input head, input tail);
    begin
      @(negedge clk);
      r_valid = 1; r_data = data; r_head = head; r_tail = tail;
      @(posedge clk);
      while (!rq_ready[bank]) @(posedge clk);
    end
  endtask
  task idle;
    begin
      @(negedge clk);
      r_valid = 0; r_head = 0; r_tail = 0;
    end
  endtask
  function [9:0] route_word;
    input which;
    route_word = which ? 10'h000 : 10'h001;
  endfunction
  task header(input is_write, input [1:0] status, input [17:0] word_address, input tail);
    begin
      word(route_word(bank), 1, 0);
      word({is_write ? 4'h2 : 4'h1, 2'b00, 2'b00, status}, 0, 0);
      word({word_address[7:0], 2'b00}, 0, 0);
      word(word_address[17:8], 0, tail);
    end
  endtask
  task expect_beat(input [17:0] a, input [31:0] mask, input [7:0] value, input [3:0] lane);
    begin
      want_addr[want_in % 16] = a; want_be[want_in % 16] = mask;
      want_byte[want_in % 16] = value; want_lane[want_in % 16] = lane;
      want_last[want_in % 16] = lane[0];
      want_in = want_in + 1;
    end
  endtask
  // Bytes without a strobe keep their data bits on the wire when dirty is set.
  task write_words(input [17:0] a, input [31:0] d, input [3:0] s, input dirty, input integer upto);
    integer k;
    reg [7:0] value;
    begin
      for (k = 3; k >= upto; k = k - 1) begin
        value = d >> (8 * k);
        if (!s[k] && !dirty) value = 0;
        expect_beat(a, s[k] ? 32'hff << (8 * k) : 32'h0, value, 4'b0001 << k);
        word({s[k], 1'b0, value}, 0, k == 0);
      end
    end
  endtask
  task write(input [17:0] a, input [31:0] d, input [3:0] s);
    begin
      got_n = 0;
      header(1, 2'b01, a, 0);
      write_words(a, d, s, 0, 0);
      idle;
      settle;
      check_write_answer(a);
    end
  endtask
  task settle;
    begin
      repeat (2) @(negedge clk);
    end
  endtask
  // Response of a write: its four header words; the tail waits for the data words.
  task check_write_answer(input [17:0] a);
    begin
      check(got_n == 4, "write_answer_words");
      check(got_head[0] === 1'b1 && got_tail[3] === 1'b1, "write_answer_frame");
      check(got_data[1][1:0] === 2'b11, "response_status");
      check(got_data[2] === {a[7:0], 2'b00} && got_data[3] === a[17:8], "write_answer_address");
      check(got_cycle[0] - started == 1 && got_cycle[3] - started == 8, "write_latency");
    end
  endtask
  reg [31:0] read_value;
  task read(input [17:0] a, output [31:0] value);
    begin
      got_n = 0;
      header(0, 2'b01, a, 1);
      idle;
      repeat (8) @(negedge clk);
      check(got_n == 8, "read_answer_words");
      check(got_head[0] === 1'b1 && got_tail[7] === 1'b1, "read_answer_frame");
      check(got_data[1][1:0] === 2'b11, "response_status");
      check(got_data[4][9:8] === 2'b10 && got_data[7][9:8] === 2'b10, "read_answer_kind");
      check(got_cycle[0] - started == 1 && got_cycle[7] - started == 8, "read_latency");
      value = {got_data[4][7:0], got_data[5][7:0], got_data[6][7:0], got_data[7][7:0]};
    end
  endtask
  function [31:0] mask_of;
    input [3:0] s;
    mask_of = {{8{s[3]}}, {8{s[2]}}, {8{s[1]}}, {8{s[0]}}};
  endfunction

  reg [31:0] v, old_value, new_value, d;
  integer s, wait_cycles;
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);

    // Reset values and constants.
    read(`CUSTOM_control32, v); check(v === 0, "reset_control32");
    read(`CUSTOM_identity, v); check(v === `CUSTOM_identity_value, "read_identity");

    // Every strobe pattern on a 32-bit control field: four beats each, also with no strobe.
    old_value = 0;
    for (s = 0; s < 16; s = s + 1) begin
      d = 32'h11223344 ^ {4{s[3:0], ~s[3:0]}};
      write(`CUSTOM_control32, d, s[3:0]);
      new_value = (d & mask_of(s[3:0])) | (old_value & ~mask_of(s[3:0]));
      check(control32 === new_value, "strobe_pin");
      read(`CUSTOM_control32, v);
      check(v === new_value, "strobe_readback");
      old_value = new_value;
    end
    check(beats == 16 * 4, "strobe_beat_count");

    // Bytes without a strobe never land, whatever they carry.
    got_n = 0;
    header(1, 2'b01, `CUSTOM_control32, 0);
    write_words(`CUSTOM_control32, 32'hffffffff, 4'b0000, 1, 0);
    idle; settle;
    check(control32 === old_value, "strobe_dirty_bytes");

    // A narrow field keeps its low byte; its strobe gates it.
    write(`CUSTOM_control8, 32'hdeadbeef, 4'hf);
    read(`CUSTOM_control8, v); check(v === 32'h000000ef, "narrow_low_byte");
    write(`CUSTOM_control8, 32'h000000aa, 4'he);
    read(`CUSTOM_control8, v); check(v === 32'h000000ef, "narrow_strobe");

    // Read-only and unmapped words: no effect, no error.
    write(`CUSTOM_identity, 32'hffffffff, 4'hf);
    read(`CUSTOM_identity, v); check(v === `CUSTOM_identity_value, "constant_after_write");
    read(18'h00005, v); check(v === 0, "hole_read");
    write(18'h00005, 32'hffffffff, 4'hf);
    read(18'h00005, v); check(v === 0, "hole_write");
    read(18'h0001f, v); check(v === 0, "high_hole_read");
    status16 = 16'hbeef;
    read(`CUSTOM_status16, v); check(v === (32'hbeef << `CUSTOM_status16_lsb), "status_read");
    write(`CUSTOM_status16, 32'h0, 4'hf);
    read(`CUSTOM_status16, v); check(v === (32'hbeef << `CUSTOM_status16_lsb), "status_write");

    // A pulse field: one cycle on written ones under their strobe; reads zero.
    write(`CUSTOM_pulse4, 32'h5, 4'h1);
    check(pulses[0] == 1 && pulses[1] == 0 && pulses[2] == 1 && pulses[3] == 0, "pulse_ones");
    read(`CUSTOM_pulse4, v); check(v === 0, "pulse_reads_zero");
    write(`CUSTOM_pulse4, 32'hf, 4'h2);
    check(pulses[0] == 1 && pulses[1] == 0 && pulses[3] == 0, "pulse_strobe");

    // Another route, and a request that is no request: every word passes, nothing is written.
    got_n = 0;
    word(10'h004, 1, 0); word(10'h143, 0, 0); word(10'h000, 0, 0); word(10'h040, 0, 1);
    idle; repeat (4) @(negedge clk);
    check(got_n == 4 && got_data[1] === 10'h143 && got_tail[3] === 1'b1, "other_route_passes");
    got_n = 0;
    header(0, 2'b10, `CUSTOM_control32, 1);
    idle; repeat (4) @(negedge clk);
    check(got_n == 4 && got_data[1][1:0] === 2'b10 && got_tail[3] === 1'b1, "miss_passes");
    check(control32 === old_value, "pass_no_write");

    // A stalled write leaves its earlier bytes visible, then finishes.
    write(`CUSTOM_control32, 32'h0, 4'hf);
    got_n = 0;
    header(1, 2'b01, `CUSTOM_control32, 0);
    write_words(`CUSTOM_control32, 32'haabbccdd, 4'hf, 0, 2);
    idle;
    repeat (6) @(negedge clk);
    check(control32 === 32'haabb0000, "partial_visible");
    check(got_n == 3, "write_tail_waits");
    write_rest;
    idle; settle;
    check(control32 === 32'haabbccdd, "partial_completes");
    check(got_n == 4, "write_tail_after_data");

    // A stalled response holds its word and every byte of the read value.
    got_n = 0;
    s_ready = 0;
    word(10'h001, 1, 0);
    @(negedge clk); r_valid = 0;
    repeat (3) @(negedge clk);
    check(rq_ready[0] === 1'b0, "response_stall_blocks");
    s_ready = 1;
    word({4'h1, 2'b00, 2'b00, 2'b01}, 0, 0);
    word({8'h10, 2'b00}, 0, 0); word(10'h000, 0, 1);
    idle; s_ready = 0;
    repeat (4) @(negedge clk);
    s_ready = 1;
    wait_cycles = 0;
    while (got_n < 8 && wait_cycles < 40) begin @(negedge clk); wait_cycles = wait_cycles + 1; end
    v = {got_data[4][7:0], got_data[5][7:0], got_data[6][7:0], got_data[7][7:0]};
    check(got_n == 8 && v === 32'haabbccdd, "response_stall_data");

    // Reset during a write clears the partly written field, and the bank recovers.
    got_n = 0;
    header(1, 2'b01, `CUSTOM_control32, 0);
    write_words(`CUSTOM_control32, 32'h55667788, 4'hf, 0, 3);
    @(negedge clk);
    rst_n = 0; r_valid = 0;
    want_in = 0; want_out = 0;
    repeat (5) @(negedge clk);
    check(control32 === 32'h0, "reset_clears");
    rst_n = 1;
    repeat (5) @(negedge clk);
    read(`CUSTOM_control32, v); check(v === 0, "read_after_reset");
    write(`CUSTOM_control32, 32'h01020304, 4'hf);
    read(`CUSTOM_control32, v); check(v === 32'h01020304, "write_after_reset");
    check(want_in == want_out, "beats_all_seen");

    // The error logger's table.
    bank = 1;
    read(`LOGGER_identity_type, v);
    check(v[`LOGGER_identity_type_lsb +: 8] === `LOGGER_identity_type_value &&
          v[`LOGGER_identity_checksum_lsb +: 24] === `LOGGER_identity_checksum_value,
          "logger_identity");
    read(`LOGGER_revision_generator, v);
    check(v[`LOGGER_revision_generator_lsb +: 24] === `LOGGER_revision_generator_value &&
          v[`LOGGER_revision_reserved_lsb +: 8] === 8'h0, "logger_revision");
    write(`LOGGER_revision_reserved, 32'h5affffff, 4'hf);
    read(`LOGGER_revision_generator, v);
    check(v[`LOGGER_revision_generator_lsb +: 24] === `LOGGER_revision_generator_value &&
          v[`LOGGER_revision_reserved_lsb +: 8] === 8'h0, "logger_user_byte");
    record_valid = 1;
    read(`LOGGER_record_valid, v); check(v[`LOGGER_record_valid_lsb] === 1'b1, "record_valid_read");
    write(`LOGGER_record_valid, 32'h0, 4'hf);
    read(`LOGGER_record_valid, v); check(v[`LOGGER_record_valid_lsb] === 1'b1, "record_valid_read_only");
    clears = 0;
    write(`LOGGER_record_clear_valid, 32'h1, 4'h0); check(clears == 0, "clear_no_strobe");
    write(`LOGGER_record_clear_valid, 32'h1, 4'he); check(clears == 0, "clear_needs_byte0");
    write(`LOGGER_record_clear_valid, 32'hfffffffe, 4'hf); check(clears == 0, "clear_needs_one");
    read(`LOGGER_record_clear_valid, v); check(v[`LOGGER_record_clear_valid_lsb] === 1'b0, "clear_reads_zero");
    write(`LOGGER_record_clear_valid, 32'h1, 4'h1); check(clears == 1, "clear_one_cycle");
    write(`LOGGER_alarm_enable, 32'h1, 4'he);
    read(`LOGGER_alarm_enable, v); check(v === 0 && alarm_enable === 1'b0, "fault_needs_byte0");
    write(`LOGGER_alarm_enable, 32'h1, 4'h1);
    read(`LOGGER_alarm_enable, v); check(v === 1 && alarm_enable === 1'b1, "fault_set");
    write(`LOGGER_serial_hold_when_full, 32'h1, 4'h1);
    read(`LOGGER_serial_hold_when_full, v); check(v === 1 && serial_hold_when_full === 1'b1, "stall_set");
    record_write = 1; record_bytes_minus_one = 2'h3; record_route = 3'h5;
    record_address = 32'h12345678;
    read(`LOGGER_record_write, v);
    check(v === (32'h3 << `LOGGER_record_bytes_minus_one_lsb
                 | 32'h1 << `LOGGER_record_write_lsb),
          "record_head");
    record_write = 0; record_exclusive = 1; record_wrap = 1; record_address_partial = 1;
    read(`LOGGER_record_write, v);
    check(v === (32'h3 << `LOGGER_record_bytes_minus_one_lsb
                 | 32'h1 << `LOGGER_record_exclusive_lsb | 32'h1 << `LOGGER_record_wrap_lsb
                 | 32'h1 << `LOGGER_record_address_partial_lsb), "record_flags");
    read(`LOGGER_record_route_low, v); check(v === 32'h5, "record_route");
    read(`LOGGER_record_address_low, v); check(v === 32'h12345678, "record_address");

    if (failures == 0) $display("CHECK_PASS checks=%0d beats=%0d", checks, beats);
    else $display("BANK_FAILURES %0d", failures);
    $finish;
  end
  initial begin
    #400000;
    $display("CHECK_FAIL global_timeout");
    $finish;
  end
  task write_rest;
    begin
      // Lanes 1 and 0 of the 32-bit control word.
      expect_beat(`CUSTOM_control32, 32'h0000ff00, 8'hcc, 4'b0010);
      word({1'b1, 1'b0, 8'hcc}, 0, 0);
      expect_beat(`CUSTOM_control32, 32'h000000ff, 8'hdd, 4'b0001);
      word({1'b1, 1'b0, 8'hdd}, 0, 1);
    end
  endtask
endmodule
