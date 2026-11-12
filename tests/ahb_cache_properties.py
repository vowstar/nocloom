"""AHB-Lite cache and protection at AXI4 targets on three transport paths."""
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from run_ahb_initiator import (MODES, Oracle, beat_addresses, configuration, driver,
                               parse, target_model, wdata)
from sim_engine import generated_rtl


def definition(path):
    d = configuration('clocked' if path == 'clocked' else 'switch')
    d['name'] = 'ahb_cache'
    d['initiators'][0]['data_width'] = 64 if path == 'wide' else 32
    d['targets'] = d['targets'][:1]
    d['transport'] = dict(data_width=32)
    if path == 'clocked':
        d['topology']['request']['exits'] = [dict(endpoint='tx', node='leaf')]
        d['topology']['response']['entries'] = [dict(endpoint='tx', node='leaf')]
    return d


def transfers(bits):
    out = []
    # Read each region before and after its write, with different read properties.
    for hp in range(16):
        for burst, beats in ((0, 1), (3, 4)):
            for phase, op in enumerate(('read', 'write', 'read')):
                out.append(dict(name=f'{hp}-{burst}-{op}-{len(out)}', src='h0', op=op,
                                addr=0x10000 + hp * 0x40 + burst * 8,
                                hburst=burst, size=bits.bit_length() - 4, beats=beats,
                                prot=hp ^ 15 if phase == 2 else hp, busy=[0] * beats, cancel=0, lock=0,
                                seed=(len(out) * 29) & 255))
    # Byte writes check the active lanes independently of full-word transfers.
    for hp in range(16):
        for op in ('write', 'read'):
            out.append(dict(name=f'byte-{hp}-{op}', src='h0', op=op,
                            addr=0x10800 + hp * 8 + hp % 4, hburst=0, size=0, beats=1,
                            prot=hp, busy=[0], cancel=0, lock=0, seed=hp * 7))
    # Target errors check non-posted completion and two-cycle AHB ERROR.
    for hp in range(16):
        for op in ('read', 'write'):
            out.append(dict(name=f'error-{hp}-{op}', src='h0', op=op,
                            addr=0x10f00, hburst=0, size=2, beats=1,
                            prot=hp, busy=[0], cancel=0, lock=0, seed=hp))
    return out


def bench(mapping, selected, bits):
    ports = {p['name']: p for p in mapping['ports']}
    sc = ports['nl_i_h0_htrans']['clock']
    tc = ports['nl_t_tx_awvalid']['clock']
    decl, init = [], []
    for n, p in ports.items():
        driven = p['endpoint'].startswith('target:') or n == 'nl_i_h0_hready'
        reg = p['direction'] == 'input' and not driven
        decl.append(f'{"reg" if reg else "wire"} [{p["width"] - 1}:0] {n};')
        if reg and p.get('signal') != 'clock':
            init.append(f'{n} = 0;')
    clocks = '\n'.join(f'initial {n} = 0; always #{3 if n == "clk_fast" else 7 if n == "clk_slow" else 5} {n} = ~{n};'
                       for n, p in ports.items() if p.get('signal') == 'clock')
    arrays = []
    for k, t in enumerate(selected):
        for name, value in dict(addr=t['addr'], size=t['size'], hburst=t['hburst'],
                                beats=t['beats'], write=int(t['op'] == 'write'),
                                prot=t['prot'], lock=0, cancel=0, seed=t['seed'], busy_at=k * 4).items():
            arrays.append(f'g_{name}[{k}] = {value};')
    count = len(selected)
    stimulus = driver('h0', bits, sc)
    end_address = 'if (canc || nx >= nb) begin nl_i_h0_htrans = 0; cb = -1; end'
    assert stimulus.count(end_address) == 1
    stimulus = stimulus.replace(end_address,
        'if (canc || nx >= nb) begin nl_i_h0_htrans = 0; nl_i_h0_hprot = ~g_prot[k]; cb = -1; end')
    return f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
assign nl_i_h0_hready = nl_i_h0_hreadyout;
{mapping['module']} dut({', '.join(f'.{n}({n})' for n in ports)});
{clocks}
reg abort = 0;
task automatic check(input condition, input [255:0] label_);
 if (condition !== 1'b1) begin $display("ASSERT_FAIL:AHB_CACHE.%0s", label_); $fatal(1); end
endtask
reg [63:0] g_addr [0:{count - 1}];
integer g_size [0:{count - 1}], g_hburst [0:{count - 1}], g_beats [0:{count - 1}];
integer g_prot [0:{count - 1}], g_seed [0:{count - 1}], g_busy_at [0:{count - 1}];
reg g_write [0:{count - 1}], g_lock [0:{count - 1}], g_cancel [0:{count - 1}];
integer g_busy [0:{4 * count - 1}];
function automatic [{bits - 1}:0] beat_data(input integer k, input integer beat, input integer bytes);
 integer lane;
 begin
 beat_data = 0;
 for (lane = 0; lane < bytes; lane = lane + 1)
 beat_data[8 * lane +: 8] = g_seed[k] + beat * 13 + lane * 7 + 8'h5a;
 end
endfunction
{target_model('tx', 'axi4', 32, 0, ports)}
{stimulus}
always @(posedge {tc}) if (reset_n) begin
 if (nl_t_tx_awvalid && nl_t_tx_awready) check(nl_t_tx_awaddr < 4096, "AW_LOCAL_ADDRESS");
 if (nl_t_tx_arvalid && nl_t_tx_arready) check(nl_t_tx_araddr < 4096, "AR_LOCAL_ADDRESS");
 if (nl_t_tx_wvalid && nl_t_tx_wready) begin
  check(nl_t_tx_wlast == (tx_wk == tx_wn), "WRITE_LAST");
  $display("EV D %h %h %h %0d", tx_wbeat, nl_t_tx_wdata, nl_t_tx_wstrb, nl_t_tx_wlast);
 end
end
initial begin : run
 integer k, a;
 {chr(10).join(init)}
 {chr(10).join(arrays)}
 for (k = 0; k < {count * 4}; k = k + 1) g_busy[k] = 0;
 repeat (12) @(posedge {sc});
 @(negedge {sc}); reset_n = 1;
 repeat (20) @(posedge {sc});
 for (k = 0; k < {count}; k = k + 1) h0_xfer(k);
 repeat (100) @(posedge {sc});
 for (a = 0; a < 4096; a = a + 1) $display("EV M tx %0h %h", a, mem_tx[a]);
 $display("NOCLOOM_PASS ahb cache"); $finish;
end
initial begin #10000000; $display("ASSERT_FAIL:AHB_CACHE.TIMEOUT"); $fatal(1); end
endmodule
'''


def compare(log, selected, bits):
    MODES['cache'] = dict(sources={'h0': (bits, 'main')},
                         targets={'tx': ('axi4', 32, 0x10000, 'main')})
    oracle = Oracle('cache')
    got = parse(log)
    requests, writes = [], []
    for k, t in enumerate(selected):
        want = oracle.run(t)
        observed = got['beats'].get(k, [])
        assert [b for b, _, _ in observed] == list(range(len(want['resp']))), ('source order', k, observed)
        assert [r for _, r, _ in observed] == want['resp'], ('source response', k, observed, want)
        for (_, _, data), expected in zip(observed, want['data']):
            if expected is not None:
                mask = ((1 << (8 * (1 << t['size']))) - 1) << (8 * (t['addr'] % (bits // 8))) if t['size'] < 2 else (1 << bits) - 1
                assert data is not None and data & mask == expected, ('source data', k, data, expected)
        # Explicit tables keep the expected cache values independent of RTL equations.
        cache = ((0, 1, 6, 7) if t['op'] == 'write' else (0, 1, 10, 11))[t['prot'] >> 2]
        prot = ((6, 2, 7, 3)[t['prot'] & 3])
        for beat, addr in enumerate(beat_addresses(t)):
            size = min(t['size'], 2)
            for offset in range(0, 1 << t['size'], 4):
                local = (addr + offset) % 4096
                requests.append((t['op'], local, size, cache, prot))
                if t['op'] == 'write':
                    lane = (addr + offset) % (bits // 8)
                    data = (wdata(t, beat, bits // 8) >> (8 * (lane // 4 * 4))) & 0xffffffff
                    mask = ((1 << (1 << size)) - 1) << (local % 4)
                    writes.append((local, data, mask))
    actual = []
    for b in got['bursts'].get('tx', []):
        assert b['burst'] == 1, ('target burst', b)
        actual.extend((b['op'], b['addr'] + i * (1 << b['size']), b['size'], b['cache'], b['prot'])
                      for i in range(b['len'] + 1))
    assert actual == requests, ('target attributes/address/order', next(((a, b) for a, b in zip(actual, requests) if a != b), (len(actual), len(requests))))
    data = []
    for line in log.splitlines():
        f = line.split()
        if f[:2] == ['EV', 'D']:
            addr, value, mask = (int(v, 16) for v in f[2:5])
            data.append((addr, value, mask))
    assert len(data) == len(writes), ('target data count', len(data), len(writes))
    for actual, expected in zip(data, writes):
        addr, value, mask = actual
        want_addr, want_value, want_mask = expected
        active = sum(255 << (8 * i) for i in range(4) if mask & (1 << i))
        assert addr == want_addr and mask == want_mask and value & active == want_value & active, ('target data/strobe/order', actual, expected)
    assert got['mem']['tx'] == dict(enumerate(oracle.mem['tx'])), 'target memory'


def run(cli, base):
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    for path in ('switch', 'wide', 'clocked'):
        case = work / path
        case.mkdir()
        config = case / 'input.yaml'
        d = definition(path)
        bits = d['initiators'][0]['data_width']
        config.write_text(json.dumps(d, indent=1) + '\n')
        p = subprocess.run([cli, 'generate', config, '--out-dir', case / 'generated'],
                           capture_output=True, text=True, timeout=180)
        assert p.returncode == 0, p.stderr
        mapping = json.loads((case / 'generated/nocloom_ahb_cache.map.json').read_text())
        selected = transfers(bits)
        tb = bench(mapping, selected, bits)
        rtl = generated_rtl(case / 'generated/nocloom_ahb_cache.v')
        for label in ('normal', 'read_cache', 'write_cache'):
            trial = case / label
            trial.mkdir()
            text = tb
            if label != 'normal':
                port = 'nl_t_tx_' + ('ar' if label == 'read_cache' else 'aw') + 'cache'
                text = tb.replace(f'.{port}({port})', f'.{port}(cache_value)').replace(
                    'module tb;', 'module tb;\nwire [3:0] cache_value;').replace(
                    'endmodule', f"assign {port} = cache_value & 4'h3;\nendmodule")
                assert text != tb
            (trial / 'network.v').write_text(rtl)
            (trial / 'tb.sv').write_text(text)
            rc = simulate(trial, case / 'compiled')
            log = (trial / 'simulation.log').read_text()
            assert rc == 0 and 'NOCLOOM_PASS ahb cache' in log and 'ASSERT_FAIL' not in log, log[-3000:]
            try:
                compare(log, selected, bits)
            except AssertionError as error:
                assert label != 'normal' and error.args[0][0] == 'target attributes/address/order', str(error)
                (trial / 'caught.txt').write_text(str(error) + '\n')
            else:
                assert label == 'normal', ('cache fault survived', path, label)
        (case / 'results.json').write_text(json.dumps(dict(status='PASS', transfers=len(selected),
            path=path, bits=bits, negatives=['read_cache', 'write_cache']), indent=1) + '\n')
    print('NOCLOOM_PASS ahb cache paths=3')
