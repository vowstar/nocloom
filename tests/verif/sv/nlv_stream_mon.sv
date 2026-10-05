`timescale 1ns/1ps
// Passive monitor for one valid/ready channel between generated components
// (a white-box observation point, never a product port). Logs each transfer
// with its packed payload; the checker splits fields by the bench's field
// table. Checks VALID and payload hold until READY.
module nlv_stream_mon #(
    parameter NAME = "s",
    parameter W = 8
) (
    input wire clk,
    input wire rst_n,
    input wire valid,
    input wire ready,
    input wire [W-1:0] payload
);
  reg prev_valid, prev_ready;
  reg [W-1:0] prev_payload;
  reg [63:0] cyc, n;
  initial begin
    prev_valid = 0; prev_ready = 0; prev_payload = 0; cyc = 0; n = 0;
  end
  function [63:0] counter;
    input integer index;
    begin
      counter = index == 0 ? n : index == 8 ? valid : 0;
    end
  endfunction
  always @(posedge clk) begin
    cyc = cyc + 1;
    if (rst_n) begin
      if (prev_valid && !prev_ready && (!valid || payload !== prev_payload))
        tb.nlv_fail_report("STREAM.HOLD", NAME, 0);
      if (valid && ready) begin
        $fwrite(tb.nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"p\":\"%0s\",\"k\":\"hs\",\"ch\":\"x\",\"x\":\"%0h\"}\n",
                ($realtime * 1000.0), cyc, NAME, payload);
        n = n + 1;
      end
    end
    prev_valid = rst_n && valid;
    prev_ready = ready;
    prev_payload = payload;
  end
endmodule
