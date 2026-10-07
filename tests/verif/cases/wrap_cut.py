"""Every WRAP of a source at least as wide as its AXI4 target, on the public schema 10 path (CLI):
every container and every start, written and read, with a target AxLEN too short for the largest
container as one burst. A long WRAP must reach the target as bursts its AxLEN can carry, with WLAST
where each ends, and read back with its data and RLAST beat by beat.

The offline checker judges every source beat against the target words of its read, and the monitors
judge WLAST and RLAST at the target; the drain judges completion. Negative control (RTL text fault):
t0's RLAST input reads one for every beat."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

PARAMS = globals().get("PARAMS", {})
SOURCE_BITS = int(PARAMS.get("source_bits", "64"))
TARGET_BITS = int(PARAMS.get("target_bits", "32"))
TARGET_LEN = int(PARAMS.get("target_len", "4"))
BURSTS = int(PARAMS.get("bursts", "4"))
TARGET_BURSTS = int(PARAMS.get("target_bursts", "4"))
SOURCE_BYTES = SOURCE_BITS // 8

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'wrap_sweep', 'address_width': 32,
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                    'data_width': SOURCE_BITS, 'max_outstanding': BURSTS, 'id_width': 2,
                    'len_width': 8}],
    'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4',
                 'data_width': TARGET_BITS, 'attributes': 'amba', 'len_width': TARGET_LEN,
                 'max_outstanding': TARGET_BURSTS,
                 'mappings': [{'base': '0x0', 'size': '0x100000'}]}],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}})

def rlast_one(rtl, _map):
    """Every use of t0's RLAST input reads one."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rlast\b', r"\g<1>1'd1", len(uses))]


CASE = dict(
    name=f"wrap_cut_{SOURCE_BITS}_{TARGET_LEN}",
    contracts=["AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=400000,
    faults=[
        dict(id="rlast_one", op="text", subs=rlast_one, contract="AXI.ROUTE.LOCAL_OFFSET",
             expect=["SOURCE_R_DATA", "SOURCE_R_RESP", "AXI.R.LAST", "SOURCE_R_LAST",
                     "SOURCE_R_COUNT", "TAPE.DRAIN_TIMEOUT:r2_0", "SOURCE_UNANSWERED"]),
    ],
)


def scenario(t, seed):
    t.reset(4)
    t.delay(2)
    size = {4: 2, 8: 3, 16: 4, 32: 5}[SOURCE_BYTES]
    area = 0x1000
    k = 0
    for beats in (2, 4, 8, 16):
        for start in range(beats):
            # A page of its own per burst: the container never crosses it.
            addr = area + start * SOURCE_BYTES
            t.write("s0", addr, beats=beats, size=size, burst="wrap", id=k % 4, cache=3)
            t.drain(limit=4000, label=f"w{beats}_{start}")
            t.read("s0", addr, beats=beats, size=size, burst="wrap", id=(k + 1) % 4, cache=3)
            t.drain(limit=4000, label=f"r{beats}_{start}")
            area += 0x1000
            k += 1
    t.end()
