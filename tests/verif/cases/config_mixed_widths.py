"""Configuration checks of the per-target width networks: every mode's
interface map (port widths, target data widths, the wide attachment and
outstanding contracts), byte-identical regeneration, and the configurations
around it that stay rejected (illegal and 2048-bit widths, dedicated wide
targets) or are now accepted (a clamped source with several bursts, a
64-bit AXI4-Lite clamp)."""

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import set_protocol  # noqa: E402
import nlverif.widths as widths_source  # noqa: E402

MODES = ('graph', 'clocked', 'net64', 'packets', 'attributes', 'outstanding', 'wide',
         'wide_packets', 'wide_clocked')


def widths_module(mode):
    """A module object of its own per mode: use() switches its tables."""
    spec = importlib.util.spec_from_file_location("nlv_config_widths_" + mode,
                                                  widths_source.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.use(mode)
    return module


def map_check(mode, w):
    wide = mode in w.WIDE_MODES

    def check(mapping, out, peers):
        widths = {p['name']: p['width'] for p in mapping['ports']}
        d = mapping['definition']
        if wide:
            for name, bits in (('nl_i_s1024_wdata', 1024), ('nl_i_s512_wstrb', 64),
                               ('nl_t_t32_wdata', 32), ('nl_t_t512_rdata', 512),
                               ('nl_t_t1024_wdata', 1024), ('nl_t_t1024_wstrb', 128),
                               ('nl_t_t1024_awsize', 3)):
                assert widths[name] == bits, (name, widths[name])
            assert d['target_data_widths'] == {'t256': 256, 't512': 512, 't1024': 1024}
            assert 'widest_endpoint_1024_bits' in d['contract']['endpoint_widths'][
                'wide_attachment']
        else:
            pairs = [('nl_i_s64_wdata', 64), ('nl_t_t32_wdata', 32), ('nl_t_t128_rdata', 128),
                     ('nl_t_t256_wstrb', 32)]
            pairs += [('nl_i_s64_awlen', 4)] if 's32' not in w.SOURCES else \
                [('nl_i_s256_rdata', 256)]
            for name, bits in pairs:
                assert widths[name] == bits, (name, widths[name])
            assert d['target_data_widths'] == ({'t128': 128, 't256': 256} if 's32' in w.SOURCES
                                               else {'t32': 32, 't128': 128, 't256': 256})
        if mode == 'outstanding':
            assert d['outstanding']['initiators']['s32'] == 4, d.get('outstanding')
        else:
            assert 'outstanding' not in d
    return check


CASES = []
for mode in MODES:
    w = widths_module(mode)
    config = w.configuration(mode)
    wide = mode in w.WIDE_MODES
    CASES.append(dict(name=f"{mode}", config=config, expect="accept", check=map_check(mode, w)))
    lite = [('lite_wide_to_narrow', lambda d: set_protocol(d['initiators'][0], 'axi4_lite'))]
    # 2048-bit endpoints stay rejected (AxSIZE tops out at 128 bytes), and
    # dedicated targets stay at 32 bits.
    beyond = [('target_2048', lambda d: d['targets'][3].update(data_width=2048)),
              ('source_2048', lambda d: d['initiators'][3].update(data_width=2048)),
              ('dedicated_1024', lambda d: d['targets'][0].update(protocol='axi4_lite',
                                                                  data_width=1024))]
    # A source with several bursts that reaches a narrower target is clamped.
    clamped = [('outstanding_clamped', lambda d: d['initiators'][1].update(max_outstanding=2))]
    for label, edit in (lite if mode == 'net64' else []) + (beyond if wide else []) + (
            clamped if mode == 'outstanding' else []) + [
            ('illegal_width', lambda d: d['targets'][1].update(data_width=96)),
            ('dedicated_wide', lambda d: d['targets'][0].update(protocol='apb4',
                                                                data_width=128))]:
        bad = json.loads(json.dumps(config))
        edit(bad)
        accepted = label in ('outstanding_clamped', 'lite_wide_to_narrow')
        CASES.append(dict(name=f"{mode}_{label}", config=bad,
                          expect="accept" if accepted else "reject", commands=("generate",)))
