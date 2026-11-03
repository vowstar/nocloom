"""Register bank at its serial port: the bench, then one run per fault.

The fixture emits the banks with their register map; the map names every offset to the bench. The
unmodified netlist must pass; each designated fault of the fault build (selection zero passes too)
must fail on the check that names it, and so must each variant build of a changed plan.

  python3 tests/verif/register_bank.py --fixture FIXTURE --bench sv/nlv_register_bank_tb.sv
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


def replace(component, at, **with_):
    return dict(component=component, op="replace", at={"binding": at}, **{"with": with_})


# Fault, the bank it changes and the checks that may name it.
FAULTS = [
    dict(id="all_strobes", expect=("beat_strobe", "strobe_pin"),
         **replace("custom", "access.write_mask", const=0xffffffff)),
    dict(id="status_kept", expect=("response_status",),
         **replace("custom", "response.header", select={"input": "nl_request_data"})),
    dict(id="tail_early", expect=("write_tail_waits", "write_latency"),
         **replace("custom", "response.offer", select={"binding": "response.valid"})),
    dict(id="clear_always", expect=("clear_no_strobe",),
         **replace("logger", "field.record_clear_valid", const=1)),
]


REVISION = 0xA5C33C

# Builds of the fixture with a changed plan, and the checks that may name them. A changed field
# adds a port, so these are separate builds; the bench leaves that output unconnected.
VARIANTS = [
    dict(id="user_byte_writable", param="revision_reserved=writable", expect=("logger_user_byte",)),
    # A given revision: the register map states it and the bench reads it back. The bench of the
    # default build, which expects the generator version, fails on that netlist.
    dict(id="revision_given", param=f"revision={REVISION}", expect=None, revision=REVISION),
    dict(id="revision_given_default_bench", param=f"revision={REVISION}",
         expect=("logger_revision",), default_bench=True),
]


def defines(probes):
    """`define lines of each bank's fields from the register map."""
    out = []
    for label, component in probes.items():
        for group in component["groups"]:
            if group["kind"] != "register_map":
                continue
            for name, field in group["fields"].items():
                prefix = f"{label.upper()}_{name}"
                out.append(f"`define {prefix} 18'd{field['word']}")
                out.append(f"`define {prefix}_lsb {field['lsb']}")
                out.append(f"`define {prefix}_value {field['width']}'h{field['value']:x}")
    return "\n".join(out) + "\n"


def run(work, args):
    results = dict(schema=1, case="register_bank", runs=[], work=str(work))
    gen = work / "gen"
    spec = work / "faults.spec.json"
    spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                            for f in FAULTS])) + "\n")
    subprocess.run([args.fixture, "register_bank", str(gen), "--probes", "--faults", str(spec)],
                   check=True)
    bench = defines(json.loads((gen / "probes.json").read_text())) + Path(args.bench).read_text()
    plain = work / "plain"
    plain.mkdir()
    (plain / "tb.sv").write_text(bench)
    (plain / "dut.v").write_text((gen / "network.v").read_text())
    faulted = work / "faulted"
    faulted.mkdir()
    (faulted / "tb.sv").write_text(bench)
    components = json.loads((gen / "ports.json").read_text())
    (faulted / "dut.v").write_text(
        (gen / "faulted.v").read_text()
        + fl.wrapper(components, json.loads((gen / "faults.json").read_text())))

    def compile_(root):
        return sim_engine.Simulation(root, "dut.v", engine=args.sim, config=args.sim_config,
                                   compile_timeout=300, run_timeout=120)

    def judge(sim, name, options, expect):
        result = sim.run(options)
        failed = sorted({line.split()[1] for line in result.stdout.splitlines()
                         if line.startswith("CHECK_FAIL ")})
        beats = result.stdout.count("WBEAT ")
        if expect is None:
            ok = result.returncode == 0 and not failed
        else:
            ok = result.returncode != 0 and bool(set(failed) & set(expect))
        results["runs"].append(dict(run=name, ok=ok, beats=beats, failed=failed[:12],
                                    exit=result.returncode))
        print(f"{name}: {'OK' if ok else 'BAD'} beats={beats} failed={failed[:6]}")
        return ok

    good = judge(compile_(plain), "baseline", (), None)
    sim = compile_(faulted)
    results["engine"] = sim.engine
    good &= judge(sim, "fault_build_selection_0", ("+NLV_FAULT=0",), None)
    for k, fault in enumerate(FAULTS, 1):
        good &= judge(sim, fault["id"], (f"+NLV_FAULT={k}",), fault["expect"])
    for variant in VARIANTS:
        out = work / variant["id"]
        subprocess.run([args.fixture, "register_bank", str(out / "gen"), "--probes",
                        "--param", variant["param"]], check=True)
        probes = json.loads((out / "gen/probes.json").read_text())
        if "revision" in variant:
            stated = [group["fields"]["revision_generator"]["value"]
                      for group in probes["logger"]["groups"] if group["kind"] == "register_map"]
            good &= stated == [variant["revision"]]
            print(f"{variant['id']}: register map states {stated}")
        (out / "tb.sv").write_text(bench if variant.get("default_bench")
                                   else defines(probes) + Path(args.bench).read_text())
        (out / "dut.v").write_text((out / "gen/network.v").read_text())
        good &= judge(compile_(out), variant["id"], (), variant["expect"])
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
        results = dict(schema=1, case="register_bank", exit=2, error=traceback.format_exc())
        print(results["error"])
    (top / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    print("REGISTER_BANK", "PASS" if results["exit"] == 0 else "FAIL")
    return results["exit"]


if __name__ == "__main__":
    sys.exit(main())
