"""Bounded round-robin review queue; never edits, removes or runs tests."""

import argparse
import fcntl
import json
import uuid
from pathlib import Path

from suite_history import append, canonical, default_store, digest, now, records

ACTIONS = {"KEEP", "SIMPLIFY", "MERGE", "ADD", "RECLASSIFY", "REMOVE"}


def rotate(inventory, policy, store, output, limit=16, assessment=None):
    if not 1 <= limit <= 32:
        raise ValueError(
            "Audit limit must be 1..32; do not turn review into a full rerun"
        )
    raw = Path(inventory).read_bytes()
    tests = json.loads(raw)["tests"]
    names = [t["name"] for t in tests]
    if not names or len(set(names)) != len(names):
        raise ValueError("Empty or duplicate test inventory")
    pol = json.loads(Path(policy).read_text())
    daily = {n: g.get("purpose") for g in pol["daily"] for n in g["tests"]}
    catalogue = (
        {r["name"]: r for r in json.loads(Path(assessment).read_text())["rows"]}
        if assessment
        else {}
    )
    policy_sha = digest(Path(policy).read_bytes())
    source_sha = digest(raw)
    normalized = {}
    for t in tests:
        if "command" not in t:
            # CTest omits commands it cannot resolve, including unbuilt targets.
            # Keep their review obligations without inventing runnable argv.
            continue
        if (
            not isinstance(t["command"], list)
            or not t["command"]
            or not all(isinstance(arg, str) for arg in t["command"])
            or not t["command"][0]
        ):
            raise ValueError("Invalid CTest command: " + t["name"])
        cmd = list(t["command"])
        # Only work-directory arguments are omitted when finding potential reuse.
        # Similar commands do not establish checker/coverage equivalence.
        for flag in ("--work",):
            if flag in cmd and cmd.index(flag) + 1 < len(cmd):
                cmd[cmd.index(flag) + 1] = "<WORK>"
        # Equal argv is insufficient: environment, cwd, fixtures and dependencies
        # can select entirely different configurations of the same executable.
        execution = {p["name"]: p["value"] for p in t.get("properties", [])}
        key = digest(canonical({"command": cmd, "properties": execution}))
        normalized.setdefault(key, []).append(t["name"])
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    with (store / "audit.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = records(store, "audit-batches")
        selected_at = {n: 0 for n in names}
        for i, batch in enumerate(sorted(history, key=lambda b: b["created_utc"]), 1):
            for n in batch["names"]:
                if n in selected_at:
                    selected_at[n] = i
        attempts = records(store, "runs")
        observations = records(store, "observations")
        risk = {n: 0 for n in names}
        costs = {n: 0.0 for n in names}
        for run in attempts:
            for row in run["tests"]:
                if row["name"] in risk:
                    risk[row["name"]] += (
                        10 if row["status"] in {"FAIL", "TIMEOUT"} else 0
                    )
                    costs[row["name"]] = max(costs[row["name"]], row["seconds"])
        completed = {r["run_id"] for r in attempts}
        for row in observations:
            if row["run_id"] not in completed and row["name"] in risk:
                risk[row["name"]] += 5
        for n, row in catalogue.items():
            if n in costs:
                costs[n] = max(
                    [costs[n]]
                    + [
                        r["seconds"]
                        for r in row.get("observed", {}).values()
                        if r.get("seconds") is not None
                    ]
                )
        # Half the seats always go to the oldest/unseen entries. Failures cannot
        # starve the rest of the inventory forever. Scheduling is not review completion.
        count = min(limit, len(names))
        fair = sorted(names, key=lambda n: (selected_at[n], n))[: (count + 1) // 2]
        priority = sorted(
            set(names) - set(fair),
            key=lambda n: (-risk[n], -costs[n], selected_at[n], n),
        )[: count - len(fair)]
        chosen = priority + fair
        rows = []
        for n in chosen:
            t = next(t for t in tests if t["name"] == n)
            props = {p["name"]: p["value"] for p in t.get("properties", [])}
            peers = next((group for group in normalized.values() if n in group), [n])
            purpose = daily.get(n) or catalogue.get(n, {}).get("purpose")
            findings = []
            if "command" not in t:
                findings.append("COMMAND_UNAVAILABLE_IN_CTEST_INVENTORY")
            if not purpose:
                findings.append("PURPOSE_NOT_DOCUMENTED")
            if len(peers) > 1:
                findings.append("POTENTIAL_SAME_COMMAND_REUSE_NOT_PROVEN_REDUNDANT")
            if costs[n] >= 120:
                findings.append("LONG_TEST_REVIEW_COMPILE_STIMULUS_AND_CHECKER_COST")
            if risk[n]:
                findings.append("RECENT_NONPASS_TRIAGE_REQUIRED")
            if "formal" in props.get("LABELS", []):
                findings.append("REQUIRE_REACHABILITY_EVIDENCE_NOT_PROOF_PASS_ALONE")
            rows.append(
                {
                    "name": n,
                    "profile": "daily+full" if n in daily else "full",
                    "purpose": purpose,
                    "command": t.get("command"),
                    "command_status": "AVAILABLE" if "command" in t else "UNAVAILABLE",
                    "properties": props,
                    "max_observed_seconds": costs[n],
                    "similar_command_candidates": peers,
                    "findings": findings,
                    "review_status": "NOT_REVIEWED",
                    "retain_case": {
                        "question": "Which contract, reachable scenario and unique fault witness are lost if this test is removed?",
                        "answer": None,
                    },
                    "simplify_case": {
                        "question": "Can identical compilation/trace work be reused or this entry merged while preserving assertions, arrival and fault detection? Give a counterexample to the proposed equivalence.",
                        "answer": None,
                    },
                    "add_case": {
                        "question": "Which uncovered defect would survive the current test set? Supply a minimal legal stimulus and expected failure before proposing another matrix.",
                        "answer": None,
                    },
                    "adversarial_check": {
                        "question": "Try to falsify both retention and removal claims using source/trace anchors. Never infer redundancy from pass rate or scenario coverage from vacuous proof.",
                        "answer": None,
                    },
                    "decision": None,
                    "evidence": [],
                    "owner": None,
                    "reviewer": None,
                }
            )
        batch = {
            "batch_id": str(uuid.uuid4()),
            "created_utc": now(),
            "inventory_sha256": source_sha,
            "policy_sha256": policy_sha,
            "assessment_sha256": digest(Path(assessment).read_bytes())
            if assessment
            else None,
            "names": chosen,
            "inventory_count": len(names),
            "review_limit": limit,
            "fair_rotation_count": len(fair),
            "priority_count": len(priority),
            "review_time_budget_minutes": 30,
            "maximum_adversarial_rounds": 2,
            "reviewed_count": 0,
            "tests_executed": 0,
            "rows": rows,
        }
        out = Path(output)
        out.mkdir(parents=True, exist_ok=False)
        (out / "queue.json").write_bytes(canonical(batch))
        lines = [
            "# Rotating suite audit",
            "",
            "This is a bounded review queue, not completed adversarial review. No test is changed or executed.",
            "",
            f"{len(chosen)}/{len(names)} queued.",
            "",
        ]
        for row in rows:
            lines += [
                "## " + row["name"],
                "",
                "Purpose: " + (row["purpose"] or "NEEDS_EVIDENCE"),
                "",
                "Findings: "
                + (
                    ", ".join(row["findings"])
                    or "No automatic finding; semantic value still needs review."
                ),
                "",
            ]
            for role in (
                "retain_case",
                "simplify_case",
                "add_case",
                "adversarial_check",
            ):
                lines.append("- " + role + ": " + row[role]["question"])
            lines += ["", "Decision: NOT_REVIEWED", ""]
        (out / "REVIEW.md").write_text("\n".join(lines) + "\n")
        append(store, "audit-batches", batch["batch_id"], batch)
    return batch


def record_review(store, path):
    review = json.loads(Path(path).read_text())
    batches = {b["batch_id"]: b for b in records(store, "audit-batches")}
    batch = batches.get(review["batch_id"])
    if not batch or review["name"] not in batch["names"]:
        raise ValueError("Review is not bound to a queued test")
    if review["decision"] not in ACTIONS:
        raise ValueError("Unknown review decision")
    for field in (
        "owner",
        "reviewer",
        "recorded_utc",
        "retain_case",
        "simplify_case",
        "add_case",
        "adversarial_check",
        "acceptance",
        "risk",
    ):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError("Unanswered adversarial field: " + field)
    if review["retain_case"] == review["simplify_case"]:
        raise ValueError("Retention and simplification must have distinct arguments")
    if not review.get("evidence"):
        raise ValueError("Review needs source/result evidence")
    for item in review["evidence"]:
        if digest(Path(item["path"]).read_bytes()) != item["sha256"]:
            raise ValueError("Review anchor mismatch")
    if review["decision"] in {"SIMPLIFY", "MERGE", "ADD", "RECLASSIFY", "REMOVE"} and (
        not review.get("validation_plan") or not review.get("coverage_preservation")
    ):
        raise ValueError(
            "Changed test proposal needs validation and coverage preservation"
        )
    review["status"] = "REVIEWED_PROPOSAL_NOT_APPLIED"
    prior = [
        r
        for r in records(store, "audit-reviews")
        if r["batch_id"] == review["batch_id"] and r["name"] == review["name"]
    ]
    if review not in prior and len(prior) >= 2:
        raise ValueError(
            "Two rounds exhausted; record the remaining question for a later evidence-backed batch"
        )
    # The record cannot itself rewrite CTest, assertions, expected failures or metadata.
    return append(store, "audit-reviews", digest(canonical(review)), review)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", default=str(default_store()))
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("rotate")
    for key in ("inventory", "policy", "output"):
        a.add_argument("--" + key, required=True)
    a.add_argument("--assessment")
    a.add_argument("--limit", type=int, default=16)
    a = sub.add_parser("record")
    a.add_argument("--file", required=True)
    a = p.parse_args()
    if a.action == "rotate":
        b = rotate(a.inventory, a.policy, a.store, a.output, a.limit, a.assessment)
        print(
            json.dumps(
                {"batch_id": b["batch_id"], "queued": len(b["names"]), "reviewed": 0}
            )
        )
    else:
        print(record_review(a.store, a.file))


if __name__ == "__main__":
    main()
