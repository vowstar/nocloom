"""Measure one CTest command without changing its output or exit status."""

import argparse
import math
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

from run_suite import descendants, process_table, save, terminate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--record", required=True)
    p.add_argument("--sample-interval", type=float, default=0.5)
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
        p.error("Sample interval must be finite and positive")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        p.error("Missing measured command")
    path = Path(args.record)
    path.parent.mkdir(parents=True, exist_ok=True)
    stage_directory = path.with_suffix(".stages").resolve()
    environment = dict(
        os.environ,
        NOCLOOM_SIM_METRICS_DIR=str(stage_directory),
        NOCLOOM_SIM_CASE=path.stem,
    )
    started = time.monotonic()
    samples, tree_samples, positive_rss_samples = 0, 0, 0
    peak, max_processes = None, None
    job = None
    rc = None
    reason = None
    received_signal = None

    def interrupted(sig, frame):
        nonlocal received_signal
        received_signal = sig
        raise KeyboardInterrupt("signal " + str(sig))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)

    def receipt(final):
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        reaped = bool(job and job["process"].returncode is not None)
        rss_status = (
            "SAMPLED"
            if peak is not None
            else "NO_POSITIVE_RSS_SAMPLE"
            if tree_samples
            else "NO_PROCESS_SAMPLE"
        )
        state = (
            "RUNNING"
            if not final
            else "EXECUTION_ERROR"
            if reason and reason.startswith("EXECUTION_ERROR:")
            else "INTERRUPTED"
            if reason
            else "COMPLETED"
        )
        return {
            "command": command,
            "stage_metrics_directory": str(stage_directory),
            "cwd": str(Path.cwd()),
            "final": final,
            "record_state": state,
            "workload_completed": final and reason is None,
            "elapsed_method": "monotonic wrapper wall time, including cleanup",
            "elapsed_seconds": time.monotonic() - started,
            "exit_code": rc,
            "interruption": reason,
            "termination_signal": -rc if rc is not None and rc < 0 else None,
            "termination_origin": ("wrapper" if received_signal is not None else "child")
            if rc is not None and rc < 0 else None,
            "samples": samples,
            "sample_interval_seconds": args.sample_interval,
            "process_tree_samples": tree_samples,
            "positive_rss_samples": positive_rss_samples,
            "peak_tree_rss_status": rss_status,
            "peak_tree_rss_method": "sampled sum of Linux /proc process-tree resident bytes",
            "peak_tree_rss_bytes": peak,
            "max_processes": max_processes,
            "children_cpu_user_seconds": usage.ru_utime if reaped else None,
            "children_cpu_system_seconds": usage.ru_stime if reaped else None,
            "children_usage_status": "REAPED_CHILDREN" if reaped else "NO_REAPED_CHILD",
            "children_usage_method": "fresh wrapper getrusage(RUSAGE_CHILDREN); waited-for children only",
            "children_maxrss_bytes": int(usage.ru_maxrss * 1024)
            if reaped and usage.ru_maxrss > 0
            else None,
            "children_maxrss_status": "REAPED_CHILD_MAXIMUM"
            if reaped and usage.ru_maxrss > 0
            else "UNAVAILABLE",
            "measurement": "Linux sampled sum of process-tree RSS; shared pages may be counted more than once and sub-interval peaks may be missed. Child rusage is separate, not an aggregate tree peak.",
        }

    try:
        child = subprocess.Popen(command, env=environment)
        job = {"process": child, "known": {}}
        while True:
            table = process_table()
            ids = descendants(
                child.pid if child.poll() is None else -1, table, job["known"]
            )
            if ids:
                tree_samples += 1
                max_processes = max(max_processes or 0, len(ids))
                rss = sum(table[pid][2] for pid in ids)
                if rss > 0:
                    positive_rss_samples += 1
                    peak = max(peak or 0, rss)
            samples += 1
            save(path, receipt(False))
            rc = child.poll()
            if rc is not None:
                break
            try:
                child.wait(timeout=args.sample_interval)
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt as exc:
        reason = str(exc)
        rc = -received_signal if received_signal is not None else 130
    except OSError as exc:
        reason = "EXECUTION_ERROR: " + str(exc)
        rc = 127
        print(reason, file=sys.stderr)
    finally:
        if job:
            terminate(job)
        save(path, receipt(True))
    # CTest treats signal termination differently from a normal nonzero exit,
    # including when WILL_FAIL is set. Publish the receipt before reproducing it.
    if rc < 0:
        sig = -rc
        if sig not in (signal.SIGKILL, signal.SIGSTOP):
            signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
    return rc


if __name__ == "__main__":
    sys.exit(main())
