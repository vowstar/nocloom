`timescale 1ns/1ps
module tb;
  reg clk=0, rst_n=0;
  always #5 clk=~clk;
  integer nlv_fd, writes=0;
  reg [2:0] a_opcode=0;
  reg [1:0] a_size=0;
  reg [31:0] a_address=0, a_data=0;
  reg [3:0] a_mask=0;
  reg a_valid=0, d_ready=0;
  wire a_ready, d_valid, d_denied, d_corrupt;
  wire [2:0] d_opcode;
  wire [1:0] d_size;
  wire [31:0] d_data;
  reg [31:0] expected, got, word_data;
  integer size, lane, mask, active, byte_lane;
  reg [4095:0] events;

  task nlv_fail_report(input [511:0] rule, input [511:0] port_name,
                       input [63:0] detail);
    $fatal(1, "CHECK_FAIL:%0s:%0s:%0h", rule, port_name, detail);
  endtask

  nlv_tl_slave #(.NAME("peer"), .MEM_BYTES(256), .SALT(1)) peer (
    .clk(clk), .rst_n(rst_n), .sideband(1'b0),
    .a_opcode(a_opcode), .a_param(3'd0), .a_size(a_size), .a_source(4'd0),
    .a_address(a_address), .a_mask(a_mask), .a_data(a_data), .a_corrupt(1'b0),
    .a_valid(a_valid), .a_ready(a_ready),
    .d_opcode(d_opcode), .d_param(), .d_size(d_size), .d_source(), .d_sink(),
    .d_data(d_data), .d_corrupt(d_corrupt), .d_denied(d_denied),
    .d_valid(d_valid), .d_ready(d_ready)
  );

  task access(input [2:0] opcode, input [1:0] sz, input [31:0] address,
              input [3:0] strobe, input [31:0] data, output [31:0] result);
    begin
      @(negedge clk);
      a_opcode=opcode; a_size=sz; a_address=address;
      a_mask=strobe; a_data=data; a_valid=1;
      @(posedge clk);
      while (!a_ready) @(posedge clk);
      @(negedge clk);
      a_valid=0; a_mask=~strobe; a_data=~data;
      while (!d_valid) @(negedge clk);
      result=d_data;
      repeat (3) begin
        if (!d_valid || d_denied || d_corrupt || d_size!=sz ||
            d_opcode!=(opcode==4 ? 1 : 0) || d_data!==result)
          $fatal(1, "CHECK_FAIL:TL.PEER.RESPONSE");
        @(negedge clk);
      end
      d_ready=1;
      @(posedge clk);
      @(negedge clk);
      d_ready=0;
    end
  endtask

  initial begin
    if (!$value$plusargs("NLV_EVENTS=%s", events))
      $fatal(1, "CHECK_FAIL:TL.PEER.EVENT_PATH");
    nlv_fd=$fopen(events, "w");
    if (!nlv_fd) $fatal(1, "CHECK_FAIL:TL.PEER.EVENT_FILE");
    repeat (100) @(negedge clk);
    rst_n=1;
    peer.set_ctl(0, 5);
    access(4, 2, 32'h10, 15, 0, expected);
    for (size=0; size<=2; size=size+1)
      for (lane=0; lane<4; lane=lane+(1<<size)) begin
        active=((1<<(1<<size))-1)<<lane;
        // All subsets include zero, each individual byte and the full size mask.
        for (mask=0; mask<16; mask=mask+1)
          if ((mask & ~active)==0) begin
            word_data=~expected;
            access(1, size, 32'h10+lane, mask, word_data, got);
            for (byte_lane=0; byte_lane<4; byte_lane=byte_lane+1)
              if (mask[byte_lane]) expected[8*byte_lane+:8]=word_data[8*byte_lane+:8];
            access(4, 2, 32'h10, 15, 0, got);
            if (got!==expected)
              $fatal(1, "CHECK_FAIL:TL.PEER.MASK size=%0d lane=%0d mask=%0h got=%0h want=%0h",
                     size, lane, mask, got, expected);
            writes=writes+1;
          end
        word_data=~expected;
        access(0, size, 32'h10+lane, active, word_data, got);
        for (byte_lane=0; byte_lane<4; byte_lane=byte_lane+1)
          if (active[byte_lane]) expected[8*byte_lane+:8]=word_data[8*byte_lane+:8];
        access(4, 2, 32'h10, 15, 0, got);
        if (got!==expected) $fatal(1, "CHECK_FAIL:TL.PEER.FULL_MASK");
        writes=writes+1;
      end
    if (writes!=39) $fatal(1, "CHECK_FAIL:TL.PEER.COVERAGE");
    $fclose(nlv_fd);
    $display("NLV_DONE writes=%0d", writes);
    $finish;
  end
  initial begin
    #200000;
    $fatal(1, "CHECK_FAIL:TL.PEER.TIMEOUT");
  end
endmodule
