#!/usr/bin/env python3
"""Say which edges of a dependency-graph artifact enter the proof.

Usage: edge_use.py GRAPH.json [GRAPH.json ...]

An edge enters the proof only if the rule lemmas need it.  Every other edge only
constrains the rank (`EdgeTable.checkRank_mono`).  Each edge gets one class:

  used         a pair the rule lemmas require, or a helper step that reaches one
  helper       a contention helper edge that no required pair reads
  environment  starts at an environment node, which `Fair` stands for
  summary      between endpoint nodes, a summary of the words' own waits
  outside      touches a hold point the model does not have
  unexplained  between two model hold points, and not required

Exit 1 if any edge is unexplained, or if an artifact has no `model` field.  The
required pairs mirror `Wormhole.required`; this tool reports, and Lean decides.
"""
import collections
import json
import sys

CLASSES = ("used", "helper", "environment", "summary", "outside", "unexplained")


def upstream_of(model):
    """Each slot that can change destination, with the slots upstream of it."""
    routes = [tuple(r) for r in model["routes"]]
    nexts = dict(map(tuple, model.get("nexts", [])))
    feeders = collections.defaultdict(set)
    for slot, _, out in routes:
        after = nexts.get(out)
        if after is not None:
            feeders[after].add(slot)
    table = {}
    for slot in set(model.get("cross", [])):
        seen = {slot}
        todo = [slot]
        while todo:
            for up in feeders[todo.pop()]:
                if up not in seen:
                    seen.add(up)
                    todo.append(up)
        table[slot] = sorted(seen)
    return table


def required_pairs(model):
    head = {tuple(h["head"]): h["node"] for h in model["holds"] if "head" in h}
    grant = {tuple(h["grant"]): h["node"] for h in model["holds"] if "grant" in h}
    routes = [tuple(r) for r in model["routes"]]
    feeders = collections.defaultdict(set)
    dests = collections.defaultdict(set)
    at_slot = collections.defaultdict(list)
    for slot, dest, out in routes:
        feeders[out].add(slot)
        dests[slot].add(dest)
        at_slot[slot].append((dest, out))
    pools = {(o, d): r for o, d, r in model.get("pools", [])}
    nexts = dict(map(tuple, model.get("nexts", [])))
    ups = upstream_of(model)
    holds = {tuple(x) for x in model.get("may_hold", [])}
    rowed = {row[0] for row in model.get("contention", [])}
    bound = set(model.get("bound", []))
    pairs = set()
    for slot, dest, out in routes:
        if out not in rowed and dest not in bound:
            for other in feeders[out]:
                if other != slot:
                    pairs.add((grant[(out, other)], head[(slot, dest)]))
        after = nexts.get(out, pools.get((out, dest)))
        if after is not None:
            for d in dests[after]:
                pairs.add((head[(after, d)], head[(slot, dest)]))
                pairs.add((head[(after, d)], grant[(out, slot)]))
        if slot in ups and (slot, dest) in holds:
            for up in ups[slot]:
                for d2, o2 in at_slot[up]:
                    if up != slot or o2 != out:
                        pairs.add((head[(up, d2)], grant[(out, slot)]))
    return pairs, set(head.values()) | set(grant.values()), head, grant, routes


def helper_steps(model, edges, head, routes):
    """Helper edges that coverage reads when an output has rows."""
    steps = set()
    rows = collections.defaultdict(list)
    for row in model.get("contention", []):
        rows[row[0]].append(row)
    grant = {tuple(h["grant"]): h["node"] for h in model["holds"] if "grant" in h}
    next_of = collections.defaultdict(list)
    for u, v in edges:
        next_of[u].append(v)
    for out, chain in rows.items():
        for a, b in zip(chain, chain[1:]):
            steps |= {(a[2], b[2]), (a[2], b[4]), (b[3], a[3]), (b[3], a[4])}
        for row in chain:
            held = grant.get((out, row[1]))
            if held is not None:
                steps |= {(held, row[2]), (held, row[3])}
    for slot, dest, out in routes:
        for row in rows.get(out, []):
            if row[1] != slot:
                continue
            came = {row[4]: None}
            queue = [row[4]]
            goal = head[(slot, dest)]
            for at in queue:
                if at == goal:
                    break
                for to in next_of[at]:
                    if to not in came:
                        came[to] = at
                        queue.append(to)
            at = goal if goal in came else None
            while at is not None and came[at] is not None:
                steps.add((came[at], at))
                at = came[at]
    return steps


def classify(graph):
    model = graph["model"]
    pairs, holds, head, _, routes = required_pairs(model)
    edges = [(e[0], e[1]) for e in graph["edges"]]
    steps = helper_steps(model, edges, head, routes)
    nodes = graph["nodes"]
    counts = collections.Counter()
    by_rule = collections.Counter()
    for (u, v), edge in zip(edges, graph["edges"]):
        kinds = (nodes[u]["kind"], nodes[v]["kind"])
        if (u, v) in pairs or (u, v) in steps:
            name = "used"
        elif kinds[0].startswith("env."):
            name = "environment"
        elif kinds[0].startswith("endpoint.") and kinds[1].startswith("endpoint."):
            name = "summary"
        elif any(k in ("grant.before", "grant.after", "grant.other") for k in kinds):
            name = "helper"
        elif u in holds and v in holds:
            name = "unexplained"
        else:
            name = "outside"
        counts[name] += 1
        by_rule[(edge[2], name)] += 1
    return counts, by_rule


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    total = collections.Counter()
    rules = collections.Counter()
    status = 0
    for path in argv[1:]:
        with open(path, encoding="utf-8") as handle:
            graph = json.load(handle)
        if not graph.get("model"):
            print("%s: no model: %s" % (graph.get("module"), graph.get("model_omitted")))
            status = 1
            continue
        counts, by_rule = classify(graph)
        total.update(counts)
        rules.update(by_rule)
        if counts["unexplained"]:
            status = 1
    print(" ".join("%s=%d" % (name, total[name]) for name in CLASSES))
    for key in sorted(rules):
        print("%s %s %d" % (key[0], key[1], rules[key]))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
