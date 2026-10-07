"""Liveness premises of shared read and write routes on the public schema 10
path (CLI): one AXI4 source s0, AXI4 targets t0 and t1, APB4 target t2.

The baseline keeps every endpoint premise and must pass whether reads and
writes travel on separate routes or share them:
  PC    the traffic of the negative controls with the premises kept: RREADY
        independent of B, a DMA that receives its read before it writes,
        a target whose WREADY does not wait for its reads;
  PC-AW an AW accepted with its first W beat 100 cycles late, and a read
        issued meanwhile: the read completes before any W beat leaves.
Each scenario fault breaks one premise through the stimulus only (op
"scenario", same RTL). Once reads and writes share each network the run
must hang, and the stall report must name the broken premise (LIVE.E1..E3)
together with the wait that timed out. On a network with separate read and
write routes these faults complete and classify as SURVIVED.

Buffer estimate for the 32-beat reads (docs/transactions.md, single domain, no explicit
topology, 32-bit source): while the source holds RREADY low, three source
beats wait in the network for an AXI4 target (two of them in its R stage)
and two for an APB4 target. A run on separate routes shows exactly that:
t0 sends three R beats, t2 completes two APB reads, then both stall. Shared
routes may add one two-entry stage per merge point on the way (at most two
here: the target response face and the source response exit), so at most
3 + 2 * 2 = 7 beats leave t0 and 6 leave t2 while RREADY is low, far below
32. The target is still inside its R burst when its B is ready (the APB4
outlet performs the write between two read transfers), so a response route
held head to tail keeps that B behind it.

Write estimate for E3: on separate routes five W beats of the 16-beat write
leave the source before the gated target stops them (two in the target W
stage, the rest on the path). With at most two more two-entry stages a
shared route holds at most nine, so the packet keeps the request route to
t0 while the target waits.

Endpoint loops (configuration "loop", a second AXI4 source s1): a component
whose target side waits for traffic that its own or another component's
initiator side sends through the network again. A definition cannot declare
such a dependency and validation cannot see it (docs/timeout-and-progress.md#shared-request-and-response-paths).
  loop_self  s1 belongs to the component behind t0: t0 takes W only after it
             served a read, and that read comes from s1 into t0. The write of
             s0 keeps the request output to t0, so the read never arrives.
  loop_cross two such components: t0 waits for a read that s1 sends after its
             write to t1, t1 for a read that s0 sends after its write to t0.
             Each source keeps its request path for its started write, so
             neither read leaves.
The positive phase of "loop" runs the same traffic with the targets taking W
freely and must finish every wait within LOOP_WINDOW; each loop fault must
hang there with the target premise named (LIVE.E3).

LOOP_WINDOW is the hang window: the arbitration wait bound times the switch
hops, plus a margin. An output has at most three candidates (the response
output to a source collects t0, t1 and t2), so a packet waits for at most
n - 1 = 2 others; the longest packet of the loop phases is the 16-beat write
(16 beats plus its header, under 20 cycles at one beat per cycle); a route
crosses one request and one response switch. 2 * 20 * 2 = 80 cycles, plus a
200-cycle margin for the target latency and the response return.
"""

from nlverif import config as nlconfig

CONFIG = dict(
    schema_version=10, name="rw_merge", address_width=32,
    reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")], clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")],
    initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                     id_width=2, len_width=8, narrow_bursts="split_non_modifiable")],
    targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                  len_width=8, attributes="amba", mappings=[dict(base="0x0", size="0x10000")]),
             dict(name="t1", clock_domain="main", protocol="axi4", data_width=32,
                  len_width=8, attributes="amba", mappings=[dict(base="0x10000", size="0x10000")]),
             dict(name="t2", clock_domain="main", protocol="apb4", data_width=32,
                  attributes="amba", mappings=[dict(base="0x20000", size="0x1000")])],
    connectivity="all",
    output=dict(backend="verilog", language="verilog_2001"))

# The same network with a second source s1 for the endpoint loops.
LOOP_CONFIG = dict(CONFIG, initiators=CONFIG["initiators"] + [dict(CONFIG["initiators"][0], name="s1")])

T0, T1, T2 = 0x0, 0x10000, 0x20000
LIMIT = 2000   # cycles a wait of this case may take before it is a hang
LOOP_WINDOW = 2 * 20 * 2 + 200   # arbitration wait bound x hops + margin (see above)
FAULT = dict(op="scenario", contract="AXI.LIVE.PREMISES")

TARGET_BFMS = [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0),
               dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1),
               dict(name="t2", kind="apb_slave", endpoint="target:t2", salt=2)]
TARGET_MODELS = dict(t0=dict(kind="axi", bus_bytes=4), t1=dict(kind="axi", bus_bytes=4),
                     t2=dict(kind="apb", bus_bytes=4))

CASE = dict(
    name="rw_merge",
    contracts=["AXI.LIVE.PREMISES", "APB.TARGET.ACCESS"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0")] + TARGET_BFMS,
    model=dict(routes=nlconfig.routes(CONFIG), targets=TARGET_MODELS),
    max_cycles=40000,
    # The premise faults run on the main configuration, the loop faults on "loop".
    negative_faults_all=True,
    configs=[dict(label=""),
             dict(label="loop", dut=dict(source="cli", config=LOOP_CONFIG),
                  bfms=[dict(name=n, kind="axi_master", endpoint="initiator:" + n)
                        for n in ("s0", "s1")] + TARGET_BFMS,
                  model=dict(routes=nlconfig.routes(LOOP_CONFIG), targets=TARGET_MODELS),
                  scenario_args=dict(loop=True),
                  faults=[dict(id="loop_self", params=dict(variant="loop_self"),
                               expect=["LIVE.E3", "TAPE.WAIT_TIMEOUT:LOOP_SELF"],
                               note="t0 takes W only after a read that s1 sends into t0",
                               **FAULT),
                          dict(id="loop_cross", params=dict(variant="loop_cross"),
                               expect=["LIVE.E3", "TAPE.WAIT_TIMEOUT:LOOP_CROSS"],
                               note="t0 and t1 each wait for a read the other's writer sends",
                               **FAULT)])],
    faults=[
        dict(id="apb_write_data", op="text", contract="APB.TARGET.ACCESS",
             subs=[(r"(assign nl_t_t2_pwdata = )[^;]+;", r"\g<1>32'd0;", 1)],
             expect=["TARGET_WRITE_BYTES"]),
        dict(id="e2_rready_awaits_b", params=dict(variant="e2_t0"),
             expect=["LIVE.E2", "TAPE.WAIT_TIMEOUT:E2_B_BEHIND_R"],
             note="E2: RREADY waits for a B; 32-beat read of t0, then a 1-beat write to t0",
             **FAULT),
        dict(id="e2_apb_rready_awaits_b", params=dict(variant="e2_t2"),
             expect=["LIVE.E2", "TAPE.WAIT_TIMEOUT:E2_B_BEHIND_R"],
             note="E2 on the APB4 target t2: 32-beat read, then a 1-beat write to t2 (on "
                  "separate routes the outlet performs the write between two read transfers)",
             **FAULT),
        dict(id="e1_dma_read_to_write", params=dict(variant="e1"),
             expect=["LIVE.E1", "TAPE.WAIT_TIMEOUT:E1_DMA_READ"],
             note="E1: 4-beat write to t1 with W beats 1..3 released by one R beat; then a "
                  "read of t0",
             **FAULT),
        dict(id="e3_w_awaits_read", params=dict(variant="e3"),
             expect=["LIVE.E3", "TAPE.WAIT_TIMEOUT:E3_W_AWAITS_R"],
             note="E3: t0 takes W only after one of its reads; 16-beat write, then a read",
             **FAULT),
    ],
)


def scenario(t, seed, variant=None, loop=False):
    s0, t0, t1 = "s0", "t0", "t1"
    t.reset(4)
    t.delay(2)

    def snaps():
        return {k: t.snap(s0, k) for k in ("b", "rlast", "w", "aw")}

    if loop:
        return loops(t, variant)
    if variant is None:
        baseline(t, snaps)
    elif variant in ("e2_t0", "e2_t2"):
        # E2 broken: the source takes R only after a B. The read is offered at
        # the source before the write is issued, so its burst holds the
        # response route of its target (and the source response exit).
        m = snaps()
        t.ready(s0, "r", "await_b", 1)
        if variant == "e2_t0":
            t.read(s0, T0 + 0x400, beats=32, id=1)
        else:
            t.read(s0, T2 + 0x400, beats=32, id=1)
        t.wait(s0, "rvalid", "==", 1, limit=LIMIT, label="E2_R_OFFERED")
        if variant == "e2_t0":
            t.write(s0, T0 + 0x800, beats=1, id=2)
        else:
            t.write(s0, T2 + 0x800, beats=1, id=2)
        t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT, label="E2_B_BEHIND_R")
        t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT)
    elif variant == "e1":
        # E1 broken: a DMA writes data it is still reading. Only W beat 0 of
        # the write leaves before an R beat arrives.
        m = snaps()
        t.set(s0, "w_after_aw", 1)
        t.release_w(s0, after_r=1, from_beat=1)
        t.write(s0, T1 + 0x100, beats=4, id=2)
        t.wait(s0, "w", "==", 1, since=m["w"], limit=LIMIT, label="E1_W0")
        t.read(s0, T0 + 0x100, beats=4, id=1)
        t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="E1_DMA_READ")
        t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT)
        t.release_w(s0, after_r=0)
    elif variant == "e3":
        # E3 broken: t0 takes W only after one of its reads completed. The
        # 16-beat write is more than the request path can hold (see the
        # module estimate), so its packet keeps the route to t0.
        m = snaps()
        t.release_w(t0, after_r=1)
        t.set(s0, "w_after_aw", 1)
        t.write(s0, T0 + 0x100, beats=16, id=2)
        t.wait(t0, "wvalid", "==", 1, limit=LIMIT, label="E3_W_AT_TARGET")
        t.read(s0, T0 + 0x800, beats=1, id=1)
        t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="E3_W_AWAITS_R")
        t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT)
        t.release_w(t0, after_r=0)
    else:
        raise ValueError("unknown scenario variant " + str(variant))
    t.drain(label="drain")
    t.end()


def baseline(t, snaps):
    s0, t0, t1 = "s0", "t0", "t1"
    # PC-E2: the same 32-beat read and 1-beat write, RREADY independent of B
    # (a few low cycles so the R burst backs up through the network).
    t.mark("phase", 1)
    m = snaps()
    t.ready(s0, "r", "modulo", 3, 0)
    t.read(s0, T0 + 0x400, beats=32, id=1)
    t.wait(s0, "rvalid", "==", 1, limit=LIMIT)
    t.write(s0, T0 + 0x800, beats=1, id=2)
    t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT, label="PC_E2_B")
    t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="PC_E2_R")
    t.drain(label="DRAIN_1")
    # PC-E2 with the R burst from the APB4 target.
    t.mark("phase", 2)
    m = snaps()
    t.read(s0, T2 + 0x400, beats=32, id=1)
    t.wait(s0, "rvalid", "==", 1, limit=LIMIT)
    t.write(s0, T0 + 0x900, beats=1, id=2)
    t.write(s0, T2 + 0x800, beats=1, id=2)
    t.wait(s0, "b", "==", 2, since=m["b"], limit=LIMIT, label="PC_E2_APB_B")
    t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="PC_E2_APB_R")
    t.ready(s0, "r", "always")
    t.drain(label="DRAIN_2")
    # PC-E1: the DMA receives its read before it writes; the W release gate
    # stays set and is already satisfied when the write starts.
    t.mark("phase", 3)
    m = snaps()
    t.set(s0, "w_after_aw", 1)
    t.release_w(s0, after_r=1, from_beat=1)
    t.read(s0, T0 + 0x100, beats=4, id=1)
    t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="PC_E1_READ")
    t.write(s0, T1 + 0x100, beats=4, id=2)
    t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT, label="PC_E1_WRITE")
    t.release_w(s0, after_r=0)
    t.drain(label="DRAIN_3")
    # PC-E3: WREADY of t0 independent of its reads; a long write, then a read.
    t.mark("phase", 4)
    m = snaps()
    t.ready(t0, "w", "modulo", 4, 0)
    t.write(s0, T0 + 0x100, beats=16, id=2)
    t.wait(t0, "wvalid", "==", 1, limit=LIMIT)
    t.read(s0, T0 + 0x800, beats=1, id=1)
    t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=LIMIT, label="PC_E3_READ")
    t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT, label="PC_E3_WRITE")
    t.ready(t0, "w", "always")
    t.drain(label="DRAIN_4")
    # PC-AW: AW accepted, first W beat 100 cycles later; a read issued after
    # the AW completes while no W beat has left the source. A network whose
    # write packet holds the source exit before its first W beat would hang
    # here.
    t.mark("phase", 5)
    m = snaps()
    t.gate(s0, "w", False)
    t.write(s0, T1 + 0x200, beats=4, id=2)
    t.wait(s0, "aw", "==", 1, since=m["aw"], limit=LIMIT, label="PC_AW_ACCEPTED")
    c = t.snap(s0, "cycle")
    t.read(s0, T0 + 0x200, beats=4, id=1)
    t.wait(s0, "rlast", "==", 1, since=m["rlast"], limit=95, label="PC_AW_READ_FIRST")
    t.expect_now(s0, "w", "==", 0, "PC_AW_READ_FIRST", since=m["w"])
    t.wait(s0, "cycle", ">=", 100, since=c, limit=LIMIT)
    t.gate(s0, "w", True)
    t.wait(s0, "b", "==", 1, since=m["b"], limit=LIMIT, label="PC_AW_WRITE")
    t.drain(label="DRAIN_5")
    # BURST_HOLD: a narrow non-modifiable burst leaves the source one packet
    # per beat, and the source keeps the request path for the whole burst up
    # to its final packet. The APB4 target serves in arrival order, so a
    # packet of the other direction issued once the burst has started reaches
    # it only after every packet of the burst.
    t.mark("phase", 6)
    m = {k: t.snap("t2", k) for k in ("write", "read")}
    b0, r0 = t.snap(s0, "b"), t.snap(s0, "rlast")
    t.write(s0, T2 + 0x200, beats=8, size=0, id=2)
    t.wait("t2", "write", ">=", 1, since=m["write"], limit=LIMIT, label="HOLD_W_STARTED")
    t.read(s0, T2 + 0x300, beats=1, id=1)
    t.wait(s0, "rlast", "==", 1, since=r0, limit=LIMIT, label="HOLD_W_READ")
    t.expect_now("t2", "write", "==", 8, "BURST_HOLD_WRITE", since=m["write"])
    t.wait(s0, "b", "==", 1, since=b0, limit=LIMIT, label="HOLD_W_B")
    t.drain(label="DRAIN_6")
    m = {k: t.snap("t2", k) for k in ("write", "read")}
    b0 = t.snap(s0, "b")
    t.read(s0, T2 + 0x200, beats=8, size=0, id=1)
    t.wait("t2", "read", ">=", 1, since=m["read"], limit=LIMIT, label="HOLD_R_STARTED")
    t.write(s0, T2 + 0x340, beats=1, id=2)
    t.wait(s0, "b", "==", 1, since=b0, limit=LIMIT, label="HOLD_R_WRITE")
    t.expect_now("t2", "read", "==", 8, "BURST_HOLD_READ", since=m["read"])
    t.drain(label="DRAIN_7")
    # Read back what the phases wrote.
    t.mark("phase", 7)
    t.read(s0, T0 + 0x100, beats=16, id=1)
    t.read(s0, T1 + 0x100, beats=4, id=1)
    t.read(s0, T1 + 0x200, beats=4, id=1)
    t.drain(label="DRAIN_8")


def loops(t, variant):
    """The endpoint loops; without a variant the same traffic with the
    targets taking W freely, every wait within LOOP_WINDOW."""
    m = {s: {k: t.snap(s, k) for k in ("b", "rlast")} for s in ("s0", "s1")}
    gated = variant is not None
    for s in ("s0", "s1"):
        t.set(s, "w_after_aw", 1)

    def arrived(target, since, label):
        # The write reached the target: WVALID held against the gate, or a
        # W beat taken without one.
        if gated:
            t.wait(target, "wvalid", "==", 1, limit=LOOP_WINDOW, label=label)
        else:
            t.wait(target, "w", ">=", 1, since=since, limit=LOOP_WINDOW, label=label)
    # loop_self: the component behind t0 reads t0 through its initiator side s1.
    if variant in (None, "loop_self"):
        if gated:
            t.release_w("t0", after_r=1)
        w0 = t.snap("t0", "w")
        t.write("s0", T0 + 0x100, beats=16, id=2)
        arrived("t0", w0, "LOOP_W_AT_TARGET")
        t.read("s1", T0 + 0x800, beats=1, id=1)
        t.wait("s1", "rlast", "==", 1, since=m["s1"]["rlast"], limit=LOOP_WINDOW,
               label="LOOP_SELF")
        t.wait("s0", "b", "==", 1, since=m["s0"]["b"], limit=LOOP_WINDOW, label="LOOP_SELF_B")
        t.release_w("t0", after_r=0)
        t.drain(label="DRAIN_LOOP_SELF")
    m = {s: {k: t.snap(s, k) for k in ("b", "rlast")} for s in ("s0", "s1")}
    # loop_cross: t0 waits for the read of s1, t1 for the read of s0; each read
    # follows its source's write.
    if variant in (None, "loop_cross"):
        if gated:
            t.release_w("t0", after_r=1)
            t.release_w("t1", after_r=1)
        w0, w1 = t.snap("t0", "w"), t.snap("t1", "w")
        t.write("s0", T0 + 0x200, beats=16, id=2)
        t.write("s1", T1 + 0x200, beats=16, id=2)
        arrived("t0", w0, "LOOP_W0_AT_TARGET")
        arrived("t1", w1, "LOOP_W1_AT_TARGET")
        t.read("s0", T1 + 0x800, beats=1, id=1)
        t.read("s1", T0 + 0x900, beats=1, id=1)
        for s in ("s0", "s1"):
            t.wait(s, "rlast", "==", 1, since=m[s]["rlast"], limit=LOOP_WINDOW,
                   label="LOOP_CROSS")
            t.wait(s, "b", "==", 1, since=m[s]["b"], limit=LOOP_WINDOW, label="LOOP_CROSS_B")
        t.release_w("t0", after_r=0)
        t.release_w("t1", after_r=0)
        t.drain(label="DRAIN_LOOP_CROSS")
    if not gated:
        # Read back what the loop phases wrote.
        t.read("s0", T0 + 0x100, beats=16, id=1)
        t.read("s0", T0 + 0x200, beats=16, id=1)
        t.read("s1", T1 + 0x200, beats=16, id=1)
        t.drain(label="DRAIN_LOOP_READBACK")
    t.end()
