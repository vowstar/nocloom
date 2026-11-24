#!/usr/bin/env python3
"""Generate a Lean instance from a dependency-graph artifact.

Usage: gen_instance.py GRAPH.json NAME OUT.lean [--model MODEL.json]
                       [--graph-only] [--kernel]

GRAPH.json is the `nocloom.dependency` artifact.  The transport tables that the
rule lemmas are checked against come from its `model` field, or from MODEL.json.
With neither, or with --graph-only, the file proves only the certificate: the
graph has no cycle.

The artifact's `completion` scope decides whether the file states `completes`
and `delivered`.  Where the generator lists unmet clauses of `Fair`, the file
states `completion_withdrawn` with those rows instead.  A scope that is
missing, malformed, or that applies with unmet clauses fails conversion.

Output is deterministic: edges are sorted and de-duplicated, and the file holds
no time or path.  Small tables use the kernel (`decide +kernel`), with edges,
ranks and node keys packed into 32-bit fields of numbers, which the kernel reads
by big-number arithmetic, and one check at a time.  Large tables use
`native_decide`, which adds the compiler to the trusted base; the file's
`#print axioms` shows it.  --kernel keeps the certificate in the kernel at any
size, at more time; rule coverage on a large table stays `native_decide`.

Exit codes: 0 written, 2 usage, 3 artifact cannot give a certificate.
"""
import argparse
import hashlib
import json
import re
import sys

import edge_use

CHUNK = 4000
GROUP = 400
KERNEL_LIMIT = 3000
KERNEL_CHUNK = 1000
PAGE = 4096
FIELD = 1 << 32


def fail(msg):
    sys.stderr.write("gen_instance: %s\n" % msg)
    sys.exit(3)


def emit_chunks(out, prefix, rows):
    """Text chunks grouped so that no list literal grows long."""
    names = []
    for i in range(0, len(rows), CHUNK):
        name = "%s%d" % (prefix, i // CHUNK)
        out.append('def %s : String := "%s"\n' % (name, "".join(rows[i:i + CHUNK])))
        names.append(name)
    groups = []
    for i in range(0, len(names), GROUP):
        name = "%sGroup%d" % (prefix, i // GROUP)
        out.append("def %s : List String := [%s]\n" % (name, ", ".join(names[i:i + GROUP])))
        groups.append(name)
    return "[%s]" % ", ".join(groups)


def nat_pairs(rows):
    return "[%s]" % ", ".join("(%d, %d)" % (a, b) for a, b in rows)


def triples(rows):
    return "[%s]" % ", ".join("(%d, %d, %d)" % tuple(r) for r in rows)


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_graph(graph):
    if graph.get("format") != "nocloom.dependency":
        fail("not a dependency artifact")
    if type(graph.get("format_version")) is not int or graph["format_version"] != 2:
        fail("format_version %r is not supported" % graph.get("format_version"))
    if graph.get("orientation") != "blocker_to_blocked":
        fail("orientation %r is not supported" % graph.get("orientation"))
    if graph.get("groups"):
        fail("or-groups are not in the model")
    for index, edge in enumerate(graph["edges"]):
        if (not isinstance(edge, list) or len(edge) != 4
                or type(edge[3]) is not int or edge[3] < 0):
            fail("NCL_CERTIFICATE_EDGE_FORMAT: edge %d must have four columns "
                 "(source, destination, rule, nonnegative integer group)" % index)
        if edge[3] != 0:
            fail("NCL_CERTIFICATE_ALTERNATIVE_GROUP: edge %d has alternative "
                 "group %d, which the certificate model does not support"
                 % (index, edge[3]))
    status = graph.get("result", {}).get("status")
    if status != "no_cycle":
        fail("artifact status is %r, so it has no rank" % status)
    nodes = graph["nodes"]
    ranks = []
    for index, node in enumerate(nodes):
        if "rank" not in node:
            fail("node %d has no rank" % index)
        if type(node["rank"]) is not int or node["rank"] < 0:
            fail("node %d rank %r is not a nonnegative integer" % (index, node["rank"]))
        ranks.append(node["rank"])
    edges = sorted({(e[0], e[1]) for e in graph["edges"]})
    return len(nodes), edges, ranks


def read_completion(graph):
    """The artifact's completion scope: whether `completes` and `delivered` may be
    stated, and otherwise the clauses of `Fair` the generated logic breaks."""
    scope = graph.get("completion")
    if not isinstance(scope, dict):
        fail("NCL_CERTIFICATE_SCOPE: the artifact has no completion scope")
    applies, unmet = scope.get("applies"), scope.get("unmet")
    if (type(applies) is not bool or scope.get("theorems") != ["completes", "delivered"]
            or not isinstance(unmet, list)):
        fail("NCL_CERTIFICATE_SCOPE: the completion scope needs applies, theorems "
             "[completes, delivered] and unmet")
    rows = []
    for row in unmet:
        if (not isinstance(row, dict) or set(row) != {"clause", "subject", "reason"}
                or not all(isinstance(row[k], str) and row[k] and '"' not in row[k]
                           and "\\" not in row[k] for k in row)):
            fail("NCL_CERTIFICATE_SCOPE: an unmet premise needs clause, subject and reason text")
        rows.append((row["clause"], row["subject"], row["reason"]))
    if len(set(rows)) != len(rows):
        fail("NCL_CERTIFICATE_SCOPE: an unmet premise is listed twice")
    if applies and rows:
        fail("NCL_CERTIFICATE_SCOPE: the completion scope applies with unmet premises")
    if not applies and not rows and graph.get("model") is not None:
        fail("NCL_CERTIFICATE_SCOPE: the completion scope withdraws the theorems without a reason")
    return applies, sorted(rows)


def graph_hints(model, edges, heads):
    """Paths for required pairs that are not one edge.

    These are only suggestions: Lean checks every step, and a wrong or missing
    path fails coverage.
    """
    rows = {(row[0], row[1]): row[4] for row in model.get("contention", [])}
    node_of = {(p, d): n for p, d, n in heads}
    wanted = {}
    direct = set(edges)
    for p, d, o in model["routes"]:
        start = rows.get((o, p))
        goal = node_of.get((p, d))
        if start is not None and goal is not None and (start, goal) not in direct:
            wanted.setdefault(start, set()).add(goal)
    try:
        pairs = edge_use.required_pairs(model)[0]
    except KeyError:
        pairs = set()
    for u, v in pairs:
        if (u, v) not in direct:
            wanted.setdefault(u, set()).add(v)
    next_of = {}
    for u, v in edges:
        next_of.setdefault(u, []).append(v)
    found = []
    for start, goals in wanted.items():
        came = {start: None}
        queue = [start]
        for at in queue:
            for to in next_of.get(at, []):
                if to not in came:
                    came[to] = at
                    queue.append(to)
        for goal in goals:
            if goal in came and goal != start:
                via = []
                at = came[goal]
                while at is not None and at != start:
                    via.append(at)
                    at = came[at]
                found.append([start, list(reversed(via)), goal])
    return found


def hop_table(model):
    """Steps left from each route.  Lean checks that they fall along the routes."""
    routes = {(r[0], r[1]): r[2] for r in model["routes"]}
    nexts = dict(map(tuple, model.get("nexts", [])))
    table = {}
    for slot, dest in routes:
        chain = []
        at = slot
        while (at, dest) not in table:
            chain.append(at)
            if len(chain) > len(routes):
                fail("a route of destination %d does not end" % dest)
            out = routes.get((at, dest))
            step = nexts.get(out) if out is not None else None
            if step is None:
                table[(at, dest)] = 1
                chain.pop()
                break
            at = step
        below = table[(at, dest)]
        for prev in reversed(chain):
            below += 1
            table[(prev, dest)] = below
    return table


def capacity_rows(model, field, keys):
    """A capacity per key, positive, listing exactly the given keys."""
    rows = model.get(field)
    if (not isinstance(rows, list) or any(
            not isinstance(r, list) or len(r) != 2 or any(type(v) is not int for v in r)
            or r[1] < 1 for r in rows)):
        fail("%s must be [key, positive capacity] rows" % field)
    if sorted(r[0] for r in rows) != sorted(set(keys)):
        fail("%s must list exactly the keys %s" % (field, sorted(set(keys))))
    return sorted(map(tuple, rows))


def pack(values):
    total = 0
    for index, value in enumerate(values):
        if not 0 <= value < FIELD:
            fail("value %d does not fit a 32-bit field" % value)
        total |= value << (index * 32)
    return hex(total)


def emit_kernel(out, size, edges, ranks):
    """A chain of per-chunk kernel theorems, ending in the certificate."""
    pages = []
    for i in range(0, max(size, 1), PAGE):
        name = "rankPage%d" % (i // PAGE)
        out.append("def %s : Nat := %s\n" % (name, pack(ranks[i:i + PAGE])))
        pages.append(name)
    out.append("def rk : Nat → Nat := EdgeTable.Kernel.rankOfPages [%s]\n\n" % ", ".join(pages))
    parts = [edges[i:i + KERNEL_CHUNK] for i in range(0, len(edges), KERNEL_CHUNK)]
    out.append("def chunks%d : List EdgeTable.Kernel.Chunk := []\n" % len(parts))
    out.append("theorem ok%d : EdgeTable.Kernel.AllOk %d rk chunks%d := trivial\n\n"
               % (len(parts), size, len(parts)))
    for k in range(len(parts) - 1, -1, -1):
        flat = [value for edge in parts[k] for value in edge]
        out.append("def lit%d : Nat := %s\n" % (k, pack(flat)))
        out.append("theorem chunk%d : EdgeTable.Kernel.chunkOk %d rk lit%d %d 0 = true := by decide +kernel\n"
                   % (k, size, k, len(parts[k])))
        out.append("def chunks%d : List EdgeTable.Kernel.Chunk := ⟨%d, lit%d⟩ :: chunks%d\n"
                   % (k, len(parts[k]), k, k + 1))
        out.append("theorem ok%d : EdgeTable.Kernel.AllOk %d rk chunks%d := ⟨chunk%d, ok%d⟩\n"
                   % (k, size, k, k, k + 1))
    out.append("\ndef table : EdgeTable := ⟨%d, EdgeTable.Kernel.edgesOf chunks0, rk⟩\n\n" % size)
    out.append("theorem certificate : table.checkRank = true :=\n"
               "  EdgeTable.Kernel.checkRank_of_allOk ok0\n\n")


def main(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("graph")
    parser.add_argument("name")
    parser.add_argument("out")
    parser.add_argument("--model")
    parser.add_argument("--graph-only", action="store_true")
    parser.add_argument("--kernel", action="store_true")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit:
        sys.stderr.write(__doc__)
        return 2
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.name):
        fail("bad instance name %r" % args.name)
    graph = load(args.graph)
    model = load(args.model) if args.model else graph.get("model")
    if args.graph_only:
        model = None
    size, edges, ranks = read_graph(graph)
    applies, unmet = read_completion(graph)
    digest = hashlib.sha256(
        json.dumps([size, edges, ranks], separators=(",", ":")).encode()
    ).hexdigest()
    prove = "decide +kernel" if len(edges) <= KERNEL_LIMIT else "native_decide"

    out = ["-- Generated by tools/gen_instance.py. Do not edit.\n",
           "import GenericNoC.Import\n\n",
           "namespace Generated.%s\n\nopen Wormhole\n\n" % args.name,
           # One kernel check at a time: in parallel their memory adds up.
           "set_option Elab.async false\n\n",
           'def digest : String := "%s"\n' % digest]
    # The kernel reads big numbers natively but walks lists and arrays one cell at a
    # time, so every kernel table is packed into 32-bit fields of numbers.
    if args.kernel or prove != "native_decide":
        emit_kernel(out, size, edges, ranks)
        if model is not None and prove == "native_decide":
            pair_rows = ["%d %d;" % e for e in edges]
            out.append("def edgeArray : Array (Nat × Nat) := Import.pairsOf %s\n"
                       % emit_chunks(out, "edgeChunk", pair_rows))
    else:
        if prove == "native_decide":
            edge_list = emit_chunks(out, "edgeChunk", ["%d %d;" % e for e in edges])
            rank_list = emit_chunks(out, "rankChunk", ["%d;" % r for r in ranks])
            out.append("def edgeArray : Array (Nat × Nat) := Import.pairsOf %s\n" % edge_list)
            out.append("def rankArray : Array Nat := Import.natsOf %s\n" % rank_list)
        out.append("def table : EdgeTable := ⟨%d, edgeArray.toList, EdgeTable.rankArr rankArray⟩\n\n" % size)
        out.append("theorem certificate : table.checkRank = true := by %s\n\n" % prove)
    out.append("theorem acyclic (u : Nat) : ¬ table.Path u u :=\n"
               "  Import.no_cycle certificate u\n\n")

    theorems = ["certificate", "acyclic"]
    if model is not None:
        heads = sorted((h["head"][0], h["head"][1], h["node"]) for h in model["holds"] if "head" in h)
        grants = sorted((h["grant"][0], h["grant"][1], h["node"]) for h in model["holds"] if "grant" in h)
        routes = sorted(map(tuple, model["routes"]))
        if prove == "native_decide":
            as_text = lambda rows: ["%d %d %d;" % r for r in rows]
            routes_term = "(Import.triplesOf %s).toList" % emit_chunks(out, "routeChunk", as_text(routes))
            heads_term = "Import.triplesOf %s" % emit_chunks(out, "headChunk", as_text(heads))
            grants_term = "Import.triplesOf %s" % emit_chunks(out, "grantChunk", as_text(grants))
        else:
            routes_term = triples(routes)
            heads_term = "%s.toArray" % triples(heads)
            grants_term = "%s.toArray" % triples(grants)
        nexts = sorted(map(tuple, model.get("nexts", [])))
        pools = sorted(map(tuple, model.get("pools", [])))
        if any(len(row) != 3 for row in pools):
            fail("pools must be (output, destination, slot)")
        may_hold = sorted(map(tuple, model.get("may_hold", [])))
        endpoints = sorted(set(model["endpoints"]))
        contig = sorted(model.get("contig", []))
        cross = sorted(model.get("cross", []))
        if prove == "native_decide":
            pair_text = lambda rows: ["%d %d;" % r for r in rows]
            nat_text = lambda rows: ["%d;" % r for r in rows]
            nexts_term = "Import.pairListOf %s" % emit_chunks(out, "nextChunk", pair_text(nexts))
            pools_term = "(Import.triplesOf %s).toList" % emit_chunks(out, "poolChunk", ["%d %d %d;" % r for r in pools])
            hold_term = "Import.sortedPairSet (Import.pairsOf %s)" % emit_chunks(out, "holdChunk", pair_text(may_hold))
            ends_term = "Import.natListOf %s" % emit_chunks(out, "endChunk", nat_text(endpoints))
            contig_term = "Import.natListOf %s" % emit_chunks(out, "contigChunk", nat_text(contig))
            cross_term = "Import.natListOf %s" % emit_chunks(out, "crossChunk", nat_text(cross))
        else:
            nexts_term = nat_pairs(nexts)
            pools_term = triples(pools)
            hold_term = "Import.pairSet %s" % nat_pairs(may_hold)
            ends_term = str(endpoints)
            contig_term = str(contig)
            cross_term = str(cross)
        hops = hop_table(model)
        hop_rows = sorted((p, d, h) for (p, d), h in hops.items())
        slots = sorted({r[0] for r in routes})
        bound = sorted(model.get("bound", []))
        ups = edge_use.upstream_of(model)
        up_rows = sorted((slot, x) for slot, xs in ups.items() for x in xs)
        if prove == "native_decide":
            hops_term = "Import.triplesOf %s" % emit_chunks(out, "hopChunk", ["%d %d %d;" % r for r in hop_rows])
            slots_term = "Import.natListOf %s" % emit_chunks(out, "slotChunk", ["%d;" % r for r in slots])
            ups_term = "fun p => (Import.upsArray (Import.pairsOf %s)).getD p []" % emit_chunks(
                out, "upChunk", ["%d %d;" % r for r in up_rows])
        else:
            hops_term = "%s.toArray" % triples(hop_rows)
            slots_term = str(slots)
            ups_term = "Import.upsList %s" % nat_pairs(up_rows)
        caps = capacity_rows(model, "caps", slots)
        pool_caps = capacity_rows(model, "pool_caps", sorted({r[2] for r in pools}))
        out.append("def capRows : List (Nat × Nat) := %s\n" % nat_pairs(caps))
        out.append("def poolCapRows : List (Nat × Nat) := %s\n\n" % nat_pairs(pool_caps))
        out.append("theorem caps_ok : capRows.all (fun r => decide (0 < r.2)) = true := by decide\n\n")
        out.append("def config : Config where\n")
        out.append("  routes := %s\n" % routes_term)
        out.append("  nexts := %s\n" % nexts_term)
        out.append("  pools := %s\n" % pools_term)
        out.append("  cap := Import.capOf capRows\n")
        out.append("  poolCap := Import.capOf poolCapRows\n")
        out.append("  endpoints := %s\n" % ends_term)
        out.append("  mayHold := %s\n" % hold_term)
        out.append("  contig := Import.natSet (%s)\n" % contig_term)
        out.append("  cross := Import.natSet (%s)\n" % cross_term)
        out.append("  bound := Import.natSet %s\n" % bound)
        out.append("  slots := %s\n" % slots_term)
        out.append("  hops := Import.keyNode (%s)\n" % hops_term)
        out.append("  hopMax := %d\n" % (max(hops.values(), default=0) + 1))
        out.append("  ups := %s\n" % ups_term)
        out.append("  answers := Import.pairSet %s\n" % nat_pairs(sorted(map(tuple, model.get("answers", [])))))
        out.append("  ahead := Import.pairSet %s\n\n" % nat_pairs(sorted(map(tuple, model.get("ahead", [])))))
        if prove == "native_decide":
            out.append("def node : Hold → Nat :=\n  Import.nodeMap (%s) (%s)\n\n"
                       % (heads_term, grants_term))
        else:
            out.append("def node : Hold → Nat :=\n  Import.packedNodeMap\n    %s %d\n    %s %d\n\n"
                       % (pack([v for row in heads for v in row]), len(heads),
                          pack([v for row in grants for v in row]), len(grants)))
        hints = model.get("hints", []) + graph_hints(model, edges, heads)
        groups = []
        hints = sorted(hints, key=lambda h: (h[0], h[2], h[1]))
        for i in range(0, len(hints), GROUP):
            out.append("def hintGroup%d : List (Nat × List Nat × Nat) := [%s]\n" % (i // GROUP, ", ".join(
                "(%d, %s, %d)" % (h[0], json.dumps(h[1]), h[2]) for h in hints[i:i + GROUP])))
            groups.append("hintGroup%d" % (i // GROUP))
        out.append("def hints : Hints := [%s].flatten.toArray\n\n" % ", ".join(groups))
        if prove == "native_decide":
            out.append("theorem index_ok : table.checkIndex edgeArray = true := by %s\n\n" % prove)
            out.append("def index : EdgeTable.Index table := EdgeTable.indexOfCheck edgeArray index_ok\n\n")
        else:
            out.append("def index : EdgeTable.Index table :=\n"
                       "  EdgeTable.Kernel.chunkIndex %d rk chunks0\n\n" % size)
        out.append("theorem wf_ok : wfCheck config = true := by %s\n\n" % prove)
        out.append("theorem hops_ok : hopsOk config = true := by %s\n\n" % prove)
        out.append("theorem heads_ok : headsInRange config table node = true := by %s\n\n" % prove)
        rows = {}
        for row in model.get("contention", []):
            rows.setdefault(row[0], []).append(row[1:])
        top = max(rows, default=-1) + 1
        if prove == "native_decide":
            text = ["%d %d %d %d %d;" % tuple(r) for r in model.get("contention", [])]
            out.append("def rowsOf : Array (List Row) := Import.rowsOfText %s\n\n"
                       % emit_chunks(out, "rowChunk", text))
        else:
            out.append("def rowsOf : Array (List Row) := #[\n")
            out.append(",\n".join("  [%s]" % ", ".join("⟨%d, %d, %d, %d⟩" % tuple(r) for r in rows.get(o, []))
                                   for o in range(top)))
            out.append("]\n\n")
        out.append("theorem covered_ok : checkCoverageFast config rowsOf index hints node = true := by %s\n\n" % prove)
        out.append("theorem structural_ok (env : (Nat → St) → Prop) :\n"
                   "    GenericNoC.StructuralInstanceLemmas (noc config table node env certificate) :=\n"
                   "  structural wf_ok certificate (checkCoverageFast_covered covered_ok) heads_ok\n\n")
        theorems.append("structural_ok")
        if not applies:
            if unmet:
                out.append("/-- `completes` and `delivered` are not stated for this network.  Each row\n"
                           "    is a clause of `Fair` that the generated logic does not provide: clause,\n"
                           "    object, reason.  The theorems hold for the model, not for this network. -/\n")
                out.append("def unmet : List (String × String × String) := [\n%s]\n\n" % ",\n".join(
                    '  ("%s", "%s", "%s")' % row for row in unmet))
                out.append("theorem completion_withdrawn : unmet ≠ [] := List.cons_ne_nil _ _\n\n")
                theorems.append("completion_withdrawn")
            model = None
    if model is not None:
        out.append("theorem completes {s : Nat → St} (h0 : s 0 = St.empty)\n"
                   "    (hs : ∀ n, Step config (s n) (s (n + 1))) (F : Fair config s) (τ k : Nat) :\n"
                   "    ∃ u, τ ≤ u ∧ (s u).live k = false :=\n"
                   "  checked_transaction_completes (t := table) (node := node) wf_ok\n"
                   "    (Import.capOf_pos caps_ok) hops_ok certificate\n"
                   "    (checkCoverageFast_covered covered_ok) heads_ok h0 hs F τ k\n\n")
        out.append("theorem delivered {s : Nat → St} (h0 : s 0 = St.empty)\n"
                   "    (hs : ∀ n, Step config (s n) (s (n + 1))) (F : Fair config s) (τ k : Nat)\n"
                   "    (hused : (s τ).used k = true) :\n"
                   "    ∃ u, τ ≤ u ∧ ∀ v, u ≤ v → (s v).live k = false ∧ ∀ p, ¬ hasTok (s v) p k :=\n"
                   "  checked_transaction_delivered (t := table) (node := node) wf_ok\n"
                   "    (Import.capOf_pos caps_ok) hops_ok certificate\n"
                   "    (checkCoverageFast_covered covered_ok) heads_ok h0 hs F τ k hused\n\n")
        theorems += ["completes", "delivered"]
    for theorem in theorems:
        out.append("#print axioms %s\n" % theorem)
    out.append("\nend Generated.%s\n" % args.name)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
