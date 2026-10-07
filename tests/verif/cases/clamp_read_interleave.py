"""A source wider than its AXI4 target, with reads of several IDs in flight, reads from a target with
several bursts in flight that alternates the read data of two IDs word by word, on the public
schema 10 path (CLI).

Source s0 (AXI4, four bursts in flight) reaches t0 (AXI4, 32 bits, four bursts in flight) over a
32-bit network, so its width clamp gathers target words into each source word, one gather per
source tag. Shapes: s0 of 64 bits (wide64) or 128 bits (wide128), and a second 64-bit source that
stays idle (two_sources: only s0 reads, so the IDs alternate at s0 and not only between sources),
and a 64-bit source over a 16-byte word (wide_word: a word that could gather beats of both reads).
Reads whose beats arrive together, reads the target alternates, and the same with pauses at the
target and a slow source must each return every beat with its read's RID and RLAST, the exact data
and OKAY. The offline checker judges every source beat against the target words of its read, paired
by RID with the oldest open read of that ID; the drain judges completion.

Negative control (RTL text fault): t0's RLAST input reads one for every beat (a word short of its
read's last then ends the read too soon)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

PARAMS = globals().get("PARAMS", {})
SHAPE = PARAMS.get("shape", "wide64")
SOURCE_BITS = 128 if SHAPE == "wide128" else 64
BEAT_BYTES = SOURCE_BITS // 8
SOURCES = ('s0', 's1') if SHAPE == "two_sources" else ('s0',)

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'clamp_interleave', 'address_width': 32,
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4',
                    'data_width': SOURCE_BITS, 'max_outstanding': 4} for n in SOURCES],
    'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': 4,
                 'mappings': [{'base': '0x0', 'size': '0x10000'}]}],
    'transport': ({'transport_word_bytes': 16} if SHAPE == "wide_word"
                  else {'data_width': 32}),
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def rlast_one(rtl, _map):
    """Every use of t0's RLAST input reads one."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', r"\g<1>1'd1", len(uses))]


CASE = dict(
    name="clamp_read_interleave_" + SHAPE,
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
    + [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=20000,
    faults=[
        dict(id="rlast_one", op="text", subs=rlast_one, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_DATA", "SOURCE_R_RESP", "AXI.R.LAST", "SOURCE_R_LAST",
                     "SOURCE_R_COUNT", "TAPE.DRAIN_TIMEOUT:drain", "SOURCE_UNANSWERED"]),
    ],
)


def scenario(t, seed):
    t.set("t0", "r_lat", 8)
    t.reset(4)
    t.delay(2)
    # Beats that arrive together: exact data with OKAY, for IDs that overlap in flight.
    t.mark("phase", 1)
    for k, beats in enumerate((2, 1, 4, 3)):
        t.read("s0", 0x1000 + 0x100 * k, beats=beats, id=k % 4, cache=3)
    t.drain(limit=4000, label="whole")
    # The target alternates IDs word by word as soon as both reads wait for R.
    t.mark("phase", 2)
    t.set("t0", "r_ilv", 1)
    t.set("t0", "r_lat", 16)
    for k, beats in enumerate((2, 2, 4, 3)):
        t.read("s0", 0x2000 + 0x100 * k, beats=beats, id=k % 4, cache=3)
    t.drain(limit=4000, label="alternate")
    # The same with pauses at the target and a slow source.
    t.mark("phase", 3)
    t.set("t0", "r_rate", 55)
    t.set("t0", "r_lat", 2)
    t.ready("s0", "r", "modulo", 3, 0)
    for k, beats in enumerate((1, 2, 3, 4)):
        t.read("s0", 0x3000 + 0x100 * k, beats=beats, id=k % 4, cache=3)
    t.drain(limit=4000, label="paced")
    # Long, unaligned, narrow and WRAP reads overlapping writes.
    t.mark("phase", 4)
    t.set("t0", "r_lat", 12)
    t.set("t0", "r_rate", 70)
    t.ready("s0", "r", "modulo", 2, 0)
    t.read("s0", 0x4000, beats=12, id=0, cache=3)
    t.write("s0", 0x4800, beats=3, id=1, cache=3)
    t.read("s0", 0x4100 + BEAT_BYTES // 2, beats=3, id=1, cache=3)
    t.read("s0", 0x4200, beats=4, size=2, id=2, cache=3)
    t.read("s0", 0x4300 + BEAT_BYTES, beats=4, burst="wrap", id=3, cache=3)
    t.write("s0", 0x4900, beats=2, id=2, cache=3)
    t.read("s0", 0x4404, beats=5, size=2, id=0, cache=3)
    t.drain(limit=4000, label="mixed")
    t.end()
