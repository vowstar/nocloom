"""Two 64-bit AXI4 sources read two 32-bit AXI4 targets that keep four bursts in flight and alternate
read data word by word, on the public schema 10 path (CLI).

Each target alternates words of reads of both sources, so a response switch output must not stay
with one target between two words of a read: the other target's next word for the other source can
wait behind that target's front word. Both sources gather target words per read, so a target ends a
response segment toward them at every alternation. Every read must complete with the exact data,
across the target pacing seeds that exercise this circular wait.

Negative control (RTL text fault): the response switch outputs release only at a packet's last
fragment. Two outputs then wait for each other and the drain times out."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

SOURCES = ('s0', 's1')
TARGETS = ('t0', 't1')
BASES = (0x0, 0x8000)
BEAT_BYTES = 8

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'clamp_exchange', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 64,
                    'max_outstanding': 4} for n in SOURCES],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': 4,
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]}
                for n, base in zip(TARGETS, BASES)],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def release_at_last_only(rtl, map_):
    """Each response output's release reads its candidates' last fragment and not their segment end:
    release = transfer & tail, tail = OR of (selected & candidate end), end = last | cut."""
    assign = re.compile(r'^assign (nl_internal_v\d+) = \((nl_internal_v\d+) ([&|]) (nl_internal_v\d+)\);$',
                        re.M)
    binary = {m[1]: (m[2], m[3], m[4]) for m in assign.finditer(rtl)}
    subs = []
    for store in map_["network"]["storage"]:
        if not store["resource"].startswith("graph/rsp/"):
            continue
        _, _, tail = binary["nl_internal_v" + store["release"].split("/")[1]]
        for selected in binary[tail][0::2]:
            _, _, end = binary[selected]
            last, op, cut = binary[end]
            assert op == "|"
            subs.append((rf'^assign {end} = \({last} \| {cut}\);$', f'assign {end} = {last};', 1))
    assert len(subs) == 4
    return subs


CASE = dict(
    name="clamp_response_exchange",
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, n in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=40000,
    seeds=[9, 5, 7, 11, 12, 1],
    faults=[
        dict(id="release_at_last_only", op="text", subs=release_at_last_only,
             contract="AXI.TARGET.R_INTERLEAVE",
             expect=["TAPE.DRAIN_TIMEOUT:paced", "SOURCE_UNANSWERED"]),
    ],
)


def reads(t, shape_beats, round_):
    for si, source in enumerate(SOURCES):
        for base in BASES:
            for k, beats in enumerate(shape_beats):
                t.read(source, base + 0x1000 * si + 0x400 * round_ + 0x100 * k, beats=beats,
                       id=k % 4, cache=3)


def scenario(t, seed):
    for n in TARGETS:
        t.set(n, "r_lat", 6)
        t.set(n, "seed", 0x1357 * seed + ord(n[1]))
    t.reset(4)
    t.delay(2)
    # Whole bursts first: no alternation yet.
    t.mark("phase", 1)
    for si, source in enumerate(SOURCES):
        for base in BASES:
            for k, beats in enumerate((2, 1, 3)):
                t.read(source, base + 0x1000 * si + 0x100 * k, beats=beats, id=0, cache=3)
    t.drain(limit=8000, label="whole")
    # Both targets alternate reads of both sources word by word.
    t.mark("phase", 2)
    for n in TARGETS:
        t.set(n, "r_ilv", 1)
        t.set(n, "r_lat", 16)
    for r in range(3):
        reads(t, (2, 4, 3, 2), r)
    t.drain(limit=8000, label="alternate")
    # The same with pauses at the targets and slow sources.
    t.mark("phase", 3)
    for n in TARGETS:
        t.set(n, "r_rate", 55)
        t.set(n, "r_lat", 2)
    for source in SOURCES:
        t.ready(source, "r", "modulo", 3, 0)
    for r in range(3):
        reads(t, (1, 2, 4, 3), r)
    t.drain(limit=8000, label="paced")
    # Long, unaligned, narrow and WRAP reads overlapping writes.
    t.mark("phase", 4)
    for n in TARGETS:
        t.set(n, "r_lat", 12)
        t.set(n, "r_rate", 70)
    for source in SOURCES:
        t.ready(source, "r", "modulo", 2, 0)
    for si, source in enumerate(SOURCES):
        for b in BASES:
            base = b + 0x1000 * si + 0x2000
            t.read(source, base, beats=12, id=0, cache=3)
            t.write(source, base + 0x800, beats=3, id=1, cache=3)
            t.read(source, base + 0x100 + BEAT_BYTES // 2, beats=3, id=1, cache=3)
            t.read(source, base + 0x200, beats=4, size=2, id=2, cache=3)
            t.read(source, base + 0x300 + BEAT_BYTES, beats=4, burst="wrap", id=3, cache=3)
            t.write(source, base + 0x900, beats=2, id=2, cache=3)
            t.read(source, base + 0x400 + 4, beats=5, size=2, id=0, cache=3)
            t.read(source, base + 0x500, beats=2, id=1, cache=3)
    t.drain(limit=8000, label="mixed")
    t.end()
