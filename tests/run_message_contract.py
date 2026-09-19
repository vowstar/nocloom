"""Check message description rules against single faults in isolated sources."""

import argparse, difflib, hashlib, json, shlex, subprocess, tempfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    for key in ("source", "compiler", "fixture", "work"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--include-dir", action="append", required=True)
    a = p.parse_args()
    source = Path(a.source).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    records = []
    original = (source / "src/core/message.cpp").read_text()
    faults = [
        ("MESSAGE_FIELD_SET", "{Kind::Write, 1}", "{static_cast<Kind>(999), 1}"),
        ("MESSAGE_BE_DERIVED", "parameters.data_bits / 8", "4"),
        (
            "MESSAGE_SOURCE_WIDTH",
            "std::bit_width(parameters.source_count - 1)",
            "std::bit_width(parameters.source_count) - 1",
        ),
        ("MESSAGE_SOURCE_OMISSION", "if (source_bits != 0)", "if (true)"),
        (
            "MESSAGE_TOTAL_WIDTH",
            "fields.end(), std::uint64_t{0}",
            "fields.end(), unsigned{0}",
        ),
        ("MESSAGE_FIXED_WIDTH.WRITE", "{Kind::Write, 1}", "{Kind::Write, 2}"),
        (
            "MESSAGE_FIXED_WIDTH.PROTECTION",
            "{Kind::Protection, 3}",
            "{Kind::Protection, 2}",
        ),
        ("MESSAGE_FIXED_WIDTH.RESULT", "{Kind::Result, 2}", "{Kind::Result, 1}"),
        (
            "MESSAGE_DATA_ADDRESS_WIDTH.ADDRESS",
            "{Kind::Address, parameters.address_bits}",
            "{Kind::Address, 32}",
        ),
        (
            "MESSAGE_DATA_ADDRESS_WIDTH.WRITE_DATA",
            "{Kind::WriteData, parameters.data_bits}",
            "{Kind::WriteData, 32}",
        ),
        (
            "MESSAGE_DATA_ADDRESS_WIDTH.READ_DATA",
            "{Kind::ReadData, parameters.data_bits}",
            "{Kind::ReadData, 32}",
        ),
        (
            "MESSAGE_PARAMETER_FAILURE.ADDRESS",
            "if (parameters.address_bits == 0)",
            "if (false)",
        ),
        (
            "MESSAGE_PARAMETER_FAILURE.SOURCE",
            "if (parameters.source_count == 0)",
            "if (false)",
        ),
        (
            "MESSAGE_PARAMETER_FAILURE.DATA_MIN",
            "parameters.data_bits < 8 || !std::has_single_bit(parameters.data_bits)",
            "!std::has_single_bit(parameters.data_bits)",
        ),
        (
            "MESSAGE_PARAMETER_FAILURE.DATA_POWER",
            "parameters.data_bits < 8 || !std::has_single_bit(parameters.data_bits)",
            "parameters.data_bits < 8",
        ),
        (
            "MESSAGE_PARAMETER_FAILURE.NO_VALUE",
            "return Result<MessageLayout>::failure(std::move(errors));",
            "return Result<MessageLayout>::success({});",
        ),
    ]
    for name, old, new in faults:
        d = root / name
        d.mkdir()
        assert original.count(old) == 1
        mutant = original.replace(old, new)
        (d / "message.cpp").write_text(mutant)
        r = subprocess.run(
            [str(Path(a.fixture).resolve()), name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        (d / "baseline.log").write_text(r.stdout + r.stderr)
        assert r.returncode == 0 and "CHECK_PASS " + name in r.stdout
        command = [
            a.compiler,
            "-std=c++20",
            *["-I" + directory for directory in a.include_dir],
            str(source / "tests/message_contract.cpp"),
            str(d / "message.cpp"),
            "-o",
            str(d / "check"),
        ]
        (d / "compile-command.txt").write_text(shlex.join(command) + "\n")
        r = subprocess.run(command, capture_output=True, text=True, timeout=30)
        (d / "compile.log").write_text(r.stdout + r.stderr)
        assert r.returncode == 0, (name, r.stderr)
        r = subprocess.run(
            [str(d / "check"), name], capture_output=True, text=True, timeout=10
        )
        (d / "fault.log").write_text(r.stdout + r.stderr)
        assert r.returncode == 1 and "CHECK_FAIL " + name in r.stdout, (
            name,
            r.stdout,
            r.stderr,
        )
        (d / "fault.diff").write_text(
            "".join(
                difflib.unified_diff(original.splitlines(True), mutant.splitlines(True))
            )
        )
        records.append(
            dict(
                name=name,
                status="CAUGHT",
                baseline="PASS",
                directory=str(d),
                source_sha256=hashlib.sha256(original.encode()).hexdigest(),
                fault_sha256=hashlib.sha256(mutant.encode()).hexdigest(),
                exit=r.returncode,
            )
        )
        (root / "message-results.json").write_text(json.dumps(records, indent=2) + "\n")
    (work / "latest.txt").write_text(str(root) + "\n")


if __name__ == "__main__":
    main()
