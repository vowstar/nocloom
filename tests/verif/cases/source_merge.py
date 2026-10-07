"""A source's request merger and response staging, on the public schema 10 path (CLI).

Sources s0 (one burst), s1 (two bursts) and s2 (one burst, regulated, with a forward packets stage)
each reach their own target. The scenario holds responses at the target or at the source's R or B
ready and judges, from handshake counts alone:

  * a request at the port competes for the merger whatever the engine holds: chosen while no unit
    or slot is free, it stays chosen, so a later request of the other direction waits behind it;
  * a burst's unit and slot free as its last response enters the source's staging, so the next
    burst reaches the target while that response waits for RREADY or BREADY, and the face then
    holds max_outstanding plus the port pipe's burst plus the staged one, and no more;
  * a regulated source's requests past the merger stop at the bound of its burst count: with its
    packets stage free, the next request waits at the merger for the last response, and reaches
    the target at a fixed distance from it.

The monitors judge the holds and the offline checker the data and codes of every beat.

Negative control (RTL text fault): the ID s0's staged B shows reads zero."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

SOURCES = (('s0', 1, {'narrow_bursts': 'split_non_modifiable'}), ('s1', 2, {}),
           ('s2', 1, {'qos': {'shaper': {'bytes_per_1024_cycles': 512, 'burst_bytes': 16},
                              'scheduler': {'under_level': 3, 'over_level': 0,
                                            'threshold_percent': 50}},
                      'pipes': {'packets': {'forward': True, 'backward': False}}}))
TARGETS = (('t0', 0x0000), ('t1', 0x8000), ('t2', 0x10000))

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'source_merge', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                    'max_outstanding': k, **extra} for n, k, extra in SOURCES],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': 4,
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]} for n, base in TARGETS],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def bid_zero(rtl, _map):
    """Every use of s0's BID output reads zero."""
    return [(r'^(\s*assign nl_i_s0_bid = )[^;]+;', r"\g<1>2'd0;", 1)]


CASE = dict(
    name="source_merge",
    contracts=["AXI.SOURCE.MERGE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n, _, _ in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _) in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
    faults=[dict(id="staged_bid", op="text", subs=bid_zero, contract="AXI.SOURCE.MERGE",
                 expect=["SOURCE_B_RESP", "SOURCE_UNANSWERED"])],
)

# The distance from the last response at s2 to its next request at t2, with the merger holding it.
GATED_RETURN = 2


def staged(t, s, target, base):
    """s, one burst: reads then writes wait with their last response staged."""
    t.ready(s, "r", "never")
    for k in range(4):
        t.read(s, base + 0x40 * k, beats=1, id=k % 4, cache=3)
    t.wait(target, "ar", "==", 2, limit=2000, label="STAGED_R_NEXT_REACHES_TARGET")
    t.delay(40)
    t.expect_now(s, "rlast", "==", 0, "STAGED_R_NOT_TAKEN")
    t.expect_now(s, "ar", "==", 3, "STAGED_R_OPEN_LIMIT")
    t.ready(s, "r", "always")
    t.wait(s, "rlast", "==", 4, limit=2000, label="STAGED_R_DONE")
    t.ready(s, "b", "never")
    for k in range(4):
        t.write(s, base + 0x100 + 0x40 * k, beats=1, id=k % 4, cache=3)
    t.wait(target, "aw", "==", 2, limit=2000, label="STAGED_B_NEXT_REACHES_TARGET")
    t.delay(40)
    t.expect_now(s, "b", "==", 0, "STAGED_B_NOT_TAKEN")
    t.expect_now(s, "aw", "==", 3, "STAGED_B_OPEN_LIMIT")
    t.ready(s, "b", "always")
    t.wait(s, "b", "==", 4, limit=2000, label="STAGED_B_DONE")


def scenario(t, seed):
    t.reset(4)
    t.delay(2)
    # Staged responses, one burst.
    t.mark("phase", 1)
    staged(t, "s0", "t0", 0x0000)

    # One burst: the read at the port while one is in flight stays chosen over a later write.
    t.mark("phase", 2)
    t.set("t0", "gate_r", 0)
    t.read("s0", 0x0400, beats=2, id=1, cache=3)
    first = t.snap("t0", "ar")
    t.wait("t0", "ar", "==", 1, limit=2000, label="ONE_FIRST_READ", since=first)
    t.read("s0", 0x0440, beats=2, id=2, cache=3)
    t.delay(6)
    aw = t.snap("t0", "aw")
    t.write("s0", 0x0480, beats=2, id=3, cache=3)
    t.delay(6)
    t.set("t0", "gate_r", 1)
    t.wait("t0", "ar", "==", 2, limit=2000, label="ONE_READ_FIRST", since=first)
    t.expect_now("t0", "aw", "==", 0, "ONE_WRITE_BEHIND_READ", since=aw)
    t.drain(limit=4000, label="one_drain")

    # One burst: the write at the port while one is in flight stays chosen over a later read.
    t.mark("phase", 7)
    t.set("t0", "gate_b", 0)
    aw = t.snap("t0", "aw")
    ar = t.snap("t0", "ar")
    t.write("s0", 0x0500, beats=1, id=1, cache=3)
    t.wait("t0", "aw", "==", 1, limit=2000, label="ONE_FIRST_WRITE", since=aw)
    t.write("s0", 0x0540, beats=1, id=2, cache=3)
    t.delay(6)
    t.read("s0", 0x0580, beats=1, id=3, cache=3)
    t.delay(6)
    t.set("t0", "gate_b", 1)
    t.wait("t0", "aw", "==", 2, limit=2000, label="ONE_WRITE_FIRST", since=aw)
    t.expect_now("t0", "ar", "==", 0, "ONE_READ_BEHIND_WRITE", since=ar)
    t.drain(limit=4000, label="one_w_drain")

    # Two bursts, slots full: the read at the port stays chosen over a later write.
    t.mark("phase", 3)
    t.set("t1", "gate_r", 0)
    first = t.snap("t1", "ar")
    for k in range(2):
        t.read("s1", 0x8400 + 0x40 * k, beats=2, id=k, cache=3)
    t.wait("t1", "ar", "==", 2, limit=2000, label="SLOTS_FULL", since=first)
    t.read("s1", 0x8480, beats=2, id=2, cache=3)
    t.delay(6)
    aw = t.snap("t1", "aw")
    t.write("s1", 0x84c0, beats=2, id=3, cache=3)
    t.delay(6)
    t.set("t1", "gate_r", 1)
    t.wait("t1", "ar", "==", 3, limit=2000, label="SLOTS_READ_FIRST", since=first)
    t.expect_now("t1", "aw", "==", 0, "SLOTS_WRITE_BEHIND_READ", since=aw)
    t.drain(limit=4000, label="slots_drain")

    # Two bursts, slots full: the write at the port stays chosen over a later read.
    t.mark("phase", 8)
    t.set("t1", "gate_b", 0)
    aw = t.snap("t1", "aw")
    ar = t.snap("t1", "ar")
    for k in range(2):
        t.write("s1", 0x8800 + 0x40 * k, beats=1, id=k, cache=3)
    t.wait("t1", "aw", "==", 2, limit=2000, label="W_SLOTS_FULL", since=aw)
    t.write("s1", 0x8880, beats=1, id=2, cache=3)
    t.delay(6)
    t.read("s1", 0x88c0, beats=1, id=3, cache=3)
    t.delay(6)
    t.set("t1", "gate_b", 1)
    t.wait("t1", "aw", "==", 3, limit=2000, label="W_SLOTS_WRITE_FIRST", since=aw)
    t.expect_now("t1", "ar", "==", 0, "W_SLOTS_READ_BEHIND", since=ar)
    t.drain(limit=4000, label="w_slots_drain")

    # Two bursts, no unit free with a read slot free: the same order.
    t.mark("phase", 4)
    t.set("t1", "gate_r", 0)
    t.set("t1", "gate_b", 0)
    first = t.snap("t1", "ar")
    aw = t.snap("t1", "aw")
    t.read("s1", 0x8500, beats=2, id=0, cache=3)
    t.write("s1", 0x8540, beats=2, id=1, cache=3)
    t.wait("t1", "ar", "==", 1, limit=2000, label="POOL_READ", since=first)
    t.wait("t1", "aw", "==", 1, limit=2000, label="POOL_WRITE", since=aw)
    t.read("s1", 0x8580, beats=2, id=2, cache=3)
    t.delay(6)
    t.write("s1", 0x85c0, beats=2, id=3, cache=3)
    t.delay(6)
    t.set("t1", "gate_r", 1)
    t.set("t1", "gate_b", 1)
    t.wait("t1", "ar", "==", 2, limit=2000, label="POOL_READ_FIRST", since=first)
    t.expect_now("t1", "aw", "==", 1, "POOL_WRITE_BEHIND_READ", since=aw)
    t.drain(limit=4000, label="pool_drain")

    # Two bursts: slots free with the last response at the staging, reads then writes.
    t.mark("phase", 5)
    first = t.snap("t1", "ar")
    done = t.snap("s1", "rlast")
    t.ready("s1", "r", "never")
    for k in range(3):
        t.read("s1", 0x8600 + 0x40 * k, beats=1, id=k, cache=3)
    t.wait("t1", "ar", "==", 3, limit=2000, label="SLOT_FREE_AT_R_STAGING", since=first)
    t.expect_now("s1", "rlast", "==", 0, "SLOT_FREE_R_NOT_TAKEN", since=done)
    t.ready("s1", "r", "always")
    t.drain(limit=4000, label="slot_r_drain")
    first = t.snap("t1", "aw")
    done = t.snap("s1", "b")
    t.ready("s1", "b", "never")
    for k in range(3):
        t.write("s1", 0x8700 + 0x40 * k, beats=1, id=k, cache=3)
    t.wait("t1", "aw", "==", 3, limit=2000, label="SLOT_FREE_AT_B_STAGING", since=first)
    t.expect_now("s1", "b", "==", 0, "SLOT_FREE_B_NOT_TAKEN", since=done)
    t.ready("s1", "b", "always")
    t.drain(limit=4000, label="slot_b_drain")

    # A lone B staged with no burst in the engine: the source's zone stays open to take it.
    t.mark("phase", 9)
    done = t.snap("s0", "b")
    t.ready("s0", "b", "never")
    t.write("s0", 0x0600, beats=1, id=1, cache=3)
    t.wait("s0", "bvalid", "==", 1, limit=2000, label="LONE_B_STAGED")
    t.delay(20)
    t.ready("s0", "b", "always")
    t.delay(3)
    t.expect_now("s0", "b", "==", 1, "LONE_B_TAKEN", since=done)
    t.drain(limit=4000, label="lone_b_drain")

    # One burst, a narrow burst cut into pieces: the next piece waits for the unit of the first.
    t.mark("phase", 10)
    t.set("t0", "gate_r", 0)
    first = t.snap("t0", "ar")
    t.read("s0", 0x0700, beats=4, size=1, id=1, cache=0)
    t.wait("t0", "ar", "==", 1, limit=2000, label="PIECE_FIRST", since=first)
    t.delay(20)
    t.expect_now("t0", "ar", "==", 1, "PIECE_WAITS_FOR_UNIT", since=first)
    t.set("t0", "gate_r", 1)
    t.wait("t0", "ar", "==", 4, limit=2000, label="PIECES_DONE", since=first)
    t.drain(limit=4000, label="piece_drain")

    # Regulated, one burst, packets stage free: a write waits at the merger behind the read in flight.
    t.mark("phase", 6)
    t.set("t2", "gate_r", 0)
    t.read("s2", 0x10400, beats=1, id=0, cache=3)
    first = t.snap("t2", "ar")
    t.wait("t2", "ar", "==", 1, limit=2000, label="LIMIT_FIRST", since=None)
    t.write("s2", 0x10440, beats=1, id=1, cache=3)
    t.delay(20)
    done = t.snap("s2", "rlast")
    second = t.snap("t2", "aw")
    t.set("t2", "gate_r", 1)
    t.wait("s2", "rlast", "==", 1, limit=2000, label="LIMIT_ANSWER", since=done)
    t.expect_now("t2", "aw", "==", 0, "LIMIT_STILL_AT_MERGER", since=second)
    t.delay(GATED_RETURN - 1)
    t.expect_now("t2", "aw", "==", 0, "LIMIT_NOT_EARLY", since=second)
    t.delay(1)
    t.expect_now("t2", "aw", "==", 1, "LIMIT_RETURN", since=second)
    t.drain(limit=4000, label="drain")
    t.end()
