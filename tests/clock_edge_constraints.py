"""Compare timed crossing exceptions with RTL edges and exercise qualified ancestry."""

import argparse
from fractions import Fraction
from itertools import pairwise
import json
from math import gcd, prod
from pathlib import Path
import re
import subprocess
import tempfile

from clock_parent_chain import clock_info, definition
from run_graph_clocked_example import period_depth
from sim_engine import Simulation, generated_rtl


def directed(cfg, names, order, kind, slow_pipeline=True):
    def graph(indices, source, target):
        nodes = [dict(name=f'n{i}', clock_domain=names[i]) for i in indices]
        return dict(nodes=nodes,
                    links=[{'from': a['name'], 'to': b['name']} for a, b in zip(nodes, nodes[1:])],
                    entries=[dict(endpoint=source, node=nodes[0]['name'])],
                    exits=[dict(endpoint=target, node=nodes[-1]['name'])])

    cfg['targets'][0]['clock_domain'] = names[order[-1]]
    cfg['topology'] = dict(request=graph(order, 'cpu', 'regs'),
                           response=graph(order[::-1], 'regs', 'cpu'))
    cfg['clock_crossings'] = []
    for a, b in zip(order, order[1:]):
        for source, target in [(a, b), (b, a)]:
            mode = kind(a, b)
            crossing = dict(from_domain=names[source], to_domain=names[target], kind=mode)
            if mode == 'edge_aligned':
                crossing['slow_pipeline'] = slow_pipeline
            elif mode == 'async':
                crossing['depth'] = 4
            cfg['clock_crossings'].append(crossing)


def generate(cli, work, cfg, *options):
    work.mkdir()
    source = work / 'definition.yaml'
    source.write_text(json.dumps(cfg))
    return subprocess.run([cli, 'generate', source, '--out-dir', work / 'rtl',
                           '--cdc-constraints', *options],
                          capture_output=True, text=True, timeout=30)


BENCH = r"""
`timescale 1ns/1fs
module tb;
reg clk=0, reset_n=1, psel=0, penable=0, qualify=1;
reg [31:0] addr=0;
always #@ROOT_HALF@ clk=~clk;
wire ready, error, target_sel, target_en, target_write;
wire [31:0] data, target_addr;
integer index, completed=0, wait_edges=0;
wire target_ready=wait_edges>=2;
nocloom_clock_parent_chain dut(
  .clk(clk), .reset_n(reset_n), @CONTROLS@
  .nl_i_cpu_psel(psel), .nl_i_cpu_penable(penable),
  .nl_i_cpu_pwrite(1'b0), .nl_i_cpu_paddr(addr),
  .nl_i_cpu_pwdata(32'b0), .nl_i_cpu_pstrb(4'b0), .nl_i_cpu_pprot(3'b0),
  .nl_i_cpu_pready(ready), .nl_i_cpu_prdata(data), .nl_i_cpu_pslverr(error),
  .nl_t_regs_psel(target_sel), .nl_t_regs_penable(target_en),
  .nl_t_regs_pwrite(target_write), .nl_t_regs_paddr(target_addr),
  .nl_t_regs_pready(target_ready), .nl_t_regs_prdata(32'h987a1000 ^ target_addr),
  .nl_t_regs_pslverr(1'b0)
);
@TRACE@
always @(posedge @TARGET@) if(reset_n) begin
  if(target_sel && target_en && target_ready) begin
    if(completed>=@READS@ || target_write!==1'b0 || target_addr!==4*(completed+1))
      $fatal(1,"CHECK_FAIL TARGET_ORDER");
    completed=completed+1;
    wait_edges<=0;
  end else if(target_sel && target_en) wait_edges<=wait_edges+1;
  else wait_edges<=0;
end

reg [15:0] sequence_bits=16'hca91;
integer skipped=0, passed=0;
always @(negedge clk) if (reset_n && @RANDOM@) begin
  sequence_bits={sequence_bits[14:0],sequence_bits[15]^sequence_bits[13]^sequence_bits[12]^sequence_bits[10]};
  qualify=@QUALIFY@;
  if (qualify) passed=passed+1; else skipped=skipped+1;
end

initial begin
  #1 reset_n=0;
  repeat (8) @(negedge clk);
  #2 reset_n=1;
  repeat (30) @(negedge clk);
  for (index=0; index<@READS@; index=index+1) begin
    @(negedge clk); addr=32'h1000+4*(index+1); psel=1;
    @(negedge clk); penable=1;
    @(posedge clk); while (ready !== 1'b1) @(posedge clk);
    if (error !== 1'b0 || data !== (32'h987a1000 ^ (4*(index+1))))
      $fatal(1,"CHECK_FAIL RESPONSE");
    @(negedge clk); psel=0; penable=0;
    repeat (index % 5) @(negedge clk);
  end
  repeat (150) @(negedge clk);
  if(completed!=@READS@) $fatal(1,"CHECK_FAIL TRANSFER_COUNT");
  if(@RANDOM@ && (skipped<20 || passed<20)) $fatal(1,"CHECK_FAIL ENABLE_COVERAGE");
  $display("CHECK_PASS EDGE_CONSTRAINTS");
  $finish;
end
initial #35000 $fatal(1,"CHECK_FAIL TIMEOUT");
endmodule
"""


def check_sdc(sdc, log, expected, scale):
    edges = {}
    for name, tick in re.findall(r'^EDGE (\w+) (\d+)$', log, re.M):
        edges.setdefault(name, []).append(int(tick))
    checked = set()
    for block in sdc.split('\n# cut/')[1:]:
        pair = re.match(r'[^\n]+: (?:sync|edge_aligned) (\w+) to (\w+);', block)
        if pair is None:
            continue
        a, b = pair.groups()
        first, second = set(edges[a]), set(edges[b])
        distinct_ticks = min(y - x for x, y in pairwise(sorted(first | second))
                             if (x in first and y in second) or (x in second and y in first))
        distinct = Fraction(distinct_ticks, 1000000 * scale)
        sp = re.findall(r'^set_max_delay (\d+(?:\.\d+)?) ', block, re.M)
        assert len(sp) == block.count('set_max_delay ') and sp, block
        for literal in sp:
            bound = Fraction(literal)
            assert bound <= distinct, (a, b, bound, distinct)
            if (a, b) in expected:
                fs = (expected[a, b] * 1000000) // 1
                assert bound == Fraction(fs, 1000000), (a, b, bound, expected[a, b])
                checked.add((a, b))
    assert checked == set(expected), (checked, expected)


def check_async_sdc(sdc, log, measured, protective):
    for name, period in measured.items():
        ticks = [int(t) for t in re.findall(rf'^EDGE {name} (\d+)$', log, re.M)]
        interval = min(b - a for a, b in zip(ticks, ticks[1:]))
        assert interval == period * 1000000, f'CLOCK_INTERVAL {name}: {interval} fs'
    checked = set()
    for block in sdc.split('\n# cut/')[1:]:
        cut = re.search(r': (\w+) to (\w+), (\d+) synchronizer stages', block)
        if cut is None:
            continue
        source, target, stages = cut.groups()
        literals = re.findall(r'^set_max_delay ([\d.]+) ', block, re.M)
        for periods in (measured, protective):
            a, b = periods[source], periods[target]
            expected = [min(a, b)] * 2 + [int(stages) * b] + [b] * 2 + [a] * 2
            actual = list(map(Fraction, literals))
            assert len(actual) == len(expected), block
            assert all(x <= y for x, y in zip(actual, expected)), (source, target, actual, expected)
        assert actual == expected, (source, target, actual, expected)
        checked.add((source, target))
    return checked


def simulate(cli, work, cfg, names, target, expected, forced='', qualifier='', negative=False,
             root_half_ns='5', scale=1, promised=False, async_periods=None):
    gen = generate(cli, work, cfg)
    assert gen.returncode == 0, gen.stderr
    top = work / 'rtl/nocloom_clock_parent_chain'
    rtl = generated_rtl(top.with_suffix('.v'))
    mapping = json.loads(top.with_suffix('.map.json').read_text())
    clocks = clock_info(rtl, mapping, names, qualifier)
    signals = dict(zip(names, ['clk'] + [f'dut.{c[2]}' for c in clocks[1:]]))
    controls = ''.join(f'.{port["name"]}(1\'b{int(port["name"] == forced)}),'
                       for port in cfg['test_ports'] + cfg['ungate_ports'])
    controls += ''.join(f'.{port["name"]}(reset_n),' for port in cfg['reset_ports']
                        if port['name'] != 'reset_n')
    if qualifier:
        controls += '.qualify(qualify),'
    trace = '\n'.join(f'always @(posedge {signal}) if (reset_n && $realtime>200)'
                      f' $display("EDGE {name} %.0f",$realtime * 1000000.0);'
                      for name, signal in signals.items())
    tb = (BENCH.replace('@CONTROLS@', controls).replace('@TRACE@', trace)
          .replace('@ROOT_HALF@', root_half_ns)
          .replace('@TARGET@', signals[target]).replace('@RANDOM@', '1' if qualifier else '0')
          .replace('@QUALIFY@', '(passed+skipped)%5==0 || (passed+skipped)%5==2' if promised
                   else 'sequence_bits[0] | sequence_bits[1]')
          .replace('@READS@', '16' if qualifier else '3'))
    (work / 'network.v').write_text(rtl)
    (work / 'tb.sv').write_text(tb)
    result = Simulation(work, 'network.v').run()
    log = (work / 'simulation.log').read_text()
    assert result.returncode == 0 and 'CHECK_PASS EDGE_CONSTRAINTS' in log, log[-3000:]
    sdc = top.with_suffix('.cdc.sdc').read_text()
    check_sdc(sdc, log, expected, scale)
    if async_periods:
        cuts = {(c['from_domain'], c['to_domain']) for c in cfg['clock_crossings']
                if c['kind'] == 'async'}
        assert check_async_sdc(sdc, log, *async_periods) == cuts
        if negative:
            unsafe = re.sub(r'^set_max_delay [\d.]+ ', 'set_max_delay 1000 ', sdc,
                            count=1, flags=re.M)
            assert unsafe != sdc
            try:
                check_async_sdc(unsafe, log, *async_periods)
            except AssertionError as error:
                assert len(error.args[0]) == 4, error
            else:
                raise AssertionError('timing checker accepted an unsafe asynchronous delay')
            mutant = work / 'closed_divider'
            mutant.mkdir()
            closed, count = re.subn(rf'(assign \w+ = ){forced};', r"\g<1>1'b0;", rtl)
            assert count == 1
            (mutant / 'network.v').write_text(closed)
            (mutant / 'tb.sv').write_text(tb)
            result = Simulation(mutant, 'network.v').run()
            log = (mutant / 'simulation.log').read_text()
            assert result.returncode == 0 and 'CHECK_PASS EDGE_CONSTRAINTS' in log
            try:
                check_async_sdc(sdc, log, *async_periods)
            except AssertionError as error:
                assert str(error).startswith('CLOCK_INTERVAL '), error
                (mutant / 'checker.log').write_text(str(error) + '\n')
            else:
                raise AssertionError('opening check accepted a closed divider')
            return mapping
    if promised:
        ticks = [int(t) for t in re.findall(rf'^EDGE {qualifier} (\d+)$', log, re.M)]
        gaps = {b - a for a, b in zip(ticks, ticks[1:])}
        assert gaps == {20_000_000, 30_000_000}, gaps
    if negative:
        old_bound = re.sub(r'^set_max_delay \d+(?:\.\d+)? ', 'set_max_delay 1000 ', sdc,
                           flags=re.M)
        assert old_bound != sdc
        try:
            check_sdc(old_bound, log, expected, scale)
        except AssertionError as error:
            assert len(error.args[0]) == 4 and error.args[0][2] > error.args[0][3], error
        else:
            raise AssertionError('timing checker accepted an unsafe delay')
    return mapping


def forced_chains(cli, work, chains):
    work.mkdir()
    for ratios in chains:
        for level, ratio in enumerate(ratios, start=1):
            if ratio == 1:
                continue
            for kind in ('async', 'related', 'related_fast', 'edge_aligned'):
                qualified = ratios[0] == 1
                cfg, names, nominal = definition(ratios, False)
                force = ('test', 'hold')[level % 2]
                field = 'test_port' if force == 'test' else 'ungate_port'
                for d in cfg['clock_domains']:
                    d.pop('test_port')
                    d.pop('ungate_port')
                cfg['clock_domains'][level][field] = force
                if qualified:
                    selected = cfg['clock_domains'][1]
                    del selected['ratio']
                    selected['enable_port'] = 'qualify'
                    cfg['enable_ports'] = [dict(name='qualify')]
                crossing_kind = 'related' if kind == 'related_fast' else kind
                directed(cfg, names, list(range(len(names))),
                         lambda a, b: 'async' if qualified and 1 in (a, b) else crossing_kind)
                has_async = any(c['kind'] == 'async' for c in cfg['clock_crossings'])
                if has_async:
                    cfg['reset_ports'].append(dict(name='other_reset_n'))
                    cfg['clock_domains'][0]['reset_port'] = 'other_reset_n'
                    for cut in cfg['clock_crossings']:
                        if cut['kind'] == 'async':
                            cut.update(synchronizer_stages=3, reset_handshake=True)
                if kind == 'related_fast':
                    for cut in cfg['clock_crossings']:
                        if cut['kind'] == 'related':
                            cut.update(fast=True, depth=2)
                protective = [t if i < level else t // ratio for i, t in enumerate(nominal)]
                expected = {}
                if kind != 'async':
                    for i, (a, b) in enumerate(zip(names, names[1:])):
                        if qualified and 1 in (i, i + 1):
                            continue
                        delay = 10 if qualified else 10 * prod(ratios[:min(i, level - 1)])
                        expected[a, b] = expected[b, a] = delay
                for opened in (False, True):
                    timing = (dict(zip(names, protective if opened else nominal)),
                              dict(zip(names, protective))) if has_async else None
                    tag = f'{len(ratios)}_{level}_{kind}_{opened}_{qualified}'
                    simulate(cli, work / tag, cfg, names, names[-1], expected,
                             forced=force if opened else '', qualifier=names[1] if qualified else '',
                             async_periods=timing, negative=(len(ratios) == 2 and level == 1
                                                            and kind == 'async' and opened))


def fixture(variables=False):
    cfg, names, _ = definition((2, 2, 3), False)
    if not variables:
        cfg['test_ports'] = []
        cfg['ungate_ports'] = []
        for domain in cfg['clock_domains']:
            del domain['test_port']
            del domain['ungate_port']
    return cfg, names


def fractional(cli, work):
    for name, ps, ratios, root_half, scale in (
            ('half', Fraction(20001, 2), (2, 4, 3), '5.000250', 1),
            ('third', Fraction(1000, 3), (3, 6, 2), '10', 60)):
        cfg, names = fixture()
        root, first, second, derived = cfg['clock_domains']
        del cfg['clock_roots'][0]['period_ps']
        for d in (root, derived):
            del d['period_ps']
        for d, ratio in zip((first, second, derived), ratios):
            d.update(edge_parent=names[0], ratio=ratio)
        first['period_ps'], second['period_ps'] = int(ps * ratios[0]), int(ps * ratios[1])
        directed(cfg, names, [0, 1, 2, 3], lambda a, b: 'related')
        a, b, c, d = names
        bound = ps / 1000
        result = simulate(cli, work / name, cfg, names, d,
                          {(a, b): bound, (b, c): bound * ratios[0],
                           (c, d): bound * gcd(ratios[1], ratios[2])},
                          negative=True, root_half_ns=root_half, scale=scale)
        root_value = result['definition']['clock_roots'][0]['period_ps']
        whole, remainder = divmod(ps.numerator, ps.denominator)
        assert root_value == dict(whole=whole, numerator=remainder, denominator=ps.denominator)
        domains = {entry['name']: entry for entry in result['definition']['clock_domains']}
        periods = {name: entry['period_ps'] for name, entry in domains.items()}
        assert periods[a] == root_value
        assert periods[b] == first['period_ps'] and periods[c] == second['period_ps']
        assert isinstance(periods[d], dict), periods
        assert Fraction(periods[d]['whole']) + Fraction(
            periods[d]['numerator'], periods[d]['denominator']) == ps * ratios[2]
        for cut in result['network']['cdc']['cuts']:
            ra, rz = [dict(zip(names, (1, *ratios)))[cut[key]]
                      for key in ('from_domain', 'to_domain')]
            assert cut['depth'] == (2 * (ra + rz) + max(ra, rz) - 1) // max(ra, rz)
            assert period_depth(domains[cut['from_domain']], domains[cut['to_domain']], 2) == cut['depth']

    # Both implied roots have the same truncated whole part.
    second['period_ps'] += 1
    result = generate(cli, work / 'fractional_mismatch', cfg)
    assert result.returncode == 1 and 'NCL_CDC_PERIOD_RELATION' in result.stderr, result.stderr
    assert '2001 ps' in result.stderr and '2000 ps' in result.stderr, result.stderr
    assert not (work / 'fractional_mismatch/rtl').exists()


def partial_period(cli, work):
    cfg, names = fixture()
    a, divided, b = names[:3]
    cfg['clock_domains'] = cfg['clock_domains'][:3]
    del cfg['clock_roots'][0]['period_ps']
    cfg['clock_roots'].append(dict(name='other', clock_port='clk_b'))
    _, child, peer = cfg['clock_domains']
    del child['period_ps']
    for key in ('period_ps', 'edge_parent', 'ratio'):
        peer.pop(key, None)
    peer.update(clock_root='other', clock_port='clk_b')
    directed(cfg, names, [0, 1, 2], lambda a, b: 'async')
    for crossing in cfg['clock_crossings']:
        if a not in (crossing['from_domain'], crossing['to_domain']):
            del crossing['depth']
            crossing['synchronizer_stages'] = 2

    maps, files = [], []
    for origin in ('derived', 'declared'):
        if origin == 'declared':
            child['period_ps'] = 20000
        case = work / origin
        result = generate(cli, case, cfg)
        assert result.returncode == 0, result.stderr
        mapping = json.loads(next((case / 'rtl').glob('*.map.json')).read_text())
        assert mapping.pop('clock_period_sources')[divided] == origin
        cuts = mapping['network']['cdc']['cuts']
        assert {(c['from_domain'], c['to_domain']) for c in cuts
                if c.get('unknown_period_domain') == b} == {(divided, b), (b, divided)}
        for cut in cuts:
            if a not in (cut['from_domain'], cut['to_domain']):
                assert (cut['depth'], cut['depth_source']) == (6, 'fallback')
            else:
                assert 'unknown_period_domain' not in cut
        maps.append(mapping)
        files.append({p.name: p.read_bytes() for p in (case / 'rtl').iterdir()
                      if not p.name.endswith('.map.json')})
    assert maps[0] == maps[1] and files[0] == files[1]

    for crossing in cfg['clock_crossings']:
        if a not in (crossing['from_domain'], crossing['to_domain']):
            crossing['kind'] = 'related'
            del crossing['synchronizer_stages']
    result = generate(cli, work / 'unrelated', cfg)
    assert result.returncode == 1 and result.stderr.count('NCL_CDC_RELATED_ROOT') == 2, result.stderr


def mixed_period(cli, work):
    work.mkdir()
    cfg, names, _ = definition((1,), False)
    del cfg['clock_roots']
    for d in cfg['clock_domains']:
        for field in ('clock_root', 'test_port', 'ungate_port', 'period_ps'):
            del d[field]
    known, unknown = cfg['clock_domains']
    del unknown['ratio'], unknown['edge_parent']
    unknown['clock_port'] = 'clk_peer'
    directed(cfg, names, [0, 1], lambda a, b: 'async')
    for ps in (10000, 10001, 10004, 4294967295999, 4294967296000, 454863562364420096):
        known['period_ps'] = ps
        case = work / str(ps)
        result = generate(cli, case, cfg)
        assert result.returncode == 0, result.stderr
        sdc = next((case / 'rtl').glob('*.cdc.sdc')).read_text()
        value = re.search(rf'^set nocloom_period_{names[0]} ([\d.]+)$', sdc, re.M)[1]
        assert sdc.count(f'min($nocloom_period_{names[0]}, $nocloom_period_{names[1]})') == 2
        assert sdc.count(f'min($nocloom_period_{names[1]}, $nocloom_period_{names[0]})') == 2
        exact = Fraction(ps, 1000)
        literal = Fraction(value)
        binary = Fraction(float(value))
        assert binary <= exact and literal <= exact, (ps, value, binary)
        if exact > 2**32:
            assert binary == literal == exact // 1
        elif Fraction(float(exact)) > exact:
            assert binary == literal
        if ps == 10000:
            assert value == '10.000'

    known.update(ratio=3, ungate_port='hold', period_ps=30000)
    case = work / 'independent_divider'
    result = generate(cli, case, cfg)
    assert result.returncode == 0, result.stderr
    sdc = next((case / 'rtl').glob('*.cdc.sdc')).read_text()
    assert f'set nocloom_period_{names[0]} 10.000\n' in sdc
    assert 'set_max_delay 20.000 ' in sdc


def directed_constraints(cli, work):
    cfg, names = fixture()
    root, parent, mid, leaf = names
    cfg['clock_domains'][3].update(edge_parent=parent, period_ps=60000)
    directed(cfg, names, [0, 1, 2, 3], lambda a, b: 'related')
    simulate(cli, work / 'siblings', cfg, names, leaf, {(mid, leaf): 20, (leaf, mid): 20},
             negative=True)

    cfg, names = fixture()
    directed(cfg, names, [0, 2, 3, 1], lambda a, b: 'edge_aligned')
    del cfg['clock_domains'][2]['period_ps']
    simulate(cli, work / 'fixed_chain', cfg, names, parent,
             {(root, mid): 10, (mid, leaf): 40, (leaf, parent): 20})

    cfg['clock_domains'][2]['period_ps'] = 80000
    mismatch = generate(cli, work / 'mismatch', cfg)
    assert mismatch.returncode != 0 and 'NCL_CDC_PERIOD_RELATION' in mismatch.stderr, mismatch
    assert '80000 ps' in mismatch.stderr and '40000 ps' in mismatch.stderr, mismatch.stderr

    for pipelined in (False, True):
        cfg, names = fixture()
        qualified = cfg['clock_domains'][1]
        del qualified['ratio']
        qualified.update(enable_port='qualify', period_ps=10000)
        cfg['enable_ports'] = [dict(name='qualify')]
        for domain in cfg['clock_domains'][2:]:
            domain['period_ps'] //= 2
        directed(cfg, names, [0, 3, 2, 1],
                 lambda a, b: 'async' if b == 1 else 'edge_aligned', pipelined)
        simulate(cli, work / f'qualified_{pipelined}', cfg, names, parent,
                 {(root, leaf): 10, (leaf, mid): 10}, qualifier=parent)

    qualified['period_ps'] = 20000
    cfg['clock_domains'][2]['period_ps'] = 40000
    cfg['clock_domains'][3]['period_ps'] = 120000
    directed(cfg, names, [0, 3, 2, 1],
             lambda a, b: 'async' if b == 1 else 'related')
    simulate(cli, work / 'qualified_promises', cfg, names, parent,
             {(root, leaf): 10, (leaf, mid): 10}, qualifier=parent,
             promised=True, negative=True)

    cfg, names = fixture(variables=True)
    directed(cfg, names, [0, 2, 3, 1], lambda a, b: 'edge_aligned')
    for force in ('hold', 'test'):
        expected = {(mid, leaf): 10, (leaf, parent): 10}
        simulate(cli, work / f'forced_{force}', cfg, names, parent, expected, forced=force)

    fractional(cli, work)
    partial_period(cli, work)
    mixed_period(cli, work / 'mixed_period')


# The forced chains each --cases value runs; constraints and all also run the
# directed constraints. The parts share no files and run as separate entries.
CHAINS = {
    'all': ((2, 3), (2, 3, 2), (1, 2, 2)),
    'constraints': (),
    'chain-2-3': ((2, 3),),
    'chain-1-2-2': ((1, 2, 2),),
    'deep-chain': ((2, 3, 2),),
}


def main(cli, work, cases):
    if cases in ('all', 'constraints'):
        directed_constraints(cli, work)
    if CHAINS[cases]:
        forced_chains(cli, work / 'forced_chains', CHAINS[cases])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--cases', choices=tuple(CHAINS), default='all')
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    main(args.cli, Path(tempfile.mkdtemp(prefix='edges-', dir=args.work)), args.cases)
