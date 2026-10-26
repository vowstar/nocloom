"""The waits of a scheduler control slot, derived from public routes and configuration."""

def check(graph, mapping):
    nodes = graph['nodes']
    index = {(n['kind'], n['key']): i for i, n in enumerate(nodes)}
    edges = {(e[0], e[1], e[2]) for e in graph['edges']}
    sources = mapping['definition']['contract']['arbitration'].get('qos', {}).get('sources', {})
    missing, expected = [], set()
    for source, options in sources.items():
        if 'scheduler' not in options:
            continue
        kinds = ('scheduler.control', 'scheduler.packet', 'source.merge', 'env.source_data')
        ids = [index.get((kind, source)) for kind in kinds]
        if any(i is None for i in ids):
            missing.append('UPGRADE_WAIT_GRAPH_NODE:' + source)
            continue
        control, packet, merge, data = ids
        required = {(control, merge, 'M8'), (packet, control, 'M3'), (data, packet, 'M7')}
        for route in mapping['network']['request_routes']:
            if route['entry'] != source:
                continue
            path = route['hardware_nodes']
            for i, node in enumerate(path):
                after = path[i + 1] if i + 1 < len(path) else route['exit']
                before = 'node/' + path[i - 1] if i else 'endpoint/' + source
                key = 'req/' + node + '>' + after + '/' + before
                held = index.get(('grant.held', key), index.get(('output.wire', key)))
                if held is None:
                    missing.append('UPGRADE_WAIT_GRAPH_PATH:' + key)
                    continue
                required.update(((held, control, 'M2'), (held, packet, 'M2')))
        if not required <= edges:
            missing.append('UPGRADE_WAIT_GRAPH_MISSING:' + source)
        incoming = {e for e in edges if e[1] == control}
        if incoming - required:
            missing.append('UPGRADE_WAIT_GRAPH_UNEXPECTED:' + source)
        expected.update(required)
    return missing, expected
