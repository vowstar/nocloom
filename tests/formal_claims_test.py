#!/usr/bin/env python3
"""Check that the claims in formal/lean/README.md match the tests and scripts they name.

Every test name in the claim tables must be a registered CTest test (a trailing * matches a
prefix). The shape count, default depth, handshake bound and hang shape count must equal those of
tests/run_liveness_formal.py, and the edge limit that of tools/gen_instance.py. The table of Fair
clauses must list exactly the fields of Fair, and the F labels must be the liveness test's. Every
dotted Lean name and every structure named as open must exist in the Lean sources. A
theorem whose conclusion follows from its progress premises alone is not a checked claim.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

NUMBERS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
           'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}


def section(text, title):
    match = re.search(r'^## %s\n(.*?)(?=^## |\Z)' % re.escape(title), text, re.M | re.S)
    if not match:
        raise AssertionError('README has no section %r' % title)
    return match.group(1)


def table(text):
    """Body rows of every table in text: header and separator rows left out."""
    lines = text.splitlines()
    rule = re.compile(r'\|\s*-')
    return [[cell.strip() for cell in line.strip('|').split('|')]
            for i, line in enumerate(lines)
            if line.startswith('|') and not rule.match(line)
            and not (i + 1 < len(lines) and rule.match(lines[i + 1]))]


def number(word):
    return int(word) if word.isdigit() else NUMBERS[word.lower()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--build', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    readme = (source / 'formal/lean/README.md').read_text(encoding='utf-8')
    sys.path.insert(0, str(source / 'tests'))
    sys.path.insert(0, str(source / 'formal/lean/tools'))
    import gen_instance
    import run_liveness_formal as live
    listed = subprocess.run(['ctest', '--test-dir', str(args.build), '--show-only=json-v1'],
                            capture_output=True, text=True, check=True)
    tests = {t['name'] for t in json.loads(listed.stdout)['tests']}
    failures = []
    lean = '\n'.join(p.read_text(encoding='utf-8')
                     for d in ('GenericNoC', 'NetlistCheck')
                     for p in sorted((source / 'formal/lean' / d).rglob('*.lean')))
    checked = section(readme, 'What is checked')
    claims = table(checked) + table(section(readme, 'What has no proof'))
    if len(table(checked)) < 5:
        failures.append('the table of what is checked has fewer than five rows')
    names = set()
    for row in claims + table(section(readme, 'Fair and generated Verilog')):
        for cell in row:
            for test in re.findall(r'(?i)tests? ((?:`[\w*]+`(?:,| and)? ?)+)', cell):
                names |= set(re.findall(r'`([\w*]+)`', test))
    if not names:
        failures.append('the claim tables name no test')
    for name in sorted(names):
        found = (any(t.startswith(name[:-1]) for t in tests) if name.endswith('*')
                 else name in tests)
        if not found:
            failures.append('test %s is not registered' % name)
    # Lean names: dotted names in the claims, and capitalized names in the status column.
    lean_names = {n for row in claims for cell in row for n in re.findall(r'`([A-Z]\w*(?:\.\w+)+)`', cell)
                  if not n.endswith('.lean') and not n.endswith('.json')}
    lean_names |= {n for row in table(section(readme, 'What has no proof'))
                   for n in re.findall(r'`([A-Z]\w+)`', row[1])}
    for name in sorted(lean_names):
        short = name.split('.')[-1]
        if not re.search(r'\b(theorem|def|structure|namespace) %s\b' % re.escape(short), lean):
            failures.append('Lean name %s is not in the sources' % name)
    shapes = re.search(r'(\w+) AXI4 network shapes', checked)
    if not shapes or number(shapes.group(1)) != len(live.SHAPES):
        failures.append('shape count differs: README %s, script %d'
                        % (shapes and shapes.group(1), len(live.SHAPES)))
    depth = re.search(r'depth (\d+) by default', checked)
    script = (source / 'tests/run_liveness_formal.py').read_text(encoding='utf-8')
    default = re.search(r"'--depth', type=int, default=(\d+)", script)
    if not depth or not default or depth.group(1) != default.group(1):
        failures.append('default depth differs')
    cycles = re.search(r'within (\d+) cycles', checked)
    bound = re.search(r"'--bound', type=int, default=(\d+)", script)
    if not cycles or not bound or cycles.group(1) != bound.group(1):
        failures.append('handshake bound differs')
    hangs = re.search(r'One fault on (\w+) shapes', checked)
    hang_shapes = {case[1] for case in live.CASES if case[2] is True}
    if not hangs or number(hangs.group(1)) != len(hang_shapes):
        failures.append('hang shape count differs: script %d' % len(hang_shapes))
    limit = re.search(r'above (\d+)\s+distinct\s+edges', readme[:readme.index('## Build and check')])
    if not limit or int(limit.group(1)) != gen_instance.KERNEL_LIMIT:
        failures.append('edge limit differs from gen_instance.KERNEL_LIMIT')
    # The clauses of Fair and the liveness assumptions they map to.
    fair = re.search(r'structure Fair .*?\n\n', lean, re.S).group(0)
    fields = set(re.findall(r'^  (\w+) :', fair, re.M))
    rows = [row for row in table(section(readme, 'Fair and generated Verilog'))
            if row[0].startswith('`')]
    labels = {row[0] for row in table(section(readme, 'Fair and generated Verilog'))
              if re.fullmatch(r'F\d', row[0])}
    if labels != {'F1', 'F2', 'F3', 'F4'}:
        failures.append('the F label table lists %s' % sorted(labels))
    clauses = {re.match(r'`(\w+)`', row[0]).group(1) for row in rows}
    if fields - clauses or clauses - fields - {'room'}:
        failures.append('Fair clauses differ: README %s, Lean %s' % (sorted(clauses), sorted(fields)))
    assumed = set(re.findall(r'\bF\d\b', ' '.join(row[2] for row in rows)))
    in_script = set(re.findall(r'\bF\d\b', live.__doc__))
    if not assumed or assumed - in_script or {'F1', 'F2', 'F3', 'F4'} - assumed:
        failures.append('F labels differ: README %s, script %s' % (sorted(assumed), sorted(in_script)))
    # The port completion theorems: their conclusion is the chain of their progress premises.
    closure = re.search(r'theorem port_completes_of_progress\b(.*?):=', lean, re.S)
    if not closure or re.search(r'\b(Config|wfCheck|checkRank|Covered|EdgeTable)\b', closure.group(1)):
        failures.append('port_completes_of_progress is missing or takes model premises')
    for name in ('checked_port_completes', 'checked_request_completes', 'port_completes_of_progress'):
        if name in checked:
            failures.append('%s is listed as checked, but follows from its progress premises' % name)
    no_proof = section(readme, 'What has no proof')
    if not all(name in no_proof for name in ('port_completes_of_progress',
                                             'answer_alone_does_not_complete')):
        failures.append('What has no proof does not name the port completion premises')
    for failure in failures:
        print('FAIL', failure)
    if failures:
        sys.exit(1)
    print('PASS %d test names, shapes, depth, hang shapes, edge limit and %d Fair clauses'
          % (len(names), len(fields)))


if __name__ == '__main__':
    main()
