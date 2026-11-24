"""Network exclusive access at a one-entry AXI4 target with shared and separate
read and write paths (public CLI).

AXI4 sources s0 and s1 keep four bursts each. The AXI4 target t0 keeps one
burst and has a network exclusive monitor. Each exclusive write starts only
after the R of its own exclusive read reached s0 (AXI A7.2.2): the wait
counts every read of s0 since the phase began, so an earlier read cannot end
it.
  1. Two reads of s0 wait at t0 ahead of the exclusive read. The exclusive
     write must succeed (EXOKAY and its bytes written).
  2. Two writes of s0 are ahead of the exclusive read, and s1 reads t0
     beside it. The exclusive write must succeed.
  3. After the exclusive read, s1 writes the reserved bytes. The exclusive
     write must fail (OKAY, no byte written).
With separate paths a write can pass a read that waits for t0, so these
phases check that a source that keeps the A7.2.2 order still gets the
monitor's answer with both path configurations.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

LIMIT = 4000  # cycles a wait may take before it is a hang: each phase needs under 400
EXOKAY, OKAY = 1, 0


def configuration(paths):
    config = {
        'schema_version': 10, 'name': 'rw_exclusive', 'address_width': 32,
        'read_write': paths,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4} for n in ('s0', 's1')],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'max_outstanding': 1, 'exclusive': 'network', 'attributes': 'amba',
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]},
                    {'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'mappings': [{'base': '0x10000', 'size': '0x10000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    return endpoint_ports(config, id_width=2, len_width=8)


SHARED, SEPARATE = configuration('shared'), configuration('separate')


def model(config):
    return dict(routes=nlconfig.routes(config), keeps_decerr=nlconfig.keeps_decerr(config),
                targets=nlconfig.targets(config, error_data_zero=True))


BFMS = [dict(name=n, kind="axi_master", endpoint="initiator:" + n) for n in ('s0', 's1')]
BFMS += [dict(name=n, kind="axi_slave", endpoint="target:" + n, salt=k + 1)
         for k, n in enumerate(('t0', 't1'))]
CASE = dict(
    name="rw_exclusive",
    contracts=["AXI.PATHS.SEPARATE", "AXI.EXCL.MONITOR"],
    dut=dict(source="cli", config=SHARED),
    bfms=BFMS,
    model=model(SHARED),
    max_cycles=20000,
    configs=[dict(label=""),
             dict(label="separate", dut=dict(source="cli", config=SEPARATE),
                  model=model(SEPARATE))],
    faults=[],
)


def exclusive_pair(t, since, addr, axid, reads_before, resp, label, between=None):
    """An exclusive read of s0, a wait for its own R (s0 R bursts counted from snapshot since),
    then the exclusive write."""
    t.read("s0", addr, beats=1, id=axid, lock=1, resp=EXOKAY)
    # reads_before reads of s0 are still open: the R of the exclusive read is the last of them.
    t.wait("s0", "rlast", ">=", reads_before + 1, since=since, limit=LIMIT,
           label=label + "_EXCLUSIVE_READ")
    if between:
        between()
    expect = dict(resp=resp) if resp == EXOKAY else dict(resp=resp, target="any")
    t.write("s0", addr, beats=1, id=axid, lock=1, **expect)
    t.drain(limit=LIMIT, label=label)


def scenario(t, seed):
    del seed
    t.reset(4)
    t.delay(2)
    t.set("t0", "r_lat", 20)
    t.set("t0", "b_lat", 20)
    # 1: reads ahead of the exclusive read at the one-entry target.
    t.mark("phase", 1)
    r0 = t.snap("s0", "rlast")
    t.read("s0", 0x100, beats=1, id=0)
    t.read("s0", 0x140, beats=4, id=1)
    exclusive_pair(t, r0, 0x800, 3, 2, EXOKAY, "READS_AHEAD")
    # 2: writes of s0 ahead and a read of s1 beside the exclusive read.
    t.mark("phase", 2)
    r0 = t.snap("s0", "rlast")
    t.write("s0", 0x200, beats=4, id=0)
    t.write("s0", 0x240, beats=1, id=1)
    t.read("s1", 0x300, beats=4, id=0)
    exclusive_pair(t, r0, 0x840, 2, 0, EXOKAY, "WRITES_AHEAD")
    # 3: s1 writes the reserved bytes between the pair: the exclusive write fails.
    t.mark("phase", 3)
    r0 = t.snap("s0", "rlast")

    def intervene():
        b1 = t.snap("s1", "b")
        t.write("s1", 0x880, beats=1, id=2)
        t.wait("s1", "b", "==", 1, since=b1, limit=LIMIT, label="INTERVENING_WRITE")

    exclusive_pair(t, r0, 0x880, 1, 0, OKAY, "INTERVENED", between=intervene)
    t.end()
