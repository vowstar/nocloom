"""AXI5 data side end to end: every data user bit through width conversion, packet splitting and a
clock domain crossing. Initiators and targets derive each byte's data and group from its address,
so a target checks every write byte and an initiator every read byte, whatever the conversions.
Unstrobed write bytes and failed read beats must carry zero groups."""
import argparse
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

# Endpoint: name -> (protocol, data bits, data side ports, extra fields). Each target owns 256 KiB
# at its base, its local address equal to the global one.
MODES = {
    # One switch, 32-bit network: every pair of 32, 64 and 128 bits, and endpoints without ports.
    'widths': dict(
        k=2, network=32, clocked=False,
        sources={'s32': ('axi5', 32, True, {}), 's64': ('axi5', 64, True, {}),
                 's128': ('axi5', 128, True, {'response_user_width': 2}),
                 'p64': ('axi4', 64, False, {})},
        targets={'t32': ('axi5', 32, True, {}),
                 't64': ('axi5', 64, True, {}),
                 't128': ('axi5', 128, True, {'response_user_width': 2, 'max_outstanding': 4}),
                 'q32': ('axi4', 32, False, {}),
                 'x64': ('axi5', 64, True, {'read_side_effects': True})},
        limits=[]),
    # Packet limits and narrow bursts split per beat.
    'split': dict(
        k=4, network=32, clocked=False,
        sources={'s32': ('axi5', 32, True, {}),
                 's64': ('axi5', 64, True, {'narrow_bursts': 'merge_modifiable',
                                            'max_outstanding': 4}),
                 's128': ('axi5', 128, True, {'max_outstanding': 4})},
        targets={'t32': ('axi5', 32, True, {'max_outstanding': 2}),
                 't64': ('axi5', 64, True, {}),
                 't128': ('axi5', 128, True, {'max_outstanding': 4})},
        limits=[(s, t, 16) for s in ('s32', 's64', 's128') for t in ('t32', 't64', 't128')]),
    # A 64-bit network: 64-bit initiators into 32, 64 and 128 bits.
    'net64': dict(
        k=3, network=64, clocked=False,
        sources={'s64': ('axi5', 64, True, {'max_outstanding': 4}),
                 'r64': ('axi5', 64, True, {'response_user_width': 2})},
        targets={'t32': ('axi5', 32, True, {'response_user_width': 2}),
                 't64': ('axi5', 64, True, {'max_outstanding': 4}),
                 't128': ('axi5', 128, True, {'read_side_effects': True})},
        limits=[('s64', 't128', 32)]),
    # Two clock domains, a 64-bit pair without conversion and a 128-bit initiator into 32 bits.
    'clocked': dict(
        k=1, network=32, clocked=True,
        sources={'s64': ('axi5', 64, True, {'response_user_width': 1}),
                 's128': ('axi5', 128, True, {}), 's32': ('axi5', 32, True, {})},
        targets={'t64': ('axi5', 64, True, {'response_user_width': 1}),
                 't32': ('axi5', 32, True, {}), 't128': ('axi5', 128, True, {})},
        limits=[('s128', 't32', 32)],
        reach={'s64': ['t64'], 's128': ['t32', 't128'], 's32': ['t32', 't128']}),
}
BASES = 0x40000


def configuration(mode):
    m = MODES[mode]
    domains = ['fast', 'slow'] if m['clocked'] else ['main']
    src_domain, dst_domain = (domains[0], domains[-1])
    transport = {'data_width': m['network'], 'data_user_bits_per_byte': m['k']}
    if m['limits']:
        transport['packet_limits'] = [{'source': s, 'target': t, 'max_bytes': b}
                                      for s, t, b in m['limits']]
    responses = [e[3].get('response_user_width', 0)
                 for e in list(m['sources'].values()) + list(m['targets'].values())]
    if any(responses):
        transport['response_user_bits'] = max(responses)
    cfg = {
        'schema_version': 10, 'name': 'axi5_data', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [], 'targets': [], 'transport': transport,
        'connectivity': {s: list(t) for s, t in m['reach'].items()} if 'reach' in m else 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    for name, (protocol, bits, ports, extra) in m['sources'].items():
        entry = {'name': name, 'clock_domain': src_domain, 'protocol': protocol,
                 'data_width': bits, 'id_width': 2, 'len_width': 8}
        if ports:
            entry['data_user_width'] = bits // 8 * m['k']
        entry.update(extra)
        cfg['initiators'].append(entry)
    for index, (name, (protocol, bits, ports, extra)) in enumerate(m['targets'].items()):
        base = (index + 1) * BASES
        entry = {'name': name, 'clock_domain': dst_domain, 'protocol': protocol,
                 'data_width': bits, 'id_width': 4, 'len_width': 8, 'attributes': 'amba',
                 'mappings': [{'base': hex(base), 'size': hex(BASES), 'local_address': hex(base)}]}
        if ports:
            entry['data_user_width'] = bits // 8 * m['k']
        entry.update(extra)
        cfg['targets'].append(entry)
    if m['clocked']:
        def graph(entries, exits, src, dst):
            return {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                              {'name': 'leaf', 'clock_domain': 'slow'}],
                    'links': [{'from': src, 'to': dst}],
                    'entries': [{'endpoint': e, 'node': 'root' if src == 'root' else 'leaf'}
                                for e in entries],
                    'exits': [{'endpoint': e, 'node': dst} for e in exits]}
        cfg['topology'] = {
            'request': graph(m['sources'], m['targets'], 'root', 'leaf'),
            'response': graph(m['targets'], m['sources'], 'leaf', 'root')}
    return cfg


def transactions(mode, seed):
    """Per source: walking ones and zeros over every group bit and byte position, then random
    patterns, over INCR, WRAP and FIXED bursts of every size, some unaligned, some failing."""
    m = MODES[mode]
    random.seed(seed)
    k = m['k']
    targets = list(m['targets'])
    reach = m.get('reach', {s: targets for s in m['sources']})
    out, slots = [], {t: 0 for t in targets}
    for s, (_, bits, _, extra) in m['sources'].items():
        beat_log = (bits // 8).bit_length() - 1
        walks = 2 * 16 * k
        for j in range(walks + 24):
            target = reach[s][j % len(reach[s])]
            write = (j // len(reach[s])) % 2 == 0
            tbits = m['targets'][target][1]
            size = random.randint(0, beat_log) if j % 3 else beat_log
            length = random.choice((0, 1, 2, 3, 5, 7, 8, 15))
            burst = 1
            if j % 5 in (2, 3) and length in (1, 3, 7, 15):
                burst = 2
            elif j % 11 == 5 and (1 << size) <= tbits // 8 and length < 16:
                burst = 0
            span = (length + 1) << size
            index = len(out)
            base = (targets.index(target) + 1) * BASES
            start = base + slots[target] * 0x200
            slots[target] += 1
            assert slots[target] * 0x200 <= BASES
            if burst == 2:
                addr = start + (random.randint(0, length) << size)
            elif burst == 0:
                addr = start + random.randint(0, 3) * (1 << size)
                span = 1 << size
                addr_lo, addr_hi = addr, addr + (1 << size)
            else:
                addr = start + (random.randint(0, (1 << size) - 1) if j % 5 == 1 else 0)
            lo, hi = (start, start + span) if burst != 0 else (addr_lo, addr_hi)
            if burst == 1:
                lo, hi = addr, ((addr >> size) << size) + span
            kind = (0 if j % 2 == 0 else 1) if j < walks else 2
            t = dict(index=index, source=s, target=target, write=write, addr=addr, len=length,
                     size=size, burst=burst, id=j % 4, lo=lo, hi=hi, kind=kind,
                     pos=(j // 2) if j < walks else random.getrandbits(16),
                     fail=j >= walks and j % 6 == 4)
            out.append(t)
    return out


def bench(mode, cfg, ports, txs, seed):
    m = MODES[mode]
    k = m['k']
    names = {p['name']: p for p in ports}
    sources = {i['name']: i for i in cfg['initiators']}
    targets = {t['name']: t for t in cfg['targets']}
    tlist = list(targets)
    sclk, tclk = ('clk_fast', 'clk_slow') if m['clocked'] else ('clk_main', 'clk_main')
    clocks = ('always #5 clk_fast = ~clk_fast;\nalways #7 clk_slow = ~clk_slow;'
              if m['clocked'] else 'always #5 clk_main = ~clk_main;')
    n = len(txs)
    decl = []
    for p in ports:
        if p['name'].startswith('clk_'):
            continue
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f"{kind} [{p['width'] - 1}:0] {p['name']};")
    conn = ', '.join(f".{p['name']}({p['name']})" for p in ports)
    table = []
    for t in txs:
        g = t['index']
        table.append(
            f"lo[{g}]=32'h{t['lo']:x}; hi[{g}]=32'h{t['hi']:x}; wr[{g}]={int(t['write'])}; "
            f"tg[{g}]={tlist.index(t['target'])}; gk[{g}]={t['kind']}; gp[{g}]={t['pos']}; "
            f"gs[{g}]={int(bool(sources[t['source']].get('data_user_width')))}; "
            f"fl[{g}]={int(t['fail'])}; "
            f"want[{g}]={(t['hi'] - t['lo']) if t['burst'] else (t['len'] + 1) << t['size']};")
    def ruser_resp(e):
        return e.get('response_user_width', 0)
    drivers = []
    for si, (s, e) in enumerate(sources.items()):
        lanes = e['data_width'] // 8
        p = f'nl_i_{s}_'
        has = bool(e.get('data_user_width'))
        rb = ruser_resp(e)
        calls = []
        for t in [x for x in txs if x['source'] == s]:
            call = 'write_' if t['write'] else 'read_'
            calls.append(f"{call}{s}({t['index']}, 32'h{t['addr']:x}, 8'd{t['len']}, "
                         f"3'd{t['size']}, 2'd{t['burst']}, 2'd{t['id']});")
        wuser = (f"{p}wuser[lane*{k} +: {k}] = grp(g, (b & ~32'd{lanes - 1}) + lane, 0);"
                 if has else '')
        rcheck = ''
        if has:
            rcheck = f'''
        if (lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size)))
          check({p}ruser[{rb} + lane*{k} +: {k}] ==
                (fail || !th[tg[g]] ? {k}'d0 : grp(g, (b & ~32'd{lanes - 1}) + lane, 5)),
                $sformatf("RUSER {s} g=%0d k=%0d lane=%0d %h", g, k, lane,
                          {p}ruser[{rb} + lane*{k} +: {k}]));'''
        resp_check = ''
        if rb:
            resp_check = (f"check(fail || {p}ruser[{rb - 1}:0] == tr[tg[g]][{rb - 1}:0], "
                          f"$sformatf(\"RUSER_RESPONSE {s} g=%0d %h\", g, {p}ruser[{rb - 1}:0]));")
        drivers.append(f'''
function automatic [31:0] beat_{s}(input [31:0] a, input [7:0] len, input [2:0] size,
                                   input [1:0] burst, input integer k);
  reg [31:0] step, container, base;
  begin
    step = 32'd1 << size;
    if (burst == 2'd0) beat_{s} = a;
    else if (burst == 2'd2) begin
      container = step * (len + 1); base = a & ~(container - 1);
      beat_{s} = base + ((a - base + k * step) % container);
    end else beat_{s} = k == 0 ? a : (a & ~(step - 1)) + k * step;
  end
endfunction
task automatic write_{s}(input integer g, input [31:0] a, input [7:0] len, input [2:0] size,
                         input [1:0] burst, input [1:0] id);
  integer k, lane, guard; reg [31:0] b; reg fail;
  begin
    fail = fl[g];
    {p}awaddr = a; {p}awlen = len; {p}awsize = size; {p}awburst = burst; {p}awid = id;
    {p}awvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}awready) begin guard = guard + 1; check(guard < 20000, "AW_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}awvalid = 0;
    for (k = 0; k <= len; k = k + 1) begin
      b = beat_{s}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        {p}wdata[lane*8 +: 8] = wfn((b & ~32'd{lanes - 1}) + lane);
        {p}wstrb[lane] = lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size));
        {wuser}
      end
      {p}wlast = k == len; {p}wvalid = 1;
      @(posedge {sclk}); guard = 0;
      while (!{p}wready) begin guard = guard + 1; check(guard < 20000, "W_HANG"); @(posedge {sclk}); end
      @(negedge {sclk}); {p}wvalid = 0;
    end
    {p}bready = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}bvalid) begin guard = guard + 1; check(guard < 40000, "B_HANG"); @(posedge {sclk}); end
    check({p}bresp == (fail ? 2'd2 : 2'd0) && {p}bid == id,
          $sformatf("B_RESPONSE {s} g=%0d resp=%0d id=%0d/%0d", g, {p}bresp, {p}bid, id));
    @(negedge {sclk}); {p}bready = 0;
  end
endtask
task automatic read_{s}(input integer g, input [31:0] a, input [7:0] len, input [2:0] size,
                        input [1:0] burst, input [1:0] id);
  integer k, lane, guard; reg [31:0] b; reg fail;
  begin
    fail = fl[g];
    {p}araddr = a; {p}arlen = len; {p}arsize = size; {p}arburst = burst; {p}arid = id;
    {p}arvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}arready) begin guard = guard + 1; check(guard < 20000, "AR_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}arvalid = 0; {p}rready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      // RREADY pauses between beats, changing only at a falling edge.
      if (k && stall(a, k + 5)) begin
        @(negedge {sclk}); {p}rready = 0;
        repeat (stall(a, k + 5)) @(negedge {sclk});
        {p}rready = 1;
      end
      @(posedge {sclk}); guard = 0;
      while (!{p}rvalid) begin guard = guard + 1; check(guard < 40000, "R_HANG"); @(posedge {sclk}); end
      check({p}rresp == (fail ? 2'd2 : 2'd0) && {p}rid == id && {p}rlast == (k == len),
            $sformatf("R_RESPONSE {s} g=%0d k=%0d resp=%0d id=%0d/%0d last=%0d", g, k, {p}rresp,
                      {p}rid, id, {p}rlast));
      {resp_check}
      b = beat_{s}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        if (!fail && lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size)))
          check({p}rdata[lane*8 +: 8] == rfn((b & ~32'd{lanes - 1}) + lane),
                $sformatf("R_DATA {s} g=%0d a=%h k=%0d lane=%0d %h/%h", g, a, k, lane,
                          {p}rdata[lane*8 +: 8], rfn((b & ~32'd{lanes - 1}) + lane)));{rcheck}
      end
    end
    @(negedge {sclk}); {p}rready = 0;
  end
endtask
initial begin : run_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
  {chr(10).join('  ' + c for c in calls)}
  done[{si}] = 1;
end
''')
    responders = []
    for ti, (tname, e) in enumerate(targets.items()):
        lanes = e['data_width'] // 8
        p = f'nl_t_{tname}_'
        has = bool(e.get('data_user_width'))
        rb = ruser_resp(e)
        wcheck = ''
        if has:
            wcheck = f'''
        check({p}wuser[lane*{k} +: {k}] ==
              ({p}wstrb[lane] && g >= 0 && gs[g] ? grp(g, (b & ~32'd{lanes - 1}) + lane, 0) : {k}'d0),
              $sformatf("WUSER {tname} g=%0d k=%0d lane=%0d strobe=%0d %h", g, k, lane,
                        {p}wstrb[lane], {p}wuser[lane*{k} +: {k}]));'''
        rdrive = ''
        if has:
            rdrive = (f"{p}ruser[{rb} + lane*{k} +: {k}] = "
                      f"g >= 0 ? grp(g, (b & ~32'd{lanes - 1}) + lane, 5) : {k}'d0;")
        resp_drive = f"{p}ruser[{rb - 1}:0] = tr[{ti}];" if rb else ''
        idw = names[p + 'awid']['width']
        responders.append(f'''
function automatic [31:0] tbeat_{tname}(input [31:0] a, input [7:0] len, input [2:0] size,
                                       input [1:0] burst, input integer k);
  reg [31:0] step, container, base;
  begin
    step = 32'd1 << size;
    if (burst == 2'd0) tbeat_{tname} = a;
    else if (burst == 2'd2) begin
      container = step * (len + 1); base = a & ~(container - 1);
      tbeat_{tname} = base + ((a - base + k * step) % container);
    end else tbeat_{tname} = k == 0 ? a : (a & ~(step - 1)) + k * step;
  end
endfunction
function automatic integer find_{tname}(input [31:0] a, input w);
  integer g;
  begin
    find_{tname} = -1;
    for (g = 0; g < {n}; g = g + 1)
      if (tg[g] == {ti} && wr[g] == w && a >= (lo[g] & ~32'd15) && a < hi[g]) find_{tname} = g;
  end
endfunction
initial begin : write_{tname}
  integer g, k, lane; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{idw - 1}:0] id;
  {p}awready = 0; {p}wready = 0; {p}bvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}awready = 1;
    @(posedge {tclk}); while (!{p}awvalid) @(posedge {tclk});
    a = {p}awaddr; len = {p}awlen; size = {p}awsize; burst = {p}awburst; id = {p}awid;
    g = find_{tname}(a, 1);
    check(g >= 0, $sformatf("AW_UNKNOWN {tname} %h", a));
    if (g >= 0) hits[g] = hits[g] + 1;
    @(negedge {tclk}); {p}awready = 0; {p}wready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      // WREADY pauses between beats, changing only at a falling edge.
      if (k && stall(a, k + 3)) begin
        @(negedge {tclk}); {p}wready = 0;
        repeat (stall(a, k + 3)) @(negedge {tclk});
        {p}wready = 1;
      end
      @(posedge {tclk}); while (!{p}wvalid) @(posedge {tclk});
      b = tbeat_{tname}(a, len, size, burst, k);
      check({p}wlast == (k == len), "W_LAST");
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        if ({p}wstrb[lane]) begin
          check({p}wdata[lane*8 +: 8] == wfn((b & ~32'd{lanes - 1}) + lane), "W_DATA");
          if (g >= 0) seen[g] = seen[g] + 1;
        end{wcheck}
      end
    end
    @(negedge {tclk}); {p}wready = 0; {p}bvalid = 1; {p}bid = id;
    {p}bresp = g >= 0 && fl[g] ? 2'd2 : 2'd0;
    @(posedge {tclk}); while (!{p}bready) @(posedge {tclk});
    @(negedge {tclk}); {p}bvalid = 0;
  end
end
initial begin : read_{tname}
  integer g, k, lane; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{idw - 1}:0] id;
  {p}arready = 0; {p}rvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}arready = 1;
    @(posedge {tclk}); while (!{p}arvalid) @(posedge {tclk});
    a = {p}araddr; len = {p}arlen; size = {p}arsize; burst = {p}arburst; id = {p}arid;
    g = find_{tname}(a, 0);
    check(g >= 0, $sformatf("AR_UNKNOWN {tname} %h", a));
    if (g >= 0) hits[g] = hits[g] + 1;
    @(negedge {tclk}); {p}arready = 0;
    for (k = 0; k <= len; k = k + 1) begin
      // A pause between beats ends a response segment.
      if (stall(a, k)) repeat (stall(a, k)) @(negedge {tclk});
      b = tbeat_{tname}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        {p}rdata[lane*8 +: 8] = rfn((b & ~32'd{lanes - 1}) + lane);
        {rdrive}
      end
      {resp_drive}
      {p}rvalid = 1; {p}rid = id; {p}rresp = g >= 0 && fl[g] ? 2'd2 : 2'd0; {p}rlast = k == len;
      @(posedge {tclk}); while (!{p}rready) @(posedge {tclk});
      @(negedge {tclk}); {p}rvalid = 0;
    end
  end
end
''')
    init = [f"{p['name']} = 0;" for p in ports if p['direction'] == 'input'
            and not p['name'].startswith('clk_') and p['name'] != 'reset_n']
    th = ' '.join(f"th[{i}] = {int(bool(t.get('data_user_width')))};"
                  for i, t in enumerate(targets.values()))
    tr = ' '.join(f"tr[{i}] = {(i + 1) % (1 << ruser_resp(t)) if ruser_resp(t) else 0};"
                  for i, t in enumerate(targets.values()))
    mask = (1 << k) - 1
    return f'''`timescale 1ns/1ps
module tb;
reg clk_main = 0, clk_fast = 0, clk_slow = 0;
{clocks}
{chr(10).join(decl)}
nocloom_axi5_data dut({conn});
integer errors = 0;
task automatic check(input ok, input string what);
  if (!ok) begin
    errors = errors + 1;
    if (errors < 20) $display("CHECK_FAIL: %0s at %0t", what, $time);
  end
endtask
function automatic [7:0] wfn(input [31:0] a);
  wfn = a[7:0] * 8'd7 + a[14:8] * 8'd13 + 8'h31;
endfunction
function automatic [7:0] rfn(input [31:0] a);
  rfn = a[7:0] * 8'd11 + a[14:8] * 8'd3 + 8'h6c;
endfunction
reg [31:0] lo [0:{n - 1}]; reg [31:0] hi [0:{n - 1}]; reg wr [0:{n - 1}]; integer tg [0:{n - 1}];
integer gk [0:{n - 1}]; integer gp [0:{n - 1}]; reg gs [0:{n - 1}]; reg fl [0:{n - 1}];
integer hits [0:{n - 1}]; integer seen [0:{n - 1}]; integer want [0:{n - 1}];
reg th [0:{len(targets) - 1}]; reg [7:0] tr [0:{len(targets) - 1}];
// A byte's group: walking one (kind 0) or zero (kind 1) over the {k} bits of 16 byte positions,
// else a hash of the address. salt 5 marks read data.
function automatic [{k - 1}:0] grp(input integer g, input [31:0] a, input integer salt);
  integer pos; reg [31:0] h;
  begin
    pos = gp[g] + salt;
    if (gk[g] == 2) begin
      h = (a * 32'h9e3779b1) ^ (pos * 32'h85ebca6b); h = h ^ (h >> 13);
      grp = h[{k - 1}:0];
    end else begin
      grp = ((a % 16) == ((pos / {k}) % 16)) ? ({k}'d1 << (pos % {k})) : {k}'d0;
      if (gk[g] == 1) grp = ~grp & {k}'d{mask};
    end
  end
endfunction
// Wait cycles before a beat: none mostly, one to three now and then.
function automatic integer stall(input [31:0] a, input integer k);
  reg [31:0] h;
  begin
    h = (a * 32'h2545f491) ^ (k * 32'h9e3779b9); h = h ^ (h >> 11);
    stall = h[3:0] < 4'd3 ? h[5:4] + 1 : 0;
  end
endfunction
reg [{len(sources) - 1}:0] done = 0;
integer i;
initial begin
  for (i = 0; i < {n}; i = i + 1) begin hits[i] = 0; seen[i] = 0; end
  {th} {tr}
  {chr(10).join('  ' + line for line in table)}
end
initial begin
  {' '.join(init)}
  reset_n = 0;
  repeat (8) @(negedge {tclk});
  reset_n = 1;
end
{''.join(drivers)}
{''.join(responders)}
integer split;
initial begin
  wait (done == {{{len(sources)}{{1'b1}}}});
  repeat (50) @(negedge {tclk});
  split = 0;
  for (i = 0; i < {n}; i = i + 1) begin
    check(hits[i] >= 1, $sformatf("NO_TARGET_BURST %0d", i));
    if (wr[i]) check(seen[i] == want[i], $sformatf("WRITE_BYTES %0d %0d/%0d", i, seen[i], want[i]));
    if (hits[i] > 1) split = split + 1;
  end
  $display("AXI5_DATA transactions={n} split=%0d seed={seed}", split);
  if (errors == 0) $display("CHECK_PASS");
  else $display("CHECK_FAIL: %0d errors", errors);
  $finish;
end
initial begin
  #50000000;
  $display("CHECK_FAIL: TIMEOUT done=%b", done);
  $finish;
end
endmodule
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--mode', choices=sorted(MODES), required=True)
    ap.add_argument('--seed', type=int, default=11)
    args = ap.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    cfg = configuration(args.mode)
    (work / 'axi5_data.yaml').write_text(json.dumps(cfg, indent=1) + '\n')
    out = work / 'generated'
    proc = subprocess.run([args.cli, 'generate', str(work / 'axi5_data.yaml'), '--out-dir',
                           str(out)], capture_output=True, text=True, timeout=600)
    if proc.returncode:
        print(proc.stderr)
        return 1
    mapping = json.loads((out / 'nocloom_axi5_data.map.json').read_text())
    (work / 'network.v').write_text(generated_rtl(out / 'nocloom_axi5_data.v'))
    txs = transactions(args.mode, args.seed)
    (work / 'tb.sv').write_text(bench(args.mode, cfg, mapping['ports'], txs, args.seed))
    code = simulate(work, base / 'compiled')
    log = (work / 'simulation.log').read_text()
    split = re.search(r'split=(\d+)', log)
    passed = (code == 0 and 'CHECK_PASS' in log and 'CHECK_FAIL' not in log and split and
              int(split.group(1)) > 0)
    print(log[-3000:] if not passed else log.splitlines()[0])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
