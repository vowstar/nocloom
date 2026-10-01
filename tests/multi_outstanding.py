"""Multiple outstanding bursts through the public CLI: configurations.

Two AXI4 sources reach AXI4 targets with and without several bursts in
flight, an APB4 target and an AXI4 target with a timeout. Modes:

  graph     explicit single-node graph with connectivity all: a decode miss
            answers DECERR from the source's default target.
  routes    the same graph with a sparse connectivity matrix: s0 may not
            reach tp, which answers DECERR from the default target like a miss.
  funnel    two sources and two targets without a topology (the generated
            one-switch crossbar); the multi-burst target is first by name.
  first     the graph with the multi-burst target first by name, plus an
            AXI4-Lite target (the tag through a Lite outlet).
  extremes  no AxID (id_bits 0), 4-bit AxLEN, a source with 64 shared burst
            slots and a target with 16 shared packet slots.
  many      six AxID bits and 64 shared burst slots (64 tracked IDs) over
            four 16-burst AXI4 targets, a timed AXI4 target and an APB4
            target: 64 bursts of four IDs in flight at four targets at once.
  clocked, clocked_slow, clocked_fast
            the graph mode as a clocked graph: the sources and the request
            switch in domain main (10 ns), the targets and the response switch
            in domain far, so every request and response crosses one
            asynchronous FIFO with the response tag in its payload. far runs
            at 14 ns with a phase offset (clocked), at 30 ns (clocked_slow)
            or at 4 ns (clocked_fast).
  clocked_extremes
            extremes (64 shared burst slots, 6-bit tag, a 16-burst target)
            across the same two domains as clocked.
  clocked_entry
            the graph with s0 alone in domain entry (7 ns): its requests and
            responses cross at the network's entry and exit; the targets and
            both switches stay in main.

The routes mode also sets transport.id_table_entries to 2, so a source with
more {direction, ID} keys in flight than its table holds must wait for an
entry.

The bench in multi_outstanding_bench.py owns the transactions and checks."""
from endpoint_ports import endpoint_ports

# name: kind, window base, window size, shared capacity, timeout cycles
TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, 1, None),
    'tb': ('axi4', 0x20000, 0x10000, 4, None),
    'tp': ('apb4', 0x30000, 0x1000, 1, None),
    'tt': ('axi4', 0x40000, 0x10000, 1, 1024),
}
FUNNEL_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, 2, None),
    'tb': ('axi4', 0x20000, 0x10000, 1, None),
}
FIRST_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, 4, None),
    'tb': ('axi4', 0x20000, 0x10000, 1, None),
    'tp': ('apb4', 0x30000, 0x1000, 1, None),
    'tt': ('axi4', 0x40000, 0x10000, 1, 1024),
    'tl': ('axi4_lite', 0x80000, 0x1000, 1, None),
}
EXTREME_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, 16, None),
    'tb': ('axi4', 0x20000, 0x10000, 1, None),
    'tp': ('apb4', 0x30000, 0x1000, 1, None),
}
# name: shared capacity (None inherits transport.max_outstanding)
SOURCES = {'s0': None, 's1': 2}
# Pairs the routes mode leaves out of its connectivity matrix.
DENIED = {('s0', 'tp')}
MANY_TARGETS = {
    'ta': ('axi4', 0x10000, 0x10000, 16, None),
    'tb': ('axi4', 0x20000, 0x10000, 16, None),
    'tc': ('axi4', 0x50000, 0x10000, 16, None),
    'td': ('axi4', 0x60000, 0x10000, 16, None),
    'tp': ('apb4', 0x30000, 0x1000, 1, None),
    'tt': ('axi4', 0x40000, 0x10000, 1, 1024),
}
MODES = ('graph', 'routes', 'funnel', 'first', 'extremes', 'many', 'clocked', 'clocked_slow',
         'clocked_fast', 'clocked_extremes', 'clocked_entry')
# Clocked modes: far domain (period ns, phase offset ns, synchronizer
# stages of every crossing); main is 10 ns.
CLOCKED = {'clocked': (14, 3, 3), 'clocked_slow': (30, 7, 4), 'clocked_fast': (4, 1, 2),
           'clocked_extremes': (14, 3, 3), 'clocked_packets': (14, 3, 3)}
# Entry modes: s0 alone in domain entry (period ns, phase offset ns).
ENTRY = {'clocked_entry': (7, 2)}


def base(mode):
    """The mode whose targets, IDs and capacities a clocked mode uses."""
    if mode == 'clocked_extremes':
        return 'extremes'
    return 'graph' if mode in CLOCKED or mode in ENTRY else mode


def crossing(mode):
    """Some burst crosses a clock domain."""
    return mode in CLOCKED or mode in ENTRY


def source_clock(mode, source):
    return 'clk_entry' if mode in ENTRY and source == 's0' else 'clk_main'


def clocked(mode):
    return mode in CLOCKED


def target_clock(mode):
    """The clock of the target models (and of the target-side checks)."""
    return 'clk_far' if clocked(mode) else 'clk_main'


def targets(mode):
    if mode == 'clocked_packets':
        return {name: (kind, base, size, 1, cycles)
                for name, (kind, base, size, _capacity, cycles) in TARGETS.items()}
    return {'funnel': FUNNEL_TARGETS, 'first': FIRST_TARGETS,
            'extremes': EXTREME_TARGETS, 'many': MANY_TARGETS}.get(base(mode), TARGETS)


def global_outstanding(mode):
    return 64 if base(mode) in ('extremes', 'many') else 4


def id_bits(mode):
    return {'extremes': 0, 'many': 6}.get(base(mode), 2)


def global_ids(mode):
    """transport.id_table_entries of the mode, None when absent."""
    return 2 if base(mode) == 'routes' else None


def len_bits(mode):
    return 4 if base(mode) == 'extremes' else 8


def capacity(mode, source):
    return SOURCES[source] if SOURCES[source] is not None else global_outstanding(mode)


def tracked_ids(mode, source):
    """The {direction, AXI ID} entries of a source's ID table (both directions)."""
    bound = min(capacity(mode, source), 2 << id_bits(mode))
    return global_ids(mode) if global_ids(mode) is not None else bound


def configuration(mode, single=False):
    """single: the same network with one shared burst slot at each source and target."""
    chosen = targets(mode)
    target_list = []
    far = 'far' if clocked(mode) else 'main'
    for name, (kind, base, size, outstanding, cycles) in chosen.items():
        entry = {'name': name, 'clock_domain': far, 'protocol': kind, 'data_width': 32,
                 'mappings': [{'base': hex(base), 'size': hex(size)}]}
        if outstanding != 1 and not single:
            entry['max_outstanding'] = outstanding
        if cycles:
            entry['timeout'] = {'cycles': cycles, 'completion': True}
        target_list.append(entry)
    initiators = []
    for name, value in SOURCES.items():
        entry = {'name': name, 'clock_domain': source_clock(mode, name)[len('clk_'):],
                 'protocol': 'axi4', 'data_width': 32}
        if value is not None:
            entry['max_outstanding'] = 1 if single else value
        initiators.append(entry)
    transport = {'max_outstanding': 1 if single else global_outstanding(mode),
                 'transport_word_bytes': 4, 'response_codes': 'distinct'}
    if global_ids(mode) is not None:
        transport['id_table_entries'] = global_ids(mode)
    config = {'schema_version': 10, 'name': 'outstanding', 'address_width': 32,
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
    if clocked(mode):
        period, _phase, stages = CLOCKED[mode]
        config['clock_domains'] = [
            {'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': 10000},
            {'name': 'far', 'clock_port': 'clk_far', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': period * 1000}]
        config['synchronizer_stages'] = stages
        config['topology']['request']['nodes'][0]['clock_domain'] = 'main'
        config['topology']['response']['nodes'][0]['clock_domain'] = 'far'
    if mode in ENTRY:
        period, _phase = ENTRY[mode]
        config['clock_domains'] = [
            {'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': 10000},
            {'name': 'entry', 'clock_port': 'clk_entry', 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': period * 1000}]
        config['topology']['request']['nodes'][0]['clock_domain'] = 'main'
        config['topology']['response']['nodes'][0]['clock_domain'] = 'main'
    return endpoint_ports(config, id_width=id_bits(mode), len_width=len_bits(mode))
