"""Decode misses beside writes in flight and turns for the last entry, with
separate read and write paths and, as a companion, with shared paths (public
CLI).

AXI4 sources s0 and s1 keep four bursts each. The AXI4 target t0 keeps two
bursts and answers the decode misses of both sources (its window is the
lowest). The AXI4 target t1 keeps one burst.

Phases 1 to 3 put a decode miss of s1 beside two one-beat writes of s0 in
flight at t0. With two entries the outlet of t0 takes the AW of the second
write, or its first W beat, while the rest of that head word still waits on
the network side. The miss may only enter once t0 is empty, so that partly taken
write must still pass.
  1. t0 holds WREADY low: both AW reach t0, then s1 reads an unmapped
     address. s1 holds RREADY low until both writes have their B: the
     writes complete while the miss answer waits.
  2. t0 holds AWREADY low: the first beat of the second write enters ahead
     of its AW. s1 reads an unmapped address.
  3. As phase 1 with a write miss of s1.
Every write and every miss must complete within LIMIT.

Phase 4 sends a read and a write of s0 to t0, which answers each after 40
cycles, then a read miss and a write miss of s0 with the same AxID: the
misses must answer after the earlier transactions of their ID.

Phases 7 and 8 come last and keep the other path busy while a miss waits.
In phase 7 s0 sends PRESSES writes of one and eight beats in turn to t0,
which answers each after PRESS_LATENCY cycles. The bursts end apart, so t0
never empties while they press. Once two AW reached t0, s1 reads an unmapped
address. The waiting miss holds back the write heads, so t0 empties and the
miss enters: at most PRESS_MOST AW reach t0 from the miss's issue to its
answer. Phase 8 swaps the directions: reads press and s1 writes an unmapped
address.

Phase 5 takes turns at t1 with reads pressing. s1 sends six one-beat reads
to t1 (t1 answers each after LATENCY cycles), and once the first reached t1,
s0 writes one beat to t1. A read that took the entry gives the next turn to
the write, so exactly one AR reaches t1 before its AW (docs/read-write-paths.md,
Capacity). Phase 6 swaps the directions: six writes of s0 press and one AW
reaches t1 before the AR of s1. With shared paths the requests arrive in
order on one path and need no turn: the companion checks at most TURNS
handshakes before the one that waits.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

MISS = 0x200000
TURN_AT, TURNS, LATENCY = 0x10000, 6, 8
LIMIT = 2000  # cycles a wait may take before it is a hang: each phase needs under 200
# Phases 7 and 8: the requests that press, t0's latency, and the handshakes of the pressing
# direction allowed at t0 while the miss waits (at most its two entries' worth).
PRESSES, PRESS_LATENCY, PRESS_MOST = 16, 12, 2
# Bursts of unequal length end apart.
PRESS_BEATS = (1, 8)


def configuration(paths):
    config = {
        'schema_version': 10, 'name': 'rw_entries', 'address_width': 32,
        'read_write': paths,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4} for n in ('s0', 's1')],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'max_outstanding': 2,
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]},
                    {'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'mappings': [{'base': hex(TURN_AT), 'size': '0x10000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    return endpoint_ports(config, id_width=2, len_width=8)


SEPARATE, SHARED = configuration('separate'), configuration('shared')



def model(config):
    return dict(routes=nlconfig.routes(config), keeps_decerr=nlconfig.keeps_decerr(config),
                targets=nlconfig.targets(config, error_data_zero=True))


def phase_starts(checker, events):
    """phase number -> the cycle of its mark."""
    index = checker.tape["marks"].index("phase")
    return {e["v"]: e["c"] for e in events if e.get("k") == "mk" and e.get("m") == index}


def before_first(events, start, counted, first):
    """The handshakes of channel counted at t1 from cycle start before the first of channel
    first, or None."""
    n = 0
    for e in events:
        if e.get("p") != "t1" or e.get("k") != "hs" or e["c"] < start:
            continue
        if e.get("ch") == counted:
            n += 1
        elif e.get("ch") == first:
            return n
    return None


def oracle(checker, events, sources, targets):
    del sources, targets
    args = checker.case.get("scenario_args", {})
    most = TURNS if args.get("shared") else args.get("entries", 1)
    starts = phase_starts(checker, events)
    out = []
    for phase, counted, first in ((5, "ar", "aw"), (6, "aw", "ar")):
        before = before_first(events, starts.get(phase, 0), counted, first)
        if before is None or before > most:
            out.append(dict(detail=f"phase {phase}: the {first.upper()} did not get its turn",
                            before=str(before)))
    out += pressed(checker, events)
    return out


def pressed(checker, events):
    """Phases 7 and 8: the handshakes of the pressing direction at t0 from the miss's issue to
    its answer at s1."""
    index = checker.tape["marks"].index("press")
    out = []
    for phase, counted, answer in ((7, "aw", "r"), (8, "ar", "b")):
        start = next((e["c"] for e in events if e.get("k") == "mk" and e.get("m") == index
                      and e["v"] == phase), None)
        end = next((e["c"] for e in events if start is not None and e.get("c", -1) > start
                    and e.get("p") == "s1" and e.get("k") == "hs" and e.get("ch") == answer
                    and (answer == "b" or e.get("l") == 1)), None)
        if end is None:
            out.append(dict(detail=f"phase {phase}: no miss answer"))
            continue
        n = sum(1 for e in events if start <= e.get("c", -1) <= end and e.get("p") == "t0"
                and e.get("k") == "hs" and e.get("ch") == counted)
        if n > PRESS_MOST:
            out.append(dict(detail=f"phase {phase}: {n} {counted.upper()} at t0 while the miss "
                                   f"waited", most=str(PRESS_MOST)))
    return out


BFMS = [dict(name=n, kind="axi_master", endpoint="initiator:" + n) for n in ('s0', 's1')]
BFMS += [dict(name=n, kind="axi_slave", endpoint="target:" + n, salt=k + 1)
         for k, n in enumerate(('t0', 't1'))]
CASE = dict(
    name="rw_entries",
    contracts=["AXI.PATHS.SEPARATE", "AXI.ERR.UNMAPPED_DECERR"],
    dut=dict(source="cli", config=SEPARATE),
    bfms=BFMS,
    model=model(SEPARATE),
    max_cycles=20000,
    oracle=oracle,
    configs=[dict(label=""),
             dict(label="shared", dut=dict(source="cli", config=SHARED), model=model(SHARED),
                  scenario_args=dict(shared=True))],
    faults=[],
)


def miss_beside_writes(t, hold, write_miss, label):
    """Two writes of s0 in flight at t0 with t0 holding hold, then a miss of s1."""
    aw0, b0 = t.snap("t0", "aw"), t.snap("s0", "b")
    b1, r1 = t.snap("s1", "b"), t.snap("s1", "rlast")
    t.ready("t0", hold, "never")
    t.ready("s1", "r", "never")
    t.write("s0", 0x100, id=0)
    t.write("s0", 0x200, id=1)
    if hold == "w":
        t.wait("t0", "aw", "==", 2, since=aw0, limit=LIMIT, label=label + "_TWO_AW")
    t.delay(40)
    if write_miss:
        t.write("s1", MISS, id=2)
    else:
        t.read("s1", MISS, id=2)
    t.delay(40)
    t.ready("t0", hold, "always")
    t.wait("s0", "b", "==", 2, since=b0, limit=LIMIT, label=label + "_WRITES")
    t.ready("s1", "r", "always")
    if write_miss:
        t.wait("s1", "b", "==", 1, since=b1, limit=LIMIT, label=label + "_MISS")
    else:
        t.wait("s1", "rlast", "==", 1, since=r1, limit=LIMIT, label=label + "_MISS")
    t.drain(limit=LIMIT, label=label + "_DRAIN")


def scenario(t, seed, shared=False, entries=1, write_miss=True):
    del shared, entries  # the oracle judges the configurations apart
    t.reset(4)
    t.delay(2)
    t.mark("phase", 1)
    miss_beside_writes(t, "w", False, "READ_MISS_AW_IN")
    t.mark("phase", 2)
    miss_beside_writes(t, "aw", False, "READ_MISS_BEAT_IN")
    if write_miss:
        t.mark("phase", 3)
        miss_beside_writes(t, "w", True, "WRITE_MISS_AW_IN")
        t.mark("phase", 4)
        t.set("t0", "r_lat", 40)
        t.set("t0", "b_lat", 40)
        r0, b0 = t.snap("s0", "rlast"), t.snap("s0", "b")
        t.read("s0", 0x300, beats=4, id=1)
        t.write("s0", 0x400, beats=4, id=1)
        t.delay(10)
        t.read("s0", MISS, id=1)
        t.write("s0", MISS, id=1)
        t.wait("s0", "rlast", "==", 2, since=r0, limit=LIMIT, label="ORDER_READS")
        t.wait("s0", "b", "==", 2, since=b0, limit=LIMIT, label="ORDER_WRITES")
        t.drain(limit=LIMIT, label="ORDER_DRAIN")
        t.set("t0", "r_lat", 0)
        t.set("t0", "b_lat", 0)
    t.mark("phase", 5)
    t.set("t1", "r_lat", LATENCY)
    t.set("t1", "b_lat", LATENCY)
    ar0, b0 = t.snap("t1", "ar"), t.snap("s0", "b")
    for k in range(TURNS):
        t.read("s1", TURN_AT + 4 * k, beats=1, id=k % 4)
    t.wait("t1", "ar", ">=", 1, since=ar0, limit=LIMIT, label="TURN_READS_STARTED")
    t.write("s0", TURN_AT + 0x100, beats=1, id=3)
    t.wait("s0", "b", "==", 1, since=b0, limit=LIMIT, label="TURN_WRITE")
    t.drain(limit=LIMIT, label="DRAIN_READ_TURNS")
    t.mark("phase", 6)
    aw0, r1 = t.snap("t1", "aw"), t.snap("s1", "rlast")
    for k in range(TURNS):
        t.write("s0", TURN_AT + 4 * k, beats=1, id=k % 4)
    t.wait("t1", "aw", ">=", 1, since=aw0, limit=LIMIT, label="TURN_WRITES_STARTED")
    t.read("s1", TURN_AT + 0x100, beats=1, id=3)
    t.wait("s1", "rlast", "==", 1, since=r1, limit=LIMIT, label="TURN_READ")
    t.drain(limit=LIMIT, label="DRAIN_WRITE_TURNS")
    t.set("t1", "r_lat", 0)
    t.set("t1", "b_lat", 0)
    for phase, write in ((7, True), (8, False)):
        t.mark("phase", phase)
        t.set("t0", "r_lat", PRESS_LATENCY)
        t.set("t0", "b_lat", PRESS_LATENCY)
        seen0 = t.snap("t0", "aw" if write else "ar")
        for k in range(PRESSES):
            if write:
                t.write("s0", 0x500 + 0x40 * k, beats=PRESS_BEATS[k % 2], id=k % 4)
            else:
                t.read("s0", 0x500 + 0x40 * k, beats=PRESS_BEATS[k % 2], id=k % 4)
        t.wait("t0", "aw" if write else "ar", ">=", 2, since=seen0, limit=LIMIT,
               label=f"P{phase}_PRESSING")
        t.mark("press", phase)
        answered = t.snap("s1", "rlast" if write else "b")
        if write:
            t.read("s1", MISS, id=2)
        else:
            t.write("s1", MISS, id=2)
        t.wait("s1", "rlast" if write else "b", "==", 1, since=answered, limit=LIMIT,
               label=f"P{phase}_MISS")
        t.drain(limit=LIMIT, label=f"P{phase}_DRAIN")
        t.set("t0", "r_lat", 0)
        t.set("t0", "b_lat", 0)
    t.end()
