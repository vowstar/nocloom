"""Schema 10 AHB-Lite initiators through the public CLI.

An AHB initiator is an AHB-Lite subordinate face in front of a full AXI4 source of its width
(docs/ahb.md#ahb-lite-initiators). The bench drives a pipelined AHB-Lite manager per initiator: NONSEQ
and SEQ transfers, BUSY inside bursts, the manager cancelling a burst after an ERROR or
continuing it, every HBURST, narrow SINGLE transfers, and transfers outside the face's profile
(HMASTLOCK, a narrow beat of a burst, HSIZE above the bus). Target models of every protocol keep
byte memories with an error region. An independent oracle predicts from the docs/protocols.md rules:

- every beat's response (OKAY or the two-cycle ERROR) and the read data of OKAY beats;
- the bursts an AXI4 target of the source's width receives: SINGLE one beat of its HSIZE,
  INCR4/8/16 and WRAP4/8/16 one burst, and undefined INCR one piece per committed beat;
- every target's final memory.

A checker on each AHB face asserts that HREADYOUT is high outside a data phase (IDLE and BUSY
complete with no wait state), that HRESP is high only in a data phase, and that ERROR takes two
cycles: HREADYOUT low, then high, both with HRESP high. A common reset in the middle of a read
burst cancels it; the transfers after it complete exactly.

Modes: switch (one 32-bit initiator, the generated switch, AXI4, APB4, AHB and AXI4-Lite
targets), wide (64- and 128-bit initiators in a 32-bit network with per-target widths) and
clocked (initiator and targets in different clock domains on a two-node graph)."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

SINGLE, INCR = 0, 1
HBURST = {'SINGLE': 0, 'INCR': 1, 'WRAP4': 2, 'INCR4': 3, 'WRAP8': 4, 'INCR8': 5, 'WRAP16': 6, 'INCR16': 7}
FIXED_BEATS = {2: 4, 3: 4, 4: 8, 5: 8, 6: 16, 7: 16}
NONBUF, BUF = 0x3, 0x7
ERR_LO, ERR_HI = 0xF00, 0xFFF  # every target model fails this local range

# sources: name -> (data bits, domain); targets: name -> (protocol, data bits, base, domain)
MODES = {
    'switch': {
        'sources': {'h0': (32, 'main')},
        'targets': {'tx': ('axi4', 32, 0x10000, 'main'), 'ta': ('apb4', 32, 0x11000, 'main'),
                    'th': ('ahb_v3', 32, 0x12000, 'main'), 'tl': ('axi4_lite', 32, 0x13000, 'main')},
        'unmapped': 0x18000, 'nodes': None},
    'wide': {
        'sources': {'w0': (64, 'main'), 'w1': (128, 'main')},
        'targets': {'tx': ('axi4', 32, 0x10000, 'main'), 'ty': ('axi4', 64, 0x11000, 'main'),
                    'th': ('ahb_v3', 64, 0x12000, 'main'), 'ta': ('apb4', 32, 0x13000, 'main'),
                    'tz': ('axi4', 128, 0x14000, 'main')},
        'connect': {'w0': ['tx', 'ty', 'th', 'ta'], 'w1': ['tx', 'th', 'ta', 'tz']},
        'unmapped': 0x18000, 'nodes': None, 'network_bits': 32},
    'clocked': {
        'sources': {'h0': (32, 'fast')},
        'targets': {'tx': ('axi4', 32, 0x10000, 'slow'), 'ta': ('apb4', 32, 0x11000, 'main'),
                    'th': ('ahb_v3', 32, 0x12000, 'slow')},
        'unmapped': 0x18000, 'nodes': [('root', 'main'), ('leaf', 'slow')],
        'domains': {'main': (2, 10), 'fast': (2, 6), 'slow': (3, 14)}},
}


def configuration(mode):
    m = MODES[mode]
    domains = m.get('domains', {'main': (2, 10)})
    config = {
        'schema_version': 10, 'name': 'ahb_initiator_' + mode, 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n', 'test_port': 'test'}
                          for d in domains],
        'initiators': [{'name': s, 'clock_domain': d, 'protocol': 'ahb_v3', 'data_width': w}
                       for s, (w, d) in m['sources'].items()],
        'targets': [], 'connectivity': m.get('connect', 'all'),
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    for name, (protocol, bits, base, domain) in m['targets'].items():
        entry = {'name': name, 'clock_domain': domain, 'protocol': protocol, 'data_width': bits,
                 'mappings': [{'base': hex(base), 'size': '0x1000'}]}
        if protocol == 'axi4':
            entry.update(id_width=2, len_width=8)
        if protocol != 'apb3':
            entry['attributes'] = 'amba'
        config['targets'].append(entry)
    if 'network_bits' in m:
        config['transport'] = {'data_width': m['network_bits']}
    if len(domains) > 1:
        config['synchronizer_stages'] = max(v[0] for v in domains.values())
    if m['nodes']:
        nodes = [{'name': n, 'clock_domain': d} for n, d in m['nodes']]
        exits = [{'endpoint': t, 'node': 'root' if t == 'ta' else 'leaf'} for t in m['targets']]
        request = {'nodes': nodes, 'links': [{'from': 'root', 'to': 'leaf'}],
                   'entries': [{'endpoint': s, 'node': 'root'} for s in m['sources']], 'exits': exits}
        response = {'nodes': [dict(n) for n in nodes], 'links': [{'from': 'leaf', 'to': 'root'}],
                    'entries': [dict(x) for x in exits],
                    'exits': [{'endpoint': s, 'node': 'root'} for s in m['sources']]}
        config['topology'] = {'request': request, 'response': response}
    return config


def initial_byte(index, local):
    return (local * 7 + (local >> 8) * 13 + 0x40 + index * 17) & 0xFF


def beat_addresses(t):
    size, n, addr = t['size'], t['beats'], t['addr']
    step = 1 << size
    if t['hburst'] in (2, 4, 6):
        container = step * n
        base = addr & ~(container - 1)
        return [base + ((addr - base + i * step) % container) for i in range(n)]
    aligned = addr & ~(step - 1)
    return [addr if i == 0 else aligned + i * step for i in range(n)]


def directed(mode):
    """Serial transfers of every source to every target and to the unmapped address."""
    m = MODES[mode]
    out = []

    def add(src, op, addr, hburst='SINGLE', size=None, beats=None, prot=NONBUF, busy=(), cancel=0, lock=0):
        width = m['sources'][src][0]
        full = (width // 8).bit_length() - 1
        code = HBURST[hburst]
        # Vary the HPROT bits beside the bufferable one the transfer is chosen for.
        prot = (prot & 4) | (0x3, 0x9, 0x0, 0x2, 0xB, 0x1)[len(out) % 6] if op == 'write' else (
            0x3, 0xB, 0x0, 0x1, 0x2, 0xF, 0x6)[len(out) % 7]
        out.append({'name': f'{src}-{len(out)}', 'src': src, 'op': op, 'addr': addr, 'hburst': code,
                    'size': full if size is None else size,
                    'beats': beats if beats is not None else FIXED_BEATS.get(code, 1),
                    'prot': prot, 'busy': list(busy), 'cancel': cancel, 'lock': lock})
    for src, (width, _) in m['sources'].items():
        B = width // 8
        full = B.bit_length() - 1
        region = 0x100 if src.endswith('0') else 0x600
        places = [(t, base) for t, (_, _, base, _) in m['targets'].items() if reaches(mode, src, t)]
        places.append(('unm', m['unmapped']))
        for tname, base in places:
            o = base + region
            if tname == 'unm':
                add(src, 'read', o)
                add(src, 'write', o)
                add(src, 'write', o + B, prot=BUF)
                add(src, 'read', o, 'INCR4')
                add(src, 'read', o, 'INCR4', cancel=1)
                add(src, 'write', o, 'INCR4')
                add(src, 'write', o, 'INCR', beats=3)
                add(src, 'write', o, 'INCR', beats=3, prot=BUF)
                continue
            add(src, 'read', o)
            add(src, 'write', o)
            add(src, 'read', o)
            add(src, 'write', o + B, prot=BUF)
            add(src, 'read', o + B)
            for s in range(full):
                add(src, 'write', o + 2 * B + (1 << s), size=s, prot=BUF if s % 2 else NONBUF)
                add(src, 'read', o + 2 * B + (1 << s), size=s)
            add(src, 'read', o + 2 * B)
            for hb in ('INCR4', 'WRAP4', 'INCR8', 'WRAP8', 'INCR16', 'WRAP16'):
                a = o + 4 * B + (B if hb.startswith('WRAP') else 0)
                for prot in (NONBUF, BUF):
                    add(src, 'write', a, hb, prot=prot)
                    add(src, 'read', a, hb)
            for n in (1, 2, 3, 5, 9, 17):
                add(src, 'write', o + 40 * B + B, 'INCR', beats=n, prot=BUF)
                add(src, 'read', o + 40 * B + B, 'INCR', beats=n)
                add(src, 'write', o + 60 * B, 'INCR', beats=n)
                add(src, 'read', o + 60 * B + 2 * B, 'INCR', beats=n)
            # Pieces near a 1 KiB boundary.
            add(src, 'write', base + 0x400 - 6 * B, 'INCR', beats=6, prot=BUF)
            add(src, 'read', base + 0x400 - 6 * B, 'INCR', beats=6)
            add(src, 'read', base + 0x400 - 3 * B, 'INCR', beats=3)
            # BUSY inside bursts.
            add(src, 'write', o + 4 * B, 'INCR4', busy=(0, 2, 0, 1))
            add(src, 'read', o + 4 * B, 'INCR', beats=6, busy=(0, 1, 0, 0, 3, 0))
            add(src, 'write', o + 4 * B, 'INCR', beats=6, busy=(0, 0, 2, 0, 0, 1), prot=BUF)
            add(src, 'read', o + 4 * B, 'WRAP8', busy=(0, 0, 0, 2, 0, 0, 0, 1))
            # Errors (local 0xf00..0xfff), the manager continuing or cancelling.
            add(src, 'read', base + 0xF10)
            add(src, 'write', base + 0xF10)
            add(src, 'write', base + 0xF20, prot=BUF)
            for cancel in (0, 1):
                add(src, 'read', base + 0xF00 - 2 * B, 'INCR4', cancel=cancel)
                add(src, 'write', base + 0xF00 - 2 * B, 'INCR4', cancel=cancel)
                add(src, 'read', base + 0xF00 - 4 * B, 'INCR', beats=6, cancel=cancel)
                add(src, 'write', base + 0xF00 - B, 'INCR', beats=3, cancel=cancel)
            add(src, 'write', base + 0xF00 - 2 * B, 'INCR4', prot=BUF)
            # Outside the face's profile: ERROR without access.
            add(src, 'read', o + 0x80, lock=1)
            add(src, 'write', o + 0x84, lock=1)
            add(src, 'write', o + 0x90, 'INCR4', size=0)
            add(src, 'read', o + 0x90, 'INCR', beats=2, size=0, cancel=1)
            add(src, 'read', o + 0x90, size=full + 1)
            add(src, 'read', o + 0x90)
    return out


def after_reset(mode):
    """Transfers after the common reset that cancels a read burst."""
    src = next(iter(MODES[mode]['sources']))
    width = MODES[mode]['sources'][src][0]
    full = (width // 8).bit_length() - 1
    base = MODES[mode]['targets']['tx'][2]
    return [{'name': f'after-{i}', 'src': src, 'op': op, 'addr': base + 0x800 + i * 0x40, 'hburst': hb,
             'size': full, 'beats': n, 'prot': prot, 'busy': [], 'cancel': 0, 'lock': 0}
            for i, (op, hb, n, prot) in enumerate([('write', 3, 4, NONBUF), ('read', 3, 4, NONBUF),
                                                   ('write', 1, 3, BUF), ('read', 1, 3, NONBUF)])]


def reaches(mode, src, target):
    return target in MODES[mode].get('connect', {}).get(src, MODES[mode]['targets'])


# An AXI4 target whose every source has its width receives the source's bursts unchanged.
def exact(mode, target):
    m = MODES[mode]
    protocol, bits, _, _ = m['targets'][target]
    return protocol == 'axi4' and all(w == bits for s, (w, _) in m['sources'].items() if reaches(mode, s, target))


class Oracle:
    """Predicts responses, read data, AXI4 bursts and memories from the docs/protocols.md rules alone."""

    def __init__(self, mode):
        self.mode = mode
        self.m = MODES[mode]
        self.mem = {}
        for index, name in enumerate(self.m['targets']):
            self.mem[name] = bytearray(initial_byte(index, a) for a in range(4096))
        self.bursts = {t: [] for t in self.m['targets']}

    def target_of(self, addr, src):
        for name, (_, _, base, _) in self.m['targets'].items():
            if base <= addr < base + 0x1000 and reaches(self.mode, src, name):
                return name, addr - base
        return None, None

    def run(self, t):
        width = self.m['sources'][t['src']][0]
        B = width // 8
        full = B.bit_length() - 1
        addrs = beat_addresses(t)
        n = t['beats']
        legal = not t['lock'] and t['size'] <= full and (t['size'] == full or t['hburst'] == SINGLE)
        tname, _ = self.target_of(t['addr'], t['src'])
        failing = [tname is None or ERR_LO <= self.target_of(a, t['src'])[1] <= ERR_HI for a in addrs]
        write, posted = t['op'] == 'write', t['op'] == 'write' and t['prot'] & 4
        if not legal:
            resp = [1] * n
            if t['cancel']:
                resp = resp[:1]
            return {'resp': resp, 'data': [None] * len(resp)}
        # AXI pieces: (first beat, beats)
        if t['hburst'] == SINGLE:
            pieces = [(0, 1)]
        elif t['hburst'] != INCR:
            pieces = [(0, n)]
        else:
            pieces = [(i, 1) for i in range(n)]
        piece_of = {}
        for k, (first, count) in enumerate(pieces):
            for i in range(first, min(first + count, n)):
                piece_of[i] = k
        resp = []
        for i in range(n):
            if not write:
                resp.append(1 if failing[i] else 0)
            elif posted:
                resp.append(0)
            else:
                first, count = pieces[piece_of[i]]
                last = i == min(first + count, n) - 1
                resp.append(1 if last and any(failing[first:min(first + count, n)]) else 0)
        issued = n
        if t['cancel'] and 1 in resp:
            issued = resp.index(1) + 1
            resp = resp[:issued]
        used = sorted({piece_of[i] for i in range(issued)})
        data = []
        for i in range(issued):
            name, _ = self.target_of(addrs[i], t['src'])
            lo = addrs[i] & ~((1 << t['size']) - 1)
            if write:
                if name and not failing[i]:
                    for b in range(1 << t['size']):
                        self.mem[name][(lo + b - self.m['targets'][name][2]) % 4096] = self.wbyte(t, i, (lo + b) % B)
                data.append(None)
            elif resp[i]:
                data.append(None)
            else:
                word = 0
                for b in range(1 << t['size']):
                    word |= self.mem[name][(lo + b - self.m['targets'][name][2]) % 4096] << (8 * ((lo + b) % B))
                data.append(word)
        if tname and exact(self.mode, tname):
            for k in used:
                first, count = pieces[k]
                strobes = []
                for i in range(first, first + count):
                    if write and i < issued and i < n:
                        lo = addrs[i] & ~((1 << t['size']) - 1)
                        strobes.append(((1 << (1 << t['size'])) - 1) << (lo % B))
                    elif write:
                        strobes.append(0)
                burst = 2 if t['hburst'] in (2, 4, 6) else 1
                # amba attributes: AxCACHE[1:0] = HPROT[3:2], AxPROT = {~HPROT[0], 1, HPROT[1]}.
                hprot = t['prot']
                self.bursts[tname].append({'op': t['op'], 'addr': addrs[first] % 0x1000, 'len': count - 1,
                                           'size': t['size'], 'burst': burst, 'cache': hprot >> 2,
                                           'prot': (~hprot & 1) << 2 | 2 | (hprot >> 1 & 1),
                                           'strobes': strobes})
        return {'resp': resp, 'data': data}

    @staticmethod
    def wbyte(t, beat, lane):
        return (t['seed'] + beat * 13 + lane * 7 + 0x5A) & 0xFF


def wdata(t, beat, B):
    return sum(Oracle.wbyte(t, beat, lane) << (8 * lane) for lane in range(B))


def driver(src, width, clock):
    p = f'nl_i_{src}_'
    return f'''
function automatic [63:0] {src}_baddr(input integer k, input integer i);
 reg [63:0] step, container, base, aligned;
 begin
  step = 64'd1 << g_size[k]; aligned = g_addr[k] & ~(step - 1);
  if (g_hburst[k] == 2 || g_hburst[k] == 4 || g_hburst[k] == 6) begin
   container = step * g_beats[k]; base = g_addr[k] & ~(container - 1);
   {src}_baddr = base + ((aligned - base + i * step) % container);
  end else {src}_baddr = i == 0 ? g_addr[k] : aligned + i * step;
 end
endfunction
// The face checker: HREADYOUT high outside a data phase, HRESP high only in a data phase, ERROR
// for two cycles.
reg {src}_pending = 0, {src}_err1 = 0;
always @(posedge {clock}) if (reset_n) begin
 if (!{src}_pending) check({p}hreadyout === 1'b1, "IDLE_ZERO_WAIT");
 if ({p}hresp === 1'b1) check({src}_pending, "HRESP_IN_DATA_PHASE");
 if ({src}_err1) check({p}hresp === 1'b1 && {p}hreadyout === 1'b1, "ERROR_SECOND_CYCLE");
 if ({p}hresp === 1'b1 && {p}hreadyout === 1'b1) check({src}_err1, "ERROR_FIRST_CYCLE");
 check({p}hreadyout !== 1'bx && {p}hresp !== 1'bx, "FACE_KNOWN");
 {src}_err1 <= {p}hresp === 1'b1 && {p}hreadyout === 1'b0;
 if ({p}hreadyout) {src}_pending <= {p}hsel && {p}htrans[1];
end else begin {src}_pending <= 0; {src}_err1 <= 0; end
task automatic {src}_xfer(input integer k);
 integer nb, nx, cb, db, bz, fin, canc, guard; reg dv;
 begin
  nb = g_beats[k]; dv = 0; canc = 0; bz = 0; fin = 0; guard = 0;
  @(negedge {clock});
  {p}hsel = 1; {p}haddr = {src}_baddr(k, 0); {p}htrans = 2; {p}hburst = g_hburst[k]; {p}hsize = g_size[k];
  {p}hwrite = g_write[k]; {p}hprot = g_prot[k]; {p}hmastlock = g_lock[k];
  cb = 0; nx = 1;
  while (!fin && !abort) begin
   @(posedge {clock});
   guard = guard + 1;
   if (guard > 4000) begin $display("ASSERT_FAIL:AHB_INITIATOR.HANG %0d", k); $fatal(1); end
   if ({p}hreadyout) begin
    if (dv) $display("EV H %0d %0d %0d %h", k, db, {p}hresp, {p}hrdata);
    if (cb >= 0) begin dv = 1; db = cb; end else dv = 0;
    @(negedge {clock});
    if (dv && g_write[k]) {p}hwdata = beat_data(k, db, {width // 8});
    if (canc || nx >= nb) begin {p}htrans = 0; cb = -1; end
    else if (bz < g_busy[g_busy_at[k] + nx]) begin {p}htrans = 1; {p}haddr = {src}_baddr(k, nx); cb = -1; bz = bz + 1; end
    else begin {p}htrans = 3; {p}haddr = {src}_baddr(k, nx); cb = nx; nx = nx + 1; bz = 0; end
    if (!dv && cb < 0 && (canc || nx >= nb)) fin = 1;
   end else if (dv && {p}hresp && g_cancel[k] && !canc) begin
    @(negedge {clock}); {p}htrans = 0; cb = -1; canc = 1;
   end
  end
 end
endtask
'''


def target_model(name, protocol, bits, index, ports):
    t = f'nl_t_{name}_'

    def has(sig):
        return t + sig in ports

    def w(sig):
        return ports[t + sig]['width']
    nb = bits // 8
    common = f'''
reg [7:0] mem_{name} [0:4095];
initial begin : init_{name}
 integer a;
 for (a = 0; a < 4096; a = a + 1) mem_{name}[a] = a * 7 + (a >> 8) * 13 + 8'h40 + {index * 17};
end
function automatic err_{name}(input [63:0] a); err_{name} = (a % 4096) >= {ERR_LO} && (a % 4096) <= {ERR_HI}; endfunction
'''
    if protocol == 'axi4':
        idw = w('awid')
        return common + f'''
// AXI4 target {name}: one burst per direction, a wait cycle before each R and W beat.
reg {name}_ract = 0, {name}_wact = 0, {name}_bpend = 0, {name}_werr = 0, {name}_rgap = 0, {name}_wgap = 0;
reg [63:0] {name}_ra, {name}_wa; reg [7:0] {name}_rn, {name}_rk, {name}_wn, {name}_wk; reg [2:0] {name}_rs, {name}_ws;
reg [1:0] {name}_rb, {name}_wb; reg [{idw - 1}:0] {name}_rid, {name}_wid;
function automatic [63:0] {name}_beat(input [63:0] a, input [7:0] k, input [7:0] n, input [2:0] s, input [1:0] b);
 reg [63:0] step, c, base;
 begin step = 64'd1 << s; c = step * (n + 1); base = a & ~(c - 1);
  {name}_beat = b == 2 ? base + ((a - base + k * step) % c) : (k == 0 ? a : (a & ~(step - 1)) + k * step); end
endfunction
wire [63:0] {name}_rbeat = {name}_beat({name}_ra, {name}_rk, {name}_rn, {name}_rs, {name}_rb);
wire [63:0] {name}_wbeat = {name}_beat({name}_wa, {name}_wk, {name}_wn, {name}_ws, {name}_wb);
reg [{bits - 1}:0] {name}_rword;
always @* begin : rword_{name}
 integer i; {name}_rword = 0;
 for (i = 0; i < {nb}; i = i + 1) {name}_rword[8 * i +: 8] = mem_{name}[({name}_rbeat % 4096) / {nb} * {nb} + i];
end
assign {t}arready = !{name}_ract;
assign {t}rvalid = {name}_ract && {name}_rgap;
assign {t}rdata = {t}rvalid && !err_{name}({name}_rbeat) ? {name}_rword : 0;
assign {t}rresp = {t}rvalid && err_{name}({name}_rbeat) ? 2'b10 : 2'b00;
assign {t}rid = {name}_rid;
assign {t}rlast = {t}rvalid && {name}_rk == {name}_rn;
assign {t}awready = !{name}_wact && !{name}_bpend;
assign {t}wready = {name}_wact && {name}_wgap;
assign {t}bvalid = {name}_bpend;
assign {t}bresp = {name}_werr ? 2'b10 : 2'b00;
assign {t}bid = {name}_wid;
always @(posedge {ports[t + 'awvalid']['clock']}) if (!reset_n) begin
 {name}_ract <= 0; {name}_wact <= 0; {name}_bpend <= 0; {name}_rgap <= 0; {name}_wgap <= 0;
end else begin : step_{name}
 integer j;
 {name}_rgap <= {name}_ract && !{name}_rgap;
 {name}_wgap <= {name}_wact && !{name}_wgap;
 if ({t}arvalid && {t}arready) begin
  $display("EV A {name} read %0h %0d %0d %0d %0d %0d", {t}araddr % 4096, {t}arlen, {t}arsize, {t}arburst, {t}arcache, {t}arprot);
  {name}_ra <= {t}araddr; {name}_rn <= {t}arlen; {name}_rs <= {t}arsize; {name}_rb <= {t}arburst;
  {name}_rid <= {t}arid; {name}_rk <= 0; {name}_ract <= 1; {name}_rgap <= 0;
 end
 if ({t}rvalid && {t}rready) begin
  {name}_rgap <= 0;
  if ({name}_rk == {name}_rn) {name}_ract <= 0; else {name}_rk <= {name}_rk + 1;
 end
 if ({t}awvalid && {t}awready) begin
  $display("EV A {name} write %0h %0d %0d %0d %0d %0d", {t}awaddr % 4096, {t}awlen, {t}awsize, {t}awburst, {t}awcache, {t}awprot);
  {name}_wa <= {t}awaddr; {name}_wn <= {t}awlen; {name}_ws <= {t}awsize; {name}_wb <= {t}awburst;
  {name}_wid <= {t}awid; {name}_wk <= 0; {name}_wact <= 1; {name}_werr <= 0; {name}_wgap <= 0;
 end
 if ({t}wvalid && {t}wready) begin
  $display("EV S {name} %0h", {t}wstrb);
  {name}_wgap <= 0;
  if (err_{name}({name}_wbeat)) {name}_werr <= 1;
  else for (j = 0; j < {nb}; j = j + 1)
   if ({t}wstrb[j]) mem_{name}[({name}_wbeat % 4096) / {nb} * {nb} + j] = {t}wdata[8 * j +: 8];
  if ({name}_wk == {name}_wn) begin {name}_wact <= 0; {name}_bpend <= 1; end else {name}_wk <= {name}_wk + 1;
 end
 if ({t}bvalid && {t}bready) {name}_bpend <= 0;
end
'''
    if protocol == 'axi4_lite':
        return common + f'''
// AXI4-Lite target {name}: AW and W together, one wait cycle before B and R.
reg {name}_rp = 0, {name}_bp = 0, {name}_rerr = 0, {name}_werr = 0, {name}_rgap = 0, {name}_bgap = 0;
reg [31:0] {name}_ra;
assign {t}arready = !{name}_rp;
assign {t}rvalid = {name}_rp && {name}_rgap;
assign {t}rdata = {t}rvalid && !{name}_rerr ? {{mem_{name}[{name}_ra % 4096 / 4 * 4 + 3], mem_{name}[{name}_ra % 4096 / 4 * 4 + 2],
                                            mem_{name}[{name}_ra % 4096 / 4 * 4 + 1], mem_{name}[{name}_ra % 4096 / 4 * 4]}} : 0;
assign {t}rresp = {name}_rerr ? 2'b10 : 2'b00;
assign {t}awready = !{name}_bp && {t}wvalid;
assign {t}wready = !{name}_bp && {t}awvalid;
assign {t}bvalid = {name}_bp && {name}_bgap;
assign {t}bresp = {name}_werr ? 2'b10 : 2'b00;
always @(posedge {ports[t + 'awvalid']['clock']}) if (!reset_n) begin
 {name}_rp <= 0; {name}_bp <= 0; {name}_rgap <= 0; {name}_bgap <= 0;
end else begin : step_{name}
 integer j;
 {name}_rgap <= {name}_rp && !{name}_rgap;
 {name}_bgap <= {name}_bp && !{name}_bgap;
 if ({t}arvalid && {t}arready) begin
  {name}_ra <= {t}araddr; {name}_rp <= 1; {name}_rerr <= err_{name}({t}araddr); {name}_rgap <= 0;
 end
 if ({t}rvalid && {t}rready) {name}_rp <= 0;
 if ({t}awvalid && {t}awready) begin
  {name}_bp <= 1; {name}_bgap <= 0; {name}_werr <= err_{name}({t}awaddr);
  if (!err_{name}({t}awaddr))
   for (j = 0; j < 4; j = j + 1) if ({t}wstrb[j]) mem_{name}[{t}awaddr % 4096 / 4 * 4 + j] = {t}wdata[8 * j +: 8];
 end
 if ({t}bvalid && {t}bready) {name}_bp <= 0;
end
'''
    if protocol == 'apb4':
        return common + f'''
// APB4 target {name}: one wait state per access.
reg {name}_wait = 0;
assign {t}pready = {t}psel && {t}penable && {name}_wait;
wire [31:0] {name}_pa = {t}paddr % 4096 / 4 * 4;
assign {t}prdata = {t}pready && !{t}pwrite ? {{mem_{name}[{name}_pa + 3], mem_{name}[{name}_pa + 2], mem_{name}[{name}_pa + 1], mem_{name}[{name}_pa]}} : 0;
assign {t}pslverr = {t}pready && err_{name}({t}paddr);
always @(posedge {ports[t + 'psel']['clock']}) if (!reset_n) {name}_wait <= 0;
else begin : step_{name}
 integer j;
 {name}_wait <= {t}psel && {t}penable && !{name}_wait;
 if ({t}pready && {t}pwrite && !err_{name}({t}paddr))
  for (j = 0; j < 4; j = j + 1) if ({t}pstrb[j]) mem_{name}[{name}_pa + j] = {t}pwdata[8 * j +: 8];
end
'''
    # AHB target: pipelined, one wait state per transfer, ERROR in two cycles.
    return common + f'''
reg {name}_dp = 0, {name}_dw = 0, {name}_cnt = 0, {name}_ph2 = 0; reg [31:0] {name}_da; reg [2:0] {name}_ds;
wire {name}_bad = err_{name}({name}_da);
wire [31:0] {name}_wbase = {name}_da % 4096 / {nb} * {nb};
reg [{bits - 1}:0] {name}_rword;
always @* begin : rword_{name}
 integer i; {name}_rword = 0;
 for (i = 0; i < {nb}; i = i + 1) {name}_rword[8 * i +: 8] = mem_{name}[{name}_wbase + i];
end
assign {t}hreadyout = !{name}_dp || ({name}_cnt && (!{name}_bad || {name}_ph2));
assign {t}hresp = {name}_dp && {name}_cnt && {name}_bad;
assign {t}hrdata = {name}_dp && !{name}_dw && {t}hready && !{name}_bad ? {name}_rword : 0;
always @(posedge {ports[t + 'htrans']['clock']}) if (!reset_n) begin {name}_dp <= 0; {name}_cnt <= 0; {name}_ph2 <= 0; end
else begin : step_{name}
 integer j;
 if ({t}hready) begin
  if ({name}_dp && {name}_dw && !{name}_bad)
   for (j = 0; j < {nb}; j = j + 1)
    if (j >= {name}_da % {nb} / (1 << {name}_ds) * (1 << {name}_ds) && j < {name}_da % {nb} / (1 << {name}_ds) * (1 << {name}_ds) + (1 << {name}_ds))
     mem_{name}[{name}_wbase + j] = {t}hwdata[8 * j +: 8];
  {name}_dp <= {t}htrans[1]; {name}_dw <= {t}hwrite; {name}_da <= {t}haddr; {name}_ds <= {t}hsize;
  {name}_cnt <= 0; {name}_ph2 <= 0;
 end else if (!{name}_cnt) {name}_cnt <= 1;
 else if ({name}_bad) {name}_ph2 <= 1;
end
'''


def bench(mode, mapping, selected, resumed):
    m = MODES[mode]
    ports = {p['name']: p for p in mapping['ports']}
    driven = set()
    models = []
    for index, (name, (protocol, bits, _, _)) in enumerate(m['targets'].items()):
        models.append(target_model(name, protocol, bits, index, ports))
        driven |= {p for p, v in ports.items() if v['endpoint'] == f'target:{name}' and v['direction'] == 'input'}
    decl, conns, init, late = [], [], [], []
    tied = {}
    for src in m['sources']:
        tied[f'nl_i_{src}_hready'] = f'nl_i_{src}_hreadyout'
    for name, p in ports.items():
        width = p['width']
        wire = 'reset_n' if p.get('signal') == 'reset_n' else name
        if name in tied:
            late.append(f'wire [{width - 1}:0] {name} = {tied[name]};')
        elif p['direction'] == 'input' and name not in driven:
            decl.append(f'reg [{width - 1}:0] {wire};')
            if wire != 'reset_n' and p.get('signal') != 'clock':
                init.append(f' {wire} = 0;')
        else:
            decl.append(f'wire [{width - 1}:0] {wire};')
        conns.append(f'.{name}({wire})')
    decl += late
    periods = {d: v[1] for d, v in m.get('domains', {'main': (2, 10)}).items()}
    clocks = sorted({p['name'] for p in mapping['ports'] if p.get('signal') == 'clock'})
    clock_gen = ''.join(f'initial {c} = 0;\nalways #{periods[c[len("clk_"):]] / 2} {c} = ~{c};\n' for c in clocks)
    every = selected + resumed
    lines = []
    busy = []
    for k, t in enumerate(every):
        lines.append(f" g_addr[{k}] = 64'h{t['addr']:x}; g_size[{k}] = {t['size']}; g_hburst[{k}] = {t['hburst']}; "
                     f"g_beats[{k}] = {t['beats']}; g_write[{k}] = {int(t['op'] == 'write')}; g_prot[{k}] = {t['prot']}; "
                     f"g_lock[{k}] = {t['lock']}; g_cancel[{k}] = {t['cancel']}; g_seed[{k}] = {t['seed']}; g_busy_at[{k}] = {len(busy)};")
        busy += list(t['busy']) + [0] * (t['beats'] - len(t['busy']))
    lines += [f' g_busy[{j}] = {v};' for j, v in enumerate(busy)]
    drivers = ''.join(driver(src, w, 'clk_' + d) for src, (w, d) in m['sources'].items())
    widest = max(w for w, _ in m['sources'].values())
    calls = ''.join(f'  if (g_src[k] == {i}) {src}_xfer(k);\n' for i, src in enumerate(m['sources']))
    src_index = ''.join(f' g_src[{k}] = {list(m["sources"]).index(t["src"])};' for k, t in enumerate(every))
    first = next(iter(m['sources']))
    main = f'clk_{m["sources"][first][1]}'
    ta_base = m['targets']['ta'][2]
    full = (m['sources'][first][0] // 8).bit_length() - 1
    quiet = ' '.join(f'nl_i_{s}_htrans = 0; nl_i_{s}_hsel = 0;' for s in m['sources'])
    dumps = ''.join(f' for (a = 0; a < 4096; a = a + 1) $display("EV M {name} %0h %h", a, mem_{name}[a]);\n'
                    for name in m['targets'])
    reset_checks = ''.join(
        f'always @(negedge clk_{domain}) if (!reset_n && cycle > 3) begin\n'
        f' check(nl_t_{name}_hready === 1\'b1, "RESET_READY");\n'
        f' check(nl_t_{name}_htrans === 2\'b00, "RESET_IDLE");\nend\n'
        for name, (protocol, _, _, domain) in m['targets'].items() if protocol == 'ahb_v3')
    N = len(every)
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
integer cycle = 0;
reg abort = 0;
{clock_gen}
always @(posedge {main}) begin
 cycle <= cycle + 1;
 if (cycle > 600000) begin $display("ASSERT_FAIL:AHB_INITIATOR.TIMEOUT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*32-1:0] label_;
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:AHB_INITIATOR.%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
reg [63:0] g_addr [0:{N - 1}]; reg [2:0] g_size [0:{N - 1}]; reg [2:0] g_hburst [0:{N - 1}]; integer g_beats [0:{N - 1}];
reg g_write [0:{N - 1}]; reg [3:0] g_prot [0:{N - 1}]; reg g_lock [0:{N - 1}]; reg g_cancel [0:{N - 1}];
integer g_seed [0:{N - 1}]; integer g_busy_at [0:{N - 1}]; integer g_src [0:{N - 1}]; integer g_busy [0:{max(len(busy), 1) - 1}];
initial begin
{chr(10).join(lines)}
{src_index}
end
function automatic [{widest - 1}:0] beat_data(input integer k, input integer beat, input integer bytes);
 integer lane;
 begin
  beat_data = 0;
  for (lane = 0; lane < bytes; lane = lane + 1) beat_data[8 * lane +: 8] = g_seed[k] + beat * 13 + lane * 7 + 8'h5a;
 end
endfunction
{''.join(models)}
{drivers}
{reset_checks}
initial begin : run
 integer k, a;
 reset_n = 0;
{chr(10).join(init)}
 repeat (10) @(posedge {main});
 @(negedge {main}); reset_n = 1;
 repeat (5) @(posedge {main});
 for (k = 0; k < {len(selected)}; k = k + 1) begin
  $display("EV T %0d", k);
{calls}  repeat (2) @(posedge {main});
 end
 // A common reset in the middle of an INCR16 read burst at ta cancels it.
 $display("PHASE reset");
 fork
  begin : burst
   integer n;
   @(negedge {main});
   nl_i_{first}_hsel = 1; nl_i_{first}_haddr = 32'h{ta_base + 0x900:x}; nl_i_{first}_htrans = 2; nl_i_{first}_hburst = 7;
   nl_i_{first}_hsize = {full}; nl_i_{first}_hwrite = 0; nl_i_{first}_hprot = 3; nl_i_{first}_hmastlock = 0;
   for (n = 1; n < 16 && !abort; n = n + 1) begin
    @(posedge {main}); while (!nl_i_{first}_hreadyout && !abort) @(posedge {main});
    @(negedge {main}); nl_i_{first}_htrans = 3; nl_i_{first}_haddr = 32'h{ta_base + 0x900:x} + n * {1 << full};
   end
  end
  begin repeat (14) @(posedge {main}); @(negedge {main}); abort = 1; reset_n = 0; end
 join
 @(negedge {main}); {quiet}
 repeat (8) @(posedge {main});
 @(negedge {main}); reset_n = 1; abort = 0;
 repeat (5) @(posedge {main});
 for (k = {len(selected)}; k < {N}; k = k + 1) begin
  $display("EV T %0d", k);
{calls}  repeat (2) @(posedge {main});
 end
 repeat (20) @(posedge {main});
{dumps} $display("NOCLOOM_PASS ahb_initiator {mode}");
 $finish;
end
endmodule
'''


def parse(log):
    out = {'beats': {}, 'bursts': {}, 'strobes': {}, 'mem': {}}
    for line in log.splitlines():
        f = line.split()
        if len(f) < 2 or f[0] != 'EV':
            continue
        if f[1] == 'H':
            out['beats'].setdefault(int(f[2]), []).append((int(f[3]), int(f[4]), int(f[5], 16) if 'x' not in f[5].lower() else None))
        elif f[1] == 'A':
            out['bursts'].setdefault(f[2], []).append({'op': f[3], 'addr': int(f[4], 16), 'len': int(f[5]),
                                                       'size': int(f[6]), 'burst': int(f[7]), 'cache': int(f[8]),
                                                       'prot': int(f[9]), 'strobes': []})
        elif f[1] == 'S':
            write = [b for b in out['bursts'][f[2]] if b['op'] == 'write']
            write[-1]['strobes'].append(int(f[3], 16))
        elif f[1] == 'M':
            out['mem'].setdefault(f[2], {})[int(f[3], 16)] = int(f[4], 16)
    return out


def compare(mode, log, selected, resumed):
    oracle = Oracle(mode)
    got = parse(log)
    every = selected + resumed
    problems = []
    for k, t in enumerate(every):
        want = oracle.run(t)
        beats = got['beats'].get(k, [])
        resp = [r for _, r, _ in beats]
        if [b for b, _, _ in beats] != list(range(len(beats))) or resp != want['resp']:
            problems.append({'tx': t['name'], 'want_resp': want['resp'], 'got_resp': resp, 'beats': [b for b, _, _ in beats]})
            continue
        width = MODES[mode]['sources'][t['src']][0]
        for i, ((_, r, data), expect) in enumerate(zip(beats, want['data'])):
            if expect is None:
                continue
            lo = beat_addresses(t)[i] & ~((1 << t['size']) - 1)
            mask = sum(0xFF << (8 * ((lo + b) % (width // 8))) for b in range(1 << t['size']))
            if data is None or data & mask != expect:
                problems.append({'tx': t['name'], 'beat': i, 'want': hex(expect), 'got': hex(data) if data is not None else 'x'})
    for name, want in oracle.bursts.items():
        if not exact(mode, name):
            continue
        if want != got['bursts'].get(name, [])[:len(want)] or len(got['bursts'].get(name, [])) != len(want):
            problems.append({'target': name, 'want_bursts': len(want), 'got_bursts': len(got['bursts'].get(name, [])),
                             'first_difference': next(({'want': a, 'got': b} for a, b in zip(want, got['bursts'].get(name, [])) if a != b), None)})
    for name, mem in oracle.mem.items():
        dumped = got['mem'].get(name, {})
        bad = [a for a in range(4096) if dumped.get(a) != mem[a]]
        if bad:
            problems.append({'target': name, 'memory_bytes_differ': len(bad), 'first': hex(bad[0]),
                             'want': mem[bad[0]], 'got': dumped.get(bad[0])})
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', choices=sorted(MODES), required=True)
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    yaml = work / 'input.yaml'
    yaml.write_text(json.dumps(configuration(args.mode), indent=1) + '\n')
    outputs = []
    for label in ('first', 'repeat'):
        dest = work / label
        proc = subprocess.run([args.cli, 'generate', yaml, '--out-dir', dest],
                              capture_output=True, text=True, timeout=300, check=False)
        assert proc.returncode == 0, proc.stderr
        outputs.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()})
    assert outputs[0] == outputs[1], outputs
    stem = 'nocloom_ahb_initiator_' + args.mode
    mapping = json.loads((work / f'first/{stem}.map.json').read_text())
    definition = mapping['definition']
    m = MODES[args.mode]
    ports = {p['name']: p for p in mapping['ports']}
    for src, (width, domain) in m['sources'].items():
        assert definition['endpoint_protocols']['initiators'][src] == 'ahb_v3', definition['endpoint_protocols']
        names = {n[len(f'nl_i_{src}_'):]: p for n, p in ports.items() if n.startswith(f'nl_i_{src}_')}
        want = {'hsel': 1, 'haddr': 32, 'htrans': 2, 'hwrite': 1, 'hsize': 3, 'hburst': 3, 'hprot': 4,
                'hmastlock': 1, 'hwdata': width, 'hready': 1, 'hreadyout': 1, 'hresp': 1, 'hrdata': width}
        assert {k: v['width'] for k, v in names.items()} == want, names
        assert all(v['clock'] == 'clk_' + domain for v in names.values()), names
    contract = definition['contract']['ahb_ingress']
    assert contract['unsupported'].startswith('HMASTLOCK'), contract
    selected = directed(args.mode)
    resumed = after_reset(args.mode)
    for k, t in enumerate(selected + resumed):
        t['seed'] = (k * 29) & 0xFF
    case = work / 'simulation'
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(work / f'first/{stem}.v'))
    (case / 'tb.sv').write_text(bench(args.mode, mapping, selected, resumed))
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    problems = compare(args.mode, log, selected, resumed)
    (case / 'mismatches.json').write_text(json.dumps(problems, indent=1) + '\n')
    passed = (rc == 0 and f'NOCLOOM_PASS ahb_initiator {args.mode}' in log and 'ASSERT_FAIL' not in log
              and not problems)
    (work / 'results.json').write_text(json.dumps(
        {'mode': args.mode, 'transfers': len(selected) + len(resumed), 'simulation_exit': rc,
         'mismatches': len(problems), 'status': 'PASS' if passed else 'FAIL',
         'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
         'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}, indent=1) + '\n')
    assert passed, (rc, problems[:3], log[-2500:])
    # A committed undefined-length transfer must not become a four-beat request.
    oracle = Oracle(args.mode)
    probe = next(t for t in selected if t['hburst'] == INCR and t['op'] == 'read'
                 and t['beats'] > 1 and oracle.target_of(t['addr'], t['src'])[0] is not None
                 and exact(args.mode, oracle.target_of(t['addr'], t['src'])[0]))
    target, address = oracle.target_of(probe['addr'], probe['src'])
    lines = log.splitlines()
    changed = False
    for i, line in enumerate(lines):
        f = line.split()
        if (len(f) == 10 and f[:4] == ['EV', 'A', target, 'read']
                and int(f[4], 16) == address and f[5] == '0'):
            f[5] = '3'
            lines[i] = ' '.join(f)
            changed = True
            break
    assert changed and compare(args.mode, '\n'.join(lines), selected, resumed)
    print(f'NOCLOOM_PASS ahb initiator mode={args.mode} transfers={len(selected) + len(resumed)}')
    if args.mode == 'clocked':
        from protocol_combinations import run_ahb_combinations
        run_ahb_combinations(args.cli, work / 'combinations')


if __name__ == '__main__':
    main()
