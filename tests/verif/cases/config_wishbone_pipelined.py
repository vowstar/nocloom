"""Pipelined endpoints retain the external Wishbone capability limits."""

from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_wishbone import CASES as CLASSIC_CASES
from wishbone import configuration


CASES = []
for classic in CLASSIC_CASES:
    entry = deepcopy(classic)
    entry["name"] = "pipelined_" + entry["name"]
    for endpoint in entry["config"]["initiators"] + entry["config"]["targets"]:
        if endpoint["protocol"] == "wishbone":
            endpoint["cycles"] = "pipelined"
    if classic["name"] == "source_capacity":
        entry["expect"] = "accept"
        entry.pop("code")
    CASES.append(entry)


def add(name, cfg, code=None):
    CASES.append(dict(name=name, config=cfg, expect="reject" if code else "accept",
                      **(dict(code=code) if code else {})))


for role in ("initiators", "targets"):
    for mode in ("classic", "pipelined"):
        cfg = configuration("axi4", "axi4")
        cfg[role][0]["cycles"] = mode
        add(role + "_axi_cycles_" + mode, cfg, "NCL_WISHBONE_CYCLES")

for capacity in (0, 1, 2, 3, 4, 63, 64, 65):
    cfg = configuration("wishbone", "wishbone")
    cfg["initiators"][0].update(cycles="pipelined", max_outstanding=capacity)
    cfg["targets"][0]["cycles"] = "pipelined"
    add("source_capacity_" + str(capacity), cfg,
        "NCL_AXI4_CAPACITY" if capacity in (0, 65) else None)

cfg = configuration("wishbone", "wishbone")
for endpoint in cfg["initiators"] + cfg["targets"]:
    endpoint["cycles"] = "pipelined"
cfg["transport"] = dict(max_outstanding=4)
add("inherited_capacity", cfg)
