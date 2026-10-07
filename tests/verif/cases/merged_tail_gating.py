"""An AXI4 target behind per-target merging with several bursts in flight and clock gating returns a
read's pool entry after the merge's last beat of it, on the public schema 10 path (CLI).

Words fan out to their source beats after they left the outlet, and the outlet's protocol zone may
rest meanwhile; the entry must still return to the pool or the target stops taking bursts. Sources
s0 and s1 (32 bits) read long INCR bursts and WRAP bursts of every container size and start beat
from t0 (256 bits) and t1 (32 bits), two bursts in flight per target, with R on a share of the
cycles. The drain judges completion, the offline checker the data, RLAST and the codes of every
source beat.

Negative control: the same case on a build without the zone's wake on the read tail times out at
READS_DONE (no RTL text fault can name the gating term)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

TARGETS = (('t0', 0x0000, 256), ('t1', 0x8000, 32))
SOURCES = ('s0', 's1')

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'merged_tail_gating', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
    'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                       'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}
                   for n in SOURCES],
    'targets': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w,
                 'attributes': 'amba', 'max_outstanding': 2,
                 'mappings': [{'base': hex(base), 'size': '0x8000'}]} for n, base, w in TARGETS],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)

CASE = dict(
    name="merged_tail_gating",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.OUTLET.FRAMING"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in SOURCES]
    + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
       for k, (n, _, _) in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=60000,
)

LONG = (1, 2, 4, 8, 3, 6, 5, 7, 12, 16, 24, 32)
WRAPS = (2, 4, 8, 16)


def scenario(t, seed):
    for n, _, _ in TARGETS:
        t.set(n, "r_lat", 2)
    t.reset(4)
    t.delay(2)
    t.mark("phase", 1)
    for n, _, _ in TARGETS:
        t.set(n, "r_rate", 40)
    for k, beats in enumerate(LONG):
        for si, s in enumerate(SOURCES):
            for _, base, _ in TARGETS:
                t.read(s, base + 0x1000 * si + 0x100 * k, beats=beats, id=(k + si) % 4, cache=3)
    for s in SOURCES:
        t.wait(s, "rlast", "==", 2 * len(LONG), limit=20000, label="READS_DONE")
    t.mark("phase", 2)
    for k, beats in enumerate(WRAPS):
        for start in range(beats):
            for _, base, _ in TARGETS:
                t.read("s0", base + 0x100 * k + 4 * start, beats=beats, burst="wrap", id=start % 4,
                       cache=3)
                t.write("s1", base + 0x1800 + 0x100 * k, beats=beats, id=start % 4, cache=3)
    t.drain(limit=25000, label="drain")
    t.end()
