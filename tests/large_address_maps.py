"""Generate sparse page mappings that all alias one target page."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def definition(count):
    mappings = "".join(
        f"      - {{base: '{index * 8192:#x}', size: '0x1000', local_address: '0x0'}}\n"
        for index in range(count)
    )
    return """schema_version: 10
name: mappings
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]
initiators: [{name: cfg, clock_domain: main, protocol: apb4, data_width: 32}]
targets:
  - name: ram
    clock_domain: main
    protocol: apb4
    data_width: 32
    address_width: 16
    attributes: amba
    mappings:
""" + mappings + """connectivity: all
output: {backend: verilog, language: verilog_2001}
"""


def check(cli, directory, count, stack_kib):
    config = directory / f"map_{count}.yaml"
    config.write_text(definition(count), encoding="ascii")
    output = directory / f"out_{count}"
    command = [str(cli), "generate", str(config), "--out-dir", str(output)]
    if stack_kib:
        command = ["sh", "-c", 'ulimit -s "$1" || exit; shift; exec "$@"', "sh",
                   str(stack_kib), *command]
    subprocess.run(command, check=True)
    with (output / "nocloom_mappings.map.json").open() as stream:
        mappings = json.load(stream)["definition"]["targets"]
    if len(mappings) != count or int(mappings[-1]["begin"]) != (count - 1) * 8192:
        raise AssertionError(f"Expected all {count} mappings in the generated map")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--count", type=int, nargs="+", required=True)
    parser.add_argument("--stack-kib", type=int, default=0)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(dir=args.work))
    for count in args.count:
        check(args.cli, directory, count, args.stack_kib)


if __name__ == "__main__":
    main()
