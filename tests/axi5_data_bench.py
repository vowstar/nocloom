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

# Endpoint: name -> (protocol, data bits, data side ports, extra fields). The ports are letters:
# u for data user, p for poison, t for trace. Each target owns 256 KiB at its base, its local address equal to
# the global one.
MODES = {
    # One switch, 32-bit network: every pair of 32, 64 and 128 bits, and endpoints without ports.
    'widths': dict(
        k=2, network=32, clocked=False,
        sources={'s32': ('axi5', 32, 'upt', {}), 's64': ('axi5', 64, 'p', {}),
                 's128': ('axi5', 128, 'upt', {'response_user_width': 2}),
                 'p64': ('axi4', 64, '', {})},
        targets={'t32': ('axi5', 32, 'upt', {}),
                 't64': ('axi5', 64, 'u', {}),
                 't128': ('axi5', 128, 'upt', {'response_user_width': 2, 'max_outstanding': 4}),
                 'q32': ('axi4', 32, '', {}),
                 'x64': ('axi5', 64, 'pt', {'read_side_effects': True})},
        limits=[]),
    # Packet limits and narrow bursts split per beat.
    'split': dict(
        k=4, network=32, clocked=False,
        sources={'s32': ('axi5', 32, 'upt', {}),
                 's64': ('axi5', 64, 'upt', {'narrow_bursts': 'merge_modifiable',
                                             'max_outstanding': 4}),
                 's128': ('axi5', 128, 'upt', {'max_outstanding': 4})},
        targets={'t32': ('axi5', 32, 'upt', {'max_outstanding': 2}),
                 't64': ('axi5', 64, 'upt', {}),
                 't128': ('axi5', 128, 'upt', {'max_outstanding': 4})},
        limits=[(s, t, 16) for s in ('s32', 's64', 's128') for t in ('t32', 't64', 't128')]),
    # ACE5-Lite sidebands in both directions through the 32-bit transport.
    'ace5_lite': dict(
        k=1, network=32, clocked=False,
        sources={'s128': ('ace5_lite', 128, 'pt', {}), 's32': ('ace5_lite', 32, 'pt', {})},
        targets={'t32': ('ace5_lite', 32, 'pt', {}), 't64': ('ace5_lite', 64, 'pt', {}),
                 't128': ('ace5_lite', 128, 'pt', {})},
        limits=[]),
    # A 64-bit network: 64-bit initiators into 32, 64 and 128 bits.
    'net64': dict(
        k=3, network=64, clocked=False,
        sources={'s64': ('axi5', 64, 'upt', {'max_outstanding': 4}),
                 'r64': ('axi5', 64, 'ut', {'response_user_width': 2})},
        targets={'t32': ('axi5', 32, 'pt', {'response_user_width': 2}),
                 't64': ('axi5', 64, 'upt', {'max_outstanding': 4}),
                 't128': ('axi5', 128, 'up', {'read_side_effects': True})},
        limits=[('s64', 't128', 32)]),
    # Targets with Max_Transaction_Bytes: packets stay within it. The 64-bit initiator declares 64
    # bytes, so its FIXED bursts fit too.
    'maxbytes': dict(
        k=2, network=32, clocked=False,
        sources={'s32': ('axi5', 32, 'upt', {}),
                 's64': ('axi5', 64, 'upt', {'axi5': {'max_transaction_bytes': 64},
                                             'max_outstanding': 4}),
                 's128': ('axi5', 128, 'up', {})},
        targets={'m64': ('axi5', 64, 'upt', {'axi5': {'max_transaction_bytes': 64}}),
                 'm32': ('axi5', 32, 'up', {'axi5': {'max_transaction_bytes': 128},
                                            'max_outstanding': 2}),
                 't128': ('axi5', 128, 'upt', {})},
        limits=[],
        reach={'s32': ['m64', 'm32', 't128'], 's64': ['m64', 'm32', 't128'],
               's128': ['t128']}),
    # Two clock domains, a 64-bit pair without conversion and a 128-bit initiator into 32 bits.
    'clocked': dict(
        k=1, network=32, clocked=True,
        sources={'s64': ('axi5', 64, 'upt', {'response_user_width': 1}),
                 's128': ('axi5', 128, 'pt', {}), 's32': ('axi5', 32, 'u', {})},
        targets={'t64': ('axi5', 64, 'upt', {'response_user_width': 1}),
                 't32': ('axi5', 32, 'ut', {}), 't128': ('axi5', 128, 'p', {})},
        limits=[('s128', 't32', 32)],
        reach={'s64': ['t64'], 's128': ['t32', 't128'], 's32': ['t32', 't128']}),
}
BASES = 0x40000


def data_side(entry, ports, bits, k):
    if 'u' in ports:
        entry['data_user_width'] = bits // 8 * k
    if 'p' in ports:
        entry.setdefault('axi5', {})['poison'] = True
    if 't' in ports:
        entry.setdefault('axi5', {})['trace'] = True


def merge(entry, extra):
    for key, value in extra.items():
        if key == 'axi5':
            entry.setdefault('axi5', {}).update(value)
        else:
            entry[key] = value


def most_bytes(endpoint):
    return endpoint[3].get('axi5', {}).get('max_transaction_bytes', 4096)


def has(endpoint, part):
    if part == 'u':
        return bool(endpoint.get('data_user_width'))
    return bool(endpoint.get('axi5', {}).get({'p': 'poison', 't': 'trace'}[part]))


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
        data_side(entry, ports, bits, m['k'])
        merge(entry, extra)
        cfg['initiators'].append(entry)
    for index, (name, (protocol, bits, ports, extra)) in enumerate(m['targets'].items()):
        base = (index + 1) * BASES
        entry = {'name': name, 'clock_domain': dst_domain, 'protocol': protocol,
                 'data_width': bits, 'id_width': 4, 'len_width': 8, 'attributes': 'amba',
                 'mappings': [{'base': hex(base), 'size': hex(BASES), 'local_address': hex(base)}]}
        data_side(entry, ports, bits, m['k'])
        merge(entry, extra)
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
            # A source that declares Max_Transaction_Bytes sends no larger burst.
            while (length + 1) << size > most_bytes(m['sources'][s]):
                length = (length + 1) // 2 - 1
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
                # Some bursts start inside a 64-byte block, unless the source declares a maximum.
                if j % 4 == 2 and most_bytes(m['sources'][s]) == 4096:
                    addr += 0x30
            lo, hi = (start, start + span) if burst != 0 else (addr_lo, addr_hi)
            if burst == 1:
                lo, hi = addr, ((addr >> size) << size) + span
            kind = (0 if j % 2 == 0 else 1) if j < walks else 2
            t = dict(index=index, source=s, target=target, write=write, addr=addr, len=length,
                     size=size, burst=burst, id=j % 4, lo=lo, hi=hi, kind=kind,
                     pos=(j // 2) if j < walks else random.getrandbits(16),
                     trace=(j // 2) % 2 if j < walks else random.getrandbits(1),
                     flip=random.getrandbits(1),
                     fail=j >= walks and j % 6 == 4)
            out.append(t)
        # A full-width WRAP of 16 beats, unaligned, into each narrower target: the width
        # conversion cuts it into pieces, so the target's BTRACE on the first piece must reach the
        # initiator through the merged B.
        for target in reach[s]:
            if m['targets'][target][1] >= bits or 16 * bits // 8 > most_bytes(m['sources'][s]):
                continue
            for write in (True, False):
                base = (targets.index(target) + 1) * BASES
                start = base + slots[target] * 0x200
                slots[target] += 1
                span = 16 << beat_log
                out.append(dict(index=len(out), source=s, target=target, write=write,
                                addr=start + (5 << beat_log), len=15, size=beat_log, burst=2,
                                id=1, lo=start, hi=start + span, kind=2,
                                pos=random.getrandbits(16), trace=0, flip=1, fail=False))
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
            f"gs[{g}]={int(has(sources[t['source']], 'u'))}; "
            f"gq[{g}]={int(has(sources[t['source']], 'p'))}; "
            f"gt[{g}]={int(has(sources[t['source']], 't'))}; rq[{g}]={t['trace']}; "
            f"bf[{g}]={t['flip']}; "
            f"fl[{g}]={int(t['fail'])}; "
            f"want[{g}]={(t['hi'] - t['lo']) if t['burst'] else (t['len'] + 1) << t['size']};")
    def ruser_resp(e):
        return e.get('response_user_width', 0)
    drivers = []
    for si, (s, e) in enumerate(sources.items()):
        lanes = e['data_width'] // 8
        granules = max(1, lanes // 8)
        p = f'nl_i_{s}_'
        user, poison, trace = has(e, 'u'), has(e, 'p'), has(e, 't')
        rb = ruser_resp(e)
        calls = []
        for t in [x for x in txs if x['source'] == s]:
            call = 'write_' if t['write'] else 'read_'
            calls.append(f"{call}{s}({t['index']}, 32'h{t['addr']:x}, 8'd{t['len']}, "
                         f"3'd{t['size']}, 2'd{t['burst']}, 2'd{t['id']});")
        wuser = (f"{p}wuser[lane*{k} +: {k}] = grp(g, (b & ~32'd{lanes - 1}) + lane, 0);"
                 if user else '')
        # WPOISON of each granule of the beat: the poison bit of its eight-byte address granule.
        wpoison = (f"for (j = 0; j < {granules}; j = j + 1) "
                   f"{p}wpoison[j] = pfn(g, ((b & ~32'd{lanes - 1}) + 8 * j) >> 3, 0);"
                   if poison else '')
        # AxTRACE of the request, WTRACE of each beat from its 16-byte block. BTRACE and RTRACE hold
        # the request's bit OR the target's.
        # These accesses are Non-shareable. A CACHE value with allocation bits set is Normal
        # memory, including bursts whose width conversion keeps the original domain.
        aw_domain = f"{p}awdomain = 0; {p}awcache = 4'b1111;" if e['protocol'] == 'ace5_lite' else ''
        ar_domain = f"{p}ardomain = 0; {p}arcache = 4'b1111;" if e['protocol'] == 'ace5_lite' else ''
        wtrace = f"{p}awtrace = rq[g];" if trace else ''
        rtrace_set = f"{p}artrace = rq[g];" if trace else ''
        wtrace_beat = f"{p}wtrace = tfn(g, b >> 4, 0);" if trace else ''
        btrace_check = (f"check({p}btrace == (rq[g] | (tt[tg[g]] & bf[g])), "
                        f"$sformatf(\"BTRACE {s} g=%0d %b\", g, {p}btrace));" if trace else '')
        rcheck = ''
        if trace:
            rcheck += f'''
        check({p}rtrace == (rq[g] | (!fail && tt[tg[g]] && tfn(g, b >> 4, 5))),
              $sformatf("RTRACE {s} g=%0d k=%0d %b", g, k, {p}rtrace));'''
        if poison:
            rcheck += f'''
        for (j = 0; j < {granules}; j = j + 1)
          if (!(8 * j + 7 < (b % {lanes}) || 8 * j >= (b % {lanes}) + (1 << size) - (b % (1 << size))))
            check({p}rpoison[j] ==
                  (fail || !tq[tg[g]] ? 1'b0 : pfn(g, ((b & ~32'd{lanes - 1}) + 8 * j) >> 3, 5)),
                  $sformatf("RPOISON {s} g=%0d k=%0d granule=%0d %b", g, k, j, {p}rpoison[j]));'''
        ruser_check = ''
        if user:
            ruser_check = f'''
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
  integer k, lane, guard, j; reg [31:0] b; reg fail;
  begin
    fail = fl[g];
    {p}awaddr = a; {p}awlen = len; {p}awsize = size; {p}awburst = burst; {p}awid = id;
    {aw_domain} {wtrace}
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
      {wpoison}
      {wtrace_beat}
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
    {btrace_check}
    @(negedge {sclk}); {p}bready = 0;
  end
endtask
task automatic read_{s}(input integer g, input [31:0] a, input [7:0] len, input [2:0] size,
                        input [1:0] burst, input [1:0] id);
  integer k, lane, guard, j; reg [31:0] b; reg fail;
  begin
    fail = fl[g];
    {p}araddr = a; {p}arlen = len; {p}arsize = size; {p}arburst = burst; {p}arid = id;
    {ar_domain} {rtrace_set}
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
                          {p}rdata[lane*8 +: 8], rfn((b & ~32'd{lanes - 1}) + lane)));{ruser_check}
      end{rcheck}
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
        granules = max(1, lanes // 8)
        user, poison, trace = has(e, 'u'), has(e, 'p'), has(e, 't')
        rb = ruser_resp(e)
        wcheck = ''
        if user:
            wcheck = f'''
        check({p}wuser[lane*{k} +: {k}] ==
              ({p}wstrb[lane] && g >= 0 && gs[g] ? grp(g, (b & ~32'd{lanes - 1}) + lane, 0) : {k}'d0),
              $sformatf("WUSER {tname} g=%0d k=%0d lane=%0d strobe=%0d %h", g, k, lane,
                        {p}wstrb[lane], {p}wuser[lane*{k} +: {k}]));'''
        rdrive = ''
        if user:
            rdrive = (f"{p}ruser[{rb} + lane*{k} +: {k}] = "
                      f"g >= 0 ? grp(g, (b & ~32'd{lanes - 1}) + lane, 5) : {k}'d0;")
        # WPOISON of each granule: the OR of the poison bits of its strobed bytes.
        pcheck = ''
        prdrive = ''
        if poison:
            pcheck = f'''
      for (j = 0; j < {granules}; j = j + 1) begin
        want_poison = 0;
        for (lane = 8 * j; lane < 8 * j + 8 && lane < {lanes}; lane = lane + 1)
          if ({p}wstrb[lane] && g >= 0 && gq[g])
            want_poison = want_poison | pfn(g, ((b & ~32'd{lanes - 1}) + lane) >> 3, 0);
        check({p}wpoison[j] == want_poison,
              $sformatf("WPOISON {tname} g=%0d k=%0d granule=%0d %b", g, k, j, {p}wpoison[j]));
      end'''
            prdrive = (f"for (j = 0; j < {granules}; j = j + 1) {p}rpoison[j] = "
                       f"g >= 0 ? pfn(g, ((b & ~32'd{lanes - 1}) + 8 * j) >> 3, 5) : 1'b0;")
        resp_drive = f"{p}ruser[{rb - 1}:0] = tr[{ti}];" if rb else ''
        # A target with Max_Transaction_Bytes gets no burst larger, nor one that crosses it.
        most = e.get('axi5', {}).get('max_transaction_bytes', 4096)
        size_check = ''
        if most < 4096:
            # The first byte of the burst's run: a WRAP's container base, else its aligned start.
            first = ("(burst == 2'd2 ? a & ~(((len + 1) << size) - 1) : "
                     "a & ~((32'd1 << size) - 1))")
            size_check = (f"check(((len + 1) << size) <= {most} && (burst == 2'd0 || "
                          f"({first} / {most}) == "
                          f"(({first} + ((len + 1) << size) - 1) / {most})), "
                          f"$sformatf(\"MAX_TRANSACTION {tname} %h len=%0d size=%0d burst=%0d\", "
                          f"a, len, size, burst));")
        # AxTRACE comes from the request. WTRACE is the OR of the strobed bytes' trace bits, and
        # this target asserts BTRACE on a request's first packet when its flip bit is set.
        aw_trace = (f"check(g < 0 || {p}awtrace == (gt[g] & rq[g]), "
                    f"$sformatf(\"AWTRACE {tname} g=%0d %b\", g, {p}awtrace));" if trace else '')
        ar_trace = (f"check(g < 0 || {p}artrace == (gt[g] & rq[g]), "
                    f"$sformatf(\"ARTRACE {tname} g=%0d %b\", g, {p}artrace));" if trace else '')
        tcheck = ''
        if trace:
            tcheck = f'''
      want_trace = 0;
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if ({p}wstrb[lane] && g >= 0 && gt[g])
          want_trace = want_trace | tfn(g, ((b & ~32'd{lanes - 1}) + lane) >> 4, 0);
      check({p}wtrace == want_trace,
            $sformatf("WTRACE {tname} g=%0d k=%0d %b", g, k, {p}wtrace));'''
        btrace_drive = (f"{p}btrace = req_trace | (g >= 0 && hits[g] == 1 && bf[g]);"
                        if trace else '')
        rtrace_drive = (f"{p}rtrace = req_trace | (g >= 0 && tfn(g, b >> 4, 5));"
                        if trace else '')
        take_aw = f" req_trace = {p}awtrace;" if trace else ''
        take_ar = f" req_trace = {p}artrace;" if trace else ''
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
  integer g, k, lane, j; reg want_poison, want_trace, req_trace; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{idw - 1}:0] id;
  {p}awready = 0; {p}wready = 0; {p}bvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}awready = 1;
    @(posedge {tclk}); while (!{p}awvalid) @(posedge {tclk});
    a = {p}awaddr; len = {p}awlen; size = {p}awsize; burst = {p}awburst; id = {p}awid;{take_aw}
    g = find_{tname}(a, 1);
    check(g >= 0, $sformatf("AW_UNKNOWN {tname} %h", a));
    {aw_trace}
    {size_check}
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
      end{pcheck}{tcheck}
    end
    @(negedge {tclk}); {p}wready = 0; {p}bvalid = 1; {p}bid = id;
    {p}bresp = g >= 0 && fl[g] ? 2'd2 : 2'd0;
    {btrace_drive}
    @(posedge {tclk}); while (!{p}bready) @(posedge {tclk});
    @(negedge {tclk}); {p}bvalid = 0;
  end
end
initial begin : read_{tname}
  integer g, k, lane, j; reg req_trace; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{idw - 1}:0] id;
  {p}arready = 0; {p}rvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}arready = 1;
    @(posedge {tclk}); while (!{p}arvalid) @(posedge {tclk});
    a = {p}araddr; len = {p}arlen; size = {p}arsize; burst = {p}arburst; id = {p}arid;{take_ar}
    g = find_{tname}(a, 0);
    check(g >= 0, $sformatf("AR_UNKNOWN {tname} %h", a));
    {ar_trace}
    {size_check}
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
      {prdrive}
      {rtrace_drive}
      {p}rvalid = 1; {p}rid = id; {p}rresp = g >= 0 && fl[g] ? 2'd2 : 2'd0; {p}rlast = k == len;
      @(posedge {tclk}); while (!{p}rready) @(posedge {tclk});
      @(negedge {tclk}); {p}rvalid = 0;
    end
  end
end
''')
    init = [f"{p['name']} = 0;" for p in ports if p['direction'] == 'input'
            and not p['name'].startswith('clk_') and p['name'] != 'reset_n']
    th = ' '.join(f"th[{i}] = {int(has(t, 'u'))}; tq[{i}] = {int(has(t, 'p'))}; "
                  f"tt[{i}] = {int(has(t, 't'))};"
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
integer gk [0:{n - 1}]; integer gp [0:{n - 1}]; reg gs [0:{n - 1}]; reg gq [0:{n - 1}];
reg gt [0:{n - 1}]; reg rq [0:{n - 1}]; reg bf [0:{n - 1}];
reg fl [0:{n - 1}];
integer hits [0:{n - 1}]; integer seen [0:{n - 1}]; integer want [0:{n - 1}];
reg th [0:{len(targets) - 1}]; reg tq [0:{len(targets) - 1}]; reg tt [0:{len(targets) - 1}]; reg [7:0] tr [0:{len(targets) - 1}];
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
// The poison bit of an eight-byte granule: walking one (kind 0) or zero (kind 1) over four
// granules, else a hash.
function automatic pfn(input integer g, input [31:0] granule, input integer salt);
  integer pos; reg [31:0] h;
  begin
    pos = gp[g] + salt;
    if (gk[g] == 2) begin
      h = (granule * 32'h7feb352d) ^ (pos * 32'h846ca68b); h = h ^ (h >> 15);
      pfn = h[4];
    end else
      pfn = ((granule % 4) == (pos % 4)) ^ (gk[g] == 1);
  end
endfunction
// The trace bit of a 16-byte block: walking one or zero over four blocks, else a hash.
function automatic tfn(input integer g, input [31:0] block, input integer salt);
  integer pos; reg [31:0] h;
  begin
    pos = gp[g] + salt;
    if (gk[g] == 2) begin
      h = (block * 32'h68e31da4) ^ (pos * 32'hb5297a4d); h = h ^ (h >> 9);
      tfn = h[6];
    end else
      tfn = ((block % 4) == (pos % 4)) ^ (gk[g] == 1);
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
