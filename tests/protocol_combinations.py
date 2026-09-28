"""Behavioral combinations exercised by the AHB and AXI3 CTest entries.

Port faults are fixed negative controls for the independent transaction checks.
Generated configurations, RTL, logs and receipts stay in each test's work directory.
"""

import copy
import hashlib
import json
import subprocess
import traceback

from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def generate(cli, work, config):
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'input.yaml'
    source.write_text(json.dumps(config, indent=2) + '\n')
    output = work / 'generated'
    result = subprocess.run([cli, 'generate', source, '--out-dir', output],
                            capture_output=True, text=True, timeout=300, check=False)
    (work / 'generate.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    stem = 'nocloom_' + config['name']
    return (json.loads((output / (stem + '.map.json')).read_text()),
            generated_rtl(output / (stem + '.v')))


def corrupt_output(tb, signal, width, mask=1, constant=None):
    """Change a DUT output before it reaches the otherwise unchanged checker/model."""
    connection = f'.{signal}({signal})'
    assert tb.count(connection) == 1, signal
    altered = signal + '_fault'
    value = f"{width}'h{constant:x}" if constant is not None else f"{altered} ^ {width}'h{mask:x}"
    declaration = f'wire [{width - 1}:0] {altered};\nassign {signal} = {value};\n'
    tb = tb.replace(connection, f'.{signal}({altered})')
    # Insert after the port nets. An assignment before their declarations can
    # create an implicit one-bit net in a Verilog compiler.
    at = tb.rfind('\n', 0, tb.index(' dut(')) + 1
    return tb[:at] + declaration + tb[at:]


def execute(work, rtl, tb, checker, fault_signal, fault_width, fault_label, completion_signal=None):
    receipts = []
    runs = [('normal', tb, None),
            ('negative', corrupt_output(tb, fault_signal, fault_width), fault_label)]
    if completion_signal:
        runs.append(('negative-completion', corrupt_output(tb, completion_signal, 1, constant=1),
                     'USER.WRITE.COMPLETE'))
    for label, text, expected_fault in runs:
        case = work / label
        case.mkdir(parents=True, exist_ok=True)
        (case / 'network.v').write_text(rtl)
        (case / 'tb.sv').write_text(text)
        rc = simulate(case, case / 'compiled')
        log = (case / 'simulation.log').read_text()
        problems = checker(log)
        (case / 'mismatches.json').write_text(json.dumps(problems, indent=2) + '\n')
        passed = rc == 0 and not problems and 'NOCLOOM_PASS' in log and 'ASSERT_FAIL' not in log
        if label == 'normal':
            assert passed, (work.name, rc, problems[:3], log[-2000:])
        else:
            # A compiler failure raises before here. A timeout or an unrelated failure
            # cannot stand in for this control's intended behavioral observation.
            assert expected_fault in log or any(p.get('check') == expected_fault for p in problems), (
                work.name, 'negative control missed', rc, problems[:3], log[-2000:])
            assert not passed, (work.name, 'negative control survived')
        receipts.append(dict(label=label, simulation_exit=rc, status='PASS' if passed else 'CAUGHT',
                             checks=problems, fault=expected_fault,
                             rtl_sha256=hashlib.sha256(rtl.encode()).hexdigest(),
                             tb_sha256=hashlib.sha256(text.encode()).hexdigest(),
                             log_sha256=hashlib.sha256(log.encode()).hexdigest()))
        print(f'NOCLOOM_PASS combination={work.name} {label} ' + ('PASS' if passed else f'CAUGHT {expected_fault}'))
    (work / 'results.json').write_text(json.dumps(receipts, indent=2) + '\n')


def run_ahb_exact(cli, work):
    import run_ahb_initiator as ahb
    mode = 'exact_read'
    ahb.MODES[mode] = dict(sources={'h0': (32, 'fast')},
                          targets={'tx': ('axi4', 64, 0x10000, 'slow'),
                                   'ta': ('apb4', 32, 0x11000, 'main')},
                          nodes=[('root', 'main'), ('leaf', 'slow')],
                          domains={'main': (2, 10), 'fast': (2, 6), 'slow': (3, 14)})
    config = ahb.configuration(mode)
    config['targets'][0]['read_side_effects'] = True
    mapping, rtl = generate(cli, work, config)
    selected = []
    for name, address, size, hburst, beats in [('byte', 0x10003, 0, 0, 1),
                                              ('split', 0x10004, 2, 3, 4),
                                              ('aligned', 0x10020, 2, 3, 4)]:
        selected.append(dict(name=name, src='h0', op='read', addr=address, size=size,
                             hburst=hburst, beats=beats, prot=3, lock=0, cancel=0,
                             busy=[0] * beats, seed=len(selected) * 29))
    expected = [(3, 0, 0), (4, 0, 2), (8, 0, 3), (16, 0, 2), (32, 1, 3)]

    def check(log):
        problems = ahb.compare(mode, log, selected, [])
        got = [(b['addr'], b['len'], b['size']) for b in ahb.parse(log)['bursts'].get('tx', [])]
        if got != expected:
            problems.append(dict(check='AHB.EXACT.ACCESS', expected=expected, got=got))
        return problems

    width = next(p['width'] for p in mapping['ports'] if p['name'] == 'nl_t_tx_araddr')
    execute(work, rtl, ahb.bench(mode, mapping, selected, []), check,
            'nl_t_tx_araddr', width, 'AHB.EXACT.ACCESS')


def run_axi3_graph(cli, work):
    import run_axi3 as axi3
    mode = 'target_graph'
    axi3.MODES[mode] = copy.deepcopy(axi3.MODES['target'])
    axi3.MODES[mode]['domains'] = {'main': 8, 'fast': 10, 'slow': 14, 'far': 18}
    config = axi3.configuration(mode)
    config['clock_domains'] = [dict(name=n, clock_port='clk_' + n, reset_port='reset_n',
                                     test_port='test', period_ps=int(p * 1000))
                               for n, p in axi3.MODES[mode]['domains'].items()]
    for item, domain in zip(config['initiators'], ['fast', 'slow', 'main']):
        item['clock_domain'] = domain
    for item, domain in zip(config['targets'], ['far', 'slow', 'far', 'main']):
        item['clock_domain'] = domain
    req = dict(nodes=[dict(name='root', clock_domain='main'), dict(name='leaf', clock_domain='far')],
               links=[{'from': 'root', 'to': 'leaf'}],
               entries=[dict(endpoint=x['name'], node='root') for x in config['initiators']],
               exits=[dict(endpoint=x['name'], node='leaf') for x in config['targets']])
    rsp = copy.deepcopy(req)
    rsp['links'] = [{'from': 'leaf', 'to': 'root'}]
    rsp['entries'], rsp['exits'] = copy.deepcopy(req['exits']), copy.deepcopy(req['entries'])
    config['topology'] = dict(request=req, response=rsp)
    mapping, rtl = generate(cli, work, config)
    every = axi3.directed(mode)
    for k, txn in enumerate(every):
        txn['seed'] = k * 29 & 255
    ports = {p['name']: p for p in mapping['ports']}
    execute(work, rtl, axi3.bench(mode, mapping, every), lambda log: axi3.compare(mode, log, every),
            'nl_t_t3_wid', ports['nl_t_t3_wid']['width'], 'AXI3.WID')


def user_configuration(label):
    lite = label == 'lite_split_buser'
    exact = label == 'exact_user'
    config = dict(schema_version=10, name=label, address_width=32,
                  reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
                  clock_domains=[dict(name=n, clock_port='clk_' + n, reset_port='reset_n',
                                      test_port='test', period_ps=p)
                                 for n, p in [('main', 10000), ('far', 14000)]],
                  initiators=[dict(name='s0', clock_domain='main', protocol='axi4', data_width=32,
                                   id_width=2, len_width=8, user_width=3, response_user_width=4,
                                   user_flags={'tag': 'user[0]', 'guard': '~user[1]', 'mode': 'user[2]'})],
                  targets=[dict(name='t0', clock_domain='far', protocol='axi4_lite' if lite else 'axi4',
                                data_width=64 if exact else 32, attributes='amba',
                                mappings=[dict(base='0x0', size='0x1000')],
                                user=['guard', '~tag', 'mode', 1], response_user_width=3)],
                  transport=dict(transport_word_bytes=4, user_flags=['tag', 'guard', 'mode'],
                                 response_user_bits=2), connectivity='all',
                  output=dict(backend='verilog', language='verilog_2001'))
    if not lite:
        config['targets'][0].update(id_width=2, len_width=8)
    if exact:
        config['targets'][0]['read_side_effects'] = True
    if lite:
        config['transport']['packet_limits'] = [dict(source='s0', target='t0', max_bytes=8)]
    req = dict(nodes=[dict(name='near', clock_domain='main'), dict(name='far', clock_domain='far')],
               links=[{'from': 'near', 'to': 'far'}], entries=[dict(endpoint='s0', node='near')],
               exits=[dict(endpoint='t0', node='far')])
    rsp = copy.deepcopy(req)
    rsp['links'] = [{'from': 'far', 'to': 'near'}]
    rsp['entries'], rsp['exits'] = copy.deepcopy(req['exits']), copy.deepcopy(req['entries'])
    config['topology'] = dict(request=req, response=rsp)
    return config


def run_user_case(cli, case, label):
    config = user_configuration(label)
    mapping, rtl = generate(cli, case, config)
    tb = user_bench(mapping, label)
    field = 'buser' if label == 'lite_split_buser' else 'aruser'
    signal = ('nl_i_s0_' if field == 'buser' else 'nl_t_t0_') + field
    width = next(p['width'] for p in mapping['ports'] if p['name'] == signal)
    marker = 'USER.BUSER' if field == 'buser' else 'USER.ARUSER'
    completion = 'nl_i_s0_bvalid' if label == 'lite_split_buser' else None
    execute(case, rtl, tb, lambda log: [], signal, width, marker, completion)


def tail_configuration():
    """A 32-bit Lite target with error codes and three packets in flight behind an AXI4 source
    keeping eight bursts."""
    return dict(schema_version=10, name='lite_tail', address_width=32,
                reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
                clock_domains=[dict(name='main', clock_port='clk_main', reset_port='reset_n',
                                    test_port='test')],
                initiators=[dict(name='s0', clock_domain='main', protocol='axi4', data_width=32,
                                 id_width=2, len_width=8, max_outstanding=8)],
                targets=[dict(name='t0', clock_domain='main', protocol='axi4_lite', data_width=32,
                              attributes='amba', max_outstanding=3,
                              mappings=[dict(base='0x0', size='0x1000')])],
                transport=dict(response_codes='distinct'), connectivity='all',
                output=dict(backend='verilog', language='verilog_2001'))


# Writes (address, beats, AxBURST, expected BRESP). The FIXED burst answers SLVERR at the outlet
# without access; the target answers SLVERR at local 0xc. Mapped words reach the target in order.
TAIL_WRITES = [(0x0, 1, 1, 0), (0x4, 1, 1, 0), (0x8, 1, 1, 0), (0x14, 1, 1, 0), (0x20, 2, 0, 2),
               (0x18, 1, 1, 0), (0xC, 1, 1, 2)]


def tail_bench(mapping):
    """The source holds BREADY low while the target keeps taking words: answered packets wait
    in the outlet, a packet answered without access waits behind them while the B of a later
    packet arrives, and that B must stay with its own packet. Then the source takes the B's in
    order and every word reaches the target."""
    ports = {p['name']: p for p in mapping['ports']}
    decl, conns, init = [], [], []
    for name, p in ports.items():
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f'{kind} [{p["width"] - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and p.get('signal') != 'clock' and not name.startswith('nl_t_'):
            init.append(f' {name} = 0;')
    s, t = 'nl_i_s0_', 'nl_t_t0_'
    n = len(TAIL_WRITES)
    words = [a for a, beats, burst, _ in TAIL_WRITES if burst for a in [a + 4 * k for k in range(beats)]]
    m = len(words)
    tables = ' '.join(f'b_addr[{k}] = {a}; b_beats[{k}] = {b}; b_burst[{k}] = {u}; b_resp[{k}] = {r};'
                      for k, (a, b, u, r) in enumerate(TAIL_WRITES))
    tables += ' ' + ' '.join(f'want_addr[{k}] = {a};' for k, a in enumerate(words))
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0; always #5 clk_main = ~clk_main;
integer cycle = 0;
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 5000) begin $display("ASSERT_FAIL:TAIL.TIMEOUT"); $fatal(1); end
end
task automatic check(input condition, input [8*40-1:0] label_);
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
integer b_addr [0:{n - 1}], b_beats [0:{n - 1}], b_burst [0:{n - 1}], b_resp [0:{n - 1}];
integer want_addr [0:{m - 1}];
reg aw_have = 0, w_have = 0, b_pending = 0;
reg [31:0] aw_addr, w_data;
integer accesses = 0, b_seen = 0, quiet = 0;
always_comb begin
 {t}awready = !aw_have && !b_pending;
 {t}wready = !w_have && !b_pending;
 {t}bvalid = b_pending;
 {t}bresp = aw_addr == 32'hc ? 2 : 0;
 {t}arready = 0; {t}rvalid = 0; {t}rresp = 0; {t}rdata = 0;
end
always @(posedge clk_main) if (!reset_n) begin
 aw_have <= 0; w_have <= 0; b_pending <= 0;
end else begin
 if ({t}awvalid && {t}awready) begin aw_have <= 1; aw_addr <= {t}awaddr; end
 if ({t}wvalid && {t}wready) begin w_have <= 1; w_data <= {t}wdata; end
 if (aw_have && w_have && !b_pending) begin
  check(accesses < {m} && aw_addr == want_addr[accesses], "TAIL.ACCESS");
  check(w_data == (32'h7a000000 | aw_addr), "TAIL.WDATA");
  $display("EV TAIL ACCESS %0d %h", accesses, aw_addr);
  accesses <= accesses + 1; b_pending <= 1; quiet <= cycle;
 end
 if ({t}bvalid && {t}bready) begin aw_have <= 0; w_have <= 0; b_pending <= 0; end
 check({t}arvalid !== 1'b1, "TAIL.READ");
end
always @(posedge clk_main) if (reset_n && {s}bvalid && {s}bready) begin
 check(b_seen < {n} && {s}bresp == b_resp[b_seen] && {s}bid == 1, "TAIL.BRESP");
 $display("EV TAIL B %0d %0d", b_seen, {s}bresp);
 b_seen <= b_seen + 1;
end
initial begin : drive
 integer a, k, j;
{chr(10).join(init)}
 {tables}
 repeat(10) @(posedge clk_main);
 @(negedge clk_main); reset_n = 1;
 repeat(10) @(posedge clk_main);
 fork
  for (a = 0; a < {n}; a = a + 1) begin
   @(negedge clk_main); {s}awaddr = b_addr[a]; {s}awlen = b_beats[a] - 1; {s}awsize = 2;
   {s}awburst = b_burst[a]; {s}awid = 1; {s}awvalid = 1;
   @(posedge clk_main); while (!{s}awready) @(posedge clk_main);
   @(negedge clk_main); {s}awvalid = 0;
  end
  for (k = 0; k < {n}; k = k + 1)
   for (j = 0; j < b_beats[k]; j = j + 1) begin
    @(negedge clk_main); {s}wvalid = 1; {s}wstrb = 4'hf; {s}wlast = j == b_beats[k] - 1;
    {s}wdata = 32'h7a000000 | (b_addr[k] + 4 * j * b_burst[k]);
    @(posedge clk_main); while (!{s}wready) @(posedge clk_main);
    @(negedge clk_main); {s}wvalid = 0;
   end
  // The source takes no B until the target has seen no new word for 40 cycles: the answered
  // packets fill the response path and the outlet holds the rest. One B then frees one place,
  // so the outlet answers its head into an empty stage that cannot leave and the next packet
  // (answered without access) waits behind it while a later B reaches the target.
  begin
   wait (cycle > quiet + 40);
   check(b_seen == 0 && accesses > 1, "TAIL.EARLY");
   @(negedge clk_main); {s}bready = 1;
   wait (b_seen == 1);
   @(negedge clk_main); {s}bready = 0;
   repeat(40) @(posedge clk_main);
   @(negedge clk_main); {s}bready = 1;
  end
 join
 wait (b_seen == {n});
 repeat(20) @(posedge clk_main);
 check(b_seen == {n} && accesses == {m} && !b_pending, "TAIL.COUNT");
 $display("NOCLOOM_PASS combination lite_tail accesses=%0d b=%0d", accesses, b_seen);
 $finish;
end
endmodule
'''


def run_tail_case(cli, case):
    mapping, rtl = generate(cli, case, tail_configuration())
    width = next(p['width'] for p in mapping['ports'] if p['name'] == 'nl_i_s0_bresp')
    execute(case, rtl, tail_bench(mapping), lambda log: [], 'nl_i_s0_bresp', width, 'TAIL.BRESP')


def run_ahb_combinations(cli, work):
    # Preserve every independent reproduction if a product or harness failure is
    # found. The original CTest entry still fails after collecting the other cases.
    outcomes = []
    for label in ('ahb_exact', 'cdc_user', 'lite_split_buser', 'exact_user', 'lite_tail'):
        case = work / label
        try:
            if label == 'ahb_exact':
                run_ahb_exact(cli, case)
            elif label == 'lite_tail':
                run_tail_case(cli, case)
            else:
                run_user_case(cli, case, label)
            outcomes.append(dict(case=label, status='PASS'))
        except (AssertionError, RuntimeError, subprocess.TimeoutExpired) as error:
            case.mkdir(parents=True, exist_ok=True)
            (case / 'failure.txt').write_text(traceback.format_exc())
            outcomes.append(dict(case=label, status='FAIL', error=str(error)))
    (work / 'outcomes.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    assert all(x['status'] == 'PASS' for x in outcomes), outcomes


def user_bench(mapping, label):
    """A serial manager and stalled target check every accepted request and response.

    The read oracle uses requested byte addresses, never observed target descriptors.
    RUSER changes per word and the source retains each packet's first RUSER.
    Lite BUSER changes per word: each eight-byte packet answers its first word's BUSER
    and the source retains the final packet's, resized through two bits.
    """
    lite, exact = label == 'lite_split_buser', label == 'exact_user'
    ports = {p['name']: p for p in mapping['ports']}
    decl, conns, init = [], [], []
    for name, p in ports.items():
        kind = 'reg' if p['direction'] == 'input' else 'wire'
        decl.append(f'{kind} [{p["width"] - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and p.get('signal') != 'clock':
            init.append(f' {name} = 0;')
    t, s = 'nl_t_t0_', 'nl_i_s0_'
    T = 8 if exact else 4
    targets = ''
    if not lite:
        targets += f'{t}rid = saved_rid; {t}rlast = read_index == read_len; {t}bid = saved_wid;'
    read_len = '0' if lite else t + 'arlen'
    read_size = '2' if lite else t + 'arsize'
    read_id = '0' if lite else t + 'arid'
    write_len = '0' if lite else t + 'awlen'
    write_id = '0' if lite else t + 'awid'
    read_check = '' if lite else f'check({t}arburst == 1, "USER.ARBURST");'
    if exact:
        read_check += f'''
   case (ar_count)
    0: check({{ {t}araddr, {t}arlen, {t}arsize }} == {{32'h23, 8'd0, 3'd0}}, "USER.EXACT.ACCESS");
    1: check({{ {t}araddr, {t}arlen, {t}arsize }} == {{32'h24, 8'd0, 3'd2}}, "USER.EXACT.ACCESS");
    2: check({{ {t}araddr, {t}arlen, {t}arsize }} == {{32'h28, 8'd0, 3'd1}}, "USER.EXACT.ACCESS");
    default: check(0, "USER.EXACT.COUNT");
   endcase
'''
    ops = f'''do_write(32'h40, 8, 3'b101);
 do_write(32'h80, 8, 3'b010);
 check(aw_count == 16 && w_count == 16 && b_count == 16, "USER.LITE.ACCESS_COUNT");''' if lite else (
        '''do_read(32'h23, 4, 1, 3'b101);
 check(ar_count == 3, "USER.EXACT.COUNT");''' if exact else '''
 do_write(32'h40, 4, 3'b101);
 do_read(32'h40, 4, 2, 3'b010);
 do_write(32'h80, 4, 3'b110);
 do_read(32'h80, 4, 2, 3'b001);
 check(ar_count == 2 && aw_count == 2 && w_count == 8 && b_count == 2, "USER.CDC.ACCESS_COUNT");''')
    # Exact blocks at 0x23,0x24,0x28 have RUSER 0,1,2. The source's
    # unaligned first halfword contains one byte; the next three use 1,1,2.
    expected_ru = "(k == 0 ? 0 : k == 3 ? 2 : 1)" if exact else '((address >> 2) & 3)'
    # The B word whose BUSER the source sees: the final packet's first word at a Lite
    # target, the burst's last beat at an AXI4 one.
    answer_beat = 'beats-2' if lite else 'beats-1'
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0; always #5 clk_main = ~clk_main;
initial clk_far = 0; always #7 clk_far = ~clk_far;
integer cycle = 0, target_cycle = 0;
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 20000) begin $display("ASSERT_FAIL:USER.TIMEOUT"); $fatal(1); end
end
task automatic check(input condition, input [8*40-1:0] label_);
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:%0s cycle=%0d", label_, cycle); $fatal(1); end
endtask
function automatic [3:0] map_user(input [2:0] u);
 map_user = {{1'b1, u[2], ~u[0], ~u[1]}};
endfunction
function automatic [31:0] word(input [31:0] a);
 integer lane; begin word = 0;
 for (lane=0;lane<4;lane=lane+1) word[8*lane+:8] = ((a & ~3) + lane) * 7 + 8'h31;
 end
endfunction
reg read_active = 0, read_offer = 0, write_active = 0, b_pending = 0;
integer read_index = 0, read_len = 0, read_size = 0, write_index = 0, write_len = 0;
integer ar_count = 0, aw_count = 0, w_count = 0, b_count = 0;
reg [31:0] read_address, write_address; reg [7:0] saved_rid, saved_wid;
reg [2:0] response_buser;
reg [3:0] expected_user;
always @* begin : target_outputs
 integer lane;
 {t}awready = !write_active && !b_pending && target_cycle % 4 == 1;
 {t}wready = write_active && target_cycle % 3 == 1;
 {t}bvalid = b_pending;
 {t}bresp = 0; {t}buser = response_buser;
 {t}arready = !read_active && target_cycle % 4 == 2;
 {t}rvalid = read_offer;
 {t}rresp = 0; {t}ruser = 4 | ((read_address >> 2) & 3);
 {t}rdata = 0;
 for (lane=0;lane<{T};lane=lane+1)
  {t}rdata[8*lane+:8] = ((read_address & ~{T - 1}) + lane) * 7 + 8'h31;
 {targets}
end
always @(posedge clk_far) if (!reset_n) begin
 read_active <= 0; read_offer <= 0; write_active <= 0; b_pending <= 0;
 target_cycle <= 0; ar_count <= 0; aw_count <= 0; w_count <= 0; b_count <= 0;
end else begin
 target_cycle <= target_cycle + 1;
 if (read_active && !read_offer && target_cycle % 3 == 1) read_offer <= 1;
 if ({t}arvalid && {t}arready) begin
  check({t}aruser === expected_user, "USER.ARUSER");
  {read_check}
  $display("EV USER AR %h %h", {t}araddr, {t}aruser);
  ar_count <= ar_count + 1; read_active <= 1; read_address <= {t}araddr;
  read_len <= {read_len}; read_size <= {read_size}; read_index <= 0; saved_rid <= {read_id};
 end
 if ({t}rvalid && {t}rready) begin
  read_offer <= 0;
  if (read_index == read_len) read_active <= 0;
  else begin read_index <= read_index + 1; read_address <= (read_address & ~((1 << read_size) - 1)) + (1 << read_size); end
 end
 if ({t}awvalid && {t}awready) begin
  check({t}awuser === expected_user, "USER.AWUSER");
  $display("EV USER AW %h %h", {t}awaddr, {t}awuser);
  aw_count <= aw_count + 1; write_active <= 1; write_address <= {t}awaddr;
  write_len <= {write_len}; write_index <= 0; saved_wid <= {write_id};
 end
 if ({t}wvalid && {t}wready) begin
  check({t}wstrb == 4'hf && {t}wdata === (32'habc00000 | (write_address >> 2)), "USER.WDATA");
  w_count <= w_count + 1;
  if (write_index == write_len) begin
   write_active <= 0; b_pending <= 1; response_buser <= 4 | ((write_address >> 2) & 3);
  end else begin write_index <= write_index + 1; write_address <= write_address + 4; end
 end
 if ({t}bvalid && {t}bready) begin b_pending <= 0; b_count <= b_count + 1; end
end
task automatic do_write(input [31:0] address, input integer beats, input [2:0] u);
 integer k, want_aw, want_w, want_b; reg [3:0] want;
 begin
  want_aw = aw_count + {'beats' if lite else '1'};
  want_w = w_count + beats;
  want_b = b_count + {'beats' if lite else '1'};
  expected_user = map_user(u);
  @(negedge clk_main); {s}awaddr=address; {s}awlen=beats-1; {s}awsize=2; {s}awburst=1;
  {s}awid=2; {s}awuser=u; {s}awvalid=1; {s}bready=0;
  @(posedge clk_main); while (!{s}awready) @(posedge clk_main);
  @(negedge clk_main); {s}awvalid=0; {s}awuser=~u;
  for(k=0;k<beats;k=k+1) begin
   {s}wvalid=1; {s}wdata=32'habc00000 | ((address+4*k)>>2); {s}wstrb=15; {s}wlast=k==beats-1;
   @(posedge clk_main); while(!{s}wready) @(posedge clk_main);
   @(negedge clk_main); {s}wvalid=0;
  end
  @(posedge clk_main); while(!{s}bvalid) @(posedge clk_main);
  check(aw_count == want_aw && w_count == want_w && b_count == want_b,
        "USER.WRITE.COMPLETE");
  check(!write_active && !b_pending, "USER.WRITE.PENDING");
  $display("EV USER COMPLETE aw=%0d w=%0d b=%0d", aw_count, w_count, b_count);
  want = ((address+4*({answer_beat}))>>2)&3;
  repeat(4) begin
   check({s}bvalid && {s}bresp==0 && {s}bid==2, "USER.BRESP");
   check({s}buser === want, "USER.BUSER");
   @(posedge clk_main);
  end
  $display("EV USER B %h", {s}buser);
  @(negedge clk_main); {s}bready=1;
  @(posedge clk_main); @(negedge clk_main); {s}bready=0;
 end
endtask
task automatic do_read(input [31:0] address, input integer beats, input integer size, input [2:0] u);
 integer k, lane, a, low, amount; reg [31:0] mask, want_data; reg [3:0] want_user;
 begin
  expected_user = map_user(u);
  @(negedge clk_main); {s}araddr=address; {s}arlen=beats-1; {s}arsize=size; {s}arburst=1;
  {s}arid=1; {s}aruser=u; {s}arvalid=1; {s}rready=0;
  @(posedge clk_main); while(!{s}arready) @(posedge clk_main);
  @(negedge clk_main); {s}arvalid=0; {s}aruser=~u;
  for(k=0;k<beats;k=k+1) begin
   @(posedge clk_main); while(!{s}rvalid) @(posedge clk_main);
   a = k==0 ? address : (address & ~((1<<size)-1)) + k*(1<<size);
   low = a%4; amount = (1<<size) - (a% (1<<size));
   mask=0; for(lane=0;lane<amount;lane=lane+1) mask=mask | (32'hff << (8*(low+lane)));
   want_data=word(a); want_user={expected_ru};
   repeat(3) begin
    check({s}rvalid && {s}rresp==0 && {s}rid==1 && {s}rlast==(k==beats-1), "USER.RRESP");
    check(({s}rdata & mask) === (want_data & mask), "USER.RDATA");
    check({s}ruser === want_user, "USER.RUSER");
    @(posedge clk_main);
   end
   $display("EV USER R %0d %h %h",k,{s}rdata,{s}ruser);
   @(negedge clk_main); {s}rready=1;
   @(posedge clk_main); @(negedge clk_main); {s}rready=0;
  end
 end
endtask
initial begin
{chr(10).join(init)}
 repeat(10) @(posedge clk_main);
 @(negedge clk_main); reset_n=1;
 repeat(20) @(posedge clk_main);
 {ops}
 repeat(10) @(posedge clk_main);
 check(!write_active && !b_pending, "USER.WRITE.PENDING");
 $display("NOCLOOM_PASS combination {label} ar=%0d aw=%0d w=%0d b=%0d", ar_count,aw_count,w_count,b_count);
 $finish;
end
endmodule
'''
