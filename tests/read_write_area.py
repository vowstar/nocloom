"""Check the area table of docs/read-write-paths.md against a synthesis of each listed example.

Each example is generated with `read_write: shared` and `read_write: separate` and synthesized
with the command of the page. Flip-flops are the one-bit DFF and DFFE cells of `stat`; mapped cells
are all cells but `$scopeinfo`. The flip-flop counts are compared with every Yosys. The mapped cell
counts depend on the mapper, so they are compared with the Yosys build that the page names and
reported as not compared with another build.
"""
import argparse
import concurrent.futures
import re
import subprocess
import tempfile
from pathlib import Path

ROW = re.compile(r"^\| `(\w+\.yaml)` \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$", re.M)
BUILD = re.compile(r"with Yosys (\S+), build `([0-9a-f]+)`")
COMMAND = "read_verilog *.v; synth -auto-top -flatten; stat"


def table(page):
    text = page.read_text()
    rows = {name: tuple(map(int, numbers)) for name, *numbers in ROW.findall(text)}
    assert len(rows) >= 7, "The area table has fewer rows than the page lists"
    assert f"yosys -p '{COMMAND}'" in text, "The page gives another synthesis command"
    build = BUILD.search(text)
    assert build, "The page does not name its Yosys build"
    return rows, build.groups()


CELL = re.compile(r"^\s+(?:(\d+)\s+(\$\S+)|(\$\S+)\s+(\d+))$", re.M)


def counts(log):
    """(flip-flops, mapped cells) of the last `stat` of a Yosys log."""
    report = log[log.rindex("Printing statistics."):]
    assert report.count("\n=== ") == 1, "The flattened design has more than one module"
    cells = {}
    for count, cell, other_cell, other_count in CELL.findall(report):
        name = cell or other_cell
        assert name not in cells, name
        cells[name] = int(count or other_count)
    assert cells, "No cell counts in the Yosys report"
    flops = sum(n for cell, n in cells.items() if re.fullmatch(r"\$_[A-Z]*DFF[A-Z]*_[A-Z01]+_", cell))
    return flops, sum(n for cell, n in cells.items() if cell != "$scopeinfo")


def measure(cli, yosys, example, mode, work):
    case = work / f"{example.stem}_{mode}"
    case.mkdir()
    text = example.read_text()
    assert "\nread_write:" not in text, example.name
    (case / "definition.yaml").write_text(text + f"read_write: {mode}\n")
    subprocess.run([cli, "generate", str(case / "definition.yaml"), "--out-dir", str(case / "rtl")],
                   check=True, capture_output=True, timeout=300)
    run = subprocess.run([yosys, "-p", COMMAND], cwd=case / "rtl", capture_output=True, text=True,
                         timeout=1500, check=False)
    (case / "yosys.log").write_text(run.stdout + run.stderr)
    assert run.returncode == 0, run.stderr[-2000:]
    return counts(run.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--yosys", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    rows, (version, build) = table(args.source / "docs/read-write-paths.md")
    banner = subprocess.run([args.yosys, "-V"], capture_output=True, text=True, check=True).stdout
    same_build = f"Yosys {version} " in banner and f"git sha1 {build}" in banner
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work.resolve()))
    cases = [(name, mode) for name in rows for mode in ("shared", "separate")]
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        measured = dict(zip(cases, pool.map(
            lambda case: measure(args.cli, args.yosys, args.source / "examples" / case[0],
                                 case[1], work), cases)))
    failures = []
    for name, (flops_shared, flops_separate, cells_shared, cells_separate) in rows.items():
        shared, separate = measured[name, "shared"], measured[name, "separate"]
        print(f"{name}: flip-flops {shared[0]} {separate[0]}, mapped cells {shared[1]} {separate[1]}")
        if (shared[0], separate[0]) != (flops_shared, flops_separate):
            failures.append(f"{name}: the page has {flops_shared} and {flops_separate} flip-flops")
        if same_build and (shared[1], separate[1]) != (cells_shared, cells_separate):
            failures.append(f"{name}: the page has {cells_shared} and {cells_separate} mapped cells")
    assert not failures, "\n".join(failures)
    if same_build:
        print(f"READ_WRITE_AREA_PASS {len(rows)} examples, flip-flops and mapped cells")
    else:
        print(f"READ_WRITE_AREA_PASS {len(rows)} examples, flip-flops only")
        print(f"NOT COMPARED: mapped cells (the page names Yosys {version} build {build}, "
              f"this is {banner.strip()})")


if __name__ == "__main__":
    main()
