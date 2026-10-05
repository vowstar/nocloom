"""Configuration layer driver: the public CLI against data tables.

Each case file defines CASES, a list of entries:

  name      unique within the file
  text      the input (YAML text), or config: a dict written as JSON
  commands  the CLI commands to run, default ("validate", "generate")
  expect    "accept" or "reject"
  code      a diagnostic code the rejection must name (stderr)
  files     accepted generate: the number of artifacts (exactly the bundle's)
  check     accepted generate: a function of (mapping, out_dir, peers) that
            raises AssertionError when the artifacts break the case's
            contract; peers maps every earlier accepted entry to its output
  api       accepted generate: argv of a C++ definition built through the
            Core library (a build-tree tool named by --tool NAME=PATH as
            "{tool:NAME}", its output directory as "{out}"); its artifacts
            must equal the CLI's byte for byte
  mutations accepted generate with check: (label, edit, message) triples; each
            edit changes a copy of the artifacts, and check must then fail
            with exactly that message (negative control of the check)
  same_rtl_as  accepted generate: another entry whose Verilog must be byte
            identical (the map and bundle may differ)
  same_as   accepted generate: another entry whose artifacts must be byte
            identical
  diffs     runs of `diff OTHER INPUT`, each a dict: against (an earlier entry,
            its YAML input or with map=True its generated map), full (pass
            --full), exit (expected exit code), stdout (the exact output, when
            given) and code (a diagnostic the output must name). Every run is
            repeated and must print the same bytes, without a local path.

A rejected generate must leave no output directory. Every accepted generate
also runs a second time from another working directory into another output
directory and must give the same names and bytes. With --other-cxx the CLI
is built again with that compiler (C++ argument evaluation order differs
between compilers) and every accepted generate must give the same bytes
with it; an empty --other-cxx exits 77 (not run, never passed).

  python3 tests/verif/config.py --cli CLI --work DIR --case FILE [--case FILE]...
         [--other-cxx CXX --source DIR [--cmake CMAKE] [--generator G]
          [--define K=V]... [--jobs N]]

Exit code: 0 every entry met its expectation, 1 one did not, 2 a case file
or tool error. results.json lists every entry.
"""

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


def load(path):
    spec = importlib.util.spec_from_file_location("nlv_config_" + Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digests(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def other_cli(a, root):
    """The CLI built again with the other compiler."""
    build = root / "other-build"
    subprocess.run([a.cmake, "-S", a.source, "-B", str(build), "-G", a.generator,
                    f"-DCMAKE_CXX_COMPILER={a.other_cxx}", "-DCMAKE_BUILD_TYPE=Release",
                    "-DNOCLOOM_BUILD_TESTS=OFF", "-DNOCLOOM_BUILD_EXAMPLES=OFF"] +
                   [f"-D{d}" for d in a.define if d.split("=", 1)[1]],
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run([a.cmake, "--build", str(build), "--target", "nocloom_cli", "-j", a.jobs],
                   check=True, stdout=subprocess.DEVNULL)
    clis = [p for p in build.iterdir() if p.name in ("nocloom", "nocloom.exe")]
    if len(clis) != 1:
        raise RuntimeError("the second compiler did not produce the CLI")
    return str(clis[0])


def run_entry(cli, root, entry, outputs, second_cli=None, tools=None):
    name = entry["name"]
    work = root / name
    work.mkdir(parents=True)
    source = work / "input.yaml"
    source.write_text(entry["text"] if "text" in entry else json.dumps(entry["config"], indent=1) + "\n")
    row = dict(name=name, expect=entry["expect"], commands=[])
    for command in entry.get("commands", ("validate", "generate")):
        argv = [cli, command, str(source)]
        out = work / "out"
        if command == "generate":
            argv += ["--out-dir", str(out)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=entry.get("timeout", 120),
                           cwd=work)
        (work / f"{command}.log").write_text(r.stdout + r.stderr)
        row["commands"].append(dict(command=command, exit=r.returncode))
        if entry["expect"] == "reject":
            if r.returncode == 0:
                return row, f"{command} accepted"
            if entry.get("code") and entry["code"] not in r.stderr:
                return row, f"{command} did not name {entry['code']}: {r.stderr[-400:]}"
            if command == "generate" and out.exists():
                return row, "rejected generate left an output directory"
            continue
        if r.returncode != 0:
            return row, f"{command} rejected: {r.stderr[-400:]}"
        if command != "generate":
            continue
        bundle = [p for p in out.iterdir() if p.name.endswith(".bundle.json")]
        if len(bundle) != 1:
            return row, f"bundles {[p.name for p in bundle]}"
        manifest = json.loads(bundle[0].read_text())
        listed = {f["path"] for f in manifest["files"]}
        present = {p.name for p in out.iterdir()}
        if listed != present:
            return row, f"bundle lists {sorted(listed)}, directory holds {sorted(present)}"
        top = manifest["top"]
        if bundle[0].name != top + ".bundle.json" or not set(
                manifest["simulation_sources"] + manifest["synthesis_sources"]) <= present:
            return row, f"bundle top {top} or sources do not match the artifacts"
        for f in present:
            text = (out / f).read_bytes()
            if str(work).encode() in text or str(out).encode() in text:
                return row, f"{f} contains a local path"
        if "files" in entry and len(present) != entry["files"]:
            return row, f"{len(present)} artifacts, want {entry['files']}"
        # Same names and bytes from another working directory and output.
        other = work / "again"
        other.mkdir()
        again = subprocess.run([cli, command, str(source.resolve()), "--out-dir", "elsewhere"],
                               capture_output=True, text=True, timeout=entry.get("timeout", 120),
                               cwd=other)
        if again.returncode != 0 or digests(out) != digests(other / "elsewhere"):
            return row, "second generation differs"
        if second_cli:
            built = subprocess.run([second_cli, command, str(source), "--out-dir",
                                    str(work / "other-compiler")], capture_output=True,
                                   text=True, timeout=entry.get("timeout", 120), cwd=work)
            if built.returncode != 0 or digests(out) != digests(work / "other-compiler"):
                return row, "the CLI of the other compiler generates other bytes"
            row["other_compiler"] = "same bytes"
        outputs[name] = digests(out)
        outputs.setdefault("_dirs", {})[name] = out
        mapping = [p for p in out.iterdir() if p.name.endswith(".map.json")]
        if "check" in entry:
            try:
                entry["check"](json.loads(mapping[0].read_text()) if mapping else None, out,
                               outputs.get("_dirs", {}))
            except AssertionError as error:
                return row, f"check: {error}"
        for label, change, message in entry.get("mutations", ()):
            copy = work / f"mutation-{label}"
            shutil.copytree(out, copy)
            change(copy)
            changed = [p for p in copy.iterdir() if p.name.endswith(".map.json")]
            try:
                entry["check"](json.loads(changed[0].read_text()) if changed else None, copy,
                               outputs.get("_dirs", {}))
            except AssertionError as error:
                if str(error) != message:
                    return row, f"mutation {label} failed the check with {error!r}, want {message!r}"
            else:
                return row, f"mutation {label} passed the check"
            row.setdefault("mutations", []).append(dict(label=label, status="CAUGHT"))
        if "api" in entry:
            api = work / "api"
            argv = []
            for part in entry["api"]:
                part = str(part)
                for key, path in (tools or {}).items():
                    part = part.replace("{tool:" + key + "}", path)
                argv.append(part.replace("{out}", str(api)))
            if any("{tool:" in part for part in argv):
                return row, f"api tool missing: {argv}"
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=entry.get("timeout", 120), cwd=work)
            (work / "api.log").write_text(r.stdout + r.stderr)
            if r.returncode != 0 or digests(api) != outputs[name]:
                return row, f"Core API artifacts differ from the CLI's (exit {r.returncode})"
            row["api"] = "same bytes"
        if "same_as" in entry and outputs.get(entry["same_as"]) != outputs[name]:
            return row, f"artifacts differ from {entry['same_as']}"
        if "same_rtl_as" in entry:
            def rtl(d):
                return {k: v for k, v in (d or {}).items() if k.endswith(".v")}
            if not rtl(outputs[name]) or rtl(outputs.get(entry["same_rtl_as"])) != rtl(outputs[name]):
                return row, f"Verilog differs from {entry['same_rtl_as']}"
    for spec in entry.get("diffs", ()):
        error = run_diff(cli, root, work, spec)
        if error:
            return row, f"diff against {spec['against']}: {error}"
        row.setdefault("diffs", []).append(spec["against"])
    return row, None


def run_diff(cli, root, work, spec):
    other = root / spec["against"]
    before = next((other / "out").glob("*.map.json")) if spec.get("map") else other / "input.yaml"
    argv = [cli, "diff", str(before), str(work / "input.yaml")] + (["--full"] if spec.get("full") else [])
    runs = [subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=work)
            for _ in range(2)]
    r = runs[0]
    if (r.returncode, r.stdout, r.stderr) != (runs[1].returncode, runs[1].stdout, runs[1].stderr):
        return "a second run printed other output"
    if r.returncode != spec["exit"]:
        return f"exit {r.returncode}, want {spec['exit']}: {(r.stdout + r.stderr)[-400:]}"
    if "stdout" in spec and r.stdout != spec["stdout"]:
        return f"printed {r.stdout!r}, want {spec['stdout']!r}"
    if spec.get("code") and spec["code"] not in r.stderr:
        return f"did not name {spec['code']}: {r.stderr[-400:]}"
    if str(root) in r.stdout:
        return "the output holds a local path"
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cli", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--case", action="append", required=True)
    p.add_argument("--tool", action="append", default=[])
    p.add_argument("--other-cxx")
    p.add_argument("--source")
    p.add_argument("--cmake", default="cmake")
    p.add_argument("--generator", default="Ninja")
    p.add_argument("--define", action="append", default=[])
    p.add_argument("--jobs", default="8")
    a = p.parse_args()
    if a.other_cxx is not None and not a.other_cxx:
        print("NOT RUN: no second C++ compiler found")
        return 77
    top = Path(a.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    (top / "latest.txt").write_text(str(root) + "\n")
    results = dict(schema=1, layer="config", files=[], work=str(root))
    failed = False
    try:
        second = other_cli(a, root) if a.other_cxx else None
        results["other_cxx"] = a.other_cxx
        for path in a.case:
            module = load(path)
            outputs = {}
            entries = []
            names = [e["name"] for e in module.CASES]
            if len(names) != len(set(names)):
                raise ValueError(f"{path}: duplicate entry names")
            for entry in module.CASES:
                row, error = run_entry(str(Path(a.cli).resolve()), root / Path(path).stem, entry,
                                       outputs, second,
                                       dict(kv.split("=", 1) for kv in a.tool))
                row["status"] = "FAIL" if error else "PASS"
                if error:
                    row["detail"] = error
                    print(f"CONFIG FAIL {Path(path).stem}/{entry['name']}: {error}")
                failed = failed or bool(error)
                entries.append(row)
            results["files"].append(dict(case=Path(path).name, entries=entries))
    except Exception:
        results["error"] = traceback.format_exc()
        code = 2
    else:
        code = 1 if failed else 0
    results["exit"] = code
    text = json.dumps(results, indent=1) + "\n"
    (root / "results.json").write_text(text)
    (top / "results.json").write_text(text)
    print("CONFIG", "PASS" if code == 0 else "FAIL",
          sum(len(f["entries"]) for f in results["files"]))
    return code


if __name__ == "__main__":
    sys.exit(main())
