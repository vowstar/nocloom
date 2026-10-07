"""Connectivity matrices through the CLI (three AXI4 sources, three AXI4
targets, no topology: one generated switch per direction whose outputs take
only the connected sources).

Every matrix is one configuration of this case (the band matrix of
examples/axi4_graph_selective.yaml first; the negative control runs on it).
Each source reaches every target in three rotating rounds of concurrent
writes and reads: a connected pair completes at its target; an unconnected
pair answers DECERR from the default target (zero read data) and no target
sees it (the model routes only the connected windows). A common reset cuts a burst
in flight. On the band matrix every target then holds its address and W
ready low while s1 parks a write on t1 (its read queues behind it at the
source: reads and writes share the source's one burst in flight), and s0's
bursts to t2's window (unconnected) and to an unmapped address must answer
within 64 cycles each while t0 and t2 stay quiet: only a source-side
completion meets the bound.

Designated faults: forced_admission (the admission match of s0's writes held
true: the burst queues behind the stalled target and misses the bound), and
two oracle controls (scenario faults: the tape expects a wider or a narrower
matrix than the generated one, and the checker must say so). The
configuration checks (routes in the map, one generated switch, equality with
an explicit one-node graph) are in config_connectivity.py.

The "crossed" configuration (negative_run: it runs in the negative case
after the main one) carries the band matrix, stall phase included, over an
explicit topology whose request and response graphs each hold two crossed
links (a -> b and b -> a, x -> y and y -> x): every route takes at most one
hop, so no route chains the cycle and the configuration is valid."""

import random

from nlverif import config as nlconfig

ALL = {(s, t) for s in range(3) for t in range(3)}
BAND = {(s, t) for s in range(3) for t in range(3) if t in (s, (s + 1) % 3)}
SEED = 20261001
UNMAPPED = 0x80000000


def valid(matrix):
    return ((0, 0) in matrix and all(any(p[0] == s for p in matrix) for s in range(3)) and
            all(any(p[1] == t for p in matrix) for t in range(3)))


def random_matrices(count):
    rng = random.Random(SEED)
    found = []
    while len(found) < count:
        matrix = {p for p in ALL if rng.random() < 0.5} | {(0, 0)}
        if valid(matrix) and matrix not in found and matrix != ALL:
            found.append(matrix)
    return found


MATRICES = {
    'band': BAND,
    'identity': {(0, 0), (1, 1), (2, 2)},
    'row': {(0, 0), (0, 1), (0, 2), (1, 1), (2, 2)},
    'column': {(0, 0), (1, 0), (2, 0), (0, 1), (2, 2)},
    'all': ALL,
    **{f'random{i}': m for i, m in enumerate(random_matrices(2))},
}


# Band routes over crossed links: s0 -> t1, s1 -> t1 and s1 -> t2 take a -> b,
# s2 -> t0 takes b -> a; t0 -> s2 takes x -> y, t1 -> s0 takes y -> x.
CROSSED = {
    'request': {'nodes': [{'name': 'a'}, {'name': 'b'}],
                'links': [{'from': 'a', 'to': 'b'}, {'from': 'b', 'to': 'a'}],
                'entries': [{'endpoint': 's0', 'node': 'a'}, {'endpoint': 's1', 'node': 'a'},
                            {'endpoint': 's2', 'node': 'b'}],
                'exits': [{'endpoint': 't0', 'node': 'a'}, {'endpoint': 't1', 'node': 'b'},
                          {'endpoint': 't2', 'node': 'b'}]},
    'response': {'nodes': [{'name': 'x'}, {'name': 'y'}],
                 'links': [{'from': 'x', 'to': 'y'}, {'from': 'y', 'to': 'x'}],
                 'entries': [{'endpoint': 't0', 'node': 'x'}, {'endpoint': 't1', 'node': 'y'},
                             {'endpoint': 't2', 'node': 'y'}],
                 'exits': [{'endpoint': 's0', 'node': 'x'}, {'endpoint': 's1', 'node': 'y'},
                           {'endpoint': 's2', 'node': 'y'}]}}


def configuration(matrix, topology=None):
    connectivity = 'all' if matrix == ALL else {
        f's{s}': [f't{t}' for t in range(3) if (s, t) in matrix] for s in range(3)}
    return {
        'schema_version': 10, 'name': 'matrix', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [{'name': f's{i}', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': 32, 'len_width': 8} for i in range(3)],
        'targets': [{'name': f't{i}', 'clock_domain': 'main', 'protocol': 'axi4',
                     'data_width': 32, 'len_width': 8, 'attributes': 'amba',
                     'mappings': [{'base': hex(65536 * i), 'size': '0x10000'}]} for i in range(3)],
        'transport': {'max_outstanding': 1, 'transport_word_bytes': 4},
        'connectivity': connectivity, **({'topology': topology} if topology else {}),
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


def model(matrix):
    cfg = configuration(matrix)  # the routes do not depend on the topology
    return dict(routes=nlconfig.routes(cfg), error_codes=True,
                targets=nlconfig.targets(cfg, cache="amba", prot=True))


def config_of(name):
    matrix = MATRICES[name]
    return dict(label=name if name != 'band' else "",
                dut=dict(source="cli", config=configuration(matrix)), model=model(matrix),
                scenario_args=dict(matrix=sorted(matrix), stall=name == 'band'))


def forced_admission(rtl, mapping):
    """The admission match of s0's writes, held true (anchored on the net
    the map names)."""
    admission = next(a for a in mapping['network']['admission'] if a['source'] == 's0')
    net = 'nl_internal_v' + admission['write_route_match'].split('/')[1]
    return [(r'^(assign ' + net + r' = )([^;]+);', r"\g<1>1'b1;", 1)]


BAND_CONFIG = config_of('band')
CROSSED_CONFIG = dict(label="crossed", negative_run=True,
                      dut=dict(source="cli", config=configuration(BAND, CROSSED)),
                      model=model(BAND), scenario_args=dict(matrix=sorted(BAND), stall=True))
CASE = dict(
    name="connectivity",
    contracts=["AXI.ROUTE.CONNECTIVITY", "AXI.ERR.UNMAPPED_DECERR", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.RESET.CANCEL"],
    dut=BAND_CONFIG["dut"],
    model=BAND_CONFIG["model"],
    scenario_args=BAND_CONFIG["scenario_args"],
    bfms=[dict(name=f"s{i}", kind="axi_master", endpoint=f"initiator:s{i}") for i in range(3)]
    + [dict(name=f"t{i}", kind="axi_slave", endpoint=f"target:t{i}", salt=i) for i in range(3)],
    configs=[config_of(name) for name in MATRICES] + [CROSSED_CONFIG],
    max_cycles=60000,
    faults=[
        dict(id="forced_admission", op="text", subs=forced_admission,
             contract="AXI.ROUTE.CONNECTIVITY",
             expect=["TAPE.WAIT_TIMEOUT:DENIED_LATENCY", "CONTRACT.QUIET.AW",
                     "CONTRACT.QUIET.W", "TARGET_ACCESS"]),
        dict(id="oracle_wider", op="scenario", params=dict(oracle="all"),
             contract="AXI.ROUTE.CONNECTIVITY", expect=["SOURCE_B_RESP"]),
        dict(id="oracle_narrower", op="scenario", params=dict(oracle="narrow"),
             contract="AXI.ROUTE.CONNECTIVITY", expect=["TARGET_ACCESS"]),
    ],
)


def scenario(t, seed, matrix=(), stall=False, oracle=None):
    matrix = {tuple(p) for p in matrix}
    t.reset(4)
    t.delay(2)
    t.mark("phase", 0)
    # Three rotating rounds: every source reaches every target once, the
    # three sources at once on three different targets.
    for r in range(3):
        for s in range(3):
            target = (s + r) % 3
            base = 0x10000 * target + 0x100 * s + 0x40 * r
            expect = {}
            if oracle == "all" and (s, target) not in matrix:
                expect = dict(resp=0)          # wrongly expected connected
            if oracle == "narrow" and (s, target) == (0, 1) and (s, target) in matrix:
                expect = dict(resp=3, target="none")   # wrongly expected denied
            t.write(f"s{s}", base, beats=4, id=s + 1, cache=3, prot=s, **expect)
            t.read(f"s{s}", base, beats=4, id=s + 1, cache=3, prot=s, **expect)
        t.drain(limit=4000, label=f"ROUND_{r}")
    # A common reset with a burst of s0 in flight at t0 (always connected).
    t.mark("phase", 1)
    t.ready("t0", "w", "modulo", 3, 0)
    m = t.snap("t0", "w")
    t.write("s0", 0x800, beats=16, id=1)
    t.wait("t0", "w", ">=", 2, since=m, limit=400, label="RESET_MID_PACKET")
    t.reset(4)
    t.ready("t0", "w", "always")
    t.delay(2)
    if stall:
        stall_phase(t)
    t.end()


def stall_phase(t):
    t.mark("phase", 2)
    for target in ("t0", "t1", "t2"):
        for ch in ("aw", "w", "ar"):
            t.ready(target, ch, "never")
    for target in ("t0", "t2"):
        for ch in ("aw", "w", "ar"):
            t.quiet(target, ch, True)
    t.write("s1", 0x10000, beats=1, id=1)
    # The write reaches t1 first; the read is offered after it and waits at
    # s1 behind the write (reads and writes share the source's one burst in
    # flight), so t1 never sees its AR while the write is parked.
    t.wait("t1", "awvalid", "==", 1, limit=40, label="STALL_REACHED")
    t.read("s1", 0x10100, beats=1, id=1)
    for address, axid in ((0x20000, 3), (UNMAPPED, 5)):
        b, r = t.snap("s0", "b"), t.snap("s0", "rlast")
        t.write("s0", address, beats=4, id=axid)
        t.wait("s0", "b", "==", 1, since=b, limit=64, label="DENIED_LATENCY")
        t.read("s0", address, beats=4, id=axid)
        t.wait("s0", "rlast", "==", 1, since=r, limit=64, label="DENIED_LATENCY")
    t.expect_now("t1", "awvalid", "==", 1, "STALL_RELEASED")
    for target in ("t0", "t2"):
        for ch in ("aw", "w", "ar"):
            t.quiet(target, ch, False)
    for target in ("t0", "t1", "t2"):
        for ch in ("aw", "w", "ar"):
            t.ready(target, ch, "always")
    t.drain(limit=4000, label="STALL_DRAIN")
