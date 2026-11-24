"""Read and write paths in a definition (docs/read-write-paths.md): read_write is shared or
separate, and an explicit shared generates the bytes of the default. With separate paths the
map names the read graphs and the paths of each arbitrated output, and the dependency file
states no transport model and no liveness check. Each option without a form for two paths is
refused with NCL_READ_WRITE_PATHS at its field."""

import copy
import json


def endpoint(name, protocol, **fields):
    return dict(name=name, clock_domain="main", protocol=protocol, data_width=32, **fields)


BASE = dict(
    schema_version=10, name="paths", address_width=32,
    reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
    clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")],
    initiators=[endpoint("s0", "axi4", id_width=2, len_width=8, max_outstanding=2),
                endpoint("s1", "axi4", id_width=2, len_width=8)],
    targets=[endpoint("t0", "axi4", len_width=8, attributes="amba", max_outstanding=2,
                      mappings=[dict(base="0x0", size="0x10000")]),
             endpoint("t1", "apb4", attributes="amba", mappings=[dict(base="0x10000",
                                                                      size="0x1000")])],
    connectivity="all",
    output=dict(backend="verilog", language="verilog_2001"))


def paths(value):
    cfg = copy.deepcopy(BASE)
    cfg["read_write"] = value
    return cfg


def local_paths(separate):
    cfg = copy.deepcopy(BASE)
    for direction in ("request", "response"):
        request = direction == "request"
        source = [{"endpoint": s, "node": "fabric"} for s in ("s0", "s1")]
        target = [{"endpoint": "t0", "node": "fabric"},
                  {"endpoint": "t1", "node": "peripheral"}]
        node = {"name": "fabric", "read_write": "separate" if separate else "shared"}
        start, end = ("fabric", "peripheral") if request else ("peripheral", "fabric")
        link = {"from": start, "to": end}
        if separate and not request:
            link["read_write"] = "separate"
        if separate and request:
            node["arbitration"] = [dict(output="node/peripheral", policy="fixed_priority",
                                        priority=["endpoint/s0", "endpoint/s1"])]
        cfg.setdefault("topology", {})[direction] = dict(
            nodes=[node, {"name": "peripheral"}], links=[link],
            entries=source if request else target, exits=target if request else source)
    return cfg


def mixed_outputs(mapping, out, peers):
    network = mapping["network"]
    assert network["read_write"] == "mixed", "network.read_write is not mixed"
    assert {r["read_write"] for r in network["request_routes"]} == {"mixed", "separate"}, "route modes"
    boundaries = network["path_boundaries"]
    assert {(b["kind"], b["side"]) for b in boundaries} == {
        ("join", "sender"), ("split", "sender")}, "boundary modes"
    graph = json.loads(next(out.glob("*.dependency.json")).read_text())
    assert graph["model_omitted"] == "mixed read and write paths", "mixed dependency model"
    assert not graph["completion"]["applies"], "mixed completion applicability"
    assert all(x["liveness"] == "" for x in graph["excluded"]), "dependency exclusions"
    for boundary in boundaries:
        if boundary["kind"] != "join":
            continue
        resource = boundary["resource"]
        grants = {n["key"].removeprefix(resource) for n in graph["nodes"]
                  if n.get("binding") == resource and n["kind"] == "join.held"}
        assert grants == {"/read", "/write"}, "join grants in dependency graph"
    unmet = [p["subject"] for p in graph["completion"]["unmet"] if p["clause"] == "arb_fair"]
    assert unmet == ["request/fabric>node/peripheral",
                     "request/fabric>node/peripheral/read"], "fixed priority premises"


def separate_outputs(mapping, out, peers):
    network = mapping["network"]
    assert network["read_write"] == "separate", "network.read_write is not separate"
    assert all(n.startswith("wreq_node") for n in network["request_nodes"]), "write request nodes"
    assert all(n.startswith("rreq_node") for n in network["read_request_nodes"]), "read nodes"
    assert all(n.startswith("rrsp_node") for n in network["read_response_nodes"]), "read nodes"
    assert len(network["read_request_routes"]) == len(network["request_routes"]), "read routes"
    nodes = mapping["definition"]["topology"]["request"]["nodes"]
    assert all(n["read_hardware_node"].startswith("rreq_node") for n in nodes), "topology record"
    outputs = mapping["definition"]["contract"]["arbitration"]["outputs"]
    assert sorted({o["carries"] for o in outputs}) == ["reads", "writes"], "arbitrated outputs"
    graph = json.loads(next(out.glob("*.dependency.json")).read_text())
    assert graph["model_omitted"] == "separate read and write paths", "dependency model"
    assert all(x["liveness"] == "" for x in graph["excluded"]), "dependency exclusions"


def edit_json(pattern, change):
    def apply(directory):
        path = next(directory.glob(pattern))
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
    return apply


CASES = [
    dict(name="default", config=BASE, expect="accept", commands=("generate",)),
    dict(name="shared", config=paths("shared"), expect="accept", commands=("generate",),
         same_as="default"),
    dict(name="separate", config=paths("separate"), expect="accept",
         commands=("validate", "generate"), check=separate_outputs,
         mutations=[("merged", edit_json("*.map.json",
                                         lambda m: m["network"].update(read_write="merged")),
                     "network.read_write is not separate"),
                    ("model", edit_json("*.dependency.json",
                                        lambda g: g.update(model_omitted="")),
                     "dependency model")]),
    dict(name="value", config=paths("both"), expect="reject", code="NCL_SCHEMA_TYPE",
         field="read_write"),
    dict(name="local_default", config=local_paths(False), expect="accept", commands=("generate",)),
    dict(name="mixed", config=local_paths(True), expect="accept",
         commands=("validate", "generate"), check=mixed_outputs,
         mutations=[
             ("missing_boundary", edit_json("*.map.json",
                                             lambda m: m["network"]["path_boundaries"].pop()),
              "boundary modes"),
             ("completion", edit_json("*.dependency.json",
                                       lambda g: g["completion"].update(applies=True)),
              "mixed completion applicability"),
             ("join_grant", edit_json("*.dependency.json",
                                     lambda g: next(n for n in g["nodes"]
                                                    if n["kind"] == "join.held").pop("binding")),
              "join grants in dependency graph"),
         ]),
]

for object_type in ("nodes", "links"):
    cfg = local_paths(False)
    cfg["topology"]["request"][object_type][0]["read_write"] = "both"
    CASES.append(dict(name=f"local_value_{object_type}", config=cfg, expect="reject",
                      code="NCL_SCHEMA_TYPE", field=f"topology.request.{object_type}[0].read_write"))

cfg = local_paths(False)
for graph in cfg["topology"].values():
    for node in graph["nodes"]:
        node.pop("read_write", None)
CASES.append(dict(name="local_default_omitted", config=cfg, expect="accept",
                  commands=("generate",), same_as="local_default"))

for mode, peer in (("shared", "local_default"), ("separate", "global_separate")):
    cfg = local_paths(False)
    for graph in cfg["topology"].values():
        for item in graph["nodes"] + graph["links"]:
            item["read_write"] = mode
    cfg["read_write"] = "separate" if mode == "shared" else "shared"
    if mode == "separate":
        global_cfg = local_paths(False)
        global_cfg["read_write"] = "separate"
        for graph in global_cfg["topology"].values():
            for node in graph["nodes"]:
                node.pop("read_write", None)
        CASES.append(dict(name=peer, config=global_cfg, expect="accept", commands=("generate",)))
    CASES.append(dict(name=f"all_local_{mode}", config=cfg, expect="accept",
                      commands=("generate",), same_as=peer))

cfg = local_paths(True)
cfg["initiators"][1]["qos"] = dict(levels=dict(read=1, write=0))
CASES.append(dict(name="mixed_levels", config=cfg, expect="reject",
                  code="NCL_READ_WRITE_PATHS", field="initiators[1].qos"))


def refused(name, field, change):
    cfg = paths("separate")
    change(cfg)
    CASES.append(dict(name="refused_" + name, config=cfg, expect="reject",
                      code="NCL_READ_WRITE_PATHS", field=field))


refused("levels", "initiators[0].qos",
        lambda c: c["initiators"][0].update(qos=dict(levels=dict(read=1, write=0))))
refused("issue_pipe", "initiators[1].pipes.issue",
        lambda c: c["initiators"][1].update(pipes=dict(issue=True)))
refused("arrival_pipe", "targets[0].pipes.arrival",
        lambda c: c["targets"][0].update(pipes=dict(arrival=dict(slice="forward"))))
refused("dispatch_pipe", "targets[0].pipes.dispatch",
        lambda c: c["targets"][0].update(pipes=dict(dispatch=True)))
refused("response_pipe", "targets[1].pipes.response",
        lambda c: c["targets"][1].update(pipes=dict(response=dict(slice="full"))))
refused("wishbone", "targets[1].protocol",
        lambda c: c["targets"][1].update(protocol="wishbone", address_unit="byte",
                                         attributes=None) or c["targets"][1].pop("attributes"))
refused("error_log", "error_logging.logs",
        lambda c: c.update(error_logging=dict(
            registers=dict(name="regs", base="0x100000", size="0x1000", clock_domain="main"),
            logs=[dict(name="errors", watch=["t0"])])))
refused("error_registers", "error_logging.registers",
        lambda c: c.update(error_logging=dict(
            registers=dict(name="regs", base="0x100000", size="0x1000", clock_domain="main"),
            logs=[dict(name="errors", watch=["t0"])])))
