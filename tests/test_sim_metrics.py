"""Resource instrumentation contracts using Python children only."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sim_metrics
import sim_process
from sim_engine import Simulation


class Metrics(unittest.TestCase):
    def setUp(self):
        # The suite wrapper injects NOCLOOM_SIM_METRICS_DIR per CTest entry;
        # these contracts must observe their own work directory instead.
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NOCLOOM_SIM_METRICS_DIR", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        (self.work / "network.v").write_text("unused")
        (self.work / "tb.sv").write_text("unused")
        self.config = self.work / "config.json"
        self.config.write_text(json.dumps(dict(version=1,
            compile=[sys.executable, "-c", "print('compiled')"],
            run=[sys.executable, "-c", "print('CHECK_PASS')", "{options}"])))

    def records(self):
        return [json.loads(p.read_text()) for p in (self.work / "simulation-metrics").glob("*.json")]

    def test_engine_both_stages_and_output(self):
        engine = Simulation(self.work, "network.v", engine="external", config=self.config)
        self.assertEqual(engine.run().stdout.strip(), "CHECK_PASS")
        self.assertEqual({r['stage'] for r in self.records()}, {'compile', 'simulation'})
        for r in self.records():
            self.assertEqual(r['resource_status'], 'MEASURED_REAPED_CHILDREN')
            self.assertGreater(r['elapsed_seconds'], 0)
            self.assertGreater(r['max_rss_bytes'], 0)
            self.assertIsNone(r['tree_rss_peak_bytes'])

    def test_process_stages_real_argv_and_case(self):
        target = self.work / 'central'
        with patch.dict(os.environ, NOCLOOM_SIM='external', NOCLOOM_SIM_CONFIG=str(self.config),
                        NOCLOOM_SIM_METRICS_DIR=str(target), NOCLOOM_SIM_CASE='contract case'):
            r = sim_process.run(['verilator', '--binary', '--top-module', 'tb', '-o', 'sim', 'tb.sv', 'network.v'], cwd=self.work, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), 'compiled')
            r = sim_process.run([self.work / 'obj_dir/sim'], cwd=self.work, capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), 'CHECK_PASS')
        rows = [json.loads(p.read_text()) for p in target.glob('*.json')]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['stage'] for r in rows}, {'compile', 'simulation'})
        for r in rows:
            self.assertEqual(r['case'], 'contract case')
            self.assertEqual(r['argv'][:2], [sys.executable, '-c'])
            self.assertIn('invocation_argv', r)

    def test_no_history_peak_leak(self):
        rows = []
        for size in [96 * 1024 * 1024, 1]:
            ticket = sim_metrics.prepare([sys.executable, '-c', 'a=bytearray(%d)' % size], self.work, 'simulation')
            r = subprocess.run(ticket['command'], check=True)
            rows.append(sim_metrics.finish(ticket, r.returncode))
        self.assertGreater(rows[0]['max_rss_bytes'], rows[1]['max_rss_bytes'] + 64 * 1024 * 1024)

    def test_failure_keeps_exit(self):
        ticket = sim_metrics.prepare([sys.executable, '-c', 'raise SystemExit(7)'], self.work, 'compile')
        r = subprocess.run(ticket['command'])
        self.assertEqual(r.returncode, 7)
        self.assertEqual(sim_metrics.finish(ticket, r.returncode)['status'], 'EXIT_NONZERO')

    def test_signal_exit_is_preserved(self):
        if os.name != 'posix':
            self.skipTest('POSIX signal contract')
        ticket = sim_metrics.prepare([sys.executable, '-c', 'import os,signal;os.kill(os.getpid(),signal.SIGKILL)'], self.work, 'simulation')
        r = subprocess.run(ticket['command'])
        self.assertEqual(r.returncode, -9)
        self.assertEqual(sim_metrics.finish(ticket, r.returncode)['exit_code'], -9)

    def test_not_started_has_no_zero_memory_claim(self):
        ticket = sim_metrics.prepare([str(self.work/'missing-executable')], self.work, 'compile')
        r = subprocess.run(ticket['command'])
        record = sim_metrics.finish(ticket, r.returncode)
        self.assertEqual(record['status'], 'EXECUTION_ERROR')
        self.assertIsNone(record['max_rss_bytes'])
        self.assertIsNone(record['user_seconds'])

    def test_timeout_is_partial(self):
        cfg = json.loads(self.config.read_text())
        cfg['run'] = [sys.executable, '-c', "import time; print('started',flush=True); time.sleep(10)", '{options}']
        self.config.write_text(json.dumps(cfg))
        engine = Simulation(self.work, 'network.v', engine='external', config=self.config, run_timeout=.3)
        with self.assertRaises(subprocess.TimeoutExpired):
            engine.run()
        r = next(r for r in self.records() if r['stage'] == 'simulation')
        self.assertEqual(r['status'], 'TIMEOUT')
        self.assertIsNone(r['max_rss_bytes'])
        self.assertIsNone(r['user_seconds'])
        self.assertGreaterEqual(r['elapsed_seconds'], .3)
        self.assertIn('started', (self.work/'simulation.log').read_text())

    def test_internal_adapter_timeout_is_visible(self):
        cfg = json.loads(self.config.read_text())
        cfg['run'] = [sys.executable, '-c', 'import time;time.sleep(10)', '{options}']
        cfg['run_timeout'] = .1
        self.config.write_text(json.dumps(cfg))
        with patch.dict(os.environ, NOCLOOM_SIM='external', NOCLOOM_SIM_CONFIG=str(self.config)):
            sim_process.run(['verilator', '--binary', '--top-module', 'tb', '-o', 'sim', 'tb.sv', 'network.v'], cwd=self.work, check=True, capture_output=True)
            result = sim_process.run([self.work/'obj_dir/sim'], cwd=self.work, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        row = next(r for r in self.records() if r['stage'] == 'simulation')
        self.assertEqual(row['status'], 'TIMEOUT')
        self.assertEqual(row['interruption'], 'TimeoutExpired')
        self.assertEqual(row['timeout_seconds'], .1)
        self.assertIsNone(row['max_rss_bytes'])

    def test_process_timeout_partial(self):
        cfg = json.loads(self.config.read_text())
        cfg['run'] = [sys.executable, '-c', 'import time;time.sleep(10)', '{options}']
        self.config.write_text(json.dumps(cfg))
        with patch.dict(os.environ, NOCLOOM_SIM='external', NOCLOOM_SIM_CONFIG=str(self.config)):
            sim_process.run(['verilator', '--binary', '--top-module', 'tb', '-o', 'sim', 'tb.sv', 'network.v'], cwd=self.work, check=True, capture_output=True)
            with self.assertRaises(subprocess.TimeoutExpired):
                sim_process.run([self.work/'obj_dir/sim'], cwd=self.work, capture_output=True, timeout=.3)
        r = next(r for r in self.records() if r['stage'] == 'simulation')
        self.assertEqual(r['status'], 'TIMEOUT')
        self.assertIsNone(r['max_rss_bytes'])


if __name__ == '__main__':
    unittest.main()
