"""64-bit AXI4 sources with one read ID and four bursts in flight read and write 32-bit AXI4 targets
at once, on the public schema 10 path (CLI). Shapes:

  k4, k2      two sources and two targets crossed: s0 reads t0 and writes t1, s1 reads t1 and writes
              t0, with targets of four or two bursts in flight. A write response for one source then
              sits between read words for the other: a response output kept across a read would wait
              for a word queued behind that response, and two such outputs wait on each other.
  one_target  one such source and one 32-bit source on one target, which alternates their reads and
              answers writes between read words: a response word of a run must not join a stored
              word of the source's read.

The source gathers one read at a time, and only that read's words and its write responses reach it,
so targets end response segments toward it. Every read must complete with the exact data.

Negative control (RTL text fault, shape k4): the response switch outputs release only at a packet's
last fragment. Two outputs then wait for each other and the drain times out."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

PARAMS = globals().get("PARAMS", {})
SHAPE = PARAMS.get("shape", "k4")
ONE_TARGET = SHAPE == "one_target"
BURSTS = 2 if SHAPE == "k2" else 4
SOURCES = {'s0': (64, 0), 's1': (32 if ONE_TARGET else 64, 2 if ONE_TARGET else 0)}
TARGETS = ('t0',) if ONE_TARGET else ('t0', 't1')
BASES = (0x0, 0x8000)

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'single_id', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                    'max_outstanding': 4, 'id_width': i} for n, (w, i) in SOURCES.items()],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': BURSTS,
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]}
                for n, base in zip(TARGETS, BASES)],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def release_at_last_only(rtl, map_):
    """Each response output's release reads its candidates' last fragment and not their segment end:
    release = transfer & tail, tail = OR of (selected & candidate end), end = last | cut."""
    assign = re.compile(
        r'^assign (nl_internal_v\d+) = \((nl_internal_v\d+) ([&|]) (nl_internal_v\d+)\);$', re.M)
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
    name="clamp_single_id_crossing_" + SHAPE,
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, n in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
    seeds=[1, 5, 9],
    faults=[] if ONE_TARGET or SHAPE == "k2" else [
        dict(id="release_at_last_only", op="text", subs=release_at_last_only,
             contract="AXI.TARGET.R_INTERLEAVE",
             expect=["TAPE.DRAIN_TIMEOUT:cross", "SOURCE_UNANSWERED"]),
    ],
)


def cross(t, shift, beats, label):
    """Each source reads one target and writes another at the same time."""
    count = len(TARGETS)
    for r in range(4):
        for si, (source, (width, ids)) in enumerate(SOURCES.items()):
            read_base = BASES[(si + shift) % count]
            write_base = BASES[(si + shift + 1) % count]
            step = width // 8
            for k in range(3):
                ident = k % 4 if ids else 0
                t.read(source, read_base + 0x1000 * si + 0x100 * r + 0x20 * step * k, beats=beats,
                       id=ident, cache=3)
                t.write(source, write_base + 0x1000 * si + 0x4000 + 0x80 * r + 0x10 * step * k,
                        beats=1 + k % 2, id=ident, cache=3)
    t.drain(limit=12000, label=label)


def scenario(t, seed):
    for n in TARGETS:
        t.set(n, "seed", 0x1357 * seed + ord(n[1]))
    t.reset(4)
    t.delay(2)
    # The target's R paced so that a B slips between words of a read.
    t.mark("phase", 1)
    for n in TARGETS:
        t.set(n, "r_ilv", 1)
        t.set(n, "r_rate", 40)
        t.set(n, "r_lat", 2)
        t.set(n, "b_lat", 1)
    cross(t, 0, 8, "cross")
    # The other crossing, slower reads.
    t.mark("phase", 2)
    for n in TARGETS:
        t.set(n, "r_rate", 25)
        t.set(n, "r_lat", 8)
    cross(t, 1, 8, "other")
    # Slow sources too.
    t.mark("phase", 3)
    for n in TARGETS:
        t.set(n, "r_rate", 50)
        t.set(n, "r_lat", 1)
    for source in SOURCES:
        t.ready(source, "r", "modulo", 3, 0)
    cross(t, 0, 16, "paced")
    t.end()
