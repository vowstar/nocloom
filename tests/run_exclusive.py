"""Exclusive access through the public CLI: generation, contract, rejection
matrix, byte stability of the default, then the transaction bench; the graph
mode then runs the same bench on the clocked graph of every CLOCKS ratio and
with the network target tm keeping several bursts in flight."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from exclusive import (CLOCKS, MODES, OUTSTANDING, OWNER_FLAGS, SPLIT_MODES, configuration,
                       granule, targets)
from exclusive_bench import write_bench
from endpoint_ports import set_protocol
from sim_engine import generated_rtl


def generate(cli, yaml, dest):
    return subprocess.run([cli, 'generate', str(yaml), '--out-dir', str(dest)],
                          capture_output=True, text=True, timeout=300, check=False)


def hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def contract(mode, mapping, outstanding=None):
    definition = mapping['definition']
    chosen = targets(mode)
    expected = {}
    for name, (_protocol, _base, kind, extra) in chosen.items():
        if kind == 'target':
            expected[name] = {'mode': 'target', 'owner_flags': OWNER_FLAGS}
        elif kind == 'network':
            # Default reservations: the values of the owner flags, at most 16.
            expected[name] = {'mode': 'network', 'reservations': extra.get('exclusive_monitor', {}).get('reservations', 8),
                              'granule_bytes': granule(mode), 'owner_flags': OWNER_FLAGS}
    if mode in SPLIT_MODES:
        assert definition['contract']['packets']['narrow_exclusive'] == (
            'narrow_bursts_merge_modifiable_sources_never_send_a_narrow_INCR_with_AxLOCK_set_one_'
            'beat_per_packet_packet_limits_still_cut_it_at_targets_without_exclusive')
        assert definition['transport']['narrow_bursts'] == ['s0']
    else:
        # Packets without a splitting source keep the plain contract.
        assert 'narrow_exclusive' not in definition['contract']['packets']
    if 'packets' in definition['contract']:
        assert 'exclusive_WRAP_sent_as_INCR' in definition['contract']['packets']['wrap']
        assert definition['contract']['packets']['narrow'] == (
            'narrow_bursts_merge_modifiable_sources_send_each_beat_of_a_narrow_INCR_with_'
            'AxCACHE1_clear_as_a_packet_each_piece_one_unit_of_the_source_pool')
    if not expected:
        assert 'exclusive_targets' not in definition and \
            'exclusive_access' not in definition['contract']
        assert definition['contract']['exclusive'] == \
            'AxLOCK_ignored_normal_access_OKAY_never_EXOKAY_AXI_A7_2_5'
        return
    assert definition['exclusive_targets'] == expected, definition['exclusive_targets']
    rules = definition['contract']['exclusive_access']
    assert rules['key'] == 'exclusive_owner_flags'
    assert definition['contract']['exclusive'].startswith('per_target_see_exclusive_targets')
    ports = {p['name']: p['width'] for p in mapping['ports']}
    for name, (protocol, _base, kind, _extra) in chosen.items():
        if protocol != 'axi4':
            continue
        # Target mode and several bursts take the default width of the widest source ID and the
        # sources; other AXI4 targets keep the default 4-bit ID port.
        want = 3 if kind == 'target' or name in (outstanding or {}) else 4
        for signal in ('arid', 'rid', 'awid', 'bid'):
            assert ports[f'nl_t_{name}_{signal}'] == want, (name, signal)
        if kind == 'target':
            assert definition['target_id_bits'][name] == want


def default_bytes(args, work, mode):
    """Writing exclusive: none everywhere keeps the bytes of omitting it, and
    the map then states the default (AxLOCK ignored, never EXOKAY)."""
    plain = configuration(mode)
    for t in plain['targets']:
        for key in ('exclusive', 'exclusive_monitor', 'exclusive_owner_flags'):
            t.pop(key, None)
    explicit = json.loads(json.dumps(plain))
    for t in explicit['targets']:
        t['exclusive'] = 'none'
    got = []
    for label, config in (('plain', plain), ('explicit', explicit)):
        path = work / (label + '.yaml')
        path.write_text(json.dumps(config) + '\n')
        proc = generate(args.cli, path, work / label)
        assert proc.returncode == 0, proc.stderr
        got.append(hashes(work / label))
        mapping = json.loads((work / label / 'nocloom_exclusive.map.json').read_text())
        definition = mapping['definition']
        assert 'exclusive_targets' not in definition and \
            'exclusive_access' not in definition['contract'], label
        assert definition['contract']['exclusive'] == \
            'AxLOCK_ignored_normal_access_OKAY_never_EXOKAY_AXI_A7_2_5'
    assert got[0] == got[1], got


def rejections(args, work, mode):
    if mode != 'graph':
        return []
    config = configuration(mode)
    index = {t['name']: i for i, t in enumerate(config['targets'])}

    def target(name, **values):
        return lambda d: d['targets'][index[name]].update(values)

    def no_source(d):
        set_protocol(d['initiators'][0], 'axi4_lite')
        d['initiators'][1]['access'] = 'read_only'
        for t in d['targets']:
            for key in ('exclusive', 'exclusive_monitor', 'exclusive_owner_flags'):
                t.pop(key, None)
        d['targets'][index['tm']]['exclusive'] = 'network'

    def granule_value(value):
        return target('tm', exclusive_monitor={'granule_bytes': value})

    def granule_without_network(d):
        for t in d['targets']:
            if t.get('exclusive') == 'network':
                t.pop('exclusive')
                t['exclusive_monitor'] = {'granule_bytes': 8}
                t.pop('exclusive_owner_flags', None)

    cases = [
        ('bad_value', target('tm', exclusive='maybe'), 'NCL_TARGET_EXCLUSIVE'),
        ('monitors_without_network', target('tn', exclusive_monitor=dict(reservations=2)), 'NCL_TARGET_EXCLUSIVE'),
        ('unknown_field', target('t3', not_a_field=8), 'NCL_SCHEMA_UNKNOWN_FIELD'),
        ('granule_without_network', granule_without_network, 'NCL_TARGET_EXCLUSIVE'),
        ('target_mode_on_apb', target('tp', exclusive='target'), 'NCL_TARGET_EXCLUSIVE'),
        ('target_mode_on_ahb', target('th', exclusive='target'), 'NCL_TARGET_EXCLUSIVE'),
        ('target_mode_on_lite', target('tl', exclusive='target'), 'NCL_TARGET_EXCLUSIVE'),
        ('read_only_target', target('tm', access='read_only'), 'NCL_TARGET_EXCLUSIVE'),
        ('monitors_zero', target('tm', exclusive_monitor=dict(reservations=0)), 'NCL_TARGET_EXCLUSIVE'),
        ('monitors_seventeen', target('tm', exclusive_monitor=dict(reservations=17)), 'NCL_TARGET_EXCLUSIVE'),
        ('monitors_beyond_keys', target('tm', exclusive_monitor=dict(reservations=9)), 'NCL_TARGET_EXCLUSIVE'),
        ('granule_three', granule_value(3), 'NCL_TARGET_EXCLUSIVE'),
        ('granule_256', granule_value(256), 'NCL_TARGET_EXCLUSIVE'),
        ('no_exclusive_source', no_source, 'NCL_TARGET_EXCLUSIVE'),
        ('not_integer', target('tm', exclusive_monitor=dict(reservations='two')), 'NCL_SCHEMA_TYPE'),
    ]
    rows = []
    for label, edit, code in cases:
        bad = json.loads(json.dumps(config))
        edit(bad)
        path = work / (label + '.yaml')
        path.write_text(json.dumps(bad) + '\n')
        dest = work / ('rejected-' + label)
        proc = generate(args.cli, path, dest)
        assert proc.returncode == 1 and code in proc.stderr and not dest.exists(), (label,
                                                                                    proc.stderr)
        rows.append({'case': label, 'code': code, 'no_output': True})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', choices=MODES, required=True)
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    yaml = work / 'input.yaml'
    yaml.write_text(json.dumps(configuration(args.mode), indent=1) + '\n')
    outputs = []
    for label in ('first', 'repeat'):
        proc = generate(args.cli, yaml, work / label)
        assert proc.returncode == 0, proc.stderr
        outputs.append(hashes(work / label))
    assert outputs[0] == outputs[1] and len(outputs[0]) == 6, outputs
    mapping = json.loads((work / 'first/nocloom_exclusive.map.json').read_text())
    contract(args.mode, mapping)
    default_bytes(args, work, args.mode)
    negatives = rejections(args, work, args.mode)
    (work / 'rejections.json').write_text(json.dumps(negatives, indent=1) + '\n')

    runs = {'': bench(work, 'bench', work / 'first', mapping, args.mode)}
    if args.mode == 'graph':
        # The same episodes with the targets and their monitors in domain far,
        # and with tm keeping several bursts in flight.
        variants = {'clocked_' + clocks: (clocks, None) for clocks in CLOCKS}
        variants['outstanding'] = (None, OUTSTANDING)
        variants['clocked_7_5_outstanding'] = ('7_5', OUTSTANDING)
        for label, (clocks, outstanding) in variants.items():
            path = work / (label + '.yaml')
            path.write_text(json.dumps(configuration(args.mode, clocks, outstanding),
                                       indent=1) + '\n')
            proc = generate(args.cli, path, work / label)
            assert proc.returncode == 0, (label, proc.stderr)
            clocked = json.loads((work / label / 'nocloom_exclusive.map.json').read_text())
            contract(args.mode, clocked, outstanding)
            runs[label] = bench(work, label + '-bench', work / label, clocked, args.mode, clocks)
    (work / 'results.json').write_text(json.dumps(
        {'runs': runs, 'status': 'PASS' if all(r['status'] == 'PASS' for r in runs.values())
         else 'FAIL'}, indent=1) + '\n')
    for label, run in runs.items():
        assert run['status'] == 'PASS', (label, run['failures'])
    print(f'NOCLOOM_PASS exclusive {args.mode} episodes={len(runs[""]["episodes"])} '
          f'steps={runs[""]["steps"]} rejections={len(negatives)} '
          f'clocked={",".join(k for k in runs if k)}')


def bench(work, name, generated, mapping, mode, clocks=None):
    """One transaction bench run on a generated network."""
    case = work / name
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(generated / 'nocloom_exclusive.v'))
    steps = write_bench(case, mapping, mode, clocks=clocks)
    rc = simulate(case, work / ('compiled-' + name))
    log = (case / 'simulation.log').read_text()
    passed = rc == 0 and f'NOCLOOM_PASS exclusive {mode}' in log and 'ASSERT_FAIL' not in log
    return {'simulation_exit': rc, 'clocks': clocks,
            'episodes': [line.split()[1] for line in log.splitlines()
                         if line.startswith('EPISODE ')],
            'steps': steps, 'status': 'PASS' if passed else 'FAIL',
            'failures': [line for line in log.splitlines() if 'ASSERT_FAIL' in line][:3]
            or ([] if passed else [log[-2000:]]),
            'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
            'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}


if __name__ == '__main__':
    main()
