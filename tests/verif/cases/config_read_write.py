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
]


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
