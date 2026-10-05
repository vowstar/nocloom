`timescale 1ns/1ps
// AXI4 / AXI4-Lite initiator-side BFM: drives a DUT slave port (a NocLoom
// initiator endpoint). Descriptors are pushed by the tape interpreter with
// zero-time tasks; channel processes run in one clocked block. AW, W and AR
// proceed independently; W of a burst may lead its AW unless w_after_aw.
// The built-in monitor checks the DUT-driven B and R channels: VALID and
// payload hold, reset quiet, unexpected responses, B before the last W of
// its write, RLAST position, contract quiet windows and a throughput window.
// Every handshake, VALID edge and reset edge seen is logged as one JSON line.
// Endpoint dependencies for liveness negative controls: R ready mode 5
// (await_b) keeps RREADY low until a1 B handshakes happened since the mode
// was set; R ready mode 7 (beats) keeps RREADY high until a1 R beats were
// taken since the mode was set, then low (a source holding one beat); the
// W release gate holds every W beat at or after beat
// W_RG_BEAT of a burst until W_RG_N R handshakes happened since W_RG_N was
// set (0 disables it). VRATE_AW/W/AR (percent, 0 = always) raise a pending
// request VALID only on that share of cycles (random backpressure from the
// initiator side; a raised VALID stays until its handshake). Strobe mode 4
// picks per beat full (13/20), sparse (3/20), half (3/20) or none (1/20).
// Strobe mode 5 strobes the active lanes whose bit is set in the 128-lane
// mask KEEP0..KEEP3 as it stood when the burst was pushed. Strobe mode 6
// takes beat k's lane mask from the strobe table (tape SET of index 32 +
// entry): STAB_W entries per beat (bit j of entry w for lane 32 * w + j;
// with one entry, lane j modulo 32), row k modulo STAB_N / STAB_W, read when
// the beat is loaded; it keeps only the beat's active lanes. stall_report, called when a tape wait times out,
// prints the channel state and raises LIVE.E1 or LIVE.E2 when this
// initiator breaks the matching premise.
module nlv_axi_master #(
    parameter NAME = "m",
    parameter ID_W = 4,
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter LEN_W = 8,
    parameter LOCK_W = 1,
    parameter QOS_W = 4,
    parameter USER_W = 1,
    parameter USER_PRESENT = 0,
    parameter DEPTH = 64,
    parameter LITE = 0
) (
    input wire clk,
    input wire rst_n,
    output reg [ID_W-1:0] awid,
    output reg [ADDR_W-1:0] awaddr,
    output reg [LEN_W-1:0] awlen,
    output reg [2:0] awsize,
    output reg [1:0] awburst,
    output reg [LOCK_W-1:0] awlock,
    output reg [3:0] awcache,
    output reg [2:0] awprot,
    output reg [QOS_W-1:0] awqos,
    output wire [3:0] awregion,
    output reg awvalid,
    input wire awready,
    output reg [ID_W-1:0] wid,
    output reg [DATA_W-1:0] wdata,
    output reg [DATA_W/8-1:0] wstrb,
    output reg wlast,
    output reg wvalid,
    input wire wready,
    input wire [ID_W-1:0] bid,
    input wire [1:0] bresp,
    input wire [USER_W-1:0] buser,
    input wire bvalid,
    output reg bready,
    output reg [ID_W-1:0] arid,
    output reg [ADDR_W-1:0] araddr,
    output reg [LEN_W-1:0] arlen,
    output reg [2:0] arsize,
    output reg [1:0] arburst,
    output reg [LOCK_W-1:0] arlock,
    output reg [3:0] arcache,
    output reg [2:0] arprot,
    output reg [QOS_W-1:0] arqos,
    output wire [3:0] arregion,
    output reg arvalid,
    input wire arready,
    input wire [ID_W-1:0] rid,
    input wire [DATA_W-1:0] rdata,
    input wire [1:0] rresp,
    input wire [USER_W-1:0] ruser,
    input wire rlast,
    input wire rvalid,
    output reg rready
);
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  localparam NID = 1 << ID_W;
  assign awregion = 4'd0;
  assign arregion = 4'd0;

  // Write and read descriptor queues.
  reg [63:0] wq_addr[0:DEPTH-1];
  reg [7:0] wq_len[0:DEPTH-1];
  reg [2:0] wq_size[0:DEPTH-1];
  reg [1:0] wq_burst[0:DEPTH-1];
  reg [ID_W-1:0] wq_id[0:DEPTH-1];
  reg wq_lock[0:DEPTH-1];
  reg [3:0] wq_cache[0:DEPTH-1];
  reg [2:0] wq_prot[0:DEPTH-1];
  reg [3:0] wq_qos[0:DEPTH-1];
  reg [7:0] wq_strb[0:DEPTH-1];
  reg [31:0] wq_salt[0:DEPTH-1];
  reg [127:0] wq_keep[0:DEPTH-1];
  reg [31:0] wq_tag[0:DEPTH-1];
  reg [63:0] rq_addr[0:DEPTH-1];
  reg [7:0] rq_len[0:DEPTH-1];
  reg [2:0] rq_size[0:DEPTH-1];
  reg [1:0] rq_burst[0:DEPTH-1];
  reg [ID_W-1:0] rq_id[0:DEPTH-1];
  reg rq_lock[0:DEPTH-1];
  reg [3:0] rq_cache[0:DEPTH-1];
  reg [2:0] rq_prot[0:DEPTH-1];
  reg [3:0] rq_qos[0:DEPTH-1];
  reg [31:0] rq_tag[0:DEPTH-1];
  integer w_tail, aw_head, w_head, w_beat, r_tail, ar_head;
  // Per-ID bookkeeping for the response monitor.
  integer b_expect[0:NID-1];
  // Per ID, the AW sequence numbers still waiting for B (W bursts follow AW
  // order, so AW k is answered only after W burst k completed: w_head > k).
  integer b_fifo_seq[0:NID-1][0:DEPTH-1];
  integer b_fifo_head[0:NID-1], b_fifo_tail[0:NID-1];
  integer w_done_before;
  integer r_fifo_len[0:NID-1][0:DEPTH-1];
  integer r_fifo_head[0:NID-1], r_fifo_tail[0:NID-1], r_left[0:NID-1];

  // Control registers (set by the tape).
  reg [31:0] ctl[0:31];
  localparam B_MODE = 0, B_A1 = 1, B_A2 = 2, R_MODE = 3, R_A1 = 4, R_A2 = 5, GATE_AW = 6,
      GATE_W = 7, GATE_AR = 8, W_AFTER_AW = 9, AW_GAP = 10, W_GAP = 11, AR_GAP = 12,
      QUIET_B = 13, QUIET_R = 14, TPUT_CH = 15, TPUT_NUM = 16, TPUT_DEN = 17, SEED = 18,
      W_RG_BEAT = 19, W_RG_N = 20, VRATE_AW = 21, VRATE_W = 22, VRATE_AR = 23, KEEP0 = 24,
      STAB_N = 28, STAB_W = 29;
  reg [31:0] stab[0:63];
  localparam R_AWAIT_B = 5, R_BEATS = 7;

  // Counters (read by the tape).
  reg [63:0] cyc;
  reg [63:0] n_aw, n_w, n_b, n_ar, n_r, n_rlast, n_wpush, n_rpush, n_wlast, n_cancel;
  reg [31:0] b_wait, r_wait, lfsr, aw_gap_left, w_gap_left, ar_gap_left;
  // Cycles each request VALID has waited for READY; snapshot bases of the
  // await_b ready mode and the W release gate.
  reg [31:0] aw_hold, w_hold, ar_hold;
  reg [63:0] r_b_base, r_r_base, w_rg_base;
  reg prev_bvalid, prev_bready, prev_rvalid, prev_rready, prev_rst;
  wire [USER_W-1:0] observed_buser = USER_PRESENT ? buser : {USER_W{1'b0}};
  wire [USER_W-1:0] observed_ruser = USER_PRESENT ? ruser : {USER_W{1'b0}};
  reg [ID_W+USER_W+1:0] prev_b;
  reg [ID_W+USER_W+DATA_W+2:0] prev_r;
  reg prev_awvalid, prev_wvalid, prev_arvalid;
  reg [63:0] tput_start, tput_count;
  integer i, k, lane_lo, lane_hi, id_i;
  reg [63:0] baddr;

  function [63:0] counter;
    input integer index;
    begin
      case (index)
        0: counter = n_aw;
        1: counter = n_w;
        2: counter = n_b;
        3: counter = n_ar;
        4: counter = n_r;
        5: counter = n_rlast;
        6: counter = n_wpush;
        7: counter = n_rpush;
        8: counter = bvalid;
        9: counter = rvalid;
        10: counter = n_wpush - n_b;
        11: counter = n_rpush - n_rlast;
        12: counter = prev_b[1:0];
        13: counter = rresp;
        14: counter = n_wlast;
        15: counter = awvalid;
        16: counter = wvalid;
        17: counter = arvalid;
        18: counter = cyc;
        19: counter = n_cancel;
        default: counter = 0;
      endcase
    end
  endfunction

  task push_write;
    input [63:0] addr;
    input [7:0] len;
    input [2:0] size;
    input [1:0] burst;
    input [31:0] id;
    input lock;
    input [3:0] cache;
    input [2:0] prot;
    input [3:0] qos;
    input [7:0] strb_mode;
    input [31:0] salt;
    input [31:0] tag;
    integer slot;
    begin
      slot = w_tail % DEPTH;
      if (w_tail - w_head >= DEPTH) tb.nlv_fail_report("TB.QUEUE_FULL", NAME, w_tail);
      wq_addr[slot] = addr;
      wq_len[slot] = LITE ? 8'd0 : len;
      wq_size[slot] = size;
      wq_burst[slot] = burst;
      wq_id[slot] = id;
      wq_lock[slot] = lock;
      wq_cache[slot] = cache;
      wq_prot[slot] = prot;
      wq_qos[slot] = qos;
      wq_strb[slot] = strb_mode;
      wq_salt[slot] = salt;
      wq_keep[slot] = {ctl[KEEP0+3], ctl[KEEP0+2], ctl[KEEP0+1], ctl[KEEP0]};
      wq_tag[slot] = tag;
      w_tail = w_tail + 1;
      n_wpush = n_wpush + 1;
      $fwrite(tb.nlv_fd,
              "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"push\",\"ch\":\"w\",\"tag\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"id\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d}\n",
              ($realtime * 1000.0), cyc, NAME, tag, addr, len, size, burst, id, lock, cache,
              prot, qos);
    end
  endtask

  task push_read;
    input [63:0] addr;
    input [7:0] len;
    input [2:0] size;
    input [1:0] burst;
    input [31:0] id;
    input lock;
    input [3:0] cache;
    input [2:0] prot;
    input [3:0] qos;
    input [31:0] tag;
    integer slot;
    begin
      slot = r_tail % DEPTH;
      if (r_tail - ar_head >= DEPTH) tb.nlv_fail_report("TB.QUEUE_FULL", NAME, r_tail);
      rq_addr[slot] = addr;
      rq_len[slot] = LITE ? 8'd0 : len;
      rq_size[slot] = size;
      rq_burst[slot] = burst;
      rq_id[slot] = id;
      rq_lock[slot] = lock;
      rq_cache[slot] = cache;
      rq_prot[slot] = prot;
      rq_qos[slot] = qos;
      rq_tag[slot] = tag;
      r_tail = r_tail + 1;
      n_rpush = n_rpush + 1;
      $fwrite(tb.nlv_fd,
              "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"push\",\"ch\":\"r\",\"tag\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"id\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d}\n",
              ($realtime * 1000.0), cyc, NAME, tag, addr, len, size, burst, id, lock, cache,
              prot, qos);
    end
  endtask

  task set_ctl;
    input integer index;
    input [31:0] value;
    begin
      if (index == TPUT_CH) begin
        if (value != 0) begin
          tput_start = cyc;
          tput_count = 0;
        end else if (ctl[TPUT_CH] != 0) begin
          $fwrite(tb.nlv_fd,
                  "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"tput\",\"ch\":%0d,\"n\":%0d,\"cycles\":%0d}\n",
                  ($realtime * 1000.0), cyc, NAME, ctl[TPUT_CH], tput_count, cyc - tput_start);
          if (tput_count * ctl[TPUT_DEN] < (cyc - tput_start) * ctl[TPUT_NUM])
            tb.nlv_fail_report("CONTRACT.THROUGHPUT", NAME, tput_count);
        end
      end
      if (index == SEED) lfsr = value == 0 ? 32'h1 : value;
      if (index == R_MODE) begin r_b_base = n_b; r_r_base = n_r; end
      if (index == W_RG_N) w_rg_base = n_r;
      if (index >= 32) stab[(index - 32) % 64] = value;
      else ctl[index] = value;
    end
  endtask

  // The W beat at the head waits for R handshakes (W release gate).
  function w_r_gated;
    input integer unused;
    begin
      w_r_gated = ctl[W_RG_N] != 0 && w_beat >= ctl[W_RG_BEAT] &&
          n_r - w_rg_base < ctl[W_RG_N];
    end
  endfunction

  // Called by the tape interpreter when a wait times out. LIVE.E1: the
  // current write burst has its AW accepted and only part of its W beats
  // sent, the next W beat waits for R, and a read of this initiator is
  // unanswered (ARVALID held for at least min_hold cycles, or AR accepted
  // and its last R beat not received). LIVE.E2: RVALID held with RREADY low
  // for at least min_hold cycles while a write of this initiator waits for
  // its B.
  task stall_report;
    input integer min_hold;
    reg gated;
    begin
      gated = w_head < w_tail && w_r_gated(0);
      $display("NLV_STALL %0s aw=%0d/%0d hold=%0d w=%0d/%0d hold=%0d beat=%0d rgate=%0d ar=%0d/%0d hold=%0d b=%0d/%0d hold=%0d r=%0d/%0d hold=%0d w_out=%0d r_out=%0d",
               NAME, awvalid, awready, aw_hold, wvalid, wready, w_hold, w_beat, gated,
               arvalid, arready, ar_hold, bvalid, bready, b_wait, rvalid, rready, r_wait,
               n_wpush - n_b, n_rpush - n_rlast);
      $fwrite(tb.nlv_fd,
              "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"stall\",\"v\":\"%0d%0d%0d%0d%0d\",\"rdy\":\"%0d%0d%0d%0d%0d\",\"hold\":[%0d,%0d,%0d,%0d,%0d],\"beat\":%0d,\"rgate\":%0d,\"w_out\":%0d,\"r_out\":%0d}\n",
              ($realtime * 1000.0), cyc, NAME, awvalid, wvalid, arvalid, bvalid, rvalid,
              awready, wready, arready, bready, rready, aw_hold, w_hold, ar_hold, b_wait, r_wait,
              w_beat, gated, n_wpush - n_b, n_rpush - n_rlast);
      if (aw_head > w_head && gated && (arvalid && ar_hold >= min_hold || n_ar > n_rlast))
        tb.nlv_fail_report("LIVE.E1", NAME, w_beat);
      if (rvalid === 1'b1 && !rready && r_wait >= min_hold && n_wpush > n_b)
        tb.nlv_fail_report("LIVE.E2", NAME, r_wait);
    end
  endtask

  // Per-lane pseudo-random bit of strobe mode 4 (a hash of the beat state).
  function strobe_bit;
    input [31:0] seed;
    input integer lane;
    input integer keep;  // keep a lane with probability keep/16
    reg [31:0] h;
    begin
      h = seed ^ (lane * 32'h9e3779b1);
      h = h ^ (h >> 15);
      h = h * 32'h2c1b3c6d;
      h = h ^ (h >> 12);
      strobe_bit = h[3:0] < keep;
    end
  endfunction

  function rate_ok;
    input [31:0] percent;
    input [31:0] random;
    begin
      rate_ok = percent == 0 || (random % 100) < percent;
    end
  endfunction

  // Strobe table geometry: STAB_W 32-bit entries per beat (lanes above 32),
  // STAB_N entries in all.
  function integer stab_words;
    input integer unused;
    begin
      stab_words = ctl[STAB_W] == 0 ? 1 : ctl[STAB_W];
    end
  endfunction

  function integer stab_beats;
    input integer unused;
    begin
      stab_beats = (ctl[STAB_N] == 0 ? 1 : ctl[STAB_N]) / stab_words(0);
      if (stab_beats == 0) stab_beats = 1;
    end
  endfunction

  task load_w_beat;
    integer j, kind;
    reg [63:0] a;
    reg [31:0] h;
    begin
      k = w_head % DEPTH;
      a = nlv_beat_addr(wq_addr[k], wq_len[k], wq_size[k], wq_burst[k], w_beat);
      // An AXI4-Lite beat covers the whole bus; WSTRB alone selects bytes.
      lane_lo = LITE ? 0 : nlv_lane_lo(a, NB);
      lane_hi = LITE ? NB - 1 : nlv_lane_hi(a, wq_size[k], NB);
      wdata <= 0;
      wstrb <= 0;
      h = nlv_lfsr_next(lfsr ^ (n_w * 32'h85ebca6b));
      kind = h % 20;
      for (j = 0; j < NB; j = j + 1)
        if (j >= lane_lo && j <= lane_hi) begin
          wdata[j*8+:8] <= nlv_source_byte((a & ~(NB - 1)) + j, wq_salt[k]);
          case (wq_strb[k])
            8'd1: wstrb[j] <= ((w_beat + j) % 3) == 0;
            8'd2: wstrb[j] <= 1'b0;
            8'd3: wstrb[j] <= j == lane_lo;
            8'd4: wstrb[j] <= kind < 13 ? 1'b1 : kind < 16 ? strobe_bit(h, j, 15) :
                             kind < 19 ? strobe_bit(h, j, 8) : 1'b0;
            8'd5: wstrb[j] <= wq_keep[k][j];
            8'd6: wstrb[j] <= stab[(w_beat % stab_beats(0)) * stab_words(0) + j / 32][j % 32];
            default: wstrb[j] <= 1'b1;
          endcase
        end
      wid <= wq_id[k];
      wlast <= w_beat == wq_len[k];
      wvalid <= 1'b1;
    end
  endtask

  initial begin
    w_tail = 0; aw_head = 0; w_head = 0; w_beat = 0; r_tail = 0; ar_head = 0;
    for (i = 0; i < 32; i = i + 1) ctl[i] = 0;
    for (i = 0; i < 64; i = i + 1) stab[i] = 32'hffffffff;
    ctl[GATE_AW] = 1; ctl[GATE_W] = 1; ctl[GATE_AR] = 1;
    cyc = 0; n_aw = 0; n_w = 0; n_b = 0; n_ar = 0; n_r = 0; n_rlast = 0; n_wpush = 0;
    n_rpush = 0; n_wlast = 0; n_cancel = 0; b_wait = 0; r_wait = 0; lfsr = 32'h1;
    aw_gap_left = 0; w_gap_left = 0; ar_gap_left = 0;
    prev_bvalid = 0; prev_bready = 0; prev_rvalid = 0; prev_rready = 0; prev_rst = 0;
    prev_b = 0; prev_r = 0; prev_awvalid = 0; prev_wvalid = 0; prev_arvalid = 0;
    tput_start = 0; tput_count = 0;
    aw_hold = 0; w_hold = 0; ar_hold = 0; r_b_base = 0; r_r_base = 0; w_rg_base = 0;
    for (i = 0; i < NID; i = i + 1) begin
      b_expect[i] = 0; r_fifo_head[i] = 0; r_fifo_tail[i] = 0; r_left[i] = 0;
      b_fifo_head[i] = 0; b_fifo_tail[i] = 0;
    end
    w_done_before = 0;
    awvalid = 0; wvalid = 0; arvalid = 0; bready = 0; rready = 0;
    awid = 0; awaddr = 0; awlen = 0; awsize = 0; awburst = 0; awlock = 0; awcache = 0;
    awprot = 0; awqos = 0; wid = 0; wdata = 0; wstrb = 0; wlast = 0;
    arid = 0; araddr = 0; arlen = 0; arsize = 0; arburst = 0; arlock = 0; arcache = 0;
    arprot = 0; arqos = 0;
  end

  always @(posedge clk) begin
    cyc = cyc + 1;
    lfsr = nlv_lfsr_next(lfsr);
    if (!rst_n) begin
      if (prev_rst == 1'b1)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":0}\n",
                ($realtime * 1000.0), cyc, NAME);
      if (prev_rst == 1'b0 && cyc > 2 && (bvalid !== 1'b0 || rvalid !== 1'b0))
        tb.nlv_fail_report("AXI.RESET.QUIET", NAME, {bvalid, rvalid});
      if (n_wpush - n_b + n_rpush - n_rlast > 0 && prev_rst)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"cancel\",\"w\":%0d,\"r\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, n_wpush - n_b, n_rpush - n_rlast);
      if (prev_rst) n_cancel = n_cancel + (n_wpush - n_b) + (n_rpush - n_rlast);
      n_wpush = n_b;
      n_rpush = n_rlast;
      w_tail = 0; aw_head = 0; w_head = 0; w_beat = 0; r_tail = 0; ar_head = 0;
      for (i = 0; i < NID; i = i + 1) begin
        b_expect[i] = 0; r_fifo_head[i] = 0; r_fifo_tail[i] = 0; r_left[i] = 0;
        b_fifo_head[i] = 0; b_fifo_tail[i] = 0;
      end
      awvalid <= 0; wvalid <= 0; arvalid <= 0; bready <= 0; rready <= 0;
      prev_bvalid = 0; prev_rvalid = 0; b_wait = 0; r_wait = 0;
      aw_hold = 0; w_hold = 0; ar_hold = 0;
      aw_gap_left = 0; w_gap_left = 0; ar_gap_left = 0;
      prev_rst = 1'b0;
    end else begin
      if (prev_rst == 1'b0)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":1}\n",
                ($realtime * 1000.0), cyc, NAME);
      prev_rst = 1'b1;
      // ---- monitor: DUT-driven B and R ----
      if (bvalid !== 1'b0 && bvalid !== 1'b1) tb.nlv_fail_report("AXI.X.BVALID", NAME, 0);
      if (rvalid !== 1'b0 && rvalid !== 1'b1) tb.nlv_fail_report("AXI.X.RVALID", NAME, 0);
      if (prev_bvalid && !prev_bready && (!bvalid || {bid, observed_buser, bresp} !== prev_b))
        tb.nlv_fail_report("AXI.B.HOLD", NAME, prev_b);
      if (prev_rvalid && !prev_rready && (!rvalid || {rid, observed_ruser, rdata, rresp, rlast} !== prev_r))
        tb.nlv_fail_report("AXI.R.HOLD", NAME, 0);
      // AXI4 A3.3.1: BVALID only after the WLAST handshake of its write; the
      // W bursts completed before this edge are w_done_before.
      w_done_before = w_head;
      if (bvalid === 1'b1 && ^bid !== 1'bx && b_fifo_head[bid] != b_fifo_tail[bid])
        if (b_fifo_seq[bid][b_fifo_head[bid]%DEPTH] >= w_done_before)
          tb.nlv_fail_report("AXI.B.BEFORE_WLAST", NAME, bid);
      if (ctl[QUIET_B] != 0 && bvalid) tb.nlv_fail_report("CONTRACT.QUIET.B", NAME, bid);
      if (ctl[QUIET_R] != 0 && rvalid) tb.nlv_fail_report("CONTRACT.QUIET.R", NAME, rid);
      if (bvalid !== prev_bvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"b\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, bvalid);
      if (rvalid !== prev_rvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"r\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, rvalid);
      if (awvalid !== prev_awvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"aw\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, awvalid);
      if (wvalid !== prev_wvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"w\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, wvalid);
      if (arvalid !== prev_arvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"ar\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, arvalid);
      // ---- handshakes ----
      if (awvalid && awready) begin
        k = aw_head % DEPTH;
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"aw\",\"tag\":%0d,\"id\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, wq_tag[k], awid, awaddr, awlen, awsize,
                awburst, awlock, awcache, awprot, awqos);
        b_expect[awid] = b_expect[awid] + 1;
        b_fifo_seq[awid][b_fifo_tail[awid]%DEPTH] = aw_head;
        b_fifo_tail[awid] = b_fifo_tail[awid] + 1;
        aw_head = aw_head + 1;
        n_aw = n_aw + 1;
        aw_gap_left = ctl[AW_GAP];
        if (ctl[TPUT_CH] == 1) tput_count = tput_count + 1;
      end
      if (wvalid && wready) begin
        k = w_head % DEPTH;
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"w\",\"tag\":%0d,\"d\":\"%0h\",\"s\":\"%0h\",\"l\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, wq_tag[k], wdata, wstrb, wlast);
        n_w = n_w + 1;
        w_gap_left = ctl[W_GAP];
        if (ctl[TPUT_CH] == 2) tput_count = tput_count + 1;
        if (wlast) begin
          w_head = w_head + 1;
          w_beat = 0;
          n_wlast = n_wlast + 1;
        end else
          w_beat = w_beat + 1;
      end
      if (arvalid && arready) begin
        k = ar_head % DEPTH;
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"ar\",\"tag\":%0d,\"id\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, rq_tag[k], arid, araddr, arlen, arsize,
                arburst, arlock, arcache, arprot, arqos);
        r_fifo_len[arid][r_fifo_tail[arid]%DEPTH] = arlen + 1;
        r_fifo_tail[arid] = r_fifo_tail[arid] + 1;
        ar_head = ar_head + 1;
        n_ar = n_ar + 1;
        ar_gap_left = ctl[AR_GAP];
        if (ctl[TPUT_CH] == 3) tput_count = tput_count + 1;
      end
      if (bvalid && bready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"b\",\"id\":%0d,\"r\":%0d,\"u\":\"%0h\"}\n",
                ($realtime * 1000.0), cyc, NAME, bid, bresp, observed_buser);
        if (b_expect[bid] <= 0) tb.nlv_fail_report("AXI.B.UNEXPECTED", NAME, bid);
        else begin
          b_expect[bid] = b_expect[bid] - 1;
          b_fifo_head[bid] = b_fifo_head[bid] + 1;
        end
        n_b = n_b + 1;
        if (ctl[TPUT_CH] == 4) tput_count = tput_count + 1;
      end
      if (rvalid && rready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"r\",\"id\":%0d,\"d\":\"%0h\",\"r\":%0d,\"l\":%0d,\"u\":\"%0h\"}\n",
                ($realtime * 1000.0), cyc, NAME, rid, rdata, rresp, rlast, observed_ruser);
        id_i = rid;
        if (r_left[id_i] == 0) begin
          if (r_fifo_head[id_i] == r_fifo_tail[id_i]) tb.nlv_fail_report("AXI.R.UNEXPECTED", NAME, rid);
          else begin
            r_left[id_i] = r_fifo_len[id_i][r_fifo_head[id_i]%DEPTH];
            r_fifo_head[id_i] = r_fifo_head[id_i] + 1;
          end
        end
        if (r_left[id_i] > 0) begin
          r_left[id_i] = r_left[id_i] - 1;
          if (!LITE && rlast !== (r_left[id_i] == 0)) tb.nlv_fail_report("AXI.R.LAST", NAME, rid);
        end
        n_r = n_r + 1;
        if (LITE || rlast) n_rlast = n_rlast + 1;
        if (ctl[TPUT_CH] == 5) tput_count = tput_count + 1;
      end
      // ---- next cycle drive ----
      if (aw_gap_left > 0) aw_gap_left = aw_gap_left - 1;
      if (w_gap_left > 0) w_gap_left = w_gap_left - 1;
      if (ar_gap_left > 0) ar_gap_left = ar_gap_left - 1;
      if (!awvalid || awready) begin
        if (aw_head < w_tail && ctl[GATE_AW] != 0 && aw_gap_left == 0 &&
            rate_ok(ctl[VRATE_AW], lfsr >> 3)) begin
          k = aw_head % DEPTH;
          awid <= wq_id[k];
          awaddr <= wq_addr[k];
          awlen <= wq_len[k];
          awsize <= wq_size[k];
          awburst <= wq_burst[k];
          awlock <= wq_lock[k];
          awcache <= wq_cache[k];
          awprot <= wq_prot[k];
          awqos <= wq_qos[k];
          awvalid <= 1'b1;
        end else
          awvalid <= 1'b0;
      end
      if (!wvalid || wready) begin
        if (w_head < w_tail && ctl[GATE_W] != 0 && w_gap_left == 0 &&
            (ctl[W_AFTER_AW] == 0 || w_head < aw_head) && !w_r_gated(0) &&
            rate_ok(ctl[VRATE_W], lfsr >> 13))
          load_w_beat;
        else
          wvalid <= 1'b0;
      end
      if (!arvalid || arready) begin
        if (ar_head < r_tail && ctl[GATE_AR] != 0 && ar_gap_left == 0 &&
            rate_ok(ctl[VRATE_AR], lfsr >> 17)) begin
          k = ar_head % DEPTH;
          arid <= rq_id[k];
          araddr <= rq_addr[k];
          arlen <= rq_len[k];
          arsize <= rq_size[k];
          arburst <= rq_burst[k];
          arlock <= rq_lock[k];
          arcache <= rq_cache[k];
          arprot <= rq_prot[k];
          arqos <= rq_qos[k];
          arvalid <= 1'b1;
        end else
          arvalid <= 1'b0;
      end
      b_wait = (bvalid && !(bvalid && bready)) ? b_wait + 1 : 0;
      r_wait = (rvalid && !(rvalid && rready)) ? r_wait + 1 : 0;
      aw_hold = (awvalid && !awready) ? aw_hold + 1 : 0;
      w_hold = (wvalid && !wready) ? w_hold + 1 : 0;
      ar_hold = (arvalid && !arready) ? ar_hold + 1 : 0;
      bready <= nlv_ready_pattern(ctl[B_MODE], ctl[B_A1], ctl[B_A2], cyc, b_wait, lfsr);
      if (ctl[R_MODE] == R_AWAIT_B)
        rready <= n_b - r_b_base >= ctl[R_A1];
      else if (ctl[R_MODE] == R_BEATS)
        rready <= n_r - r_r_base < ctl[R_A1];
      else
        rready <= nlv_ready_pattern(ctl[R_MODE], ctl[R_A1], ctl[R_A2], cyc, r_wait, lfsr >> 7);
    end
    prev_bvalid = bvalid;
    prev_bready = bready;
    prev_b = {bid, observed_buser, bresp};
    prev_rvalid = rvalid;
    prev_rready = rready;
    prev_r = {rid, observed_ruser, rdata, rresp, rlast};
    prev_awvalid = awvalid;
    prev_wvalid = wvalid;
    prev_arvalid = arvalid;
  end
endmodule
