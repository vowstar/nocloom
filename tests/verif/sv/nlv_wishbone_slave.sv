// Independent Wishbone memory with the shared tape controls and access events.
module nlv_wishbone_slave #(
    parameter NAME = "t",
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter DIALECT = 4, parameter WORD = 0, parameter RETRY = 0,
    parameter MEM_BYTES = 65536,
    parameter SALT = 0
) (
    input wire clk,
    input wire rst_n,
    input wire cyc, stb, we,
    input wire [ADDR_W-1:0] adr,
    input wire [31:0] dat_w,
    input wire [3:0] sel,
    output wire [31:0] dat_r,
    output wire ack, err, rty,
    input wire sideband
);
  reg [31:0] ctl[0:63];
  reg last_cyc = 0;
  wire psel = cyc && stb, penable = stb, pwrite = we;
  wire [31:0] paddr = WORD ? {adr, 2'b00} : adr;
  wire [31:0] pwdata = dat_w;
  wire [3:0] pstrb = sel;
  wire [2:0] pprot = 0;
  wire pready, pslverr;
  reg [31:0] prdata;
  assign dat_r = prdata;
  assign ack = ctl[62] == 1 ? 1'b1 : psel && pready && !pslverr;
  assign err = psel && pslverr && !RETRY;
  assign rty = psel && pslverr && RETRY;
  nlv_wishbone_check #(.NAME(NAME), .ADDR_W(ADDR_W)) wb_check (
      .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(we), .adr(adr),
      .dat_w(dat_w), .dat_r(dat_r), .sel(sel), .ack(ack), .err(err), .rty(rty), .constant_ack(ctl[62] == 1));
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  reg [7:0] mem[0:MEM_BYTES-1];
  localparam WAIT_N = 0, READY_MODE = 1, READY_A1 = 2, READY_A2 = 3, ERR_DEFAULT = 15,
      OVR_I = 17, OVR_C = 21, QUIET = 35, BASE = 41, REG0_LO = 43, REG0_HI = 44,
      REG0_C = 45, REG1_LO = 47, REG1_HI = 48, REG1_C = 49, SEED = 54, ERRM_MASK = 55,
      ERRM_MATCH = 56, SLOW_MASK = 57, SLOW_MATCH = 58, SLOW_WAIT = 59, FIFO_EN = 60,
      FIFO_ADDR = 61, GAP_HI = 40;
  reg [63:0] n_b2b;
  reg [63:0] cycle_count, n_access, n_write, n_read, base;
  reg [31:0] waited, lfsr, low_run;
  reg ready_r, err_r, prev_psel, prev_penable, prev_rst, prev_pready, prev_done;
  reg [ADDR_W+DATA_W+DATA_W/8+3:0] held;
  integer i;
  reg [63:0] la;

  assign pready = (DIALECT == 2 || ctl[62] != 0) ? 1'b1 : ready_r;
  assign pslverr = DIALECT == 2 ? 1'b0 : (ready_r && err_r);

  function [63:0] counter;
    input integer index;
    begin
      case (index)
        0: counter = n_access;
        1: counter = n_write;
        2: counter = n_read;
        8: counter = psel;
        9: counter = n_b2b;
        15: counter = sideband;
        20: counter = cycle_count;
        default: counter = 0;
      endcase
    end
  endfunction

  function err_code;
    input [63:0] index;
    input [63:0] addr;
    integer e;
    begin
      if (ctl[REG0_HI] > ctl[REG0_LO] && addr >= ctl[REG0_LO] && addr < ctl[REG0_HI])
        err_code = ctl[REG0_C] != 0;
      else if (ctl[REG1_HI] > ctl[REG1_LO] && addr >= ctl[REG1_LO] && addr < ctl[REG1_HI])
        err_code = ctl[REG1_C] != 0;
      else
        err_code = ctl[ERR_DEFAULT] != 0;
      if (ctl[ERRM_MASK] != 0 && (addr & ctl[ERRM_MASK]) == ctl[ERRM_MATCH]) err_code = 1;
      for (e = 0; e < 4; e = e + 1)
        if (ctl[OVR_I+e] != 0 && ctl[OVR_I+e] - 1 == index) err_code = ctl[OVR_C+e] != 0;
    end
  endfunction

  task set_ctl;
    input integer index;
    input [31:0] value;
    begin
      if (index == BASE) base = n_access;
      if (index == SEED) lfsr = value == 0 ? 32'h1 : value;
      ctl[index] = value;
    end
  endtask

  initial begin
    for (i = 0; i < 64; i = i + 1) ctl[i] = 0;
    for (i = 0; i < MEM_BYTES; i = i + 1) mem[i] = nlv_target_byte(i, SALT);
    cycle_count = 0; n_access = 0; n_write = 0; n_read = 0; base = 0; waited = 0; lfsr = 32'h1;
    low_run = 0;
    n_b2b = 0;
    ready_r = 0; err_r = 0; prev_psel = 0; prev_penable = 0; prev_rst = 0; prev_pready = 0;
    prev_done = 0;
    held = 0; prdata = 0;
  end

  // PREADY and PSLVERR settle before the clock edge that samples them; they
  // are registered from the cycle count of the current ACCESS phase.
  always @(negedge clk) begin
    if (!rst_n || !(psel && penable)) begin
      ready_r = DIALECT == 2;
      waited = 0;
    end else begin
      ready_r = ctl[READY_MODE] != 0
          ? nlv_ready_pattern(ctl[READY_MODE], ctl[READY_A1], ctl[READY_A2], cycle_count, waited, lfsr)
          : waited >= (ctl[SLOW_MASK] != 0 && (paddr & ctl[SLOW_MASK]) == ctl[SLOW_MATCH]
                       ? ctl[SLOW_WAIT] : ctl[WAIT_N]);
      err_r = err_code(n_access - base, paddr);
      la = paddr & ~(NB - 1);
      if (!pwrite)
        for (i = 0; i < NB; i = i + 1) prdata[i*8+:8] = mem[(la + i) % MEM_BYTES];
    end
  end

  always @(posedge clk) begin
    if (cyc != last_cyc)
      $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"p\":\"%0s\",\"k\":\"lv\",\"ch\":\"cyc\",\"v\":%0d}\n",
              ($realtime * 1000.0), NAME, cyc);
    last_cyc = cyc;
    cycle_count = cycle_count + 1;
    lfsr = nlv_lfsr_next(lfsr);
    if (!rst_n) begin
      if (prev_rst == 1'b1)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":0}\n",
                ($realtime * 1000.0), cycle_count, NAME);
      if (prev_rst == 1'b0 && cycle_count > 2 && psel !== 1'b0)
        tb.nlv_fail_report("WB.RULE_3_20", NAME, psel);
      prev_rst = 1'b0;
      prev_done = 0;
      waited = 0;
      low_run = 0;
    end else begin
      if (prev_rst == 1'b0)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":1}\n",
                ($realtime * 1000.0), cycle_count, NAME);
      prev_rst = 1'b1;
      if (!prev_psel) begin
        held = {paddr, pwdata, pstrb, pprot, pwrite};
        waited = 0;
      end
      if (psel && penable) begin
        if (pready) begin
          la = paddr & ~(NB - 1);
          if (pwrite && !pslverr)
            for (i = 0; i < NB; i = i + 1)
              if (DIALECT != 4 || pstrb[i]) mem[(la + i) % MEM_BYTES] = pwdata[i*8+:8];
          $fwrite(tb.nlv_fd,
                  "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"apb\",\"a\":\"%0h\",\"w\":%0d,\"d\":\"%0h\",\"s\":\"%0h\",\"pr\":%0d,\"r\":%0d,\"waits\":%0d}\n",
                  ($realtime * 1000.0), cycle_count, NAME, paddr, pwrite, pwrite ? pwdata : prdata,
                  DIALECT == 4 ? pstrb : {NB{1'b1}}, DIALECT == 4 ? pprot : 3'd0, pslverr, waited);
          n_access = n_access + 1;
          if (pwrite) n_write = n_write + 1;
          else begin
            n_read = n_read + 1;
            // A FIFO word: every read of it returns a new value.
            if (ctl[FIFO_EN] != 0 && la == ctl[FIFO_ADDR])
              for (i = 0; i < NB; i = i + 1) mem[(la + i) % MEM_BYTES] = mem[(la + i) % MEM_BYTES] + 1;
          end
        end else
          waited = waited + 1;
      end
      prev_done = psel && penable && pready;
      // An ACCESS waited GAP_HI cycles in a row without the timeout status.
      low_run = psel && penable && !pready ? low_run + 1 : 0;
      if (ctl[GAP_HI] != 0 && low_run == ctl[GAP_HI] && sideband !== 1'b1)
        tb.nlv_fail_report("CONTRACT.TIMEOUT.GAP", NAME, low_run);
    end
    prev_psel = psel;
    prev_penable = penable;
    prev_pready = pready;
  end
endmodule
