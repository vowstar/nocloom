"""Formal checks of the target timer on stand-alone outlets (SymbiYosys).

The common reset is applied once at the start. AXI4 network response READY
can stay low indefinitely. The other outlets' network responses are always
accepted. AHB request packets are framed: first flags follow last flags and
a read is one beat. The monitor counts consecutive target waits from the bus
handshakes. AXI response credit comes from the target AW, WLAST, AR, B and R
handshakes. The AXI target offers only responses it owes before expiry.
An AXI4 target holds VALID and payload while the outlet blocks B or R. Separate bounded
tasks exercise both directions of shared response backpressure. The unbounded
proofs use the full 1024-cycle timer. Each outlet is checked with completion
and report-only answers.

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
              reserved beats that close a packet.
  LATE_*      (AHB) after expiry the reads started, the read in the address
              phase and the beats left of the held packet stay within 17.
  POOL_*      (AXI4, two bursts in flight) the in-flight pool, less releases still on their way, counts exactly
              the bursts the outlet holds: the read queue, the write issue
              stage, the write slots and the B stage, also after expiry; the
              reads received are the last beats in the R stage.
  REACH       a separate task proves that expiry is reachable with a target
              that never completes a transfer: the assertion that the status
              never rises must fail. AXI4 stalls a mapped read at the target
              AR port. With capacity two, an earlier read keeps the other entry.
One PDR run over every assertion does not converge reliably, so the
unbounded proof runs three tasks:
relate proves RELATED and HELD_*; watch proves EXPIRY, STICKY and HELPER with
RELATED assumed, which is sound because relate proves it for every reachable
state before the outlet expires under the same environment. The watch task
checks the status even on the first cycle when it could differ from the
monitor. The after task proves the target-side properties and LATE_*.
For the AXI4 outlet with two bursts, relate holds the status low and proves
the wait and credit relations together. Every prefix before the first expiry
has the same state and inputs in this model. Watch keeps the real counters
and uses the wait relation while both statuses are still low. After checks
the pool and held-state properties across expiry with the counters free.
HELD_* and LATE_* are the inductive strengthenings PDR otherwise
has to find for the AHB outlet's several held packets; for the AXI4 outlet with
two bursts the after task also carries HELD_*/POOL_* to describe delayed pool release.
The after task lets each timer count of the outlet vary freely, so the status
can rise in any cycle in which the outlet waits. This admits every real counter
trace and more, without searching for the 1024-cycle count in the target-side
proof. The exact watch task proves when the status really rises. The AXI4 after
task uses the outlet status for the response credit assumption, which the watch
task proves equal to the monitor. The bounded mode checks every assertion in
one task with the actual counters.
The read bound before expiry (P5 at 16 cycles) is not proved at 1024 cycles:
PDR does not converge on it within the test time; the RTL benches cover it.
READ_AFTER_EXPIRY bounds cycles without an offered read response or a ready
read request port. The bound pauses while a write holds a pool entry. A held
response can occupy that entry indefinitely, so no fairness is assumed."""
import argparse
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

CYCLES = 1024
ANSWERS = ('completion', 'report')
AXI_CHANNELS = ('aw', 'w', 'ar', 'b', 'r')
# Assertion groups and the sby tasks that check them: check runs every group
# in one task (bounded mode), the unbounded proof splits them; reach is the
# separate reachability task.
GROUPS = ('RELATE', 'WATCH', 'AFTER')
DEFINES = {'check': ['CHECK_RELATE', 'CHECK_WATCH', 'CHECK_AFTER'],
           'relate': ['CHECK_RELATE'],
           'watch': ['CHECK_WATCH', 'ASSUME_RELATED'],
           'after': ['CHECK_AFTER'],
           'reach': ['REACH'], 'overlap': ['COVER_OVERLAP'],
           'backpressure': ['CHECK_RELATE', 'CHECK_BACKPRESSURE'],
           'shared': [], 'shared_cover': []}
# Outlet state probed for the held-packet helpers.
PROBES = {'axi4x2': ['pool.count', 'r.count',
                     'w.busy', 'w.head_pending', 'w.head_beat_in', 'w.slot0.valid',
                     'w.slot1.valid', 'w.response_full', 'w.response_spare', 'r.slot0.valid',
                     'r.slot1.valid', 'r.slot0.received', 'r.slot1.received', 'r.full',
                     'r.response_last', 'r.spare', 'r.spare_last', 'w.remaining',
                     'pool.read_release', 'pool.write_release',
                     'w.done', 'w.full', 'w.reject', 'w.aw_pending', 'w.beat_drop',
                     'w.slot0.local', 'w.slot1.local', 'timeout.w_tail_pending', 'timeout.b_owed',
                     'r.slot0.issued', 'r.slot1.issued', 'r.slot0.reject', 'r.slot1.reject',
                     'r.slot0.length', 'r.slot1.length', 'r.beat', 'r.select'],
          'ahb_lite': ['w.receiving', 'w.context0.valid', 'w.context1.valid', 'w.context2.valid',
                     'w.response_full', 'r.issuing', 'r.count', 'r.inflight', 'r.metadata0.last',
                     'r.metadata1.last', 'r.issue_left', 'r.subread.started',
                     'timeout.orphan.taken', 'timeout.orphan.issuing', 'timeout.orphan.left',
                     'bus.address_phase', 'bus.write']}


def ports(work):
    result = []
    for line in (work / 'ports.tsv').read_text().splitlines():
        name, width, is_input = line.split('\t')
        result.append((name, int(width), is_input == '1'))
    return result


def header_ports(text):
    """Names in the port list of the bench's module header, in order."""
    header = re.match(r'module formal_top\(([^;]*)\);\n', text)
    assert header, 'the bench does not start with the formal_top header'
    return [re.fullmatch(r'.*?(\w+)', part.strip(), re.S).group(1)
            for part in header.group(1).split(',')]


def check_header(text):
    """A port declared twice is an error in SystemVerilog tools other than Yosys."""
    names = header_ports(text)
    repeated = sorted({name for name in names if names.count(name) > 1})
    assert not repeated, f'formal_top declares ports more than once: {", ".join(repeated)}'
    return text


def monitors(kind, n):
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
    if kind == 'ahb_lite':
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
reg q_aw = 0, q_w = 0, q_ar = 0, q_pav = 0, q_pwv = 0, q_parv = 0;
reg [11:0] q_aa, q_ara; reg [3:0] q_al, q_arl, q_ws, q_ac, q_arc; reg [2:0] q_as, q_ars, q_ap, q_arp;
reg [1:0] q_ab, q_arb; reg [31:0] q_wd; reg q_wl;
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
end
''', 2 * n + 8


def axi_credits(bus):
    tail = f'&& nl_{bus}_wlast' if bus == 'axi' else ''
    beats = f'({{1\'b0, nl_{bus}_arlen}} + 5\'d1)' if bus == 'axi' else "1'b1"
    handshakes = '\n'.join(f'wire q_{c}_take = nl_{bus}_{c}valid && nl_{bus}_{c}ready;'
                           for c in AXI_CHANNELS)
    return f'''
{handshakes}
reg [7:0] q_aw_credit = 0, q_w_credit = 0, q_r_credit = 0;
wire q_b_owed = q_aw_credit != 0 && q_w_credit != 0;
wire q_r_owed = q_r_credit != 0;
wire q_b_blocked = q_b_owed && nl_{bus}_bvalid && !nl_{bus}_bready;
wire q_r_blocked = q_r_owed && nl_{bus}_rvalid && !nl_{bus}_rready;
always @(posedge nl_clk)
  if (!nl_reset_n) begin q_aw_credit <= 0; q_w_credit <= 0; q_r_credit <= 0; end
  else begin
    q_aw_credit <= q_aw_credit + q_aw_take - q_b_take;
    q_w_credit <= q_w_credit + (q_w_take {tail}) - q_b_take;
    q_r_credit <= q_r_credit + (q_ar_take ? {beats} : 0) - q_r_take;
  end
'''


def expiry_monitor(waits, held, cycles, watch_requires_both_live=False):
    bits = (cycles - 1).bit_length()
    parts = [held, 'reg q_exp = 0;']
    reaches = []
    watch_guard = ' && !q_exp' if watch_requires_both_live else ''
    for channel, waiting in waits.items():
        suffix = f'_{channel}' if channel else ''
        reaches.append(f'q_wait{suffix} && q_count{suffix} == {cycles - 1}')
        parts.append(f'''
reg [{bits - 1}:0] q_count{suffix} = 0;
wire q_wait{suffix} = {waiting};
wire q_related{suffix} = nl_probe_timeout{suffix}_waiting == q_wait{suffix};
always @(posedge nl_clk)
  if (!nl_reset_n || !q_wait{suffix} || q_exp) q_count{suffix} <= 0;
  else q_count{suffix} <= q_count{suffix} + 1;
`ifndef CHECK_PREFIX_RELATION
always @(posedge nl_clk) if (RELATE && nl_reset_n && started && !nl_status_timed_out)
  RELATED{suffix}: assert (q_related{suffix});
`endif
`ifdef ASSUME_RELATED
always @(posedge nl_clk) if (nl_reset_n && started && !nl_status_timed_out{watch_guard}) assume (q_related{suffix});
`endif
always @(posedge nl_clk) if (WATCH && nl_reset_n && started && !q_exp)
  HELPER{suffix}: assert (nl_probe_timeout{suffix}_count == q_count{suffix});
''')
    parts.append(f'''
always @(posedge nl_clk)
  if (!nl_reset_n) q_exp <= 0;
  else if ({' || '.join(f'({condition})' for condition in reaches)}) q_exp <= 1;
''')
    return '\n'.join(parts)


def axi_waits(bus):
    predicates = {c: f'nl_{bus}_{c}valid && !nl_{bus}_{c}ready' for c in ('aw', 'w', 'ar')}
    predicates.update({c: f'q_{c}_owed && nl_{bus}_{c}ready && !nl_{bus}_{c}valid' for c in ('b', 'r')})
    return {c: f'({wait}) && !q_b_blocked && !q_r_blocked' for c, wait in predicates.items()}


AXI4_RESPONSE_HOLD = '''
// The target keeps a blocked response offered with the same payload.
reg q_b_stalled = 0, q_r_stalled = 0;
reg [1:0] q_bresp, q_rresp;
reg [31:0] q_rdata;
reg q_rlast;
always @(posedge nl_clk) begin
  q_b_stalled <= nl_reset_n && nl_axi_bvalid && !nl_axi_bready;
  q_r_stalled <= nl_reset_n && nl_axi_rvalid && !nl_axi_rready;
  q_bresp <= nl_axi_bresp;
  q_rresp <= nl_axi_rresp;
  q_rdata <= nl_axi_rdata;
  q_rlast <= nl_axi_rlast;
end
always @* if (nl_reset_n) begin
  if (q_b_stalled) assume (nl_axi_bvalid && nl_axi_bresp == q_bresp);
  if (q_r_stalled) assume (nl_axi_rvalid &&
      {nl_axi_rresp, nl_axi_rdata, nl_axi_rlast} == {q_rresp, q_rdata, q_rlast});
end
'''


def axi_reach_inputs(kind, cycles):
    if kind == 'axi4x2':
        # The first AR is accepted. One nonfinal R beat resets the R wait
        # count, so the stalled second AR is the first channel to expire.
        state = f'''
reg [1:0] requests = 0;
reg [{(cycles + 8).bit_length() - 1}:0] tick = 0;
reg first_ar = 0, ar_at_limit = 0;
always @(posedge nl_clk) begin
  tick <= tick + 1'b1;
  if (nl_reset_n && nl_rreq_valid && nl_rreq_ready) requests <= requests + 1'b1;
  if (nl_reset_n && nl_axi_arvalid && nl_axi_arready) first_ar <= 1;
  ar_at_limit <= nl_reset_n && !nl_status_timed_out &&
      nl_probe_timeout_ar_waiting && nl_probe_timeout_ar_count == {cycles - 1};
end
'''
        return state, {'nl_rreq_valid': 'nl_reset_n && requests < 2',
                       'nl_rreq_source': '1', 'nl_rreq_length': '1',
                       'nl_axi_arready': '!first_ar',
                       'nl_axi_rvalid': f'first_ar && tick == {min(16, cycles // 2)}'}
    return '''
reg request_sent = 0;
always @(posedge nl_clk)
  if (nl_rreq_valid && nl_rreq_ready) request_sent <= 1;
''', {'nl_rreq_valid': 'nl_reset_n && !request_sent'}


def axi_reach_harness(kind, port_list, cycles, text):
    state, traffic = axi_reach_inputs(kind, cycles)
    values = {'nl_reset_n': '!initial_cycle', 'nl_rrsp_ready': '1', 'nl_wrsp_ready': '1',
              'nl_rreq_address': "32'h1000", 'nl_rreq_start_address': "32'h1000",
              'nl_rreq_size': '2', 'nl_rreq_burst': '1',
              'nl_rreq_first': '1', 'nl_rreq_last': '1', **traffic}
    declarations = [f"wire [{width - 1}:0] {name} = {values.get(name, '0')};"
                    for name, width, incoming in port_list if incoming and name != 'nl_clk']
    _, separator, body = text.partition(');\n')
    return f"module formal_top(input wire nl_clk{separator}" + state + '\n'.join(declarations) + '\n' + body


def expiry_stimulus(kind, stall):
    return '' if kind.startswith('axi4') else f'always @* assume ({stall});'


def axi4x2_held():
    return '''reg [3:0] q_wleft = 0; reg [2:0] q_trw = 0, q_trr = 0;
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
wire q_held_wleft = nl_probe_w_head_pending || q_wleft == nl_probe_w_remaining;
wire q_held_pool =
  q_trw + q_trr + nl_probe_w_head_pending + nl_probe_w_head_beat_in == q_pool;
wire q_pool_held = q_pool == q_held;
wire q_pool_received = q_received == (nl_probe_r_full && nl_probe_r_response_last) +
                                     (nl_probe_r_spare && nl_probe_r_spare_last);
`ifndef CHECK_PREFIX_RELATION
always @(posedge nl_clk) if ((RELATE || AFTER) && nl_reset_n && started) begin
  HELD_WLEFT: assert (q_held_wleft);
  if (!nl_status_timed_out) HELD_POOL: assert (q_held_pool);
  POOL_HELD: assert (q_pool_held);
  POOL_RECEIVED: assert (q_pool_received);
end
`endif'''


def axi4x2_relation():
    return '''
`ifdef CHECK_PREFIX_RELATION
// A stalled W counts unless an owed B or R is offered and the outlet cannot
// take it. One burst keeps these obligations together. With two bursts, a
// head can take W before AW, or move into a B slot before its last W leaves.
// The credits from the port handshakes must equal the debts in both slots
// and the split head. An early AW counts alone. A staged tail has no W credit.
wire live_w0 = nl_probe_w_slot0_valid && !nl_probe_w_slot0_local;
wire live_w1 = nl_probe_w_slot1_valid && !nl_probe_w_slot1_local;
wire [2:0] wslots = {2'b0, live_w0} + {2'b0, live_w1};
wire aw_main = nl_probe_w_busy && !nl_probe_w_reject && !nl_probe_w_aw_pending;
wire w_main = nl_probe_w_busy && !nl_probe_w_reject && nl_probe_w_done &&
              (!nl_probe_w_full || nl_probe_w_head_beat_in);
wire early_w = nl_probe_w_head_beat_in && !nl_probe_w_full && !nl_probe_w_beat_drop &&
               nl_wreq_length == 0;
wire credit_aw = q_aw_credit == wslots + aw_main;
wire credit_w = q_w_credit == wslots + w_main + early_w - nl_probe_timeout_w_tail_pending;
wire credit_join = q_b_owed == nl_probe_timeout_b_owed;
// An issued read owes its full length until beats leave the target. The
// current burst alone can have partially retired beats. Local reads owe none.
wire live_r0 = nl_probe_r_slot0_valid && nl_probe_r_slot0_issued &&
               !nl_probe_r_slot0_received && !nl_probe_r_slot0_reject;
wire live_r1 = nl_probe_r_slot1_valid && nl_probe_r_slot1_issued &&
               !nl_probe_r_slot1_received && !nl_probe_r_slot1_reject;
wire [7:0] r0 = live_r0 ? (8'd1 + nl_probe_r_slot0_length) : 0;
wire [7:0] r1 = live_r1 ? (8'd1 + nl_probe_r_slot1_length) : 0;
wire [7:0] r_used = (nl_probe_r_select ? live_r1 : live_r0) ? nl_probe_r_beat : 0;
wire credit_r = q_r_credit == r0 + r1 - r_used;
// Prove the credit identities, wait predicates and pool invariants in one
// induction target. The solver can use the entire relation for every clause.
always @(posedge nl_clk) if (nl_reset_n && started)
  RELATED_ALL: assert (q_related_aw && q_related_w && q_related_ar && q_related_b && q_related_r &&
    q_held_wleft && q_held_pool && q_pool_held && q_pool_received &&
    credit_aw && credit_w && credit_r && credit_join);
`endif
'''


AXI4X2_OVERLAP = '''
`ifdef COVER_OVERLAP
// Withhold the network read response while presenting beats from one read.
// A separate one-beat write waits at W once the target owes the read.
reg sent_read = 0, sent_write = 0;
always @(posedge nl_clk) if (nl_reset_n) begin
  if (nl_rreq_valid && nl_rreq_ready) sent_read <= 1;
  if (nl_wreq_valid && nl_wreq_ready) sent_write <= 1;
end
always @* if (nl_reset_n) begin
  assume(nl_wrsp_ready && !nl_rrsp_ready);
  assume(nl_axi_arready && nl_axi_awready && !nl_axi_wready && !nl_axi_bvalid);
  assume(nl_axi_rvalid == q_r_owed && nl_axi_rlast == (q_r_credit == 1));
  assume(nl_axi_rresp == 0 && nl_axi_rdata == 0);
  assume(nl_rreq_valid == !sent_read && nl_wreq_valid == (q_r_owed && !sent_write));
  assume(nl_rreq_address == 32'h1000 && nl_rreq_start_address == 32'h1000 &&
         nl_rreq_length == 7 && nl_rreq_size == 2 && nl_rreq_burst == 1 &&
         nl_rreq_source == 1 && !nl_rreq_write && !nl_rreq_error && nl_rreq_first && nl_rreq_last);
  assume(nl_wreq_address == 32'h1000 && nl_wreq_start_address == 32'h1000 &&
         nl_wreq_length == 0 && nl_wreq_size == 2 && nl_wreq_burst == 1 &&
         nl_wreq_source == 0 && nl_wreq_write && !nl_wreq_error && nl_wreq_first && nl_wreq_last);
end
always @(posedge nl_clk) if (nl_reset_n && started && !nl_status_timed_out)
  COVER_BLOCKED_W: cover (nl_probe_pool_count == 2 && nl_axi_wvalid && !nl_axi_wready &&
                         (q_b_blocked || q_r_blocked));
`endif
'''


def harness(kind, port_list, answer, n):
    extra, bound = monitors(kind, n)
    conns = [f'.{name}({name})' for name, _, _ in port_list]
    axi4 = kind in ('axi4', 'axi4x2')
    tied = () if axi4 else ('nl_rrsp_ready', 'nl_wrsp_ready')
    ties = '\n'.join(f"assign {name} = 1'b1;" for name in tied)
    # The clock is the header's first port.
    inputs = [f'input wire [{w - 1}:0] {n}' for n, w, i in port_list
              if i and n not in tied and n != 'nl_clk']
    wires = [f'wire [{w - 1}:0] {n};' for n, w, i in port_list if not i or n in tied]
    stall = {'apb4': '!nl_apb_pready', 'apb3': '!nl_apb_pready', 'ahb_lite': '!nl_ahb_hreadyout',
             'axi4_lite': '!nl_axil_bvalid && !nl_axil_rvalid',
             'axi4': '!nl_axi_bvalid && !nl_axi_rvalid',
             'axi4x2': '!nl_axi_bvalid && !nl_axi_rvalid'}[kind]
    # The held-packet invariants cover the engines with overlapping packet state.
    if kind == 'ahb_lite':
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
end'''
    elif kind == 'axi4x2':
        # Two bursts in flight: a write from its head (the first beat taken
        # with no beats left, framed by AxLEN as the outlet frames it) until
        # its B is taken, a read from its request until its last beat is
        # taken. Until expiry they are the pool; at every cycle the pool is
        # the bursts the outlet holds, and the reads received are the last
        # beats staged.
        held = axi4x2_held()
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
    if kind.startswith('apb'):
        waits = {'': 'nl_apb_psel && nl_apb_penable && !nl_apb_pready'}
    elif kind == 'ahb_lite':
        waits = {'': '(q_dp || (nl_ahb_hsel && nl_ahb_htrans[1])) && !nl_ahb_hreadyout'}
    else:
        bus = 'axil' if kind == 'axi4_lite' else 'axi'
        waits = axi_waits(bus)
        held += axi_credits(bus) + f'''
`ifdef ABSTRACT_TIMER_COUNT
always @* if (nl_reset_n && !nl_status_timed_out) begin
`else
always @* if (nl_reset_n && !q_exp) begin
`endif
  if (nl_{bus}_bvalid) assume (q_b_owed);
  if (nl_{bus}_rvalid) assume (q_r_owed);
end
'''
        if axi4:
            held += AXI4_RESPONSE_HOLD
    expiry = expiry_monitor(waits, held, n, watch_requires_both_live=axi4)
    overlap = ''
    if kind == 'axi4x2':
        expiry += axi4x2_relation()
        overlap = AXI4X2_OVERLAP
    elif kind == 'axi4':
        overlap = '''
`ifdef CHECK_BACKPRESSURE
// With one entry, an owed blocked response cannot coexist with a W wait.
always @(posedge nl_clk) if (nl_reset_n && started && !nl_status_timed_out)
  NO_BLOCKED_W: assert (!(nl_axi_wvalid && !nl_axi_wready && (q_b_blocked || q_r_blocked)));
`endif
'''
    read_bound = ''
    if bound and answer == 'completion':
        # Cycles after expiry since the read burst was accepted or last
        # produced a beat. AXI4 with the pooled outlet (one burst in flight
        # across both directions): a held write blocks a new read's
        # acceptance until its response has left, so the deadline pauses
        # while a write burst is in flight at the outlet (q_trw). A read
        # response already offered also resets the deadline even if blocked.
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
    reached = ('nl_status_timed_out && ar_at_limit && nl_probe_pool_count == 2'
               if kind == 'axi4x2' else 'nl_status_timed_out')
    return f'''module formal_top(input wire nl_clk, {', '.join(inputs)});
{groups}
{chr(10).join(wires)}
nocloom_{'axi4_outlet' if kind in ('axi4', 'axi4x2') else 'apb_burst_outlet' if kind.startswith('apb') else
          'axi_lite_outlet' if kind == 'axi4_lite' else 'ahb_outlet'}_probe dut({', '.join(conns)});
{ties}
reg initial_cycle = 1, started = 0, q_failed = 0;
always @(posedge nl_clk) begin initial_cycle <= 0; started <= 1; q_failed <= nl_reset_n && nl_status_timed_out; end
// The common reset is applied once at the start and then released.
always @* if (initial_cycle) assume (!nl_reset_n); else assume (nl_reset_n);
`ifdef REACH
// Reachability: with a target that never completes a transfer, this
// assertion must fail, with a trace to expiry.
{expiry_stimulus(kind, stall)}
always @(posedge nl_clk) if (nl_reset_n) REACH: assert (!({reached}));
`else
{extra}
{expiry}
{overlap}
{read_bound}
{silent}
always @(posedge nl_clk) if (WATCH && nl_reset_n && started) begin
  EXPIRY: assert (nl_status_timed_out == q_exp);
  if (q_failed) STICKY: assert (nl_status_timed_out);
end
`endif
endmodule
'''


def shared_harness(port_list, cycles):
    """Drive a target that can issue either B or R on its shared response path."""
    input_values = {
        'nl_reset_n': 'started',
        'nl_wreq_valid': 'q_writes < (hold_read ? 1 : 3) && (!hold_read || q_r_owed)',
        'nl_wreq_write': '1', 'nl_wreq_first': '1', 'nl_wreq_last': '1', 'nl_wreq_be': "4'hf",
        'nl_rreq_valid': '!q_sent_read && (hold_read || q_writes == 3)',
        'nl_rreq_length': "hold_read ? 4'd15 : 4'd0",
        'nl_rreq_first': '1', 'nl_rreq_last': '1', 'nl_rreq_source': '1',
        'nl_axi_awready': '1', 'nl_axi_wready': '1', 'nl_axi_arready': '1',
        'nl_axi_bvalid': 'q_b_owed && (hold_read ? !q_r_owed : q_start_response)',
        'nl_axi_rvalid': 'q_r_owed && (hold_read ? q_start_response : !q_b_owed)',
        'nl_axi_rlast': 'q_r_credit == 1',
        'nl_wrsp_ready': 'hold_read || q_release',
        'nl_rrsp_ready': '!hold_read || q_release',
    }
    for direction in ('w', 'r'):
        input_values[f'nl_{direction}req_size'] = '2'
        input_values[f'nl_{direction}req_burst'] = '1'
        for field in ('address', 'start_address'):
            input_values[f'nl_{direction}req_{field}'] = "32'h1000"
    wires = '\n'.join(f'wire [{width - 1}:0] {name}'
                      + (f' = {input_values.get(name, "0")};' if incoming else ';')
                      for name, width, incoming in port_list if name != 'nl_clk')
    conns = ', '.join(f'.{name}({name})' for name, _, _ in port_list)
    return f'''module formal_top(input wire nl_clk);
(* anyconst *) reg hold_read;
reg started = 0, q_sent_read = 0, q_release = 0;
reg q_start_response = 0, q_wait_before_block = 0;
reg [2:0] q_writes = 0, q_b_received = 0;
reg [5:0] q_r_received = 0;
reg [{cycles.bit_length()}:0] q_stall_age = 0;
{wires}
nocloom_axi4_outlet_probe dut({conns});
{axi_credits('axi')}
wire q_missing_b = q_b_owed && nl_axi_bready && !nl_axi_bvalid;
wire q_missing_r = q_r_owed && nl_axi_rready && !nl_axi_rvalid;
wire q_delay_b = q_r_blocked && q_missing_b;
wire q_delay_r = q_b_blocked && q_missing_r;
wire q_countable = (hold_read ? q_missing_b : q_missing_r) && !q_b_blocked && !q_r_blocked;
always @(posedge nl_clk) begin
  started <= 1;
  if (started) begin
    // The target begins the first response after it owes the other one.
    // The other channel must have counted before backpressure clears it.
    if (hold_read ? q_b_owed : q_r_owed) q_start_response <= 1;
    if ((q_delay_b || q_delay_r) && $past(q_countable)) q_wait_before_block <= 1;
    if (nl_wreq_valid && nl_wreq_ready) q_writes <= q_writes + 1;
    if (nl_rreq_valid && nl_rreq_ready) q_sent_read <= 1;
    if (nl_wrsp_valid && nl_wrsp_ready) q_b_received <= q_b_received + 1;
    if (nl_rrsp_valid && nl_rrsp_ready) q_r_received <= q_r_received + 1;
    q_stall_age <= (hold_read ? q_delay_b : q_delay_r) ? q_stall_age + 1 : 0;
    if (q_stall_age == {cycles}) q_release <= 1;
    SHARED_RESPONSE: assert (!nl_status_timed_out);
    if (q_delay_b && $past(q_countable)) COUNTED_B: assert (nl_probe_timeout_b_count != 0);
    if (q_delay_r && $past(q_countable)) COUNTED_R: assert (nl_probe_timeout_r_count != 0);
    if ($past(q_delay_b)) RESET_B: assert (nl_probe_timeout_b_count == 0);
    if ($past(q_delay_r)) RESET_R: assert (nl_probe_timeout_r_count == 0);
    B_BEHIND_R: cover (hold_read && q_wait_before_block && q_release &&
                       q_b_received == 1 && q_r_received == 16);
    R_BEHIND_B: cover (!hold_read && q_wait_before_block && q_release &&
                       q_b_received == 3 && q_r_received == 1);
  end
end
endmodule
'''


def remove_w_backpressure(rtl):
    """Remove only the response-block gate from the W wait expression."""
    assignments = dict(re.findall(r'^assign (\w+) = ([^;]+);$', rtl, re.M))
    wait = assignments['nl_probe_timeout_w_waiting']
    before_expiry, live = re.fullmatch(r'\((\w+) & (\w+)\)', assignments[wait]).groups()
    raw_wait, _ = re.fullmatch(r'\((\w+) & (\w+)\)', assignments[before_expiry]).groups()
    original = f'assign {wait} = {assignments[wait]};'
    assert rtl.count(original) == 1
    return rtl.replace(original, f'assign {wait} = ({raw_wait} & {live});')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--kind', required=True, choices=['apb4', 'apb3', 'axi4_lite', 'ahb_lite', 'axi4'])
    parser.add_argument('--depth', type=int, default=48)
    parser.add_argument('--cycles', type=int, default=CYCLES)
    parser.add_argument('--mode', choices=['prove', 'bmc'], default='prove')
    parser.add_argument('--timeout', type=int, default=2400, help='seconds per formal task')
    # The reach task needs a trace to expiry (1030 frames). The solver does not
    # reach that depth on the queue-structured Lite engine within the test
    # timeout, so its registered check runs the safety tasks only and the
    # expiry's reachability is witnessed by verif_target_timeouts_negative.
    parser.add_argument('--tasks', default='')
    # Bursts in flight of the AXI4 outlet: 1, or 2 (fixture kind axi4x2).
    parser.add_argument('--capacities', default='1')
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error('timeout must be positive')
    default_tasks = 'relate,watch,after,reach'
    if args.kind == 'axi4' and args.mode == 'prove':
        default_tasks += ',overlap,backpressure'
    tasks = (args.tasks or default_tasks).split(',')
    if any(task not in DEFINES for task in tasks):
        parser.error(f'tasks are among {", ".join(DEFINES)}')
    # The watch group assumes RELATED, so it only counts with RELATED proved
    # in the same run.
    if 'watch' in tasks and 'relate' not in tasks:
        parser.error('watch needs relate')
    shared = any(task.startswith('shared') for task in tasks)
    if shared and (args.kind != 'axi4' or any(not task.startswith('shared') for task in tasks)):
        parser.error('shared response tasks run separately on AXI4')
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    capacities = [int(c) for c in args.capacities.split(',')]
    if any(c not in (1, 2) for c in capacities) or (capacities != [1] and args.kind != 'axi4'):
        parser.error('capacity 2 applies to the axi4 kind only')
    if {'overlap', 'backpressure'} & set(tasks) and args.kind != 'axi4':
        parser.error('the backpressure checks apply to AXI4 only')
    if tasks == ['overlap'] and 2 not in capacities:
        parser.error('the overlap cover requires capacity 2')
    results = []
    for capacity, answer in [(c, a) for c in capacities for a in ANSWERS]:
        run_tasks = [task for task in tasks if task != 'overlap' or capacity == 2]
        if not run_tasks:
            continue
        kind = 'axi4x4' if shared else 'axi4x2' if capacity == 2 else args.kind
        work = Path(tempfile.mkdtemp(prefix=f'run-{kind}-{answer}-', dir=base))
        (base / f'latest-{kind}-{answer}.txt').write_text(str(work) + '\n')
        subprocess.run([args.fixture, str(work), kind, str(args.cycles), answer,
                        *PROBES.get(kind, ())], check=True, timeout=120)
        port_list = ports(work)
        top = check_header(shared_harness(port_list, args.cycles) if shared else
                           harness(kind, port_list, answer, args.cycles))
        (work / 'formal_top.sv').write_text(top)
        directed_reach = kind in ('axi4', 'axi4x2') and 'reach' in run_tasks
        if directed_reach:
            (work / 'reach.sv').write_text(
                check_header(axi_reach_harness(kind, port_list, args.cycles, top)))
        if 'backpressure' in run_tasks:
            (work / 'backpressure.v').write_text(remove_w_backpressure((work / 'outlet.v').read_text()))
        # The bounded mode uses ABC's BMC to the same depth: on the AXI4-Lite
        # outlet it takes seconds where smtbmc takes minutes.
        engine = ('smtbmc z3' if shared else 'abc pdr' if args.mode == 'prove' else 'abc bmc3')
        safety = [task for task in run_tasks if task != 'reach']
        sby = '[tasks]\n' + '\n'.join(run_tasks) + '\n\n[options]\n'
        prefix = kind == 'axi4x2' and args.mode == 'prove'
        exact_axi_watch = kind in ('axi4', 'axi4x2') and args.mode == 'prove'
        modes = {'shared': 'bmc', 'shared_cover': 'cover', 'overlap': 'cover',
                 'backpressure': 'bmc' if capacity == 2 else 'prove'}
        for task in safety:
            mode = modes.get(task, args.mode)
            depth = 3 if exact_axi_watch and task == 'watch' else args.depth
            if task in ('overlap', 'backpressure'):
                depth = 12
            sby += f'{task}: mode {mode}\n{task}: depth {depth}\n'
            if (task == 'backpressure' or (mode == 'bmc' and not shared)
                    or (mode == 'prove' and not (exact_axi_watch and task == 'watch'))):
                sby += f'{task}: aigsmt z3\n'
        if 'reach' in run_tasks:
            sby += 'reach: vcd off\n'
            if directed_reach:
                sby += f'reach: mode bmc\nreach: depth {args.cycles + 8}\n'
            else:
                sby += 'reach: mode prove\n'
        sby += f'timeout {args.timeout}\n\n[engines]\n'
        for task in safety:
            selected_engine = engine
            if prefix and task == 'relate':
                # Cancel the engine's per-output schedule and generalize
                # clauses for the joint credit and wait invariant.
                selected_engine = 'abc pdr -l -r -c -n'
            elif exact_axi_watch and task == 'watch':
                # Once the waits agree, each counter's next value follows
                # from its current value. Prove that by temporal induction.
                selected_engine = 'smtbmc z3'
            elif task == 'overlap':
                selected_engine = 'smtbmc z3'
            elif task == 'backpressure':
                selected_engine = 'abc bmc3' if capacity == 2 else 'abc pdr -l -r -c -n'
            sby += f'{task}: {selected_engine}\n'
        if 'reach' in run_tasks:
            sby += f'reach: abc {"bmc3" if directed_reach else "pdr"}\n'
        sby += '\n[script]\n'
        for task in run_tasks:
            before_expiry = (task == 'relate' and prefix) or (task == 'backpressure' and capacity == 2)
            abstract_count = task == 'after' and args.mode == 'prove' and 'watch' in run_tasks
            names = DEFINES[task] + (['ABSTRACT_TIMER_COUNT'] if abstract_count or before_expiry else [])
            if before_expiry:
                names += ['CHECK_PREFIX_RELATION']
            defines = ' '.join(f'-D{name}' for name in names)
            rtl = 'backpressure.v' if task == 'backpressure' else 'outlet.v'
            bench = 'reach.sv' if task == 'reach' and directed_reach else 'formal_top.sv'
            sby += f'{task}: read_verilog -formal {rtl}\n'
            sby += f'{task}: read_verilog -formal -sv {defines} {bench}\n'
            if before_expiry:
                sby += (f'{task}: prep -top formal_top -flatten\n{task}: cd formal_top\n'
                        f"{task}: connect -set nl_status_timed_out 1'b0\n{task}: cd ..\n"
                        f'{task}: opt -full\n')
            elif abstract_count:
                # Every timer count of the outlet: one per AXI channel, one
                # for the APB and AHB outlets. A count left uncut would still
                # pass, only slower, so the selection must name all of them.
                counts = 'w:dut.nl_probe_timeout_count w:dut.nl_probe_timeout_*_count'
                expected = len(AXI_CHANNELS) if kind.startswith('axi4') else 1
                sby += 'after: prep -top formal_top -flatten\n'
                sby += (f'after: select -assert-count {expected} {counts}\n'
                        f'after: cutpoint {counts}\nafter: opt_clean\n')
            else:
                sby += f'{task}: prep -top formal_top\n'
        sby += f'''

[files]
{work / 'outlet.v'}
{work / 'formal_top.sv'}
'''
        if directed_reach:
            sby += f"{work / 'reach.sv'}\n"
        if 'backpressure' in run_tasks:
            sby += f"{work / 'backpressure.v'}\n"
        (work / 'timeout.sby').write_text(sby)
        started = time.monotonic()
        proc = subprocess.run(['sby', 'timeout.sby'], cwd=work, capture_output=True,
                              text=True, timeout=args.timeout + 30, check=False)
        seconds = round(time.monotonic() - started, 1)
        (work / 'sby.log').write_text(proc.stdout + proc.stderr)
        status = {}
        for task in run_tasks:
            path = work / f'timeout_{task}' / 'status'
            status[task] = path.read_text().split()[0] if path.exists() else 'MISSING'
        results.append({'capacity': 4 if shared else capacity, 'answer': answer, 'status': status,
                        'seconds': seconds})
        (base / 'status.json').write_text(json.dumps({'kind': args.kind, 'mode': args.mode,
                                                      'depth': args.depth,
                                                      'results': results}) + '\n')
        expected = {task: 'FAIL' if task == 'reach' else 'PASS' for task in run_tasks}
        if 'backpressure' in run_tasks and capacity == 2:
            expected['backpressure'] = 'FAIL'
            assert re.search(r'(?:failed assertion formal_top\.|Assert failed in formal_top: )RELATED_ALL\b',
                             (work / 'timeout_backpressure' / 'logfile.txt').read_text()), proc.stdout[-3000:]
        assert status == expected, \
            (answer, status, proc.stdout[-3000:])
        print(f'FORMAL_PASS target timeout {kind} {answer} cycles={args.cycles} '
              f'seconds={seconds}')


if __name__ == '__main__':
    shutil.which('sby') or exit('sby not found')
    main()
