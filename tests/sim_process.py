"""Route direct compile/run call sites through an explicitly selected HDL engine.

Non-simulation commands, including syntax and formal checks, pass through unchanged.
The drivers own their behavioral assertions and mutation verdicts.
"""

import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time

from sim_engine import FAILURE, Simulation, selection
import sim_metrics


def _path(value, cwd):
    p = Path(value)
    return p.resolve() if p.is_absolute() else (cwd / p).resolve()


def _expand(template, values, sources, options, parameters=()):
    if not isinstance(template, list) or not template or not all(isinstance(x, str) and x for x in template):
        raise ValueError("Commands must be nonempty argv arrays")
    result = []
    for arg in template:
        if arg == "{sources}":
            result.extend(sources)
        elif arg == "{options}":
            result.extend(options)
        elif arg == "{parameters}":
            result.extend(parameters)
        else:
            result.append(arg.format_map(values))
    return result


def _parameter(text, top, parameters):
    name, separator, value = text.partition("=")
    if name.startswith(top + "."):
        name = name[len(top) + 1:]
    integer = r"[+-]?(?:[0-9][0-9_]*|(?:[0-9]+)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+)"
    if (not separator or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_$]*", name)
            or not re.fullmatch(integer, value)):
        raise ValueError("Unsupported integral top parameter: " + text)
    if name in parameters:
        raise ValueError("Duplicate top parameter: " + name)
    parameters[name] = value


def _external_parameters(config, parameters, values):
    template = config.get("parameter")
    compile_template = config["compile"]
    if not isinstance(compile_template, list):
        raise ValueError("Commands must be nonempty argv arrays")
    count = compile_template.count("{parameters}")
    if template is not None:
        if (not isinstance(template, list) or not template
                or not all(isinstance(arg, str) and arg for arg in template)
                or not any("{name}" in arg for arg in template)
                or not any("{value}" in arg for arg in template)):
            raise ValueError("parameter template must bind name and value")
    if count > 1 or (parameters and (count != 1 or template is None)):
        raise ValueError("Top parameters require parameter template and one {parameters}")
    result = []
    for name, value in parameters.items():
        result.extend(_expand(template, dict(values, name=name, value=value), (), ()))
    return result


def translate(command, cwd=None, env=None):
    if isinstance(command, (str, bytes)):
        return command
    argv = list(map(os.fspath, command))
    if not argv:
        return argv
    work = Path(cwd or Path.cwd()).resolve()
    environment = os.environ if env is None else env
    engine = selection(environment.get("NOCLOOM_SIM", "iverilog"))
    tool = Path(argv[0]).name
    if tool == "vvp" and len(argv) > 1:
        record = Path(str(_path(argv[1], work)) + ".engine.json")
        if record.exists():
            return [sys.executable, str(Path(__file__).resolve()), "execute", str(record), *argv[2:]]
    executable_record = Path(str(_path(argv[0], work)) + ".engine.json")
    if executable_record.is_file():
        return [sys.executable, str(Path(__file__).resolve()), "execute", str(executable_record), *argv[1:]]
    verilator = tool == "verilator" and "--binary" in argv
    icarus = tool == "iverilog" and "-s" in argv and argv[argv.index("-s") + 1] == "tb"
    if not (verilator or icarus) or engine == "verilator":
        return argv
    sources, top, output = [], "tb", "sim"
    parameters = {}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("--top-module", "-s", "-o", "--build-jobs", "--output-split", "-CFLAGS"):
            if i + 1 == len(argv):
                raise ValueError("Missing compile option value: " + arg)
            if arg in ("--top-module", "-s"):
                top = argv[i + 1]
            if arg == "-o":
                output = argv[i + 1]
            i += 2
            continue
        if arg in ("--binary", "--timing", "-Wno-fatal", "-g2012"):
            i += 1
            continue
        if (verilator and arg.startswith("-G")) or (icarus and arg.startswith("-P")):
            _parameter(arg[2:], top, parameters)
            i += 1
            continue
        if arg.startswith("-") or Path(arg).suffix not in (".v", ".sv"):
            raise ValueError("Unsupported behavioral compile argument: " + arg)
        sources.append(str(_path(arg, work)))
        i += 1
    if not sources or top != "tb":
        raise ValueError("A behavioral compile requires explicit sources and top tb")
    logical = Path(output)
    if not logical.is_absolute():
        logical = (work / "obj_dir" if verilator else work) / logical
    assert not logical.is_symlink()
    assert logical.resolve() != work and logical.resolve().is_relative_to(work)
    logical = logical.resolve()
    logical.parent.mkdir(parents=True, exist_ok=True)
    build = logical.parent / (logical.name + ".engine-build")
    build.mkdir(exist_ok=True)
    record = Path(str(logical) + ".engine.json")
    if logical.exists():
        assert logical.resolve() != work and logical.resolve().is_relative_to(work)
        logical.unlink()
    inputs = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}
    request = dict(engine=engine, work=str(work), build=str(build), logical=str(logical),
                   sources=sources, top=top, parameters=parameters, inputs=inputs, original_compile=argv)
    if engine == "external":
        config = environment.get("NOCLOOM_SIM_CONFIG")
        if not config:
            raise ValueError("external requires NOCLOOM_SIM_CONFIG")
        raw = Path(config).read_bytes()
        request.update(config=json.loads(raw), config_sha256=hashlib.sha256(raw).hexdigest())
        _external_parameters(request["config"], parameters, dict(top=top))
    compiler = request["config"]["compile"][0] if engine == "external" else "iverilog"
    if shutil.which(compiler, path=environment.get("PATH", os.defpath)) is None:
        raise FileNotFoundError(2, "Simulation compiler not found", compiler)
    record.write_text(json.dumps(request, indent=2) + "\n")
    return [sys.executable, str(Path(__file__).resolve()), "compile", str(record)]


def snapshot_launcher(logical, alias):
    """Snapshot a routed compile product under an alias name in the same directory.

    On routed engines the compile product is a launcher script; the engine record
    and build directory carry the real image. Copying the launcher alone leaves
    `vvp alias` reading the script, and sharing the original record would run
    whatever a later compile wrote into the original build directory. The alias
    gets its own record and a private copy of the build directory. Returns True
    when a engine record was snapshotted, False for a plain binary copy.
    """
    logical = Path(logical)
    alias = Path(alias)
    record = Path(str(logical) + ".engine.json")
    if not record.is_file():
        shutil.copyfile(logical, alias)
        return False
    request = json.loads(record.read_text())
    alias_build = alias.parent / (alias.name + ".engine-build")
    if alias_build.exists():
        work = Path(request["work"]).resolve()
        assert not alias_build.is_symlink()
        assert alias_build.resolve() != work and alias_build.resolve().is_relative_to(work)
        shutil.rmtree(alias_build)
    shutil.copytree(request["build"], alias_build)
    alias_record = Path(str(alias) + ".engine.json")
    request.update(logical=str(alias), build=str(alias_build))
    alias_record.write_text(json.dumps(request, indent=2) + "\n")
    invocation = [sys.executable, str(Path(__file__).resolve()), "execute", str(alias_record)]
    alias.write_text("#!/bin/sh\nexec " + shlex.join(invocation) + ' "$@"\n')
    alias.chmod(0o755)
    return True


class Popen(subprocess.Popen):
    """Own the process group for routed commands, including timeout cleanup."""

    def __init__(self, command, *args, **kwargs):
        translated = translate(command, kwargs.get("cwd"), kwargs.get("env"))
        self.engine_group = (os.name == "posix" and isinstance(translated, list)
                           and len(translated) > 2 and translated[1] == str(Path(__file__).resolve()))
        self.metrics = None
        self.metrics_error = None
        stage = None
        if self.engine_group:
            stage = "compile" if translated[2] == "compile" else "simulation"
        elif isinstance(translated, list) and translated:
            tool = Path(translated[0]).name
            if (tool == "iverilog" and "-s" in translated and "tb" in translated) or (tool == "verilator" and "--binary" in translated):
                stage = "compile"
            elif tool == "vvp" or Path(translated[0]).parent.name == "obj_dir":
                stage = "simulation"
        if stage:
            self.metrics = sim_metrics.prepare(translated, kwargs.get("cwd"), stage, kwargs.get("env"))
            translated = self.metrics["command"]
            self.engine_group = os.name == "posix"
        if self.engine_group:
            kwargs["start_new_session"] = True
        try:
            super().__init__(translated, *args, **kwargs)
        except OSError as error:
            if self.metrics:
                sim_metrics.finish(self.metrics, error=type(error).__name__)
            raise

    def wait(self, timeout=None):
        try:
            result = super().wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.metrics_error = "TimeoutExpired"
            if self.metrics:
                self.metrics["timeout_seconds"] = timeout
            raise
        if self.metrics:
            sim_metrics.finish(self.metrics, result, self.metrics_error)
        return result

    def communicate(self, input=None, timeout=None):
        try:
            return super().communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.metrics_error = "TimeoutExpired"
            if self.metrics:
                self.metrics["timeout_seconds"] = timeout
            raise

    def send_signal(self, sig):
        if self.engine_group:
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                pass
        else:
            super().send_signal(sig)


def run(command, *args, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with Popen(command, *args, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            error.output, error.stderr = process.communicate()
            raise
        except BaseException:
            process.kill()
            process.wait()
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def scan_output(record, spool, limit):
    """Scan whole Unicode streams in a short-lived, time-bounded subprocess."""
    request = json.loads(Path(record).read_text())
    patterns = [re.compile(FAILURE)] + [re.compile(p) for p in request.get("config", {}).get("failure_patterns", [])]
    streams = []
    for name in ("stdout.bin", "stderr.bin"):
        path = Path(spool) / name
        size = path.stat().st_size
        if size > limit:
            streams.append(dict(stream=name, bytes=size, status="LIMIT_EXCEEDED", matched=False))
            continue
        # Each reader is closed before scanning. Limit the read anyway, so an
        # unexpected file change cannot bypass the memory budget.
        with path.open("rb") as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            streams.append(dict(stream=name, bytes=len(data), status="LIMIT_EXCEEDED", matched=False))
            continue
        text = data.decode("utf-8", errors="replace")
        del data
        matched = any(pattern.search(text) for pattern in patterns)
        del text
        streams.append(dict(stream=name, bytes=size, status="COMPLETE", matched=matched))
    complete = all(row["status"] == "COMPLETE" for row in streams)
    print(json.dumps(dict(status="COMPLETE" if complete else "INCOMPLETE",
                          reason=None if complete else "SCAN_SIZE_LIMIT",
                          matched=any(row["matched"] for row in streams), streams=streams)))


def main():
    stage, record = sys.argv[1], Path(sys.argv[2])
    request = json.loads(record.read_text())
    build, logical = Path(request["build"]), Path(request["logical"])
    sources = request["sources"]
    tb = [p for p in sources if Path(p).name == "tb.sv"]
    rtl = [p for p in sources if p not in tb]
    values = dict(work=request["work"], build=str(build), top=request["top"],
                  tb=tb[0] if len(tb) == 1 else "", rtl=rtl[0] if len(rtl) == 1 else "")
    if request["engine"] == "iverilog":
        argv = (["iverilog", "-g2012", "-s", request["top"], "-o", str(build / "sim.vvp"),
                 *("-P" + request["top"] + "." + name + "=" + value
                   for name, value in request.get("parameters", {}).items()), *sources]
                if stage == "compile" else ["vvp", str(build / "sim.vvp"), *sys.argv[3:]])
    else:
        config = request["config"]
        allowed = {"version", "compile", "run", "compile_timeout", "run_timeout", "failure_patterns", "parameter", "version_probe"}
        if not isinstance(config, dict) or set(config) - allowed or type(config.get("version")) is not int or config["version"] != 1:
            raise ValueError("Unsupported adapter version")
        template = config["compile" if stage == "compile" else "run"]
        if stage == "compile" and len(rtl) != 1 and any("{rtl}" in x for x in template):
            raise ValueError("Multiple RTL files require standalone {sources}")
        if stage == "execute" and template.count("{options}") != 1:
            raise ValueError("run must contain exactly one standalone {options}")
        parameters = (_external_parameters(config, request.get("parameters", {}), values)
                      if stage == "compile" else ())
        argv = _expand(template, values, sources, sys.argv[3:], parameters)
    if stage == "compile" and logical.exists():
        work = Path(request["work"]).resolve()
        assert not logical.is_symlink()
        assert logical.resolve() != work and logical.resolve().is_relative_to(work)
        logical.unlink()  # Never leave a stale executable after a failed compile.
    cwd = str(build) if stage == "compile" else str(Path.cwd())
    timeout = None
    if request["engine"] == "external":
        budget = config.get("compile_timeout" if stage == "compile" else "run_timeout")
        timeout = Simulation.timeout(budget) if budget is not None else None
    sim_metrics.actual_argv(argv)
    failure_seen = []
    failure_scan = None
    try:
        if stage == "compile":
            completed = subprocess.run(argv, cwd=cwd, timeout=timeout)
        else:
            # Preserve byte streams separately. Arbitrary adapter regexes require
            # complete strings, not artificial line boundaries from chunk overlap.
            limit = int(os.environ.get("NOCLOOM_FAILURE_SCAN_MAX_BYTES", 256 * 1024 * 1024))
            scan_timeout = Simulation.timeout(float(os.environ.get("NOCLOOM_FAILURE_SCAN_TIMEOUT", "30")))
            if limit <= 0:
                raise ValueError("NOCLOOM_FAILURE_SCAN_MAX_BYTES must be positive")
            spool = Path(tempfile.mkdtemp(prefix="simulation-scan-", dir=cwd))
            stream_errors = []
            def forward(pipe, destination, path):
                try:
                    with path.open("wb") as log:
                        while data := pipe.read1(65536):
                            destination.write(data)
                            destination.flush()
                            log.write(data)
                except OSError as error:
                    stream_errors.append(str(error))
                    # Keep draining so a failed log sink cannot deadlock the child.
                    while pipe.read1(65536):
                        pass
                finally:
                    pipe.close()
            with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                readers = [threading.Thread(target=forward, args=(pipe, dest, spool / name), daemon=True)
                           for pipe, dest, name in ((child.stdout, sys.stdout.buffer, "stdout.bin"),
                                                    (child.stderr, sys.stderr.buffer, "stderr.bin"))]
                for reader in readers:
                    reader.start()
                try:
                    child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    raise
                else:
                    for reader in readers:
                        reader.join()
                completed = subprocess.CompletedProcess(argv, child.returncode)
            scan_start = time.monotonic()
            failure_scan = dict(status="INCOMPLETE", max_bytes_per_stream=limit,
                                timeout_seconds=scan_timeout, spool=str(spool))
            try:
                scanned = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                          "scan", str(record), str(spool), str(limit)],
                                         capture_output=True, timeout=scan_timeout, check=True)
                failure_scan.update(json.loads(scanned.stdout))
            except subprocess.TimeoutExpired:
                failure_scan["reason"] = "SCAN_TIMEOUT"
            except (subprocess.CalledProcessError, ValueError) as error:
                failure_scan["reason"] = "SCAN_EXECUTION_ERROR"
                failure_scan["error"] = str(error)
                if isinstance(error, subprocess.CalledProcessError):
                    (spool / "scanner.stderr").write_bytes(error.stderr or b"")
            failure_scan["elapsed_seconds"] = time.monotonic() - scan_start
            if stream_errors:
                failure_scan.update(status="INCOMPLETE", reason="STREAM_IO_ERROR", errors=stream_errors)
            failure_seen.extend([True] if failure_scan.get("matched") else [])
            if failure_scan["status"] == "COMPLETE":
                # Callers have already received the exact bytes; only incomplete
                # scans need their duplicate spool retained for diagnosis.
                assert not spool.is_symlink()
                assert spool.resolve() != Path(cwd).resolve() and spool.resolve().is_relative_to(Path(cwd).resolve())
                shutil.rmtree(spool)
                failure_scan["spool_retained"] = False
            else:
                failure_scan["spool_retained"] = True

    except subprocess.TimeoutExpired:
        sim_metrics.interrupted(timeout)
        event = dict(stage=stage, engine=request["engine"], argv=argv, cwd=cwd,
                     error="TimeoutExpired", timeout=timeout, inputs=request["inputs"])
        with (Path(cwd) / "simulation-process.jsonl").open("a") as f:
            f.write(json.dumps(event) + "\n")
        # All descendants inherit this helper's dedicated process group.
        if os.name == "posix" and (os.getpgrp() == os.getpid()
                                   or os.environ.get("NOCLOOM_STAGE_METRICS_RECEIPT")):
            os.killpg(os.getpgrp(), signal.SIGKILL)
        raise
    raw_exit = completed.returncode
    scan_incomplete = failure_scan is not None and failure_scan["status"] != "COMPLETE"
    delivered_exit = 1 if raw_exit == 0 and (failure_seen or scan_incomplete) else raw_exit
    event = dict(stage=stage, engine=request["engine"], argv=argv, cwd=cwd,
                 raw_exit_code=raw_exit, exit_code=delivered_exit, failure_marker=bool(failure_seen), inputs=request["inputs"],
                 failure_scan=failure_scan,
                 infrastructure_error="FAILURE_SCAN_INCOMPLETE" if scan_incomplete else None,
                 config_sha256=request.get("config_sha256"), behavioral_verdict="OWNED_BY_CALLER")
    with (Path(cwd) / "simulation-process.jsonl").open("a") as f:
        f.write(json.dumps(event) + "\n")
    if stage == "compile" and completed.returncode == 0:
        invocation = [sys.executable, str(Path(__file__).resolve()), "execute", str(record)]
        logical.write_text("#!/bin/sh\nexec " + shlex.join(invocation) + ' "$@"\n')
        logical.chmod(0o755)
    if delivered_exit < 0 and os.name == "posix":
        original_signal = -delivered_exit
        if original_signal not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(original_signal, signal.SIG_DFL)
        os.kill(os.getpid(), original_signal)
    raise SystemExit(delivered_exit)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        scan_output(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        main()
