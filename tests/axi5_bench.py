"""The transaction bench of the AXI5 tests: AXI initiator drivers, target responders and
channel checks. A target identifies the transaction of each of its bursts by address and checks
the expected port values there. Data and strobes follow a function of the byte address, so the
checks hold through width conversion and packet splitting."""


def bench(cfg, ports, txs, seed, top, marker):
    """The bench of a network of AXI initiators and targets. Each transaction drives the address
    channel signals of t['drive'] (suffix -> (bits, value)) and expects t['expect'] at every
    target burst of it, for each such port the target has."""
    sources = {i['name']: i['data_width'] for i in cfg['initiators']}
    targets = {x['name']: x['data_width'] for x in cfg['targets']}
    suffixes = sorted({k for t in txs for k in t['expect']})
    widths = {k: max(t['expect'][k][0] for t in txs if k in t['expect']) for k in suffixes}
    names = {p['name']: p for p in ports}
    decl = []
    for p in ports:
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f"{kind} [{p['width'] - 1}:0] {p['name']};")
    conn = ', '.join(f".{p['name']}({p['name']})" for p in ports)
    clocked = len(cfg['clock_domains']) > 1
    sclk, tclk = ('clk_fast', 'clk_slow') if clocked else ('clk_main', 'clk_main')
    clocks = ('always #5 clk_fast = ~clk_fast;\nalways #7 clk_slow = ~clk_slow;' if clocked
              else 'always #5 clk_main = ~clk_main;')
    n = len(txs)
    lines = []
    for t in txs:
        g = t['index']
        items = [f"lo[{g}]=32'h{t['lo']:x};", f"hi[{g}]=32'h{t['hi']:x};",
                 f"wr[{g}]={int(t['write'])};", f"tg[{g}]={list(targets).index(t['target'])};",
                 f"want[{g}]={(t['len'] + 1) << t['size']};"]
        for k, (bits, value) in t['expect'].items():
            items.append(f"e_{k}[{g}]={widths[k]}'d{value}; v_{k}[{g}]=1;")
        lines.append(' '.join(items))
    # Initiator drivers.
    drivers = []
    for si, (s, bits) in enumerate(sources.items()):
        mine = [t for t in txs if t['source'] == s]
        lanes = bits // 8
        p = f'nl_i_{s}_'
        drive = []
        for t in mine:
            ch = 'aw' if t['write'] else 'ar'
            sets = [f"{p}{ch}addr = 32'h{t['addr']:x};", f"{p}{ch}len = 8'd{t['len']};",
                    f"{p}{ch}size = 3'd{t['size']};", f"{p}{ch}burst = 2'd{t['burst']};",
                    f"{p}{ch}id = 2'd{t['id']};"]
            sets += [f"{p}{ch}{k} = {w}'d{v};" for k, (w, v) in t['drive'].items()]
            call = 'write_' if t['write'] else 'read_'
            drive.append(' '.join(sets) + f" {call}{s}(32'h{t['addr']:x}, 8'd{t['len']}, "
                         f"3'd{t['size']}, 2'd{t['burst']}, 2'd{t['id']});")
        drivers.append(f'''
function automatic [31:0] beat_{s}(input [31:0] a, input [7:0] len, input [2:0] size,
                                   input [1:0] burst, input integer k);
  reg [31:0] step, container, base;
  begin
    step = 32'd1 << size;
    if (burst == 2'd0) beat_{s} = a;
    else if (burst == 2'd2) begin
      container = step * (len + 1); base = a & ~(container - 1);
      beat_{s} = base + ((a - base + k * step) % container);
    end else beat_{s} = (a & ~(step - 1)) + k * step;
  end
endfunction
task automatic write_{s}(input [31:0] a, input [7:0] len, input [2:0] size, input [1:0] burst,
                         input [1:0] id);
  integer k, lane, guard; reg [31:0] b;
  begin
    {p}awvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}awready) begin guard = guard + 1; check(guard < 20000, "AW_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}awvalid = 0;
    for (k = 0; k <= len; k = k + 1) begin
      b = beat_{s}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1) begin
        {p}wdata[lane*8 +: 8] = wfn((b & ~32'd{lanes - 1}) + lane);
        {p}wstrb[lane] = lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size));
      end
      {p}wlast = k == len; {p}wvalid = 1;
      @(posedge {sclk}); guard = 0;
      while (!{p}wready) begin guard = guard + 1; check(guard < 20000, "W_HANG"); @(posedge {sclk}); end
      @(negedge {sclk}); {p}wvalid = 0;
    end
    {p}bready = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}bvalid) begin guard = guard + 1; check(guard < 40000, "B_HANG"); @(posedge {sclk}); end
    check({p}bresp == 0 && {p}bid == id,
          $sformatf("B_RESPONSE {s} %h resp=%0d id=%0d/%0d", a, {p}bresp, {p}bid, id));
    @(negedge {sclk}); {p}bready = 0;
  end
endtask
task automatic read_{s}(input [31:0] a, input [7:0] len, input [2:0] size, input [1:0] burst,
                        input [1:0] id);
  integer k, lane, guard; reg [31:0] b;
  begin
    {p}arvalid = 1;
    @(posedge {sclk}); guard = 0;
    while (!{p}arready) begin guard = guard + 1; check(guard < 20000, "AR_HANG"); @(posedge {sclk}); end
    @(negedge {sclk}); {p}arvalid = 0; {p}rready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      @(posedge {sclk}); guard = 0;
      while (!{p}rvalid) begin guard = guard + 1; check(guard < 40000, "R_HANG"); @(posedge {sclk}); end
      check({p}rresp == 0 && {p}rid == id && {p}rlast == (k == len),
            $sformatf("R_RESPONSE {s} %h k=%0d resp=%0d id=%0d/%0d last=%0d", a, k, {p}rresp,
                      {p}rid, id, {p}rlast));
      b = beat_{s}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if (lane >= (b % {lanes}) && lane < (b % {lanes}) + (1 << size) - (b % (1 << size)))
          check({p}rdata[lane*8 +: 8] == rfn((b & ~32'd{lanes - 1}) + lane), "R_DATA");
    end
    @(negedge {sclk}); {p}rready = 0;
  end
endtask
initial begin : run_{s}
  wait (reset_n); repeat (3) @(negedge {sclk});
  {chr(10).join('  ' + d for d in drive)}
  done[{si}] = 1;
end
''')
    # Target responders and attribute monitors.
    responders = []
    for ti, (tname, bits) in enumerate(targets.items()):
        lanes = bits // 8
        p = f'nl_t_{tname}_'
        checks = {}
        for ch in ('aw', 'ar'):
            c = [f"(!v_{k}[g] || {p}{ch}{k} == e_{k}[g][{names[p + ch + k]['width'] - 1}:0])"
                 for k in suffixes if p + ch + k in names]
            checks[ch] = ' && '.join(c) or '1'
            shown = [k for k in suffixes if p + ch + k in names]
            checks[ch + '_show'] = (' '.join(f'{k}=%0d/%0d' for k in shown),
                                    ', '.join(f'{p}{ch}{k}, e_{k}[g]' for k in shown))
        responders.append(f'''
function automatic [31:0] tbeat_{tname}(input [31:0] a, input [7:0] len, input [2:0] size,
                                       input [1:0] burst, input integer k);
  reg [31:0] step, container, base;
  begin
    step = 32'd1 << size;
    if (burst == 2'd0) tbeat_{tname} = a;
    else if (burst == 2'd2) begin
      container = step * (len + 1); base = a & ~(container - 1);
      tbeat_{tname} = base + ((a - base + k * step) % container);
    end else tbeat_{tname} = (a & ~(step - 1)) + k * step;
  end
endfunction
// The transaction of a target burst by its address.
function automatic integer find_{tname}(input [31:0] a, input w);
  integer g;
  begin
    find_{tname} = -1;
    for (g = 0; g < {n}; g = g + 1)
      if (tg[g] == {ti} && wr[g] == w && a >= lo[g] && a < hi[g]) find_{tname} = g;
  end
endfunction
initial begin : write_{tname}
  integer g, k, lane; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{names[p + 'awid']['width'] - 1}:0] id;
  {p}awready = 0; {p}wready = 0; {p}bvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}awready = 1;
    @(posedge {tclk}); while (!{p}awvalid) @(posedge {tclk});
    a = {p}awaddr; len = {p}awlen; size = {p}awsize; burst = {p}awburst; id = {p}awid;
    g = find_{tname}(a, 1);
    check(g >= 0, $sformatf("AW_UNKNOWN {tname} %h", a));
    if (g >= 0) begin
      check({checks['aw']}, $sformatf("AW_ATTRIBUTES {tname} %0d %h {checks['aw_show'][0]}", g, a{', ' if checks['aw_show'][1] else ''}{checks['aw_show'][1]}));
      hits[g] = hits[g] + 1;
    end
    @(negedge {tclk}); {p}awready = 0; {p}wready = 1;
    for (k = 0; k <= len; k = k + 1) begin
      @(posedge {tclk}); while (!{p}wvalid) @(posedge {tclk});
      b = tbeat_{tname}(a, len, size, burst, k);
      check({p}wlast == (k == len), "W_LAST");
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        if ({p}wstrb[lane]) begin
          check({p}wdata[lane*8 +: 8] == wfn((b & ~32'd{lanes - 1}) + lane), "W_DATA");
          if (g >= 0) seen[g] = seen[g] + 1;
        end
    end
    @(negedge {tclk}); {p}wready = 0; {p}bvalid = 1; {p}bid = id; {p}bresp = 0;
    @(posedge {tclk}); while (!{p}bready) @(posedge {tclk});
    @(negedge {tclk}); {p}bvalid = 0;
  end
end
initial begin : read_{tname}
  integer g, k, lane; reg [31:0] a, b; reg [7:0] len; reg [2:0] size; reg [1:0] burst;
  reg [{names[p + 'arid']['width'] - 1}:0] id;
  {p}arready = 0; {p}rvalid = 0;
  wait (reset_n);
  forever begin
    @(negedge {tclk}); {p}arready = 1;
    @(posedge {tclk}); while (!{p}arvalid) @(posedge {tclk});
    a = {p}araddr; len = {p}arlen; size = {p}arsize; burst = {p}arburst; id = {p}arid;
    g = find_{tname}(a, 0);
    check(g >= 0, $sformatf("AR_UNKNOWN {tname} %h", a));
    if (g >= 0) begin
      check({checks['ar']}, $sformatf("AR_ATTRIBUTES {tname} %0d %h {checks['ar_show'][0]}", g, a{', ' if checks['ar_show'][1] else ''}{checks['ar_show'][1]}));
      hits[g] = hits[g] + 1;
    end
    @(negedge {tclk}); {p}arready = 0;
    for (k = 0; k <= len; k = k + 1) begin
      b = tbeat_{tname}(a, len, size, burst, k);
      for (lane = 0; lane < {lanes}; lane = lane + 1)
        {p}rdata[lane*8 +: 8] = rfn((b & ~32'd{lanes - 1}) + lane);
      {p}rvalid = 1; {p}rid = id; {p}rresp = 0; {p}rlast = k == len;
      @(posedge {tclk}); while (!{p}rready) @(posedge {tclk});
      @(negedge {tclk}); {p}rvalid = 0;
    end
  end
end
''')
    # VALID stays and the payload holds while READY is low, on every channel of every port.
    stable = []
    groups = {}
    for p in ports:
        if not p['name'].startswith('nl_'):
            continue
        stem = p['name'].split('_', 3)
        endpoint, signal = '_'.join(stem[:3]), stem[3]
        outward = (endpoint.startswith('nl_i') and signal[:1] in 'rb' and signal != 'bready'
                   and signal != 'rready') or (endpoint.startswith('nl_t')
                                               and signal[:2] in ('aw', 'ar') or
                                               endpoint.startswith('nl_t') and signal[:1] == 'w')
        if not outward:
            continue
        ch = signal[:2] if signal[:2] in ('aw', 'ar') else signal[:1]
        groups.setdefault((endpoint, ch), []).append(p['name'])
    for (endpoint, ch), signals in sorted(groups.items()):
        valid, ready = f'{endpoint}_{ch}valid', f'{endpoint}_{ch}ready'
        if valid not in names or ready not in names:
            continue
        payload = [s for s in signals if s not in (valid, ready)]
        clk = sclk if endpoint.startswith('nl_i') else tclk
        stable.append(f'''
reg held_{endpoint}_{ch}; reg [{sum(names[s]['width'] for s in payload) - 1}:0] last_{endpoint}_{ch};
always @(posedge {clk}) begin
  if (reset_n && held_{endpoint}_{ch})
    check({valid} && {{{', '.join(payload)}}} == last_{endpoint}_{ch}, "STABLE_{endpoint}_{ch}");
  held_{endpoint}_{ch} <= reset_n && {valid} && !{ready};
  last_{endpoint}_{ch} <= {{{', '.join(payload)}}};
end''')
    init = [f"{p['name']} = 0;" for p in ports if p['direction'] == 'input'
            and not p['name'].startswith('clk_') and p['name'] != 'reset_n']
    return f'''`timescale 1ns/1ps
module tb;
reg clk_main = 0, clk_fast = 0, clk_slow = 0;
{clocks}
{chr(10).join(d for d in decl if not d.split()[-1].rstrip(';').startswith('clk_'))}
{top} dut({conn});
integer errors = 0;
task automatic check(input ok, input string what);
  if (!ok) begin
    errors = errors + 1;
    if (errors < 20) $display("CHECK_FAIL: %0s at %0t", what, $time);
  end
endtask
function automatic [7:0] wfn(input [31:0] a);
  wfn = a[7:0] * 8'd7 + a[14:8] * 8'd13 + 8'h31;
endfunction
function automatic [7:0] rfn(input [31:0] a);
  rfn = a[7:0] * 8'd11 + a[14:8] * 8'd3 + 8'h6c;
endfunction
reg [31:0] lo [0:{n - 1}]; reg [31:0] hi [0:{n - 1}]; reg wr [0:{n - 1}]; integer tg [0:{n - 1}];
{''.join(f"reg [{widths[k] - 1}:0] e_{k} [0:{n - 1}]; reg v_{k} [0:{n - 1}];{chr(10)}" for k in suffixes)}integer hits [0:{n - 1}]; integer seen [0:{n - 1}]; integer want [0:{n - 1}];
reg [{len(sources) - 1}:0] done = 0;
integer i;
initial begin
  for (i = 0; i < {n}; i = i + 1) begin
    hits[i] = 0; seen[i] = 0;
    {' '.join(f"v_{k}[i] = 0; e_{k}[i] = 0;" for k in suffixes)}
  end
  {chr(10).join('  ' + l for l in lines)}
end
initial begin
  {' '.join(init)}
  reset_n = 0;
  repeat (8) @(negedge {tclk});
  reset_n = 1;
end
{''.join(stable)}
{''.join(drivers)}
{''.join(responders)}
integer split;
initial begin
  wait (done == {{{len(sources)}{{1'b1}}}});
  repeat (50) @(negedge {tclk});
  split = 0;
  for (i = 0; i < {n}; i = i + 1) begin
    check(hits[i] >= 1, "NO_TARGET_BURST");
    if (wr[i]) check(seen[i] == want[i], "WRITE_BYTES");
    if (hits[i] > 1) split = split + 1;
  end
  $display("{marker} transactions={n} split=%0d seed={seed}", split);
  if (errors == 0) $display("CHECK_PASS");
  else $display("CHECK_FAIL: %0d errors", errors);
  $finish;
end
initial begin
  #20000000;
  $display("CHECK_FAIL: TIMEOUT done=%b", done);
  $finish;
end
endmodule
'''
