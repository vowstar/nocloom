"""AXI4 sources with an AxQOS level map, a shaper or a scheduler, sharing one AXI4 target.

Three 32-bit AXI4 sources issue reads and writes of 1..8 beats to target t0 with up to four bursts
in flight: s0 maps its AxQOS to levels (MAP), s1 uses a shaper (64 bytes per 1024 cycles,
burst 64, level 1) or a scheduler (260 bytes per 1024 cycles, burst 64, levels 3 and 0)
with one or four bursts in flight, and s2 uses the fixed level 2. A burst's address names its source, direction and number k,
and its AxQOS is QOS(k, write). In the wide variant s0 is 64 bits wide and issues 8-byte beats:
the 32-bit target receives each burst with twice as many words. Source s1 issues unaligned FIXED
and INCR bursts, and narrow INCR bursts that the network packs into two words. The checks use the source requests to
calculate charges. The target observations confirm delivery and packing. Each cycle the bench checks:
- each decision of the target's switch output serves a valid candidate of the highest level among
  the valid ones (s0 at MAP[AxQOS] of its next burst in the direction of its request, s1 at 1, s2
  at 2; shaper variant), s0's request carries that level whenever it is valid at a decision, and
  s0's decisions carry MAP[AxQOS] of its bursts in the order the target receives them;
- the shaper counter, in 1/1024 bytes, follows next = max(0, A - 64 + q), q = bytes * 1024 of
  the burst entering, and a burst enters only while A / 1024 <= 64;
- the scheduler counter follows the capped step with q of the burst whose last response left the
  cycle before; with four in flight, each charge is that of a burst of s1 whose last R or B leaves
  its face within four cycles of the charge, every burst charged once;
- gated and ungated clocks (test low and high) give the same rows.
"""
import json
import random
import re
from collections import deque
from pathlib import Path

MAP = [0, 0, 1, 1, 2, 2, 3, 3]
SHAPER_BUDGET = dict(bytes_per_1024_cycles=64, burst_bytes=64)
SCHEDULER_BUDGET = dict(bytes_per_1024_cycles=260, burst_bytes=64)
SCHEDULE = dict(under_level=3, over_level=0, threshold_percent=50)
BURSTS = 120


def qos(k, write):
    return (3 * k + write) % 8


def config(use_scheduler, axqos_map=MAP, shaper_budget=SHAPER_BUDGET,
           scheduler_budget=SCHEDULER_BUDGET, outstanding=1, s0_bits=32):
    source = lambda name, width=32, **q: {'name': name, 'clock_domain': 'main', 'protocol': 'axi4',
                                          'data_width': width, 'id_width': 2, 'len_width': 8,
                                          'max_outstanding': 4, **q}
    s1 = (source('s1', max_outstanding=outstanding,
                 qos={'shaper': scheduler_budget, 'scheduler': SCHEDULE}) if use_scheduler else
          source('s1', qos={'levels': {'read': 1, 'write': 1}, 'shaper': shaper_budget}))
    return {
        'schema_version': 10, 'name': 'qos_axi', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [source('s0', s0_bits, qos={'axqos_map': axqos_map}), s1,
                       source('s2', qos={'levels': {'read': 2, 'write': 2}})],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'len_width': 8, 'attributes': 'amba', 'max_outstanding': 2,
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]}],
        'connectivity': 'all', 'arbitration': {'policy': 'round_robin'},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


def write_config(path, use_scheduler, **kw):
    Path(path).write_text(json.dumps(config(use_scheduler, **kw), indent=1) + '\n')


def bursts(seed=7):
    """Per source and direction: address, AxLEN, AxID, AxQOS and pause."""
    rng = random.Random(seed)
    out = {}
    for s in range(3):
        for w in (0, 1):
            out[s, w] = []
            for k in range(BURSTS):
                a = 0x4000 * s + 0x2000 * w + 0x20 * k
                length = rng.choice([0, 1, 3, 7, 5, 2])
                if s == 1 and k % 8 == 1:
                    a, length = a + 1, 3  # unaligned FIXED, 12 bytes
                if s == 1 and k % 8 == 2:
                    length = 2  # aligned narrow INCR, 6 bytes in two packed words
                if s == 1 and k % 8 == 3:
                    a += 1  # unaligned INCR
                out[s, w].append((a, length, rng.randrange(4), qos(k, w),
                                  rng.choice([0, 0, 0, 1, 3])))
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
reg [31:0] araddr[0:2], awaddr[0:2];
reg [63:0] wdata[0:2];
reg [7:0] wstrb[0:2];
reg [7:0] arlen[0:2], awlen[0:2];
reg [1:0] arid[0:2], awid[0:2];
reg [2:0] arqos[0:2], awqos[0:2];
wire [2:0] size_r = araddr[1][7:5] == 3'd2 ? 3'd1 : 3'd2;
wire [2:0] size_w = awaddr[1][7:5] == 3'd2 ? 3'd1 : 3'd2;
wire [1:0] burst_r = araddr[1][7:5] == 3'd1 ? 2'd0 : 2'd1;
wire [1:0] burst_w = awaddr[1][7:5] == 3'd1 ? 2'd0 : 2'd1;
reg [2:0] arvalid=0, awvalid=0, wvalid=0, wlast=0;
wire [2:0] arready, awready, wready, bvalid, rvalid, rlast;
wire [1:0] bid[0:2], bresp[0:2], rid[0:2], rresp[0:2];
wire [63:0] rdata[0:2];
wire [31:0] t_araddr, t_awaddr, t_wdata;
wire [2:0] t_arsize, t_awsize;
wire [3:0] t_arid, t_awid; wire [7:0] t_arlen, t_awlen;
wire t_arvalid, t_awvalid, t_wvalid, t_wlast, t_bready, t_rready;
reg t_arready=0, t_awready=0, t_wready=0, t_bvalid=0, t_rvalid=0, t_rlast=0;
reg [3:0] t_bid=0, t_rid=0; reg [31:0] t_rdata=0;
nocloom_qos_axi dut(.clk(clk), .reset_n(reset_n), .test(test),
@PORTS@);
integer k;
initial begin
  for(k=0;k<3;k=k+1) begin araddr[k]=0; awaddr[k]=0; wdata[k]=0; wstrb[k]=0; arlen[k]=0; awlen[k]=0;
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
// The byte lanes of a beat of `bytes` bytes on a face of `word` bytes.
function automatic [7:0] lanes(input [31:0] start, input integer beat,
                                input integer bytes, input bit fixed_addr, input integer word);
  integer lo, hi;
  begin
    lo = (fixed_addr || beat==0) ? start : ((start & ~(bytes-1)) + beat*bytes);
    lo = lo & (word-1);
    hi = lo | (bytes-1);
    lanes = (8'hff << lo) & (8'hff >> (7-hi));
  end
endfunction
task automatic drive_w(input integer s);
integer b, bytes; bit fixed_addr; reg [39:0] e;
begin
  forever begin
    @(negedge clk);
    if(wq_r[s] < wq_w[s]) begin
      e = wq[s][wq_r[s]%256];
      bytes = (s==1 && e[7:5]==3'd2) ? 2 : s==0 ? @S0_BYTES@ : 4;
      fixed_addr = s==1 && e[7:5]==3'd1;
      for(b=0;b<=e[39:32];b=b+1) begin
        wstrb[s] = lanes(e[31:0], b, bytes, fixed_addr, s==0 ? @S0_BYTES@ : 4);
        wdata[s] = {2{(e[31:0] + 32'd4*b) ^ 32'h5a000000}}; wlast[s] = (b==e[39:32]); wvalid[s]=1;
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
integer s_i, i, lane;
reg [39:0] read_q[0:2][0:3][0:127];
integer read_head[0:2][0:3], read_tail[0:2][0:3], read_beat[0:2][0:3];
reg [39:0] expected_read;
reg [3:0] read_lanes;
initial for (integer s=0; s<3; s=s+1)
  for (integer id=0; id<4; id=id+1) begin
    read_head[s][id]=0; read_tail[s][id]=0; read_beat[s][id]=0;
  end
always @(posedge clk) if(reset_n) for(s_i=0;s_i<3;s_i=s_i+1) begin
  if(arvalid[s_i] && arready[s_i]) begin
    i = arid[s_i];
    read_q[s_i][i][read_tail[s_i][i]%128] = {arlen[s_i], araddr[s_i]};
    read_tail[s_i][i] = read_tail[s_i][i]+1;
  end
  if(rvalid[s_i]) begin
    i = rid[s_i];
    expected_read = read_q[s_i][i][read_head[s_i][i]%128];
    if(read_head[s_i][i]>=read_tail[s_i][i] || rresp[s_i]!==2'b00 ||
       rlast[s_i] !== (read_beat[s_i][i]==expected_read[39:32])) begin
      $display("ASSERT_FAIL:QOS_AXI.READ source=%0d", s_i); $fatal(1); end
    read_lanes = lanes(expected_read[31:0], read_beat[s_i][i],
                       s_i==1 && expected_read[7:5]==3'd2 ? 2 : s_i==0 ? @S0_BYTES@ : 4,
                       s_i==1 && expected_read[7:5]==3'd1, s_i==0 ? @S0_BYTES@ : 4);
    for(lane=0;lane<8;lane=lane+1)
      if(read_lanes[lane] && rdata[s_i][8*lane+:8] !== 8'ha5) begin
        $display("ASSERT_FAIL:QOS_AXI.READ_DATA source=%0d lane=%0d", s_i, lane); $fatal(1); end
    if(rlast[s_i]) begin
      rdone[s_i]=rdone[s_i]+1;
      read_head[s_i][i]=read_head[s_i][i]+1;
      read_beat[s_i][i]=0;
    end else read_beat[s_i][i]=read_beat[s_i][i]+1;
  end
  if(bvalid[s_i]) begin
    if(bresp[s_i]!==2'b00) begin $display("ASSERT_FAIL:QOS_AXI.WRITE source=%0d", s_i); $fatal(1); end
    wdone[s_i]=wdone[s_i]+1;
  end
end
// The requests and completions of s1 at its port.
always @(posedge clk) if(reset_n) begin
  if(arvalid[1] && arready[1])
    $display("S 0 %h %0d %0d %0d %0d", araddr[1], arlen[1], size_r, burst_r, arid[1]);
  if(awvalid[1] && awready[1])
    $display("S 1 %h %0d %0d %0d %0d", awaddr[1], awlen[1], size_w, burst_w, awid[1]);
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
    $display("A %0d 0 %h %0d %0d", cyc, t_araddr[15:0], t_arlen, t_arsize); end
  if(t_awvalid && t_awready) begin awq[awq_w%256] = t_awid; awq_w=awq_w+1;
    $display("A %0d 1 %h %0d %0d", cyc, t_awaddr[15:0], t_awlen, t_awsize); end
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
      t_rdata <= 32'ha5a5a5a5;
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


def bench(gen, test, s0_bits=32):
    """The bench of the network generated in gen, with the test input at test and s0 of s0_bits."""
    m = json.loads((gen / 'nocloom_qos_axi.map.json').read_text())
    rtl = (gen / 'nocloom_qos_axi.v').read_text()
    num = lambda ref: ref.removeprefix('value/')
    net = lambda ref: 'dut.nl_internal_v' + num(ref)
    formal = m['network']['formal']['sources']
    store = {s['resource']: s for s in m['network']['storage']}
    grant = store['graph/req/req_node0>endpoint/t0']
    use_scheduler = 'source/s1/scheduler' in store
    q = store['source/s1/scheduler' if use_scheduler else 'source/s1/shaper']
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
            d = c[1]
            size = f'size_{d}' if s == 1 else f"3'd{(s0_bits if s == 0 else 32).bit_length() - 4}"
            burst = f'burst_{d}' if s == 1 else "2'b01"
            ports += [f'.{p}{c}addr({c}addr[{s}])', f'.{p}{c}burst({burst})',
                      f".{p}{c}cache(4'b0011)", f'.{p}{c}id({c}id[{s}])', f'.{p}{c}len({c}len[{s}])',
                      f".{p}{c}lock(1'b0)", f".{p}{c}prot(3'b000)", f'.{p}{c}ready({c}ready[{s}])',
                      f".{p}{c}size({size})", f'.{p}{c}valid({c}valid[{s}])']
            if s == 0:
                ports.append(f'.{p}{c}qos({c}qos[0])')
        bits = s0_bits if s == 0 else 32
        ports += [f'.{p}bid(bid[{s}])', f".{p}bready(1'b1)", f'.{p}bresp(bresp[{s}])',
                  f'.{p}bvalid(bvalid[{s}])', f'.{p}rdata(rdata[{s}][{bits - 1}:0])',
                  f'.{p}rid(rid[{s}])', f'.{p}rlast(rlast[{s}])', f".{p}rready(1'b1)",
                  f'.{p}rresp(rresp[{s}])', f'.{p}rvalid(rvalid[{s}])',
                  f'.{p}wdata(wdata[{s}][{bits - 1}:0])', f'.{p}wlast(wlast[{s}])',
                  f'.{p}wready(wready[{s}])', f'.{p}wstrb(wstrb[{s}][{bits // 8 - 1}:0])',
                  f'.{p}wvalid(wvalid[{s}])']
    t = 'nl_t_t0_'
    for c in ('ar', 'aw'):
        ports += [f'.{t}{c}addr(t_{c}addr)', f'.{t}{c}burst()', f'.{t}{c}cache()',
                  f'.{t}{c}id(t_{c}id)', f'.{t}{c}len(t_{c}len)', f'.{t}{c}lock()',
                  f'.{t}{c}prot()', f'.{t}{c}ready(t_{c}ready)', f'.{t}{c}size(t_{c}size)',
                  f'.{t}{c}valid(t_{c}valid)']
    ports += [f'.{t}bid(t_bid)', f'.{t}bready(t_bready)', f".{t}bresp(2'b00)",
              f'.{t}bvalid(t_bvalid)', f'.{t}rdata(t_rdata)', f'.{t}rid(t_rid)',
              f'.{t}rlast(t_rlast)', f'.{t}rready(t_rready)', f".{t}rresp(2'b00)",
              f'.{t}rvalid(t_rvalid)', f'.{t}wdata(t_wdata)', f'.{t}wlast(t_wlast)',
              f'.{t}wready(t_wready)', f'.{t}wstrb()', f'.{t}wvalid(t_wvalid)']
    decide = f"(({net(grant['acquire'])} | {net(grant['release'])}) & !dut.nl_internal_v{held})"
    return (TB.replace('@ROM@', '\n'.join(rom)).replace('@BURSTS@', str(BURSTS))
            .replace('@PORTS@', ',\n'.join(' ' + p for p in ports)).replace('@TEST@', str(test))
            .replace('@S0_BYTES@', str(s0_bits // 8))
            .replace('@V0@', net(formal['s0']['request_valid']))
            .replace('@V1@', net(formal['s1']['request_valid']))
            .replace('@V2@', net(formal['s2']['request_valid']))
            .replace('@W0@', net(formal['s0']['request_write']))
            .replace('@W1@', net(formal['s1']['request_write']))
            .replace('@L0@', net(formal['s0']['request_level']))
            .replace('@DECIDE@', decide).replace('@WINNER@', f'dut.nl_internal_v{win}')
            .replace('@COUNTER@', net(q['control'][0])).replace('@CHARGE@', net(q['acquire'])))


def check(log, use_scheduler, multi=False, s0_bits=32):
    """Check a simulation log and return its rows.

    Raise RuntimeError for incomplete observations and AssertionError for a violation.
    """
    rows, arrivals = [], {(s, w): [] for s in range(3) for w in (0, 1)}
    left, source = [], {0: {}, 1: {}}
    for line in log.splitlines():
        f = line.split()
        if f and f[0] == 'C':
            rows.append(tuple(int(x) for x in f[1:]))
        elif f and f[0] == 'D':
            left.append(tuple(int(x) for x in f[1:]))
        elif f and f[0] == 'S':
            w, a = int(f[1]), int(f[2], 16)
            assert a not in source[w], f'duplicate source request {w} {a:#x}'
            source[w][a] = tuple(map(int, f[3:]))
        elif f and f[0] == 'A':
            a = int(f[3], 16)
            arrivals[a >> 14, int(f[2])].append((int(f[1]), a, int(f[4]), int(f[5])))
    if not ('CHECK_PASS qos_axi' in log) or len(rows) <= 1000:
        raise RuntimeError('incomplete simulation')
    if any(nxt[0] != row[0] + 1 for row, nxt in zip(rows, rows[1:])):
        raise RuntimeError('nonconsecutive cycle observations')
    plan = bursts()
    for key, got in arrivals.items():
        assert [a for _, a, *_ in got] == [b[0] for b in plan[key]], f'bursts of {key} at the target'
    for w in (0, 1):
        assert list(source[w]) == [b[0] for b in plan[1, w]], f'source requests {w}'
        # A wide s0 burst reaches the target as twice as many target words.
        words = s0_bits // 32
        assert [(ln, sz) for _, _, ln, sz in arrivals[0, w]] == \
            [((b[1] + 1) * words - 1, 2) for b in plan[0, w]], f'target bursts of s0 direction {w}'
        packed = [(ln, sz) for _, a, ln, sz in arrivals[1, w] if (a >> 5) % 8 == 2]
        assert packed and set(packed) == {(1, 2)}, f'narrow requests of direction {w} were not packed'
    decided = {0: [], 1: []}
    contests = 0
    for c, v0, v1, v2, w0, w1, l0, dec, win, *_ in rows:
        if not dec:
            continue
        valid = [v0, v1, v2]
        assert valid[win], f'decision for an invalid candidate at cycle {c}'
        # s0's head is its next undecided burst of the request's direction.
        head = plan[0, w0][len(decided[w0])] if v0 and len(decided[w0]) < BURSTS else None
        if win == 0:
            decided[w0].append(l0)
        if not use_scheduler:
            if v0:
                assert head is not None and l0 == MAP[head[3]], f'level of s0 at cycle {c}'
            levels = [MAP[head[3]] if v0 else 0, 1, 2]
            top = max(levels[i] for i in range(3) if valid[i])
            contests += sum(valid) > 1
            assert levels[win] == top, f'a lower level won at cycle {c}'
    for w in (0, 1):
        want = [MAP[b[3]] for b in plan[0, w]]
        assert decided[w] == want, f'source 0 levels of direction {w} differ from its AxQOS map'
    assert use_scheduler or contests > 100, 'too few contests'
    # The shaper follows the accepted heads in each direction. The scheduler
    # follows the completions reported by the source.
    queues = {w: deque(request_charge(a, ln, sz, kind)
                       for a, (ln, sz, kind, _) in source[w].items()) for w in (0, 1)}
    total = sum(map(len, queues.values()))
    if multi:
        # The bursts of one direction and ID leave in issue order; each charge takes the earliest
        # unmatched burst that left within four cycles.
        by_id = {(w, i): deque(request_charge(a, ln, sz, kind)
                               for a, (ln, sz, kind, tag) in source[w].items() if tag == i)
                 for w in (0, 1) for i in range(4)}
        pending = [[c, by_id[w, i].popleft(), False] for c, w, i in left]
        assert len(pending) == total, 'bursts of s1 leaving its face'
    elif use_scheduler:
        completed = deque(queues[w].popleft() for _, w, _ in left)
        assert len(completed) == total, 'bursts of s1 leaving its face'
    k, waited = 0, 0
    p = SCHEDULER_BUDGET if use_scheduler else SHAPER_BUDGET
    sat = p['burst_bytes']
    for row, nxt in zip(rows, rows[1:]):
        a, q = row[9], row[10]
        add = 0
        if q and multi:
            match = next((e for e in pending if not e[2] and abs(e[0] - row[0]) <= 4), None)
            assert match is not None, f'a charge without a burst leaving at cycle {row[0]}'
            match[2], k = True, k + 1
            add = match[1]
        elif q:
            due = completed if use_scheduler else queues[row[5]]
            assert due, f'an extra charge at cycle {row[0]}'
            add, k = due.popleft(), k + 1
            if not use_scheduler:
                assert a // 1024 <= sat, f'a burst entered over the burst_bytes at cycle {row[0]}'
        x = a - p['bytes_per_1024_cycles'] + add
        if use_scheduler:
            want = 0 if x < 0 else (1024 * sat if x // 1024 > sat else x)
        else:
            want = max(0, x)
            waited += a // 1024 > sat
        assert nxt[9] == want, f'counter at cycle {row[0]}: {a} -> {nxt[9]}, charge {add}'
    assert k == total, f'{k} charges for {total} bursts'
    assert use_scheduler or waited > 100, 'the shaper never closed'
    return rows


def request_charge(address, length, size, burst):
    beat = 1 << size
    offset = address % beat
    return 1024 * ((length + 1) * beat - offset * (length + 1 if burst == 0 else 1))


def mutants():
    """Altered configurations the checks must reject: a map one level higher than the one the
    checks expect, a wide source whose requests all carry level 0, a faster shaper, a faster
    scheduler and a scheduler of four bursts in flight with a smaller burst_bytes."""
    return {'map_raised': (False, dict(axqos_map=[1, 1, 2, 2, 3, 3, 4, 4])),
            'map_wide_zero': (False, dict(s0_bits=64, axqos_map=[0] * 8)),
            'shaper_rate': (False, dict(shaper_budget=dict(SHAPER_BUDGET, bytes_per_1024_cycles=68))),
            'scheduler_rate': (True, dict(scheduler_budget=dict(SCHEDULER_BUDGET, bytes_per_1024_cycles=264))),
            'scheduler_four_burst_bytes': (
                True, dict(outstanding=4, scheduler_budget=dict(SCHEDULER_BUDGET, burst_bytes=48)))}
