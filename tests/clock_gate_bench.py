"""Clock gate cells and endpoint transactions.

run_async_fifo.py --case faults imports the cell checks. Running this script
checks separate, shared and transport_only endpoint modes with two source
flows and two target flows, same-domain and asynchronous paths, request wake,
read and write response back pressure, test and ungate controls. AHB-Lite
reads to an APB4 target also check selected IDLE, high-phase wake requests,
wake with HREADY low, force release during a wait and common reset during an access.

The fixture (generate_async_fifo OUT clock_gate FAULT) emits a 16-bit counter
clocked through one clock gate, with the cell model ahead of the module. The
bench drives the domain clock (rising at k*T, falling at k*T + T/2), changes
enable at seeded offsets inside both clock phases and checks:

  GLITCH     every gated clock edge falls on a clock edge: the gated clock only
             passes whole high phases
  ENABLE     with test low, the gated clock pulses in cycle k exactly when
             enable was high just before the rising edge
  TEST_MODE  with test high, the gated clock pulses every cycle whatever enable
  COUNT      the counter on the gated clock counts exactly those pulses

Faults (cell model edits): no_latch gates with the live enable and must fail
GLITCH; no_test ignores test and must fail TEST_MODE. The unmodified cell
passes the same stimulus.
"""

import json
import random
import re
import subprocess
from pathlib import Path

from sim_engine import Simulation

PERIOD = 10000
FAULTS = {"no_latch": {"GLITCH"}, "no_test": {"TEST_MODE"}}


def bench(gated, offsets, enables, tests):
    n = len(offsets)
    table = "\n".join(f"    offset[{i}] = {o}; level[{i}] = {e}; mode[{i}] = {t};"
                      for i, (o, e, t) in enumerate(zip(offsets, enables, tests)))
    half = PERIOD // 2
    return f"""`timescale 1ps/1ps
module tb;
  // Assigned at time zero, not initialized, so the cell latch sees its first low phase.
  reg clk, reset_n, enable, test;
  initial {{ clk, reset_n, enable, test }} = 4'b0;
  wire [15:0] count;
  integer offset [0:{n - 1}];
  reg level [0:{n - 1}];
  reg mode [0:{n - 1}];
  integer k, j, pulses = 0, failures = 0;
  reg expected;
  nocloom_clock_gate_probe dut(.nl_clk(clk), .nl_reset_n(reset_n), .test(test),
                               .nl_enable(enable), .nl_count(count));
  wire gated = dut.{gated};
  task fail(input [8*16-1:0] label);
    begin
      $display("CHECK_FAIL %0s at %0t", label, $time);
      failures = failures + 1;
    end
  endtask
  always @(gated)
    if ($time > 0 && ($time % {PERIOD}) != (gated ? 0 : {half})) fail("GLITCH");
  initial begin
{table}
  end
  // Clock rising edges at (k + 1) * T; enable and test change once per cycle at a seeded
  // offset in either phase, never on an edge.
  initial begin
    #({PERIOD});
    for (k = 0; k < {n}; k = k + 1) begin
      #(offset[k]);
      enable = level[k];
      test = mode[k];
      #({PERIOD} - offset[k]);
    end
  end
  initial #({PERIOD} * 2 + {half} + 2000) reset_n = 1;
  initial begin
    #({PERIOD});
    for (j = 0; j < {n}; j = j + 1) begin
      expected = enable | test;
      clk = 1;
      #1 if (gated !== expected) fail(test ? "TEST_MODE" : "ENABLE");
      if (expected && reset_n) pulses = pulses + 1;
      #({half} - 1) clk = 0;
      #({half});
    end
    #({PERIOD});
    if (count !== pulses[15:0]) fail("COUNT");
    if (failures == 0) $display("CHECK_PASS clock_gate pulses=%0d", pulses);
    $finish;
  end
endmodule
"""


def write_bench(work, meta, rng, cycles=400):
    # Offsets avoid the clock edges by more than the checks' one picosecond.
    offsets = [rng.choice([rng.randrange(10, PERIOD // 2 - 10),
                           rng.randrange(PERIOD // 2 + 10, PERIOD - 10)]) for _ in range(cycles)]
    enables = [rng.randrange(2) for _ in range(cycles)]
    tests = [1 if cycles // 2 <= i < cycles // 2 + 40 else 0 for i in range(cycles)]
    (work / "tb.sv").write_text(bench(meta["gated"], offsets, enables, tests))


def case_rows(fixture, root, rng):
    """One run of the unmodified cell and one per fault, on the same stimulus."""
    seed = rng.randrange(1, 1 << 30)
    rows = []
    for fault in ("none", *FAULTS):
        work = Path(root) / "designs" / f"clock_gate_{fault}"
        work.mkdir(parents=True)
        generated = subprocess.run([fixture, str(work), "clock_gate", fault], capture_output=True,
                                   text=True, timeout=60, check=False)
        if generated.returncode:
            raise AssertionError(f"fixture failed: clock_gate {fault}: {generated.stderr}")
        write_bench(work, json.loads((work / "bindings.json").read_text()), random.Random(seed))
        result = Simulation(work, "network.v").run()
        text = (work / "simulation.log").read_text(errors="replace")
        failed = sorted({m.group(1) for m in re.finditer(r"CHECK_FAIL\W*([A-Z_]+)", text)})
        passed = result.returncode == 0 and "CHECK_PASS clock_gate" in text and not failed
        row = dict(name=f"clock_gate_{fault}", design=work.name, fault=fault, seed=seed,
                   passed=passed, failure=failed[0] if failed else None, failures=failed,
                   log=str(work / "simulation.log"))
        if fault == "none":
            row.update(expected="PASS", accepted=passed)
        else:
            row.update(expected=sorted(FAULTS[fault]),
                       accepted=not passed and bool(FAULTS[fault] & set(failed)))
        rows.append(row)
    return rows


def endpoint_configuration(mode, crossing):
    domains = [dict(name='main', clock_port='clk', reset_port='reset_n', test_port='test',
                    ungate_port='ungate')]
    if crossing:
        domains.append(dict(name='far', clock_port='far_clk', reset_port='far_reset_n',
                            test_port='test', ungate_port='ungate'))
    target_domain = 'far' if crossing else 'main'
    cfg = dict(schema_version=10, name='endpoint_gates', address_width=32,
               reset_ports=[dict(name='reset_n'), *([dict(name='far_reset_n')] if crossing else [])],
               test_ports=[dict(name='test')], ungate_ports=[dict(name='ungate')],
               clock_domains=domains,
               initiators=[dict(name='s', clock_domain='main', protocol='axi4', data_width=32,
                                id_width=1, len_width=0, route_classes=['ordinary', 'urgent'],
                                default_class='ordinary', id_ranges=[dict(first=1, last=1, **{'class': 'urgent'})],
                                clock_gating=mode)],
               targets=[dict(name='t', clock_domain=target_domain, protocol='axi4_lite', data_width=32,
                             attributes='amba', flows=2, clock_gating=mode,
                             mappings=[dict(base=hex(0x1000 + 0x1000 * f), size='0x1000', flow=f)
                                       for f in range(2)])], connectivity='all',
               output=dict(backend='verilog', language='verilog_2001'))
    if crossing:
        cfg['output']['register_reset'] = 'synchronous'
    cfg['topology'] = {
        direction: dict(nodes=[dict(name='entry', clock_domain='main')], links=[],
                        entries=[dict(endpoint=e, node='entry') for e in starts],
                        exits=[dict(endpoint=e, node='entry') for e in ends])
        for direction, starts, ends in [('request', ['s'], ['t']), ('response', ['t'], ['s'])]}
    return cfg


def endpoint_bench(mapping, crossing):
    declarations, connections = [], []
    for p in mapping['ports']:
        name, width = p['name'], p['width']
        declarations.append(f"{'reg' if p['direction'] == 'input' else 'wire'} "
                            f"[{width-1}:0] {name}" + ("=0;" if p['direction'] == 'input' else ';'))
        connections.append(f'.{name}({name})')
    gates = mapping['network']['clock_gates']
    sources = [g for g in gates if g.get('initiator') == 's']
    remaining = lambda g: 'dut.nl_internal_v' + g['remaining'].split('/')[1]
    sleeps = '\n'.join(f'if ({remaining(g)} !== 0) $fatal(1,"CHECK_FAIL SLEEP");'
                       for g in sources)
    sleeps += '\n' + '\n'.join(
        f'if (!test && !ungate && dut.@CLOCK_{i}@ !== 0) '
        '$fatal(1,"CHECK_FAIL SLEEP_CLOCK");'
        for i, g in enumerate(gates) if g.get('initiator') == 's')
    all_sleep = '\n'.join(f'if ({remaining(g)} !== 0) $fatal(1,"CHECK_FAIL IDLE_GATES");'
                           for g in gates)
    forced = '\n'.join(f'if ({remaining(g)} !== 0) $fatal(1,"CHECK_FAIL FORCE_COUNT");'
                       for g in sources)
    # Gate output nets are identified from the enable bindings and the generated cell instances.
    forced_clocks = '\n'.join(
        f'if (dut.@CLOCK_{i}@ !== {"far_clk" if g["clock_domain"] == "far" else "clk"}) '
        '$fatal(1,"CHECK_FAIL FORCE_CLOCK");'
        for i, g in enumerate(gates))
    return '''`timescale 1ns/1ps
module tb;
@DECL@
always #5 clk=~clk;
@FAR_CLOCK@
nocloom_endpoint_gates dut(@CONNECTIONS@);
integer flow, phase, n;
integer accepted=0, reads=0, writes=0, target_reads=0, target_writes=0;
always @(posedge clk) if(reset_n) begin
 if(nl_i_s_arvalid && nl_i_s_arready) accepted=accepted+1;
 if(nl_i_s_awvalid && nl_i_s_awready) accepted=accepted+1;
 if(nl_i_s_rvalid && nl_i_s_rready) reads=reads+1;
 if(nl_i_s_bvalid && nl_i_s_bready) writes=writes+1;
end
always @(posedge @TARGET_CLOCK@) if(@TARGET_RESET@) begin
 if(nl_t_t_arvalid_0 && nl_t_t_arready_0) target_reads=target_reads+1;
 if(nl_t_t_arvalid_1 && nl_t_t_arready_1) target_reads=target_reads+1;
 if(nl_t_t_awvalid_0 && nl_t_t_awready_0) target_writes=target_writes+1;
 if(nl_t_t_awvalid_1 && nl_t_t_awready_1) target_writes=target_writes+1;
end
reg [31:0] saved;
reg completed;
task tick; begin @(posedge clk); #1; end endtask
task quiet; begin repeat(60) tick; @SLEEP@ end endtask
task wait_source(input integer response);
 begin
 completed=0;
 for(n=0;n<2000 && !completed;n=n+1) begin
  @(posedge clk);
  if(response==0 && nl_i_s_arready) completed=1;
  if(response==1 && nl_i_s_awready) completed=1;
  if(response==2 && nl_i_s_wready) completed=1;
 end
 if(!completed) $fatal(1,"CHECK_FAIL REQUEST_TIMEOUT");
 @(negedge clk);
 end
endtask
initial begin
 nl_i_s_arsize=2; nl_i_s_awsize=2; nl_i_s_arburst=1; nl_i_s_awburst=1; nl_i_s_wlast=1;
 repeat(8) tick;
 @(negedge clk); reset_n=1; @FAR_RESET@
 quiet; @ALL_SLEEP@
 for(phase=0;phase<4;phase=phase+1) begin
  if(phase==3) begin
   @(negedge clk); reset_n=0; @ASSERT_FAR_RESET@
   repeat(8) tick;
   @(negedge clk); reset_n=1; @FAR_RESET@
   quiet; @ALL_SLEEP@
  end
  // Test and ungate must pass clocks even after the activity count reaches zero.
  @(negedge clk); test=(phase==1); ungate=(phase==2);
  repeat(4) tick;
  if(phase==1 || phase==2) begin @FORCED_CLOCKS@ @FORCE_COUNT@ end
  for(flow=0;flow<2;flow=flow+1) begin
   @(posedge clk); #2; nl_i_s_arid=flow[0]; nl_i_s_araddr=32'h1000+flow*32'h1000;
   nl_i_s_arvalid=1;
   #1; if(phase==0) begin @MID_CLOSED@ end
   wait_source(0); nl_i_s_arvalid=0;
   if(flow==0) begin
    wait(nl_t_t_arvalid_0);
    if(nl_t_t_araddr_0!==0) $fatal(1,"CHECK_FAIL READ_ROUTE");
    @(negedge @TARGET_CLOCK@); nl_t_t_arready_0=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_arready_0=0;
   end else begin
    wait(nl_t_t_arvalid_1);
    if(nl_t_t_araddr_1!==0) $fatal(1,"CHECK_FAIL READ_ROUTE");
    @(negedge @TARGET_CLOCK@); nl_t_t_arready_1=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_arready_1=0;
   end
   if(phase==2) begin @(negedge clk); ungate=0; end
   quiet;
   if(flow==0) begin nl_t_t_rdata_0=32'h12345678; nl_t_t_rvalid_0=1;
    wait(nl_t_t_rready_0); @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_rvalid_0=0;
   end else begin nl_t_t_rdata_1=32'h12345678; nl_t_t_rvalid_1=1;
    wait(nl_t_t_rready_1); @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_rvalid_1=0;
   end
   wait(nl_i_s_rvalid); saved=nl_i_s_rdata;
   repeat(20) begin tick;
    if(!nl_i_s_rvalid || nl_i_s_rdata!==saved || nl_i_s_rresp!==0 || nl_i_s_rid!==flow[0] || !nl_i_s_rlast || saved!==32'h12345678)
     $fatal(1,"CHECK_FAIL RESPONSE_STABLE");
   end
   @(negedge clk); nl_i_s_rready=1;
   @(posedge clk); @(negedge clk); nl_i_s_rready=0;
   quiet;
   // A write response also arrives after the source has gone to sleep.
   @(negedge clk); nl_i_s_awid=flow[0]; nl_i_s_awaddr=32'h1000+flow*32'h1000; nl_i_s_awvalid=1;
   wait_source(1); nl_i_s_awvalid=0;
   nl_i_s_wdata=32'habcdef01; nl_i_s_wstrb=15; nl_i_s_wvalid=1;
   wait_source(2); nl_i_s_wvalid=0;
   if(flow==0) begin
    wait(nl_t_t_awvalid_0);
    if(nl_t_t_awaddr_0!==0) $fatal(1,"CHECK_FAIL WRITE_ROUTE");
    @(negedge @TARGET_CLOCK@); nl_t_t_awready_0=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_awready_0=0;
    wait(nl_t_t_wvalid_0);
    if(nl_t_t_wdata_0!==32'habcdef01 || nl_t_t_wstrb_0!==15) $fatal(1,"CHECK_FAIL WRITE_DATA");
    @(negedge @TARGET_CLOCK@); nl_t_t_wready_0=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_wready_0=0;
   end else begin
    wait(nl_t_t_awvalid_1);
    if(nl_t_t_awaddr_1!==0) $fatal(1,"CHECK_FAIL WRITE_ROUTE");
    @(negedge @TARGET_CLOCK@); nl_t_t_awready_1=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_awready_1=0;
    wait(nl_t_t_wvalid_1);
    if(nl_t_t_wdata_1!==32'habcdef01 || nl_t_t_wstrb_1!==15) $fatal(1,"CHECK_FAIL WRITE_DATA");
    @(negedge @TARGET_CLOCK@); nl_t_t_wready_1=1;
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_wready_1=0;
   end
   quiet;
   if(flow==0) begin nl_t_t_bvalid_0=1; wait(nl_t_t_bready_0);
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_bvalid_0=0;
   end else begin nl_t_t_bvalid_1=1; wait(nl_t_t_bready_1);
    @(posedge @TARGET_CLOCK@); @(negedge @TARGET_CLOCK@); nl_t_t_bvalid_1=0;
   end
   wait(nl_i_s_bvalid);
   repeat(20) begin tick;
    if(!nl_i_s_bvalid || nl_i_s_bresp!==0 || nl_i_s_bid!==flow[0]) $fatal(1,"CHECK_FAIL WRITE_RESPONSE_STABLE");
   end
   @(negedge clk); nl_i_s_bready=1;
   @(posedge clk); @(negedge clk); nl_i_s_bready=0;
   quiet;
  end
  @(negedge clk); test=0; ungate=0; quiet; @ALL_SLEEP@
 end
 if(accepted!=16 || reads!=8 || writes!=8 || target_reads!=8 || target_writes!=8)
  $fatal(1,"CHECK_FAIL COMPLETE_ONCE");
 $display("CHECK_PASS endpoint_gates accepted=%0d reads=%0d writes=%0d",accepted,reads,writes); $finish;
end
initial begin #2000000; $fatal(1,"CHECK_FAIL TRANSACTION_TIMEOUT"); end
endmodule
'''.replace('@DECL@', '\n'.join(declarations)).replace(
        '@CONNECTIONS@', ','.join(connections)).replace('@SLEEP@', sleeps).replace(
        '@ALL_SLEEP@', all_sleep).replace('@MID_CLOSED@', '\n'.join(
            f'if (dut.@CLOCK_{i}@ !== 0) $fatal(1,"CHECK_FAIL MID_HIGH_WAKE");'
            for i, g in enumerate(gates) if g.get('initiator') == 's')).replace(
        '@ASSERT_FAR_RESET@', 'far_reset_n=0;' if crossing else '').replace('@FORCE_COUNT@', forced).replace('@FORCED_CLOCKS@', forced_clocks).replace(
        '@FAR_CLOCK@', 'always #7 far_clk=~far_clk;' if crossing else '').replace(
        '@FAR_RESET@', 'far_reset_n=1;' if crossing else '').replace(
        '@TARGET_CLOCK@', 'far_clk' if crossing else 'clk').replace(
        '@TARGET_RESET@', 'far_reset_n' if crossing else 'reset_n')


def endpoint_diff_cases(cli, root):
    from itertools import combinations
    root.mkdir(exist_ok=True)
    for kind in ('initiators', 'targets'):
        paths = {}
        for mode in (None, 'separate', 'shared', 'transport_only'):
            cfg = endpoint_configuration('separate', False)
            for endpoints in ('initiators', 'targets'):
                cfg[endpoints][0].pop('clock_gating')
            if mode is not None:
                cfg[kind][0]['clock_gating'] = mode
            path = root / f'{kind}_{mode}.yaml'
            path.write_text(json.dumps(cfg))
            paths[mode] = path
        for left, right in [(None, 'separate'), *combinations(
                ('separate', 'shared', 'transport_only'), 2)]:
            result = subprocess.run([cli, 'diff', str(paths[left]), str(paths[right])],
                                    capture_output=True, text=True, timeout=120)
            expected = 0 if left is None else 4
            assert result.returncode == expected, (kind, left, right, result.stdout, result.stderr)
            print(f'PASS diff {kind} {left} {right} exit={expected}', flush=True)


def ahb_configuration(mode):
    cfg = endpoint_configuration('separate', False)
    cfg['initiators'] = [dict(name='s', clock_domain='main', protocol='ahb_v3', data_width=32)]
    cfg['targets'] = [dict(name='t', clock_domain='main', protocol='apb4', data_width=32,
                          attributes='amba', mappings=[dict(base='0x1000', size='0x1000')])]
    if mode is not None:
        for kind in ('initiators', 'targets'):
            cfg[kind][0]['clock_gating'] = mode
    return cfg


def named_endpoint_cases(cli, root):
    from copy import deepcopy
    from sim_engine import generated_rtl
    for graph in (False, True):
        source, target = ('transport_edge', 'protocol_edge') if graph else ('protocol', 'transport')
        for mode in (None, 'separate', 'shared', 'transport_only'):
            work = root / f'names_{int(graph)}_{mode}'
            work.mkdir()
            cfg = endpoint_configuration(mode, graph)
            cfg['initiators'][0]['name'] = source
            cfg['targets'][0]['name'] = target
            if mode is None:
                for kind in ('initiators', 'targets'):
                    cfg[kind][0].pop('clock_gating')
            if graph:
                for direction in ('request', 'response'):
                    for kind in ('entries', 'exits'):
                        for attachment in cfg['topology'][direction][kind]:
                            attachment['endpoint'] = source if attachment['endpoint'] == 's' else target
            else:
                cfg.pop('topology')
            definition = work / 'definition.yaml'
            definition.write_text(json.dumps(cfg))
            result = subprocess.run([cli, 'generate', str(definition), '--out-dir', str(work / 'out')],
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stderr
            mapping = json.loads((work / 'out/nocloom_endpoint_gates.map.json').read_text())
            gates = mapping['network']['clock_gates']
            expected = (4 if mode in (None, 'separate') else 2) + (2 if graph else 0)
            assert len(gates) == expected, gates
            rtl = generated_rtl(work / 'out/nocloom_endpoint_gates.v')
            synchronous_cell = work / 'out/nocloom_synchronizer_sync_reset.v'
            if synchronous_cell.is_file():
                rtl += synchronous_cell.read_text()
            assert len(re.findall(r'^nocloom_clock_gate nl_internal_', rtl, re.M)) == expected
            bench_map = deepcopy(mapping)
            for gate in bench_map['network']['clock_gates']:
                if 'initiator' in gate:
                    gate['initiator'] = 's'
                if 'target' in gate:
                    gate['target'] = 't'
            tb = endpoint_bench(bench_map, graph).replace('nl_i_s_', f'nl_i_{source}_').replace(
                'nl_t_t_', f'nl_t_{target}_')
            (work / 'network.v').write_text(rtl)
            (work / 'tb.sv').write_text(bind_gate_clocks(tb, rtl, gates))
            result = Simulation(work, 'network.v').run()
            assert result.returncode == 0, (work / 'simulation.log').read_text()
            print(f'PASS names graph={graph} mode={mode}', flush=True)


def ahb_bench(mapping):
    declarations, connections = [], []
    for p in mapping['ports']:
        name, width = p['name'], p['width']
        if name == 'nl_i_s_hready':
            declarations.append('wire nl_i_s_hready;')
        else:
            driven = p['direction'] == 'input'
            declarations.append(f"{'reg' if driven else 'wire'} [{width-1}:0] {name}"
                                + ('=0;' if driven else ';'))
        connections.append(f'.{name}({name})')
    declarations.append('reg block_ready=0;\n'
                        'assign nl_i_s_hready = !block_ready && nl_i_s_hreadyout;')
    gates = mapping['network']['clock_gates']
    monitors, idle, forced, mid = [], [], [], []
    for i, gate in enumerate(gates):
        clock = f'dut.@CLOCK_{i}@'
        monitors.append(f'''integer edges_{i}=0, before_{i};
time rise_{i}=0;
always @(posedge {clock}) begin
 if(clk!==1) $fatal(1,"CHECK_FAIL MID_HIGH_WAKE");
 rise_{i}=$time; edges_{i}=edges_{i}+1;
end
always @(negedge {clock}) if(reset_n && $time-rise_{i}!=5)
 $fatal(1,"CHECK_FAIL SHORT_PULSE");''')
        idle.append(f'before_{i}=edges_{i};')
        forced.append(f'if({clock}!==clk) $fatal(1,"CHECK_FAIL FORCE_CLOCK");')
        mid.append(f'if({clock}!==0) $fatal(1,"CHECK_FAIL MID_HIGH_WAKE");')
    sleep = '\n'.join(f'if(edges_{i}!=before_{i}) $fatal(1,"CHECK_FAIL SELECTED_IDLE");'
                      for i in range(len(gates)))
    counts = '\n'.join(f'$display("CLOCK_COUNT zone={g["zone"]} owner={g.get("initiator", g.get("target", "switch"))} edges=%0d cycles=20", edges_{i}-before_{i});'
                       for i, g in enumerate(gates))
    wake = '\n'.join(f'if(edges_{i}<=before_{i}) $fatal(1,"CHECK_FAIL HREADY_INDEPENDENT_WAKE");'
                     for i, g in enumerate(gates)
                     if g.get('initiator') == 's' and g['zone'] in ('protocol', 'shared'))
    return '''`timescale 1ns/1ps
module tb;
@DECL@
always #5 clk=~clk;
nocloom_endpoint_gates dut(@CONNECTIONS@);
@MONITORS@
integer accepted=0, completed=0, target_completed=0, cancelled=0;
integer phase, n, start_completed, start_target;
reg pending=0;
reg [31:0] expected;
reg [1:0] expected_status=0;
always @(posedge clk) begin
 if(!reset_n) pending=0;
 else if(nl_i_s_hready) begin
  if(pending) begin
   if(nl_i_s_hrdata!==expected || nl_i_s_hresp!==expected_status)
    $fatal(1,"CHECK_FAIL AHB_RESPONSE");
   completed=completed+1;
  end
  pending=nl_i_s_hsel && nl_i_s_htrans[1];
  if(pending) accepted=accepted+1;
 end
 if(reset_n && nl_t_t_psel && nl_t_t_penable && nl_t_t_pready)
  target_completed=target_completed+1;
end
task tick; begin @(posedge clk); #1; end endtask
task quiet;
 begin
 repeat(60) tick;
 @IDLE@
 repeat(20) tick;
 @COUNTS@
 if(!test && !ungate) begin @SLEEP@ end
 if(pending || completed+cancelled!=accepted)
  $fatal(1,"CHECK_FAIL COMPLETE_ONCE");
 end
endtask
task request(input [31:0] address, input [31:0] data);
 begin
 expected=data; nl_t_t_prdata=data; nl_t_t_pready=0;
 // The request arrives inside a high phase with no gated rising edge.
 @(posedge clk); #2;
 nl_i_s_haddr=address; nl_i_s_htrans=2;
 #1;
 if(!test && !ungate) begin @MID@ end
 @(posedge clk);
 while(!nl_i_s_hready) @(posedge clk);
 @(negedge clk); nl_i_s_htrans=0;
 wait(nl_t_t_psel && nl_t_t_penable);
 if(nl_t_t_paddr!==(address-32'h1000) || nl_t_t_pwrite)
  $fatal(1,"CHECK_FAIL AHB_ROUTE");
 end
endtask
task response;
 begin
 // The APB target holds the data phase and its address through back pressure.
 repeat(12) begin
  tick;
  if(!nl_t_t_psel || !nl_t_t_penable || nl_i_s_hready)
   $fatal(1,"CHECK_FAIL WAIT_STABLE");
 end
 @(negedge clk); nl_t_t_pready=1;
 @(posedge clk); @(negedge clk); nl_t_t_pready=0;
 n=0;
 while(completed==start_completed && n<200) begin tick; n=n+1; end
 if(completed!=start_completed+1 || target_completed!=start_target+1)
  $fatal(1,"CHECK_FAIL COMPLETE_ONCE");
 quiet;
 end
endtask
initial begin
 nl_i_s_hsel=1; nl_i_s_hsize=2; nl_i_s_hprot=3;
 repeat(8) tick;
 @(negedge clk); reset_n=1;
 quiet;
 // An address requests clock pulses while another bus participant holds HREADY low.
 @(negedge clk); block_ready=1;
 expected=32'hface1234; nl_t_t_prdata=expected; nl_t_t_pready=0;
 @(posedge clk); #2; nl_i_s_haddr=32'h1000; nl_i_s_htrans=2;
 #1; @MID@
 @IDLE@
 repeat(20) begin
  tick;
  if(accepted || completed || pending || nl_t_t_psel || nl_t_t_penable)
   $fatal(1,"CHECK_FAIL HREADY_ADDRESS_ACCEPTED");
 end
 @WAKE@
 @(negedge clk); block_ready=0;
 @(posedge clk);
 while(!nl_i_s_hready) @(posedge clk);
 @(negedge clk); nl_i_s_htrans=0;
 wait(nl_t_t_psel && nl_t_t_penable);
 if(nl_t_t_paddr!==0 || nl_t_t_pwrite || accepted!=1)
  $fatal(1,"CHECK_FAIL AHB_ROUTE");
 start_completed=completed; start_target=target_completed;
 response;
 if(accepted!=1 || completed!=1 || target_completed!=1)
  $fatal(1,"CHECK_FAIL HREADY_COMPLETE_ONCE");
 for(phase=0;phase<4;phase=phase+1) begin
  if(phase==3) begin
   // Common reset cancels one waiting access. No response may leak into the next access.
   start_completed=completed; start_target=target_completed;
   request(32'h100c,32'habad0000);
   @(negedge clk); reset_n=0; cancelled=cancelled+1;
   repeat(8) tick;
   @(negedge clk); reset_n=1;
   #1;
   if(nl_t_t_psel || nl_t_t_penable || !nl_i_s_hreadyout)
    $fatal(1,"CHECK_FAIL RESET_QUIET");
   quiet;
   if(completed!=start_completed || target_completed!=start_target)
    $fatal(1,"CHECK_FAIL RESET_RESPONSE");
  end
  @(negedge clk); test=(phase==1); ungate=(phase==2);
  if(phase==1 || phase==2) begin
   repeat(20) begin
    tick; @FORCED@
    @(negedge clk); #1; @FORCED@
   end
  end
  start_completed=completed; start_target=target_completed;
  request(32'h1000+phase*4,32'h12345670+phase);
  if(phase==2) begin
   @(negedge clk); ungate=0;
   if(!pending || completed!=start_completed) $fatal(1,"CHECK_FAIL FORCE_RELEASE_SETUP");
  end
  response;
  @(negedge clk); test=0; ungate=0;
  quiet;
 end
 if(accepted!=6 || completed!=5 || cancelled!=1 || target_completed!=5)
  $fatal(1,"CHECK_FAIL COMPLETE_ONCE");
 $display("CHECK_PASS ahb_gates accepted=%0d completed=%0d cancelled=%0d target=%0d",
          accepted,completed,cancelled,target_completed);
 $finish;
end
initial begin #100000; $fatal(1,"CHECK_FAIL AHB_TIMEOUT"); end
endmodule
'''.replace('@DECL@', '\n'.join(declarations)).replace(
        '@CONNECTIONS@', ','.join(connections)).replace('@MONITORS@', '\n'.join(monitors)).replace(
        '@IDLE@', '\n'.join(idle)).replace('@SLEEP@', sleep).replace('@COUNTS@', counts).replace(
        '@MID@', '\n'.join(mid)).replace('@FORCED@', '\n'.join(forced)).replace('@WAKE@', wake)


def bind_gate_clocks(tb, rtl, gates):
    for i, gate in enumerate(gates):
        enable = 'nl_internal_v' + gate['enable'].split('/')[1]
        combined = re.search(rf'^assign (nl_internal_v\d+) = \({enable} \| .*\);$', rtl, re.M)
        assert combined, enable
        clock = re.search(rf'\.enable\({combined[1]}\).*\.clock_out\((nl_internal_v\d+)\)', rtl)
        assert clock, combined[1]
        tb = tb.replace(f'@CLOCK_{i}@', clock[1])
    return tb


def endpoint_cases(cli, root):
    from sim_engine import generated_rtl
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    for mode in ('separate', 'shared', 'transport_only'):
        for crossing in (False, True):
            work = root / f'{mode}_{int(crossing)}'
            work.mkdir(exist_ok=True)
            definition = work / 'definition.yaml'
            definition.write_text(json.dumps(endpoint_configuration(mode, crossing)))
            result = subprocess.run([cli, 'generate', str(definition), '--out-dir', str(work / 'out')],
                                    capture_output=True, text=True, timeout=120)
            assert result.returncode == 0, result.stderr
            mapping = json.loads((work / 'out/nocloom_endpoint_gates.map.json').read_text())
            bundle = json.loads((work / 'out/nocloom_endpoint_gates.bundle.json').read_text())
            assert bundle['definition'] == mapping['definition']
            gates = mapping['network']['clock_gates']
            expected = 2 if mode == 'separate' else 1
            for kind, name in [('initiator', 's'), ('target', 't')]:
                owned = [g for g in gates if g.get(kind) == name]
                assert len(owned) == expected, owned
                record = mapping['network']['endpoint_clock_gating'][kind][name]
                assert record == dict(
                    mode=mode, gate_count=expected,
                    protocol_clock='domain' if mode == 'transport_only' else 'gated'), record
                assert all(g['mode'] == mode for g in owned), owned
                assert {g['zone'] for g in owned} == (
                    {'protocol', 'transport'} if mode == 'separate' else
                    {'shared'} if mode == 'shared' else {'transport'}), owned
                assert all(g['zones'] == (['protocol', 'transport'] if mode == 'shared'
                                         else [g['zone']]) for g in owned), owned
                assert {z for g in owned for z in g['zones']} == (
                    {'transport'} if mode == 'transport_only' else {'transport', 'protocol'})
            rtl = generated_rtl(work / 'out/nocloom_endpoint_gates.v')
            synchronous_cell = work / 'out/nocloom_synchronizer_sync_reset.v'
            if synchronous_cell.is_file():
                rtl += synchronous_cell.read_text()
            tb = endpoint_bench(mapping, crossing)
            tb = bind_gate_clocks(tb, rtl, gates)
            (work / 'network.v').write_text(rtl)
            (work / 'tb.sv').write_text(tb)
            result = Simulation(work, 'network.v').run()
            assert result.returncode == 0, (work / 'simulation.log').read_text()
            print(f'PASS {mode} crossing={crossing}', flush=True)
    endpoint_diff_cases(cli, root / 'diff')
    named_endpoint_cases(cli, root)
    for mode in (None, 'separate', 'shared', 'transport_only'):
        work = root / f'ahb_{mode}'
        work.mkdir()
        definition = work / 'definition.yaml'
        definition.write_text(json.dumps(ahb_configuration(mode)))
        result = subprocess.run([cli, 'generate', str(definition), '--out-dir', str(work / 'out')],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        mapping = json.loads((work / 'out/nocloom_endpoint_gates.map.json').read_text())
        bundle = json.loads((work / 'out/nocloom_endpoint_gates.bundle.json').read_text())
        assert bundle['definition'] == mapping['definition']
        rtl = generated_rtl(work / 'out/nocloom_endpoint_gates.v')
        tb = bind_gate_clocks(ahb_bench(mapping), rtl, mapping['network']['clock_gates'])
        (work / 'network.v').write_text(rtl)
        (work / 'tb.sv').write_text(tb)
        result = Simulation(work, 'network.v').run()
        assert result.returncode == 0, (work / 'simulation.log').read_text()
        print(f'PASS ahb {mode}', flush=True)



if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    endpoint_cases(args.cli, args.work)
