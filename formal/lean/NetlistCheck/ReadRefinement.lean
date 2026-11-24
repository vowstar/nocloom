import NetlistCheck.Netlist
import GenericNoC.ReadRefinement

namespace NetlistCheck.ReadRefinement

open Wormhole.ReadRefinement

/-- Value indices of a request or response face. The selector is the write bit
    for a request and the source flow for a response. -/
structure Face where
  valid : Nat
  ready : Nat
  last : Nat
  selector : Nat

/-- The observations in the read refinement map, in netlist value numbering. -/
structure Map where
  count : Nat
  requestIn : Face
  requestOut : Face
  responseIn : Face
  responseOut : Face

/-- Signal names used by the checker. The count after an edge is sampled
    separately from the combinational values before it. -/
inductive Signal where
  | before (index : Nat)
  | nextCount
  | after (index : Nat)

/-- The Boolean checker as a circuit, shared by evaluation and RTL emission. -/
inductive Guard where
  | accept
  | eq (signal : Signal) (value : Nat)
  | sameValue (left right : Signal)
  | leOne (signal : Signal)
  | neg (arg : Guard)
  | both (left right : Guard)
  | either (left right : Guard)
  | same (left right : Guard)

def Guard.eval (value : Signal → Nat) : Guard → Bool
  | .accept => true
  | .eq s k => value s == k
  | .sameValue a b => value a == value b
  | .leOne s => value s ≤ 1
  | .neg a => !a.eval value
  | .both a b => a.eval value && b.eval value
  | .either a b => a.eval value || b.eval value
  | .same a b => a.eval value == b.eval value

/-- Render the guard with the caller's names for its signal inputs. -/
def Guard.render (name : Signal → String) : Guard → String
  | .accept => "1'b1"
  | .eq s k => s!"({name s} == {k})"
  | .sameValue a b => s!"({name a} == {name b})"
  | .leOne s => s!"({name s} <= 1)"
  | .neg a => s!"(!{a.render name})"
  | .both a b => s!"({a.render name} && {b.render name})"
  | .either a b => s!"({a.render name} || {b.render name})"
  | .same a b => s!"({a.render name} == {b.render name})"

def Face.transfer (f : Face) : Guard :=
  .both (.eq (.before f.valid) 1) (.eq (.before f.ready) 1)

def Face.single (f : Face) (selector : Nat) : Guard :=
  .both (.eq (.before f.last) 1) (.eq (.before f.selector) selector)

def Map.boundaries (m : Map) : Guard :=
  let req := m.requestIn.transfer
  let rsp := m.responseIn.transfer
  .both (.same req m.requestOut.transfer) <|
    .both (.same rsp m.responseOut.transfer) <|
      .both (.either (.neg req) (.both (m.requestIn.single 0) (m.requestOut.single 0)))
        (.either (.neg rsp) (.both (m.responseIn.single 1) (m.responseOut.single 1)))

def patterns (before req rsp after : Guard) : Guard :=
  .either (.both (.both (.neg req) (.neg rsp)) (.same before after)) <|
    .either (.both (.both (.neg before) req) (.both (.neg rsp) after))
      (.both (.both before (.neg req)) (.both rsp (.neg after)))

theorem patterns_sound (value : Signal → Nat) (before req rsp after : Guard) :
    (patterns before req rsp after).eval value =
      stepOK ⟨before.eval value, req.eval value, rsp.eval value, after.eval value⟩ := by
  simp only [patterns, Guard.eval]
  cases before.eval value <;> cases req.eval value <;> cases rsp.eval value <;>
    cases after.eval value <;> decide

/-- The occupancy must be a bit. Accepted transfers agree at both ends of
    each transparent path and carry the single-beat read markers. -/
def Map.guard (m : Map) : Guard :=
  .both (.both (.leOne (.before m.count)) (.leOne .nextCount)) <|
    .both m.boundaries <|
      patterns (.eq (.before m.count) 1) m.requestIn.transfer m.responseIn.transfer
        (.eq .nextCount 1)

/-- Read the old register values by netlist value index. During a rising edge,
    each register samples the same combinational valuation. The caller supplies
    a netlist with one clock and direct active-low reset. -/
def rising (n : Netlist) (inputs current : Nat → Nat) (i : Nat) : Nat :=
  match n.op? i with
  | some (.state k) =>
    match n.states[k]? with
    | some reg =>
      let value := n.eval inputs current
      match reg.reset with
      | some (reset, init) => if value reset == 0 then init else value reg.next
      | none => value reg.next
    | none => current i
  | _ => current i

def Map.valuation (m : Map) (n : Netlist) (inputs current : Nat → Nat) : Signal → Nat
  | .before i => n.eval inputs current i
  | .nextCount => rising n inputs current m.count
  | .after i => rising n inputs current i

/-- Check a rising edge outside reset. The driver checks reset separately. -/
def edgeOK (n : Netlist) (m : Map) (inputs current : Nat → Nat) : Bool :=
  m.guard.eval (m.valuation n inputs current)

/-- Soundness for an observed rising edge of the netlist. Physical occupancy
    is read from the netlist register, and logical identity is carried by the
    transaction token in the matching invariant. -/
theorem edge_sound {n : Netlist} {m : Map} {inputs current : Nat → Nat}
    {c : Wormhole.Config} {p : Path} (cfg : p.Valid c) {σ : Wormhole.St} {k : Nat}
    (h : Matches p σ (n.eval inputs current m.count == 1) k)
    (ok : edgeOK n m inputs current = true) :
    ∃ σ', Matches p σ' (rising n inputs current m.count == 1)
        (if m.responseIn.transfer.eval (m.valuation n inputs current) then k + 1 else k) ∧
      (σ' = σ ∨ ∃ mid, GenericNoC.Steps (Wormhole.Step c) σ mid σ') := by
  simp only [edgeOK, Map.guard, Guard.eval, Bool.and_eq_true, patterns_sound] at ok
  exact step_sound (sample := ⟨_, _, _, _⟩) cfg h ok.2.2

/-- A low reset on the mapped context register initializes the model invariant. -/
theorem reset_sound {n : Netlist} {m : Map} {inputs current : Nat → Nat}
    {p : Path} {k reset : Nat} {reg : State}
    (state : n.op? m.count = some (.state k)) (row : n.states[k]? = some reg)
    (clear : reg.reset = some (reset, 0)) (low : n.eval inputs current reset = 0) :
    Matches p Wormhole.St.empty (rising n inputs current m.count == 1) 0 := by
  simpa [rising, state, row, clear, low] using matches_reset p

end NetlistCheck.ReadRefinement
