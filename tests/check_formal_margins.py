#!/usr/bin/env python3
"""Print formal CTest and SBY wall times against their existing limits.

Run after CTest with --output-junit FILE. SBY logs have one-second resolution.
An optional invocation timing JSONL supplies precise wall times, including Lean.
A timing file selects its runs. Otherwise latest pointers or the newest run are used.
Shared driver limits and cumulative budgets of split covers are shown separately.
Timing JSONL fields: cwd, argv, seconds, returncode. The caller records wall time
around each sby, lean or lake invocation and appends one JSON object per line.
SBY FAIL can be an expected negative control; this report does not judge acceptance.
"""
import argparse
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path


def driver_limit(name, task, command):
    defaults = {'connectivity_formal': 3500, 'formal2_adapter_axioms': 150,
                'retirement_formal': 290}
    if name == 'async_fifo_formal':
        return (float(command[command.index('--timeout') + 1])
                if '--timeout' in command else 1800) + 120
    elif name.startswith('liveness_formal_'):
        option, default = '--limit', 1200
    elif name.startswith('target_timeout_formal_'):
        return 2400
    elif name == 'arbiter_library_formal':
        return 5400 if 'fifo-8' in task and not re.search(r'[/ ]bmc(?:[/. ]|$)', task) else 1800
    else:
        return defaults.get(name)
    return float(command[command.index(option) + 1]) if option in command else default


def current_runs(work, invocations):
    roots = [p for p in work.iterdir() if p.is_dir() and
             p.name.startswith(('run-', 'selftest-', 'retirement-'))] if work.is_dir() else []
    if not roots:
        return [work]
    recorded = [p for p in roots if any(Path(c['cwd']).is_relative_to(p) for c in invocations)]
    if recorded:
        return recorded
    pointers = {p.read_text().strip() for p in work.glob('latest*.txt')}
    selected = [p for p in roots if str(p) in pointers]
    return selected or [max(roots, key=lambda p: p.stat().st_mtime_ns)]


def collect(build, junit, timings, plan_file=None):
    plan = json.loads(plan_file.read_text() if plan_file else subprocess.check_output(
        ['ctest', '--test-dir', str(build), '--show-only=json-v1'], text=True))
    measured = {t.attrib['name']: t for t in ET.parse(junit).iter('testcase')}
    invocations = [json.loads(line) for line in timings.read_text().splitlines()] if timings else []
    rows = []
    for test in plan['tests']:
        props = {p['name']: p['value'] for p in test['properties']}
        if 'formal' not in props.get('LABELS', []):
            continue
        name, command = test['name'], test['command']
        result = measured.get(name)
        state = 'NOT_RUN'
        if result is not None:
            state = ('NOT_RUN' if result.find('skipped') is not None else
                     'FAIL' if result.find('failure') is not None or result.find('error') is not None
                     else 'PASS')
        rows.append(dict(kind='ctest', test=name, task=name, status=state,
                         seconds=float(result.attrib['time']) if result is not None and state != 'NOT_RUN' else None,
                         limit=props.get('TIMEOUT'), scope='ctest'))
        if state == 'NOT_RUN' or '--work' not in command:
            continue
        work = Path(command[command.index('--work') + 1])
        if not work.is_absolute():
            work = Path(props.get('WORKING_DIRECTORY', build)) / work
        current = current_runs(work, invocations)
        matched_calls = set()
        for log in sorted(p for root in current for p in root.rglob('logfile.txt')):
            config = log.parent / 'config.sby'
            if not config.is_file():
                continue
            text = log.read_text(errors='replace')
            clock = re.findall(r'Elapsed clock time \[H:MM:SS \(secs\)\]:\s*(\d+):(\d+):(\d+)', text)
            seconds = sum(int(x) * scale for x, scale in zip(clock[-1], (3600, 60, 1))) if clock else None
            for index, call in enumerate(invocations):
                configs = [arg for arg in call['argv'] if arg.endswith('.sby')]
                if len(configs) == 1 and log.parent == Path(call['cwd']) / Path(configs[0]).stem:
                    seconds = call['seconds']
                    matched_calls.add(index)
            status = log.parent / 'status'
            state = status.read_text().split()[0] if status.exists() else 'INCOMPLETE'
            options = config.read_text()
            limits = re.findall(r'^\s*timeout\s+(\d+)\s*$', options, re.M)
            task = log.parent.relative_to(work).as_posix()
            rows.append(dict(kind='sby', test=name, task=task, status=state,
                             seconds=seconds, limit=int(limits[-1]) if limits else driver_limit(name, task, command),
                             scope='sby' if limits else 'shared driver invocation'))
        for root in current:
            budget_file = root / 'budgets.json'
            if budget_file.exists():
                for budget in json.loads(budget_file.read_text()):
                    rows.append(dict(kind='group', test=name,
                                     task=(root.relative_to(work) / budget['task']).as_posix(),
                                     status=budget['status'], seconds=budget['seconds'],
                                     limit=budget['limit'], scope='sum of split proof wall times'))
        for index, call in enumerate(invocations):
            if index in matched_calls:
                continue
            cwd = Path(call['cwd'])
            if not any(cwd.is_relative_to(root) for root in current):
                continue
            task = (cwd.relative_to(work).as_posix() + ' ' + ' '.join(call['argv'])).strip()
            rows.append(dict(kind='invocation', test=name, task=task,
                             status=f"exit={call['returncode']}", seconds=call['seconds'],
                             limit=driver_limit(name, task, command) or props.get('TIMEOUT'),
                             scope='driver invocation' if name != 'formal_lean' else 'shared ctest'))
    for row in rows:
        seconds, limit = row['seconds'], row['limit']
        row['margin_seconds'] = limit - seconds if seconds is not None and limit else None
        row['used_fraction'] = seconds / limit if seconds is not None and limit else None
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('build', type=Path)
    parser.add_argument('--junit', type=Path, required=True)
    parser.add_argument('--timings', type=Path)
    parser.add_argument('--plan', type=Path, help='saved CTest JSON plan instead of querying CTest')
    parser.add_argument('--json', type=Path)
    parser.add_argument('--over-half', action='store_true', help='show rows over half their limit, and unknown margins')
    args = parser.parse_args()
    rows = collect(args.build.resolve(), args.junit, args.timings, args.plan)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + '\n')
    print('kind\ttest\ttask\tstatus\tseconds\tlimit_s\tmargin_s\tused_pct\tlimit_scope')
    for row in rows:
        used = row['used_fraction']
        if args.over_half and used is not None and used <= 0.5:
            continue
        numbers = [row['seconds'], row['limit'], row['margin_seconds'], used * 100 if used is not None else None]
        print('\t'.join([row['kind'], row['test'], row['task'], row['status'],
                         *('unknown' if x is None else f'{x:.2f}' for x in numbers), row['scope']]))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
