"""Source flow admission limits within a 4 KiB page."""

import copy


def configuration():
    return dict(
        schema_version=10, name="flow_pages", address_width=32,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main",
                            reset_port="reset_n", test_port="test")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4",
                         data_width=32, id_width=2, len_width=8, max_outstanding=4,
                         route_classes=["even", "odd"], default_class="odd",
                         id_ranges=[dict(first=i, last=i, **{"class": "even"})
                                    for i in (0, 2)]),
                    dict(name="s1", clock_domain="main", protocol="axi4",
                         data_width=32, len_width=8)],
        targets=[dict(name=f"t{i}", clock_domain="main", protocol="axi4",
                      data_width=32, len_width=8, attributes="amba",
                      mappings=[dict(base=base, size=size)])
                 for i, (base, size) in enumerate((("0x1400", "0x200"),
                                                  ("0x1800", "0x400")))],
        connectivity=dict(s0=[dict(target="t0", source_path=0),
                              dict(target="t1", source_path=1)], s1=["t0", "t1"]),
        output=dict(backend="verilog", language="verilog_2001"))


CASES = []
for name in ("implicit", "explicit", "axi3", "read_only", "write_only", "high", "nested",
             "overlapping_masks", "single", "pages", "separate_subpages", "all",
             "default_partial", "named_default_partial", "same_target"):
    cfg = copy.deepcopy(configuration())
    source, targets, conn = cfg["initiators"][0], cfg["targets"], cfg["connectivity"]
    if name == "explicit":
        source["default_target"] = "t1"
    elif name == "axi3":
        source.update(protocol="axi3", len_width=4)
    elif name in ("read_only", "write_only"):
        source["access"] = name
    elif name == "high":
        cfg["address_width"] = 48
        for t in targets:
            t["mappings"][0]["base"] = hex(int(t["mappings"][0]["base"], 16) + (1 << 40))
    elif name == "nested":
        targets[0]["mappings"] = [dict(base="0x1000", size="0x1000")]
    elif name == "overlapping_masks":
        conn["s0"].append(dict(target="t0", source_path=1))
    elif name == "single":
        source["max_outstanding"] = 1
    elif name == "pages":
        for i, t in enumerate(targets):
            t["mappings"] = [dict(base=hex((i + 1) * 4096), size="0x1000")]
    elif name == "separate_subpages":
        targets[1]["mappings"][0]["base"] = "0x2800"
    elif name == "all":
        cfg["connectivity"] = "all"
    elif name == "default_partial":
        conn["s0"][1].pop("source_path")
    elif name == "named_default_partial":
        source["default_target"] = "t1"
        conn["s0"][0].pop("source_path")
    elif name == "same_target":
        targets[0]["flows"] = 2
        targets[0]["mappings"].append(dict(base="0x1800", size="0x400", flow=1))
        cfg["targets"] = targets[:1]
        conn["s0"] = [dict(target="t0", source_path=i, target_path=i) for i in (0, 1)]
        conn["s1"] = ["t0"]
    reject = name in ("implicit", "explicit", "axi3", "read_only", "write_only", "high", "nested",
                      "overlapping_masks")
    CASES.append(dict(name=name, config=cfg, expect="reject" if reject else "accept",
                      **(dict(code="NCL_SOURCE_PATH", field="connectivity.s0") if reject else {})))
