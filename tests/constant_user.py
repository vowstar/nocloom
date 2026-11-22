"""Check constant target AxUSER bits on a network without user flags.

An AXI4 manager writes and reads through the network. The target model checks AWUSER and
ARUSER at every accepted request. A second run changes one user bit before the checker and
must fail.
"""

import argparse
import subprocess
import tempfile
from pathlib import Path

from protocol_combinations import execute, generate

BITS = [1, 0, 1, 1]
VALUE = sum(bit << index for index, bit in enumerate(BITS))


def definition(mode):
    lite = mode == 'axi4_lite'
    target = dict(name='t0', clock_domain='main', data_width=32, attributes='amba',
                  protocol='axi3' if mode == 'axi3' else 'axi4_lite' if lite else 'axi4',
                  mappings=[dict(base='0x0', size='0x1000')], user=BITS)
    if not lite:
        target.update(id_width=4, len_width=4)
    if mode == 'axi4_timeout':
        target.update(max_outstanding=2, timeout=dict(cycles=64))
    initiator = dict(name='s0', clock_domain='main', protocol='axi4', data_width=32, id_width=2,
                     len_width=8)
    config = dict(schema_version=10, name='constant_user_' + mode, address_width=32,
                  reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
                  clock_domains=[dict(name='main', clock_port='clk_main', reset_port='reset_n',
                                      test_port='test')],
                  initiators=[initiator], targets=[target], connectivity='all',
                  output=dict(backend='verilog', language='verilog_2001'))
    if mode == 'axi4_timeout':
        initiator['max_outstanding'] = 2
        config['transport'] = dict(max_outstanding=2)
    return config


def bench(mapping, mode):
    lite = mode == 'axi4_lite'
    ports = {p['name']: p for p in mapping['ports']}
    decl, conns, init = [], [], []
    for name, p in ports.items():
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f'{kind} [{p["width"] - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and p.get('signal') != 'clock':
            init.append(f' {name} = 0;')
    t, s = 'nl_t_t0_', 'nl_i_s0_'
    width = len(BITS)
    assert ports[t + 'awuser']['width'] == width and ports[t + 'aruser']['width'] == width
    ids = '' if lite else f'{t}bid = write_id; {t}rid = read_id; {t}rlast = read_left == 0;'
    write_last = "1'b1" if lite else t + 'wlast'
    write_id = '0' if lite else t + 'awid'
    read_len = '0' if lite else t + 'arlen'
    read_id = '0' if lite else t + 'arid'
    # A Lite target takes one request for each word.
    requests = 3 if lite else 2
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0; always #5 clk_main = ~clk_main;
integer cycle = 0;
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 5000) begin $display("ASSERT_FAIL:CONST.TIMEOUT"); $fatal(1); end
end
task automatic check(input condition, input [8*24-1:0] label_);
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
reg write_open = 0, b_pending = 0, read_open = 0;
integer aw_count = 0, w_count = 0, b_count = 0, ar_count = 0, r_count = 0, sent = 0;
integer read_left = 0;
reg [31:0] read_address;
reg [7:0] write_id, read_id;
always @* begin
 {t}awready = !write_open && !b_pending && cycle % 3 == 1;
 {t}wready = write_open && cycle % 2 == 0;
 {t}bvalid = b_pending; {t}bresp = 0;
 {t}arready = !read_open && cycle % 3 == 2;
 {t}rvalid = read_open && cycle % 2 == 1; {t}rresp = 0;
 {t}rdata = 32'hd0000000 | read_address[15:0];
 {ids}
end
always @(posedge clk_main) if (reset_n) begin
 if ({t}awvalid && {t}awready) begin
  check({t}awuser === {width}'d{VALUE}, "CONST.AWUSER");
  $display("EV CONST AW %h %h", {t}awaddr, {t}awuser);
  aw_count <= aw_count + 1; write_open <= 1; write_id <= {write_id};
 end
 if ({t}wvalid && {t}wready) begin
  check({t}wstrb == 4'hf && {t}wdata === (32'habc00000 | w_count), "CONST.WDATA");
  w_count <= w_count + 1;
  if ({write_last}) begin write_open <= 0; b_pending <= 1; end
 end
 if ({t}bvalid && {t}bready) begin b_pending <= 0; b_count <= b_count + 1; end
 if ({t}arvalid && {t}arready) begin
  check({t}aruser === {width}'d{VALUE}, "CONST.ARUSER");
  $display("EV CONST AR %h %h", {t}araddr, {t}aruser);
  ar_count <= ar_count + 1; read_open <= 1; read_left <= {read_len};
  read_address <= {t}araddr; read_id <= {read_id};
 end
 if ({t}rvalid && {t}rready) begin
  r_count <= r_count + 1;
  if (read_left == 0) read_open <= 0;
  else begin read_left <= read_left - 1; read_address <= read_address + 4; end
 end
end
task automatic do_write(input [31:0] address, input integer beats);
 integer k;
 begin
  @(negedge clk_main); {s}awaddr = address; {s}awlen = beats - 1; {s}awsize = 2; {s}awburst = 1;
  {s}awid = 2; {s}awvalid = 1; {s}bready = 1;
  @(posedge clk_main); while (!{s}awready) @(posedge clk_main);
  @(negedge clk_main); {s}awvalid = 0;
  for (k = 0; k < beats; k = k + 1) begin
   {s}wvalid = 1; {s}wdata = 32'habc00000 | sent; {s}wstrb = 15; {s}wlast = k == beats - 1;
   @(posedge clk_main); while (!{s}wready) @(posedge clk_main);
   sent = sent + 1;
   @(negedge clk_main); {s}wvalid = 0;
  end
  @(posedge clk_main); while (!{s}bvalid) @(posedge clk_main);
  check({s}bresp == 0 && {s}bid == 2, "CONST.BRESP");
  @(negedge clk_main); {s}bready = 0;
 end
endtask
task automatic do_read(input [31:0] address, input integer beats);
 integer k;
 begin
  @(negedge clk_main); {s}araddr = address; {s}arlen = beats - 1; {s}arsize = 2; {s}arburst = 1;
  {s}arid = 1; {s}arvalid = 1; {s}rready = 1;
  @(posedge clk_main); while (!{s}arready) @(posedge clk_main);
  @(negedge clk_main); {s}arvalid = 0;
  for (k = 0; k < beats; k = k + 1) begin
   @(posedge clk_main); while (!{s}rvalid) @(posedge clk_main);
   check({s}rresp == 0 && {s}rid == 1 && {s}rlast == (k == beats - 1), "CONST.RRESP");
   check({s}rdata === (32'hd0000000 | ((address + 4 * k) & 16'hffff)), "CONST.RDATA");
  end
  @(negedge clk_main); {s}rready = 0;
 end
endtask
initial begin
{chr(10).join(init)}
 repeat (10) @(posedge clk_main);
 @(negedge clk_main); reset_n = 1;
 repeat (20) @(posedge clk_main);
 do_write(32'h40, 1);
 do_read(32'h40, 1);
 do_write(32'h80, 2);
 do_read(32'h80, 2);
 repeat (10) @(posedge clk_main);
 check(aw_count == {requests} && ar_count == {requests}, "CONST.REQUESTS");
 check(w_count == 3 && b_count == {requests} && r_count == 3, "CONST.BEATS");
 $display("NOCLOOM_PASS constant_user {mode} aw=%0d ar=%0d", aw_count, ar_count);
 $finish;
end
endmodule
'''


YAML = '''schema_version: 10
name: constant_user
address_width: 32
reset_ports: [{name: reset_n}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, gated: false}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32}
targets:
  - name: t0
    clock_domain: main
    protocol: PROTOCOL
    data_width: 32
    attributes: amba
    mappings: [{base: '0x0', size: '0x1000'}]
    user: [1, SECOND, 1]
connectivity: all
output: {backend: verilog, language: verilog_2001}
'''


def check_cli(cli, work):
    """The CLI accepts constants and locates a flag name without a declaration."""
    for protocol in ('axi4', 'axi3', 'axi4_lite'):
        text = YAML.replace('PROTOCOL', protocol)
        valid = work / (protocol + '.yaml')
        valid.write_text(text.replace('SECOND', '0'))
        result = subprocess.run([cli, 'validate', valid], capture_output=True, text=True,
                                timeout=120, check=False)
        assert result.returncode == 0 and 'Definition is valid' in result.stdout, (
            protocol, result.stdout, result.stderr)
        output = work / (protocol + '-generated')
        result = subprocess.run([cli, 'generate', valid, '--out-dir', output],
                                capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, (protocol, result.stderr)
        rtl = (output / 'nocloom_constant_user.v').read_text()
        for port in ('nl_t_t0_awuser', 'nl_t_t0_aruser'):
            assert f'output wire [2:0] {port}' in rtl, (protocol, port)
        named = work / (protocol + '-flag.yaml')
        named.write_text(text.replace('SECOND', 'tracked'))
        result = subprocess.run([cli, 'validate', named], capture_output=True, text=True,
                                timeout=120, check=False)
        message = result.stdout + result.stderr
        assert result.returncode == 1, (protocol, result.returncode, message)
        assert 'NCL_USER_FLAGS [targets[0].user[1]]' in message, (protocol, message)
        assert f'{named.name}:15:11:' in message, (protocol, message)
        assert 'NCL_EXCHANGE_COMPOSE' not in message and 'map::at' not in message, message
    print('CONSTANT_USER_PASS cli')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', required=True,
                        choices=['cli', 'axi4', 'axi4_timeout', 'axi3', 'axi4_lite'])
    args = parser.parse_args()
    root = Path(args.work)
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=root))
    if args.mode == 'cli':
        check_cli(args.cli, work)
        return
    mapping, rtl = generate(args.cli, work, definition(args.mode))
    tb = bench(mapping, args.mode)
    for channel, marker in (('aw', 'CONST.AWUSER'), ('ar', 'CONST.ARUSER')):
        execute(work / channel, rtl, tb, lambda log: [], f'nl_t_t0_{channel}user', len(BITS),
                marker)
    print('CONSTANT_USER_PASS ' + args.mode)


if __name__ == '__main__':
    main()
