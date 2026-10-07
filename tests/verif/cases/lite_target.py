"""AXI4-Lite target behind one AXI4 initiator (fixture path, 32-bit): the
shared word sequencer (one transaction on the bus at a time), the network
exclusive monitor (two reservations, 16-byte granule) and a 1024-cycle target
timer that answers SLVERR after expiry (completion). Each case number is a marker.
The configuration report has the same target with a timer that only reports
(no completion): it runs the reset and the exclusive writes after expiry
(case 12), each configuration with its own faults."""

import os

# A replacement: the value, held low once the target has failed.
UNLESS_FAILED = {"op": "BitAnd", "args": [
    {"self": True}, {"op": "BitNot", "args": [{"select": {"binding": "timeout.failed"}}]}]}

WIDTH = int(os.environ.get("NLV_LITE_WIDTH", "32"))
SB = WIDTH // 8
BASE = 0x1000
# Rising edges from the target's AW or AR handshake of the waiting burst to
# the edge that first samples the timed-out status. The watchdog counts from
# the cycle after the request reaches the outlet; the pipelined outlet's bus
# load is two edges before that handshake: timeout_cycles (1024) minus those
# two, plus one for that cycle and one because the status is a register output
# seen at the following edge. The window is exact.
GAP = 1024

CASE = dict(
    name=f"lite_target_{WIDTH}",
    contracts=["AXI.LITE.WORD_ACCESS", "AXI.TIMEOUT.EXACT", "AXI.EXCL.MONITOR",
               "AXI.RESET.CANCEL", "AXI.ERR.READ_ZERO", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="fixture", recipe="lite_target", params=dict(width=str(WIDTH))),
    nets={"front": [("nl_axi_", "s_")], "target": [("nl_axil_", "t_")]},
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="axi_slave", component="target", endpoint="axil", lite=True,
               sideband=("target", "status", "timed_out"), salt=3)],
    model=dict(routes={"s0": [("t0", BASE, 0x1000)]},
               targets={"t0": dict(kind="lite", bus_bytes=SB, error_data_zero=True)}),
    max_cycles=30000,
    faults=[
        dict(id="read_address", component="target", op="set_driver", port="nl_axil_araddr",
             **{"with": {"op": "Subtract", "args": [{"self": True}, {"const": SB}]}},
             expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "SOURCE_R_DATA"],
             contract="AXI.ROUTE.LOCAL_OFFSET"),
        dict(id="aw_only", component="target", op="replace",
             at={"match": {"op": "BitAnd", "ordered": True, "args": [
                 {"op": "BitOr", "args": [{"binding": "bus.aw_done"}, "*"]},
                 {"op": "BitOr", "args": [{"binding": "bus.w_done"}, "*"]}]}},
             **{"with": {"operand": 0}},
             expect=["AXI.W.HOLD"],
             contract="AXI.LITE.WORD_ACCESS"),
        dict(id="wvalid_late", component="target", op="set_driver", port="nl_axil_wvalid",
             **{"with": {"delay": {"self": True}}},
             expect=["AXI.W.HOLD", "AXI.W.LAST", "TARGET_FOOTPRINT", "TARGET_WRITE_BYTES",
                     "TARGET_UNATTRIBUTED"],
             contract="AXI.LITE.WORD_ACCESS", note="added state: WVALID one cycle late"),
        dict(id="timer_late", component="target", op="replace",
             at={"binding": "timeout.waiting"}, **{"with": {"delay": {"self": True}}},
             expect=["CONTRACT.TIMEOUT.GAP"],
             contract="AXI.TIMEOUT.EXACT", note="added state: the count starts a cycle late"),
        dict(id="timer_stopped", component="target", op="replace",
             at={"binding": "timeout.waiting"}, **{"with": {"const": 0}},
             expect=["CONTRACT.TIMEOUT.GAP"],
             contract="AXI.TIMEOUT.EXACT"),
        dict(id="exclusive_pending", component="target", op="replace",
             at={"match": {"op": "BitAnd", "args": [
                 {"op": "BitAnd", "args": [{"binding": "w.busy"}, {"op": "BitNot"}]}, "*"]},
                 "within": {"binding": "r.exclusive_contaminated"}},
             **{"with": {"const": 0}},
             expect=["SOURCE_B_RESP", "TARGET_WRITE_BYTES"],
             contract="AXI.EXCL.MONITOR"),
        dict(id="exclusive_fail_lost", component="target", op="replace",
             at={"binding": "w.exclusive_fail"}, **{"with": {"const": 0}},
             expect=["SOURCE_B_RESP"],
             contract="AXI.EXCL.MONITOR",
             note="a failed exclusive write answers EXOKAY"),
        dict(id="expired_exclusive_fail_lost", component="target", op="replace",
             at={"binding": "w.fail_now"}, **{"with": {"const": 0}},
             expect=["SOURCE_B_RESP"],
             contract="AXI.EXCL.MONITOR",
             note="after expiry a failed exclusive write answers SLVERR"),
        dict(id="expired_write_keeps_reservation", component="target", op="replace",
             at={"binding": "w.exclusive_clear"}, **{"with": UNLESS_FAILED},
             expect=["SOURCE_B_RESP"],
             contract="AXI.EXCL.MONITOR",
             note="a write to the failed target clears no reservation"),
        dict(id="expired_read_records_nothing", component="target", op="replace",
             at={"binding": "r.exclusive_read"}, **{"with": UNLESS_FAILED},
             expect=["SOURCE_B_RESP"],
             contract="AXI.EXCL.MONITOR",
             note="an exclusive read to the failed target records no reservation"),
    ],
    configs=[
        dict(label=""),
        dict(label="report",
             dut=dict(source="fixture", recipe="lite_target",
                      params=dict(width=str(WIDTH), timeout_completion="off")),
             scenario_args=dict(report=True),
             faults=[
                 dict(id="report_exclusive_answered", component="target", op="replace",
                      at={"binding": "w.frame_push"},
                      **{"with": {"op": "BitOr", "args": [
                          {"self": True},
                          {"op": "BitAnd", "args": [{"operand": 0},
                                                    {"select": {"binding": "w.fail_now"}}]}]}},
                      expect=["CONTRACT.QUIET.B"],
                      contract="AXI.EXCL.MONITOR",
                      note="without completion a failed exclusive write is still answered"),
             ]),
    ],
    negative_faults_all=True,
)


def scenario(t, seed, report=False):
    s0, t0 = "s0", "t0"
    t.set(s0, "w_after_aw", 1)
    t.ready(s0, "b", "delay", 4)        # B and R must hold four cycles unaccepted
    t.ready(s0, "r", "delay", 4)
    t.set(t0, "cap_w", 1)
    t.set(t0, "cap_r", 1)

    def reset():
        t.reset(8)
        for ch in ("aw", "w", "ar"):
            t.ready(t0, ch, "always")
        t.gate(t0, "b", True)
        t.gate(t0, "r", True)
        t.set(t0, "b_default", 0)
        t.set(t0, "r_default", 0)
        t.delay(12)

    def snaps():
        return {(p, k): t.snap(p, k) for p, k in
                ((s0, "aw"), (s0, "ar"), (s0, "b"), (s0, "rlast"), (t0, "aw"), (t0, "w"), (t0, "ar"),
                 (t0, "applied"), (t0, "rlast"), (t0, "b"))}

    reset()
    if report:
        expired_report(t, snaps, reset)
        t.drain()
        t.end()
        return
    # 1: one word per beat, a sparse and a full strobe, then the read back.
    t.mark("case", 1)
    m = snaps()
    t.write(s0, BASE + 0x40, beats=2, strobe="first", prot=3, id=2)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.read(s0, BASE + 0x40, beats=2, prot=5, id=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.expect_now(t0, "w", "==", 2, "WORD_PER_BEAT", since=m[(t0, "w")])
    # 2: a write and a read issued together share the bus; both complete.
    t.mark("case", 2)
    m = snaps()
    t.write(s0, BASE, beats=2, prot=3, id=2)
    t.read(s0, BASE + 0x80, beats=2, prot=5, id=1)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    # 3: a network B held at the source does not hold the read.
    t.mark("case", 3)
    m = snaps()
    t.ready(s0, "b", "never")
    t.write(s0, BASE, strobe="none", prot=3, id=2)
    t.wait(s0, "bvalid", "==", 1, label="NETWORK_B_HELD")
    t.read(s0, BASE + 0x40, beats=2, prot=5, id=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")], label="NETWORK_B_HELD")
    t.expect_now(s0, "bvalid", "==", 1, "NETWORK_B_HELD")
    t.expect_now(t0, "applied", "==", 1, "NETWORK_B_HELD", since=m[(t0, "applied")])
    t.ready(s0, "b", "delay", 4)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    # 4: a network R held at the source does not hold the write.
    t.mark("case", 4)
    m = snaps()
    t.ready(s0, "r", "never")
    t.read(s0, BASE + 0x40, prot=5, id=1)
    t.wait(s0, "rvalid", "==", 1, label="NETWORK_R_HELD")
    t.write(s0, BASE, beats=2, prot=3, id=2)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")], label="NETWORK_R_HELD")
    t.expect_now(s0, "rvalid", "==", 1, "NETWORK_R_HELD")
    t.ready(s0, "r", "delay", 4)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    # 5: target errors in both directions; failed read data is zero.
    t.mark("case", 5)
    m = snaps()
    t.set(t0, "b_default", 2)
    t.set(t0, "r_default", 2)
    t.write(s0, BASE, prot=3, id=2)
    t.read(s0, BASE + 0x40, prot=5, id=1)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.set(t0, "b_default", 0)
    t.set(t0, "r_default", 0)
    # 6: the common reset while a write waits for its B at the target.
    t.mark("case", 6)
    m = snaps()
    t.gate(t0, "b", False)
    t.write(s0, BASE, prot=3, id=2)
    t.read(s0, BASE + 0x40, prot=5, id=1)
    t.wait(t0, "w", "==", 1, since=m[(t0, "w")], label="RESET_NONEMPTY")
    reset()
    m = snaps()
    t.read(s0, BASE + 0x40, prot=5, id=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")], label="POST_RESET")
    t.write(s0, BASE, strobe="first", prot=3, id=2)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")], label="POST_RESET")
    # 7: an exclusive pair succeeds.
    t.mark("case", 7)
    m = snaps()
    t.read(s0, BASE + 0xC0, lock=1, prot=5, id=1, resp=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.write(s0, BASE + 0xC0, lock=1, prot=3, id=1, resp=1)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    # 8: a write waiting at the target when the exclusive read is taken
    # contaminates the reservation. The exclusive write fails: it still goes
    # out as one write with WSTRB zero, changes no byte and answers OKAY.
    t.mark("case", 8)
    m = snaps()
    t.gate(t0, "b", False)
    t.write(s0, BASE + 0xC0, prot=3, id=1)
    t.wait(t0, "w", "==", 1, since=m[(t0, "w")])
    t.read(s0, BASE + 0xC0, lock=1, prot=5, id=1, resp=1)
    t.delay(24)
    t.gate(t0, "b", True)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    m = snaps()
    t.write(s0, BASE + 0xC0, lock=1, prot=3, id=1, resp=0, bytes="none")
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.expect_now(t0, "w", "==", 1, "FAILED_EXCLUSIVE_WSTRB_ZERO", since=m[(t0, "w")])
    # 9: AW done, W stalled: the write times out exactly 1024 target cycles
    # after it reached the outlet; the stalled W stays offered, later words answer SLVERR
    # without access, and the late B is accepted and discarded.
    t.mark("case", 9)
    reset()
    m = snaps()
    t.set(t0, "gap_dir", 1)
    t.set(t0, "gap_lo", GAP)
    t.set(t0, "gap_hi", GAP)
    t.ready(t0, "w", "never")
    t.write(s0, BASE, prot=3, id=2, resp=2, target="any")
    t.wait(t0, "aw", "==", 1, since=m[(t0, "aw")])
    t.wait(t0, "sideband", "==", 1, limit=1040, label="WRITE_TIMEOUT_BOUND")
    t.expect_now(t0, "w", "==", 0, "WRITE_TIMEOUT_REACHED", since=m[(t0, "w")])
    t.expect_now(t0, "wvalid", "==", 1, "WRITE_TIMEOUT_REACHED")
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.read(s0, BASE + 0x40, prot=5, id=1, resp=2, target="none")
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.expect_now(t0, "wvalid", "==", 1, "LATE_W_STABLE")
    t.ready(t0, "w", "always")
    t.wait(t0, "b", "==", 1, since=m[(t0, "b")], label="LATE_B")
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.delay(12)
    t.quiet(s0, "b", False)
    t.quiet(s0, "r", False)
    t.set(t0, "gap_dir", 0)
    reset()
    # 10: R stalled at the target: the read times out exactly; a later write
    # answers SLVERR without access and the late R is discarded.
    t.mark("case", 10)
    m = snaps()
    t.set(t0, "gap_dir", 2)
    t.set(t0, "gap_lo", GAP)
    t.set(t0, "gap_hi", GAP)
    t.gate(t0, "r", False)
    t.read(s0, BASE + 0x40, prot=5, id=1, resp=2, target="any")
    t.wait(t0, "ar", "==", 1, since=m[(t0, "ar")])
    t.wait(t0, "sideband", "==", 1, limit=1060, label="READ_TIMEOUT_BOUND")
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.write(s0, BASE, prot=3, id=2, resp=2, target="none")
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    t.gate(t0, "r", True)
    t.wait(t0, "rlast", "==", 1, since=m[(t0, "rlast")], label="LATE_R")
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.delay(12)
    t.quiet(s0, "b", False)
    t.quiet(s0, "r", False)
    t.set(t0, "gap_dir", 0)
    reset()
    m = snaps()
    t.read(s0, BASE + 0x40, prot=5, id=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    t.write(s0, BASE, strobe="first", prot=3, id=2)
    t.wait(s0, "b", "==", 1, since=m[(s0, "b")])
    expired_monitor(t, snaps, reset)
    t.drain()
    t.end()


def expired_monitor(t, snaps, reset):
    """11: the monitor keeps its rules after a timeout with completion. The
    key {s0, 1} records 0x100 before expiry; after it every burst answers
    without access: an exclusive write without a matching reservation OKAY
    (also once its own matching write or a normal write cleared it), one with
    a matching reservation (recorded before expiry, or by an exclusive read
    answered SLVERR after it) SLVERR."""
    s0, t0 = "s0", "t0"
    t.mark("case", 11)
    m = snaps()
    t.read(s0, BASE + 0x100, lock=1, prot=5, id=1, resp=1)
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    m = snaps()
    t.gate(t0, "r", False)
    t.read(s0, BASE + 0x40, prot=5, id=2, resp=2, target="any")
    t.wait(t0, "sideband", "==", 1, limit=1100, label="EXCLUSIVE_EXPIRY")
    t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
    for write, offset, ident, lock, resp, label in (
            (True, 0x180, 2, 1, 0, "UNMATCHED_OKAY"),
            (True, 0x100, 1, 1, 2, "MATCHED_SLVERR"),
            (True, 0x100, 1, 1, 0, "OWN_WRITE_CLEARS"),
            (False, 0x140, 1, 1, 2, "EXPIRED_READ_RECORDS"),
            (True, 0x140, 2, 0, 2, "NORMAL_WRITE_CLEARS"),
            (True, 0x140, 1, 1, 0, "CLEARED_OKAY"),
            (False, 0x1C0, 1, 1, 2, "EXPIRED_READ_RECORDS"),
            (True, 0x1C0, 1, 1, 2, "RECORDED_AFTER_EXPIRY_SLVERR")):
        m = snaps()
        if write:
            t.write(s0, BASE + offset, lock=lock, prot=3, id=ident, resp=resp, target="none")
            t.wait(s0, "b", "==", 1, since=m[(s0, "b")], label=label)
        else:
            t.read(s0, BASE + offset, lock=lock, prot=5, id=ident, resp=resp, target="none")
            t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")], label=label)
    m = snaps()
    t.gate(t0, "r", True)
    t.wait(t0, "rlast", "==", 1, since=m[(t0, "rlast")], label="EXCLUSIVE_LATE_R")
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.delay(12)
    t.quiet(s0, "b", False)
    t.quiet(s0, "r", False)
    reset()


def expired_report(t, snaps, reset):
    """12 (configuration report): without completion the failure is only
    reported. After expiry an exclusive write is accepted, write data included,
    and never answered, whether it holds a matching reservation (round 1) or not
    (round 2), and does not reach the target; the common reset ends it. The
    source keeps one write in flight, so each round has its own expiry."""
    s0, t0 = "s0", "t0"
    t.mark("case", 12)
    for offset, ident, label in ((0x100, 1, "MATCHED"), (0x180, 2, "UNMATCHED")):
        m = snaps()
        t.read(s0, BASE + 0x100, lock=1, prot=5, id=1, resp=1)
        t.wait(s0, "rlast", "==", 1, since=m[(s0, "rlast")])
        m = snaps()
        w = t.snap(s0, "w")
        t.gate(t0, "r", False)
        t.read(s0, BASE + 0x40, prot=5, id=2)
        t.wait(t0, "sideband", "==", 1, limit=1100, label="REPORT_EXPIRY")
        t.quiet(s0, "b", True)
        t.quiet(s0, "r", True)
        t.write(s0, BASE + offset, lock=1, prot=3, id=ident)
        t.wait(s0, "w", "==", 1, since=w, limit=200, label="REPORT_" + label + "_ABSORBED")
        t.delay(40)
        t.expect_now(t0, "aw", "==", 0, "REPORT_" + label + "_NO_ACCESS", since=m[(t0, "aw")])
        t.gate(t0, "r", True)
        t.wait(t0, "rlast", "==", 1, since=m[(t0, "rlast")], label="REPORT_LATE_R")
        t.delay(12)
        reset()
        t.quiet(s0, "b", False)
        t.quiet(s0, "r", False)
