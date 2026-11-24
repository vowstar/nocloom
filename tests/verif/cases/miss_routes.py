"""Decode miss answers of a source of two flows, through the public CLI.

The AXI4 source s0 has two flows: AxID 0 and 2 take flow 0, AxID 1 and 3 flow 1. Its named
default target t0 (AXI4, four bursts) answers its decode misses. A miss request takes the flow
pair of flow 0, the lowest source flow that reaches t0's mapping. Its answer returns over the
route of its AxID's flow, behind the earlier answers of that AxID (docs/network.md, Connectivity
validation and misses).

In the main configuration both flows reach t0 and return over their own routes: flow 0 by ra,
rb and rd, flow 1 by ra, rc and rd, whose two links each hold a forward slice. The oracle counts
the words that pass the slice on the first rc link (map storage) in each phase:
  1, 2. A read miss, then a write miss, of AxID 1 alone: one word each.
  3. A read and a write of AxID 1 to t0: two words.
  4, 5. Three reads, then three writes, of AxID 1, which t0 answers after 30 cycles each, then
     a read miss and a write miss of AxID 3: five words.
  6, 7. Three reads, then three writes, of AxID 1 and then a miss of the same direction and
     AxID: four words, and the miss answer arrives after the three answers of its AxID.
  8. s0 holds RREADY while a read miss of AxID 3 waits, and writes one beat to t0 with AxID 1:
     two words.
  9. s0 holds BREADY while a write miss and a read miss of AxID 3 wait behind three reads of
     AxID 1: five words.
  10. A read miss and a write miss of AxID 0 and 2: none.

In the configuration "target_flows" t0 has two flows: its window 0x0 takes flow 1 and its window
0x1000 flow 0. s0 flow 0 reaches both, s0 flow 1 only t0 flow 0. Each connected pair returns
over its own route through ra, with a forward slice on its first link: flow pair (0, 0) by rb,
(0, 1) by rc and (1, 0) by re. A miss takes t0 flow 1, the smallest reached mapping, and the
pair (0, 1). The answer of a miss of AxID 1 has the pair (1, 1), which the matrix leaves out,
so it returns by rc, the miss route. The oracle counts the words on each slice per phase, a
read and a write each: misses of AxID 1 (rc), misses of AxID 0 (rc), AxID 1 to the window
0x1000 (re), AxID 0 to the window 0x1000 (rb), AxID 0 to the window 0x0 (rc). Then three
writes of AxID 1 to the window 0x1000, which t0 answers after 30 cycles, are followed by a
write miss of AxID 1: its window differs, so the source holds it until those answers return.

In the configuration "unconnected" flow 1 reaches only t1, so its misses return over the miss's
flow pair. Three writes, then three reads, of AxID 1 to t1, which answers after 30 cycles, are
followed by a miss of AxID 1 of the same direction: the source holds the miss until those
answers return, so they arrive in order.

The configurations "separate" and "separate_unconnected" run the same with separate read and
write paths.
"""

from nlverif import config as nlconfig

MISS = 0x200000
T1 = 0x10000
LIMIT = 2000  # cycles a wait may take before it is a hang: each phase needs under 300
WORDS = {1: 1, 2: 1, 3: 2, 4: 5, 5: 5, 6: 4, 7: 4, 8: 2, 9: 5, 10: 0}


def configuration(connected=True, paths=None):
    targets = [dict(name="t0", clock_domain="main", protocol="axi4", data_width=32, len_width=8,
                    id_width=4, attributes="amba", max_outstanding=4,
                    mappings=[dict(base="0x0", size="0x10000")])]
    source = dict(name="s0", clock_domain="main", protocol="axi4", data_width=32, id_width=2,
                  len_width=8, max_outstanding=4, default_target="t0",
                  route_classes=["even", "odd"], default_class="odd",
                  id_ranges=[dict(first=k, last=k, **{"class": "even"}) for k in (0, 2)])
    if connected:
        nodes = ["ra", "rb", "rc", "rd"]
        links = [{"from": "ra", "to": "rb"}, {"from": "rb", "to": "rd"},
                 {"from": "ra", "to": "rc", "pipeline": dict(slice="forward")},
                 {"from": "rc", "to": "rd", "pipeline": dict(slice="forward")}]
        exits = [dict(endpoint="t0", node="qa")]
        entries = [dict(endpoint="t0", node="ra")]
        routes = [dict(initiator="s0", target="t0", request=["qa"], response=["ra", "rb", "rd"]),
                  dict(initiator="s0", target="t0", source_path=1, request=["qa"],
                       response=["ra", "rc", "rd"])]
        connectivity = "all"
    else:
        targets.append(dict(name="t1", clock_domain="main", protocol="axi4", data_width=32,
                            len_width=8, id_width=4, attributes="amba", max_outstanding=4,
                            mappings=[dict(base=hex(T1), size="0x10000")]))
        nodes = ["ra", "rd"]
        links = [{"from": "ra", "to": "rd"}]
        exits = [dict(endpoint="t0", node="qa"), dict(endpoint="t1", node="qa")]
        entries = [dict(endpoint="t0", node="ra"), dict(endpoint="t1", node="rd")]
        routes = [dict(initiator="s0", target="t0", request=["qa"], response=["ra", "rd"]),
                  dict(initiator="s0", target="t1", request=["qa"], response=["rd"]),
                  dict(initiator="s0", target="t1", source_path=1, request=["qa"],
                       response=["rd"])]
        connectivity = {"s0": [dict(target="t0", source_path=0), dict(target="t1")]}
    config = dict(
        schema_version=10, name="miss_routes", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                            test_port="test")],
        initiators=[source], targets=targets, connectivity=connectivity,
        topology=dict(
            request=dict(nodes=[dict(name="qa")], links=[],
                         entries=[dict(endpoint="s0", node="qa")], exits=exits),
            response=dict(nodes=[dict(name=n) for n in nodes], links=links, entries=entries,
                          exits=[dict(endpoint="s0", node="rd")]),
            routes=routes),
        output=dict(backend="verilog", language="verilog_2001"))
    if paths:
        config["read_write"] = paths
    return config


def model(config):
    return dict(routes=nlconfig.routes(config), keeps_decerr=nlconfig.keeps_decerr(config),
                targets=nlconfig.targets(config, error_data_zero=True))


def slow_slices(mapping):
    """The valid flags of the slices on the first link of the rc route (map storage), one per
    response path. The map names the nodes by index: ra is node 0 and rc node 2."""
    nets = [[store["resource"], "nl_internal_v" + store["control"][0].removeprefix("value/"), 1]
            for store in mapping["network"]["storage"]
            if store["resource"].startswith("pipeline/") and "node0>" in store["resource"]]
    return [dict(name="slow", kind="storage", nets=nets)] if nets else []


def phase_spans(checker, events):
    """phase number -> (first cycle, end cycle)."""
    index = checker.tape["marks"].index("phase")
    marks = sorted((e["c"], e["v"]) for e in events if e.get("k") == "mk" and e.get("m") == index)
    ends = [c for c, _ in marks[1:]] + [max(e.get("c", 0) for e in events) + 1]
    return {v: (c, end) for (c, v), end in zip(marks, ends)}


def oracle(checker, events, sources, targets):
    """Each word on the rc route holds its slice one cycle (s0 takes R and B at once): the
    cycles a slice is valid count the words."""
    del sources, targets
    if checker.case.get("scenario_args", {}).get("target_flows"):
        return flow_pair_oracle(checker, events)
    if not checker.case.get("scenario_args", {}).get("connected", True):
        return []
    group = next((g for g in checker.bench.get("probes", []) if g["name"] == "slow"), None)
    if group is None or not group["fields"]:
        return [dict(detail="no slice probe on the rc route")]
    held = [e["c"] for e in events if e.get("k") == "pr" and e.get("p") == group["port"]
            for valid in checker._unpack(group, e).values() if valid]
    spans = phase_spans(checker, events)
    out = []
    for phase, want in WORDS.items():
        start, end = spans.get(phase, (0, 0))
        got = sum(start <= c < end for c in held)
        if got != want:
            out.append(dict(detail=f"phase {phase}: {got} words on the rc route, want {want}"))
    return out


def variant(label, connected=True, paths=None):
    config = configuration(connected, paths)
    bfms = [dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
            dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=3)]
    if not connected:
        bfms.append(dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=4))
    entry = dict(label=label, dut=dict(source="cli", config=config), model=model(config),
                 bfms=bfms, scenario_args=dict(connected=connected))
    if connected:
        entry["map_probes"] = dict(groups=slow_slices)
    return entry


CONFIGS = [variant(""), variant("unconnected", connected=False),
           variant("separate", paths="separate"),
           variant("separate_unconnected", connected=False, paths="separate")]
CASE = dict(
    name="miss_routes",
    contracts=["AXI.ERR.UNMAPPED_DECERR"],
    dut=CONFIGS[0]["dut"],
    bfms=CONFIGS[0]["bfms"],
    model=CONFIGS[0]["model"],
    map_probes=dict(groups=slow_slices),
    max_cycles=20000,
    oracle=oracle,
    configs=CONFIGS,
    faults=[],
)


# The configuration "target_flows": routes by flow pair (source flow, target flow).
PAIR_ROUTES = {(0, 0): "rb", (0, 1): "rc", (1, 0): "re"}
# Words per slice and phase: the slices of rb, rc and re.
PAIR_WORDS = {1: (0, 2, 0), 2: (0, 2, 0), 3: (0, 0, 2), 4: (2, 0, 0), 5: (0, 2, 0),
              6: (0, 1, 3)}


def target_flows_configuration():
    target = dict(name="t0", clock_domain="main", protocol="axi4", data_width=32, len_width=8,
                  id_width=4, attributes="amba", max_outstanding=4, flows=2,
                  mappings=[dict(base="0x0", size="0x1000", flow=1),
                            dict(base="0x1000", size="0x1000")])
    source = dict(name="s0", clock_domain="main", protocol="axi4", data_width=32, id_width=2,
                  len_width=8, max_outstanding=4, default_target="t0",
                  route_classes=["even", "odd"], default_class="odd",
                  id_ranges=[dict(first=k, last=k, **{"class": "even"}) for k in (0, 2)])
    nodes = ["ra", "rb", "rc", "rd", "re"]
    links = []
    for middle in ("rb", "rc", "re"):
        links += [{"from": "ra", "to": middle, "pipeline": dict(slice="forward")},
                  {"from": middle, "to": "rd"}]
    routes = [dict(initiator="s0", target="t0", source_path=f, target_path=k, request=["qa"],
                   response=["ra", middle, "rd"]) for (f, k), middle in PAIR_ROUTES.items()]
    for route in routes:
        for key in ("source_path", "target_path"):
            if not route[key]:
                del route[key]
    return dict(
        schema_version=10, name="miss_routes", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                            test_port="test")],
        initiators=[source], targets=[target],
        connectivity={"s0": [dict(target="t0", source_path=0),
                             dict(target="t0", source_path=1, target_path=0)]},
        topology=dict(
            request=dict(nodes=[dict(name="qa")], links=[],
                         entries=[dict(endpoint="s0", node="qa")],
                         exits=[dict(endpoint="t0", node="qa")]),
            response=dict(nodes=[dict(name=n) for n in nodes], links=links,
                          entries=[dict(endpoint="t0", node="ra")],
                          exits=[dict(endpoint="s0", node="rd")]),
            routes=routes),
        output=dict(backend="verilog", language="verilog_2001"))


def flow_pair_oracle(checker, events):
    """The words on the first slice of the routes by rb, rc and re in each phase. The map names
    the nodes by index: ra is node 0, rb node 1, rc node 2 and re node 4."""
    group = next((g for g in checker.bench.get("probes", []) if g["name"] == "slow"), None)
    names = {f"node0>rsp_node{n}": i for i, n in enumerate((1, 2, 4))}
    fields = {i: f for f, _ in (group or {}).get("fields", []) for key, i in names.items()
              if f.endswith(key)}
    if len(fields) != 3:
        return [dict(detail="no slice probes on the rb, rc and re routes")]
    held = {i: [] for i in fields}
    for e in events:
        if e.get("k") == "pr" and e.get("p") == group["port"]:
            row = checker._unpack(group, e)
            for i, field in fields.items():
                if row[field]:
                    held[i].append(e["c"])
    spans = phase_spans(checker, events)
    out = []
    for phase, want in PAIR_WORDS.items():
        start, end = spans.get(phase, (0, 0))
        got = tuple(sum(start <= c < end for c in held[i]) for i in range(3))
        if got != want:
            out.append(dict(detail=f"phase {phase}: words by rb, rc, re {got}, want {want}"))
    return out


def target_flow_phases(t):
    phases = ((1, 1, MISS), (2, 0, MISS + 0x40), (3, 1, 0x1100), (4, 0, 0x1200), (5, 0, 0x100))
    for phase, axid, address in phases:
        t.mark("phase", phase)
        t.read("s0", address, id=axid)
        t.write("s0", address + 0x10, id=axid)
        t.drain(limit=LIMIT, label=f"PAIR_{phase}")
        t.delay(20)
    t.mark("phase", 6)
    behind(t, True, True, 1, base=0x1300)
    t.drain(limit=LIMIT, label="OTHER_WINDOW_FIRST")
    latency(t, ["t0"], 0)
    t.delay(20)


TARGET_FLOWS = target_flows_configuration()
CONFIGS.append(dict(label="target_flows", dut=dict(source="cli", config=TARGET_FLOWS),
                    model=model(TARGET_FLOWS), map_probes=dict(groups=slow_slices),
                    scenario_args=dict(target_flows=True)))


def behind(t, write, miss_write, miss_id, base=0x300):
    """Three one-beat transfers of AxID 1, answered after 30 cycles, then a miss."""
    for target in ("t0", "t1") if base >= T1 else ("t0",):
        t.set(target, "r_lat", 30)
        t.set(target, "b_lat", 30)
    for k in range(3):
        (t.write if write else t.read)("s0", base + 0x10 * k, id=1)
    t.delay(6)
    (t.write if miss_write else t.read)("s0", MISS + 0x100, id=miss_id)


def latency(t, targets, value):
    for target in targets:
        t.set(target, "r_lat", value)
        t.set(target, "b_lat", value)


def connected_phases(t):
    t.mark("phase", 1)
    t.read("s0", MISS, id=1)
    t.drain(limit=LIMIT, label="READ_MISS")
    t.delay(20)
    t.mark("phase", 2)
    t.write("s0", MISS, id=1)
    t.drain(limit=LIMIT, label="WRITE_MISS")
    t.delay(20)
    t.mark("phase", 3)
    t.read("s0", 0x100, id=1)
    t.drain(limit=LIMIT, label="FLOW_READ")
    t.write("s0", 0x100, id=1)
    t.drain(limit=LIMIT, label="FLOW_WRITE")
    t.delay(20)
    for phase, write in ((4, False), (5, True)):
        t.mark("phase", phase)
        latency(t, ["t0"], 30)
        for k in range(3):
            (t.write if write else t.read)("s0", 0x200 + 0x10 * k, id=1)
        t.delay(6)
        t.read("s0", MISS + 0x40, id=3)
        t.write("s0", MISS + 0x80, id=3)
        t.drain(limit=LIMIT, label=f"MISSES_BEHIND_{phase}")
        latency(t, ["t0"], 0)
        t.delay(20)
    for phase, write in ((6, False), (7, True)):
        t.mark("phase", phase)
        behind(t, write, write, 1)
        t.drain(limit=LIMIT, label=f"SAME_ID_MISS_{phase}")
        latency(t, ["t0"], 0)
        t.delay(20)
    t.mark("phase", 8)
    t.ready("s0", "r", "never")
    t.read("s0", MISS + 0x140, id=3)
    t.delay(20)
    t.write("s0", 0x400, id=1)
    t.delay(40)
    t.ready("s0", "r", "always")
    t.drain(limit=LIMIT, label="WRITE_BESIDE_HELD_MISS")
    t.delay(20)
    t.mark("phase", 9)
    t.ready("s0", "b", "never")
    behind(t, False, True, 3)
    t.read("s0", MISS + 0x180, id=3)
    t.delay(120)
    t.ready("s0", "b", "always")
    t.drain(limit=LIMIT, label="TWO_MISSES_HELD_B")
    latency(t, ["t0"], 0)
    t.delay(20)
    t.mark("phase", 10)
    t.read("s0", MISS + 0x1c0, id=0)
    t.write("s0", MISS + 0x200, id=2)
    t.drain(limit=LIMIT, label="FLOW0_MISSES")
    t.delay(20)


def unconnected_phases(t):
    for phase, write in ((1, True), (2, False)):
        t.mark("phase", phase)
        behind(t, write, write, 1, base=T1 + 0x300)
        t.drain(limit=LIMIT, label=f"OTHER_TARGET_FIRST_{phase}")
        latency(t, ["t0", "t1"], 0)
        t.delay(20)
    t.mark("phase", 3)
    t.read("s0", MISS, id=3)
    t.write("s0", MISS + 0x40, id=1)
    t.read("s0", T1 + 0x40, id=1)
    t.drain(limit=LIMIT, label="FLOW1_MISSES")


def scenario(t, seed, connected=True, target_flows=False):
    del seed
    t.reset(8)
    t.delay(8)
    if target_flows:
        target_flow_phases(t)
    elif connected:
        connected_phases(t)
    else:
        unconnected_phases(t)
    t.end()
