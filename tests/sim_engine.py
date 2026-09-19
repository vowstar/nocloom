"""Explicit simulation engines; an external engine takes its commands from an adapter file."""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess

import sim_metrics


FAILURE = r"ASSERT_FAIL:|CHECK_FAIL(?:\s|:)|(?im:^\s*(?:fatal|error):)|%Error"


def add_arguments(parser):
    parser.add_argument("--sim", choices=("verilator", "iverilog", "external"),
                        help="Simulation engine (otherwise NOCLOOM_SIM or iverilog)")
    parser.add_argument("--sim-config", help="External adapter JSON")


# Every success marker the suite's testbenches print. Callers must not keep their
# own subsets: a marker missing here turns a passing case into a failure.
# All emitted completion markers must be registered here.
PASS_MARKERS = (
    "ADDRESS_PASS", "ARBITER_PASS", "AXI4_INITIATOR_PASS", "AXI4_LOOPBACK_PASS", "AXI4_TARGET_PASS",
    "AXI_NETWORK_PASS", "BASELINE_PASS",
    "CAPACITY_PASS", "CHECK_PASS", "CROSS_APB_MATRIX_PASS", "CROSS_APB_PASS",
    "CROSS_AXI_MATRIX_PASS", "CROSS_AXI_PASS", "CROSS_DOMAIN_CONFIG_PASS", "CROSS_MULTI_PASS",
    "CROSS_RESET_FAMILY_PASS", "CROSS_RESET_PASS", "CROSS_SHARED_SANITY_PASS", "CROSS_STARTUP_PASS",
    "CROSS_WIDE_APB_PASS", "CROSS_WIDE_AXI64_PASS", "CROSS_WIDTH_PASS",
    "CYCLE_PASS", "EXCHANGE_PASS", "EXPANDED_CHECKS_PASS", "EXPANDED_CLI_PASS", "EXPANDED_COST_PASS",
    "EXPANDED_FORMAL_SMOKE_PASS", "FABRIC_PASS", "HELD_BRIDGE_PASS", "HELD_PASS",
    "INSTALLED_NETWORK_PASS", "LARGE_INSTALLED_PASS", "LARGE_NETWORK_PASS", "APB_ARBITRATION_PASS",
    "AXI_LITE_BRIDGE_PASS", "MAILBOX_CDC_PASS", "MATRIX_PASS", "MESH_AXI_NETWORK_CHECK_PASS",
    "MESH_AXI_NETWORK_PASS", "MESH_COMPONENT_PASS", "MESH_MEASUREMENT_CONTROLS_PASS", "MESH_NETWORK_CHECK_PASS",
    "MESH_NETWORK_PASS", "NOCLOOM_PASS", "NODE_CASE_PASS", "NODE_MUTATIONS_PASS",
    "NODE_PASS", "OBSERVER_CONTROLS_PASS", "OWNER_RESET_PASS", "PACKET_SWITCH_PASS", "PROGRESS_PASS",
    "QOS_OUTSTANDING_PASS", "QUEUE_CHAIN_PASS", "QUEUE_CONTROLS_PASS", "QUEUE_PASS", "RELATED_CLOCK_PASS",
    "RESET_PASS",
    "RESET_STALL_PASS", "SATURATION_PASS", "SCALE_CHECKS_PASS", "SCALE_CLI_PASS", "SCALE_FORMAL_SMOKE_PASS",
    "SCALE_NETWORK_PASS", "STRESS_PASS", "STRUCTURE_PASS", "SWITCH_PASS", "WIDTH_ADAPTER_PASS",
    "WIDE_APB_PASS", "WIDE_AXI64_PASS", "WIDE_ENDPOINTS_PASS",
    "AXI4_EXCHANGE_PASS", "ASYNC_FIFO_PASS",
)


def generated_rtl(path):
    """A generated module's text followed by the cell models generated beside it, if any (the
    module instantiates them), so that one source file simulates the module."""
    path = Path(path)
    cells = [path.parent / f"nocloom_{c}.v" for c in ("clock_gate", "synchronizer")]
    return path.read_text() + "".join(c.read_text() for c in cells if c.is_file() and c != path)


def selection(engine=None):
    value = engine or os.environ.get("NOCLOOM_SIM", "iverilog")
    if value not in ("verilator", "iverilog", "external"):
        raise ValueError("Unknown simulation engine: " + value)
    return value


def verilator_command(rtl="network.v", tb="tb.sv"):
    return ["verilator", "--binary", "--timing", "--top-module", "tb",
            "-Wno-fatal", "--build-jobs", "8", "--output-split", "10000",
            "-o", "sim", str(tb), str(rtl)]


def probe_version(config):
    """First stdout line of an adapter's version_probe; None when undeclared."""
    probe = config.get("version_probe")
    if probe is None:
        return None
    if (not isinstance(probe, list) or not probe
            or not all(isinstance(arg, str) and arg for arg in probe)):
        raise ValueError("version_probe must be a nonempty argv array")
    result = subprocess.run(probe, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("version_probe failed: " + result.stderr[-500:])
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise RuntimeError("version_probe printed nothing")
    return lines[0]


class Simulation:
    """Compile then run with receipts; callers retain their behavioral checkers."""

    def __init__(self, work, rtl, *, engine=None, config=None,
                 compile_argv=None, binary=None, compile_work=None,
                 compile_timeout=120, run_timeout=40, success=PASS_MARKERS,
                 reuse=False, runner=None):
        self.work = Path(work).resolve()
        self.build = Path(compile_work or work).resolve()
        self.build.mkdir(parents=True, exist_ok=True)
        self.engine = "runner" if runner else selection(engine)
        if config and self.engine != "external":
            raise ValueError("--sim-config requires the external engine")
        self.run_timeout = run_timeout
        self.success = success
        self.extra_failure = []
        origin = ("runner" if runner else "argument" if engine
                  else "NOCLOOM_SIM" if "NOCLOOM_SIM" in os.environ else "default")
        self.record = dict(engine=self.engine, selection=origin,
                           inputs={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in (self.work / rtl, self.work / "tb.sv")},
                           steps=[])
        values = dict(work=str(self.work), build=str(self.build),
                      rtl=str(self.work / rtl), tb=str(self.work / "tb.sv"), top="tb")
        self.values = values
        if runner:
            self.command = [runner, str(self.work)]
        elif self.engine == "external":
            path = config or os.environ.get("NOCLOOM_SIM_CONFIG")
            if not path:
                raise ValueError("external requires --sim-config or NOCLOOM_SIM_CONFIG")
            raw = Path(path).read_bytes()
            cfg = json.loads(raw)
            allowed = {"version", "compile", "run", "compile_timeout", "run_timeout",
                       "failure_patterns", "parameter", "version_probe"}
            if (not isinstance(cfg, dict) or set(cfg) - allowed
                    or type(cfg.get("version")) is not int or cfg["version"] != 1):
                raise ValueError("Unsupported external adapter configuration")
            self.record["config_sha256"] = hashlib.sha256(raw).hexdigest()
            # This entry has no parameter overrides; the translator owns them.
            compile_argv = self.expand(cfg["compile"], ())
            self.command = cfg["run"]
            self.run_timeout = self.timeout(cfg.get("run_timeout", run_timeout))
            compile_timeout = self.timeout(cfg.get("compile_timeout", compile_timeout))
            self.extra_failure = cfg.get("failure_patterns", [])
            if not isinstance(self.extra_failure, list) or not all(
                    isinstance(p, str) and p for p in self.extra_failure):
                raise ValueError("failure_patterns must contain nonempty regex strings")
            for pattern in self.extra_failure:
                re.compile(pattern)
            probe = cfg.get("version_probe")
            if probe is not None and (not isinstance(probe, list) or not probe
                                      or not all(isinstance(arg, str) and arg for arg in probe)):
                raise ValueError("version_probe must be a nonempty argv array")
            if not isinstance(self.command, list) or self.command.count("{options}") != 1:
                raise ValueError("run must contain exactly one standalone {options}")
            self.expand(self.command, ())
        elif self.engine == "iverilog":
            compile_argv = ["iverilog", "-g2012", "-s", "tb", "-o",
                            str(self.build / "sim.vvp"), values["tb"], values["rtl"]]
            self.command = ["vvp", str(self.build / "sim.vvp")]
        else:
            compile_argv = compile_argv or verilator_command(rtl)
            self.command = [str(binary or self.build / "obj_dir/sim")]
        if not runner:
            if reuse and self.engine == "verilator":
                self.record["compile_reused"] = True
            else:
                result = self.execute(compile_argv, self.build, "compile.log", compile_timeout)
                if result.returncode:
                    raise RuntimeError("Simulation compile failed; see " + str(self.build / "compile.log"))
        self.save()

    @staticmethod
    def timeout(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < float("inf"):
            raise ValueError("Timeout must be a finite positive number")
        return value

    def expand(self, template, options):
        if not isinstance(template, list) or not template or not all(isinstance(x, str) and x for x in template):
            raise ValueError("Commands must be nonempty argv arrays")
        result = []
        for arg in template:
            if arg == "{sources}":
                result.extend([self.values["tb"], self.values["rtl"]])
            elif arg == "{parameters}":
                continue
            elif arg == "{options}":
                result.extend(options)
            else:
                result.append(arg.format_map(self.values))
        return result

    def save(self):
        (self.work / "sim-engine.json").write_text(json.dumps(self.record, indent=2) + "\n")

    def execute(self, argv, cwd, log_name, timeout):
        argv = list(map(str, argv))
        step = dict(argv=argv, cwd=str(cwd), timeout=timeout, log=str(cwd / log_name))
        self.record["steps"].append(step)
        self.save()
        ticket = sim_metrics.prepare(argv, cwd, "compile" if log_name == "compile.log" else "simulation")
        ticket["timeout_seconds"] = timeout
        step["metrics_path"] = str(ticket["path"])
        try:
            with (cwd / log_name).open("w") as log:
                process = subprocess.Popen(ticket["command"], cwd=cwd, stdout=log,
                                           stderr=subprocess.STDOUT,
                                           start_new_session=(os.name == "posix"))
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.wait()
                    raise
                result = subprocess.CompletedProcess(argv, process.returncode)
            step["exit_code"] = result.returncode
            step["metrics"] = sim_metrics.finish(ticket, result.returncode)
        except (subprocess.TimeoutExpired, OSError) as error:
            step["error"] = type(error).__name__
            step["metrics"] = sim_metrics.finish(ticket, error=type(error).__name__)
            if (cwd / log_name).exists():
                step["log_sha256"] = hashlib.sha256((cwd / log_name).read_bytes()).hexdigest()
            self.save()
            raise
        text = (cwd / log_name).read_text(errors="replace")
        step["log_sha256"] = hashlib.sha256((cwd / log_name).read_bytes()).hexdigest()
        self.save()
        return subprocess.CompletedProcess(argv, result.returncode, text, "")

    def run(self, options=()):
        argv = (self.expand(self.command, options) if self.engine == "external"
                else [*self.command, *options])
        result = self.execute(argv, self.work, "simulation.log", self.run_timeout)
        failure = bool(re.search(FAILURE, result.stdout) or any(
            re.search(p, result.stdout) for p in self.extra_failure))
        reached = [marker for marker in self.success if marker in result.stdout]
        self.record.update(raw_exit_code=result.returncode, failure_marker=failure,
                           success_markers=reached,
                           status="PASS" if result.returncode == 0 and reached and not failure else "FAIL")
        self.save()
        # Some engines return zero after a fatal assertion. Never call that PASS.
        if self.record["status"] != "PASS" and result.returncode == 0:
            result.returncode = 1
        return result
