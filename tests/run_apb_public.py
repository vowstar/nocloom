"""Generate real APB targets through installed CLI and independent Core consumers."""
import argparse
import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from apb_burst_outlet_bench import write_bench
from cross_hardware_sim import simulate
from endpoint_ports import endpoint_ports
from sim_engine import generated_rtl


def configuration(mode):
    clocks = [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}]
    targets = [{'name': f't{i}', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                'mappings': [{'base': hex(0x1000+0x4000*i), 'size': '0x1000'}]}
               for i in range(2 if mode == 'funnel' else 3)]
    targets[1]['protocol'] = 'apb4'
    if len(targets) == 3:
        targets[2]['protocol'] = 'ahb_v3'
    transport = {'max_outstanding': 1, 'transport_word_bytes': 4}
    topology = None
    if mode != 'funnel':
        topology = {
            'request': {'nodes': [{'name': 'q0', 'transport_word_bytes': 16},
                                  {'name': 'q1', 'transport_word_bytes': 8}],
                        'links': [{'from': 'q0', 'to': 'q1', 'transport_word_bytes': 4}],
                        'entries': [{'endpoint': f's{i}', 'node': 'q0'} for i in range(2)],
                        'exits': [{'endpoint': f't{i}', 'node': 'q1'} for i in range(3)]},
            'response': {'nodes': [{'name': 'p0', 'transport_word_bytes': 8},
                                   {'name': 'p1', 'transport_word_bytes': 16}],
                         'links': [{'from': 'p0', 'to': 'p1', 'transport_word_bytes': 4}],
                         'entries': [{'endpoint': f't{i}', 'node': 'p0'} for i in range(3)],
                         'exits': [{'endpoint': f's{i}', 'node': 'p1'} for i in range(2)]}}
    if mode == 'clocked':
        clocks = [{'name': name, 'clock_port': 'clk_' + name, 'reset_port': 'reset_n', 'test_port': 'test'}
                  for name in ('main', 'middle', 'target')]
        crossings = [{'from_domain': 'main', 'to_domain': 'middle', 'synchronizer_stages': 4},
                     {'from_domain': 'middle', 'to_domain': 'target', 'synchronizer_stages': 3}]
        targets[1]['clock_domain'] = 'target'
        for direction, owners in [('request', ['main', 'middle']),
                                  ('response', ['middle', 'main'])]:
            for node, owner in zip(topology[direction]['nodes'], owners, strict=True):
                node['clock_domain'] = owner
    config = {'schema_version': 10, 'name': 'apb_public', 'address_width': 32,
              'arbitration': {'policy': 'round_robin'}, 'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': clocks,
              'initiators': [{'name': f's{i}', 'clock_domain': 'main', 'protocol': 'axi4',
                              'data_width': 32} for i in range(2)],
              'targets': targets, 'transport': transport, 'connectivity': 'all',
              'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if topology:
        config['topology'] = topology
    if mode == 'clocked':
        config['clock_crossings'] = crossings
    return endpoint_ports(config, id_width=4, len_width=8)


def main():
    parser = argparse.ArgumentParser()
    for key in ('source', 'build', 'compiler', 'work'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--mode', choices=['funnel', 'graph', 'clocked'], required=True)
    args = parser.parse_args()
    source, build, base = (Path(getattr(args, k)).resolve() for k in ('source', 'build', 'work'))
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base/'latest.txt').write_text(str(work)+'\n')
    commands = []

    def run(argv, label, expected=0):
        argv = list(map(str, argv)); start = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        (work/(label+'.log')).write_text(proc.stdout+proc.stderr)
        commands.append({'argv': argv, 'exit': proc.returncode, 'seconds': time.monotonic()-start})
        (work/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')
        assert proc.returncode == expected, proc.stderr
        return proc

    original, prefix, consumer = work/'original-prefix', work/'relocated-prefix', work/'consumer'
    run(['cmake', '--install', build, '--prefix', original], 'install')
    shutil.move(original, prefix)
    consumer.mkdir()
    for name in ['apb_public_consumer.cpp', 'apb_public_definition.hpp', 'endpoint_ports.hpp']:
        shutil.copyfile(source/'tests'/name, consumer/name)
    (consumer/'CMakeLists.txt').write_text(
        'cmake_minimum_required(VERSION 3.20)\nproject(consumer LANGUAGES CXX)\n'
        'find_package(nocloom CONFIG REQUIRED)\nadd_executable(consumer apb_public_consumer.cpp)\n'
        'target_link_libraries(consumer PRIVATE nocloom::core)\n')
    run(['cmake', '-S', consumer, '-B', consumer/'build', '-DCMAKE_CXX_COMPILER='+args.compiler,
         '-DCMAKE_PREFIX_PATH='+str(prefix), '-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'], 'consumer-configure')
    run(['cmake', '--build', consumer/'build', '-j2'], 'consumer-build')
    config = configuration(args.mode)
    yaml = work/'input.yaml'; yaml.write_text(json.dumps(config, indent=2)+'\n')
    hashes = []
    for origin in ['cli', 'api', 'repeat']:
        dest = work/origin
        if origin == 'api':
            run([consumer/'build/consumer', dest, args.mode], origin)
        else:
            run([prefix/'bin/nocloom', 'generate', yaml, '--out-dir', dest], origin)
        artifacts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
        assert len(artifacts) == 6
        hashes.append(artifacts)
    assert hashes[0] == hashes[1] == hashes[2]
    (work/'artifact-comparison.json').write_text(json.dumps(hashes, indent=2)+'\n')
    mapping = json.loads((work/'cli/nocloom_apb_public.map.json').read_text())
    bundle = json.loads((work/'cli/nocloom_apb_public.bundle.json').read_text())
    assert mapping['definition'] == bundle['definition']
    expected_protocols = {'t0': 'axi4', 't1': 'apb4'}
    if args.mode != 'funnel':
        expected_protocols['t2'] = 'ahb_v3'
    assert mapping['definition']['endpoint_protocols']['targets'] == expected_protocols
    ports = {p['signal']: p['width'] for p in mapping['ports'] if p['name'].startswith('nl_t_t1_')}
    assert ports == {'paddr': 32, 'pwdata': 32, 'prdata': 32, 'pprot': 3, 'pstrb': 4,
                     'pwrite': 1, 'psel': 1, 'penable': 1, 'pready': 1, 'pslverr': 1}
    results = []

    def simulation(case, expected=None):
        rc = simulate(case, work/'compiled')
        text = (case/'simulation.log').read_text()
        if expected:
            # The label is terminated: one label must not match a longer one.
            assert 'ASSERT_FAIL:APB_BURST_OUTLET.'+expected+' case=' in text and 'NOCLOOM_PASS' not in text
        else:
            assert rc == 0 and 'NOCLOOM_PASS apb-burst-outlet cases=18' in text and 'ASSERT_FAIL:' not in text
        results.append({'case': case.name, 'status': 'CAUGHT' if expected else 'PASS',
                        'expected_fail': expected, 'simulation_exit': rc,
                        'rtl_sha256': hashlib.sha256((case/'network.v').read_bytes()).hexdigest(),
                        'tb_sha256': hashlib.sha256((case/'tb.sv').read_bytes()).hexdigest()})
        (work/'results.json').write_text(json.dumps(results, indent=2)+'\n')

    for index in range(2):
        case = work/f'source{index}'; case.mkdir()
        (case/'network.v').write_text(generated_rtl(work/'cli/nocloom_apb_public.v'))
        write_bench(case, mapping, index, 1, decerr=3, run_word=4 if args.mode == 'funnel' else 16)
        simulation(case)  # distinct response classes
    baseline = work/'source0'; rtl = (baseline/'network.v').read_text()
    for signal, expected in [('paddr', 'READ_ADDRESS'), ('pprot', 'PROTECTION'), ('pstrb', 'WRITE_STROBE')]:
        case = work/('fault-'+signal); case.mkdir()
        replacement = (lambda m: m[1]+"(nl_t_t1_pwrite ? ("+m[2]+") : 32'b0);") if signal == 'paddr' else (lambda m: m[1]+'0;')
        bad, count = re.subn(r'(assign nl_t_t1_'+signal+r' = )([^;]+);', replacement, rtl)
        assert count == 1
        (case/'network.v').write_text(bad)
        shutil.copyfile(baseline/'tb.sv', case/'tb.sv'); simulation(case, expected)
    # Every graph publishes storage obligations, the generated switch of the
    # default mode included. The bench monitors the APB target's owners there.
    assert 'storage' in mapping['network']
    if 'storage' in mapping['network']:
        # Release the APB read owner on the current word being the last
        # (r.last) instead of on the offered response being the last: the
        # owner is then released when the second-to-last response leaves,
        # before the last read access. The map names the same wire.
        store = next(s for s in mapping['network']['storage']
                     if s['resource'] == 'burst_service/t1/read')
        index = lambda ref: ref.removeprefix('value/')
        control = [index(c) for c in store['control']]
        release = index(store['release'])
        found = re.search(r'^assign nl_internal_v'+release+r' = \((nl_internal_v\d+) & nl_internal_v(\d+)\);$', rtl, re.M)
        assert found and found[2] in control
        remaining = [c for c in control if re.search(r'^reg \[7:0\] nl_internal_v'+c+';$', rtl, re.M)]
        assert len(remaining) == 1
        last = [m[1] for m in re.finditer(r'^assign (nl_internal_v\d+) = \(nl_internal_v'+remaining[0]+r' == (nl_internal_v\d+)\);$', rtl, re.M)
                if re.search(r'^assign '+m[2]+r" = 8'd0;$", rtl, re.M)]
        assert len(last) == 1
        case = work/'fault-release'; case.mkdir()
        bad, count = re.subn(r'^(assign nl_internal_v'+release+r' = \(nl_internal_v\d+ & )nl_internal_v\d+\);$',
                             lambda m: m[1]+last[0]+');', rtl, flags=re.M)
        assert count == 1
        (case/'network.v').write_text(bad)
        shutil.copyfile(baseline/'tb.sv', case/'tb.sv'); simulation(case, 'READ_RELEASE')
        # The write owner: released at its acquisition (before any write
        # access), or never. Releasing on the response valid alone is
        # equivalent here: the network takes this write response at once.
        store = next(s for s in mapping['network']['storage']
                     if s['resource'] == 'burst_service/t1/write')
        release, acquire = index(store['release']), index(store['acquire'])
        pattern = r'^assign nl_internal_v'+release+r' = \((nl_internal_v\d+) & nl_internal_v\d+\);$'
        assert re.search(pattern, rtl, re.M)
        for label, replacement, expected in [
                ('acquire', lambda m: 'assign nl_internal_v'+release+' = nl_internal_v'+acquire+';', 'WRITE_RELEASE'),
                ('never', lambda m: 'assign nl_internal_v'+release+" = 1'd0;", 'RELEASE_ONCE')]:
            case = work/('fault-write-release-'+label); case.mkdir()
            bad, count = re.subn(pattern, replacement, rtl, flags=re.M)
            assert count == 1
            (case/'network.v').write_text(bad)
            shutil.copyfile(baseline/'tb.sv', case/'tb.sv'); simulation(case, expected)
    restored = work/'restored'; restored.mkdir()
    for name in ['network.v', 'tb.sv']:
        shutil.copyfile(baseline/name, restored/name)
    simulation(restored)
    negatives = []
    # 'window' is not word aligned; 'odd_size' is word aligned but not a
    # power of two.
    for label, value in [('protocol', 'apb5'), ('address', 40), ('width', 64), ('window', '0x182'),
                         ('odd_size', '0x180')]:
        bad = copy.deepcopy(config)
        if label == 'protocol': bad['targets'][1]['protocol'] = value
        elif label == 'address': bad['address_width'] = value
        elif label == 'width':
            for endpoint in bad['initiators'] + bad['targets']:
                endpoint['data_width'] = value
        else: bad['targets'][1]['mappings'][0]['size'] = value
        inp = work/(label+'.yaml'); inp.write_text(json.dumps(bad)+'\n')
        dest = work/('rejected-'+label)
        proc = run([prefix/'bin/nocloom', 'generate', inp, '--out-dir', dest], 'reject-'+label, 1)
        code = {'protocol': 'NCL_UNSUPPORTED_PROTOCOL', 'address': 'NCL_APB_BURST_OUTLET_PROFILE',
                'width': 'NCL_APB_BURST_OUTLET_PROFILE', 'window': 'NCL_ADDRESS_RANGE',
                'odd_size': 'NCL_ADDRESS_RANGE'}[label]
        assert code in proc.stderr and not dest.exists(), (label, proc.stderr)
        negatives.append({'case': label, 'status': 'REJECTED', 'no_output': True,
                          'stderr': proc.stderr})
    (work/'rejections.json').write_text(json.dumps(negatives, indent=2)+'\n')
    print('NOCLOOM_PASS installed APB burst targets '+args.mode)


if __name__ == '__main__':
    main()
