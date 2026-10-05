"""The single driver: generate -> bench -> tape -> simulate -> check ->
results.json. Optionally the negative control: scenario faults (the
baseline build with a tape from the fault's params), then the fault build,
sel=0 event hash equal to the baseline and every designated RTL fault.

  python3 tests/verif/nlverif/run.py --case CASE.py --work DIR
         [--param KEY=VALUE]... [--cli PATH] [--fixture PATH] [--negative]
         [--only ID,...] [--sim ENGINE] [--sim-config JSON]
         [--register-reset asynchronous|synchronous]

A case module sees the --param values as the dict PARAMS when it loads (one
case file serves several registered widths or modes). A case may list
seeds (each one a tape on the same build) and configs (companion DUTs of
the same scenario: case overrides plus scenario_args; the negative control
runs on the first). --register-reset generates every DUT with that register reset (the CLI's
output.register_reset, the fixture's register_reset parameter) instead of as the case states.

Exit code: 0 every variant met its expectation, 1 a judgment failed,
2 infrastructure (generation, compile, tool) error. Only the exit code and
results.json are verdicts.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

from nlverif import bench as benchgen  # noqa: E402
from nlverif import config as nlconfig  # noqa: E402
from nlverif import events as ev  # noqa: E402
from nlverif import faults as fl  # noqa: E402
from nlverif.check import Checker  # noqa: E402
from nlverif.tape import Tape  # noqa: E402
import sim_engine  # noqa: E402


class Infra(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_case(path, params=None):
    spec = importlib.util.spec_from_file_location("nlv_case_" + Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = dict(params or {})
    spec.loader.exec_module(module)
    return module


def validate_selection(case, only, negative):
    configs = case.get("configs", [{}])
    if not configs:
        raise ValueError("At least one configuration must run")
    faults = [fault for config in configs[:None if case.get("negative_faults_all") else 1]
              for fault in config.get("faults", case.get("faults", []))]
    if any(fault.get("designated", True) is not True for fault in faults):
        raise ValueError("Every listed fault must be detected")
    if only:
        if not negative:
            raise ValueError("--only requires --negative")
        missing = set(only.split(",")) - {fault["id"] for fault in faults}
        if missing:
            raise ValueError("Unknown fault selection: " + ", ".join(sorted(missing)))


# --register-reset: the register reset every DUT is generated with, or None (as the case states).
REGISTER_RESET = None


def fixture_args(recipe):
    params = dict(recipe.get("params", {}))
    if REGISTER_RESET:
        params["register_reset"] = REGISTER_RESET
    return sum((["--param", f"{k}={v}"] for k, v in params.items()), [])


def cli_config(dut):
    """The CLI input of a DUT, with --register-reset applied."""
    cfg = json.loads(json.dumps(dut["config"]))
    if REGISTER_RESET:
        cfg.setdefault("output", {})["register_reset"] = REGISTER_RESET
    return cfg


def run_tool(argv, log, timeout=300):
    started = time.monotonic()
    r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout)
    Path(log).write_text(r.stdout + r.stderr)
    return r, time.monotonic() - started


class Build:
    """One compiled simulation (tb.sv + one DUT file with its cell models), run many times."""

    def __init__(self, root, tb_text, dut_text, args, case, cells=""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tb.sv").write_text(tb_text)
        (self.root / "dut.v").write_text(dut_text + cells)
        started = time.monotonic()
        try:
            self.sim = sim_engine.Simulation(
                self.root, "dut.v", engine=args.sim, config=args.sim_config,
                compile_timeout=case.get("compile_timeout", 600),
                run_timeout=case.get("run_timeout", 300), success=("NLV_DONE",))
        except Exception as error:  # compilation or simulation failure
            raise Infra(f"compile failed in {self.root}: {error}") from error
        self.compile_seconds = time.monotonic() - started
        self.engine = self.sim.engine

    def run(self, out, tape, plus=()):
        out = Path(out)
        work = self.root.parent.resolve()
        assert out.resolve() != work and out.resolve().is_relative_to(work)
        out.mkdir(parents=True, exist_ok=True)
        events, verdict = out / "events.jsonl", out / "verdict.json"
        for f in (events, verdict):
            if f.exists():
                assert not f.is_symlink()
                assert f.resolve() != out.resolve() and f.resolve().is_relative_to(out.resolve())
                f.unlink()
        options = [f"+NLV_EVENTS={events}", f"+NLV_VERDICT={verdict}", f"+NLV_TAPE={tape}", *plus]
        started = time.monotonic()
        status = None
        try:
            result = self.sim.run(options)
            code = result.returncode
        except Exception as error:
            code, status = None, f"TOOL_ERROR: {type(error).__name__}"
        seconds = time.monotonic() - started
        log = self.root / "simulation.log"
        if log.exists():
            (out / "simulation.log").write_bytes(log.read_bytes())
        return dict(exit=code, seconds=round(seconds, 3), tool_error=status,
                    events=str(events), verdict_path=str(verdict))


def evaluate(run, case, bench_meta, tape_meta):
    """Reads the verdict and the event log of one run; offline check."""
    result = dict(run=run)
    if run.get("tool_error"):
        result["status"] = "TOOL_ERROR"
        return result
    vpath = Path(run["verdict_path"])
    if not vpath.exists():
        result["status"] = "TOOL_ERROR"
        result["detail"] = "no verdict.json"
        return result
    verdict = ev.load_verdict(vpath)
    verdict.pop("_n", None)
    labels = tape_meta["labels"]
    for rule, detail, label in (("first_rule", "first_detail", "first_label"),
                                ("hang_rule", "hang_detail", "hang_label")):
        if verdict.get(rule, "").startswith("TAPE."):
            d = verdict.get(detail, 0)
            verdict[label] = (labels[d] if isinstance(d, int) and 0 <= d < len(labels)
                              else str(d))
    result["verdict"] = verdict
    started = time.monotonic()
    events = ev.load(run["events"])
    result["event_count"] = len(events)
    result["event_sha256"] = ev.digest(events)
    checker = Checker(case, bench_meta, tape_meta)
    result["check"] = checker.run(events)
    result["check_seconds"] = round(time.monotonic() - started, 3)
    c = result["check"]
    if c["infra"]:
        # The testbench itself misbehaved (reset propagation): no verdict on the DUT.
        result["status"] = "TOOL_ERROR"
        result["detail"] = "testbench self-check: " + json.dumps(c["infra"][:4])
        return result
    ok = (verdict.get("status") == "DONE" and verdict.get("fails", 1) == 0
          and not c["mismatches"] and c["issued"] == c["completed"] + c["cancelled"])
    result["status"] = "PASS" if ok else "FAIL"
    return result


def tape_for(module, case, bfms, seed):
    tape = Tape(bfms, max_cycles=case.get("max_cycles", 200000))
    module.scenario(tape, seed, **case.get("scenario_args", {}))
    return tape


def generate_dut(args, case, work):
    """Generates the DUT of one configuration: (components, rtl, probes, cells), cells the
    cell models the DUT instantiates (kept apart from rtl, which text faults
    copy)."""
    dut = case["dut"]
    gen = work / "generated"
    gen.mkdir(exist_ok=True)
    if dut["source"] == "fixture":
        r, _ = run_tool([args.fixture, dut["recipe"], gen, *fixture_args(dut),
                         *(["--probes"] if "probes" in case else [])], work / "fixture.log")
        if r.returncode:
            raise Infra("fixture failed: " + r.stderr[-2000:])
        components = json.loads((gen / "ports.json").read_text())
        rtl = (gen / "network.v").read_text()
        cell = gen / "cells.v"
    else:
        if dut["source"] == "generated":
            # Artifacts a CLI already wrote (the install layer's installed CLI on a shipped
            # example): judged as they are, the cell models collected beside them.
            if REGISTER_RESET:
                raise Infra("--register-reset needs a DUT this driver generates")
            out = Path(dut["dir"])
            bundle = json.loads(next(out.glob("*.bundle.json")).read_text())
            top = bundle["top"]
            cell = gen / "cells.v"
        else:
            cfg = work / "input.yaml"
            cfg.write_text(json.dumps(cli_config(dut), indent=1) + "\n")
            out = gen / "cli"
            if "service" in dut:
                # The fixture's plan names a service target; the CLI has no field for one yet.
                r, _ = run_tool([args.fixture, "service_network", gen / "service",
                                 "--param", f"config={cfg}", "--param", f"service={dut['service']}",
                                 "--param", f"artifacts={out}"], work / "fixture.log")
            else:
                r, _ = run_tool([args.cli, "generate", cfg, "--out-dir", out], work / "cli.log")
            if r.returncode:
                raise Infra("cli generate failed: " + r.stderr[-2000:])
            top = "nocloom_" + dut["config"]["name"]
            bundle = json.loads((out / f"{top}.bundle.json").read_text())
            cell = out / "cells.v"
        mapping = json.loads((out / f"{top}.map.json").read_text())
        components = benchgen.normalize_components(mapping)
        case["model"]["response_runs"] = nlconfig.response_runs(mapping)
        rtl = (out / f"{top}.v").read_text()
        # A configuration binding library cells names their models (a file under tests/).
        library = [Path(__file__).resolve().parents[2] / dut["library"]] if "library" in dut else []
        cell.write_text("".join((out / f).read_text() for f in bundle["simulation_sources"]
                                if f != f"{top}.v") + "".join(p.read_text() for p in library))
        if "probes" in case:
            # White-box probes name nets of the CLI netlist through a
            # fixture recipe that must build it byte for byte.
            recipe = case.get("fault_recipe", dut)
            r, _ = run_tool([args.fixture, recipe["recipe"], gen, *fixture_args(recipe),
                             "--probes"], work / "fixture.log")
            if r.returncode:
                raise Infra("probe fixture failed: " + r.stderr[-2000:])
            if (gen / "network.v").read_text() != rtl:
                raise Infra("probe fixture recipe differs from the CLI netlist")
    probes = json.loads((gen / "probes.json").read_text()) if "probes" in case else None
    if "map_probes" in case:
        # Internal nets of a CLI netlist that its map names (for example the
        # storage release of a burst owner): each must be declared once.
        groups = case["map_probes"]["groups"](mapping)
        for g in groups:
            for _, wire, _ in g["nets"]:
                if len(re.findall(r"^\s*(?:wire|reg)\b[^;]*\b" + re.escape(wire) + r";", rtl,
                                  flags=re.M)) != 1:
                    raise Infra(f"map probe {wire} is not one declared net of the netlist")
        probes = dict(probes or {}, dut=dict(wrapped=False, groups=groups))
    return components, rtl, probes, cell.read_text() if cell.exists() else ""


def run_baseline(args, module, case, work, label, seeds, results):
    """One configuration: generate, bench, compile once, run every seed's
    tape. Appends a variant per seed; returns the state the negative
    control reuses."""
    work.mkdir(parents=True, exist_ok=True)
    prefix = label + "/" if label else ""
    t0 = time.monotonic()
    components, rtl, probes, cells = generate_dut(args, case, work)
    timings = results["timings"].setdefault(label or "main", {})
    timings["generate"] = round(time.monotonic() - t0, 3)
    tb_text, bench_meta = benchgen.generate(case, components, probes=probes)
    (work / "bench.json").write_text(json.dumps(bench_meta, indent=1) + "\n")
    tape = tape_for(module, case, bench_meta["bfms"], seeds[0])
    (work / "tape.hex").write_text(tape.assemble())
    tape_meta = json.loads(json.dumps(tape.metadata()))
    (work / "tape.json").write_text(json.dumps(tape_meta, indent=1) + "\n")
    results["inputs"][label or "main"] = dict(
        dut_sha256=hashlib.sha256(rtl.encode()).hexdigest(),
        tb_sha256=hashlib.sha256(tb_text.encode()).hexdigest(), tape_sha256=sha(work / "tape.hex"))
    a = Build(work / "build-baseline", tb_text, rtl, args, case, cells)
    results["engine"] = a.engine
    timings["compile_baseline"] = round(a.compile_seconds, 3)
    base = evaluate(a.run(work / "baseline", work / "tape.hex"), case, bench_meta, tape_meta)
    base.update(variant=prefix + "baseline", expect="PASS", seed=seeds[0])
    results["variants"].append(base)
    timings["run_baseline"] = base["run"]["seconds"]
    timings["check_baseline"] = base.get("check_seconds")
    if case.get("test_lockstep"):
        # The test port held high passes every clock gate: the endpoints must see the same
        # events, cycle for cycle.
        lock = evaluate(a.run(work / "test-high", work / "tape.hex", ["+NLV_TEST"]), case,
                        bench_meta, tape_meta)
        lock.update(variant=prefix + "test-high", expect="PASS and baseline event hash",
                    seed=seeds[0], hash_equal=lock.get("event_sha256") == base.get("event_sha256"))
        if lock["status"] == "PASS" and not lock["hash_equal"]:
            lock["status"], lock["detail"] = "FAIL", "test-high event hash differs from baseline"
        results["variants"].append(lock)
        base["status"] = base["status"] if lock["status"] == "PASS" else lock["status"]
    if case.get("ungate_lockstep"):
        # The ungate port held high passes every clock gate, like the test port.
        lock = evaluate(a.run(work / "ungate-high", work / "tape.hex", ["+NLV_UG"]), case,
                        bench_meta, tape_meta)
        lock.update(variant=prefix + "ungate-high", expect="PASS and baseline event hash",
                    seed=seeds[0], hash_equal=lock.get("event_sha256") == base.get("event_sha256"))
        if lock["status"] == "PASS" and not lock["hash_equal"]:
            lock["status"], lock["detail"] = "FAIL", "ungate-high event hash differs from baseline"
        results["variants"].append(lock)
        base["status"] = base["status"] if lock["status"] == "PASS" else lock["status"]
    state = dict(build=a, components=components, rtl=rtl, probes=probes, tb_text=tb_text, cells=cells,
                 bench_meta=bench_meta, tape_meta=tape_meta, base=base,
                 failed=base["status"] != "PASS", infra=base["status"] == "TOOL_ERROR")
    # Further fixed seeds on the same build (faults run on the first seed).
    for extra in seeds[1:]:
        if state["infra"]:
            break
        out = work / f"baseline-s{extra}"
        st = tape_for(module, case, bench_meta["bfms"], extra)
        out.mkdir()
        (out / "tape.hex").write_text(st.assemble())
        res = evaluate(a.run(out, out / "tape.hex"), case, bench_meta,
                       json.loads(json.dumps(st.metadata())))
        res.update(variant=f"{prefix}baseline-s{extra}", expect="PASS", seed=extra)
        if "check" in res:
            res["check"]["mismatches"] = res["check"]["mismatches"][:10]
        results["variants"].append(res)
        timings["run_seeds"] = round(timings.get("run_seeds", 0) + res["run"]["seconds"], 3)
        state["failed"] = state["failed"] or res["status"] != "PASS"
        state["infra"] = res["status"] == "TOOL_ERROR"
    return state


def run_negative(args, module, case, state, work, label, seeds, results, failed):
    """The negative control of one configuration: scenario faults on its
    baseline build, then one fault build (sel=0 must reproduce the baseline
    event hash) and every designated RTL fault. Returns the failed flag."""
    prefix = label + "/" if label else ""
    a, base = state["build"], state["base"]
    components, baseline_rtl, probes = state["components"], state["rtl"], state["probes"]
    tb_text, bench_meta, tape_meta = state["tb_text"], state["bench_meta"], state["tape_meta"]
    work = work / (label or "main")
    timings = results["timings"].setdefault(label or "main", {})
    selected = [f for f in case["faults"]
                if not args.only or f["id"] in args.only.split(",")]
    faults = [f for f in selected if f["op"] != "scenario"]
    # ---- scenario faults: the baseline build, a tape that breaks an
    # endpoint premise ----
    for f in selected:
        if f["op"] != "scenario":
            continue
        out = work / f"scenario-{f['id']}"
        st = Tape(bench_meta["bfms"], max_cycles=case.get("max_cycles", 200000))
        module.scenario(st, seeds[0], **case.get("scenario_args", {}), **f.get("params", {}))
        out.mkdir()
        (out / "tape.hex").write_text(st.assemble())
        smeta = json.loads(json.dumps(st.metadata()))
        res = evaluate(a.run(out, out / "tape.hex"), case, bench_meta, smeta)
        res.update(variant=prefix + f["id"], expect=f.get("expect", []), contract=f.get("contract"),
                   operator="scenario", params=f.get("params", {}))
        res["class"] = fl.classify(f, res)
        if "check" in res:
            res["check"]["mismatches"] = res["check"]["mismatches"][:10]
        res["designated"] = True
        results["variants"].append(res)
        failed = failed or res["class"] != "CAUGHT"
    if not faults:
        timings["run_faults_total"] = round(sum(
            v["run"]["seconds"] for v in results["variants"] if "operator" in v), 3)
        return failed
    # ---- fault build ----
    t0 = time.monotonic()
    if any(f["op"] == "text" for f in faults):
        # Text faults rewrite a copy of the generated module (no fixture):
        # each copy is one variant behind the selection wrapper.
        if not all(f["op"] == "text" for f in faults) or len(components) != 1:
            raise Infra("text faults need one generated module and no IR faults")
        try:
            fjson, faulted = fl.text_faults(components[0], baseline_rtl, faults)
        except fl.AnchorLost as error:
            results["fault_build"] = dict(status="ANCHOR_LOST", detail=str(error))
            return True
        fcomponents = components
        fgen = None
    else:
        spec = work / "faults.spec.json"
        spec.write_text(json.dumps(dict(faults=faults), indent=1) + "\n")
        fgen = work / "faulted"
        recipe = case.get("fault_recipe", case["dut"])
        r, _ = run_tool([args.fixture, recipe["recipe"], fgen, "--faults", spec,
                         *fixture_args(recipe), *(["--probes"] if probes else [])],
                        work / "fault-fixture.log")
        if r.returncode:
            results["fault_build"] = dict(
                status="ANCHOR_LOST" if r.returncode == 3 else "BUILD_FAIL",
                detail=r.stderr[-2000:])
            return True
        fcomponents = json.loads((fgen / "ports.json").read_text())
        if (fgen / "network.v").read_text() != baseline_rtl:
            results["fault_build"] = dict(status="BUILD_FAIL",
                                          detail="fixture recipe differs from the baseline DUT")
            return True
        fjson = json.loads((fgen / "faults.json").read_text())
        faulted = (fgen / "faulted.v").read_text()
    (work / "faults.json").write_text(json.dumps(fjson, indent=1) + "\n")
    faulted += fl.wrapper(fcomponents, fjson)
    timings["fault_generate"] = round(time.monotonic() - t0, 3)
    # The faulted copy has its own net numbers: the probes follow it.
    ftb = tb_text
    if probes and fgen is None:
        # Text faults: the wrapper holds the unchanged core and one copy per
        # fault; every probe net follows the copy +NLV_FAULT selects.
        variants = [v["index"] for m in fjson["modules"] for v in m["variants"]]
        ftb, fmeta = benchgen.generate(case, components, probes={
            label: dict(entry, wrapped=True, variants=variants)
            for label, entry in probes.items()})
        if json.loads(json.dumps(fmeta["probes"])) != json.loads(
                json.dumps(bench_meta["probes"])):
            raise Infra("faulted probe groups differ from the baseline")
    elif probes:
        ftb, fmeta = benchgen.generate(case, components, probes=json.loads(
            (fgen / "faulted_probes.json").read_text()))
        if json.loads(json.dumps(fmeta["probes"])) != json.loads(
                json.dumps(bench_meta["probes"])):
            raise Infra("faulted probe groups differ from the baseline")
    b = Build(work / "build-faulted", ftb, faulted, args, case, state["cells"])
    timings["compile_faulted"] = round(b.compile_seconds, 3)
    results["fault_build"] = dict(status="OK", faults=fjson["faults"],
                                  dut_sha256=hashlib.sha256(faulted.encode()).hexdigest())
    zero = evaluate(b.run(work / "fault-0", work / "tape.hex", ["+NLV_FAULT=0"]), case,
                    bench_meta, tape_meta)
    zero.update(variant=prefix + "sel0", expect="PASS and baseline event hash")
    zero["hash_equal"] = zero.get("event_sha256") == base.get("event_sha256")
    if zero["status"] == "PASS" and not zero["hash_equal"]:
        zero["status"] = "FAIL"
        zero["detail"] = "sel=0 event hash differs from baseline"
    results["variants"].append(zero)
    failed = failed or zero["status"] != "PASS"
    by_index = {f["id"]: f["index"] for f in fjson["faults"]}
    for f in faults:
        k = by_index[f["id"]]
        res = evaluate(b.run(work / f"fault-{k}-{f['id']}", work / "tape.hex",
                             [f"+NLV_FAULT={k}"]), case, bench_meta, tape_meta)
        res.update(variant=prefix + f["id"], index=k, expect=f.get("expect", []),
                   contract=f.get("contract"), operator=f["op"])
        res["class"] = fl.classify(f, res)
        if "check" in res:
            res["check"]["mismatches"] = res["check"]["mismatches"][:10]
        results["variants"].append(res)
        res["designated"] = True
        failed = failed or res["class"] != "CAUGHT"
    timings["run_faults_total"] = round(sum(
        v["run"]["seconds"] for v in results["variants"] if "operator" in v), 3)
    return failed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--case", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--cli")
    p.add_argument("--fixture")
    p.add_argument("--negative", action="store_true")
    p.add_argument("--only", default="")
    p.add_argument("--seed", type=int)
    p.add_argument("--param", action="append", default=[])
    p.add_argument("--register-reset", choices=("asynchronous", "synchronous"))
    sim_engine.add_arguments(p)
    args = p.parse_args()
    global REGISTER_RESET
    REGISTER_RESET = args.register_reset
    top = Path(args.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    # Every invocation owns a fresh directory; latest.txt names it.
    work = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    (top / "latest.txt").write_text(str(work) + "\n")
    results = dict(schema=1, case=None, variants=[], timings={}, inputs={}, work=str(work))
    out_path = work / "results.json"

    def save(code):
        results["exit"] = code
        text = json.dumps(results, indent=1, default=str) + "\n"
        out_path.write_text(text)
        (top / "results.json").write_text(text)
        return code

    try:
        params = dict(kv.split("=", 1) for kv in args.param)
        module = load_case(args.case, params)
        case = module.CASE
        validate_selection(case, args.only, args.negative)
        results["case"] = case["name"]
        results["contracts"] = case.get("contracts", [])
        seeds = [args.seed] if args.seed is not None else case.get("seeds", [case.get("seed", 1)])
        results["seed"] = seeds[0]
        # Configurations: the first is the main one (the negative control
        # runs on it); the others are companion DUTs of the same scenario
        # (overrides of the case, scenario_args for the tape).
        configs = case.get("configs", [dict(label="")])
        runs_all = case.get("negative_runs_all") or case.get("negative_faults_all")
        if args.negative and not runs_all:
            # The negative control runs the main configuration and its first
            # seed; the rtl registration covers the others (a case without
            # one sets negative_runs_all, or negative_run on a configuration).
            configs = configs[:1] + [c for c in configs[1:] if c.get("negative_run")]
            seeds = seeds[:1]
        states, failed = [], False
        for k, config in enumerate(configs):
            merged = dict(case, **{key: v for key, v in config.items()
                                   if key not in ("label", "seeds", "negative_run")})
            label = config.get("label", "")
            own = (config.get("seeds", seeds)
                   if args.seed is None and (not args.negative or runs_all)
                   else seeds)
            st = run_baseline(args, module, merged, work / (label or "main"), label, own,
                              results)
            if st["infra"]:
                return save(2)
            failed = failed or st["failed"]
            states.append((st, merged, label))
        if not args.negative:
            return save(1 if failed else 0)
        # The negative control runs on the main configuration, or on every one
        # with negative_faults_all (a fault each configuration must catch).
        for st, merged, label in states[:None if case.get("negative_faults_all") else 1]:
            failed = run_negative(args, module, merged, st, work, label, seeds, results, failed)
        return save(1 if failed else 0)
    except Infra as error:
        results["error"] = str(error)
        return save(2)
    except Exception:
        results["error"] = traceback.format_exc()
        return save(2)


if __name__ == "__main__":
    sys.exit(main())
