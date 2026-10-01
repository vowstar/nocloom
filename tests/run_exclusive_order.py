"""Exclusive access (AXI A7.2): ordering at the target and the byte range of
the clearing write, through the public CLI and a transaction bench.

held_data   A normal write of s1 whose AW the AXI4 target has accepted but
            whose data it holds back; an exclusive read of s0 to the same word.
            At a multi-burst target the read overtakes the write and returns
            the old bytes, so the exclusive write of s0 fails (AXI A7.2: a
            write to the location since the exclusive read). At a one-burst
            target the shared in-flight pool lets the read in only after the
            write's B has left, so it returns the landed word and the
            exclusive write succeeds. Network monitors and target-mode
            targets both; the target-mode target is the bench's own monitor.
ranges      Every exclusive-capable target, exclusive INCR and WRAP bursts of
            1 to 32 bytes, and a foreign normal write that overlaps the first
            or last word, sits just below or above, or is a WRAP or FIXED
            burst whose container or beat decides the overlap. The model in
            exclusive.py gives every response and byte.
"""
import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from exclusive import OKAY, SLVERR, configuration, targets
from exclusive_bench import write_bench
from sim_engine import generated_rtl

MODES = ('graph', 'wide')
# Exclusive shapes (length, size, burst) at an offset aligned to their bytes.
SHAPES = ((0, 2, 1), (1, 2, 1), (3, 2, 1), (1, 2, 2), (3, 2, 2), (7, 2, 2))


def held_data(b, t, offset):
    """s1 writes one word that the target holds; s0's exclusive read of it
    while the write is in flight: at a multi-burst target the read overtakes
    and returns the old word, so s0's exclusive write fails; at a one-burst
    target the shared in-flight pool lets the read in only after the write's
    B has left, so it returns the landed word and the exclusive write
    succeeds."""
    old = b.model.word(t, offset)
    values, beats = b.data('s1', offset, 0, 2, 1)
    word, strobe = beats[0]
    b.emit(f'  s1_wq_data[0] = 32\'h{word:08X}; s1_wq_strb[0] = 4\'h{strobe:X};')
    b.unexpect()
    b.emit(f'  {t}_whold = 1; mark_aw = {t}_awcount;')
    b.emit('  fork')
    b.emit(f'   write_s1(32\'h{b.base(t) + offset:08X}, 0, 2, 1, 0, 0, 4\'h0);')
    if targets(b.mode)[t][3].get('max_outstanding', 1) == 1:
        # The read waits behind the write in the outlet's shared in-flight
        # pool; the held data releases as soon as the AW has landed.
        b.emit(f'   begin while ({t}_awcount == mark_aw) @(posedge clk_main); {t}_whold = 0; end')
        b.emit(f'   begin while ({t}_awcount == mark_aw) @(posedge clk_main);')
        b.emit(f'    read_s0(32\'h{b.base(t) + offset:08X}, 0, 2, 1, 1, 1, 4\'h0); end')
    else:
        # The read overtakes the held write; only then does the data release.
        b.emit(f'   begin while ({t}_awcount == mark_aw) @(posedge clk_main);')
        b.emit(f'    read_s0(32\'h{b.base(t) + offset:08X}, 0, 2, 1, 1, 1, 4\'h0);')
        b.emit(f'    {t}_whold = 0; end')
    b.emit('  join')
    lane = (offset % (b.ports['nl_i_s0_wdata'] // 8)) * 8
    b.emit(f'  check(s1_bresp === 2\'d0, "{t.upper()}_HELD_WRITE_OKAY");')
    b.model.memory[t].update(values)
    b.model.clear(t, offset, 4)
    if targets(b.mode)[t][3].get('max_outstanding', 1) == 1:
        b.emit(f'  check(s0_rresp[0] === 2\'d1, "{t.upper()}_HELD_READ_EXOKAY");')
        b.emit(f'  check(s0_rdata[0][{lane} +: 32] === 32\'h{word:08X}, "{t.upper()}_HELD_READ_NEW");')
        b.model.read('s0', t, offset, 0, 2, 1, 1)
        b.xwrite('s0', t, offset, ident=1, tag=t + '_after_held_data')
    else:
        b.emit(f'  check(s0_rresp[0] === 2\'d1, "{t.upper()}_HELD_READ_EXOKAY");')
        b.emit(f'  check(s0_rdata[0][{lane} +: 32] === 32\'h{old:08X}, "{t.upper()}_HELD_READ_OLD");')
        b.model.reservations[t].pop(('s0', 1), None)
        b.xwrite('s0', t, offset, ident=1, expect=(OKAY, False), tag=t + '_after_held_data')
    b.read('s1', t, offset, tag=t + '_held_readback')


def interferers(offset, total):
    """Foreign normal writes (name, offset, length, size, burst) around an
    exclusive region [offset, offset + total)."""
    return (('first', offset, 0, 2, 1),
            ('last', offset + total - 4, 0, 2, 1),
            ('below', offset - 4, 0, 2, 1),
            ('above', offset + total, 0, 2, 1),
            # WRAP of 16 bytes starting 8 bytes into the container that holds
            # the region's first word: the container overlaps the region.
            ('wrap_in', (offset & ~15) + 8, 3, 2, 2),
            # WRAP whose container ends at the region: its span from the start
            # address would overlap, the container does not.
            ('wrap_below', offset - 8, 3, 2, 2),
            # FIXED beats on the word below: only the beat counts.
            ('fixed_below', offset - 4, 3, 2, 0))


def episodes(mode):
    held = [t for t, spec in targets(mode).items() if spec[0] == 'axi4' and spec[2]]
    ranged = [t for t, spec in targets(mode).items() if spec[2]]

    def run(b):
        for t in held:
            b.episode('held_data_' + t)
            b.write('s1', t, 0x40, tag=t + '_held_seed')
            held_data(b, t, 0x40)
        for t in ranged:
            b.episode('ranges_' + t)
            area = 0x100
            for length, size, burst in SHAPES:
                total = (length + 1) << size
                for name, w_offset, w_length, w_size, w_burst in interferers(0x20, total):
                    offset = area + 0x20
                    tag = f'{t}_{length}_{burst}_{name}'
                    b.xread('s0', t, offset, length=length, size=size, burst=burst, ident=1,
                            tag=tag)
                    foreign = dict(length=w_length, size=w_size, burst=w_burst, tag=tag + '_foreign')
                    if w_burst == 0 and targets(mode)[t][0] != 'axi4':
                        # APB, AHB and AXI4-Lite targets answer a FIXED burst
                        # with SLVERR locally: it is no write and clears nothing.
                        kept = dict(b.model.reservations[t]), dict(b.model.memory[t])
                        b.write('s1', t, area + w_offset, expect=(SLVERR, False), **foreign)
                        b.model.reservations[t], b.model.memory[t] = kept
                    else:
                        b.write('s1', t, area + w_offset, **foreign)
                    b.xwrite('s0', t, offset, length=length, size=size, burst=burst, ident=1,
                             tag=tag)
                    b.read('s1', t, offset, length=length, size=size, burst=burst,
                           tag=tag + '_readback')
                    area += 0x40
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--mode', choices=MODES, required=True)
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    yaml = work / 'input.yaml'
    yaml.write_text(json.dumps(configuration(args.mode), indent=1) + '\n')
    proc = subprocess.run([args.cli, 'generate', str(yaml), '--out-dir', str(work / 'out')],
                          capture_output=True, text=True, timeout=300, check=False)
    assert proc.returncode == 0, proc.stderr
    mapping = json.loads((work / 'out/nocloom_exclusive.map.json').read_text())
    case = work / 'bench'
    case.mkdir()
    (case / 'network.v').write_text(generated_rtl(work / 'out/nocloom_exclusive.v'))
    steps = write_bench(case, mapping, args.mode, episodes(args.mode))
    rc = simulate(case, work / 'compiled')
    log = (case / 'simulation.log').read_text()
    passed = rc == 0 and f'NOCLOOM_PASS exclusive {args.mode}' in log and 'ASSERT_FAIL' not in log
    seen = [line.split()[1] for line in log.splitlines() if line.startswith('EPISODE ')]
    (work / 'results.json').write_text(json.dumps(
        {'simulation_exit': rc, 'episodes': seen, 'steps': steps,
         'status': 'PASS' if passed else 'FAIL',
         'rtl_sha256': hashlib.sha256((case / 'network.v').read_bytes()).hexdigest(),
         'tb_sha256': hashlib.sha256((case / 'tb.sv').read_bytes()).hexdigest()}, indent=1) + '\n')
    assert passed, [line for line in log.splitlines() if 'ASSERT_FAIL' in line][:3] or log[-2000:]
    print(f'NOCLOOM_PASS exclusive_order {args.mode} episodes={len(seen)} steps={steps}')


if __name__ == '__main__':
    main()
