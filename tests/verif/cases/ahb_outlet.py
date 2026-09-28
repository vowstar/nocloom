"""The AHB outlet behind an AXI4 initiator front of the same width (fixture
recipe ahb_outlet, window 0x1000..0x2000 or the 512-byte sub-page window),
one registration per data width (--param data_bits=32..1024).

Modes: 0 plain, 1 and 24 sparse strobes (24 splits containers into blocks
of several sizes), 2 ERROR on the second transfer address of each
direction, 4 a common reset in flight, 5 and 6 RETRY and SPLIT there
(cancel the next address and fail the remaining packet), 7 no strobes, 8 unmapped, 13 the second
transfer of each direction fails, 14 a FIFO word, 15 write data held about
1000 cycles after the second word while the read waits (the bus shows BUSY
throughout), 16 RREADY held about 1000 cycles (a long read waits for a
response slot in BUSY), 17 to 20 a partial or empty last, middle or first
word, 21 HREADY low on every other idle cycle and W at full rate (block
boundaries meet waited cycles), 22 the empty last word arriving while HREADY is low in BUSY, 23
two packets of each direction one after the other, 25 a full-rate source
(at least 90 transfers in 100 cycles inside bursts with zero wait states).
The 8-byte beat of a 32-bit source is excluded: the
initiator BFM never drives an AxSIZE above its bus.

Judged: the offline checker (bytes, strobes, HPROT, read data, responses,
causality, attribution, FIXED refused), the stream monitors on the outlet's
response faces (metadata per beat and burst), the case oracle with the
independent shape model and bus rules of ahb_shape.py (every transfer's
HBURST, HTRANS, HSIZE and the transfer counts, ownership of the bus by a
packet that keeps it, the bus in BUSY for every cycle of a long stall), and
coverage: an ERROR (and with a 2-bit HRESP a RETRY or SPLIT) met the next
transfer in its address phase, block boundaries reached (and with the fixed
profile met waited cycles), full-rate cases and at the wide widths
full-width, narrow and split transfers of two sizes."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ahb_plan  # noqa: E402
import ahb_shape as shape  # noqa: E402

PARAMS = globals().get("PARAMS", {})
WIDTH = int(PARAMS.get("data_bits", "32"))
if WIDTH not in (32, 64, 128, 256, 512, 1024):
    raise ValueError(f"data_bits {WIDTH} not in 32..1024")
LANES = WIDTH // 8
WIN = 0x1000


def tokens_of(variant):
    return set(variant.split("+")) - {"baseline"}


def model(tokens):
    size = 0x200 if "window" in tokens else 0x1000
    t0 = dict(kind="ahb", bus_bytes=LANES, engine_bytes=LANES, prot=False, attributes=None,
              fixed="single" if "fixed_single" in tokens else "reject", error_data_zero=True)
    return dict(routes={"s0": [("t0", WIN, size)]}, error_codes=True, targets={"t0": t0})


def config(variant, waits="fixed", faults=()):
    tokens = tokens_of(variant)
    params = dict(data_bits=str(WIDTH))
    for token in tokens:
        params[token] = {"window": "512", "timeout": "4096"}.get(token, "1")
    label = variant + ("" if waits == "fixed" else "@" + waits)
    return dict(label=label, dut=dict(source="fixture", recipe="ahb_outlet", params=params),
                model=model(tokens),
                scenario_args=dict(tokens=sorted(tokens), waits=waits),
                faults=list(faults))


SHAPED32 = ["complete", "blocks", "complete+blocks", "fixed_single+complete",
            "fixed_single+blocks", "fixed_single+complete+blocks", "window", "window+blocks",
            "window+complete", "window+complete+blocks", "hresp2", "hresp2+fixed_single",
            "hresp2+complete+blocks"]
VARIANTS_WIDE = ["fixed_single", "complete", "blocks", "complete+blocks",
                 "fixed_single+complete+blocks", "hresp2", "hresp2+complete+blocks", "window",
                 "window+blocks", "window+fixed_single+complete", "timeout",
                 "timeout+complete+blocks"]
PROFILES = [("baseline", "zero"), ("baseline", "random"), ("blocks", "zero"),
            ("complete+blocks", "random"), ("hresp2", "zero"), ("hresp2+fixed_single", "random"),
            ("window+blocks", "zero"), ("fixed_single+complete", "random")]


def fault(id_, expect, contract, **spec):
    return dict(id=id_, component="target", expect=expect, contract=contract, **spec)


def every_use(binding, const):
    """Every use of a bound value takes a constant."""
    return dict(op="replace", at={"binding": binding}, **{"with": {"const": const}})


SHAPE = ["ORACLE"]
FAULTS32 = [
    fault("address", ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "SOURCE_R_DATA", "ORACLE"],
          "AXI.ROUTE.LOCAL_OFFSET", op="set_driver", port="nl_ahb_haddr",
          **{"with": {"const": 0}}),
    fault("read_data", ["SOURCE_R_DATA"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_rrsp_data", **{"with": {"const": 0}}),
    fault("result", ["SOURCE_B_RESP"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_wrsp_result", **{"with": {"const": 0}}),
    fault("protection", ["TARGET_PROT"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_ahb_hprot", **{"with": {"const": 0}}),
    fault("mask_hold", ["TAPE.DRAIN_TIMEOUT:CASE_2"], "APB.TARGET.ACCESS", op="set_operand",
          operand=2, at={"state_next": {"binding": "w.mask"}},
          **{"with": {"select": {"binding": "w.mask"}}},
          note="a sparse word issues its first block again without end"),
    fault("packet_count", ["STREAM_FIELD"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_rrsp_count", **{"with": {"const": 0}}),
    fault("hold", ["AHB.ADDR.HOLD", "AHB.BUSY.CONTROL", "ORACLE"], "APB.TARGET.ACCESS",
          **every_use("bus.quiet", 1),
          note="a new transfer replaces one waiting in its address phase"),
    fault("hwdata", ["TARGET_WRITE_BYTES"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_ahb_hwdata", **{"with": {"select": {"binding": "w.data"}}},
          note="HWDATA shows the address phase's word"),
    fault("room", ["SOURCE_R_DATA", "SOURCE_R_RESP", "SOURCE_R_COUNT", "STREAM_FIELD",
                   "AXI.R.LAST", "AXI.R.UNEXPECTED", "SOURCE_UNANSWERED"],
          "APB.TARGET.ACCESS", **every_use("r.room", 1),
          note="a read issues without response room"),
    fault("flight", ["SOURCE_EARLY:w", "TARGET_WRITE_BYTES", "SOURCE_B_RESP"],
          "APB.TARGET.ACCESS", **every_use("w.in_flight", 0),
          note="the B leaves before the packet's last transfers complete"),
    fault("peel", SHAPE, "APB.TARGET.ACCESS", **every_use("w.peeled", 0),
          note="a sparse word whose rest is a half goes out as that half"),
    fault("single", SHAPE, "APB.TARGET.ACCESS", **every_use("r.single", 0),
          note="a one-beat read keeps its container instead of its bytes or the word"),
    dict(id="dependent", op="scenario", params=dict(dependent=True),
         expect=["LIVE.E1", "TAPE.WAIT_TIMEOUT:DEPENDENT_HOLD"], contract="AXI.LIVE.PREMISES",
         note="write data that waits on reads of the same manager breaks AXI A3.3: the write "
              "burst holds the bus in BUSY and the read never makes a transfer"),
]
FAULTS_WIDE = [
    fault("stride", ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "SOURCE_R_DATA", "ORACLE"],
          "APB.TARGET.ACCESS", op="set_operand", operand=1,
          at={"path": [1, 2, 2, 0], "from": {"state_next": {"binding": "r.issue_address"}},
              "ops": ["Select", "Select", "BitNot", "Subtract"]},
          **{"with": {"const": 4}},
          note="read beats step 4 bytes whatever their AxSIZE"),
    fault("hsize_word", ["ORACLE", "TARGET_WRITE_BYTES", "TARGET_FOOTPRINT", "SOURCE_R_DATA"],
          "APB.TARGET.ACCESS", op="set_driver", port="nl_ahb_hsize", **{"with": {"const": 2}}),
    fault("lane", ["TARGET_WRITE_BYTES", "TARGET_FOOTPRINT", "ORACLE"], "APB.TARGET.ACCESS",
          op="replace",
          at={"match": {"op": "ZeroExtend", "args": [{"binding": "bus.lane"}]}},
          **{"with": {"const": 0}},
          note="a split transfer loses its byte lane: HADDR is the word address"),
    fault("read_data", ["SOURCE_R_DATA"], "APB.TARGET.ACCESS", op="set_driver",
          port="nl_rrsp_data", **{"with": {"const": 0}}),
    fault("peel", SHAPE, "APB.TARGET.ACCESS", **every_use("w.peeled", 0)),
    fault("start", SHAPE, "APB.TARGET.ACCESS", **every_use("bus.start_block", 0),
          note="a one-beat write's start block goes out by bytes"),
]
if WIDTH == 1024:
    FAULTS_WIDE.append(fault(
        "wrap_kib", ["ORACLE"], "APB.TARGET.ACCESS", op="set_operand", operand=0, hits=4,
        at={"glob": "*.wrap_kib"}, **{"with": {"const": 0}},
        note="a WRAP16 of 128-byte beats keeps its 2 KiB container"))


def configs():
    if WIDTH == 32:
        out = [config("baseline", faults=FAULTS32), config("fixed_single")]
        out += [config(v) for v in SHAPED32]
    else:
        out = [config("baseline", faults=FAULTS_WIDE)] + [config(v) for v in VARIANTS_WIDE]
    return out + [config(v, w) for v, w in PROFILES]


CASE = dict(
    name=f"ahb_outlet_{WIDTH}",
    contracts=["APB.TARGET.ACCESS", "AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO",
               "AXI.ERR.UNMAPPED_DECERR", "AXI.RESET.CANCEL"]
    + (["AXI.LIVE.PREMISES"] if WIDTH == 32 else []),
    dut=dict(source="fixture", recipe="ahb_outlet", params=dict(data_bits=str(WIDTH))),
    nets={"front": [("nl_axi_", "s_")], "target": [("nl_ahb_", "t_")]},
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="ahb_slave", component="target", endpoint="ahb", salt=5)],
    streams=[dict(name="u_wrsp", component="target", endpoint="wrsp"),
             dict(name="u_rrsp", component="target", endpoint="rrsp")],
    model=model(set()),
    configs=configs(),
    scenario_args=dict(tokens=[], waits="fixed"),
    negative_faults_all=True,
    max_cycles=2000000,
    run_timeout=900,
    faults=[],
)


def case_list(tokens):
    window = 0x200 if "window" in tokens else None
    out = shape.cases(LANES, LANES, window, "fixed_single" in tokens)
    # RETRY and SPLIT need the 2-bit HRESP.
    return [c for c in out if "hresp2" in tokens or c[3] not in (5, 6)]


def segment(tokens, waits, dependent=False):
    limit = 0x200 if "window" in tokens else 0x1000
    selected = [(8, 2, 1, 12, 0)] if dependent else case_list(tokens)
    return ahb_plan.Segment("s0", "t0", WIN, limit, LANES, LANES, selected,
                            complete="complete" in tokens, no_incr="blocks" in tokens,
                            waits=waits)


def scenario(t, seed, tokens=(), waits="fixed", dependent=False):
    tokens = set(tokens)
    seg = segment(tokens, waits, dependent)
    for k, _, case in ahb_plan.plan([seg]):
        ahb_plan.run_case(t, k, seg, case, dependent)
        if dependent:
            break
    if not dependent:
        if not seg.window:
            t.expect_now("t0", "err_next", ">=", 1, "ERROR_NEXT_COVER")
            if "hresp2" in tokens:
                t.expect_now("t0", "retry_next", ">=", 1, "RETRY_NEXT_COVER")
        t.check(kind="stream_beats", stream="u_rrsp", port="s0", dir="r")
        t.check(kind="stream_beats", stream="u_wrsp", port="s0", dir="w", per="burst")
    t.end()


def oracle(checker, events, sources, targets):
    args = checker.case.get("scenario_args", {})
    tokens = set(args.get("tokens", ()))
    waits = args.get("waits", "fixed")
    coverage = Counter()
    out = ahb_plan.judge(checker, events, sources, targets, [segment(tokens, waits)], coverage)
    if LANES > 4 and not tokens and waits == "fixed":
        sizes = [k for k in coverage if k.startswith("split_size_")]
        if not (coverage["full"] and coverage["narrow"] and coverage["split"] and len(sizes) >= 2):
            out.append(dict(rule="SHAPE_COVER", coverage=dict(coverage)))
    return out[:8]


CASE["oracle"] = oracle
