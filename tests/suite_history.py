"""Append-only suite attempts, provisional failures, and descriptive statistics."""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

STATUSES = {"PASS", "FAIL", "TIMEOUT", "NOT_RUN"}
CAUSES = {
    "UNTRIAGED",
    "FUNCTIONAL_ASSERTION",
    "TEST_HARNESS",
    "TOOLCHAIN_ENV",
    "TIMEOUT_BUDGET",
    "RESOURCE_INTERRUPTION",
    "INFRASTRUCTURE",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def default_store():
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "nocloom/suites"
    )


def append(store, category, key, value):
    """Atomic insert with byte-identical idempotence; never rewrite an event."""
    folder = Path(store) / category
    assert folder.resolve() != Path(store).resolve() and folder.resolve().is_relative_to(Path(store).resolve())
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (digest(key.encode()) + ".json")
    data = canonical(value)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(name, dest)
            return True
        except FileExistsError:
            if dest.read_bytes() != data:
                raise ValueError("Conflicting immutable history key: " + key)
            return False
    finally:
        pending = Path(name)
        assert not pending.is_symlink()
        assert pending.resolve() != folder.resolve() and pending.resolve().is_relative_to(folder.resolve())
        pending.unlink()


def bound_file(root, relative, required=True):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or (required and not path.is_file()):
        raise ValueError("Missing or escaping result artifact: " + str(relative))
    return path


def records(store, category):
    return [
        json.loads(p.read_text())
        for p in sorted((Path(store) / category).glob("*.json"))
    ]


def ingest(run, store):
    root = Path(run).resolve()
    result_raw = (root / "results.json").read_bytes()
    plan_raw = (root / "plan.json").read_bytes()
    result, plan = json.loads(result_raw), json.loads(plan_raw)
    selected = plan["selected"]
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Empty or duplicate selected tests")
    if len(result["engines"]) != len(plan["engines"]):
        raise ValueError("Missing engine results")
    evidence = {}
    events = []
    engines = set()
    for engine_result in result["engines"]:
        engine = engine_result["engine"]
        if (
            not re.fullmatch(r"[a-z0-9_-]+", engine)
            or engine not in plan["engines"]
            or engine in engines
        ):
            raise ValueError("Unexpected or duplicate engine")
        engines.add(engine)
        names = [r["name"] for r in engine_result["tests"]]
        if len(set(names)) != len(names) or set(names) != set(selected):
            raise ValueError("Result test set does not match selection")
        xml_path = root / engine / "results.xml"
        if xml_path.exists():
            from run_suite import read_results

            actual = {
                r["name"]: r
                for r in read_results(xml_path, [{"name": n} for n in selected])
            }
            evidence[str(xml_path.relative_to(root))] = digest(xml_path.read_bytes())
        else:
            actual = {n: {"status": "NOT_RUN"} for n in selected}
        for row in engine_result["tests"]:
            status = row["status"]
            if status not in STATUSES or status != actual[row["name"]]["status"]:
                raise ValueError("Summary disagrees with JUnit: " + row["name"])
            seconds = float(row["seconds"])
            if not math.isclose(
                seconds,
                float(actual[row["name"]].get("seconds", 0)),
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise ValueError("Elapsed time disagrees with JUnit")
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("Invalid elapsed time")
            resource = row.get("resources", {})
            if row.get("resource_record"):
                path = bound_file(root, row["resource_record"], required=bool(resource))
                if path.exists():
                    evidence[row["resource_record"]] = digest(path.read_bytes())
                    if json.loads(path.read_text()) != resource:
                        raise ValueError("Resource record mismatch")
            events.append(
                {
                    "name": row["name"],
                    "engine": engine,
                    "status": status,
                    "seconds": seconds,
                    "peak_tree_rss_bytes": resource.get("peak_tree_rss_bytes"),
                    "resource_final": resource.get("final"),
                    "cause": "UNTRIAGED" if status in {"FAIL", "TIMEOUT"} else None,
                }
            )
        log_path = root / engine / "ctest.log"
        if log_path.exists():
            # Preserve the raw failure text, not only a digest to a future-deleted log.
            data = log_path.read_bytes()
            h = digest(data)
            append(
                store,
                "evidence",
                h,
                {"sha256": h, "text": data.decode(errors="replace")},
            )
            evidence[str(log_path.relative_to(root))] = h
    if result["status"] == "PASS" and (
        result.get("interruption")
        or any(r["status"] != "PASS" for r in events)
        or any(l["ctest_exit"] != 0 for l in result["engines"])
    ):
        raise ValueError("False-green suite summary")
    run_id = plan.get("run_id") or digest(plan_raw + result_raw)
    record = {
        "run_id": run_id,
        "started_utc": plan.get("started_utc"),
        "plan_sha256": digest(plan_raw),
        "results_sha256": digest(result_raw),
        "profile": result["profile"],
        "interruption": result.get("interruption"),
        "candidate_sha256": plan.get("candidate_manifest_sha256"),
        "environment_sha256": plan.get("environment_manifest_sha256"),
        "runner_sha256": plan.get("runner_sha256"),
        "probe_sha256": plan.get("probe_sha256"),
        "policy_sha256": plan.get("policy_sha256"),
        "sim_config_sha256": plan.get("sim_config_sha256"),
        "tests": events,
        "evidence": evidence,
        "plan": plan,
        "result": result,
    }
    added = append(store, "runs", run_id, record)
    return {"run_id": run_id, "added": added, "tests": len(events)}


def observe(run_id, engine, log, store):
    """Record non-passes as observations; they are not final statistical attempts."""
    path = Path(log)
    text = path.read_text(errors="replace")
    pattern = r"^.*?Test\s+#\s*\d+:\s+(.+?)\s+\.{2,}\s*(\*\*\*.*?)\s+([\d.]+)\s+sec.*$"
    added = 0
    for line in text.splitlines():
        match = re.match(pattern, line)
        if not match:
            continue
        name, status, seconds = match.groups()
        value = {
            "run_id": run_id,
            "engine": engine,
            "name": name,
            "observed_status": status,
            "seconds": float(seconds),
            "line": line,
            "line_sha256": digest(line.encode()),
            "provisional": True,
            "statistics_eligible": False,
        }
        added += append(
            store,
            "observations",
            run_id + engine + name + status + value["line_sha256"],
            value,
        )
    return added


def triage(store, review):
    if (
        review["cause"] not in CAUSES
        or not review.get("reason")
        or not review.get("reviewer")
    ):
        raise ValueError("Triage needs a known cause, reason and reviewer")
    runs = {r["run_id"]: r for r in records(store, "runs")}
    run = runs.get(review["run_id"])
    if not run or not any(
        t["name"] == review["name"]
        and t["engine"] == review["engine"]
        and t["status"] in {"FAIL", "TIMEOUT"}
        for t in run["tests"]
    ):
        raise ValueError("Triage does not identify a final non-pass")
    if not review.get("evidence"):
        raise ValueError("Triage needs evidence")
    for anchor in review["evidence"]:
        if digest(Path(anchor["path"]).read_bytes()) != anchor["sha256"]:
            raise ValueError("Triage evidence mismatch")
    if not review.get("recorded_utc"):
        raise ValueError("Triage timestamp is required")
    # Corrections append a superseding review; the original attempt never changes.
    return append(store, "triage", digest(canonical(review)), review)


def report(store, output):
    runs = records(store, "runs")
    reviews = sorted(records(store, "triage"), key=lambda r: r["recorded_utc"])
    causes = {(r["run_id"], r["engine"], r["name"]): r["cause"] for r in reviews}
    buckets = defaultdict(list)
    contexts = defaultdict(set)
    for run in runs:
        for t in run["tests"]:
            entry = {
                **t,
                "run_id": run["run_id"],
                "started_utc": run.get("started_utc"),
                "candidate_sha256": run.get("candidate_sha256"),
                "environment_sha256": run.get("environment_sha256"),
                "cause": causes.get(
                    (run["run_id"], t["engine"], t["name"]), t["cause"]
                ),
                "batch_interruption": run.get("interruption"),
            }
            buckets[(t["name"], t["engine"])].append(entry)
            if (
                run.get("candidate_sha256")
                and run.get("environment_sha256")
                and t["status"] != "NOT_RUN"
            ):
                context = (
                    t["name"],
                    t["engine"],
                    run["candidate_sha256"],
                    run["environment_sha256"],
                    run["runner_sha256"],
                    run["probe_sha256"],
                    run["sim_config_sha256"],
                )
                contexts[context].add(t["status"])
    mixed = {
        key[:2]
        for key, statuses in contexts.items()
        if "PASS" in statuses and len(statuses) > 1
    }
    rows = []
    for (name, engine), attempts in sorted(buckets.items()):
        counts = Counter(a["status"] for a in attempts)
        executed = sum(counts[s] for s in ("PASS", "FAIL", "TIMEOUT"))
        nonpass = counts["FAIL"] + counts["TIMEOUT"]
        measured = [a for a in attempts if a["status"] != "NOT_RUN"]
        times = sorted(a["seconds"] for a in measured)
        peaks = [
            a["peak_tree_rss_bytes"]
            for a in measured
            if a["peak_tree_rss_bytes"] is not None
        ]
        ordered = sorted(
            attempts, key=lambda a: (a.get("started_utc") or "", a["run_id"])
        )
        chronological = all(a.get("started_utc") for a in ordered)
        failures = [a for a in ordered if a["status"] in {"FAIL", "TIMEOUT"}]
        first = failures[0]["run_id"] if chronological and failures else None
        last = failures[-1]["run_id"] if chronological and failures else None
        recent = (
            [a for a in ordered if a["status"] != "NOT_RUN"][-20:]
            if chronological
            else []
        )
        recent_counts = Counter(a["status"] for a in recent)
        by_candidate = {}
        by_environment = {}
        for a in attempts:
            candidate = a["candidate_sha256"] or "UNBOUND"
            by_candidate.setdefault(candidate, Counter())[a["status"]] += 1
            environment = a.get("environment_sha256") or "UNBOUND"
            by_environment.setdefault(environment, Counter())[a["status"]] += 1
        rows.append(
            {
                "name": name,
                "engine": engine,
                "counts": dict(counts),
                "executed_attempts": executed,
                "nonpass_rate": nonpass / executed if executed else None,
                "frequent_nonpass": executed >= 5
                and nonpass >= 2
                and nonpass / executed >= 0.2,
                "insufficient_samples": executed < 5,
                "first_nonpass_run": first,
                "last_nonpass_run": last,
                "chronology_known": chronological,
                "recent_20_counts": dict(recent_counts),
                "by_candidate": {k: dict(v) for k, v in by_candidate.items()},
                "by_environment": {k: dict(v) for k, v in by_environment.items()},
                "median_seconds": statistics.median(times) if times else None,
                "p95_seconds": times[max(0, math.ceil(len(times) * 0.95) - 1)]
                if times
                else None,
                "max_tree_rss_bytes": max(peaks) if peaks else None,
                "mixed_outcomes_same_bound_context": (name, engine) in mixed,
                "flake_claim": False,
                "cause_counts": dict(
                    Counter(a["cause"] for a in attempts if a["cause"])
                ),
                "attempts": ordered,
            }
        )
    complete_ids = {r["run_id"] for r in runs}
    provisional = [
        r for r in records(store, "observations") if r["run_id"] not in complete_ids
    ]
    result = {
        "generated_utc": now(),
        "run_count": len(runs),
        "rows": rows,
        "provisional_nonpasses": provisional,
        "provisional_count": len(provisional),
        "boundary": "Rates describe executed CTest attempts, not independent requirements or product defect rates. NOT_RUN and provisional observations are excluded. Mixed outcomes require triage, not an automatic flakiness verdict.",
    }
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    (out / "statistics.json").write_bytes(canonical(result))
    lines = [
        "# Suite history",
        "",
        result["boundary"],
        "",
        f"Final runs: {len(runs)}; provisional non-passes: {len(provisional)}.",
        "",
        "| Test | Engine | Attempts | Non-pass | Rate | p95 s | Peak RSS MiB |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sorted(
        rows,
        key=lambda r: (
            -sum(r["counts"].get(s, 0) for s in ("FAIL", "TIMEOUT")),
            r["name"],
            r["engine"],
        ),
    ):
        rate = (
            f"{100 * row['nonpass_rate']:.1f}%"
            if row["nonpass_rate"] is not None
            else "N/A"
        )
        peak = row["max_tree_rss_bytes"]
        lines.append(
            f"| {row['name']} | {row['engine']} | {row['executed_attempts']} | {row['counts'].get('FAIL', 0) + row['counts'].get('TIMEOUT', 0)} | {rate} | {row['p95_seconds']} | {round(peak / 1024**2, 1) if peak is not None else 'UNMEASURED'} |"
        )
    lines += ["", "## Provisional failures (excluded from rates)", ""]
    lines += [
        f"- {r['run_id']} / {r['engine']} / {r['name']}: {r['observed_status']}"
        for r in provisional
    ]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", default=str(default_store()))
    subs = p.add_subparsers(dest="action", required=True)
    a = subs.add_parser("ingest")
    a.add_argument("--run", required=True)
    a = subs.add_parser("report")
    a.add_argument("--output", required=True)
    a = subs.add_parser("triage")
    a.add_argument("--file", required=True)
    a = subs.add_parser("observe")
    for key in ("run-id", "engine", "log"):
        a.add_argument("--" + key, required=True)
    a = p.parse_args()
    if a.action == "ingest":
        print(json.dumps(ingest(a.run, a.store)))
    elif a.action == "report":
        r = report(a.store, a.output)
        print(json.dumps({k: r[k] for k in ("run_count", "provisional_count")}))
    elif a.action == "triage":
        print(triage(a.store, json.loads(Path(a.file).read_text())))
    else:
        print(observe(a.run_id, a.engine, a.log, a.store))


if __name__ == "__main__":
    main()
