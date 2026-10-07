"""One domain reset alone while a packet is in flight across its boundary (part of the clocked
graphs' ratios registration, graph_clocked.py).

The networks (public CLI) have three domains with their own reset ports: src holds the sources
s0 (128-bit AXI4), s2 (APB4) and s3 (64-bit AXI4, two bursts in flight, 64-byte packets to t4),
net the switch nodes, the source s1 (AXI4-Lite) and the APB4 target t3, tgt the targets t0 and
t4 (64-bit AXI4, t4 with four bursts in flight), t1 (APB4) and t2 (AHB). The star networks put
one request and one response node in net, with every register reset (all) or only the control
ones (minimal); the backbone network puts the request node of s0, s2 and s3 in src and the
response node of t0, t1, t2 and t4 in tgt, each joined to a node in net by a link of another
word width. Every BFM resets with its domain.

Each generation cuts one transaction of s0 with t0 (a 16-beat burst, 32 beats at t0) by one
reset alone and ends with a common reset: (1) src while the write is half way into net, (2) src
while the read response is half way into src, (3) net while the write spans both crossings,
(4) tgt while t0 holds the B of a whole write, (5) tgt while the write is half way into tgt,
(6) tgt while t0 is half way through the read response, (7) net while the read response is
half way into net; then (8) src while a read of s2 waits whole at t1 (a slow APB access), and
(9) to (16) src while a read or a write of s0 or s3 waits whole at t4 (its R or B withheld) as
one packet or two (WIDE). The all-register star repeats (1) and (6) with the reset 0, 1, 2, 3,
5 and 8 cycles after the first beat. A reset of net or tgt lasts past the quiet window that
follows it.

Judged is only what such a reset leaves defined: the docs/clocks-and-resets.md promises nothing for the words of a
cut packet that reach a side reset meanwhile, nor for the paths the cut packet holds.
  O1 the reset falls inside the transaction (TAPE.EXPECT SCENARIO_UNREACHED_<g>);
  O2 transactions on paths that share no hop with the cut packet (s1 with t3, t1, and s2 with
     t2; in the backbone s1 with t3), issued after the quiet window (toward net or tgt while
     that domain is still in reset), complete correctly before any common reset
     (TAPE.WAIT_TIMEOUT ISOLATION_<g>; the offline checks attribute every access and response
     of s1, s2, t1, t2 and t3, so no word of the cut packet reaches them);
  O3 after the common reset every source completes with every target (RECOVER_<g>);
  O4 after (4), and in the star after (6), s1 and s2 complete with t0 (FREED_<g>);
  O5 in (6) every 32-bit lane of an R beat s0 takes is t0's data of that beat or a word
     cleared by the reset, and no beat is the last (ORACLE CUT_SOURCE_DATA);
  O6 in (1) the W beats t0 takes before the reset are s0's beats in order (CUT_TARGET_BYTES);
  O7 in (8) to (16) the source's endpoint drops every beat of the answer of the abandoned read
     or write (the write's answers SLVERR) and its next transactions get their own responses
     (DROPPED_<g> and the offline checks);
  O8 in (1) and (6) no target starts an access between the reset and the end of the quiet
     window (ORACLE CLEARED_WORD_ACCESS);
  O9 in (4) to (7) s0 never completes the cut transaction (TAPE.EXPECT CUT_COMPLETED_<g>);
  O10 in (2) the read reached t0 whole, so t0 sends all its beats: s0's endpoint drops the rest
     of the answer instead of holding t0 (TAPE.WAIT_TIMEOUT DRAINED_2).
The negative controls (on the all-register star) and the oracle each catches: N1 crossing
enables ignore the other side (O2), N2 a source takes an unwaited response (O7), N3 a read index
survives the reset (O2), N4 a sender takes words while the other side is reset (the requests
toward a domain in reset are lost), N5 the reset waits for the last beat (O1), N6 the tgt state
resets from rst_net_n (O4), N7 t0's W data bit 0 flipped (O6), N8 (4) without its reset (O9),
N9 an access in the quiet window (O8), N10 (1) without its common reset (O3), N11 s0's R data
bit 0 flipped (O5)."""

import json
import re

from nlverif import config as nlconfig

STAR_SOURCES = ("s0", "s1", "s2", "s3")
TARGETS = ("t0", "t1", "t2", "t3", "t4")
PORTS = ("rst_net_n", "rst_src_n", "rst_tgt_n")
PORT = {d: PORTS.index(f"rst_{d}_n") for d in ("net", "src", "tgt")}
DOMAIN = dict(s0="src", s1="net", s2="src", s3="src", t0="tgt", t1="tgt", t2="tgt", t3="net",
              t4="tgt")
BASE = dict(t0=0x0, t1=0x10000, t2=0x20000, t3=0x30000, t4=0x40000)
SWEEP = (0, 1, 2, 3, 5, 8)


def network(name, backbone=False, minimal=False):
    sources = [dict(name="s0", protocol="axi4", data_width=128, id_width=2, len_width=8),
               dict(name="s1", protocol="axi4_lite", data_width=32),
               dict(name="s2", protocol="apb4", data_width=32),
               dict(name="s3", protocol="axi4", data_width=64, id_width=2, len_width=8,
                    max_outstanding=2)]
    targets = [dict(name="t0", protocol="axi4", data_width=64, len_width=8),
               dict(name="t1", protocol="apb4", data_width=32),
               dict(name="t2", protocol="ahb_v3", data_width=32),
               dict(name="t3", protocol="apb4", data_width=32),
               dict(name="t4", protocol="axi4", data_width=64, len_width=8, max_outstanding=4)]
    for e in sources + targets:
        e["clock_domain"] = DOMAIN[e["name"]]
    for t in targets:
        t.update(attributes="amba", mappings=[dict(base=hex(BASE[t["name"]]), size="0x10000")])
    names = [t["name"] for t in targets]
    if backbone:
        request = dict(nodes=[dict(name="qa", clock_domain="src", transport_word_bytes=16),
                              dict(name="qb", clock_domain="net", transport_word_bytes=4)],
                       links=[{"from": "qa", "to": "qb", "transport_word_bytes": 8}],
                       entries=[dict(endpoint=s, node="qb" if s == "s1" else "qa")
                                for s in STAR_SOURCES],
                       exits=[dict(endpoint=n, node="qb") for n in names])
        response = dict(nodes=[dict(name="pa", clock_domain="tgt", transport_word_bytes=8),
                               dict(name="pb", clock_domain="net", transport_word_bytes=4)],
                        links=[{"from": "pa", "to": "pb"}],
                        entries=[dict(endpoint=n, node="pa" if n != "t3" else "pb")
                                 for n in names],
                        exits=[dict(endpoint=s, node="pb") for s in STAR_SOURCES])
    else:
        request = dict(nodes=[dict(name="q", clock_domain="net")], links=[],
                       entries=[dict(endpoint=s, node="q") for s in STAR_SOURCES],
                       exits=[dict(endpoint=n, node="q") for n in names])
        response = dict(nodes=[dict(name="p", clock_domain="net")], links=[],
                        entries=[dict(endpoint=n, node="p") for n in names],
                        exits=[dict(endpoint=s, node="p") for s in STAR_SOURCES])
    cfg = {"schema_version": 10, "name": name, "address_width": 32,
           "reset_ports": [{"name": p} for p in PORTS],
           "clock_domains": [{"name": d, "clock_port": f"clk_{d}", "reset_port": f"rst_{d}_n"}
                             for d in ("src", "net", "tgt")],
           "clock_gating": "none", "initiators": sources, "targets": targets,
           "transport": {"transport_word_bytes": 4,
                         "packet_limits": [dict(source="s3", target="t4", max_bytes=64)]},
           "topology": {"request": request, "response": response},
           "connectivity": "all",
           "output": {"backend": "verilog", "language": "verilog_2001"}}
    if minimal:
        cfg["reset_coverage"] = "minimal"
    return json.loads(json.dumps(cfg))


def bfms():
    out = []
    for s in STAR_SOURCES:
        spec = dict(name=s, endpoint=f"initiator:{s}", clock=DOMAIN[s], reset=f"rst_{DOMAIN[s]}_n")
        spec.update(dict(kind="apb_master", dialect=4) if s == "s2"
                    else dict(kind="axi_master", lite=s == "s1"))
        out.append(spec)
    kinds = dict(t0="axi_slave", t1="apb_slave", t2="ahb_slave", t3="apb_slave", t4="axi_slave")
    for k, t in enumerate(TARGETS):
        spec = dict(name=t, endpoint=f"target:{t}", clock=DOMAIN[t], reset=f"rst_{DOMAIN[t]}_n",
                    kind=kinds[t], salt=k + 1)
        if kinds[t] == "apb_slave":
            spec["dialect"] = 4
        out.append(spec)
    return out


# ---------------------------------------------------------------------------------------------
# The offline oracle (O5, O6, O8) over the event log; marks: "gen" at the start of a generation,
# "cut" just before its reset, "quiet" at the end of the quiet window, "end" before its
# common reset.
def oracle(checker, events, sources, targets):
    marks = checker.tape["marks"]
    bfm = checker.bench["bfms"]
    windows, gen, edges = {}, None, {}
    for e in events:
        if e.get("k") == "mk":
            name = marks[e["m"]] if e["m"] < len(marks) else "?"
            if name == "gen":
                gen = e["v"]
                edges = windows.setdefault(gen, dict(kind=None))
                edges["start"] = e["t"]
            elif gen is not None and name in ("cut", "quiet", "end"):
                edges[name] = e["t"]
                if name == "cut":
                    edges["kind"] = e["v"]
    out = []
    by_port = {}
    for e in events:
        if e.get("k") in ("hs", "ahb_addr") and e.get("p") in bfm:
            by_port.setdefault(e["p"], []).append(e)
    sbytes, tbytes = bfm["s0"]["data_bits"] // 8, bfm["t0"]["data_bits"] // 8
    ratio = sbytes // tbytes
    for g, w in sorted(windows.items()):
        if "cut" not in w:
            continue
        inside = lambda e, lo, hi: lo <= e["t"] < hi  # noqa: E731
        kind = w["kind"]
        if kind == 1:
            # O6: t0's W beats before the reset are s0's beats in order, lane for lane.
            src = [e for e in by_port.get("s0", []) if e.get("ch") == "w"
                   and inside(e, w["start"], w["cut"])]
            got = [e for e in by_port.get("t0", []) if e.get("ch") == "w"
                   and inside(e, w["start"], w["cut"])]
            for j, e in enumerate(got):
                if j // ratio >= len(src):
                    out.append(dict(cls="CUT_TARGET_BYTES", gen=g, beat=j, detail="beat not sent"))
                    break
                shift = 8 * tbytes * (j % ratio)
                want = int(src[j // ratio]["d"], 16) >> shift & ((1 << 8 * tbytes) - 1)
                if int(e["d"], 16) != want or e.get("l"):
                    out.append(dict(cls="CUT_TARGET_BYTES", gen=g, beat=j, got=e["d"],
                                    want=hex(want)))
                    break
        if kind == 6:
            # O5: every R beat s0 takes is t0's data of that beat or a cleared word, not last.
            supply = [e for e in by_port.get("t0", []) if e.get("ch") == "r"
                      and inside(e, w["start"], w["cut"])]
            got = [e for e in by_port.get("s0", []) if e.get("ch") == "r"
                   and inside(e, w["start"], w["end"])]
            for k, e in enumerate(got):
                parts = supply[k * ratio:(k + 1) * ratio]
                want = sum(int(p["d"], 16) << (8 * tbytes * i) for i, p in enumerate(parts))
                value = int(e["d"], 16)
                # A word cleared by the sender's reset leaves as zero: every 32-bit lane is the
                # target's or zero.
                lanes = [(value >> (32 * i) & 0xFFFFFFFF, want >> (32 * i) & 0xFFFFFFFF)
                         for i in range(sbytes // 4)]
                if e.get("l") or any(v and (v != x or len(parts) < ratio) for v, x in lanes):
                    out.append(dict(cls="CUT_SOURCE_DATA", gen=g, beat=k, got=e["d"],
                                    want=hex(want), last=e.get("l")))
                    break
        if kind in (1, 6):
            # O8: no target starts an access between the reset and the end of the quiet window.
            for t in TARGETS:
                for e in by_port.get(t, []):
                    starts = (e.get("ch") in ("aw", "ar", "apb") or e.get("k") == "ahb_addr")
                    if starts and inside(e, w["cut"], w["quiet"]):
                        out.append(dict(cls="CLEARED_WORD_ACCESS", gen=g, port=t,
                                        ch=e.get("ch", "ahb"), a=e.get("a")))
    return out


def configuration(label, backbone=False, minimal=False, sweep=False, faults=()):
    cfg = network("oneside_" + label.replace("-", "_"), backbone, minimal)
    targets = nlconfig.targets(cfg, prot=True)
    for t in targets.values():
        if t["kind"] == "axi":
            t["cache"] = "amba"
    return dict(label=label, dut=dict(source="cli", config=cfg), main_clock="src",
                clocks=dict(src=10.0, net=dict(period=13.0, phase=1.3),
                            tgt=dict(period=17.0, phase=2.9)),
                # net and tgt stay in reset well past the quiet window: the transactions issued
                # after it toward them wait for their release.
                resets={p: dict(clock=p.split("_")[1], release=[4 if p == "rst_src_n" else 400])
                        for p in PORTS},
                scenario_args=dict(mode="inflight", sweep=sweep, backbone=backbone),
                max_cycles=400000, bfms=bfms(), oracle=oracle,
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg), targets=targets),
                faults=list(faults))


# ---------------------------------------------------------------------------------------------
def gen_base(g):
    return 0x400 * g


def isolation(t, g, backbone):
    """O2: the paths that share no hop with the cut packet, issued after the quiet window; they
    complete before any common reset."""
    b = gen_base(g)
    pairs = [("s1", "t3")] + ([] if backbone else [("s1", "t1"), ("s2", "t2")])
    snaps = {(s, c): t.snap(s, c) for s in {p[0] for p in pairs} for c in ("b", "r")}
    for k, (s, tg) in enumerate(pairs):
        t.write(s, BASE[tg] + b + 0x40 * (k + 1))
        t.read(s, BASE[tg] + b + 0x40 * (k + 1))
    for (s, c), mark in sorted(snaps.items()):
        t.wait(s, c, ">=", sum(p[0] == s for p in pairs), since=mark, limit=20000,
               label=f"ISOLATION_{g}")


def freed(t, g):
    """O4: s1 and s2 complete with t0 after the reset alone of tgt."""
    b = gen_base(g)
    snaps = {(s, c): t.snap(s, c) for s in ("s1", "s2") for c in ("b", "r")}
    t.write("s1", BASE["t0"] + b + 0x100)
    t.read("s1", BASE["t0"] + b + 0x100)
    t.read("s2", BASE["t0"] + b + 0x140)
    for (s, c), mark in snaps.items():
        if (s, c) != ("s2", "b"):
            t.wait(s, c, ">=", 1, since=mark, limit=20000, label=f"FREED_{g}")


def recover(t, g, common=True):
    """O3: the common reset, then every source with every target."""
    t.mark("end", g)
    if common:
        t.reset(40)
    t.delay(100)
    b = gen_base(g)
    for k, tg in enumerate(TARGETS):
        a = BASE[tg] + b + 0x200
        t.write("s0", a, beats=2, id=k % 4, cache=2)
        t.read("s0", a, beats=2, id=k % 4, cache=2)
        t.write("s1", a + 0x40)
        t.read("s1", a + 0x40)
        t.write("s2", a + 0x80)
        t.read("s2", a + 0x80)
        t.write("s3", a + 0xc0, beats=2, id=k % 4, cache=2)
        t.read("s3", a + 0xc0, beats=2, id=k % 4, cache=2)
    t.drain(limit=40000, label=f"RECOVER_{g}")


def abandoned(t, g):
    """Generation 8: src resets alone while a read of s2 waits whole at t1 (a slow APB access);
    t1 answers after the release, s2's endpoint drops the answer and s2's next transactions get
    their own responses (O7)."""
    t.mark("gen", g)
    a = BASE["t1"] + gen_base(g) + 0x100
    t.set("t1", "slow_mask", 0xFFFC)
    t.set("t1", "slow_match", a & 0xFFFC)
    t.set("t1", "slow_wait", 1500)
    r = t.snap("s2", "r")
    t.read("s2", a)
    t.wait("t1", "psel", "==", 1, limit=20000, label=f"TRIGGER_{g}")
    t.delay(100)
    t.expect_now("s2", "r", "==", 0, f"SCENARIO_UNREACHED_{g}", since=r)
    t.mark("cut", 0)
    t.port_reset(PORT["src"], 40)
    t.delay(3500)
    t.mark("quiet", g)
    t.set("t1", "slow_mask", 0)
    t.read("s2", a + 0x40)
    t.write("s2", a + 0x40)
    t.read("s2", a + 0x40)
    t.drain(limit=20000, label=f"DROPPED_{g}")
    recover(t, g)


# Generations 9 to 16: (source, write, packets). s0 sends 16 beats as one packet and 160 as
# two (t4's 256-word packet limit), s3 8 beats as one and 16 as two (its 64-byte limit).
WIDE = [(s, w, n) for s in ("s0", "s3") for w in (False, True) for n in (1, 2)]
WIDE_BEATS = {("s0", 1): 16, ("s0", 2): 160, ("s3", 1): 8, ("s3", 2): 16}


def wide_abandoned(t, g, source, write, packets):
    """Generations 9 to 16: src resets alone while a read or write of s0 or s3 waits whole at
    t4, its R or B withheld; t4 answers every packet after the release (a write with SLVERR),
    the source's endpoint drops the whole answer and its next transactions get their own
    responses (O7)."""
    t.mark("gen", g)
    a = BASE["t4"] + 0x1000 * (g - 9)
    beats = WIDE_BEATS[source, packets]
    channel, sent = ("b", "wlast") if write else ("r", "ar")
    t.gate("t4", channel, False)
    if write:
        t.set("t4", "errm0_mask", 0xF000)
        t.set("t4", "errm0_match", a & 0xF000)
        t.set("t4", "errm0_code", 2)
    done, issued = t.snap(source, channel), t.snap("t4", sent)
    (t.write if write else t.read)(source, a, beats=beats, id=1, cache=2)
    t.wait("t4", sent, ">=", packets, since=issued, limit=20000, label=f"TRIGGER_{g}")
    t.delay(100)
    t.expect_now(source, channel, "==", 0, f"SCENARIO_UNREACHED_{g}", since=done)
    t.mark("cut", 0)
    t.port_reset(PORT["src"], 40)
    t.delay(300)
    t.gate("t4", channel, True)
    # The whole answer reaches the source and is dropped before its next transactions.
    t.delay(3000 if beats > 16 else 800)
    t.mark("quiet", g)
    t.set("t4", "errm0_mask", 0)
    b = a + 0xc00
    t.read(source, b, beats=4, id=1, cache=2)
    t.write(source, b, beats=4, id=1, cache=2)
    t.read(source, b, beats=4, id=2, cache=2)
    t.drain(limit=20000, label=f"DROPPED_{g}")
    recover(t, g)


def cut(t, g, kind, offset=None, late=False, backbone=False, control=None):
    """One generation: s0's transaction with t0, cut by the reset alone of one domain. control
    names a negative control's change (scenario faults)."""
    t.mark("gen", g)
    a = BASE["t0"] + gen_base(g) + 0x1000
    done = {}
    write = kind in (1, 3, 4, 5)
    if write:
        t.ready("t0", "w", "modulo", 6, 0)
        if kind == 4:
            t.ready("t0", "w", "always")
            t.gate("t0", "b", False)
        w, wlast, b0 = t.snap("t0", "w"), t.snap("t0", "wlast"), t.snap("s0", "b")
        done["b"] = b0
        t.write("s0", a, beats=4 if kind == 4 else 16, id=1, cache=2)
        if kind == 4:
            t.wait("t0", "wlast", ">=", 1, since=wlast, limit=20000, label=f"TRIGGER_{g}")
            t.delay(50)
            t.expect_now("s0", "b", "==", 0, f"SCENARIO_UNREACHED_{g}", since=b0)
        else:
            beats = 1 if offset is not None else {1: 3, 3: 2, 5: 3}[kind]
            if late:
                # Negative control N5: the trigger waits for the burst's last beat.
                t.wait("t0", "wlast", ">=", 1, since=wlast, limit=20000, label=f"TRIGGER_{g}")
            else:
                t.wait("t0", "w", ">=", beats, since=w, limit=20000, label=f"TRIGGER_{g}")
            if offset:
                t.delay(offset)
            t.expect_now("t0", "wlast", "==", 0, f"SCENARIO_UNREACHED_{g}", since=wlast)
    else:
        t.ready("s0", "r", "modulo", 8, 0)
        r, rlast, r0 = t.snap("t0", "r"), t.snap("t0", "rlast"), t.snap("s0", "rlast")
        done["rlast"] = r0
        s0r = t.snap("s0", "r")
        t.read("s0", a, beats=16, id=1, cache=2)
        if kind == 2:
            t.wait("s0", "r", ">=", 2, since=s0r, limit=20000, label=f"TRIGGER_{g}")
            t.expect_now("s0", "rlast", "==", 0, f"SCENARIO_UNREACHED_{g}", since=r0)
        else:
            beats = 1 if offset is not None else {6: 8, 7: 10}[kind]
            t.wait("t0", "r", ">=", beats, since=r, limit=20000, label=f"TRIGGER_{g}")
            if offset:
                t.delay(offset)
            t.expect_now("t0", "rlast", "==", 0, f"SCENARIO_UNREACHED_{g}", since=rlast)
    t.mark("cut", kind)
    domain = {1: "src", 2: "src", 3: "net", 4: "tgt", 5: "tgt", 6: "tgt", 7: "net"}[kind]
    if control != "answered":
        t.port_reset(PORT[domain], 40)
    t.ready("t0", "w", "always")
    t.ready("s0", "r", "always")
    t.gate("t0", "b", True)
    # The quiet window: nothing new is issued (O8).
    if control == "quiet_access":
        t.write("s1", BASE["t3"] + gen_base(g) + 0x300)
    t.delay(400)
    t.mark("quiet", g)
    if kind == 2:
        # O10: t0 completes the read, whose answer s0's endpoint drops.
        t.wait("t0", "rlast", ">=", 1, since=rlast, limit=20000, label=f"DRAINED_{g}")
    if offset is None:
        isolation(t, g, backbone)
        if kind == 4 or kind == 6 and not backbone:
            freed(t, g)
    if kind >= 4:
        # O9: s0 never completes the cut transaction.
        t.expect_now("s0", "b" if write else "rlast", "==", 0, f"CUT_COMPLETED_{g}",
                     since=done["b" if write else "rlast"])
    recover(t, g, common=control != "no_common_reset")


def scenario(t, seed, sweep=False, late=False, backbone=False, control=None, at=None):
    """control at generation `at`: a negative control's change of the tape."""
    for s in ("s0", "s1", "s3"):
        t.ready(s, "b", "always")
        t.ready(s, "r", "always")
    for tg in ("t0", "t4"):
        for c in ("aw", "w", "ar"):
            t.ready(tg, c, "always")
    t.reset(4)
    t.delay(10)
    for g in range(1, 8):
        cut(t, g, g, late=late and g == 1, backbone=backbone, control=control if at == g else None)
    abandoned(t, 8)
    for k, (source, write, packets) in enumerate(WIDE):
        wide_abandoned(t, 9 + k, source, write, packets)
    if sweep:
        first = 9 + len(WIDE)
        for k, offset in enumerate(SWEEP):
            cut(t, first + k, 1, offset=offset)
            cut(t, first + len(SWEEP) + k, 6, offset=offset)
    t.end()


# ---------------------------------------------------------------------------------------------
# Negative controls: N1 (peer_unmasked), N2 (response_undropped) and N6 (rewire) are the clocked
# graphs' text faults; N3 breaks a crossing's receiving side, N4 its sending side; N5 and N8 to
# N10 are scenario faults.
def enables(rtl, mapped, role="receiver"):
    """(cut, enable net) of every crossing's receiving or sending side: its announce
    resynchronized from the other side and its own on."""
    out = []
    for cut in mapped["network"]["cdc"]["cuts"]:
        side = cut.get("handshake", {}).get(role)
        if not side:
            continue
        last, on = (x.split("/")[1] for x in (side["announce_sync"][-1], side["on"]))
        found = re.findall(rf"^assign (nl_internal_v\d+) = \(nl_internal_v{last} & "
                           rf"nl_internal_v{on}\);$", rtl, re.M)
        if len(found) != 1:
            return None
        out.append((cut, found[0]))
    return out


def read_index_keeps(rtl, mapped):
    """Text fault N3: every crossing's read index keeps its value while its side's enable is
    low instead of clearing: after the sending side's reset alone the reader resumes at an old
    slot."""
    subs = []
    for cut, enable in enables(rtl, mapped) or []:
        index = "nl_internal_v" + cut["read_index"].split("/")[1]
        nxt = re.findall(rf"^    {index} <= (nl_internal_v\d+);$", rtl, re.M)
        if len(nxt) != 1:
            return [(r"\Zanchor-not-found", "", 1)]
        subs.append((rf"^assign {nxt[0]} = \({enable} \? (nl_internal_v\d+) : "
                     rf"nl_internal_v\d+\);$", rf"assign {nxt[0]} = \1;", 1))
    return subs or [(r"\Zanchor-not-found", "", 1)]


def input_unqualified(rtl, mapped):
    """Text fault N4: every crossing's sending side takes words whatever its enable: while the
    other side is reset it accepts a word and then drops it with its queue."""
    subs = []
    for _, enable in enables(rtl, mapped, "sender") or []:
        subs.append((rf"^assign (nl_internal_v\d+) = \((nl_internal_v\d+) & {enable}\);$",
                     r"assign \1 = \2;", 1))
    return subs or [(r"\Zanchor-not-found", "", 1)]


def configurations(peer_unmasked, response_undropped, rewire):
    """The star networks (all registers reset, with the offset sweep; control registers only)
    and the backbone network; the negative controls run on the first."""
    text = dict(op="text", contract="AXI.RESET.DOMAIN")
    scenario = dict(op="scenario", contract="AXI.RESET.DOMAIN")
    faults = [
        dict(id="inflight_peer_unmasked", subs=peer_unmasked,
             expect=["TAPE.WAIT_TIMEOUT:ISOLATION_2", "SOURCE_UNANSWERED"],
             note="N1: every crossing side's enable ignores the other side: after the reset of "
                  "src alone the APB source's crossing replays words and its next access hangs",
             **text),
        dict(id="inflight_response_undropped", subs=response_undropped,
             expect=["SOURCE_R_DATA", "SOURCE_EARLY", "TAPE.DRAIN_TIMEOUT:DROPPED_8"],
             note="N2: a source endpoint takes a response no burst of its own waits for: the "
                  "abandoned read's data answers the next read of s2", **text),
        dict(id="inflight_read_index_keeps", subs=read_index_keeps,
             expect=["TAPE.WAIT_TIMEOUT:ISOLATION_2", "SOURCE_UNANSWERED"],
             note="N3: a crossing's read index survives the other side's reset", **text),
        dict(id="inflight_input_unqualified", subs=input_unqualified,
             expect=["TAPE.WAIT_TIMEOUT:TRIGGER_1", "SOURCE_UNANSWERED"],
             note="N4: a crossing takes words while the other side is reset: a request toward "
                  "the reset domain is lost instead of waiting for the release", **text),
        dict(id="inflight_unreached", params=dict(late=True),
             expect=["TAPE.EXPECT:SCENARIO_UNREACHED_1"],
             note="N5: the reset of generation 1 waits for the burst's last beat", **scenario),
        dict(id="inflight_target_on_net_reset", subs=rewire("tgt", "net"),
             expect=["TAPE.WAIT_TIMEOUT:FREED_4"],
             note="N6: the tgt state resets from rst_net_n: the reset of tgt alone leaves t0's "
                  "endpoint waiting for the forgotten B", **text),
        dict(id="inflight_target_wdata", expect=["ORACLE", "TARGET_WRITE_BYTES"],
             subs=[(r"^assign nl_t_t0_wdata = (nl_internal_v\d+);$",
                    r"assign nl_t_t0_wdata = (\1 ^ 64'd1);", 1)],
             note="N7: t0's W data has bit 0 flipped: the cut burst's beats differ from s0's "
                  "(CUT_TARGET_BYTES)", **text),
        dict(id="inflight_source_rdata", expect=["ORACLE", "SOURCE_R_DATA"],
             subs=[(r"^assign nl_i_s0_rdata = (nl_internal_v\d+);$",
                    r"assign nl_i_s0_rdata = (\1 ^ 128'd1);", 1)],
             note="N11: s0's R data has bit 0 flipped: the cut read's beats differ from t0's "
                  "(CUT_SOURCE_DATA)", **text),
        dict(id="inflight_answered", params=dict(control="answered", at=4),
             expect=["TAPE.EXPECT:CUT_COMPLETED_4"],
             note="N8: generation 4 without its reset: t0 answers the held write", **scenario),
        dict(id="inflight_quiet_access", params=dict(control="quiet_access", at=1),
             expect=["ORACLE"],
             note="N9: an access starts at t3 in the quiet window of generation 1 "
                  "(CLEARED_WORD_ACCESS)", **scenario),
        dict(id="inflight_no_common_reset", params=dict(control="no_common_reset", at=1),
             expect=["TAPE.DRAIN_TIMEOUT:RECOVER_1"],
             note="N10: generation 1 ends without its common reset: t0 still waits for the cut "
                  "burst", **scenario)]
    return [configuration("inflight-all", sweep=True, faults=faults),
            configuration("inflight-minimal", minimal=True),
            configuration("inflight-backbone", backbone=True)]
