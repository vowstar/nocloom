"""Sub-page APB, AHB and AXI4-Lite windows under a 40-bit system address:
Configuration, case table and an independent per-beat model.

The oracle decodes every beat independently from the declared windows, so it
does not reuse the generator's routing or outlet arithmetic. Used by
cases/subpage.py: the target accesses and the response codes of every case."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

APB256 = 0xA0F0400000   # apb4, 256 B, 8-bit PADDR
APB1K = 0xA0F0410000    # apb4, 1 KiB, 10-bit PADDR
AHB1K = 0xA0F0500000    # ahb_v3, 1 KiB, 10-bit HADDR
AXI4K = 0x1000000000    # read-only axi4 target: no AW/W/B ports, writes answer SLVERR
APB8 = 0xA0F0420000     # apb4, 8 B, 3-bit PADDR: a WRAP container and a wide beat exceed it
APB3 = 0xA0F0430000     # apb3, 256 B, 8-bit PADDR, no PSTRB/PPROT
APB2 = 0xA0F0440000     # apb2, 1 KiB, 10-bit PADDR, no PREADY/PSLVERR either
LITE = 0xA0F0460000     # axi4_lite, 64 KiB, 16-bit address
WIDE = 0x2000000000     # widths mode only: an idle 64-bit axi4 target

TARGETS = {
    't0': {'base': AXI4K, 'size': 0x1000, 'kind': 'axi4', 'bits': None, 'read_only': True},
    't1': {'base': APB256, 'size': 0x100, 'kind': 'apb4', 'bits': 8},
    't2': {'base': AHB1K, 'size': 0x400, 'kind': 'ahb_v3', 'bits': 10},
    't3': {'base': APB1K, 'size': 0x400, 'kind': 'apb4', 'bits': 10},
    't4': {'base': APB8, 'size': 0x8, 'kind': 'apb4', 'bits': 3},
    't5': {'base': APB3, 'size': 0x100, 'kind': 'apb3', 'bits': 8},
    't6': {'base': APB2, 'size': 0x400, 'kind': 'apb2', 'bits': 10},
    't7': {'base': LITE, 'size': 0x10000, 'kind': 'axi4_lite', 'bits': 16},
}
SOURCES = {'s0': 32, 's1': 128, 's2': 32}
# Source ports beside the 8-bit length of the others; s2 has no write channels.
SOURCE_PORTS = {'s0': {'id_width': 3}, 's1': {'id_width': 3, 'len_width': 4},
                's2': {'id_width': 3, 'access': 'read_only'}}
# Selective routing withholds the AHB target from the wide source; every mode
# withholds the 8-byte window, smaller than the wide source's 16-byte packet
# alignment word.
DENIED = {('s1', 't2')}
PACKET_DENIED = {('s1', 't4')}
# Target attribute maps of the attributes mode (bit 0 first).
ATTRIBUTES = {
    't1': {'prot': ['allocate', '~privileged', 'instruction']},
    't3': 'amba',
    't2': {'hprot': ['~privileged', 'instruction', 'allocate', 'lookup']},
    't7': {'prot': ['nonsecure', '~instruction', 'allocate']},
}
# FIXED modes run a base mode with these dedicated targets serving FIXED
# bursts as single transfers; t3 spells the default reject explicitly and t4
# omits it.
FIXED_MODES = {'fixed': 'graph', 'fixed_widths': 'widths', 'fixed_packets': 'packets'}
FIXED_SINGLE = {'t1', 't2', 't5', 't6', 't7'}
# In FIXED modes this local word of every FIXED_SINGLE target reads like a
# FIFO: each read access within a case returns a new value, so every FIXED
# read beat must carry its own access's data.
FIFO_LOCAL = 0xc8
AMBA = {'prot': ['privileged', 'nonsecure', 'instruction'],
        'hprot': ['instruction', 'privileged', 'bufferable', 'modifiable']}


# Shaped mode: the widths base with the AHB target sending complete write
# bursts and no undefined-length INCR.
SHAPED_MODES = {'ahb_shaped': 'widths'}
# The widths base with the idle AXI4 target at 1024 bits: the transport
# occupancy widens to eight bits, and every dedicated outlet takes the
# truncated endpoint count.
WIDE_TARGET_MODES = {'widths1024': 'widths'}
AHB_SHAPING = {'write_bursts': 'complete', 'undefined_length_bursts': False}
# The shaped mode also gives the AHB target the 2-bit AMBA 2 HRESP; the other
# modes keep the default 1-bit HRESP (the FIXED modes answer ERROR on it).
# Width 2 here covers only the port width and the map: the target model drives
# a 1-bit error, zero-extended, so it never answers RETRY or SPLIT. That
# coverage lives in verif_ahb_outlet_*_negative, verif_ahb_overlap_negative
# and ahb_public_installed.
AHB_HRESP = {'hresp_width': 2}


# Mappings mode: the graph base with several mappings per target and local
# addresses. t1 has two relocated sub-page mappings in its page (a burst from
# the first through the unmapped middle reaches the second); t6 adds a mapping
# aliasing its upper half into the page of t5, which the two targets share
# (each mapping routes itself, the rest of the page is a decode miss sent to
# the default target); t2 adds a mapping. t0, writable here, adds a page and
# three relocated sub-pages: two in a page of its own (packets cut at every
# mapping edge, so each decodes once at the AXI4 outlet, the rest of the page
# a decode miss) and one sharing the page of t3. Overlapping mappings, the
# smaller decoding: t3 maps 256 B inside the first page of t0 and t5 256 B
# inside t7, so t0 and t7 cut their packets at those mappings' alignment.
MAP_MODES = {'mappings': 'graph', 'tiles': 'graph', 'crossing': 'graph'}
MAPPINGS = {
    't1': [(APB256, 0x100, 0x300), (APB256 + 0x200, 0x100, 0x100)],
    't6': [(APB2, 0x400, 0), (APB3 + 0x200, 0x200, 0x200)],
    't2': [(AHB1K, 0x400, 0), (AHB1K + 0x800, 0x200, 0x200)],
    't3': [(APB1K, 0x400, 0), (AXI4K + 0xc00, 0x100, 0x100)],
    't5': [(APB3, 0x100, 0), (LITE + 0x1100, 0x100, 0)],
    't0': [(AXI4K, 0x1000, 0), (AXI4K + 0x2000, 0x1000, 0x5000),
           (AXI4K + 0x4100, 0x100, 0x6100), (AXI4K + 0x4400, 0x40, 0x6040),
           (APB1K + 0x800, 0x100, 0x6300)],
}
MAP_BITS = {'t1': 10}
# Tiles mode: the graph base with t5 and a read-only 64-bit t7 sharing the
# APB3 page in 64-byte mappings (t5 up to 0x140, t7 from there) and no AXI4
# sub-page, so only the shared page makes the wide source cut a WRAP container
# of up to 16 words of t7 crossing a mapping edge. s2 does not reach t7: its
# mapping is a decode miss of s2 in the shared page.
TILES = {'t5': [(APB3, 0x100, 0), (APB3 + 0x100, 0x40, 0x40)],
         't7': [(LITE, 0x10000, 0), (APB3 + 0x140, 0x40, 0x140)]}
# Crossing mode: the graph base with four bursts in flight per source and the
# page of t1 shared with t3 (local 0x100) and t7 (local 0x180) in mappings of
# 0x100 and 0x80 bytes, the rest a decode miss. Bursts cross from target to
# target; a slow t3 between t1 and t7 makes a packet that left before the
# earlier ones of its burst were answered, or a burst issued under its ID
# while another target still answers it, return its data out of order.
CROSSING = {'t3': [(APB1K, 0x400, 0), (APB256 + 0x100, 0x80, 0x100)],
            't7': [(LITE, 0x10000, 0), (APB256 + 0x180, 0x80, 0x180)]}
MODE_MAPPINGS = {'mappings': MAPPINGS, 'tiles': TILES, 'crossing': CROSSING}


def base_mode(mode):
    return FIXED_MODES.get(mode, SHAPED_MODES.get(mode, WIDE_TARGET_MODES.get(
        mode, MAP_MODES.get(mode, mode))))


def mappings(mode, name):
    """(global base, size, local address) of every mapping of a target."""
    t = TARGETS[name]
    return MODE_MAPPINGS.get(mode, {}).get(name, [(t['base'], t['size'], 0)])


def holding(mode, address, names=TARGETS):
    """(size, base, local, target) of the smallest mapping holding an address, else None."""
    return min(((s, b, at, n) for n in names for b, s, at in mappings(mode, n) if b <= address < b + s),
               default=None)


def local(mode, name, address):
    """The local address of a global one at a target, None outside its mappings."""
    held = holding(mode, address, [name])
    return None if held is None else address - held[1] + held[2]


def bits(mode, name):
    return MAP_BITS.get(name, TARGETS[name]['bits']) if mode == 'mappings' else TARGETS[name]['bits']


def fixed_single(mode, target):
    return mode in FIXED_MODES and target in FIXED_SINGLE


# The uniform mode also makes the AHB and APB2 targets read-only:
# every write to them answers SLVERR without access, whatever its strobes.
READ_ONLY_MODES = {'uniform': {'t2', 't6'}, 'tiles': {'t7'}}


def read_only(mode, target):
    if mode == 'mappings' and target in MAPPINGS:
        return False
    return TARGETS[target].get('read_only') or target in READ_ONLY_MODES.get(mode, set())


def denied(mode):
    extra = {('s2', 't7')} if mode == 'tiles' else set()
    mode = base_mode(mode)
    return (DENIED | PACKET_DENIED if mode == 'selective' else PACKET_DENIED) | extra


def configuration(mode):
    t8_bits = 1024 if mode in WIDE_TARGET_MODES else 64
    named = mode
    fixed, shaped, mode = mode in FIXED_MODES, mode in SHAPED_MODES, base_mode(mode)
    clocks = [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}]
    targets = []
    for name, t in TARGETS.items():
        entry = {'name': name, 'clock_domain': 'main', 'protocol': t['kind'], 'data_width': 32,
                 'mappings': [dict({'base': hex(b), 'size': hex(s)},
                                   **({'local_address': hex(at)} if at else {}))
                              for b, s, at in mappings(named, name)]}
        if t['kind'] != 'axi4':
            entry['address_width'] = bits(named, name)
        if read_only(named, name):
            entry['access'] = 'read_only'
        if named == 'tiles' and name == 't7':
            entry['data_width'] = 64
        if fixed and name in FIXED_SINGLE:
            entry['fixed_bursts'] = 'single'
        elif fixed and name == 't3':
            entry['fixed_bursts'] = 'reject'
        if shaped and t['kind'] == 'ahb_v3':
            entry.update(AHB_SHAPING)
            entry.update(AHB_HRESP)
        targets.append(entry)
    names = list(TARGETS)
    request = {'nodes': [{'name': 'q0', 'transport_word_bytes': 16}, {'name': 'q1'}],
               'links': [{'from': 'q0', 'to': 'q1', 'transport_word_bytes': 4}],
               'entries': [{'endpoint': s, 'node': 'q0'} for s in SOURCES],
               'exits': [{'endpoint': t, 'node': 'q1'} for t in names]}
    response = {'nodes': [{'name': 'p0'}, {'name': 'p1', 'transport_word_bytes': 16}],
                'links': [{'from': 'p0', 'to': 'p1', 'transport_word_bytes': 4}],
                'entries': [{'endpoint': t, 'node': 'p0'} for t in names],
                'exits': [{'endpoint': s, 'node': 'p1'} for s in SOURCES]}
    topology = {'request': request, 'response': response}
    if mode == 'clocked':
        clocks = [{'name': name, 'clock_port': 'clk_' + name, 'reset_port': 'reset_n', 'test_port': 'test'}
                  for name in ('main', 'middle', 'peripheral')]
        for t in targets:
            t['clock_domain'] = 'peripheral'
        for node, owner in zip(request['nodes'], ['main', 'middle'], strict=True):
            node['clock_domain'] = owner
        for node, owner in zip(response['nodes'], ['middle', 'main'], strict=True):
            node['clock_domain'] = owner
    if mode in ('widths', 'widths_uniform'):
        # A 64-bit AXI4 target sets per-target widths: the AXI4 targets merge
        # narrow bursts (the 128-bit source reaches the 32-bit targets through
        # the width clamp in every mode). No case addresses it.
        targets.append({'name': 't8', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': t8_bits,
                        'mappings': [{'base': hex(WIDE), 'size': '0x1000'}]})
        request['exits'].append({'endpoint': 't8', 'node': 'q1'})
        response['entries'].append({'endpoint': 't8', 'node': 'p0'})
    connectivity = {s: [t['name'] for t in targets if (s, t['name']) not in denied(named)]
                    for s in SOURCES}
    transport = {'max_outstanding': 4 if named == 'crossing' else 1, 'transport_word_bytes': 4}
    if mode == 'attributes':
        for t in targets:
            if t['name'] in ATTRIBUTES:
                t['attributes'] = ATTRIBUTES[t['name']]
    if mode in ('uniform', 'widths_uniform'):
        transport['response_codes'] = 'uniform'
    return endpoint_ports({'schema_version': 10, 'name': 'subpage', 'address_width': 40,
            'arbitration': {'policy': 'round_robin'},
            'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': clocks,
            **({'synchronizer_stages': 3} if mode == 'clocked' else {}),
            'initiators': [{'name': s, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                            **SOURCE_PORTS[s],
                            **({'narrow_bursts': 'split_non_modifiable'}
                               if mode == 'packets' and s == 's1' else {})}
                           for s, w in SOURCES.items()],
            'targets': targets,
            'transport': transport,
            'connectivity': connectivity,
            'topology': topology,
            'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, len_width=8)


def case(name, source, write, address, beats, size, errors=(), partner=None, burst=1, keep=None,
         keeps=None, batch=()):
    # keep: a byte-strobe mask ANDed into every write beat on the source bus;
    # keeps: an optional per-beat mask ANDed in as well.
    width = SOURCES[source] // 8
    c = {'name': name, 'source': source, 'write': write, 'address': address, 'beats': beats,
         'size': size, 'burst': burst, 'errors': [list(e) for e in errors],
         'keep': (1 << width) - 1 if keep is None else keep}
    if keeps is not None:
        assert len(keeps) == beats
        c['keeps'] = list(keeps)
    if partner:
        # A concurrent burst from the other source; target order may interleave.
        c['partner'] = dict(partner, errors=c['errors'])
    if batch:
        # Bursts issued right after it, each with its own ID, before the drain.
        c['batch'] = [dict(b, errors=c['errors'], id=ident) for b, ident in batch]
    return c


def upsize_plan(case, tbytes, write):
    """Target merge rules (as the upsize bench states them): the target
    descriptor (mode, start, length, size, burst) of one source burst; mode 0
    passes it unchanged."""
    size, burst, a = case['size'], case['burst'], case['address']
    t = tbytes.bit_length() - 1
    step, n = 1 << size, case['length'] + 1
    if burst == 0 or size > t or (burst == 2 and size == t):
        return 0, a, case['length'], size, burst
    span = n * step
    if burst == 2:
        base = a // span * span
        if span <= tbytes:
            return 1, base, 0, span.bit_length() - 1, 1
        start = a // tbytes * tbytes
        if write and a % tbytes:
            start = base | ((start + tbytes) & (span - 1))
        return 2, start, span // tbytes - 1, t, 2
    end = a // step * step + span
    words = (end - a // tbytes * tbytes + tbytes - 1) // tbytes
    if words > 1:
        return 1, a, words - 1, t, 1
    cover = next(q for q in range(t + 1) if a >> q == (end - 1) >> q)
    return 1, a, 0, cover, 1



def cases(mode):
    fixed, shaped, burst_shapes = mode in FIXED_MODES, mode in SHAPED_MODES, \
        mode in ('widths', *SHAPED_MODES, *WIDE_TARGET_MODES)
    if mode == 'tiles':
        # 128-byte WRAP writes of the wide source across the t5/t7 mapping edge:
        # t5 takes its beats in wrap order, read-only t7 answers SLVERR for
        # its own (DECERR at t5 if the container stayed whole).
        # s2 reads from t5 into the mapping of t7 it does not reach, which
        # answers DECERR, and the rest of the page.
        return [case('w-tiles-wrap8', 's1', 1, APB3 + 0x120, 8, 4, burst=2),
                case('w-tiles-wrap16', 's1', 1, APB3 + 0x108, 16, 3, burst=2),
                case('r-tiles-s2-into-t7', 's2', 0, APB3 + 0x130, 8, 2),
                case('r-tiles-s2-t7', 's2', 0, APB3 + 0x148, 2, 2),
                case('r-tiles-s2-gap', 's2', 0, APB3 + 0x1f8, 4, 2)]
    if mode == 'crossing':
        return crossing_cases()
    mapped = mode == 'mappings'
    mode = base_mode(mode)
    out = []
    if mapped:
        # Relocated mappings: inside the second mapping of t1, from its first
        # mapping through the unmapped middle into the second, the second's
        # tail; the alias of t6 in the page it shares with t5, the shared
        # page's unmapped middle (routed to t5), its tail (routed to t6); the
        # added mappings of t2 (INCR and WRAP) and t0; the sub-pages of t0:
        # inside, past a mapping end, from the remainder into the next mapping,
        # a wide source, a WRAP container larger than the mapping, and inside
        # and out of the mapping in the page of t3.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-m-t1-second', 's0', w, APB256 + 0x210, 4, 2, [('t1', 0x114)]),
                    case(f'{d}-m-t1-across', 's0', w, APB256 + 0xf8, 68, 2),
                    case(f'{d}-m-t1-tail', 's0', w, APB256 + 0x2f8, 4, 2),
                    case(f'{d}-m-t1-wide', 's1', w, APB256 + 0x1f0, 3, 4),
                    case(f'{d}-m-t6-alias', 's0', w, APB3 + 0x210, 4, 2, [('t6', 0x214)]),
                    case(f'{d}-m-t6-wide', 's1', w, APB3 + 0x3e0, 2, 4),
                    case(f'{d}-m-shared-hole', 's0', w, APB3 + 0x110, 2, 2),
                    case(f'{d}-m-t6-tail', 's0', w, APB3 + 0x3f8, 4, 2),
                    case(f'{d}-m-t2-second', 's0', w, AHB1K + 0x810, 6, 2),
                    case(f'{d}-m-t2-wrap', 's0', w, AHB1K + 0x9f8, 4, 2, burst=2),
                    case(f'{d}-m-t0-second', 's0', w, AXI4K + 0x2ff0, 4, 2),
                    case(f'{d}-m-t0-sub', 's0', w, AXI4K + 0x4110, 4, 2, [('t0', 0x6110)]),
                    case(f'{d}-m-t0-sub-end', 's0', w, AXI4K + 0x41f8, 4, 2),
                    case(f'{d}-m-t0-sub-gap', 's0', w, AXI4K + 0x43f0, 12, 2),
                    case(f'{d}-m-t0-sub-wide', 's1', w, AXI4K + 0x41f0, 2, 4),
                    case(f'{d}-m-t0-sub-wrap', 's1', w, AXI4K + 0x4410, 8, 4, burst=2),
                    case(f'{d}-m-t0-shared', 's0', w, APB1K + 0x810, 4, 2),
                    case(f'{d}-m-t0-shared-end', 's0', w, APB1K + 0x8f8, 4, 2),
                    # Decode misses of cut pages: WRAP and FIXED bursts in the
                    # rest of a shared page and of the AXI4 sub-page page, and
                    # a burst from a t0 mapping through the rest into the next.
                    case(f'{d}-m-shared-gap-wrap', 's0', w, APB3 + 0x108, 4, 2, burst=2),
                    case(f'{d}-m-t0-gap-wrap', 's0', w, AXI4K + 0x4208, 4, 2, burst=2),
                    case(f'{d}-m-t0-gap-fixed', 's0', w, AXI4K + 0x4300, 2, 2, burst=0),
                    case(f'{d}-m-t0-through', 's0', w, AXI4K + 0x41f0, 136, 2),
                    # Overlapping mappings: from t0 into t3 inside it and back,
                    # inside t3, a wide source; from t7 into t5 inside it, t5,
                    # and a long t7 burst cut at t5's alignment.
                    case(f'{d}-m-ov-t0-t3', 's0', w, AXI4K + 0xbf0, 8, 2),
                    case(f'{d}-m-ov-t3', 's0', w, AXI4K + 0xc10, 4, 2, [('t3', 0x114)]),
                    case(f'{d}-m-ov-t3-t0', 's0', w, AXI4K + 0xcf8, 4, 2),
                    case(f'{d}-m-ov-t0-wide', 's1', w, AXI4K + 0xbe0, 4, 4),
                    case(f'{d}-m-ov-t7-t5', 's0', w, LITE + 0x10f8, 4, 2),
                    case(f'{d}-m-ov-t5', 's1', w, LITE + 0x1180, 2, 4),
                    case(f'{d}-m-ov-t7-long', 's0', w, LITE + 0x1200, 72, 2)]
    for w in (0, 1):
        d = 'w' if w else 'r'
        b = APB256
        # Window-end crossings into the page remainder of the 256 B APB window.
        out += [case(f'{d}-inside', 's0', w, b + 0xf8, 2, 2),
                case(f'{d}-tail1', 's0', w, b + 0xfc, 2, 2),
                case(f'{d}-tail3', 's0', w, b + 0xfc, 4, 2),
                case(f'{d}-long', 's0', w, b + 0xc0, 64, 2),
                case(f'{d}-page', 's0', w, b + 0x80, 256, 2),
                case(f'{d}-hole', 's0', w, b + 0x100, 4, 2),
                case(f'{d}-slverr-first', 's0', w, b + 0xf8, 4, 2, [('t1', 0xf8)]),
                case(f'{d}-slverr-last', 's0', w, b + 0xf8, 4, 2, [('t1', 0xfc)]),
                case(f'{d}-whole-slverr', 's0', w, b, 64, 2, [('t1', 0x80)]),
                # Unaligned start and byte beats across the window end.
                case(f'{d}-unaligned', 's0', w, b + 0xfd, 2, 2),
                case(f'{d}-bytes', 's0', w, b + 0xfe, 4, 0),
                # The wide source converts 16-byte beats into word accesses.
                case(f'{d}-wide-inside', 's1', w, b + 0xe0, 2, 4),
                case(f'{d}-wide-tail', 's1', w, b + 0xf0, 2, 4, [('t1', 0xf4)]),
                case(f'{d}-wide-long', 's1', w, b + 0x80, 16, 4),
                case(f'{d}-wide-hole', 's1', w, b + 0x200, 2, 4),
                # The 1 KiB APB and AHB windows, and a 40-bit alias whose low
                # 32 bits fall inside the APB window.
                case(f'{d}-apb1k-tail', 's0', w, APB1K + 0x3f8, 4, 2, [('t3', 0x3f8)]),
                case(f'{d}-ahb-tail', 's0', w, AHB1K + 0x3f8, 4, 2),
                case(f'{d}-alias', 's0', w, APB256 & 0xffffffff | 0x0100000000, 2, 2),
                case(f'{d}-ahb-wide', 's1', w, AHB1K + 0x3e0, 2, 4),
                case(f'{d}-ahb-fixed', 's0', w, AHB1K + 0x10, 3, 2, burst=0),
                case(f'{d}-ahb-fixed1', 's0', w, AHB1K + 0x20, 1, 1, burst=0),
                # A FIXED burst starting in the page remainder decodes first.
                case(f'{d}-ahb-fixed-hole', 's0', w, AHB1K + 0x400, 2, 2, burst=0),
                # WRAP and FIXED keep per-beat decode: a WRAP read that starts
                # in the page remainder wraps back into the window.
                case(f'{d}-wrap-in', 's0', w, b + 0x14, 4, 2, burst=2),
                case(f'{d}-wrap-from-hole', 's0', w, APB8 + 0x8, 4, 2, burst=2),
                case(f'{d}-wrap-into-hole', 's0', w, APB8 + 0x4, 4, 2, burst=2),
                case(f'{d}-wide-wrap', 's1', w, APB8 + 0xc, 4, 2, burst=2),
                case(f'{d}-fixed-in', 's0', w, b + 0xfc, 4, 2, burst=0),
                case(f'{d}-fixed-hole', 's0', w, b + 0x100, 3, 2, burst=0),
                # A 16-byte beat straddles the 8-byte window end.
                case(f'{d}-wide-straddle', 's1', w, APB8, 2, 4),
                # APB3/APB2: full words pass, partial words answer SLVERR
                # without access, later beats continue; APB2 never errors.
                case(f'{d}-apb3-inside', 's0', w, APB3 + 0xf0, 4, 2, [('t5', 0xf4)]),
                case(f'{d}-apb3-partial', 's0', w, APB3 + 0xf5, 3, 2),
                case(f'{d}-apb3-bytes', 's0', w, APB3 + 0x10, 4, 0),
                case(f'{d}-apb3-tail', 's1', w, APB3 + 0xe0, 3, 4),
                case(f'{d}-apb2-inside', 's0', w, APB2 + 0x3f0, 4, 2, [('t6', 0x3f4)]),
                case(f'{d}-apb2-tail', 's1', w, APB2 + 0x3e0, 3, 4),
                case(f'{d}-apb2-half', 's0', w, APB2 + 0x22, 2, 1),
                case(f'{d}-apb2-fixed', 's0', w, APB2 + 0x40, 2, 2, burst=0),
                # AXI4-Lite: one transaction per word, strobes preserved.
                case(f'{d}-lite-inside', 's0', w, LITE + 0xfff0, 4, 2, [('t7', 0xfff4)]),
                case(f'{d}-lite-bytes', 's0', w, LITE + 0x123, 3, 0),
                case(f'{d}-lite-wide', 's1', w, LITE + 0x8000, 4, 4, [('t7', 0x8014), ('t7', 0x8028)]),
                case(f'{d}-lite-wrap', 's1', w, LITE + 0x44, 4, 2, burst=2),
                case(f'{d}-lite-fixed', 's0', w, LITE + 0x40, 2, 2, burst=0),
                # Explicit sparse and empty strobes (writes only; reads ignore keep).
                case(f'{d}-apb3-sparse', 's0', w, APB3 + 0x20, 3, 2, keep=0x5),
                case(f'{d}-apb3-empty', 's0', w, APB3 + 0x30, 2, 2, keep=0x0),
                case(f'{d}-apb2-wide-keep', 's1', w, APB2 + 0x100, 2, 4, keep=0x0f30),
                case(f'{d}-lite-sparse', 's1', w, LITE + 0x200, 2, 4, keep=0x80f1),
                case(f'{d}-lite-empty', 's0', w, LITE + 0x300, 2, 2, keep=0x0),
                # AXI4-Lite writes a word without strobes with WSTRB zero and
                # returns the target's response: an injected SLVERR fails B.
                case(f'{d}-lite-empty-err', 's0', w, LITE + 0x310, 2, 2, [('t7', 0x314)], keep=0x0),
                case(f'{d}-apb4-sparse', 's0', w, b + 0x40, 2, 2, keep=0xa)]
    if mode == 'attributes':
        # Mapped protection on APB4, AHB and AXI4-Lite targets for several AxCACHE values.
        for w in (0, 1):
            d = 'w' if w else 'r'
            for t, base in (('t1', APB256), ('t2', AHB1K), ('t3', APB1K), ('t7', LITE)):
                for cache in (0, 3, 4, 8, 0xa, 0xf):
                    out.append(dict(case(f'{d}-a-{t}-c{cache}', 's0', w, base + 0x10 + 4 * cache,
                                         1, 2), cache=cache, prot=cache * 3 & 7))
        # A write and a read share each dedicated bus; AxCACHE 4 allocates on
        # reads only, so the direction of every mapped access shows.
        for t, base in (('t1', APB256), ('t2', AHB1K), ('t7', LITE)):
            out.append(dict(case(f'a-{t}-rw', 's0', 1, base + 0x80, 4, 2,
                                 partner=dict(case('', 's2', 0, base + 0xc0, 4, 2), cache=4,
                                              prot=5)),
                            cache=4, prot=5))
    if mode == 'packets':
        # A partial word fails independently; later full words are served.
        # The wide source splits its non-modifiable narrow INCR per beat, so a
        # partial word makes no access.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-p-apb3-drain', 's0', w, APB3 + 0x2, 40, 2),
                    case(f'{d}-p-apb2-drain', 's0', w, APB2 + 0x142, 40, 2),
                    case(f'{d}-p-narrow-split', 's1', w, APB3 + 0x40, 4, 2, keep=0xffdf),
                    # A modifiable narrow INCR (AxCACHE[1] set) is not split.
                    dict(case(f'{d}-p-narrow-merge', 's1', w, APB3 + 0x60, 4, 2, keep=0xffdf),
                         cache=2),
                    case(f'{d}-p-ahb-long', 's0', w, AHB1K + 0x3c, 40, 2),
                    # A narrow WRAP whose container fits one source word goes
                    # out as one beat at the container base, whatever its
                    # AxCACHE: targets see the container in address order.
                    dict(case(f'{d}-p-fold-apb', 's1', w, APB256 + 0x28, 4, 2, keep=0xff7f,
                              burst=2), cache=2),
                    dict(case(f'{d}-p-fold-ahb', 's1', w, AHB1K + 0x108, 4, 2, burst=2), cache=2),
                    case(f'{d}-p-fold-c0', 's1', w, APB1K + 0x38, 4, 2, burst=2),
                    case(f'{d}-p-fold-bytes', 's1', w, APB256 + 0x46, 4, 0, keep=0xffbf, burst=2),
                    case(f'{d}-p-fold-half', 's1', w, LITE + 0x2c, 2, 2, burst=2),
                    case(f'{d}-p-fold-err', 's1', w, LITE + 0x58, 4, 2, [('t7', 0x5c)], burst=2),
                    # A container larger than the source word keeps its WRAP;
                    # a 32-bit source folds a byte WRAP4 into its word.
                    case(f'{d}-p-wrap-big', 's1', w, LITE + 0x64, 8, 2, burst=2),
                    case(f'{d}-p-wrap-s0', 's0', w, APB256 + 0x62, 4, 0, burst=2)]
            out += [case(f'{d}-p-x-apb3-drain', 's1', w, APB3 + 0x88, 4, 2, keep=0xfff7, burst=2),
                    case(f'{d}-p-x-apb2', 's1', w, APB2 + 0x1f4, 2, 2, burst=2),
                    case(f'{d}-p-x-apb3-bytes', 's1', w, APB3 + 0xa5, 4, 0, burst=2),
                    case(f'{d}-p-x-ahb-b', 's1', w, AHB1K + 0x3fd, 2, 0, burst=2),
                    case(f'{d}-p-x-ro', 's1', w, AXI4K + 0x40c, 4, 2, burst=2),
                    case(f'{d}-p-x-err-apb', 's1', w, APB256 + 0xfa, 4, 1, [('t1', 0xf8)], burst=2),
                    case(f'{d}-p-x-len15', 's1', w, APB1K + 0x3f9, 16, 0, keep=0xdfff, burst=2)]
        out.append(case('rw-p-x-both', 's1', 1, LITE + 0x208, 4, 2, burst=2,
                        partner=case('', 's1', 0, LITE + 0x304, 4, 1, burst=2)))
        out.append(case('rw-p-x-mix', 's1', 1, APB1K + 0x108, 4, 1, burst=2,
                        partner=case('', 's1', 0, APB1K + 0x204, 3, 2)))
        out.append(case('r-p-fold-axi', 's1', 0, AXI4K + 0x408, 4, 2, burst=2))
    if mode in ('widths', 'widths_uniform'):
        # The width clamp keeps wide WRAP bursts (as the wide-to-32 converter
        # does): each 16-byte beat splits into its words in container
        # order. A beat straddling the 8-byte window end merges DECERR words
        # with a SLVERR word.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-clamp-wrap-apb', 's1', w, APB256 + 0x20, 4, 4, burst=2),
                    case(f'{d}-clamp-wrap-ahb', 's1', w, AHB1K + 0x3d0, 2, 4, burst=2),
                    case(f'{d}-clamp-wrap-lite', 's1', w, LITE + 0x130, 4, 4, [('t7', 0x124)],
                         burst=2),
                    case(f'{d}-clamp-wrap-apb3', 's1', w, APB3 + 0x40, 2, 4, burst=2),
                    case(f'{d}-clamp-merge', 's1', w, APB8, 2, 4, [('t4', 0x4)])]
    if fixed:
        # FIXED bursts served as single transfers: every beat at the repeated
        # address with its own size and strobes; t3 keeps the SLVERR rule.
        # AHB subword and sparse transfers are covered by the AHB outlet bench.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-f-apb4', 's0', w, APB256 + 0x40, 5, 2, burst=0),
                    case(f'{d}-f-apb4-one', 's0', w, APB256 + 0x44, 1, 2, burst=0),
                    case(f'{d}-f-apb4-byte', 's0', w, APB256 + 0x4b, 3, 0, burst=0),
                    case(f'{d}-f-apb4-sparse', 's0', w, APB256 + 0x50, 3, 2, keep=0x9, burst=0),
                    case(f'{d}-f-apb4-err', 's0', w, APB256 + 0x60, 3, 2, [('t1', 0x60)], burst=0),
                    case(f'{d}-f-apb4-miss', 's0', w, APB256 + 0x104, 3, 2, burst=0),
                    case(f'{d}-f-reject', 's0', w, APB1K + 0x10, 3, 2, burst=0),
                    case(f'{d}-f-reject-hole', 's0', w, APB8 + 0x10, 2, 2, burst=0),
                    case(f'{d}-f-ahb', 's0', w, AHB1K + 0x30, 4, 2, burst=0),
                    case(f'{d}-f-ahb-miss', 's0', w, AHB1K + 0x800, 2, 2, burst=0),
                    # APB3, an unaligned FIXED start: a read reads the word on
                    # every beat; a write carries the start's partial lanes on
                    # every beat (the source BFM), so every beat fails alone.
                    case(f'{d}-f-apb3-unaligned', 's0', w, APB3 + 0x21, 3, 2, burst=0),
                    case(f'{d}-f-apb3-partial', 's0', w, APB3 + 0x30, 2, 2, keep=0x3, burst=0),
                    case(f'{d}-f-apb3-empty', 's0', w, APB3 + 0x34, 2, 2, keep=0x0, burst=0),
                    case(f'{d}-f-apb3-err', 's0', w, APB3 + 0x38, 2, 2, [('t5', 0x38)], burst=0),
                    case(f'{d}-f-apb2', 's0', w, APB2 + 0x80, 4, 2, burst=0),
                    case(f'{d}-f-apb2-byte', 's0', w, APB2 + 0x85, 2, 0, burst=0),
                    case(f'{d}-f-lite', 's0', w, LITE + 0x1230, 16, 2, burst=0),
                    case(f'{d}-f-lite-err', 's0', w, LITE + 0x1240, 2, 2, [('t7', 0x1240)], burst=0),
                    # The wide source: a 4-byte FIXED beat reaches its lane; a
                    # beat wider than the 32-bit target answers SLVERR without
                    # access, also at a target serving FIXED.
                    case(f'{d}-f-wide-word', 's1', w, APB256 + 0x74, 3, 2, burst=0),
                    case(f'{d}-f-wide-lite', 's1', w, LITE + 0x2008, 2, 2, keep=0x0600, burst=0),
                    case(f'{d}-f-wide-beat', 's1', w, LITE + 0x2020, 2, 4, burst=0),
                    case(f'{d}-f-wide-apb3', 's1', w, APB3 + 0x80, 2, 3, burst=0),
                    case(f'{d}-f-wide-reject', 's1', w, APB1K + 0x80, 2, 4, burst=0),
                    # In the page remainder the window decode comes first: DECERR,
                    # also at targets serving FIXED (APB4, APB3).
                    case(f'{d}-f-wide-reject-tail', 's1', w, APB1K + 0x800, 2, 4, burst=0),
                    case(f'{d}-f-wide-single-tail', 's1', w, APB256 + 0x200, 2, 4, burst=0),
                    case(f'{d}-f-wide-apb3-tail', 's1', w, APB3 + 0x408, 3, 3, burst=0)]
            # FIFO word: every read beat returns the value of its own access.
            out += [case(f'{d}-f-fifo-{t}', 's0', w, TARGETS[t]['base'] + FIFO_LOCAL, 4, 2, burst=0)
                    for t in sorted(FIXED_SINGLE)]
            out += [case(f'{d}-f-fifo-byte', 's0', w, APB256 + FIFO_LOCAL + 1, 3, 0, burst=0),
                    case(f'{d}-f-fifo-ahb-byte', 's0', w, AHB1K + FIFO_LOCAL, 3, 0, burst=0),
                    # AHB ERROR (two-cycle response) on every beat of a
                    # served FIXED burst: each beat reaches the target and
                    # fails; the write B merges SLVERR.
                    case(f'{d}-f-ahb-err', 's0', w, AHB1K + 0x40, 3, 2, [('t2', 0x40)], burst=0)]
        # APB3/APB2: a partial-strobe beat in the middle of a FIXED burst fails
        # alone; the full beats after it are still written.
        out += [case('w-f-apb3-mid-partial', 's0', 1, APB3 + 0x90, 4, 2, burst=0,
                     keeps=[0xf, 0x3, 0xf, 0xf]),
                case('w-f-apb2-mid-partial', 's0', 1, APB2 + 0x90, 3, 2, burst=0,
                     keeps=[0xf, 0xc, 0xf]),
                case('w-f-apb3-two-partial', 's0', 1, APB3 + 0x94, 4, 2, burst=0,
                     keeps=[0x1, 0xf, 0x8, 0xf]),
                # An unaligned FIXED write as an AXI master drives it: every
                # beat carries the start's partial strobes. APB4 writes them
                # on every beat; APB3 fails every beat without access.
                case('w-f-apb4-unaligned-same', 's0', 1, APB256 + 0xa1, 3, 2, burst=0,
                     keeps=[0xe, 0xe, 0xe]),
                case('w-f-apb3-unaligned-same', 's0', 1, APB3 + 0xa1, 3, 2, burst=0,
                     keeps=[0xe, 0xe, 0xe])]
    if burst_shapes:
        # AHB burst shapes: INCR packets of several lengths and across the
        # window end (also the 1 KiB boundary), and 64-bit beats of the wide
        # source in WRAP4 and WRAP8 bursts, whose 8- and 16-word containers
        # reach the 32-bit target as WRAP8 and WRAP16; a WRAP16 of 64-bit
        # beats (32 target words) reaches it as two INCR pieces in wrap order.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-ahb-incr5', 's0', w, AHB1K + 0x100, 5, 2),
                    case(f'{d}-ahb-incr7', 's0', w, AHB1K + 0x124, 7, 2),
                    case(f'{d}-ahb-incr17', 's0', w, AHB1K + 0x140, 17, 2),
                    case(f'{d}-ahb-end20', 's0', w, AHB1K + 0x3c4, 20, 2),
                    case(f'{d}-ahb-end16', 's0', w, AHB1K + 0x3f0, 16, 2),
                    case(f'{d}-ahb-wrap4-64', 's1', w, AHB1K + 0x2a8, 4, 3, burst=2),
                    case(f'{d}-ahb-wrap8-64', 's1', w, AHB1K + 0x248, 8, 3, burst=2),
                    case(f'{d}-ahb-wrap16-64', 's1', w, AHB1K + 0x188, 16, 3, burst=2)]
        if shaped:
            # Complete writes: a partial word inside what would be INCR4 or
            # WRAP8 does not end a fixed-length burst.
            out += [case('w-ahb-sparse-incr4', 's0', 1, AHB1K + 0x60, 4, 2,
                         keeps=[0xf, 0xf, 0x3, 0xf]),
                    case('w-ahb-sparse-wrap8', 's0', 1, AHB1K + 0x88, 8, 2, burst=2,
                         keeps=[0xf, 0xf, 0xf, 0x3, 0xf, 0xf, 0xf, 0xf])]
    # Read-only AXI4 target: reads burst normally, writes drain W and answer
    # SLVERR; the read-only source s2 reads through the same network.
    out += [case('ro-read', 's0', 0, AXI4K + 0x7f0, 4, 2, [('t0', 0x7f4)]),
            case('ro-write', 's0', 1, AXI4K + 0x100, 4, 2),
            case('ro-wide-write', 's1', 1, AXI4K + 0x200, 2, 4),
            case('ro-wide-read', 's1', 0, AXI4K + 0x300, 16, 4),
            case('s2-apb-read', 's2', 0, APB256 + 0xf8, 4, 2),
            case('s2-lite-read', 's2', 0, LITE + 0x40, 3, 2, [('t7', 0x44)]),
            case('s2-ro-read', 's2', 0, AXI4K + 0x10, 2, 2),
            # An unaligned single word, Non-modifiable and modifiable: only
            # the target merge of the widths modes gives the modifiable one
            # its covering size, so the target sees its unaligned address.
            dict(case('ro-single-c0', 's0', 0, AXI4K + 0x7e2, 1, 2), cache=0),
            dict(case('ro-single-c2', 's0', 0, AXI4K + 0x7e2, 1, 2), cache=2),
            case('s2-contend', 's2', 0, APB256 + 0x10, 4, 2,
                 partner=case('', 's0', 1, APB256 + 0x10, 4, 2))]
    # Both sources contend for one APB outlet: separate read/write owners share
    # the bus, and two writes serialize on the single write owner.
    out += [case('rw-contend', 's0', 1, APB256 + 0xf8, 4, 2,
                 partner=case('', 's1', 0, APB256 + 0xe0, 4, 4)),
            # The partner write stays off the first one's words: the checker
            # attributes target accesses by their words.
            case('ww-contend', 's0', 1, APB256 + 0xf0, 8, 2, [('t1', 0xf4)],
                 partner=case('', 's1', 1, APB256 + 0x80, 4, 4)),
            case('rr-contend', 's0', 0, APB256 + 0xfc, 2, 2,
                 partner=case('', 's1', 0, APB256 + 0xf0, 2, 4)),
            case('lite-contend', 's0', 1, LITE + 0x100, 4, 2,
                 partner=case('', 's1', 0, LITE + 0x100, 2, 4))]
    return out


def crossing_cases():
    """Single bursts across t1, t3 and t7 (and into the decode miss) of both
    sources, a WRAP container across the t3/t7 edge, then batches issued
    without waiting: a burst crossing from t1 into slow t3, one under the
    same ID back at fast t1 (it waits for the first to be answered), one
    continuing at t3, and others under their own IDs."""
    b, out = APB256, []
    for w in (0, 1):
        d = 'w' if w else 'r'
        out += [case(f'{d}-x-t1-t3', 's0', w, b + 0xf0, 8, 2),
                case(f'{d}-x-t3-t7', 's0', w, b + 0x170, 8, 2),
                case(f'{d}-x-all', 's0', w, b + 0xe0, 80, 2),
                case(f'{d}-x-t7-miss', 's0', w, b + 0x1f8, 4, 2),
                case(f'{d}-x-wide', 's1', w, b + 0xc0, 12, 4),
                case(f'{d}-x-wrap', 's1', w, b + 0x1a0, 16, 4, burst=2),
                dict(case(f'{d}-x-batch', 's0', w, b + 0xf0, 8, 2, batch=[
                    (case('', 's0', w, b + 0x10, 4, 2), 1),
                    (case('', 's0', w, b + 0x120, 4, 2), 1),
                    (case('', 's0', w, b + 0x170, 8, 2), 2),
                    (case('', 's0', w, b + 0x1a0, 4, 2), 3),
                    (case('', 's0', w, b + 0x40, 4, 2), 2)]), id=1)]
    out.append(case('rw-x-batch', 's1', 0, b + 0xc0, 8, 4, batch=[
        (case('', 's1', 1, b + 0x60, 12, 4), 1), (case('', 's2', 0, b + 0x160, 16, 2), 1),
        (case('', 's1', 0, b + 0x1c0, 2, 4), 1), (case('', 's0', 1, b + 0x150, 16, 2), 4)]))
    return out


def beats(c):
    step = 1 << c['size']
    aligned = c['address'] // step * step
    span = c['beats'] * step
    for i in range(c['beats']):
        if c['burst'] == 0:
            start = aligned
        elif c['burst'] == 2:
            start = aligned // span * span + (aligned % span + i * step) % span
        else:
            start = aligned + i * step
        yield i, c['address'] if i == 0 else start, start + step


def page_owner(address, source, mode):
    """The target of the mapping holding an address; elsewhere in a page of
    sub-page mappings one whose decode answers it DECERR without access (the
    last mapping at or below it, else the first of the page), as the default
    target does for a decode miss of a cut page (docs/address-map.md#target-mappings)."""
    maps = [(b, s, name) for name in TARGETS for b, s, _ in mappings(mode, name)]
    owner = (holding(mode, address) or [None] * 4)[3]
    if owner is None:
        page = sorted(m for m in maps if m[1] < 0x1000 and m[0] // 0x1000 == address // 0x1000)
        below = [m for m in page if m[0] <= address]
        owner = below[-1][2] if below else page[0][2] if page else None
    return None if owner is None or (source, owner) in denied(mode) else owner


def data_word(beat, lane):
    return (0x50403020 ^ (beat * 0x01010101) ^ (lane << 28)) & 0xffffffff


def read_word(target, local):
    return ((0xa0 + int(target[1:])) << 24) | local


def expected(c, mode):
    """Target accesses in order, per-beat R (resp, data) or one B code.
    With a partner, accesses compare as a multiset and results per source."""
    if 'partner' in c:
        first, second = single(c, mode), single(c['partner'], mode)
        return sorted(first[0] + second[0]), sorted(first[1] + second[1])
    return single(c, mode)


def protection(target, write, cache, prot, mode):
    # PPROT, AXI4-Lite AxPROT or AHB HPROT the target sees for one access.
    ahb = TARGETS[target]['kind'] == 'ahb_v3'
    spec = ATTRIBUTES.get(target) if mode == 'attributes' else None
    if spec is None:
        return (cache & 3) << 2 | (prot & 1) << 1 | (~prot >> 2 & 1) if ahb else prot
    if spec == 'zero':
        bits = ['0'] * (4 if ahb else 3)
    else:
        bits = (AMBA if spec == 'amba' else spec)['hprot' if ahb else 'prot']
    flags = {'0': 0, '1': 1, 'bufferable': cache & 1, 'modifiable': cache >> 1 & 1,
             'allocate': cache >> (3 if write else 2) & 1, 'lookup': (cache >> 2 | cache >> 3) & 1,
             'privileged': prot & 1, 'nonsecure': prot >> 1 & 1, 'instruction': prot >> 2 & 1}
    m = sum((flags[b.lstrip('~')] ^ b.startswith('~')) << i for i, b in enumerate(bits))
    return (m & 0xe) | (~m & 1) if ahb else m


def protection_events(c, mode):
    # One (target, write, protection) per access to an APB4, AHB or AXI4-Lite
    # target; sources drive AxPROT 3'b010 on writes and 3'b000 on reads.
    if 'partner' in c:
        return sorted(protection_events({k: v for k, v in c.items() if k != 'partner'}, mode)
                      + protection_events(c['partner'], mode))
    events = []
    for target, write, *_ in single(c, mode)[0]:
        if TARGETS[target]['kind'] in ('apb4', 'ahb_v3', 'axi4_lite'):
            events.append((target, write, protection(target, write, c.get('cache', 0),
                                                     c.get('prot', 2 if write else 0), mode)))
    return events


def packet_ends(c, mode):
    # Packets: exclusive ends of the packets of an INCR burst to a dedicated
    # target (limit 128 B, AHB 64 B, from the alignment word max(source bytes,
    # 4), cut at the window's own boundary); the tail-hole remainder and other
    # bursts are one packet.
    step = 1 << c['size']
    end = c['address'] // step * step + c['beats'] * step
    owner = page_owner(c['address'], c['source'], mode)
    if c['burst'] != 1 or owner is None:
        return [end]
    t = TARGETS[owner]
    if t['kind'] == 'axi4':
        return [end]
    named = mode
    if (base_mode(mode) == 'packets' and c['source'] == 's1' and c['size'] < 4
            and not c.get('cache', 0) & 2):
        # Non-modifiable narrow INCR (AxCACHE 0): one packet per beat.
        return [c['address'] // step * step + (i + 1) * step for i in range(c['beats'])]
    align = max(SOURCES[c['source']] // 8, 4)
    limit = 64 if t['kind'] == 'ahb_v3' else 128
    ends, start = [], c['address']
    while start < end:
        held = holding(named, start, [owner])
        if not held:
            ends.append(end)
            break
        # A mapping holding smaller ones also cuts at their alignment.
        inner = [b & -b for n in TARGETS for b, s, _ in mappings(named, n)
                 if held[1] < b < held[1] + held[0] and s < held[0]]
        cross = min([held[0], 1024 if t['kind'] == 'ahb_v3' else 1 << bits(named, owner)] + inner)
        start = min(start // align * align + limit, start // cross * cross + cross, end)
        ends.append(start)
    return ends


def folded(c, mode):
    # A write may fold a source word. Read responses can share a container
    # only while one physical target word supplies all of its source beats.
    span = c['beats'] << c['size']
    width = SOURCES[c['source']] // 8 if c['write'] else 4
    return c['burst'] == 2 and 1 << c['size'] < width and span <= width


def bus_beats(c, mode):
    # (source beats answered, lo, hi, owner of each byte) per beat on the bus.
    if folded(c, mode):
        span = c['beats'] << c['size']
        base = c['address'] // span * span
        owner = {}
        for i, lo, hi in beats(c):
            for byte in range(lo, hi):
                owner[byte] = i
        return [(list(range(c['beats'])), base, base + span, owner)]
    split = (base_mode(mode) == 'packets' and c['source'] == 's1' and c['size'] < 4
             and not c.get('cache', 0) & 2)
    if c['burst'] != 1 or c['size'] >= 2 and c['beats'] > 1 or split:
        return [([i], lo, hi, None) for i, lo, hi in beats(c)]
    # Every addressed target packs at 4 bytes: the beats of one 32-bit word go
    # out as one word.
    groups = {}
    for i, lo, hi in beats(c):
        answered, first, _, owner = groups.setdefault(lo // 4, ([], lo, hi, {}))
        answered.append(i)
        owner.update({byte: i for byte in range(lo, hi)})
        groups[lo // 4] = (answered, first, hi, owner)
    return list(groups.values())


def merged_read(c, mode):
    # Widths modes merge an INCR read of a 32-bit source into the read-only
    # 32-bit AXI4 target's words, whatever its AxCACHE.
    return (base_mode(mode) in ('widths', 'widths_uniform') and not c['write']
            and c['burst'] == 1 and SOURCES[c['source']] == 32
            and page_owner(c['address'], c['source'], mode) == 't0')


def single_merged(c):
    # The target burst from the merge rules; the target logs and answers each
    # beat at its AxSIZE-aligned address. A failed word fails the source beats
    # it holds with zero data.
    base = TARGETS['t0']['base']
    errors = {(t, a) for t, a in c['errors']}
    _, start, length, size, _ = upsize_plan(
        {'address': c['address'], 'size': c['size'], 'length': c['beats'] - 1, 'burst': 1}, 4,
        False)
    target_beats = [(start >> size << size) + (k << size) for k in range(length + 1)]
    accesses = [('t0', 0, b - base, 0, 0) for b in target_beats]
    results = []
    for i, lo, hi in beats(c):
        b = next(b for b in target_beats if b <= lo < b + (1 << size))
        if ('t0', b - base) in errors:
            results.append(('R', c['source'], i, 2, 0))
            continue
        word = read_word('t0', b - base)
        data = 0
        for a in range(lo >> c['size'] << c['size'], hi):
            data |= ((word >> 8 * (a % 4)) & 0xff) << 8 * (a % 4)
        results.append(('R', c['source'], i, 0, data))
    return accesses, results


def single(c, mode):
    if merged_read(c, mode):
        return single_merged(c)
    width = SOURCES[c['source']] // 8
    # A WRAP longer than 16 target words reaches the target as INCR pieces in
    # wrap order: per word, the same accesses as below.
    mode_is_fifo = mode in FIXED_MODES
    errors = {(t, a) for t, a in c['errors']}
    start = page_owner(c['address'], c['source'], mode)
    own = TARGETS.get(start)
    if (c['burst'] == 0 and 1 << c['size'] > 4 and own is not None
            and local(mode, start, c['address']) is not None):
        # A FIXED beat wider than the 32-bit target is never cut: SLVERR on
        # every beat without access, whatever fixed_bursts says. The window
        # decode comes first, so a start in the page remainder is cut and
        # its words answer DECERR below.
        if c['write']:
            return [], [('B', c['source'], 2)]
        return [], [('R', c['source'], i, 2, 0) for i in range(c['beats'])]
    accesses, results = [], []
    fifo = {}  # FIFO-word read accesses so far in this case, per target
    for answered, lo, hi, owner_of in bus_beats(c, mode):
        i = answered[0]
        code, data = 0, 0
        owner = page_owner(lo, c['source'], mode) if start is not None else None
        t = TARGETS.get(owner)
        if owner is None:
            code = 3
        else:
            # Writes carry the exact strobes. Reads return every lane of the
            # AxSIZE-aligned beat; the bytes below an unaligned start are
            # unspecified by AXI and this profile returns them.
            first = lo if c['write'] else lo >> c['size'] << c['size']
            keep = c['keep'] & c['keeps'][i] if 'keeps' in c else c['keep']
            for word in range(first // 4 * 4, hi, 4):
                lanes = [k for k in range(4) if first <= word + k < hi and
                         (not c['write'] or keep >> (word % width + k) & 1)]
                inside = local(mode, owner, word) is not None
                if c['write'] and inside and read_only(mode, owner):
                    # A read-only target absorbs the write: SLVERR, no access.
                    code = 3 if code == 3 else 2
                    continue
                if c['write'] and not lanes and t['kind'] != 'axi4_lite':
                    continue  # no strobes: no APB or AHB access and no error
                if not inside:
                    # Each 32-bit word decodes on its own; a word outside the
                    # window reads zero and makes the whole beat DECERR.
                    code = 3
                    continue
                if c['burst'] == 0 and t['kind'] != 'axi4' and not fixed_single(mode, owner):
                    # APB, AHB and AXI4-Lite have no FIXED burst: by default
                    # FIXED, single beats included, answers SLVERR without
                    # access. With fixed_bursts: single each beat is served
                    # on its own.
                    code = 3 if code == 3 else 2
                    continue
                strobeless = t['kind'] in ('apb3', 'apb2')
                if c['write'] and strobeless and len(lanes) != 4:
                    # No PSTRB: reject this physical word and keep checking.
                    code = 3 if code == 3 else 2
                    continue
                at = local(mode, owner, word)
                if t['kind'] == 'axi4' and c['burst'] == 1 and c['beats'] == 1:
                    # A packed single beat reaches an AXI4 target at the
                    # container of the size covering its bytes.
                    cover = next(q for q in range(3) if lo >> q == (hi - 1) >> q)
                    at = local(mode, owner, lo >> cover << cover)
                lane = (word % width) // 4
                strobe = sum(1 << k for k in lanes)
                if c['write']:
                    payload = data_word(i, lane)
                    if owner_of is not None:
                        # A folded word keeps only the strobed bytes, each
                        # from the beat that wrote it.
                        payload = sum((data_word(owner_of[word + k], lane) >> 8 * k & 0xff)
                                      << 8 * k for k in lanes)
                    # Sources drive data on every lane; a byte without its
                    # strobe reaches the target as zero.
                    payload &= sum(0xff << 8 * k for k in lanes)
                    accesses.append((owner, 1, at, strobe, payload))
                else:
                    accesses.append((owner, 0, at, 0, 0))
                reads = 0
                if not c['write'] and mode_is_fifo and owner in FIXED_SINGLE and \
                        at == FIFO_LOCAL:
                    reads = fifo.get(owner, 0)
                    fifo[owner] = reads + 1
                if (owner, at) in errors and t['kind'] != 'apb2':
                    # The Lite BFM's second error region returns DECERR.
                    # Reads preserve it on just the beats using that word.
                    regions = [a for name, a in c['errors'] if name == owner]
                    native = 3 if not c['write'] and t['kind'] == 'axi4_lite' and \
                        regions.index(at) == 1 else 2
                    code = 3 if code == 3 or native == 3 else 2
                elif not c['write']:
                    value = read_word(owner, at) ^ (reads * 0x01010101)
                    for k in lanes:
                        data |= ((value >> (8 * k)) & 0xff) << (8 * (k + 4 * lane))
        # A failed beat reads as zero, a wide one as a whole.
        if code:
            data = 0
        if mode in ('uniform', 'widths_uniform') and code:
            code = 2
        results += [(code, data if not c['write'] else 0)] * len(answered)
    if c['write']:
        codes = [r[0] for r in results]
        b = 3 if 3 in codes else 2 if 2 in codes else 0
        return accesses, [('B', c['source'], b)]
    return accesses, [('R', c['source'], i, code, data) for i, (code, data) in enumerate(results)]
