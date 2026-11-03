/-
  GenericNoC.Witness: the obligations an instance must discharge, as records:
  W2 structural lemmas, W3 timed lemmas, W4 clock and power events, W5
  refinement to a circuit, W6 service continuation, W7 accepted work.  It also
  holds the final goal `DeadlockFree`.

  Every record field is a hypothesis.  The generic theorems in Theorems.lean
  consume exactly these fields, and an instance must prove them or state them
  as assumptions.
-/
import GenericNoC.Model

namespace GenericNoC

/-- W2: per-component structural lemmas.  `block_cross` maps a real
    cross-class block to a dependency-graph path; `block_local` gives
    strict local-rank descent within one node.  Quantified per component k
    owning the blocked point y. -/
structure StructuralInstanceLemmas (N : GenericNoC) : Prop where
  block_cross : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (t : Nat) (x y : N.Hold), N.componentOf y = k →
      N.blocked (s t) x y → N.classOf x ≠ N.classOf y →
      N.Reaches (N.classOf x) (N.classOf y)
  block_local : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (t : Nat) (x y : N.Hold), N.componentOf y = k →
      N.blocked (s t) x y → N.classOf x = N.classOf y →
      N.localRank (s t) x < N.localRank (s t) y

/-- W2 split by graph rule.  Every real block in a legal run is an instance of
    a named rule (`exhaustive`), and each rule's instances are connected in the
    dependency graph (`covered`).  Instances give `covered` as one lemma per
    rule, so what is proved and what is assumed can be read off the rule type.
    A rule that holds no lemma makes this record impossible to build. -/
structure RuleCover (N : GenericNoC) where
  Rule : Type
  fires : Rule → N.State → N.Hold → N.Hold → Prop
  exhaustive : ∀ (s : Nat → N.State), N.Legal s → ∀ (t : Nat) (x y : N.Hold),
    N.blocked (s t) x y → ∃ r, fires r (s t) x y
  covered : ∀ (r : Rule) (σ : N.State) (x y : N.Hold), fires r σ x y →
    N.Reaches (N.classOf x) (N.classOf y)

/-- A rule cover gives W2.  Same-node blocks cannot occur, because a covered
    block is a positive-length path and the rank certificate excludes a path
    from a node to itself. -/
theorem RuleCover.toStructural {N : GenericNoC} (R : RuleCover N) :
    StructuralInstanceLemmas N where
  block_cross := by
    intro _ s hlegal t x y _ hb _
    obtain ⟨r, hr⟩ := R.exhaustive s hlegal t x y hb
    exact R.covered r _ x y hr
  block_local := by
    intro _ s hlegal t x y _ hb heq
    obtain ⟨r, hr⟩ := R.exhaustive s hlegal t x y hb
    have hp := R.covered r _ x y hr
    rw [heq] at hp
    exact absurd hp (fun h => Reaches.no_self N h)

/-- W3: per-component timed lemmas.  `rank_stable` fixes the blocked point's local rank over
    a stall; `eventual_blocker` traces a permanent stall to a
    permanent-stall direct blocker.  Env appears only in the premise of `eventual_blocker`. -/
structure InstanceLemmas (N : GenericNoC) : Prop where
  rank_stable : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (τ : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      ∀ t, τ ≤ t → N.localRank (s t) y = N.localRank (s τ) y
  eventual_blocker : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    N.env s → ∀ (τ : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      ∃ t, τ ≤ t ∧ ∃ x : N.Hold, N.blocked (s t) x y ∧ N.Stall s t x

/-- W4: the clock/power event interface for cross-domain instances.
    `edgeValid`/`powered`/`resetDone` are the per-domain qualifiers
    (their conjunction is Q); `syncDone` is the settled-control observation D.
    Same-domain profiles do not route through this record: single-clock evidence
    (single clock, common reset, no gating) discharges W3 directly. -/
structure TimedInstanceLemmas (N : GenericNoC) : Type 1 where
  Dom : Type
  dom : N.Hold → Dom
  edgeValid : Dom → Nat → Prop
  powered : Dom → Nat → Prop
  resetDone : Dom → Nat → Prop
  syncDone : N.Hold → Nat → Prop
  /-- Progress implies a qualified edge (prog ⇒ Q). -/
  prog_qualified : ∀ (s : Nat → N.State), N.Legal s → ∀ (t : Nat) (h : N.Hold),
    N.prog (s t) (s (t + 1)) h →
      edgeValid (dom h) t ∧ powered (dom h) t ∧ resetDone (dom h) t
  /-- Without a valid edge, owner and local rank persist. -/
  off_edge_stable : ∀ (s : Nat → N.State), N.Legal s → ∀ (t : Nat) (h : N.Hold),
    ¬ edgeValid (dom h) t →
      N.own (s (t + 1)) h = N.own (s t) h ∧
      N.localRank (s (t + 1)) h = N.localRank (s t) h
  /-- `rank_stable`, delivered per instance. -/
  rank_stable : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (τ : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      ∀ t, τ ≤ t → N.localRank (s t) y = N.localRank (s τ) y
  /-- A domain holding a stalled object keeps producing qualified
      edges (empty domains may stop). -/
  active_fair : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s → N.env s →
    ∀ (τ : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      ∀ a, τ ≤ a → ∃ t, a ≤ t ∧
        edgeValid (dom y) t ∧ powered (dom y) t ∧ resetDone (dom y) t
  /-- The cross-domain control for a stalled object becomes visible. -/
  settles : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s → N.env s →
    ∀ (τ : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      ∃ t, τ ≤ t ∧ syncDone y t
  /-- And it stays visible over the stall suffix. -/
  settled_persists : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (τ t : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      syncDone y t → τ ≤ t → ∀ u, t ≤ u → syncDone y u
  /-- At a settled qualified edge, a stalled blocker is exposed. -/
  blocker_at_settled_edge : ∀ (k : N.Component) (s : Nat → N.State), N.Legal s →
    ∀ (τ t : Nat) (y : N.Hold), N.componentOf y = k → N.Stall s τ y →
      edgeValid (dom y) t → powered (dom y) t → resetDone (dom y) t →
      syncDone y t → τ ≤ t →
      ∃ x : N.Hold, N.blocked (s t) x y ∧ N.Stall s t x

/-- Take the settling time from `settles`, then a
    qualified edge at or after it from `active_fair`; persistence carries D
    forward and `blocker_at_settled_edge` exposes the stalled blocker, which is
    `eventual_blocker`.  Holds for any
    instance and any domain set. -/
def TimedInstanceLemmas.toInstance {N : GenericNoC} (T : TimedInstanceLemmas N) :
    InstanceLemmas N where
  rank_stable := T.rank_stable
  eventual_blocker := by
    intro k s hlegal henv τ y hk hstall
    obtain ⟨t, htτ, hdt⟩ := T.settles k s hlegal henv τ y hk hstall
    obtain ⟨u, htu, he, hp, hr⟩ := T.active_fair k s hlegal henv τ y hk hstall t htτ
    have hdu : T.syncDone y u :=
      T.settled_persists k s hlegal τ t y hk hstall hdt htτ u htu
    obtain ⟨x, hb, hs⟩ :=
      T.blocker_at_settled_edge k s hlegal τ u y hk hstall he hp hr hdu
        (Nat.le_trans htτ htu)
    exact ⟨u, Nat.le_trans htτ htu, x, hb, hs⟩

/-- W5: refinement from the physical circuit to the abstract model.
    `abs` is the state observation, `toHold`/`fromHold` the resource
    bijection, and the observational equalities hold per step. -/
structure CircuitRefinement (N : GenericNoC) : Type 1 where
  CState : Type
  CHold : Type
  cinit : CState → Prop
  cstep : CState → CState → Prop
  cown : CState → CHold → Option N.Owner
  cprog : CState → CState → CHold → Prop
  abs : CState → N.State
  toHold : CHold → N.Hold
  fromHold : N.Hold → CHold
  from_to : ∀ p, fromHold (toHold p) = p
  to_from : ∀ h, toHold (fromHold h) = h
  /-- Initial states map into I. -/
  init_map : ∀ c, cinit c → N.init (abs c)
  /-- Circuit steps map into T. -/
  step_map : ∀ c c', cstep c c' → N.step (abs c) (abs c')
  /-- Owner observation commutes with the mapping. -/
  own_eq : ∀ c p, cown c p = N.own (abs c) (toHold p)
  /-- Progress events correspond. -/
  prog_eq : ∀ c c' p, cprog c c' p ↔ N.prog (abs c) (abs c') (toHold p)

namespace CircuitRefinement

/-- A legal circuit trajectory maps to a legal abstract trajectory. -/
theorem map_legal {N : GenericNoC} (R : CircuitRefinement N)
    {cs : Nat → R.CState} (h0 : R.cinit (cs 0))
    (hstep : ∀ t, R.cstep (cs t) (cs (t + 1))) :
    N.Legal (fun t => R.abs (cs t)) :=
  ⟨R.init_map _ h0, fun t => R.step_map _ _ (hstep t)⟩

/-- Permanent stall of a physical hold point. -/
def CStall {N : GenericNoC} (R : CircuitRefinement N)
    (cs : Nat → R.CState) (τ : Nat) (p : R.CHold) : Prop :=
  ∀ t, τ ≤ t → (R.cown (cs t) p).isSome ∧ ¬ R.cprog (cs t) (cs (t + 1)) p

/-- A physical stall maps to an abstract stall. -/
theorem stall_map {N : GenericNoC} (R : CircuitRefinement N)
    (cs : Nat → R.CState) (τ : Nat) (p : R.CHold)
    (h : R.CStall cs τ p) :
    N.Stall (fun t => R.abs (cs t)) τ (R.toHold p) := by
  intro t ht
  obtain ⟨hheld, hnprog⟩ := h t ht
  refine ⟨?_, ?_⟩
  · rw [Option.isSome_iff_exists] at hheld ⊢
    obtain ⟨o, ho⟩ := hheld
    exact ⟨o, by rw [← R.own_eq]; exact ho⟩
  · exact fun hp => hnprog ((R.prog_eq _ _ _).2 hp)

end CircuitRefinement

/-- W6: for every reachable state, a complete legal execution passes through
    it; the whole execution and every suffix satisfy the external service
    condition. -/
structure ServiceContinuation (N : GenericNoC) : Prop where
  extend : ∀ σ, N.Reachable σ →
    ∃ (g : Nat → N.State) (j : Nat), N.Legal g ∧ N.env g ∧ g j = σ ∧
      ∀ i, N.env (fun n => g (n + i))

/-- W7: the accepted-work inventory: `pending`, the owner token `tok` that
    carries the allocation generation, and token uniqueness among
    simultaneously pending work. -/
structure AcceptedWorkInventory (N : GenericNoC) : Type 1 where
  Work : Type
  pending : N.State → Work → Prop
  tok : Work → N.Owner
  token_unique : ∀ σ, N.Reachable σ → ∀ q q',
    pending σ q → pending σ q' → tok q = tok q' → q = q'

/-- Explicit assumption, not part of any graph artifact: every pending work
    holds a physical point by its token in the current state.  The dependency
    graph says nothing about this.  It is discharged per instance, either by a
    proof over the instance's state (`Toy` does this) or by bounded-liveness
    runs on generated RTL, which is evidence and not a proof in this model. -/
structure PendingHeld {N : GenericNoC} (W7 : AcceptedWorkInventory N) : Prop where
  held : ∀ σ, N.Reachable σ → ∀ q, W7.pending σ q →
    ∃ h : N.Hold, N.own σ h = some (W7.tok q)

/-- For every reachable
    state and every pending work, some hold point owned by the work's token
    has a compliant follow-up execution on which the same token holds the
    point throughout and a real progress event occurs. -/
def DeadlockFree (N : GenericNoC) (W7 : AcceptedWorkInventory N) : Prop :=
  ∀ σ, N.Reachable σ → ∀ q, W7.pending σ q →
    ∃ (h : N.Hold) (f : Nat → N.State) (t : Nat),
      N.own σ h = some (W7.tok q) ∧
      f 0 = σ ∧
      (∀ j, N.step (f j) (f (j + 1))) ∧
      N.env f ∧
      (∀ j, j ≤ t → N.own (f j) h = some (W7.tok q)) ∧
      N.prog (f t) (f (t + 1)) h

end GenericNoC
