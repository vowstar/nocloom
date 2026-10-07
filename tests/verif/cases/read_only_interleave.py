"""A 64-bit AXI4 source reads a read-only 32-bit AXI4 target that keeps four bursts in flight and
alternates read data by ID, on the public schema 10 path (CLI).

The target has no ID width of its own, so the plan gives it one. It interleaves by that ID, and the
response assembler on the wider link must end a run at every change of ID or two reads' words mix.
Every read must complete with the exact data; writes to the read-only target run alongside them and
take the target's local answer.

Negative control (RTL text fault): the assembler ignores the segment end, so a word of one ID gathers
with a word of another and the checker reports wrong data."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'read_only_interleave', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 64,
                    'max_outstanding': 4}],
    'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                 'attributes': 'amba', 'max_outstanding': 4, 'access': 'read_only',
                 'mappings': [{'base': '0x0', 'size': '0x8000'}]}],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def no_segment_end(rtl, map_):
    """Keep packet tails and word tops, but ignore both fragment end markers."""
    assign = re.compile(r'^assign (nl_internal_v\d+) = \((nl_internal_v\d+) ([&|]) (nl_internal_v\d+)\);$',
                        re.M)
    binary = {m[1]: (m[2], m[3], m[4]) for m in assign.finditer(rtl)}
    subs = []
    for store in map_["network"]["storage"]:
        if not store["resource"].startswith("adapter/rsp/"):
            continue
        accepted, _, _ = binary["nl_internal_v" + store["release"].split("/")[1]]
        _, _, offered = binary[accepted]
        _, _, ends = binary[offered]
        rest, op, cut = binary[ends]
        assert op == "|"
        last, op, inner = binary[rest]
        assert op == "|"
        wrap_end, op, top = binary[inner]
        assert op == "|"
        subs.append((rf'^assign {ends} = \({rest} \| {cut}\);$',
                     f'assign {ends} = ({last} | {top});', 1))
    assert len(subs) == 1
    return subs


CASE = dict(
    name="read_only_interleave",
    contracts=["AXI.TARGET.R_INTERLEAVE"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=40000,
    seeds=[1, 2, 3, 4],
    faults=[
        dict(id="no_segment_end", op="text", subs=no_segment_end,
             contract="AXI.TARGET.R_INTERLEAVE", expect=["SOURCE_R_DATA"]),
    ],
)


def reads(t, beats_of, round_, writes):
    for k, beats in enumerate(beats_of):
        t.read("s0", 0x400 * round_ + 0x100 * k, beats=beats, id=k % 4, cache=3)
        if writes:
            t.write("s0", 0x4000 + 0x400 * round_ + 0x100 * k, beats=1 + k % 3, id=k % 4, cache=3)


def scenario(t, seed):
    t.set("t0", "r_lat", 6)
    t.set("t0", "seed", 0x1357 * seed + 0x30)
    t.reset(4)
    t.delay(2)
    # Whole bursts first: no alternation yet.
    t.mark("phase", 1)
    for k, beats in enumerate((2, 1, 3)):
        t.read("s0", 0x100 * k, beats=beats, id=0, cache=3)
    t.drain(limit=8000, label="whole")
    # The target alternates reads by ID, word by word.
    t.mark("phase", 2)
    t.set("t0", "r_ilv", 1)
    t.set("t0", "r_lat", 16)
    for r in range(3):
        reads(t, (2, 4, 3, 2), r, False)
    t.drain(limit=8000, label="alternate")
    # The same with pauses at the target, a slow source and writes alongside.
    t.mark("phase", 3)
    t.set("t0", "r_rate", 55)
    t.set("t0", "r_lat", 2)
    t.ready("s0", "r", "modulo", 3, 0)
    for r in range(3):
        reads(t, (1, 2, 4, 3), r, True)
    t.drain(limit=8000, label="paced")
    t.end()
