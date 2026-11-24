"""Check the CLI path report: the flag adds one file and one bundle entry, and an unmet plan is
reported without failing the generation."""

import argparse
import json
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--examples", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()
    root = Path(args.work).resolve()
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="nocloom-path-report-", dir=root))
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
