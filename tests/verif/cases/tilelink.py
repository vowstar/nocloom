"""Single-beat TileLink traffic through the public CLI and shared peers."""
from nlverif import config as nlconfig

SOURCE = PARAMS.get("source", "tl_ul")
TARGET = PARAMS.get("target", "tl_ul")
CDC = PARAMS.get("cdc", "0") == "1"
SOURCE_WIDTH = int(PARAMS.get("source_width", "4"))


def configuration():
    cfg = dict(schema_version=10, name="tilelink", address_width=32,
               reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
               clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                                   test_port="test")],
               initiators=[dict(name="s", protocol=SOURCE, data_width=32, clock_domain="main")],
               targets=[dict(name="t", protocol=TARGET, data_width=32,
                             clock_domain="far" if CDC else "main",
                             mappings=[dict(base="0x0", size="0x1000")])],
               connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    s, t = cfg["initiators"][0], cfg["targets"][0]
    if SOURCE in ("tl_ul", "axi4"):
        s["max_outstanding"] = 1 if SOURCE=="tl_ul" and SOURCE_WIDTH==0 else 4
    if SOURCE == "tl_ul":
        s["source_width"] = SOURCE_WIDTH
    if SOURCE == "axi4":
        s.update(id_width=4, len_width=8)
    if TARGET != "tl_ul":
        t["attributes"] = "amba"
    if TARGET == "axi4":
        t.update(id_width=4, len_width=8)
    if TARGET == "tl_ul":
        t.update(source_width=2, sink_width=2, read_side_effects=True)
    if CDC:
        cfg["clock_domains"][0]["period_ps"] = 10000
        cfg["clock_domains"].append(dict(name="far", clock_port="clk_far", reset_port="reset_n",
                                           test_port="test", period_ps=14000))
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint="s", node="q")], exits=[dict(endpoint="t", node="q")]),
            response=dict(nodes=[dict(name="r", clock_domain="far")], links=[],
                          entries=[dict(endpoint="t", node="r")], exits=[dict(endpoint="s", node="r")]))
    return cfg


CFG = configuration()
MASTER = {"tl_ul": "tl_master", "axi4": "axi_master", "apb4": "apb_master", "ahb_lite": "ahb_master"}[SOURCE]
SLAVE = {"tl_ul": "tl_slave", "axi4": "axi_slave", "apb4": "apb_slave", "ahb_lite": "ahb_slave"}[TARGET]
CASE = dict(
    name="tilelink", dut=dict(source="cli", config=CFG),
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO", "AXI.W.UNSTROBED_ZERO", "TL.ACCESS"],
    bfms=[dict(name="s", kind=MASTER, endpoint="initiator:s"),
          dict(name="t", kind=SLAVE, endpoint="target:t", clock="far" if CDC else "main", salt=1)],
    model=dict(routes=nlconfig.routes(CFG), targets=nlconfig.targets(CFG, error_data_zero=True)),
    clocks=dict(main=10.0, far=14.0) if CDC else dict(main=10.0),
    max_cycles=50000, faults=[dict(id="duplicate_source", op="scenario", params=dict(duplicate=True),
                                  expect=["TL.6.4.SOURCE"], contract="TL.ACCESS")] if MASTER=="tl_master" else [],
)


def scenario(t, seed, duplicate=False):
    def source_id(k):
        if MASTER not in ("tl_master", "axi_master") or (MASTER=="tl_master" and SOURCE_WIDTH==0):
            return 0
        return k | (0x8000 if SOURCE_WIDTH==16 and MASTER=="tl_master" else 0)
    t.reset(160)
    t.delay(32)
    if MASTER in ("tl_master", "axi_master"):
        t.ready("s", "b", "delay", 15)
        t.ready("s", "r", "delay", 15)
    if SLAVE == "axi_slave":
        t.ready("t", "aw", "delay", 5)
        t.ready("t", "w", "delay", 5)
        t.ready("t", "ar", "delay", 5)
    else:
        t.set("t", "wait", 5)
    sizes = [0, 1, 2] if MASTER in ("tl_master", "axi_master") else [2]
    for size in sizes:
        for lane in range(0, 4, 1 << size):
            addr = 0x40 + lane
            if MASTER=="tl_master":
                t.set("s", "opcode", 0)
            t.write("s", addr, size=size, id=source_id(1), prot=2, salt=seed)
            t.drain(limit=3000, label="WRITE_SIZE")
            if MASTER=="tl_master":
                t.set("s", "opcode", 8)
            t.read("s", addr, size=size, id=source_id(2), prot=2)
            t.drain(limit=3000, label="READ_SIZE")
    for mask in range(16):
        t.strobes("s", [mask]) if MASTER != "ahb_master" else None
        t.write("s", 0x80, id=source_id(3), prot=2, strobe="table" if MASTER != "ahb_master" else "full", salt=mask)
        t.drain(limit=3000, label="MASK_WRITE")
        t.read("s", 0x80, id=source_id(4), prot=2)
        t.drain(limit=3000, label="MASK_READ")
    if MASTER in ("tl_master", "axi_master") and (MASTER!="tl_master" or SOURCE_WIDTH!=0):
        # Four distinct source values remain live while the first response waits.
        t.ready("s", "r", "never")
        if duplicate:
            t.set("s", "repeat_source", 1)
        if MASTER=="tl_master":
            t.set("s", "withdraw", 1)
        for k in range(5 if MASTER=="tl_master" else 4):
            t.read("s", 0x100 + 4*k, id=source_id(k), prot=2)
        t.wait("s", "ar", ">=", 4, label="MULTIPLE_REQUESTS")
        t.delay(160)
        if MASTER=="tl_master" and not duplicate:
            t.expect_now("s", "peak", ">=", 3, "SOURCES_IN_FLIGHT")
            t.expect_now("s", "retracted", ">=", 1, "REQUEST_RETRACTED")
        t.ready("s", "r", "always")
        t.drain(limit=6000, label="MULTIPLE_RESPONSES")
    if MASTER=="tl_master":
        for opcode in (2, 3):
            t.set("s", "opcode", opcode)
            t.read("s", 0x140, id=source_id(1), prot=2, target="none", resp=2)
            t.drain(limit=3000, label="REJECT_ATOMIC")
        t.set("s", "opcode", 5)
        for param in range(8):
            counters = ("aw", "ar") if SLAVE=="axi_slave" else ("access",) if SLAVE=="apb_slave" else ("addr",)
            before = {name: t.snap("t", name) for name in counters}
            t.set("s", "param", param)
            t.write("s", 0x140, id=source_id(2), prot=2, target="none", resp=2)
            t.drain(limit=3000, label=f"REJECT_INTENT_PARAM_{param}")
            for name in counters:
                t.expect_now("t", name, "==", 0, f"INTENT_PARAM_{param}_NO_TARGET_{name}", since=before[name])
        t.set("s", "param", 0)
        for opcode in (6, 7):
            t.set("s", "opcode", opcode)
            t.write("s", 0x140, id=source_id(2), prot=2, target="none", resp=2)
            t.drain(limit=3000, label="REJECT_OPCODE")
        t.set("s", "opcode", 8)
        t.set("s", "corrupt", 1)
        t.write("s", 0x140, id=source_id(3), prot=2, target="none", resp=2)
        t.drain(limit=3000, label="REJECT_CORRUPT")
        t.set("s", "corrupt", 0)
        t.set("s", "size_override", 4)
        t.read("s", 0x140, beats=2, id=source_id(4), prot=2, target="none", resp=2)
        t.drain(limit=3000, label="REJECT_SIZE")
        t.write("s", 0x140, beats=2, id=source_id(5), prot=2, target="none", resp=2)
        t.drain(limit=3000, label="DRAIN_REJECTED_WRITE")
        t.set("s", "size_override", 0)
        t.read("s", 0x140, id=source_id(5), prot=2)
        t.drain(limit=3000, label="AFTER_REJECTION")
    if MASTER=="tl_master" and SOURCE_WIDTH!=0:
        for hold in (40, 41):
            if SLAVE=="axi_slave":
                t.gate("t", "b", False)
            else:
                t.set("t", "wait", 1000)
            t.ready("s", "b", "never")
            t.ready("s", "r", "never")
            t.write("s", 0x240, id=source_id(10), prot=2)
            t.delay(12)
            before = t.snap("s", "rlast")
            t.set("s", "size_override", 4)
            t.read("s", 0x240, beats=2, id=source_id(11), prot=2, target="none", resp=2)
            t.wait("s", "rvalid", "==", 1, limit=2000, label="LOCAL_RESPONSE_STARTED")
            t.set("s", "size_override", 0)
            if SLAVE=="axi_slave":
                t.gate("t", "b", True)
            else:
                t.set("t", "wait", 5)
            t.ready("s", "r", "delay", hold)
            t.wait("s", "rlast", ">=", 1, since=before, limit=2000,
                   label="LOCAL_RESPONSE_NOT_INTERLEAVED")
            t.ready("s", "b", "always")
            t.drain(limit=6000, label="LOCAL_RESPONSE_AND_WRITE")
    if MASTER=="tl_master" and SOURCE_WIDTH!=0:
        t.ready("s", "b", "never")
        for k in range(4):
            t.write("s", 0x200 + 4*k, id=source_id(8)+k, prot=2)
        t.delay(160)
        t.ready("s", "b", "always")
        t.drain(limit=6000, label="MULTIPLE_WRITES")
    if MASTER=="axi_master" and TARGET=="tl_ul":
        for beats in (3, 7, 17):
            for size in (0, 1, 2):
                t.write("s", 0x280, beats=beats, size=size, id=source_id(1), prot=2)
                t.drain(limit=6000, label="SPLIT_AXI_WRITE")
                t.read("s", 0x280, beats=beats, size=size, id=source_id(2), prot=2)
                t.drain(limit=6000, label="SPLIT_AXI_READ")
        t.write("s", 0x304, beats=4, burst="wrap", id=source_id(1), prot=2)
        t.drain(limit=6000, label="WRAP_WRITE")
        t.read("s", 0x304, beats=4, burst="wrap", id=source_id(2), prot=2)
        t.drain(limit=6000, label="WRAP_READ")
        t.write("s", 0x300, beats=4, burst="fixed", id=source_id(1), prot=2, target="none", resp=2)
        t.drain(limit=3000, label="FIXED_REJECT")
    if MASTER=="tl_master":
        denied_before = t.snap("s", "read_denied")
        corrupt_before = t.snap("s", "read_corrupt")
        write_before = t.snap("s", "write_denied")
    # Both target errors must return through the protocol adapter.
    if SLAVE == "axi_slave":
        t.set("t", "b_default", 2)
        t.set("t", "r_default", 2)
    else:
        t.set("t", "err_default", 1)
    t.write("s", 0x180, id=source_id(6), prot=2)
    t.drain(limit=3000, label="DENIED_WRITE")
    t.read("s", 0x180, id=source_id(7), prot=2)
    t.drain(limit=3000, label="CORRUPT_READ")
    if MASTER=="tl_master":
        t.expect_now("s", "read_denied", "==", 0, "NETWORK_READ_NOT_DENIED", since=denied_before)
        t.expect_now("s", "read_corrupt", "==", 1, "NETWORK_READ_CORRUPT", since=corrupt_before)
        t.expect_now("s", "write_denied", "==", 1, "NETWORK_WRITE_DENIED", since=write_before)
        t.read("s", 0x2000, id=source_id(0), prot=2, target="none", resp=2)
        t.drain(limit=3000, label="DECODE_MISS_CORRUPT")
        t.expect_now("s", "read_denied", "==", 0, "DECODE_MISS_NOT_DENIED", since=denied_before)
    if SLAVE=="tl_slave":
        t.set("t", "corrupt_only", 1)
        t.read("s", 0x180, id=source_id(0), prot=2)
        t.drain(limit=3000, label="TARGET_CORRUPT_ONLY")
        t.set("t", "immediate_error", 1)
        t.write("s", 0x180, id=source_id(0), prot=2)
        t.drain(limit=3000, label="SAME_CYCLE_ACK")
        t.read("s", 0x180, id=source_id(0), prot=2)
        t.drain(limit=3000, label="SAME_CYCLE_DATA")
        t.expect_now("t", "same_cycle", ">=", 2, "SAME_CYCLE_RESPONSES")
        t.expect_now("t", "a_stalls", ">=", 1, "A_BACKPRESSURE")
    if MASTER=="tl_master":
        t.expect_now("s", "d_stalls", ">=", 1, "D_BACKPRESSURE")
    t.end()
