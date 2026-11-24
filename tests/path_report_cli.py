"""Check the CLI path report: the flag adds one file and one bundle entry, and an unmet plan is
reported without failing the generation. A network with separate read and write paths reports
the links and paths of each direction. --separate-examples runs every example with
`read_write: separate` instead: each one gives a report or is rejected with a located diagnostic.
--jobs checks that many examples at once.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import re
from pathlib import Path
import subprocess
import tempfile

TIMING = """link_timing:
  periods: [{clock_domain: main, period_ps: 1000}]
  stage: {launch_ps: 40, capture_ps: 30, forward_pass_ps: 20, ready_pass_ps: 10}
  latency_limits:
    - {initiator: s0, target: t0, request_cycles: 0}
"""


def generate(cli, definition, out, *flags):
    run = subprocess.run([cli, "generate", str(definition), "--out-dir", str(out), *flags],
                         capture_output=True, text=True, timeout=120, check=False)
    assert run.returncode == 0, run.stderr
    return run.stdout, {p.name: p.read_text() for p in sorted(out.iterdir())}


def check_directions(body, expected):
    """Every link and path names what it carries, and a path stays on links of its direction."""
    carries = {link["link"]: link["carries"] for link in body["links"]}
    assert len(carries) == len(body["links"]), "A link is listed twice"
    assert set(carries.values()) == expected, sorted(set(carries.values()))
    assert {path["carries"] for path in body["paths"]} == expected
    for path in body["paths"]:
        for link in path["links"]:
            assert carries[link] in ("both", path["carries"]), (path["carries"], link)


def separate(text):
    """The definition with separate read and write paths, whatever it sets now."""
    text, set_before = re.subn(r"^read_write:.*$", "read_write: separate", text, flags=re.M)
    return text if set_before else text + "read_write: separate\n"


def separate_example(cli, example, work):
    """One example with separate paths: True for a report of both directions, False for a located
    rejection."""
    definition = work / example.name
    definition.write_text(separate(example.read_text()))
    out = work / example.stem
    run = subprocess.run([cli, "generate", str(definition), "--out-dir", str(out),
                          "--path-report"],
                         capture_output=True, text=True, timeout=120, check=False)
    assert "NCL_INTERNAL" not in run.stdout + run.stderr, (example.name, run.stderr)
    if run.returncode:
        # Options that separate paths do not support (docs/read-write-paths.md).
        assert run.returncode == 1 and "NCL_READ_WRITE_PATHS [" in run.stderr, (
            example.name, run.stderr)
        assert not out.exists() or not any(out.iterdir()), example.name
        return False
    report, = out.glob("*.path_report.json")
    check_directions(json.loads(report.read_text()), {"writes", "reads"})
    validated = subprocess.run([cli, "validate", str(definition), "--path-report"],
                               capture_output=True, text=True, timeout=120, check=False)
    assert validated.returncode == 0, (example.name, validated.stderr)
    return True


def separate_examples(cli, examples, work, jobs):
    """Every example with separate paths: a report of both directions, or a located rejection."""
    names = sorted(examples.glob("*.yaml"))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(lambda example: separate_example(cli, example, work), names))
    reported = [e.stem for e, ok in zip(names, results) if ok]
    rejected = [e.stem for e, ok in zip(names, results) if not ok]
    assert len(reported) >= 30 and len(reported) > 5 * len(rejected), (reported, rejected)
    print(f"path report with separate paths: {len(reported)} examples reported, "
          f"{len(rejected)} rejected ({', '.join(rejected)})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--examples", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--separate-examples", action="store_true")
    parser.add_argument("--jobs", type=int, default=1)
    args = parser.parse_args()
    root = Path(args.work).resolve()
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nocloom-path-report-", dir=root))
    if args.separate_examples:
        separate_examples(args.cli, Path(args.examples), work, args.jobs)
        return
    example = Path(args.examples) / "axi4_graph.yaml"
    _, plain = generate(args.cli, example, work / "plain")
    _, report = generate(args.cli, example, work / "report", "--path-report")
    added = set(report) - set(plain)
    assert added == {"nocloom_graph.path_report.json"}, added
    for name, text in plain.items():
        if name.endswith(".bundle.json"):
            bundle = json.loads(report[name])
            files = [f for f in bundle["files"] if f["role"] != "path_report"]
            assert len(files) + 1 == len(bundle["files"])
            assert {**bundle, "files": files} == json.loads(text)
        else:
            assert report[name] == text, name
    body = json.loads(report["nocloom_graph.path_report.json"])
    assert body["plan"]["status"] == "none"
    check_directions(body, {"both"})
    # Separate paths: each link and each path once for the writes and once for the reads.
    apart = work / "separate.yaml"
    apart.write_text(separate(example.read_text()))
    _, files = generate(args.cli, apart, work / "separate", "--path-report")
    both = json.loads(files["nocloom_graph.path_report.json"])
    check_directions(both, {"writes", "reads"})
    assert len(both["links"]) == 2 * len(body["links"])
    assert len(both["paths"]) == 2 * len(body["paths"])
    limited = work / "limited.yaml"
    link = "{from: merge, to: branch, transport_word_bytes: 4}"
    piped = "{from: merge, to: branch, transport_word_bytes: 4, pipeline: {slice: forward}}"
    assert link in example.read_text()
    limited.write_text(example.read_text().replace(link, piped) + TIMING)
    stdout, files = generate(args.cli, limited, work / "limited", "--path-report")
    assert "Stage plan limits unmet: 1, see nocloom_graph.path_report.json" in stdout, stdout
    plan = json.loads(files["nocloom_graph.path_report.json"])["plan"]
    assert plan["unmet"][0]["field"] == "link_timing.latency_limits[0].request_cycles", plan
    # validate reports the same count and writes nothing.
    for definition, lines in ((limited, ["Definition is valid", "Stage plan limits unmet: 1"]),
                              (example, ["Definition is valid"])):
        run = subprocess.run([args.cli, "validate", str(definition), "--path-report"],
                             capture_output=True, text=True, timeout=120, check=False)
        assert run.returncode == 0 and run.stdout.splitlines() == lines, (run.stdout, run.stderr)
    print("path report CLI checks passed")


if __name__ == "__main__":
    main()
