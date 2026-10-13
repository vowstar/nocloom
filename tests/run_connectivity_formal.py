"""Formal check: an unconnected pair causes no target handshake (SymbiYosys).

Network: two AXI4 sources and two AXI4 targets through the generated switch,
connectivity s0 -> t0 and s1 -> {t0, t1}, so s0 -> t1 is unconnected. Every
input of the network is free except that the common reset is applied in the
first cycle and released after it, and s1 never raises AWVALID, WVALID or
ARVALID. Any handshake at t1 would then have to come from s0. The port pipes
of s0 accept a second AR (AW) while the engine still serves the first, so the
harness keeps the open bursts of s0 three deep per direction, oldest first (a third waits with
its staged last response once its context is free):

  NO_T1_AW/W/AR  t1 never sees AWVALID, WVALID or ARVALID (unbounded proof).
  DEPTH_AR/AW    s0 never accepts a fourth open burst on one direction, and
  OPEN_R/B       never answers without an open burst, so the three-deep
                 bookkeeping is exact (unbounded proof).
  ADMISSION_R    the oldest open read of s0, when it addresses t1's window,
                 never answers OKAY or EXOKAY (unbounded proof; see the read
                 helpers below).
  ADMISSION_B    the same for the oldest open write, checked to depth 16 only
                 (twice the DECERR round trip).

Covers show the harness is not vacuous: s0 reaches t0 on AW and AR, s0
receives a DECERR B and a DECERR R for a burst into t1's window (the
unconnected pair, not an unmapped address), and s0 holds two open bursts on
each direction. Controls: the same harness on the network generated with
connectivity all fails NO_T1 (a counterexample exists); on that network, with
every burst into t1's window opened behind another one (it waits in the port
pipe), ADMISSION_R and ADMISSION_B each fail; and the sparse network with the
s0 admission match forced true still passes, because the generated switch has
no s0 input at t1's output.

A miss goes to the default target. ADMISSION_B is a bounded check
(abc bmc3, depth 16), with a control that removes the miss packet's error bit
and must fail ADMISSION_B. The unbounded write property is not established.

Read helpers L1R..L7R are assumptions of the unbounded task. They describe
s0's port pipe, engine state and offer through map-derived probe ports.
This flow checks the helpers only to depth 16. Its unbounded read result is
conditional on those helpers. control_helper_wrong must reject a helper
that forgets the staged burst.

"""
import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

YAML = """schema_version: 10
name: formal
address_width: 32
reset_ports: [{name: reset_n}]
clock_gating: none
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 1}
  - {name: s1, clock_domain: main, protocol: axi4, data_width: 32, id_width: 1}
targets:
  - {name: t0, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}
  - {name: t1, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba, mappings: [{base: '0x1000', size: '0x1000'}]}
transport: {max_outstanding: 1, transport_word_bytes: 4, response_codes: distinct}
@CONNECTIVITY@
output: {backend: verilog, language: verilog_2001}
"""
SPARSE = 'connectivity:\n  s0: [t0]\n  s1: [t0, t1]'


def flight_queue(name, push, address, id_in, pop):
    """Three-deep bookkeeping of s0's open bursts on one direction, oldest first.

    <name>_n counts them (0 to 3); <name>0.. say whether each addresses t1's window and
    <name>_i0.. hold its ID (1 bit here). An empty entry reads 0. A burst opens at its address
    handshake and closes at its response handshake (pop)."""
    def shift(field, value):
        return f"""\
  end else begin
    {name}{field}0 <= {name}_pop ? ({name}_n == 2'd1 ? ({name}_push ? {value} : 1'b0) : {name}{field}1)
                  : ({name}_push && {name}_n == 2'd0 ? {value} : {name}{field}0);
    {name}{field}1 <= {name}_pop ? ({name}_n == 2'd2 ? ({name}_push ? {value} : 1'b0) : {name}{field}2)
                  : ({name}_push && {name}_n == 2'd1 ? {value} : {name}{field}1);
    {name}{field}2 <= {name}_pop ? ({name}_n == 2'd3 && {name}_push ? {value} : 1'b0)
                  : ({name}_push && {name}_n == 2'd2 ? {value} : {name}{field}2);
  end
"""
    fields = [('', f'{name}_t1'), ('_i', id_in)]
    body = ''.join(f"""always @(posedge clk_main) begin
  if (!reset_n) begin
    {name}{f}0 <= 1'b0;
    {name}{f}1 <= 1'b0;
    {name}{f}2 <= 1'b0;
{shift(f, v)}end
""" for f, v in fields)
    return f"""reg [1:0] {name}_n = 0;
reg {name}0 = 0, {name}1 = 0, {name}2 = 0, {name}_i0 = 0, {name}_i1 = 0, {name}_i2 = 0;
wire {name}_push = {push};
wire {name}_pop = {pop} && {name}_n != 2'd0;
wire {name}_t1 = {address}[31:12] == 20'h1;
always @(posedge clk_main) begin
  if (!reset_n) {name}_n <= 2'd0;
  else if ({name}_push && !{name}_pop) {name}_n <= {name}_n + 2'd1;
  else if ({name}_pop && !{name}_push) {name}_n <= {name}_n - 2'd1;
end
{body}"""


# Internal nets of s0 the read helpers observe, exposed as probe ports of the prove case's design
# (name, width): the AR port pipe's occupancy, the read engine's state and the read offered to
# the merge (map: storage source/s0/pipe/ar, formal anchors read_state and pre_merge_read_valid).
PROBES = [('probe_ar_full', 1, False), ('probe_read_state', 2, False),
          ('probe_read_offered', 1, False), ('probe_read_staged', 1, False),
          ('probe_read_id', 1, False)]


def probed(rtl, mapping):
    """The sparse design with PROBES as extra outputs."""
    s0 = mapping['network']['formal']['sources']['s0']
    pipe = next(s for s in mapping['network']['storage'] if s['resource'] == 'source/s0/pipe/ar')
    assert len(pipe['control']) == 1, pipe
    net = lambda v: 'nl_internal_v' + v.split('/')[1]
    nets = [net(pipe['control'][0]), net(s0['read_state']), net(s0['pre_merge_read_valid']),
            f"{net(s0['read_staged_full'])} & {net(s0['read_staged_last'])}",
            net(s0['read_desc_id'])]
    header = 'module nocloom_formal (\n'
    assert rtl.count(header) == 1
    rtl = rtl.replace(header, header + ''.join(
        f'  output wire {f"[{w - 1}:0] " if w > 1 else ""}{name},\n' for name, w, _ in PROBES))
    end = rtl.index('endmodule')
    return rtl[:end] + ''.join(f'assign {name} = {value};\n'
                               for (name, _, _), value in zip(PROBES, nets)) + rtl[end:]


def harness(ports, assume_r=False, checks=('NO_T1', 'DEPTH', 'ORDER', 'ADMISSION_R', 'ADMISSION_B'),
            t1_second=False, helpers_checked=False, helper_wrong=False):
    assume_r = assume_r or helpers_checked
    if assume_r:
        ports = ports + PROBES
    decl = ',\n  '.join(f'{"input" if inp else "output"} wire '
                        f'{f"[{w - 1}:0] " if w > 1 else ""}{name}' for name, w, inp in ports)
    conn = ',\n  '.join(f'.{name}({name})' for name, _, _ in ports)
    asserts = {
        'NO_T1': '''\
  NO_T1_AW: assert (!nl_t_t1_awvalid);
  NO_T1_W: assert (!nl_t_t1_wvalid);
  NO_T1_AR: assert (!nl_t_t1_arvalid);
''',
        'DEPTH': '''\
  DEPTH_AR: assert (!(rq_n == 2'd3 && rq_push && !rq_pop));
  DEPTH_AW: assert (!(wq_n == 2'd3 && wq_push && !wq_pop));
  OPEN_R: assert (!(nl_i_s0_rvalid && rq_n == 2'd0));
  OPEN_B: assert (!(nl_i_s0_bvalid && wq_n == 2'd0));
''',
        'ORDER': '''\
  ORDER_R: assert (!(nl_i_s0_rvalid && nl_i_s0_rid != rq_i0));
  ORDER_B: assert (!(nl_i_s0_bvalid && nl_i_s0_bid != wq_i0));
''',
        'ADMISSION_B': '''\
  ADMISSION_B: assert (!(nl_i_s0_bvalid && wq0) || nl_i_s0_bresp[1]);
''',
        'ADMISSION_R': '''\
  ADMISSION_R: assert (!(nl_i_s0_rvalid && rq0) || nl_i_s0_rresp[1]);
'''}
    second = '''\
// Control only: a burst into t1's window opens only behind an open burst of
// the same direction, so it waits in the port pipe of s0.
always @* assume (!(rq_push && rq_t1 && rq_n == 2'd0) && !(wq_push && wq_t1 && wq_n == 2'd0));
''' if t1_second else ''
    read_helpers = '''
// Read-flight helpers, assumed so the open-source proof of ADMISSION_R closes.
// They are proven only offline (see the module docstring); the write side has
// no such closure and stays bounded. rf_t0: t0 saw an address handshake for
// the oldest open read of s0 since it became the oldest (not for a later read
// while its last beat is staged).
reg rf_t0 = 0;
always @(posedge clk_main) begin
  if (!reset_n || rq_pop) rf_t0 <= 1'b0;
  else if (nl_t_t0_arvalid && nl_t_t0_arready && !probe_read_staged) rf_t0 <= 1'b1;
end
// While the oldest open read is a miss the default target t0 never sees the
// address phase (the cycle that read closes excepted: the next one may pass
// through combinationally; a later read passes once the miss's last beat is staged),
// and an R beat that returns without a t0 address handshake carries the error bit.
always @* if (!initial_cycle) L1R: assume (!(rq0 && nl_t_t0_arvalid && !rq_pop && !probe_read_staged));
always @* if (!initial_cycle) L2R: assume (!(rq0 && !rf_t0 && nl_i_s0_rvalid) || nl_i_s0_rresp[1]);
// Port-pipe helpers (s0 keeps an AR in its one-entry port pipe until the merge takes the burst),
// probed through the map: probe_ar_full (the pipe holds an AR), probe_read_state (the engine:
// 2 waiting for the response, 3 answering locally) and probe_read_offered (a read offered to the
// merge). L3R: the open reads are the pipe's AR, the burst the engine answers (unless that burst is
// the pipe's own, still offered) and the burst whose staged last beat waits (probe_read_staged).
// L4R/L5R: an answering engine has an open read, and
// with no staged last beat ahead of it the oldest one's ID is its own (probe_read_id). L6R/L7R:
// rf_t0 stays low while the oldest open read is a miss, and with no read open.
always @* if (!initial_cycle) L3R: assume (rq_n == probe_ar_full +
  ((probe_read_state[1] && !(probe_ar_full && probe_read_offered)) ? 2'd1 : 2'd0) +
  (probe_read_staged ? 2'd1 : 2'd0));
always @* if (!initial_cycle) L4R: assume (!probe_read_state[1] || rq_n != 2'd0);
always @* if (!initial_cycle) L5R: assume (!probe_read_state[1] || probe_read_staged ||
  probe_read_id == rq_i0);
always @* if (!initial_cycle) L6R: assume (!(rq0 && rf_t0));
always @* if (!initial_cycle) L7R: assume (rq_n != 2'd0 || !rf_t0);
''' if assume_r else ''
    # Bounded here: the helpers themselves hold on the probed design (see the docstring).
    if helpers_checked:
        read_helpers = re.sub(r'(L\dR): assume', r'\1: assert', read_helpers)
    # Control: L3R that forgets the staged burst must fail the bounded check.
    if helper_wrong:
        read_helpers = read_helpers.replace("(probe_read_staged ? 2'd1 : 2'd0)", "2'd0")
    queues = flight_queue('rq', 'nl_i_s0_arvalid && nl_i_s0_arready', 'nl_i_s0_araddr',
                          'nl_i_s0_arid', 'nl_i_s0_rvalid && nl_i_s0_rready && nl_i_s0_rlast') + \
        flight_queue('wq', 'nl_i_s0_awvalid && nl_i_s0_awready', 'nl_i_s0_awaddr',
                     'nl_i_s0_awid', 'nl_i_s0_bvalid && nl_i_s0_bready')
    body = ''.join(asserts[c] for c in checks)
    return f'''module formal_top(
  {decl}
);
nocloom_formal dut(
  {conn}
);
reg initial_cycle = 1;
always @(posedge clk_main) initial_cycle <= 0;
// The common reset is applied once at the start and then released.
always @* if (initial_cycle) assume (!reset_n); else assume (reset_n);
// s1 stays idle: every t1 handshake would have to come from s0.
always @* assume (!nl_i_s1_awvalid && !nl_i_s1_wvalid && !nl_i_s1_arvalid);
// The open bursts of s0, at most three per direction: the port pipe holds one
// while the engine (max_outstanding 1) serves the previous one and the one
// before waits with its staged last response.
{queues}{second}// NO_T1: t1 never sees AWVALID, WVALID or ARVALID. DEPTH/OPEN/ORDER: the
// bookkeeping is exact (no third open burst, no response without an open
// burst, every response carries the ID of the oldest open burst). ADMISSION: the oldest open burst of s0, when it addresses t1's
// window, never answers OKAY or EXOKAY (DECERR, or SLVERR for an illegal
// burst shape).
always @* if (!initial_cycle) begin
{body}end
always @* if (!initial_cycle) begin
  REACH_T0_AW: cover (nl_t_t0_awvalid);
  REACH_T0_AR: cover (nl_t_t0_arvalid);
  // DECERR for the unconnected pair itself, not for an unmapped address.
  DENIED_B: cover (nl_i_s0_bvalid && wq0 && nl_i_s0_bresp == 2'b11);
  DENIED_R: cover (nl_i_s0_rvalid && rq0 && nl_i_s0_rresp == 2'b11);
  // A second burst waits in the port pipe of s0 on each direction.
  TWO_OPEN_AW: cover (wq_n == 2'd2);
  TWO_OPEN_AR: cover (rq_n == 2'd2);
  // A third waits behind a staged last response.
  THREE_OPEN_AW: cover (wq_n == 2'd3);
  THREE_OPEN_AR: cover (rq_n == 2'd3);
end
{read_helpers}endmodule
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--solver', default='z3')
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')

    def generate(label, connectivity):
        config = work / f'{label}.yaml'
        config.write_text(YAML.replace('@CONNECTIVITY@', connectivity))
        proc = subprocess.run([args.cli, 'generate', str(config), '--out-dir', str(work / label)],
                              capture_output=True, text=True, timeout=300, check=False)
        assert proc.returncode == 0, proc.stderr
        mapping = json.loads((work / label / 'nocloom_formal.map.json').read_text())
        # The module, then the cell models it instantiates (its reset release synchronizer).
        bundle = json.loads((work / label / 'nocloom_formal.bundle.json').read_text())
        return mapping, ''.join((work / label / f).read_text()
                                for f in ['nocloom_formal.v'] + bundle['simulation_sources'][:-1])

    mapping, sparse = generate('sparse', SPARSE)
    full_map, full = generate('all', 'connectivity: all')
    ports = [(p['name'], p['width'], p['direction'] == 'input') for p in mapping['ports']]
    assert ports == [(p['name'], p['width'], p['direction'] == 'input') for p in full_map['ports']]
    admission = next(a for a in mapping['network']['admission'] if a['source'] == 's0')
    signal = 'nl_internal_v' + admission['write_route_match'].split('/')[1]
    forced, count = re.subn(r'(assign ' + signal + r' = )([^;]+);', r"\g<1>1'b1;", sparse)
    assert count == 1
    # Negative control for the bounded write check: the miss write packet's
    # error bit (the pre-merge formal anchor) forced low, so the miss reaches
    # t0 as an ordinary write and may answer OKAY -- ADMISSION_B must fail.
    miss_error = mapping['network']['formal']['sources']['s0']['pre_merge_write_error']
    miss_signal = 'nl_internal_v' + miss_error.split('/')[1]
    miss_okay, count = re.subn(r'(assign ' + miss_signal + r' = )([^;]+);', r"\g<1>1'b0;", sparse)
    assert count == 1
    engine = {'prove': 'abc pdr', 'bmc': f'smtbmc {args.solver}', 'cover': f'smtbmc {args.solver}',
              'abc_bmc': 'abc bmc3'}
    # With s0's write admission forced, the write into t1's window reaches t0's
    # outlet, whose window check answers DECERR: every property still holds.
    # On the full matrix t1 answers it, so NO_T1 and ADMISSION fail.
    # The forced control's teeth are shallow (a hidden s0 -> t1 path fails NO_T1 within a
    # few cycles, and the DECERR round trip takes about 8). Depth 12 is twice the DECERR round
    # trip, and the round trip and the control's counterexamples stay shallow.
    # ADMISSION_B is bounded at 16 = twice the DECERR round trip and checked with abc bmc3.
    # The control_miss_okay counterexample is at step 6.
    # The control_second_* cases run the full matrix with every burst into t1's
    # window opened behind another one, so it waits in s0's port pipe: the
    # two-deep bookkeeping must catch its OKAY answer (counterexamples at step 10).
    prove = {'assume_r': True, 'checks': ('NO_T1', 'DEPTH', 'ORDER', 'ADMISSION_R')}
    second_r = {'checks': ('ADMISSION_R',), 't1_second': True}
    second_b = {'checks': ('ADMISSION_B',), 't1_second': True}
    cases = [('prove', probed(sparse, mapping), 'prove', 0, 'PASS', '', prove),
             ('helpers_bounded', probed(sparse, mapping), 'abc_bmc', 16, 'PASS', '',
              {'helpers_checked': True, 'checks': ('DEPTH',)}),
             ('control_helper_wrong', probed(sparse, mapping), 'abc_bmc', 16, 'FAIL', ('L3R',),
              {'helpers_checked': True, 'helper_wrong': True, 'checks': ('DEPTH',)}),
             ('cover', sparse, 'cover', 16, 'PASS', '', {}),
             ('control_all', full, 'bmc', 16, 'FAIL', ('NO_T1', 'ADMISSION'), {}),
             ('control_forced_admission', forced, 'abc_bmc', 12, 'PASS', '', {}),
             ('admission_b_bounded', sparse, 'abc_bmc', 16, 'PASS', '', {}),
             ('control_miss_okay', miss_okay, 'abc_bmc', 16, 'FAIL', ('ADMISSION_B',), {}),
             ('control_second_r', full, 'abc_bmc', 16, 'FAIL', ('ADMISSION_R',), second_r),
             ('control_second_b', full, 'abc_bmc', 16, 'FAIL', ('ADMISSION_B',), second_b)]
    rows = []
    for name, rtl, mode, depth, want, expect_failed, setup in cases:
        case = work / name
        case.mkdir()
        (case / 'design.v').write_text(rtl)
        (case / 'formal_top.sv').write_text(harness(ports, **setup))
        options = f'mode {mode}\n' if mode != 'abc_bmc' else f'mode bmc\naigsmt {args.solver}\n'
        (case / 'task.sby').write_text(
            f'[options]\n{options}' + (f'depth {depth}\n' if depth else '') +
            f'\n[engines]\n{engine[mode]}\n\n[script]\nread_verilog design.v\n'
            'read_verilog -formal -sv formal_top.sv\nprep -top formal_top\n\n'
            '[files]\ndesign.v\nformal_top.sv\n')
        proc = subprocess.run(['sby', '-f', 'task.sby'], cwd=case, capture_output=True, text=True,
                              timeout=3500, check=False)
        (case / 'sby.log').write_text(proc.stdout + proc.stderr)
        status = (case / 'task' / 'status').read_text().split()[0] \
            if (case / 'task' / 'status').exists() else 'MISSING'
        failed = sorted(set(re.findall(r'failed assertion formal_top\.(\w+)', proc.stdout)))
        # The engine log names every reached cover; the summary lists only five.
        reached = sorted(set(re.findall(r'Reached cover statement in step \d+ at formal_top: (\w+)',
                                        proc.stdout)))
        ok = status == want and (name != 'cover' or reached == [
            'DENIED_B', 'DENIED_R', 'REACH_T0_AR', 'REACH_T0_AW', 'THREE_OPEN_AR', 'THREE_OPEN_AW',
            'TWO_OPEN_AR', 'TWO_OPEN_AW'])
        ok = ok and (want != 'FAIL' or bool(failed) and all(f.startswith(expect_failed)
                                                             for f in failed))
        rows.append({'case': name, 'mode': mode, 'status': status, 'expected': want,
                     'assume_read_helpers': bool(setup.get('assume_r')), 'failed': failed,
                     'reached': reached, 'ok': ok})
        (work / 'results.json').write_text(json.dumps(rows, indent=1) + '\n')
        assert ok, (rows[-1], proc.stdout[-3000:])
    print('FORMAL_PASS connectivity unconnected pair: unbounded NO_T1, DEPTH and ADMISSION_R'
          ' (read helpers, checked to depth 16), bounded ADMISSION_B, OKAY-fault,'
          ' helper and port-pipe controls')


if __name__ == '__main__':
    shutil.which('sby') or exit('sby not found')
    main()
