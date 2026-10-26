"""Completion charging, frozen request levels and accepted path upgrades on APB.

The target stalls every eleventh cycle. The initiators share bounded issue slots.
Checks use actual request and control handshakes, including empty control snapshots.
"""
import json
import re
from pathlib import Path

RATE, BURST_BYTES, HIGH, LOW, PEER = 260, 64, 3, 0, 2
PERCENT = 50
CONFIG = {
    'schema_version': 10, 'name': 'qos_scheduler', 'address_width': 32,
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [
        {'name': 's0', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
         'qos': {'shaper': {'bytes_per_1024_cycles': RATE, 'burst_bytes': BURST_BYTES},
                 'scheduler': {'under_level': HIGH, 'over_level': LOW, 'threshold_percent': 50}}},
        {'name': 's1', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
         'qos': {'levels': {'read': PEER, 'write': PEER}}}],
    'targets': [
        {'name': f't{i}', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
         'address_width': 16, 'attributes': 'amba',
         'mappings': [{'base': hex(0x1000 * i), 'size': '0x1000'}]} for i in range(2)],
    'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}

TB = r'''`timescale 1ps/1ps
module tb;
reg clk=0, reset_n=0, test=0;
always #625 clk=~clk;
integer cyc=0;
always @(posedge clk) cyc <= cyc+1;
reg [31:0] ia[0:1];
reg [1:0] isel=0, ien=0;
wire [31:0] ird[0:1];
wire [1:0] irdy, ierr;
wire [15:0] ta[0:1];
wire [1:0] tsel, ten, twrite;
wire [31:0] twd[0:1];
integer seq[0:1];
reg [1:0] done=0;
wire target_ready=(cyc%11 != 4);
nocloom_qos_scheduler dut(.clk(clk), .reset_n(reset_n), .test(test),
@PORTS@);
initial begin
  ia[0]=0; ia[1]=0; seq[0]=0; seq[1]=0;
  #10; test=@TEST@;
  repeat(20) @(negedge clk); reset_n=1;
end
task automatic source;
input integer id;
integer waited;
begin
  wait(cyc>=20 && reset_n==1); repeat(16) @(negedge clk);
  while(cyc<100000) begin
    if(id==0 && cyc>=50000 && cyc<50300)
      while(cyc<50300) @(negedge clk);
    while(cyc%16 != 0) @(negedge clk);
    ia[id]=(id*32'h800) | ((seq[id]%512)*4);
    isel[id]=1; ien[id]=0;
    @(negedge clk); ien[id]=1;
    waited=0;
    @(posedge clk);
    while(irdy[id] !== 1'b1 && waited<10000) begin
      waited=waited+1; @(posedge clk);
    end
    if(irdy[id] !== 1'b1 || ierr[id] !== 1'b0 || ird[id] !== (32'ha5000000 ^ ia[id])) begin
      $display("ASSERT_FAIL:QOS_SCHEDULER.RESPONSE source=%0d seq=%0d",id,seq[id]); $fatal(1);
    end
    @(negedge clk); ien[id]=0; isel[id]=0; seq[id]=seq[id]+1;
  end
  done[id]=1;
end
endtask
initial source(0);
initial source(1);
integer accesses=0;
always @(posedge clk) if(reset_n) begin
  if(tsel[1] && ten[1]) begin $display("ASSERT_FAIL:QOS_SCHEDULER.TARGET1"); $fatal(1); end
  if(tsel[0] && ten[0] && target_ready) accesses = accesses + 1;
  $display("C %0d %b %b %b %0d %b %0d %0d %b %b %b",cyc,@VALID0@,@VALID1@,@RAISE@,@COUNTER@,@CHARGED@,
           @HELD@,@RAISE_LEVEL@,isel[0] & ien[0] & irdy[0],@READY0@,@READY1@);
end
initial begin
  wait(done==2'b11); repeat(100) @(posedge clk);
  $display("CHECK_PASS qos_scheduler %0d %0d %0d",seq[0],seq[1],accesses); $finish;
end
initial begin #300000000; $display("ASSERT_FAIL:QOS_SCHEDULER.TIMEOUT"); $fatal(1); end
endmodule
'''


def _store(gen):
    m = json.loads((gen / 'nocloom_qos_scheduler.map.json').read_text())
    return m, next(s for s in m['network']['storage'] if s['resource'] == 'source/s0/regulator')


def bench(gen, test):
    """The bench of the network generated in gen, with the test input at test."""
    m, store = _store(gen)
    net = lambda ref: 'dut.nl_internal_v' + ref.removeprefix('value/')
    formal = m['network']['formal']['sources']
    words = store['control']
    ports = []
    for i in range(2):
        p = f'nl_i_s{i}_'
        ports += [f'.{p}psel(isel[{i}])', f'.{p}penable(ien[{i}])', f".{p}pwrite(1'b0)",
                  f'.{p}paddr(ia[{i}])', f".{p}pwdata(32'b0)", f".{p}pstrb(4'b1111)",
                  f".{p}pprot(3'b000)", f'.{p}pready(irdy[{i}])', f'.{p}prdata(ird[{i}])',
                  f'.{p}pslverr(ierr[{i}])']
        p = f'nl_t_t{i}_'
        ports += [f'.{p}pready(target_ready)', f".{p}prdata(32'ha5000000 ^ {{16'b0,ta[{i}]}})",
                  f".{p}pslverr(1'b0)", f'.{p}paddr(ta[{i}])', f'.{p}pprot()',
                  f'.{p}pwrite(twrite[{i}])', f'.{p}psel(tsel[{i}])', f'.{p}penable(ten[{i}])',
                  f'.{p}pwdata(twd[{i}])', f'.{p}pstrb()']
    return (TB.replace('@PORTS@', ',\n'.join(' ' + p for p in ports)).replace('@TEST@', str(test))
            .replace('@VALID0@', net(formal['s0']['request_valid']))
            .replace('@VALID1@', net(formal['s1']['request_valid']))
            .replace('@RAISE@', net(formal['s0']['request_raise']))
            .replace('@COUNTER@', net(words[0])).replace('@CHARGED@', net(words[1]))
            .replace('@HELD@', net(formal['s0']['scheduler_held']))
            .replace('@RAISE_LEVEL@', net(formal['s0']['scheduler_level']))
            .replace('@READY0@', net(formal['s0']['request_ready']))
            .replace('@READY1@', net(formal['s1']['request_ready'])))


def check(log):
    """Checks a simulation log; returns the entry cycles of both sources and the raise cycles.
    Raises RuntimeError for incomplete observations and AssertionError for a violation."""
    rows = []
    for line in log.splitlines():
        f = line.split()
        if f and f[0] == 'C':
            rows.append(tuple(int(x) for x in f[1:]))
    passed = re.search(r'CHECK_PASS qos_scheduler (\d+) (\d+) (\d+)', log)
    if not (passed) or len(rows) <= 100000:
        raise RuntimeError('incomplete simulation')
    if any(nxt[0] != row[0] + 1 for row, nxt in zip(rows, rows[1:])):
        raise RuntimeError('nonconsecutive cycle observations')
    s = BURST_BYTES // 16
    level = lambda a: HIGH if a // 16384 <= s * PERCENT // 100 else LOW
    entered, raises, offered_at = {0: [], 1: []}, [], None
    in_flight, flight_level, contests, before = False, None, [], (0, 0)
    for row, nxt in zip(rows, rows[1:]):
        c, v0, v1, r, a, q, held, rl, pready, ready0, ready1 = row
        x = a - RATE + 16384 * q
        expect = 0 if x < 0 else (16384 * s if x // 16384 > s else x)
        assert nxt[4] == expect, f'counter at cycle {c}: {a} -> {nxt[4]}, charged {q}'
        assert q == pready, f'charge differs from the last response at cycle {c}'
        request = v0 and not r
        if request and offered_at is None:
            offered_at = level(a)
        # The request's level is loaded into the held word every cycle.
        if request:
            assert nxt[6] == offered_at, f'request level changed while offered at cycle {c}'
        for src, v, ready in ((0, request, ready0), (1, v1, ready1)):
            if v and ready:
                entered[src].append(c)
        # Both requests first offered in this cycle: the output decides here and holds the
        # decision, so the first to enter has the higher arbitration level now.
        if request and v1 and before == (0, 0):
            contests.append((c, max(offered_at, level(a)) > PEER))
        before = (request, v1)
        if request and ready0:
            in_flight, flight_level, offered_at = True, offered_at, None
        if r and ready0:
            assert rl == HIGH, f'invalid upgrade level at cycle {c}'
            raises.append(c)
            flight_level = rl
        if pready:
            in_flight = False
    assert PERCENT != 50 or raises, 'no upgrade accepted during sustained recovery'
    for c, first in contests:
        s0 = next(e for e in entered[0] if e >= c)
        s1 = next(e for e in entered[1] if e >= c)
        assert (s0 < s1) == first, f'the lower level entered first after cycle {c}'
    assert {first for _, first in contests} == {True, False}, 'both orders must occur'
    seq0, seq1, accesses = map(int, passed.groups())
    assert len(entered[0]) == seq0 and len(entered[1]) == seq1
    assert accesses == seq0 + seq1, 'a raise packet reached the target'
    return entered[0], entered[1], raises


def mutants(rtl, gen):
    """Altered regulators the checks must reject: a slower drain, an exact byte cap, a charge
    without the cycle of delay, a request level that follows the counter while offered and no
    raise packets."""
    mapping, store = _store(gen)
    anchors = mapping['network']['formal']['sources']['s0']
    name = lambda ref: 'nl_internal_v' + ref.removeprefix('value/')
    counter, charged, keep, offered = (name(anchors['scheduler_' + key])
                                       for key in ('counter', 'charged', 'keep', 'valid'))

    def one(pattern):
        found = re.findall(pattern, rtl, re.M)
        assert len(found) == 1, (pattern, found)
        return found[0]

    total = one(rf'^assign (nl_internal_v\d+) = \({charged} \? nl_internal_v\d+ : nl_internal_v\d+\);$')
    whole = one(rf'^assign (nl_internal_v\d+) = {total}\[26:14\];$')
    low = one(rf'^assign (nl_internal_v\d+) = {total}\[26:0\];$')
    over = one(rf'^assign (nl_internal_v\d+) = \(nl_internal_v\d+ < {whole}\);$')
    done = one(rf'^    {charged} <= (nl_internal_v\d+);$')
    drain = f"= 28'd{RATE};"
    assert rtl.count(drain) == 1
    sub = lambda pattern, text: re.sub(pattern, text, rtl, count=1, flags=re.M)
    return {'slow_drain': rtl.replace(drain, f"= 28'd{RATE - 1};"),
            'exact_cap': sub(rf'^assign {over} = .*$',
                             f"assign {over} = (27'd{16384 * (BURST_BYTES // 16)} < {low});"),
            'undelayed_charge': sub(rf'^assign {total} = \({charged} \?', f'assign {total} = ({done} ?'),
            'unkept_level': sub(rf'^    {keep} <= nl_internal_v\d+;$', f"    {keep} <= 1'd0;"),
            'no_raise': sub(rf'^    {offered} <= nl_internal_v\d+;$', f"    {offered} <= 1'd0;")}


def set_threshold(percent):
    """Another threshold for the same bench and checks."""
    global PERCENT
    PERCENT = percent
    CONFIG['initiators'][0]['qos']['scheduler']['threshold_percent'] = percent


def write_config(path):
    Path(path).write_text(json.dumps(CONFIG))
