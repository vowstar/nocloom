"""Finite pressure phases with one reproducible seed for reads and writes."""
import random

PHASES = (
    'Queue equal-length heads from each source for initial contention',
    'Hold grants through long packets and downstream stalls',
    'Reuse IDs while both read and write queues remain occupied',
    'Pause arrivals so stored pressure retires and restarts',
    'Mix short and long packets under sustained contention',
)


def stimulus(seed=261105):
    rng = random.Random(seed)
    lines = ['initial begin']
    for source in range(3):
        for direction, rom in enumerate(('RROM', 'WROM')):
            for phase, purpose in enumerate(PHASES):
                lines.append(f'  // Phase {phase}: {purpose}.')
                for offset in range(60):
                    n = phase * 60 + offset
                    length = (0 if phase == 0 else 7 if phase == 1 else
                              3 if phase == 2 else rng.choice((0, 1, 3, 7)))
                    pause = 7 if phase == 3 else 0
                    ident = offset % 4 if phase < 4 else rng.randrange(4)
                    address = source * 0x4000 + direction * 0x2000 + n * 32
                    lines.append(f"  {rom}[{source * 1024 + n}] = "
                                 f"{{4'd{pause}, 2'd{ident}, 8'd{length}, 32'h{address:08x}}};")
    return '\n'.join(lines + ['end'])
