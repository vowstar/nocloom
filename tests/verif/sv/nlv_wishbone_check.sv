// WISHBONE B4 RULE 3.20, 3.25, 3.30, 3.35, 3.45, 3.50, 3.60.
// Read data is sampled by the scoreboard under RULE 3.65.
module nlv_wishbone_check #(
    parameter NAME = "wb", parameter ADDR_W = 32, parameter ALLOW_ABORT = 0,
    parameter CONSTANT_ACK = 0, parameter PIPELINED = 0
) (
    input wire clk, rst_n, cyc, stb, we, ack, err, rty,
    input wire constant_ack,
    input wire stall,
    input wire [ADDR_W-1:0] adr,
    input wire [31:0] dat_w, dat_r,
    input wire [3:0] sel
);
  integer outstanding = 0;
  reg was_reset = 0;
  reg waiting = 0;
  reg [ADDR_W+36:0] held;
  always @(posedge clk) begin
    was_reset <= !rst_n;
    if (was_reset && (cyc || stb)) tb.nlv_fail_report("WB.RULE_3_20", NAME, 0);
    if (!rst_n) begin
      waiting <= 0;
      outstanding = 0;
    end else begin
      if (stb && !cyc) tb.nlv_fail_report("WB.RULE_3_25", NAME, 0);
      if ((ack && err) || (ack && rty) || (err && rty))
        tb.nlv_fail_report("WB.RULE_3_45", NAME, 0);
      if ((CONSTANT_ACK || constant_ack) && !ack)
        tb.nlv_fail_report("WB.RULE_3_35", NAME, 0);
      if ((err || rty || (ack && !CONSTANT_ACK && !constant_ack)) && !cyc)
        tb.nlv_fail_report("WB.RULE_3_30", NAME, 0);
      if (!PIPELINED &&
          (err || rty || (ack && !CONSTANT_ACK && !constant_ack)) && !stb)
        tb.nlv_fail_report("WB.RULE_3_50", NAME, 0);
      if (waiting && (!ALLOW_ABORT || (cyc && stb)) &&
          (!(cyc && stb) || {adr, we, sel, dat_w} !== held))
        tb.nlv_fail_report("WB.RULE_3_60", NAME, 0);
      // B4 RULE 3.57, 3.58 and 3.59 separate acceptance from termination.
      if (PIPELINED) begin
        if (cyc && stb && !stall) outstanding = outstanding + 1;
        if (cyc && (ack || err || rty)) begin
          if (outstanding == 0) tb.nlv_fail_report("WB.RULE_3_59", NAME, 0);
          else outstanding = outstanding - 1;
        end
        if (!cyc && ALLOW_ABORT) outstanding = 0;
      end
      waiting <= cyc && stb && (PIPELINED ? stall : !(ack || err || rty));
      held <= {adr, we, sel, dat_w};
    end
  end
endmodule
