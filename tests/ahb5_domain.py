"""Check seven-bit AHB5 memory types on AXI target address transfers."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def topology(sources, targets):
    def plane(entries, exits, first, second):
        return {
            'nodes': [{'name': n, 'clock_domain': d}
                      for n, d in [('root', 'main'), ('leaf', 'slow')]],
            'entries': [{'endpoint': e, 'node': first} for e in entries],
            'exits': [{'endpoint': e, 'node': second} for e in exits],
            'links': [{'from': first, 'to': second}],
        }
    return {'request': plane(sources, targets, 'root', 'leaf'),
            'response': plane(targets, sources, 'leaf', 'root')}


def definition(mode):
    mixed = mode == 'mixed'
    clocked = mode == 'clocked'
    target = {
        'name': 'mem', 'protocol': 'ace5_lite', 'data_width': 32, 'attributes': 'amba',
        'clock_domain': 'slow' if clocked else 'main', 'id_width': 4, 'len_width': 4,
        'mappings': [{'base': '0x1000', 'size': '0x1000'}],
    }
    h = {'name': 'cpu', 'protocol': 'ahb5', 'data_width': 64 if clocked else 32,
         'clock_domain': 'main', 'ahb5': {'hprot_width': 7}}
    sources = [h]
    transport = {'data_width': 32}
    if mixed:
        sources += [
            {'name': 'legacy', 'protocol': 'ahb5', 'data_width': 32, 'clock_domain': 'main'},
            {'name': 'axi', 'protocol': 'axi5', 'data_width': 32, 'clock_domain': 'main',
             'id_width': 2, 'len_width': 4, 'axi5': {'shareable': True}},
        ]
    cfg = {
        'schema_version': 10, 'name': 'ahb5_domain', 'address_width': 32, 'clock_gating': False,
        'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in (['main', 'slow'] if clocked else ['main'])],
        'initiators': sources, 'targets': [target], 'transport': transport, 'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'},
    }
    if clocked:
        cfg['topology'] = topology(['cpu'], ['mem'])
    return cfg


def generate(cli, work, cfg):
    work.mkdir(parents=True)
    path = work / 'input.yaml'
    path.write_text(json.dumps(cfg) + '\n')
    dest = work / 'rtl'
    result = subprocess.run([cli, 'generate', path, '--out-dir', dest],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    stem = dest / ('nocloom_' + cfg['name'])
    ports = json.loads(stem.with_suffix('.map.json').read_text())['ports']
    return generated_rtl(stem.with_suffix('.v')), ports


def harness(module, ports, body, driven=()):
    decl, init, conns, clocks = [], [], [], []
    for p in ports:
        name = p['name']
        direct = p['direction'] == 'input' and name not in driven
        decl.append(f"{'reg' if direct else 'wire'} [{p['width'] - 1}:0] {name};")
        if direct and p.get('signal') != 'clock':
            init.append(f'{name} = 0;')
        if p.get('signal') == 'clock':
            clocks.append(f'initial {name} = 0; always #{7 if name == "clk_slow" else 5} {name} = ~{name};')
        conns.append(f'.{name}({name})')
    return f"""`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{module} dut({', '.join(conns)});
{chr(10).join(clocks)}
task automatic check(input condition, input string label_);
  if (condition !== 1'b1) begin $display("ASSERT_FAIL:%s", label_); $fatal(1); end
endtask
initial begin
  {' '.join(init)}
  repeat (8) @(negedge clk_main);
  reset_n = 1;
end
initial begin #200000; $display("ASSERT_FAIL:TIMEOUT"); $fatal(1); end
{body}
endmodule
"""


def ahb_driver(name, width):
    p = 'nl_i_' + name + '_'
    return f"""
assign {p}hready = {p}hreadyout;
task automatic transfer_{name}(input bit wr, input [31:0] addr, input [{width - 1}:0] prot,
    input [2:0] size, input bit lock, input bit err);
  integer cycles;
  @(negedge clk_main);
  {p}hsel = 1; {p}htrans = 2; {p}haddr = addr; {p}hwrite = wr;
  {p}hsize = size; {p}hprot = prot;
  @(posedge clk_main);
  check({p}hreadyout && !{p}hresp, "AHB_ADDRESS");
  @(negedge clk_main);
  {p}htrans = 0; {p}hsel = 0; {p}hprot = ~prot;
  {p}hwdata = {{2{{32'hcafe1ab5}}}};
  @(posedge clk_main); cycles = 1;
  while (!{p}hreadyout) begin
    cycles = cycles + 1;
    @(posedge clk_main);
  end
  check({p}hresp == err, "AHB_RESPONSE");
  if (!wr && !err)
    check({p}hrdata[31:0] == 32'hcafe1ab5 &&
          (size != 3 || {p}hrdata == 64'hcafe1ab5cafe1ab5), "AHB_DATA");
  $display("AHB_CYCLES {name} %h %0d %0d", addr, wr, cycles);
endtask
"""


def target_model(clocked):
    clk = 'clk_slow' if clocked else 'clk_main'
    p = 'nl_t_mem_'
    return f"""
reg [7:0] write_left = 0, read_left = 0;
task automatic address_check(input wr, input [31:0] addr, input [1:0] domain,
    input [3:0] cache, input [3:0] snoop, input [2:0] size, input lock);
  check(addr == expected_addr + 4 * seen && wr == expected_write, "TARGET_ADDRESS");
  check(domain == expected_domain, $sformatf("AXDOMAIN %h %0d got %0d want %0d",
      addr, wr, domain, expected_domain));
  check(cache == expected_cache, "AXCACHE");
  check(snoop == 0 && size == 2 && lock == expected_lock, "AXSHAPE");
  seen = seen + 1;
endtask
always @(posedge {clk}) if (reset_n) begin
  if ({p}awvalid && {p}awready) begin
    address_check(1, {p}awaddr, {p}awdomain, {p}awcache, {p}awsnoop, {p}awsize, {p}awlock);
    {p}bid <= {p}awid;
    {p}bresp <= {p}awlock ? 1 : 0;
    check({p}awlen == expected_len, "AWLEN");
    write_left <= {p}awlen;
    {p}awready <= 0;
    {p}wready <= 1;
  end
  if ({p}wvalid && {p}wready) begin
    check({p}wdata == 32'hcafe1ab5 && {p}wstrb == 4'hf && {p}wlast == (write_left == 0), "AXI_WRITE");
    if (write_left == 0) begin
      {p}wready <= 0;
      {p}bvalid <= 1;
    end else write_left <= write_left - 1;
  end
  if ({p}bvalid && {p}bready) begin
    {p}bvalid <= 0;
    {p}awready <= 1;
  end
  if ({p}arvalid && {p}arready) begin
    address_check(0, {p}araddr, {p}ardomain, {p}arcache, {p}arsnoop, {p}arsize, {p}arlock);
    check({p}arlen == expected_len, "ARLEN");
    read_left <= {p}arlen;
    {p}rid <= {p}arid;
    {p}rresp <= {p}arlock ? 1 : 0;
    {p}arready <= 0;
    {p}rvalid <= 1;
  end else if ({p}rvalid && {p}rready) begin
    if (read_left == 0) begin
      {p}rvalid <= 0;
      {p}arready <= 1;
    end else read_left <= read_left - 1;
  end
end
assign {p}rlast = read_left == 0;
"""


def requests(mode):
    # IHI 0033C table 3-7: HPROT[6:2], domain, ARCACHE, AWCACHE.
    rows = [(0, 3, 0, 0), (1, 3, 1, 1),
            (2, 0, 2, 2), (6, 0, 10, 6), (14, 0, 14, 14),
            (7, 0, 11, 7), (15, 0, 15, 15),
            (18, 2, 2, 2), (22, 2, 10, 6), (30, 2, 14, 14),
            (23, 2, 11, 7), (31, 2, 15, 15)]
    index = 0
    for hp, domain, rc, wc in rows:
        for write in (0, 1):
            # Device transfers keep their 32-bit size. A 64-bit Normal beat
            # crosses the clock boundary as two beats of one AXI burst.
            count = 2 if mode == 'clocked' and hp >= 2 else 1
            yield ('cpu', write, 0x1000 + index * 16, hp * 4 + 3, domain,
                   wc if write else rc, count, 0, 0)
            index += 1
    if mode == 'mixed':
        for write in (0, 1):
            yield ('legacy', write, 0x1180 + 16 * write, 11, 0, 6 if write else 10, 1, 0, 0)
            yield ('legacy', write, 0x11a0 + 16 * write, 3, 3, 0, 1, 0, 0)


def bench(mode, ports):
    clocked = mode == 'clocked'
    drivers = ahb_driver('cpu', 7)
    driven = ['nl_i_cpu_hready', 'nl_t_mem_rlast']
    calls = []
    for source, wr, address, prot, domain, cache, count, lock, err in requests(mode):
        low = address - 0x1000
        calls.append(f"""expect_request({wr}, {low}, {domain}, {cache}, {int(count == 2)}, {lock});
    transfer_{source}({wr}, 32'h{address:x}, {prot}, {3 if count == 2 else 2}, {lock}, {err});
    finish_request({int(count != 0)});""")
    if mode == 'mixed':
        driven.append('nl_i_legacy_hready')
        drivers += ahb_driver('legacy', 4)
        calls.append("""
    expect_request(0, 'h200, 1, 10, 0, 0);
    nl_i_axi_araddr = 'h1200; nl_i_axi_arsize = 2; nl_i_axi_arburst = 1;
    nl_i_axi_arcache = 10; nl_i_axi_ardomain = 1; nl_i_axi_arvalid = 1; nl_i_axi_rready = 1;
    @(posedge clk_main);
    while (!nl_i_axi_arready) @(posedge clk_main);
    @(negedge clk_main); nl_i_axi_arvalid = 0;
    @(posedge clk_main);
    while (!nl_i_axi_rvalid) @(posedge clk_main);
    check(nl_i_axi_rresp == 0 && nl_i_axi_rlast && nl_i_axi_rdata == 32'hcafe1ab5, "AXI_READ");
    finish_request(1);""")
    body = f"""
integer expected_addr, expected_domain, expected_cache, expected_write, expected_len, expected_lock, seen;
task automatic expect_request(input integer wr, addr, domain, cache, length_, lock);
  expected_write = wr; expected_addr = addr; expected_domain = domain; expected_cache = cache; seen = 0;
  expected_len = length_; expected_lock = lock;
endtask
task automatic finish_request(input integer n);
  repeat (50) @(negedge clk_main);
  check(seen == n, $sformatf("TARGET_COUNT got %0d want %0d", seen, n));
endtask
{drivers}
{target_model(clocked)}
initial begin
  wait (reset_n);
  repeat (10) @(negedge clk_main);
  nl_t_mem_awready = 1; nl_t_mem_arready = 1;
  nl_t_mem_rdata = 32'hcafe1ab5;
  {' '.join(calls)}
  $display("NOCLOOM_PASS AHB5 domain {mode}"); $finish;
end
"""
    return harness('nocloom_ahb5_domain', ports, body, driven)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--mode', choices=('single', 'mixed', 'clocked'), required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    case = Path(tempfile.mkdtemp(prefix='run-', dir=args.work))
    rtl, ports = generate(args.cli, case / 'generated', definition(args.mode))
    (case / 'network.v').write_text(rtl)
    (case / 'tb.sv').write_text(bench(args.mode, ports))
    rc = simulate(case, case / 'compiled')
    log = (case / 'simulation.log').read_text()
    assert rc == 0 and 'NOCLOOM_PASS' in log and 'ASSERT_FAIL' not in log, (case, log[-3000:])


if __name__ == '__main__':
    main()
