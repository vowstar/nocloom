"""Negative control of every online rule (registry_contracts evidence).

One compile of the generated bench around sv/nlv_rule_stub.v; one run per
scenario. The stub's mode 0 is a legal design and the clean scenario must
pass the online monitors and the offline checker (including the reset
propagation self-check). Every other scenario breaks one rule, through a
stub mode (+NLV_STUB=k) or a tape that violates a declared contract, and
must stop with exactly that rule as the first failure.

  python3 tests/verif/rulecheck.py --stub sv/nlv_rule_stub.v --work DIR [--sim ENGINE]

Exit code: 0 every scenario met its expectation, 1 a scenario did not,
2 infrastructure (bench generation or compile) error.
"""

import argparse
import json
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from nlverif import bench as benchgen  # noqa: E402
from nlverif import contracts as C  # noqa: E402
from nlverif.tape import Tape  # noqa: E402

# Stub modes; the numbers match the localparams of nlv_rule_stub.v.
MODES = dict(GOOD=0, B_HOLD=1, R_HOLD=2, B_EXTRA=3, R_EXTRA=4, R_LAST=5, B_EARLY=6,
             M_RESET=7, B_X=8, R_X=9, AW_HOLD=10, W_HOLD=11, AR_HOLD=12, W_LAST=13,
             T_RESET=14, AW_X=15, W_X=16, AR_X=17, GAP=18, APB_RESET=19, APB_X=20,
             APB_ORDER=21, APB_STABLE=22, APB_DROP=23, APB_ABANDON=24, AHB_RESET=25,
             AHB_X=26, AHB_HOLD=27, STREAM_HOLD=28, MERGE=29, B_SAME=30, AHB_BUSY_OUT=31,
             AHB_BUSY_CTRL=32, AHB_BUSY_OK=33, AHB_CTRL_X=34, AHB_RESP_X=35,
             AHB_DATA_X=36, AHB_DATA_MOVE=37, AHB_CANCEL_OK=38)


def _pre(t):
    t.reset(8)
    t.delay(2)


def _run(*steps):
    """A tape: reset, the given steps in order, a settle delay, END."""
    def build(t):
        _pre(t)
        for step in steps:
            step(t)
        t.delay(40)
        t.end()
    return build


def W(beats=4, id=1):
    return lambda t: t.write("s0", 0x100, beats=beats, id=id)


def R(beats=4, id=2):
    return lambda t: t.read("s0", 0x200, beats=beats, id=id)


def SET(port, register, value):
    return lambda t: t.set(port, register, value)


def READY(port, channel, mode):
    return lambda t: t.ready(port, channel, mode)


def _clean(t):
    _pre(t)
    t.write("s0", 0x100, beats=4, id=1)
    t.read("s0", 0x100, beats=4, id=2)
    t.wait("s0", "b", "==", 1, limit=200, label="CLEAN_B")
    t.wait("s0", "rlast", "==", 1, limit=200, label="CLEAN_R")
    t.delay(40)
    t.drain(limit=200)
    t.end()


def _b_after_wlast(t):
    """A zero-latency target: BVALID rises in the cycle right after the
    WLAST handshake, the earliest legal cycle."""
    _pre(t)
    t.ready("t0", "w", "always")
    t.set("t0", "b_lat", 0)
    t.write("s0", 0x100, beats=1, id=1)
    t.wait("s0", "b", "==", 1, limit=50, label="B_AFTER_WLAST")
    t.delay(10)
    t.drain(limit=200)
    t.end()


def _bad_bfm(t):
    _pre(t)
    t._emit("WRITE", 99)
    t.end()


def _bad_op(t):
    _pre(t)
    t.records.append([77, 0] + [0] * 14)
    t.end()


def _queue_full(t):
    _pre(t)
    t.gate("s0", "aw", False)
    t.gate("s0", "w", False)
    for k in range(65):
        t.write("s0", 0x100 + 4 * k, id=k % 4)
    t.delay(10)
    t.end()


def _live(premise, broken):
    """Liveness premises on the MERGE stub (shared read/write routes). The
    broken tape hangs and the stall report must name the premise; the legal
    tape keeps the same endpoint dependency but orders the traffic so the
    premise holds, and must pass."""
    def build(t):
        if premise == "E1":
            # The initiator releases W beats 1.. only after one R beat.
            t.set("s0", "w_after_aw", 1)
            t.release_w("s0", after_r=1, from_beat=1)
        elif premise == "E2":
            # RREADY waits for a B; the target answers B 20 cycles late so
            # the R is offered first.
            t.ready("s0", "r", "await_b", 1)
            t.set("t0", "b_lat", 20)
        else:
            # The target takes W only after one of its reads completed.
            t.set("s0", "w_after_aw", 1)
            t.release_w("t0", after_r=1)
        _pre(t)
        write = dict(E1=4, E2=1, E3=2)[premise]
        if broken and premise in ("E1", "E3"):
            t.write("s0", 0x100, beats=write, id=1)
            t.wait("s0", "aw", "==", 1, limit=50)
            t.wait("s0" if premise == "E1" else "t0", "w" if premise == "E1" else "wvalid",
                   "==", 1, limit=50)
            t.read("s0", 0x200, beats=2, id=2)
            t.wait("s0", "rlast", "==", 1, limit=200, label=premise + "_READ")
        elif broken:
            t.read("s0", 0x200, beats=4, id=2)
            t.wait("s0", "rvalid", "==", 1, limit=50)
            t.write("s0", 0x100, beats=write, id=1)
            t.wait("s0", "b", "==", 1, limit=200, label="E2_B")
        else:
            if premise == "E2":
                t.ready("s0", "r", "always")
            t.read("s0", 0x200, beats=4 if premise == "E2" else 2, id=2)
            t.wait("s0", "rlast", "==", 1, limit=200, label=premise + "_READ")
            t.write("s0", 0x100, beats=write, id=1)
            t.wait("s0", "b", "==", 1, limit=200, label=premise + "_B")
        t.delay(40)
        t.end()
    return build


# (scenario, stub mode, tape builder, expected first rule; None = clean)
SCENARIOS = [
    ("clean", "GOOD", _clean, None),
    ("b_hold", "B_HOLD", _run(READY("s0", "b", "never"), W()), "AXI.B.HOLD"),
    ("r_hold", "R_HOLD", _run(READY("s0", "r", "never"), R()), "AXI.R.HOLD"),
    ("b_unexpected", "B_EXTRA", _run(), "AXI.B.UNEXPECTED"),
    ("r_unexpected", "R_EXTRA", _run(), "AXI.R.UNEXPECTED"),
    ("r_last", "R_LAST", _run(R()), "AXI.R.LAST"),
    ("b_before_wlast", "B_EARLY", _run(W(beats=2)), "AXI.B.BEFORE_WLAST"),
    # The boundary: BVALID in the cycle of the WLAST handshake breaks the
    # rule; BVALID in the next cycle (a zero-latency target) is legal.
    ("b_with_wlast", "B_SAME", _run(SET("s0", "w_after_aw", 1), W(beats=1)),
     "AXI.B.BEFORE_WLAST"),
    ("b_after_wlast_legal", "GOOD", _b_after_wlast, None),
    ("m_reset_quiet", "M_RESET", _run(), "AXI.RESET.QUIET"),
    ("b_x", "B_X", _run(), "AXI.X.BVALID"),
    ("r_x", "R_X", _run(), "AXI.X.RVALID"),
    ("aw_hold", "AW_HOLD", _run(READY("t0", "aw", "never"), W()), "AXI.AW.HOLD"),
    ("w_hold", "W_HOLD", _run(READY("t0", "w", "never"), W()), "AXI.W.HOLD"),
    ("ar_hold", "AR_HOLD", _run(READY("t0", "ar", "never"), R()), "AXI.AR.HOLD"),
    ("w_last", "W_LAST", _run(W()), "AXI.W.LAST"),
    ("t_reset_quiet", "T_RESET", _run(), "AXI.RESET.QUIET"),
    ("aw_x", "AW_X", _run(), "AXI.X.AWVALID"),
    ("w_x", "W_X", _run(), "AXI.X.WVALID"),
    ("ar_x", "AR_X", _run(), "AXI.X.ARVALID"),
    ("timeout_gap", "GAP", _run(SET("t0", "gap_dir", 1), SET("t0", "gap_lo", 5),
                                SET("t0", "gap_hi", 6)), "CONTRACT.TIMEOUT.GAP"),
    ("quiet_aw", "GOOD", _run(lambda t: t.quiet("t0", "aw", True), W()), "CONTRACT.QUIET.AW"),
    ("quiet_w", "GOOD", _run(lambda t: t.quiet("t0", "w", True), W()), "CONTRACT.QUIET.W"),
    ("quiet_ar", "GOOD", _run(lambda t: t.quiet("t0", "ar", True), R()), "CONTRACT.QUIET.AR"),
    ("quiet_b", "GOOD", _run(lambda t: t.quiet("s0", "b", True), W()), "CONTRACT.QUIET.B"),
    ("quiet_r", "GOOD", _run(lambda t: t.quiet("s0", "r", True), R()), "CONTRACT.QUIET.R"),
    ("quiet_apb", "GOOD", _run(SET("p0", "quiet", 1)), "CONTRACT.QUIET.APB"),
    ("quiet_ahb", "GOOD", _run(SET("h0", "quiet", 1)), "CONTRACT.QUIET.AHB"),
    ("throughput", "GOOD", _run(lambda t: t.throughput("t0", "aw", num=1, den=1),
                                lambda t: t.delay(10), lambda t: t.throughput("t0", "aw")),
     "CONTRACT.THROUGHPUT"),
    ("outstanding_w", "GOOD", _run(SET("t0", "out_cap", 1), lambda t: t.gate("t0", "b", False),
                                   W(beats=1, id=0), W(beats=1, id=1)), "CONTRACT.OUTSTANDING.W"),
    ("outstanding_r", "GOOD", _run(SET("t0", "out_cap", 1), lambda t: t.gate("t0", "r", False),
                                   R(beats=1, id=0), R(beats=1, id=1)), "CONTRACT.OUTSTANDING.R"),
    ("tape_expect", "GOOD", _run(lambda t: t.expect_now("s0", "aw", "==", 5, "NEVER_FIVE")),
     "TAPE.EXPECT"),
    ("tape_wait", "GOOD", _run(lambda t: t.wait("s0", "b", "==", 1, limit=5, label="NEVER_B")),
     "TAPE.WAIT_TIMEOUT"),
    ("tape_drain", "GOOD", _run(lambda t: t.gate("t0", "b", False), W(beats=1),
                                lambda t: t.drain(limit=20, label="DRAIN_STUCK")),
     "TAPE.DRAIN_TIMEOUT"),
    ("queue_full", "GOOD", _queue_full, "TB.QUEUE_FULL"),
    ("bad_header", "GOOD", _clean, "TB.TAPE"),
    ("bad_bfm", "GOOD", _bad_bfm, "TB.BAD_BFM"),
    ("bad_op", "GOOD", _bad_op, "TB.BAD_OP"),
    ("apb_reset_quiet", "APB_RESET", _run(), "APB.RESET.QUIET"),
    ("apb_x", "APB_X", _run(), "APB.X.PSEL"),
    ("apb_setup_order", "APB_ORDER", _run(), "APB.SETUP.ORDER"),
    ("apb_stable", "APB_STABLE", _run(SET("p0", "wait", 3)), "APB.ACCESS.STABLE"),
    ("apb_penable_drop", "APB_DROP", _run(), "APB.PENABLE.DROP"),
    ("apb_abandon", "APB_ABANDON", _run(SET("p0", "wait", 3)), "APB.ACCESS.ABANDON"),
    ("ahb_reset_quiet", "AHB_RESET", _run(), "AHB.RESET.QUIET"),
    ("ahb_x", "AHB_X", _run(), "AHB.X.HTRANS"),
    ("ahb_addr_hold", "AHB_HOLD", _run(SET("h0", "wait", 2)), "AHB.ADDR.HOLD"),
    ("ahb_busy_outside", "AHB_BUSY_OUT", _run(), "AHB.BUSY.OUTSIDE"),
    ("ahb_busy_control", "AHB_BUSY_CTRL", _run(), "AHB.BUSY.CONTROL"),
    ("ahb_busy_legal", "AHB_BUSY_OK", _run(), None),
    ("ahb_control_x", "AHB_CTRL_X", _run(), "AHB.X.CONTROL"),
    ("ahb_response_x", "AHB_RESP_X", _run(), "AHB.X.RESPONSE"),
    ("ahb_data_x", "AHB_DATA_X", _run(), "AHB.X.DATA"),
    ("ahb_data_hold", "AHB_DATA_MOVE", _run(SET("h0", "wait", 3)), "AHB.DATA.HOLD"),
    ("ahb_cancel_missing", "GOOD", _run(SET("h0", "reg0_lo", 32), SET("h0", "reg0_hi", 36),
                                       SET("h0", "reg0_c", 2)), "AHB.CANCEL.ADDRESS"),
    ("ahb_cancel_legal", "AHB_CANCEL_OK", _run(SET("h0", "reg0_lo", 32), SET("h0", "reg0_hi", 36),
                                              SET("h0", "reg0_c", 2)), None),
    ("ahb_response_format", "GOOD", _run(SET("h0", "err_default", 1),
                                         SET("h0", "response_one_cycle", 1)), "AHB.RESPONSE.TWO_CYCLE"),
    ("stream_hold", "STREAM_HOLD", _run(), "STREAM.HOLD"),
    ("live_e1", "MERGE", _live("E1", True), "LIVE.E1"),
    ("live_e1_legal", "MERGE", _live("E1", False), None),
    ("live_e2", "MERGE", _live("E2", True), "LIVE.E2"),
    ("live_e2_legal", "MERGE", _live("E2", False), None),
    ("live_e3", "MERGE", _live("E3", True), "LIVE.E3"),
    ("live_e3_legal", "MERGE", _live("E3", False), None),
]

# Rules proven here (read by the registry gate).
PROVEN = sorted({rule for _, _, _, rule in SCENARIOS if rule})


def stub_components():
    """ports.json-style description of nlv_rule_stub.v."""
    widths = dict(ID_W=2, ADDR_W=16, DATA_W=32, STRB_W=4, LEN_W=8, LOCK_W=1, QOS_W=4, HRESP_W=2)
    ports = [dict(name="clk", width=1, input=True, endpoint="domain:main", signal="clock"),
             dict(name="rst_n", width=1, input=True, endpoint="domain:main", signal="reset_n")]

    def add(prefix, endpoint, table, skip=()):
        for name, bfm_dir, width in table:
            if name in skip:
                continue
            w = widths[width] if isinstance(width, str) else width
            ports.append(dict(name=f"{prefix}_{name}", width=w, input=bfm_dir == "o",
                              endpoint=endpoint, signal=name))

    add("m", "m", benchgen.AXI_MASTER, skip=("awregion", "arregion", "wid", "buser", "ruser"))
    add("t", "t", benchgen.AXI_SLAVE, skip=("awregion", "arregion", "wid", "buser", "ruser"))
    ports.append(dict(name="sb_status", width=1, input=False, endpoint="status",
                      signal="timed_out"))
    add("p", "p", benchgen.APB_SLAVE)
    add("h", "h", benchgen.AHB_SLAVE)
    for name, width in (("valid", 1), ("ready", 1), ("data", 8)):
        ports.append(dict(name=f"x_{name}", width=width, input=False, endpoint="x", signal=name))
    return [dict(label="stub", module="nlv_rule_stub", ports=ports)]


CASE = dict(
    name="rulecheck",
    bfms=[dict(name="s0", kind="axi_master", component="stub", endpoint="m"),
          dict(name="t0", kind="axi_slave", component="stub", endpoint="t", mem_bytes=4096,
               sideband=("stub", "status", "timed_out")),
          dict(name="p0", kind="apb_slave", component="stub", endpoint="p", mem_bytes=4096),
          dict(name="h0", kind="ahb_slave", component="stub", endpoint="h", mem_bytes=4096)],
    streams=[dict(name="x0", component="stub", endpoint="x")],
    model=dict(routes={"s0": [("t0", 0, 0x10000)]},
               targets={"t0": dict(kind="axi", bus_bytes=4)}),
    # The stub drives the APB and AHB buses itself: online rules only.
    unjudged_targets=("p0", "h0"),
    compile_timeout=300,
    run_timeout=60,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stub", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--only", default="")
    from nlverif import run as nlrun  # noqa: PLC0415  (imports the simulation engines)
    nlrun.sim_engine.add_arguments(p)
    args = p.parse_args()
    only = set(filter(None, args.only.split(",")))
    missing = only - {name for name, _, _, _ in SCENARIOS}
    if missing:
        p.error("Unknown rule scenario: " + ", ".join(sorted(missing)))
    top = Path(args.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    (top / "latest.txt").write_text(str(work) + "\n")
    results = dict(schema=1, case="rulecheck", scenarios=[], work=str(work))

    def save(code):
        results["exit"] = code
        text = json.dumps(results, indent=1, default=str) + "\n"
        (work / "results.json").write_text(text)
        (top / "results.json").write_text(text)
        return code

    try:
        tb_text, bench_meta = benchgen.generate(CASE, stub_components())
        (work / "bench.json").write_text(json.dumps(bench_meta, indent=1) + "\n")
        build = nlrun.Build(work / "build", tb_text, Path(args.stub).read_text(), args, CASE)
        results["engine"] = build.engine
        results["compile_seconds"] = round(build.compile_seconds, 3)
    except Exception:  # bench or compile
        results["error"] = traceback.format_exc()
        return save(2)
    failed = False
    for name, mode, builder, rule in SCENARIOS:
        if only and name not in only:
            continue
        entry = dict(scenario=name, mode=mode, expect=rule or "PASS")
        results["scenarios"].append(entry)
        if rule in C.FOUR_STATE_RULES and build.engine == "verilator":
            entry["status"] = "NOT_RUN"
            entry["detail"] = "two-state engine cannot drive X"
            continue
        tape = Tape(bench_meta["bfms"], max_cycles=2000)
        builder(tape)
        text = tape.assemble()
        if rule == "TB.TAPE":
            text = "%016x" % 0 + text[16:]
        out = work / name
        out.mkdir()
        (out / "tape.hex").write_text(text)
        tape_meta = json.loads(json.dumps(tape.metadata()))
        try:
            res = nlrun.evaluate(build.run(out, out / "tape.hex", [f"+NLV_STUB={MODES[mode]}"]),
                                 CASE, bench_meta, tape_meta)
        except Exception:
            res = dict(status="TOOL_ERROR", detail=traceback.format_exc()[-2000:])
        verdict = res.get("verdict", {})
        entry["first_rule"] = verdict.get("first_rule")
        entry["first_port"] = verdict.get("first_port")
        entry["result"] = res.get("status")
        if res.get("detail"):
            entry["detail"] = res["detail"]
        if rule is None:
            ok = res.get("status") == "PASS"
            entry["mismatches"] = res.get("check", {}).get("mismatches", [])[:5]
        else:
            ok = (res.get("status") != "TOOL_ERROR" and verdict.get("status") == "FAIL"
                  and verdict.get("first_rule") == rule)
        entry["status"] = "PASS" if ok else "FAIL"
        failed = failed or not ok
        print(f"RULECHECK {entry['status']} {name} expect={entry['expect']} "
              f"first={entry['first_rule']} result={entry['result']}")
    return save(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
