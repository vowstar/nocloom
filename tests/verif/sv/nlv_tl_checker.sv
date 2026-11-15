`timescale 1ns/1ps
// TileLink 1.8.1 checks at accepted A and D beats. Section 4.1 permits
// retraction before acceptance, so a stalled sender need not hold valid.
module nlv_tl_checker #(
    parameter NAME="tl", ADDR_W=32, SOURCE_W=4, SIZE_W=2, UH=0, SOURCE_LIMIT=0
)(
    input wire clk, rst_n,
    input wire allow_reject,
    input wire [2:0] a_opcode, a_param,
    input wire [SIZE_W-1:0] a_size,
    input wire [SOURCE_W-1:0] a_source,
    input wire [ADDR_W-1:0] a_address,
    input wire [3:0] a_mask,
    input wire [31:0] a_data,
    input wire a_corrupt, a_valid, a_ready,
    input wire [2:0] d_opcode,
    input wire [1:0] d_param,
    input wire [SIZE_W-1:0] d_size,
    input wire [SOURCE_W-1:0] d_source,
    input wire [31:0] d_data,
    input wire d_corrupt, d_denied, d_valid, d_ready
);
  reg busy[0:(1<<SOURCE_W)-1];
  reg [2:0] response_opcode[0:(1<<SOURCE_W)-1];
  reg [SIZE_W-1:0] sizes[0:(1<<SOURCE_W)-1];
  integer i, active_mask, n, inflight, peak=0, a_left=0;
  integer left[0:(1<<SOURCE_W)-1];
  reg reset_seen=0;
  reg [2:0] a_owner_opcode, a_owner_param, d_owner_opcode;
  reg [SIZE_W-1:0] a_owner_size, d_owner_size;
  reg [SOURCE_W-1:0] a_owner_source, d_owner_source;
  reg [ADDR_W-1:0] a_owner_address;
  reg d_owner=0, d_owner_denied;
  integer d_left=0;

  always @(posedge clk) begin
    if (!rst_n) begin
      if (!reset_seen)
        for (i=0; i<(1<<SOURCE_W); i=i+1) busy[i]=0;
      reset_seen=1; inflight=0; peak=0;
      a_left=0; d_owner=0; d_left=0;
      // Section 3.2.2: valid is low during reset.
      if (a_valid || d_valid) tb.nlv_fail_report("TL.3.2.2.RESET", NAME, 0);
    end else begin
      reset_seen=0;
      if(SOURCE_LIMIT!=0 && a_valid && a_source>=SOURCE_LIMIT)
        tb.nlv_fail_report("TL.6.4.RANGE", NAME, a_source);
      if(SOURCE_LIMIT!=0 && d_valid && d_source>=SOURCE_LIMIT)
        tb.nlv_fail_report("TL.6.4.RANGE", NAME, d_source);
      if (a_valid && a_left!=0) begin
        if (a_opcode!=a_owner_opcode || a_param!=a_owner_param || a_size!=a_owner_size ||
            a_source!=a_owner_source || a_address!=a_owner_address)
          tb.nlv_fail_report("TL.4.1.A_OWNER", NAME, a_source);
        if (a_ready) begin
          if (a_opcode!=1 && a_mask!=15) tb.nlv_fail_report("TL.4.5.MASK", NAME, a_mask);
          a_left=a_left-1;
        end
      end
      else if (a_valid && a_ready) begin
        // Section 6.4: the source remains unique through the response edge.
        if (busy[a_source]) tb.nlv_fail_report("TL.6.4.SOURCE", NAME, a_source);
        // Sections 7.2.1 to 7.2.3: single-beat access operations.
        if (!allow_reject && a_opcode!=0 && a_opcode!=1 && a_opcode!=4 && !(UH && a_opcode==5))
          tb.nlv_fail_report("TL.7.2.OPCODE", NAME, a_opcode);
        if (!UH && !allow_reject && a_size>2) tb.nlv_fail_report("TL.7.2.SIZE", NAME, a_size);
        // Section 4.5: aligned power-of-two size and legal byte lanes.
        n=1<<a_size;
        active_mask=n>=4 ? 15 : ((1<<n)-1) << (a_address % 4);
        if (a_address % n) tb.nlv_fail_report("TL.4.5.ALIGN", NAME, a_address);
        if ((a_mask & ~active_mask)!=0 || (a_opcode!=1 && a_mask!=active_mask))
          tb.nlv_fail_report("TL.4.5.MASK", NAME, a_mask);
        if (!allow_reject && (a_param!=0 || (a_opcode==4 && a_corrupt)))
          tb.nlv_fail_report("TL.7.2.PARAM", NAME, a_param);
        a_left=a_opcode<4 && a_size>2 ? (1<<(a_size-2))-1 : 0;
        a_owner_opcode=a_opcode; a_owner_param=a_param; a_owner_size=a_size;
        a_owner_source=a_source; a_owner_address=a_address;
        busy[a_source]=1;
        inflight=inflight+1;
        response_opcode[a_source]=(a_opcode==4 || a_opcode==2 || a_opcode==3) ? 1 : a_opcode==5 ? 2 : 0;
        left[a_source]=response_opcode[a_source]==1 && a_size>2 ? (1<<(a_size-2))-1 : 0;
        sizes[a_source]=a_size;
      end
      if (d_valid && d_owner) begin
        if (d_opcode!=d_owner_opcode || d_size!=d_owner_size || d_source!=d_owner_source ||
            d_denied!=d_owner_denied || d_param!=0)
          tb.nlv_fail_report("TL.4.2.D_OWNER", NAME, d_source);
      end
      if (d_valid && d_ready) begin
        if (!d_owner) begin
          if (!busy[d_source]) tb.nlv_fail_report("TL.4.2.RESPONSE", NAME, d_source);
          if (d_opcode!=response_opcode[d_source] || d_size!=sizes[d_source] || d_param!=0)
            tb.nlv_fail_report("TL.7.2.RESPONSE", NAME, d_opcode);
          d_owner_opcode=d_opcode; d_owner_size=d_size; d_owner_source=d_source;
          d_owner_denied=d_denied;
          d_left=left[d_source]+1;
          busy[d_source]=0;
        end
        if ((d_opcode==1 && d_denied && !d_corrupt) || (d_opcode!=1 && d_corrupt))
          tb.nlv_fail_report("TL.4.4.CORRUPT", NAME, d_corrupt);
        d_left=d_left-1;
        d_owner=d_left!=0;
        if (!d_owner) inflight=inflight-1;
      end
      if (inflight>peak) peak=inflight;
    end
  end
endmodule
