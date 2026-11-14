#!/usr/bin/env python3
"""Check generated dependency imports and Lean rank certificates. Deletes nothing."""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def raw_acyclic(graph):
    """Check ordinary directed edges, independently of alternative-group semantics."""
    degree = [0] * len(graph['nodes'])
    leaving = [[] for _ in degree]
    for u, v, _, _ in graph['edges']:
        degree[v] += 1
        leaving[u].append(v)
    ready = [v for v, count in enumerate(degree) if count == 0]
    for u in ready:
        for v in leaving[u]:
            degree[v] -= 1
            if degree[v] == 0:
                ready.append(v)
    return len(ready) == len(degree)


def run(command, cwd, log):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=600)
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    return result


def convert(source, work, name, graph, options=(), existing=False):
    path = work / (name + '.json')
    path.write_text(json.dumps(graph), encoding='utf-8')
    out = work / (name + '.lean')
    if existing:
        out.write_bytes(b'preserve existing proof\n')
    result = run([sys.executable, str(source / 'formal/lean/tools/gen_instance.py'),
                  str(path), name, str(out), *options], work, work / (name + '.log'))
    return result, out


def rejects(source, work, name, graph, diagnostic, options=(), existing=False):
    result, out = convert(source, work, name, graph, options, existing)
    require(result.returncode == 3 and diagnostic in result.stderr,
            f'{name}: expected exit 3 with {diagnostic}, got {result.returncode}: {result.stderr}')
    require(not result.stdout, f'{name}: rejection wrote to stdout')
    require(out.read_bytes() == b'preserve existing proof\n' if existing else not out.exists(),
            f'{name}: rejection created or changed proof source')
    print(f'PASS {name}: rejected before writing proof source')


def check_imports(source, work, graphs):
    for name in ('grouped_dag', 'cycle_escape'):
        graph = graphs[name]
        for suffix, options, existing in (('Default', (), False),
                                          ('GraphOnly', ('--graph-only',), False),
                                          ('Kernel', ('--kernel',), False),
                                          ('Existing', (), True)):
            rejects(source, work, name.replace('_', '') + suffix, graph,
                    'NCL_CERTIFICATE_ALTERNATIVE_GROUP', options, existing)
    ordinary = graphs['ordinary']
    result, out = convert(source, work, 'Ordinary', ordinary)
    require(result.returncode == 0 and out.exists(), f'ordinary conversion: {result.stderr}')
    first = out.read_bytes()
    result, out = convert(source, work, 'Ordinary', ordinary)
    require(result.returncode == 0 and out.read_bytes() == first, 'ordinary import changed bytes')
    rejects(source, work, 'CycleStatus', graphs['cycle'], "status is 'cycle'")
    # A duplicate pair must still be checked before pair de-duplication.
    duplicate = copy.deepcopy(ordinary)
    duplicate['edges'].append([*ordinary['edges'][0][:3], 7])
    rejects(source, work, 'DuplicateGroup', duplicate, 'NCL_CERTIFICATE_ALTERNATIVE_GROUP')
    for name, edge in (('ShortEdge', ordinary['edges'][0][:3]),
                       ('LongEdge', [*ordinary['edges'][0], 0]),
                       ('StringGroup', [*ordinary['edges'][0][:3], '0']),
                       ('BooleanGroup', [*ordinary['edges'][0][:3], False]),
                       ('ZeroFloatGroup', [*ordinary['edges'][0][:3], 0.0]),
                       ('OneFloatGroup', [*ordinary['edges'][0][:3], 1.0]),
                       ('FractionalGroup', [*ordinary['edges'][0][:3], 0.5]),
                       ('NegativeGroup', [*ordinary['edges'][0][:3], -1])):
        graph = copy.deepcopy(ordinary)
        graph['edges'][-1] = edge
        for existing in (False, True):
            rejects(source, work, name + ('Existing' if existing else ''), graph,
                    'NCL_CERTIFICATE_EDGE_FORMAT', existing=existing)
    for name, version in (('TrueVersion', True), ('FalseVersion', False),
                          ('FloatVersion', 1.0), ('FractionalVersion', 1.5),
                          ('ZeroVersion', 0), ('OtherVersion', 2),
                          ('StringVersion', '1'), ('NullVersion', None)):
        graph = copy.deepcopy(ordinary)
        graph['format_version'] = version
        for existing in (False, True):
            rejects(source, work, name + ('Existing' if existing else ''), graph,
                    f'format_version {version!r} is not supported', existing=existing)


def check_proofs(source, work, graphs):
    for tool in ('lean', 'lake'):
        require(shutil.which(tool) is not None, f'{tool} is required for certificate verification')
    sys.path.insert(0, str(source / 'formal/lean/tools'))
    import run_checks as checks
    package = checks.copy_sources(work / 'package')
    pinned = (package / 'lean-toolchain').read_text().strip().rsplit(':v', 1)[-1]
    version = run(['lean', '--version'], package, work / 'version.log')
    require(version.returncode == 0 and f'version {pinned},' in version.stdout,
            f'Lean {pinned} is required: {version.stdout}{version.stderr}')
    built = run(['lake', 'build'], package, work / 'lake-build.log')
    require(built.returncode == 0, f'lake build failed: {built.stdout}{built.stderr}')
    cases = [('Ordinary', graphs['ordinary'], (), True),
             ('KernelOrdinary', graphs['ordinary'], ('--kernel', '--graph-only'), True)]
    corrupted = copy.deepcopy(graphs['ordinary'])
    u, v = corrupted['edges'][0][:2]
    corrupted['nodes'][v]['rank'] = corrupted['nodes'][u]['rank']
    forged = copy.deepcopy(graphs['cycle'])
    forged['result']['status'] = 'no_cycle'
    for i, node in enumerate(forged['nodes']):
        node['rank'] = i
    cases += [('CorruptedRank', corrupted, (), False),
              ('ForgedCycleRank', forged, (), False)]
    for name, graph, options, valid in cases:
        converted, out = convert(source, work, name, graph, options)
        require(converted.returncode == 0, f'{name}: conversion failed: {converted.stderr}')
        result = run(['lake', 'env', 'lean', str(out)], package, work / (name + '-lean.log'))
        text = result.stdout + result.stderr
        if valid:
            theorems = ('certificate', 'acyclic') if '--graph-only' in options else checks.MODEL_THEOREMS
            wanted = {f'Generated.{name}.{t}' for t in theorems}
            require(result.returncode == 0 and 'sorry' not in text
                    and not wanted - checks.listed(text)
                    and not checks.check_axioms(name, text), f'{name}: proof check failed: {text}')
        else:
            line = checks.first_error_line(out, text)
            require(result.returncode != 0 and line is not None
                    and line.startswith('theorem certificate')
                    and 'Tactic `decide` proved that the proposition' in text,
                    f'{name}: rank was not rejected at certificate: {text}')
        print(f'PASS {name}: Lean {"checked the certificate" if valid else "rejected the rank"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--case', choices=('import', 'proof'), required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=args.work.resolve()))
    (args.work / 'latest.txt').write_text(str(work) + '\n', encoding='utf-8')
    produced = run([str(args.fixture.resolve()), str(source / 'examples/axi4_exchange.yaml'),
                    str(work)], work, work / 'fixture.log')
    require(produced.returncode == 0, f'graph generation failed: {produced.stdout}{produced.stderr}')
    graphs = {name: json.loads((work / (name + '.json')).read_text())
              for name in ('ordinary', 'grouped_dag', 'cycle', 'cycle_escape')}
    for name, graph in graphs.items():
        require(graph['edges'] and all(len(e) == 4 for e in graph['edges']), f'{name}: edge format')
        require(not graph.get('groups'), f'{name}: must exercise per-edge group column')
        grouped = any(e[3] != 0 for e in graph['edges'])
        require(grouped == (name in ('grouped_dag', 'cycle_escape')), f'{name}: group coverage')
        require(raw_acyclic(graph) == (name in ('ordinary', 'grouped_dag')), f'{name}: raw cycle')
        require(graph['result']['status'] == ('cycle' if name == 'cycle' else 'no_cycle'),
                f'{name}: cycle status')
    if args.case == 'import':
        check_imports(source, work, graphs)
    else:
        check_proofs(source, work, graphs)
    print(f'PASS certificate {args.case}')


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
        sys.exit(f'FAIL {error}')
