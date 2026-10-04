"""Measure burst beat rates through one clocked graph crossing per direction.

One AXI4 source s0 in domain src and one AXI4 target t0 in domain dst. The
request path crosses at the request node's exit and the response path at the
response node's exit, so every beat crosses one FIFO. Each variant generates a
definition through the CLI and runs one 256-beat write burst and one 256-beat
read burst with every ready held high. Handshakes are stamped in the clock of
the endpoint that sees them, and the
report gives beats per cycle of that clock and of the slower clock.
"""
import argparse
import json
import random
import shutil
import statistics
import subprocess
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

BEATS = 256
# name: (src period ps, dst period ps, synchronizer stages src -> dst, stages
#        dst -> src, explicit depth or None,
#        dst phase offset ps or None for a third of its period, minimum and maximum
#        rate relative to the network without a crossing, or None for aligned
#        equal clocks). With aligned equal clocks a depth D FIFO passes
#        D / (2n + 2) beats per cycle, so the expected rate there is the
#        smaller of that and the single-domain rate; it follows whatever the
#        endpoints of this network allow. The FIFO alone at every depth is
#        checked by async_fifo_throughput.
VARIANTS = {
    'same': (10000, 10000, 2, 2, None, None, 0.95, 1.01),
    'equal': (10000, 10000, 2, 2, None, None, 0.95, 1.01),
    'equal_aligned': (10000, 10000, 2, 2, None, 0, None, None),
    'equal_aligned_depth5': (10000, 10000, 2, 2, 5, 0, None, None),
    'equal_aligned_depth3': (10000, 10000, 2, 2, 3, 0, None, None),
    'equal_aligned_depth2': (10000, 10000, 2, 2, 2, 0, None, None),
    'equal_aligned_n4': (10000, 10000, 4, 4, None, 0, None, None),
    'equal_aligned_n4_depth9': (10000, 10000, 4, 4, 9, 0, None, None),
    'equal_aligned_n4_depth5': (10000, 10000, 4, 4, 5, 0, None, None),
    'equal_aligned_n4_depth4': (10000, 10000, 4, 4, 4, 0, None, None),
    'equal_aligned_n4_depth3': (10000, 10000, 4, 4, 3, 0, None, None),
    'slow_dst_1_3': (10000, 30000, 2, 2, None, None, 0.95, 1.01),
    'fast_dst_3_1': (30000, 10000, 2, 2, None, None, 0.95, 1.01),
    'ratio_7_5': (14000, 10000, 2, 2, None, None, 0.95, 1.01),
    'ratio_5_7': (10000, 14000, 3, 2, None, None, 0.95, 1.01),
    'fallback': (None, None, 2, 2, None, None, 0.95, 1.01),
}


def random_variants(seeds):
    result = {}
    for seed in seeds:
        rng = random.Random(seed)
        a, b = 2 * rng.randint(2000, 20000), 2 * rng.randint(2000, 20000)
        result[f'random_{seed}'] = (a, b, rng.randint(2, 4), rng.randint(2, 4), None,
                                    rng.randint(1, b - 1), 0.90, 1.01)
    return result


def definition(name, variant):
    src, dst, na, nb, depth = variant[:5]
    same = name == 'same'

    def domain(label, period):
        text = f'{{name: {label}, clock_port: clk_{label}, reset_port: reset_n, test_port: test'
        if period is not None:
            text += f', period_ps: {period}'
        return text + '}'
    # A period on a domain without a crossing is rejected, and so are
    # synchronizer stages: one domain declares neither.
    domains = [domain('src', None if same else src)] + ([] if same else [domain('dst', dst)])
    crossings = '' if same else f'synchronizer_stages: {na}\n'
    pair = {}
    if depth is not None:
        pair = {('src', 'dst'): f'depth: {depth}', ('dst', 'src'): f'depth: {depth}'}
    if not same and nb != na:
        pair[('dst', 'src')] = ', '.join(filter(None, [pair.get(('dst', 'src')), f'synchronizer_stages: {nb}']))
    if pair:
        crossings += 'clock_crossings:\n' + ''.join(
            f'  - {{from_domain: {a}, to_domain: {b}, {v}}}\n' for (a, b), v in pair.items())
    far = 'src' if same else 'dst'
    return f"""schema_version: 10
name: throughput
address_width: 32
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains:
""" + ''.join(f'  - {d}\n' for d in domains) + crossings + f"""initiators:
  - {{name: s0, clock_domain: src, protocol: axi4, data_width: 32, len_width: 8}}
targets:
  - {{name: t0, clock_domain: {far}, protocol: axi4, data_width: 32, len_width: 8, attributes: amba, mappings: [{{base: '0x0', size: '0x10000'}}]}}
transport: {{max_outstanding: 1, transport_word_bytes: 4}}
connectivity: all
topology:
  request:
    nodes: [{{name: n, clock_domain: src}}]
    links: []
    entries: [{{endpoint: s0, node: n}}]
    exits: [{{endpoint: t0, node: n}}]
  response:
    nodes: [{{name: r, clock_domain: {far}}}]
    links: []
    entries: [{{endpoint: t0, node: r}}]
    exits: [{{endpoint: s0, node: r}}]
output: {{backend: verilog, language: verilog_2001}}
"""


def bench(ports, src_ps, dst_ps, same, offset):
    inputs = [p for p in ports if p['direction'] == 'input' and p['name'].startswith(('nl_i_', 'nl_t_'))]
    outputs = [p for p in ports if p['direction'] == 'output']
    # %t follows the finest precision in the design, which some simulators set
    # below 1 ps; print stamps in picoseconds so the rates do not depend on it.
    lines = ['`timescale 1ps/1ps', 'module tb;', 'initial $timeformat(-12, 0, "", 0);',
             'reg reset_n=0;']
    clocks = sorted({p['name'] for p in ports if p['signal'] == 'clock'})
    for clock in clocks:
        lines.append(f'reg {clock}=0;')
    for p in inputs:
        lines.append(f"reg [{p['width']-1}:0] {p['name']}=0;")
    for p in outputs:
        lines.append(f"wire [{p['width']-1}:0] {p['name']};")
    lines.append('nocloom_throughput dut(' + ','.join(f".{p['name']}({p['name']})" for p in ports) + ');')
    lines.append(f'always #{src_ps // 2} clk_src=~clk_src;')
    if not same:
        # A fixed phase offset keeps the two clocks from sharing edges.
        lines.append(f'initial begin #{offset}; forever #{dst_ps // 2} clk_dst=~clk_dst; end')
    s, t = 'clk_src', ('clk_src' if same else 'clk_dst')
    lines.append(f"""
integer aw=0, w=0, b=0, ar=0, r=0, tw=0, tr=0, ta=0, tb_=0, errors=0;
reg [31:0] wdata_next=0;
reg write_open=0, read_open=0, target_write=0, target_read=0;
task check; input ok; input [8*32-1:0] tag; begin if(!ok) begin $display("ASSERT_FAIL:THROUGHPUT.%0s", tag); errors=errors+1; end end endtask
// Source on src: drive on negedge, sample on posedge.
always @(negedge {s}) begin
  nl_i_s0_awvalid = write_open && aw==0;
  nl_i_s0_awaddr = 32'h100; nl_i_s0_awlen = {BEATS-1}; nl_i_s0_awsize = 2; nl_i_s0_awburst = 1;
  nl_i_s0_wvalid = write_open && w<{BEATS}; nl_i_s0_wdata = 32'h5a000000 + w; nl_i_s0_wstrb = 4'hf; nl_i_s0_wlast = w=={BEATS-1};
  nl_i_s0_bready = 1;
  nl_i_s0_arvalid = read_open && ar==0;
  nl_i_s0_araddr = 32'h200; nl_i_s0_arlen = {BEATS-1}; nl_i_s0_arsize = 2; nl_i_s0_arburst = 1;
  nl_i_s0_rready = 1;
end
always @(posedge {s}) if(reset_n) begin
  if(nl_i_s0_awvalid && nl_i_s0_awready) begin aw=aw+1; $display("CY %0t s0 aw", $time); end
  if(nl_i_s0_wvalid && nl_i_s0_wready) begin w=w+1; $display("CY %0t s0 w", $time); end
  if(nl_i_s0_bvalid && nl_i_s0_bready) begin b=b+1; check(nl_i_s0_bresp==0, "BRESP"); $display("CY %0t s0 b", $time); end
  if(nl_i_s0_arvalid && nl_i_s0_arready) begin ar=ar+1; $display("CY %0t s0 ar", $time); end
  if(nl_i_s0_rvalid && nl_i_s0_rready) begin
    check(nl_i_s0_rdata === 32'ha5000000 + r, "RDATA");
    check(nl_i_s0_rlast === (r=={BEATS-1}), "RLAST");
    r=r+1; $display("CY %0t s0 r", $time);
  end
end
// Target on its own clock: every ready high, responses as soon as legal.
always @(negedge {t}) begin
  nl_t_t0_awready = !target_write; nl_t_t0_wready = target_write && tw<{BEATS};
  nl_t_t0_bvalid = target_write && tw=={BEATS}; nl_t_t0_bresp = 0; nl_t_t0_bid = nl_t_t0_awid;
  nl_t_t0_arready = !target_read;
  nl_t_t0_rvalid = target_read && tr<{BEATS}; nl_t_t0_rdata = 32'ha5000000 + tr; nl_t_t0_rlast = tr=={BEATS-1};
  nl_t_t0_rresp = 0; nl_t_t0_rid = nl_t_t0_arid;
end
always @(posedge {t}) if(reset_n) begin
  if(nl_t_t0_awvalid && nl_t_t0_awready) begin target_write=1; ta=ta+1; check(nl_t_t0_awlen=={BEATS-1}, "AWLEN"); $display("CY %0t t0 aw", $time); end
  if(nl_t_t0_wvalid && nl_t_t0_wready) begin
    check(nl_t_t0_wdata === 32'h5a000000 + tw, "WDATA");
    check(nl_t_t0_wlast === (tw=={BEATS-1}), "WLAST");
    tw=tw+1; $display("CY %0t t0 w", $time);
  end
  if(nl_t_t0_bvalid && nl_t_t0_bready) begin target_write=0; $display("CY %0t t0 b", $time); end
  if(nl_t_t0_arvalid && nl_t_t0_arready) begin target_read=1; tb_=tb_+1; check(nl_t_t0_arlen=={BEATS-1}, "ARLEN"); $display("CY %0t t0 ar", $time); end
  if(nl_t_t0_rvalid && nl_t_t0_rready) begin tr=tr+1; $display("CY %0t t0 r", $time); if(tr=={BEATS}) target_read=0; end
end
initial begin
  #({max(src_ps, dst_ps)} * 3 + 7) reset_n = 1;
  #({max(src_ps, dst_ps)} * 40);
  write_open = 1;
  wait(b==1);
  write_open = 0;
  #({max(src_ps, dst_ps)} * 20);
  read_open = 1;
  wait(r=={BEATS});
  read_open = 0;
  #({max(src_ps, dst_ps)} * 20);
  check(aw==1 && w=={BEATS} && b==1 && ar==1 && r=={BEATS} && ta==1 && tb_==1 && tw=={BEATS} && tr=={BEATS}, "EXACT_COUNT");
  if(errors==0) $display("CYCLE_PASS throughput beats={BEATS}");
  $finish;
end
initial begin #({max(src_ps, dst_ps)} * 40000); $display("ASSERT_FAIL:THROUGHPUT.TIMEOUT"); $finish; end
endmodule
""")
    return '\n'.join(lines) + '\n'


def rates(log, src_ps, dst_ps, same):
    events = {}
    for line in log.splitlines():
        if line.startswith('CY '):
            _, time, endpoint, channel = line.split()
            events.setdefault(f'{endpoint}.{channel}', []).append(int(time))
    period = {'s0': src_ps, 't0': src_ps if same else dst_ps}
    slow = max(src_ps, src_ps if same else dst_ps)
    result = {}
    for key in ('s0.w', 't0.w', 't0.r', 's0.r'):
        times = events.get(key, [])
        assert len(times) == BEATS, (key, len(times))
        own = period[key.split('.')[0]]
        gaps = [(b - a) // own for a, b in zip(times, times[1:])]
        span = times[-1] - times[0]
        result[key] = {'beats': len(times), 'span_ps': span,
                       'beats_per_cycle': round((len(times) - 1) * own / span, 4),
                       'beats_per_slow_cycle': round((len(times) - 1) * slow / span, 4),
                       'gap_cycles': {'mean': round(statistics.fmean(gaps), 4), 'max': max(gaps), 'min': min(gaps)}}
    # Write latency from source AW to target AW, read from target R0 to source R0.
    result['latency_ps'] = {'aw': events['t0.aw'][0] - events['s0.aw'][0],
                            'r0': events['s0.r'][0] - events['t0.r'][0],
                            'b': events['s0.b'][0] - events['t0.b'][0]}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--variants', nargs='*')
    parser.add_argument('--seeds', nargs='*', type=int, default=[11, 23, 37])
    args = parser.parse_args()
    variants = dict(VARIANTS)
    variants.update(random_variants(args.seeds))
    if args.variants is not None:
        if not args.variants:
            parser.error('At least one throughput variant must run')
        missing = set(args.variants) - variants.keys()
        if missing:
            parser.error('Unknown throughput variant: ' + ', '.join(sorted(missing)))
        # The single-domain run gives the rate of the same network without a crossing.
        variants = {k: v for k, v in variants.items() if k in args.variants or k == 'same'}
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    single_domain = {}
    for name, variant in variants.items():
        case = work / name
        if case.exists():
            assert not case.is_symlink()
            assert case.resolve() != work and case.resolve().is_relative_to(work)
            shutil.rmtree(case)
        case.mkdir()
        (case / 'throughput.yaml').write_text(definition(name, variant))
        subprocess.run([args.cli, 'generate', str(case / 'throughput.yaml'), '--out-dir', str(case / 'out')],
                       check=True, capture_output=True, text=True)
        mapping = json.loads((case / 'out/nocloom_throughput.map.json').read_text())
        (case / 'network.v').write_text(generated_rtl(case / 'out/nocloom_throughput.v'))
        src_ps = variant[0] or 10000
        dst_ps = variant[1] or 10000
        same = name == 'same'
        offset = dst_ps // 3 + 1 if variant[5] is None else variant[5]
        (case / 'tb.sv').write_text(bench(mapping['ports'], src_ps, dst_ps, same, offset))
        rc = simulate(case, work / 'compiled')
        log = (case / 'simulation.log').read_text()
        ok = rc == 0 and 'CYCLE_PASS' in log and 'ASSERT_FAIL' not in log
        row = {'variant': name, 'src_period_ps': src_ps, 'dst_period_ps': dst_ps, 'dst_offset_ps': offset,
               'src_stages': variant[2], 'dst_stages': variant[3], 'exit': rc,
               'passed': ok}
        cdc = mapping['network'].get('cdc', {})
        row['cuts'] = [{k: c.get(k) for k in ('name', 'depth', 'depth_source', 'count_bits')} for c in cdc.get('cuts', [])]
        if ok:
            row.update(rates(log, src_ps, dst_ps, same))
            if same:
                single_domain = {key: row[key]['beats_per_cycle'] for key in ('t0.w', 's0.r')}
            # Bounds are relative to the rate the network reaches without a
            # crossing: the source side moves at most one beat per source cycle
            # and the target side at most its single-domain rate per target cycle.
            low, high = variant[6], variant[7]
            for key in ('t0.w', 's0.r'):
                ideal = min(1 / src_ps, single_domain[key] / (src_ps if same else dst_ps))
                if variant[6] is None:
                    depth = row['cuts'][0]['depth']
                    # Aligned variants keep one stage count n: 2n + 2 cycles.
                    loop = depth / (variant[2] + variant[3] + 2)
                    expected = min(loop, single_domain[key]) / single_domain[key]
                    row[key]['expected_relative'] = round(expected, 4)
                    low, high = expected - 0.03, expected + 0.03
                rate = (row[key]['beats'] - 1) / row[key]['span_ps']
                relative = round(rate / ideal, 4)
                row[key]['relative_to_same'] = relative
                if not low <= relative <= high:
                    failures.append(f'{name} {key} relative beat rate {relative} outside [{low}, {high}]')
        else:
            failures.append(f'{name} simulation failed: ' + log[-2000:])
        rows.append(row)
        (work / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    table = ['| variant | src ps | dst ps | nA/nB | depths | t0.w beats/slow cycle | s0.r beats/slow cycle | relative to same | AW latency ps | R0 latency ps |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for row in rows:
        if 't0.w' in row:
            depths = '/'.join(f"{c['depth']}{(c['depth_source'] or 'held')[0]}" if c['depth'] else 'held' for c in row['cuts']) or '-'
            table.append(f"| {row['variant']} | {row['src_period_ps']} | {row['dst_period_ps']} | {row['src_stages']}/{row['dst_stages']} | "
                         f"{depths} | {row['t0.w']['beats_per_slow_cycle']} | {row['s0.r']['beats_per_slow_cycle']} | "
                         f"{row['t0.w']['relative_to_same']}/{row['s0.r']['relative_to_same']} | "
                         f"{row['latency_ps']['aw']} | {row['latency_ps']['r0']} |")
    (work / 'throughput.md').write_text('\n'.join(table) + '\n')
    print('\n'.join(table))
    assert not failures, '\n'.join(failures)
    print(f'CYCLE_PASS clocked_graph_throughput variants={len(rows)}')


if __name__ == '__main__':
    main()
