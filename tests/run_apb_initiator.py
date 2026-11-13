"""Schema 10 APB4 and APB3 initiators through the public CLI.

An APB initiator issues each transfer as one AXI4-Lite beat of the 32-bit
network: PADDR is cleared to its word before any check and PSTRB passes as
given (APB3: all ones, PPROT unprivileged Non-secure data), every failure
answers PSLVERR with PRDATA zero, and an unstrobed byte reaches the target as
zero. The bench mixes APB4, APB3, AXI4-Lite and AXI4 initiators with APB4,
APB3, APB2, AXI4-Lite, AXI4 and AHB target models (the AHB model fails reads
at one offset) in these networks: a two-node graph, a four-node tree, a
clocked graph, the generated switch without a topology, the generated switch
with a target timeout, a graph with a connectivity matrix (unconnected
pairs), and the graph with an AXI4 target keeping two packet slots shared by
reads and writes (mode multi: the APB, AXI4-Lite and AXI4 initiators all reach
it, and its bus model queues two reads and two writes). An independent oracle
predicts every target access and every source result from the declared
windows and routes:

- directed transfers one at a time (all PSTRB masks, all PPROT values,
  unaligned PADDR, window edges, sub-page remainders, unmapped addresses,
  target errors, zero-strobe writes, back-to-back transfers), compared per
  transfer;
- every source streaming at once to every target, compared as per-source
  result sequences and the multiset of target accesses;
- the common reset at four points: an APB transfer waiting in ACCESS for its
  target (in mode timeout the target times out first: PSLVERR, then a read of
  the failed target answers PSLVERR with PRDATA zero), an APB transfer in
  SETUP, an APB transfer one cycle into ACCESS, and every source streaming
  with transfers in the network. Before the reset each source's results are a
  prefix of its stream and every target access belongs to a streamed
  transfer; after it, new transfers from every source complete exactly.

Mode apb4_tree runs the same transfers on an APB4-only rooted tree.

The testbench also checks that PREADY rises only in ACCESS, that PRDATA is
zero with PSLVERR and on a write, that PSLVERR and PRDATA keep their last answer between answers, and that
no PSEL or PREADY is high after a clock edge with reset held low."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path

from bench_registered import registered
from cross_hardware_sim import simulate
from endpoint_ports import endpoint_ports
from sim_engine import generated_rtl

# Target ports a registered model loads at the falling edge (bench_registered).
REGISTERED = set()

ERROR_OFFSET = 0x2C  # every target model that can fail answers an error here


def mirror(request, targets, sources):
    """The response graph: the request tree reversed."""
    return {'nodes': [dict(n) for n in request['nodes']],
            'links': [{'from': l['to'], 'to': l['from']} for l in request['links']],
            'entries': [{'endpoint': t, 'node': x['node']} for x in request['exits'] for t in [x['endpoint']]],
            'exits': [{'endpoint': s, 'node': x['node']} for x in request['entries'] for s in [x['endpoint']]]}


# name: (protocol, address bits or None, domain)
# Targets: name: (protocol, base, size, local address bits or None, domain)
MODES = {
    'graph': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb4', None, 'main'),
                    'a2': ('apb3', 17, 'main'), 'l0': ('axi4_lite', None, 'main'),
                    'x0': ('axi4', None, 'main')},
        'targets': {'tp': ('apb4', 0x10000, 0x1000, 12, 'main'), 't3': ('apb3', 0x12000, 0x1000, 12, 'main'),
                    't2': ('apb2', 0x13000, 0x1000, 12, 'main'), 'tl': ('axi4_lite', 0x14000, 0x1000, 12, 'main'),
                    'ta': ('axi4', 0x15000, 0x1000, None, 'main'), 'th': ('ahb_v3', 0x16000, 0x1000, 12, 'main'),
                    'ts': ('apb4', 0x18000, 0x100, 8, 'main')},
        'nodes': [('root', None), ('periph', None)], 'links': [('root', 'periph')],
        'entry': 'root', 'exit': {'ta': 'root', 'tl': 'root'}, 'default_exit': 'periph'},
    # The graph with a route list: pairs without a route answer like an
    # unmapped address (PSLVERR, DECERR at AXI sources), without access.
    'selective': {
        'sources': {'a0': ('apb4', None, 'main'), 'a2': ('apb3', 17, 'main'), 'l0': ('axi4_lite', None, 'main')},
        'targets': {'tp': ('apb4', 0x10000, 0x1000, 12, 'main'), 't3': ('apb3', 0x12000, 0x1000, 12, 'main'),
                    'tl': ('axi4_lite', 0x14000, 0x1000, 12, 'main'), 'ta': ('axi4', 0x15000, 0x1000, None, 'main')},
        'nodes': [('root', None), ('periph', None)], 'links': [('root', 'periph')],
        'entry': 'root', 'exit': {'ta': 'root', 'tl': 'root'}, 'default_exit': 'periph',
        'no_route': {('a0', 'ta'), ('a2', 't3'), ('a2', 'tl'), ('l0', 'tp')}},
    # A rooted tree: every source enters at the root, targets
    # hang off a branch and a chain.
    'tree': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb4', None, 'main'),
                    'a2': ('apb4', None, 'main'), 'b0': ('apb3', None, 'main')},
        'targets': {'tp': ('apb4', 0x0000, 0x1000, 12, 'main'), 't1': ('apb4', 0x2000, 0x2000, 13, 'main'),
                    't2': ('apb4', 0x5000, 0x1000, 12, 'main'), 't3': ('apb3', 0x7000, 0x1000, 12, 'main'),
                    'tl': ('axi4_lite', 0xfffff000, 0x1000, 12, 'main')},
        'nodes': [('n0', None), ('n1', None), ('n2', None), ('n3', None)],
        'links': [('n0', 'n1'), ('n0', 'n2'), ('n1', 'n3')],
        'entry': 'n0', 'exit': {'tp': 'n0', 't1': 'n1', 't2': 'n2', 't3': 'n3', 'tl': 'n3'},
        'default_exit': 'n0'},
    'clocked': {
        'sources': {'a0': ('apb4', None, 'slow'), 'a1': ('apb3', None, 'main'),
                    'l0': ('axi4_lite', None, 'main')},
        'targets': {'tp': ('apb4', 0x10000, 0x1000, 12, 'main'), 't3': ('apb3', 0x11000, 0x1000, 12, 'mid'),
                    'ta': ('axi4', 0x20000, 0x1000, None, 'slow')},
        'nodes': [('root', 'mid'), ('leaf', 'main')], 'links': [('root', 'leaf')],
        'entry': 'root', 'exit': {'ta': 'root'}, 'default_exit': 'leaf',
        'domains': {'main': (2, 10), 'slow': (3, 17), 'mid': (2, 7)}},
    # APB4 only on a rooted tree.
    'apb4_tree': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb4', None, 'main'),
                    'a2': ('apb4', None, 'main')},
        'targets': {'tp': ('apb4', 0x0000, 0x1000, 12, 'main'), 't1': ('apb4', 0x2000, 0x2000, 13, 'main'),
                    't2': ('apb4', 0x5000, 0x1000, 12, 'main'), 't3': ('apb4', 0xfffff000, 0x1000, 12, 'main')},
        'nodes': [('n0', None), ('n1', None), ('n2', None), ('n3', None)],
        'links': [('n0', 'n1'), ('n0', 'n2'), ('n1', 'n3')],
        'entry': 'n0', 'exit': {'tp': 'n0', 't1': 'n1', 't2': 'n2', 't3': 'n3'},
        'default_exit': 'n0'},
    # Audit: the generated switch with a 1024-cycle timeout (completion) on tp and an AHB target that
    # fails reads.
    'timeout': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb3', None, 'main')},
        'targets': {'tp': ('apb4', 0x1000, 0x1000, 12, 'main'), 'th': ('ahb_v3', 0x9000, 0x1000, 12, 'main')},
        'nodes': None},
    # The graph with ta sharing two transaction slots across reads and writes.
    'multi': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb4', None, 'main'),
                    'a2': ('apb3', 17, 'main'), 'l0': ('axi4_lite', None, 'main'),
                    'x0': ('axi4', None, 'main')},
        'targets': {'tp': ('apb4', 0x10000, 0x1000, 12, 'main'), 'tl': ('axi4_lite', 0x14000, 0x1000, 12, 'main'),
                    'ta': ('axi4', 0x15000, 0x1000, None, 'main'), 'th': ('ahb_v3', 0x16000, 0x1000, 12, 'main')},
        'nodes': [('root', None), ('periph', None)], 'links': [('root', 'periph')],
        'entry': 'root', 'exit': {'ta': 'root', 'tl': 'root'}, 'default_exit': 'periph',
        'target_outstanding': {'ta': 2}},
    # No topology: the generated two-source, two-target switch.
    'funnel': {
        'sources': {'a0': ('apb4', None, 'main'), 'a1': ('apb3', None, 'main')},
        'targets': {'tp': ('apb4', 0x1000, 0x1000, 12, 'main'), 'ta': ('axi4', 0x8000, 0x1000, None, 'main')},
        'nodes': None},
}
# Narrow APB faces retain a 32-bit network and target model. Each mode
# includes both protocols at both widths, next to an unchanged AXI source.
MODES['narrow'] = deepcopy(MODES['graph'])
MODES['narrow']['targets'].pop('th')
MODES['narrow']['sources']['b0'] = ('apb3', None, 'main')
MODES['narrow']['source_widths'] = {'a0': 8, 'a1': 16, 'a2': 8, 'b0': 16}
MODES['narrow_clocked'] = deepcopy(MODES['clocked'])
MODES['narrow_clocked']['sources'].update(a2=('apb3', None, 'mid'), b0=('apb4', None, 'main'))
MODES['narrow_clocked']['source_widths'] = {'a0': 8, 'a1': 16, 'a2': 8, 'b0': 16}
APB = ('apb4', 'apb3')
def configuration(mode):
    m = MODES[mode]
    domains = m.get('domains', {'main': None})
    clock_domains = []
    for name in domains:
        clock_domains.append({'name': name, 'clock_port': 'clk_' + name, 'reset_port': 'reset_n', 'test_port': 'test'})
    initiators = []
    for name, (protocol, bits, domain) in m['sources'].items():
        entry = {'name': name, 'clock_domain': domain, 'protocol': protocol, 'data_width': m.get('source_widths', {}).get(name, 32)}
        if bits:
            entry['address_width'] = bits
        initiators.append(entry)
    targets = []
    for name, (protocol, base, size, bits, domain) in m['targets'].items():
        entry = {'name': name, 'clock_domain': domain, 'protocol': protocol, 'data_width': 32,
                 'mappings': [{'base': hex(base), 'size': hex(size)}]}
        if bits:
            entry['address_width'] = bits
        if mode == 'timeout' and name == 'tp':
            entry['timeout'] = {'cycles': 1024, 'completion': True}
        if m.get('target_outstanding', {}).get(name, 1) > 1:
            entry['max_outstanding'] = m['target_outstanding'][name]
        targets.append(entry)
    config = {'schema_version': 10, 'name': 'apb_initiator_' + mode, 'address_width': 32,
              'arbitration': {'policy': 'round_robin'}, 'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': clock_domains,
              'initiators': initiators, 'targets': targets,
              'transport': {'max_outstanding': 1, 'transport_word_bytes': 4,
                            'response_codes': 'distinct'},
              'connectivity': 'all',
              'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if any(domains.values()):
        # Every crossing takes the largest stage count of the mode.
        config['synchronizer_stages'] = max(v[0] for v in domains.values())
    if 'no_route' in m:
        config['connectivity'] = {a: [b for b in m['targets'] if (a, b) not in m['no_route']]
                                  for a in m['sources']}
    if m['nodes']:
        nodes = [dict({'name': n}, **({'clock_domain': d} if d else {})) for n, d in m['nodes']]
        request = {'nodes': nodes, 'links': [{'from': a, 'to': b} for a, b in m['links']],
                   'entries': [{'endpoint': s, 'node': m['entry']} for s in m['sources']],
                   'exits': [{'endpoint': t, 'node': m['exit'].get(t, m['default_exit'])} for t in m['targets']]}
        config['topology'] = {'request': request, 'response': mirror(request, m['targets'], m['sources'])}
    return endpoint_ports(config, id_width=2, len_width=8)


def pattern(index, local):
    # Target read data: a target tag above the local word address.
    return ((0xa0 + index) << 24) | (local & 0xfffff)


def lanes(strobe):
    return sum(0xff << (8 * k) for k in range(4) if strobe >> k & 1)


def expected(mode, t):
    """(target accesses, source result) of one transfer t."""
    m = MODES[mode]
    protocol = m['sources'][t['source']][0]
    apb = protocol in APB
    source_bits = m.get('source_widths', {}).get(t['source'], 32)
    source_bytes = source_bits // 8
    source_mask = (1 << source_bytes) - 1
    write, strobe, prot = t['write'], t['strobe'] & source_mask, t['prot']
    if protocol == 'apb3':
        strobe, prot = source_mask, 2  # unprivileged, Non-secure, data
    if not write:
        strobe = 0
    aligned = t['address'] & ~(source_bytes - 1)
    lane = aligned % 4
    strobe <<= lane
    source_data = (t['data'] & ((1 << source_bits) - 1)) << (8 * lane)
    owner = None
    for index, (name, (kind, base, size, _, _)) in enumerate(m['targets'].items()):
        if base <= aligned < base + max(size, 0x1000):
            owner = (index, name, kind, base, size)

    def result(code, data=0):
        if apb:
            return ('S', t['source'], int(code != 0), (data >> (8 * lane)) & ((1 << source_bits) - 1)
                    if code == 0 and not write else 0)
        return ('S', t['source'], code, data if code == 0 and not write else 0)

    if owner is None or not owner[3] <= aligned < owner[3] + owner[4]:
        return [], result(3)  # unmapped, or the page remainder of a sub-page window
    if (t['source'], owner[1]) in m.get('no_route', ()):
        return [], result(3)  # no route from this source to the target
    index, name, kind, base, _ = owner
    local = aligned - base
    if kind != 'axi4':
        local &= ~3
    fails = local & 0x3f == ERROR_OFFSET and (kind in ('apb4', 'apb3', 'axi4_lite', 'axi4') or (kind == 'ahb_v3' and not write))
    code = 2 if fails else 0
    shown_prot = prot if kind in ('apb4', 'axi4_lite', 'axi4') else 0
    if not write:
        return [(name, 0, local, 0, 0, shown_prot)], result(code, pattern(index, local))
    if kind in ('apb4', 'apb3', 'apb2', 'ahb_v3') and strobe == 0:
        return [], result(0)  # a write without strobes completes without target access
    if kind in ('apb3', 'apb2') and strobe != 0xf:
        return [], result(2)  # no PSTRB: a partial write answers SLVERR without access
    shown_strobe = 0xf if kind in ('apb3', 'apb2') else strobe
    # Sources zero every unstrobed byte.
    data = source_data & lanes(strobe)
    return [(name, 1, local, shown_strobe, data, shown_prot)], result(code)


def unmapped_addresses(mode, source):
    bits = MODES[mode]['sources'][source][1] or 32
    top = (1 << bits) - 1
    targets = MODES[mode]['targets'].values()

    def free(a):
        return all(not base <= (a & ~3) < base + max(size, 0x1000) for _, base, size, _, _ in targets)
    candidates = [top - 3, top, top & 0x8000_0003, 0x1f003 & top, 0x0ffd]
    return [a for a in dict.fromkeys(candidates) if free(a)][:2]


def directed(mode):
    """Transfers issued one at a time, each compared on its own."""
    m = MODES[mode]
    out = []

    def add(source, write, address, strobe=0xf, prot=None, gap=1):
        n = len(out)
        out.append({'name': f'd{n}-{source}-{"w" if write else "r"}-{address:x}', 'source': source,
                    'write': write, 'address': address, 'strobe': strobe if write else 0,
                    'prot': (n * 3) % 8 if prot is None else prot, 'gap': gap,
                    'data': int.from_bytes(bytes((n * 37 + k * 11 + 5) % 256 for k in range(4)), 'little')})

    apb_sources = [s for s, (p, _, _) in m['sources'].items() if p in APB]
    for source in apb_sources:
        full_targets = [(n, v) for n, v in m['targets'].items() if v[2] >= 0x1000]
        for name, (kind, base, size, _, _) in full_targets:
            masks = range(16) if kind in ('apb4', 'axi4_lite') and source == apb_sources[0] else (0xf, 0x3, 0x0, 0x8)
            for k, mask in enumerate(masks):
                add(source, 1, base + 0x40 + 4 * k, mask, gap=k % 2)
            add(source, 1, base + 0x41 + 0x100, 0xe)         # unaligned PADDR, PSTRB as given
            add(source, 1, base + 0xc2 + 0x100, 0xf, gap=0)  # a whole word from an unaligned PADDR
            add(source, 0, base + 0x46 + 0x100, gap=0)       # unaligned read: the whole word
            add(source, 1, base + ERROR_OFFSET, 0xf)          # target error
            add(source, 0, base + ERROR_OFFSET, gap=0)        # target error read: PRDATA zero
            add(source, 0, base + 0x100 + ERROR_OFFSET + 1, gap=0)  # unaligned error read
            add(source, 1, base + 0x40 + ERROR_OFFSET, 0x0)   # zero strobes where the target fails
            add(source, 1, base, 0x1)                          # first byte
            add(source, 0, base + size - 1)                    # last byte
            if base + size < 1 << (m['sources'][source][1] or 32):
                add(source, 0, base + size)                    # one past the end
        for name, (kind, base, size, _, _) in m['targets'].items():
            if size < 0x1000:  # sub-page window and its page remainder
                add(source, 0, base + size - 1)
                add(source, 1, base + size - 3, 0x6)
                add(source, 0, base + size + 1)
                add(source, 1, base + size + 2, 0xf)
        for address in unmapped_addresses(mode, source):
            add(source, 0, address)
            add(source, 1, address, 0xf)
        # All eight PPROT values back to back on the first target.
        first = next(iter(m['targets'].values()))
        for prot in range(8):
            add(source, prot % 2, first[1] + 0x200 + 4 * prot, 0xf, prot=prot, gap=0)
    for source, width in m.get('source_widths', {}).items():
        for name, (kind, base, size, _, _) in m['targets'].items():
            for offset in range(4):
                for mask in range(1 << (width // 8)):
                    add(source, 1, base + 0x240 + offset, mask, gap=0)
                    add(source, 0, base + 0x240 + offset, gap=0)
    for source, (protocol, _, _) in m['sources'].items():
        if protocol not in APB:
            for name, (kind, base, size, _, _) in m['targets'].items():
                add(source, 1, base + 0x300, 0x5)
                add(source, 0, base + 0x300)
            add(source, 0, unmapped_addresses(mode, source)[0])
    return back_to_back(out)


def back_to_back(seq):
    # A transfer leaves PSEL high (gap 0) only when the same source starts the
    # next transfer in the following cycle.
    for t, following in zip(seq, seq[1:] + [None]):
        if following is None or following['source'] != t['source']:
            t['gap'] = max(t['gap'], 1)
    return seq


def streams(mode):
    """Per-source transfer lists issued concurrently."""
    m = MODES[mode]
    names = list(m['targets'])
    out = {}
    for s, (source, (protocol, bits, _)) in enumerate(m['sources'].items()):
        seq = []
        for visit in range(len(names)):
            name = names[(visit + s) % len(names)]
            _, base, size, _, _ = m['targets'][name]
            for n in range(6):
                local = (s * 0x80 + n * 4) % size
                if n == 3:
                    local = (s * 0x80 + ERROR_OFFSET) % size
                data = (0x983a1700 ^ (s * 151 + n + visit * 256)) & 0xffffffff
                gap = 0 if (n + s) % 3 == 0 else 1
                seq.append({'source': source, 'write': 1, 'address': base + local + (n % 4 if protocol in APB else 0),
                            'strobe': (n * 5 + s) % 16, 'prot': (n + s) % 8, 'data': data, 'gap': gap})
                seq.append({'source': source, 'write': 0, 'address': base + local, 'strobe': 0,
                            'prot': (n + 2 * s) % 8, 'data': 0, 'gap': gap})
        for name, k in m.get('target_outstanding', {}).items():
            # Every source reads the multi-transaction target at once first:
            # reads of different sources must not overlap at its outlet.
            _, base, size, _, _ = m['targets'][name]
            seq = [{'source': source, 'write': 0, 'address': base + (s * 0x80 + 0x40 + n * 4) % size,
                    'strobe': 0, 'prot': (n + s) % 8, 'data': 0, 'gap': 0}
                   for n in range(6)] + seq
        out[source] = back_to_back(seq)
    return out


def target_models(mode, ports, check_reset):
    m = MODES[mode]
    models, driven = [], set()
    size_comparison = '<= 2' if m.get('source_widths') else '=== 2'
    for index, (name, (kind, base, size, bits, domain)) in enumerate(m['targets'].items()):
        t = f'nl_t_{name}_'
        clk = f'clk_{domain}'
        tag = f"8'h{0xa0 + index:02x}"
        lb = bits or 20
        # An APB target of a tree drives a full-width PADDR offset.
        local = f'{name}_local' if kind.startswith('apb') else None
        fail = f"({{a}}[5:0] == 6'h{ERROR_OFFSET:02x})"
        if kind in ('apb4', 'apb3', 'apb2'):
            strobe = f'{t}pstrb' if kind == 'apb4' else "4'hf"
            prot = f'{t}pprot' if kind == 'apb4' else "3'd0"
            driven |= {t + 'prdata'}
            if kind != 'apb2':
                driven |= {t + 'pready', t + 'pslverr'}
            done = f'{t}psel && {t}penable' + (f' && {t}pready' if kind != 'apb2' else '')
            models.append(f'''
reg {name}_wait, {name}_hold, {name}_rq;
wire [{lb - 1}:0] {name}_local = {t}paddr[{lb - 1}:0];
assign {t}prdata = {{{tag}, 4'd0, {20 - lb}'d0, {local}}};
{f"assign {t}pready = {t}psel && {t}penable && !{name}_wait && !{name}_hold;" if kind != 'apb2' else ''}
{f"assign {t}pslverr = {fail.format(a=local)};" if kind != 'apb2' else ''}
initial {name}_hold = 0;
always @(posedge {clk}) begin
 {name}_rq <= reset_n;
 if (!reset_n && !{name}_rq && {t}psel) begin $display("ASSERT_FAIL:APB_INITIATOR.TARGET_PSEL_IN_RESET {name}"); $fatal(1); end
 if (!reset_n) {name}_wait <= 1;
 else if ({t}psel && !{t}penable) {name}_wait <= {local}[2] | {local}[3];
 else if ({name}_wait) {name}_wait <= 0;
 if (reset_n && {done})
  $display("EV T {name} %0d %0h %0h %0h %0d", {t}pwrite, {local}, {t}pwrite ? {strobe} : 4'h0,
           {t}pwrite ? {t}pwdata : 32'h0, {prot});
 if (reset_n && {t}psel && !{t}penable && {local}[1:0] != 2'b00) begin
  $display("ASSERT_FAIL:APB_INITIATOR.TARGET_WORD_ADDRESS {name}"); $fatal(1);
 end
end
''')
        elif kind == 'axi4' and m.get('target_outstanding', {}).get(name, 1) > 1:
            # Up to the outlet's capacity of reads and writes in flight (port
            # checks). Writes answer in arrival order; of two queued reads
            # with different target IDs the younger answers first (legal
            # AXI reordering), so a read steered by position alone reaches
            # the wrong source. The outlet keeps reads of one source and
            # AxID together, so with correct steering one read waits at a
            # time. Outputs are registers loaded at the falling edge
            # (bench_registered).
            k = m['target_outstanding'][name]
            idw = ports[t + 'awid']['width']
            sigs = ('awready', 'wready', 'bvalid', 'bresp', 'arready', 'rvalid', 'rresp', 'rdata',
                    'rlast', 'bid', 'rid')
            driven |= {t + s for s in sigs}
            text, ports_loaded = registered(f'''
reg [31:0] {name}_qa [0:2]; reg [{idw - 1}:0] {name}_qi [0:2]; integer {name}_qn, {name}_qmax = 0;
reg [31:0] {name}_wa [0:2]; reg [{idw - 1}:0] {name}_wi [0:2]; reg [2:0] {name}_wp [0:2];
reg [31:0] {name}_wd [0:2]; reg [3:0] {name}_ws [0:2]; integer {name}_an, {name}_wn, {name}_amax = 0, {name}_k;
integer {name}_tick = 0, {name}_rw, {name}_bw, {name}_sel = 0;
reg {name}_arhs, {name}_rhs, {name}_awhs, {name}_whs, {name}_bhs;
assign {t}arready = {name}_qn < 2 && {name}_tick % 3 != 2;
assign {t}rvalid = {name}_qn > 0 && {name}_rw == 0;
assign {t}rresp = {fail.format(a=name + '_qa[' + name + '_sel]')} ? 2'b10 : 2'b00;
assign {t}rdata = {{{tag}, 4'd0, {name}_qa[{name}_sel][19:0]}};
assign {t}rlast = 1'b1;
assign {t}rid = {name}_qi[{name}_sel];
assign {t}awready = {name}_an < 2 && {name}_tick % 3 != 1;
assign {t}wready = {name}_wn < {name}_an && {name}_tick % 4 != 2;
assign {t}bvalid = {name}_wn > 0 && {name}_bw == 0;
assign {t}bresp = {fail.format(a=name + '_wa[0]')} ? 2'b10 : 2'b00;
assign {t}bid = {name}_wi[0];
always @(posedge {clk}) begin
 {name}_tick <= {name}_tick + 1;
 // Handshakes as the port saw them at this edge, before any update.
 {name}_arhs = {t}arvalid && {t}arready; {name}_rhs = {t}rvalid && {t}rready;
 {name}_awhs = {t}awvalid && {t}awready; {name}_whs = {t}wvalid && {t}wready;
 {name}_bhs = {t}bvalid && {t}bready;
 if (!reset_n) begin
  {name}_qn = 0; {name}_an = 0; {name}_wn = 0; {name}_rw = 0; {name}_bw = 0; {name}_sel = 0;
 end
 else begin
  // Each response waits a few cycles, so a second request arrives meanwhile.
  if ({name}_qn > 0 && {name}_rw > 0 && !{name}_rhs) {name}_rw = {name}_rw - 1;
  if ({name}_wn > 0 && {name}_bw > 0 && !{name}_bhs) {name}_bw = {name}_bw - 1;
  if ({name}_rhs) begin
   {name}_rw = 5;
   for ({name}_k = {name}_sel; {name}_k < 2; {name}_k = {name}_k + 1) begin
    {name}_qa[{name}_k] = {name}_qa[{name}_k + 1]; {name}_qi[{name}_k] = {name}_qi[{name}_k + 1];
   end
   {name}_qn = {name}_qn - 1;
  end
  if ({name}_bhs) begin
   {name}_bw = 5;
   for ({name}_k = 0; {name}_k < 2; {name}_k = {name}_k + 1) begin
    {name}_wa[{name}_k] = {name}_wa[{name}_k + 1]; {name}_wi[{name}_k] = {name}_wi[{name}_k + 1];
    {name}_wp[{name}_k] = {name}_wp[{name}_k + 1]; {name}_wd[{name}_k] = {name}_wd[{name}_k + 1];
    {name}_ws[{name}_k] = {name}_ws[{name}_k + 1];
   end
   {name}_an = {name}_an - 1; {name}_wn = {name}_wn - 1;
  end
  if ({name}_whs) begin
   check({t}wlast === 1, "TARGET_WLAST");
   {name}_wd[{name}_wn] = {t}wdata; {name}_ws[{name}_wn] = {t}wstrb;
   $display("EV T {name} 1 %0h %0h %0h %0d", {name}_wa[{name}_wn], {t}wstrb, {t}wdata, {name}_wp[{name}_wn]);
   check(({t}wdata & ~strobe_mask({t}wstrb)) == 0, "UNSTROBED_BYTES_ZERO");
   if ({name}_wn == 0) {name}_bw = 5;
   {name}_wn = {name}_wn + 1;
  end
  if ({name}_awhs) begin
   check({name}_an < {k}, "TARGET_WRITE_CAPACITY");
   check({t}awlen === 0 && {t}awsize {size_comparison} && {t}awburst === 1, "TARGET_SINGLE_BEAT");
   {name}_wa[{name}_an] = {t}awaddr; {name}_wi[{name}_an] = {t}awid; {name}_wp[{name}_an] = {t}awprot;
   {name}_an = {name}_an + 1;
   if ({name}_an > {name}_amax) {name}_amax = {name}_an;
  end
  if ({name}_arhs) begin
   $display("EV T {name} 0 %0h 0 0 %0d", {t}araddr, {t}arprot);
   check({name}_qn < {k}, "TARGET_READ_CAPACITY");
   check({t}arlen === 0 && {t}arsize {size_comparison} && {t}arburst === 1, "TARGET_SINGLE_BEAT");
   if ({name}_qn == 0) {name}_rw = 5;
   {name}_qa[{name}_qn] = {t}araddr; {name}_qi[{name}_qn] = {t}arid; {name}_qn = {name}_qn + 1;
   if ({name}_qn > {name}_qmax) {name}_qmax = {name}_qn;
  end
  {name}_sel = {name}_qn > 1 && {name}_qi[1] != {name}_qi[0] ? 1 : 0;
 end
end
''', clk)
            models.append(text)
            REGISTERED.update(ports_loaded)
        elif kind in ('axi4', 'axi4_lite'):
            full = kind == 'axi4'
            aw = 32 if full else lb
            idw = ports[t + 'awid']['width'] if t + 'awid' in ports else 0
            sigs = ('awready', 'wready', 'bvalid', 'bresp', 'arready', 'rvalid', 'rresp', 'rdata')
            driven |= {t + s for s in sigs + (('rlast', 'bid', 'rid') if full else ())}
            models.append(f'''
reg {name}_aw, {name}_w, {name}_b, {name}_r; reg [{aw - 1}:0] {name}_a, {name}_ra; reg [31:0] {name}_d;
reg [3:0] {name}_s; reg [2:0] {name}_p; reg [7:0] {name}_id, {name}_rid; integer {name}_tick = 0;
assign {t}awready = !{name}_aw && !{name}_b && {name}_tick % 3 != 1;
assign {t}wready = !{name}_w && !{name}_b && {name}_tick % 4 != 2;
assign {t}bvalid = {name}_b; assign {t}bresp = {fail.format(a=name + '_a')} ? 2'b10 : 2'b00;
assign {t}arready = !{name}_r && {name}_tick % 3 != 2;
assign {t}rvalid = {name}_r; assign {t}rresp = {fail.format(a=name + '_ra')} ? 2'b10 : 2'b00;
assign {t}rdata = {{{tag}, 4'd0, {f"{name}_ra[19:0]" if full else f"{20 - lb}'d0, {name}_ra"}}};
{f"assign {t}rlast = 1'b1;" if full else ""}
{f"assign {t}bid = {name}_id; assign {t}rid = {name}_rid;" if idw else ""}
always @(posedge {clk}) begin
 {name}_tick <= {name}_tick + 1;
 if (!reset_n) begin {name}_aw <= 0; {name}_w <= 0; {name}_b <= 0; {name}_r <= 0; end
 else begin
  if ({t}awvalid && {t}awready) begin
   {name}_aw <= 1; {name}_a <= {t}awaddr; {name}_p <= {t}awprot;{f" {name}_id <= {t}awid;" if idw else ""}
   {f'check({t}awlen === 0 && {t}awsize {size_comparison} && {t}awburst === 1, "TARGET_SINGLE_BEAT");' if full else ""}
  end
  if ({t}wvalid && {t}wready) begin
   {name}_w <= 1; {name}_d <= {t}wdata; {name}_s <= {t}wstrb;
   {f'check({t}wlast === 1, "TARGET_WLAST");' if full else ""}
  end
  if ({name}_aw && {name}_w && !{name}_b) begin
   $display("EV T {name} 1 %0h %0h %0h %0d", {name}_a, {name}_s, {name}_d, {name}_p);
   check(({name}_d & ~strobe_mask({name}_s)) == 0, "UNSTROBED_BYTES_ZERO");
   {name}_b <= 1; {name}_aw <= 0; {name}_w <= 0;
  end
  if ({name}_b && {t}bready) {name}_b <= 0;
  if ({t}arvalid && {t}arready) begin
   $display("EV T {name} 0 %0h 0 0 %0d", {t}araddr, {t}arprot);
   {f'check({t}arlen === 0 && {t}arsize {size_comparison} && {t}arburst === 1, "TARGET_SINGLE_BEAT");' if full else ""}
   {name}_r <= 1; {name}_ra <= {t}araddr;{f" {name}_rid <= {t}arid;" if idw else ""}
  end
  if ({name}_r && {t}rready) {name}_r <= 0;
 end
end
''')
        else:  # ahb_v3: one wait state on odd words, strobes from HSIZE/HADDR, never an error
            driven |= {t + s for s in ('hreadyout', 'hresp', 'hrdata')}
            models.append(f'''
reg {name}_dp, {name}_dw, {name}_dwait; reg [{lb - 1}:0] {name}_da; reg [2:0] {name}_ds; reg [3:0] {name}_strobe;
reg [1:0] {name}_derr;
assign {t}hreadyout = !{name}_dp || (!{name}_dwait && {name}_derr != 1);
assign {t}hresp = {name}_dp && {name}_derr != 0;
assign {t}hrdata = {{{tag}, 4'd0, {20 - lb}'d0, {name}_da[{lb - 1}:2], 2'b00}};
always @* {name}_strobe = {name}_ds == 2 ? 4'hf : {name}_ds == 1 ? (4'h3 << {name}_da[1:0]) : (4'h1 << {name}_da[1:0]);
always @(posedge {clk}) begin
 if (!reset_n) begin {name}_dp <= 0; {name}_dwait <= 0; {name}_derr <= 0; end
 else begin
  if ({name}_dp && {name}_dwait) {name}_dwait <= 0;
  else if ({name}_dp && {name}_derr == 1) {name}_derr <= 2;
  else if ({name}_dp) begin
   {name}_derr <= 0;
   $display("EV H {name} %0d %0h %0h %0h 0", {name}_dw, {name}_da, {name}_dw ? {name}_strobe : 4'h0,
            {name}_dw ? {t}hwdata & strobe_mask({name}_strobe) : 32'h0);
   {name}_dp <= 0;
  end
  if ({t}hsel && {t}htrans[1] && {t}hready) begin
   {name}_dp <= 1; {name}_dw <= {t}hwrite; {name}_da <= {t}haddr; {name}_ds <= {t}hsize;
   {name}_dwait <= {t}haddr[2];
   {name}_derr <= (!{t}hwrite && {t}haddr[5:0] == 6'h{ERROR_OFFSET:02x}) ? 1 : 0;
  end
 end
end
''')
    if m.get('source_widths'):
        for name, (kind, *_rest) in m['targets'].items():
            if kind != 'axi4':
                continue
            t = f'nl_t_{name}_'
            for channel, write in (('aw', 1), ('ar', 0)):
                check = (f'check({t}{channel}len === 0 && {t}{channel}size {size_comparison} '
                         f'&& {t}{channel}burst === 1, "TARGET_SINGLE_BEAT");')
                observation = (f' $display("SIZE {name} {write} %0h %0d", '
                               f'{t}{channel}addr, {t}{channel}size);')
                models = [text.replace(check, check + observation) for text in models]
    return models, driven


def source_tasks(mode):
    m = MODES[mode]
    tasks = []
    for s, (source, (protocol, bits, domain)) in enumerate(m['sources'].items()):
        i = f'nl_i_{source}_'
        clk = f'clk_{domain}'
        if protocol in APB:
            v4 = protocol == 'apb4'
            tasks.append(f'''
reg {source}_rq;
reg {source}_err = 1'b0;
reg [31:0] {source}_rd = 32'h0;
always @(posedge {clk}) begin
 {source}_rq <= reset_n;
 {source}_err <= reset_n && ({i}pready ? {i}pslverr : {source}_err);
 {source}_rd <= !reset_n ? 32'h0 : {i}pready ? {i}prdata : {source}_rd;
 if (reset_n && {source}_rq && !{i}pready && {i}prdata !== {source}_rd) begin $display("ASSERT_FAIL:APB_INITIATOR.PRDATA_HOLD {source}"); $fatal(1); end
 if ({i}pready && {i}pwrite && {i}prdata !== 32'h0) begin $display("ASSERT_FAIL:APB_INITIATOR.WRITE_PRDATA_ZERO {source}"); $fatal(1); end
 if (reset_n && {source}_rq && !{i}pready && {i}pslverr !== {source}_err) begin $display("ASSERT_FAIL:APB_INITIATOR.PSLVERR_HOLD {source}"); $fatal(1); end
 if ({i}pready && !({i}psel && {i}penable)) begin $display("ASSERT_FAIL:APB_INITIATOR.PREADY_OUTSIDE_ACCESS {source}"); $fatal(1); end
 if (!reset_n && !{source}_rq && {i}pready) begin $display("ASSERT_FAIL:APB_INITIATOR.PREADY_IN_RESET {source}"); $fatal(1); end
 if ({i}pready && {i}pslverr && !{i}pwrite && {i}prdata !== 32'h0) begin $display("ASSERT_FAIL:APB_INITIATOR.PSLVERR_PRDATA_ZERO {source}"); $fatal(1); end
end
task automatic xfer_{source};
 input write; input [31:0] addr; input [3:0] strobe; input [31:0] data; input [2:0] prot; input integer gap;
 integer n;
 begin
  // abort: the common reset cancels the transfer; the master gives it up.
  if (!abort) begin
  @(negedge {clk});
  {i}paddr = addr; {i}pwrite = write; {i}pwdata = data;{f" {i}pstrb = write ? strobe : 4'h0; {i}pprot = prot;" if v4 else ""}
  {i}psel = 1; {i}penable = 0;
  @(negedge {clk}); {i}penable = 1;
  n = 0;
  @(posedge {clk});
  while (!{i}pready && !abort) begin
   n = n + 1;
   if (n > 3000) begin $display("ASSERT_FAIL:APB_INITIATOR.RESPONSE_BOUND {source}"); $fatal(1); end
   @(posedge {clk});
  end
  if (!abort) $display("EV S {source} %0d %0h", {i}pslverr, write ? 32'h0 : {i}prdata);
  end
  if (gap && !abort) begin
   @(negedge {clk}); {i}psel = 0; {i}penable = 0; {i}paddr = ~addr; {i}pwdata = ~data;
   repeat (gap - 1) @(negedge {clk});
  end
 end
endtask
task automatic idle_{source};
 begin @(negedge {clk}); {i}psel = 0; {i}penable = 0; end
endtask
''')
        else:
            full = protocol == 'axi4'
            extra_aw = f" {i}awlen = 0; {i}awsize = 2; {i}awburst = 1; {i}awid = {s % 4};" if full else ''
            extra_ar = f" {i}arlen = 0; {i}arsize = 2; {i}arburst = 1; {i}arid = {s % 4};" if full else ''
            tasks.append(f'''
always @(negedge {clk}) begin {i}rready = cycle % 3 != 0; {i}bready = cycle % 4 != 1; end
task automatic xfer_{source};
 input write; input [31:0] addr; input [3:0] strobe; input [31:0] data; input [2:0] prot; input integer gap;
 integer got;
 begin
  got = 0;
  if (!abort) @(negedge {clk});
  if (abort) begin end
  else if (write) begin
   {i}awaddr = addr; {i}awprot = prot; {i}wdata = data; {i}wstrb = strobe;{extra_aw}{f" {i}wlast = 1;" if full else ""}
   fork
    begin {i}awvalid = 1; @(posedge {clk}); while (!{i}awready && !abort) @(posedge {clk}); @(negedge {clk}); {i}awvalid = 0; end
    begin {i}wvalid = 1; @(posedge {clk}); while (!{i}wready && !abort) @(posedge {clk}); @(negedge {clk}); {i}wvalid = 0; end
   join
   while (!got && !abort) begin
    @(posedge {clk});
    if ({i}bvalid && {i}bready && !abort) begin $display("EV S {source} %0d 0", {i}bresp); got = 1; end
   end
  end else begin
   {i}araddr = addr; {i}arprot = prot;{extra_ar} {i}arvalid = 1;
   @(posedge {clk}); while (!{i}arready && !abort) @(posedge {clk});
   @(negedge {clk}); {i}arvalid = 0;
   while (!got && !abort) begin
    @(posedge {clk});
    if ({i}rvalid && {i}rready && !abort) begin $display("EV S {source} %0d %0h", {i}rresp, {i}rresp == 0 ? {i}rdata : 32'h0); got = 1; end
   end
  end
 end
endtask
task automatic idle_{source};
 begin end
endtask
''')
    return tasks


def call(t):
    return (f"xfer_{t['source']}({t['write']}, 32'h{t['address']:x}, 4'h{t['strobe']:x}, "
            f"32'h{t['data']:x}, 3'd{t['prot']}, {t['gap']});")


RESET_POINTS = ('access_wait', 'setup', 'access_start', 'queued')


def reset_transfers(mode, point):
    """New transfers from every source after one reset point."""
    m = MODES[mode]
    tp = m['targets']['tp']
    out = []
    for s, (source, (protocol, _, _)) in enumerate(m['sources'].items()):
        if (source, 'tp') in m.get('no_route', ()):
            continue
        for write in (1, 0):
            out.append({'name': f'reset-{point}-{source}-{write}', 'source': source, 'write': write,
                        'address': tp[1] + 0x600 + 4 * s, 'strobe': 0xb if write else 0, 'prot': 5,
                        'data': 0x5a5a0000 | s | RESET_POINTS.index(point) << 8, 'gap': 1})
    return out


def reset_streams(mode):
    """Transfers in flight from every source when the common reset hits
    (no AHB target: a cancelled word may be split into partial transfers)."""
    m = MODES[mode]
    out = {}
    for source, seq in streams(mode).items():
        kept = [t for t in seq if m['targets'][owner_name(mode, t['address'])][0] != 'ahb_v3']
        out[source] = [dict(t, data=t['data'] ^ 0x00ff00ff) for t in kept[:12]]
    return out


def owner_name(mode, address):
    for name, (_, base, size, _, _) in MODES[mode]['targets'].items():
        if base <= address & ~3 < base + max(size, 0x1000):
            return name
    raise AssertionError(hex(address))


def multi_check(mode):
    """Multi-transaction targets: the concurrent phase filled the write side.
    Every initiator here keeps one transfer; reads of different sources
    overlap at the outlet, up to its capacity."""
    lines = []
    for name, k in MODES[mode].get('target_outstanding', {}).items():
        lines.append(f' $display("TARGET_IN_FLIGHT {name} r=%0d w=%0d", {name}_qmax, {name}_amax);')
        lines.append(f' check({name}_qmax >= 1 && {name}_qmax <= {k} && {name}_amax == {k}, "TARGET_FILLED");')
    return '\n'.join(lines)


def bench(mode, mapping, selected, concurrent, after_reset, queued):
    m = MODES[mode]
    ports = {p['name']: p for p in mapping['ports']}
    models, driven = target_models(mode, ports, True)
    decl, conns, init = [], [], []
    clocks = sorted({p['name'] for p in mapping['ports'] if p.get('signal') == 'clock'})
    for name, p in ports.items():
        width = p['width']
        # The tree profile names its reset per domain.
        wire = 'reset_n' if p.get('signal') == 'reset_n' else name
        if p['direction'] == 'input' and (name not in driven or name in REGISTERED):
            decl.append(f'reg [{width - 1}:0] {wire};')
            if wire != 'reset_n' and p.get('signal') != 'clock' and name not in REGISTERED:
                init.append(f' {wire} = 0;')
        else:
            decl.append(f'wire [{width - 1}:0] {wire};')
        conns.append(f'.{name}({wire})')
    periods = {d: v[1] for d, v in m.get('domains', {'main': (0, 10)}).items()}
    clock_gen = ''.join(f'initial {c} = 0;\nalways #{periods[c[len("clk_"):]] / 2} {c} = ~{c};\n'
                        for c in clocks)
    main = 'clk_main'
    directed_runs = []
    for t in selected:
        settle = f'repeat (4) @(posedge {main}); ' if t['gap'] else ''
        directed_runs.append(f' $display("CASE_BEGIN {t["name"]}"); {call(t)}\n'
                             f' {settle}$display("CASE_END {t["name"]}");')
    blocks = []
    for source, seq in concurrent.items():
        body = '\n'.join('   ' + call(t) for t in seq)
        blocks.append(f'  begin\n{body}\n   idle_{source};\n  end')
    tp = m['targets']['tp']
    a0 = 'nl_i_a0_'
    a0_clk = f"clk_{m['sources']['a0'][2]}"
    def after(point):
        return '\n'.join(f' $display("CASE_BEGIN {t["name"]}"); {call(t)}\n'
                         f' repeat (4) @(posedge {main}); $display("CASE_END {t["name"]}");'
                         for t in after_reset if t['name'].startswith(f'reset-{point}-'))
    queued_blocks = []
    for source, seq in queued.items():
        body = '\n'.join('    ' + call(t) for t in seq)
        queued_blocks.append(f'   begin\n{body}\n   end')
    # Every initiator handshake input low, as a master does under reset.
    quiet = ' '.join(f'{p["name"]} = 0;' for p in mapping['ports']
                     if p['endpoint'].startswith('initiator:') and p['direction'] == 'input'
                     and p['signal'] in ('psel', 'penable', 'awvalid', 'wvalid', 'arvalid'))
    pulse = (f' repeat (6) @(posedge {main});\n @(negedge {main}); {quiet} tp_hold = 0;\n'
             f' repeat (2) @(posedge {main});\n @(negedge {main}); reset_n = 1; abort = 0;')
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
integer cycle = 0;
reg abort = 0;
{clock_gen}
always @(posedge {main}) begin
 cycle <= cycle + 1;
 if (cycle > 400000) begin $display("ASSERT_FAIL:APB_INITIATOR.TIMEOUT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*32-1:0] label_;
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:APB_INITIATOR.%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
function [31:0] strobe_mask;
 input [3:0] strobe;
 integer k;
 begin
  strobe_mask = 0;
  for (k = 0; k < 4; k = k + 1) if (strobe[k]) strobe_mask[8 * k +: 8] = 8'hff;
 end
endfunction
{''.join(models)}
{''.join(source_tasks(mode))}
initial begin
 reset_n = 0;
{chr(10).join(init)}
 repeat (10) @(posedge {main});
 @(negedge {main}); reset_n = 1;
 $display("PHASE directed");
{chr(10).join(directed_runs)}
 $display("PHASE concurrent");
 fork
{chr(10).join(blocks)}
 join
 repeat (20) @(posedge {main});
{multi_check(mode)}
 $display("PHASE reset");
 // a0 writes to tp, which holds PREADY low; the common reset cancels it.
 tp_hold = 1;
 @(negedge {a0_clk});
 {a0}paddr = 32'h{tp[1] + 0x5f0:x}; {a0}pwrite = 1; {a0}pwdata = 32'hdeadbeef; {a0}pstrb = 4'hf; {a0}pprot = 0;
 {a0}psel = 1; {a0}penable = 0;
 @(negedge {a0_clk}); {a0}penable = 1;
 begin : stalled
  integer n; n = 0;
  while (!(nl_t_tp_psel && nl_t_tp_penable) && n < 3000) begin @(posedge {main}); n = n + 1; end
  check(nl_t_tp_psel && nl_t_tp_penable, "RESET_TRANSFER_REACHED_ACCESS");
 end
 repeat (5) @(posedge {main});
 if ({int(mode == 'timeout')}) begin : timed
  integer n; n = 0;
  check({a0}pready === 1'b0, "STALLED_TRANSFER_WAITS_BEFORE_TIMEOUT");
  @(posedge {a0_clk}); while (!{a0}pready && n < 1200) begin @(posedge {a0_clk}); n = n + 1; end
  check({a0}pready === 1'b1 && {a0}pslverr === 1'b1, "TIMEOUT_PSLVERR");
  $display("TIMEOUT_WRITE_DONE %0d", n);
  @(negedge {a0_clk}); {a0}psel = 0; {a0}penable = 0;
  @(negedge {a0_clk}); {a0}pwrite = 0; {a0}paddr = 32'h{tp[1] + 0x10:x}; {a0}psel = 1;
  @(negedge {a0_clk}); {a0}penable = 1;
  n = 0; @(posedge {a0_clk}); while (!{a0}pready && n < 200) begin @(posedge {a0_clk}); n = n + 1; end
  check({a0}pready === 1'b1 && {a0}pslverr === 1'b1 && {a0}prdata === 32'h0, "FAILED_TARGET_READ_PSLVERR_ZERO");
  $display("TIMEOUT_READ_DONE %0d %0h", n, {a0}prdata);
 end
 else check({a0}pready === 1'b0, "STALLED_TRANSFER_WAITS");
 // Reset point access_wait: a0 waits in ACCESS for PREADY.
 @(negedge {main}); reset_n = 0;
{pulse}
{after('access_wait')}
 // Reset point setup: a0 is in SETUP when the reset arrives.
 @(negedge {a0_clk});
 {a0}paddr = 32'h{tp[1] + 0x5f4:x}; {a0}pwrite = 1; {a0}pwdata = 32'h0badf00d; {a0}pstrb = 4'hf; {a0}pprot = 0;
 {a0}psel = 1; {a0}penable = 0; reset_n = 0;
{pulse}
{after('setup')}
 // Reset point access_start: the transfer left a0 at SETUP and travels.
 @(negedge {a0_clk});
 {a0}paddr = 32'h{tp[1] + 0x5f8:x}; {a0}pwrite = 0; {a0}psel = 1; {a0}penable = 0;
 @(negedge {a0_clk}); {a0}penable = 1; reset_n = 0;
{pulse}
{after('access_start')}
 // Reset point queued: every source streams; transfers wait in the network.
 $display("PHASE queued");
 fork
  begin
   fork
{chr(10).join(queued_blocks)}
   join
  end
  begin repeat (40) @(posedge {main}); @(negedge {main}); abort = 1; reset_n = 0; end
 join
 $display("PHASE reset");
{pulse}
{after('queued')}
 $display("NOCLOOM_PASS apb_initiator {mode}");
 $finish;
end
endmodule
'''


def parse(log):
    cases, phase, current = {}, None, None
    streams_seen = {'accesses': [], 'results': {}, 'queued_accesses': [], 'queued_results': {},
                    'stray': [], 'sizes': []}
    for line in log.splitlines():
        part = line.split()
        if line.startswith('PHASE '):
            phase = part[1]
        elif line.startswith('CASE_BEGIN '):
            current = part[1]
            cases[current] = {'accesses': [], 'ahb': [], 'results': [], 'sizes': []}
        elif line.startswith('CASE_END '):
            current = None
        elif line.startswith('SIZE '):
            event = (part[1], int(part[2]), int(part[3], 16), int(part[4]))
            if phase == 'concurrent':
                streams_seen['sizes'].append(event)
            elif current is not None:
                cases[current]['sizes'].append(event)
        elif line.startswith('EV '):
            if part[1] in ('T', 'H'):
                event = (part[2], int(part[3]), int(part[4], 16), int(part[5], 16), int(part[6], 16), int(part[7]))
            else:
                event = ('S', part[2], int(part[3]), int(part[4], 16))
            if phase == 'concurrent':
                if part[1] == 'S':
                    streams_seen['results'].setdefault(part[2], []).append(event)
                else:
                    streams_seen['accesses'].append((part[1], event))
            elif current is not None:
                key = {'T': 'accesses', 'H': 'ahb', 'S': 'results'}[part[1]]
                cases[current][key].append(event)
            elif phase == 'queued':
                if part[1] == 'S':
                    streams_seen['queued_results'].setdefault(part[2], []).append(event)
                else:
                    streams_seen['queued_accesses'].append(event)
            elif phase == 'reset':
                streams_seen['stray'].append(line)
            else:
                raise AssertionError('event outside a case: ' + line)
    return cases, streams_seen


def merge_ahb(ahb):
    # AHB splits a word by its strobes into byte and halfword transfers: fold
    # each word's transfers into one access with the union of lanes. A read
    # word is one transfer.
    words = {}
    for name, write, address, strobe, data, prot in ahb:
        key = (name, write, address & ~3)
        s, d = words.get(key, (0, 0))
        assert not s & strobe, 'AHB lane written twice: ' + repr(ahb)
        words[key] = (s | strobe, d | data)
    return [(name, write, address, s, d, 0) for (name, write, address), (s, d) in words.items()]


def compare(mode, log, selected, concurrent, after_reset, queued=None):
    cases, seen = parse(log)
    mismatches = []
    def expected_sizes(t):
        width = MODES[mode].get('source_widths', {}).get(t['source'], 32)
        return [(name, write, address, (width // 8).bit_length() - 1)
                for name, write, address, *_rest in expected(mode, t)[0]
                if MODES[mode]['targets'][name][0] == 'axi4']
    for t in selected + after_reset:
        want = expected(mode, t)
        got = cases.get(t['name'])
        if got is None:
            mismatches.append({'case': t['name'], 'observed': None})
            continue
        if MODES[mode].get('source_widths') and got['sizes'] != expected_sizes(t):
            mismatches.append({'case': t['name'], 'size_expected': expected_sizes(t),
                               'size_observed': got['sizes']})
        observed = (got['accesses'] + merge_ahb(got['ahb']), got['results'])
        if observed != (want[0], [want[1]]):
            mismatches.append({'case': t['name'], 'transfer': t, 'expected': repr(want), 'observed': repr(observed)})
    accesses = Counter(e for k, e in seen['accesses'] if k == 'T')
    accesses.update(merge_ahb([e for k, e in seen['accesses'] if k == 'H']))
    want_accesses = Counter()
    for source, seq in concurrent.items():
        results = [expected(mode, t)[1] for t in seq]
        for t in seq:
            want_accesses.update(expected(mode, t)[0])
        if seen['results'].get(source) != results:
            mismatches.append({'stream': source, 'expected': repr(results),
                               'observed': repr(seen['results'].get(source))})
    if MODES[mode].get('source_widths'):
        sizes = Counter(x for seq in concurrent.values() for t in seq for x in expected_sizes(t))
        if Counter(seen['sizes']) != sizes:
            mismatches.append({'stream_sizes_expected': repr(sizes),
                               'stream_sizes_observed': repr(Counter(seen['sizes']))})
    # A cancelled transfer leaves no trace outside the queued window; there
    # each source's results are a prefix of its stream and every access
    # belongs to one of the streamed transfers, at most once.
    if seen['stray']:
        mismatches.append({'reset_stray_events': seen['stray'][:5]})
    if queued is not None:
        allowed = Counter()
        for source, seq in queued.items():
            results = [expected(mode, t)[1] for t in seq]
            got = seen['queued_results'].get(source, [])
            if got != results[:len(got)]:
                mismatches.append({'queued_source': source, 'expected_prefix': repr(results),
                                   'observed': repr(got)})
            for t in seq:
                allowed.update(expected(mode, t)[0])
        extra = Counter(seen['queued_accesses']) - allowed
        if extra:
            mismatches.append({'queued_accesses_not_streamed': repr(extra)})
    if accesses != want_accesses:
        mismatches.append({'stream_accesses': 'differ', 'missing': repr(want_accesses - accesses),
                           'extra': repr(accesses - want_accesses)})
    return mismatches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', choices=sorted(MODES) + ['apb5', 'apb5_clocked', 'apb5_data', 'apb5_data_clocked'], required=True)
    args = parser.parse_args()
    if args.mode.startswith('apb5_data'):
        from apb5_user_bench import run_data_user
        run_data_user(args.cli, args.work, args.mode.endswith('clocked'))
        return
    if args.mode.startswith('apb5'):
        from apb5_user_bench import run
        run(args.cli, args.work, args.mode == 'apb5_clocked')
        return
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
    assert outputs[0] == outputs[1] and len(outputs[0]) == 6, outputs
    stem = 'nocloom_apb_initiator_' + args.mode
    mapping = json.loads((work / f'first/{stem}.map.json').read_text())
    definition = mapping['definition']
    m = MODES[args.mode]
    for source, (protocol, bits, _) in m['sources'].items():
        assert definition['endpoint_protocols']['initiators'][source] == protocol, definition['endpoint_protocols']
    assert definition['contract']['apb_ingress']['error'] == 'PSLVERR_for_every_failure_including_unmapped_PRDATA_zero'
    ports = {p['name']: p for p in mapping['ports']}
    for source, (protocol, bits, domain) in m['sources'].items():
        if protocol in APB:
            names = {n[len(f'nl_i_{source}_'):] for n in ports if n.startswith(f'nl_i_{source}_')}
            want = {'psel', 'penable', 'pwrite', 'paddr', 'pwdata', 'pready', 'prdata', 'pslverr'}
            if protocol == 'apb4':
                want |= {'pstrb', 'pprot'}
            assert names == want, (source, names)
            assert ports[f'nl_i_{source}_paddr']['width'] == (bits or 32)
            width = m.get('source_widths', {}).get(source, 32)
            assert ports[f'nl_i_{source}_pwdata']['width'] == width
            assert ports[f'nl_i_{source}_prdata']['width'] == width
            if protocol == 'apb4':
                assert ports[f'nl_i_{source}_pstrb']['width'] == width // 8
            if m.get('source_widths'):
                assert definition['endpoint_data_widths']['initiators'][source] == width
                assert definition['transport']['data_width'] == 32
            assert ports[f'nl_i_{source}_psel'].get('clock', 'clk_' + domain) == 'clk_' + domain, ports[f'nl_i_{source}_psel']
    selected = directed(args.mode)
    concurrent = streams(args.mode)
    after_reset = [t for point in RESET_POINTS for t in reset_transfers(args.mode, point)]
    queued = reset_streams(args.mode)
    case = work / 'simulation'
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(work / f'first/{stem}.v'))
    (case / 'tb.sv').write_text(bench(args.mode, mapping, selected, concurrent, after_reset, queued))
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    mismatches = compare(args.mode, log, selected, concurrent, after_reset, queued)
    (case / 'mismatches.json').write_text(json.dumps(mismatches, indent=1) + '\n')
    passed = (rc == 0 and f'NOCLOOM_PASS apb_initiator {args.mode}' in log and 'ASSERT_FAIL' not in log
              and not mismatches)
    transfers = len(selected) + len(after_reset) + sum(map(len, concurrent.values()))
    (work / 'results.json').write_text(json.dumps(
        {'mode': args.mode, 'directed': len(selected), 'concurrent': sum(map(len, concurrent.values())),
         'after_reset': len(after_reset), 'simulation_exit': rc, 'mismatches': len(mismatches),
         'status': 'PASS' if passed else 'FAIL',
         'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
         'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}, indent=1) + '\n')
    assert passed, (rc, mismatches[:3], log[-2500:])
    print(f'NOCLOOM_PASS apb initiator mode={args.mode} transfers={transfers}')


if __name__ == '__main__':
    main()
