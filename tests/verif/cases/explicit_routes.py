"""Explicit routes (docs/network.md#routes) through the public CLI: AXI4 sources i0 and i1
and APB4 targets t0 and t1, every pair on a written request route and response route over a
request graph with links both ways between two switches and a response graph whose routes
differ from the request routes. The merged network attaches both sources to one switch ahead
of the diamond, so request routes toward one target split at a switch input by their source;
the response_merged network attaches both targets to one switch ahead of the response diamond,
so response routes toward one source split at a switch input by their target (the response
route path). The repeat network routes i1 to t0 through qb twice, entering it over two links.
The flows network gives i0 two initiator flows (AxID bit 0 clear, else) whose routes to each
target differ in both graphs: request routes part at qa by flow, response routes part at ra by
flow and by target. The target_paths network adds two target flows to each target: t0 an APB4
target with a port set per flow (windows 0x0 and 0x1000, one BFM each), t1 an AXI4 target
(windows 0x2000 and 0x3000) whose AxID carries the flow in bit 0; every pair of initiator flow and
target flow takes its own route, so request routes also part at qa by target flow and response
routes at ra by target flow. The oracle also requires the flow bit of every t1 transaction. The
ahb_flows and lite_flows networks are target_paths with t0 an AHB target, or an AXI4-Lite target
of two packets in flight, of a port set per flow (one BFM each). The conn_flows network is
target_paths with a connectivity matrix by flow pair: i0 flow 0 reaches t0 flow 0 and t1 flow 0,
i0 flow 1 t0 and t1 flow 1, i1 t0 flow 1 and t1; the other flow pairs take no route (and no link
qb > qc), and the random phase sends only connected flow pairs.

Each route runs a single and a four-beat write and read alone, the network drained around it,
with an AxID of its flow. The fixture recipe explicit_routes builds the same definition byte
for byte and names the valid and ready of every switch hop; the oracle requires that the links
a transaction's packets take are exactly the links of the routes this file states for its pair
and flow, request and response. A random phase then runs both sources at once and is judged end
to end. The faults invert both output selections of one switch input (qa's from i0, or from qm
in the merged network, and ra's from t0, or from rm in the response_merged network), so two
routes of that input swap links: every transaction still completes, only the hops differ."""

import random

from nlverif import config as nlconfig

SHAPES = ("diamond", "merged", "response_merged", "repeat", "flows", "target_paths", "ahb_flows",
          "lite_flows", "conn_flows")
# The networks of two flows at each target, and the protocol of their t0.
TARGET_PATHS = {"target_paths": "apb4", "ahb_flows": "ahb_v3", "lite_flows": "axi4_lite",
                "conn_flows": "apb4"}
# The flow pairs (initiator, initiator flow, target, target flow) conn_flows leaves out.
UNCONNECTED = {("i0", 0, "t0", 1), ("i0", 1, "t1", 0), ("i1", 0, "t0", 0)}


def base(shape, target, target_path):
    """The window base of a target flow."""
    if shape in TARGET_PATHS:
        return {"t0": 0x0, "t1": 0x2000}[target] + 0x1000 * target_path
    return {"t0": 0x0, "t1": 0x1000}[target]


def routes(shape):
    """(initiator, flow, target, target flow) -> (request path, response path)."""
    out = {
        ("i0", 0, "t0", 0): (["qa", "qb", "qd"], ["ra", "rc", "rd"]),
        ("i0", 0, "t1", 0): (["qa", "qc", "qd"], ["ra", "rb", "rd"]),
        ("i1", 0, "t0", 0): (["qa", "qc", "qb", "qd"], ["ra", "rb", "rd"]),
        ("i1", 0, "t1", 0): (["qa", "qb", "qc", "qd"], ["ra", "rc", "rb", "rd"]),
    }
    if shape == "merged":
        out = {key: (["qm"] + q, r) for key, (q, r) in out.items()}
    elif shape == "response_merged":
        out = {key: (q, ["rm"] + r) for key, (q, r) in out.items()}
        out[("i0", 0, "t0", 0)] = (out[("i0", 0, "t0", 0)][0], ["rm", "ra", "rc", "rd"])
        out[("i0", 0, "t1", 0)] = (out[("i0", 0, "t1", 0)][0], ["rm", "ra", "rb", "rd"])
        out[("i1", 0, "t0", 0)] = (out[("i1", 0, "t0", 0)][0], ["rm", "ra", "rb", "rd"])
    elif shape == "repeat":
        out[("i1", 0, "t0", 0)] = (["qa", "qb", "qc", "qb", "qd"], out[("i1", 0, "t0", 0)][1])
    elif shape == "flows":
        out = {
            ("i0", 0, "t0", 0): (["qa", "qb", "qd"], ["ra", "rb", "rd"]),
            ("i0", 1, "t0", 0): (["qa", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i0", 0, "t1", 0): (["qa", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i0", 1, "t1", 0): (["qa", "qb", "qd"], ["ra", "rc", "rb", "rd"]),
            ("i1", 0, "t0", 0): (["qa", "qc", "qb", "qd"], ["ra", "rc", "rb", "rd"]),
            ("i1", 0, "t1", 0): (["qa", "qb", "qc", "qd"], ["ra", "rb", "rd"]),
        }
    elif shape in TARGET_PATHS:
        out = {
            ("i0", 0, "t0", 0): (["qa", "qb", "qd"], ["ra", "rb", "rd"]),
            ("i0", 0, "t0", 1): (["qa", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i0", 1, "t0", 0): (["qa", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i0", 1, "t0", 1): (["qa", "qb", "qd"], ["ra", "rb", "rd"]),
            ("i0", 0, "t1", 0): (["qa", "qc", "qb", "qd"], ["ra", "rc", "rb", "rd"]),
            ("i0", 0, "t1", 1): (["qa", "qb", "qd"], ["ra", "rb", "rd"]),
            ("i0", 1, "t1", 0): (["qa", "qb", "qc", "qd"], ["ra", "rb", "rd"]),
            ("i0", 1, "t1", 1): (["qa", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i1", 0, "t0", 0): (["qa", "qb", "qc", "qd"], ["ra", "rc", "rd"]),
            ("i1", 0, "t0", 1): (["qa", "qc", "qb", "qd"], ["ra", "rb", "rd"]),
            ("i1", 0, "t1", 0): (["qa", "qc", "qd"], ["ra", "rb", "rd"]),
            ("i1", 0, "t1", 1): (["qa", "qb", "qd"], ["ra", "rc", "rb", "rd"]),
        }
    if shape == "conn_flows":
        out = {k: v for k, v in out.items() if k not in UNCONNECTED}
        out[("i0", 1, "t0", 1)] = (["qa", "qc", "qd"], ["ra", "rc", "rd"])
    return out


def connectivity(shape):
    """The connectivity matrix: all, or conn_flows' by flow pair."""
    if shape != "conn_flows":
        return "all"
    return {"i0": [{"target": "t0", "source_path": 0, "target_path": 0},
                   {"target": "t0", "source_path": 1},
                   {"target": "t1", "source_path": 0},
                   {"target": "t1", "source_path": 1, "target_path": 1}],
            "i1": [{"target": "t0", "target_path": 1}, "t1"]}


def configuration(shape):
    merged, response_merged = shape == "merged", shape == "response_merged"
    q = "qm" if merged else "qa"
    r = "rm" if response_merged else "ra"
    request = dict(
        nodes=[dict(name=n) for n in ["qa", "qb", "qc", "qd"] + (["qm"] if merged else [])],
        links=[{"from": a, "to": b} for a, b in
               [("qa", "qb"), ("qa", "qc"), ("qc", "qb"), ("qb", "qd"), ("qc", "qd")]
               + ([("qb", "qc")] if shape != "conn_flows" else [])
               + ([("qm", "qa")] if merged else [])],
        entries=[dict(endpoint=i, node=q) for i in ("i0", "i1")],
        exits=[dict(endpoint=t, node="qd") for t in ("t0", "t1")])
    response = dict(
        nodes=[dict(name=n) for n in ["ra", "rb", "rc", "rd"] + (["rm"] if response_merged else [])],
        links=[{"from": a, "to": b} for a, b in
               [("ra", "rb"), ("ra", "rc"), ("rb", "rd"), ("rc", "rd"), ("rc", "rb")]
               + ([("rm", "ra")] if response_merged else [])],
        entries=[dict(endpoint=t, node=r) for t in ("t0", "t1")],
        exits=[dict(endpoint=i, node="rd") for i in ("i0", "i1")])
    initiators = [dict(name=i, clock_domain="main", protocol="axi4", data_width=32, id_width=2,
                       len_width=8) for i in ("i0", "i1")]
    if shape == "flows" or shape in TARGET_PATHS:
        initiators[0].update(route_classes=["even", "odd"], default_class="odd",
                             id_ranges=[dict(first=k, last=k, **{"class": "even"}) for k in (0, 2)])
    rows = []
    for (i, f, t, g), (qp, rp) in routes(shape).items():
        row = dict(initiator=i, target=t, request=qp, response=rp)
        if f:
            row["source_path"] = f
        if g:
            row["target_path"] = g
        rows.append(row)
    targets = [dict(name=t, clock_domain="main", protocol="apb4", data_width=32,
                    address_width=16, attributes="amba",
                    mappings=[{"base": hex(base(shape, t, 0)), "size": "0x1000"}])
               for t in ("t0", "t1")]
    if shape in TARGET_PATHS:
        targets[0].update(protocol=TARGET_PATHS[shape], flows=2,
                          mappings=[{"base": "0x0", "size": "0x1000"},
                                    {"base": "0x1000", "size": "0x1000", "flow": 1}])
        if shape == "lite_flows":
            targets[0]["max_outstanding"] = 2
        targets[1] = dict(name="t1", clock_domain="main", protocol="axi4", data_width=32,
                          attributes="amba", id_width=3, len_width=8, flows=2,
                          mappings=[{"base": "0x2000", "size": "0x1000"},
                                    {"base": "0x3000", "size": "0x1000", "flow": 1}])
    return {
        "schema_version": 10,
        "name": "explicit_routes" if shape == "diamond" else "explicit_routes_" + shape,
        "address_width": 32, "reset_ports": [{"name": "reset_n"}], "test_ports": [{"name": "test"}],
        "clock_domains": [{"name": "main", "clock_port": "clk_main", "reset_port": "reset_n",
                           "test_port": "test"}],
        "initiators": initiators,
        "targets": targets,
        "connectivity": connectivity(shape),
        "topology": dict(request=request, response=response, routes=rows),
        "output": {"backend": "verilog", "language": "verilog_2001"}}


def links(shape, key):
    """The links of a route: entry, node to node and exit, both directions."""
    source, _, target, _ = key
    q, r = routes(shape)[key]
    chain = [source] + q + [target], [target] + r + [source]
    return {(g, a, b) for g, nodes in zip(("request", "response"), chain)
            for a, b in zip(nodes, nodes[1:])}


def cases(shape):
    """case number -> (route key, direction, beats, AxID): an ID of the route's flow."""
    out = {}
    for key in routes(shape):
        for direction in ("w", "r"):
            for beats in (1, 4):
                k = len(out) + 1
                flows = (shape == "flows" or shape in TARGET_PATHS) and key[0] == "i0"
                ident = 2 * (k % 2) + key[1] if flows else k % 4
                out[k] = (key, direction, beats, ident)
    return out


def case_windows(checker, events):
    """case number -> (start, end) from the case marks."""
    if "case" not in checker.tape["marks"]:
        return {}
    index = checker.tape["marks"].index("case")
    starts = [(e["v"], e["t"]) for e in events if e.get("k") == "mk" and e.get("m") == index]
    end = max((e.get("t", 0) for e in events), default=0) + 1
    return {v: (t, starts[k + 1][1] if k + 1 < len(starts) else end)
            for k, (v, t) in enumerate(starts)}


def strip(key):
    return key.split("/", 1)[1] if "/" in key else key


def oracle(checker, events, sources, targets):
    """Every serial case takes exactly the links of its route."""
    shape = checker.case.get("scenario_args", {}).get("shape", "diamond")
    taken = []
    for group in checker.bench.get("probes", []):
        if group.get("kind") != "hops":
            continue
        for e in events:
            if e.get("k") != "pr" or e.get("p") != group["port"]:
                continue
            row = checker._unpack(group, e)
            if row is None:
                return [dict(detail="hop probe X", event=e.get("_n"))]
            for k, (into, node, onward) in enumerate(group["hops"]):
                if row[f"valid{k}"] and row[f"ready{k}"]:
                    taken.append((e["t"], group["graph"], strip(into), node, strip(onward)))
    if not taken:
        return [dict(detail="no hop probes")]
    out, seen = [], set()
    serial = cases(shape)
    for k, (lo, hi) in sorted(case_windows(checker, events).items()):
        if k not in serial:
            continue
        key = serial[k][0]
        got = set()
        for at, graph, into, node, onward in taken:
            if lo <= at < hi:
                got |= {(graph, into, node), (graph, node, onward)}
        want = links(shape, key)
        seen.add(key)
        if got != want:
            out.append(dict(detail="links of the case differ from its route", case=k,
                            route=list(key), missing=sorted(want - got),
                            extra=sorted(got - want)))
    if len(seen) != len(routes(shape)):
        out.append(dict(detail="routes not covered", got=sorted(seen)))
    # The AXI4 target's AxID carries its target flow in bit 0: the window of the source address.
    if shape in TARGET_PATHS:
        for tt in targets:
            if tt.port == "t1" and tt.owner is not None and tt.id & 1 != tt.owner.addr >> 12 & 1:
                out.append(dict(detail="target flow bit of the AXI ID", id=tt.id,
                                address=tt.owner.addr))
    return out[:4]


def target_path_bfms(shape):
    """The target BFMs of a network of target flows: one per t0 port set, one AXI4 t1."""
    kind = {"apb4": dict(kind="apb_slave", dialect=4), "ahb_v3": dict(kind="ahb_slave"),
            "axi4_lite": dict(kind="axi_slave", lite=True)}[TARGET_PATHS[shape]]
    return [dict(name=f"t0_{k}", endpoint="target:t0", interface=k, salt=k + 1, **kind)
            for k in range(2)] + [
        dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=3)]


def network(shape):
    cfg = configuration(shape)
    # Hardware switch identities follow the configured names: qa..qd, qm and ra..rd, rm.
    request = "node/req_node4" if shape == "merged" else "endpoint/i0"
    response = "node/rsp_node4" if shape == "response_merged" else "endpoint/t0"
    inputs = dict(request=("req", "req_node0", request), response=("rsp", "rsp_node0", response))
    faults = [dict(id=f"swap_{d}", component="network", op="replace", hits=2,
                   at={"glob": f"graph/{g}/{node}>node/*.hop/{key}.select"},
                   **{"with": {"op": "BitNot", "args": [{"self": True}]}},
                   expect=["ORACLE"], contract="AXI.ROUTE.EXPLICIT")
              for d, (g, node, key) in inputs.items()]
    return dict(
        label=shape, dut=dict(source="cli", config=cfg),
        fault_recipe=dict(recipe="explicit_routes", params=dict(shape=shape)),
        scenario_args=dict(shape=shape), faults=faults,
        bfms=[dict(name=i, kind="axi_master", endpoint=f"initiator:{i}") for i in ("i0", "i1")]
        + (target_path_bfms(shape) if shape in TARGET_PATHS else
           [dict(name=t, kind="apb_slave", endpoint=f"target:{t}", dialect=4, salt=k + 1)
            for k, t in enumerate(("t0", "t1"))]),
        model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                   targets=nlconfig.targets(cfg, error_data_zero=True)))


CASE = dict(
    name="explicit_routes",
    contracts=["AXI.ROUTE.EXPLICIT", "AXI.ROUTE.LOCAL_OFFSET"],
    probes=dict(instance={"network": "dut"}),
    configs=[network(shape) for shape in SHAPES],
    negative_faults_all=True,
    oracle=oracle,
    seed=20261026,
    max_cycles=200000,
)
CASE.update({k: v for k, v in CASE["configs"][0].items() if k != "label"})


def scenario(t, seed, shape="diamond", denied=False):
    rnd = random.Random(seed)
    t.reset(8)
    t.delay(4)
    for k, ((source, _, target, target_path), direction, beats, ident) in cases(shape).items():
        t.mark("case", k)
        address = base(shape, target, target_path) + 0x40 * k
        if direction == "w":
            t.write(source, address, beats=beats, id=ident)
        else:
            t.read(source, address, beats=beats, id=ident)
        t.drain(limit=4000, label="SERIAL")
        t.delay(4)
    # Both sources at once on every pair, judged end to end.
    t.mark("case", 0)
    for step in range(48):
        for source in ("i0", "i1"):
            if shape == "conn_flows":
                # A connected flow pair: an AxID, then a target flow its flow reaches.
                ident = rnd.randrange(4)
                flow = ident & 1 if source == "i0" else 0
                _, _, target, target_path = rnd.choice(
                    sorted(k for k in routes(shape) if k[:2] == (source, flow)))
            else:
                target = rnd.choice(("t0", "t1"))
                ident = None
            target_path = (target_path if shape == "conn_flows" else
                           rnd.randrange(2) if shape in TARGET_PATHS else 0)
            beats = rnd.randrange(4) + 1
            address = (base(shape, target, target_path) + 0x800 + 0x100 * (source == "i1")
                       + 4 * rnd.randrange(32))
            if ident is None:
                ident = rnd.randrange(4) if rnd.randrange(2) else -1 - rnd.randrange(4)
            elif rnd.randrange(2):
                ident = -1 - ident
            if ident >= 0:
                t.write(source, address, beats=beats, id=ident)
            else:
                t.read(source, address, beats=beats, id=-1 - ident)
        if step % 8 == 7:
            t.drain(limit=20000, label="CONCURRENT")
    t.drain(limit=20000, label="CONCURRENT")
    if denied:
        for source, flow, target, target_path in sorted(UNCONNECTED):
            ident = flow if source == "i0" else 0
            address = base(shape, target, target_path) + 0x40
            t.write(source, address, id=ident, resp=3, target="none")
            t.read(source, address, id=ident, resp=3, target="none")
        t.drain(limit=4000, label="DENIED_FLOW_PAIRS")
    t.end()
