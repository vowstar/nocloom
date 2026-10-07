"""An AXI4 default target answers a write miss as the miss enters, on the public schema 10 path (CLI).

An unmapped one-beat write has its B a fixed number of cycles after the request, at a target of
capacity one (the burst's own entry) and of capacity four (no older write of its ID and a free
response stage), and a two-beat write has its B after the last W beat. The miss never reaches the
target (the offline checker sees DECERR and no target access), and writes into the window on both
sides of it still complete.

Negative control: the bound one cycle tighter must time out at MISS_B, so the wait resolves the
single cycle this case exists for. A build that answers after draining the beat fails MISS_B."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

MISS = 0x8000
# Cycles from the write's issue to its B: one beat, then two beats with W following AW.
BOUND = dict(one_beat=5, two_beat=6)


def config(capacity):
    return endpoint_ports({
        'schema_version': 10, 'name': f'axi4_write_miss_{capacity}', 'address_width': 16,
        'transport': {},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': capacity}],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'max_outstanding': capacity,
                     'mappings': [{'base': '0x0', 'size': '0x1000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=1, len_width=8)


def case_for(capacity):
    cfg = config(capacity)
    return dict(
        dut=dict(source="cli", config=cfg),
        bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
              dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=0)],
        model=dict(routes=nlconfig.routes(cfg), error_codes=True,
                   targets=nlconfig.targets(cfg, cache="amba", prot=True)),
        max_cycles=4000,
    )


CASE = dict(
    name="axi4_write_miss_answer",
    contracts=["AXI.ERR.UNMAPPED_DECERR"],
    configs=[dict(label="capacity1", **case_for(1)), dict(label="capacity4", **case_for(4))],
    faults=[dict(id="tighter_bound", op="scenario", params=dict(slack=1),
                 expect=["TAPE.WAIT_TIMEOUT:MISS_B"], contract="AXI.ERR.UNMAPPED_DECERR",
                 note="one cycle fewer than the miss answer takes")],
)


def scenario(t, seed, slack=0):
    t.reset(4)
    t.delay(4)
    for address, beats, name in ((MISS, 1, "one_beat"), (MISS + 0x100, 2, "two_beat")):
        for repeat in range(3):
            snap = t.snap("s0", "b")
            t.write("s0", address + 0x10 * repeat, beats=beats, id=repeat % 2)
            t.wait("s0", "b", "==", 1, limit=BOUND[name] - slack, label="MISS_B", since=snap)
            t.delay(6)
    t.write("s0", 0x100, beats=4, id=0)
    t.write("s0", 0x200, beats=1, id=1)
    t.drain(limit=2000, label="drain")
    t.end()
