"""Exercise the high selector bit together with a low bit in an APB decode."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from sim_engine import Simulation, generated_rtl


def configuration(count):
    low = [{'name': f'sel{i:02}', 'equals': {'strap': i}} for i in range(count - 1)]
    high = {'name': 'upper', 'equals': {'high': 1}}
    mappings = [
        {'base': '0x1000', 'size': '0x1000', 'when': condition,
         'local_address': hex(page * 4096)}
        for page, condition in enumerate((
            {'sel00': True, 'upper': True},
            {'sel00': True, 'upper': False},
            {'sel00': False}), 1)]
    mappings.append({'base': '0x2000', 'size': '0x1000',
                     'when': dict.fromkeys((s['name'] for s in low[1:]), False)})
    return dict(
        schema_version=10, name='selector_span', address_width=32,
        clock_domains=[dict(name='main', clock_port='clk', reset_port='reset_n', test_port='test')],
        reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
        address_inputs=[dict(name='strap', width=5, clock_domain='main'),
                        dict(name='high', width=1, clock_domain='main')],
        address_selectors=low + [high],
        initiators=[dict(name='cpu', clock_domain='main', protocol='apb4', data_width=32)],
        targets=[dict(name='regs', clock_domain='main', protocol='apb4', data_width=32,
                      address_width=16, attributes='amba', mappings=mappings)],
        connectivity='all', output=dict(backend='verilog', language='verilog_2001'))


BENCH = r"""
`timescale 1ns/1ps
module tb;
  reg clk = 0, reset_n = 1;
  always #5 clk = ~clk;
  reg [4:0] strap = 0;
  reg high = 0, sel = 0, en = 0;
  wire ready, error, target_sel, target_en, target_write;
  wire [31:0] data;
  wire [15:0] local_addr;
  reg [15:0] expected_addr;
  integer completed = 0;
  nocloom_selector_span dut(
    .clk(clk), .reset_n(reset_n), .test(1'b0), .strap(strap), .high(high),
    .nl_i_cpu_psel(sel), .nl_i_cpu_penable(en), .nl_i_cpu_paddr(32'h1004),
    .nl_i_cpu_pwrite(1'b0), .nl_i_cpu_pwdata(32'b0), .nl_i_cpu_pstrb(4'b0),
    .nl_i_cpu_pprot(3'b0), .nl_i_cpu_prdata(data), .nl_i_cpu_pready(ready),
    .nl_i_cpu_pslverr(error), .nl_t_regs_psel(target_sel), .nl_t_regs_penable(target_en),
    .nl_t_regs_pwrite(target_write), .nl_t_regs_paddr(local_addr),
    .nl_t_regs_prdata({16'hba91, local_addr}), .nl_t_regs_pready(1'b1),
    .nl_t_regs_pslverr(1'b0)
  );
  always @(posedge clk)
    if (target_sel && target_en) begin
      if (target_write || local_addr !== expected_addr)
        $fatal(1, "CHECK_FAIL SELECTOR_ADDRESS");
      completed = completed + 1;
    end

  task automatic access(input [4:0] low, input high_select, input [15:0] addr);
    @(negedge clk); strap = low; high = high_select; expected_addr = addr; sel = 1;
    @(negedge clk); en = 1;
    @(posedge clk);
    while (ready !== 1'b1) @(posedge clk);
    if (error !== 0 || data !== {16'hba91, addr}) $fatal(1, "CHECK_FAIL SELECTOR_DATA");
    @(negedge clk); en = 0; sel = 0;
  endtask

  initial begin
    #1 reset_n = 0;
    repeat (8) @(negedge clk);
    reset_n = 1;
    repeat (8) @(negedge clk);
    access(0, 1, 16'h1004);
    access(0, 0, 16'h2004);
    access(1, 1, 16'h3004);
    access(1, 0, 16'h3004);
    if (completed != 4) $fatal(1, "CHECK_FAIL SELECTOR_COUNT");
    $display("CHECK_PASS SELECTOR_SPAN completed=%0d", completed);
    $finish;
  end
  initial #20000 $fatal(1, "CHECK_FAIL SELECTOR_TIMEOUT");
endmodule
"""


def run(cli, root, count):
    root.mkdir()
    cfg = root / 'input.yaml'
    cfg.write_text(json.dumps(configuration(count)))
    result = subprocess.run([cli, 'generate', cfg, '--out-dir', root / 'rtl'], capture_output=True,
                            text=True, timeout=30)
    if count == 33:
        assert result.returncode != 0 and 'NCL_ADDRESS_SELECTOR' in result.stderr, result.stderr
        return
    assert result.returncode == 0, result.stderr
    rtl = generated_rtl(root / 'rtl/nocloom_selector_span.v')
    mapping = json.loads((root / 'rtl/nocloom_selector_span.map.json').read_text())
    high = mapping['network']['address_selectors']['upper'].split('/')[1]
    mutant, replaced = re.subn(rf'^assign nl_internal_v{high} = [^;]+;$',
                               f"assign nl_internal_v{high} = 1'b0;", rtl, flags=re.M)
    assert replaced == 1, high
    for mode, source in (('normal', rtl), ('missing_high_bit', mutant)):
        work = root / mode
        work.mkdir()
        (work / 'network.v').write_text(source)
        (work / 'tb.sv').write_text(BENCH)
        result = Simulation(work, 'network.v').run()
        log = (work / 'simulation.log').read_text()
        if mode == 'normal':
            assert result.returncode == 0 and 'CHECK_PASS SELECTOR_SPAN' in log, log
        else:
            assert result.returncode != 0 and 'CHECK_FAIL SELECTOR_ADDRESS' in log, log


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=args.work))
    for selectors in (17, 32, 33):
        run(args.cli, work / str(selectors), selectors)
