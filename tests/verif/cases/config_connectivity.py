"""Configuration checks of connectivity:
for every matrix of the transaction case the generated switch carries exactly
the matrix and its inverse, the definition records it and the topology is
one generated switch per direction; the shipped axi4_connectivity example
routes its sparse 3x4 matrix with arbiters only where two sources meet; and
every AXI4 example without a topology, plus a wide-target and a
several-bursts configuration, generates the same bytes as an explicit
one-node graph per direction. A named default target (on the band matrix):
naming the implicit one changes only the map, naming another target s0
reaches changes the Verilog and the map records it, naming a target s0 does
not reach or an unknown one is rejected. The transaction checks of a named
default target are in target_timeouts.py."""

import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from endpoint_ports import endpoint_ports  # noqa: E402

_spec = importlib.util.spec_from_file_location("nlv_config_connectivity_case",
                                               HERE / "connectivity.py")
CONN = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CONN)
EXAMPLES = HERE.parents[2] / "examples"


def routes_check(matrix):
    want = {(f's{s}', f't{t}') for s, t in matrix}

    def check(mapping, out, peers):
        net = mapping['network']
        assert {(r['entry'], r['exit']) for r in net['request_routes']} == want, "request routes"
        assert {(r['exit'], r['entry']) for r in net['response_routes']} == want, "response routes"
        assert {(p['source'], p['target'])
                for p in mapping['definition']['connectivity']} == want, "definition"
        assert mapping['definition']['topology']['generated'] == 'one_switch_per_direction'
    return check


def example_check(mapping, out, peers):
    want = {('cpu', t) for t in ('ram', 'rom', 'uart', 'timer')} | {
        ('dma', 'ram'), ('dbg', 'uart'), ('dbg', 'timer')}
    assert {(r['entry'], r['exit']) for r in mapping['network']['request_routes']} == want
    grants = {r['name'] for r in mapping['network']['resources'] if r['role'] == 'packet_grant'}
    # ram, uart and timer take two sources each; rom has one and no arbiter.
    assert {g.split('endpoint/')[1].split('/')[0] for g in grants if 'graph/req/' in g} == \
        {'ram', 'uart', 'timer'}, sorted(grants)


def one_switch(text):
    """The same YAML with an explicit one-node graph per direction."""
    def names(section):
        start = re.search(r'^' + section + r':', text, re.M).end()
        end = re.search(r'^\w', text[start:], re.M)
        body = text[start:start + end.start()] if end else text[start:]
        return re.findall(r'\bname: (\w+)', body)
    sources, targets = names('initiators'), names('targets')

    def side(entries, exits, node):
        return (f'    nodes: [{{name: {node}}}]\n    links: []\n'
                f'    entries: [{", ".join(f"{{endpoint: {e}, node: {node}}}" for e in entries)}]\n'
                f'    exits: [{", ".join(f"{{endpoint: {e}, node: {node}}}" for e in exits)}]\n')
    topology = ('topology:\n  request:\n' + side(sources, targets, 'q') + '  response:\n' +
                side(targets, sources, 'p'))
    return re.sub(r'^output:', lambda _: topology + 'output:', text, count=1, flags=re.M)


def inline(value):
    if isinstance(value, dict):
        return '{' + ', '.join(f'{k}: {inline(v)}' for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(inline(v) for v in value) + ']'
    if isinstance(value, str) and value.startswith('0x'):
        return f"'{value}'"
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def block(config):
    """A configuration dict as block YAML (quoted window strings)."""
    lines = []
    for key, value in config.items():
        if isinstance(value, list):
            lines.append(f'{key}:')
            lines += ['  - {' + ', '.join(f'{k}: {inline(v)}' for k, v in item.items()) + '}'
                      for item in value]
        else:
            lines.append(f'{key}: {inline(value)}')
    return '\n'.join(lines) + '\n'


# Without a topology beyond the examples: a target at its own width beside an
# APB4 target with an AXI4-Lite source, and several bursts in flight at both
# ends beside an AHB target.
EXTRA = {
    'wide_target': {
        'schema_version': 10, 'name': 'wide', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [{'name': 'a', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32},
                       {'name': 'b', 'clock_domain': 'main', 'protocol': 'axi4_lite',
                        'data_width': 32}],
        'targets': [{'name': 'mem', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128,
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]},
                    {'name': 'regs', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
                     'mappings': [{'base': '0x10000', 'size': '0x100'}]}],
        'transport': {'response_codes': 'distinct'},
        'connectivity': {'a': ['mem', 'regs'], 'b': ['regs']},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}},
    'outstanding': {
        'schema_version': 10, 'name': 'queued', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [{'name': 'a', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4},
                       {'name': 'b', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}],
        'targets': [{'name': 'mem', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'max_outstanding': 2, 'mappings': [{'base': '0x0', 'size': '0x10000'}]},
                    {'name': 'rom', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'mappings': [{'base': '0x10000', 'size': '0x10000'}]},
                    {'name': 'uart', 'clock_domain': 'main', 'protocol': 'ahb_v3',
                     'data_width': 32, 'mappings': [{'base': '0x20000', 'size': '0x1000'}]}],
        'transport': {'response_codes': 'distinct'},
        'connectivity': {'a': ['mem', 'rom', 'uart'], 'b': ['mem']},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}},
}

CASES = [dict(name=f"matrix_{name}", config=CONN.configuration(matrix), expect="accept",
              check=routes_check(matrix)) for name, matrix in CONN.MATRICES.items()]
CASES.append(dict(name="example_axi4_connectivity",
                  text=(EXAMPLES / "axi4_connectivity.yaml").read_text(), expect="accept",
                  commands=("generate",), check=example_check))
INPUTS = {p.stem: p.read_text() for p in sorted(EXAMPLES.glob('axi4_*.yaml'))
          if not re.search(r'^topology:', p.read_text(), re.M)}
INPUTS.update({k: block(endpoint_ports(v, id_width=2, len_width=8)) for k, v in EXTRA.items()})
if len(INPUTS) < 8:
    raise ValueError(f"only {sorted(INPUTS)} without a topology")
for name, text in INPUTS.items():
    CASES.append(dict(name=f"switch_{name}_generated", text=text, expect="accept",
                      commands=("generate",)))
    CASES.append(dict(name=f"switch_{name}_explicit", text=one_switch(text), expect="accept",
                      commands=("generate",), same_rtl_as=f"switch_{name}_generated"))


def named_default(target):
    cfg = CONN.configuration(CONN.BAND)
    cfg['initiators'][0]['default_target'] = target
    return cfg


def default_check(target, same):
    """The map lists the named default target. The Verilog never equals the band matrix's: a named
    target adds an aperture to the target IDs where it answers misses (docs/transactions.md#target-identifiers)."""
    def check(mapping, out, peers):
        definition = mapping['definition']
        assert definition['default_targets'] == {'s0': target}, definition.get('default_targets')
        assert 'named_default_target' in definition['contract']['connectivity']
        band = peers['matrix_band']
        mine, theirs = [sorted(d.glob('*.v')) for d in (out, band)]
        equal = [a.read_bytes() for a in mine] == [b.read_bytes() for b in theirs]
        assert equal == same, f"Verilog equal to the implicit default: {equal}"
    return check


# s0 reaches t0 and t1 on the band matrix; t0 (lowest base, equal sizes) is its implicit default.
CASES += [
    dict(name="default_target_implicit", config=named_default('t0'), expect="accept",
         commands=("generate",), check=default_check('t0', True)),
    dict(name="default_target_other", config=named_default('t1'), expect="accept",
         commands=("generate",), check=default_check('t1', False)),
    dict(name="default_target_unreached", config=named_default('t2'), expect="reject",
         code="NCL_CONNECTIVITY_REFERENCE"),
    dict(name="default_target_unknown", config=named_default('t9'), expect="reject",
         code="NCL_CONNECTIVITY_REFERENCE"),
    dict(name="default_target_number", config=named_default(7), expect="reject",
         code="NCL_SCHEMA_TYPE"),
]
