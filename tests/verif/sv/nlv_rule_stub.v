`timescale 1ns/1ps
// Rule self-test DUT (tests/verif/rulecheck.py); not a product artifact.
// Mode 0 is a legal design: an AXI wire from the m port to the t port, and
// one scripted APB write, AHB write pair and stream transfer after each
// reset. Every other mode (+NLV_STUB=k) breaks exactly one protocol rule
// so that the named BFM monitor rule must be the first failure. Mode
// MERGE models a network whose reads and writes share routes: an AR waits
// while a write burst has its AW accepted and its last W beat not yet
// passed, and a B waits while an R is offered. The tape then breaks a
// liveness premise and the stall report must name it (LIVE.E1..E3).
module nlv_rule_stub (
    input wire clk,
    input wire rst_n,
    // AXI4 slave port, driven by the initiator BFM.
    input wire [1:0] m_awid,
    input wire [15:0] m_awaddr,
    input wire [7:0] m_awlen,
    input wire [2:0] m_awsize,
    input wire [1:0] m_awburst,
    input wire m_awlock,
    input wire [3:0] m_awcache,
    input wire [2:0] m_awprot,
    input wire [3:0] m_awqos,
    input wire m_awvalid,
    output wire m_awready,
    input wire [31:0] m_wdata,
    input wire [3:0] m_wstrb,
    input wire m_wlast,
    input wire m_wvalid,
    output wire m_wready,
    output wire [1:0] m_bid,
    output wire [1:0] m_bresp,
    output wire m_bvalid,
    input wire m_bready,
    input wire [1:0] m_arid,
    input wire [15:0] m_araddr,
    input wire [7:0] m_arlen,
    input wire [2:0] m_arsize,
    input wire [1:0] m_arburst,
    input wire m_arlock,
    input wire [3:0] m_arcache,
    input wire [2:0] m_arprot,
    input wire [3:0] m_arqos,
    input wire m_arvalid,
    output wire m_arready,
    output wire [1:0] m_rid,
    output wire [31:0] m_rdata,
    output wire [1:0] m_rresp,
    output wire m_rlast,
    output wire m_rvalid,
    input wire m_rready,
    // AXI4 master port, answered by the target BFM.
    output wire [1:0] t_awid,
    output wire [15:0] t_awaddr,
    output wire [7:0] t_awlen,
    output wire [2:0] t_awsize,
    output wire [1:0] t_awburst,
    output wire t_awlock,
    output wire [3:0] t_awcache,
    output wire [2:0] t_awprot,
    output wire [3:0] t_awqos,
    output wire t_awvalid,
    input wire t_awready,
    output wire [31:0] t_wdata,
    output wire [3:0] t_wstrb,
    output wire t_wlast,
    output wire t_wvalid,
    input wire t_wready,
    input wire [1:0] t_bid,
    input wire [1:0] t_bresp,
    input wire t_bvalid,
    output wire t_bready,
    output wire [1:0] t_arid,
    output wire [15:0] t_araddr,
    output wire [7:0] t_arlen,
    output wire [2:0] t_arsize,
    output wire [1:0] t_arburst,
    output wire t_arlock,
    output wire [3:0] t_arcache,
    output wire [2:0] t_arprot,
    output wire [3:0] t_arqos,
    output wire t_arvalid,
    input wire t_arready,
    input wire [1:0] t_rid,
    input wire [31:0] t_rdata,
    input wire [1:0] t_rresp,
    input wire t_rlast,
    input wire t_rvalid,
    output wire t_rready,
    output wire sb_status,
    // APB master port, answered by the APB target BFM.
    output reg p_psel,
    output reg p_penable,
    output wire p_pwrite,
    output wire [15:0] p_paddr,
    output wire [31:0] p_pwdata,
    output wire [3:0] p_pstrb,
    output wire [2:0] p_pprot,
    input wire [31:0] p_prdata,
    input wire p_pready,
    input wire p_pslverr,
    // AHB master port, answered by the AHB target BFM.
    output wire h_hsel,
    output wire [15:0] h_haddr,
    output reg [1:0] h_htrans,
    output wire h_hwrite,
    output wire [2:0] h_hsize,
    output wire [2:0] h_hburst,
    output wire [3:0] h_hprot,
    output wire h_hmastlock,
    output wire [31:0] h_hwdata,
    output wire h_hready,
    input wire h_hreadyout,
    input wire [1:0] h_hresp,
    input wire [31:0] h_hrdata,
    // A valid/ready stream watched by the stream monitor.
    output wire x_valid,
    output wire x_ready,
    output wire [7:0] x_data
);
  localparam GOOD = 0, B_HOLD = 1, R_HOLD = 2, B_EXTRA = 3, R_EXTRA = 4, R_LAST = 5,
      B_EARLY = 6, M_RESET = 7, B_X = 8, R_X = 9, AW_HOLD = 10, W_HOLD = 11, AR_HOLD = 12,
      W_LAST = 13, T_RESET = 14, AW_X = 15, W_X = 16, AR_X = 17, GAP = 18, APB_RESET = 19,
      APB_X = 20, APB_ORDER = 21, APB_STABLE = 22, APB_DROP = 23, APB_ABANDON = 24,
      AHB_RESET = 25, AHB_X = 26, AHB_HOLD = 27, STREAM_HOLD = 28, MERGE = 29, B_SAME = 30,
      AHB_BUSY_OUT = 31, AHB_BUSY_CTRL = 32, AHB_BUSY_OK = 33, AHB_CTRL_X = 34,
      AHB_RESP_X = 35, AHB_DATA_X = 36, AHB_DATA_MOVE = 37, AHB_CANCEL_OK = 38;

  integer mode;
  initial begin
    mode = GOOD;
    if (!$value$plusargs("NLV_STUB=%d", mode)) mode = GOOD;
  end

  // Cycles since the end of the last reset; stall counters of held VALIDs.
  reg [15:0] pc;
  reg [7:0] b_stall, r_stall, aw_stall, w_stall, ar_stall;
  reg aw_seen, b_sent;
  reg [1:0] aw_id;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      pc <= 0;
      b_stall <= 0; r_stall <= 0; aw_stall <= 0; w_stall <= 0; ar_stall <= 0;
      aw_seen <= 0; b_sent <= 0; aw_id <= 0;
    end else begin
      pc <= pc + 1;
      b_stall <= (t_bvalid && !m_bready) ? b_stall + 1 : 0;
      r_stall <= (t_rvalid && !m_rready) ? r_stall + 1 : 0;
      aw_stall <= (m_awvalid && !t_awready) ? aw_stall + 1 : 0;
      w_stall <= (m_wvalid && !t_wready) ? w_stall + 1 : 0;
      ar_stall <= (m_arvalid && !t_arready) ? ar_stall + 1 : 0;
      if (m_awvalid && m_awready && !aw_seen) begin
        aw_seen <= 1;
        aw_id <= m_awid;
      end
      if (m_bvalid && m_bready && aw_seen) b_sent <= 1;
    end

  // ---- MERGE: shared read/write routes; an offered VALID is never withdrawn ----
  reg [7:0] aw_open, wlast_open;
  reg ar_held, b_held;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      aw_open <= 0; wlast_open <= 0; ar_held <= 0; b_held <= 0;
    end else begin
      if (m_awvalid && m_awready) aw_open <= aw_open + 1;
      if (m_wvalid && m_wready && m_wlast) wlast_open <= wlast_open + 1;
      ar_held <= t_arvalid && !t_arready;
      b_held <= m_bvalid && !m_bready;
    end
  wire ar_pass = mode != MERGE || aw_open == wlast_open || ar_held;
  wire b_pass = mode != MERGE || !t_rvalid || b_held;

  // ---- AXI wire with the AXI rule breaks ----
  wire pulse = rst_n && pc == 20;
  wire early_b = mode == B_EARLY && aw_seen && !b_sent;
  // B_SAME: BVALID raised in the very cycle of the WLAST handshake (one
  // cycle too early; the cycle after it is legal, see the clean scenario).
  wire same_b = mode == B_SAME && aw_seen && m_wvalid && m_wready && m_wlast && !b_sent;
  assign m_awready = t_awready;
  assign m_wready = mode == B_EARLY ? 1'b0 : t_wready;
  assign m_arready = t_arready && ar_pass;
  assign m_bid = early_b || same_b ? aw_id : t_bid;
  assign m_bresp = early_b || same_b ? 2'd0 : t_bresp;
  assign m_bvalid = mode == B_HOLD && b_stall == 3 ? 1'b0 :
                    mode == B_EXTRA && pulse ? 1'b1 :
                    mode == B_EARLY ? early_b :
                    mode == B_SAME ? same_b :
                    mode == M_RESET && !rst_n ? 1'b1 :
                    mode == B_X && pulse ? 1'bx : t_bvalid && b_pass;
  assign m_rid = t_rid;
  assign m_rdata = mode == R_HOLD && r_stall == 3 ? t_rdata ^ 32'd1 : t_rdata;
  assign m_rresp = t_rresp;
  assign m_rlast = mode == R_LAST || mode == R_EXTRA && pulse ? 1'b1 : t_rlast;
  assign m_rvalid = mode == R_EXTRA && pulse ? 1'b1 : mode == R_X && pulse ? 1'bx : t_rvalid;
  assign t_bready = mode == B_SAME ? 1'b1 : m_bready && b_pass;
  assign t_rready = m_rready;
  assign t_awid = m_awid;
  assign t_awaddr = m_awaddr;
  assign t_awlen = m_awlen;
  assign t_awsize = m_awsize;
  assign t_awburst = m_awburst;
  assign t_awlock = m_awlock;
  assign t_awcache = m_awcache;
  assign t_awprot = m_awprot;
  assign t_awqos = m_awqos;
  assign t_awvalid = mode == AW_HOLD && aw_stall == 3 ? 1'b0 :
                     mode == T_RESET && !rst_n ? 1'b1 :
                     mode == AW_X && pulse ? 1'bx : m_awvalid;
  assign t_wdata = mode == W_HOLD && w_stall == 3 ? m_wdata ^ 32'd1 : m_wdata;
  assign t_wstrb = m_wstrb;
  assign t_wlast = mode == W_LAST ? 1'b1 : m_wlast;
  assign t_wvalid = mode == B_EARLY ? 1'b0 : mode == W_X && pulse ? 1'bx : m_wvalid;
  assign t_arid = m_arid;
  assign t_araddr = mode == AR_HOLD && ar_stall == 3 ? m_araddr ^ 16'd4 : m_araddr;
  assign t_arlen = m_arlen;
  assign t_arsize = m_arsize;
  assign t_arburst = m_arburst;
  assign t_arlock = m_arlock;
  assign t_arcache = m_arcache;
  assign t_arprot = m_arprot;
  assign t_arqos = m_arqos;
  assign t_arvalid = mode == AR_X && pulse ? 1'bx : m_arvalid && ar_pass;
  assign sb_status = mode == GAP && rst_n && pc == 30;

  // ---- APB: one write per reset, SETUP then ACCESS until PREADY ----
  reg [1:0] apb;   // 0 idle, 1 setup, 2 access, 3 finished
  reg [3:0] apb_cycles, apb_after;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      apb <= 0;
      apb_cycles <= 0;
      apb_after <= 0;
    end else begin
      case (apb)
        2'd0: if (pc == 10) apb <= mode == APB_ORDER ? 2'd2 : 2'd1;
        2'd1: apb <= 2'd2;
        2'd2: begin
          apb_cycles <= apb_cycles + 1;
          if (p_pready || mode == APB_ABANDON && apb_cycles == 1) apb <= 2'd3;
        end
        default: if (apb_after != 4'hf) apb_after <= apb_after + 1;
      endcase
    end
  always @* begin
    p_psel = apb == 2'd1 || apb == 2'd2;
    p_penable = apb == 2'd2 || mode == APB_DROP && apb == 2'd3 && apb_after == 0;
    if (mode == APB_RESET && !rst_n) p_psel = 1'b1;
    if (mode == APB_X && rst_n && pc == 5) p_psel = 1'bx;
  end
  assign p_pwrite = 1'b1;
  assign p_paddr = mode == APB_STABLE && apb == 2'd2 && apb_cycles >= 1 ? 16'h0014 : 16'h0010;
  assign p_pwdata = 32'hA5A5_0001;
  assign p_pstrb = 4'hf;
  assign p_pprot = 3'd0;

  // ---- AHB: two NONSEQ writes per reset; the second waits on the first ----
  reg [1:0] ahb;   // 0 idle, 1 first address, 2 second address, 3 finished
  reg [3:0] ahb_waits;
  reg ahb_cancel;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      ahb <= 0;
      ahb_waits <= 0;
      ahb_cancel <= 0;
    end else begin
      if (mode == AHB_CANCEL_OK && !h_hready && h_hresp >= 2) ahb_cancel <= 1;
      else if (h_hready) ahb_cancel <= 0;
      case (ahb)
        2'd0: if (pc == 10) ahb <= 2'd1;
        2'd1: if (h_hready) ahb <= 2'd2;
        2'd2: if (h_hready && !ahb_cancel) ahb <= 2'd3;
                else ahb_waits <= ahb_waits + 1;
        default: ;
      endcase
    end
  always @* begin
    h_htrans = ahb == 2'd1 || ahb == 2'd2 ? 2'b10 : 2'b00;
    if (mode == AHB_RESET && !rst_n) h_htrans = 2'b10;
    if (mode == AHB_X && rst_n && pc == 5) h_htrans = 2'bxx;
    // BUSY after the SINGLE writes (outside a burst), or instead of the
    // second write after an INCR first write: at the next address (legal)
    // or at another one.
    if (mode == AHB_BUSY_OUT && ahb == 2'd3 && pc == 40) h_htrans = 2'b01;
    if ((mode == AHB_BUSY_CTRL || mode == AHB_BUSY_OK) && ahb == 2'd2) h_htrans = 2'b01;
    if (ahb_cancel) h_htrans = 2'b00;
  end
  assign h_hsel = !ahb_cancel && (ahb == 2'd1 || ahb == 2'd2 || mode == AHB_RESET && !rst_n ||
                  mode == AHB_BUSY_OUT && ahb == 2'd3 && pc == 40);
  assign h_haddr = mode == AHB_CTRL_X && ahb == 2'd1 ? 16'hxxxx :
                  ahb == 2'd2 ? (mode == AHB_HOLD && ahb_waits >= 1 || mode == AHB_BUSY_CTRL ?
                                   16'h002c : 16'h0024) : 16'h0020;
  assign h_hwrite = 1'b1;
  assign h_hsize = 3'd2;
  assign h_hburst = mode == AHB_BUSY_CTRL || mode == AHB_BUSY_OK ? 3'd1 : 3'd0;
  assign h_hprot = 4'd3;
  assign h_hmastlock = 1'b0;
  assign h_hwdata = mode == AHB_DATA_X && ahb == 2'd2 ? 32'hxxxx_xxxx :
                   mode == AHB_DATA_MOVE && ahb_waits >= 1 ? 32'h5566_7788 : 32'h1122_3344;
  assign h_hready = mode == AHB_RESP_X && ahb == 2'd2 ? 1'bx : h_hreadyout;

  // ---- Stream: one transfer per reset, READY two cycles late ----
  reg [1:0] str;
  reg [3:0] str_cycles;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin
      str <= 0;
      str_cycles <= 0;
    end else if (str == 0 && pc == 10)
      str <= 1;
    else if (str == 1) begin
      if (x_valid && x_ready) str <= 2;
      else str_cycles <= str_cycles + 1;
    end
  assign x_valid = str == 1 && !(mode == STREAM_HOLD && str_cycles == 1);
  assign x_ready = str == 1 && str_cycles >= 2;
  assign x_data = 8'hA5;
endmodule
