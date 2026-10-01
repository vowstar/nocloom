"""Exclusive access (AXI A7.2) through the public CLI: configurations.

Modes:

  graph  Two 32-bit AXI4 sources (AxID 2 bits) on a one-node graph. Targets:
         tm  AXI4, network monitor with 2 reservations (eviction)
         tx  AXI4, target mode, 2 shared packet slots (its own monitor)
         ty  AXI4, target mode, 1 shared packet slot
         tp  APB4, network, timeout 1024 cycles (completion)
         t3  APB3, network (no PSTRB: narrow exclusive SLVERR)
         th  AHB, network
         tl  AXI4-Lite, network
         tn  AXI4 without exclusive support (AxLOCK ignored, OKAY)
         Every network monitor reserves 8-byte granules (transport
         exclusive_granule); the other modes keep the 1-byte default.
  funnel The default two-source two-target funnel: tm AXI4 and tp APB4 with
         two reservations, both network.
  wide   A 64-bit and a 32-bit source, per-target widths and bounded packets:
         tw  AXI4 64-bit, network (the 32-bit source's beats merge into words)
         tx  AXI4 32-bit, target mode (64-bit exclusive bursts clamp to 32-bit
             beats)
         th  AHB 32-bit, network (an exclusive burst may not cross its 64-byte
             packet limit)
         tp  APB4, network (a 64-bit exclusive beat gathers two words)
  split  wide's sources with narrow_bursts split_non_modifiable on the 64-bit s0,
         bounded packets and 64-bit AXI4 targets: an exclusive narrow INCR is
         not split into single beats, so each target sees one burst, while the same
         non-exclusive burst goes out one packet per beat:
         tw  AXI4 64-bit, network
         tx  AXI4 64-bit, target mode
         tn  AXI4 64-bit without exclusive support (AxLOCK ignored, OKAY)
         tp  APB4, network
  split_none  split without any exclusive support (no Weft lock field): the
         source's AxLOCK still keeps the narrow burst whole.

The graph mode also runs as a clocked graph per CLOCKS ratio: the sources and
the request switch in domain main, the targets (with their monitors) and the
response switch in domain far, so every exclusive request and response
crosses an asynchronous FIFO with its lock and exclusive_okay fields; and
with tm keeping four bursts in flight (OUTSTANDING), so its monitor sees reads
overlap writes in flight, and tl four packets, which the network still serves
in arrival order.

The bench in exclusive_bench.py owns the transactions and checks."""
from endpoint_ports import endpoint_ports

# name: protocol, window base, exclusive mode, extra target keys
GRAPH_TARGETS = {
    'tm': ('axi4', 0x10000, 'network', {'exclusive_monitors': 2}),
    'tx': ('axi4', 0x20000, 'target', {'max_outstanding': 2}),
    'ty': ('axi4', 0x21000, 'target', {}),
    'tp': ('apb4', 0x30000, 'network', {'timeout': {'cycles': 1024, 'completion': True}}),
    't3': ('apb3', 0x31000, 'network', {}),
    'th': ('ahb_v3', 0x32000, 'network', {}),
    'tl': ('axi4_lite', 0x33000, 'network', {}),
    'tn': ('axi4', 0x40000, None, {}),
}
WIDE_TARGETS = {
    'tw': ('axi4', 0x10000, 'network', {'data_width': 64}),
    'tx': ('axi4', 0x20000, 'target', {}),
    'th': ('ahb_v3', 0x32000, 'network', {}),
    'tp': ('apb4', 0x30000, 'network', {}),
}
SPLIT_TARGETS = {
    'tw': ('axi4', 0x10000, 'network', {'data_width': 64}),
    'tx': ('axi4', 0x20000, 'target', {'data_width': 64}),
    'tn': ('axi4', 0x40000, None, {'data_width': 64}),
    'tp': ('apb4', 0x30000, 'network', {}),
}
SPLIT_NONE_TARGETS = {name: (protocol, base, None, extra)
                      for name, (protocol, base, _kind, extra) in SPLIT_TARGETS.items()}
# The default two-target funnel (no topology).
FUNNEL_TARGETS = {
    'tm': ('axi4', 0x10000, 'network', {}),
    'tp': ('apb4', 0x20000, 'network', {'exclusive_monitors': 2}),
}
MODES = ('graph', 'wide', 'funnel', 'split', 'split_none')
# Clocked graph ratios main:far: main and far periods (ps) and far's phase
# offset (ps) against main.
CLOCKS = {'1_1': (10000, 10000, 3000), '1_3': (10000, 30000, 7000),
          '7_5': (14000, 10000, 2000)}
# The network targets of several bursts or packets in flight and their capacity.
OUTSTANDING = {'tm': 4, 'tl': 4}
SPLIT_MODES = ('split', 'split_none')
SOURCES = ('s0', 's1')
ID_BITS = 2
# The owner of an exclusive access: {source, AxID}, the bench drives AxUSER with AxID.
OWNER_FLAGS = ['id0', 'id1', 'source']
WINDOW = 0x1000


def granule(mode):
    """The global reservation granule of the network monitors."""
    return 8 if mode == 'graph' else 1


def targets(mode):
    return {'graph': GRAPH_TARGETS, 'wide': WIDE_TARGETS, 'funnel': FUNNEL_TARGETS,
            'split': SPLIT_TARGETS, 'split_none': SPLIT_NONE_TARGETS}[mode]


def len_bits(mode):
    return 8


def source_width(mode, source):
    return 64 if mode in ('wide',) + SPLIT_MODES and source == 's0' else 32


def target_width(mode, target):
    return targets(mode)[target][3].get('data_width', 32)


def configuration(mode, clocks=None, outstanding=None):
    """clocks: a CLOCKS key, the graph in two domains; outstanding: target
    capacities (OUTSTANDING)."""
    chosen = targets(mode)
    target_list = []
    for name, (protocol, base, exclusive, extra) in chosen.items():
        entry = {'name': name, 'clock_domain': 'far' if clocks else 'main', 'protocol': protocol,
                 'data_width': 32,
                 'mappings': [{'base': hex(base), 'size': hex(WINDOW)}]}
        if exclusive:
            entry['exclusive'] = exclusive
            entry['exclusive_id_flags'] = OWNER_FLAGS
        entry.update(extra)
        if name in (outstanding or {}):
            entry['max_outstanding'] = outstanding[name]
        target_list.append(entry)
    initiators = []
    for index, name in enumerate(SOURCES):
        entry = {'name': name, 'clock_domain': 'main', 'protocol': 'axi4',
                 'data_width': source_width(mode, name), 'user_width': ID_BITS,
                 'user_flags': {'id0': 'user[0]', 'id1': 'user[1]', 'source': index}}
        if mode in SPLIT_MODES and name == 's0':
            entry['narrow_bursts'] = 'split_non_modifiable'
        initiators.append(entry)
    transport = {'transport_word_bytes': 4, 'user_flags': OWNER_FLAGS}
    if granule(mode) != 1:
        transport['exclusive_granule'] = granule(mode)
    config = {'schema_version': 10, 'name': 'exclusive', 'address_width': 32,
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
    if clocks:
        main, far, _phase = CLOCKS[clocks]
        config['clock_domains'] = [
            {'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
             'test_port': 'test', 'period_ps': main},
            {'name': 'far', 'clock_port': 'clk_far', 'reset_port': 'reset_n',
             'test_port': 'test', 'period_ps': far}]
        config['topology']['request']['nodes'][0]['clock_domain'] = 'main'
        config['topology']['response']['nodes'][0]['clock_domain'] = 'far'
    return endpoint_ports(config, id_width=ID_BITS, len_width=len_bits(mode))


OKAY, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3


class Model:
    """Independent AMBA reading of the exclusive contract for sequential steps.

    A reservation per {source, AxID} key and target; network monitors round
    the region out to the granule, a target-mode slave keeps the exact bytes.
    Capacity is not modelled: episodes that exceed it check counts instead."""

    def __init__(self, mode):
        self.mode = mode
        self.targets = targets(mode)
        self.memory = {t: {} for t in self.targets}
        self.reservations = {t: {} for t in self.targets}
        self.failed = set()  # targets whose timeout expired

    def kind(self, target):
        return self.targets[target][2]

    def granule(self, target):
        return granule(self.mode)

    def region(self, target, offset, total):
        if self.kind(target) != 'network':
            return offset, total
        size = max(total, self.granule(target))
        return offset & ~(size - 1), size

    def word_limit(self, target):
        protocol = self.targets[target][0]
        return target_width(self.mode, target) // 8 if protocol in ('axi4', 'ahb_v3') else 4

    def admitted(self, source, target, offset, length, size, burst):
        """The exclusive envelope the ingress enforces for an exclusive target:
        at most 16 power-of-two beats, aligned to their total bytes, within
        the window's byte limit, and not FIXED (AXI A7.2.4)."""
        beats = length + 1
        total = beats << size
        if self.kind(target) is None:
            return True
        if beats > 16 or beats & (beats - 1) or burst == 0 or offset % total:
            return False
        # At most 128 bytes, 2^len_bits target words (the target's AxLEN, else
        # the network length field), 16 words at an AHB target (its packet
        # limit, which an exclusive burst is never cut across) and the target's
        # own window.
        limit = min(128, (1 << len_bits(self.mode)) * self.word_limit(target), WINDOW)
        if self.targets[target][0] == 'ahb_v3' or self.kind(target) == 'target':
            limit = min(limit, 16 * self.word_limit(target))
        if total > limit:
            return False
        if self.targets[target][0] in ('apb3', 'apb2') and size < 2:
            return False
        return True

    def clear(self, target, offset, total):
        for key, (base, span) in list(self.reservations[target].items()):
            if base < offset + total and offset < base + span:
                del self.reservations[target][key]

    def read(self, source, target, offset, length, size, ident, lock, burst=1):
        total = (length + 1) << size
        if lock and not self.admitted(source, target, offset, length, size, burst):
            return [SLVERR] * (length + 1)
        # The monitor records at the request, a failed target included.
        if lock and self.kind(target):
            self.reservations[target][(source, ident)] = self.region(target, offset, total)
        if target in self.failed:
            return [SLVERR] * (length + 1)
        return [EXOKAY if lock and self.kind(target) else OKAY] * (length + 1)

    def write(self, source, target, offset, length, size, ident, lock, data, burst=1):
        """data: byte offset -> value. Returns the B response and whether the
        target was written."""
        total = (length + 1) << size
        if lock and not self.admitted(source, target, offset, length, size, burst):
            return SLVERR, False
        exclusive = bool(lock and self.kind(target))
        if exclusive:
            held = self.reservations[target].get((source, ident))
            if held != self.region(target, offset, total):
                return OKAY, False
        # The bytes the burst covers: the WRAP container, the FIXED beat.
        low = offset & ~(total - 1) if burst == 2 else offset
        self.clear(target, low, (1 << size) if burst == 0 else total)
        if target in self.failed:
            return SLVERR, False
        self.memory[target].update(data)
        return (EXOKAY if exclusive else OKAY), True

    def word(self, target, offset):
        return sum(self.memory[target].get(offset + k, 0) << (8 * k) for k in range(4))

    def reset(self):
        for target in self.targets:
            self.reservations[target].clear()
        self.failed.clear()
