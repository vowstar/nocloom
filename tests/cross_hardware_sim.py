"""Engine-neutral execution with one open-source compilation per immutable fixture."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import sim_process

from sim_engine import PASS_MARKERS, Simulation, selection


def run(command, work, name, timeout=1800, allow_failure=False):
    with (work / name).open("w") as log:
        result = sim_process.run(
            command, cwd=work, stdout=log, stderr=subprocess.STDOUT, timeout=timeout
        )
    if result.returncode and not allow_failure:
        raise RuntimeError(
            f"{name} exited {result.returncode}: {(work/name).read_text()[-5000:]}"
        )
    return result.returncode


# Threads of one test that compile the same fixture share its cache entry
# one at a time: the second reuses the first's binary.
_cache_locks = {}
_cache_locks_guard = threading.Lock()


def _cache_lock(path):
    with _cache_locks_guard:
        return _cache_locks.setdefault(path, threading.Lock())


def simulate(work, cache, options=()):
    work = Path(work).resolve()
    cache = Path(cache).resolve()
    runner = os.environ.get("NOCLOOM_SIM_RUNNER")
    explicit = os.environ.get("NOCLOOM_SIM")
    if runner and explicit:
        raise ValueError("Select NOCLOOM_SIM or NOCLOOM_SIM_RUNNER, not both")
    if runner or selection() != "verilator":
        engine = Simulation(work, "network.v", runner=runner,
                          compile_timeout=1800, run_timeout=1800,
                          success=PASS_MARKERS)
        return engine.run(options).returncode
    digest = hashlib.sha256(
        (work / "network.v").read_bytes() + (work / "tb.sv").read_bytes()
    ).hexdigest()
    cache = cache / digest
    with _cache_lock(cache):
        cache.mkdir(parents=True, exist_ok=True)
        receipt = cache / "compiled.json"
        engine = Simulation(
            work, "network.v", compile_work=cache,
            compile_argv=[
                "verilator", "--binary", "--timing", "-Wno-fatal",
                "--top-module", "tb", "--build-jobs", "2", "-o", "sim",
                str(work / "tb.sv"), str(work / "network.v"),
            ],
            binary=cache / "obj_dir/sim", compile_timeout=1800, run_timeout=1800,
            success=PASS_MARKERS,
            reuse=receipt.exists() and (cache / "obj_dir/sim").is_file(),
        )
        receipt.write_text(json.dumps({"sha256": digest}) + "\n")
        return engine.run(options).returncode
