"""Sub-page APB, AHB and AXI4-Lite windows under a 40-bit system address
through the public CLI. One
registration per group of modes; the first mode of a group carries the
designated faults, the others are companion configurations:

  graph    mappings, graph, selective, clocked, tiles, crossing
  uniform  uniform, widths_uniform
  widths   widths, ahb_shaped, widths1024
  packets  packets, attributes
  fixed    fixed, fixed_widths, fixed_packets

The case table and the per-beat oracle live in subpage_oracle.py (an
independent decode of the declared windows). From it every source burst
gets its response codes (B, every R beat) and the ordered target-local
words its target accesses visit; the offline checker judges bytes, strobes,
read data, attributes and attribution, and the oracle hook below the word
sequences and the AHB burst rules at the AHB target."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subpage_oracle as so  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402
from nlverif import model as nlmodel  # noqa: E402

PARAMS = globals().get("PARAMS", {})
GROUPS = {"graph": ("mappings", "graph", "selective", "clocked", "tiles", "crossing"),
          "uniform": ("uniform", "widths_uniform"),
          "widths": ("widths", "ahb_shaped", "widths1024"),
          "packets": ("packets", "attributes"),
          "fixed": ("fixed", "fixed_widths", "fixed_packets")}
GROUP = PARAMS.get("group", "graph")
MODES = GROUPS[GROUP]

LITE_TARGETS = {"t7"}
CLOCKS = dict(main=10.0, middle=14.0, peripheral=18.0)


def bfms(mode):
    clock = "peripheral" if so.base_mode(mode) == "clocked" else "main"
    out = [dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
           dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
           dict(name="s2", kind="axi_master", endpoint="initiator:s2")]
    for k, (name, t) in enumerate(so.TARGETS.items()):
        entry = dict(name=name, endpoint=f"target:{name}", salt=k + 1, clock=clock)
        if t["kind"] in ("apb4", "apb3", "apb2"):
            entry.update(kind="apb_slave", dialect=int(t["kind"][-1]))
        elif t["kind"] == "ahb_v3":
            entry.update(kind="ahb_slave")
        else:
            entry.update(kind="axi_slave", lite=name in LITE_TARGETS)
        out.append(entry)
    if so.base_mode(mode) in ("widths", "widths_uniform"):
        out.append(dict(name="t8", kind="axi_slave", endpoint="target:t8", salt=9))
    return out


def model(mode, cfg):
    return dict(routes=nlconfig.routes(cfg),
                error_codes=mode not in ("uniform", "widths_uniform"),
                targets=nlconfig.targets(cfg, error_data_zero=True),
                **({"split_narrow": ["s1"]} if so.base_mode(mode) == "packets" else {}))


def config_entry(mode):
    cfg = so.configuration(mode)
    return dict(label=mode, dut=dict(source="cli", config=cfg), model=model(mode, cfg),
                bfms=bfms(mode), scenario_args=dict(mode=mode),
                **({"clocks": CLOCKS} if so.base_mode(mode) == "clocked" else {}))


def text_fault(ident, port, replacement, contract, expect):
    return dict(id=ident, op="text", contract=contract,
                subs=[(rf"(assign {port} = )([^;]+);", replacement, 1)], expect=expect)


CONFIGS = [config_entry(m) for m in MODES]
CASE = dict(
    name="subpage_" + GROUP,
    contracts=["APB.SUBPAGE.DECODE", "APB.TARGET.ACCESS", "AXI.LITE.WORD_ACCESS",
               "AXI.ERR.UNMAPPED_DECERR", "AXI.ERR.READ_ZERO", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.W.UNSTROBED_ZERO", "AXI.ID.RETURN"],
    dut=CONFIGS[0]["dut"],
    bfms=CONFIGS[0]["bfms"],
    model=CONFIGS[0]["model"],
    scenario_args=CONFIGS[0]["scenario_args"],
    configs=[dict(c, label="") if k == 0 else c for k, c in enumerate(CONFIGS)],
    max_cycles=900000,
    # The graph group also runs its companions: the mappings mode replaced
    # graph as the main configuration.
    negative_runs_all=GROUP == "graph",
    # The scenario's single RTL corruptions, on the first mode of the group.
    faults=[
        # The narrow APB port drops its top bit: 0x80 and 0x00 alias.
        text_fault("paddr_alias", "nl_t_t1_paddr", r"\g<1>(\g<2>) & 8'h7f;",
                   "AXI.ROUTE.LOCAL_OFFSET",
                   ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES",
                    "SOURCE_R_DATA", "ORACLE"]),
        # Target data and OKAY on every read beat, DECERR beats included.
        text_fault("rresp_okay", "nl_i_s0_rresp", r"\g<1>2'b00;", "APB.SUBPAGE.DECODE",
                   ["SOURCE_R_RESP", "SOURCE_R_DATA"]),
        # OKAY for every write burst.
        text_fault("bresp_okay", "nl_i_s0_bresp", r"\g<1>2'b00;", "APB.SUBPAGE.DECODE",
                   ["SOURCE_B_RESP"]),
        # A constant write ID and a constant read ID.
        text_fault("bid_constant", "nl_i_s0_bid", r"\g<1>3'd5;", "AXI.ID.RETURN",
                   ["AXI.B.UNEXPECTED", "SOURCE_UNANSWERED", "SOURCE_EXTRA_B",
                    "TAPE.DRAIN_TIMEOUT:CASE_0"]),
        text_fault("rid_constant", "nl_i_s1_rid", r"\g<1>3'd6;", "AXI.ID.RETURN",
                   ["AXI.R.UNEXPECTED", "SOURCE_UNANSWERED", "SOURCE_EXTRA_R",
                    "SOURCE_R_COUNT"]),
        # The mapping of t6 aliased into the page of t5 loses its local
        # address: its accesses land on the lower half.
        text_fault("t6_alias_local", "nl_t_t6_paddr", r"\g<1>(\g<2>) & 10'h1ff;",
                   "AXI.ROUTE.LOCAL_OFFSET",
                   ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES",
                    "SOURCE_R_DATA", "ORACLE"]),
        # The AXI4 target loses bit 13 of its read address: only its relocated
        # sub-page mappings (local 0x6000 up, mappings mode) land on the wrong
        # words.
        *([text_fault("t0_subpage_local", "nl_t_t0_araddr", r"\g<1>(\g<2>) & 40'hffffffdfff;",
                      "AXI.ROUTE.LOCAL_OFFSET",
                      ["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "SOURCE_R_DATA", "ORACLE"])]
          if GROUP == "graph" else []),
        # The AXI4-Lite target loses its write protection attributes.
        text_fault("lite_awprot_zero", "nl_t_t7_awprot", r"\g<1>3'b000;", "APB.TARGET.ACCESS",
                   ["TARGET_PROT"]),
    ],
)


def _regions(t, name, errors, previous):
    """Error words of one case at one target (two slots; an
    AXI4-Lite target answers the second with DECERR)."""
    kind = so.TARGETS[name]["kind"]
    if not errors and not previous:
        return
    slots = (("reg0", 2), ("reg1", 3 if name in LITE_TARGETS else 2))
    if kind == "ahb_v3" and len(errors) > 1:
        raise ValueError("the AHB target model has one error region")
    for k, (reg, code) in enumerate(slots[:1] if kind == "ahb_v3" else slots):
        lo, hi = (errors[k], errors[k] + 4) if k < len(errors) else (0, 0)
        t.set(name, reg + "_lo", lo)
        t.set(name, reg + "_hi", hi)
        if kind in ("axi4", "axi4_lite"):
            t.set(name, reg + "_w", code)
            t.set(name, reg + "_r", code)
        else:
            t.set(name, reg + "_c", 1)


def _expectation(o, mode):
    """Response codes and the ordered target words of one source burst."""
    if o["write"] and o["burst"] == 0 and o["address"] % (1 << o["size"]):
        # The source BFM drives an unaligned FIXED write with the start's
        # lanes on every beat (AXI: the valid lanes of a FIXED burst are the
        # same for every beat).
        width = so.SOURCES[o["source"]] // 8
        end = (o["address"] >> o["size"] << o["size"]) + (1 << o["size"])
        first = sum(1 << (b % width) for b in range(o["address"], end))
        o = dict(o, keeps=[first & k for k in o.get("keeps", [first] * o["beats"])])
    accesses, results = so.single(o, mode)
    words = [(target, local // 4 * 4) for target, _, local, *_ in accesses]
    if o["write"]:
        return dict(resp=results[0][2], target_words=words)
    return dict(beat_resps=[r[3] for r in results], target_words=words)


def _issue(t, c, o, ident, mode, partner):
    width = so.SOURCES[o["source"]] // 8
    keep = o["keep"]
    exp = _expectation(o, mode)
    if partner:
        exp["partner"] = True
    cache = o.get("cache", c.get("cache", 0))
    burst = ("fixed", "incr", "wrap")[o["burst"]]
    if o["write"]:
        masks = ([keep & k for k in o["keeps"]] if "keeps" in o
                 else [keep] if keep != (1 << width) - 1 else None)
        if masks is not None:
            t.strobes(o["source"], masks)
        t.write(o["source"], o["address"], beats=o["beats"], size=o["size"], burst=burst,
                id=ident, cache=cache, prot=o.get("prot", c.get("prot", 2)),
                strobe="full" if masks is None else "table", **exp)
    else:
        t.read(o["source"], o["address"], beats=o["beats"], size=o["size"], burst=burst,
               id=ident, cache=cache, prot=o.get("prot", c.get("prot", 0)), **exp)


def scenario(t, seed, mode="graph"):
    for s in so.SOURCES:
        t.ready(s, "r", "modulo", 3, 0)
        if so.SOURCE_PORTS[s].get("access") != "read_only":
            t.ready(s, "b", "modulo", 4, 1)
    for name, spec in so.TARGETS.items():
        if spec["kind"] in ("apb4", "apb3", "apb2", "ahb_v3"):
            t.set(name, "wait", 6 if mode == "crossing" and name == "t3" else 1)
        elif name in LITE_TARGETS:
            t.ready(name, "aw", "modulo", 3, 1)
            t.ready(name, "w", "modulo", 4, 2)
            t.ready(name, "ar", "modulo", 3, 2)
        else:
            t.ready(name, "ar", "modulo", 3, 2)
        if mode in so.FIXED_MODES and name in so.FIXED_SINGLE:
            # FIFO word: every read access returns a new value, so each FIXED
            # read beat must carry the data of its own access.
            t.set(name, "fifo_en", 1)
            t.set(name, "fifo_addr", so.FIFO_LOCAL)
    t.reset(10)
    t.delay(20)
    previous = set()
    for number, c in enumerate(so.cases(mode)):
        t.mark("case", number)
        errors = {}
        for target, local in c["errors"]:
            errors.setdefault(target, []).append(local)
        for name in sorted(set(errors) | previous):
            _regions(t, name, errors.get(name, []), name in previous)
        previous = set(errors)
        # Every 3-bit ID value recurs; a partner reuses the ID from the other
        # source (or the other direction of the same one). A batch follows
        # with IDs of its own.
        ident = c.get("id", (number * 5 + 1) % 8)
        _issue(t, c, c, ident, mode, "partner" in c)
        if "partner" in c:
            _issue(t, c, c["partner"], ident, mode, True)
        for o in c.get("batch", ()):
            _issue(t, c, o, o["id"], mode, False)
        t.drain(limit=40000, label=f"CASE_{number}")
        t.delay(6)
    t.end()


def _target_words(checker, sources):
    """The ordered target-local words each source burst's target accesses
    visit, against the oracle (a burst with a concurrent partner: as a
    multiset, the partner may interleave at the same target)."""
    out = []
    for s in sources:
        want = s.expect.get("target_words")
        if want is None or not s.done:
            continue
        got = []
        for tt in s.targets:
            for i in range(max(tt.length, len(tt.beats))):
                a = nlmodel.beat_addr(tt.addr, tt.length, tt.size, tt.burst, i)
                got.append((tt.port, a // 4 * 4))
        want = [tuple(w) for w in want]
        if s.expect.get("partner"):
            got, want = sorted(got), sorted(want)
        if got != want:
            out.append(dict(detail="target words", tag=s.tag, port=s.port, dir=s.dir,
                            got=[(p, hex(a)) for p, a in got[:6]],
                            want=[(p, hex(a)) for p, a in want[:6]],
                            n_got=len(got), n_want=len(want)))
    return out


def _ahb_beats(code):
    return 0 if code < 2 else 4 << ((code - 2) // 2)


def _ahb_rules(checker, events, mode):
    """AHB burst rules on every accepted NONSEQ/SEQ address phase (AHB-Lite
    3.5, AHB5 3.6): a fixed-length burst is exactly its beats, NONSEQ then
    SEQ; a SEQ continues the open burst at its next address (WRAP inside the
    container, HADDR wraps at the local port width) with unchanged HBURST,
    HSIZE, HWRITE and HPROT; INCRn stays inside the 1 KiB boundary and the
    window. Shaping adds no undefined-length INCR, and SINGLE writes when
    writes also complete."""
    out = []
    shaped = mode in so.SHAPED_MODES
    for name, spec in so.TARGETS.items():
        if spec["kind"] != "ahb_v3":
            continue
        wrap_at = 1 << spec["bits"]
        open_, left, last = False, 0, None
        for e in events:
            if e.get("p") != name:
                continue
            if e.get("k") == "rst":
                open_, left = False, 0
                continue
            if e.get("k") != "ahb_addr":
                continue
            a, tr, code, size = int(e["a"], 16), e["tr"], e["bu"], e["sz"]
            ctrl = (code, size, e["w"], e["pr"])
            if tr == 2:
                if left > 0:
                    out.append(dict(detail="AHB fixed-length burst ended early", port=name,
                                    t=e["t"]))
                beats = _ahb_beats(code)
                if code > 1 and code % 2 == 1 and (a % 1024 + (beats << size) > 1024
                                                    or a + (beats << size) > spec["size"]):
                    out.append(dict(detail="AHB INCRn crosses its block", port=name, a=hex(a)))
                if shaped and code == 1:
                    out.append(dict(detail="AHB undefined-length INCR", port=name, a=hex(a)))
                if shaped and e["w"] and code != 0:
                    out.append(dict(detail="AHB write not SINGLE", port=name, a=hex(a)))
                last, left = (a, ctrl), max(beats - 1, 0)
                open_ = code == 1 or left > 0
            elif tr == 3:
                if not open_ or ctrl != last[1]:
                    out.append(dict(detail="AHB SEQ control", port=name, a=hex(a), t=e["t"]))
                    continue
                prev = last[0]
                if code >= 2 and code % 2 == 0:
                    span = _ahb_beats(code) << size
                    nxt = prev // span * span + (prev + (1 << size)) % span
                else:
                    nxt = prev + (1 << size)
                if a != nxt % wrap_at:
                    out.append(dict(detail="AHB SEQ address", port=name, a=hex(a),
                                    want=hex(nxt % wrap_at)))
                last = (a, ctrl)
                if code >= 2:
                    left -= 1
                    open_ = left > 0
        if left > 0:
            out.append(dict(detail="AHB fixed-length burst never completed", port=name))
    return out


def oracle(checker, events, sources, targets):
    mode = checker.case.get("scenario_args", {}).get("mode", MODES[0])
    return _target_words(checker, sources) + _ahb_rules(checker, events, mode)


CASE["oracle"] = oracle
