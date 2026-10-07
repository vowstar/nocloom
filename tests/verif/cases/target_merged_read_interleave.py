"""AXI4 targets with several bursts in flight behind per-target merging alternate read data of
different target IDs word by word, on the public schema 10 path (CLI).

Each target word names its read, and each read walks its own source beats, so a word of another read
may follow any word. Every shape runs whole bursts, alternating reads, paced reads, long, unaligned,
narrow and WRAP reads overlapping writes, and each source reading one target while writing another.
WRAP reads start inside a target word: those of different sources share one store for the start word
and take turns at the request face. The offline checker judges the data, RLAST and codes of every
source beat, and the drain the completion.

Shapes (sources; targets with their bursts in flight):
  narrow:   one 32-bit source; 64-bit K=4 and 32-bit K=4
  single:   two 32-bit sources with one burst each; 64-bit K=4 and 32-bit K=4
  wide:     one 64-bit source, clamped; 64-bit K=4 and 32-bit K=4
  two:      two 64-bit sources, clamped; 64-bit K=4 and 32-bit K=4
  third:    two 64-bit sources, clamped; 32-bit K=1, 64-bit K=4 and 64-bit K=4
  collide:  two 32-bit sources on 2-bit target IDs, so reads of different sources and IDs share a
            target ID, wait for each other and do not interleave; 64-bit K=2 and 32-bit K=2
  read_only: one 32-bit source; a read-only 64-bit K=4, whose outlet answers writes, and 32-bit K=4
  wider:    two 32-bit sources; 128-bit K=4 and 32-bit K=4, for WRAP containers of less than a word

The wide shape also times the link: a read request and a write response hold no payload, so one
transport word carries each although the beat is wider than the transport.

Negative controls (RTL text faults): RID reads zero, so words name the wrong read; RREADY of the
merged target stays high, so a word held for its beats is overwritten."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

SHAPES = {
    "narrow": (((32, 4),), ((64, 4, None), (32, 4, None))),
    "single": (((32, 1), (32, 1)), ((64, 4, None), (32, 4, None))),
    "wide": (((64, 4),), ((64, 4, None), (32, 4, None))),
    "two": (((64, 4), (64, 4)), ((64, 4, None), (32, 4, None))),
    "third": (((64, 4), (64, 4)), ((32, 1, None), (64, 4, None), (64, 4, None))),
    "collide": (((32, 4), (32, 4)), ((64, 2, 2), (32, 2, 2))),
    "read_only": (((32, 4),), ((64, 4, "ro"), (32, 4, None))),
    "wider": (((32, 4), (32, 4)), ((128, 4, None), (32, 4, None))),
}
SHAPE = globals().get("PARAMS", {}).get("shape", "narrow")
SOURCES = tuple((f"s{k}", w, o) for k, (w, o) in enumerate(SHAPES[SHAPE][0]))
TARGETS = tuple((f"t{k}", w, c, i) for k, (w, c, i) in enumerate(SHAPES[SHAPE][1]))
BASES = tuple(0x8000 * k for k in range(len(TARGETS)))

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'merged_interleave', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                    'max_outstanding': o} for n, w, o in SOURCES],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                 'attributes': 'amba', 'max_outstanding': c,
                 **({'id_width': i} if isinstance(i, int) else {}),
                 **({'access': 'read_only'} if i == 'ro' else {}),
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]}
                for (n, w, c, i), base in zip(TARGETS, BASES)],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)

MERGED = next(n for n, w, _, _ in TARGETS if w >= 64)
# One transport word per message meets these; two words per message take longer.
READ_HEADS = 5
WRITE_B = 200
WRITE_RETURN = 2
SEGMENT_CYCLES = 131


def rid_zero(rtl, _map):
    """Every use of the merged target's RID input reads zero."""
    pattern = rf'^(?!\s*input\b)(.*)\bnl_t_{MERGED}_rid\b'
    return [(pattern, r"\g<1>{" + str(_width(rtl, f"nl_t_{MERGED}_rid")) + "{1'b0}}",
             len(re.findall(pattern, rtl, re.M)))]


def _width(rtl, port):
    m = re.search(rf'input wire \[(\d+):0\] {port}\b', rtl)
    return int(m[1]) + 1


def rready_high(rtl, _map):
    """The merged target's RREADY output reads one."""
    pattern = rf'^assign nl_t_{MERGED}_rready = .*;$'
    return [(pattern, f"assign nl_t_{MERGED}_rready = 1'd1;", 1)]


CASE = dict(
    name="target_merged_read_interleave_" + SHAPE,
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n, _, _ in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _, _, _) in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
    seeds=[1, 2, 3],
    faults=[
        dict(id="rid_zero", op="text", subs=rid_zero, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_DATA", "SOURCE_R_RESP", "SOURCE_R_LAST", "SOURCE_R_COUNT",
                     "SOURCE_UNANSWERED"]),
        dict(id="rready_high", op="text", subs=rready_high, contract="AXI.TARGET.R_INTERLEAVE",
             expect=["SOURCE_R_DATA", "SOURCE_R_RESP", "SOURCE_R_LAST", "SOURCE_R_COUNT",
                     "SOURCE_UNANSWERED"]),
    ],
    negative_faults_all=True,
)


def ident(k):
    return k % 4


def whole(t):
    t.mark("phase", 1)
    for si, (source, _, _) in enumerate(SOURCES):
        for base in BASES:
            for k, beats in enumerate((2, 1, 3)):
                t.read(source, base + 0x1000 * si + 0x100 * k, beats=beats, id=0, cache=3)
    t.drain(limit=8000, label="whole")


def alternate(t, label, beats_of, words_within=None):
    for r in range(3):
        for si, (source, _, _) in enumerate(SOURCES):
            for base in BASES:
                for k, beats in enumerate(beats_of):
                    t.read(source, base + 0x1000 * si + 0x400 * r + 0x100 * k, beats=beats,
                           id=ident(k), cache=3)
    if words_within:
        t.wait(MERGED, "r", ">=", 21, limit=words_within, label="SEGMENTS_END_AT_WORDS")
    t.drain(limit=8000, label=label)


def mixed(t):
    t.mark("phase", 4)
    for n, _, _, _ in TARGETS:
        t.set(n, "r_lat", 12)
        t.set(n, "r_rate", 70)
    for source, _, _ in SOURCES:
        t.ready(source, "r", "modulo", 2, 0)
    for si, (source, width, _) in enumerate(SOURCES):
        bytes_ = width // 8
        for b in BASES:
            base = b + 0x1000 * si + 0x2000
            t.read(source, base, beats=12, id=0, cache=3)
            t.write(source, base + 0x800, beats=3, id=1, cache=3)
            t.read(source, base + 0x100 + bytes_ // 2, beats=3, id=1, cache=3)
            t.read(source, base + 0x200, beats=4, size=2, id=2, cache=3)
            # Inside a 64-bit target word for a 32-bit source; on a source word for a 64-bit one.
            t.read(source, base + 0x300 + bytes_, beats=4, burst="wrap", id=3, cache=3)
            t.write(source, base + 0x900, beats=2, id=2, cache=3)
            t.read(source, base + 0x400 + 4, beats=5, size=2, id=0, cache=3)
            t.read(source, base + 0x500, beats=2, id=1, cache=3)
    t.drain(limit=8000, label="mixed")


def wraps_together(t):
    """Unaligned WRAP reads of every source and ID at once: the start word store takes turns."""
    t.mark("phase", 5)
    for n, _, _, _ in TARGETS:
        t.set(n, "r_lat", 4)
        t.set(n, "r_rate", 60)
    for source, _, _ in SOURCES:
        t.ready(source, "r", "always")
    for r in range(2):
        for si, (source, width, _) in enumerate(SOURCES):
            bytes_ = width // 8
            for b in BASES:
                for k in range(4):
                    t.read(source, b + 0x1000 * si + 0x4000 + 0x100 * r + 0x40 * k + bytes_,
                           beats=(4 if k % 2 else 8), burst="wrap", id=ident(k), cache=3)
    t.drain(limit=12000, label="wraps")
    # A WRAP container of one target word turns inside the word. Slow sources keep stored beats
    # leaving while B responses of other writes wait behind them.
    for source, _, _ in SOURCES:
        t.ready(source, "r", "modulo", 4, 0)
    for n, _, _, _ in TARGETS:
        t.set(n, "b_lat", 0)
    for r in range(3):
        for si, (source, width, _) in enumerate(SOURCES):
            bytes_ = width // 8
            for b in BASES:
                base = b + 0x1000 * si + 0x5000 + 0x100 * r
                t.read(source, base + 4, beats=2, size=2, burst="wrap", id=0, cache=3)
                t.read(source, base + 0x22, beats=2, size=1, burst="wrap", id=0, cache=3)
                t.read(source, base + 0x26, beats=2, size=1, burst="wrap", id=0, cache=3)
                t.read(source, base + 0x44, beats=2, size=2, burst="wrap", id=0, cache=3)
                t.read(source, base + 0x6c, beats=2, size=2, burst="wrap", id=0, cache=3)
                t.write(source, base + 0x40, beats=1, id=1, cache=3)
                t.read(source, base + 0x80 + bytes_, beats=8, burst="wrap", id=2, cache=3)
                t.write(source, base + 0xc0, beats=1, id=3, cache=3)
    t.drain(limit=12000, label="turns")


def cross(t, label, shift):
    """Each source reads one target and writes another at the same time."""
    t.mark("phase", 6 + shift)
    n = len(TARGETS)
    for name, _, _, _ in TARGETS:
        t.set(name, "r_rate", 40 if shift == 0 else 25)
        t.set(name, "r_lat", 2 if shift == 0 else 8)
        t.set(name, "b_lat", 1)
    for r in range(3):
        for si, (source, width, _) in enumerate(SOURCES):
            bytes_ = width // 8
            rd, wr = (si + shift) % n, (si + shift + 1) % n
            for k in range(3):
                t.read(source, BASES[rd] + 0x1000 * si + 0x100 * r + 0x20 * bytes_ * k, beats=8,
                       id=ident(k), cache=3)
                t.write(source, BASES[wr] + 0x1000 * si + 0x4000 + 0x80 * r + 0x10 * bytes_ * k,
                        beats=1 + k % 2, id=ident(k), cache=3)
    t.drain(limit=12000, label=label)


def one_word_messages(t):
    """Read requests leave one per cycle, and a write response takes no extra cycle on the link."""
    source = SOURCES[0][0]
    t.set(MERGED, "r_lat", 40)
    for k in range(3):
        t.read(source, BASES[0] + 0x40 * k, beats=2, id=k + 1, cache=3)
    t.wait(MERGED, "ar", ">=", 3, limit=READ_HEADS, label="READ_HEADS_ONE_WORD")
    t.drain(limit=8000, label="heads")
    t.set(MERGED, "r_lat", 6)
    t.write(source, BASES[0] + 0x400, beats=1, id=0, cache=3)
    t.wait(MERGED, "b", ">=", 1, limit=WRITE_B, label="WRITE_B")
    t.delay(WRITE_RETURN)
    t.expect_now(source, "b", ">=", 1, "WRITE_B_ONE_WORD")
    t.drain(limit=8000, label="heads_b")


def scenario(t, seed):
    for n, _, _, _ in TARGETS:
        t.set(n, "r_lat", 6)
        t.set(n, "seed", 0x1357 * seed + ord(n[1]))
    t.reset(4)
    t.delay(2)
    if SHAPE == "wide":
        one_word_messages(t)
    whole(t)
    t.mark("phase", 2)
    for n, _, _, _ in TARGETS:
        t.set(n, "r_ilv", 1)
        t.set(n, "r_lat", 16)
    # A segment ends only as a target word's last beat leaves: ending at every beat takes longer.
    alternate(t, "alternate", (2, 4, 3, 2), SEGMENT_CYCLES if SHAPE == "narrow" and seed == 1 else None)
    t.mark("phase", 3)
    for n, _, _, _ in TARGETS:
        t.set(n, "r_rate", 55)
        t.set(n, "r_lat", 2)
    for source, _, _ in SOURCES:
        t.ready(source, "r", "modulo", 3, 0)
    alternate(t, "paced", (1, 2, 4, 3))
    mixed(t)
    wraps_together(t)
    cross(t, "cross", 0)
    cross(t, "cross2", 1)
    t.end()
