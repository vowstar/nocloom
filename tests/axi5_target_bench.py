"""Target AxREGION and AxQOS: the region of each address map window and the AxQOS of each
scheduling level at AXI5 and AXI4 targets, through packet splitting and a clock crossing."""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

from axi5_bench import bench
from cross_hardware_sim import simulate
from sim_engine import generated_rtl

# Target name -> (protocol, AxQOS of each level, mappings as (base, region)). Every mapping is
# 32 KiB at local address 0, so the windows of one target alias one local range.
TARGETS = {
    'r5': ('axi5', [1, 2, 3, 5, 8, 9, 13, 15], [(0x00000, 3), (0x08000, 9), (0x10000, 0)]),
    'r4': ('axi4', [0, 2, 4, 6, 8, 10, 12, 14], [(0x20000, 15), (0x28000, 1)]),
}
MAP16 = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7]
MAP8 = list(range(8))


def sources(mode):
    """name -> (protocol, data bits, qos)."""
    if mode == 'wide':
        return {'a5': ('axi5', 128, {}), 'a4': ('axi4', 32, {})}
    fixed = {'fixed': ('axi5', 32, {'levels': {'read': 5, 'write': 2}}),
             'wide': ('axi5', 64, {'levels': {'read': 7, 'write': 6}})}
    # The crossing mode has fixed levels alone: no initiator maps AxQOS.
    if mode == 'clocked':
        return fixed
    return fixed | {'mapped': ('axi4', 32, {'axqos_map': MAP16}),
                    'mapped5': ('axi5', 32, {'axqos_map': MAP8})}


def configuration(mode):
    clocked = mode == 'clocked'
    src, dst = ('fast', 'slow') if clocked else ('main', 'main')
    domains = ['fast', 'slow'] if clocked else ['main']
    width = 64 if mode == 'wide' else 32
    cfg = {
        'schema_version': 10, 'name': 'axi5_target', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [], 'targets': [], 'connectivity': 'all',
        'transport': {'data_width': 32},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    for name, (protocol, bits, qos) in sources(mode).items():
        entry = {'name': name, 'clock_domain': src, 'protocol': protocol, 'data_width': bits,
                 'id_width': 2, 'len_width': 8}
        if qos:
            entry['qos'] = qos
        cfg['initiators'].append(entry)
    for name, (protocol, levels, maps) in TARGETS.items():
        cfg['targets'].append({
            'name': name, 'clock_domain': dst, 'protocol': protocol, 'data_width': width,
            'id_width': 4, 'len_width': 8, 'attributes': 'amba', 'region_port': True,
            'axqos_levels': levels,
            'mappings': [{'base': hex(b), 'size': '0x8000', 'local_address': '0x0',
                          'region': r} for b, r in maps]})
    if mode == 'wide':
        cfg['transport']['packet_limits'] = [{'source': 'a5', 'target': t, 'max_bytes': 32}
                                             for t in TARGETS]
    if clocked:
        cfg['topology'] = {
            'request': {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                                  {'name': 'leaf', 'clock_domain': 'slow'}],
                        'links': [{'from': 'root', 'to': 'leaf'}],
                        'entries': [{'endpoint': s, 'node': 'root'} for s in sources(mode)],
                        'exits': [{'endpoint': t, 'node': 'leaf'} for t in TARGETS]},
            'response': {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                                   {'name': 'leaf', 'clock_domain': 'slow'}],
                         'links': [{'from': 'leaf', 'to': 'root'}],
                         'entries': [{'endpoint': t, 'node': 'leaf'} for t in TARGETS],
                         'exits': [{'endpoint': s, 'node': 'root'} for s in sources(mode)]}}
    return cfg


def transactions(mode):
    out = []
    slots = {t: 0 for t in TARGETS}
    for s, (protocol, bits, qos) in sources(mode).items():
        beat = (bits // 8).bit_length() - 1
        for j in range(64):
            target = list(TARGETS)[j % 2]
            _, levels, maps = TARGETS[target]
            base, region = maps[(j // 2) % len(maps)]
            write = (j // 4) % 2 == 0
            length = (j * 3) % 8 if mode != 'wide' else (j * 5) % 16
            # Each transaction owns 256 local bytes of its target, in one of its windows.
            local = slots[target] * 0x100
            slots[target] += 1
            t = dict(index=len(out), source=s, target=target, write=write, addr=base + local,
                     len=length, size=beat, burst=1, id=j % 4, lo=local,
                     hi=local + ((length + 1) << beat))
            q = j % (16 if qos.get('axqos_map') == MAP16 else 8)
            if 'axqos_map' in qos:
                level = qos['axqos_map'][q]
                t['drive'] = {'qos': ((len(qos['axqos_map']) - 1).bit_length(), q)}
            else:
                level = qos.get('levels', {}).get('write' if write else 'read', 0)
                t['drive'] = {}
            t['drive']['prot'] = (3, j % 8)
            t['level'] = level
            t['expect'] = {'qos': (4, levels[level]), 'region': (4, region), 'prot': (3, j % 8)}
            out.append(t)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--mode', choices=('levels', 'clocked', 'wide'), required=True)
    args = ap.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    cfg = configuration(args.mode)
    (work / 'axi5_target.yaml').write_text(json.dumps(cfg, indent=1) + '\n')
    out = work / 'generated'
    subprocess.run([args.cli, 'generate', str(work / 'axi5_target.yaml'), '--out-dir', str(out)],
                   check=True, capture_output=True, text=True, timeout=600)
    mapping = json.loads((out / 'nocloom_axi5_target.map.json').read_text())
    (work / 'network.v').write_text(generated_rtl(out / 'nocloom_axi5_target.v'))
    txs = transactions(args.mode)
    # Every level reaches each target in the modes with levels, and every window its region.
    for name, (_, levels, maps) in TARGETS.items():
        mine = [t for t in txs if t['target'] == name]
        assert {t['expect']['region'][1] for t in mine} == {r for _, r in maps}
        levels = {t['level'] for t in mine}
        assert levels == ({0} if args.mode == 'wide' else
                          {2, 5, 6, 7} if args.mode == 'clocked' else set(range(8))), levels
    (work / 'tb.sv').write_text(
        bench(cfg, mapping['ports'], txs, 7, 'nocloom_axi5_target', 'AXI5_TARGET'))
    code = simulate(work, base / 'compiled')
    log = (work / 'simulation.log').read_text()
    split = re.search(r'split=(\d+)', log)
    passed = (code == 0 and 'CHECK_PASS' in log and 'CHECK_FAIL' not in log and split and
              (args.mode != 'wide' or int(split.group(1)) > 0))
    print(log[-2000:] if not passed else log.splitlines()[0])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
