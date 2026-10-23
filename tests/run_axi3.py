"""Schema 10 AXI3 initiators and targets through the public CLI.

An AXI3 initiator is an AXI4 source behind AMBA 3 AXI ports (docs/protocols.md#axi3): a
two-bit AxLOCK, a four-bit AxLEN and WID; an AXI3 target is an AXI4 target behind them (docs/protocols.md#axi3). The bench drives one AXI3 (or AXI4) manager per
initiator, one burst at a time: INCR and WRAP bursts, narrow and unaligned beats,
exclusive pairs, the target error range, unmapped addresses, AxLOCK 0b10 (locked) and 0b11
(reserved), and W beats whose WID differs from their AWID. Target models of every protocol
keep byte memories with an error range. An independent oracle predicts from the docs/protocols.md rules:

- every beat's response (OKAY, EXOKAY, SLVERR or DECERR) and the read data of OKAY and EXOKAY beats;
- the bursts an AXI4 target of the source's width receives (address, length, size, type);
- every target's final memory.

Locked and reserved bursts answer SLVERR and reach no target; a WID mismatch writes its beats
and answers SLVERR. AWID changes once AW is taken, so WID must match the AWID the burst was taken
with. The bench checks RID, BID and RLAST on every response.

An AXI3 target model takes up to two AW ahead of their W beats and fails the bench on a W beat
whose WID is not the AWID of the oldest burst still taking W beats, or on AxLOCK above 0b01; it
counts the bursts with AxLOCK 0b01, which only an `exclusive: target` target receives.

Modes: switch (a 32-bit AXI3 and a 32-bit AXI4 initiator, the generated switch, AXI4 with a
network exclusive monitor, APB4, AHB and AXI4-Lite targets), wide (64-bit AXI3 with two
bursts in flight and 128-bit AXI3 initiators in a 32-bit network with per-target widths) and
target (AXI4, AXI3 and 64-bit AXI4 initiators running at once against AXI3 targets of 32 bits
with two bursts in flight, of 64 bits, and with `exclusive: target`, and an APB4 target)."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from run_ahb_initiator import ERR_HI, ERR_LO, initial_byte, target_model
from sim_engine import generated_rtl

OKAY, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3
BURST = {'FIXED': 0, 'INCR': 1, 'WRAP': 2}

# sources: name -> (protocol, data bits, id bits, max_outstanding);
# targets: name -> (protocol, data bits, base, exclusive); concurrent sources run at once.
MODES = {
    'switch': {
        'sources': {'s0': ('axi3', 32, 3, 1), 's1': ('axi4', 32, 2, 1)},
        'targets': {'tx': ('axi4', 32, 0x10000, 'network'), 'ta': ('apb4', 32, 0x11000, None),
                    'th': ('ahb_v3', 32, 0x12000, None), 'tl': ('axi4_lite', 32, 0x13000, None)},
        'unmapped': 0x18000},
    'wide': {
        'sources': {'w0': ('axi3', 64, 2, 2), 'w1': ('axi3', 128, 2, 1)},
        'targets': {'tx': ('axi4', 32, 0x10000, None), 'ty': ('axi4', 64, 0x11000, None),
                    'th': ('ahb_v3', 64, 0x12000, None), 'ta': ('apb4', 32, 0x13000, None)},
        'unmapped': 0x18000, 'network_bits': 32},
    'target': {
        'sources': {'s0': ('axi4', 32, 2, 2), 's1': ('axi3', 32, 2, 1), 's2': ('axi4', 64, 2, 1)},
        'targets': {'t3': ('axi3', 32, 0x10000, None), 'tw': ('axi3', 64, 0x11000, None),
                    'tz': ('axi3', 32, 0x12000, 'target'), 'ta': ('apb4', 32, 0x13000, None)},
        'outstanding': {'t3': 2}, 'unmapped': 0x18000, 'network_bits': 32, 'concurrent': True},
}


def configuration(mode):
    m = MODES[mode]
    config = {
        'schema_version': 10, 'name': 'axi3_' + mode, 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [], 'targets': [], 'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    # The owner of an exclusive access is its source: two constant flags.
    exclusive_mode = any(spec[3] for spec in m['targets'].values())
    if exclusive_mode:
        config['transport'] = {'user_flags': ['owner0', 'owner1']}
    for index, (name, (protocol, bits, ids, outstanding)) in enumerate(m['sources'].items()):
        entry = {'name': name, 'clock_domain': 'main', 'protocol': protocol, 'data_width': bits,
                 'id_width': ids, 'len_width': 4 if protocol == 'axi3' else 8}
        if exclusive_mode:
            entry['user_flags'] = {'owner0': index & 1, 'owner1': index >> 1 & 1}
        if outstanding > 1:
            entry['max_outstanding'] = outstanding
        config['initiators'].append(entry)
    for name, (protocol, bits, base, exclusive) in m['targets'].items():
        entry = {'name': name, 'clock_domain': 'main', 'protocol': protocol, 'data_width': bits,
                 'attributes': 'amba', 'mappings': [{'base': hex(base), 'size': '0x1000'}]}
        if protocol == 'axi4':
            entry.update(id_width=4, len_width=8)
        if protocol == 'axi3':
            entry['len_width'] = 4
        if name in m.get('outstanding', {}):
            entry['max_outstanding'] = m['outstanding'][name]
        if exclusive:
            entry['exclusive'] = exclusive
            entry['exclusive_id_flags'] = ['owner0', 'owner1']
        config['targets'].append(entry)
    if 'network_bits' in m:
        config.setdefault('transport', {})['data_width'] = m['network_bits']
    return config


def beat_addresses(addr, size, burst, beats):
    step = 1 << size
    if burst == 'FIXED':
        return [addr] * beats
    if burst == 'WRAP':
        container = step * beats
        base = addr & ~(container - 1)
        return [base + ((addr - base + i * step) % container) for i in range(beats)]
    aligned = addr & ~(step - 1)
    return [addr if i == 0 else aligned + i * step for i in range(beats)]


def lanes(address, size, B):
    """The byte lanes of a beat at `address` on a B-byte bus."""
    lo = address % B
    hi = (address & ~((1 << size) - 1)) % B + (1 << size)
    return range(lo, hi)


def directed(mode):
    """Serial bursts of every source to every target, the unmapped address and the odd inputs."""
    m = MODES[mode]
    out = []

    def add(src, op, addr, beats=1, size=None, burst='INCR', axid=1, lock=0, wid=None, cache=3):
        full = (m['sources'][src][1] // 8).bit_length() - 1
        out.append({'name': f'{len(out)}-{src}-{op}-{addr:x}', 'src': src, 'op': op, 'addr': addr,
                    'beats': beats, 'size': full if size is None else size, 'burst': burst,
                    'axid': axid, 'lock': lock, 'wid': axid if wid is None else wid, 'cache': cache})

    for index, (src, (protocol, bits, ids, _)) in enumerate(m['sources'].items()):
        B = bits // 8
        full = B.bit_length() - 1
        # Each source keeps to its own bytes of every target, so concurrent sources never meet.
        region = 0x80 + 0x280 * index
        top = (1 << ids) - 1
        for tname, (tprotocol, _, base, exclusive) in m['targets'].items():
            o = base + region
            add(src, 'write', o, axid=top)
            add(src, 'read', o, axid=top)
            for s in range(full):
                add(src, 'write', o + 2 * B + (1 << s), size=s, axid=s & top)
                add(src, 'read', o + 2 * B + (1 << s), size=s)
            add(src, 'read', o + 2 * B)
            for n in (4, 16) if protocol == 'axi3' else (4, 16, 32):
                add(src, 'write', o + 4 * B, beats=n, axid=n & top)
                add(src, 'read', o + 4 * B, beats=n, axid=0)
            for n in (2, 4):
                add(src, 'write', o + 24 * B + B * (n // 2 - (1 if n > 2 else 0)), beats=n, burst='WRAP')
                add(src, 'read', o + 24 * B + B * (n // 2 - (1 if n > 2 else 0)), beats=n, burst='WRAP')
            add(src, 'write', o + 36 * B, beats=5, size=full - 1, axid=0)
            add(src, 'read', o + 36 * B, beats=5, size=full - 1)
            add(src, 'write', o + 44 * B + 1, beats=3)
            add(src, 'read', o + 44 * B + 1, beats=3)
            add(src, 'read', base + ERR_LO + region // 16, beats=2)
            add(src, 'write', base + ERR_LO + region // 16, beats=2)
            if protocol == 'axi3':
                # Locked and reserved AxLOCK reach no target; the read after them sees the old bytes.
                add(src, 'write', o + 48 * B, beats=2, lock=2)
                add(src, 'read', o + 48 * B, beats=2, lock=2)
                add(src, 'write', o + 48 * B, lock=3)
                add(src, 'read', o + 48 * B, lock=3)
                add(src, 'read', o + 48 * B, beats=2)
                # A W beat with another ID is written and the burst answers SLVERR.
                add(src, 'write', o + 52 * B, beats=2, axid=1, wid=2)
                add(src, 'read', o + 52 * B, beats=2)
            if exclusive:
                x = o + 56 * B
                add(src, 'read', x, beats=2, lock=1)
                add(src, 'write', x, beats=2, lock=1)
                add(src, 'write', x, beats=2, lock=1)
                add(src, 'read', x, beats=2, lock=1)
                if not m.get('concurrent'):
                    other = next(s for s in m['sources'] if s != src)
                    add(other, 'write', x, axid=0)
                add(src, 'write', x, beats=2, lock=1)
                add(src, 'read', x, beats=2)
        add(src, 'read', m['unmapped'] + region, beats=2)
        add(src, 'write', m['unmapped'] + region)
    return out


class Oracle:
    """Predicts responses, read data, AXI4 bursts and memories from the docs/protocols.md rules alone."""

    def __init__(self, mode):
        self.m = MODES[mode]
        self.mem = {name: bytearray(initial_byte(i, a) for a in range(4096))
                    for i, name in enumerate(self.m['targets'])}
        self.bursts = {t: [] for t in self.m['targets']}
        self.reservations = {}  # (target, source, AxID) -> (start, bytes)
        self.locked = {t: 0 for t in self.m['targets']}  # bursts with AxLOCK 0b01 at a target

    def target_of(self, addr):
        for name, (_, _, base, _) in self.m['targets'].items():
            if base <= addr < base + 0x1000:
                return name, base
        return None, None

    def run(self, t):
        protocol, bits, _, _ = self.m['sources'][t['src']]
        B = bits // 8
        n = t['beats']
        addrs = beat_addresses(t['addr'], t['size'], t['burst'], n)
        tname, base = self.target_of(t['addr'])
        write = t['op'] == 'write'
        if tname is None or t['lock'] >= 2:
            code = DECERR if tname is None else SLVERR
            return {'resp': [code] if write else [code] * n, 'data': [None] * (1 if write else n)}
        exclusive = t['lock'] == 1 and self.m['targets'][tname][3] == 'network'
        if t['lock'] == 1 and self.m['targets'][tname][3] == 'target':
            self.locked[tname] += 1
        key = (tname, t['src'])
        region = (t['addr'], n << t['size'])
        failing = [ERR_LO <= a - base <= ERR_HI for a in addrs]
        # An AXI4 target whose every source has its width receives one burst per burst: full-width
        # bursts and single beats unchanged, the narrow beats of a burst merged into words.
        if self.m['targets'][tname][0] == 'axi4' and all(
                v[1] == self.m['targets'][tname][1] for v in self.m['sources'].values()):
            self.bursts[tname].append(
                {'op': t['op'], 'addr': t['addr'] - base, 'len': n - 1, 'size': t['size'],
                 'burst': BURST[t['burst']]} if n == 1 or t['size'] == B.bit_length() - 1 else
                {'op': t['op']})
        if not write:
            if exclusive:
                self.reservations[key] = region
            resp, data = [], []
            for a, bad in zip(addrs, failing):
                resp.append(SLVERR if bad else EXOKAY if exclusive else OKAY)
                data.append(None if bad else sum(self.mem[tname][(a & ~(B - 1)) + k - base] << (8 * k)
                                                for k in lanes(a, t['size'], B)))
            return {'resp': resp, 'data': data}
        performed = not exclusive or self.reservations.get(key) == region
        if performed:
            for i, (a, bad) in enumerate(zip(addrs, failing)):
                if not bad:
                    for k in lanes(a, t['size'], B):
                        self.mem[tname][(a & ~(B - 1)) + k - base] = self.wbyte(t, i, k)
            lo, hi = region[0], region[0] + region[1]
            for k, (rs, rb) in list(self.reservations.items()):
                if k[0] == tname and rs < hi and lo < rs + rb:
                    del self.reservations[k]
        if any(failing) or t['wid'] != t['axid']:
            code = SLVERR
        else:
            code = EXOKAY if exclusive and performed else OKAY
        return {'resp': [code], 'data': [None]}

    @staticmethod
    def wbyte(t, beat, lane):
        return (t['seed'] + beat * 13 + lane * 7 + 0x5A) & 0xFF


def driver(src, protocol, bits, ports):
    """One manager task: AW and W (or AR), then the B (or every R beat)."""
    p = f'nl_i_{src}_'
    B = bits // 8
    wid = f' {p}wid = g_wid[k];' if p + 'wid' in ports else ''
    idw = ports[p + 'awid']['width']
    return f'''
task automatic {src}_xfer(input integer k);
 integer b;
 begin
  @(negedge clk_main);
  if (g_write[k]) begin
   {p}awaddr = g_addr[k]; {p}awlen = g_beats[k] - 1; {p}awsize = g_size[k]; {p}awburst = g_burst[k];
   {p}awid = g_id[k]; {p}awlock = g_lock[k]; {p}awcache = g_cache[k]; {p}awprot = 2; {p}awvalid = 1;
   @(posedge clk_main); while (!{p}awready) @(posedge clk_main);
   // AW may change once taken: WID compares with the AWID the burst was taken with.
   @(negedge clk_main); {p}awvalid = 0; {p}awid = ~g_id[k];
   for (b = 0; b < g_beats[k]; b = b + 1) begin
    {p}wdata = beat_data(k, b, {B}); {p}wstrb = g_strb[g_strb_at[k] + b]; {p}wlast = b == g_beats[k] - 1;{wid}
    {p}wvalid = 1;
    @(posedge clk_main); while (!{p}wready) @(posedge clk_main);
    @(negedge clk_main); {p}wvalid = 0;
   end
   @(posedge clk_main); while (!{p}bvalid) @(posedge clk_main);
   check({p}bid == g_id[k][{idw - 1}:0], "BID");
   $display("EV R %0d 0 %0d x", k, {p}bresp);
  end else begin
   {p}araddr = g_addr[k]; {p}arlen = g_beats[k] - 1; {p}arsize = g_size[k]; {p}arburst = g_burst[k];
   {p}arid = g_id[k]; {p}arlock = g_lock[k]; {p}arcache = g_cache[k]; {p}arprot = 2; {p}arvalid = 1;
   @(posedge clk_main); while (!{p}arready) @(posedge clk_main);
   @(negedge clk_main); {p}arvalid = 0;
   for (b = 0; b < g_beats[k]; b = b + 1) begin
    @(posedge clk_main); while (!{p}rvalid) @(posedge clk_main);
    check({p}rid == g_id[k][{idw - 1}:0], "RID");
    check({p}rlast == (b == g_beats[k] - 1), "RLAST");
    $display("EV R %0d %0d %0d %h", k, b, {p}rresp, {p}rdata);
   end
  end
 end
endtask
'''


def axi3_model(name, bits, index, ports):
    """An AXI3 target: W beats are taken into a queue of 16, ahead of their AW (AWREADY waits eleven
    cycles, W beats one in four), and applied in order to the oldest of up to two AW (WID must be
    its AWID); B in AW order, one read at a time, a wait cycle before each R beat."""
    t = f'nl_t_{name}_'
    idw = ports[t + 'awid']['width']
    nb = bits // 8
    clock = ports[t + 'awvalid']['clock']
    return f'''
reg [7:0] mem_{name} [0:4095];
initial begin : init_{name}
 integer a;
 for (a = 0; a < 4096; a = a + 1) mem_{name}[a] = a * 7 + (a >> 8) * 13 + 8'h40 + {index * 17};
end
function automatic err_{name}(input [63:0] a); err_{name} = (a % 4096) >= {ERR_LO} && (a % 4096) <= {ERR_HI}; endfunction
function automatic [63:0] {name}_beat(input [63:0] a, input [7:0] k, input [7:0] n, input [2:0] s, input [1:0] b);
 reg [63:0] step, c, base;
 begin step = 64'd1 << s; c = step * (n + 1); base = a & ~(c - 1);
  {name}_beat = b == 0 ? a : b == 2 ? base + ((a - base + k * step) % c) : (k == 0 ? a : (a & ~(step - 1)) + k * step); end
endfunction
reg [63:0] {name}_qa [0:3]; reg [7:0] {name}_qn [0:3]; reg [2:0] {name}_qs [0:3]; reg [1:0] {name}_qb [0:3];
reg [{idw - 1}:0] {name}_qi [0:3]; reg [{idw - 1}:0] {name}_bi [0:3]; reg {name}_be [0:3];
reg [2:0] {name}_qc = 0, {name}_bc = 0; reg [1:0] {name}_qh = 0, {name}_qt = 0, {name}_bh = 0, {name}_bt = 0;
reg [7:0] {name}_wk = 0; reg {name}_werr = 0; reg [1:0] {name}_wgap = 0; reg [3:0] {name}_await = 0;
reg [{bits - 1}:0] {name}_fd [0:15]; reg [{nb - 1}:0] {name}_fs [0:15]; reg [{idw - 1}:0] {name}_fi [0:15];
reg {name}_fl [0:15]; reg [4:0] {name}_fc = 0; reg [3:0] {name}_fh = 0, {name}_ft = 0;
reg {name}_ract = 0, {name}_rgap = 0; reg [63:0] {name}_ra; reg [7:0] {name}_rn, {name}_rk; reg [2:0] {name}_rs;
reg [1:0] {name}_rb; reg [{idw - 1}:0] {name}_rid;
wire [63:0] {name}_rbeat = {name}_beat({name}_ra, {name}_rk, {name}_rn, {name}_rs, {name}_rb);
wire [63:0] {name}_wbeat = {name}_beat({name}_qa[{name}_qh], {name}_wk, {name}_qn[{name}_qh], {name}_qs[{name}_qh], {name}_qb[{name}_qh]);
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
assign {t}awready = {name}_qc < 2 && {name}_bc < 2 && {name}_await == 11;
assign {t}wready = {name}_fc < 16 && {name}_wgap == 3;
assign {t}bvalid = {name}_bc != 0;
assign {t}bresp = {name}_be[{name}_bh] ? 2'b10 : 2'b00;
assign {t}bid = {name}_bi[{name}_bh];
always @(posedge {clock}) if (!reset_n) begin
 {name}_ract <= 0; {name}_rgap <= 0; {name}_qc <= 0; {name}_bc <= 0; {name}_qh <= 0; {name}_qt <= 0;
 {name}_bh <= 0; {name}_bt <= 0; {name}_wk <= 0; {name}_werr <= 0; {name}_wgap <= 0; {name}_await <= 0;
 {name}_fc <= 0; {name}_fh <= 0; {name}_ft <= 0;
end else begin : step_{name}
 integer j; reg [2:0] qc, bc; reg [4:0] fc;
 qc = {name}_qc; bc = {name}_bc; fc = {name}_fc;
 {name}_rgap <= {name}_ract && !{name}_rgap;
 if ({name}_wgap != 3) {name}_wgap <= {name}_wgap + 1;
 if ({t}awvalid && {name}_await != 11) {name}_await <= {name}_await + 1;
 if ({t}arvalid && {t}arready) begin
  check({t}arlock <= 1, "ARLOCK");
  $display("EV A {name} read %0h %0d %0d %0d %0d %0d", {t}araddr % 4096, {t}arlen, {t}arsize, {t}arburst, {t}arcache, {t}arprot);
  $display("EV L {name} %0d", {t}arlock);
  {name}_ra <= {t}araddr; {name}_rn <= {t}arlen; {name}_rs <= {t}arsize; {name}_rb <= {t}arburst;
  {name}_rid <= {t}arid; {name}_rk <= 0; {name}_ract <= 1; {name}_rgap <= 0;
 end
 if ({t}rvalid && {t}rready) begin
  {name}_rgap <= 0;
  if ({name}_rk == {name}_rn) {name}_ract <= 0; else {name}_rk <= {name}_rk + 1;
 end
 if ({t}wvalid && {t}wready) begin
  {name}_wgap <= 0;
  {name}_fd[{name}_ft] <= {t}wdata; {name}_fs[{name}_ft] <= {t}wstrb; {name}_fi[{name}_ft] <= {t}wid;
  {name}_fl[{name}_ft] <= {t}wlast; {name}_ft <= {name}_ft + 1; fc = fc + 1;
 end
 if (qc != 0 && {name}_fc != 0) begin
  check({name}_fi[{name}_fh] == {name}_qi[{name}_qh], "WID");
  if (err_{name}({name}_wbeat)) {name}_werr <= 1;
  else for (j = 0; j < {nb}; j = j + 1)
   if ({name}_fs[{name}_fh][j]) mem_{name}[({name}_wbeat % 4096) / {nb} * {nb} + j] = {name}_fd[{name}_fh][8 * j +: 8];
  {name}_fh <= {name}_fh + 1; fc = fc - 1;
  if ({name}_wk == {name}_qn[{name}_qh]) begin
   check({name}_fl[{name}_fh], "WLAST");
   {name}_bi[{name}_bt] <= {name}_qi[{name}_qh]; {name}_be[{name}_bt] <= {name}_werr || err_{name}({name}_wbeat);
   {name}_bt <= {name}_bt + 1; bc = bc + 1;
   {name}_qh <= {name}_qh + 1; qc = qc - 1; {name}_wk <= 0; {name}_werr <= 0;
  end else {name}_wk <= {name}_wk + 1;
 end
 if ({t}awvalid && {t}awready) begin
  {name}_await <= 0;
  check({t}awlock <= 1, "AWLOCK");
  $display("EV A {name} write %0h %0d %0d %0d %0d %0d", {t}awaddr % 4096, {t}awlen, {t}awsize, {t}awburst, {t}awcache, {t}awprot);
  $display("EV L {name} %0d", {t}awlock);
  {name}_qa[{name}_qt] <= {t}awaddr; {name}_qn[{name}_qt] <= {t}awlen; {name}_qs[{name}_qt] <= {t}awsize;
  {name}_qb[{name}_qt] <= {t}awburst; {name}_qi[{name}_qt] <= {t}awid; {name}_qt <= {name}_qt + 1; qc = qc + 1;
 end
 if ({t}bvalid && {t}bready) begin {name}_bh <= {name}_bh + 1; bc = bc - 1; end
 {name}_qc <= qc; {name}_bc <= bc; {name}_fc <= fc;
end
'''


def bench(mode, mapping, every):
    m = MODES[mode]
    ports = {p['name']: p for p in mapping['ports']}
    driven, models = set(), []
    for index, (name, (protocol, bits, _, _)) in enumerate(m['targets'].items()):
        models.append(axi3_model(name, bits, index, ports) if protocol == 'axi3' else
                      target_model(name, protocol.replace('_v3', ''), bits, index, ports))
        driven |= {p for p, v in ports.items() if v['endpoint'] == f'target:{name}' and v['direction'] == 'input'}
    decl, conns, init = [], [], []
    for name, p in ports.items():
        wire = 'reset_n' if p.get('signal') == 'reset_n' else name
        if p['direction'] == 'input' and name not in driven:
            decl.append(f'reg [{p["width"] - 1}:0] {wire};')
            if wire != 'reset_n' and p.get('signal') != 'clock':
                init.append(f' {wire} = 0;')
        else:
            decl.append(f'wire [{p["width"] - 1}:0] {wire};')
        conns.append(f'.{name}({wire})')
    for src in m['sources']:
        for ready in ('bready', 'rready'):
            init.append(f' nl_i_{src}_{ready} = 1;')
    lines, strobes = [], []
    for k, t in enumerate(every):
        B = m['sources'][t['src']][1] // 8
        lines.append(f" g_addr[{k}] = 64'h{t['addr']:x}; g_size[{k}] = {t['size']}; g_burst[{k}] = {BURST[t['burst']]}; "
                     f"g_beats[{k}] = {t['beats']}; g_write[{k}] = {int(t['op'] == 'write')}; g_id[{k}] = {t['axid']}; "
                     f"g_wid[{k}] = {t['wid']}; g_lock[{k}] = {t['lock']}; g_cache[{k}] = {t['cache']}; "
                     f"g_seed[{k}] = {t['seed']}; g_strb_at[{k}] = {len(strobes)}; g_src[{k}] = {list(m['sources']).index(t['src'])};")
        for a in beat_addresses(t['addr'], t['size'], t['burst'], t['beats']):
            strobes.append(sum(1 << b for b in lanes(a, t['size'], B)))
    lines += [f" g_strb[{j}] = 128'h{v:x};" for j, v in enumerate(strobes)]
    widest = max(v[1] for v in m['sources'].values())
    calls = ''.join(f'  if (g_src[k] == {i}) {src}_xfer(k);\n' for i, src in enumerate(m['sources']))
    if m.get('concurrent'):
        # Each source runs its own bursts in order, all sources at once.
        own = {src: [k for k, t in enumerate(every) if t['src'] == src] for src in m['sources']}
        lines += [f' {src}_list[{j}] = {k};' for src, ks in own.items() for j, k in enumerate(ks)]
        lists = ''.join(f'integer {src}_list [0:{max(len(ks), 1) - 1}];\n' for src, ks in own.items())
        loop = ' fork\n' + ''.join(
            f'  begin : run_{src}\n   integer j;\n   for (j = 0; j < {len(ks)}; j = j + 1) begin\n'
            f'    $display("EV T %0d", {src}_list[j]); {src}_xfer({src}_list[j]); repeat (2) @(posedge clk_main);\n'
            f'   end\n  end\n' for src, ks in own.items()) + ' join\n'
    else:
        lists = ''
        loop = f''' for (k = 0; k < {len(every)}; k = k + 1) begin
  $display("EV T %0d", k);
{calls}  repeat (2) @(posedge clk_main);
 end
'''
    drivers = ''.join(driver(src, v[0], v[1], ports).replace(
        'clk_main', ports[f'nl_i_{src}_awvalid']['clock']) for src, v in m['sources'].items())
    clocks = ''.join(f'initial clk_{name} = 0;\nalways #{period / 2} clk_{name} = ~clk_{name};\n'
                     for name, period in m.get('domains', {'main': 10}).items())
    dumps = ''.join(f' for (a = 0; a < 4096; a = a + 1) $display("EV M {name} %0h %h", a, mem_{name}[a]);\n'
                    for name in m['targets'])
    N = len(every)
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
integer cycle = 0;
{clocks}
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 400000) begin $display("ASSERT_FAIL:AXI3.TIMEOUT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*16-1:0] label_;
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:AXI3.%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
reg [63:0] g_addr [0:{N - 1}]; reg [2:0] g_size [0:{N - 1}]; reg [1:0] g_burst [0:{N - 1}]; integer g_beats [0:{N - 1}];
reg g_write [0:{N - 1}]; reg [7:0] g_id [0:{N - 1}]; reg [7:0] g_wid [0:{N - 1}]; reg [1:0] g_lock [0:{N - 1}];
reg [3:0] g_cache [0:{N - 1}]; integer g_seed [0:{N - 1}]; integer g_strb_at [0:{N - 1}]; integer g_src [0:{N - 1}];
reg [127:0] g_strb [0:{len(strobes) - 1}];
{lists}initial begin
{chr(10).join(lines)}
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
initial begin : run
 integer k, a;
 reset_n = 0;
{chr(10).join(init)}
 repeat (10) @(posedge clk_main);
 @(negedge clk_main); reset_n = 1;
 repeat (5) @(posedge clk_main);
{loop} repeat (20) @(posedge clk_main);
{dumps} $display("NOCLOOM_PASS axi3 {mode}");
 $finish;
end
endmodule
'''


def parse(log):
    out = {'resp': {}, 'bursts': {}, 'mem': {}, 'locked': {}}
    for line in log.splitlines():
        f = line.split()
        if len(f) < 2 or f[0] != 'EV':
            continue
        if f[1] == 'R':
            out['resp'].setdefault(int(f[2]), []).append(
                (int(f[4]), None if 'x' in f[5].lower() else int(f[5], 16)))
        elif f[1] == 'A':
            out['bursts'].setdefault(f[2], []).append({'op': f[3], 'addr': int(f[4], 16), 'len': int(f[5]),
                                                       'size': int(f[6]), 'burst': int(f[7])})
        elif f[1] == 'M':
            out['mem'].setdefault(f[2], {})[int(f[3], 16)] = int(f[4], 16)
        elif f[1] == 'L':
            out['locked'][f[2]] = out['locked'].get(f[2], 0) + int(f[3] == '1')
    return out


def compare(mode, log, every):
    oracle = Oracle(mode)
    got = parse(log)
    problems = []
    for k, t in enumerate(every):
        want = oracle.run(t)
        beats = got['resp'].get(k, [])
        if [r for r, _ in beats] != want['resp']:
            problems.append({'tx': t['name'], 'want_resp': want['resp'], 'got_resp': [r for r, _ in beats]})
            continue
        B = MODES[mode]['sources'][t['src']][1] // 8
        addrs = beat_addresses(t['addr'], t['size'], t['burst'], t['beats'])
        for i, ((_, data), expect) in enumerate(zip(beats, want['data'])):
            if expect is None:
                continue
            mask = sum(0xFF << (8 * b) for b in lanes(addrs[i], t['size'], B))
            if data is None or data & mask != expect:
                problems.append({'tx': t['name'], 'beat': i, 'want': hex(expect),
                                 'got': hex(data) if data is not None else 'x'})
    for name, want in oracle.bursts.items():
        seen = [{'op': b['op']} if len(w) == 1 else b for w, b in zip(want, got['bursts'].get(name, []))]
        if want and (seen != want or len(got['bursts'].get(name, [])) != len(want)):
            problems.append({'target': name, 'want_bursts': len(want), 'got_bursts': len(got['bursts'].get(name, [])),
                             'first_difference': next(({'want': a, 'got': b} for a, b in
                                                       zip(want, seen) if a != b), None)})
    for name, count in oracle.locked.items():
        if count != got['locked'].get(name, 0):
            problems.append({'target': name, 'want_locked_bursts': count,
                             'got_locked_bursts': got['locked'].get(name, 0)})
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
    stem = 'nocloom_axi3_' + args.mode
    mapping = json.loads((work / f'first/{stem}.map.json').read_text())
    definition = mapping['definition']
    m = MODES[args.mode]
    ports = {p['name']: p for p in mapping['ports']}
    for src, (protocol, bits, ids, _) in m['sources'].items():
        assert definition['endpoint_protocols']['initiators'][src] == protocol, definition['endpoint_protocols']
        if protocol == 'axi3':
            widths = {s: ports[f'nl_i_{src}_{s}']['width'] for s in ('awlock', 'arlock', 'awlen', 'arlen', 'wid')}
            assert widths == {'awlock': 2, 'arlock': 2, 'awlen': 4, 'arlen': 4, 'wid': ids}, widths
    if any(v[0] == 'axi3' for v in m['sources'].values()):
        assert definition['contract']['axi3_ingress']['lock'].startswith('AxLOCK_01_exclusive'), definition['contract']
    for name, (protocol, _, _, _) in m['targets'].items():
        if protocol == 'axi3':
            assert definition['endpoint_protocols']['targets'][name] == 'axi3', definition['endpoint_protocols']
            widths = {s: ports[f'nl_t_{name}_{s}']['width'] for s in ('awlock', 'arlock', 'awlen', 'arlen')}
            assert widths == {'awlock': 2, 'arlock': 2, 'awlen': 4, 'arlen': 4}, widths
            assert ports[f'nl_t_{name}_wid']['width'] == ports[f'nl_t_{name}_awid']['width']
    every = directed(args.mode)
    for k, t in enumerate(every):
        t['seed'] = (k * 29) & 0xFF
    case = work / 'simulation'
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(work / f'first/{stem}.v'))
    (case / 'tb.sv').write_text(bench(args.mode, mapping, every))
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    problems = compare(args.mode, log, every)
    (case / 'mismatches.json').write_text(json.dumps(problems, indent=1) + '\n')
    passed = rc == 0 and f'NOCLOOM_PASS axi3 {args.mode}' in log and 'ASSERT_FAIL' not in log and not problems
    (work / 'results.json').write_text(json.dumps(
        {'mode': args.mode, 'bursts': len(every), 'simulation_exit': rc, 'mismatches': len(problems),
         'status': 'PASS' if passed else 'FAIL',
         'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
         'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}, indent=1) + '\n')
    assert passed, (rc, problems[:3], log[-2500:])
    print(f'NOCLOOM_PASS axi3 mode={args.mode} bursts={len(every)}')
    if args.mode == 'target':
        from protocol_combinations import run_axi3_graph
        run_axi3_graph(args.cli, work / 'graph-clocked')


if __name__ == '__main__':
    main()
