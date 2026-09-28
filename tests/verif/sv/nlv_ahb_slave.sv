`timescale 1ns/1ps
// AHB (AHB-Lite style single slave) target-side BFM. An address phase is
// accepted when HSEL, HTRANS NONSEQ or SEQ and HREADY are high; its data
// phase waits WAIT_N cycles (HREADYOUT low), then completes. An error is the
// two-cycle response (HRESP high with HREADYOUT low, then high). Events:
// ahb_addr at address acceptance and ahb_data at completion. Monitor: reset
// quiet, address/control hold while the bus is waited, X on HTRANS, and BUSY
// (AHB-Lite 3.2): only inside an open burst (an undefined-length INCR, or a
// fixed-length burst with beats left; NONSEQ or IDLE closes it), showing the
// burst's HBURST, HSIZE, HWRITE and HPROT and the next beat's address
// (wrapped at the container for WRAP, modulo the port's address width).
// SEQ rules are judged offline from the ahb_addr events. A data phase at an
// address where (HADDR & SLOW_MASK) == SLOW_MATCH waits SLOW_WAIT cycles
// instead of WAIT_N (a hung target until SLOW_WAIT is lowered). BUSY_HOLD
// holds HREADYOUT low while no data phase is pending and the bus shows BUSY,
// once BUSY_FROM data phases completed since the base mark (a target AHB
// forbids; a timer must still end the wait). With GAP_HI set, HREADYOUT held
// low for GAP_HI cycles in a row while the sideband status is low is
// CONTRACT.TIMEOUT.GAP (a target timer that does not expire in time).
// Responses: ERROR, and with a 2-bit HRESP RETRY (2) or SPLIT (3), each a
// two-cycle response; a write that is not OKAY changes no byte. Wait
// profiles: WAIT_MODE 0 waits WAIT_N cycles in every data phase, 1 waits
// 2 + cycle % 3, 2 waits 0 to 3 cycles in a quarter of the data phases
// (pseudo-random); IDLE_MODE holds HREADYOUT low without a data phase on
// every seventh cycle (1), an eighth of the cycles (2, pseudo-random), every
// other cycle (3) or always (4). Counters err_next and retry_next count the
// first cycles of an ERROR, or of a RETRY or SPLIT, that met the next
// transfer in its address phase. Every change of HSEL, HTRANS or HREADY is a
// "tr" event (the bus state between address phases).
module nlv_ahb_slave #(
    parameter NAME = "t",
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter HRESP_W = 1,
    parameter MEM_BYTES = 65536,
    parameter SALT = 0
) (
    input wire clk,
    input wire rst_n,
    input wire hsel,
    input wire [ADDR_W-1:0] haddr,
    input wire [1:0] htrans,
    input wire hwrite,
    input wire [2:0] hsize,
    input wire [2:0] hburst,
    input wire [3:0] hprot,
    input wire hmastlock,
    input wire [DATA_W-1:0] hwdata,
    input wire hready,
    output reg hreadyout,
    output reg [HRESP_W-1:0] hresp,
    output reg [DATA_W-1:0] hrdata,
    input wire sideband
);
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  reg [7:0] mem[0:MEM_BYTES-1];
  reg [31:0] ctl[0:63];
  localparam WAIT_N = 0, WAIT_MODE = 1, IDLE_MODE = 2, ERR_DEFAULT = 15, OVR_I = 17, OVR_C = 21,
      QUIET = 35, GAP_HI = 40, BASE = 41, REG0_LO = 43, REG0_HI = 44, REG0_C = 45, REG1_LO = 47,
      REG1_HI = 48, REG1_C = 49, SEED = 54, SLOW_MASK = 57, SLOW_MATCH = 58, SLOW_WAIT = 59,
      FIFO_EN = 60, FIFO_ADDR = 61, BUSY_HOLD = 62, BUSY_FROM = 63, ONE_CYCLE = 55;
  reg [63:0] cyc, n_addr, n_data, base, base_addr;
  // The open burst of the last NONSEQ: type, address, size, write, prot and
  // the beats a fixed-length burst still owes.
  reg b_open, b_write;
  reg [2:0] b_burst, b_size;
  reg [3:0] b_prot;
  reg [ADDR_W-1:0] b_addr;
  integer b_left;
  function [ADDR_W-1:0] next_addr;
    input [ADDR_W-1:0] a;
    input [2:0] burst, size;
    reg [ADDR_W-1:0] span;
    begin
      span = burst >= 2 ? (4 << ((burst - 2) / 2)) << size : 0;
      next_addr = burst >= 2 && burst % 2 == 0 ? (a / span) * span + (a + (1 << size)) % span
                                               : a + (1 << size);
    end
  endfunction
  reg pending, pend_write, second, prev_rst, prev_wait, prev_sel, prev_ready;
  reg [1:0] err_now, prev_bus;
  reg [63:0] n_err_next, n_retry_next;
  reg [31:0] pend_wait, lfsr;
  reg [63:0] pend_addr;
  reg [2:0] pend_size;
  reg [31:0] waited, low_run;
  reg [ADDR_W+10:0] prev_ctrl;
  reg [1:0] prev_trans;
  reg abort_expected = 0, response_first = 0;
  reg [HRESP_W-1:0] first_response;
  reg data_held = 0;
  reg [DATA_W-1:0] held_data;
  integer i, e;
  reg [63:0] la;

  function [63:0] counter;
    input integer index;
    begin
      case (index)
        0: counter = n_addr;
        1: counter = n_data;
        8: counter = htrans != 0 && hsel;
        9: counter = hsel ? htrans : 0;
        10: counter = n_err_next;
        11: counter = n_retry_next;
        15: counter = sideband;
        20: counter = cyc;
        default: counter = 0;
      endcase
    end
  endfunction

  task set_ctl;
    input integer index;
    input [31:0] value;
    begin
      if (index == BASE) begin
        base = n_data;
        base_addr = n_addr;
      end
      if (index == SEED) lfsr = value == 0 ? 32'h1 : value;
      ctl[index] = value;
    end
  endtask

  initial begin
    for (i = 0; i < 64; i = i + 1) ctl[i] = 0;
    for (i = 0; i < MEM_BYTES; i = i + 1) mem[i] = nlv_target_byte(i, SALT);
    cyc = 0; n_addr = 0; n_data = 0; base = 0; base_addr = 0; pending = 0; pend_write = 0; err_now = 0;
    second = 0; prev_rst = 0; prev_wait = 0; pend_addr = 0; pend_size = 0; waited = 0;
    low_run = 0; n_err_next = 0; n_retry_next = 0; pend_wait = 0; lfsr = 32'h1;
    prev_sel = 0; prev_ready = 0; prev_bus = 0;
    prev_ctrl = 0; prev_trans = 0; hreadyout = 1; hresp = 0; hrdata = 0;
  end

  always @(posedge clk) begin
    cyc = cyc + 1;
    lfsr = nlv_lfsr_next(lfsr);
    if (!rst_n) begin
      abort_expected = 0; response_first = 0;
      data_held = 0;
      if (prev_rst == 1'b1)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":0}\n",
                ($realtime * 1000.0), cyc, NAME);
      if (prev_rst == 1'b0 && cyc > 2 && htrans !== 2'b00 && hsel)
        tb.nlv_fail_report("AHB.RESET.QUIET", NAME, htrans);
      pending = 0; second = 0; waited = 0; prev_rst = 0; b_open = 0; b_left = 0; low_run = 0;
      hreadyout <= 1; hresp <= 0;
    end else begin
      if (prev_rst == 1'b0)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":1}\n",
                ($realtime * 1000.0), cyc, NAME);
      prev_rst = 1;
      if (^htrans === 1'bx) tb.nlv_fail_report("AHB.X.HTRANS", NAME, 0);
      if (hsel && htrans[1] && ^{haddr,hwrite,hsize,hburst,hprot} === 1'bx)
        tb.nlv_fail_report("AHB.X.CONTROL", NAME, 0);
      if (pending && ^{hready,hresp} === 1'bx)
        tb.nlv_fail_report("AHB.X.RESPONSE", NAME, 0);
      if (pending && pend_write && ^hwdata === 1'bx)
        tb.nlv_fail_report("AHB.X.DATA", NAME, 0);
      if (pending && !pend_write && hready && hresp == 0 && ^hrdata === 1'bx)
        tb.nlv_fail_report("AHB.X.DATA", NAME, 0);
      if (data_held && hwdata !== held_data)
        tb.nlv_fail_report("AHB.DATA.HOLD", NAME, 0);
      data_held = pending && pend_write && !hready;
      held_data = hwdata;
      if (hsel !== prev_sel || htrans !== prev_bus || hready !== prev_ready)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"tr\",\"s\":%0d,\"tr\":%0d,\"rd\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, hsel, htrans, hready);
      // The first cycle of an error response meeting the next address phase.
      if (pending && !hreadyout && hresp != 0 && hsel && htrans[1]) begin
        if (hresp == 1) n_err_next = n_err_next + 1;
        else n_retry_next = n_retry_next + 1;
      end
      if (ctl[QUIET] != 0 && hsel && htrans[1]) tb.nlv_fail_report("CONTRACT.QUIET.AHB", NAME, haddr);
      if (abort_expected && (htrans !== 2'b00 || hsel !== 1'b0))
        tb.nlv_fail_report("AHB.CANCEL.ADDRESS", NAME, haddr);
      if (pending && hready && hresp != 0 && !response_first)
        tb.nlv_fail_report("AHB.RESPONSE.TWO_CYCLE", NAME, haddr);
      if (response_first && (!hready || hresp !== first_response))
        tb.nlv_fail_report("AHB.RESPONSE.TWO_CYCLE", NAME, haddr);
      if (prev_wait && !abort_expected && prev_trans[1] && htrans != 2'b01 &&
          {haddr, htrans, hwrite, hsize, hburst} !== prev_ctrl)
        tb.nlv_fail_report("AHB.ADDR.HOLD", NAME, haddr);
      abort_expected = pending && !hreadyout && HRESP_W == 2 && hresp >= 2;
      response_first = pending && !hreadyout && hresp != 0;
      first_response = hresp;
      if (hsel && hready) begin
        if (htrans == 2'b01) begin
          if (!b_open) tb.nlv_fail_report("AHB.BUSY.OUTSIDE", NAME, haddr);
          else if (haddr !== next_addr(b_addr, b_burst, b_size) || hburst !== b_burst ||
                   hsize !== b_size || hwrite !== b_write || hprot !== b_prot)
            tb.nlv_fail_report("AHB.BUSY.CONTROL", NAME, haddr);
        end else if (htrans == 2'b11) begin
          b_addr = haddr;
          if (b_burst >= 2) begin
            b_left = b_left - 1;
            b_open = b_left > 0;
          end
        end else begin
          b_open = 0;
          if (htrans == 2'b10) begin
            b_burst = hburst; b_addr = haddr; b_size = hsize; b_write = hwrite; b_prot = hprot;
            b_left = hburst >= 2 ? (4 << ((hburst - 2) / 2)) - 1 : 0;
            b_open = hburst == 3'd1 || b_left > 0;
          end
        end
      end
      // Data phase of the pending transfer.
      if (pending && hready) begin
        // The cycle with HREADY high completes the data phase.
        la = pend_addr & ~(NB - 1);
        if (pend_write && err_now == 0)
          for (i = 0; i < NB; i = i + 1)
            if (i >= pend_addr % NB && i < (pend_addr % NB) + (1 << pend_size))
              mem[(la + i) % MEM_BYTES] = hwdata[i*8+:8];
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"ahb_data\",\"a\":\"%0h\",\"w\":%0d,\"d\":\"%0h\",\"r\":%0d,\"waits\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, pend_addr, pend_write,
                pend_write ? hwdata : hrdata, err_now, waited);
        n_data = n_data + 1;
        // A FIFO word: every read of it returns a new value.
        if (!pend_write && ctl[FIFO_EN] != 0 && la == ctl[FIFO_ADDR])
          for (i = 0; i < NB; i = i + 1) mem[(la + i) % MEM_BYTES] = mem[(la + i) % MEM_BYTES] + 1;
        pending = 0;
      end
      // Address phase.
      if (hsel && hready && htrans[1]) begin
        $fwrite(tb.nlv_fd,
                "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"ahb_addr\",\"a\":\"%0h\",\"w\":%0d,\"sz\":%0d,\"bu\":%0d,\"tr\":%0d,\"pr\":%0d,\"lk\":%0d}\n",
                ($realtime * 1000.0), cyc, NAME, haddr, hwrite, hsize, hburst, htrans,
                hprot, hmastlock);
        pending = 1;
        pend_write = hwrite;
        pend_addr = haddr;
        pend_size = hsize;
        waited = 0;
        n_addr = n_addr + 1;
        err_now = ctl[ERR_DEFAULT];
        if (ctl[REG0_HI] > ctl[REG0_LO] && haddr >= ctl[REG0_LO] && haddr < ctl[REG0_HI])
          err_now = ctl[REG0_C];
        else if (ctl[REG1_HI] > ctl[REG1_LO] && haddr >= ctl[REG1_LO] && haddr < ctl[REG1_HI])
          err_now = ctl[REG1_C];
        for (e = 0; e < 4; e = e + 1)
          if (ctl[OVR_I+e] != 0 && ctl[OVR_I+e] - 1 == n_addr - 1 - base_addr) err_now = ctl[OVR_C+e];
        pend_wait = ctl[WAIT_MODE] == 1 ? 2 + cyc % 3 :
                    ctl[WAIT_MODE] == 2 ? (lfsr[3:2] == 0 ? lfsr[1:0] : 0) : 0;
        second = 0;
      end
      // Drive HREADYOUT/HRESP/HRDATA for the next cycle.
      if (pending) begin
        if (waited < (ctl[SLOW_MASK] != 0 && (pend_addr & ctl[SLOW_MASK]) == ctl[SLOW_MATCH]
                      ? ctl[SLOW_WAIT] : ctl[WAIT_MODE] != 0 ? pend_wait : ctl[WAIT_N])) begin
          waited = waited + 1;
          hreadyout <= 0;
          hresp <= 0;
        end else if (err_now != 0 && !second) begin
          second = 1;
          hreadyout <= ctl[ONE_CYCLE] != 0;
          hresp <= err_now;
        end else begin
          hreadyout <= 1;
          hresp <= err_now;
          la = pend_addr & ~(NB - 1);
          for (i = 0; i < NB; i = i + 1) hrdata[i*8+:8] <= mem[(la + i) % MEM_BYTES];
        end
      end else begin
        hreadyout <= !(ctl[BUSY_HOLD] != 0 && hsel && htrans == 2'b01 &&
                       n_data - base >= ctl[BUSY_FROM]) &&
                     !(ctl[IDLE_MODE] == 1 && cyc % 7 == 2 || ctl[IDLE_MODE] == 2 && lfsr[7:5] == 0 ||
                       ctl[IDLE_MODE] == 3 && cyc % 2 == 1 || ctl[IDLE_MODE] == 4);
        hresp <= 0;
      end
      // HREADYOUT low for GAP_HI cycles in a row without the timeout status.
      low_run = hreadyout ? 0 : low_run + 1;
      if (ctl[GAP_HI] != 0 && low_run == ctl[GAP_HI] && sideband !== 1'b1)
        tb.nlv_fail_report("CONTRACT.TIMEOUT.GAP", NAME, low_run);
    end
    prev_wait = !hready;
    prev_trans = htrans;
    prev_sel = hsel;
    prev_bus = htrans;
    prev_ready = hready;
    prev_ctrl = {haddr, htrans, hwrite, hsize, hburst};
  end
endmodule
