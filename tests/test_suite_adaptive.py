"""Exercise gradual admission and unchanged CTest dependency/failure semantics."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_suite import GIB, cmake_quote, read_results
from suite_adaptive import (
    Admission,
    busy_fraction,
    components,
    duration_class,
    merge_junit,
    take_wave,
)
from test_suite_runner import test

HERE = Path(__file__).resolve().parent


class AdaptiveContract(unittest.TestCase):
    def setUp(self):
        runtime = tempfile.TemporaryDirectory(prefix="suite-adaptive-runtime-")
        self.addCleanup(runtime.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime.name})
        environment.start()
        self.addCleanup(environment.stop)

    def admission(self):
        return Admission(8, 2, 30, 2, 16 * GIB, 128 * GIB, 8 * GIB, 0.5)

    def test_increase_one_only_after_continuous_headroom(self):
        a = self.admission()
        for stamp in (0, 5, 29):
            self.assertEqual(
                a.update(stamp, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 2
            )
        self.assertEqual(a.update(30, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 3)
        self.assertEqual(a.update(31, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 3)
        for stamp in range(60, 500, 30):
            row = a.update(stamp, 200 * GIB, 0.1, [0, 0])
        self.assertEqual(row["slots_per_engine"], 8)

    def test_cpu_pressure_reduces_and_restarts_healthy_window(self):
        a = self.admission()
        a.update(0, 200 * GIB, 0.1, [0, 0])
        self.assertEqual(a.update(29, 200 * GIB, 0.95, [0, 0])["slots_per_engine"], 1)
        self.assertEqual(a.update(30, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 1)
        self.assertEqual(a.update(60, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 2)

    def test_memory_forecast_pauses_before_reserve_then_recovers_gradually(self):
        a = self.admission()
        a.update(0, 200 * GIB, 0.1, [0, 0])
        row = a.update(1, 190 * GIB, 0.1, [0, 0])
        self.assertEqual(row["reason"], "PAUSE_MEMORY")
        self.assertEqual(row["slots_per_engine"], 0)
        self.assertEqual(a.update(2, 190 * GIB, 0.1, [0, 0])["slots_per_engine"], 0)
        self.assertEqual(a.update(32, 190 * GIB, 0.1, [0, 0])["slots_per_engine"], 1)

    def test_observed_memory_reduces_ceiling_without_forgetting_peak(self):
        a = self.admission()
        row = a.update(0, 200 * GIB, 0.1, [0, 0], 70 * GIB)
        self.assertEqual(row["slots_per_engine"], 1)
        self.assertEqual(a.update(50, 200 * GIB, 0.1, [0, 0])["slots_per_engine"], 1)
        self.assertEqual(
            a.update(51, 200 * GIB, 0.1, [95 * GIB, 0])["slots_per_engine"], 0
        )

    def test_fixture_and_dependency_components_not_split(self):
        tests = [
            test("use", FIXTURES_REQUIRED=["f"], DEPENDS=["dep"]),
            test("setup", FIXTURES_SETUP=["f"]),
            test("cleanup", FIXTURES_CLEANUP=["f"]),
            test("dep"),
            test("other"),
        ]
        pending = components(tests)
        self.assertEqual(
            {t["name"] for t in take_wave(pending, 1)},
            {"use", "setup", "cleanup", "dep"},
        )
        self.assertEqual(take_wave(pending, 0), [])
        self.assertEqual(take_wave(pending, 1)[0]["name"], "other")
        with self.assertRaises(ValueError):
            components([test("use", DEPENDS=["missing"])])

    def test_processors_weight_limits_wave(self):
        pending = components(
            [test("a", PROCESSORS=2), test("b", PROCESSORS=2), test("c")]
        )
        self.assertEqual([t["name"] for t in take_wave(pending, 3)], ["a"])
        self.assertEqual([t["name"] for t in take_wave(pending, 3)], ["b", "c"])

    def test_cpu_measurement_and_duration_boundaries(self):
        self.assertAlmostEqual(busy_fraction((100, 50), (200, 125)), 0.25)
        self.assertEqual(busy_fraction((100, 50), (100, 50)), 1)
        for seconds, speed in (
            (30, "FAST"),
            (31, "MEDIUM"),
            (120, "MEDIUM"),
            (121, "SLOW"),
            (600, "SLOW"),
            (601, "VERY_SLOW"),
        ):
            self.assertEqual(duration_class("PASS", seconds)["speed"], speed)
        self.assertTrue(duration_class("TIMEOUT", 1800)["lower_bound"])
        self.assertEqual(duration_class("NOT_RUN", 0)["speed"], "UNMEASURED")

    def test_junit_merge_keeps_failure_and_rejects_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            a = p / "a.xml"
            b = p / "b.xml"
            out = p / "all.xml"
            a.write_text(
                '<testsuite><testcase name="bad" time="2"><failure>assert</failure></testcase></testsuite>'
            )
            b.write_text('<testsuite><testcase name="good" time="1"/></testsuite>')
            selected = [test("bad"), test("good"), test("notrun")]
            merge_junit([a, b], out, selected)
            self.assertEqual(
                [r["status"] for r in read_results(out, selected)],
                ["FAIL", "PASS", "NOT_RUN"],
            )
            with self.assertRaises(ValueError):
                merge_junit([a, a], out, selected)

    def test_real_waves_run_fixture_once_and_retain_earlier_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build = root / "build"
            build.mkdir()
            out = root / "out"
            marker = root / "marker"
            count = root / "count"
            specs = [
                ("bad", "raise SystemExit(3)", {}),
                (
                    "setup",
                    f'from pathlib import Path;Path({str(marker)!r}).write_text("ok");Path({str(count)!r}).write_text("once")',
                    {"FIXTURES_SETUP": "f"},
                ),
                (
                    "use1",
                    f"from pathlib import Path;assert Path({str(marker)!r}).exists()",
                    {"FIXTURES_REQUIRED": "f"},
                ),
                (
                    "use2",
                    f"from pathlib import Path;assert Path({str(marker)!r}).exists()",
                    {"FIXTURES_REQUIRED": "f", "DEPENDS": "use1"},
                ),
                (
                    "cleanup",
                    f"from pathlib import Path;p=Path({str(marker)!r});r=Path({str(root)!r}).resolve();"
                    "assert not p.is_symlink();assert p.resolve()!=r and p.resolve().is_relative_to(r);p.unlink()",
                    {"FIXTURES_CLEANUP": "f"},
                ),
                ("tail", 'print("done")', {}),
            ]
            lines = []
            for name, code, properties in specs:
                lines.append(
                    "add_test("
                    + name
                    + " "
                    + cmake_quote(sys.executable)
                    + " -c "
                    + cmake_quote(code)
                    + ")"
                )
                for key, value in {"TIMEOUT": "5", **properties}.items():
                    lines.append(
                        "set_tests_properties("
                        + name
                        + " PROPERTIES "
                        + key
                        + " "
                        + cmake_quote(value)
                        + ")"
                    )
            (build / "CTestTestfile.cmake").write_text("\n".join(lines))
            args = [
                sys.executable,
                str(HERE / "run_suite.py"),
                "--profile",
                "full",
                "--build",
                str(build),
                "--engine",
                "iverilog",
                "--output",
                str(out),
                "--history",
                str(root / "history"),
                "--run",
                "--protection",
                "monitored",
                "--cpu-fraction",
                "1",
                "--initial-jobs",
                "1",
                "--max-jobs",
                "2",
                "--ramp-seconds",
                "0.1",
                "--max-memory-gib",
                "1",
                "--reserve-gib",
                "0.1",
                "--memory-per-slot-gib",
                "0.1",
                "--disk-reserve-gib",
                "0.01",
            ]
            run = subprocess.run(
                args, capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            results = json.loads((out / "results.json").read_text())
            engine = results["engines"][0]
            self.assertEqual(engine["counts"], {"FAIL": 1, "PASS": 5})
            self.assertNotEqual(engine["ctest_exit"], 0)
            waves = json.loads((out / "iverilog/command.json").read_text())["waves"]
            self.assertGreater(len(waves), 1)
            names = [n for w in waves for n in w["names"]]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(count.read_text(), "once")
            self.assertFalse(marker.exists())
            self.assertEqual(
                len(json.loads((out / "durations.json").read_text())["rows"]), 6
            )


if __name__ == "__main__":
    unittest.main()
