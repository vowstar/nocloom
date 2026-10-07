"""Protocol traffic of the eight feature examples shipped under examples/."""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import explicit_routes  # noqa: E402
import mode_remap  # noqa: E402
from nlverif import config as nlconfig  # noqa: E402


def artifact(stem):
    root = Path(os.environ.get("NOCLOOM_EXAMPLE_CONFIG_ROOT", "."))
    return dict(source="generated", dir=str(root / "config_examples" / stem / "out"))


def axi_bfms(count):
    return ([dict(name=f"s{k}", kind="axi_master", endpoint=f"initiator:s{k}")
             for k in range(count)]
            + [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=3)])


def model(cfg, **extra):
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, **extra))


def route(shape):
    stem = "explicit_routes" if shape == "diamond" else "explicit_routes_" + shape
    out = explicit_routes.network(shape)
    out.update(label=shape, dut=artifact(stem),
               probes=dict(instance={"network": "dut"}),
               scenario_args=dict(kind="route", shape=shape, denied=shape == "conn_flows"))
    return out


def level_probes(mapping):
    row = mapping["network"]["formal"]["sources"]["s0"]
    nets = [(field, "nl_internal_v" + row[key].split("/")[1], width)
            for field, key, width in (("valid", "request_valid", 1),
                                      ("write", "request_write", 1),
                                      ("level", "request_level", 3))]
    return [dict(name="axqos", kind="axqos", nets=nets)]


def level_oracle(checker, events, sources, targets):
    group = next(g for g in checker.bench["probes"] if g["name"] == "axqos")
    levels = {"w": set(), "r": set()}
    for event in events:
        if event.get("k") != "pr" or event.get("p") != group["port"]:
            continue
        row = checker._unpack(group, event)
        if row is None:
            return [dict(detail="AxQOS probe X")]
        if row["valid"][0]:
            levels["w" if row["write"] else "r"].add(row["level"][0])
    if levels != {"w": {0, 1, 2, 3}, "r": {0, 1, 2, 3}}:
        return [dict(detail="AxQOS levels not covered", levels=levels)]
    return []


def oracle(checker, events, sources, targets):
    kind = checker.case["scenario_args"]["kind"]
    if kind == "route":
        return explicit_routes.oracle(checker, events, sources, targets)
    if kind == "axqos":
        return level_oracle(checker, events, sources, targets)
    return []


def qos_scenario(t):
    t.reset(4)
    t.delay(2)
    t.mark("phase", 1)
    start = t.snap("t0", "aw")
    for k in range(12):
        t.write("s0", 0x100 + 0x40 * k, id=k % 4)
    t.wait("t0", "aw", ">=", 6, since=start, label="LIMITER_BURST_BURST_BYTES")
    t.delay(20)
    t.expect_now("t0", "aw", "<", 12, "LIMITER_HOLDS_BURST", since=start)
    t.drain(limit=5000, label="LIMITER_DRAIN")

    t.mark("phase", 2)
    t.write("s1", 0x1000, id=0)
    t.write("s2", 0x2000, id=0)
    t.drain(limit=4000, label="REGULATOR_HIGH")
    t.check(kind="first", port="t0", ch="aw", addr=0x1000,
            after_mark="phase", mark_value=2)
    for k in range(8):
        t.write("s1", 0x3000 + 0x40 * k, beats=4, id=k % 4)
    t.drain(limit=5000, label="REGULATOR_CHARGE")

    t.mark("phase", 3)
    t.write("s1", 0x1100, id=1)
    t.write("s2", 0x2100, id=1)
    t.drain(limit=4000, label="REGULATOR_LOW")
    t.check(kind="first", port="t0", ch="aw", addr=0x2100,
            after_mark="phase", mark_value=3)
    t.end()


def axqos_scenario(t):
    t.reset(4)
    t.delay(2)
    for q in range(8):
        t.write("s0", 0x100 + 0x40 * q, id=q % 4, qos=q)
        t.read("s0", 0x1000 + 0x40 * q, id=q % 4, qos=q)
    t.drain(limit=6000, label="AXQOS_ALL_LEVELS")

    t.mark("phase", 1)
    t.write("s0", 0x2000, id=0, qos=7)
    t.write("s2", 0x2100, id=0)
    t.drain(limit=4000, label="AXQOS_HIGH")
    t.check(kind="first", port="t0", ch="aw", addr=0x2000,
            after_mark="phase", mark_value=1)

    t.mark("phase", 2)
    t.write("s0", 0x3000, id=1, qos=0)
    t.write("s1", 0x3100, id=1)
    t.drain(limit=4000, label="AXQOS_LOW")
    t.check(kind="first", port="t0", ch="aw", addr=0x3100,
            after_mark="phase", mark_value=2)
    t.end()


def id_table_scenario(t):
    t.gate("t0", "r", False)
    t.gate("t0", "b", False)
    t.reset(4)
    t.delay(2)
    t.mark("phase", 1)
    reads, writes = t.snap("t0", "ar"), t.snap("t0", "aw")
    t.read("s0", 0x100, id=0)
    t.read("s0", 0x200, id=1)
    t.wait("t0", "ar", "==", 2, since=reads, label="ID_TABLE_TWO_READ_KEYS")
    t.write("s0", 0x300, id=0)
    t.delay(20)
    t.expect_now("t0", "aw", "==", 0, "ID_TABLE_THIRD_KEY_WAITS", since=writes)
    t.gate("t0", "r", True)
    t.wait("t0", "aw", "==", 1, since=writes, label="ID_TABLE_KEY_FREED")
    t.gate("t0", "b", True)
    t.drain(limit=5000, label="ID_TABLE_DRAIN")
    t.end()


def wrap_scenario(t):
    t.gate("t0", "r", False)
    t.reset(4)
    t.delay(2)
    t.mark("phase", 1)
    start = t.snap("t0", "ar")
    for k in range(5):
        t.read("s0", 0x104 + 0x100 * k, beats=4, size=2, burst="wrap", id=k % 4,
               pieces=[[0x100 + 0x100 * k, 1, 3, 2]])
    t.wait("t0", "ar", ">=", 4, since=start, limit=2000, label="WRAP_ALL_CONTEXTS")
    t.delay(20)
    t.expect_now("t0", "ar", "==", 4, "WRAP_POOL_CAPACITY", since=start)
    t.gate("t0", "r", True)
    t.drain(limit=5000, label="WRAP_READ_DRAIN")
    t.write("s0", 0x308, beats=4, size=3, burst="wrap", id=2,
            pieces=[[0x308, 3, 3, 2]])
    t.read("s0", 0x408, beats=4, size=3, burst="wrap", id=3,
           pieces=[[0x408, 3, 3, 2]])
    t.drain(limit=5000, label="WRAP_FULL_WORD_DRAIN")
    t.end()


def scenario(t, seed, kind="mode", shape="diamond", denied=False):
    if kind == "mode":
        mode_remap.scenario(t, seed)
    elif kind == "route":
        explicit_routes.scenario(t, seed, shape=shape, denied=denied)
    elif kind == "qos":
        qos_scenario(t)
    elif kind == "axqos":
        axqos_scenario(t)
    elif kind == "id_table":
        id_table_scenario(t)
    elif kind == "wrap":
        wrap_scenario(t)
    else:
        raise ValueError(kind)


def endpoint_model(sources, width=32):
    cfg = dict(schema_version=10,
               initiators=[dict(name=f"s{k}") for k in range(sources)],
               targets=[dict(name="t0", protocol="axi4", data_width=width,
                             mappings=[dict(base="0x0", size="0x10000")])],
               connectivity="all")
    return model(cfg)


MODE = mode_remap.CFG
CASE = dict(
    name="example_features",
    contracts=["AXI.ROUTE.MODE", "AXI.ROUTE.EXPLICIT", "AXI.ROUTE.CONNECTIVITY",
               "AXI.QOS.LEVEL", "AXI.OUTSTANDING.REACH", "AXI.ROUTE.LOCAL_OFFSET"],
    configs=[
        dict(label="mode", dut=artifact("mode_remap"), modes=["boot", "bank"],
             bfms=mode_remap.bfms(), model=mode_remap.model(MODE),
             scenario_args=dict(kind="mode")),
        route("diamond"), route("target_paths"), route("conn_flows"),
        dict(label="qos", dut=artifact("axi4_qos_profiles"), bfms=axi_bfms(3),
             model=endpoint_model(3), scenario_args=dict(kind="qos")),
        dict(label="axqos", dut=artifact("axi4_axqos_map"), bfms=axi_bfms(3),
             model=endpoint_model(3), map_probes=dict(groups=level_probes),
             scenario_args=dict(kind="axqos")),
        dict(label="id-table", dut=artifact("axi4_id_table"), bfms=axi_bfms(1),
             model=endpoint_model(1), scenario_args=dict(kind="id_table")),
        dict(label="wrap", dut=artifact("axi4_wrap_alignment"), bfms=axi_bfms(1),
             model=endpoint_model(1, 64), scenario_args=dict(kind="wrap")),
    ],
    oracle=oracle,
    seed=20261028,
    max_cycles=200000,
)
for config in CASE["configs"][1:]:
    config["modes"] = []
CASE.update({k: v for k, v in CASE["configs"][0].items() if k != "label"})
