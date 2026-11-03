`timescale 1ns/1ps
// Register banks driven at their serial request port with frames of nine-bit words. A frame starts
// with a header, the bank's key above the index of the register. A header alone asks for a read;
// four data words after it, a strobe above a byte, byte 3 first, make a write. The tail mark stands
// beside the last word, and the word after it is a header. The keys, offsets and fixed values come
// from the register_map the fixture names (the defines the driver puts above this file). Every
// write byte lands in its own cycle; the scoreboard checks each bus beat against the request and
// each answer against its frame.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, beats = 0, cycle = 0;

  // Request and response wires, one bench driver per bank through the selector.
  reg bank = 0; // 0 custom, 1 logger
  reg r_valid = 0, r_tail = 0, s_ready = 1;
  reg [8:0] r_data = 0;
  wire [1:0] rq_ready, rs_valid, rs_tail,
             access_write_active, access_read_active, access_complete;
  wire [8:0] rs_data[0:1];
  wire [3:0] access_word_index[0:1];
  wire [31:0] access_write_mask[0:1], access_write_value[0:1], access_read_value[0:1];
  wire [3:0] lanes[0:1];
  wire [31:0] rw32;
  wire [7:0] rw8;
  wire [3:0] rw1c4_clear;
  wire alarm_enable, wait_when_full, record_held_clear, record_overflow_clear;
  reg [15:0] ro16 = 0;
  reg [3:0] rw1c4 = 0;
  reg record_held = 0, record_overflow = 0;
  reg record_write = 0, record_exclusive = 0, record_wrap = 0, record_address_partial = 0;
  reg [2:0] record_bytes = 0;
  reg record_source_path = 0, record_target_path = 0, record_region_index = 0;
  reg [31:0] record_address = 0;

  nocloom_register_bank_custom custom (.nl_clk(clk), .nl_reset_n(rst_n),
    .nl_request_valid(r_valid & ~bank), .nl_request_data(r_data),
    .nl_request_tail(r_tail), .nl_request_ready(rq_ready[0]), .nl_response_ready(s_ready),
    .nl_response_valid(rs_valid[0]), .nl_response_data(rs_data[0]),
    .nl_response_tail(rs_tail[0]), .nl_field_ro16(ro16),
    .nl_field_rw32(rw32), .nl_field_rw8(rw8), .nl_field_rw1c4(rw1c4),
    .nl_field_rw1c4_clear(rw1c4_clear),
    .nl_access_word_index(access_word_index[0]),
    .nl_access_write_active(access_write_active[0]), .nl_access_read_active(access_read_active[0]),
    .nl_access_complete(access_complete[0]), .nl_access_write_mask(access_write_mask[0]),
    .nl_access_write_value(access_write_value[0]),
    .nl_access_read_value(access_read_value[0]), .nl_bus_lanes(lanes[0]));
  nocloom_register_bank_logger logger (.nl_clk(clk), .nl_reset_n(rst_n),
    .nl_request_valid(r_valid & bank), .nl_request_data(r_data),
    .nl_request_tail(r_tail), .nl_request_ready(rq_ready[1]), .nl_response_ready(s_ready),
    .nl_response_valid(rs_valid[1]), .nl_response_data(rs_data[1]),
    .nl_response_tail(rs_tail[1]),
    .nl_field_alarm_enable(alarm_enable), .nl_field_wait_when_full(wait_when_full),
    .nl_field_record_held(record_held), .nl_field_record_held_clear(record_held_clear),
    .nl_field_record_overflow(record_overflow),
    .nl_field_record_overflow_clear(record_overflow_clear),
    .nl_field_record_write(record_write), .nl_field_record_exclusive(record_exclusive),
    .nl_field_record_wrap(record_wrap), .nl_field_record_address_partial(record_address_partial),
    .nl_field_record_bytes(record_bytes), .nl_field_record_source_path(record_source_path),
    .nl_field_record_target_path(record_target_path),
    .nl_field_record_region_index(record_region_index),
    .nl_field_record_address_low(record_address),
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
  reg [3:0] want_addr[0:15];
  reg [31:0] want_be[0:15];
  reg [7:0] want_byte[0:15];
  reg [3:0] want_lane[0:15];
  reg want_last[0:15];
  integer want_in = 0, want_out = 0;
  // Request words and answer words in order, with the cycle each was taken.
  reg [8:0] sent_data[0:63], got_data[0:63];
  reg sent_tail[0:63], got_tail[0:63];
  integer sent_cycle[0:63], got_cycle[0:63];
  integer sent_n = 0, got_n = 0;
  // The cycle that took the header of the last frame, and those of a write's first and last byte.
  integer started = 0, first_beat = 0, last_beat = 0;
  reg in_frame = 0;
  reg prev_offer = 0, prev_ready = 1;
  reg [8:0] prev_word = 0;
  // Cycles each clear output stood at one.
  integer pulses[0:3];
  integer clears = 0, overflow_clears = 0;
  initial begin
    pulses[0] = 0; pulses[1] = 0; pulses[2] = 0; pulses[3] = 0;
  end
  always @(posedge clk) begin
    cycle = cycle + 1;
    if (rst_n) begin
      if (r_valid && rq_ready[bank]) begin
        if (!in_frame) started = cycle;
        in_frame = !r_tail;
        sent_data[sent_n % 64] = r_data;
        sent_tail[sent_n % 64] = r_tail;
        sent_cycle[sent_n % 64] = cycle;
        sent_n = sent_n + 1;
      end
      if (access_write_active[bank]) check(rs_valid[bank] === 1'b0, "write_answer_quiet");
      if (access_write_active[bank] && r_valid) begin
        beats = beats + 1;
        if (lanes[bank][3]) first_beat = cycle;
        last_beat = cycle;
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
        got_data[got_n % 64] = rs_data[bank];
        got_tail[got_n % 64] = rs_tail[bank];
        got_cycle[got_n % 64] = cycle;
        got_n = got_n + 1;
      end
      if (prev_offer && !prev_ready)
        check(rs_valid[bank] === 1'b1 && rs_data[bank] === prev_word, "response_hold");
      if (!bank) begin : pulse_count
        integer k;
        for (k = 0; k < 4; k = k + 1)
          if (rw1c4_clear[k]) pulses[k] = pulses[k] + 1;
      end
      if (record_held_clear) clears = clears + 1;
      if (record_overflow_clear) overflow_clears = overflow_clears + 1;
      prev_offer = rs_valid[bank];
      prev_ready = s_ready;
      prev_word = rs_data[bank];
    end else begin
      prev_offer = 0;
      in_frame = 0;
    end
  end

  // ---- Request driver. ----
  task word(input [8:0] data, input tail);
    begin
      @(negedge clk);
      r_valid = 1; r_data = data; r_tail = tail;
      @(posedge clk);
      while (!rq_ready[bank]) @(posedge clk);
    end
  endtask
  task idle;
    begin
      @(negedge clk);
      r_valid = 0; r_tail = 0;
    end
  endtask
  // Forgets the words of the frames before.
  task fresh;
    begin
      sent_n = 0; got_n = 0;
    end
  endtask
  function [4:0] key_of;
    input which;
    key_of = which ? `LOGGER_KEY : `CUSTOM_KEY;
  endfunction
  // The header of a frame for the bank in use.
  function [8:0] header;
    input [3:0] index;
    header = {key_of(bank), index};
  endfunction
  task expect_beat(input [3:0] a, input [31:0] mask, input [7:0] value, input [3:0] lane);
    begin
      want_addr[want_in % 16] = a; want_be[want_in % 16] = mask;
      want_byte[want_in % 16] = value; want_lane[want_in % 16] = lane;
      want_last[want_in % 16] = lane[0];
      want_in = want_in + 1;
    end
  endtask
  // Bytes without a strobe keep their data bits on the wire when dirty is set.
  task write_words(input [3:0] a, input [31:0] d, input [3:0] s, input dirty, input integer upto);
    integer k;
    reg [7:0] value;
    begin
      for (k = 3; k >= upto; k = k - 1) begin
        value = d >> (8 * k);
        if (!s[k] && !dirty) value = 0;
        expect_beat(a, s[k] ? 32'hff << (8 * k) : 32'h0, value, 4'b0001 << k);
        word({s[k], value}, k == 0);
      end
    end
  endtask
  reg write_shown = 0, read_shown = 0;
  task write(input [3:0] a, input [31:0] d, input [3:0] s);
    begin
      fresh;
      word(header(a), 0);
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
  // Answer of a write: its header alone, as it came, with the tail mark. With the header taken in
  // cycle t the bytes land in cycles t+1 to t+4 and the answer leaves in cycle t+5.
  task check_write_answer(input [3:0] a);
    begin
      check(got_n == 1, "write_answer_words");
      check(got_tail[0] === 1'b1, "write_answer_frame");
      check(got_data[0] === header(a), "write_answer_header");
      check(first_beat - started == 1 && last_beat - started == 4, "write_data_cycles");
      check(got_cycle[0] - started == 5, "write_latency");
      if (!write_shown)
        $display("LATENCY write data=+%0d..+%0d answer=+%0d", first_beat - started,
                 last_beat - started, got_cycle[0] - started);
      write_shown = 1;
    end
  endtask
  // Answer of a read: five words, the header as it came, then the four bytes, byte 3 first, each
  // under a zero bit 8, the tail mark on the last. With the header taken in cycle t the words leave
  // in cycles t+1 to t+5.
  reg [31:0] read_value;
  task read(input [3:0] a, output [31:0] value);
    begin
      fresh;
      word(header(a), 1);
      idle;
      repeat (6) @(negedge clk);
      check(got_n == 5, "read_answer_words");
      check(got_tail[0] === 1'b0 && got_tail[1] === 1'b0 && got_tail[2] === 1'b0
            && got_tail[3] === 1'b0 && got_tail[4] === 1'b1, "read_answer_frame");
      check(got_data[0] === header(a), "read_answer_header");
      check(got_data[1][8] === 1'b0 && got_data[2][8] === 1'b0 && got_data[3][8] === 1'b0
            && got_data[4][8] === 1'b0, "read_answer_kind");
      check(got_cycle[0] - started == 1 && got_cycle[4] - started == 5, "read_latency");
      if (!read_shown)
        $display("LATENCY read header=+%0d bytes=+%0d..+%0d", got_cycle[0] - started,
                 got_cycle[1] - started, got_cycle[4] - started);
      read_shown = 1;
      value = {got_data[1][7:0], got_data[2][7:0], got_data[3][7:0], got_data[4][7:0]};
    end
  endtask
  // A frame of another key, words long, its words back to back. The words after its header are
  // the bank's own header, bytes under a strobe and bytes without one. passed: every word left as
  // it came, the tail mark on the last alone. pass_timed: each left in the cycle after it came.
  reg passed, pass_timed;
  task pass_frame(input [4:0] key, input [3:0] index, input integer words);
    integer k;
    reg [8:0] data;
    begin
      fresh;
      for (k = 0; k < words; k = k + 1) begin
        case (k)
          0: data = {key, index};
          1: data = header(index);
          2: data = 9'h1ff;
          3: data = 9'h0a5;
          default: data = 9'h15a;
        endcase
        word(data, k == words - 1);
      end
      idle;
      settle;
      passed = got_n == words && sent_n == words;
      pass_timed = passed;
      for (k = 0; k < words && passed; k = k + 1) begin
        if (got_data[k] !== sent_data[k] || got_tail[k] !== sent_tail[k]
            || sent_tail[k] !== (k == words - 1))
          passed = 0;
        if (got_cycle[k] - sent_cycle[k] != 1) pass_timed = 0;
      end
      pass_timed = pass_timed && passed;
    end
  endtask
  function [31:0] mask_of;
    input [3:0] s;
    mask_of = {{8{s[3]}}, {8{s[2]}}, {8{s[1]}}, {8{s[0]}}};
  endfunction
  // A field's value at its place in a word.
  function [31:0] at;
    input [31:0] value;
    input integer lsb;
    at = value << lsb;
  endfunction
  // The logger's ID word and its two rw bits, as the register map states them.
  wire [31:0] logger_id =
      at(`LOGGER_layout_value, `LOGGER_layout_lsb)
      | at(`LOGGER_log_index_value, `LOGGER_log_index_lsb)
      | at(`LOGGER_logger_index_value, `LOGGER_logger_index_lsb)
      | at(`LOGGER_capability_given_revision_value, `LOGGER_capability_given_revision_lsb)
      | at(`LOGGER_capability_next_logger_value, `LOGGER_capability_next_logger_lsb)
      | at(`LOGGER_capability_target_user_flags_value, `LOGGER_capability_target_user_flags_lsb)
      | at(`LOGGER_capability_user_flags_value, `LOGGER_capability_user_flags_lsb)
      | at(`LOGGER_capability_audit_flags_value, `LOGGER_capability_audit_flags_lsb);
  wire [31:0] alarm_bit = at(1, `LOGGER_alarm_enable_lsb),
              wait_bit = at(1, `LOGGER_wait_when_full_lsb),
              held_bit = at(1, `LOGGER_record_held_lsb),
              overflow_bit = at(1, `LOGGER_record_overflow_lsb);

  reg [31:0] v, old_value, new_value, d;
  integer s, wait_cycles;
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);

    // Reset values and fixed fields.
    read(`CUSTOM_rw32, v); check(v === 0, "reset_rw32");
    read(`CUSTOM_fixed_word, v); check(v === `CUSTOM_fixed_word_value, "read_fixed_word");

    // Every strobe pattern on a 32-bit rw field: four beats each, also with no strobe.
    old_value = 0;
    for (s = 0; s < 16; s = s + 1) begin
      d = 32'h11223344 ^ {4{s[3:0], ~s[3:0]}};
      write(`CUSTOM_rw32, d, s[3:0]);
      new_value = (d & mask_of(s[3:0])) | (old_value & ~mask_of(s[3:0]));
      check(rw32 === new_value, "strobe_pin");
      read(`CUSTOM_rw32, v);
      check(v === new_value, "strobe_readback");
      old_value = new_value;
    end
    check(beats == 16 * 4, "strobe_beat_count");

    // Bytes without a strobe never land, whatever they carry.
    fresh;
    word(header(`CUSTOM_rw32), 0);
    write_words(`CUSTOM_rw32, 32'hffffffff, 4'b0000, 1, 0);
    idle; settle;
    check(rw32 === old_value, "strobe_dirty_bytes");
    check_write_answer(`CUSTOM_rw32);

    // A narrow field keeps its low byte; its strobe gates it.
    write(`CUSTOM_rw8, 32'hdeadbeef, 4'hf);
    read(`CUSTOM_rw8, v); check(v === 32'h000000ef, "narrow_low_byte");
    write(`CUSTOM_rw8, 32'h000000aa, 4'he);
    read(`CUSTOM_rw8, v); check(v === 32'h000000ef, "narrow_strobe");

    // Read-only and unmapped words: no effect, no error.
    write(`CUSTOM_fixed_word, 32'hffffffff, 4'hf);
    read(`CUSTOM_fixed_word, v); check(v === `CUSTOM_fixed_word_value, "fixed_after_write");
    read(4'h5, v); check(v === 0, "hole_read");
    write(4'h5, 32'hffffffff, 4'hf);
    read(4'h5, v); check(v === 0, "hole_write");
    // This unmapped index differs from rw32 only in its high bit.
    read(4'h8, v); check(v === 0, "high_bit_read");
    write(4'h8, 32'hffffffff, 4'hf);
    read(4'h8, v); check(v === 0, "high_bit_write");
    read(4'hf, v); check(v === 0, "high_hole_read");
    write(4'hf, 32'hffffffff, 4'hf);
    read(4'hf, v); check(v === 0, "high_hole_write");
    ro16 = 16'hbeef;
    read(`CUSTOM_ro16, v); check(v === (32'hbeef << `CUSTOM_ro16_lsb), "ro_read");
    write(`CUSTOM_ro16, 32'h0, 4'hf);
    read(`CUSTOM_ro16, v); check(v === (32'hbeef << `CUSTOM_ro16_lsb), "ro_write");

    // An rw1c field: one cycle on its clear output for each written one under the strobe of its
    // byte, nothing for a written zero or without that strobe. A read gives the input.
    write(`CUSTOM_rw1c4, 32'h5, 4'h1);
    check(pulses[0] == 1 && pulses[1] == 0 && pulses[2] == 1 && pulses[3] == 0, "clear_ones");
    write(`CUSTOM_rw1c4, 32'hf, 4'he);
    check(pulses[0] == 1 && pulses[1] == 0 && pulses[2] == 1 && pulses[3] == 0, "clear_strobe");
    write(`CUSTOM_rw1c4, 32'hfffffff0, 4'hf);
    check(pulses[0] == 1 && pulses[1] == 0 && pulses[2] == 1 && pulses[3] == 0, "clear_zeros");
    read(`CUSTOM_rw1c4, v); check(v === 0, "clear_reads_input");
    rw1c4 = 4'h9;
    read(`CUSTOM_rw1c4, v); check(v === (32'h9 << `CUSTOM_rw1c4_lsb), "clear_reads_input");
    write(`CUSTOM_rw1c4, 32'ha, 4'h1);
    check(pulses[0] == 1 && pulses[1] == 1 && pulses[2] == 1 && pulses[3] == 1, "clear_ones");
    read(`CUSTOM_rw1c4, v); check(v === (32'h9 << `CUSTOM_rw1c4_lsb), "clear_keeps_input");
    rw1c4 = 4'h6;
    read(`CUSTOM_rw1c4, v); check(v === (32'h6 << `CUSTOM_rw1c4_lsb), "clear_reads_input");
    check(pulses[0] == 1 && pulses[1] == 1 && pulses[2] == 1 && pulses[3] == 1, "clear_read_quiet");
    rw1c4 = 4'h0;

    // A frame of another key passes from its header to its tail, a read of one word and a write
    // of five, whichever bit of the key differs, and nothing is written. The word after its tail
    // is a header again.
    for (s = 0; s < 32; s = s + 1)
      if (s[4:0] !== `CUSTOM_KEY) begin
        pass_frame(s[4:0], `CUSTOM_rw32, 1);
        check(passed, "other_key_passes");
        check(pass_timed, "pass_latency");
        pass_frame(s[4:0], `CUSTOM_rw32, 5);
        check(passed, "other_key_passes");
        check(pass_timed, "pass_latency");
      end
    check(rw32 === old_value, "pass_no_write");
    read(`CUSTOM_rw32, v); check(v === old_value, "read_after_pass");
    // A frame of another key and the bank's own read behind it, with no cycle between them.
    fresh;
    word({`CUSTOM_KEY ^ 5'h01, `CUSTOM_fixed_word}, 0);
    word(header(`CUSTOM_rw32), 0);
    word(9'h1ff, 0); word(9'h1ff, 0); word(9'h1ff, 1);
    word(header(`CUSTOM_fixed_word), 1);
    idle;
    repeat (7) @(negedge clk);
    check(got_n == 10 && got_tail[4] === 1'b1 && got_data[5] === header(`CUSTOM_fixed_word)
          && got_tail[5] === 1'b0 && got_tail[9] === 1'b1, "own_after_pass_frame");
    v = {got_data[6][7:0], got_data[7][7:0], got_data[8][7:0], got_data[9][7:0]};
    check(v === `CUSTOM_fixed_word_value, "own_after_pass_value");
    check(got_cycle[5] - started == 1 && got_cycle[9] - started == 5, "read_latency");
    check(rw32 === old_value, "pass_no_write");

    // A stalled write leaves its earlier bytes visible, then finishes. Its answer waits for the
    // last byte.
    write(`CUSTOM_rw32, 32'h0, 4'hf);
    fresh;
    word(header(`CUSTOM_rw32), 0);
    write_words(`CUSTOM_rw32, 32'haabbccdd, 4'hf, 0, 2);
    idle;
    repeat (6) @(negedge clk);
    check(rw32 === 32'haabb0000, "partial_visible");
    check(got_n == 0, "write_answer_waits");
    write_rest;
    idle; settle;
    check(rw32 === 32'haabbccdd, "partial_completes");
    check(got_n == 1 && got_tail[0] === 1'b1 && got_data[0] === header(`CUSTOM_rw32)
          && got_cycle[0] - last_beat == 1, "write_answer_after_data");

    // A stalled answer holds its word, and the bank takes no word behind it. A passing frame
    // first: its header waits in the bank, the word behind it at the port.
    fresh;
    s_ready = 0;
    word({`CUSTOM_KEY ^ 5'h10, `CUSTOM_rw32}, 0);
    @(negedge clk);
    r_data = 9'h1c3; r_tail = 0;
    repeat (3) @(negedge clk);
    check(rq_ready[0] === 1'b0, "response_stall_blocks");
    check(rs_valid[0] === 1'b1 && rs_data[0] === {`CUSTOM_KEY ^ 5'h10, `CUSTOM_rw32}
          && rs_tail[0] === 1'b0 && got_n == 0, "response_stall_word");
    s_ready = 1;
    @(posedge clk);
    while (!rq_ready[0]) @(posedge clk);
    word(9'h03c, 1);
    idle; settle;
    check(got_n == 3 && got_data[1] === 9'h1c3 && got_data[2] === 9'h03c && got_tail[1] === 1'b0
          && got_tail[2] === 1'b1, "response_stall_passes");
    // The bank's own read: stalled at the header, then between the bytes, every byte of the read
    // value arrives.
    fresh;
    s_ready = 0;
    word(header(`CUSTOM_rw32), 1);
    idle;
    repeat (3) @(negedge clk);
    check(rq_ready[0] === 1'b0, "response_stall_blocks");
    check(rs_valid[0] === 1'b1 && rs_data[0] === header(`CUSTOM_rw32) && rs_tail[0] === 1'b0
          && got_n == 0, "response_stall_word");
    s_ready = 1;
    @(negedge clk);
    s_ready = 0;
    repeat (4) @(negedge clk);
    check(got_n == 1, "response_stall_word");
    s_ready = 1;
    repeat (2) @(negedge clk);
    s_ready = 0;
    repeat (3) @(negedge clk);
    s_ready = 1;
    wait_cycles = 0;
    while (got_n < 5 && wait_cycles < 40) begin @(negedge clk); wait_cycles = wait_cycles + 1; end
    v = {got_data[1][7:0], got_data[2][7:0], got_data[3][7:0], got_data[4][7:0]};
    check(got_n == 5 && v === 32'haabbccdd, "response_stall_data");
    check(got_data[0] === header(`CUSTOM_rw32) && got_tail[0] === 1'b0
          && got_tail[3] === 1'b0 && got_tail[4] === 1'b1
          && {got_data[1][8], got_data[2][8], got_data[3][8], got_data[4][8]} === 4'b0000,
          "response_stall_frame");
    settle;
    check(got_n == 5, "response_stall_frame");

    // Reset during a write clears the partly written field, and the bank recovers.
    fresh;
    word(header(`CUSTOM_rw32), 0);
    write_words(`CUSTOM_rw32, 32'h55667788, 4'hf, 0, 3);
    @(negedge clk);
    rst_n = 0; r_valid = 0;
    want_in = 0; want_out = 0;
    repeat (5) @(negedge clk);
    check(rw32 === 32'h0, "reset_clears");
    rst_n = 1;
    repeat (5) @(negedge clk);
    read(`CUSTOM_rw32, v); check(v === 0, "read_after_reset");
    write(`CUSTOM_rw32, 32'h01020304, 4'hf);
    read(`CUSTOM_rw32, v); check(v === 32'h01020304, "write_after_reset");
    check(want_in == want_out, "beats_all_seen");

    // The error logger's bank has the first key of a chain: a frame of the custom bank's key
    // passes it.
    bank = 1;
    pass_frame(`CUSTOM_KEY, `LOGGER_alarm_enable, 1);
    check(passed, "logger_other_key_passes");
    check(pass_timed, "pass_latency");
    pass_frame(`CUSTOM_KEY, `LOGGER_alarm_enable, 5);
    check(passed, "logger_other_key_passes");
    check(pass_timed, "pass_latency");
    check(alarm_enable === 1'b0 && wait_when_full === 1'b0, "logger_pass_no_write");
    // Its table: the ID word and the revision are fixed.
    read(`LOGGER_layout, v);
    check(v === logger_id, "logger_id");
    check(v[31:24] === 8'h02, "logger_layout");
    read(`LOGGER_revision, v);
    check(v === `LOGGER_revision_value, "logger_revision");
    write(`LOGGER_layout, 32'hffffffff, 4'hf);
    read(`LOGGER_layout, v); check(v === logger_id, "logger_id_fixed");
    write(`LOGGER_revision, 32'h5affffff, 4'hf);
    read(`LOGGER_revision, v);
    check(v === `LOGGER_revision_value, "logger_revision_fixed");
    // The rw bits stand in byte 1 of STATE and take its strobe alone.
    write(`LOGGER_alarm_enable, alarm_bit | wait_bit, 4'hd);
    read(`LOGGER_alarm_enable, v);
    check(v === 0 && alarm_enable === 1'b0 && wait_when_full === 1'b0, "alarm_needs_byte1");
    write(`LOGGER_alarm_enable, alarm_bit, 4'h2);
    read(`LOGGER_alarm_enable, v);
    check(v === alarm_bit && alarm_enable === 1'b1 && wait_when_full === 1'b0, "alarm_set");
    write(`LOGGER_wait_when_full, wait_bit, 4'h2);
    read(`LOGGER_wait_when_full, v);
    check(v === wait_bit && alarm_enable === 1'b0 && wait_when_full === 1'b1, "wait_set");
    write(`LOGGER_wait_when_full, alarm_bit | wait_bit, 4'h2);
    // The status bits read the logger's values beside the rw bits.
    record_held = 1;
    read(`LOGGER_record_held, v);
    check(v === (alarm_bit | wait_bit | held_bit), "record_held_read");
    record_held = 0; record_overflow = 1;
    read(`LOGGER_record_overflow, v);
    check(v === (alarm_bit | wait_bit | overflow_bit), "record_overflow_read");
    record_held = 1;
    // A clear output stands one cycle for a written one under byte 0's strobe, each bit its own.
    clears = 0; overflow_clears = 0;
    write(`LOGGER_record_held, held_bit | overflow_bit, 4'h0);
    check(clears == 0, "clear_no_strobe");
    check(overflow_clears == 0, "overflow_clear_no_strobe");
    write(`LOGGER_record_held, alarm_bit | wait_bit | held_bit | overflow_bit, 4'he);
    check(clears == 0, "clear_needs_byte0");
    check(overflow_clears == 0, "overflow_clear_needs_byte0");
    write(`LOGGER_record_held, ~(held_bit | overflow_bit), 4'hf);
    check(clears == 0, "clear_needs_one");
    check(overflow_clears == 0, "overflow_clear_needs_one");
    write(`LOGGER_record_held, held_bit, 4'h1);
    check(clears == 1, "clear_one_cycle");
    check(overflow_clears == 0, "clear_leaves_overflow");
    write(`LOGGER_record_overflow, overflow_bit, 4'h1);
    check(overflow_clears == 1, "overflow_clear_one_cycle");
    check(clears == 1, "overflow_clear_leaves_held");
    write(`LOGGER_record_held, held_bit | overflow_bit, 4'h1);
    check(clears == 2 && overflow_clears == 2, "clear_both");
    // The bank keeps no status bit of its own, and byte 0 holds no rw bit.
    read(`LOGGER_record_held, v);
    check(v === (alarm_bit | wait_bit | held_bit | overflow_bit), "clear_reads_input");
    check(alarm_enable === 1'b1 && wait_when_full === 1'b1, "clear_keeps_control");
    record_held = 0; record_overflow = 0;
    // The record's fields at their places.
    record_write = 1; record_bytes = 3'h4; record_address = 32'h12345678;
    record_source_path = 1; record_region_index = 1;
    read(`LOGGER_record_write, v);
    check(v === (alarm_bit | wait_bit | at(1, `LOGGER_record_write_lsb)), "record_state");
    record_write = 0; record_exclusive = 1; record_wrap = 1; record_address_partial = 1;
    read(`LOGGER_record_write, v);
    check(v === (alarm_bit | wait_bit | at(1, `LOGGER_record_exclusive_lsb)
                 | at(1, `LOGGER_record_wrap_lsb) | at(1, `LOGGER_record_address_partial_lsb)),
          "record_flags");
    read(`LOGGER_record_bytes, v); check(v === at(4, `LOGGER_record_bytes_lsb), "record_bytes");
    read(`LOGGER_record_source_path, v);
    check(v === at(1, `LOGGER_record_source_path_lsb), "record_path");
    read(`LOGGER_record_region_index, v);
    check(v === at(1, `LOGGER_record_region_index_lsb), "record_origin");
    record_source_path = 0; record_target_path = 1; record_region_index = 0;
    read(`LOGGER_record_target_path, v);
    check(v === at(1, `LOGGER_record_target_path_lsb), "record_path");
    read(`LOGGER_record_region_index, v); check(v === 0, "record_origin");
    read(`LOGGER_record_address_low, v); check(v === 32'h12345678, "record_address");
    // This record has no audit flags and no user flags: their words read zero.
    read(4'h6, v); check(v === 0, "absent_reads_zero");
    read(4'h8, v); check(v === 0, "absent_reads_zero");
    read(4'hf, v); check(v === 0, "absent_reads_zero");

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
      // Lanes 1 and 0 of the 32-bit rw word.
      expect_beat(`CUSTOM_rw32, 32'h0000ff00, 8'hcc, 4'b0010);
      word({1'b1, 8'hcc}, 0);
      expect_beat(`CUSTOM_rw32, 32'h000000ff, 8'hdd, 4'b0001);
      word({1'b1, 8'hdd}, 1);
    end
  endtask
endmodule
