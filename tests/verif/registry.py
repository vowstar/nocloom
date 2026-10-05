"""Registry gate (quick).

1. Every verif case carries exactly one layer and one tier, a bounded
   timeout, a memory estimate, and contracts where required.
2. Orphans, from the registry: every case file, driver and stub under
   tests/verif is named on some case's command line (verif-registry.json
   "files"), imported by a registered Python file, inlined by the bench
   generator (sv/), or built by CMake (fixture/).
3. Contracts: an rtl case registers exactly the contracts of its case file;
   a negative case exactly those of its designated faults; every name is in
   the contract table.
4. registry_contracts: every online rule and offline mismatch kind has
   negative-control evidence that a test actually exercises (a rulecheck.py
   scenario or a selftest.py seeded defect; a name in a fault's expectation
   list is no evidence, it may never be hit) or a registered gap with a
   reason, never both; every rule the SV and bench sources raise and every
   kind check.py raises is in the table, and nothing else is.
5. Fault expectations name known rules or kinds; TAPE.* only with a label.
"""

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

LAYERS = {"unit", "config", "structure", "rtl", "negative", "formal", "install", "measure"}
TIERS = {"quick", "focused", "release", "full", "characterization", "formal"}


def load_module(path, name, params=()):
    """A case file as its registration loads it (--param values as PARAMS)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = dict(p.split("=", 1) for p in params)
    spec.loader.exec_module(module)
    return module


def imports_of(path):
    """Module names a Python file imports (absolute and package-relative)."""
    tree = ast.parse(Path(path).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{a.name}" if base else a.name for a in node.names)
    return {n.split(".")[-1] for n in names} | {n.replace(".", "/") for n in names}


def check_cases(reg, errors):
    names = set()
    for c in reg["cases"]:
        if c["name"] in names:
            errors.append("duplicate " + c["name"])
        names.add(c["name"])
        if c["layer"] not in LAYERS or c["tier"] not in TIERS:
            errors.append(f"{c['name']}: layer/tier {c['layer']}/{c['tier']}")
        if c["layer"] in ("rtl", "negative") and not c["contracts"]:
            errors.append(c["name"] + ": no contracts")
        if c["layer"] in ("rtl", "negative", "formal") and c["tier"] == "quick":
            errors.append(c["name"] + ": simulation case in quick")
        if not isinstance(c.get("memory_mb"), int) or c["memory_mb"] <= 0:
            errors.append(c["name"] + ": no MEMORY_MB")


def check_orphans(reg, src, errors):
    registered = {f for c in reg["cases"] for f in c.get("files", [])}
    for f in sorted(registered):
        if not (src / f).is_file():
            errors.append("registered file missing " + f)
    python = {f for f in registered if f.endswith(".py")}
    # Python reachable from registered files through imports.
    reachable = set(python)
    frontier = list(python)
    modules = {p.relative_to(src).as_posix(): p for p in src.rglob("*.py")
               if "__pycache__" not in p.parts}
    while frontier:
        f = frontier.pop()
        wanted = imports_of(src / f)
        for rel in modules:
            stem = rel[:-3]
            if rel in reachable:
                continue
            if Path(stem).name in wanted or stem in wanted or (
                    stem.endswith("/__init__") and stem[:-9] in wanted):
                reachable.add(rel)
                frontier.append(rel)
    bench = (src / "nlverif" / "bench.py").read_text()
    inlined = set(re.findall(r'"(nlv_[\w]+\.svh?)"', bench)) | {"nlv_common.svh"}
    cmake = (src / "CMakeLists.txt").read_text()
    for f in sorted(src.rglob("*")):
        if f.suffix not in (".py", ".sv", ".svh", ".v", ".cpp") or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(src).as_posix()
        if rel in registered or rel in reachable:
            continue
        if rel.startswith("sv/") and f.name in inlined:
            continue
        if rel.startswith("fixture/") and rel in cmake:
            continue
        errors.append("orphan " + rel)


def case_faults(case):
    """Every fault a negative run of a case executes: the case's own, or a
    configuration's own list in its place (with negative_faults_all each
    configuration runs its list, otherwise only the first runs)."""
    configs = case.get("configs") or [{}]
    if not case.get("negative_faults_all"):
        configs = configs[:1]
    return [f for config in configs for f in config.get("faults", case.get("faults", []))]


def check_contracts(reg, src, C, errors):
    for c in reg["cases"]:
        for name in c["contracts"]:
            if name not in C.CONTRACTS:
                errors.append(f"{c['name']}: unknown contract {name}")
        cases = [f for f in c.get("files", []) if f.startswith("cases/")]
        if c["layer"] not in ("rtl", "negative"):
            continue
        if len(cases) != 1:
            errors.append(f"{c['name']}: expected one case file, got {cases}")
            continue
        case = load_module(src / cases[0], "nlv_registry_" + Path(cases[0]).stem,
                           c.get("params", [])).CASE
        if c["layer"] == "rtl":
            want = set(case.get("contracts", []))
        else:
            want = {f["contract"] for f in case_faults(case)}
            extra = want - set(case.get("contracts", []))
            if extra:
                errors.append(f"{c['name']}: fault contracts {sorted(extra)} not in the case")
        if set(c["contracts"]) != want:
            errors.append(f"{c['name']}: CONTRACTS {sorted(c['contracts'])} != case {sorted(want)}")


def raised_rules(src):
    text = "".join(p.read_text() for p in sorted((src / "sv").glob("*.sv")))
    text += (src / "nlverif" / "bench.py").read_text()
    rules = set(re.findall(r'nlv_fail_report\("([A-Z][A-Z0-9_.]+)"', text))
    rules |= set(re.findall(r'nlv_first_rule = "([A-Z][A-Z0-9_.]+)"', text))
    return rules


def raised_kinds(src):
    return set(re.findall(r'self\.miss\("([A-Z][A-Z0-9_]*)"', (src / "nlverif" / "check.py").read_text()))


def check_rule_evidence(reg, src, C, errors, report):
    rulecheck = load_module(src / "rulecheck.py", "nlv_registry_rulecheck")
    selftest = load_module(src / "selftest.py", "nlv_registry_selftest")
    registered = {f for c in reg["cases"] for f in c.get("files", [])}
    for gate in ("rulecheck.py", "selftest.py"):
        if gate not in registered:
            errors.append(f"{gate} is evidence but not registered")
    for f in sorted(registered):
        if not f.startswith("cases/"):
            continue
        # Configuration tables (CASES) carry no faults.
        case = getattr(load_module(src / f, "nlv_registry_ev_" + Path(f).stem), "CASE", {})
        for fault in case_faults(case):
            if fault.get("designated", True) is not True:
                errors.append(f"{f}:{fault['id']}: listed fault must be detected")
            for name in fault.get("expect", []):
                base = name.split(":")[0]
                if base not in C.RULES and base not in C.MISMATCHES:
                    errors.append(f"{f}:{fault['id']}: unknown expectation {name}")
                if base.startswith("TAPE.") and ":" not in name:
                    errors.append(f"{f}:{fault['id']}: unlabeled expectation {name}")
    online = set(rulecheck.PROVEN)
    offline = set(selftest.OFFLINE_PROVEN)
    for rule in sorted(raised_rules(src) ^ set(C.RULES)):
        errors.append(f"rule table and sources differ: {rule}")
    for kind in sorted(raised_kinds(src) ^ set(C.MISMATCHES)):
        errors.append(f"mismatch table and check.py differ: {kind}")
    for name in sorted(set(C.GAPS) - set(C.RULES) - set(C.MISMATCHES)):
        errors.append(f"gap for unknown rule {name}")
    for name in list(C.RULES) + list(C.MISMATCHES):
        evidence = []
        if name in online:
            evidence.append("rulecheck")
        if name in offline:
            evidence.append("selftest")
        gap = C.GAPS.get(name)
        if evidence and gap:
            errors.append(f"stale gap {name}: evidence {evidence}")
        elif not evidence and not gap:
            errors.append(f"no negative-control evidence or gap for {name}")
        report[name] = dict(evidence=evidence, gap=gap)
    for name in sorted(online - set(C.RULES)):
        errors.append(f"rulecheck proves unknown rule {name}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--registry", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--work", required=True)
    a = p.parse_args()
    reg = json.loads(Path(a.registry).read_text())
    src = Path(a.source).resolve()
    sys.path.insert(0, str(src))
    sys.path.insert(0, str(src.parent))
    from nlverif import contracts as C  # noqa: PLC0415
    errors, report = [], {}
    check_cases(reg, errors)
    check_orphans(reg, src, errors)
    check_contracts(reg, src, C, errors)
    check_rule_evidence(reg, src, C, errors, report)
    Path(a.work, "registry-check.json").write_text(
        json.dumps(dict(errors=errors, evidence=report), indent=1, sort_keys=True) + "\n")
    for e in errors:
        print("REGISTRY:", e)
    print("REGISTRY", "FAIL" if errors else "PASS", len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
