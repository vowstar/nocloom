`timescale 1ns/1ps
// The outlet of a register window at its four faces, with a chain of register banks behind it.
// The driver puts the window above this file as defines: its size, the base of each bank, the
// table of a bank from the register_map the fixture names, and the instances with their wiring.
// The outlet's frames go to bank 0, each bank's response port feeds the next bank's request port,
// and the last bank's response port is the answer path of the outlet.
//
// The network admits one packet at a time to a register window: a word is under way from its
// request handshake to its response handshake, and the head of the next waits until none is. The
// bench keeps that count and offers a word only while it is zero, at the earliest in the cycle
// after the response handshake of the word before, so words of different sources follow back to
// back. It sends single words from four sources, one face at a time: reads and writes of the
// banks, words outside every bank and decode misses. Each response face stalls by its own random
// pattern: single cycles, periodic ready, long stalls, and a ready that falls in the cycle an
// answer appears.
//
// The scoreboard checks that every word taken gets one answer, on the face of its kind, with its
// own source, id, tag and result, a read with the value of the register it names, and that an
// answer stays as it is while its face stalls; a model of every register follows the words in
// the order they are taken. A run fails when too few answers from a bank, local answers, write
// answers or read answers waited for a stalled face: it cannot pass without stalled response
// faces. The check under_way_at_most_one states the admission itself. With the option that
// offers a word while another is under way that check fails, and an answer leaves with the
// fields of another word: the outlet keeps the fields of one word.
module tb;
  localparam integer BANKS = `OUTLET_BANKS, WINDOW = `OUTLET_WINDOW,
                     BANK_BYTES = `OUTLET_BANK_BYTES;
  // Depth of the scoreboard's queue, and what ends a run early: cycles without a word or an
  // answer, cycles in all, failed checks.
  localparam integer Q = 16, QUIET = 1000, CYCLES = 600000, FAILURES = 400;
  // Answers of each kind that must have waited for a stalled face, and words that must have
  // followed a word of another source back to back.
  localparam integer NEED = 8;
  // Results of an answer.
  localparam [1:0] OKAY = 2'd0, MISSED = 2'd1, FAILED = 2'd2;

  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0;

  // ---- The request faces. The outlet reads the fields the bench drives from registers. ----
  reg wreq_valid = 0, rreq_valid = 0, wreq_error = 0, rreq_error = 0;
  reg [31:0] wreq_address = 0, rreq_address = 0, wreq_data = 0;
  reg [3:0] wreq_be = 0;
  reg [1:0] wreq_source = 0, rreq_source = 0, wreq_tag = 0, rreq_tag = 0;
  reg [2:0] wreq_id = 0, rreq_id = 0;
  wire wreq_ready, rreq_ready;
  wire wreq_write = 1'b1, rreq_write = 1'b0;
  wire [2:0] wreq_protection = 3'd0, rreq_protection = 3'd0, wreq_size = 3'd2, rreq_size = 3'd2;
  wire [7:0] wreq_length = 8'd0, rreq_length = 8'd0;
  wire [1:0] wreq_burst = 2'd1, rreq_burst = 2'd1;
  wire [3:0] wreq_cache = 4'd0, rreq_cache = 4'd0, rreq_be = 4'd0;
  wire [31:0] wreq_start_address = wreq_address, rreq_start_address = rreq_address;
  wire [31:0] rreq_data = 32'd0;
  wire [5:0] wreq_count = 6'd4, rreq_count = 6'd4;
  wire wreq_ends = 1'b1, rreq_ends = 1'b1, wreq_wrap_end = 1'b1, rreq_wrap_end = 1'b1;
  wire wreq_first = 1'b1, rreq_first = 1'b1, wreq_last = 1'b1, rreq_last = 1'b1;

  // ---- The response faces. ----
  reg wrsp_ready = 1, rrsp_ready = 1;
  wire wrsp_valid, rrsp_valid, wrsp_first, rrsp_first, wrsp_last, rrsp_last, wrsp_error, rrsp_error;
  wire [31:0] wrsp_address, rrsp_address, wrsp_data, rrsp_data;
  wire [1:0] wrsp_result, rrsp_result, wrsp_source, rrsp_source, wrsp_tag, rrsp_tag;
  wire [2:0] wrsp_id, rrsp_id;
  wire [5:0] wrsp_count, rrsp_count;

  // ---- The serial path: the outlet's frames, the chain of banks, the answer path. ----
  wire hint_bank_valid, hint_bank_tail, hint_bank_ready;
  wire hint_bank_out_valid, hint_bank_out_tail, hint_bank_out_ready;
  wire [8:0] hint_bank_data, hint_bank_out_data;
  wire [BANKS-1:0] rq_valid, rq_tail, rq_ready, rs_valid, rs_tail, rs_ready;
  wire [8:0] rq_data[0:BANKS-1], rs_data[0:BANKS-1];
  wire [31:0] control[0:BANKS-1];
  wire [7:0] level[0:BANKS-1];
  wire [3:0] flags_clear[0:BANKS-1];
  reg [4*BANKS-1:0] flags_in = 0;
  // The outlet's two waiting flags: a local answer, an answer from a bank.
  wire wait_local, wait_answer;
  // Fault selection of a fault build.
  reg [7:0] fault_sel = 0;
  // Stimulus options: the seed, the number of random words, response faces that never stall, and
  // a word offered while another is under way.
  integer seed = `OUTLET_SEED, words = `OUTLET_WORDS, option = 0;
  reg open = 0, relaxed = 0, calm = 1;

  `OUTLET_INSTANCES

  // A failed check: printed the first time for each kind, counted every time.
  localparam integer K_UNDER = 0, K_FLAGS = 1, K_WITHDRAWN = 2, K_STABLE = 3, K_TWO = 4,
                     K_NO_WORD = 5, K_FACE = 6, K_SOURCE = 7, K_ID = 8, K_TAG = 9, K_RESULT = 10,
                     K_DATA = 11, K_SHAPE = 12, K_BOTH = 13, K_FULL = 14, K_TIMEOUT = 15,
                     KINDS = 16;
  reg [KINDS-1:0] seen = 0;
  task fail(input integer kind, input [8*32-1:0] what);
    begin
      failures = failures + 1;
      if (!seen[kind]) begin
        seen[kind] = 1;
        $display("CHECK_FAIL %0s cycle=%0d", what, cycle);
      end
    end
  endtask
  task check(input ok, input integer kind, input [8*32-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) fail(kind, what);
    end
  endtask
  // A check of the closing report: every failure is printed.
  task closing(input ok, input [8*32-1:0] what, input integer at);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL %0s at=%0d", what, at);
      end
    end
  endtask

  // ---- Random numbers: one generator for the words, one for the stalls. ----
  reg [31:0] rng = 1, stall_rng = 1;
  function [31:0] draw32;
    input dummy;
    begin
      rng = rng ^ (rng << 13);
      rng = rng ^ (rng >> 17);
      rng = rng ^ (rng << 5);
      draw32 = rng;
    end
  endfunction
  // A value below limit.
  function [31:0] draw;
    input [31:0] limit;
    reg [31:0] value;
    begin
      value = draw32(1'b0);
      draw = (value >> 4) % limit;
    end
  endfunction
  function [31:0] stall_draw;
    input [31:0] limit;
    begin
      stall_rng = stall_rng ^ (stall_rng << 13);
      stall_rng = stall_rng ^ (stall_rng >> 17);
      stall_rng = stall_rng ^ (stall_rng << 5);
      stall_draw = (stall_rng >> 4) % limit;
    end
  endfunction

  // ---- The model: the window's banks and every register as the words are taken. ----
  reg [31:0] base[0:BANKS-1], fixed[0:BANKS-1], m_control[0:BANKS-1];
  reg [7:0] m_level[0:BANKS-1];
  // The bank an address is in, or -1 for an address outside every bank.
  function integer bank_of;
    input [31:0] address;
    integer k;
    begin
      bank_of = -1;
      for (k = 0; k < BANKS; k = k + 1)
        if ((address & (WINDOW - 1) & ~(BANK_BYTES - 1)) == base[k]) bank_of = k;
    end
  endfunction
  function [31:0] model_read;
    input integer bank;
    input [3:0] index;
    begin
      model_read = 32'h0;
      if (index == `OUTLET_place) model_read = fixed[bank];
      if (index == `OUTLET_control) model_read = m_control[bank];
      if (index == `OUTLET_level) model_read = {24'h0, m_level[bank]} << `OUTLET_level_lsb;
      if (index == `OUTLET_flags)
        model_read = {28'h0, flags_in[4 * bank +: 4]} << `OUTLET_flags_lsb;
    end
  endfunction
  task model_write(input integer bank, input [3:0] index, input [31:0] data, input [3:0] be);
    integer b;
    begin
      if (index == `OUTLET_control)
        for (b = 0; b < 4; b = b + 1)
          if (be[b]) m_control[bank][8 * b +: 8] = data[8 * b +: 8];
      if (index == `OUTLET_level && be[`OUTLET_level_lsb / 8])
        m_level[bank] = data[`OUTLET_level_lsb +: 8];
    end
  endtask

  // The word on a request face, and the words taken whose answers have not left, in order.
  reg cur_write = 0, cur_error = 0;
  reg [31:0] cur_address = 0, cur_data = 0;
  reg [3:0] cur_be = 0;
  reg [1:0] cur_source = 0, cur_tag = 0;
  reg [2:0] cur_id = 0;
  reg e_write[0:Q-1];
  reg [1:0] e_source[0:Q-1], e_tag[0:Q-1], e_result[0:Q-1];
  reg [2:0] e_id[0:Q-1];
  reg [31:0] e_data[0:Q-1];
  integer exp_in = 0, exp_out = 0;

  // ---- The scoreboard, at every rising edge. ----
  integer answers = 0, taken_words = 0, reads = 0, writes = 0, outside_words = 0, miss_words = 0;
  // The network's count of words under way: one more at a request handshake, one fewer at a
  // response handshake.
  integer under_way = 0;
  // Answers that waited for a stalled face: from a bank, local, on the write face, on the read
  // face, and those whose face was ready until the cycle they appeared in. The cycles all of
  // them waited, and the stalls of 20 cycles.
  integer stalled_bank = 0, stalled_local = 0, stalled_write = 0, stalled_read = 0;
  integer start_stalls = 0, stall_cycles = 0, long_stalls = 0, stall_run = 0, quiet = 0;
  // Words offered in the cycle after the response handshake of a word of another source.
  integer back_to_back = 0;
  integer h, e, bank_hit;
  reg take_w, take_r, showing, stalled, cur_served, finished = 0;
  // The answer of the cycle before, when its face did not take it.
  reg held = 0, held_write = 0;
  reg [40:0] held_fields = 0, face_fields;
  // An answer left in the cycle before, and the source it went to. The readies of the cycle
  // before: read, write.
  reg left = 0;
  reg [1:0] left_source = 0, ready_before = 2'b11;

  task answer(input write, input [1:0] source, input [2:0] id, input [1:0] tag,
              input [1:0] result, input [31:0] data, input shaped);
    begin
      quiet = 0;
      answers = answers + 1;
      left = 1;
      left_source = source;
      if (under_way > 0) under_way = under_way - 1;
      if (exp_in == exp_out) fail(K_NO_WORD, "answer_without_word");
      else begin
        check(write == e_write[h], K_FACE, "answer_face");
        check(source == e_source[h], K_SOURCE, "answer_source");
        check(id == e_id[h], K_ID, "answer_id");
        check(tag == e_tag[h], K_TAG, "answer_tag");
        check(result == e_result[h], K_RESULT, "answer_result");
        if (!write) check(data == e_data[h], K_DATA, "read_data");
        check(shaped, K_SHAPE, "answer_shape");
        exp_out = exp_out + 1;
      end
    end
  endtask

  always @(posedge clk) if (rst_n && !finished) begin
    cycle = cycle + 1;
    quiet = quiet + 1;
    take_w = wreq_valid && wreq_ready;
    take_r = rreq_valid && rreq_ready;
    showing = wrsp_valid || rrsp_valid;
    stalled = (wrsp_valid && !wrsp_ready) || (rrsp_valid && !rrsp_ready);
    h = exp_out % Q;
    cur_served = !cur_error && bank_of(cur_address) >= 0;
    if (left && (wreq_valid || rreq_valid) && cur_source != left_source)
      back_to_back = back_to_back + 1;
    left = 0;
    // One of the outlet's two flags says what waits: an answer from a bank or a local one.
    if (showing && exp_in != exp_out)
      check(wait_local != wait_answer && wait_answer == (e_result[h] == OKAY), K_FLAGS,
            "waiting_flags");
    // An answer that was not taken stays on its face as it was. A write answer has no data.
    face_fields = wrsp_valid ? {wrsp_source, wrsp_id, wrsp_tag, wrsp_result, 32'h0}
                             : {rrsp_source, rrsp_id, rrsp_tag, rrsp_result, rrsp_data};
    if (held) begin
      if (!(held_write ? wrsp_valid : rrsp_valid)) fail(K_WITHDRAWN, "answer_withdrawn");
      else check(face_fields == held_fields, K_STABLE, "answer_stable");
    end else if (stalled && exp_in != exp_out) begin
      // The first cycle of an answer that waits.
      if (e_result[h] == OKAY) stalled_bank = stalled_bank + 1;
      else stalled_local = stalled_local + 1;
      if (wrsp_valid) stalled_write = stalled_write + 1;
      else stalled_read = stalled_read + 1;
      if (wrsp_valid ? ready_before[0] : ready_before[1]) start_stalls = start_stalls + 1;
    end
    if (stalled) stall_cycles = stall_cycles + 1;
    stall_run = stalled ? stall_run + 1 : 0;
    if (stall_run == 20) long_stalls = long_stalls + 1;
    held = stalled;
    held_write = wrsp_valid;
    held_fields = face_fields;
    ready_before = {rrsp_ready, wrsp_ready};
    // The answers that leave.
    check(!(wrsp_valid && rrsp_valid), K_TWO, "answer_two_faces");
    if (wrsp_valid && wrsp_ready)
      answer(1'b1, wrsp_source, wrsp_id, wrsp_tag, wrsp_result, wrsp_data,
             wrsp_count == 6'd4 && wrsp_first && wrsp_last && !wrsp_error);
    h = exp_out % Q;
    if (rrsp_valid && rrsp_ready)
      answer(1'b0, rrsp_source, rrsp_id, rrsp_tag, rrsp_result, rrsp_data,
             rrsp_count == 6'd4 && rrsp_first && rrsp_last && !rrsp_error);
    // The word that is taken: what its answer must be, and what it leaves in the registers.
    if (take_w || take_r) begin
      quiet = 0;
      under_way = under_way + 1;
      check(!(take_w && take_r), K_BOTH, "two_words");
      check(exp_in - exp_out < Q, K_FULL, "queue_overrun");
      e = exp_in % Q;
      bank_hit = bank_of(cur_address);
      e_write[e] = cur_write;
      e_source[e] = cur_source;
      e_id[e] = cur_id;
      e_tag[e] = cur_tag;
      e_result[e] = cur_error ? MISSED : (bank_hit < 0 ? FAILED : OKAY);
      e_data[e] = 32'h0;
      if (cur_served && !cur_write) e_data[e] = model_read(bank_hit, cur_address[5:2]);
      if (cur_served && cur_write) model_write(bank_hit, cur_address[5:2], cur_data, cur_be);
      exp_in = exp_in + 1;
      taken_words = taken_words + 1;
      if (cur_error) miss_words = miss_words + 1;
      else if (bank_hit < 0) outside_words = outside_words + 1;
      else if (cur_write) writes = writes + 1;
      else reads = reads + 1;
    end
    // The admission of the network: one word under way.
    check(under_way <= 1, K_UNDER, "under_way_at_most_one");
    if (quiet > QUIET || cycle > CYCLES || failures > FAILURES) begin
      if (failures <= FAILURES) fail(K_TIMEOUT, "global_timeout");
      report;
      $finish;
    end
  end

  // ---- The stalls of the response faces, at every falling edge: face 0 write, face 1 read. ----
  integer stall_mode[0:1], stall_left[0:1], stall_period[0:1];
  reg [1:0] face_valid, face_was = 0, face_ready;
  integer f, pick_stall;
  always @(negedge clk) begin
    face_valid = {rrsp_valid, wrsp_valid};
    for (f = 0; f < 2; f = f + 1) begin
      if (open || calm) face_ready[f] = 1;
      else begin
        if (stall_left[f] <= 0) begin
          pick_stall = stall_draw(10);
          stall_period[f] = 2 + stall_draw(5);
          if (pick_stall < 3) begin stall_mode[f] = 0; stall_left[f] = 1 + stall_draw(12); end
          else if (pick_stall < 7) begin stall_mode[f] = 1; stall_left[f] = 5 + stall_draw(40); end
          else if (pick_stall < 9) begin stall_mode[f] = 2; stall_left[f] = 10 + stall_draw(60); end
          else begin stall_mode[f] = 3; stall_left[f] = 20 + stall_draw(130); end
        end
        // A stall that starts in the cycle an answer appears.
        if (face_valid[f] && !face_was[f] && stall_draw(3) == 0) begin
          stall_mode[f] = 3;
          stall_left[f] = 1 + stall_draw(30);
        end
        stall_left[f] = stall_left[f] - 1;
        case (stall_mode[f])
          0: face_ready[f] = 1;
          1: face_ready[f] = (stall_draw(2) == 0);
          2: face_ready[f] = (stall_left[f] % stall_period[f] == 0);
          default: face_ready[f] = 0;
        endcase
      end
    end
    face_was = face_valid;
    wrsp_ready = face_ready[0];
    rrsp_ready = face_ready[1];
  end

  // ---- The request driver. Every task starts and ends just behind a falling clock edge. ----
  task send(input write, input miss, input [31:0] address, input [31:0] data, input [3:0] be,
            input [1:0] source, input [2:0] id, input [1:0] tag);
    begin
      // No word is under way.
      while (!relaxed && under_way != 0) @(negedge clk);
      cur_write = write; cur_error = miss; cur_address = address; cur_data = data; cur_be = be;
      cur_source = source; cur_id = id; cur_tag = tag;
      if (write) begin
        wreq_address = address; wreq_data = data; wreq_be = be; wreq_source = source;
        wreq_id = id; wreq_tag = tag; wreq_error = miss; wreq_valid = 1;
      end else begin
        rreq_address = address; rreq_source = source; rreq_id = id; rreq_tag = tag;
        rreq_error = miss; rreq_valid = 1;
      end
      @(posedge clk);
      while (!(write ? wreq_ready : rreq_ready)) @(posedge clk);
      @(negedge clk);
      wreq_valid = 0;
      rreq_valid = 0;
    end
  endtask
  task idle(input integer cycles);
    begin
      repeat (cycles) @(negedge clk);
    end
  endtask
  task drain;
    begin
      while (exp_in != exp_out) @(negedge clk);
    end
  endtask
  // The stalls start and stop at a rising edge, half a cycle away from the process that makes
  // them: every simulator sees the same order.
  task stall(input on);
    begin
      @(posedge clk);
      calm = !on;
      @(negedge clk);
    end
  endtask
  // An address of the window outside every bank.
  function [31:0] outside;
    input dummy;
    reg [31:0] address;
    begin
      address = draw(WINDOW / 4) * 4;
      while (bank_of(address) >= 0) address = draw(WINDOW / 4) * 4;
      outside = address;
    end
  endfunction

  // ---- The closing report. ----
  task report;
    integer k;
    begin
      finished = 1;
      closing(exp_out == exp_in, "answer_missing", exp_in - exp_out);
      closing(!wrsp_valid && !rrsp_valid && !wait_local && !wait_answer && !hint_bank_valid &&
              !hint_bank_out_valid && rq_valid == 0 && rs_valid == 0 && !wreq_valid && !rreq_valid,
              "end_idle", cycle);
      for (k = 0; k < BANKS; k = k + 1)
        closing(control[k] === m_control[k] && level[k] === m_level[k], "final_field", k);
      closing(reads > 0, "cover_read", reads);
      closing(writes > 0, "cover_write", writes);
      closing(outside_words > 0, "cover_outside", outside_words);
      closing(miss_words > 0, "cover_miss", miss_words);
      closing(back_to_back >= NEED, "back_to_back_unseen", back_to_back);
      // No pass without answers of each kind and on each face that waited for a stalled face.
      closing(stalled_bank >= NEED, "stall_bank_unseen", stalled_bank);
      closing(stalled_local >= NEED, "stall_local_unseen", stalled_local);
      closing(stalled_write >= NEED, "stall_write_unseen", stalled_write);
      closing(stalled_read >= NEED, "stall_read_unseen", stalled_read);
      closing(start_stalls > 0, "stall_at_start_unseen", start_stalls);
      closing(long_stalls > 0, "long_stall_unseen", long_stalls);
      $display("OUTLET seed=%0d words=%0d answers=%0d reads=%0d writes=%0d outside=%0d miss=%0d",
               seed, taken_words, answers, reads, writes, outside_words, miss_words);
      $display("OUTLET cycles=%0d back_to_back=%0d", cycle, back_to_back);
      $display("STALL answers bank=%0d local=%0d write_face=%0d read_face=%0d at_start=%0d",
               stalled_bank, stalled_local, stalled_write, stalled_read, start_stalls);
      $display("STALL cycles=%0d long=%0d", stall_cycles, long_stalls);
      if (failures == 0) $display("CHECK_PASS checks=%0d words=%0d", checks, taken_words);
      else $display("OUTLET_FAILURES %0d", failures);
    end
  endtask

  integer bank, turn, pick, s;
  reg [31:0] address, data;
  reg [3:0] index, strobes;
  reg [1:0] source, last_source;
  reg write, miss;
  initial begin
    if ($value$plusargs("NLV_SEED=%d", option)) seed = option;
    if ($value$plusargs("NLV_WORDS=%d", option)) words = option;
    if ($value$plusargs("NLV_FAULT=%d", option)) fault_sel = option;
    if ($value$plusargs("NLV_OPEN=%d", option)) open = option != 0;
    if ($value$plusargs("NLV_RELAXED=%d", option)) relaxed = option != 0;
    rng = seed;
    if (rng == 32'h0) rng = 32'h1;
    stall_rng = rng ^ 32'h9e3779b9;
    if (stall_rng == 32'h0) stall_rng = 32'h1;
    // The first values of a small seed are small: they are not used.
    for (turn = 0; turn < 16; turn = turn + 1) begin
      data = draw32(1'b0);
      data = stall_draw(2);
    end
    $display("SEED %0d", seed);
    `OUTLET_BASES
    `OUTLET_FIXED
    for (bank = 0; bank < BANKS; bank = bank + 1) begin
      flags_in[4 * bank +: 4] = draw(16);
      m_control[bank] = 32'h0;
      m_level[bank] = 8'h0;
    end
    for (turn = 0; turn < 2; turn = turn + 1) begin
      stall_mode[turn] = 0;
      stall_left[turn] = 0;
      stall_period[turn] = 2;
    end
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);

    // Every source once at every bank with ready response faces: a write of the rw word with a
    // value that names the source and the bank, read back by the next source, a read of the fixed
    // word, a write outside every bank and a read that missed.
    for (s = 0; s < 4; s = s + 1)
      for (bank = 0; bank < BANKS; bank = bank + 1) begin
        data = (draw32(1'b0) & 32'hffffff00) | (s << 4) | bank;
        send(1, 0, base[bank] + 4 * `OUTLET_control, data, 4'hf, s, draw(8), draw(4));
        send(0, 0, base[bank] + 4 * `OUTLET_control, 32'h0, 4'h0, s + 1, draw(8), draw(4));
        send(0, 0, base[bank] + 4 * `OUTLET_place, 32'h0, 4'h0, s, draw(8), draw(4));
        send(1, 0, outside(1'b0), draw32(1'b0), 4'hf, s + 2, draw(8), draw(4));
        send(0, 1, base[bank] + 4 * `OUTLET_control, 32'h0, 4'h0, s + 3, draw(8), draw(4));
      end
    drain;

    // Random words while both response faces stall. Most words come from another source than the
    // word before.
    stall(1);
    last_source = 0;
    for (turn = 0; turn < words; turn = turn + 1) begin
      pick = draw(100);
      if (pick >= 95) idle(5 + draw(16));
      else if (pick >= 70) idle(1 + draw(3));
      source = last_source + 1 + draw(3);
      if (draw(8) == 0) source = last_source;
      last_source = source;
      bank = draw(BANKS);
      pick = draw(8);
      case (pick)
        0, 1: index = `OUTLET_control;
        2: index = `OUTLET_level;
        3: index = `OUTLET_flags;
        4: index = `OUTLET_place;
        default: index = draw(16);
      endcase
      address = base[bank] + 4 * index;
      miss = 0;
      pick = draw(8);
      if (pick == 4 || pick == 5) address = outside(1'b0);
      if (pick == 6) begin
        miss = 1;
        if (draw(2) == 0) address = outside(1'b0);
      end
      write = draw(2);
      data = draw32(1'b0);
      strobes = draw(16);
      if (draw(4) == 0) strobes = 4'hf;
      send(write, miss, address, data, strobes, source, draw(8), draw(4));
    end

    // Every rw field of every bank read back while the response faces still stall.
    for (bank = 0; bank < BANKS; bank = bank + 1) begin
      send(0, 0, base[bank] + 4 * `OUTLET_control, 32'h0, 4'h0, bank, draw(8), draw(4));
      send(0, 0, base[bank] + 4 * `OUTLET_level, 32'h0, 4'h0, bank + 1, draw(8), draw(4));
    end
    drain;
    stall(0);
    // A word or an answer that should not exist would show within these cycles.
    idle(4 * BANKS + 16);
    report;
    $finish;
  end
endmodule
