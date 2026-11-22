"""AXI5 request attributes end to end: every bit of every attribute through width conversion,
packet splitting and a clock domain crossing, checked at each target burst by address."""
import argparse
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from axi5_bench import bench
from sim_engine import generated_rtl

# Initiators: name -> (protocol, data bits, axi5 object). Targets: name -> (protocol, data bits,
# base, axi5 object). Each target's local address equals its global address.
SOURCES = {
    'wide': ('axi5', 128, dict(mpam='partid_12', subsystem_id_width=5, nsaid=True, pbha=True,
                               prot=False, priv_inst=True, pas_width=1, cache=False)),
    'narrow': ('axi5', 32, dict(mpam='partid_9', subsystem_id=6, nsaid=True)),
    'plain': ('axi4', 32, None),
}
TARGETS = {
    't5': ('axi5', 64, 0x00000, dict(mpam='partid_12', subsystem_id_width=8, nsaid=True,
                                     pbha=True, prot=False, priv_inst=True, pas_width=1)),
    't5b': ('axi5', 32, 0x10000, dict(mpam='partid_12', subsystem_id_width=5, pbha=True,
                                      cache=False)),
    't9': ('axi5', 32, 0x20000, dict(mpam='partid_9', subsystem_id_width=3, prot=False,
                                     pas_width=1)),
    't4': ('axi4', 32, 0x30000, None),
    # NSAID without AxPROT and AxPAS, and NSAID behind an AxPROT mapping that makes every
    # request Secure: both targets see no Non-secure request.
    't5s': ('axi5', 32, 0x40000, dict(nsaid=True, prot=False, priv_inst=True)),
    't5r': ('axi5', 32, 0x50000, dict(nsaid=True)),
}
# Target AxPROT mappings other than the AMBA one.
SECURE = {'t5r': ['privileged', 0, 'instruction']}
REACH = {'wide': ['t5', 't5b', 't4', 't5s', 't5r'],
         'narrow': ['t5', 't5b', 't9', 't4', 't5s', 't5r'],
         'plain': ['t5', 't5b', 't9', 't4', 't5s', 't5r']}
# Request attribute fields of the network: the widest source sets each width.
FIELD = {'nsaid': 4, 'pbha': 4, 'subsysid': 5, 'mpam': 14}


def configuration(mode):
    clocked = mode == 'clocked'
    src_domain, dst_domain = ('fast', 'slow') if clocked else ('main', 'main')
    domains = ['fast', 'slow'] if clocked else ['main']
    cfg = {
        'schema_version': 10, 'name': 'axi5_request', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [], 'targets': [],
        'transport': {'data_width': 32,
                      'packet_limits': [{'source': 'wide', 'target': 't5', 'max_bytes': 32}]},
        'connectivity': {s: list(t) for s, t in REACH.items()},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    for name, (protocol, bits, axi5) in SOURCES.items():
        entry = {'name': name, 'clock_domain': src_domain, 'protocol': protocol,
                 'data_width': bits, 'id_width': 2, 'len_width': 8}
        if axi5:
            entry['axi5'] = axi5
        cfg['initiators'].append(entry)
    for name, (protocol, bits, base, axi5) in TARGETS.items():
        entry = {'name': name, 'clock_domain': dst_domain, 'protocol': protocol,
                 'data_width': bits, 'id_width': 4, 'len_width': 8, 'attributes': 'amba',
                 'mappings': [{'base': hex(base), 'size': '0x10000', 'local_address': hex(base)}]}
        if name in SECURE:
            entry['attributes'] = {'preset': 'amba', 'prot': SECURE[name]}
        if axi5:
            entry['axi5'] = axi5
        cfg['targets'].append(entry)
    if clocked:
        cfg['topology'] = {
            'request': {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                                  {'name': 'leaf', 'clock_domain': 'slow'}],
                        'links': [{'from': 'root', 'to': 'leaf'}],
                        'entries': [{'endpoint': s, 'node': 'root'} for s in SOURCES],
                        'exits': [{'endpoint': t, 'node': 'leaf'} for t in TARGETS]},
            'response': {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                                   {'name': 'leaf', 'clock_domain': 'slow'}],
                         'links': [{'from': 'leaf', 'to': 'root'}],
                         'entries': [{'endpoint': t, 'node': 'leaf'} for t in TARGETS],
                         'exits': [{'endpoint': s, 'node': 'root'} for s in SOURCES]}}
    return cfg


def walk(j, offset, width):
    """Walking ones on even steps, walking zeros on odd ones, then mixed values."""
    if j < 4 * width:
        bit = 1 << ((j // 2 + offset) % width)
        return bit if j % 2 == 0 else ((1 << width) - 1) ^ bit
    return random.getrandbits(width)


def mpam_value(raw, bits, to):
    """IHI 0022 Issue L A12.1.2: MPAM_NS bit 0, PARTID above it, PMG on top."""
    if bits == to:
        return raw
    pmg = raw >> (bits - 1)
    low = raw & 0x3ff
    return (pmg << (to - 1)) | low


def transactions(rng_seed):
    random.seed(rng_seed)
    out = []
    slots = {name: 0 for name in TARGETS}
    for s, (protocol, bits, axi5) in SOURCES.items():
        axi5 = axi5 or {}
        beat = bits // 8
        for j in range(64):
            target = REACH[s][j % len(REACH[s])]
            write = (j // len(REACH[s])) % 2 == 0
            base = TARGETS[target][2]
            size = (beat.bit_length() - 1) if j % 3 else max(2, (beat.bit_length() - 2))
            length = (j * 5) % 8 if s != 'wide' else (j * 3) % 16
            burst = 2 if j % 7 == 3 and length in (1, 3, 7, 15) else 1
            span = (length + 1) << size
            index = len(out)
            # Each transaction owns 512 bytes of its target, aligned to its WRAP container.
            start = base + slots[target] * 0x200
            slots[target] += 1
            addr = start + ((j % (length + 1)) << size if burst == 2 else 0)
            t = dict(index=index, source=s, target=target, write=write, addr=addr, len=length,
                     size=size, burst=burst, id=j % 4, lo=start, hi=start + span)
            # Source attribute ports, each field on its own walking bit.
            t['mpam_in'] = walk(j, 0, 14 if axi5.get('mpam') == 'partid_12' else 11)
            t['subsys_in'] = walk(j, 1, axi5.get('subsystem_id_width', 0) or 1)
            t['nsaid_in'] = walk(j, 2, 4)
            t['pbha_in'] = walk(j, 3, 4)
            t['prot_in'] = walk(j, 1, 3)
            t['cache_in'] = random.choice((0, 1, 2, 3, 6, 7, 10, 11, 14, 15))
            # The source's protection and AxCACHE as the network sees them.
            prot = t['prot_in']
            cache = t['cache_in'] if axi5.get('cache', True) else 0
            # Network fields: the source's value or its default.
            field = {}
            field['nsaid'] = t['nsaid_in'] if axi5.get('nsaid') else 0
            field['pbha'] = t['pbha_in'] if axi5.get('pbha') else 0
            if axi5.get('subsystem_id_width'):
                field['subsysid'] = t['subsys_in'] & ((1 << axi5['subsystem_id_width']) - 1)
            else:
                field['subsysid'] = axi5.get('subsystem_id', 0)
            if axi5.get('mpam'):
                w = 14 if axi5['mpam'] == 'partid_12' else 11
                field['mpam'] = mpam_value(t['mpam_in'] & ((1 << w) - 1), w, 14)
            else:
                field['mpam'] = (prot >> 1) & 1
            t['prot'], t['cache'], t['field'] = prot, cache, field
            out.append(t)
    return out


def expected(t):
    """The target port values of a transaction (AMBA attribute mapping)."""
    _, _, _, axi5 = TARGETS[t['target']]
    axi5 = axi5 or {}
    c = t['cache']
    if t['write']:
        cache = (c & 3) | (int(bool(c & 12)) << 2) | (((c >> 3) & 1) << 3)
    else:
        cache = (c & 3) | (((c >> 2) & 1) << 2) | (int(bool(c & 12)) << 3)
    prot = t['prot'] & ~2 if t['target'] in SECURE else t['prot']
    out = dict(prot=prot, cache=cache)
    f = t['field']
    # NSAID is zero unless the target sees a Non-secure request on AxPROT[1] or AxPAS (IHI 0022
    # Issue L, A10.1.1).
    nonsecure = (axi5.get('prot', True) or axi5.get('pas_width')) and prot >> 1 & 1
    out['nsaid'] = (f['nsaid'] if nonsecure else 0) if axi5.get('nsaid') else None
    out['pbha'] = f['pbha'] if axi5.get('pbha') else None
    width = axi5.get('subsystem_id_width', 0)
    out['subsysid'] = f['subsysid'] & ((1 << width) - 1) if width else None
    if axi5.get('mpam'):
        out['mpam'] = mpam_value(f['mpam'], 14, 14 if axi5['mpam'] == 'partid_12' else 11)
    else:
        out['mpam'] = None
    return out


def ports_of(t):
    """The address channel inputs a transaction drives and the values its targets expect."""
    _, _, axi5 = SOURCES[t['source']]
    axi5 = axi5 or {}
    drive = {}
    if axi5.get('prot', True):
        drive['prot'] = (3, t['prot_in'])
    else:
        if axi5.get('priv_inst'):
            drive.update(priv=(1, t['prot_in'] & 1), inst=(1, t['prot_in'] >> 2 & 1))
        if axi5.get('pas_width'):
            drive['pas'] = (1, t['prot_in'] >> 1 & 1)
    if axi5.get('cache', True):
        drive['cache'] = (4, t['cache_in'])
    if axi5.get('mpam'):
        w = 14 if axi5['mpam'] == 'partid_12' else 11
        drive['mpam'] = (w, t['mpam_in'] & ((1 << w) - 1))
    if axi5.get('subsystem_id_width'):
        w = axi5['subsystem_id_width']
        drive['subsysid'] = (w, t['subsys_in'] & ((1 << w) - 1))
    for key in ('nsaid', 'pbha'):
        if axi5.get(key):
            drive[key] = (4, t[key + '_in'])
    e = expected(t)
    expect = dict(prot=(3, e['prot']), priv=(1, e['prot'] & 1), inst=(1, e['prot'] >> 2 & 1),
                  pas=(1, e['prot'] >> 1 & 1), cache=(4, e['cache']))
    for key, bits in (('nsaid', 4), ('pbha', 4), ('subsysid', 8), ('mpam', 14)):
        if e[key] is not None:
            expect[key] = (bits, e[key])
    return drive, expect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--mode', choices=('switch', 'clocked'), required=True)
    ap.add_argument('--seed', type=int, default=7)
    args = ap.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    cfg = configuration(args.mode)
    (work / 'axi5_request.yaml').write_text(json.dumps(cfg, indent=1) + '\n')
    out = work / 'generated'
    subprocess.run([args.cli, 'generate', str(work / 'axi5_request.yaml'), '--out-dir', str(out)],
                   check=True, capture_output=True, text=True, timeout=600)
    mapping = json.loads((out / 'nocloom_axi5_request.map.json').read_text())
    (work / 'network.v').write_text(generated_rtl(out / 'nocloom_axi5_request.v'))
    txs = transactions(args.seed)
    for x in txs:
        x['drive'], x['expect'] = ports_of(x)
    (work / 'tb.sv').write_text(
        bench(cfg, mapping['ports'], txs, args.seed, 'nocloom_axi5_request', 'AXI5_REQUEST'))
    code = simulate(work, base / 'compiled')
    log = (work / 'simulation.log').read_text()
    split = re.search(r'split=(\d+)', log)
    # Packet splitting cuts some transactions into several target bursts.
    passed = (code == 0 and 'CHECK_PASS' in log and 'CHECK_FAIL' not in log and split and
              int(split.group(1)) > 0)
    print(log[-2000:] if not passed else log.splitlines()[0])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
