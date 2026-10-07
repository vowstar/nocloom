"""AXI4 targets that interleave read data of different IDs beat by beat, on the public schema 10
path (CLI).

Sources s0 and s1 (AXI4, 32 bits, two ID bits, four bursts in flight) reach t0 and t1 (AXI4,
32 bits, four bursts in flight each, windows of 32 KiB). Both targets run their R channel with
R_ILV: every R beat goes to a ready read of another ID than the previous beat's when there is
one, so reads of different IDs and sources interleave beat by beat at each target and their
response segments (message.hpp point 17) meet at the response switch outputs. The sources first
write known data into both targets, then read it back with several IDs at once, against
responsive targets, then with R and B returned on a random share of cycles (R pauses that end
response segments) and source R and B ready patterns. The offline checker pairs every target R
beat with the oldest open read of its RID and every source beat with the oldest open burst of
its ID, and judges the data, RLAST and the codes; the monitors judge holds and unexpected
responses; the drain judges completion.

Negative control (RTL text fault): the outlet of t0 reads RID 0 for every beat (an R beat must
name its read by its RID)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

TARGETS = (('t0', 0x0000), ('t1', 0x8000))
SOURCES = ('s0', 's1')

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'interleave', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                    'max_outstanding': 4} for n in SOURCES],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': 4,
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]} for n, base in TARGETS],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def rid_zero(rtl, _map):
    """Every use of t0's RID input reads zero."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rid\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rid\b', r"\g<1>4'd0", len(uses))]


CASE = dict(
    name="target_interleave",
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET", "AXI.OUTLET.FRAMING"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _) in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
    faults=[
        dict(id="rid_ignored", op="text", subs=rid_zero, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_RESP", "SOURCE_R_DATA", "AXI.R.LAST", "SOURCE_R_LAST",
                     "SOURCE_R_COUNT", "TAPE.DRAIN_TIMEOUT:drain", "SOURCE_UNANSWERED"]),
    ],
)

BEATS = (1, 2, 4, 8, 3, 6, 5, 7)


def scenario(t, seed):
    for n, _ in TARGETS:
        t.set(n, "r_ilv", 1)
        t.set(n, "r_lat", 2)
    t.reset(4)
    t.delay(2)
    # Known data: each source writes its own region of both targets.
    t.mark("phase", 1)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for _, base in TARGETS:
                t.write(s, base + 0x1000 * si + 0x40 * k, beats=BEATS[k], id=k % 4, cache=3)
    for s in SOURCES:
        t.wait(s, "b", "==", 16, limit=20000, label="WRITES_DONE")
    # Reads of several IDs from both targets at once; responsive targets.
    t.mark("phase", 2)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for _, base in TARGETS:
                t.read(s, base + 0x1000 * si + 0x40 * k, beats=BEATS[k], id=(k + si) % 4, cache=3)
    for s in SOURCES:
        t.wait(s, "rlast", "==", 16, limit=20000, label="READS_DONE")
    # Again with R and B on a random share of cycles and source ready patterns.
    t.mark("phase", 3)
    for n, _ in TARGETS:
        t.set(n, "r_rate", 55)
        t.set(n, "b_rate", 60)
    t.ready("s0", "r", "modulo", 3, 0)
    t.ready("s1", "b", "modulo", 4, 0)
    for k in range(8):
        for si, s in enumerate(SOURCES):
            for _, base in TARGETS:
                t.read(s, base + 0x1000 * si + 0x40 * k, beats=BEATS[7 - k], id=(k + 2 * si) % 4,
                       cache=3)
                t.write(s, base + 0x1000 * si + 0x800 + 0x40 * k, beats=BEATS[k], id=k % 4, cache=3)
    t.drain(limit=40000, label="drain")
    t.end()
