"""The target-side upsize merge behind an AXI4 front of the target width
(narrow bursts in the target layout, as the width adapters deliver them)
and an outlet of that width (fixture recipe axi4_upsize), one registration
per target width (--param data_bits=32..1024).

  cases     every case of the scenario (INCR, bytes, unaligned, half start,
            small and shifted WRAP, FIXED, full width, a full-width beat from
            mid-word, a WRAP container below the width, single beats, back
            to back, a read with a failed target word), as a modifiable and
            a Non-modifiable burst of source 0 and of source 1 (two
            configurations): the target bursts must be the oracle's
            (axi4_upsize_bench.plan), every strobed byte once, read data
            fanned out to each source beat;
  context   (--param mode=context, 128 bits) fifteen, two and seven merge
            contexts per direction, with the front taking one burst more of
            its shared read/write pool (freed on downstream accept) and the
            outlet one burst more of its shared 16-entry pool, so the merge is
            the bound: per direction for two and seven (both fill at once),
            for one direction for fifteen (writes fill, drain, then reads
            fill, so the sixteenth burst of a faulty merge fits the outlet):
            writes of two sources (address bit 12) whose B the target returns
            youngest first, reads of one source; the contexts fill while the
            target holds its responses and no further burst reaches the
            target (W_CAPACITY, R_CAPACITY); a read tail held at
            the source keeps its context and the shared gather (R_EARLY_POP,
            the later reads' data intact); a common reset with full contexts,
            then an exact access (fifteen: full write contexts and one read).
            The fault variant merge_contexts_plus_one (sixteen contexts at the
            main fifteen) shows that the capacity checks fail. The inline
            fault early_tail_pop takes a new read request the cycle the
            previous read's last response beat is merely presented, not
            accepted; the early-taken request wedges inside the merge (no AR
            reaches the target, so the black-box R_EARLY_POP count cannot see
            it) and the held read never completes, which the drain timeout
            and the unanswered read catch.

The fault cases use IR faults and a variant of the merge."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import axi4_upsize_bench as oracle  # noqa: E402

PARAMS = globals().get("PARAMS", {})
DATA = int(PARAMS.get("data_bits", "128"))
MODE = PARAMS.get("mode", "cases")
if DATA not in (32, 128, 256, 512, 1024):
    raise ValueError(f"data_bits {DATA} not in 32, 128..1024")
if MODE not in ("cases", "context") or (MODE == "context" and DATA != 128):
    raise ValueError(f"mode {MODE} (context runs at 128 bits only)")
TBYTES = DATA // 8


def recipe(source, capacity=1):
    params = dict(data_bits=str(DATA), source=str(source))
    if capacity > 1:
        params["max_outstanding"] = str(capacity)
    return dict(source="fixture", recipe="axi4_upsize", params=params)


MODEL = dict(routes={"s0": [("t0", 0, 1 << 32)]}, error_codes=True,
             targets={"t0": dict(kind="axi", bus_bytes=TBYTES, cache="amba", prot=True)})
NETS = {"front": [("nl_axi_", "s_"), ("nl_", "u_")],
        "shim": [("nl_up_", "u_"), ("nl_down_", "v_")],
        "merge": [("nl_up_", "v_"), ("nl_down_", "d_"), ("nl_hint_", "d_hint_")],
        "target": [("nl_axi_", "t_"), ("nl_", "d_")]}

CONFIGS = [dict(label=""),
           dict(label="source1", dut=recipe(1), scenario_args=dict(source=1))]
MERGE_FAULTS = [
    dict(id="merge_strobes_lost", component="merge", op="set_driver",
         port="nl_down_wreq_be", **{"with": {"const": 0}},
         expect=["TARGET_WRITE_BYTES", "TARGET_UNSTROBED_DATA"],
         contract="AXI.UPSIZE.MERGE", note="merge capacity and tail retention"),
    dict(id="merge_length_zero", component="merge", op="set_driver",
         port="nl_down_wreq_length", **{"with": {"const": 0}},
         expect=["TARGET_DESCRIPTOR", "AXI.W.LAST", "TARGET_WRITE_BYTES",
                 "TARGET_FOOTPRINT", "TAPE.WAIT_TIMEOUT:CASE_DONE"],
         contract="AXI.UPSIZE.MERGE", note="merge capacity and tail retention"),
    dict(id="merge_read_data_zero", component="merge", op="set_driver",
         port="nl_up_rrsp_data", **{"with": {"const": 0}},
         expect=["SOURCE_R_DATA"], contract="AXI.UPSIZE.MERGE",
         note="merge capacity and tail retention"),
]
if MODE == "context":
    CONFIGS = [dict(label=f"context{k}", dut=recipe(1, k), scenario_args=dict(capacity=k))
               for k in (15, 2, 3, 4, 7)]
    for k in (2, 3, 4, 7, 15):
        dut = recipe(1, k)
        dut["params"]["joined"] = "1"
        CONFIGS.append(dict(label=f"replay{k}", dut=dut,
                            scenario_args=dict(capacity=k, replay_only=True), faults=[]))
    MERGE_FAULTS = [
        dict(id="merge_contexts_plus_one", component="merge", op="variant",
             params=dict(merge_outstanding="16"),
             expect=["TAPE.EXPECT:W_CAPACITY", "TAPE.EXPECT:R_CAPACITY", "TAPE.EXPECT:REPLAY_CAPACITY"],
             contract="AXI.UPSIZE.MERGE",
             note="one context more than the stated capacity"),
        dict(id="early_tail_pop", component="merge", op="set_driver",
             port="nl_up_rreq_ready",
             **{"with": {"op": "BitOr", "args": [
                 {"self": 1},
                 {"op": "BitAnd", "args": [{"select": {"driver": "nl_up_rrsp_valid"}},
                                        {"select": {"driver": "nl_up_rrsp_last"}}]}]}},
             expect=["TAPE.DRAIN_TIMEOUT:DRAIN", "SOURCE_UNANSWERED"],
             contract="AXI.UPSIZE.MERGE",
             note="a read context freed while its "
                  "tail is still held at the source; the early-taken request wedges "
                  "inside the merge (no AR reaches the target) and the held read "
                  "never completes"),
        MERGE_FAULTS[2],
    ]

CASE = dict(
    name=f"axi4_upsize_{DATA}" + ("_context" if MODE == "context" else ""),
    contracts=["AXI.UPSIZE.MERGE", "AXI.OUTSTANDING.REACH", "AXI.RESET.CANCEL",
               "AXI.ERR.READ_ZERO"],
    dut=recipe(0),
    nets=NETS,
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="axi_slave", component="target", endpoint="axi", salt=9)],
    model=MODEL,
    configs=CONFIGS,
    negative_runs_all=True,
    max_cycles=2000000,
    run_timeout=600,
    faults=MERGE_FAULTS,
)


def shape(c):
    return dict(beats=c["length"] + 1, size=c["size"],
                burst=("fixed", "incr", "wrap")[c["burst"]], cache=c.get("cache", 0))


def scenario(t, seed, source=0, capacity=1, replay_only=False):
    t.ready("t0", "aw", "modulo", 3, 1)
    t.ready("t0", "w", "modulo", 4, 1)
    t.ready("t0", "ar", "modulo", 5, 2)
    t.reset(4)
    t.delay(2)
    if replay_only:
        replay_contexts(t, capacity)
        t.end()
        return
    if capacity > 1:
        return context(t, capacity)
    for c in oracle.cases(TBYTES):
        parts = [c] + ([c["then"]] if c.get("then") else [])
        # A beat wider than the target never reaches the merge through an
        # AXI face of the target width.
        if c.get("source", 0) != source or max(1 << p["size"] for p in parts) > TBYTES:
            continue
        t.mark("case", 0)
        m = {p: t.snap("s0", p) for p in ("b", "rlast")}
        bad = c.get("error_word")
        if bad is not None:
            t.set("t0", "reg0_lo", bad)
            t.set("t0", "reg0_hi", bad + TBYTES)
            t.set("t0", "reg0_r", 2)
        for part in parts:
            _, start, length, size, burst = oracle.plan(dict(part, length=part["length"]),
                                                        TBYTES, bool(c["write"]))
            exp = dict(pieces=[[start, length, size, burst]])
            if c["write"] and part.get("zero_beat") is not None:
                # A beat without strobes (w-zero-beat): the strobe table
                # gives that beat an empty lane mask.
                t.strobes("s0", [0 if k == part["zero_beat"] else 0xFFFFFFFF
                                 for k in range(part["length"] + 1)])
                t.write("s0", part["address"], id=5, prot=5, strobe="table", **shape(part),
                        **exp)
            elif c["write"]:
                t.write("s0", part["address"], id=5, prot=5, **shape(part), **exp)
            else:
                t.read("s0", part["address"], id=6, prot=3, **shape(part), **exp)
        counter = "b" if c["write"] else "rlast"
        t.wait("s0", counter, "==", len(parts), since=m[counter], limit=20000, label="CASE_DONE")
        if bad is not None:
            t.set("t0", "reg0_hi", 0)
    t.end()


def shapes(k, base, step, second):
    """The scenario's packet mix in turn: INCR, narrow INCR, an unaligned
    WRAP of eight beats and a FIXED burst; second(j) puts packet j at the
    second source (address bit 12)."""
    out = []
    for j in range(k):
        kind = j % 4
        address = base + j * step + (0x1000 if second(j) else 0) + (4 if kind == 2 else 0)
        out.append((address, dict(beats=8 if kind == 2 else 4, size=1 if kind == 1 else 2,
                                  burst="wrap" if kind == 2 else "fixed" if kind == 3 else "incr")))
    return out


def context(t, k):
    """Exercise the merge's context bookkeeping: a capacity fill with held
    responses, then a read tail held at the source, then a reset. Above
    seven the outlet's pool holds k + 1 bursts of one direction only, so the
    directions fill in turn."""
    split = k > 7
    t.mark("phase", 1)
    t.gate("t0", "b", False)
    t.gate("t0", "r", False)
    # B youngest first: the merge matches each B of either source to the
    # oldest context of that source.
    t.set("t0", "b_order", 1)
    writes = shapes(k + 1, 0x100, 0x80, lambda j: j % 2)
    reads = shapes(k + 1, 0x2100, 0x80, lambda j: True)
    # The last write that fits answers SLVERR; the last read that fits
    # reads a failed word.
    t.set("t0", "reg0_lo", writes[k - 1][0] // 16 * 16)
    t.set("t0", "reg0_hi", writes[k - 1][0] // 16 * 16 + 16)
    t.set("t0", "reg0_w", 2)
    t.set("t0", "reg1_lo", reads[k - 1][0] // 16 * 16)
    t.set("t0", "reg1_hi", reads[k - 1][0] // 16 * 16 + 16)
    t.set("t0", "reg1_r", 2)
    # ">=": one context more than stated lets the last burst through, and
    # the exact count stays the expect_now below.
    if split:
        m = t.snap("t0", "aw")
        for j, (wa, ws) in enumerate(writes):
            t.write("s0", wa, id=4 + j % 2, cache=2, prot=5, **ws)
        t.wait("t0", "aw", ">=", k, since=m, limit=20000, label="W_FULL")
        t.delay(12)
        t.expect_now("t0", "aw", "==", k, "W_CAPACITY", since=m)
        t.gate("t0", "b", True)
        t.drain(limit=20000, label="DRAIN")
        m = t.snap("t0", "ar")
        for ra, rs in reads:
            t.read("s0", ra, id=6, cache=0, prot=3, **rs)
        t.wait("t0", "ar", ">=", k, since=m, limit=20000, label="R_FULL")
        t.delay(12)
        t.expect_now("t0", "ar", "==", k, "R_CAPACITY", since=m)
    else:
        m = {p: t.snap("t0", p) for p in ("aw", "ar")}
        for j, ((wa, ws), (ra, rs)) in enumerate(zip(writes, reads)):
            t.write("s0", wa, id=4 + j % 2, cache=2, prot=5, **ws)
            t.read("s0", ra, id=6, cache=0, prot=3, **rs)
        t.wait("t0", "aw", ">=", k, since=m["aw"], limit=20000, label="W_FULL")
        t.wait("t0", "ar", ">=", k, since=m["ar"], limit=20000, label="R_FULL")
        t.delay(12)
        t.expect_now("t0", "aw", "==", k, "W_CAPACITY", since=m["aw"])
        t.expect_now("t0", "ar", "==", k, "R_CAPACITY", since=m["ar"])
    t.gate("t0", "b", True)
    t.gate("t0", "r", True)
    t.drain(limit=20000, label="DRAIN")
    t.set("t0", "b_order", 0)
    t.set("t0", "reg0_hi", 0)
    t.set("t0", "reg1_hi", 0)
    # A read tail held at the source: the front stages one beat, so after
    # two taken beats the merge offers the fourth (the tail); it must keep
    # that context and the shared gather while the later reads' words wait.
    t.mark("phase", 2)
    t.gate("t0", "r", False)
    m = {p: t.snap("t0", p) for p in ("ar", "r")}
    for j in range(k + 1):
        t.read("s0", 0x3100 + j * 0x80, beats=4, size=2, id=6, prot=3)
    t.wait("t0", "ar", ">=", k, since=m["ar"], limit=20000, label="R_FULL")
    t.ready("s0", "r", "beats", 2)
    t.gate("t0", "r", True)
    t.wait("t0", "r", ">=", 1, since=m["r"], limit=20000, label="TAIL_READY")
    t.delay(40)
    t.expect_now("t0", "ar", "==", k, "R_EARLY_POP", since=m["ar"])
    t.ready("s0", "r", "always")
    t.drain(limit=20000, label="DRAIN")
    # A common reset with full contexts (above seven: full write contexts
    # and the one read the outlet's pool leaves), then one exact access.
    t.mark("phase", 3)
    t.gate("t0", "b", False)
    t.gate("t0", "r", False)
    m = {p: t.snap("t0", p) for p in ("aw", "ar")}
    reads = 1 if split else k
    for j in range(k):
        t.write("s0", 0x4100 + j * 0x80 + (0x1000 if j % 2 else 0), beats=4, size=2,
                id=4 + j % 2, cache=2, prot=5)
        if j < reads:
            t.read("s0", 0x7100 + j * 0x80, beats=4, size=2, id=6, prot=3)
    t.wait("t0", "aw", "==", k, since=m["aw"], limit=20000, label="RESET_NONEMPTY")
    t.wait("t0", "ar", "==", reads, since=m["ar"], limit=20000, label="RESET_NONEMPTY")
    t.reset(4)
    t.gate("t0", "b", True)
    t.gate("t0", "r", True)
    t.delay(2)
    t.write("s0", 0x10c, beats=1, size=2, id=4, prot=5)
    t.read("s0", 0x110c, beats=1, size=2, id=6, prot=3)
    t.drain(limit=20000, label="RESET_RECOVERY")
    t.check(kind="reach", port="s0", dir=["w", "r"], inflight=k)
    t.end()


def replay_contexts(t, k):
    """Every descriptor can retain a WRAP start word, including distinct keys."""
    for distinct in (False, True):
        t.reset(4)
        t.gate("t0", "r", False)
        t.ready("s0", "r", "never")
        t.set("t0", "r_order", 1 if distinct else 0)
        t.set("t0", "r_ilv", 1 if distinct else 0)
        count = t.snap("t0", "ar")
        for j in range(k + 1):
            t.read("s0", 0x4004 + j * 0x80, beats=16, size=2, burst="wrap",
                   id=j % 4 if distinct else 6, cache=2, prot=3)
        t.wait("t0", "ar", ">=", k, since=count, limit=20000, label="REPLAY_ALL_K")
        t.delay(12)
        t.expect_now("t0", "ar", "==", k, "REPLAY_CAPACITY", since=count)
        t.gate("t0", "r", True)
        t.delay(30)
        t.expect_now("t0", "ar", "==", k, "REPLAY_TAIL_HOLD", since=count)
        t.ready("s0", "r", "modulo", 4, 0)
        t.drain(limit=40000, label="REPLAY_DRAIN")
    t.set("t0", "r_order", 0)
    t.set("t0", "r_ilv", 0)
    t.reset(4)
    t.ready("s0", "r", "always")
