#!/usr/bin/env python3
"""Check the Lean certificate of every example and every liveness shape.

Writes only to a new directory under --work.

Each network is generated with the CLI, converted by formal/lean/tools/gen_instance.py and checked
by Lean. A network with a model must end with the theorems its completion scope allows: completes
and delivered, or completion_withdrawn. Only a file that uses native_decide may depend on the
compiler axioms. A failing network is reported and the others still run.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(command, cwd, timeout):
    return subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--only', default='')
    args = parser.parse_args()
    source = args.source.resolve()
    sys.path.insert(0, str(source / 'formal/lean/tools'))
    sys.path.insert(0, str(source / 'tests'))
    import run_checks as checks
    import run_liveness_formal as live
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=args.work.resolve()))
    for tool in ('lean', 'lake'):
        if shutil.which(tool) is None:
            sys.exit(f'FAIL {tool} is not on PATH')
    package = checks.copy_sources(work / 'package')
    pinned = (package / 'lean-toolchain').read_text().strip().rsplit(':v', 1)[-1]
    version = run(['lean', '--version'], package, 60)
    if version.returncode or f'version {pinned},' not in version.stdout:
        sys.exit(f'FAIL Lean {pinned} is required: {version.stdout}')
    built = run(['lake', 'build'], package, 1800)
    if built.returncode:
        sys.exit(f'FAIL lake build: {built.stdout[-2000:]}')
    inputs = {p.stem: p for p in sorted((source / 'examples').glob('*.yaml'))}
    shapes = work / 'shapes'
    shapes.mkdir()
    for name, shape in sorted(live.SHAPES.items()):
        path = shapes / (name + '.yaml')
        path.write_text(json.dumps(shape['config']), encoding='utf-8')
        inputs['shape_' + name] = path
    failures = []
    for name, path in inputs.items():
        if args.only and not re.search(args.only, name):
            continue
        begin = time.monotonic()
        gen = work / name
        made = run([str(args.cli.resolve()), 'generate', str(path), '--out-dir', str(gen)], work, 600)
        artifact = next(gen.glob('*.dependency.json'), None) if made.returncode == 0 else None
        if artifact is None:
            failures.append(f'{name}: generation failed: {made.stdout[-500:]}')
            continue
        lean = work / (name + '.lean')
        instance = 'E_' + re.sub(r'\W', '_', name)
        converted = run([sys.executable, str(package / 'tools/gen_instance.py'), str(artifact),
                         instance, str(lean)], package, 600)
        if converted.returncode:
            failures.append(f'{name}: conversion failed: {converted.stdout}')
            continue
        result = run(['lake', 'env', 'lean', str(lean)], package, 3600)
        (work / (name + '.log')).write_text(result.stdout, encoding='utf-8')
        theorems, absent = checks.expected_theorems(lean)
        listed = checks.listed(result.stdout)
        wanted = {f'Generated.{instance}.{t}' for t in theorems}
        unwanted = {f'Generated.{instance}.{t}' for t in absent}
        native = 'native_decide' in lean.read_text(encoding='utf-8')
        model = json.loads(artifact.read_text(encoding='utf-8'))['model'] is not None
        bad = checks.check_axioms(name, result.stdout, checks.NATIVE if native else ())
        good = (result.returncode == 0 and 'sorry' not in result.stdout and not wanted - listed
                and not unwanted & listed and not bad and model == ('structural_ok' in theorems))
        scope = ('completes' if 'completes' in theorems
                 else 'withdrawn' if 'completion_withdrawn' in theorems else 'certificate only')
        print(f'{"PASS" if good else "FAIL"} {name}: {scope}, '
              f'{"native_decide" if native else "kernel"}, {time.monotonic() - begin:.0f} s',
              flush=True)
        if not good:
            failures.append(f'{name}: Lean check failed: {result.stdout[-1500:]} {bad}')
    if failures:
        print('\n'.join('FAIL ' + f for f in failures))
        sys.exit(1)
    print('PASS every example and shape')


if __name__ == '__main__':
    main()
