"""Transaction bench for exclusive access (AXI A7.2) through the public network.

Independent checks:

- Every step's RRESP/BRESP and read data against the AMBA model in
  exclusive.py (a reservation per {source, AxID} key, cleared by any
  overlapping write, one per key).
- Target access: a rejected exclusive write never reaches the target, a
  failed one changes nothing: an AXI4 or AXI4-Lite target receives it with
  WSTRB zero, an APB or AHB target sees no transfer (the target models count
  every committed beat with strobes and, apart, every beat without), a target without
  exclusive support is written by an exclusive write (AXI A7.2.5) and never
  sees AxLOCK, a target-mode AXI4 target sees AxLOCK and an ID encoding
  the exclusive owner flags described by definition.target_ids in the map.
- Target-mode targets are AXI4 slaves with their own monitor keyed by the full
  AxID. The exclusive owner flags distinguish reservations from different
  sources and source IDs.
- Atomic increments: both sources run exclusive read/increment/write loops on
  one counter per target; a lost update shows as a final count below the
  successes or as two successes writing the same value. This checks
  atomicity whatever the monitor's replacement order.
"""

from exclusive import (CLOCKS, EXOKAY, ID_BITS, OKAY, SLVERR, SOURCES, WINDOW, Model,
                       source_width, target_width, targets)

QUEUE = 256  # every beat count representable by the eight-bit AxLEN port
# Network-monitor targets whose outlet serves read and write in arrival order:
# a read waits until an earlier write has completed and its B has left. The
# network serves a shared-bus target (APB, AXI4-Lite) so at any capacity; the
# AXI4 outlet's in-flight pool of one serializes the directions the same way.
# An AXI4 outlet of several bursts overlaps them: a read that meets a write in
# flight over its region records nothing.
SERVED_IN_ORDER = ('tp', 'tm', 'tl')


def _fill(text, **values):
    for key, value in values.items():
        text = text.replace('@' + key + '@', str(value))
    return text


_SOURCE = '''
reg [@DW@-1:0] @S@_wq_data [0:@Q@-1]; reg [@SB@-1:0] @S@_wq_strb [0:@Q@-1];
reg [@DW@-1:0] @S@_rdata [0:@Q@-1]; reg [1:0] @S@_rresp [0:@Q@-1]; reg [1:0] @S@_bresp;
integer @S@_gap = 0, @S@_race_done = 0, @S@_race_tries = 0;
reg [15:0] @S@_lfsr = 16'h@SEED@;
always @(negedge clk_main) begin
 @S@_lfsr <= {@S@_lfsr[14:0], @S@_lfsr[15] ^ @S@_lfsr[13] ^ @S@_lfsr[12] ^ @S@_lfsr[10]};
 nl_i_@S@_rready <= @S@_gap == 0 || @S@_lfsr[2:0] >= @S@_gap;
 nl_i_@S@_bready <= @S@_gap == 0 || @S@_lfsr[5:3] >= @S@_gap;
end
task automatic read_@S@;
 input [31:0] addr; input [7:0] len; input [2:0] size; input [1:0] burst; input lock;
 input [@IW@-1:0] id; input [3:0] cache;
 integer k;
 begin
  @(negedge clk_main);
  nl_i_@S@_araddr = addr; nl_i_@S@_arlen = len; nl_i_@S@_arsize = size; nl_i_@S@_arburst = burst;
  nl_i_@S@_arlock = lock; nl_i_@S@_arid = id; nl_i_@S@_aruser = id; nl_i_@S@_arcache = cache; nl_i_@S@_arprot = 0;
  nl_i_@S@_arvalid = 1;
  @(posedge clk_main); while (!nl_i_@S@_arready) @(posedge clk_main);
  @(negedge clk_main); nl_i_@S@_arvalid = 0; nl_i_@S@_arlock = 0;
  k = 0;
  while (k <= len) begin
   @(posedge clk_main);
   if (nl_i_@S@_rvalid && nl_i_@S@_rready) begin
    @S@_rdata[k] = nl_i_@S@_rdata; @S@_rresp[k] = nl_i_@S@_rresp;
    check(nl_i_@S@_rid === id, "@U@_RID");
    check(nl_i_@S@_rlast === (k == len), "@U@_RLAST");
    k = k + 1;
   end
  end
 end
endtask
task automatic write_@S@;
 input [31:0] addr; input [7:0] len; input [2:0] size; input [1:0] burst; input lock;
 input [@IW@-1:0] id; input [3:0] cache;
 integer k;
 begin
  @(negedge clk_main);
  nl_i_@S@_awaddr = addr; nl_i_@S@_awlen = len; nl_i_@S@_awsize = size; nl_i_@S@_awburst = burst;
  nl_i_@S@_awlock = lock; nl_i_@S@_awid = id; nl_i_@S@_awuser = id; nl_i_@S@_awcache = cache; nl_i_@S@_awprot = 0;
  nl_i_@S@_awvalid = 1;
  @(posedge clk_main); while (!nl_i_@S@_awready) @(posedge clk_main);
  for (k = 0; k <= len; k = k + 1) begin
   @(negedge clk_main);
   nl_i_@S@_awvalid = 0; nl_i_@S@_awlock = 0;
   nl_i_@S@_wvalid = 1; nl_i_@S@_wdata = @S@_wq_data[k]; nl_i_@S@_wstrb = @S@_wq_strb[k];
   nl_i_@S@_wlast = k == len;
   @(posedge clk_main); while (!nl_i_@S@_wready) @(posedge clk_main);
  end
  @(negedge clk_main); nl_i_@S@_wvalid = 0; nl_i_@S@_wlast = 0;
  @(posedge clk_main); while (!(nl_i_@S@_bvalid && nl_i_@S@_bready)) @(posedge clk_main);
  @S@_bresp = nl_i_@S@_bresp;
  check(nl_i_@S@_bid === id, "@U@_BID");
 end
endtask
// Atomic increments of one 32-bit counter: exclusive read, add one,
// exclusive write; an OKAY write retries.
task automatic race_@S@;
 input [31:0] addr; input [@IW@-1:0] id; input integer goal; input integer lane;
 reg [31:0] value;
 begin
  @S@_race_done = 0; @S@_race_tries = 0;
  while (@S@_race_done < goal) begin
   read_@S@(addr, 0, 2, 1, 1, id, 4'h0);
   check(@S@_rresp[0] === 2'd1, "@U@_RACE_READ_EXOKAY");
   value = @S@_rdata[0] >> (32 * lane);
   repeat (@S@_lfsr[3:0]) @(posedge clk_main);
   @S@_wq_data[0] = {(@DW@ / 32){value + 32'd1}}; @S@_wq_strb[0] = {@SB@{1'b0}} | (4'hf << (4 * lane));
   write_@S@(addr, 0, 2, 1, 1, id, 4'h0);
   if (@S@_bresp === 2'd1) begin
    check(race_seen[value + 1] === 1'b0, "@U@_RACE_DUPLICATE_SUCCESS");
    race_seen[value + 1] = 1'b1;
    @S@_race_done = @S@_race_done + 1;
   end else check(@S@_bresp === 2'd0, "@U@_RACE_WRITE_RESP");
   @S@_race_tries = @S@_race_tries + 1;
   check(@S@_race_tries < 400, "@U@_RACE_PROGRESS");
  end
 end
endtask
'''

# AXI4 slave with a byte memory. MON: the target's own exclusive monitor,
# keyed by the full AxID (target mode); otherwise AxLOCK must stay 0.
_AXI = '''
reg [7:0] @T@_mem [0:4095];
integer @T@_init;
initial for (@T@_init = 0; @T@_init < 4096; @T@_init = @T@_init + 1) @T@_mem[@T@_init] = 0;
reg @T@_awb = 0, @T@_bp = 0, @T@_rb = 0, @T@_pass = 0, @T@_awlk = 0, @T@_arlk = 0;
// @T@_whold holds WREADY low: an accepted AW waits for its data.
reg @T@_whold = 0;
reg [11:0] @T@_wa, @T@_ra, @T@_awa0, @T@_ara0; reg [7:0] @T@_awl, @T@_arl, @T@_wbeat, @T@_rbeat;
reg [2:0] @T@_aws, @T@_ars; reg [1:0] @T@_awbu, @T@_arbu, @T@_bresp_r;
reg [@IW@-1:0] @T@_awid, @T@_arid;
integer @T@_wcount = 0, @T@_zcount = 0, @T@_rcount = 0, @T@_awcount = 0, @T@_blat = 0, @T@_rlat = 0;
integer @T@_bwait = 0, @T@_rwait = 0, @T@_expect_src = -1, @T@_i;
reg @T@_res_v [0:@NID@-1]; reg [11:0] @T@_res_a [0:@NID@-1]; reg [12:0] @T@_res_n [0:@NID@-1];
reg @T@_ok, @T@_dirty;
assign nl_t_@T@_awready = !@T@_awb && !@T@_bp;
assign nl_t_@T@_wready = @T@_awb && !@T@_bp && !@T@_whold;
assign nl_t_@T@_bvalid = @T@_bp && @T@_bwait == 0;
assign nl_t_@T@_bresp = @T@_bresp_r;
assign nl_t_@T@_arready = !@T@_rb;
assign nl_t_@T@_rvalid = @T@_rb && @T@_rwait == 0;
// @T@_rerr names a local read address answered SLVERR.
reg [31:0] @T@_rerr = 32'hffffffff;
assign nl_t_@T@_rresp = {20'd0, @T@_ra} == @T@_rerr ? 2'd2 : (@MON@ && @T@_arlk) ? 2'd1 : 2'd0;
assign nl_t_@T@_rlast = @T@_rbeat == @T@_arl;
assign nl_t_@T@_rdata = @RDATA@;
@IDS@
function [11:0] @T@_next;
 input [11:0] a; input [11:0] a0; input [7:0] len; input [2:0] size; input [1:0] burst;
 reg [12:0] span;
 begin
  span = ({5'd0, len} + 13'd1) << size;
  if (burst == 2'd0) @T@_next = a;
  else if (burst == 2'd2) @T@_next = (a0 & ~(span[11:0] - 12'd1)) | ((a + (12'd1 << size)) & (span[11:0] - 12'd1));
  else @T@_next = (a & ~((12'd1 << size) - 12'd1)) + (12'd1 << size);
 end
endfunction
// The bytes [lo, hi) a burst covers: the WRAP container, the FIXED beat or
// the INCR span (one bit wider than the 4 KiB window).
function [12:0] @T@_lo;
 input [11:0] a; input [7:0] len; input [2:0] size; input [1:0] burst;
 @T@_lo = burst == 2'd2 ? {1'b0, a} & ~((({5'd0, len} + 13'd1) << size) - 13'd1) : {1'b0, a};
endfunction
function [12:0] @T@_hi;
 input [11:0] a; input [7:0] len; input [2:0] size; input [1:0] burst;
 @T@_hi = @T@_lo(a, len, size, burst) +
          (burst == 2'd0 ? 13'd1 << size : ({5'd0, len} + 13'd1) << size);
endfunction
always @(posedge @TCLK@) begin
 if (!reset_n) begin
  @T@_awb <= 0; @T@_bp <= 0; @T@_rb <= 0;
  for (@T@_i = 0; @T@_i < @NID@; @T@_i = @T@_i + 1) @T@_res_v[@T@_i] <= 0;
 end else begin
  if (nl_t_@T@_awvalid && nl_t_@T@_awready) begin
   @T@_awb <= 1; @T@_wa <= nl_t_@T@_awaddr[11:0]; @T@_awa0 <= nl_t_@T@_awaddr[11:0];
   @T@_awl <= nl_t_@T@_awlen; @T@_aws <= nl_t_@T@_awsize; @T@_awbu <= nl_t_@T@_awburst;
   @T@_awid <= nl_t_@T@_awid; @T@_awlk <= nl_t_@T@_awlock; @T@_wbeat <= 0;
   @T@_awcount <= @T@_awcount + 1;
   if (!@MON@) check(nl_t_@T@_awlock === 1'b0, "@U@_AWLOCK");
   @SRC_AW@
   @T@_ok = !nl_t_@T@_awlock ||
            (@T@_res_v[nl_t_@T@_awid] && @T@_res_a[nl_t_@T@_awid] == nl_t_@T@_awaddr[11:0] &&
             @T@_res_n[nl_t_@T@_awid] == (({5'd0, nl_t_@T@_awlen} + 13'd1) << nl_t_@T@_awsize));
   @T@_pass <= @T@_ok;
   // A write that updates memory clears every reservation it overlaps.
   if (@T@_ok)
    for (@T@_i = 0; @T@_i < @NID@; @T@_i = @T@_i + 1)
     if (@T@_res_v[@T@_i] &&
         @T@_res_a[@T@_i] < @T@_hi(nl_t_@T@_awaddr[11:0], nl_t_@T@_awlen, nl_t_@T@_awsize, nl_t_@T@_awburst) &&
         @T@_lo(nl_t_@T@_awaddr[11:0], nl_t_@T@_awlen, nl_t_@T@_awsize, nl_t_@T@_awburst) < @T@_res_a[@T@_i] + @T@_res_n[@T@_i])
      @T@_res_v[@T@_i] <= 0;
  end
  if (nl_t_@T@_wvalid && nl_t_@T@_wready) begin
   if (@T@_pass)
    for (@T@_i = 0; @T@_i < @BYTES@; @T@_i = @T@_i + 1)
     if (nl_t_@T@_wstrb[@T@_i]) @T@_mem[{@T@_wa[11:@LANE@], @LANEZ@} + @T@_i] <= nl_t_@T@_wdata[8 * @T@_i +: 8];
   if (@T@_pass && nl_t_@T@_wstrb != 0) @T@_wcount <= @T@_wcount + 1;
   if (@T@_pass && nl_t_@T@_wstrb == 0) @T@_zcount <= @T@_zcount + 1;
   @T@_wa <= @T@_next(@T@_wa, @T@_awa0, @T@_awl, @T@_aws, @T@_awbu);
   @T@_wbeat <= @T@_wbeat + 1;
   if (nl_t_@T@_wlast) begin
    check(@T@_wbeat == @T@_awl, "@U@_WLAST");
    @T@_awb <= 0; @T@_bp <= 1; @T@_bwait <= @T@_blat;
    @T@_bresp_r <= (@MON@ && @T@_awlk) ? (@T@_pass ? 2'd1 : 2'd0) : 2'd0;
   end
  end
  if (@T@_bp && @T@_bwait > 0) @T@_bwait <= @T@_bwait - 1;
  if (nl_t_@T@_bvalid && nl_t_@T@_bready) @T@_bp <= 0;
  if (nl_t_@T@_arvalid && nl_t_@T@_arready) begin
   @T@_rb <= 1; @T@_ra <= nl_t_@T@_araddr[11:0]; @T@_ara0 <= nl_t_@T@_araddr[11:0];
   @T@_arl <= nl_t_@T@_arlen; @T@_ars <= nl_t_@T@_arsize; @T@_arbu <= nl_t_@T@_arburst;
   @T@_arid <= nl_t_@T@_arid; @T@_arlk <= nl_t_@T@_arlock; @T@_rbeat <= 0;
   @T@_rwait <= @T@_rlat; @T@_rcount <= @T@_rcount + 1;
   if (!@MON@) check(nl_t_@T@_arlock === 1'b0, "@U@_ARLOCK");
   @SRC_AR@
   // One reservation per AxID: an exclusive read replaces it. Memory takes
   // a write's data at its W beats, after its AW cleared the reservations:
   // a read accepted from that AW until the last W beat may return the old
   // bytes, so it records nothing and its exclusive write fails.
   @T@_dirty = nl_t_@T@_awvalid && nl_t_@T@_awready
            ? @T@_ok && @T@_lo(nl_t_@T@_awaddr[11:0], nl_t_@T@_awlen, nl_t_@T@_awsize, nl_t_@T@_awburst) < @T@_hi(nl_t_@T@_araddr[11:0], nl_t_@T@_arlen, nl_t_@T@_arsize, nl_t_@T@_arburst) &&
              @T@_lo(nl_t_@T@_araddr[11:0], nl_t_@T@_arlen, nl_t_@T@_arsize, nl_t_@T@_arburst) < @T@_hi(nl_t_@T@_awaddr[11:0], nl_t_@T@_awlen, nl_t_@T@_awsize, nl_t_@T@_awburst)
            : @T@_awb && @T@_pass && @T@_lo(@T@_awa0, @T@_awl, @T@_aws, @T@_awbu) < @T@_hi(nl_t_@T@_araddr[11:0], nl_t_@T@_arlen, nl_t_@T@_arsize, nl_t_@T@_arburst) &&
              @T@_lo(nl_t_@T@_araddr[11:0], nl_t_@T@_arlen, nl_t_@T@_arsize, nl_t_@T@_arburst) < @T@_hi(@T@_awa0, @T@_awl, @T@_aws, @T@_awbu);
   if (@MON@ && nl_t_@T@_arlock) begin
    @T@_res_v[nl_t_@T@_arid] <= !@T@_dirty; @T@_res_a[nl_t_@T@_arid] <= nl_t_@T@_araddr[11:0];
    @T@_res_n[nl_t_@T@_arid] <= ({5'd0, nl_t_@T@_arlen} + 13'd1) << nl_t_@T@_arsize;
   end
  end
  if (@T@_rb && @T@_rwait > 0) @T@_rwait <= @T@_rwait - 1;
  if (nl_t_@T@_rvalid && nl_t_@T@_rready) begin
   @T@_ra <= @T@_next(@T@_ra, @T@_ara0, @T@_arl, @T@_ars, @T@_arbu);
   @T@_rbeat <= @T@_rbeat + 1;
   if (@T@_rbeat == @T@_arl) @T@_rb <= 0; else @T@_rwait <= @T@_rlat;
  end
 end
end
'''

# APB4 or APB3 slave with a byte memory, PREADY wait states, a PSLVERR
# address and a hang knob for the target timeout.
_APB = '''
reg [7:0] @T@_mem [0:4095];
integer @T@_init;
initial for (@T@_init = 0; @T@_init < 4096; @T@_init = @T@_init + 1) @T@_mem[@T@_init] = 0;
integer @T@_wcount = 0, @T@_rcount = 0, @T@_delay = 0, @T@_count = 0, @T@_hang = 0, @T@_i;
reg [31:0] @T@_err = 32'hffffffff;
wire [11:0] @T@_word = {nl_t_@T@_paddr[11:2], 2'b00};
wire [3:0] @T@_strobe = @STROBE@;
assign nl_t_@T@_pready = nl_t_@T@_psel && nl_t_@T@_penable && !@T@_hang && @T@_count >= @T@_delay;
assign nl_t_@T@_pslverr = nl_t_@T@_pready && nl_t_@T@_paddr[31:0] == @T@_err;
assign nl_t_@T@_prdata = {@T@_mem[@T@_word + 3], @T@_mem[@T@_word + 2], @T@_mem[@T@_word + 1], @T@_mem[@T@_word]};
always @(posedge @TCLK@) begin
 if (nl_t_@T@_psel && nl_t_@T@_penable && !nl_t_@T@_pready) @T@_count <= @T@_count + 1;
 else @T@_count <= 0;
 if (reset_n && nl_t_@T@_pready) begin
  if (nl_t_@T@_pwrite) begin
   // A write is committed (counted) only without PSLVERR.
   if (!nl_t_@T@_pslverr) begin
    for (@T@_i = 0; @T@_i < 4; @T@_i = @T@_i + 1)
     if (@T@_strobe[@T@_i]) @T@_mem[@T@_word + @T@_i] <= nl_t_@T@_pwdata[8 * @T@_i +: 8];
    @T@_wcount <= @T@_wcount + 1;
   end
  end else @T@_rcount <= @T@_rcount + 1;
 end
end
'''

# AHB-Lite slave with a byte memory: data phase after the address phase,
# @T@_wait wait states on each data phase.
_AHB = '''
reg [7:0] @T@_mem [0:4095];
integer @T@_init;
initial for (@T@_init = 0; @T@_init < 4096; @T@_init = @T@_init + 1) @T@_mem[@T@_init] = 0;
reg @T@_dp = 0, @T@_dw = 0; reg [11:0] @T@_da; reg [2:0] @T@_ds;
integer @T@_wcount = 0, @T@_rcount = 0, @T@_wait = 0, @T@_wc = 0, @T@_i;
wire [11:0] @T@_word = {@T@_da[11:2], 2'b00};
assign nl_t_@T@_hreadyout = !@T@_dp || @T@_wc == 0;
assign nl_t_@T@_hresp = 0;
assign nl_t_@T@_hrdata = {@T@_mem[@T@_word + 3], @T@_mem[@T@_word + 2], @T@_mem[@T@_word + 1], @T@_mem[@T@_word]};
always @(posedge @TCLK@) begin
 if (!reset_n) @T@_dp <= 0;
 else begin
  if (@T@_dp && @T@_wc > 0) @T@_wc <= @T@_wc - 1;
  else if (@T@_dp) begin
   if (@T@_dw) begin
    for (@T@_i = 0; @T@_i < (1 << @T@_ds); @T@_i = @T@_i + 1)
     @T@_mem[@T@_da + @T@_i] <= nl_t_@T@_hwdata[8 * ((@T@_da[1:0] + @T@_i) % 4) +: 8];
    @T@_wcount <= @T@_wcount + 1;
   end else @T@_rcount <= @T@_rcount + 1;
   @T@_dp <= 0;
  end
  if (nl_t_@T@_hsel && nl_t_@T@_htrans[1] && nl_t_@T@_hready) begin
   @T@_dp <= 1; @T@_dw <= nl_t_@T@_hwrite; @T@_da <= nl_t_@T@_haddr[11:0];
   @T@_ds <= nl_t_@T@_hsize; @T@_wc <= @T@_wait;
  end
 end
end
'''

# AXI4-Lite slave with a byte memory: AW and W independently, one at a time.
_LITE = '''
reg [7:0] @T@_mem [0:4095];
integer @T@_init;
initial for (@T@_init = 0; @T@_init < 4096; @T@_init = @T@_init + 1) @T@_mem[@T@_init] = 0;
reg @T@_aw = 0, @T@_w = 0, @T@_b = 0, @T@_r = 0; reg [11:0] @T@_a, @T@_ra; reg [31:0] @T@_d;
reg [3:0] @T@_s; integer @T@_wcount = 0, @T@_zcount = 0, @T@_rcount = 0, @T@_tick = 0, @T@_i;
integer @T@_blat = 0, @T@_bwait = 0;
assign nl_t_@T@_awready = !@T@_aw && !@T@_b && @T@_tick % 3 != 1;
assign nl_t_@T@_wready = !@T@_w && !@T@_b && @T@_tick % 4 != 2;
assign nl_t_@T@_bvalid = @T@_b && @T@_bwait == 0; assign nl_t_@T@_bresp = 2'b00;
assign nl_t_@T@_arready = !@T@_r && @T@_tick % 3 != 2;
assign nl_t_@T@_rvalid = @T@_r; assign nl_t_@T@_rresp = 2'b00;
assign nl_t_@T@_rdata = {@T@_mem[{@T@_ra[11:2], 2'd3}], @T@_mem[{@T@_ra[11:2], 2'd2}],
                         @T@_mem[{@T@_ra[11:2], 2'd1}], @T@_mem[{@T@_ra[11:2], 2'd0}]};
always @(posedge @TCLK@) begin
 @T@_tick <= @T@_tick + 1;
 if (!reset_n) begin @T@_aw <= 0; @T@_w <= 0; @T@_b <= 0; @T@_r <= 0; end
 else begin
  if (nl_t_@T@_awvalid && nl_t_@T@_awready) begin @T@_aw <= 1; @T@_a <= nl_t_@T@_awaddr[11:0]; end
  if (nl_t_@T@_wvalid && nl_t_@T@_wready) begin @T@_w <= 1; @T@_d <= nl_t_@T@_wdata; @T@_s <= nl_t_@T@_wstrb; end
  if (@T@_aw && @T@_w && !@T@_b) begin
   for (@T@_i = 0; @T@_i < 4; @T@_i = @T@_i + 1)
    if (@T@_s[@T@_i]) @T@_mem[{@T@_a[11:2], 2'b00} + @T@_i] <= @T@_d[8 * @T@_i +: 8];
   if (@T@_s != 0) @T@_wcount <= @T@_wcount + 1; else @T@_zcount <= @T@_zcount + 1;
   @T@_b <= 1; @T@_bwait <= @T@_blat; @T@_aw <= 0; @T@_w <= 0;
  end
  if (@T@_b && @T@_bwait > 0) @T@_bwait <= @T@_bwait - 1;
  if (nl_t_@T@_bvalid && nl_t_@T@_bready) @T@_b <= 0;
  if (nl_t_@T@_arvalid && nl_t_@T@_arready) begin
   @T@_r <= 1; @T@_ra <= nl_t_@T@_araddr[11:0]; @T@_rcount <= @T@_rcount + 1;
  end
  if (@T@_r && nl_t_@T@_rready) @T@_r <= 0;
 end
end
'''


def beat_addresses(addr, length, size, burst):
    count = 1 << size
    beats = length + 1
    base = addr & ~(count - 1)
    if burst == 0:
        return [base] * beats
    if burst == 2:
        span = beats * count
        start = addr & ~(span - 1)
        return [start + ((base - start + k * count) % span) for k in range(beats)]
    return [base + k * count for k in range(beats)]


class Bench:
    def __init__(self, mode, mapping):
        self.mode = mode
        self.model = Model(mode)
        self.targets = targets(mode)
        self.lines = []
        self.seq = 0
        self.steps = 0
        self.ports = {p['name']: p['width'] for p in mapping['ports']}
        self.capacity = mapping['definition'].get('outstanding', {}).get('targets', {})

    def served_in_order(self, target):
        return target in SERVED_IN_ORDER and (self.targets[target][0] != 'axi4' or
                                              self.capacity.get(target, 1) == 1)

    def emit(self, text):
        self.lines.append(text)

    def base(self, target):
        return self.targets[target][1]

    def counts(self, target):
        return f'{target}_wcount', f'{target}_rcount'

    def prepare(self, source, target):
        """Target-mode models check the source index and AxLOCK of the next access."""
        si = SOURCES.index(source)
        for t, spec in self.targets.items():
            if spec[2] == 'target':
                self.emit(f'  {t}_expect_src = {si if t == target else -1};')

    def unexpect(self):
        """Concurrent traffic: target-mode models stop checking the source index."""
        for t, spec in self.targets.items():
            if spec[2] == 'target':
                self.emit(f'  {t}_expect_src = -1;')

    def zeroes(self, target):
        """AXI4 and AXI4-Lite target models also count writes without strobes."""
        return self.targets[target][0] in ('axi4', 'axi4_lite')

    def mark(self, target):
        w, r = self.counts(target)
        self.emit(f'  mark_w = {w}; mark_r = {r};')
        if self.zeroes(target):
            self.emit(f'  mark_z = {target}_zcount;')

    def label(self, text):
        self.steps += 1
        return f'S{self.steps}_{text}'.upper()

    def xread(self, source, target, offset, length=0, size=2, ident=1, lock=1, burst=1,
              cache=0, expect=None, check_data=True, tag=''):
        label = self.label(tag or 'read')
        resps = self.model.read(source, target, offset, length, size, ident, lock, burst)
        if expect is not None:
            resps = expect
        bus = source_width(self.mode, source) // 8
        self.prepare(source, target)
        self.mark(target)
        addr = self.base(target) + offset
        self.emit(f'  read_{source}(32\'h{addr:08X}, {length}, {size}, {burst}, {lock}, {ident}, '
                  f'4\'h{cache:X});')
        for k, (beat, resp) in enumerate(zip(beat_addresses(offset, length, size, burst), resps)):
            self.emit(f'  check({source}_rresp[{k}] === 2\'d{resp}, "{label}_RRESP{k}");')
            if check_data and resp in (OKAY, EXOKAY):
                lane = beat % bus
                mask = expect_value = 0
                for b in range(1 << size):
                    byte = self.model.memory[target].get(beat + b, 0)
                    mask |= 0xFF << (8 * (lane + b))
                    expect_value |= byte << (8 * (lane + b))
                bits = bus * 8
                self.emit(f'  check(({source}_rdata[{k}] & {bits}\'h{mask:0{bits // 4}X}) === '
                          f'{bits}\'h{expect_value:0{bits // 4}X}, "{label}_RDATA{k}");')
        if all(r == SLVERR for r in resps) and lock and not self.model.admitted(
                source, target, offset, length, size, burst):
            self.emit(f'  check({target}_rcount == mark_r && {target}_wcount == mark_w, '
                      f'"{label}_NO_ACCESS");')
        return resps

    def data(self, source, offset, length, size, burst):
        """Deterministic write bytes and the bus beats that carry them."""
        bus = source_width(self.mode, source) // 8
        values, beats = {}, []
        for k, beat in enumerate(beat_addresses(offset, length, size, burst)):
            self.seq += 1
            lane = beat % bus
            word = strobe = 0
            for b in range(1 << size):
                value = (self.seq * 37 + b * 11 + 1) & 0xFF
                values[beat + b] = value
                word |= value << (8 * (lane + b))
                strobe |= 1 << (lane + b)
            beats.append((word, strobe))
        return values, beats

    def xwrite(self, source, target, offset, length=0, size=2, ident=1, lock=1, burst=1,
               cache=0, expect=None, tag='', zero=False):
        label = self.label(tag or 'write')
        values, beats = self.data(source, offset, length, size, burst)
        if zero:
            # Every beat without strobes: no byte changes, but an AXI4-Lite
            # target still receives the write (WSTRB zero).
            values, beats = {}, [(word, 0) for word, _ in beats]
        bus = source_width(self.mode, source) // 8
        resp, written = self.model.write(source, target, offset, length, size, ident, lock, values,
                                         burst)
        if expect is not None:
            resp, written = expect
            if written:
                self.model.memory[target].update(values)
        for k, (word, strobe) in enumerate(beats):
            self.emit(f'  {source}_wq_data[{k}] = {bus * 8}\'h{word:0{bus * 2}X}; '
                      f'{source}_wq_strb[{k}] = {bus}\'h{strobe:0{(bus + 3) // 4}X};')
        self.prepare(source, target)
        self.mark(target)
        addr = self.base(target) + offset
        self.emit(f'  write_{source}(32\'h{addr:08X}, {length}, {size}, {burst}, {lock}, {ident}, '
                  f'4\'h{cache:X});')
        self.emit(f'  check({source}_bresp === 2\'d{resp}, "{label}_BRESP");')
        zeroes = self.zeroes(target)
        if written and zero:
            self.emit(f'  check({target}_zcount != mark_z, "{label}_WRITTEN_WITHOUT_STROBES");')
        elif written:
            self.emit(f'  check({target}_wcount != mark_w, "{label}_WRITTEN");')
        else:
            self.emit(f'  check({target}_wcount == mark_w, "{label}_NOT_WRITTEN");')
        # A failed exclusive write (OKAY) to a network monitor still goes out
        # with every strobe clear at an AXI4 or AXI4-Lite target.
        failed = (lock and not written and resp == OKAY and zeroes and
                  self.model.kind(target) == 'network' and target not in self.model.failed)
        if failed:
            self.emit(f'  check({target}_zcount != mark_z, "{label}_FAILED_WSTRB_ZERO");')
        elif zeroes and not (written and zero):
            self.emit(f'  check({target}_zcount == mark_z, "{label}_NO_ZERO_STROBE_WRITE");')
        return resp

    def read(self, source, target, offset, length=0, size=2, ident=0, **kw):
        return self.xread(source, target, offset, length, size, ident, lock=0, **kw)

    def write(self, source, target, offset, length=0, size=2, ident=0, **kw):
        return self.xwrite(source, target, offset, length, size, ident, lock=0, **kw)

    def reset(self):
        self.model.reset()
        self.emit('  pulse_reset;')

    def episode(self, name):
        self.emit(f'  $display("EPISODE {name}");')


def in_flight(b, t, write_offset, beats, read_offset, delay, ident=1):
    """s1 writes beats words at write_offset (normal) while s0, delay cycles
    later, issues an exclusive read of one word at read_offset.
    race_write_first records whether the target served a write beat before
    the read."""
    values, words = b.data('s1', write_offset, beats - 1, 2, 1)
    for k, (word, strobe) in enumerate(words):
        b.emit(f'  s1_wq_data[{k}] = 32\'h{word:08X}; s1_wq_strb[{k}] = 4\'h{strobe:X};')
    b.emit(f'  race_w = {t}_wcount; race_r = {t}_rcount;')
    b.emit('  fork')
    b.emit(f'   begin wait ({t}_wcount != race_w || {t}_rcount != race_r); '
           f'race_write_first = {t}_wcount != race_w; end')
    b.emit(f'   write_s1(32\'h{b.base(t) + write_offset:08X}, {beats - 1}, 2, 1, 0, 0, 4\'h0);')
    b.emit(f'   begin repeat ({delay}) @(posedge clk_main); '
           f'read_s0(32\'h{b.base(t) + read_offset:08X}, 0, 2, 1, 1, {ident}, 4\'h0); end')
    b.emit('  join')
    b.emit(f'  check(s0_rresp[0] === 2\'d1, "{t.upper()}_RACED_READ_EXOKAY");')
    b.emit(f'  check(s1_bresp === 2\'d0, "{t.upper()}_RACED_WRITE_OKAY");')
    b.model.memory[t].update(values)


def graph_episodes(b):
    """Single-step exclusive semantics on every target, then races."""
    network = ['tm', 'tp', 't3', 'th', 'tl']
    passed = ['ty', 'tx']
    for t in network + passed:
        b.episode('pair_' + t)
        b.xread('s0', t, 0x40, ident=1, tag=t + '_pair_read')
        b.xwrite('s0', t, 0x40, ident=1, tag=t + '_pair_write')
        b.read('s1', t, 0x40, tag=t + '_pair_readback')
        b.episode('competing_writer_' + t)
        b.xread('s0', t, 0x80, ident=1)
        b.write('s1', t, 0x80, ident=0)
        b.xwrite('s0', t, 0x80, ident=1, tag=t + '_after_foreign_write')
        b.read('s0', t, 0x80)
        b.episode('same_source_write_' + t)
        b.xread('s0', t, 0xC0, ident=1)
        b.write('s0', t, 0xC0, ident=2)
        b.xwrite('s0', t, 0xC0, ident=1, tag=t + '_after_own_write')
        b.episode('source_in_key_' + t)
        # Both sources use AxID 0: the second reservation of s1 must not
        # revive s0's (AXI A7.2.1).
        b.xread('s0', t, 0x100, ident=0)
        b.xread('s1', t, 0x100, ident=0)
        b.xwrite('s1', t, 0x100, ident=0, tag=t + '_second_wins')
        b.xread('s1', t, 0x100, ident=0)
        b.xwrite('s0', t, 0x100, ident=0, tag=t + '_stale_fails')
        b.xwrite('s1', t, 0x100, ident=0, tag=t + '_fresh_succeeds')
        b.episode('different_ids_' + t)
        b.xread('s0', t, 0x140, ident=1)
        b.xread('s0', t, 0x180, ident=2)
        b.xwrite('s0', t, 0x180, ident=2)
        b.xwrite('s0', t, 0x140, ident=1)
        b.episode('moved_reservation_' + t)
        b.xread('s0', t, 0x1C0, ident=3)
        b.xread('s0', t, 0x200, ident=3)
        b.xwrite('s0', t, 0x1C0, ident=3, tag=t + '_moved_away')
        b.xwrite('s0', t, 0x200, ident=3, tag=t + '_moved_here')
        b.episode('burst_' + t)
        b.xread('s1', t, 0x240, length=3, ident=3)
        b.xwrite('s1', t, 0x240, length=3, ident=3)
        b.xread('s1', t, 0x280, length=1, ident=2, burst=2, tag=t + '_wrap_read')
        b.xwrite('s1', t, 0x280, length=1, ident=2, burst=2, tag=t + '_wrap_write')
        b.episode('failed_burst_' + t)
        # A failed four-beat exclusive write changes no byte: every beat, not
        # only the first, goes out without strobes (or not at all).
        b.xread('s0', t, 0xE00, length=3, ident=2)
        b.write('s1', t, 0xE04)
        b.xwrite('s0', t, 0xE00, length=3, ident=2, tag=t + '_failed_burst')
        b.episode('restrictions_' + t)
        b.xread('s0', t, 0x2C0, length=2, ident=1, tag=t + '_three_beats')
        b.xwrite('s0', t, 0x2C0, length=2, ident=1, tag=t + '_three_beats')
        b.xread('s0', t, 0x2C8, length=3, ident=1, tag=t + '_unaligned')
        b.xwrite('s0', t, 0x2C8, length=3, ident=1, tag=t + '_unaligned')
        b.xwrite('s0', t, 0x2C0, length=0, ident=1, burst=0, tag=t + '_fixed')
        b.xread('s0', t, 0x2C0, length=0, ident=1, burst=0, tag=t + '_fixed')
        b.episode('long_pair_' + t)
        # 32 four-byte beats violate the exclusive beat limit, even at 128 bytes.
        b.xread('s0', t, 0x900, length=31, ident=1, tag=t + '_b128_read')
        b.xwrite('s0', t, 0x900, length=31, ident=1, tag=t + '_b128_write')
        b.read('s0', t, 0x900, length=31, tag=t + '_b128_readback')
    b.episode('long_limits')
    # Overlong exclusive bursts are refused whole, including narrow transfers.
    b.xread('s0', 'tm', 0xA00, length=63, ident=1, tag='tm_b256_read')
    b.xread('s0', 'tm', 0xA80, length=31, size=0, ident=1, tag='tm_narrow32_read')
    b.xwrite('s0', 'tm', 0xA80, length=31, size=0, ident=1, tag='tm_narrow32_write')
    b.xread('s0', 'tm', 0xB00, length=127, size=0, ident=1, tag='tm_narrow128_read')
    b.xwrite('s0', 'tm', 0xB00, length=127, size=0, ident=1, tag='tm_narrow128_write')
    b.xread('s0', 'tm', 0xB80, length=15, size=0, ident=1, tag='tm_narrow16_read')
    b.xwrite('s0', 'tm', 0xB80, length=15, size=0, ident=1, tag='tm_narrow16_write')
    b.episode('zero_strobe_tl')
    # An exclusive write without strobes is still a write: on success the
    # AXI4-Lite target receives it (WSTRB zero) and the source gets EXOKAY; the
    # repeat finds its reservation cleared and fails, again as a WSTRB-zero write.
    b.xread('s0', 'tl', 0x380, ident=1, tag='tl_zero_read')
    b.xwrite('s0', 'tl', 0x380, ident=1, zero=True, tag='tl_zero_exokay')
    b.emit('  check(tl_s === 4\'h0 && tl_a == 12\'h380, "TL_ZERO_WSTRB_AT_TARGET");')
    b.xwrite('s0', 'tl', 0x380, ident=1, zero=True, tag='tl_zero_failed')
    b.read('s1', 'tl', 0x380, tag='tl_zero_readback')
    b.episode('narrow_strobeless')
    b.xread('s0', 't3', 0x300, size=1, ident=1, tag='t3_narrow')
    b.xwrite('s0', 't3', 0x300, size=1, ident=1, tag='t3_narrow')
    b.xread('s0', 'tp', 0x302, size=1, ident=1, tag='tp_narrow_read')
    b.xwrite('s0', 'tp', 0x302, size=1, ident=1, tag='tp_narrow_write')
    b.episode('granule')
    # Monitors reserve 8-byte granules: a write to the neighbouring word
    # clears, a write to the next granule does not.
    b.xread('s0', 't3', 0x340, ident=1)
    b.write('s1', 't3', 0x344)
    b.xwrite('s0', 't3', 0x340, ident=1, tag='t3_granule_neighbour')
    b.xread('s0', 'tp', 0x340, ident=1)
    b.write('s1', 'tp', 0x348)
    b.xwrite('s0', 'tp', 0x340, ident=1, tag='tp_next_granule')
    b.episode('degrade')
    # No exclusive support: a normal access answered OKAY; the write updates
    # memory and the target never sees AxLOCK; no A7.2.4 check applies.
    b.xread('s0', 'tn', 0x40, ident=1, tag='tn_read_okay')
    b.xwrite('s0', 'tn', 0x40, ident=1, tag='tn_write_okay')
    b.read('s1', 'tn', 0x40, tag='tn_written')
    b.xread('s0', 'tn', 0x80, length=2, ident=1, tag='tn_three_beats_normal')
    b.xwrite('s0', 'tn', 0x80, length=2, ident=1, tag='tn_three_beats_normal')
    b.read('s0', 'tn', 0x80, length=2)
    # A 128-byte burst with AxLOCK is a normal 128-byte access here.
    b.xread('s0', 'tn', 0x900, length=31, ident=1, tag='tn_b128_normal')
    b.xwrite('s0', 'tn', 0x900, length=31, ident=1, tag='tn_b128_normal')
    b.read('s0', 'tn', 0x900, length=31, tag='tn_b128_readback')
    b.episode('eviction')
    # tm holds two reservations: a third key evicts one of the first two.
    for offset, ident, source in ((0x400, 1, 's0'), (0x440, 2, 's0'), (0x480, 1, 's1')):
        b.xread(source, 'tm', offset, ident=ident, check_data=False)
    b.model.reservations['tm'].clear()
    b.emit('  evicted_ok = 0;')
    b.xwrite('s1', 'tm', 0x480, ident=1, expect=(EXOKAY, True), tag='tm_newest_kept')
    for offset, ident in ((0x400, 1), (0x440, 2)):
        values, beats = b.data('s0', offset, 0, 2, 1)
        word, strobe = beats[0]
        b.emit(f'  s0_wq_data[0] = 32\'h{word:08X}; s0_wq_strb[0] = 4\'h{strobe:X};')
        b.emit(f'  write_s0(32\'h{0x10000 + offset:08X}, 0, 2, 1, 1, {ident}, 4\'h0);')
        b.emit('  check(s0_bresp === 2\'d1 || s0_bresp === 2\'d0, "EVICTION_RESP");')
        b.emit('  if (s0_bresp === 2\'d1) evicted_ok = evicted_ok + 1;')
    b.emit('  check(evicted_ok == 1, "EVICTION_ONE_OF_TWO_SURVIVES");')
    for offset in (0x400, 0x440):
        b.write('s0', 'tm', offset)  # resynchronize the model's memory
    b.episode('write_in_flight')
    # A write ahead of an exclusive read at a served-in-order outlet (slow
    # target): the read waits until the write's B has left, then records its
    # reservation, so the exclusive write succeeds.
    for t, knob, clear in (('tp', 'tp_delay = 10;', 'tp_delay = 0;'),
                           ('tm', 'tm_blat = 40;', 'tm_blat = 0;'),
                           ('tl', 'tl_blat = 40;', 'tl_blat = 0;')):
        b.emit(f'  {knob}')
        values, beats = b.data('s1', 0x500, 3, 2, 1)
        for k, (word, strobe) in enumerate(beats):
            b.emit(f'  s1_wq_data[{k}] = 32\'h{word:08X}; s1_wq_strb[{k}] = 4\'h{strobe:X};')
        b.emit(f'  race_w = {t}_wcount; race_r = {t}_rcount;')
        b.emit('  fork')
        b.emit(f'   begin wait ({t}_wcount != race_w || {t}_rcount != race_r); '
               f'race_write_first = {t}_wcount != race_w; end')
        b.emit(f'   write_s1(32\'h{b.base(t) + 0x500:08X}, 3, 2, 1, 0, 0, 4\'h0);')
        b.emit(f'   begin repeat (6) @(posedge clk_main); '
               f'read_s0(32\'h{b.base(t) + 0x50C:08X}, 0, 2, 1, 1, 1, 4\'h0); end')
        b.emit('  join')
        b.emit(f'  check(s0_rresp[0] === 2\'d1, "{t.upper()}_IN_FLIGHT_READ_EXOKAY");')
        b.emit(f'  check(s1_bresp === 2\'d0, "{t.upper()}_IN_FLIGHT_WRITE_OKAY");')
        b.model.memory[t].update(values)
        b.emit(f'  {clear}')
        if b.served_in_order(t):
            # The outlet serves the read only after the earlier write has
            # completed and its B has left, so no write is in flight when the
            # read reaches the outlet: it records its reservation and the
            # exclusive write succeeds.
            b.emit(f'  check(race_write_first == 1, "{t.upper()}_READ_AFTER_WRITE");')
            b.model.read('s0', t, 0x50C, 0, 2, 1, 1)
            b.xwrite('s0', t, 0x50C, ident=1, tag=t + '_after_write_succeeds')
        else:
            b.xwrite('s0', t, 0x50C, ident=1, expect=(OKAY, False), tag=t + '_in_flight_fails')
    b.episode('target_error')
    # The monitor records at the exclusive read's request, whatever the read
    # answers: after an error read the exclusive write passes the monitor and
    # reaches the target, which here fails it as well.
    b.emit('  tp_err = 32\'h600;')
    b.xread('s0', 'tp', 0x600, ident=1, expect=[SLVERR], tag='tp_error_read')
    b.xwrite('s0', 'tp', 0x600, ident=1, expect=(SLVERR, False), tag='tp_error_read_kept')
    b.emit('  tp_err = 32\'hffffffff;')
    b.emit('  tm_rerr = 32\'h640;')
    b.xread('s0', 'tm', 0x640, ident=1, expect=[SLVERR], tag='tm_error_read')
    b.emit('  tm_rerr = 32\'hffffffff;')
    b.xwrite('s0', 'tm', 0x640, ident=1, tag='tm_error_read_kept')
    b.episode('same_cycle_heads')
    # A write head and an overlapping exclusive read head may meet in one
    # cycle; served in arrival order, the outcome follows the order. The slow
    # target keeps the write in flight past every read head, so the read
    # always lands after it here.
    for t, knob, clear in (('tp', 'tp_delay = 16;', 'tp_delay = 0;'),
                           ('tm', 'tm_blat = 30;', 'tm_blat = 0;'),
                           ('tl', 'tl_blat = 30;', 'tl_blat = 0;')):
        b.emit(f'  {knob}')
        for delay in range(9):
            offset = 0xA00 + 0x10 * delay
            in_flight(b, t, offset, 1, offset, delay)
            if b.served_in_order(t):
                # Served in arrival order the outcome follows the order: a
                # read after the completed write records its reservation (the
                # exclusive write succeeds), a read before it is cleared by
                # the write (it fails). A normal write then resynchronizes
                # the model's memory.
                b.emit('  if (race_write_first) begin')
                b.xwrite('s0', t, offset, ident=1, expect=(EXOKAY, True),
                         tag=f'{t}_same_cycle_{delay}_after')
                b.emit('  end else begin')
                b.xwrite('s0', t, offset, ident=1, expect=(OKAY, False),
                         tag=f'{t}_same_cycle_{delay}_before')
                b.emit('  end')
                b.write('s0', t, offset)
            else:
                b.xwrite('s0', t, offset, ident=1, expect=(OKAY, False),
                         tag=f'{t}_same_cycle_{delay}')
        b.emit(f'  {clear}')
    b.episode('old_reservation_behind_write')
    # Served in arrival order, the read behind the in-flight write records
    # its region and replaces its key's older reservation (AXI A7.2.1). An
    # outlet of several bursts lets the read meet the write: it leaves the
    # monitor unchanged, so the key keeps its older reservation.
    for t, knob, clear in (('tp', 'tp_delay = 10;', 'tp_delay = 0;'),
                           ('tm', 'tm_blat = 40;', 'tm_blat = 0;'),
                           ('tl', 'tl_blat = 40;', 'tl_blat = 0;')):
        b.xread('s0', t, 0xB00, ident=1)
        b.emit(f'  {knob}')
        in_flight(b, t, 0xB40, 4, 0xB4C, 6)
        b.emit(f'  {clear}')
        if b.served_in_order(t):
            b.model.reservations[t].pop(('s0', 1), None)
            b.xwrite('s0', t, 0xB00, ident=1, expect=(OKAY, False), tag=t + '_old_replaced')
        else:
            b.xwrite('s0', t, 0xB00, ident=1, tag=t + '_old_kept')
    b.episode('record_beats_clear')
    # A key's new reservation recorded in the cycle a write clears the slot's
    # old region keeps the new one.
    for t, knob, clear in (('tp', 'tp_delay = 4;', 'tp_delay = 0;'),
                           ('tm', 'tm_blat = 20;', 'tm_blat = 0;')):
        b.emit(f'  {knob}')
        for delay in range(12):
            old, new = 0xC00 + 0x10 * delay, 0xD00 + 0x10 * delay
            b.xread('s0', t, old, ident=1, check_data=False)
            in_flight(b, t, old, 1, new, delay)
            b.model.reservations[t][('s0', 1)] = b.model.region(t, new, 4)
            b.xwrite('s0', t, new, ident=1, tag=f'{t}_kept_{delay}')
        b.emit(f'  {clear}')
    b.episode('reset')
    for t in ('tm', 'th', 'ty'):
        b.xread('s0', t, 0x700, ident=1)
    b.reset()
    for t in ('tm', 'th', 'ty'):
        b.xwrite('s0', t, 0x700, ident=1, tag=t + '_after_reset')
    b.episode('timeout')
    b.emit('  tp_hang = 1;')
    b.model.failed.add('tp')
    b.read('s0', 'tp', 0x780, tag='tp_expiry')
    b.emit('  check(nl_t_tp_timed_out === 1\'b1, "TP_TIMED_OUT");')
    b.xread('s0', 'tp', 0x784, ident=1, tag='tp_failed_read')
    b.xwrite('s0', 'tp', 0x784, ident=1, tag='tp_failed_write')
    b.emit('  tp_hang = 0;')
    b.reset()
    b.xread('s0', 'tp', 0x788, ident=1, tag='tp_recovered')
    b.xwrite('s0', 'tp', 0x788, ident=1, tag='tp_recovered')
    b.episode('race')
    b.unexpect()
    goal = 6
    for t in network + passed:
        b.emit('  for (race_i = 0; race_i < 64; race_i = race_i + 1) race_seen[race_i] = 0;')
        b.emit('  s0_gap = 2; s1_gap = 3;')
        addr = b.base(t) + 0x800
        b.emit('  fork')
        b.emit(f'   race_s0(32\'h{addr:08X}, 1, {goal}, 0);')
        b.emit(f'   race_s1(32\'h{addr:08X}, 1, {goal}, 0);')
        b.emit('  join')
        b.emit('  s0_gap = 0; s1_gap = 0;')
        for k, value in enumerate((2 * goal).to_bytes(4, 'little')):
            b.model.memory[t][0x800 + k] = value
        b.read('s0', t, 0x800, tag=t + '_race_count')


def wide_episodes(b):
    """Width conversion and bounded packets with exclusive bursts."""
    b.episode('clamped_target_mode')
    # 64 bytes from the 64-bit source: 16 beats at the 32-bit target.
    b.xread('s0', 'tx', 0x40, length=7, size=3, ident=1, tag='tx_64_bytes')
    b.xwrite('s0', 'tx', 0x40, length=7, size=3, ident=1, tag='tx_64_bytes')
    # 128 bytes would become 32 AxLOCK beats at the target and are refused.
    b.xread('s0', 'tx', 0x80, length=15, size=3, ident=1, tag='tx_128_bytes')
    b.xwrite('s0', 'tx', 0x80, length=15, size=3, ident=1, tag='tx_128_bytes')
    # The same source shape fits 16 beats at a 64-bit target and succeeds.
    b.xread('s0', 'tw', 0x80, length=15, size=3, ident=1, tag='tw_128_bytes')
    b.xwrite('s0', 'tw', 0x80, length=15, size=3, ident=1, tag='tw_128_bytes')
    b.xread('s1', 'tx', 0x100, length=15, ident=1, tag='tx_16_words')
    b.xwrite('s1', 'tx', 0x100, length=15, ident=1, tag='tx_16_words')
    b.episode('unsplit_packet')
    # Bounded packets stop AHB packets at 16 words: an exclusive 128-byte burst
    # is past that limit and refused whole at the source (an exclusive burst is
    # never cut); a 64-byte one is admitted, and a foreign write inside its
    # region clears the reservation.
    b.xread('s0', 'th', 0x100, length=15, size=3, ident=2, tag='th_128_bytes')
    b.xwrite('s0', 'th', 0x100, length=15, size=3, ident=2, tag='th_128_bytes')
    b.read('s1', 'th', 0x100, length=15)
    b.xread('s0', 'th', 0x200, length=7, size=3, ident=2)
    b.write('s1', 'th', 0x23C)
    b.xwrite('s0', 'th', 0x200, length=7, size=3, ident=2, tag='th_tail_cleared')
    b.episode('gathered_beat')
    # A 64-bit exclusive beat at a 32-bit APB target gathers two words; it is
    # EXOKAY only if both are.
    b.xread('s0', 'tp', 0x200, length=0, size=3, ident=1, tag='tp_gathered')
    b.xwrite('s0', 'tp', 0x200, length=0, size=3, ident=1, tag='tp_gathered')
    b.emit('  tp_err = 32\'h30C;')
    b.xread('s0', 'tp', 0x308, length=0, size=3, ident=1, expect=[SLVERR], check_data=False,
            tag='tp_half_failed')
    b.model.reservations['tp'].clear()
    b.emit('  tp_err = 32\'hffffffff;')
    b.episode('length_limit')
    # 128 bytes at the 32-bit APB target are 32 target words of one packet.
    b.xread('s0', 'tp', 0x400, length=15, size=3, ident=2, tag='tp_128_bytes')
    b.xwrite('s0', 'tp', 0x400, length=15, size=3, ident=2, tag='tp_128_bytes')
    b.episode('wrap_cut')
    # A 64-bit WRAP16 is 32 words at the 32-bit APB target. A normal one is
    # cut into INCR pieces in wrap order. An exclusive one goes out as INCR16
    # from its start (its container base): one piece of 32 target words that
    # succeeds like any 128-byte exclusive burst; so does an exclusive WRAP8.
    b.read('s0', 'tp', 0x520, length=15, size=3, burst=2, tag='tp_wrap32_normal')
    b.write('s0', 'tp', 0x520, length=15, size=3, burst=2, tag='tp_wrap32_normal')
    b.read('s1', 'tp', 0x500, length=31, tag='tp_wrap32_readback')
    b.xread('s0', 'tp', 0x600, length=15, size=3, ident=1, burst=2, tag='tp_wrap32_exclusive')
    b.xwrite('s0', 'tp', 0x600, length=15, size=3, ident=1, burst=2, tag='tp_wrap32_exclusive')
    b.xread('s0', 'tp', 0x700, length=7, size=3, ident=1, burst=2, tag='tp_wrap16_exclusive')
    b.xwrite('s0', 'tp', 0x700, length=7, size=3, ident=1, burst=2, tag='tp_wrap16_exclusive')
    b.episode('merged_words')
    # The 32-bit source's modifiable beats merge into one 64-bit word at tw;
    # the word's exclusive_okay answers both beats.
    b.xread('s1', 'tw', 0x40, length=1, ident=1, cache=2, tag='tw_merged')
    b.xwrite('s1', 'tw', 0x40, length=1, ident=1, cache=2, tag='tw_merged')
    b.xread('s0', 'tw', 0x80, length=1, size=3, ident=1, tag='tw_wide')
    b.write('s1', 'tw', 0x88, ident=0)
    b.xwrite('s0', 'tw', 0x80, length=1, size=3, ident=1, tag='tw_wide_cleared')
    b.episode('race')
    b.unexpect()
    for t in ('tw', 'tx', 'th', 'tp'):
        b.emit('  for (race_i = 0; race_i < 64; race_i = race_i + 1) race_seen[race_i] = 0;')
        addr = b.base(t) + 0x800
        b.emit('  fork')
        b.emit(f'   race_s0(32\'h{addr:08X}, 1, 6, 0);')
        b.emit(f'   race_s1(32\'h{addr:08X}, 1, 6, 0);')
        b.emit('  join')
        for k, value in enumerate((12).to_bytes(4, 'little')):
            b.model.memory[t][0x800 + k] = value
        b.read('s1', t, 0x800, tag=t + '_race_count')


def split_episodes(b):
    """narrow_bursts split_non_modifiable: an exclusive narrow INCR of the
    64-bit source reaches each target as one burst (merged into 64-bit words
    at the AXI4 targets), whatever the target's exclusive support; the same
    burst without AxLOCK goes out one packet per beat."""
    for t in ('tw', 'tx', 'tn'):
        b.episode('narrow_exclusive_' + t)
        b.xread('s0', t, 0x40, length=3, size=2, ident=1, tag=t + '_narrow_read')
        b.emit(f'  check({t}_rcount == mark_r + 1, "{t.upper()}_NARROW_READ_ONE_BURST");')
        b.emit(f'  mark_aw = {t}_awcount;')
        b.xwrite('s0', t, 0x40, length=3, size=2, ident=1, tag=t + '_narrow_write')
        b.emit(f'  check({t}_awcount == mark_aw + 1, "{t.upper()}_NARROW_WRITE_ONE_BURST");')
        b.read('s1', t, 0x40, length=3, tag=t + '_narrow_readback')
        b.episode('narrow_normal_' + t)
        b.read('s0', t, 0x80, length=3, size=2, tag=t + '_split_read')
        b.emit(f'  check({t}_rcount == mark_r + 4, "{t.upper()}_NORMAL_READ_PER_BEAT");')
        b.emit(f'  mark_aw = {t}_awcount;')
        b.write('s0', t, 0x80, length=3, size=2, tag=t + '_split_write')
        b.emit(f'  check({t}_awcount == mark_aw + 4, "{t.upper()}_NORMAL_WRITE_PER_BEAT");')
        b.read('s1', t, 0x80, length=3, tag=t + '_split_readback')
    b.episode('narrow_exclusive_tp')
    b.xread('s0', 'tp', 0x40, length=3, size=2, ident=1, tag='tp_narrow_read')
    b.xwrite('s0', 'tp', 0x40, length=3, size=2, ident=1, tag='tp_narrow_write')
    b.read('s1', 'tp', 0x40, length=3, tag='tp_narrow_readback')
    b.episode('narrow_exclusive_cleared')
    # A foreign write between the narrow exclusive read and write fails it.
    for t in ('tw', 'tx'):
        b.xread('s0', t, 0xC0, length=3, size=2, ident=2)
        b.write('s1', t, 0xC8)
        b.xwrite('s0', t, 0xC0, length=3, size=2, ident=2, tag=t + '_narrow_cleared')


def funnel_episodes(b):
    """The default funnel: the general per-target semantics, then races."""
    for t in ('tm', 'tp'):
        b.episode('pair_' + t)
        b.xread('s0', t, 0x40, ident=1)
        b.xwrite('s0', t, 0x40, ident=1, tag=t + '_pair_write')
        b.episode('competing_writer_' + t)
        b.xread('s1', t, 0x80, ident=1)
        b.write('s0', t, 0x80, ident=0)
        b.xwrite('s1', t, 0x80, ident=1, tag=t + '_after_foreign_write')
        b.episode('source_in_key_' + t)
        b.xread('s0', t, 0x100, ident=0)
        b.xread('s1', t, 0x100, ident=0)
        b.xwrite('s1', t, 0x100, ident=0)
        b.xread('s1', t, 0x100, ident=0)
        b.xwrite('s0', t, 0x100, ident=0, tag=t + '_stale_fails')
        b.xwrite('s1', t, 0x100, ident=0)
        b.episode('restrictions_' + t)
        b.xread('s0', t, 0x2C0, length=2, ident=1, tag=t + '_three_beats')
        b.xwrite('s0', t, 0x2C8, length=3, ident=1, tag=t + '_unaligned')
    b.episode('race')
    b.unexpect()
    for t in ('tm', 'tp'):
        b.emit('  for (race_i = 0; race_i < 64; race_i = race_i + 1) race_seen[race_i] = 0;')
        addr = b.base(t) + 0x800
        b.emit('  s0_gap = 1; s1_gap = 2;')
        b.emit('  fork')
        b.emit(f'   race_s0(32\'h{addr:08X}, 2, 6, 0);')
        b.emit(f'   race_s1(32\'h{addr:08X}, 2, 6, 0);')
        b.emit('  join')
        b.emit('  s0_gap = 0; s1_gap = 0;')
        for k, value in enumerate((12).to_bytes(4, 'little')):
            b.model.memory[t][0x800 + k] = value
        b.read('s1', t, 0x800, tag=t + '_race_count')


def write_bench(work, mapping, mode, episodes=None, clocks=None):
    """clocks: a CLOCKS key; the target models then run on clk_far."""
    b = Bench(mode, mapping)
    tclk = 'clk_far' if clocks else 'clk_main'
    main_ps, far_ps, phase_ps = CLOCKS[clocks] if clocks else (10000, None, 0)
    (episodes or {'graph': graph_episodes, 'wide': wide_episodes, 'funnel': funnel_episodes,
                  'split': split_episodes, 'split_none': split_episodes}[mode])(b)
    ports = mapping['ports']
    decl, conns, init = [], [], []
    driven = set()
    models = []
    for t, (protocol, _base, kind, _extra) in targets(mode).items():
        width = target_width(mode, t)
        if protocol == 'axi4':
            iw = b.ports.get(f'nl_t_{t}_awid', 0)
            nid = 1 << iw
            bytes_ = width // 8
            lane = (bytes_ - 1).bit_length()
            words = ', '.join(f'{t}_mem[{{{t}_ra[11:{lane}], {lane}\'d{k}}}]'
                              for k in reversed(range(bytes_)))
            ids = (f'assign nl_t_{t}_bid = {t}_awid; assign nl_t_{t}_rid = {t}_arid;'
                   if iw else '')
            src = ''
            if kind == 'target' and iw > ID_BITS:
                # This fixture assigns the source-index owner flags above the source-ID flags.
                # The port carries the value with its bits reversed.
                owner = ', '.join(f'nl_t_{t}_@D@id[{k}]' for k in range(iw - ID_BITS))
                src = (f'if ({t}_expect_src >= 0 && nl_t_{t}_@D@lock) '
                       f'check({{{owner}}} == {t}_expect_src, "{t.upper()}_@D@_SOURCE_INDEX");')
            text = _fill(_AXI, TCLK=tclk, T=t, U=t.upper(), IW=max(1, iw), NID=nid,
                         MON=1 if kind == 'target' else 0, BYTES=bytes_, LANE=lane,
                         LANEZ=f"{lane}'d0", RDATA='{' + words + '}', IDS=ids,
                         SRC_AW=src.replace('@D@', 'aw'), SRC_AR=src.replace('@D@', 'ar'))
            if not iw:
                decl.append(f"wire [0:0] nl_t_{t}_awid = 1'b0, nl_t_{t}_arid = 1'b0;")
            models.append(text)
            driven |= {f'nl_t_{t}_{s}' for s in ('awready', 'wready', 'bvalid', 'bresp', 'arready',
                                                 'rvalid', 'rresp', 'rlast', 'rdata', 'bid', 'rid')}
        elif protocol in ('apb4', 'apb3'):
            strobe = f'nl_t_{t}_pstrb' if protocol == 'apb4' else "4'hf"
            models.append(_fill(_APB, TCLK=tclk, T=t, STROBE=strobe))
            driven |= {f'nl_t_{t}_{s}' for s in ('pready', 'pslverr', 'prdata')}
        elif protocol == 'ahb_v3':
            models.append(_fill(_AHB, TCLK=tclk, T=t))
            driven |= {f'nl_t_{t}_{s}' for s in ('hreadyout', 'hresp', 'hrdata')}
        else:
            models.append(_fill(_LITE, TCLK=tclk, T=t))
            driven |= {f'nl_t_{t}_{s}' for s in ('awready', 'wready', 'bvalid', 'bresp', 'arready',
                                                 'rvalid', 'rresp', 'rdata')}
    for port in ports:
        name, width = port['name'], port['width']
        if port['direction'] == 'input' and name not in driven:
            decl.append(f'reg [{width - 1}:0] {name};')
            if name not in ('clk_main', 'clk_far', 'reset_n'):
                init.append(f'  {name} = 0;')
        else:
            decl.append(f'wire [{width - 1}:0] {name};')
        conns.append(f'.{name}({name})')
    sources = ''.join(
        _fill(_SOURCE, S=s, U=s.upper(), DW=source_width(mode, s), SB=source_width(mode, s) // 8,
              IW=ID_BITS, Q=QUEUE, SEED=f'{0xACE1 + 0x1111 * i:04X}')
        for i, s in enumerate(SOURCES))
    body = '\n'.join(b.lines)
    # A clocked graph: clk_far starts phase_ps after clk_main; a reset holds
    # four cycles of each clock and releases two cycles of each before traffic.
    far = (f'initial begin clk_far = 0; #{phase_ps / 1000:g}; '
           f'forever #{far_ps / 2000:g} clk_far = ~clk_far; end' if clocks else '')
    hold = ' repeat (4) @(negedge clk_far);' if clocks else ''
    settle = ' repeat (2) @(negedge clk_far);' if clocks else ''
    text = f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0;
always #{main_ps / 2000:g} clk_main = ~clk_main;
{far}
integer cycle = 0, mark_w = 0, mark_r = 0, mark_z = 0, mark_aw = 0, evicted_ok = 0, race_i,
        race_w = 0, race_r = 0, race_write_first = 0;
reg race_seen [0:63];
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 2000000) begin $display("ASSERT_FAIL:EXCLUSIVE.BENCH_LIMIT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*48-1:0] label_;
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:EXCLUSIVE.%0s cycle=%0d", label_, cycle); $fatal(1);
 end
endtask
task automatic pulse_reset;
 begin
  @(negedge clk_main); reset_n = 0;
  repeat (4) @(negedge clk_main);{hold}
  reset_n = 1;
  repeat (2) @(negedge clk_main);{settle}
 end
endtask
{''.join(models)}
{sources}
initial begin
{chr(10).join(init)}
  reset_n = 0;
  repeat (5) @(negedge clk_main);{hold}
  reset_n = 1;
  repeat (2) @(negedge clk_main);{settle}
{body}
  repeat (20) @(negedge clk_main);
  $display("NOCLOOM_PASS exclusive {mode} steps={b.steps}");
  $finish;
end
endmodule
'''
    (work / 'tb.sv').write_text(text)
    return b.steps
