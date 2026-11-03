#!/usr/bin/env python3
"""Write a synthetic N x N single-crossbar artifact and model for scale tests.

Usage: gen_scale.py N GRAPH.json MODEL.json [--rows]

The graph is what the per-word policy requires: every held grant blocks each
head word of another input.  Direct edges number N^3.  With --rows the same waits
go through helper nodes per input, so the edges number about 7 N^2 and the
coverage check reads each helper edge once.  Output is deterministic.
"""
import json
import sys


def main(argv):
    if len(argv) not in (4, 5) or (len(argv) == 5 and argv[4] != "--rows"):
        sys.stderr.write(__doc__)
        return 2
    n = int(argv[1])
    use_rows = len(argv) == 5
    head = lambda p, d: p * n + d
    grant = lambda o, p: n * n + o * n + p
    nodes = [{"kind": "target.head", "key": "%d/%d" % (p, d), "rank": 1}
             for p in range(n) for d in range(n)]
    nodes += [{"kind": "grant.held", "key": "%d/%d" % (o, p), "rank": 0}
              for o in range(n) for p in range(n)]
    edges = [[grant(d, q), head(p, d), "M1", 0]
             for p in range(n) for d in range(n) for q in range(n) if q != p]
    contention = []
    if use_rows:
        # Node ranks: grant 0, before and after rise along input order, other, head.
        base = 2 * n * n
        before = lambda o, i: base + 3 * (o * n + i)
        after = lambda o, i: base + 3 * (o * n + i) + 1
        other = lambda o, i: base + 3 * (o * n + i) + 2
        nodes = [dict(x) for x in nodes]
        for i in range(n * n):
            nodes[i]["rank"] = n + 3
        nodes += [{"kind": "helper", "key": str(i), "rank": 0} for i in range(3 * n * n)]
        edges = []
        for o in range(n):
            for i in range(n):
                nodes[before(o, i)]["rank"] = 1 + i
                nodes[after(o, i)]["rank"] = n - i
                nodes[other(o, i)]["rank"] = n + 2
                edges += [[grant(o, i), before(o, i), "M1", 0], [grant(o, i), after(o, i), "M1", 0],
                          [other(o, i), head(i, o), "M1", 0]]
                if i > 0:
                    edges += [[before(o, i - 1), before(o, i), "M1", 0],
                              [before(o, i - 1), other(o, i), "M1", 0]]
                if i + 1 < n:
                    edges += [[after(o, i + 1), after(o, i), "M1", 0],
                              [after(o, i + 1), other(o, i), "M1", 0]]
                contention.append([o, i, before(o, i), after(o, i), other(o, i)])
    graph = {"format": "nocloom.dependency", "format_version": 1,
             "orientation": "blocker_to_blocked", "nodes": nodes, "edges": edges,
             "groups": [], "result": {"status": "no_cycle"}}
    model = {"contention": contention, "routes": [[p, d, d] for p in range(n) for d in range(n)],
             "cap": 4, "endpoints": list(range(n)), "ahead": [[p, d] for p in range(n) for d in range(n)],
             "holds": [{"head": [p, d], "node": head(p, d)} for p in range(n) for d in range(n)]
                      + [{"grant": [o, p], "node": grant(o, p)} for o in range(n) for p in range(n)]}
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump(graph, handle, separators=(",", ":"))
    with open(argv[3], "w", encoding="utf-8") as handle:
        json.dump(model, handle, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
