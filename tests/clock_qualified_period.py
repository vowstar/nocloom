"""Check spacing promises, including APB transfers through both directions of the crossing."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile

from clock_edge_constraints import generate
from clock_parent_chain import definition
from sim_engine import Simulation, generated_rtl


BENCH = r"""
`timescale 1ns/1ps
module tb;
  reg clk = 0, reset_n = 0, qualify = 0;
  always #5 clk = ~clk;
  integer input_edges = 0;
  always @(negedge clk) begin
    input_edges = input_edges + 1;
    qualify <= @SELECT@;
  end

  reg sel = 0, access_phase = 0;
  reg [31:0] addr = 0;
  wire ready, error, target_sel, target_access, target_write;
  wire [31:0] data, target_addr;
  nocloom_clock_parent_chain dut(
    .clk(clk), .reset_n(reset_n), .qualify(qualify),
    .nl_i_cpu_psel(sel), .nl_i_cpu_penable(access_phase), .nl_i_cpu_pwrite(1'b0),
    .nl_i_cpu_paddr(addr), .nl_i_cpu_pwdata(32'b0), .nl_i_cpu_pstrb(4'b0),
    .nl_i_cpu_pprot(3'b0), .nl_i_cpu_pready(ready), .nl_i_cpu_prdata(data),
    .nl_i_cpu_pslverr(error), .nl_t_regs_psel(target_sel),
    .nl_t_regs_penable(target_access), .nl_t_regs_pwrite(target_write),
    .nl_t_regs_paddr(target_addr), .nl_t_regs_pready(1'b1),
    .nl_t_regs_prdata(target_addr ^ 32'h61000000), .nl_t_regs_pslverr(1'b0)
  );

  integer reads = 0, root_edges = 0;
  always @(posedge clk) begin
    root_edges = root_edges + 1;
    if (reset_n && qualify && target_sel && target_access) begin
      reads = reads + 1;
      if (target_write !== 1'b0 || target_addr !== 4 * reads)
        $fatal(1, "CHECK_FAIL TARGET_ADDRESS");
    end
  end

  task automatic read_word(input integer offset);
    integer started;
    @(negedge clk);
    addr = 32'h1000 + offset;
    sel = 1;
    started = root_edges;
    @(negedge clk);
    access_phase = 1;
    @(posedge clk);
    while (ready !== 1'b1) @(posedge clk);
    if (error !== 1'b0 || data !== (32'h61000000 ^ offset))
      $fatal(1, "CHECK_FAIL RESPONSE");
    #1 $display("ROOT_CYCLES %0d", root_edges - started);
    @(negedge clk);
    sel = 0;
    access_phase = 0;
  endtask

  initial begin
    repeat (8) @(negedge clk);
    #1 reset_n = 1;
    repeat (12) @(negedge clk);
    read_word(4);
    read_word(8);
    repeat (20) @(negedge clk);
    if (reads != 2) $fatal(1, "CHECK_FAIL READ_COUNT");
    $display("CHECK_PASS QUALIFIED_PERIOD");
    $finish;
  end
  initial #15000 $fatal(1, "CHECK_FAIL TIMEOUT");
endmodule
"""


def fixture(ratios):
    cfg, names, _ = definition(ratios, False)
    cfg['enable_ports'] = [dict(name='qualify')]
    cfg['test_ports'] = cfg['ungate_ports'] = cfg['clock_crossings'] = []
    for domain in cfg['clock_domains']:
        del domain['test_port'], domain['ungate_port']
    selected = cfg['clock_domains'][1]
    del selected['ratio']
    selected.update(enable_port='qualify', period_ps=20000)
    return cfg, names


def artifacts(cli, work, cfg, *diagnostic):
    result = generate(cli, work, cfg, '--ipxact')
    if diagnostic:
        assert result.returncode == 1 and all(s in result.stderr for s in diagnostic), result.stderr
        assert not (work / 'rtl').exists()
        return
    assert result.returncode == 0, result.stderr
    return {file.name: file.read_bytes() for file in (work / 'rtl').iterdir()}


def mapping(files):
    return json.loads(files['nocloom_clock_parent_chain.map.json'])


def check_periods(files, names, periods, sources):
    data = mapping(files)
    assert data['clock_period_sources'] == dict(zip(names, sources)), data['clock_period_sources']
    actual = {d['name']: d.get('period_ps') for d in data['definition']['clock_domains']}
    assert actual == dict(zip(names, periods)), actual
    return data


def descendants(cli, work):
    cfg, names = fixture((1, 2, 3))
    root, selected, divided, leaf = cfg['clock_domains']
    selected['period_ps'] = 20001
    divided['period_ps'] = 41000
    del leaf['period_ps']
    sources = ['declared', 'promised', 'promised', 'derived']
    files = artifacts(cli, work / 'inherited', cfg)
    check_periods(files, names, [10000, 20001, 41000, 123000], sources)
    sdc = files['nocloom_clock_parent_chain.cdc.sdc'].decode()
    for name, ps in zip(names, (10, 10, 20, 60)):
        assert f'set nocloom_period_{name} {ps}.000\n' in sdc, sdc

    leaf['period_ps'] = 122999
    artifacts(cli, work / 'below_parent', cfg, 'NCL_CDC_PERIOD_RELATION', '122999 ps', '123000 ps')
    del leaf['period_ps'], root['period_ps'], cfg['clock_roots'][0]['period_ps']
    files = artifacts(cli, work / 'unknown_root', cfg)
    data = check_periods(files, names, [None, 20001, 41000, 123000],
                         ['unknown', *sources[1:]])
    assert 'period_ps' not in data['definition']['clock_roots'][0]
    sdc = files['nocloom_clock_parent_chain.cdc.sdc'].decode()
    for name in names[1:]:
        assert f'Set nocloom_period_{name} (minimum possible period in ns)' in sdc


def wide_period(cli, work):
    cfg, names = fixture((1, 2**20, 2**20))
    root, selected, divided, leaf = cfg['clock_domains']
    cfg['clock_roots'][0]['period_ps'] = root['period_ps'] = 100
    selected['period_ps'] = 10**9
    del divided['period_ps'], leaf['period_ps']
    periods = [100, 10**9, 10**9 * 2**20, str(10**9 * 2**40)]
    check_periods(artifacts(cli, work, cfg), names, periods,
                  ['declared', 'promised', 'derived', 'derived'])


def sizing(cli, work):
    work.mkdir()
    cfg, names = fixture((1,))
    for graph in cfg['topology'].values():
        for node in graph['nodes']:
            node['transport_word_bytes'] = 4 if node['clock_domain'] == names[0] else 8
    for tag, period, bits in (('promised', 20000, 64), ('equal', 10000, 32)):
        cfg['clock_domains'][1]['period_ps'] = period
        cuts = mapping(artifacts(cli, work / tag, cfg))['network']['cdc']['cuts']
        assert all(sum(f['width'] for f in c['fields'] if f['name'].startswith('data')) == bits
                   for c in cuts), cuts

    del cfg['clock_roots']
    for domain in cfg['clock_domains']:
        del domain['clock_root']
    selected = cfg['clock_domains'][1]
    del selected['edge_parent']
    selected.update(clock_port='clk_peer', period_ps=20000)
    files = artifacts(cli, work / 'independent', cfg)
    check_periods(files, names, [10000, 20000], ['declared', 'promised'])
    sdc = files['nocloom_clock_parent_chain.cdc.sdc']
    assert b'Set nocloom_period_stage_c (minimum possible period in ns)' in sdc
    xml = files['nocloom_clock_parent_chain.ipxact.xml']
    assert xml.count(b'<spirit:clockPeriod ') == 1 and b'>10000</spirit:clockPeriod>' in xml


def transfers(cli, work):
    cfg, names = fixture((1,))
    files = artifacts(cli, work / 'promise', cfg)
    data = check_periods(files, names, [10000, 20000], ['declared', 'promised'])
    assert [(c['depth'], c['depth_source']) for c in data['network']['cdc']['cuts']] == [(5, 'formula')] * 2
    sdc = files['nocloom_clock_parent_chain.cdc.sdc']
    assert b'set nocloom_period_stage_c 10.000\n' in sdc
    assert re.findall(rb'^set_max_delay ([\d.]+) ', sdc, re.M) == [b'10.000', b'10.000', b'20.000'] * 2

    equal = deepcopy(cfg)
    equal['clock_domains'][1]['period_ps'] = 10000
    explicit = artifacts(cli, work / 'equal', equal)
    del equal['clock_domains'][1]['period_ps']
    derived = artifacts(cli, work / 'derived', equal)
    name = 'nocloom_clock_parent_chain.map.json'
    assert explicit.pop(name).replace(b'"promised"', b'"derived"') == derived.pop(name)
    assert explicit == derived
    equal['clock_domains'][1]['period_ps'] = 9999
    artifacts(cli, work / 'below_root', equal, 'NCL_CDC_PERIOD_RELATION', '9999 ps', '10000 ps')

    rtl = generated_rtl(work / 'promise/rtl/nocloom_clock_parent_chain.v')
    wrong, count = re.subn(r'^assign nl_i_cpu_prdata = (.*);$',
                          r"assign nl_i_cpu_prdata = (\1) ^ 32'h1;", rtl, flags=re.M)
    assert count == 1
    # The root and selected clocks rise in the same time slot. Event ordering
    # at their crossing gives these two sets of completion counts.
    for label, select, cycles, source in (
            ('regular', '!qualify', {(20, 19), (19, 19)}, rtl),
            ('irregular', 'input_edges % 5 == 0 || input_edges % 5 == 2', {(22, 22), (24, 24)}, rtl),
            ('wrong_response', '!qualify', None, wrong)):
        case = work / label
        case.mkdir()
        (case / 'network.v').write_text(source)
        (case / 'tb.sv').write_text(BENCH.replace('@SELECT@', select))
        result = Simulation(case, 'network.v').run()
        log = (case / 'simulation.log').read_text()
        if cycles is None:
            assert result.returncode != 0 and 'CHECK_FAIL RESPONSE' in log, log
        else:
            assert result.returncode == 0 and 'CHECK_PASS QUALIFIED_PERIOD' in log, log
            assert tuple(map(int, re.findall(r'^ROOT_CYCLES (\d+)$', log, re.M))) in cycles, log


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='promise-', dir=args.work))
    descendants(args.cli, work)
    wide_period(args.cli, work / 'wide_period')
    sizing(args.cli, work / 'sizing')
    transfers(args.cli, work)
