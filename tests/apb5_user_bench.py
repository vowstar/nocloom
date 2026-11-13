"""APB5 request and response user transactions through the public CLI."""
import json
import copy
from pathlib import Path
import subprocess
import tempfile

from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def configuration(source, target, clocked, request, response, transport, back_to_back=False):
    flags = [f'u{k}' for k in range(request)]
    domains = ['main', 'target'] if clocked else ['main']
    cfg = {
        'schema_version': 10, 'name': 'apb5_user', 'address_width': 32,
        'clock_gating': 'none', 'reset_ports': [{'name': 'reset_n'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': 'reset_n'}
                          for d in domains],
        'initiators': [{'name': 's', 'clock_domain': 'main', 'protocol': source,
                        'data_width': 32, 'user_width': request,
                        'response_user_width': response,
                        'user_flags': {f: f'user[{k}]' for k, f in enumerate(flags)}}],
        'targets': [{'name': 't', 'clock_domain': domains[-1], 'protocol': target,
                     'data_width': 32, 'attributes': 'amba', 'user': flags,
                     'response_user_width': response,
                     'mappings': [{'base': '0x1000', 'size': '0x1000', 'local_address': '0x1000'}]}],
        'transport': {'user_flags': flags, 'response_user_bits': transport},
        'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if source in ('axi4', 'axi3'):
        cfg['initiators'][0].update(id_width=0, len_width=4)
        if not request:
            cfg['initiators'][0].pop('user_width')
        if not response:
            cfg['initiators'][0].pop('response_user_width')
    if target in ('axi4', 'axi3'):
        cfg['targets'][0].update(id_width=0, len_width=4)
        if not response:
            cfg['targets'][0].pop('response_user_width')
    if target == 'apb4':
        cfg['targets'][0].pop('user')
        cfg['targets'][0].pop('response_user_width')
    if source == 'apb4':
        cfg['initiators'][0].pop('user_width')
        cfg['initiators'][0].pop('response_user_width')
        cfg['initiators'][0]['user_flags'] = {f: k % 2 for k, f in enumerate(flags)}
    if target == 'apb5':
        cfg['targets'][0]['apb_back_to_back'] = back_to_back
    if clocked:
        cfg['topology'] = {}
        for direction, entry, exit_ in [('request', 's', 't'), ('response', 't', 's')]:
            cfg['topology'][direction] = {
                'nodes': [{'name': 'q', 'clock_domain': 'main'}], 'links': [],
                'entries': [{'endpoint': entry, 'node': 'q'}],
                'exits': [{'endpoint': exit_, 'node': 'q'}]}
    return cfg


def user_value(index, width):
    # Each 32-bit group differs, including bits above a machine word.
    return sum((((index * 0x13579b + k * 0x2345 + 0xa53c) & 0xffffffff) << k)
               for k in range(0, width, 32)) & ((1 << width) - 1)


def burst_extension(request, source_response, target_response, transport, split):
    def response(n):
        value = ([0x11, 0x22, 0x44, 0x88][n - 32] if n < 36 else
                 0xa251 if n < 40 else 0x9631 ^ (n * 0x1a3))
        return value & ((1 << min(source_response, target_response, transport)) - 1)
    request_drive = ''
    if request:
        request_drive = (f"nl_i_s_awuser = {request}'h{user_value(32, request):x}; "
                         f"nl_i_s_aruser = {request}'h{user_value(40, request):x};")
    check_b = (f"check(nl_i_s_buser == (packet == 0 ? 32'h{response(34 if split else 32):x} : 32'h{response(36):x}), \"BURST_BUSER\");"
               if source_response else '')
    check_r = (f"check(nl_i_s_ruser == (k < 2 ? 32'h{response(40):x} : 32'h{response(42 if split else 40):x}), \"BURST_RUSER_FIRST\");"
               if source_response else '')
    return f'''
task burst_transfers;
 integer k, packet;
 begin
 for (packet = 0; packet < 2; packet = packet + 1) begin
 @(negedge clk_main); {request_drive}
 nl_i_s_awaddr = 'h1080 + packet*16; nl_i_s_awprot = 0; nl_i_s_awsize = 2;
 nl_i_s_awburst = 1; nl_i_s_awlen = 3; nl_i_s_awvalid = 1;
 @(posedge clk_main); while (!nl_i_s_awready) @(posedge clk_main);
 @(negedge clk_main); nl_i_s_awvalid = 0;
 for (k = 0; k < 4; k = k + 1) begin
  nl_i_s_wdata = 'h12340020 + packet*4 + k; nl_i_s_wstrb = packet == 1 && k == 0 ? 0 : 4'hb;
  nl_i_s_wlast = k == 3; nl_i_s_wvalid = 1;
  @(posedge clk_main); while (!nl_i_s_wready) @(posedge clk_main);
  @(negedge clk_main); nl_i_s_wvalid = 0;
 end
 nl_i_s_bready = 1;
 @(posedge clk_main); while (!nl_i_s_bvalid) @(posedge clk_main);
 check(nl_i_s_bresp == 0, "BURST_BRESP"); {check_b}
 @(negedge clk_main); nl_i_s_bready = 0;
 end
 nl_i_s_araddr = 'h10a0; nl_i_s_arprot = 0; nl_i_s_arsize = 2;
 nl_i_s_arburst = 1; nl_i_s_arlen = 3; nl_i_s_arvalid = 1;
 @(posedge clk_main); while (!nl_i_s_arready) @(posedge clk_main);
 @(negedge clk_main); nl_i_s_arvalid = 0;
 // Hold responses so the target must retain queued user values.
 repeat (80) @(negedge clk_main);
 nl_i_s_rready = 1;
 for (k = 0; k < 4; k = k + 1) begin
  @(posedge clk_main); while (!nl_i_s_rvalid) @(posedge clk_main);
  check(nl_i_s_rresp == 0 && nl_i_s_rlast == (k == 3), "BURST_RRESP");
  check(nl_i_s_rdata == 'h789a0028 + k, "BURST_RDATA"); {check_r}
 end
 @(negedge clk_main); nl_i_s_rready = 0;
 end
endtask
'''


def bench(mapping, cfg):
    ports = {p['name']: p for p in mapping['ports']}
    source, target = cfg['initiators'][0], cfg['targets'][0]
    sa, ta = source['protocol'].startswith('apb'), target['protocol'].startswith('apb')
    req, rsp = len(cfg['transport']['user_flags']), cfg['transport']['response_user_bits']
    sb, tb = source.get('response_user_width', 0), target.get('response_user_width', 0)
    if not ta and 'nl_t_t_buser' not in ports:
        tb = 0
    tw = len(target.get('user', []))
    decl, init = [], []
    for name, p in ports.items():
        decl.append(f'{"reg" if p["direction"] == "input" else "wire"} [{p["width"]-1}:0] {name};')
        if p['direction'] == 'input':
            init.append(f'{name} = 0;')
    conns = ', '.join(f'.{n}({n})' for n in ports)
    clk_t = 'clk_target' if len(cfg['clock_domains']) > 1 else 'clk_main'
    clocks = 'always #5 clk_main = ~clk_main;'
    if clk_t != 'clk_main':
        clocks += '\nalways #7 clk_target = ~clk_target;'
    lanes = target['data_width'] // 8 if ta else 4
    burst = not sa and target['protocol'] == 'apb5' and lanes == 4
    split = bool(cfg['transport'].get('packet_limits'))
    expected_request = []
    def mapped_request(value):
        out = 0
        for k, driver in enumerate(target.get('user', [])):
            if isinstance(driver, int):
                bit = driver
            else:
                invert = driver.startswith('~')
                bit = ((value >> int(driver.lstrip('~')[1:])) & 1) ^ invert
            out |= bit << k
        return out
    calls = []
    for n in range(18):
        # Read and write alternate without an idle cycle on the APB source.
        value = user_value(n, req)
        if source['protocol'] == 'apb4':
            value = sum((k % 2) << k for k in range(req))
        expected_request.append(f'{n}: expected_request = {max(1, tw)}\'h{mapped_request(value):x};')
        result = ((0x9631 ^ (n * 0x1a3)) & ((1 << tb) - 1)) if tb else 0
        result &= (1 << rsp) - 1
        result &= (1 << sb) - 1
        calls.append(f'xfer({n}, {n % 2}, {max(1, req)}\'h{value:x}, {max(1, sb)}\'h{result:x}, 4\'hb);')
    if burst:
        for n in (32, 40):
            expected_request.append(f"{n}: expected_request = {max(1, tw)}'h{mapped_request(user_value(n, req)):x};")
    # A silent write follows a nonzero response and must not select the target.
    if target['protocol'] == 'apb5':
        calls.append(f"xfer(19, 1, {max(1, req)}'h0, {max(1, sb)}'h0, 0);")
    set_request = ''
    if source.get('user_width', 0):
        set_request = ('nl_i_s_pauser = user;' if sa else
                       'nl_i_s_awuser = user; nl_i_s_aruser = user;')
    source_user_check = ''
    if sb:
        source_user_check = ('check(nl_i_s_pbuser == expected_user, "PBUSER");' if sa else
                            'if (write) check(nl_i_s_buser == expected_user, "BUSER"); '
                            'else check(nl_i_s_ruser == expected_user, "RUSER");')
    target_user_check = ''
    if tw:
        actual = 'nl_t_t_pauser' if ta else '(write ? nl_t_t_awuser : nl_t_t_aruser)'
        target_user_check = f'check({actual} == expected_request(index >= 40 ? 40 : index >= 32 ? 32 : index), "PAUSER");'
    drive_response = ''
    if tb:
        drive_response = ('nl_t_t_pbuser = response_value;' if ta else
                          'nl_t_t_buser = response_value; nl_t_t_ruser = response_value;')
    if sa:
        task = f'''
 @(negedge clk_main);
 nl_i_s_psel = 1; nl_i_s_penable = 0; nl_i_s_paddr = 'h1000 + 4*index;
 nl_i_s_pwrite = write; nl_i_s_pwdata = 'h12340000 + index;
 nl_i_s_pstrb = write ? strobes : 0; nl_i_s_pprot = index % 8; {set_request}
 @(negedge clk_main); nl_i_s_penable = 1;
 @(posedge clk_main); while (!nl_i_s_pready) @(posedge clk_main);
 check(nl_i_s_pslverr == (index % 7 == 3), "PSLVERR");
 if (!write) check(nl_i_s_prdata == (index % 7 == 3 ? 0 : 'h789a0000 + index), "PRDATA");
 {source_user_check}
'''
    else:
        task = f'''
 @(negedge clk_main); {set_request}
 if (write) begin
  nl_i_s_awaddr = 'h1000 + 4*index; nl_i_s_awprot = index % 8;
  nl_i_s_awsize = 2; nl_i_s_awburst = 1; nl_i_s_awvalid = 1;
  nl_i_s_wdata = 'h12340000 + index; nl_i_s_wstrb = strobes;
  nl_i_s_wlast = 1; nl_i_s_wvalid = 1;
  fork
   begin @(posedge clk_main); while (!nl_i_s_awready) @(posedge clk_main);
    @(negedge clk_main); nl_i_s_awvalid = 0; end
   begin @(posedge clk_main); while (!nl_i_s_wready) @(posedge clk_main);
    @(negedge clk_main); nl_i_s_wvalid = 0; end
  join
  repeat (5) @(negedge clk_main);
  nl_i_s_bready = 1;
  @(posedge clk_main); while (!nl_i_s_bvalid) @(posedge clk_main);
  check(nl_i_s_bresp == (index % 7 == 3 ? 2 : 0), "BRESP");
 end else begin
  nl_i_s_araddr = 'h1000 + 4*index; nl_i_s_arprot = index % 8;
  nl_i_s_arsize = 2; nl_i_s_arburst = 1; nl_i_s_arvalid = 1;
  @(posedge clk_main); while (!nl_i_s_arready) @(posedge clk_main);
  @(negedge clk_main); nl_i_s_arvalid = 0;
  repeat (5) @(negedge clk_main);
  nl_i_s_rready = 1;
  @(posedge clk_main); while (!nl_i_s_rvalid) @(posedge clk_main);
  check(nl_i_s_rresp == (index % 7 == 3 ? 2 : 0), "RRESP");
  check(nl_i_s_rdata == (index % 7 == 3 ? 0 : 'h789a0000 + index), "RDATA");
 end
 {source_user_check}
 @(negedge clk_main); nl_i_s_bready = 0; nl_i_s_rready = 0;
'''
    if ta:
        target_model = f'''
integer waited = 0;
reg busy = 0;
reg [31:0] saved_address;
reg [2:0] saved_prot;
reg saved_write;
reg [{max(1,tw)-1}:0] saved_user;
always @(negedge {clk_t}) begin
 nl_t_t_pready = 0;
 if (!reset_n) begin waited = 0; busy = 0; end
 else if (nl_t_t_psel) begin
  index = (nl_t_t_paddr - 'h1000) / 4;
  write = nl_t_t_pwrite;
  {target_user_check}
  if (!busy) begin
   check(!nl_t_t_penable, "SETUP");
   saved_address = nl_t_t_paddr; saved_write = write; saved_prot = nl_t_t_pprot;
   {"saved_user = nl_t_t_pauser;" if tw else ""}
   busy = 1; waited = 0;
  end else begin
   check(nl_t_t_paddr == saved_address && write == saved_write && nl_t_t_pprot == saved_prot,
         "REQUEST_STABLE");
   {"check(nl_t_t_pauser == saved_user, \"PAUSER_STABLE\");" if tw else ""}
  end
  check(nl_t_t_pprot == (index >= 32 ? 0 : index % 8), "PPROT");
  check(nl_t_t_pstrb == (write ? ((4'hb >> (nl_t_t_paddr % 4)) & {lanes}'h{(1<<lanes)-1:x}) : 0), "PSTRB");
  if (write) check(nl_t_t_pwdata == ((('h12340000 + index) & 32'hff00ffff) >> (8*(nl_t_t_paddr % 4)) & {target['data_width']}'h{(1<<target['data_width'])-1:x}), "PWDATA");
  if (nl_t_t_penable) begin
   waited = waited + 1;
   nl_t_t_pready = waited > (index % 3 == 0 ? 37 : index % 3);
  end
  nl_t_t_prdata = ('h789a0000 + index) >> (8*(nl_t_t_paddr % 4));
  nl_t_t_pslverr = index < 32 && index % 7 == 3;
  response_value = index == 32 ? 16'h0011 : index == 33 ? 16'h0022 :
                   index == 34 ? 16'h0044 : index == 35 ? 16'h0088 :
                   index >= 36 && index < 40 ? 16'ha251 : 16'h9631 ^ (index * 'h1a3);
  // Response user is meaningful only at completion.
  if (!nl_t_t_pready) response_value = ~response_value ^ waited;
  {drive_response}
 end
end
always @(posedge {clk_t}) begin
 if (reset_n && nl_t_t_psel && nl_t_t_penable && nl_t_t_pready) begin
  completed = completed + 1; busy = 0;
 end
end
'''
    else:
        target_model = f'''
reg write_pending = 0, read_pending = 0;
integer write_index = 0, read_index = 0, wwait = 0, rwait = 0;
always @(negedge {clk_t}) begin
 nl_t_t_awready = !write_pending; nl_t_t_wready = write_pending;
 nl_t_t_arready = !read_pending;
 if (write_pending && wwait > 0) wwait = wwait - 1;
 if (read_pending && rwait > 0) rwait = rwait - 1;
 nl_t_t_bvalid = write_pending && wwait == 0;
 nl_t_t_rvalid = read_pending && rwait == 0;
 nl_t_t_bresp = write_index % 7 == 3 ? 2 : 0;
 nl_t_t_rresp = read_index % 7 == 3 ? 2 : 0;
 nl_t_t_rdata = 'h789a0000 + read_index; nl_t_t_rlast = 1;
 {"nl_t_t_buser = 16'h9631 ^ (write_index * 'h1a3); nl_t_t_ruser = 16'h9631 ^ (read_index * 'h1a3);" if tb else ""}
end
always @(posedge {clk_t}) begin
 if (!reset_n) begin write_pending = 0; read_pending = 0; end
 else begin
  if (nl_t_t_awvalid && nl_t_t_awready) begin
   index = (nl_t_t_awaddr - 'h1000) / 4; write = 1; {target_user_check}
   write_index = index; write_pending = 1; wwait = 10000;
  end
  if (nl_t_t_wvalid && nl_t_t_wready) begin
   check(nl_t_t_wdata == (('h12340000 + write_index) & 32'hff00ffff), "WDATA");
   check(nl_t_t_wstrb == 4'hb && nl_t_t_wlast, "WSTRB");
   wwait = write_index % 3 == 0 ? 37 : 2;
  end
  if (nl_t_t_arvalid && nl_t_t_arready) begin
   index = (nl_t_t_araddr - 'h1000) / 4; write = 0; {target_user_check}
   read_index = index; read_pending = 1; rwait = index % 3 == 0 ? 37 : 2;
  end
  if (nl_t_t_bvalid && nl_t_t_bready) begin write_pending = 0; completed = completed + 1; end
  if (nl_t_t_rvalid && nl_t_t_rready) begin read_pending = 0; completed = completed + 1; end
 end
end
'''
    packet_check = ''
    if target['protocol'] == 'apb5' and sb and tb and rsp:
        row = mapping['network']['formal']['targets']['t']
        response_net = 'dut.nl_internal_v' + row['write_response_user'].split('/')[1]
        storage = next(r for r in mapping['network']['storage'] if r['resource'] == 'burst_service/t/write')
        release_net = 'dut.nl_internal_v' + storage['release'].split('/')[1]
        mask = (1 << min(tb, rsp)) - 1
        users = [(0x9631 ^ (n * 0x1a3)) & mask for n in range(1, 18, 2)] + [0]
        if burst:
            users += ([0x11, 0x44, 0xa251, 0xa251] if split else [0x11, 0xa251])
            users = [u & mask for u in users]
        arms = '\n'.join(f"{k}: expected_packet_user = {rsp}'h{u:x};" for k, u in enumerate(users))
        packet_check = f'''
integer packet_responses = 0;
function [{rsp-1}:0] expected_packet_user;
 input integer packet_number;
 begin
 case (packet_number)
 {arms}
 default: expected_packet_user = 0;
 endcase
 end
endfunction
always @(posedge {clk_t}) begin
 if (reset_n && {release_net}) begin
  check(packet_responses < {len(users)}, "PACKET_RESPONSE_COUNT");
  check({response_net} == expected_packet_user(packet_responses), "PACKET_RESPONSE_USER");
  $display("PACKET_USER packet=%0d value=%h", packet_responses, {response_net});
  packet_responses = packet_responses + 1;
 end
end
'''
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({conns});
{clocks}
integer cycles = 0, completed = 0, index = 0;
reg write = 0;
reg [15:0] response_value;
always @(posedge clk_main) begin
 cycles = cycles + 1;
 if (cycles > {40000 if source.get('qos', {}).get('shaper') else 15000}) begin $display("ASSERT_FAIL:APB5.TIMEOUT"); $fatal(1); end
end
task check;
 input condition; input [255:0] label_;
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:APB5.%0s index=%0d", label_, index); $fatal(1); end
endtask
function [{max(1,tw)-1}:0] expected_request;
 input integer index;
 begin
 case (index)
 {chr(10).join(expected_request)}
 default: expected_request = 0;
 endcase
 end
endfunction
{target_model}
{packet_check}
{burst_extension(req, sb, tb, rsp, split) if burst else ""}
task xfer;
 input integer index; input write;
 input [{max(1,req)-1}:0] user; input [{max(1,sb)-1}:0] expected_user;
 input [3:0] strobes;
 begin
 {task}
 end
endtask
initial begin
 {chr(10).join(init)}
 repeat (8) @(negedge clk_main); reset_n = 1;
 {chr(10).join(calls)}
 {'burst_transfers;' if burst else ''}
 {"@(negedge clk_main); nl_i_s_psel = 0; nl_i_s_penable = 0;" if sa else ""}
 repeat (4) @(negedge clk_main);
 check(completed == {29 if burst else (9 * (4//lanes + (3 if lanes == 1 else 2 if lanes == 2 else 1)) if ta else 18)}, "COMPLETION_COUNT");
 {f'check(packet_responses == {len(users)}, "PACKET_RESPONSE_COUNT");' if packet_check else ""}
 {'check(cycles > 8000, "SHAPER_DELAY");' if source.get("qos", {}).get("shaper") else ""}
 $display("NOCLOOM_PASS apb5 user"); $finish;
end
endmodule
'''


def timeout_bench(mapping, cfg):
    ports = mapping['ports']
    declarations = '\n'.join(
        f'{"reg" if p["direction"] == "input" else "wire"} [{p["width"]-1}:0] {p["name"]};'
        for p in ports)
    initialize = '\n'.join(f'{p["name"]} = 0;' for p in ports if p['direction'] == 'input')
    connections = ', '.join(f'.{p["name"]}({p["name"]})' for p in ports)
    target_clock = 'clk_target' if len(cfg['clock_domains']) > 1 else 'clk_main'
    extra_clock = 'always #7 clk_target = ~clk_target;' if target_clock != 'clk_main' else ''
    width = min(cfg['initiators'][0]['response_user_width'],
                cfg['targets'][0]['response_user_width'], cfg['transport']['response_user_bits'])
    return f'''`timescale 1ns/1ps
module tb;
{declarations}
{mapping['module']} dut({connections});
always #5 clk_main = ~clk_main;
{extra_clock}
integer cycles = 0, completed = 0, allowed = 0, target_base = 'h1000;
integer reads = 0, writes = 0, expected_reads = 0, expected_writes = 0;
integer good_reads = 0, stage = 0, read_length = 4;
reg [15:0] completion_user = 0, expected_user = 0;
reg [1:0] expected_bresp = 0;
reg target_write = 0;
reg target_error = 0;
reg [31:0] written_data [0:3];
localparam [15:0] USER_MASK = 16'h{(1 << width) - 1:x};
task check;
 input condition; input [255:0] label_;
 begin
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:APB5.%0s stage=%0d beat=%0d", label_, stage, reads);
  $fatal(1);
 end
 end
endtask
always @(posedge clk_main) begin
 cycles = cycles + 1;
 check(cycles < 20000, "TIMEOUT_BOUND");
 if (reset_n) begin
  if (nl_i_s_rvalid && nl_i_s_rready) begin
   check(reads < expected_reads, "TIMEOUT_RCOUNT");
   check(nl_i_s_rresp == (reads < good_reads ? 0 : 2), "TIMEOUT_RRESP");
   check(nl_i_s_ruser == (expected_user & USER_MASK), "TIMEOUT_RUSER");
   check(nl_i_s_rdata == (reads < good_reads ? 'h789a0000 + reads : 0), "TIMEOUT_RORDER");
   check(nl_i_s_rlast == (reads == expected_reads - 1), "TIMEOUT_RLAST");
   reads = reads + 1;
  end
  if (nl_i_s_bvalid && nl_i_s_bready) begin
   check(writes < expected_writes, "TIMEOUT_BCOUNT");
   check(nl_i_s_bresp == expected_bresp, "TIMEOUT_BRESP");
   check(nl_i_s_buser == (expected_user & USER_MASK), "TIMEOUT_BUSER");
   writes = writes + 1;
  end
 end
end
always @(negedge {target_clock}) begin
 nl_t_t_pready = reset_n && completed < allowed;
 nl_t_t_pslverr = target_error;
 nl_t_t_prdata = 'h789a0000 + completed;
 // Waiting values must never establish a packet's response user.
 nl_t_t_pbuser = nl_t_t_pready ? completion_user ^ completed : 16'hbad9;
end
always @(posedge {target_clock}) begin
 if (!reset_n) completed = 0;
 else if (nl_t_t_psel && nl_t_t_penable && nl_t_t_pready) begin
  check(nl_t_t_paddr == target_base + completed*4, "TIMEOUT_APB_ORDER");
  check(nl_t_t_pwrite == target_write, "TIMEOUT_APB_DIRECTION");
  if (target_write) begin
   check(nl_t_t_pwdata == 'h12340000 + completed, "TIMEOUT_APB_WDATA");
   check(nl_t_t_pstrb == 15, "TIMEOUT_APB_WSTRB");
   written_data[completed] = nl_t_t_pwdata;
  end
  $display("APB_COMPLETE stage=%0d access=%0d address=%h write=%0d data=%h user=%h",
           stage, completed, nl_t_t_paddr, nl_t_t_pwrite, nl_t_t_pwdata, nl_t_t_pbuser);
  completed = completed + 1;
 end
end
task reset_network;
 begin
 reset_n = 0;
 nl_i_s_arvalid = 0; nl_i_s_awvalid = 0; nl_i_s_wvalid = 0;
 nl_i_s_rready = 0; nl_i_s_bready = 0;
 repeat (12) @(negedge clk_main);
 reads = 0; writes = 0; expected_reads = 0; expected_writes = 0;
 reset_n = 1;
 repeat (12) @(negedge clk_main);
 end
endtask
task start_read;
 input integer good; input [15:0] user;
 begin
 reads = 0; expected_reads = read_length; good_reads = good; expected_user = user;
 nl_i_s_araddr = 'h1000; nl_i_s_arlen = read_length - 1; nl_i_s_arsize = 2;
 nl_i_s_arburst = 1; nl_i_s_arvalid = 1; nl_i_s_rready = 1;
 @(posedge clk_main); while (!nl_i_s_arready) @(posedge clk_main);
 @(negedge clk_main); nl_i_s_arvalid = 0;
 end
endtask
task finish_read;
 begin
 while (reads < expected_reads) @(negedge clk_main);
 repeat (24) @(negedge clk_main);
 check(reads == expected_reads, "TIMEOUT_RCOUNT_FINAL");
 $display("READ_RESULT stage=%0d count=%0d completed=%0d user=%h",
          stage, reads, completed, expected_user & USER_MASK);
 nl_i_s_rready = 0;
 end
endtask
task write_packet;
 input [15:0] user; input [1:0] response;
 integer k;
 begin
 writes = 0; expected_writes = 1; expected_user = user; expected_bresp = response;
 nl_i_s_awaddr = 'h1000; nl_i_s_awlen = 3; nl_i_s_awsize = 2;
 nl_i_s_awburst = 1; nl_i_s_awvalid = 1; nl_i_s_bready = 1;
 @(posedge clk_main); while (!nl_i_s_awready) @(posedge clk_main);
 @(negedge clk_main); nl_i_s_awvalid = 0;
 for (k = 0; k < 4; k = k + 1) begin
  nl_i_s_wdata = 'h12340000 + k; nl_i_s_wstrb = 15;
  nl_i_s_wlast = k == 3; nl_i_s_wvalid = 1;
  @(posedge clk_main); while (!nl_i_s_wready) @(posedge clk_main);
  @(negedge clk_main); nl_i_s_wvalid = 0;
 end
 while (writes < expected_writes) @(negedge clk_main);
 repeat (24) @(negedge clk_main);
 check(writes == 1, "TIMEOUT_BCOUNT_FINAL");
 $display("WRITE_RESULT stage=%0d count=%0d completed=%0d response=%h user=%h",
          stage, writes, completed, expected_bresp, expected_user & USER_MASK);
 nl_i_s_bready = 0;
 end
endtask
initial begin
 {initialize}
 allowed = 1; completion_user = 'hcaf1;
 reset_network;
 stage = 1; start_read(1, 'hcaf1); finish_read;
 check(completed == 1, "TIMEOUT_FIRST_COMPLETION");
 stage = 2; start_read(0, 0); finish_read;
 check(completed == 1, "TIMEOUT_FAILED_TARGET");
 allowed = 4; completion_user = 'h3c5a; reset_network;
 stage = 3; start_read(4, 'h3c5a); finish_read;
 check(completed == 4, "TIMEOUT_RESET_COMPLETIONS");
 // Cancel a packet after its first nonzero response, before timeout.
 allowed = 1; completion_user = 'hcaf1; reset_network;
 stage = 4; start_read(1, 'hcaf1);
 while (reads < 1) @(negedge clk_main);
 check(completed == 1, "TIMEOUT_CANCEL_COMPLETION");
 allowed = 0; reset_network;
 stage = 5; start_read(0, 0); finish_read;
 check(completed == 0, "TIMEOUT_NO_COMPLETION");
 allowed = 1; completion_user = 'hcaf1; target_write = 1; reset_network;
 stage = 6; write_packet(0, 2);
 check(completed == 1, "TIMEOUT_WRITE_COMPLETION");
 check(written_data[0] == 'h12340000, "TIMEOUT_WRITE_EFFECT");
 stage = 7; write_packet(0, 2);
 check(completed == 1, "TIMEOUT_WRITE_FAILED_TARGET");
 check(written_data[0] == 'h12340000, "TIMEOUT_WRITE_EFFECT_RETAINED");
 allowed = 4; completion_user = 'h3c5a; reset_network;
 stage = 8; write_packet('h3c5a, 0);
 check(completed == 4, "TIMEOUT_WRITE_RESET_COMPLETIONS");
 allowed = 0; reset_network;
 stage = 9; write_packet(0, 2);
 check(completed == 0, "TIMEOUT_WRITE_NO_COMPLETION");
 allowed = 4; target_error = 1; reset_network;
 stage = 10; write_packet('h3c5a, 2);
 check(completed == 4, "TIMEOUT_WRITE_ERROR_COMPLETIONS");
 target_error = 0;
 // A late completion belongs to the expired packet.
 allowed = 1; target_write = 0; completion_user = 'hcaf1; reset_network;
 stage = 11; start_read(1, 'hcaf1); finish_read;
 stage = 12; read_length = 16; start_read(0, 0); nl_i_s_rready = 0;
 repeat (20) @(negedge clk_main);
 allowed = 4;
 repeat (80) @(negedge clk_main);
 check(completed == 4, "TIMEOUT_LATE_COMPLETIONS");
 nl_i_s_rready = 1; finish_read;
 $display("NOCLOOM_PASS apb5 timeout user"); $finish;
end
endmodule
'''


def timeout_cases(cli, work, clocked):
    cases = [(16, 16, 16, False, False), (16, 16, 16, True, False),
             (32, 9, 16, True, False), (9, 16, 3, False, False),
             (16, 16, 16, False, True), (16, 16, 16, True, True)]
    for number, (source_width, target_width, transport, continuous, default) in enumerate(cases):
        cfg = configuration('axi4', 'apb5', clocked, 0, target_width, transport, continuous)
        cfg['initiators'][0]['response_user_width'] = source_width
        cfg['targets'][0]['timeout'] = {'cycles': 1024, 'completion': True}
        if default:
            cfg['initiators'][0]['default_target'] = 't'
        case = work / f'timeout_{number}'
        case.mkdir()
        path = case / 'input.yaml'
        path.write_text(json.dumps(cfg))
        dest = case / 'generated'
        result = subprocess.run([cli, 'generate', str(path), '--out-dir', str(dest)],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        mapping = json.loads((dest / 'nocloom_apb5_user.map.json').read_text())
        (case / 'network.v').write_text(generated_rtl(dest / 'nocloom_apb5_user.v'))
        (case / 'tb.sv').write_text(timeout_bench(mapping, cfg))
        rc = simulate(case, work / 'compiled')
        log = (case / 'simulation.log').read_text()
        assert rc == 0 and 'NOCLOOM_PASS apb5 timeout user' in log, log[-3000:]
    print(f'NOCLOOM_PASS apb5 timeout cases={len(cases)} clocked={clocked}')


def rejected_configurations(cli, work):
    valid = configuration('apb5', 'apb5', False, 7, 16, 9)
    variants = []
    for endpoint in ('initiators', 'targets'):
        for key in ('pwuser_width', 'pruser_width', 'wuser_width'):
            cfg = copy.deepcopy(valid)
            cfg[endpoint][0][key] = 1
            variants.append((cfg, 'NCL_SCHEMA_UNKNOWN_FIELD'))
    for endpoint, key, value in [('initiators', 'user_width', 129),
                                 ('initiators', 'response_user_width', 17),
                                 ('targets', 'user', [0] * 129),
                                 ('targets', 'response_user_width', 17)]:
        cfg = copy.deepcopy(valid)
        cfg[endpoint][0][key] = value
        variants.append((cfg, 'NCL_APB5_USER_WIDTH'))
    cfg = copy.deepcopy(valid)
    cfg['initiators'][0]['qos'] = {'levels': {'read': 1, 'write': 1}}
    cfg['targets'][0]['data_width'] = 16
    variants.append((cfg, 'NCL_QOS_PROFILE'))
    cfg = copy.deepcopy(valid)
    cfg['initiators'][0]['qos'] = {'axqos_map': [0, 1]}
    variants.append((cfg, 'NCL_QOS_MAP'))
    for n, (cfg, code) in enumerate(variants):
        path = work / f'reject_{n}.yaml'
        path.write_text(json.dumps(cfg))
        dest = work / f'reject_{n}'
        proc = subprocess.run([cli, 'generate', str(path), '--out-dir', str(dest)],
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 1 and code in proc.stderr, proc.stderr
        assert not dest.exists(), str(dest)

    for protocol, width, driver, message in [
            ('apb5', 128, 'user[128]', "this initiator's PAUSER port"),
            ('axi4', 128, 'user[128]', "this initiator's AxUSER port"),
            ('apb4', 1, 0, 'APB5 request widths are 0 to 128 and response widths are 0 to 16')]:
        cfg = copy.deepcopy(valid)
        cfg['initiators'][0].update(protocol=protocol, user_width=width)
        cfg['initiators'][0]['user_flags']['u0'] = driver
        path = work / f'diagnostic_{protocol}.yaml'
        path.write_text(json.dumps(cfg))
        proc = subprocess.run([cli, 'validate', str(path)], capture_output=True, text=True, timeout=120)
        assert proc.returncode == 1 and 'NCL_USER_FLAGS' in proc.stderr, proc.stderr
        assert message in proc.stderr, proc.stderr
        if protocol == 'apb5':
            assert 'AxUSER' not in proc.stderr, proc.stderr


def run(cli, base, clocked):
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    manual = (Path(__file__).resolve().parents[1] / 'docs/dedicated-targets.md').read_text()
    assert 'Dedicated targets are APB5, APB4, APB3, APB2' in manual
    assert '| Partial at APB5 or APB4 |' in manual
    assert 'user-signals.md#apb5-user-ports' in manual
    rejected_configurations(cli, work)
    timeout_cases(cli, work, clocked)
    pairs = [('apb5', 'apb5'), ('apb5', 'axi4'), ('axi4', 'apb5')]
    pairs += [('apb5', 'axi3'), ('axi3', 'apb5'), ('apb4', 'apb5'), ('apb5', 'apb4')]
    configurations = []
    for pair in pairs:
        for req, resp, transport in [(0, 0, 0), (7, 16, 9), (128, 16, 20), (1, 1, 0), (0, 16, 16)]:
            cfg = configuration(*pair, clocked, req, resp, transport, back_to_back=bool(req))
            if pair[1] == 'apb5' and req == 0 and resp:
                cfg['targets'][0]['user'] = [1, 0]
            if pair[1] == 'apb5' and req == 7:
                cfg['targets'][0]['user'] = ['u6', '~u0', 1, 0, 'u3']
            if pair == ('apb5', 'apb5') and req == 128:
                cfg['initiators'][0]['qos'] = {'levels': {'read': 1, 'write': 2}}
            configurations.append(cfg)
    for protocol in ('apb5', 'axi4'):
        for width in (8, 16, 32):
            for back_to_back in (False, True):
                cfg = configuration(protocol, 'apb5', clocked, 7, 16, 16, back_to_back)
                cfg['targets'][0]['data_width'] = width
                configurations.append(cfg)
    for response, transport, split in [(32, 32, False), (9, 16, False),
                                        (16, 3, False), (16, 0, False), (16, 16, True)]:
        cfg = configuration('axi4', 'apb5', clocked, 7, 16, transport, True)
        cfg['initiators'][0]['response_user_width'] = response
        if split:
            cfg['transport']['packet_limits'] = [{'source': 's', 'target': 't', 'max_bytes': 8}]
        configurations.append(cfg)
    cfg = configuration('apb5', 'apb5', clocked, 7, 16, 16, True)
    cfg['initiators'][0]['qos'] = {'shaper': {'bytes_per_1024_cycles': 16, 'burst_bytes': 16}}
    configurations.append(cfg)
    if not clocked:
        configurations.append(configuration('apb5', 'apb5', False, 7, 16, 16))
    for number, cfg in enumerate(configurations):
        req = len(cfg['transport']['user_flags'])
        resp = cfg['targets'][0].get('response_user_width', 0)
        case = work / f'case_{number}'
        case.mkdir()
        yaml = case / 'input.yaml'
        yaml.write_text(json.dumps(cfg))
        if not clocked and number == len(configurations) - 1:
            yaml = Path(__file__).resolve().parents[1] / 'examples/apb5_user.yaml'
        dest = case / 'generated'
        proc = subprocess.run([cli, 'generate', str(yaml), '--out-dir', str(dest),
                               '--ipxact', '--address-table'], capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        mapping = json.loads((dest / 'nocloom_apb5_user.map.json').read_text())
        ports = {p['name']: p for p in mapping['ports']}
        for role, endpoint in [('i', cfg['initiators'][0]), ('t', cfg['targets'][0])]:
            if endpoint['protocol'] != 'apb5':
                continue
            prefix = f'nl_{role}_{endpoint["name"]}_'
            for field in ('pwuser', 'pruser', 'pwakeup', 'pnse'):
                assert prefix + field not in ports
            assert (prefix + 'pauser' in ports) == bool(endpoint.get('user_width', 0) if role == 'i' else endpoint.get('user', []))
            if prefix + 'pauser' in ports:
                assert ports[prefix + 'pauser']['width'] == (req if role == 'i' else len(endpoint['user']))
            assert (prefix + 'pbuser' in ports) == bool(endpoint.get('response_user_width', 0))
        (case / 'network.v').write_text(generated_rtl(dest / 'nocloom_apb5_user.v'))
        (case / 'tb.sv').write_text(bench(mapping, cfg))
        rc = simulate(case, work / 'compiled')
        log = (case / 'simulation.log').read_text()
        assert rc == 0 and 'NOCLOOM_PASS apb5 user' in log, log[-3000:]
    print(f'NOCLOOM_PASS apb5 user cases={len(configurations)} clocked={clocked}')
