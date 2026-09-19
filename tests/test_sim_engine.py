"""Exercise adapter failure boundaries without an HDL engine or private tools."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sim_engine import PASS_MARKERS, Simulation, selection
from cross_hardware_sim import simulate


# These observations do not mean that an HDL simulation reached its end.
# Bind each exception to its actual consumers so new uses require review.
NONTERMINAL_MARKERS = {
    "TREE_TOPOLOGY_CORE_PASS": {"install_examples.py", "test_sim_engine.py"},
    "API_FEATURE_CORE_PASS": {"install_examples.py", "test_sim_engine.py"},
    "ASYNC_FIFO_FORMAL_PASS": {"async_fifo_formal_driver_test.py", "test_sim_engine.py"},
}


def wait_for_process_exit(pid, timeout=10.0):
    """Allow signaled descendants to finish exiting; reject a live survivor.

    The wait returns as soon as the process is gone, so a long limit costs
    nothing on an idle host and tolerates a loaded one."""
    if not (Path("/proc") / "self/stat").is_file():
        raise RuntimeError("Process-exit observation requires Linux /proc")
    start = time.monotonic()
    status = Path("/proc") / str(pid) / "stat"
    while True:
        try:
            state = status.read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            state = "GONE"
        elapsed = time.monotonic() - start
        if state in ("GONE", "Z") or elapsed >= timeout:
            return dict(pid=pid, state=state, elapsed=elapsed,
                        exited=state in ("GONE", "Z"))
        time.sleep(0.01)


class Contract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="engine contract ", dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "network.v").write_text("module dut; endmodule\n")
        (self.work / "tb.sv").write_text("module tb; endmodule\n")
        self.config = self.work / "adapter.json"

    def engine(self, program, **extra):
        cfg = dict(version=1, compile=[sys.executable, "-c", "print('compiled')"],
                   run=[sys.executable, "-c", program, "{options}"])
        cfg.update(extra)
        self.config.write_text(json.dumps(cfg))
        return Simulation(self.work, "network.v", engine="external", config=self.config)

    def test_fifo_diagnostic_requires_a_verdict(self):
        import random
        import run_async_fifo as fifo

        class DiagnosticDesigns:
            def get(self, depth, stages, fault="none", handshake=False):
                return fault, handshake

            def run(self, design, name, args):
                if name == "binary_count_end_to_end":
                    return fifo.Designs.run(observed, name, args)
                fault, handshake = design
                if fault != "none":
                    labels = next((labels for key, labels, _ in fifo.FAULTS
                                   if key == fault), fifo.ONE_SIDE_LABELS)
                    return dict(passed=False, failure=sorted(labels)[0])
                if "no_handshake" in name:
                    return dict(passed=False, failure="FALSE_DATA")
                return dict(passed=True)

        for output, code, accepted in (
                ("", 0, False), ("", 3, False), ("ASYNC_FIFO_PASS", 0, False),
                ("ASYNC_FIFO_PASS delivered=400 canceled=0 generations=1 "
                 "meta_events=1 latency_min=4 latency_max=8 lost=0", 0, True),
                ("ASYNC_FIFO_PASS delivered=399 canceled=0 generations=1 "
                 "meta_events=1 latency_min=4 latency_max=8 lost=0", 0, False),
                ("ASYNC_FIFO_PASS delivered=400 canceled=0 generations=1 "
                 "meta_events=1 latency_min=4 latency_max=8 lost=1", 0, False),
                ("ASSERT_FAIL:ASYNC_FIFO.ORDER", 1, True),
                ("ASSERT_FAIL:ASYNC_FIFO.TIMEOUT", 1, False),
                ("ASSERT_FAIL:ASYNC_FIFO.BENCH_ARGUMENTS", 1, False),
                ("ASSERT_FAIL:ASYNC_FIFO.UNKNOWN", 1, False),
                ("ASSERT_FAIL:ASYNC_FIFO.CDC_ONE_BIT", 1, False)):
            with self.subTest(output=output, exit=code):
                engine = self.engine(f"print({output!r}); raise SystemExit({code})")
                observed = dict(work=self.work, engine=engine, runs=0,
                                generated_sha256="synthetic", rtl_sha256="synthetic",
                                tb_sha256="synthetic")
                rows = fifo.case_faults(DiagnosticDesigns(), random.Random(1))
                diagnostic = [row for row in rows if row.get("expected") == "report"]
                self.assertEqual(len(diagnostic), 1)
                self.assertEqual(diagnostic[0]["accepted"], accepted)
                self.assertTrue(all(row["accepted"] for row in rows
                                    if row.get("expected") != "report"))

    def test_qos_negative_controls_require_complete_observations(self):
        import qos_axi_bench
        import qos_pressure_bench
        import qos_scheduler_bench
        import qos_shaper_bench

        for module, marker, count, fields, args in (
                (qos_shaper_bench, "CHECK_PASS qos_shaper", 100001, 4, ()),
                (qos_scheduler_bench, "CHECK_PASS qos_scheduler 0 0 0", 100001, 10, ()),
                (qos_axi_bench, "CHECK_PASS qos_axi", 1001, 10, (False,))):
            with self.subTest(checker=module.__name__, condition="missing rows"):
                with self.assertRaisesRegex(RuntimeError, "incomplete simulation"):
                    module.check(marker, *args)
            with self.subTest(checker=module.__name__, condition="cycle gap"):
                rows = [f"C {cycle + (cycle > count // 2)}" + " 0" * fields
                        for cycle in range(count)]
                with self.assertRaisesRegex(RuntimeError, "nonconsecutive"):
                    module.check("\n".join([*rows, marker]), *args)
        for log, error in (
                ("", "PRESSURE_COMPLETE"),
                ("CHECK_PASS pressure", "PRESSURE_OBSERVER_EMPTY"),
                ("CHECK_PASS pressure\nRET 1 R 0 0 0 1 0", "PRESSURE_OBSERVER_EMPTY"),
                ("CHECK_PASS pressure\nPROBE 0 1 0\nPROBE 0 3 0", "PRESSURE_OBSERVER_GAP"),
                ("CHECK_PASS pressure\nPROBE 0 17 0\nRET 1 R 0 0 0 1 0",
                 "PRESSURE_OBSERVER_GAP")):
            with self.subTest(checker="pressure", log=log):
                with self.assertRaisesRegex(RuntimeError, error):
                    qos_pressure_bench.check(log, [dict(fields=[])])

    def test_pass_and_argv_with_spaces(self):
        engine = self.engine("import sys; print(sys.argv[1:]); print('MATRIX_PASS')")
        result = engine.run(["+ONLY=a b", "+COMPETE"])
        self.assertEqual(result.returncode, 0)
        receipt = json.loads((self.work / "sim-engine.json").read_text())
        self.assertEqual(receipt["steps"][-1]["argv"][-2:], ["+ONLY=a b", "+COMPETE"])
        self.assertEqual(receipt["steps"][-1]["cwd"], str(self.work))
        self.assertEqual(len(receipt["inputs"]), 2)
        self.assertEqual(receipt["status"], "PASS")


    def test_switch_marker_is_accepted(self):
        # A testbench that prints SWITCH_PASS and exits zero passes.
        engine = self.engine("print('SWITCH_PASS deliveries=22')")
        result = engine.run()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads((self.work / "sim-engine.json").read_text())["status"], "PASS")

    def test_every_marker_used_by_the_suite_is_registered(self):
        # Class guard: keep the single shared list complete instead of letting each
        # driver enumerate its own, which is how SWITCH_PASS went missing.
        import re
        pattern = re.compile(r'["\']([A-Z][A-Z0-9_]*_PASS)["\']')
        here = Path(__file__).resolve().parent
        found = {}
        for source in sorted([*here.glob("*.py"), *here.glob("verif/**/*.py")]):
            for marker in pattern.findall(source.read_text()):
                found.setdefault(marker, set()).add(source.name)
        for marker, consumers in NONTERMINAL_MARKERS.items():
            self.assertNotIn(marker, PASS_MARKERS)
            self.assertEqual(found.pop(marker, set()), consumers)
        missing = {m: sorted(v) for m, v in found.items() if m not in PASS_MARKERS}
        self.assertEqual(missing, {}, "unregistered success markers: %r" % (missing,))

    def test_nonterminal_markers_do_not_complete_simulations(self):
        for marker in NONTERMINAL_MARKERS:
            with self.subTest(marker=marker):
                result = self.engine("print(" + repr(marker) + ")").run()
                self.assertNotEqual(result.returncode, 0)

    def test_zero_exit_assertion_is_failure(self):
        for marker in ("ASSERT_FAIL:x", "CHECK_FAIL x", "Fatal: x", "Error: x", "%Error: x"):
            with self.subTest(marker=marker):
                self.assertNotEqual(self.engine("print(" + repr("MATRIX_PASS\n" + marker) + ")").run().returncode, 0)

    def test_no_success_marker_is_failure(self):
        self.assertNotEqual(self.engine("print('finished')").run().returncode, 0)

    def test_nonzero_with_success_is_failure(self):
        result = self.engine("print('MATRIX_PASS'); raise SystemExit(7)").run()
        self.assertEqual(result.returncode, 7)

    def test_extra_failure_pattern(self):
        result = self.engine("print('MATRIX_PASS engine_fault')", failure_patterns=["engine_fault"]).run()
        self.assertNotEqual(result.returncode, 0)

    def test_shared_parameter_adapter_with_no_overrides(self):
        engine = self.engine("print('MATRIX_PASS')",
                         compile=[sys.executable, "-c", "import sys; print(sys.argv[1:])", "{parameters}"],
                         parameter=["--set", "{top}.{name}={value}"])
        self.assertEqual(engine.run().returncode, 0)
        self.assertEqual((self.work / "compile.log").read_text().strip(), "[]")

    def test_compile_failure_is_not_mutation_evidence(self):
        with self.assertRaises(RuntimeError):
            self.engine("print('MATRIX_PASS')", compile=[sys.executable, "-c", "raise SystemExit(3)"])
        self.assertFalse((self.work / "simulation.log").exists())

    def test_timeout_preserves_output(self):
        engine = self.engine("import time; print('started', flush=True); time.sleep(30)", run_timeout=10)
        with self.assertRaises(subprocess.TimeoutExpired):
            engine.run()
        self.assertIn("started", (self.work / "simulation.log").read_text())
        receipt = json.loads((self.work / "sim-engine.json").read_text())
        self.assertEqual(receipt["steps"][-1]["error"], "TimeoutExpired")

    def test_invalid_config(self):
        for extra in (dict(version=2), dict(unknown=True), dict(run_timeout=0),
                      dict(run_timeout=float("inf")), dict(run=["tool"]),
                      dict(compile="tool"), dict(failure_patterns=[""])):
            with self.subTest(extra=extra), self.assertRaises((ValueError, TypeError)):
                self.engine("print('MATRIX_PASS')", **extra)

    def test_missing_external_config_never_falls_back(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            Simulation(self.work, "network.v", engine="external")

    def test_unknown_selection(self):
        with self.assertRaises(ValueError):
            selection("missing")

    def test_runner_arguments_and_log(self):
        runner = self.work / "runner"
        runner.write_text("#!" + sys.executable + "\n"
                          "import sys\nprint(repr(sys.argv[1:]))\nprint('CROSS_APB_PASS')\n")
        runner.chmod(0o755)
        with patch.dict(os.environ, {"NOCLOOM_SIM_RUNNER": str(runner)}, clear=True):
            result = simulate(self.work, self.work / "cache", ["+PA=12", "+ONLY=a b"])
        self.assertEqual(result, 0)
        log = (self.work / "simulation.log").read_text()
        self.assertIn(repr([str(self.work), "+PA=12", "+ONLY=a b"]), log)

    def test_cli_selection_overrides_environment(self):
        with patch.dict(os.environ, {"NOCLOOM_SIM": "external"}):
            self.assertEqual(selection("iverilog"), "iverilog")

    def test_conflicting_runner_selection(self):
        with patch.dict(os.environ, {"NOCLOOM_SIM_RUNNER": "unused", "NOCLOOM_SIM": "external"}), self.assertRaises(ValueError):
            simulate(self.work, self.work / "cache")


class ProcessContract(unittest.TestCase):
    def setUp(self):
        import sim_process
        self.process = sim_process
        self.tmp = tempfile.TemporaryDirectory(prefix="process engine ", dir=Path.cwd())
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        for name in ("tb.sv", "one.v", "two.v"):
            (self.work / name).write_text("// contract input\n")
        self.config = self.work / "adapter.json"
        self.cfg = dict(version=1,
                        compile=[sys.executable, "-c", "import sys; print(sys.argv[1:])", "{sources}"],
                        run=[sys.executable, "-c", "import sys; print(sys.argv[1:])", "{options}"])
        self.config.write_text(json.dumps(self.cfg))
        self.env = dict(os.environ, NOCLOOM_SIM="external", NOCLOOM_SIM_CONFIG=str(self.config))
        self.command = ["verilator", "--binary", "--timing", "--top-module", "tb",
                        "--build-jobs", "4", "-Wno-fatal", "-o", "sim", "tb.sv", "one.v", "two.v"]

    def compile(self):
        return self.process.run(self.command, cwd=self.work, env=self.env,
                                capture_output=True, text=True, check=True)

    def test_ordered_sources_and_runtime_options(self):
        result = self.compile()
        self.assertLess(result.stdout.index("one.v"), result.stdout.index("two.v"))
        binary = self.work / "obj_dir/sim"
        result = self.process.run([binary, "+ONLY=a b"], cwd=self.work, env=self.env,
                                  capture_output=True, text=True, check=True)
        self.assertIn("+ONLY=a b", result.stdout)
        event = json.loads((self.work / "simulation-process.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["engine"], "external")
        self.assertEqual(len(event["inputs"]), 3)
        self.assertEqual(event["argv"][-1], "+ONLY=a b")

    def test_icarus_callsite_uses_same_external_selection(self):
        self.command = ["iverilog", "-g2012", "-s", "tb", "-o", "sim", "tb.sv", "one.v"]
        self.compile()
        result = self.process.run(["vvp", "sim", "+X=1"], cwd=self.work, env=self.env,
                                  capture_output=True, text=True, check=True)
        self.assertIn("+X=1", result.stdout)

    def test_snapshot_launcher_survives_later_recompiles(self):
        # Compile-reuse aliases must carry
        # the engine record and a private build copy: the launcher alone is a
        # shell script, and a later compile rewrites the original record and
        # build directory.
        self.compile()
        binary = self.work / "obj_dir/sim"
        alias = self.work / "obj_dir/sim.baseline"
        self.assertTrue(self.process.snapshot_launcher(binary, alias))
        self.assertTrue((self.work / "obj_dir/sim.baseline.engine-build").is_dir())
        self.cfg["run"] = [sys.executable, "-c", "print('CLOBBERED')", "{options}"]
        self.config.write_text(json.dumps(self.cfg))
        self.compile()
        restored = self.process.run(["vvp", "obj_dir/sim.baseline"], cwd=self.work,
                                    env=self.env, capture_output=True, text=True, check=True)
        current = self.process.run([binary], cwd=self.work, env=self.env,
                                   capture_output=True, text=True, check=True)
        self.assertNotIn("CLOBBERED", restored.stdout)
        self.assertIn("CLOBBERED", current.stdout)
        record = json.loads((self.work / "obj_dir/sim.baseline.engine.json").read_text())
        self.assertEqual(record["logical"], str(alias))
        self.assertEqual(record["build"], str(self.work / "obj_dir/sim.baseline.engine-build"))

    def test_snapshot_launcher_copies_plain_binaries(self):
        plain = self.work / "plain-sim"
        plain.write_bytes(b"image")
        alias = self.work / "plain-sim.baseline"
        self.assertFalse(self.process.snapshot_launcher(plain, alias))
        self.assertEqual(alias.read_bytes(), b"image")
        self.assertFalse((self.work / "plain-sim.baseline.engine.json").exists())

    def test_compile_failure_removes_previous_launcher(self):
        self.compile()
        self.cfg["compile"] = [sys.executable, "-c", "raise SystemExit(7)"]
        self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(subprocess.CalledProcessError):
            self.compile()
        self.assertFalse((self.work / "obj_dir/sim").exists())

    def test_top_parameter_requires_explicit_adapter_support(self):
        with self.assertRaisesRegex(ValueError, "Top parameters require"):
            self.process.translate(self.command + ["-GDEPTH=4"], self.work, self.env)

    def test_top_parameter_preserved_in_compile_and_receipt(self):
        self.cfg["parameter"] = ["--set", "{top}.{name}={value}"]
        self.cfg["compile"].append("{parameters}")
        self.config.write_text(json.dumps(self.cfg))
        self.command += ["-GDEPTH=4", "-GSIGNED=-2"]
        result = self.compile()
        self.assertIn("tb.DEPTH=4", result.stdout)
        self.assertIn("tb.SIGNED=-2", result.stdout)
        record = json.loads((self.work / "obj_dir/sim.engine.json").read_text())
        self.assertEqual(record["parameters"], {"DEPTH": "4", "SIGNED": "-2"})

    def test_icarus_parameter_preserved_in_external_translation(self):
        self.cfg["parameter"] = ["{top}.{name}={value}"]
        self.cfg["compile"].append("{parameters}")
        self.config.write_text(json.dumps(self.cfg))
        self.command = ["iverilog", "-g2012", "-s", "tb", "-Ptb.DEPTH=2", "-o", "sim", "tb.sv", "one.v"]
        self.assertIn("tb.DEPTH=2", self.compile().stdout)

    def test_parameter_template_cannot_drop_value_or_name(self):
        self.cfg["compile"].append("{parameters}")
        for template in (["fixed"], ["{name}"], ["{value}"], "{name}={value}"):
            self.cfg["parameter"] = template
            self.config.write_text(json.dumps(self.cfg))
            with self.subTest(template=template), self.assertRaisesRegex(ValueError, "template must bind"):
                self.process.translate(self.command + ["-GDEPTH=4"], self.work, self.env)

    def test_invalid_and_duplicate_parameters_rejected(self):
        for options in (["-GDEPTH="], ["-Gbad.name=2"], ["-GDEPTH=2;exit"],
                        ["-GDEPTH=2", "-GDEPTH=4"]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.process.translate(self.command + options, self.work, self.env)

    def test_unknown_options_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported behavioral compile"):
            self.process.translate(self.command + ["--unknown-option"], self.work, self.env)

    def test_syntax_and_formal_commands_unchanged(self):
        for command in (["iverilog", "-g2001", "-tnull", "one.v"],
                        ["verilator", "--lint-only", "one.v"], ["sby", "task.sby"]):
            self.assertEqual(self.process.translate(command, self.work, self.env), command)

    @unittest.skipUnless((Path("/proc") / "self/stat").is_file(),
                         "Linux /proc required for descendant state observation")
    def test_timeout_cleans_descendants(self):
        self.cfg["run"] = [sys.executable, "-c",
            "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "print(p.pid,flush=True); time.sleep(30)", "{options}"]
        self.config.write_text(json.dumps(self.cfg))
        self.compile()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.process.run([self.work / "obj_dir/sim"], cwd=self.work, env=self.env,
                             capture_output=True, text=True, timeout=10)
        pid = int(caught.exception.output.strip())
        observed = wait_for_process_exit(pid)
        self.assertTrue(observed["exited"], observed)

    @unittest.skipUnless((Path("/proc") / "self/stat").is_file(),
                         "Linux /proc required for descendant state observation")
    def test_exit_wait_rejects_live_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            observed = wait_for_process_exit(process.pid, timeout=0.1)
            self.assertFalse(observed["exited"], observed)
            self.assertIsNone(process.poll())
        finally:
            process.kill()
            process.wait()

    def test_zero_exit_fatal_is_not_success(self):
        self.cfg["run"] = [sys.executable, "-c", "print('CHECK_FAIL required_control')", "{options}"]
        self.config.write_text(json.dumps(self.cfg))
        self.compile()
        result = self.process.run([self.work / "obj_dir/sim"], cwd=self.work, env=self.env,
                                  capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("CHECK_FAIL required_control", result.stdout)
        event = json.loads((self.work / "simulation-process.jsonl").read_text().splitlines()[-1])
        self.assertEqual(event["raw_exit_code"], 0)
        self.assertTrue(event["failure_marker"])

    def output_case(self, code, patterns=(), **environment):
        self.cfg["run"] = [sys.executable, "-c", code, "{options}"]
        self.cfg["failure_patterns"] = list(patterns)
        self.config.write_text(json.dumps(self.cfg))
        self.compile()
        result = self.process.run([self.work / "obj_dir/sim"], cwd=self.work,
                                  env=dict(self.env, **environment), capture_output=True, timeout=10)
        event = json.loads((self.work / "simulation-process.jsonl").read_text().splitlines()[-1])
        return result, event

    def test_failure_scan_does_not_invent_line_start(self):
        payload = b"a" * 63488 + b"error:" + b"b" * 2042
        code = ("import os,time; os.write(1,b'a'*63488+b'error:'+b'b'*2042); "
                "time.sleep(.03); os.write(1,b'\\nMATRIX_PASS\\n')")
        result, event = self.output_case(code)
        self.assertEqual(result.stdout, payload + b"\nMATRIX_PASS\n")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(event["failure_marker"])
        self.assertEqual(event["failure_scan"]["status"], "COMPLETE")

    def test_failure_scan_unbounded_pattern_without_final_newline(self):
        result, event = self.output_case(
            "import os; os.write(1,b'BEGIN'+b'a'*70000+b'FAIL')", [r"^BEGIN.*FAIL$"])
        self.assertEqual(result.stdout, b"BEGIN" + b"a" * 70000 + b"FAIL")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(event["raw_exit_code"], 0)
        self.assertTrue(event["failure_marker"])

    def test_failure_scan_preserves_split_utf8_and_invalid_bytes(self):
        result, event = self.output_case(
            "import os,time; os.write(1,b'\\xc3'); time.sleep(.03); "
            "os.write(1,b'\\xa9\\xff'); os.write(2,b'normal\\xff')", [r"^é\ufffd$"])
        self.assertEqual(result.stdout, b"\xc3\xa9\xff")
        self.assertEqual(result.stderr, b"normal\xff")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(event["failure_marker"])

    def test_failure_scan_cross_line_regex_and_split_marker(self):
        result, event = self.output_case(
            "import os,time; os.write(1,b'FIRST\\n'); time.sleep(.03); os.write(1,b'LAST')",
            [r"(?s)^FIRST.*LAST$"])
        self.assertEqual(result.returncode, 1)
        self.assertTrue(event["failure_marker"])
        result, event = self.output_case(
            "import os,time; os.write(2,b'CHECK_'); time.sleep(.03); os.write(2,b'FAIL: bad')")
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"CHECK_FAIL: bad")
        self.assertEqual(result.returncode, 1)

    def test_failure_scan_keeps_streams_separate_and_raw_exit(self):
        result, event = self.output_case(
            "import os; os.write(1,b'BEGIN'); os.write(2,b'FAIL')", [r"(?s)BEGIN.*FAIL"])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(event["failure_marker"])
        result, event = self.output_case("import sys; print('normal'); sys.exit(7)")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(event["raw_exit_code"], 7)
        self.assertFalse(event["failure_marker"])

    def test_failure_scan_size_budget_is_explicit_and_recoverable(self):
        result, event = self.output_case("print('normal output')", NOCLOOM_FAILURE_SCAN_MAX_BYTES="4")
        self.assertEqual(result.stdout, b"normal output\n")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(event["raw_exit_code"], 0)
        self.assertFalse(event["failure_marker"])
        self.assertEqual(event["infrastructure_error"], "FAILURE_SCAN_INCOMPLETE")
        self.assertEqual(event["failure_scan"]["reason"], "SCAN_SIZE_LIMIT")
        self.assertEqual((Path(event["failure_scan"]["spool"]) / "stdout.bin").read_bytes(), result.stdout)
        result, event = self.output_case("print('normal output')", NOCLOOM_FAILURE_SCAN_MAX_BYTES="100")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(event["failure_scan"]["spool_retained"])
        self.assertFalse(Path(event["failure_scan"]["spool"]).exists())

    @unittest.skipUnless(os.name == "posix", "POSIX signal return codes required")
    def test_failure_scan_preserves_signal_exit(self):
        import signal
        for value in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=value):
                result, event = self.output_case(f"import os; os.kill(os.getpid(),{value})")
                self.assertEqual(result.returncode, -value)
                self.assertEqual(event["raw_exit_code"], -value)
                self.assertEqual(event["exit_code"], -value)

    def test_failure_scan_regex_budget_is_explicit(self):
        result, event = self.output_case("print('a'*10000+'!')", [r"^(a+)+$"],
                                         NOCLOOM_FAILURE_SCAN_TIMEOUT="0.1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(event["raw_exit_code"], 0)
        self.assertEqual(event["failure_scan"]["reason"], "SCAN_TIMEOUT")
        self.assertEqual(event["infrastructure_error"], "FAILURE_SCAN_INCOMPLETE")
        self.assertLess(event["failure_scan"]["elapsed_seconds"], 3)

    def test_missing_tool_exception_is_preserved_for_caller(self):
        self.cfg["compile"] = ["missing-simulation-compiler", "{sources}"]
        self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(FileNotFoundError):
            self.compile()

    def test_no_external_configuration_fallback(self):
        with self.assertRaises(ValueError):
            self.process.translate(self.command, self.work, {"NOCLOOM_SIM": "external"})


if __name__ == "__main__":
    unittest.main()
