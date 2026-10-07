"""The admitted AXI4 profile and explicit rejection of unsupported input.
"""

import json
import re


def text(source=32, target=None, ports="", target_ports=""):
    """Schema 10 two-by-two crossbar with every endpoint width stated; ports
    and target_ports add fields to every initiator and target."""
    target = source if target is None else target
    return f"""schema_version: 10
name: burst_input
address_width: 32
arbitration: {{policy: round_robin}}
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains: [{{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}}]
initiators:
  - {{name: cpu0, clock_domain: main, protocol: axi4, data_width: {source}{ports}}}
  - {{name: cpu1, clock_domain: main, protocol: axi4, data_width: {source}{ports}}}
targets:
  - {{name: bank0, clock_domain: main, protocol: axi4, data_width: {target}, attributes: amba{target_ports}, mappings: [{{base: '0x0', size: '0x1000'}}]}}
  - {{name: bank1, clock_domain: main, protocol: axi4, data_width: {target}, attributes: amba{target_ports}, mappings: [{{base: '0x1000', size: '0x1000'}}]}}
connectivity: all
output: {{backend: verilog, language: verilog_2001}}
"""


TEXT = text()
CASES = []
for width in (32, 64):
    CASES.append(dict(name=f"plain_{width}", text=text(width), expect="accept", files=6))
    CASES.append(dict(
        name=f"endpoint_fields_{width}",
        text=text(width, None, ", id_width: 8, len_width: 4", ", id_width: 2, len_width: 8")
        + "transport: {max_outstanding: 1, transport_word_bytes: 4}\n",
        expect="accept", files=6))
for k, extra in enumerate((
        "transport: {id_bits: 4}\n",
        "transport: {len_bits: 8}\n",
        "transport: {max_outstanding: 65}\n",
        "transport: {transport_word_bytes: 0}\n",
        "transport: {data_width: 128}\n",
        "transport: {max_outstanding: -1}\n",
        "transport: {max_outstanding: true}\n",
        "transport: {max_outstanding: 1, max_outstanding: 2}\n",
        "transport: {unknown: 1}\n",
        "transport: []\n",
        "protocol: axi4\n",
        "data_width: 32\n",
        "unknown_option: 1\n")):
    CASES.append(dict(name=f"refused_field_{k}", text=TEXT + extra, expect="reject"))
for k, (bad, code) in enumerate((
        (TEXT.replace("protocol: axi4, ", "", 1), "NCL_SCHEMA_MISSING_FIELD"),
        (TEXT.replace(", data_width: 32}", "}", 1), "NCL_SCHEMA_MISSING_FIELD"),
        (TEXT.replace("schema_version: 10", "schema_version: 9"), "NCL_SCHEMA_VERSION"),
        (TEXT.replace(", attributes: amba", "", 1), "NCL_TARGET_ATTRIBUTES"),
        (text(32, None, ", id_width: 17"), "NCL_ENDPOINT_TRANSACTION_WIDTH"),
        (text(32, None, ", len_width: 9"), "NCL_ENDPOINT_TRANSACTION_WIDTH"),
        (text(32, None, "", ", len_width: 3"), "NCL_ENDPOINT_TRANSACTION_WIDTH"),
        (text(32, None, "", ", id_width: 32"), "NCL_ENDPOINT_TRANSACTION_WIDTH"),
        (text(32, None, ", id_width: -1"), "NCL_SCHEMA_TYPE"))):
    CASES.append(dict(name=f"refused_code_{k}", text=bad, expect="reject", code=code,
                      commands=("validate",)))
# A 32-bit AXI4 target with the default four length bits takes a 512-bit
# source (16 words a beat) and refuses a 1024-bit one; eight admit it.
CASES += [
    dict(name="source_512_target_32", text=text(512, 32), expect="accept"),
    dict(name="source_1024_target_32", text=text(1024, 32), expect="reject",
         code="NCL_ENDPOINT_TRANSACTION_WIDTH", commands=("validate",)),
    dict(name="source_1024_target_32_len8", text=text(1024, 32, "", ", len_width: 8"),
         expect="accept"),
]
# APB4 and AXI4-Lite targets take 128-byte packets of any source.
for protocol in ("apb4", "axi4_lite"):
    CASES.append(dict(name=f"dedicated_{protocol}", expect="accept", commands=("generate",),
                      text=text(1024, 32).replace("protocol: axi4, data_width: 32",
                                                  f"protocol: {protocol}, data_width: 32")))
for width in (0, 16, 128, 256, 1024):
    CASES.append(dict(name=f"crossbar_width_{width}", text=text(width), expect="reject",
                      commands=("validate",)))

# output.register_reset: synchronous clears every register on its clock edge (no reset edge in
# any event control), with the synchronizer cell under its own name; asynchronous is the default,
# byte for byte; other values are refused.
SYNC_CELL = "nocloom_synchronizer_sync_reset"


def synchronous_check(mapping, out, peers):
    bundle = json.loads((out / "nocloom_burst_input.bundle.json").read_text())
    assert mapping["definition"].get("register_reset") == "synchronous", "register_reset recorded"
    assert mapping["definition"] == bundle["definition"], "definition identity"
    assert bundle["simulation_sources"] == [
        "nocloom_clock_gate.v", SYNC_CELL + ".v", "nocloom_burst_input.v"], "sources"
    rtl = "".join((out / f).read_text() for f in bundle["simulation_sources"])
    controls = re.findall(r"^always @\((posedge .*)\)", rtl, re.M)
    assert controls and all(re.fullmatch(r"posedge \w+", c) for c in controls), "edge only"
    assert re.search(rf"^{SYNC_CELL} #\(\.STAGES\(2\)\) ", rtl, re.M), "synchronizer cell"
    assert "nocloom_synchronizer " not in rtl, "no asynchronous synchronizer"


def sync_text(value):
    return TEXT.replace("language: verilog_2001}", f"language: verilog_2001, register_reset: {value}}}")
CASES += [
    dict(name="register_reset_synchronous", text=sync_text("synchronous"), expect="accept",
         files=6, check=synchronous_check,
         mutations=[("unrecorded", lambda d: [
             f.write_text(f.read_text().replace('"register_reset": "synchronous",\n', ""))
             for f in d.glob("*.map.json")], "register_reset recorded"),
                    ("reset_edge", lambda d: [
             f.write_text(re.sub(r"^always @\(posedge (\w+)\)", r"always @(posedge \1 or negedge "
                                 "reset_n)", f.read_text(), count=1, flags=re.M))
             for f in d.glob("nocloom_burst_input.v")], "edge only")]),
    dict(name="register_reset_asynchronous", text=sync_text("asynchronous"),
         expect="accept", same_as="plain_32"),
    dict(name="register_reset_unknown", text=sync_text("sync"), expect="reject",
         code="NCL_SCHEMA_TYPE", commands=("validate",)),
]
