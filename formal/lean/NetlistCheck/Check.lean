import Std.Data.HashSet
import NetlistCheck.Netlist
import NetlistCheck.Verilog
import NetlistCheck.Parse
import NetlistCheck.NetlistText

/-!
# The module conforms to the netlist

`conform M N` compares a parsed module with a netlist item by item:

| Part of the module | Must equal |
| --- | --- |
| name and ports | the netlist's name and ports, in order |
| `wire` and `reg` declarations | one per value in value order, except the inner stages of synchronizer chains; `reg` for states outside chains |
| continuous assignments | `exprOf i` for each value that is not a state or clock gate, in value order, then one per output driver |
| clock gate instances | the clock gate values, in value order |
| synchronizer instances | chains of synchronizer stages that cover every stage once |
| `always` blocks | the states outside chains, as a set |

`Sound.lean` proves what the assignment part means. The other rows are read
by the fixed meaning of each construct (`Verilog.lean`, registers and cells).
-/

namespace NetlistCheck
namespace Netlist

def portIdent (N : Netlist) (p : Nat) : Ident :=
  Ident.ofString ((N.ports[p]?.map (·.name)).getD "")

/-- The right side the printer writes for a value that is not a leaf. -/
def exprOf (N : Netlist) (i : Nat) : Expr :=
  match N.op? i with
  | some (.input p) => .id (N.portIdent p)
  | some (.const l) => .lit (N.width i) l
  | some (.zext a) => .cat (.lit (N.width i - N.width a) 0) (.id (.net a))
  | some (.ult a b) => .bin .lt (.id (.net a)) (.id (.net b))
  | some (.eq a b) => .bin .eq (.id (.net a)) (.id (.net b))
  | some (.not a) => .not (.id (.net a))
  | some (.and a b) => .bin .and (.id (.net a)) (.id (.net b))
  | some (.or a b) => .bin .or (.id (.net a)) (.id (.net b))
  | some (.sub a b) => .bin .sub (.id (.net a)) (.id (.net b))
  | some (.select c a b) => .cond (.id (.net c)) (.id (.net a)) (.id (.net b))
  | some (.slice a o) =>
    if N.width a == 1 then .id (.net a)
    else .part (.net a) (N.lit o + N.width i - 1) (N.lit o)
  | some (.concat a b) => .cat (.id (.net a)) (.id (.net b))
  | _ => .id (.net i)

/-- The nets `exprOf i` reads. -/
def refs (N : Netlist) (i : Nat) : List Nat :=
  match N.op? i with
  | some (.slice a _) => [a]
  | some op => op.operands
  | none => []

def isLeafAt (N : Netlist) (i : Nat) : Bool := (N.op? i).any Op.isLeaf

def isStateAt (N : Netlist) (i : Nat) : Bool :=
  match N.op? i with
  | some (.state _) => true
  | _ => false

end Netlist

open Netlist

/-! ## Synchronizer chains -/

def syncModule (syncReset : Bool) : String :=
  if syncReset then "nocloom_synchronizer_sync_reset" else "nocloom_synchronizer"

/-- One synchronizer instance as written. -/
structure SyncInst where
  module : String
  stages : Nat
  inst : Ident
  clock : Ident
  reset : Ident
  d : Conn
  q : Conn
  deriving Inhabited

def Module.syncs (M : Module) : Array SyncInst :=
  M.body.filterMap fun
    | .sync m k i c r d q => some ⟨m, k, i, c, r, d, q⟩
    | _ => none

def Conn.ident : Conn → Ident
  | .whole x | .bit x _ => x

/-- Checks the synchronizer instances against the netlist and returns which
values are inner chain stages (not declared in the module) and which are
chain tails (declared as `wire`, driven by the instances). Each run of
instances with one `q` net is one chain: one instance for a one-bit chain,
else one per bit in bit order. Walking back from the tail, each of the
`STAGES` registers must be a synchronizer stage on the instance's clock,
cleared to 0 by its reset, sampling the next one, and the last samples `d`.
Every synchronizer stage of the netlist must be in exactly one chain. -/
def syncCheck (M : Module) (N : Netlist) : Except String (Array Bool × Array Bool) := do
  let n := N.values.size
  let mut isStage := Array.replicate n false
  for s in N.syncStages do
    if s ≥ n then throw s!"synchronizer stage {s} out of range"
    isStage := isStage.set! s true
  let mut seen := Array.replicate n false
  let mut hidden := Array.replicate n false
  let mut tail := Array.replicate n false
  let syncs := M.syncs
  let mut j := 0
  while j < syncs.size do
    let first := syncs[j]!
    let .net t := first.q.ident | throw s!"synchronizer {first.inst.render}: q is not a value"
    let .net c := first.clock | throw s!"synchronizer {first.inst.render}: clock is not a value"
    let .net r := first.reset | throw s!"synchronizer {first.inst.render}: reset is not a value"
    let .net s := first.d.ident | throw s!"synchronizer {first.inst.render}: d is not a value"
    let k := first.stages
    if first.module != syncModule N.syncReset then
      throw s!"synchronizer {first.inst.render}: module {first.module} does not match the reset kind"
    if k < 2 then throw s!"synchronizer {first.inst.render}: fewer than two stages"
    -- Walk the chain from its tail.
    let mut x := t
    let mut head := t
    for step in [0:k] do
      let some (.state st) := N.op? x | throw s!"synchronizer at {t}: value {x} is not a state"
      let some state := N.states[st]? | throw s!"synchronizer at {t}: state {st} missing"
      if !isStage[x]! || seen[x]! then
        throw s!"synchronizer at {t}: value {x} is not a free synchronizer stage"
      if state.clock != c || state.reset != some (r, 0) then
        throw s!"synchronizer at {t}: stage {x} has another clock or reset"
      seen := seen.set! x true
      if step == 0 then tail := tail.set! x true else hidden := hidden.set! x true
      head := x
      if step + 1 < k then x := state.next
      else if state.next != s then throw s!"synchronizer at {t}: first stage does not sample d"
    let w := N.width t
    let bits := if w == 1 then 1 else w
    for b in [0:bits] do
      let some inst := syncs[j + b]? | throw s!"synchronizer at {t}: missing bit {b}"
      let name := netPrefix ++ toString head ++ "_sync" ++ (if w == 1 then "" else toString b)
      let (d, q) := if w == 1 then (Conn.whole (.net s), Conn.whole (.net t))
        else (Conn.bit (.net s) b, Conn.bit (.net t) b)
      if inst.module != first.module || inst.stages != k || inst.inst != .name name ||
          inst.clock != .net c || inst.reset != .net r || inst.d != d || inst.q != q then
        throw s!"synchronizer at {t}: instance {inst.inst.render} does not match bit {b}"
    j := j + bits
  for s in N.syncStages do
    if !seen[s]! then throw s!"synchronizer stage {s} is in no chain"
  -- The nets an instance reads are declared: none is an inner chain stage.
  for inst in syncs do
    for x in [inst.clock, inst.reset, inst.d.ident] do
      let .net a := x | throw s!"synchronizer {inst.inst.render}: a pin is not a value"
      if hidden[a]! then throw s!"synchronizer {inst.inst.render}: reads an inner stage {a}"
  return (hidden, tail)

/-! ## The other parts -/

def expDecls (N : Netlist) (hidden tail : Array Bool) : List Decl :=
  (List.range N.values.size).filterMap fun i =>
    if hidden[i]?.getD false then none
    else some ⟨if N.isStateAt i && !(tail[i]?.getD false) then .reg else .wire, N.width i, .net i⟩

def expAssigns (N : Netlist) : List (Ident × Expr) :=
  (List.range N.values.size).filterMap (fun i =>
    if N.isLeafAt i then none else some (.net i, N.exprOf i)) ++
  N.drivers.toList.map fun (p, d) => (N.portIdent p, .id (.net d))

def Module.gates (M : Module) : List (Ident × Ident × Ident × Ident × Ident) :=
  M.body.toList.filterMap fun
    | .gate i c e t o => some (i, c, e, t, o)
    | _ => none

def expGates (N : Netlist) : List (Ident × Ident × Ident × Ident × Ident) :=
  (List.range N.values.size).filterMap fun i =>
    match N.op? i with
    | some (.gate c e t) => some (.name (netPrefix ++ toString i ++ "_gate"), .net c, .net e, .net t, .net i)
    | _ => none

/-- A register: output, clock, optional (reset, cleared value), next. -/
abbrev Reg := Nat × Nat × Option (Nat × Nat) × Nat

def Module.regs (M : Module) (N : Netlist) : Except String (Array Reg) := do
  let mut out := #[]
  for item in M.body do
    match item with
    | .resetBlock (.net c) (.net r) neg clears loads =>
      if neg == N.syncReset then throw s!"always block on {c}: reset edge does not match the reset kind"
      if clears.map (·.q) != loads.map (·.1) then throw s!"always block on {c}: branches differ"
      for (x, (_, d)) in clears.zip loads do
        let (.net q, .net d) := (x.q, d) | throw s!"always block on {c}: a register is not a value"
        if x.width != N.width q then throw s!"always block on {c}: reset literal width of {q}"
        out := out.push (q, c, some (r, x.value), d)
    | .holdBlock (.net c) loads =>
      for (q, d) in loads do
        let (.net q, .net d) := (q, d) | throw s!"always block on {c}: a register is not a value"
        out := out.push (q, c, none, d)
    | .resetBlock .. | .holdBlock .. => throw "an always block names a clock or reset that is not a value"
    | _ => pure ()
  return out

def expRegs (N : Netlist) : Array Reg :=
  let stage := N.syncStages.foldl (fun acc s => acc.setIfInBounds s true)
    (Array.replicate N.values.size false)
  N.states.filterMap fun s =>
    if stage[s.output]?.getD false then none else some (s.output, s.clock, s.reset, s.next)

def regsCheck (M : Module) (N : Netlist) : Except String Unit := do
  let lt := fun (a b : Reg) => decide (a.1 < b.1)
  let got := (← M.regs N).qsort lt
  let want := (expRegs N).qsort lt
  if got != want then
    let diff := (got.zip want).find? (fun (a, b) => a != b)
    throw s!"always blocks differ from the states ({got.size} against {want.size}): {repr diff}"

def expPorts (N : Netlist) : List PortDecl :=
  N.ports.toList.map fun p => ⟨p.input, p.width, Ident.ofString p.name⟩

/-- Port and instance names are unique and are not value names. -/
def namesCheck (M : Module) : Except String Unit := do
  let mut names : Std.HashSet String := {}
  let insts := (M.gates.map (·.1)) ++ (M.syncs.toList.map (·.inst))
  for x in M.ports.toList.map (·.name) ++ insts do
    let .name s := x | throw s!"{x.render} declares a value name"
    if names.contains s then throw s!"{s} is declared twice"
    names := names.insert s

/-- Nets the module reads, other than the right sides of assignments and the
synchronizer instances, are declared. -/
def usedDeclared (N : Netlist) (hidden tail : Array Bool) : Bool :=
  let ok := fun (a : Nat) => a < N.values.size && !(hidden[a]?.getD false)
  let chained := fun (a : Nat) => hidden[a]?.getD false || tail[a]?.getD false
  N.states.all (fun s => chained s.output ||
    (ok s.next && ok s.clock && (s.reset.all fun (r, _) => ok r))) &&
  N.values.all fun v =>
    match v.op with
    | .gate c e t => ok c && ok e && ok t
    | _ => true

/-! ## The whole check -/

/-- The cell modules the top module instantiates; the top module has another name. -/
def cellModules : List String :=
  ["nocloom_clock_gate", "nocloom_synchronizer", "nocloom_synchronizer_sync_reset"]

/-- Words that make a synthesis or simulation tool act on a comment. -/
def toolWords : List String :=
  ["translate", "synthesis", "pragma", "full_case", "parallel_case", "coverage", "lint_",
   "map_to", "keep", "dont_"]

def plainComment (c : String) : Bool :=
  let lower := c.toLower
  toolWords.all fun w => (lower.splitOn w).length == 1

/-- The header the generator writes: its source line and the two license lines. -/
def headerComments (cs : Array String) : Bool :=
  match cs.toList with
  | [a, b, c] =>
    a == " Generated by NocLoom" && b.startsWith " SPDX-License-Identifier: " &&
      c.startsWith " NocLoom supplied content only." && [a, b, c].all plainComment
  | _ => false

/-- Declarations come first, then assignments and instances, then `always`
blocks, and no `always` block is empty. -/
def itemOrder (M : Module) : Bool := Id.run do
  let mut phase := 0
  for item in M.body do
    let (p, ok) := match item with
      | .decl _ => (0, true)
      | .assign .. | .gate .. | .sync .. => (1, true)
      | .resetBlock _ _ _ clears loads => (2, !clears.isEmpty && !loads.isEmpty)
      | .holdBlock _ loads => (2, !loads.isEmpty)
    if p < phase || !ok then return false
    phase := p
  return true

/-- The parts of `conform` once the chains are known. Each has a name for the
report of a failing check. -/
def parts (M : Module) (N : Netlist) (hidden tail : Array Bool) : List (String × Bool) :=
  [("netlist structure", N.wf),
   ("module name", M.name == N.name),
   ("ports", M.ports.toList == expPorts N),
   ("unique names", (namesCheck M).toBool),
   ("unique port names", decide ((N.ports.toList.map fun p => Ident.ofString p.name).Nodup)),
   ("port names are not value names", N.ports.all fun p =>
      match Ident.ofString p.name with
      | .name _ => true
      | .net _ => false),
   ("one driver per port", decide ((N.drivers.toList.map (·.1)).Nodup)),
   ("leaves only hidden", (List.range N.values.size).all fun i =>
      N.isLeafAt i || !(hidden[i]?.getD false)),
   ("references declared", (List.range N.values.size).all fun i =>
      N.isLeafAt i || (N.refs i).all fun a => a < N.values.size && !(hidden[a]?.getD false)),
   ("driver references declared", N.drivers.toList.all fun (_, d) =>
      d < N.values.size && !(hidden[d]?.getD false)),
   ("other references declared", usedDeclared N hidden tail),
   ("declarations", M.decls == expDecls N hidden tail),
   ("assignments", M.assigns == expAssigns N),
   ("clock gates", M.gates == expGates N),
   ("always blocks", (regsCheck M N).toBool),
   ("module name is not a cell name", !cellModules.contains N.name),
   ("header comments", headerComments M.comments),
   ("item order", itemOrder M)]

def conform (M : Module) (N : Netlist) : Bool :=
  match syncCheck M N with
  | .ok (hidden, tail) => (parts M N hidden tail).all (·.2)
  | .error _ => false

/-- The whole check of one design: the Verilog text parses, renders back to
the same bytes, and conforms to the netlist text. -/
def check (verilog netlist : String) : Bool :=
  match parse verilog, parseNetlist netlist with
  | .ok M, .ok N => M.render == verilog && conform M N
  | _, _ => false

/-- The reason `check` fails, or `none` when it passes. -/
def explain (verilog netlist : String) : Option String :=
  match parse verilog with
  | .error e => some s!"Verilog: {e}"
  | .ok M =>
    if M.render != verilog then
      let a := M.render.toUTF8
      let b := verilog.toUTF8
      let same := Id.run do
        let mut k := 0
        while k < a.size && k < b.size && a.get! k == b.get! k do k := k + 1
        return k
      some s!"Verilog: the text is not the rendering of its parse, from byte {same}"
    else match parseNetlist netlist with
    | .error e => some s!"netlist: {e}"
    | .ok N => match syncCheck M N with
      | .error e => some s!"synchronizers: {e}"
      | .ok (hidden, tail) =>
        match (parts M N hidden tail).find? (!·.2) with
        | some (name, _) =>
          let detail := match name with
            | "unique names" => match namesCheck M with | .error e => e | _ => ""
            | "always blocks" => match regsCheck M N with | .error e => e | _ => ""
            | _ => ""
          some s!"{name} {detail}"
        | none => none

end NetlistCheck
