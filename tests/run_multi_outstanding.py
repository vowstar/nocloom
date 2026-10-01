"""Multiple outstanding bursts through the public CLI: generation, contract,
byte stability at capacity one, rejection matrix, then the transaction bench."""
import argparse
import os
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from multi_outstanding import (MODES, SOURCES, capacity, clocked, configuration, crossing,
                               global_outstanding, id_bits, targets, tracked_ids)
from multi_outstanding_bench import episodes, measured, write_bench
from endpoint_ports import set_protocol
from sim_engine import generated_rtl


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
    chosen = targets(mode)
    assert definition['transport']['max_outstanding'] == global_outstanding(mode)
    assert definition['outstanding'] == {
        'initiators': {s: capacity(mode, s) for s in SOURCES},
        'targets': {t: spec[3] for t, spec in chosen.items()},
        'initiator_id_tables': {s: tracked_ids(mode, s) for s in SOURCES
                          if capacity(mode, s) > 1}}, definition['outstanding']
    assert definition['contract']['ordering']['id_table'].startswith(
        'initiator_id_tables_entries_of_direction_and_AXI_ID')
    assert definition['contract']['ordering']['responses'].startswith(
        'same_ID_in_issue_order_different_IDs_in_any_order')
    rules = definition['contract']['ordering']
    assert rules['decode_miss'] == 'DECERR_from_the_default_target_without_target_access'
    # Clocked graphs state that every crossing carries the tag in order, and
    # every crossing's payload lists it.
    assert ('crossings' in rules) == crossing(mode), rules
    if crossing(mode):
        tag = (global_outstanding(mode) - 1).bit_length()
        cuts = mapping['network']['cdc']['cuts']
        assert cuts and all(any(f['name'] == 'tag' and f['width'] == tag for f in c['fields'])
                            for c in cuts), cuts[0]['fields'] if cuts else None
    assert 'exclusive' not in rules
    assert definition['contract']['capacity'] == 'source_bursts_per_direction_as_many_transactions_shared_by_reads_and_writes_target_bursts_shared_by_reads_and_writes_see_outstanding'
    multi = {t for t, spec in chosen.items() if spec[3] > 1}
    assert set(definition['target_id_bits']) == multi
    ports = {p['name']: p['width'] for p in mapping['ports']}
    for t in chosen:
        if chosen[t][0] != 'axi4':
            continue
        # A multi-burst target's default ID port has room for AxID and one bit for the two sources
        # reaching it; the others keep the default 4-bit ID port.
        want = id_bits(mode) + 1 if t in multi else 4
        for signal in ('arid', 'rid', 'awid', 'bid'):
            assert ports.get(f'nl_t_{t}_{signal}', 0) == want, (t, signal)
        if t in multi:
            assert definition['target_id_bits'][t] == want
    assert 'target_outstanding' in definition['contract']


def single_outstanding_bytes(args, work, mode):
    """Capacity one written explicitly keeps the bytes of omitting it."""
    plain = configuration(mode)
    plain['transport']['max_outstanding'] = 1
    plain['transport'].pop('id_table_entries', None)
    for i in plain['initiators']:
        i.pop('max_outstanding', None)
    for t in plain['targets']:
        t.pop('max_outstanding', None)
    explicit = json.loads(json.dumps(plain))
    for i in explicit['initiators']:
        i['max_outstanding'] = 1
    for t in explicit['targets']:
        t['max_outstanding'] = 1
    got = []
    for label, config in (('plain', plain), ('explicit', explicit)):
        path = work / (label + '.yaml')
        path.write_text(json.dumps(config) + '\n')
        proc = generate(args.cli, path, work / label)
        assert proc.returncode == 0, proc.stderr
        got.append(hashes(work / label))
        definition = json.loads((work / label / 'nocloom_outstanding.map.json').read_text())[
            'definition']
        assert not {'outstanding', 'target_id_bits'} & set(definition)
        assert not {'ordering', 'target_outstanding'} & set(definition['contract']), label
    assert got[0] == got[1], got


def rejections(args, work, mode):
    config = configuration(mode)
    first_target = 0
    apb = next((i for i, t in enumerate(config['targets']) if t.get('protocol') == 'apb4'), None)
    cases = [
        ('global_too_large', lambda d: d['transport'].update(max_outstanding=65), 'NCL_AXI4_CAPACITY'),
        ('initiator_zero', lambda d: d['initiators'][1].update(max_outstanding=0),
         'NCL_AXI4_CAPACITY'),
        ('target_too_large', lambda d: d['targets'][first_target].update(max_outstanding=17),
         'NCL_AXI4_CAPACITY'),
        ('not_integer', lambda d: d['initiators'][0].update(max_outstanding='four'),
         'NCL_SCHEMA_TYPE'),
        ('unknown_transport_key', lambda d: d['transport'].update(unknown_option=1),
         'NCL_SCHEMA_UNKNOWN_FIELD'),
        # A Lite source has one key per direction and at most two entries.
        ('lite_source_ids', lambda d: set_protocol(d['initiators'][0], 'axi4_lite',
                                                    id_table_entries=3),
         'NCL_AXI4_ID_CAPACITY'),
        ('lite_source_id_one', lambda d: set_protocol(d['initiators'][0], 'axi4_lite',
                                                       id_table_entries=1), None),
        ('width_conversion', lambda d: ([s.update(data_width=64) for s in d['initiators']],
                                        d['initiators'][1].update(
            data_width=32)), None),
        # One watchdog serves every burst in flight at the target.
        ('target_timeout', lambda d: d['targets'][first_target].update(
            max_outstanding=2, timeout={'cycles': 1024}), None),
        ('target_widths', lambda d: d['targets'][first_target].update(
            max_outstanding=2, data_width=64), None),
        ('source_with_target_widths', lambda d: (
            [t.pop('max_outstanding', None) for t in d['targets']],
            d['targets'][first_target].update(data_width=64)), None),
        # A multi-burst target reached by an AXI4-Lite source (AxID 0) or by a
        # source converted to the target width.
        ('target_lite_source', lambda d: set_protocol(
            d['initiators'][1], 'axi4_lite', max_outstanding=1), None),
        ('target_converted_source', lambda d: d['initiators'][1].update(
            data_width=64, max_outstanding=1), None),
        ('target_read_only', lambda d: d['targets'][first_target].update(
            max_outstanding=2, access='read_only'), None),
        # The ID table: 1..min(capacity, 2 * 2^ID width) and only above one burst.
        ('ids_zero', lambda d: d['initiators'][0].update(id_table_entries=0),
         'NCL_AXI4_ID_CAPACITY'),
        ('ids_above_bound', lambda d: d['initiators'][0].update(
            id_table_entries=capacity(mode, 's0') + 1), 'NCL_AXI4_ID_CAPACITY'),
        ('ids_single_burst', lambda d: d['initiators'][1].update(
            max_outstanding=1, id_table_entries=1), 'NCL_AXI4_ID_CAPACITY'),
        ('ids_not_integer', lambda d: d['initiators'][0].update(id_table_entries='two'),
         'NCL_SCHEMA_TYPE'),
        # Exclusive access support combines with several bursts per source,
        # clocked graphs included, and a network monitor with several bursts
        # in flight at its AXI4 target.
        ('exclusive_access_handling', lambda d: owner_flags(d, d['targets'][first_target], 'target'),
         None),
        ('exclusive_network_multi_target', lambda d: (
            owner_flags(d, d['targets'][first_target], 'network'),
            d['targets'][first_target].update(max_outstanding=2)), None),
        ('ids_global_unused', lambda d: (d['transport'].update(max_outstanding=1, id_table_entries=1),
                                         d['initiators'][1].update(max_outstanding=1)),
         'NCL_AXI4_ID_CAPACITY')]
    if apb is not None:
        cases.append(('dedicated_target', lambda d: d['targets'][apb].update(max_outstanding=2),
                      'NCL_AXI4_CAPACITY_COMBINATION'))
    if mode != 'funnel' and not crossing(mode):
        # One explicit domain: a clocked graph without crossings.
        def one_domain(d):
            for direction in ('request', 'response'):
                for node in d['topology'][direction]['nodes']:
                    node['clock_domain'] = 'main'
        cases.append(('clocked_graph', one_domain, None))
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
    mapping = json.loads((work / 'first/nocloom_outstanding.map.json').read_text())
    contract(args.mode, mapping)
    single_outstanding_bytes(args, work, args.mode)
    negatives = rejections(args, work, args.mode)
    (work / 'rejections.json').write_text(json.dumps(negatives, indent=1) + '\n')
    # The public example generates with the capacities it documents.
    example = Path(__file__).resolve().parents[1] / 'examples/axi4_outstanding.yaml'
    proc = generate(args.cli, example, work / 'example')
    assert proc.returncode == 0, proc.stderr
    shown = json.loads((work / 'example/nocloom_outstanding.map.json').read_text())['definition']
    assert shown['outstanding'] == {'initiators': {'cpu0': 4, 'cpu1': 1},
                                    'targets': {'dram0': 4, 'sram0': 1, 'uart0': 1},
                                    'initiator_id_tables': {'cpu0': 4}}, shown
    assert shown['target_id_bits'] == {'dram0': 5}

    # NOCLOOM_MO_RULE_MONITORS=0 leaves only the scoreboard and capacity checks
    # (fault analysis: does a broken rule also break the observed traffic?).
    rules = os.environ.get("NOCLOOM_MO_RULE_MONITORS", "1") != "0"
    results = run_bench(work, 'bench', mapping, generated_rtl(work / 'first/nocloom_outstanding.v'),
                        args.mode, episodes(args.mode), rules, False)
    results['rejections'] = negatives
    gain = {}
    if clocked(args.mode):
        # The same clocked network with one shared burst slot at each source and target:
        # several bursts in flight hide the crossing round trip.
        single_yaml = work / 'single.yaml'
        single_yaml.write_text(json.dumps(configuration(args.mode, single=True), indent=1) + '\n')
        proc = generate(args.cli, single_yaml, work / 'single')
        assert proc.returncode == 0, proc.stderr
        single_map = json.loads((work / 'single/nocloom_outstanding.map.json').read_text())
        results['single'] = run_bench(work, 'single-bench', single_map,
                                      generated_rtl(work / 'single/nocloom_outstanding.v'),
                                      args.mode, measured(args.mode), rules, True)
        many, one = results['cycles'], results['single']['cycles']
        assert set(one) == {'same_target_reads', 'same_target_writes'}, one
        for name in one:
            gain[name] = round(one[name] / many[name], 3)
            # Eight bursts of four beats to one target: with four in flight
            # the round trip through both crossings overlaps. In
            # clocked_extremes the target model serves one read at a time
            # after its latency, which bounds the gain: only require one.
            if args.mode == 'clocked_extremes':
                assert many[name] < one[name], (name, many[name], one[name])
            else:
                assert many[name] * 4 < one[name] * 3, (name, many[name], one[name])
        results['gain'] = gain
    (work / 'results.json').write_text(json.dumps(results, indent=1) + '\n')
    print(f'NOCLOOM_PASS multiple outstanding {args.mode} episodes={len(results["episodes"])} '
          f'rule_monitors={"on" if rules else "off"} '
          f'rejections={len(negatives)}' + (f' gain={json.dumps(gain)}' if gain else ''))


def run_bench(work, name, mapping, rtl, mode, eps, rules, single):
    case = work / name
    case.mkdir()
    (case / 'network.v').write_text(rtl)
    write_bench(case, mapping, mode, eps, rules, single)
    rc = simulate(case, work / ('compiled-' + name))
    log = (case / 'simulation.log').read_text()
    passed = rc == 0 and 'NOCLOOM_PASS multiple outstanding' in log and 'ASSERT_FAIL' not in log
    seen, cycles = [], {}
    for line in log.splitlines():
        if line.startswith('EPISODE '):
            seen.append(line.split()[1])
        elif line.startswith('EPISODE_CYCLES ') and seen:
            cycles[seen[-1]] = int(line.split()[1])
    result = {'simulation_exit': rc, 'episodes': seen, 'status': 'PASS' if passed else 'FAIL',
              'rule_monitors': rules, 'cycles': cycles,
              'in_flight': [line for line in log.splitlines() if line.startswith('IN_FLIGHT')],
              'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
              'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}
    assert passed, [line for line in log.splitlines() if 'ASSERT_FAIL' in line][:3] or log[-2000:]
    return result

if __name__ == '__main__':
    main()
