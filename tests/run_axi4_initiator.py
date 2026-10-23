#!/usr/bin/env python3
"""AXI4 ingress endpoint bench: baseline, designated faults, restored identity."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from cross_hardware_sim import simulate
from axi4_initiator_bench import write_bench

# fault -> designated checker that must fire
FAULTS = [
    ('premature_last', 'AXI4I.PACKET_BOUNDARY'),
    ('count_load_one', 'AXI4I.PACKET_BOUNDARY'),
    ('address_hold', 'AXI4I.BEAT_ADDRESS'),
    ('wrap_turn_stuck', 'AXI4I.BEAT_ADDRESS'),
    ('wlast_blind', 'AXI4I.WRITE_COMPLETION'),
    ('span_blow', 'AXI4I.WRITE_COMPLETION'),
    ('rlast_drop', 'AXI4I.RLAST_POSITION'),
    ('early_w', 'AXI4I.W_AHEAD_OF_AW'),
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', required=True)
    p.add_argument('--work', required=True)
    p.add_argument('--data', type=int, default=32)
    p.add_argument('--len-bits', type=int, default=8)
    p.add_argument('--id-bits', type=int, default=4)
    p.add_argument('--prepare', action='store_true')
    args = p.parse_args()
    root = Path(args.work).resolve()
    rows = []
    wide_faults = [('write_high_lost', 'AXI4I.BYTE_CONSERVATION'),
                   ('read_high_lost', 'AXI4I.READ_DATA'),
                   ('strobe_high_lost', 'AXI4I.WSTRB_FIDELITY')] if args.data == 128 else []
    for fault, assertion in [('', None)] + FAULTS + wide_faults + [('restored', None)]:
        work = root / (fault or 'baseline')
        work.mkdir(parents=True, exist_ok=True)
        command = [str(Path(args.fixture).resolve()), str(work), 'probe', str(args.data),
                   str(args.len_bits), str(args.id_bits)]
        if fault and fault != 'restored':
            command.append(fault)
        subprocess.run(command, check=True, timeout=120)
        write_bench(work, args.data, args.len_bits, args.id_bits)
        (work / 'scenario.json').write_text(json.dumps(
            {'fault': fault, 'expected_fail': assertion,
             'profile': f'{args.data}b_len{args.len_bits}_id{args.id_bits}'}) + '\n')
        if args.prepare:
            continue
        if fault == 'restored' and args.data != 128:
            rows.append({'case': 'restored', 'status': 'PASS', 'simulation_exit': None,
                         'expected_fail': None, 'proven_by': 'byte_identity'})
            continue
        rc = simulate(work, root / 'compiled')
        log = (work / 'simulation.log').read_text()
        if assertion:
            if f'ASSERT_FAIL:{assertion}' not in log.splitlines() or 'AXI4_INITIATOR_PASS' in log:
                raise RuntimeError(f'Designated checker was not triggered: {work} {assertion}')
        elif rc or 'AXI4_INITIATOR_PASS' not in log or 'ASSERT_FAIL:' in log:
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
