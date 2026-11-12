"""Installed mixed target protocols through the public CLI and Core API."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ahb_mixed_axi_bench import write_bench as write_axi_bench
from ahb_outlet_bench import write_bench
from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def protocol_check(cli, work):
    config = work/'input.yaml'
    config.write_text('''schema_version: 10
name: ahb_protocol
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators: [{name: s0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}]
targets: [{name: t0, clock_domain: main, protocol: ahb_v3, data_width: 32, attributes: amba, mappings: [{base: '0x1000', size: '0x1000'}]}]
connectivity: all
output: {backend: verilog, language: verilog_2001}
''')
    dest = work/'generated'
    subprocess.run([cli, 'generate', str(config), '--out-dir', str(dest)], check=True, timeout=180)
    mapping = json.loads((dest/'nocloom_ahb_protocol.map.json').read_text())
    (work/'network.v').write_text(generated_rtl(dest/'nocloom_ahb_protocol.v'))
    count = write_bench(work, mapping, decerr=3, protocol_only=True)
    rc = simulate(work, work/'compiled')
    log = (work/'simulation.log').read_text()
    assert rc == 0 and f'NOCLOOM_PASS ahb-outlet cases={count}' in log and 'ASSERT_FAIL:' not in log, log[-3000:]


def main():
    p = argparse.ArgumentParser()
    for key in ('source', 'build', 'compiler', 'work'):
        p.add_argument('--'+key, required=True)
    p.add_argument('--protocol-only', action='store_true')
    p.add_argument('--cli')
    a = p.parse_args()
    source, build, work = (Path(getattr(a, key)).resolve() for key in ('source', 'build', 'work'))
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=work))
    if a.protocol_only:
        protocol_check(a.cli, work)
        return
    commands, results = [], []

    def run(argv, label, expected=0):
        argv = list(map(str, argv)); start = time.monotonic()
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        (work/(label+'.log')).write_text(r.stdout+r.stderr)
        commands.append({'argv': argv, 'exit': r.returncode, 'seconds': time.monotonic()-start})
        (work/'commands.json').write_text(json.dumps(commands, indent=2)+'\n')
        assert r.returncode == expected, r.stderr
        return r

    prefix, consumer = work/'installed', work/'consumer'
    run(['cmake', '--install', build, '--prefix', prefix], 'install')
    consumer.mkdir(exist_ok=True)
    shutil.copyfile(source/'tests/ahb_public_consumer.cpp', consumer/'main.cpp')
    shutil.copyfile(source/'tests/endpoint_ports.hpp', consumer/'endpoint_ports.hpp')
    (consumer/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\nproject(consumer LANGUAGES CXX)\nfind_package(nocloom CONFIG REQUIRED)\nadd_executable(consumer main.cpp)\ntarget_link_libraries(consumer PRIVATE nocloom::core)\n')
    run(['cmake', '-S', consumer, '-B', consumer/'build', '-DCMAKE_CXX_COMPILER='+a.compiler, '-DCMAKE_PREFIX_PATH='+str(prefix)], 'configure')
    run(['cmake', '--build', consumer/'build', '-j2'], 'build')

    def simulation(case, expected=None, cases=None):
        rc = simulate(case, work/'compiled'); text = (case/'simulation.log').read_text()
        if expected:
            assert 'ASSERT_FAIL:AHB_OUTLET.'+expected in text and 'NOCLOOM_PASS' not in text
        else:
            marker = f'NOCLOOM_PASS ahb-outlet cases={cases}\n' if cases else 'NOCLOOM_PASS mixed AXI target cases=2'
            assert rc == 0 and marker in text and 'ASSERT_FAIL:' not in text
        results.append({'case': str(case.relative_to(work)), 'status': 'CAUGHT' if expected else 'PASS', 'expected_fail': expected,
                            'simulation_exit': rc, 'rtl_sha256': hashlib.sha256((case/'network.v').read_bytes()).hexdigest(),
                            'tb_sha256': hashlib.sha256((case/'tb.sv').read_bytes()).hexdigest()})
        (work/'results.json').write_text(json.dumps(results, indent=2)+'\n')

    # Selection 3: both targets AHB behind a 64-bit source; t0 sends complete
    # write bursts without undefined-length INCR, t1 only drops INCR and has
    # the 2-bit AMBA 2 HRESP (RETRY and SPLIT complete as SLVERR). Selection 2
    # spells the default 1-bit HRESP at t0; the API leaves it out.
    shaping = {0: ', write_bursts: complete, undefined_length_bursts: false',
               1: ', undefined_length_bursts: false, hresp_width: 2'}
    for selected, word, width in [(0, 4, 32), (1, 32, 32), (2, 8, 32), (2, 16, 128), (3, 4, 64)]:
        root = work/f'target{selected}-word{word}-width{width}';root.mkdir()
        both = selected in (2, 3)
        shaped = {i: shaping[i] if selected == 3 else '' for i in range(2)}
        if selected == 2:
            shaped[0] = ', hresp_width: 1'
        hresp = {i: 2 if selected == 3 and i == 1 else 1 for i in range(2)}
        protocol = {i: 'ahb_v3' if selected == i or both else 'axi4' for i in range(2)}
        lens = {i: ', len_width: 8' if protocol[i] == 'axi4' else '' for i in range(2)}
        text = f'''schema_version: 10
name: ahb_public
address_width: 32
arbitration: {{policy: round_robin}}
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains: [{{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}}]
initiators: [{{name: s0, clock_domain: main, protocol: axi4, data_width: {width}, len_width: 8}}, {{name: s1, clock_domain: main, protocol: axi4, data_width: {width}, len_width: 8}}]
targets:
 - {{name: t0, clock_domain: main, protocol: {protocol[0]}, data_width: 32{shaped[0]}{lens[0]}, attributes: amba, mappings: [{{base: '0x1000', size: '0x1000'}}]}}
 - {{name: t1, clock_domain: main, protocol: {protocol[1]}, data_width: 32{shaped[1]}{lens[1]}, attributes: amba, mappings: [{{base: '0x5000', size: '0x1000'}}]}}
transport: {{max_outstanding: 1, transport_word_bytes: {word}}}
connectivity: all
output: {{backend: verilog, language: verilog_2001}}
'''
        config = root/'input.yaml';config.write_text(text);hashes=[]
        for origin in ['cli', 'api', 'repeat']:
            dest = root/origin
            if origin == 'api':
                run([consumer/'build/consumer', dest, selected, word, width], f't{selected}-api')
            else:
                run([prefix/'bin/nocloom', 'generate', config, '--out-dir', dest], f't{selected}-{origin}')
            mapping = json.loads((dest/'nocloom_ahb_public.map.json').read_text())
            bundle = json.loads((dest/'nocloom_ahb_public.bundle.json').read_text())
            assert mapping['definition'] == bundle['definition']
            assert mapping['definition']['endpoint_protocols']['targets'] == {
                f't{i}': 'ahb_v3' if selected == i or both else 'axi4' for i in range(2)}
            assert mapping['definition']['ahb_hresp_widths'] == {
                f't{i}': hresp[i] for i in range(2) if selected == i or both}
            for i in range(2):
                port = [q for q in mapping['ports'] if q['name'] == f'nl_t_t{i}_hresp']
                assert [q['width'] for q in port] == ([hresp[i]] if selected == i or both else [])
            assert mapping['definition'].get('ahb_bursts') == ({
                't0': {'write_bursts': 'complete', 'undefined_length_bursts': False},
                't1': {'write_bursts': 'packet', 'undefined_length_bursts': False}}
                if selected == 3 else None)
            hashes.append({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()})
            if origin == 'repeat': continue
            for target in range(2):
                if selected != target and not both: continue
                for source_index in range(2):
                    case = dest/f't{target}-s{source_index}';case.mkdir()
                    (case/'network.v').write_text(generated_rtl(dest/'nocloom_ahb_public.v'))
                    cases = write_bench(case, mapping, source_index, target,
                                        complete=selected == 3 and target == 0, blocks=selected == 3,
                                        hresp_width=hresp[target], decerr=3,
                                        gather=word, run_word=max(word, width // 8))
                    simulation(case, cases=cases)
            if origin == 'cli' and selected == 1:
                case=dest/'contention';case.mkdir()
                (case/'network.v').write_text(generated_rtl(dest/'nocloom_ahb_public.v'))
                cases = write_bench(case,mapping,0,1,contend=True,decerr=3,gather=word, run_word=max(word, width // 8))
                simulation(case, cases=cases)
            if not both:
                for source_index in range(2):
                    case = dest/f'axi-t{1-selected}-s{source_index}';case.mkdir()
                    (case/'network.v').write_text(generated_rtl(dest/'nocloom_ahb_public.v'))
                    write_axi_bench(case, mapping, 1-selected, source_index);simulation(case)
            if origin == 'cli' and selected == 0:
                baseline = dest/'t0-s0'
                original = (baseline/'network.v').read_text()
                for signal, expected in [('haddr', 'ADDRESS'), ('hprot', 'PROTECTION')]:
                    fault = dest/('fault-'+signal);fault.mkdir()
                    bad, count = re.subn(r'(assign nl_t_t0_'+signal+r' = )[^;]+;', r'\g<1>0;', original)
                    assert count == 1
                    (fault/'network.v').write_text(bad);shutil.copyfile(baseline/'tb.sv', fault/'tb.sv');simulation(fault, expected)
                restored = dest/'restored';restored.mkdir()
                for f in ['network.v', 'tb.sv']:shutil.copyfile(baseline/f, restored/f)
                simulation(restored, cases=cases)
        assert hashes[0] == hashes[1] == hashes[2]

        def t0_hresp(value):
            # t0 with only this HRESP width: AHB in selections 0, 2 and 3, AXI4 in 1.
            line = next(x for x in text.splitlines() if x.startswith(' - {name: t0,'))
            fresh = re.sub(r', hresp_width: [^,}]+', '', line).replace(
                '{name: t0, clock_domain: main', '{name: t0, clock_domain: main, hresp_width: ' + value)
            return text.replace(line, fresh)
        for label, bad, code in [
            ('protocol', text.replace('ahb_v3', 'ahb_bad'), 'NCL_UNSUPPORTED_PROTOCOL'),
            # Every endpoint at 64 bits: a 64-bit network, which AHB targets reject.
            ('data', text.replace('data_width: 128', 'data_width: 64').replace('data_width: 32', 'data_width: 64'), 'NCL_AHB_OUTLET_PROFILE'),
            ('address', text.replace('address_width: 32', 'address_width: 40'), 'NCL_AHB_OUTLET_PROFILE'),
            # HRESP is 1 or 2 bits and exists only on AHB targets.
            ('hresp_domain', t0_hresp('3'), 'NCL_TARGET_AHB_HRESP'),
            ('hresp_type', t0_hresp("'2'"), 'NCL_SCHEMA_TYPE')]:
            invalid=root/(label+'.yaml');invalid.write_text(bad);dest=root/('invalid-'+label)
            r=run([prefix/'bin/nocloom','generate',invalid,'--out-dir',dest],f't{selected}-{label}',1)
            assert code in r.stderr and not dest.exists()
    print('NOCLOOM_PASS installed dedicated AHB targets')

    ahb5 = work / 'ahb5-api'
    run([consumer / 'build/consumer', ahb5, 4, 4, 32], 'ahb5-api')
    amap = json.loads((ahb5 / 'nocloom_ahb_public.map.json').read_text())
    ports = {p['name']: p for p in amap['ports']}
    assert ports['nl_i_s0_hmaster']['width'] == 5
    assert ports['nl_t_t0_hmaster']['width'] == ports['nl_t_t1_hmaster']['width'] == 7
    assert ports['nl_i_s0_hprot']['width'] == ports['nl_t_t0_hprot']['width'] == 7


if __name__ == '__main__':
    main()
