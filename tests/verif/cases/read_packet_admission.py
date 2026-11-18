"""An unchanged read can issue while four counted packets await replies."""

from nlverif import config as nlconfig

TARGET_BASES = (0, 0x1000)
PACKET_BYTES = 16

CONFIG = dict(
    schema_version=10, name="read_packet_admission", address_width=32,
    reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
    clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")],
    initiators=[dict(name="s0", protocol="axi4", clock_domain="main", data_width=32,
                     access="read_only", id_width=3, len_width=8, max_outstanding=12)],
    targets=[dict(name=f"t{i}", protocol="axi4", clock_domain="main", data_width=32,
                  access="read_only", attributes="amba", len_width=8, max_outstanding=8,
                  mappings=[dict(base=hex(base), size="0x1000")])
             for i, base in enumerate(TARGET_BASES)],
    connectivity="all", arbitration=dict(policy="round_robin"),
    transport=dict(packet_limits=[dict(source="s0", target="t0", max_bytes=PACKET_BYTES)]),
    output=dict(backend="verilog", language="verilog_2001"),
)


def scenario(t, seed):
    t.reset(4)
    t.set("t0", "seed", seed)
    t.set("t0", "r_rate", 55)
    t.set("t1", "r_lat", 4)
    t.ready("t0", "ar", "modulo", 3, 0)
    t.ready("s0", "r", "modulo", 4, 0)

    # The first burst has exactly four packets. Hold their replies while other IDs issue.
    t.gate("t0", "r", False)
    t.read("s0", 0x100, beats=16, id=0, cache=3)
    t.wait("t0", "ar", "==", 4, limit=120, label="four counted packets arrive")
    t.mark("short_reads", 1)
    for k in range(4):
        base = TARGET_BASES[k % 2]
        t.read("s0", base + 0x200 + 0x10 * k, beats=4 if k == 2 else 1, id=k + 1, cache=3)
    t.delay(80)
    # The checker also pairs this read with the burst ahead of it under ID 0.
    t.read("s0", 0x300, id=0, cache=3)
    t.mark("short_reads", 0)
    t.gate("t0", "r", True)
    t.drain(limit=1000, label="unchanged reads and split burst")

    # The fifth packet still waits. Another read with its ID changes target only after it answers.
    t.gate("t0", "r", False)
    counted = t.snap("t0", "ar")
    other = t.snap("t1", "ar")
    t.read("s0", 0x400, beats=20, id=0, cache=3)
    t.read("s0", 0x1400, id=0, cache=3)
    t.delay(80)
    t.expect_now("t0", "ar", "==", 4, "fifth counted packet waits", since=counted)
    t.expect_now("t1", "ar", "==", 0, "same ID waits across targets", since=other)
    t.gate("t0", "r", True)
    t.drain(limit=1000, label="fifth packet and same ID read")
    t.expect_now("t0", "ar", "==", 5, "fifth counted packet arrives", since=counted)
    t.expect_now("t1", "ar", "==", 1, "same ID reaches the other target", since=other)
    t.end()


def admission(checker, events, sources, _targets):
    marks = checker.tape["marks"]
    release = next((e["t"] for e in events
                    if e["k"] == "mk" and marks[e["m"]] == "short_reads" and e["v"] == 0), None)
    short = [s for s in sources if 1 <= s.id <= 4]
    if release is None or len(short) != 4:
        yield dict(detail="unchanged reads did not run through the hold window")
        return
    for s in short:
        if len(s.targets) != 1 or s.targets[0].t >= release:
            yield dict(detail="unchanged read did not reach its target during the hold", id=s.id)
        if s.addr >= TARGET_BASES[1] and (s.done_t is None or s.done_t >= release):
            yield dict(detail="unchanged read did not complete during the hold", id=s.id)
    split = next((s for s in sources if s.addr == 0x400), None)
    following = next((s for s in sources if s.addr == 0x1400), None)
    if following and following.targets and split and split.targets:
        if any(p.end_t is None or p.end_t >= following.targets[0].t for p in split.targets):
            yield dict(detail="same ID switched targets before the split burst answered")


def fifth_packet(_rtl, _mapping):
    # The one packet limit comparison in this two-target read configuration.
    return [(r"^(assign (nl_internal_v\d+) = )3'd4(;\n"
             r"assign nl_internal_v\d+ = \(nl_internal_v\d+ < \2\);)$",
             r"\g<1>3'd5\g<3>", 1)]


CASE = dict(
    name="read_packet_admission", max_cycles=3000, seed=109,
    contracts=["AXI.OUTSTANDING.REACH", "AXI.ID.RETURN", "AXI.OUTLET.FRAMING"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0")]
    + [dict(name=f"t{i}", kind="axi_slave", endpoint=f"target:t{i}", salt=i)
       for i in range(2)],
    model=dict(routes=nlconfig.routes(CONFIG), keeps_decerr=nlconfig.keeps_decerr(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    oracle=admission,
    faults=[dict(id="extra_counted_packet", op="text", subs=fifth_packet,
                 contract="AXI.OUTSTANDING.REACH", expect=["TAPE.EXPECT:fifth counted packet waits"])],
)
