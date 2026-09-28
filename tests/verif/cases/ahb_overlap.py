"""Packets overlap at an AHB outlet without sharing response metadata
(fixture recipe ahb_outlet with overlap=1: the several-packets outlet with
tags, window 0x1000..0x11000, behind an AXI4 front that keeps eight bursts
per direction in flight), at 32 and 1024 bits.

Scenarios: 1 one-word writes only (the AHB bus takes write transfers at
most two cycles apart), 2 four-word reads only (at least 80 transfers in 100
cycles), 3 four-word packets both ways with response backpressure, wait
states and an ERROR (RETRY with the 2-bit HRESP) on every word of the third
packet, 4 a common reset with packets in flight, 6 as 3 with one-word
packets; with the timeout, 5 holds HREADY low from the start so the target
expires and every burst answers SLVERR, then 1 runs after the common reset.

Judged: the offline checker (bytes, read data, responses with the
failed packet's codes, every B and R to its own AXI ID in order, causality,
attribution, one transfer per word), the shape model and bus rules of
ahb_shape.py, the stream monitors on the outlet's request and response
faces (packets overlap in the outlet: a request head accepted while two
earlier packets of its direction are unanswered), the write transfer gap of
scenario 1 and the read transfer rate of scenario 2, and with the 2-bit
HRESP a RETRY that met the next transfer in its address phase."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ahb_plan  # noqa: E402

N = 24
WIDTHS = (32, 1024)
MODES = ("plain", "retry", "shaped", "timeout")
WIN = 0x1000


def params(width, mode):
    out = dict(data_bits=str(width), overlap="1", fixed_single="1")
    if mode == "retry":
        out["hresp2"] = "1"
    if mode == "shaped":
        out.update(complete="1", blocks="1")
    if mode == "timeout":
        out["timeout"] = "1024"
    return out


def model(width):
    t0 = dict(kind="ahb", bus_bytes=width // 8, engine_bytes=width // 8, prot=False,
              attributes=None, fixed="single", error_data_zero=True)
    return dict(routes={"s0": [("t0", WIN, 0x10000)]}, error_codes=True, targets={"t0": t0})


def fault(id_, expect, contract, **spec):
    return dict(id=id_, component="target", expect=expect, contract=contract, **spec)


FAULTS = [
    fault("read_boundary", ["AXI.R.UNEXPECTED", "AXI.R.LAST", "SOURCE_R_DATA", "SOURCE_R_RESP",
                            "SOURCE_EXTRA_R", "SOURCE_UNANSWERED", "STREAM.HOLD",
                            "TAPE.DRAIN_TIMEOUT:SCENARIO_2"],
          "AXI.ID.RETURN", op="set_next", at={"binding": "r.metadata0.tag"},
          **{"with": {"select": {"binding": "r.metadata1.tag"}}},
          note="a response's tag shifts before its beat has left"),
    fault("write_order", ["AXI.B.UNEXPECTED", "SOURCE_EXTRA_B", "SOURCE_B_RESP",
                          "SOURCE_UNANSWERED", "STREAM.HOLD", "TAPE.DRAIN_TIMEOUT:SCENARIO_1"],
          "AXI.ID.RETURN", op="set_driver", port="nl_wrsp_tag",
          **{"with": {"select": {"binding": "w.context1.descriptor.tag"}}},
          note="a held B takes the tag of the packet at the write head"),
    fault("serial", ["ORACLE"], "APB.TARGET.ACCESS", op="set_operand", operand=0,
          at={"path": [1, 1, 1], "from": {"driver": "nl_wreq_ready"},
              "ops": ["BitAnd", "BitOr", "BitNot"]},
          **{"with": {"select": {"binding": "w.busy"}}},
          note="correct packets, but each head waits for every older write"),
]


def bfms(mode):
    status = dict(sideband=("target", "status", "timed_out")) if mode == "timeout" else {}
    return [dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
            dict(name="t0", kind="ahb_slave", component="target", endpoint="ahb", salt=5,
                 **status)]


def config(width, mode):
    faults = FAULTS if (width, mode) == (32, "plain") else []
    if (width, mode) == (32, "retry"):
        faults = [dict(id=f"single_cycle_response_{code}", op="scenario",
                       params=dict(malformed=True, malformed_code=code),
                       expect=["AHB.RESPONSE.TWO_CYCLE"], contract="APB.TARGET.ACCESS")
                  for code in (2, 3)]
    return dict(label=f"{mode}{width}",
                dut=dict(source="fixture", recipe="ahb_outlet", params=params(width, mode)),
                model=model(width), bfms=bfms(mode), scenario_args=dict(width=width, mode=mode),
                faults=faults)


CASE = dict(
    name="ahb_overlap",
    contracts=["APB.TARGET.ACCESS", "AXI.ID.RETURN", "AXI.ERR.READ_ZERO", "AXI.RESET.CANCEL",
               "AXI.TIMEOUT.EXACT"],
    dut=dict(source="fixture", recipe="ahb_outlet", params=params(32, "plain")),
    nets={"front": [("nl_axi_", "s_")], "target": [("nl_ahb_", "t_")]},
    bfms=bfms("plain"),
    streams=[dict(name=f"u_{face}", component="target", endpoint=face)
             for face in ("wreq", "wrsp", "rreq", "rrsp")],
    model=model(32),
    configs=[config(w, m) for w in WIDTHS for m in MODES],
    scenario_args=dict(width=32, mode="plain"),
    negative_faults_all=True,
    max_cycles=400000,
    faults=[],
)


def runs_of(mode):
    return (5, 1) if mode == "timeout" else (1, 2, 3, 6, 4, 1)


def scenario(t, seed, width=32, mode="plain", malformed=False, malformed_code=2):
    s0, t0 = "s0", "t0"
    lanes = width // 8
    size = lanes.bit_length() - 1
    code = malformed_code if malformed else 2 if mode == "retry" else 1
    t.set(t0, "response_one_cycle", int(malformed))
    for k, kind in enumerate(runs_of(mode), 1):
        beats = 1 if kind in (1, 6) else 4
        t.mark("scenario", k)
        # Zero wait states and every response taken, or (3 to 6) waits and
        # the responses held at first, then taken most cycles.
        late = kind >= 3
        t.set(t0, "wait_mode", 2 if late else 0)
        t.set(t0, "wait", 0)
        t.set(t0, "idle_mode", 0)
        for reg in ("reg0_lo", "reg0_hi", "reg0_c", "reg1_lo", "reg1_hi", "reg1_c"):
            t.set(t0, reg, 0)
        t.ready(s0, "b", "never" if late else "always")
        t.ready(s0, "r", "never" if late else "always")
        t.reset(4)
        t.delay(2)
        if kind in (3, 6):
            # Every word of the third packet of each direction fails.
            for reg, base in (("reg0", 0), ("reg1", 0x8000)):
                t.set(t0, reg + "_lo", base + 2 * beats * lanes)
                t.set(t0, reg + "_hi", base + 3 * beats * lanes)
                t.set(t0, reg + "_c", code)
        if kind == 5:
            # HREADY stays low: the first transfer waits until the target
            # expires; every burst answers SLVERR.
            t.set(t0, "wait", 1_000_000)
            t.set(t0, "idle_mode", 4)
        failed = kind in (3, 6)
        for p in range(N):
            # Each word one transfer; after expiry SLVERR, accesses not judged.
            write, read = dict(targets=beats), dict(targets=beats)
            if kind == 5:
                write = read = dict(resp=2, target="any")
            elif failed and p == 2:
                write = dict(write, resp=2)
                read = dict(read, beat_resps=[2] * beats)
                if mode == "retry":
                    write.update(abort_after=1, targets=1)
                    read.update(abort_after=1, targets=1)
            if kind != 2:
                t.write(s0, WIN + p * beats * lanes, beats=beats, size=size, id=p % 4, **write)
            if kind != 1:
                t.read(s0, 0x9000 + p * beats * lanes, beats=beats, size=size, id=p % 4, **read)
        if kind == 4:
            t.delay(12)
            t.reset(4)
            t.delay(20)
            continue
        if late:
            t.delay(35)
            t.ready(s0, "r", "modulo", 7, 2)
            t.delay(10)
            t.ready(s0, "b", "modulo", 5, 1)
        t.drain(limit=40000, label=f"SCENARIO_{k}")
        if kind == 5:
            t.expect_now(t0, "sideband", "==", 1, "TIMEOUT_EXPIRED")
    if mode == "retry":
        t.expect_now(t0, "retry_next", ">=", 1, "RETRY_REACHED")
    t.end()


def scenario_windows(checker, events):
    marks = checker.tape["marks"]
    if "scenario" not in marks:
        return {}
    index = marks.index("scenario")
    starts = [(e["v"], e["t"]) for e in events if e.get("k") == "mk" and e.get("m") == index]
    end = max((e.get("t", 0) for e in events), default=0) + 1
    return {v: (t, starts[i + 1][1] if i + 1 < len(starts) else end)
            for i, (v, t) in enumerate(starts)}


def overlap(checker):
    """A request head accepted while two earlier packets of its direction
    are unanswered at the outlet's response face."""
    reached = 0
    for req, rsp in (("u_wreq", "u_wrsp"), ("u_rreq", "u_rrsp")):
        fields = {s["name"]: s["fields"] for s in checker.bench["streams"]}
        stream = sorted([(e["t"], 0, e) for e in checker.streams.get(req, [])]
                        + [(e["t"], 1, e) for e in checker.streams.get(rsp, [])],
                        key=lambda x: (x[0], x[1]))
        heads = answered = 0
        epoch = None
        for _, kind, e in stream:
            if e["_epoch"] != epoch:
                epoch, heads, answered = e["_epoch"], 0, 0
            value = int(e["x"], 16)
            row, shift = {}, 0
            for name, width in reversed(fields[req if kind == 0 else rsp]):
                row[name] = (value >> shift) & ((1 << width) - 1)
                shift += width
            if kind == 0 and row.get("first"):
                if heads - answered >= 2:
                    reached += 1
                heads += 1
            elif kind == 1 and row.get("last"):
                answered += 1
    return reached


def oracle(checker, events, sources, targets):
    args = checker.case.get("scenario_args", {})
    width, mode = args.get("width", 32), args.get("mode", "plain")
    lanes = width // 8
    seg = ahb_plan.Segment("s0", "t0", WIN, 0x10000, lanes, lanes, [],
                           complete=mode == "shaped", no_incr=mode == "shaped")
    out = ahb_plan.judge(checker, events, sources, targets, [seg], Counter())
    windows = scenario_windows(checker, events)
    bus = sorted((tt for tt in targets if tt.port == "t0"), key=lambda tt: tt.t)
    for k, kind in enumerate(runs_of(mode), 1):
        if k not in windows:
            continue
        lo, hi = windows[k]
        mine = [tt.addr_cycle for tt in bus if lo <= tt.t < hi]
        if kind == 1 and mine:
            widest = max((b - a for a, b in zip(mine, mine[1:])), default=0)
            if widest > 2:
                out.append(dict(rule="WRITE_GAP", scenario=k, widest=widest))
        if kind == 2 and mine and (len(mine) - 1) * 100 < 80 * (mine[-1] - mine[0]):
            out.append(dict(rule="READ_RATE", scenario=k, transfers=len(mine),
                            cycles=mine[-1] - mine[0]))
    if mode != "timeout" and not overlap(checker):
        out.append(dict(rule="OVERLAP_REACHED"))
    return out[:8]


CASE["oracle"] = oracle
