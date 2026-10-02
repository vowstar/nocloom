"""Exercise the formal entry point with injected solver results, without a proof."""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def check_driver(driver, fixture, work):
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="control-", dir=work))
    cases = [
        ("end_to_end_prove", "PASS", 0, 0),
        ("end_to_end_prove", "FAIL", 2, 1),
        ("end_to_end_prove", "ERROR", 2, 1),
        ("end_to_end_prove", "ERROR", 16, 1),
        ("end_to_end_prove", "UNKNOWN", 4, 1),
        ("end_to_end_prove", "TIMEOUT", "timeout", 1),
        ("end_to_end_prove", "PASS", 2, 1),
        ("bmc", "FAIL", 2, 0),
        ("bmc", "ERROR", 2, 1),
        ("bmc", "ERROR", 16, 1),
        ("bmc", "FAIL", 1, 1),
        ("bmc", "PASS", 0, 1),
    ]
    receipts = []
    for stage, status, code, expected in cases:
        case = work / f"{stage}-{status}-{code}"
        case.mkdir()

        def injected(directory, *args, **kwargs):
            directory.mkdir()
            label = "GRAY_ONE_BIT_W" if "binary_count" in str(directory) else "NO_OVERFLOW"
            log = f"INJECTED_SBY_RESULT {status}; no proof ran\n"
            if status == "FAIL":
                log += f"Assert failed in nocloom_async_fifo: {label}\n"
            (directory / "run.log").write_text(log)
            return status, code, log

        argv = ["run_async_fifo_formal.py", "--fixture", fixture, "--work", str(case),
                "--shapes", "0,1", "--stages", stage]
        output = io.StringIO()
        with patch.object(driver, "sby", side_effect=injected), patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(output):
                result = driver.main()
        (case / "driver.log").write_text(output.getvalue())
        paths = list(case.glob("run-*/results.json"))
        assert len(paths) == 1, paths
        rows = json.loads(paths[0].read_text())
        expected_tasks = {((2, 2), "binary_count"), ((3, 3), "binary_count")}
        if stage == "bmc":
            expected_tasks.add(((2, 2), "full_late"))
        assert len(rows) == len(expected_tasks), rows
        assert {(tuple(row["shape"]), row["fault"]) for row in rows} == expected_tasks, rows
        assert all(row["accepted"] == (expected == 0) for row in rows), rows
        assert result == expected, (stage, status, code, result, expected)
        assert ("ASYNC_FIFO_FORMAL_PASS" in output.getvalue()) == (expected == 0)
        receipts.append(dict(stage=stage, status=status, solver_exit=code, driver_exit=result,
                             expected_exit=expected, tasks=len(rows), proof_executed=False))
    (work / "results.json").write_text(json.dumps(receipts, indent=2) + "\n")
    check_product_hold(driver, fixture, work)
    print(f"ASYNC_FIFO_DRIVER_PASS controls={len(receipts)}")


def check_product_hold(driver, fixture, work):
    """A paired proof requires its hold lemma to finish successfully."""
    generated = work / "product-inputs"
    generated.mkdir()
    jobs = driver.product_jobs(generated, fixture, driver.violated,
                               only="product_64_k1_uniform")
    cases = [("PASS", 0), ("ERROR", 16), ("UNKNOWN", 4),
             ("TIMEOUT", "timeout"), ("PASS", 2), ("FAIL", 2)]
    receipts = []
    for status, code in cases:
        case = work / f"product-{status}-{code}"
        case.mkdir()

        def product_jobs(root, *args, **kwargs):
            selected = []
            for row, directory, *rest in jobs:
                destination = root / directory.relative_to(generated)
                destination.parent.mkdir(parents=True, exist_ok=True)
                selected.append((row, destination, *rest))
            return selected

        def injected(directory, text, mode, engine, *args):
            directory.mkdir()
            if directory.name == "hold":
                # SBY supplies another -l toggle. Two toggles disable the
                # per-output deadlines while retaining CTG generalization.
                assert engine.split() == ["abc", "pdr", "-l", "-r", "-c", "-n"]
                return status, code, ""
            return "PASS", 0, ""

        argv = ["run_async_fifo_formal.py", "--fixture", fixture, "--work", str(case),
                "--shapes", "product", "--only", "product_64_k1_uniform",
                "--stages", "product_prove"]
        output = io.StringIO()
        with patch.object(driver, "product_jobs", side_effect=product_jobs), \
                patch.object(driver, "sby", side_effect=injected), patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(output):
                result = driver.main()
        rows = json.loads(next(case.glob("run-*/results.json")).read_text())
        accepted = (status, code) == ("PASS", 0)
        assert {r["stage"] for r in rows} == {"product_hold", "product_prove"}, rows
        assert all(r["accepted"] == accepted for r in rows), rows
        assert next(r for r in rows if r["stage"] == "product_prove")["hold_discharged"] == accepted
        assert result == (0 if accepted else 1), (status, code, result)
        (case / "driver.log").write_text(output.getvalue())
        receipts.append(dict(status=status, solver_exit=code, driver_exit=result,
                             accepted=accepted, proof_executed=False))
    (work / "product-results.json").write_text(json.dumps(receipts, indent=2) + "\n")
