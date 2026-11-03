/-
  GenericNoC.Instances.Requests: a request of two words and its answer.

  Slot 0 is a source and slot 1 a target's response slot.  The target has one
  context.  A transaction takes one context, however many words its request has, and
  gives it back when its last word leaves the response slot.  A later request word
  can still be added once the first has arrived, and only the requester is answered.
-/
import GenericNoC.WormholeDone
open Wormhole
namespace Wormhole.Requests

def cfg : Config where
  routes := [(0, 1, 0), (1, 0, 1)]
  nexts := []
  pools := [(0, 1, 1)]
  cap := fun _ => 4
  poolCap := fun _ => 1
  endpoints := [0, 1]
  mayHold := fun _ _ => true
  contig := fun _ => true
  cross := fun _ => false
  bound := fun _ => false
  slots := [0, 1]
  hops := fun _ _ => 1
  hopMax := 2
  ups := fun p => [p]
  answers := fun s d => s == 0 && d == 0
  ahead := fun _ _ => false

/-- First request word, keeping the output. -/
def r1 : Word := ⟨1, false, false, 1⟩
/-- Second request word. -/
def r2 : Word := ⟨1, true, false, 1⟩
/-- The answer. -/
def a1 : Word := ⟨0, true, true, 1⟩

def s1 : St := injected St.empty 0 r1 2
def s2 : St := { s1 with grant := upd s1.grant 0 (some 0), ev := none }
def s3 : St := place cfg (pop s2 0 0 r1) 0 r1
def s4 : St := injected s3 0 r2 0
def s5 : St := place cfg (pop s4 0 0 r2) 0 r2
def s6 : St := injected s5 1 a1 0
def s7 : St := { s6 with grant := upd s6.grant 1 (some 1), ev := none }
def s8 : St := place cfg (pop s7 1 1 a1) 1 a1

theorem steps1 : Step cfg St.empty s1 :=
  Step.inject St.empty 0 r1 2 (by decide) (by decide) (Or.inr (Or.inl rfl)) (fun _ _ h => by simp [St.empty] at h)
    rfl (by decide) (by decide) (by decide) (by decide) (by decide) (fun h => by simp [r1] at h)
    (fun h => by simp [cfg] at h)

theorem steps2 : Step cfg s1 s2 := Step.arb s1 0 0 r1 rfl rfl rfl
theorem steps3 : Step cfg s2 s3 :=
  Step.fire s2 0 0 r1 rfl rfl rfl (Or.inl (show poolUse s2 1 < 1 by decide)) (by decide) (fun h => absurd h (by decide))

/-- A later word of a request can be added after the first has arrived. -/
theorem steps4 : Step cfg s3 s4 :=
  Step.inject s3 0 r2 0 (by decide) (show (0 : Nat) < 4 by decide) (Or.inl rfl)
    (fun _ d h => by
      have e : s3.openIn 0 = some 1 := rfl
      rw [e] at h
      cases h
      rfl) rfl
    (by decide) (by decide) (by decide) (by decide) (by decide) (fun h => by simp [r2] at h)
    (fun h => by simp [cfg] at h)

/-- It needs no second context, although the pool is full. -/
theorem pool_full_at_second_word : ¬ room cfg s4 0 1 :=
  fun h => absurd (show poolUse s4 1 < 1 from h) (by decide)

theorem steps5 : Step cfg s4 s5 :=
  Step.fire s4 0 0 r2 rfl rfl rfl (Or.inr ⟨rfl, by decide⟩) (by decide) (fun h => absurd h (by decide))

theorem steps6 : Step cfg s5 s6 :=
  Step.inject s5 1 a1 0 (by decide) (by decide) (Or.inl rfl)
    (fun _ d h => by
      have e : s5.openIn 1 = none := rfl
      rw [e] at h
      cases h) rfl
    (by decide) (by decide) (by decide) (by decide) (by decide)
    (fun _ => ⟨rfl, .sink (o := 1) rfl rfl rfl⟩) (fun h => by simp [cfg] at h)

theorem steps7 : Step cfg s6 s7 := Step.arb s6 1 1 a1 rfl rfl rfl
theorem steps8 : Step cfg s7 s8 :=
  Step.fire s7 1 1 a1 rfl rfl rfl (Or.inl trivial) (by decide) (fun h => absurd h (by decide))

/-- One context for the two words. -/
theorem one_context : poolUse s5 1 = 1 := by decide

/-- After the answer: the context is back and the transaction has ended. -/
theorem no_leak : poolUse s8 1 = 0 ∧ s8.live 1 = false ∧ s8.q 0 = [] ∧ s8.q 1 = [] := by decide

/-- A response slot cannot answer a transaction nobody requested. -/
theorem no_phantom (w : Word) :
    ¬ (isResponse cfg 1 = true → St.empty.live w.tok = true ∧ St.empty.home w.tok = some 1 ∧
        St.empty.reqDone w.tok = true ∧ answersSrc cfg (St.empty.src w.tok) w.dest = true) := by
  intro h
  have := (h (by decide)).1
  simp [St.empty] at this

/-- Nor before the whole request has arrived: after the first word, not yet. -/
theorem no_early_answer : s3.reqDone 1 = false := by decide

/-- After the last request word is added, no more request words. -/
theorem no_late_word : s4.closed 1 = true ∧ s4.home 1 = some 1 ∧ ¬ (s4.src 1 = some 0 ∧
    (s4.closed 1 = false ∨ cfg.pools = [])) := by
  refine ⟨by decide, by decide, ?_⟩
  rintro ⟨_, h | h⟩
  · simp [s4, s3, s2, s1, injected, r2, upd, place, pop, cfg, Config.next, Config.pool, r1, St.empty] at h
  · simp [cfg] at h

/-- And it cannot answer anyone but the requester. -/
theorem only_the_requester (d : Nat) (h : d ≠ 0) : answersSrc cfg (some 0) d = false := by
  simp [answersSrc, cfg, h]

#print axioms no_leak
#print axioms no_phantom

end Wormhole.Requests
