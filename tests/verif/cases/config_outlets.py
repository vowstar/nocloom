"""Configuration checks of the target outlet networks that the transaction
cases do not judge: map records and contract
text, port widths, the Core consumer of
the timeout network (the same bytes as the CLI), the artifacts of a network
without any timeout, and the inputs the CLI rejects without output."""

import copy
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def case_module(stem):
    spec = importlib.util.spec_from_file_location("nlv_config_outlets_" + stem,
                                                  HERE / (stem + ".py"))
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = {}
    spec.loader.exec_module(module)
    return module


TIMEOUTS = case_module("target_timeouts")
APB = case_module("apb_outlet")
AHB_WIDE = case_module("ahb_wide_graph")


def timeouts_check(mapping, out, peers):
    """One status output per timed target in its target's clock domain; the
    timeouts, AHB shaping and HRESP widths and the timeout contract in the
    map."""
    ports = {p["name"]: p for p in mapping["ports"]}
    timed = {t: spec[3] for t, spec in TIMEOUTS.TARGETS.items() if spec[3]}
    for name, (kind, *_) in TIMEOUTS.TARGETS.items():
        port = ports.get(f"nl_t_{name}_timed_out")
        assert (port is not None) == (name in timed), name
        if port:
            face = "paddr" if kind.startswith("apb") else "haddr" if kind == "ahb_v3" else "araddr"
            assert port["direction"] == "output" and port["width"] == 1, name
            assert port["clock"] == ports[f"nl_t_{name}_{face}"]["clock"], name
    definition = mapping["definition"]
    assert definition["target_timeouts"] == {
        t: {"cycles": c, "completion": TIMEOUTS.TARGETS[t][4]} for t, c in timed.items()}
    assert definition["ahb_bursts"] == {
        t: {"write_bursts": "packet", "undefined_length_bursts": False} for t in TIMEOUTS.SHAPED}
    # Both HRESP widths: 'ab' sets the 2-bit AMBA 2 HRESP, 'ah' keeps 1 bit.
    hresp = {t: TIMEOUTS.SHAPED.get(t, {}).get("hresp_width", 1)
             for t, entry in TIMEOUTS.TARGETS.items() if entry[0] == "ahb_v3"}
    assert hresp == {"ah": 1, "ab": 2, "qh": 1}
    assert definition["ahb_hresp_widths"] == hresp
    assert {t: ports[f"nl_t_{t}_hresp"]["width"] for t in hresp} == hresp
    contract = definition["contract"]["target_timeout"]
    assert contract["expiry"] == "target_failed_until_common_reset"
    assert contract["external_time_base"] == "unsupported"


def plain_check(mapping, out, peers):
    """Without any timeout the artifacts carry no timeout port, key or text."""
    for artifact in out.iterdir():
        text = artifact.read_text()
        assert "timed_out" not in text and "target_timeout" not in text, artifact.name


def without_timeouts(cfg):
    cfg = copy.deepcopy(cfg)
    for t in cfg["targets"]:
        t.pop("timeout", None)
    return cfg


def edited(cfg, index, **fields):
    cfg = copy.deepcopy(cfg)
    cfg["targets"][index].update(fields)
    return cfg


def cadence_check(mapping, out, peers):
    definition = mapping["definition"]
    assert definition["apb_back_to_back_targets"] == ["t1"]
    assert definition["contract"]["apb_back_to_back"]["read_response_capacity"] == 2


def ahb_wide_check(width, cut):
    """Wide AHB targets: ports of the target width, a 3-bit HSIZE, the HRESP
    widths, the source widths, the target widths, contract and shaping."""
    def check(mapping, out, peers):
        widths = {q["name"]: q["width"] for q in mapping["ports"]}
        for t in ("t0", "t1"):
            assert widths[f"nl_t_{t}_hwdata"] == widths[f"nl_t_{t}_hrdata"] == width, t
            assert widths[f"nl_t_{t}_hsize"] == 3, t
        assert widths["nl_t_t1_hresp"] == 2 and widths["nl_t_t0_hresp"] == 1
        assert widths["nl_i_s0_wdata"] == width and widths["nl_i_s1_wdata"] == 32
        assert widths["nl_i_s2_wdata"] == 2 * width
        definition = mapping["definition"]
        assert definition["target_data_widths"] == dict(
            {"t0": width, "t1": width}, **({"t3": 1024} if cut else {}))
        contract = definition["contract"]["ahb_targets"]
        assert contract["profile"] == "dedicated_no_shared_bus_retry"
        assert contract["data_width"].startswith("32_to_1024_bits")
        assert "address" in contract
        assert definition["ahb_bursts"] == {
            "t1": {"write_bursts": "complete", "undefined_length_bursts": False}}
    return check


LOGGED = {
    "schema_version": 10, "name": "logged", "address_width": 32,
    "reset_ports": [{"name": "reset_n"}], "test_ports": [{"name": "test"}],
    "clock_domains": [{"name": "main", "clock_port": "clk", "reset_port": "reset_n",
                       "test_port": "test"}],
    "initiators": [{"name": "cfg", "clock_domain": "main", "protocol": "apb4", "data_width": 32},
                   {"name": "data", "clock_domain": "main", "protocol": "apb4", "data_width": 32}],
    "targets": [{"name": "ram", "clock_domain": "main", "protocol": "apb4", "data_width": 32,
                 "attributes": "amba", "mappings": [{"base": "0x0", "size": "0x1000"}]}],
    "error_logging": {"registers": {"name": "csr", "base": "0x100000", "size": "0x100000",
                                    "clock_domain": "main"},
                      "logs": [{"name": "errors", "watch": ["ram"]}]},
    "connectivity": {"cfg": ["csr"], "data": ["ram"]},
    "output": {"backend": "verilog", "language": "verilog_2001"},
}
GRAPH = TIMEOUTS.configuration("graph")


def granule_config(**fields):
    """One AXI4 source and target in the shapes a read interleave size takes, edited."""
    target = dict(name="t0", clock_domain="main", protocol="axi4", data_width=32, attributes="amba",
                  max_outstanding=4, id_width=4, len_width=8, read_interleave_bytes=8,
                  mappings=[dict(base="0x0", size="0x8000")])
    target.update(fields)
    return dict(schema_version=10, name="granule", address_width=32,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                                    test_port="test")],
                initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                                 max_outstanding=4, id_width=2, len_width=8)],
                targets=[target], connectivity="all",
                output=dict(backend="verilog", language="verilog_2001"))


# A shared AHB timeout response must keep READY out of its response code.
TIMEOUT_RESPONSE_BYPASS = {'schema_version': 10,
 'name': 'timeout_response_bypass',
 'address_width': 16,
 'reset_ports': [{'name': 'reset_n'}],
 'test_ports': [{'name': 'test'}],
 'clock_domains': [{'name': 'main',
                    'clock_port': 'clk_main',
                    'reset_port': 'reset_n',
                    'test_port': 'test'}],
 'initiators': [{'name': 'cpu',
                 'clock_domain': 'main',
                 'protocol': 'axi4',
                 'data_width': 32,
                 'id_width': 2,
                 'len_width': 8,
                 'user_flags': {'owner': 0}}],
 'targets': [{'name': 'mem',
              'clock_domain': 'main',
              'protocol': 'axi4',
              'data_width': 64,
              'len_width': 8,
              'attributes': 'amba',
              'exclusive': 'network',
              'exclusive_id_flags': ['owner'],
              'mappings': [{'base': '0x0', 'size': '0x1000'}],
              'timeout': {'cycles': 1024, 'completion': True}},
             {'name': 'regs',
              'clock_domain': 'main',
              'protocol': 'apb4',
              'data_width': 32,
              'attributes': 'amba',
              'exclusive': 'network',
              'exclusive_id_flags': ['owner'],
              'mappings': [{'base': '0x1000', 'size': '0x100'}],
              'timeout': {'cycles': 1024}},
             {'name': 'port',
              'clock_domain': 'main',
              'protocol': 'axi4_lite',
              'data_width': 32,
              'attributes': 'amba',
              'exclusive': 'network',
              'exclusive_id_flags': ['owner'],
              'mappings': [{'base': '0x2000', 'size': '0x100'}],
              'timeout': {'cycles': 1024}},
             {'name': 'bus',
              'clock_domain': 'main',
              'protocol': 'ahb_v3',
              'data_width': 32,
              'attributes': 'amba',
              'exclusive': 'network',
              'exclusive_id_flags': ['owner'],
              'mappings': [{'base': '0x3000', 'size': '0x100'}],
              'timeout': {'cycles': 1024}}],
 'transport': {'response_codes': 'distinct', 'user_flags': ['owner']},
 'connectivity': 'all',
 'topology': {'request': {'nodes': [{'name': 'q'}],
                          'links': [],
                          'entries': [{'endpoint': 'cpu', 'node': 'q'}],
                          'exits': [{'endpoint': 'mem', 'node': 'q'},
                                    {'endpoint': 'regs', 'node': 'q'},
                                    {'endpoint': 'port', 'node': 'q'},
                                    {'endpoint': 'bus', 'node': 'q'}]},
              'response': {'nodes': [{'name': 'p'}],
                           'links': [],
                           'entries': [{'endpoint': 'mem', 'node': 'p'},
                                       {'endpoint': 'regs', 'node': 'p'},
                                       {'endpoint': 'port', 'node': 'p'},
                                       {'endpoint': 'bus', 'node': 'p'}],
                           'exits': [{'endpoint': 'cpu', 'node': 'p'}]}},
 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


CASES = [
    dict(name="ahb_timeout_response_bypass", config=TIMEOUT_RESPONSE_BYPASS,
         expect="accept", files=6, commands=("validate", "generate")),
    dict(name="timeouts_graph", config=GRAPH, expect="accept", files=6, check=timeouts_check,
         api=["{tool:timeout_public}", "{out}"]),
    dict(name="timeouts_clocked", config=TIMEOUTS.configuration("clocked"), expect="accept",
         files=6, check=timeouts_check),
    dict(name="timeouts_plain", config=without_timeouts(GRAPH), expect="accept", files=6,
         check=plain_check),
    *[dict(name="timeout_" + label, config=edited(GRAPH, index, **fields), expect="reject",
           code=code)
      for label, index, fields, code in [
          ("not_allowed", 0, dict(timeout=dict(cycles=1536)), "NCL_TARGET_TIMEOUT"),
          ("too_small", 0, dict(timeout=dict(cycles=8)), "NCL_TARGET_TIMEOUT"),
          ("too_large", 0, dict(timeout=dict(cycles=2097152)), "NCL_TARGET_TIMEOUT"),
          ("apb2", 1, dict(protocol="apb2"), "NCL_TARGET_TIMEOUT"),
          ("unknown_key", 0, dict(timeout=dict(cycles=1024, enable="x")),
           "NCL_SCHEMA_UNKNOWN_FIELD"),
          ("completion_not_boolean", 0, dict(timeout=dict(cycles=1024, completion="yes")),
           "NCL_SCHEMA_TYPE"),
          ("missing_cycles", 0, dict(timeout={}), "NCL_SCHEMA_MISSING_FIELD"),
          ("not_an_object", 0, dict(timeout=16), "NCL_SCHEMA_TYPE")]],
    # User register banks and every other name for error logging stay unknown.
    *[dict(name="registers_" + label, config={**GRAPH, label: value}, expect="reject",
           code="NCL_SCHEMA_UNKNOWN_FIELD")
      for label, value in [("register_banks", [dict(name="b", fields=[])]),
                           ("error_loggers", [dict(name="log")]), ("service", dict(route=1))]],
    dict(name="error_log", config=LOGGED, expect="accept", files=6),
    dict(name="error_log_no_target",
         config={**LOGGED, "error_logging": {**LOGGED["error_logging"],
                                             "logs": [dict(name="e", watch=["x"])]}},
         expect="reject", code="NCL_UNSUPPORTED_TOPOLOGY"),
    dict(name="error_log_unknown_name",
         config={**LOGGED, "error_logging": {**LOGGED["error_logging"],
                                             "logs": [dict(name="e", watch=["ram"], steering=1)]}},
         expect="reject", code="NCL_SCHEMA_UNKNOWN_FIELD"),
    *[dict(name=f"ahb_wide_{width}{'_cut' if cut else ''}",
           config=AHB_WIDE.configuration(width, cut), expect="accept", files=6,
           check=ahb_wide_check(width, cut))
      for width in AHB_WIDE.WIDTHS for cut in (False, True)],
    *[dict(name="read_interleave_" + label, config=edited(GRAPH, 0, **fields), expect="reject",
           code=code)
      for label, fields, code in [
          ("not_power", dict(read_interleave_bytes=12), "NCL_TARGET_READ_INTERLEAVE"),
          ("no_len_width", dict(read_interleave_bytes=0), "NCL_TARGET_READ_INTERLEAVE"),
          ("not_integer", dict(read_interleave_bytes="8"), "NCL_SCHEMA_TYPE")]],
    *[dict(name="read_interleave_domain_" + label, config=granule_config(**fields), expect="reject",
           code="NCL_TARGET_READ_INTERLEAVE")
      for label, fields in [
          ("too_large", dict(read_interleave_bytes=64, data_width=64)),
          ("not_power_of_two", dict(read_interleave_bytes=24)),
          ("below_word", dict(read_interleave_bytes=2)),
          ("axi3", dict(protocol="axi3", len_width=4)),
          ("wide_target", dict(data_width=128)),
          ("five_bursts", dict(max_outstanding=5)),
          ("five_id_bits", dict(id_width=5)),
          ("write_only", dict(access="write_only", read_interleave_bytes=0)),
          ("read_only_no_id", dict(access="read_only", id_width=0)),
          ("read_side_effects", dict(read_side_effects=True))]],
    *[dict(name=f"read_interleave_accept_{g}", config=granule_config(read_interleave_bytes=g),
           expect="accept", files=6) for g in (0, 4, 8, 16, 32)],
    dict(name="apb_cadence_clocked", config=APB.CLOCKED, expect="accept", files=6,
         check=cadence_check),
    dict(name="apb_cadence_non_apb", config=edited(APB.CLOCKED, 1, protocol="axi4_lite"),
         expect="reject", code="NCL_TARGET_APB_ACCESS"),
    dict(name="apb_cadence_invalid_bool", config=edited(APB.CLOCKED, 1, apb_back_to_back="yes"),
         expect="reject", code="NCL_SCHEMA_TYPE"),
]
