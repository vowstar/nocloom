"""Endpoint fields that follow each endpoint's protocol in test configurations."""

ATTRIBUTED = ('axi4', 'axi4_lite', 'apb4', 'ahb_v3')


def endpoint_ports(cfg, id_width=None, len_width=None):
    """Give AXI4 initiators id_width and len_width, AXI4 targets len_width,
    and every target with attributes the amba preset, unless an endpoint
    states its own. None leaves the default. Returns cfg."""
    for initiator in cfg.get('initiators', []):
        if initiator.get('protocol') == 'axi4':
            for key, value in (('id_width', id_width), ('len_width', len_width)):
                if value is not None:
                    initiator.setdefault(key, value)
    for target in cfg.get('targets', []):
        if target.get('protocol') == 'axi4' and len_width is not None:
            target.setdefault('len_width', len_width)
        if target.get('protocol') in ATTRIBUTED:
            target.setdefault('attributes', 'amba')
    return cfg


def set_protocol(endpoint, protocol, **fields):
    """Change an endpoint's protocol (and fields) with the fields that follow
    it; a target is the endpoint with a window. Returns the endpoint."""
    endpoint['protocol'] = protocol
    endpoint.update(fields)
    if protocol != 'axi4':
        endpoint.pop('id_width', None)
        endpoint.pop('len_width', None)
    if 'mappings' in endpoint:
        if protocol in ATTRIBUTED:
            endpoint.setdefault('attributes', 'amba')
        else:
            endpoint.pop('attributes', None)
    return endpoint


def port_slots(mapping, endpoint, channel):
    """Words the pipe on AXI channel aw, w, ar, r or b at an endpoint's port holds (one map storage
    control flag per slot; 0 without a pipe). An AXI4 source's AW and AR pipes take a burst its
    engine has not admitted yet, and its W pipe a beat ahead of that beat's AW, so its face holds
    up to max_outstanding plus that many bursts per direction."""
    resource = f'{endpoint}/pipe/{channel}'
    return sum(len(x['control']) for x in mapping['network']['storage'] if x['resource'] == resource)
