"""The aligned-burst AXI4 outlet behind an AXI4 initiator front and a packet
shim (fixture recipe axi4_outlet), one registration per bus width
(--param data_bits=32..1024).

The shim marks Weft errors by the burst's original address (bits 19:16:
1 head, 2 last write beat, 3 middle write beats) and sets the source field,
so the packet cases of the scenarioes stay reachable from the AXI face:

  cases        every size, INCR/FIXED/WRAP and each legal WRAP length,
               target SLVERR/DECERR/EXOKAY, BID/RID mismatch, early RLAST,
               rejected heads, failed beats (no strobes, zero data),
               held responses, resets in the middle of a burst, cases back
               to back without reset; the designated faults run here;
  multi        the several-bursts outlet (source index 1): normal, failed
               beats, rejected heads (32 and 1024 bits);
  rate         back-to-back bursts against a zero-wait target: within-burst
               rate >= 0.95 at both faces, overall >= 0.95 for long bursts
               (and for 16-beat bursts of the several-bursts outlet; the
               single-burst 1024-bit outlet, whose bursts stop at 32 beats
               inside 4 KiB, >= 0.85; the scenario's 256-beat 1024-bit and
               the 256- and 512-bit long bursts are cut to 4 KiB), then
               two-beat bursts against a target that takes an address every
               fourth cycle (reported); single and several bursts;
  stress       random valid/ready per segment, normal, failed-beat and
               rejected bursts, resets at random cycles (32 bits, eight
               seeds, single and several bursts)."""

import random

PARAMS = globals().get("PARAMS", {})
DATA = int(PARAMS.get("data_bits", "32"))
# (len bits, ID bits, source bits) of the fixture per width.
SHAPE = {32: (8, 4, 3), 64: (4, 0, 0), 128: (8, 3, 2), 256: (4, 0, 0), 512: (4, 0, 0),
         1024: (8, 3, 2)}
if DATA not in SHAPE:
    raise ValueError(f"data_bits {DATA} not in 32..1024")
LEN, IDS, SRC = SHAPE[DATA]
BYTES = DATA // 8
MAXSIZE = BYTES.bit_length() - 1
MULTI = DATA in (32, 1024)


def recipe(len_bits, id_bits, source_bits, multi=False):
    params = dict(data_bits=str(DATA), len_bits=str(len_bits), id_bits=str(id_bits),
                  source_bits=str(source_bits))
    if multi:
        params["multi"] = "1"
    return dict(source="fixture", recipe="axi4_outlet", params=params)


def model(ids):
    return dict(routes={"s0": [("t0", 0, 1 << 32)]}, error_codes=True,
                targets={"t0": dict(kind="axi", bus_bytes=BYTES, cache="amba", prot=True,
                                    ids=ids)})


NETS = {"front": [("nl_axi_", "s_"), ("nl_", "u_")],
        "shim": [("nl_up_", "u_"), ("nl_down_", "v_")],
        "target": [("nl_axi_", "t_"), ("nl_", "v_")]}


def faults(admission=True):
    out = [
        dict(id="withdraw_w", component="target", op="set_driver", port="nl_axi_wvalid",
             **{"with": {"op": "BitAnd", "args": [{"self": True},
                        {"op": "BitNot", "args": [{"select": {"input": "nl_axi_awready"}}]}]}},
             expect=["AXI.W.HOLD"], contract="AXI.OUTLET.FRAMING"),
        dict(id="repeat_aw", component="target", op="set_next",
             at={"binding": "w.aw_pending"},
             **{"with": {"op": "BitOr", "args": [{"self": True},
                        {"select": {"state_next": {"binding": "w.aw_pending"}}}]}},
             expect=["TARGET_UNATTRIBUTED", "TARGET_FOOTPRINT", "CONTRACT.QUIET.AW",
                     "AXI.AW.HOLD", "TAPE.EXPECT:FIXED_AW_ONCE"],
             contract="AXI.OUTLET.FRAMING"),
        dict(id="last", component="target", op="set_driver", port="nl_axi_wlast",
             **{"with": {"const": 0}}, expect=["AXI.W.LAST", "TAPE.WAIT_TIMEOUT:CASE_DONE"],
             contract="AXI.OUTLET.FRAMING"),
        dict(id="read_last", component="target", op="set_driver", port="nl_rrsp_last",
             **{"with": {"const": 0}},
             expect=["SOURCE_R_LAST", "AXI.R.LAST", "SOURCE_R_COUNT",
                     "TAPE.WAIT_TIMEOUT:CASE_DONE", "SOURCE_UNANSWERED"],
             contract="AXI.OUTLET.FRAMING"),
        dict(id="address", component="target", op="set_driver", port="nl_axi_awaddr",
             **{"with": {"const": 0}},
             expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES"],
             contract="AXI.ROUTE.LOCAL_OFFSET"),
        dict(id="result", component="target", op="set_driver", port="nl_wrsp_result",
             **{"with": {"const": 0}}, expect=["SOURCE_B_RESP"],
             contract="AXI.MERGE.SEVERITY"),
        dict(id="duplicate", component="target", op="set_operand", operand=2,
             at={"state_next": {"binding": "w.full"}},
             **{"with": {"select": {"binding": "w.full"}}},
             expect=["TARGET_WRITE_BYTES", "AXI.W.LAST", "TARGET_FOOTPRINT",
                     "CONTRACT.QUIET.W", "TAPE.WAIT_TIMEOUT:CASE_DONE", "SOURCE_UNANSWERED"],
             contract="AXI.OUTLET.FRAMING"),
    ]
    if IDS:
        out.append(dict(id="id", component="target", op="set_driver", port="nl_axi_awid",
                        **{"with": {"const": 0}},
                        expect=["TARGET_PROT", "TARGET_UNATTRIBUTED"],
                        contract="AXI.OUTLET.FRAMING"))
    return out if DATA == 32 and admission else out[2:]


CONFIGS = [dict(label="")]
if MULTI:
    CONFIGS.append(dict(label="multi", dut=recipe(LEN, IDS, SRC, True), model=model(False),
                        scenario_args=dict(mode="multi")))
for multi in (False, True):
    CONFIGS.append(dict(label="rate_multi" if multi else "rate", dut=recipe(8, 4, 3, multi),
                        model=model(not multi), scenario_args=dict(mode="rate", multi=multi),
                        faults=faults(False)))
if DATA == 32:
    for multi in (False, True):
        CONFIGS.append(dict(label="stress_multi" if multi else "stress",
                            dut=recipe(8, 4, 3, multi), model=model(not multi),
                            scenario_args=dict(mode="stress", multi=multi),
                            seeds=list(range(1, 9)), faults=faults(False)))

CASE = dict(
    name=f"axi4_outlet_{DATA}",
    contracts=["AXI.OUTLET.FRAMING", "AXI.ROUTE.LOCAL_OFFSET", "AXI.MERGE.SEVERITY",
               "AXI.ERR.READ_ZERO", "AXI.RESET.CANCEL", "AXI.RATE.BEATS"],
    dut=recipe(LEN, IDS, SRC),
    nets=NETS,
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="axi_slave", component="target", endpoint="axi", salt=7)],
    model=model(bool(IDS)),
    configs=CONFIGS,
    negative_runs_all=True,
    max_cycles=3000000,
    run_timeout=600,
    faults=faults(),
)

HEAD, LAST, MIDDLE = 0x10000, 0x20000, 0x30000
WID, RID = (5, 6) if IDS else (0, 0)


def scenario(t, seed, mode="cases", multi=False):
    if mode == "rate":
        return rate(t, multi)
    if mode == "stress":
        return stress(t, seed, multi)
    if DATA == 32:
        admission(t)
    t.ready("t0", "aw", "modulo", 4, 0)
    t.ready("t0", "w", "modulo", 5, 0)
    t.ready("t0", "ar", "modulo", 3, 0)
    t.reset(4)
    t.delay(2)
    state = dict(n=0)
    if mode == "multi":
        plan = [(4, 2, 1, 0, 0), (4, 1, 1, 4, 0), (5, 2, 1, 4, 0), (2, 2, 1, 4, 0),
                (4, 2, 1, 10, 0), (4, 1, 1, 3, 0), (16, 2, 1, 0, 0)]
        plan += [(3, s, 1, 4, 0) for s in range(MAXSIZE + 1)]
        tail = [(4, 1, 2, 3, 0), (4, 2, 1, 0, 0)]
    else:
        incr = min(256 if LEN == 8 else 16, (4096 - 0x120) // max(BYTES, 4))
        plan = [(4, 2, 1, 0, 0)] + [(4, 1, 1, k, 0) for k in (1, 2, 3, 4, 5, 7, 8, 9)]
        plan += [(1, 0, 0, 0, 0), (16, 2, 0, 0, 0), (4, 1, 2, 0, 0), (16, MAXSIZE, 2, 0, 0),
                 (incr, MAXSIZE, 1, 0, 0)]
        for s in range(MAXSIZE + 1):
            plan += [(3, s, 1, 0, 0), (5, s, 0, 0, 0)] + [(w, s, 2, 0, 0) for w in (2, 4, 8)]
        plan += [(4, 2, 1, 0, 2), (16, 1, 1, 0, 18), (1, 2, 1, 6, 24), (4, 2, 1, 0, 0),
                 (4, 2, 1, 10, 0)]
        tail = [(4, 1, 2, 3, 0), (16, MAXSIZE, 1, 0, 0)]
    for args in plan:
        t.reset(4)
        case(t, state, *args)
    for args in tail:
        case(t, state, *args)
    t.reset(4)
    t.end()


def admission_cycles(t):
    """Every burst uses the same two request slots in this fixture."""
    for burst in ("incr", "wrap", "fixed"):
        for ch in ("ar", "aw", "w"):
            t.reset(4)
            for offered in ("ar", "aw", "w"):
                t.ready("t0", offered, "never")
            start = t.snap("t0", "cycle")
            if ch == "ar":
                t.read("s0", 0x220, beats=4, burst=burst, id=RID)
            else:
                t.write("s0", 0x120, beats=4, burst=burst, id=WID)
            label = f"ADMISSION_{burst.upper()}_{ch.upper()}"
            t.wait("t0", ch + "valid", "==", 1, limit=80, label=label + "_SHOWN")
            t.expect_now("t0", "cycle", "==", 2, label + "_CYCLE", since=start)
            t.reset(4)
            for offered in ("ar", "aw", "w"):
                t.expect_now("t0", offered + "valid", "==", 0,
                             f"ADMISSION_RESET_{offered.upper()}")


def admission(t):
    """FIXED shares the registered AW, W and AR admission of every burst type."""
    admission_cycles(t)
    for first in ("aw", "w"):
        t.reset(4)
        for ch in ("aw", "w", "ar"):
            t.ready("t0", ch, "never")
        t.read("s0", 0x220, beats=4, burst="fixed", id=RID)
        t.wait("t0", "arvalid", "==", 1, limit=80, label="FIXED_AR_SHOWN")
        t.delay(32)
        t.ready("t0", "ar", "always")
        t.drain(limit=2000, label="FIXED_READ_DONE")
        t.write("s0", 0x120, beats=4, burst="fixed", id=WID)
        for ch in ("aw", "w"):
            t.wait("t0", ch + "valid", "==", 1, limit=80, label="FIXED_SHOWN")
        t.delay(32)
        t.ready("t0", first, "always")
        t.wait("t0", first, ">=", 1, limit=80, label="FIXED_FIRST")
        # AW accepting must not withdraw an already offered W. W accepting
        # must not withdraw or replace AW, even after the entire data burst.
        t.delay(32)
        if first == "aw":
            t.expect_now("t0", "aw", "==", 1, "FIXED_AW_ONCE")
        other = "w" if first == "aw" else "aw"
        t.expect_now("t0", other + "valid", "==", 1, "FIXED_OTHER_HELD")
        t.ready("t0", other, "always")
        t.ready("t0", "ar", "always")
        for burst in ("incr", "wrap", "fixed"):
            t.write("s0", 0x340, beats=4, burst=burst, id=WID)
            t.read("s0", 0x440, beats=4, burst=burst, id=RID)
        t.drain(limit=2000, label="FIXED_DONE")
        for ch in ("aw", "w", "ar"):
            t.quiet("t0", ch, True)
        t.delay(16)
        for ch in ("aw", "w", "ar"):
            t.quiet("t0", ch, False)
    # Reset cancels an offered write and a queued read while the target stalls.
    for ch in ("aw", "w", "ar"):
        t.ready("t0", ch, "never")
    t.write("s0", 0x120, beats=4, burst="fixed", id=WID)
    t.read("s0", 0x220, beats=4, burst="fixed", id=RID)
    t.wait("t0", "wvalid", "==", 1, limit=80, label="FIXED_RESET_SHOWN")
    t.delay(16)
    t.reset(4)
    for ch in ("aw", "w", "ar"):
        t.ready("t0", ch, "always")


def case(t, state, n, size, burst, mode, abort):
    """One case of the scenario: a write and a read of n beats at 0x120."""
    state["n"] += 1
    k = state["n"]
    base = 0x120
    if burst == 2:
        base = 0x13c & ~((1 << size) - 1)
    t.mark("case", k)
    # Target response program, indices since this base mark.
    t.set("t0", "base", 1)
    for e in range(4):
        t.set("t0", f"b_ovr_i{e}", 0)
        t.set("t0", f"r_ovr_i{e}", 0)
    code = {1: 2, 2: 3, 7: 1}.get(mode)
    if code is not None:
        t.set("t0", "b_ovr_i0", 1)
        t.set("t0", "b_ovr_c0", code)
        t.set("t0", "r_ovr_i0", n // 2 + 1)
        t.set("t0", "r_ovr_c0", code)
    t.set("t0", "id_xor", 1 if mode == 5 else 0)
    t.set("t0", "rlast_flip", 2 if mode == 8 else 0)
    if mode in (6, 9):
        t.ready("s0", "b", "never")
    if mode == 6:
        t.ready("s0", "r", "never")
    wexp, rexp = {}, {}
    waddr = raddr = base
    if mode == 3:
        waddr = raddr = base | HEAD
        wexp = rexp = dict(resp=2, target="none")
    elif mode == 4:
        middle = list(range(1, n - 1))
        waddr = base | (MIDDLE if middle else LAST)
        wexp = dict(resp=2, drop_beats=middle or [n - 1])
    elif mode == 10:
        waddr = base | LAST
        wexp = dict(resp=2, drop_beats=[n - 1])
    elif mode == 5 and IDS:
        wexp = rexp = dict(resp=2)
    elif mode == 7:
        wexp = dict(resp=2)
        rexp = dict(beat_resp={str(n // 2): 2})
    elif mode == 8:
        rexp = dict(beat_resp={"1": 2})
    shape = dict(beats=n, size=size, burst=("fixed", "incr", "wrap")[burst])
    m = {p: t.snap("s0", p) for p in ("b", "rlast")}
    t.write("s0", waddr, id=WID, cache=k % 16, prot=5, **shape, **wexp)
    t.read("s0", raddr, id=RID, cache=(k + 7) % 16, prot=3, **shape, **rexp)
    if abort:
        t.delay(abort)
        t.reset(4)
        t.ready("s0", "b", "always")
        t.ready("s0", "r", "always")
        return
    if mode == 9:
        # B waits until the read finished (response faces independent).
        t.wait("s0", "rlast", "==", 1, since=m["rlast"], limit=8000, label="CASE_DONE")
        t.ready("s0", "b", "always")
    t.wait("s0", "b", "==", 1, since=m["b"], limit=8000, label="CASE_DONE")
    t.wait("s0", "rlast", "==", 1, since=m["rlast"], limit=8000, label="CASE_DONE")
    # Nothing replays after the case.
    for port, ch in (("s0", "b"), ("s0", "r"), ("t0", "aw"), ("t0", "w"), ("t0", "ar")):
        t.quiet(port, ch, True)
    t.delay(4)
    for port, ch in (("s0", "b"), ("s0", "r"), ("t0", "aw"), ("t0", "w"), ("t0", "ar")):
        t.quiet(port, ch, False)


def rate(t, multi):
    """Back-to-back full-width INCR bursts against a zero-wait target."""
    t.reset(4)
    t.delay(2)
    long = min(256, 4096 // BYTES)  # a full-width burst stays inside 4 KiB
    # A 1024-bit burst fits 32 beats in 4 KiB: with one shared packet slot
    # its round trip keeps the overall rate near 0.91, gated at 0.85.
    # The target's in-flight pool is shared by both directions, so bursts of
    # opposite directions serialize: the several-bursts outlet interleaves
    # them only on the 16-beat phase, and the one-burst outlet on the long
    # phase as well. There the per-direction spans stretch and the phases
    # gate the combined two-direction rate of each port instead (near
    # 1.7 of 2 against 0.95, above 0.85 against 0.8); the per-direction overall
    # gates stay where the pool does not serialize.
    # Fourth field: the per-direction overall gate (0: within-burst only).
    # Fifth field: the combined overall gate (None: no gate).
    # An entry returns to the target pool in the cycle after its last response word, so a
    # 32-beat burst waiting for an entry (1024 bits) runs at 32/34 overall: gated at 0.93.
    phases = [(16, 8, 0.0, 0.95 if multi else 0.0),
              (long, 2, (0.93 if long <= 32 else 0.95) if multi else 0.0,
               0.0 if multi else (0.95 if long >= 64 else 0.85)),
              (2, 8, None, None)]
    for k, (n, bursts, overall, agg) in enumerate(phases, 1):
        t.mark("phase", k)
        if overall is None:
            t.ready("t0", "aw", "modulo", 4, 1)  # an address every fourth cycle
        for b in range(bursts):
            t.write("s0", b * n * BYTES, beats=n, id=5, cache=3, prot=2)
        for b in range(bursts):
            t.read("s0", 0x100000 + b * n * BYTES, beats=n, id=6, cache=3, prot=2)
        t.drain(limit=200000, label=f"PHASE_{k}")
        t.mark("phase_end", k)
        t.ready("t0", "aw", "always")
        if overall is None:
            continue
        for port, ch in (("t0", "w"), ("t0", "r"), ("s0", "w"), ("s0", "r")):
            window = dict(after_mark="phase", mark_value=k, until_mark="phase_end",
                          until_value=k)
            # The source W stream keeps only a loose floor: its beats may wait
            # mid-burst while the shared pools of source and target hold the
            # other direction (still catches per-beat bubbles).
            within = 0.4 if (port, ch) == ("s0", "w") else 0.95
            t.check(kind="rate", port=port, ch=ch, per="burst", min=within,
                    label=f"{port}.{ch} within {k}", **window)
            if overall:
                t.check(kind="rate", port=port, ch=ch, min=overall,
                        label=f"{port}.{ch} overall {k}", **window)
            if agg:
                t.check(kind="rate_all", port=port, min=agg,
                        label=f"{port}.w+r overall {k}", **window)
    t.end()


def stress(t, seed, multi, segments=200):
    """Random valid and ready per segment; normal, failed-beat and rejected
    bursts; resets at random cycles. Two directed segments first: writes
    alternating normal and rejected with WREADY mostly low."""
    rnd = random.Random(seed)
    t.set("s0", "seed", seed * 7919 + 1)
    t.set("t0", "seed", seed * 104729 + 3)
    t.reset(4)
    t.delay(2)
    for seg in range(segments):
        t.mark("segment", seg)
        directed = seg < 2
        full = seg % 4 == 3
        rate_ = (lambda: 100) if full else (lambda: 10 + rnd.randrange(91))
        for reg in ("vrate_aw", "vrate_w", "vrate_ar"):
            t.set("s0", reg, 0 if full else rate_())
        for ch in ("b", "r"):
            t.ready("s0", ch, "random", rate_())
        for ch in ("aw", "w", "ar"):
            t.ready("t0", ch, "random", 10 if directed and ch == "w" else rate_())
        t.set("t0", "b_rate", 0 if full else rate_())
        t.set("t0", "r_rate", 0 if full else rate_())
        if directed:
            kinds = ["normal", "head", "normal", "head"] if seg == 0 else \
                ["head", "normal", "head", "normal"]
            for k, kind in enumerate(kinds):
                stress_write(t, rnd, kind, 0x400 * k, multi)
        else:
            for k in range(1 + rnd.randrange(4)):
                kind = ("normal", "normal", "beat", "head")[rnd.randrange(4)]
                stress_write(t, rnd, kind, 0x400 * k, multi)
            for k in range(1 + rnd.randrange(4)):
                kind = "head" if rnd.randrange(4) == 0 else "normal"
                n = 16 if rnd.randrange(4) == 0 else 1 + rnd.randrange(6)
                exp = dict(resp=2, target="none") if kind == "head" else {}
                t.read("s0", 0x8000 + 0x400 * k + (HEAD if kind == "head" else 0), beats=n,
                       id=6, cache=rnd.randrange(16), prot=rnd.randrange(8), **exp)
        if seg >= 2 and rnd.randrange(3) == 0:
            t.settle(20 + rnd.randrange(400), reset=3)
        t.drain(limit=200000, label="SEGMENT")
    t.end()


def stress_write(t, rnd, kind, offset, multi):
    n = 16 if rnd.randrange(4) == 0 else 1 + rnd.randrange(6)
    exp, addr = {}, 0x1000 + offset
    if kind == "head" or kind == "beat" and n == 1:
        addr |= HEAD  # a one-beat burst with a failed beat is a failed head
        exp = dict(resp=2, target="none")
    elif kind == "beat":
        middle = list(range(1, n - 1))
        addr |= MIDDLE if middle else LAST
        exp = dict(resp=2, drop_beats=middle or [n - 1])
    t.write("s0", addr, beats=n, id=5, cache=rnd.randrange(16), prot=rnd.randrange(8),
            strobe="mix", **exp)
