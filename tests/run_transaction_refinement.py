#!/usr/bin/env python3
"""Check the single-beat AXI4 transaction map and prove its cycle assertions."""
import argparse
import copy
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from assertion_solver import covers, prove, refute, run_all
from run_netlist_roundtrip import LEAN
from transaction_progress import CONTROLS, designs
from run_read_refinement import (
    artifact, checked, check_axioms, clear_context, clock_port, instrument,
    port_for, prepare_lean, stuck, tuple_term,
)


def map_terms(mapping):
    faces = mapping["boundaries"]
    tx = mapping["transactions"]
    rows = [str(mapping["context_count"])]
    for name in ("req/entry", "req/exit", "rsp/entry", "rsp/exit"):
        selector = "write" if name.startswith("req") else "source"
        rows.append(tuple_term([faces[name][key] for key in ("valid", "ready", "last", selector)]))
    payloads = ["[" + ", ".join(tuple_term(row) for row in tx[key]) + "]"
                for key in ("request_payload", "response_payload")]
    return f'⟨⟨{", ".join(rows)}⟩, {tx["write_count"]}, {", ".join(payloads)}⟩'


def proof_source(mapping):
    routes = tuple_term(mapping["request"] + mapping["response"])
    return f"""
open NetlistCheck NetlistCheck.ReadRefinement NetlistCheck.TransactionRefinement
open Wormhole.ReadRefinement

def path : Path := {routes}
theorem pathValid : path.Valid Generated.Transactions.config := by constructor <;> decide
def txMap : TransactionRefinement.Map := {map_terms(mapping)}

theorem checkedTransactionEdge {{n : Netlist}} {{inputs current : Nat → Nat}}
    {{σ : Wormhole.St}} {{k : Nat}}
    (h : Matches path σ ((txMap.anyOccupied false).eval (txMap.transport.valuation n inputs current)) k)
    (ok : TransactionRefinement.edgeOK n txMap inputs current = true) :
    let v := txMap.transport.valuation n inputs current
    ∃ σ', Matches path σ' ((txMap.anyOccupied true).eval v)
        (if txMap.transport.responseIn.transfer.eval v then k + 1 else k) ∧
      (σ' = σ ∨ ∃ mid, GenericNoC.Steps (Wormhole.Step Generated.Transactions.config) σ mid σ') ∧
      (txMap.transport.requestIn.transfer.eval v = true →
        ∀ pair ∈ txMap.requestPayload, v (.before pair.1) = v (.before pair.2)) ∧
      (txMap.transport.responseIn.transfer.eval v = true →
        ∀ pair ∈ txMap.responsePayload, v (.before pair.1) = v (.before pair.2)) :=
  TransactionRefinement.edge_sound pathValid h ok

#print axioms checkedTransactionEdge
#print axioms NetlistCheck.TransactionRefinement.reset_sound
#print axioms NetlistCheck.TransactionRefinement.kind_sound
#print axioms NetlistCheck.TransactionRefinement.transfer_sound

private def incoming : ReadRefinement.Face := ⟨0, 1, 2, 3⟩
private def outgoing : ReadRefinement.Face := ⟨4, 5, 6, 7⟩
private def oneExcept (missing : Nat) : Signal → Nat
  | .before i => if i = missing then 0 else 1
  | _ => 0

example : (sameTransfer incoming outgoing).eval (oneExcept 8) = true := by decide
example : (sameTransfer incoming outgoing).eval (oneExcept 5) = false := by decide
example : (sameTransfer incoming outgoing).eval (oneExcept 6) = false := by decide
example : (sameTransfer incoming outgoing).eval (oneExcept 7) = false := by decide
example : (payloadGuard incoming.transfer [(8, 9), (10, 11)]).eval (oneExcept 12) = true := by decide
example : (payloadGuard incoming.transfer [(8, 9), (10, 11)]).eval (oneExcept 9) = false := by decide
example : (payloadGuard incoming.transfer [(8, 9), (10, 11)]).eval (oneExcept 11) = false := by decide

def main (args : List String) : IO UInt32 := do
  let [rtl, netlist, faultyRtl, faultyNetlist] := args | throw (IO.userError "expected two designs")
  let text ← IO.FS.readFile netlist
  let faultyText ← IO.FS.readFile faultyNetlist
  if !check (← IO.FS.readFile rtl) text || !check (← IO.FS.readFile faultyRtl) faultyText then
    throw (IO.userError "Verilog and netlist disagree")
  let .ok n := parseNetlist text | throw (IO.userError "invalid netlist")
  let .ok faulty := parseNetlist faultyText | throw (IO.userError "invalid fault netlist")
  let some (.input rst) := n.op? {mapping["reset"]} | throw (IO.userError "invalid reset index")
  for i in [txMap.transport.count, txMap.writeCount] do
    let some (.state k) := n.op? i | throw (IO.userError "expected a context register")
    let some reg := n.states[k]? | throw (IO.userError "missing register")
    if reg.clock != {mapping["clock"]} || reg.reset != some ({mapping["reset"]}, 0) then
      throw (IO.userError "unexpected context clock or reset")
  let inputs := fun i => if i = rst then 1 else 0
  let current := fun i => if i = txMap.writeCount then 1 else 0
  let controlOK := fun design => txMap.control.eval (txMap.transport.valuation design inputs current)
  if !controlOK n || controlOK faulty then
    throw (IO.userError "wrong verdict for a stalled write context")
  let signal := fun s => match s with
    | .before i => "$past(nl_internal_v" ++ toString i ++ ")"
    | .after i => "nl_internal_v" ++ toString i
    | .nextCount => "nl_internal_v" ++ toString txMap.transport.count
  for (name, g) in
      [("context_steps", txMap.control), ("transport_faces", txMap.boundaries),
       ("write_payload", payloadGuard txMap.transport.requestIn.transfer txMap.requestPayload),
       ("read_payload", payloadGuard txMap.transport.responseIn.transfer txMap.responsePayload)] do
    IO.println (name ++ " " ++ g.render signal)
  return 0
"""


COMPLETION_PROOF = """
theorem checkedCompletion {n : Netlist} (hw : TransactionRefinement.Run n txMap)
    (progress : PortProgress hw.cycles) {t : Nat} (request : progress.sourceRequest t = true) :
    ∃ u, t < u ∧ progress.sourceResponse u = true := by
  open Generated.Transactions in
  exact checked_port_completes (table := table) (node := node) pathValid hw.cycles
    wf_ok (Wormhole.Import.capOf_pos caps_ok) hops_ok certificate
    (Wormhole.checkCoverageFast_covered covered_ok) heads_ok
    (Wormhole.Import.capOf_pos (by decide)) progress request

private def stalled : Cycles where
  occupied t := t != 0
  request t := t == 0
  response _ := false
  initial := rfl
  accepted t := by cases t <;> simp [stepOK]

theorem stalled_lacks_progress : ¬ Nonempty (EndpointProgress stalled) := by
  intro ⟨environment⟩
  obtain ⟨issue, _, sent⟩ := environment.issue 0 rfl
  obtain ⟨answer, _, seen⟩ := environment.answer issue sent
  obtain ⟨response, _, fire⟩ := environment.forward answer seen
  simp [stalled] at fire

#print axioms checkedCompletion
#print axioms stalled_lacks_progress
#print axioms Wormhole.ReadRefinement.fair_of_endpoint_progress
#print axioms Wormhole.ReadRefinement.port_completes_of_progress
#print axioms Wormhole.ReadRefinement.answer_alone_does_not_complete
"""


def implies(condition, result):
    return f"(!({condition}) || ({result}))"


class Monitor:
    def __init__(self, mapping, interface, netlist):
        self.mapping = mapping
        self.interface = interface
        self.clock = clock_port(netlist, mapping["clock"])
        self.reset = clock_port(netlist, mapping["reset"])
        self.lines = []

    def port(self, side, signal):
        endpoint = "initiator" if side == "src" else "target"
        return port_for(self.interface, endpoint + ":" + self.mapping[endpoint], signal)

    def sig(self, side, signal):
        return self.port(side, signal)["name"]

    def property(self, name, kind, expression):
        self.lines.append(f"{name}: {kind} property (@(posedge {self.clock}) {expression});")

    def assume(self, name, expression):
        self.property(name, "assume", f"{self.reset} |-> ({expression})")

    def check(self, name, expression):
        condition, _, result = expression.partition(" |-> ")
        self.property(name, "assert", f"{self.reset} && ({condition}) |-> ({result})")

    def cover(self, name, expression):
        self.property(name, "cover", f"{self.reset} && ({expression})")

    def transfer(self, side, channel):
        valid, ready = (self.sig(side, channel + suffix) for suffix in ("valid", "ready"))
        return f"({valid} && {ready})"

    def channel(self, side, channel, fields):
        take = f"{side}_{channel}"
        self.lines += [
            f"wire {take} = {self.transfer(side, channel)};",
            f"reg seen_{take};",
        ]
        for field in fields:
            sig = channel + field
            spec = self.port(side, sig)
            self.lines += [
                f'reg [{spec["width"] - 1}:0] kept_{side}_{sig};',
                f"always @(posedge {self.clock}) if ({take}) kept_{side}_{sig} <= {spec['name']};",
                f'wire [{spec["width"] - 1}:0] sample_{side}_{sig} = '
                f"seen_{take} ? kept_{side}_{sig} : {spec['name']};",
            ]
        self.lines.append(f"""
always @(posedge {self.clock}) begin
  if (!{self.reset} || src_r || src_b)
    seen_{take} <= 0;
  else if ({take})
    seen_{take} <= 1;
end""")

    def hold(self, side, channel, fields):
        valid, ready = (self.sig(side, channel + suffix) for suffix in ("valid", "ready"))
        payload = ", ".join(self.sig(side, channel + field) for field in fields)
        self.property(f"hold_{side}_{channel}", "assume",
                      f"{self.reset} && {valid} && !{ready} |=> {valid} && $stable({{{payload}}})")

    def environment(self):
        s, t = (lambda signal: self.sig("src", signal)), (lambda signal: self.sig("tgt", signal))
        size = (self.port("src", "wdata")["width"] // 8).bit_length() - 1
        self.property("initial_reset", "assume", f"ref_active |-> {self.reset}")
        self.assume("serial_requests",
                    f"!({s('arvalid')} && {s('awvalid')}) && " +
                    implies("seen_src_ar || seen_src_aw", f"!{s('arvalid')} && !{s('awvalid')}"))
        for ch in ("ar", "aw"):
            self.assume(f"single_{ch}", implies(s(ch + "valid"),
                        f"{s(ch + 'addr')} == 0 && {s(ch + 'len')} == 0 && "
                        f"{s(ch + 'size')} == {size} && {s(ch + 'burst')} == 1 && !{s(ch + 'lock')}"))
            self.hold("src", ch, ["id", "cache", "prot"])
        self.assume("single_w", implies(s("wvalid"),
                    f"!seen_src_w && (seen_src_aw || {s('awvalid')}) && {s('wlast')}"))
        self.hold("src", "w", ["data", "strb"])
        self.assume("target_r", implies(t("rvalid"), "seen_tgt_ar && !seen_tgt_r && "
                    f"{t('rid')} == kept_tgt_arid && {t('rlast')} && {t('rresp')} != 1"))
        self.assume("target_b", implies(t("bvalid"), "seen_tgt_aw && seen_tgt_w && !seen_tgt_b && "
                    f"{t('bid')} == kept_tgt_awid && {t('bresp')} != 1"))
        self.hold("tgt", "r", ["data", "resp"])
        self.hold("tgt", "b", ["resp"])

    def endpoints(self):
        s, t = (lambda signal: self.sig("src", signal)), (lambda signal: self.sig("tgt", signal))
        for ch in ("ar", "aw"):
            self.check(f"target_{ch}_once",
                       f"tgt_{ch} |-> (!seen_tgt_{ch} && (seen_src_{ch} || src_{ch}))")
        self.check("target_w_once", "tgt_w |-> (!seen_tgt_w && (seen_src_w || src_w))")
        self.check("target_w_strobes", f"tgt_w |-> ({t('wstrb')} == sample_src_wstrb && {t('wlast')})")
        for i in range(self.port("src", "wstrb")["width"]):
            lane = f"[{i * 8} +: 8]"
            self.check(f"target_w_lane_{i}", f"(tgt_w && sample_src_wstrb[{i}]) |-> "
                       f"({t('wdata')}{lane} == sample_src_wdata{lane})")
        self.check("source_r_data", f"src_r |-> (seen_src_ar && (seen_tgt_r || tgt_r) && "
                   f"{s('rid')} == kept_src_arid && {s('rlast')} && "
                   f"{s('rdata')} == (sample_tgt_rresp == 0 ? sample_tgt_rdata : 0) && "
                   f"{s('rresp')} == sample_tgt_rresp)")
        self.check("source_b_result", f"src_b |-> (seen_src_aw && seen_src_w && "
                   f"(seen_tgt_b || tgt_b) && {s('bid')} == kept_src_awid && "
                   f"{s('bresp')} == sample_tgt_bresp)")

    def transport(self, guards):
        read = self.mapping["context_count"]
        write = self.mapping["transactions"]["write_count"]
        self.check("context_reset", f"!ref_active |-> (nl_internal_v{read} == 0 && nl_internal_v{write} == 0)")
        for name, guard in guards.items():
            self.property(name, "assert", f"ref_active |-> {guard}")
        for flow, face in (("req", "req/entry"), ("rsp", "rsp/entry")):
            row = self.mapping["boundaries"][face]
            selector = "write" if flow == "req" else "source"
            self.lines += [
                f'wire {flow}_take = nl_internal_v{row["valid"]} && nl_internal_v{row["ready"]};',
                f'wire {flow}_write = nl_internal_v{row[selector]} == {int(flow == "req")};',
            ]
        self.check("transport_write_request", "(req_take && req_write) |-> "
                   "((seen_src_aw || src_aw) && (seen_src_w || src_w))")
        self.check("transport_read_request", "(req_take && !req_write) |-> (seen_src_ar || src_ar)")
        self.check("transport_write_response", "(rsp_take && rsp_write) |-> (seen_tgt_b || tgt_b)")
        self.check("transport_read_response", "(rsp_take && !rsp_write) |-> (seen_tgt_r || tgt_r)")
        for ch, flow, kind in (("aw", "req", "req_write"), ("ar", "req", "!req_write"),
                               ("b", "rsp", "rsp_write"), ("r", "rsp", "!rsp_write")):
            mark = f"seen_{flow}_{ch}"
            out = f"tgt_{ch}" if flow == "req" else f"src_{ch}"
            take = f"{flow}_take && {kind}"
            self.lines.append(f"""
reg {mark};
always @(posedge {self.clock}) begin
  if (!{self.reset} || {out}) {mark} <= 0;
  else if ({take}) {mark} <= 1;
end""")
            self.check(f"{flow}_{ch}_observed", f"{out} |-> ({mark} || ({take}))")
            self.cover(f"cover_{flow}_{ch}", take)
        self.cover("write_strobes", "tgt_w && sample_src_wstrb != 0 && !(&sample_src_wstrb)")
        self.cover("write_target_stall", f"{self.sig('tgt', 'wvalid')} && !{self.sig('tgt', 'wready')}")
        self.cover("read_source_stall", f"{self.sig('src', 'rvalid')} && !{self.sig('src', 'rready')}")

    def render(self, guards):
        self.lines.append(f"reg ref_active;\nalways @(posedge {self.clock}) ref_active <= {self.reset};")
        self.lines.extend(f"wire src_{ch} = {self.transfer('src', ch)};" for ch in ("r", "b"))
        for side, ch, fields in (
            ("src", "ar", ["id"]), ("src", "aw", ["id"]), ("src", "w", ["data", "strb"]),
            ("tgt", "ar", ["id"]), ("tgt", "aw", ["id"]), ("tgt", "w", []),
            ("tgt", "r", ["data", "resp"]), ("tgt", "b", ["resp"]),
        ):
            self.channel(side, ch, fields)
        self.environment()
        self.endpoints()
        self.transport(guards)
        return "\n".join(self.lines)


def corrupt_port(rtl, port):
    """Flip the lowest bit on an output before it crosses the port."""
    name = port["name"]
    assignment = re.compile(rf"(?m)^(assign {name} = )([^;]+);$")
    broken, count = assignment.subn(rf"\g<1>\g<2> ^ {port['width']}'d1;", rtl)
    if count != 1:
        raise RuntimeError(f"Expected exactly one driver of {name}")
    return broken


def fixture(cli, work, lean, width, synchronous, completion):
    base = Path(__file__).with_name("formal") / "axi4_read_refinement.yaml"
    config = base.read_text().replace("data_width: 32", f"data_width: {width}")
    config = config.replace("transport_word_bytes: 4", f"transport_word_bytes: {width // 8}")
    if synchronous:
        config = config.replace("language: verilog_2001", "language: verilog_2001, register_reset: synchronous")
    work.mkdir()
    inp = work / "config.yaml"
    inp.write_text(config)
    gen = work / "generated"
    checked([cli, "generate", inp, "--out-dir", gen, "--refinement"])
    mapping = json.loads(artifact(gen, "refinement.json").read_text())
    tx = mapping["transactions"]
    if len(tx["request_payload"]) != width // 16 or len(tx["response_payload"]) != width // 32:
        raise RuntimeError("Unexpected payload lane counts")
    rtl_path, netlist_path = artifact(gen, "v"), artifact(gen, "netlist.txt")
    rtl, netlist = rtl_path.read_text(), netlist_path.read_text()
    fault, fault_netlist = clear_context(rtl, netlist, tx["write_count"])
    fault_v = work / "fault.v"
    fault_n = work / "fault.netlist.txt"
    fault_v.write_text(fault)
    fault_n.write_text(fault_netlist)

    instance = work / "Transactions.lean"
    checked([sys.executable, LEAN / "tools/gen_instance.py", artifact(gen, "dependency.json"),
             "Transactions", instance, "--kernel"])
    imports = "import NetlistCheck.Check\nimport NetlistCheck.TransactionRefinement\n"
    if completion:
        imports += "import NetlistCheck.TransactionCompletion\n"
    source = instance.read_text() + proof_source(mapping)
    instance.write_text(imports + source + (COMPLETION_PROOF if completion else ""))
    result = checked(["lake", "env", "lean", "--run", instance,
                      rtl_path, netlist_path, fault_v, fault_n], lean)
    names = {"reset_sound", "kind_sound", "transfer_sound"}
    expected = {"checkedTransactionEdge"} | {f"NetlistCheck.TransactionRefinement.{name}" for name in names}
    if completion:
        expected.update({"checkedCompletion", "stalled_lacks_progress"} |
                        {f"Wormhole.ReadRefinement.{name}" for name in (
                            "fair_of_endpoint_progress", "port_completes_of_progress",
                            "answer_alone_does_not_complete")})
    check_axioms(result, expected)
    guards = dict(re.findall(r"^(\w+) (\([^\n]+)$", result, re.M))
    if set(guards) != {"context_steps", "transport_faces", "write_payload", "read_payload"}:
        raise RuntimeError(f"Missing cycle guards: {result}")
    monitor = Monitor(mapping, json.loads(artifact(gen, "map.json").read_text()), netlist)
    assertions = monitor.render(guards)
    port_fault = rtl
    for side, signal in (("tgt", "wdata"), ("tgt", "wstrb"), ("src", "rdata"), ("src", "bresp")):
        port_fault = corrupt_port(port_fault, monitor.port(side, signal))
    for name, design in (("design", rtl), ("fault", fault), ("port_fault", port_fault)):
        (work / f"{name}.sv").write_text(instrument(design, assertions))
    if completion:
        for name, design in designs(rtl, monitor, instrument).items():
            (work / f"{name}.sv").write_text(design)
    (work / "proof.json").write_text(json.dumps({"module": mapping["module"],
                                               "clock": monitor.clock, "reset": monitor.reset}) + "\n")
    if completion:
        refuted = solve_completion(work, synchronous)
    else:
        refuted = solve(work, lean, gen, (rtl_path, netlist_path, fault_v, fault_n), mapping,
                        netlist, assertions, port_fault, synchronous)
    print(f"PASS {width}-bit transaction map, checked edge, rejected context fault, proof, covers "
          f"and {refuted} refuted controls")


PORT_CHECKS = {"target_w_strobes", "source_r_data", "source_b_result"} | {
    f"target_w_lane_{i}" for i in range(8)}
MAP_CHECKS = {"context_reset", "context_steps", "transport_faces", "write_payload", "read_payload"}


def wrong_maps(mapping, netlist):
    """Maps that pass the Lean stage and that the generated Verilog refutes."""
    values = [line.split() for line in netlist.splitlines() if line.startswith("value ")]
    swapped, unrelated = copy.deepcopy(mapping), copy.deepcopy(mapping)
    swapped["context_count"] = mapping["transactions"]["write_count"]
    swapped["transactions"]["write_count"] = mapping["context_count"]
    entry, exit_ = mapping["transactions"]["request_payload"][0]
    other = next(i for i, v in enumerate(values)
                 if v[1] == values[entry][1] and v[2] == "constant" and i not in (entry, exit_))
    unrelated["transactions"]["request_payload"][0] = [entry, other]
    return {"swapped_contexts": swapped, "unrelated_payload": unrelated}


def map_guards(work, name, gen, files, mapping, lean):
    """The Lean stage for another map. It accepts the maps of wrong_maps."""
    instance = work / f"{name}.lean"
    checked([sys.executable, LEAN / "tools/gen_instance.py", artifact(gen, "dependency.json"),
             "Transactions", instance, "--kernel"])
    instance.write_text("import NetlistCheck.Check\nimport NetlistCheck.TransactionRefinement\n" +
                        instance.read_text() + proof_source(mapping))
    result = checked(["lake", "env", "lean", "--run", instance, *files], lean)
    guards = dict(re.findall(r"^(\w+) (\([^\n]+)$", result, re.M))
    if set(guards) != {"context_steps", "transport_faces", "write_payload", "read_payload"}:
        raise RuntimeError(f"Missing cycle guards: {result}")
    return guards


def solve(work, lean, gen, files, mapping, netlist, assertions, port_fault, synchronous):
    """Prove the assertions on the design. On the asynchronous fixture, refute
    changed write contexts, corrupted ports and wrong maps."""
    proof = json.loads((work / "proof.json").read_text())
    rtl = files[0].read_text()
    jobs = [(prove, work, "design", instrument(rtl, assertions), proof),
            *covers(work, "cover", instrument(rtl, assertions), proof)]
    if synchronous:
        run_all(jobs)
        return 0
    write = mapping["transactions"]["write_count"]
    faults = {"fault": (files[2].read_text(), {"context_steps"}),
              "held": (stuck(rtl, write, "1'b1"), {"context_steps"}),
              "frozen": (stuck(rtl, write, f"nl_internal_v{write}"), {"context_steps"}),
              "port_fault": (port_fault, PORT_CHECKS)}
    for name, (design, expected) in faults.items():
        jobs.append((refute, work, name, instrument(design, assertions), proof, expected))
    interface = json.loads(artifact(gen, "map.json").read_text())
    wrong = wrong_maps(mapping, netlist)
    for name, observed in wrong.items():
        paths = (work / f"{name}.fault.v", work / f"{name}.fault.netlist.txt")
        for path, text in zip(paths, clear_context(rtl, netlist,
                                                   observed["transactions"]["write_count"])):
            path.write_text(text)
        guards = map_guards(work, name, gen, (*files[:2], *paths), observed, lean)
        text = Monitor(observed, interface, netlist).render(guards)
        jobs.append((refute, work, name, instrument(rtl, text), proof, MAP_CHECKS))
    run_all(jobs)
    return len(faults) + len(wrong)


def solve_completion(work, synchronous):
    """Prove the progress assertions. On the asynchronous fixture, refute one
    design for each progress step."""
    proof = json.loads((work / "proof.json").read_text())
    design = (work / "completion.sv").read_text()
    jobs = [(prove, work, "completion", design, proof), *covers(work, "cover", design, proof)]
    if not synchronous:
        jobs += [(refute, work, name, (work / f"{name}.sv").read_text(), proof, expected)
                 for name, expected in CONTROLS.items()]
    run_all(jobs)
    return 0 if synchronous else len(CONTROLS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--completion", action="store_true")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="check-", dir=args.work.resolve()))
    start = time.monotonic()
    lean = prepare_lean(work)
    module = "TransactionCompletion" if args.completion else "TransactionRefinement"
    checked(["lake", "build", "+NetlistCheck." + module], lean)
    for width, sync in ((32, False), (64, True)):
        fixture(args.cli.resolve(), work / f"axi{width}", lean, width, sync, args.completion)
    print(f"PASS transaction refinement ({time.monotonic() - start:.1f} s) in {work}")


if __name__ == "__main__":
    main()
