"""Shared drivers for directed exclusive transactions through the public CLI."""

import json
from pathlib import Path
import subprocess
import tempfile

from exclusive_bench import _AHB, _APB, _AXI, _LITE, _SOURCE, _fill
from sim_engine import Simulation, generated_rtl

_WISHBONE = '''
reg [7:0] @T@_mem [0:4095];
integer @T@_i, @T@_wcount = 0, @T@_rcount = 0;
initial for (@T@_i = 0; @T@_i < 4096; @T@_i = @T@_i + 1) @T@_mem[@T@_i] = 0;
wire [11:0] @T@_word = {nl_t_@T@_adr[11:2], 2'b00};
assign nl_t_@T@_ack = nl_t_@T@_cyc && nl_t_@T@_stb;
assign nl_t_@T@_err = 0;
assign nl_t_@T@_dat_r = {@T@_mem[@T@_word + 3], @T@_mem[@T@_word + 2],
                         @T@_mem[@T@_word + 1], @T@_mem[@T@_word]};
always @(posedge clk_main) if (reset_n && nl_t_@T@_ack) begin
 if (nl_t_@T@_we) begin
  for (@T@_i = 0; @T@_i < 4; @T@_i = @T@_i + 1)
   if (nl_t_@T@_sel[@T@_i]) @T@_mem[@T@_word + @T@_i] <= nl_t_@T@_dat_w[8 * @T@_i +: 8];
  @T@_wcount <= @T@_wcount + 1;
 end else @T@_rcount <= @T@_rcount + 1;
end
'''


def configuration(targets, width=32):
    return {
        'schema_version': 10, 'name': 'exclusive_regression', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main',
                           'reset_port': 'reset_n', 'gated': False}],
        'initiators': [
            {'name': s, 'clock_domain': 'main', 'protocol': 'axi4',
             'data_width': width, 'id_width': 2, 'len_width': 4,
             'user_width': 2, 'max_outstanding': 2,
             'user_flags': {'id0': 'user[0]', 'id1': 'user[1]', 'source': i}}
            for i, s in enumerate(('s0', 's1'))],
        'targets': targets,
        'transport': {'data_width': 32, 'user_flags': ['id0', 'id1', 'source']},
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'},
    }


def target(name, protocol, base, size=4096, mode='network'):
    entry = {'name': name, 'clock_domain': 'main', 'protocol': protocol,
             'data_width': 32,
             'mappings': [{'base': hex(base), 'size': hex(size)}],
             'exclusive': mode, 'exclusive_owner_flags': ['id0', 'id1', 'source']}
    if protocol in ('axi4', 'axi3', 'axi4_lite', 'ahb_lite', 'apb4'):
        entry['attributes'] = 'amba'
    if protocol in ('axi4', 'axi3'):
        entry['len_width'] = 4
        entry['max_outstanding'] = 2
    if protocol == 'wishbone':
        entry['address_unit'] = 'byte'
    return entry


def bench(mapping, cfg, body, checks=''):
    widths = {p['name']: p['width'] for p in mapping['ports']}
    models, driven = [], set()
    for t in cfg['targets']:
        name, protocol = t['name'], t['protocol']
        fields = dict(T=name, TCLK='clk_main', U=name.upper())
        if protocol in ('axi4', 'axi3'):
            iw = widths[f'nl_t_{name}_awid']
            models.append(_fill(
                _AXI, **fields, IW=iw, NID=1 << iw,
                MON=int(t['exclusive'] == 'target'), BYTES=4, LANE=2, LANEZ="2'd0",
                RDATA='{' + ', '.join(f'{name}_mem[{{{name}_ra[11:2], 2\'d{k}}}]'
                                      for k in reversed(range(4))) + '}',
                IDS=f'assign nl_t_{name}_bid = {name}_awid; '
                    f'assign nl_t_{name}_rid = {name}_arid;', SRC_AW='', SRC_AR=''))
            signals = ('awready', 'wready', 'bvalid', 'bresp', 'bid', 'arready',
                       'rvalid', 'rresp', 'rid', 'rlast', 'rdata')
        elif protocol == 'apb4':
            models.append(_fill(_APB, **fields, STROBE=f'nl_t_{name}_pstrb'))
            signals = ('pready', 'pslverr', 'prdata')
        elif protocol == 'ahb_lite':
            models.append(_fill(_AHB, **fields))
            signals = ('hreadyout', 'hresp', 'hrdata')
        elif protocol == 'wishbone':
            models.append(_fill(_WISHBONE, **fields))
            signals = ('ack', 'err', 'dat_r')
        else:
            models.append(_fill(_LITE, **fields))
            signals = ('awready', 'wready', 'bvalid', 'bresp', 'arready',
                       'rvalid', 'rresp', 'rdata')
        driven.update(f'nl_t_{name}_{s}' for s in signals)
    decl, init, conns = [], [], []
    for p in mapping['ports']:
        name = p['name']
        reg = p['direction'] == 'input' and name not in driven
        decl.append(f'{"reg" if reg else "wire"} [{p["width"] - 1}:0] {name};')
        if reg and name not in ('reset_n', 'clk_main'):
            init.append(f'{name} = 0;')
        conns.append(f'.{name}({name})')
    width = cfg['initiators'][0]['data_width']
    sources = ''.join(_fill(_SOURCE, S=s, U=s.upper(), DW=width, SB=width // 8,
                            IW=cfg['initiators'][i]['id_width'], Q=256, SEED=f'{0xace1 + i:04x}')
                      for i, s in enumerate(('s0', 's1')))
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0;
always #5 clk_main = ~clk_main;
integer cycle = 0, k, mark_w, mark_r, mark_aw;
reg reject_expected = 0;
reg race_seen [0:63];
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 check(cycle < 20000, "PROGRESS");
end
task automatic check;
 input condition; input [8*80-1:0] label_;
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:EXCLUSIVE.%0s cycle=%0d", label_, cycle); $fatal(1);
 end
endtask
{''.join(models)}
{sources}
{checks}
initial begin
{chr(10).join(init)}
 reset_n = 0;
 repeat (5) @(negedge clk_main);
 reset_n = 1;
 repeat (5) @(negedge clk_main);
{body}
 repeat (20) @(negedge clk_main);
 $display("NOCLOOM_PASS exclusive directed");
 $finish;
end
endmodule
'''


def run(cli, work, cfg, body, checks=''):
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=work))
    path = work / 'config.yaml'
    path.write_text(json.dumps(cfg) + '\n')
    proc = subprocess.run([cli, 'generate', str(path), '--out-dir', str(work / 'rtl')],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    mapping = json.loads(next((work / 'rtl').glob('*.map.json')).read_text())
    (work / 'network.v').write_text(generated_rtl(work / 'rtl' / (mapping['module'] + '.v')))
    (work / 'tb.sv').write_text(bench(mapping, cfg, body, checks))
    result = Simulation(work, 'network.v', compile_timeout=300, run_timeout=120).run()
    assert result.returncode == 0, result.stdout[-6000:]
