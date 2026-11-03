/-
  GenericNoC.Theorems: the generic theorems, proved once for every instance.

  T1 `noClosedResourceWait`: well-founded descent in a fixed state.
  T2 `no_stall`: eventual blockers would give an infinite descent.
  T5 `held_episode_can_advance`: earliest progress step of the same owner.
  T6 `deadlock_free`: T2 and T5 combined with W6 and W7.
  `advances_on_every_compliant_run`: T2 and T5 on any compliant run.

  The per-instance obligations enter only as the hypothesis records of
  Witness.lean.
-/
import GenericNoC.Witness

namespace GenericNoC

/-- Rank factoring used by both T1 and T2: a direct block strictly decreases
    the composite lexicographic rank from the blocked to the blocker.
    Cross-class: `block_cross` gives a path, `edge_rank` iterated gives the strict global
    drop.  Same-class: `block_local` gives the strict local drop. -/
theorem blocked_rank (N : GenericNoC) (str : StructuralInstanceLemmas N)
    {s : Nat → N.State} (hlegal : N.Legal s) {t : Nat} {x y : N.Hold}
    (hb : N.blocked (s t) x y) :
    N.graphRank (N.classOf x) < N.graphRank (N.classOf y) ∨
      (N.classOf x = N.classOf y ∧
        N.localRank (s t) x < N.localRank (s t) y) := by
  by_cases hμ : N.classOf x = N.classOf y
  · exact Or.inr ⟨hμ, str.block_local _ _ hlegal _ _ _ rfl hb hμ⟩
  · exact Or.inl (Reaches.rank_lt N (str.block_cross _ _ hlegal _ _ _ rfl hb hμ))

/-- T1: in any
    state of a legal trajectory there is no nonempty closed wait set — no W
    such that every member is directly blocked by another member.  Proof by
    well-founded induction on the composite rank at the fixed state. -/
theorem noClosedResourceWait (N : GenericNoC) (str : StructuralInstanceLemmas N)
    {s : Nat → N.State} (hlegal : N.Legal s) (t : Nat)
    {W : N.Hold → Prop} {y₀ : N.Hold} (hy₀ : W y₀)
    (hclosed : ∀ y, W y → ∃ x, W x ∧ N.blocked (s t) x y) : False := by
  let ρ : N.Hold → Nat × Nat :=
    fun h => (N.graphRank (N.classOf h), N.localRank (s t) h)
  have key : ∀ p, Acc NatLex p → ∀ y, ρ y = p → W y → False := by
    intro p hp
    induction hp with
    | intro p _ ih =>
      intro y hρ hy
      obtain ⟨x, hx, hbx⟩ := hclosed y hy
      have hlt : NatLex (ρ x) (ρ y) := by
        cases blocked_rank N str hlegal hbx with
        | inl h => exact Or.inl h
        | inr h => exact Or.inr ⟨congrArg N.graphRank h.1, h.2⟩
      exact ih (ρ x) (hρ ▸ hlt) x rfl hx
  exact key (ρ y₀) (natLexWf.apply _) y₀ rfl hy₀

/-- T1 corollary over reachability. -/
theorem noClosedResourceWait_reachable (N : GenericNoC)
    (str : StructuralInstanceLemmas N) {σ : N.State} (hσ : N.Reachable σ)
    {W : N.Hold → Prop} {y₀ : N.Hold} (hy₀ : W y₀)
    (hclosed : ∀ y, W y → ∃ x, W x ∧ N.blocked σ x y) : False := by
  obtain ⟨s, hlegal, t, rfl⟩ := hσ
  exact noClosedResourceWait N str hlegal t hy₀ hclosed

/-- T2: on a legal trajectory
    satisfying the environment condition, no hold point stalls permanently.
    eventual_blocker iterates to an infinite chain whose composite
    ranks strictly descend — rank_stable restores the blocked point's
    rank at the later time — excluded by well-foundedness of the lex order. -/
theorem no_stall (N : GenericNoC) (str : StructuralInstanceLemmas N)
    (inst : InstanceLemmas N) {s : Nat → N.State} (hlegal : N.Legal s)
    (henv : N.env s) :
    ∀ τ y, ¬ N.Stall s τ y := by
  intro τ y hstall
  let A := { p : Nat × N.Hold // τ ≤ p.1 ∧ N.Stall s p.1 p.2 }
  let rankOf : A → Nat × Nat :=
    fun p => (N.graphRank (N.classOf p.1.2), N.localRank (s p.1.1) p.1.2)
  have hstep : ∀ p : A, ∃ q : A, NatLex (rankOf q) (rankOf p) := by
    intro ⟨⟨t, y'⟩, htτ, hstall'⟩
    obtain ⟨u, htu, x, hbx, hstallx⟩ :=
      inst.eventual_blocker (N.componentOf y') s hlegal henv t y' rfl hstall'
    have hθ : N.localRank (s u) y' = N.localRank (s t) y' :=
      inst.rank_stable (N.componentOf y') s hlegal t y' rfl hstall' u htu
    have hdesc : NatLex
        (N.graphRank (N.classOf x), N.localRank (s u) x)
        (N.graphRank (N.classOf y'), N.localRank (s t) y') := by
      cases blocked_rank N str hlegal hbx with
      | inl hψ => exact Or.inl hψ
      | inr hμθ =>
        exact Or.inr ⟨congrArg N.graphRank hμθ.1, hθ ▸ hμθ.2⟩
    exact ⟨⟨(u, x), Nat.le_trans htτ htu, hstallx⟩, hdesc⟩
  obtain ⟨g, hg⟩ := Classical.axiomOfChoice hstep
  let p0 : A := ⟨(τ, y), Nat.le_refl τ, hstall⟩
  obtain ⟨n, hn⟩ := no_infinite_descent natLexWf (fun n => rankOf (iter g n p0))
  exact hn (hg _)

/-- T5: on a step-legal trajectory
    with no permanent stall, a hold point occupied by a given token admits a
    progress step, and the same token owns the point up to that step.
    Proof: if progress never occurs, `own_stable` keeps the owner fixed
    forever — a permanent stall, excluded by hypothesis.  Take the earliest
    progress step (exists_minimal_witness). -/
theorem held_episode_can_advance (N : GenericNoC)
    {f : Nat → N.State} (hlegal : ∀ t, N.step (f t) (f (t + 1)))
    (hns : ∀ τ y, ¬ N.Stall f τ y)
    (h : N.Hold) {tok : N.Owner} (h0 : N.own (f 0) h = some tok) :
    ∃ t, N.prog (f t) (f (t + 1)) h ∧ ∀ j, j ≤ t → N.own (f j) h = some tok := by
  have hex : ∃ t, N.prog (f t) (f (t + 1)) h := by
    apply Classical.byContradiction
    intro hno
    have hown : ∀ t, N.own (f t) h = some tok := by
      intro t
      induction t with
      | zero => exact h0
      | succ t ih =>
        have hheld : (N.own (f t) h).isSome := Option.isSome_iff_exists.2 ⟨tok, ih⟩
        exact (N.own_stable (hlegal t) hheld (fun hp => hno ⟨t, hp⟩)).trans ih
    exact hns 0 h (fun t _ =>
      ⟨Option.isSome_iff_exists.2 ⟨tok, hown t⟩, fun hp => hno ⟨t, hp⟩⟩)
  obtain ⟨t, ht, hmin⟩ := exists_minimal_witness hex
  have hstable : ∀ j, j ≤ t → N.own (f j) h = some tok := by
    intro j hj
    induction j with
    | zero => exact h0
    | succ j ih =>
      have hjj : j ≤ t := Nat.le_of_succ_le hj
      have hheld : (N.own (f j) h).isSome := Option.isSome_iff_exists.2 ⟨tok, ih hjj⟩
      have hnprog : ¬ N.prog (f j) (f (j + 1)) h :=
        hmin j (Nat.lt_of_lt_of_le (Nat.lt_succ_self j) hj)
      exact (N.own_stable (hlegal j) hheld hnprog).trans (ih hjj)
  exact ⟨t, ht, hstable⟩

/-- T6 (derivation-chain step 5; the final theorem G
    deadlock_free_of_held_episode_progress): W7 binds each pending work to a
    held point by its token; W6 supplies a compliant execution through the
    state; T2 excludes stalls on it; T5 delivers the advancing episode —
    together `DeadlockFree`. -/
theorem deadlock_free (N : GenericNoC) (str : StructuralInstanceLemmas N)
    (inst : InstanceLemmas N) (w6 : ServiceContinuation N)
    (w7 : AcceptedWorkInventory N) (held : PendingHeld w7) : N.DeadlockFree w7 := by
  intro σ hreach q hpending
  obtain ⟨h, hheld⟩ := held.held σ hreach q hpending
  obtain ⟨g, j, hlegal, henv, hj, hsf⟩ := w6.extend σ hreach
  have hf0 : (fun n => g (n + j)) 0 = σ := by
    simp only [Nat.zero_add]; exact hj
  have hfstep : ∀ t, N.step (g (t + j)) (g (t + 1 + j)) := by
    intro t
    have hcom : t + 1 + j = t + j + 1 := Nat.add_right_comm t 1 j
    rw [hcom]
    exact hlegal.2 (t + j)
  have hns : ∀ τ y, ¬ N.Stall (fun n => g (n + j)) τ y := by
    intro τ y hst
    apply no_stall N str inst hlegal henv (τ + j) y
    intro t ht
    obtain ⟨hd1, hd2⟩ := hst (t - j) (by omega)
    simp only [] at hd1 hd2
    have hteq : t - j + j = t := by omega
    have hteq2 : t - j + 1 + j = t + 1 := by omega
    rw [hteq] at hd1
    rw [hteq, hteq2] at hd2
    exact ⟨hd1, hd2⟩
  obtain ⟨t, htprog, htown⟩ :=
    held_episode_can_advance N hfstep hns h (tok := w7.tok q) (by
      show N.own (g (0 + j)) h = some (w7.tok q)
      simp only [Nat.zero_add]
      rw [hj]
      exact hheld)
  exact ⟨h, fun n => g (n + j), t, hheld, hf0, hfstep, hsf j, htown, htprog⟩

/-- Stronger than `deadlock_free` where it applies: it holds on every legal run
    that satisfies the environment condition, not on one chosen run, and it
    needs neither W6 nor W7.  A point held at time `t` makes a progress step at
    some later time, and its owner does not change before that step.  It does
    not say that a transaction completes: that needs a variant per transaction,
    which this model does not have. -/
theorem advances_on_every_compliant_run (N : GenericNoC) (str : StructuralInstanceLemmas N)
    (inst : InstanceLemmas N) {s : Nat → N.State} (hlegal : N.Legal s) (henv : N.env s)
    (t : Nat) (h : N.Hold) {tok : N.Owner} (hown : N.own (s t) h = some tok) :
    ∃ u, t ≤ u ∧ N.prog (s u) (s (u + 1)) h ∧
      ∀ j, t ≤ j → j ≤ u → N.own (s j) h = some tok := by
  have hstep : ∀ n, N.step (s (n + t)) (s (n + 1 + t)) := by
    intro n
    have hcom : n + 1 + t = n + t + 1 := Nat.add_right_comm n 1 t
    rw [hcom]
    exact hlegal.2 (n + t)
  have hns : ∀ τ y, ¬ N.Stall (fun n => s (n + t)) τ y := by
    intro τ y hst
    apply no_stall N str inst hlegal henv (τ + t) y
    intro n hn
    obtain ⟨hd1, hd2⟩ := hst (n - t) (by omega)
    simp only [] at hd1 hd2
    have e1 : n - t + t = n := by omega
    have e2 : n - t + 1 + t = n + 1 := by omega
    rw [e1] at hd1
    rw [e1, e2] at hd2
    exact ⟨hd1, hd2⟩
  obtain ⟨v, hv, hown'⟩ := held_episode_can_advance N hstep hns h (tok := tok) (by
    show N.own (s (0 + t)) h = some tok
    rw [Nat.zero_add]; exact hown)
  refine ⟨v + t, by omega, ?_, ?_⟩
  · have e : v + t + 1 = v + 1 + t := by omega
    rw [e]; exact hv
  · intro j hj1 hj2
    have := hown' (j - t) (by omega)
    have e : j - t + t = j := by omega
    simp only [] at this
    rw [e] at this
    exact this

end GenericNoC
