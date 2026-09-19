"""The AXI target ID a request takes, read from the map's definition.target_ids as a user reads
it: an independent reading for the benches, which must not take the source AxID for the target ID.
A port lists its fields, uses the folded route number, or carries only the target flow."""
import re


def number(labels, name, flow=0):
    """A compact endpoint-flow index from the map."""
    return labels[name][flow]


def folded_sequence(numbers, source, target, width, axid, write, apb=False, source_path=0,
                    target_path=0):
    """The XOR of the width-bit slices of the transaction word."""
    bits = numbers['route_bits']
    direction = bits['direction']
    positions = {f['name']: f['lsb'] for f in numbers['route_fields']}
    route = (number(numbers['source_paths'], source, source_path) << positions.get('source_path', 0)
             | number(numbers['target_paths'], target, target_path) << positions.get('target_path', 0)
             | axid << direction
             | (1 if write and direction and not apb else 0))
    mask = (1 << width) - 1
    sequence = 0
    while route:
        sequence ^= route & mask
        route >>= width
    return sequence


def source_index(port, source, source_path=0):
    """The index of an initiator flow among those that reach the target, None when it does not."""
    for index, flow in enumerate(port['sources']):
        if flow['source'] == source and flow['source_path'] == source_path:
            return index
    return None


def compose(port, flow, index, axid):
    """The port value: each field the port lists holds the low bits of its part."""
    parts = {'flow': flow, 'source_index': index, 'axid': axid}
    value = 0
    for field in port['fields']:
        value |= (parts[field['name']] & (1 << field['width']) - 1) << field['lsb']
    return value


def target_id(numbers, source, target, axid, write=False, apb=False, flow=0, source_path=0):
    """The ID for a request of one initiator flow, from the map's definition.target_ids."""
    if 'ports' not in numbers:
        return flow
    port = numbers['ports'][target]
    if port.get('folded'):
        width = port['width']
        flow_bits = (len(numbers['target_paths'][target]) - 1).bit_length()
        if width == flow_bits:
            return flow
        sequence = folded_sequence(numbers, source, target, width - flow_bits, axid, write,
                                   apb, source_path=source_path, target_path=flow)
        return sequence << flow_bits | flow
    return compose(port, flow, source_index(port, source, source_path), axid)


def id_function(name, numbers, ports, axid):
    """A Verilog function name(target, source, read) of every s<k> to every t<k> with an ID port,
    axid(source, read) giving the source's AxID. Both endpoints use their first flow."""
    targets = sorted(int(m[1]) for p in ports
                     for m in [re.fullmatch(r'nl_t_t(\d+)_arid', p['name'])] if m)
    sources = sorted({int(m[1]) for p in ports for m in [re.match(r'nl_i_s(\d+)_', p['name'])] if m})
    layouts = numbers.get('ports')
    lines = [f'function integer {name}; input integer t,s,read;', ' begin', f'  {name}=0;']
    for t in targets:
        target = f't{t}'
        if layouts is not None and target not in layouts:
            continue
        port = layouts[target] if layouts is not None else None
        for s in sources:
            source = f's{s}'
            if source not in numbers['source_paths']:
                continue
            if port is not None and not port.get('folded') and source_index(port, source) is None:
                continue
            for read in (0, 1):
                value = target_id(numbers, source, target, axid(s, read), not read)
                lines.append(f'  if(t=={t} && s=={s} && read=={read}) {name}={value};')
    return '\n'.join(lines + [' end', 'endfunction'])


def source_id_bits(cfg, initiator):
    """The ID bits an initiator sends into the network."""
    protocol = initiator.get('protocol', 'axi4')
    if protocol in ('axi4', 'axi3'):
        return initiator.get('id_width', 4)
    if protocol == 'tl_ul':
        return initiator.get('source_width', 4)
    if protocol == 'tl_uh':
        capacity = initiator.get('max_outstanding', cfg.get('transport', {}).get('max_outstanding', 1))
        return max(1, (capacity - 1).bit_length())
    return 0


def reaching(cfg, target):
    """(initiator, flow) of every initiator flow connected to a target: initiators in byte order
    of their names, the flows of each in local order."""
    flows = []
    for initiator in sorted(cfg['initiators'], key=lambda i: i['name'].encode()):
        count = max(1, len(initiator.get('route_classes', [])))
        connectivity = cfg.get('connectivity', 'all')
        if connectivity == 'all':
            reached = set(range(count))
        else:
            reached = set()
            for entry in connectivity.get(initiator['name'], []):
                pair = dict(target=entry) if isinstance(entry, str) else entry
                if pair['target'] == target['name']:
                    reached |= {pair['source_path']} if 'source_path' in pair else set(range(count))
        flows += [(initiator, flow) for flow in sorted(reached)]
    return flows


def layout(cfg, target, width):
    """The rule itself, from a plain definition (endpoints in full, connectivity 'all' or by
    initiator): what definition.target_ids.ports must say of a target whose ID port has `width`
    bits. From the lowest bit: F bits of the target flow, P bits of the index of the initiator
    flow, W bits of AxID; a port narrower than F + P + W is folded."""
    flows = reaching(cfg, target)
    wanted = (('flow', (target.get('flows', 1) - 1).bit_length()),
              ('source_index', (len(flows) - 1).bit_length() if flows else 0),
              ('axid', max([source_id_bits(cfg, initiator) for initiator, _ in flows] or [0])))
    # A port that cannot hold the fields keeps the folded form.
    if width < sum(bits for _, bits in wanted):
        return dict(width=width, folded=True)
    fields, at = [], 0
    for name, bits in wanted:
        if bits:
            fields.append(dict(name=name, lsb=at, width=bits))
        at += bits
    return dict(width=width, fields=fields,
                sources=[dict(source=initiator['name'], source_path=flow)
                         for initiator, flow in flows])
