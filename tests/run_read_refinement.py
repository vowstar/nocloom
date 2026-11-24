#!/usr/bin/env python3
"""Check the direct AXI4 read map and prove its cycle assertions."""
import argparse
import copy
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from assertion_solver import covers, prove, refute, run_all
from run_netlist_roundtrip import ALLOWED_AXIOMS, LEAN, run


def checked(cmd, cwd=None):
    result = run(cmd, cwd=cwd)
    if result.returncode or re.search(r"\bwarning:", result.stdout, re.I):
        raise RuntimeError(result.stdout or f"{cmd[0]} exited {result.returncode}")
    return result.stdout


def prepare_lean(work):
    for tool in ("lean", "lake", "sby"):
        if shutil.which(tool) is None:
            sys.exit(f"FAIL {tool} is not on PATH")
    lean = work / "lean"
    lean.mkdir()
    for name in ("GenericNoC", "NetlistCheck"):
        shutil.copytree(LEAN / name, lean / name)
    for name in ("lakefile.lean", "lake-manifest.json", "lean-toolchain", "GenericNoC.lean"):
        shutil.copy2(LEAN / name, lean / name)
    pinned = (lean / "lean-toolchain").read_text().strip().rsplit(":v", 1)[-1]
    if f"version {pinned}," not in checked(["lean", "--version"], lean):
        raise RuntimeError(f"Expected Lean {pinned}")
    checked(["lake", "build", "GenericNoC", "+NetlistCheck.Check",
             "+NetlistCheck.ReadRefinement"], lean)
    return lean


def artifact(folder, suffix):
    path, = folder.glob(f"*.{suffix}")
    return path


def port_for(interface, endpoint, signal):
    port, = (p for p in interface["ports"] if p["endpoint"] == endpoint and p["signal"] == signal)
    return port


def tuple_term(items):
    if any(type(item) is not int or item < 0 for item in items):
        raise ValueError("Expected natural number indices")
    return "⟨" + ", ".join(map(str, items)) + "⟩"


def proof_source(mapping, instance, count, clock, reset):
    faces = mapping["boundaries"]

    def face(name, selector):
        return tuple_term([faces[name][field] for field in ("valid", "ready", "last", selector)])

    routes = tuple_term(mapping["request"] + mapping["response"])
    observations = (f'⟨{count}, {face("req/entry", "write")}, {face("req/exit", "write")}, '
                    f'{face("rsp/entry", "source")}, {face("rsp/exit", "source")}⟩')
    return instance + f"""

open NetlistCheck NetlistCheck.ReadRefinement Wormhole.ReadRefinement

def readPath : Path := {routes}
theorem readPathValid : readPath.Valid Generated.ReadTransport.config := by
  constructor <;> decide

def observation : Map := {observations}

theorem checkedReadEdge {{n : Netlist}} {{inputs current : Nat → Nat}}
    {{σ : Wormhole.St}} {{k : Nat}}
    (h : Matches readPath σ (n.eval inputs current observation.count == 1) k)
    (ok : edgeOK n observation inputs current = true) :
    ∃ σ', Matches readPath σ' (rising n inputs current observation.count == 1)
        (if observation.responseIn.transfer.eval (observation.valuation n inputs current)
         then k + 1 else k) ∧
      (σ' = σ ∨ ∃ mid, GenericNoC.Steps (Wormhole.Step Generated.ReadTransport.config) σ mid σ') :=
  edge_sound readPathValid h ok

#print axioms checkedReadEdge
#print axioms reset_sound

def main (args : List String) : IO UInt32 := do
  let [rtl, netlist, brokenRtl, brokenNetlist] := args
    | throw (IO.userError "expected a netlist and its Verilog for each design")
  let nText ← IO.FS.readFile netlist
  let brokenText ← IO.FS.readFile brokenNetlist
  if !check (← IO.FS.readFile rtl) nText || !check (← IO.FS.readFile brokenRtl) brokenText then
    throw (IO.userError "Verilog and netlist disagree")
  let .ok n := parseNetlist nText | throw (IO.userError "could not read netlist")
  let .ok broken := parseNetlist brokenText | throw (IO.userError "could not read fault netlist")
  let some (.state k) := n.op? observation.count | throw (IO.userError "expected a context state")
  let some reg := n.states[k]? | throw (IO.userError "missing context state")
  let some (.input _) := n.op? {clock} | throw (IO.userError "expected a clock input")
  let some (.input resetPort) := n.op? {reset} | throw (IO.userError "expected a reset input")
  if reg.clock != {clock} || reg.reset != some ({reset}, 0) then
    throw (IO.userError "context clock or reset disagrees with the map")
  let inp := fun i => if i = resetPort then 1 else 0
  let leaf := fun i => if i = observation.count then 1 else 0
  if !edgeOK n observation inp leaf || edgeOK broken observation inp leaf then
    throw (IO.userError "wrong verdict for the idle edge with an occupied context")
  IO.println ("GUARD " ++ observation.guard.render
    (fun s => match s with
      | .before i => "$past(nl_internal_v" ++ toString i ++ ")"
      | .nextCount => "nl_internal_v" ++ toString observation.count))
  return 0
"""


def check_axioms(output):
    expected = {"checkedReadEdge", "NetlistCheck.ReadRefinement.reset_sound"}
    seen = set()
    for name, dependencies in re.findall(r"'([^']+)' depends on axioms: \[([^\]]*)\]", output):
        if name in expected:
            seen.add(name)
            if {a.strip() for a in dependencies.split(",")} - ALLOWED_AXIOMS:
                raise RuntimeError(f"Unexpected axioms for {name}: {dependencies}")
    for name in re.findall(r"'([^']+)' does not depend on any axioms", output):
        if name in expected:
            seen.add(name)
    if seen != expected:
        raise RuntimeError(f"Missing axiom checks: {expected - seen}\n{output}")


def clear_context(rtl, netlist, count):
    """Disconnect the context next value and replace it with same-width zero."""
    values = [line.split() for line in netlist.splitlines() if line.startswith("value ")]
    width = values[count][1]
    zero = next(i for i, v in enumerate(values) if v[1:] == [width, "constant", "0"])
    row, = (line for line in netlist.splitlines() if line.startswith(f"state {count} "))
    _, _, next_value, clock, reset, init = row.split()
    assignment = f"    nl_internal_v{count} <= nl_internal_v{next_value};\n"
    if rtl.count(assignment) != 1:
        raise RuntimeError("Did not find the context update")
    faulty_rtl = rtl.replace(assignment, f"    nl_internal_v{count} <= nl_internal_v{zero};\n")
    faulty_netlist = netlist.replace(row + "\n", f"state {count} {zero} {clock} {reset} {init}\n")
    return faulty_rtl, faulty_netlist


def clock_port(netlist, value):
    values = [line.split() for line in netlist.splitlines() if line.startswith("value ")]
    ports = [line.split() for line in netlist.splitlines() if line.startswith("port ")]
    _, width, op, index = values[value]
    if (width, op) != ("1", "input"):
        raise ValueError("Expected a one-bit input")
    return ports[int(index)][-1]


def assertions(mapping, interface, netlist, guard):
    src = lambda signal: port_for(interface, "initiator:" + mapping["initiator"], signal)["name"]
    tgt = lambda signal: port_for(interface, "target:" + mapping["target"], signal)["name"]
    clock = clock_port(netlist, mapping["clock"])
    reset = clock_port(netlist, mapping["reset"])
    count = mapping["context_count"]
    data_width = port_for(interface, "initiator:" + mapping["initiator"], "rdata")["width"]
    size = (data_width // 8).bit_length() - 1
    id_width = port_for(interface, "target:" + mapping["target"], "arid")["width"]
    req, rsp = (mapping["boundaries"][name] for name in ("req/entry", "rsp/entry"))

    def transfer(face):
        return f'nl_internal_v{face["valid"]} && nl_internal_v{face["ready"]}'

    return clock, reset, f"""
wire ref_src_ar = {src('arvalid')} && {src('arready')};
wire ref_src_r = {src('rvalid')} && {src('rready')};
wire ref_tgt_ar = {tgt('arvalid')} && {tgt('arready')};
wire ref_tgt_r = {tgt('rvalid')} && {tgt('rready')};
wire ref_req = {transfer(req)};
wire ref_rsp = {transfer(rsp)};
reg ref_source_busy, ref_target_busy;
reg ref_requested, ref_responded, ref_answered;
reg [{id_width - 1}:0] ref_id;
reg ref_active;
reg ref_returned;

always @(posedge {clock}) begin
  if (!{reset}) begin
    ref_source_busy <= 0;
    ref_target_busy <= 0;
    ref_requested <= 0;
    ref_responded <= 0;
    ref_answered <= 0;
    ref_id <= 0;
    ref_returned <= 0;
    ref_active <= 0;
  end else begin
    ref_active <= 1;
    if (ref_src_ar) ref_source_busy <= 1;
    if (ref_src_r) ref_source_busy <= 0;
    if (ref_tgt_ar) begin
      ref_target_busy <= 1;
      ref_id <= {tgt('arid')};
    end
    if (ref_tgt_r) ref_target_busy <= 0;
    if (ref_req) ref_requested <= 1;
    if (ref_tgt_ar) ref_requested <= 0;
    if (ref_rsp) ref_responded <= 1;
    if (ref_src_r) ref_responded <= 0;
    if (ref_tgt_r) ref_answered <= 1;
    if (ref_rsp) ref_answered <= 0;
    if (ref_src_r) ref_returned <= 1;

  end
end

environment_reset: assume property (@(posedge {clock}) ref_active |-> {reset});
environment_read: assume property (@(posedge {clock}) {reset} |->
  !{src('awvalid')} && !{src('wvalid')} && !{tgt('bvalid')});
environment_request: assume property (@(posedge {clock}) {reset} && {src('arvalid')} |->
  !ref_source_busy && {src('araddr')} == 0 && {src('arlen')} == 0 &&
  {src('arsize')} == {size} && {src('arburst')} == 1 && !{src('arlock')});
environment_response: assume property (@(posedge {clock}) {reset} && {tgt('rvalid')} |->
  ref_target_busy && {tgt('rid')} == ref_id && {tgt('rlast')});
environment_hold_request: assume property (@(posedge {clock})
  {reset} && {src('arvalid')} && !{src('arready')} |=>
  {src('arvalid')} && $stable({{{src('arid')}, {src('arcache')}, {src('arprot')}}}));
environment_hold_response: assume property (@(posedge {clock})
  {reset} && {tgt('rvalid')} && !{tgt('rready')} |=>
  {tgt('rvalid')} && $stable({{{tgt('rdata')}, {tgt('rresp')}}}));

context_reset: assert property (@(posedge {clock})
  {reset} && !ref_active |-> nl_internal_v{count} == 0);
transport_refines: assert property (@(posedge {clock}) ref_active |-> {guard});
request_input: assert property (@(posedge {clock}) {reset} && ref_req |->
  ref_source_busy || ref_src_ar);
request_output: assert property (@(posedge {clock}) {reset} && ref_tgt_ar |->
  ref_requested || ref_req);
response_input: assert property (@(posedge {clock}) {reset} && ref_rsp |->
  ref_answered || ref_tgt_r);
response_output: assert property (@(posedge {clock}) {reset} && ref_src_r |->
  (ref_responded || ref_rsp) && {src('rlast')});

request_observed: cover property (@(posedge {clock}) {reset} && ref_req);
response_observed: cover property (@(posedge {clock}) {reset} && ref_rsp);
repeat_read: cover property (@(posedge {clock}) {reset} &&
  ref_source_busy && ref_returned && ref_src_r);
target_stalled: cover property (@(posedge {clock})
  {reset} && {tgt('arvalid')} && !{tgt('arready')});
source_stalled: cover property (@(posedge {clock})
  {reset} && {src('rvalid')} && !{src('rready')});
"""


def instrument(rtl, monitor):
    marker = "\nendmodule\n"
    if rtl.count(marker) != 1:
        raise RuntimeError("Expected one top module")
    return rtl.replace(marker, monitor + marker)


def fixture(cli, work, lean, width, synchronous):
    base = Path(__file__).with_name("formal") / "axi4_read_refinement.yaml"
    text = base.read_text()
    text = text.replace("data_width: 32", f"data_width: {width}")
    text = text.replace("transport_word_bytes: 4", f"transport_word_bytes: {width // 8}")
    if synchronous:
        text = text.replace("language: verilog_2001", "language: verilog_2001, register_reset: synchronous")
    work.mkdir()
    inp = work / "config.yaml"
    inp.write_text(text)
    gen = work / "generated"
    checked([cli, "validate", inp, "--refinement"])
    checked([cli, "generate", inp, "--out-dir", gen, "--refinement"])
    plain = work / "plain"
    checked([cli, "generate", inp, "--out-dir", plain])
    mapping = json.loads(artifact(gen, "refinement.json").read_text())
    rtl_path, netlist_path = artifact(gen, "v"), artifact(gen, "netlist.txt")
    rtl, netlist = rtl_path.read_text(), netlist_path.read_text()
    for old in plain.iterdir():
        if not old.name.endswith(".bundle.json") and old.read_bytes() != (gen / old.name).read_bytes():
            raise RuntimeError(f"Refinement changed {old.name}")
    bundle = json.loads(artifact(gen, "bundle.json").read_text())
    ordinary = json.loads(artifact(plain, "bundle.json").read_text())
    extras = [{"path": artifact(gen, suffix).name, "role": role}
              for suffix, role in (("netlist.txt", "netlist"), ("refinement.json", "refinement_map"))]
    ordinary["files"].extend(extras)
    if bundle != ordinary or set(p.name for p in gen.iterdir()) - set(p.name for p in plain.iterdir()) != {
            row["path"] for row in extras}:
        raise RuntimeError("Unexpected difference in generated artifacts")
    if mapping["format_version"] != 1 or mapping["module"] != bundle["top"]:
        raise RuntimeError("Refinement map does not describe the generated module")
    faulty_rtl, faulty_netlist = clear_context(rtl, netlist, mapping["context_count"])
    fault_v, fault_n = work / "fault.v", work / "fault.netlist.txt"
    fault_v.write_text(faulty_rtl)
    fault_n.write_text(faulty_netlist)

    def rendered(name, observed):
        instance = work / f"{name}.lean"
        checked([sys.executable, LEAN / "tools/gen_instance.py", artifact(gen, "dependency.json"),
                 "ReadTransport", instance, "--kernel"])
        instance.write_text("import NetlistCheck.Check\nimport NetlistCheck.ReadRefinement\n" +
                            proof_source(observed, instance.read_text(), observed["context_count"],
                                         observed["clock"], observed["reset"]))
        output = checked(["lake", "env", "lean", "--run", instance, rtl_path, netlist_path,
                          fault_v, fault_n], lean)
        check_axioms(output)
        guard, = re.findall(r"^GUARD (.+)$", output, re.M)
        return assertions(observed, json.loads(artifact(gen, "map.json").read_text()), netlist, guard)

    clock, reset, monitor = rendered("Instance", mapping)
    for name, product in (("design", rtl), ("fault", faulty_rtl)):
        (work / f"{name}.sv").write_text(instrument(product, monitor))
    proof = {"module": mapping["module"], "clock": clock, "reset": reset}
    (work / "proof.json").write_text(json.dumps(proof) + "\n")
    jobs = [(prove, work, "design", instrument(rtl, monitor), proof),
            *covers(work, "cover", instrument(rtl, monitor), proof)]
    refuted = 0
    if not synchronous:
        count = mapping["context_count"]
        faults = {"fault": faulty_rtl, "held": stuck(rtl, count, "1'b1"),
                  "frozen": stuck(rtl, count, f"nl_internal_v{count}")}
        for name, product in faults.items():
            jobs.append((refute, work, name, instrument(product, monitor), proof,
                         {"transport_refines"}))
        wrong = wrong_maps(mapping, netlist)
        for name, observed in wrong.items():
            jobs.append((refute, work, name, instrument(rtl, rendered(name, observed)[2]), proof,
                         MAP_CHECKS))
        refuted = len(faults) + len(wrong)
    run_all(jobs)
    print(f"PASS {width}-bit {'synchronous' if synchronous else 'asynchronous'} reset: "
          f"instance, map, checked edge, rejected fault, proof, covers and {refuted} refuted controls")


MAP_CHECKS = {"transport_refines", "request_input", "request_output", "response_input",
              "response_output"}


def stuck(rtl, count, value):
    """Give one context register a next value that ignores the transport."""
    update = re.compile(rf"(?m)^(    nl_internal_v{count} <= )nl_internal_v\d+;$")
    faulty, hits = update.subn(rf"\g<1>{value};", rtl)
    if hits != 1:
        raise RuntimeError("Did not find the context update")
    return faulty


def wrong_maps(mapping, netlist):
    """Maps that pass the Lean stage and that the generated Verilog refutes."""
    values = [line.split() for line in netlist.splitlines() if line.startswith("value ")]
    zero = next(i for i, v in enumerate(values) if v[1:] == ["1", "constant", "0"])
    quiet, crossed = copy.deepcopy(mapping), copy.deepcopy(mapping)
    quiet["boundaries"] = {name: dict.fromkeys(face, zero)
                           for name, face in mapping["boundaries"].items()}
    request, response = (mapping["boundaries"][name] for name in ("req/entry", "rsp/entry"))
    crossed["boundaries"]["req/entry"] = {**response, "write": response["source"]}
    crossed["boundaries"]["rsp/entry"] = {**request, "source": request["write"]}
    for face, selector in (("req/entry", "source"), ("rsp/entry", "write")):
        del crossed["boundaries"][face][selector]
    return {"quiet_map": quiet, "crossed_map": crossed}


def unsupported(cli, work):
    config = LEAN.parent.parent / "examples" / "axi4_exchange.yaml"
    destination = work / "unsupported"
    for command, options in (("validate", []), ("generate", ["--out-dir", destination])):
        result = run([cli, command, config, "--refinement", *options])
        if result.returncode != 1 or "NCL_REFINEMENT_SCOPE" not in result.stdout:
            raise RuntimeError(f"Expected refinement scope failure from {command}: {result.stdout}")
    if destination.exists():
        raise RuntimeError("Unsupported generation wrote files")
    checked([cli, "generate", config, "--out-dir", destination])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cli", type=Path, required=True)
    p.add_argument("--work", type=Path, required=True)
    args = p.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="check-", dir=args.work.resolve()))
    start = time.monotonic()
    unsupported(args.cli.resolve(), work)
    lean = prepare_lean(work)
    for width, sync in ((32, False), (64, True)):
        fixture(args.cli.resolve(), work / f"read{width}", lean, width, sync)
    print(f"PASS read refinement ({time.monotonic() - start:.1f} s) in {work}")


if __name__ == "__main__":
    main()
