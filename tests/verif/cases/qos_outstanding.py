"""Fixed QoS levels with source bursts, target outstanding contexts and
registered links on the public schema 10 path (CLI). """

from nlverif import config as nlconfig

CONFIG = dict(
    schema_version=10, name="arbitration_funnel_graph", address_width=32,
    arbitration=dict(policy="fifo"),
    reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")], clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")],
    initiators=[dict(name=f"s{i}", clock_domain="main", protocol="axi4", data_width=32,
                     id_width=2, len_width=8, narrow_bursts="split_non_modifiable",
                     qos=dict(levels=dict(write=(1, 7)[i], read=(7, 1)[i])))
                for i in range(2)],
    targets=[dict(name=f"t{i}", clock_domain="main", protocol="axi4", data_width=32,
                  len_width=8, attributes="amba", max_outstanding=4,
                  mappings=[dict(base=hex(i * 65536), size="0x10000")])
             for i in range(2)],
    # Reads and writes share the source capacity: eight keeps four of each.
    transport=dict(max_outstanding=8),
    connectivity="all",
    topology=dict(
        request=dict(nodes=[dict(name=n) for n in ("a", "b", "m")],
                     links=[{"from": n, "to": "m", "pipeline": {"forward": True, "backward": True}} for n in ("a", "b")],
                     entries=[dict(endpoint="s0", node="a"), dict(endpoint="s1", node="b")],
                     exits=[dict(endpoint=t, node="m") for t in ("t0", "t1")]),
        response=dict(nodes=[dict(name=n) for n in ("n", "x")],
                      links=[{"from": "x", "to": "n", "pipeline": {"forward": True, "backward": True}}],
                      entries=[dict(endpoint=t, node="x") for t in ("t0", "t1")],
                      exits=[dict(endpoint=s, node="n") for s in ("s0", "s1")])),
    output=dict(backend="verilog", language="verilog_2001"))


def response_configuration():
    """A companion DUT: two low responses queue behind the other target's long read."""
    config = dict(
        schema_version=10, name="response_freeze", address_width=32,
        arbitration=dict(policy="round_robin"),
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=8, max_outstanding=4,
                         qos=dict(shaper=dict(bytes_per_1024_cycles=512, burst_bytes=16),
                                  scheduler=dict(under_level=3, over_level=0, threshold_percent=50))),
                    dict(name="s1", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=8, max_outstanding=4,
                         qos=dict(levels=dict(write=2, read=2))),
                    dict(name="s2", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=8, max_outstanding=4)],
        targets=[dict(name=f"t{i}", clock_domain="main", protocol="axi4", data_width=32,
                      len_width=8, attributes="amba", max_outstanding=2,
                      pipes=dict(response=dict(forward=True)),
                      mappings=[dict(base=hex(i * 65536), size="0x10000")])
                 for i in range(2)],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    return dict(
        label="response-freeze", dut=dict(source="cli", config=config),
        fault_recipe=dict(recipe="response_freeze"), scenario_args=dict(mode="freeze"),
        probe_checks=dict(flow_levels={0: 0, 1: 0, 2: 2, 3: 2, 4: 0, 5: 0}, outputs=5,
                          policy_rule=dict(default="round_robin", overrides={}),
                          reach=("frozen_low",)),
        bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
              dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
              dict(name="s2", kind="axi_master", endpoint="initiator:s2"),
              dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0),
              dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1)],
        model=dict(routes=nlconfig.routes(config), error_codes=nlconfig.error_codes(config),
                   targets=nlconfig.targets(config)),
        faults=[dict(id="response_level_unfrozen", component="network", op="replace",
                     at={"binding": "pressure/target/t0/response.pending"},
                     **{"with": {"const": 0}}, expect=["PROBE_GRANT", "REACH_MISS"],
                     contract="AXI.QOS.LEVEL")])


CASE = dict(
    name="qos_outstanding",
    contracts=["AXI.QOS.LEVEL", "AXI.OUTSTANDING.REACH", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.MERGE.SEVERITY", "AXI.RESET.CANCEL", "AXI.ERR.READ_ZERO"],
    dut=dict(source="cli", config=CONFIG),
    fault_recipe=dict(recipe="qos_bounded"),
    probes=dict(instance={"network": "dut"}),
    # Flow 2i is source i's writes, 2i+1 its reads (s0 1/7, s1 7/1).
    # The input sets FIFO for every output; three outputs arbitrate.
    probe_checks=dict(flow_levels={0: 1, 1: 7, 2: 7, 3: 1}, outputs=3,
                      policy_rule=dict(default="fifo", overrides={}),
                      reach=("held_higher", "contended", "backpressure")),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0),
          dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, error_data_zero=True, narrow_single=True)),
    max_cycles=30000,
    faults=[
        dict(id="wrong_response_source", component="network", op="set_operand", operand=0,
             at={"match": {"op": "BitOr", "ordered": True, "args": [
                 {"binding": "burst_service/t0/w.rsp.source"}, "*"]}},
             **{"with": {"op": "Subtract", "args": [{"self": True}, {"const": 2}]}},
             # A misrouted B reaching an idle source is dropped there (or extra at a busy one).
             expect=["AXI.B.UNEXPECTED", "SOURCE_EXTRA_B", "TAPE.DRAIN_TIMEOUT:drain",
                     "SOURCE_UNANSWERED"],
             note="Misroute only t0 write responses and check source ownership and completion.",
             contract="AXI.ROUTE.LOCAL_OFFSET"),
        dict(id="early_output_release", component="network", op="replace", hits=3,
             at={"glob": "graph/*.release", "where": {"op": "BitAnd"}},
             **{"with": {"binding_sub": [".release", ".transfer"]}},
             expect=["TARGET_WRITE_BYTES", "TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED",
                     "AXI.W.LAST", "SOURCE_R_DATA", "SOURCE_R_RESP", "SOURCE_B_RESP",
                     "AXI.R.LAST", "AXI.B.UNEXPECTED", "AXI.R.UNEXPECTED"],
             note="Release an output before its accepted tail and check packet data and framing.",
             contract="AXI.QOS.LEVEL"),
        dict(id="target_outstanding_2", component="network", op="variant",
             params={"target_outstanding": "2"},
             expect=["TAPE.WAIT_TIMEOUT:N4_K4_REACHED", "TAPE.EXPECT:N4_K4_REACHED"],
             contract="AXI.OUTSTANDING.REACH",
             note="parameter-level variant: target max_outstanding 4 -> 2"),
        dict(id="level_flat", component="network", op="replace", hits=2,
             at={"glob": "graph/req/req_node2>endpoint/t0.level*"},
             **{"with": {"const": 0}},
             expect=["PROBE_GRANT", "ORDER_INTERPOSE"], contract="AXI.QOS.LEVEL",
             note="equal levels at the t0 output; the port-level order alone may not tell, "
                  "the per-cycle grant replay does"),
    ],
)
CASE["configs"] = [dict(label=""), response_configuration()]
CASE["negative_faults_all"] = True


def response_scenario(t):
    """Queue two low responses, then raise their context while t1 holds the response port."""
    for s in ("s0", "s1", "s2"):
        t.ready(s, "b", "always")
        t.ready(s, "r", "always")
        t.set(s, "w_after_aw", 0)
        t.set(s, "w_gap", 0)
        t.set(s, "ar_gap", 0)
    for target in ("t0", "t1"):
        for ch in ("aw", "w", "ar"):
            t.ready(target, ch, "always")
        t.set(target, "out_cap", 2)
        t.set(target, "b_lat", 2)
        t.set(target, "r_lat", 2)
    t.gate("t1", "b", False)
    t.reset(4)
    t.delay(2)
    t.read("s0", 0x10000, beats=32, id=0)
    t.read("s0", 0x10020, beats=1, id=1)
    t.read("s0", 0x10040, beats=64, id=2)
    t.write("s1", 0x13000, id=0)
    t.wait("s0", "rlast", "==", 1, label="FIRST_CHARGE")
    t.write("s0", 0x1000, id=0)
    t.write("s0", 0x1020, id=1)
    t.read("s0", 0x40, id=3)
    t.read("s0", 0x10060, id=0)
    t.delay(10)
    t.write("s0", 0x1040, id=2)
    t.write("s0", 0x1060, id=3)
    t.delay(150)
    t.gate("t1", "b", True)
    t.drain(limit=10000, label="RESPONSE_FREEZE_DRAIN")
    t.end()


def scenario(t, seed, mode="fixed"):
    if mode == "freeze":
        return response_scenario(t)
    s0, s1, t0, t1 = "s0", "s1", "t0", "t1"
    sources, targets = (s0, s1), (t0, t1)

    def source_ready(on):
        for s, (bm, rm) in zip(sources, ((4, 3), (5, 4))):
            if on:
                t.ready(s, "b", "modulo", bm, 0)
                t.ready(s, "r", "modulo", rm, 0)
            else:
                t.ready(s, "b", "never")
                t.ready(s, "r", "never")

    def release(target, on):
        t.gate(target, "b", on)
        t.gate(target, "r", on)

    for s in sources:
        t.set(s, "w_after_aw", 1)
        t.set(s, "w_gap", 2)
    for target in targets:
        t.ready(target, "aw", "modulo", 5, 0)
        t.ready(target, "w", "modulo", 4, 0)
        t.ready(target, "ar", "modulo", 3, 0)
        t.set(target, "out_cap", 4)
        # Target error rule of the scenario: address bit 8, or low byte 6.
        t.set(target, "errm0_mask", 0x100)
        t.set(target, "errm0_match", 0x100)
        t.set(target, "errm0_code", 2)
        t.set(target, "errm1_mask", 0xFF)
        t.set(target, "errm1_match", 0x06)
        t.set(target, "errm1_code", 2)
    source_ready(True)
    t.reset(4)
    t.delay(2)

    def snaps():
        return {(p, k): t.snap(p, k) for p, k in
                ((t0, "aw"), (t0, "ar"), (t0, "applied"), (t0, "b"), (t1, "aw"), (t1, "ar"),
                 (t1, "applied"))}

    # 1: the target's in-flight pool of four is shared by both directions:
    # four writes fill it; while they hold it, the reads wait at the outlet,
    # and each B that leaves frees an entry a waiting read takes.
    t.mark("phase", 1)
    m = snaps()
    release(t0, False)
    for k in range(4):
        t.write(s0, 0x800 + k * 256, size=2, id=1)
    t.wait(t0, "aw", "==", 4, since=m[(t0, "aw")], label="N4_K4_REACHED")
    t.delay(12)
    t.expect_now(t0, "peak_w", ">=", 4, "N4_K4_REACHED")
    t.expect_now(s0, "w_out", "==", 4, "N4_K4_REACHED")
    for a in (0x800, 0x900, 0xA00, 0xB00):
        t.read(s0, a, size=2, id=1)
    t.delay(12)
    t.expect_now(t0, "ar", "==", 0, "POOL_FULL_BLOCKS_READS", since=m[(t0, "ar")])
    t.gate(t0, "b", True)
    t.wait(t0, "ar", "==", 4, since=m[(t0, "ar")], label="READS_FOLLOW_FREED_ENTRIES")
    t.delay(12)
    t.expect_now(t0, "peak_r", ">=", 4, "N4_K4_REACHED")
    t.expect_now(s0, "r_out", "==", 4, "N4_K4_REACHED")
    release(t0, True)
    t.drain(label="DRAIN_1")
    # 2: one 12-beat narrow burst splits into packets; a higher level packet
    # of the other source lands between them. The read and write bursts of s0
    # share its request path, whose grant stays with a burst until its last
    # packet: the split read burst, issued once the write's first packet
    # reached the target, waits behind the whole split write burst.
    t.mark("phase", 2)
    m = snaps()
    release(t0, False)
    t.write(s0, 0x2000, beats=12, size=1, id=2)
    t.wait(t0, "aw", ">=", 1, since=m[(t0, "aw")], label="SAME_TAG_FOUR_PACKETS")
    t.read(s0, 0x4000, beats=12, size=1, id=2)
    t.wait(t0, "aw", "==", 4, since=m[(t0, "aw")], label="SAME_TAG_FOUR_PACKETS")
    t.delay(12)
    t.expect_now(t0, "aw", "==", 4, "SAME_TAG_FOUR_PACKETS", since=m[(t0, "aw")])
    t.expect_now(s0, "r_out", "==", 1, "BURST_HOLDS_READ")
    t.expect_now(t0, "ar", "==", 0, "BURST_HOLDS_READ", since=m[(t0, "ar")])
    t.expect_now(s0, "w_out", "==", 1, "SAME_TAG_FOUR_PACKETS")
    t.write(s1, 0x2200, size=2, id=2)
    t.delay(8)
    t.gate(t0, "b", True)
    source_ready(False)
    t.delay(30)
    source_ready(True)
    t.wait(t0, "ar", "==", 4, since=m[(t0, "ar")], label="SAME_TAG_FOUR_PACKETS")
    t.delay(12)
    t.expect_now(t0, "ar", "==", 4, "SAME_TAG_FOUR_PACKETS", since=m[(t0, "ar")])
    t.expect_now(s0, "r_out", "==", 1, "SAME_TAG_FOUR_PACKETS")
    t.gate(t0, "r", True)
    source_ready(False)
    t.delay(30)
    source_ready(True)
    t.drain(label="DRAIN_2")
    t.expect_now(t0, "aw", "==", 13, "TWELVE_PACKETS_ONE_BURST", since=m[(t0, "aw")])
    t.expect_now(t0, "ar", "==", 12, "TWELVE_PACKETS_ONE_BURST", since=m[(t0, "ar")])
    t.check(kind="interpose", port=t0, ch="aw", outer=(0x2000, 0x2018), inner=0x2200,
            after_mark="phase", mark_value=2)
    # 3: B returned out of order across sources at one target.
    t.mark("phase", 3)
    m = snaps()
    t.gate(t0, "b", False)
    t.set(t0, "b_order", 1)
    t.write(s0, 0x6000, size=2, id=0)
    t.wait(t0, "aw", "==", 1, since=m[(t0, "aw")], label="FIRST_REORDER_DESCRIPTOR")
    t.write(s1, 0x6100, size=2, id=2)
    t.wait(t0, "aw", "==", 2, since=m[(t0, "aw")], label="SECOND_REORDER_DESCRIPTOR")
    t.wait(t0, "applied", "==", 2, since=m[(t0, "applied")], label="SECOND_REORDER_DESCRIPTOR")
    t.gate(t0, "b", True)
    t.drain(label="DRAIN_3")
    t.set(t0, "b_order", 0)
    t.check(kind="reorder", port=t0, after_mark="phase", mark_value=3)
    # 4: both sources, both directions, one target.
    t.mark("phase", 4)
    t.write(s0, 0x6800, beats=8, size=2, id=1)
    t.write(s1, 0x6900, beats=8, size=2, id=1)
    t.read(s0, 0x7000, beats=8, size=2, id=1)
    t.read(s1, 0x7100, beats=8, size=2, id=1)
    t.drain(label="DRAIN_4")
    # 5: a long write then a shorter one from the other source.
    t.mark("phase", 5)
    m = snaps()
    t.write(s0, 0x7800, beats=16, size=2, id=2)
    t.wait(t0, "aw", ">=", 1, since=m[(t0, "aw")])
    t.write(s1, 0x7900, beats=8, size=2, id=2)
    t.drain(label="DRAIN_5")
    # 6: both targets hold their responses, then release them together.
    t.mark("phase", 6)
    m = snaps()
    release(t0, False)
    release(t1, False)
    t.write(s0, 0x8000, size=2, id=3)
    t.write(s1, 0x18000, size=2, id=3)
    t.read(s0, 0x8200, beats=8, size=2, id=3)
    t.read(s1, 0x18200, beats=8, size=2, id=3)
    t.wait(t0, "applied", "==", 1, since=m[(t0, "applied")])
    t.wait(t1, "applied", "==", 1, since=m[(t1, "applied")])
    t.wait(t0, "ar", "==", 1, since=m[(t0, "ar")])
    t.wait(t1, "ar", "==", 1, since=m[(t1, "ar")])
    t.delay(5)
    release(t0, True)
    release(t1, True)
    t.drain(label="DRAIN_6")
    # 7: reads of both sources cross at the two targets.
    t.mark("phase", 7)
    m = snaps()
    t.read(s1, 0x8800, beats=16, size=2, id=0)
    t.wait(t0, "ar", ">=", 1, since=m[(t0, "ar")])
    t.delay(3)
    t.read(s0, 0x18800, beats=8, size=2, id=0)
    t.drain(label="DRAIN_7")
    # 8: common reset with packets of both directions inside the target: two
    # packets per direction fill the shared in-flight pool of four.
    t.mark("phase", 8)
    m = snaps()
    release(t0, False)
    t.write(s0, 0xA000, beats=2, size=1, id=1)
    t.read(s0, 0xB000, beats=2, size=1, id=1)
    t.wait(t0, "aw", "==", 2, since=m[(t0, "aw")], label="RESET_NONEMPTY")
    t.wait(t0, "ar", "==", 2, since=m[(t0, "ar")], label="RESET_NONEMPTY")
    t.expect_now(t0, "w_pend", ">", 0, "RESET_NONEMPTY")
    t.expect_now(t0, "r_pend", ">", 0, "RESET_NONEMPTY")
    t.reset(4)
    release(t0, True)
    t.delay(2)
    # 9: exact traffic after the reset.
    t.mark("phase", 9)
    m = snaps()
    t.write(s0, 0xC000, beats=6, size=1, id=3)
    t.read(s1, 0xD000, beats=6, size=1, id=2)
    t.drain(label="DRAIN_9")
    t.expect_now(t0, "aw", "==", 6, "POST_RESET_EXACT", since=m[(t0, "aw")])
    t.expect_now(t0, "ar", "==", 6, "POST_RESET_EXACT", since=m[(t0, "ar")])
    # 10: equal-depth arrival at the shared output: the higher level wins an
    # unheld choice (s1 writes at level 7, s0 reads at level 7).
    t.mark("phase", 10)
    t.write(s0, 0x9000, size=2, id=1)
    t.write(s1, 0x9100, size=2, id=1)
    t.read(s1, 0x9200, size=2, id=1)
    t.read(s0, 0x9300, size=2, id=1)
    t.drain(label="DRAIN_10")
    t.check(kind="first", port=t0, ch="aw", addr=0x9100, after_mark="phase", mark_value=10)
    t.check(kind="first", port=t0, ch="ar", addr=0x9300, after_mark="phase", mark_value=10)
    t.end()
