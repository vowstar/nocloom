import GenericNoC.TransactionCompletion
import NetlistCheck.TransactionRefinement

namespace NetlistCheck.TransactionRefinement

open NetlistCheck.ReadRefinement
open Wormhole.ReadRefinement

theorem sample_sound {n : Netlist} {m : Map} {inputs current : Nat → Nat}
    (ok : edgeOK n m inputs current = true) :
    let v := m.transport.valuation n inputs current
    stepOK ⟨(m.anyOccupied false).eval v, m.transport.requestIn.transfer.eval v,
      m.transport.responseIn.transfer.eval v, (m.anyOccupied true).eval v⟩ = true := by
  simp only [edgeOK, Map.guard, Guard.eval, Bool.and_eq_true, Map.control, patterns_sound] at ok
  exact ok.1.2.2.2.2

/-- Observations of a run on one clock, with reset and agreement of the context
    sampled after each edge and before the following edge. -/
structure Run (n : Netlist) (m : Map) where
  inputs : Nat → Nat → Nat
  current : Nat → Nat → Nat
  reset : (m.anyOccupied false).eval (m.transport.valuation n (inputs 0) (current 0)) = false
  linked : ∀ t,
    (m.anyOccupied true).eval (m.transport.valuation n (inputs t) (current t)) =
      (m.anyOccupied false).eval (m.transport.valuation n (inputs (t + 1)) (current (t + 1)))
  checked : ∀ t, edgeOK n m (inputs t) (current t) = true

def Run.cycles {n : Netlist} {m : Map} (r : Run n m) : Cycles where
  occupied t := (m.anyOccupied false).eval (m.transport.valuation n (r.inputs t) (r.current t))
  request t := m.transport.requestIn.transfer.eval (m.transport.valuation n (r.inputs t) (r.current t))
  response t := m.transport.responseIn.transfer.eval (m.transport.valuation n (r.inputs t) (r.current t))
  initial := r.reset
  accepted t := r.linked t ▸ sample_sound (r.checked t)

end NetlistCheck.TransactionRefinement
