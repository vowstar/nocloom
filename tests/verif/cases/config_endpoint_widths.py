"""Configuration, metadata and Core API checks of unequal and wide AXI4
source widths.

Every configuration generates through the CLI with the expected interface
map and through a C++ definition built on the Core library (a build-tree
tool, see "api") with the same bytes; the metadata checks of the downsizing
networks have negative controls (mutations of the definition that the
check must name); unsupported widths and shapes are rejected with their
codes."""

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nlv_config_endpoint_widths", Path(__file__).resolve().parent / "endpoint_widths.py")
EW = importlib.util.module_from_spec(_spec)
EW.PARAMS = dict(mode="data")
_spec.loader.exec_module(EW)
TOP = "nocloom_axi4_public"


def data_check(widths, lite):
    def check(mapping, out, peers):
        bundle = json.loads((out / f"{TOP}.bundle.json").read_text())
        assert mapping['definition'] == bundle['definition'], "definition identity"
        assert mapping['definition']['endpoint_data_widths'] == {
            'initiators': dict(zip(['s0', 's1'], widths, strict=True)),
            'targets': {'t0': 32, 't1': 32},
            'transport': 'per_endpoint_attachment'}, "endpoint data widths"
        ports = {p['name']: p['width'] for p in mapping['ports']}
        for i, width in enumerate(widths):
            for name, bits in (('wdata', width), ('rdata', width), ('wstrb', width // 8)):
                assert ports[f'nl_i_s{i}_{name}'] == bits, "port width"
            assert (f'nl_i_s{i}_awlen' not in ports) == (lite and i == 1), "lite ports"
    return check


def inspect(width):
    """The downsizing network's metadata: wide sources attach at their own width and clamp
    their bursts to the 32-bit targets (run_axi4_downsize_public.inspect)."""
    def check(mapping, out, peers):
        expected = {TOP + s for s in ('.v', '.map.json', '.dependency.json', '.bundle.json')} | {'nocloom_clock_gate.v',
                                                                     'nocloom_synchronizer.v'}
        assert {p.name for p in out.iterdir() if p.is_file()} == expected, 'artifact set'
        bundle = json.loads((out / f"{TOP}.bundle.json").read_text())
        assert mapping['definition'] == bundle['definition'], 'definition identity'
        d = mapping['definition']
        assert 'data_width' not in d and \
            d['transport']['data_width'] == 32, 'widths'
        assert d['endpoint_data_widths'] == {
            'initiators': {'s0': width, 's1': width}, 'targets': {'t0': 32, 't1': 32},
            'transport': 'per_endpoint_attachment'}, 'conversion contract'
        assert 'target_data_widths' not in d, 'target widths'
        widths = d['contract']['endpoint_widths']
        assert widths['wide_to_narrow'] == \
            'burst_clamped_to_the_width_of_the_target_at_its_start_address', 'clamp contract'
        assert widths['clamp_reject'] == \
            'FIXED_wider_than_target_or_exclusive_burst_needing_more_than_one_piece_SLVERR_' \
            'no_target_access_window_decode_first', 'clamp contract'
        assert 'narrow_to_wide' not in widths, 'no target merge with uniform target widths'
        assert mapping['module'] == bundle['top'] == TOP, 'top'
        assert bundle['synthesis_sources'] == bundle['simulation_sources'] == [
            'nocloom_clock_gate.v', 'nocloom_synchronizer.v', TOP + '.v'], \
            'sources'
        assert {x['path'] for x in bundle['files']} == expected, 'bundle files'
        ports = {p['name']: p for p in mapping['ports']}
        assert len(ports) == len(mapping['ports']), 'unique ports'
        for side, bus in (('i', width), ('t', 32)):
            for i in range(2):
                prefix = f'nl_{side}_{"s" if side == "i" else "t"}{i}_'
                for signal, bits in (('wdata', bus), ('rdata', bus), ('wstrb', bus // 8),
                                     ('awlen', 8), ('arlen', 8), ('awid', 4), ('arid', 4)):
                    assert ports[prefix + signal]['width'] == bits, 'port width'
    return check


def mutate(change):
    """Edits the definition in every JSON artifact of a copy."""
    def edit(directory):
        for f in [*directory.glob('*.map.json'), *directory.glob('*.bundle.json')]:
            obj = json.loads(f.read_text())
            change(obj['definition'])
            f.write_text(json.dumps(obj))
    return edit


def edited(widths, lite=False, **changes):
    cfg = EW.configuration(widths, lite)
    for key, value in changes.items():
        if key == 'targets':
            for t in cfg['targets']:
                t.update(value)
        elif key == 'initiators':
            for i in cfg['initiators']:
                if i['protocol'] == 'axi4':
                    i.update(value)
        else:
            cfg[key].update(value)
    return cfg


CASES = []
for label, widths, lite in EW.DATA_SETS:
    CASES.append(dict(name=f"data_{label}", config=EW.configuration(widths, lite),
                      expect="accept", check=data_check(widths, lite),
                      api=["{tool:endpoint_data}", "{out}", *widths,
                           "lite" if lite else "full"]))
    CASES.append(dict(name=f"data_{label}_2048", config=EW.configuration((2048, widths[1]), lite),
                      expect="reject", code="NCL_ENDPOINT_DATA_WIDTH", commands=("generate",)))
for width in (64, 128, 256, 512, 1024):
    pair = (width, width)
    CASES.append(dict(
        name=f"downsize_{width}", config=EW.configuration(pair, False), expect="accept",
        check=inspect(width), api=["{tool:public}", "{out}", width, 32, "downsize"],
        mutations=[
            ("transport_data_width",
             mutate(lambda d, w=width: d['transport'].update(data_width=w)), 'widths'),
            ("source_bits",
             mutate(lambda d: d['endpoint_data_widths']['initiators'].update(s0=32)),
             'conversion contract')]))
    for name, cfg, code in (
            ('other_width', EW.configuration((2048, 2048), False), 'NCL_ENDPOINT_DATA_WIDTH'),
            # Targets wider than 32 bits: a 64-bit network (no wide-to-32
            # conversion), or 64-bit APB4 targets, which keep 32-bit data.
            ('other_target', edited(pair, targets=dict(protocol='apb4', data_width=64))
             if width == 64 else edited(pair, targets=dict(data_width=64)),
             'NCL_APB_BURST_OUTLET_PROFILE' if width == 64 else 'NCL_WIDTH_CONVERSION'),
            # Four-bit 32-bit targets take a source beat of at most 16 words.
            ('length', edited(pair, targets=dict(len_width=4), initiators=dict(len_width=4)),
             'NCL_ENDPOINT_TRANSACTION_WIDTH' if width == 1024 else None),
            ('lite', EW.configuration(pair, True),
             None if width == 64 else 'NCL_UNSUPPORTED_PROTOCOL')):
        CASES.append(dict(name=f"downsize_{width}_{name}", config=cfg,
                          expect="reject" if code else "accept", code=code,
                          commands=("generate",)))
    CASES.append(dict(
        name=f"downsize_{width}_capacity", config=edited(pair, transport=dict(max_outstanding=2)),
        expect="accept", commands=("generate",),
        check=lambda m, o, p: None
        if 'clamp_outstanding' in m['definition']['contract']['endpoint_widths']
        else (_ for _ in ()).throw(AssertionError('clamp outstanding'))))
