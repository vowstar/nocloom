"""Per-invocation resource receipts; RSS is not aggregate process-tree memory."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def check_owner(path, owner):
    directory = path.parent
    info = directory.stat(follow_symlinks=False)
    if (path.name != "receipt.json" or not directory.name.startswith("stage-")
            or directory.resolve() != directory or path.is_symlink()
            or (info.st_dev, info.st_ino) != owner):
        raise ValueError(f"Resource receipt is outside its invocation directory: {path}")


def read(path, owner):
    check_owner(path, owner)
    return json.loads(path.read_text())


def write(path, record, owner):
    check_owner(path, owner)
    with tempfile.NamedTemporaryFile(mode="w", prefix=".publish-", dir=path.parent,
                                     delete=False) as pending:
        pending.write(json.dumps(record, indent=2) + "\n")
    temporary = Path(pending.name)
    assert not temporary.is_symlink() and temporary.resolve().parent == path.parent
    check_owner(path, owner)
    temporary.replace(path)


def prepare(argv, cwd, stage, env=None):
    environment = os.environ if env is None else env
    work = Path(cwd or Path.cwd()).resolve()
    directory = Path(environment.get("NOCLOOM_SIM_METRICS_DIR", work / "simulation-metrics")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    owned = Path(tempfile.mkdtemp(prefix="stage-", dir=directory))
    info = owned.stat()
    owner = (info.st_dev, info.st_ino)
    path = owned / "receipt.json"
    record = dict(stage=stage, argv=list(map(os.fspath, argv)), cwd=str(work),
                  case=environment.get("NOCLOOM_SIM_CASE"),
                  status="STARTING", elapsed_seconds=None, user_seconds=None,
                  system_seconds=None, max_rss_bytes=None, exit_code=None,
                  memory_scope="RUSAGE_CHILDREN maximum individual process RSS, not concurrent tree sum",
                  resource_status="NOT_MEASURED", tree_rss_peak_bytes=None)
    write(path, record, owner)
    return dict(path=path, owner=owner, start=time.monotonic(),
                command=[sys.executable, str(Path(__file__).resolve()),
                         str(path), *map(str, owner)])


def finish(ticket, exit_code=None, error=None):
    path = ticket["path"]
    owner = ticket["owner"]
    record = read(path, owner)
    if record.get("interruption") == "TimeoutExpired":
        error = "TimeoutExpired"
    if record["status"] in ("STARTING", "RUNNING"):
        record.update(status="TIMEOUT" if error == "TimeoutExpired" else "INCOMPLETE",
                      elapsed_seconds=time.monotonic() - ticket["start"],
                      elapsed_scope="parent observed invocation including helper startup",
                      resource_status="UNAVAILABLE_HELPER_INTERRUPTED", exit_code=exit_code)
    if "timeout_seconds" in ticket:
        record["timeout_seconds"] = ticket["timeout_seconds"]
    if error:
        record["error"] = error
        if error == "TimeoutExpired":
            record["status"] = "TIMEOUT"
    write(path, record, owner)
    return dict(path=str(path), **record)


def actual_argv(argv):
    """Expose the translated engine command inside a routing helper."""
    value = os.environ.get("NOCLOOM_STAGE_METRICS_RECEIPT")
    if value:
        path = Path(value)
        owner = tuple(map(int, os.environ["NOCLOOM_STAGE_METRICS_OWNER"].split()))
        record = read(path, owner)
        record["invocation_argv"] = record["argv"]
        record["argv"] = list(map(os.fspath, argv))
        write(path, record, owner)


def interrupted(timeout):
    value = os.environ.get("NOCLOOM_STAGE_METRICS_RECEIPT")
    if value:
        path = Path(value)
        owner = tuple(map(int, os.environ["NOCLOOM_STAGE_METRICS_OWNER"].split()))
        record = read(path, owner)
        record.update(interruption="TimeoutExpired", timeout_seconds=timeout)
        write(path, record, owner)


def main():
    path = Path(sys.argv[1])
    owner = tuple(map(int, sys.argv[2:4]))
    record = read(path, owner)
    argv = record["argv"]
    record["status"] = "RUNNING"
    write(path, record, owner)
    environment = dict(os.environ, NOCLOOM_STAGE_METRICS_RECEIPT=str(path),
                       NOCLOOM_STAGE_METRICS_OWNER=" ".join(map(str, owner)))
    start = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=record["cwd"], env=environment)
        code = completed.returncode
        error = None
    except OSError as failure:
        code = 127
        error = type(failure).__name__
    record = read(path, owner)
    record.update(status="EXITED" if code == 0 else "EXIT_NONZERO", exit_code=code,
                  elapsed_seconds=time.monotonic() - start,
                  elapsed_scope="fresh helper observed child lifetime")
    try:
        if error:
            record["resource_status"] = "UNAVAILABLE_EXECUTION_ERROR"
            record["status"] = "EXECUTION_ERROR"
            raise RuntimeError("child not started: " + error)
        if sys.platform not in ("linux", "darwin"):
            record["resource_status"] = "UNAVAILABLE_PLATFORM"
            raise RuntimeError("unknown resource units")
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        multiplier = 1 if sys.platform == "darwin" else 1024
        record.update(user_seconds=usage.ru_utime, system_seconds=usage.ru_stime,
                      max_rss_bytes=usage.ru_maxrss * multiplier,
                      resource_status="MEASURED_REAPED_CHILDREN",
                      rss_source_unit="bytes" if multiplier == 1 else "KiB")
    except ImportError:
        record["resource_status"] = "UNAVAILABLE_PLATFORM"
    except RuntimeError:
        pass
    if error:
        record["error"] = error
    write(path, record, owner)
    if code < 0:
        # Preserve signal termination to the caller, rather than wrapping to 255.
        import signal
        if -code not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
