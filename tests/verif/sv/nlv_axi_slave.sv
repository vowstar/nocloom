`timescale 1ns/1ps
// AXI4 / AXI4-Lite target-side BFM: answers a DUT master port (a NocLoom
// target endpoint) from a byte memory. AW, W and AR ready follow per-channel
// patterns and tape gates; writes complete when an AW has all its beats;
// B and R return after a latency, under tape gates, oldest or youngest first
// among the transactions with no older unanswered one of the same ID (AXI
// keeps one ID in order).
// Responses come from an override table indexed by response count since the
// last base mark, a two-entry address region table, or a default code.
// Built-in monitor: VALID/payload hold on AW, W and AR, reset quiet, X on
// VALID, WLAST position, contract quiet windows, an outstanding cap, a
// throughput window and the timeout gap of a sideband status input.
// W ready mode 6 holds WREADY low before the last beat of each burst whose
// AW is known, for W_A1 + (burst number % W_A2) cycles of offered WVALID
// (a target that stalls each burst's last word).
// B_RATE and R_RATE (percent, 0 = always) raise a ready B or R beat only on
// that share of cycles (random response backpressure). With LOCK_OK set
// every locked (exclusive) transaction answers EXOKAY: a target that keeps
// its own exclusive monitor and finds every pair intact. With ERR_ON set, a
// write with a beat at address ERR_ADDR answers SLVERR, and so does a read
// beat at ERR_ADDR (one failed target beat). Protocol errors on
// purpose, for a DUT that must reject them: ID_XOR flips BID and RID by that
// mask, RLAST_FLIP = k + 1 inverts RLAST on beat k of every read. The event
// log keeps the intended BID and RID, so the offline pairing stays exact.
// W release by reads (liveness negative controls): with W_RG_N nonzero,
// WREADY stays low until W_RG_N last R beats were sent since W_RG_N was set.
// R_ILV set interleaves read data of different IDs beat by beat: each R beat
// goes to the oldest ready read (no older unanswered one of its ID) of another
// ID than the previous beat's, else to the oldest ready read, and every read
// keeps its own beat count (AXI A5.3: data of different IDs may interleave).
// R_ILV above one keeps that many beats of a read together before the next pick.
// stall_report, called when a tape wait times out, prints the channel state
// and raises LIVE.E3 when this target holds W against its own reads.
module nlv_axi_slave #(
    parameter NAME = "t",
    parameter ID_W = 4,
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter LEN_W = 8,
    parameter LOCK_W = 1,
    parameter USER_W = 1,
    parameter USER_PRESENT = 0,
    parameter DEPTH = 64,
    parameter LITE = 0,
    parameter MEM_BYTES = 65536,
    parameter SALT = 0
) (
    input wire clk,
    input wire rst_n,
    input wire [ID_W-1:0] awid,
    input wire [ADDR_W-1:0] awaddr,
    input wire [LEN_W-1:0] awlen,
    input wire [2:0] awsize,
    input wire [1:0] awburst,
    input wire [LOCK_W-1:0] awlock,
    input wire [3:0] awcache,
    input wire [2:0] awprot,
    input wire [3:0] awqos,
    input wire [3:0] awregion,
    input wire awvalid,
    output reg awready,
    input wire [ID_W-1:0] wid,
    input wire [DATA_W-1:0] wdata,
    input wire [DATA_W/8-1:0] wstrb,
    input wire wlast,
    input wire wvalid,
    output reg wready,
    output reg [ID_W-1:0] bid,
    output reg [1:0] bresp,
    output reg [USER_W-1:0] buser,
    output reg bvalid,
    input wire bready,
    input wire [ID_W-1:0] arid,
    input wire [ADDR_W-1:0] araddr,
    input wire [LEN_W-1:0] arlen,
    input wire [2:0] arsize,
    input wire [1:0] arburst,
    input wire [LOCK_W-1:0] arlock,
    input wire [3:0] arcache,
    input wire [2:0] arprot,
    input wire [3:0] arqos,
    input wire [3:0] arregion,
    input wire arvalid,
    output reg arready,
    output reg [ID_W-1:0] rid,
    output reg [DATA_W-1:0] rdata,
    output reg [1:0] rresp,
    output reg [USER_W-1:0] ruser,
    output reg rlast,
    output reg rvalid,
    input wire rready,
    input wire sideband
);
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  localparam [2:0] LITE_SIZE = NB == 8 ? 3'd3 : NB == 4 ? 3'd2 : NB == 2 ? 3'd1 : 3'd0;

  reg [7:0] mem[0:MEM_BYTES-1];
  // Accepted write descriptors and their completion state.
  reg [63:0] aq_addr[0:DEPTH-1];
  reg [7:0] aq_len[0:DEPTH-1];
  reg [2:0] aq_size[0:DEPTH-1];
  reg [1:0] aq_burst[0:DEPTH-1];
  reg [ID_W-1:0] aq_id[0:DEPTH-1];
  reg [63:0] aq_ready_at[0:DEPTH-1];
  reg [1:0] aq_code[0:DEPTH-1];
  reg aq_lock[0:DEPTH-1], rq_lock[0:DEPTH-1], aq_err[0:DEPTH-1];
  reg aq_done[0:DEPTH-1], aq_answered[0:DEPTH-1];
  integer aw_tail, aw_apply, apply_beat;
  // Buffered W beats.
  reg [DATA_W-1:0] wb_data[0:DEPTH-1];
  reg [DATA_W/8-1:0] wb_strb[0:DEPTH-1];
  reg wb_last[0:DEPTH-1];
  integer wb_tail, wb_head, w_bursts, b_since, w_in_beat, w_stall;
  // Accepted reads.
  reg [63:0] rq_addr[0:DEPTH-1];
  reg [7:0] rq_len[0:DEPTH-1];
  reg [2:0] rq_size[0:DEPTH-1];
  reg [1:0] rq_burst[0:DEPTH-1];
  reg [ID_W-1:0] rq_id[0:DEPTH-1];
  reg [63:0] rq_ready_at[0:DEPTH-1];
  reg rq_done[0:DEPTH-1];
  integer ar_tail, r_cur, r_beat;
  // R_ILV: each read's beats sent, and the ID of the last beat sent.
  integer rq_beat[0:DEPTH-1];
  reg [ID_W-1:0] r_last_id;
  reg r_any;

  reg [31:0] ctl[0:79];
  localparam AW_MODE = 0, AW_A1 = 1, AW_A2 = 2, W_MODE = 3, W_A1 = 4, W_A2 = 5, AR_MODE = 6,
      AR_A1 = 7, AR_A2 = 8, GATE_B = 9, GATE_R = 10, B_LAT = 11, R_LAT = 12, CAP_W = 13,
      CAP_R = 14, B_DEFAULT = 15, R_DEFAULT = 16, B_OVR_I = 17, B_OVR_C = 21, R_OVR_I = 25,
      R_OVR_C = 29, B_ORDER = 33, R_ORDER = 34, QUIET_AW = 35, QUIET_W = 36, QUIET_AR = 37,
      GAP_DIR = 38, GAP_LO = 39, GAP_HI = 40, BASE = 41, OUT_CAP = 42, REG0_LO = 43,
      REG0_HI = 44, REG0_W = 45, REG0_R = 46, REG1_LO = 47, REG1_HI = 48, REG1_W = 49,
      REG1_R = 50, TPUT_CH = 51, TPUT_NUM = 52, TPUT_DEN = 53, SEED = 54, ERRM0_MASK = 55,
      ERRM0_MATCH = 56, ERRM0_CODE = 57, ERRM1_MASK = 58, ERRM1_MATCH = 59, ERRM1_CODE = 60,
      W_RG_N = 61, B_RATE = 62, R_RATE = 63, LOCK_OK = 64, ID_XOR = 65, RLAST_FLIP = 66,
      ERR_ADDR = 67, ERR_ON = 68, FIFO_EN = 69, FIFO_ADDR = 70, W_STOP = 71, R_STOP = 72,
      R_ILV = 73, R_USER_BASE = 74, R_USER_STEP = 75, B_USER_BASE = 76;

  reg [63:0] cyc, n_aw, n_w, n_b, n_ar, n_r, n_rlast, n_wlast, n_applied, peak_w, peak_r;
  reg [63:0] base_b, base_r, last_wprog, last_rprog, tput_start, tput_count;
  reg gap_active;
  reg [63:0] gap_start;
  reg [31:0] lfsr, aw_wait, w_wait, ar_wait, b_hold, r_hold;
  reg [63:0] w_rg_base, w_stop_base, r_stop_base;
  reg prev_awvalid, prev_awready, prev_wvalid, prev_wready, prev_arvalid, prev_arready;
  reg prev_bvalid, prev_rvalid, prev_sb, prev_rst;
  reg [ID_W+ADDR_W+LEN_W+LOCK_W+19:0] prev_aw, prev_ar;
  reg [DATA_W+DATA_W/8:0] prev_w;
  integer i, j, k, pick;
  reg [63:0] a, la;
  reg [1:0] code;

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
        6: counter = n_aw - n_b;
        7: counter = n_ar - n_rlast;
        8: counter = awvalid;
        9: counter = wvalid;
        10: counter = arvalid;
        11: counter = bvalid;
        12: counter = rvalid;
        13: counter = peak_w;
        14: counter = peak_r;
        15: counter = sideband;
        16: counter = n_wlast;
        17: counter = n_applied;
        18: counter = bready;
        19: counter = rready;
        20: counter = cyc;
        default: counter = 0;
      endcase
    end
  endfunction

  function [1:0] region_code;
    input [63:0] addr;
    input write;
    begin
      if (ctl[REG0_HI] > ctl[REG0_LO] && addr >= ctl[REG0_LO] && addr < ctl[REG0_HI])
        region_code = write ? ctl[REG0_W] : ctl[REG0_R];
      else if (ctl[REG1_HI] > ctl[REG1_LO] && addr >= ctl[REG1_LO] && addr < ctl[REG1_HI])
        region_code = write ? ctl[REG1_W] : ctl[REG1_R];
      else
        region_code = write ? ctl[B_DEFAULT] : ctl[R_DEFAULT];
      // Address-bit rules apply to both directions (e.g. bit 8 set).
      if (ctl[ERRM0_MASK] != 0 && (addr & ctl[ERRM0_MASK]) == ctl[ERRM0_MATCH])
        region_code = ctl[ERRM0_CODE];
      else if (ctl[ERRM1_MASK] != 0 && (addr & ctl[ERRM1_MASK]) == ctl[ERRM1_MATCH])
        region_code = ctl[ERRM1_CODE];
    end
  endfunction

  function [1:0] b_code;
    input [63:0] index;
    input [63:0] addr;
    integer e;
    begin
      b_code = region_code(addr, 1'b1);
      for (e = 0; e < 4; e = e + 1)
        if (ctl[B_OVR_I+e] != 0 && ctl[B_OVR_I+e] - 1 == index) b_code = ctl[B_OVR_C+e];
    end
  endfunction

  function [1:0] r_code;
    input [63:0] index;
    input [63:0] addr;
    integer e;
    begin
      r_code = region_code(addr, 1'b0);
      for (e = 0; e < 4; e = e + 1)
        if (ctl[R_OVR_I+e] != 0 && ctl[R_OVR_I+e] - 1 == index) r_code = ctl[R_OVR_C+e];
    end
  endfunction

  // An older accepted write of the same ID still waits for its B.
  function b_blocked;
    input integer index;
    integer o;
    begin
      b_blocked = 0;
      for (o = index - 1; o >= 0 && o >= aw_apply - DEPTH; o = o - 1)
        if (!aq_answered[o%DEPTH] && aq_id[o%DEPTH] == aq_id[index%DEPTH]) b_blocked = 1;
    end
  endfunction

  // An older accepted read of the same ID has not returned its last beat.
  function r_blocked;
    input integer index;
    integer o;
    begin
      r_blocked = 0;
      for (o = index - 1; o >= 0 && o >= ar_tail - DEPTH; o = o - 1)
        if (!rq_done[o%DEPTH] && rq_id[o%DEPTH] == rq_id[index%DEPTH]) r_blocked = 1;
    end
  endfunction

  task set_ctl;
    input integer index;
    input [31:0] value;
    begin
      if (index == BASE) begin
        base_b = n_applied;
        base_r = n_r;
      end
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
      if (index == W_RG_N) w_rg_base = n_rlast;
      if (index == W_STOP) w_stop_base = n_w;
      if (index == R_STOP) r_stop_base = n_r;
      ctl[index] = value;
    end
  endtask

  // A target that stops answering: R_STOP = k + 1 offers no R beat after k
  // beats since it was set (W_STOP likewise takes no W beat); 0 disables.
  function r_stopped;
    input integer unused;
    begin
      r_stopped = ctl[R_STOP] != 0 && n_r - r_stop_base >= ctl[R_STOP] - 1;
    end
  endfunction

  // WREADY waits for read completions of this target (W release gate).
  function w_r_gated;
    input integer unused;
    begin
      w_r_gated = ctl[W_RG_N] != 0 && n_rlast - w_rg_base < ctl[W_RG_N];
    end
  endfunction

  // Called by the tape interpreter when a wait times out. LIVE.E3: WVALID
  // held with WREADY low for at least min_hold cycles while this target
  // waits for its own read completions (the W release gate is closed), or
  // while it has reads in flight and its write capacity is not what holds
  // WREADY.
  task stall_report;
    input integer min_hold;
    reg cap_ok;
    begin
      cap_ok = w_bursts - b_since < ctl[CAP_W] && wb_tail - wb_head < DEPTH - 1;
      $display("NLV_STALL %0s aw=%0d/%0d hold=%0d w=%0d/%0d hold=%0d rgate=%0d ar=%0d/%0d hold=%0d b=%0d/%0d hold=%0d r=%0d/%0d hold=%0d w_pend=%0d r_pend=%0d",
               NAME, awvalid, awready, aw_wait, wvalid, wready, w_wait, w_r_gated(0),
               arvalid, arready, ar_wait, bvalid, bready, b_hold, rvalid, rready, r_hold,
               n_aw - n_b, n_ar - n_rlast);
      $fwrite(tb.nlv_fd,
              "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"stall\",\"v\":\"%0d%0d%0d%0d%0d\",\"rdy\":\"%0d%0d%0d%0d%0d\",\"hold\":[%0d,%0d,%0d,%0d,%0d],\"rgate\":%0d,\"w_pend\":%0d,\"r_pend\":%0d}\n",
              ($realtime * 1000.0), cyc, NAME, awvalid, wvalid, arvalid, bvalid, rvalid,
              awready, wready, arready, bready, rready, aw_wait, w_wait, ar_wait, b_hold, r_hold,
              w_r_gated(0), n_aw - n_b, n_ar - n_rlast);
      if (wvalid === 1'b1 && !wready && w_wait >= min_hold &&
          (w_r_gated(0) || n_ar > n_rlast && cap_ok))
        tb.nlv_fail_report("LIVE.E3", NAME, w_wait);
    end
  endtask

  task clear;
    begin
      aw_tail = 0; aw_apply = 0; apply_beat = 0; wb_tail = 0; wb_head = 0; w_bursts = 0; b_since = 0;
      w_in_beat = 0; w_stall = 0;
      gap_active = 0; gap_start = 0;
      ar_tail = 0; r_cur = -1; r_beat = 0; r_any = 0; r_last_id = 0;
      for (i = 0; i < DEPTH; i = i + 1) begin
        aq_done[i] = 0; aq_answered[i] = 0; rq_done[i] = 0;
      end
    end
  endtask

  initial begin
    for (i = 0; i < 80; i = i + 1) ctl[i] = 0;
    ctl[CAP_W] = DEPTH; ctl[CAP_R] = DEPTH; ctl[GATE_B] = 1; ctl[GATE_R] = 1;
    for (i = 0; i < MEM_BYTES; i = i + 1) mem[i] = nlv_target_byte(i, SALT);
    clear;
    cyc = 0; n_aw = 0; n_w = 0; n_b = 0; n_ar = 0; n_r = 0; n_rlast = 0; n_wlast = 0;
    n_applied = 0; peak_w = 0; peak_r = 0; base_b = 0; base_r = 0; last_wprog = 0;
    last_rprog = 0; tput_start = 0; tput_count = 0; lfsr = 32'h1;
    aw_wait = 0; w_wait = 0; ar_wait = 0; b_hold = 0; r_hold = 0; w_rg_base = 0;
    w_stop_base = 0; r_stop_base = 0;
    prev_awvalid = 0; prev_awready = 0; prev_wvalid = 0; prev_wready = 0; prev_arvalid = 0;
    prev_arready = 0; prev_bvalid = 0; prev_rvalid = 0; prev_sb = 0; prev_rst = 0;
    prev_aw = 0; prev_ar = 0; prev_w = 0;
    awready = 0; wready = 0; arready = 0; bvalid = 0; rvalid = 0;
    bid = 0; bresp = 0; buser = 0; ruser = 0; rid = 0; rdata = 0; rresp = 0; rlast = 0;
  end

  always @(posedge clk) begin
    cyc = cyc + 1;
    lfsr = nlv_lfsr_next(lfsr);
    if (!rst_n) begin
      if (prev_rst == 1'b1)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":0}\n",
                ($realtime * 1000.0), cyc, NAME);
      if (prev_rst == 1'b0 && cyc > 2 &&
          (awvalid !== 1'b0 || wvalid !== 1'b0 || arvalid !== 1'b0))
        tb.nlv_fail_report("AXI.RESET.QUIET", NAME, {awvalid, wvalid, arvalid});
      clear;
      // Pending counts restart: the common reset cancels everything in flight.
      n_b = n_aw;
      n_rlast = n_ar;
      awready <= 0; wready <= 0; arready <= 0; bvalid <= 0; rvalid <= 0;
      prev_awvalid = 0; prev_wvalid = 0; prev_arvalid = 0;
      prev_bvalid = 0; prev_rvalid = 0;
      prev_rst = 1'b0;
    end else begin
      if (prev_rst == 1'b0)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":1}\n",
                ($realtime * 1000.0), cyc, NAME);
      prev_rst = 1'b1;
      // ---- monitor: DUT-driven AW, W, AR ----
      if (awvalid !== 1'b0 && awvalid !== 1'b1) tb.nlv_fail_report("AXI.X.AWVALID", NAME, 0);
      if (wvalid !== 1'b0 && wvalid !== 1'b1) tb.nlv_fail_report("AXI.X.WVALID", NAME, 0);
      if (arvalid !== 1'b0 && arvalid !== 1'b1) tb.nlv_fail_report("AXI.X.ARVALID", NAME, 0);
      if (prev_awvalid && !prev_awready &&
          (!awvalid || {awid, awaddr, awlen, awsize, awburst, awlock, awcache, awprot, awqos, awregion} !== prev_aw))
        tb.nlv_fail_report("AXI.AW.HOLD", NAME, awvalid);
      if (prev_wvalid && !prev_wready && (!wvalid || {wdata, wstrb, wlast} !== prev_w))
        tb.nlv_fail_report("AXI.W.HOLD", NAME, wvalid);
      if (prev_arvalid && !prev_arready &&
          (!arvalid || {arid, araddr, arlen, arsize, arburst, arlock, arcache, arprot, arqos, arregion} !== prev_ar))
        tb.nlv_fail_report("AXI.AR.HOLD", NAME, arvalid);
      if (ctl[QUIET_AW] != 0 && awvalid) tb.nlv_fail_report("CONTRACT.QUIET.AW", NAME, awaddr);
      if (ctl[QUIET_W] != 0 && wvalid) tb.nlv_fail_report("CONTRACT.QUIET.W", NAME, 0);
      if (ctl[QUIET_AR] != 0 && arvalid) tb.nlv_fail_report("CONTRACT.QUIET.AR", NAME, araddr);
      if (awvalid !== prev_awvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"aw\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, awvalid);
      if (wvalid !== prev_wvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"w\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, wvalid);
      if (arvalid !== prev_arvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"ar\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, arvalid);
      if (bvalid !== prev_bvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"b\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, bvalid);
      if (rvalid !== prev_rvalid)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"r\",\"v\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, rvalid);
      if (sideband !== prev_sb) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"sb\",\"n\":\"status\",\"v\":%0d,\"gw\":%0d,\"gr\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, sideband, cyc - last_wprog, cyc - last_rprog);
        if (sideband === 1'b1 && ctl[GAP_DIR] != 0) begin
          a = ctl[GAP_DIR] == 1 ? last_wprog : last_rprog;
          if (gap_active && a < gap_start) a = gap_start;
          a = cyc - a;
          if (a < ctl[GAP_LO] || a > ctl[GAP_HI])
            tb.nlv_fail_report("CONTRACT.TIMEOUT.GAP", NAME, a);
        end
      end
      // An enabled timeout contract also detects a status that never rises.
      // Start at the first pending request, including an unaccepted address,
      // and restart the interval on each handshake in the selected direction.
      if (ctl[GAP_DIR] == 0 || sideband ||
          (ctl[GAP_DIR] == 1 ? !(awvalid || wvalid || n_aw > n_b) :
                               !(arvalid || n_ar > n_rlast))) begin
        gap_active = 0;
      end else begin
        if (!gap_active) begin gap_active = 1; gap_start = cyc; end
        a = ctl[GAP_DIR] == 1 ? last_wprog : last_rprog;
        if (a < gap_start) a = gap_start;
        if (cyc - a > ctl[GAP_HI])
          tb.nlv_fail_report("CONTRACT.TIMEOUT.GAP", NAME, cyc - a);
      end
      // ---- handshakes ----
      if (awvalid && awready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"aw\",\"id\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d,\"rg\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, awid, awaddr, LITE ? 8'd0 : awlen,
                LITE ? LITE_SIZE : awsize, LITE ? 2'd1 : awburst, awlock,
                awcache, awprot, awqos, awregion);
        k = aw_tail % DEPTH;
        aq_addr[k] = awaddr;
        aq_len[k] = LITE ? 8'd0 : awlen;
        aq_size[k] = LITE ? LITE_SIZE : awsize;
        aq_burst[k] = LITE ? 2'd1 : awburst;
        aq_id[k] = awid;
        aq_lock[k] = awlock;
        aq_err[k] = 0;
        aq_done[k] = 0;
        aq_answered[k] = 0;
        aw_tail = aw_tail + 1;
        n_aw = n_aw + 1;
        last_wprog = cyc;
        if (n_aw - n_b > peak_w) peak_w = n_aw - n_b;
        if (ctl[OUT_CAP] != 0 && n_aw - n_b > ctl[OUT_CAP])
          tb.nlv_fail_report("CONTRACT.OUTSTANDING.W", NAME, n_aw - n_b);
        if (ctl[TPUT_CH] == 1) tput_count = tput_count + 1;
      end
      if (wvalid && wready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"w\",\"d\":\"%0h\",\"s\":\"%0h\",\"l\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, wdata, wstrb, LITE ? 1'b1 : wlast);
        k = wb_tail % DEPTH;
        wb_data[k] = wdata;
        wb_strb[k] = wstrb;
        wb_last[k] = LITE ? 1'b1 : wlast;
        wb_tail = wb_tail + 1;
        n_w = n_w + 1;
        if (LITE || wlast) begin
          n_wlast = n_wlast + 1;
          w_bursts = w_bursts + 1;
          w_in_beat = 0;
          w_stall = 0;
        end else
          w_in_beat = w_in_beat + 1;
        last_wprog = cyc;
        if (ctl[TPUT_CH] == 2) tput_count = tput_count + 1;
      end
      if (arvalid && arready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"ar\",\"id\":%0d,\"a\":\"%0h\",\"len\":%0d,\"sz\":%0d,\"bu\":%0d,\"lk\":%0d,\"ca\":%0d,\"pr\":%0d,\"qo\":%0d,\"rg\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, arid, araddr, LITE ? 8'd0 : arlen,
                LITE ? LITE_SIZE : arsize, LITE ? 2'd1 : arburst, arlock,
                arcache, arprot, arqos, arregion);
        k = ar_tail % DEPTH;
        rq_addr[k] = araddr;
        rq_len[k] = LITE ? 8'd0 : arlen;
        rq_size[k] = LITE ? LITE_SIZE : arsize;
        rq_burst[k] = LITE ? 2'd1 : arburst;
        rq_id[k] = arid;
        rq_lock[k] = arlock;
        rq_ready_at[k] = cyc + ctl[R_LAT];
        rq_done[k] = 0;
        rq_beat[k] = 0;
        ar_tail = ar_tail + 1;
        n_ar = n_ar + 1;
        last_rprog = cyc;
        if (n_ar - n_rlast > peak_r) peak_r = n_ar - n_rlast;
        if (ctl[OUT_CAP] != 0 && n_ar - n_rlast > ctl[OUT_CAP])
          tb.nlv_fail_report("CONTRACT.OUTSTANDING.R", NAME, n_ar - n_rlast);
        if (ctl[TPUT_CH] == 3) tput_count = tput_count + 1;
      end
      if (bvalid && bready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"b\",\"id\":%0d,\"r\":%0d,\"u\":\"%0h\"}\n",
                ($realtime * 1000.0), cyc, NAME, bid ^ ctl[ID_XOR][ID_W-1:0], bresp, buser);
        n_b = n_b + 1;
        b_since = b_since + 1;
        last_wprog = cyc;
        if (ctl[TPUT_CH] == 4) tput_count = tput_count + 1;
      end
      if (rvalid && rready) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"r\",\"id\":%0d,\"d\":\"%0h\",\"r\":%0d,\"l\":%0d,\"u\":\"%0h\"}\n",
                ($realtime * 1000.0), cyc, NAME, rid ^ ctl[ID_XOR][ID_W-1:0], rdata, rresp, LITE ? 1'b1 : rlast, ruser);
        n_r = n_r + 1;
        last_rprog = cyc;
        // A FIFO word: every read beat of it returns a new value.
        if (ctl[FIFO_EN] != 0 && r_cur >= 0) begin
          a = nlv_beat_addr(rq_addr[r_cur%DEPTH], rq_len[r_cur%DEPTH], rq_size[r_cur%DEPTH],
                            rq_burst[r_cur%DEPTH], r_beat);
          la = a & ~(NB - 1);
          if (la == ctl[FIFO_ADDR])
            for (i = 0; i < NB; i = i + 1) mem[(la + i) % MEM_BYTES] = mem[(la + i) % MEM_BYTES] + 1;
        end
        if (ctl[TPUT_CH] == 5) tput_count = tput_count + 1;
        r_last_id = rq_id[r_cur % DEPTH];
        r_any = 1;
        if (LITE || r_beat == rq_len[r_cur % DEPTH]) begin
          n_rlast = n_rlast + 1;
          rq_done[r_cur % DEPTH] = 1;
          r_cur = -1;
          r_beat = 0;
        end else if (ctl[R_ILV] != 0 && (r_beat + 1) % ctl[R_ILV] == 0) begin
          rq_beat[r_cur % DEPTH] = r_beat + 1;
          r_cur = -1;
          r_beat = 0;
        end else
          r_beat = r_beat + 1;
      end
      // ---- write engine: match buffered beats with accepted AWs ----
      while (aw_apply < aw_tail && wb_head < wb_tail) begin
        k = aw_apply % DEPTH;
        j = wb_head % DEPTH;
        a = nlv_beat_addr(aq_addr[k], aq_len[k], aq_size[k], aq_burst[k], apply_beat);
        la = a & ~(NB - 1);
        if (ctl[ERR_ON] != 0 && a == ctl[ERR_ADDR]) aq_err[k] = 1;
        for (i = 0; i < NB; i = i + 1)
          if (wb_strb[j][i]) mem[(la + i) % MEM_BYTES] = wb_data[j][i*8+:8];
        if (!LITE && wb_last[j] !== (apply_beat == aq_len[k]))
          tb.nlv_fail_report("AXI.W.LAST", NAME, apply_beat);
        wb_head = wb_head + 1;
        if (apply_beat == aq_len[k] || wb_last[j]) begin
          aq_done[k] = 1;
          aq_ready_at[k] = cyc + ctl[B_LAT];
          aq_code[k] = ctl[LOCK_OK] != 0 && aq_lock[k] ? 2'd1 : aq_err[k] ? 2'd2 :
                       b_code(n_applied - base_b, aq_addr[k]);
          n_applied = n_applied + 1;
          aw_apply = aw_apply + 1;
          apply_beat = 0;
        end else
          apply_beat = apply_beat + 1;
      end
      // ---- B channel ----
      if (!bvalid || bready) begin
        pick = -1;
        if (ctl[GATE_B] != 0 && (ctl[B_RATE] == 0 || ((lfsr >> 19) % 100) < ctl[B_RATE]))
          for (i = aw_apply - 1; i >= 0 && i >= aw_apply - DEPTH; i = i - 1)
            if (aq_done[i%DEPTH] && !aq_answered[i%DEPTH] && aq_ready_at[i%DEPTH] <= cyc &&
                (pick < 0 || ctl[B_ORDER] == 0) && !b_blocked(i))
              pick = i;
        if (pick >= 0) begin
          k = pick % DEPTH;
          aq_answered[k] = 1;
          bid <= aq_id[k] ^ ctl[ID_XOR][ID_W-1:0];
          bresp <= aq_code[k];
          buser <= USER_PRESENT ? ctl[B_USER_BASE] : 0;
          bvalid <= 1'b1;
        end else
          bvalid <= 1'b0;
      end
      // ---- R channel ----
      if (!rvalid || rready) begin
        if (r_cur < 0 && ctl[GATE_R] != 0 && ctl[R_ILV] != 0) begin
          pick = -1;
          for (i = ar_tail - 1; i >= 0 && i >= ar_tail - DEPTH; i = i - 1)
            if (!rq_done[i%DEPTH] && rq_ready_at[i%DEPTH] <= cyc && !r_blocked(i)) begin
              r_cur = i;
              if (!r_any || rq_id[i%DEPTH] != r_last_id) pick = i;
            end
          if (pick >= 0) r_cur = pick;
          if (r_cur >= 0) r_beat = rq_beat[r_cur % DEPTH];
        end else if (r_cur < 0 && ctl[GATE_R] != 0) begin
          for (i = ar_tail - 1; i >= 0 && i >= ar_tail - DEPTH; i = i - 1)
            if (!rq_done[i%DEPTH] && rq_ready_at[i%DEPTH] <= cyc &&
                (r_cur < 0 || ctl[R_ORDER] == 0) && !r_blocked(i))
              r_cur = i;
        end
        if (r_cur >= 0 && (ctl[GATE_R] != 0 || r_beat > 0) && !r_stopped(0) &&
            (ctl[R_RATE] == 0 || ((lfsr >> 23) % 100) < ctl[R_RATE])) begin
          k = r_cur % DEPTH;
          a = nlv_beat_addr(rq_addr[k], rq_len[k], rq_size[k], rq_burst[k], r_beat);
          la = a & ~(NB - 1);
          for (i = 0; i < NB; i = i + 1) rdata[i*8+:8] <= mem[(la + i) % MEM_BYTES];
          rid <= rq_id[k] ^ ctl[ID_XOR][ID_W-1:0];
          ruser <= USER_PRESENT ? ctl[R_USER_BASE] + n_r * ctl[R_USER_STEP] : 0;
          rresp <= ctl[LOCK_OK] != 0 && rq_lock[k] ? 2'd1 :
                   ctl[ERR_ON] != 0 && a == ctl[ERR_ADDR] ? 2'd2 : r_code(n_r - base_r, a);
          rlast <= (r_beat == rq_len[k]) ^ (ctl[RLAST_FLIP] != 0 && r_beat == ctl[RLAST_FLIP] - 1);
          rvalid <= 1'b1;
        end else
          rvalid <= 1'b0;
      end
      // ---- ready for the next cycle ----
      aw_wait = (awvalid && !(awvalid && awready)) ? aw_wait + 1 : 0;
      w_wait = (wvalid && !(wvalid && wready)) ? w_wait + 1 : 0;
      ar_wait = (arvalid && !(arvalid && arready)) ? ar_wait + 1 : 0;
      b_hold = (bvalid && !bready) ? b_hold + 1 : 0;
      r_hold = (rvalid && !rready) ? r_hold + 1 : 0;
      awready <= nlv_ready_pattern(ctl[AW_MODE], ctl[AW_A1], ctl[AW_A2], cyc, aw_wait, lfsr) &&
          n_aw - n_b < ctl[CAP_W] && aw_tail - aw_apply < DEPTH;
      if (ctl[W_MODE] == 6 && !LITE && wvalid && !wready && w_bursts < aw_tail &&
          w_in_beat == aq_len[w_bursts%DEPTH])
        w_stall = w_stall + 1;
      wready <= (ctl[W_MODE] == 6 ?
          !(w_bursts < aw_tail && w_in_beat == aq_len[w_bursts%DEPTH] &&
            w_stall < ctl[W_A1] + (ctl[W_A2] != 0 ? w_bursts % ctl[W_A2] : 0)) :
          nlv_ready_pattern(ctl[W_MODE], ctl[W_A1], ctl[W_A2], cyc, w_wait, lfsr >> 5)) &&
          w_bursts - b_since < ctl[CAP_W] && wb_tail - wb_head < DEPTH - 1 && !w_r_gated(0) &&
          !(ctl[W_STOP] != 0 && n_w - w_stop_base >= ctl[W_STOP] - 1);
      arready <= nlv_ready_pattern(ctl[AR_MODE], ctl[AR_A1], ctl[AR_A2], cyc, ar_wait, lfsr >> 9) &&
          n_ar - n_rlast < ctl[CAP_R];
    end
    prev_awvalid = awvalid;
    prev_awready = awready;
    prev_aw = {awid, awaddr, awlen, awsize, awburst, awlock, awcache, awprot, awqos, awregion};
    prev_wvalid = wvalid;
    prev_wready = wready;
    prev_w = {wdata, wstrb, wlast};
    prev_arvalid = arvalid;
    prev_arready = arready;
    prev_ar = {arid, araddr, arlen, arsize, arburst, arlock, arcache, arprot, arqos, arregion};
    prev_bvalid = bvalid;
    prev_rvalid = rvalid;
    prev_sb = sideband;
  end
endmodule
