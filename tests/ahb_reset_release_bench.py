"""Start AHB transfers on the first edge after reset with the shared bus model."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from ahb_cache_properties import bench, definition
from cross_hardware_sim import simulate
from run_ahb_initiator import MODES, compare


def transfers(first, bits):
    selected = [dict(name=f'{op}-{k}', src='h0', op=op, addr=0x10040,
                 hburst=0, size=(bits // 8).bit_length()-1, beats=1,
                 prot=3, busy=[0], cancel=0, lock=0, seed=37)
            for k, op in enumerate((first if first in ('read', 'write') else 'read',
                                    'read', 'write', 'read'))]
    if first == 'lock':
        selected[0]['lock'] = 1
    elif first == 'oversize':
        selected[0]['size'] += 1
    elif first == 'narrow':
        selected[0].update(hburst=3, size=0, beats=4, busy=[0]*4, cancel=1)
    return selected


def startup(bits):
    return f'''
 @(posedge clk_main); #1; reset_n = 1;
 repeat (5) @(posedge clk_main);
 @(negedge clk_main);
 nl_i_h0_hsel = 1; nl_i_h0_htrans = 2; nl_i_h0_haddr = 32'h10040;
 nl_i_h0_hsize = {(bits // 8).bit_length()-1}; nl_i_h0_hburst = 0;
 nl_i_h0_hwrite = 0; nl_i_h0_hprot = 3; nl_i_h0_hmastlock = 0;
 @(posedge clk_main);
 @(negedge clk_main); reset_n = 0; nl_i_h0_hsel = 0; nl_i_h0_htrans = 0;
 @(posedge clk_main); #1;
 check(nl_i_h0_hreadyout === 1'b1, "RESET_AFTER_ACTIVE");
 repeat (5) @(posedge clk_main);
 @(posedge clk_main); #1; reset_n = 1; first_check = 1;
'''


def run_case(args, work, bits, reset_mode, synchronize):
    case = work / f'{bits}-{reset_mode}'
    case.mkdir()
    cfg = definition('wide' if bits > 32 else 'switch')
    cfg['initiators'][0]['data_width'] = bits
    if bits == 32:
        cfg['clock_gating'] = False
    cfg['output']['register_reset'] = reset_mode
    cfg['clock_domains'][0]['reset_synchronizer'] = synchronize
    source = case / 'input.yaml'
    source.write_text(json.dumps(cfg))
    subprocess.run([args.cli, 'generate', str(source), '--out-dir', str(case / 'rtl')],
                   check=True, capture_output=True, text=True)
    mapping = json.loads((case / 'rtl/nocloom_ahb_cache.map.json').read_text())
    selected = transfers(args.first, bits)
    text = bench(mapping, selected, bits)
    delay = 'repeat (20) @(posedge clk_main);'
    release = '@(negedge clk_main); reset_n = 1;'
    start = '@(negedge clk_main);\n  nl_i_h0_hsel = 1;'
    assert text.count(delay) == text.count(release) == text.count(start) == 1
    properties = '\n'.join(f"g_lock[{k}] = {t['lock']}; g_cancel[{k}] = {t['cancel']};"
                           for k, t in enumerate(selected))
    text = text.replace(delay, '').replace(release, properties + startup(bits))
    text = text.replace(start, 'nl_i_h0_hsel = 1;')
    loop = f'for (k = 0; k < {len(selected)}; k = k + 1) h0_xfer(k);'
    assert text.count(loop) == 1
    text = text.replace(loop, loop.replace('h0_xfer(k);',
                        'begin h0_xfer(k); if (k == 0) first_done = 1; end'))
    invalid = int(args.first not in ('read', 'write'))
    text = text.replace('initial begin : run', f'''
reg first_done = 0, first_check = 0;
always @(posedge clk_main) if (reset_n && first_check && !first_done && {invalid})
 check(!nl_t_tx_awvalid && !nl_t_tx_arvalid, "LOCAL_REJECT_NO_ACCESS");
always @(posedge clk_main) begin
 #1; if (!reset_n) check(nl_i_h0_hreadyout === 1'b1, "RESET_READY_HIGH");
end
initial begin : run''')
    (case / 'tb.sv').write_text(text)
    bundle = json.loads((case / 'rtl/nocloom_ahb_cache.bundle.json').read_text())
    (case / 'network.v').write_text(''.join((case / 'rtl' / name).read_text()
                                          for name in bundle['simulation_sources']))
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    assert rc == 0, (case, log[-2000:])
    MODES['reset_release'] = dict(sources={'h0': (bits, 'main')},
                                 targets={'tx': ('axi4', 32, 0x10000, 'main')})
    problems = compare('reset_release', log, selected, [])
    assert not problems, (case, problems[:3])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cli', required=True)
    p.add_argument('--work', required=True)
    p.add_argument('--first', choices=('read', 'write', 'lock', 'oversize', 'narrow'), required=True)
    args = p.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work)+'\n')
    for synchronize in (True, False):
        release_work = work / ('synchronized' if synchronize else 'direct')
        release_work.mkdir()
        for bits in (32, 128):
            for reset_mode in ('asynchronous', 'synchronous'):
                run_case(args, release_work, bits, reset_mode, synchronize)
    print(f'NOCLOOM_PASS ahb reset release first={args.first} cases=8')


if __name__ == '__main__':
    main()
