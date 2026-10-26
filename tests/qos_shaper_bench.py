"""A rate-limited APB source competing with a lower-level peer for one APB target.

Two 32-bit APB4 sources read target t0 on a common 16-cycle issue slot, each holding one
transfer; s0 (read level 3) has a shaper of 260 bytes per 1024 cycles and burst 64, s1 has level 2 and no
limiter. s0 pauses in cycles 50000..50300; the target stalls every eleventh cycle. The checks:
the budget counter follows next = max(0, counter - rate + 16384 * charge) every cycle; s0's
request enters the network in the first cycle the budget admits it (counter / 16384 <=
burst_bytes / 16) once it has arrived; a request entering with the other one waiting is s0's;
s0's charged bandwidth over cycles 20000..100000 is within 1% of rate / 1024 bytes per cycle;
gated and ungated clocks (test low and high) give the same entry cycles.
"""
import json
from pathlib import Path

RATE, BURST_BYTES, LEVELS = 260, 64, (3, 2)
CONFIG = {
    'schema_version': 10, 'name': 'qos_shaper', 'address_width': 32,
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [
        {'name': 's0', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
         'qos': {'levels': {'read': LEVELS[0]},
                 'shaper': {'bytes_per_1024_cycles': RATE, 'burst_bytes': BURST_BYTES}}},
        {'name': 's1', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
         'qos': {'levels': {'read': LEVELS[1], 'write': LEVELS[1]}}}],
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
nocloom_qos_shaper dut(.clk(clk), .reset_n(reset_n), .test(test),
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
    $display("START %0d %0d %0d",cyc,id,seq[id]);
    @(negedge clk); ien[id]=1;
    waited=0;
    @(posedge clk);
    while(irdy[id] !== 1'b1 && waited<10000) begin
      waited=waited+1; @(posedge clk);
    end
    if(irdy[id] !== 1'b1 || ierr[id] !== 1'b0 || ird[id] !== (32'ha5000000 ^ ia[id])) begin
      $display("ASSERT_FAIL:QOS_SHAPER.RESPONSE source=%0d seq=%0d",id,seq[id]); $fatal(1);
    end
    @(negedge clk); ien[id]=0; isel[id]=0; seq[id]=seq[id]+1;
  end
  done[id]=1;
end
endtask
initial source(0);
initial source(1);
always @(posedge clk) if(reset_n) begin
  if(tsel[1] && ten[1]) begin $display("ASSERT_FAIL:QOS_SHAPER.TARGET1"); $fatal(1); end
  $display("C %0d %b %b %0d %b",cyc,@VALID0@,@VALID1@,@COUNTER@,@CHARGE@);
end
initial begin
  wait(done==2'b11); repeat(100) @(posedge clk);
  $display("CHECK_PASS qos_shaper %0d %0d",seq[0],seq[1]); $finish;
end
initial begin #300000000; $display("ASSERT_FAIL:QOS_SHAPER.TIMEOUT"); $fatal(1); end
endmodule
'''


def bench(gen, test):
    """The bench of the network generated in gen, with the test input at test."""
    m = json.loads((gen / 'nocloom_qos_shaper.map.json').read_text())
    net = lambda ref: 'dut.nl_internal_v' + ref.removeprefix('value/')
    formal = m['network']['formal']['sources']
    store = next(s for s in m['network']['storage'] if s['resource'] == 'source/s0/limiter')
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
            .replace('@COUNTER@', net(store['control'][0])).replace('@CHARGE@', net(store['acquire'])))


def check(log):
    """Check a simulation log and return s0's entry cycles.

    Raise RuntimeError for incomplete observations and AssertionError for a violation.
    """
    rows, starts = [], {0: [], 1: []}
    for line in log.splitlines():
        f = line.split()
        if f and f[0] == 'C':
            rows.append((int(f[1]), int(f[2]), int(f[3]), int(f[4]), int(f[5])))
        elif f and f[0] == 'START':
            starts[int(f[2])].append(int(f[1]))
    if not ('CHECK_PASS qos_shaper' in log) or len(rows) <= 100000:
        raise RuntimeError('incomplete simulation')
    if any(nxt[0] != row[0] + 1 for row, nxt in zip(rows, rows[1:])):
        raise RuntimeError('nonconsecutive cycle observations')
    s = BURST_BYTES // 16
    entered = {0: [], 1: []}  # the cycle each request enters: the last of its valid run
    for (c, v0, v1, a, q), (c1, n0, n1, a1, _) in zip(rows, rows[1:]):
        assert a1 == max(0, a - RATE + 16384 * q), f'counter at cycle {c}: {a} -> {a1}, charge {q}'
        for src, v, n in ((0, v0, n0), (1, v1, n1)):
            if v and not n:
                entered[src].append(c)
        assert q == (v0 and not n0), f'charge differs from entry at cycle {c}'
        assert not q or a // 16384 <= s, f'admitted over the burst_bytes at cycle {c}'
        # A request entering while the other one waits is the higher-level one (s0).
        assert not (v0 and v1 and not n1 and n0), f'level 2 entered before level 3 at cycle {c}'
    counters = {c: a for c, _, _, a, _ in rows}
    first = {c for c, v0, *_ in rows if v0}
    rises = sorted(c for c in first if c - 1 not in first)
    # The arrival delay of a request that the budget does not hold: the first one's.
    delay = rises[0] - starts[0][0]
    for start, rise in zip(starts[0], rises):
        arrive = start + delay
        admit = next((c for c in range(arrive, rise + 1) if counters[c] // 16384 <= s), None)
        assert rise == admit, f'request from cycle {start} entered at {rise}, admissible at {admit}'
    assert len(rises) == len(starts[0]) == len(entered[0])
    window = [c for c in entered[0] if 20000 <= c < 100000]
    measured, expected = 16 * len(window), RATE / 1024 * 80000
    assert abs(measured - expected) / expected < 0.01, f'charged {measured}, expected {expected}'
    return entered[0], entered[1]


def mutants(rtl):
    """Altered limiters the checks must reject: an exact byte burst_bytes and a slower drain."""
    admit = f"= 27'd{16384 * (BURST_BYTES // 16 + 1)};"
    drain = f"= 27'd{RATE};"
    assert rtl.count(admit) == 1 and rtl.count(drain) == 1
    return {'exact_burst_bytes': rtl.replace(admit, f"= 27'd{16384 * (BURST_BYTES // 16) + 1};"),
            'slow_drain': rtl.replace(drain, f"= 27'd{RATE - 1};")}


def write_config(path):
    Path(path).write_text(json.dumps(CONFIG))


def set_shaper(rate, burst):
    """Another shaper for the same bench and checks."""
    global RATE, BURST_BYTES
    RATE, BURST_BYTES = rate, burst
    CONFIG['initiators'][0]['qos']['shaper'] = {'bytes_per_1024_cycles': rate, 'burst_bytes': burst}
