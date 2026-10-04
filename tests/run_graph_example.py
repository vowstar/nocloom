"""Relocated public graph generation and independent external AXI transactions."""
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
from graph_cross_bench import write_bench as cross_bench
from selective_graph_bench import graph_bench, selective_bench
from sim_engine import generated_rtl


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    for key in ('source', 'build', 'compiler', 'work'):
        parser.add_argument('--' + key, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--selective", action="store_true")
    mode.add_argument("--cross", action="store_true")
    args = parser.parse_args()
    selective = args.selective or args.cross
    source, build, work = (Path(getattr(args, k)).resolve() for k in ('source', 'build', 'work'))
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    commands, results = [], []

    def run(argv, label, expected=0):
        argv = list(map(str, argv))
        start = time.monotonic()
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        (work / (label + '.log')).write_text(result.stdout + result.stderr)
        commands.append({'argv': argv, 'exit': result.returncode, 'seconds': time.monotonic() - start})
        (work / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    installed, prefix, consumer = work / 'installed', work / 'relocated', work / 'consumer'
    run(['cmake', '--install', build, '--prefix', installed], 'install')
    installed.rename(prefix)
    consumer.mkdir()
    for name in ('graph_example_consumer.cpp', 'graph_example_definition.hpp'):
        shutil.copyfile(source / 'tests' / name, consumer / name)
    (consumer / 'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\nproject(consumer LANGUAGES CXX)\n'
        'find_package(nocloom CONFIG REQUIRED)\n'
        'add_executable(consumer graph_example_consumer.cpp)\n'
        'target_link_libraries(consumer PRIVATE nocloom::core)\n')
    run(['cmake', '-S', consumer, '-B', consumer / 'build', '-DCMAKE_CXX_COMPILER=' + args.compiler,
         '-DCMAKE_PREFIX_PATH=' + str(prefix), '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON', '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF'], 'configure')
    run(['cmake', '--build', consumer / 'build', '-j2'], 'consumer-build')
    config = source / ('examples/axi4_graph_mixed.yaml' if args.cross else 'examples/axi4_graph_selective.yaml' if selective else 'examples/axi4_graph.yaml')
    artifacts = []
    for origin in ('cli', 'api', 'repeat'):
        dest = work / origin
        if origin == 'api':
            run([consumer / 'build/consumer', dest] + (['mixed' if args.cross else 'selective'] if selective else []), origin)
        else:
            run([prefix / 'bin/nocloom', 'validate', config], origin + '-validate')
            run([prefix / 'bin/nocloom', 'generate', config, '--out-dir', dest], origin)
        artifacts.append({p.name: digest(p) for p in dest.iterdir()})
        mapping = json.loads((dest / 'nocloom_graph.map.json').read_text())
        bundle = json.loads((dest / 'nocloom_graph.bundle.json').read_text())
        assert mapping['definition'] == bundle['definition']
        topo, network = mapping['definition']['topology'], mapping['network']
        expected_words = {'leaf0': 4, 'leaf1': 4, 'leaf2': 4, 'merge': 32, 'branch': 16, 'tail': 8}
        assert {n['name']: n['transport_word_bytes'] for n in topo['request']['nodes']} == expected_words
        assert {n['name']: n['transport_word_bytes'] for n in topo['response']['nodes']} == {'merge': 8, 'branch': 32}
        identities = [n['hardware_node'] for key in ('request','response') for n in topo[key]['nodes']]
        assert len(identities) == len(set(identities)) == 8
        for direction, entries, exits in [('request', 's', 't'), ('response', 't', 's')]:
            routes = network[direction + '_routes']
            admitted = {(i,j) for i in range(3) for j in range(3) if not selective or j in (i,(i+1)%3)}
            expected = {(entries+str(i), exits+str(j)) if direction=='request' else (entries+str(j), exits+str(i)) for i,j in admitted}
            assert {(r['entry'], r['exit']) for r in routes} == expected
            assert len(routes) == len(expected)
            assert all(r['hardware_nodes'] for r in routes)
        if args.cross:
            ports = {p['name']: p['width'] for p in mapping['ports']}
            assert mapping['definition']['endpoint_data_widths'] == {
                'initiators': {'s0': 128, 's1': 32, 's2': 256},
                'targets': {'t0': 32, 't1': 32, 't2': 32}, 'transport': 'per_endpoint_attachment'}
            for i, bits in enumerate((128, 32, 256)):
                assert ports[f'nl_i_s{i}_wdata'] == ports[f'nl_i_s{i}_rdata'] == bits
                assert ports[f'nl_i_s{i}_wstrb'] == bits // 8
                assert (f'nl_i_s{i}_awlen' in ports) == (i != 1)
        assert network['adapters'] and network['storage']
        # The domain's reset release registers are stored too (resynchronized release).
        expected_stores = {r['name'] for r in network['resources'] if r['role'] != 'environment'}
        stores = network['storage']
        assert expected_stores == {s['resource'] for s in stores}
        assert len(stores) == len(expected_stores)
        assert all(s['control'] or s['payload'] for s in stores)
        if origin == 'repeat':
            continue
        case = dest / 'baseline'
        case.mkdir()
        (case / 'network.v').write_text(generated_rtl(dest / 'nocloom_graph.v'))
        meta = {'module': mapping['module'], 'data_bits': 32, 'address_bits': 32, 'id_bits': 4, 'len_bits': 8,
                'target_ids': mapping['definition'].get('target_ids'),
                'ports': [{'name': p['name'], 'width': p['width'], 'input': p['direction'] == 'input'} for p in mapping['ports']],
                'targets': [{'name': t['name'], 'base': int(t['begin']), 'size': int(t['end']) - int(t['begin'])} for t in mapping['definition']['targets']]}
        (case / 'bindings.json').write_text(json.dumps(meta, indent=2) + '\n')
        # The public default preserves DECERR for a miss.
        if args.cross:
            cross_bench(case, mapping, decerr=3)
        elif selective:
            selective_bench(case, decerr=3)
        else:
            graph_bench(case, decerr=3)
        cases = [(case, None)]
        if origin == 'cli':
            faults = [('address', 'nl_t_t2_awaddr', 'RAW_WRITE_ADDRESS'), ('identity', 'nl_i_s2_rid', 'R_ID')]
            forced = {}
            if selective:
                admission = next(x for x in network['admission'] if x['source']=='s0')
                # A forced admission with the denied target's index sends the denied
                # burst into the graph toward t2, a target outside the matrix.
                faults = [(op, 'nl_internal_v'+admission[op+'_route_match'].split('/')[1],
                           ('DENIED_'+op.upper()+'_COMPLETION', 'FORBIDDEN_'+op.upper(), op.upper()+'_ROUTE'))
                          for op in ('write','read')]
                forced = {op: 'nl_internal_v'+admission[op+'_route_target'].split('/')[1] for op in ('write','read')}
            if args.cross:
                forced = {}
                # hold_r/hold_b: source 0's response shows only while it is
                # ready, so a held response never reaches the held source.
                faults = [('write', 'nl_t_t2_wdata', 'W_DATA'),
                          ('read', 'nl_i_s2_rdata', 'R_DATA'),
                          ('strobe', 'nl_t_t2_wstrb', 'W_STROBE'),
                          ('hold_r', 'nl_i_s0_rvalid', 'R_HOLD_SOURCE'),
                          ('hold_b', 'nl_i_s0_bvalid', 'B_HOLD_SOURCE')]
            for name, signal, expected in faults:
                fault = dest / ('fault-' + name)
                fault.mkdir()
                rtl = (case / 'network.v').read_text()
                def inject(match, name=name):
                    if name in ('hold_r', 'hold_b'):
                        return match[1] + f"({match[2]}) & nl_i_s0_{name[-1]}ready;"
                    if args.cross:
                        mask = "256'h" + format(1 << 255, 'x') if name == 'read' else "4'd8" if name == 'strobe' else "32'd1"
                        return match[1] + f"({match[2]}) ^ {mask};"
                    return match[1] + ("1'b1" if selective else f"({match[2]}) ^ 32'd1" if name == 'address' else '0') + ';'
                rtl, count = re.subn(r'(assign ' + signal + r' = )([^;]+);', inject, rtl)
                assert count == 1
                if name in forced:
                    rtl, count = re.subn(r'(assign ' + forced[name] + r' = )([^;]+);', r"\g<1>2'd2;", rtl)
                    assert count == 1
                (fault / 'network.v').write_text(rtl)
                shutil.copyfile(case / 'tb.sv', fault / 'tb.sv')
                cases.append((fault, expected))
            restored = dest / 'restored'
            restored.mkdir()
            for filename in ('network.v', 'tb.sv'):
                shutil.copyfile(case / filename, restored / filename)
                assert digest(case / filename) == digest(restored / filename)
            cases.append((restored, None))
        for current, expected in cases:
            start = time.monotonic()
            rc = simulate(current, work / 'compiled')
            text = (current / 'simulation.log').read_text()
            marker = ('AXI4_TARGET_PASS graph_cross phases=10 admitted_pairs=6 denied_pairs=3 reset_mid_packet=1' if args.cross
                      else 'AXI4_TARGET_PASS real_exchange cases=16')
            observation = marker if args.cross else ('SELECTIVE_OBSERVATION admitted_pairs=6 denied_pairs=3' if selective else 'GRAPH_OBSERVATION routes=9 reset_mid_packet=1')
            passed = rc == 0 and marker in text and observation in text and 'ASSERT_FAIL' not in text
            failure_prefix = 'ASSERT_FAIL:GRAPH_CROSS.' if args.cross else 'ASSERT_FAIL:AXI4_MASTER.'
            expected_set = (expected,) if isinstance(expected, str) else expected or ()
            caught = expected is not None and any(failure_prefix + e in text for e in expected_set) and 'AXI4_TARGET_PASS' not in text
            results.append({'case': str(current.relative_to(work)), 'status': 'CAUGHT' if caught else 'PASS' if passed and expected is None else 'FAIL',
                            'expected_fail': expected, 'exit': rc, 'seconds': time.monotonic() - start,
                            'rtl_sha256': digest(current / 'network.v'), 'tb_sha256': digest(current / 'tb.sv')})
            (work / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            assert results[-1]['status'] != 'FAIL', text[-4000:]
    assert artifacts[0] == artifacts[1] == artifacts[2]
    (work / 'artifact-comparison.json').write_text(json.dumps(artifacts, indent=2) + '\n')
    text = config.read_text()
    bad_cases = {
        'unknown': text.replace('topology:', 'unknown_topology:'),
        'width': text.replace('transport_word_bytes: 32', 'transport_word_bytes: 3'),
        'absent': text.replace('from: leaf0, to: merge', 'from: absent, to: merge'),
        # A back link that no route takes closes a cycle but carries nothing; a link into
        # its own node would route through that node twice.
        'cycle': text.replace('    entries:', '      - {from: tail, to: merge}\n    entries:', 1),
        'self_link': text.replace('      - {from: branch, to: tail}\n',
                                  '      - {from: branch, to: tail}\n      - {from: tail, to: tail}\n', 1),
        'ambiguous': text.replace('    entries:', '      - {from: leaf0, to: tail}\n    entries:', 1),
        'attachment': text.replace('endpoint: s0, node: leaf0', 'endpoint: absent, node: leaf0'),
        'duplicate': text.replace('name: leaf1', 'name: leaf0'),
    }
    # An attachment of an undeclared endpoint leaves a connected pair without
    # its graph endpoint.
    expected_codes = {'unknown': 'NCL_SCHEMA_UNKNOWN_FIELD', 'width': 'NCL_AXI4_EXCHANGE_PROFILE',
                      'absent': 'NCL_AXI4_TOPOLOGY', 'cycle': 'NCL_SWITCH_UNUSED_OBJECT',
                      'self_link': 'NCL_SWITCH_ROUTE_CYCLE',
                      'ambiguous': 'NCL_SWITCH_ROUTE_AMBIGUOUS', 'attachment': 'NCL_SWITCH_PAIR',
                      'duplicate': 'NCL_AXI4_TOPOLOGY'}
    # A route entry keyed by source instead of initiator is an unknown field.
    bad_cases['old_routes'] = text.replace('topology:\n', 'topology:\n  routes: [{source: s0, target: t0}]\n', 1)
    expected_codes['old_routes'] = 'NCL_SCHEMA_UNKNOWN_FIELD'
    if selective:
        bad_cases.update(empty_pairs=re.sub(r'connectivity:\n.*?(?=topology:)', 'connectivity: {}\n', text, flags=re.DOTALL),
                         duplicate_pair=text.replace('s0: [t0, t1]', 's0: [t0, t0]'),
                         unknown_pair=text.replace('s0: [t0, t1]', 'absent: [t0, t1]'))
        expected_codes.update(empty_pairs='NCL_CONNECTIVITY_UNUSED_SOURCE',
                              duplicate_pair='NCL_CONNECTIVITY_DUPLICATE', unknown_pair='NCL_CONNECTIVITY_REFERENCE')
    else:
        bad_cases['missing_connectivity'] = text.replace('connectivity: all\n', '')
        expected_codes['missing_connectivity'] = 'NCL_CONNECTIVITY_REQUIRED'
    assert all(bad_cases[label] != text for label in bad_cases)
    negatives = []
    for label, content in bad_cases.items():
        path, dest = work / (label + '.yaml'), work / ('rejected-' + label)
        path.write_text(content)
        codes = []
        for verb in ('validate', 'generate'):
            argv = [prefix / 'bin/nocloom', verb, path]
            if verb == 'generate':
                argv += ['--out-dir', dest]
            result = run(argv, label + '-' + verb, 1)
            codes.append(re.findall(r'NCL_[A-Z0-9_]+', result.stderr))
            assert expected_codes[label] in codes[-1] and not dest.exists(), result.stderr
        assert codes[0] == codes[1]
        negatives.append({'case': label, 'status': 'REJECTED', 'diagnostics': codes[0], 'no_output': not dest.exists()})
    (work / 'rejections.json').write_text(json.dumps(negatives, indent=2) + '\n')
    print('AXI4_TARGET_PASS installed graph CLI API and relocation')


if __name__ == '__main__':
    main()
