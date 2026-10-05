"""Quick self-test of the nlverif package: burst model,
tape encoding, checker verdicts on synthetic logs (every offline mismatch
kind fires on a seeded defect; OFFLINE is the registry_contracts evidence
for the offline categories), repair of X/Z event fields, the reset
propagation self-check, classification, wrapper and bench generation, with
an Icarus compilation and simulation of the 256-initiator idle predicate."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nlverif import bench, events, faults, model  # noqa: E402
from nlverif import contracts as C  # noqa: E402
from nlverif.check import Checker  # noqa: E402
from nlverif.tape import Tape, TapeError  # noqa: E402

FAILS = []


def expect(cond, what):
    if not cond:
        FAILS.append(what)
        print("SELFTEST_FAIL", what)


def test_model():
    from axi4_width_stress_bench import MODES, configuration
    for mode in MODES:
        config = configuration(mode, True)
        owners = [s["user_flags"]["owner"] for s in config["initiators"]]
        expect(owners == [i % 2 for i in range(len(owners))], f"{mode} exclusive owners")
    expect(model.beat_addr(0x1034, 4, 2, 2, 3) == 0x1030, "wrap beat 3")
    expect(model.beat_addr(0x1001, 4, 1, 1, 1) == 0x1002, "incr unaligned")
    pieces, beats = model.downsize_pieces(4, 4, 2, 0x1020)
    expect(pieces == [(0x1020, 15, 2, 2)] and len(beats) == 16, "wrap of 16 words stays one WRAP")
    pieces, _ = model.downsize_pieces(65, 4, 1, 0x1000)
    expect([p[1] for p in pieces] == [255, 3], "INCR pieces cut at 256 words")
    expect(model.merge([0, 2, 3, 0]) == 3 and model.merge([0, 2]) == 2, "merge severity")
    expect(model.merge([1, 1]) == 1 and model.merge([1, 0]) == 0, "exclusive merge")
    expect(model.crosses_4k(0xFF8, 2, 3, 1) and not model.crosses_4k(0xFF0, 2, 3, 1), "4 KiB")


def bfms():
    return {"s0": dict(index=0, kind="axi_master", size=2, data_bits=32, lite=False),
            "t0": dict(index=1, kind="axi_slave", size=2, data_bits=32, lite=False)}


def test_tape():
    t = Tape(bfms())
    tag = t.write("s0", 0x1000, beats=2)
    t.wait("s0", "b", "==", 1, label="X")
    slots = {t.snap("s0", "b") for _ in range(70)}
    expect(len(slots) == 63 and max(slots) == 63, "snapshot slots wrap")
    t.end()
    text = t.assemble().split()
    expect(text[0] == "%016x" % 0x4E4C5654 and len(text) == 16 * (len(t.records) + 1), "tape size")
    expect(tag == 1 and t.labels[-1] == "X", "tags and labels")
    try:
        t.write("t0", 0)
        expect(False, "write on target refused")
    except TapeError:
        pass
    # The bench holds RECORDS records: one more is refused, not truncated.
    from nlverif.tape import RECORDS
    t = Tape(bfms())
    for _ in range(RECORDS - 1):
        t.delay(1)
    t.end()
    expect(len(t.assemble().split()) == 16 * (RECORDS + 1), "a full tape assembles")
    t = Tape(bfms())
    for _ in range(RECORDS):
        t.delay(1)
    t.end()
    try:
        t.assemble()
        expect(False, "a tape past RECORDS is refused")
    except TapeError:
        pass
    t = Tape(bfms())
    t.ready("s0", "r", "await_b")
    t.release_w("s0", after_r=1, from_beat=1)
    t.release_w("t0", after_r=2)
    expect([r[2:4] for r in t.records] == [[3, 5], [4, 1], [5, 0], [19, 1], [20, 1], [61, 2]],
           "await_b and W release encoding")
    for bad in (lambda: t.ready("s0", "b", "await_b"), lambda: t.release_w("t0", 1, 1)):
        try:
            bad()
            expect(False, "illegal liveness control refused")
        except TapeError:
            pass


def case(**target):
    t0 = dict(kind="axi", bus_bytes=4)
    t0.update(target)
    return dict(model=dict(routes={"s0": [("t0", 0x1000, 0x1000)]}, targets={"t0": t0}))


def push(t, ch, tag, a, n=0, id=1):
    return dict(t=t, k="push", p="s0", ch=ch, tag=tag, a=a, len=n, sz=2, bu=1, id=id, lk=0,
                ca=0, pr=0, qo=0)


def hs(t, port, ch, **fields):
    return dict(t=t, k="hs", p=port, ch=ch, **fields)


def req(t, port, ch, a, n=0, tag=None, id=1):
    e = hs(t, port, ch, a=a, len=n, sz=2, bu=1, id=id, lk=0, ca=0, pr=0, qo=0)
    if tag is not None:
        e["tag"] = tag
    return e


def number(ev):
    for n, e in enumerate(ev):
        e["_n"] = n
    return ev


def log(*, data="44332211", target_data="44332211", extra_b=False, resp=0, unmapped_resp=3):
    """A mapped single-beat write and an unmapped read (local DECERR)."""
    ev = [
        push(1, "w", 1, "1004"),
        req(2, "s0", "aw", "1004", tag=1),
        hs(3, "s0", "w", tag=1, d=data, s="f", l=1),
        req(4, "t0", "aw", "4"),
        hs(5, "t0", "w", d=target_data, s="f", l=1),
        hs(6, "t0", "b", id=1, r=resp),
        hs(7, "s0", "b", id=1, r=resp),
        push(8, "r", 2, "3000", id=2),
        req(9, "s0", "ar", "3000", tag=2, id=2),
        hs(10, "s0", "r", id=2, d="0", r=unmapped_resp, l=1),
    ]
    if extra_b:
        ev.append(hs(11, "s0", "b", id=1, r=0))
    return number(ev)


def kinds(events_, tape=None, bench_meta=None, model_case=None):
    tape = tape or dict(expect={}, checks=[], labels=["<none>"], marks=["<none>"])
    meta = bench_meta or dict(bfms=bfms(), streams=[])
    r = Checker(model_case or case(), meta, tape).run(events_)
    return {m["kind"] for m in r["mismatches"]}, r


def changed(index, **fields):
    ev = log()
    ev[index] = dict(ev[index], **fields)
    return number(ev)


def dropped(index):
    return number([e for i, e in enumerate(log()) if i != index])


def appended(*extra):
    return number(log() + list(extra))


def read2(lasts):
    """The unmapped read as a two-beat burst with the given RLAST values."""
    ev = log()[:7] + [push(8, "r", 2, "3000", 1, id=2), req(9, "s0", "ar", "3000", 1, tag=2, id=2)]
    ev += [hs(10 + i, "s0", "r", id=2, d="0", r=3, l=last) for i, last in enumerate(lasts)]
    return number(ev)


def wide_target_write():
    """The target sees two words for a one-word source write."""
    ev = log()
    ev[3] = req(4, "t0", "aw", "4", 1)
    ev[4] = hs(5, "t0", "w", d="44332211", s="f", l=0)
    ev.insert(5, hs(5, "t0", "w", d="0", s="0", l=1))
    return number(ev)


def local_write_at_target():
    """A write crossing 4 KiB is answered by the network (SLVERR) and still
    reaches the target."""
    return number([push(1, "w", 1, "1ffc", 1), req(2, "s0", "aw", "1ffc", 1, tag=1),
                   hs(3, "s0", "w", tag=1, d="1", s="f", l=0),
                   hs(4, "s0", "w", tag=1, d="2", s="f", l=1),
                   req(5, "t0", "aw", "ffc"), hs(6, "t0", "w", d="1", s="f", l=1),
                   hs(7, "t0", "b", id=1, r=0), hs(8, "s0", "b", id=1, r=2)])


def split_overlap():
    """One source burst reaches a downsize target as two pieces; the second
    starts before the first is answered."""
    return number([push(1, "w", 1, "1004", 1), req(2, "s0", "aw", "1004", 1, tag=1),
                   hs(3, "s0", "w", tag=1, d="1", s="f", l=0),
                   hs(4, "s0", "w", tag=1, d="2", s="f", l=1),
                   req(5, "t0", "aw", "4"), req(6, "t0", "aw", "8"),
                   hs(7, "t0", "w", d="1", s="f", l=1), hs(8, "t0", "w", d="2", s="f", l=1),
                   hs(9, "t0", "b", id=1, r=0), hs(10, "t0", "b", id=1, r=0),
                   hs(11, "s0", "b", id=1, r=0)])


def first_check():
    ev = log()
    ev.insert(3, dict(t=3, k="mk", m=1, v=0))
    return number(ev), dict(expect={}, labels=["<none>"], marks=["<none>", "m1"],
                            checks=[dict(kind="first", after_mark="m1", port="t0", ch="aw",
                                         addr=0x999)])


def stream():
    meta = dict(bfms=dict(bfms(), u=dict(index=2, kind="stream")),
                streams=[dict(name="u", fields=[("address", 32), ("count", 8)])])
    ev = log()
    ev.insert(3, hs(3, "u", "x", x="%x" % ((0x1000 << 8) | 4)))
    tape = dict(expect={}, labels=["<none>"], marks=["<none>"],
                checks=[dict(kind="stream_beats", stream="u", port="s0", dir="w")])
    return number(ev), tape, meta


def probe_log(winner=0, reach=()):
    """One fixed-level arbiter group of two candidates: candidate 0 (flow 0,
    level 1, pressure 1) requests alone with its head, ends its packet and is granted."""
    fields = ([("valid", 1)] * 2 + [("last", 1)] * 2 + [("flow", 2)] * 2 + [("first", 1)] * 2
              + [("level", 7)] * 2 + [("eligible", 1)] * 2
              + [("ready", 1), ("winner", 1), ("held", 1), ("transfer", 1), ("release", 1)])
    values = [1, 0, 1, 0, 0, 2, 1, 0, 1, 0, 1, 0, 1, winner, 0, 1, 1]
    x = 0
    for (_, width), v in zip(fields, values):
        x = (x << width) | v
    meta = dict(bfms=bfms(), streams=[], probes=[dict(
        name="arbiter/a", kind="arbiter", graph="request", node="m", output="endpoint/t0",
        candidates=["node/a", "node/b"], policy="fifo", count=2, rank=[], port="q0",
        fields=fields)])
    model_case = case()
    model_case["probe_checks"] = dict(flow_levels={0: 1, 1: 7, 2: 7, 3: 1}, reach=list(reach))
    ev = log()
    ev.insert(1, dict(t=1, c=1, k="pr", p="q0", x="%x" % x))
    return number(ev), None, meta, model_case


def response_probe_log(rows):
    """A response-level probe with explicit pending and three-bit level fields."""
    fields = [("pending", 1), ("level", 3)]
    meta = dict(bfms=bfms(), streams=[], probes=[dict(
        name="target/t0/response", kind="response_level", port="q0", fields=fields)])
    model_case = case()
    model_case["probe_checks"] = dict(reach=("frozen_low",))
    ev = [dict(t=i + 1, c=i + 1, k="pr", p="q0", x="%x" % ((pending << 3) | level))
          for i, (pending, level) in enumerate(rows)]
    return number(ev), None, meta, model_case


def checks(*spec):
    return dict(expect={}, labels=["<none>"], marks=["<none>"], checks=list(spec))


def rate_log(gap):
    """A four-beat write burst at the target, one beat every gap cycles."""
    ev = [push(1, "w", 1, "1000", 3), req(2, "s0", "aw", "1000", 3, tag=1)]
    ev += [dict(hs(3 + i, "s0", "w", tag=1, d="1", s="f", l=int(i == 3)), c=3 + i)
           for i in range(4)]
    ev += [req(8, "t0", "aw", "0", 3)]
    ev += [dict(hs(9 + gap * i, "t0", "w", d="1", s="f", l=int(i == 3)), c=9 + gap * i)
           for i in range(4)]
    ev += [hs(30, "t0", "b", id=1, r=0), hs(31, "s0", "b", id=1, r=0)]
    return number(ev)


def interleaved(ids=(2, 3), order=(0, 1, 0, 1), lasts=(0, 0, 1, 1), lens=(1, 1)):
    """Two unmapped reads (IDs `ids`, AxLEN `lens`) whose R beats arrive by
    `order` (the read of each beat) with RLAST values `lasts`."""
    ev = [push(1, "r", 1, "3000", lens[0], id=ids[0]),
          req(2, "s0", "ar", "3000", lens[0], tag=1, id=ids[0]),
          push(3, "r", 2, "3100", lens[1], id=ids[1]),
          req(4, "s0", "ar", "3100", lens[1], tag=2, id=ids[1])]
    ev += [hs(5 + k, "s0", "r", id=ids[i], d="0", r=3, l=last)
           for k, (i, last) in enumerate(zip(order, lasts))]
    return number(ev)


def dedicated_bfms(kind):
    return {"s0": dict(index=0, kind="axi_master", size=2, data_bits=32, lite=False),
            "t0": dict(index=1, kind=kind, size=2, data_bits=32, lite=False)}


def dedicated(kind, wdata="44332211", rdata="0", size=0x1000, read_at="3008", read_r=0):
    """A one-beat write to 0x1004 and a one-beat read through an APB or AHB
    target whose window is [0x1000, 0x1000 + size)."""
    meta = dict(bfms=dedicated_bfms(kind + "_slave"), streams=[])
    model_case = case(kind=kind, attributes="amba", engine_bytes=4)
    model_case["model"]["routes"]["s0"] = [("t0", 0x1000, size)]
    ev = [push(1, "w", 1, "1004"), req(2, "s0", "aw", "1004", tag=1),
          hs(3, "s0", "w", tag=1, d="44332211", s="f", l=1)]
    if kind == "apb":
        ev += [dict(t=4, k="hs", p="t0", ch="apb", a="4", w=1, d=wdata, s="f", pr=0, r=0)]
    else:
        ev += [dict(t=4, k="ahb_addr", p="t0", a="4", w=1, sz=2, bu=0, tr=2, pr=1, lk=0),
               dict(t=5, k="ahb_data", p="t0", a="4", w=1, d=wdata, r=0)]
    ev += [hs(6, "s0", "b", id=1, r=0), push(7, "r", 2, read_at[:-3] + read_at[-3:], id=2),
           req(8, "s0", "ar", read_at, tag=2, id=2)]
    if int(read_at, 16) < 0x1000 + size:
        local = "%x" % (int(read_at, 16) - 0x1000)
        if kind == "apb":
            ev += [dict(t=9, k="hs", p="t0", ch="apb", a=local, w=0, d=rdata, s="f", pr=0, r=0)]
        else:
            ev += [dict(t=9, k="ahb_addr", p="t0", a=local, w=0, sz=2, bu=0, tr=2, pr=1, lk=0),
                   dict(t=9.5, k="ahb_data", p="t0", a=local, w=0, d=rdata, r=0)]
    ev += [hs(10, "s0", "r", id=2, d="0", r=read_r, l=1)]
    return number(ev), None, meta, model_case


def oracle_case():
    model_case = case()
    model_case["oracle"] = lambda checker, events, sources, targets: [dict(detail="seeded")]
    return model_case


def late_target():
    """The source B (t=7) precedes the target access it covers."""
    ev = log()
    for i, t in ((3, 7.5), (4, 7.6), (5, 7.7)):
        ev[i] = dict(ev[i], t=t)
    return number(sorted(ev, key=lambda e: e["t"]))


def unknown_field():
    ev = log()
    ev[5] = events.parse_line('{"t":6,"k":"hs","p":"t0","ch":"b","id":1,"r":x}', 5)
    return number(ev)


def read_user(source_user=3):
    meta = dict(bfms=bfms(), streams=[])
    meta["bfms"]["s0"]["response_user_bits"] = 2
    ev = number([push(1, "r", 1, "1004"), req(2, "s0", "ar", "1004", tag=1),
                 req(3, "t0", "ar", "4"), hs(4, "t0", "r", id=1, d="0", r=0, l=1, u="3"),
                 hs(5, "s0", "r", id=1, d="0", r=0, l=1, u=format(source_user, "x"))])
    return ev, None, meta


# Every offline mismatch kind with a seeded defect that must raise it: the
# registry_contracts evidence for the offline categories.
OFFLINE = {
    "SOURCE_UNANSWERED": lambda: kinds(dropped(6)),
    "SOURCE_EXTRA_B": lambda: kinds(log(extra_b=True)),
    "SOURCE_EXTRA_R": lambda: kinds(appended(hs(11, "s0", "r", id=3, d="0", r=0, l=1))),
    "SOURCE_B_RESP": lambda: kinds(changed(6, r=2)),
    "SOURCE_R_RESP": lambda: kinds(log(unmapped_resp=0)),
    "SOURCE_R_DATA": lambda: kinds(changed(9, d="5")),
    "SOURCE_R_USER": lambda: kinds(*read_user(0)),
    "SOURCE_R_COUNT": lambda: kinds(read2([1])),
    "SOURCE_R_LAST": lambda: kinds(read2([0, 0])),
    "TARGET_UNATTRIBUTED": lambda: kinds(changed(3, a="800")),
    "TARGET_EXTRA_B": lambda: kinds(appended(hs(11, "t0", "b", id=3, r=0))),
    "TARGET_EXTRA_R": lambda: kinds(appended(hs(11, "t0", "r", id=3, d="0", r=0, l=1))),
    "TARGET_FOOTPRINT": lambda: kinds(wide_target_write()),
    "TARGET_WRITE_BYTES": lambda: kinds(log(target_data="44332210")),
    "TARGET_DESCRIPTOR": lambda: kinds(changed(3, sz=1, len=1),
                                       model_case=case(narrow_single=True)),
    "TARGET_ACCESS": lambda: kinds(local_write_at_target()),
    "TARGET_PROT": lambda: kinds(changed(3, pr=1)),
    "TARGET_ORDER": lambda: kinds(split_overlap(), model_case=case(kind="downsize")),
    "ORDER_INTERPOSE": lambda: kinds(*first_check()),
    "STREAM_FIELD": lambda: kinds(*stream()),
    "EVENT_X": lambda: kinds(unknown_field()),
    "ACCOUNTING": lambda: kinds(appended(req(11, "s0", "aw", "1004", tag=9))),
    "PROBE_GRANT": lambda: kinds(*probe_log(winner=1)),
    "TARGET_UNSTROBED_DATA": lambda: kinds(changed(4, s="e")),
    "SOURCE_EARLY": lambda: kinds(late_target()),
    "TARGET_ILLEGAL": lambda: kinds(changed(3, bu=3)),
    "ORACLE": lambda: kinds(log(), model_case=oracle_case()),
    "RATE_LOW": lambda: kinds(rate_log(2), tape=checks(dict(kind="rate", port="t0", ch="w",
                                                             per="burst", min=1.0))),
    "REACH_MISS": lambda: kinds(log(), tape=checks(dict(kind="reach", port="s0", dir="w",
                                                         inflight=2))),
}
OFFLINE_PROVEN = sorted(OFFLINE)


def single_transfer_read(source_kind, source_bytes, target_bytes, offset, drop=None, extra=False):
    """A single-transfer read and the APB words holding its aligned bytes."""
    size = source_bytes.bit_length() - 1
    aligned = offset // source_bytes * source_bytes
    first = aligned // target_bytes * target_bytes
    last = (aligned + source_bytes - 1) // target_bytes * target_bytes
    words = list(range(first, last + 1, target_bytes))
    if drop is not None:
        words.pop(drop)
    if extra:
        words.append(last + target_bytes)
    meta = dict(bfms={
        "s0": dict(index=0, kind=source_kind, size=size, data_bits=source_bytes * 8, lite=True),
        "t0": dict(index=1, kind="apb_slave", size=target_bytes.bit_length() - 1,
                   data_bits=target_bytes * 8, lite=False)}, streams=[])
    model_case = case(kind="apb", bus_bytes=target_bytes)
    ev = [dict(push(1, "r", 1, "%x" % (0x1000 + offset)), sz=size),
          dict(req(2, "s0", "ar", "%x" % (0x1000 + offset), tag=1), sz=size)]
    ev += [dict(t=3 + i, k="hs", p="t0", ch="apb", a="%x" % word,
                w=0, d="0", s="0", pr=0, r=0) for i, word in enumerate(words)]
    ev += [hs(20, "s0", "r", id=1, d="0", r=0, l=1)]
    return number(ev), None, meta, model_case


def test_single_transfer_read_footprint():
    # A source reads only the target words holding its own aligned bytes;
    # narrower or wider adjacent controls use the same geometric rule.
    offsets = (0, 1, 2, 3, 4, 5, 6, 7, 0x3F, 0x40, 0xFFE, 0xFFF)
    for source_kind in ("apb_master", "axi_master"):
        for source_bytes, target_bytes in ((2, 2), (2, 4), (4, 2), (4, 4), (8, 2), (8, 4)):
            for offset in offsets:
                label = (source_kind, source_bytes, target_bytes, offset)
                got, r = kinds(*single_transfer_read(source_kind, source_bytes, target_bytes, offset))
                expect(not got and r["target_transactions"] ==
                       (source_bytes + target_bytes - 1) // target_bytes,
                       f"single-transfer read {label}: {got}")
                got, _ = kinds(*single_transfer_read(source_kind, source_bytes, target_bytes,
                                                     offset, drop=-1))
                expect("TARGET_FOOTPRINT" in got, f"single-transfer missing word {label}: {got}")
                got, _ = kinds(*single_transfer_read(source_kind, source_bytes, target_bytes,
                                                     offset, extra=True))
                expect("TARGET_UNATTRIBUTED" in got, f"single-transfer extra word {label}: {got}")


def test_physical_apb_read_errors():
    for word_bytes in (1, 2):
        size = word_bytes.bit_length() - 1
        meta = dict(bfms=bfms(), streams=[])
        meta["bfms"]["t0"].update(kind="apb_slave", data_bits=8 * word_bytes, size=size)
        ev = [dict(push(1, "r", 1, "1000", 1), sz=size),
              dict(req(2, "s0", "ar", "1000", 1, tag=1), sz=size),
              dict(t=3, k="hs", p="t0", ch="apb", a="0", w=0, d="0", s="0", pr=0, r=2),
              hs(4, "s0", "r", id=1, d="0", r=2, l=0),
              dict(t=5, k="hs", p="t0", ch="apb", a=format(word_bytes, "x"),
                   w=0, d="0", s="0", pr=0, r=0),
              hs(6, "s0", "r", id=1, d="0", r=0, l=1)]
        model_case = case(kind="apb", bus_bytes=word_bytes, engine_bytes=4)
        got, _ = kinds(number(ev), bench_meta=meta, model_case=model_case)
        expect(not got, f"APB{8 * word_bytes} independent read responses: {got}")
        ev[-1]["r"] = 2
        got, _ = kinds(number(ev), bench_meta=meta, model_case=model_case)
        expect("SOURCE_R_RESP" in got, f"APB{8 * word_bytes} successful suffix polluted: {got}")



def test_source_error_encoding():
    for source_kind in ("apb_master", "axi_master"):
        expected = C.SLVERR if source_kind == "apb_master" else C.DECERR
        for direction in ("w", "r"):
            for local in (False, True):
                meta = dict(bfms=bfms(), streams=[])
                meta["bfms"]["s0"]["kind"] = source_kind
                address = "8000" if local else "1000"
                ev = [push(1, direction, 1, address),
                      req(2, "s0", "aw" if direction == "w" else "ar", address, tag=1)]
                if direction == "w":
                    ev.append(hs(3, "s0", "w", tag=1, d="0", s="f", l=1))
                if not local:
                    ev.append(req(4, "t0", "aw" if direction == "w" else "ar", "0"))
                    if direction == "w":
                        ev += [hs(5, "t0", "w", d="0", s="f", l=1),
                               hs(6, "t0", "b", id=1, r=C.DECERR)]
                    else:
                        ev.append(hs(6, "t0", "r", id=1, d="0", r=C.DECERR, l=1))
                ev.append(hs(7, "s0", "b" if direction == "w" else "r", id=1,
                             r=expected, **({} if direction == "w" else dict(d="0", l=1))))
                label = (source_kind, direction, local)
                got, _ = kinds(number(ev), bench_meta=meta, model_case=case())
                expect(not got, f"source error encoding {label}: {got}")
                for wrong in (C.OKAY, C.DECERR if source_kind == "apb_master" else C.SLVERR):
                    bad = [dict(e) for e in ev]
                    bad[-1]["r"] = wrong
                    got, _ = kinds(number(bad), bench_meta=meta, model_case=case())
                    rule = "SOURCE_B_RESP" if direction == "w" else "SOURCE_R_RESP"
                    expect(rule in got, f"wrong source error encoding {label}/{wrong}: {got}")


def test_checker():
    id_case = case(ids=True, id_match=False)
    id_case["dut"] = dict(recipe="axi4_outlet")
    id_meta = dict(bfms=bfms(), streams=[])
    id_meta["bfms"]["t0"]["id_bits"] = 4
    id_trace = log()
    id_trace[3] = dict(id_trace[3], id=8)
    id_trace[5] = dict(id_trace[5], id=8)
    got, _ = kinds(number(id_trace), bench_meta=id_meta, model_case=id_case)
    expect(not got, f"correct target ID: {got}")
    wrong_id = log()
    wrong_id[3] = dict(wrong_id[3], id=7)
    wrong_id[5] = dict(wrong_id[5], id=7)
    got, _ = kinds(number(wrong_id), bench_meta=id_meta, model_case=id_case)
    expect("TARGET_PROT" in got, f"target ID cannot be exempted: {got}")
    test_single_transfer_read_footprint()
    for trace, tag, expectation, mismatch in (
            (changed(6, r=2), "1", {"skip": True}, "SOURCE_B_RESP"),
            (log(target_data="44332210"), "1", {"bytes": "skip"}, "TARGET_WRITE_BYTES"),
            (changed(9, d="5"), "2", {"data": "skip"}, "SOURCE_R_DATA"),
            (log(unmapped_resp=0), "2", {"target": "any"}, "SOURCE_R_RESP"),
            (changed(9, d="5"), "2", {"target": "any"}, "SOURCE_R_DATA")):
        tape = dict(expect={tag: expectation}, checks=[], labels=["<none>"], marks=["<none>"])
        got, _ = kinds(trace, tape=tape)
        expect(mismatch in got, f"transaction expectations preserve {mismatch}: {got}")
    test_physical_apb_read_errors()
    got, _ = kinds(*read_user())
    expect(not got, f"target response user reaches its source: {got}")
    e = events.parse_line('{"t":1,"k":"hs","p":"s0","ch":"r","u":"x"}', 0)
    expect(e.get("_x"), "unknown response user is an event error")
    k, r = kinds(log())
    expect(not k and not r["infra"] and r["issued"] == 2 and r["completed"] == 2,
           f"clean log: {k}")
    # Event times past 2^31 ps (the BFMs print 64-bit real times): the same
    # log shifted there, read back through the event parser, stays clean;
    # attribution still orders by time (the target write before the source B).
    late = [events.parse_line(json.dumps(dict(e, t=e["t"] + 3_000_000_000)), n)
            for n, e in enumerate(log())]
    k, r = kinds(number(late))
    expect(late[0]["t"] > 2 ** 31 and not k and r["completed"] == 2, f"times past 2^31: {k}")
    swapped = [dict(e) for e in late]
    swapped[5]["t"], swapped[6]["t"] = swapped[6]["t"], swapped[5]["t"] - 1
    k, _ = kinds(number(sorted(swapped, key=lambda e: e["t"])))
    expect("SOURCE_EARLY" in k, f"order past 2^31: {k}")
    # R beats of different IDs interleave legally; the same pattern within
    # one ID, a later burst of an ID first, or RLAST off the beat count fail.
    k, r = kinds(interleaved())
    expect(not k and r["completed"] == 2, f"interleaved IDs: {k}")
    k, _ = kinds(interleaved(ids=(2, 2)))
    expect("SOURCE_R_LAST" in k, f"one ID interleaved: {k}")
    k, _ = kinds(interleaved(ids=(2, 2), order=(1, 0, 0), lasts=(1, 0, 1), lens=(1, 0)))
    expect("SOURCE_R_LAST" in k, f"one ID out of issue order: {k}")
    k, _ = kinds(interleaved(lasts=(0, 1, 0, 1)))
    expect("SOURCE_R_LAST" in k, f"RLAST misplaced in interleaving: {k}")
    for kind, run in OFFLINE.items():
        got, _ = run()
        expect(kind in got, f"offline {kind} not raised: {sorted(got)}")
    expect(set(OFFLINE) == set(C.MISMATCHES), "every mismatch kind has a seeded defect")
    # The direction qualifier names the failing direction only.
    _, r = kinds(changed(3, a="800"))
    found = faults.hits(dict(check=r))
    expect("TARGET_UNATTRIBUTED:w" in found and "TARGET_UNATTRIBUTED:r" not in found,
           "direction qualifier")
    # A B the target sends without a request has its own kind.
    got, _ = kinds(appended(hs(11, "t0", "b", id=3, r=0)))
    expect("TARGET_UNATTRIBUTED" not in got, "extra target B is not an attribution failure")
    # target=any owns only accesses inside its footprint and lifetime.
    anyw = dict(expect={"1": {"target": "any"}}, checks=[], labels=["<none>"], marks=["<none>"])
    got, _ = kinds(log(), tape=anyw)
    expect(not got, f"target=any keeps its own access: {got}")
    got, _ = kinds(changed(3, a="800"), tape=anyw)
    expect("TARGET_UNATTRIBUTED" in got, f"target=any does not absorb a foreign word: {got}")
    def second_access(t):
        ev = log()
        ev += [req(t, "t0", "aw", "4"), hs(t + 0.1, "t0", "w", d="44332211", s="f", l=1),
               hs(t + 0.2, "t0", "b", id=1, r=0)]
        return number(sorted(ev, key=lambda e: e["t"]))
    got, _ = kinds(second_access(5.5), tape=anyw)
    expect(not got, f"target=any owns a second access in its lifetime: {got}")
    got, _ = kinds(second_access(7.5), tape=anyw)
    expect("TARGET_UNATTRIBUTED" in got, f"target=any does not own a later access: {got}")
    # With late_access declared (a target timeout whose completion keeps
    # visiting the word) the same later access is owned.
    anyw_late = dict(expect={"1": {"target": "any", "late_access": True}}, checks=[],
                     labels=["<none>"], marks=["<none>"])
    got, _ = kinds(second_access(7.5), tape=anyw_late)
    expect(not got, f"late_access owns an access after the answer: {got}")
    # A stated policy the generated arbiter does not have is a grant failure.
    ev, _, meta, mc = probe_log()
    mc["probe_checks"]["policies"] = {"request:m:endpoint/t0": ("round_robin", None)}
    got, _ = kinds(ev, None, meta, mc)
    expect("PROBE_GRANT" in got, f"stated policy differs: {got}")
    # APB and AHB targets: one-word transactions, attribution, bytes, data.
    for kind in ("apb", "ahb"):
        got, r = kinds(*dedicated(kind, read_at="1008"))
        expect(not got and r["target_transactions"] == 2, f"{kind} clean: {got}")
        got, _ = kinds(*dedicated(kind, wdata="44332210", read_at="1008"))
        expect("TARGET_WRITE_BYTES" in got, f"{kind} write byte: {got}")
        got, _ = kinds(*dedicated(kind, rdata="5", read_at="1008"))
        expect("SOURCE_R_DATA" in got, f"{kind} read data: {got}")
    # A sub-page window owns its page: a read of the remainder answers
    # SLVERR (DECERR with error codes) with zero data and no target access.
    got, _ = kinds(*dedicated("apb", size=0x100, read_at="1100", read_r=3))
    expect(not got, f"sub-page remainder read: {got}")
    got, _ = kinds(*dedicated("apb", size=0x100, read_at="1100", read_r=0))
    expect("SOURCE_R_RESP" in got, f"sub-page remainder must fail: {got}")
    # Probe reach: a required level decision that never happened.
    got, _ = kinds(*probe_log(reach=("held_higher",)))
    expect("REACH_MISS" in got, f"probe reach: {got}")
    got, r = kinds(*response_probe_log([(1, 0), (1, 0)]))
    expect(not got and r["probes"]["target/t0/response"]["frozen_low"] == 2,
           f"response level stays frozen: {got}")
    got, _ = kinds(*response_probe_log([(1, 0), (1, 7)]))
    expect("PROBE_GRANT" in got, f"response level changed while pending: {got}")
    got, _ = kinds(*response_probe_log([(0, 0)]))
    expect("REACH_MISS" in got, f"response freeze reach: {got}")
    # Rates: one beat per cycle passes a full-rate gate, one in two fails it.
    gate = checks(dict(kind="rate", port="t0", ch="w", per="burst", min=1.0))
    got, r = kinds(rate_log(1), tape=gate)
    expect(not got and r["rates"][0]["lowest"] == 1.0, f"full rate burst: {got}")
    got, _ = kinds(rate_log(2), tape=gate)
    expect("RATE_LOW" in got, f"half rate burst: {got}")
    # Each source beat reflects only its contributing target words.
    def mapped_read():
        return number([push(1, "r", 1, "1004", 1), req(2, "s0", "ar", "1004", 1, tag=1),
                       req(3, "t0", "ar", "4", 1),
                       hs(4, "t0", "r", id=1, d="0", r=0, l=0), hs(5, "t0", "r", id=1, d="0", r=2, l=1),
                       hs(6, "s0", "r", id=1, d="0", r=0, l=0), hs(7, "s0", "r", id=1, d="0", r=2, l=1)])
    got, _ = kinds(mapped_read())
    expect(not got, f"per-beat codes pass without whole: {got}")
    polluted = mapped_read()
    polluted[-2]["r"] = 2
    got, _ = kinds(polluted)
    expect("SOURCE_R_RESP" in got, f"a failed neighbor must not pollute a successful beat: {got}")
    # Exact target bursts (pieces): the right list passes, another fails.
    for pieces, ok in (([[4, 0, 2, 1, 0, 0]], True), ([[8, 0, 2, 1, 0, 0]], False)):
        got, _ = kinds(log(), tape=dict(expect={"1": {"pieces": pieces}}, checks=[],
                                        labels=["<none>"], marks=["<none>"]))
        expect(("TARGET_DESCRIPTOR" not in got) == ok, f"pieces {pieces}: {got}")
    # A probe cycle that agrees with the policy model raises nothing.
    got, r = kinds(*probe_log())
    expect(not got and r["probes"]["arbiter/a"]["transfers"] == 1, f"probe replay clean: {got}")
    # Narrow-packet rule: a full-width FIXED beat is no narrow packet.
    got, _ = kinds(changed(3, bu=0), model_case=case(narrow_single=True))
    expect(not got, f"narrow_single leaves full-width beats alone: {got}")


def test_reset_propagation():
    meta = dict(bfms=dict(s0=dict(bfms()["s0"], period_ps=10000),
                          t0=dict(bfms()["t0"], period_ps=10000)), streams=[])
    base = [dict(t=1000, k="rst", v=0), dict(t=90000, k="rst", v=1),
            dict(t=200000, k="mk", m=0, v=0)]
    on_time = number(base + [dict(t=95000, k="rst", p="s0", v=1),
                             dict(t=95000, k="rst", p="t0", v=1)])
    _, r = kinds(on_time, bench_meta=meta)
    expect(not r["infra"], f"reset seen at the next edge: {r['infra']}")
    late = number(base + [dict(t=105000, k="rst", p="s0", v=1),
                          dict(t=95000, k="rst", p="t0", v=1)])
    _, r = kinds(late, bench_meta=meta)
    expect([i["port"] for i in r["infra"]] == ["s0"] and not r["mismatches"],
           "a BFM that sees the reset a clock late is an infrastructure error")


def test_events():
    e = events.parse_line('{"t":1,"k":"hs","ch":"b","id":x,"r":Z}', 1)
    expect(e.get("_x") == ["id", "r"] and e["id"] == "x", "bare X/Z decimal fields repaired")
    expect(events.parse_line('{"t":1,', 2)["k"] == "bad", "broken line kept as bad")
    e = events.parse_line('{"t":1,"k":"hs","ch":"x","x":"1f"}', 3)
    expect("_x" not in e and e["ch"] == "x", "a quoted x is a value")


def test_wrapper():
    from nlverif.run import validate_selection
    selection = dict(faults=[dict(id="data")], configs=[{}, dict(faults=[dict(id="address")])])
    validate_selection(selection, "data", True)
    validate_selection(dict(selection, negative_faults_all=True), "data,address", True)
    for spec, only, negative in ((selection, "absent", True), (selection, "data", False),
                                (selection, "address", True), (dict(configs=[]), "", False),
                                (dict(faults=[dict(id="data", designated=False)]), "", True)):
        try:
            validate_selection(spec, only, negative)
            expect(False, "fault selection cannot omit its required checks")
        except ValueError:
            pass
    comps = [dict(label="c", module="nocloom_x", ports=[
        dict(name="nl_a", width=1, input=True), dict(name="nl_y", width=4, input=False)])]
    fj = dict(modules=[dict(label="c", module="nocloom_x", core="nocloom_x__nlv",
                            sel_port="nl_nlv_fault_sel", variants=[dict(index=2, module="nocloom_x__nlvf2")])])
    text = faults.wrapper(comps, fj)
    expect("NLV_FAULT=%d" in text and "(nlv_sel == 8'd2) ? v2_nl_y : core_nl_y" in text, "wrapper")
    r = dict(status="FAIL", verdict=dict(first_rule="AXI.W.HOLD"), check=dict(mismatches=[]))
    expect(faults.classify(dict(expect=["AXI.W.HOLD"]), r) == "CAUGHT", "classify caught")
    expect(faults.classify(dict(expect=["X"]), r) == "CAUGHT_OTHER", "classify other")
    expect(faults.classify({}, dict(status="PASS")) == "SURVIVED", "classify survived")
    expect(faults.classify({}, dict(status="FAIL", verdict=dict(status="TIMEOUT"))) == "TIMEOUT",
           "timeout is not caught")
    hang = dict(status="FAIL", verdict=dict(first_rule="TAPE.WAIT_TIMEOUT", first_label="PROGRESS"),
                check=dict(mismatches=[]))
    expect(faults.classify(dict(expect=["TAPE.WAIT_TIMEOUT:PROGRESS"]), hang) == "CAUGHT",
           "labeled tape expectation")
    expect(faults.classify(dict(expect=["TAPE.WAIT_TIMEOUT:OTHER"]), hang) == "CAUGHT_OTHER",
           "another label is another hang")
    try:
        faults.classify(dict(id="f", expect=["TAPE.WAIT_TIMEOUT"]), hang)
        expect(False, "unlabeled TAPE expectation refused")
    except ValueError:
        pass
    # A premise named at a hang: the wait label survives as hang_rule.
    live = dict(status="FAIL", check=dict(mismatches=[]),
                verdict=dict(first_rule="LIVE.E2", hang_rule="TAPE.WAIT_TIMEOUT",
                             hang_label="E2_B_BEHIND_R"))
    both = ["LIVE.E2", "TAPE.WAIT_TIMEOUT:E2_B_BEHIND_R"]
    expect(faults.classify(dict(op="scenario", expect=both), live) == "CAUGHT",
           "scenario fault caught by premise and hang")
    expect(faults.classify(dict(expect=["TAPE.WAIT_TIMEOUT:E2_B_BEHIND_R"]), live) == "CAUGHT",
           "hang label kept behind a premise rule")
    bare = dict(status="FAIL", check=dict(mismatches=[]),
                verdict=dict(first_rule="TAPE.WAIT_TIMEOUT", first_label="E2_B_BEHIND_R",
                             hang_rule="TAPE.WAIT_TIMEOUT", hang_label="E2_B_BEHIND_R"))
    expect(faults.classify(dict(op="scenario", expect=both), bare) == "CAUGHT_OTHER",
           "scenario hang without the premise is not caught")


def test_bench(fixture, work):
    out = Path(work) / "lite"
    subprocess.run([fixture, "lite_target", out], check=True)
    comps = json.loads((out / "ports.json").read_text())
    spec = dict(nets={"front": [("nl_axi_", "s_")], "target": [("nl_axil_", "t_")]},
                bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
                      dict(name="t0", kind="axi_slave", component="target", endpoint="axil",
                           lite=True, sideband=("target", "status", "timed_out"))])
    text, meta = bench.generate(spec, comps)
    expect("nocloom_axil" not in text and "bfm_s0.push_write" in text, "bench text")
    expect(meta["bfms"]["t0"]["lite"] and meta["bfms"]["s0"]["size"] == 2, "bench meta")
    spec["bfms"].pop()
    try:
        bench.generate(spec, comps)
        expect(False, "undriven DUT inputs refused")
    except bench.BenchError:
        pass


def test_install_timeout(work):
    # A step past its limit fails the entry by name: the label reaches the entry's failure line.
    import install
    ctx = install.Context(argparse.Namespace(source="."), Path(work))
    try:
        ctx.run(["sleep", "5"], "slow_step", cwd=work, timeout=0.1)
        expect(False, "step past its limit refused")
    except AssertionError as error:
        expect(str(error) == "slow_step: no result within 0.1 s", f"timeout text: {error}")


def test_idle_function(work):
    """Every queue participates, including four-state values and the empty set."""
    directory = Path(work) / "idle-function"
    directory.mkdir(parents=True, exist_ok=True)
    masters = [dict(instance=f"m{i}") for i in range(256)]
    functions = "\n".join((bench.idle_function(masters),
                           bench.idle_function([]).replace("nlv_idle", "idle_empty"),
                           bench.idle_function(masters[-1:]).replace("nlv_idle", "idle_last")))
    instances = "\n".join(f"queue_counts #({i}) m{i}();" for i in range(256))
    source = directory / "tb.sv"
    source.write_text("""module queue_counts;
parameter ID = 0;
function [63:0] counter(input integer idx);
  counter = tb.pending[ID][idx - 10];
endfunction
endmodule
module tb;
reg [63:0] pending [0:255][0:1];
""" + instances + "\n" + functions + """
initial begin
  for (integer i = 0; i < 256; i = i + 1)
    for (integer q = 0; q < 2; q = q + 1) pending[i][q] = 0;
  if (idle_empty(0) !== 1'b1 || nlv_idle(0) !== 1'b1 || idle_last(0) !== 1'b1)
    $fatal(1, "empty or idle queues");
  for (integer i = 0; i < 256; i = i + 1) begin
    for (integer q = 0; q < 2; q = q + 1) begin
      pending[i][q] = 64'h8000000000000000;
      if (nlv_idle(0) !== 1'b0) $fatal(1, "busy queue %0d %0d", i, q);
      pending[i][q] = 64'bx;
      if (nlv_idle(0) !== 1'bx) $fatal(1, "unknown queue %0d %0d", i, q);
      pending[(i + 1) % 256][q] = 1;
      if (nlv_idle(0) !== 1'b0) $fatal(1, "busy dominates unknown %0d %0d", i, q);
      pending[(i + 1) % 256][q] = 0;
      pending[i][q] = 64'bz;
      if (nlv_idle(0) !== 1'bx) $fatal(1, "undriven queue %0d %0d", i, q);
      pending[i][q] = 0;
      if (nlv_idle(0) !== 1'b1) $fatal(1, "queue drained %0d %0d", i, q);
    end
  end
  pending[255][1] = 1;
  if (idle_last(0) !== 1'b0 || idle_empty(0) !== 1'b1) $fatal(1, "single initiator");
  $display("CHECK_PASS idle_function");
  $finish;
end
endmodule
""")
    binary = directory / "sim.vvp"
    for label, command in (("compile", ["iverilog", "-g2012", "-s", "tb", "-o", binary, source]),
                           ("run", ["vvp", binary])):
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        output = result.stdout + result.stderr
        (directory / (label + ".log")).write_text(output)
        expect(result.returncode == 0, f"idle function {label}: {output[-1000:]}")
        if result.returncode:
            return
    expect("CHECK_PASS idle_function" in output, "idle function reached completion")


def test_rule_selection(work):
    path = Path(__file__).with_name("rulecheck.py")
    result = subprocess.run([sys.executable, path, "--stub", path.with_name("sv") / "nlv_rule_stub.v",
                             "--work", Path(work) / "unknown-rule", "--only", "not_a_rule"],
                            capture_output=True, text=True, timeout=5)
    expect(result.returncode == 2 and "Unknown rule scenario" in result.stderr,
           "an unknown rule selection fails before simulation")


def test_throughput_selection(fixture, work):
    path = Path(__file__).resolve().parents[1] / "run_graph_clocked_throughput.py"
    for names, message in ((["not_a_variant"], "Unknown throughput variant"),
                           ([], "At least one throughput variant")):
        result = subprocess.run([sys.executable, path, "--cli", fixture,
                                 "--work", Path(work) / "unknown-throughput", "--variants", *names],
                                capture_output=True, text=True, timeout=5)
        expect(result.returncode == 2 and message in result.stderr,
               "an empty throughput selection fails before generation")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--work", required=True)
    a = p.parse_args()
    test_model()
    test_tape()
    test_checker()
    test_source_error_encoding()
    test_reset_propagation()
    test_events()
    test_wrapper()
    test_bench(a.fixture, a.work)
    test_idle_function(a.work)
    test_install_timeout(a.work)
    test_rule_selection(a.work)
    test_throughput_selection(a.fixture, a.work)
    print("SELFTEST", "FAIL" if FAILS else "PASS", len(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
