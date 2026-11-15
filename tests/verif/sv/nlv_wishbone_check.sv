// WISHBONE B4 RULE 3.20, 3.25, 3.30, 3.35, 3.45, 3.50, 3.60.
// Read data is sampled by the scoreboard under RULE 3.65.
module nlv_wishbone_check #(
    parameter NAME = "wb", parameter ADDR_W = 32, parameter ALLOW_ABORT = 0,
    parameter CONSTANT_ACK = 0, parameter PIPELINED = 0,
    parameter REGISTERED_FEEDBACK = 0, parameter MASTER = 0, parameter WORD = 0
) (
    input wire clk, rst_n, cyc, stb, we, ack, err, rty,
    input wire constant_ack,
    input wire stall,
    input wire [ADDR_W-1:0] adr,
    input wire [31:0] dat_w, dat_r,
    input wire [2:0] cti,
    input wire [1:0] bte,
    input wire [3:0] sel
);
  integer outstanding = 0;
  reg was_reset = 0;
  reg waiting = 0;
  reg [ADDR_W+36:0] held;
  reg [4:0] held_tags;
  reg owed = 0;
  reg [2:0] previous_cti;
  reg [1:0] previous_bte;
  reg [ADDR_W-1:0] expected_adr;
  reg expected_we;
  reg [3:0] expected_sel;
  function [ADDR_W-1:0] next_address(input [ADDR_W-1:0] a, input [1:0] b);
    reg [ADDR_W-1:0] mask, step;
    begin
      mask = b == 1 ? (WORD ? 3 : 15) : b == 2 ? (WORD ? 7 : 31) :
             b == 3 ? (WORD ? 15 : 63) : {ADDR_W{1'b1}};
      step = a + (WORD ? 1 : 4);
      next_address = (a & ~mask) | (step & mask);
    end
  endfunction
  always @(posedge clk) begin
    was_reset <= !rst_n;
    if (was_reset && (cyc || stb)) tb.nlv_fail_report("WB.RULE_3_20", NAME, 0);
    if (!rst_n) begin
      waiting <= 0;
      outstanding = 0;
      owed <= 0;
    end else begin
      if (stb && !cyc) tb.nlv_fail_report("WB.RULE_3_25", NAME, 0);
      if ((ack && err) || (ack && rty) || (err && rty))
        tb.nlv_fail_report("WB.RULE_3_45", NAME, 0);
      if ((CONSTANT_ACK || constant_ack) && !ack)
        tb.nlv_fail_report("WB.RULE_3_35", NAME, 0);
      if ((err || rty || (ack && !CONSTANT_ACK && !constant_ack)) && !cyc)
        tb.nlv_fail_report("WB.RULE_3_30", NAME, 0);
      if (!PIPELINED && !(REGISTERED_FEEDBACK && MASTER) &&
          (err || rty || (ack && !CONSTANT_ACK && !constant_ack)) && !stb)
        tb.nlv_fail_report("WB.RULE_3_50", NAME, 0);
      if (waiting && (!ALLOW_ABORT || (cyc && stb)) &&
          (!(cyc && stb) || {adr, we, sel, dat_w} !== held))
        tb.nlv_fail_report("WB.RULE_3_60", NAME, 0);
      // RULE 4.00 and 4.05 allow classic and final transfers with tags.
      // RULE 4.10 and 4.25 permit a slave to serve every offered word classically.
      // RULE 4.15 consumes terminations only on valid STB edges.
      if (REGISTERED_FEEDBACK) begin
        if (waiting && (!ALLOW_ABORT || (cyc && stb)) && {cti, bte} !== held_tags)
          tb.nlv_fail_report("WB.RULE_3_60", NAME, 0);
        if (!cyc && owed && !ALLOW_ABORT)
          tb.nlv_fail_report("WB.RULE_4_30", NAME, 0);
        if (!cyc && ALLOW_ABORT) owed <= 0;
        if (cyc && stb) begin
          if (MASTER && cti != 0 && cti != 1 && cti != 2 && cti != 7)
            tb.nlv_fail_report("WB.RULE_4_00", NAME, 0);
          if (owed) begin
            if (previous_cti == 2 && bte !== previous_bte)
              tb.nlv_fail_report("WB.RULE_4_20", NAME, 0);
            if ((previous_cti == 2 && cti != 2 && cti != 7) ||
                (previous_cti == 1 && cti != 1 && cti != 7))
              tb.nlv_fail_report("WB.RULE_4_30", NAME, 0);
            if ({adr, we, sel} !== {expected_adr, expected_we, expected_sel})
              if (previous_cti == 1) tb.nlv_fail_report("WB.RULE_4_35", NAME, 0);
              else tb.nlv_fail_report("WB.RULE_4_40", NAME, 0);
          end
          if (ack || err || rty) begin
            owed <= cti == 1 || cti == 2;
            previous_cti <= cti;
            previous_bte <= bte;
            expected_adr <= cti == 1 ? adr : next_address(adr, bte);
            expected_we <= we;
            expected_sel <= sel;
          end
        end
      end
      held_tags <= {cti, bte};
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
