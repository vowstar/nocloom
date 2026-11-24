"""A stream of decode misses beside a request of the other direction at the default target,
with separate read and write paths, with mixed paths and, as a companion, with shared paths
(public CLI).

AXI4 sources s0 and s1 keep two requests each and reach the AXI4 target t0, which keeps two
bursts and answers the decode misses of both sources. In each phase s1 sends STREAM one-beat
misses of one direction. Phases 1 to 4 queue them all, so that two are in flight and the next
miss waits at t0 while the one before it is answered. Phases 5 to 8 send each miss once the
one before it is answered. After two misses are answered, s0 sends one request of the other
direction: to the window of t0 or to an unmapped address.

| Phase | s1 stream | s0 request |
| --- | --- | --- |
| 1, 5 | write misses | read of t0 |
| 2, 6 | write misses | read miss |
| 3, 7 | read misses | write of t0 |
| 4, 8 | read misses | write miss |

The oracle takes the cycles from the request of s0 to its response, which must be at most
BOUND: where a miss meets a head of the other path, the paths take turns
(docs/read-write-paths.md, Decode misses), so s0 waits for at most one miss. Without the turns
s0 waits until the stream ends in phases 1 to 3, about four cycles per miss. In phases 5 to 8
the stream leaves a gap of a cycle after each answer, and s0 waits for at most one miss also
without the turns: these phases check the bound for a stream of one miss in flight.

The configuration "mixed" carries the requests on separate paths and the responses on shared
paths. "shared" keeps one request path, where the switch serves the sources by turns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

MISS = 0x200000
STREAM = 32
# Cycles from the request of s0 to its response, with the stream queued and sent one by one:
# up to 9 and 10 with turns, about 4 * STREAM and 10 without.
BOUND = dict(queued=20, single=14)
PHASES = ((True, False), (True, True), (False, False), (False, True))


def configuration(paths):
    config = {
        'schema_version': 10, 'name': 'rw_miss_turns', 'address_width': 32,
        'read_write': 'shared' if paths == 'mixed' else paths,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': n, 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 2} for n in ('s0', 's1')],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'max_outstanding': 2, 'mappings': [{'base': '0x0', 'size': '0x10000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if paths == 'mixed':
        # Separate request graph, shared response graph.
        config['topology'] = {}
        for direction in ('request', 'response'):
            request = direction == 'request'
            nodes = [dict(name='fabric', clock_domain='main'), dict(name='far', clock_domain='main')]
            link = dict(zip(('from', 'to'), ('fabric', 'far') if request else ('far', 'fabric')))
            if request:
                for node in nodes:
                    node['read_write'] = 'separate'
                link['read_write'] = 'separate'
            sources = [dict(endpoint=n, node='fabric') for n in ('s0', 's1')]
            targets = [dict(endpoint='t0', node='far')]
            config['topology'][direction] = dict(
                nodes=nodes, links=[link], entries=sources if request else targets,
                exits=targets if request else sources)
    return endpoint_ports(config, id_width=2, len_width=8)


def model(config):
    return dict(routes=nlconfig.routes(config), keeps_decerr=nlconfig.keeps_decerr(config),
                targets=nlconfig.targets(config, error_data_zero=True))


def variant(label, paths):
    config = configuration(paths)
    return dict(label=label, dut=dict(source="cli", config=config), model=model(config))


def answers(e, port):
    """A B or a last R beat at port."""
    return (e.get("p") == port and e.get("k") == "hs" and
            (e.get("ch") == "b" or (e.get("ch") == "r" and e.get("l") == 1)))


def oracle(checker, events, sources, targets):
    """The cycles from the request of s0 to its response, per phase."""
    del sources, targets
    index = checker.tape["marks"].index("phase")
    out, phase, issued, waits = [], 0, None, {}
    for e in events:
        if e.get("k") == "mk" and e.get("m") == index:
            phase = e["v"]
        elif e.get("p") == "s0" and e.get("k") == "push":
            issued = e["c"]
        elif answers(e, "s0") and issued is not None:
            waits[phase] = e["c"] - issued
            issued = None
    for phase in range(1, 2 * len(PHASES) + 1):
        most = BOUND["queued" if phase <= len(PHASES) else "single"]
        if waits.get(phase, most + 1) > most:
            out.append(dict(detail=f"phase {phase}: the request of s0 waited too long",
                            cycles=str(waits.get(phase)), most=str(most)))
    return out


SEPARATE = configuration('separate')
BFMS = [dict(name=n, kind="axi_master", endpoint="initiator:" + n) for n in ('s0', 's1')]
BFMS.append(dict(name='t0', kind="axi_slave", endpoint="target:t0", salt=1))
CASE = dict(
    name="rw_miss_turns",
    contracts=["AXI.PATHS.SEPARATE", "AXI.ERR.UNMAPPED_DECERR"],
    dut=dict(source="cli", config=SEPARATE),
    bfms=BFMS,
    model=model(SEPARATE),
    max_cycles=12000,
    oracle=oracle,
    configs=[dict(label=""), variant("mixed", 'mixed'), variant("shared", 'shared')],
    faults=[],
)


def send(t, port, write, address, id):
    if write:
        t.write(port, address, id=id)
    else:
        t.read(port, address, id=id)


def scenario(t, seed):
    t.reset(4)
    t.delay(2)
    for phase in range(1, 2 * len(PHASES) + 1):
        stream_writes, request_miss = PHASES[(phase - 1) % len(PHASES)]
        queued = phase <= len(PHASES)
        t.mark("phase", phase)
        answer = "b" if stream_writes else "rlast"
        answered = t.snap("s1", answer)
        address = MISS + 0x8000 if request_miss else 0x100
        for k in range(STREAM):
            send(t, "s1", stream_writes, MISS + 0x40 * k, k % 2)
            if not queued:
                t.wait("s1", answer, "==", k + 1, since=answered, limit=200,
                       label=f"P{phase}_STREAM")
            elif k == STREAM - 1:
                t.wait("s1", answer, ">=", 2, since=answered, limit=200,
                       label=f"P{phase}_STREAM")
            if (queued and k == STREAM - 1) or (not queued and k == 1):
                send(t, "s0", not stream_writes, address, 2)
        t.drain(limit=2000, label=f"P{phase}_DRAIN")
    t.end()
