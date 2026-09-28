"""Wide AHB targets in an explicit graph through the public CLI (64 and 256
bits).

Judged by the offline checker and the shape model and bus rules of
ahb_shape.py (ahb_plan.judge). Faults: HSIZE held at a word on t0 (a text
fault on the netlist), and the cut network's expectation run on the
network that does not cut (its WRAP reaches t0 as WRAPn)."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ahb_plan  # noqa: E402
import ahb_shape as shape  # noqa: E402
from nlverif import config as nlconfig  # noqa: E402

WIDTHS = (64, 256)
UNMAPPED = (0x9120, 0x9800)


def configuration(width, cut=False):
    targets = [
        dict(name="t0", clock_domain="main", protocol="ahb_v3", attributes="amba",
             data_width=width, mappings=[dict(base="0x1000", size="0x1000")]),
        dict(name="t1", clock_domain="main", protocol="ahb_v3", attributes="amba",
             data_width=width, write_bursts="complete", undefined_length_bursts=False,
             hresp_width=2, mappings=[dict(base="0x5000", size="0x200")]),
        dict(name="t2", clock_domain="main", protocol="axi4", attributes="amba", len_width=8,
             data_width=32, mappings=[dict(base="0x20000", size="0x1000")])]
    if cut:
        targets.append(dict(name="t3", clock_domain="main", protocol="axi4", attributes="amba",
                            data_width=1024, mappings=[dict(base="0x40000", size="0x1000")]))
    names = [t["name"] for t in targets]
    sources = ("s0", "s1", "s2")
    return dict(
        schema_version=10, name="ahb_wide", address_width=32,
        arbitration=dict(policy="round_robin"),
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")], clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", len_width=8,
                         data_width=width),
                    dict(name="s1", clock_domain="main", protocol="axi4", len_width=8,
                         data_width=32),
                    dict(name="s2", clock_domain="main", protocol="axi4", len_width=8,
                         data_width=2 * width)],
        targets=targets, transport=dict(max_outstanding=1), connectivity="all",
        topology=dict(
            request=dict(nodes=[dict(name="q0", transport_word_bytes=8), dict(name="q1")],
                         links=[{"from": "q0", "to": "q1"}],
                         entries=[dict(endpoint=s, node="q0") for s in sources],
                         exits=[dict(endpoint=n, node="q1") for n in names]),
            response=dict(nodes=[dict(name="p0"), dict(name="p1", transport_word_bytes=8)],
                          links=[{"from": "p0", "to": "p1"}],
                          entries=[dict(endpoint=n, node="p0") for n in names],
                          exits=[dict(endpoint=s, node="p1") for s in sources])),
        output=dict(backend="verilog", language="verilog_2001"))


def wrap_cut(width, source_bits, cut=False):
    """The exchange composition's packet table for a source: its WRAP limit
    at the AHB targets when it cuts WRAP bursts, else None (the scenario's
    rule: a target's limit is the largest power of two up to its packet
    limit, 16 words at an AXI4 target and the widest source limit, then at
    most the pair's packet limit; one word or less is none)."""
    lanes, own_bytes = width // 8, source_bits // 8

    def own(word):
        return max(min(64, 16 * word), 2 * word)
    widest = max(own(b) for b in (lanes, 4, 2 * lanes))
    paths = []
    for word, limit, cap in [(lanes, min(16 * lanes, 1024), 1024), (4, 1024, 64)] + (
            [(128, 2048, 2048)] if cut else []):
        upper = min(limit, cap, widest)
        path = min(1 << (upper.bit_length() - 1), max(limit, own_bytes, word))
        paths.append(path if path > word else 0)
    if all(path and own(own_bytes) <= path for path in paths):
        return None
    return min(paths[0], own(own_bytes)) if paths[0] else 1


def one_packet(selected, lanes, slanes, window, wrap_container_limit):
    """The cases that reach the target as one AHB packet:
    an INCR keeps the source beats of its first packet at both its write
    and its read start; a WRAP that the source cuts from its container base
    at both starts reaches the target as one INCR, one past wrap_container_limit becomes
    two packets and is left out; narrow WRAPs folded into one source beat,
    narrow INCRs packed into words and narrow WRAPs merged in front of a
    wide target are left out."""
    limit, align = min(16 * lanes, 1024), max(lanes, slanes)
    cross = min(1024, window or 1024)
    write_base = 0x100 if window is not None or lanes > 4 else 0x120
    read_base = 0x000 if window is not None else 0x800

    def fit(case):
        beats, size, burst, mode, offset = case
        if burst == 2 and 1 << size < slanes and beats << size <= slanes:
            return None
        if burst == 1 and beats > 1 and 1 << size < min(slanes, lanes):
            return None
        if burst == 2 and lanes > 4 and 1 << size < lanes:
            return None
        if burst == 2 and wrap_container_limit is not None:
            span = beats << size
            if all((x + offset) % span == 0 for x in (write_base, read_base)):
                return case if span <= min(limit, 1024) else None
            if span > wrap_container_limit:
                return None
        if burst != 1:
            return case
        for start in (write_base + offset, read_base + offset):
            first = start >> size << size
            end = min(start // align * align + limit, start // cross * cross + cross)
            beats = max(1, min(beats, (end - first) >> size))
        return (beats, size, burst, mode, offset)
    return [c for c in map(fit, selected) if c]


def segments(width, cut=False, uncut_control=False):
    """The (source, target) runs of one network."""
    lanes = width // 8
    out = []
    targets = [("t0", 0x1000, 0x1000, False)] if cut else [
        ("t0", 0x1000, 0x1000, False), ("t1", 0x5000, 0x200, True)]
    for tgt, win, limit, shaped in targets:
        window = limit if limit < 0x1000 else None
        for src, bits in (("s0", width), ("s2", 2 * width)):
            wrap_container_limit = wrap_cut(width, bits, cut or uncut_control)
            selected = one_packet(shape.cases(lanes, bits // 8, window, False), lanes, bits // 8,
                                  window, wrap_container_limit)
            # The 1-bit HRESP of t0 has no RETRY or SPLIT.
            selected = [c for c in selected if shaped or c[3] not in (5, 6)]
            out.append(ahb_plan.Segment(src, tgt, win, limit, bits // 8, lanes, selected,
                                        complete=shaped, no_incr=shaped,
                                        expect_cut=wrap_container_limit is not None, unmapped=UNMAPPED))
        if not cut:
            # The 32-bit source, merged into target words: no error modes
            # (an error at a target word reaches every beat merged into it).
            selected = [c for c in shape.cases(lanes, 4, window, False)
                        if c[3] not in (2, 5, 6, 13)]
            out.append(ahb_plan.Segment("s1", tgt, win, limit, 4, lanes, selected,
                                        complete=shaped, no_incr=shaped, merged=True,
                                        unmapped=UNMAPPED))
    if uncut_control:
        out = [s for s in out if s.tgt == "t0" and s.src == "s0"]
        for s in out:
            s.mark = dict(cut_control=True)
    return out


def bfms(cut):
    out = [dict(name=s, kind="axi_master", endpoint=f"initiator:{s}") for s in ("s0", "s1", "s2")]
    out += [dict(name="t0", kind="ahb_slave", endpoint="target:t0", salt=5),
            dict(name="t1", kind="ahb_slave", endpoint="target:t1", salt=6),
            dict(name="t2", kind="axi_slave", endpoint="target:t2", salt=7)]
    if cut:
        out.append(dict(name="t3", kind="axi_slave", endpoint="target:t3", salt=8))
    return out


def network(width, cut):
    cfg = configuration(width, cut)
    return dict(label=f"{'cut' if cut else 'graph'}{width}", dut=dict(source="cli", config=cfg),
                bfms=bfms(cut),
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                           targets=nlconfig.targets(cfg, error_data_zero=True)),
                scenario_args=dict(width=width, cut=cut))


MAIN = configuration(WIDTHS[0])
CASE = dict(
    name="ahb_wide_graph",
    contracts=["APB.TARGET.ACCESS", "AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO",
               "AXI.WIDTH.ENDPOINT", "APB.SUBPAGE.DECODE"],
    dut=dict(source="cli", config=MAIN),
    bfms=bfms(False),
    model=network(WIDTHS[0], False)["model"],
    configs=[network(w, cut) for w in WIDTHS for cut in (False, True)],
    scenario_args=dict(width=WIDTHS[0], cut=False),
    negative_runs_all=True,
    max_cycles=3000000,
    run_timeout=900,
    faults=[
        dict(id="hsize_word", op="text", contract="APB.TARGET.ACCESS",
             subs=[(r"(assign nl_t_t0_hsize = )[^;]+;", r"\g<1>3'd2;", 1)],
             expect=["ORACLE", "TARGET_WRITE_BYTES", "TARGET_FOOTPRINT", "SOURCE_R_DATA"],
             note="HSIZE held at a word: a wide beat fails"),
        dict(id="uncut", op="scenario", params=dict(uncut_control=True), expect=["ORACLE"],
             contract="AXI.WIDTH.ENDPOINT",
             note="the cut network's expectation (a WRAP from its container base reaches t0 "
                  "as INCR) on the network that does not cut: t0 sees WRAPn"),
    ],
)


def scenario(t, seed, width=WIDTHS[0], cut=False, uncut_control=False):
    for k, seg, case in ahb_plan.plan(segments(width, cut, uncut_control)):
        ahb_plan.run_case(t, k, seg, case)
    t.end()


def oracle(checker, events, sources, targets):
    args = checker.case.get("scenario_args", {})
    # The scenario fault marks its run: every source WRAP it issued carries
    # the cut expectation of the cut network.
    uncut = any(s.expect.get("cut_control") for s in sources)
    segs = segments(args.get("width", WIDTHS[0]), args.get("cut", False), uncut)
    return ahb_plan.judge(checker, events, sources, targets, segs, Counter())[:8]


CASE["oracle"] = oracle
