"""Verify installed public clock placement against real independently clocked ports."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from cross_hardware_sim import simulate
from graph_clocked_bench import write_bench
from sim_engine import generated_rtl


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    for key in ('source', 'build', 'compiler', 'work'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--mode', choices=('endpoints', 'twelve'), required=True)
    args = parser.parse_args()
    source, build, root = (Path(getattr(args, k)).resolve() for k in ('source', 'build', 'work'))
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=root))
    commands = []

    def run(argv, label, expected=0):
        argv = list(map(str, argv))
        start = time.monotonic()
        result = subprocess.run(argv, capture_output=True, text=True, timeout=240, check=False)
        (work / (label + '.log')).write_text(result.stdout + result.stderr)
        commands.append({'argv': argv, 'exit': result.returncode, 'seconds': time.monotonic() - start})
        (work / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    installed, prefix, consumer = work / 'installed', work / 'relocated', work / 'consumer'
    run(['cmake', '--install', build, '--prefix', installed], 'install')
    installed.rename(prefix)
    consumer.mkdir()
    for name in ('graph_clocked_consumer.cpp', 'graph_clocked_definition.hpp', 'graph_example_definition.hpp'):
        shutil.copyfile(source / 'tests' / name, consumer / name)
    (consumer / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\nproject(consumer LANGUAGES CXX)\n'
        'find_package(nocloom CONFIG REQUIRED)\nadd_executable(consumer graph_clocked_consumer.cpp)\n'
        'target_link_libraries(consumer PRIVATE nocloom::core)\n')
    run(['cmake', '-S', consumer, '-B', consumer / 'build', '-DCMAKE_CXX_COMPILER=' + args.compiler,
         '-DCMAKE_PREFIX_PATH=' + str(prefix), '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF'], 'configure')
    run(['cmake', '--build', consumer / 'build', '-j2'], 'consumer-build')
    text = (source / 'examples/axi4_graph_clocked.yaml').read_text()
    if args.mode == 'twelve':
        domains = ['main', 'source1', 'source2', 'target'] + ['node'+str(i) for i in range(8)]
        declaration = 'clock_domains:\n' + ''.join(
            f'  - {{name: {name}, clock_port: clk_{name}, reset_port: reset_n, test_port: test}}\n' for name in domains)
        declaration += 'synchronizer_stages: 3\n'
        text = re.sub(r'clock_domains:.*?(?=initiators:)', declaration, text, flags=re.DOTALL)
        for i in (1, 2):
            text = text.replace(f'name: s{i}, clock_domain: main', f'name: s{i}, clock_domain: source{i}')
        request, response = text.split('\n  response:')
        for i, name in enumerate(('merge', 'branch', 'tail', 'leaf0', 'leaf1', 'leaf2')):
            request, count = re.subn(r'(\{name: '+name+r'[^}\n]*clock_domain: )middle', rf'\g<1>node{i}', request)
            assert count == 1
        for i, name in enumerate(('merge', 'branch'), 6):
            response, count = re.subn(r'(\{name: '+name+r'[^}\n]*clock_domain: )middle', rf'\g<1>node{i}', response)
            assert count == 1
        text = request + '\n  response:' + response
    config = work / 'clocked.yaml'
    config.write_text(text)
    artifacts = []
    for origin in ('cli', 'api', 'repeat'):
        dest = work / origin
        if origin == 'api':
            run([consumer / 'build/consumer', dest, args.mode], origin)
        else:
            run([prefix / 'bin/nocloom', 'validate', config], origin + '-validate')
            run([prefix / 'bin/nocloom', 'generate', config, '--out-dir', dest], origin)
        artifacts.append({p.name: digest(p) for p in dest.iterdir()})
    assert artifacts[0] == artifacts[1] == artifacts[2]
    (work / 'artifact-comparison.json').write_text(json.dumps(artifacts, indent=2)+'\n')
    # The API/repeat bytes inherit this exact DUT's behavioral evidence; not extra runs.
    dest = work / 'cli'
    mapping = json.loads((dest / 'nocloom_graph.map.json').read_text())
    bundle = json.loads((dest / 'nocloom_graph.bundle.json').read_text())
    assert mapping['definition'] == bundle['definition']
    network, definition = mapping['network'], mapping['definition']
    cdc = network['cdc']
    assert cdc['physical_status'] == 'OPEN'
    domains = definition['clock_domains']
    assert len(domains) == (3 if args.mode == 'endpoints' else 12)
    assert set(cdc['objects'].values()) == {d['name'] for d in domains}
    assert len(cdc['cuts']) == (12 if args.mode == 'endpoints' else 18)
    expected_sources = {'s0': 'main', 's1': 'main', 's2': 'main'}
    if args.mode == 'twelve':
        expected_sources.update(s1='source1', s2='source2')
    expected_objects = {'source/'+k: v for k, v in expected_sources.items()}
    expected_objects.update({'target/t'+str(i): 'target' for i in range(3)})
    for direction, names, offset, key in (
            ('request', ('merge', 'branch', 'tail', 'leaf0', 'leaf1', 'leaf2'), 0, 'req/'),
            ('response', ('merge', 'branch'), 6, 'rsp/')):
        nodes = definition['topology'][direction]['nodes']
        assert {n['name'] for n in nodes} == set(names) and len(nodes) == len(names)
        for n in nodes:
            expected = 'middle' if args.mode == 'endpoints' else 'node'+str(offset+names.index(n['name']))
            assert n['clock_domain'] == expected
            expected_objects[key+n['hardware_node']] = expected
    assert cdc['objects'] == expected_objects
    for cut in cdc['cuts']:
        direction, edge = cut['segment'].split('/')
        left, right = edge.split('>')
        for name, side in ((left, 'from_domain'), (right, 'to_domain')):
            obj = direction+'/'+name
            if obj not in expected_objects:
                obj = ('source/' if name.startswith('s') else 'target/')+name
            assert cut[side] == expected_objects[obj]
    owners = definition['endpoint_clock_domains']
    for p in mapping['ports']:
        if p['signal'] == 'clock':
            assert p['clock'] == p['name']
        elif p['endpoint'].startswith(('reset:', 'test:')):
            assert p['clock'] == ''
        else:
            kind, name = p['endpoint'].split(':')
            assert p['clock'] == 'clk_' + owners[kind+'s'][name]
    regs = {r['value']: r for r in cdc['registers']}
    assert len(regs) == len(cdc['registers'])
    rtl = (dest / 'nocloom_graph.v').read_text()
    input_aliases = {port: value for value, port in re.findall(r'assign (nl_internal_v[0-9]+) = (clk_[A-Za-z0-9_]+|reset_n);', rtl)}
    assert len(input_aliases) == len(domains)+1
    gated = {out: clock for clock, out in re.findall(
        r'^nocloom_clock_gate \w+ \(\.clock_in\((nl_internal_v[0-9]+)\), [^;]*\.clock_out\((nl_internal_v[0-9]+)\)\);',
        rtl, flags=re.M)}
    assert gated and len(gated) == len(network['clock_gates'])
    storage = {s['resource']: s for s in network['storage']}
    rtl_of = lambda ref: 'nl_internal_v' + ref.removeprefix('value/')
    # Synchronizer cell instances by name: stages, clock, reset_n.
    cells = {m[1]: m for m in re.findall(
        r'^nocloom_synchronizer #\(\.STAGES\((\d+)\)\) (\w+) \(\.clock\((\w+)\), \.reset_n\((\w+)\),',
        rtl, flags=re.M)}
    for reg in regs.values():
        assert reg['clock'] in {p['name'] for p in mapping['ports'] if p['signal'] == 'clock'}
        # Each domain's state resets from the last register of its release chain; the
        # chain itself resets from the shared reset port.
        chain = storage['domain/' + reg['clock'].removeprefix('clk_') + '/init']['control']
        reset = input_aliases['reset_n'] if reg['value'] in chain else rtl_of(chain[-1])
        # A synchronizer stage is a register of each of its chain's instances, on the domain clock.
        if 'synchronizer' in reg:
            for name in reg['synchronizer']['instances']:
                stages, _, clock, cell_reset = cells[name]
                assert clock == input_aliases[reg['clock']] and cell_reset == reset
                assert reg['synchronizer']['stage'] < int(stages) and reg['async_reset']
            continue
        assert re.search(r'\breg\b[^;]*\b'+reg['rtl']+r';', rtl)
        # Match each actual sequential block, including clock and reset form.
        block = next(b for b in rtl.split('always @')[1:] if reg['rtl']+' <=' in b)
        # A gated register's clock is a clock gate output fed by its domain's clock.
        clock = re.match(r'\(posedge (nl_internal_v[0-9]+)', block)[1]
        assert gated.get(clock, clock) == input_aliases[reg['clock']]
        assert block.startswith('(posedge '+clock+(' or negedge '+reset+')' if reg['async_reset'] else ')'))
    sync = set(cdc['synchronizer_stages'])
    assert len(sync) == len(cdc['synchronizer_stages'])
    assert all(r in regs and regs[r]['async_reset'] for r in sync)
    assert {r for r in regs if 'synchronizer' in regs[r]} == sync
    assert {i for r in sync for i in regs[r]['synchronizer']['instances']} == set(cells)
    resources = {r['name']: r for r in network['resources']}
    storage = {s['resource']: s for s in network['storage']}
    assert len(resources) == len(network['resources'])
    assert len(storage) == len(network['storage'])
    assert set(storage) == {k for k, r in resources.items() if r['role'] != 'environment'}
    for key, store in storage.items():
        assert store['clock'] == 'clk_'+resources[key]['clock_domain']
        for value in store['payload'] + store['control']:
            assert regs[value]['clock'] == store['clock']
    # Independent depth oracle: explicit pair depth, else the loop formula with
    # both periods, else 6. One stage count per crossing: its own, else the
    # global default.
    by_name = {d['name']: d for d in domains}
    crossings = {(c['from_domain'], c['to_domain']): c for c in definition.get('clock_crossings', [])}
    assert definition['synchronizer_stages'] == (2 if args.mode == 'endpoints' else 3)
    sources, stage_counts = set(), set()
    for cut in cdc['cuts']:
        assert cut['from_domain'] != cut['to_domain']
        a, z = by_name[cut['from_domain']], by_name[cut['to_domain']]
        entry = crossings.get((cut['from_domain'], cut['to_domain']), {})
        n = entry.get('synchronizer_stages', definition['synchronizer_stages'])
        na = nb = n
        stage_counts.add(n)
        if 'depth' in entry:
            expected = (entry['depth'], 'explicit')
        elif 'period_ps' in a and 'period_ps' in z:
            ta, tb = a['period_ps'], z['period_ps']
            expected = (-(-((n+1)*(ta+tb)) // max(ta, tb)), 'formula')
        else:
            expected = (6, 'fallback')
        assert (cut['depth'], cut['depth_source']) == expected, (cut['name'], cut['depth'], expected)
        sources.add(cut['depth_source'])
        depth, w = cut['depth'], cut['count_bits']
        assert 2**w > depth >= 2**(w-1) and cut['synchronizer_stages'] == n
        sender, receiver = 'clk_'+cut['from_domain'], 'clk_'+cut['to_domain']
        for key, clock in (('write_count', sender), ('read_count', receiver)):
            reg = regs[cut[key]]
            assert reg['clock'] == clock and reg['async_reset'] and reg['width'] == w
        for key, source, clock, length in (('write_count_sync', cut['write_count'], receiver, nb),
                                            ('read_count_sync', cut['read_count'], sender, na)):
            chain = cut[key]
            assert len(chain) == length
            for stage in chain:
                reg = regs[stage]
                assert stage in sync and reg['clock'] == clock and reg['width'] == w and reg['next'] == source
                source = stage
        valid = regs[cut['receive_valid']]
        assert valid['clock'] == receiver and valid['async_reset'] and valid['width'] == 1
        for field in cut['fields']:
            assert len(field['storage']) == depth
            for slot in field['storage']:
                reg = regs[slot]
                # The default reset_coverage all resets the payload registers too.
                assert reg['async_reset'] and reg['width'] == field['width']
                assert reg['clock'] == sender
            reg = regs[field['received']]
            assert reg['async_reset'] and reg['width'] == field['width'] and reg['clock'] == receiver
        kinds = [o['kind'] for o in cut['obligations']]
        assert kinds == ['gray_count_skew', 'gray_count_skew', 'slot_read_max_delay']
        assert cut['obligations'][0]['source'] == cut['write_count']
        assert cut['obligations'][0]['destination'] == cut['write_count_sync'][0]
        assert cut['obligations'][1]['source'] == cut['read_count']
        assert cut['obligations'][1]['destination'] == cut['read_count_sync'][0]
        assert len(cut['obligations'][2]['sources']) == depth*len(cut['fields'])
    assert sources == ({'formula', 'explicit'} if args.mode == 'endpoints' else {'fallback'})
    assert stage_counts == ({2, 3, 4} if args.mode == 'endpoints' else {3})
    for r in network['resources']:
        assert r['capacity'] == (next(c['depth'] for c in cdc['cuts'] if c['name'] == r['name'])
                                 if r['role'] == 'crossing_fifo' else 1)
    node = next(n['hardware_node'] for n in definition['topology']['request']['nodes'] if n['name'] == 'leaf0')
    cut = next(c for c in cdc['cuts'] if c['segment'] == 'req/s0>'+node)
    count_reg = regs[cut['write_count']]
    bench = {'module': mapping['module'], 'ports': mapping['ports'], 'mode': args.mode,
             'definition': mapping['definition'],
             'objects': cdc['objects'], 'domains': [{'name': d['name']} for d in domains]}
    baseline = work / 'baseline'
    baseline.mkdir()
    (baseline / 'network.v').write_text(generated_rtl(dest / 'nocloom_graph.v'))
    (baseline / 'bindings.json').write_text(json.dumps({'module': mapping['module'], 'data_bits': 32,
        'address_bits': 32, 'id_bits': 4, 'len_bits': 8,
        'ports': [{'name': p['name'], 'width': p['width'], 'input': p['direction'] == 'input'} for p in mapping['ports']]}))
    write_bench(baseline, bench, decerr=3)  # distinct response classes
    (work / 'clock-contract.json').write_text(json.dumps(bench, indent=2)+'\n')
    rows = []
    for name in ('baseline', 'target_address', 'cut_count', 'reset_quiet', 'restored'):
        case = work / name
        if case != baseline:
            case.mkdir()
            for f in ('network.v', 'tb.sv'):
                shutil.copyfile(baseline / f, case / f)
        modified = (case / 'network.v').read_text()
        if name in ('target_address', 'reset_quiet'):
            signal, suffix = ('nl_t_t2_awaddr', " ^ 32'd1") if name == 'target_address' else ('nl_t_t0_awvalid', ' | reset_n')
            modified, count = re.subn(r'(assign '+signal+r' = )([^;]+);', lambda m, suffix=suffix: m[1]+'('+m[2]+')'+suffix+';', modified)
            assert count == 1
        elif name == 'cut_count':
            # Freeze one FIFO's Gray write count: its receiver never sees data.
            old = count_reg['rtl']+' <= nl_internal_v'+count_reg['next'].split('/')[1]+';'
            assert modified.count(old) == 1
            modified = modified.replace(old, count_reg['rtl']+' <= '+count_reg['rtl']+';')
        (case / 'network.v').write_text(modified)
        rc = simulate(case, work / 'compiled')
        log = (case / 'simulation.log').read_text()
        expected = {'target_address': 'AW_DESCRIPTOR', 'cut_count': 'EXACT_COUNT', 'reset_quiet': 'LATE_REQUEST'}.get(name)
        ok = ('ASSERT_FAIL:GRAPH_CROSS.'+expected in log and 'AXI4_TARGET_PASS' not in log) if expected else (
            rc == 0 and 'AXI4_TARGET_PASS graph_async phases=10' in log and 'ASSERT_FAIL' not in log)
        rows.append({'case': name, 'status': ('CAUGHT' if expected else 'PASS') if ok else 'FAIL',
                     'exit': rc, 'expected_fail': expected, 'rtl_sha256': digest(case/'network.v'), 'tb_sha256': digest(case/'tb.sv')})
        (work / 'results.json').write_text(json.dumps(rows, indent=2)+'\n')
        assert ok, log[-5000:]
    crossings = re.search(r'clock_crossings:\n(?:  - .*\n)+', text)

    def with_crossing(entry):
        block = 'clock_crossings:\n  - ' + entry + '\n'
        return text.replace(crossings[0], block) if crossings else text.replace('initiators:', block + 'initiators:', 1)
    domain_block = re.search(r'clock_domains:\n(?:  - .*\n)+', text)[0]
    bad = {
        'missing_node_domain': re.sub(r', clock_domain: (middle|node[0-9]+)(?=})', '', text, count=1),
        'unknown_domain': text.replace('name: s0, clock_domain: main', 'name: s0, clock_domain: absent'),
        'depth': re.sub(r'(?m)^synchronizer_stages: [0-9]', 'synchronizer_stages: 1', text, count=1),
        'crossing_stages': with_crossing('{from_domain: middle, to_domain: main, synchronizer_stages: 5}'),
        'empty_crossing': with_crossing('{from_domain: middle, to_domain: main}'),
        'domain_stages': text.replace('{name: main, clock_port: clk_main, reset_port: reset_n', '{name: main, clock_port: clk_main, reset_port: reset_n, synchronizer_stages: 2', 1),
        'unused_domain': text.replace(domain_block, domain_block + '  - {name: unused, clock_port: clk_unused, reset_port: reset_n, test_port: test}\n'),
        'period_range': re.sub(r'(\{name: target, [^}]*?)(, period_ps: [0-9]+)?\}', r'\1, period_ps: 99}', text, count=1),
        'crossing_depth': with_crossing('{from_domain: main, to_domain: target, depth: 11}'),
        'unused_crossing': with_crossing('{from_domain: target, to_domain: main, depth: 4}'),
    }
    expected_codes = {'period_range': 'NCL_CDC_PERIOD_RANGE', 'crossing_depth': 'NCL_CDC_DEPTH_RANGE',
                      'unused_crossing': 'NCL_CDC_CROSSING_UNUSED', 'depth': 'NCL_CDC_STAGES_UNSUPPORTED',
                      'crossing_stages': 'NCL_CDC_STAGES_UNSUPPORTED', 'empty_crossing': 'NCL_SCHEMA_MISSING_FIELD',
                      'domain_stages': 'NCL_SCHEMA_UNKNOWN_FIELD'}
    # Omitted global stages default to 2 and keep the explicit-2 bytes.
    explicit_two = '\nsynchronizer_stages: 2\n'
    if explicit_two in text:
        defaulted = work / 'defaulted.yaml'
        defaulted.write_text(text.replace(explicit_two, '\n', 1))
        run([prefix/'bin/nocloom', 'generate', defaulted, '--out-dir', work/'defaulted'], 'defaulted')
        assert {p.name: digest(p) for p in (work/'defaulted').iterdir()} == artifacts[0]
    rejections = []
    for name, content in bad.items():
        path, output = work/(name+'.yaml'), work/('rejected-'+name)
        assert content != text
        path.write_text(content)
        codes = []
        for verb in ('validate', 'generate'):
            result = run([prefix/'bin/nocloom', verb, path] + (['--out-dir', output] if verb == 'generate' else []), name+'-'+verb, 1)
            codes.append(re.findall(r'NCL_[A-Z0-9_]+', result.stderr))
            assert codes[-1] and not output.exists()
        assert codes[0] == codes[1]
        assert expected_codes.get(name, codes[0][0]) in codes[0], (name, codes)
        rejections.append({'case': name, 'status': 'REJECTED', 'diagnostics': codes[0], 'no_output': True})
    (work / 'rejections.json').write_text(json.dumps(rejections, indent=2)+'\n')
    print('AXI4_TARGET_PASS installed clocked graph CLI API relocation mode='+args.mode)


if __name__ == '__main__':
    main()
