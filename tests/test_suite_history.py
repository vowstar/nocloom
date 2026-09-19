"""Negative controls for persistent statistics and bounded review scheduling."""

import json
import tempfile
import unittest
from pathlib import Path

import audit_suite
import suite_history as h


class HistoryContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="suite-history-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"

    def run_fixture(
        self, key, verdict="PASS", candidate="candidate", environment="env", seconds=1
    ):
        root = self.root / key
        root.mkdir()
        (root / "iverilog").mkdir()
        plan = {
            "run_id": key,
            "started_utc": "2026-01-01T00:00:00Z",
            "selected": ["traffic"],
            "engines": ["iverilog"],
            "candidate_manifest_sha256": candidate,
            "environment_manifest_sha256": environment,
            "runner_sha256": "runner",
            "probe_sha256": "probe",
            "policy_sha256": "policy",
        }
        (root / "plan.json").write_bytes(h.canonical(plan))
        inner = {
            "PASS": "",
            "FAIL": '<failure message="assertion">failure detail</failure>',
            "TIMEOUT": '<failure message="Timeout"/>',
            "NOT_RUN": "<skipped/>",
        }[verdict]
        (root / "iverilog/results.xml").write_text(
            f'<testsuite><testcase name="traffic" time="{seconds}">{inner}</testcase></testsuite>'
        )
        (root / "iverilog/ctest.log").write_text("ORIGINAL " + verdict + "\n")
        result = {
            "profile": "daily",
            "status": "PASS" if verdict == "PASS" else "NONPASS",
            "interruption": None,
            "engines": [
                {
                    "engine": "iverilog",
                    "ctest_exit": 0 if verdict == "PASS" else 8,
                    "tests": [
                        {"name": "traffic", "status": verdict, "seconds": seconds}
                    ],
                }
            ],
        }
        (root / "results.json").write_bytes(h.canonical(result))
        return root

    def report(self, name="report"):
        return h.report(self.store, self.root / name)

    def test_same_command_different_environment_is_not_duplicate(self):
        from audit_suite import rotate

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inv = root / "inventory.json"
            inv.write_text(
                json.dumps(
                    {
                        "tests": [
                            {
                                "name": "a",
                                "command": ["fixture"],
                                "properties": [
                                    {"name": "ENVIRONMENT", "value": ["SCHEMA=5"]}
                                ],
                            },
                            {
                                "name": "b",
                                "command": ["fixture"],
                                "properties": [
                                    {"name": "ENVIRONMENT", "value": ["SCHEMA=8"]}
                                ],
                            },
                        ]
                    }
                )
            )
            policy = root / "policy.json"
            policy.write_text('{"daily": []}')
            batch = rotate(inv, policy, root / "history", root / "review", limit=2)
            self.assertTrue(
                all(
                    r["similar_command_candidates"] == [r["name"]]
                    for r in batch["rows"]
                )
            )

    def test_unavailable_commands_are_retained_without_reuse_claims(self):
        inv = self.root / "inventory.json"
        policy = self.root / "policy.json"
        inv.write_text(json.dumps({"tests": [{"name": "a"}, {"name": "b"}]}))
        policy.write_text('{"daily": [{"purpose": "metadata only", "tests": ["a"]}]}')
        batch = audit_suite.rotate(
            inv, policy, self.store, self.root / "queue", limit=2
        )
        self.assertEqual(set(batch["names"]), {"a", "b"})
        for row in batch["rows"]:
            self.assertIsNone(row["command"])
            self.assertEqual(row["command_status"], "UNAVAILABLE")
            self.assertEqual(row["similar_command_candidates"], [row["name"]])
            self.assertIn("COMMAND_UNAVAILABLE_IN_CTEST_INVENTORY", row["findings"])
            self.assertNotIn(
                "POTENTIAL_SAME_COMMAND_REUSE_NOT_PROVEN_REDUNDANT", row["findings"]
            )
        for malformed in (None, [], "command", [""], [1]):
            inv.write_text(
                json.dumps({"tests": [{"name": "bad", "command": malformed}]})
            )
            with self.assertRaisesRegex(ValueError, "Invalid CTest command: bad"):
                audit_suite.rotate(inv, policy, self.store, self.root / "invalid")
        self.assertFalse((self.root / "invalid").exists())
        inv.write_text(
            json.dumps(
                {"tests": [{"name": "empty_argument", "command": ["python", "-c", ""]}]}
            )
        )
        batch = audit_suite.rotate(
            inv, policy, self.store, self.root / "empty-argument"
        )
        self.assertEqual(batch["rows"][0]["command"], ["python", "-c", ""])

    def test_elapsed_summary_must_match_xml(self):
        run = self.run_fixture("a")
        result = json.loads((run / "results.json").read_text())
        result["engines"][0]["tests"][0]["seconds"] = 0.001
        (run / "results.json").write_bytes(h.canonical(result))
        with self.assertRaises(ValueError):
            h.ingest(run, self.store)

    def test_duplicate_import_does_not_inflate(self):
        run = self.run_fixture("a", "FAIL")
        self.assertTrue(h.ingest(run, self.store)["added"])
        self.assertFalse(h.ingest(run, self.store)["added"])
        row = self.report()["rows"][0]
        self.assertEqual(row["executed_attempts"], 1)
        self.assertTrue(row["insufficient_samples"])
        self.assertFalse(row["frequent_nonpass"])

    def test_recovery_keeps_original_failure(self):
        h.ingest(self.run_fixture("a", "FAIL"), self.store)
        h.ingest(self.run_fixture("b", "PASS"), self.store)
        row = self.report()["rows"][0]
        self.assertEqual(row["counts"], {"FAIL": 1, "PASS": 1})
        self.assertEqual(row["nonpass_rate"], 0.5)
        self.assertTrue(row["mixed_outcomes_same_bound_context"])
        self.assertFalse(row["flake_claim"])

    def test_different_candidate_or_missing_binding_is_not_flake(self):
        for key, verdict, candidate in [
            ("a", "FAIL", "one"),
            ("b", "PASS", "two"),
            ("c", "FAIL", None),
            ("d", "PASS", None),
        ]:
            h.ingest(self.run_fixture(key, verdict, candidate), self.store)
        self.assertFalse(self.report()["rows"][0]["mixed_outcomes_same_bound_context"])

    def test_skips_and_provisional_failures_excluded_from_rates(self):
        h.ingest(self.run_fixture("a", "PASS"), self.store)
        h.ingest(self.run_fixture("b", "NOT_RUN"), self.store)
        log = self.root / "live.log"
        log.write_text("1/2 Test #2: traffic ...***Failed 2.00 sec\n")
        self.assertEqual(h.observe("live", "iverilog", log, self.store), 1)
        self.assertEqual(h.observe("live", "iverilog", log, self.store), 0)
        r = self.report()
        self.assertEqual(r["rows"][0]["executed_attempts"], 1)
        self.assertEqual(r["rows"][0]["nonpass_rate"], 0)
        self.assertEqual(r["provisional_count"], 1)

    def test_conflicting_same_run_cannot_rewrite_history(self):
        run = self.run_fixture("a", "FAIL")
        h.ingest(run, self.store)
        r = json.loads((run / "results.json").read_text())
        r["profile"] = "full"
        (run / "results.json").write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            h.ingest(run, self.store)
        self.assertEqual(h.records(self.store, "runs")[0]["profile"], "daily")

    def test_missing_xml_and_false_green_are_rejected(self):
        run = self.run_fixture("a")
        xml = run / "iverilog/results.xml"
        self.assertFalse(xml.is_symlink())
        self.assertNotEqual(xml.resolve(), self.root.resolve())
        self.assertTrue(xml.resolve().is_relative_to(self.root.resolve()))
        xml.unlink()
        with self.assertRaises(ValueError):
            h.ingest(run, self.store)
        run = self.run_fixture("b", "FAIL")
        r = json.loads((run / "results.json").read_text())
        r["status"] = "PASS"
        (run / "results.json").write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            h.ingest(run, self.store)

    def test_resource_escape_is_rejected(self):
        run = self.run_fixture("a")
        r = json.loads((run / "results.json").read_text())
        r["engines"][0]["tests"][0]["resource_record"] = "../outside"
        (run / "results.json").write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            h.ingest(run, self.store)

    def test_resource_missing_for_not_run_is_allowed(self):
        run = self.run_fixture("a", "NOT_RUN")
        r = json.loads((run / "results.json").read_text())
        r["engines"][0]["tests"][0]["resource_record"] = "iverilog/missing.json"
        (run / "results.json").write_bytes(h.canonical(r))
        h.ingest(run, self.store)
        self.assertEqual(self.report()["rows"][0]["executed_attempts"], 0)

    def test_cause_is_separate_and_requires_evidence(self):
        h.ingest(self.run_fixture("a", "FAIL"), self.store)
        anchor = self.root / "diagnosis.txt"
        anchor.write_text("real cause evidence")
        review = {
            "run_id": "a",
            "name": "traffic",
            "engine": "iverilog",
            "cause": "TEST_HARNESS",
            "reason": "counterexample in driver",
            "reviewer": "fixture",
            "recorded_utc": "2026-01-02T00:00:00Z",
            "evidence": [
                {"path": str(anchor), "sha256": h.digest(anchor.read_bytes())}
            ],
        }
        h.triage(self.store, review)
        row = self.report()["rows"][0]
        self.assertEqual(row["cause_counts"], {"TEST_HARNESS": 1})
        self.assertEqual(row["counts"], {"FAIL": 1})
        review["evidence"][0]["sha256"] = "wrong"
        with self.assertRaises(ValueError):
            h.triage(self.store, review)

    def inventory(self, n=12):
        inv = self.root / "inventory.json"
        policy = self.root / "policy.json"
        inv.write_bytes(
            h.canonical(
                {
                    "tests": [
                        {
                            "name": f"t{i:02}",
                            "command": ["python", "driver.py", "--work", str(i)],
                            "properties": [],
                        }
                        for i in range(n)
                    ]
                }
            )
        )
        policy.write_bytes(
            h.canonical({"daily": [{"tests": ["t00"], "purpose": "smoke"}]})
        )
        return inv, policy

    def test_rotation_reaches_every_entry_even_with_hot_failures(self):
        inv, policy = self.inventory()
        # A persistent high-risk test must not starve the fair half of the queue.
        h.append(
            self.store,
            "observations",
            "hot",
            {
                "run_id": "live",
                "name": "t11",
                "engine": "iverilog",
                "provisional": True,
            },
        )
        seen = set()
        for i in range(6):
            batch = audit_suite.rotate(
                inv, policy, self.store, self.root / f"batch{i}", 4
            )
            self.assertLessEqual(len(batch["names"]), 4)
            self.assertEqual(batch["reviewed_count"], 0)
            seen.update(batch["names"])
        self.assertEqual(len(seen), 12)
        self.assertEqual(h.records(self.store, "audit-reviews"), [])

    def test_audit_does_not_claim_duplicate_is_redundant(self):
        inv, policy = self.inventory(2)
        b = audit_suite.rotate(inv, policy, self.store, self.root / "batch", 2)
        for r in b["rows"]:
            self.assertIn(
                "POTENTIAL_SAME_COMMAND_REUSE_NOT_PROVEN_REDUNDANT", r["findings"]
            )
            self.assertIsNone(r["decision"])
        self.assertEqual(b["tests_executed"], 0)

    def test_review_cannot_be_recorded_without_opposition_and_evidence(self):
        inv, policy = self.inventory(2)
        b = audit_suite.rotate(inv, policy, self.store, self.root / "batch", 2)
        r = {"batch_id": b["batch_id"], "name": "t00", "decision": "MERGE"}
        p = self.root / "review.json"
        p.write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            audit_suite.record_review(self.store, p)
        for field in [
            "owner",
            "reviewer",
            "recorded_utc",
            "retain_case",
            "simplify_case",
            "add_case",
            "adversarial_check",
            "acceptance",
            "risk",
        ]:
            r[field] = field
        r["evidence"] = [{"path": str(inv), "sha256": h.digest(inv.read_bytes())}]
        p.write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            audit_suite.record_review(self.store, p)
        r.update(
            validation_plan="same input negative-control comparison",
            coverage_preservation="retain all reachable faults",
        )
        p.write_bytes(h.canonical(r))
        self.assertTrue(audit_suite.record_review(self.store, p))
        self.assertEqual(
            h.records(self.store, "audit-reviews")[0]["status"],
            "REVIEWED_PROPOSAL_NOT_APPLIED",
        )
        r["risk"] = "second round"
        p.write_bytes(h.canonical(r))
        audit_suite.record_review(self.store, p)
        r["risk"] = "third round"
        p.write_bytes(h.canonical(r))
        with self.assertRaises(ValueError):
            audit_suite.record_review(self.store, p)


if __name__ == "__main__":
    unittest.main()
