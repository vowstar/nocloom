"""Exclusive access with several bursts in flight per source through the
public CLI: generation, contract, the combinations still refused, then the
transaction bench."""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from exclusive_outstanding import CAPACITY, MODES, SOURCES, configuration, targets
from exclusive_outstanding_bench import write_bench
from sim_engine import generated_rtl

MAP = 'nocloom_exclusive_outstanding.map.json'


def generate(cli, yaml, dest):
    return subprocess.run([cli, 'generate', str(yaml), '--out-dir', str(dest)],
                          capture_output=True, text=True, timeout=300, check=False)


def hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def contract(mode, mapping):
    definition = mapping['definition']
    chosen = targets(mode)
    assert definition['outstanding']['initiators'] == {s: CAPACITY for s in SOURCES}
    assert definition['outstanding']['initiator_id_tables'] == {s: CAPACITY for s in SOURCES}
    assert definition['outstanding']['targets'] == {t: spec[3] for t, spec in chosen.items()}
    shown = definition['exclusive_targets']
    for name, (_protocol, _base, kind, _k) in chosen.items():
        assert (name in shown) == bool(kind), name
        if kind:
            assert shown[name]['mode'] == kind, shown
    assert definition['contract']['exclusive_access']['key'] == 'exclusive_id_flags'
    assert definition['contract']['ordering']['exclusive'].startswith(
        'monitors_key_on_exclusive_id_flags')
    assert definition['contract']['ordering']['responses'].startswith(
        'same_ID_in_issue_order_different_IDs_in_any_order')


def rejections(args, work, mode):
    """Combinations that stay refused next to exclusive support."""
    config = configuration(mode)
    index = {t['name']: i for i, t in enumerate(config['targets'])}

    def clocked(d):
        for direction in ('request', 'response'):
            for node in d['topology'][direction]['nodes']:
                node['clock_domain'] = 'main'

    cases = [
        # A network monitor keeps several bursts in flight at an AXI4 target.
        ('network_multi_target', lambda d: d['targets'][index['tm']].update(max_outstanding=4),
         None),
        # A converted source reaches the graph's multi-burst target as well.
        ('converted_source', lambda d: d['initiators'][0].update(data_width=64, len_width=8),
         None),
        ('converted_short_len', lambda d: d['initiators'][0].update(data_width=64), None),
        # One shared burst slot keeps the same exclusive support.
        ('single_outstanding', lambda d: d['transport'].update(max_outstanding=1), None)]
    if mode == 'graph':
        cases.append(('clocked_graph', clocked, None))
    rows = []
    for label, edit, code in cases:
        bad = json.loads(json.dumps(config))
        edit(bad)
        path = work / (label + '.yaml')
        path.write_text(json.dumps(bad) + '\n')
        dest = work / ('rejected-' + label)
        proc = generate(args.cli, path, dest)
        if code is None:
            assert proc.returncode == 0 and dest.exists(), (label, proc.stderr)
            definition = json.loads((dest / MAP).read_text())['definition']
            shown = definition.get('outstanding', {})
            if label == 'single_outstanding':
                assert set(shown.get('initiators', {}).values()) <= {1} and \
                    'initiator_id_tables' not in shown, shown
            rows.append({'case': label, 'code': None, 'generated': True})
            continue
        assert proc.returncode == 1 and code in proc.stderr and not dest.exists(), (label,
                                                                                    proc.stderr)
        rows.append({'case': label, 'code': code, 'no_output': True})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', choices=MODES, required=True)
    parser.add_argument('--configuration-only', action='store_true')
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    if args.configuration_only:
        rows = rejections(args, work, args.mode)
        (work / 'configuration-results.json').write_text(json.dumps(rows, indent=1)+'\n')
        print('NOCLOOM_PASS exclusive configurations ' + args.mode)
        return
    yaml = work / 'input.yaml'
    yaml.write_text(json.dumps(configuration(args.mode), indent=1) + '\n')
    outputs = []
    for label in ('first', 'repeat'):
        proc = generate(args.cli, yaml, work / label)
        assert proc.returncode == 0, proc.stderr
        outputs.append(hashes(work / label))
    assert outputs[0] == outputs[1] and len(outputs[0]) == 6, outputs
    mapping = json.loads((work / 'first' / MAP).read_text())
    contract(args.mode, mapping)
    negatives = rejections(args, work, args.mode)
    case = work / 'bench'
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(work / 'first/nocloom_exclusive_outstanding.v'))
    write_bench(case, mapping, args.mode, CAPACITY)
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    passed = (rc == 0 and 'NOCLOOM_PASS exclusive outstanding' in log and
              'ASSERT_FAIL' not in log)
    seen = [line.split()[1] for line in log.splitlines() if line.startswith('EPISODE ')]
    flight = [line for line in log.splitlines() if line.startswith(('IN_FLIGHT', 'CONTENTION'))]
    (work / 'results.json').write_text(json.dumps(
        {'simulation_exit': rc, 'episodes': seen, 'in_flight': flight, 'rejections': negatives,
         'status': 'PASS' if passed else 'FAIL',
         'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
         'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}, indent=1) + '\n')
    assert passed, [line for line in log.splitlines() if 'ASSERT_FAIL' in line][:3] or log[-2000:]
    print(f'NOCLOOM_PASS exclusive outstanding {args.mode} episodes={len(seen)} '
          f'rejections={len(negatives)} {" ".join(flight)}')


if __name__ == '__main__':
    main()
