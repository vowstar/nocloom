"""Run explicit daily or full CTest suites with shared Linux resource budgets."""

import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import audit_suite
import suite_adaptive
import suite_history

GIB = 1024**3
HERE = Path(__file__).resolve().parent


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def properties(test):
    return {p["name"]: p["value"] for p in test.get("properties", [])}


def select(inventory, profile, policy, include=()):
    tests = inventory["tests"]
    names = [t["name"] for t in tests]
    if not names or len(names) != len(set(names)):
        raise ValueError("CTest inventory is empty or contains duplicate names")
    daily = {n for group in policy["daily"] for n in group["tests"]}
    if set(include) - set(names):
        raise ValueError("Affected test missing from inventory")
    daily.update(include)
    if profile == "daily" and daily - set(names):
        raise ValueError(
            "Daily tests missing from build: " + ", ".join(sorted(daily - set(names)))
        )
    chosen = tests if profile == "full" else [t for t in tests if t["name"] in daily]
    if not chosen:
        raise ValueError("Zero tests selected")
    # CTest handles dependencies and fixture setup/cleanup; resolve them explicitly
    # so unexpected automatic additions cannot disappear from result accounting.
    selected = {t["name"] for t in chosen}
    while True:
        before = set(selected)
        fixtures = set()
        for t in tests:
            if t["name"] in selected:
                p = properties(t)
                selected.update(p.get("DEPENDS", []))
                fixtures.update(p.get("FIXTURES_REQUIRED", []))
        for t in tests:
            p = properties(t)
            if fixtures.intersection(
                p.get("FIXTURES_SETUP", []) + p.get("FIXTURES_CLEANUP", [])
            ):
                selected.add(t["name"])
        if selected == before:
            break
    if selected - set(names):
        raise ValueError("Dependency is not in the inventory")
    chosen = [t for t in tests if t["name"] in selected]
    unavailable = [t["name"] for t in chosen if "command" not in t]
    if unavailable:
        raise ValueError(
            "Selected CTest command unavailable: " + ", ".join(unavailable)
        )
    for t in chosen:
        if (
            not isinstance(t["command"], list)
            or not t["command"]
            or not all(isinstance(arg, str) for arg in t["command"])
            or not t["command"][0]
        ):
            raise ValueError("Invalid selected CTest command: " + t["name"])
    return chosen


def memory():
    return {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
    }


def host_resources():
    mem = memory()
    cpus = sorted(os.sched_getaffinity(0))
    limits = []
    totals = [mem["MemTotal"]]
    # Respect enclosing cgroups, including ancestor limits. Affinity accounts for
    # cpusets; CPU quotas additionally bound the allocation inside containers.
    cpu_capacity = float(len(cpus))
    for row in Path("/proc/self/cgroup").read_text().splitlines():
        _, controllers, relative = row.split(":", 2)
        if not controllers:
            root = Path("/sys/fs/cgroup")
            leaf = root / relative.lstrip("/")
            for p in [leaf, *leaf.parents]:
                if p == root.parent:
                    break
                try:
                    limit = (p / "memory.max").read_text().strip()
                    if limit != "max":
                        totals.append(int(limit))
                        limits.append(
                            max(0, int(limit) - int((p / "memory.current").read_text()))
                        )
                    quota, period = (p / "cpu.max").read_text().split()
                    if quota != "max":
                        cpu_capacity = min(cpu_capacity, int(quota) / int(period))
                except FileNotFoundError:
                    pass
        elif "memory" in controllers.split(","):
            root = Path("/sys/fs/cgroup/memory")
            leaf = root / relative.lstrip("/")
            for p in [leaf, *leaf.parents]:
                if p == root.parent:
                    break
                try:
                    totals.append(int((p / "memory.limit_in_bytes").read_text()))
                    limits.append(
                        max(
                            0,
                            int((p / "memory.limit_in_bytes").read_text())
                            - int((p / "memory.usage_in_bytes").read_text()),
                        )
                    )
                except FileNotFoundError:
                    pass
        elif "cpu" in controllers.split(","):
            root = Path("/sys/fs/cgroup/cpu")
            leaf = root / relative.lstrip("/")
            for p in [leaf, *leaf.parents]:
                if p == root.parent:
                    break
                try:
                    quota = int((p / "cpu.cfs_quota_us").read_text())
                    period = int((p / "cpu.cfs_period_us").read_text())
                    if quota > 0:
                        cpu_capacity = min(cpu_capacity, quota / period)
                except FileNotFoundError:
                    pass
    return {
        "cpus": cpus,
        "cpu_capacity": cpu_capacity,
        "load1": os.getloadavg()[0],
        "memory_total": min(totals),
        "memory_available": min([mem["MemAvailable"], *limits]),
    }


def budget(host, engines, max_processors, args):
    reserve = max(args.reserve_gib * GIB, host["memory_total"] * 0.1)
    usable = min((host["memory_available"] - reserve) * 0.5, args.max_memory_gib * GIB)
    cpus = min(
        math.floor(host["cpu_capacity"] * args.cpu_fraction),
        math.floor(host["cpu_capacity"] - host["load1"]),
    )
    cpu_per_engine = min(args.max_jobs, cpus // engines)
    mem_per_engine = math.floor(usable / engines)
    jobs = min(cpu_per_engine, mem_per_engine // int(args.memory_per_slot_gib * GIB))
    # A PROCESSORS>N test may run alone under CTest -jN. Reserve for that case too.
    if jobs < 1 or mem_per_engine < max_processors * args.memory_per_slot_gib * GIB:
        raise ValueError("Insufficient CPU/memory headroom; no tests started")
    return {
        "jobs_per_engine": jobs,
        "cpu_count_per_engine": jobs,
        "memory_bytes_per_engine": mem_per_engine,
        "host_reserve_bytes": math.ceil(reserve),
        "memory_per_slot_bytes": int(args.memory_per_slot_gib * GIB),
        "build_jobs_per_engine": min(4, jobs),
        "protection": args.protection,
        "maximum_processors": max_processors,
    }


def read_results(path, selected):
    rows = {
        t["name"]: {"name": t["name"], "status": "NOT_RUN", "seconds": 0}
        for t in selected
    }
    if not Path(path).exists():
        return list(rows.values())
    seen = set()
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.attrib["name"]
        if name not in rows or name in seen:
            raise ValueError("Unexpected or duplicate JUnit test: " + name)
        seen.add(name)
        status = case.attrib.get("status", "run")
        if status not in ("run", "notrun", "disabled", "fail"):
            raise ValueError("Unrecognized JUnit status: " + status)
        if case.find("skipped") is not None or status in ("notrun", "disabled"):
            verdict = "NOT_RUN"
        elif (
            case.find("failure") is not None
            or case.find("error") is not None
            or status == "fail"
        ):
            # CTest's failure type is authoritative. Captured output may contain
            # command arguments such as timeout=120 without a timeout occurring.
            failure_types = []
            for tag in ("failure", "error"):
                failure = case.find(tag)
                if failure is not None:
                    declared = [
                        failure.get(k) for k in ("message", "type") if failure.get(k)
                    ]
                    failure_types.extend(declared or [failure.text or ""])
            verdict = (
                "TIMEOUT"
                if any(
                    re.fullmatch(r"timeout|timed out", value.strip(), re.IGNORECASE)
                    for value in failure_types
                )
                else "FAIL"
            )
        else:
            verdict = "PASS"
        rows[name] = {
            "name": name,
            "status": verdict,
            "seconds": float(case.attrib.get("time", 0)),
        }
    return list(rows.values())


def cmake_quote(value):
    value = str(value)
    eq = "="
    while "]" + eq + "]" in value:
        eq += "="
    return "[" + eq + "[" + value + "]" + eq + "]"


def instrument(selected, directory, default_cwd):
    directory.mkdir()
    records = directory.parent / "test-resources"
    records.mkdir()
    lines, mapping = [], {}
    for index, test in enumerate(selected):
        record = records / (str(index) + ".json")
        mapping[test["name"]] = str(record.relative_to(directory.parent))
        command = [
            sys.executable,
            str(HERE / "suite_probe.py"),
            "--record",
            str(record),
            "--",
            *test["command"],
        ]
        lines.append(
            "add_test("
            + " ".join(cmake_quote(x) for x in [test["name"], *command])
            + ")"
        )
        props = {"WORKING_DIRECTORY": str(default_cwd), **properties(test)}
        for key, value in props.items():
            if isinstance(value, list):
                if any(not isinstance(x, str) for x in value):
                    raise ValueError("Unsupported structured CTest property: " + key)
                value = ";".join(value)
            elif isinstance(value, bool):
                value = "TRUE" if value else "FALSE"
            elif not isinstance(value, (str, float, int)):
                raise TypeError("Unsupported CTest property: " + key)
            lines.append(
                "set_tests_properties("
                + cmake_quote(test["name"])
                + " PROPERTIES "
                + cmake_quote(key)
                + " "
                + cmake_quote(value)
                + ")"
            )
    (directory / "CTestTestfile.cmake").write_text("\n".join(lines) + "\n")
    save(directory.parent / "resource-map.json", mapping)
    return mapping


def process_table():
    table = {}
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != os.getuid():
                continue
            fields = (p / "stat").read_text().rsplit(")", 1)[1].split()
            table[int(p.name)] = (
                int(fields[1]),
                fields[19],
                int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            )
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return table


def descendants(root, table, known):
    ids = {
        pid for pid, start in known.items() if pid in table and table[pid][1] == start
    }
    if root in table and (root not in known or table[root][1] == known[root]):
        ids.add(root)
    while True:
        more = {pid for pid, item in table.items() if item[0] in ids} - ids
        if not more:
            break
        ids.update(more)
    known.update({pid: table[pid][1] for pid in ids})
    return ids


def terminate(job):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        table = process_table()
        ids = descendants(
            job["process"].pid if job["process"].poll() is None else -1,
            table,
            job["known"],
        )
        if not ids:
            break
        for pid in sorted(ids, reverse=True):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(0.25)
    job["process"].wait(timeout=10)


def read_telemetry(path, root):
    """Keep corrupt evidence visible without inventing measurements."""
    path, root = Path(path), Path(root)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Resource record escapes suite results: " + str(path))
    row = {"path": str(path.relative_to(root)), "sha256": None}
    try:
        raw = path.read_bytes()
        row["sha256"] = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError("Resource record must be a JSON object")
        for field in (
            "peak_tree_rss_bytes",
            "children_maxrss_bytes",
            "children_cpu_user_seconds",
            "children_cpu_system_seconds",
            "elapsed_seconds",
            "max_rss_bytes",
            "user_seconds",
            "system_seconds",
        ):
            number = value.get(field)
            if number is not None and (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or number < 0
            ):
                raise ValueError("Invalid nonnegative resource value: " + field)
        row.update(status="COLLECTED", resources=value)
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
        row.update(status="TELEMETRY_ERROR", error=type(exc).__name__ + ": " + str(exc))
    return row


def seal_results(root):
    """Hash retained readable files; explicitly record rejected/unreadable entries."""
    root = Path(root)
    lines, errors = [], []
    for path in sorted(root.rglob("*")):
        if path.name in ("MANIFEST.txt", "manifest-errors.json"):
            continue
        if path.is_symlink():
            errors.append(
                {
                    "path": str(path.relative_to(root)),
                    "sha256": None,
                    "error": "Symbolic link rejected; target not read",
                }
            )
            continue
        if not path.is_file():
            continue
        try:
            lines.append(sha(path) + "  " + str(path.relative_to(root)) + "\n")
        except OSError as exc:
            errors.append(
                {
                    "path": str(path.relative_to(root)),
                    "sha256": None,
                    "error": type(exc).__name__ + ": " + str(exc),
                }
            )
    error_path = root / "manifest-errors.json"
    save(
        error_path, {"status": "INCOMPLETE" if errors else "COMPLETE", "errors": errors}
    )
    lines.append(sha(error_path) + "  manifest-errors.json\n")
    (root / "MANIFEST.txt").write_text("".join(lines))
    return errors


def collect_stage_metrics(record, root):
    """Bind observed per-call records without inferring uninstrumented coverage."""
    directory = Path(record).with_suffix(".stages")
    if not directory.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("Stage resource directory escapes suite results")
    rows = []
    paths = (
        sorted(p for p in directory.iterdir() if p.suffix == ".json")
        if directory.exists()
        else []
    )
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(Path(root).resolve()):
            raise ValueError("Stage resource record escapes suite results")
        rows.append(read_telemetry(path, root))
    return {
        "status": (
            "TELEMETRY_ERROR"
            if any(r["status"] == "TELEMETRY_ERROR" for r in rows)
            else "COLLECTED"
            if rows
            else "NO_RECORDS_OBSERVED"
        ),
        "completeness": "NOT_INFERRED_FROM_RECORD_PRESENCE",
        "records": rows,
    }


def progress(path, total):
    text = Path(path).read_text(errors="replace")
    pattern = r"Test\s+#\s*\d+:\s+(.+?)\s+\.{2,}\s*(Passed|\*\*\*.*?)\s+([\d.]+)\s+sec"
    rows = {m[0]: m[1] for m in re.findall(pattern, text)}
    return {
        "completed": len(rows),
        "selected": total,
        "observed_counts": dict(Counter(rows.values())),
        "nonpass": [n for n, state in rows.items() if state != "Passed"],
        "provisional": True,
    }


def create_cgroup(root, name, limit):
    root = Path(root).resolve()
    filesystem = subprocess.check_output(
        ["stat", "-f", "-c", "%T", str(root)], text=True
    ).strip()
    if filesystem != "cgroup2fs":
        raise ValueError("Hard protection requires a real cgroup v2 filesystem")
    group = root / name
    assert not group.is_symlink()
    assert group.resolve() != root and group.resolve().is_relative_to(root)
    group.mkdir()
    try:
        if not all(
            (group / f).exists()
            for f in ("memory.max", "memory.swap.max", "cgroup.procs")
        ):
            raise ValueError("Delegated memory controller is not enabled")
        (group / "memory.max").write_text(str(limit))
        (group / "memory.swap.max").write_text("0")
        if int((group / "memory.max").read_text()) != limit:
            raise ValueError("Memory cap was not applied")
    except Exception:
        assert not group.is_symlink()
        assert group.resolve() != root and group.resolve().is_relative_to(root)
        group.rmdir()
        raise
    return group


def start_wave(job, ctest, slots, plan):
    selected = suite_adaptive.take_wave(job["pending"], slots)
    number = len(job["waves"])
    wave_dir = job["engine_dir"] / "waves" / str(number)
    wave_dir.mkdir(parents=True)
    expression = "^(" + "|".join(re.escape(t["name"]) for t in selected) + ")$"
    cmd = [
        ctest,
        "--test-dir",
        str(job["engine_dir"] / "ctest"),
        "-j",
        str(slots),
        "--output-on-failure",
        "--output-junit",
        str(wave_dir / "results.xml"),
        "-R",
        expression,
    ]
    env = dict(
        job["env"],
        CMAKE_BUILD_PARALLEL_LEVEL=str(min(plan["build_jobs_per_engine"], slots)),
    )
    record = {
        "argv": cmd,
        "names": [t["name"] for t in selected],
        "slots": slots,
        "engine": job["engine"],
        "cpus": job["cpus"],
        "build_jobs": env["CMAKE_BUILD_PARALLEL_LEVEL"],
        "test_timeouts_unchanged": True,
    }
    save(wave_dir / "command.json", record)
    job["waves"].append(record)
    save(job["engine_dir"] / "command.json", {"waves": job["waves"], "engine": job["engine"]})
    # CTest starts only admitted work; resource wait never consumes a test timeout.
    with (job["engine_dir"] / "ctest.log").open("a") as log:
        process = subprocess.Popen(
            cmd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=job["setup"],  # noqa: PLW1509 - Single-threaded coordinator.
        )
    job.update(process=process, known={}, collected=False, wave_dir=wave_dir)


def finish_wave(job):
    terminate(job)
    rc = job["process"].returncode
    job["exit_codes"].append(rc)
    result = job["wave_dir"] / "results.xml"
    if result.exists():
        # A wave may not silently pull in a test assigned to a later invocation.
        read_results(result, [{"name": n} for n in job["waves"][-1]["names"]])
        job["wave_results"].append(result)
    elif rc == 0:
        raise ValueError("Successful CTest wave has no JUnit result")
    suite_adaptive.merge_junit(
        job["wave_results"], job["engine_dir"] / "results.xml", job["selected"]
    )
    source = job["engine_dir"] / "ctest/Testing/Temporary/LastTest.log"
    if source.exists():
        shutil.copyfile(source, job["wave_dir"] / "LastTest.log")
        with (job["engine_dir"] / "LastTest.log").open("a") as log:
            log.write(source.read_text(errors="replace") + "\n")
    save(job["wave_dir"] / "exit.json", {"ctest_exit": rc})
    job["collected"] = True


def run(args):
    if args.run and os.getuid() == 0:
        raise ValueError("Run tests as an ordinary user")
    policy_path = Path(args.policy).resolve()
    policy = json.loads(policy_path.read_text())
    builds = [Path(b).resolve() for b in args.build]
    if len(builds) != len(args.engine) or len(set(builds)) != len(builds):
        raise ValueError("Provide one distinct build directory per engine")
    if len(set(args.engine)) != len(args.engine):
        raise ValueError("Duplicate engine")
    if "external" in args.engine and not args.sim_config:
        raise ValueError("External engine requires --sim-config")
    if args.run and args.protection == "cgroup" and not args.cgroup_root:
        raise ValueError(
            "Hard memory protection requires a delegated cgroup v2 --cgroup-root"
        )
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    inventories, selections = [], []
    for build, engine in zip(builds, args.engine):
        raw = subprocess.check_output(
            [args.ctest, "--test-dir", str(build), "--show-only=json-v1"]
        )
        inv = json.loads(raw)
        inventories.append(inv)
        selections.append(select(inv, args.profile, policy, args.include))
        (out / (engine + "-inventory.json")).write_bytes(raw)
    expected = {t["name"] for t in inventories[0]["tests"]}
    if any({t["name"] for t in i["tests"]} != expected for i in inventories):
        raise ValueError("Engine inventories differ")
    host = host_resources()
    maxp = max(int(properties(t).get("PROCESSORS", 1)) for s in selections for t in s)
    plan = budget(host, len(builds), maxp, args)
    plan.update(
        run_id=str(uuid.uuid4()),
        started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        candidate_manifest_sha256=sha(args.candidate_manifest)
        if args.candidate_manifest
        else None,
        environment_manifest_sha256=sha(args.environment_manifest)
        if args.environment_manifest
        else None,
        history_store=str(Path(args.history).resolve()),
        profile=args.profile,
        additional_affected_tests=args.include,
        host=host,
        policy_sha256=sha(policy_path),
        inventory_sha256=[sha(out / (e + "-inventory.json")) for e in args.engine],
        probe_sha256=sha(HERE / "suite_probe.py"),
        adaptive_sha256=sha(HERE / "suite_adaptive.py"),
        scheduling=args.scheduling,
        initial_jobs=min(args.initial_jobs, plan["jobs_per_engine"]),
        ramp_seconds=args.ramp_seconds,
        engines=args.engine,
        build_directories=list(map(str, builds)),
        selected=[t["name"] for t in selections[0]],
        not_selected=sorted(expected - {t["name"] for t in selections[0]}),
        inventory_total=len(expected),
        run_requested=args.run,
        sim_config_sha256=sha(args.sim_config) if args.sim_config else None,
        runner_sha256=sha(__file__),
        ctest_version=subprocess.check_output(
            [args.ctest, "--version"], text=True
        ).splitlines()[0],
        python_version=sys.version,
        note="Budgets are shared by these engines. Monitored mode has no aggregate kernel memory cap.",
    )
    save(out / "plan.json", plan)
    print(
        json.dumps(
            {
                k: plan[k]
                for k in (
                    "profile",
                    "jobs_per_engine",
                    "memory_bytes_per_engine",
                    "protection",
                )
            }
        ),
        flush=True,
    )
    if not args.run:
        print("PLAN_ONLY: no tests started", flush=True)
        return 0
    # A user-wide local-host lock coordinates both engines and subsequent invocations.
    # Separate builds are mandatory because tests can share files inside a build.
    lockdir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / (
        "nocloom-suite-" + str(os.getuid())
    )
    lockdir.mkdir(mode=0o700, exist_ok=True)
    if (
        lockdir.is_symlink()
        or lockdir.stat().st_uid != os.getuid()
        or lockdir.stat().st_mode & 0o077
    ):
        raise ValueError("Unsafe coordinator lock directory")
    lock = os.open(lockdir / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if any(shutil.disk_usage(b).free < args.disk_reserve_gib * GIB for b in builds):
        os.close(lock)
        raise ValueError("Insufficient scratch space; no tests started")
    if (
        memory()["MemAvailable"]
        < plan["host_reserve_bytes"] + len(builds) * plan["memory_bytes_per_engine"]
    ):
        os.close(lock)
        raise ValueError("Memory availability changed; no tests started")
    Path(args.history).mkdir(parents=True, exist_ok=True)
    jobs, groups = [], []
    start = time.monotonic()
    interrupted = None

    def on_signal(signum, frame):
        raise KeyboardInterrupt("signal " + str(signum))

    old_handler = signal.signal(signal.SIGTERM, on_signal)
    try:
        for index, (build, engine, selected) in enumerate(
            zip(builds, args.engine, selections)
        ):
            engine_dir = out / engine
            engine_dir.mkdir()
            (engine_dir / "ctest.log").touch()
            group = None
            if args.protection == "cgroup":
                group = create_cgroup(
                    args.cgroup_root,
                    "nocloom-" + str(os.getpid()) + "-" + engine,
                    plan["memory_bytes_per_engine"],
                )
                groups.append(group)
            cpus = set(
                host["cpus"][
                    index * plan["jobs_per_engine"] : (index + 1) * plan["jobs_per_engine"]
                ]
            )

            def setup(cpus=cpus, group=group):
                os.sched_setaffinity(0, cpus)
                cap = plan["memory_bytes_per_engine"]
                resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                if group:
                    (group / "cgroup.procs").write_text(str(os.getpid()))

            env = dict(
                os.environ,
                NOCLOOM_SIM=engine,
                CMAKE_BUILD_PARALLEL_LEVEL=str(plan["build_jobs_per_engine"]),
                OMP_NUM_THREADS="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            env.pop("NOCLOOM_SIM_RUNNER", None)
            env.pop("NOCLOOM_SIM_CONFIG", None)
            if engine == "external":
                env["NOCLOOM_SIM_CONFIG"] = str(Path(args.sim_config).resolve())
            mapping = instrument(selected, engine_dir / "ctest", build)
            measured_inventory = json.loads(
                subprocess.check_output(
                    [
                        args.ctest,
                        "--test-dir",
                        str(engine_dir / "ctest"),
                        "--show-only=json-v1",
                    ]
                )
            )
            if {t["name"] for t in measured_inventory["tests"]} != {
                t["name"] for t in selected
            }:
                raise ValueError("Instrumented CTest selection changed")
            for original, measured in zip(selected, measured_inventory["tests"]):
                if original["command"] != measured["command"][5:]:
                    raise ValueError(
                        "Instrumentation changed test argv: " + original["name"]
                    )
                mp = properties(measured)
                if any(mp.get(k) != v for k, v in properties(original).items()):
                    raise ValueError(
                        "Instrumentation changed test properties: " + original["name"]
                    )
            save(engine_dir / "instrumented-inventory.json", measured_inventory)
            jobs.append(
                {
                    "process": None,
                    "known": {},
                    "engine_dir": engine_dir,
                    "selected": selected,
                    "engine": engine,
                    "build": build,
                    "group": group,
                    "mapping": mapping,
                    "cpus": sorted(cpus),
                    "setup": setup,
                    "env": env,
                    "pending": suite_adaptive.components(selected)
                    if args.scheduling == "adaptive"
                    else [selected],
                    "waves": [],
                    "wave_results": [],
                    "exit_codes": [],
                    "collected": True,
                }
            )
        admission = suite_adaptive.Admission(
            plan["jobs_per_engine"],
            args.initial_jobs,
            args.ramp_seconds,
            len(jobs),
            plan["host_reserve_bytes"],
            plan["memory_bytes_per_engine"],
            plan["memory_per_slot_bytes"],
            args.cpu_fraction,
        )
        ticks = suite_adaptive.cpu_ticks()
        while any(
            j["pending"] or (j["process"] and j["process"].poll() is None) for j in jobs
        ):
            for job in jobs:
                if (
                    job["process"]
                    and job["process"].poll() is not None
                    and not job["collected"]
                ):
                    finish_wave(job)
            table = process_table()
            mem = memory()
            snapshot = {
                "elapsed_seconds": time.monotonic() - start,
                "memory_available_bytes": mem["MemAvailable"],
                "engines": [],
            }
            for job in jobs:
                ids = descendants(
                    job["process"].pid
                    if job["process"] and job["process"].poll() is None
                    else -1,
                    table,
                    job["known"],
                )
                rss = sum(table[pid][2] for pid in ids)
                suite_history.observe(
                    plan["run_id"],
                    job["engine"],
                    job["engine_dir"] / "ctest.log",
                    args.history,
                )
                snapshot["engines"].append(
                    {
                        "engine": job["engine"],
                        "rss_bytes": rss,
                        "processes": len(ids),
                        "progress": progress(
                            job["engine_dir"] / "ctest.log", len(job["selected"])
                        ),
                    }
                )
                if rss > plan["memory_bytes_per_engine"] * 0.85:
                    interrupted = "RESOURCE_INTERRUPTION: batch memory high-water"
                free = shutil.disk_usage(job["build"]).free
                if free < args.disk_reserve_gib * GIB:
                    interrupted = "RESOURCE_INTERRUPTION: disk reserve"
            if mem["MemAvailable"] < plan["host_reserve_bytes"]:
                interrupted = "RESOURCE_INTERRUPTION: host memory reserve"
            if time.monotonic() - start > args.max_hours * 3600:
                interrupted = "RUNNER_TIME_LIMIT"
            with (out / "resources.jsonl").open("a") as f:
                f.write(json.dumps(snapshot) + "\n")
            if interrupted:
                break
            current_ticks = suite_adaptive.cpu_ticks()
            busy = suite_adaptive.busy_fraction(ticks, current_ticks)
            ticks = current_ticks
            observed_slot = 0
            for job in jobs:
                for test in job["selected"]:
                    record = job["engine_dir"] / job["mapping"][test["name"]]
                    if record.exists():
                        try:
                            measured = read_telemetry(record, out)
                        except ValueError:
                            measured = {}
                        peak = measured.get("resources", {}).get("peak_tree_rss_bytes")
                        weight = max(1, int(properties(test).get("PROCESSORS", 1)))
                        if peak is not None:
                            observed_slot = max(
                                observed_slot, math.ceil(peak * 1.5 / weight)
                            )
            decision = admission.update(
                snapshot["elapsed_seconds"],
                mem["MemAvailable"],
                busy,
                [x["rss_bytes"] for x in snapshot["engines"]],
                observed_slot,
            )
            if args.scheduling == "fixed":
                decision.update(
                    slots_per_engine=plan["jobs_per_engine"], reason="FIXED_EXPLICIT"
                )
            decision["elapsed_seconds"] = snapshot["elapsed_seconds"]
            save(
                out / "status.json",
                {
                    "state": "RUNNING",
                    "observation": snapshot,
                    "admission": decision,
                    "note": "Admission applies to the next wave; running commands and timeouts stay unchanged",
                },
            )
            if (
                decision["memory_safe_slots_per_engine"] == 0
                and args.scheduling == "adaptive"
            ):
                interrupted = "RESOURCE_INTERRUPTION: observed slot cannot fit the fixed safe memory budget"
                break
            with (out / "parallelism.jsonl").open("a") as f:
                f.write(json.dumps(decision) + "\n")
            for job in jobs:
                if (
                    job["pending"]
                    and job["collected"]
                    and decision["slots_per_engine"] > 0
                ):
                    start_wave(job, args.ctest, decision["slots_per_engine"], plan)
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = "INTERRUPTED"
    except (
        OSError,
        ValueError,
        TypeError,
        ET.ParseError,
        subprocess.SubprocessError,
    ) as exc:
        interrupted = "RUNNER_ERROR: " + str(exc)
    finally:
        for job in jobs:
            if job["process"]:
                # Also collect/terminate retained descendants after their parent exits.
                terminate(job)
                if not job["collected"]:
                    try:
                        finish_wave(job)
                    except (ValueError, ET.ParseError, OSError) as exc:
                        interrupted = "RUNNER_ERROR: " + str(exc)
        signal.signal(signal.SIGTERM, old_handler)
        for group in groups:
            events = (group / "memory.events").read_text()
            (out / (group.name + "-memory-events.txt")).write_text(events)
            if any(
                int(line.split()[1]) > 0
                for line in events.splitlines()
                if line.split()[0] in ("oom", "oom_kill")
            ):
                interrupted = "RESOURCE_INTERRUPTION: cgroup OOM"
            try:
                root = Path(args.cgroup_root).resolve()
                assert not group.is_symlink()
                assert group.resolve() != root and group.resolve().is_relative_to(root)
                group.rmdir()
            except OSError:
                interrupted = "RESOURCE_INTERRUPTION: cgroup still populated; retained"
        os.close(lock)
    results = []
    for job in jobs:
        try:
            rows = read_results(job["engine_dir"] / "results.xml", job["selected"])
            error = None
        except (ValueError, ET.ParseError) as exc:
            rows = [
                {"name": t["name"], "status": "NOT_RUN", "seconds": 0}
                for t in job["selected"]
            ]
            error = str(exc)
        for row in rows:
            record = job["engine_dir"] / job["mapping"][row["name"]]
            row.update(suite_adaptive.duration_class(row["status"], row["seconds"]))
            row["resource_record"] = str(record.relative_to(out))
            row["telemetry_errors"] = []
            if record.exists() or record.is_symlink():
                try:
                    measured = read_telemetry(record, out)
                    row["resource_evidence"] = measured
                    if "resources" in measured:
                        row["resources"] = measured["resources"]
                    else:
                        row["telemetry_errors"].append(measured)
                        # Only validated resource objects enter measurement history.
                        # The raw file remains bound above and in telemetry_errors.
                        row["resource_record"] = None
                except ValueError as exc:
                    row["telemetry_errors"].append(
                        {
                            "path": row["resource_record"],
                            "sha256": None,
                            "error": str(exc),
                        }
                    )
                    row["resource_record"] = None
            elif row["status"] != "NOT_RUN":
                row["telemetry_errors"].append(
                    {
                        "path": row["resource_record"],
                        "sha256": None,
                        "error": "Expected resource record missing",
                    }
                )
            try:
                row["stage_resources"] = collect_stage_metrics(record, out)
                row["telemetry_errors"].extend(
                    r
                    for r in row["stage_resources"]["records"]
                    if r["status"] == "TELEMETRY_ERROR"
                )
            except (OSError, ValueError) as exc:
                rejected = {
                    "path": str(record.with_suffix(".stages").relative_to(out)),
                    "sha256": None,
                    "error": type(exc).__name__ + ": " + str(exc),
                }
                row["stage_resources"] = {
                    "status": "TELEMETRY_ERROR",
                    "records": [],
                    "error": rejected,
                    "completeness": "REJECTED",
                }
                row["telemetry_errors"].append(rejected)
        last = job["engine_dir"] / "ctest/Testing/Temporary/LastTest.log"
        if last.exists() and not (job["engine_dir"] / "LastTest.log").exists():
            shutil.copyfile(last, job["engine_dir"] / "LastTest.log")
        results.append(
            {
                "engine": job["engine"],
                "ctest_exit": next((rc for rc in job["exit_codes"] if rc != 0), 0)
                if job["exit_codes"]
                else None,
                "wave_count": len(job["waves"]),
                "counts": dict(Counter(r["status"] for r in rows)),
                "tests": rows,
                "parse_error": error,
            }
        )
    telemetry_errors = [
        dict(engine=engine_result["engine"], test=row["name"], **error)
        for engine_result in results
        for row in engine_result["tests"]
        for error in row["telemetry_errors"]
    ]
    manifest_errors = seal_results(out)
    passed = (
        not telemetry_errors
        and not manifest_errors
        and not interrupted
        and len(results) == len(builds)
        and all(
            r["ctest_exit"] == 0
            and not r["parse_error"]
            and r["counts"] == {"PASS": len(plan["selected"])}
            for r in results
        )
    )
    receipt = {
        "status": "PASS" if passed else "NONPASS",
        "profile": args.profile,
        "measurement_status": "MEASUREMENT_INCOMPLETE"
        if telemetry_errors or manifest_errors
        else "OBSERVED_RECORDS_VALID",
        "telemetry_errors": telemetry_errors,
        "manifest_errors": manifest_errors,
        "elapsed_seconds": time.monotonic() - start,
        "interruption": interrupted,
        "selected_count": len(plan["selected"]),
        "inventory_total": len(expected),
        "not_selected": plan["not_selected"],
        "engines": results,
        "evidence_note": "Summary archive contains CTest evidence only; nested case artifacts remain in build directories. No automatic deletion.",
    }
    save(out / "results.json", receipt)
    duration_rows = [
        {
            "engine": r["engine"],
            "name": t["name"],
            "status": t["status"],
            "seconds": t["seconds"],
            "speed": t["speed"],
            "lower_bound": t["lower_bound"],
            "peak_tree_rss_bytes": t.get("resources", {}).get("peak_tree_rss_bytes"),
            "peak_tree_rss_status": t.get("resources", {}).get(
                "peak_tree_rss_status", "UNAVAILABLE"
            ),
            "children_maxrss_bytes": t.get("resources", {}).get(
                "children_maxrss_bytes"
            ),
            "children_usage_status": t.get("resources", {}).get(
                "children_usage_status", "UNAVAILABLE"
            ),
            "children_cpu_user_seconds": t.get("resources", {}).get(
                "children_cpu_user_seconds"
            ),
            "children_cpu_system_seconds": t.get("resources", {}).get(
                "children_cpu_system_seconds"
            ),
        }
        for r in results
        for t in r["tests"]
    ]
    save(
        out / "durations.json",
        {
            "thresholds_seconds": {"FAST": 30, "MEDIUM": 120, "SLOW": 600},
            "not_a_profile_change": True,
            "rows": duration_rows,
        },
    )
    (out / "durations.tsv").write_text(
        "engine\ttest\tstatus\tseconds\tspeed\ttimeout_lower_bound\tpeak_tree_rss_bytes\n"
        + "".join(
            "\t".join(
                str(row[k])
                for k in (
                    "engine",
                    "name",
                    "status",
                    "seconds",
                    "speed",
                    "lower_bound",
                    "peak_tree_rss_bytes",
                )
            )
            + "\n"
            for row in duration_rows
        )
    )
    save(
        out / "status.json",
        {"state": "FINISHED", "status": receipt["status"], "interruption": interrupted},
    )
    lines = [
        "# Suite results",
        "",
        "Profile: **" + args.profile + "**; status: **" + receipt["status"] + "**.",
        "Measurement: **"
        + receipt["measurement_status"]
        + "**; behavior statuses remain from JUnit.",
        "",
        str(len(plan["selected"]))
        + " selected / "
        + str(len(expected))
        + " registered; unselected tests are not passes.",
        "",
        "| Engine | PASS | FAIL | TIMEOUT | NOT_RUN |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in results:
        lines.append(
            "| "
            + r["engine"]
            + " | "
            + " | ".join(
                str(r["counts"].get(s, 0))
                for s in ("PASS", "FAIL", "TIMEOUT", "NOT_RUN")
            )
            + " |"
        )
        for t in r["tests"]:
            if t["status"] != "PASS":
                lines.append(
                    "\n- " + r["engine"] + ": `" + t["name"] + "` — " + t["status"]
                )
    for r in results:
        lines += [
            "",
            "Observed duration classes: "
            + str(dict(Counter(t["speed"] for t in r["tests"]))),
            "",
        ]
        lines += [
            "",
            "## " + r["engine"] + " slowest tests",
            "",
            "| Test | Seconds | Tree peak RSS MiB |",
            "| --- | ---: | ---: |",
        ]
        for t in sorted(r["tests"], key=lambda t: t["seconds"], reverse=True)[:10]:
            peak = t.get("resources", {}).get("peak_tree_rss_bytes")
            lines.append(
                "| "
                + t["name"]
                + " | "
                + str(round(t["seconds"], 2))
                + " | "
                + (str(round(peak / 1024**2, 1)) if peak is not None else "UNMEASURED")
                + " |"
            )
        lines += ["", "## " + r["engine"] + " largest sampled memory", ""]
        for t in sorted(
            r["tests"],
            key=lambda t: t.get("resources", {}).get("peak_tree_rss_bytes") or -1,
            reverse=True,
        )[:10]:
            peak = t.get("resources", {}).get("peak_tree_rss_bytes")
            lines.append(
                "- "
                + t["name"]
                + ": "
                + (
                    str(round(peak / 1024**2, 1)) + " MiB"
                    if peak is not None
                    else "UNMEASURED"
                )
            )
    for error in telemetry_errors + manifest_errors:
        lines.append("\n- TELEMETRY_ERROR: " + error["path"] + " — " + error["error"])
    lines += ["", "Interruption: " + str(interrupted), "", receipt["evidence_note"]]
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n")
    seal_results(out)
    finalization_stage = "history_ingest"
    try:
        registration = suite_history.ingest(out, args.history)
        save(out / "history-registration.json", registration)
        finalization_stage = "history_report"
        suite_history.report(args.history, out / "history-summary")
        finalization_stage = "audit_queue"
        audit_suite.rotate(
            out / (args.engine[0] + "-inventory.json"),
            policy_path,
            args.history,
            out / "next-audit",
            limit=16,
        )
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        save(
            out / "history-error.json",
            {"error": str(exc), "stage": finalization_stage, "tests_unchanged": True},
        )
        print(
            "Suite finalization failed at " + finalization_stage + ": " + str(exc),
            file=sys.stderr,
        )
        return 2
    # Seal the exported statistics and audit queue together with the run evidence.
    seal_results(out)
    print(str(out / "SUMMARY.md"), flush=True)
    return 0 if passed else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", choices=("daily", "full"), required=True)
    p.add_argument("--build", action="append", required=True)
    p.add_argument(
        "--engine", action="append", choices=("iverilog", "external"), required=True
    )
    p.add_argument(
        "--include",
        action="append",
        default=[],
        help="Add a named affected full-only test to daily",
    )
    p.add_argument("--sim-config")
    p.add_argument("--history", default=str(suite_history.default_store()))
    p.add_argument(
        "--candidate-manifest",
        help="Frozen candidate input manifest, for history grouping",
    )
    p.add_argument(
        "--environment-manifest",
        help="Frozen engine/compiler/environment manifest, for history grouping",
    )
    p.add_argument("--policy", default=str(HERE / "suite_profiles.json"))
    p.add_argument("--ctest", default="ctest")
    p.add_argument("--output", required=True)
    p.add_argument(
        "--run", action="store_true", help="Execute; otherwise only write the plan"
    )
    p.add_argument("--cpu-fraction", type=float, default=0.5)
    p.add_argument(
        "--max-jobs",
        type=int,
        default=16,
        help="Ceiling per engine, not the chosen concurrency",
    )
    p.add_argument("--memory-per-slot-gib", type=float, default=8)
    p.add_argument(
        "--max-memory-gib", type=float, default=256, help="Shared by all engines"
    )
    p.add_argument("--reserve-gib", type=float, default=16)
    p.add_argument("--disk-reserve-gib", type=float, default=16)
    p.add_argument("--max-hours", type=float, default=48)
    p.add_argument("--scheduling", choices=("adaptive", "fixed"), default="adaptive")
    p.add_argument("--initial-jobs", type=int, default=2)
    p.add_argument("--ramp-seconds", type=float, default=30)
    p.add_argument("--protection", choices=("cgroup", "monitored"), default="cgroup")
    p.add_argument(
        "--cgroup-root",
        help="Writable delegated cgroup v2 with memory controller enabled",
    )
    a = p.parse_args()
    if not 0 < a.cpu_fraction <= 1 or any(
        getattr(a, k) <= 0
        for k in (
            "max_jobs",
            "initial_jobs",
            "ramp_seconds",
            "memory_per_slot_gib",
            "max_memory_gib",
            "reserve_gib",
            "disk_reserve_gib",
            "max_hours",
        )
    ):
        p.error("Budgets must be positive and finite; CPU fraction must be in (0,1]")
    if any(
        not math.isfinite(getattr(a, k))
        for k in (
            "ramp_seconds",
            "memory_per_slot_gib",
            "max_memory_gib",
            "reserve_gib",
            "disk_reserve_gib",
            "max_hours",
        )
    ):
        p.error("Budgets must be finite")
    try:
        return run(a)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        print("Suite refused: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
