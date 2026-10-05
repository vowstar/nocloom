"""Generate every shipped example, then simulate the feature examples."""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--config-case", required=True)
    parser.add_argument("--traffic-case", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    work = Path(args.work)
    config = subprocess.run([
        sys.executable, str(here / "config.py"), "--cli", args.cli,
        "--case", args.config_case, "--work", str(work / "config")])
    if config.returncode:
        return config.returncode
    env = os.environ.copy()
    env["NOCLOOM_EXAMPLE_CONFIG_ROOT"] = (work / "config/latest.txt").read_text().strip()
    traffic = subprocess.run([
        sys.executable, str(here / "nlverif/run.py"), "--case", args.traffic_case,
        "--cli", args.cli, "--fixture", args.fixture, "--work", str(work / "traffic")], env=env)
    return traffic.returncode


if __name__ == "__main__":
    sys.exit(main())
