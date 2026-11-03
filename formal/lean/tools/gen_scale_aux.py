#!/usr/bin/env python3
"""Write a synthetic N x N artifact with a linear number of edges per pair.

Usage: gen_scale_aux.py N FANOUT GRAPH.json

Same size as a real large graph: a tree of auxiliary nodes keeps the edge count
near N^2 * FANOUT.  Certificate only: it has no model.  Output is deterministic.
"""
import json
import sys


def main(argv):
    if len(argv) != 4:
        sys.stderr.write(__doc__)
        return 2
    n, fanout = int(argv[1]), int(argv[2])
    size = n * n * 3
    nodes = [{"kind": "synthetic", "key": str(i), "rank": i // (n * n)} for i in range(size)]
    edges = []
    for a in range(n * n):
        for k in range(fanout):
            edges.append([a, n * n + (a + k) % (n * n), "M1", 0])
            edges.append([n * n + a, 2 * n * n + (a + k) % (n * n), "M2", 0])
    graph = {"format": "nocloom.dependency", "format_version": 1,
             "orientation": "blocker_to_blocked", "nodes": nodes, "edges": edges,
             "groups": [], "result": {"status": "no_cycle"}}
    with open(argv[3], "w", encoding="utf-8") as handle:
        json.dump(graph, handle, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
