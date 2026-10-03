#!/usr/bin/env python3
"""Round-trip one width adapter hop and three isolated fault controls."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from cross_hardware_sim import simulate
from width_adapter_bench import hop_shape, totals, write_bench


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', required=True)
    p.add_argument('--work', required=True)
    p.add_argument('--wide', type=int, required=True)
    p.add_argument('--narrow', type=int, required=True)
    p.add_argument('--prepare', action='store_true')
    args = p.parse_args()
    shape = hop_shape(args.wide, args.narrow)
    wide_total, narrow_total = totals(shape)
    pass_line = f'WIDTH_ADAPTER_PASS deliveries={wide_total} narrow={narrow_total}'
    root = Path(args.work).resolve()
    rows = []
    for fault, assertion in [('', None), ('premature_last', 'ADAPTER.PACKET_BOUNDARY'),
                             ('lane_shift', 'ADAPTER.BYTE_CONSERVATION'),
                             ('drop_err', 'ADAPTER.ERR_MERGE'), ('restored', None)]:
        work = root / (fault or 'baseline')
        work.mkdir(parents=True, exist_ok=True)
        command = [str(Path(args.fixture).resolve()), str(work), str(args.wide), str(args.narrow)]
        if fault and fault != 'restored':
            command.append(fault)
        subprocess.run(command, check=True, timeout=60)
        (work / 'network.v').write_bytes((work / 'adapter.v').read_bytes())
        write_bench(work, args.wide, args.narrow)
        (work / 'scenario.json').write_text(json.dumps(
            {'fault': fault, 'expected_fail': assertion,
             'hop': f'{args.wide}x{args.narrow}'}) + '\n')
        if args.prepare:
            continue
        if fault == 'restored':
            rows.append({'case': 'restored', 'status': 'PASS', 'simulation_exit': None,
                         'expected_fail': None, 'proven_by': 'byte_identity'})
            continue
        rc = simulate(work, root / 'compiled')
        log = (work / 'simulation.log').read_text()
        if assertion:
            if f'ASSERT_FAIL:{assertion}' not in log.splitlines() or 'WIDTH_ADAPTER_PASS' in log:
                raise RuntimeError(f'Designated checker was not triggered: {work}')
        elif rc or pass_line not in log or 'ASSERT_FAIL:' in log:
            raise RuntimeError(f'Baseline did not complete: {work}')
        rows.append({'case': fault or 'baseline', 'status': 'CAUGHT' if assertion else 'PASS',
                     'simulation_exit': rc, 'expected_fail': assertion})
    for name in ('network.v', 'tb.sv'):
        before = (root / 'baseline' / name).read_bytes()
        if before != (root / 'restored' / name).read_bytes():
            raise RuntimeError('Recovery did not restore the baseline DUT')
    if not args.prepare:
        (root / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')


if __name__ == '__main__':
    main()
