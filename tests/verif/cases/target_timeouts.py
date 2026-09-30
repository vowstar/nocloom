"""Target timeouts through the public CLI: two AXI4 sources (m0 with four
bursts in flight) and sixteen targets of every protocol (an APB4 target with
a sub-page window, APB3, AXI4-Lite, AHB and AXI4 targets that answer SLVERR
after expiry, an APB4 target without a timer, an APB4 target with the largest
timeout, an AHB target without undefined-length INCR and a 2-bit HRESP, one
APB4, AXI4-Lite, AHB and AXI4 target whose timeout only reports, and an AXI4
target with completion (four bursts in flight), one without (two), an
AXI4-Lite target with completion (four packets) and an AXI4 target with its
own exclusive monitor (four bursts, completion), each under its one
watchdog).

Each hung target stops at a chosen handshake (a slow word of an APB or AHB
target, a ready or valid held low, W or R stopped after some beats) and
answers late when released. Judged directly: every source response (codes,
data of the beats before expiry, zero after), the status of each timed
target rising and staying until the common reset, the target accesses each
burst made (late completions owned by the burst that waited, none for
bursts answered after expiry, quiet contracts on the target and the source
from expiry to the reset where nothing may start or answer), the counts
of transfers issued and completed at each target, the AHB bus left in BUSY
or IDLE, and the APB and AHB monitors' bound on a wait that the timer must
end (CONTRACT.TIMEOUT.GAP above timeout plus 8 cycles, plus 16 on AHB where
a BUSY wait may precede the pending transfer). The AXI4 read that holds the
target's only slot expires exactly one window after its last progress
although writes wait behind it (the gap of its status edge). Both
configurations gate their clocks (the default); clocked also runs its tape with
the test port high (the same events) and once without clock gates
(clocked-ungated)."""

from nlverif import config as nlconfig

PARAMS = globals().get("PARAMS", {})

TARGETS = {
    # name: kind, window base, window size, timeout cycles (None: no timer), completion
    'pa': ('apb4', 0x10000, 0x100, 1024, True),
    'pb': ('apb3', 0x11000, 0x1000, 2048, True),
    'lt': ('axi4_lite', 0x12000, 0x1000, 1024, True),
    'ah': ('ahb_v3', 0x13000, 0x1000, 1024, True),
    'ax': ('axi4', 0x20000, 0x1000, 1024, True),
    'ok': ('apb4', 0x14000, 0x1000, None, False),
    'lg': ('apb4', 0x15000, 0x1000, 4096, True),
    'tn': ('apb4', 0x1B000, 0x1000, 16, True),
    'hg': ('apb4', 0x1C000, 0x1000, 8192, True),
    'ab': ('ahb_v3', 0x16000, 0x1000, 1024, True),
    'qa': ('apb4', 0x17000, 0x1000, 1024, False),
    'ql': ('axi4_lite', 0x18000, 0x1000, 1024, False),
    'qh': ('ahb_v3', 0x19000, 0x1000, 1024, False),
    'qx': ('axi4', 0x21000, 0x1000, 1024, False),
    'mx': ('axi4', 0x26000, 0x1000, 1024, True),
    'mq': ('axi4', 0x27000, 0x1000, 1024, False),
    'ml': ('axi4_lite', 0x1A000, 0x1000, 1024, True),
    'xe': ('axi4', 0x28000, 0x1000, 1024, True),
}
# Targets with several bursts in flight under their one watchdog, used by m0, a
# source with four bursts in flight (s0 keeps two).
CAPACITY = {'mx': 4, 'mq': 2, 'ml': 4, 'xe': 4}
# Exclusive support at the target itself (AxLOCK passed, EXOKAY answered there).
OWN_MONITOR = {'xe': 'target'}
# Burst shaping per target: 'ab' sends INCR packets as INCR16/8/4 and SINGLE
# blocks, so a timeout can fall on a block's last transfer, and has the
# 2-bit AMBA 2 HRESP; 'ah' keeps the default 1-bit HRESP.
SHAPED = {'ab': {'undefined_length_bursts': False, 'hresp_width': 2}}
# Network exclusive monitors with a timeout, in the configurations exclusive
# and implicit (graph with these targets): an APB4, an AXI4-Lite and an AXI4 target
# that answer after expiry and an AHB target whose timeout only reports. The
# AHB target's 64-byte window is the smallest mapping: s0's default target in
# implicit, while exclusive names the untimed 'ok' instead.
EXCLUSIVE = {
    'xp': ('apb4', 0x22000, 0x1000, 1024, True),
    'xl': ('axi4_lite', 0x25000, 0x1000, 1024, True),
    'xx': ('axi4', 0x24000, 0x1000, 1024, True),
    'xq': ('ahb_v3', 0x23000, 0x40, 1024, False),
}
HUNG = 1_000_000


def targets(mode):
    return TARGETS | (EXCLUSIVE if mode in ('exclusive', 'implicit') else {})


def configuration(mode):
    clocks = [dict(name='main', clock_port='clk_main', reset_port='reset_n', test_port='test')]
    domain = 'main'
    request_node, response_node = dict(name='q0'), dict(name='p0')
    if mode == 'clocked':
        # The targets run on a slower clock: every count uses that clock.
        clocks.append(dict(name='slow', clock_port='clk_slow', reset_port='reset_n', test_port='test'))
        domain = 'slow'
        request_node['clock_domain'] = response_node['clock_domain'] = 'main'
    entries = []
    for name, (kind, base, size, cycles, completion) in targets(mode).items():
        entry = dict(name=name, clock_domain=domain, protocol=kind, data_width=32,
                     mappings=[dict(base=hex(base), size=hex(size))])
        if kind in ('axi4', 'axi4_lite', 'apb4', 'ahb_v3'):
            entry['attributes'] = 'amba'
        if kind == 'axi4':
            entry['len_width'] = 8
        if cycles:
            entry['timeout'] = dict(cycles=cycles, completion=True) if completion else dict(
                cycles=cycles)
        if name in CAPACITY:
            entry['max_outstanding'] = CAPACITY[name]
        entry.update(SHAPED.get(name, {}))
        if name in EXCLUSIVE:
            entry['exclusive'] = 'network'
        if name in OWN_MONITOR:
            entry['exclusive'] = OWN_MONITOR[name]
        if 'exclusive' in entry:
            entry['exclusive_id_flags'] = ['owner']
        entries.append(entry)
    topology = dict(
        request=dict(nodes=[request_node], links=[],
                     entries=[dict(endpoint=s, node='q0') for s in ('s0', 'm0')],
                     exits=[dict(endpoint=t, node='q0') for t in targets(mode)]),
        response=dict(nodes=[response_node], links=[],
                      entries=[dict(endpoint=t, node='p0') for t in targets(mode)],
                      exits=[dict(endpoint=s, node='p0') for s in ('s0', 'm0')]))
    # The packets pipe keeps two request words, so the bursts that follow an
    # expired one are taken while its target is still held.
    source = dict(name='s0', clock_domain='main', protocol='axi4', data_width=32, id_width=2,
                  len_width=8, pipes=dict(packets=dict(forward=True, backward=True)),
                  user_flags=dict(owner=0))
    if mode == 'exclusive':
        source['default_target'] = 'ok'
    return dict(schema_version=10, name='timeouts', address_width=32,
                arbitration=dict(policy='round_robin'), reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')], clock_domains=clocks,
                initiators=[source,
                            dict(name='m0', clock_domain='main', protocol='axi4',
                                 data_width=32, id_width=2, len_width=8, max_outstanding=4,
                                 user_flags=dict(owner=1))],
                targets=entries, connectivity='all',
                # Reads and writes share the source's bursts in flight: two
                # keep a write flowing while a read hangs.
                transport=dict(max_outstanding=2, transport_word_bytes=4,
                               user_flags=['owner']),
                topology=topology,
                output=dict(backend='verilog', language='verilog_2001'))


def bfms(mode):
    clock = "slow" if mode == "clocked" else "main"
    out = [dict(name=s, kind="axi_master", endpoint="initiator:" + s) for s in ("s0", "m0")]
    for i, (name, (kind, _, _, cycles, _)) in enumerate(targets(mode).items()):
        spec = dict(name=name, endpoint=f"target:{name}", clock=clock, salt=i + 1)
        if cycles:
            spec["sideband"] = ("dut", f"target:{name}", "timed_out")
        if kind.startswith("apb"):
            spec.update(kind="apb_slave", dialect=int(kind[-1]))
        elif kind == "ahb_v3":
            spec.update(kind="ahb_slave")
        else:
            spec.update(kind="axi_slave", lite=kind == "axi4_lite")
        out.append(spec)
    return out


def companion(mode, lockstep=False, ungated=False):
    """One configuration, gated (the default) or ungated; lockstep: the tape runs with the test
    port low and high."""
    cfg = nlconfig.ungated(configuration(mode)) if ungated else configuration(mode)
    return dict(label=mode + ("-ungated" if ungated else ""), dut=dict(source="cli", config=cfg),
                bfms=bfms(mode), test_lockstep=lockstep,
                model=dict(routes=nlconfig.routes(cfg), error_codes=True,
                           targets=nlconfig.targets(cfg, error_data_zero=True)),
                clocks=dict(main=10.0, slow=30.0) if mode == "clocked" else {},
                scenario_args=dict(ratio=3 if mode == "clocked" else 1,
                                   exclusive=mode in ("exclusive", "implicit"),
                                   named=mode == "exclusive"))


def timer_stopped(target):
    """A text fault: the timer state behind nl_t_<target>_timed_out never
    sets (its clocked update keeps zero), so the target never fails."""
    def subs(rtl, mapping):
        import re  # noqa: PLC0415
        found = re.findall(rf"^assign nl_t_{target}_timed_out = (nl_internal_v\d+);", rtl, re.M)
        if len(found) != 1:
            return [(r"(?!)", "", 1)]  # no anchor: ANCHOR_LOST
        return [(rf"^(\s*{found[0]} <= )nl_internal_v\d+;", r"\g<1>1'd0;", 1)]
    return subs


MAIN = configuration("graph")
CASE = dict(
    name="target_timeouts",
    contracts=["AXI.TIMEOUT.EXACT", "AXI.ERR.READ_ZERO", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.RESET.CANCEL", "APB.SUBPAGE.DECODE"],
    dut=dict(source="cli", config=MAIN),
    bfms=bfms("graph"),
    model=dict(routes=nlconfig.routes(MAIN), error_codes=True,
               targets=nlconfig.targets(MAIN, error_data_zero=True)),
    configs=[companion("exclusive"), companion("implicit"), companion("graph"),
             companion("clocked", lockstep=True), companion("clocked", ungated=True)],
    scenario_args=dict(ratio=1),
    negative_runs_all=True,
    max_cycles=3000000,
    run_timeout=600,
    faults=[
        dict(id=f"{t}_timer_stopped", op="text", subs=timer_stopped(t),
             expect=(["CONTRACT.TIMEOUT.GAP"] if t == "ax" else
                     ["TAPE.DRAIN_TIMEOUT:" + label, "CONTRACT.TIMEOUT.GAP"]),
             contract="AXI.TIMEOUT.EXACT",
             note=f"the {t} timer state never sets: its target never fails")
        for t, label in (("pa", "PA_HANG_ANSWER"), ("ah", "AH_HANG_ANSWER"),
                         ("lt", "LT_W_HANG_ANSWER"), ("ax", "AX_AW_HANG_ANSWER"))] + [
        dict(id=f"{t}_timer_stopped", op="text", subs=timer_stopped(t),
             expect=["TAPE.WAIT_TIMEOUT:" + label], contract="AXI.TIMEOUT.EXACT",
             note=f"the {t} timer state never sets: none of its bursts in flight expires")
        for t, label in (("mx", "MX_WRITES_EXPIRE"), ("ml", "ML_WRITES_EXPIRE"))] + [
        # Oracle controls: a tape that expects the exclusive writes after expiry
        # the other way round (OKAY with a matching reservation, SLVERR without).
        dict(id=f"{t}_exclusive_swapped", op="scenario", params=dict(swapped=t),
             expect=["SOURCE_B_RESP"], contract="AXI.TIMEOUT.EXACT",
             note=f"the checker tells the {t} monitor rules after expiry from their inverse")
        for t in ("xp", "xl", "xx")] + [
        # The tape of implicit on exclusive: s0's misses after xq's expiry would go
        # unanswered; the named default target answers them.
        dict(id="default_target_ignored", op="scenario", params=dict(ignored=True),
             expect=["CONTRACT.QUIET.R"], contract="AXI.TIMEOUT.EXACT",
             note="the checker tells a named default target from the implicit one")],
)


def scenario(t, seed, ratio=1, exclusive=False, named=False, swapped=None, ignored=False):
    s0, m0 = "s0", "m0"
    state = dict(episode=0)
    active = targets("exclusive" if exclusive else "graph")
    timed = [n for n, spec in active.items() if spec[3]]
    apb = [n for n, s in active.items() if s[0].startswith("apb")]
    ahb = [n for n, s in active.items() if s[0] == "ahb_v3"]
    axi = [n for n, s in active.items() if s[0].startswith("axi4")]

    def defaults():
        for m in (s0, m0):
            t.ready(m, "b", "always")
            t.ready(m, "r", "always")
            t.set(m, "w_gap", 0)
            t.gate(m, "w", True)
            t.release_w(m, 0)
            t.quiet(m, "b", False)
            t.quiet(m, "r", False)
        for n in apb + ahb:
            t.set(n, "wait", 0)
            t.set(n, "slow_mask", 0)
            t.set(n, "quiet", 0)
            cycles = active[n][3]
            if cycles:
                t.set(n, "gap_hi", cycles + (16 if n in ahb else 8))
        for n in ahb:
            t.set(n, "busy_hold", 0)
        for n in axi:
            for ch in ("aw", "w", "ar"):
                t.ready(n, ch, "always")
                t.quiet(n, ch, False)
            t.gate(n, "b", True)
            t.gate(n, "r", True)
            for reg in ("w_stop", "r_stop", "b_lat", "r_lat", "gap_dir"):
                t.set(n, reg, 0)
            # One write and one read at a time.
            t.set(n, "cap_w", 1)
            t.set(n, "cap_r", 1)

    def episode(name):
        state["episode"] += 1
        t.mark("episode", state["episode"])
        defaults()
        t.reset(12)
        t.delay(20 * ratio)
        for n in timed:
            t.expect_now(n, "sideband", "==", 0, "RESET_CLEARS_EXPIRY")

    def drain(label, limit=None):
        t.drain(limit=limit or 20000 * ratio, label=label)

    def slow(n, local, wait=HUNG):
        t.set(n, "slow_mask", 0xFFF)
        t.set(n, "slow_match", local)
        t.set(n, "slow_wait", wait)

    def release(n):
        t.set(n, "slow_wait", 0)

    def expired(n, label, limit=None):
        cycles = active[n][3]
        t.wait(n, "sideband", "==", 1, limit=limit or (cycles + 200) * ratio, label=label)

    def wr(addr, beats=1, **kw):
        return t.write(s0, addr, beats=beats, size=2, id=1, prot=2, **kw)

    def rd(addr, beats=1, **kw):
        return t.read(s0, addr, beats=beats, size=2, id=2, **kw)

    def mwr(addr, beats=1, **kw):
        return t.write(m0, addr, beats=beats, size=2, id=1, prot=2, **kw)

    def mrd(addr, beats=1, **kw):
        return t.read(m0, addr, beats=beats, size=2, id=2, **kw)

    late = dict(target="any", late_access=True)
    none = dict(target="none")

    # ---- APB4 (pa, sub-page window): a read hangs. Sanity on every kind
    # first; after expiry later bursts answer SLVERR without access, the
    # window decode wins (DECERR); other targets are unaffected; the late
    # PREADY is taken and discarded and the failure stays.
    episode("apb4_read_hang")
    rd(0x10000)
    rd(0x14008)
    wr(0x13000, 4)
    rd(0x20010, 2)
    drain("SANITY")
    slow("pa", 0x4)
    rd(0x10004, resp=2, targets=1, **late)
    drain("PA_HANG_ANSWER")
    t.expect_now("pa", "sideband", "==", 1, "PA_TIMED_OUT")
    wr(0x10008, 2, resp=2, **none)
    rd(0x10000, 3, resp=2, **none)
    rd(0x10100, 2, resp=3, **none)
    wr(0x10FFC, resp=3, **none)
    rd(0x14000)
    wr(0x12000)
    drain("PA_LOCAL_ANSWERS")
    release("pa")
    t.wait("pa", "psel", "==", 0, limit=100 * ratio, label="PA_LATE_COMPLETION")
    t.expect_now("pa", "sideband", "==", 1, "PA_LATE_COMPLETION")
    t.set("pa", "quiet", 1)
    rd(0x10000, resp=2, **none)
    drain("PA_STAYS_FAILED")

    episode("apb4_recovered")
    rd(0x10000)
    wr(0x10004)
    drain("PA_RECOVERED")

    # A write burst hangs after two of four words: the third (live at
    # expiry) completes late; the fourth never starts.
    episode("apb4_write_burst_mid_hang")
    w = t.snap("pa", "write")
    slow("pa", 0x28)
    wr(0x10020, 4, resp=2, targets=3, **late)
    drain("PA_WRITE_HANG_ANSWER")
    t.expect_now("pa", "write", "==", 2, "PA_TWO_WORDS_WRITTEN", since=w)
    t.expect_now("pa", "psel", "==", 1, "PA_TWO_WORDS_WRITTEN")
    release("pa")
    t.wait("pa", "write", "==", 3, since=w, limit=100 * ratio, label="PA_LATE_WORD_ONLY")
    t.wait("pa", "psel", "==", 0, limit=100 * ratio, label="PA_LATE_WORD_ONLY")
    t.set("pa", "quiet", 1)
    t.delay(10 * ratio)

    # APB3 read burst: the first beat returns data, the rest SLVERR with
    # zero; after the late PREADY the packet's last two words still reach
    # the target.
    episode("apb3_read_burst_mid_hang")
    r = t.snap("pb", "read")
    slow("pb", 0x44)
    rd(0x11040, 4, beat_resps=[0, 2, 2, 2], targets=4, **late)
    wr(0x11000, resp=2, **none)
    drain("PB_HANG_ANSWER")
    release("pb")
    t.wait("pb", "read", "==", 4, since=r, limit=100 * ratio, label="PB_HELD_READ_FINISHES")
    t.wait("pb", "psel", "==", 0, limit=100 * ratio, label="PB_HELD_READ_FINISHES")
    t.set("pb", "quiet", 1)
    t.delay(10 * ratio)

    # AXI4-Lite: W never accepted, AW was; the late B is taken; no AR
    # after expiry.
    episode("lite_w_hang")
    a, w, ar = t.snap("lt", "aw"), t.snap("lt", "w"), t.snap("lt", "ar")
    t.ready("lt", "w", "never")
    wr(0x12010, resp=2, targets=1, **late)
    drain("LT_W_HANG_ANSWER")
    t.expect_now("lt", "aw", "==", 1, "LT_AW_ONLY", since=a)
    t.expect_now("lt", "w", "==", 0, "LT_AW_ONLY", since=w)
    t.quiet("lt", "ar", True)
    t.ready("lt", "w", "always")
    b = t.snap("lt", "b")
    t.wait("lt", "b", "==", 1, since=b, limit=100 * ratio, label="LT_LATE_B_TAKEN")
    t.quiet("lt", "aw", True)
    rd(0x12000, resp=2, **none)
    drain("LT_NO_AR_AFTER_EXPIRY")
    t.expect_now("lt", "ar", "==", 0, "LT_NO_AR_AFTER_EXPIRY", since=ar)

    episode("lite_aw_hang")
    b = t.snap("lt", "b")
    t.ready("lt", "aw", "never")
    wr(0x12020, 2, resp=2, **late)
    drain("LT_AW_HANG_ANSWER")
    t.ready("lt", "aw", "always")
    t.wait("lt", "b", "==", 1, since=b, limit=100 * ratio, label="LT_AW_LATE")

    # The late R is discarded and the packet's third word is still read.
    episode("lite_r_hang")
    r = t.snap("lt", "rlast")
    t.set("lt", "r_stop", 2)
    rd(0x12030, 3, beat_resps=[0, 2, 2], targets=3, **late)
    drain("LT_R_HANG_ANSWER")
    t.set("lt", "r_stop", 0)
    t.wait("lt", "rlast", "==", 3, since=r, limit=100 * ratio, label="LT_HELD_READ_FINISHES")

    episode("lite_b_hang")
    b = t.snap("lt", "b")
    t.gate("lt", "b", False)
    wr(0x12040, resp=2, targets=1, **late)
    drain("LT_B_HANG_ANSWER")
    t.gate("lt", "b", True)
    t.wait("lt", "b", "==", 1, since=b, limit=100 * ratio, label="LT_LATE_B")

    # A write word without strobes is still a transaction (WSTRB zero): its
    # B hangs, the timeout answers SLVERR, and a later zero-strobe word is
    # answered SLVERR without access.
    episode("lite_last_zero")
    a, b = t.snap("lt", "aw"), t.snap("lt", "b")
    t.gate("lt", "b", False)
    wr(0x12060, strobe="none", resp=2, targets=1, **late)
    drain("LT_ZERO_ANSWER")
    t.expect_now("lt", "aw", "==", 1, "LT_ZERO_SENT_AND_TIMED_OUT", since=a)
    t.expect_now("lt", "sideband", "==", 1, "LT_ZERO_SENT_AND_TIMED_OUT")
    wr(0x12064, strobe="none", resp=2, **none)
    drain("LT_ZERO_LOCAL")
    t.expect_now("lt", "aw", "==", 1, "LT_ZERO_NO_ACCESS_AFTER_EXPIRY", since=a)
    t.gate("lt", "b", True)
    t.wait("lt", "b", "==", 1, since=b, limit=100 * ratio, label="LT_ZERO_CLEANUP")

    # The first word's B hangs; the pipelined outlet may already offer the
    # zero-strobe last word, which waits behind the held B (one write at a
    # time), is answered SLVERR and may complete late without strobes.
    episode("lite_last_zero_buffered")
    a = t.snap("lt", "aw")
    t.gate("lt", "b", False)
    t.strobes(s0, [0xF, 0x0])
    wr(0x12070, 2, strobe="table", resp=2, **late)
    drain("LT_ZERO_BUFFERED_ANSWER")
    t.expect_now("lt", "aw", "==", 1, "LT_ZERO_BUFFERED_NOT_SENT", since=a)
    t.gate("lt", "b", True)
    t.delay(40 * ratio)

    episode("lite_ar_hang")
    r = t.snap("lt", "rlast")
    t.ready("lt", "ar", "never")
    rd(0x12050, resp=2, **late)
    drain("LT_AR_HANG_ANSWER")
    t.ready("lt", "ar", "always")
    t.wait("lt", "rlast", "==", 1, since=r, limit=100 * ratio, label="LT_LATE_R")

    # AHB INCR4 write: the second data phase hangs; the third transfer may
    # wait in its address phase. After the late HREADY that transfer
    # completes, the unfinished burst stays in BUSY and nothing else starts.
    episode("ahb_incr4_mid_hang")
    a = t.snap("ah", "addr")
    slow("ah", 0x104)
    wr(0x13100, 4, resp=2, **late)
    drain("AH_HANG_ANSWER")
    t.expect_now("ah", "addr", "==", 2, "AH_TWO_TRANSFERS_ISSUED", since=a)
    release("ah")
    t.wait("ah", "trans", "==", 1, limit=100 * ratio, label="AH_BUSY_HELD")
    t.set("ah", "quiet", 1)
    rd(0x13000, 2, resp=2, **none)
    drain("AH_LOCAL_READ")
    t.expect_now("ah", "trans", "==", 1, "AH_STILL_BUSY")

    # AHB INCR4 read: the second data phase hangs while the third transfer
    # waits in its address phase; the rest answers SLVERR with zero data;
    # after the late HREADY the burst runs to its end at the target and the
    # bus goes IDLE.
    episode("ahb_incr4_read_mid_hang")
    a, d = t.snap("ah", "addr"), t.snap("ah", "data")
    slow("ah", 0x104)
    rd(0x13100, 4, beat_resps=[0, 2, 2, 2], targets=4, **late)
    expired("ah", "AH_READ_EXPIRES")
    t.expect_now("ah", "addr", "==", 2, "AH_READ_TWO_TRANSFERS_ISSUED", since=a)
    t.expect_now("ah", "trans", "==", 3, "AH_READ_ADDRESS_PHASE_AT_EXPIRY")
    drain("AH_READ_HANG_ANSWER")
    release("ah")
    t.wait("ah", "data", "==", 4, since=d, limit=100 * ratio, label="AH_READ_BURST_FINISHES")
    t.wait("ah", "trans", "==", 0, limit=100 * ratio, label="AH_READ_BURST_FINISHES")
    t.set("ah", "quiet", 1)
    wr(0x13000, resp=2, **none)
    rd(0x13000, 2, resp=2, **none)
    drain("AH_READ_NO_ACCESS_AFTER_EXPIRY")

    episode("ahb_read_recovered")
    rd(0x13100, 4, targets=4)
    drain("AH_READ_RECOVERED")
    t.wait("ah", "trans", "==", 0, limit=100 * ratio, label="AH_READ_RECOVERED")

    # A target that holds HREADY low while an open burst shows BUSY (gaps in
    # the write data): the timer still expires and the outlet answers; once
    # HREADY rises the burst stays in BUSY.
    episode("ahb_busy_hready_low")
    a, d = t.snap("ah", "addr"), t.snap("ah", "data")
    t.set("ah", "busy_hold", 1)
    t.set("ah", "busy_from", 0)
    t.set(s0, "w_gap", 8 * ratio)
    wr(0x13300, 4, resp=2, **late)
    drain("AH_BUSY_ANSWER")
    t.expect_now("ah", "sideband", "==", 1, "AH_BUSY_EXPIRY")
    t.expect_now("ah", "addr", "==", 1, "AH_BUSY_EXPIRY", since=a)
    t.expect_now("ah", "data", "==", 1, "AH_BUSY_EXPIRY", since=d)
    t.set(s0, "w_gap", 0)
    rd(0x13000, resp=2, **none)
    drain("AH_BUSY_LOCAL_READ")
    t.set("ah", "busy_hold", 0)
    t.wait("ah", "trans", "==", 1, limit=100 * ratio, label="AH_BUSY_KEPT")
    t.set("ah", "quiet", 1)
    t.delay(10 * ratio)
    t.expect_now("ah", "trans", "==", 1, "AH_BUSY_KEPT")

    # An INCR4 write whose last word has no strobes ends after three
    # transfers while the bus shows BUSY with HREADY low (the source holds
    # the zero-strobe tail until then); the next write waits past the
    # timeout and expires; the late HREADY leaves the burst in BUSY.
    episode("ahb_ended_busy_hready_low")
    a, d = t.snap("ah", "addr"), t.snap("ah", "data")
    t.set("ah", "busy_hold", 1)
    t.set("ah", "busy_from", 3)
    t.strobes(s0, [0xF, 0xF, 0xF, 0x0])
    t.release_w(s0, after_r=1, from_beat=3)
    wr(0x13300, 4, strobe="table", resp=0, targets=3)
    t.wait("ah", "data", "==", 3, since=d, limit=200 * ratio, label="AH_ENDED_BUSY_REACHED")
    t.wait("ah", "trans", "==", 1, limit=100 * ratio, label="AH_ENDED_BUSY_REACHED")
    t.expect_now("ah", "sideband", "==", 0, "AH_ENDED_BUSY_REACHED_BEFORE_TAIL")
    t.release_w(s0, 0)
    drain("AH_ENDED_BUSY_ANSWER")
    t.expect_now("ah", "sideband", "==", 0, "AH_ENDED_BUSY_WAITS")
    t.expect_now("ah", "trans", "==", 1, "AH_ENDED_BUSY_WAITS")
    t.expect_now("ah", "addr", "==", 3, "AH_ENDED_BUSY_WAITS", since=a)
    wr(0x13400, resp=2, **none)
    drain("AH_ENDED_EXPIRY")
    t.expect_now("ah", "sideband", "==", 1, "AH_ENDED_EXPIRY")
    t.set("ah", "busy_hold", 0)
    t.set("ah", "quiet", 1)
    t.delay(20 * ratio)
    t.expect_now("ah", "trans", "==", 1, "AH_ENDED_BUSY_KEPT")
    t.expect_now("ah", "data", "==", 3, "AH_ENDED_BUSY_KEPT", since=d)

    episode("ahb_single_read_hang")
    d = t.snap("ah", "data")
    slow("ah", 0x200)
    rd(0x13200, resp=2, targets=1, **late)
    drain("AH_SINGLE_ANSWER")
    release("ah")
    t.wait("ah", "data", "==", 1, since=d, limit=100 * ratio, label="AH_IDLE_AFTER_SINGLE")
    t.wait("ah", "trans", "==", 0, limit=100 * ratio, label="AH_IDLE_AFTER_SINGLE")
    t.set("ah", "quiet", 1)
    t.delay(10 * ratio)

    # Without undefined-length INCR a six-word packet is INCR4 then two
    # SINGLE transfers: wherever the timeout falls, the held read packet
    # runs to its end at the target after the late HREADY and the bus shows
    # IDLE; the held write finishes the transfers it issued.
    for label, local, resps in (("ahb_block_end_read_hang", 0x10C, [0, 0, 0, 2, 2, 2]),
                                ("ahb_block_third_read_hang", 0x108, [0, 0, 2, 2, 2, 2]),
                                ("ahb_block_mid_read_hang", 0x104, [0, 2, 2, 2, 2, 2])):
        episode(label)
        a, d = t.snap("ab", "addr"), t.snap("ab", "data")
        slow("ab", local)
        rd(0x16100, 6, beat_resps=resps, targets=6, **late)
        expired("ab", "AB_READ_EXPIRES")
        issued = resps.index(2) + 1
        t.expect_now("ab", "addr", "==", issued, "AB_BLOCK_ISSUED", since=a)
        if local == 0x108:
            t.expect_now("ab", "trans", "==", 3, "AB_ADDRESS_PHASE_AT_EXPIRY")
        drain("AB_READ_HANG_ANSWER")
        release("ab")
        t.wait("ab", "data", "==", 6, since=d, limit=100 * ratio, label="AB_IDLE_AFTER_BLOCK")
        t.wait("ab", "trans", "==", 0, limit=100 * ratio, label="AB_IDLE_AFTER_BLOCK")
        t.set("ab", "quiet", 1)
        t.delay(10 * ratio)
    episode("ahb_block_end_write_hang")
    a = t.snap("ab", "addr")
    slow("ab", 0x10C)
    wr(0x16100, 6, resp=2, **late)
    drain("AB_WRITE_HANG_ANSWER")
    t.expect_now("ab", "addr", "==", 4, "AB_WRITE_BLOCK_ISSUED", since=a)
    release("ab")
    t.wait("ab", "trans", "==", 0, limit=100 * ratio, label="AB_WRITE_IDLE_AFTER_BLOCK")
    t.set("ab", "quiet", 1)
    t.delay(10 * ratio)

    # FIXED requests retain offered channels across expiry, in either write
    # handshake order and while AR waits. Reset then clears the failed target.
    for channel in ("aw", "w", "ar"):
        episode("axi4_fixed_" + channel + "_hang")
        before = t.snap("ax", channel)
        t.ready("ax", channel, "never")
        t.set("ax", "gap_dir", 2 if channel == "ar" else 1)
        t.set("ax", "gap_lo", 0)
        t.set("ax", "gap_hi", active["ax"][3] + 8)
        if channel == "ar":
            rd(0x20100, 4, burst="fixed", resp=2, targets=1, **late)
        else:
            wr(0x20100, 4, burst="fixed", resp=2, targets=1, **late)
        drain("AX_FIXED_TIMEOUT")
        t.expect_now("ax", "sideband", "==", 1, "AX_FIXED_EXPIRED")
        t.expect_now("ax", channel + "valid", "==", 1, "AX_FIXED_HELD")
        t.delay(32 * ratio)
        t.ready("ax", channel, "always")
        t.wait("ax", channel, ">=", 1, since=before, limit=100 * ratio,
               label="AX_FIXED_LATE_HANDSHAKE")

    # AXI4: AW never accepted though all W beats were; the late AW is
    # taken; nothing new starts after expiry.
    episode("axi4_aw_hang")
    a, w = t.snap("ax", "aw"), t.snap("ax", "w")
    t.ready("ax", "aw", "never")
    wr(0x20100, 4, resp=2, targets=1, **late)
    drain("AX_AW_HANG_ANSWER")
    t.expect_now("ax", "w", "==", 4, "AX_W_SENT_AW_HELD", since=w)
    t.expect_now("ax", "aw", "==", 0, "AX_W_SENT_AW_HELD", since=a)
    t.quiet("ax", "ar", True)
    t.ready("ax", "aw", "always")
    t.wait("ax", "aw", "==", 1, since=a, limit=100 * ratio, label="AX_LATE_AW")
    t.quiet("ax", "aw", True)
    t.quiet("ax", "w", True)
    wr(0x20000, 2, resp=2, **none)
    rd(0x20000, 2, resp=2, **none)
    drain("AX_NO_NEW_AFTER_EXPIRY")

    # W stops after two of four beats; the beat already offered is taken
    # late and no further W is offered.
    episode("axi4_w_mid_hang")
    w = t.snap("ax", "w")
    t.set("ax", "w_stop", 3)
    wr(0x20200, 4, resp=2, **late)
    drain("AX_W_HANG_ANSWER")
    t.expect_now("ax", "w", "==", 2, "AX_TWO_BEATS", since=w)
    t.set("ax", "w_stop", 0)
    t.wait("ax", "w", "==", 3, since=w, limit=100 * ratio, label="AX_ONLY_HELD_BEAT")
    t.quiet("ax", "w", True)
    t.delay(20 * ratio)

    episode("axi4_b_hang")
    b = t.snap("ax", "b")
    t.gate("ax", "b", False)
    wr(0x20300, 2, resp=2, targets=1, **late)
    drain("AX_B_HANG_ANSWER")
    t.gate("ax", "b", True)
    t.wait("ax", "b", "==", 1, since=b, limit=100 * ratio, label="AX_LATE_B_TAKEN")

    episode("axi4_r_mid_hang")
    r = t.snap("ax", "rlast")
    t.set("ax", "r_stop", 3)
    rd(0x20400, 4, beat_resps=[0, 0, 2, 2], targets=1, **late)
    drain("AX_R_HANG_ANSWER")
    t.set("ax", "r_stop", 0)
    t.wait("ax", "rlast", "==", 1, since=r, limit=100 * ratio, label="AX_LATE_R_TAKEN")

    episode("axi4_ar_hang")
    r = t.snap("ax", "rlast")
    t.ready("ax", "ar", "never")
    rd(0x20500, 2, resp=2, **late)
    drain("AX_AR_HANG_ANSWER")
    t.ready("ax", "ar", "always")
    t.wait("ax", "rlast", "==", 1, since=r, limit=100 * ratio, label="AX_LATE_R")

    # One watchdog per target and one burst in flight at it: the hung read
    # holds the target, the writes wait behind it, no response restarts the
    # count, and the target expires exactly one window after the read's last
    # progress (the status edge's gap); the waiting writes then answer
    # SLVERR without reaching the target.
    episode("axi4_read_hang_writes_blocked")
    wr(0x20700, 2)
    wr(0x20700, 2)
    drain("AX_WRITES_BEFORE")
    ar = t.snap("ax", "ar")
    t.set("ax", "gap_dir", 2)
    t.set("ax", "gap_lo", 1024 - 8)
    t.set("ax", "gap_hi", 1024 + 8)
    t.gate("ax", "r", False)
    rd(0x20600, resp=2, **late)
    t.wait("ax", "ar", "==", 1, since=ar, limit=100 * ratio, label="AX_READ_HOLDS_TARGET")
    t.quiet("ax", "aw", True)
    t.quiet("ax", "w", True)
    for _ in range(3):
        wr(0x20700, 2, resp=2, **none)
    expired("ax", "AX_BLOCKED_WRITES_DO_NOT_RENEW")
    drain("AX_READ_EXPIRES_BLOCKING_WRITES")
    t.set("ax", "gap_dir", 0)
    wr(0x20700, 2, resp=2, **none)
    drain("AX_AFTER_EXPIRY")

    # The largest timeout: a wait well within it passes, a longer one fails.
    episode("apb4_large_timeout")
    t.set("lg", "wait", 4080)
    rd(0x15000)
    wr(0x15004)
    drain("LG_NOT_EARLY")
    t.expect_now("lg", "sideband", "==", 0, "LG_NOT_EARLY")
    t.set("lg", "wait", 0)
    slow("lg", 0x8)
    rd(0x15008, resp=2, targets=1, **late)
    drain("LG_EXPIRED")
    t.expect_now("lg", "sideband", "==", 1, "LG_EXPIRED")
    wr(0x15008, resp=2, **none)
    drain("LG_NO_ACCESS_AFTER_EXPIRY")
    release("lg")
    t.wait("lg", "psel", "==", 0, limit=100 * ratio, label="LG_LATE")
    t.set("lg", "quiet", 1)

    # The ends of the range this bench can reach: the watchdog has as many bits as the count needs.
    for name, base, margin in (("tn", 0x1B000, 4), ("hg", 0x1C000, 8160)):
        episode(f"apb4_timeout_{name}")
        t.set(name, "wait", margin)
        rd(base)
        wr(base + 4)
        drain(f"{name.upper()}_NOT_EARLY")
        t.expect_now(name, "sideband", "==", 0, f"{name.upper()}_NOT_EARLY")
        t.set(name, "wait", 0)
        slow(name, 0x8)
        rd(base + 8, resp=2, targets=1, **late)
        drain(f"{name.upper()}_EXPIRED")
        t.expect_now(name, "sideband", "==", 1, f"{name.upper()}_EXPIRED")
        release(name)
        t.wait(name, "psel", "==", 0, limit=100 * ratio, label=f"{name.upper()}_LATE")
        t.set(name, "quiet", 1)

    # No expiry: every response within the timeout, writes completing
    # within it, and response backpressure far beyond it at the source.
    episode("no_expiry")
    t.set("pa", "wait", 1000)
    t.set("pb", "wait", 2000)
    t.set("ah", "wait", 1000)
    t.set("lt", "b_lat", 1000)
    t.set("lt", "r_lat", 1000)
    t.set("ax", "r_lat", 1000)
    rd(0x10000, 4)
    rd(0x11000, 3)
    rd(0x13000, 4)
    rd(0x12000, 2)
    rd(0x20000, 4)
    drain("NO_EXPIRY_READS", 60000 * ratio)
    t.set("pa", "wait", 200)
    t.set("pb", "wait", 400)
    t.set("ah", "wait", 200)
    t.set("lt", "b_lat", 400)
    t.set("lt", "r_lat", 400)
    t.set("ax", "r_lat", 0)
    t.set("ax", "b_lat", 900)
    wr(0x10000, 4)
    wr(0x11000, 3)
    wr(0x13000, 4)
    wr(0x12000, 2)
    wr(0x20000, 4)
    drain("NO_EXPIRY_WRITES", 60000 * ratio)
    for n, reg in (("pa", "wait"), ("pb", "wait"), ("ah", "wait"), ("lt", "b_lat"),
                   ("lt", "r_lat"), ("ax", "b_lat")):
        t.set(n, reg, 0)
    t.ready(s0, "r", "delay", 1100)
    rd(0x20000, 4)
    rd(0x10000, 3)
    rd(0x13000, 4)
    rd(0x12000, 2)
    drain("NO_EXPIRY_BACKPRESSURE", 60000 * ratio)
    t.ready(s0, "r", "always")
    t.gate(s0, "w", False)
    wr(0x12000)
    t.delay(1100)
    t.gate(s0, "w", True)
    drain("NO_EXPIRY_SLOW_LITE_WORD")
    for n in ("pa", "pb", "ah", "lt", "ax"):
        t.expect_now(n, "sideband", "==", 0, "NO_EXPIRY")

    # Write data that the source sends more slowly than the timeout fails
    # the target although the target never stalls.
    episode("slow_write_data")
    t.set(s0, "w_gap", 1100)
    wr(0x20000, 4, resp=2, **late)
    wr(0x10000, 4, resp=2, **late)
    wr(0x13000, 4, resp=2, **late)
    drain("SLOW_WRITE_DATA_EXPIRES", 60000 * ratio)
    for n in ("ax", "pa", "ah"):
        t.expect_now(n, "sideband", "==", 1, "SLOW_WRITE_DATA_EXPIRES")
    t.set(s0, "w_gap", 0)

    # ---- Several bursts in flight (mx, mq: four AXI4 bursts; ml: four
    # AXI4-Lite packets) share the target's one watchdog: every response
    # restarts it, and at expiry every burst still in flight answers SLVERR
    # (read beats delivered before stay), late B and R are taken and
    # discarded, and later bursts answer SLVERR without access.
    episode("multi_writes_hang")
    b = t.snap("mx", "b")
    t.set("mx", "cap_w", 4)
    t.gate("mx", "b", False)
    for k in range(4):
        mwr(0x26000 + 0x40 * k, 2, resp=2, targets=1, **late)
    expired("mx", "MX_WRITES_EXPIRE")
    t.expect_now("mx", "peak_w", "==", 4, "MX_FOUR_WRITES_IN_FLIGHT")
    drain("MX_WRITES_ANSWER")
    t.gate("mx", "b", True)
    t.wait("mx", "b", "==", 4, since=b, limit=100 * ratio, label="MX_LATE_B_TAKEN")
    for ch in ("aw", "w", "ar"):
        t.quiet("mx", ch, True)
    mwr(0x26000, 2, resp=2, **none)
    mrd(0x26000, 2, resp=2, **none)
    drain("MX_NO_ACCESS_AFTER_EXPIRY")

    # The target takes one write at a time and holds its B: the second
    # write's AW and W beat wait at expiry and are taken late, after the
    # first B; the writes behind them never reach the target.
    episode("multi_aw_hang")
    a, b = t.snap("mx", "aw"), t.snap("mx", "b")
    t.gate("mx", "b", False)
    mwr(0x26000, resp=2, targets=1, **late)
    mwr(0x26040, resp=2, **late)
    mwr(0x26080, resp=2, **none)
    mwr(0x260C0, resp=2, **none)
    expired("mx", "MX_AW_EXPIRE")
    t.expect_now("mx", "aw", "==", 1, "MX_ONE_AW_TAKEN", since=a)
    drain("MX_AW_ANSWER")
    t.gate("mx", "b", True)
    t.wait("mx", "b", "==", 2, since=b, limit=100 * ratio, label="MX_HELD_AW_LATE")
    t.quiet("mx", "aw", True)
    t.quiet("mx", "w", True)
    t.delay(20 * ratio)

    # The first read delivers two beats before R stops; the three reads
    # behind it answer SLVERR on every beat.
    episode("multi_reads_hang")
    r = t.snap("mx", "rlast")
    t.set("mx", "cap_r", 4)
    t.set("mx", "r_stop", 3)
    mrd(0x26100, 4, beat_resps=[0, 0, 2, 2], targets=1, **late)
    for k in range(1, 4):
        mrd(0x26100 + 0x40 * k, 4, resp=2, targets=1, **late)
    expired("mx", "MX_READS_EXPIRE")
    t.expect_now("mx", "peak_r", "==", 4, "MX_FOUR_READS_IN_FLIGHT")
    drain("MX_READS_ANSWER")
    t.set("mx", "r_stop", 0)
    t.wait("mx", "rlast", "==", 4, since=r, limit=200 * ratio, label="MX_LATE_R_TAKEN")

    # Reads and writes share the pool and the watchdog.
    episode("multi_mixed_hang")
    b, r = t.snap("mx", "b"), t.snap("mx", "rlast")
    t.set("mx", "cap_w", 4)
    t.set("mx", "cap_r", 4)
    t.gate("mx", "b", False)
    t.gate("mx", "r", False)
    mwr(0x26200, 1, resp=2, targets=1, **late)
    mrd(0x26300, 2, resp=2, targets=1, **late)
    mwr(0x26240, 4, resp=2, targets=1, **late)
    mrd(0x26340, 1, resp=2, targets=1, **late)
    expired("mx", "MX_MIXED_EXPIRE")
    drain("MX_MIXED_ANSWER")
    t.gate("mx", "b", True)
    t.gate("mx", "r", True)
    t.wait("mx", "b", "==", 2, since=b, limit=100 * ratio, label="MX_MIXED_LATE")
    t.wait("mx", "rlast", "==", 2, since=r, limit=100 * ratio, label="MX_MIXED_LATE")

    # The target serves one read at a time, 700 cycles each: the first answer
    # restarts the count, so the target expires one window after it (the
    # status edge's gap from the next read's AR) although the third read has
    # waited at the outlet since the start. That read's AR, held at expiry,
    # is accepted late.
    episode("multi_restart")
    r = t.snap("mx", "rlast")
    t.set("mx", "r_lat", 700)
    t.set("mx", "gap_dir", 2)
    t.set("mx", "gap_lo", 1024 - 8)
    t.set("mx", "gap_hi", 1024 + 8)
    mrd(0x26400)
    mrd(0x26440, resp=2, **late)
    mrd(0x26480, resp=2, **late)
    t.wait("mx", "rlast", "==", 1, since=r, limit=1000 * ratio, label="MX_FIRST_READ_ANSWERED")
    t.gate("mx", "r", False)
    expired("mx", "MX_EXPIRES_AFTER_LAST_RESPONSE")
    drain("MX_RESTART_ANSWER")
    t.set("mx", "gap_dir", 0)
    t.gate("mx", "r", True)
    t.wait("mx", "rlast", "==", 3, since=r, limit=2000 * ratio, label="MX_RESTART_LATE")

    # No expiry: three reads, then three writes, each served alone in 700
    # cycles, 2100 in all; every answer restarts the count.
    episode("multi_no_expiry")
    t.set("mx", "r_lat", 700)
    t.set("mx", "b_lat", 700)
    for k in range(3):
        mrd(0x26500 + 0x40 * k, 2)
    drain("MX_SLOW_READS", 10000 * ratio)
    for k in range(3):
        mwr(0x26600 + 0x40 * k, 2)
    drain("MX_SLOW_WRITES", 10000 * ratio)
    t.expect_now("mx", "sideband", "==", 0, "MX_NO_EXPIRY")

    # Without completion nothing answers: the write and the read held at
    # expiry, and the bursts after it, which still leave the target's two
    # entries (two from s0, then two 8-beat writes of m0 that no buffer on
    # the way holds whole); the late B and R are discarded.
    episode("multi_report")
    bb, rr = t.snap(m0, "b"), t.snap(m0, "r")
    b, r = t.snap("mq", "b"), t.snap("mq", "rlast")
    t.set("mq", "cap_w", 4)
    t.set("mq", "cap_r", 4)
    t.gate("mq", "b", False)
    t.gate("mq", "r", False)
    mwr(0x27000, 2)
    mrd(0x27100, 2)
    expired("mq", "MQ_EXPIRED")
    for m in (s0, m0):
        t.quiet(m, "b", True)
        t.quiet(m, "r", True)
    for ch in ("aw", "w", "ar"):
        t.quiet("mq", ch, True)
    w, mw = t.snap(s0, "w"), t.snap(m0, "w")
    wr(0x27040)
    wr(0x27080)
    t.wait(s0, "w", "==", 2, since=w, limit=200 * ratio, label="MQ_WRITES_ABSORBED")
    mwr(0x270C0, 8)
    mwr(0x27100, 8)
    t.wait(m0, "w", "==", 16, since=mw, limit=400 * ratio, label="MQ_WRITES_ABSORBED")
    t.gate("mq", "b", True)
    t.gate("mq", "r", True)
    t.wait("mq", "b", "==", 1, since=b, limit=100 * ratio, label="MQ_LATE_DISCARDED")
    t.wait("mq", "rlast", "==", 1, since=r, limit=100 * ratio, label="MQ_LATE_DISCARDED")
    t.delay(50 * ratio)
    t.expect_now(m0, "b", "==", 0, "MQ_NOTHING_ANSWERED", since=bb)
    t.expect_now(m0, "r", "==", 0, "MQ_NOTHING_ANSWERED", since=rr)

    # AXI4-Lite: three write packets on the bus with their B held.
    episode("multi_lite_writes_hang")
    b = t.snap("ml", "b")
    t.set("ml", "cap_w", 4)
    t.gate("ml", "b", False)
    for k in range(3):
        mwr(0x1A000 + 0x10 * k, resp=2, targets=1, **late)
    expired("ml", "ML_WRITES_EXPIRE")
    t.expect_now("ml", "peak_w", "==", 3, "ML_THREE_WRITES_IN_FLIGHT")
    drain("ML_WRITES_ANSWER")
    t.gate("ml", "b", True)
    t.wait("ml", "b", "==", 3, since=b, limit=100 * ratio, label="ML_LATE_B_TAKEN")
    for ch in ("aw", "w", "ar"):
        t.quiet("ml", ch, True)
    mwr(0x1A000, resp=2, **none)
    drain("ML_NO_ACCESS_AFTER_EXPIRY")

    # Two read packets, the second (another AxPROT) held at its second word's
    # AR: after the late R beats the held packet finishes at the target with
    # its own AxPROT, and its data is discarded.
    episode("multi_lite_reads_hang")
    ar, r = t.snap("ml", "ar"), t.snap("ml", "rlast")
    t.set("ml", "cap_r", 3)
    t.gate("ml", "r", False)
    mrd(0x1A100, 2, prot=0, resp=2, targets=2, **late)
    mrd(0x1A200, 3, prot=3, resp=2, targets=3, **late)
    expired("ml", "ML_READS_EXPIRE")
    t.expect_now("ml", "ar", "==", 3, "ML_THREE_WORDS_ISSUED", since=ar)
    drain("ML_READS_ANSWER")
    t.gate("ml", "r", True)
    t.wait("ml", "ar", "==", 5, since=ar, limit=200 * ratio, label="ML_HELD_PACKET_FINISHES")
    t.wait("ml", "rlast", "==", 5, since=r, limit=200 * ratio, label="ML_HELD_PACKET_FINISHES")
    t.quiet("ml", "ar", True)
    t.delay(20 * ratio)

    # Exclusive support at the target (xe): exclusive bursts pass AxLOCK and
    # answer EXOKAY before expiry; held at expiry, or after it, an exclusive
    # burst answers SLVERR like any other, without EXOKAY.
    episode("multi_exclusive_target")
    okay = dict(resp=1, target_lock=1)
    lock = dict(target_lock=1)
    t.set("xe", "lock_ok", 1)
    t.set("xe", "cap_w", 4)
    t.set("xe", "cap_r", 4)
    mrd(0x28000, lock=1, **okay)
    mwr(0x28000, lock=1, **okay)
    drain("XE_EXCLUSIVE_BEFORE")
    b, r = t.snap("xe", "b"), t.snap("xe", "rlast")
    t.gate("xe", "b", False)
    t.gate("xe", "r", False)
    mrd(0x28040, lock=1, resp=2, targets=1, **lock, **late)
    mwr(0x28040, lock=1, resp=2, targets=1, **lock, **late)
    # Another AxID: this write does not wait for the exclusive one (docs/exclusive-access.md#exclusive-access).
    t.write(m0, 0x28080, beats=1, size=2, id=3, prot=2, resp=2, targets=1, **late)
    expired("xe", "XE_EXPIRED")
    drain("XE_EXCLUSIVE_ANSWER")
    t.gate("xe", "b", True)
    t.gate("xe", "r", True)
    t.wait("xe", "b", "==", 2, since=b, limit=100 * ratio, label="XE_LATE_DISCARDED")
    t.wait("xe", "rlast", "==", 1, since=r, limit=100 * ratio, label="XE_LATE_DISCARDED")
    for ch in ("aw", "w", "ar"):
        t.quiet("xe", ch, True)
    mrd(0x28000, lock=1, resp=2, **none)
    mwr(0x28000, lock=1, resp=2, **none)
    drain("XE_NO_ACCESS_AFTER_EXPIRY")

    # Without completion the failure is only reported: requests are still
    # accepted, no response reaches the source for the beats left or for
    # later bursts (quiet until the next reset), and the read packet already
    # held still finishes at the target.
    episode("report_apb4")
    rd(0x17000)
    drain("QA_SANITY")
    r, w = t.snap("qa", "read"), t.snap(s0, "w")
    slow("qa", 0x40)
    rd(0x17040, 3)
    expired("qa", "QA_EXPIRED")
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    wr(0x17080, 2)
    t.wait(s0, "w", "==", 2, since=w, limit=200 * ratio, label="QA_REQUESTS_ACCEPTED")
    t.expect_now("qa", "read", "==", 0, "QA_NOTHING_ANSWERED", since=r)
    release("qa")
    t.wait("qa", "read", "==", 3, since=r, limit=100 * ratio,
           label="QA_HELD_READ_FINISHES_SILENTLY")
    t.wait("qa", "psel", "==", 0, limit=100 * ratio, label="QA_HELD_READ_FINISHES_SILENTLY")
    t.set("qa", "quiet", 1)
    t.delay(20 * ratio)

    episode("report_lite")
    rr, w, a = t.snap(s0, "r"), t.snap(s0, "w"), t.snap("ql", "aw")
    r = t.snap("ql", "rlast")
    t.set("ql", "r_stop", 2)
    rd(0x18030, 3)
    expired("ql", "QL_EXPIRED")
    t.expect_now(s0, "r", "==", 1, "QL_FIRST_BEAT_ONLY", since=rr)
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.quiet("ql", "aw", True)
    t.quiet("ql", "w", True)
    wr(0x18000)
    t.wait(s0, "w", "==", 1, since=w, limit=200 * ratio, label="QL_WRITE_ABSORBED")
    t.set("ql", "r_stop", 0)
    t.wait("ql", "rlast", "==", 3, since=r, limit=100 * ratio,
           label="QL_HELD_READ_FINISHES_SILENTLY")
    t.delay(20 * ratio)
    t.expect_now("ql", "aw", "==", 0, "QL_WRITE_ABSORBED", since=a)

    episode("report_ahb")
    rr, w = t.snap(s0, "r"), t.snap(s0, "w")
    a, d = t.snap("qh", "addr"), t.snap("qh", "data")
    slow("qh", 0x104)
    rd(0x19100, 4)
    expired("qh", "QH_EXPIRED")
    t.expect_now(s0, "r", "==", 1, "QH_FIRST_BEAT_ONLY", since=rr)
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    wr(0x19000, 2)
    t.wait(s0, "w", "==", 2, since=w, limit=200 * ratio, label="QH_WRITE_ABSORBED")
    t.expect_now("qh", "addr", "==", 2, "QH_WRITE_ABSORBED", since=a)
    release("qh")
    t.wait("qh", "data", "==", 4, since=d, limit=100 * ratio,
           label="QH_HELD_READ_FINISHES_SILENTLY")
    t.wait("qh", "trans", "==", 0, limit=100 * ratio, label="QH_HELD_READ_FINISHES_SILENTLY")
    t.set("qh", "quiet", 1)
    t.delay(20 * ratio)
    t.expect_now("qh", "addr", "==", 4, "QH_WRITE_ABSORBED", since=a)

    episode("report_axi4")
    rr, w = t.snap(s0, "r"), t.snap(s0, "w")
    r = t.snap("qx", "rlast")
    t.set("qx", "r_stop", 3)
    rd(0x21400, 4)
    expired("qx", "QX_EXPIRED")
    t.expect_now(s0, "r", "==", 2, "QX_TWO_BEATS_ONLY", since=rr)
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.quiet("qx", "aw", True)
    t.quiet("qx", "w", True)
    wr(0x21000, 2)
    t.wait(s0, "w", "==", 2, since=w, limit=200 * ratio, label="QX_WRITE_ABSORBED")
    t.set("qx", "r_stop", 0)
    t.wait("qx", "rlast", "==", 1, since=r, limit=100 * ratio, label="QX_LATE_BEATS_DISCARDED")
    t.delay(20 * ratio)
    if exclusive:
        exclusive_episodes(t, episode, slow, release, expired, ratio, swapped)
        default_episode(t, episode, slow, release, expired, ratio, named and not ignored)
    t.reset(12)
    t.end()


def exclusive_episodes(t, episode, slow, release, expired, ratio, swapped=None):
    """A network monitor keeps its rules after its target's timeout. With
    completion every burst answers without access: an exclusive write without a
    matching reservation OKAY (also once its own write or a normal write cleared
    the reservation), one with a matching reservation (recorded before expiry,
    or by an exclusive read answered SLVERR after it) SLVERR. Without
    completion (xq) neither exclusive write is answered, matching or not, and
    neither reaches the target."""
    s0 = "s0"
    after = (("UNMATCHED_OKAY", True, 0x180, 2, 1, 0),
             ("MATCHED_SLVERR", True, 0x100, 1, 1, 2),
             ("OWN_WRITE_CLEARS", True, 0x100, 1, 1, 0),
             ("EXPIRED_READ_RECORDS", False, 0x140, 1, 1, 2),
             ("NORMAL_WRITE_CLEARS", True, 0x140, 2, 0, 2),
             ("CLEARED_OKAY", True, 0x140, 1, 1, 0),
             ("EXPIRED_READ_RECORDS", False, 0x1C0, 1, 1, 2),
             ("RECORDED_AFTER_EXPIRY_SLVERR", True, 0x1C0, 1, 1, 2))
    for n, base in (("xp", 0x22000), ("xl", 0x25000), ("xx", 0x24000)):
        episode(n + "_exclusive_expired")
        t.read(s0, base + 0x100, size=2, id=1, lock=1, resp=1)
        t.drain(limit=20000 * ratio, label=n.upper() + "_RESERVED")
        if n == "xp":
            slow(n, 0x40)
        else:
            t.gate(n, "r", False)
        t.read(s0, base + 0x40, size=2, id=2, resp=2, target="any", late_access=True)
        expired(n, n.upper() + "_EXCLUSIVE_EXPIRY")
        t.drain(limit=20000 * ratio, label=n.upper() + "_EXCLUSIVE_HANG_ANSWER")
        for label, write, offset, ident, lock, resp in after:
            if swapped == n and write and lock:
                resp = 2 - resp
            if write:
                t.write(s0, base + offset, size=2, id=ident, lock=lock, prot=2, resp=resp,
                        target="none")
            else:
                t.read(s0, base + offset, size=2, id=ident, lock=lock, resp=resp, target="none")
            t.drain(limit=20000 * ratio, label=n.upper() + "_" + label)
        if n == "xp":
            release(n)
            t.wait(n, "psel", "==", 0, limit=100 * ratio, label="XP_LATE_COMPLETION")
            t.set(n, "quiet", 1)
        else:
            r = t.snap(n, "rlast")
            t.gate(n, "r", True)
            t.wait(n, "rlast", "==", 1, since=r, limit=100 * ratio, label=n.upper() + "_LATE_R")
            for ch in ("aw", "w", "ar"):
                t.quiet(n, ch, True)
        t.delay(20 * ratio)

    episode("xq_exclusive_report")
    t.read(s0, 0x23000, size=2, id=1, lock=1, resp=1)
    t.drain(limit=20000 * ratio, label="XQ_RESERVED")
    a, d, w = t.snap("xq", "addr"), t.snap("xq", "data"), t.snap(s0, "w")
    slow("xq", 0x10)
    t.read(s0, 0x23010, size=2, id=2)
    expired("xq", "XQ_EXCLUSIVE_EXPIRY")
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    t.write(s0, 0x23020, size=2, id=2, lock=1, prot=2)
    t.write(s0, 0x23000, size=2, id=1, lock=1, prot=2)
    t.wait(s0, "w", "==", 2, since=w, limit=200 * ratio, label="XQ_EXCLUSIVE_WRITES_ABSORBED")
    t.expect_now("xq", "addr", "==", 1, "XQ_EXCLUSIVE_WRITES_ABSORBED", since=a)
    release("xq")
    t.wait("xq", "data", "==", 1, since=d, limit=100 * ratio, label="XQ_HELD_READ_FINISHES")
    t.wait("xq", "trans", "==", 0, limit=100 * ratio, label="XQ_HELD_READ_FINISHES")
    t.set("xq", "quiet", 1)
    t.delay(40 * ratio)


def default_episode(t, episode, slow, release, expired, ratio, named):
    """s0's decode misses after its default target failed. xq, the owner of the
    smallest mapping, only reports its timeout: with it as the default target
    (implicit) a miss is never answered; exclusive names the untimed 'ok', which
    answers DECERR without access to a miss in xq's cut page and to an unmapped
    address."""
    s0 = "s0"
    episode("xq_default_miss")
    d, r = t.snap("xq", "data"), t.snap(s0, "r")
    slow("xq", 0x10)
    t.read(s0, 0x23010, size=2, id=2)
    expired("xq", "XQ_DEFAULT_EXPIRY")
    if named:
        t.read(s0, 0x23100, size=2, id=1, resp=3, target="none")
        t.wait(s0, "r", "==", 1, since=r, limit=200 * ratio, label="NAMED_DEFAULT_ANSWERS")
        t.read(s0, 0x40000000, beats=2, size=2, id=1, resp=3, target="none")
        t.wait(s0, "r", "==", 3, since=r, limit=200 * ratio, label="NAMED_DEFAULT_ANSWERS")
    else:
        t.quiet(s0, "r", True)
        t.read(s0, 0x23100, size=2, id=1)
        t.delay(200 * ratio)
    t.quiet(s0, "b", True)
    t.quiet(s0, "r", True)
    release("xq")
    t.wait("xq", "data", "==", 1, since=d, limit=100 * ratio, label="XQ_DEFAULT_HELD_READ")
    t.wait("xq", "trans", "==", 0, limit=100 * ratio, label="XQ_DEFAULT_HELD_READ")
    t.set("xq", "quiet", 1)
    t.delay(40 * ratio)
