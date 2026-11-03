/-
  GenericNoC.Toy: the two-slot chain, discharged end to end.

  An upstream point `up` needs the downstream slot `dn` to be free, and the
  environment serves `dn`.  The graph is node(dn) = 0 -> node(up) = 1 with the
  identity rank.  All W2, W3, W6 and W7 fields are proved, so `toy_deadlockFree`
  closes the final goal for this instance with no assumption left open.  The
  ring negative control is in Checkers.lean.

  The toy has one clock, so W3 is proved directly and does not use W4.
-/
import GenericNoC.Theorems
import GenericNoC.Checkers

namespace ToyChain

open GenericNoC

/-- Hold points of the two-slot chain. -/
inductive ToyHold where
  | up | dn
  deriving DecidableEq, Repr

/-- State: (upstream occupied, downstream occupied). -/
abbrev ToyState := Bool × Bool

/-- Transitions: stutter, fire up (needs the downstream slot free, moves the
    occupant downstream), fire down (environment drains the slot). -/
abbrev stepRel (σ σ' : ToyState) : Prop :=
  σ' = σ ∨
    (σ.1 = true ∧ σ.2 = false ∧ σ' = (false, true)) ∨
    (σ.2 = true ∧ σ' = (σ.1, false))

abbrev ownFn (σ : ToyState) (h : ToyHold) : Option Nat :=
  match h with
  | .up => if σ.1 = true then some 0 else none
  | .dn => if σ.2 = true then some 0 else none

abbrev progFn (σ σ' : ToyState) (h : ToyHold) : Prop :=
  match h with
  | .up => σ.1 = true ∧ σ'.1 = false
  | .dn => σ.2 = true ∧ σ'.2 = false

/-- Up needs dn's capacity; nothing needs anything else. -/
abbrev needFn (_σ : ToyState) (x y : ToyHold) : Prop := x = .dn ∧ y = .up
abbrev permitFn (σ : ToyState) (x y : ToyHold) : Prop := needFn σ x y → σ.2 = false
abbrev readyFn (_σ : ToyState) (_y : ToyHold) : Prop := True
abbrev enFn (σ : ToyState) (y : ToyHold) : Prop := ∀ x, needFn σ x y → permitFn σ x y

/-- External service: an occupied point whose enable holds is eventually
    served (weak fairness per point; `ready` is constant true here). -/
abbrev envFn (s : Nat → ToyState) : Prop :=
  (∀ t, (s t).1 = true → (s t).2 = false →
    ∃ t', t ≤ t' ∧ progFn (s t') (s (t' + 1)) .up) ∧
  (∀ t, (s t).2 = true →
    ∃ t', t ≤ t' ∧ progFn (s t') (s (t' + 1)) .dn)

abbrev classOfFn : ToyHold → Fin 2
  | .dn => 0
  | .up => 1

/-- The single dependency edge: the downstream slot blocks the upstream point
    (blocker → blocked). -/
abbrev edgeFn (u v : Fin 2) : Prop := u = 0 ∧ v = 1

theorem own_up_isSome {σ : ToyState} : (ownFn σ .up).isSome ↔ σ.1 = true := by
  cases h1 : σ.1 <;> simp [ownFn, h1]

theorem own_dn_isSome {σ : ToyState} : (ownFn σ .dn).isSome ↔ σ.2 = true := by
  cases h2 : σ.2 <;> simp [ownFn, h2]

/-- With the downstream slot empty and up not progressing, the slot stays
    empty (fire up is the only filler, and it is a progress of up). -/
theorem dn_stays_empty {σ σ' : ToyState} (hstep : stepRel σ σ') (h2 : σ.2 = false)
    (hnup : ¬ progFn σ σ' .up) : σ'.2 = false := by
  cases hstep with
  | inl h => cases h; exact h2
  | inr h =>
    cases h with
    | inl hfu =>
      obtain ⟨h1, _, hσ'⟩ := hfu
      exact absurd ⟨h1, by simp [hσ']⟩ hnup
    | inr hfd =>
      obtain ⟨h2t, _⟩ := hfd
      rw [h2] at h2t
      exact absurd h2t (by decide)

/-- An occupied downstream slot that does not progress stays occupied. -/
theorem dn_stable {σ σ' : ToyState} (hstep : stepRel σ σ') (h2 : σ.2 = true)
    (hn : ¬ progFn σ σ' .dn) : σ'.2 = true := by
  cases hstep with
  | inl h => cases h; exact h2
  | inr h =>
    cases h with
    | inl hfu =>
      obtain ⟨_, h2f, _⟩ := hfu
      rw [h2f] at h2
      exact absurd h2 (by decide)
    | inr hfd =>
      obtain ⟨h2t, hσ'⟩ := hfd
      exact absurd ⟨h2t, by simp [hσ']⟩ hn

/-- The toy model as a `GenericNoC` (W1), with the hold-point axioms and the
    rank certificate discharged by finite checks. -/
abbrev toyNoC : GenericNoC where
  State := ToyState
  Hold := ToyHold
  Node := Fin 2
  Component := Unit
  Owner := Nat
  init := fun _ => True
  step := stepRel
  own := ownFn
  prog := progFn
  need := needFn
  permit := permitFn
  ready := readyFn
  en := enFn
  en_iff := by
    intro σ y
    exact ⟨fun h => ⟨True.intro, h⟩, fun h => h.2⟩
  prog_held_en := by
    intro σ σ' h hstep hprog
    cases h with
    | up =>
      obtain ⟨h1, h2⟩ := hprog
      constructor
      · simp [ownFn, h1]
      · intro x hneed
        obtain ⟨rfl, _⟩ := hneed
        intro _
        cases hstep with
        | inl h =>
          cases h
          rw [h1] at h2
          exact absurd h2 (by decide)
        | inr h =>
          cases h with
          | inl hfu => exact hfu.2.1
          | inr hfd =>
            obtain ⟨_, hσ'⟩ := hfd
            simp [hσ'] at h2
            rw [h1] at h2
            exact absurd h2 (by decide)
    | dn =>
      obtain ⟨h2t, _⟩ := hprog
      constructor
      · simp [ownFn, h2t]
      · intro x hneed
        obtain ⟨_, hy⟩ := hneed
        exact absurd hy (by decide : ¬ (ToyHold.dn = ToyHold.up))
  own_stable := by
    intro σ σ' h hstep hheld hnprog
    cases h with
    | up =>
      cases hstep with
      | inl h => cases h; rfl
      | inr h =>
        cases h with
        | inl hfu =>
          obtain ⟨h1, _, hσ'⟩ := hfu
          exact absurd ⟨h1, by simp [hσ']⟩ hnprog
        | inr hfd =>
          obtain ⟨_, hσ'⟩ := hfd
          simp [ownFn, hσ']
    | dn =>
      cases hstep with
      | inl h => cases h; rfl
      | inr h =>
        cases h with
        | inl hfu =>
          obtain ⟨_, h2f, _⟩ := hfu
          simp [ownFn, h2f] at hheld
        | inr hfd =>
          obtain ⟨h2t, hσ'⟩ := hfd
          exact absurd ⟨h2t, by simp [hσ']⟩ hnprog
  componentOf := fun _ => ()
  classOf := classOfFn
  edge := edgeFn
  graphRank := fun v => v.val
  localRank := fun _ _ => 0
  edge_rank := by decide
  env := envFn

/-- W2 for the toy: the only real block is dn ⊣ up, covered by the single
    edge; the same-node case is vacuous. -/
def toyStructural : StructuralInstanceLemmas toyNoC where
  block_cross := by
    intro k s hlegal t x y hk hb hne
    obtain ⟨_, _, hneed, _⟩ := hb
    obtain ⟨rfl, rfl⟩ := hneed
    exact GenericNoC.Reaches.single ⟨rfl, rfl⟩
  block_local := by
    intro k s hlegal t x y hk hb heq
    obtain ⟨_, _, hneed, _⟩ := hb
    obtain ⟨rfl, rfl⟩ := hneed
    exact absurd heq (by decide)

/-- W3 for the toy: the local rank is constant, and a
    permanently stalled up eventually shows the permanently stalled dn as its
    direct blocker — dn itself cannot stall, the environment serves
    it. -/
def toyInstance : InstanceLemmas toyNoC where
  rank_stable := by
    intro k s hlegal τ y hk hstall t ht
    rfl
  eventual_blocker := by
    intro k s hlegal henv τ y hk hstall
    cases y with
    | dn =>
      exfalso
      have hheld : (s τ).2 = true := own_dn_isSome.1 (hstall τ (Nat.le_refl τ)).1
      obtain ⟨t', ht', hprog⟩ := henv.2 τ hheld
      exact (hstall t' ht').2 hprog
    | up =>
      have hup : ∀ t, τ ≤ t → (s t).1 = true ∧ ¬ progFn (s t) (s (t + 1)) .up := by
        intro t ht
        obtain ⟨hheld, hnprog⟩ := hstall t ht
        exact ⟨own_up_isSome.1 hheld, hnprog⟩
      have hdn_occ : ∃ t₀, τ ≤ t₀ ∧ (s t₀).2 = true := by
        apply Classical.byContradiction
        intro hno
        have hdf : ∀ t, τ ≤ t → (s t).2 = false := by
          intro t ht
          cases h2 : (s t).2 with
          | false => rfl
          | true => exact absurd ⟨t, ht, h2⟩ hno
        obtain ⟨t', ht', hprog⟩ := henv.1 τ (hup τ (Nat.le_refl τ)).1 (hdf τ (Nat.le_refl τ))
        exact (hup t' ht').2 hprog
      obtain ⟨t₀, ht₀, hocc₀⟩ := hdn_occ
      have hdn_noprog : ∀ t, t₀ ≤ t → ¬ progFn (s t) (s (t + 1)) .dn := by
        intro t ht hprog
        have hfalse1 : (s (t + 1)).2 = false := hprog.2
        have hforever : ∀ k, (s (t + 1 + k)).2 = false := by
          intro k
          induction k with
          | zero => exact hfalse1
          | succ k ih =>
            exact dn_stays_empty (hlegal.2 (t + 1 + k)) ih (hup (t + 1 + k) (by omega)).2
        have hoccU : (s (t + 1)).1 = true := (hup (t + 1) (by omega)).1
        obtain ⟨t', ht', hprogU⟩ := henv.1 (t + 1) hoccU hfalse1
        exact (hup t' (by omega)).2 hprogU
      have hdn_holds : ∀ k, (s (t₀ + k)).2 = true := by
        intro k
        induction k with
        | zero => exact hocc₀
        | succ k ih =>
          exact dn_stable (hlegal.2 (t₀ + k)) ih (hdn_noprog (t₀ + k) (by omega))
      refine ⟨t₀, ht₀, .dn, ⟨?_, ?_, ?_, ?_⟩, ?_⟩
      · exact own_dn_isSome.2 hocc₀
      · exact own_up_isSome.2 (hup t₀ ht₀).1
      · exact ⟨rfl, rfl⟩
      · intro hperm
        exact absurd (hperm ⟨rfl, rfl⟩) (by rw [hocc₀]; decide)
      · intro t ht
        refine ⟨own_dn_isSome.2 ?_, hdn_noprog t ht⟩
        have ht' := hdn_holds (t - t₀)
        have hteq : t₀ + (t - t₀) = t := by omega
        rw [hteq] at ht'
        exact ht'

/-- The deterministic drain-first schedule from any state; each step is a
    legal transition. -/
abbrev nextState (σ : ToyState) : ToyState :=
  if σ.2 = true then (σ.1, false) else if σ.1 = true then (false, true) else σ

theorem nextState_step {σ : ToyState} : stepRel σ (nextState σ) := by
  cases h2 : σ.2 with
  | true =>
    refine Or.inr (Or.inr ⟨h2, ?_⟩)
    simp [nextState, h2]
  | false =>
    cases h1 : σ.1 with
    | true =>
      refine Or.inr (Or.inl ⟨h1, h2, ?_⟩)
      simp [nextState, h2, h1]
    | false =>
      refine Or.inl ?_
      simp [nextState, h2, h1]

/-- The scheduled trajectory is served immediately: an occupied point whose
    enable holds progresses at the very next step, so both Env conjuncts hold
    for every suffix. -/
theorem env_iter (a : ToyState) : envFn (fun n => iter nextState n a) := by
  constructor
  · intro t h1 h2
    refine ⟨t, Nat.le_refl t, ?_⟩
    show progFn (iter nextState t a) (nextState (iter nextState t a)) .up
    exact ⟨h1, by simp [nextState, h2, h1]⟩
  · intro t h2
    refine ⟨t, Nat.le_refl t, ?_⟩
    show progFn (iter nextState t a) (nextState (iter nextState t a)) .dn
    exact ⟨h2, by simp [nextState, h2]⟩

/-- W6 for the toy: the drain-first schedule through the state; every suffix
    is again such a schedule (iter_add), hence satisfies Env. -/
def toyService : ServiceContinuation toyNoC where
  extend := by
    intro σ hreach
    refine ⟨fun n => iter nextState n σ, 0, ⟨trivial, fun t => nextState_step⟩, env_iter σ,
      rfl, ?_⟩
    intro i
    show envFn (fun n => iter nextState (n + i) σ)
    have heq : (fun n => iter nextState (n + i) σ) =
        fun n => iter nextState n (iter nextState i σ) :=
      funext fun n => iter_add nextState n i σ
    rw [heq]
    exact env_iter _

/-- W7 for the toy: one transaction kind with token 0; pending means some
    slot is occupied. -/
abbrev pendingFn (σ : ToyState) (_q : Unit) : Prop := σ.1 = true ∨ σ.2 = true

def toyWork : AcceptedWorkInventory toyNoC where
  Work := Unit
  pending := pendingFn
  tok := fun _ => 0
  token_unique := by
    intro σ hreach q q' _ _ _
    exact Subsingleton.elim q q'

/-- The toy discharges the pending-held assumption by proof. -/
theorem toyHeld : PendingHeld toyWork where
  held := by
    intro σ hreach q hp
    cases hp with
    | inl h1 => exact ⟨.up, by show ownFn σ .up = some 0; simp [ownFn, h1]⟩
    | inr h2 => exact ⟨.dn, by show ownFn σ .dn = some 0; simp [ownFn, h2]⟩

/-- Every toy state is reachable (stutter trajectory; the toy takes all
    states as initial). -/
theorem toy_reachable (σ : ToyState) : toyNoC.Reachable σ :=
  ⟨fun _ => σ, ⟨trivial, fun _ => Or.inl rfl⟩, 0, rfl⟩

/-- End-to-end: the two-slot chain is deadlock-free. -/
theorem toy_deadlockFree : toyNoC.DeadlockFree toyWork :=
  deadlock_free toyNoC toyStructural toyInstance toyService toyWork toyHeld

/-- A concrete episode: the state (upstream occupied, downstream free) with
    its pending unit of work admits a compliant advancing execution. -/
theorem toy_episode_example :
    ∃ (h : ToyHold) (f : Nat → ToyState) (t : Nat),
      toyNoC.own (true, false) h = some 0 ∧
      f 0 = (true, false) ∧
      (∀ j, toyNoC.step (f j) (f (j + 1))) ∧
      toyNoC.env f ∧
      (∀ j, j ≤ t → toyNoC.own (f j) h = some 0) ∧
      toyNoC.prog (f t) (f (t + 1)) h :=
  toy_deadlockFree (true, false) (toy_reachable _) () (Or.inl rfl)

-- T1 and T2 instantiated on the toy, for the record.
example (s : Nat → toyNoC.State) (hlegal : toyNoC.Legal s) (henv : toyNoC.env s)
    (τ : Nat) (y : toyNoC.Hold) : ¬ toyNoC.Stall s τ y :=
  no_stall toyNoC toyStructural toyInstance hlegal henv τ y

example (s : Nat → toyNoC.State) (hlegal : toyNoC.Legal s) (t : Nat)
    (W : toyNoC.Hold → Prop) (y₀ : toyNoC.Hold) (hy₀ : W y₀)
    (hclosed : ∀ y, W y → ∃ x, W x ∧ toyNoC.blocked (s t) x y) : False :=
  noClosedResourceWait toyNoC toyStructural hlegal t hy₀ hclosed

#print axioms noClosedResourceWait
#print axioms no_stall
#print axioms held_episode_can_advance
#print axioms deadlock_free
#print axioms TimedInstanceLemmas.toInstance
#print axioms toy_deadlockFree

end ToyChain
