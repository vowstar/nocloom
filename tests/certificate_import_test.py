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
                          ('FloatVersion', 2.0), ('FractionalVersion', 1.5),
                          ('ZeroVersion', 0), ('OldVersion', 1), ('OtherVersion', 3),
                          ('StringVersion', '1'), ('NullVersion', None)):
        graph = copy.deepcopy(ordinary)
        graph['format_version'] = version
        for existing in (False, True):
            rejects(source, work, name + ('Existing' if existing else ''), graph,
                    f'format_version {version!r} is not supported', existing=existing)
    for name, rank in (('FloatRank', 1.0), ('StringRank', '1'), ('TrueRank', True),
                       ('NegativeRank', -1), ('NullRank', None)):
        graph = copy.deepcopy(ordinary)
        graph['nodes'][0]['rank'] = rank
        for suffix, options in (('', ()), ('GraphOnly', ('--graph-only',)),
                                ('Kernel', ('--kernel',))):
            rejects(source, work, name + suffix, graph,
                    f'node 0 rank {rank!r} is not a nonnegative integer', options)


def check_scope(source, work, graphs):
    """The completion scope decides whether the Lean file states completion."""
    ordinary, withdrawn = graphs['ordinary'], graphs['withdrawn']
    require(ordinary['completion'] == {'applies': True, 'theorems': ['completes', 'delivered'],
                                       'unmet': []}, 'axi4_exchange must keep its completion')
    unmet = withdrawn['completion']['unmet']
    require(withdrawn['completion']['applies'] is False and unmet and
            {(u['clause'], u['reason']) for u in unmet} ==
            {('answer', 'timeout_without_completion_never_answers'),
             ('pool_fair', 'timeout_without_completion_never_answers')},
            f'axi4_target_timeout must withdraw completion: {withdrawn["completion"]}')
    for name, graph, theorem in (('ScopeApplies', ordinary, 'theorem completes'),
                                 ('ScopeWithdrawn', withdrawn, 'theorem completion_withdrawn')):
        result, out = convert(source, work, name, graph)
        text = out.read_text(encoding='utf-8') if result.returncode == 0 else ''
        require(theorem in text, f'{name}: {theorem} missing: {result.stderr}')
        if name == 'ScopeWithdrawn':
            require('theorem completes' not in text and 'theorem delivered' not in text,
                    f'{name}: completion stated for a withdrawn scope')
        print(f'PASS {name}: the Lean file follows the completion scope')
    forged = copy.deepcopy(withdrawn)
    forged['completion']['applies'] = True
    rejects(source, work, 'ScopeForged', forged, 'applies with unmet premises')
    silent = copy.deepcopy(withdrawn)
    silent['completion']['unmet'] = []
    rejects(source, work, 'ScopeSilent', silent, 'withdraws the theorems without a reason')
    missing = copy.deepcopy(ordinary)
    del missing['completion']
    rejects(source, work, 'ScopeMissing', missing, 'no completion scope')
    for name, change in (('ScopeTheorems', ('theorems', ['completes'])),
                         ('ScopeApplyText', ('applies', 'true')),
                         ('ScopeRow', ('unmet', [{'clause': 'answer', 'subject': 'target/t0'}])),
                         ('ScopeQuote', ('unmet', [{'clause': 'answer', 'subject': 't"0',
                                                    'reason': 'x'}]))):
        graph = copy.deepcopy(withdrawn)
        graph['completion'][change[0]] = change[1]
        rejects(source, work, name, graph, 'NCL_CERTIFICATE_SCOPE')
    shaped = graphs['shaped']['completion']
    require(shaped == {'applies': False, 'theorems': ['completes', 'delivered'],
                       'unmet': [{'clause': 'arb_fair', 'subject': 'source/s0',
                                  'reason': 'shaper_without_rate_holds_later_transfers'}]},
            f'a shaper of rate zero must withdraw completion: {shaped}')
    result, out = convert(source, work, 'ScopeShaped', graphs['shaped'])
    text = out.read_text(encoding='utf-8') if result.returncode == 0 else ''
    require('theorem completion_withdrawn' in text and 'theorem completes' not in text
            and '("arb_fair", "source/s0", "shaper_without_rate_holds_later_transfers")' in text,
            f'ScopeShaped: completion not withdrawn: {result.stderr}')
    print('PASS ScopeShaped: a shaper of rate zero withdraws completion')


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
              ('ForgedCycleRank', forged, (), False),
              ('Withdrawn', graphs['withdrawn'], (), True),
              ('Shaped', graphs['shaped'], (), True)]
    for name, graph, options, valid in cases:
        converted, out = convert(source, work, name, graph, options)
        require(converted.returncode == 0, f'{name}: conversion failed: {converted.stderr}')
        if name == 'Ordinary':
            witness = (source / 'formal/lean/tests/short_request.lean').read_text(encoding='utf-8')
            proof = out.read_text(encoding='utf-8')
            out.write_text('import GenericNoC.WormholeService\n' + proof + '\n' + witness,
                           encoding='utf-8')
        result = run(['lake', 'env', 'lean', str(out)], package, work / (name + '-lean.log'))
        text = result.stdout + result.stderr
        if valid:
            theorems, absent = checks.expected_theorems(out, '--graph-only' in options)
            require((theorems == checks.WITHDRAWN_THEOREMS) == (name in ('Withdrawn', 'Shaped')),
                    f'{name}: wrong completion scope')
            wanted = {f'Generated.{name}.{t}' for t in theorems}
            if name == 'Ordinary':
                wanted.update({'Generated.Ordinary.no_service_continuation',
                               'Generated.Ordinary.no_universal_finite_drain'})
            unwanted = {f'Generated.{name}.{t}' for t in absent}
            require(result.returncode == 0 and 'sorry' not in text
                    and not wanted - checks.listed(text) and not unwanted & checks.listed(text)
                    and not checks.check_axioms(name, text), f'{name}: proof check failed: {text}')
        else:
            line = checks.first_error_line(out, text)
            require(result.returncode != 0 and line is not None
                    and line.startswith('theorem chunk')
                    and 'Tactic `decide` proved that the proposition' in text,
                    f'{name}: rank was not rejected at a certificate chunk: {text}')
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
    # Report-only target timeouts: a network whose completion scope is withdrawn.
    produced = run([str(args.fixture.resolve()), str(source / 'examples/axi4_target_timeout.yaml'),
                    str(work / 'withdrawn'), 'ordinary'], work, work / 'fixture-withdrawn.log')
    require(produced.returncode == 0, f'graph generation failed: {produced.stdout}{produced.stderr}')
    withdrawn = json.loads((work / 'withdrawn' / 'ordinary.json').read_text())
    # axi4_exchange with a shaper of rate zero on s0: after its burst, s0 never sends again.
    exchange = (source / 'examples/axi4_exchange.yaml').read_text(encoding='utf-8')
    s0 = '{name: s0, clock_domain: main, data_width: 32, protocol: axi4, len_width: 8'
    require(exchange.count(s0 + '}') == 1, 'axi4_exchange: s0 line changed')
    shaped_yaml = work / 'shaped.yaml'
    shaped_yaml.write_text(exchange.replace(
        s0 + '}', s0 + ', qos: {shaper: {bytes_per_1024_cycles: 0, burst_bytes: 0}}}'),
        encoding='utf-8')
    produced = run([str(args.fixture.resolve()), str(shaped_yaml), str(work / 'shaped'),
                    'ordinary'], work, work / 'fixture-shaped.log')
    require(produced.returncode == 0, f'graph generation failed: {produced.stdout}{produced.stderr}')
    shaped = json.loads((work / 'shaped' / 'ordinary.json').read_text())
    for name, graph in graphs.items():
        require(graph['edges'] and all(len(e) == 4 for e in graph['edges']), f'{name}: edge format')
        require(not graph.get('groups'), f'{name}: must exercise per-edge group column')
        grouped = any(e[3] != 0 for e in graph['edges'])
        require(grouped == (name in ('grouped_dag', 'cycle_escape')), f'{name}: group coverage')
        require(raw_acyclic(graph) == (name in ('ordinary', 'grouped_dag')), f'{name}: raw cycle')
        require(graph['result']['status'] == ('cycle' if name == 'cycle' else 'no_cycle'),
                f'{name}: cycle status')
    graphs['withdrawn'] = withdrawn
    graphs['shaped'] = shaped
    if args.case == 'import':
        check_imports(source, work, graphs)
        check_scope(source, work, graphs)
    else:
        check_proofs(source, work, graphs)
    print(f'PASS certificate {args.case}')


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
        sys.exit(f'FAIL {error}')
