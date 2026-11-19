"""Configuration checks of the route cycle report (docs/network.md#route-cycle-report): the
exact diagnostics and notes of request and response cycles, of cycles across the flows of one
endpoint, of several cycles and of more cycles than the limit; both limits at their defaults, at
other values and at 0; the same report after the declarations are reordered; a definition
without the contributors of one wait is accepted and generates the same bytes at every limit.

The expected notes come from the routes of each fixture: a contributor of a wait is a route
that takes both links of the wait in turn."""

import copy
from collections import defaultdict
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("nlv_config_route_ring_case", HERE / "route_ring.py")
RING = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RING)


def ring_trips(size, pairs, response=False):
    """(name, switches) of every route around a ring, endpoint i at switch i."""
    trips = []
    for s, t in sorted(pairs):
        first, last = (t, s) if response else (s, t)
        hops = (last - first) % size
        trips.append((f"s{s} to t{t}", [f"n{(first + k) % size}" for k in range(hops + 1)]))
    return trips


def report(direction, cycles, trips, contributor_limit=8, more=None):
    """The stderr lines of the cycles (each the switches of its name, the first not repeated)."""
    contributors = defaultdict(set)
    for name, nodes in trips:
        for turn in zip(nodes, nodes[1:], nodes[2:]):
            contributors[turn].add(name)

    lines = []
    for cycle in cycles:
        lines.append(f"NCL_SWITCH_ROUTE_CYCLE [topology.{direction}.links]: Routes chain the links "
                     f"of the cycle {' -> '.join(cycle + cycle[:1])}; no deadlock claim is made")
        for k in range(len(cycle)):
            a, b, c = (cycle[(k + j) % len(cycle)] for j in range(3))
            names = sorted(contributors[a, b, c], key=lambda name: name.encode())
            assert names, (a, b, c)
            listed = names if contributor_limit == 0 else names[:contributor_limit]
            rest = f" and {len(names) - len(listed)} more" if len(listed) < len(names) else ""
            lines.append(f"note: {a} -> {b} waits for {b} -> {c} in {len(names)} {direction} "
                         f"route{'s' if len(names) > 1 else ''}: {', '.join(listed)}{rest}")
    if more:
        lines.append(f"note: more {direction} cycles are present; the cycle limit is {more}")
    return lines


def rejected(name, config, stderr, args=(), code="NCL_SWITCH_ROUTE_CYCLE", **fields):
    return dict(name=name, config=config, expect="reject", code=code, exit=1, stderr=stderr,
                args=list(args), **fields)


def reordered(config):
    """The same definition with every list in the opposite order (flow indices kept)."""
    out = copy.deepcopy(config)
    out["initiators"].reverse()
    out["targets"].reverse()
    if isinstance(out["connectivity"], dict):
        out["connectivity"] = {k: list(reversed(v))
                               for k, v in reversed(list(out["connectivity"].items()))}
    for direction in ("request", "response"):
        for key in ("nodes", "links", "entries", "exits"):
            out["topology"][direction][key].reverse()
    out["topology"].get("routes", []).reverse()
    return out


RING4 = [f"n{i}" for i in range(4)]
ALL4 = RING.ALL
FORWARD = {(i, (i + 2) % 4) for i in range(4)}
BACKWARD = {((i + 2) % 4, i) for i in range(4)}
DROP_ONE = ALL4 - {(0, 2)}
ALL16 = {(s, t) for s in range(16) for t in range(16)}
RING16 = [f"n{i}" for i in range(16)]


def flows(by_target):
    """Two flow pairs of s0 and t0 that take a -> b and c -> d in opposite orders."""
    links = [("a", "b"), ("b", "c"), ("b", "v"), ("c", "d"), ("d", "a"), ("d", "v"), ("u", "a"),
             ("u", "c")]
    request = {"nodes": [{"name": n} for n in "uabcdv"],
               "links": [{"from": a, "to": b} for a, b in links],
               "entries": [{"endpoint": "s0", "node": "u"}],
               "exits": [{"endpoint": "t0", "node": "v"}]}
    response = {"nodes": [{"name": "r"}], "links": [], "entries": [{"endpoint": "t0", "node": "r"}],
                "exits": [{"endpoint": "s0", "node": "r"}]}
    key = "target_path" if by_target else "source_path"
    paths = (list("uabcdv"), list("ucdabv"))
    routes = [{"initiator": "s0", "target": "t0", key: k, "request": path, "response": ["r"]}
              for k, path in enumerate(paths)]
    if by_target:
        s0 = RING.initiator("s0")
        t0 = RING.target("t0", 0, flows=2, mappings=[
            {"base": "0x0", "size": "0x1000", "flow": 0},
            {"base": "0x1000", "size": "0x1000", "flow": 1}])
    else:
        s0 = RING.initiator("s0", route_classes=["p0", "p1"], default_class="p0",
                            id_ranges=[{"first": 1, "last": 3, "class": "p1"}])
        t0 = RING.target("t0", 0)
    name = (lambda k: f"s0 to t0 flow {k}") if by_target else (lambda k: f"s0 flow {k} to t0")
    return (RING.definition([s0], [t0], "all", request, response, routes),
            [(name(k), path) for k, path in enumerate(paths)])


def two_rings():
    """A ring of five switches a0.. and a ring of three x0.., each route two links long."""
    rings = ([f"a{i}" for i in range(5)], [f"x{i}" for i in range(3)])
    nodes = rings[0] + rings[1]
    links, pairs, trips = [], {}, []
    for ring in rings:
        for i, node in enumerate(ring):
            links.append({"from": node, "to": ring[(i + 1) % len(ring)]})
            s, t = nodes.index(node), nodes.index(ring[(i + 2) % len(ring)])
            pairs[f"s{s}"] = [f"t{t}"]
            trips.append((f"s{s} to t{t}", [ring[(i + k) % len(ring)] for k in range(3)]))
    request = {"nodes": [{"name": n} for n in nodes], "links": links,
               "entries": [{"endpoint": f"s{i}", "node": n} for i, n in enumerate(nodes)],
               "exits": [{"endpoint": f"t{i}", "node": n} for i, n in enumerate(nodes)]}
    config = RING.definition([RING.initiator(f"s{i}") for i in range(8)],
                             [RING.target(f"t{i}", i) for i in range(8)], pairs, request,
                             RING.graph(False, True, 8))
    return config, rings, trips


def ladder(stages):
    """A ring of stages: hub c<i> leads to a<i> and b<i>, and both lead to the next hub. An
    initiator at each hub for each of a and b reaches the targets at both switches of the next
    stage over that switch, so the routes chain every turn: a cycle takes a or b at each stage."""
    def name(kind, i):
        return f"{kind}{i % stages:02d}"
    nodes, links, entries, exits, routes, trips, pairs = [], [], [], [], [], [], {}
    initiators, targets = [], []
    for i in range(stages):
        nodes += [name("c", i), name("a", i), name("b", i)]
        for x in "ab":
            links += [{"from": name("c", i), "to": name(x, i)},
                      {"from": name(x, i), "to": name("c", i + 1)}]
            source, sink = f"s{i:02d}{x}", f"t{i:02d}{x}"
            initiators.append(RING.initiator(source))
            targets.append(RING.target(sink, len(targets)))
            entries.append({"endpoint": source, "node": name("c", i)})
            exits.append({"endpoint": sink, "node": name(x, i)})
            pairs[source] = [f"t{(i + 1) % stages:02d}{y}" for y in "ab"]
            for y in "ab":
                path = [name("c", i), name(x, i), name("c", i + 1), name(y, i + 1)]
                routes.append({"initiator": source, "target": f"t{(i + 1) % stages:02d}{y}",
                               "request": path, "response": ["hub"]})
                trips.append((f"{source} to t{(i + 1) % stages:02d}{y}", path))
    response = {"nodes": [{"name": "hub"}], "links": [],
                "entries": [{"endpoint": t["name"], "node": "hub"} for t in targets],
                "exits": [{"endpoint": i["name"], "node": "hub"} for i in initiators]}
    request = {"nodes": [{"name": n} for n in nodes], "links": links, "entries": entries,
               "exits": exits}
    return RING.definition(initiators, targets, pairs, request, response, routes), trips


def ladder_cycles(stages, count):
    """The first one-lap cycles of a ladder in report order, each from its first link in byte
    order. Every choice of a or b for each stage is a cycle."""
    assert count <= 1 << stages - 1 or stages <= 10
    cycles = []
    # The cycles through a of stage 0 come first: b at the last stages as a binary count.
    for k in range(count if stages > 10 else 1 << stages):
        cycle = []
        for i in range(stages):
            cycle += [f"{'b' if k >> (stages - 1 - i) & 1 else 'a'}{i:02d}",
                      f"c{(i + 1) % stages:02d}"]
        first = min(range(len(cycle)), key=lambda j: (cycle[j], cycle[(j + 1) % len(cycle)]))
        cycles.append(cycle[first:] + cycle[:first])
    return sorted(cycles, key=lambda cycle: " -> ".join(cycle).encode())[:count]


def ring(pairs, request=True, response=False, size=4):
    return RING.configuration(pairs, request, response, size)


SOURCE_FLOWS, SOURCE_TRIPS = flows(False)
TARGET_FLOWS, TARGET_TRIPS = flows(True)
TWO_RINGS, (LONG, SHORT), TWO_TRIPS = two_rings()
LADDER3, LADDER3_TRIPS = ladder(3)
LADDER40, LADDER40_TRIPS = ladder(40)
OPTION = ["NCL_VALIDATION_OPTION [validation.cycle_limit]: The cycle limit must be 1 to 1024"]
RING_ALL = report("request", [RING4], ring_trips(4, ALL4))
BOTH = (report("request", [RING4], ring_trips(4, FORWARD)) +
        report("response", [RING4], ring_trips(4, FORWARD, True)))
RING16_TRIPS = ring_trips(16, ALL16)
assert RING_ALL[1] == ("note: n0 -> n1 waits for n1 -> n2 in 3 request routes: "
                       "s0 to t2, s0 to t3, s3 to t2")
assert report("request", [RING16], RING16_TRIPS)[1] == (
    "note: n0 -> n1 waits for n1 -> n2 in 105 request routes: s0 to t10, s0 to t11, s0 to t12, "
    "s0 to t13, s0 to t14, s0 to t15, s0 to t2, s0 to t3 and 97 more")

CASES = [
    # Accepted: the rings without the contributors of one wait, at the default and other limits.
    dict(name="open_both", config=ring(RING.PAIRS["both"], True, True), expect="accept"),
    dict(name="open_both_limits", config=ring(RING.PAIRS["both"], True, True), expect="accept",
         args=["--cycle-limit", "1", "--contributor-limit", "0"], same_as="open_both"),
    dict(name="open_request", config=ring(RING.PAIRS["request"]), expect="accept"),
    dict(name="open_response", config=ring(RING.PAIRS["response"], False, True), expect="accept"),
    # One cycle: one route or several for each wait, a response cycle, both directions.
    rejected("ring_pairs", ring(FORWARD), report("request", [RING4], ring_trips(4, FORWARD)),
             field="topology.request.links"),
    rejected("ring_all", ring(ALL4), RING_ALL),
    rejected("ring_all_reordered", reordered(ring(ALL4)), RING_ALL),
    rejected("ring_drop_one", ring(DROP_ONE), report("request", [RING4], ring_trips(4, DROP_ONE))),
    rejected("ring_response", ring(BACKWARD, False, True),
             report("response", [RING4], ring_trips(4, BACKWARD, True)),
             field="topology.response.links"),
    rejected("ring_both", ring(FORWARD, True, True), BOTH),
    rejected("ring_both_reordered", reordered(ring(FORWARD, True, True)), BOTH),
    # A cycle across two flows of one initiator, and of one target.
    rejected("source_flows", SOURCE_FLOWS, report("request", [list("abcd")], SOURCE_TRIPS)),
    rejected("source_flows_reordered", reordered(SOURCE_FLOWS),
             report("request", [list("abcd")], SOURCE_TRIPS)),
    rejected("target_flows", TARGET_FLOWS, report("request", [list("abcd")], TARGET_TRIPS)),
    # Several cycles: the shorter first; at a limit of 1 the note of more cycles.
    rejected("two_rings", TWO_RINGS, report("request", [SHORT, LONG], TWO_TRIPS)),
    rejected("two_rings_reordered", reordered(TWO_RINGS),
             report("request", [SHORT, LONG], TWO_TRIPS)),
    rejected("two_rings_limit_1", TWO_RINGS, report("request", [SHORT], TWO_TRIPS, more=1),
             args=["--cycle-limit", "1"]),
    rejected("two_rings_limit_2", TWO_RINGS, report("request", [SHORT, LONG], TWO_TRIPS),
             args=["--cycle-limit", "2"]),
    # More cycles than the limit: 12 cycles, and more than 2^40.
    rejected("ladder", LADDER3,
             report("request", ladder_cycles(3, 4), LADDER3_TRIPS, more=4)),
    rejected("ladder_limit_8", LADDER3,
             report("request", ladder_cycles(3, 8), LADDER3_TRIPS, more=8),
             args=["--cycle-limit", "8"]),
    rejected("ladder_many", LADDER40,
             report("request", ladder_cycles(40, 4), LADDER40_TRIPS, more=4)),
    rejected("ladder_many_reordered", reordered(LADDER40),
             report("request", ladder_cycles(40, 4), LADDER40_TRIPS, more=4)),
    rejected("ladder_many_limit_64", LADDER40,
             report("request", ladder_cycles(40, 64), LADDER40_TRIPS, more=64),
             args=["--cycle-limit", "64"]),
    # More contributors than the limit: the default, another value, and 0 for all.
    rejected("ring16", ring(ALL16, size=16), report("request", [RING16], RING16_TRIPS)),
    rejected("ring16_limit_2", ring(ALL16, size=16),
             report("request", [RING16], RING16_TRIPS, contributor_limit=2),
             args=["--contributor-limit", "2"]),
    rejected("ring16_limit_0", ring(ALL16, size=16),
             report("request", [RING16], RING16_TRIPS, contributor_limit=0),
             args=["--contributor-limit", "0"]),
    rejected("ring16_limit_105", ring(ALL16, size=16),
             report("request", [RING16], RING16_TRIPS, contributor_limit=105),
             args=["--contributor-limit", "105"]),
    # Limits out of range: a diagnostic from the library, a syntax error from the command line.
    rejected("cycle_limit_0", ring(ALL4), OPTION, args=["--cycle-limit", "0"],
             code="NCL_VALIDATION_OPTION", field="validation.cycle_limit"),
    rejected("cycle_limit_0_valid", ring(RING.PAIRS["request"]), OPTION,
             args=["--cycle-limit", "0"], code="NCL_VALIDATION_OPTION"),
    rejected("cycle_limit_1025", ring(ALL4), OPTION, args=["--cycle-limit", "1025"],
             code="NCL_VALIDATION_OPTION"),
    rejected("cycle_limit_1024", ring(ALL4), RING_ALL, args=["--cycle-limit", "1024"]),
    dict(name="cycle_limit_negative", config=ring(ALL4), expect="reject", exit=2,
         args=["--cycle-limit=-1"]),
    dict(name="contributor_limit_text", config=ring(ALL4), expect="reject", exit=2,
         args=["--contributor-limit", "all"]),
]
