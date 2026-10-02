"""Clock-event formal model of the emitted asynchronous FIFO crossing.

Both clock edges become independent nondeterministic steps of one formal tick, so
every interleaving of sender and receiver edges is explored. Each synchronizer
first stage may resolve every bit that changed in the previous tick to its old or
new value, chosen freely per bit. The FIFO is data independent, so one freely
chosen write carries a marked word and ORDER checks that it leaves exactly at its
position, once. The environment asserts both side resets together at any tick and
releases them in any order; the sender offers no word while its side is in reset.

oneside_model checks a FIFO under resets of either side alone, asserted and released
at any tick. Words carry their class in data[1:0]: 0 before one freely chosen marked
write, 1 for it, 2 after. Delivered classes never decrease, the marked word leaves at
most once and only after its write, no more words leave than were written, and a
later word overtakes the marked one only if a reset was asserted at or after its write. With
the reset handshake the counts move one Gray bit per update while their side is
enabled, and with both sides enabled the count distance is at most the depth. With
reset_all payload registers clear in a one-side reset, so a word the receiver selected
just as the sender's reset arrived may leave with a cleared payload inside the
synchronization window: the delivered class sequence restarts at a reset assertion.
"""
import re

PROPERTIES = ("NO_OVERFLOW", "NO_UNDERFLOW", "ORDER", "SLOT_STABLE", "GRAY_ONE_BIT_W", "GRAY_ONE_BIT_R",
              "OUTPUT_QUALIFIED", "RESET_CANCEL")
COVERS = ("COVER_FULL", "COVER_WRAP", "COVER_DRAIN", "COVER_MARKED")
DATA = ("CLASS_ORDER", "MARK_ONCE", "WRITTEN", "LOSS_ONLY_AFTER_RESET")
HANDSHAKE = ("GRAY_ENABLED_W", "GRAY_ENABLED_R", "POINTERS", "SLOT_STABLE", "OUTPUT_QUALIFIED")


def clock_events(rtl, meta, joint):
    """Clock edges as nondeterministic tick steps and the sampling-skew model on every
    synchronizer first stage; returns (rtl, extra lines). With joint resets the prior value
    of a source is zero while its side is in reset; otherwise it is the value a tick ago."""
    v = lambda i: f"nl_internal_v{i}"
    clocks = {c: d for d, c in enumerate(meta["clocks"])}
    rst = [v(i) for i in meta["resets"]]

    def reset_group(m):
        d = clocks[int(m.group(1))]
        return (f"always @(posedge nl_tick) begin\n  if (!{m.group(2)}) begin\n{m.group(3)}"
                f"  end else if (nl_step{d}) begin\n")
    blocks = rtl.count("always @(")
    rtl, groups = re.subn(r"always @\(posedge nl_internal_v(\d+) or negedge (nl_internal_v\d+)\) begin\n"
                          r"  if \(!\2\) begin\n(.*?)  end else begin\n", reset_group, rtl, flags=re.S)
    rtl, held = re.subn(r"always @\(posedge nl_internal_v(\d+)\) begin\n",
                        lambda m: f"always @(posedge nl_tick) if (nl_step{clocks[int(m.group(1))]}) begin\n", rtl)
    if groups != 2 or groups + held != blocks or "negedge" in rtl:
        raise AssertionError(f"unexpected register groups: {groups} reset, {held} held")
    ports = ["  input wire nl_tick,", "  input wire nl_step0,", "  input wire nl_step1,", "  input wire nl_mark,"]
    extra = []
    for row in meta["first_stages"]:
        x, y, w = row["stage"], row["source"], row["width"]
        r = f"[{w - 1}:0] " if w > 1 else ""
        old = f"    {v(x)} <= {v(y)};"
        if rtl.count(old) != 1:
            raise AssertionError(f"first stage v{x} not found exactly once")
        rtl = rtl.replace(old, f"    {v(x)} <= ({v(y)} & ~nl_skew_v{x}) | (nl_prior_v{y} & nl_skew_v{x});")
        ports.append(f"  input wire {r}nl_skew_v{x},")
        prior = f"{rst[1 - row['domain']]} ? {v(y)} : 0" if joint else v(y)
        if f"reg {r}nl_prior_v{y};" not in extra:
            extra += [f"reg {r}nl_prior_v{y};", f"always @(posedge nl_tick) nl_prior_v{y} <= {prior};"]
    header = f"module {meta['module']} (\n"
    if rtl.count(header) != 1:
        raise AssertionError("module header not found")
    return rtl.replace(header, header + "\n".join(ports) + "\n"), extra


def cover_locations(log, reached):
    """Keep the full module and instance path of each named cover event."""
    event = (r"Reached cover statement in step \d+ at " if reached else
             r"Unreached cover statement at ")
    locations = set()
    for line in log.splitlines():
        match = re.search(event, line)
        if not match:
            continue
        parts = line[match.end():].split()
        if parts:
            location = parts[0]
            if location.endswith(":") and len(parts) > 1:
                location = location[:-1] + "." + parts[1]
            locations.add(location)
    return locations


def internal_outputs(rtl):
    """Keep observation signals internal so unused logic can be removed."""
    header = re.search(r"(module\s+\w+\s*\()(.*?)(\);\n)", rtl, re.S)
    if not header:
        raise AssertionError("unexpected miter header")
    ports = [port.strip() for port in header[2].split(",")]
    if not all(port.startswith(("input wire ", "output wire ")) for port in ports):
        raise AssertionError("unexpected miter ports")
    inputs = [port for port in ports if port.startswith("input wire ")]
    outputs = [port for port in ports if port.startswith("output wire ")]
    declarations = "\n".join(port.replace("output wire ", "wire ", 1) + ";" for port in outputs)
    new_header = header[1] + "\n  " + ",\n  ".join(inputs) + "\n);\n" + declarations + "\n"
    return rtl[:header.start()] + new_header + rtl[header.end():]


def finish(rtl, extra):
    marker = "endmodule\n`default_nettype wire\n"
    if not rtl.endswith(marker):
        raise AssertionError("unexpected module tail")
    return internal_outputs(rtl[: -len(marker)] + "\n".join(extra) + "\n" + marker)


def model(rtl, meta, omit=(), combine=False):
    v = lambda i: f"nl_internal_v{i}"
    rst = [v(i) for i in meta["resets"]]
    rtl, extra = clock_events(rtl, meta, True)
    depth, w = meta["depth"], meta["count_bits"]
    data = meta["fields"][0]["width"]
    if data < 8:
        raise AssertionError("the sequence payload needs at least 8 data bits")
    wc, rc = v(meta["write_count"]), v(meta["read_count"])
    write, read = v(meta["write"]), v(meta["read"])
    wi, ri = v(meta["write_index"]), v(meta["read_index"])
    iw = max(1, (depth - 1).bit_length())
    o = "(nl_step1 && nl_stream_out_valid && nl_stream_out_ready)"
    wr = f"(nl_step0 && {write})"
    extra += [
        "reg nl_f_init = 1'b1;",
        "always @(posedge nl_tick) nl_f_init <= 1'b0;",
        # A side enters reset only with the other side in reset too; the two release in
        # any order. One side alone is in reset only before its release.
        f"wire nl_reset_n = {rst[0]} || {rst[1]};",
        "reg nl_f_pr0, nl_f_pr1;",
        f"always @(posedge nl_tick) begin nl_f_pr0 <= {rst[0]}; nl_f_pr1 <= {rst[1]}; end",
        "always @* if (nl_f_init) assume(!nl_reset_n);",
        f"always @* if (!nl_f_init && ((nl_f_pr0 && !{rst[0]}) || (nl_f_pr1 && !{rst[1]}))) assume(!nl_reset_n);",
        f"always @* if (!{rst[0]}) assume(!nl_stream_valid);",
        # occ: words in the slots; pend: words in the slots or the forward register.
        # One write chosen freely carries data 1, every other write carries 0. before
        # counts the outputs still due ahead of that marked word.
        "reg [4:0] nl_f_occ, nl_f_pend, nl_f_before, nl_f_lead, nl_f_wseq, nl_f_rseq;",
        f"reg nl_f_prst, nl_f_sent, nl_f_done, nl_f_wrapped; reg [{w - 1}:0] nl_f_pwc, nl_f_prc;",
        f"reg nl_f_pwrite; reg [{iw - 1}:0] nl_f_pwi;",
        "wire nl_f_marking = nl_mark && !nl_f_sent;",
        "always @* assume(nl_stream_in_f0 == nl_f_marking);",
        "always @(posedge nl_tick) begin",
        f"  nl_f_prst <= nl_reset_n; nl_f_pwc <= {wc}; nl_f_prc <= {rc};",
        f"  nl_f_pwrite <= nl_reset_n && {wr}; nl_f_pwi <= {wi};",
        "  if (!nl_reset_n) begin",
        "    nl_f_occ <= 0; nl_f_pend <= 0; nl_f_before <= 0; nl_f_lead <= 0; nl_f_wseq <= 0; nl_f_rseq <= 0;",
        "    nl_f_sent <= 0; nl_f_done <= 0; nl_f_wrapped <= 0;",
        "  end else begin",
        f"    nl_f_occ <= nl_f_occ + {wr} - (nl_step1 && {read});",
        f"    nl_f_pend <= nl_f_pend + {wr} - {o};",
        f"    if ({wr} && nl_f_wseq != 5'd31) nl_f_wseq <= nl_f_wseq + 5'd1;",
        f"    if ({o} && nl_f_rseq != 5'd31) nl_f_rseq <= nl_f_rseq + 5'd1;",
        f"    if ({wr} && nl_f_wseq == 5'd{(1 << w)}) nl_f_wrapped <= 1;",
        f"    if ({wr} && nl_f_marking) begin nl_f_sent <= 1; nl_f_before <= nl_f_pend - {o}; nl_f_lead <= nl_f_pend - {o}; end",
        f"    else if ({o} && nl_f_sent && !nl_f_done) begin",
        "      if (nl_f_before == 0) nl_f_done <= 1; else nl_f_before <= nl_f_before - 5'd1;",
        "    end",
        "  end",
        "end",
        "always @* if (nl_reset_n && !nl_f_init) begin",
    ]
    checks = {
        "NO_OVERFLOW": f"nl_f_occ <= {depth}",
        "NO_UNDERFLOW": f"!(nl_step1 && {read}) || nl_f_occ != 0",
        "ORDER": f"!{o} || nl_stream_out_f0 == (nl_f_sent && !nl_f_done && nl_f_before == 0)",
        # The slot being read was not written in this tick or the previous one.
        "SLOT_STABLE": f"!(nl_step1 && {read}) || (!({wr} && {wi} == {ri}) && !(nl_f_pwrite && nl_f_pwi == {ri}))",
        "GRAY_ONE_BIT_W": f"!nl_f_pr0 || !{rst[0]} || (({wc} ^ nl_f_pwc) & (({wc} ^ nl_f_pwc) - 1'b1)) == 0",
        "GRAY_ONE_BIT_R": f"!nl_f_pr1 || !{rst[1]} || (({rc} ^ nl_f_prc) & (({rc} ^ nl_f_prc) - 1'b1)) == 0",
        "OUTPUT_QUALIFIED": "nl_stream_out_valid || (nl_stream_out_f0 == 0 && nl_stream_out_f1 == 0)",
        "RESET_CANCEL": f"nl_f_prst || ({wc} == 0 && {rc} == 0 && !nl_stream_out_valid && nl_f_occ == 0)",
    }
    selected = [name for name in PROPERTIES if name not in omit]
    if combine:
        extra.append("  FIFO_ALL: assert(" + " && ".join(f"({checks[name]})" for name in selected) + ");")
    else:
        for name in selected:
            extra.append(f"  {name}: assert({checks[name]});")
    extra += [
        f"  COVER_FULL: cover(nl_f_occ == {depth} && !nl_stream_ready);",
        f"  COVER_WRAP: cover(nl_f_wrapped && nl_stream_out_valid && nl_f_rseq == 5'd{(1 << w)});",
        "  COVER_DRAIN: cover(nl_f_rseq == 5'd2 && nl_f_pend == 0 && !nl_stream_out_valid);",
        f"  COVER_MARKED: cover(nl_f_done && nl_f_lead == {depth});",
        "end",
    ]
    return finish(rtl, extra)


def oneside_model(rtl, meta, omit=()):
    v = lambda i: f"nl_internal_v{i}"
    rst = [v(i) for i in meta["resets"]]
    rtl, extra = clock_events(rtl, meta, False)
    hs = meta.get("handshake", [])
    depth, w = meta["depth"], meta["count_bits"]
    wc, rc = v(meta["write_count"]), v(meta["read_count"])
    read, wi, ri = v(meta["read"]), v(meta["write_index"]), v(meta["read_index"])
    o = "(nl_step1 && nl_stream_out_valid && nl_stream_out_ready)"
    wr = f"(nl_step0 && {v(meta['write'])})"
    cls = "nl_stream_out_f0[1:0]"
    extra += [
        "reg nl_f_init = 1'b1;",
        "always @(posedge nl_tick) nl_f_init <= 1'b0;",
        f"always @* if (nl_f_init) assume(!{rst[0]} && !{rst[1]});",
        f"always @* if (!{rst[0]}) assume(!nl_stream_valid);",
        # A side leaves reset only after the other side saw it: that side was in reset too,
        # or took n + 2 clock steps meanwhile.
        "reg [2:0] nl_f_cnt0 = 0, nl_f_cnt1 = 0; reg nl_f_seen0 = 0, nl_f_seen1 = 0, nl_f_pr0 = 0, nl_f_pr1 = 0;",
        "always @(posedge nl_tick) begin",
        f"  nl_f_pr0 <= {rst[0]}; nl_f_pr1 <= {rst[1]};",
    ]
    for d in (0, 1):
        e = 1 - d
        extra += [
            f"  if ({rst[d]}) begin nl_f_cnt{d} <= 0; nl_f_seen{d} <= 0; end",
            f"  else if (!{rst[e]} || (nl_step{e} && nl_f_cnt{d} == {meta['n'] + 1})) nl_f_seen{d} <= 1;",
            f"  else if (nl_step{e}) nl_f_cnt{d} <= nl_f_cnt{d} + 3'd1;",
        ]
    extra += [
        "end",
        f"always @* if (!nl_f_init && !nl_f_pr0 && {rst[0]}) assume(nl_f_seen0);",
        f"always @* if (!nl_f_init && !nl_f_pr1 && {rst[1]}) assume(nl_f_seen1);",
        "reg nl_f_sent = 0, nl_f_mdone = 0, nl_f_reset = 0, nl_f_sat = 0, nl_f_pwrite = 0;",
        f"reg [1:0] nl_f_last = 0; reg [5:0] nl_f_bal = 0; reg [{w - 1}:0] nl_f_pwc, nl_f_prc;",
        f"reg [{max(1, (depth - 1).bit_length()) - 1}:0] nl_f_pwi; reg nl_f_pea = 0, nl_f_pez = 0;",
        "wire nl_f_marking = nl_mark && !nl_f_sent;",
        "always @* assume(nl_stream_in_f0[1:0] == (nl_f_marking ? 2'd1 : nl_f_sent ? 2'd2 : 2'd0));",
        "always @* assume(nl_stream_in_f0[7:2] == 0);",
        # The first tick only applies the resets: the state before it is arbitrary.
        "always @(posedge nl_tick) if (!nl_f_init) begin",
        f"  nl_f_pwc <= {wc}; nl_f_prc <= {rc}; nl_f_pwrite <= {wr}; nl_f_pwi <= {wi};",
    ]
    if hs:
        ea, ez = v(hs[0]["enable"]), v(hs[1]["enable"])
        extra.append(f"  nl_f_pea <= {ea}; nl_f_pez <= {ez};")
    extra += [
        f"  if ({wr} && nl_f_marking) begin nl_f_sent <= 1; nl_f_reset <= !{rst[0]} || !{rst[1]}; end",
        f"  else if (!{rst[0]} || !{rst[1]}) nl_f_reset <= 1;",
        f"  if ({o}) begin nl_f_last <= {cls}; if ({cls} == 2'd1) nl_f_mdone <= 1; end",
    ]
    if meta.get("reset_all"):
        # A word the receiver selected just as the sender's reset arrived can leave with
        # its payload cleared inside the synchronization window: the class sequence restarts.
        extra.append(f"  if (!{rst[0]} || !{rst[1]}) nl_f_last <= 0;")
    extra += [
        f"  if ({wr} && !{o} && nl_f_bal == 6'd63) nl_f_sat <= 1;",
        f"  else if ({wr} && !{o}) nl_f_bal <= nl_f_bal + 6'd1;",
        f"  else if (!{wr} && {o} && nl_f_bal != 0) nl_f_bal <= nl_f_bal - 6'd1;",
        "end",
        "always @* if (!nl_f_init) begin",
    ]
    onebit = lambda x, p: f"(({x} ^ {p}) & (({x} ^ {p}) - 1'b1)) == 0"
    checks = {
        "CLASS_ORDER": f"!{o} || ({cls} != 2'd3 && {cls} >= nl_f_last)",
        "MARK_ONCE": f"!{o} || {cls} != 2'd1 || (nl_f_sent && !nl_f_mdone)",
        "WRITTEN": f"!{o} || nl_f_sat || nl_f_bal != 0",
        "LOSS_ONLY_AFTER_RESET": f"!{o} || {cls} != 2'd2 || nl_f_mdone || nl_f_reset",
        "GRAY_ENABLED_W": f"!nl_f_pea || {onebit(wc, 'nl_f_pwc')}",
        "GRAY_ENABLED_R": f"!nl_f_pez || {onebit(rc, 'nl_f_prc')}",
        "POINTERS": f"!nl_f_pea || !nl_f_pez || (({wc} ^ ({wc} >> 1) ^ ({wc} >> 2) ^ ({wc} >> 3)) - "
                    f"({rc} ^ ({rc} >> 1) ^ ({rc} >> 2) ^ ({rc} >> 3))) % {1 << w} <= {depth}",
        "SLOT_STABLE": f"!(nl_step1 && {read}) || (!({wr} && {wi} == {ri}) && !(nl_f_pwrite && nl_f_pwi == {ri}))",
        "OUTPUT_QUALIFIED": "nl_stream_out_valid || (nl_stream_out_f0 == 0 && nl_stream_out_f1 == 0)",
    }
    for name in DATA + (HANDSHAKE if hs else ()):
        if name not in omit:
            extra.append(f"  {name}: assert({checks[name]});")
    extra += [
        # A word written after a one-side reset of each side leaves; a word is lost.
        "  COVER_MARK_AFTER_RESET: cover(nl_f_mdone && nl_f_last == 2'd1 && nl_f_pea && nl_f_pez);" if hs else
        "  COVER_MARK: cover(nl_f_mdone);",
        "  COVER_LOSS: cover(nl_f_sent && !nl_f_mdone && nl_f_last == 2'd2);",
        "end",
    ]
    return finish(rtl, extra)


GATE = ("GATE_SAFE",)
GATE_COVERS = ("COVER_GATE_CLOSED", "COVER_GATE_WAKE")


def gate_model(rtl):
    """The switch region of generate_async_fifo gate_formal with its clock gate passing every
    edge (the test port high). GATE_SAFE: while the enable is low no gated register would
    change, so stopping their clock then changes nothing and the gated run equals this one. The
    covers show the gate closes and is woken while closed. Reset is asserted in the first cycle
    and free after it."""
    rtl, n = re.subn(r"^nocloom_clock_gate (nl_internal_v\d+)_gate \(\.clock_in\((nl_internal_v\d+)\), "
                     r".*\.clock_out\(\1\)\);$", r"assign \1 = \2;", rtl, flags=re.M)
    if n != 1:
        raise AssertionError(f"expected one clock gate, found {n}")
    return finish(rtl, [
        "reg nl_g_init = 1'b1;",
        "always @(posedge nl_clk) nl_g_init <= 1'b0;",
        "always @* if (nl_g_init) assume(!nl_reset_n);",
        "always @* if (!nl_g_init) begin",
        "  GATE_SAFE: assert(nl_gate_safe);",
        "  COVER_GATE_CLOSED: cover(nl_reset_n && !nl_gate_enable);",
        "  COVER_GATE_WAKE: cover(nl_reset_n && nl_gate_remaining == 0 && nl_gate_wake);",
        "end"])


CELLS = ("SYNC_EQUAL", "CLOCK_EQUAL")
CELL_COVERS = ("COVER_SYNC_HIGH", "COVER_CLOCK_PULSE")


def cell_equivalence(default, bound, library, stages):
    """The cell wrappers of a binding (bound, with the library cell models) against the
    behavioral cell models (default, renamed): from a cleared, clock-low start under free clock,
    reset, data, enable and test, the synchronizers of the given stages and the clock gates give
    the same outputs at every step (multiclock: asynchronous reset and latch timing)."""
    model = default.replace("nocloom_", "nl_model_").replace("NOCLOOM_", "NL_MODEL_")
    return model + bound + library + f"""module nl_cells_equal (
  input wire clock,
  input wire reset_n,
  input wire d,
  input wire enable,
  input wire test
);
wire q_model, q_cell, clock_model, clock_cell;
nl_model_synchronizer #(.STAGES({stages})) u_sync_model (.clock(clock), .reset_n(reset_n), .d(d), .q(q_model));
nocloom_synchronizer #(.STAGES({stages})) u_sync (.clock(clock), .reset_n(reset_n), .d(d), .q(q_cell));
nl_model_clock_gate u_gate_model (.clock_in(clock), .enable(enable), .test(test), .clock_out(clock_model));
nocloom_clock_gate u_gate (.clock_in(clock), .enable(enable), .test(test), .clock_out(clock_cell));
initial assume (!reset_n && !clock);
always @* begin
  SYNC_EQUAL: assert (q_cell == q_model);
  CLOCK_EQUAL: assert (clock_cell == clock_model);
  COVER_SYNC_HIGH: cover (reset_n && q_cell);
  COVER_CLOCK_PULSE: cover (clock_cell && !test);
end
endmodule
"""


ENDPOINT = ("GATE_EQUAL",)
ENDPOINT_HELPERS = ("STATE_EQUAL", "GATE_REST")
ENDPOINT_COVERS = ("COVER_SIDE_CLOSED", "COVER_SIDE_WAKE")


def endpoint_model(rtl, helpers=False, joint=False):
    """A gating-safety miter of generate_async_fifo endpoint_formal or crossing_formal:
    GATE_EQUAL, every output of the gated copy equals the ungated copy's, so stopping a region's
    clock while its enable is low changes nothing at the component's ports. The helpers assert
    STATE_EQUAL, every watched
    register of the gated copy equals the ungated copy's, and GATE_REST, a gate whose last edge
    saw its region idle without a wake still sees it idle. AHB also asserts BURST_STATE:
    a burst retains bus ownership, and each live block fits its packet's remaining transfers.
    These assertions form one inductive target. The covers show the (observed) gate
    closes and is woken while closed. Both copies power up alike (nl_init_equal: their registers
    without a reset start equal), with the resets (nl_reset_n, or a crossing's nl_reset_0_n and
    nl_reset_1_n) asserted in the first cycle. A crossing's sender offers no word while its
    side is in reset; joint (a crossing without the reset handshake) enters reset on both sides
    together, as its contract requires, and releases them in any order. Otherwise the resets are
    free after the first cycle."""
    resets = re.findall(r"input wire (nl_reset_\d_n),", rtl) or ["nl_reset_n"]
    if f"input wire {resets[0]}," not in rtl:
        raise AssertionError("no miter reset")
    live = " && ".join(resets)
    checks = ENDPOINT + (ENDPOINT_HELPERS if helpers else ())
    crossing = []
    if len(resets) == 2:
        crossing = ["always @* if (!nl_reset_0_n) assume(!nl_stream_valid);"]
    if joint:
        crossing += ["reg nl_g_pr0, nl_g_pr1;",
                     "always @(posedge nl_clk) begin nl_g_pr0 <= nl_reset_0_n; nl_g_pr1 <= nl_reset_1_n; end",
                     "always @* if (!nl_g_init && ((nl_g_pr0 && !nl_reset_0_n) || (nl_g_pr1 && !nl_reset_1_n)))",
                     "  assume(!nl_reset_0_n && !nl_reset_1_n);"]
    if "nl_tail_legal" in rtl:
        crossing += ["always @* assume(nl_tail_legal);"]
    properties = {"GATE_EQUAL": "nl_gate_equal", "STATE_EQUAL": "nl_state_equal",
                  "GATE_REST": "nl_gate_rest"}
    if helpers and "output wire nl_burst_invariant," in rtl:
        properties["BURST_STATE"] = "nl_burst_invariant"
        checks += ("BURST_STATE",)
    assertions = [f"  {name}: assert({properties[name]});" for name in checks]
    if helpers:
        assertions = ["  GATE_ALL: assert(" + " && ".join(properties[name] for name in checks) + ");"]
    return finish(rtl, [
        "reg nl_g_init = 1'b1;",
        "always @(posedge nl_clk) nl_g_init <= 1'b0;",
        "always @* if (nl_g_init) assume(!(" + " || ".join(resets) + ") && nl_init_equal);"]
        + crossing + ["always @* if (!nl_g_init) begin"]
        + assertions
        + [f"  COVER_SIDE_CLOSED: cover({live} && !nl_gate_enable);",
           f"  COVER_SIDE_WAKE: cover({live} && nl_gate_wake && nl_gate_idle);",
           "end"])
