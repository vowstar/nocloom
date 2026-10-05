"""Chain of register banks at its serial port, with the register stage after every eighth bank.

For each chain length the fixture emits the banks and the stage module, and this driver writes the
chain above the bench: the table of a bank from the register map, the instances and their wiring.
The bench sends random reads and writes from a fixed seed while the answer's ready stalls at
random. Per length:

- the unmodified netlist must pass, and so must the fault build with selection zero,
- the unmodified netlist must fail the stall checks when the answer never stalls (with one access
  at a time no stage sees a stall, with many in flight no stall comes from the answer),
- each designated fault of the stage, put on one stage of the chain, must fail on the check of
  that stage that names it, and no other stage may report a word of its own.

  python3 tests/verif/register_chain.py --fixture FIXTURE --bench sv/nlv_register_chain_tb.sv
          --work DIR [--sim ENGINE] [--sim-config JSON]

Exit code: 0 every run met its expectation, 1 a run did not, 2 infrastructure error.
"""

import argparse
import json
import re
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

# A stage stands after every eighth bank of a chain, so these lengths have 1, 1, 2 and 3 stages:
# one bank behind the stage, a full segment that ends the chain, one bank behind the second stage,
# and the longest chain.
SEGMENT = 8
LENGTHS = (9, 16, 17, 32)


def stages_of(banks):
    return (banks - 1) // SEGMENT


def replace(at, **with_):
    return dict(component="stage", op="replace", at={"binding": at}, **{"with": with_})


# Fault of the stage and the checks of the faulted stage that name it: each of them must fail.
FAULTS = [
    # The stage reports ready while both places are full: it takes a word it has no place for,
    # and a word it held never leaves.
    dict(id="ready_when_full", expect=("word_overrun", "word_lost"),
         **replace("stage.ready", const=1)),
    # The stage offers its word for one more cycle after it left: the next bank takes it twice.
    dict(id="valid_again", expect=("word_twice",),
         **replace("stage.valid", op="BitOr", args=[{"self": True}, {"delay": {"self": True}}])),
    # The stage never offers the word it holds.
    dict(id="valid_never", expect=("word_withheld",), **replace("stage.valid", const=0)),
]

# Checks of a stage's own words. A fault on one stage must leave these quiet on every other.
WORD_CHECKS = ("places", "word_withheld", "word_twice", "word_lost", "word_order", "word_overrun")
# Checks that demand back-pressure through a stage.
STALL_CHECKS = ("never_stalled", "answer_stall_unseen", "second_place_unused")

BANK_NETS = {
    "nl_clk": "clk", "nl_reset_n": "rst_n",
    "nl_request_valid": "rq_valid[{k}]", "nl_request_data": "rq_data[{k}]",
    "nl_request_tail": "rq_tail[{k}]", "nl_request_ready": "rq_ready[{k}]",
    "nl_response_valid": "rs_valid[{k}]", "nl_response_data": "rs_data[{k}]",
    "nl_response_tail": "rs_tail[{k}]", "nl_response_ready": "rs_ready[{k}]",
    "nl_field_control": "control[{k}]", "nl_field_level": "level[{k}]",
    "nl_field_flags": "flags_in[4*{k} +: 4]", "nl_field_flags_clear": "flags_clear[{k}]",
    "nl_access_word_index": "word_index[{k}]", "nl_access_write_active": "write_active[{k}]",
    "nl_access_complete": "complete[{k}]", "nl_access_write_mask": "write_mask[{k}]",
    "nl_access_write_value": "write_value[{k}]", "nl_bus_lanes": "lanes[{k}]",
}
# The stage in front of bank k takes the response port of bank k-1.
STAGE_NETS = {
    "nl_clk": "clk", "nl_reset_n": "rst_n",
    "nl_hint_in_valid": "rs_valid[{k}-1]", "nl_hint_in_data": "rs_data[{k}-1]",
    "nl_hint_in_tail": "rs_tail[{k}-1]", "nl_hint_in_ready": "rs_ready[{k}-1]",
    "nl_hint_out_valid": "rq_valid[{k}]", "nl_hint_out_data": "rq_data[{k}]",
    "nl_hint_out_tail": "rq_tail[{k}]", "nl_hint_out_ready": "rq_ready[{k}]",
    "nl_place_first": "place_first[{s}]", "nl_place_second": "place_second[{s}]",
}
FIELDS = dict(place=32, control=32, level=8, flags=4)


def instance(module, name, ports, nets, extra=(), **where):
    """One instance with every port of the module on its bench net."""
    assert {p["name"] for p in ports} == set(nets), (module, sorted(p["name"] for p in ports))
    pins = [f".{p['name']}({nets[p['name']].format(**where)})" for p in ports] + list(extra)
    return f"{module} {name} ({', '.join(pins)});"


def chain(banks, components, core=None):
    """The instances and the wiring of a chain. core: the stage's module of a fault build and
    its fault selection port, which the bench drives on the one stage that carries the fault."""
    by_label = {c["label"]: c for c in components}
    stage = by_label["stage"]
    lines = ["assign rq_valid[0] = r_valid;", "assign rq_data[0] = r_data;",
             "assign rq_tail[0] = r_tail;"]
    for k in range(banks):
        bank = by_label[f"bank{k}"]
        if k and k % SEGMENT == 0:
            s = k // SEGMENT
            module, extra = stage["module"], ()
            if core:
                module = core["core"]
                extra = (f".{core['sel_port']}((fault_stage == {s}) ? fault_sel : 8'd0)",)
            lines.append(instance(module, f"stage{s}", stage["ports"], STAGE_NETS, extra, k=k, s=s))
        elif k:
            lines += [f"assign rq_valid[{k}] = rs_valid[{k - 1}];",
                      f"assign rq_data[{k}] = rs_data[{k - 1}];",
                      f"assign rq_tail[{k}] = rs_tail[{k - 1}];",
                      f"assign rs_ready[{k - 1}] = rq_ready[{k}];"]
        lines.append(instance(bank["module"], f"bank{k}", bank["ports"], BANK_NETS, k=k))
    lines.append(f"assign rs_ready[{banks - 1}] = s_ready;")
    return lines


def defines(banks, probes, components, seed, accesses, core=None):
    """`define lines above the bench: the chain's size, the table of a bank, the fixed word of
    each bank, and the instances."""
    maps = []
    for k in range(banks):
        (group,) = [g for g in probes[f"bank{k}"]["groups"] if g["kind"] == "register_map"]
        assert group["key"] == k and group["words"] == 16, group
        maps.append(group["fields"])
    table = {name: (f["word"], f["lsb"], f["width"]) for name, f in maps[0].items()}
    assert {name: width for name, (_, _, width) in table.items()} == FIELDS, table
    assert all({name: (f["word"], f["lsb"], f["width"]) for name, f in m.items()} == table
               for m in maps), "every bank of the chain has the same table"
    assert len({table[name][0] for name in table}) == len(table), table
    assert table["place"][1] == 0 and table["control"][1] == 0, table
    fixed = [m["place"]["value"] for m in maps]
    assert len(set(fixed)) == banks, "the fixed word tells the banks apart"
    (stage,) = [g for g in probes["stage"]["groups"] if g["kind"] == "stage_places"]
    assert stage["word_bits"] == 9, stage
    out = [f"`define CHAIN_BANKS {banks}", f"`define CHAIN_STAGES {stages_of(banks)}",
           f"`define CHAIN_SEGMENT {SEGMENT}", f"`define CHAIN_STAGE_PLACES {stage['places']}",
           f"`define CHAIN_SEED {seed}", f"`define CHAIN_ACCESSES {accesses}"]
    for name, (word, lsb, _) in table.items():
        out.append(f"`define CHAIN_{name} 4'd{word}")
        out.append(f"`define CHAIN_{name}_lsb {lsb}")
    out.append("`define CHAIN_FIXED \\\n" + " \\\n".join(
        f"  fixed[{k}] = 32'h{value:08x};" for k, value in enumerate(fixed)))
    out.append("`define CHAIN_INSTANCES \\\n" + " \\\n".join(
        "  " + line for line in chain(banks, components, core)))
    return "\n".join(out) + "\n"


def numbers(line):
    return {key: int(value) for key, value in re.findall(r"(\w+)=(-?\d+)", line)}


def run_length(banks, top, args, results):
    work = top / f"banks{banks}"
    gen = work / "gen"
    work.mkdir()
    spec = work / "faults.spec.json"
    spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                            for f in FAULTS])) + "\n")
    subprocess.run([args.fixture, "register_chain", str(gen), "--probes", "--param",
                    f"banks={banks}", "--faults", str(spec)], check=True)
    probes = json.loads((gen / "probes.json").read_text())
    components = json.loads((gen / "ports.json").read_text())
    assert len(components) == banks + 1, [c["label"] for c in components]
    faults = json.loads((gen / "faults.json").read_text())
    (core,) = faults["modules"]
    assert core["label"] == "stage" and not core["variants"], core
    assert [(f["id"], f["index"]) for f in faults["faults"]] == [
        (f["id"], k) for k, f in enumerate(FAULTS, 1)], faults["faults"]
    stages = stages_of(banks)
    seed = 1000 * banks + 7
    accesses = 300 + 25 * banks
    text = Path(args.bench).read_text()
    plain = work / "plain"
    plain.mkdir()
    (plain / "tb.sv").write_text(defines(banks, probes, components, seed, accesses) + text)
    (plain / "dut.v").write_text((gen / "network.v").read_text())
    faulted = work / "faulted"
    faulted.mkdir()
    (faulted / "tb.sv").write_text(defines(banks, probes, components, seed, accesses, core) + text)
    (faulted / "dut.v").write_text((gen / "faulted.v").read_text())

    def compile_(root):
        return sim_engine.Simulation(root, "dut.v", engine=args.sim, config=args.sim_config,
                                   compile_timeout=300, run_timeout=120)

    record = dict(banks=banks, stages=stages, seed=seed, runs=[])
    results["lengths"].append(record)

    def judge(sim, name, options, expect=(), stage=None, allowed=None):
        """expect: check names that must all fail; none means the run must pass. stage: the one
        stage whose word checks may fail. allowed: the only checks that may fail."""
        result = sim.run(options)
        lines = result.stdout.splitlines()
        failed = sorted({line.split()[1] for line in lines if line.startswith("CHECK_FAIL ")})
        if not expect:
            ok = result.returncode == 0 and not failed
        else:
            ok = result.returncode != 0 and set(expect) <= set(failed)
        if stage is not None:
            others = [f for f in failed if any(f.endswith("_" + c) for c in WORD_CHECKS)
                      and not f.startswith(f"stage{stage}_")]
            ok = ok and not others
        if allowed is not None:
            ok = ok and set(failed) <= set(allowed)
        caught = [f for f in failed if f in expect]
        record["runs"].append(dict(run=name, ok=ok, caught=caught, failed=failed[:24],
                                   exit=result.returncode))
        print(f"banks={banks} {name}: {'OK' if ok else 'BAD'} caught={caught} "
              f"failed={len(failed)} {[f for f in failed if f not in expect][:6]}")
        return ok, lines

    sim = compile_(plain)
    results["engine"] = sim.engine
    good, lines = judge(sim, "baseline", ())
    shown = [line for line in lines if line.startswith(("SEED ", "CHAIN ", "LATENCY ", "STAGE "))]
    print("\n".join(shown))
    record["report"] = shown
    # The numbers the report states: the seed, every stage with stalls, with stalls that the
    # answer's ready caused and with words in its second place, and the latency of an access on
    # an idle chain.
    chain_line = numbers(" ".join(line for line in shown if line.startswith("CHAIN ")))
    stage_lines = [numbers(line) for line in shown if line.startswith("STAGE ")]
    latency = numbers(" ".join(line for line in shown if line.startswith("LATENCY ")))
    stated = (chain_line.get("seed") == seed and chain_line.get("banks") == banks
              and chain_line.get("accesses") == accesses + 5 * banks
              and len(stage_lines) == stages
              and all(s["stalls"] > 0 and s["answer_stalls"] > 0 and s["second_place"] > 0
                      and s["words"] > 0 for s in stage_lines)
              and latency == dict(read=banks + stages + 4, write=banks + stages + 4))
    print(f"banks={banks} report: {'OK' if stated else 'BAD'}")
    good &= stated
    record.update(accesses=chain_line.get("accesses"), stage_report=stage_lines)

    # Without back-pressure the same netlist must fail. One access at a time and a ready answer:
    # no stage sees a stall. Many in flight and a ready answer: no stall comes from the answer.
    every = [f"stage{s}_{c}" for s in range(1, stages + 1) for c in STALL_CHECKS]
    good &= judge(sim, "no_stall_at_all", ("+NLV_OPEN=1", "+NLV_SPARSE=1", "+NLV_ACCESSES=0"),
                  expect=every, allowed=every)[0]
    good &= judge(sim, "no_stall_of_answer", ("+NLV_OPEN=1", f"+NLV_ACCESSES={accesses // 2}"),
                  expect=[f"stage{s}_answer_stall_unseen" for s in range(1, stages + 1)],
                  allowed=every)[0]

    sim = compile_(faulted)
    good &= judge(sim, "fault_build_selection_0", ("+NLV_FAULT=0",))[0]
    for s in range(1, stages + 1):
        for k, fault in enumerate(FAULTS, 1):
            good &= judge(sim, f"{fault['id']}_stage{s}",
                          (f"+NLV_FAULT={k}", f"+NLV_FAULT_STAGE={s}"),
                          expect=[f"stage{s}_{c}" for c in fault["expect"]], stage=s)[0]
    return good


def run(work, args):
    results = dict(schema=1, case="register_chain", lengths=[], work=str(work))
    good = True
    for banks in LENGTHS:
        good &= run_length(banks, work, args, results)
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
        results = dict(schema=1, case="register_chain", exit=2, error=traceback.format_exc())
        print(results["error"])
    (top / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    print("REGISTER_CHAIN", "PASS" if results["exit"] == 0 else "FAIL")
    return results["exit"]


if __name__ == "__main__":
    sys.exit(main())
