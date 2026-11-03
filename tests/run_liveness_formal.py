#!/usr/bin/env python3
"""Bounded liveness of small networks with SymbiYosys: every accepted request answers within a bound.

Each case generates a network through the CLI, closes it with the source and target models below and
checks that while anything is outstanding some handshake happens within N cycles, under the fairness
assumptions F1..F4. Sources issue a bounded number of requests of known length, so handshakes are
finite and this bounds every request's answer. A source hung while another still runs shows once the
other drains. A BMC to depth D says nothing past D, and data is not checked: those stay with the
transaction benches.

Assumptions, each with three checks because a wrong assumption still lets a proof pass (F1..F4):
  remove     without it the network must answer with a counterexample: the assumption is needed;
  cover      the state it could wrongly remove is reachable with it: it is not too strong;
  overstrong under a stronger form that same cover must fail: the covers can see a strong one.

F1 sources raise RREADY and BREADY within P cycles. F2 W within P cycles of its AW. F3 targets raise
ARREADY, AWREADY, WREADY within P cycles while they have room. F4 a target with open reads offers a
word within Q cycles, and each open read that may go is chosen within Q picks (order otherwise free).
The strong form of F4 serves one request at a time.

Known hang RTL: a fault removes the segment end from every response grant, which brings back two
outputs waiting for each other, in the crossed single-ID shapes too.

Weak by depth, as their names say: L4 (clock gates, 12 cycles) and L8 (three targets, 18 cycles) stop
before most traffic can finish. L4 does not cover merged-read clock wake loss: that class of hang
relies on the bench negative control in tests/verif/cases/merged_tail_gating.py.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from endpoint_ports import endpoint_ports  # noqa: E402
from async_fifo_formal import cover_locations  # noqa: E402

P, Q, PICKS = 1, 1, 2  # F1..F3 within P cycles, F4 within Q cycles and PICKS picks
# The requests one source issues: a BMC is an under-approximation in any case, and a hang needs few.
READS, WRITES = 2, 1


def shape(name, sources, targets, topology=None, gating=False, wrap=0, reads=READS, length=0, slow=P,
          history=False):
    cfg = {'schema_version': 10, 'name': 'live', 'address_width': 32,
           'arbitration': {'policy': 'round_robin'},
           'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
           'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                              'test_port': 'test'}],
           'initiators': [dict({'name': f's{i}', 'clock_domain': 'main', 'protocol': 'axi4',
                                'data_width': s['w'], 'max_outstanding': s['o']},
                               **({'access': s['access']} if 'access' in s else {}))
                          for i, s in enumerate(sources)],
           'targets': [{'name': f't{i}', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': t['w'],
                        'attributes': 'amba', 'max_outstanding': t['k'],
                        'mappings': [{'base': hex(0x1000 * i), 'size': '0x1000'}]}
                       for i, t in enumerate(targets)],
           'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if topology:
        cfg['topology'] = topology
    if not gating:
        cfg['clock_gating'] = 'none'
    cfg = endpoint_ports(cfg, id_width=1, len_width=4)
    for c, s in zip(cfg['initiators'], sources):
        c['id_width'] = s.get('id', 1)
        c['len_width'] = s.get('len', 4)
        if 'qos' in s:
            c['qos'] = s['qos']
    return dict(name=name, config=cfg, sources=sources, targets=targets, gating=gating, wrap=wrap,
                reads=reads, length=length, slow=slow, history=history)


def S(w=32, o=2, **k):
    return dict(w=w, o=o, **k)


def T(w=32, k=2):
    return dict(w=w, k=k)


MULTI_HOP = {
    'request': {'nodes': [{'name': 'q'}], 'links': [],
                'entries': [{'endpoint': 's0', 'node': 'q'}, {'endpoint': 's1', 'node': 'q'}],
                'exits': [{'endpoint': 't0', 'node': 'q'}, {'endpoint': 't1', 'node': 'q'}]},
    'response': {'nodes': [{'name': x} for x in ('a', 'c', 'd0', 'd1')],
                 'links': [{'from': f, 'to': t} for f in ('a', 'c') for t in ('d0', 'd1')],
                 'entries': [{'endpoint': 't0', 'node': 'a'}, {'endpoint': 't1', 'node': 'c'}],
                 'exits': [{'endpoint': 's0', 'node': 'd0'}, {'endpoint': 's1', 'node': 'd1'}]}}
SHAPES = {s['name']: s for s in [
    shape('L1_plain', [S(), S()], [T(), T()]),
    shape('L2_read_write', [S(w=64), S(w=64)], [T(), T()]),
    shape('L3_one_burst', [S(w=64), S(w=64)], [T(k=1), T(k=1)]),
    shape('L4_gated_merge', [S(access='read_only', len=8)], [T(w=256), T()], gating=True, reads=3, length=16, slow=4),
    shape('L5_long_wrap', [S(w=64, o=1, access='read_only', len=8)], [T(k=1)], wrap=16),
    shape('L6_single_id', [S(w=64, id=0), S(w=64, id=0)], [T(), T()], history=True),
    shape('L6_multi_hop', [S(w=64, id=0), S(w=64, id=0)], [T(), T()], topology=MULTI_HOP),
    shape('L7_single_answer', [S(w=64, id=0, access='read_only'), S(w=64, id=0, access='read_only')],
          [T(), T()]),
    # Four beats make two words of a 64-bit target: one word per read has nothing to alternate with.
    shape('L9_merged_reads', [S(), S()], [T(w=64), T(w=64)], length=4),
    shape('L8_upgrade', [S(o=2, qos=dict(shaper=dict(bytes_per_1024_cycles=4096, burst_bytes=16),
          scheduler=dict(under_level=3, over_level=0, threshold_percent=0)))],
          [T()], reads=3, length=8),
    shape('L8_merged_contexts', [S(w=64), S(w=64)], [T(k=1), T(w=64), T(w=64)]),
]}


def source_env(i, ports, bases, spans, wrap, reads, length, slow):
    n = f'nl_i_s{i}_'
    idw = ports.get(n + 'arid', 0)
    read_only = n + 'awvalid' not in ports
    t_sel = max(1, len(bases) - 1)
    target_bits = 1 if len(bases) <= 2 else 2
    base_mux = lambda sel: ' : '.join(f"({sel} == {k}) ? 32'h{b:x}" for k, b in enumerate(bases)) + " : 32'h0"
    id_ar = f"assign {n}arid = ar_id{i};\n" if idw else ''
    id_aw = f"assign {n}awid = aw_id{i};\n" if idw and not read_only else ''
    id_decl = f"reg [{max(idw, 1) - 1}:0] ar_id{i} = 0, aw_id{i} = 0; (* anyseq *) wire [{max(idw, 1) - 1}:0] new_id{i};\n"
    text = f"""
// Source {i}: AR and AW hold while valid; writes of one beat; bounded ready and W under F1, F2.
(* anyseq *) wire new_ar{i}, new_aw{i}, want_w{i}, want_r{i}, want_b{i};
(* anyseq *) wire [{target_bits - 1}:0] ar_t{i}_new, aw_t{i}_new;
(* anyseq *) wire ar_len{i}_new;
{id_decl}reg ar_v{i} = 0, aw_v{i} = 0;
reg [{target_bits - 1}:0] ar_t{i} = 0, aw_t{i} = 0;
reg ar_len{i} = 0;
reg [2:0] w_owed{i} = 0;
reg w_v{i} = 0;
reg [2:0] r_wait{i} = 0, b_wait{i} = 0, w_wait{i} = 0;
always @* assume (ar_t{i}_new < {len(bases)} && aw_t{i}_new < {len(bases)});
reg [2:0] ar_sent{i} = 0, aw_sent{i} = 0;
wire ar_go{i} = reset_n && !ar_v{i} && new_ar{i} && ar_sent{i} < {reads};
wire aw_go{i} = reset_n && !aw_v{i} && new_aw{i} && aw_sent{i} < {WRITES};
always @(posedge clk_main) begin
  if (!reset_n) begin ar_sent{i} <= 0; aw_sent{i} <= 0; end
  else begin
    if (ar_go{i}) ar_sent{i} <= ar_sent{i} + 1;
    if (aw_go{i}) aw_sent{i} <= aw_sent{i} + 1;
  end
end
assign {n}arvalid = ar_v{i};
assign {n}araddr = {base_mux(f'ar_t{i}')};
assign {n}arlen = {f"8'd{(wrap or length) - 1}" if wrap or length else "{7'd0, ar_len" + str(i) + "}"};
assign {n}arsize = {spans};
assign {n}arburst = {"2'b10" if wrap else "2'b01"};
assign {n}arlock = 1'b0;
assign {n}arcache = 4'd3;
assign {n}arprot = 3'd0;
{id_ar}`ifdef NO_F1
assign {n}rready = want_r{i};
assign {n}bready = want_b{i};
`elsif F1_STRONG
assign {n}rready = 1'b1;
assign {n}bready = 1'b1;
`else
assign {n}rready = want_r{i} || r_wait{i} == {slow};
assign {n}bready = want_b{i} || b_wait{i} == {P};
`endif
wire r_block{i} = {n}rvalid && !{n}rready;
always @(posedge clk_main) begin
  if (!reset_n) begin
    ar_v{i} <= 0; r_wait{i} <= 0; b_wait{i} <= 0;
  end else begin
    if (ar_go{i}) begin ar_v{i} <= 1; ar_t{i} <= ar_t{i}_new; ar_len{i} <= ar_len{i}_new; ar_id{i} <= new_id{i}; end
    else if ({n}arready) ar_v{i} <= 0;
    r_wait{i} <= {n}rready ? 0 : r_wait{i} + 1;
    b_wait{i} <= {n}bready ? 0 : b_wait{i} + 1;
  end
end
"""
    if read_only:
        return f"wire unused_bready{i};\n" + text.replace(f'{n}bready', f'unused_bready{i}')
    text += f"""
assign {n}awvalid = aw_v{i};
assign {n}awaddr = {base_mux(f'aw_t{i}')};
assign {n}awlen = 8'd0;
assign {n}awsize = {spans};
assign {n}awburst = 2'b01;
assign {n}awlock = 1'b0;
assign {n}awcache = 4'd3;
assign {n}awprot = 3'd0;
{id_aw}assign {n}wvalid = w_v{i};
assign {n}wdata = 0;
assign {n}wstrb = ~0;
assign {n}wlast = 1'b1;
always @(posedge clk_main) if (!reset_n) begin
  aw_v{i} <= 0; w_owed{i} <= 0; w_v{i} <= 0; w_wait{i} <= 0;
end else begin
  if (aw_go{i}) begin aw_v{i} <= 1; aw_t{i} <= aw_t{i}_new; aw_id{i} <= new_id{i}; end
  else if ({n}awready) aw_v{i} <= 0;
  w_owed{i} <= w_owed{i} + ({n}awvalid && {n}awready) - ({n}wvalid && {n}wready);
  if (w_v{i} && {n}wready) w_v{i} <= 0;
  else if (!w_v{i} && w_owed{i} != 0 && (want_w{i} ||
`ifdef NO_F2
    1'b0
`else
    w_wait{i} == {P}
`endif
  )) w_v{i} <= 1;
`ifdef F2_STRONG
  if ({n}awvalid && {n}awready) w_v{i} <= 1;
`endif
  w_wait{i} <= (w_owed{i} != 0 && !w_v{i}) ? w_wait{i} + 1 : 0;
end
"""
    return text


def target_env(j, ports, k):
    n = f'nl_t_t{j}_'
    idw = ports[n + 'arid'] if n + 'arid' in ports else 0
    data = ports[n + 'rdata']
    # A load comes from ARLEN and a decrement cannot underflow. Its upper
    # zero bits stay zero, including when both updates select the same row.
    length_bits = min(8, ports[n + 'arlen'])
    entries = max(k, 1)
    rows = range(entries)
    text = f"""
// Target {j}: at most {entries} open reads; words of any open read that is the oldest of its ID.
(* anyseq *) wire want_ar{j}, want_aw{j}, want_w_t{j}, want_rv{j}, want_bv{j};
(* anyseq *) wire [{max(1, entries - 1).bit_length() - 1}:0] pick{j};
reg [{entries - 1}:0] open{j} = 0;
reg [{max(idw, 1) - 1}:0] rid{j} [0:{entries - 1}];
reg [{length_bits - 1}:0] left{j} [0:{entries - 1}];
reg [{(entries).bit_length()}:0] born{j} [0:{entries - 1}];
reg [2:0] passed{j} [0:{entries - 1}];
reg [{(entries).bit_length()}:0] clock{j} = 0;
reg rv{j} = 0, bv{j} = 0;
reg [{max(1, entries - 1).bit_length() - 1}:0] cur{j} = 0;
reg [2:0] rv_wait{j} = 0, bv_wait{j} = 0, ar_wait{j} = 0, aw_wait{j} = 0, wt_wait{j} = 0;
reg [{max(idw, 1) - 1}:0] bid{j} [0:2];
reg [2:0] b_n{j} = 0, aw_n{j} = 0;
reg [{max(idw, 1) - 1}:0] awq{j} [0:2];
reg [{max(idw, 1) - 1}:0] bid_head{j} = 0;
integer ix{j};
initial for (ix{j} = 0; ix{j} < {entries}; ix{j} = ix{j} + 1) begin left{j}[ix{j}] = 0; rid{j}[ix{j}] = 0; born{j}[ix{j}] = 0; passed{j}[ix{j}] = 0; end
wire room{j} = ~&open{j};
reg [{max(1, entries - 1).bit_length() - 1}:0] free{j};
always @* begin free{j} = 0; for (ix{j} = {entries - 1}; ix{j} >= 0; ix{j} = ix{j} - 1) if (!open{j}[ix{j}]) free{j} = ix{j}; end
// A row may answer when no older open row has its ID (AXI keeps one ID in order).
reg [{entries - 1}:0] may{j};
always @* begin
  may{j} = open{j};
  for (ix{j} = 0; ix{j} < {entries}; ix{j} = ix{j} + 1)
    if (open{j}[ix{j}])
"""
    for row in rows:
        text += f"""      if (open{j}[{row}] && {row} != ix{j} && rid{j}[{row}] == rid{j}[ix{j}] &&
          born{j}[{row}] < born{j}[ix{j}]) may{j}[ix{j}] = 1'b0;
"""
    forced = f"(rv_wait{j} == {Q})"
    text += f"""end
always @* assume (pick{j} < {entries});
// F4: a row passed over {PICKS} times goes next. The strong form takes one request at a time.
wire pick_ok{j} = may{j}[pick{j}];
"""
    for row in rows:
        text += f"""always @* if (reset_n && new_pick{j}) assume (!(may{j}[{row}] && passed{j}[{row}] >= {PICKS}) || pick{j} == {row});
"""
    text += f"""`ifdef F4_STRONG
assign {n}arready = open{j} == 0 && b_n{j} == 0 && aw_n{j} == 0 && !bv{j} && (want_ar{j} || ar_wait{j} == {P});
assign {n}awready = open{j} == 0 && b_n{j} == 0 && aw_n{j} == 0 && !bv{j} && (want_aw{j} || aw_wait{j} == {P});
assign {n}wready = (aw_n{j} != 0) && (want_w_t{j} || wt_wait{j} == {P});
`elsif F3_STRONG
assign {n}arready = room{j};
assign {n}awready = aw_n{j} < 3;
assign {n}wready = aw_n{j} != 0;
`elsif NO_F3
assign {n}arready = room{j} && want_ar{j};
assign {n}awready = (aw_n{j} < 3) && want_aw{j};
assign {n}wready = (aw_n{j} != 0) && want_w_t{j};
`else
assign {n}arready = room{j} && (want_ar{j} || ar_wait{j} == {P});
assign {n}awready = (aw_n{j} < 3) && (want_aw{j} || aw_wait{j} == {P});
assign {n}wready = (aw_n{j} != 0) && (want_w_t{j} || wt_wait{j} == {P});
`endif
assign {n}rvalid = rv{j};
assign {n}rid = rid{j}[cur{j}];
assign {n}rlast = left{j}[cur{j}] == 0;
assign {n}rresp = 2'd0;
assign {n}rdata = {data}'d0;
assign {n}bvalid = bv{j};
assign {n}bid = bid_head{j};
wire switched{j} = rv{j} && open{j}[cur{j}] && |(open{j} & ~({entries}'d1 << cur{j}));
assign {n}bresp = 2'd0;
wire ar_take{j} = {n}arvalid && {n}arready;
wire r_done{j} = rv{j} && {n}rready;
`ifdef NO_F4
wire new_pick{j} = (!rv{j} || r_done{j}) && want_rv{j};
`else
wire new_pick{j} = (!rv{j} || r_done{j}) && (want_rv{j} || {forced});
`endif
always @* if (reset_n && new_pick{j} && |may{j}) assume (pick_ok{j});
always @(posedge clk_main) begin
  if (!reset_n) begin
    open{j} <= 0; rv{j} <= 0; bv{j} <= 0; b_n{j} <= 0; aw_n{j} <= 0; clock{j} <= 0;
  end else begin
    if (ar_take{j}) begin
      open{j}[free{j}] <= 1; rid{j}[free{j}] <= {n}arid; left{j}[free{j}] <= {n}arlen; born{j}[free{j}] <= clock{j};
      clock{j} <= clock{j} + 1;
    end
    if (r_done{j}) begin
      rv{j} <= 0;
      if (left{j}[cur{j}] == 0) open{j}[cur{j}] <= 0; else left{j}[cur{j}] <= left{j}[cur{j}] - 1;
    end
    if (new_pick{j} && may{j}[pick{j}] && !(r_done{j} && pick{j} == cur{j} && left{j}[cur{j}] == 0)) begin
      rv{j} <= 1; cur{j} <= pick{j};
      for (ix{j} = 0; ix{j} < {entries}; ix{j} = ix{j} + 1)
        if (may{j}[ix{j}] && ix{j} != pick{j}) passed{j}[ix{j}] <= passed{j}[ix{j}] + 1;
      passed{j}[pick{j}] <= 0;
    end
    rv_wait{j} <= (|open{j} && !rv{j}) ? (rv_wait{j} == {Q} ? {Q} : rv_wait{j} + 1) : 0;
    ar_wait{j} <= ({n}arvalid && !{n}arready && room{j}) ? ar_wait{j} + 1 : 0;
    aw_wait{j} <= ({n}awvalid && !{n}awready && aw_n{j} < 3) ? aw_wait{j} + 1 : 0;
    wt_wait{j} <= ({n}wvalid && !{n}wready && aw_n{j} != 0) ? wt_wait{j} + 1 : 0;
    // Writes: AW opens, one W per AW in order (single beats), then B in order.
    if ({n}awvalid && {n}awready) begin awq{j}[0] <= {n}awid; awq{j}[1] <= awq{j}[0]; awq{j}[2] <= awq{j}[1]; end
    aw_n{j} <= aw_n{j} + ({n}awvalid && {n}awready) - ({n}wvalid && {n}wready && {n}wlast);
    if ({n}wvalid && {n}wready && {n}wlast) begin
      bid{j}[0] <= awq{j}[aw_n{j} - 1]; bid{j}[1] <= bid{j}[0]; bid{j}[2] <= bid{j}[1];
    end
    b_n{j} <= b_n{j} + ({n}wvalid && {n}wready && {n}wlast) - (bv{j} && {n}bready);
    if (bv{j} && {n}bready) bv{j} <= 0;
    else if (!bv{j} && b_n{j} != 0 && (want_bv{j} || bv_wait{j} == {Q})) begin
      bv{j} <= 1; bid_head{j} <= bid{j}[b_n{j} - 1];
    end
    bv_wait{j} <= (b_n{j} != 0 && !bv{j}) ? bv_wait{j} + 1 : 0;
  end
end
"""
    return text


def quiet_counter(bound, history=False):
    """Keep every first failure. Values after it need no further counting."""
    if history and 0 < bound < 256:
        return (f"reg [{bound - 1}:0] quiet = 0;\n"
                "always @(posedge clk_main) quiet <= (!reset_n || any_fire || pending == 0) ? 0 : "
                f"((quiet << 1) | 1'b1);\n", f"!quiet[{bound - 1}]")
    bits = min(8, max(1, bound.bit_length()))
    return (f"reg [{bits - 1}:0] quiet = 0;\n"
            f"always @(posedge clk_main) quiet <= (!reset_n || any_fire || pending == 0) ? 0 : "
            f"(quiet < {bound} ? quiet + 1 : quiet);\n", f"quiet < {bound}")


def monitor(ports, sources, bound, history=False):
    """While anything is outstanding some handshake happens within bound cycles. Fairness makes the
    environment act within a few cycles and no path through the network is longer, so only a state
    waiting on itself stays silent. Sources issue a bounded number of requests of known length, so
    handshakes are finite and this bounds every request's answer."""
    fire = [f"({p[:-5]}valid && {p[:-5]}ready)" for p in ports if p.endswith('valid') and p[:-5] + 'ready' in ports]
    fire += [f"({p}valid && {p}ready)" for p in ()]
    text = f"\nwire any_fire = {' || '.join(fire)};\nwire [{len(sources) - 1}:0] pending;\n"
    for i in range(len(sources)):
        n = f'nl_i_s{i}_'
        accept_w = f"({n}awvalid && {n}awready)" if n + 'awvalid' in ports else "1'b0"
        answer_b = f"({n}bvalid && {n}bready)" if n + 'bvalid' in ports else "1'b0"
        text += f"""reg [3:0] out{i} = 0;
always @(posedge clk_main) if (reset_n)
  out{i} <= out{i} + ({n}arvalid && {n}arready) + {accept_w} - ({n}rvalid && {n}rready && {n}rlast) - {answer_b};
assign pending[{i}] = out{i} != 0;
"""
    counter, predicate = quiet_counter(bound, history)
    text += counter + f"always @* if (reset_n) PROGRESS: assert ({predicate});\n"
    return text


def harness(shape_, mapping, bound, covers, cover_group=None):
    ports = {p['name']: p['width'] for p in mapping['ports']}
    sources, targets = shape_['sources'], shape_['targets']
    bases = [0x1000 * j for j in range(len(targets))]
    head = """module nl_live(input wire clk_main);
""" if not shape_['gating'] else """module nl_live;
(* gclk *) wire global_clock;
reg clk_main = 0;
always @(posedge global_clock) clk_main <= !clk_main;
"""
    text = "`default_nettype none\n" + head + """reg started = 0;
always @(posedge clk_main) started <= 1;
wire reset_n = started;
"""
    for name, width in ports.items():
        if name not in ('clk_main', 'reset_n', 'test'):
            text += f"wire [{width - 1}:0] {name};\n"
    text += f"{mapping['module']} u_dut (.clk_main(clk_main), .reset_n(reset_n), .test(1'b0),\n"
    text += ",\n".join(f"  .{n}({n})" for n in ports if n not in ('clk_main', 'reset_n', 'test'))
    text += ");\n"
    for i, s in enumerate(sources):
        text += source_env(i, ports, bases, f"3'd{(s['w'] // 8).bit_length() - 1}", shape_['wrap'], shape_['reads'], shape_['length'], shape_['slow'])
    for j, t in enumerate(targets):
        text += target_env(j, ports, t['k'])
    text += monitor(ports, sources, bound, shape_['history'])
    if covers:
        def cover(label, predicate):
            if cover_group is None or label == cover_group or label.startswith(cover_group + '_'):
                return f"  {label}: cover ({predicate});\n"
            return ''

        text += """// Reachable with the fairness on: none of these may be assumed away.
always @(posedge clk_main) if (reset_n) begin
"""
        for i in range(len(sources)):
            text += cover(f'C_F1_{i}', f'r_block{i}')
        for i, s in enumerate(sources):
            if f'nl_i_s{i}_awvalid' in ports:
                text += cover(f'C_F2_{i}', f'w_owed{i} != 0 && !w_v{i}')
                break
        text += cover('C_F3', 'nl_t_t0_arvalid && !nl_t_t0_arready')
        text += cover('C_F4', 'switched0 && want_rv0 && rv0 && nl_t_t0_rready && !nl_t_t0_rlast')
        text += "end\n"
    text += "endmodule\n"
    if shape_['gating']:
        # The environment changes while the clock is low, so a gate latches what the flops see.
        def latched(m):
            width = m[1] or ''
            return ''.join(f"(* anyseq *) wire {width}{name}_any;\nreg {width}{name} = 0;\n"
                           f"always @(negedge clk_main) {name} <= {name}_any;\n"
                           for name in m[2].split(', '))
        text = re.sub(r"\(\* anyseq \*\) wire (\[[^\]]*\] )?((?:\w+, )*\w+);\n", latched, text)
    return text


def release_at_last_only(rtl, mapping):
    """Each response output's release reads its candidates' last fragment and not their segment end."""
    assign = re.compile(r'^assign (nl_internal_v\d+) = \((nl_internal_v\d+) ([&|]) (nl_internal_v\d+)\);$', re.M)
    binary = {m[1]: (m[2], m[3], m[4]) for m in assign.finditer(rtl)}
    for store in mapping['network']['storage']:
        if not store['resource'].startswith('graph/rsp/'):
            continue
        _, _, tail = binary['nl_internal_v' + store['release'].split('/')[1]]
        for selected in binary[tail][0::2]:
            _, _, end = binary[selected]
            last, op, cut = binary[end]
            assert op == '|'
            rtl, n = re.subn(rf'^assign {end} = \({last} \| {cut}\);$', f'assign {end} = {last};', rtl, flags=re.M)
            assert n == 1
    return rtl


def run_case(case, args):
    name, shape_name, fault, defines, mode, depth, want = case
    depth = depth or args.depth
    shape_ = SHAPES[shape_name]
    work = args.work / name
    work.mkdir(parents=True)
    gen = work / 'gen'
    (work / 'in.yaml').write_text(json.dumps(shape_['config']))
    subprocess.run([args.cli, 'generate', work / 'in.yaml', '--out-dir', gen], check=True,
                   stdout=subprocess.DEVNULL)
    mapping = json.loads(next(gen.glob('*.map.json')).read_text())
    rtl = (gen / (mapping['module'] + '.v')).read_text()
    if fault == 'no_upgrade':
        control = mapping['network']['formal']['sources']['s0']['scheduler_valid']
        wire = 'nl_internal_v' + control.removeprefix('value/')
        rtl, hits = re.subn(rf'^    {wire} <= nl_internal_v\d+;$',
                            f"    {wire} <= 1'b0;", rtl, flags=re.M)
        assert hits == 1, 'upgrade mutation did not bind'
    elif fault:
        rtl = release_at_last_only(rtl, mapping)
    if shape_name == 'L8_upgrade' and mode == 'cover':
        anchors = mapping['network']['formal']['sources']['s0']
        value = lambda key: 'nl_internal_v' + anchors[key].removeprefix('value/')
        marker = 'endmodule'
        assert rtl.count(marker) == 1
        observe = (f"always @(posedge clk_main) if (reset_n) begin UPGRADE_REACHED: "
                   f"cover ({value('scheduler_valid')} && {value('request_raise')} && "
                   f"{value('request_ready')}); end\n")
        rtl = rtl.replace(marker, observe + marker)
    design = rtl
    for cell in gen.glob('nocloom_*.v'):
        if cell.name != mapping['module'] + '.v':
            design += '\n' + cell.read_text()
    (work / 'design.v').write_text(design)
    # Each positive group retains its original cover. Stronger-form controls
    # check their own covers without finding unrelated witnesses first.
    strong = [d.removesuffix('_STRONG') for d in defines if d.endswith('_STRONG')]
    assert len(strong) <= 1
    group = 'C_' + strong[0] if mode == 'cover' and strong else None
    if name.startswith('L2_covers_'):
        group = 'C_' + name.removeprefix('L2_covers_')
        assert group in FAIR_COVERS
    top = harness(shape_, mapping, args.bound, mode == 'cover' and shape_name != 'L8_upgrade', group)
    expected_unreached = set(re.findall(r'\b(C_F\w+): cover', top)) if strong else set()
    assert not strong or expected_unreached, 'strong fairness cover did not bind'
    if shape_name == 'L8_upgrade' and mode == 'cover':
        # A legal directed witness. The bounded safety task leaves these choices free.
        choices = ('new_ar0 && new_aw0 && want_w0 && want_r0 && want_b0',
                   'ar_t0_new == 0 && aw_t0_new == 0 && new_id0 == ar_sent0[0]',
                   'want_ar0 && want_aw0 && want_w_t0 && want_rv0 && want_bv0')
        directed = 'always @* if(reset_n) begin ' + ''.join(
            f'assume({choice});' for choice in choices) + 'end\n'
        top = top.replace('endmodule', directed + 'endmodule')
    (work / 'top.sv').write_text(''.join(f'`define {d}\n' for d in defines) + top)
    engine = {'bmc': 'abc bmc3' if args.engine == 'abc' else 'smtbmc z3', 'cover': 'smtbmc z3'}[mode]
    if mode == 'cover' and want == 'pass':
        engine = 'smtbmc --unroll z3'
    options = 'aigsmt z3\n' if mode == 'bmc' and args.engine == 'abc' else ''
    if shape_['gating']:
        options += 'multiclock on\n'
        depth *= 2
    # Flatten these models before pruning so unused child ports do not
    # remain in the SMT model used for witnesses and counterexamples.
    flatten = mode == 'cover' or shape_['history']
    prep = 'prep -top nl_live' + (' -flatten\nopt_clean -purge' if flatten else '')
    (work / 'live.sby').write_text(
        f"[options]\nmode {mode}\ndepth {depth}\n{options}\n[engines]\n{engine}\n\n[script]\n"
        f"read_verilog -formal design.v\nread -formal top.sv\n{prep}\n\n"
        "[files]\ndesign.v\ntop.sv\n")
    begin = time.monotonic()
    try:
        r = subprocess.run(['sby', '-f', 'live.sby'], cwd=work, capture_output=True, text=True,
                           timeout=args.limit)
        got = 'pass' if r.returncode == 0 else 'fail' if r.returncode == 2 else f'error{r.returncode}'
        log = r.stdout
    except subprocess.TimeoutExpired as e:
        got, log = 'timeout', ''
    if mode == 'cover' and want == 'pass' and got == 'pass':
        pattern = r'\b(\w+):\s*cover\s*\('
        labels = {'nl_live.' + label for label in re.findall(pattern, top)}
        labels.update('nl_live.u_dut.' + label for label in re.findall(pattern, design))
        reached = cover_locations(log, True)
        if not labels or reached != labels:
            got = 'unrelated coverage'
    if fault == 'no_upgrade' and got == 'fail':
        if cover_locations(log, False) != {'nl_live.u_dut.UPGRADE_REACHED'}:
            got = 'unrelated failure'
    if expected_unreached and got == 'fail':
        if cover_locations(log, False) != {'nl_live.' + label for label in expected_unreached}:
            got = 'unrelated failure'
    took = time.monotonic() - begin
    (work / 'sby.log').write_text(log)
    return dict(name=name, shape=shape_name, want=want, got=got, seconds=took,
                mode=mode, depth=depth, limit=args.limit)


D = 0  # --depth: the bound and the slowest fair path before a hang shows
PASS, CEX = 'pass', 'fail'
FAIR_COVERS = ('C_F1_0', 'C_F1_1', 'C_F2_0', 'C_F3', 'C_F4')
CASES = [
    ('L1_prove', 'L1_plain', False, [], 'bmc', D, PASS),
    ('L2_prove', 'L2_read_write', False, [], 'bmc', D, PASS),
    ('L2_hang', 'L2_read_write', True, [], 'bmc', D, CEX),
    *[(f'L2_covers_{label[2:]}', 'L2_read_write', False, [], 'cover', 14, PASS)
      for label in FAIR_COVERS],
    ('L3_prove', 'L3_one_burst', False, [], 'bmc', D, PASS),
    ('L4_depth12_weak_prove', 'L4_gated_merge', False, [], 'bmc', 12, PASS),
    ('L5_depth38_prove', 'L5_long_wrap', False, [], 'bmc', 38, PASS),
    ('L6_prove', 'L6_single_id', False, [], 'bmc', D, PASS),
    ('L6_multi_hop_prove', 'L6_multi_hop', False, [], 'bmc', D, PASS),
    ('L6_hang', 'L6_single_id', True, [], 'bmc', D, CEX),
    ('L6_multi_hop_hang', 'L6_multi_hop', True, [], 'bmc', D, CEX),
    ('L7_prove', 'L7_single_answer', False, [], 'bmc', D, PASS),
    ('L8_upgrade_depth32_prove', 'L8_upgrade', False, [], 'bmc', 32, PASS),
    ('L8_upgrade_cover', 'L8_upgrade', False, [], 'cover', 40, PASS),
    ('L8_upgrade_missing_cover', 'L8_upgrade', 'no_upgrade', [], 'cover', 40, CEX),
    ('L8_depth18_weak_prove', 'L8_merged_contexts', False, [], 'bmc', 18, PASS),
    # Reads of several IDs behind a merge: the merge's stored word and its waits.
    ('L9_prove', 'L9_merged_reads', False, [], 'bmc', D, PASS),
    ('L9_hang', 'L9_merged_reads', True, [], 'bmc', D, CEX),
    # A needed assumption: without it the same RTL stalls for ever under the environment.
    ('F1_needed', 'L1_plain', False, ['NO_F1'], 'bmc', D, CEX),
    ('F2_needed', 'L1_plain', False, ['NO_F2'], 'bmc', D, CEX),
    ('F3_needed', 'L1_plain', False, ['NO_F3'], 'bmc', D, CEX),
    ('F4_needed', 'L1_plain', False, ['NO_F4'], 'bmc', D, CEX),
    # An assumption too strong: the state it must leave reachable is gone, or the hang is hidden.
    ('F1_strong_kills_cover', 'L2_read_write', False, ['F1_STRONG'], 'cover', 14, CEX),
    ('F2_strong_kills_cover', 'L2_read_write', False, ['F2_STRONG'], 'cover', 14, CEX),
    ('F3_strong_kills_cover', 'L2_read_write', False, ['F3_STRONG'], 'cover', 14, CEX),
    ('F4_strong_kills_cover', 'L2_read_write', False, ['F4_STRONG'], 'cover', 14, CEX),
]


def selftest(args):
    """Check cover result binding."""
    from unittest.mock import patch

    real_run = subprocess.run
    controls = [c for c in CASES if c[0].startswith('F') and c[0].endswith('_kills_cover')]
    assert len(controls) == 4
    root = args.work / f'selftest-{time.time_ns()}'
    for case in controls:
        group = case[3][0].removesuffix('_STRONG')
        labels = [f'C_{group}_{i}' for i in range(2)] if group == 'F1' else (
            ['C_F2_0'] if group == 'F2' else [f'C_{group}'])
        unrelated = 'C_F4' if group != 'F4' else 'C_F3'
        trials = [(labels, 2, 'fail'), ([unrelated], 2, 'unrelated failure'),
                  (labels, 1, 'error1')]
        if group == 'F1':
            trials.append((labels[:1], 2, 'unrelated failure'))
        for index, (unreached, rc, want) in enumerate(trials):
            def run(command, **kwargs):
                if command[0] != 'sby':
                    return real_run(command, **kwargs)
                log = '\n'.join('Unreached cover statement at nl_live.' + label
                                for label in unreached)
                return subprocess.CompletedProcess(command, rc, stdout=log, stderr='')

            opts = argparse.Namespace(cli=args.cli.resolve(), work=root / f'{group}-{index}',
                                   bound=10, depth=24, engine='abc', limit=1200)
            with patch.object(subprocess, 'run', side_effect=run):
                row = run_case(case, opts)
            assert row['got'] == want, (case[0], unreached, row, want)
    for positive in (c for c in CASES if c[0].startswith('L2_covers_')):
        labels = ['C_' + positive[0].removeprefix('L2_covers_')]
        trials = [(labels, 'pass'), (labels[:-1], 'unrelated coverage'),
                  (labels + ['UNRELATED'], 'unrelated coverage')]
        for index, (reached, want) in enumerate(trials):
            def run(command, **kwargs):
                if command[0] != 'sby':
                    return real_run(command, **kwargs)
                log = '\n'.join('Reached cover statement in step 10 at nl_live: ' + label for label in reached)
                return subprocess.CompletedProcess(command, 0, stdout=log, stderr='')

            opts = argparse.Namespace(cli=args.cli.resolve(), work=root / f'{positive[0]}-{index}',
                                      bound=10, depth=24, engine='abc', limit=1200)
            with patch.object(subprocess, 'run', side_effect=run):
                row = run_case(positive, opts)
            assert row['got'] == want, (reached, row, want)
    upgrade = next(c for c in CASES if c[0] == 'L8_upgrade_cover')
    for index, (label, want) in enumerate((('u_dut.UPGRADE_REACHED', 'pass'),
                                          ('UPGRADE_REACHED', 'unrelated coverage'),
                                          ('other.UPGRADE_REACHED', 'unrelated coverage'),
                                          ('UNRELATED', 'unrelated coverage'))):
        def run(command, **kwargs):
            if command[0] != 'sby':
                return real_run(command, **kwargs)
            log = 'Reached cover statement in step 21 at nl_live: ' + label
            return subprocess.CompletedProcess(command, 0, stdout=log, stderr='')

        opts = argparse.Namespace(cli=args.cli.resolve(), work=root / f'upgrade-{index}',
                                  bound=10, depth=24, engine='abc', limit=1200)
        with patch.object(subprocess, 'run', side_effect=run):
            row = run_case(upgrade, opts)
        assert row['got'] == want, (label, row, want)
    print('LIVENESS DRIVER PASS')
    return 0


def check_quiet_counters(root):
    root.mkdir(parents=True)
    # Check the actual counter constructor against the original eight-bit
    # monitor for arbitrary reset and activity, including after a failure.
    for history, broken in ((False, False), (False, True), (True, False), (True, True)):
        kind = 'history' if history else 'binary'
        folder = root / (kind + ('_wrong_bound' if broken else '_equal'))
        folder.mkdir()
        counter, predicate = quiet_counter(10, history)
        (folder / 'formal.v').write_text(f"""module nl_quiet_check(input clk_main, input reset_n, input any_fire, input pending);
{counter}
reg [7:0] old_quiet=0;
reg old_bad=0, new_bad=0;
always @(posedge clk_main) begin
  old_quiet<=(!reset_n || any_fire || pending==0) ? 0 : old_quiet+1;
  old_bad<=old_bad || (reset_n && old_quiet>={11 if broken else 10});
  new_bad<=new_bad || (reset_n && !({predicate}));
end
always @* PREFIX_FAILURE_EQUAL: assert(old_bad==new_bad);
endmodule
""")
        (folder / 'task.sby').write_text(
            '[options]\nmode prove\ntimeout 30\naigsmt z3\n[engines]\nabc pdr -l\n'
            '[script]\nread -formal formal.v\nprep -top nl_quiet_check\n[files]\nformal.v\n')
        result = subprocess.run(['sby', '-f', 'task.sby'], cwd=folder, capture_output=True,
                          text=True, timeout=60)
        log = result.stdout + result.stderr
        (folder / 'run.log').write_text(log)
        status = (folder / 'task/status').read_text().split()[0]
        assert status == ('FAIL' if broken else 'PASS') and result.returncode == (2 if broken else 0)
        if broken:
            assert 'failed assertion nl_quiet_check.PREFIX_FAILURE_EQUAL' in log
            assert list((folder / 'task').rglob('*.vcd'))


def split_cover_budgets(rows, limit):
    members = [r for r in rows if r['name'].startswith('L2_covers_') and r['want'] == PASS]
    if len(members) != len(FAIR_COVERS):
        return []
    seconds = sum(r['seconds'] for r in members)
    accepted = seconds <= limit and all(r['got'] == PASS for r in members)
    return [dict(task='L2_covers', seconds=seconds, limit=limit,
                 status='PASS' if accepted else 'FAIL')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', type=Path, required=True)
    ap.add_argument('--work', type=Path, required=True)
    ap.add_argument('--only', default='')
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--bound', type=int, default=10)
    ap.add_argument('--depth', type=int, default=24)
    ap.add_argument('--limit', type=int, default=1200)
    ap.add_argument('--engine', choices=('abc', 'z3'), default='abc')
    # Require each selected case to produce a counterexample, for fault-injected RTL.
    ap.add_argument('--expect-fail', action='store_true')
    args = ap.parse_args()
    args.cli = args.cli.resolve()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.self_test:
        return selftest(args)
    args.work = args.work / f'run-{time.time_ns()}'
    if not args.only or re.search(args.only, 'L1_prove'):
        check_quiet_counters(args.work / 'counter_contracts')
    rows = []
    for case in CASES:
        if args.only and not re.search(args.only, case[0]):
            continue
        row = run_case(case, args)
        if args.expect_fail:
            row['want'] = CEX
        print(f"LIVENESS {row['name']}: want {row['want']} got {row['got']} {row['seconds']:.1f} s", flush=True)
        rows.append(row)
    budgets = split_cover_budgets(rows, args.limit)
    (args.work / 'budgets.json').write_text(json.dumps(budgets, indent=1) + '\n')
    for budget in budgets:
        print('COVER_BUDGET', budget['task'], budget['seconds'], budget['limit'], budget['status'])
    (args.work / 'results.json').write_text(json.dumps(rows, indent=1) + '\n')
    bad = [r for r in rows if r['want'] != r['got']] + [b for b in budgets if b['status'] != 'PASS']
    print('LIVENESS', 'FAIL' if bad else 'PASS', len(rows))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
