"""Configuration checks of target ID guarantees and exclusive_id_flags.

The expected route labels are explicit cases for the numbering in src/core/network_plan_profiles.cpp."""

import copy


def endpoint(name, **fields):
    return dict(name=name, clock_domain="main", protocol="axi4", data_width=32, **fields)


def source(name, id_width=2, **fields):
    return endpoint(name, id_width=id_width, len_width=8, **fields)


def target(name, base, **fields):
    mappings = fields.pop("mappings", [dict(base=hex(base), size="0x1000")])
    return endpoint(name, len_width=8, attributes="amba", mappings=mappings, **fields)


BASE = dict(schema_version=10, name="ids", address_width=16,
            reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
            clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                                test_port="test")],
            initiators=[source("s0"), source("s1")],
            targets=[target("t1", 0x1000), target("t2", 0x2000)],
            connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def numbers(name, edit, bits, initiators, targets):
    """bits: initiator flow, target flow, sub-range, sequence, direction."""
    cfg = copy.deepcopy(BASE)
    edit(cfg)
    want = dict(zip(("source_path", "target_path", "region_index", "sequence", "direction"), bits))

    def check(mapping, out, peers):
        ids = mapping["definition"]["target_ids"]
        transaction = dict(want, region_index=0)
        assert ids["route_bits"] == transaction, ids["route_bits"]
        observed = {f["name"]: f["width"] for f in ids["record_fields"]}
        assert {name: observed.get(name, 0) for name in want} == want, observed
        for key, order in (("route_fields", ("direction", "sequence", "source_path", "target_path")),
                           ("record_fields", ("region_index", "target_path", "source_path", "sequence", "direction"))):
            at = 0
            expected_fields = []
            widths = transaction if key == "route_fields" else want
            for field in order:
                if widths[field]:
                    expected_fields.append(dict(name=field, lsb=at, width=widths[field]))
                at += widths[field]
            assert ids[key] == expected_fields, ids[key]
        for labels, field, expected in ((ids["source_paths"], "source_path", initiators),
                                        (ids["target_paths"], "target_path", targets)):
            got = {name: flows[0] for name, flows in labels.items()}
            assert got == expected, labels
            assert all(flows == list(range(flows[0], flows[0] + len(flows)))
                       for flows in labels.values()), labels

    return dict(name=name, config=cfg, expect="accept", check=check)


def edits(*steps):
    def apply(cfg):
        for step in steps:
            step(cfg)
    return apply


def add_source(entry):
    return lambda cfg: cfg["initiators"].append(entry)


def only(cfg_pairs):
    return lambda cfg: cfg.update(connectivity=cfg_pairs)


def apb_only(cfg):
    cfg["initiators"] = [dict(name="p0", clock_domain="main", protocol="apb4", data_width=32)]


def owner(mode, flags=("src", "idb"), **fields):
    cfg = copy.deepcopy(BASE)
    cfg["transport"] = dict(user_flags=["src", "idb"])
    for index, entry in enumerate(cfg["initiators"]):
        entry["user_flags"] = dict(src=index, idb=0)
    cfg["targets"][0].update(exclusive=mode, exclusive_id_flags=list(flags), **fields)
    return cfg


def rejected(name, cfg, code):
    return dict(name=name, config=cfg, expect="reject", code=code)


def apb_source(name, **fields):
    return dict(name=name, clock_domain="main", protocol="apb4", data_width=32, **fields)


def apb_target(name, *windows, **fields):
    return dict(endpoint(name, attributes="amba", mappings=[map_of(w) for w in windows]),
                **{"protocol": "apb4", **fields})


def map_of(window):
    base, size, *when = window
    return dict(base=hex(base), size=hex(size), **(dict(when=when[0]) if when else {}))


def axi_target(name, *windows, max_outstanding=4, id_width=3, **fields):
    return dict(target(name, 0, mappings=[map_of(w) for w in windows]), max_outstanding=max_outstanding,
                id_width=id_width, **fields)


def order_shape(targets, initiators=None, connectivity=None, modes=None):
    cfg = copy.deepcopy(BASE)
    cfg.update(initiators=initiators or [apb_source("p0")], targets=targets,
               connectivity=connectivity or "all")
    if modes:
        cfg.update(address_inputs=[dict(name="boot", width=2, clock_domain="main")],
                   address_selectors=[dict(name=f, equals=dict(boot=v)) for f, v in modes.items()])
    return cfg


TOPOLOGY = "NCL_UNSUPPORTED_TOPOLOGY"
NO_MAPPING = "NCL_CONNECTIVITY_REFERENCE"
K = 0x1000
LITE = dict(protocol="axi4_lite")
ALT, NOT_ALT = dict(alt=True), dict(alt=False)
ALT_BET, BET_ALT = dict(alt=True, bet=False), dict(alt=False, bet=True)
# Direction identifies transactions from non-APB sources, independent of mappings and targets.
ORDER = [
    ("window_base_apb_first", [apb_target("a", (0, K)), axi_target("t1", (K, K))], None, None, 0),
    ("window_base_axi_first", [apb_target("a", (K, K)), axi_target("t1", (0, K))], None, None, 0),
    ("declared_first_higher", [axi_target("a", (K, K)), apb_target("t1", (0, K))], None, None, 0),
    ("second_mapping_later", [apb_target("a", (0x4000, K)),
                              axi_target("t1", (0xc000, 4 * K), (0x8000, 2 * K))], None, None, 0),
    ("read_only_first", [axi_target("r0", (0, K), access="read_only"), axi_target("t1", (K, K))],
     None, None, 0),
    ("axi3_target", [apb_target("a", (0, K)),
                     dict(axi_target("t1", (K, K)), protocol="axi3", len_width=4)], None, None, 0),
    ("one_burst_target", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1)],
     None, None, 0),
    ("one_burst_target_first", [axi_target("a", (0, K), max_outstanding=1),
                                apb_target("t2", (2 * K, K))], None, None, 0),
    ("two_burst_target", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=2)],
     None, None, 0),
    ("one_of_two_targets", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1),
                            axi_target("t2", (2 * K, K))], None, None, 0),
    ("no_id_port", [apb_target("a", (0, K)), axi_target("t1", (K, K), id_width=0)], None, None, 0),
    ("read_only_later", [apb_target("a", (0, K)), axi_target("t1", (K, K), access="read_only")],
     None, None, 0),
    ("write_only_later", [apb_target("a", (0, K)), axi_target("t1", (K, K), access="write_only")],
     None, None, 0),
    ("lite_target_later", [apb_target("a", (0, K)), dict(apb_target("t1", (K, K)), **LITE)], None, None, 0),
    ("ahb_target_later", [apb_target("a", (0, K)), apb_target("t1", (K, K), protocol="ahb_v3")], None, None, 0),
    ("axi_source_too", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1)],
     [apb_source("p0"), source("x0")], None, 1),
    ("lite_source", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1)],
     [dict(apb_source("p0"), protocol="axi4_lite")], None, 1),
    ("ahb_source", [apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1)],
     [dict(apb_source("p0"), protocol="ahb_v3")], None, 1),
    ("both_apb_later", [apb_target("a", (0, K)), axi_target("t1", (K, K))],
     [apb_source("p0"), apb_source("p1")], None, 0),
    ("above_narrow_source", [apb_target("a", (0x10000, K)), axi_target("t1", (0x2000, 2 * K))],
     [apb_source("p0", address_width=16), apb_source("p1")], dict(p0=["t1"], p1=["a", "t1"]), 0),
]
MODES = [
    ("mode_required_first", [apb_target("a", (0, K, ALT)), axi_target("t1", (0, K, NOT_ALT))], 0),
    ("mode_excluded_later", [apb_target("a", (0, K, NOT_ALT)), axi_target("t1", (0, K, ALT))], 0),
    ("mode_names_apb", [apb_target("a", (0, K, ALT_BET)), axi_target("t1", (0, K, BET_ALT))], 0),
    ("mode_names_axi", [apb_target("a", (0, K, BET_ALT)), axi_target("t1", (0, K, ALT_BET))], 0),
]


def order_case(name, cfg, direction):
    def check(mapping, out, peers):
        # A network without an AXI target ID lists no numbers.
        ids = mapping["definition"].get("target_ids")
        assert (ids["route_bits"]["direction"] if ids else 0) == direction, ids

    return dict(name=name, config=cfg, expect="accept", check=check)


def order_cases():
    out = []
    for name, targets, initiators, connectivity, direction in ORDER:
        cfg = order_shape(targets, initiators, connectivity)
        if name.startswith("above_narrow_source"):
            cfg["address_width"] = 20
        out.append(order_case(name, cfg, direction))
    for name, targets, direction in MODES:
        flags = {f for t in targets for m in t["mappings"] for f in m["when"]}
        out.append(order_case(name, order_shape(targets, modes={f: dict(alt=1, bet=2)[f] for f in flags}), direction))
    return out


# A connection needs a mapping below the source's address limit, flow pair by flow pair.
NARROW = [apb_source("p0", address_width=16), apb_source("p1")]


def narrow_shape(targets, connectivity=None):
    cfg = order_shape(targets, NARROW, connectivity)
    cfg["address_width"] = 20
    return cfg


def two_flows_above():
    return dict(axi_target("t1", (0x2000, 2 * K)), flows=2,
                mappings=[dict(base="0x2000", size="0x2000", flow=0),
                          dict(base="0x10000", size="0x1000", flow=1)])


# Sub-range widths: two sources of one target, whose error apertures count, or with a wider source
# that has none.
def two_sources(*windows):
    return edits(lambda cfg: cfg.update(targets=[axi_target("t1", *windows)]))


def wider_source(*windows):
    def apply(cfg):
        cfg.update(address_width=20,
                   initiators=[source("s0", address_width=16), source("s1", address_width=16),
                               source("w0", address_width=20)],
                   targets=[axi_target("t1", *windows), apb_target("u", (0x30000, K))],
                   connectivity=dict(s0=["t1"], s1=["t1"], w0=["t1", "u"]))
    return apply



def flagged(*names):
    def apply(cfg):
        cfg.update(address_inputs=[dict(name="boot", width=2, clock_domain="main")],
                   address_selectors=[dict(name=n, equals=dict(boot=dict(alt=1, bet=2)[n])) for n in names])
    return apply


def region_index(name, width, edit):
    return numbers(name, edit, (1, 0, width, 2, 1), dict(s0=0, s1=1), dict(t1=0))


def wide_range(name, edit):
    return numbers(name, edit, (2, 1, 1, 2, 1), dict(s0=0, s1=1, w0=2), dict(t1=0, u=1))


NOT_BET = dict(alt=False, bet=False)
NOT_ALT_BET = dict(alt=False, bet=True)
REGION_INDICES = [
    region_index("range_full", 0, two_sources((0, 0x10000))),
    region_index("range_half", 1, two_sources((0, 0x8000))),
    region_index("range_mode_full", 1, edits(flagged("alt"), two_sources((0, 0x10000, NOT_ALT)))),
    region_index("range_mode_both", 1, edits(flagged("alt"),
                                          two_sources((0, 0x10000, NOT_ALT), (0, 0x10000, ALT)))),
    region_index("range_mode_split", 2, edits(flagged("alt"), two_sources(
        (0x8000, 0x8000, NOT_ALT), (0, 0x8000), (0x8000, 0x8000, ALT)))),
    region_index("range_mode_hole", 2, edits(flagged("alt"), two_sources((0, 0x8000, NOT_ALT)))),
    # One aperture per set of blocks an assignment of the flags leaves visible, when it has a hole.
    region_index("range_flag_sets", 3, edits(flagged("alt", "bet"), two_sources(
        (0x8000, 0x8000, NOT_ALT), (0, 0x4000, NOT_BET)))),
    region_index("range_plain_and_flag", 3, edits(flagged("alt"), two_sources(
        (0x4000, 0x4000, NOT_ALT), (0, 0x4000), (0x8000, 0x4000), (0xc000, 0x4000)))),
    region_index("range_both_flags_full", 2, edits(flagged("alt", "bet"), two_sources(
        (0, 0x10000, NOT_ALT_BET), (0, 0x10000, NOT_BET)))),
    region_index("range_full_in_every_assignment", 2, edits(flagged("alt", "bet"), two_sources(
        (0, 0x8000, NOT_BET), (0x8000, 0x8000, NOT_BET), (0, 0x10000, NOT_ALT_BET)))),
    region_index("range_larger_than_source", 0, edits(two_sources((0, 0x40000)), lambda cfg: cfg.update(
        address_width=20, initiators=[source("s0", address_width=16), source("s1", address_width=16)]))),
    wide_range("range_above_full", wider_source((0, 0x10000), (0x20000, K))),
    wide_range("range_above_half", wider_source((0, 0x8000), (0x20000, K))),
    wide_range("range_block_above", wider_source((0x8000, 0x10000))),
    wide_range("range_mode_above", edits(flagged("alt"), wider_source((0, 0x10000), (0x20000, K, ALT)))),
]
CONNECTION = [
    rejected("connection_above_narrow_source",
             narrow_shape([apb_target("a", (0x10000, K)), axi_target("t1", (0x2000, 2 * K))]),
             NO_MAPPING),
    rejected("connection_above_narrow_source_listed",
             narrow_shape([apb_target("a", (0x10000, K)), axi_target("t1", (0x2000, 2 * K))],
                          dict(p0=["a", "t1"], p1=["a", "t1"])),
             NO_MAPPING),
    rejected("connection_at_address_limit",
             narrow_shape([apb_target("a", (0x10000, 2 * K), (0x18000, K)),
                           axi_target("t1", (0x2000, 2 * K))]),
             NO_MAPPING),
    rejected("connection_target_path_above", narrow_shape([two_flows_above()]), NO_MAPPING),
    dict(name="connection_other_target_path",
         config=narrow_shape([two_flows_above()],
                             dict(p0=[dict(target="t1", target_path=0)], p1=["t1"])),
         expect="accept"),
    dict(name="connection_block_below_limit",
         config=narrow_shape([apb_target("a", (0xf000, 2 * K)), axi_target("t1", (0x2000, K))]),
         expect="accept"),
    dict(name="connection_window_over_limit",
         config=narrow_shape([apb_target("a", (0x40000, K)), axi_target("t1", (0, 0x40000))],
                             dict(p0=["t1"], p1=["a", "t1"])),
         expect="accept"),
]


# Every aligned block of a mapping needs a connected source that addresses it.
UNUSED = "NCL_CONNECTIVITY_UNUSED_TARGET"


def only_narrow(*windows):
    cfg = order_shape([axi_target("t1", *windows)], [source("s0", address_width=16)])
    cfg["address_width"] = 20
    return cfg


def flow_not_connected():
    cfg = narrow_shape([two_flows_above()], dict(p0=[dict(target="t1", target_path=0)],
                                                 p1=[dict(target="t1", target_path=0)]))
    return cfg


UNUSED_MAPPINGS = [
    rejected("mapping_above_every_source", only_narrow((0, 0x10000), (0x20000, K)), UNUSED),
    rejected("mapping_above_with_hole", only_narrow((0, 0x8000), (0x20000, K)), UNUSED),
    rejected("block_above_every_source", only_narrow((0x8000, 0x10000)), UNUSED),
    rejected("target_path_of_no_source", flow_not_connected(), UNUSED),
    dict(name="mapping_above_narrow_reached_by_wide",
         config=narrow_shape([apb_target("a", (0x10000, K), (0x8000, 4 * K)),
                              axi_target("t1", (0x2000, 2 * K))]),
         expect="accept"),
]


def no_flags():
    cfg = owner("network")
    del cfg["targets"][0]["exclusive_id_flags"]
    return cfg


def flags_without_mode():
    cfg = owner("network")
    del cfg["targets"][0]["exclusive"]
    return cfg


CASES = [
    numbers("two_by_two", lambda cfg: None, (1, 1, 0, 2, 1), dict(s0=0, s1=1), dict(t1=0, t2=1)),
    numbers("name_orders", edits(add_source(source("a0")), lambda cfg: cfg["targets"][1].update(name="a")),
            (2, 1, 0, 2, 1), dict(a0=0, s0=1, s1=2), dict(a=0, t1=1)),
    numbers("two_mappings",
            lambda cfg: cfg["targets"][0]["mappings"].append(dict(base="0x4000", size="0x1000")),
            (1, 1, 1, 2, 1), dict(s0=0, s1=1), dict(t1=0, t2=1)),
    numbers("three_blocks",
            lambda cfg: cfg["targets"][0]["mappings"].__setitem__(0, dict(base="0x1000", size="0x3000"))
            or cfg["targets"][1]["mappings"].__setitem__(0, dict(base="0x8000", size="0x1000")),
            (1, 1, 1, 2, 1), dict(s0=0, s1=1), dict(t1=0, t2=1)),
    numbers("wider_source", lambda cfg: cfg["initiators"][1].update(id_width=3),
            (1, 1, 0, 3, 1), dict(s0=0, s1=1), dict(t1=0, t2=1)),
    # A source of one target: its unmapped addresses add an aperture.
    numbers("error_aperture",
            edits(add_source(source("c0")),
                  only(dict(s0=["t1", "t2"], s1=["t1", "t2"], c0=["t2"]))),
            (2, 1, 1, 2, 1), dict(c0=0, s0=1, s1=2), dict(t1=0, t2=1)),
    # The same with its whole address space mapped: none.
    numbers("no_hole",
            edits(lambda cfg: cfg.update(initiators=[source("c0")],
                                         targets=[target("t1", 0, mappings=[dict(base="0x0", size="0x10000")])])),
            (0, 0, 0, 2, 1), dict(c0=0), dict(t1=0)),
    numbers("apb_only_ordered", edits(apb_only, lambda cfg: cfg.update(
        targets=[target("t1", 0x1000, mappings=[dict(base="0x0", size="0x10000")])])),
            (0, 0, 0, 0, 0), dict(p0=0), dict(t1=0)),
    numbers("apb_only_read_only", edits(apb_only, lambda cfg: cfg.update(
        targets=[target("t1", 0x1000, access="read_only",
                        mappings=[dict(base="0x0", size="0x10000")])])),
            (0, 0, 0, 0, 0), dict(p0=0), dict(t1=0)),
    # APB identities need neither a direction bit nor a source AxID field.
    numbers("apb_later_one_burst", edits(lambda cfg: cfg.update(
        initiators=[apb_source("p0"), apb_source("p1")],
        targets=[apb_target("a", (0, K)), axi_target("t1", (K, K), max_outstanding=1)])),
            (1, 1, 0, 0, 0), dict(p0=0, p1=1), dict(a=0, t1=1)),
    numbers("apb_first_one_burst", edits(lambda cfg: cfg.update(
        initiators=[apb_source("p0"), apb_source("p1")],
        targets=[apb_target("a", (K, K)), axi_target("t1", (0, K), max_outstanding=1)])),
            (1, 1, 0, 0, 0), dict(p0=0, p1=1), dict(a=0, t1=1)),
    # Endpoints take compact intervals in name order, regardless of their flow counts.
    numbers("compact_flows",
            lambda cfg: cfg.update(
                initiators=[source("a0"), source("z0", route_classes=["even", "odd"], default_class="odd",
                                  id_ranges=[dict(first=k, last=k, **{"class": "even"}) for k in (0, 2)])],
                targets=[target("a1", 0, id_width=3,
                                mappings=[dict(base="0x1000", size="0x1000", flow=0),
                                          dict(base="0x2000", size="0x1000", flow=1)], flows=2),
                         target("b1", 0, id_width=3, flows=3,
                                mappings=[dict(base="0x4000", size="0x1000", flow=0),
                                          dict(base="0x5000", size="0x1000", flow=1),
                                          dict(base="0x6000", size="0x1000", flow=2)])]),
            (2, 3, 0, 2, 1), dict(a0=0, z0=1), dict(a1=0, b1=2)),
    numbers("eight_compact_paths",
            lambda cfg: cfg.update(
                initiators=[source(n, route_classes=[f"p{f}" for f in range(count)],
                                   default_class=f"p{count-1}",
                                   id_ranges=[dict(first=f, last=f, **{"class": f"p{f}"})
                                              for f in range(count-1)])
                            for n, count in (("a0", 3), ("b0", 3), ("c0", 2))],
                targets=[target(n, 0, flows=count, id_width=4,
                                mappings=[dict(base=hex((group*4+f+1)*0x1000), size="0x1000", flow=f)
                                          for f in range(count)])
                         for group, (n, count) in enumerate((("a1", 3), ("b1", 3), ("c1", 2)))]),
            (3, 3, 0, 2, 1), dict(a0=0, b0=3, c0=6), dict(a1=0, b1=3, c1=6)),
    dict(name="any_id_width_with_bursts",
         config=(lambda c: c["targets"][0].update(id_width=0, max_outstanding=4) or c)(copy.deepcopy(BASE)),
         expect="accept"),
    dict(name="owner_flags_network", config=owner("network"), expect="accept"),
    dict(name="owner_flags_target", config=owner("target", id_width=2, max_outstanding=4), expect="accept"),
    rejected("owner_flags_missing", no_flags(), "NCL_TARGET_EXCLUSIVE"),
    rejected("owner_flags_without_mode", flags_without_mode(), "NCL_TARGET_EXCLUSIVE"),
    rejected("owner_flag_unknown", owner("network", flags=("other",)), "NCL_TARGET_EXCLUSIVE"),
    rejected("owner_flag_twice", owner("target", flags=("src", "src")), "NCL_TARGET_EXCLUSIVE"),
    rejected("monitors_above_values", owner("network", flags=("src",), exclusive_monitors=3),
             "NCL_TARGET_EXCLUSIVE"),
    rejected("target_id_below_owner", owner("target", id_width=1), "NCL_AXI4_EXCHANGE_ID_WIDTH"),
    *order_cases(),
    *CONNECTION,
    *REGION_INDICES,
    *UNUSED_MAPPINGS,
]
