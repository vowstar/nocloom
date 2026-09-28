"""Scenario and judgment of the AHB outlet cases: a segment is one source
driving one AHB target through the case list (ahb_outlet_bench.run_case
on its timeline), and the judgment applies the shape model and bus rules of
ahb_shape.py to every segment of a run."""

import ahb_shape as shape
from ahb_shape import STROBE_MODES, Packet, byte_masks, bus_rules, bus_state, runs

WAITS = dict(fixed=(1, 1), zero=(0, 0), random=(2, 2))


class Segment:
    """One source driving one AHB target. win and limit: the target window's
    base and size; slanes and tlanes: source and target bus bytes; complete
    and no_incr: the target's write shaping; merged: a source narrower than
    the target (bytes, unstrobed lanes and bus rules only, no shapes);
    expect_cut: the source cuts WRAP bursts, so a WRAP from its container
    base reaches the target as INCR (on a network that does not cut, a
    negative control of the model); waits: the target's wait profile; mark:
    expectation keys every burst of the segment carries (for its oracle)."""

    def __init__(self, src, tgt, win, limit, slanes, tlanes, cases, complete=False,
                 no_incr=False, merged=False, expect_cut=False, unmapped=(0x3120, 0x3800),
                 waits="fixed", mark=None):
        self.src, self.tgt, self.win, self.limit = src, tgt, win, limit
        self.slanes, self.tlanes, self.cases = slanes, tlanes, cases
        self.complete, self.no_incr, self.merged = complete, no_incr, merged
        self.expect_cut = expect_cut
        self.window = limit < 0x1000
        self.write_base = 0x100 if self.window or tlanes > 4 else 0x120
        self.read_base = 0x000 if self.window else 0x800
        self.unmapped = unmapped
        self.waits = waits
        self.mark = mark or {}

    def big(self):
        return self.tlanes.bit_length() - 1


def run_case(t, k, seg, case, dependent=False):
    """One case after a common reset: the target's wait profile and
    responses, the source's pacing and response backpressure on the
    driver's timeline (cycles from the reset)."""
    s0, t0 = seg.src, seg.tgt
    beats, size, burst, mode, offset = case
    wait_mode, idle_mode = WAITS[seg.waits]
    t.mark("case", k)
    wbase, rbase = seg.win + seg.write_base + offset, seg.win + seg.read_base + offset
    if mode == 8:
        wbase, rbase = seg.unmapped
    t.set(t0, "wait_mode", wait_mode)
    t.set(t0, "idle_mode", 3 if mode == 21 else idle_mode)
    t.set(t0, "wait", 0)
    for reg in ("reg0_lo", "reg0_hi", "reg0_c", "reg1_lo", "reg1_hi", "reg1_c", "fifo_en",
                "ovr_i0", "ovr_c0"):
        t.set(t0, reg, 0)
    full_rate = mode == 25
    t.set(s0, "vrate_w", 0 if full_rate or mode == 21 else 80)
    t.ready(s0, "b", "always" if full_rate else "never")
    t.ready(s0, "r", "always" if full_rate else "never")
    t.release_w(s0, 0)
    t.reset(4)
    t.delay(2)
    tsz = min(size, seg.big())
    # Errors at the second transfer address of each direction.
    if mode in (2, 5, 6):
        code = {2: 1, 5: 2, 6: 3}[mode]
        for reg, start in (("reg0", wbase), ("reg1", rbase)):
            local = start - seg.win + (1 << tsz)
            t.set(t0, reg + "_lo", local)
            t.set(t0, reg + "_hi", local + 1)
            t.set(t0, reg + "_c", code)
    if mode == 14:
        t.set(t0, "fifo_en", 1)
        t.set(t0, "fifo_addr", (rbase - seg.win) // seg.tlanes * seg.tlanes)
    name = ("fixed", "incr", "wrap")[burst]
    strobe = "full"
    if mode in STROBE_MODES:
        t.strobes(s0, byte_masks(wbase, beats, size, burst, mode, seg.slanes), lanes=seg.slanes)
        strobe = "table"
    write = dict(beats=beats, size=size, burst=name, id=5, cache=2, prot=5, strobe=strobe,
                 **seg.mark)
    read = dict(beats=beats, size=size, burst=name, id=6, cache=1, prot=1, **seg.mark)
    if mode in (5, 6):
        write.update(abort_after=2, targets=2, resp=2)
        read.update(abort_after=2, targets=2, beat_resps=[0] + [2] * (beats - 1))
    if mode == 13:
        # The second transfer of each direction fails (by count, one
        # direction at a time).
        for direction in ("w", "r"):
            t.set(t0, "base", 0)
            t.set(t0, "ovr_i0", 2)
            t.set(t0, "ovr_c0", 1)
            if direction == "w":
                t.write(s0, wbase, **write)
            else:
                t.read(s0, rbase, **read)
            t.ready(s0, "b", "delay", 2)
            t.ready(s0, "r", "modulo", 4, 0)
            t.drain(limit=40000, label=f"CASE_{k}")
        return
    events = [(20 if mode == 16 else 5, "aw"), (20 if mode in (12, 15, 17) else 0, "ar")]
    if not full_rate:
        events += [(1100 if mode == 16 else 70, "r_ready"), (80, "b_ready")]
    if mode == 15:
        t.release_w(s0, after_r=1000, from_beat=2)
        events.append((1100, "w_open"))
    if mode == 22:
        t.release_w(s0, after_r=1000, from_beat=beats - 1)
        events += [(190, "idle_low"), (200, "w_open"), (220, "idle_back")]
    if dependent:
        t.release_w(s0, after_r=1, from_beat=1)
    if mode == 4:
        events.append((35, "reset"))
    now = 0
    for at, action in sorted(events, key=lambda e: e[0]):
        if at > now:
            t.delay(at - now)
            now = at
        if action == "aw":
            t.write(s0, wbase, **write)
        elif action == "ar":
            t.read(s0, rbase, **read)
        elif action == "r_ready":
            t.ready(s0, "r", "modulo", 4, 0)
        elif action == "b_ready":
            t.ready(s0, "b", "modulo", 3, 0)
        elif action == "w_open":
            t.release_w(s0, 0)
        elif action == "idle_low":
            t.set(t0, "idle_mode", 4)
        elif action == "idle_back":
            t.set(t0, "idle_mode", idle_mode)
        elif action == "reset":
            t.reset(4)
            t.delay(20)
    if mode == 4:
        return
    if dependent:
        t.wait(s0, "b", "==", 1, limit=3000, label="DEPENDENT_HOLD")
        return
    t.drain(limit=60000, label=f"CASE_{k}")
    if mode == 23:
        # A second packet of each direction after the first completed.
        t.write(s0, wbase, **write)
        t.read(s0, rbase, **read)
        t.drain(limit=60000, label=f"CASE_{k}")


def plan(segments):
    """(case number, segment, case) in the order the scenario marks them."""
    out, k = [], 0
    for seg in segments:
        for case in seg.cases:
            k += 1
            out.append((k, seg, case))
    return out


def case_windows(checker, events):
    if "case" not in checker.tape["marks"]:
        return {}
    index = checker.tape["marks"].index("case")
    starts = [(e["v"], e["t"]) for e in events if e.get("k") == "mk" and e.get("m") == index]
    end = max((e.get("t", 0) for e in events), default=0) + 1
    return {v: (t, starts[i + 1][1] if i + 1 < len(starts) else end)
            for i, (v, t) in enumerate(starts)}


def judge(checker, events, sources, targets, segments, coverage):
    """Every packet of every segment against the model (merged sources: the
    unstrobed write lanes), the bus rules of each target, the ownership of
    each bus and the timing rules of the cases."""
    out = []
    owner = {}
    by_pair = {(seg.src, seg.tgt): seg for seg in segments}
    for s in sources:
        # A burst answered by a target timeout (target any) is not shaped.
        if (s.cancelled or not s.done or s.local_error is not None or not s.window
                or s.expect.get("target") == "any"):
            continue
        seg = by_pair.get((s.port, s.window[0]))
        if seg is None:
            continue
        transfers = sorted(s.targets, key=lambda tt: tt.t)
        strobes = [strb for _, strb in s.beats] if s.dir == "w" else None
        cut = seg.expect_cut and s.burst == 2 and (s.addr - seg.win) % (s.length << s.size) == 0
        p = Packet(s.dir == "w", s.addr, s.length, s.size, s.burst, strobes, seg.tlanes,
                   seg.slanes, (seg.win, seg.limit), complete=seg.complete,
                   no_incr=seg.no_incr, cut=cut)
        if seg.merged:
            # Merged words are checked for their enabled bytes.
            if s.dir == "w":
                out += unstrobed(p, transfers, s.tag)
            continue
        problems = []
        shape.judge_packet(p, transfers, problems, coverage, s.expect.get("abort_after"))
        out += [dict(d, tag=s.tag, dir=s.dir, src=s.port) for d in problems[:1]]
        sparse = p.sparse_from()
        for tt in transfers:
            owner[id(tt)] = (s.dir, p.keeps_bus(p.word_of(tt.addr), sparse)
                            and getattr(tt, "hresp", 0) < 2)
    windows = case_windows(checker, events)
    for tgt in sorted({seg.tgt for seg in segments}):
        seg = next(sg for sg in segments if sg.tgt == tgt)
        out += [dict(d, port=tgt) for d in bus_rules(
            events, tgt, seg.limit, seg.big(), seg.complete, seg.no_incr)]
        bus = sorted((tt for tt in targets if tt.port == tgt), key=lambda tt: tt.t)
        for a, b in zip(bus, bus[1:]):
            mine = owner.get(id(a))
            if mine and mine[1] and b.epoch == a.epoch and b.dir != mine[0]:
                out.append(dict(rule="OWNERSHIP", port=tgt, a=hex(a.addr), next=hex(b.addr)))
                break
        here = [(k, sg, case) for k, sg, case in plan(segments) if sg.tgt == tgt]
        out += timing(windows, bus_state(events, tgt), here, owner, bus)
    return out


def unstrobed(p, transfers, tag):
    """A merged write: every HWDATA lane that no beat of the transfer's
    target word strobes carries zero (the source clears it)."""
    for tt in transfers:
        word = tt.addr // p.lanes
        mask = 0
        for k in range(p.tn):
            if p.tcontainer(k) // p.lanes == word:
                mask |= p.tbeat_strobes(k)
        data = tt.beats[0][0] or 0
        for lane in range(p.lanes):
            if not mask >> lane & 1 and data >> (8 * lane) & 0xFF:
                return [dict(rule="UNSTROBED_BYTE", tag=tag, a=hex(tt.addr), lane=lane)]
    return []


def timing(windows, edges, here, owner, bus):
    """Full-rate throughput (zero wait states, at least 90 transfers in 100
    cycles inside bursts), the bus in BUSY through a long stall, and block
    boundaries (with the fixed profile met by waited cycles)."""
    out = []
    boundary_seen = boundary_waits = 0
    for k, seg, (beats, size, burst, mode, offset) in here:
        if k not in windows:
            continue
        lo, hi = windows[k]
        mine = [tt for tt in bus if lo <= tt.t < hi]
        if mode == 21:
            for a, b in zip(mine, mine[1:]):
                if owner.get(id(a), (None, False))[1] and b.htrans == 2 and b.dir == a.dir:
                    boundary_seen += 1
                    prior = [e for e in edges if e[1] < b.addr_cycle]
                    if prior and prior[-1][2] and prior[-1][3] == 2 and not prior[-1][4]:
                        boundary_waits += 1
        if mode == 25 and seg.waits == "zero" and not seg.window:
            for direction in ("w", "r"):
                count = cycles = 0
                last = None
                for tt in mine:
                    if last is not None and tt.dir == direction and (
                            tt.htrans == 3 or owner.get(id(last), (None, False))[1]):
                        count += 1
                        cycles += tt.addr_cycle - last.addr_cycle
                    last = tt
                if count and count * 100 < cycles * 90:
                    out.append(dict(rule="THROUGHPUT", case=k, dir=direction, transfers=count,
                                    cycles=cycles))
        if seg.merged:
            continue
        stalled = None
        if mode == 15 and not (burst == 2 or seg.no_incr):
            stalled = "w"
        if mode == 16 and beats << max(0, size - seg.big()) > 8:
            stalled = "r"
        if stalled:
            mine_dir = [tt for tt in mine if tt.dir == stalled]
            gaps = [(b.addr_cycle - a.addr_cycle, a, b) for a, b in zip(mine_dir, mine_dir[1:])]
            if not gaps or max(g for g, _, _ in gaps) < 900:
                out.append(dict(rule="LONG_STALL_REACHED", case=k,
                                widest=max((g for g, _, _ in gaps), default=0)))
                continue
            # Between the two transfers the bus shows BUSY, or the next
            # transfer waiting in its address phase; never IDLE.
            _, a, b = max(gaps, key=lambda g: g[0])
            for c0, c1, _ in runs(edges, a.t, b.t, lambda s, tr, rd: not s or tr == 0):
                if c0 > a.addr_cycle and c1 <= b.addr_cycle:
                    out.append(dict(rule="LONG_STALL_BUSY", case=k, cycles=(c0, c1)))
                    break
    segs = list({id(seg): seg for _, seg, _ in here}.values())
    if not boundary_seen and any(seg.no_incr and not seg.merged
                                 and any(c[3] == 21 for c in seg.cases) for seg in segs):
        out.append(dict(rule="BOUNDARY_COVER"))
    if boundary_seen and not boundary_waits and any(seg.waits == "fixed" for seg in segs):
        out.append(dict(rule="BOUNDARY_WAIT_COVER", seen=boundary_seen))
    return out
