#!/usr/bin/env python3
"""Tie the two hang controls together. Writes only to a new directory under --work.

tests/run_liveness_formal.py injects one fault into the RTL of its hang cases: response grants end
a segment only at the last word. In the Lean model this is the `holding` release policy, which no
graph can certify (Wormhole.TwoTargets.holding_unprovable). Here the same fault is applied to the
dependency graph of each hang case's shape: the graph must report a cycle and the Lean importer
must refuse it, while the shape without the fault has no cycle and converts.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source / 'tests'))
    import run_liveness_formal as live
    holding = (source / 'formal/lean/GenericNoC/Instances/TwoTargets.lean').read_text()
    if 'theorem holding_unprovable' not in holding:
        sys.exit('FAIL the Lean side of the control, holding_unprovable, is missing')
    shapes = sorted({case[1] for case in live.CASES if case[2] is True})
    if not shapes:
        sys.exit('FAIL the liveness test has no hang case')
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=args.work.resolve()))
    failures = []
    for name in shapes:
        config = work / (name + '.yaml')
        config.write_text(json.dumps(live.SHAPES[name]['config']), encoding='utf-8')
        out = work / name
        made = subprocess.run([str(args.fixture.resolve()), str(config), str(out), 'held'],
                              capture_output=True, text=True)
        if made.returncode:
            failures.append(f'{name}: fixture failed: {made.stderr}')
            continue
        status = {}
        for graph in ('ordinary', 'held'):
            data = json.loads((out / (graph + '.json')).read_text())
            status[graph] = data['result']['status']
            converted = subprocess.run(
                [sys.executable, str(source / 'formal/lean/tools/gen_instance.py'),
                 str(out / (graph + '.json')), 'H', str(out / (graph + '.lean')), '--graph-only'],
                capture_output=True, text=True)
            status[graph + '_import'] = converted.returncode
        good = status == {'ordinary': 'no_cycle', 'ordinary_import': 0,
                          'held': 'cycle', 'held_import': 3}
        print(f'{"PASS" if good else "FAIL"} {name}: {status}')
        if not good:
            failures.append(f'{name}: {status}')
    if failures:
        sys.exit('FAIL ' + '; '.join(failures))
    print(f'PASS the RTL hang fault is a graph cycle on {len(shapes)} shapes')


if __name__ == '__main__':
    main()
