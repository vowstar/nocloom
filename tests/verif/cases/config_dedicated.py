"""Configuration checks of the dedicated-target networks that the
transaction cases do not judge: port widths and presence in the map, contract
text, the per-mode map records of the sub-page networks, and the sub-page
configurations the CLI rejects without output."""

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import subpage_oracle as so  # noqa: E402

SUBPAGE_MODES = ("graph", "selective", "clocked", "uniform", "widths", "widths_uniform",
                 "packets", "attributes", "fixed", "fixed_widths", "fixed_packets",
                 "ahb_shaped", "widths1024")


def case_module(stem):
    spec = importlib.util.spec_from_file_location("nlv_config_dedicated_" + stem,
                                                  HERE / (stem + ".py"))
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = {}
    spec.loader.exec_module(module)
    return module


def widths_of(mapping):
    return {p["name"]: p["width"] for p in mapping["ports"]}


def subpage_check(mode):
    def check(mapping, out, peers):
        widths = widths_of(mapping)
        assert widths["nl_t_t1_paddr"] == 8 and widths["nl_t_t3_paddr"] == 10
        assert widths["nl_t_t2_haddr"] == 10 and widths["nl_i_s0_awaddr"] == 40
        # APB3 drops PSTRB/PPROT, APB2 also PREADY/PSLVERR; AXI4-Lite keeps a
        # 16-bit local address port.
        assert widths["nl_t_t5_paddr"] == 8 and "nl_t_t5_pstrb" not in widths
        assert "nl_t_t5_pprot" not in widths and "nl_t_t5_pready" in widths
        assert widths["nl_t_t6_paddr"] == 10 and "nl_t_t6_pready" not in widths
        assert "nl_t_t6_pslverr" not in widths
        assert widths["nl_t_t7_awaddr"] == 16 and widths["nl_t_t7_wstrb"] == 4
        # Narrower source ports, a read-only source and a read-only AXI4 target.
        assert widths["nl_i_s0_awid"] == 3 and widths["nl_i_s1_arlen"] == 4
        assert widths["nl_i_s1_rid"] == 3
        assert not any(n.startswith(("nl_i_s2_aw", "nl_i_s2_w", "nl_i_s2_b")) for n in widths)
        assert not any(n.startswith(("nl_t_t0_aw", "nl_t_t0_w", "nl_t_t0_b")) for n in widths)
        assert widths["nl_i_s2_araddr"] == 40 and "nl_t_t0_araddr" in widths
        # Per-endpoint widths: the 128-bit source attaches at its own width.
        definition = mapping["definition"]
        t8_bits = 1024 if mode in so.WIDE_TARGET_MODES else 64
        assert (definition.get("target_data_widths") == {"t8": t8_bits}) == \
            so.base_mode(mode).startswith("widths")
        contract = definition["contract"]
        uniform = mode in ("uniform", "widths_uniform")
        remainder = "each_beat_SLVERR" if uniform else "each_beat_DECERR"
        assert contract["apb_targets"]["address"]["subpage_remainder"].startswith(remainder)
        assert ("DECERR" in json.dumps(contract)) == (not uniform)
        assert "address" in contract["ahb_targets"]
        assert contract["ahb_targets"]["fixed"] == contract["apb_targets"]["fixed"]
        assert ("error_responses" in contract) == uniform
        assert set(contract["dedicated_dialects"]) >= {"apb3", "apb2", "axi4_lite", "fixed"}
        fixed = mode in so.FIXED_MODES
        # Only FIXED modes list single-transfer targets; the others keep the rule.
        assert definition.get("fixed_bursts_single") == (sorted(so.FIXED_SINGLE) if fixed else None)
        assert ("fixed_bursts" in contract) == fixed
        assert contract["apb_targets"]["fixed"].endswith("fixed_bursts_single_targets") == fixed
        # Only the shaped mode lists the AHB target's burst shaping.
        shaped = mode in so.SHAPED_MODES
        assert definition.get("ahb_bursts") == ({"t2": so.AHB_SHAPING} if shaped else None)
        assert ("ahb_bursts" in contract) == shaped
        hresp = so.AHB_HRESP["hresp_width"] if shaped else 1
        assert widths["nl_t_t2_hresp"] == hresp
        assert definition["ahb_hresp_widths"] == {"t2": hresp}
    return check


def small_address_check(sources):
    def check(mapping, out, peers):
        widths = widths_of(mapping)
        for name, protocol, _, bits in sources:
            port = "_paddr" if protocol.startswith("apb") else "_awaddr"
            assert widths["nl_i_" + name + port] == bits, (name, widths["nl_i_" + name + port])
    return check


def lite_target64_check(mapping, out, peers):
    widths = widths_of(mapping)
    assert all(widths["nl_t_t0_" + n] == w
               for n, w in [("wdata", 64), ("rdata", 64), ("wstrb", 8), ("awaddr", 8)])


def lite_unaligned_check(width):
    def check(mapping, out, peers):
        contract = mapping["definition"]["contract"]["lite_ingress"]
        assert contract["unaligned"] == \
            "address_cleared_to_bus_width_before_checks_WSTRB_as_given_read_whole_word"
        widths = widths_of(mapping)
        assert widths["nl_i_l0_wdata"] == width and widths["nl_i_l0_wstrb"] == width // 8
        assert "nl_i_l0_awlen" not in widths and "nl_i_l0_wlast" not in widths
    return check


def apb_widths_check(mapping, out, peers):
    widths = widths_of(mapping)
    for t in range(6):
        assert widths[f"nl_t_t{t}_pwdata"] == (8 if t % 2 == 0 else 16)


CASES = [dict(name="subpage_" + m, config=so.configuration(m), expect="accept", files=6,
              check=subpage_check(m)) for m in SUBPAGE_MODES]

# Invalid sub-page configurations are rejected without output.
for label, edit, code in [
        # A sub-page window is a power of two at a page start.
        ("not_power_of_two", lambda d: d["targets"][1]["mappings"][0].update(size="0xc0"),
         "NCL_ADDRESS_RANGE"),
        ("unaligned_size", lambda d: d["targets"][1]["mappings"][0].update(size="0x82"),
         "NCL_ADDRESS_RANGE"),
        # An AXI4 sub-page holds at least one target word.
        ("axi_subpage", lambda d: (d["targets"][0]["mappings"][0].update(size="0x4"),
                                   d["targets"][0].update(data_width=64)), "NCL_ADDRESS_RANGE"),
        ("unaligned_base", lambda d: d["targets"][3]["mappings"][0].update(base=hex(0xA0F0410100)),
         "NCL_ADDRESS_RANGE"),
        ("unaligned_word_base",
         lambda d: d["targets"][3]["mappings"][0].update(base=hex(0xA0F0410102)), "NCL_ADDRESS_RANGE"),
        ("wide_local", lambda d: d["targets"][1].pop("address_width"),
         "NCL_APB_BURST_OUTLET_PROFILE"),
        ("port_too_narrow", lambda d: d["targets"][2].update(address_width=9),
         "NCL_ENDPOINT_ADDRESS_WIDTH"),
        # AXI4 targets take FIXED bursts unchanged; unknown values fail.
        ("fixed_axi4", lambda d: d["targets"][0].update(fixed_bursts="single"),
         "NCL_TARGET_FIXED_BURSTS"),
        ("fixed_axi4_reject", lambda d: d["targets"][0].update(fixed_bursts="reject"),
         "NCL_TARGET_FIXED_BURSTS"),
        ("fixed_value", lambda d: d["targets"][1].update(fixed_bursts="split"),
         "NCL_TARGET_FIXED_BURSTS"),
        # Burst shaping exists only on AHB targets; unknown values fail.
        ("shaping_apb", lambda d: d["targets"][1].update(write_bursts="packet"),
         "NCL_TARGET_AHB_BURSTS"),
        ("shaping_axi4", lambda d: d["targets"][0].update(undefined_length_bursts=True),
         "NCL_TARGET_AHB_BURSTS"),
        ("shaping_lite", lambda d: d["targets"][7].update(write_bursts="complete"),
         "NCL_TARGET_AHB_BURSTS"),
        ("shaping_value", lambda d: d["targets"][2].update(write_bursts="split"),
         "NCL_TARGET_AHB_BURSTS"),
        # HRESP is 1 or 2 bits and exists only on AHB targets.
        ("hresp_apb", lambda d: d["targets"][1].update(hresp_width=1), "NCL_TARGET_AHB_HRESP"),
        ("hresp_value", lambda d: d["targets"][2].update(hresp_width=4), "NCL_TARGET_AHB_HRESP")]:
    bad = json.loads(json.dumps(so.configuration("graph")))
    edit(bad)
    CASES.append(dict(name="subpage_rejects_" + label, config=bad, expect="reject", code=code,
                      commands=("generate",)))

_small = case_module("small_address")
_lite64 = case_module("lite_target64")
_unaligned = case_module("lite_unaligned")
_apb = case_module("apb_target_widths")
for tiny in (False, True):
    config, sources = _small.configuration(tiny)
    CASES.append(dict(name="small_address" + ("_tiny" if tiny else ""), config=config,
                      expect="accept", check=small_address_check(sources)))
for clocked in (False, True):
    CASES.append(dict(name="lite_target64" + ("_clocked" if clocked else ""),
                      config=_lite64.configuration(clocked), expect="accept",
                      check=lite_target64_check))
    CASES.append(dict(name="apb_target_widths" + ("_clocked" if clocked else ""),
                      config=_apb.configuration(clocked), expect="accept",
                      check=apb_widths_check))
for width in (32, 64):
    CASES.append(dict(name=f"lite_unaligned_{width}", config=_unaligned.configuration(width),
                      expect="accept", files=6, check=lite_unaligned_check(width)))
