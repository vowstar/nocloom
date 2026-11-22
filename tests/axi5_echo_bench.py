"""AXI5 response echoes end to end: the unique ID indicator and the loopback values return with
each response, with several transactions in flight per initiator, packet splitting, a clock
domain crossing and local decode errors. Targets see zero for the echoed request signals."""
import argparse
import json
import random
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

# Initiators: name -> (data bits, outstanding, axi5 object). Targets: name -> (protocol, data
# bits, outstanding, axi5 object).
SOURCES = {
    'm64': (64, 4, dict(unique_id=True, write_loop_width=3, read_loop_width=5)),
    'h32': (32, 1, dict(unique_id=True, write_loop_width=8, read_loop_width=1)),
    'w128': (128, 2, dict(write_loop_width=2, read_loop_width=2)),
}
TARGETS = {
    't64': ('axi5', 64, 4, dict(unique_id=True, write_loop_width=4, read_loop_width=4)),
    't32': ('axi5', 32, 2, None),
    'q128': ('axi4', 128, 1, None),
}
BASE = 0x40000
MISS = 0x800000  # no mapping holds it: the initiator answers DECERR
# A WRAP of three beats is not legal: the initiator answers SLVERR without a target access.


def configuration(mode):
    clocked = mode == 'clocked'
    domains = ['fast', 'slow'] if clocked else ['main']
    cfg = {
        'schema_version': 10, 'name': 'axi5_echo', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [], 'targets': [],
        'transport': {'data_width': 32,
                      'packet_limits': [{'source': 'm64', 'target': 't32', 'max_bytes': 16},
                                        {'source': 'w128', 'target': 't64', 'max_bytes': 32}]},
        'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    for name, (bits, outstanding, axi5) in SOURCES.items():
        cfg['initiators'].append({'name': name, 'clock_domain': domains[0], 'protocol': 'axi5',
                                  'data_width': bits, 'id_width': 2, 'len_width': 8,
                                  'max_outstanding': outstanding, 'axi5': axi5})
    for index, (name, (protocol, bits, outstanding, axi5)) in enumerate(TARGETS.items()):
        base = (index + 1) * BASE
        entry = {'name': name, 'clock_domain': domains[-1], 'protocol': protocol,
                 'data_width': bits, 'id_width': 4, 'len_width': 8, 'attributes': 'amba',
                 'max_outstanding': outstanding,
                 'mappings': [{'base': hex(base), 'size': hex(BASE), 'local_address': hex(base)}]}
        if axi5:
            entry['axi5'] = axi5
        cfg['targets'].append(entry)
    if clocked:
        def graph(entries, exits, src, dst):
            return {'nodes': [{'name': 'root', 'clock_domain': 'fast'},
                              {'name': 'leaf', 'clock_domain': 'slow'}],
                    'links': [{'from': src, 'to': dst}],
                    'entries': [{'endpoint': e, 'node': src} for e in entries],
                    'exits': [{'endpoint': e, 'node': dst} for e in exits]}
        cfg['topology'] = {'request': graph(SOURCES, TARGETS, 'root', 'leaf'),
                           'response': graph(TARGETS, SOURCES, 'leaf', 'root')}
    return cfg


def transactions(seed):
    """Per initiator: writes and reads over every target and the unmapped window, IDs mixed so
    that several are in flight, loop values walking one and zero bits, then random."""
    random.seed(seed)
    out = []
    slots = {t: 0 for t in TARGETS}
    for s, (bits, _, axi5) in SOURCES.items():
        beat = (bits // 8).bit_length() - 1
        for j in range(48):
            write = j % 2 == 0
            loop_bits = axi5.get('write_loop_width' if write else 'read_loop_width', 0)
            if j < 4 * max(loop_bits, 1):
                bit = 1 << ((j // 4) % max(loop_bits, 1))
                loop = bit if j % 4 < 2 else ((1 << loop_bits) - 1) ^ bit
            else:
                loop = random.getrandbits(max(loop_bits, 1))
            loop &= (1 << loop_bits) - 1
            choice = (j // 2) % 4
            if choice == 3:
                target, addr = None, MISS + j * 0x100
            else:
                target = list(TARGETS)[choice]
                addr = (list(TARGETS).index(target) + 1) * BASE + slots[target] * 0x200
                slots[target] += 1
            bad = target is not None and (j // 2) % 7 == 5
            out.append(dict(index=len(out), source=s, target=target, write=write, addr=addr,
                            len=2 if bad else random.choice((0, 1, 3, 7)), size=beat,
                            burst=2 if bad else 1, id=random.randrange(4), loop=loop,
                            unique=bool(axi5.get('unique_id')) and j % 3 != 1,
                            resp=3 if target is None else 2 if bad else 0))
    return out


def bench(cfg, ports, txs, seed, clocked):
    names = {p['name'] for p in ports}
    sclk, tclk = ('clk_fast', 'clk_slow') if clocked else ('clk_main', 'clk_main')
    clocks = ('always #5 clk_fast = ~clk_fast;\nalways #7 clk_slow = ~clk_slow;' if clocked
              else 'always #5 clk_main = ~clk_main;')
    n = len(txs)
    decl = [f"{'reg' if p['direction'] == 'input' else 'wire'} [{p['width'] - 1}:0] {p['name']};"
            for p in ports if not p['name'].startswith('clk_')]
    conn = ', '.join(f".{p['name']}({p['name']})" for p in ports)
    table = [f"ad[{t['index']}]=32'h{t['addr']:x}; ln[{t['index']}]={t['len']}; "
             f"lp[{t['index']}]={t['loop']}; uq[{t['index']}]={int(t['unique'])}; "
             f"ok[{t['index']}]={int(t['target'] is not None and t['resp'] == 0)}; "
             f"bt[{t['index']}]={t['burst']}; er[{t['index']}]={t['resp']};" for t in txs]
    drivers = []
    for si, (s, (bits, _, axi5)) in enumerate(SOURCES.items()):
        lanes = bits // 8
        p = f'nl_i_{s}_'
        wl, rl = axi5.get('write_loop_width', 0), axi5.get('read_loop_width', 0)
        unique = bool(axi5.get('unique_id'))
        writes = [t for t in txs if t['source'] == s and t['write']]
        reads = [t for t in txs if t['source'] == s and not t['write']]
        def issue(ch, t):
            return (f"issue_{ch}_{s}({t['index']}, 2'd{t['id']});")
        set_w = (f"{p}awloop = lp[g];" if wl else '') + (f" {p}awidunq = u;" if unique else '')
        set_r = (f"{p}arloop = lp[g];" if rl else '') + (f" {p}aridunq = u;" if unique else '')
        chk_b = ((f"check({p}bloop == lp[g][{wl - 1}:0], $sformatf(\"BLOOP {s} g=%0d %h\", g, "
                  f"{p}bloop));") if wl else '') + \
                ((f" check({p}bidunq == un[g], $sformatf(\"BIDUNQ {s} g=%0d %b\", g, "
                  f"{p}bidunq));") if unique else '')
        chk_r = ((f"check({p}rloop == lp[g][{rl - 1}:0], $sformatf(\"RLOOP {s} g=%0d %h\", g, "
                  f"{p}rloop));") if rl else '') + \
                ((f" check({p}ridunq == un[g], $sformatf(\"RIDUNQ {s} g=%0d %b\", g, "
                  f"{p}ridunq));") if unique else '')
        for ch, direction, setter, valid, ready in (('w', 'aw', set_w, 'awvalid', 'awready'),
                                                    ('r', 'ar', set_r, 'arvalid', 'arready')):
            drivers.append(f'''
// The {direction.upper()} issue of {s}: a unique ID only while no request of the ID is in flight,
// and no request of an ID while a unique one of it is.
task automatic issue_{ch}_{s}(input integer g, input [1:0] id);
  integer k, lane, guard; reg u;
  begin
    while (busy_{ch}_{s}[id]) @(negedge {sclk});
    u = uq[g] && count_{ch}_{s}[id] == 0;
    un[g] = u;
    {p}{direction}addr = ad[g]; {p}{direction}len = ln[g]; {p}{direction}size = 3'd{lanes.bit_length() - 1};
    {p}{direction}burst = bt[g]; {p}{direction}id = id; {setter}
    {p}{valid} = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}{ready}) begin guard = guard + 1; check(guard < 20000, $sformatf("{direction.upper()}_HANG {s} g=%0d", g)); @(posedge {sclk}); end
    fifo_{ch}_{s}[id][tail_{ch}_{s}[id] % 64] = g; tail_{ch}_{s}[id] = tail_{ch}_{s}[id] + 1;
    count_{ch}_{s}[id] = count_{ch}_{s}[id] + 1;
    if (u) busy_{ch}_{s}[id] = 1;
    @(negedge {sclk}); {p}{valid} = 0;
''' + (f'''    for (k = 0; k <= ln[g]; k = k + 1) begin
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        {p}wdata[lane*8 +: 8] = wfn(ad[g] + k * {lanes} + lane);
      {p}wstrb = {{{lanes}{{1'b1}}}}; {p}wlast = k == ln[g]; {p}wvalid = 1;
      @(posedge {sclk}); guard = 0;
      while (!{p}wready) begin guard = guard + 1; check(guard < 20000, $sformatf("W_HANG {s} g=%0d k=%0d", g, k)); @(posedge {sclk}); end
      @(negedge {sclk}); {p}wvalid = 0;
      if (stall(g, k)) repeat (stall(g, k)) @(negedge {sclk});
    end
''' if ch == 'w' else '') + '''  end
endtask''')
        drivers.append(f'''
initial begin : run_w_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
  {chr(10).join('  ' + issue('w', t) for t in writes)}
  issued[{2 * si}] = 1;
end
initial begin : run_r_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
  {chr(10).join('  ' + issue('r', t) for t in reads)}
  issued[{2 * si + 1}] = 1;
end
// Responses: each belongs to the oldest request of its ID.
initial begin : take_b_{s}
  integer g, id;
  {p}bready = 0;
  wait (reset_n);
  forever begin
    @(negedge {sclk}); {p}bready = !stall(answered_{s} + ticks, 7);
    @(posedge {sclk});
    if ({p}bvalid && {p}bready) begin
      id = {p}bid;
      g = fifo_w_{s}[id][head_w_{s}[id] % 64]; head_w_{s}[id] = head_w_{s}[id] + 1;
      check({p}bresp == er[g], $sformatf("BRESP {s} g=%0d %0d", g, {p}bresp));
      {chk_b}
      count_w_{s}[id] = count_w_{s}[id] - 1;
      if (un[g]) busy_w_{s}[id] = 0;
      answered_{s} = answered_{s} + 1; finished = finished + 1;
    end
  end
end
initial begin : take_r_{s}
  integer g, id, k, lane;
  {p}rready = 0;
  wait (reset_n);
  forever begin
    @(negedge {sclk}); {p}rready = !stall(answered_{s} + ticks, 9);
    @(posedge {sclk});
    if ({p}rvalid && {p}rready) begin
      id = {p}rid;
      g = fifo_r_{s}[id][head_r_{s}[id] % 64];
      k = beat_r_{s}[id];
      check({p}rresp == er[g] && {p}rlast == (k == ln[g]),
            $sformatf("R_RESPONSE {s} g=%0d k=%0d %0d %b", g, k, {p}rresp, {p}rlast));
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if (ok[g])
          check({p}rdata[lane*8 +: 8] == rfn(ad[g] + k * {lanes} + lane), "R_DATA");
      {chk_r}
      beat_r_{s}[id] = k + 1;
      if ({p}rlast) begin
        head_r_{s}[id] = head_r_{s}[id] + 1; beat_r_{s}[id] = 0;
        count_r_{s}[id] = count_r_{s}[id] - 1;
        if (un[g]) busy_r_{s}[id] = 0;
        finished = finished + 1;
      end
      answered_{s} = answered_{s} + 1;
    end
  end
end''')
    responders = []
    for name, (protocol, bits, _, axi5) in TARGETS.items():
        lanes = bits // 8
        p = f'nl_t_{name}_'
        axi5 = axi5 or {}
        zero_w = ' && '.join(f"{p}{x} == 0" for x in ('awidunq', 'awloop') if p + x in names)
        zero_r = ' && '.join(f"{p}{x} == 0" for x in ('aridunq', 'arloop') if p + x in names)
        noise_b = ' '.join(f"{p}{x} = $random;" for x in ('bidunq', 'bloop') if p + x in names)
        noise_r = ' '.join(f"{p}{x} = $random;" for x in ('ridunq', 'rloop') if p + x in names)
        responders.append(f'''
initial begin : write_{name}
  integer k, lane; reg [31:0] a; reg [7:0] len;
  {p}awready = 0; {p}wready = 0; {p}bvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}awready = 1;
    @(posedge {tclk}); while (!{p}awvalid) @(posedge {tclk});
    a = {p}awaddr; len = {p}awlen; {p}bid = {p}awid;
    check({zero_w or '1'}, $sformatf("AW_ECHO_PORTS {name} %h", a));
    @(negedge {tclk}); {p}awready = 0; {p}wready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      @(posedge {tclk}); while (!{p}wvalid) @(posedge {tclk});
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if ({p}wstrb[lane])
          check({p}wdata[lane*8 +: 8] == wfn((a & ~32'd{lanes - 1}) + k * {lanes} + lane), "W_DATA");
    end
    @(negedge {tclk}); {p}wready = 0; {p}bvalid = 1; {p}bresp = 0; {noise_b}
    @(posedge {tclk}); while (!{p}bready) @(posedge {tclk});
    @(negedge {tclk}); {p}bvalid = 0;
  end
end
initial begin : read_{name}
  integer k, lane; reg [31:0] a; reg [7:0] len;
  {p}arready = 0; {p}rvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}arready = 1;
    @(posedge {tclk}); while (!{p}arvalid) @(posedge {tclk});
    a = {p}araddr; len = {p}arlen; {p}rid = {p}arid;
    check({zero_r or '1'}, $sformatf("AR_ECHO_PORTS {name} %h", a));
    @(negedge {tclk}); {p}arready = 0;
    for (k = 0; k <= len; k = k + 1) begin
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        {p}rdata[lane*8 +: 8] = rfn((a & ~32'd{lanes - 1}) + k * {lanes} + lane);
      {p}rvalid = 1; {p}rresp = 0; {p}rlast = k == len; {noise_r}
      @(posedge {tclk}); while (!{p}rready) @(posedge {tclk});
      @(negedge {tclk}); {p}rvalid = 0;
    end
  end
end''')
    state = []
    for s in SOURCES:
        for ch in 'wr':
            state.append(f"integer fifo_{ch}_{s} [0:3][0:63]; integer head_{ch}_{s} [0:3]; "
                         f"integer tail_{ch}_{s} [0:3]; integer count_{ch}_{s} [0:3]; "
                         f"reg busy_{ch}_{s} [0:3];")
        state.append(f"integer beat_r_{s} [0:3]; integer answered_{s} = 0;")
    resets = []
    for s in SOURCES:
        for ch in 'wr':
            resets.append(f"head_{ch}_{s}[i] = 0; tail_{ch}_{s}[i] = 0; count_{ch}_{s}[i] = 0; "
                          f"busy_{ch}_{s}[i] = 0;")
        resets.append(f"beat_r_{s}[i] = 0;")
    init = [f"{p['name']} = 0;" for p in ports if p['direction'] == 'input'
            and not p['name'].startswith('clk_') and p['name'] != 'reset_n']
    return f'''`timescale 1ns/1ps
module tb;
reg clk_main = 0, clk_fast = 0, clk_slow = 0;
{clocks}
{chr(10).join(decl)}
nocloom_axi5_echo dut({conn});
integer errors = 0;
task automatic check(input ok, input string what);
  if (!ok) begin
    errors = errors + 1;
    if (errors < 20) $display("CHECK_FAIL: %0s at %0t", what, $time);
    if (errors >= 20) begin
      $display("CHECK_FAIL: %0d errors", errors);
      $finish;
    end
  end
endtask
function automatic [7:0] wfn(input [31:0] a);
  wfn = a[7:0] * 8'd7 + a[14:8] * 8'd13 + 8'h31;
endfunction
function automatic [7:0] rfn(input [31:0] a);
  rfn = a[7:0] * 8'd11 + a[14:8] * 8'd3 + 8'h6c;
endfunction
function automatic integer stall(input integer a, input integer k);
  reg [31:0] h;
  begin
    h = (a * 32'h2545f491) ^ (k * 32'h9e3779b9); h = h ^ (h >> 11);
    stall = h[3:0] < 4'd3 ? h[5:4] + 1 : 0;
  end
endfunction
reg [31:0] ad [0:{n - 1}]; integer ln [0:{n - 1}]; reg [7:0] lp [0:{n - 1}];
reg uq [0:{n - 1}]; reg un [0:{n - 1}]; reg ok [0:{n - 1}]; reg [1:0] bt [0:{n - 1}];
reg [1:0] er [0:{n - 1}];
{chr(10).join(state)}
reg [{2 * len(SOURCES) - 1}:0] issued = 0;
integer finished = 0, i, ticks = 0;
always @(posedge {sclk}) ticks <= ticks + 1;
initial begin
  for (i = 0; i < 4; i = i + 1) begin
    {' '.join(resets)}
  end
  {chr(10).join('  ' + line for line in table)}
end
initial begin
  {' '.join(init)}
  reset_n = 0;
  repeat (8) @(negedge {tclk});
  reset_n = 1;
end
{''.join(drivers)}
{''.join(responders)}
initial begin
  wait (issued == {{{2 * len(SOURCES)}{{1'b1}}}} && finished == {n});
  repeat (20) @(negedge {tclk});
  $display("AXI5_ECHO transactions={n} seed={seed}");
  if (errors == 0) $display("CHECK_PASS");
  else $display("CHECK_FAIL: %0d errors", errors);
  $finish;
end
initial begin
  #5000000;
  $display("CHECK_FAIL: TIMEOUT issued=%b finished=%0d", issued, finished);
  $finish;
end
endmodule
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cli', required=True)
    ap.add_argument('--work', required=True)
    ap.add_argument('--mode', choices=('switch', 'clocked'), required=True)
    ap.add_argument('--seed', type=int, default=5)
    args = ap.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    cfg = configuration(args.mode)
    (work / 'axi5_echo.yaml').write_text(json.dumps(cfg, indent=1) + '\n')
    out = work / 'generated'
    proc = subprocess.run([args.cli, 'generate', str(work / 'axi5_echo.yaml'), '--out-dir',
                           str(out)], capture_output=True, text=True, timeout=600)
    if proc.returncode:
        print(proc.stderr)
        return 1
    mapping = json.loads((out / 'nocloom_axi5_echo.map.json').read_text())
    (work / 'network.v').write_text(generated_rtl(out / 'nocloom_axi5_echo.v'))
    txs = transactions(args.seed)
    (work / 'tb.sv').write_text(bench(cfg, mapping['ports'], txs, args.seed,
                                      args.mode == 'clocked'))
    code = simulate(work, base / 'compiled')
    log = (work / 'simulation.log').read_text()
    passed = code == 0 and 'CHECK_PASS' in log and 'CHECK_FAIL' not in log
    print(log[-3000:] if not passed else log.splitlines()[0])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
