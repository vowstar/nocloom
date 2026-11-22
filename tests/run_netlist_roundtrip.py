#!/usr/bin/env python3
"""Check the printed Verilog against the generator's netlist with the Lean checker.

formal/lean/NetlistCheck reads a generated top module back in Lean, requires that it
renders to the same bytes, and compares it with the netlist that `--netlist` writes
(check_sound states what a pass proves). This test:

  1. builds the checker and its proofs in a copy of formal/lean, and requires that
     check_sound uses only the axioms propext, Classical.choice and Quot.sound;
  2. generates every example, every bounded liveness shape and two variants (held
     registers, synchronous reset) with and without --netlist: every file but the bundle
     keeps its bytes, the bundle only lists the netlist file, and the checker accepts
     the RTL and netlist pair;
  3. runs negative controls: each changes one construct of a passing RTL or netlist
     text, as a printer or netlist fault would, and the checker must reject it for the
     stated reason, so a control cannot pass by failing for another one.

Lean and lake must be on PATH at the pinned version: a missing tool fails. Writes only
under --work, each run in a new directory there.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LEAN = ROOT / "formal" / "lean"
LEAN_FILES = ("NetlistCheck", "NetlistCheck.lean", "lakefile.lean", "lake-manifest.json",
              "lean-toolchain")
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def run(cmd, cwd=None):
    return subprocess.run([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def build_checker(work):
    """Copy the Lean package, build the checker and its proofs, check the axioms."""
    for tool in ("lean", "lake"):
        if shutil.which(tool) is None:
            sys.exit("FAIL %s is not on PATH" % tool)
    pinned = (LEAN / "lean-toolchain").read_text(encoding="utf-8").strip().rsplit(":v", 1)[-1]
    if "version %s," % pinned not in run(["lean", "--version"]).stdout:
        sys.exit("FAIL lean is not the pinned %s" % pinned)
    root = work / "lean"
    root.mkdir(parents=True, exist_ok=True)
    for name in LEAN_FILES:
        source = LEAN / name
        if source.is_dir():
            shutil.copytree(source, root / name, dirs_exist_ok=True)
        else:
            shutil.copy2(source, root / name)
    for path in sorted((root / "NetlistCheck").rglob("*.lean")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("--")[0]
            if re.search(r"\bsorry\b", code) or re.match(r"\s*axiom\b", code):
                sys.exit("FAIL %s:%d: %s" % (path.name, number, line.strip()))
    start = time.monotonic()
    built = run(["lake", "build", "NetlistCheck", "netlist_check"], cwd=root)
    if built.returncode != 0:
        print(built.stdout)
        sys.exit("FAIL lake build")
    print("PASS lake build NetlistCheck netlist_check (%.1f s)" % (time.monotonic() - start))
    probe = root / "axioms.lean"
    probe.write_text("import NetlistCheck\n#print axioms NetlistCheck.check_sound\n",
                     encoding="utf-8")
    printed = run(["lake", "env", "lean", probe], cwd=root)
    found = re.search(r"'NetlistCheck.check_sound' depends on axioms: \[([^\]]*)\]", printed.stdout)
    names = {n.strip() for n in found.group(1).split(",")} if found else None
    if printed.returncode != 0 or names is None or names - ALLOWED_AXIOMS:
        print(printed.stdout)
        sys.exit("FAIL axioms of check_sound: %s" % names)
    print("PASS check_sound uses %s" % sorted(names))
    return root / ".lake" / "build" / "bin" / "netlist_check"


def designs(work):
    """(name, definition path): the examples, the liveness shapes and two variants."""
    out = [(path.stem, path) for path in sorted((ROOT / "examples").glob("*.yaml"))]
    sys.path.insert(0, str(HERE))
    from run_liveness_formal import SHAPES  # noqa: E402
    defs = work / "definitions"
    defs.mkdir(parents=True, exist_ok=True)
    extra = {"live_" + name: shape["config"] for name, shape in SHAPES.items()}
    for name, config in sorted(extra.items()):
        path = defs / (name + ".yaml")
        path.write_text(json.dumps(config, indent=1, sort_keys=True), encoding="utf-8")
        out.append((name, path))
    # Two reset ports turn on the reset handshake, whose counters have no reset.
    held = (ROOT / "examples" / "cross_tree_apb.yaml").read_text(encoding="utf-8")
    for old, new in (("reset_ports: [{name: reset_n}]",
                      "reset_ports: [{name: reset_a}, {name: reset_b}]"),
                     ("clock_port: clk_alpha, reset_port: reset_n",
                      "clock_port: clk_alpha, reset_port: reset_a"),
                     ("clock_port: clk_beta, reset_port: reset_n",
                      "clock_port: clk_beta, reset_port: reset_b")):
        assert held.count(old) == 1, old
        held = held.replace(old, new)
    sync = (ROOT / "examples" / "axi4_graph_clocked.yaml").read_text(encoding="utf-8")
    old = "output: {backend: verilog, language: verilog_2001}"
    assert sync.count(old) == 1, old
    sync = sync.replace(old, old[:-1] + ", register_reset: synchronous}")
    for name, text in (("variant_held", held), ("variant_sync_reset", sync)):
        path = defs / (name + ".yaml")
        path.write_text(text, encoding="utf-8")
        out.append((name, path))
    return out


def generate(cli, definition, out, netlist):
    cmd = [cli, "generate", definition, "--out-dir", out] + (["--netlist"] if netlist else [])
    result = run(cmd)
    if result.returncode != 0:
        raise RuntimeError("generate %s: %s" % (definition, result.stdout))


def same_outputs(plain, extended):
    """Every file but the bundle keeps its bytes, and the bundle only lists the netlist."""
    plain_files = {p.name: p for p in plain.iterdir()}
    extra = sorted(set(p.name for p in extended.iterdir()) - set(plain_files))
    if len(extra) != 1 or not extra[0].endswith(".netlist.txt"):
        return "extra files %s" % extra
    for name, path in plain_files.items():
        if name.endswith(".bundle.json"):
            bundle = json.loads((extended / name).read_text(encoding="utf-8"))
            if bundle["files"][-1] != {"path": extra[0], "role": "netlist"}:
                return "bundle does not list the netlist last"
            bundle["files"].pop()
            if json.dumps(bundle, indent=2) + "\n" != path.read_text(encoding="utf-8"):
                return "bundle changed beyond the netlist entry"
        elif (extended / name).read_bytes() != path.read_bytes():
            return "%s changed" % name
    return None


def check(checker, rtl, netlist):
    """The checker's verdict, its wall time and its peak memory in KB (0 when GNU time is
    missing: a forked Python image would count in the child's own peak)."""
    timer = shutil.which("time") if Path("/usr/bin/time").exists() else None
    prefix = ["/usr/bin/time", "-f", "maxrss_kb %M"] if timer else []
    start = time.monotonic()
    result = run(prefix + [checker, rtl, netlist])
    seconds = time.monotonic() - start
    rss = re.search(r"^maxrss_kb (\d+)$", result.stdout, re.M)
    return result, seconds, int(rss.group(1)) if rss else 0


# (name, design, file, pattern, replacement, reason): the first match of pattern in the
# design's RTL ("v") or netlist ("n") is replaced, and the checker must report reason. A file
# of "vn" applies a list of (file, pattern, replacement) edits to both.
def bump(match, group=1):
    return str(int(match.group(group)) + 1)


CONTROLS = [
    ("less_operands_swapped", "apb_simple", "v", r"\((nl_internal_v\d+) < (nl_internal_v\d+)\)",
     r"(\2 < \1)", "assignments"),
    ("and_printed_as_or", "apb_simple", "v", r"\((nl_internal_v\d+) & (nl_internal_v\d+)\)",
     r"(\1 | \2)", "assignments"),
    ("subtract_operands_swapped", "apb_simple", "v",
     r"\((nl_internal_v\d+) - (nl_internal_v\d+)\)", r"(\2 - \1)", "assignments"),
    ("not_dropped", "apb_simple", "v", r"= \(~(nl_internal_v\d+)\);", r"= \1;", "assignments"),
    ("select_arms_swapped", "apb_simple", "v",
     r"\((nl_internal_v\d+) \? (nl_internal_v\d+) : (nl_internal_v\d+)\)", r"(\1 ? \3 : \2)",
     "assignments"),
    ("equal_printed_as_less", "apb_simple", "v", r"\((nl_internal_v\d+) == (nl_internal_v\d+)\)",
     r"(\1 < \2)", "assignments"),
    ("part_select_shifted", "apb_simple", "v", r"(nl_internal_v\d+)\[(\d+):([1-9]\d*)\];",
     lambda m: "%s[%d:%d];" % (m.group(1), int(m.group(2)) - 1, int(m.group(3)) - 1),
     "assignments"),
    ("constant_changed", "apb_simple", "v", r"= (\d+)'d0;", r"= \1'd1;", "assignments"),
    ("zero_extension_padded_with_one", "apb_simple", "v", r"\{(\d+)'d0, ", r"{\1'd1, ",
     "assignments"),
    ("concatenation_swapped", "apb_simple", "v", r"\{(nl_internal_v\d+), (nl_internal_v\d+)\}",
     r"{\2, \1}", "assignments"),
    ("assignment_dropped", "apb_simple", "v", r"assign nl_internal_v\d+ = [^\n]*\n", "",
     "assignments"),
    ("output_rewired", "apb_simple", "v", r"(assign nl_[it]_\w+ = nl_internal_v)(\d+);",
     lambda m: "%s%s;" % (m.group(1), bump(m, 2)), "assignments"),
    ("declaration_widened", "apb_simple", "v", r"\nwire \[(\d+):0\] ",
     lambda m: "\nwire [%s:0] " % bump(m), "declarations"),
    ("register_declared_wire", "apb_simple", "v", r"\nreg ", "\nwire ", "declarations"),
    ("reset_value_changed", "apb_simple", "v", r"(    nl_internal_v\d+ <= \d+'d)0;", r"\g<1>1;",
     "always blocks"),
    ("asynchronous_reset_dropped", "apb_simple", "v", r" or negedge nl_internal_v\d+\)", ")",
     "always blocks"),
    ("register_loads_other_net", "apb_simple", "v",
     r"(    nl_internal_v\d+ <= nl_internal_v)(\d+);",
     lambda m: "%s%s;" % (m.group(1), bump(m, 2)), "always blocks"),
    ("clock_gate_pins_swapped", "apb_simple", "v",
     r"\.enable\((nl_internal_v\d+)\), \.test\((nl_internal_v\d+)\)", r".enable(\2), .test(\1)",
     "clock gates"),
    ("synchronizer_stages_changed", "apb_simple", "v", r"#\(\.STAGES\(2\)\)", "#(.STAGES(3))",
     "synchronizers"),
    ("synchronizer_input_changed", "apb_simple", "v", r"\.d\(nl_internal_v(\d+)\)",
     lambda m: ".d(nl_internal_v%s)" % bump(m), "synchronizers"),
    ("port_narrowed", "apb_simple", "v", r"input wire \[31:0\]", "input wire [30:0]", "ports"),
    ("operator_outside_subset", "apb_simple", "v", r"\((nl_internal_v\d+) - ", r"(\1 + ",
     "outside the subset"),
    ("extra_space", "apb_simple", "v", r";\n", "; \n", "not the rendering of its parse"),
    ("comment_with_tool_directive", "apb_simple", "v", r"^// Generated by NocLoom\n",
     "// Generated by NocLoom translate_off\n", "header comments"),
    ("comment_added", "apb_simple", "v", r"^// Generated by NocLoom\n",
     "// Generated by NocLoom\n// extra\n", "header comments"),
    ("comment_with_control_byte", "apb_simple", "v", r"^// Generated by NocLoom\n",
     "// Generated by NocLoom\x01\n", "outside printable ASCII"),
    ("empty_always_block", "apb_simple", "v", r"\nendmodule\n",
     "\nalways @(posedge nl_internal_v119) begin\nend\nendmodule\n", "item order"),
    # The last declaration moves to the end, so the declarations keep their order.
    ("declaration_after_use", "apb_simple", "v",
     r"(?s)\n((?:wire|reg) [^\n]*;\n)((?:assign |nocloom_).*)\nendmodule\n",
     r"\n\2\n\1endmodule\n", "item order"),
    ("module_named_as_cell", "apb_simple", "vn",
     [("v", r"\nmodule nocloom_peripheral_bus \(", "\nmodule nocloom_clock_gate ("),
      ("n", r"\nmodule nocloom_peripheral_bus\n", "\nmodule nocloom_clock_gate\n")],
     None, "module name is not a cell name"),
    ("netlist_operation_changed", "apb_simple", "n", r"\n(value \d+) bit_and ", r"\n\1 bit_or ",
     "assignments"),
    # The first state is a synchronizer stage; the second is a register.
    ("netlist_reset_value_changed", "apb_simple", "n",
     r"(\nstate [^\n]*\nstate \d+ \d+ \d+ \d+) 0\n", r"\1 1\n", "always blocks"),
    ("netlist_stage_dropped", "apb_simple", "n", r"\nsynchronizer \d+\n", "\n", "synchronizers"),
    # The chain of value 0 (stages 0 and 1951) is clocked by its own inner stage in both files.
    ("synchronizer_reads_inner_stage", "apb_simple", "vn",
     [("v", r"\.clock\(nl_internal_v119\), \.reset_n\(nl_internal_v2034\)",
       ".clock(nl_internal_v1951), .reset_n(nl_internal_v2034)"),
      ("n", r"\nstate 0 1951 119 2034 0\n", "\nstate 0 1951 1951 2034 0\n"),
      ("n", r"\nstate 1951 3645 119 2034 0\n", "\nstate 1951 3645 1951 2034 0\n")],
     None, "reads an inner stage"),
    ("hold_register_loads_other_net", "variant_held", "v",
     r"(always @\(posedge nl_internal_v\d+\) begin\n  nl_internal_v\d+ <= nl_internal_v)(\d+);",
     lambda m: "%s%s;" % (m.group(1), bump(m, 2)), "always blocks"),
    ("hold_register_given_reset", "variant_held", "n", r"\n(state \d+ \d+ \d+)\n",
     r"\n\1 0 0\n", "always blocks"),
    ("synchronous_reset_made_asynchronous", "variant_sync_reset", "v",
     r"always @\(posedge (nl_internal_v\d+)\) begin\n  if \(!(nl_internal_v\d+)\)",
     r"always @(posedge \1 or negedge \2) begin\n  if (!\2)", "always blocks"),
    ("synchronizer_module_for_other_reset", "variant_sync_reset", "v",
     r"nocloom_synchronizer_sync_reset #", "nocloom_synchronizer #", "synchronizers"),
]


def edits_of(edits):
    return sorted({which for which, _, _ in edits})


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    checker = build_checker(work)
    # Each run writes a new directory.
    number = 0
    while (work / ("run%d" % number)).exists():
        number += 1
    work = work / ("run%d" % number)
    work.mkdir()
    failures = []
    passed = {}
    total = time.monotonic()
    (work / "plain").mkdir(exist_ok=True)
    (work / "netlist").mkdir(exist_ok=True)
    for name, definition in designs(work):
        plain, extended = work / "plain" / name, work / "netlist" / name
        try:
            generate(args.cli, definition, plain, False)
            generate(args.cli, definition, extended, True)
        except RuntimeError as error:
            failures.append(str(error))
            continue
        differ = same_outputs(plain, extended)
        if differ:
            failures.append("%s: %s" % (name, differ))
        netlist = next(extended.glob("*.netlist.txt"))
        rtl = netlist.with_name(netlist.name[:-len(".netlist.txt")] + ".v")
        result, seconds, rss = check(checker, rtl, netlist)
        ok = result.returncode == 0 and ("PASS %s" % rtl) in result.stdout
        print("%s %s (%.2f s, %d MB, %d KB of RTL)" % ("PASS" if ok else "FAIL", name, seconds,
                                                     rss // 1024, rtl.stat().st_size // 1024))
        if ok:
            passed[name] = (rtl, netlist)
        else:
            failures.append("%s: %s" % (name, result.stdout.strip()))
    print("checked %d designs in %.1f s" % (len(passed), time.monotonic() - total))
    controls = work / "controls"
    controls.mkdir(exist_ok=True)
    for name, design, kind, pattern, replacement, reason in CONTROLS:
        if design not in passed:
            failures.append("control %s: design %s did not pass" % (name, design))
            continue
        files = dict(zip("vn", passed[design]))
        edits = pattern if kind == "vn" else [(kind, pattern, replacement)]
        texts = {k: f.read_text(encoding="utf-8") for k, f in files.items()}
        missing = False
        for which, find, replace in edits:
            texts[which], count = re.subn(find, replace, texts[which], count=1)
            missing = missing or count != 1
        unchanged = all(texts[k] == files[k].read_text(encoding="utf-8") for k in edits_of(edits))
        if missing or unchanged:
            failures.append("control %s: pattern not found" % name)
            continue
        for k in edits_of(edits):
            mutant = controls / ("%s%s" % (name, files[k].suffix))
            mutant.write_text(texts[k], encoding="utf-8")
            files[k] = mutant
        result, _, _ = check(checker, files["v"], files["n"])
        rejected = result.returncode == 1 and reason in result.stdout
        print("%s control %s: %s" % ("PASS" if rejected else "FAIL", name, result.stdout.strip()))
        if not rejected:
            failures.append("control %s not rejected for %r: %s" % (name, reason, result.stdout))
    for failure in failures:
        print("FAIL", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
