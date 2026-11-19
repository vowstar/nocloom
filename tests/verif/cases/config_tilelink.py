"""TileLink endpoint fields and explicit configuration rejection."""
from copy import deepcopy
from nlverif.config import ENDPOINT_GATE_SETTINGS

BASE = dict(schema_version=10, name="tilelink_fields", address_width=32,
            reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
            clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")],
            initiators=[dict(name="s", protocol="tl_ul", data_width=32, clock_domain="main")],
            targets=[dict(name="t", protocol="tl_ul", data_width=32, clock_domain="main",
                          mappings=[dict(base="0x0", size="0x1000")])],
            connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def ports(mapping, out, peers):
    pins = {p["name"]: p for p in mapping["ports"]}
    assert pins["nl_i_s_a_size"]["width"] == 2
    assert pins["nl_i_s_a_source"]["width"] == 4
    assert "nl_t_t_a_source" not in pins
    assert "nl_i_s_d_sink" not in pins
    assert not any(p.endswith("awvalid") for p in pins)
    assert mapping["definition"]["endpoint_protocols"]["initiators"]["s"] == "tl_ul"
    assert mapping["definition"]["endpoint_protocols"]["targets"]["t"] == "tl_ul"
    assert mapping["definition"]["tilelink"]["initiators"]["s"]["source_width"] == 4
    assert "ahb_hresp_widths" not in mapping["definition"]
    assert "ahb_targets" not in mapping["definition"]["contract"]


fields = [dict(name="defaults", config=BASE, expect="accept", check=ports)]
for source_width in (0, 1, 16):
    cfg = deepcopy(BASE)
    cfg["initiators"][0].update(source_width=source_width, size_width=4, sink_width=16)
    cfg["targets"][0].update(source_width=16, size_width=4, sink_width=16,
                              max_transfer_bytes=4, max_outstanding=1)
    fields.append(dict(name=f"widths_{source_width}", config=cfg, expect="accept"))
for kind in ("initiators", "targets"):
    for field, value, code in (
            ("protocol", "tl_uh", "NCL_UNSUPPORTED_PROTOCOL"),
            ("data_width", 64, "NCL_TILELINK_PROFILE"),
            ("source_width", 17, "NCL_TILELINK_PROFILE"),
            ("size_width", 1, "NCL_TILELINK_PROFILE"),
            ("size_width", 5, "NCL_TILELINK_PROFILE"),
            ("sink_width", 17, "NCL_TILELINK_PROFILE"),
            ("max_transfer_bytes", 8, "NCL_TILELINK_PROFILE"),
            ("len_width", 4, "NCL_TILELINK_PROFILE"),
            ("id_width", 4, "NCL_TILELINK_PROFILE"),
            ("atomics", True, "NCL_SCHEMA_UNKNOWN_FIELD")):
        cfg = deepcopy(BASE)
        cfg[kind][0][field] = value
        if field == "protocol":
            fields.append(dict(name=f"uh_{kind}", config=cfg, expect="accept"))
            continue
        fields.append(dict(name=f"{kind}_{field}_{value}", config=cfg, expect="reject", code=code))
for field, value in (("max_outstanding", 2), ("flows", 2),
                     ("timeout", dict(cycles=16, completion=True)),
                     ("attributes", "amba")):
    cfg = deepcopy(BASE)
    cfg["targets"][0][field] = value
    fields.append(dict(name=f"target_{field}", config=cfg, expect="reject", code="NCL_TILELINK_PROFILE"))
for kind in ("initiators", "targets"):
    for field in ("source_width", "sink_width", "size_width", "max_transfer_bytes"):
        cfg = deepcopy(BASE)
        cfg[kind][0].update(protocol="axi4", **{field: 4})
        if kind == "targets":
            cfg[kind][0]["attributes"] = "amba"
        fields.append(dict(name=f"other_{kind}_{field}", config=cfg, expect="reject", code="NCL_TILELINK_PROFILE"))

for address_width in (2, 12, 40, 48):
    cfg = deepcopy(BASE)
    cfg["address_width"] = address_width
    cfg["targets"][0]["address_width"] = min(32, address_width)
    if address_width == 2:
        cfg["targets"][0]["mappings"][0]["size"] = "0x4"
    fields.append(dict(name=f"address_{address_width}", config=cfg, expect="accept"))


cfg = deepcopy(BASE)
cfg["initiators"][0]["qos"] = dict(levels=dict(read=0, write=0))
fields.append(dict(name="zero_levels", config=cfg, expect="accept", same_as="defaults"))


def logging_config(source, target, whole=False, flags=False, observed=True):
    cfg = deepcopy(BASE)
    cfg["initiators"][0]["protocol"] = source
    cfg["initiators"].append(dict(name="host", protocol="apb4", data_width=32,
                                   clock_domain="main"))
    cfg["targets"][0]["protocol"] = target
    if target != "tl_ul":
        cfg["targets"][0]["attributes"] = "amba"
    cfg["connectivity"] = dict(s=["t"], host=["csr"])
    cfg["error_logging"] = dict(
        registers=dict(name="csr", base="0x10000", size="0x1000", clock_domain="main"),
        logs=[dict(name="log", watch=["t"], whole_record=whole)])
    if not observed:
        cfg["targets"].append(dict(name="other", protocol="tl_ul", data_width=32,
                                   clock_domain="main", mappings=[dict(base="0x2000", size="0x1000")]))
        cfg["connectivity"] = dict(s=["other"], host=["t", "csr"])
    if flags:
        cfg["error_logging"]["audit_flags"] = ["flag"]
        for initiator in cfg["initiators"]:
            initiator["audit_flags"] = dict(flag="0")
    return cfg


options = []
for target in ("axi4", "apb4", "tl_ul"):
    for whole in (False, True):
        options.append(dict(name=f"logging_tl_source_{target}_{whole}",
                          config=logging_config("tl_ul", target, whole),
                          expect="reject", code="NCL_UNSUPPORTED_TOPOLOGY"))
for source in ("axi4", "apb4"):
    for whole in (False, True):
        options.append(dict(name=f"logging_control_{source}_{whole}",
                          config=logging_config(source, source, whole), expect="accept"))
    options.append(dict(name=f"logging_tl_target_{source}",
                      config=logging_config(source, "tl_ul"), expect="reject",
                      code="NCL_UNSUPPORTED_TOPOLOGY"))
for source in ("tl_ul", "axi4", "apb4"):
    options.append(dict(name=f"audit_flags_{source}",
                      config=logging_config(source, "apb4", flags=True, observed=False),
                      expect="reject" if source == "tl_ul" else "accept",
                      code="NCL_UNSUPPORTED_TOPOLOGY"))
cfg = logging_config("tl_ul", "apb4", observed=False)
options.append(dict(name="unobserved_tl_source", config=cfg, expect="reject",
                  code="NCL_UNSUPPORTED_TOPOLOGY"))
cfg = deepcopy(cfg)
cfg["connectivity"]["s"].append("csr")
options.append(dict(name="tl_service_access", config=cfg, expect="reject",
                  code="NCL_UNSUPPORTED_TOPOLOGY"))

for role in ("initiators", "targets"):
    # A scheduling check must see both external endpoint roles.
    for qos in (dict(levels=dict(read=1, write=2)), dict(axqos_map=[0, 1]),
                dict(shaper=dict(bytes_per_1024_cycles=16, burst_bytes=32)),
                dict(shaper=dict(bytes_per_1024_cycles=16, burst_bytes=32),
                     scheduler=dict(under_level=1, over_level=2, threshold_percent=50))):
        cfg = deepcopy(BASE)
        other = "targets" if role == "initiators" else "initiators"
        cfg[other][0]["protocol"] = "axi4"
        if other == "targets":
            cfg[other][0]["attributes"] = "amba"
        cfg["initiators"][0]["qos"] = qos
        code = "NCL_QOS_PROFILE"
        if role == "initiators" and "axqos_map" in qos:
            code = "NCL_QOS_MAP"
        options.append(dict(name=f"qos_{role}_{next(reversed(qos))}", config=cfg,
                          expect="reject", code=code))
    for mode, settings in ENDPOINT_GATE_SETTINGS.items():
        cfg = deepcopy(BASE)
        cfg["clock_gating"] = True
        cfg[role][0]["clock_gating"] = settings
        options.append(dict(name=f"gating_{role}_{mode}", config=cfg, expect="accept"))

for field, value, code in (
        ("hresp_width", 2, "NCL_TARGET_AHB_HRESP"),
        ("not_a_field", True, "NCL_SCHEMA_UNKNOWN_FIELD"),
        ("undefined_length_bursts", False, "NCL_TARGET_AHB_BURSTS"),
        ("apb_back_to_back", True, "NCL_TARGET_APB_ACCESS"),
        ("exclusive", "target", "NCL_TARGET_EXCLUSIVE"),
        ("pipes", dict(request=dict(slice="full")), "NCL_ENDPOINT_PIPE")):
    cfg = deepcopy(BASE)
    cfg["targets"][0][field] = value
    options.append(dict(name=f"target_protocol_{field}", config=cfg,
                      expect="reject", code=code))
for field, value in (("fixed_bursts", "single"), ("read_side_effects", True),
                     ("timeout", dict(cycles=16, completion=False)),
                     ("pipes", dict(arrival=dict(slice="forward"), dispatch=True,
                                    response=dict(slice="forward")))):
    cfg = deepcopy(BASE)
    cfg["targets"][0][field] = value
    options.append(dict(name=f"target_supported_{field}", config=cfg, expect="accept"))
cfg = deepcopy(BASE)
cfg["initiators"][0].update(protocol="apb4", qos=dict(
    shaper=dict(bytes_per_1024_cycles=16, burst_bytes=32)))
options.append(dict(name="apb_shaper_tl_target", config=cfg, expect="accept"))
cfg = deepcopy(BASE)
cfg["transport"] = dict(user_flags=["owner"])
cfg["initiators"][0]["user_flags"] = dict(owner="0")
options.append(dict(name="constant_user_flag", config=cfg, expect="accept"))
cfg = deepcopy(cfg)
cfg["initiators"][0]["protocol"] = "axi4"
cfg["targets"][0].update(exclusive="network", exclusive_owner_flags=["owner"])
options.append(dict(name="network_exclusive_tl_target", config=cfg, expect="accept"))
cfg = deepcopy(BASE)
cfg["initiators"][0].update(route_classes=["data"], default_class="data")
options.append(dict(name="tl_source_route_class", config=cfg, expect="reject",
                  code="NCL_SOURCE_PATH"))

source_limits = []
# Each maximum is checked with its minimum size encoding and adjacent failures.
for maximum in (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
    cfg = deepcopy(BASE)
    width = 2 if maximum <= 8 else 3 if maximum <= 128 else 4
    cfg["initiators"][0].update(protocol="tl_uh", max_transfer_bytes=maximum, size_width=width)
    source_limits.append(dict(name=f"uh_maximum_{maximum}", config=cfg, expect="accept"))
for field, value in (("max_transfer_bytes", 3), ("max_transfer_bytes", 6),
                     ("max_transfer_bytes", 8192), ("size_width", 1), ("size_width", 5),
                     ("source_width", 17), ("sink_width", 17), ("len_width", 4),
                     ("id_width", 4), ("pipes", dict(packets=dict(slice="forward")))):
    cfg = deepcopy(BASE)
    cfg["initiators"][0].update(protocol="tl_uh", **{field: value})
    source_limits.append(dict(name=f"uh_invalid_{field}_{value}", config=cfg,
                      expect="reject", code="NCL_TILELINK_PROFILE"))

cfg = deepcopy(BASE)
cfg["address_width"] = 2
cfg["targets"][0]["mappings"][0]["size"] = "0x4"
cfg["initiators"][0]["protocol"] = "tl_uh"
source_limits.append(dict(name="uh_address_two_bits", config=cfg, expect="reject",
                  code="NCL_TILELINK_PROFILE", field="initiators[0].address_width"))
cfg = deepcopy(cfg)
cfg["initiators"][0]["max_transfer_bytes"] = 4
source_limits.append(dict(name="uh_address_two_bits_maximum_four", config=cfg, expect="accept"))

for capacity in (1, 3, 64):
    for source_width in (0, 16):
        cfg = deepcopy(BASE)
        cfg["initiators"][0].update(protocol="tl_uh", source_width=source_width,
                                     max_outstanding=capacity, max_transfer_bytes=4096,
                                     size_width=4)
        source_limits.append(dict(name=f"uh_capacity_{capacity}_source_{source_width}",
                          config=cfg, expect="accept"))
target_limits = []
for maximum in (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
    cfg = deepcopy(BASE)
    cfg["targets"][0].update(protocol="tl_uh", max_transfer_bytes=maximum,
                              size_width=2 if maximum<=8 else 3 if maximum<=128 else 4)
    target_limits.append(dict(name=f"uh_target_maximum_{maximum}", config=cfg, expect="accept"))
for field, value in (("max_transfer_bytes", 0), ("max_transfer_bytes", 3),
                     ("max_transfer_bytes", 6), ("max_transfer_bytes", 8192),
                     ("source_width", 17),
                     ("sink_width", 17), ("max_outstanding", 0),
                     ("max_outstanding", 17), ("flows", 2),
                     ("timeout", dict(cycles=16, completion=True))):
    cfg = deepcopy(BASE)
    cfg["targets"][0].update(protocol="tl_uh", **{field:value})
    target_limits.append(dict(name=f"uh_target_reject_{field}_{value}", config=cfg,
                      expect="reject", code="NCL_TILELINK_PROFILE"))
for target in ("axi4", "apb4", "tl_ul"):
    target_limits.append(dict(name=f"uh_logging_{target}", config=logging_config("tl_uh", target),
                      expect="reject", code="NCL_UNSUPPORTED_TOPOLOGY"))

capacity_groups = {}
for capacity in range(1, 17):
    group = "small_capacity"
    if capacity > 8:
        start = capacity if capacity % 2 else capacity - 1
        group = f"capacity_{start}_{start + 1}"
    target_capacity = capacity_groups.setdefault(group, [])
    for source_width in (None, (capacity-1).bit_length(), 16):
        cfg = deepcopy(BASE)
        cfg["targets"][0].update(protocol="tl_uh", max_outstanding=capacity)
        if source_width is not None:
            cfg["targets"][0]["source_width"] = source_width
        target_capacity.append(dict(name=f"uh_target_capacity_{capacity}_source_{source_width}",
                          config=cfg, expect="accept"))
    if capacity > 1:
        cfg = deepcopy(BASE)
        cfg["targets"][0].update(protocol="tl_uh", max_outstanding=capacity,
                                  source_width=(capacity-1).bit_length()-1)
        target_capacity.append(dict(name=f"uh_target_source_short_{capacity}", config=cfg,
                          expect="reject", code="NCL_TILELINK_PROFILE",
                          field="targets[0].source_width"))

clocked_targets = []
for capacity in (1, 3):
    for width in (32, 128):
        for gating in (False, True):
            cfg = deepcopy(BASE)
            cfg["clock_gating"] = gating
            cfg["clock_domains"][0]["period_ps"] = 10000
            cfg["clock_domains"].append(dict(name="far", clock_port="clk_far",
                reset_port="reset_n", test_port="test", period_ps=17000))
            cfg["initiators"][0].update(protocol="axi4", data_width=width,
                id_width=4, len_width=8, max_outstanding=4)
            cfg["targets"][0].update(protocol="tl_uh", clock_domain="far",
                max_outstanding=capacity, max_transfer_bytes=1024, size_width=4)
            cfg["topology"] = dict(
                request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                    entries=[dict(endpoint="s", node="q")], exits=[dict(endpoint="t", node="q")]),
                response=dict(nodes=[dict(name="r", clock_domain="far")], links=[],
                    entries=[dict(endpoint="t", node="r")], exits=[dict(endpoint="s", node="r")]))
            clocked_targets.append(dict(name=f"uh_target_clocked_{capacity}_{width}_{gating}",
                              config=cfg, expect="accept"))

# Keep entries that compare against an earlier artifact in the same group.
CASE_GROUPS = dict(fields=fields, options=options, source_limits=source_limits,
                   target_limits=target_limits, **capacity_groups,
                   clocked_targets=clocked_targets)
CASES = [entry for group in CASE_GROUPS.values() for entry in group]
