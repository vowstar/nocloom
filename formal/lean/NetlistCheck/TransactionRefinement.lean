import NetlistCheck.ReadRefinement

namespace NetlistCheck.TransactionRefinement

open NetlistCheck.ReadRefinement
open Wormhole.ReadRefinement

/-- The two context observations and the lane indices on the two paths. -/
structure Map where
  transport : ReadRefinement.Map
  writeCount : Nat
  requestPayload : List (Nat × Nat)
  responsePayload : List (Nat × Nat)

def Map.occupied (m : Map) (write : Bool) (atNext : Bool) : Guard :=
  let i := if write then m.writeCount else m.transport.count
  .eq (if atNext then .after i else .before i) 1

def Map.anyOccupied (m : Map) (atNext : Bool) : Guard :=
  .either (m.occupied false atNext) (m.occupied true atNext)

def Face.transferAs (f : ReadRefinement.Face) (selector : Nat) : Guard :=
  .both f.transfer (.eq (.before f.selector) selector)

def Map.kind (m : Map) (write : Bool) : Guard :=
  patterns (m.occupied write false)
    (Face.transferAs m.transport.requestIn (if write then 1 else 0))
    (Face.transferAs m.transport.responseIn (if write then 0 else 1))
    (m.occupied write true)

private def counts (read write : Signal) : Guard :=
  .both (.both (.leOne read) (.leOne write)) (.either (.eq read 0) (.eq write 0))

def Map.control (m : Map) : Guard :=
  .both (counts (.before m.transport.count) (.before m.writeCount)) <|
  .both (counts (.after m.transport.count) (.after m.writeCount)) <|
  .both (m.kind false) <| .both (m.kind true) <|
    patterns (m.anyOccupied false) m.transport.requestIn.transfer
      m.transport.responseIn.transfer (m.anyOccupied true)

def sameTransfer (a b : ReadRefinement.Face) : Guard :=
  .both (.same a.transfer b.transfer) <|
    .either (.neg a.transfer) <|
      .both (.both (.eq (.before a.last) 1) (.eq (.before b.last) 1)) <|
        .both (.leOne (.before a.selector)) (.sameValue (.before a.selector) (.before b.selector))

def Map.boundaries (m : Map) : Guard :=
  .both (sameTransfer m.transport.requestIn m.transport.requestOut)
    (sameTransfer m.transport.responseIn m.transport.responseOut)

theorem transfer_sound {a b : ReadRefinement.Face} {v : Signal → Nat}
    (ok : (sameTransfer a b).eval v = true) :
    a.transfer.eval v = b.transfer.eval v ∧
      (a.transfer.eval v = true →
        v (.before a.last) = 1 ∧ v (.before b.last) = 1 ∧
          v (.before a.selector) ≤ 1 ∧ v (.before a.selector) = v (.before b.selector)) := by
  simp only [sameTransfer, Guard.eval, Bool.and_eq_true, beq_iff_eq] at ok
  refine ⟨ok.1, fun take => ?_⟩
  have fields := ok.2
  simp only [take, Bool.not_true, Bool.false_or, Bool.and_eq_true, beq_iff_eq,
    decide_eq_true_eq] at fields
  exact ⟨fields.1.1, fields.1.2, fields.2⟩

theorem kind_sound {m : Map} {v : Signal → Nat} {write : Bool}
    (ok : m.control.eval v = true) :
    stepOK ⟨(m.occupied write false).eval v,
      (Face.transferAs m.transport.requestIn (if write then 1 else 0)).eval v,
      (Face.transferAs m.transport.responseIn (if write then 0 else 1)).eval v,
      (m.occupied write true).eval v⟩ = true := by
  simp only [Map.control, Guard.eval, Bool.and_eq_true] at ok
  cases write
  · simpa only [Map.kind, patterns_sound, Bool.false_eq_true, if_false] using ok.2.2.1
  · simpa only [Map.kind, patterns_sound, if_true] using ok.2.2.2.1

def payloadGuard (transfer : Guard) (lanes : List (Nat × Nat)) : Guard :=
  .either (.neg transfer) <|
    lanes.foldr (fun (a, b) rest => .both (.sameValue (.before a) (.before b)) rest)
      .accept

def Map.guard (m : Map) : Guard :=
  .both m.control <| .both m.boundaries <|
    .both (payloadGuard m.transport.requestIn.transfer m.requestPayload)
      (payloadGuard m.transport.responseIn.transfer m.responsePayload)

def edgeOK (n : Netlist) (m : Map) (inputs current : Nat → Nat) : Bool :=
  m.guard.eval (m.transport.valuation n inputs current)

theorem payload_sound {v : Signal → Nat} {transfer : Guard} {lanes : List (Nat × Nat)}
    (ok : (payloadGuard transfer lanes).eval v = true) (take : transfer.eval v = true) :
    ∀ pair ∈ lanes, v (.before pair.1) = v (.before pair.2) := by
  simp only [payloadGuard, Guard.eval, take, Bool.not_true, Bool.false_or] at ok
  induction lanes with
  | nil => simp
  | cons pair rest ih =>
    obtain ⟨a, b⟩ := pair
    simp only [List.foldr_cons, Guard.eval, Bool.and_eq_true, beq_iff_eq] at ok
    intro pair mem
    rcases List.mem_cons.mp mem with rfl | mem
    · exact ok.1
    · exact ih ok.2 pair mem

theorem edge_sound {n : Netlist} {m : Map} {inputs current : Nat → Nat}
    {c : Wormhole.Config} {p : Path} (cfg : p.Valid c) {σ : Wormhole.St} {k : Nat}
    (h : Matches p σ ((m.anyOccupied false).eval (m.transport.valuation n inputs current)) k)
    (ok : edgeOK n m inputs current = true) :
    let v := m.transport.valuation n inputs current
    ∃ σ', Matches p σ' ((m.anyOccupied true).eval v)
        (if m.transport.responseIn.transfer.eval v then k + 1 else k) ∧
      (σ' = σ ∨ ∃ mid, GenericNoC.Steps (Wormhole.Step c) σ mid σ') ∧
      (m.transport.requestIn.transfer.eval v = true →
        ∀ pair ∈ m.requestPayload, v (.before pair.1) = v (.before pair.2)) ∧
      (m.transport.responseIn.transfer.eval v = true →
        ∀ pair ∈ m.responsePayload, v (.before pair.1) = v (.before pair.2)) := by
  simp only [edgeOK, Map.guard, Guard.eval, Bool.and_eq_true] at ok
  obtain ⟨control, _, req, rsp⟩ := ok
  simp only [Map.control, Guard.eval, Bool.and_eq_true, patterns_sound] at control
  obtain ⟨σ', inv, steps⟩ := step_sound (sample := ⟨_, _, _, _⟩) cfg h control.2.2.2.2
  exact ⟨σ', inv, steps, payload_sound req, payload_sound rsp⟩

theorem reset_sound {n : Netlist} {m : Map} {inputs current : Nat → Nat}
    {p : Path} {r w reset : Nat} {readReg writeReg : State}
    (read : n.op? m.transport.count = some (.state r)) (readRow : n.states[r]? = some readReg)
    (write : n.op? m.writeCount = some (.state w)) (writeRow : n.states[w]? = some writeReg)
    (readClear : readReg.reset = some (reset, 0)) (writeClear : writeReg.reset = some (reset, 0))
    (low : n.eval inputs current reset = 0) :
    Matches p Wormhole.St.empty ((m.anyOccupied true).eval (m.transport.valuation n inputs current))
      0 := by
  simpa [Map.anyOccupied, Map.occupied, Guard.eval, ReadRefinement.Map.valuation,
    rising, read, readRow, write, writeRow, readClear, writeClear, low] using matches_reset p

end NetlistCheck.TransactionRefinement
