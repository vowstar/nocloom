"""A read beside a longer write to the same target, with separate read and
write paths and, as a companion, with shared paths (public CLI).

Initiator w0 writes and initiator r0 reads the AXI4 target t0, which keeps
four bursts (max_outstanding 4). Phase 1 measures a lone four-beat read of
r0. Phase 2 starts a 256-beat write of w0 to t0. The target holds WREADY low
in one cycle of four, so the write packet takes about 340 cycles to pass.
Once its first beat has reached t0, r0 issues the same read. Each source has its
own entry into the network and t0 has free entries, so only the paths can
make the read wait.

The oracle takes the cycles from each read's AR handshake at r0 to its last
R handshake at r0. With separate paths (the main configuration) the read
reaches t0 on the read path and the read of phase 2 must take no longer
than the read of phase 1. With shared paths (configuration "shared") the
request output to t0 is held by the write packet to its last beat, so the
read of phase 2 must take longer than the read of phase 1: the scenario
does make a read and a write meet. Both configurations also check that t0
never holds more than four bursts and holds two at some point.

Phase 3 takes turns at the AXI4 target t1, which keeps one burst. w0 keeps
four writes in flight and sends eight single-beat writes to t1, so a write
head waits at t1 whenever its entry frees. r0 reads t1 after the first write
reached it. The read must reach t1 before the last write: a write or read
that took the entry gives the next turn to the other direction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402
from nlverif import model as nlmodel  # noqa: E402

READ_ALONE, READ_BESIDE = 0x100, 0x200
TURN_AT, TURN_WRITES = 0x10000, 8
WRITE_AT, WRITE_BEATS = 0x8000, 256
LIMIT = 4000  # cycles a wait may take before it is a hang: the write takes about 340


def configuration(paths):
    config = {
        'schema_version': 10, 'name': 'rw_latency', 'address_width': 32,
        'read_write': paths,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4} for n in ('r0', 'w0')],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'max_outstanding': 4,
                     'mappings': [{'base': '0x0', 'size': '0x10000'}]},
                    {'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'mappings': [{'base': hex(TURN_AT), 'size': '0x10000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    return endpoint_ports(config, id_width=2, len_width=8)


SEPARATE, SHARED = configuration('separate'), configuration('shared')


def model(config):
    return dict(routes=nlconfig.routes(config), keeps_decerr=nlconfig.keeps_decerr(config),
                targets=nlconfig.targets(config))


def read_cycles(events, port):
    """The cycles of each read of port by its address: AR handshake to the last R handshake."""
    out, started = {}, []
    for e in events:
        if e.get("p") != port or e.get("k") != "hs":
            continue
        if e.get("ch") == "ar":
            started.append((int(e["a"], 16), e["c"]))
        elif e.get("ch") == "r" and e.get("l") and started:
            address, at = started.pop(0)
            out[address] = e["c"] - at
    return out


def turn_order(events):
    """The number of t1 writes whose AW came before the read's AR at t1, or None."""
    writes = 0
    for e in events:
        if e.get("p") == "t1" and e.get("k") == "hs" and e.get("ch") == "aw":
            writes += 1
        elif e.get("p") == "t1" and e.get("k") == "hs" and e.get("ch") == "ar":
            return writes
    return None


def oracle(checker, events, sources, targets):
    out = nlmodel.target_pool("t0", 4)(checker, events, sources, targets)
    before = turn_order(events)
    if before is None or before >= TURN_WRITES:
        out.append(dict(detail="the read did not get a turn at t1", writes_before=str(before)))
    cycles = read_cycles(events, "r0")
    alone, beside = cycles.get(READ_ALONE), cycles.get(READ_BESIDE)
    if alone is None or beside is None:
        return out + [dict(detail="a measured read did not complete", reads=str(cycles))]
    shared = checker.case.get("scenario_args", {}).get("shared", False)
    if not shared and beside > alone:
        out.append(dict(detail="the read beside the write waited longer", alone=alone,
                        beside=beside))
    if shared and beside <= alone:
        out.append(dict(detail="the read met no write on shared paths", alone=alone,
                        beside=beside))
    return out


CASE = dict(
    name="rw_latency",
    contracts=["AXI.PATHS.SEPARATE", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=SEPARATE),
    bfms=[dict(name=n, kind="axi_master", endpoint="initiator:" + n) for n in ('r0', 'w0')]
    + [dict(name=n, kind="axi_slave", endpoint="target:" + n, salt=k + 1)
       for k, n in enumerate(('t0', 't1'))],
    model=model(SEPARATE),
    max_cycles=20000,
    oracle=oracle,
    configs=[dict(label=""),
             dict(label="shared", dut=dict(source="cli", config=SHARED), model=model(SHARED),
                  scenario_args=dict(shared=True))],
    faults=[],
)


def scenario(t, seed, shared=False):
    del shared  # the oracle judges the configurations apart
    t.reset(4)
    t.delay(2)
    # Phase 1: the lone read.
    t.mark("phase", 1)
    r0 = t.snap("r0", "rlast")
    t.read("r0", READ_ALONE, beats=4, id=1)
    t.wait("r0", "rlast", "==", 1, since=r0, limit=LIMIT, label="READ_ALONE")
    t.drain(label="DRAIN_ALONE")
    # Phase 2: the same read once a long write has started at t0.
    t.mark("phase", 2)
    t.ready("t0", "w", "modulo", 4, 0)
    w0 = t.snap("t0", "w")
    b0, r0 = t.snap("w0", "b"), t.snap("r0", "rlast")
    t.write("w0", WRITE_AT, beats=WRITE_BEATS, id=2)
    t.wait("t0", "w", ">=", 1, since=w0, limit=LIMIT, label="WRITE_STARTED")
    t.read("r0", READ_BESIDE, beats=4, id=1)
    t.wait("r0", "rlast", "==", 1, since=r0, limit=LIMIT, label="READ_BESIDE")
    t.wait("w0", "b", "==", 1, since=b0, limit=LIMIT, label="WRITE_DONE")
    t.ready("t0", "w", "always")
    t.drain(label="DRAIN_BESIDE")
    # Phase 3: turns at the one entry of t1.
    t.mark("phase", 3)
    t.set("t1", "b_lat", 8)
    aw0, r0 = t.snap("t1", "aw"), t.snap("r0", "rlast")
    for k in range(TURN_WRITES):
        t.write("w0", TURN_AT + 4 * k, beats=1, id=k % 4)
    t.wait("t1", "aw", ">=", 1, since=aw0, limit=LIMIT, label="TURN_WRITES_STARTED")
    t.read("r0", TURN_AT + 0x100, beats=1, id=1)
    t.wait("r0", "rlast", "==", 1, since=r0, limit=LIMIT, label="TURN_READ")
    t.drain(label="DRAIN_TURNS")
    # Read back the write.
    t.read("w0", WRITE_AT, beats=WRITE_BEATS, id=1)
    t.drain(limit=LIMIT, label="DRAIN_READBACK")
    t.end()
