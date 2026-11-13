`timescale 1ns/1ps
// Full-width, non-bufferable SINGLE transfers on an AHB-Lite initiator.
// HREADY follows HREADYOUT for a single subordinate. Address and data phases
// use separate cycles. Events use the tape checker's single-beat format.
module nlv_ahb_master #(
    parameter NAME = "m",
    parameter ADDR_W = 32,
    parameter DATA_W = 32,
    parameter DEPTH = 64
) (
    input wire clk,
    input wire rst_n,
    output reg hsel,
    output reg [ADDR_W-1:0] haddr,
    output reg [1:0] htrans,
    output reg hwrite,
    output wire [2:0] hsize,
    output wire [2:0] hburst,
    output reg [3:0] hprot,
    output wire hmastlock,
    output reg [DATA_W-1:0] hwdata,
    output wire hready,
    input wire hreadyout,
    input wire hresp,
    input wire [DATA_W-1:0] hrdata
);
  `include "nlv_common.svh"
  localparam NB = DATA_W / 8;
  localparam [2:0] SIZE = $clog2(NB);
  assign hsize = SIZE;
  assign hburst = 3'd0;
  assign hmastlock = 1'b0;
  assign hready = hreadyout;
  reg [63:0] q_addr[0:DEPTH-1];
  reg q_write[0:DEPTH-1];
  reg [2:0] q_prot[0:DEPTH-1];
  reg [31:0] q_salt[0:DEPTH-1], q_tag[0:DEPTH-1];
  integer head = 0, tail = 0, state = 0, j;
  reg [63:0] cyc = 0, n_b = 0, n_r = 0, n_wpush = 0, n_rpush = 0;
  reg prev_rst = 0, err_first = 0;

  function [63:0] counter(input integer index);
    case (index)
      0, 1, 2: counter = n_b;
      3, 4, 5: counter = n_r;
      6: counter = n_wpush;
      7: counter = n_rpush;
      10: counter = n_wpush - n_b;
      11: counter = n_rpush - n_r;
      18: counter = cyc;
      default: counter = 0;
    endcase
  endfunction

  // AxPROT bit 1 is always set. HPROT has no security attribute.
  function [2:0] eff_prot(input [2:0] prot);
    eff_prot = {prot[2], 1'b1, prot[0]};
  endfunction

  task push(input wr, input [63:0] addr, input [2:0] prot,
            input [31:0] salt, input [31:0] tag);
    integer slot;
    begin
      if (tail - head >= DEPTH) tb.nlv_fail_report("TB.QUEUE_FULL", NAME, tail);
      slot = tail % DEPTH;
      q_addr[slot] = addr;
      q_write[slot] = wr;
      q_prot[slot] = eff_prot(prot);
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

  task push_write(input [63:0] addr, input [7:0] len, input [2:0] size,
                  input [1:0] burst, input [31:0] id, input lock,
                  input [3:0] cache, input [2:0] prot, input [3:0] qos,
                  input [7:0] strb_mode, input [31:0] salt, input [31:0] tag);
    push(1'b1, addr, prot, salt, tag);
  endtask

  task push_read(input [63:0] addr, input [7:0] len, input [2:0] size,
                 input [1:0] burst, input [31:0] id, input lock,
                 input [3:0] cache, input [2:0] prot, input [3:0] qos,
                 input [31:0] tag);
    push(1'b0, addr, prot, 0, tag);
  endtask

  task set_ctl(input integer index, input [31:0] value);
    tb.nlv_fail_report("TB.TAPE", NAME, index);
  endtask

  initial begin
    hsel = 0; haddr = 0; htrans = 0; hwrite = 0; hprot = 0; hwdata = 0;
  end

  always @(posedge clk) begin
    cyc = cyc + 1;
    if (rst_n != prev_rst)
      $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"rst\",\"v\":%0d}\n",
              ($realtime * 1000.0), cyc, NAME, rst_n);
    prev_rst = rst_n;
    if (!rst_n) begin
      head = 0; tail = 0; state = 0; err_first = 0;
      n_wpush = n_b; n_rpush = n_r;
      hsel <= 0; htrans <= 0;
    end else begin
      case (state)
        0: if (head < tail) begin
          j = head % DEPTH;
          hsel <= 1; htrans <= 2;
          haddr <= q_addr[j]; hwrite <= q_write[j];
          hprot <= {2'b00, q_prot[j][0], ~q_prot[j][2]};
          for (integer lane = 0; lane < NB; lane = lane + 1)
            hwdata[lane*8+:8] <= nlv_source_byte((q_addr[j] & ~(NB - 1)) + lane, q_salt[j]);
          state = 1;
        end
        1: if (hreadyout) begin
          j = head % DEPTH;
          $fwrite(tb.nlv_fd,
                  "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"%0s\",\"tag\":%0d,\"id\":0,\"a\":\"%0h\",\"len\":0,\"sz\":%0d,\"bu\":1,\"lk\":0,\"ca\":0,\"pr\":%0d,\"qo\":0}\n",
                  ($realtime * 1000.0), cyc, NAME, q_write[j] ? "aw" : "ar",
                  q_tag[j], haddr, SIZE, q_prot[j]);
          if (q_write[j])
            $fwrite(tb.nlv_fd,
                    "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"w\",\"tag\":%0d,\"d\":\"%0h\",\"s\":\"%0h\",\"l\":1}\n",
                    ($realtime * 1000.0), cyc, NAME, q_tag[j], hwdata, {NB{1'b1}});
          hsel <= 0; htrans <= 0;
          state = 2;
        end
        2: begin
          if (err_first && (!hresp || !hreadyout))
            tb.nlv_fail_report("AHB.RESPONSE.TWO_CYCLE", NAME, cyc);
          if (hresp && !hreadyout) err_first = 1;
          if (hreadyout) begin
            j = head % DEPTH;
            if (hresp != err_first)
              tb.nlv_fail_report("AHB.RESPONSE.TWO_CYCLE", NAME, cyc);
            if (q_write[j]) begin
              $fwrite(tb.nlv_fd,
                      "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"b\",\"id\":0,\"r\":%0d}\n",
                      ($realtime * 1000.0), cyc, NAME, hresp ? 2 : 0);
              n_b = n_b + 1;
            end else begin
              $fwrite(tb.nlv_fd,
                      "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"r\",\"id\":0,\"d\":\"%0h\",\"r\":%0d,\"l\":1}\n",
                      ($realtime * 1000.0), cyc, NAME, hrdata, hresp ? 2 : 0);
              n_r = n_r + 1;
            end
            head = head + 1; state = 0; err_first = 0;
          end
        end
      endcase
    end
  end
endmodule
