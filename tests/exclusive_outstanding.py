"""Exclusive access with several bursts in flight per source: configurations.

Two 32-bit AXI4 sources keep four bursts across reads and writes (AxID 2 bits, so up to
four IDs in flight at once) and reach exclusive targets whose monitors key on
{source, AxID}. Modes:

  graph   a one-node graph:
          tm  AXI4, network monitor, two bursts in flight
          tx  AXI4, target mode, two shared packet slots (the bench's slave
              keeps its own monitor per target ID)
          tp  APB4, network monitor
          tn  AXI4 without exclusive support (AxLOCK ignored)
  funnel  the generated two-source two-target switch: tm (network) and tx
          (target mode, one shared packet slot).

The bench in exclusive_outstanding_bench.py owns the transactions and checks."""
from endpoint_ports import endpoint_ports

# name: protocol, window base, exclusive mode, shared capacity
GRAPH_TARGETS = {
    'tm': ('axi4', 0x10000, 'network', 2),
    'tx': ('axi4', 0x20000, 'target', 2),
    'tp': ('apb4', 0x30000, 'network', 1),
    'tn': ('axi4', 0x40000, None, 1),
}
FUNNEL_TARGETS = {
    'tm': ('axi4', 0x10000, 'network', 1),
    'tx': ('axi4', 0x20000, 'target', 1),
}
MODES = ('graph', 'funnel')
SOURCES = ('s0', 's1')
ID_BITS = 2
CAPACITY = 4
WINDOW = 0x1000


def targets(mode):
    return GRAPH_TARGETS if mode == 'graph' else FUNNEL_TARGETS


def configuration(mode, capacity=CAPACITY):
    chosen = targets(mode)
    target_list = []
    for name, (protocol, base, exclusive, outstanding) in chosen.items():
        entry = {'name': name, 'clock_domain': 'main', 'protocol': protocol, 'data_width': 32,
                 'mappings': [{'base': hex(base), 'size': hex(WINDOW)}]}
        if exclusive:
            entry['exclusive'] = exclusive
            entry['exclusive_id_flags'] = ['id0', 'id1', 'source']
        if outstanding > 1:
            entry['max_outstanding'] = outstanding
        target_list.append(entry)
    # The owner of an exclusive access: {source, AxID}, the bench drives AxUSER with AxID.
    initiators = [{'name': s, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                   'user_width': ID_BITS,
                   'user_flags': {'id0': 'user[0]', 'id1': 'user[1]', 'source': index}}
                  for index, s in enumerate(SOURCES)]
    config = {'schema_version': 10, 'name': 'exclusive_outstanding', 'address_width': 32,
              'arbitration': {'policy': 'round_robin'},
              'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
              'initiators': initiators, 'targets': target_list,
              'transport': {'max_outstanding': capacity,
                            'transport_word_bytes': 4, 'response_codes': 'distinct',
                            'user_flags': ['id0', 'id1', 'source']},
              'connectivity': 'all',
              'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if mode == 'graph':
        config['topology'] = {
            'request': {'nodes': [{'name': 'q0'}], 'links': [],
                        'entries': [{'endpoint': s, 'node': 'q0'} for s in SOURCES],
                        'exits': [{'endpoint': t, 'node': 'q0'} for t in chosen]},
            'response': {'nodes': [{'name': 'p0'}], 'links': [],
                         'entries': [{'endpoint': t, 'node': 'p0'} for t in chosen],
                         'exits': [{'endpoint': s, 'node': 'p0'} for s in SOURCES]}}
    return endpoint_ports(config, id_width=ID_BITS, len_width=4)
