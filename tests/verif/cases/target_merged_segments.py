"""AXI4 targets behind per-target merging with one burst in flight take their read data in response
segments, on the public schema 10 path (CLI).

Sources s0 and s1 (AXI4, 32 bits, one burst in flight each) reach t0 (256 bits) and t1 (32 bits),
one burst in flight per target, so every AXI4 target merges (per-target widths). Both targets answer
through one response link ahead of the source switch, where a segment's end frees the grant for
the other target's words. R returns on a random share of cycles, which holds a word until the next
word shows or ends a segment after two cycles without one. Reads cover INCR bursts over several
target words and WRAP bursts of every container size, with any start beat, whose start word
answers after the target's last word; each is overlapped with writes by the other source, so a
target's next burst must wait for the merge's last beat. The monitors judge holds and unexpected
responses, the offline checker the data, RLAST and the codes of every source beat, the drain
completion.

s2 (128 bits) reads t1 clamped to t1's width. While t1 stops inside s2's burst, t0's answer to s0
has to pass on the shared link: one burst in flight, so segments end toward s2. The companion
configuration gives s2 two bursts on a link that widens; it gathers per tag, so segments end toward
it too and t0's answer passes in the same way.

Negative control: t0's RLAST input reads one for every beat (RTL text fault: a word short of its
read's last then answers with a fault); the companion run under a held expectation has to fail,
which is what a filter on every clamped source would give."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

TARGETS = (('t0', 0x0000, 256), ('t1', 0x8000, 32))
READ_ONLY = ('t2', 0x10000, 128)
SOURCES = ('s0', 's1')
WIDE = 's2'  # wider than t1, so it clamps; one burst in flight, so it takes segments too
# s2 and the 32-bit sources read the same words with the same IDs: attribution has to tell them apart.
NODE = dict(transport_word_bytes=4, clock_domain='main')

def config(wide_extra):
    return endpoint_ports({
        'schema_version': 10, 'name': 'merged_segments', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}
                       for n in SOURCES]
        + [{'name': WIDE, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128, **wide_extra}],
        'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                     'attributes': 'amba', 'mappings': [{'base': hex(base), 'size': '0x8000'}],
                     **({'access': 'read_only'} if n == READ_ONLY[0] else {})}
                    for n, base, w in (*TARGETS, READ_ONLY)],
        'connectivity': 'all',
        'topology': {
            'request': {'nodes': [dict(name='req', **NODE)], 'links': [],
                        'entries': [{'endpoint': n, 'node': 'req'} for n in (*SOURCES, WIDE)],
                        'exits': [{'endpoint': n, 'node': 'req'} for n, _, _ in (*TARGETS, READ_ONLY)]},
            'response': {'nodes': [dict(name='rsp', **NODE), dict(name='leaf', **NODE)],
                         'links': [{'from': 'rsp', 'to': 'leaf'}],
                         'entries': [{'endpoint': n, 'node': 'rsp'}
                                     for n, _, _ in (*TARGETS, READ_ONLY)],
                         'exits': [{'endpoint': n, 'node': 'leaf'} for n in (*SOURCES, WIDE)]}},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


CONFIG = config({})
TWO_BURSTS = config({'max_outstanding': 2})


def rlast_one(rtl, _map):
    """Every use of t0's RLAST input reads one."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', r"\g<1>1'd1", len(uses))]


CASE = dict(
    name="target_merged_segments",
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET", "AXI.OUTLET.FRAMING"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in (*SOURCES, WIDE)]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _, _) in enumerate((*TARGETS, READ_ONLY))],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
    faults=[
        dict(id="rlast_one", op="text", subs=rlast_one, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_RESP", "SOURCE_R_DATA", "AXI.R.LAST", "SOURCE_R_LAST",
                     "SOURCE_R_COUNT", "TAPE.DRAIN_TIMEOUT:drain", "SOURCE_UNANSWERED"]),
    ],
    configs=[
        dict(label=""),
        dict(label="two_bursts", negative_run=True, dut=dict(source="cli", config=TWO_BURSTS),
             model=dict(routes=nlconfig.routes(TWO_BURSTS), error_codes=nlconfig.error_codes(TWO_BURSTS),
                        targets=nlconfig.targets(TWO_BURSTS, cache="amba", prot=True)),
             scenario_args=dict(segments="two_bursts"),
             faults=[dict(id="two_bursts_held", op="scenario", params=dict(claim="held"),
                          expect=["TAPE.EXPECT:CLAMPED_SEGMENT_HELD"],
                          contract="AXI.TARGET.R_INTERLEAVE",
                          note="a clamp that gathers per tag takes segments: the link is free")]),
    ],
    negative_faults_all=True,
)

LONG = (1, 2, 4, 8, 3, 6, 5, 7, 12, 16, 24, 32)
WRAPS = (2, 4, 8, 16)


def clamped_hole(t, expected):
    """t1 stops inside s2's clamped burst; s0 reads t0 over the link they share."""
    wide_last = t.snap(WIDE, "rlast")
    last = t.snap("s0", "rlast")
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 0)
    t.set("t1", "r_stop", 8)  # seven beats, inside the second of s2's four 128-bit beats
    t.read(WIDE, TARGETS[1][1] + 0x2000, beats=4, id=0, cache=3)
    t.delay(40)
    t.read("s0", TARGETS[0][1] + 0x2000, beats=1, id=0, cache=3)
    if expected == "apart":
        t.wait("s0", "rlast", "==", 1, limit=200, label="CLAMPED_SEGMENT_PASSES", since=last)
    else:
        t.delay(200)
        t.expect_now("s0", "rlast", "==", 0, "CLAMPED_SEGMENT_HELD", since=last)
    t.expect_now(WIDE, "rlast", "==", 0, "CLAMPED_BURST_OPEN", since=wide_last)
    t.set("t1", "r_stop", 0)
    t.wait(WIDE, "rlast", "==", 1, limit=2000, label="CLAMPED_BURST_DONE", since=wide_last)
    t.wait("s0", "rlast", "==", 1, limit=2000, label="CLAMPED_S0_DONE", since=last)


def scenario(t, seed, segments="apart", claim=None):
    for n, _, _ in (*TARGETS, READ_ONLY):
        t.set(n, "r_lat", 2)
    t.reset(4)
    t.delay(2)
    if segments == "two_bursts":
        clamped_hole(t, claim or "apart")
        t.drain(limit=5000, label="drain")
        t.end()
        return
    t.mark("phase", 1)
    for k, beats in enumerate(LONG):
        for si, s in enumerate(SOURCES):
            for _, base, _ in TARGETS:
                t.write(s, base + 0x1000 * si + 0x100 * k, beats=beats, id=k % 4, cache=3)
    for s in SOURCES:
        t.wait(s, "b", "==", 2 * len(LONG), limit=20000, label="WRITES_DONE")
    t.mark("phase", 2)
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 40)
    for k, beats in enumerate(LONG):
        for si, s in enumerate(SOURCES):
            for _, base, _ in TARGETS:
                t.read(s, base + 0x1000 * si + 0x100 * k, beats=beats, id=(k + si) % 4, cache=3)
    for k in range(8):
        for _, base, _ in TARGETS:
            t.read(WIDE, base + 0x100 * k, beats=1 << (k % 3), id=k % 4, cache=3)
    t.wait(WIDE, "rlast", "==", 16, limit=30000, label="WIDE_READS_DONE")
    for s in SOURCES:
        t.wait(s, "rlast", "==", 2 * len(LONG), limit=30000, label="READS_DONE")
    clamped_hole(t, segments)
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 40)
    t.mark("phase", 3)
    # A write to the read-only target passes its merge and is answered with SLVERR at its outlet.
    t.set(READ_ONLY[0], "r_rate", 40)
    for k, beats in enumerate(LONG[:8]):
        t.read("s1", READ_ONLY[1] + 0x100 * k, beats=beats, id=k % 4, cache=3)
        t.write("s0", READ_ONLY[1] + 0x1000 + 0x100 * k, beats=beats, id=k % 4, cache=3)
    for k, beats in enumerate(WRAPS):
        for start in range(beats):
            for _, base, _ in TARGETS:
                t.read("s0", base + 0x100 * k + 4 * start, beats=beats, burst="wrap", id=start % 4,
                       cache=3)
                t.write("s1", base + 0x1800 + 0x100 * k, beats=beats, id=start % 4, cache=3)
    t.ready("s0", "r", "modulo", 3, 0)
    t.ready("s1", "b", "modulo", 4, 0)
    t.drain(limit=60000, label="drain")
    t.end()
