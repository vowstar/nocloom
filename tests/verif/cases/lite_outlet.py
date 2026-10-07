"""Pipelined AXI4-Lite target outlet (fixture path, 32-bit): words of a burst
issue back to back without waiting for B or R, and up to `outstanding` packets
of one direction stay in flight. Rate gates pin the cadence at the target port;
the pre-pipeline engine (one word per B/R round trip) fails them. Each case
number is a marker."""

WIDTH = 32
SB = WIDTH // 8
BASE = 0x1000

PARAMS = globals().get("PARAMS", {})
K = int(PARAMS.get("outstanding", "16"))

CASE = dict(
    name=f"lite_outlet_{K}",
    contracts=["AXI.LITE.WORD_ACCESS", "AXI.RATE.BEATS", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.ERR.READ_ZERO"],
    dut=dict(source="fixture", recipe="lite_target",
             params=dict(width=str(WIDTH), max_outstanding=str(K))),
    nets={"front": [("nl_axi_", "s_")], "target": [("nl_axil_", "t_")]},
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="axi_slave", component="target", endpoint="axil", lite=True,
               salt=3)],
    model=dict(routes={"s0": [("t0", BASE, 0x1000)]},
               targets={"t0": dict(kind="lite", bus_bytes=SB, error_data_zero=True)}),
    max_cycles=60000,
    faults=[
        # The bus reload loses its same-cycle completion bypass: one word per
        # two cycles instead of one per cycle. Function stays correct.
        dict(id="no_reload", component="target", op="replace",
             at={"match": {"op": "BitOr", "ordered": True, "args": [
                 {"op": "BitNot", "args": [{"binding": "bus.open"}]}, "*"]}},
             **{"with": {"operand": 0}},
             expect=["RATE_LOW"],
             contract="AXI.RATE.BEATS", note="one write word per two cycles"),
        # ARVALID one cycle late: the target footprints shift under the DUT's
        # own handshake accounting, breaking attribution.
        dict(id="ar_late", component="target", op="set_driver", port="nl_axil_arvalid",
             **{"with": {"delay": {"self": True}}},
             expect=["TARGET_UNATTRIBUTED", "RATE_LOW"],
             contract="AXI.ROUTE.LOCAL_OFFSET", note="added state: ARVALID one cycle late"),
    ],
)


def scenario(t, seed):
    s0, t0 = "s0", "t0"
    t.set(s0, "w_after_aw", 1)
    t.set(t0, "cap_w", 64)
    t.set(t0, "cap_r", 64)
    t.ready(s0, "b", "always")
    t.ready(s0, "r", "always")
    t.ready(t0, "aw", "always")
    t.ready(t0, "w", "always")
    t.ready(t0, "ar", "always")
    t.gate(t0, "b", True)
    t.gate(t0, "r", True)
    t.reset(8)

    # 1: sanity, one write burst read back, then sparse strobes.
    t.mark("case", 1)
    t.write(s0, BASE + 0x40, beats=4, prot=3, id=2)
    t.read(s0, BASE + 0x40, beats=4, prot=5, id=1)
    t.drain()
    t.write(s0, BASE + 0x80, beats=4, strobe="sparse", prot=3, id=1)
    t.read(s0, BASE + 0x80, beats=4, prot=5, id=2)
    t.drain()

    # 2: back-to-back 16-beat bursts, four writes then four reads; the target
    # port must sustain the burst-interior rate of a pipelined outlet. A Lite
    # port carries one single-beat transaction per word, so the gate is the
    # sustained rate over the whole phase (a per-transaction grouping would be
    # vacuous at one beat each).
    t.mark("phase", 2)
    for b in range(4):
        t.write(s0, BASE + 0x100 + 64 * b, beats=16, prot=3, id=2)
    for b in range(4):
        t.read(s0, BASE + 0x200 + 64 * b, beats=16, prot=5, id=1)
    t.drain(limit=40000, label="PHASE_2")
    t.mark("phase_end", 2)
    # One packet in flight (the default) exposes the B/R round trip between
    # bursts; four or more pipeline it away. The unpipelined engine measures
    # 0.17 here.
    rate_min = 0.75 if K == 1 else 0.9
    t.check(kind="rate", port="t0", ch="w", per="window", min=rate_min, after_mark="phase",
            mark_value=2, until_mark="phase_end", until_value=2, label="t0.w 16-beat phase")
    t.check(kind="rate", port="t0", ch="r", per="window", min=rate_min, after_mark="phase",
            mark_value=2, until_mark="phase_end", until_value=2, label="t0.r 16-beat phase")

    # 3: single-beat packets back to back: the outlet pipelines packets, so
    # with B held at the target the first K writes all apply before the first
    # B leaves; each write still answers exactly one B after its W beat. The
    # fixture front has no port pipes, so W comes with its AW here to let a
    # single write pass straight through every cycle.
    if K >= 16:
        t.mark("phase", 3)
        t.set(s0, "w_after_aw", 0)
        t.gate(t0, "b", False)
        applied = t.snap(t0, "applied")
        for b in range(16):
            t.write(s0, BASE + 0x300 + 4 * b, prot=3, id=1)
        t.wait(t0, "applied", "==", 16, since=applied, limit=20000, label="HELD_WRITES")
        t.gate(t0, "b", True)
        t.drain(limit=20000, label="PHASE_3")
        t.mark("phase_end", 3)
        t.check(kind="rate", port="t0", ch="w", per="window", min=0.9, after_mark="phase",
                mark_value=3, until_mark="phase_end", until_value=3, label="t0.w held-B singles")
        t.set(s0, "w_after_aw", 1)

    # 4: a direction change waits for the earlier direction's responses;
    # interleaved reads and writes stay in arrival order (the model checks
    # every byte and footprint).
    t.mark("case", 4)
    for b in range(4):
        t.read(s0, BASE + 0x400 + 16 * b, beats=4, prot=5, id=1)
        t.write(s0, BASE + 0x400 + 16 * b, beats=4, prot=3, id=2)
    t.read(s0, BASE + 0x400, beats=16, prot=5, id=2)
    t.drain()
    t.end()
