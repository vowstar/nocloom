"""Every response returns to its initiator with its AxID, at targets that answer out of order.

Three AXI4 initiators with several bursts in flight send random AxIDs to three targets. t0 and t2
answer the youngest ready request first. a0 has two initiator flows by AxID, and its even flow
reaches t0 only.

| Target | Port | Reached by | What the port carries, from bit 0 |
| --- | --- | --- | --- |
| t0, AXI4, two flows, four bursts | default width, 7 bits | a0 even, a0 odd, b0, c0 | flow 1, index 2, AxID 4 |
| t1, AXI4, one burst | 3 bits | a0 odd, c0 | the folded form: index 1 and AxID 4 need 5 bits |
| t2, AXI3, four bursts | 5 bits | b0, c0 | index 1, AxID 4 |

The offline checker owns every target access to one source transaction by the ID the map gives
(tests/target_id_model.py), follows its bytes to the target memory and back, and requires every
source response on its own port with its own AxID and data. The configuration layer compares the
map's entries of these ports with the rule read from the definition
(tests/verif/cases/config_target_ids.py).

Negative controls on t0's read ID, in a copy of the generated Verilog: the index and the low AxID
bits exchanged, and the top bit dropped. Each loses the pairing (TARGET_UNATTRIBUTED)."""

import random

from nlverif import config as nlconfig

SOURCES = ("a0", "b0", "c0")
ID_BITS = dict(a0=3, b0=2, c0=4)
# (initiator, initiator flow) -> the (target, target flow, window base) it reaches.
T0 = [("t0", 0, 0x0000), ("t0", 1, 0x1000)]
REACH = {("a0", 0): T0,
         ("a0", 1): T0 + [("t1", 0, 0x2000)],
         ("b0", 0): T0 + [("t2", 0, 0x3000)],
         ("c0", 0): T0 + [("t1", 0, 0x2000), ("t2", 0, 0x3000)]}


def configuration():
    initiators = [dict(name=s, clock_domain="main", protocol="axi4", data_width=32,
                       id_width=ID_BITS[s], len_width=8, max_outstanding=4) for s in SOURCES]
    initiators[0].update(route_classes=["even", "odd"], default_class="odd",
                         id_ranges=[dict(first=k, last=k, **{"class": "even"}) for k in (0, 2)])
    axi = dict(clock_domain="main", data_width=32, attributes="amba")
    return dict(
        schema_version=10, name="target_id_fields", address_width=16,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                            test_port="test")],
        initiators=initiators,
        targets=[dict(axi, name="t0", protocol="axi4", len_width=8, flows=2, max_outstanding=4,
                      mappings=[dict(base="0x0", size="0x1000"),
                                dict(base="0x1000", size="0x1000", flow=1)]),
                 dict(axi, name="t1", protocol="axi4", len_width=8, id_width=3,
                      mappings=[dict(base="0x2000", size="0x1000")]),
                 dict(axi, name="t2", protocol="axi3", len_width=4, id_width=5, max_outstanding=4,
                      mappings=[dict(base="0x3000", size="0x1000")])],
        connectivity=dict(a0=["t0", dict(target="t1", source_path=1)], b0=["t0", "t2"],
                          c0=["t0", "t1", "t2"]),
        output=dict(backend="verilog", language="verilog_2001"))


def read_id_fault(name, bits, contract, note):
    """t0's ARID rebuilt from the named bits of the generated value, highest first."""
    return dict(id=name, op="text", component="dut",
                subs=[(r"^assign nl_t_t0_arid = (\S+);",
                       r"wire [6:0] nl_fault_arid = \1;\nassign nl_t_t0_arid = {" + bits + "};", 1)],
                expect=["TARGET_UNATTRIBUTED"], contract=contract, note=note)


def target_flows(checker, events, sources, targets):
    for burst in targets:
        if burst.port == "t0" and burst.owner is not None:
            flow = burst.owner.addr // 0x1000
            if burst.id & 1 != flow:
                yield dict(port=burst.port, id=burst.id, want_flow=flow)


CFG = configuration()
CASE = dict(
    name="target_id_fields",
    contracts=["AXI.ID.RETURN", "AXI.TARGET.ID_RULE"],
    dut=dict(source="cli", config=CFG),
    bfms=[dict(name=s, kind="axi_master", endpoint=f"initiator:{s}") for s in SOURCES]
    + [dict(name=t, kind="axi_slave", endpoint=f"target:{t}", salt=k + 1)
       for k, t in enumerate(("t0", "t1", "t2"))],
    model=dict(routes=nlconfig.routes(CFG), keeps_decerr=True,
               targets=nlconfig.targets(CFG, cache="amba", prot=True)),
    oracle=target_flows,
    faults=[read_id_fault("fields_swapped",
                          "nl_fault_arid[6:5], nl_fault_arid[2:1], nl_fault_arid[4:3], "
                          "nl_fault_arid[0]",
                          "AXI.TARGET.ID_RULE",
                          "t0's index and its two low AxID bits change places"),
            read_id_fault("top_bit_dropped", "1'b0, nl_fault_arid[5:0]",
                          "AXI.ID.RETURN",
                          "t0 loses the highest AxID bit")],
    seed=20261117,
    max_cycles=200000,
)


def overtake(t, target, initiators, addresses, ids, issue, request, response):
    first, second = initiators
    t.gate(target, response, False)
    requests = t.snap(target, request)
    first_done, second_done = t.snap(first, response), t.snap(second, response)
    issue(first, addresses[0], id=ids[0])
    t.wait(target, request, "==", 1, since=requests, label="FIRST_REQUEST")
    issue(second, addresses[1], id=ids[1])
    t.wait(target, request, "==", 2, since=requests, label="SECOND_REQUEST")
    t.delay(12)
    t.gate(target, response, True)
    t.wait(second, response, "==", 1, since=second_done, label="YOUNGER_RESPONSE")
    t.expect_now(first, response, "==", 0, "OLDER_PENDING", since=first_done)
    t.drain(label="OVERTAKE")


def scenario(t, seed):
    rnd = random.Random(seed)
    t.reset(8)
    t.delay(4)
    # Responses wait a few cycles, then the youngest ready one leaves first.
    for target in ("t0", "t1", "t2"):
        t.set(target, "r_order", 1)
        t.set(target, "b_order", 1)
        t.set(target, "r_lat", 6)
        t.set(target, "b_lat", 6)
    for issue, request, response in ((t.read, "ar", "r"), (t.write, "aw", "b")):
        overtake(t, "t0", ("a0", "c0"), (0x80, 0x1180), (2, 10), issue, request, response)
        overtake(t, "t2", ("b0", "c0"), (0x3080, 0x3180), (2, 10), issue, request, response)
    # The two reads have the same ID key and distinct addresses in the same mapping.
    t.read("a0", 0x140, id=2)
    t.read("a0", 0x144, id=2)
    t.drain(label="SAME_KEY")
    # Every AxID of every initiator once to each target flow it reaches, both directions.
    for index, source in enumerate(SOURCES):
        for ident in range(1 << ID_BITS[source]):
            flow = 0 if source != "a0" or ident in (0, 2) else 1
            for target, _, base in REACH[(source, flow)]:
                address = base + 0x100 * index + 4 * ident
                t.write(source, address, id=ident)
                t.read(source, address, id=ident)
            t.drain(limit=20000, label="EVERY_ID")
    # All initiators at once with random AxIDs, lengths and targets.
    for step in range(64):
        for index, source in enumerate(SOURCES):
            ident = rnd.randrange(1 << ID_BITS[source])
            flow = 0 if source != "a0" or ident in (0, 2) else 1
            target, _, base = rnd.choice(REACH[(source, flow)])
            beats = rnd.randrange(4) + 1
            address = base + 0x800 + 0x100 * index + 4 * rnd.randrange(32)
            if rnd.randrange(2):
                t.write(source, address, beats=beats, id=ident)
            else:
                t.read(source, address, beats=beats, id=ident)
        if step % 8 == 7:
            t.drain(limit=20000, label="CONCURRENT")
    t.drain(limit=20000, label="CONCURRENT")
    t.end()
