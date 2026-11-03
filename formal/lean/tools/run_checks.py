#!/usr/bin/env python3
"""Run the whole Lean check set.  Exit 0 only if every check behaves as required.

Usage: run_checks.py [--skip-build] [--work DIR]

Needs `lean` and `lake` on PATH; a missing tool or another Lean version is a
failure, not a skip.  With --work the sources are copied to DIR and everything
runs there, so the source tree stays clean; CTest uses this.  Otherwise it
writes only under .lake/build/checks.  Deletes nothing.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ("GenericNoC", "GenericNoC.lean", "lakefile.lean", "lake-manifest.json",
           "lean-toolchain", "tests", "tools")
ALLOWED = {"propext", "Classical.choice", "Quot.sound"}
NATIVE = {"Lean.ofReduceBool", "Lean.trustCompiler"}

# What each must-fail file has to be rejected with: the text of the error, and the
# start of the source line it is reported on.
MUST_FAIL = {
    "cycle_has_rank.lean": ("Tactic `decide` proved that the proposition", "  decide"),
    "missing_rule_lemma.lean": ("Fields missing: `covered`", "    (h : t.checkRank"),
    "no_axiom_allowed.lean": ("assumed_rule", None),
    "multi_hop_same_slot.lean": ("Tactic `decide` proved that the proposition", "theorem bad"),
}

# (name, graph, model, expected).  The Kernel cases use the chunked kernel route.
# expected is "ok", or ("gen", text the generator refuses with), or the theorem
# Lean must reject.  So a case cannot pass by failing for another reason.
COVER = "covered_ok"
CASES = [
    ("Good", "graph_good", None, "ok"),
    ("PerWord", "graph_good", "model_per_word", "ok"),
    ("Contiguous", "graph_good", "model_contiguous", "ok"),
    ("KernelGood", "graph_good", None, "ok"),
    ("KernelWrongRank", "graph_wrong_rank", None, "chunk"),
    ("Rows", "graph_rows", None, "ok"),
    ("RowsBroken", "graph_rows_broken", None, COVER),
    ("CycleStatus", "graph_cycle_status", None, ("gen", "status is 'cycle'")),
    ("OrGroup", "graph_or_group", None, ("gen", "or-groups")),
    ("CycleForgedRank", "graph_cycle_forged_rank", None, "certificate"),
    ("WrongRank", "graph_wrong_rank", None, "certificate"),
    ("MissingEdge", "graph_missing_edge", "model_per_word", COVER),
    ("HoldingGoodGraph", "graph_good", "model_holding", COVER),
    ("HeadOutOfRange", "graph_good", "model_head_out_of_range", "heads_ok"),
    ("ForgedBound", "graph_good", "model_forged_bound", "wf_ok"),
    ("ForgedCoherent", "graph_good", "model_forged_coherent", "wf_ok"),
    # A real artifact with its multi-hop edges removed, so it reports no cycle.
    ("MultiHopStripped", "graph_multi_hop_stripped", None, COVER),
    # A real artifact claiming that a slot with no cut logic never holds.
    ("NoHoldClaim", "graph_z2_no_hold", None, "wf_ok"),
    # With no answers, or some missing, `Fair` could only hold with no traffic.
    ("NoAnswers", "graph_no_answers", None, "wf_ok"),
    ("PartialAnswers", "graph_partial_answers", None, "wf_ok"),
    ("RealTwoTargets", "graph_real_two_targets", None, "ok"),
    ("RealFlows", "graph_real_flows", None, "ok"),
    ("RealBound", "graph_real_bound", None, "ok"),
]

# Theorems every instance with a model has to end up with.
MODEL_THEOREMS = ("certificate", "acyclic", "structural_ok", "completes", "delivered")


def copy_sources(work):
    """Copy the package to work and return the new root."""
    work.mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, work / name, dirs_exist_ok=True)
        else:
            shutil.copy2(source, work / name)
    return work


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def axioms_of(text):
    found = []
    for match in re.finditer(r"'([^']+)' depends on axioms: \[([^\]]*)\]", text):
        names = {n.strip() for n in match.group(2).split(",") if n.strip()}
        found.append((match.group(1), names))
    return found


def first_error_line(path, text):
    """The source line of the first error Lean reports on path, or None."""
    for line in text.splitlines():
        match = re.match(r".*?\.lean:(\d+):\d+: error", line)
        if match:
            lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
            return lines[int(match.group(1)) - 1]
    return None


def listed(text):
    """Theorems the output lists axioms for, with or without any."""
    names = {name for name, _ in axioms_of(text)}
    names |= set(re.findall(r"'([^']+)' does not depend on any axioms", text))
    return names


def check_axioms(where, text, extra=frozenset()):
    bad = []
    for theorem, names in axioms_of(text):
        unknown = names - ALLOWED - set(extra)
        if unknown:
            bad.append("%s: %s uses %s" % (where, theorem, sorted(unknown)))
    return bad


def main(argv):
    global ROOT
    failures = []
    for tool in ("lean", "lake", "python3"):
        if shutil.which(tool) is None:
            print("FAIL %s is not on PATH" % tool)
            return 1
    if "--work" in argv:
        ROOT = copy_sources(pathlib.Path(argv[argv.index("--work") + 1]).resolve())
    WORK = ROOT / ".lake" / "build" / "checks"
    pinned = (ROOT / "lean-toolchain").read_text(encoding="utf-8").strip().rsplit(":v", 1)[-1]
    version = run(["lean", "--version"]).stdout
    if "version %s," % pinned not in version:
        print("FAIL lean is not the pinned %s: %s" % (pinned, version.strip()))
        return 1
    if "--skip-build" not in argv:
        result = run(["lake", "build"])
        if result.returncode != 0:
            print(result.stdout)
            print("FAIL lake build")
            return 1
        print("PASS lake build")

    # The list is read from a file of its own, so a cached build cannot hide it.
    axioms_file = ROOT / "tests" / "axioms.lean"
    result = run(["lake", "env", "lean", str(axioms_file)])
    wanted = set(re.findall(r"^#print axioms (\S+)", axioms_file.read_text(encoding="utf-8"), re.M))
    if result.returncode != 0 or "sorry" in result.stdout or wanted - listed(result.stdout):
        failures.append("axiom list: rc %d, missing %s\n%s" % (
            result.returncode, sorted(wanted - listed(result.stdout)), result.stdout[:1000]))
    failures += check_axioms("library", result.stdout)
    print("PASS axioms of %d theorems" % len(wanted) if not failures else "FAIL axioms")

    for path in sorted((ROOT / "GenericNoC").rglob("*.lean")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("--")[0]
            if re.search(r"\bsorry\b", code) or re.match(r"\s*axiom\b", code):
                failures.append("%s:%d: %s" % (path.relative_to(ROOT), number, line.strip()))

    WORK.mkdir(parents=True, exist_ok=True)
    for path in sorted((ROOT / "tests" / "fail").glob("*.lean")):
        result = run(["lake", "env", "lean", str(path)])
        text, where = MUST_FAIL[path.name]
        if where is None:
            # This one compiles, and only the axiom gate may reject it.
            rejected = result.returncode == 0 and bool(check_axioms(path.name, result.stdout)) and (
                text in result.stdout)
        else:
            line = first_error_line(path, result.stdout)
            rejected = (result.returncode != 0 and text in result.stdout and line is not None
                        and line.startswith(where))
        print("%s must-fail %s" % ("PASS" if rejected else "FAIL", path.name))
        if not rejected:
            failures.append("%s not rejected as stated\n%s" % (path.name, result.stdout[:600]))

    for name, graph, model, expected in CASES:
        out = WORK / ("%s.lean" % name)
        cmd = ["python3", "tools/gen_instance.py", "tests/data/%s.json" % graph, name, str(out)]
        if name.startswith("Kernel"):
            cmd.append("--kernel")
        if model:
            cmd += ["--model", "tests/data/%s.json" % model]
        generated = run(cmd)
        if isinstance(expected, tuple):
            good = generated.returncode == 3 and expected[1] in generated.stdout
            print("%s generator refuses %s" % ("PASS" if good else "FAIL", name))
            if not good:
                failures.append("generator on %s: %s" % (name, generated.stdout))
            continue
        if generated.returncode != 0:
            failures.append("generator failed on %s: %s" % (name, generated.stdout))
            continue
        first = out.read_bytes()
        again = run(cmd)
        if again.returncode != 0 or out.read_bytes() != first:
            failures.append("generator not deterministic on %s" % name)
        result = run(["lake", "env", "lean", str(out)])
        if expected == "ok":
            names = listed(result.stdout)
            has_model = "def config : Config" in out.read_text(encoding="utf-8")
            want = {"Generated.%s.%s" % (name, t)
                    for t in (MODEL_THEOREMS if has_model else ("certificate", "acyclic"))}
            good = result.returncode == 0 and "sorry" not in result.stdout and not want - names
            failures += check_axioms(name, result.stdout, () if name.startswith("Kernel") else NATIVE)
        else:
            line = first_error_line(out, result.stdout)
            good = result.returncode != 0 and line is not None and line.startswith("theorem %s" % expected)
        print("%s instance %s: %s" % ("PASS" if good else "FAIL", name, expected))
        if not good:
            failures.append("%s: expected %s\n%s" % (name, expected, result.stdout[:1000]))

    data = ROOT / "tests" / "data"
    for name, want, text in (("decl_single_id", 0, "0 contradictions"),
                             ("decl_single_id_contig", 1, "is not cross"),
                             ("decl_single_id_bound", 1, "has both directions")):
        result = run(["python3", "tools/check_declarations.py", str(data / ("%s.json" % name)),
                      str(data / "map_single_id.json")])
        good = result.returncode == want and text in result.stdout
        print("%s declarations %s: %s" % ("PASS" if good else "FAIL", name, text))
        if not good:
            failures.append("check_declarations on %s: %s" % (name, result.stdout[:300]))
    for name, want in (("graph_rows", "unexplained=0"), ("graph_rows_unexplained", "unexplained=1"),
                       ("graph_real_two_targets", "unexplained=0"), ("graph_real_flows", "unexplained=0"),
                       ("graph_real_bound", "unexplained=0")):
        result = run(["python3", "tools/edge_use.py", str(data / ("%s.json" % name))])
        code = 0 if want == "unexplained=0" else 1
        good = result.returncode == code and want in result.stdout
        print("%s edge_use %s: %s" % ("PASS" if good else "FAIL", name, want))
        if not good:
            failures.append("edge_use on %s: %s" % (name, result.stdout[:300]))

    if failures:
        print("\n".join("FAIL " + f for f in failures))
        return 1
    print("PASS all checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
