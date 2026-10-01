"""Transaction bench for multiple outstanding bursts.

Each source issues its reads and writes back to back without waiting for
responses. Independent checks:

- Scoreboard (per AXI ID): a B or an R beat must belong to the oldest burst
  of its ID still waiting at that source face, so bursts of one ID neither
  reorder nor interleave; R beats of different IDs may interleave, and each
  burst counts its own beats. ID, response, data and RLAST are checked. Bursts
  of different IDs may complete in any order; the bench counts completions
  that overtake an older burst of another ID and R beats that interleave with
  an unfinished burst, and requires them where an episode is built to produce
  them.
- Concurrency: per episode the bench requires the bursts in flight at a
  source face and at a multi-burst target port to reach the configured
  capacities, and in cross-target episodes bursts of one source in flight at
  two targets at once, so a bench that serializes cannot pass vacuously.
- Destination rule (per ID): when a target port accepts an AR or AW, every
  earlier burst of the same source, direction and AXI ID bound for another
  destination (a target or local completion) must already be complete at the
  source face.
- ID table bound: when a target port accepts an AR or AW, the distinct AXI IDs
  among that source direction's bursts accepted at target ports and not yet
  complete must not exceed the source's ID table (a lower bound of the IDs it
  tracks, since local completions are not visible at a target).
- Target port rules: at most the target's capacity in flight; the target ID
  follows definition.target_ids in the map; W beats follow AW order; reads and
  writes of different IDs overlap and complete in any order.
- Slave models may return different IDs in reverse arrival order (legal AXI),
  so an outlet that matched responses by position alone would fail.

In the clocked modes the target models run on the far domain clock and the
sources on main; the rule monitors read the source tables across the two
clocks, which is safe because a request needs several cycles of both clocks
to cross. Each episode prints its length in main cycles (EPISODE_CYCLES); the
runner compares the measured episodes with the same network at one shared burst slot per source and target.

Source and burst serial are encoded in the local address of AXI4 targets:
bit 13 is the source, bits 12:6 the serial. APB4 targets keep bit 11 for the
source and bits 10:6 for the serial modulo 32 (a 4 KiB window).
"""

from multi_outstanding import (CLOCKED, DENIED, ENTRY, SOURCES, capacity, clocked, crossing,
                               id_bits, len_bits, source_clock, target_clock, targets,
                               tracked_ids)
from multi_outstanding import base as base_mode
from endpoint_ports import port_slots

MAXC = 96
OK, SLVERR, DECERR = 0, 2, 3
# Dedicated targets: a 4 KiB window, data by the 12-bit local address.
DEDICATED = ('apb4', 'axi4_lite')


def _fill(text, **values):
    for key, value in values.items():
        text = text.replace('@' + key + '@', str(value))
    return text


class Cmd:
    """One burst; `expect` fills dest, resp and tag."""

    def __init__(self, write, addr, beats, ident, burst=1, lock=0):
        self.write, self.addr, self.beats, self.id = write, addr, beats, ident
        self.burst, self.lock = burst, lock
        self.wlx = 0
        self.dest, self.resp, self.tag = None, None, None


class Model:
    def __init__(self, mode):
        self.mode = mode
        self.targets = targets(mode)
        self.names = sorted(self.targets)
        self.index = {t: i for i, t in enumerate(self.names)}

    def base(self, target):
        return self.targets[target][1]

    def addr(self, target, source, slot, offset=0):
        if self.targets[target][0] in DEDICATED:
            return self.base(target) + (source << 11) + ((slot % 32) << 6) + offset
        return self.base(target) + (source << 13) + ((slot % 128) << 6) + offset

    def expect(self, source, cmd, overrides=None):
        name = 's%d' % source
        # No target supports exclusive access: AxLOCK is ignored (a normal
        # access). An illegal WRAP completes locally.
        if cmd.burst == 2 and (cmd.addr % 4 or cmd.beats not in (2, 4, 8, 16)):
            cmd.dest, cmd.resp, cmd.tag = 'local', SLVERR, None
            cmd.dest_index = -1
            return cmd
        hit = None
        for t, spec in self.targets.items():
            if spec[1] <= cmd.addr < spec[1] + spec[2]:
                hit = t
        if hit is None or (self.mode == 'routes' and (name, hit) in DENIED):
            # A decode miss goes to the source's default target, the reached
            # target with the smallest window (the lowest based), which
            # answers DECERR without access; its data is zero.
            reached = [t for t in self.names if not (self.mode == 'routes' and (name, t) in DENIED)]
            cmd.dest = min(reached, key=lambda t: (self.targets[t][2], self.targets[t][1]))
            cmd.resp, cmd.tag = DECERR, None
        else:
            cmd.dest, cmd.resp, cmd.tag = hit, OK, self.index[hit]
        if overrides and cmd.dest in overrides:
            cmd.resp, cmd.tag = overrides[cmd.dest], None
        cmd.dest_index = -1 if cmd.dest == 'local' else self.index[cmd.dest]
        return cmd


def episodes(mode):
    """The scenario of one mode: a list of episodes for write_bench."""
    import random
    import re
    m = Model(mode)
    rng = random.Random(20260930)
    names = m.names
    miss = 0x70000

    mask = (1 << id_bits(mode)) - 1
    tracked = id_bits(mode) > 0

    def program(source, write, items, overrides=None):
        out = []
        for slot, item in enumerate(items):
            target, beats, ident, extra = (list(item) + [{}])[:4]
            cmd = Cmd(write, miss + (source << 11) + ((slot % 32) << 6) if target is None else
                      m.addr(target, source, slot) + extra.get('offset', 0), beats, ident & mask,
                      extra.get('burst', 1), extra.get('lock', 0))
            cmd = m.expect(source, cmd, overrides)
            if extra.get('wlx'):
                cmd.wlx = 1
                if cmd.dest != 'local':
                    cmd.resp = SLVERR
            out.append(cmd)
        return out

    def failing(cmds, serial):
        cmds[serial].resp, cmds[serial].tag = SLVERR, None
        return cmds

    n0 = capacity(mode, 's0')
    first = names[0]
    multi = [t for t in names if m.targets[t][3] > 1][0]
    other = [t for t in names if t not in (first, multi) and m.targets[t][0] == 'axi4']
    apb = [t for t in names if m.targets[t][0] in DEDICATED]
    timed = [t for t in names if m.targets[t][4]]
    # A fast destination after the slow first target.
    fast = multi if multi != first else other[0]
    eps = []
    k = m.targets[multi][3]
    # 1. N bursts in flight to one multi-burst target with one ID: the
    # source face and the target port both fill up.
    count, latency = (8, 12) if k <= 4 else (30, 60)
    eps.append({'name': 'same_target_reads', 'knobs': [f'{multi}_rlat = {latency};'],
                'cmds': {('s0', 'rd'): program(0, False, [(multi, 4, 1)] * count)},
                'checks': [(f's0_rmax >= {min(n0, k + 1)}', 'S0_READS_IN_FLIGHT'),
                           (f'{multi}_rmax == {min(k, n0)}', 'TARGET_READS_IN_FLIGHT')]})
    eps.append({'name': 'same_target_writes', 'knobs': [f'{multi}_blat = {latency};'],
                'cmds': {('s0', 'wr'): program(0, True, [(multi, 4, 1)] * count)},
                'checks': [(f's0_wmax >= {min(n0, k + 1)}', 'S0_WRITES_IN_FLIGHT'),
                           (f'{multi}_wmax == {min(k, n0)}', 'TARGET_WRITES_IN_FLIGHT')]})
    # A single-burst target with mixed IDs: one burst at the target, the
    # next waiting in the network and one more in the source's issue stage.
    single = first if m.targets[first][3] == 1 else other[0]
    eps.append({'name': 'single_target_mixed_ids', 'knobs': [f'{single}_rlat = 12;',
                                                             f'{single}_blat = 12;'],
                'cmds': {('s0', 'rd'): program(0, False, [(single, 4, j % 4) for j in range(8)]),
                         ('s0', 'wr'): program(0, True, [(single, 2, j % 4) for j in range(8)])},
                'checks': [('s0_rmax >= 2 && s0_wmax >= 2', 'S0_PAST_ONE_IN_FLIGHT')]})
    # 2. Bursts of different IDs at two targets at once: the slow target's
    # burst is overtaken by the fast target's, which needs the tag to find
    # its burst. With one ID (no AxID) every burst waits for the slow one.
    cross = [(first, 4, 0), (fast, 2, 1), (fast, 1, 1), (fast, 3, 1), (first, 2, 0), (fast, 1, 1)]
    for direction, write in (('reads', False), ('writes', True)):
        knob = f'{first}_rlat = 40;' if not write else f'{first}_blat = 40;'
        checks = []
        if tracked:
            letter = 'w' if write else 'r'
            checks = [(f's0_{letter}ooo > 0', 'OVERTAKEN_BY_ANOTHER_ID'),
                      (f's0_{letter}dests >= 2', 'TWO_TARGETS_IN_FLIGHT')]
        eps.append({'name': f'cross_target_{direction}', 'knobs': [knob],
                    'cmds': {('s0', 'wr' if write else 'rd'): program(0, write, cross)},
                    'checks': checks})
    # The same ID moving to another target waits until that ID drains; a
    # later burst of another ID waits behind it in the issue stage.
    switch = [(first, 4, 1), (fast, 2, 1), (fast, 1, 2), (first, 1, 1), (fast, 2, 2)]
    eps.append({'name': 'same_id_switch', 'knobs': [f'{first}_rlat = 30;', f'{first}_blat = 30;'],
                'cmds': {('s0', 'rd'): program(0, False, switch),
                         ('s0', 'wr'): program(0, True, switch)}})
    # Two bursts of one ID at the slow target, then that ID at the fast one:
    # it waits for both, not only the first.
    drain = [(first, 3, 1), (first, 2, 1), (fast, 1, 1), (fast, 2, 2), (first, 1, 1)]
    eps.append({'name': 'same_id_drains_count',
                'knobs': [f'{first}_rlat = 30;', f'{first}_blat = 30;'],
                'cmds': {('s0', 'rd'): program(0, False, drain),
                         ('s0', 'wr'): program(0, True, drain)}})
    # A dedicated (APB4) target returns the tag of a burst in a later slot.
    if apb:
        tagged = [(first, 4, 0), (apb[0], 2, 1), (apb[0], 1, 2), (first, 1, 3)]
        eps.append({'name': 'dedicated_target_tag',
                    'knobs': [f'{first}_rlat = 30;', f'{first}_blat = 30;',
                              f'{apb[0]}_delay = 1;'],
                    'cmds': {('s0', 'rd'): program(0, False, tagged),
                             ('s0', 'wr'): program(0, True, tagged)}})
    # More IDs than the ID table holds, all at the slow first target: a new
    # ID waits for an entry (two entries in the routes mode).
    crowd = [(first, 2, j) for j in range(6)] + [(fast, 1, 7), (first, 1, 0)]
    eps.append({'name': 'id_table_full', 'knobs': [f'{first}_rlat = 20;', f'{first}_blat = 20;'],
                'cmds': {('s0', 'rd'): program(0, False, crowd),
                         ('s0', 'wr'): program(0, True, crowd)}})
    # Three IDs at three slow targets, then the third ID completing locally:
    # where the table holds two IDs, the third waits for an entry, and the
    # local completion of that ID waits for its slow burst.
    spare = [t for t in names if m.targets[t][0] == 'axi4' and t not in (first, fast)]
    if spare:
        late = [(first, 4, 0), (fast, 4, 1), (spare[0], 4, 2), (None, 1, 2, {'burst': 2})]
        eps.append({'name': 'third_id_then_local',
                    'knobs': [f'{t}_rlat = 40; {t}_blat = 40;' for t in (first, fast, spare[0])],
                    'cmds': {('s0', 'rd'): program(0, False, late),
                             ('s0', 'wr'): program(0, True, late)}})
    # A local completion offered while RREADY is held low, and a network
    # response of another ID arriving meanwhile: the offered local beat stays
    # on the face until it is taken (AXI A3.2.1), then the network burst.
    offered = [(first, 2, 0), (first, 1, 1, {'burst': 2}), (first, 1, 2)]
    eps.append({'name': 'local_offered_under_hold',
                'knobs': [f'{first}_rlat = 8;', 's0_rhold = 40;'],
                'cmds': {('s0', 'rd'): program(0, False, offered)}})
    # Targets answering in reverse latency order with interleaved IDs from
    # both sources.
    reverse = [(first, 2, 0), (fast, 2, 1), (first, 1, 0), (fast, 4, 2), (first, 3, 0),
               (fast, 1, 3)]
    eps.append({'name': 'reverse_latencies',
                'knobs': [f'{first}_rlat = 25;', f'{first}_blat = 25;', f'{fast}_rlat = 2;',
                          f'{fast}_blat = 2;'],
                'cmds': {('s0', 'rd'): program(0, False, reverse),
                         ('s0', 'wr'): program(0, True, reverse),
                         ('s1', 'rd'): program(1, False, [(fast, 2, 2), (first, 1, 3)] * 3),
                         ('s1', 'wr'): program(1, True, [(fast, 2, 2), (first, 1, 3)] * 3)}})
    # Local completions (decode misses, illegal WRAP) interleaved with network
    # bursts of the same and other IDs.
    local_ids = [(first, 4, 0), (None, 2, 1), (first, 2, 0),
                 (first, 2, 2, {'burst': 2, 'offset': 2}), (fast, 2, 1), (None, 1, 0),
                 (fast, 1, 2), (first, 2, 1, {'burst': 2, 'offset': 4}), (fast, 1, 3)]
    eps.append({'name': 'local_errors_by_id',
                'knobs': [f'{first}_rlat = 18;', f'{first}_blat = 18;', 's0_rgap = 3;',
                          's0_bgap = 3;'],
                'cmds': {('s0', 'rd'): program(0, False, local_ids),
                         ('s0', 'wr'): program(0, True, local_ids)}})
    # 3. Local completions between network bursts, and decode misses.
    mixed = [(first, 4, 0), (first, 2, 1), (first, 2, 2, {'burst': 2, 'offset': 2}), (first, 3, 3),
             (None, 2, 1), (first, 4, 2, {'burst': 2, 'offset': 4}), (multi, 2, 0), (None, 1, 3),
             (first, 2, 0)] + [(t, 2, 1) for t in apb]
    eps.append({'name': 'local_errors_mixed', 'knobs': [f'{first}_rlat = 8;', f'{first}_blat = 8;'],
                'cmds': {('s0', 'rd'): program(0, False, mixed),
                         ('s0', 'wr'): program(0, True, mixed)}})
    between = [(multi, 2, 1), (multi, 1, 1, {'burst': 2}), (multi, 3, 1), (multi, 1, 1),
               (multi, 2, 1, {'burst': 2, 'offset': 2}), (multi, 2, 1), (multi, 4, 1)]
    eps.append({'name': 'local_behind_backpressured',
                'knobs': ['s0_rgap = 4;', 's0_bgap = 4;'],
                'cmds': {('s0', 'rd'): program(0, False, between * 3),
                         ('s0', 'wr'): program(0, True, between * 3)}})
    held = [(first, 1, 1), (None, 1, 1), (first, 2, 1), (first, 1, 1)] * 2
    eps.append({'name': 'local_slot_under_b_backpressure',
                'knobs': [f'{first}_blat = 0;', 's0_bgap = 7;', 's0_rgap = 7;'],
                'cmds': {('s0', 'wr'): program(0, True, held),
                         ('s0', 'rd'): program(0, False, held)}})
    eps.append({'name': 'local_and_network_two_sources',
                'knobs': [f'{first}_blat = 1;', 's0_bgap = 7;', 's1_bgap = 7;', 's0_rgap = 7;',
                          's1_rgap = 7;'],
                'cmds': {('s0', 'wr'): program(0, True, [(first, 1, 1)] * 16),
                         ('s1', 'wr'): program(1, True, [(None, 1, 2)] * 16),
                         ('s0', 'rd'): program(0, False, [(first, 1, 1)] * 16),
                         ('s1', 'rd'): program(1, False, [(None, 1, 2)] * 16)}})
    eps.append({'name': 'two_sources_reverse_zero_latency',
                'knobs': [f'{multi}_reorder = 1;'],
                'cmds': {('s0', 'wr'): program(0, True, [(multi, 1, 1)] * 6),
                         ('s1', 'wr'): program(1, True, [(multi, 1, 2)] * 6),
                         ('s0', 'rd'): program(0, False, [(multi, 1, 1)] * 6),
                         ('s1', 'rd'): program(1, False, [(multi, 1, 2)] * 6)}})
    alternate = [(multi, 2, 1 + (j % 2)) for j in range(6)]
    eps.append({'name': 'alternating_ids_with_error',
                'knobs': [f'{multi}_rlat = 20;', f'{multi}_blat = 20;', f'{multi}_reorder = 1;',
                          f'{multi}_err = 1;'],
                'cmds': {('s0', 'rd'): failing(program(0, False, alternate), 1),
                         ('s0', 'wr'): failing(program(0, True, alternate), 1)}})
    eps.append({'name': 'local_between_pipelined',
                'cmds': {('s0', 'rd'): program(0, False, between),
                         ('s0', 'wr'): program(0, True, between)}})
    behind = [(first, 2, 1), (None, 1, 1), (first, 2, 1), (None, 2, 1), (first, 1, 1)]
    eps.append({'name': 'misses_behind_slow_target',
                'knobs': [f'{first}_rlat = 15;', f'{first}_blat = 15;'],
                'cmds': {('s0', 'rd'): program(0, False, behind),
                         ('s0', 'wr'): program(0, True, behind)}})
    slow = [(multi, 4, 1), (None, 2, 2), (first, 2, 3), (None, 1, 0), (None, 3, 1), (multi, 1, 2)]
    eps.append({'name': 'miss_after_slow_target',
                'knobs': [f'{multi}_rlat = 40;', f'{multi}_blat = 40;'],
                'cmds': {('s0', 'rd'): program(0, False, slow),
                         ('s0', 'wr'): program(0, True, slow)}})
    # 4. A multi-burst AXI4 target: same-ID reads overlap, writes of two
    # sources overlap and their B may return in reverse order. One burst
    # answers SLVERR at the target: the error must reach exactly that burst.
    eps.append({'name': 'target_pipelined_reads',
                'knobs': [f'{multi}_rlat = 10;', f'{multi}_err = 2;'],
                'cmds': {('s0', 'rd'): failing(program(0, False, [(multi, 4, 1)] * 6), 2)},
                'checks': [(f'{multi}_rmax == {min(k, n0, 6)}', 'TARGET_READS_IN_FLIGHT'),
                           (f's0_rmax >= {min(n0, k + 1, 6)}', 'S0_READS_IN_FLIGHT')]})
    eps.append({'name': 'target_pipelined_writes',
                'knobs': [f'{multi}_blat = 10;', f'{multi}_reorder = 1;', f'{multi}_err = 3;'],
                'cmds': {('s0', 'wr'): failing(program(0, True, [(multi, 4, 1)] * 6), 3),
                         ('s1', 'wr'): program(1, True, [(multi, 2, 2)] * 4)},
                'checks': [(f'{multi}_wmax == {k}' if k <= 4 else f'{multi}_wmax >= 4',
                            'TARGET_WRITES_IN_FLIGHT')]})
    eps.append({'name': 'target_mixed_ids', 'knobs': [f'{multi}_rlat = 5;', f'{multi}_reorder = 1;'],
                'cmds': {('s0', 'rd'): program(0, False, [(multi, 2, i % 3) for i in range(6)]),
                         ('s1', 'rd'): program(1, False, [(multi, 3, 1)] * 3),
                         ('s0', 'wr'): program(0, True, [(multi, 2, i % 2) for i in range(5)]),
                         ('s1', 'wr'): program(1, True, [(multi, 1, 3)] * 3)}})
    # The pool is shared by both directions: a hung read holds one entry of
    # the multi-burst target while writes flow through the rest (capacity
    # one would block them; see the target timeout bench).
    eps.append({'name': 'hung_read_writes_flow', 'reset_after': 200,
                'knobs': [f'{multi}_blat = 30;', f'{multi}_hang = 1;'],
                'cmds': {('s0', 'rd'): program(0, False, [(multi, 4, 1)]),
                         ('s0', 'wr'): program(0, True, [(multi, 2, 1)] * 8)},
                'checks': [(f'{multi}_rmax == 1', 'HUNG_READ_HELD'),
                           (f'{multi}_wmax >= {min(k - 1, 3)}', 'WRITES_FLOW_AROUND_IT'),
                           ('tgt_done >= 1', 'WRITE_COMPLETED_WHILE_READ_HUNG')]})
    # 5. Many bursts with distinct IDs spread over every AXI4 target at
    # different latencies: the slot table fills.
    axi = [t for t in names if m.targets[t][0] == 'axi4']
    mask = (1 << id_bits(mode)) - 1
    spread = [(axi[(j & mask) % len(axi)], 1 + j % 3, j) for j in range(min(MAXC, n0 + 8))]
    lat = {t: (5 + 13 * i) % 37 for i, t in enumerate(axi)}
    # Reads and writes share the capacity: each direction spreads on its own,
    # then both together. Overtaking evidence lives in the id_reorder_* episodes
    # below: with the targets' shared pools a capacity-one target queues the
    # second direction behind the first, so spread episodes do not overtake.
    for write, letter in ((False, 'r'), (True, 'w')):
        eps.append({'name': f'many_ids_spread_{letter}', 'limit': 120000,
                    'knobs': [f'{t}_rlat = {lat[t]}; {t}_blat = {lat[t]};' for t in axi] +
                             [f'{multi}_reorder = 1;'],
                    'cmds': {('s0', 'wr' if write else 'rd'): program(0, write, spread)},
                    'checks': [(f's0_{letter}max >= 2', 'S0_PAST_ONE_IN_FLIGHT')]})
    eps.append({'name': 'many_ids_spread', 'limit': 120000,
                'knobs': [f'{t}_rlat = {lat[t]}; {t}_blat = {lat[t]};' for t in axi] +
                         [f'{multi}_reorder = 1;'],
                'cmds': {('s0', 'rd'): program(0, False, spread),
                         ('s0', 'wr'): program(0, True, spread)},
                'checks': [('s0_rmax >= 2 && s0_wmax >= 2', 'S0_PAST_ONE_IN_FLIGHT')]})
    # Responses overtake an older burst of another ID: reads at a fast and a
    # slow target with no writes between (at a one-burst target a write would
    # hold the shared in-flight pool ahead of the read), then writes likewise.
    if tracked and len(axi) >= 2:
        # The slow target must not be a timed one: a timed target's latency
        # knob is not scaled in clocked modes, which would thin the margin.
        slow = [t for t in axi if not m.targets[t][4]][-1]
        fast_t = axi[0] if axi[0] != slow else axi[1]
        eps.append({'name': 'id_reorder_reads',
                    'knobs': [f'{fast_t}_rlat = 4;', f'{slow}_rlat = 30;'],
                    'cmds': {('s0', 'rd'): program(0, False, [(fast_t, 2, 0), (slow, 2, 1),
                                                              (fast_t, 2, 2), (slow, 2, 3)])},
                    'checks': [('s0_rooo > 0', 'OVERTAKEN_BY_ANOTHER_ID')]})
        eps.append({'name': 'id_reorder_writes',
                    'knobs': [f'{fast_t}_blat = 4;', f'{slow}_blat = 30;'],
                    'cmds': {('s0', 'wr'): program(0, True, [(fast_t, 2, 0), (slow, 2, 1),
                                                             (fast_t, 2, 2), (slow, 2, 3)])},
                    'checks': [('s0_wooo > 0', 'OVERTAKEN_BY_ANOTHER_ID')]})
    # Every burst a source can keep: four IDs, each bound to its own
    # multi-burst target, fill the source's slot table at four targets. With
    # the targets' in-flight pools shared by both directions, the four targets
    # and the network hold less than they did, so the source's table fills
    # only to what the system absorbs; the bound is the measured fill with
    # margin.
    deep = [t for t in names if m.targets[t][3] >= 16]
    if len(deep) >= 4 and n0 == 64:
        full = [(deep[j % 4], 2, j % 4) for j in range(72)]
        eps.append({'name': 'all_slots_four_targets', 'limit': 120000,
                    'knobs': [f'{t}_rlat = {400 - 20 * i}; {t}_blat = {400 - 20 * i};'
                              for i, t in enumerate(deep[:4])],
                    'cmds': {('s0', 'rd'): program(0, False, full),
                             ('s0', 'wr'): program(0, True, full)},
                    'checks': [('s0_rmax >= 30 && s0_wmax >= 30', 'S0_ALL_SLOTS'),
                               ('s0_rooo > 0 && s0_wooo > 0', 'OVERTAKEN_BY_ANOTHER_ID'),
                               ('s0_rdests == 4 && s0_wdests == 4', 'FOUR_TARGETS_IN_FLIGHT')] +
                              [(f'{t}_rmax + {t}_wmax >= 16', 'TARGET_POOL_FULL')
                               for t in deep[:4]]})
    # 6. Random traffic from both sources with response backpressure.
    choices = names + [None]
    rand = {}
    for s in (0, 1):
        for direction, write in (('rd', False), ('wr', True)):
            items = []
            for _ in range(24):
                t = rng.choice(choices)
                extra = {'lock': 1} if rng.random() < 0.08 else {}
                items.append((t, rng.randint(1, 8), rng.randint(0, 3), extra))
            rand[('s%d' % s, direction)] = program(s, write, items)
    eps.append({'name': 'two_sources_random', 'limit': 60000,
                'knobs': [f'{t}_rlat = {rng.randint(0, 9)}; {t}_blat = {rng.randint(0, 9)};'
                          for t in names if m.targets[t][0] == 'axi4'] +
                         [f'{multi}_reorder = 1;', 's0_rgap = 3;', 's0_bgap = 2;',
                          's1_rgap = 2;', 's1_bgap = 3;'] + [f'{t}_delay = 2;' for t in apb],
                'cmds': rand})
    # A read cut into packets at a dedicated target (128-byte packets) holds the
    # source's reassembly context only. Slow reads of other IDs issued before it
    # answer while its first packet streams and go out at that packet's end,
    # before its second packet: the R face interleaves bursts.
    if apb and len_bits(mode) == 8 and not (base_mode(mode) == 'routes' and ('s0', apb[0]) in DENIED):
        split = [(fast, 4, 1 + j) for j in range(3)] + [(apb[0], 64, 0)]
        eps.append({'name': 'split_read_interleaves',
                    'knobs': [f'{fast}_rlat = 40;', f'{apb[0]}_delay = 1;'],
                    'cmds': {('s0', 'rd'): program(0, False, split)},
                    'checks': [('s0_rilv > 0', 'R_INTERLEAVED')]})
    # 7. A hung timed target: its bursts complete with SLVERR in ID order.
    for t in timed:
        hung = {t: SLVERR}
        reads = program(0, False, [(t, 4, 0), (t, 2, 1), (t, 1, 2), (t, 3, 3), (first, 2, 0)], hung)
        eps.append({'name': 'timeout_read_queue', 'knobs': [f'{t}_hang = 1;'],
                    'cmds': {('s0', 'rd'): reads},
                    'checks': [(f'nl_t_{t}_timed_out === 1', 'TIMED_OUT')]})
        writes = program(0, True, [(t, 4, 0), (t, 2, 1), (t, 1, 2), (first, 2, 3)], hung)
        eps.append({'name': 'timeout_write_queue', 'knobs': [f'{t}_hang = 2;'],
                    'cmds': {('s0', 'wr'): writes},
                    'checks': [(f'nl_t_{t}_timed_out === 1', 'TIMED_OUT')]})
    # 8. Common reset with bursts of several IDs in flight at several
    # targets, then normal service. The reads hold the multi-burst
    # target's pool, so the first write takes another target: the request
    # stream is head-of-line per source, and a write behind a pool-full
    # target would never reach the second.
    non_multi = [t for t in names if m.targets[t][0] == 'axi4' and t != multi][0]
    eps.append({'name': 'reset_mid_flight', 'reset_after': 40,
                'knobs': [f'{multi}_rlat = 60;', f'{non_multi}_blat = 60;'],
                'cmds': {('s0', 'rd'): program(0, False, [(multi, 4, 1), (multi, 2, 2)] * 3),
                         ('s0', 'wr'): program(0, True, [(non_multi, 4, 2), (multi, 1, 3)] * 3),
                         ('s1', 'wr'): program(1, True, [(multi, 2, 2)] * 3)},
                'checks': [(f's0_rmax >= 2 && s0_wmax >= 2', 'IN_FLIGHT_BEFORE_RESET')]})
    eps.append({'name': 'after_reset',
                'cmds': {('s0', 'rd'): program(0, False, [(multi, 2, 1), (first, 2, 0), (fast, 1, 2)]),
                         ('s0', 'wr'): program(0, True, [(first, 2, 2), (multi, 1, 1), (fast, 1, 3)]),
                         ('s1', 'rd'): program(1, False, [(multi, 2, 0)])}})
    wl = [(multi, 2, 1, {'wlx': 1}), (multi, 1, 1), (multi, 1, 1, {'wlx': 1}), (multi, 3, 1),
          (first, 1, 0, {'wlx': 1}), (first, 2, 0), (None, 1, 0, {'wlx': 1}), (multi, 1, 1, {'wlx': 1})]
    eps.append({'name': 'first_beat_wlast_error', 'cmds': {('s0', 'wr'): program(0, True, wl * 2)}})
    if clocked(mode):
        # Target latencies count far cycles: scale them so that a target
        # still answers later than the source issues (in main cycles). A
        # timed target keeps its latencies below its timeout.
        scale = max(1, -(-10 // CLOCKED[mode][0]))
        pattern = r'\b(' + '|'.join(t for t in names if not m.targets[t][4]) + r')(_[rb]lat = )(\d+)'
        for ep in eps:
            ep['knobs'] = [re.sub(pattern, lambda x: x.group(1) + x.group(2) +
                                  str(int(x.group(3)) * scale), k) for k in ep.get('knobs', [])]
    if crossing(mode):
        # The pipelined-target episodes count the bursts that reach the
        # target before it answers the first: with the crossing in between
        # (and random port backpressure in the W-ahead variant) the target
        # waits three times as long.
        for ep in eps:
            if ep['name'] in ('target_pipelined_reads', 'target_pipelined_writes'):
                ep['knobs'] = [re.sub(r'(_[rb]lat = )(\d+)', lambda x: x.group(1) +
                                      str(int(x.group(2)) * 3), k) for k in ep['knobs']]
        # Bursts in flight across the crossings: the reset lands once some
        # request accepted at a source face has not reached its target and
        # some response has left its target without completing at its
        # source, so tagged words sit in the crossings (checked at the reset).
        eps.append({'name': 'reset_in_crossings', 'reset_when_crossing': True,
                    'knobs': [f'{multi}_rlat = 6;', f'{fast}_blat = 6;'],
                    'cmds': {('s0', 'rd'): program(0, False, [(multi, 2, 1), (fast, 1, 2), (first, 1, 3)] * 3),
                             ('s0', 'wr'): program(0, True, [(multi, 1, 1), (first, 2, 2)] * 3),
                             ('s1', 'rd'): program(1, False, [(multi, 1, 0)] * 4),
                             ('s1', 'wr'): program(1, True, [(fast, 1, 0)] * 4)}})
        eps.append({'name': 'after_crossing_reset',
                    'cmds': {('s0', 'rd'): program(0, False, [(multi, 2, 1), (fast, 1, 2)] * 2),
                             ('s0', 'wr'): program(0, True, [(first, 1, 2), (multi, 1, 1)] * 2),
                             ('s1', 'wr'): program(1, True, [(multi, 2, 3)] * 2)}})
    return eps


def measured(mode):
    """Throughput episodes: one ID to the multi-burst target, no checks."""
    eps = episodes(mode)
    return [dict(ep, checks=[]) for ep in eps if ep['name'] in ('same_target_reads',
                                                                 'same_target_writes')]


# AXI4 slave model. Reads and writes queue up to sixteen bursts. Reads are
# served one burst at a time after @T@_rlat cycles; writes collect W in AW
# order and answer B after @T@_blat cycles. With @T@_reorder the model serves
# the youngest burst whose ID has no older burst queued (legal AXI reorder).
# @T@_hang 1 holds RVALID low, 2 holds BVALID low. @T@_err names the burst
# (source * 128 + serial, local address bits 13:6) that answers SLVERR.
_AXI = '''
integer @T@_rlat = 0, @T@_blat = 0, @T@_reorder = 0, @T@_hang = 0, @T@_err = -1;
reg [15:0] @T@_bpl = 16'h1d0f + @IDX@;
always @(negedge @CLK@) @T@_bpl <= {@T@_bpl[14:0], @T@_bpl[15] ^ @T@_bpl[13] ^ @T@_bpl[12] ^ @T@_bpl[10]};
wire @T@_go_a = !w_ahead || @T@_bpl[1:0] != 0, @T@_go_w = !w_ahead || @T@_bpl[3:2] != 0,
     @T@_go_r = !w_ahead || @T@_bpl[5:4] != 0;
integer @T@_rn, @T@_rcur, @T@_rk, @T@_rwait, @T@_rmax, @T@_an, @T@_wq, @T@_wk, @T@_bcur, @T@_bwait,
        @T@_wmax, @T@_k, @T@_j, @T@_pick, @T@_ok;
reg [31:0] @T@_ra [0:16]; reg [7:0] @T@_rl [0:16]; reg [@IW@-1:0] @T@_ri [0:16];
reg [31:0] @T@_wa [0:16]; reg [7:0] @T@_wl [0:16]; reg [@IW@-1:0] @T@_wi [0:16]; reg @T@_wd [0:16];
assign nl_t_@T@_arready = @T@_rn < 16 && @T@_go_r;
assign nl_t_@T@_rvalid = @T@_rcur >= 0 && @T@_rwait == 0 && @T@_hang != 1;
assign nl_t_@T@_rid = @T@_rcur >= 0 ? @T@_ri[@T@_rcur] : 0;
assign nl_t_@T@_rlast = @T@_rcur >= 0 && @T@_rk == @T@_rl[@T@_rcur];
wire [31:0] @T@_rbeat = (@T@_rcur >= 0 ? @T@_ra[@T@_rcur] : 0) + @T@_rk * 4;
assign nl_t_@T@_rdata = {8'ha0 | 8'd@IDX@, 8'h00, @T@_rbeat[15:0]};
assign nl_t_@T@_rresp = @T@_rcur >= 0 && @T@_ra[@T@_rcur][13:6] == @T@_err ? 2'b10 : 2'b00;
assign nl_t_@T@_awready = @T@_an < 16 && @T@_go_a;
assign nl_t_@T@_wready = @T@_wq < @T@_an && @T@_go_w;
assign nl_t_@T@_bvalid = @T@_bcur >= 0 && @T@_bwait == 0 && @T@_hang != 2;
assign nl_t_@T@_bid = @T@_bcur >= 0 ? @T@_wi[@T@_bcur] : 0;
assign nl_t_@T@_bresp = @T@_bcur >= 0 && @T@_wa[@T@_bcur][13:6] == @T@_err ? 2'b10 : 2'b00;
wire @T@_arhs = nl_t_@T@_arvalid && nl_t_@T@_arready, @T@_rhs = nl_t_@T@_rvalid && nl_t_@T@_rready;
wire @T@_awhs = nl_t_@T@_awvalid && nl_t_@T@_awready, @T@_whs = nl_t_@T@_wvalid && nl_t_@T@_wready;
wire @T@_bhs = nl_t_@T@_bvalid && nl_t_@T@_bready;
always @(posedge @CLK@) begin
 if (!reset_n) begin
  @T@_rn = 0; @T@_rcur = -1; @T@_rk = 0; @T@_rwait = 0; @T@_an = 0; @T@_wq = 0; @T@_wk = 0;
  @T@_bcur = -1; @T@_bwait = 0;
 end else begin
  // Requests accepted and responses completed at target ports (the
  // reset_in_crossings episode compares them with the source faces).
  if (@T@_arhs) tgt_acc = tgt_acc + 1;
  if (@T@_awhs) tgt_acc = tgt_acc + 1;
  if (@T@_rhs && nl_t_@T@_rlast) tgt_done = tgt_done + 1;
  if (@T@_bhs) tgt_done = tgt_done + 1;
  // The outlet never has more than its capacity in flight (counts before
  // this edge's completions), reads and writes combined (one shared pool).
  if (@T@_arhs) check(@T@_rn < @CAP@, "@U@_READ_CAPACITY");
  if (@T@_awhs) check(@T@_an < @CAP@, "@U@_WRITE_CAPACITY");
  check(@T@_rn + @T@_an <= @CAP@, "@U@_POOL_CAPACITY");
  // Response side first: it sees the queues as they were before this edge.
  if (@T@_rhs) begin
   if (@T@_rk == @T@_rl[@T@_rcur]) begin
    rd_retired[@T@_ra[@T@_rcur][13] * MAXC + @T@_ra[@T@_rcur][12:6]] = 1;
    for (@T@_k = @T@_rcur; @T@_k < 16; @T@_k = @T@_k + 1) begin
     @T@_ra[@T@_k] = @T@_ra[@T@_k + 1]; @T@_rl[@T@_k] = @T@_rl[@T@_k + 1]; @T@_ri[@T@_k] = @T@_ri[@T@_k + 1];
    end
    @T@_rn = @T@_rn - 1; @T@_rcur = -1; @T@_rk = 0;
   end else @T@_rk = @T@_rk + 1;
  end else if (@T@_rcur >= 0 && @T@_rwait > 0) @T@_rwait = @T@_rwait - 1;
  if (@T@_bhs) begin
   wr_retired[@T@_wa[@T@_bcur][13] * MAXC + @T@_wa[@T@_bcur][12:6]] = 1;
   for (@T@_k = @T@_bcur; @T@_k < 16; @T@_k = @T@_k + 1) begin
    @T@_wa[@T@_k] = @T@_wa[@T@_k + 1]; @T@_wl[@T@_k] = @T@_wl[@T@_k + 1]; @T@_wi[@T@_k] = @T@_wi[@T@_k + 1];
    @T@_wd[@T@_k] = @T@_wd[@T@_k + 1];
   end
   @T@_an = @T@_an - 1; @T@_wq = @T@_wq - 1; @T@_bcur = -1;
  end else if (@T@_bcur >= 0 && @T@_bwait > 0) @T@_bwait = @T@_bwait - 1;
  if (@T@_whs) begin
   check(nl_t_@T@_wdata === ((@BASE@ + @T@_wa[@T@_wq]) ^ (@T@_wk * 32'h01010101)) &&
         nl_t_@T@_wstrb === 4'hf && nl_t_@T@_wlast === (@T@_wk == @T@_wl[@T@_wq]), "@U@_W_BEAT");
   if (@T@_wk == @T@_wl[@T@_wq]) begin @T@_wd[@T@_wq] = 1; @T@_wq = @T@_wq + 1; @T@_wk = 0; end
   else @T@_wk = @T@_wk + 1;
  end
  if (@T@_arhs) begin
@SRC_R@
   destination_rule(0, @IDX@, nl_t_@T@_araddr);
   @T@_ra[@T@_rn] = nl_t_@T@_araddr; @T@_rl[@T@_rn] = nl_t_@T@_arlen; @T@_ri[@T@_rn] = nl_t_@T@_arid;
   @T@_rn = @T@_rn + 1;
  end
  if (@T@_awhs) begin
@SRC_W@
   destination_rule(1, @IDX@, nl_t_@T@_awaddr);
   @T@_wa[@T@_an] = nl_t_@T@_awaddr; @T@_wl[@T@_an] = nl_t_@T@_awlen; @T@_wi[@T@_an] = nl_t_@T@_awid;
   @T@_wd[@T@_an] = 0; @T@_an = @T@_an + 1;
  end
  // Choose the next read and B: the oldest, or with reorder the youngest
  // whose ID has no older queued burst.
  if (@T@_rcur < 0 && @T@_rn > 0) begin
   @T@_pick = 0;
   if (@T@_reorder)
    for (@T@_k = 0; @T@_k < @T@_rn; @T@_k = @T@_k + 1) begin
     @T@_ok = 1;
     for (@T@_j = 0; @T@_j < @T@_k; @T@_j = @T@_j + 1) if (@T@_ri[@T@_j] == @T@_ri[@T@_k]) @T@_ok = 0;
     if (@T@_ok) @T@_pick = @T@_k;
    end
   @T@_rcur = @T@_pick; @T@_rk = 0; @T@_rwait = @T@_rlat;
  end
  if (@T@_bcur < 0) begin
   @T@_pick = -1;
   for (@T@_k = 0; @T@_k < @T@_wq; @T@_k = @T@_k + 1) begin
    @T@_ok = 1;
    for (@T@_j = 0; @T@_j < @T@_k; @T@_j = @T@_j + 1) if (@T@_wi[@T@_j] == @T@_wi[@T@_k]) @T@_ok = 0;
    if (@T@_ok && (@T@_pick < 0 || @T@_reorder)) @T@_pick = @T@_k;
   end
   if (@T@_pick >= 0) begin @T@_bcur = @T@_pick; @T@_bwait = @T@_blat; end
  end
  if (@T@_rn > @T@_rmax) @T@_rmax = @T@_rn;
  if (@T@_an > @T@_wmax) @T@_wmax = @T@_an;
 end
end
'''

# APB4 slave: PREADY after @T@_delay ACCESS cycles; read data by address.
_APB = '''
integer @T@_delay = 0, @T@_acc;
assign nl_t_@T@_pready = nl_t_@T@_psel && nl_t_@T@_penable && @T@_acc >= @T@_delay;
assign nl_t_@T@_prdata = {8'ha0 | 8'd@IDX@, 8'h00, 4'h0, nl_t_@T@_paddr[11:0]};
assign nl_t_@T@_pslverr = 1'b0;
always @(posedge @CLK@) begin
 if (!reset_n) @T@_acc <= 0;
 else if (nl_t_@T@_psel && nl_t_@T@_penable && !nl_t_@T@_pready) @T@_acc <= @T@_acc + 1;
 else @T@_acc <= 0;
 if (reset_n && nl_t_@T@_psel && nl_t_@T@_penable && nl_t_@T@_pready && nl_t_@T@_pwrite)
  check(nl_t_@T@_pwdata === ((@BASE@ + {nl_t_@T@_paddr[31:6], 6'd0}) ^
                            ((nl_t_@T@_paddr[5:2]) * 32'h01010101)), "@U@_PWDATA");
end
'''

# AXI4-Lite slave: one AW, one W and one AR at a time; B after both, R one
# cycle after AR, plus @T@_delay cycles. Write data and read data follow the
# APB model's address rules.
_LITE = '''
integer @T@_delay = 0, @T@_wait;
reg @T@_aw, @T@_w, @T@_b, @T@_ar, @T@_r; reg [31:0] @T@_awa, @T@_wd, @T@_ara;
assign nl_t_@T@_awready = !@T@_aw;
assign nl_t_@T@_wready = !@T@_w;
assign nl_t_@T@_bvalid = @T@_b;
assign nl_t_@T@_bresp = 2'b00;
assign nl_t_@T@_arready = !@T@_ar && !@T@_r;
assign nl_t_@T@_rvalid = @T@_r;
assign nl_t_@T@_rdata = {8'ha0 | 8'd@IDX@, 8'h00, 4'h0, @T@_ara[11:0]};
assign nl_t_@T@_rresp = 2'b00;
always @(posedge @CLK@) begin
 if (!reset_n) begin
  @T@_aw <= 0; @T@_w <= 0; @T@_b <= 0; @T@_ar <= 0; @T@_r <= 0; @T@_wait <= 0;
 end else begin
  if (nl_t_@T@_awvalid && !@T@_aw) begin @T@_aw <= 1; @T@_awa <= nl_t_@T@_awaddr; end
  if (nl_t_@T@_wvalid && !@T@_w) begin
   check(nl_t_@T@_wstrb === 4'hf, "@U@_WSTRB"); @T@_w <= 1; @T@_wd <= nl_t_@T@_wdata;
  end
  if (@T@_aw && @T@_w && !@T@_b) begin
   check(@T@_wd === ((@BASE@ + {@T@_awa[31:6], 6'd0}) ^ (@T@_awa[5:2] * 32'h01010101)), "@U@_WDATA");
   @T@_b <= 1; @T@_aw <= 0; @T@_w <= 0;
  end
  if (@T@_b && nl_t_@T@_bready) @T@_b <= 0;
  if (nl_t_@T@_arvalid && nl_t_@T@_arready) begin @T@_ar <= 1; @T@_ara <= nl_t_@T@_araddr; @T@_wait <= 0; end
  else if (@T@_ar && !@T@_r) begin
   if (@T@_wait >= @T@_delay) begin @T@_r <= 1; @T@_ar <= 0; end else @T@_wait <= @T@_wait + 1;
  end
  if (@T@_r && nl_t_@T@_rready) @T@_r <= 0;
 end
end
'''

# Per source: issue processes, the per-ID scoreboard and the bookkeeping the
# destination rule and the ID table bound read. Tables: source @I@ owns
# entries @I@*MAXC+k.
_SOURCE = '''
integer @S@_ri, @S@_rd, @S@_rb, @S@_rcur, @S@_wi, @S@_wd, @S@_rmax, @S@_wmax, @S@_rgap = 0,
        @S@_bgap = 0, @S@_rhold = 0, @S@_rooo, @S@_wooo, @S@_rilv, @S@_rdests, @S@_wdests, @S@_pick, @S@_old, @S@_cur,
        @S@_k, @S@_awbeats = 0, @S@_wbeats = 0, @S@_awgap = 3, @S@_wlead = 0, @S@_opick;
reg [15:0] @S@_lfsr;
// AXI A3.2.1: an offered R or B beat stays unchanged until it is accepted.
reg @S@_rheld, @S@_bheld, @S@_rlast_q; reg [63:0] @S@_rid_q, @S@_rdata_q, @S@_bid_q;
reg [1:0] @S@_rresp_q, @S@_bresp_q;
always @(negedge @SCLK@) begin
 @S@_lfsr <= {@S@_lfsr[14:0], @S@_lfsr[15] ^ @S@_lfsr[13] ^ @S@_lfsr[12] ^ @S@_lfsr[10]};
 // @S@_rhold holds RREADY low for that many cycles (an offered beat waits).
 nl_i_@S@_rready <= @S@_rhold == 0 && (@S@_rgap == 0 || @S@_lfsr[2:0] >= @S@_rgap);
 if (@S@_rhold > 0) @S@_rhold = @S@_rhold - 1;
 nl_i_@S@_bready <= @S@_bgap == 0 || @S@_lfsr[5:3] >= @S@_bgap;
end
always @(posedge @SCLK@) begin
 if (!reset_n) begin
  @S@_ri <= 0; @S@_rd <= 0; @S@_rcur <= -1; @S@_wi <= 0; @S@_wd <= 0;
  @S@_rheld <= 0; @S@_bheld <= 0; @S@_awbeats = 0; @S@_wbeats = 0;
 end else begin
  if (@S@_rheld)
   check(nl_i_@S@_rvalid === 1'b1 && @S@_rid_q === nl_i_@S@_rid &&
         @S@_rdata_q === nl_i_@S@_rdata && nl_i_@S@_rresp === @S@_rresp_q &&
         nl_i_@S@_rlast === @S@_rlast_q, "@U@_R_STABLE");
  if (@S@_bheld)
   check(nl_i_@S@_bvalid === 1'b1 && @S@_bid_q === nl_i_@S@_bid &&
         nl_i_@S@_bresp === @S@_bresp_q, "@U@_B_STABLE");
  @S@_rheld <= nl_i_@S@_rvalid && !nl_i_@S@_rready;
  @S@_rid_q <= nl_i_@S@_rid; @S@_rdata_q <= nl_i_@S@_rdata; @S@_rresp_q <= nl_i_@S@_rresp;
  @S@_rlast_q <= nl_i_@S@_rlast;
  @S@_bheld <= nl_i_@S@_bvalid && !nl_i_@S@_bready;
  @S@_bid_q <= nl_i_@S@_bid; @S@_bresp_q <= nl_i_@S@_bresp;
  // A burst's ID binding frees as its last response enters the source, which then offers it
  // here: the oldest waiting burst of the offered ID, accepted or not.
  if (nl_i_@S@_bvalid) begin
   @S@_opick = -1;
   for (@S@_k = 0; @S@_k < @S@_wi; @S@_k = @S@_k + 1)
    if (!wr_complete[@I@*MAXC + @S@_k] && @S@_opick < 0 && wr_id[@I@*MAXC + @S@_k] === nl_i_@S@_bid)
     @S@_opick = @S@_k;
   if (@S@_opick >= 0) wr_offered[@I@*MAXC + @S@_opick] <= 1;
  end
  if (nl_i_@S@_rvalid && nl_i_@S@_rlast) begin
   @S@_opick = -1;
   for (@S@_k = 0; @S@_k < @S@_ri; @S@_k = @S@_k + 1)
    if (!rd_complete[@I@*MAXC + @S@_k] && @S@_opick < 0 && rd_id[@I@*MAXC + @S@_k] === nl_i_@S@_rid)
     @S@_opick = @S@_k;
   if (@S@_opick >= 0) rd_offered[@I@*MAXC + @S@_opick] <= 1;
  end
  if (nl_i_@S@_arvalid && nl_i_@S@_arready) @S@_ri <= @S@_ri + 1;
  if (nl_i_@S@_awvalid && nl_i_@S@_awready) @S@_wi <= @S@_wi + 1;
  if (nl_i_@S@_awvalid && nl_i_@S@_awready) @S@_awbeats = @S@_awbeats + nl_i_@S@_awlen + 1;
  if (nl_i_@S@_wvalid && nl_i_@S@_wready) begin
   check(@S@_wbeats < @S@_awbeats + @NW@, "@U@_W_AHEAD_OF_AW");
   @S@_wbeats = @S@_wbeats + 1;
  end
  // W offered ahead of its AW, taken into the W port pipe or held.
  if (nl_i_@S@_wvalid && @S@_wbeats >= @S@_awbeats) @S@_wlead = @S@_wlead + 1;
  if (nl_i_@S@_rvalid && nl_i_@S@_rready) begin
   // Every beat belongs to the oldest waiting burst of its RID and continues
   // its own beat count; a beat of another burst than the previous,
   // unfinished one interleaves.
   @S@_pick = -1; @S@_old = -1;
   for (@S@_k = 0; @S@_k < @S@_ri; @S@_k = @S@_k + 1)
    if (!rd_complete[@I@*MAXC + @S@_k]) begin
     if (@S@_old < 0) @S@_old = @S@_k;
     if (@S@_pick < 0 && rd_id[@I@*MAXC + @S@_k] === nl_i_@S@_rid) @S@_pick = @S@_k;
    end
   check(@S@_pick >= 0, "@U@_R_WITHOUT_BURST");
   @S@_cur = @S@_pick; @S@_rb = rd_beat[@I@*MAXC + @S@_cur];
   if (@S@_rb == 0 && @S@_pick != @S@_old) @S@_rooo = @S@_rooo + 1;
   if (@S@_rcur >= 0 && @S@_rcur != @S@_cur) @S@_rilv = @S@_rilv + 1;
   check(nl_i_@S@_rid === rd_id[@I@*MAXC + @S@_cur], "@U@_RID_ORDER");
   check(nl_i_@S@_rresp === rd_resp[@I@*MAXC + @S@_cur], "@U@_RRESP");
   check(nl_i_@S@_rdata === expected_data(rd_tag[@I@*MAXC + @S@_cur], rd_addr[@I@*MAXC + @S@_cur], @S@_rb),
         "@U@_RDATA");
   check(nl_i_@S@_rlast === (@S@_rb == rd_len[@I@*MAXC + @S@_cur]), "@U@_RLAST");
   if (@S@_rb == rd_len[@I@*MAXC + @S@_cur]) begin
    rd_complete[@I@*MAXC + @S@_cur] <= 1; @S@_rd <= @S@_rd + 1; @S@_rcur <= -1;
   end else begin
    rd_beat[@I@*MAXC + @S@_cur] <= @S@_rb + 1; @S@_rcur <= @S@_cur;
   end
  end
  if (nl_i_@S@_bvalid && nl_i_@S@_bready) begin
   // A B belongs to the oldest waiting write of its BID.
   @S@_pick = -1; @S@_old = -1;
   for (@S@_k = 0; @S@_k < @S@_wi; @S@_k = @S@_k + 1)
    if (!wr_complete[@I@*MAXC + @S@_k]) begin
     if (@S@_old < 0) @S@_old = @S@_k;
     if (@S@_pick < 0 && wr_id[@I@*MAXC + @S@_k] === nl_i_@S@_bid) @S@_pick = @S@_k;
    end
   check(@S@_pick >= 0, "@U@_B_WITHOUT_BURST");
   if (@S@_pick != @S@_old) @S@_wooo = @S@_wooo + 1;
   if (@S@_pick >= 0) begin
    check(nl_i_@S@_bresp === wr_resp[@I@*MAXC + @S@_pick], "@U@_BRESP");
    wr_complete[@I@*MAXC + @S@_pick] <= 1;
   end
   @S@_wd <= @S@_wd + 1;
  end
  if (@S@_ri - @S@_rd > @S@_rmax) @S@_rmax <= @S@_ri - @S@_rd;
  if (@S@_wi - @S@_wd > @S@_wmax) @S@_wmax <= @S@_wi - @S@_wd;
  // The face never holds more than the source's capacity, the bursts its AW and AR pipes hold and
  // the one whose last response waits at the face (its context already free).
  check(@S@_ri - @S@_rd <= @N@ + @NAR@ + 1 && @S@_wi - @S@_wd <= @N@ + @NAW@ + 1, "@U@_CAPACITY");
 end
end
task automatic issue_reads_@S@;
 integer i;
 begin
  for (i = 0; i < rd_n[@I@] && !abort; i = i + 1) begin
   @(negedge @SCLK@);
   nl_i_@S@_arvalid = 1; nl_i_@S@_araddr = rd_addr[@I@*MAXC + i]; nl_i_@S@_arlen = rd_len[@I@*MAXC + i];
   nl_i_@S@_arsize = 2; nl_i_@S@_arburst = rd_burst[@I@*MAXC + i]; nl_i_@S@_arlock = rd_lock[@I@*MAXC + i];
   nl_i_@S@_arid = rd_id[@I@*MAXC + i]; nl_i_@S@_arprot = 0; nl_i_@S@_arcache = 0;
   @(posedge @SCLK@); while (!nl_i_@S@_arready && !abort) @(posedge @SCLK@);
  end
  @(negedge @SCLK@); nl_i_@S@_arvalid = 0;
 end
endtask
task automatic issue_writes_@S@;
 integer i, k, j, n;
 begin
  if (w_ahead) begin
   fork
    begin : aw_lead_@S@
     for (i = 0; i < wr_n[@I@] && !abort; i = i + 1) begin
      @(negedge @SCLK@); nl_i_@S@_awvalid = 0;
      while (@S@_awgap != 0 && @S@_lfsr[10:8] < @S@_awgap && !abort) @(negedge @SCLK@);
      nl_i_@S@_awvalid = 1; nl_i_@S@_awaddr = wr_addr[@I@*MAXC + i]; nl_i_@S@_awlen = wr_len[@I@*MAXC + i];
      nl_i_@S@_awsize = 2; nl_i_@S@_awburst = wr_burst[@I@*MAXC + i]; nl_i_@S@_awlock = wr_lock[@I@*MAXC + i];
      nl_i_@S@_awid = wr_id[@I@*MAXC + i]; nl_i_@S@_awprot = 0; nl_i_@S@_awcache = 0;
      @(posedge @SCLK@); while (!nl_i_@S@_awready && !abort) @(posedge @SCLK@);
     end
     @(negedge @SCLK@); nl_i_@S@_awvalid = 0;
    end
    begin : w_lead_@S@
     for (j = 0; j < wr_n[@I@] && !abort; j = j + 1)
      for (n = 0; n <= wr_len[@I@*MAXC + j] && !abort; n = n + 1) begin
       @(negedge @SCLK@);
       nl_i_@S@_wvalid = 1; nl_i_@S@_wdata = wr_addr[@I@*MAXC + j] ^ (n * 32'h01010101);
       nl_i_@S@_wstrb = 4'hf; nl_i_@S@_wlast = (n == wr_len[@I@*MAXC + j]) ^ (n == 0 && wr_wlx[@I@*MAXC + j]);
       @(posedge @SCLK@); while (!nl_i_@S@_wready && !abort) @(posedge @SCLK@);
      end
     @(negedge @SCLK@); nl_i_@S@_wvalid = 0;
    end
   join
  end else begin
  for (i = 0; i < wr_n[@I@] && !abort; i = i + 1) begin
   @(negedge @SCLK@);
   // W of the previous burst was taken: WVALID drops until this AW is taken
   // (no stale beat held valid across bursts, AXI A3.2.1).
   nl_i_@S@_wvalid = 0;
   nl_i_@S@_awvalid = 1; nl_i_@S@_awaddr = wr_addr[@I@*MAXC + i]; nl_i_@S@_awlen = wr_len[@I@*MAXC + i];
   nl_i_@S@_awsize = 2; nl_i_@S@_awburst = wr_burst[@I@*MAXC + i]; nl_i_@S@_awlock = wr_lock[@I@*MAXC + i];
   nl_i_@S@_awid = wr_id[@I@*MAXC + i]; nl_i_@S@_awprot = 0; nl_i_@S@_awcache = 0;
   @(posedge @SCLK@); while (!nl_i_@S@_awready && !abort) @(posedge @SCLK@);
   for (k = 0; k <= wr_len[@I@*MAXC + i] && !abort; k = k + 1) begin
    @(negedge @SCLK@);
    nl_i_@S@_awvalid = 0;
    nl_i_@S@_wvalid = 1; nl_i_@S@_wdata = wr_addr[@I@*MAXC + i] ^ (k * 32'h01010101);
    nl_i_@S@_wstrb = 4'hf; nl_i_@S@_wlast = (k == wr_len[@I@*MAXC + i]) ^ (k == 0 && wr_wlx[@I@*MAXC + i]);
    @(posedge @SCLK@); while (!nl_i_@S@_wready && !abort) @(posedge @SCLK@);
   end
  end
  @(negedge @SCLK@); nl_i_@S@_awvalid = 0; nl_i_@S@_wvalid = 0;
  end
 end
endtask
'''

# Per-ID destination rule and ID table bound, called when an AXI4 target port
# accepts an AR (write 0) or AW (write 1). Local address bit 13 is the
# source and bits 12:6 the burst serial. The ID table holds {direction, ID}
# keys of both directions; a burst leaves it once its last response reaches
# the source, so the bound counts only bursts whose last target response has
# not completed yet (a subset of the table's bursts).
_RULES = '''
task automatic destination_rule;
 input integer write, target; input [31:0] local_addr;
 integer s, slot, j, q, ids, dests, fresh, d, e;
 begin
  s = local_addr[13]; slot = local_addr[12:6];
  if (write) wr_net[s*MAXC + slot] = 1; else rd_net[s*MAXC + slot] = 1;
  // Earlier bursts of the same ID bound elsewhere are answered: their last response reached the
  // source (it is offered or accepted there) or, sooner seen, left its target.
  for (j = 0; j < slot; j = j + 1)
   if (!rules) ;
   else if (write) check(wr_id[s*MAXC + j] != wr_id[s*MAXC + slot] || wr_dest[s*MAXC + j] == target ||
                         wr_complete[s*MAXC + j] || wr_offered[s*MAXC + j] || wr_retired[s*MAXC + j],
                         "DESTINATION_RULE_W");
   else check(rd_id[s*MAXC + j] != rd_id[s*MAXC + slot] || rd_dest[s*MAXC + j] == target ||
              rd_complete[s*MAXC + j] || rd_offered[s*MAXC + j] || rd_retired[s*MAXC + j],
              "DESTINATION_RULE_R");
  // Distinct {direction, ID} keys among both directions' network bursts whose
  // last target response is still due.
  ids = 0;
  for (d = 0; d < 2; d = d + 1)
   for (j = 0; j < MAXC; j = j + 1)
    if (d ? (wr_net[s*MAXC + j] && !wr_complete[s*MAXC + j] && !wr_retired[s*MAXC + j])
          : (rd_net[s*MAXC + j] && !rd_complete[s*MAXC + j] && !rd_retired[s*MAXC + j])) begin
     fresh = 1;
     for (q = 0; q < j; q = q + 1)
      if (d ? (wr_net[s*MAXC + q] && !wr_complete[s*MAXC + q] && !wr_retired[s*MAXC + q] &&
               wr_id[s*MAXC + q] == wr_id[s*MAXC + j])
            : (rd_net[s*MAXC + q] && !rd_complete[s*MAXC + q] && !rd_retired[s*MAXC + q] &&
               rd_id[s*MAXC + q] == rd_id[s*MAXC + j]))
       fresh = 0;
     ids = ids + fresh;
    end
  check(!rules || ids <= id_table[s], "ID_TABLE_BOUND");
  // Distinct targets among this direction's network bursts in flight.
  dests = 0;
  for (j = 0; j < MAXC; j = j + 1)
   if (write ? (wr_net[s*MAXC + j] && !wr_complete[s*MAXC + j])
             : (rd_net[s*MAXC + j] && !rd_complete[s*MAXC + j])) begin
    fresh = 1;
    for (q = 0; q < j; q = q + 1)
     if (write ? (wr_net[s*MAXC + q] && !wr_complete[s*MAXC + q] && wr_dest[s*MAXC + q] == wr_dest[s*MAXC + j])
               : (rd_net[s*MAXC + q] && !rd_complete[s*MAXC + q] && rd_dest[s*MAXC + q] == rd_dest[s*MAXC + j]))
      fresh = 0;
    dests = dests + fresh;
   end
  if (s == 0) begin
   if (write && dests > s0_wdests) s0_wdests = dests;
   if (!write && dests > s0_rdests) s0_rdests = dests;
  end else begin
   if (write && dests > s1_wdests) s1_wdests = dests;
   if (!write && dests > s1_rdests) s1_rdests = dests;
  end
 end
endtask
'''


def write_bench(work, mapping, mode, episodes, rules=True, single=False):
    """episodes: list of dicts produced by scenario(); see run_multi_outstanding.
    single: the network gives each source and target a shared capacity of one burst.
    NOCLOOM_MO_W_AHEAD=1 selects the write master whose W stream runs ahead of
    AW and random AWREADY/WREADY/ARREADY backpressure at every AXI4 target."""
    import os
    w_ahead = 1 if os.environ.get('NOCLOOM_MO_W_AHEAD') == '1' else 0
    model = Model(mode)
    tclk = target_clock(mode)

    def face_cap(source):
        return 1 if single else capacity(mode, source)
    decl, conns, init = [], [], []
    for port in mapping['ports']:
        name, width = port['name'], port['width']
        kind = 'reg' if port['direction'] == 'input' and not name.startswith('nl_t_') else 'wire'
        decl.append(f'{kind} [{width - 1}:0] {name};')
        conns.append(f'.{name}({name})')
        if kind == 'reg' and not name.startswith('clk_') and name != 'reset_n':
            init.append(f'{name} = 0;')
    widths = {p['name']: p['width'] for p in mapping['ports']}
    sources = list(SOURCES)
    idb = id_bits(mode)
    for s in sources:
        if f'nl_i_{s}_arid' not in widths:
            # No ID ports: the issue tasks drive dummies, the faces answer ID 0.
            decl.append(f'reg [0:0] nl_i_{s}_arid, nl_i_{s}_awid;')
            decl.append(f"wire [0:0] nl_i_{s}_rid = 1'b0, nl_i_{s}_bid = 1'b0;")
    models = []
    for name in model.names:
        kind, base, _size, cap, _cycles = model.targets[name]
        values = {'T': name, 'U': name.upper(), 'IDX': model.index[name], 'BASE': base,
                  'CAP': 1 if single else cap, 'CLK': tclk}
        if kind == 'apb4':
            models.append(_fill(_APB, **values))
        elif kind == 'axi4_lite':
            models.append(_fill(_LITE, **values))
        else:
            iw = widths.get(f'nl_t_{name}_arid', 0)
            if not iw:
                # No ID ports: the model works on a constant one-bit ID.
                decl.append(f"wire [0:0] nl_t_{name}_arid = 1'b0, nl_t_{name}_awid = 1'b0;")
                decl.append(f'wire [0:0] nl_t_{name}_rid, nl_t_{name}_bid;')
            # The model echoes each target ID without interpreting its numeric encoding.
            index = ''
            models.append(_fill(_AXI, IW=max(1, iw), IDB=idb,
                                SRC_R=index.replace('{field}', 'id'),
                                SRC_W=index.replace('{field}', 'id').replace('_arid', '_awid')
                                .replace('_araddr', '_awaddr').replace('_AR_', '_AW_'), **values))
    source_text = ''.join(_fill(_SOURCE, S=s, U=s.upper(), I=i, N=face_cap(s),
                                SCLK=source_clock(mode, s),
                                **{f'N{c.upper()}': port_slots(mapping, f'source/{s}', c)
                                   for c in ('ar', 'aw', 'w')})
                          for i, s in enumerate(sources))
    knobs = []
    for name in model.names:
        if model.targets[name][0] in DEDICATED:
            knobs.append(f'{name}_delay = 0;')
        else:
            knobs.append(f'{name}_rlat = 0; {name}_blat = 0; {name}_reorder = 0; {name}_hang = 0; '
                         f'{name}_err = -1;')
    for s in sources:
        knobs.append(f'{s}_rgap = 0; {s}_bgap = 0; {s}_rhold = 0;')
    reset_knobs = ' '.join(knobs)
    maxima = ' '.join(f'{t}_rmax = 0; {t}_wmax = 0;' for t in model.names
                      if model.targets[t][0] not in DEDICATED) + ' ' + \
        ' '.join(f'{s}_rmax = 0; {s}_wmax = 0; {s}_rooo = 0; {s}_wooo = 0; {s}_rdests = 0; '
                 f'{s}_wdests = 0; {s}_rilv = 0;' for s in sources)
    issue = '\n'.join(f'   issue_reads_{s};\n   issue_writes_{s};' for s in sources)
    done = ' && '.join(f'{s}_rd == rd_n[{i}] && {s}_wd == wr_n[{i}]' for i, s in enumerate(sources))
    tables = ' '.join(f'id_table[{i}] = {tracked_ids(mode, s) if face_cap(s) > 1 else 1};'
                      for i, s in enumerate(sources))
    far_clock, far_reset = '', ''
    if clocked(mode):
        period, phase = CLOCKED[mode][:2]
        far_clock = (f'initial begin clk_far = 0; #{phase}; '
                     f'forever #{period / 2} clk_far = ~clk_far; end')
        # The far domain sees the reset on its own edges.
        far_reset = 'repeat (4) @(posedge clk_far);'
    else:
        tclk = 'clk_main'
    if mode in ENTRY:
        period, phase = ENTRY[mode]
        far_clock = (f'initial begin clk_entry = 0; #{phase}; '
                     f'forever #{period / 2} clk_entry = ~clk_entry; end')
    assert tclk == target_clock(mode)
    # The APB4 models answer the 12-bit local address (expected_data).
    apb = ' || '.join(f'tag == {model.index[t]}' for t in model.names
                      if model.targets[t][0] in DEDICATED) or '0'
    body = []
    for ep in episodes:
        if w_ahead and ep.get('checks'):
            # With W ahead of AW and random target readies the target
            # port lags the outlet's in-flight pool; the fill is proven in
            # the plain mode.
            ep = dict(ep, checks=[c for c in ep['checks'] if c[1] != 'TARGET_POOL_FULL'])
        body.append(_episode(ep, sources))
    n2 = 2 * MAXC - 1
    text = f'''`timescale 1ns/1ps
module tb;
{chr(10).join(decl)}
{mapping['module']} dut({', '.join(conns)});
initial clk_main = 0;
always #5 clk_main = ~clk_main;
{far_clock}
localparam MAXC = {MAXC};
integer cycle = 0, abort = 0, t0 = 0, tgt_acc = 0, tgt_done = 0;
integer w_ahead = {w_ahead};
// Rule monitors (destination rule per ID, ID table bound, one ID per read
// set and per write source at multi-burst targets).
integer rules = {1 if rules else 0};
reg [31:0] rd_addr [0:{n2}]; reg [7:0] rd_len [0:{n2}];
reg [1:0] rd_burst [0:{n2}]; reg rd_lock [0:{n2}];
reg [{max(1, idb) - 1}:0] rd_id [0:{n2}]; reg [1:0] rd_resp [0:{n2}];
integer rd_tag [0:{n2}]; integer rd_dest [0:{n2}]; reg rd_complete [0:{n2}]; reg rd_net [0:{n2}];
reg rd_retired [0:{n2}]; reg wr_retired [0:{n2}]; reg rd_offered [0:{n2}]; reg wr_offered [0:{n2}];
integer rd_beat [0:{n2}];
reg [31:0] wr_addr [0:{n2}]; reg [7:0] wr_len [0:{n2}];
reg [1:0] wr_burst [0:{n2}]; reg wr_lock [0:{n2}];
reg [{max(1, idb) - 1}:0] wr_id [0:{n2}]; reg [1:0] wr_resp [0:{n2}]; reg wr_wlx [0:{n2}];
integer wr_dest [0:{n2}]; reg wr_complete [0:{n2}]; reg wr_net [0:{n2}];
integer rd_n [0:1]; integer wr_n [0:1]; integer id_table [0:1];
always @(posedge clk_main) begin
 cycle <= cycle + 1;
 if (cycle > 900000) begin $display("ASSERT_FAIL:OUTSTANDING.BENCH_LIMIT"); $fatal(1); end
end
task automatic check;
 input condition; input [8*40-1:0] label_;
 if (condition !== 1'b1) begin
  $display("ASSERT_FAIL:OUTSTANDING.%0s cycle=%0d", label_, cycle); $fatal(1);
 end
endtask
function [31:0] expected_data;
 input integer tag; input [31:0] addr; input integer beat;
 reg [31:0] at;
 begin
  at = addr + beat * 4;
  expected_data = tag < 0 ? 32'h0 : {{8'ha0 | tag[7:0], 8'h00, at[15:0] & ({apb} ? 16'h0fff : 16'hffff)}};
 end
endfunction
{source_text}
{_RULES}
{''.join(models)}
task automatic clear_tables;
 integer k;
 begin
  for (k = 0; k < {2 * MAXC}; k = k + 1) begin
   rd_complete[k] = 0; wr_complete[k] = 0; rd_dest[k] = -2; wr_dest[k] = -2; rd_tag[k] = -1;
   rd_beat[k] = 0;
   rd_net[k] = 0; wr_net[k] = 0; wr_wlx[k] = 0; rd_retired[k] = 0; wr_retired[k] = 0;
   rd_offered[k] = 0; wr_offered[k] = 0;
  end
  rd_n[0] = 0; rd_n[1] = 0; wr_n[0] = 0; wr_n[1] = 0;
  {tables}
 end
endtask
task automatic episode;
 input [8*40-1:0] label_;
 begin
  @(negedge clk_main); reset_n = 0; abort = 0;
  {reset_knobs}
  clear_tables;
  repeat (5) @(posedge clk_main);
  {far_reset}
  @(negedge clk_main); reset_n = 1;
  {maxima} tgt_acc = 0; tgt_done = 0;
  repeat (3) @(posedge clk_main);
  t0 = cycle;
  $display("EPISODE %0s", label_);
 end
endtask
task automatic run_traffic;
 input integer limit;
 begin
  fork
{issue}
  join
  while (!({done}) && cycle - t0 < limit) @(posedge clk_main);
  check({done}, "ALL_RESPONSES");
  $display("EPISODE_CYCLES %0d", cycle - t0);
  repeat (30) @(posedge clk_main);
  check({done}, "NO_EXTRA_RESPONSES");
 end
endtask
initial begin
 reset_n = 0;
{chr(10).join(' ' + i for i in init)}
{' '.join(f'{s}_lfsr = 16\'h{0xace1 + 7 * i:04x};' for i, s in enumerate(sources))}
 clear_tables;
 repeat (10) @(posedge clk_main);
{chr(10).join(body)}
 $display("W_AHEAD_COVER s0 lead=%0d s1 lead=%0d", s0_wlead, s1_wlead);
 // The W-ahead master must really offer W before its AW at least once.
 if (w_ahead && s0_wlead == 0) begin $display("ASSERT_FAIL:OUTSTANDING.W_AHEAD_UNCOVERED"); $fatal(1); end
 $display("NOCLOOM_PASS multiple outstanding");
 $finish;
end
endmodule
'''
    (work / 'tb.sv').write_text(text)


def _episode(ep, sources):
    lines = [f' episode("{ep["name"]}");']
    for i, s in enumerate(sources):
        for direction in ('rd', 'wr'):
            cmds = ep['cmds'].get((s, direction), [])
            assert len(cmds) <= MAXC
            lines.append(f' {direction}_n[{i}] = {len(cmds)};')
            for k, c in enumerate(cmds):
                at = f'{i}*MAXC+{k}'
                dest = -1 if c.dest == 'local' else c.dest_index
                parts = [f"{direction}_addr[{at}] = 32'h{c.addr:08x};",
                         f'{direction}_len[{at}] = {c.beats - 1};',
                         f'{direction}_burst[{at}] = {c.burst};', f'{direction}_lock[{at}] = {c.lock};',
                         f'{direction}_id[{at}] = {c.id};', f'{direction}_resp[{at}] = {c.resp};',
                         f'{direction}_dest[{at}] = {dest};']
                if direction == 'rd':
                    parts.append(f'rd_tag[{at}] = {-1 if c.tag is None else c.tag};')
                else:
                    parts.append(f'wr_wlx[{at}] = {c.wlx};')
                lines.append(' ' + ' '.join(parts))
    for line in ep.get('knobs', []):
        lines.append(' ' + line)
    if ep.get('reset_when_crossing'):
        # The reset lands once a request accepted at a source face has not
        # reached its target port and a response that left its target port
        # has not completed at its source: tagged words are in the network
        # and its crossings.
        acc = ' + '.join(f'{s}_ri + {s}_wi' for s in sources)
        done = ' + '.join(f'{s}_rd + {s}_wd' for s in sources)
        lines.append(f''' fork
  fork
{chr(10).join(f'   issue_reads_{s};{chr(10)}   issue_writes_{s};' for s in sources)}
  join
  begin
   while (!({acc} > tgt_acc && tgt_done > {done}) && cycle - t0 < 4000) @(posedge clk_main);
   check({acc} > tgt_acc && tgt_done > {done}, "TAGS_IN_CROSSINGS");
   $display("RESET_IN_CROSSINGS requests=%0d responses=%0d", {acc} - tgt_acc, tgt_done - ({done}));
   // The common reset at the next falling edge: the issuers stop while it
   // is held, and the next episode releases it.
   abort = 1; @(negedge clk_main); reset_n = 0;
  end
 join_any
 while (!abort || reset_n) @(posedge clk_main);
 repeat (8) @(posedge clk_main);''')
    elif 'reset_after' in ep:
        # The issuers stop at the abort flag; the next episode's reset then
        # lands while bursts are still in flight.
        lines.append(f''' fork
  fork
{chr(10).join(f'   issue_reads_{s};{chr(10)}   issue_writes_{s};' for s in sources)}
  join
  begin repeat ({ep['reset_after']}) @(posedge clk_main); abort = 1; end
 join_any
 repeat ({ep['reset_after']} + 2) @(posedge clk_main);''')
    else:
        lines.append(f' run_traffic({ep.get("limit", 20000)});')
    # Bursts in flight reached at the source faces and multi-burst ports,
    # completions that overtook another ID and targets in flight at once.
    lines.append(' $display("IN_FLIGHT ' +
                 ' '.join(f'{s}_r=%0d {s}_w=%0d {s}_rooo=%0d {s}_wooo=%0d {s}_rdests=%0d '
                          f'{s}_wdests=%0d {s}_rilv=%0d' for s in sources) +
                 '", ' + ', '.join(f'{s}_rmax, {s}_wmax, {s}_rooo, {s}_wooo, {s}_rdests, '
                                   f'{s}_wdests, {s}_rilv' for s in sources) + ');')
    for check in ep.get('checks', []):
        lines.append(f' check({check[0]}, "{check[1]}");')
    return '\n'.join(lines)
