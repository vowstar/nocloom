"""AHB5 request properties under waited transfers and asynchronous clocks."""
import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

from cross_hardware_sim import simulate
from run_ahb_initiator import configuration, target_model
from sim_engine import generated_rtl

# IHI0033C table 3-7: permitted HPROT[6:2], including both shareability values.
MEMORY = (0, 1, 2, 6, 14, 7, 15, 18, 22, 30, 23, 31)


def definition(clocked, bits=32, protection=7, security=True, target_protection=7,
               master_width=32, target_master_width=32, target_security=True, target_protocol='ahb5'):
    d = configuration('clocked' if clocked else 'switch')
    d['name'] = 'ahb5_properties'
    source_domain = 'fast' if clocked else 'main'
    target_domain = 'slow' if clocked else 'main'
    d['initiators'] = [dict(name='h0', clock_domain=source_domain, protocol='ahb5', data_width=bits,
                            ahb5=dict(hprot_width=protection, hnonsec=security, hmaster_width=master_width)),
                       dict(name='x0', clock_domain=source_domain, protocol='axi4', data_width=32,
                            id_width=4, len_width=4),
                       dict(name='l0', clock_domain=source_domain, protocol='ahb_v3', data_width=32)]
    d['targets'] = [dict(name=n, clock_domain=target_domain, protocol=p, data_width=32,
                         attributes='amba', mappings=[dict(base=hex(a), size='0x1000')], **opts)
                    for n, p, a, opts in [('th', 'ahb5', 0x1000, dict(ahb5=dict(hprot_width=target_protection, hnonsec=True, hmaster_width=target_master_width))),
                                           ('tx', 'axi4', 0x2000, dict(id_width=2, len_width=4))]]
    d['targets'][0]['protocol'] = target_protocol
    if target_protocol == 'ahb5':
        d['targets'][0]['ahb5']['hnonsec'] = target_security
    else:
        d['targets'][0].pop('ahb5')
    d['connectivity'] = 'all'
    # Named flags coexist with the protocol metadata without exposing AHB user ports.
    d['transport'] = dict(data_width=32, user_flags=['trace'])
    for i in d['initiators']:
        i['user_flags'] = dict(trace='nonsecure')
    if clocked:
        for direction in ('request', 'response'):
            g = d['topology'][direction]
            g['entries'] = ([dict(endpoint=s, node='root') for s in ('h0', 'x0', 'l0')] if direction == 'request'
                            else [dict(endpoint=t, node='leaf') for t in ('th', 'tx')])
            g['exits'] = ([dict(endpoint=t, node='leaf') for t in ('th', 'tx')] if direction == 'request'
                          else [dict(endpoint=s, node='root') for s in ('h0', 'x0', 'l0')])
    return d


def transfers(protection=7, target_protection=7):
    out = []
    # All protection and security combinations, with independent master values.
    for target, base in [('th', 0x1000), ('tx', 0x2000)]:
        for mem in (MEMORY if protection == 7 else range(4)):
            for low in range(4):
                for sec in range(2):
                    prot = (mem << 2) | low
                    master = (0x80000001, 0x21, 1, 0x80000020, 0x80000000, 0x20, 3)[len(out) // 2 % 7]
                    for write in (1, 0):
                        raw = prot if protection == 7 else prot | ((prot & 8) << 1)
                        cache = ((raw >> 2) & 3) | (((raw >> 4) & 1) << (2 if write else 3)) | (((raw >> 5) & 1) << (3 if write else 2))
                        axprot = ((~prot & 1) << 2) | (sec << 1) | ((prot >> 1) & 1)
                        out.append(dict(source='h', target=target, addr=base + 0x100, write=write,
                                        hprot=prot, sec=sec, master=master, cache=cache, prot=axprot))
        for write in (1, 0):
            out.append(dict(source='h', target=target, addr=base + 0xf00, write=write,
                            hprot=3, sec=0, master=31, cache=0, prot=1))
    if protection == 7:
        errors = []
        for hp in (0x5f, 0x7f):
            errors.append(dict(source='h', target='th', addr=0x1f00, write=1,
                            hprot=hp, sec=1, master=0x80000021, cache=0, prot=3))
            errors.append(dict(source='h', target='th', addr=0x1100, write=1,
                            hprot=3, sec=0, master=0x20, cache=0, prot=1))
        out = errors + out
    # Every legal AXI4 cache encoding, including legacy read/write allocate encodings.
    for cache in (range(16) if target_protection == 4 else (0, 1, 2, 3, 6, 7, 10, 11, 14, 15)):
        for prot in range(8):
            for write in (1, 0):
                if target_protection == 4:
                    # Four-bit targets map CACHE[1:0] directly, for either AXI channel.
                    hp = ((cache & 3) << 2) | ((prot & 1) << 1) | (1 - (prot >> 2))
                    if cache == 3:
                        assert hp == (13, 15, 13, 15, 12, 14, 12, 14)[prot]
                else:
                    hp = (((cache >> 3 if write else cache >> 2) & 1) << 5) | (int(bool(cache & 12)) << 4)
                    hp |= (cache & 2) << 2 | ((cache & 1) if cache != 3 else 0) << 2
                    hp |= (prot & 1) << 1 | ((~prot >> 2) & 1)
                out.append(dict(source='x', target='th', addr=0x1100, write=write,
                                hprot=hp, sec=(prot >> 1) & 1, master=0, cache=cache, prot=prot))
    for write in (1, 0):
        out.append(dict(source='x', target='th', addr=0x1f00, write=write,
                        hprot=3, sec=1, master=0, cache=0, prot=3))
    for hp in range(16):
        for write in (1, 0):
            out.append(dict(source='l', target='th', addr=0x1200, write=write,
                            hprot=hp | ((hp & 8) << 1), source_hprot=hp,
                            sec=1, master=0, cache=0, prot=0))
    return out


def legacy_driver(sc):
    return f'''
task automatic l_transfer(input [31:0] a, input wr, input [3:0] ca, input [2:0] pr);
 integer guard;
 begin
 @(negedge {sc});
 nl_i_l0_hsel = 1; nl_i_l0_htrans = 2; nl_i_l0_haddr = a; nl_i_l0_hsize = 2;
 nl_i_l0_hburst = 0; nl_i_l0_hwrite = wr; nl_i_l0_hprot = drive_hprot;
 @(posedge {sc}); while (!nl_i_l0_hreadyout) @(posedge {sc});
 @(negedge {sc}); nl_i_l0_htrans = 0; nl_i_l0_hwdata = data_at(a);
 nl_i_l0_hprot = ~drive_hprot;
 guard = 0;
 @(posedge {sc});
 while (!nl_i_l0_hreadyout) begin
  guard = guard + 1; check(guard < 2000, "L_HANG"); @(posedge {sc});
 end
 check(!nl_i_l0_hresp, "L_RESPONSE");
 if (!wr) check(nl_i_l0_hrdata == data_at(a), "L_DATA");
 @(negedge {sc}); nl_i_l0_hsel = 0;
 end
endtask
task automatic l_burst(input wr);
 integer cb, dp, nx, done_, guard;
 begin
 @(negedge {sc});
 nl_i_l0_hsel = 1; nl_i_l0_htrans = 2; nl_i_l0_haddr = want_addr;
 nl_i_l0_hsize = 2; nl_i_l0_hburst = 3; nl_i_l0_hwrite = wr;
 nl_i_l0_hprot = drive_hprot;
 cb = 0; dp = -1; nx = 1; done_ = 0; guard = 0;
 while (done_ < 4) begin
  @(posedge {sc}); guard = guard + 1; check(guard < 2000, "L_BURST_HANG");
  if (nl_i_l0_hreadyout) begin
   if (dp >= 0) begin
    check(!nl_i_l0_hresp, "L_BURST_RESPONSE");
    if (!wr) check(nl_i_l0_hrdata == data_at(want_addr + 4 * dp), "L_BURST_DATA");
    done_ = done_ + 1;
   end
   dp = cb;
   @(negedge {sc});
   if (dp >= 0 && wr) nl_i_l0_hwdata = data_at(want_addr + 4 * dp);
   if (nx < 4) begin
    nl_i_l0_htrans = 3; nl_i_l0_haddr = want_addr + 4 * nx; cb = nx; nx = nx + 1;
   end else begin nl_i_l0_htrans = 0; cb = -1; end
  end
 end
 nl_i_l0_hsel = 0;
 end
endtask
'''


def extra_calls(bits, protection, security, sc, master_width=32, target_master_width=32):
    calls = []
    # Four consecutive source beats become distinct 32-bit target transfers.
    for source, target in [('h', 0), ('h', 1), ('l', 0), ('x', 0)]:
        for wr in (1, 0):
            for master in ((0x80000021, 0x20, 1) if source == 'h' else (0,)):
                hp = (0x5b if wr else 0x7b) if source == 'h' and protection == 7 else 0x1b
                source_hp = hp if source == 'h' and protection == 7 else 0xb
                words = bits // 8 if source == 'h' else 4
                # Four wide beats exceed the target's sixteen-word packet limit.
                addr = (0x2000 if target else 0x1000) + 0x380
                cache = 6 if wr else 14 if source == 'h' and protection == 7 else 10
                sec = 0 if source == 'h' and security and wr else 1
                if source == 'x' and not wr:
                    sec = 0
                prot = 1 | (sec << 1)
                calls.append(f'''
 axi_active = {int(source == "x")}; legacy_active = {int(source == "l")}; want_hprot = {hp}; drive_hprot = {source_hp}; want_sec = {sec};
 want_master = 32'h{master & ((1 << master_width) - 1) if target_master_width else 0:x}; want_target = {target}; want_write = {wr};
 want_cache = {cache}; want_prot = {prot};
 want_addr = 32'h{addr:x}; want_words = {words}; start_seen = seen; before_packets = packet_seen;
 {source + '_burst(' + str(wr) + ')' if source != 'x' else 'x_transfer(want_addr, ' + str(wr) + f', {cache}, {prot}, 4)'};
 repeat (120) @(posedge {sc});
 check(seen == start_seen + want_words && data_seen == seen, "BURST_COUNT");
 {('check(packet_seen - before_packets >= 2, "PACKET_SPLIT");' if source == 'h' and bits > 32 else '')}
''')
    return ''.join(calls)


def reserved_calls(protection, sc):
    if protection != 7:
        return ''
    calls = ['protocol_only = 1;']
    for high in sorted(set(range(32)) - set(MEMORY)):
        calls.append(f'''
 drive_hprot = {high << 2}; want_hprot = drive_hprot; want_sec = 1;
 h_transfer(32'h1100, 0, 0, 3);
 repeat (120) @(posedge {sc});
''')
    calls.append('check(violations == 20, "RESERVED_ENCODINGS");')
    return '\n'.join(calls)


def bench(mapping, clocked, bits=32, protection=7, security=True, target_protection=7,
          master_width=32, target_master_width=32):
    ports = {p['name']: p for p in mapping['ports']}
    driven = {n for n, p in ports.items() if p['endpoint'].startswith('target:') and p['direction'] == 'input'}
    decl, init, conns = [], [], []
    for name, p in ports.items():
        if name in ('nl_i_h0_hready', 'nl_i_l0_hready'):
            decl.append(f'wire {name};')
        elif p['direction'] == 'input' and name not in driven:
            decl.append(f'reg [{p["width"] - 1}:0] {name};')
            if p.get('signal') != 'clock':
                init.append(f'{name} = 0;')
        else:
            decl.append(f'wire [{p["width"] - 1}:0] {name};')
        conns.append(f'.{name}({name})')
    clocks = ''.join(f'initial {n} = 0; always #{3 if n == "clk_fast" else 7 if n == "clk_slow" else 5} {n} = ~{n};\n'
                     for n, p in ports.items() if p.get('signal') == 'clock')
    sc = 'clk_fast' if clocked else 'clk_main'
    tc = 'clk_slow' if clocked else 'clk_main'
    models = target_model('th', 'ahb5', 32, 0, ports) + target_model('tx', 'axi4', 32, 1, ports)
    calls = []
    selected = transfers(protection, target_protection)
    for k, original in enumerate(selected):
        t = dict(original)
        if t['source'] == 'h':
            t['source_hprot'] = t['hprot']
            if protection == 4:
                t['hprot'] |= (t['hprot'] & 8) << 1
            if not security:
                t['sec'] = 1
                t['prot'] |= 2
        t['source_hprot'] = t.get('source_hprot', t['hprot'])
        t['master'] &= (1 << master_width) - 1 if target_master_width else 0
        calls.append(f'''want_hprot = {t['hprot']}; drive_hprot = {t['source_hprot']}; want_sec = {t['sec']}; want_master = {t['master']};
axi_active = {int(t["source"] == "x")}; legacy_active = {int(t["source"] == "l")}; want_cache = {t['cache']}; want_prot = {t['prot']}; want_target = {int(t['target'] == 'tx')};
$display("CASE {k}");
want_addr = 32'h{t['addr']:x}; want_write = {t['write']};
want_words = {bits // 32 if t['source'] == 'h' else 1}; start_seen = seen;
{t['source']}_transfer(want_addr, want_write, {t['cache']}, {t['prot']}{", 1" if t["source"] == "x" else ""});
{"" if t["source"] == "x" else "repeat (120) @(posedge " + sc + ");"}
check(seen == start_seen + want_words, "TRANSFER_COUNT");''')
    properties = ['nl_t_th_' + field for field in ('haddr', 'hprot', 'hnonsec', 'hmaster')
                  if 'nl_t_th_' + field in ports]
    held_width = sum(ports[n]['width'] for n in properties)
    held_value = ', '.join(properties)
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
assign nl_i_h0_hready = nl_i_h0_hreadyout;
assign nl_i_l0_hready = nl_i_l0_hreadyout;
{mapping['module']} dut({', '.join(conns)});
{clocks}
integer seen = 0, want_hprot, drive_hprot, want_sec, want_cache, want_prot, want_target;
reg [31:0] want_master;
integer want_addr, want_write, want_words, start_seen;
integer data_seen = 0, packet_seen = 0, violations = 0;
reg protocol_only = 0, legacy_active = 0, axi_active = 0;
integer before_packets;

integer waits = 0, errors = 0, target_waits = 0;
reg error_first = 0;
task automatic check(input condition, input [255:0] label_);
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:AHB5.%0s", label_); $fatal(1); end
endtask
{models}
// The address phase carries each property, including while the target inserts waits.
reg stalled = 0;
reg [{held_width - 1}:0] held;
always @(posedge {tc}) if (reset_n && !protocol_only) begin
 if (stalled) check({{{held_value}}} === held, "STABLE");
 if (stalled) target_waits = target_waits + 1;
 stalled <= nl_t_th_hsel && nl_t_th_htrans[1] && !nl_t_th_hready;
 held <= {{{held_value}}};
 if (nl_t_th_hsel && nl_t_th_htrans[1] && nl_t_th_hready) begin
  check(want_target == 0, "TARGET");
  if (nl_t_th_hprot != (want_hprot & {(1 << target_protection) - 1})) $display("HPROT got=%h want=%h addr=%h", nl_t_th_hprot, want_hprot, nl_t_th_haddr);
  check(nl_t_th_hprot == (want_hprot & {(1 << target_protection) - 1}), "HPROT");
  if (axi_active) $display("AXI_HPROT cache=%h prot=%h write=%0d hp=%h", want_cache, want_prot, want_write, nl_t_th_hprot);
  {'check(nl_t_th_hnonsec == want_sec, "HNONSEC");' if "nl_t_th_hnonsec" in ports else ""}
  {'check(nl_t_th_hmaster == want_master, "HMASTER");' if target_master_width else ""}
  check(nl_t_th_haddr == want_addr % 4096 + 4 * (seen - start_seen), "H_ADDRESS_ORDER");
  check(nl_t_th_hwrite == want_write && nl_t_th_hsize == 2, "H_CONTROL");
  if (nl_t_th_htrans == 2) packet_seen = packet_seen + 1;
  seen = seen + 1;
 end
 if (th_dp && nl_t_th_hready) begin
  if (th_dw) check(nl_t_th_hwdata == data_at(th_da + 32'h1000), "H_WRITE_DATA");
  data_seen = data_seen + 1;
 end
 if (nl_t_tx_awvalid && nl_t_tx_awready) begin
  check(want_target == 1 && nl_t_tx_awcache == want_cache && nl_t_tx_awprot == want_prot, "AW_ATTRIBUTES");
  check(nl_t_tx_awaddr == want_addr % 4096 + 4 * (seen - start_seen), "AW_ADDRESS_ORDER");
  seen = seen + nl_t_tx_awlen + 1;
  packet_seen = packet_seen + 1;
 end
 if (nl_t_tx_arvalid && nl_t_tx_arready) begin
  check(want_target == 1 && nl_t_tx_arcache == want_cache && nl_t_tx_arprot == want_prot, "AR_ATTRIBUTES");
  check(nl_t_tx_araddr == want_addr % 4096 + 4 * (seen - start_seen), "AR_ADDRESS_ORDER");
  seen = seen + nl_t_tx_arlen + 1;
  packet_seen = packet_seen + 1;
 end
 if (nl_t_tx_wvalid && nl_t_tx_wready) begin
  check(nl_t_tx_wdata == data_at(tx_wbeat + 32'h2000) && nl_t_tx_wstrb == 15, "X_WRITE_DATA");
  check(nl_t_tx_wlast == (tx_wk == tx_wn), "X_WRITE_LAST");
  data_seen = data_seen + 1;
 end
 if (nl_t_tx_rvalid && nl_t_tx_rready) data_seen = data_seen + 1;
end
reg reset_clock_seen = 0;
always @(posedge {tc}) reset_clock_seen <= 1'b1;
always @(negedge {tc}) if (reset_clock_seen && !reset_n) begin
 check(nl_t_th_hready === 1'b1, "RESET_READY");
 check(nl_t_th_htrans === 2'b00, "RESET_IDLE");
end
function automatic legal_hprot(input [6:0] hp);
 case (hp[6:2])
  0, 1, 2, 6, 7, 14, 15, 18, 22, 23, 30, 31: legal_hprot = 1;
  default: legal_hprot = 0;
 endcase
endfunction
always @(posedge {sc}) if (reset_n && {int(protection == 7)} &&
 nl_i_h0_hsel && nl_i_h0_htrans[1] && nl_i_h0_hready) begin
 if (!legal_hprot(nl_i_h0_hprot)) begin
  violations = violations + 1;
  $display("PROTOCOL_VIOLATION:AHB5.HPROT value=%h", nl_i_h0_hprot);
 end
end
reg [21:0] h_page = 0, l_page = 0;
always @(posedge {sc}) if (reset_n) begin
 if (nl_i_h0_hsel && nl_i_h0_htrans[1] && nl_i_h0_hready) begin
  if (nl_i_h0_htrans == 2) h_page <= nl_i_h0_haddr[31:10];
  else check(nl_i_h0_haddr[31:10] == h_page, "BURST_BOUNDARY");
 end
 if (nl_i_l0_hsel && nl_i_l0_htrans[1] && nl_i_l0_hready) begin
  if (nl_i_l0_htrans == 2) l_page <= nl_i_l0_haddr[31:10];
  else check(nl_i_l0_haddr[31:10] == l_page, "BURST_BOUNDARY");
 end
end
always @(posedge {sc}) if (reset_n) begin
 if (!nl_i_h0_hreadyout) waits = waits + 1;
 if (error_first) begin
  check(nl_i_h0_hreadyout && nl_i_h0_hresp, "ERROR_SECOND");
  errors = errors + 1;
 end
 error_first <= nl_i_h0_hresp && !nl_i_h0_hreadyout;
 if (nl_i_h0_hresp && nl_i_h0_hreadyout) check(error_first, "ERROR_FIRST");
end
function automatic [{bits - 1}:0] source_data(input [31:0] a);
 integer lane; begin
 for (lane = 0; lane < {bits // 32}; lane = lane + 1)
 source_data[32 * lane +: 32] = (a + 4 * lane) ^ 32\'hcafeaced;
 end
endfunction
function automatic [31:0] data_at(input [31:0] a);
 data_at = a ^ 32'hcafeaced;
endfunction
task automatic h_transfer(input [31:0] a, input wr, input [3:0] ca, input [2:0] pr);
 integer guard;
 begin
 @(negedge {sc});
 nl_i_h0_hsel = 1; nl_i_h0_htrans = 2; nl_i_h0_haddr = a; nl_i_h0_hsize = {(bits // 8).bit_length() - 1};
 nl_i_h0_hburst = 0; nl_i_h0_hwrite = wr; nl_i_h0_hmastlock = 0;
 nl_i_h0_hprot = drive_hprot; {"nl_i_h0_hnonsec = want_sec;" if security else ""} nl_i_h0_hmaster = want_master;
 @(posedge {sc}); while (!nl_i_h0_hreadyout) @(posedge {sc});
 @(negedge {sc}); nl_i_h0_htrans = 0; nl_i_h0_hwdata = source_data(a);
 // Change the next address phase's properties before this request reaches the target.
 nl_i_h0_hprot = ~drive_hprot; {"nl_i_h0_hnonsec = !want_sec;" if security else ""} nl_i_h0_hmaster = ~want_master;
 guard = 0;
 @(posedge {sc});
 while (!nl_i_h0_hreadyout) begin
  guard = guard + 1; check(guard < 2000, "HANG"); @(posedge {sc});
 end
 if (!protocol_only) begin
 check(nl_i_h0_hresp == (a[11:8] == 15 && !(wr && want_hprot[2] && !want_hprot[6])), "H_RESPONSE");
 if (wr && want_hprot[6]) check(data_seen >= start_seen + want_words, "SHAREABLE_COMPLETION");
 if (!wr && a[11:8] != 15) check(nl_i_h0_hrdata == source_data(a), "H_DATA");
 end
 @(negedge {sc}); nl_i_h0_hsel = 0;
 end
endtask
{legacy_driver(sc)}
task automatic h_burst(input wr);
 integer cb, dp, nx, done_, guard;
 begin
 @(negedge {sc});
 nl_i_h0_hsel = 1; nl_i_h0_htrans = 2; nl_i_h0_haddr = want_addr;
 nl_i_h0_hsize = {(bits // 8).bit_length() - 1}; nl_i_h0_hburst = 3; nl_i_h0_hwrite = wr;
 nl_i_h0_hprot = drive_hprot; {"nl_i_h0_hnonsec = want_sec;" if security else ""} nl_i_h0_hmaster = want_master;
 cb = 0; dp = -1; nx = 1; done_ = 0; guard = 0;
 while (done_ < 4) begin
  @(posedge {sc}); guard = guard + 1; check(guard < 2000, "BURST_HANG");
  if (nl_i_h0_hreadyout) begin
   if (dp >= 0) begin
    check(!nl_i_h0_hresp, "BURST_RESPONSE");
    if (!wr) check(nl_i_h0_hrdata == source_data(want_addr + {bits // 8} * dp), "BURST_DATA");
    done_ = done_ + 1;
   end
   dp = cb;
   @(negedge {sc});
   if (dp >= 0 && wr) nl_i_h0_hwdata = source_data(want_addr + {bits // 8} * dp);
   if (nx < 4) begin
    nl_i_h0_htrans = 3; nl_i_h0_haddr = want_addr + {bits // 8} * nx; cb = nx; nx = nx + 1;
   end else begin nl_i_h0_htrans = 0; cb = -1; end
  end
 end
 nl_i_h0_hsel = 0;
 end
endtask
task automatic x_transfer(input [31:0] a, input wr, input [3:0] ca, input [2:0] pr, input integer beats);
 integer guard, beat;
 begin
 @(negedge {sc});
 if (wr) begin
  nl_i_x0_arcache = ~ca; nl_i_x0_arprot = ~pr;
  nl_i_x0_awlen = beats - 1; nl_i_x0_awid = 9; nl_i_x0_awaddr = a; nl_i_x0_awsize = 2; nl_i_x0_awburst = 1;
  nl_i_x0_awcache = ca; nl_i_x0_awprot = pr; nl_i_x0_awvalid = 1;
  @(posedge {sc}); while (!nl_i_x0_awready) @(posedge {sc});
  @(negedge {sc}); nl_i_x0_awvalid = 0;
  nl_i_x0_awcache = ~ca; nl_i_x0_awprot = ~pr;
  for (beat = 0; beat < beats; beat = beat + 1) begin
   nl_i_x0_wdata = data_at(a + 4 * beat); nl_i_x0_wstrb = 15;
   nl_i_x0_wlast = beat == beats - 1; nl_i_x0_wvalid = 1;
   @(posedge {sc}); while (!nl_i_x0_wready) @(posedge {sc});
   @(negedge {sc}); nl_i_x0_wvalid = 0;
  end
  guard = 0;
  while (!nl_i_x0_bvalid) begin @(posedge {sc}); guard = guard + 1; check(guard < 2000, "B_HANG"); end
  check(nl_i_x0_bid == 9, "B_ID");
  check(nl_i_x0_bresp == (a[11:8] == 15 ? 2 : 0), "B_RESPONSE");
  @(negedge {sc}); nl_i_x0_bready = 1;
  @(negedge {sc}); nl_i_x0_bready = 0;
 end else begin
  nl_i_x0_awcache = ~ca; nl_i_x0_awprot = ~pr;
  nl_i_x0_arlen = beats - 1; nl_i_x0_arid = 6; nl_i_x0_araddr = a; nl_i_x0_arsize = 2; nl_i_x0_arburst = 1;
  nl_i_x0_arcache = ca; nl_i_x0_arprot = pr; nl_i_x0_arvalid = 1;
  @(posedge {sc}); while (!nl_i_x0_arready) @(posedge {sc});
  @(negedge {sc}); nl_i_x0_arvalid = 0; nl_i_x0_arcache = ~ca; nl_i_x0_arprot = ~pr;
  for (beat = 0; beat < beats; beat = beat + 1) begin
  guard = 0;
  while (!nl_i_x0_rvalid) begin @(posedge {sc}); guard = guard + 1; check(guard < 2000, "R_HANG"); end
  check(nl_i_x0_rid == 6, "R_ID");
  check(nl_i_x0_rresp == (a[11:8] == 15 ? 2 : 0), "R_RESPONSE");
  if (a[11:8] != 15) check(nl_i_x0_rdata == data_at(a + 4 * beat), "R_DATA");
  check(nl_i_x0_rlast == (beat == beats - 1), "R_LAST");
  @(negedge {sc}); nl_i_x0_rready = 1;
  @(negedge {sc}); nl_i_x0_rready = 0;
  end
 end
 end
endtask
initial begin
 {chr(10).join(init)}
 repeat (12) @(posedge {sc});
 @(negedge {sc}); reset_n = 1;
 repeat (20) @(posedge {sc});
 {chr(10).join(calls)}
 {extra_calls(bits, protection, security, sc, master_width, target_master_width)}
 check(waits > 0 && target_waits > 0 && errors == {6 if protection == 7 else 4}, "COVERAGE");
 check(violations == 0, "LEGAL_ENCODINGS");
 {reserved_calls(protection, sc)}
 $display("NOCLOOM_PASS ahb5 properties transfers={len(selected)}"); $finish;
end
initial begin #10000000; $display("ASSERT_FAIL:AHB5.TIMEOUT"); $fatal(1); end
endmodule
'''


def reject_properties(cli, work):
    valid = definition(False)
    trials = []
    for role in ('initiators', 'targets'):
        for key, value in [('hprot_width', 0), ('hprot_width', 5), ('hprot_width', 6),
                           ('hprot_width', 8), ('hmaster_width', 33), ('exclusive', True), ('hauser_width', 1),
                           ('hwuser_width', 1), ('hruser_width', 1), ('hbuser_width', 1)]:
            d = copy.deepcopy(valid)
            d[role][0]['ahb5'][key] = value
            trials.append((d, 'NCL_AHB5_PROPERTIES'))
        d = copy.deepcopy(valid)
        d[role][0]['protocol'] = 'ahb_v3'
        trials.append((d, 'NCL_AHB5_PROPERTIES'))
    d = copy.deepcopy(valid)
    d['targets'][0]['ahb5']['hmaster_width'] = 4
    trials.append((d, 'NCL_AHB5_PROPERTIES'))
    d = copy.deepcopy(valid)
    d['targets'][0]['hresp_width'] = 2
    trials.append((d, 'NCL_TARGET_AHB_HRESP'))
    d = copy.deepcopy(valid)
    d['targets'][0]['attributes'] = 'zero'
    trials.append((d, 'NCL_AHB5_PROPERTIES'))
    d = copy.deepcopy(valid)
    d['initiators'][0]['ahb5']['hsplit_width'] = 1
    trials.append((d, 'NCL_SCHEMA_UNKNOWN_FIELD'))
    for key, value in [('hnonsec', 1), ('hprot_width', '7')]:
        for role in ('initiators', 'targets'):
            d = copy.deepcopy(valid)
            d[role][0]['ahb5'][key] = value
            trials.append((d, 'NCL_SCHEMA_TYPE'))
    d = copy.deepcopy(valid)
    d['targets'][0]['attributes'] = dict(hprot=[1, 1, 0, 0])
    trials.append((d, 'NCL_AHB5_PROPERTIES'))
    for k, (d, code) in enumerate(trials):
        config = work / f'rejected-{k}.yaml'
        config.write_text(json.dumps(d) + '\n')
        dest = work / f'rejected-{k}'
        p = subprocess.run([cli, 'generate', config, '--out-dir', dest],
                           capture_output=True, text=True, timeout=180)
        assert p.returncode != 0 and code in p.stderr and not dest.exists(), (k, p.stderr)
        if isinstance(d['targets'][0]['attributes'], dict):
            assert "does not support per-bit attribute overrides" in p.stderr, p.stderr

def fault_bench(tb, port, width, expression):
    connection = f'.{port}({port})'
    assert connection in tb
    return tb.replace(connection, f'.{port}(fault_value)').replace(
        'module tb;', f'module tb;\nwire [{width - 1}:0] fault_value;').replace(
        'endmodule', f'assign {port} = {expression};\nendmodule')


def run_case(cli, work, clocked, bits, protection, security, target_protection,
             master_width=32, target_master_width=32, target_security=True, target_protocol='ahb5'):
    work.mkdir()
    config = work / 'input.yaml'
    d = definition(clocked, bits, protection, security, target_protection,
                   master_width, target_master_width, target_security, target_protocol)
    if protection == 4 and not security:
        d['initiators'][0]['ahb5'].pop('hprot_width')
        d['initiators'][0]['ahb5'].pop('hnonsec')
    config.write_text(json.dumps(d, indent=1) + '\n')
    dest = work / 'generated'
    p = subprocess.run([cli, 'generate', config, '--out-dir', dest, '--ipxact', '--address-table'],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    mapping = json.loads((dest / 'nocloom_ahb5_properties.map.json').read_text())
    xml = ET.parse(dest / 'nocloom_ahb5_properties.ipxact.xml')
    ns = {'s': 'http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009'}
    model_ports = {p.findtext('s:name', namespaces=ns)
                   for p in xml.findall('s:model/s:ports/s:port', ns)}
    for endpoint, prefix, width in [('h0', 'nl_i_h0_', protection),
                                     ('th', 'nl_t_th_', target_protection)]:
        interface = next(b for b in xml.findall('s:busInterfaces/s:busInterface', ns)
                         if b.findtext('s:name', namespaces=ns) == endpoint)
        mapped = {p.findtext('s:logicalPort/s:name', namespaces=ns):
                  p.findtext('s:physicalPort/s:name', namespaces=ns)
                  for p in interface.findall('s:portMaps/s:portMap', ns)}
        if width == 4:
            assert mapped.get('HPROT') == prefix + 'hprot', mapped
        else:
            assert 'HPROT' not in mapped, mapped
        assert 'HNONSEC' not in mapped and 'HMASTER' not in mapped, mapped
        for signal in ('hprot', 'hnonsec', 'hmaster'):
            assert (prefix + signal in model_ports) == any(
                p['name'] == prefix + signal for p in mapping['ports']), (endpoint, signal)
    ports = {p['name']: p for p in mapping['ports']}
    assert ports['nl_i_h0_hmaster']['width'] == master_width
    assert ('nl_t_th_hmaster' in ports) == bool(target_master_width)
    if target_master_width:
        assert ports['nl_t_th_hmaster']['width'] == target_master_width
    assert ports['nl_i_h0_hprot']['width'] == protection
    assert ports['nl_t_th_hprot']['width'] == target_protection
    assert ('nl_i_h0_hnonsec' in ports) == security
    assert ('nl_t_th_hnonsec' in ports) == target_security
    if target_security:
        assert ports['nl_t_th_hnonsec']['direction'] == 'output'
    if security:
        assert ports['nl_i_h0_hnonsec']['direction'] == 'input'
    case = work / 'simulation'
    case.mkdir()
    rtl = generated_rtl(dest / 'nocloom_ahb5_properties.v')
    tb = bench(mapping, clocked, bits, protection, security, target_protection, master_width, target_master_width)
    (case / 'network.v').write_text(rtl)
    (case / 'tb.sv').write_text(tb)
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    assert rc == 0 and 'NOCLOOM_PASS ahb5 properties' in log and 'ASSERT_FAIL' not in log, log[-3000:]
    violations = [line for line in log.splitlines() if line.startswith('PROTOCOL_VIOLATION:')]
    expected = sorted(set(range(32)) - set(MEMORY)) if protection == 7 else []
    assert sorted(int(line.split('=')[1], 16) >> 2 for line in violations) == expected, violations
    negatives = ([
        ('master', fault_bench(tb, 'nl_t_th_hmaster', target_master_width,
                    "fault_value & 32'h1f" if master_width == 32 else
                    "fault_value | (fault_value[4] ? 7'h60 : 0)"), 'HMASTER')] if target_master_width else []) + [
        ('reset', fault_bench(tb, 'nl_t_th_hready', 1, 'fault_value & reset_n'), 'RESET_READY'),
        ('legacy', fault_bench(tb, 'nl_t_th_hprot', target_protection,
                    f'fault_value ^ ((legacy_active && want_hprot[3]) ? {64 if target_protection == 7 else 1} : 0)'), 'HPROT')]
    if protection == 7:
        negatives += [
            ('protection', fault_bench(tb, 'nl_t_th_hprot', target_protection, f"fault_value ^ {target_protection}'h{64 if target_protection == 7 else 1:x}"), 'HPROT'),
            ('reserved', tb.replace('if (!legal_hprot(nl_i_h0_hprot))', 'if (1\'b0)'), 'RESERVED_ENCODINGS')]
    else:
        negatives += [
            ('cache', fault_bench(tb, 'nl_t_tx_arcache', 4, "fault_value & 4'h7"), 'AR_ATTRIBUTES'),
            ('nonsecure', fault_bench(tb, 'nl_t_tx_awprot', 3, "fault_value & 3'h5"), 'AW_ATTRIBUTES')]
    if target_security:
        negatives.append(('security', fault_bench(tb, 'nl_t_th_hnonsec', 1, '!fault_value'), 'HNONSEC'))
    if bits > 32:
        bad_tb = tb.replace("want_addr = 32'h1380;", "want_addr = 32'h13e0;")
        assert bad_tb != tb
        negatives.append(('boundary', bad_tb, 'BURST_BOUNDARY'))
    for label, bad_tb, assertion in negatives:
        bad = work / ('negative-' + label)
        bad.mkdir()
        (bad / 'network.v').write_text(rtl)
        (bad / 'tb.sv').write_text(bad_tb)
        nrc = simulate(bad, work / 'compiled')
        nlog = (bad / 'simulation.log').read_text()
        assert nrc != 0 and 'ASSERT_FAIL:AHB5.' + assertion in nlog and 'NOCLOOM_PASS' not in nlog, (label, nlog[-3000:])
    (work / 'results.json').write_text(json.dumps(dict(status='PASS', bits=bits,
        protection=protection, security=security, master_width=master_width,
        target_master_width=target_master_width, target_protection=target_protection,
        target_security=target_security, target_protocol=target_protocol, reserved=expected,
        negatives=[n[0] for n in negatives], rtl_sha256=hashlib.sha256(rtl.encode()).hexdigest()), indent=1) + '\n')


def run(cli, base, clocked):
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    reject_properties(cli, work)
    for name, bits, hp, sec, target_hp in [('extended', 32, 7, True, 7),
                                          ('wide', 256, 7, True, 7),
                                          ('compact', 32, 4, False, 7),
                                          ('target4', 32, 7, True, 4)]:
        run_case(cli, work / name, clocked, bits, hp, sec, target_hp)
    run_case(cli, work / 'master_extension', clocked, 32, 7, True, 7, 5, 7)
    for width in (4, 7):
        run_case(cli, work / f'target{width}_no_security', clocked, 32, 7, True, width,
                 target_security=False)
    run_case(cli, work / 'lite_target', clocked, 32, 7, True, 4,
             target_master_width=0, target_security=False, target_protocol='ahb_v3')
    print('NOCLOOM_PASS ahb5 properties matrices=8')
