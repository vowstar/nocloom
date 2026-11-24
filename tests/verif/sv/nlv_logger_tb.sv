`timescale 1ns/1ps
// An error logger over APB4 at PSEL, PENABLE and PREADY: a configuration source reads and writes
// the logger's bank through a service window; a data source makes errors at an observed target. The
// bank's offsets and bit places arrive as macros from the network's map.
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg rst_n = 0;
  integer failures = 0, checks = 0, cycle = 0;
  reg [31:0] c_addr = 0, c_wdata = 0, d_addr = 0;
  reg [3:0] c_strb = 0, lanes = 4'hf; // lanes: the strobes of the next write
  reg c_sel = 0, c_en = 0, c_wr = 0, d_sel = 0, d_en = 0, d_wr = 0;
  wire [31:0] c_rdata, d_rdata;
  wire c_ready, c_err, d_ready, d_err, alarm;
  wire ram_sel, ram_en;
  wire [15:0] ram_addr;
  reg ram_err = 0;
  reg [2:0] d_prot = 0;
  localparam [31:0] BANK = 32'h00100000;
  // STATE: the status bits in byte 0, the rw bits in byte 1, the record's flags above.
  localparam [31:0] HELD = 32'h1 << `RECORD_HELD_lsb, OVERFLOW = 32'h1 << `RECORD_OVERFLOW_lsb,
                    ALARM = 32'h1 << `ALARM_ENABLE_lsb, WAIT = 32'h1 << `WAIT_WHEN_FULL_lsb,
                    WRITE = 32'h1 << `RECORD_WRITE_lsb,
                    PARTIAL = 32'h1 << `RECORD_ADDRESS_PARTIAL_lsb;
  // The route of a record: the data source is source path 1, the observed target is target path 1.
  localparam [31:0] DATA_TO_RAM = 32'h1 << `RECORD_SOURCE_PATH_lsb
                                  | 32'h1 << `RECORD_TARGET_PATH_lsb;
  nocloom_logged dut (.clk(clk), .reset_n(rst_n), .test(1'b0), .nl_o_errors_alarm(alarm),
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
      c_addr = BANK + offset; c_wr = w; c_wdata = d; c_strb = w ? lanes : 4'h0; c_sel = 1; c_en = 0;
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
  // A written one under byte 0's strobe clears a status bit; the rw bits stay.
  task clear_bits(input [31:0] bits);
    begin
      lanes = 4'h1;
      csr(1, `OFFSET_STATE, bits, v);
      lanes = 4'hf;
      repeat (2) @(negedge clk);
    end
  endtask
  task clear;
    clear_bits(HELD);
  endtask
  // The rw bits, with zero in byte 0.
  task control(input [31:0] bits);
    csr(1, `OFFSET_STATE, bits, v);
  endtask
  task state(output held, output overflow);
    begin
      csr(0, `OFFSET_STATE, 0, v);
      held = v[`RECORD_HELD_lsb];
      overflow = v[`RECORD_OVERFLOW_lsb];
    end
  endtask
  reg held, overflow;
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
  // Cycle of the target's access edge and of the next rise of the alarm output.
  integer access_at = 0, alarm_at = 0;
  reg alarm_before = 0;
  always @(posedge clk) begin
    if (ram_sel && ram_en) access_at <= cycle;
    if (alarm && !alarm_before) alarm_at <= cycle;
    alarm_before <= alarm;
  end
`ifdef PLAIN
  // The logger's event model, from the record that arrives and the two clears. A record that
  // arrives takes the logger when it is empty and in the cycle of a clear of record_held. At a
  // held record without that clear it is lost and sets record_overflow. A set and a clear of
  // record_overflow in one cycle leave it set.
  // The probe's model, from its failure events and the logger's take: a failure while the probe
  // holds a record that does not leave in that cycle is dropped and marks the record. The mark
  // leaves with the record and sets record_overflow, even in the cycle of its clear.
  reg previous_held = 0;
  reg check_alarm = 0, expected_held = 0, expected_overflow = 0, expected_flag = 0;
  wire arrival_takes = `LOG_OFFERED && (!expected_held || `LOG_CLEAR);
  wire probe_drop = `PROBE_EVENT && `PROBE_PENDING && !`PROBE_TAKEN;
  integer probe_drops = 0;
  reg took_at_leave = 0, probe_clear_tie = 0;
  // What a window saw: the record arrive before the clear of record_held, in its cycle or after
  // it, a record lost before the clear of record_overflow, in its cycle or after it, and the alarm
  // fall.
  reg watching = 0, cleared = 0, arrived_early = 0, arrived_tied = 0, arrived_late = 0;
  reg overflow_cleared = 0, lost_early = 0, lost_tied = 0, lost_late = 0, alarm_fell = 0;
  always @(posedge clk) begin
    if (rst_n && check_alarm) check(alarm === previous_held, "alarm_cycle");
    if (!rst_n) begin
      expected_held <= 0;
      expected_overflow <= 0;
      expected_flag <= 0;
    end else begin
      check(`LOG_VALID === expected_held, "clear_capture");
      check(`LOG_OVERFLOW === expected_overflow, "overflow_model");
      check(`PROBE_DROPPED === probe_drop, "probe_drop_model");
      check(`PROBE_FLAG === expected_flag, "probe_flag_model");
      expected_held <= arrival_takes || (expected_held && !`LOG_CLEAR);
      expected_overflow <= (`LOG_OFFERED && !arrival_takes) || (`PROBE_TAKEN && expected_flag)
                           || (expected_overflow && !`LOG_OVERFLOW_CLEAR);
      expected_flag <= probe_drop || (expected_flag && !`PROBE_TAKEN);
      if (probe_drop) probe_drops = probe_drops + 1;
      if (`PROBE_ACCEPTED && `PROBE_TAKEN) took_at_leave = 1;
      if (`PROBE_PROBE_LOST && `LOG_OVERFLOW_CLEAR) probe_clear_tie = 1;
    end
    if (!watching) begin
      cleared <= 0; arrived_early <= 0; arrived_tied <= 0; arrived_late <= 0;
      overflow_cleared <= 0; lost_early <= 0; lost_tied <= 0; lost_late <= 0; alarm_fell <= 0;
    end else begin
      if (`LOG_CLEAR) cleared <= 1;
      if (`LOG_OFFERED && `LOG_CLEAR) arrived_tied <= 1;
      else if (`LOG_OFFERED && cleared) arrived_late <= 1;
      else if (`LOG_OFFERED) arrived_early <= 1;
      if (`LOG_OVERFLOW_CLEAR) overflow_cleared <= 1;
      if (`LOG_LOST && `LOG_OVERFLOW_CLEAR) lost_tied <= 1;
      else if (`LOG_LOST && overflow_cleared) lost_late <= 1;
      else if (`LOG_LOST) lost_early <= 1;
      if (alarm_before && !alarm) alarm_fell <= 1;
    end
    previous_held <= `LOG_VALID && `LOG_ALARM;
    check_alarm <= rst_n;
  end
  // Opens a window.
  task watch;
    begin
      watching = 0;
      @(negedge clk);
      watching = 1;
    end
  endtask
  // The logger holds a record of base and waits while full. The probe holds base + 4.
  task probe_fill(input [31:0] base);
    begin
      reset_network;
      probe_drops = 0;
      control(ALARM | WAIT);
      data(0, base);
      settle;
      data(0, base + 4);
      settle;
      check(`PROBE_PENDING && probe_drops == 0, "probe_fill");
    end
  endtask
  integer gap, meets, dropped_at, taken_at;
  // A target error's record keeps the low byte of its address.
  localparam [31:0] LOW = 32'hff;
  reg [31:0] word, next_word;
  reg next_held, next_overflow;
  // Drops at a probe in the log's domain: the waiting record carries them to the logger.
  task probe_drops_check;
    begin
      probe_fill(32'h300);
      data(0, 32'h308);
      data(1, 32'h30c);
      settle;
      check(probe_drops == 2, "probe_drop_counted");
      state(held, overflow);
      check(held == 1 && overflow == 0, "probe_drop_late");
      clear;
      settle;
      state(held, overflow);
      csr(0, `OFFSET_ADDRESS, 0, word);
      check(held == 1 && overflow == 1, "probe_drop_reported");
      check(word == (32'h304 & LOW), "probe_drop_record");
      // The mark left with its record: the next record carries none.
      control(ALARM);
      clear_bits(HELD | OVERFLOW);
      data(0, 32'h310);
      settle;
      state(held, overflow);
      csr(0, `OFFSET_ADDRESS, 0, word);
      check(held == 1 && overflow == 0 && word == (32'h310 & LOW), "probe_drop_once");
      // A clear of record_held lets the waiting record go, and an error comes at a distance from
      // the clear. Before the cycle the record leaves the probe drops the error and marks the
      // record. From that cycle on the probe takes the error, which waits in its turn. Exactly one
      // distance puts the error in the cycle the record leaves.
      meets = 0; dropped_at = 0; taken_at = 0;
      for (gap = 0; gap < 16; gap = gap + 1) begin
        probe_fill(32'h400);
        took_at_leave = 0;
        lanes = 4'h1;
        fork
          csr(1, `OFFSET_STATE, HELD, v);
          begin
            repeat (gap) @(negedge clk);
            data(0, 32'h440 + 4 * gap);
          end
        join
        lanes = 4'hf;
        settle;
        state(held, overflow);
        csr(0, `OFFSET_ADDRESS, 0, word);
        check(held == 1 && word == (32'h404 & LOW), "probe_race_record");
        check(overflow == (probe_drops == 1) && probe_drops <= 1, "probe_race_trace");
        clear_bits(HELD | OVERFLOW);
        settle;
        state(next_held, next_overflow);
        csr(0, `OFFSET_ADDRESS, 0, next_word);
        // No error goes without a trace, and no record is marked without a drop.
        check(probe_drops == 1 ? !next_held && !next_overflow
                               : next_held && !next_overflow && next_word == ((32'h440 + 4 * gap) & LOW),
              "probe_race_trace");
        if (took_at_leave) meets = meets + 1;
        if (probe_drops == 1) dropped_at = dropped_at + 1;
        else taken_at = taken_at + 1;
      end
      check(meets == 1, "probe_race_meet");
      check(dropped_at > 0 && taken_at > meets, "probe_race_span");
      // A marked record waits behind the held one. One write turns wait_when_full off in byte 1
      // and clears both status bits in byte 0, a cycle later. The marked record finds room in the
      // cycle of the clear, and its drop outlives the clear of record_overflow.
      probe_fill(32'h600);
      data(0, 32'h6f0);
      settle;
      probe_clear_tie = 0;
      lanes = 4'h3;
      csr(1, `OFFSET_STATE, HELD | OVERFLOW | ALARM, v);
      lanes = 4'hf;
      settle;
      state(held, overflow);
      csr(0, `OFFSET_ADDRESS, 0, word);
      check(probe_clear_tie, "probe_clear_tied");
      check(held == 1 && overflow == 1, "probe_clear_race");
      check(word == (32'h604 & LOW), "probe_clear_record");
      // A reset while the probe holds a marked record: nothing of the record or its mark is left.
      probe_fill(32'h700);
      data(0, 32'h7f0);
      settle;
      reset_network;
      csr(0, `OFFSET_STATE, 0, v);
      check(v == 0, "probe_reset_clean");
      check(!`PROBE_PENDING && !`PROBE_FLAG, "probe_reset_clean");
      data(0, 32'h7f4);
      settle;
      state(held, overflow);
      csr(0, `OFFSET_ADDRESS, 0, word);
      check(held == 1 && overflow == 0 && word == (32'h7f4 & LOW), "probe_reset_fresh");
    end
  endtask
`endif
  integer d, ties, mode;
  reg [31:0] kept, arriving;
  reg left;
  initial begin
    repeat (5) @(negedge clk);
    rst_n = 1;
    repeat (3) @(negedge clk);
    control(ALARM);
    data(0, 32'h40);
    settle;
    csr(0, `OFFSET_STATE, 0, v);
    check(v == ALARM, "clean_read");
    ram_err = 1;
`ifdef PLAIN
    data(0, 32'h40);
    settle;
    state(held, overflow);
    check(held == 1, "first_record");
    check(overflow == 0, "overflow_clean");
    check(alarm === 1'b1, "alarm_on");
    check(alarm_at > access_at, "alarm_after_event");
    // A record: its flags, its bytes, its route and the kept low bits of its address.
    check(v == (HELD | ALARM | PARTIAL), "first_head");
    csr(0, `OFFSET_EXTENT, 0, v);
    check(v == 32'h4 << `RECORD_BYTES_lsb, "first_bytes");
    csr(0, `OFFSET_PATH, 0, v);
    check(v == DATA_TO_RAM, "first_path");
    csr(0, `OFFSET_REGION, 0, v);
    check(v == 0, "first_region");
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "first_address");
    // A written one without byte 0's strobe clears nothing.
    lanes = 4'he;
    csr(1, `OFFSET_STATE, HELD | OVERFLOW | ALARM, v);
    lanes = 4'hf;
    repeat (2) @(negedge clk);
    state(held, overflow);
    check(held == 1, "clear_needs_strobe");
    // A second error finds the logger full: the first record stays and the overflow bit sets.
    data(1, 32'h1c8);
    settle;
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "kept_first");
    state(held, overflow);
    check(held == 1 && overflow == 1, "overflow_sets");
    // Writes of the rw bits carry zero in byte 0: both status bits stay.
    control(0);
    repeat (3) @(negedge clk);
    check(alarm === 1'b0, "alarm_follows_enable");
    control(ALARM);
    repeat (3) @(negedge clk);
    check(alarm === 1'b1, "alarm_follows_enable");
    csr(0, `OFFSET_STATE, 0, v);
    check(v == (HELD | OVERFLOW | ALARM | PARTIAL), "clear_needs_one");
    // Each status bit clears alone. The overflow bit first: the record is still held.
    clear_bits(OVERFLOW);
    state(held, overflow);
    check(overflow == 0, "overflow_cleared");
    check(held == 1, "overflow_clear_alone");
    check(alarm === 1'b1, "overflow_clear_alone");
    data(0, 32'h44);
    settle;
    state(held, overflow);
    check(held == 1 && overflow == 1, "overflow_sets");
    // Then the held bit: the overflow bit stays, and it does not drive the alarm.
    clear;
    check(alarm === 1'b0, "alarm_follows_clear");
    state(held, overflow);
    check(held == 0, "cleared");
    check(overflow == 1, "held_clear_alone");
    repeat (3) @(negedge clk);
    check(alarm === 1'b0, "overflow_no_alarm");
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h40, "clear_keeps_record");
    clear_bits(OVERFLOW);
    csr(0, `OFFSET_STATE, 0, v);
    check(v == (ALARM | PARTIAL), "overflow_cleared");
    // A write and a miss of this source: the miss keeps its own address and is not partial.
    data(1, 32'h1c8);
    settle;
    csr(0, `OFFSET_STATE, 0, v);
    check(v == (HELD | ALARM | PARTIAL | WRITE), "write_opcode");
    csr(0, `OFFSET_EXTENT, 0, v);
    check(v == 32'h4 << `RECORD_BYTES_lsb, "write_bytes");
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'hc8, "write_address");
    clear;
    data(0, 32'h00090104);
    settle;
    csr(0, `OFFSET_PATH, 0, v);
    check(v == DATA_TO_RAM, "miss_path");
    csr(0, `OFFSET_REGION, 0, v);
    check(v == 32'h1 << `RECORD_REGION_INDEX_lsb, "miss_region");
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h00090104, "miss_address");
    csr(0, `OFFSET_STATE, 0, v);
    check(v == (HELD | ALARM), "miss_whole");
    clear;
    // Wait: the second error waits on the path and enters after the clear. The third is dropped at
    // the probe, and the second record carries that drop to the logger.
    control(ALARM | WAIT);
    data(0, 32'h200);
    settle;
    data(0, 32'h204);
    settle;
    data(0, 32'h208);
    settle;
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h0, "wait_first");
    state(held, overflow);
    check(held == 1 && overflow == 0, "wait_no_overflow");
    watch;
    clear;
    settle;
    // The waiting record stays on its path in the cycle of the clear and enters after it.
    check(arrived_late && !arrived_tied && !arrived_early, "wait_enters_later");
    state(held, overflow);
    check(held == 1, "wait_kept");
    check(overflow == 1, "probe_drop_reported");
    csr(0, `OFFSET_ADDRESS, 0, v);
    check(v == 32'h4, "wait_kept");
    clear;
    settle;
    csr(0, `OFFSET_STATE, 0, v);
    check(!v[`RECORD_HELD_lsb], "third_lost");
    check(v == (OVERFLOW | ALARM | WAIT | PARTIAL), "probe_drop_reported");
    clear_bits(OVERFLOW);
    control(ALARM);
    // A record that arrives in the cycle of a clear of record_held takes the logger: nothing is
    // dropped, so record_overflow keeps its value. The clear and a read error come at every
    // distance, and exactly one distance puts the arrival in the cycle of the clear. Before that
    // distance the record arrives first: at a held record it is lost, at an empty logger it is
    // taken and then cleared. After it the record finds the logger empty.
    // mode[0]: the logger holds a record, a write of another address. mode[1]: record_overflow is
    // set. mode[2]: the write names both status bits, and clears record_overflow.
    for (mode = 0; mode < 8; mode = mode + 1) begin
      ties = 0;
      for (d = 0; d < 8; d = d + 1) begin
        reset_network;
        control(ALARM);
        if (mode[0] || mode[1]) begin
          data(1, 32'h80);
          settle;
        end
        if (mode[1]) begin
          data(1, 32'h84);
          settle;
        end
        if (!mode[0] && mode[1]) clear;
        arriving = 32'h40 + 4 * d;
        watch;
        lanes = 4'h1;
        fork
          csr(1, `OFFSET_STATE, mode[2] ? HELD | OVERFLOW : HELD, v);
          begin
            repeat (d) @(negedge clk);
            data(0, arriving);
          end
        join
        lanes = 4'hf;
        settle;
        csr(0, `OFFSET_STATE, 0, v);
        csr(0, `OFFSET_ADDRESS, 0, kept);
        held = v[`RECORD_HELD_lsb];
        overflow = v[`RECORD_OVERFLOW_lsb];
        // The held record was left when the arriving record came first and was lost.
        left = mode[0] && arrived_early;
        check(arrived_early + arrived_tied + arrived_late == 1, "clear_capture_window");
        check(held == !arrived_early, "clear_capture_held");
        check(overflow == (!mode[2] && (mode[1] || left)), "clear_capture_overflow");
        check(kept == (left ? 32'h80 : arriving), "clear_capture_record");
        check(v[`RECORD_WRITE_lsb] == left, "clear_capture_direction");
        check(alarm === held, "clear_capture_alarm");
        // No record is lost without a trace.
        if (mode[0] && !mode[2]) check(held || overflow, "clear_capture_trace");
        if (arrived_tied) begin
          ties = ties + 1;
          check(held == 1, "clear_capture_held");
          check(overflow == (mode[1] && !mode[2]), "clear_capture_overflow");
          check(kept == arriving, "clear_capture_record");
          check(!v[`RECORD_WRITE_lsb], "clear_capture_direction");
          // The alarm of a held record stays up through the cycle of the clear.
          if (mode[0]) check(!alarm_fell, "clear_capture_alarm");
          // A clear with no record in its cycle empties the logger and keeps the fields.
          clear;
          csr(0, `OFFSET_STATE, 0, v);
          check(!v[`RECORD_HELD_lsb], "clear_capture_emptied");
          check(v[`RECORD_OVERFLOW_lsb] == (mode[1] && !mode[2]), "clear_capture_emptied");
          check(alarm === 1'b0, "clear_capture_emptied");
          csr(0, `OFFSET_ADDRESS, 0, kept);
          check(kept == arriving, "clear_capture_emptied");
        end
      end
      check(ties == 1, "clear_capture_overlap");
    end
    probe_drops_check;
    // A set and a clear of record_overflow in one cycle leave it set. The logger holds a record
    // and a write clears record_overflow alone while a second error comes at every distance:
    // exactly one distance puts the loss in the cycle of the clear. Before that distance the
    // write clears the loss. At it and after it the loss stays. The held record stays throughout.
    ties = 0;
    for (d = 0; d < 8; d = d + 1) begin
      reset_network;
      data(1, 32'h80);
      settle;
      watch;
      lanes = 4'h1;
      fork
        csr(1, `OFFSET_STATE, OVERFLOW, v);
        begin
          repeat (d) @(negedge clk);
          data(0, 32'h40 + 4 * d);
        end
      join
      lanes = 4'hf;
      settle;
      state(held, overflow);
      csr(0, `OFFSET_ADDRESS, 0, kept);
      check(lost_early + lost_tied + lost_late == 1, "overflow_tie_window");
      check(held == 1 && kept == 32'h80, "overflow_tie_record");
      check(overflow == !lost_early, "overflow_tie_order");
      if (lost_tied) begin
        ties = ties + 1;
        check(overflow == 1, "overflow_set_wins");
      end
    end
    check(ties == 1, "overflow_set_wins");
`elsif AUDIT
    // Zeta is PPROT[0], Alpha PPROT[1] inverted, mid PPROT[2]: a record's word stands in that order,
    // and the logger takes only Alpha one and zeta zero.
    data(0, 32'h40);
    settle;
    state(held, overflow);
    check(held == 1, "filter_accepts");
    csr(0, `OFFSET_AUDIT, 0, v);
    check(v == 32'h2, "audit_record");
    clear;
    d_prot = 3'b100;
    data(1, 32'h44);
    d_prot = 3'b001;
    settle;
    state(held, overflow);
    check(held == 1, "filter_accepts");
    csr(0, `OFFSET_AUDIT, 0, v);
    check(v == 32'h6, "audit_order");
    clear;
    d_prot = 3'b001;
    data(0, 32'h48);
    settle;
    state(held, overflow);
    check(held == 0, "filter_rejects_one");
    d_prot = 3'b010;
    data(1, 32'h4c);
    settle;
    state(held, overflow);
    check(held == 0, "filter_rejects_zero");
    // Behind a full logger a record the filter rejects is no overflow; one it takes is.
    d_prot = 3'b000;
    data(0, 32'h50);
    settle;
    d_prot = 3'b010;
    data(0, 32'h54);
    settle;
    state(held, overflow);
    check(held == 1 && overflow == 0, "filter_no_overflow");
    d_prot = 3'b000;
    data(0, 32'h58);
    settle;
    state(held, overflow);
    check(held == 1 && overflow == 1, "overflow_sets");
    clear_bits(OVERFLOW);
    // Wait does not look at the filter: behind a full logger a rejected record still waits.
    control(ALARM | WAIT);
    d_prot = 3'b010;
    data(0, 32'h54);
    settle;
    d_prot = 3'b000;
    data(0, 32'h58);
    settle;
    clear;
    settle;
    state(held, overflow);
    check(held == 0, "filter_waits");
    check(overflow == 1, "probe_drop_filtered");
`else
    // The decode class is skipped: its records wait like any error and are dropped as the logger
    // takes them.
    data(0, 32'h00090000);
    settle;
    state(held, overflow);
    check(held == 0, "skipped_dropped");
    data(0, 32'h40);
    settle;
    csr(0, `OFFSET_STATE, 0, v);
    check(v == (HELD | ALARM | PARTIAL | 32'h2 << `RECORD_CLASS_lsb), "target_class_logged");
    // Behind a full logger an skipped record is no overflow; a record of the target class is.
    data(0, 32'h00090000);
    settle;
    state(held, overflow);
    check(held == 1 && overflow == 0, "skipped_no_overflow");
    data(0, 32'h44);
    settle;
    state(held, overflow);
    check(held == 1 && overflow == 1, "overflow_sets");
    clear_bits(OVERFLOW);
    control(ALARM | WAIT);
    data(0, 32'h00090000);
    settle;
    data(0, 32'h80);
    settle;
    clear;
    settle;
    state(held, overflow);
    check(held == 0, "skipped_waits");
    check(overflow == 1, "probe_drop_filtered");
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
