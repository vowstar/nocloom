"""AXI4 sources with an AxQOS level map, a rate limiter or a regulator, sharing one AXI4 target.

Three 32-bit AXI4 sources issue reads and writes of 1..8 beats to target t0 with up to four bursts
in flight: s0 maps its AxQOS to levels (MAP), s1 has a shaper (64 bytes per 1024 cycles, burst 64, levels 1)
or, in the regulated variants, a scheduler (260 bytes per 1024 cycles, burst 64, levels 3 and 0) with one or four
bursts in flight, and s2 the fixed level 2. A burst's address names its source, direction and number k,
and its AxQOS is QOS(k, write). The checks, every cycle:
- each decision of the target's switch output serves a valid candidate of the highest level among
  the valid ones (s0 at the level of its request, s1 at 1, s2 at 2; limiter variant), and s0's
  decisions carry MAP[AxQOS] of its bursts in the order the target receives them;
- the limiter counter follows next = max(0, A - 64 + q), q = ceil(bytes / 16) * 16384 of the
  burst entering, and a burst enters only while A / 16384 <= 4;
- the regulator counter follows the capped step with q of the burst whose last response left the
  cycle before; with four in flight, each charge is that of a burst of s1 whose last R or B leaves
  its face within four cycles of the charge, every burst charged once;
- gated and ungated clocks (test low and high) give the same rows.
"""
import json
import random
import re
from pathlib import Path

MAP = [0, 0, 1, 1, 2, 2, 3, 3]
LIMIT = dict(bytes_per_1024_cycles=64, burst_bytes=64)
REGULATE = dict(bytes_per_1024_cycles=260, burst_bytes=64)
SCHEDULE = dict(under_level=3, over_level=0, threshold_percent=50)
BURSTS = 120


def qos(k, write):
    return (3 * k + write) % 8


def config(regulated, axqos_map=MAP, limiter=LIMIT, regulator=REGULATE, outstanding=1):
    source = lambda name, **q: {'name': name, 'clock_domain': 'main', 'protocol': 'axi4',
                                'data_width': 32, 'id_width': 2, 'len_width': 8,
                                'max_outstanding': 4, **q}
    s1 = (source('s1', max_outstanding=outstanding, qos={'shaper': regulator, 'scheduler': SCHEDULE}) if regulated else
          source('s1', qos={'levels': {'read': 1, 'write': 1}, 'shaper': limiter}))
    return {
        'schema_version': 10, 'name': 'qos_axi', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [source('s0', qos={'axqos_map': axqos_map}), s1,
                       source('s2', qos={'levels': {'read': 2, 'write': 2}})],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'len_width': 8, 'attributes': 'amba', 'max_outstanding': 2,
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]}],
        'connectivity': 'all', 'arbitration': {'policy': 'round_robin'},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


def write_config(path, regulated, **kw):
    Path(path).write_text(json.dumps(config(regulated, **kw), indent=1) + '\n')


def bursts(seed=7):
    """Per source and direction: (address, AxLEN, AxID, AxQOS, pause before)."""
    rng = random.Random(seed)
    out = {}
    for s in range(3):
        for w in (0, 1):
            out[s, w] = [(0x4000 * s + 0x2000 * w + 0x20 * k, rng.choice([0, 1, 3, 7, 5, 2]),
                          rng.randrange(4), qos(k, w), rng.choice([0, 0, 0, 1, 3]))
                         for k in range(BURSTS)]
    return out


TB = r'''`timescale 1ps/1ps
module tb;
reg clk=0, reset_n=0, test=0;
always #625 clk=~clk;
integer cyc=0;
always @(posedge clk) cyc <= cyc+1;
reg [47:0] ROM[0:767];
initial begin
@ROM@
end
reg [31:0] araddr[0:2], awaddr[0:2], wdata[0:2];
reg [7:0] arlen[0:2], awlen[0:2];
reg [1:0] arid[0:2], awid[0:2];
reg [2:0] arqos[0:2], awqos[0:2];
reg [2:0] arvalid=0, awvalid=0, wvalid=0, wlast=0;
wire [2:0] arready, awready, wready, bvalid, rvalid, rlast;
wire [1:0] bid[0:2], bresp[0:2], rid[0:2], rresp[0:2];
wire [31:0] rdata[0:2];
wire [31:0] t_araddr, t_awaddr, t_wdata;
wire [3:0] t_arid, t_awid; wire [7:0] t_arlen, t_awlen;
wire t_arvalid, t_awvalid, t_wvalid, t_wlast, t_bready, t_rready;
reg t_arready=0, t_awready=0, t_wready=0, t_bvalid=0, t_rvalid=0, t_rlast=0;
reg [3:0] t_bid=0, t_rid=0; reg [31:0] t_rdata=0;
nocloom_qos_axi dut(.clk(clk), .reset_n(reset_n), .test(test),
@PORTS@);
integer k;
initial begin
  for(k=0;k<3;k=k+1) begin araddr[k]=0; awaddr[k]=0; wdata[k]=0; arlen[k]=0; awlen[k]=0;
    arid[k]=0; awid[k]=0; arqos[k]=0; awqos[k]=0; end
  #10; test=@TEST@;
  repeat(20) @(negedge clk); reset_n=1;
end
reg [39:0] wq[0:2][0:255]; integer wq_w[0:2], wq_r[0:2], rdone[0:2], wdone[0:2];
reg [5:0] fin=0;
initial for(k=0;k<3;k=k+1) begin wq_w[k]=0; wq_r[k]=0; rdone[k]=0; wdone[k]=0; end
task automatic issue(input integer s, input integer w);
integer n; reg [47:0] e;
begin
  wait(cyc>=24 && reset_n==1); repeat(16) @(negedge clk);
  for(n=0;n<@BURSTS@;n=n+1) begin
    e = ROM[(2*s+w)*128+n];
    repeat(e[47:44]) @(negedge clk);
    if(w) begin
      awaddr[s]=e[31:0]; awlen[s]=e[39:32]; awid[s]=e[41:40]; awqos[s]=e[44:42]; awvalid[s]=1;
      wq[s][wq_w[s]%256] = e[39:0]; wq_w[s]=wq_w[s]+1;
      @(posedge clk); while(awready[s]!==1'b1) @(posedge clk);
      @(negedge clk); awvalid[s]=0;
    end else begin
      araddr[s]=e[31:0]; arlen[s]=e[39:32]; arid[s]=e[41:40]; arqos[s]=e[44:42]; arvalid[s]=1;
      @(posedge clk); while(arready[s]!==1'b1) @(posedge clk);
      @(negedge clk); arvalid[s]=0;
    end
  end
  fin[2*s+w]=1;
end
endtask
task automatic drive_w(input integer s);
integer b; reg [39:0] e;
begin
  forever begin
    @(negedge clk);
    if(wq_r[s] < wq_w[s]) begin
      e = wq[s][wq_r[s]%256];
      for(b=0;b<=e[39:32];b=b+1) begin
        wdata[s] = (e[31:0] + 4*b) ^ 32'h5a000000; wlast[s] = (b==e[39:32]); wvalid[s]=1;
        @(posedge clk); while(wready[s]!==1'b1) @(posedge clk);
        @(negedge clk); wvalid[s]=0; wlast[s]=0;
      end
      wq_r[s]=wq_r[s]+1;
    end
  end
end
endtask
initial issue(0,0); initial issue(0,1); initial issue(1,0); initial issue(1,1);
initial issue(2,0); initial issue(2,1);
initial drive_w(0); initial drive_w(1); initial drive_w(2);
integer s_i;
always @(posedge clk) if(reset_n) for(s_i=0;s_i<3;s_i=s_i+1) begin
  if(rvalid[s_i]) begin
    if(rresp[s_i]!==2'b00 || rdata[s_i][31:24]!==8'ha5) begin
      $display("ASSERT_FAIL:QOS_AXI.READ source=%0d", s_i); $fatal(1); end
    if(rlast[s_i]) rdone[s_i]=rdone[s_i]+1;
  end
  if(bvalid[s_i]) begin
    if(bresp[s_i]!==2'b00) begin $display("ASSERT_FAIL:QOS_AXI.WRITE source=%0d", s_i); $fatal(1); end
    wdone[s_i]=wdone[s_i]+1;
  end
end
// The bursts of s1 leaving its face: cycle, direction, ID.
always @(posedge clk) if(reset_n) begin
  if(rvalid[1] && rlast[1]) $display("D %0d 0 %0d", cyc, rid[1]);
  if(bvalid[1]) $display("D %0d 1 %0d", cyc, bid[1]);
end
// The target: AR, AW and W with periodic stalls; reads answer in order after two cycles.
reg [43:0] rq[0:255]; integer rq_w=0, rq_r=0, rbeat=0, rlat=0;
reg [3:0] bq[0:255]; reg [3:0] awq[0:255]; integer bq_w=0, bq_r=0, awq_w=0, awq_r=0, wl=0;
always @(posedge clk) begin
  t_arready <= (cyc%7 != 3); t_awready <= (cyc%9 != 5); t_wready <= (cyc%5 != 2);
end
always @(posedge clk) if(reset_n) begin
  if(t_arvalid && t_arready) begin rq[rq_w%256] = {t_arid, t_arlen, t_araddr}; rq_w=rq_w+1;
    $display("A %0d 0 %h %0d", cyc, t_araddr[15:0], t_arlen); end
  if(t_awvalid && t_awready) begin awq[awq_w%256] = t_awid; awq_w=awq_w+1;
    $display("A %0d 1 %h %0d", cyc, t_awaddr[15:0], t_awlen); end
  if(t_wvalid && t_wready && t_wlast) wl=wl+1;
  if(awq_r < awq_w && awq_r < wl) begin bq[bq_w%256] = awq[awq_r%256]; bq_w=bq_w+1; awq_r=awq_r+1; end
  if(t_bvalid && t_bready) t_bvalid <= 0;
  if((!t_bvalid || t_bready) && bq_r < bq_w) begin t_bvalid <= 1; t_bid <= bq[bq_r%256]; bq_r=bq_r+1; end
  if(t_rvalid && t_rready) begin
    if(t_rlast) begin t_rvalid <= 0; rq_r=rq_r+1; rbeat=0; rlat=0; end else rbeat=rbeat+1;
  end
  if(rq_r < rq_w && !(t_rvalid && t_rready && t_rlast)) begin
    if(rlat < 2) rlat=rlat+1;
    else begin
      t_rvalid <= 1; t_rid <= rq[rq_r%256][43:40]; t_rlast <= (rbeat == rq[rq_r%256][39:32]);
      t_rdata <= 32'ha5000000 | ((rq[rq_r%256][15:0] + 4*rbeat) & 16'hffff);
    end
  end
end
always @(posedge clk) if(reset_n)
  $display("C %0d %b %b %b %b %b %0d %b %0d %0d %b",cyc,@V0@,@V1@,@V2@,@W0@,@W1@,@L0@,
           @DECIDE@,@WINNER@,@COUNTER@,@CHARGE@);
initial begin
  wait(fin==6'b111111);
  for(k=0;k<3;k=k+1) wait(rdone[k] >= @BURSTS@ && wdone[k] >= @BURSTS@);
  repeat(50) @(posedge clk);
  $display("CHECK_PASS qos_axi %0d", cyc); $finish;
end
initial begin #400000000; $display("ASSERT_FAIL:QOS_AXI.TIMEOUT"); $fatal(1); end
endmodule
'''


def bench(gen, test):
    """The bench of the network generated in gen, with the test input at test."""
    m = json.loads((gen / 'nocloom_qos_axi.map.json').read_text())
    rtl = (gen / 'nocloom_qos_axi.v').read_text()
    num = lambda ref: ref.removeprefix('value/')
    net = lambda ref: 'dut.nl_internal_v' + num(ref)
    formal = m['network']['formal']['sources']
    store = {s['resource']: s for s in m['network']['storage']}
    grant = store['graph/req/req_node0>endpoint/t0']
    regulated = 'source/s1/regulator' in store
    q = store['source/s1/regulator' if regulated else 'source/s1/limiter']
    # The arbiter's choice: the owner register loads it in an unheld cycle.
    owner, held = num(grant['control'][-2]), num(grant['control'][-1])
    nxt = re.search(r'nl_internal_v%s <= nl_internal_v(\d+);' % owner, rtl).group(1)
    sel = re.search(r'assign nl_internal_v%s = \(nl_internal_v\d+ \? nl_internal_v(\d+) : '
                    r'nl_internal_v%s\);' % (nxt, owner), rtl).group(1)
    win = re.search(r'assign nl_internal_v%s = \(nl_internal_v%s \? nl_internal_v%s : '
                    r'nl_internal_v(\d+)\);' % (sel, held, owner), rtl).group(1)
    rom = []
    for (s, w), items in sorted(bursts().items()):
        for n, (a, ln, i, qv, p) in enumerate(items):
            rom.append(f"  ROM[{(2 * s + w) * 128 + n}] = {{4'd{p}, 3'd{qv}, 2'd{i}, 8'd{ln}, "
                       f"32'h{a:08x}}};")
    ports = []
    for s in range(3):
        p = f'nl_i_s{s}_'
        for c in ('ar', 'aw'):
            ports += [f'.{p}{c}addr({c}addr[{s}])', f".{p}{c}burst(2'b01)",
                      f".{p}{c}cache(4'b0011)", f'.{p}{c}id({c}id[{s}])', f'.{p}{c}len({c}len[{s}])',
                      f".{p}{c}lock(1'b0)", f".{p}{c}prot(3'b000)", f'.{p}{c}ready({c}ready[{s}])',
                      f".{p}{c}size(3'd2)", f'.{p}{c}valid({c}valid[{s}])']
            if s == 0:
                ports.append(f'.{p}{c}qos({c}qos[0])')
        ports += [f'.{p}bid(bid[{s}])', f".{p}bready(1'b1)", f'.{p}bresp(bresp[{s}])',
                  f'.{p}bvalid(bvalid[{s}])', f'.{p}rdata(rdata[{s}])', f'.{p}rid(rid[{s}])',
                  f'.{p}rlast(rlast[{s}])', f".{p}rready(1'b1)", f'.{p}rresp(rresp[{s}])',
                  f'.{p}rvalid(rvalid[{s}])', f'.{p}wdata(wdata[{s}])', f'.{p}wlast(wlast[{s}])',
                  f'.{p}wready(wready[{s}])', f".{p}wstrb(4'hf)", f'.{p}wvalid(wvalid[{s}])']
    t = 'nl_t_t0_'
    for c in ('ar', 'aw'):
        ports += [f'.{t}{c}addr(t_{c}addr)', f'.{t}{c}burst()', f'.{t}{c}cache()',
                  f'.{t}{c}id(t_{c}id)', f'.{t}{c}len(t_{c}len)', f'.{t}{c}lock()',
                  f'.{t}{c}prot()', f'.{t}{c}ready(t_{c}ready)', f'.{t}{c}size()',
                  f'.{t}{c}valid(t_{c}valid)']
    ports += [f'.{t}bid(t_bid)', f'.{t}bready(t_bready)', f".{t}bresp(2'b00)",
              f'.{t}bvalid(t_bvalid)', f'.{t}rdata(t_rdata)', f'.{t}rid(t_rid)',
              f'.{t}rlast(t_rlast)', f'.{t}rready(t_rready)', f".{t}rresp(2'b00)",
              f'.{t}rvalid(t_rvalid)', f'.{t}wdata(t_wdata)', f'.{t}wlast(t_wlast)',
              f'.{t}wready(t_wready)', f'.{t}wstrb()', f'.{t}wvalid(t_wvalid)']
    decide = f"(({net(grant['acquire'])} | {net(grant['release'])}) & !dut.nl_internal_v{held})"
    return (TB.replace('@ROM@', '\n'.join(rom)).replace('@BURSTS@', str(BURSTS))
            .replace('@PORTS@', ',\n'.join(' ' + p for p in ports)).replace('@TEST@', str(test))
            .replace('@V0@', net(formal['s0']['request_valid']))
            .replace('@V1@', net(formal['s1']['request_valid']))
            .replace('@V2@', net(formal['s2']['request_valid']))
            .replace('@W0@', net(formal['s0']['request_write']))
            .replace('@W1@', net(formal['s1']['request_write']))
            .replace('@L0@', net(formal['s0']['request_level']))
            .replace('@DECIDE@', decide).replace('@WINNER@', f'dut.nl_internal_v{win}')
            .replace('@COUNTER@', net(q['control'][0])).replace('@CHARGE@', net(q['acquire'])))


def check(log, regulated, multi=False):
    """Check a simulation log and return its rows.

    Raise RuntimeError for incomplete observations and AssertionError for a violation.
    """
    rows, arrivals = [], {(s, w): [] for s in range(3) for w in (0, 1)}
    left = []
    for line in log.splitlines():
        f = line.split()
        if f and f[0] == 'C':
            rows.append(tuple(int(x) for x in f[1:]))
        elif f and f[0] == 'D':
            left.append(tuple(int(x) for x in f[1:]))
        elif f and f[0] == 'A':
            a = int(f[3], 16)
            arrivals[a >> 14, int(f[2])].append((int(f[1]), a, int(f[4])))
    if not ('CHECK_PASS qos_axi' in log) or len(rows) <= 1000:
        raise RuntimeError('incomplete simulation')
    if any(nxt[0] != row[0] + 1 for row, nxt in zip(rows, rows[1:])):
        raise RuntimeError('nonconsecutive cycle observations')
    plan = bursts()
    for key, got in arrivals.items():
        assert [a for _, a, _ in got] == [b[0] for b in plan[key]], f'bursts of {key} at the target'
    charge = lambda a, ln: -(-(ln + 1) * 4 // 16) * 16384
    decided = {0: [], 1: []}
    contests = 0
    for c, v0, v1, v2, w0, w1, l0, dec, win, *_ in rows:
        if not dec:
            continue
        valid = [v0, v1, v2]
        assert valid[win], f'decision for an invalid candidate at cycle {c}'
        if win == 0:
            decided[w0].append(l0)
        if not regulated:
            levels = [l0, 1, 2]
            top = max(levels[i] for i in range(3) if valid[i])
            contests += sum(valid) > 1
            assert levels[win] == top, f'a lower level won at cycle {c}'
    for w in (0, 1):
        want = [MAP[b[3]] for b in plan[0, w]]
        assert decided[w] == want, f'source 0 levels of direction {w} differ from its AxQOS map'
    assert regulated or contests > 100, 'too few contests'
    # The counter: the limiter charges a burst as its head enters, the regulator after its last
    # response; both take the bursts of s1 in arrival order.
    order = sorted(arrivals[1, 0] + arrivals[1, 1])
    charges = [charge(a, ln) for _, a, ln in order]
    if multi:
        # The bursts of one direction and ID leave in issue order; each charge takes the earliest
        # unmatched burst of its amount that left within four cycles.
        queues = {(w, i): [charge(0, b[1]) for b in plan[1, w] if b[2] == i]
                  for w in (0, 1) for i in range(4)}
        pending = [[c, queues[w, i].pop(0), False] for c, w, i in left]
        assert len(pending) == len(charges), 'bursts of s1 leaving its face'
    k, waited = 0, 0
    p = REGULATE if regulated else LIMIT
    sat = p['burst_bytes'] // 16
    for row, nxt in zip(rows, rows[1:]):
        a, q = row[9], row[10]
        add = 0
        if q and multi:
            match = next((e for e in pending if not e[2] and abs(e[0] - row[0]) <= 4), None)
            assert match is not None, f'a charge without a burst leaving at cycle {row[0]}'
            match[2], k = True, k + 1
            add = match[1]
        elif q:
            assert k < len(charges), f'an extra charge at cycle {row[0]}'
            add, k = charges[k], k + 1
            if not regulated:
                assert a // 16384 <= sat, f'a burst entered over the burst_bytes at cycle {row[0]}'
        x = a - p['bytes_per_1024_cycles'] + add
        if regulated:
            want = 0 if x < 0 else (16384 * sat if x // 16384 > sat else x)
        else:
            want = max(0, x)
            waited += a // 16384 > sat
        assert nxt[9] == want, f'counter at cycle {row[0]}: {a} -> {nxt[9]}, charge {add}'
    assert k == len(charges), f'{k} charges for {len(charges)} bursts'
    assert regulated or waited > 100, 'the limiter never closed'
    return rows


def mutants():
    """Altered configurations the checks must reject: a map one level higher than the one the
    checks expect, a faster limiter, a faster regulator and a regulator of four bursts in flight
    with a smaller burst_bytes."""
    return {'map_raised': (False, dict(axqos_map=[1, 1, 2, 2, 3, 3, 4, 4])),
            'limiter_rate': (False, dict(limiter=dict(LIMIT, bytes_per_1024_cycles=68))),
            'regulator_rate': (True, dict(regulator=dict(REGULATE, bytes_per_1024_cycles=264))),
            'regulator_four_burst_bytes': (True, dict(outstanding=4,
                                                     regulator=dict(REGULATE, burst_bytes=48)))}
