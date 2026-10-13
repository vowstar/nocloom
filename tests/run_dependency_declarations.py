#!/usr/bin/env python3
"""Every example's dependency artifact against its map, with formal/lean/tools/check_declarations.py.

The labels Lean takes as given (cross, contig, bound) come from the plan, and the map shares that
plan, so this catches a label that contradicts the ports or the outstanding limits, not one the
plan itself has wrong: tests/unit/dependency_graph.cpp reads those from the lowered hardware.

Also: each model name lists at its number. Control: a forged bound source with ID bits must be
rejected.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from scheduler_graph_contract import check as scheduler_waits


def check(tool, graph, mapping):
    return subprocess.run([sys.executable, tool, graph, mapping], capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', type=Path, required=True)
    ap.add_argument('--work', type=Path, required=True)
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    tool = root / 'formal/lean/tools/check_declarations.py'
    args.work.mkdir(parents=True, exist_ok=True)
    base = args.work.resolve()
    args.work = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    (base / "latest.txt").write_text(str(args.work) + "\n")
    fails, seen, forged = [], 0, False
    scheduler_control = False
    for yaml in sorted((root / 'examples').glob('*.yaml')):
        out = args.work / yaml.stem
        if not out.exists():
            subprocess.run([args.cli.resolve(), 'generate', yaml, '--out-dir', out], check=True,
                           stdout=subprocess.DEVNULL)
        graph_path = next(out.glob('*.dependency.json'))
        map_path = next(out.glob('*.map.json'))
        graph = json.loads(graph_path.read_text())
        mapping = json.loads(map_path.read_text())
        scheduler_errors, scheduler_edges = scheduler_waits(graph, mapping)
        fails.extend(f'{yaml.stem}: {error}' for error in scheduler_errors)
        if scheduler_edges and not scheduler_control:
            import copy
            removed = copy.deepcopy(graph)
            edge = next(e for e in removed['edges'] if tuple(e[:3]) in scheduler_edges)
            removed['edges'].remove(edge)
            if not scheduler_waits(removed, mapping)[0]:
                fails.append('UPGRADE_WAIT_GRAPH_MISSING control passed')
            added = copy.deepcopy(graph)
            control = next(i for i, n in enumerate(graph['nodes']) if n['kind'] == 'scheduler.control')
            pool = next(i for i, n in enumerate(graph['nodes']) if n['kind'] == 'target.pool')
            added['edges'].append([pool, control, 'M6', 0])
            if not scheduler_waits(added, mapping)[0]:
                fails.append('UPGRADE_WAIT_GRAPH_UNEXPECTED control passed')
            scheduler_control = True
        model = graph['model']
        if model is None:
            fails.append(f'{yaml.stem}: no model: {graph.get("model_omitted")}')
            continue
        seen += 1
        r = check(tool, graph_path, map_path)
        if r.returncode:
            fails.append(f'{yaml.stem}: {r.stdout.strip()}')
        # Numbers in the tables name these lists: every endpoint slot must be a source or target.
        slots = model['slots']
        for at in model['endpoints']:
            if not slots[at].startswith(('source/', 'target/')):
                fails.append(f'{yaml.stem}: endpoint slot {at} is named {slots[at]}')
        for at, dest, out_ in model['routes']:
            name = model['outputs'][out_]
            if not (name.startswith('req/') or name.startswith('rsp/')):
                fails.append(f'{yaml.stem}: output {out_} is named {name}')
        sources = [d for d, n in enumerate(model['destinations']) if n.startswith('source/')]
        ports = json.loads(map_path.read_text())['ports']
        if not forged and sources and any(p.get('signal') == 'arid' for p in ports):
            model['bound'] = sources[:1]
            bad = args.work / 'forged.json'
            bad.write_text(json.dumps(graph))
            if check(tool, bad, map_path).returncode != 1:
                fails.append('a forged bound source passed')
            forged = True
    if not scheduler_control:
        fails.append('no scheduler wait control was reached')
    if not forged:
        fails.append('no example could carry the forged control')
    for fail in fails:
        print('DECLARATIONS FAIL', fail)
    print('DECLARATIONS', 'FAIL' if fails else 'PASS', seen)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
