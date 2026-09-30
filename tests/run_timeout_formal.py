"""Formal checks of the target timer on stand-alone outlets (SymbiYosys).

Every input of the outlet is free except that the common reset is applied
once at the start, the network always accepts responses (a pooled AXI4 outlet
holds an in-flight entry until its response leaves, so a stuck response
channel would legitimately block the other direction) and, for the AHB outlet
only, frames its request packets (first flags follow last flags, a
read is one beat). A monitor
recounts the watchdog from the network faces alone: cycles that follow one
with a request offered or a packet held and offer no response, restarted by a
response. The
timer is 1024 cycles (the smallest allowed), so the properties are proved
unbounded with PDR rather than by a deep BMC; each outlet is checked with
completion and report-only answers.

  EXPIRY      nl_status_timed_out rises exactly when the count reaches the
              timeout and matches the monitor on every cycle (P1).
  STICKY      once high it stays high until reset (P2).
  NO_NEW      after expiry no write starts on the target interface, and (APB,
              AXI4-Lite, AHB) at most the 16 reads of the packet already held
              do; an AXI4 outlet starts nothing (P3).
  STABLE      a pending APB transfer, AHB phase or AXI VALID keeps its
              signals until accepted, across expiry as well (P4).
  READ_AFTER  after expiry every read beat follows within four cycles with
              completion (P6), and without it no response valid rises (P8).
              On the pooled AXI4 outlet the deadline pauses while a write
              burst is in flight: the held write keeps the shared pool entry
              until its response has left.
  HELPER      before expiry the outlet's count and counting condition equal
              the monitor's.
  RELATED     until the outlet's status rises, its counting condition equals
              the monitor's.
  HELD_*      (AHB) until then the monitor's held packets are the outlet's:
              write contexts and the B buffer, the read issue beat and the
              reserved beats that close a packet; its recorded condition is
              the outlet's.
  LATE_*      (AHB) after expiry the reads started, the read in the address
              phase and the beats left of the held packet stay within 17.
  POOL_*      (AXI4, two bursts in flight) the in-flight pool, less releases still on their way, counts exactly
              the bursts the outlet holds: the read queue, the write issue
              stage, the write slots and the B stage, also after expiry; the
              reads received are the last beats in the R stage.
  REACH       a separate task proves that expiry is reachable with a target
              that never completes a transfer: the assertion that the status
              never rises must fail (PDR finds it; no VCD).
One PDR run over every assertion does not converge reliably, so the
unbounded proof runs three tasks:
relate proves RELATED and HELD_*; watch proves EXPIRY, STICKY and HELPER with
RELATED assumed, which is sound because relate proves it for every reachable
state under the same environment; after proves the target-side properties
and LATE_*. HELD_* and LATE_* are the inductive strengthenings PDR otherwise
has to find for the AHB outlet's several held packets; for the AXI4 outlet with
two bursts the after task also carries HELD_*/POOL_* to describe delayed pool release. The bounded mode
checks every assertion in one task.
The read bound before expiry (P5 at 16 cycles) is not proved at 1024 cycles:
PDR does not converge on it within the test time; the RTL benches cover it.
With the pooled AXI4 outlet (one in-flight entry shared by both directions,
held until its response leaves) READ_AFTER_EXPIRY only holds while the network
drains responses at all: a write whose B is never taken keeps its pool entry
and a newly offered read waits. The environment therefore accepts write responses."""
import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

CYCLES = 1024
ANSWERS = ('completion', 'report')
# Assertion groups and the sby tasks that check them: check runs every group
# in one task (bounded mode), the unbounded proof splits them; reach is the
# separate reachability task.
GROUPS = ('RELATE', 'WATCH', 'AFTER')
DEFINES = {'check': ['CHECK_RELATE', 'CHECK_WATCH', 'CHECK_AFTER'],
           'relate': ['CHECK_RELATE'],
           'watch': ['CHECK_WATCH', 'ASSUME_RELATED'],
           'after': ['CHECK_AFTER'],
           'reach': ['REACH']}
# Outlet state probed for the held-packet helpers.
PROBES = {'axi4x2': ['pool.count', 'r.count', 'w.busy', 'w.head_pending', 'w.head_beat_in',
                     'w.slot0.valid',
                     'w.slot1.valid', 'w.response_full', 'w.response_spare', 'r.slot0.valid',
                     'r.slot1.valid', 'r.slot0.received', 'r.slot1.received', 'r.full',
                     'r.response_last', 'r.spare', 'r.spare_last', 'w.remaining',
                     'pool.read_release', 'pool.write_release'],
          'ahb_v3': ['w.receiving', 'w.context0.valid', 'w.context1.valid', 'w.context2.valid',
                     'w.response_full', 'r.issuing', 'r.count', 'r.inflight', 'r.metadata0.last',
                     'r.metadata1.last', 'timeout.pending', 'r.issue_left', 'r.subread.started',
                     'timeout.orphan.taken', 'timeout.orphan.issuing', 'timeout.orphan.left',
                     'bus.address_phase', 'bus.write']}


def ports(work):
    result = []
    for line in (work / 'ports.tsv').read_text().splitlines():
        name, width, is_input = line.split('\t')
        result.append((name, int(width), is_input == '1'))
    return result


def monitors(kind):
    n = CYCLES
    kind = 'axi4' if kind == 'axi4x2' else kind
    # Reads that start after expiry: the rest of the packet already held.
    late = '''
reg [7:0] q_late_reads = 0;
always @(posedge nl_clk) begin
  if (!nl_reset_n) q_late_reads <= 0;
  else if (q_failed && @START@ && q_late_reads != 8'hff) q_late_reads <= q_late_reads + 1;
end
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) LATE_READS: assert (q_late_reads <= 17);
'''
    if kind in ('apb4', 'apb3'):
        strobe = kind == 'apb4'
        same = ('nl_apb_paddr == q_addr && nl_apb_pwrite == q_write && nl_apb_pwdata == q_data' +
                (' && nl_apb_pstrb == q_strb && nl_apb_pprot == q_prot' if strobe else ''))
        save = ('q_addr <= nl_apb_paddr; q_write <= nl_apb_pwrite; q_data <= nl_apb_pwdata;' +
                (' q_strb <= nl_apb_pstrb; q_prot <= nl_apb_pprot;' if strobe else ''))
        return f'''
reg q_held = 0, q_psel = 0; reg [11:0] q_addr; reg q_write; reg [31:0] q_data; reg [3:0] q_strb; reg [2:0] q_prot;
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) begin
  if (q_held) STABLE: assert (nl_apb_psel && nl_apb_penable && {same});
  if (q_failed && nl_apb_psel && !q_psel) NO_NEW: assert (!nl_apb_pwrite);
end
always @(posedge nl_clk) begin
  q_held <= nl_reset_n && nl_apb_psel && !(nl_apb_penable && nl_apb_pready); q_psel <= nl_apb_psel;
  {save}
end
''' + late.replace('@START@', 'nl_apb_psel && !q_psel'), 2 * n + 8
    if kind == 'ahb_v3':
        return '''
reg q_held = 0, q_seq = 0, q_dp = 0; reg [1:0] q_trans; reg [11:0] q_addr; reg [2:0] q_size, q_burst; reg q_write;
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) begin
  if (q_held) STABLE: assert (nl_ahb_htrans == q_trans && nl_ahb_haddr == q_addr &&
                               nl_ahb_hsize == q_size && nl_ahb_hburst == q_burst && nl_ahb_hwrite == q_write);
  if (q_failed && nl_ahb_hsel && nl_ahb_htrans[1] && !q_seq) NO_NEW: assert (!nl_ahb_hwrite);
end
always @(posedge nl_clk) begin
  q_held <= nl_reset_n && !nl_ahb_hreadyout; q_trans <= nl_ahb_htrans; q_addr <= nl_ahb_haddr;
  q_size <= nl_ahb_hsize; q_burst <= nl_ahb_hburst; q_write <= nl_ahb_hwrite;
  q_seq <= nl_ahb_hsel && nl_ahb_htrans[1];
  if (!nl_reset_n) q_dp <= 0;
  else if (nl_ahb_hreadyout) q_dp <= nl_ahb_hsel && nl_ahb_htrans[1];
end
// A legal target answers IDLE and BUSY with zero wait states: HREADY is low
// only during the data phase of a NONSEQ or SEQ transfer.
always @* if (!q_dp) assume (nl_ahb_hreadyout);
''' + late.replace('@START@', 'nl_ahb_hsel && nl_ahb_htrans[1] && nl_ahb_hreadyout && !nl_ahb_hwrite') + '''
// Reads still to start after expiry: the read in the address phase and the
// beats left of the held packet (its orphan copy once taken). With the reads
// already started they stay within LATE_READS's bound.
wire [4:0] q_rleft = nl_probe_timeout_orphan_taken ?
  (nl_probe_timeout_orphan_issuing ? nl_probe_timeout_orphan_left + 5'd1 : 5'd0) :
  (nl_probe_r_issuing ? nl_probe_r_issue_left + 5'd1 : 5'd0);
wire [8:0] q_rlate = q_late_reads + q_rleft + (nl_probe_bus_address_phase && !nl_probe_bus_write);
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) begin
  if (!q_failed) LATE_NONE: assert (q_late_reads == 0);
  if (nl_status_timed_out) LATE_HELD: assert (q_rlate <= 17);
end
''', None
    if kind == 'axi4_lite':
        return '''
reg q_aw = 0, q_w = 0, q_ar = 0, q_pav = 0, q_pwv = 0, q_parv = 0; reg [11:0] q_aa, q_ara; reg [31:0] q_wd;
reg [3:0] q_ws; reg [2:0] q_ap, q_arp;
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) begin
  if (q_aw) STABLE_AW: assert (nl_axil_awvalid && nl_axil_awaddr == q_aa && nl_axil_awprot == q_ap);
  if (q_w) STABLE_W: assert (nl_axil_wvalid && nl_axil_wdata == q_wd && nl_axil_wstrb == q_ws);
  if (q_ar) STABLE_AR: assert (nl_axil_arvalid && nl_axil_araddr == q_ara && nl_axil_arprot == q_arp);
  if (q_failed && ((nl_axil_awvalid && !q_pav) || (nl_axil_wvalid && !q_pwv))) NO_NEW: assert (0);
end
always @(posedge nl_clk) begin
  q_aw <= nl_reset_n && nl_axil_awvalid && !nl_axil_awready; q_aa <= nl_axil_awaddr; q_ap <= nl_axil_awprot;
  q_w <= nl_reset_n && nl_axil_wvalid && !nl_axil_wready; q_wd <= nl_axil_wdata; q_ws <= nl_axil_wstrb;
  q_ar <= nl_reset_n && nl_axil_arvalid && !nl_axil_arready; q_ara <= nl_axil_araddr; q_arp <= nl_axil_arprot;
  q_pav <= nl_axil_awvalid; q_pwv <= nl_axil_wvalid; q_parv <= nl_axil_arvalid;
end
''' + late.replace('@START@', 'nl_axil_arvalid && !q_parv'), 6 * n
    return f'''
reg q_aw = 0, q_w = 0, q_ar = 0, q_pav = 0, q_pwv = 0, q_parv = 0; reg [7:0] q_awc = 0, q_wlc = 0;
reg [11:0] q_aa, q_ara; reg [3:0] q_al, q_arl, q_ws, q_ac, q_arc; reg [2:0] q_as, q_ars, q_ap, q_arp;
reg [1:0] q_ab, q_arb; reg [31:0] q_wd; reg q_wl;
wire aw_hs = nl_axi_awvalid && nl_axi_awready, w_hs = nl_axi_wvalid && nl_axi_wready;
wire b_hs = nl_axi_bvalid && nl_axi_bready, ar_hs = nl_axi_arvalid && nl_axi_arready;
wire r_hs = nl_axi_rvalid && nl_axi_rready;
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) begin
  if (q_aw) STABLE_AW: assert (nl_axi_awvalid && nl_axi_awaddr == q_aa && nl_axi_awlen == q_al &&
                               nl_axi_awsize == q_as && nl_axi_awburst == q_ab && nl_axi_awprot == q_ap &&
                               nl_axi_awcache == q_ac);
  if (q_w) STABLE_W: assert (nl_axi_wvalid && nl_axi_wdata == q_wd && nl_axi_wstrb == q_ws && nl_axi_wlast == q_wl);
  if (q_ar) STABLE_AR: assert (nl_axi_arvalid && nl_axi_araddr == q_ara && nl_axi_arlen == q_arl &&
                               nl_axi_arsize == q_ars && nl_axi_arburst == q_arb && nl_axi_arprot == q_arp &&
                               nl_axi_arcache == q_arc);
  if (q_failed && ((nl_axi_awvalid && !q_pav) || (nl_axi_wvalid && !q_pwv) || (nl_axi_arvalid && !q_parv)))
    NO_NEW: assert (0);
  if (q_failed) BREADY_HIGH: assert (nl_axi_bready && nl_axi_rready);
end
always @(posedge nl_clk) begin
  q_aw <= nl_reset_n && nl_axi_awvalid && !nl_axi_awready; q_aa <= nl_axi_awaddr; q_al <= nl_axi_awlen;
  q_as <= nl_axi_awsize; q_ab <= nl_axi_awburst; q_ap <= nl_axi_awprot; q_ac <= nl_axi_awcache;
  q_w <= nl_reset_n && nl_axi_wvalid && !nl_axi_wready; q_wd <= nl_axi_wdata; q_ws <= nl_axi_wstrb;
  q_wl <= nl_axi_wlast;
  q_ar <= nl_reset_n && nl_axi_arvalid && !nl_axi_arready; q_ara <= nl_axi_araddr; q_arl <= nl_axi_arlen;
  q_ars <= nl_axi_arsize; q_arb <= nl_axi_arburst; q_arp <= nl_axi_arprot; q_arc <= nl_axi_arcache;
  q_pav <= nl_axi_awvalid; q_pwv <= nl_axi_wvalid; q_parv <= nl_axi_arvalid;
  if (!nl_reset_n) begin q_awc <= 0; q_wlc <= 0; end
  else begin
    if (aw_hs) q_awc <= q_awc + 1;
    if (w_hs && nl_axi_wlast) q_wlc <= q_wlc + 1;
  end
end
''', 2 * n + 8


def harness(kind, port_list, answer):
    extra, bound = monitors(kind)
    conns = [f'.{name}({name})' for name, _, _ in port_list]
    # The network always accepts responses: a pooled AXI4 outlet holds its
    # in-flight entry until the response leaves, so a stuck response channel
    # legitimately blocks the other direction and is outside the properties.
    tied = ('nl_rrsp_ready', 'nl_wrsp_ready')
    inputs = [f'input wire [{w - 1}:0] {n}' for n, w, i in port_list
              if i and n not in tied]
    wires = [f'wire [{w - 1}:0] {n};' for n, w, i in port_list if not i or n in tied]
    n = CYCLES
    stall = {'apb4': '!nl_apb_pready', 'apb3': '!nl_apb_pready', 'ahb_v3': '!nl_ahb_hreadyout',
             'axi4_lite': '!nl_axi_bvalid && !nl_axi_rvalid',
             'axi4': '!nl_axi_bvalid && !nl_axi_rvalid',
             'axi4x2': '!nl_axi_bvalid && !nl_axi_rvalid'}[kind]
    # The watchdog recounted from the network faces: a cycle after one with a
    # request offered or a packet held counts while no response is offered;
    # an offered response restarts the count. Before expiry the outlet's counter
    # equals the recount. The pooled AXI4 outlet holds one burst per direction
    # from its head until its response leaves (a flag suffices). APB and
    # AXI4-Lite outlets stage a completed packet's response while the next
    # packet's head is already accepted, so their held packets are counted,
    # not flagged: a write from a request beat taken until its response is
    # taken, a read from a request taken until its last response is taken. An
    # AHB outlet holds several packets, each answered on its own; there the
    # network frames its packets (a write beat is first exactly after a last
    # one, a read is one beat) and the monitor counts them.
    if kind == 'ahb_v3':
        held = '''reg q_inw = 0; reg [2:0] q_trw = 0, q_trr = 0;
always @* if (nl_wreq_valid) assume (nl_wreq_first == !q_inw);
always @* if (nl_rreq_valid) assume (nl_rreq_first && nl_rreq_last);
always @(posedge nl_clk)
  if (!nl_reset_n) begin q_inw <= 0; q_trw <= 0; q_trr <= 0; end
  else begin
    if (nl_wreq_valid && nl_wreq_ready) q_inw <= !nl_wreq_last;
    q_trw <= q_trw + (nl_wreq_valid && nl_wreq_ready && nl_wreq_first) - (nl_wrsp_valid && nl_wrsp_ready);
    q_trr <= q_trr + (nl_rreq_valid && nl_rreq_ready) - (nl_rrsp_valid && nl_rrsp_ready && nl_rrsp_last);
  end
// Until expiry the recount's packets are the outlet's: a write from its
// context to its B (a context per packet, then the B buffer), a read from its
// issue beat to its last response (the issue beat, then the two reserved
// beats, in flight or buffered, that close a packet). These lift RELATED into
// an inductive invariant.
wire [2:0] q_rheld = nl_probe_r_count + nl_probe_r_inflight;
// A suffix's first subread reserves metadata while its issue beat remains
// live. A last beat in that state represents the same packet twice.
wire q_shared_tail = nl_probe_r_issuing && nl_probe_r_subread_started && nl_probe_r_issue_left == 0;
always @(posedge nl_clk) if (RELATE && nl_reset_n && started && !nl_status_timed_out) begin
  HELD_INW: assert (q_inw == nl_probe_w_receiving);
  HELD_W: assert (q_trw == nl_probe_w_context0_valid + nl_probe_w_context1_valid +
                           nl_probe_w_context2_valid + nl_probe_w_response_full);
  HELD_RN: assert (q_rheld <= 2);
  HELD_R: assert (q_trr == nl_probe_r_issuing + (q_rheld > 0 && nl_probe_r_metadata0_last) +
                           (q_rheld > 1 && nl_probe_r_metadata1_last) - q_shared_tail);
  HELD_WAS: assert (q_was == nl_probe_timeout_pending);
end'''
    elif kind == 'axi4x2':
        # Two bursts in flight: a write from its head (the first beat taken
        # with no beats left, framed by AxLEN as the outlet frames it) until
        # its B is taken, a read from its request until its last beat is
        # taken. Until expiry they are the pool; at every cycle the pool is
        # the bursts the outlet holds, and the reads received are the last
        # beats staged.
        held = '''reg [3:0] q_wleft = 0; reg [2:0] q_trw = 0, q_trr = 0;
// The valid/ready rule of every generated stream (the verif stream monitors check it as
// STREAM.HOLD): a write word offered stays offered with its payload until taken. The outlet
// relies on it, since its AW may enter before the word leaves (axi4_outlet.hpp).
wire [255:0] q_wword = {nl_wreq_address, nl_wreq_write, nl_wreq_protection, nl_wreq_length,
  nl_wreq_size, nl_wreq_burst, nl_wreq_cache, nl_wreq_start_address, nl_wreq_source,
  nl_wreq_count, nl_wreq_error, nl_wreq_first, nl_wreq_last, nl_wreq_data, nl_wreq_be};
reg q_woffered = 0; reg [255:0] q_wkept = 0;
always @(posedge nl_clk) begin
  q_woffered <= nl_reset_n && nl_wreq_valid && !nl_wreq_ready; q_wkept <= q_wword;
end
always @* if (q_woffered) assume (nl_wreq_valid && q_wword == q_wkept);
wire q_whead = nl_wreq_valid && nl_wreq_ready && q_wleft == 0;
always @(posedge nl_clk)
  if (!nl_reset_n) begin q_wleft <= 0; q_trw <= 0; q_trr <= 0; end
  else begin
    if (nl_wreq_valid && nl_wreq_ready) q_wleft <= q_wleft == 0 ? nl_wreq_length : q_wleft - 1;
    q_trw <= q_trw + q_whead - (nl_wrsp_valid && nl_wrsp_ready);
    q_trr <= q_trr + (nl_rreq_valid && nl_rreq_ready) -
             (nl_rrsp_valid && nl_rrsp_ready && nl_rrsp_last);
  end
// A head the pool admitted before the network word is taken: its AW in the main entry with its
// first beat still in the word (head_pending), or its first beat in the W entry before its AW
// (head_beat_in, not yet in the main entry).
wire [3:0] q_held = nl_probe_r_count + nl_probe_w_busy + nl_probe_w_head_beat_in +
  nl_probe_w_slot0_valid + nl_probe_w_slot1_valid + nl_probe_w_response_full +
  nl_probe_w_response_spare;
// An entry returns to the pool in the cycle after its last response word left.
wire [3:0] q_pool = nl_probe_pool_count - nl_probe_pool_read_release - nl_probe_pool_write_release;
wire [1:0] q_received = (nl_probe_r_slot0_valid && nl_probe_r_slot0_received) +
                        (nl_probe_r_slot1_valid && nl_probe_r_slot1_received);
always @(posedge nl_clk) if ((RELATE || AFTER) && nl_reset_n && started) begin
  if (!nl_probe_w_head_pending) HELD_WLEFT: assert (q_wleft == nl_probe_w_remaining);
  if (!nl_status_timed_out)
    HELD_POOL: assert (q_trw + q_trr + nl_probe_w_head_pending + nl_probe_w_head_beat_in == q_pool);
  POOL_HELD: assert (q_pool == q_held);
  POOL_RECEIVED: assert (q_received == (nl_probe_r_full && nl_probe_r_response_last) +
                                       (nl_probe_r_spare && nl_probe_r_spare_last));
end'''
    elif kind == 'axi4':
        # The pooled AXI4 outlet holds one burst per direction until its
        # response leaves: a held flag per direction, set at the head accepted
        # with none held, cleared when the response is taken.
        held = '''reg q_trw = 0, q_trr = 0;
always @(posedge nl_clk)
  if (!nl_reset_n) begin q_trw <= 0; q_trr <= 0; end
  else begin
    if (nl_wreq_valid && nl_wreq_ready && !q_trw) q_trw <= 1;
    else if (nl_wrsp_valid && nl_wrsp_ready) q_trw <= 0;
    if (nl_rreq_valid && nl_rreq_ready) q_trr <= 1;
    else if (nl_rrsp_valid && nl_rrsp_ready && nl_rrsp_last) q_trr <= 0;
  end'''
    else:
        held = '''reg q_inw = 0; reg [2:0] q_trw = 0, q_trr = 0;
always @* if (nl_wreq_valid) assume (nl_wreq_first == !q_inw);
always @(posedge nl_clk)
  if (!nl_reset_n) begin q_inw <= 0; q_trw <= 0; q_trr <= 0; end
  else begin
    if (nl_wreq_valid && nl_wreq_ready) q_inw <= !nl_wreq_last;
    q_trw <= q_trw + (nl_wreq_valid && nl_wreq_ready && nl_wreq_first) -
             (nl_wrsp_valid && nl_wrsp_ready);
    q_trr <= q_trr + (nl_rreq_valid && nl_rreq_ready) -
             (nl_rrsp_valid && nl_rrsp_ready && nl_rrsp_last);
  end'''
    expiry = f'''
{held}
reg q_exp = 0, q_was = 0; reg [11:0] q_cnt = 0;
wire q_pending = nl_wreq_valid || nl_rreq_valid || q_trw != 0 || q_trr != 0;
always @(posedge nl_clk) begin
  if (!nl_reset_n) begin q_exp <= 0; q_was <= 0; q_cnt <= 0; end
  else begin
    q_was <= q_pending;
    if (nl_wrsp_valid || nl_rrsp_valid) q_cnt <= 0;
    else if (q_was && !q_exp) begin q_cnt <= q_cnt + 1; if (q_cnt == {n - 1}) q_exp <= 1; end
  end
end
// RELATED: until the outlet expires, its counting condition equals the
// recount's. It ties the outlet's held packets to the network faces and does
// not depend on the count, so it is proved on its own; the watch group then
// assumes it, which makes the count helpers inductive.
wire q_related = nl_probe_timeout_waiting == (q_was && !nl_wrsp_valid && !nl_rrsp_valid);
always @(posedge nl_clk) if (RELATE && nl_reset_n && started && !nl_status_timed_out)
  RELATED: assert (q_related);
`ifdef ASSUME_RELATED
always @(posedge nl_clk) if (nl_reset_n && started && !nl_status_timed_out) assume (q_related);
`endif
always @(posedge nl_clk) if (WATCH && nl_reset_n && started && !q_exp) begin
  HELPER: assert (nl_probe_timeout_count == q_cnt);
  HELPER_WAITING: assert (nl_probe_timeout_waiting == (q_was && !nl_wrsp_valid && !nl_rrsp_valid));
end
'''
    read_bound = ''
    if bound and answer == 'completion':
        # Cycles after expiry since the read burst was accepted or last
        # produced a beat. AXI4 with the pooled outlet (one burst in flight
        # across both directions): a held write blocks a new read's
        # acceptance until its response has left, so the deadline pauses
        # while a write burst is in flight at the outlet (q_trw); the
        # response channels are tied ready, so the pause spans only the few
        # drain cycles.
        pause = ' || q_trw' if kind.startswith('axi4') and kind != 'axi4_lite' else ''
        read_bound = f'''
reg [2:0] q_late = 0;
always @(posedge nl_clk) begin
  if (!nl_reset_n || nl_rrsp_valid || nl_rreq_ready || !q_failed{pause}) q_late <= 0;
  else if (q_late != 3'h7) q_late <= q_late + 1;
end
always @(posedge nl_clk) if (AFTER && nl_reset_n && started) READ_AFTER_EXPIRY: assert (q_late < 4);
'''
    silent = ''
    if answer == 'report':
        # Without completion no response valid rises after expiry.
        silent = '''
reg q_rv = 0, q_wv = 0;
always @(posedge nl_clk) begin q_rv <= nl_rrsp_valid; q_wv <= nl_wrsp_valid; end
always @(posedge nl_clk) if (AFTER && nl_reset_n && started && q_failed) begin
  if (!q_rv) SILENT_R: assert (!nl_rrsp_valid);
  if (!q_wv) SILENT_W: assert (!nl_wrsp_valid);
end
'''
    groups = '\n'.join(f'''`ifdef CHECK_{g}
localparam {g} = 1'b1;
`else
localparam {g} = 1'b0;
`endif''' for g in GROUPS)
    return f'''module formal_top(input wire nl_clk, {', '.join(inputs)});
{groups}
{chr(10).join(wires)}
nocloom_{'axi4_outlet' if kind in ('axi4', 'axi4x2') else 'apb_burst_outlet' if kind.startswith('apb') else
          'axi_lite_outlet' if kind == 'axi4_lite' else 'ahb_outlet'}_probe dut({', '.join(conns)});
assign nl_rrsp_ready = 1'b1;
assign nl_wrsp_ready = 1'b1;
reg initial_cycle = 1, started = 0, q_failed = 0;
always @(posedge nl_clk) begin initial_cycle <= 0; started <= 1; q_failed <= nl_reset_n && nl_status_timed_out; end
// The common reset is applied once at the start and then released.
always @* if (initial_cycle) assume (!nl_reset_n); else assume (nl_reset_n);
`ifdef REACH
// Reachability: with a target that never completes a transfer, this
// assertion must fail, with a trace to expiry.
always @* assume ({stall});
always @(posedge nl_clk) if (nl_reset_n) REACH: assert (!nl_status_timed_out);
`else
{extra}
{expiry}
{read_bound}
{silent}
always @(posedge nl_clk) if (WATCH && nl_reset_n && started) begin
  EXPIRY: assert (nl_status_timed_out == q_exp);
  if (q_failed) STICKY: assert (nl_status_timed_out);
end
`endif
endmodule
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--kind', required=True, choices=['apb4', 'apb3', 'axi4_lite', 'ahb_v3', 'axi4'])
    parser.add_argument('--depth', type=int, default=48)
    parser.add_argument('--mode', choices=['prove', 'bmc'], default='prove')
    # The reach task needs a trace to expiry (1030 frames). The solver does not
    # reach that depth on the queue-structured Lite engine within the test
    # timeout, so its registered check runs the safety tasks only and the
    # expiry's reachability is witnessed by verif_target_timeouts_negative.
    parser.add_argument('--tasks', default='relate,watch,after,reach')
    # Bursts in flight of the AXI4 outlet: 1, or 2 (fixture kind axi4x2).
    parser.add_argument('--capacities', default='1')
    args = parser.parse_args()
    tasks = args.tasks.split(',')
    if any(task not in DEFINES for task in tasks):
        parser.error(f'tasks are among {", ".join(DEFINES)}')
    # The watch group assumes RELATED, so it only counts with RELATED proved
    # in the same run.
    if 'watch' in tasks and 'relate' not in tasks:
        parser.error('watch needs relate')
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    capacities = [int(c) for c in args.capacities.split(',')]
    if any(c not in (1, 2) for c in capacities) or (capacities != [1] and args.kind != 'axi4'):
        parser.error('capacity 2 applies to the axi4 kind only')
    results = []
    for capacity, answer in [(c, a) for c in capacities for a in ANSWERS]:
        # Expiry of the two-burst outlet is reached in the RTL benches
        # (verif_target_timeouts_negative, targets mx, mq): a PDR trace to it
        # does not finish within the test time.
        run_tasks = [task for task in tasks if capacity == 1 or task != 'reach']
        kind = 'axi4x2' if capacity == 2 else args.kind
        work = Path(tempfile.mkdtemp(prefix=f'run-{kind}-{answer}-', dir=base))
        (base / f'latest-{kind}-{answer}.txt').write_text(str(work) + '\n')
        subprocess.run([args.fixture, str(work), kind, str(CYCLES), answer,
                        *PROBES.get(kind, ())], check=True, timeout=120)
        (work / 'formal_top.sv').write_text(harness(kind, ports(work), answer))
        engine = 'abc pdr' if args.mode == 'prove' else 'smtbmc z3'
        safety = [task for task in run_tasks if task != 'reach']
        sby = '[tasks]\n' + '\n'.join(run_tasks) + '\n\n[options]\n'
        for task in safety:
            sby += f'{task}: mode {args.mode}\n{task}: depth {args.depth}\n{task}: aigsmt z3\n'
        if 'reach' in run_tasks:
            sby += 'reach: mode prove\nreach: vcd off\n'
        sby += '\n[engines]\n'
        for task in safety:
            sby += f'{task}: {engine}\n'
        if 'reach' in run_tasks:
            sby += 'reach: abc pdr\n'
        sby += '\n[script]\nread_verilog -formal outlet.v\n'
        for task in run_tasks:
            defines = ' '.join(f'-D{name}' for name in DEFINES[task])
            sby += f'{task}: read_verilog -formal -sv {defines} formal_top.sv\n'
        sby += f'''prep -top formal_top

[files]
{work / 'outlet.v'}
{work / 'formal_top.sv'}
'''
        (work / 'timeout.sby').write_text(sby)
        started = time.monotonic()
        proc = subprocess.run(['sby', '-f', 'timeout.sby'], cwd=work, capture_output=True,
                              text=True, timeout=2400, check=False)
        seconds = round(time.monotonic() - started, 1)
        (work / 'sby.log').write_text(proc.stdout + proc.stderr)
        status = {}
        for task in run_tasks:
            path = work / f'timeout_{task}' / 'status'
            status[task] = path.read_text().split()[0] if path.exists() else 'MISSING'
        results.append({'capacity': capacity, 'answer': answer, 'status': status,
                        'seconds': seconds})
        (base / 'status.json').write_text(json.dumps({'kind': args.kind, 'mode': args.mode,
                                                      'depth': args.depth,
                                                      'results': results}) + '\n')
        expected = {task: 'FAIL' if task == 'reach' else 'PASS' for task in run_tasks}
        assert status == expected, \
            (answer, status, proc.stdout[-3000:])
        print(f'FORMAL_PASS target timeout {kind} {answer} {args.mode} cycles={CYCLES} '
              f'seconds={seconds}')


if __name__ == '__main__':
    shutil.which('sby') or exit('sby not found')
    main()
