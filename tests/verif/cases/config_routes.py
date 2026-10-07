"""Configuration checks of explicit routes and initiator flows (docs/network.md#routes): the
map records the routes as written and every pair's route in generated switches, with the flows
that take it; the order of route entries does not change a byte; a path absent from an entry is
the unique route of its graph; a cycle of links that no route continues around is accepted, as
are routes that split a switch input toward one initiator and a route that passes a switch
twice over two links, while routes that chain a cycle (one route taking a link twice, or the
routes of two flows of one initiator together), a path over a node of the other direction,
every malformed entry and every malformed flow list are rejected with their codes. Target flows
(docs/network.md#endpoint-flows) take their own routes the same way, their union of routes obeys the same
wait rule, and malformed target flows, their routes and their ID widths are rejected. A
connectivity matrix by flow pair (conn_flows) records the connected flow pairs of every pair and
routes only those; a flow out of range, a flow pair listed twice and a route of a flow pair it
leaves out are rejected."""

import copy
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("nlv_config_routes_case", HERE / "explicit_routes.py")
ROUTES = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ROUTES)
BASE = ROUTES.configuration("diamond")
FLOWS = ROUTES.configuration("flows")
TARGET_PATHS = ROUTES.configuration("target_paths")
CONN_FLOWS = ROUTES.configuration("conn_flows")
PLAIN = {(i, t): paths for (i, _, t, _), paths in ROUTES.routes("diamond").items()}


def edited(edit, cfg=BASE):
    cfg = copy.deepcopy(cfg)
    edit(cfg)
    return cfg


def route(cfg, source, target, flow=0, target_path=0):
    return next(r for r in cfg["topology"]["routes"]
                if r["initiator"] == source and r["target"] == target
                and r.get("source_path", 0) == flow and r.get("target_path", 0) == target_path)


def set_path(i, t, **fields):
    return lambda cfg: route(cfg, i, t).update(fields)


def rejected(name, edit, code, cfg=BASE):
    return dict(name=name, config=edited(edit, cfg), expect="reject", code=code)


def classes_of(**fields):
    return lambda cfg: cfg["initiators"][0].update(fields)


def map_check(mapping, out, peers):
    topology = mapping["definition"]["topology"]
    rows = {(r["initiator"], r["target"]): (r["request"], r["response"])
            for r in topology["routes"]}
    assert rows == {pair: tuple(paths) for pair, paths in PLAIN.items()}, "routes as written"
    names = {}
    for direction in ("request", "response"):
        names.update({(direction, n["hardware_node"]): n["name"]
                      for n in topology[direction]["nodes"]})
    for direction, key in (("request", "request_routes"), ("response", "response_routes")):
        for r in mapping["network"][key]:
            pair = (r["entry"], r["exit"]) if direction == "request" else (r["exit"], r["entry"])
            got = [names[(direction, n)] for n in r["hardware_nodes"]]
            want = PLAIN[pair][0 if direction == "request" else 1]
            assert got == want, f"{direction} route of {pair}"


def flows_check(mapping, out, peers, shape="flows"):
    """Every flow pair takes its written route, and the map names the flows of each route."""
    topology = mapping["definition"]["topology"]
    rows = {(r["initiator"], r.get("source_path", 0), r["target"], r.get("target_path", 0)):
            (r["request"], r["response"]) for r in topology["routes"]}
    assert rows == {key: tuple(paths) for key, paths in ROUTES.routes(shape).items()}, \
        "routes as written"
    assert mapping["definition"]["endpoint_transactions"]["initiators"]["i0"]["route_classes"] == \
        ["even", "odd"], "initiator classes"
    names = {}
    for direction in ("request", "response"):
        names.update({(direction, n["hardware_node"]): n["name"]
                      for n in topology[direction]["nodes"]})
    classes = mapping["definition"]["endpoint_transactions"]["initiators"]
    for direction, table in (("request", "request_routes"), ("response", "response_routes")):
        seen = set()
        for r in mapping["network"][table]:
            i, t = (r["entry"], r["exit"]) if direction == "request" else (r["exit"], r["entry"])
            got = [names[(direction, n)] for n in r["hardware_nodes"]]
            count = max(1, len(classes.get(i, {}).get("route_classes", [])))
            for flow in r.get("source_paths", range(count)):
                key = (i, flow, t, r.get("target_path", 0))
                assert key not in seen, f"{direction} duplicate route class"
                seen.add(key)
                want = ROUTES.routes(shape)[key][0 if direction == "request" else 1]
                assert got == want, f"{direction} route of a flow"
        assert seen == set(ROUTES.routes(shape)), f"{direction} route class coverage"


def target_paths_check(mapping, out, peers):
    """Every flow pair takes its written route; the map records the target flows."""
    flows_check(mapping, out, peers, "target_paths")
    targets = mapping["definition"]["endpoint_transactions"]["targets"]
    assert targets["t0"]["flows"] == 2 and targets["t1"]["flows"] == 2, "target flows"
    flows = {(row["name"], int(row["begin"])): row.get("flow", 0)
             for row in mapping["definition"]["targets"]}
    assert flows == {("t0", 0): 0, ("t0", 0x1000): 1, ("t1", 0x2000): 0, ("t1", 0x3000): 1}, \
        "mapping flows"


def conn_flows_check(mapping, out, peers):
    """Only the connected flow pairs take routes; the map lists them per pair."""
    flows_check(mapping, out, peers, "conn_flows")
    pairs = {(r["source"], r["target"]): sorted(map(tuple, r.get("flow_pairs", [])))
             for r in mapping["definition"]["connectivity"]}
    assert pairs == {("i0", "t0"): [(0, 0), (1, 0), (1, 1)], ("i0", "t1"): [(0, 0), (0, 1), (1, 1)],
                     ("i1", "t0"): [(0, 1)], ("i1", "t1"): [(0, 0), (0, 1)]}, "flow pairs"


def drop_flow_pairs(out):
    """Mutation: the map's connectivity loses its flow pairs."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    for r in mapping["definition"]["connectivity"]:
        r.pop("flow_pairs", None)
    path.write_text(json.dumps(mapping))


def drop_target_path(out):
    """Mutation: the map's routes lose their target flows."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    for r in mapping["network"]["request_routes"] + mapping["network"]["response_routes"]:
        r.pop("target_path", None)
    path.write_text(json.dumps(mapping))


def target(name, **fields):
    """Edit: fields of a target."""
    return lambda cfg: next(t for t in cfg["targets"] if t["name"] == name).update(fields)


def target_path_union_cycle(cfg):
    # The two flows of t0 from i1 take qb > qc and qe > qf in opposite orders: the routes of the
    # two target flows together continue around qb > qc > qe > qf > qb.
    request = cfg["topology"]["request"]
    request["nodes"] += [dict(name="qe"), dict(name="qf")]
    request["links"] += [{"from": a, "to": b} for a, b in (
        ("qc", "qe"), ("qe", "qf"), ("qf", "qd"), ("qa", "qe"), ("qf", "qb"))]
    route(cfg, "i1", "t0", 0, 0)["request"] = ["qa", "qb", "qc", "qe", "qf", "qd"]
    route(cfg, "i1", "t0", 0, 1)["request"] = ["qa", "qe", "qf", "qb", "qc", "qd"]


def drop_flow(out):
    """Mutation: the map's request routes lose their flows."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    for r in mapping["network"]["request_routes"]:
        r.pop("source_paths", None)
    path.write_text(json.dumps(mapping))


def drop_default_return(out):
    """Mutation: the default class loses one response route."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    for route in mapping["network"]["response_routes"]:
        if route["exit"] == "i0" and 1 in route.get("source_paths", []):
            route["source_paths"].remove(1)
            break
    else:
        raise AssertionError("No default route to mutate")
    path.write_text(json.dumps(mapping))


def drop_route(out):
    """Mutation: the map loses one written route."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    mapping["definition"]["topology"]["routes"].pop()
    path.write_text(json.dumps(mapping))


def reverse_hops(out):
    """Mutation: the map's request route of i0 to t1 takes the other branch."""
    import json
    path = next(p for p in out.iterdir() if p.name.endswith(".map.json"))
    mapping = json.loads(path.read_text())
    for r in mapping["network"]["request_routes"]:
        if (r["entry"], r["exit"]) == ("i0", "t1"):
            r["hardware_nodes"][1] = "req_node1"
    path.write_text(json.dumps(mapping))


def reordered(cfg):
    cfg["topology"]["routes"].reverse()
    cfg["topology"]["request"]["links"].reverse()


def single_request_node(cfg):
    """One request switch: every request path is unique, the entries give response paths only."""
    cfg["topology"]["request"] = dict(
        nodes=[dict(name="q")], links=[],
        entries=[dict(endpoint=i, node="q") for i in ("i0", "i1")],
        exits=[dict(endpoint=t, node="q") for t in ("t0", "t1")])
    for r in cfg["topology"]["routes"]:
        r.pop("request")


def flow_cycle(cfg):
    # Links qb > qc and qc > qb both carry routes, but no route continues around them.
    for t in ("t0", "t1"):
        route(cfg, "i0", t)["request"] = ["qa", "qb", "qc", "qd"]
        route(cfg, "i1", t)["request"] = ["qa", "qc", "qb", "qd"]


def channel_cycle(cfg):
    # i0 takes qb > qc before qe > qf, i1 qe > qf before qb > qc: each path is simple, the routes
    # together continue around qb > qc > qe > qf > qb.
    request = cfg["topology"]["request"]
    request["nodes"] += [dict(name="qe"), dict(name="qf")]
    request["links"] = [{"from": a, "to": b} for a, b in (
        ("qa", "qb"), ("qb", "qc"), ("qc", "qe"), ("qe", "qf"), ("qf", "qd"), ("qa", "qe"),
        ("qf", "qb"), ("qc", "qd"))]
    for t in ("t0", "t1"):
        route(cfg, "i0", t)["request"] = ["qa", "qb", "qc", "qe", "qf", "qd"]
        route(cfg, "i1", t)["request"] = ["qa", "qe", "qf", "qb", "qc", "qd"]


def joint_cycle(cfg):
    # A request path over response switches and a response path over request switches, as one
    # switch carrying both directions would: every node belongs to one direction.
    for t in ("t0", "t1"):
        route(cfg, "i0", t).update(request=["qa", "qb", "qc", "rb", "rc", "qd"],
                                   response=["ra", "rb", "rc", "qb", "qc", "rd"])


def flow_union_cycle(cfg):
    # The two flows of i0 to t0 take qb > qc and qe > qf in opposite orders: each path is
    # simple, the routes of the two flows together continue around qb > qc > qe > qf > qb.
    request = cfg["topology"]["request"]
    request["nodes"] += [dict(name="qe"), dict(name="qf")]
    request["links"] += [{"from": a, "to": b} for a, b in (
        ("qc", "qe"), ("qe", "qf"), ("qf", "qd"), ("qa", "qe"), ("qf", "qb"))]
    route(cfg, "i0", "t0", 0)["request"] = ["qa", "qb", "qc", "qe", "qf", "qd"]
    route(cfg, "i0", "t0", 1)["request"] = ["qa", "qe", "qf", "qb", "qc", "qd"]


def flow_crossing(cfg):
    # The two flows of i0 to t0 cross qb > qc and qc > qb, but no route continues around them.
    route(cfg, "i0", "t0", 0)["request"] = ["qa", "qb", "qc", "qd"]
    route(cfg, "i0", "t0", 1)["request"] = ["qa", "qc", "qb", "qd"]


def selective(cfg):
    cfg["connectivity"] = {"i0": ["t0", "t1"], "i1": ["t1"]}


CASES = [
    dict(name="diamond", config=BASE, expect="accept", check=map_check,
         mutations=[("drop_route", drop_route, "routes as written"),
                    ("reverse_hops", reverse_hops, "request route of ('i0', 't1')")]),
    dict(name="reordered", config=edited(reordered), expect="accept", same_as="diamond"),
    dict(name="merged", config=ROUTES.configuration("merged"), expect="accept"),
    dict(name="response_merged", config=ROUTES.configuration("response_merged"),
         expect="accept"),
    dict(name="repeat", config=ROUTES.configuration("repeat"), expect="accept"),
    dict(name="flows", config=FLOWS, expect="accept", check=flows_check,
         mutations=[("drop_flow", drop_flow, "request route of a flow"),
                    ("drop_default_return", drop_default_return, "response route class coverage")]),
    dict(name="flow_crossing", config=edited(flow_crossing, FLOWS), expect="accept"),
    rejected("flow_union_cycle", flow_union_cycle, "NCL_SWITCH_ROUTE_CYCLE", FLOWS),
    rejected("link_twice", set_path("i1", "t0", request=["qa", "qb", "qc", "qb", "qc", "qd"]),
             "NCL_SWITCH_ROUTE_CYCLE"),
    rejected("flow_missing_route", lambda cfg: cfg["topology"]["routes"].pop(1),
             "NCL_SWITCH_ROUTE_AMBIGUOUS", FLOWS),
    rejected("flow_out_of_range", lambda cfg: route(cfg, "i0", "t0", 1).update(source_path=2),
             "NCL_ROUTE_REFERENCE", FLOWS),
    rejected("flow_of_single", set_path("i1", "t0", source_path=1), "NCL_ROUTE_REFERENCE"),
    rejected("flow_duplicate", lambda cfg: cfg["topology"]["routes"].append(
        dict(initiator="i0", source_path=1, target="t0", request=["qa", "qc", "qd"])),
             "NCL_ROUTE_DUPLICATE", FLOWS),
    dict(name="target_paths", config=TARGET_PATHS, expect="accept", check=target_paths_check,
         mutations=[("drop_target_path", drop_target_path, "request duplicate route class")]),
    rejected("target_path_union_cycle", target_path_union_cycle, "NCL_SWITCH_ROUTE_CYCLE",
             TARGET_PATHS),
    rejected("target_path_missing_route",
             lambda cfg: cfg["topology"]["routes"].remove(route(cfg, "i0", "t0", 0, 1)),
             "NCL_SWITCH_ROUTE_AMBIGUOUS", TARGET_PATHS),
    rejected("target_path_out_of_range",
             lambda cfg: route(cfg, "i0", "t0", 0, 1).update(target_path=2),
             "NCL_ROUTE_REFERENCE", TARGET_PATHS),
    rejected("target_path_of_single", set_path("i1", "t0", target_path=1), "NCL_ROUTE_REFERENCE"),
    rejected("target_path_duplicate", lambda cfg: cfg["topology"]["routes"].append(
        dict(initiator="i1", target="t0", target_path=1, request=["qa", "qc", "qb", "qd"])),
             "NCL_ROUTE_DUPLICATE", TARGET_PATHS),
    dict(name="conn_flows", config=CONN_FLOWS, expect="accept", check=conn_flows_check,
         mutations=[("drop_flow_pairs", drop_flow_pairs, "flow pairs")]),
    rejected("conn_flow_out_of_range", lambda cfg: cfg["connectivity"]["i1"][0].update(
        target_path=2), "NCL_CONNECTIVITY_REFERENCE", CONN_FLOWS),
    rejected("conn_source_path_out_of_range", lambda cfg: cfg["connectivity"]["i1"].append(
        {"target": "t0", "source_path": 1, "target_path": 0}), "NCL_CONNECTIVITY_REFERENCE",
             CONN_FLOWS),
    rejected("conn_flow_pair_twice", lambda cfg: cfg["connectivity"]["i0"].append(
        {"target": "t0", "target_path": 0}), "NCL_CONNECTIVITY_DUPLICATE", CONN_FLOWS),
    rejected("conn_route_unconnected", lambda cfg: cfg["topology"]["routes"].append(
        dict(initiator="i1", target="t0", request=["qa", "qb", "qd"])), "NCL_ROUTE_REFERENCE",
             CONN_FLOWS),
    rejected("conn_unknown_field", lambda cfg: cfg["connectivity"]["i1"][0].update(flow=1),
             "NCL_SCHEMA_UNKNOWN_FIELD", CONN_FLOWS),
    rejected("target_paths_zero", target("t0", flows=0), "NCL_TARGET_PATH", TARGET_PATHS),
    rejected("target_paths_too_many", target("t0", flows=33), "NCL_TARGET_PATH", TARGET_PATHS),
    rejected("target_path_without_mapping", target("t1", flows=3), "NCL_TARGET_PATH",
             TARGET_PATHS),
    rejected("mapping_flow_out_of_range", lambda cfg: cfg["targets"][0]["mappings"][1].update(
        flow=2), "NCL_TARGET_PATH", TARGET_PATHS),
    dict(name="target_paths_lite", config=edited(target("t0", protocol="axi4_lite"), TARGET_PATHS),
         expect="accept"),
    dict(name="target_paths_ahb", config=edited(target("t0", protocol="ahb_v3"), TARGET_PATHS),
         expect="accept"),
    rejected("target_paths_no_id", target("t1", id_width=0), "NCL_TARGET_PATH", TARGET_PATHS),
    rejected("target_paths_id_width", target("t1", id_width=0),
             "NCL_TARGET_PATH", TARGET_PATHS),
    rejected("mapping_flow_unknown_field", lambda cfg: cfg["targets"][0]["mappings"][1].update(
        flows=1), "NCL_SCHEMA_UNKNOWN_FIELD", TARGET_PATHS),
    rejected("classes_overlap", classes_of(route_classes=["a", "b"], default_class="b",
             id_ranges=[dict(first=0, last=2, **{"class": "a"}),
                        dict(first=2, last=3, **{"class": "b"})]), "NCL_SOURCE_PATH"),
    rejected("classes_unknown_default", classes_of(route_classes=["a"], default_class="b"),
             "NCL_SOURCE_PATH"),
    rejected("classes_missing_default", classes_of(route_classes=["a"]), "NCL_SOURCE_PATH"),
    rejected("classes_unreachable", classes_of(route_classes=["a", "b"], default_class="b",
             id_ranges=[dict(first=0, last=3, **{"class": "a"})]), "NCL_SOURCE_PATH"),
    rejected("classes_endpoint", classes_of(route_classes=["a"], default_class="a",
             id_ranges=[dict(first=0, last=4, **{"class": "a"})]), "NCL_SOURCE_PATH"),
    rejected("classes_reversed", classes_of(route_classes=["a"], default_class="a",
             id_ranges=[dict(first=2, last=1, **{"class": "a"})]), "NCL_SOURCE_PATH"),
    rejected("classes_unknown", classes_of(route_classes=["a"], default_class="a",
             id_ranges=[dict(first=0, last=1, **{"class": "b"})]), "NCL_SOURCE_PATH"),
    rejected("classes_duplicate", classes_of(route_classes=["a", "a"], default_class="a"),
             "NCL_SOURCE_PATH"),
    rejected("classes_no_id", classes_of(id_width=0, route_classes=["a", "b"], default_class="a"),
             "NCL_SOURCE_PATH"),
    dict(name="classes_no_id_single", config=edited(classes_of(
         id_width=0, route_classes=["a"], default_class="a")), expect="accept"),
    dict(name="response_paths_only", config=edited(single_request_node), expect="accept"),
    dict(name="flow_cycle", config=edited(flow_cycle), expect="accept"),
    rejected("channel_cycle", channel_cycle, "NCL_SWITCH_ROUTE_CYCLE"),
    rejected("joint_cycle", joint_cycle, "NCL_ROUTE_REFERENCE"),
    rejected("missing_link", set_path("i0", "t0", request=["qa", "qd"]), "NCL_ROUTE_PATH"),
    rejected("wrong_start", set_path("i0", "t0", request=["qb", "qd"]), "NCL_ROUTE_PATH"),
    rejected("wrong_end", set_path("i0", "t0", response=["ra", "rc"]), "NCL_ROUTE_PATH"),
    rejected("unknown_node", set_path("i0", "t0", request=["qa", "qz", "qd"]),
             "NCL_ROUTE_REFERENCE"),
    rejected("unknown_initiator", set_path("i0", "t0", initiator="i9"), "NCL_ROUTE_REFERENCE"),
    rejected("unknown_target", set_path("i0", "t0", target="t9"), "NCL_ROUTE_REFERENCE"),
    rejected("unconnected_pair", selective, "NCL_ROUTE_REFERENCE"),
    rejected("duplicate", lambda cfg: cfg["topology"]["routes"].append(
        dict(initiator="i0", target="t0", request=["qa", "qb", "qd"])), "NCL_ROUTE_DUPLICATE"),
    rejected("no_path", lambda cfg: [route(cfg, "i0", "t0").pop(k)
                                     for k in ("request", "response")],
             "NCL_SCHEMA_MISSING_FIELD"),
    rejected("empty_path", set_path("i0", "t0", request=[]), "NCL_ROUTE_PATH"),
    rejected("unknown_field", set_path("i0", "t0", source="i0"), "NCL_SCHEMA_UNKNOWN_FIELD"),
    rejected("ambiguous", lambda cfg: cfg["topology"]["routes"].pop(0),
             "NCL_SWITCH_ROUTE_AMBIGUOUS"),
    rejected("unused_link", lambda cfg: cfg["topology"]["request"]["links"].append(
        {"from": "qd", "to": "qa"}), "NCL_SWITCH_UNUSED_OBJECT"),
]
