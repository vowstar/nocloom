// Library-style cell models for the cell binding checks (run_async_fifo_formal.py cells).
`default_nettype none
// Positive-edge integrated clock gate with a test enable: while CP is low the latch follows
// E | TE.
module lib_icg_te (
  input wire CP,
  input wire E,
  input wire TE,
  output wire Q
);
reg open;
always @(CP or E or TE)
  if (!CP)
    open <= E | TE;
assign Q = CP & open;
endmodule
// Integrated clock gate without a test pin; RN clears the latch (the binding ties it high).
module lib_icg_rn (
  input wire CK,
  input wire EN,
  input wire RN,
  output wire GCK
);
reg open;
always @(CK or EN or RN)
  if (!RN)
    open <= 1'b0;
  else if (!CK)
    open <= EN;
assign GCK = CK & open;
endmodule
// Scan flip-flop with an active-low clear: SE selects SI (the binding ties both low).
module lib_sdff_rn (
  input wire CK,
  input wire D,
  input wire SI,
  input wire SE,
  input wire RN,
  output reg Q
);
always @(posedge CK or negedge RN)
  if (!RN)
    Q <= 1'b0;
  else
    Q <= SE ? SI : D;
endmodule
// Two-stage synchronizer with an active-high clear.
module lib_sync2_r (
  input wire CK,
  input wire D,
  input wire R,
  output wire Q
);
reg [1:0] stage;
always @(posedge CK or posedge R)
  if (R)
    stage <= 2'b00;
  else
    stage <= {stage[0], D};
assign Q = stage[1];
endmodule
`default_nettype wire
