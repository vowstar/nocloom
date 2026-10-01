"""AXI4-Lite sources with several transactions in flight through the public
CLI: generation, contract, byte stability at capacity one, rejection matrix,
the transaction bench and, in the graph mode, a throughput comparison against
the same network with one shared source transaction slot."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from lite_outstanding_bench import (BOUNDED, CLOCKED, MODES, SOURCES,
                                    TARGET_CAPACITY, capacity, configuration, episodes, lite,
                                    measured, write_bench, writes_allowed)
from sim_engine import generated_rtl

MAP = 'nocloom_lite_outstanding.map.json'


def owner_flags(d, target, mode):
    """Exclusive support on a target, the owner of an access naming its source."""
    d.setdefault('transport', {})['user_flags'] = ['owner']
    for index, initiator in enumerate(d['initiators']):
        initiator['user_flags'] = {'owner': index % 2}
    target.update(exclusive=mode, exclusive_id_flags=['owner'])


def generate(cli, yaml, dest):
    return subprocess.run([cli, 'generate', yaml, '--out-dir', dest], capture_output=True,
                          text=True, timeout=300, check=False)


def hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def contract(mode, mapping):
    definition = mapping['definition']
    outstanding = definition['outstanding']
    assert outstanding['initiators'] == {s: capacity(mode, s) for s in SOURCES}, outstanding
    # Lite has one key per direction and appears only when its table limits them;
    # a full AXI4 table holds min(capacity, 2 directions * 2^ID width) keys.
    ids = 0 if mode == 'funnel' else 2
    expected = {s: min(capacity(mode, s), 2 << ids) for s in SOURCES
                if capacity(mode, s) > 1 and not lite(mode, s)}
    if os.environ.get('NOCLOOM_LO_ID_TABLE') == '1':
        expected.update({s: 1 for s in SOURCES
                         if capacity(mode, s) > 1 and lite(mode, s) and writes_allowed(mode, s)})
    assert outstanding.get('initiator_id_tables', {}) == expected, outstanding
    ingress = definition['contract']['lite_ingress']
    assert ingress['outstanding'].startswith('one_ordering_key_per_direction'), ingress
    limited = os.environ.get('NOCLOOM_LO_ID_TABLE') == '1' and any(
        capacity(mode, s) > 1 and lite(mode, s) and writes_allowed(mode, s) for s in SOURCES)
    assert ('id_table' in ingress) == limited, ingress
    names = {p['name'] for p in mapping['ports']}
    for s in SOURCES:
        for signal in ('arid', 'rid', 'awid', 'bid', 'rlast', 'wlast', 'arlen', 'awlen'):
            assert (f'nl_i_{s}_{signal}' in names) != lite(mode, s), (s, signal)
    if mode == 'perwidth':
        widths = {p['name']: p['width'] for p in mapping['ports']}
        assert widths['nl_i_s0_rdata'] == widths['nl_i_s1_rdata'] == 32
        assert widths['nl_t_ta_rdata'] == 64 and widths['nl_t_tb_rdata'] == 32


def single_outstanding_bytes(args, work, mode):
    """Capacity one written explicitly keeps the bytes of omitting it, and no
    outstanding contract appears."""
    plain = configuration(mode, single=True)
    for i in plain['initiators']:
        i.pop('max_outstanding', None)
    plain['transport'].pop('max_outstanding')
    explicit = json.loads(json.dumps(plain))
    explicit['transport']['max_outstanding'] = 1
    for i in explicit['initiators']:
        i['max_outstanding'] = 1
    got = []
    for label, config in (('plain', plain), ('explicit', explicit)):
        path = work / (label + '.yaml')
        path.write_text(json.dumps(config) + '\n')
        proc = generate(args.cli, path, work / label)
        assert proc.returncode == 0, proc.stderr
        got.append(hashes(work / label))
        definition = json.loads((work / label / MAP).read_text())['definition']
        assert 'outstanding' not in definition, label
        assert 'outstanding' not in definition['contract']['lite_ingress'], label
        assert 'ordering' not in definition['contract'], label
    assert got[0] == got[1], got


def rejections(args, work, mode):
    config = configuration(mode)

    def clocked(d):
        for direction in ('request', 'response'):
            for node in d['topology'][direction]['nodes']:
                node['clock_domain'] = 'main'

    # Every exclusive target needs a source that can issue exclusive access
    # (a full AXI4 source with writes); a network monitor keeps several bursts
    # in flight at its AXI4 target.
    exclusive_source = any(not lite(mode, s) for s in SOURCES)
    bounded = mode in BOUNDED
    cases = [
        ('lite_too_large', lambda d: d['initiators'][1].update(max_outstanding=65),
         'NCL_AXI4_CAPACITY'),
        ('lite_zero', lambda d: d['initiators'][1].update(max_outstanding=0), 'NCL_AXI4_CAPACITY'),
        ('lite_ids_zero', lambda d: d['initiators'][0].update(id_table_entries=0),
         'NCL_AXI4_ID_CAPACITY'),
        ('lite_ids_three', lambda d: d['initiators'][0].update(id_table_entries=3),
         'NCL_AXI4_ID_CAPACITY'),
        ('lite_ids_one', lambda d: d['initiators'][0].update(id_table_entries=1), None),
        ('lite_ids_two', lambda d: d['initiators'][0].update(id_table_entries=2), None),
        ('exclusive_access_handling', lambda d: owner_flags(d, d['targets'][0], 'target'),
         None if exclusive_source else 'NCL_TARGET_EXCLUSIVE'),
        # A multi-transaction target reached by Lite sources (AxID 0).
        ('multi_target', lambda d: d['targets'][0].update(max_outstanding=2), None)]
    if mode not in ('funnel',) + CLOCKED:
        # Several transactions in flight and exclusive support cross clocked graphs.
        cases.append(('clocked_graph', clocked, None))
    if all(lite(mode, s) for s in SOURCES):
        cases.append(('global_ids_only_lite', lambda d: d['transport'].update(
            id_table_entries=1), None))
    rows = []
    for label, edit, code in cases:
        bad = json.loads(json.dumps(config))
        edit(bad)
        path = work / (label + '.yaml')
        path.write_text(json.dumps(bad) + '\n')
        dest = work / ('rejected-' + label)
        proc = generate(args.cli, path, dest)
        if code is None:
            # A supported combination generates successfully.
            assert proc.returncode == 0 and dest.exists(), (label, proc.stderr)
            rows.append({'case': label, 'code': None, 'generated': True})
            continue
        assert proc.returncode == 1 and code in proc.stderr and not dest.exists(), (label,
                                                                                    proc.stderr)
        rows.append({'case': label, 'code': code, 'no_output': True})
    return rows


def run_bench(work, mapping, rtl, mode, eps, rules, single, name):
    case = work / name
    case.mkdir()
    (case / 'network.v').write_text(rtl)
    write_bench(case, mapping, mode, eps, rules, single)
    rc = simulate(case, work / ('compiled-' + name))
    log = (case / 'simulation.log').read_text()
    passed = rc == 0 and 'NOCLOOM_PASS lite outstanding' in log and 'ASSERT_FAIL' not in log
    seen = [line.split()[1] for line in log.splitlines() if line.startswith('EPISODE ')]
    cycles = {m.group(1): int(m.group(2))
              for m in re.finditer(r'THROUGHPUT (\S+) cycles=(\d+)', log)}
    result = {'simulation_exit': rc, 'episodes': seen, 'status': 'PASS' if passed else 'FAIL',
              'rule_monitors': rules, 'throughput_cycles': cycles,
              'in_flight': [line for line in log.splitlines() if line.startswith('IN_FLIGHT')],
              'id_table_mutex': [line for line in log.splitlines()
                                 if line.startswith('ID_TABLE_MUTEX')],
              'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
              'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}
    assert passed, [line for line in log.splitlines() if 'ASSERT_FAIL' in line][:3] or log[-2000:]
    return result


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
    mapping = json.loads((work / 'first' / MAP).read_text())
    contract(args.mode, mapping)
    single_outstanding_bytes(args, work, args.mode)
    negatives = rejections(args, work, args.mode)
    rules = os.environ.get('NOCLOOM_LO_RULE_MONITORS', '1') != '0'
    rtl = generated_rtl(work / 'first/nocloom_lite_outstanding.v')
    eps = episodes(args.mode)
    results = {'mode': args.mode, 'rejections': negatives,
               'multi': run_bench(work, mapping, rtl, args.mode, eps, rules, False, 'bench')}
    if args.mode in ('graph', 'multi_target'):
        # The same network and traffic with one shared transaction slot per source:
        # pipelining must shorten every back-to-back run to one target. In
        # the graph mode the targets share one transaction slot across directions, so
        # the gain is the overlapped network transit; in multi_target ta also
        # overlaps transactions at the target.
        single_yaml = work / 'single.yaml'
        single_yaml.write_text(json.dumps(configuration(args.mode, single=True), indent=1) + '\n')
        proc = generate(args.cli, single_yaml, work / 'single')
        assert proc.returncode == 0, proc.stderr
        single_map = json.loads((work / 'single' / MAP).read_text())
        timed = [dict(ep, checks=[]) for ep in measured(eps)]
        results['single'] = run_bench(work, single_map,
                                      generated_rtl(work / 'single/nocloom_lite_outstanding.v'),
                                      args.mode, timed, rules, True, 'single-bench')
        many, one = results['multi']['throughput_cycles'], results['single']['throughput_cycles']
        assert set(many) == set(one) and many, (many, one)
        for name in many:
            if name.startswith('back_to_back'):
                assert many[name] < one[name], (name, many[name], one[name])
            else:
                assert many[name] <= one[name], (name, many[name], one[name])
    (work / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'NOCLOOM_PASS lite outstanding {args.mode} '
          f'episodes={len(results["multi"]["episodes"])} rejections={len(negatives)} '
          f'rule_monitors={"on" if rules else "off"} '
          f'throughput={json.dumps(results["multi"]["throughput_cycles"])} '
          f'single={json.dumps(results.get("single", {}).get("throughput_cycles", {}))}')


if __name__ == '__main__':
    main()
