"""Challenge the library process boundary, the installed API surface, the IR verifier and
failed-generation emptiness."""

import argparse
import json
import re
import shlex
import sim_process
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True


class CheckFailure(AssertionError):
    pass


def require(ok, name):
    if not ok:
        raise CheckFailure("CHECK_FAIL " + name)


def main():
    parser = argparse.ArgumentParser()
    for key in ("boundary", "source", "build", "work", "compiler"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--include-dir", action="append", required=True)
    for key in ("core-library", "io-library", "config-library", "yaml-library"):
        parser.add_argument("--" + key, required=True)
    a = parser.parse_args()
    includes = ["-I" + directory for directory in a.include_dir]
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    source = Path(a.source).resolve()
    records = []

    def witness(name, normal, faulty, checker, folder, kind):
        checker(normal)
        try:
            checker(faulty)
        except CheckFailure as failure:
            require(str(failure) == "CHECK_FAIL " + name, name)
            (folder / "checker.log").write_text(
                "BASELINE_PASS\nREACHED " + name + "\n" + str(failure) + "\n"
            )
            records.append(
                dict(
                    name=name,
                    status="CAUGHT",
                    baseline="PASS",
                    directory=str(folder),
                    witness_kind=kind,
                )
            )
        else:
            raise AssertionError("Uncaught control: " + name)

    def process(args, folder, label, env=None):
        args = list(map(str, args))
        (folder / (label + "-command.txt")).write_text(shlex.join(args) + "\n")
        r = sim_process.run(
            args,
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        (folder / (label + ".log")).write_text(r.stdout + r.stderr)
        return r

    boundary = root / "boundary"
    boundary.mkdir()
    results = {}
    for fault in ("none", "io", "print", "exit"):
        folder = boundary / fault
        folder.mkdir()
        r = process(
            [
                "strace",
                "-f",
                "-e",
                "trace=%file,write,exit_group",
                "-o",
                "trace.log",
                Path(a.boundary).resolve(),
                fault,
            ],
            folder,
            "run",
        )
        trace = (folder / "trace.log").read_text()
        results[fault] = dict(
            stdout=r.stdout, stderr=r.stderr, exit=r.returncode, trace=trace
        )
    for fault, name in (
        ("io", "LIBRARY_NO_IO"),
        ("print", "LIBRARY_NO_PRINT_EXIT.PRINT"),
        ("exit", "LIBRARY_NO_PRINT_EXIT.EXIT"),
    ):

        def checker(r, name=name, fault=fault):
            if fault == "exit":
                require(
                    r["exit"] == 0
                    and r["stdout"] == "RETURNED\n"
                    and "LIB_END\n" in r["stderr"],
                    name,
                )
            elif fault == "print":
                require(
                    r["stdout"] == "RETURNED\n"
                    and r["stderr"] == "LIB_BEGIN\nLIB_END\n",
                    name,
                )
            else:
                between = (
                    r["trace"].split("LIB_BEGIN\\n", 1)[1].split("LIB_END\\n", 1)[0]
                )
                require(
                    not re.search(
                        r"\b(open|openat|creat|unlink|rename|mkdir|stat|newfstatat)\(",
                        between,
                    ),
                    name,
                )

        witness(
            name,
            results["none"],
            results[fault],
            checker,
            boundary / fault,
            "test-only process observer probe; not production mutation",
        )
    install = root / "installed"
    r = process(
        ["cmake", "--install", Path(a.build).resolve(), "--prefix", install],
        root,
        "install",
    )
    assert r.returncode == 0
    headers = sorted(
        str(p.relative_to(install)) for p in (install / "include").rglob("*.*")
    )
    name = "INTERNAL_API_BOUNDARY"
    allowed = {
        "include/" + str(p.relative_to(source / "include"))
        for p in (source / "include").rglob("*.hpp")
    }
    witness(
        name,
        headers,
        headers + ["include/nocloom/core/message.hpp"],
        lambda items: require(bool(items) and set(items) <= allowed, name),
        root,
        "installed manifest corruption",
    )

    ir = root / "ir"
    ir.mkdir()
    normal = process([Path(a.boundary).resolve(), "ir"], ir, "baseline")
    assert normal.returncode == 0 and "CHECK_PASS HARDWARE_IR_VERIFY" in normal.stdout
    original = (source / "src/core/hardware.cpp").read_text()
    anchor = "port.width != ir.values[driver.value.index].width ||"
    assert original.count(anchor) == 1
    (ir / "hardware.cpp").write_text(original.replace(anchor, ""))
    compiled = process(
        [
            a.compiler,
            "-std=c++20",
            *includes,
            source / "tests/library_boundary.cpp",
            ir / "hardware.cpp",
            a.core_library,
            "-o",
            ir / "mutant",
        ],
        ir,
        "compile",
    )
    assert compiled.returncode == 0, compiled.stderr
    changed = process([ir / "mutant", "ir"], ir, "fault")
    assert changed.returncode == 1 and "CHECK_FAIL HARDWARE_IR_VERIFY" in changed.stderr
    records.append(
        dict(
            name="HARDWARE_IR_VERIFY",
            status="CAUGHT",
            baseline="PASS",
            directory=str(ir),
            witness_kind="isolated production verifier rule removal; designated validator assertion failed",
        )
    )
    artifacts = root / "artifacts"
    artifacts.mkdir()
    normal = process(
        [Path(a.boundary).resolve(), "artifacts"], artifacts, "api-baseline"
    )
    assert (
        normal.returncode == 0 and "CHECK_PASS ARTIFACT_FAILURE_EMPTY" in normal.stdout
    )
    original = (source / "src/backends/generation.cpp").read_text()
    anchor = "if (options.backend != Backend::Verilog2001)"
    assert original.count(anchor) == 1
    faulty = original.replace(anchor, "if (false)")
    cli_original = (source / "cli/main.cpp").read_text()
    cli_anchor = 'if (target != "verilog")'
    assert cli_original.count(cli_anchor) == 1
    (artifacts / "main.cpp").write_text(cli_original.replace(cli_anchor, "if (false)"))
    (artifacts / "generation.cpp").write_text(faulty)
    commands = json.loads((Path(a.build) / "compile_commands.json").read_text())
    entry = next(
        c for c in commands if c["file"] == str(source / "src/backends/generation.cpp")
    )
    command = shlex.split(entry["command"])
    command[command.index("-o") + 1] = str(artifacts / "generation.o")
    command[-1] = str(artifacts / "generation.cpp")
    command += includes
    compiled = process(command, Path(entry["directory"]), "artifact-mutation-compile")
    assert compiled.returncode == 0, compiled.stderr
    for label, probe, libraries in (
        ("api", source / "tests/library_boundary.cpp", [a.core_library]),
        (
            "cli",
            artifacts / "main.cpp",
            [a.io_library, a.config_library, a.core_library, a.yaml_library],
        ),
    ):
        command = [
            a.compiler,
            "-std=c++20",
            *includes,
            str(probe),
        ]
        if label == "api":
            command += [str(artifacts / "generation.o")]
        else:
            # The CLI's diff subcommand lives beside main.cpp.
            command += ["-I" + str(source / "cli"), str(source / "cli/diff.cpp")]
        command += libraries
        command += ["-o", str(artifacts / label)]
        compiled = process(command, artifacts, label + "-compile")
        assert compiled.returncode == 0, compiled.stderr
    faulty_api = process([artifacts / "api", "artifacts"], artifacts, "api-fault")
    assert (
        faulty_api.returncode == 1
        and "CHECK_FAIL ARTIFACT_FAILURE_EMPTY" in faulty_api.stderr
    )
    # Same parsed configuration reaches the generation rejection in the CLI.
    (artifacts / "input.yaml").write_text("""schema_version: 10
name: failure_contract
output: {backend: verilog, language: verilog_2001}
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators: [{name: cpu, clock_domain: main, protocol: apb4, data_width: 32}]
targets:
  - {name: registers, clock_domain: main, protocol: apb4, attributes: amba, data_width: 32,
     mappings: [{base: "0", size: "4096"}]}
connectivity: all
""")

    def cli_result(executable, label):
        output = artifacts / (label + "-output")
        result = process(
            [
                executable,
                "generate",
                artifacts / "input.yaml",
                "--target",
                "unsupported",
                "--out-dir",
                output,
            ],
            artifacts,
            label,
        )
        if label == "cli-baseline":
            assert "NCL_UNSUPPORTED_BACKEND" in result.stderr, result.stderr
        return result.returncode, list(output.rglob("*")) if output.exists() else []

    normal_cli = cli_result(Path(a.build).resolve() / "nocloom", "cli-baseline")
    faulty_cli = cli_result(artifacts / "cli", "cli-fault")
    witness(
        "ARTIFACT_FAILURE_EMPTY",
        normal_cli,
        faulty_cli,
        lambda value: require(value[0] != 0 and not value[1], "ARTIFACT_FAILURE_EMPTY"),
        artifacts,
        "isolated API and CLI unsupported-backend guards independently removed; false success rejected",
    )
    (root / "facility-results.json").write_text(json.dumps(records, indent=2) + "\n")
    (work / "latest.txt").write_text(str(root) + "\n")


if __name__ == "__main__":
    main()
