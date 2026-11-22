/-
  GenericNoC.Refinement: refinement from a circuit in which one clock step may
  take several model steps, inside a circuit invariant.

  A clock step of a circuit can complete several handshakes at once.  The
  model takes one event per step, so one circuit step maps to a nonempty finite
  sequence of model steps, in an order the instance chooses.  The circuit facts
  hold only on states inside `inv`, which holds at reset and is kept by every
  step.

  `CircuitRefinement` in Witness.lean is the case of one model step per circuit
  step and the invariant `True` (`CircuitRefinement.toInv`).
-/
import GenericNoC.Witness

namespace GenericNoC

/-- `Steps R a l b`: steps of `R` from `a` through the states of `l`, in order,
    to `b`.  At least one step. -/
inductive Steps {α : Type} (R : α → α → Prop) : α → List α → α → Prop where
  | last {a b : α} : R a b → Steps R a [] b
  | cons {a x : α} {l : List α} {b : α} : R a x → Steps R x l b → Steps R a (x :: l) b

/-- The `j`-th state of the sequence `a, l, b`. -/
def segAt {α : Type} : α → List α → α → Nat → α
  | a, _, _, 0 => a
  | _, [], b, _ + 1 => b
  | _, x :: l, b, j + 1 => segAt x l b j

theorem segAt_end {α : Type} (a : α) (l : List α) (b : α) : segAt a l b (l.length + 1) = b := by
  induction l generalizing a with
  | nil => rfl
  | cons x l ih => exact ih x

/-- Every step of the sequence is a step of `R`. -/
theorem Steps.at {α : Type} {R : α → α → Prop} {a : α} {l : List α} {b : α}
    (h : Steps R a l b) : ∀ j, j ≤ l.length → R (segAt a l b j) (segAt a l b (j + 1)) := by
  induction h with
  | last hr =>
    intro j hj
    have : j = 0 := by simp at hj; omega
    subst this
    exact hr
  | @cons a x l b hr _ ih =>
    intro j hj
    cases j with
    | zero => simpa [segAt] using hr
    | succ j => exact ih j (by simp at hj; omega)

/-- Steps of a relation that holds at every step of the sequence. -/
theorem Steps.of_at {α : Type} {R : α → α → Prop} :
    ∀ {a : α} {l : List α} {b : α},
      (∀ j, j ≤ l.length → R (segAt a l b j) (segAt a l b (j + 1))) → Steps R a l b
  | _, [], _, h => .last (h 0 (Nat.le_refl _))
  | a, x :: l, b, h =>
    .cons (by simpa [segAt] using h 0 (Nat.zero_le _)) (Steps.of_at (fun j hj => h (j + 1) (by simp; omega)))

/-! ### One run from the segments of all circuit steps -/

section Flatten

variable {α : Type} (pt : Nat → α) (seg : Nat → List α)

/-- Model steps taken by circuit step `t`. -/
def segLen (t : Nat) : Nat := (seg t).length + 1

/-- Circuit step and position within it of model step `n`. -/
def locate : Nat → Nat × Nat
  | 0 => (0, 0)
  | n + 1 =>
    if (locate n).2 + 1 < segLen seg (locate n).1 then ((locate n).1, (locate n).2 + 1)
    else ((locate n).1 + 1, 0)

/-- The model run: the states of every circuit step's sequence, one after another. -/
def flatRun (n : Nat) : α :=
  segAt (pt (locate seg n).1) (seg (locate seg n).1) (pt ((locate seg n).1 + 1)) (locate seg n).2

/-- The model step at which circuit step `t` starts. -/
def flatPos : Nat → Nat
  | 0 => 0
  | t + 1 => flatPos t + segLen seg t

theorem locate_lt (n : Nat) : (locate seg n).2 < segLen seg (locate seg n).1 := by
  induction n with
  | zero => simp [locate, segLen]
  | succ n ih =>
    unfold locate
    split
    · assumption
    · simp [segLen]

theorem locate_mono (n : Nat) : (locate seg n).1 ≤ (locate seg (n + 1)).1 := by
  show (locate seg n).1 ≤ (locate seg (n + 1)).1
  conv => rhs; unfold locate
  split <;> simp

theorem locate_within {t : Nat} (h : locate seg (flatPos seg t) = (t, 0)) :
    ∀ j, j < segLen seg t → locate seg (flatPos seg t + j) = (t, j) := by
  intro j
  induction j with
  | zero => intro _; exact h
  | succ j ih =>
    intro hj
    have hprev := ih (by omega)
    show locate seg (flatPos seg t + j + 1) = (t, j + 1)
    unfold locate
    rw [hprev]
    simp [hj]

theorem locate_flatPos (t : Nat) : locate seg (flatPos seg t) = (t, 0) := by
  induction t with
  | zero => rfl
  | succ t ih =>
    have hl : 0 < segLen seg t := by simp [segLen]
    have hlast := locate_within seg ih (segLen seg t - 1) (by omega)
    show locate seg (flatPos seg t + segLen seg t) = (t + 1, 0)
    have : flatPos seg t + segLen seg t = flatPos seg t + (segLen seg t - 1) + 1 := by omega
    rw [this]
    unfold locate
    rw [hlast]
    simp only
    rw [if_neg (by omega)]

theorem flatRun_flatPos (t : Nat) : flatRun pt seg (flatPos seg t) = pt t := by
  simp [flatRun, locate_flatPos, segAt]

theorem flatPos_lt (t : Nat) : flatPos seg t < flatPos seg (t + 1) := by
  simp [flatPos, segLen]

theorem locate_ge {τ : Nat} : ∀ k, τ ≤ (locate seg (flatPos seg τ + k)).1
  | 0 => by rw [Nat.add_zero, locate_flatPos]; exact Nat.le_refl _
  | k + 1 => Nat.le_trans (locate_ge k) (locate_mono seg _)

/-- If every circuit step from `τ` on is a sequence of `Q` steps, then every
    model step from `flatPos τ` on is a `Q` step. -/
theorem flatRun_step {Q : α → α → Prop} {τ : Nat}
    (h : ∀ t, τ ≤ t → Steps Q (pt t) (seg t) (pt (t + 1))) :
    ∀ n, flatPos seg τ ≤ n → Q (flatRun pt seg n) (flatRun pt seg (n + 1)) := by
  intro n hn
  obtain ⟨k, rfl⟩ : ∃ k, n = flatPos seg τ + k := ⟨n - flatPos seg τ, by omega⟩
  have hτ := locate_ge seg (τ := τ) k
  generalize hp : flatPos seg τ + k = n at hτ ⊢
  have hlt := locate_lt seg n
  have hsteps := (h _ hτ).at
  rcases hloc : locate seg n with ⟨t, j⟩
  simp only [hloc] at hτ hlt hsteps
  have hnext : locate seg (n + 1) =
      if j + 1 < segLen seg t then (t, j + 1) else (t + 1, 0) := by
    show (if (locate seg n).2 + 1 < segLen seg (locate seg n).1 then
      ((locate seg n).1, (locate seg n).2 + 1) else ((locate seg n).1 + 1, 0)) = _
    rw [hloc]
  unfold flatRun
  rw [hloc, hnext]
  simp only [segLen] at hlt hnext ⊢
  split
  · exact hsteps j (by omega)
  · have hj : j = (seg t).length := by omega
    have := hsteps j (by omega)
    subst hj
    simp only [segAt_end] at this
    simpa [segAt] using this

end Flatten

/-- W5: refinement from a circuit to the model in which one circuit step is a
    nonempty finite sequence of model steps.  The step facts hold inside the
    circuit invariant `inv`. -/
structure CircuitRefinementInv (N : GenericNoC) : Type 1 where
  CState : Type
  CHold : Type
  cinit : CState → Prop
  cstep : CState → CState → Prop
  cown : CState → CHold → Option N.Owner
  cprog : CState → CState → CHold → Prop
  /-- The circuit invariant: it holds at reset and every step keeps it. -/
  inv : CState → Prop
  inv_init : ∀ c, cinit c → inv c
  inv_step : ∀ c c', inv c → cstep c c' → inv c'
  abs : CState → N.State
  /-- The model states a circuit step passes through, after `abs c` and before `abs c'`. -/
  mid : CState → CState → List N.State
  toHold : CHold → N.Hold
  fromHold : N.Hold → CHold
  from_to : ∀ p, fromHold (toHold p) = p
  to_from : ∀ h, toHold (fromHold h) = h
  /-- Initial states map into I. -/
  init_map : ∀ c, cinit c → N.init (abs c)
  /-- A circuit step maps to model steps through `mid`. -/
  step_map : ∀ c c', inv c → cstep c c' → Steps N.step (abs c) (mid c c') (abs c')
  /-- Owner observation commutes with the mapping. -/
  own_eq : ∀ c p, inv c → cown c p = N.own (abs c) (toHold p)
  /-- A circuit step makes progress at a point exactly when one of its model steps does. -/
  prog_eq : ∀ c c' p, inv c → cstep c c' →
    (cprog c c' p ↔ ¬ Steps (fun σ σ' => ¬ N.prog σ σ' (toHold p)) (abs c) (mid c c') (abs c'))

namespace CircuitRefinementInv

variable {N : GenericNoC} (R : CircuitRefinementInv N)

/-- The model run of a circuit run. -/
def run (cs : Nat → R.CState) : Nat → N.State :=
  flatRun (fun t => R.abs (cs t)) (fun t => R.mid (cs t) (cs (t + 1)))

/-- The model step at which circuit step `t` starts. -/
def pos (cs : Nat → R.CState) : Nat → Nat :=
  flatPos (fun t => R.mid (cs t) (cs (t + 1)))

theorem inv_run {cs : Nat → R.CState} (h0 : R.cinit (cs 0))
    (hstep : ∀ t, R.cstep (cs t) (cs (t + 1))) : ∀ t, R.inv (cs t)
  | 0 => R.inv_init _ h0
  | t + 1 => R.inv_step _ _ (inv_run h0 hstep t) (hstep t)

/-- Circuit step `t` starts at the image of its state. -/
theorem run_pos (cs : Nat → R.CState) (t : Nat) : R.run cs (R.pos cs t) = R.abs (cs t) :=
  flatRun_flatPos _ _ t

theorem pos_lt (cs : Nat → R.CState) (t : Nat) : R.pos cs t < R.pos cs (t + 1) :=
  flatPos_lt _ t

/-- A legal circuit run maps to a legal model run. -/
theorem map_legal {cs : Nat → R.CState} (h0 : R.cinit (cs 0))
    (hstep : ∀ t, R.cstep (cs t) (cs (t + 1))) : N.Legal (R.run cs) := by
  refine ⟨?_, fun n => ?_⟩
  · have := R.run_pos cs 0
    simp only [pos, flatPos] at this
    rw [this]
    exact R.init_map _ h0
  · exact flatRun_step _ _ (τ := 0)
      (fun t _ => R.step_map (cs t) (cs (t + 1)) (R.inv_run h0 hstep t) (hstep t)) n
      (Nat.zero_le _)

/-- Permanent stall of a physical hold point. -/
def CStall (cs : Nat → R.CState) (τ : Nat) (p : R.CHold) : Prop :=
  ∀ t, τ ≤ t → (R.cown (cs t) p).isSome ∧ ¬ R.cprog (cs t) (cs (t + 1)) p

/-- A physical stall maps to a model stall from the model step at which it starts. -/
theorem stall_map {cs : Nat → R.CState} (h0 : R.cinit (cs 0))
    (hstep : ∀ t, R.cstep (cs t) (cs (t + 1))) {τ : Nat} {p : R.CHold}
    (h : R.CStall cs τ p) : N.Stall (R.run cs) (R.pos cs τ) (R.toHold p) := by
  have hinv := R.inv_run h0 hstep
  have hlegal := R.map_legal h0 hstep
  have quiet : ∀ n, R.pos cs τ ≤ n → ¬ N.prog (R.run cs n) (R.run cs (n + 1)) (R.toHold p) :=
    flatRun_step _ _ (Q := fun σ σ' => ¬ N.prog σ σ' (R.toHold p)) (τ := τ) (fun t ht => by
      have hq := (h t ht).2
      rw [R.prog_eq (cs t) (cs (t + 1)) p (hinv t) (hstep t)] at hq
      exact Classical.byContradiction hq)
  have held : ∀ k, (N.own (R.run cs (R.pos cs τ + k)) (R.toHold p)).isSome := by
    intro k
    induction k with
    | zero =>
      rw [Nat.add_zero, R.run_pos, ← R.own_eq _ _ (hinv τ)]
      exact (h τ (Nat.le_refl _)).1
    | succ k ih =>
      have hs := N.own_stable (hlegal.2 (R.pos cs τ + k)) ih (quiet _ (by omega))
      rw [← Nat.add_assoc, hs]
      exact ih
  intro n hn
  obtain ⟨k, rfl⟩ : ∃ k, n = R.pos cs τ + k := ⟨n - R.pos cs τ, by omega⟩
  exact ⟨held k, quiet _ hn⟩

end CircuitRefinementInv

/-- A refinement with one model step per circuit step is the special case of no
    intermediate states and the invariant `True`. -/
def CircuitRefinement.toInv {N : GenericNoC} (R : CircuitRefinement N) :
    CircuitRefinementInv N where
  CState := R.CState
  CHold := R.CHold
  cinit := R.cinit
  cstep := R.cstep
  cown := R.cown
  cprog := R.cprog
  inv := fun _ => True
  inv_init := fun _ _ => trivial
  inv_step := fun _ _ _ _ => trivial
  abs := R.abs
  mid := fun _ _ => []
  toHold := R.toHold
  fromHold := R.fromHold
  from_to := R.from_to
  to_from := R.to_from
  init_map := R.init_map
  step_map := fun c c' _ h => .last (R.step_map c c' h)
  own_eq := fun c p _ => R.own_eq c p
  prog_eq := fun c c' p _ _ => by
    rw [R.prog_eq]
    constructor
    · intro hp hs
      cases hs with
      | last hq => exact hq hp
    · intro hn
      exact Classical.byContradiction fun hq => hn (.last hq)

/-- With one model step per circuit step, the model run is the image of the circuit run. -/
theorem CircuitRefinement.toInv_run {N : GenericNoC} (R : CircuitRefinement N)
    (cs : Nat → R.CState) (t : Nat) : R.toInv.run cs t = R.abs (cs t) := by
  have hpos : ∀ t, R.toInv.pos cs t = t := by
    intro t
    induction t with
    | zero => rfl
    | succ t ih =>
      show flatPos _ t + segLen _ t = t + 1
      exact (congrArg (· + 1) ih)
  have := R.toInv.run_pos cs t
  rwa [hpos] at this

end GenericNoC
