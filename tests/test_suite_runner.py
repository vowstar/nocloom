"""Check suite selection, resource refusal, tracing and truthful result handling."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_suite import GIB, budget, cmake_quote, instrument, read_results, select

HERE = Path(__file__).resolve().parent


def test(name, **props):
    return {
        "name": name,
        "command": [sys.executable, "-c", "pass"],
        "properties": [{"name": k, "value": v} for k, v in props.items()],
    }


class SuiteContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="suite-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        runtime = self.root / "runtime"
        runtime.mkdir()
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_daily_is_explicit_full_includes_new(self):
        inv = {"tests": [test("quick"), test("new-slow")]}
        policy = {"daily": [{"tests": ["quick"]}]}
        self.assertEqual([t["name"] for t in select(inv, "daily", policy)], ["quick"])
        self.assertEqual(len(select(inv, "full", policy)), 2)

    def test_daily_includes_affected_full_only_test(self):
        inv = {"tests": [test("daily"), test("slow")]}
        policy = {"daily": [{"tests": ["daily"]}]}
        self.assertEqual(len(select(inv, "daily", policy, ["slow"])), 2)
        with self.assertRaises(ValueError):
            select(inv, "daily", policy, ["missing"])

    def test_selected_unavailable_command_is_rejected(self):
        inv = {"tests": [test("ready"), {"name": "unbuilt", "properties": []}]}
        policy = {"daily": [{"purpose": "ready only", "tests": ["ready"]}]}
        self.assertEqual([t["name"] for t in select(inv, "daily", policy)], ["ready"])
        for profile, include in (("full", []), ("daily", ["unbuilt"])):
            with self.assertRaisesRegex(
                ValueError, "Selected CTest command unavailable: unbuilt"
            ):
                select(inv, profile, policy, include)
        inv["tests"][0]["command"] = [sys.executable, "-c", ""]
        self.assertEqual(
            select(inv, "daily", policy)[0]["command"], [sys.executable, "-c", ""]
        )
        inv["tests"][0]["properties"] = [{"name": "DEPENDS", "value": ["unbuilt"]}]
        with self.assertRaisesRegex(ValueError, "Selected CTest command unavailable"):
            select(inv, "daily", policy)

    def test_daily_typo_is_failure(self):
        with self.assertRaises(ValueError):
            select(
                {"tests": [test("quick")]}, "daily", {"daily": [{"tests": ["typo"]}]}
            )

    def test_empty_and_duplicate_inventory_refused(self):
        for tests in ([], [test("same"), test("same")]):
            with self.assertRaises(ValueError):
                select({"tests": tests}, "full", {"daily": []})

    def test_fixture_dependency_closure(self):
        inv = {
            "tests": [
                test("use", FIXTURES_REQUIRED=["data"], DEPENDS=["dep"]),
                test("setup", FIXTURES_SETUP=["data"]),
                test("cleanup", FIXTURES_CLEANUP=["data"]),
                test("dep"),
                test("other"),
            ]
        }
        selected = select(inv, "daily", {"daily": [{"tests": ["use"]}]})
        self.assertEqual(
            {t["name"] for t in selected}, {"use", "setup", "cleanup", "dep"}
        )

    def args(self):
        return argparse.Namespace(
            reserve_gib=16,
            max_memory_gib=256,
            cpu_fraction=0.5,
            max_jobs=16,
            memory_per_slot_gib=8,
            protection="monitored",
        )

    def test_dual_budget_does_not_double_host_allocation(self):
        host = {
            "cpu_capacity": 160,
            "load1": 2,
            "memory_total": 3000 * GIB,
            "memory_available": 1600 * GIB,
        }
        b = budget(host, 2, 8, self.args())
        self.assertEqual(b["jobs_per_engine"], 16)
        self.assertEqual(b["memory_bytes_per_engine"], 128 * GIB)
        self.assertGreaterEqual(b["host_reserve_bytes"], 300 * GIB)

    def test_low_memory_load_and_fractional_quota_refuse(self):
        host = {
            "cpu_capacity": 16,
            "load1": 0,
            "memory_total": 64 * GIB,
            "memory_available": 6 * GIB,
        }
        with self.assertRaises(ValueError):
            budget(host, 2, 8, self.args())
        host.update(memory_total=3000 * GIB, memory_available=1600 * GIB, load1=16)
        with self.assertRaises(ValueError):
            budget(host, 2, 8, self.args())
        host.update(cpu_capacity=0.5, load1=0)
        with self.assertRaises(ValueError):
            budget(host, 1, 1, self.args())

    def test_oversized_processors_memory_is_reserved(self):
        a = self.args()
        a.max_memory_gib = 32
        with self.assertRaises(ValueError):
            budget(
                {
                    "cpu_capacity": 160,
                    "load1": 0,
                    "memory_total": 3000 * GIB,
                    "memory_available": 1600 * GIB,
                },
                1,
                8,
                a,
            )

    def test_junit_fail_timeout_skip_missing(self):
        path = self.root / "results.xml"
        path.write_text(
            '<testsuite><testcase name="ok" time="1"/><testcase name="bad"><failure>x</failure></testcase>'
            '<testcase name="late"><failure>Timeout</failure></testcase><testcase name="skip"><skipped/></testcase></testsuite>'
        )
        rows = read_results(
            path, [test(n) for n in ["ok", "bad", "late", "skip", "absent"]]
        )
        self.assertEqual(
            [r["status"] for r in rows],
            ["PASS", "FAIL", "TIMEOUT", "NOT_RUN", "NOT_RUN"],
        )
        self.assertEqual(
            read_results(self.root / "missing.xml", [test("x")])[0]["status"], "NOT_RUN"
        )

    def test_duplicate_and_unexpected_junit_refused(self):
        p = self.root / "results.xml"
        for body in (
            '<testcase name="x"/><testcase name="x"/>',
            '<testcase name="y"/>',
        ):
            p.write_text("<testsuite>" + body + "</testsuite>")
            with self.assertRaises(ValueError):
                read_results(p, [test("x")])

    def test_timeout_words_in_output_do_not_reclassify_failure(self):
        path = self.root / "result.xml"
        for body in (
            '<failure message="Failed"/><system-out>call(timeout=120): ValueError</system-out>',
            '<failure message="Failed">TimeoutError in a nested command</failure>',
            '<error type="RuntimeError"/><system-out>timed out</system-out>',
            "<failure>expected timeout but got wrong response</failure>",
        ):
            path.write_text(
                '<testsuite><testcase name="x">' + body + "</testcase></testsuite>"
            )
            self.assertEqual(read_results(path, [test("x")])[0]["status"], "FAIL")
        path.write_text(
            '<testsuite><testcase name="x"><failure message="Timeout"/>'
            "<system-out>no additional output</system-out></testcase></testsuite>"
        )
        self.assertEqual(read_results(path, [test("x")])[0]["status"], "TIMEOUT")

    def test_probe_memory_and_exit_passthrough(self):
        record = self.root / "probe.json"
        cmd = [
            sys.executable,
            str(HERE / "suite_probe.py"),
            "--record",
            str(record),
            "--",
            sys.executable,
            "-c",
            "import time; a=bytearray(24*1024*1024); time.sleep(0.7); raise SystemExit(7)",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        r = json.loads(record.read_text())
        self.assertTrue(r["final"])
        self.assertEqual(r["exit_code"], 7)
        self.assertGreater(r["peak_tree_rss_bytes"], 24 * 1024 * 1024)
        self.assertGreaterEqual(r["samples"], 2)

    def test_instrument_preserves_timeout_environment_and_space(self):
        t = test(
            "space name",
            TIMEOUT=0.3,
            PROCESSORS=2,
            ENVIRONMENT=["TEST_FLAG=bound"],
            LABELS=["one", "two"],
        )
        t["command"] = [
            sys.executable,
            "-c",
            'import os,time; assert os.environ["TEST_FLAG"]=="bound"; time.sleep(4)',
        ]
        build = self.root / "ctest"
        instrument([t], build, self.root)
        actual = json.loads(
            subprocess.check_output(
                ["ctest", "--test-dir", str(build), "--show-only=json-v1"]
            )
        )
        p = {x["name"]: x["value"] for x in actual["tests"][0]["properties"]}
        self.assertEqual(p["TIMEOUT"], 0.3)
        self.assertEqual(p["PROCESSORS"], 2)
        self.assertEqual(p["ENVIRONMENT"], ["TEST_FLAG=bound"])
        result = subprocess.run(
            [
                "ctest",
                "--test-dir",
                str(build),
                "--output-junit",
                str(self.root / "results.xml"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            read_results(self.root / "results.xml", [t])[0]["status"], "TIMEOUT"
        )

    def test_cmake_bracket_escape(self):
        self.assertEqual(cmake_quote("a]=]b"), "[==[a]=]b]==]")

    def test_fake_cgroup_is_rejected(self):
        from run_suite import create_cgroup

        with self.assertRaises(ValueError):
            create_cgroup(self.root, "fake-cap", 1024 * 1024)
        self.assertFalse((self.root / "fake-cap").exists())

    def test_unknown_junit_state_is_rejected(self):
        path = self.root / "unknown.xml"
        path.write_text('<testsuite><testcase name="x" status="unknown"/></testsuite>')
        with self.assertRaises(ValueError):
            read_results(path, [test("x")])

    def test_progress_cannot_hide_failed_or_timeout(self):
        from run_suite import progress

        path = self.root / "progress.log"
        path.write_text(
            "1/3 Test #1: x ... Passed 1.00 sec\n2/3 Test #2: y ...***Failed 2.00 sec\n3/3 Test #3: z ...***Timeout 3.00 sec\n"
        )
        row = progress(path, 3)
        self.assertEqual(row["completed"], 3)
        self.assertEqual(row["nonpass"], ["y", "z"])
        self.assertTrue(row["provisional"])

    def test_telemetry_reader_retains_bad_bytes_and_rejects_escape(self):
        from run_suite import collect_stage_metrics, read_telemetry, sha

        path = self.root / "bad.json"
        for raw in (
            "{",
            "[]",
            '{"peak_tree_rss_bytes": "bad"}',
            '{"max_rss_bytes": -1}',
        ):
            path.write_text(raw)
            row = read_telemetry(path, self.root)
            self.assertEqual(row["status"], "TELEMETRY_ERROR")
            self.assertEqual(row["sha256"], sha(path))
            self.assertTrue(row["error"])
            self.assertNotIn("resources", row)
        self.assertEqual(
            collect_stage_metrics(path, self.root)["status"], "NO_RECORDS_OBSERVED"
        )
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "outside.json"
            link.symlink_to(Path(outside) / "target")
            with self.assertRaisesRegex(ValueError, "escapes"):
                read_telemetry(link, self.root)

    def test_bad_telemetry_keeps_junit_history_and_archive(self):
        import hashlib
        import os

        for mode in ("malformed", "unreadable", "escape"):
            with self.subTest(mode=mode):
                root = self.root / mode
                root.mkdir()
                builds = []
                for engine in ("iverilog", "external"):
                    build = root / engine
                    build.mkdir()
                    (build / "CTestTestfile.cmake").write_text(
                        "add_test(tiny "
                        + cmake_quote(sys.executable)
                        + " -c "
                        + cmake_quote("print('hello')")
                        + ")\n"
                        + "add_test(broken "
                        + cmake_quote(sys.executable)
                        + " -c "
                        + cmake_quote("raise SystemExit(7)")
                        + ")\n"
                    )
                    builds.append(build)
                policy, adapter = root / "policy.json", root / "adapter.json"
                policy.write_text(
                    json.dumps({"daily": [{"tests": ["tiny", "broken"]}]})
                )
                adapter.write_text("{}")
                outside = root / "outside.json"
                outside.write_text("PRIVATE_OUTSIDE_SENTINEL")
                wrapper = root / "inject.py"
                wrapper.write_text(
                    "import sys,json\nfrom pathlib import Path\nsys.path.insert(0,"
                    + repr(str(HERE))
                    + ")\nimport run_suite\n"
                    "original=run_suite.read_results\nseen=set()\n"
                    "def injected(path, selected):\n"
                    " rows=original(path,selected)\n"
                    " engine=Path(path).parent\n"
                    " if not (engine/'resource-map.json').exists() or str(engine) in seen:return rows\n"
                    " seen.add(str(engine))\n"
                    " mapping=json.loads((engine/'resource-map.json').read_text())\n"
                    " parent=engine/mapping['tiny']\n"
                    " directory=parent.with_suffix('.stages');directory.mkdir(exist_ok=True)\n"
                    " stage=directory/'bad.json'\n"
                    " mode=" + repr(mode) + "\n"
                    " if mode=='malformed':\n"
                    "  parent.write_text('{');stage.write_text('{')\n"
                    " elif mode=='unreadable':\n"
                    "  parent.chmod(0);stage.write_text('{}');stage.chmod(0)\n"
                    " else:\n"
                    "  stage.symlink_to(" + repr(str(outside)) + ")\n"
                    " return rows\n"
                    "run_suite.read_results=injected\nraise SystemExit(run_suite.main())\n"
                )
                out = root / "result"
                command = [
                    sys.executable,
                    str(wrapper),
                    "--history",
                    str(root / "history"),
                    "--profile",
                    "daily",
                    "--policy",
                    str(policy),
                    "--build",
                    str(builds[0]),
                    "--engine",
                    "iverilog",
                    "--build",
                    str(builds[1]),
                    "--engine",
                    "external",
                    "--sim-config",
                    str(adapter),
                    "--output",
                    str(out),
                    "--run",
                    "--scheduling",
                    "fixed",
                    "--protection",
                    "monitored",
                    "--max-memory-gib",
                    "1",
                    "--reserve-gib",
                    "0.1",
                    "--memory-per-slot-gib",
                    "0.1",
                    "--max-jobs",
                    "1",
                    "--disk-reserve-gib",
                    "0.01",
                ]
                # chmod tests require an unprivileged execution identity.
                if mode == "unreadable" and os.geteuid() == 0:
                    continue
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=20, check=False
                )
                (root / "runner.log").write_text(result.stdout + result.stderr)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                receipt = json.loads((out / "results.json").read_text())
                self.assertEqual(receipt["status"], "NONPASS")
                self.assertEqual(
                    receipt["measurement_status"], "MEASUREMENT_INCOMPLETE"
                )
                self.assertTrue(receipt["telemetry_errors"])
                self.assertTrue((out / "history-registration.json").is_file())
                self.assertFalse((out / "history-error.json").exists())
                self.assertIn(
                    "MEASUREMENT_INCOMPLETE", (out / "SUMMARY.md").read_text()
                )
                for engine in receipt["engines"]:
                    self.assertEqual(engine["counts"], {"PASS": 1, "FAIL": 1})
                    self.assertEqual(
                        {r["name"]: r["status"] for r in engine["tests"]},
                        {"tiny": "PASS", "broken": "FAIL"},
                    )
                errors = receipt["telemetry_errors"]
                if mode == "malformed":
                    self.assertTrue(
                        all(
                            row["sha256"] == hashlib.sha256(b"{").hexdigest()
                            for row in errors
                        )
                    )
                if mode == "unreadable":
                    self.assertTrue(all(row["sha256"] is None for row in errors))
                    self.assertTrue(
                        all("PermissionError" in row["error"] for row in errors)
                    )
                if mode == "escape":
                    self.assertTrue(all("escapes" in row["error"] for row in errors))
                    self.assertNotIn(
                        "PRIVATE_OUTSIDE_SENTINEL", (out / "results.json").read_text()
                    )
                    self.assertTrue(receipt["manifest_errors"])
                check = subprocess.run(
                    ["sha256sum", "-c", "MANIFEST.txt", "--quiet"],
                    cwd=out,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(check.returncode, 0, check.stderr)
                for path in out.rglob("*.json"):
                    if not path.is_symlink():
                        path.chmod(0o600)

    def test_complete_runner_and_manifest(self):
        builds = []
        for engine in ["iverilog", "external"]:
            build = self.root / engine
            build.mkdir()
            (build / "CTestTestfile.cmake").write_text(
                "add_test(tiny "
                + cmake_quote(sys.executable)
                + " -c "
                + cmake_quote('print("hello")')
                + ")\nadd_test(unbuilt "
                + cmake_quote(str(build / "unbuilt-executable"))
                + ")\n"
            )
            builds.append(build)
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"daily": [{"tests": ["tiny"]}]}))
        adapter = self.root / "adapter.json"
        adapter.write_text("{}")
        out = self.root / "result"
        cmd = [
            sys.executable,
            str(HERE / "run_suite.py"),
            "--history",
            str(self.root / "history"),
            "--profile",
            "daily",
            "--policy",
            str(policy),
            "--build",
            str(builds[0]),
            "--engine",
            "iverilog",
            "--build",
            str(builds[1]),
            "--engine",
            "external",
            "--sim-config",
            str(adapter),
            "--output",
            str(out),
            "--run",
            "--protection",
            "monitored",
            "--max-memory-gib",
            "1",
            "--reserve-gib",
            "0.1",
            "--memory-per-slot-gib",
            "0.1",
            "--max-jobs",
            "1",
            "--disk-reserve-gib",
            "0.01",
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        r = json.loads((out / "results.json").read_text())
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(len(r["engines"]), 2)
        self.assertTrue(all(x["tests"][0]["resources"]["final"] for x in r["engines"]))
        queue = json.loads((out / "next-audit/queue.json").read_text())
        unbuilt = next(row for row in queue["rows"] if row["name"] == "unbuilt")
        self.assertIsNone(unbuilt["command"])
        self.assertEqual(unbuilt["command_status"], "UNAVAILABLE")
        self.assertIn("COMMAND_UNAVAILABLE_IN_CTEST_INVENTORY", unbuilt["findings"])
        self.assertFalse((out / "history-error.json").exists())
        from suite_history import ingest, records

        self.assertFalse(ingest(out, self.root / "history")["added"])
        self.assertEqual(len(records(self.root / "history", "runs")), 1)
        check = subprocess.run(
            ["sha256sum", "-c", "MANIFEST.txt", "--quiet"],
            cwd=out,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
