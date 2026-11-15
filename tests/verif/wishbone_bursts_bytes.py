"""Compare all artifacts for omitted and explicitly disabled Wishbone tags."""
import argparse
import copy
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "cases"))
from wishbone import configuration


def generate(cli, cfg, work):
    work.mkdir(parents=True)
    (work / "config.yaml").write_text(json.dumps(cfg))
    command = [cli, "generate", "config.yaml", "--out-dir", "out", "--ipxact",
               "--address-table", "--c-header"]
    if len(cfg["clock_domains"]) > 1:
        command.append("--cdc-constraints")
    result = subprocess.run(command, cwd=work, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode())
    files = {str(p.relative_to(work / "out")): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((work / "out").rglob("*")) if p.is_file()}
    return dict(stdout=result.stdout.hex(), stderr=result.stderr.hex(), files=files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    rows = []
    pairs = (("wishbone", "wishbone"), ("wishbone", "axi4"), ("axi4", "wishbone"))
    for i, ((source, target), unit, cycles, cdc) in enumerate(itertools.product(
            pairs, ("byte", "word"), ("classic", "pipelined"), (False, True))):
        cfg = configuration(source, target, unit, cdc)
        for role in ("initiators", "targets"):
            for endpoint in cfg[role]:
                if endpoint["protocol"] == "wishbone":
                    endpoint["cycles"] = cycles
        disabled = copy.deepcopy(cfg)
        for role in ("initiators", "targets"):
            for endpoint in disabled[role]:
                if endpoint["protocol"] == "wishbone":
                    endpoint["burst_tags"] = False
        omitted = generate(args.baseline or args.cli, cfg, work / str(i) / "omitted")
        explicit = generate(args.cli, disabled, work / str(i) / "false")
        if omitted != explicit:
            raise RuntimeError(f"artifact mismatch: {source} {target} {unit} {cycles} {cdc}")
        rows.append(dict(source=source, target=target, unit=unit, cycles=cycles, cdc=cdc,
                         files=len(omitted["files"])))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    print(json.dumps(dict(definitions=len(rows), unchanged=len(rows), changed=0,
                          files=sum(row["files"] for row in rows))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
