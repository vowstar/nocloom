`timescale 1ns/1ps
// A chain of register banks driven at its serial port, with the register stage of a chain after
// every eighth bank. The driver puts the chain above this file as defines: its length, its
// stages, the table of a bank from the register_map the fixture names, and the instances with
// their wiring. Bank 0 takes the request, each bank's response port feeds the next bank's request
// port, and the last bank's response port is the answer of the chain.
//
// A frame is a header (the bank's key above the index of the register) and, for a write, four
// data words (a strobe above a byte, byte 3 first); the tail mark stands beside the last word. A
// bank passes the frames of other keys, so the answer of a bank travels through the banks after
// it, and the answers leave the chain in the order of the requests.
//
// The bench sends random reads and writes with random idle cycles between words and frames while
// the answer's ready stalls at random. A model of every register follows the requests; the
// scoreboard checks every answer frame, every write beat of every bank, every clear output and
// every word that enters and leaves a stage. A run fails when a stage saw no stall, no stall that
// the answer's ready caused, or no word in its second place: it cannot pass without back-pressure
// through every stage.
module tb;
  localparam integer BANKS = `CHAIN_BANKS, STAGES = `CHAIN_STAGES, SEGMENT = `CHAIN_SEGMENT,
                     PLACES = `CHAIN_STAGE_PLACES;
  // Depths of the scoreboard's queues: accesses in flight, beats of a bank, words of a stage.
  localparam integer Q = 256, BQ = 64, SQ = 128;
  // Accesses the bench keeps in flight, and what ends a run early: cycles without an answer
  // word, cycles in all, failed checks of answers and beats.
  localparam integer FLIGHT = 64, QUIET = 1000, CYCLES = 600000, FAILURES = 200;

  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, shown = 0, cycle = 0;

  // The request of the chain and the ready of its answer.
  reg r_valid = 0, r_tail = 0, s_ready = 1;
  reg [8:0] r_data = 0;
  // Request port and response port of bank k.
  wire [BANKS-1:0] rq_valid, rq_tail, rq_ready, rs_valid, rs_tail, rs_ready;
  wire [8:0] rq_data[0:BANKS-1], rs_data[0:BANKS-1];
  // Fields and bus observation of bank k. The rw1c inputs of bank k stand at bits 4k+3:4k.
  wire [31:0] control[0:BANKS-1];
  wire [7:0] level[0:BANKS-1];
  wire [3:0] flags_clear[0:BANKS-1];
  reg [4*BANKS-1:0] flags_in = 0;
  wire [BANKS-1:0] write_active, complete;
  wire [3:0] word_index[0:BANKS-1], lanes[0:BANKS-1];
  wire [31:0] write_mask[0:BANKS-1], write_value[0:BANKS-1];
  // The two places of stage s, the stage in front of bank s * SEGMENT. Index 0 is not a stage.
  wire [STAGES:0] place_first, place_second;
  assign place_first[0] = 1'b0;
  assign place_second[0] = 1'b0;
  // Fault selection of a fault build: the fault and the stage that carries it.
  reg [7:0] fault_sel = 0;
  integer fault_stage = 0;
  // Stimulus options: the seed, an answer that never stalls, one access at a time.
  integer seed = `CHAIN_SEED, accesses = `CHAIN_ACCESSES, option = 0;
  reg open = 0, sparse = 0;

  `CHAIN_INSTANCES

  task check(input ok, input [8*32-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        shown = shown + 1;
        if (shown <= 60) $display("CHECK_FAIL %0s cycle=%0d", what, cycle);
      end
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
  // A failed check of stage s: printed the first time for each kind, counted every time, and
  // no reason to end the run.
  integer stage_fails[0:(STAGES+1)*8-1];
  task stage_fail(input integer s, input integer kind, input [8*32-1:0] what);
    begin
      failures = failures + 1;
      stage_fails[s * 8 + kind] = stage_fails[s * 8 + kind] + 1;
      if (stage_fails[s * 8 + kind] == 1)
        $display("CHECK_FAIL stage%0d_%0s cycle=%0d", s, what, cycle);
    end
  endtask
  task stage_closing(input ok, input integer s, input [8*32-1:0] what);
    begin
      checks = checks + 1;
      if (!ok) begin
        failures = failures + 1;
        $display("CHECK_FAIL stage%0d_%0s", s, what);
      end
    end
  endtask

  // ---- Random numbers: one generator for the requests, one for the stalls. ----
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

  // ---- The model: every register as the requests leave it, in the order they are sent. ----
  reg [31:0] fixed[0:BANKS-1], m_control[0:BANKS-1];
  reg [7:0] m_level[0:BANKS-1];
  // The accesses in flight, in order: what each answer must be.
  reg [4:0] exp_key[0:Q-1];
  reg [3:0] exp_index[0:Q-1];
  reg exp_write[0:Q-1], exp_timed[0:Q-1];
  reg [31:0] exp_data[0:Q-1];
  integer exp_beats[0:Q-1];
  integer exp_in = 0, exp_out = 0;
  // The write beats each bank must show, in order.
  reg [3:0] want_addr[0:BANKS*BQ-1], want_lane[0:BANKS*BQ-1];
  reg [31:0] want_mask[0:BANKS*BQ-1];
  reg [7:0] want_byte[0:BANKS*BQ-1];
  integer want_in[0:BANKS-1], want_out[0:BANKS-1];
  // Cycles each clear output must stand at one, and accesses sent to each bank.
  integer want_pulses[0:4*BANKS-1], sent_to[0:BANKS-1];

  function [31:0] mask_of;
    input [3:0] s;
    mask_of = {{8{s[3]}}, {8{s[2]}}, {8{s[1]}}, {8{s[0]}}};
  endfunction
  // What a read of a word of bank k gives.
  function [31:0] model_word;
    input integer k;
    input [3:0] index;
    begin
      model_word = 32'h0;
      if (index == `CHAIN_place) model_word = fixed[k];
      if (index == `CHAIN_control) model_word = m_control[k];
      if (index == `CHAIN_level) model_word = {24'h0, m_level[k]} << `CHAIN_level_lsb;
      if (index == `CHAIN_flags) model_word = {28'h0, flags_in[4*k +: 4]} << `CHAIN_flags_lsb;
    end
  endfunction

  // ---- The scoreboard's view of the hardware. ----
  // The fields as the beats seen so far leave them.
  reg [31:0] hw_control[0:BANKS-1];
  reg [7:0] hw_level[0:BANKS-1];
  integer beats_seen[0:BANKS-1], pulses[0:4*BANKS-1];
  integer answered_from[0:BANKS-1], reads_of[0:BANKS-1], writes_of[0:BANKS-1];
  // The cycle that took each request header, in order.
  integer take_cycle[0:Q-1];
  integer take_n = 0;
  reg in_frame = 0;
  // The answer in progress: 0 at a header, 1 to 4 at the bytes of a read.
  integer a_phase = 0, quiet = 0;
  reg a_skip = 0, gave_up = 0, timed_out = 0;
  reg [31:0] a_value = 0;
  integer request_words = 0, answer_words = 0, answer_stalls = 0, reads = 0, writes = 0;
  integer latency_read = 0, latency_write = 0;
  // The words inside each stage, its counts, and what its places showed the cycle before.
  reg [9:0] sq_word[0:(STAGES+1)*SQ-1];
  integer sq_head[0:STAGES], sq_count[0:STAGES];
  integer came[0:STAGES], went[0:STAGES], stalls[0:STAGES], deep_stalls[0:STAGES],
          second_words[0:STAGES];
  reg second_before[0:STAGES];

  integer init;
  initial begin
    for (init = 0; init < BANKS; init = init + 1) begin
      fixed[init] = 32'h0; m_control[init] = 32'h0; m_level[init] = 8'h0;
      hw_control[init] = 32'h0; hw_level[init] = 8'h0;
      want_in[init] = 0; want_out[init] = 0; sent_to[init] = 0;
      beats_seen[init] = 0; answered_from[init] = 0; reads_of[init] = 0; writes_of[init] = 0;
    end
    for (init = 0; init < 4 * BANKS; init = init + 1) begin
      want_pulses[init] = 0; pulses[init] = 0;
    end
    for (init = 0; init < BANKS * BQ; init = init + 1) begin
      want_addr[init] = 4'h0; want_lane[init] = 4'h0; want_mask[init] = 32'h0;
      want_byte[init] = 8'h0;
    end
    for (init = 0; init < Q; init = init + 1) begin
      exp_key[init] = 5'h0; exp_index[init] = 4'h0; exp_write[init] = 1'b0;
      exp_timed[init] = 1'b0; exp_data[init] = 32'h0; exp_beats[init] = 0; take_cycle[init] = 0;
    end
    for (init = 0; init <= STAGES; init = init + 1) begin
      sq_head[init] = 0; sq_count[init] = 0; came[init] = 0; went[init] = 0;
      stalls[init] = 0; deep_stalls[init] = 0; second_words[init] = 0;
      second_before[init] = 1'b0;
    end
    for (init = 0; init < (STAGES + 1) * SQ; init = init + 1) sq_word[init] = 10'h0;
    for (init = 0; init < (STAGES + 1) * 8; init = init + 1) stage_fails[init] = 0;
    `CHAIN_FIXED
  end

  // The answer of the access at the head of the queue is complete.
  task answered(input integer e);
    integer took;
    begin
      answered_from[exp_key[e]] = answered_from[exp_key[e]] + 1;
      took = cycle - take_cycle[e];
      if (exp_write[e]) begin
        writes_of[exp_key[e]] = writes_of[exp_key[e]] + 1;
        writes = writes + 1;
        if (exp_timed[e] && latency_write == 0) latency_write = took;
      end else begin
        reads_of[exp_key[e]] = reads_of[exp_key[e]] + 1;
        reads = reads + 1;
        if (exp_timed[e] && latency_read == 0) latency_read = took;
      end
      // An idle chain with a ready answer: a bank a cycle, a stage a cycle, and the five cycles
      // of the bank that serves the access, counted from the cycle that took the header.
      if (exp_timed[e]) check(took == BANKS + STAGES + 4, "chain_latency");
      exp_out = exp_out + 1;
    end
  endtask

  // One word of the chain's answer.
  task answer_word(input [8:0] data, input tail);
    integer e, other;
    reg found;
    begin
      e = exp_out % Q;
      if (a_skip) begin
        // Words behind a frame that should have ended.
        check(0, "answer_frame_long");
        a_skip = !tail;
      end else if (a_phase == 0) begin
        if (exp_out == exp_in) begin
          check(0, "answer_unexpected");
          a_skip = !tail;
        end else begin
          if (data !== {exp_key[e], exp_index[e]}) begin
            // The header of another access in flight, or of none.
            found = 0;
            for (other = exp_out + 1; other < exp_in; other = other + 1)
              if (data === {exp_key[other % Q], exp_index[other % Q]}) found = 1;
            if (found) check(0, "answer_order");
            else check(0, "answer_header");
          end else check(1, "answer_header");
          if (exp_write[e]) begin
            check(tail === 1'b1, "write_answer_frame");
            check(beats_seen[exp_key[e]] >= exp_beats[e], "write_answer_early");
            answered(e);
            a_skip = !tail;
          end else begin
            check(tail === 1'b0, "read_answer_frame");
            if (tail) begin
              check(0, "read_answer_words");
              answered(e);
            end else begin
              a_phase = 1;
              a_value = 32'h0;
            end
          end
        end
      end else begin
        check(data[8] === 1'b0, "read_answer_kind");
        a_value = {a_value[23:0], data[7:0]};
        check(tail === (a_phase == 4), "read_answer_frame");
        if (a_phase == 4) begin
          check(a_value === exp_data[e], "read_data");
          answered(e);
          a_phase = 0;
          a_skip = !tail;
        end else if (tail) begin
          check(0, "read_answer_words");
          answered(e);
          a_phase = 0;
        end else a_phase = a_phase + 1;
      end
    end
  endtask

  integer k, n, s, at_in, at_out, slot, behind;
  reg deep;
  reg [31:0] image;
  always @(posedge clk) begin
    cycle = cycle + 1;
    if (rst_n) begin
      // The request: the cycle of each header.
      if (r_valid && rq_ready[0]) begin
        request_words = request_words + 1;
        if (!in_frame) begin
          take_cycle[take_n % Q] = cycle;
          take_n = take_n + 1;
        end
        in_frame = !r_tail;
      end

      // Every bank: its fields change by its own write beats alone, each beat is the next one
      // the requests ask of this bank, and a clear output stands for a requested one alone.
      for (k = 0; k < BANKS; k = k + 1) begin
        if (control[k] !== hw_control[k] || level[k] !== hw_level[k]) begin
          check(0, "field_value");
          hw_control[k] = control[k];
          hw_level[k] = level[k];
        end
        if (write_active[k] && rq_valid[k]) begin
          beats_seen[k] = beats_seen[k] + 1;
          if (want_out[k] == want_in[k]) check(0, "beat_unexpected");
          else begin
            slot = k * BQ + want_out[k] % BQ;
            check(word_index[k] === want_addr[slot], "beat_addr");
            check(write_mask[k] === want_mask[slot], "beat_strobe");
            check(write_value[k][7:0] === want_byte[slot], "beat_byte");
            check(lanes[k] === want_lane[slot], "beat_lane");
            check(complete[k] === want_lane[slot][0], "beat_last");
            if (want_addr[slot] == `CHAIN_control)
              hw_control[k] = (hw_control[k] & ~want_mask[slot])
                              | ({4{want_byte[slot]}} & want_mask[slot]);
            if (want_addr[slot] == `CHAIN_level) begin
              image = {24'h0, hw_level[k]} << `CHAIN_level_lsb;
              image = (image & ~want_mask[slot]) | ({4{want_byte[slot]}} & want_mask[slot]);
              hw_level[k] = image >> `CHAIN_level_lsb;
            end
            want_out[k] = want_out[k] + 1;
          end
        end
        if (flags_clear[k] !== 4'h0)
          for (n = 0; n < 4; n = n + 1)
            if (flags_clear[k][n]) begin
              pulses[4 * k + n] = pulses[4 * k + n] + 1;
              check(pulses[4 * k + n] <= want_pulses[4 * k + n], "clear_unexpected");
            end
      end
      checks = checks + BANKS;

      // Every stage: the words it took leave it once each, in order, the first of them offered
      // in every cycle, and its two places hold what the words say.
      for (s = 1; s <= STAGES; s = s + 1) begin
        at_in = s * SEGMENT - 1;
        at_out = s * SEGMENT;
        if (sq_count[s] != place_first[s] + place_second[s]) stage_fail(s, 0, "places");
        if (sq_count[s] > 0 && !rq_valid[at_out]) stage_fail(s, 1, "word_withheld");
        if (rq_valid[at_out] && !rq_ready[at_out]) stalls[s] = stalls[s] + 1;
        if (place_second[s] && !second_before[s]) second_words[s] = second_words[s] + 1;
        second_before[s] = place_second[s];
        if (rq_valid[at_out] && rq_ready[at_out]) begin
          went[s] = went[s] + 1;
          if (sq_count[s] == 0) stage_fail(s, 2, "word_twice");
          else begin
            // The place of the leaving word among the words inside, the first of them at 0.
            behind = 0;
            while (behind < sq_count[s] && {rq_tail[at_out], rq_data[at_out]}
                                           !== sq_word[s * SQ + (sq_head[s] + behind) % SQ])
              behind = behind + 1;
            if (behind == sq_count[s]) begin
              // A word the stage never took.
              stage_fail(s, 4, "word_order");
              behind = 0;
            end else if (behind != 0) begin
              // A word behind the first left: the words before it are gone.
              stage_fail(s, 3, "word_lost");
            end
            sq_head[s] = (sq_head[s] + behind + 1) % SQ;
            sq_count[s] = sq_count[s] - behind - 1;
          end
        end
        if (rs_valid[at_in] && rs_ready[at_in]) begin
          came[s] = came[s] + 1;
          // Both places hold a word: the stage has no place for this one.
          if (sq_count[s] >= PLACES) stage_fail(s, 5, "word_overrun");
          if (sq_count[s] < SQ) begin
            sq_word[s * SQ + (sq_head[s] + sq_count[s]) % SQ] = {rs_tail[at_in], rs_data[at_in]};
            sq_count[s] = sq_count[s] + 1;
          end
        end
      end
      // A stall of a stage that the answer's ready causes: from the stage to the end of the
      // chain every bank offers a word that is not taken.
      deep = !s_ready;
      for (k = BANKS - 1; k >= 1; k = k - 1) begin
        deep = deep && rs_valid[k] && !rs_ready[k];
        if (k % SEGMENT == 0 && deep && rq_valid[k] && !rq_ready[k])
          deep_stalls[k / SEGMENT] = deep_stalls[k / SEGMENT] + 1;
      end

      // The answer of the chain.
      if (rs_valid[BANKS-1] && !s_ready) answer_stalls = answer_stalls + 1;
      if (rs_valid[BANKS-1] && s_ready) begin
        answer_words = answer_words + 1;
        quiet = 0;
        answer_word(rs_data[BANKS-1], rs_tail[BANKS-1]);
      end else if (exp_in != exp_out) quiet = quiet + 1;
      else quiet = 0;
      if (quiet > QUIET) gave_up = 1;
      if (cycle > CYCLES) begin
        gave_up = 1;
        timed_out = 1;
      end
      // A run with this many failed answers and beats has said what it has to say.
      if (shown > FAILURES) gave_up = 1;
      if (gave_up) begin
        report;
        $finish;
      end
    end else begin
      in_frame = 0;
    end
  end

  // ---- The ready of the answer: open, random, one cycle in a few, or closed, for a while. ----
  reg calm = 1;
  integer stall_mode = 0, stall_left = 0, stall_period = 2;
  always @(negedge clk) begin
    if (calm || open) s_ready = 1;
    else begin
      if (stall_left == 0) begin
        case (stall_draw(8))
          0, 1: begin stall_mode = 0; stall_left = 1 + stall_draw(24); end
          2, 3: begin stall_mode = 1; stall_left = 8 + stall_draw(120); end
          4, 5, 6: begin
            stall_mode = 2;
            stall_period = 2 + stall_draw(5);
            stall_left = 40 + stall_draw(400);
          end
          default: begin stall_mode = 3; stall_left = 20 + stall_draw(130); end
        endcase
      end
      stall_left = stall_left - 1;
      case (stall_mode)
        0: s_ready = 1;
        1: s_ready = (stall_draw(2) == 0);
        2: s_ready = (stall_left % stall_period == 0);
        default: s_ready = 0;
      endcase
    end
  end

  // The stalls start and stop at a rising edge, half a cycle away from the process that makes
  // them: every simulator sees the same order.
  task stall(input on);
    begin
      @(posedge clk);
      calm = !on;
      @(negedge clk);
    end
  endtask

  // ---- Request driver. Every task starts and ends just behind a falling clock edge. ----
  task word(input [8:0] data, input tail);
    begin
      r_valid = 1; r_data = data; r_tail = tail;
      @(posedge clk);
      while (!rq_ready[0]) @(posedge clk);
      @(negedge clk);
      r_valid = 0; r_tail = 0;
    end
  endtask
  task idle(input integer cycles);
    begin
      repeat (cycles) @(negedge clk);
    end
  endtask
  task drain;
    begin
      while (exp_out != exp_in) @(negedge clk);
    end
  endtask
  // One access: what its answer and its beats must be, then its words. An access that is not
  // timed leaves random idle cycles between the words of a write.
  task access(input integer bank, input [3:0] index, input write, input [31:0] d,
              input [3:0] s, input timed);
    integer e, b, pick, slot;
    reg [31:0] mask, image;
    reg [3:0] ones;
    begin
      while (exp_in - exp_out >= FLIGHT || want_in[bank] - want_out[bank] > BQ - 8)
        @(negedge clk);
      e = exp_in % Q;
      exp_key[e] = bank; exp_index[e] = index; exp_write[e] = write; exp_timed[e] = timed;
      exp_data[e] = model_word(bank, index);
      if (write) begin
        mask = mask_of(s);
        if (index == `CHAIN_control) m_control[bank] = (m_control[bank] & ~mask) | (d & mask);
        if (index == `CHAIN_level) begin
          image = {24'h0, m_level[bank]} << `CHAIN_level_lsb;
          image = (image & ~mask) | (d & mask);
          m_level[bank] = image >> `CHAIN_level_lsb;
        end
        if (index == `CHAIN_flags) begin
          // A written one under the strobe of its byte: one cycle on that clear output.
          image = (d & mask) >> `CHAIN_flags_lsb;
          ones = image[3:0];
          for (b = 0; b < 4; b = b + 1)
            if (ones[b]) want_pulses[4 * bank + b] = want_pulses[4 * bank + b] + 1;
        end
        // Every data word is a beat, with or without its strobe, and carries its byte.
        for (b = 3; b >= 0; b = b - 1) begin
          slot = bank * BQ + want_in[bank] % BQ;
          want_addr[slot] = index;
          want_mask[slot] = s[b] ? 32'hff << (8 * b) : 32'h0;
          want_byte[slot] = d[8 * b +: 8];
          want_lane[slot] = 4'b0001 << b;
          want_in[bank] = want_in[bank] + 1;
        end
      end
      exp_beats[e] = want_in[bank];
      sent_to[bank] = sent_to[bank] + 1;
      exp_in = exp_in + 1;
      word({bank[4:0], index}, !write);
      if (write)
        for (b = 3; b >= 0; b = b - 1) begin
          if (!timed) begin
            pick = draw(100);
            if (pick >= 95) idle(4 + draw(17));
            else if (pick >= 70) idle(1 + draw(3));
          end
          word({s[b], d[8 * b +: 8]}, b == 0);
        end
    end
  endtask
  // New values on the rw1c inputs of a bank that has no access in flight.
  integer rearms = 0;
  task rearm(input integer bank);
    begin
      if (sent_to[bank] == answered_from[bank]) begin
        flags_in[4 * bank +: 4] = draw(16);
        rearms = rearms + 1;
      end
    end
  endtask

  // ---- The closing report. ----
  task report;
    integer k, n, s, clears;
    begin
      closing(!timed_out, "global_timeout", cycle);
      closing(exp_out == exp_in, "answer_missing", exp_in - exp_out);
      closing(a_phase == 0 && !a_skip, "answer_frame_open", a_phase);
      for (k = 0; k < BANKS; k = k + 1) begin
        closing(control[k] === m_control[k] && level[k] === m_level[k], "final_field", k);
        closing(want_in[k] == want_out[k], "beats_all_seen", k);
        clears = 0;
        for (n = 0; n < 4; n = n + 1) begin
          closing(pulses[4 * k + n] == want_pulses[4 * k + n], "clear_count", k);
          clears = clears + pulses[4 * k + n];
        end
        closing(reads_of[k] > 0, "cover_read", k);
        closing(writes_of[k] > 0, "cover_write", k);
        closing(clears > 0, "cover_clear", k);
      end
      $display("CHAIN banks=%0d stages=%0d seed=%0d accesses=%0d reads=%0d writes=%0d",
               BANKS, STAGES, seed, exp_out, reads, writes);
      $display("CHAIN request_words=%0d answer_words=%0d answer_stalls=%0d rearms=%0d cycles=%0d",
               request_words, answer_words, answer_stalls, rearms, cycle);
      $display("LATENCY read=%0d write=%0d", latency_read, latency_write);
      for (s = 1; s <= STAGES; s = s + 1) begin
        $display("STAGE %0d after_bank=%0d words=%0d stalls=%0d answer_stalls=%0d second_place=%0d",
                 s, s * SEGMENT, went[s], stalls[s], deep_stalls[s], second_words[s]);
        // A chain that gave up keeps words in its stages: the counts say nothing then.
        if (!gave_up) begin
          stage_closing(came[s] <= went[s], s, "word_lost");
          stage_closing(came[s] >= went[s], s, "word_twice");
        end
        // No pass without back-pressure through the stage.
        stage_closing(stalls[s] > 0, s, "never_stalled");
        stage_closing(deep_stalls[s] > 0, s, "answer_stall_unseen");
        stage_closing(second_words[s] > 0, s, "second_place_unused");
      end
      if (failures == 0) $display("CHECK_PASS checks=%0d accesses=%0d", checks, exp_out);
      else $display("CHAIN_FAILURES %0d", failures);
    end
  endtask

  integer bank, turn, pick, pause;
  reg [3:0] index, strobes;
  reg [31:0] data;
  reg write;
  initial begin
    if ($value$plusargs("NLV_SEED=%d", option)) seed = option;
    if ($value$plusargs("NLV_ACCESSES=%d", option)) accesses = option;
    if ($value$plusargs("NLV_FAULT=%d", option)) fault_sel = option;
    if ($value$plusargs("NLV_FAULT_STAGE=%d", option)) fault_stage = option;
    if ($value$plusargs("NLV_OPEN=%d", option)) open = option != 0;
    if ($value$plusargs("NLV_SPARSE=%d", option)) sparse = option != 0;
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
    for (bank = 0; bank < BANKS; bank = bank + 1) flags_in[4 * bank +: 4] = draw(16);
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);

    // Every bank once, one access at a time on an idle chain with a ready answer: a read of its
    // fixed word, a write of its rw word and a clear of every flag. Each access takes the cycles
    // of the chain's length.
    for (bank = 0; bank < BANKS; bank = bank + 1) begin
      access(bank, `CHAIN_place, 0, 32'h0, 4'h0, 1); drain;
      data = draw32(1'b0);
      access(bank, `CHAIN_control, 1, data, 4'hf, 1); drain;
      access(bank, `CHAIN_flags, 1, 32'hf << `CHAIN_flags_lsb, 4'hf, 1); drain;
    end

    // Random accesses, many in flight, while the answer stalls.
    stall(1);
    for (turn = 0; turn < accesses; turn = turn + 1) begin
      pick = draw(100);
      pause = 0;
      if (pick >= 99) pause = 60 + draw(100);
      else if (pick >= 85) pause = 5 + draw(36);
      else if (pick >= 55) pause = 1 + draw(4);
      idle(pause);
      if (sparse) drain;
      bank = draw(BANKS);
      if (draw(6) == 0) rearm(bank);
      bank = draw(BANKS);
      pick = draw(8);
      case (pick)
        0, 1: index = `CHAIN_control;
        2: index = `CHAIN_level;
        3: index = `CHAIN_flags;
        4: index = `CHAIN_place;
        default: index = draw(16);
      endcase
      write = draw(2);
      data = draw32(1'b0);
      strobes = draw(16);
      if (draw(4) == 0) strobes = 4'hf;
      access(bank, index, write, data, strobes, 0);
    end
    drain;

    // Every rw field of every bank read back while the answer still stalls.
    for (bank = 0; bank < BANKS; bank = bank + 1) begin
      access(bank, `CHAIN_control, 0, 32'h0, 4'h0, 0);
      if (sparse) drain;
      access(bank, `CHAIN_level, 0, 32'h0, 4'h0, 0);
      if (sparse) drain;
    end
    drain;
    stall(0);
    // A word that should not exist would leave the chain within these cycles.
    idle(BANKS + PLACES * STAGES + 16);
    report;
    $finish;
  end
endmodule
