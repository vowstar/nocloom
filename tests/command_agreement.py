"""Check that validate and generate give the same verdict on boundary definitions."""

import argparse
from itertools import combinations
import json
from pathlib import Path
import re
import subprocess
import tempfile


def definition(target_domain="main", log_domain=None, *, window_domain="main",
               periods=False, bank_offset="0x0", whole_record=False, related=False):
    target = dict(name="ram", clock_domain=target_domain, protocol="apb4", data_width=32,
                  attributes="amba", mappings=[dict(base="0x0", size="0x1000")])
    window = dict(name="csr", base="0x100000", size="0x1000", clock_domain=window_domain)
    log = dict(name="errors", watch=["ram"], whole_record=whole_record,
               loggers=[dict(offset=bank_offset)])
    if log_domain is not None:
        log["clock_domain"] = log_domain

    def graph(side, endpoints):
        rows = list(zip(endpoints, (window_domain, target_domain), strict=True))
        return dict(nodes=[dict(name=f"{side}{i}", clock_domain=domain)
                           for i, (_, domain) in enumerate(rows)],
                    links=[],
                    entries=[dict(endpoint=pair[0], node=f"{side}{i}")
                             for i, (pair, _) in enumerate(rows)],
                    exits=[dict(endpoint=pair[1], node=f"{side}{i}")
                           for i, (pair, _) in enumerate(rows)])

    domains = sorted({window_domain, target_domain, log_domain} - {None})
    clocks = [dict(name=domain, clock_port=f"clk_{domain}", reset_port="rst_n", test_port="test")
              for domain in domains]
    if periods:
        for index, clock in enumerate(clocks):
            clock["period_ps"] = 1000 * (index + 1)
    roots = []
    if related:
        # Every other domain takes each second edge of the window's clock.
        roots = [dict(name="root", clock_port=f"clk_{window_domain}")]
        for clock in clocks:
            clock.update(clock_root="root", clock_port=f"clk_{window_domain}")
            if clock["name"] != window_domain:
                clock.update(edge_parent=window_domain, ratio=2)
    return dict(schema_version=10, name="logged", address_width=32,
                reset_ports=[dict(name="rst_n")], test_ports=[dict(name="test")],
                clock_roots=roots, clock_domains=clocks,
                initiators=[dict(name=name, clock_domain=domain, protocol="apb4", data_width=32)
                            for name, domain in (("cfg", window_domain), ("data", target_domain))],
                targets=[target], error_logging=dict(registers=window, logs=[log]),
                connectivity=dict(cfg=["csr"], data=["ram"]),
                topology=dict(request=graph("q", [("cfg", "csr"), ("data", "ram")]),
                              response=graph("r", [("csr", "cfg"), ("ram", "data")])),
                output=dict(backend="verilog", language="verilog_2001"))


def check(cli, work, name, config, diagnostic=None, flags=()):
    input_path, output_path = work / f"{name}.yaml", work / name
    input_path.write_text(json.dumps(config), encoding="ascii")
    validated = subprocess.run([cli, "validate", input_path, *flags], capture_output=True, text=True)
    if output_path.exists():
        raise AssertionError(f"{name}: validation produced artifacts")
    generated = subprocess.run([cli, "generate", input_path, "--out-dir", output_path, *flags],
                               capture_output=True, text=True)
    expected = int(diagnostic is not None)
    if (validated.returncode, generated.returncode) != (expected, expected):
        raise AssertionError(f"{name}: validate={validated.returncode} {validated.stderr}"
                             f"generate={generated.returncode} {generated.stderr}")
    if validated.stderr != generated.stderr:
        raise AssertionError(f"{name}: different diagnostics\n"
                             f"validate: {validated.stderr}\ngenerate: {generated.stderr}")
    if diagnostic:
        if diagnostic not in validated.stderr:
            raise AssertionError(f"{name}: expected {diagnostic}, got {validated.stderr}")
        if (diagnostic.startswith(("NCL_C_HEADER_", "NCL_IPXACT_"))
                or diagnostic == "NCL_ADDRESS_CONDITION"):
            location = rf"{re.escape(str(input_path))}:1:[1-9][0-9]*: {diagnostic} "
            if not re.match(location, validated.stderr):
                raise AssertionError(f"{name}: missing diagnostic location in {validated.stderr}")
        if output_path.exists():
            raise AssertionError(f"{name}: rejected definition produced artifacts")
        return
    top = f"nocloom_{config['name']}"
    if not (output_path / f"{top}.v").is_file():
        raise AssertionError(f"{name}: missing generated Verilog")
    for flag, suffixes in OUTPUTS.items():
        for suffix in suffixes:
            if bool(list(output_path.glob(f"{top}*{suffix}"))) != (flag in flags):
                raise AssertionError(f"{name}: unexpected artifact set for {flag}")


OUTPUTS = {
    "--cdc-constraints": (".cdc.sdc", ".cdc_preserve.txt"),
    "--c-header": (".addresses.h",),
    "--address-table": (".address_table.json", ".address_table.csv"),
    "--ipxact": (".ipxact.xml",),
}


def output_definition(*, sources=("s",), target="t", widths=(), crossing=False):
    target_domain = "slow" if crossing else "m"
    domains = [dict(name="m", clock_port="clk", reset_port="rst_n", test_port="test")]
    config = dict(schema_version=10, name="n", address_width=16,
                  reset_ports=[dict(name="rst_n")], test_ports=[dict(name="test")],
                  clock_domains=domains,
                  initiators=[dict(name=name, clock_domain="m", protocol="apb4", data_width=32)
                              for name in sources],
                  targets=[dict(name=target, clock_domain=target_domain, protocol="apb4",
                                attributes="amba", data_width=32,
                                mappings=[dict(base="0", size="4096")])],
                  connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    if widths:
        config["address_inputs"] = [dict(name=f"b{i}", width=width, clock_domain="m")
                                    for i, width in enumerate(widths)]
        config["address_selectors"] = [dict(name="alt", equals={f"b{i}": 1
                                                                for i in range(len(widths))})]
        config["targets"][0]["mappings"][0]["when"] = {"alt": False}
    if crossing:
        domains.append(dict(name="slow", clock_port="clk_slow", reset_port="rst_n",
                            test_port="test"))
        config["synchronizer_stages"] = 2
        nodes = [dict(name="a", clock_domain="m"), dict(name="b", clock_domain="slow")]
        entries = [dict(endpoint=source, node="a") for source in sources]
        exits = [dict(endpoint=target, node="b")]
        config["topology"] = dict(
            request=dict(nodes=nodes, links=[{"from": "a", "to": "b"}],
                         entries=entries, exits=exits),
            response=dict(nodes=nodes, links=[{"from": "b", "to": "a"}],
                          entries=exits, exits=entries))
    return config


def output_cases():
    cdc = {"--cdc-constraints": "NCL_UNSUPPORTED_BACKEND"}
    ipxact = {"--ipxact": "NCL_ADDRESS_CONDITION"}
    collision = {"--c-header": "NCL_C_HEADER_NAME"}
    ipxact_collision = {"--ipxact": "NCL_IPXACT_NAME"}
    reserved = {"--c-header": "NCL_C_HEADER_RESERVED"}
    return [
        ("no_crossing", output_definition(), cdc),
        ("two_bit_selector", output_definition(widths=(2,)), cdc | ipxact),
        ("two_port_selector", output_definition(widths=(1, 1)), cdc | ipxact),
        ("case_collision", output_definition(sources=("s", "S")), cdc | collision),
        ("shared_endpoint_name", output_definition(sources=("t",)), cdc | ipxact_collision),
        ("reserved_macro", output_definition(target="t_"), cdc | reserved),
        ("crossing", output_definition(crossing=True, widths=(1,)), {}),
        ("crossing_two_bit", output_definition(crossing=True, widths=(2,)), ipxact),
        ("crossing_reserved", output_definition(crossing=True, target="t_", widths=(2,)),
         reserved | ipxact),
    ]


def check_output_options(cli, work, name, config, errors):
    for count in range(len(OUTPUTS) + 1):
        for flags in combinations(OUTPUTS, count):
            diagnostic = next((errors[flag] for flag in flags if flag in errors), None)
            check(cli, work, name + "_" + "_".join(flag[2:] for flag in flags),
                  config, diagnostic, flags)


def check_clock_domains(cli, work):
    check(cli, work, "local", definition())
    check(cli, work, "separate_log", definition(log_domain="slow", periods=True))
    check(cli, work, "related_domain", definition("slow", related=True))
    for domain, name in ((None, "implicit_domain"), ("main", "explicit_domain")):
        check(cli, work, name, definition("slow", domain), "NCL_EXCHANGE_CLOCK_BINDING")
    check(cli, work, "bad_bank", definition(bank_offset="0x1000"),
          "NCL_UNSUPPORTED_TOPOLOGY")
    check(cli, work, "whole_record_crossing",
          definition(log_domain="slow", periods=True, whole_record=True),
          "NCL_UNSUPPORTED_TOPOLOGY")


def main():
    cases = output_cases()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--case", choices=["clock_domains", *(name for name, _, _ in cases)])
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=args.work))
    if args.case in (None, "clock_domains"):
        check_clock_domains(args.cli, work)
    for name, config, errors in cases:
        if args.case in (None, name):
            check_output_options(args.cli, work, name, config, errors)


if __name__ == "__main__":
    main()
