"""AXI4-Lite sources with several transactions in flight: configurations and
the transaction bench.

An AXI4-Lite face has no AXI ID, so all transactions of one direction share
one ordering key: consecutive transactions to one target pipeline through
the network, a transaction to another destination waits after its address
handshake until every earlier one of its direction has completed, and the
responses return in issue order.

Modes:

  graph   explicit single-node graph with connectivity all: Lite sources s0
          (inherits transport.max_outstanding 4) and s1 (its own 2) reach
          AXI4 targets ta (slow, errors) and tb (fast), an APB4 target tp,
          an AXI4 target tt with a timeout and an AXI4-Lite target tl. A
          decode miss answers DECERR from the default target, as its own
          destination.
  routes  the same graph with a connectivity matrix that leaves out (s0, tp):
          the unconnected pair also answers DECERR from the default target.
  funnel  two sources and two targets without a topology (the generated
          one-switch network) or AXI IDs, with transport.max_outstanding 64.
  mixed   the graph with s1 a full AXI4 source keeping two single-beat
          bursts across reads and writes with AxIDs 0..3 next to the Lite source s0.
  wide64  a 64-bit network of AXI4 targets: the 64-bit Lite source s0 keeps
          four, the 64-bit read-only Lite source s1 two.
  perwidth
          graph with a 64-bit AXI4 target ta; both Lite sources and the other
          targets stay 32-bit. Narrow accesses alternate the target lanes.
  bounded the mixed sources with s1 at one shared burst slot and
          packets cut by default: the Lite source keeps four in flight
          (it bypasses the packet cutter), the full AXI4 source one.
  exclusive
          the mixed sources (s1 a full AXI4 source keeping two) with exclusive
          support at ta (network monitor) and tb (target mode, the target ID
          encodes the exclusive owner flags): the Lite face has no AxLOCK, so every
          access is a normal one (OKAY) and clears overlapping reservations.
  multi_target
          the graph with ta sharing four and tb two packet slots across reads
          and writes. Both Lite sources reach multi-transaction AXI4 outlets
          (source AxID 0). Target port IDs follow definition.target_ids in the map.
          The target models
          answer the youngest transaction of another target ID first, so a
          response overtakes an older one of the other source.
  clocked multi_target as a clocked graph: the sources and the request switch
          in domain main (10 ns), the targets and the response switch in
          domain far (14 ns, phase offset), so every transaction crosses two
          asynchronous FIFOs with its response tag, and the Lite sources
          reach multi-transaction outlets across them. The network overlap
          checks are left out.
  clocked_bounded
          bounded in the same two domains: a Lite source with four in flight
          next to a packetized AXI4 source across the crossings.

Independent checks of the bench (none reuses the generator's decode):

- Scoreboard: every R and B belongs to the oldest transaction of its source
  and direction still waiting (issue order); RRESP, BRESP and the read data
  are checked against a model of the windows, routes and target knobs.
- Ordering rule: when a target accepts a request (AR or AW handshake, APB
  setup phase), every earlier transaction of that source and direction bound
  to another destination must already be complete at the source face.
- Network overlap: a target adapter with one shared transaction slot
  serializes reads and writes. Source transactions can still overlap while
  travelling through the network. A target accept counts as overlapped when
  the source handshake of that transaction came while the previous transaction of that source to
  that target was still in flight at the source face (handshaken, its R or
  B not yet completed): two transactions of one source are in the network
  at once. The back-to-back episodes require every accept after the first to
  overlap. Separately the face accepts AR and AW ahead of earlier responses
  (its in-flight count exceeds one).
- Access once: every transaction that must reach a target arrives exactly
  once; misses, unlisted pairs and APB writes without strobes never arrive.
- Target side: word-aligned local address (unaligned Lite addresses are
  cleared), AxPROT, WSTRB and write data with unstrobed bytes zero.
- Source face: capacity bound, R and B stable while offered (AXI A3.2.1).

Responses are matched per AXI ID (always ID 0 on a Lite face, so a Lite
source answers strictly in issue order), and the ordering rule compares IDs.

Local address of every access: bit 11 the source, bits 10:4 the serial,
then a bus word and an unaligned offset below the bus width."""

import os

from bench_registered import registered
from endpoint_ports import endpoint_ports, port_slots

OK, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3
MAXC = 96
MISS = 0x70000

# name: kind, window base, window size, timeout cycles
GRAPH_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, None),
    'tb': ('axi4', 0x20000, 0x10000, None),
    'tl': ('axi4_lite', 0x80000, 0x1000, None),
    'tp': ('apb4', 0x30000, 0x1000, None),
    'tt': ('axi4', 0x40000, 0x10000, 1024),
}
FUNNEL_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, None),
    'tl': ('axi4_lite', 0x80000, 0x1000, None),
}
WIDE_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, None),
    'tb': ('axi4', 0x20000, 0x10000, None),
    'tt': ('axi4', 0x40000, 0x10000, 1024),
}
SOURCES = ('s0', 's1')
DENIED = {('s0', 'tp')}
MODES = ('graph', 'routes', 'funnel', 'mixed', 'wide64', 'bounded', 'exclusive', 'multi_target',
         'clocked', 'clocked_bounded', 'perwidth')
CLOCKED = ('clocked', 'clocked_bounded')
BOUNDED = ('bounded', 'clocked_bounded')
# name: protocol, own capacity (None inherits transport.max_outstanding), access
LITE_SOURCES = {'s0': ('axi4_lite', None, 'read_write'), 's1': ('axi4_lite', 2, 'read_write')}
MIXED_SOURCES = {'s0': ('axi4_lite', None, 'read_write'), 's1': ('axi4', 2, 'read_write')}
WIDE_SOURCES = {'s0': ('axi4_lite', None, 'read_write'), 's1': ('axi4_lite', 2, 'read_only')}
BOUNDED_SOURCES = {'s0': ('axi4_lite', None, 'read_write'), 's1': ('axi4', 1, 'read_write')}
# Per-mode target extras: exclusive modes and shared read/write capacities.
EXCLUSIVE = {'exclusive': {'ta': 'network', 'tb': 'target'}}
TARGET_CAPACITY = {'multi_target': {'ta': 4, 'tb': 2}, 'clocked': {'ta': 4, 'tb': 2}}


def targets(mode):
    return {'funnel': FUNNEL_TARGETS, 'wide64': WIDE_TARGETS}.get(mode, GRAPH_TARGETS)


def sources(mode):
    return {'mixed': MIXED_SOURCES, 'wide64': WIDE_SOURCES, 'bounded': BOUNDED_SOURCES,
            'clocked_bounded': BOUNDED_SOURCES, 'exclusive': MIXED_SOURCES}.get(mode, LITE_SOURCES)


def target_capacity(mode, target, single=False):
    return 1 if single else TARGET_CAPACITY.get(mode, {}).get(target, 1)


def width(mode):
    return 64 if mode == 'wide64' else 32


def global_outstanding(mode):
    return 64 if mode == 'funnel' else 4


def capacity(mode, source, single=False):
    if single:
        return 1
    own = sources(mode)[source][1]
    return own if own is not None else global_outstanding(mode)


def lite(mode, source):
    return sources(mode)[source][0] == 'axi4_lite'


def writes_allowed(mode, source):
    return sources(mode)[source][2] == 'read_write'


def target_clock(mode):
    return 'clk_far' if mode in CLOCKED else 'clk_main'


def configuration(mode, single=False):
    chosen = targets(mode)
    target_list = []
    for name, (kind, base, size, cycles) in chosen.items():
        entry = {'name': name, 'clock_domain': 'far' if mode in CLOCKED else 'main',
                 'protocol': kind,
                 'data_width': 64 if mode == 'perwidth' and name == 'ta' else width(mode),
                 'mappings': [{'base': hex(base), 'size': hex(size)}]}
        if cycles:
            entry['timeout'] = {'cycles': cycles, 'completion': True}
        if name in EXCLUSIVE.get(mode, {}):
            entry['exclusive'] = EXCLUSIVE[mode][name]
            entry['exclusive_id_flags'] = ['owner']
        if target_capacity(mode, name, single) > 1:
            entry['max_outstanding'] = target_capacity(mode, name, single)
        target_list.append(entry)
    initiators = []
    for name, (protocol, own, access) in sources(mode).items():
        entry = {'name': name, 'clock_domain': 'main', 'protocol': protocol,
                 'data_width': width(mode)}
        if own is not None:
            entry['max_outstanding'] = 1 if single else own
        if access != 'read_write':
            entry['access'] = access
        if mode in EXCLUSIVE:
            entry['user_flags'] = {'owner': len(initiators) % 2}
        if protocol == 'axi4_lite' and os.environ.get('NOCLOOM_LO_ID_TABLE'):
            entries = int(os.environ['NOCLOOM_LO_ID_TABLE'])
            assert entries in (1, 2), entries
            entry['id_table_entries'] = entries
        initiators.append(entry)
    transport = {'max_outstanding': 1 if single else global_outstanding(mode),
                 'transport_word_bytes': width(mode) // 8, 'response_codes': 'distinct'}
    if mode in EXCLUSIVE:
        transport['user_flags'] = ['owner']
    config = {'schema_version': 10, 'name': 'lite_outstanding', 'address_width': 32,
              'arbitration': {'policy': 'round_robin'},
              'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
              'initiators': initiators, 'targets': target_list, 'transport': transport,
              'connectivity': 'all',
              'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if mode != 'funnel':
        config['topology'] = {
            'request': {'nodes': [{'name': 'q0'}], 'links': [],
                        'entries': [{'endpoint': s, 'node': 'q0'} for s in SOURCES],
                        'exits': [{'endpoint': t, 'node': 'q0'} for t in chosen]},
            'response': {'nodes': [{'name': 'p0'}], 'links': [],
                         'entries': [{'endpoint': t, 'node': 'p0'} for t in chosen],
                         'exits': [{'endpoint': s, 'node': 'p0'} for s in SOURCES]}}
        if mode == 'routes':
            config['connectivity'] = {s: [t for t in chosen if (s, t) not in DENIED]
                                      for s in SOURCES}
    if mode in CLOCKED:
        config['clock_domains'] = [
            {'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': 10000},
            {'name': 'far', 'clock_port': 'clk_far', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': 14000}]
        config['synchronizer_stages'] = 3
        config['topology']['request']['nodes'][0]['clock_domain'] = 'main'
        config['topology']['response']['nodes'][0]['clock_domain'] = 'far'
    return endpoint_ports(config, id_width=0 if mode == 'funnel' else 2, len_width=8)


def _fill(text, **values):
    for key, value in values.items():
        text = text.replace('@' + key + '@', str(value))
    return text


def _widen(word, bits):
    return word if bits == 32 else ((~word & 0xffffffff) << 32) | word


def read_data(index, seen, bits):
    """The data a target model returns at the bus-aligned local address seen."""
    return _widen(((0xa0 | index) << 24) | (0x5a << 16) | (seen & 0xffff), bits)


def write_data(source, serial, bits):
    return _widen(((serial & 0xff) << 24) | ((0x3c ^ source) << 16) | ((~serial & 0xff) << 8) |
                  0xc3, bits)


class Cmd:
    """lock, local and resp: an exclusive access of a full AXI4 source at an
    explicit local offset with its expected response (untracked episodes)."""

    def __init__(self, write, target, strobe=None, offset=0, prot=0, lock=0, local=None,
                 resp=None):
        self.write, self.target, self.strobe, self.offset, self.prot = (write, target, strobe,
                                                                       offset, prot)
        self.lock, self.local, self.resp = lock, local, resp


class Model:
    """The expected destination, response, data and arrival of a transaction."""

    def __init__(self, mode):
        self.mode = mode
        self.bits = width(mode)
        self.targets = targets(mode)
        self.names = sorted(self.targets)
        self.index = {t: i for i, t in enumerate(self.names)}

    def place(self, source, serial, cmd, knobs):
        name = 's%d' % source
        bus = self.bits // 8
        word = (serial * 5) & (16 // bus - 1)
        # A full AXI4 source issues aligned single beats with AxIDs 0..3.
        offset = cmd.offset if lite(self.mode, name) else 0
        local = (source << 11) | ((serial & 0x7f) << 4) | (word * bus) | offset
        if cmd.local is not None:
            local = cmd.local
        seen = local & ~(bus - 1)
        strobe = (1 << bus) - 1 if cmd.strobe is None else cmd.strobe
        row = {'write': cmd.write, 'strobe': strobe, 'prot': cmd.prot,
               'id': 0 if lite(self.mode, name) else serial & 3,
               'wdata': write_data(source, serial, self.bits), 'lock': cmd.lock}
        # A decode miss goes to the source's default target (the reached
        # target with the smallest window, the lowest based), which answers
        # DECERR without a target access.
        reached = [t for t in self.names if not (self.mode == 'routes' and (name, t) in DENIED)]
        fallback = self.index[min(reached, key=lambda t: (self.targets[t][2], self.targets[t][1]))]
        if cmd.target is None:
            row['addr'] = MISS + local
            row['dest'], row['resp'], row['reach'], row['rdata'] = fallback, DECERR, 0, 0
            return row
        kind, base, _size, _cycles = self.targets[cmd.target]
        row['addr'] = base + local
        if self.mode == 'routes' and (name, cmd.target) in DENIED:
            row['dest'], row['resp'], row['reach'], row['rdata'] = fallback, DECERR, 0, 0
            return row
        index = self.index[cmd.target]
        row['dest'] = index
        row['rdata'] = read_data(index, seen, self.bits)
        error = knobs.get('err', {}).get(cmd.target)
        # A per-source error knob: every transaction of that source fails.
        if knobs.get('errsrc', {}).get(cmd.target) == source:
            error = source << 7 | serial
        hung = knobs.get('hang', {}).get(cmd.target)
        row['resp'] = OK
        if error is not None and error == (source << 7 | serial):
            row['resp'] = SLVERR
        # An APB4 write without strobes completes without an access.
        row['reach'] = 0 if (cmd.write and kind == 'apb4' and cmd.strobe == 0) else 1
        if hung:
            row['resp'], row['reach'] = SLVERR, 2
        if cmd.resp is not None:
            row['resp'] = cmd.resp
        return row


def _with_knobs(ep):
    """Knob text and the model view of the error and hang knobs."""
    text, view = [], {'err': {}, 'hang': {}, 'errsrc': {}}
    for target, value in ep.get('errsrc', {}).items():
        text.append(f'{target}_errsrc = {value};')
        view['errsrc'][target] = value
    for target, value in ep.get('err', {}).items():
        text.append(f'{target}_err = {value};')
        view['err'][target] = value
    for target, value in ep.get('hang', {}).items():
        text.append(f'{target}_hang = {value};')
        view['hang'][target] = value
    return text + ep.get('knobs', []), view


def episodes(mode):
    import random
    rng = random.Random(20261001)
    m = Model(mode)
    names = m.names
    funnel = mode == 'funnel'
    slow = 'ta'
    fast = 'tl' if funnel else 'tb'
    full = (1 << (m.bits // 8)) - 1
    dedicated = [t for t in names if m.targets[t][0] != 'axi4']
    eps = []

    def delay(target, which, value):
        # AXI4 models have separate R and B latencies, dedicated ones one delay.
        return (f'{target}_{which} = {value};' if m.targets[target][0] == 'axi4'
                else f'{target}_delay = {value};')

    def reads(items):
        return [Cmd(False, *item) if isinstance(item, tuple) else Cmd(False, item) for item in items]

    def writes(items):
        return [Cmd(True, *item) if isinstance(item, tuple) else Cmd(True, item) for item in items]

    n0 = capacity(mode, 's0')
    # 1. Back to back to one target: every request after the first is
    # already in the network when the target answers the previous one, and
    # the face fills past one.
    for target, lat in ((fast, 6), (slow, 6)):
        lat_knobs = ([f'{target}_rlat = {lat};', f'{target}_blat = {lat};']
                     if m.targets[target][0] == 'axi4' else [f'{target}_delay = {lat};'])
        eps.append({'name': f'back_to_back_reads_{target}', 'measure': True, 'knobs': lat_knobs,
                    'cmds': {('s0', 'rd'): reads([target] * 24)},
                    'checks': [('s0_rmax >= 2', 'S0_READS_PAST_ONE'),
                               ('s0_rpipe == 23', 'READS_OVERLAP_IN_NETWORK')]})
        eps.append({'name': f'back_to_back_writes_{target}', 'measure': True, 'knobs': lat_knobs,
                    'cmds': {('s0', 'wr'): writes([target] * 24)},
                    'checks': [('s0_wmax >= 2', 'S0_WRITES_PAST_ONE'),
                               ('s0_wpipe == 23', 'WRITES_OVERLAP_IN_NETWORK')]})
    # W ahead of AW and AW ahead of W (Lite accepts them independently).
    eps.append({'name': 'writes_w_lead', 'knobs': ['s0_awlag = 3;', delay(fast, 'blat', 2)],
                'cmds': {('s0', 'wr'): writes([fast] * 10)}})
    eps.append({'name': 'writes_aw_lead', 'knobs': ['s0_wlag = 3;', delay(fast, 'blat', 2)],
                'cmds': {('s0', 'wr'): writes([fast] * 10)}})
    # 2. Switching targets: a transaction to another target waits until the
    # earlier ones complete (the ordering rule monitor), responses in order.
    switch = [slow, slow, fast, fast, slow, fast, slow, slow, fast]
    eps.append({'name': 'switch_reads', 'measure': True,
                'knobs': [f'{slow}_rlat = 20;'],
                'cmds': {('s0', 'rd'): reads(switch)}})
    eps.append({'name': 'switch_writes', 'measure': True,
                'knobs': [f'{slow}_blat = 20;'],
                'cmds': {('s0', 'wr'): writes(switch)}})
    # Every dedicated target in turn, both directions.
    tour = [slow] + dedicated + [fast] + dedicated[::-1] + [slow]
    eps.append({'name': 'dedicated_tour',
                'knobs': [f'{t}_delay = 2;' for t in dedicated] + [f'{slow}_rlat = 9;',
                                                                     f'{slow}_blat = 9;'],
                'cmds': {('s0', 'rd'): reads(tour), ('s0', 'wr'): writes(tour),
                         ('s1', 'rd'): reads(tour[::-1]), ('s1', 'wr'): writes(tour[::-1])}})
    # 3. Error responses: target SLVERR (by serial), decode misses and, with
    # routes, the unlisted pair, between network transactions.
    errs = [slow, slow, None, slow, fast, None, None, slow, fast, slow]
    if 'tp' in names:
        errs += ['tp', 'tp', slow]
    eps.append({'name': 'errors_in_order', 'err': {slow: (0 << 7) | 3},
                'knobs': [f'{slow}_rlat = 7;', f'{slow}_blat = 7;'],
                'cmds': {('s0', 'rd'): reads(errs), ('s0', 'wr'): writes(errs)}})
    if 'tl' in names:
        eps.append({'name': 'lite_target_errors', 'err': {'tl': (1 << 7) | 1},
                    'cmds': {('s1', 'rd'): reads(['tl', 'tl', slow, 'tl']),
                             ('s1', 'wr'): writes(['tl', 'tl', slow, 'tl'])}})
    if 'tp' in names:
        eps.append({'name': 'apb_target_errors', 'err': {'tp': (1 << 7) | 2},
                    'knobs': ['tp_delay = 1;'],
                    'cmds': {('s1', 'rd'): reads(['tp', 'tp', 'tp', fast]),
                             ('s1', 'wr'): writes(['tp', 'tp', 'tp', fast])}})
    # 4. Unaligned addresses, partial and zero strobes, every AxPROT.
    shaped = []
    for k in range(16):
        target = [slow, fast] + dedicated
        shaped.append((target[k % len(target)], k if k % 5 else 0, k & 3, k & 7))
    eps.append({'name': 'unaligned_strobes_prot',
                'cmds': {('s0', 'wr'): writes(shaped),
                         ('s0', 'rd'): reads([(t, full, o, p) for t, _s, o, p in shaped])}})
    # 5. Both sources, random destinations, response backpressure.
    choices = names + [None]
    rand = {}
    for s in (0, 1):
        for direction, write in (('rd', False), ('wr', True)):
            items = [(rng.choice(choices), rng.randint(0, 15), rng.randint(0, 3), rng.randint(0, 7))
                     for _ in range(28)]
            rand[('s%d' % s, direction)] = [Cmd(write, *item) for item in items]
    eps.append({'name': 'two_sources_random', 'limit': 60000,
                'knobs': [f'{t}_rlat = {rng.randint(0, 9)}; {t}_blat = {rng.randint(0, 9)};'
                          for t in names if m.targets[t][0] == 'axi4'] +
                         [f'{t}_delay = {rng.randint(0, 3)};' for t in dedicated] +
                         ['s0_rgap = 3;', 's0_bgap = 2;', 's1_rgap = 2;', 's1_bgap = 3;',
                          's0_awlag = 1;', 's1_wlag = 2;'],
                'cmds': rand})
    eps.append({'name': 'response_hold', 'knobs': ['s0_rhold = 30;', 's0_bhold = 30;',
                                                   delay(fast, 'blat', 1)],
                'cmds': {('s0', 'rd'): reads([fast, fast, None, fast]),
                         ('s0', 'wr'): writes([fast, fast, None, fast])}})
    # The face fills to the capacity (s1 keeps two) while responses are held, and accepts no more
    # than the bursts its AW and AR port pipes hold besides (s1_nar, s1_naw): a full AXI4 source
    # keeps a burst's AW or AR in its pipe until the network takes the burst, so the pipe holds a
    # further burst only once the network has taken the one before. A burst whose last response
    # waits at the face has given its context back, so one more may be open.
    cap1 = capacity(mode, 's1')
    eps.append({'name': 'capacity_under_hold',
                'knobs': ['s1_rhold = 60;', 's1_bhold = 60;', delay(fast, 'rlat', 1),
                          delay(fast, 'blat', 1)],
                'cmds': {('s1', 'rd'): reads([fast] * 6), ('s1', 'wr'): writes([fast] * 6)},
                'checks': [(f's1_rmax >= {cap1} && s1_rmax <= {cap1} + s1_nar + 1' +
                            (f' && s1_wmax >= {cap1} && s1_wmax <= {cap1} + s1_naw + 1'
                             if writes_allowed(mode, 's1') else ''), 'S1_CAPACITY_REACHED')]})
    if mode == 'exclusive':
        # Lite writes reach the network monitor at ta like any write: the
        # full AXI4 source s1 holds a reservation on X (AxID 0); a Lite
        # write to X clears it, so the exclusive write fails (OKAY); a Lite
        # write to another word W leaves the reservation on Y (AxID 1), so
        # that exclusive write succeeds (EXOKAY). The steps run in phases,
        # one after another without a reset; target-side bookkeeping is off
        # (both sources address the same words).
        x, y, w = 0x7f0, 0x7e0, 0x7d0
        eps.append({'name': 'lite_writes_clear_reservations', 'knobs': ['track = 0;'],
                    'cmds': {('s1', 'rd'): [Cmd(False, 'ta', lock=1, local=x, resp=EXOKAY),
                                            Cmd(False, 'ta', lock=1, local=y, resp=EXOKAY)],
                             ('s1', 'wr'): [Cmd(True, 'ta', lock=1, local=x, resp=OK),
                                            Cmd(True, 'ta', lock=1, local=y, resp=EXOKAY)],
                             ('s0', 'wr'): [Cmd(True, 'ta', local=x), Cmd(True, 'ta', local=w)]},
                    'phases': [{('s1', 'rd'): 1}, {('s0', 'wr'): 1}, {('s1', 'wr'): 1},
                               {('s1', 'rd'): 1}, {('s0', 'wr'): 1}, {('s1', 'wr'): 1}]})
    if mode in TARGET_CAPACITY:
        # Multi-transaction outlets: one Lite source fills ta (four) with
        # transactions of its single ordering key; both sources share ta and
        # tb while the models answer the other source's younger transaction
        # first, so the tag must steer every response to its own source.
        # Reads and writes share the source capacity, so each direction
        # fills ta on its own.
        eps.append({'name': 'lite_fills_target_reads', 'target_maxima': ('ta', 'tb'),
                    'knobs': ['ta_rlat = 8;', 'ta_blat = 8;'],
                    'cmds': {('s0', 'rd'): reads(['ta'] * 12)},
                    'checks': [('ta_rmax == 4', 'TA_FILLED_BY_ONE_SOURCE')]})
        eps.append({'name': 'lite_fills_target_writes', 'target_maxima': ('ta', 'tb'),
                    'knobs': ['ta_rlat = 8;', 'ta_blat = 8;'],
                    'cmds': {('s0', 'wr'): writes(['ta'] * 12)},
                    'checks': [('ta_wmax == 4', 'TA_FILLED_BY_ONE_SOURCE')]})
        eps.append({'name': 'lite_fills_target_pooled', 'target_maxima': ('ta', 'tb'),
                    'knobs': ['ta_rlat = 8;', 'ta_blat = 8;'],
                    'cmds': {('s0', 'rd'): reads(['ta'] * 12), ('s0', 'wr'): writes(['ta'] * 12)},
                    # Two table entries share the pool; one entry excludes that overlap.
                    'checks': [(f'ta_rmax + ta_wmax {"<" if os.environ.get("NOCLOOM_LO_ID_TABLE") == "1" else ">="} 4',
                                'TA_POOL_FILLED')]})
        eps.append({'name': 'two_sources_reordered', 'target_maxima': ('ta', 'tb'),
                    'knobs': ['ta_rlat = 8;', 'ta_blat = 8;', 'ta_reorder = 1;', 'tb_rlat = 3;',
                              'tb_blat = 3;', 'tb_reorder = 1;'],
                    'cmds': {('s0', 'rd'): reads(['ta'] * 8 + ['tb'] * 6),
                             ('s1', 'rd'): reads(['ta'] * 6 + ['tb'] * 6),
                             ('s0', 'wr'): writes(['tb'] * 6 + ['ta'] * 8),
                             ('s1', 'wr'): writes(['tb'] * 6 + ['ta'] * 6)},
                    # The shared pool admits in arrival order; writes pile to
                    # their entries while reads slip in one at a time.
                    'checks': [('ta_rmax >= 1 && ta_wmax >= 2', 'TA_SHARED'),
                               ('tb_rmax + tb_wmax >= 2', 'TB_FILLED')]})
        # Every transaction of s1 fails at ta while the model answers the
        # younger source first: a B or R completed for the wrong source
        # carries the wrong response.
        eps.append({'name': 'source_errors_reordered', 'target_maxima': ('ta', 'tb'),
                    'errsrc': {'ta': 1},
                    'knobs': ['ta_rlat = 6;', 'ta_blat = 6;', 'ta_reorder = 1;'],
                    'cmds': {('s0', 'wr'): writes(['ta'] * 10), ('s1', 'wr'): writes(['ta'] * 8),
                             ('s0', 'rd'): reads(['ta'] * 8), ('s1', 'rd'): reads(['ta'] * 6)},
                    'checks': [('ta_wmax >= 2', 'TA_WRITES_SHARED')]})
        eps.append({'name': 'target_errors_reordered', 'target_maxima': ('ta', 'tb'), 'err': {'ta': (1 << 7) | 2},
                    'knobs': ['ta_rlat = 4;', 'ta_blat = 4;', 'ta_reorder = 1;'],
                    'cmds': {('s0', 'rd'): reads(['ta'] * 6), ('s1', 'rd'): reads(['ta'] * 5),
                             ('s0', 'wr'): writes(['ta'] * 6), ('s1', 'wr'): writes(['ta'] * 5)}})
    # 6. A hung timed target: its queued transactions answer SLVERR in order,
    # later transactions to another target wait for them.
    if 'tt' in names:
        eps.append({'name': 'timeout_reads', 'hang': {'tt': 1},
                    'cmds': {('s0', 'rd'): reads(['tt', 'tt', 'tt', fast])},
                    'checks': [('nl_t_tt_timed_out === 1', 'TIMED_OUT')]})
        eps.append({'name': 'timeout_writes', 'hang': {'tt': 2},
                    'cmds': {('s0', 'wr'): writes(['tt', 'tt', fast, 'tt'])},
                    'checks': [('nl_t_tt_timed_out === 1', 'TIMED_OUT')]})
    # 7. Common reset with transactions in flight, then normal service. The
    # read and write packets of s0 share one request path to the slow target,
    # whose outlet shares one transaction slot across reads and writes: a second
    # packet of either direction waits on that path and the other direction queues
    # behind it. The target therefore answers quickly and s0 holds its R and B,
    # so both directions fill in the network before the reset.
    eps.append({'name': 'reset_mid_flight', 'reset_after': 40,
                'knobs': [f'{slow}_rlat = 4;', f'{slow}_blat = 4;', 's0_rhold = 60;',
                          's0_bhold = 60;'],
                'cmds': {('s0', 'rd'): reads([slow] * 8), ('s0', 'wr'): writes([slow] * 8),
                         ('s1', 'rd'): reads([slow, fast] * 3)},
                'checks': [('s0_rmax >= 2 && s0_wmax >= 2', 'IN_FLIGHT_BEFORE_RESET')]})
    eps.append({'name': 'after_reset',
                'cmds': {('s0', 'rd'): reads([slow, fast, slow]),
                         ('s0', 'wr'): writes([fast, slow, fast]),
                         ('s1', 'wr'): writes([slow, slow])}})
    if funnel and n0 == 64:
        # Many in flight: with RREADY held low the responses queue in the
        # network and the face keeps accepting reads behind them.
        eps.append({'name': 'deep_reads', 'knobs': [f'{slow}_rlat = 2;', 's0_rhold = 300;'],
                    'cmds': {('s0', 'rd'): reads([slow] * 80)},
                    'checks': [('s0_rmax >= 3', 'S0_DEEP')]})
    if mode in CLOCKED:
        # The network overlap checks are left out: keep every other check.
        for ep in eps:
            ep['checks'] = [c for c in ep.get('checks', []) if 'pipe' not in c[0]]
    for ep in eps:
        text, view = _with_knobs(ep)
        ep['knob_text'] = text
        ep['rows'] = {}
        # A read-only source has no write face.
        ep['cmds'] = {key: cmds for key, cmds in ep['cmds'].items()
                      if key[1] == 'rd' or writes_allowed(mode, key[0])}
        for (source, direction), cmds in ep['cmds'].items():
            si = SOURCES.index(source)
            ep['rows'][(source, direction)] = [m.place(si, k, c, view) for k, c in enumerate(cmds)]
    return eps


def measured(eps):
    return [ep for ep in eps if ep.get('measure')]


# AXI4 bus model with one transaction per direction at a time. The adapter
# shares one slot across both directions. @T@_rlat/@T@_blat delay
# R and B; @T@_err names the transaction (source * 128 + serial) answering
# SLVERR; @T@_hang 1 holds RVALID low, 2 holds BVALID low.
_AXI = '''
integer @T@_rlat = 0, @T@_blat = 0, @T@_hang = 0, @T@_err = -1, @T@_rwait, @T@_bwait;
reg @T@_rbusy, @T@_rv, @T@_aw, @T@_w, @T@_bv; reg [31:0] @T@_ra, @T@_wa;
reg [@IW@-1:0] @T@_ri, @T@_wi;
assign nl_t_@T@_arready = !@T@_rbusy;
assign nl_t_@T@_rvalid = @T@_rv && @T@_hang != 1;
assign nl_t_@T@_rdata = @RDATA@;
assign nl_t_@T@_rresp = serial_of(@T@_ra) == @T@_err ? 2'b10 : 2'b00;
assign nl_t_@T@_rlast = 1'b1;
assign nl_t_@T@_rid = @T@_ri;
assign nl_t_@T@_awready = !@T@_aw;
assign nl_t_@T@_wready = @T@_aw && !@T@_w;
assign nl_t_@T@_bvalid = @T@_bv && @T@_hang != 2;
assign nl_t_@T@_bresp = serial_of(@T@_wa) == @T@_err ? 2'b10 : 2'b00;
assign nl_t_@T@_bid = @T@_wi;
always @(posedge @CLK@) begin
 if (!reset_n) begin
  @T@_rbusy = 0; @T@_rv = 0; @T@_aw = 0; @T@_w = 0; @T@_bv = 0;
 end else begin
  if (nl_t_@T@_rvalid && nl_t_@T@_rready) begin @T@_rbusy = 0; @T@_rv = 0; end
  else if (@T@_rbusy && !@T@_rv) begin
   if (@T@_rwait == 0) @T@_rv = 1; else @T@_rwait = @T@_rwait - 1;
  end
  if (nl_t_@T@_bvalid && nl_t_@T@_bready) begin @T@_aw = 0; @T@_w = 0; @T@_bv = 0; end
  else if (@T@_w && !@T@_bv) begin
   if (@T@_bwait == 0) @T@_bv = 1; else @T@_bwait = @T@_bwait - 1;
  end
  if (nl_t_@T@_wvalid && nl_t_@T@_wready) begin
   check(nl_t_@T@_wlast === 1'b1, "@U@_WLAST");
   @WBEAT@
   @T@_w = 1; @T@_bwait = @T@_blat;
  end
  if (nl_t_@T@_arvalid && nl_t_@T@_arready) begin
   check(nl_t_@T@_arlen === 0 && nl_t_@T@_arsize === @SIZE@ && nl_t_@T@_arburst === 2'd1 &&
         nl_t_@T@_arlock === 0 && nl_t_@T@_arcache === 0, "@U@_AR_SHAPE");
   arrive(0, @IDX@, nl_t_@T@_araddr, nl_t_@T@_arprot);
   @T@_rbusy = 1; @T@_ra = nl_t_@T@_araddr; @T@_ri = nl_t_@T@_arid; @T@_rwait = @T@_rlat;
  end
  if (nl_t_@T@_awvalid && nl_t_@T@_awready) begin
   check(nl_t_@T@_awlen === 0 && nl_t_@T@_awsize === @SIZE@ && nl_t_@T@_awburst === 2'd1 &&
         nl_t_@T@_awlock === 0 && nl_t_@T@_awcache === 0, "@U@_AW_SHAPE");
   arrive(1, @IDX@, nl_t_@T@_awaddr, nl_t_@T@_awprot);
   @T@_aw = 1; @T@_wa = nl_t_@T@_awaddr; @T@_wi = nl_t_@T@_awid;
  end
 end
end
'''

# AXI4 bus model with sixteen queue entries per direction. @CAP@ is the
# adapter's shared read/write capacity. This model checks each direction's
# occupancy against @CAP@.
# Reads and writes are answered one at a time after @T@_rlat/@T@_blat
# cycles; with @T@_reorder the model answers the youngest queued transaction
# whose target ID has no older one queued (legal AXI reordering between
# IDs: here between the sources). @T@_rmax/@T@_wmax record the transactions
# in flight at the port.
_AXI_MULTI = '''
integer @T@_rlat = 0, @T@_blat = 0, @T@_hang = 0, @T@_err = -1, @T@_reorder = 0, @T@_rmax = 0,
        @T@_errsrc = -1,
        @T@_wmax = 0, @T@_rn, @T@_rcur, @T@_rwait, @T@_an, @T@_wq, @T@_bcur, @T@_bwait, @T@_k, @T@_j,
        @T@_pick, @T@_ok;
reg [31:0] @T@_ra [0:16]; reg [@IW@-1:0] @T@_ri [0:16];
reg [31:0] @T@_wa [0:16]; reg [@IW@-1:0] @T@_wi [0:16];
assign nl_t_@T@_arready = @T@_rn < 16;
assign nl_t_@T@_rvalid = @T@_rcur >= 0 && @T@_rwait == 0 && @T@_hang != 1;
assign nl_t_@T@_rdata = data_of(@IDX@, @T@_rcur >= 0 ? @T@_ra[@T@_rcur] : 0);
assign nl_t_@T@_rresp = @T@_rcur >= 0 && (serial_of(@T@_ra[@T@_rcur]) == @T@_err ||
                        @T@_ra[@T@_rcur][11] == @T@_errsrc) ? 2'b10 : 2'b00;
assign nl_t_@T@_rlast = 1'b1;
assign nl_t_@T@_rid = @T@_rcur >= 0 ? @T@_ri[@T@_rcur] : 0;
assign nl_t_@T@_awready = @T@_an < 16;
assign nl_t_@T@_wready = @T@_wq < @T@_an;
assign nl_t_@T@_bvalid = @T@_bcur >= 0 && @T@_bwait == 0 && @T@_hang != 2;
assign nl_t_@T@_bresp = @T@_bcur >= 0 && (serial_of(@T@_wa[@T@_bcur]) == @T@_err ||
                        @T@_wa[@T@_bcur][11] == @T@_errsrc) ? 2'b10 : 2'b00;
assign nl_t_@T@_bid = @T@_bcur >= 0 ? @T@_wi[@T@_bcur] : 0;
reg @T@_arhs, @T@_rhs, @T@_awhs, @T@_whs, @T@_bhs;
always @(posedge @CLK@) begin
 // Handshakes as the port saw them at this edge, before any update.
 @T@_arhs = nl_t_@T@_arvalid && nl_t_@T@_arready; @T@_rhs = nl_t_@T@_rvalid && nl_t_@T@_rready;
 @T@_awhs = nl_t_@T@_awvalid && nl_t_@T@_awready; @T@_whs = nl_t_@T@_wvalid && nl_t_@T@_wready;
 @T@_bhs = nl_t_@T@_bvalid && nl_t_@T@_bready;
 if (!reset_n) begin
  @T@_rn = 0; @T@_rcur = -1; @T@_rwait = 0; @T@_an = 0; @T@_wq = 0; @T@_bcur = -1; @T@_bwait = 0;
 end else begin
  if (@T@_arhs) check(@T@_rn < @CAP@, "@U@_READ_CAPACITY");
  if (@T@_awhs) check(@T@_an < @CAP@, "@U@_WRITE_CAPACITY");
  if (@T@_rhs) begin
   for (@T@_k = @T@_rcur; @T@_k < 16; @T@_k = @T@_k + 1) begin
    @T@_ra[@T@_k] = @T@_ra[@T@_k + 1]; @T@_ri[@T@_k] = @T@_ri[@T@_k + 1];
   end
   @T@_rn = @T@_rn - 1; @T@_rcur = -1;
  end else if (@T@_rcur >= 0 && @T@_rwait > 0) @T@_rwait = @T@_rwait - 1;
  if (@T@_bhs) begin
   for (@T@_k = @T@_bcur; @T@_k < 16; @T@_k = @T@_k + 1) begin
    @T@_wa[@T@_k] = @T@_wa[@T@_k + 1]; @T@_wi[@T@_k] = @T@_wi[@T@_k + 1];
   end
   @T@_an = @T@_an - 1; @T@_wq = @T@_wq - 1; @T@_bcur = -1;
  end else if (@T@_bcur >= 0 && @T@_bwait > 0) @T@_bwait = @T@_bwait - 1;
  if (@T@_whs) begin
   check(nl_t_@T@_wlast === 1'b1, "@U@_WLAST");
   wbeat(@T@_wa[@T@_wq], nl_t_@T@_wdata, nl_t_@T@_wstrb);
   @T@_wq = @T@_wq + 1;
  end
  if (@T@_arhs) begin
   check(nl_t_@T@_arlen === 0 && nl_t_@T@_arsize === @SIZE@ && nl_t_@T@_arburst === 2'd1 &&
         nl_t_@T@_arlock === 0 && nl_t_@T@_arcache === 0, "@U@_AR_SHAPE");
   arrive(0, @IDX@, nl_t_@T@_araddr, nl_t_@T@_arprot);
   @T@_ra[@T@_rn] = nl_t_@T@_araddr; @T@_ri[@T@_rn] = nl_t_@T@_arid; @T@_rn = @T@_rn + 1;
  end
  if (@T@_awhs) begin
   check(nl_t_@T@_awlen === 0 && nl_t_@T@_awsize === @SIZE@ && nl_t_@T@_awburst === 2'd1 &&
         nl_t_@T@_awlock === 0 && nl_t_@T@_awcache === 0, "@U@_AW_SHAPE");
   arrive(1, @IDX@, nl_t_@T@_awaddr, nl_t_@T@_awprot);
   @T@_wa[@T@_an] = nl_t_@T@_awaddr; @T@_wi[@T@_an] = nl_t_@T@_awid; @T@_an = @T@_an + 1;
  end
  // The next read and B: the oldest, or with reorder the youngest whose ID
  // has no older one queued (B only once its W beat arrived).
  if (@T@_rcur < 0 && @T@_rn > 0) begin
   @T@_pick = 0;
   if (@T@_reorder)
    for (@T@_k = 0; @T@_k < @T@_rn; @T@_k = @T@_k + 1) begin
     @T@_ok = 1;
     for (@T@_j = 0; @T@_j < @T@_k; @T@_j = @T@_j + 1) if (@T@_ri[@T@_j] == @T@_ri[@T@_k]) @T@_ok = 0;
     if (@T@_ok) @T@_pick = @T@_k;
    end
   @T@_rcur = @T@_pick; @T@_rwait = @T@_rlat;
  end
  if (@T@_bcur < 0) begin
   @T@_pick = -1;
   for (@T@_k = 0; @T@_k < @T@_wq; @T@_k = @T@_k + 1) begin
    @T@_ok = 1;
    for (@T@_j = 0; @T@_j < @T@_k; @T@_j = @T@_j + 1) if (@T@_wi[@T@_j] == @T@_wi[@T@_k]) @T@_ok = 0;
    if (@T@_ok && (@T@_pick < 0 || @T@_reorder)) @T@_pick = @T@_k;
   end
   if (@T@_pick >= 0) begin @T@_bcur = @T@_pick; @T@_bwait = @T@_blat; end
  end
  if (@T@_rn > @T@_rmax) @T@_rmax = @T@_rn;
  if (@T@_an > @T@_wmax) @T@_wmax = @T@_an;
 end
end
'''

# AXI4-Lite target model: AW then W, B after @T@_delay cycles; R after
# @T@_delay cycles.
_LITE = '''
integer @T@_delay = 0, @T@_hang = 0, @T@_err = -1, @T@_rwait, @T@_bwait;
reg @T@_rbusy, @T@_rv, @T@_aw, @T@_w, @T@_bv; reg [31:0] @T@_ra, @T@_wa;
assign nl_t_@T@_arready = !@T@_rbusy;
assign nl_t_@T@_rvalid = @T@_rv;
assign nl_t_@T@_rdata = data_of(@IDX@, @T@_ra);
assign nl_t_@T@_rresp = serial_of(@T@_ra) == @T@_err ? 2'b10 : 2'b00;
assign nl_t_@T@_awready = !@T@_aw;
assign nl_t_@T@_wready = @T@_aw && !@T@_w;
assign nl_t_@T@_bvalid = @T@_bv;
assign nl_t_@T@_bresp = serial_of(@T@_wa) == @T@_err ? 2'b10 : 2'b00;
always @(posedge @CLK@) begin
 if (!reset_n) begin
  @T@_rbusy = 0; @T@_rv = 0; @T@_aw = 0; @T@_w = 0; @T@_bv = 0;
 end else begin
  if (nl_t_@T@_rvalid && nl_t_@T@_rready) begin @T@_rbusy = 0; @T@_rv = 0; end
  else if (@T@_rbusy && !@T@_rv) begin
   if (@T@_rwait == 0) @T@_rv = 1; else @T@_rwait = @T@_rwait - 1;
  end
  if (nl_t_@T@_bvalid && nl_t_@T@_bready) begin @T@_aw = 0; @T@_w = 0; @T@_bv = 0; end
  else if (@T@_w && !@T@_bv) begin
   if (@T@_bwait == 0) @T@_bv = 1; else @T@_bwait = @T@_bwait - 1;
  end
  if (nl_t_@T@_wvalid && nl_t_@T@_wready) begin
   wbeat(@T@_wa, nl_t_@T@_wdata, nl_t_@T@_wstrb);
   @T@_w = 1; @T@_bwait = @T@_delay;
  end
  if (nl_t_@T@_arvalid && nl_t_@T@_arready) begin
   arrive(0, @IDX@, nl_t_@T@_araddr, nl_t_@T@_arprot);
   @T@_rbusy = 1; @T@_ra = nl_t_@T@_araddr; @T@_rwait = @T@_delay;
  end
  if (nl_t_@T@_awvalid && nl_t_@T@_awready) begin
   arrive(1, @IDX@, nl_t_@T@_awaddr, nl_t_@T@_awprot);
   @T@_aw = 1; @T@_wa = nl_t_@T@_awaddr;
  end
 end
end
'''

# APB4 target model: PREADY after @T@_delay ACCESS cycles. The setup phase is
# the request acceptance the ordering rule observes.
_APB = '''
integer @T@_delay = 0, @T@_hang = 0, @T@_err = -1, @T@_acc;
assign nl_t_@T@_pready = nl_t_@T@_psel && nl_t_@T@_penable && @T@_acc >= @T@_delay;
assign nl_t_@T@_prdata = data_of(@IDX@, nl_t_@T@_paddr);
assign nl_t_@T@_pslverr = nl_t_@T@_pready && serial_of(nl_t_@T@_paddr) == @T@_err;
always @(posedge @CLK@) begin
 if (!reset_n) @T@_acc = 0;
 else begin
  if (nl_t_@T@_psel && !nl_t_@T@_penable) arrive(nl_t_@T@_pwrite, @IDX@, nl_t_@T@_paddr, nl_t_@T@_pprot);
  if (nl_t_@T@_psel && nl_t_@T@_penable && nl_t_@T@_pready && nl_t_@T@_pwrite)
   wbeat(nl_t_@T@_paddr, nl_t_@T@_pwdata, nl_t_@T@_pstrb);
  if (nl_t_@T@_psel && nl_t_@T@_penable && !nl_t_@T@_pready) @T@_acc = @T@_acc + 1;
  else @T@_acc = 0;
 end
end
'''

# One Lite source: AR, AW and W issue processes, the in-order scoreboard and
# the face checks. Tables: source @I@ owns entries @I@*MAXC+k.
_SOURCE = '''
integer @S@_nar = @NAR@, @S@_naw = @NAW@;
integer @S@_ri, @S@_rd, @S@_wi, @S@_wd, @S@_wv, @S@_rmax, @S@_wmax, @S@_rpipe, @S@_wpipe,
        @S@_rgap = 0, @S@_bgap = 0, @S@_rhold = 0, @S@_bhold = 0, @S@_awlag = 0, @S@_wlag = 0,
        @S@_last, @S@_pick, @S@_k, @S@_opick;
reg [15:0] @S@_lfsr;
reg @S@_rheld, @S@_bheld; reg [@W@-1:0] @S@_rdata_q; reg [1:0] @S@_rresp_q, @S@_bresp_q;
always @(negedge clk_main) begin
 @S@_lfsr <= {@S@_lfsr[14:0], @S@_lfsr[15] ^ @S@_lfsr[13] ^ @S@_lfsr[12] ^ @S@_lfsr[10]};
 nl_i_@S@_rready <= @S@_rhold == 0 && (@S@_rgap == 0 || @S@_lfsr[2:0] >= @S@_rgap);
 nl_i_@S@_bready <= @S@_bhold == 0 && (@S@_bgap == 0 || @S@_lfsr[5:3] >= @S@_bgap);
 if (@S@_rhold > 0) @S@_rhold = @S@_rhold - 1;
 if (@S@_bhold > 0) @S@_bhold = @S@_bhold - 1;
end
always @(posedge clk_main) begin
 if (!reset_n) begin
  @S@_ri <= 0; @S@_rd <= 0; @S@_wi <= 0; @S@_wd <= 0; @S@_wv <= 0; @S@_rheld <= 0; @S@_bheld <= 0;
 end else begin
  // AXI A3.2.1: an offered R or B stays unchanged until it is accepted.
  if (@S@_rheld)
   check(nl_i_@S@_rvalid === 1'b1 && nl_i_@S@_rdata === @S@_rdata_q &&
         nl_i_@S@_rresp === @S@_rresp_q, "@U@_R_STABLE");
  if (@S@_bheld) check(nl_i_@S@_bvalid === 1'b1 && nl_i_@S@_bresp === @S@_bresp_q, "@U@_B_STABLE");
  @S@_rheld <= nl_i_@S@_rvalid && !nl_i_@S@_rready;
  @S@_rdata_q <= nl_i_@S@_rdata; @S@_rresp_q <= nl_i_@S@_rresp;
  @S@_bheld <= nl_i_@S@_bvalid && !nl_i_@S@_bready;
  @S@_bresp_q <= nl_i_@S@_bresp;
  if (nl_i_@S@_arvalid && nl_i_@S@_arready) begin
   rd_hs[@I@*MAXC + @S@_ri] <= cycle; @S@_ri <= @S@_ri + 1;
  end
  if (nl_i_@S@_awvalid && nl_i_@S@_awready) begin
   wr_hs[@I@*MAXC + @S@_wi] <= cycle; @S@_wi <= @S@_wi + 1;
  end
  if (nl_i_@S@_wvalid && nl_i_@S@_wready) @S@_wv <= @S@_wv + 1;
  // A transaction's ordering key binding frees as its response enters the source, which then
  // offers it here: the oldest waiting transaction of the offered ID, accepted or not.
  if (nl_i_@S@_rvalid) begin
   @S@_opick = -1;
   for (@S@_k = @S@_ri - 1; @S@_k >= 0; @S@_k = @S@_k - 1)
    if (!rd_complete[@I@*MAXC + @S@_k] && rd_idv[@I@*MAXC + @S@_k] == @RID@) @S@_opick = @S@_k;
   if (@S@_opick >= 0) rd_offered[@I@*MAXC + @S@_opick] <= 1;
  end
  if (nl_i_@S@_bvalid) begin
   @S@_opick = -1;
   for (@S@_k = @S@_wi - 1; @S@_k >= 0; @S@_k = @S@_k - 1)
    if (!wr_complete[@I@*MAXC + @S@_k] && wr_idv[@I@*MAXC + @S@_k] == @BID@) @S@_opick = @S@_k;
   if (@S@_opick >= 0) wr_offered[@I@*MAXC + @S@_opick] <= 1;
  end
  // Each response belongs to the oldest waiting transaction of its AXI ID
  // (ID 0 on a Lite face: strict issue order).
  if (nl_i_@S@_rvalid && nl_i_@S@_rready) begin
@R_LAST@   @S@_pick = -1;
   for (@S@_k = @S@_ri - 1; @S@_k >= 0; @S@_k = @S@_k - 1)
    if (!rd_complete[@I@*MAXC + @S@_k] && rd_idv[@I@*MAXC + @S@_k] == @RID@) @S@_pick = @S@_k;
   check(@S@_pick >= 0, "@U@_R_WITHOUT_READ");
   if (@S@_pick >= 0) begin
    @S@_pick = @I@*MAXC + @S@_pick;
    check(nl_i_@S@_rresp === rd_resp[@S@_pick], "@U@_RRESP");
    check(rd_resp[@S@_pick] != 0 || nl_i_@S@_rdata === rd_data[@S@_pick], "@U@_RDATA");
    rd_complete[@S@_pick] <= 1; rd_done[@S@_pick] <= cycle;
   end
   @S@_rd <= @S@_rd + 1; @S@_last <= cycle;
  end
  if (nl_i_@S@_bvalid && nl_i_@S@_bready) begin
   check(@S@_wd < @S@_wi && @S@_wd < @S@_wv, "@U@_B_WITHOUT_WRITE");
   @S@_pick = -1;
   for (@S@_k = @S@_wi - 1; @S@_k >= 0; @S@_k = @S@_k - 1)
    if (!wr_complete[@I@*MAXC + @S@_k] && wr_idv[@I@*MAXC + @S@_k] == @BID@) @S@_pick = @S@_k;
   check(@S@_pick >= 0, "@U@_B_WITHOUT_WRITE_ID");
   if (@S@_pick >= 0) begin
    @S@_pick = @I@*MAXC + @S@_pick;
    check(nl_i_@S@_bresp === wr_resp[@S@_pick], "@U@_BRESP");
    wr_complete[@S@_pick] <= 1; wr_done[@S@_pick] <= cycle;
   end
   @S@_wd <= @S@_wd + 1; @S@_last <= cycle;
  end
  if (@S@_ri - @S@_rd > @S@_rmax) @S@_rmax <= @S@_ri - @S@_rd;
  if (@S@_wi - @S@_wd > @S@_wmax) @S@_wmax <= @S@_wi - @S@_wd;
  check(@S@_ri - @S@_rd <= @N@ + @NAR@ + 1 && @S@_wi - @S@_wd <= @N@ + @NAW@ + 1, "@U@_CAPACITY");
 end
end
task automatic issue_reads_@S@;
 integer i;
 begin
  for (i = rd_from[@I@]; i < rd_n[@I@] && !abort; i = i + 1) begin
   @(negedge clk_main);
   nl_i_@S@_arvalid = 1; nl_i_@S@_araddr = rd_addr[@I@*MAXC + i]; nl_i_@S@_arprot = rd_prot[@I@*MAXC + i];
@AR_EXTRA@   @(posedge clk_main); while (!nl_i_@S@_arready && !abort) @(posedge clk_main);
  end
  @(negedge clk_main); nl_i_@S@_arvalid = 0;
 end
endtask
task automatic issue_aw_@S@;
 integer i;
 begin
  for (i = wr_from[@I@]; i < wr_n[@I@] && !abort; i = i + 1) begin
   repeat (@S@_awlag) @(negedge clk_main);
   @(negedge clk_main);
   nl_i_@S@_awvalid = 1; nl_i_@S@_awaddr = wr_addr[@I@*MAXC + i]; nl_i_@S@_awprot = wr_prot[@I@*MAXC + i];
@AW_EXTRA@   @(posedge clk_main); while (!nl_i_@S@_awready && !abort) @(posedge clk_main);
   @(negedge clk_main); nl_i_@S@_awvalid = 0;
  end
 end
endtask
task automatic issue_w_@S@;
 integer i;
 begin
  for (i = wr_from[@I@]; i < wr_n[@I@] && !abort; i = i + 1) begin
   repeat (@S@_wlag) @(negedge clk_main);
   @(negedge clk_main);
   nl_i_@S@_wvalid = 1; nl_i_@S@_wdata = wr_data[@I@*MAXC + i]; nl_i_@S@_wstrb = wr_strb[@I@*MAXC + i];
@W_EXTRA@   @(posedge clk_main); while (!nl_i_@S@_wready && !abort) @(posedge clk_main);
   @(negedge clk_main); nl_i_@S@_wvalid = 0;
  end
 end
endtask
'''


def write_bench(work, mapping, mode, eps, rules=True, single=False):
    model = Model(mode)
    decl, conns, init = [], [], []
    for port in mapping['ports']:
        name, port_bits = port['name'], port['width']
        kind = 'reg' if port['direction'] == 'input' and not name.startswith('nl_t_') else 'wire'
        decl.append(f'{kind} [{port_bits - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and not name.startswith('clk_') and name != 'reset_n':
            init.append(f'{name} = 0;')
    widths = {p['name']: p['width'] for p in mapping['ports']}
    bits = width(mode)
    bus = bits // 8
    size = "3'd3" if bus == 8 else "3'd2"
    extras = {}
    for s in SOURCES:
        axi = {f'nl_i_{s}_{x}' for x in ('arid', 'rid', 'bid', 'awid', 'rlast', 'wlast', 'awlen',
                                         'arlen')}
        if lite(mode, s):
            assert not axi & set(widths), s
            extras[s] = {'RID': "8'd0", 'BID': "8'd0", 'R_LAST': '', 'AR_EXTRA': '',
                         'AW_EXTRA': '', 'W_EXTRA': ''}
        else:
            assert axi <= set(widths), s
            extras[s] = {
                'RID': f'nl_i_{s}_rid', 'BID': f'nl_i_{s}_bid',
                'R_LAST': f'   check(nl_i_{s}_rlast === 1\'b1, "{s.upper()}_RLAST");\n',
                'AR_EXTRA': f'   nl_i_{s}_arid = rd_idv[@I@*MAXC + i]; nl_i_{s}_arlen = 0; '
                            f'nl_i_{s}_arlock = rd_lock[@I@*MAXC + i]; '
                            f'nl_i_{s}_arsize = {size}; nl_i_{s}_arburst = 1;\n',
                'AW_EXTRA': f'   nl_i_{s}_awid = wr_idv[@I@*MAXC + i]; nl_i_{s}_awlen = 0; '
                            f'nl_i_{s}_awlock = wr_lock[@I@*MAXC + i]; '
                            f'nl_i_{s}_awsize = {size}; nl_i_{s}_awburst = 1;\n',
                'W_EXTRA': f'   nl_i_{s}_wlast = 1;\n'}
        # A read-only source has no write face: inert stand-ins keep the
        # shared issue tasks (which issue nothing for it) compilable.
        if not writes_allowed(mode, s):
            for signal, bits_, driven in (('awaddr', 32, 1), ('awprot', 3, 1), ('awvalid', 1, 1),
                                          ('wdata', bits, 1), ('wstrb', bus, 1), ('wvalid', 1, 1),
                                          ('bready', 1, 1), ('awready', 1, 0), ('wready', 1, 0),
                                          ('bvalid', 1, 0), ('bresp', 2, 0)):
                name = f'nl_i_{s}_{signal}'
                assert name not in widths, name
                decl.append(f'reg [{bits_ - 1}:0] {name};' if driven else
                            f"wire [{bits_ - 1}:0] {name} = 0;")
                if driven and signal != 'bready':
                    init.append(f'{name} = 0;')
    models, registers = [], set()
    for name in model.names:
        kind = model.targets[name][0]
        values = {'T': name, 'U': name.upper(), 'IDX': model.index[name],
                  'CLK': target_clock(mode)}
        if kind == 'apb4':
            models.append(_fill(_APB, **values))
        elif kind == 'axi4_lite':
            models.append(_fill(_LITE, **values))
        else:
            iw = widths.get(f'nl_t_{name}_arid', 0)
            if not iw:
                decl.append(f"wire [0:0] nl_t_{name}_arid = 1'b0, nl_t_{name}_awid = 1'b0;")
                decl.append(f'wire [0:0] nl_t_{name}_rid, nl_t_{name}_bid;')
            k = target_capacity(mode, name, single)
            if TARGET_CAPACITY.get(mode, {}).get(name, 1) > 1:
                # The network ID field is the widest source ID: two bits with
                # a full AXI4 source, none with AXI4-Lite sources only.
                nid = 2 if mode != 'funnel' and any(
                    v[0] == 'axi4' for v in sources(mode).values()) else 0
                text, driven = registered(_fill(_AXI_MULTI, IW=max(1, iw), SIZE=size, CAP=k,
                                                NID=nid, **values), target_clock(mode))
                models.append(text)
                registers.update(driven)
            else:
                target_bits = widths[f'nl_t_{name}_rdata']
                rdata = f'data_of({model.index[name]}, {name}_ra)'
                wdata, strb = f'nl_t_{name}_wdata', f'nl_t_{name}_wstrb'
                checks = ''
                if target_bits != bits:
                    assert target_bits > bits
                    lane_bits = (target_bits // 8).bit_length() - 1
                    rlane = f'{name}_ra[{lane_bits - 1}:0]'
                    wlane = f'{name}_wa[{lane_bits - 1}:0]'
                    rdata = f"({{{target_bits - bits}'b0, {rdata}}} << ({rlane} * 8))"
                    # Check the complete target word before the source-sized
                    # task arguments can truncate unselected byte lanes.
                    mask = f"{target_bits}'h{(1 << bits) - 1:x}"
                    strobes = f"{target_bits // 8}'h{(1 << bus) - 1:x}"
                    checks = (f'check(({wdata} & ~({mask} << ({wlane} * 8))) === 0, '
                              f'"{name.upper()}_WDATA_OTHER_LANES");\n   '
                              f'check(({strb} & ~({strobes} << {wlane})) === 0, '
                              f'"{name.upper()}_WSTRB_OTHER_LANES");\n   ')
                    wdata = f'({wdata} >> ({wlane} * 8))'
                    strb = f'({strb} >> {wlane})'
                wbeat = checks + f'wbeat({name}_wa, {wdata}, {strb});'
                models.append(_fill(_AXI, IW=max(1, iw), SIZE=size, RDATA=rdata, WBEAT=wbeat,
                                    **values))
    # Ports a registered model loads (bench_registered) are registers.
    decl = [line.replace('wire ', 'reg ', 1) if line.split()[-1].rstrip(';') in registers else line
            for line in decl]
    source_text = ''.join(_fill(_fill(_SOURCE, **extras[s]), S=s, U=s.upper(), I=i,
                                N=capacity(mode, s, single), W=bits,
                                NAR=port_slots(mapping, f'source/{s}', 'ar'),
                                NAW=port_slots(mapping, f'source/{s}', 'aw'))
                          for i, s in enumerate(SOURCES))
    knobs = []
    for name in model.names:
        if TARGET_CAPACITY.get(mode, {}).get(name, 1) > 1:
            knobs.append(f'{name}_rlat = 0; {name}_blat = 0; {name}_hang = 0; {name}_err = -1; '
                         f'{name}_reorder = 0; {name}_rmax = 0; {name}_wmax = 0; '
                         f'{name}_errsrc = -1;')
        elif model.targets[name][0] == 'axi4':
            knobs.append(f'{name}_rlat = 0; {name}_blat = 0; {name}_hang = 0; {name}_err = -1;')
        else:
            knobs.append(f'{name}_delay = 0; {name}_hang = 0; {name}_err = -1;')
    for s in SOURCES:
        knobs.append(f'{s}_rgap = 0; {s}_bgap = 0; {s}_rhold = 0; {s}_bhold = 0; '
                     f'{s}_awlag = 0; {s}_wlag = 0;')
    maxima = ' '.join(f'{s}_rmax = 0; {s}_wmax = 0; {s}_rpipe = 0; {s}_wpipe = 0; {s}_last = 0;'
                      for s in SOURCES)
    issue = '\n'.join(f'   issue_reads_{s};\n   issue_aw_{s};\n   issue_w_{s};' for s in SOURCES)
    done = ' && '.join(f'{s}_rd == rd_n[{i}] && {s}_wd == wr_n[{i}]' for i, s in enumerate(SOURCES))
    body = [_episode(ep, bits) for ep in eps]
    n2 = 2 * MAXC - 1
    table_values = ' '.join(
        f'id_table[{i}] = {int(os.environ.get("NOCLOOM_LO_ID_TABLE", "2")) if lite(mode, s) else 0};'
        for i, s in enumerate(SOURCES))
    text = f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0;
always #5 clk_main = ~clk_main;
{"initial begin clk_far = 0; #3; forever #7 clk_far = ~clk_far; end" if mode in CLOCKED else ''}
localparam MAXC = {MAXC};
integer cycle = 0, abort = 0, t0 = 0, active = 0;
integer rules = {1 if rules else 0};
reg [31:0] rd_addr [0:{n2}]; reg [2:0] rd_prot [0:{n2}]; reg [1:0] rd_resp [0:{n2}];
reg [{bits - 1}:0] rd_data [0:{n2}]; integer rd_dest [0:{n2}]; integer rd_reach [0:{n2}];
reg rd_complete [0:{n2}]; integer rd_seen [0:{n2}]; reg rd_offered [0:{n2}]; reg wr_offered [0:{n2}];
reg [31:0] wr_addr [0:{n2}]; reg [2:0] wr_prot [0:{n2}]; reg [1:0] wr_resp [0:{n2}];
reg [{bits - 1}:0] wr_data [0:{n2}]; reg [{bus - 1}:0] wr_strb [0:{n2}];
reg [7:0] rd_idv [0:{n2}]; reg [7:0] wr_idv [0:{n2}]; integer wr_dest [0:{n2}];
integer wr_reach [0:{n2}]; reg wr_complete [0:{n2}]; integer wr_seen [0:{n2}];
integer rd_hs [0:{n2}]; integer wr_hs [0:{n2}]; integer rd_done [0:{n2}]; integer wr_done [0:{n2}];
integer rd_n [0:1]; integer wr_n [0:1]; integer rd_from [0:1]; integer wr_from [0:1];
integer id_table [0:1]; integer mutex_checks = 0;
reg rd_lock [0:{n2}]; reg wr_lock [0:{n2}];
// track 0: target-side bookkeeping (arrivals, beats, access counts) off.
integer track = 1;
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 900000) begin $display("ASSERT_FAIL:LITE_OUTSTANDING.BENCH_LIMIT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*40-1:0] label_;
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:LITE_OUTSTANDING.%0s cycle=%0d", label_, cycle); $fatal(1);
 end
endtask
function [{bits - 1}:0] data_of;
 input integer index; input [31:0] local_addr;
 reg [31:0] word;
 begin
  word = {{8'ha0 | index[7:0], 8'h5a, local_addr[15:0]}};
  data_of = {{{'~word, ' if bits == 64 else ''}word}};
 end
endfunction
function integer serial_of;
 input [31:0] local_addr;
 serial_of = local_addr[11] * 128 + local_addr[10:4];
endfunction
{_base_function(model)}
{source_text}
// A request accepted at a target port (write: 1 for AW or an APB write).
task automatic arrive;
 input integer write, target; input [31:0] local_addr; input [2:0] prot;
 integer s, k, j;
 if (track) begin
  s = local_addr[11]; k = s * MAXC + local_addr[10:4];
  check((local_addr & {bus - 1}) == 0, "UNALIGNED_ADDRESS_CLEARED");
  if (write) begin
   check(local_addr[10:4] < wr_n[s] && wr_dest[k] == target && wr_reach[k] != 0, "W_UNEXPECTED_ACCESS");
   check(wr_seen[k] == 0, "W_ACCESS_ONCE");
   check(prot === wr_prot[k] && local_addr === ((wr_addr[k] - base_of(target)) & ~{bus - 1}),
         "W_ADDRESS_PROT");
   wr_seen[k] = wr_seen[k] + 1;
   for (j = s * MAXC; j < (s + 1) * MAXC; j = j + 1)
    if (id_table[s] == 1) begin
     mutex_checks = mutex_checks + 1;
     check(!rd_seen[j] || rd_complete[j] || rd_offered[j], "ID_TABLE_MUTEX_W");
    end
   for (j = s * MAXC; j < k; j = j + 1)
    check(!rules || wr_idv[j] != wr_idv[k] || wr_dest[j] == target || wr_complete[j] || wr_offered[j],
          "ORDERING_RULE_W");
   // Network overlap: this transaction was handshaken at the source while
   // the previous one of this source to this target was still in flight
   // there (not yet completed, or completed only after this handshake).
   if (k > s * MAXC && wr_dest[k - 1] == target &&
       (!wr_complete[k - 1] || wr_hs[k] < wr_done[k - 1])) begin
    if (s == 0) s0_wpipe = s0_wpipe + 1; else s1_wpipe = s1_wpipe + 1;
   end
  end else begin
   check(local_addr[10:4] < rd_n[s] && rd_dest[k] == target && rd_reach[k] != 0, "R_UNEXPECTED_ACCESS");
   check(rd_seen[k] == 0, "R_ACCESS_ONCE");
   check(prot === rd_prot[k] && local_addr === ((rd_addr[k] - base_of(target)) & ~{bus - 1}),
         "R_ADDRESS_PROT");
   rd_seen[k] = rd_seen[k] + 1;
   for (j = s * MAXC; j < (s + 1) * MAXC; j = j + 1)
    if (id_table[s] == 1) begin
     mutex_checks = mutex_checks + 1;
     check(!wr_seen[j] || wr_complete[j] || wr_offered[j], "ID_TABLE_MUTEX_R");
    end
   for (j = s * MAXC; j < k; j = j + 1)
    check(!rules || rd_idv[j] != rd_idv[k] || rd_dest[j] == target || rd_complete[j] || rd_offered[j],
          "ORDERING_RULE_R");
   // Network overlap: this transaction was handshaken at the source while
   // the previous one of this source to this target was still in flight
   // there (not yet completed, or completed only after this handshake).
   if (k > s * MAXC && rd_dest[k - 1] == target &&
       (!rd_complete[k - 1] || rd_hs[k] < rd_done[k - 1])) begin
    if (s == 0) s0_rpipe = s0_rpipe + 1; else s1_rpipe = s1_rpipe + 1;
   end
  end
 end
endtask
// A write beat at a target: strobes as issued, unstrobed bytes zero.
task automatic wbeat;
 input [31:0] local_addr; input [{bits - 1}:0] data; input [{bus - 1}:0] strb;
 integer k, b;
 reg [{bits - 1}:0] mask;
 if (track) begin
  k = local_addr[11] * MAXC + local_addr[10:4];
  for (b = 0; b < {bits}; b = b + 1) mask[b] = strb[b / 8];
  check(strb === wr_strb[k], "WSTRB");
  check(data === (wr_data[k] & mask), "WDATA_UNSTROBED_ZERO");
 end
endtask
{''.join(models)}
task automatic clear_tables;
 integer k;
 begin
  for (k = 0; k < {2 * MAXC}; k = k + 1) begin
   rd_complete[k] = 0; wr_complete[k] = 0; rd_seen[k] = 0; wr_seen[k] = 0;
   rd_offered[k] = 0; wr_offered[k] = 0;
   rd_dest[k] = -2; wr_dest[k] = -2; rd_reach[k] = 0; wr_reach[k] = 0;
   rd_hs[k] = 0; wr_hs[k] = 0; rd_done[k] = -1; wr_done[k] = -1;
  end
  rd_n[0] = 0; rd_n[1] = 0; wr_n[0] = 0; wr_n[1] = 0;
  rd_from[0] = 0; rd_from[1] = 0; wr_from[0] = 0; wr_from[1] = 0; track = 1;
  {table_values}
 end
endtask
task automatic episode;
 input [8*40-1:0] label_;
 begin
  @(negedge clk_main); reset_n = 0; abort = 0;
  {' '.join(knobs)}
  clear_tables;
  repeat (5) @(posedge clk_main);
  {"repeat (4) @(posedge clk_far);" if mode in CLOCKED else ''}
  @(negedge clk_main); reset_n = 1;
  {maxima}
  repeat (3) @(posedge clk_main);
  t0 = cycle;
  $display("EPISODE %0s", label_);
 end
endtask
task automatic access_check;
 integer k;
 if (track) begin
  for (k = 0; k < {2 * MAXC}; k = k + 1) begin
   check(rd_reach[k] == 2 || rd_seen[k] == rd_reach[k], "R_ACCESS_COUNT");
   check(wr_reach[k] == 2 || wr_seen[k] == wr_reach[k], "W_ACCESS_COUNT");
  end
 end
endtask
task automatic run_traffic;
 input integer limit;
 begin
  fork
{issue}
  join
  while (!({done}) && cycle - t0 < limit) @(posedge clk_main);
  check({done}, "ALL_RESPONSES");
  repeat (30) @(posedge clk_main);
  check({done}, "NO_EXTRA_RESPONSES");
  access_check;
 end
endtask
initial begin
 reset_n = 0;
{chr(10).join(' ' + i for i in init)}
{' '.join(f"{s}_lfsr = 16'h{0xace1 + 7 * i:04x};" for i, s in enumerate(SOURCES))}
 clear_tables;
 repeat (10) @(posedge clk_main);
{chr(10).join(body)}
 if ((id_table[0] == 1 || id_table[1] == 1) && rules) check(mutex_checks > 0, "ID_TABLE_COVERAGE");
 $display("ID_TABLE_MUTEX checks=%0d", mutex_checks);
 $display("NOCLOOM_PASS lite outstanding");
 $finish;
end
endmodule
'''
    (work / 'tb.sv').write_text(text)


def _base_function(model):
    arms = ' '.join(f"{model.index[t]}: base_of = 32'h{model.targets[t][1]:08x};"
                    for t in model.names)
    return f'''function [31:0] base_of;
 input integer target;
 case (target) {arms} default: base_of = 0; endcase
endfunction'''


def _episode(ep, bits):
    lines = [f' episode("{ep["name"]}");']
    for i, s in enumerate(SOURCES):
        for direction in ('rd', 'wr'):
            rows = ep['rows'].get((s, direction), [])
            assert len(rows) <= MAXC
            lines.append(f' {direction}_n[{i}] = {len(rows)};')
            for k, r in enumerate(rows):
                at = f'{i}*MAXC+{k}'
                parts = [f"{direction}_addr[{at}] = 32'h{r['addr']:08x};",
                         f"{direction}_idv[{at}] = {r['id']};",
                         f"{direction}_prot[{at}] = {r['prot']};",
                         f"{direction}_resp[{at}] = {r['resp']};",
                         f"{direction}_dest[{at}] = {r['dest']};",
                         f"{direction}_reach[{at}] = {r['reach']};",
                         f"{direction}_lock[{at}] = {r['lock']};"]
                if direction == 'rd':
                    parts.append(f"rd_data[{at}] = {bits}'h{r['rdata']:0{bits // 4}x};")
                else:
                    parts += [f"wr_data[{at}] = {bits}'h{r['wdata']:0{bits // 4}x};",
                              f"wr_strb[{at}] = {bits // 8}'h{r['strobe']:x};"]
                lines.append(' ' + ' '.join(parts))
    for line in ep['knob_text']:
        lines.append(' ' + line)
    if 'reset_after' in ep:
        lines.append(f''' fork
  fork
{chr(10).join(f'   issue_reads_{s};{chr(10)}   issue_aw_{s};{chr(10)}   issue_w_{s};' for s in SOURCES)}
  join
  begin repeat ({ep['reset_after']}) @(posedge clk_main); abort = 1; end
 join_any
 repeat ({ep['reset_after']} + 2) @(posedge clk_main);''')
    else:
        if 'phases' in ep:
            # Each phase issues the next commands of the listed lists and
            # waits for them; the tables keep the whole episode.
            done = {(s, d): 0 for s in SOURCES for d in ('rd', 'wr')}
            for phase in ep['phases']:
                for i, s in enumerate(SOURCES):
                    for d in ('rd', 'wr'):
                        lines.append(f' {d}_from[{i}] = {done[(s, d)]};')
                        done[(s, d)] += phase.get((s, d), 0)
                        lines.append(f' {d}_n[{i}] = {done[(s, d)]};')
                lines.append(f' run_traffic({ep.get("limit", 20000)});')
        else:
            lines.append(f' run_traffic({ep.get("limit", 20000)});')
    lines.append(' $display("IN_FLIGHT ' + ' '.join(
        f'{s}_r=%0d {s}_w=%0d {s}_rpipe=%0d {s}_wpipe=%0d'
        for s in SOURCES) + '", ' +
        ', '.join(f'{s}_rmax, {s}_wmax, {s}_rpipe, {s}_wpipe' for s in SOURCES) + ');')
    if ep.get('measure'):
        lines.append(f' $display("THROUGHPUT {ep["name"]} cycles=%0d", s0_last - t0);')
    if ep.get('target_maxima'):
        lines.append(' $display("TARGET_IN_FLIGHT ' +
                     ' '.join(f'{t}_r=%0d {t}_w=%0d' for t in ep['target_maxima']) + '", ' +
                     ', '.join(f'{t}_rmax, {t}_wmax' for t in ep['target_maxima']) + ');')
    for check in ep.get('checks', []):
        lines.append(f' check({check[0]}, "{check[1]}");')
    return '\n'.join(lines)
