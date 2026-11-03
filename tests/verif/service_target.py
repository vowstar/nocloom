"""Service target on the APB network: the bench, then one run per fault.

The fixture builds a network from a YAML definition with one target serving the error logger's
register bank. The unmodified netlist must pass; each designated fault of the fault build
(selection zero passes too) must fail on a check that names it.

  python3 tests/verif/service_target.py --fixture FIXTURE --bench sv/nlv_service_tb.sv
          --work DIR [--sim ENGINE] [--sim-config JSON]

Exit code: 0 every run met its expectation, 1 a run did not, 2 infrastructure error.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.dont_write_bytecode = True

import sim_engine  # noqa: E402
from nlverif import faults as fl  # noqa: E402

KEY = "burst_service/service/"


def replace(at, **with_):
    return dict(component="network", op="replace", at={"binding": KEY + at}, **{"with": with_})


def definition(sources, data_target, pipes):
    source_names = [f"m{k}" for k in range(sources)]
    names = source_names + (["data"] if data_target else [])
    text = ["schema_version: 10", "name: service", "address_width: 32",
            "reset_ports: [{name: reset_n}]", "test_ports: [{name: test}]",
            "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]",
            "initiators:"]
    text += [f"  - {{name: {n}, clock_domain: main, protocol: apb4, data_width: 32}}" for n in names]
    text += ["targets:",
             "  - {name: service, clock_domain: main, protocol: apb4, data_width: 32, "
             "address_width: 20, attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}"]
    if data_target:
        text.append("  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, "
                    "address_width: 16, attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}")
    if pipes:
        nodes = ", ".join(["{name: q}"] + [f"{{name: l{k}}}" for k in range(sources)])
        links = ", ".join(f"{{from: l{k}, to: q, pipeline: {{forward: true}}}}" for k in range(sources))
        text += ["topology:", "  request:", f"    nodes: [{nodes}]", f"    links: [{links}]",
                 "    entries:"]
        text += [f"      - {{endpoint: m{k}, node: l{k}}}" for k in range(sources)]
        text += ["    exits:", "      - {endpoint: service, node: q}", "  response:",
                 "    nodes: [{name: p}]", "    links: []", "    entries:",
                 "      - {endpoint: service, node: p}", "    exits:"]
        text += [f"      - {{endpoint: m{k}, node: p}}" for k in range(sources)]
    text.append("connectivity:")
    text += [f"  {n}: [service]" for n in source_names]
    if data_target:
        text.append("  data: [ram]")
    text.append("output: {backend: verilog, language: verilog_2001}")
    return "\n".join(text) + "\n"


# Each configuration: its network, bench define and the faults with the checks that may name them.
CONFIGS = [
    dict(label="single", define="SINGLE", yaml=definition(1, True, False), faults=[
        dict(id="serial_late", expect=("read_beats", "write_beats", "outside_beats"),
             **replace("bus.serving", op="BitAnd", args=[{"self": True},
                                                         {"delay": {"self": True}}])),
        dict(id="write_short", expect=("timeout", "write_stored", "write_beats"),
             **replace("bus.at_last", select={"binding": KEY + "bus.step_three"})),
        dict(id="outside_inside", expect=("outside_beats", "outside_error"),
             **replace("bus.inside", const=1)),
        dict(id="read_bytes_low", expect=("read_value",),
             **replace("bus.read_word", op="BitAnd", args=[{"self": True}, {"const": 0xff}])),
        dict(id="miss_at_bank", expect=("miss_beats", "miss_error"),
             **replace("bus.miss", const=0)),
    ]),
    dict(label="two_sources", define="TWO", yaml=definition(2, False, True), faults=[
        dict(id="owner_swapped", expect=("timeout", "owner_data"), **replace("bus.source", add=2)),
    ]),
]


def offsets(artifacts):
    """The bank's offsets, from the map."""
    network = json.loads(next(artifacts.glob("*.map.json")).read_text())
    registers = network["definition"]["log_registers"]["service"]["registers"]
    fields = {r["name"]: r for r in registers}
    wanted = dict(IDENTITY="identity_type", CONTROL="alarm_enable", HOLD="serial_hold_when_full",
                  STATUS="record_valid", RECORD_HEAD="record_write")
    lines = [f"`define OFFSET_{k} {fields[v]['offset']}\n" for k, v in wanted.items()]
    return "".join(lines)


def run(work, args):
    results = dict(schema=1, case="service_target", runs=[], work=str(work))
    good = True
    for config in CONFIGS:
        root = work / config["label"]
        gen = root / "gen"
        root.mkdir()
        (root / "network.yaml").write_text(config["yaml"])
        spec = root / "faults.spec.json"
        spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                                for f in config["faults"]])) + "\n")
        subprocess.run([args.fixture, "service_network", str(gen), "--param",
                        f"config={root / 'network.yaml'}", "--param", "service=service",
                        "--param", f"artifacts={root / 'artifacts'}",
                        "--faults", str(spec)], check=True)
        bench = (f"`define {config['define']}\n" + offsets(root / "artifacts")
                 + Path(args.bench).read_text())
        cells = (gen / "cells.v").read_text() if (gen / "cells.v").exists() else ""
        plain = root / "plain"
        plain.mkdir()
        (plain / "tb.sv").write_text(bench)
        (plain / "dut.v").write_text((gen / "network.v").read_text() + cells)
        faulted = root / "faulted"
        faulted.mkdir()
        (faulted / "tb.sv").write_text(bench)
        components = json.loads((gen / "ports.json").read_text())
        (faulted / "dut.v").write_text(
            (gen / "faulted.v").read_text() + cells
            + fl.wrapper(components, json.loads((gen / "faults.json").read_text())))

        def compile_(where):
            return sim_engine.Simulation(where, "dut.v", engine=args.sim, config=args.sim_config,
                                       compile_timeout=300, run_timeout=120)

        def judge(sim, name, options, expect):
            result = sim.run(options)
            failed = sorted({line.split()[1] for line in result.stdout.splitlines()
                             if line.startswith("CHECK_FAIL ")})
            if expect is None:
                ok = result.returncode == 0 and not failed
            else:
                ok = result.returncode != 0 and bool(set(failed) & set(expect))
            results["runs"].append(dict(run=name, ok=ok, failed=failed[:12],
                                        exit=result.returncode))
            print(f"{name}: {'OK' if ok else 'BAD'} failed={failed[:6]}")
            return ok

        label = config["label"]
        good &= judge(compile_(plain), f"{label}/baseline", (), None)
        sim = compile_(faulted)
        results["engine"] = sim.engine
        good &= judge(sim, f"{label}/fault_build_selection_0", ("+NLV_FAULT=0",), None)
        for k, fault in enumerate(config["faults"], 1):
            good &= judge(sim, f"{label}/{fault['id']}", (f"+NLV_FAULT={k}",), fault["expect"])
    results["exit"] = 0 if good else 1
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--bench", required=True)
    p.add_argument("--work", required=True)
    sim_engine.add_arguments(p)
    args = p.parse_args()
    top = Path(args.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    try:
        results = run(work, args)
    except Exception:  # generation, compilation or simulation
        results = dict(schema=1, case="service_target", exit=2, error=traceback.format_exc())
        print(results["error"])
    (top / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    print("SERVICE_TARGET", "PASS" if results["exit"] == 0 else "FAIL")
    return results["exit"]


if __name__ == "__main__":
    sys.exit(main())
