"""AXI4 targets with a read interleave size, on the public schema 10 path (CLI).

Sources s0 and s1 (AXI4, 32 bits, two ID bits, four bursts in flight) reach two 32-bit targets. In
the first configuration t0 (8 bytes, two words) alternates IDs only at a block's end and t1 (0)
answers one read to its end, both with four bursts in flight; in the second t0 and t1 (16 bytes,
four words) keep one burst each. The targets' R channels do just that (R_ILV), with R on a random
share of cycles and source R ready patterns; reads start inside blocks and at their edges, some
WRAP. The offline checker pairs every target R beat with the oldest open read of its RID and every
source beat with the oldest open burst of its ID, and judges data, RLAST and codes; the case's
oracle judges that no INCR read AR of a blocked target runs from inside a block across its top.

Negative controls: the outlet of t0 reads RID 0 for every beat (RTL text fault); with every read
starting on a block's edge the oracle sees nothing to judge (scenario fault)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

BASES = (0x0000, 0x8000)
SOURCES = ('s0', 's1')


def config(granules, bursts):
    return endpoint_ports({
        'schema_version': 10, 'name': 'granule', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4} for n in SOURCES],
        'targets': [{'name': f't{i}', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'max_outstanding': bursts,
                     'read_interleave_bytes': g,
                     'mappings': [{'base': hex(base), 'size': '0x8000'}]}
                    for i, (base, g) in enumerate(zip(BASES, granules))],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def recipe(granules, bursts):
    cfg = config(granules, bursts)
    return dict(dut=dict(source="cli", config=cfg),
                bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
                + [dict(name=f't{k}', kind="axi_slave", endpoint=f"target:t{k}", salt=k)
                   for k in range(2)],
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                           targets=nlconfig.targets(cfg, cache="amba", prot=True)),
                scenario_args=dict(granules=granules),
                oracle=reads_stop_at_blocks(granules))


def rid_zero(rtl, _map):
    """Every use of t0's RID input reads zero."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rid\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rid\b', r"\g<1>2'd0", len(uses))]


def reads_stop_at_blocks(granules):
    def oracle(checker, events, sources, targets):
        """An INCR read AR starting inside a block of its target stays inside it."""
        out, inside = [], {f't{i}': g for i, g in enumerate(granules) if g > 4}
        seen = 0
        for e in events:
            if e.get("k") != "hs" or e.get("ch") != "ar" or e.get("p") not in inside:
                continue
            if e.get("bu", 1) != 1:
                continue
            block, start = inside[e["p"]], int(e["a"], 16)
            end = start + (e["len"] + 1) * 4 - (start & 3)
            if start & (block - 1):
                seen += 1
                if end > (start | (block - 1)) + 1:
                    out.append(dict(detail="read crosses a block from inside", port=e["p"],
                                    addr=hex(start), len=e["len"], c=e.get("c")))
        if not seen:
            out.append(dict(detail="no read started inside a block"))
        return out[:8]
    return oracle


CASE = dict(
    name="target_read_granule",
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.OUTLET.FRAMING"],
    max_cycles=60000,
    faults=[
        dict(id="rid_ignored", op="text", subs=rid_zero, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_RESP", "SOURCE_R_DATA", "AXI.R.LAST", "SOURCE_R_LAST",
                     "SOURCE_R_COUNT", "TAPE.DRAIN_TIMEOUT:drain", "SOURCE_UNANSWERED"]),
        dict(id="no_start_inside", op="scenario", params=dict(aligned=True), expect=["ORACLE"],
             contract="AXI.TARGET.R_INTERLEAVE", note="the oracle needs reads starting in a block"),
    ],
    **recipe((8, 0), 4),
    configs=[dict(label=""), dict(label="one_burst", **recipe((16, 16), 1))],
)

STARTS = (0x00, 0x04, 0x0c, 0x18, 0x24)
BEATS = (1, 2, 4, 8, 3, 6, 5, 7)


def scenario(t, seed, granules, aligned=False):
    starts = (0,) * 5 if aligned else STARTS
    names = [f't{i}' for i in range(2)]
    for n, g in zip(names, granules):
        t.set(n, "r_ilv", g // 4)
        t.set(n, "r_lat", 2)
    t.reset(4)
    t.delay(2)
    # Known data, then reads of several IDs at once; responsive targets.
    t.mark("phase", 1)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for base in BASES:
                t.write(s, base + 0x1000 * si + 0x80 * k, beats=8, id=k % 4, cache=3)
    t.drain(limit=20000, label="writes")
    t.mark("phase", 2)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for base in BASES:
                t.read(s, base + 0x1000 * si + 0x80 * k + starts[k % 5], beats=BEATS[k],
                       id=(k + si) % 4, cache=3)
    t.drain(limit=20000, label="reads")
    # With R and B on a share of cycles, source ready patterns, WRAP and writes across.
    t.mark("phase", 3)
    for n in names:
        t.set(n, "r_rate", 55)
        t.set(n, "b_rate", 60)
    t.ready("s0", "r", "modulo", 3, 0)
    t.ready("s1", "b", "modulo", 4, 0)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for base in BASES:
                t.read(s, base + 0x1000 * si + 0x80 * k + starts[(k + 2) % 5], beats=BEATS[7 - k],
                       id=(k + 2 * si) % 4, cache=3)
                wrap = 0 if aligned else 4 * (k % 4)
                t.read(s, base + 0x1000 * si + 0x400 + 0x40 * k + wrap, beats=4,
                       burst="wrap", id=(k + si + 1) % 4, cache=3)
                t.write(s, base + 0x1000 * si + 0x800 + 0x40 * k, beats=BEATS[k], id=k % 4, cache=3)
    t.drain(limit=40000, label="drain")
    t.end()
