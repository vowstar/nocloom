"""Clock gate cell bench, run by run_async_fifo.py --case faults.

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
                                )],
               targets=[dict(name='t', clock_domain=target_domain, protocol='axi4_lite', data_width=32,
                             attributes='amba', flows=2,
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

def ahb_configuration(mode):
    cfg = endpoint_configuration('separate', False)
    cfg['initiators'] = [dict(name='s', clock_domain='main', protocol='ahb_v3', data_width=32)]
    cfg['targets'] = [dict(name='t', clock_domain='main', protocol='apb4', data_width=32,
                          attributes='amba', mappings=[dict(base='0x1000', size='0x1000')])]
    if mode is not None:
        for kind in ('initiators', 'targets'):
            cfg[kind][0]['clock_gating'] = mode
    return cfg

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
    for mode in (None,):
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
