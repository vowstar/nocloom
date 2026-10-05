"""Outlet of a register window at its faces, with stalled response faces.

The fixture emits the outlet and the banks behind it, and this driver writes the window above the
bench: the bases of the banks, the table of a bank from the register map, the instances and their
wiring. The bench sends words from four sources from a fixed seed, one under way at a time as the
network admits them, and stalls both response faces at random. Per seed:

- the unmodified netlist must pass, and so must the fault build with selection zero,
- the unmodified netlist must fail the stall checks, and only those, when the response faces never
  stall,
- the unmodified netlist must fail the check of the admission, and an answer must leave with the
  fields of another word, when a word is offered while another is under way,
- each designated fault of the outlet must fail on the check that names it.

  python3 tests/verif/service_outlet.py --fixture FIXTURE --bench sv/nlv_service_outlet_tb.sv
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

SEEDS = (4107, 90211)
WORDS = 900
BANKS = 3
FIELDS = dict(place=32, control=32, level=8, flags=4)

# The check of the network's admission: one word under way.
ADMISSION = "under_way_at_most_one"
# Checks that an answer has the fields of the word it belongs to.
OWN_WORD = ("answer_source", "answer_face")
# Checks that demand answers that waited for a stalled face.
STALL_CHECKS = ("stall_bank_unseen", "stall_local_unseen", "stall_write_unseen",
                "stall_read_unseen", "stall_at_start_unseen", "long_stall_unseen")


def state(name):
    return {"select": {"binding": name}}


def port(name):
    return {"select": {"input": name}}


def gate(op, *args):
    return dict(op=op, args=list(args))


def fault(id_, op, at, with_, **expect):
    return dict(id=id_, component="outlet", op=op, at={"binding": at}, expect=expect,
                **{"with": with_})


# The last answer word of a frame as the outlet sees it, and the response face taking the answer.
TAIL = gate("BitAnd", gate("BitAnd", port("nl_hint_bank_out_valid"), port("nl_hint_bank_out_tail")),
            gate("BitNot", {"self": True}))
LEAVES = gate("Select", state("bus.write"), port("nl_wrsp_ready"), port("nl_rrsp_ready"))

# Fault of the outlet and what must catch it. groups: at least one check of each group fails.
FAULTS = [
    # The last byte of a read answer is not kept.
    fault("answer_byte_dropped", "set_next", "r.last", dict(const=0), groups=(("read_data",),)),
    # The flag of an answer from a bank falls after one cycle, taken or not.
    fault("answer_flag_early", "set_next", "bus.answer", TAIL, groups=(("answer_withdrawn",),)),
    # The flag of an answer from a bank that left in its first cycle stays for one more.
    fault("answer_flag_late", "set_next", "bus.answer",
          gate("BitOr", TAIL, gate("BitAnd", {"self": True}, gate("BitNot", gate(
              "BitAnd", LEAVES, {"delay": {"self": True}})))),
          groups=(("answer_without_word",),)),
]


def nets_of(ports, special):
    """Every port of a module on the bench net of its name, but for the special ones."""
    return {p["name"]: special.get(p["name"], p["name"][3:]) for p in ports}


BANK_NETS = {
    "nl_clk": "clk", "nl_reset_n": "rst_n",
    "nl_request_valid": "rq_valid[{k}]", "nl_request_data": "rq_data[{k}]",
    "nl_request_tail": "rq_tail[{k}]", "nl_request_ready": "rq_ready[{k}]",
    "nl_response_valid": "rs_valid[{k}]", "nl_response_data": "rs_data[{k}]",
    "nl_response_tail": "rs_tail[{k}]", "nl_response_ready": "rs_ready[{k}]",
    "nl_field_control": "control[{k}]", "nl_field_level": "level[{k}]",
    "nl_field_flags": "flags_in[4*{k} +: 4]", "nl_field_flags_clear": "flags_clear[{k}]",
}
FACE_FIELDS = dict(
    request=("address", "write", "protection", "length", "size", "burst", "cache",
             "start_address", "tag", "id", "source", "count", "ends", "wrap_end", "error", "first",
             "last", "data", "be", "valid", "ready"),
    response=("valid", "address", "result", "count", "first", "last", "error", "data", "tag", "id",
              "source", "ready"))
OUTLET_PORTS = (
    {"nl_clk", "nl_reset_n", "nl_wait_local", "nl_wait_answer"}
    | {f"nl_hint_bank_{s}" for s in ("valid", "data", "tail", "ready", "out_valid", "out_data",
                                     "out_tail", "out_ready")}
    | {f"nl_{face}_{field}" for face in ("wreq", "rreq") for field in FACE_FIELDS["request"]}
    | {f"nl_{face}_{field}" for face in ("wrsp", "rrsp") for field in FACE_FIELDS["response"]})


def instance(module, name, ports, nets, extra=(), **where):
    """One instance with every port of the module on its bench net."""
    assert {p["name"] for p in ports} == set(nets), (module, sorted(p["name"] for p in ports))
    pins = [f".{p['name']}({nets[p['name']].format(**where)})" for p in ports] + list(extra)
    return f"{module} {name} ({', '.join(pins)});"


def window(components, core=None):
    """The instances and the wiring of the window. core: the outlet's module of a fault build
    and its fault selection port."""
    by_label = {c["label"]: c for c in components}
    outlet = by_label["outlet"]
    assert {p["name"] for p in outlet["ports"]} == OUTLET_PORTS, sorted(
        {p["name"] for p in outlet["ports"]} ^ OUTLET_PORTS)
    module, extra = outlet["module"], ()
    if core:
        module, extra = core["core"], (f".{core['sel_port']}(fault_sel)",)
    lines = [instance(module, "outlet", outlet["ports"],
                      nets_of(outlet["ports"], {"nl_clk": "clk", "nl_reset_n": "rst_n"}), extra),
             "assign rq_valid[0] = hint_bank_valid;", "assign rq_data[0] = hint_bank_data;",
             "assign rq_tail[0] = hint_bank_tail;", "assign hint_bank_ready = rq_ready[0];"]
    for k in range(BANKS):
        bank = by_label[f"bank{k}"]
        if k:
            lines += [f"assign rq_valid[{k}] = rs_valid[{k - 1}];",
                      f"assign rq_data[{k}] = rs_data[{k - 1}];",
                      f"assign rq_tail[{k}] = rs_tail[{k - 1}];",
                      f"assign rs_ready[{k - 1}] = rq_ready[{k}];"]
        lines.append(instance(bank["module"], f"bank{k}", bank["ports"], BANK_NETS, k=k))
    lines += [f"assign hint_bank_out_valid = rs_valid[{BANKS - 1}];",
              f"assign hint_bank_out_data = rs_data[{BANKS - 1}];",
              f"assign hint_bank_out_tail = rs_tail[{BANKS - 1}];",
              f"assign rs_ready[{BANKS - 1}] = hint_bank_out_ready;"]
    return lines


def defines(probes, components, core=None):
    """`define lines above the bench: the window, the table of a bank, the base and the fixed
    word of each bank, and the instances."""
    (space,) = [g for g in probes["outlet"]["groups"] if g["kind"] == "window"]
    assert (space["source_bits"], space["id_bits"], space["tag_bits"]) == (2, 3, 2), space
    maps = []
    for k in range(BANKS):
        (group,) = [g for g in probes[f"bank{k}"]["groups"] if g["kind"] == "register_map"]
        assert group["key"] == k and group["words"] * 4 == space["bank_bytes"] == 64, group
        maps.append(group)
    table = {name: (f["word"], f["lsb"], f["width"]) for name, f in maps[0]["fields"].items()}
    assert {name: width for name, (_, _, width) in table.items()} == FIELDS, table
    assert all({name: (f["word"], f["lsb"], f["width"]) for name, f in m["fields"].items()}
               == table for m in maps), "every bank of the window has the same table"
    assert len({table[name][0] for name in table}) == len(table), table
    assert table["place"][1] == 0 and table["control"][1] == 0 and table["level"][1] % 8 == 0, table
    bases = [m["base"] for m in maps]
    assert len(set(bases)) == BANKS and all(b % 64 == 0 and b < space["bytes"] for b in bases)
    assert BANKS * 64 < space["bytes"], "the window has addresses outside every bank"
    fixed = [m["fields"]["place"]["value"] for m in maps]
    assert len(set(fixed)) == BANKS, "the fixed word tells the banks apart"
    out = [f"`define OUTLET_BANKS {BANKS}", f"`define OUTLET_WINDOW {space['bytes']}",
           f"`define OUTLET_BANK_BYTES {space['bank_bytes']}",
           f"`define OUTLET_SEED {SEEDS[0]}", f"`define OUTLET_WORDS {WORDS}"]
    for name, (word, lsb, _) in table.items():
        out.append(f"`define OUTLET_{name} 4'd{word}")
        out.append(f"`define OUTLET_{name}_lsb {lsb}")
    out.append("`define OUTLET_BASES \\\n" + " \\\n".join(
        f"  base[{k}] = 32'h{value:08x};" for k, value in enumerate(bases)))
    out.append("`define OUTLET_FIXED \\\n" + " \\\n".join(
        f"  fixed[{k}] = 32'h{value:08x};" for k, value in enumerate(fixed)))
    out.append("`define OUTLET_INSTANCES \\\n" + " \\\n".join(
        "  " + line for line in window(components, core)))
    return "\n".join(out) + "\n"


def numbers(line):
    return {key: int(value) for key, value in re.findall(r"(\w+)=(-?\d+)", line)}


def run(work, args):
    results = dict(schema=1, case="service_outlet", seeds=list(SEEDS), runs=[], work=str(work))
    gen = work / "gen"
    spec = work / "faults.spec.json"
    spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                            for f in FAULTS])) + "\n")
    subprocess.run([args.fixture, "service_outlet", str(gen), "--probes", "--faults", str(spec)],
                   check=True)
    probes = json.loads((gen / "probes.json").read_text())
    components = json.loads((gen / "ports.json").read_text())
    assert len(components) == BANKS + 1, [c["label"] for c in components]
    faults = json.loads((gen / "faults.json").read_text())
    (core,) = faults["modules"]
    assert core["label"] == "outlet" and not core["variants"], core
    assert [(f["id"], f["index"]) for f in faults["faults"]] == [
        (f["id"], k) for k, f in enumerate(FAULTS, 1)], faults["faults"]
    text = Path(args.bench).read_text()
    plain = work / "plain"
    plain.mkdir()
    (plain / "tb.sv").write_text(defines(probes, components) + text)
    (plain / "dut.v").write_text((gen / "network.v").read_text())
    faulted = work / "faulted"
    faulted.mkdir()
    (faulted / "tb.sv").write_text(defines(probes, components, core) + text)
    (faulted / "dut.v").write_text((gen / "faulted.v").read_text())

    def compile_(root):
        return sim_engine.Simulation(root, "dut.v", engine=args.sim, config=args.sim_config,
                                     compile_timeout=300, run_timeout=120)

    def judge(sim, name, options, groups=(), allowed=None):
        """groups: of each, one check must fail; none means the run must pass. allowed: the only
        checks that may fail. The line of a run states where each of its checks failed first."""
        result = sim.run(options)
        lines = result.stdout.splitlines()
        first = [line for line in lines if line.startswith("CHECK_FAIL ")]
        failed = sorted({line.split()[1] for line in first})
        if not groups:
            ok = result.returncode == 0 and not failed
        else:
            ok = result.returncode != 0 and all(set(group) & set(failed) for group in groups)
        if allowed is not None:
            ok = ok and set(failed) <= set(allowed)
        where = {line.split()[1]: line.split()[2] for line in reversed(first)}
        caught = [f"{f} {where[f]}" for f in failed if any(f in group for group in groups)]
        other = [f for f in failed if not any(f in group for group in groups)]
        results["runs"].append(dict(run=name, ok=ok, caught=caught, failed=failed,
                                    first=first[:8], exit=result.returncode))
        print(f"{name}: {'OK' if ok else 'BAD'} caught={caught} other={other}")
        return ok, lines

    good = True
    sim = compile_(plain)
    results["engine"] = sim.engine
    results["reports"] = []
    for seed in SEEDS:
        ok, lines = judge(sim, f"baseline_seed{seed}", (f"+NLV_SEED={seed}",))
        shown = [line for line in lines if line.startswith(("SEED ", "OUTLET ", "STALL "))]
        print("\n".join(shown))
        results["reports"].append(shown)
        # The numbers the report states: the seed, the words with one answer each, the words
        # back to back, and answers of each kind and on each face that waited for a stalled face.
        outlet = numbers(" ".join(line for line in shown if line.startswith("OUTLET ")))
        stalls = numbers(" ".join(line for line in shown if line.startswith("STALL ")))
        stated = (outlet.get("seed") == seed and outlet.get("words") == outlet.get("answers")
                  and outlet.get("words") == WORDS + 22 * BANKS
                  and all(outlet.get(kind, 0) > 0 for kind in ("reads", "writes", "outside",
                                                                 "miss"))
                  and outlet.get("back_to_back", 0) >= 8
                  and all(stalls.get(key, 0) >= 8
                          for key in ("bank", "local", "write_face", "read_face"))
                  and stalls.get("at_start", 0) > 0 and stalls.get("long", 0) > 0)
        print(f"report_seed{seed}: {'OK' if stated else 'BAD'}")
        good &= ok and stated
        # Response faces that never stall: the same netlist fails the stall checks alone.
        good &= judge(sim, f"no_stall_seed{seed}", (f"+NLV_SEED={seed}", "+NLV_OPEN=1"),
                      groups=[(c,) for c in STALL_CHECKS], allowed=STALL_CHECKS)[0]
        # A word offered while another is under way: the same netlist fails the check of the
        # admission, and an answer leaves with the fields of another word. The outlet keeps the
        # fields of one word, so it depends on the network's admission of one packet at a time.
        good &= judge(sim, f"relaxed_admission_seed{seed}",
                      (f"+NLV_SEED={seed}", "+NLV_RELAXED=1"), groups=((ADMISSION,), OWN_WORD))[0]

    sim = compile_(faulted)
    for seed in SEEDS:
        good &= judge(sim, f"fault_build_selection_0_seed{seed}",
                      (f"+NLV_SEED={seed}", "+NLV_FAULT=0"))[0]
        for k, f in enumerate(FAULTS, 1):
            good &= judge(sim, f"{f['id']}_seed{seed}", (f"+NLV_SEED={seed}", f"+NLV_FAULT={k}"),
                          **f["expect"])[0]
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
        results = dict(schema=1, case="service_outlet", exit=2, error=traceback.format_exc())
        print(results["error"])
    (top / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    print("SERVICE_OUTLET", "PASS" if results["exit"] == 0 else "FAIL")
    return results["exit"]


if __name__ == "__main__":
    sys.exit(main())
