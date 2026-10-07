"""Configuration checks of mode-dependent mappings (docs/address-map.md#address-selectors): the map
records each mapping at its global window with its condition, the address inputs and selectors and the
nets of each selector and of each source's held plane; a definition without modes keeps every byte
(verif_examples); every invalid address input, selector and condition is rejected with its code."""

import copy
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("nlv_config_modes_case", HERE / "mode_remap.py")
REMAP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REMAP)
BASE = REMAP.CFG


def edited(edit):
    cfg = copy.deepcopy(BASE)
    edit(cfg)
    return cfg


def target(cfg, name):
    return next(t for t in cfg['targets'] if t['name'] == name)


def map_check(mapping, out, peers):
    d = mapping['definition']
    rows = {(r['name'], int(r['begin']), int(r['end'])): r.get('when') for r in d['targets']}
    assert rows[('ram', 0, 0x2000)] == {'rom': False}, "ram shared window condition"
    assert rows[('rom', 0, 0x1000)] == {'rom': True}, rows
    assert rows[('dev', 0x4000, 0x5000)] == {'high': True, 'rom': True}, rows
    assert rows[('ram', 0x10000, 0x12000)] is None, rows
    assert [p['name'] for p in d['address_inputs']] == ['bank', 'boot'], d['address_inputs']
    assert {f['name']: f['equals'] for f in d['address_selectors']} == {
        'high': {'bank': 2}, 'rom': {'boot': 1}}, d['address_selectors']
    assert set(mapping['network']['address_selectors']) == {'high', 'rom'}
    planes = {a['source']: a for a in mapping['network']['admission']}
    assert all('write_mode_plane' in planes[s] and 'read_mode_plane' in planes[s]
               for s in ('a0', 'm0', 'l0', 'p0')), planes
    ports = {p['name']: p for p in mapping['ports']}
    assert ports['boot']['endpoint'] == 'address_input:boot' and ports['bank']['width'] == 2, ports
    assert ports['boot']['signal'] == 'address_input', ports


def address_input(**fields):
    return lambda cfg: cfg['address_inputs'][0].update(fields)


def flag(**fields):
    return lambda cfg: cfg['address_selectors'][0].update(fields)


def when(name, condition):
    return lambda cfg: target(cfg, name)['mappings'][0].update(when=condition)


def rejected(name, edit, code):
    return dict(name=name, config=edited(edit), expect="reject", code=code)


CASES = [
    dict(name="remap", config=BASE, expect="accept", check=map_check,
         mutations=[("plain_row", lambda out: _drop_when(out), "ram shared window condition")]),
    # Overlapping mappings of one condition follow the overlap rule of their plane.
    dict(name="nested_condition", config=edited(lambda cfg: target(cfg, 'dev')['mappings'].append(
        {'base': '0x0', 'size': '0x1000', 'when': {'rom': False}})), expect="accept"),
    rejected("port_width", address_input(width=33), "NCL_ADDRESS_INPUT"),
    rejected("port_domain", address_input(clock_domain='far'), "NCL_DOMAIN_REFERENCE"),
    rejected("port_unused", lambda cfg: cfg['address_inputs'].append(
        {'name': 'spare', 'width': 1, 'clock_domain': 'main'}), "NCL_ADDRESS_INPUT"),
    rejected("port_name", address_input(name='test'), "NCL_NAME_COLLISION"),
    rejected("flag_empty", flag(equals={}), "NCL_ADDRESS_SELECTOR"),
    rejected("flag_value", flag(equals={'boot': 2}), "NCL_ADDRESS_SELECTOR"),
    rejected("flag_port", flag(equals={'strap': 1}), "NCL_ADDRESS_SELECTOR"),
    rejected("flag_unused", lambda cfg: cfg['address_selectors'].append(
        {'name': 'spare', 'equals': {'boot': 0}}), "NCL_ADDRESS_SELECTOR"),
    rejected("flag_twice", lambda cfg: cfg['address_selectors'].append(
        {'name': 'rom', 'equals': {'boot': 0}}), "NCL_NAME_COLLISION"),
    rejected("flags_17", lambda cfg: (
        cfg['address_selectors'].extend({'name': f'f{k}', 'equals': {'bank': k % 4}} for k in range(15)),
        target(cfg, 'dev')['mappings'].append({'base': '0x50000', 'size': '0x1000', 'when': {
            f'f{k}': True for k in range(15)}})), "NCL_ADDRESS_SELECTOR"),
    rejected("condition_flag", when('ram', {'boot': True}), "NCL_MODE_CONDITION"),
    rejected("condition_subpage", lambda cfg: target(cfg, 'rom')['mappings'][0].update(
        size='0x100'), "NCL_MODE_CONDITION"),
    # Conditions that can hold together must be equal where mappings overlap.
    rejected("overlap_compatible", when('ram', {'high': True}), "NCL_ADDRESS_OVERLAP"),
    rejected("overlap_always", lambda cfg: target(cfg, 'ram')['mappings'][0].pop('when'),
             "NCL_ADDRESS_OVERLAP"),
    rejected("plane_bits", lambda cfg: (cfg.update(address_width=48), next(
        s for s in cfg['initiators'] if s['name'] == 'p0').update(address_width=32)),
             "NCL_MODE_CONDITION"),
    dict(name="when_empty", text="""schema_version: 10
name: modes
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
address_inputs: [{name: boot, clock_domain: main}]
address_selectors: [{name: rom, equals: {boot: 1}}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]
initiators: [{name: s0, clock_domain: main, protocol: apb4, data_width: 32}]
targets: [{name: t0, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
           mappings: [{base: "0x0", size: "0x1000", when: {}}]}]
connectivity: all
""", expect="reject", code="NCL_SCHEMA_TYPE"),
    dict(name="when_number", text="""schema_version: 10
name: modes
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
address_inputs: [{name: boot, clock_domain: main}]
address_selectors: [{name: rom, equals: {boot: 1}}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]
initiators: [{name: s0, clock_domain: main, protocol: apb4, data_width: 32}]
targets: [{name: t0, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
           mappings: [{base: "0x0", size: "0x1000", when: {rom: 1}}]}]
connectivity: all
""", expect="reject", code="NCL_SCHEMA_TYPE"),
]


def _drop_when(out):
    """Mutation: the map loses the condition of the RAM's shared window."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith('.map.json'))
    mapping = json.loads(path.read_text())
    for row in mapping['definition']['targets']:
        if row['name'] == 'ram' and row['begin'] == '0':
            row.pop('when')
    path.write_text(json.dumps(mapping))
