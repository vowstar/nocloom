"""A clamped AXI4 source with several bursts and read IDs reads targets of one burst each, on the
public schema 10 path (CLI). A narrow read-only target without an ID does not interleave, so it does
not make the source gather per read; a target as wide as the source does interleave its response.

A paced read of the wide target ends a segment at each pause, so its source output is free for the
read of the narrow target, whose words leave while the wide read is still open. The source takes
these words between the beats of the wide read and joins each read's words apart. The offline checker
judges the data, RLAST and codes of every source beat, and the drain the completion.

Shapes (sources; targets):
  single:  one 64-bit source; 32-bit read-only, 64-bit
  two:     two 64-bit sources; 32-bit read-only, 64-bit, 64-bit
  wider:   one 128-bit source; 32-bit and 64-bit read-only, 128-bit, 128-bit

Negative control (RTL text fault, shape two): response switch outputs release only at a packet's
last fragment. The narrow read then waits for the end of the wide one."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

PARAMS = globals().get("PARAMS", {})
SHAPE = PARAMS.get("shape", "single")
SOURCES, TARGETS = {
    "single": ((("s0", 64),), (("t0", 32, True), ("t1", 64, False))),
    "two": ((("s0", 64), ("s1", 64)), (("t0", 32, True), ("t1", 64, False), ("t2", 64, False))),
    "wider": ((("s0", 128),), (("t0", 32, True), ("t1", 64, True), ("t2", 128, False),
                               ("t3", 128, False))),
}[SHAPE]
BASES = tuple(0x8000 * i for i in range(len(TARGETS)))
NARROW = TARGETS[0][0]
WIDE = next(n for n, _, read_only in TARGETS if not read_only)
WIDE_BASE = BASES[[n for n, _, _ in TARGETS].index(WIDE)]

CONFIG = endpoint_ports({
    "schema_version": 10, "name": "one_burst", "address_width": 32,
    "arbitration": {"policy": "round_robin"},
    "reset_ports": [{"name": "reset_n"}], "test_ports": [{"name": "test"}],
    "clock_domains": [{"name": "main", "clock_port": "clk_main", "reset_port": "reset_n",
                       "test_port": "test"}],
    "initiators": [{"name": n, "clock_domain": "main", "protocol": "axi4", "data_width": w,
                    "max_outstanding": 4} for n, w in SOURCES],
    "targets": [{"name": n, "clock_domain": "main", "protocol": "axi4", "data_width": w,
                 "attributes": "amba", "max_outstanding": 1,
                 "mappings": [{"base": hex(base), "size": "0x8000"}],
                 **({"access": "read_only", "id_width": 0} if read_only else {})}
                for (n, w, read_only), base in zip(TARGETS, BASES)],
    "connectivity": "all",
    "output": {"backend": "verilog", "language": "verilog_2001"}}, id_width=2, len_width=8)


def release_at_last_only(rtl, map_):
    """Each response output's release reads its candidates' last fragment and not their segment end:
    release = transfer & tail, tail = an OR tree of (selected & end), end = last | cut."""
    assign = re.compile(
        r"^assign (nl_internal_v\d+) = \((nl_internal_v\d+) ([&|]) (nl_internal_v\d+)\);$", re.M)
    binary = {m[1]: (m[2], m[3], m[4]) for m in assign.finditer(rtl)}
    subs = []

    def visit(node):
        left, op, right = binary[node]
        if op == "|":
            visit(left)
            visit(right)
            return
        last, op, cut = binary[right]
        assert op == "|"
        subs.append((rf"^assign {right} = \({last} \| {cut}\);$", f"assign {right} = {last};", 1))

    for store in map_["network"]["storage"]:
        if store["resource"].startswith("graph/rsp/"):
            visit(binary["nl_internal_v" + store["release"].split("/")[1]][2])
    assert len(subs) == 6
    return subs


CASE = dict(
    name="clamp_one_burst_targets_" + SHAPE,
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n, _ in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _, _) in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=80000,
    seeds=[1, 5, 9],
    faults=[] if SHAPE != "two" else [
        dict(id="release_at_last_only", op="text", subs=release_at_last_only,
             contract="AXI.TARGET.R_INTERLEAVE", expect=["TAPE.WAIT_TIMEOUT:NARROW_PASSES_WIDE"]),
    ],
)


def passing(t):
    """The narrow read finishes while a slow read of the wide target stays open."""
    t.mark("phase", 1)
    source = SOURCES[0][0]
    t.set(WIDE, "r_rate", 8)
    t.set(WIDE, "r_lat", 2)
    t.set(NARROW, "r_rate", 100)
    t.set(NARROW, "r_lat", 2)
    t.read(source, WIDE_BASE, beats=32, id=0, cache=3)
    t.wait(WIDE, "r", ">=", 2, limit=400, label="WIDE_OPEN")
    t.read(source, 0x100, beats=2, id=1, cache=3)
    words = 2 * SOURCES[0][1] // TARGETS[0][1]
    t.wait(NARROW, "r", ">=", words, limit=120, label="NARROW_PASSES_WIDE")
    t.expect_now(WIDE, "r", "<", 32, "WIDE_STILL_OPEN")
    t.drain(limit=4000, label="passing")


def alternating(t, label, rounds, beats):
    """Each source keeps reads open at the narrow and the wide targets at once."""
    for r in range(rounds):
        for si, (source, _) in enumerate(SOURCES):
            for k in range(4):
                base = BASES[k % len(BASES)]
                t.read(source, base + 0x1000 * si + 0x100 * r + 0x20 * k, beats=beats, id=k % 4,
                       cache=3)
    t.drain(limit=12000, label=label)


def scenario(t, seed):
    for n, _, _ in TARGETS:
        t.set(n, "seed", 0x1357 * seed + ord(n[1]))
    t.reset(4)
    t.delay(2)
    passing(t)
    t.mark("phase", 2)
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 35)
        t.set(n, "r_lat", 2)
    alternating(t, "paced", 6, 6)
    t.mark("phase", 3)
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 60)
        t.set(n, "r_lat", 1)
    for source, _ in SOURCES:
        t.ready(source, "r", "modulo", 3, 0)
    alternating(t, "backpressure", 4, 3)
    # Writes of the wide targets between the reads.
    t.mark("phase", 4)
    for source, _ in SOURCES:
        t.ready(source, "r", "always")
    for r in range(4):
        for si, (source, width) in enumerate(SOURCES):
            step = width // 8
            for k in range(3):
                t.read(source, BASES[k % len(BASES)] + 0x1000 * si + 0x100 * r + 0x20 * step * k,
                       beats=4, id=k % 4, cache=3)
                t.write(source, WIDE_BASE + 0x1000 * si + 0x4000 + 0x80 * r + 0x10 * step * k,
                        beats=1 + k % 2, id=(k + 1) % 4, cache=3)
    t.drain(limit=12000, label="writes")
    t.end()
