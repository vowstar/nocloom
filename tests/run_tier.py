"""Run a development or release test tier in a configured CTest build."""
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build")
    parser.add_argument("tier", choices=("edit", "precommit", "ci", "release"))
    parser.add_argument("-j", "--jobs", type=int)
    parser.add_argument("--area", help="CTest name regex for additional focused tests")
    args = parser.parse_args()
    if args.area and args.tier != "precommit":
        parser.error("--area requires precommit")
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be positive")

    command = ["ctest", "--test-dir", args.build, "--output-on-failure", "--no-tests=error"]
    if args.jobs is not None:
        command += ["-j", str(args.jobs)]
    selection = [] if args.tier == "release" else ["-L", f"^{args.tier}$"]
    status = subprocess.call(command + selection)
    if status == 0 and args.area:
        status = subprocess.call(command + [
            "-L", "^focused$", "-R", args.area, "-LE", "^(precommit|release)$",
        ])
    return status


if __name__ == "__main__":
    raise SystemExit(main())
