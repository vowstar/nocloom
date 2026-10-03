"""Per-target data widths: configuration, cases and an address-level oracle.
The merge rule upsize_plan also serves the target-side merge bench.
The verif case cases/mixed_widths.py drives
the cases and turns address_events into the exact target bursts and
attributes each source transaction must reach.

Sources of 32..256 bits reach AXI4 targets of 32, 128 and 256 bits. Every
source cuts its INCR bursts into packets (2^len_bits target words, at most 4 KiB,
from the alignment word) and folds a narrow WRAP whose container fits its word
into one INCR beat. A burst no wider than its target is merged into target
words whatever its AxCACHE (a split_non_modifiable source first sends each beat
of a Non-modifiable narrow INCR as its own packet). A wider burst is
clamped to the target width: INCR covers the target words from the start word
on and WRAP keeps its container. Such a burst reaches the target as pieces: a
WRAP of at most 16 target words stays one WRAP; otherwise the wrap point
starts a new INCR piece and no piece holds more than 2^len_bits target words.
Pieces follow each other without waiting for responses and merge into one B.
A FIXED burst wider than its target is never cut: it answers SLVERR (one B
after all W beats, every read beat with zero data) and the target sees nothing.

The net64 variant is a 64-bit network with 4-bit AxLEN and one 64-bit source;
the wide variants attach 32- to 1024-bit sources to 32-, 256-, 512- and
1024-bit targets (wide_packets adds a splitting 512-bit source, wide_clocked puts the switches and targets in their own domains);
use(mode) switches the shared endpoint tables before anything else
reads them. The outstanding variant is the graph network with a shared capacity of four bursts
at s32, the one source that reaches no narrower target: the
response tag rides every converter and target merge."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

SOURCES = {'s32': 32, 's64': 64, 's128': 128, 's256': 256}
TARGETS = {
    't32': {'base': 0x1000, 'size': 0x1000, 'bits': 32},
    't128': {'base': 0x10000, 'size': 0x10000, 'bits': 128},
    't256': {'base': 0x20000, 'size': 0x10000, 'bits': 256},
}
ROUTES = [(s, t) for s in SOURCES for t in TARGETS]


def upsize_plan(case, tbytes, write):
    # Merge rules: target descriptor (mode, start, length, size, burst); mode 0 passes.
    size, burst, a = case['size'], case['burst'], case['address']
    t = tbytes.bit_length() - 1
    step, n = 1 << size, case['length'] + 1
    if burst == 0 or size > t or (burst == 2 and size == t):
        return 0, a, case['length'], size, burst
    span = n * step
    if burst == 2:
        base = a // span * span
        if span <= tbytes:
            # The container is accessed at its own size.
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


NETWORK = {'bits': 32, 'len_bits': 8, 'attributes': {}, 'split': set(),
           'merge': set(), 'outstanding': {}}
# Target attribute maps of the attributes mode: a preset name or explicit bit
# lists, least significant bit first.
ATTRIBUTES = {
    't32': 'zero',
    't128': 'amba',
    't256': {'read_cache': ['~bufferable', 'allocate', 'lookup', '1'],
             'write_cache': ['modifiable', 'allocate', '~lookup', '0'],
             'prot': ['~privileged', '1', 'nonsecure']},
}
AMBA = {'read_cache': ['bufferable', 'modifiable', 'allocate', 'lookup'],
        'write_cache': ['bufferable', 'modifiable', 'lookup', 'allocate'],
        'prot': ['privileged', 'nonsecure', 'instruction']}


def attribute_bits(target, write, cache, prot):
    # (AxCACHE, AxPROT) a target sees: carried through without a map.
    spec = NETWORK['attributes'].get(target)
    if spec is None:
        return cache, prot
    if spec == 'zero':
        return 0, 0
    spec = AMBA if spec == 'amba' else spec
    flags = {'0': 0, '1': 1, 'bufferable': cache & 1, 'modifiable': cache >> 1 & 1,
             'allocate': cache >> (3 if write else 2) & 1, 'lookup': (cache >> 2 | cache >> 3) & 1,
             'privileged': prot & 1, 'nonsecure': prot >> 1 & 1, 'instruction': prot >> 2 & 1}
    def value(bits):
        return sum((flags[b.lstrip('~')] ^ b.startswith('~')) << i for i, b in enumerate(bits))
    return value(spec['write_cache' if write else 'read_cache']), value(spec['prot'])


WIDE_MODES = ('wide', 'wide_packets', 'wide_clocked')


def use(mode):
    if mode in WIDE_MODES:
        SOURCES.clear()
        SOURCES.update({'s32': 32, 's256': 256, 's512': 512, 's1024': 1024})
        TARGETS.clear()
        TARGETS.update({
            't32': {'base': 0x1000, 'size': 0x1000, 'bits': 32},
            't256': {'base': 0x10000, 'size': 0x10000, 'bits': 256},
            't512': {'base': 0x20000, 'size': 0x10000, 'bits': 512},
            't1024': {'base': 0x40000, 'size': 0x10000, 'bits': 1024},
        })
        ROUTES[:] = [(s, t) for s in SOURCES for t in TARGETS]
        # s256 merges every burst into the wider targets' words.
        NETWORK.update(merge={'s256'})
        if mode == 'wide_packets':
            NETWORK.update(split={'s512'})
    if mode in ('graph', 'clocked', 'outstanding'):
        # s64 spells the default (narrow_bursts merge).
        NETWORK.update(merge={'s64'})
    if mode == 'outstanding':
        NETWORK.update(outstanding={'s32': 4})
    if mode == 'net64':
        SOURCES.clear()
        SOURCES['s64'] = 64
        ROUTES[:] = [(s, t) for s in SOURCES for t in TARGETS]
        NETWORK.update(bits=64, len_bits=4)
    if mode == 'packets':
        # s128 splits non-modifiable narrow INCR per beat and folds narrow
        # WRAP bursts that fit its word.
        NETWORK.update(split={'s128'})
    if mode == 'attributes':
        NETWORK.update(attributes=ATTRIBUTES)


def configuration(mode):
    targets = [{'name': t, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': spec['bits'],
                **({'attributes': NETWORK['attributes'][t]} if t in NETWORK['attributes'] else {}),
                'mappings': [{'base': hex(spec['base']), 'size': hex(spec['size'])}]}
               for t, spec in TARGETS.items()]
    request = {'nodes': [{'name': 'q0', 'transport_word_bytes': 32},
                         {'name': 'q1', 'transport_word_bytes': 16}],
               'links': [{'from': 'q0', 'to': 'q1', 'transport_word_bytes': 8}],
               'entries': [{'endpoint': s, 'node': 'q0'} for s in SOURCES],
               'exits': [{'endpoint': t, 'node': 'q1'} for t in TARGETS]}
    response = {'nodes': [{'name': 'p0', 'transport_word_bytes': 16},
                          {'name': 'p1', 'transport_word_bytes': 32}],
                'links': [{'from': 'p0', 'to': 'p1', 'transport_word_bytes': 4}],
                'entries': [{'endpoint': t, 'node': 'p0'} for t in TARGETS],
                'exits': [{'endpoint': s, 'node': 'p1'} for s in SOURCES]}
    clocks = [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}]
    if mode in ('clocked', 'wide_clocked'):
        clocks = [{'name': n, 'clock_port': 'clk_' + n, 'reset_port': 'reset_n', 'test_port': 'test'} for n in ('main', 'fabric', 'memory')]
        for t in targets:
            t['clock_domain'] = 'memory'
        for node in request['nodes'] + response['nodes']:
            node['clock_domain'] = 'fabric'
    # The network data width is the narrowest target unless stated: the
    # 64-bit network keeps a 32-bit target, so it names its width.
    transport = {'max_outstanding': 1, 'response_codes': 'distinct'}
    if NETWORK['bits'] != min(min(spec['bits'] for spec in TARGETS.values()), 64):
        transport = {'data_width': NETWORK['bits'], **transport}
    return endpoint_ports({'schema_version': 10, 'name': 'widths', 'address_width': 32,
            'arbitration': {'policy': 'round_robin'},
            'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': clocks,
            **({'synchronizer_stages': 3} if len(clocks) > 1 else {}),
            'initiators': [{'name': s, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                            **({'narrow_bursts': 'split_non_modifiable'}
                               if s in NETWORK['split'] else {}),
                            **({'narrow_bursts': 'merge'} if s in NETWORK['merge'] else {}),
                            **({'max_outstanding': NETWORK['outstanding'][s]}
                               if s in NETWORK['outstanding'] else {})}
                           for s, w in SOURCES.items()],
            'targets': targets,
            'transport': transport,
            'connectivity': {s: [t for r, t in ROUTES if r == s] for s in SOURCES},
            'topology': {'request': request, 'response': response},
            'output': {'backend': 'verilog', 'language': 'verilog_2001'}},
        id_width=3, len_width=NETWORK['len_bits'])


def case(name, source, write, address, beats, size, burst=1, keep=None, partner=None,
         error=None):
    # error: a target-local beat address answered with SLVERR.
    width = SOURCES[source] // 8
    c = {'name': name, 'source': source, 'write': write, 'address': address, 'beats': beats,
         'size': size, 'burst': burst, 'errors': [] if error is None else [[None, error]],
         'keep': (1 << width) - 1 if keep is None else keep}
    if partner:
        c['partner'] = partner
    return c


def pattern(nbytes):
    # The sparse keep mask: alternating byte pairs across the whole bus.
    return int('5a' * nbytes, 16) & ((1 << nbytes) - 1)


def wide_cases(mode):
    # 512- and 1024-bit endpoints: every route with a full-width INCR, a
    # narrow unaligned INCR, WRAP and FIXED, then widths that merge, clamp
    # and contend.
    out = []
    base = {t: spec['base'] for t, spec in TARGETS.items()}
    for w in (0, 1):
        d = 'w' if w else 'r'
        for s, bits in SOURCES.items():
            full = (bits // 8).bit_length() - 1
            for t in TARGETS:
                b = base[t]
                out += [case(f'{d}-{s}-{t}-incr', s, w, b + 0x40, 5, full),
                        case(f'{d}-{s}-{t}-narrow', s, w, b + 0x103, 6, 0),
                        case(f'{d}-{s}-{t}-wrap', s, w, b + 0x400 + (1 << full), 4, full, burst=2),
                        case(f'{d}-{s}-{t}-fixed', s, w, b + 0x800, 3, min(full, 2), burst=0)]
            out.append(case(f'{d}-{s}-sparse', s, w, base['t1024'] + 0xc00, 2, full,
                            keep=pattern(bits // 8)))
        # Narrow bursts into the wide targets, Non-modifiable and modifiable:
        # s32 merges only the modifiable ones, s256 (narrow_bursts merge)
        # merges both, and in wide_packets s512 splits the Non-modifiable ones.
        for s in ('s32', 's256', 's512'):
            for cache in (0, 2):
                n = f'{d}-{s}-c{cache}'
                out += [dict(case(f'{n}-t1024-narrow-half', s, w, base['t1024'] + 0x1022, 9, 1),
                             cache=cache),
                        dict(case(f'{n}-t1024-incr4', s, w, base['t1024'] + 0x1100, 4, 2),
                             cache=cache),
                        dict(case(f'{n}-t512-wrap8', s, w, base['t512'] + 0x1214, 8, 2, burst=2),
                             cache=cache),
                        dict(case(f'{n}-t1024-wrap16', s, w, base['t1024'] + 0x1308, 16, 2,
                                  burst=2), cache=cache),
                        dict(case(f'{n}-t512-slverr', s, w, base['t512'] + 0x1400, 32, 2,
                                  error=0x1440), cache=cache)]
        # A full-width 512-bit source into the 1024-bit target, and the wide
        # sources clamped to narrower targets: INCR at the 256-word limit and
        # beyond, WRAP of 16 target words and beyond, FIXED rejected, a target
        # error and a sparse beat.
        out += [case(f'{d}-s512-t1024-half', 's512', w, base['t1024'] + 0x1540, 6, 6),
                case(f'{d}-s512-t1024-wrap2', 's512', w, base['t1024'] + 0x1640, 2, 6, burst=2),
                case(f'{d}-s1024-t512-long', 's1024', w, base['t512'] + 0x2000, 32, 7),
                case(f'{d}-s1024-t256-wrap16', 's1024', w, base['t256'] + 0x2080, 4, 7, burst=2),
                case(f'{d}-s1024-t32-k256', 's1024', w, base['t32'] + 0x0, 8, 7),
                case(f'{d}-s1024-t32-k260', 's1024', w, base['t32'] + 0x0, 9, 7),
                case(f'{d}-s512-t32-wrap32', 's512', w, base['t32'] + 0x200, 2, 6, burst=2),
                case(f'{d}-s1024-t512-fixed-wide', 's1024', w, base['t512'] + 0x2400, 2, 7,
                     burst=0),
                case(f'{d}-s1024-t256-slverr', 's1024', w, base['t256'] + 0x2500, 2, 7,
                     error=0x2560),
                case(f'{d}-s1024-t512-sparse', 's1024', w, base['t512'] + 0x2604, 3, 7,
                     keep=pattern(128) >> 8 << 8)]
        # Cut pieces of 1024- and 512-bit bursts: WRAP2..16 of 128-byte
        # beats into the 32-bit target (two runs, each at most 256 words),
        # WRAP into the 256- and 512-bit targets, and a 4 KiB INCR with an
        # error in its third piece. Multi-beat FIXED into the 32-bit target,
        # modifiable or not, is never cut (SLVERR); a FIXED beat as wide as
        # the 512-bit target passes.
        for n in (2, 4, 8, 16):
            span = n * 128
            out.append(case(f'{d}-s1024-t32-wrap{n}-cut', 's1024', w,
                            base['t32'] + 0x800 // span * span + span // 2, n, 7, burst=2))
        out += [case(f'{d}-s512-t256-wrap4', 's512', w, base['t256'] + 0x2780, 4, 6, burst=2),
                case(f'{d}-s1024-t512-wrap16-cut', 's1024', w, base['t512'] + 0x2800 + 0x300,
                     16, 7, burst=2),
                case(f'{d}-s1024-t32-fixed3', 's1024', w, base['t32'] + 0x400, 3, 7, burst=0),
                dict(case(f'{d}-s512-t32-fixed2-sparse-c2', 's512', w, base['t32'] + 0x500, 2, 6,
                          burst=0, keep=pattern(64)), cache=2),
                case(f'{d}-s1024-t512-fixed-fit', 's1024', w, base['t512'] + 0x2900, 3, 6,
                     burst=0),
                case(f'{d}-s1024-t32-k1024-err', 's1024', w, base['t32'] + 0x0, 32, 7,
                     error=0x900)]
        # Unmapped start addresses: above every window, and in the hole
        # between the 32- and 256-bit targets.
        out += [case(f'{d}-s1024-unmapped', 's1024', w, 0x80000, 2, 7),
                case(f'{d}-s32-unmapped-hole', 's32', w, 0x3000, 3, 2)]
    if mode == 'wide_packets':
        # Bounded packets cut a wide INCR at the 4 KiB boundary and at the
        # target's word limit; an error in a later packet merges into one B.
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-p-s1024-t32-four', 's1024', w, base['t32'] + 0x0, 32, 7),
                    case(f'{d}-p-s1024-t32-two', 's1024', w, base['t32'] + 0x0, 16, 7),
                    case(f'{d}-p-s512-t32-err2', 's512', w, base['t32'] + 0x800, 32, 6,
                         error=0xc00),
                    dict(case(f'{d}-p-s512-t1024-fold', 's512', w, base['t1024'] + 0x5108, 4, 2,
                              burst=2), cache=2)]
    out += [case('contend-t1024', 's32', 1, base['t1024'] + 0x6000, 4, 2,
                 partner=case('', 's1024', 0, base['t1024'] + 0x6000, 2, 7)),
            case('contend-t512', 's1024', 1, base['t512'] + 0x6100, 2, 7,
                 partner=case('', 's512', 1, base['t512'] + 0x6400, 3, 6))]
    return out


def cases(mode):
    if mode in WIDE_MODES:
        return wide_cases(mode)
    out = []
    for w in (0, 1):
        d = 'w' if w else 'r'
        for s, bits in SOURCES.items():
            full = (bits // 8).bit_length() - 1
            for t in TARGETS:
                if (s, t) not in ROUTES:
                    continue
                b = TARGETS[t]['base']
                # Full-width INCR, a narrow unaligned INCR, WRAP and FIXED.
                out += [case(f'{d}-{s}-{t}-incr', s, w, b + 0x40, 5, full),
                        case(f'{d}-{s}-{t}-narrow', s, w, b + 0x103, 6, 0),
                        case(f'{d}-{s}-{t}-wrap', s, w, b + 0x200 + (1 << full), 4, full, burst=2),
                        case(f'{d}-{s}-{t}-fixed', s, w, b + 0x300, 3, min(full, 2), burst=0)]
            out.append(case(f'{d}-{s}-sparse', s, w, TARGETS['t256']['base'] + 0x400, 2, full,
                            keep=0x5a5a5a5a5a5a5a5a & ((1 << (bits // 8)) - 1)))
    if mode == 'packets':
        # Packets: 1 KiB to the 32-bit target, 4 KiB to wide ones,
        # always cut at 4 KiB; an error in a later packet merges into one B.
        t32, t128, t256 = (TARGETS[t]['base'] for t in ('t32', 't128', 't256'))
        for w in (0, 1):
            d = 'w' if w else 'r'
            # Packets count from the start word, not from 1 KiB boundaries.
            out += [case(f'{d}-p-s32-t32-from-start', 's32', w, t32 + 0x10, 256, 2),
                    case(f'{d}-p-s256-t32-two', 's256', w, t32 + 0x0, 64, 5),
                    case(f'{d}-p-s64-t32-two', 's64', w, t32 + 0x808, 255, 3),
                    case(f'{d}-p-s256-t32-err2', 's256', w, t32 + 0x0, 64, 5, error=0x404),
                    case(f'{d}-p-s128-t128-4k', 's128', w, t128 + 0x3000, 256, 4),
                    case(f'{d}-p-s32-t256-narrow', 's32', w, t256 + 0x3ff0, 8, 1),
                    # The alignment word is the 32-byte source word, not the
                    # target word; an error in the first packet still fails B.
                    case(f'{d}-p-s256-t32-unaligned', 's256', w, t32 + 0x10, 63, 5),
                    case(f'{d}-p-s256-t32-err1', 's256', w, t32 + 0x0, 64, 5, error=0x8)]
            # A narrow WRAP whose container fits the 16-byte source word
            # reaches the target as one INCR from the container base, of any
            # AxCACHE; a larger container keeps its WRAP.
            out += [dict(case(f'{d}-p-fold-t32', 's128', w, t32 + 0x508, 4, 2, burst=2), cache=2),
                    case(f'{d}-p-fold-t32-c0', 's128', w, t32 + 0x5e4, 4, 2, burst=2,
                         keep=0xff0f),
                    dict(case(f'{d}-p-fold-t32-half', 's128', w, t32 + 0x706, 4, 1, burst=2),
                         cache=3),
                    case(f'{d}-p-fold-t32-err', 's128', w, t32 + 0x808, 2, 3, burst=2,
                         error=0x80c),
                    dict(case(f'{d}-p-fold-t128', 's128', w, t128 + 0x508, 4, 2, burst=2),
                         cache=2),
                    case(f'{d}-p-fold-t256', 's128', w, t256 + 0x60a, 8, 1, burst=2),
                    case(f'{d}-p-wrap-big-t32', 's128', w, t32 + 0x914, 8, 2, burst=2),
                    case(f'{d}-p-x-len15', 's128', w, t32 + 0xb07, 16, 0, burst=2, keep=0xf7fe),
                    case(f'{d}-p-x-b2', 's128', w, t128 + 0xb21, 2, 0, burst=2),
                    case(f'{d}-p-x-h2', 's128', w, t256 + 0xb46, 2, 1, burst=2),
                    case(f'{d}-p-x-zero', 's128', w, t32 + 0xb88, 4, 2, burst=2, keep=0),
                    case(f'{d}-p-x-errbase', 's128', w, t32 + 0xc0c, 4, 2, burst=2, error=0xc00),
                    case(f'{d}-p-x-upper', 's128', w, t32 + 0xd14, 2, 2, burst=2),
                    case(f'{d}-p-x-d8', 's128', w, t32 + 0xd38, 2, 3, burst=2),
                    case(f'{d}-p-x-top', 's128', w, t32 + 0xe1c, 4, 2, burst=2),
                    # A modifiable narrow INCR stays one burst.
                    dict(case(f'{d}-p-narrow-merge', 's128', w, t32 + 0xa02, 6, 1), cache=2)]
        # Packets interleave between sources at packet boundaries: a
        # concurrent burst is served between the two packets of a long one.
        out.append(dict(case('contend-packets', 's256', 1, t32 + 0x0, 64, 5,
                             partner=dict(case('', 's32', 1, t32 + 0xc00, 4, 2), delay=60)),
                        interleave=True))
    if mode == 'attributes':
        # Every AxCACHE value to each target in both directions.
        for w in (0, 1):
            d = 'w' if w else 'r'
            for t in TARGETS:
                for cache in range(16):
                    out.append(dict(case(f'{d}-a-{t}-c{cache}', 's32', w,
                                         TARGETS[t]['base'] + 0x100 + 16 * cache, 2, 2),
                                    cache=cache, prot=cache * 3 & 7))
    if mode == 'net64':
        # 4-bit AxLEN: an INCR clamp reaches at most 16 target beats.
        t32, t128 = TARGETS['t32']['base'], TARGETS['t128']['base']
        for w in (0, 1):
            d = 'w' if w else 'r'
            out += [case(f'{d}-s64-t32-k16', 's64', w, t32 + 0x0, 8, 3),
                    case(f'{d}-s64-t32-k18', 's64', w, t32 + 0x100, 9, 3),
                    case(f'{d}-s64-t32-k15', 's64', w, t32 + 0x204, 8, 3),
                    case(f'{d}-s64-t32-wrap16', 's64', w, t32 + 0x840, 8, 3, burst=2),
                    case(f'{d}-s64-t32-fixed-wide', 's64', w, t32 + 0xa00, 2, 3, burst=0),
                    case(f'{d}-s64-t32-slverr', 's64', w, t32 + 0xb04, 4, 3, error=0xb0c),
                    case(f'{d}-s64-t128-long', 's64', w, t128 + 0x1000, 16, 3),
                    # 4-bit AxLEN pieces: a WRAP16 of 32 words in four, and an
                    # unaligned INCR cut at 16 words with a late error; a
                    # multi-beat FIXED of wider beats answers SLVERR.
                    case(f'{d}-s64-t32-wrap32-cut', 's64', w, t32 + 0xc40, 16, 3, burst=2),
                    case(f'{d}-s64-t32-fixed3', 's64', w, t32 + 0xd00, 3, 3, burst=0),
                    case(f'{d}-s64-t32-k31-err', 's64', w, t32 + 0xe04, 16, 3, error=0xe48)]
        return out
    # AxCACHE 0 and 2 pairs: every source merges both (s64 spells merge, the
    # split source s128 first splits the Non-modifiable narrow INCR per beat).
    # Narrow INCR, full-width INCR4, unaligned INCR1, narrow WRAP, and a failed
    # beat that fails only itself unless merged into a word.
    t256, t128 = TARGETS['t256']['base'], TARGETS['t128']['base']
    pair_sources = ['s32'] + sorted(NETWORK['merge'] | NETWORK['split'])
    for w in (0, 1):
        d = 'w' if w else 'r'
        for s in pair_sources:
            for cache in (0, 2):
                n = f'{d}-{s}-c{cache}'
                out += [dict(case(f'{n}-narrow', s, w, t128 + 0xe03, 6, 0), cache=cache),
                        dict(case(f'{n}-narrow-half', s, w, t256 + 0xe22, 5, 1), cache=cache),
                        dict(case(f'{n}-incr4', s, w, t256 + 0xe40, 4, 2), cache=cache),
                        dict(case(f'{n}-incr1-unaligned', s, w, t128 + 0xe86, 1, 2),
                             cache=cache),
                        dict(case(f'{n}-wrap', s, w, t256 + 0xec6, 4, 1, burst=2), cache=cache),
                        dict(case(f'{n}-slverr', s, w, t128 + 0xf00, 4, 2, error=0xf08),
                             cache=cache)]
        # A target-width single beat from the upper half of a word: merged,
        # it takes the smaller size covering its bytes; passed, it keeps AxSIZE.
        for cache in (0, 2):
            out.append(dict(case(f'{d}-s128-t128-c{cache}-full-mid', 's128', w, t128 + 0xf4a, 1,
                                 4), cache=cache))
    for w in (0, 1):
        d = 'w' if w else 'r'
        # Unaligned full-width first beats, narrow WRAP and target SLVERR.
        out += [case(f'{d}-s256-unaligned', 's256', w, t256 + 0x610, 3, 5),
                case(f'{d}-s128-unaligned', 's128', w, t256 + 0x708, 2, 4),
                case(f'{d}-s32-wrap-narrow', 's32', w, t256 + 0x904, 4, 2, burst=2),
                case(f'{d}-s64-wrap16', 's64', w, t128 + 0xa18, 16, 3, burst=2),
                # Merged beats reach the target as words: the error sits on a word.
                case(f'{d}-s64-slverr', 's64', w, t256 + 0xb00, 8, 3, error=0xb20),
                case(f'{d}-s32-slverr', 's32', w, t128 + 0xc00, 6, 2, error=0xc10)]
    # Clamp limits: target beat counts at 256 and 16, and the local rejections.
    t32, t128 = TARGETS['t32']['base'], TARGETS['t128']['base']
    for w in (0, 1):
        d = 'w' if w else 'r'
        out += [case(f'{d}-s128-t32-k256', 's128', w, t32 + 0x0, 64, 4),
                case(f'{d}-s128-t32-k260', 's128', w, t32 + 0x0, 65, 4),
                case(f'{d}-s128-t32-k255', 's128', w, t32 + 0x4, 64, 4),
                case(f'{d}-s256-t128-long', 's256', w, t128 + 0x1008, 40, 5),
                case(f'{d}-s64-t32-wrap16', 's64', w, t32 + 0x840, 8, 3, burst=2),
                case(f'{d}-s256-t32-wrap32', 's256', w, t32 + 0x900, 4, 5, burst=2),
                case(f'{d}-s256-t32-fixed-wide', 's256', w, t32 + 0xa00, 2, 3, burst=0),
                case(f'{d}-s256-t128-fixed-wide', 's256', w, t128 + 0xa00, 2, 5, burst=0),
                case(f'{d}-s256-t32-slverr', 's256', w, t32 + 0xb04, 2, 5, error=0xb24),
                case(f'{d}-s128-t32-sparse', 's128', w, t32 + 0xc06, 3, 4,
                     keep=0x0ff0)]
        # Cut pieces: WRAP2..16 of 32-byte beats from mid-container (16 words
        # stay one WRAP, more are cut at the wrap point and at 256 words), a
        # full 4 KiB INCR in four pieces with an error in its third piece,
        # and an unaligned INCR whose cut falls mid-beat. A multi-beat FIXED
        # of wider beats answers SLVERR whatever its AxCACHE; one as wide as
        # the 128-bit target passes.
        for n in (2, 4, 8, 16):
            span = n * 32
            out.append(case(f'{d}-s256-t32-wrap{n}-cut', 's256', w,
                            t32 + span + span // 2, n, 5,
                            burst=2))
        out += [case(f'{d}-s256-t32-fixed4', 's256', w, t32 + 0xa40, 4, 5, burst=0),
                dict(case(f'{d}-s256-t32-fixed4-c2', 's256', w, t32 + 0xa80, 4, 5, burst=0),
                     cache=2),
                case(f'{d}-s256-t128-fixed-fit', 's256', w, t128 + 0xb00, 3, 4, burst=0),
                case(f'{d}-s128-t32-k1024-err', 's128', w, t32 + 0x0, 256, 4, error=0x900),
                case(f'{d}-s256-t32-unaligned-cut', 's256', w, t32 + 0x14, 40, 5,
                     keep=0x0ff0ff0f),
                case(f'{d}-s128-t32-wrap16-err', 's128', w, t32 + 0xe80, 16, 4, burst=2,
                     error=0xe04)]
    out += [case('contend-t32', 's256', 1, TARGETS['t32']['base'] + 0xd00, 2, 5,
                 partner=case('', 's32', 0, TARGETS['t32']['base'] + 0xe00, 4, 2)),
            case('contend-256', 's32', 1, TARGETS['t256']['base'] + 0x800, 4, 2,
                 partner=case('', 's256', 0, TARGETS['t256']['base'] + 0x800, 2, 5)),
            case('contend-128', 's128', 1, TARGETS['t128']['base'] + 0x900, 3, 4,
                 partner=case('', 's64', 1, TARGETS['t128']['base'] + 0xa00, 3, 3))]
    return out


def beat_addresses(c):
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
        yield i, (c['address'] if i == 0 else start), start + step


def owner(address):
    return next((t for t, spec in TARGETS.items()
                 if spec['base'] <= address < spec['base'] + spec['size']), None)


def unmapped(c):
    # No window holds the start address: the default target adapter answers DECERR,
    # one B or every read beat with zero data, without a target bus access.
    return owner(c['address']) is None


def clamp_log(c):
    # log2 of the width a burst reaches its target at.
    return min(SOURCES[c['source']], TARGETS[owner(c['address'])]['bits']).bit_length() - 4


def rejected(c):
    # A FIXED burst wider than its target answers SLVERR without access.
    return c['burst'] == 0 and clamp_log(c) < c['size']


def clamp_beats(c):
    # Target words of a clamped INCR or WRAP burst in emission order: (source
    # beat, word address, first byte). WRAP words follow the wrap order; an
    # INCR first beat starts at the start word.
    assert c['burst'] != 0
    t = 1 << clamp_log(c)
    beats = list(beat_addresses(c))
    if c['burst'] == 2:
        return [(i, w, w) for i, lo, hi in beats for w in range(lo, hi, t)]
    return [(i, w, max(w, lo)) for i, lo, hi in beats for w in range(lo // t * t, hi, t)]


def cut_pieces(c, start, words):
    # Target bursts of clamped words: (AxADDR, word count, AxBURST). A WRAP of
    # 2, 4, 8 or 16 words stays one WRAP; otherwise a discontinuity starts a
    # new INCR piece and a piece holds at most 2^len_bits words. A piece that
    # begins at the start word carries the raw start address.
    t = 1 << clamp_log(c)
    if c['burst'] == 2 and len(words) in (2, 4, 8, 16):
        return [(start, len(words), 2)]
    runs = []
    for w in words:
        if runs and w == runs[-1][-1] + t:
            runs[-1].append(w)
        else:
            runs.append([w])
    limit, out = 1 << NETWORK['len_bits'], []
    for run in runs:
        for k in range(0, len(run), limit):
            chunk = run[k:k + limit]
            out.append((start if chunk[0] == start // t * t else chunk[0], len(chunk), 1))
    return out


def sub_bursts(c):
    # The bursts the clamp sees: one per bounded packet of an INCR, else c.
    if c['burst'] != 1:
        return [c]
    step = 1 << c['size']
    return [dict(c, address=lo, beats=(hi - lo // step * step + step - 1) // step)
            for lo, hi in packets(c)]


def piece_heads(c):
    # Word index -> logged address of each piece's first target beat.
    heads, n = {}, 0
    for sub in sub_bursts(c):
        words = [w for _, w, _ in clamp_beats(sub)]
        offset = 0
        for address, count, _ in cut_pieces(sub, sub['address'], words):
            heads[n + offset] = address
            offset += count
        n += len(words)
    return heads


def merges(c):
    # INCR goes through the target merge; WRAP when narrower than the target;
    # FIXED never.
    t = (TARGETS[owner(c['address'])]['bits'] // 8).bit_length() - 1
    return c['size'] <= t and (c['burst'] == 1 or (c['burst'] == 2 and c['size'] < t))


def target_bursts(c):
    # Merged target bursts: (start, AxLEN, AxSIZE, AxBURST, source beat offset)
    # per packet, from the merge rules (axi4_upsize_bench.plan).
    tbytes = TARGETS[owner(c['address'])]['bits'] // 8
    step = 1 << c['size']
    out, offset = [], 0
    for lo, hi in packets(c):
        beats = (hi - lo // step * step + step - 1) // step if c['burst'] == 1 else c['beats']
        sub = {'address': lo, 'size': c['size'], 'length': beats - 1, 'burst': c['burst']}
        _, start, length, size, burst = upsize_plan(sub, tbytes, c['write'])
        out.append((start, length, size, burst, offset, sub))
        offset += beats
    return out


def folds(c):
    # A narrow WRAP whose container fits its source's word.
    width = SOURCES[c['source']] // 8
    target = owner(c['address'])
    if not c['write'] and target is not None:
        width = min(width, TARGETS[target]['bits'] // 8)
    return c['burst'] == 2 and 1 << c['size'] < width and c['beats'] << c['size'] <= width


def fold_view(c):
    # The folded burst: one INCR beat of the container's size at its base;
    # each byte keeps the source beat that wrote it.
    span = c['beats'] << c['size']
    owner_of = {a: i for i, lo, hi in beat_addresses(c) for a in range(lo, hi)}
    return dict(c, address=c['address'] // span * span, beats=1, size=span.bit_length() - 1,
                burst=1, byte_beat=owner_of)


def source_wrap(source):
    # The largest WRAP container a source issues: max(min(64 B, 16 beats), 2 beats).
    width = SOURCES[source] // 8
    return max(min(64, 16 * width), 2 * width)


def wrap_limit(source, target):
    # The pair's WRAP limit (0: none): the largest power of two up to the
    # target's packet limit, 16 target words and the largest WRAP limit of the
    # sources reaching it, at most the pair's packet limit and above one word.
    tbytes, sbytes = TARGETS[target]['bits'] // 8, SOURCES[source] // 8
    limit = min(tbytes << NETWORK['len_bits'], 4096)
    upper = min(limit, 16 * tbytes, max(source_wrap(s) for s, t in ROUTES if t == target))
    path = min(1 << (upper.bit_length() - 1), max(limit, sbytes, tbytes)) if upper else 0
    return path if path > tbytes else 0


def wrap_runs(c):
    # A WRAP of a source that cuts WRAP bursts (a reached target's WRAP limit
    # is none or below the source's): one INCR from its container base, two
    # INCR runs (start to top, base to start) past the pair's WRAP limit
    # (and the source's), else None. Each byte keeps its source beat.
    if c['burst'] != 2 or unmapped(c) or folds(c) or 'run_offset' in c:
        return None
    source = c['source']
    if all(wrap_limit(source, t) >= source_wrap(source) for r, t in ROUTES if r == source):
        return None
    span, step = c['beats'] << c['size'], 1 << c['size']
    base = c['address'] // span * span
    limit = min(wrap_limit(source, owner(c['address'])) or 1, source_wrap(source))
    if c['address'] != base and span <= limit:
        return None
    owner_of = {a: i for i, lo, hi in beat_addresses(c) for a in range(lo, hi)}
    first = (base + span - c['address']) // step
    runs = [(c['address'], c['beats'], 0)] if c['address'] == base else \
        [(c['address'], first, 0), (base, c['beats'] - first, first)]
    return [dict(c, address=a, beats=n, burst=1, byte_beat=owner_of, run_offset=k)
            for a, n, k in runs]


def packets(c):
    # Packet ranges [start, end) of an INCR burst to one AXI target: the limit
    # min(2^len_bits target words, 4 KiB) from the alignment word
    # max(source, target bytes), cut at 4 KiB crossings; one range otherwise.
    target = owner(c['address'])
    tbytes, sbytes = TARGETS[target]['bits'] // 8, SOURCES[c['source']] // 8
    step = 1 << c['size']
    end = c['address'] // step * step + c['beats'] * step
    if c['burst'] != 1:
        return [(c['address'], end)]
    if (c['source'] in NETWORK['split'] and step < sbytes and not c.get('cache', 0) & 2):
        # A non-modifiable narrow INCR goes out one packet per beat.
        return [(lo, hi) for _, lo, hi in beat_addresses(c)]
    limit = min((1 << NETWORK['len_bits']) * tbytes, 4096)
    cross, align = 4096, max(sbytes, tbytes)
    out, start = [], c['address']
    while start < end:
        stop = min(start // align * align + limit, start // cross * cross + cross, end)
        out.append((start, stop))
        start = stop
    return out


def address_events(c):
    # Target AW/AR handshakes: (target, write, local AxADDR, AxLEN, AxSIZE, AxBURST).
    # An interleave case expects the partner between the first two packets.
    if 'partner' in c:
        own = address_events({k: v for k, v in c.items() if k not in ('partner', 'interleave')})
        other = address_events(c['partner'])
        return own[:1] + other + own[1:] if c.get('interleave') else sorted(own + other)
    if unmapped(c):
        return []
    if wrap_runs(c):
        return [e for run in wrap_runs(c) for e in address_events(run)]
    if folds(c):
        return address_events(fold_view(c))
    target = owner(c['address'])
    base = TARGETS[target]['base']
    k = clamp_log(c)
    # Sources drive AxPROT 3'b010 on writes and 3'b000 on reads.
    attrs = attribute_bits(target, c['write'], c.get('cache', 0),
                           c.get('prot', 2 if c['write'] else 0))
    events = []
    if k < c['size'] and rejected(c):
        return []
    if k < c['size']:
        for sub in sub_bursts(c):
            words = [w for _, w, _ in clamp_beats(sub)]
            events += [(target, c['write'], address - base, count - 1, k, kind, *attrs)
                       for address, count, kind in cut_pieces(sub, sub['address'], words)]
        return events
    for lo, hi in packets(c):
        if not merges(c):
            step = 1 << c['size']
            beats = (hi - lo // step * step + step - 1) // step if c['burst'] == 1 else c['beats']
            events.append((target, c['write'], lo - base, beats - 1, c['size'], c['burst'], *attrs))
    if k >= c['size'] and merges(c):
        events = [(target, c['write'], start - base, length, size, burst, *attrs)
                  for start, length, size, burst, _, _ in target_bursts(c)]
    return events
