`timescale 1ns/1ps
// APB initiator-side BFM (APB4 with PSTRB and PPROT, APB3 without): drives a
// DUT APB completer port (a NocLoom APB initiator endpoint). Descriptors are
// pushed by the tape with the push_write/push_read tasks of the AXI
// initiator BFM and served one at a time in push order: an idle cycle, SETUP,
// then ACCESS until PREADY. A completed transfer is logged as the AXI-shaped
// handshakes of one single-beat transaction (aw, w, b for a write; ar, r for
// a read; the address handshake at SETUP, the rest at completion) so the
// offline checker judges it like an AXI4-Lite beat; PSLVERR is SLVERR.
module nlv_apb_master #(
    parameter NAME = "m",
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter DIALECT = 4,
    parameter DEPTH = 64
) (
    input wire clk,
    input wire rst_n,
    output reg psel,
    output reg penable,
    output reg pwrite,
    output reg [ADDR_W-1:0] paddr,
    output reg [DATA_W-1:0] pwdata,
    output reg [DATA_W/8-1:0] pstrb,
    output reg [2:0] pprot,
    input wire [DATA_W-1:0] prdata,
    input wire pready,
    input wire pslverr
);
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  localparam [2:0] SIZE = NB == 4 ? 3'd2 : NB == 2 ? 3'd1 : 3'd0;
  reg [63:0] q_addr[0:DEPTH-1];
  reg q_write[0:DEPTH-1];
  reg [2:0] q_prot[0:DEPTH-1];
  reg [7:0] q_strb[0:DEPTH-1];
  reg [31:0] q_salt[0:DEPTH-1];
  reg [31:0] q_tag[0:DEPTH-1];
  integer head, tail, state, j;
  reg [31:0] ctl[0:95];
  localparam SEED = 18, STAB_N = 24;
  reg [63:0] cyc, n_w, n_b, n_r, n_wpush, n_rpush, n_cancel;
  reg [31:0] lfsr;
  reg prev_rst;

  function [63:0] counter;
    input integer index;
    begin
      case (index)
        0: counter = n_b;
        1: counter = n_b;
        2: counter = n_b;
        3: counter = n_r;
        4: counter = n_r;
        5: counter = n_r;
        6: counter = n_wpush;
        7: counter = n_rpush;
        10: counter = n_wpush - n_b;
        11: counter = n_rpush - n_r;
        18: counter = cyc;
        19: counter = n_cancel;
        default: counter = 0;
      endcase
    end
  endfunction

  // APB3 has no PPROT: its transfers are unprivileged Non-secure data.
  function [2:0] eff_prot;
    input [2:0] prot;
    begin
      eff_prot = DIALECT == 4 ? prot : 3'b010;
    end
  endfunction

  task push;
    input wr;
    input [63:0] addr;
    input [2:0] prot;
    input [7:0] strb_mode;
    input [31:0] salt;
    input [31:0] tag;
    integer slot;
    begin
      slot = tail % DEPTH;
      if (tail - head >= DEPTH) tb.nlv_fail_report("TB.QUEUE_FULL", NAME, tail);
      q_addr[slot] = addr;
      q_write[slot] = wr;
      q_prot[slot] = prot;
      q_strb[slot] = strb_mode;
      q_salt[slot] = salt;
      q_tag[slot] = tag;
      tail = tail + 1;
      if (wr) n_wpush = n_wpush + 1;
      else n_rpush = n_rpush + 1;
      $fwrite(tb.nlv_fd,
              "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"push\",\"ch\":\"%0s\",\"tag\":%0d,\"a\":\"%0h\",\"len\":0,\"sz\":%0d,\"bu\":1,\"id\":0,\"lk\":0,\"ca\":0,\"pr\":%0d,\"qo\":0}\n",
              ($realtime * 1000.0), cyc, NAME, wr ? "w" : "r", tag, addr, SIZE, eff_prot(prot));
    end
  endtask

  // The tape's write and read records (AXI shape; one beat, size of the bus).
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
    begin
      push(1'b1, addr, prot, strb_mode, salt, tag);
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
    begin
      push(1'b0, addr, prot, 8'd0, 32'd0, tag);
    end
  endtask

  task set_ctl;
    input integer index;
    input [31:0] value;
    begin
      if (index == SEED) lfsr = value == 0 ? 32'h1 : value;
      ctl[index % 96] = value;
    end
  endtask

  task stall_report;
    input integer min_hold;
    begin
      $display("NLV_STALL %0s psel=%0d penable=%0d pready=%0d queued=%0d", NAME, psel, penable,
               pready, tail - head);
    end
  endtask

  // Strobe of lane j for strobe mode m (as the AXI initiator BFM, one beat).
  function strobe_of;
    input [7:0] m;
    input integer lane;
    begin
      case (m)
        8'd1: strobe_of = (lane % 3) == 0;
        8'd2: strobe_of = 1'b0;
        8'd3: strobe_of = lane == 0;
        8'd6: strobe_of = ctl[32][lane % 32];
        default: strobe_of = 1'b1;
      endcase
    end
  endfunction

  initial begin
    head = 0; tail = 0; state = 0;
    for (j = 0; j < 96; j = j + 1) ctl[j] = 0;
    ctl[32] = 32'hffffffff;
    cyc = 0; n_w = 0; n_b = 0; n_r = 0; n_wpush = 0; n_rpush = 0; n_cancel = 0; lfsr = 1;
    prev_rst = 0;
    psel = 0; penable = 0; pwrite = 0; paddr = 0; pwdata = 0; pstrb = 0; pprot = 0;
  end

  always @(posedge clk) begin
    cyc = cyc + 1;
    lfsr = nlv_lfsr_next(lfsr);
    if (!rst_n) begin
      if (prev_rst == 1'b1)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":0}\n",
                ($realtime * 1000.0), cyc, NAME);
      if (prev_rst) n_cancel = n_cancel + (n_wpush - n_b) + (n_rpush - n_r);
      n_wpush = n_b;
      n_rpush = n_r;
      head = 0; tail = 0; state = 0;
      psel <= 0; penable <= 0;
      prev_rst = 1'b0;
    end else begin
      if (prev_rst == 1'b0)
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":1}\n",
                ($realtime * 1000.0), cyc, NAME);
      prev_rst = 1'b1;
      case (state)
        0: if (head < tail) begin
          // SETUP of the oldest descriptor.
          j = head % DEPTH;
          psel <= 1'b1;
          penable <= 1'b0;
          pwrite <= q_write[j];
          paddr <= q_addr[j];
          pprot <= DIALECT == 4 ? q_prot[j] : 3'd0;
          state = 1;
          // The address handshake of the transfer: its SETUP cycle.
          if (q_write[j])
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"aw\",\"tag\":%0d,\"id\":0,\"a\":\"%0h\",\"len\":0,\"sz\":%0d,\"bu\":1,\"lk\":0,\"ca\":0,\"pr\":%0d,\"qo\":0}\n",
                    ($realtime * 1000.0), cyc, NAME, q_tag[j], q_addr[j], SIZE, eff_prot(q_prot[j]));
          else
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"ar\",\"tag\":%0d,\"id\":0,\"a\":\"%0h\",\"len\":0,\"sz\":%0d,\"bu\":1,\"lk\":0,\"ca\":0,\"pr\":%0d,\"qo\":0}\n",
                    ($realtime * 1000.0), cyc, NAME, q_tag[j], q_addr[j], SIZE, eff_prot(q_prot[j]));

        end
        1: begin
          penable <= 1'b1;
          state = 2;
        end
        default: if (pready === 1'b1) begin
          j = head % DEPTH;
          if (q_write[j]) begin
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"w\",\"tag\":%0d,\"d\":\"%0h\",\"s\":\"%0h\",\"l\":1}\n",
                    ($realtime * 1000.0), cyc, NAME, q_tag[j], pwdata,
                    DIALECT == 4 ? pstrb : {NB{1'b1}});
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"b\",\"id\":0,\"r\":%0d}\n",
                    ($realtime * 1000.0), cyc, NAME, pslverr === 1'b1 ? 2 : 0);
            n_b = n_b + 1;
          end else begin
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"r\",\"id\":0,\"d\":\"%0h\",\"r\":%0d,\"l\":1}\n",
                    ($realtime * 1000.0), cyc, NAME, prdata, pslverr === 1'b1 ? 2 : 0);
            n_r = n_r + 1;
          end
          head = head + 1;
          psel <= 1'b0;
          penable <= 1'b0;
          state = 0;
        end
      endcase
      // Write data and strobes of the descriptor entering SETUP.
      if (state == 1) begin
        j = head % DEPTH;
        for (integer l = 0; l < NB; l = l + 1) begin
          pwdata[l*8+:8] <= nlv_source_byte((q_addr[j] & ~(NB - 1)) + l, q_salt[j]);
          pstrb[l] <= q_write[j] && strobe_of(q_strb[j], l);
        end
      end
    end
  end
endmodule
