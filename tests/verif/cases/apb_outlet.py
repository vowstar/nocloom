"""The APB4, APB3 and APB2 burst outlet behind a 32-bit AXI4 initiator front
(fixture recipe apb_outlet, window 0x1000..0x2000), and the same outlet
with back-to-back accesses, alone and in a clocked network (public CLI).


Modes: 0 plain, 1 and 12 sparse strobes, 2 PSLVERR on the second word of
each direction, 4/9/10/11 a common reset at SETUP, in a waited ACCESS,
with R held and with B held, 6 zero wait, 7 no strobes, 8 unmapped, 13
FIXED with the second access of each direction failing, 14 a FIFO word,
15 full rate (each direction's completions at most three edges apart, two
with back-to-back accesses), 16 zero-wait PSLVERR, 17 PSLVERR on the last
word, 18 both directions at once (one write block inside the read packet,
every completion three edges after the one before, two back-to-back: the
cadence of the shared engine; without back-to-back accesses the
write packet waits until the read packet is answered: every read, then
every write), 19 and 20 partial words on the
strobeless dialects (SLVERR for the partial word, later full words served), 22 a common
reset with the read response queue full. Mode 5 (a peripheral that
commits despite PSLVERR) and the 8-byte INCR beat on the 32-bit source
(mode 3) are excluded: the offline checker counts a failed APB write
as delivered either way, and the initiator BFM never drives an AxSIZE
above its bus. With the 2048-cycle timeout (back-to-back APB4 and APB3) a
second read word waits 2200 cycles: the target expires during that live
access, the source gets the first beat and SLVERR with zero data for the
rest, the late completion and the packet's last two words still reach the
target, a later read is answered without access, and the common reset
restores service.

The offline checker judges bytes, strobes, PPROT, read data, responses,
causality (no B before the last access of its packet), attribution after
every reset and the access counts; the stream monitors judge the outlet's
response metadata per beat and per burst. The case oracle judges the
outlet's own write response face (offered only after the packet's last
access completed), PSTRB zero on reads, the cadence of modes 15 and 18,
and with back-to-back accesses the read response queue every clock (a full
queue stops read issue; it fills, stalls, pops and pushes in one cycle and
skips; the write buffer prefetches during a waited access). Without
back-to-back accesses it judges the serial outlet, one access at a
time: no packet head is taken while an earlier packet of
either direction is unanswered, and no read access completes while the
response of an earlier one has not been taken. In the clocked
network it judges the map's storage obligations of the APB target's burst
owners: each read and write packet released once, after its last access."""

import sys
from pathlib import Path

from nlverif import config as nlconfig

STRB = 15

APB4 = [(4, 2, 1, 0, 0), (8, 2, 1, 1, 0), (16, 2, 1, 12, 0), (4, 2, 1, 2, 0), (4, 2, 1, 6, 0),
        (4, 2, 1, 7, 0), (4, 2, 1, 8, 0), (16, 2, 1, 4, 0), (16, 2, 1, 9, 0),
        (16, 2, 1, 10, 0), (1, 2, 1, 11, 0), (8, 0, 1, 0, 1), (8, 1, 1, 0, 1), (8, 2, 1, 0, 1),
        (8, 2, 0, 3, 0), (8, 2, 2, 0, 4), (32, 2, 1, 0, 0), (256, 2, 1, 0, 0), (4, 2, 1, 0, 732),
        (16, 2, 1, 15, 0), (8, 2, 1, 16, 0), (4, 2, 1, 17, 0), (1, 2, 1, 17, 0),
        (16, 2, 1, 18, 0)]
FIXED_SINGLE = [(8, 2, 0, 0, 0), (1, 2, 0, 0, 0), (8, 2, 0, 1, 0), (16, 2, 0, 12, 0),
                (6, 0, 0, 0, 1), (6, 1, 0, 0, 2), (5, 2, 0, 13, 0), (8, 2, 0, 14, 0),
                (6, 0, 0, 14, 1)]
STROBELESS = [(4, 2, 1, 0, 0), (8, 2, 1, 7, 0), (8, 2, 2, 0, 4), (16, 2, 1, 15, 0), (16, 2, 1, 18, 0),
              (4, 2, 1, 19, 0), (1, 2, 1, 19, 0), (6, 2, 1, 20, 0), (8, 0, 1, 0, 1)]
APB3_EXTRA = [(4, 2, 1, 2, 0), (4, 2, 1, 17, 0), (1, 2, 1, 17, 0)]

BASE, WBASE, RBASE = 0x1000, 0x1120, 0x1800
# Mode 18 at the serial outlet: edges from the last read completion to the
# first write completion (the last read response is taken, then the write
# packet head enters and issues).
SERIAL_TURN = 4
# Behind a network source a narrow INCR is packed into words and a long
# burst is cut into 128-byte packets, each released on its own: the clocked
# configuration keeps one packet per direction and case.
NETWORK_PACKED = [(8, 0, 1, 0, 1), (8, 1, 1, 0, 1), (256, 2, 1, 0, 0)]


def model(dialect, fixed_single):
    t0 = dict(kind="apb", bus_bytes=4, engine_bytes=4, prot=False, pack_narrow=False,
              fixed="single" if fixed_single else "reject", error_data_zero=True)
    if dialect == "apb4":
        t0["attributes"] = None  # PPROT is AxPROT (AMBA preset of a dedicated target)
    else:
        t0["dialect"] = int(dialect[-1])
    return dict(routes={"s0": [("t0", BASE, 0x1000)]}, error_codes=True, targets={"t0": t0})


def cases_of(dialect, fixed_single, back_to_back=False, network=False):
    """The case list of a configuration."""
    if dialect != "apb4":
        cases = STROBELESS + (APB3_EXTRA if dialect == "apb3" else [])
        return cases + ([(8, 2, 1, 22, 0), (4, 2, 1, 8, 0)] if back_to_back else [])
    cases = list(APB4)
    if fixed_single:
        at = cases.index((8, 2, 0, 3, 0))
        cases[at:at + 1] = FIXED_SINGLE
    if network:
        # The outlet's own cadence (modes 15 to 18) is measured at the outlet
        # alone: in a network the source path paces the words.
        return [c for c in cases if c[3] not in (15, 16, 17, 18)]
    return cases + ([(8, 2, 1, 22, 0)] if back_to_back else [])


def bfms(dialect, timeout=False):
    return [dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
            dict(name="t0", kind="apb_slave", component="target", endpoint="apb", dialect=dialect,
                 salt=3, **({"sideband": ("target", "status", "timed_out")} if timeout else {}))]


def config(label, dialect="apb4", fixed_single=False, back_to_back=False, timeout=False,
           faults=()):
    params = dict(dialect=dialect)
    if fixed_single:
        params["fixed_single"] = "1"
    extra = {}
    if back_to_back:
        params["back_to_back"] = "1"
        # The read response queue and the write buffer, every clock.
        extra["probes"] = {}
    if timeout:
        params["timeout_cycles"] = "2048"
    return dict(label=label, dut=dict(source="fixture", recipe="apb_outlet", params=params),
                model=model(dialect, fixed_single), bfms=bfms(int(dialect[-1]), timeout),
                scenario_args=dict(dialect=dialect,
                                   cases=cases_of(dialect, fixed_single, back_to_back),
                                   back_to_back=back_to_back, timeout=timeout),
                faults=list(faults), **extra)


def clocked_input():
    """The CLI path: two AXI4 sources, an
    AXI4, an APB4 and an AHB target over two-node graphs in three clock
    domains; the APB4 target t1 (domain target) takes back-to-back accesses
    and serves FIXED bursts as single transfers."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from run_apb_public import configuration  # noqa: PLC0415
    cfg = configuration("clocked")
    cfg["targets"][1]["apb_back_to_back"] = True
    cfg["targets"][1]["fixed_bursts"] = "single"
    return cfg


CLOCKED = clocked_input()


def storage_probes(mapping):
    """The release nets of t1's read and write burst owners (map storage)."""
    stores = {s["resource"]: s for s in mapping["network"]["storage"]}
    return [dict(name="release", kind="storage", nets=[
        [d, "nl_internal_v" + stores[f"burst_service/t1/{d}"]["release"].removeprefix("value/"), 1]
        for d in ("read", "write")])]


def clocked_config():
    return dict(
        label="clocked", dut=dict(source="cli", config=CLOCKED), nets={}, streams=[],
        bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
              dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
              dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=1),
              dict(name="t1", kind="apb_slave", endpoint="target:t1", clock="target", salt=3),
              dict(name="t2", kind="ahb_slave", endpoint="target:t2", salt=4)],
        clocks=dict(main=10.0, middle=14.0, target=22.0),
        model=dict(routes=nlconfig.routes(CLOCKED), error_codes=nlconfig.error_codes(CLOCKED),
                   targets=nlconfig.targets(CLOCKED, error_data_zero=True)),
        map_probes=dict(groups=storage_probes, clock="target"),
        scenario_args=dict(dialect="apb4", cases=[c for c in cases_of("apb4", True, network=True)
                                                  if c not in NETWORK_PACKED],
                           back_to_back=True, network=True),
        faults=[])


def fault(id_, expect, contract, **spec):
    return dict(id=id_, component="target", expect=expect, contract=contract, **spec)


# The designated faults, as IR faults, and a
# back-to-back variant for the cadence configurations.
FAULTS = {
    "address": fault("address", ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES",
                                 "SOURCE_R_DATA"], "AXI.ROUTE.LOCAL_OFFSET",
                     op="set_driver", port="nl_apb_paddr",
                     **{"with": {"op": "Select", "args": [{"select": {"binding": "bus.write"}},
                                                          {"const": 0}, {"self": True}]}}),
    "read_address": fault("read_address", ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED",
                                           "SOURCE_R_DATA"], "AXI.ROUTE.LOCAL_OFFSET",
                          op="set_driver", port="nl_apb_paddr",
                          **{"with": {"op": "Select", "args": [
                              {"select": {"binding": "bus.write"}}, {"self": True},
                              {"op": "Subtract", "args": [{"self": True},
                                                          {"const": 0xFFFFFF00}]}]}}),
    "read_data": fault("read_data", ["SOURCE_R_DATA"], "APB.TARGET.ACCESS",
                       op="set_driver", port="nl_rrsp_data", **{"with": {"const": 0}}),
    "result": fault("result", ["SOURCE_B_RESP"], "APB.TARGET.ACCESS",
                    op="set_driver", port="nl_wrsp_result", **{"with": {"const": 0}}),
    "partial_result": fault("partial_result", ["SOURCE_B_RESP"], "APB.TARGET.ACCESS",
                            op="set_driver", port="nl_wrsp_result",
                            **{"with": {"const": 0}},
                            note="a rejected partial physical word must contribute SLVERR"),
    "protection": fault("protection", ["TARGET_PROT"], "APB.TARGET.ACCESS",
                        op="set_driver", port="nl_apb_pprot", **{"with": {"const": 0}}),
    # The back-to-back outlet's mask keeps the word as the engine sees it in
    # that cycle (accepted now or buffered), not only a buffered word's. The
    # serial outlet of whole words issues a word as it takes it and keeps no
    # mask.
    "mask_hold": fault("mask_hold", ["TAPE.DRAIN_TIMEOUT:CASE_1", "TAPE.DRAIN_TIMEOUT:CASE_2"],
                       "APB.TARGET.ACCESS",
                       op="set_next", at={"binding": "w.mask"},
                       **{"with": {"select": {"match": {"op": "Select", "ordered": True, "args": [
                           {"binding": "w.accept"}, "*", {"binding": "w.mask"}]}}}},
                       note="a word keeps its issued lanes: it issues again without end"),
    "packet_count": fault("packet_count", ["STREAM_FIELD"], "APB.TARGET.ACCESS",
                          op="set_driver", port="nl_rrsp_count", **{"with": {"const": 0}}),
    "strobe": fault("strobe", ["TARGET_WRITE_BYTES", "TARGET_UNSTROBED_DATA"],
                    "APB.TARGET.ACCESS",
                    op="set_driver", port="nl_apb_pstrb", **{"with": {"const": 0}}),
    "setup": fault("setup", ["APB.SETUP.ORDER"], "APB.TARGET.ACCESS",
                   op="set_driver", port="nl_apb_penable",
                   **{"with": {"select": {"driver": "nl_apb_psel"}}}),
    "flight": fault("flight", ["ORACLE", "SOURCE_EARLY:w", "SOURCE_B_RESP"], "APB.TARGET.ACCESS",
                    op="replace", at={"binding": "w.last_done"},
                    **{"with": {"op": "BitAnd", "args": [
                        {"select": {"binding": "w.last"}}, {"select": {"binding": "w.flight"}}]}},
                    note="a packet's last write word leaves the buffer at issue: the write "
                         "response is offered before that access completes"),
    "room": fault("room", ["STREAM_FIELD", "SOURCE_R_DATA", "SOURCE_R_RESP", "SOURCE_R_COUNT",
                           "AXI.R.LAST", "AXI.R.UNEXPECTED", "SOURCE_UNANSWERED", "ORACLE",
                           "TAPE.DRAIN_TIMEOUT:CASE_15", "TAPE.DRAIN_TIMEOUT:CASE_18"],
                  "APB.TARGET.ACCESS", op="replace", at={"binding": "r.room"},
                  **{"with": {"const": 1}},
                  note="reads issue whatever the response queue holds: a completion into a "
                       "full queue overwrites a response not yet taken"),
    "read_first": fault("read_first", ["ORACLE"], "APB.TARGET.ACCESS", op="replace",
                        at={"match": {"op": "BitAnd", "args": [
                            {"binding": "w.full"},
                            {"op": "BitNot", "args": [{"op": "Equal", "args": ["*", "*"]}]}]}},
                        **{"with": {"op": "BitAnd", "args": [
                            {"self": True}, {"op": "BitNot", "args": [{"select": {"match": {
                                "op": "BitAnd", "args": [
                                    {"binding": "r.busy"},
                                    {"op": "BitAnd", "args": [
                                        "*", {"op": "BitNot", "args": ["*"]}]}]}}}]}]}},
                        note="reads win the bus when both directions have a word: the write "
                             "choice wcan becomes wcan & ~rcan (the issue condition keeps "
                             "wcan | rcan)"),
    "decoupled": fault("decoupled", ["ORACLE"], "APB.TARGET.ACCESS", op="replace",
                       at={"binding": "apb.serial"}, **{"with": {"const": 0}},
                       note="the decoupled outlet: a packet head enters while the earlier "
                            "response waits and a read issues into the second response slot"),
    "back_to_back_off": fault("back_to_back_off", ["ORACLE"], "APB.TARGET.ACCESS", op="variant",
                              params={"back_to_back": "0"},
                              note="parameter-level variant: an idle cycle after every "
                                   "completion (cadence three edges, not two)"),
}

CASE = dict(
    name="apb_outlet",
    contracts=["APB.TARGET.ACCESS", "AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO",
               "AXI.ERR.UNMAPPED_DECERR", "AXI.RESET.CANCEL", "AXI.W.UNSTROBED_ZERO",
               "AXI.TIMEOUT.EXACT"],
    dut=dict(source="fixture", recipe="apb_outlet", params=dict(dialect="apb4")),
    nets={"front": [("nl_axi_", "s_")], "target": [("nl_apb_", "t_")]},
    bfms=bfms(4),
    streams=[dict(name=f"u_{face}", component="target", endpoint=face)
             for face in ("wreq", "wrsp", "rreq", "rrsp")],
    model=model("apb4", False),
    configs=[config("apb4", faults=[FAULTS[k] for k in (
                 "address", "read_address", "read_data", "result", "protection",
                 "packet_count", "strobe", "setup", "flight", "room", "decoupled")]),
             config("fixed_single", fixed_single=True),
             config("apb3", "apb3", faults=[FAULTS["flight"], FAULTS["room"],
                                            FAULTS["decoupled"], FAULTS["partial_result"]]),
             config("apb2", "apb2",
                    faults=[FAULTS["flight"], FAULTS["room"], FAULTS["decoupled"],
                            FAULTS["partial_result"]]),
             # Back-to-back accesses (a SETUP right after
             # a completion), a 2048-cycle timeout on APB4 and APB3.
             # Only the back-to-back outlet holds a word of each direction at
             # once (mode 18), so the write priority fault runs there.
             config("b2b_apb4", back_to_back=True, timeout=True,
                    faults=[FAULTS["back_to_back_off"], FAULTS["room"], FAULTS["read_first"],
                            FAULTS["mask_hold"]]),
             config("b2b_apb3", "apb3", back_to_back=True, timeout=True),
             config("b2b_apb2", "apb2", back_to_back=True, faults=[FAULTS["read_first"]]),
             clocked_config()],
    scenario_args=dict(dialect="apb4", cases=cases_of("apb4", False)),
    negative_faults_all=True,
    max_cycles=400000,
    faults=[],
)


def table(mode, beats):
    """The strobe table of a mode (lane masks per beat), or None for full."""
    if mode == 1:
        return [5, 10, 0, STRB]
    if mode == 12:
        return [i % 16 for i in range(16)]
    if mode == 19:
        return [STRB] * (beats - 1) + [3]
    if mode == 20:
        return [STRB, 12] + [STRB] * (beats - 2)
    return None


def scenario(t, seed, dialect="apb4", cases=(), back_to_back=False, timeout=False,
             network=False):
    s0 = "s0"
    t0 = "t1" if network else "t0"
    base = 0x5000 if network else BASE
    t.set(s0, "w_after_aw", 0)
    for k, (beats, size, burst, mode, offset) in enumerate(cases, 1):
        t.mark("case", k)
        wbase, rbase = base + 0x120 + offset, base + 0x800 + offset
        if mode == 8:
            wbase, rbase = 0x3120, 0x3800
        zero_wait = dialect == "apb2" or mode in (6, 15, 16, 18)
        # The target: two wait states, or none; no failure, FIFO or slow word.
        t.set(t0, "wait", 0 if zero_wait else 2)
        for reg in ("reg0_lo", "reg0_hi", "reg0_c", "reg1_lo", "reg1_hi", "reg1_c", "fifo_en",
                    "ovr_i0", "ovr_c0", "slow_mask"):
            t.set(t0, reg, 0)
        # The source: every response taken at once (full rate), or R held for
        # the first 70 cycles and B for 80 (the outlet's response queue
        # fills), then R taken three cycles in four and each B after two.
        full_rate = mode in (15, 18)
        t.ready(s0, "b", "always" if full_rate else "never")
        t.ready(s0, "r", "always" if full_rate else "never")

        def responses():
            if not full_rate:
                t.delay(70)
                t.ready(s0, "r", "modulo", 4, 0)
                t.delay(10)
                t.ready(s0, "b", "delay", 2)
        t.reset(4)
        t.delay(2)
        step = 1 << size
        burst_name = ("fixed", "incr", "wrap")[burst]

        def word(start, beat):
            if beat == 0 or burst == 0:
                a = start
            elif burst == 2:
                span = beats * step
                a = start // span * span + (start // step * step + beat * step) % span
            else:
                a = start // step * step + beat * step
            return (a - base) // 4 * 4

        def fail(start, beat, reg):
            # PSLVERR on one word (a one-word address region).
            local = word(start, beat)
            t.set(t0, reg + "_lo", local)
            t.set(t0, reg + "_hi", local + 4)
            t.set(t0, reg + "_c", 1)
        rresp = None
        if mode in (2, 16):
            fail(wbase, 1, "reg0")
            fail(rbase, 1, "reg1")
            rresp = [2 if i == 1 else 0 for i in range(beats)]
        if mode == 17:
            fail(wbase, beats - 1, "reg0")
            fail(rbase, beats - 1, "reg1")
            rresp = [2 if i == beats - 1 else 0 for i in range(beats)]
        if mode == 14:
            t.set(t0, "fifo_en", 1)
            t.set(t0, "fifo_addr", word(rbase, 0))
        strobes = table(mode, beats)
        if strobes:
            t.strobes(s0, strobes)
        wstrobe = "table" if strobes else "none" if mode == 7 else "full"
        write = dict(beats=beats, size=size, burst=burst_name, id=5, cache=2, prot=5,
                     strobe=wstrobe)
        read = dict(beats=beats, size=size, burst=burst_name, id=6, cache=1, prot=1)
        if rresp is not None:
            read["beat_resps"] = rresp
        if mode == 13:
            # FIXED: the second access of each direction fails (by count).
            t.set(t0, "base", 0)
            t.set(t0, "ovr_i0", 2)
            t.set(t0, "ovr_c0", 1)
            t.write(s0, wbase, **write)
            responses()
            t.drain(label=f"CASE_{k}")
            t.set(t0, "base", 0)
            t.read(s0, rbase, beat_resps=[2 if i == 1 else 0 for i in range(beats)], **read)
            t.drain(label=f"CASE_{k}")
            continue
        if mode == 15:
            # Full rate: the read after the write's B.
            b = t.snap(s0, "b")
            t.write(s0, wbase, **write)
            t.wait(s0, "b", "==", 1, since=b, label=f"CASE_{k}_B")
            t.read(s0, rbase, **read)
            t.drain(label=f"CASE_{k}")
            continue
        if mode == 18:
            # Both directions offered: AR first, AW four cycles later.
            t.read(s0, rbase, **read)
            t.delay(4)
            t.write(s0, wbase, **write)
            t.drain(label=f"CASE_{k}")
            continue
        if mode in (4, 9, 10, 11, 22):
            # A common reset in the middle of the case; nothing replays after it.
            if mode == 9:
                t.set(t0, "wait", 6)
            if mode not in (10, 22):
                t.ready(s0, "r", "modulo", 4, 0)
            if mode != 11:
                t.ready(s0, "b", "delay", 2)
            if mode != 22:
                t.write(s0, wbase, **write)
            t.read(s0, rbase, **read)
            if mode == 4:
                t.wait(t0, "psel", "==", 1, label=f"CASE_{k}_SETUP")
            elif mode == 9:
                t.wait(t0, "psel", "==", 1, label=f"CASE_{k}_ACCESS")
                t.delay(3)
            elif mode == 10:
                t.wait(s0, "rvalid", "==", 1, label=f"CASE_{k}_R_HELD")
            elif mode == 11:
                t.wait(s0, "bvalid", "==", 1, label=f"CASE_{k}_B_HELD")
            else:
                # R held from the start: the response queue fills and the
                # reads stop (judged on the probes), then the reset.
                t.wait(s0, "rvalid", "==", 1, label=f"CASE_{k}_R_HELD")
                t.delay(40)
            t.reset(4)
            t.delay(20)
            continue
        t.write(s0, wbase, **write)
        t.read(s0, rbase, **read)
        responses()
        t.drain(label=f"CASE_{k}")
    if timeout:
        timeout_case(t, len(cases) + 1)
    if not network:
        # The outlet's response metadata: every read beat's address and
        # byte count, each write burst's start and beat size.
        t.check(kind="stream_beats", stream="u_rrsp", port="s0", dir="r")
        t.check(kind="stream_beats", stream="u_wrsp", port="s0", dir="w", per="burst")
    t.end()


def timeout_case(t, k):
    """The 2048-cycle timeout during a live access:
    the second read word waits 2200 cycles. R is held for the first 20
    cycles, so the source takes the first beat early (a waiting response
    restarts the watchdog) and the stalled second access then expires."""
    s0, t0 = "s0", "t0"
    t.mark("case", k)
    t.set(t0, "wait", 0)
    t.set(t0, "slow_mask", 0xFFC)
    t.set(t0, "slow_match", RBASE - BASE + 4)
    t.set(t0, "slow_wait", 2200)
    t.ready(s0, "r", "never")
    t.ready(s0, "b", "always")
    t.reset(4)
    t.delay(2)
    a = t.snap(t0, "read")
    t.read(s0, RBASE, beats=4, size=2, id=6, cache=1, prot=1, target="any", late_access=True,
           targets=4, beat_resps=[0, 2, 2, 2])
    t.delay(20)
    t.ready(s0, "r", "always")
    # At expiry the second access is live on the bus (PSEL high) and only
    # the first access completed.
    t.wait(t0, "sideband", "==", 1, limit=2300, label="TIMEOUT_EXPIRES")
    t.expect_now(t0, "psel", "==", 1, "TIMEOUT_LIVE_ACCESS")
    t.expect_now(t0, "read", "==", 1, "TIMEOUT_LIVE_ACCESS", since=a)
    # The late completion and the packet's last two words reach the target;
    # the failure stays.
    t.wait(t0, "read", "==", 4, since=a, limit=400, label="TIMEOUT_LATE_READS")
    t.drain(label="TIMEOUT_ANSWER")
    t.expect_now(t0, "sideband", "==", 1, "TIMEOUT_STAYS")
    # The failed target answers without access.
    t.read(s0, RBASE, size=2, id=6, resp=2, target="none")
    t.drain(label="TIMEOUT_FAILED_TARGET_QUIET")
    # The common reset restores service.
    t.set(t0, "slow_mask", 0)
    t.reset(4)
    t.delay(2)
    t.expect_now(t0, "sideband", "==", 0, "TIMEOUT_RESET_CLEARS")
    t.read(s0, RBASE, size=2, id=6)
    t.drain(label="TIMEOUT_RECOVERY")


def apb_events(events, port):
    return [e for e in events if e.get("p") == port and e.get("k") == "hs" and e.get("ch") == "apb"]


def case_windows(checker, events):
    """case number -> (start time, end time) from the case markers."""
    if "case" not in checker.tape["marks"]:
        return {}
    index = checker.tape["marks"].index("case")
    starts = [(e["v"], e["t"]) for e in events if e.get("k") == "mk" and e.get("m") == index]
    end = max((e.get("t", 0) for e in events), default=0) + 1
    return {v: (t, starts[i + 1][1] if i + 1 < len(starts) else end)
            for i, (v, t) in enumerate(starts)}


def cadence(checker, events, cases, spacing):
    """Modes 15 and 18 from the APB completions: in mode 15 each direction's
    completions follow each other within spacing edges (exactly spacing at
    the widest with back-to-back accesses); in mode 18 the first completion
    is a read, the writes form one block inside the read packet and every
    completion follows the one before by exactly spacing edges. The serial
    outlet (spacing three, no back-to-back accesses) takes the write packet
    only after the read packet is answered: every read, then every write,
    each direction at spacing edges and the first write SERIAL_TURN edges
    after the last read."""
    out = []
    windows = case_windows(checker, events)
    apb = apb_events(events, "t0")
    for k, (beats, _, _, mode, _) in enumerate(cases, 1):
        if mode not in (15, 18) or k not in windows:
            continue
        lo, hi = windows[k]
        seq = [e for e in apb if lo <= e["t"] < hi]
        if mode == 15:
            for write in (1, 0):
                cycles = [e["c"] for e in seq if e["w"] == write]
                widest = max((b - a for a, b in zip(cycles, cycles[1:])), default=0)
                if len(cycles) != beats or widest > spacing or (spacing == 2 and widest != 2):
                    out.append(dict(detail="full-rate cadence", case=k, write=write,
                                    completions=len(cycles), widest=widest, want=spacing))
            continue
        dirs = [e["w"] for e in seq]
        if spacing == 3:
            gaps = [b["c"] - a["c"] for a, b in zip(seq, seq[1:])]
            if dirs != [0] * beats + [1] * beats:
                out.append(dict(detail="serial outlet: write before the read packet ended",
                                case=k, order="".join("w" if d else "r" for d in dirs)))
            elif set(gaps[:beats - 1] + gaps[beats:]) != {spacing} or gaps[beats - 1] != SERIAL_TURN:
                out.append(dict(detail="serial direction cadence", case=k, gaps=gaps,
                                want=spacing, turn=SERIAL_TURN))
            continue
        first = dirs.index(1) if 1 in dirs else None
        block = (first is not None and dirs[first:first + beats] == [1] * beats
                 and dirs.count(1) == beats)
        if len(seq) != 2 * beats or dirs[0] != 0 or not block or first + beats >= len(seq):
            out.append(dict(detail="write block not inside the read packet", case=k,
                            order="".join("w" if d else "r" for d in dirs)))
        gaps = {b["c"] - a["c"] for a, b in zip(seq, seq[1:])}
        if gaps != {spacing}:
            out.append(dict(detail="direction cadence", case=k, gaps=sorted(gaps), want=spacing))
    return out


def serial(checker, events, cases):
    """The serial outlet, one access at a time (no back-to-back
    accesses), from the outlet's own faces: a packet head
    (either direction) is taken only after every earlier packet's last
    response was taken, at a strictly later edge; and in each case without
    a common reset no read access completes before the responses of all
    earlier read accesses were taken."""
    out = []
    fields = {s["name"]: s["fields"] for s in checker.bench["streams"]}

    def row(name, e):
        value, shift, r = int(e["x"], 16), 0, {}
        for field, width in reversed(fields[name]):
            r[field] = (value >> shift) & ((1 << width) - 1)
            shift += width
        return r
    faces = [("u_wreq", 0), ("u_rreq", 0), ("u_wrsp", 1), ("u_rrsp", 1)]
    stream = sorted(((e["t"], kind, name, e) for name, kind in faces
                     for e in checker.streams.get(name, [])), key=lambda x: (x[0], x[1]))
    epoch, open_ = None, 0
    for t, kind, name, e in stream:
        if e["_epoch"] != epoch:
            epoch, open_ = e["_epoch"], 0
        r = row(name, e)
        if kind == 0 and r.get("first"):
            if open_:
                out.append(dict(detail="serial outlet: packet head before the earlier "
                                       "response was taken", face=name, t=t, open=open_))
                break
            open_ += 1
        elif kind == 1 and r.get("last"):
            open_ = max(0, open_ - 1)
    windows = case_windows(checker, events)
    reads = [e for e in apb_events(events, "t0") if not e["w"]]
    taken = [e["t"] for e in checker.streams.get("u_rrsp", [])]
    for k, (_, _, _, mode, _) in enumerate(cases, 1):
        if mode in (4, 9, 10, 11, 22) or k not in windows:
            continue
        lo, hi = windows[k]
        mine = [e["t"] for e in reads if lo <= e["t"] < hi]
        answered = [t for t in taken if lo <= t < hi]
        for i, t in enumerate(mine):
            if sum(1 for a in answered if a < t) < i:
                out.append(dict(detail="serial outlet: read access before the earlier "
                                       "response was taken", case=k, access=i, t=t))
                break
    # A zero-strobe word keeps its place in the serial word sequence even
    # though it makes no APB access. Retiring it must not admit the next
    # word before the idle/SETUP/ACCESS sequence has completed.
    for k, (beats, _, _, mode, _) in enumerate(cases, 1):
        if mode != 7 or k not in windows:
            continue
        lo, hi = windows[k]
        words = [e for e in checker.streams.get("u_wreq", []) if lo <= e["t"] < hi]
        gaps = [b["c"] - a["c"] for a, b in zip(words, words[1:])]
        if len(words) != beats or any(gap < 3 for gap in gaps):
            out.append(dict(detail="zero-strobe word retired before its serial slot",
                            case=k, words=len(words), want=beats, gaps=gaps))
    return out[:4]


def response_face(checker, events, sources, strobes):
    """The outlet's write response leaves only after the last write access
    of its packet completed (a strictly later edge), and an APB4 read drives
    PSTRB zero."""
    out = []
    for e in apb_events(events, "t0"):
        if strobes and not e["w"] and int(e.get("s", "0"), 16):
            out.append(dict(detail="PSTRB on a read", a=e["a"], c=e.get("c")))
            break
    clean = {t.epoch for t in sources} - {t.epoch for t in sources if t.cancelled}
    writes = [t for t in sources if t.port == "s0" and t.dir == "w" and t.epoch in clean]
    face = [e for e in checker.streams.get("u_wrsp", []) if e["_epoch"] in clean]
    for t, e in zip(writes, face):
        if t.targets and e["t"] <= max(tt.t for tt in t.targets):
            out.append(dict(detail="write response before the packet's last access",
                            tag=t.tag, t=e["t"]))
            break
    return out


def probe_rows(checker, events, name):
    group = next((g for g in checker.bench.get("probes", []) if g["name"] == name), None)
    if group is None:
        return None
    return checker._probe_rows(group, [e for e in events
                                       if e.get("k") == "pr" and e.get("p") == group["port"]])


def queue(checker, events, dialect):
    """Back-to-back read response queue, every clock: a full queue (front
    and tail, nothing popped) stops read issue; it fills and stalls, pops
    and pushes in one cycle, pops while skipping; the write buffer takes the
    next word while an access waits (not on APB2, which never waits)."""
    rows = probe_rows(checker, events, "apb_queue")
    if rows is None:
        return [dict(detail="no apb_queue probes")]
    out, seen = [], dict(tail_full=0, full_stalls=0, pop_push=0, skip_pop=0, prefetch=0)
    for _, cycle, r in rows:
        seen["tail_full"] += r["tail_full"]
        if r["front_full"] and r["tail_full"] and not r["pop"]:
            seen["full_stalls"] += 1
            if r["read_issue"]:
                out.append(dict(detail="read issued into a full response queue", c=cycle))
        seen["pop_push"] += r["pop"] and r["push"]
        seen["skip_pop"] += r["pop"] and r["skip"]
        seen["prefetch"] += (r["write_full"] and r["write_flight"] and r["phase"] == 2
                             and not r["complete"])
    missing = [k for k, v in seen.items() if not v and (k != "prefetch" or dialect != "apb2")]
    if missing:
        out.append(dict(detail="queue coverage not reached", missing=missing, seen=seen))
    return out[:4]


def releases(checker, events, cases):
    """The map's storage obligations of t1's burst owners (sampled on the
    main clock: a release is a rising edge of its net): in each case the
    read and the write packet are each released once (an unmapped case
    never reaches t1), after the last access of that direction; the reset
    cases are not counted."""
    group = next((g for g in checker.bench.get("probes", []) if g["name"] == "release"), None)
    if group is None:
        return [dict(detail="no release probes")]
    rises, level = dict(read=[], write=[]), dict(read=0, write=0)
    for e in events:
        if e.get("k") != "pr" or e.get("p") != group["port"]:
            continue
        row = checker._unpack(group, e)
        for d in rises:
            if row[d] and not level[d]:
                rises[d].append(e["t"])
            level[d] = row[d]
    apb = apb_events(events, "t1")
    out = []
    windows = case_windows(checker, events)
    for k, (_, _, _, mode, _) in enumerate(cases, 1):
        if mode in (4, 9, 10, 11) or k not in windows:
            continue
        lo, hi = windows[k]
        for d, write in (("read", 0), ("write", 1)):
            got = [r for r in rises[d] if lo <= r < hi]
            last = max((e["t"] for e in apb if lo <= e["t"] < hi and e["w"] == write), default=lo)
            if len(got) != (0 if mode == 8 else 1) or any(r <= last for r in got):
                out.append(dict(detail=f"{d} release", case=k, releases=len(got),
                                before_last_access=any(r <= last for r in got)))
    return out


def oracle(checker, events, sources, targets):
    args = checker.case.get("scenario_args", {})
    if args.get("network"):
        return releases(checker, events, args.get("cases", ()))[:8]
    spacing = 2 if args.get("back_to_back") else 3
    dialect = args.get("dialect", "apb4")
    out = cadence(checker, events, args.get("cases", ()), spacing)
    out += response_face(checker, events, sources, dialect == "apb4")
    if args.get("back_to_back"):
        out += queue(checker, events, dialect)
    else:
        out += serial(checker, events, args.get("cases", ()))
    return out[:8]


CASE["oracle"] = oracle
