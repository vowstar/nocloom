"""Transaction bench: exclusive access with several bursts in flight per source.

Each source face is shared by four ID threads. A thread issues one access at
a time and waits for its own response, so the four IDs of a source keep up to
four reads and four writes in flight on the face at once; the noise thread
(AxID 3) streams normal bursts without waiting. Independent checks:

- Per-ID response order: every R and B is matched to the oldest request of
  its source and AxID still waiting (a response for an ID with nothing
  waiting fails). Noise reads return a known word pattern per address, so a
  same-ID response out of order fails its data check; RLAST closes exactly
  the requested beats.
- Exclusive semantics under contention (atomic increment): every thread
  loops exclusive read, exclusive write of the read value plus one, until it
  has a fixed number of successes. Exclusive reads answer EXOKAY; an
  exclusive write answers EXOKAY or OKAY and nothing else. At the end the
  counter equals the number of EXOKAY writes, every EXOKAY wrote a distinct
  value, and the target models count exactly that many writes reaching the
  counter word (a failed exclusive write never reaches the target).
- Deterministic outcomes with concurrency elsewhere: a normal write of
  another ID of the same source, or of another source with the same AxID,
  to the reserved bytes between an exclusive read and its write makes the
  exclusive write fail (OKAY, memory keeps the other write); a write to
  other bytes leaves it succeeding (EXOKAY); of two keys holding the same
  region the first exclusive write wins and clears the other. Noise threads
  run on other bytes and other targets meanwhile.
- Common reset with exclusive bursts in flight, then an exclusive write
  without a new exclusive read fails (every reservation dropped) and a fresh
  pair succeeds.
- Target ports: at most the outlet's capacity in flight; a network-monitor
  target sees AxLOCK low and only writes that succeeded (with several bursts
  in flight, an exclusive read meeting a write in flight over its word
  records nothing); a target-mode
  target sees AxLOCK and a target ID encoding the exclusive owner flags,
  and the bench's slave keeps its own monitor per target ID.
- Concurrency: the faces must reach at least three reads and three writes in
  flight in the contention episode, so a serializing source cannot pass.
"""

from bench_registered import registered
from endpoint_ports import port_slots
from exclusive_outstanding import ID_BITS, SOURCES, WINDOW, targets

OKAY, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3
COUNTER = 0x100
MAXT = 1024  # tickets per source and episode
QUEUE = 64   # requests waiting per source face


def _fill(text, **values):
    for key, value in values.items():
        text = text.replace('@' + key + '@', str(value))
    return text


# AXI4 memory slave. The model queues four reads and four writes; the port
# capacity check uses the outlet's. Reads are served in order after
# @T@_lat plus 0..3 cycles of jitter; W beats go to the oldest AW waiting for
# data and the write commits at WLAST; B in commit order. @MON@ 1: target
# mode, the slave keeps a reservation per target ID (AxLOCK honored).
# @T@_landed counts counter writes with strobes, @T@_zero those without (a
# failed network exclusive write still goes out, with WSTRB zero).
_AXI = '''
integer @T@_lat = 0, @T@_blat = 0, @T@_rn, @T@_rk, @T@_rw, @T@_an, @T@_wk, @T@_bn, @T@_bw, @T@_k,
        @T@_landed, @T@_zero;
reg [31:0] @T@_ra [0:4]; reg [3:0] @T@_rl [0:4]; reg [@IW@-1:0] @T@_ri [0:4]; reg @T@_rx [0:4];
reg [31:0] @T@_wa [0:4]; reg [3:0] @T@_wl [0:4]; reg [@IW@-1:0] @T@_wi [0:4]; reg @T@_wx [0:4];
reg [31:0] @T@_wbuf [0:15]; reg [3:0] @T@_sbuf [0:15];
reg [@IW@-1:0] @T@_bi [0:4]; reg [1:0] @T@_br [0:4];
reg [31:0] mem_@T@ [0:1023];
reg @T@_mv [0:15]; reg [31:0] @T@_ma [0:15]; reg [7:0] @T@_ml [0:15];
reg [15:0] @T@_lfsr;
assign nl_t_@T@_arready = @T@_rn < 4;
assign nl_t_@T@_rvalid = @T@_rn > 0 && @T@_rw == 0;
assign nl_t_@T@_rid = @T@_ri[0];
assign nl_t_@T@_rdata = mem_@T@[((@T@_ra[0] >> 2) + @T@_rk) & 1023];
assign nl_t_@T@_rresp = @T@_rx[0] ? 2'b01 : 2'b00;
assign nl_t_@T@_rlast = @T@_rk == @T@_rl[0];
assign nl_t_@T@_awready = @T@_an < 4;
assign nl_t_@T@_wready = @T@_an > 0 && @T@_bn < 4;
assign nl_t_@T@_bvalid = @T@_bn > 0 && @T@_bw == 0;
assign nl_t_@T@_bid = @T@_bi[0];
assign nl_t_@T@_bresp = @T@_br[0];
task automatic @T@_commit;
 reg ok; integer b, bytes;
 begin
  bytes = (@T@_wl[0] + 1) * 4;
  ok = 1;
  if (@T@_wx[0]) ok = @T@_mv[@T@_wi[0]] && @T@_ma[@T@_wi[0]] == @T@_wa[0] && @T@_ml[@T@_wi[0]] == bytes;
  if (ok) begin
   for (b = 0; b <= @T@_wl[0]; b = b + 1)
    mem_@T@[((@T@_wa[0] >> 2) + b) & 1023] = merge(mem_@T@[((@T@_wa[0] >> 2) + b) & 1023], @T@_wbuf[b], @T@_sbuf[b]);
   if (@T@_wa[0][11:0] == @COUNTER@ && @T@_sbuf[0] != 0) @T@_landed = @T@_landed + 1;
   if (@T@_wa[0][11:0] == @COUNTER@ && @T@_sbuf[0] == 0) @T@_zero = @T@_zero + 1;
   // Any write that updates the slave drops overlapping reservations.
   for (b = 0; b < 16; b = b + 1)
    if (@T@_mv[b] && @T@_ma[b] < @T@_wa[0] + bytes && @T@_wa[0] < @T@_ma[b] + @T@_ml[b]) @T@_mv[b] = 0;
  end
  @T@_bi[@T@_bn] = @T@_wi[0]; @T@_br[@T@_bn] = @T@_wx[0] ? (ok ? 2'b01 : 2'b00) : 2'b00;
  if (@T@_bn == 0) @T@_bw = @T@_lat + @T@_blat + @T@_lfsr[1:0];
  @T@_bn = @T@_bn + 1;
  for (b = 0; b < 4; b = b + 1) begin
   @T@_wa[b] = @T@_wa[b + 1]; @T@_wl[b] = @T@_wl[b + 1]; @T@_wi[b] = @T@_wi[b + 1]; @T@_wx[b] = @T@_wx[b + 1];
  end
  @T@_an = @T@_an - 1;
 end
endtask
reg @T@_arhs, @T@_rhs, @T@_awhs, @T@_whs, @T@_bhs;
always @(posedge clk_main) begin
 // Handshakes as the port saw them at this edge, before any update.
 @T@_arhs = nl_t_@T@_arvalid && nl_t_@T@_arready; @T@_rhs = nl_t_@T@_rvalid && nl_t_@T@_rready;
 @T@_awhs = nl_t_@T@_awvalid && nl_t_@T@_awready; @T@_whs = nl_t_@T@_wvalid && nl_t_@T@_wready;
 @T@_bhs = nl_t_@T@_bvalid && nl_t_@T@_bready;
 @T@_lfsr <= {@T@_lfsr[14:0], @T@_lfsr[15] ^ @T@_lfsr[13] ^ @T@_lfsr[12] ^ @T@_lfsr[10]};
 if (!reset_n) begin
  @T@_rn = 0; @T@_rk = 0; @T@_rw = 0; @T@_an = 0; @T@_wk = 0; @T@_bn = 0; @T@_bw = 0;
  for (@T@_k = 0; @T@_k < 16; @T@_k = @T@_k + 1) @T@_mv[@T@_k] = 0;
 end else begin
  if (@T@_arhs) check(@T@_rn < @CAP@, "@U@_READ_CAPACITY");
  if (@T@_awhs) check(@T@_an + @T@_bn < @CAP@, "@U@_WRITE_CAPACITY");
  if (@T@_rhs) begin
   if (@T@_rk == @T@_rl[0]) begin
    for (@T@_k = 0; @T@_k < 4; @T@_k = @T@_k + 1) begin
     @T@_ra[@T@_k] = @T@_ra[@T@_k + 1]; @T@_rl[@T@_k] = @T@_rl[@T@_k + 1];
     @T@_ri[@T@_k] = @T@_ri[@T@_k + 1]; @T@_rx[@T@_k] = @T@_rx[@T@_k + 1];
    end
    @T@_rn = @T@_rn - 1; @T@_rk = 0; @T@_rw = @T@_lat + @T@_lfsr[3:2];
   end else @T@_rk = @T@_rk + 1;
  end else if (@T@_rn > 0 && @T@_rw > 0) @T@_rw = @T@_rw - 1;
  if (@T@_bhs) begin
   for (@T@_k = 0; @T@_k < 4; @T@_k = @T@_k + 1) begin
    @T@_bi[@T@_k] = @T@_bi[@T@_k + 1]; @T@_br[@T@_k] = @T@_br[@T@_k + 1];
   end
   @T@_bn = @T@_bn - 1; @T@_bw = @T@_lat + @T@_blat + @T@_lfsr[5:4];
  end else if (@T@_bn > 0 && @T@_bw > 0) @T@_bw = @T@_bw - 1;
  if (@T@_whs) begin
   check(nl_t_@T@_wlast === (@T@_wk == @T@_wl[0]), "@U@_WLAST");
   @T@_wbuf[@T@_wk] = nl_t_@T@_wdata; @T@_sbuf[@T@_wk] = nl_t_@T@_wstrb;
   if (@T@_wk == @T@_wl[0]) begin @T@_commit; @T@_wk = 0; end else @T@_wk = @T@_wk + 1;
  end
  if (@T@_arhs) begin
   check(nl_t_@T@_arburst === 2'd1 && nl_t_@T@_arsize === 3'd2, "@U@_AR_SHAPE");
@AR_LOCK@
   @T@_ra[@T@_rn] = nl_t_@T@_araddr; @T@_rl[@T@_rn] = nl_t_@T@_arlen; @T@_ri[@T@_rn] = nl_t_@T@_arid;
   @T@_rx[@T@_rn] = @MON@ && nl_t_@T@_arlock;
   if (@MON@ && nl_t_@T@_arlock) begin
    @T@_mv[nl_t_@T@_arid] = 1; @T@_ma[nl_t_@T@_arid] = nl_t_@T@_araddr;
    @T@_ml[nl_t_@T@_arid] = (nl_t_@T@_arlen + 1) * 4;
   end
   if (@T@_rn == 0) @T@_rw = @T@_lat + @T@_lfsr[3:2];
   @T@_rn = @T@_rn + 1;
  end
  if (@T@_awhs) begin
   check(nl_t_@T@_awburst === 2'd1 && nl_t_@T@_awsize === 3'd2, "@U@_AW_SHAPE");
@AW_LOCK@
   @T@_wa[@T@_an] = nl_t_@T@_awaddr; @T@_wl[@T@_an] = nl_t_@T@_awlen; @T@_wi[@T@_an] = nl_t_@T@_awid;
   @T@_wx[@T@_an] = @MON@ && nl_t_@T@_awlock;
   @T@_an = @T@_an + 1;
  end
 end
end
'''

# APB4 memory: PREADY after @T@_lat plus 0..1 cycles; writes merge strobes.
_APB = '''
integer @T@_lat = 0, @T@_acc, @T@_landed;
reg [31:0] mem_@T@ [0:1023];
reg [15:0] @T@_lfsr;
assign nl_t_@T@_pready = nl_t_@T@_psel && nl_t_@T@_penable && @T@_acc >= @T@_lat + @T@_lfsr[0];
assign nl_t_@T@_prdata = mem_@T@[nl_t_@T@_paddr[11:2]];
assign nl_t_@T@_pslverr = 1'b0;
always @(posedge clk_main) begin
 @T@_lfsr <= {@T@_lfsr[14:0], @T@_lfsr[15] ^ @T@_lfsr[13] ^ @T@_lfsr[12] ^ @T@_lfsr[10]};
 if (!reset_n) @T@_acc = 0;
 else begin
  if (nl_t_@T@_psel && nl_t_@T@_penable && nl_t_@T@_pready) begin
   if (nl_t_@T@_pwrite) begin
    mem_@T@[nl_t_@T@_paddr[11:2]] = merge(mem_@T@[nl_t_@T@_paddr[11:2]], nl_t_@T@_pwdata, nl_t_@T@_pstrb);
    if (nl_t_@T@_paddr[11:0] == @COUNTER@) @T@_landed = @T@_landed + 1;
   end
   @T@_acc = 0;
  end else if (nl_t_@T@_psel && nl_t_@T@_penable) @T@_acc = @T@_acc + 1;
 end
end
'''

# One source face: request queues and their issuers, the per-ID ticket FIFOs
# and the response monitors. Tickets of source @I@ are @I@*MAXT + n.
_SOURCE = '''
integer @S@_arh, @S@_art, @S@_awh, @S@_awt, @S@_wh, @S@_wk, @S@_ri, @S@_rd, @S@_wi, @S@_wd,
        @S@_rmax, @S@_wmax, @S@_tk, @S@_n, @S@_ar_hs, @S@_aw_hs, @S@_w_hs;
reg [15:0] @S@_lfsr;
integer @S@_rgap = 0, @S@_bgap = 0;
always @(negedge clk_main) begin
 @S@_lfsr <= {@S@_lfsr[14:0], @S@_lfsr[15] ^ @S@_lfsr[13] ^ @S@_lfsr[12] ^ @S@_lfsr[10]};
 nl_i_@S@_rready <= @S@_rgap == 0 || @S@_lfsr[2:0] >= @S@_rgap;
 nl_i_@S@_bready <= @S@_bgap == 0 || @S@_lfsr[5:3] >= @S@_bgap;
 // Issuers: a new request after the previous one's handshake.
 // After an abort the offered request stays until it is taken (AXI A3.2.1)
 // and no new one is loaded; the following common reset clears the face.
 if (!reset_n) begin
  nl_i_@S@_arvalid <= 0; nl_i_@S@_awvalid <= 0; nl_i_@S@_wvalid <= 0;
 end else begin
  if (!nl_i_@S@_arvalid || @S@_ar_hs) begin
   if (@S@_arh < @S@_art && !abort) begin
    @S@_n = @I@*QUEUE + @S@_arh % QUEUE;
    nl_i_@S@_arvalid <= 1; nl_i_@S@_araddr <= q_addr[@S@_n]; nl_i_@S@_arlen <= q_len[@S@_n];
    nl_i_@S@_arid <= q_id[@S@_n]; nl_i_@S@_aruser <= q_id[@S@_n]; nl_i_@S@_arlock <= q_lock[@S@_n];
    nl_i_@S@_arsize <= 2; nl_i_@S@_arburst <= 1; nl_i_@S@_arcache <= 0; nl_i_@S@_arprot <= 0;
    @S@_arh = @S@_arh + 1;
   end else nl_i_@S@_arvalid <= 0;
  end
  if (!nl_i_@S@_awvalid || @S@_aw_hs) begin
   if (@S@_awh < @S@_awt && !abort) begin
    @S@_n = @I@*QUEUE + @S@_awh % QUEUE;
    nl_i_@S@_awvalid <= 1; nl_i_@S@_awaddr <= w_addr[@S@_n]; nl_i_@S@_awlen <= w_len[@S@_n];
    nl_i_@S@_awid <= w_id[@S@_n]; nl_i_@S@_awuser <= w_id[@S@_n]; nl_i_@S@_awlock <= w_lock[@S@_n];
    nl_i_@S@_awsize <= 2; nl_i_@S@_awburst <= 1; nl_i_@S@_awcache <= 0; nl_i_@S@_awprot <= 0;
    @S@_awh = @S@_awh + 1;
   end else nl_i_@S@_awvalid <= 0;
  end
  // W beats of a burst follow its AW handshake.
  if (!nl_i_@S@_wvalid || @S@_w_hs) begin
   if (@S@_wh < @S@_wi && (!abort || @S@_wk != 0)) begin
    @S@_n = @I@*QUEUE + @S@_wh % QUEUE;
    nl_i_@S@_wvalid <= 1; nl_i_@S@_wdata <= w_data[@S@_n] + @S@_wk; nl_i_@S@_wstrb <= 4'hf;
    nl_i_@S@_wlast <= @S@_wk == w_len[@S@_n];
    if (@S@_wk == w_len[@S@_n]) begin @S@_wk = 0; @S@_wh = @S@_wh + 1; end
    else @S@_wk = @S@_wk + 1;
   end else nl_i_@S@_wvalid <= 0;
  end
 end
 @S@_ar_hs = 0; @S@_aw_hs = 0; @S@_w_hs = 0;
end
always @(posedge clk_main) begin
 if (!reset_n) begin
  @S@_ri = 0; @S@_rd = 0; @S@_wi = 0; @S@_wd = 0; @S@_ar_hs = 0; @S@_aw_hs = 0; @S@_w_hs = 0;
 end else begin
  if (nl_i_@S@_arvalid && nl_i_@S@_arready) begin @S@_ar_hs = 1; @S@_ri = @S@_ri + 1; end
  if (nl_i_@S@_awvalid && nl_i_@S@_awready) begin @S@_aw_hs = 1; @S@_wi = @S@_wi + 1; end
  if (nl_i_@S@_wvalid && nl_i_@S@_wready) @S@_w_hs = 1;
  if (nl_i_@S@_rvalid && nl_i_@S@_rready) begin
   // The oldest waiting read of this AxID.
   @S@_n = @I@*4 + nl_i_@S@_rid;
   check(rq_h[@S@_n] < rq_t[@S@_n], "@U@_R_WITHOUT_REQUEST");
   @S@_tk = rq[@S@_n*QUEUE + rq_h[@S@_n] % QUEUE];
   if (tk_beat[@S@_tk] == 0) tk_data[@S@_tk] = nl_i_@S@_rdata;
   check(tk_eresp[@S@_tk] < 0 || nl_i_@S@_rresp === tk_eresp[@S@_tk], "@U@_RRESP");
   check(!tk_check[@S@_tk] || nl_i_@S@_rdata === pattern(tk_addr[@S@_tk] + 4 * tk_beat[@S@_tk]), "@U@_RDATA_ORDER");
   check(nl_i_@S@_rlast === (tk_beat[@S@_tk] == tk_len[@S@_tk]), "@U@_RLAST");
   tk_resp[@S@_tk] = nl_i_@S@_rresp;
   if (nl_i_@S@_rlast) begin
    tk_done[@S@_tk] = 1; rq_h[@S@_n] = rq_h[@S@_n] + 1; @S@_rd = @S@_rd + 1;
   end else tk_beat[@S@_tk] = tk_beat[@S@_tk] + 1;
  end
  if (nl_i_@S@_bvalid && nl_i_@S@_bready) begin
   @S@_n = @I@*4 + nl_i_@S@_bid;
   check(bq_h[@S@_n] < bq_t[@S@_n], "@U@_B_WITHOUT_REQUEST");
   @S@_tk = bq[@S@_n*QUEUE + bq_h[@S@_n] % QUEUE];
   check(tk_eresp[@S@_tk] < 0 || nl_i_@S@_bresp === tk_eresp[@S@_tk], "@U@_BRESP");
   tk_resp[@S@_tk] = nl_i_@S@_bresp; tk_done[@S@_tk] = 1; bq_h[@S@_n] = bq_h[@S@_n] + 1;
   @S@_wd = @S@_wd + 1;
  end
  if (@S@_ri - @S@_rd > @S@_rmax) @S@_rmax = @S@_ri - @S@_rd;
  if (@S@_wi - @S@_wd > @S@_wmax) @S@_wmax = @S@_wi - @S@_wd;
  check(@S@_ri - @S@_rd <= @N@ + @NAR@ && @S@_wi - @S@_wd <= @N@ + @NAW@, "@U@_CAPACITY");
 end
end
'''


def write_bench(work, mapping, mode, capacity):
    chosen = targets(mode)
    names = sorted(chosen)
    index = {t: i for i, t in enumerate(names)}
    decl, conns, init = [], [], []
    for port in mapping['ports']:
        name, width = port['name'], port['width']
        # Target-side inputs are registers the models load at the falling
        # edge (bench_registered).
        kind = 'reg' if port['direction'] == 'input' else 'wire'
        decl.append(f'{kind} [{width - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and name not in ('clk_main', 'reset_n') and \
                not name.startswith('nl_t_'):
            init.append(f'{name} = 0;')
    widths = {p['name']: p['width'] for p in mapping['ports']}
    models = []
    for name in names:
        protocol, base, exclusive, outstanding = chosen[name]
        values = {'T': name, 'U': name.upper(), 'IDX': index[name], 'COUNTER': COUNTER}
        if protocol == 'apb4':
            models.append(registered(_fill(_APB, **values), 'clk_main')[0])
            continue
        iw = widths[f'nl_t_{name}_arid']
        target_mode = exclusive == 'target'
        if target_mode:
            # AxLOCK reaches the slave: its EXOKAY answers depend on it. The
            # target ID uses the exclusive owner flags from definition.target_ids.
            assert iw == ID_BITS + 1, (name, iw)
            lock_ar = lock_aw = ''
        elif exclusive == 'network':
            # The monitor sits in the outlet: the slave sees normal accesses.
            lock_ar = f'   check(nl_t_{name}_arlock === 1\'b0, "{name.upper()}_AR_LOCK_LOW");'
            lock_aw = f'   check(nl_t_{name}_awlock === 1\'b0, "{name.upper()}_AW_LOCK_LOW");'
        else:
            lock_ar = lock_aw = ''
        models.append(registered(_fill(_AXI, IW=iw, CAP=outstanding, MON=1 if target_mode else 0,
                                       AR_LOCK=lock_ar, AW_LOCK=lock_aw, **values),
                                 'clk_main')[0])
    source_text = ''.join(_fill(_SOURCE, S=s, U=s.upper(), I=i, N=capacity,
                                NAR=port_slots(mapping, f'source/{s}', 'ar'),
                                NAW=port_slots(mapping, f'source/{s}', 'aw'))
                          for i, s in enumerate(SOURCES))
    base_arms = ' '.join(f"{index[t]}: base_of = 32'h{chosen[t][1]:08x};" for t in names)
    kind_arms = ' '.join(f"{index[t]}: excl_of = {1 if chosen[t][2] else 0};" for t in names)
    mem_init = '\n'.join(
        f'  for (k = 0; k < 1024; k = k + 1) mem_{t}[k] = pattern(32\'h{chosen[t][1]:08x} + 4 * k);\n'
        f'  mem_{t}[{COUNTER // 4}] = 0; {t}_landed = 0; {t}_lat = 0;' +
        (f' {t}_blat = 0; {t}_zero = 0;' if chosen[t][0] != 'apb4' else '') for t in names)
    lfsr = ' '.join(f"{t}_lfsr = 16'h{0x1234 + 77 * i:04x};" for i, t in enumerate(names))
    episodes = _episodes(mode, names, chosen)
    text = f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0;
always #5 clk_main = ~clk_main;
localparam MAXT = {MAXT}, QUEUE = {QUEUE};
integer cycle = 0, abort = 0, k;
// Request queues per source face and the tickets their responses fill.
reg [31:0] q_addr [0:{2 * QUEUE - 1}]; reg [3:0] q_len [0:{2 * QUEUE - 1}];
reg [1:0] q_id [0:{2 * QUEUE - 1}]; reg q_lock [0:{2 * QUEUE - 1}];
reg [31:0] w_addr [0:{2 * QUEUE - 1}]; reg [3:0] w_len [0:{2 * QUEUE - 1}];
reg [1:0] w_id [0:{2 * QUEUE - 1}]; reg w_lock [0:{2 * QUEUE - 1}]; reg [31:0] w_data [0:{2 * QUEUE - 1}];
integer rq [0:{8 * QUEUE - 1}]; integer rq_h [0:7]; integer rq_t [0:7];
integer bq [0:{8 * QUEUE - 1}]; integer bq_h [0:7]; integer bq_t [0:7];
reg [31:0] tk_data [0:{2 * MAXT - 1}]; reg [31:0] tk_addr [0:{2 * MAXT - 1}];
integer tk_resp [0:{2 * MAXT - 1}]; integer tk_eresp [0:{2 * MAXT - 1}]; integer tk_beat [0:{2 * MAXT - 1}];
integer tk_len [0:{2 * MAXT - 1}]; reg tk_done [0:{2 * MAXT - 1}]; reg tk_check [0:{2 * MAXT - 1}];
integer tk_next [0:1];
// Contention bookkeeping per target: EXOKAY writes and the values written.
integer wins [0:7], losses [0:7]; reg [7:0] won [0:{8 * 256 - 1}];
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 2000000) begin $display("ASSERT_FAIL:EXCLUSIVE_OUTSTANDING.BENCH_LIMIT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*40-1:0] label_;
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:EXCLUSIVE_OUTSTANDING.%0s cycle=%0d", label_, cycle); $fatal(1);
 end
endtask
// The initial word of every address: noise reads check it.
function [31:0] pattern;
 input [31:0] address;
 pattern = {{8'h5a ^ address[23:16], address[15:8], 8'hc3, address[7:0]}};
endfunction
function [31:0] merge;
 input [31:0] old, data; input [3:0] strb;
 integer b;
 begin
  merge = old;
  for (b = 0; b < 4; b = b + 1) if (strb[b]) merge[8*b +: 8] = data[8*b +: 8];
 end
endfunction
function [31:0] base_of;
 input integer target;
 case (target) {base_arms} default: base_of = 0; endcase
endfunction
function integer excl_of;
 input integer target;
 case (target) {kind_arms} default: excl_of = 0; endcase
endfunction
{source_text}
{''.join(models)}
// A thread's request: queue it on its source face and register its ticket
// in the FIFO of its AxID (issue order is queue order).
task automatic issue_read;
 input integer s, id; input [31:0] addr; input integer len, lock, eresp, check_data;
 output integer tk;
 integer n;
 begin
  while (!abort && (s == 0 ? s0_art - s0_arh >= QUEUE : s1_art - s1_arh >= QUEUE))
   @(posedge clk_main);
  tk = s * MAXT + tk_next[s]; tk_next[s] = tk_next[s] + 1;
  check(tk_next[s] < MAXT, "TICKETS");
  tk_done[tk] = 0; tk_beat[tk] = 0; tk_len[tk] = len; tk_eresp[tk] = eresp; tk_addr[tk] = addr;
  tk_check[tk] = check_data;
  n = s * 4 + id; rq[n * QUEUE + rq_t[n] % QUEUE] = tk; rq_t[n] = rq_t[n] + 1;
  if (s == 0) begin
   q_addr[s0_art % QUEUE] = addr; q_len[s0_art % QUEUE] = len; q_id[s0_art % QUEUE] = id;
   q_lock[s0_art % QUEUE] = lock; s0_art = s0_art + 1;
  end else begin
   q_addr[QUEUE + s1_art % QUEUE] = addr; q_len[QUEUE + s1_art % QUEUE] = len;
   q_id[QUEUE + s1_art % QUEUE] = id; q_lock[QUEUE + s1_art % QUEUE] = lock; s1_art = s1_art + 1;
  end
 end
endtask
task automatic issue_write;
 input integer s, id; input [31:0] addr; input integer len, lock; input [31:0] data;
 input integer eresp; output integer tk;
 integer n;
 begin
  while (!abort && (s == 0 ? s0_awt - s0_wh >= QUEUE : s1_awt - s1_wh >= QUEUE))
   @(posedge clk_main);
  tk = s * MAXT + tk_next[s]; tk_next[s] = tk_next[s] + 1;
  check(tk_next[s] < MAXT, "TICKETS");
  tk_done[tk] = 0; tk_eresp[tk] = eresp; tk_addr[tk] = addr; tk_len[tk] = len;
  n = s * 4 + id; bq[n * QUEUE + bq_t[n] % QUEUE] = tk; bq_t[n] = bq_t[n] + 1;
  if (s == 0) begin
   w_addr[s0_awt % QUEUE] = addr; w_len[s0_awt % QUEUE] = len; w_id[s0_awt % QUEUE] = id;
   w_lock[s0_awt % QUEUE] = lock; w_data[s0_awt % QUEUE] = data; s0_awt = s0_awt + 1;
  end else begin
   w_addr[QUEUE + s1_awt % QUEUE] = addr; w_len[QUEUE + s1_awt % QUEUE] = len;
   w_id[QUEUE + s1_awt % QUEUE] = id; w_lock[QUEUE + s1_awt % QUEUE] = lock;
   w_data[QUEUE + s1_awt % QUEUE] = data; s1_awt = s1_awt + 1;
  end
 end
endtask
task automatic wait_ticket;
 input integer tk;
 while (!tk_done[tk] && !abort) @(posedge clk_main);
endtask
// Exclusive read: EXOKAY at an exclusive target, OKAY elsewhere (AxLOCK ignored).
task automatic ldrex;
 input integer s, id; input [31:0] addr; output [31:0] value;
 integer tk;
 begin
  issue_read(s, id, addr, 0, 1, excl_of(index_of(addr)) ? {EXOKAY} : {OKAY}, 0, tk);
  wait_ticket(tk); value = tk_data[tk];
 end
endtask
task automatic strex;
 input integer s, id; input [31:0] addr, value; input integer expect_; output integer resp;
 integer tk;
 begin
  issue_write(s, id, addr, 0, 1, value, expect_, tk);
  wait_ticket(tk); resp = tk_resp[tk];
  check(abort || resp == {EXOKAY} || resp == {OKAY}, "STREX_RESPONSE");
 end
endtask
task automatic store;
 input integer s, id; input [31:0] addr, value;
 integer tk;
 begin issue_write(s, id, addr, 0, 0, value, {OKAY}, tk); wait_ticket(tk); end
endtask
task automatic load;
 input integer s, id; input [31:0] addr; output [31:0] value;
 integer tk;
 begin issue_read(s, id, addr, 0, 0, {OKAY}, 0, tk); wait_ticket(tk); value = tk_data[tk]; end
endtask
function integer index_of;
 input [31:0] addr;
 case (addr[31:12]) {' '.join(f"20'h{chosen[t][1] >> 12:05x}: index_of = {index[t]};" for t in names)}
  default: index_of = -1; endcase
endfunction
// Atomic increment: n successful exclusive read / exclusive write pairs.
task automatic atomic;
 input integer s, id; input [31:0] addr; input integer n;
 integer got, tries, resp, t;
 reg [31:0] value;
 begin
  got = 0; tries = 0; t = index_of(addr);
  while (got < n && !abort) begin
   ldrex(s, id, addr, value);
   strex(s, id, addr, value + 1, -1, resp);
   if (!abort && resp == {EXOKAY}) begin
    // Every success writes a value no other success wrote.
    check(value < 255 && won[t * 256 + value + 1] == 0, "EXOKAY_VALUE_REUSED");
    won[t * 256 + value + 1] = 1; wins[t] = wins[t] + 1; got = got + 1;
   end else if (!abort) losses[t] = losses[t] + 1;
   tries = tries + 1;
   check(tries < 500, "EXCLUSIVE_LIVELOCK");
  end
 end
endtask
// Noise: pipelined normal bursts of one AxID without waiting, reads of the
// unwritten region (pattern data) and writes to the scratch region.
task automatic noise;
 input integer s, id, count, target;
 integer i, tk, last;
 begin
  last = -1;
  for (i = 0; i < count && !abort; i = i + 1) begin
   issue_read(s, id, base_of(target) + 32'h800 + ((s * 64 + id * 16 + i * 4) % 240) * 4, i % 4, 0, {OKAY}, 1, tk);
   issue_write(s, id, base_of(target) + 32'hc00 + ((s * 64 + id * 16 + i * 4) % 240) * 4, i % 4, 0,
               32'h77000000 + i, {OKAY}, last);
  end
  if (last >= 0) wait_ticket(last);
 end
endtask
task automatic quiesce;
 begin
  while (!abort && (s0_ri != s0_rd || s0_wi != s0_wd || s1_ri != s1_rd || s1_wi != s1_wd ||
                    s0_arh != s0_art || s1_arh != s1_art || s0_wh != s0_awt || s1_wh != s1_awt))
   @(posedge clk_main);
  repeat (20) @(posedge clk_main);
  check(s0_ri == s0_rd && s0_wi == s0_wd && s1_ri == s1_rd && s1_wi == s1_wd, "NO_EXTRA_RESPONSES");
 end
endtask
task automatic episode;
 input [8*40-1:0] label_;
 integer i;
 begin
  @(negedge clk_main); reset_n = 0; abort = 1;
  repeat (5) @(posedge clk_main);
  abort = 0;
  s0_arh = 0; s0_art = 0; s0_awh = 0; s0_awt = 0; s0_wh = 0; s0_wk = 0;
  s1_arh = 0; s1_art = 0; s1_awh = 0; s1_awt = 0; s1_wh = 0; s1_wk = 0;
  s0_rmax = 0; s0_wmax = 0; s1_rmax = 0; s1_wmax = 0; s0_rgap = 0; s0_bgap = 0; s1_rgap = 0; s1_bgap = 0;
  for (i = 0; i < 8; i = i + 1) begin
   rq_h[i] = 0; rq_t[i] = 0; bq_h[i] = 0; bq_t[i] = 0; wins[i] = 0; losses[i] = 0;
  end
  for (i = 0; i < 8 * 256; i = i + 1) won[i] = 0;
  tk_next[0] = 0; tk_next[1] = 0;
{mem_init}
  @(negedge clk_main); reset_n = 1;
  repeat (3) @(posedge clk_main);
  $display("EPISODE %0s", label_);
 end
endtask
initial begin
 reset_n = 0;
{chr(10).join(' ' + i for i in init)}
 s0_lfsr = 16'hace1; s1_lfsr = 16'hbeef; {lfsr}
 repeat (10) @(posedge clk_main);
{episodes}
 $display("NOCLOOM_PASS exclusive outstanding");
 $finish;
end
endmodule
'''
    (work / 'tb.sv').write_text(text)


def _episodes(mode, names, chosen):
    excl = [t for t in names if chosen[t][2]]
    plain = [t for t in names if not chosen[t][2]]
    noise_target = plain[0] if plain else excl[0]
    lines = []
    idx = {t: i for i, t in enumerate(names)}

    def base(t):
        return chosen[t][1]

    # 2. Deterministic outcomes per exclusive target, with noise elsewhere.
    for t in excl:
        b = base(t) + 0x200
        lines.append(f' episode("deterministic_{t}");')
        lines.append(f' {t}_lat = 3;' if chosen[t][0] != 'apb4' else ' ;')
        lines.append(' fork')
        lines.append(f'  noise(0, 3, 16, {idx[noise_target]});')
        lines.append(f'  noise(1, 3, 16, {idx[noise_target]});')
        lines.append(f'  noise(1, 2, 12, {idx[t]});')
        lines.append(f'  begin : seq_{t} reg [31:0] v, w; integer r, tk, k2, k3, k4;')
        # Another ID of the same source writes the reserved word: fail.
        lines.append(f'   ldrex(0, 0, 32\'h{b:08x}, v);')
        lines.append(f'   store(0, 1, 32\'h{b:08x}, 32\'h0000a001);')
        lines.append(f'   strex(0, 0, 32\'h{b:08x}, 32\'h0000b001, {OKAY}, r);')
        lines.append(f'   load(0, 2, 32\'h{b:08x}, w); check(w == 32\'h0000a001, "OTHER_ID_WRITE_KEPT");')
        # Another source with the same AxID writes it: fail.
        lines.append(f'   ldrex(0, 0, 32\'h{b + 0x10:08x}, v);')
        lines.append(f'   store(1, 0, 32\'h{b + 0x10:08x}, 32\'h0000a002);')
        lines.append(f'   strex(0, 0, 32\'h{b + 0x10:08x}, 32\'h0000b002, {OKAY}, r);')
        lines.append(f'   load(0, 1, 32\'h{b + 0x10:08x}, w); check(w == 32\'h0000a002, "OTHER_SOURCE_WRITE_KEPT");')
        # Other bytes written meanwhile (another ID and another source): success.
        lines.append(f'   ldrex(0, 1, 32\'h{b + 0x20:08x}, v);')
        lines.append(f'   store(0, 2, 32\'h{b + 0x24:08x}, 32\'h0000a003);')
        lines.append(f'   store(1, 1, 32\'h{b + 0x40:08x}, 32\'h0000a004);')
        lines.append(f'   strex(0, 1, 32\'h{b + 0x20:08x}, 32\'h0000b003, {EXOKAY}, r);')
        lines.append(f'   load(1, 0, 32\'h{b + 0x20:08x}, w); check(w == 32\'h0000b003, "DISJOINT_WRITE_SUCCEEDS");')
        # Two keys hold the same region: the first exclusive write wins and
        # clears the other.
        lines.append(f'   ldrex(0, 0, 32\'h{b + 0x30:08x}, v);')
        lines.append(f'   ldrex(1, 0, 32\'h{b + 0x30:08x}, v);')
        lines.append(f'   strex(1, 0, 32\'h{b + 0x30:08x}, 32\'h0000b004, {EXOKAY}, r);')
        lines.append(f'   strex(0, 0, 32\'h{b + 0x30:08x}, 32\'h0000b005, {OKAY}, r);')
        lines.append(f'   load(0, 3, 32\'h{b + 0x30:08x}, w); check(w == 32\'h0000b004, "FIRST_WRITER_WINS");')
        # An exclusive pair while another ID of the same source keeps bursts
        # to the same target in flight.
        lines.append(f'   ldrex(0, 2, 32\'h{b + 0x50:08x}, v);')
        lines.append(f'   strex(0, 2, 32\'h{b + 0x50:08x}, v + 1, {EXOKAY}, r);')
        if chosen[t][0] == 'axi4' and chosen[t][2] == 'network' and chosen[t][3] > 1:
            # A write of another ID ahead of the exclusive read at an outlet
            # of several bursts (its B held at the slave): the read enters
            # while the write is in flight over its word, answers EXOKAY and
            # records nothing, so the exclusive write fails and the word keeps
            # the write's value.
            lines.append(f'   {t}_blat = 60;')
            lines.append(f'   issue_write(0, 1, 32\'h{b + 0x60:08x}, 0, 0, 32\'h0000a006, {OKAY}, tk);')
            lines.append(f'   while ({t}_an == 0 && {t}_bn == 0) @(posedge clk_main);')
            lines.append(f'   ldrex(0, 0, 32\'h{b + 0x60:08x}, v);')
            lines.append(f'   strex(0, 0, 32\'h{b + 0x60:08x}, v + 1, {OKAY}, r);')
            lines.append(f'   wait_ticket(tk); {t}_blat = 0;')
            lines.append(f'   load(1, 3, 32\'h{b + 0x60:08x}, w); '
                         f'check(w == 32\'h0000a006, "IN_FLIGHT_READ_RECORDS_NOTHING");')
        elif chosen[t][0] == 'axi4' and chosen[t][2] == 'network':
            # A write of another ID ahead of the exclusive read at the outlet
            # (its B held at the slave): the shared in-flight pool lets the
            # read in only after the write's B has left, so the read sees the
            # landed word and records its reservation, and the exclusive
            # write succeeds.
            lines.append(f'   {t}_blat = 60;')
            lines.append(f'   issue_write(0, 1, 32\'h{b + 0x60:08x}, 0, 0, 32\'h0000a006, {OKAY}, tk);')
            lines.append(f'   while ({t}_an == 0 && {t}_bn == 0) @(posedge clk_main);')
            lines.append(f'   ldrex(0, 0, 32\'h{b + 0x60:08x}, v);')
            lines.append(f'   check(v == 32\'h0000a006, "POOL_READ_SEES_LANDED_WRITE");')
            lines.append(f'   strex(0, 0, 32\'h{b + 0x60:08x}, v + 1, {EXOKAY}, r);')
            lines.append(f'   wait_ticket(tk); {t}_blat = 0;')
            lines.append(f'   load(1, 3, 32\'h{b + 0x60:08x}, w); '
                         f'check(w == 32\'h0000a007, "POOL_READ_RECORDS");')
        # The key is {source, AxID}: two IDs of one source hold separate
        # reservations at the same target, and a key without a reservation
        # fails on another key's region (another ID of the same source, or
        # the same AxID of another source).
        lines.append(f'   ldrex(0, 0, 32\'h{b + 0x70:08x}, v);')
        lines.append(f'   ldrex(0, 1, 32\'h{b + 0x80:08x}, w);')
        lines.append(f'   strex(0, 0, 32\'h{b + 0x70:08x}, v + 1, {EXOKAY}, r);')
        lines.append(f'   strex(0, 1, 32\'h{b + 0x80:08x}, w + 1, {EXOKAY}, r);')
        lines.append(f'   ldrex(0, 0, 32\'h{b + 0x90:08x}, v);')
        lines.append(f'   strex(0, 1, 32\'h{b + 0x90:08x}, 32\'h0000c001, {OKAY}, r);')
        lines.append(f'   strex(1, 0, 32\'h{b + 0x90:08x}, 32\'h0000c002, {OKAY}, r);')
        lines.append(f'   load(1, 1, 32\'h{b + 0x90:08x}, w); check(w == v, "KEYLESS_WRITES_FAIL");')
        lines.append(f'   strex(0, 0, 32\'h{b + 0x90:08x}, v + 1, {EXOKAY}, r);')
        lines.append(f'   ldrex(1, 0, 32\'h{b + 0xa0:08x}, v);')
        lines.append(f'   ldrex(0, 0, 32\'h{b + 0xb0:08x}, w);')
        lines.append(f'   strex(1, 0, 32\'h{b + 0xa0:08x}, v + 1, {EXOKAY}, r);')
        lines.append(f'   strex(0, 0, 32\'h{b + 0xb0:08x}, w + 1, {EXOKAY}, r);')
        # An exclusive admission failure (16 bytes not aligned to 16)
        # completes locally after the slow network burst of its ID.
        slow = f'{t}_lat = 30;' if chosen[t][0] != 'apb4' else f'{t}_lat = 20;'
        lines.append(f'   {slow}')
        lines.append(f'   issue_write(0, 2, 32\'h{b + 0xc0:08x}, 0, 0, 32\'h0000d001, {OKAY}, tk);')
        lines.append(f'   issue_write(0, 2, 32\'h{b + 0xd4:08x}, 3, 1, 32\'h0000d002, {SLVERR}, k2);')
        lines.append(f'   issue_read(0, 2, 32\'h{b + 0xc0:08x}, 0, 0, {OKAY}, 0, k3);')
        lines.append(f'   issue_read(0, 2, 32\'h{b + 0xd4:08x}, 3, 1, {SLVERR}, 0, k4);')
        lines.append('   wait_ticket(tk); wait_ticket(k2); wait_ticket(k3); wait_ticket(k4);')
        lines.append(f'   {t}_lat = 3;')
        lines.append('  end')
        lines.append(' join')
        lines.append(' quiesce;')

    # 1. Contention: three IDs of each source increment the counter of every
    # exclusive target; AxID 3 streams noise. Each source keeps up to four
    # reads and four writes in flight.
    rounds = 4
    lines.append(' episode("atomic_increment");')
    lines.append(' s0_rgap = 2; s1_bgap = 2;')
    for t in names:
        if chosen[t][0] != 'apb4':
            lines.append(f' {t}_lat = {2 + 3 * idx[t]};')
    lines.append(' fork')
    for s in (0, 1):
        for ident in (0, 1, 2):
            # Each thread visits every exclusive target in turn.
            body = ' '.join(f'atomic({s}, {ident}, 32\'h{base(t) + COUNTER:08x}, {rounds});'
                            for t in (excl[ident % len(excl):] + excl[:ident % len(excl)]))
            lines.append(f'  begin {body} end')
        lines.append(f'  noise({s}, 3, 24, {idx[noise_target]});')
    lines.append(' join')
    lines.append(' quiesce;')
    threads = 2 * 3 * rounds
    for t in excl:
        i = idx[t]
        lines.append(f' check(wins[{i}] == {threads}, "WINS_{t.upper()}");')
        lines.append(f' check(mem_{t}[{COUNTER // 4}] == {threads}, "COUNTER_{t.upper()}");')
        lines.append(f' check({t}_landed == {threads}, "LANDED_{t.upper()}");')
        if chosen[t][0] == 'axi4' and chosen[t][2] == 'network':
            # Every lost exclusive write reached the target without strobes.
            lines.append(f' check({t}_zero == losses[{i}], "ZERO_STROBE_{t.upper()}");')
    lines.append(' $display("IN_FLIGHT s0_r=%0d s0_w=%0d s1_r=%0d s1_w=%0d", s0_rmax, s0_wmax, '
                 's1_rmax, s1_wmax);')
    # Contention happened: some exclusive writes lost their reservation.
    lines.append(' $display("CONTENTION ' + ' '.join(f'{t}_lost=%0d' for t in excl) + '", ' +
                 ', '.join(f'losses[{idx[t]}]' for t in excl) + ');')
    lines.append(' check(' + ' + '.join(f'losses[{idx[t]}]' for t in excl) + ' > 0, "CONTENTION");')
    lines.append(' check(s0_rmax >= 3 && s0_wmax >= 3 && s1_rmax >= 3 && s1_wmax >= 3, '
                 '"FACES_IN_FLIGHT");')
    # A read of the counter through the network agrees with the model.
    for t in excl:
        lines.append(f' begin : read_{t} reg [31:0] v; load(1, 2, 32\'h{base(t) + COUNTER:08x}, v); '
                     f'check(v == {threads}, "READ_BACK_{t.upper()}"); end')

    # 3. Common reset with exclusive bursts in flight; afterwards no
    # reservation survives and a fresh pair succeeds.
    lines.append(' episode("reset_mid_flight");')
    # Reservations held when the reset arrives (s0, AxID 3 at every
    # exclusive target): an exclusive write after it, without a new
    # exclusive read, fails.
    held = {t: base(t) + 0x300 for t in excl}
    lines.append(' begin : held reg [31:0] v;')
    for t in excl:
        lines.append(f'  ldrex(0, 3, 32\'h{held[t]:08x}, v);')
    lines.append(' end')
    lines.append(' fork')
    for s in (0, 1):
        for ident in (0, 1, 2):
            t = excl[(s + ident) % len(excl)]
            lines.append(f'  atomic({s}, {ident}, 32\'h{base(t) + COUNTER:08x}, 50);')
        lines.append(f'  noise({s}, 3, 30, {idx[noise_target]});')
    lines.append('  begin repeat (60) @(posedge clk_main); abort = 1; end')
    lines.append(' join')
    lines.append(' episode("after_reset");')
    lines.append(' begin : after reg [31:0] v; integer r;')
    for t in excl:
        lines.append(f'  strex(0, 3, 32\'h{held[t]:08x}, 32\'h0000e001, {OKAY}, r);')
        lines.append(f'  load(1, 3, 32\'h{held[t]:08x}, v); '
                     f'check(v == pattern(32\'h{held[t]:08x}), "RESET_DROPS_{t.upper()}");')
    for t in excl:
        a = base(t) + COUNTER
        lines.append(f'  ldrex(0, 0, 32\'h{a:08x}, v);')
        lines.append(f'  strex(0, 0, 32\'h{a:08x}, v + 1, {EXOKAY}, r);')
        # A second exclusive write without a new exclusive read fails.
        lines.append(f'  strex(0, 0, 32\'h{a:08x}, v + 2, {OKAY}, r);')
        lines.append(f'  load(1, 0, 32\'h{a:08x}, v); check(v == 1, "AFTER_RESET_{t.upper()}");')
    lines.append(' end')
    lines.append(' quiesce;')
    return '\n'.join(lines)
