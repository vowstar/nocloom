"""Shareable transactions end to end: AxDOMAIN and AxSNOOP of ACE5-Lite and AXI5 initiators at
every target, through width conversion, packet splitting and a clock domain crossing. Each target
burst finds its transaction by address. The bench checks the domain, the opcode and the data there,
and that a rejected transaction gets SLVERR with its beat count and ID and reaches no target."""
import argparse
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

LINE = 64
SH = (1, 2)  # the Shareable AxDOMAIN codes


def layout(mode):
    """Initiators: name -> (protocol, data bits, axi5 object). Targets: name -> (protocol, data
    bits, axi5 object). Packet limits as (source, target, bytes)."""
    if mode == 'line':
        sources = {f's{b}': ('ace5_lite', b, None) for b in (32, 128, 512)}
        targets = {f't{b}': ('ace5_lite', b, None) for b in (32, 64, 128, 256, 512, 1024)}
        return sources, targets, [('s128', 't256', 32)]
    if mode == 'plain':
        return ({'plain': ('axi4', 32, None), 'wide': ('axi4', 128, None)},
                {'t64': ('ace5_lite', 64, None), 't32': ('axi5', 32, {'shareable': True})}, [])
    sources = {
        'ace': ('ace5_lite', 128, {'nsaid': True}),
        'axs': ('axi5', 32, {'shareable': True, 'write_snoop_width': 4}),
        'plain': ('axi4', 32, None),
    }
    targets = {
        't64': ('ace5_lite', 64, {'nsaid': True}),
        't32': ('axi5', 32, {'shareable': True, 'write_snoop_width': 4, 'read_snoop_width': 4}),
        'tq': ('ace5_lite', 32, {'write_snoop_width': 0, 'read_snoop_width': 0}),
        't4': ('axi4', 32, None),
    }
    return sources, targets, [('ace', 't64', 32), ('ace', 't32', 64)]




def windows(name, k):
    """The (global base, local address, bytes) windows of target k: one 64 KiB window, a page off
    its global base for t32."""
    base = k << 16
    return [(base, base + (0x1000 if name == 't32' else 0), 0x10000)]


def shareable_ports(protocol, axi5):
    """(AxDOMAIN, AWSNOOP, ARSNOOP) port presence with the protocol defaults."""
    axi5 = axi5 or {}
    if protocol == 'axi4':
        return False, False, False
    ace = protocol == 'ace5_lite'
    return (axi5.get('shareable', ace), axi5.get('write_snoop_width', 4 if ace else 0) == 4,
            axi5.get('read_snoop_width', 4 if ace else 0) == 4)


def configuration(mode):
    clocked = mode == 'clocked'
    # Each initiator keeps up to four bursts in flight per direction.
    deep = mode == 'outstanding'
    src, dst = ('fast', 'slow') if clocked else ('main', 'main')
    domains = ['fast', 'slow'] if clocked else ['main']
    sources, targets, limits = layout(mode)
    cfg = {
        'schema_version': 10, 'name': 'ace5_lite', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [], 'targets': [], 'connectivity': 'all',
        'transport': {'data_width': 32, **({'max_outstanding': 4} if deep else {})},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if limits:
        cfg['transport']['packet_limits'] = [{'source': s, 'target': t, 'max_bytes': b}
                                             for s, t, b in limits]
    for name, (protocol, bits, axi5) in sources.items():
        entry = {'name': name, 'clock_domain': src, 'protocol': protocol, 'data_width': bits,
                 'id_width': 2, 'len_width': 8}
        if axi5:
            entry['axi5'] = axi5
        cfg['initiators'].append(entry)
    for k, (name, (protocol, bits, axi5)) in enumerate(targets.items()):
        entry = {'name': name, 'clock_domain': dst, 'protocol': protocol, 'data_width': bits,
                 'id_width': 4, 'len_width': 8, 'attributes': 'amba',
                 'mappings': [{'base': hex(g), 'size': hex(n), 'local_address': hex(a)}
                              for g, a, n in windows(name, k)]}
        if axi5:
            entry['axi5'] = axi5
        cfg['targets'].append(entry)
    if clocked:
        def plane(entries, exits, link):
            return {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                              {'name': 'leaf', 'clock_domain': 'slow'}],
                    'links': [link], 'entries': entries, 'exits': exits}
        cfg['topology'] = {
            'request': plane([{'endpoint': s, 'node': 'root'} for s in sources],
                             [{'endpoint': t, 'node': 'leaf'} for t in targets],
                             {'from': 'root', 'to': 'leaf'}),
            'response': plane([{'endpoint': t, 'node': 'leaf'} for t in targets],
                              [{'endpoint': s, 'node': 'root'} for s in sources],
                              {'from': 'leaf', 'to': 'root'})}
    return cfg


def line_at_source(t, bytes_):
    """One Regular 64-byte cache line on a bus of bytes_ bytes, from an aligned address."""
    total = (t['len'] + 1) << t['size']
    regular = t['len'] == 0 or (1 << t['size']) == bytes_
    return total == LINE and regular and t['addr'] % LINE == 0 and t['burst'] in (1, 2)


def default_domain(cache):
    return 0 if cache & 0xC else 3


class Plan:
    def __init__(self, mode, seed):
        self.mode = mode
        self.sources, self.targets, _ = layout(mode)
        self.txs = []
        self.slots = {name: 0 for name in self.targets}
        random.seed(seed)

    def slot(self, target):
        """A 512-byte slot of the target, aligned to it."""
        base = list(self.targets).index(target) << 16
        start = base + self.slots[target] * 0x200
        self.slots[target] += 1
        return start

    def add(self, source, target, write, length, size, burst, domain=0, opcode=0, lock=0,
            cache=0, offset=0, rejected=False):
        protocol, bits, axi5 = self.sources[source]
        dport, wport, rport = shareable_ports(protocol, axi5)
        start = self.slot(target)
        span = (length + 1) << size
        addr = start + offset
        nsaid = random.getrandbits(4)
        t = dict(index=len(self.txs), source=source, target=target, write=write, addr=addr,
                 len=length, size=size, burst=burst, id=len(self.txs) % 4, lock=lock,
                 cache=cache, prot=random.getrandbits(3), domain_in=domain, opcode=opcode,
                 nsaid_in=nsaid, nsaid=nsaid if (axi5 or {}).get('nsaid') else 0, lo=start,
                 hi=addr + span, resp=2 if rejected else 0)
        effective = domain if dport else default_domain(cache)
        t['domain'] = effective
        t['wuf'] = int(write and opcode == 1 and effective in SH and not rejected and
                       line_at_source(t, bits // 8))
        if burst == 2:
            container = span
            t['lo'] = start - (start % container) if container else start
        t['drive'] = {}
        if dport:
            t['drive']['domain'] = (2, domain)
        if (wport if write else rport):
            t['drive']['snoop'] = (4, opcode)
        if (axi5 or {}).get('nsaid'):
            t['drive']['nsaid'] = (4, t['nsaid_in'])
        self.txs.append(t)
        return t

    def legal(self):
        """Legal transactions of each source to each target: each domain value on each channel,
        WriteUniqueFull lines as INCR and WRAP, a WriteUniqueFull that is not a line, and
        exclusive and FIXED accesses outside the Shareable domain."""
        caches = (0, 1, 2, 3, 6, 7, 10, 11, 14, 15)
        for s, (protocol, bits, axi5) in self.sources.items():
            dport, wport, _ = shareable_ports(protocol, axi5)
            word = (bits // 8).bit_length() - 1
            j = 0
            for target in self.targets:
                for write in (True, False):
                    for domain in range(4):
                        j += 1
                        # Without AxDOMAIN ports, a Device and a Cacheable AxCACHE in turn.
                        cache = random.choice(caches[:4] if domain % 2 else caches[4:])
                        shared = (domain if dport else default_domain(cache)) in SH
                        length = (j * 5) % 8
                        size = word if j % 3 else max(0, word - 1)
                        burst = 2 if j % 4 == 1 and length in (1, 3, 7) else 1
                        lock = 0
                        if not shared and j % 5 == 2:
                            # A FIXED beat no wider than the narrowest target.
                            burst, size = 0, min(size, 2)
                        if not shared and j % 5 == 4:
                            lock, burst, length = 1, 1, 0
                        offset = (j % (length + 1)) << size if burst == 2 else 0
                        self.add(s, target, write, length, size, burst, domain, 0, lock, cache,
                                 offset)
                        if not (write and wport and shared):
                            continue
                        # A WriteUniqueFull of one line, as INCR and as a WRAP from the line
                        # start.
                        beats = LINE // (bits // 8)
                        for burst in (1, 2):
                            self.add(s, target, True, beats - 1, word, burst, domain, 1,
                                     cache=cache)
                        # WriteUniqueFull requests that are not one Regular line at the source
                        # travel as WriteUniquePtl: a line from a misaligned start, two lines,
                        # and one line of narrow beats.
                        self.add(s, target, True, beats - 1, word, 1, domain, 1, cache=cache,
                                 offset=1 << word)
                        self.add(s, target, True, 2 * beats - 1, word, 1, domain, 1, cache=cache)
                        self.add(s, target, True, 2 * beats - 1, word - 1, 1, domain, 1,
                                 cache=cache)

    def rejected(self):
        """One transaction of each refused opcode and pair (docs/ace5-lite.md)."""
        if 'ace' not in self.sources:
            return
        names = [n for n in self.targets]
        k = 0

        def refuse(source, write, opcode, domain, lock=0, burst=1):
            nonlocal k
            bits = self.sources[source][1]
            word = (bits // 8).bit_length() - 1
            target = names[k % len(names)]
            k += 1
            self.add(source, target, write, 3 if write else 2, word, burst, domain, opcode, lock,
                     cache=2, rejected=True)
        for domain in (0, 3):
            refuse('ace', True, 1, domain)  # WriteNoSnoopFull
        for opcode in range(2, 16):
            refuse('ace', True, opcode, 2)
        for opcode in range(1, 16):
            refuse('ace', False, opcode, 1)
        for write in (True, False):
            refuse('ace', write, 0, 1, lock=1)
            refuse('ace', write, 0, 2, burst=0)
        refuse('ace', True, 1, 2, lock=1)
        refuse('axs', True, 7, 1)
        refuse('axs', True, 1, 3)
        refuse('axs', False, 0, 2, burst=0)

    def lines(self):
        """A WriteUniqueFull line from each source to each target, and a WriteUniquePtl."""
        for s, (_, bits, _) in self.sources.items():
            word = (bits // 8).bit_length() - 1
            for target in self.targets:
                if bits // 8 <= LINE:
                    self.add(s, target, True, LINE // (bits // 8) - 1, word, 1, 2, 1, cache=15)
                else:
                    self.add(s, target, True, 0, 6, 1, 1, 1, cache=15)
                self.add(s, target, True, 0, 2, 1, 2, 0, cache=15)
                self.add(s, target, False, 0, 2, 1, 1, 0, cache=15)


def address_sets(p, t):
    """The address channel inputs of transaction t on initiator prefix p."""
    ch = 'aw' if t['write'] else 'ar'
    sets = [f"{p}{ch}addr = 32'h{t['addr']:x};", f"{p}{ch}size = 3'd{t['size']};",
            f"{p}{ch}burst = 2'd{t['burst']};", f"{p}{ch}id = 2'd{t['id']};",
            f"{p}{ch}len = 8'd{t['len']};", f"{p}{ch}lock = 1'b{t['lock']};",
            f"{p}{ch}cache = 4'd{t['cache']};", f"{p}{ch}prot = 3'd{t['prot']};"]
    return sets + [f"{p}{ch}{k} = {w}'d{v};" for k, (w, v) in t['drive'].items()]


def pipelined_driver(s, si, lanes, txs, sclk):
    """An initiator that issues each direction's requests back to back, sends write data in AW
    order and takes each response for the oldest open request of its ID. A response's ID and
    result must match that request, read data and RLAST its beat. Each direction must reach two
    bursts in flight."""
    p = f'nl_i_{s}_'
    ws = [t for t in txs if t['source'] == s and t['write']]
    rs = [t for t in txs if t['source'] == s and not t['write']]
    nw, nr = max(1, len(ws)), max(1, len(rs))
    rows = []
    for d, seq in (('w', ws), ('r', rs)):
        for k, t in enumerate(seq):
            rows.append(f"{d}a_{s}[{k}]=32'h{t['addr']:x}; {d}l_{s}[{k}]={t['len']}; "
                        f"{d}z_{s}[{k}]={t['size']}; {d}b_{s}[{k}]={t['burst']}; "
                        f"{d}i_{s}[{k}]={t['id']}; {d}e_{s}[{k}]={t['resp']};")
    issue_w = [' '.join(address_sets(p, t)) + f' issue_aw_{s}();' for t in ws]
    issue_r = [' '.join(address_sets(p, t)) + f' issue_ar_{s}();' for t in rs]
    nl = chr(10)
    return f'''
reg [31:0] wa_{s} [0:{nw - 1}]; integer wl_{s} [0:{nw - 1}]; integer wz_{s} [0:{nw - 1}];
integer wb_{s} [0:{nw - 1}]; integer wi_{s} [0:{nw - 1}]; integer we_{s} [0:{nw - 1}];
reg wd_{s} [0:{nw - 1}];
reg [31:0] ra_{s} [0:{nr - 1}]; integer rl_{s} [0:{nr - 1}]; integer rz_{s} [0:{nr - 1}];
integer rb_{s} [0:{nr - 1}]; integer ri_{s} [0:{nr - 1}]; integer re_{s} [0:{nr - 1}];
integer rk_{s} [0:{nr - 1}];
integer aw_{s} = 0, b_{s} = 0, ar_{s} = 0, r_{s} = 0, wdeep_{s} = 0, rdeep_{s} = 0;
initial begin : rows_{s}
  integer k;
  for (k = 0; k < {nw}; k = k + 1) wd_{s}[k] = 0;
  for (k = 0; k < {nr}; k = k + 1) rk_{s}[k] = 0;
{nl.join('  ' + r for r in rows)}
end
task automatic issue_aw_{s};
  integer guard;
  begin
    {p}awvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}awready) begin guard = guard + 1; check(guard < 20000, "AW_HANG"); @(posedge {sclk}); end
    aw_{s} = aw_{s} + 1;
    if (aw_{s} - b_{s} > wdeep_{s}) wdeep_{s} = aw_{s} - b_{s};
    @(negedge {sclk}); {p}awvalid = 0;
  end
endtask
task automatic issue_ar_{s};
  integer guard;
  begin
    {p}arvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}arready) begin guard = guard + 1; check(guard < 20000, "AR_HANG"); @(posedge {sclk}); end
    ar_{s} = ar_{s} + 1;
    if (ar_{s} - r_{s} > rdeep_{s}) rdeep_{s} = ar_{s} - r_{s};
    @(negedge {sclk}); {p}arvalid = 0;
  end
endtask
initial begin : aw_thread_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
{nl.join('  ' + c for c in issue_w)}
end
initial begin : ar_thread_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
{nl.join('  ' + c for c in issue_r)}
end
initial begin : w_thread_{s}
  integer k, j, lane, guard; reg [31:0] b;
  wait (reset_n);
  for (k = 0; k < {len(ws)}; k = k + 1) begin
    wait (aw_{s} > k); @(negedge {sclk});
    for (j = 0; j <= wl_{s}[k]; j = j + 1) begin
      b = beat(wa_{s}[k], wl_{s}[k], wz_{s}[k], wb_{s}[k], j);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        {p}wdata[lane*8 +: 8] = wfn((b & ~32'd{lanes - 1}) + lane);
        {p}wstrb[lane] = lane >= (b % {lanes}) &&
                          lane < (b % {lanes}) + (1 << wz_{s}[k]) - (b % (1 << wz_{s}[k]));
      end
      {p}wlast = j == wl_{s}[k]; {p}wvalid = 1;
      @(posedge {sclk}); guard = 0;
      while (!{p}wready) begin guard = guard + 1; check(guard < 20000, "W_HANG"); @(posedge {sclk}); end
      @(negedge {sclk}); {p}wvalid = 0;
    end
  end
end
initial begin : b_thread_{s}
  integer k, open, guard;
  wait (reset_n); {p}bready = 1;
  while (b_{s} < {len(ws)}) begin
    @(posedge {sclk}); guard = 0;
    while (!{p}bvalid) begin guard = guard + 1; check(guard < 40000, "B_HANG"); @(posedge {sclk}); end
    open = -1;
    for (k = {len(ws) - 1}; k >= 0; k = k - 1)
      if (k < aw_{s} && !wd_{s}[k] && wi_{s}[k] == {p}bid) open = k;
    check(open >= 0, $sformatf("B_UNEXPECTED {s} id=%0d", {p}bid));
    if (open >= 0) begin
      check({p}bresp == we_{s}[open], $sformatf("B_RESPONSE {s} %h resp=%0d/%0d", wa_{s}[open],
            {p}bresp, we_{s}[open]));
      if (we_{s}[open] == 2) refused = refused + 1;
      wd_{s}[open] = 1;
    end
    b_{s} = b_{s} + 1;
  end
end
initial begin : r_thread_{s}
  integer k, open, lane, guard; reg [31:0] b;
  wait (reset_n); {p}rready = 1;
  while (r_{s} < {len(rs)}) begin
    @(posedge {sclk}); guard = 0;
    while (!{p}rvalid) begin guard = guard + 1; check(guard < 40000, "R_HANG"); @(posedge {sclk}); end
    open = -1;
    for (k = {len(rs) - 1}; k >= 0; k = k - 1)
      if (k < ar_{s} && rk_{s}[k] <= rl_{s}[k] && ri_{s}[k] == {p}rid) open = k;
    check(open >= 0, $sformatf("R_UNEXPECTED {s} id=%0d", {p}rid));
    if (open >= 0) begin
      check({p}rresp == re_{s}[open] && {p}rlast == (rk_{s}[open] == rl_{s}[open]),
            $sformatf("R_RESPONSE {s} %h k=%0d resp=%0d/%0d last=%0d", ra_{s}[open],
                      rk_{s}[open], {p}rresp, re_{s}[open], {p}rlast));
      b = beat(ra_{s}[open], rl_{s}[open], rz_{s}[open], rb_{s}[open], rk_{s}[open]);
      if (re_{s}[open] == 0)
        for (lane = 0; lane < {lanes}; lane = lane + 1)
          if (lane >= (b % {lanes}) &&
              lane < (b % {lanes}) + (1 << rz_{s}[open]) - (b % (1 << rz_{s}[open])))
            check({p}rdata[lane*8 +: 8] == rfn((b & ~32'd{lanes - 1}) + lane), "R_DATA");
      if (re_{s}[open] == 2) check({p}rdata == 0, "R_REFUSED_DATA");
      if (rk_{s}[open] == rl_{s}[open]) begin
        r_{s} = r_{s} + 1;
        if (re_{s}[open] == 2) refused = refused + 1;
      end
      rk_{s}[open] = rk_{s}[open] + 1;
    end
  end
end
initial begin : finish_{s}
  wait (b_{s} == {len(ws)} && r_{s} == {len(rs)});
  check(wdeep_{s} >= 2 && rdeep_{s} >= 2,
        $sformatf("IN_FLIGHT {s} writes=%0d reads=%0d", wdeep_{s}, rdeep_{s}));
  $display("IN_FLIGHT {s} writes=%0d reads=%0d", wdeep_{s}, rdeep_{s});
  done[{si}] = 1;
end
'''


def bench(cfg, ports, txs, seed):
    names = {p['name']: p for p in ports}
    sources = {i['name']: i['data_width'] for i in cfg['initiators']}
    targets = {x['name']: x['data_width'] for x in cfg['targets']}
    decl = []
    for p in ports:
        if p['name'].startswith('clk_'):
            continue
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f"{kind} [{p['width'] - 1}:0] {p['name']};")
    conn = ', '.join(f".{p['name']}({p['name']})" for p in ports)
    clocked = len(cfg['clock_domains']) > 1
    sclk, tclk = ('clk_fast', 'clk_slow') if clocked else ('clk_main', 'clk_main')
    clocks = ('always #5 clk_fast = ~clk_fast;\nalways #7 clk_slow = ~clk_slow;' if clocked
              else 'always #5 clk_main = ~clk_main;')
    n = len(txs)
    init_rows = []
    for t in txs:
        g = t['index']
        init_rows.append(
            f"lo[{g}]=32'h{t['lo']:x}; hi[{g}]=32'h{t['hi']:x}; wr[{g}]={int(t['write'])}; "
            f"tg[{g}]={list(targets).index(t['target'])}; want[{g}]={(t['len'] + 1) << t['size']};"
            f" reach[{g}]={int(t['resp'] == 0)}; e_domain[{g}]=2'd{t['domain']}; "
            f"wuf[{g}]={t['wuf']}; e_nsaid[{g}]=4'd{t['nsaid']};")
    drivers = []
    pipelined = cfg['transport'].get('max_outstanding', 1) > 1
    for si, (s, bits) in enumerate(sources.items()):
        lanes = bits // 8
        p = f'nl_i_{s}_'
        if pipelined:
            drivers.append(pipelined_driver(s, si, lanes, txs, sclk))
            continue
        calls = []
        for t in (x for x in txs if x['source'] == s):
            sets = address_sets(p, t)
            call = 'write_' if t['write'] else 'read_'
            calls.append(' '.join(sets) + f" {call}{s}(32'h{t['addr']:x}, 8'd{t['len']}, "
                         f"3'd{t['size']}, 2'd{t['burst']}, 2'd{t['id']}, 2'd{t['resp']});")
        drivers.append(f'''
task automatic write_{s}(input [31:0] a, input [7:0] len, input [2:0] size, input [1:0] burst,
                         input [1:0] id, input [1:0] resp);
  integer k, lane, guard; reg [31:0] b;
  begin
    {p}awvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}awready) begin guard = guard + 1; check(guard < 20000, "AW_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}awvalid = 0;
    for (k = 0; k <= len; k = k + 1) begin
      b = beat(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        {p}wdata[lane*8 +: 8] = wfn((b & ~32'd{lanes - 1}) + lane);
        {p}wstrb[lane] = lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size));
      end
      {p}wlast = k == len; {p}wvalid = 1;
      @(posedge {sclk}); guard = 0;
      while (!{p}wready) begin guard = guard + 1; check(guard < 20000, "W_HANG"); @(posedge {sclk}); end
      @(negedge {sclk}); {p}wvalid = 0;
    end
    {p}bready = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}bvalid) begin guard = guard + 1; check(guard < 40000, "B_HANG"); @(posedge {sclk}); end
    check({p}bresp == resp && {p}bid == id,
          $sformatf("B_RESPONSE {s} %h resp=%0d/%0d id=%0d/%0d", a, {p}bresp, resp, {p}bid, id));
    if (resp == 2) refused = refused + 1;
    @(negedge {sclk}); {p}bready = 0;
  end
endtask
task automatic read_{s}(input [31:0] a, input [7:0] len, input [2:0] size, input [1:0] burst,
                        input [1:0] id, input [1:0] resp);
  integer k, lane, guard; reg [31:0] b;
  begin
    {p}arvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}arready) begin guard = guard + 1; check(guard < 20000, "AR_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}arvalid = 0; {p}rready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      @(posedge {sclk}); guard = 0;
      while (!{p}rvalid) begin guard = guard + 1; check(guard < 40000, "R_HANG"); @(posedge {sclk}); end
      check({p}rresp == resp && {p}rid == id && {p}rlast == (k == len),
            $sformatf("R_RESPONSE {s} %h k=%0d resp=%0d/%0d id=%0d/%0d last=%0d", a, k, {p}rresp,
                      resp, {p}rid, id, {p}rlast));
      b = beat(a, len, size, burst, k);
      if (resp == 0)
        for (lane = 0; lane < {lanes}; lane = lane + 1)
          if (lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size)))
            check({p}rdata[lane*8 +: 8] == rfn((b & ~32'd{lanes - 1}) + lane), "R_DATA");
      // A refused read returns zero data.
      if (resp == 2) check({p}rdata == 0, "R_REFUSED_DATA");
    end
    if (resp == 2) refused = refused + 1;
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
    for ti, (tname, bits) in enumerate(targets.items()):
        lanes = bits // 8
        word = lanes.bit_length() - 1
        p = f'nl_t_{tname}_'

        def has(signal):
            return p + signal in names
        aw = []
        ar = []
        if has('awdomain'):
            aw.append(f'check({p}awdomain == e_domain[g], $sformatf("AW_DOMAIN {tname} %0d %0d/%0d", '
                      f'g, {p}awdomain, e_domain[g]));')
            ar.append(f'check({p}ardomain == e_domain[g], $sformatf("AR_DOMAIN {tname} %0d %0d/%0d", '
                      f'g, {p}ardomain, e_domain[g]));')
        if has('awsnoop'):
            aw.append(f'opcode = wuf[g] && line(la, len, size, burst, {word}) ? 4\'d1 : 4\'d0; '
                      f'check({p}awsnoop == opcode, $sformatf("AW_SNOOP {tname} %0d %0d/%0d %h '
                      f'%0d %0d", g, {p}awsnoop, opcode, a, len, size)); '
                      f'if ({p}awsnoop == 1) full_lines = full_lines + 1; '
                      f'else if (wuf[g]) partial_lines = partial_lines + 1;')
        if has('arsnoop'):
            ar.append(f'check({p}arsnoop == 0, "AR_SNOOP {tname}");')
        if has('awnsaid'):
            aw.append(f'check({p}awnsaid == e_nsaid[g], "AW_NSAID {tname}");')
            ar.append(f'check({p}arnsaid == e_nsaid[g], "AR_NSAID {tname}");')
        convert = ' else '.join(
            f"if (la >= 32'h{a:x} && la < 32'h{a + n:x}) global_{tname} = la - 32'h{a:x} + 32'h{g:x};"
            for g, a, n in windows(tname, ti))
        responders.append(f'''
function automatic [31:0] global_{tname}(input [31:0] la);
  begin
    global_{tname} = 32'hffffffff;
    {convert}
  end
endfunction
function automatic integer find_{tname}(input [31:0] a, input w);
  integer g;
  begin
    find_{tname} = -1;
    for (g = 0; g < {n}; g = g + 1)
      if (tg[g] == {ti} && wr[g] == w && a >= lo[g] && a < hi[g]) find_{tname} = g;
  end
endfunction
initial begin : write_{tname}
  integer g, k, lane; reg [31:0] la, a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [3:0] opcode;
  reg [{names[p + 'awid']['width'] - 1}:0] id;
  {p}awready = 0; {p}wready = 0; {p}bvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}awready = 1;
    @(posedge {tclk}); while (!{p}awvalid) @(posedge {tclk});
    la = {p}awaddr; a = global_{tname}(la);
    len = {p}awlen; size = {p}awsize; burst = {p}awburst; id = {p}awid;
    g = find_{tname}(a, 1);
    check(g >= 0, $sformatf("AW_UNKNOWN {tname} %h", a));
    if (g >= 0) begin
      check(reach[g], $sformatf("REJECTED_REACHED {tname} %0d", g));
      {' '.join(aw)}
      hits[g] = hits[g] + 1;
    end
    @(negedge {tclk}); {p}awready = 0; {p}wready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      @(posedge {tclk}); while (!{p}wvalid) @(posedge {tclk});
      b = beat(a, len, size, burst, k);
      check({p}wlast == (k == len), "W_LAST");
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if ({p}wstrb[lane]) begin
          check({p}wdata[lane*8 +: 8] == wfn((b & ~32'd{lanes - 1}) + lane), "W_DATA");
          if (g >= 0) seen[g] = seen[g] + 1;
        end
    end
    @(negedge {tclk}); {p}wready = 0; {p}bvalid = 1; {p}bid = id; {p}bresp = 0;
    @(posedge {tclk}); while (!{p}bready) @(posedge {tclk});
    @(negedge {tclk}); {p}bvalid = 0;
  end
end
initial begin : read_{tname}
  integer g, k, lane; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{names[p + 'arid']['width'] - 1}:0] id;
  {p}arready = 0; {p}rvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}arready = 1;
    @(posedge {tclk}); while (!{p}arvalid) @(posedge {tclk});
    a = global_{tname}({p}araddr);
    len = {p}arlen; size = {p}arsize; burst = {p}arburst; id = {p}arid;
    g = find_{tname}(a, 0);
    check(g >= 0, $sformatf("AR_UNKNOWN {tname} %h", a));
    if (g >= 0) begin
      check(reach[g], $sformatf("REJECTED_REACHED {tname} %0d", g));
      {' '.join(ar)}
      hits[g] = hits[g] + 1;
    end
    @(negedge {tclk}); {p}arready = 0;
    for (k = 0; k <= len; k = k + 1) begin
      b = beat(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        {p}rdata[lane*8 +: 8] = rfn((b & ~32'd{lanes - 1}) + lane);
      {p}rvalid = 1; {p}rid = id; {p}rresp = 0; {p}rlast = k == len;
      @(posedge {tclk}); while (!{p}rready) @(posedge {tclk});
      @(negedge {tclk}); {p}rvalid = 0;
    end
  end
end
''')
    init = [f"{p['name']} = 0;" for p in ports if p['direction'] == 'input'
            and not p['name'].startswith('clk_') and p['name'] != 'reset_n']
    return f'''`timescale 1ns/1ps
module tb;
reg clk_main = 0, clk_fast = 0, clk_slow = 0;
{clocks}
{chr(10).join(decl)}
nocloom_ace5_lite dut({conn});
integer errors = 0, refused = 0, full_lines = 0, partial_lines = 0;
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
function automatic [31:0] beat(input [31:0] a, input [7:0] len, input [2:0] size,
                               input [1:0] burst, input integer k);
  reg [31:0] step, container, base;
  begin
    step = 32'd1 << size;
    if (burst == 2'd0) beat = a;
    else if (burst == 2'd2) begin
      container = step * (len + 1); base = a & ~(container - 1);
      beat = base + ((a - base + k * step) % container);
    end else beat = (a & ~(step - 1)) + k * step;
  end
endfunction
// One Regular 64-byte cache line on a port of 2^word bytes (IHI 0022 Issue L, A3.1.8 and A8.2).
function automatic line(input [31:0] a, input [7:0] len, input [2:0] size, input [1:0] burst,
                        input integer word);
  line = ((len + 1) << size) == {LINE} && a % {LINE} == 0 && (burst == 1 || burst == 2) &&
         (len == 0 || size == word);
endfunction
reg [31:0] lo [0:{n - 1}]; reg [31:0] hi [0:{n - 1}]; reg wr [0:{n - 1}]; integer tg [0:{n - 1}];
reg reach [0:{n - 1}]; reg wuf [0:{n - 1}]; reg [1:0] e_domain [0:{n - 1}];
reg [3:0] e_nsaid [0:{n - 1}];
integer hits [0:{n - 1}]; integer seen [0:{n - 1}]; integer want [0:{n - 1}];
reg [{len(sources) - 1}:0] done = 0;
integer i;
initial begin
  for (i = 0; i < {n}; i = i + 1) begin hits[i] = 0; seen[i] = 0; end
  {chr(10).join('  ' + r for r in init_rows)}
end
initial begin
  {' '.join(init)}
  reset_n = 0;
  repeat (8) @(negedge {tclk});
  reset_n = 1;
end
{''.join(drivers)}
{''.join(responders)}
integer reached, split;
initial begin
  wait (done == {{{len(sources)}{{1'b1}}}});
  repeat (50) @(negedge {tclk});
  reached = 0; split = 0;
  for (i = 0; i < {n}; i = i + 1) begin
    if (reach[i]) begin
      check(hits[i] >= 1, $sformatf("NO_TARGET_BURST %0d", i));
      if (wr[i]) check(seen[i] == want[i], $sformatf("WRITE_BYTES %0d", i));
      reached = reached + 1;
      if (hits[i] > 1) split = split + 1;
    end
  end
  $display("ACE5_LITE transactions={n} reached=%0d refused=%0d split=%0d full=%0d partial=%0d seed={seed}",
           reached, refused, split, full_lines, partial_lines);
  if (errors == 0) $display("CHECK_PASS");
  else $display("CHECK_FAIL: %0d errors", errors);
  $finish;
end
initial begin
  #20000000;
  $display("CHECK_FAIL: TIMEOUT done=%b", done);
  $finish;
end
endmodule
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--mode', choices=('switch', 'clocked', 'line', 'plain', 'outstanding'),
                    required=True)
    ap.add_argument('--seed', type=int, default=11)
    args = ap.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    cfg = configuration(args.mode)
    (work / 'ace5_lite.yaml').write_text(json.dumps(cfg, indent=1) + '\n')
    out = work / 'generated'
    subprocess.run([args.cli, 'generate', str(work / 'ace5_lite.yaml'), '--out-dir', str(out)],
                   check=True, capture_output=True, text=True, timeout=600)
    mapping = json.loads((out / 'nocloom_ace5_lite.map.json').read_text())
    (work / 'network.v').write_text(generated_rtl(out / 'nocloom_ace5_lite.v'))
    plan = Plan(args.mode, args.seed)
    if args.mode == 'line':
        plan.lines()
    else:
        plan.legal()
        plan.rejected()
    txs = plan.txs
    (work / 'tb.sv').write_text(bench(cfg, mapping['ports'], txs, args.seed))
    code = simulate(work, base / 'compiled')
    log = (work / 'simulation.log').read_text()
    found = re.search(r'reached=(\d+) refused=(\d+) split=(\d+) full=(\d+) partial=(\d+)', log)
    counts = [int(x) for x in found.groups()] if found else None
    want_refused = sum(t['resp'] == 2 for t in txs)
    # Both outcomes of a WriteUniqueFull occur: kept as one line, and travelling as a
    # WriteUniquePtl after a split or a narrow port.
    # Without an AxDOMAIN initiator no request is a WriteUniqueFull.
    lines = counts and (counts[3] == counts[4] == 0 if args.mode == 'plain'
                        else counts[3] > 0 and counts[4] > 0)
    passed = (code == 0 and 'CHECK_PASS' in log and 'CHECK_FAIL' not in log and counts and
              counts[1] == want_refused and lines)
    print(log[-3000:] if not passed else log.splitlines()[0])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
