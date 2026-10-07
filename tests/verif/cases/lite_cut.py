"""A 64-bit AXI4-Lite source with four transactions in flight pipelines
single beats through narrow conversion (public CLI).

Eight writes (full strobes, then 0xa5) and eight reads to consecutive
words; every target burst has the shape of its target width (two 32-bit
beats or one 64-bit beat), targets answer after 24 cycles, and at least
three transactions of each direction must reach the source face (a
two-word packets pipe holds the write words the targets have not taken)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402
from nlverif import model as nlmodel  # noqa: E402


def configuration(clocked, target_outstanding=1, exact=False):
    widths = {'t0': 32, **({'t1': 64} if clocked else {})}
    config = {'schema_version': 10, 'name': 'lite_cut', 'address_width': 32,
              'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
              'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4_lite',
                              'data_width': 64, 'max_outstanding': 4,
                              'pipes': {'packets': {'forward': True, 'backward': True}}}],
              'targets': [{'name': n, 'clock_domain': 'far' if clocked else 'main',
                           'protocol': 'axi4', 'data_width': bits,
                           'mappings': [{'base': hex(i * 65536), 'size': '0x10000'}]}
                          for i, (n, bits) in enumerate(widths.items())],
              'transport': {'transport_word_bytes': 4},
              'connectivity': 'all',
              'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if target_outstanding > 1:
        config['targets'][0]['max_outstanding'] = target_outstanding
    if exact:
        config['targets'][0]['read_side_effects'] = True
    if clocked:
        config['clock_domains'] = [{'name': n, 'clock_port': 'clk_' + n, 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': p}
                                   for n, p in [('main', 10000), ('far', 14000)]]
        config['topology'] = {
            'request': {'nodes': [{'name': 'q0', 'clock_domain': 'main'}], 'links': [],
                        'entries': [{'endpoint': 's0', 'node': 'q0'}],
                        'exits': [{'endpoint': t, 'node': 'q0'} for t in widths]},
            'response': {'nodes': [{'name': 'p0', 'clock_domain': 'far'}], 'links': [],
                         'entries': [{'endpoint': t, 'node': 'p0'} for t in widths],
                         'exits': [{'endpoint': 's0', 'node': 'p0'}]}}
    return endpoint_ports(config, len_width=8)


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, error_data_zero=True))


def bfms(clocked):
    return ([dict(name="s0", kind="axi_master", endpoint="initiator:s0", lite=True),
             dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=1,
                  clock="far" if clocked else "main")]
            + ([dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=2, clock="far")]
               if clocked else []))


PLAIN, CLOCKED, POOLED = configuration(False), configuration(True), configuration(False, 2)
EXACT = configuration(False, exact=True)
CASE = dict(
    name="lite_cut",
    contracts=["AXI.LITE.WORD_ACCESS", "AXI.WIDTH.DOWNSIZE.PIECES", "AXI.OUTSTANDING.REACH",
               "AXI.W.UNSTROBED_ZERO"],
    dut=dict(source="cli", config=PLAIN),
    bfms=bfms(False),
    model=model(PLAIN),
    configs=[dict(label=""),
             dict(label="clocked", dut=dict(source="cli", config=CLOCKED), model=model(CLOCKED),
                  bfms=bfms(True), clocks=dict(main=10.0, far=14.0),
                  scenario_args=dict(clocked=True)),
             dict(label="target2", dut=dict(source="cli", config=POOLED), model=model(POOLED),
                  oracle=nlmodel.target_pool("t0", 2)),
             dict(label="exact", dut=dict(source="cli", config=EXACT), model=model(EXACT))],
    max_cycles=20000,
    faults=[],
)


def scenario(t, seed, clocked=False):
    targets = ("t0", "t1") if clocked else ("t0",)
    for target in targets:
        t.set(target, "b_lat", 24)
        t.set(target, "r_lat", 24)
    t.strobes("s0", [0xa5])
    t.reset(8)
    t.delay(12)

    def place(i):
        address = ((0 if i < 4 else 65536) if clocked else 0) + i * 8
        wide = clocked and i >= 4
        # A 32-bit target takes two beats, a 64-bit one one beat.
        shape = [(address % 65536, 0, 3, 1)] if wide else [(address % 65536, 1, 2, 1)]
        return address, shape
    for i in range(8):
        address, shape = place(i)
        t.write("s0", address, strobe="full" if i % 2 == 0 else "table", descriptors=shape)
        t.read("s0", address, descriptors=shape)
    t.drain(limit=10000, label="ALL_DONE")
    t.end()
    for d in ("w", "r"):
        t.check(kind="reach", port="s0", dir=d, inflight=3)
