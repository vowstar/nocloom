`timescale 1ns/1ps
module tb;
  reg clk = 0, reset_n = 0;
  always #5 clk = ~clk;

  reg arvalid = 0, rready = 0;
  reg [3:0] arid = 0;
  wire arready, rvalid, rlast;
  wire [3:0] rid;
  wire [1:0] rresp;
  wire [31:0] rdata;

  reg d_valid = 0;
  reg [1:0] d_source = 0;
  reg [31:0] d_data = 0;
  wire d_ready, a_valid;
  wire [1:0] a_source;
  wire [15:0] a_address;
  wire [2:0] a_opcode, a_size;

  function automatic [15:0] read_address(input [3:0] id);
    read_address = 16'h0100 + {8'd0, id, 4'd0};
  endfunction

  nocloom_uh_read_rotation dut(
    .clk(clk), .reset_n(reset_n),
    .nl_i_s0_awid(4'd0), .nl_i_s0_awaddr(16'd0), .nl_i_s0_awlen(4'd0),
    .nl_i_s0_awsize(3'd2), .nl_i_s0_awburst(2'd1), .nl_i_s0_awlock(1'b0),
    .nl_i_s0_awcache(4'd0), .nl_i_s0_awprot(3'd0), .nl_i_s0_awvalid(1'b0),
    .nl_i_s0_wdata(32'd0), .nl_i_s0_wstrb(4'd0), .nl_i_s0_wlast(1'b0),
    .nl_i_s0_wvalid(1'b0), .nl_i_s0_bready(1'b1),
    .nl_i_s0_arid(arid), .nl_i_s0_araddr(read_address(arid)), .nl_i_s0_arlen(4'd0),
    .nl_i_s0_arsize(3'd2), .nl_i_s0_arburst(2'd1), .nl_i_s0_arlock(1'b0),
    .nl_i_s0_arcache(4'd0), .nl_i_s0_arprot(3'd0), .nl_i_s0_arvalid(arvalid),
    .nl_i_s0_arready(arready), .nl_i_s0_rready(rready), .nl_i_s0_rvalid(rvalid),
    .nl_i_s0_rid(rid), .nl_i_s0_rdata(rdata), .nl_i_s0_rresp(rresp), .nl_i_s0_rlast(rlast),
    .nl_t_t0_a_ready(1'b1), .nl_t_t0_a_valid(a_valid), .nl_t_t0_a_source(a_source),
    .nl_t_t0_a_address(a_address), .nl_t_t0_a_opcode(a_opcode), .nl_t_t0_a_size(a_size),
    .nl_t_t0_d_valid(d_valid), .nl_t_t0_d_ready(d_ready), .nl_t_t0_d_source(d_source),
    .nl_t_t0_d_data(d_data), .nl_t_t0_d_opcode(3'd1), .nl_t_t0_d_param(2'd0),
    .nl_t_t0_d_size(3'd2), .nl_t_t0_d_denied(1'b0), .nl_t_t0_d_corrupt(1'b0)
  );

  integer gets = 0, responses = 0;
  reg [3:0] sent_id [0:3];
  reg [1:0] expected_source [0:3];
  reg [3:0] order [0:3];
  always @(posedge clk) begin
    if (reset_n && a_valid) begin
      if (gets >= 4 || a_opcode !== 3'd4 || a_size !== 3'd2 ||
          a_address !== read_address(sent_id[gets]) || a_source !== expected_source[gets])
        $fatal(1, "Unexpected Get %0d: op=%0d size=%0d address=%h source=%0d",
               gets, a_opcode, a_size, a_address, a_source);
      gets = gets + 1;
    end
    if (reset_n && rvalid && rready) begin
      if (responses >= 4 || rresp !== 2'd0 || rlast !== 1'b1 || rdata !== (32'h1000 | {28'd0, rid}))
        $fatal(1, "Unexpected AXI response %0d: id=%0d resp=%0d last=%0d data=%h",
               responses, rid, rresp, rlast, rdata);
      order[responses] = rid;
      responses = responses + 1;
    end
  end

  task automatic send_read(input [3:0] id);
    @(negedge clk);
    arid = id;
    arvalid = 1;
    do @(posedge clk); while (arready !== 1'b1);
    @(negedge clk);
    arvalid = 0;
  endtask

  task automatic reply(input [1:0] source, input [3:0] id);
    @(negedge clk);
    d_source = source;
    d_data = 32'h1000 | {28'd0, id};
    d_valid = 1;
    do @(posedge clk); while (d_ready !== 1'b1);
    @(negedge clk);
    d_valid = 0;
  endtask

  initial begin
    {sent_id[0], sent_id[1], sent_id[2], sent_id[3]} = {4'd0, 4'd12, 4'd13, 4'd14};
    {expected_source[0], expected_source[1], expected_source[2], expected_source[3]} =
        {2'd0, 2'd0, 2'd1, 2'd2};
    repeat (8) @(negedge clk);
    reset_n = 1;
    repeat (8) @(negedge clk);

    // Fill the response path with one held read, then fill all three target contexts.
    send_read(4'd0);
    wait (gets == 1);
    reply(2'd0, 4'd0);
    repeat (12) @(negedge clk);
    if (rvalid !== 1'b1 || rid !== 4'd0)
      $fatal(1, "The first read is not held at the AXI port");
    send_read(4'd12);
    send_read(4'd13);
    send_read(4'd14);
    wait (gets == 4);

    // Context 1 takes the response channel while 0 and 2 both queue a reply.
    // After context 1, the turn points to 2 and must service it before wrapping to 0.
    reply(2'd1, 4'd13);
    reply(2'd0, 4'd12);
    reply(2'd2, 4'd14);
    repeat (30) @(negedge clk);
    rready = 1;
    wait (responses == 4);
    repeat (12) @(negedge clk);
    if ({order[0], order[1], order[2], order[3]} !== {4'd0, 4'd13, 4'd14, 4'd12})
      $display("CHECK_FAIL READ_ROTATION got=%0d,%0d,%0d,%0d", order[0], order[1], order[2], order[3]);
    else
      $display("UH_PASS");
    $finish;
  end

  initial begin
    repeat (400) @(negedge clk);
    $fatal(1, "Read rotation timed out: gets=%0d responses=%0d", gets, responses);
  end
endmodule
