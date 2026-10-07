"""Bounded packets at a multi-transaction AXI4 target through the public
CLI: two AXI4 sources (eight bursts each, narrow non-modifiable bursts split
into one-beat packets) and one AXI4 target that keeps max_outstanding
packets of a pool shared by reads and writes.

Judged directly, from the target's handshakes at every cycle (case
oracle): the AW and AR handshakes not yet answered by B or the last R never
exceed the pool and reach it; reads of different target ARIDs overlap
and the last R closes the oldest open read of its ARID; a B overtakes an
older write of the other source (phase 3). The offline checker judges bytes, strobes, responses, read
data, causality and that each 12-beat narrow burst reaches the target as
twelve one-beat packets. The fixture recipe target_bounded builds the CLI
netlist byte for byte and the fault copy."""

from nlverif import config as nlconfig

MODES = {"k2": (2, 2), "k4": (4, 2), "id16": (4, 16), "src4": (8, 2), "src1": (8, 2),
         "ro": (4, 2), "ro1": (1, 2), "wo": (4, 2), "wo1": (1, 2)}
# Phases 11 and 12 (ro1): the beats of each s0 write to the read-only t0.
RO_WRITE_BEATS = {11: 6, 12: 6}
# Shared burst capacity of each source; src4 and src1 show the source pool
# below the target's, src1 with each burst cut into six packets (phase 9).
SOURCE_OUTSTANDING = {"src4": 4, "src1": 1}
# AXI IDs of the scenario's four streams: with 16-bit IDs their low eight
# bits are equal, so identity aliasing in narrow fields shows.
IDS = {2: (0, 1, 2, 3), 16: (0x00FF, 0x80FF, 0xFFFF, 0x40FF)}


def configuration(capacity, id_bits, source_outstanding=8, read_only=(), sub_windows=False,
                  target_read_only=False, write_only=False):
    # Phase 9 (src1): six 16-byte sub-page mappings cut a burst into six
    # packets at their edges.
    mappings = ([dict(base=hex(16 * i), size="0x10") for i in range(6)]
                if sub_windows else [dict(base="0x0", size="0x10000")])
    return dict(
        schema_version=10, name="target_bounded", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")], clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")],
        # Reads and writes share the source capacity: eight lets a source
        # fill the target's pool with either direction.
        initiators=[dict(name=s, protocol="axi4", clock_domain="main", data_width=32,
                         max_outstanding=source_outstanding,
                         narrow_bursts="split_non_modifiable",
                         id_width=id_bits, len_width=8,
                         **({"access": "read_only"} if s in read_only else {}),
                         **({"access": "write_only"} if write_only and s == "s1" else {}))
                    for s in ("s0", "s1")],
        targets=[dict(name="t0", protocol="axi4", clock_domain="main", data_width=32,
                      max_outstanding=capacity, mappings=mappings,
                      len_width=8, attributes="amba",
                      **({"access": "read_only"} if target_read_only else {}),
                      **({"access": "write_only"} if write_only else {}))]
        # Phase 10 (ro): a read-only AXI4-Lite target of four packets beside t0.
        + ([dict(name="t1", protocol="axi4_lite", clock_domain="main", data_width=32,
                 max_outstanding=capacity, access="read_only", attributes="amba",
                 mappings=[dict(base="0x10000", size="0x1000")])] if target_read_only else [])
        # Phase 13 (wo): the write-only mirror.
        + ([dict(name="t1", protocol="axi4_lite", clock_domain="main", data_width=32,
                 max_outstanding=capacity, access="write_only", attributes="amba",
                 mappings=[dict(base="0x10000", size="0x1000")])] if write_only else []),
        transport=dict(response_codes='distinct'), arbitration=dict(policy="round_robin"),
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=True,
                targets=nlconfig.targets(cfg, error_data_zero=True, narrow_single=True))


def companion(label):
    capacity, id_bits = MODES[label]
    source = SOURCE_OUTSTANDING.get(label)
    cfg = configuration(capacity, id_bits, source or 8, ("s1",) if label == "src4" else (),
                        label == "src1", label in ("ro", "ro1"), label in ("wo", "wo1"))
    args = dict(capacity=capacity, id_bits=id_bits)
    extra = {}
    if label in ("ro", "ro1"):
        args["read_only_target"] = True
        if label == "ro1":
            args["read_only_writes"] = True
        extra["bfms"] = CASE_BFMS + [dict(name="t1", kind="axi_slave", endpoint="target:t1",
                                          lite=True, salt=1)]
    if label in ("wo", "wo1"):
        args["write_only_target"] = True
        extra["bfms"] = CASE_BFMS + [dict(name="t1", kind="axi_slave", endpoint="target:t1",
                                          lite=True, salt=1)]
    if source:
        # Transactions one source keeps in flight: its capacity n.
        args["source_pool"] = source
    if label == "src1":
        # Phase 9 counts packets at the target: each burst crosses five
        # sub-page edges and travels as six packets on one pool entry; the
        # target sees at most the splitter's four packets of it in flight.
        args["burst_packets"] = 6
        args["pool_peak"] = 4
    return dict(label=label, dut=dict(source="cli", config=cfg), model=model(cfg),
                fault_recipe=dict(recipe="target_bounded", params=dict(
                    target_outstanding=str(capacity), id_bits=str(id_bits))),
                scenario_args=args, **extra)


CASE_BFMS = [dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
             dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
             dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0)]
MAIN = configuration(4, 2)
CASE = dict(
    name="target_bounded",
    contracts=["AXI.OUTSTANDING.REACH", "AXI.ROUTE.LOCAL_OFFSET", "AXI.MERGE.SEVERITY",
               "AXI.ERR.UNMAPPED_DECERR", "AXI.ERR.READ_ZERO", "AXI.RESET.CANCEL",
               "AXI.W.UNSTROBED_ZERO", "AXI.ID.RETURN"],
    dut=dict(source="cli", config=MAIN),
    fault_recipe=dict(recipe="target_bounded", params=dict(target_outstanding="4", id_bits="2")),
    bfms=CASE_BFMS,
    model=model(MAIN),
    configs=[companion("k4"), companion("k2"), companion("id16"), companion("src4"),
             companion("src1"), companion("ro"), companion("ro1"), companion("wo"),
             companion("wo1")],
    scenario_args=dict(capacity=4, id_bits=2),
    # Every configuration runs its baseline; the faults run on k4.
    negative_runs_all=True,
    max_cycles=60000,
    faults=[
        dict(id="pool_write_unbounded", component="network", op="replace",
             at={"binding": "burst_service/t0/pool.write_ok"}, **{"with": {"const": 1}},
             expect=["ORACLE", "TAPE.EXPECT:P1_TARGET_LIMIT_W"],
             contract="AXI.OUTSTANDING.REACH",
             note="a write head enters whatever the pool holds"),
        dict(id="target_outstanding_5", component="network", op="variant",
             params={"target_outstanding": "5"},
             expect=["ORACLE", "TAPE.EXPECT:P1_TARGET_LIMIT_W"],
             contract="AXI.OUTSTANDING.REACH",
             note="parameter-level variant: target max_outstanding 4 -> 5"),
        dict(id="awid_source_lost", component="network", op="set_driver", port="nl_t_t0_awid",
             **{"with": {"op": "BitAnd", "args": [{"self": True}, {"const": 3}]}},
             expect=["TARGET_UNATTRIBUTED"],
             contract="AXI.ID.RETURN",
             note="the target AWID loses its high bits, so it is not the route sum"),
    ],
)


def scenario(t, seed, capacity=4, id_bits=2, source_pool=None, burst_packets=0,
             pool_peak=None, read_only_target=False, read_only_writes=False,
             write_only_target=False):
    s0, s1, t0 = "s0", "s1", "t0"
    i0, i1, i2, i3 = IDS[id_bits]

    def source_ready(on):
        for s, (bm, rm) in ((s0, (4, 3)), (s1, (5, 4))):
            if on:
                t.ready(s, "b", "modulo", bm, 0)
                t.ready(s, "r", "modulo", rm, 0)
            else:
                t.ready(s, "b", "never")
                t.ready(s, "r", "never")

    def release(b, r):
        t.gate(t0, "b", b)
        t.gate(t0, "r", r)

    t.ready(t0, "aw", "modulo", 5, 0)
    t.ready(t0, "w", "modulo", 4, 0)
    t.ready(t0, "ar", "modulo", 3, 0)
    # Target error rule of the scenario: address bit 8, or low byte 6.
    t.set(t0, "errm0_mask", 0x100)
    t.set(t0, "errm0_match", 0x100)
    t.set(t0, "errm0_code", 2)
    t.set(t0, "errm1_mask", 0xFF)
    t.set(t0, "errm1_match", 0x06)
    t.set(t0, "errm1_code", 2)
    source_ready(True)
    t.reset(4)
    t.delay(2)
    if write_only_target:
        (write_only_target_pool if capacity > 1 else write_only_target_reads)(
            t, capacity, release, (i1, i2, i3))
        t.end()
        return
    if read_only_writes:
        read_only_target_writes(t, release, (i1, i2))
        t.end()
        return
    if read_only_target:
        read_only_target_pool(t, capacity, release, (i1, i2, i3))
        t.end()
        return
    # The waiting burst of phase 9 is a read behind a six-packet write.
    if source_pool and burst_packets:
        transaction_packets(t, source_pool, burst_packets, release, (i1, i2))
        t.end()
        return
    if source_pool:
        source_packets(t, source_pool, release, (i1, i2, i3))
        read_only_pieces(t, source_pool, release, i1)
        t.end()
        return

    # 1: distinct source tags, one AXI ID, two bursts more than the pool:
    # the writes fill the pool while every B is held, the last two wait
    # in the source and the network (a source keeps an AW in its port pipe
    # until the network takes the burst, so it takes no more than that; a W beat
    # passes the source only as it leaves) and enter only as Bs free
    # entries; then the reads do the same while every R is held. The pool
    # bound is judged at every target handshake (oracle); the hold after
    # the pool filled gives an extra entry time to show.
    t.mark("phase", 1)
    release(False, False)
    m = t.snap(t0, "aw")
    for k in range(capacity + 2):
        t.write(s0, 0x800 + k * 256, size=2, id=i1)
    t.wait(t0, "aw", "==", capacity, since=m, label="P1_TARGET_LIMIT_W")
    t.delay(40)
    t.expect_now(t0, "w_pend", "==", capacity, "P1_TARGET_LIMIT_W")
    m = t.snap(t0, "ar")
    t.gate(t0, "b", True)
    for k in range(capacity + 2):
        t.read(s0, 0x800 + k * 256, size=2, id=i1)
    t.wait(t0, "ar", "==", capacity, since=m, label="P1_TARGET_LIMIT_R")
    t.delay(40)
    t.expect_now(t0, "r_pend", "==", capacity, "P1_TARGET_LIMIT_R")
    t.gate(t0, "r", True)
    t.drain(label="P1_DRAIN")
    # 2: one source tag across twelve one-beat packets (a middle packet
    # fails): the burst gets one B and its own RLAST. Each direction fills
    # the pool on its own (the request path keeps a burst's grant to its
    # last packet); the source then holds the responses for 30 cycles.
    t.mark("phase", 2)
    release(False, False)
    m = t.snap(t0, "aw")
    t.write(s0, 0x2000, beats=12, size=1, id=i2, targets=12)
    t.wait(t0, "aw", "==", capacity, since=m, label="P2_SPLIT_CREDITS_W")
    t.expect_now(t0, "w_pend", "==", capacity, "P2_SPLIT_CREDITS_W")
    t.gate(t0, "b", True)
    source_ready(False)
    t.delay(30)
    source_ready(True)
    t.drain(label="P2_DRAIN_W")
    m = t.snap(t0, "ar")
    t.read(s0, 0x4000, beats=12, size=1, id=i2, targets=12)
    t.wait(t0, "ar", "==", capacity, since=m, label="P2_SPLIT_CREDITS_R")
    t.expect_now(t0, "r_pend", "==", capacity, "P2_SPLIT_CREDITS_R")
    t.gate(t0, "r", True)
    source_ready(False)
    t.delay(30)
    source_ready(True)
    t.drain(label="P2_DRAIN_R")
    # 3: the target answers B youngest first: s1's write overtakes s0's
    # (oracle). Reads of two sources and IDs flow and overlap.
    t.mark("phase", 3)
    release(False, True)
    t.set(t0, "b_order", 1)
    m = t.snap(t0, "aw")
    w = t.snap(t0, "wlast")
    t.write(s0, 0x6000, size=2, id=i0)
    t.wait(t0, "aw", "==", 1, since=m, label="P3_FIRST_WRITE")
    # A different target ID from i0's (docs/transactions.md#target-identifiers), so the B may overtake.
    other = i2 if id_bits == 2 else i3
    t.write(s1, 0x6100, size=2, id=other)
    t.read(s0, 0x7000, beats=4, size=1, id=i0, targets=4)
    t.read(s1, 0x7100, beats=4, size=1, id=other, targets=4)
    t.wait(t0, "aw", "==", 2, since=m, label="P3_SECOND_WRITE")
    t.wait(t0, "wlast", "==", 2, since=w, label="P3_SECOND_WRITE")
    t.gate(t0, "b", True)
    t.drain(label="P3_DRAIN")
    t.set(t0, "b_order", 0)
    # 4: decode errors follow the earlier responses of the same source ID.
    t.mark("phase", 4)
    t.write(s0, 0x8000, beats=4, size=1, id=i2, targets=4)
    t.write(s0, 0x10000, size=2, id=i2)
    t.read(s0, 0x9000, beats=4, size=1, id=i2, targets=4)
    t.read(s0, 0x10000, size=2, id=i2)
    t.drain(label="P4_DRAIN")
    # 5: the common reset cancels a nonempty target pipeline (half the pool
    # per direction).
    t.mark("phase", 5)
    release(False, False)
    m, n = t.snap(t0, "aw"), t.snap(t0, "ar")
    t.write(s0, 0xA000, beats=capacity // 2, size=1, id=i1)
    t.read(s0, 0xB000, beats=capacity // 2, size=1, id=i1)
    t.wait(t0, "aw", "==", capacity // 2, since=m, label="P5_RESET_NONEMPTY")
    t.wait(t0, "ar", "==", capacity // 2, since=n, label="P5_RESET_NONEMPTY")
    t.reset(4)
    t.delay(2)
    # 6: every slot and tag is reused after the reset.
    t.mark("phase", 6)
    release(True, True)
    t.write(s0, 0xC000, beats=6, size=1, id=i3, targets=6)
    t.read(s1, 0xD000, beats=6, size=1, id=i2, targets=6)
    t.drain(label="P6_DRAIN")
    t.end()


def source_packets(t, packets, release, ids):
    """7: the source's transactions in flight, reads and writes together,
    stop at `packets` below the target's pool. A write held at t0 keeps one
    entry of s0 while local completions (a read and a write across
    4 KiB, answered SLVERR at the source) pass and a decode miss (DECERR from
    the default target t0, one transaction out and back) waits until t0 is
    empty, so after the held write's B: none may leave an entry taken or free
    one. Then s0 offers four writes and four reads with
    every response held: four writes and packets - 4 reads reach t0 (none at a
    capacity of four), the other reads wait until the Bs free their entries."""
    s0, t0 = "s0", "t0"
    held, other, miss = ids
    t.mark("phase", 7)
    release(False, True)
    m, n = t.snap(t0, "aw"), t.snap(t0, "ar")
    b, r = t.snap(s0, "b"), t.snap(s0, "rlast")
    t.write(s0, 0x100, size=2, id=held)
    t.wait(t0, "aw", "==", 1, since=m, label="P7_HELD_WRITE")
    t.read(s0, 0x0FFC, beats=2, size=2, id=other)
    t.write(s0, 0x1FFC, beats=2, size=2, id=other)
    t.read(s0, 0x10000, size=2, id=miss)
    t.read(s0, 0x200, size=2, id=other)
    t.wait(s0, "b", "==", 1, since=b, label="P7_LOCAL_DONE")
    t.wait(s0, "rlast", "==", 1, since=r, label="P7_LOCAL_DONE")
    # t0 takes the miss only once empty: the held write's B lets it and the read behind it in.
    t.delay(8)
    t.wait(s0, "rlast", "==", 1, since=r, label="P7_MISS_WAITS")
    t.wait(t0, "ar", "==", 0, since=n, label="P7_MISS_WAITS")
    release(True, True)
    t.wait(s0, "b", "==", 2, since=b, label="P7_MISS_DONE")
    t.wait(s0, "rlast", "==", 3, since=r, label="P7_MISS_DONE")
    t.wait(t0, "ar", "==", 1, since=n, label="P7_MISS_DONE")
    release(False, False)
    m, n = t.snap(t0, "aw"), t.snap(t0, "ar")
    for k in range(4):
        t.write(s0, 0x400 + k * 256, size=2, id=held)
    t.wait(t0, "aw", "==", 4, since=m, label="P7_WRITES_IN")
    for k in range(4):
        t.read(s0, 0x800 + k * 256, size=2, id=held)
    t.wait(t0, "ar", "==", packets - 4, since=n, label="P7_SOURCE_POOL")
    t.delay(40)
    t.expect_now(t0, "ar", "==", packets - 4, "P7_SOURCE_POOL", since=n)
    t.expect_now(t0, "w_pend", "==", 4, "P7_SOURCE_POOL")
    t.gate(t0, "b", True)
    t.wait(t0, "ar", "==", 4, since=n, label="P7_POOL_FREED")
    t.gate(t0, "r", True)
    t.drain(label="P7_DRAIN")


def read_only_pieces(t, packets, release, rid):
    """8: the read-only s1 splits a 12-beat narrow non-modifiable read into
    one-beat packets, each a transaction of its own: with every R held,
    `packets` of them reach t0 and the rest wait in the source's pool until R
    frees them."""
    s1, t0 = "s1", "t0"
    t.mark("phase", 8)
    release(True, False)
    n = t.snap(t0, "ar")
    t.read(s1, 0xE000, beats=12, size=1, id=rid, targets=12)
    t.wait(t0, "ar", "==", packets, since=n, label="P8_READ_ONLY_POOL")
    t.delay(40)
    t.expect_now(t0, "ar", "==", packets, "P8_READ_ONLY_POOL", since=n)
    t.gate(t0, "r", True)
    t.drain(label="P8_DRAIN")


def read_only_target_pool(t, capacity, release, ids):
    """10: at a read-only target of several bursts, one source's reads fill
    the pool while every R is held, two more wait in the network, and writes
    of the other source (single and burst) answer SLVERR
    without target access. Then reads of both sources and IDs and writes
    interleave until drained; a read of another source or ID enters only
    once the reads in flight are answered."""
    s0, s1, t0 = "s0", "s1", "t0"
    held, other, third = ids
    t.mark("phase", 10)
    release(True, False)
    n = t.snap(t0, "ar")
    for k in range(capacity + 2):
        t.read(s0, 0x800 + k * 256, size=2, id=held)
    t.wait(t0, "ar", "==", capacity, since=n, label="P10_TARGET_LIMIT_R")
    t.write(s1, 0x900, size=2, id=other)
    t.write(s1, 0x1100, beats=4, size=2, id=third)
    t.delay(40)
    t.expect_now(t0, "ar", "==", capacity, "P10_TARGET_LIMIT_R", since=n)
    t.gate(t0, "r", True)
    t.drain(label="P10_DRAIN_R")
    # The Lite target t1 keeps its packets of one direction the same way.
    t.gate("t1", "r", False)
    n = t.snap("t1", "ar")
    for k in range(capacity + 2):
        t.read(s1, 0x10000 + k * 4, size=2, id=other)
    t.wait("t1", "ar", "==", capacity, since=n, label="P10_LITE_LIMIT_R")
    t.write(s0, 0x10040, beats=2, size=2, id=third)
    t.delay(40)
    t.expect_now("t1", "ar", "==", capacity, "P10_LITE_LIMIT_R", since=n)
    t.gate("t1", "r", True)
    t.drain(label="P10_DRAIN_LITE")
    for k in range(3):
        t.read(s0, 0x3000 + k * 64, beats=4, size=2, id=held)
        t.write(s0, 0x3400 + k * 64, beats=2, size=2, id=other)
        t.read(s1, 0x3800 + k * 64, beats=2, size=2, id=third)
        t.write(s1, 0x3C00 + k * 64, size=2, id=held)
        t.read(s0, 0x10100 + k * 16, beats=4, size=2, id=third)
        t.write(s1, 0x10200 + k * 16, beats=2, size=2, id=other)
    t.drain(label="P10_DRAIN")


def read_only_target_writes(t, release, ids):
    """11, 12: writes of RO_WRITE_BEATS beats from s0 to the read-only t0 of
    one burst, back to back (11, beside a read of s1) and with W beats 12
    cycles apart (12, a read of s0 behind it): every W beat is taken and one
    SLVERR B follows the last one, never earlier (oracle; the checker judges
    the response and the reads)."""
    s0, s1, t0 = "s0", "s1", "t0"
    held, other = ids
    release(True, True)
    t.mark("phase", 11)
    n = t.snap(t0, "ar")
    t.write(s0, 0x500, beats=RO_WRITE_BEATS[11], size=2, id=held)
    t.read(s1, 0x600, beats=4, size=2, id=other)
    t.wait(t0, "ar", "==", 1, since=n, label="P11_READ")
    t.drain(label="P11_DRAIN")
    t.mark("phase", 12)
    t.set(s0, "w_gap", 12)
    t.write(s0, 0x700, beats=RO_WRITE_BEATS[12], size=2, id=held)
    t.read(s0, 0x800, beats=4, size=2, id=held)
    t.drain(label="P12_DRAIN")
    t.set(s0, "w_gap", 0)


def write_only_target_pool(t, capacity, release, ids):
    """13: at a write-only target of several bursts, the writes of s1 (a
    write-only source) fill the pool while every B is held and two more wait
    in the network; reads of s0 (single and burst) answer SLVERR with zero
    data without target access once a pool entry frees. The write-only Lite
    target t1 keeps its packets the same way. Then reads and writes of both
    sources interleave until drained."""
    s0, s1, t0 = "s0", "s1", "t0"
    held, other, third = ids
    t.mark("phase", 13)
    release(False, True)
    n = t.snap(t0, "aw")
    for k in range(capacity + 2):
        t.write(s1, 0x800 + k * 256, size=2, id=held)
    t.wait(t0, "aw", "==", capacity, since=n, label="P13_TARGET_LIMIT_W")
    t.read(s0, 0x900, size=2, id=other)
    t.read(s0, 0x1100, beats=4, size=2, id=third)
    t.delay(40)
    t.expect_now(t0, "aw", "==", capacity, "P13_TARGET_LIMIT_W", since=n)
    t.gate(t0, "b", True)
    t.drain(label="P13_DRAIN_W")
    t.gate("t1", "b", False)
    n = t.snap("t1", "aw")
    for k in range(capacity + 2):
        t.write(s1, 0x10000 + k * 4, size=2, id=other)
    t.wait("t1", "aw", "==", capacity, since=n, label="P13_LITE_LIMIT_W")
    t.read(s0, 0x10040, beats=2, size=2, id=third)
    t.delay(40)
    t.expect_now("t1", "aw", "==", capacity, "P13_LITE_LIMIT_W", since=n)
    t.gate("t1", "b", True)
    t.drain(label="P13_DRAIN_LITE")
    for k in range(3):
        t.read(s0, 0x3000 + k * 64, beats=4, size=2, id=held)
        t.write(s0, 0x3400 + k * 64, beats=2, size=2, id=other)
        t.write(s1, 0x3800 + k * 64, beats=2, size=2, id=third)
        t.read(s0, 0x10100 + k * 16, beats=4, size=2, id=third)
        t.write(s1, 0x10200 + k * 16, beats=2, size=2, id=other)
    t.drain(label="P13_DRAIN")


def write_only_target_reads(t, capacity, release, ids):
    """14: at a write-only target of one burst, multi-beat reads of s0 back to
    back beside a write of s1, then a read behind a write of s0: each read
    answers SLVERR with zero data on every beat, RLAST on the last."""
    s0, s1, t0 = "s0", "s1", "t0"
    held, other, _ = ids
    release(True, True)
    t.mark("phase", 14)
    n = t.snap(t0, "aw")
    t.read(s0, 0x500, beats=6, size=2, id=held)
    t.write(s1, 0x600, beats=4, size=2, id=other)
    t.read(s0, 0x540, beats=3, size=2, id=held)
    t.wait(t0, "aw", "==", 1, since=n, label="P14_WRITE")
    t.drain(label="P14_DRAIN")
    t.write(s0, 0x700, beats=2, size=2, id=held)
    t.read(s0, 0x800, beats=8, size=2, id=held)
    t.drain(label="P14_DRAIN_BEHIND")


def read_only_writes(marks, events):
    """Phases 11 and 12 at s0: each write takes RO_WRITE_BEATS beats, the
    last alone with WLAST, and exactly one B follows that beat."""
    seen, phase, out = {k: ([], []) for k in RO_WRITE_BEATS}, 0, []
    for e in events:
        if e.get("k") == "mk" and marks[e.get("m", 0)] == "phase":
            phase = e.get("v")
        if phase in seen and e.get("p") == "s0" and e.get("k") == "hs" and e.get("ch") in ("w", "b"):
            seen[phase][e["ch"] == "b"].append(e)
    for phase, (beats, answers) in seen.items():
        lasts = [e.get("l") for e in beats]
        if lasts != [0] * (RO_WRITE_BEATS[phase] - 1) + [1]:
            out.append(dict(detail="read-only write beats", phase=phase, wlast=lasts))
        if len(answers) != 1 or not beats or answers[0]["c"] <= beats[-1]["c"]:
            out.append(dict(detail="read-only write B not one after its last beat", phase=phase,
                            b=[e["c"] for e in answers], wlast=beats[-1]["c"] if beats else None))
    return out


def transaction_packets(t, units, packets, release, ids):
    """9: a burst cut into several packets holds one entry of the source's
    pool from its first packet to the last answer of its final one. A pool of
    one lets the burst's packets follow each other without waiting for their
    Bs (the splitter's four-in-flight limit still applies), so the burst's
    second packet enters before its first packet's B; the read behind the
    burst waits until the burst's last packet is answered. Per-packet
    counting would make the second packet wait for the first packet's B, and
    freeing the entry one packet early would let the read in before the last
    B (the oracle judges both orders)."""
    s0, t0 = "s0", "t0"
    held, reading = ids
    t.mark("phase", 9)
    t.set(t0, "b_lat", 10)
    release(True, False)
    m, n = t.snap(t0, "aw"), t.snap(t0, "ar")
    b = t.snap(t0, "b")
    # The 24-beat write crosses five sub-page edges: six packets of four
    # words each; issued first, it holds the pool's one entry while the
    # splitter's four packets are in flight; the read behind it waits.
    t.write(s0, 0x0, beats=24, size=2, id=held, targets=6)
    t.wait(t0, "aw", "==", 4, since=m, label="P9_TXN_FILL")
    t.read(s0, 0x0, beats=1, size=2, id=reading)
    t.wait(t0, "b", "==", packets, since=b, label="P9_LAST_B")
    t.wait(t0, "ar", "==", 1, since=n, label="P9_TXN_WAITED")
    t.gate(t0, "r", True)
    t.drain(label="P9_DRAIN")


def pool(checker, events, sources, targets):
    """At the target port, from its handshakes in every cycle: AW and AR not
    yet answered (B, last R) stay within the pool and reach it, and a last R
    closes the oldest open read of its ARID; in phase 3 a B answers a
    write of one source while an older write of the other is open.
    Transaction ownership is checked using the map's target ID encoding. Phase 9: the waiting write's
    first packet (the third AW) enters only after the pool-holding write's
    final packet is answered (its second B)."""
    capacity = checker.case.get("scenario_args", {}).get("capacity", 4)
    id_bits = checker.case.get("scenario_args", {}).get("id_bits", 2)
    # Phases 7..9 alone (source_pool): the source's pool, not the target's, bounds the
    # peak; phase 9's burst travels as burst_packets packets on one entry and its peak is
    # the splitter's in-flight limit (pool_peak).
    source_pool = checker.case.get("scenario_args", {}).get("source_pool")
    burst_packets = checker.case.get("scenario_args", {}).get("burst_packets", 1)
    read_only = any(checker.case.get("scenario_args", {}).get(k, False)
                    for k in ("read_only_target", "write_only_target"))
    writes_only = checker.case.get("scenario_args", {}).get("read_only_writes", False)
    expect = (checker.case.get("scenario_args", {}).get("pool_peak")
              or (source_pool * burst_packets if source_pool else capacity))
    out, writes, reads, level, peak, reorders, phase = [], [], [], 0, 0, 0, 0
    aw9, b9 = 0, 0
    early_aw, first_b, waited_ar, last_b = None, None, None, None
    marks = checker.tape["marks"]
    if writes_only:
        out += read_only_writes(marks, events)
    for e in events:
        if e.get("k") == "mk" and marks[e.get("m", 0)] == "phase":
            phase = e.get("v")
        if e.get("p") != "t0":
            continue
        k, ch = e.get("k"), e.get("ch")
        if k == "rst" and e.get("v") == 0:
            writes, reads, level = [], [], 0
            continue
        if k != "hs":
            continue
        if ch == "aw":
            writes.append(e["id"])
            level += 1
            if phase == 9:
                aw9 += 1
                if aw9 == 2:
                    early_aw = e.get("c")
        elif ch == "ar":
            reads.append(e["id"])
            level += 1
            if phase == 9 and waited_ar is None:
                waited_ar = e.get("c")
        elif ch == "b":
            # AXI keeps one ID in order: the oldest open write of this ID.
            at = writes.index(e["id"]) if e["id"] in writes else None
            if at is None:
                continue
            if phase == 3 and any(w != e["id"] for w in writes[:at]):
                reorders += 1
            writes.pop(at)
            level -= 1
            if phase == 9:
                b9 += 1
                if b9 == 1:
                    first_b = e.get("c")
                if b9 == burst_packets:
                    last_b = e.get("c")
        elif ch == "r" and e.get("l"):
            if e["id"] not in reads:
                out.append(dict(detail="last R of no open read", rid=e["id"], c=e.get("c")))
                continue
            reads.remove(e["id"])
            level -= 1
        if level > capacity:
            out.append(dict(detail="pool exceeded", level=level, capacity=capacity,
                            c=e.get("c")))
        peak = max(peak, level)
    if peak != expect and not any(d["detail"] == "pool exceeded" for d in out):
        out.append(dict(detail="pool not reached", peak=peak, capacity=expect))
    if not reorders and not source_pool and not read_only:
        out.append(dict(detail="no B overtook an older write of another target ID"))
    if burst_packets > 1:
        if early_aw is None or first_b is None or early_aw >= first_b:
            out.append(dict(detail="the second packet of a burst cut into several waited "
                            "for the first packet's answer", aw=early_aw, b=first_b))
        if waited_ar is None or last_b is None or waited_ar <= last_b:
            out.append(dict(detail="the waiting burst's first packet did not follow the "
                            "pool-holding burst's last answer", ar=waited_ar, b=last_b))
    return out[:8]


CASE["oracle"] = pool
