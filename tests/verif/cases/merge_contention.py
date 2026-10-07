"""Two sources of different widths write merged bursts to one wider AXI4
target while the target holds WREADY low before every burst's last beat.


Sources s0 (32 bits) and s1 (64 bits) reach t0 (128 bits) in per-target
width mode; every burst is Modifiable, so the target-side merge packs it into
128-bit words. Each source sends six back-to-back eight-beat INCR writes and
reads them back once all their B have returned. The target takes AW at once,
stalls W for 3 + (burst number % 4) cycles before the last beat of each burst
(so the next source's burst reaches the merge while the previous last word
waits) and answers B the cycle after the last W beat. The offline checker
judges every strobed target byte (exactly once), the footprint, B and R
codes and the read-back data; the monitors judge holds, WLAST and
unexpected responses; the drain judges completion."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

SOURCES = (('s0', 32, 0x0000), ('s1', 64, 0x8000))
BURSTS, BEATS = 6, 8

CONFIG = endpoint_ports({
    'schema_version': 10, 'name': 'contend', 'address_width': 32,
    'arbitration': {'policy': 'round_robin'},
    'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
    'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': w}
                   for n, w, _ in SOURCES],
    'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128,
                 'mappings': [{'base': '0x0', 'size': '0x10000'}]}],
    'connectivity': 'all',
    'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)

CASE = dict(
    name="merge_contention",
    contracts=["AXI.WIDTH.UPSIZE.MERGE", "AXI.ROUTE.LOCAL_OFFSET", "AXI.W.UNSTROBED_ZERO"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n, _, _ in SOURCES]
    + [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=1)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=40000,
    faults=[],
)


def scenario(t, seed):
    t.ready("t0", "w", "last_hold", 3, 4)
    t.reset(4)
    t.delay(2)
    for k in range(BURSTS):
        for n, bits, base in SOURCES:
            t.write(n, base + k * 256, beats=BEATS, id=1, cache=3, prot=0)
    for n, _, _ in SOURCES:
        t.wait(n, "b", "==", BURSTS, limit=20000, label="WRITES_DONE")
    for k in range(BURSTS):
        for n, bits, base in SOURCES:
            t.read(n, base + k * 256, beats=BEATS, id=2, cache=3, prot=0)
    t.drain(limit=20000, label="COMPLETION")
    t.end()
