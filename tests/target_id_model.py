"""The AXI target ID a request takes, from the map's numbers: an independent reading for the
benches, which must not take the source AxID for the target ID."""
import re


def number(labels, name, flow=0):
    """A compact endpoint-flow index from the map."""
    return labels[name][flow]


def on_port(flow, sequence, width, flow_bits):
    """The port value: the flow number in the lowest bits, the sequence ID above it reversed."""
    reversed_sequence = 0
    for k in range(width - flow_bits):
        reversed_sequence |= (sequence >> k & 1) << (width - flow_bits - 1 - k)
    return reversed_sequence << flow_bits | flow


def from_port(value, width, flow_bits):
    """(flow number, sequence ID) of a port value."""
    sequence = 0
    high = value >> flow_bits
    for k in range(width - flow_bits):
        sequence |= (high >> k & 1) << (width - flow_bits - 1 - k)
    return value & (1 << flow_bits) - 1, sequence


def target_id(numbers, source, target, width, axid, write, apb=False):
    """numbers: the map's definition.target_ids; width: the target's ID port bits (one flow)."""
    if not width:
        return 0
    bits = numbers['route_bits']
    direction = bits['direction']
    positions = {f['name']: f['lsb'] for f in numbers['route_fields']}
    at_initiator = positions.get('source_path', 0)
    at_target = positions.get('target_path', 0)
    route = (number(numbers['source_paths'], source) << at_initiator
             | number(numbers['target_paths'], target) << at_target
             | axid << direction
             | (1 if write and direction and not apb else 0))
    mask = (1 << width) - 1
    total = 0
    while route:
        total += route & mask
        route >>= width
    return on_port(0, total & mask, width, 0)


def id_function(name, numbers, ports, axid):
    """A Verilog function name(target, source, read) of every s<k> to every t<k> with an ID port,
    axid(source, read) giving the source's AxID."""
    widths = {int(m[1]): p['width'] for p in ports
              for m in [re.fullmatch(r'nl_t_t(\d+)_arid', p['name'])] if m}
    sources = sorted({int(m[1]) for p in ports for m in [re.match(r'nl_i_s(\d+)_', p['name'])] if m})
    lines = [f'function integer {name}; input integer t,s,read;', ' begin', f'  {name}=0;']
    for t, width in sorted(widths.items()):
        for s in sources:
            if f's{s}' not in numbers['source_paths'] or f't{t}' not in numbers['target_paths']:
                continue
            for read in (0, 1):
                value = target_id(numbers, f's{s}', f't{t}', width, axid(s, read), not read)
                lines.append(f'  if(t=={t} && s=={s} && read=={read}) {name}={value};')
    return '\n'.join(lines + [' end', 'endfunction'])
