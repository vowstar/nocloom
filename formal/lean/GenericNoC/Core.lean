/-
  GenericNoC.Core: self-contained proof kernels used by the generic theorems.

  All of these are proved against Lean 4.24.0 core only (no mathlib), because
  the corresponding core facilities are absent or shaped differently in 4.24.0:
  - `Nat.find` is not in core: `exists_minimal_witness` replaces it.
  - `Function.iterate` (`f^[n]`) is not in core: `iter` replaces it.
  - `Prod.lex`'s `WellFoundedRelation` exists but its `rel` does not unfold
    definitionally to `Prod.Lex Nat.lt Nat.lt`, so `natLexWf` is proved
    directly by nested strong recursion.
-/
universe u v

/-- Lexicographic order on natural-number pairs: the composite rank order of
    `(graphRank ∘ classOf, localRank)`.  Defined as a plain `Or`
    for painless construction/destruction; `natLex_iff_prodLex` ties it to the
    core `Prod.Lex`. -/
def NatLex (a b : Nat × Nat) : Prop := a.1 < b.1 ∨ (a.1 = b.1 ∧ a.2 < b.2)

/-- The chosen order is exactly core's `Prod.Lex Nat.lt Nat.lt`. -/
theorem natLex_iff_prodLex {a b : Nat × Nat} : NatLex a b ↔ Prod.Lex Nat.lt Nat.lt a b := by
  constructor
  · intro h
    cases h with
    | inl hlt => exact Prod.Lex.left _ _ hlt
    | inr h =>
      obtain ⟨heq, hlt⟩ := h
      obtain ⟨a1, a2⟩ := a
      obtain ⟨b1, b2⟩ := b
      cases heq
      exact Prod.Lex.right _ hlt
  · intro h
    match h with
    | Prod.Lex.left _ _ hlt => exact Or.inl hlt
    | Prod.Lex.right _ hlt => exact Or.inr ⟨rfl, hlt⟩

/-- Well-foundedness of the lexicographic order on `Nat × Nat`. -/
theorem natLexWf : WellFounded NatLex :=
  WellFounded.intro fun ⟨a, b⟩ =>
    (Nat.strongRecOn a (motive := fun a => ∀ b, Acc NatLex (a, b))
      (fun a iha b =>
        Nat.strongRecOn b (motive := fun b => Acc NatLex (a, b))
          (fun b ihb => Acc.intro _ fun p h => by
            obtain ⟨a', b'⟩ := p
            cases h with
            | inl hlt => exact iha a' hlt b'
            | inr h =>
              obtain ⟨heq, hlt⟩ := h
              subst heq
              exact ihb b' hlt))) b

/-- A well-founded relation admits no infinite descending chain.
    Proved once here; consumed by the no-stall theorem T2. -/
theorem no_infinite_descent {α : Type u} {r : α → α → Prop} (hwf : WellFounded r)
    (f : Nat → α) : ∃ n, ¬ r (f (n + 1)) (f n) := by
  have key : ∀ a, Acc r a → ∀ g : Nat → α, g 0 = a → ∃ n, ¬ r (g (n + 1)) (g n) := by
    intro a ha
    induction ha with
    | intro x _ ih =>
      intro g hg0
      by_cases hstep : r (g 1) (g 0)
      · have h10 : r (g 1) x := hg0 ▸ hstep
        obtain ⟨n, hn⟩ := ih (g 1) h10 (fun k => g (k + 1)) rfl
        exact ⟨n + 1, hn⟩
      · exact ⟨0, hstep⟩
  obtain ⟨n, hn⟩ := key (f 0) (hwf.apply _) f rfl
  exact ⟨n, hn⟩

/-- Least-witness principle (classical), replacing the absent `Nat.find`:
    from an existential, produce a witness with nothing smaller below it. -/
theorem exists_minimal_witness {P : Nat → Prop} (h : ∃ n, P n) :
    ∃ n, P n ∧ ∀ m, m < n → ¬ P m := by
  obtain ⟨n, hn⟩ := h
  induction n using Nat.strongRecOn with
  | ind n ih =>
    by_cases hmin : ∀ m, m < n → ¬ P m
    · exact ⟨n, hn, hmin⟩
    · have hex : ∃ m, m < n ∧ P m := by
        apply Classical.byContradiction
        intro hc
        exact hmin (fun m hmn hm => hc ⟨m, hmn, hm⟩)
      obtain ⟨m, hmn, hm⟩ := hex
      exact ih m hmn hm

/-- Iterated function application, replacing the absent `Function.iterate`. -/
def iter {α : Type u} (f : α → α) : Nat → α → α
  | 0, a => a
  | n + 1, a => f (iter f n a)

theorem iter_zero {α : Type u} (f : α → α) (a : α) : iter f 0 a = a := rfl

theorem iter_succ {α : Type u} (f : α → α) (n : Nat) (a : α) :
    iter f (n + 1) a = f (iter f n a) := rfl

theorem iter_add {α : Type u} (f : α → α) (m n : Nat) (a : α) :
    iter f (m + n) a = iter f m (iter f n a) := by
  induction m with
  | zero => simp [iter]
  | succ m ih => simp only [Nat.succ_add, iter]; exact congrArg f ih
