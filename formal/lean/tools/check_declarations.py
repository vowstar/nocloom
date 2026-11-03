#!/usr/bin/env python3
"""Check an artifact's declarations against the map of the same generation.

Usage: check_declarations.py GRAPH.json MAP.json

Lean takes `cross`, `contig` and `bound` as given.  They come from the plan, so this
is not proof, but it reads other facts: the map's list of interleaving targets, its
limit of outstanding reads per target, and the source's port list.

  A target that interleaves with more than one outstanding read must be `cross`,
  or end its segments ahead of the next word.
  A slot fed by a `cross` slot must be `cross`.
  A destination is `bound` only if its source has no ID bits and one direction.

Exit 0 when all hold, 1 on a contradiction, 2 on usage, 3 when there is no model.
"""
import json
import sys


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(__doc__)
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        graph = json.load(handle)
    with open(argv[2], encoding="utf-8") as handle:
        mapping = json.load(handle)
    model = graph.get("model")
    if not model:
        print("no model: %s" % graph.get("model_omitted"))
        return 3
    slots = model["slots"]
    cross = {slots[i] for i in model.get("cross", [])}
    interleaved = set(mapping["network"].get("interleaved_targets", []))
    outstanding = mapping["definition"].get("outstanding", {}).get("targets", {})
    problems = []
    ahead = {tuple(x) for x in model.get("ahead", [])}
    for target in sorted(interleaved):
        if outstanding.get(target, 1) <= 1:
            continue
        for slot, dest, _ in model["routes"]:
            if slots[slot].startswith("target/%s" % target) and slots[slot] not in cross and (
                    (slot, dest) not in ahead):
                problems.append("target %s interleaves with %d outstanding, and toward %s is "
                                "not cross and does not end segments ahead"
                                % (target, outstanding[target], model["destinations"][dest]))
    nexts = dict(map(tuple, model.get("nexts", [])))
    for slot, _, out in model["routes"]:
        after = nexts.get(out)
        if after is not None and slot in model.get("cross", []) and after not in model.get("cross", []):
            problems.append("%s is cross and feeds %s, which is not" % (slots[slot], slots[after]))
    ports = {}
    for port in mapping["ports"]:
        ports.setdefault(port.get("endpoint"), set()).add(port.get("signal"))
    transactions = mapping["definition"].get("endpoint_transactions", {}).get("initiators", {})
    for dest in model.get("bound", []):
        name = model["destinations"][dest]
        if not name.startswith("source/"):
            problems.append("bound destination %s is not a source" % name)
            continue
        source = name[len("source/"):]
        signals = ports.get("initiator:%s" % source, set())
        if transactions.get(source, {}).get("id_width", 1) != 0:
            problems.append("bound source %s has ID bits" % source)
        if "arvalid" in signals and "awvalid" in signals:
            problems.append("bound source %s has both directions" % source)
    for problem in problems:
        print(problem)
    print("declarations: %d contradictions" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
