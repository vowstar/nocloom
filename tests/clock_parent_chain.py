"""Check divided and qualified clock edges and an APB transfer through the chain."""

import argparse
from functools import lru_cache
from fractions import Fraction
import json
from itertools import accumulate
from operator import mul
from pathlib import Path
import re
import subprocess
import tempfile

from sim_engine import Simulation, generated_rtl


def definition(ratios, qualified):
    # The parent names sort after the child names.
    names = ['root_domain', 'stage_c', 'stage_b', 'stage_a'][:len(ratios) + 1]
    periods = list(accumulate(ratios, mul, initial=10))
    if qualified:
        periods[-1] = periods[-2]
    domains = [
        dict(name=name, clock_root='root', clock_port='clk', reset_port='reset_n',
             test_port='test', ungate_port='hold', period_ps=1000 * period)
        for name, period in zip(names, periods)
    ]
    for domain, name, ratio in zip(domains[1:], names, ratios):
        domain.update(edge_parent=name, ratio=ratio)
    if qualified:
        domains[-1].pop('ratio')
        domains[-1].update(enable_port='qualify', ungate_port='force_leaf')
        del domains[-2]['ungate_port']

    def graph(path, source, target):
        return dict(nodes=[dict(name=f'n{i}', clock_domain=names[d]) for i, d in enumerate(path)],
                    links=[{'from': f'n{i}', 'to': f'n{i + 1}'} for i in range(len(path) - 1)],
                    entries=[dict(endpoint=source, node='n0')],
                    exits=[dict(endpoint=target, node=f'n{len(path) - 1}')])

    # The request crosses intermediate domains. The response also checks a crossing
    # that skips a level and must still take every intervening divider into account.
    order = list(range(len(names)))
    return dict(
        schema_version=10, name='clock_parent_chain', address_width=32,
        clock_gating=qualified,
        clock_roots=[dict(name='root', clock_port='clk', period_ps=10000)],
        clock_domains=domains, test_ports=[dict(name='test')],
        ungate_ports=[dict(name='hold')] + ([dict(name='force_leaf')] if qualified else []),
        enable_ports=[dict(name='qualify')] if qualified else [],
        reset_ports=[dict(name='reset_n')],
        initiators=[dict(name='cpu', protocol='apb4', data_width=32, clock_domain=names[0])],
        targets=[dict(name='regs', protocol='apb4', data_width=32, clock_domain=names[-1],
                      attributes='amba', mappings=[dict(base='0x1000', size='0x1000')])],
        connectivity='all',
        clock_crossings=[] if qualified else [
            dict(from_domain=source, to_domain=target, kind='edge_aligned')
            for source, target in [*zip(names, names[1:]), (names[-1], names[0])]
        ],
        topology=dict(request=graph(order, 'cpu', 'regs'),
                      response=graph([order[-1], 0], 'regs', 'cpu')),
        output=dict(backend='verilog', language='verilog_2001')), names, periods


BENCH = r"""
`timescale 1ns/1ps
module tb;
reg clk=0, reset_n=1, setup=0, access_phase=0, qualify=1, force_leaf=0;
reg measure=0;
always #5 clk=~clk;
wire ready, error, target_sel, target_en, target_write;
wire [31:0] data, addr;
nocloom_clock_parent_chain dut(
  .clk(clk), .reset_n(reset_n), .test(1'b0), .hold(1'b0), @CONTROLS@
  .nl_i_cpu_psel(setup), .nl_i_cpu_penable(access_phase),
  .nl_i_cpu_pwrite(1'b0), .nl_i_cpu_paddr(32'h1004),
  .nl_i_cpu_pwdata(32'b0), .nl_i_cpu_pstrb(4'b0), .nl_i_cpu_pprot(3'b0),
  .nl_i_cpu_pready(ready), .nl_i_cpu_prdata(data), .nl_i_cpu_pslverr(error),
  .nl_t_regs_psel(target_sel), .nl_t_regs_penable(target_en),
  .nl_t_regs_pwrite(target_write), .nl_t_regs_paddr(addr),
  .nl_t_regs_pready(1'b1), .nl_t_regs_prdata(32'h156a340b), .nl_t_regs_pslverr(1'b0)
);

integer completed=0;
always @(posedge dut.@LEAF@)
  if (reset_n && target_sel && target_en) begin
    if (target_write !== 1'b0 || addr !== 32'h4) $fatal(1, "CHECK_FAIL TARGET_ADDRESS");
    completed=completed+1;
  end

@MONITORS@

task automatic read_register;
  @(negedge clk); setup=1;
  @(negedge clk); access_phase=1;
  @(posedge clk);
  while (ready !== 1'b1) @(posedge clk);
  if (data !== 32'h156a340b || error !== 1'b0) $fatal(1, "CHECK_FAIL RESPONSE");
  @(negedge clk); setup=0; access_phase=0;
endtask

initial begin
  #1 reset_n=0;
  repeat (8) @(negedge clk);
  reset_n=1;
  #(@LEAF_PERIOD@ * 3 + 1) measure=1;
  read_register();
  #(@LEAF_PERIOD@ * 4 + 1);
  @COVERAGE@
  @FORCE_CHECK@
  if (completed != @TRANSACTIONS@) $fatal(1, "CHECK_FAIL TRANSFER_COUNT");
  $display("CHECK_PASS CLOCK_PARENT_CHAIN");
  $finish;
end
initial #(150 * @LEAF_PERIOD@) $fatal(1, "CHECK_FAIL CHAIN_TIMEOUT");
endmodule
"""


def clock_info(rtl, mapping, names, qualified):
    assignments = dict(re.findall(r'^assign (\w+) = (.*);$', rtl, re.M))
    gate_pins = re.findall(r'^nocloom_clock_gate .*?\.clock_in\((\w+)\).*?\.enable\((\w+)\)'
                           r'.*?\.clock_out\((\w+)\)', rtl, re.M)
    counters = {s['resource']: 'nl_internal_v' + s['control'][0].split('/')[1]
                for s in mapping['network']['storage'] if s['resource'].endswith('/divider')}

    @lru_cache(maxsize=None)
    def distance(net, source):
        if net == source:
            return 0
        return 1 + min((distance(n, source) for n in re.findall(r'\b[A-Za-z_]\w*\b',
                                                                assignments.get(net, ''))),
                       default=float('inf'))

    clocks = [('clk', '', 'clk')]
    qualifier = names[-1] if qualified is True else qualified
    for name in names[1:]:
        source = ('qualify' if name == qualifier
                  else counters[f'domain/{name}/divider'])
        distances = [distance(pins[1], source) for pins in gate_pins]
        best = min(distances)
        # A descendant reaches the source through the domain's own enable.
        assert best < float('inf') and distances.count(best) == 1, (name, distances)
        clocks.append(gate_pins[distances.index(best)])
    assert len({pins[2] for pins in clocks}) == len(clocks), clocks
    root_net = clocks[1][0]
    assert root_net == 'clk' or assignments[root_net] == 'clk', clocks
    assert all(pins[0] == root_net for pins in clocks[1:]), clocks
    for counter in counters.values():
        assert re.search(rf'always @\(posedge {root_net}[^\n]*\n'
                         rf'(?:[^\n]+\n){{0,2}}\s*{counter} <=', rtl), counter
    return clocks


def testbench(clocks, periods, qualified):
    monitors, coverage = [], []
    for i, ((_, _, clock), period) in enumerate(zip(clocks, periods)):
        signal = 'clk' if i == 0 else f'dut.{clock}'
        monitors.append(f"""
time previous_{i}=0;
integer edges_{i}=0;
always @(posedge {signal}) if (measure) begin
  if (edges_{i} && $time-previous_{i} != {period})
    $fatal(1, "CHECK_FAIL CHAIN_INTERVAL level={i} expected={period} observed=%0d",
           $time-previous_{i});
  previous_{i}=$time;
  edges_{i}=edges_{i}+1;
end
""")
        coverage.append(f'if (edges_{i} < 4) $fatal(1, "CHECK_FAIL NO_EDGES level={i}");')
    force_check = "" if not qualified else f"""
  measure=0;
  @(negedge clk); qualify=0;
  repeat (3) @(negedge dut.{clocks[-2][2]});
  repeat (6) begin
    @(posedge clk); #1;
    if (dut.{clocks[-1][2]} !== 1'b0) $fatal(1, "CHECK_FAIL QUALIFIER");
  end
  @(negedge clk); force_leaf=1;
  repeat (6) begin
    @(posedge clk); #1;
    if (dut.{clocks[-1][2]} !== dut.{clocks[-2][2]}) $fatal(1, "CHECK_FAIL FORCED_PARENT");
  end
  read_register();
"""
    return (BENCH.replace('@CONTROLS@', '.qualify(qualify), .force_leaf(force_leaf),' if qualified else '')
            .replace('@LEAF@', clocks[-1][2]).replace('@LEAF_PERIOD@', str(periods[-1]))
            .replace('@MONITORS@', ''.join(monitors)).replace('@COVERAGE@', '\n'.join(coverage))
            .replace('@FORCE_CHECK@', force_check).replace('@TRANSACTIONS@', '2' if qualified else '1'))


def run(cli, root, ratios, qualified=False, negative=False):
    root.mkdir()
    cfg, names, periods = definition(ratios, qualified)
    path = root / 'definition.yaml'
    path.write_text(json.dumps(cfg))
    out = root / 'rtl'
    generated = subprocess.run([cli, 'generate', path, '--out-dir', out, '--cdc-constraints'],
                               capture_output=True, text=True, timeout=30)
    assert generated.returncode == 0, generated.stderr
    top = out / 'nocloom_clock_parent_chain'
    mapping = json.loads(top.with_suffix('.map.json').read_text())
    sdc = top.with_suffix('.cdc.sdc').read_text()
    for name in names:
        assert f'set nocloom_period_{name} 10.000\n' in sdc, sdc
    if not qualified:
        assert 'set_clock_groups -asynchronous' not in sdc
        assert all(cut['kind'] == 'edge_aligned' for cut in mapping['network']['cdc']['cuts'])
    rtl = generated_rtl(top.with_suffix('.v'))
    clocks = clock_info(rtl, mapping, names, qualified)
    for label in ('normal', 'gate_without_parent_enable') if negative else ('normal',):
        work = root / label
        work.mkdir()
        source = rtl
        if label != 'normal':
            _, enable, _ = clocks[-1]
            source, count = re.subn(rf'^assign {enable} = \(?\w+ & (\w+)\)?;$',
                                    rf'assign {enable} = \1;', rtl, flags=re.M)
            assert count == 1, enable
        (work / 'network.v').write_text(source)
        (work / 'tb.sv').write_text(testbench(clocks, periods, qualified))
        result = Simulation(work, 'network.v').run()
        log = (work / 'simulation.log').read_text()
        if label == 'normal':
            assert result.returncode == 0 and 'CHECK_PASS CLOCK_PARENT_CHAIN' in log, log
        else:
            assert result.returncode != 0 and 'CHECK_FAIL CHAIN_INTERVAL' in log, log


def check_ratio_width(cli, root):
    root.mkdir()
    maximum = 2**32 - 1
    path = root / 'maximum.yaml'
    cfg, names, _ = definition((maximum,), False)
    for domain in cfg['clock_domains']:
        del domain['period_ps']
    path.write_text(json.dumps(cfg))
    result = subprocess.run([cli, 'generate', path, '--out-dir', root / 'rtl'], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    mapping = json.loads((root / 'rtl/nocloom_clock_parent_chain.map.json').read_text())
    count = next(s['control'][0] for s in mapping['network']['storage']
                 if s['resource'] == f'domain/{names[-1]}/divider')
    reg = next(r for r in mapping['network']['cdc']['registers'] if r['value'] == count)
    assert reg['width'] == 32 and reg['reset']['value'] == maximum - 1, reg


def period_artifacts(cli, work, cfg, rejection=()):
    work.mkdir()
    source = work / 'definition.yaml'
    source.write_text(json.dumps(cfg))
    result = subprocess.run([cli, 'generate', source, '--out-dir', work / 'rtl',
                             '--cdc-constraints', '--ipxact'],
                            capture_output=True, text=True, timeout=30)
    if rejection:
        assert result.returncode == 1, result
        assert all(text in result.stderr for text in rejection), result.stderr
        assert not (work / 'rtl').exists()
        return {}
    assert result.returncode == 0, result.stderr
    return {file.name: file.read_bytes() for file in (work / 'rtl').iterdir()}


def period_pair(cli, work, root_ps, wide, ratios=(2, 2)):
    cfg, names, _ = definition(ratios, False)
    cfg['clock_roots'][0]['period_ps'] = root_ps
    periods = dict(zip(names, accumulate(ratios, mul, initial=root_ps)))
    for domain in cfg['clock_domains']:
        domain['period_ps'] = periods[domain['name']]
    cfg['clock_crossings'] = []
    if wide:
        for graph in cfg['topology'].values():
            for node in graph['nodes']:
                node['transport_word_bytes'] = 8 if node['clock_domain'] == names[-1] else 4
    explicit = period_artifacts(cli, work / 'explicit', cfg)
    for domain in cfg['clock_domains']:
        del domain['period_ps']
    derived = period_artifacts(cli, work / 'derived', cfg)
    assert explicit.keys() == derived.keys()
    top = 'nocloom_clock_parent_chain'
    map_name = top + '.map.json'
    declared_map, derived_map = explicit.pop(map_name), derived.pop(map_name)
    assert declared_map.replace(b'"declared"', b'"derived"') == derived_map
    a, b = json.loads(declared_map), json.loads(derived_map)
    assert a.pop('clock_period_sources') == dict.fromkeys(names, 'declared')
    assert b.pop('clock_period_sources') == dict.fromkeys(names, 'derived')
    assert a == b and explicit == derived
    assert {d['name']: d['period_ps'] for d in b['definition']['clock_domains']} == periods
    depths = {(d['from_domain'], d['to_domain']): (d['depth'], d['depth_source'])
              for d in b['network']['cdc']['cuts']}
    for (sender, receiver), depth in depths.items():
        send, recv = periods[sender], periods[receiver]
        slow = max(send, recv)
        assert depth == ((3 * (send + recv) + slow - 1) // slow, 'formula'), depths
    assert depths
    assert f'<spirit:clockPeriod spirit:units="ps">{root_ps}</spirit:clockPeriod>'.encode() in (
        derived[top + '.ipxact.xml'])
    for name in names:
        assert f'set nocloom_period_{name} {root_ps // 1000}.{root_ps % 1000:03}\n'.encode() in (
            derived[top + '.cdc.sdc'])
    return cfg, names, b


def check_effective_periods(cli, work):
    work.mkdir()
    ordinary = work / 'width'
    ordinary.mkdir()
    cfg, names, mapping = period_pair(cli, ordinary, 10000, True)
    rtl = generated_rtl(ordinary / 'derived/rtl/nocloom_clock_parent_chain.v')
    clocks = clock_info(rtl, mapping, names, False)
    for name in ('transfer', 'wrong_response'):
        case = work / name
        case.mkdir()
        source = rtl
        if name == 'wrong_response':
            source, count = re.subn(r'^assign nl_i_cpu_prdata = (.*);$',
                                    r"assign nl_i_cpu_prdata = (\1) ^ 32'h1;", rtl, flags=re.M)
            assert count == 1
        (case / 'network.v').write_text(source)
        (case / 'tb.sv').write_text(testbench(clocks, [10, 20, 40], False))
        result = Simulation(case, 'network.v').run()
        log = (case / 'simulation.log').read_text()
        if name == 'transfer':
            assert result.returncode == 0 and 'CHECK_PASS CLOCK_PARENT_CHAIN' in log, log
        else:
            assert result.returncode != 0 and 'CHECK_FAIL RESPONSE' in log, log
    cfg['clock_domains'][0]['period_ps'] = 80000
    period_artifacts(cli, work / 'contradiction', cfg,
                     ('NCL_CDC_PERIOD_RELATION', '80000 ps', '10000 ps'))
    large = work / 'upper_input'
    large.mkdir()
    period_pair(cli, large, 1000000000, False)

    for root, ratio in ((255, 16843009), (128, 33554432)):
        pair = work / str(root * ratio)
        pair.mkdir()
        period_pair(cli, pair, root, False, (ratio, 1))

    cfg, names, _ = definition((2**25, 2**25), False)
    cfg['clock_crossings'] = []
    cfg['clock_roots'][0]['period_ps'] = 128
    for domain in cfg['clock_domains']:
        del domain['period_ps'], domain['test_port'], domain['ungate_port']
    files = period_artifacts(cli, work / 'large_delay', cfg)
    sdc = files['nocloom_clock_parent_chain.cdc.sdc'].decode()
    assert 'set_max_delay 288230376151711 ' in sdc, sdc
    assert 'set_max_delay 4294967.296 ' in sdc, sdc
    assert '[expr ' not in sdc, sdc

    for exponent in (26, 29):
        for domain in cfg['clock_domains'][1:]:
            domain['ratio'] = 2**exponent
        derived = 128 * 2**(2 * exponent)
        files = period_artifacts(cli, work / f'large_derived_{exponent}', cfg)
        data = json.loads(files['nocloom_clock_parent_chain.map.json'])
        periods = {d['name']: d['period_ps'] for d in data['definition']['clock_domains']}
        assert int(periods[names[-1]]) == derived
        cfg['clock_domains'][-1]['period_ps'] = derived
        period_artifacts(cli, work / f'declared_overflow_{exponent}', cfg, ('NCL_CDC_PERIOD_RANGE',))
        del cfg['clock_domains'][-1]['period_ps']


def widest_periods(cli, work):
    declaration = (2**64 - 1) // 32
    for name, root in (('explicit', Fraction(10**9)), ('inferred', Fraction(declaration)),
                       ('fractional', Fraction(declaration, 2))):
        cfg, names, _ = definition((2**32 - 1, 641, 6700417), False)
        cfg['clock_crossings'] = []
        for domain in cfg['clock_domains']:
            del domain['period_ps'], domain['test_port'], domain['ungate_port']
        if name == 'explicit':
            cfg['clock_roots'][0]['period_ps'] = int(root)
        elif name == 'inferred':
            del cfg['clock_roots'][0]['period_ps']
            cfg['clock_domains'][0]['period_ps'] = declaration
        elif name == 'fractional':
            del cfg['clock_roots'][0]['period_ps']
            cfg['clock_domains'].append(dict(cfg['clock_domains'][1], name='root_sample',
                                             ratio=2, period_ps=declaration))
            request = cfg['topology']['request']
            request['nodes'].append(dict(name='root_sample', clock_domain='root_sample'))
            request['links'][0]['from'] = 'root_sample'
            request['links'].insert(0, {'from': 'n0', 'to': 'root_sample'})
        files = period_artifacts(cli, work / name, cfg)
        top = 'nocloom_clock_parent_chain'
        data = json.loads(files[top + '.map.json'])
        periods = {d['name']: d['period_ps'] for d in data['definition']['clock_domains']}
        derived = root * (2**64 - 1)
        whole, remainder = divmod(derived.numerator, derived.denominator)
        assert periods[names[-1]] == (str(whole) if not remainder else
                                      dict(whole=str(whole), numerator=1, denominator=2))
        sdc = files[top + '.cdc.sdc'].decode()
        line = re.search(rf'^set nocloom_period_{names[-1]} ([\d.]+)$', sdc, re.M)
        assert 0 <= derived / 1000 - Fraction(line[1]) < Fraction(1, 1000000)
        xml = files[top + '.ipxact.xml']
        assert b'<spirit:clockPeriod ' in xml


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='chain-', dir=args.work))
    run(args.cli, work / 'two_levels', (2, 2), negative=True)
    run(args.cli, work / 'three_levels', (2, 3, 2), negative=True)
    run(args.cli, work / 'qualification', (2, 1), qualified=True)
    run(args.cli, work / 'large_parent', (65, 3), negative=True)
    for ratio in (65, 100, 1000):
        run(args.cli, work / f'ratio_{ratio}', (ratio,))
    check_ratio_width(args.cli, work / 'counter_width')
    check_effective_periods(args.cli, work / 'effective_periods')
    widest_periods(args.cli, work)
