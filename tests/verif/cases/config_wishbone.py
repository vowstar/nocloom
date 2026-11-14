"""External protocol limits and supported Wishbone network combinations."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wishbone import configuration
from nlverif.config import ENDPOINT_GATE_SETTINGS


CASES = []


def add(name, config, code=None):
    CASES.append(dict(name=name, config=config, expect="reject" if code else "accept",
                      **(dict(code=code) if code else {})))


def endpoint_case(name, source, target, role, fields, code=None):
    cfg = configuration(source, target)
    cfg[role][0].update(fields)
    if fields.get("exclusive") == "network":
        cfg["targets"][0]["exclusive_owner_flags"] = ["owner"]
        cfg["initiators"][0]["user_flags"] = dict(owner=0)
        cfg["transport"] = dict(user_flags=["owner"])
    add(name, cfg, code)


for source, target in (("wishbone", "wishbone"), ("wishbone", "axi4"),
                       ("axi4", "wishbone"), ("apb4", "wishbone")):
    prefix = source + "_" + target
    add(prefix, configuration(source, target))
    for label, qos in (
        ("levels", dict(levels=dict(read=1, write=1))),
        ("shaper", dict(shaper=dict(bytes_per_1024_cycles=64, burst_bytes=64))),
        ("scheduler", dict(shaper=dict(bytes_per_1024_cycles=64, burst_bytes=64),
                           scheduler=dict(under_level=3, over_level=0, threshold_percent=50))),
    ):
        endpoint_case(prefix + "_" + label, source, target, "initiators", dict(qos=qos),
                      None if source == "apb4" and label == "shaper" else "NCL_QOS_PROFILE")

for name, role, fields, code in (
    ("source_axqos", "initiators", dict(qos=dict(axqos_map=[0, 1])), "NCL_QOS_MAP"),
    ("source_request_pipe", "initiators", dict(pipes=dict(request=dict(slice="full"))),
     "NCL_ENDPOINT_PIPE"),
    ("source_response_pipe", "initiators", dict(pipes=dict(response=dict(slice="full"))),
     "NCL_ENDPOINT_PIPE"),
    ("source_id_paths", "initiators", dict(route_classes=["a"], default_class="a"),
     "NCL_SOURCE_PATH"),
    ("source_capacity", "initiators", dict(max_outstanding=2), "NCL_AXI4_CAPACITY_COMBINATION"),
    ("source_access", "initiators", dict(access="read_only"), "NCL_WISHBONE_OPTION"),
    ("target_capacity", "targets", dict(max_outstanding=2), "NCL_AXI4_CAPACITY_COMBINATION"),
    ("target_flows", "targets", dict(flows=2), "NCL_WISHBONE_OPTION"),
    ("target_apb_mode", "targets", dict(apb_back_to_back=True), "NCL_TARGET_APB_ACCESS"),
    ("target_attributes", "targets", dict(attributes="amba"), "NCL_TARGET_ATTRIBUTES"),
    ("target_request_pipe", "targets", dict(pipes=dict(request=dict(slice="full"))), "NCL_ENDPOINT_PIPE"),
    ("target_exclusive", "targets", dict(exclusive="target"), "NCL_TARGET_EXCLUSIVE"),
):
    endpoint_case(name, "wishbone", "wishbone", role, fields, code)

for name, fields in (
    ("timeout", dict(timeout=dict(cycles=32))),
    ("fixed_single", dict(fixed_bursts="single")),
    ("read_side_effects", dict(read_side_effects=True)),
    ("read_only", dict(access="read_only")),
    ("write_only", dict(access="write_only")),
    ("network_exclusive", dict(exclusive="network")),
):
    endpoint_case(name, "axi4", "wishbone", "targets", fields)

for gate, settings in ENDPOINT_GATE_SETTINGS.items():
    cfg = configuration("wishbone", "wishbone")
    for endpoint in cfg["initiators"] + cfg["targets"]:
        endpoint["clock_gating"] = settings
    add("clock_gating_" + gate, cfg)
add("async_crossing", configuration("wishbone", "wishbone", "word", True, True))


def logging(source, target, *, whole=False, flags=False, crossed=False):
    cfg = configuration(source, target)
    cfg["initiators"][0]["name"] = "data"
    cfg["initiators"].append(dict(name="cfg", protocol="apb4", data_width=32, clock_domain="main"))
    cfg["connectivity"] = dict(data=["t0"], cfg=["csr"])
    cfg["error_logging"] = dict(
        registers=dict(name="csr", base="0x100000", size="0x1000", clock_domain="main"),
        logs=[dict(name="errors", watch=["t0"], whole_record=whole)])
    if flags:
        cfg["error_logging"]["audit_flags"] = ["audit"]
        for endpoint in cfg["initiators"]:
            endpoint["audit_flags"] = dict(audit="privileged")
    if crossed:
        cfg["clock_domains"][0]["period_ps"] = 10000
        cfg["clock_domains"].append(dict(name="logs", clock_port="clk_logs", reset_port="reset_n",
                                          test_port="test", period_ps=20000))
        cfg["error_logging"]["logs"][0]["clock_domain"] = "logs"
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint=n, node="q") for n in ("cfg", "data")],
                         exits=[dict(endpoint=n, node="q") for n in ("t0", "csr")]),
            response=dict(nodes=[dict(name="p", clock_domain="main")], links=[],
                          entries=[dict(endpoint=n, node="p") for n in ("t0", "csr")],
                          exits=[dict(endpoint=n, node="p") for n in ("cfg", "data")]))
    return cfg


for source, target in (("apb4", "apb4"), ("axi4", "axi4"), ("wishbone", "axi4"),
                       ("apb4", "wishbone"), ("wishbone", "wishbone")):
    for label, options in (("plain", {}), ("whole", dict(whole=True)), ("audit", dict(flags=True))):
        add("log_" + source + "_" + target + "_" + label, logging(source, target, **options),
            "NCL_UNSUPPORTED_TOPOLOGY" if "wishbone" in (source, target) else None)
for source, target in (("apb4", "apb4"), ("apb4", "wishbone"), ("wishbone", "apb4")):
    add("log_crossed_" + source + "_" + target, logging(source, target, crossed=True),
        "NCL_UNSUPPORTED_TOPOLOGY" if "wishbone" in (source, target) else None)

cfg = logging("apb4", "apb4")
cfg["initiators"][1].update(protocol="wishbone", address_unit="byte")
add("service_wishbone_source", cfg, "NCL_UNSUPPORTED_TOPOLOGY")


def check_protocols(mapping, out_dir, peers):
    definition = mapping["definition"]
    assert definition["endpoint_protocols"]["targets"]["t0"] == "wishbone"
    assert "apb_targets" not in definition["contract"], "Wishbone has no APB bus contract"


CASES[0]["check"] = check_protocols
