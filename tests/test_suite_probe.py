"""Check missing-sample semantics and preservation of real process outcomes."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from run_suite import cmake_quote, read_results

HERE = Path(__file__).resolve().parent


class ProbeContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="suite-probe-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_probe_preserves_child_signals_and_receipts(self):
        for sig in (None, signal.SIGTERM, signal.SIGUSR1, signal.SIGKILL):
            with self.subTest(signal=sig):
                record = self.root / f"child-{sig}.json"
                script = ("import os,signal,resource;resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
                          + (f"os.kill(os.getpid(),{int(sig)})" if sig else "raise SystemExit(0)"))
                result = subprocess.run(
                    [sys.executable, str(HERE / "suite_probe.py"), "--record", str(record),
                     "--", sys.executable, "-c", script],
                    capture_output=True, text=True, timeout=10,
                )
                expected = -sig if sig else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                receipt = json.loads(record.read_text())
                self.assertTrue(receipt["final"])
                self.assertEqual(receipt["exit_code"], expected)
                self.assertEqual(receipt["termination_signal"], sig)
                self.assertEqual(receipt["termination_origin"], "child" if sig else None)
                self.assertEqual(receipt["record_state"], "COMPLETED")
                self.assertGreater(receipt["elapsed_seconds"], 0)
                self.assertIsNotNone(receipt["children_cpu_user_seconds"])
                self.assertIsNotNone(receipt["children_maxrss_bytes"])

    def test_probe_will_fail_does_not_invert_child_signal(self):
        script = self.root / "terminate.py"
        script.write_text("import os,signal,resource\nresource.setrlimit(resource.RLIMIT_CORE,(0,0))\nos.kill(os.getpid(),signal.SIGTERM)\n")
        build = self.root / "signal-ctest"
        build.mkdir()
        commands = {
            "plain": [sys.executable, str(script)],
            "wrapped": [sys.executable, str(HERE / "suite_probe.py"), "--record",
                        str(self.root / "signal.json"), "--", sys.executable, str(script)],
            "ordinary_nonzero": [sys.executable, str(HERE / "suite_probe.py"), "--record",
                                 str(self.root / "nonzero.json"), "--", sys.executable,
                                 "-c", "raise SystemExit(7)"],
        }
        (build / "CTestTestfile.cmake").write_text("\n".join(
            "add_test(" + name + " " + " ".join(cmake_quote(a) for a in command) + ")\n"
            + "set_tests_properties(" + name + " PROPERTIES WILL_FAIL TRUE TIMEOUT 10)"
            for name, command in commands.items()))
        xml = self.root / "signals.xml"
        result = subprocess.run(["ctest", "--test-dir", str(build), "--output-junit", str(xml)],
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        verdicts = {r["name"]: r["status"] for r in read_results(xml, [{"name": n} for n in commands])}
        self.assertEqual(verdicts, {"plain": "FAIL", "wrapped": "FAIL", "ordinary_nonzero": "PASS"})

    def test_probe_wrapper_signal_cleans_child_and_records_origin(self):
        record = self.root / "interrupted.json"
        child_pid = self.root / "child.pid"
        script = ("import os,time;from pathlib import Path;"
                  + f"Path({str(child_pid)!r}).write_text(str(os.getpid()));time.sleep(30)")
        with subprocess.Popen([sys.executable, str(HERE / "suite_probe.py"), "--record", str(record),
                               "--", sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + 30
            while not child_pid.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_pid.exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, -signal.SIGTERM, stderr)
        receipt = json.loads(record.read_text())
        self.assertTrue(receipt["final"])
        self.assertEqual(receipt["record_state"], "INTERRUPTED")
        self.assertFalse(receipt["workload_completed"])
        self.assertEqual(receipt["termination_origin"], "wrapper")
        self.assertEqual(receipt["termination_signal"], signal.SIGTERM)
        self.assertEqual(receipt["exit_code"], -signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(child_pid.read_text()), 0)

    def run_probe(self, program, override="", missing=False):
        record = self.root / "resources.json"
        command = (
            ["missing-probe-test-executable"]
            if missing
            else [sys.executable, "-c", program]
        )
        wrapper = (
            "import sys;sys.path.insert(0,"
            + repr(str(HERE))
            + ");import suite_probe;"
            + override
        )
        wrapper += (
            "sys.argv=['suite_probe','--record',"
            + repr(str(record))
            + ",'--',*"
            + repr(command)
            + "];raise SystemExit(suite_probe.main())"
        )
        run = subprocess.run(
            [sys.executable, "-c", wrapper],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return run, json.loads(record.read_text())

    def test_missed_short_child_keeps_rusage_and_null_tree(self):
        run, row = self.run_probe(
            "a=bytearray(32*1024*1024); print('actual')",
            "suite_probe.process_table=lambda:{};",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("actual", run.stdout)
        self.assertIsNone(row["peak_tree_rss_bytes"])
        self.assertEqual(row["peak_tree_rss_status"], "NO_PROCESS_SAMPLE")
        self.assertGreater(row["children_maxrss_bytes"], 32 * 1024**2)
        self.assertEqual(row["children_usage_status"], "REAPED_CHILDREN")
        self.assertIsNotNone(row["children_cpu_user_seconds"])

    def test_zero_only_proc_sample_is_not_a_zero_memory_claim(self):
        override = "suite_probe.process_table=lambda:{1:(0,'1',0)};suite_probe.descendants=lambda *a:{1};"
        run, row = self.run_probe("pass", override)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsNone(row["peak_tree_rss_bytes"])
        self.assertEqual(row["peak_tree_rss_status"], "NO_POSITIVE_RSS_SAMPLE")
        self.assertGreater(row["process_tree_samples"], 0)
        self.assertEqual(row["positive_rss_samples"], 0)

    def test_missing_executable_has_unavailable_usage(self):
        run, row = self.run_probe("", missing=True)
        self.assertEqual(run.returncode, 127)
        self.assertEqual(row["record_state"], "EXECUTION_ERROR")
        self.assertIsNone(row["peak_tree_rss_bytes"])
        self.assertIsNone(row["children_maxrss_bytes"])
        self.assertIsNone(row["children_cpu_user_seconds"])
        self.assertFalse(row["workload_completed"])

    def test_interruption_keeps_partial_record_and_reaped_usage(self):
        record = self.root / "interrupted.json"
        process = subprocess.Popen(
            [
                sys.executable,
                str(HERE / "suite_probe.py"),
                "--record",
                str(record),
                "--",
                sys.executable,
                "-c",
                "import time;a=bytearray(8*1024*1024);time.sleep(30)",
            ]
        )
        try:
            deadline = time.monotonic() + 30
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(record.exists())
            running = json.loads(record.read_text())
            self.assertFalse(running["final"])
            self.assertEqual(running["record_state"], "RUNNING")
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=30), -signal.SIGTERM)
            final = json.loads(record.read_text())
            self.assertTrue(final["final"])
            self.assertEqual(final["record_state"], "INTERRUPTED")
            self.assertFalse(final["workload_completed"])
            self.assertEqual(final["children_usage_status"], "REAPED_CHILDREN")
            self.assertGreaterEqual(
                final["elapsed_seconds"], running["elapsed_seconds"]
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)

    def test_stage_directory_propagation_and_hash_binding(self):
        from run_suite import collect_stage_metrics, sha

        program = "import os,json,pathlib;d=pathlib.Path(os.environ['NOCLOOM_SIM_METRICS_DIR']);d.mkdir();(d/'call.json').write_text(json.dumps({'stage':'compile','case':os.environ['NOCLOOM_SIM_CASE'],'max_rss_bytes':None}))"
        run, row = self.run_probe(program)
        self.assertEqual(run.returncode, 0, run.stderr)
        directory = self.root / "resources.stages"
        self.assertEqual(row["stage_metrics_directory"], str(directory))
        collected = collect_stage_metrics(self.root / "resources.json", self.root)
        self.assertEqual(collected["status"], "COLLECTED")
        self.assertEqual(collected["completeness"], "NOT_INFERRED_FROM_RECORD_PRESENCE")
        record = collected["records"][0]
        self.assertEqual(record["sha256"], sha(directory / "call.json"))
        self.assertEqual(record["resources"]["case"], "resources")
        self.assertIsNone(record["resources"]["max_rss_bytes"])
        absent = collect_stage_metrics(self.root / "absent.json", self.root)
        self.assertEqual(absent["status"], "NO_RECORDS_OBSERVED")

    def test_stage_record_escape_is_rejected(self):
        from run_suite import collect_stage_metrics

        directory = self.root / "resources.stages"
        directory.mkdir()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.json"
            target.write_text("{}")
            (directory / "linked.json").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "escapes"):
                collect_stage_metrics(self.root / "resources.json", self.root)

    def test_invalid_interval_rejected(self):
        for interval in ("0", "-1", "nan", "inf"):
            run = subprocess.run(
                [
                    sys.executable,
                    str(HERE / "suite_probe.py"),
                    "--record",
                    str(self.root / "no.json"),
                    "--sample-interval",
                    interval,
                    "--",
                    "true",
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(run.returncode, 2)
        self.assertFalse((self.root / "no.json").exists())

    def test_null_rss_runner_admission_sort_and_history(self):
        import test_suite_runner

        copied = self.root / "tools"
        copied.mkdir()
        for name in (
            "run_suite.py",
            "suite_probe.py",
            "suite_history.py",
            "suite_adaptive.py",
            "audit_suite.py",
            "suite_profiles.json",
        ):
            shutil.copy2(HERE / name, copied / name)
        probe = copied / "suite_probe.py"
        probe.write_text(
            probe.read_text().replace(
                'if __name__ == "__main__":',
                'if __name__ == "__main__":\n    process_table = lambda: {}',
            )
        )
        contract = test_suite_runner.SuiteContract()
        contract.setUp()
        try:
            with patch.object(test_suite_runner, "HERE", copied):
                contract.test_complete_runner_and_manifest()
            rows = json.loads((contract.root / "result/results.json").read_text())[
                "engines"
            ]
            self.assertTrue(
                all(
                    row["tests"][0]["resources"]["peak_tree_rss_bytes"] is None
                    for row in rows
                )
            )
            self.assertIn(
                "UNMEASURED", (contract.root / "result/SUMMARY.md").read_text()
            )
        finally:
            contract.doCleanups()


if __name__ == "__main__":
    unittest.main()
