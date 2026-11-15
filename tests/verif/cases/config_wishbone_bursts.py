"""Burst tag parsing, defaults, metadata and rejected combinations."""
import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wishbone import configuration

CASES = []


def add(name, cfg, code=None):
    CASES.append(dict(name=name, config=cfg, expect="reject" if code else "accept",
                      **(dict(code=code) if code else {})))


for role in ("initiators", "targets"):
    for value in (False, True):
        cfg = configuration("wishbone", "wishbone")
        cfg[role][0]["burst_tags"] = value
        add(f"{role}_{value}", cfg)
        for protocol in ("axi4", "axi3", "axi4_lite", "apb4", "ahb_lite", "tl_ul"):
            invalid = configuration(protocol if role == "initiators" else "axi4",
                                    protocol if role == "targets" else "wishbone")
            if role == "targets" and protocol == "tl_ul":
                invalid[role][0].pop("attributes", None)
            invalid[role][0]["burst_tags"] = value
            add(f"{role}_{protocol}_{value}", invalid, "NCL_WISHBONE_OPTION")
    cfg = configuration("wishbone", "wishbone")
    cfg[role][0].update(burst_tags=True, cycles="pipelined")
    add(role + "_pipelined_tags", cfg, "NCL_WISHBONE_OPTION")
    for bad in ("true", 1, [], {}):
        cfg = configuration("wishbone", "wishbone")
        cfg[role][0]["burst_tags"] = bad
        add(role + "_type_" + str(type(bad).__name__), cfg, "NCL_SCHEMA_TYPE")
for completion in (False, True):
    cfg = configuration("axi4", "wishbone")
    cfg["targets"][0].update(burst_tags=True, timeout=dict(cycles=32, completion=completion))
    add("target_timeout_" + str(completion), cfg, "NCL_WISHBONE_OPTION")
# The field on an initiator does not restrict a different endpoint's timeout.
cfg = configuration("wishbone", "wishbone")
cfg["initiators"][0]["burst_tags"] = True
cfg["targets"][0]["timeout"] = dict(cycles=32)
add("initiator_tags_target_timeout", cfg)
for role in ("initiators", "targets"):
    for value in (False, True):
        cfg = configuration("wishbone", "wishbone")
        cfg["templates"] = {role: {"tagged": {"burst_tags": value}}}
        cfg[role][0]["use"] = "tagged"
        add(f"{role}_template_{value}", cfg)


def buffering_check(mapping, out, peers):
    buffering = mapping["definition"]["wishbone"]["targets"]["t0"]["buffering"]
    assert buffering["write_banks"] == 2, "write_banks"
    assert buffering["physical_words_per_bank"] == 32, "physical_words_per_bank"
    assert buffering["read_response_slots"] == 2, "read_response_slots"
    assert buffering["active_packets"] == 1, "active_packets"


cfg = configuration("axi4", "wishbone")
cfg["targets"][0]["burst_tags"] = True
CASES.append(dict(name="target_buffering", config=cfg, expect="accept", check=buffering_check))
