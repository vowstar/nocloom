/-
  GenericNoC.WormholeDone: every accepted transaction completes.

  A transaction declares how many words it may still add when it is accepted.
  The variant is `hopMax` per word not yet added, plus the steps left for each of
  its words in the network.  Every event of the transaction lowers the variant
  and nothing else raises it, so with no permanent stall the transaction ends.

  This is a statement about the model, a checked table and `Fair`.  It relies on
  `Fair`: a target that never answers breaks it.  It is not RTL equivalence, and
  it gives no time bound.
-/
import GenericNoC.WormholeLive

namespace Wormhole

/-- Steps left for one transaction's words in a list at a slot. -/
def wSum (c : Config) (k p : Nat) : List Word → Nat
  | [] => 0
  | w :: ws => (if w.tok = k then c.hops p w.dest else 0) + wSum c k p ws

theorem wSum_push (c : Config) (k p : Nat) (l : List Word) (w : Word) :
    wSum c k p (l ++ [w]) = wSum c k p l + (if w.tok = k then c.hops p w.dest else 0) := by
  induction l with
  | nil => simp [wSum]
  | cons a as ih => simp only [List.cons_append, wSum, ih]; omega

/-- A sum over slots. -/
def sSum (f : Nat → Nat) : List Nat → Nat
  | [] => 0
  | x :: xs => f x + sSum f xs

theorem sSum_congr {f g : Nat → Nat} : ∀ {l : List Nat}, (∀ x, x ∈ l → f x = g x) →
    sSum f l = sSum g l := by
  intro l
  induction l with
  | nil => intro _; rfl
  | cons a as ih =>
    intro h
    simp only [sSum, h a (List.mem_cons_self), ih (fun x hx => h x (List.mem_cons_of_mem _ hx))]

/-- Changing one slot changes the sum by that slot's difference. -/
theorem sSum_upd {f : Nat → Nat} {p v : Nat} : ∀ {l : List Nat}, up l = true → p ∈ l →
    sSum (upd f p v) l + f p = sSum f l + v := by
  intro l
  induction l with
  | nil => intro _ hp; simp at hp
  | cons x xs ih =>
    intro hu hp
    simp only [sSum]
    by_cases hx : x = p
    · subst hx
      have hrest : sSum (upd f x v) xs = sSum f xs := by
        apply sSum_congr
        intro y hy
        have := up_lt hu y hy
        exact upd_ne _ _ (by omega)
      rw [upd_same, hrest]
      omega
    · have hm : p ∈ xs := by
        rcases List.mem_cons.1 hp with h | h
        · exact absurd h.symm hx
        · exact h
      have := ih (up_tail hu) hm
      rw [upd_ne _ _ hx]
      omega

/-- The variant of a transaction. -/
def variant (c : Config) (σ : St) (k : Nat) : Nat :=
  c.hopMax * σ.budget k + sSum (fun p => wSum c k p (σ.q p)) c.slots

/-- A fire does not touch budgets. -/
theorem wSum_cut (c : Config) (k p : Nat) {l : List Word} {w : Word} (h : l.head? = some w) :
    wSum c k p ({ w with tail := true } :: l.tail) = wSum c k p l := by
  cases l with
  | nil => simp at h
  | cons a as =>
    have : a = w := by simpa using h
    subst this
    rfl

theorem place_budget {c : Config} {τ : St} {o : Nat} {w : Word} :
    (place c τ o w).budget = τ.budget := by
  unfold place; split
  · rfl
  · split <;> rfl

section Step

variable {c : Config} {σ : St} {k : Nat}

/-- What a fire does to the variant. -/
theorem variant_fire (wf : WF c) (hf : HopFacts c) {p o : Nat} {w : Word}
    (hh : (σ.q p).head? = some w) (hr : c.route p w.dest = some o)
    (hloop : c.next o ≠ some p) :
    variant c (place c (pop σ p o w) o w) k ≤ variant c σ k ∧
      (w.tok = k → variant c (place c (pop σ p o w) o w) k < variant c σ k) := by
  obtain ⟨rest, hq⟩ : ∃ rest, σ.q p = w :: rest := by
    cases hq : σ.q p with
    | nil => rw [hq] at hh; simp at hh
    | cons a as =>
      rw [hq] at hh
      have : a = w := by simpa using hh
      subst this
      exact ⟨as, rfl⟩
  have hbud : (place c (pop σ p o w) o w).budget k = σ.budget k := by
    rw [place_budget]; rfl
  simp only [variant, hbud]
  generalize hF : (fun x => wSum c k x (σ.q x)) = F
  have hFx : ∀ x, F x = wSum c k x (σ.q x) := fun x => by rw [← hF]
  have hFp : F p = (if w.tok = k then c.hops p w.dest else 0) + wSum c k p rest := by
    rw [hFx, hq]; rfl
  have hpm : p ∈ c.slots := hf.mem hr
  by_cases htk : w.tok = k
  · -- The transaction's own word moves.
    have hsum : sSum (fun x => wSum c k x ((place c (pop σ p o w) o w).q x)) c.slots < sSum F c.slots := by
      simp only [htk, if_true] at hFp
      have hpos := hf.pos hr
      cases hn : c.next o with
      | none =>
        have hcong : sSum (fun x => wSum c k x ((place c (pop σ p o w) o w).q x)) c.slots =
            sSum (upd F p (wSum c k p rest)) c.slots := by
          apply sSum_congr
          intro x _
          simp only [fire_q hloop, hn]
          by_cases hx : x = p
          · subst hx; simp [hq]
          · simp [hx, upd_ne, hFx]
        have := sSum_upd (f := F) (p := p) (v := wSum c k p rest) hf.up_slots hpm
        rw [hcong]
        omega
      | some r =>
        have hrp : r ≠ p := fun e => hloop (by rw [hn, e])
        have hdown := hf.down hr hn
        have hrm : r ∈ c.slots := by
          obtain ⟨o', ho'⟩ := Option.isSome_iff_exists.1 (wf.route_closed p w.dest o r hr hn)
          exact hf.mem ho'
        have hcong : sSum (fun x => wSum c k x ((place c (pop σ p o w) o w).q x)) c.slots =
            sSum (upd (upd F p (wSum c k p rest)) r (F r + c.hops r w.dest)) c.slots := by
          apply sSum_congr
          intro x _
          simp only [fire_q hloop, hn, Option.some.injEq]
          by_cases hx : r = x
          · subst hx
            simp [wSum_push, htk, hFx]
          · have hxr : x ≠ r := fun e => hx e.symm
            rw [upd_ne _ _ hxr]
            by_cases hxp : x = p
            · subst hxp; simp [hx, hq]
            · simp [hx, hxp, upd_ne, hFx]
        have h1 := sSum_upd (f := F) (p := p) (v := wSum c k p rest) hf.up_slots hpm
        have h2 := sSum_upd (f := upd F p (wSum c k p rest)) (p := r)
          (v := F r + c.hops r w.dest) hf.up_slots hrm
        rw [upd_ne _ _ hrp] at h2
        rw [hcong]
        omega
    exact ⟨by omega, fun _ => by omega⟩
  · -- Another transaction's word moves: nothing changes for this one.
    have hsum : sSum (fun x => wSum c k x ((place c (pop σ p o w) o w).q x)) c.slots = sSum F c.slots := by
      apply sSum_congr
      intro x _
      rw [hFx, fire_q hloop]
      split
      · simp [wSum_push, htk]
      · split
        · rename_i hx
          subst hx
          rw [hq]
          simp [wSum, htk]
        · rfl
    rw [hsum]
    exact ⟨Nat.le_refl _, fun h => absurd h htk⟩

/-- What adding a word does to the variant of a live transaction. -/
theorem variant_inject (hf : HopFacts c) {p : Nat} {w : Word} {b : Nat}
    (hk : w.tok = k → σ.live k = true)
    (hrt : (c.route p w.dest).isSome = true)
    (hbudget : σ.live w.tok = true → 0 < σ.budget w.tok) :
    variant c (injected σ p w b) k ≤ variant c σ k ∧
    (w.tok = k → variant c (injected σ p w b) k < variant c σ k) := by
  obtain ⟨o, ho⟩ := Option.isSome_iff_exists.1 hrt
  simp only [variant, injected]
  generalize hF : (fun x => wSum c k x (σ.q x)) = F
  have hFx : ∀ x, F x = wSum c k x (σ.q x) := fun x => by rw [← hF]
  have hpm := hf.mem ho
  by_cases htk : w.tok = k
  · subst htk
    have hlive := hk rfl
    have hb := hbudget hlive
    have hl := hf.lt_max ho
    have hcong : sSum (fun x => wSum c w.tok x (upd σ.q p (σ.q p ++ [w]) x)) c.slots =
        sSum (upd F p (F p + c.hops p w.dest)) c.slots := by
      apply sSum_congr
      intro x _
      by_cases hx : x = p
      · subst hx; simp [wSum_push, hFx]
      · simp [upd_ne, hx, hFx]
    have h1 := sSum_upd (f := F) (p := p) (v := F p + c.hops p w.dest) hf.up_slots hpm
    have hnb : newBudget σ w b = σ.budget w.tok - 1 := by simp [newBudget, hlive]
    simp only [upd_same, hnb]
    rw [hcong]
    obtain ⟨n, hn⟩ : ∃ n, σ.budget w.tok = n + 1 := ⟨σ.budget w.tok - 1, by omega⟩
    rw [hn]
    simp only [Nat.add_sub_cancel, Nat.mul_add, Nat.mul_one]
    have hlt : sSum (upd F p (F p + c.hops p w.dest)) c.slots + 0 < sSum F c.slots + c.hopMax := by
      omega
    exact ⟨by omega, fun _ => by omega⟩
  · have hbud : upd σ.budget w.tok (newBudget σ w b) k = σ.budget k :=
      upd_ne _ _ (fun e => htk e.symm)
    have hcong : sSum (fun x => wSum c k x (upd σ.q p (σ.q p ++ [w]) x)) c.slots = sSum F c.slots := by
      apply sSum_congr
      intro x _
      by_cases hx : x = p
      · subst hx; simp [wSum_push, htk, hFx]
      · simp [upd_ne, hx, hFx]
    simp only [hbud]
    rw [hcong]
    exact ⟨Nat.le_refl _, fun h => absurd h htk⟩

/-- The variant of a transaction that is live, or that this step does not bring
    back, never rises, and its own points' progress lowers it. -/
theorem variant_step (wf : WF c) (hf : HopFacts c) {σ' : St} (hs : Step c σ σ')
    (hlive : σ.live k = true ∨ σ'.live k = false) :
    variant c σ' k ≤ variant c σ k ∧
      (∀ p, prog σ σ' (.ctx k p) → variant c σ' k < variant c σ k) ∧
      (prog σ σ' (.wait k) → variant c σ' k < variant c σ k) := by
  cases hs with
  | stutter =>
    refine ⟨Nat.le_refl _, ?_, ?_⟩
    · rintro p ⟨_, o, he⟩; simp at he
    · rintro ⟨⟨_, hno⟩, p, hx⟩; exact absurd hx (hno p)
  | arb =>
    refine ⟨Nat.le_refl _, ?_, ?_⟩
    · rintro p ⟨_, o, he⟩; simp at he
    · rintro ⟨⟨_, hno⟩, p, hx⟩; exact absurd hx (hno p)
  | cut p1 w _ hh _ _ =>
    have heq : variant c { σ with
        q := upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail)
        ev := none } k = variant c σ k := by
      simp only [variant]
      congr 1
      apply sSum_congr
      intro x _
      by_cases hx : x = p1
      · subst hx
        simp only [upd_same]
        exact wSum_cut c k x hh
      · simp only [upd_ne _ _ hx]
    refine ⟨Nat.le_of_eq heq, ?_, ?_⟩
    · rintro p ⟨_, o, he⟩; simp at he
    · rintro ⟨⟨_, hno⟩, p, a, ha, hak⟩
      exfalso
      have ha' : a ∈ upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p := ha
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha'
        rcases List.mem_cons.1 ha' with rfl | hm
        · exact hno p ⟨w, List.mem_of_head? hh, hak⟩
        · exact hno p ⟨a, List.mem_of_mem_tail hm, hak⟩
      · rw [upd_ne _ _ hx] at ha'
        exact hno p ⟨a, ha', hak⟩
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    obtain ⟨h1, h2⟩ := variant_fire (k := k) wf hf hh hr hloop
    refine ⟨h1, ?_, ?_⟩
    · rintro p ⟨⟨w0, hw0, htk⟩, o, he⟩
      rw [fire_ev] at he
      simp only [Option.some.injEq, Prod.mk.injEq] at he
      obtain ⟨rfl, _⟩ := he
      rw [hh] at hw0
      have : w = w0 := by simpa using hw0
      subst this
      exact h2 htk
    · rintro ⟨⟨_, hno⟩, p, a, ha, hak⟩
      exfalso
      rcases mem_fire_q hloop ha with hm | ⟨rfl, _⟩
      · exact hno p ⟨a, hm, hak⟩
      · exact hno p1 ⟨a, List.mem_of_head? hh, hak⟩
  | inject p1 w b _ _ _ _ hrt _ _ _ hbudget _ _ _ =>
    have hk : w.tok = k → σ.live k = true := by
      intro e
      rcases hlive with h | h
      · exact h
      · subst e
        simp [injected] at h
    obtain ⟨h1, h2⟩ := variant_inject (k := k) (b := b) (p := p1) hf hk hrt hbudget
    refine ⟨h1, ?_, ?_⟩
    · rintro p ⟨_, o, he⟩; simp [injected] at he
    · rintro ⟨⟨_, hno⟩, p, a, ha, hak⟩
      apply h2
      have ha' : a ∈ upd σ.q p1 (σ.q p1 ++ [w]) p := ha
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha'
        rcases List.mem_append.1 ha' with hm | hm
        · exact absurd ⟨a, hm, hak⟩ (hno p)
        · have : a = w := by simpa using hm
          subst this
          exact hak
      · rw [upd_ne _ _ hx] at ha'
        exact absurd ⟨a, ha', hak⟩ (hno p)

end Step

/-! ### A transaction always has a way to end -/

/-- A live transaction with nothing left to add has its last word in the network,
    on a path to a free sink. -/
def HasFinal (c : Config) (σ : St) (k : Nat) : Prop :=
  σ.live k = true → σ.budget k = 0 →
    ∃ p w, w ∈ σ.q p ∧ w.tok = k ∧ w.last = true ∧ EndsFree c p w.dest

theorem hasFinal_empty (c : Config) (k : Nat) : HasFinal c St.empty k := by
  intro h
  simp [St.empty] at h

/-- A fire cannot bring a transaction back. -/
theorem place_live_back {c : Config} {τ : St} {o k : Nat} {w : Word}
    (h : (place c τ o w).live k = true) : τ.live k = true := by
  by_cases hne : w.tok = k
  · subst hne
    unfold place at h
    split at h
    · exact h
    · split at h
      · exact h
      · simp only at h
        split at h
        · simp at h
        · exact h
  · rwa [place_live_other hne] at h

/-- The last word reaching its free sink ends its transaction. -/
theorem place_last_ends {c : Config} {τ : St} {o : Nat} {w : Word} (hn : c.next o = none)
    (hp : c.pool o w.dest = none) (hl : w.last = true) : (place c τ o w).live w.tok = false := by
  simp [place, hn, hp, hl]

theorem hasFinal_step {c : Config} {σ σ' : St} {k : Nat} (hs : Step c σ σ') (I : HasFinal c σ k) :
    HasFinal c σ' k := by
  cases hs with
  | stutter => exact I
  | arb => exact I
  | cut p1 w _ hh _ _ =>
    intro hlive hbud
    obtain ⟨p, a, ha, hak, hal, hend⟩ := I hlive hbud
    by_cases hx : p = p1
    · subst hx
      cases hq : σ.q p with
      | nil => rw [hq] at ha; simp at ha
      | cons b bs =>
        rw [hq] at ha hh
        have hb : b = w := by simpa using hh
        subst hb
        rcases List.mem_cons.1 ha with rfl | hm
        · exact ⟨p, { a with tail := true }, by
            show _ ∈ upd σ.q p ({ a with tail := true } :: (a :: bs).tail) p
            rw [upd_same]; simp, hak, hal, hend⟩
        · exact ⟨p, a, by
            show _ ∈ upd σ.q p ({ b with tail := true } :: (b :: bs).tail) p
            rw [upd_same]; simp [hm], hak, hal, hend⟩
    · exact ⟨p, a, by
        show _ ∈ upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p
        rw [upd_ne _ _ hx]; exact ha, hak, hal, hend⟩
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    intro hlive hbud
    have hl0 : (pop σ p1 o1 w).live k = true := place_live_back hlive
    have hb0 : σ.budget k = 0 := by
      rw [place_budget] at hbud
      exact hbud
    obtain ⟨p, a, ha, hak, hal, hend⟩ := I hl0 hb0
    obtain ⟨rest, hq⟩ : ∃ rest, σ.q p1 = w :: rest := by
      cases hq : σ.q p1 with
      | nil => rw [hq] at hh; simp at hh
      | cons x xs =>
        rw [hq] at hh
        have : x = w := by simpa using hh
        subst this
        exact ⟨xs, rfl⟩
    have hstay : ∀ {x b}, (x ≠ p1 ∨ b ∈ rest) → b ∈ σ.q x →
        b ∈ (place c (pop σ p1 o1 w) o1 w).q x := by
      intro x b hor hb
      rw [fire_q hloop]
      split
      · exact List.mem_append.2 (Or.inl hb)
      · split
        · rename_i hx
          subst hx
          rcases hor with hne | hr'
          · exact absurd rfl hne
          · simpa [hq] using hr'
        · exact hb
    by_cases hmoved : p = p1 ∧ a ∉ rest
    · obtain ⟨rfl, hnot⟩ := hmoved
      have haw : a = w := by
        rw [hq] at ha
        rcases List.mem_cons.1 ha with h | h
        · exact h
        · exact absurd h hnot
      subst haw
      cases hend with
      | sink hr1 hn1 hp1 =>
        rw [hr] at hr1
        cases hr1
        rw [← hak, place_last_ends hn1 hp1 hal] at hlive
        simp at hlive
      | step hr1 hn1 hnext =>
        rw [hr] at hr1
        cases hr1
        refine ⟨_, a, ?_, hak, hal, hnext⟩
        rw [fire_q hloop, if_pos hn1]
        exact List.mem_append.2 (Or.inr (by simp))
    · refine ⟨p, a, hstay ?_ ha, hak, hal, hend⟩
      by_cases hx : p = p1
      · right
        apply Classical.byContradiction
        intro hn
        exact hmoved ⟨hx, hn⟩
      · exact Or.inl hx
  | inject p1 w b _ _ _ _ _ _ _ _ hbudget hlast hfinal _ =>
    intro hlive hbud
    have hkeep : ∀ {x a}, a ∈ σ.q x → a ∈ upd σ.q p1 (σ.q p1 ++ [w]) x := by
      intro x a ha
      by_cases hx : x = p1
      · subst hx; rw [upd_same]; exact List.mem_append.2 (Or.inl ha)
      · rw [upd_ne _ _ hx]; exact ha
    by_cases htk : w.tok = k
    · subst htk
      have hzero : newBudget σ w b = 0 := by
        have : upd σ.budget w.tok (newBudget σ w b) w.tok = 0 := hbud
        rwa [upd_same] at this
      have hl := hlast.2 hzero
      refine ⟨p1, w, ?_, rfl, hl, (hfinal hl).2⟩
      show w ∈ upd σ.q p1 (σ.q p1 ++ [w]) p1
      rw [upd_same]
      simp
    · have hl0 : σ.live k = true := by
        have : upd σ.live w.tok true k = true := hlive
        rwa [upd_ne _ _ (fun e => htk e.symm)] at this
      have hb0 : σ.budget k = 0 := by
        have : upd σ.budget w.tok (newBudget σ w b) k = 0 := hbud
        rwa [upd_ne _ _ (fun e => htk e.symm)] at this
      obtain ⟨p, a, ha, hak, hal, hend⟩ := I hl0 hb0
      exact ⟨p, a, hkeep ha, hak, hal, hend⟩

theorem hasFinal_run {c : Config} {s : Nat → St} (h0 : s 0 = St.empty)
    (hs : ∀ n, Step c (s n) (s (n + 1))) (k t : Nat) : HasFinal c (s t) k := by
  induction t with
  | zero => rw [h0]; exact hasFinal_empty c k
  | succ n ih => exact hasFinal_step (hs n) ih

/-- A transaction waiting for words always has words left to add.  So `Fair`'s
    clause on answers never has to cover an empty promise. -/
theorem waiting_has_budget {c : Config} {s : Nat → St} (h0 : s 0 = St.empty)
    (hs : ∀ n, Step c (s n) (s (n + 1))) (t k : Nat) (hw : waiting (s t) k) :
    0 < (s t).budget k := by
  apply Classical.byContradiction
  intro hno
  have hz : (s t).budget k = 0 := by omega
  obtain ⟨p, w, hmem, htk, _, _⟩ := hasFinal_run h0 hs k t hw.1 hz
  exact hw.2 p ⟨w, hmem, htk⟩

/-! ### Completion -/

/-- On every legal `Fair` run of a checked instance, every live transaction ends.
    This is about the model, the table and `Fair`: a target that never answers
    breaks `Fair`.  It is not RTL equivalence, and it gives no time bound. -/
theorem checked_transaction_completes {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r) (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node)
    (hheads : headsInRange c t node = true)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1)))
    (F : Fair c s) (τ k : Nat) :
    ∃ u, τ ≤ u ∧ (s u).live k = false := by
  apply Classical.byContradiction
  intro hno
  have hall : ∀ u, τ ≤ u → (s u).live k = true := by
    intro u hu
    cases hl : (s u).live k with
    | true => rfl
    | false => exact absurd ⟨u, hu, hl⟩ hno
  have wf := wfCheck_sound hwf
  have hf := hopFacts hhops
  have hmono : ∀ a b, τ ≤ a → a ≤ b → variant c (s b) k ≤ variant c (s a) k := by
    intro a b ha hb
    induction hb with
    | refl => exact Nat.le_refl _
    | @step b hb ih =>
      exact Nat.le_trans (variant_step wf hf (hs b) (Or.inl (hall b (Nat.le_trans ha hb)))).1 ih
  have key : ∀ n a, τ ≤ a → variant c (s a) k = n → False := by
    intro n
    induction n using Nat.strongRecOn with
    | ind n ih =>
      intro a ha hv
      have hdrop : ∃ u, a ≤ u ∧ variant c (s (u + 1)) k < variant c (s u) k := by
        by_cases hany : ∃ p, hasTok (s a) p k
        · obtain ⟨p, hp⟩ := hany
          obtain ⟨u, hu, hprog, _⟩ := checked_instance_advances hwf hcap hhops hrank hcov hheads h0 hs F
            a (.ctx k p) (own_ctx_some hp)
          exact ⟨u, hu, (variant_step wf hf (hs u) (Or.inl (hall u (Nat.le_trans ha hu)))).2.1 p hprog⟩
        · have hw : waiting (s a) k := ⟨hall a ha, fun p hp => hany ⟨p, hp⟩⟩
          obtain ⟨u, hu, hprog, _⟩ := checked_instance_advances hwf hcap hhops hrank hcov hheads h0 hs F
            a (.wait k) (own_wait_some hw)
          exact ⟨u, hu, (variant_step wf hf (hs u) (Or.inl (hall u (Nat.le_trans ha hu)))).2.2 hprog⟩
      obtain ⟨u, hu, hlt⟩ := hdrop
      have hle := hmono a u ha hu
      exact ih _ (by omega) (u + 1) (by omega) rfl
  exact key _ τ (Nat.le_refl τ) rfl

/-- A used token stays used. -/
theorem used_step {c : Config} {σ σ' : St} {k : Nat} (hs : Step c σ σ') (h : σ.used k = true) :
    σ'.used k = true := by
  cases hs with
  | stutter => exact h
  | arb => exact h
  | cut => exact h
  | fire p o w _ _ _ _ _ _ =>
    have : (place c (pop σ p o w) o w).used = σ.used := by
      unfold place; split
      · rfl
      · split <;> rfl
    rw [this]; exact h
  | inject p w b =>
    show upd σ.used w.tok true k = true
    by_cases hx : k = w.tok
    · subst hx; simp
    · rw [upd_ne _ _ hx]; exact h

/-- A transaction that has ended is not accepted again. -/
theorem no_restart {c : Config} {σ σ' : St} {k : Nat} (hs : Step c σ σ') (hl : σ.live k = false)
    (hu : σ.used k = true) : σ'.live k = false := by
  cases hs with
  | stutter => exact hl
  | arb => exact hl
  | cut => exact hl
  | fire p o w =>
    cases hx : (place c (pop σ p o w) o w).live k with
    | false => rfl
    | true =>
      have := place_live_back hx
      have h2 : σ.live k = true := this
      rw [hl] at h2
      simp at h2
  | inject p w b _ _ _ _ _ hfresh =>
    show upd σ.live w.tok true k = false
    by_cases hx : k = w.tok
    · subst hx
      rw [hfresh hl] at hu
      simp at hu
    · rw [upd_ne _ _ hx]; exact hl

/-- Delivery.  `live = false` alone does not say the words arrived, because the last
    word can end a transaction while an earlier one is still in the network.  For a
    token that has been accepted, on every legal `Fair` run of a checked instance a
    time comes when it has ended and none of its words is in any slot.  A word only leaves the network through a sink: `place` puts it in the next
    slot otherwise. -/
theorem checked_transaction_delivered {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r) (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node)
    (hheads : headsInRange c t node = true)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1)))
    (F : Fair c s) (τ k : Nat) (hused : (s τ).used k = true) :
    ∃ u, τ ≤ u ∧ ∀ v, u ≤ v → (s v).live k = false ∧ ∀ p, ¬ hasTok (s v) p k := by
  have huse : ∀ n, τ ≤ n → (s n).used k = true := by
    intro n hn
    induction hn with
    | refl => exact hused
    | @step n hn ih => exact used_step (hs n) ih
  have honce : ∀ n, τ ≤ n → (s n).live k = false → (s (n + 1)).live k = false := fun n hn hl =>
    no_restart (hs n) hl (huse n hn)
  obtain ⟨u0, hu0, hl0⟩ := checked_transaction_completes hwf hcap hhops hrank hcov hheads h0 hs F τ k
  have hdead : ∀ v, u0 ≤ v → (s v).live k = false := by
    intro v hv
    induction hv with
    | refl => exact hl0
    | @step v hv ih => exact honce v (Nat.le_trans hu0 hv) ih
  have wf := wfCheck_sound hwf
  have hf := hopFacts hhops
  have hstep : ∀ v, u0 ≤ v → _ := fun v hv =>
    variant_step (k := k) wf hf (hs v) (Or.inr (hdead (v + 1) (Nat.le_succ_of_le hv)))
  have hmono : ∀ a b, u0 ≤ a → a ≤ b → variant c (s b) k ≤ variant c (s a) k := by
    intro a b ha hb
    induction hb with
    | refl => exact Nat.le_refl _
    | @step b hb ih => exact Nat.le_trans (hstep b (Nat.le_trans ha hb)).1 ih
  have key : ∀ n a, u0 ≤ a → variant c (s a) k = n → ∃ u, a ≤ u ∧ ∀ p, ¬ hasTok (s u) p k := by
    intro n
    induction n using Nat.strongRecOn with
    | ind n ih =>
      intro a ha hv
      by_cases hany : ∃ p, hasTok (s a) p k
      · obtain ⟨p, hp⟩ := hany
        obtain ⟨u, hu, hprog, _⟩ := checked_instance_advances hwf hcap hhops hrank hcov hheads h0 hs F
          a (.ctx k p) (own_ctx_some hp)
        have hlt := (hstep u (Nat.le_trans ha hu)).2.1 p hprog
        have hle := hmono a u ha hu
        obtain ⟨w, hw, hno⟩ := ih _ (by omega) (u + 1) (by omega) rfl
        exact ⟨w, by omega, hno⟩
      · exact ⟨a, Nat.le_refl a, fun p hp => hany ⟨p, hp⟩⟩
  obtain ⟨u, hu, hno⟩ := key _ u0 (Nat.le_refl u0) rfl
  refine ⟨u, Nat.le_trans hu0 hu, ?_⟩
  -- With no word left and no new one, none appears later.
  intro v hv
  have hlive := hdead v (Nat.le_trans hu hv)
  refine ⟨hlive, ?_⟩
  induction hv with
  | refl => exact hno
  | @step v hv ihv =>
    intro p hp
    have hprev := ihv (hdead v (Nat.le_trans hu hv))
    have hstepv := hs v
    have hlv1 := hdead (v + 1) (Nat.le_trans hu (Nat.le_succ_of_le hv))
    generalize hσ : s (v + 1) = σ' at hstepv hp hlv1
    generalize hσ0 : s v = σ at hstepv hprev
    obtain ⟨a, ha, hak⟩ := hp
    cases hstepv with
    | stutter => exact hprev p ⟨a, ha, hak⟩
    | arb => exact hprev p ⟨a, ha, hak⟩
    | cut p1 w _ hh _ _ =>
      have ha' : a ∈ upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p := ha
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha'
        rcases List.mem_cons.1 ha' with rfl | hm
        · exact hprev p ⟨w, List.mem_of_head? hh, hak⟩
        · exact hprev p ⟨a, List.mem_of_mem_tail hm, hak⟩
      · rw [upd_ne _ _ hx] at ha'
        exact hprev p ⟨a, ha', hak⟩
    | fire p1 o1 w hh _ _ _ hloop _ =>
      rcases mem_fire_q hloop ha with hm | ⟨rfl, _⟩
      · exact hprev p ⟨a, hm, hak⟩
      · exact hprev p1 ⟨a, List.mem_of_head? hh, hak⟩
    | inject p1 w b _ _ _ _ _ _ _ _ _ _ _ _ =>
      have ha' : a ∈ upd σ.q p1 (σ.q p1 ++ [w]) p := ha
      have hwk : w.tok ≠ k := by
        intro e
        subst e
        simp [injected] at hlv1
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha'
        rcases List.mem_append.1 ha' with hm | hm
        · exact hprev p ⟨a, hm, hak⟩
        · have : a = w := by simpa using hm
          subst this
          exact hwk hak
      · rw [upd_ne _ _ hx] at ha'
        exact hprev p ⟨a, ha', hak⟩

/-! ### Answers belong to requests -/

/-- A word in a response slot answers its requester, and only after its whole request
    has arrived.  This holds in every state of a run. -/
def AnswersRequest (c : Config) (σ : St) : Prop :=
  ∀ p w, isResponse c p = true → w ∈ σ.q p → σ.live w.tok = true →
    σ.home w.tok = some p ∧ σ.reqDone w.tok = true ∧ answersSrc c (σ.src w.tok) w.dest = true

/-- Every word in the network belongs to an accepted token. -/
def WordsUsed (σ : St) : Prop := ∀ p w, w ∈ σ.q p → σ.used w.tok = true

theorem answers_step {c : Config} (wf : WF c) (hre : ∀ p, isResponse c p = true → p ∈ c.endpoints)
    {σ σ' : St} (hs : Step c σ σ') (I : AnswersRequest c σ ∧ WordsUsed σ) :
    AnswersRequest c σ' ∧ WordsUsed σ' := by
  obtain ⟨Ia, Iu⟩ := I
  cases hs with
  | stutter => exact ⟨Ia, Iu⟩
  | arb => exact ⟨Ia, Iu⟩
  | cut p1 w _ hh _ _ =>
    have hmem : ∀ p a, a ∈ upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p →
        a ∈ σ.q p ∨ (a = { w with tail := true } ∧ p = p1) := by
      intro p a ha
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha
        rcases List.mem_cons.1 ha with h | h
        · exact Or.inr ⟨h, rfl⟩
        · exact Or.inl (List.mem_of_mem_tail h)
      · rw [upd_ne _ _ hx] at ha
        exact Or.inl ha
    constructor
    · intro p a hr ha hl
      rcases hmem p a ha with h | ⟨rfl, rfl⟩
      · exact Ia p a hr h hl
      · exact Ia p w hr (List.mem_of_head? hh) hl
    · intro p a ha
      rcases hmem p a ha with h | ⟨rfl, rfl⟩
      · exact Iu p a h
      · exact Iu p w (List.mem_of_head? hh)
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    have hmem : ∀ p a, a ∈ (place c (pop σ p1 o1 w) o1 w).q p →
        a ∈ σ.q p ∨ (a = w ∧ c.next o1 = some p) := fun p a ha => mem_fire_q hloop ha
    have hused : (place c (pop σ p1 o1 w) o1 w).used = σ.used := by
      unfold place; split
      · rfl
      · split <;> rfl
    have hsrc : (place c (pop σ p1 o1 w) o1 w).src = σ.src := by
      unfold place; split
      · rfl
      · split <;> rfl
    constructor
    · intro p a hre1 ha hl
      have hold : a ∈ σ.q p := by
        rcases hmem p a ha with h | ⟨_, hn⟩
        · exact h
        · exact absurd (hre p hre1) (wf.endpoint_unfed o1 p hn)
      have hl0 : σ.live a.tok = true := by
        have h' : (pop σ p1 o1 w).live a.tok = true := place_live_back hl
        exact h'
      obtain ⟨h1, h2, h3⟩ := Ia p a hre1 hold hl0
      refine ⟨?_, ?_, ?_⟩
      · -- The word's home does not move once its request is done, and a transaction
        -- that is still live has not been cleared.
        unfold place at hl ⊢
        split
        · exact h1
        · rename_i hn
          split
          · simp only [pop]
            rename_i r hpool
            by_cases hdone : (σ.reqDone w.tok = true)
            · simp [hdone]; exact h1
            · simp only [hdone]
              have hne : a.tok ≠ w.tok := fun e => hdone (e ▸ h2)
              exact (upd_ne _ _ hne).trans h1
          · simp only [pop]
            rename_i hpool
            by_cases hlast : w.last = true
            · simp only [hlast, if_true]
              have hne : a.tok ≠ w.tok := by
                intro e
                simp only [hn, hpool, pop, hlast, if_true] at hl
                rw [e, upd_same] at hl
                simp at hl
              exact (upd_ne _ _ hne).trans h1
            · simp only [hlast]; exact h1
      · -- `reqDone` only grows.
        unfold place
        split
        · exact h2
        · split
          · simp only [pop]
            split
            · by_cases hx : a.tok = w.tok
              · rw [hx, upd_same]
              · rw [upd_ne _ _ hx]; exact h2
            · exact h2
          · exact h2
      · rw [hsrc]; exact h3
    · intro p a ha
      rw [hused]
      rcases hmem p a ha with h | ⟨rfl, _⟩
      · exact Iu p a h
      · exact Iu p1 a (List.mem_of_head? hh)
  | inject p1 w b _ _ _ _ _ hfresh _ hresp _ _ _ _ =>
    have hmem : ∀ p a, a ∈ upd σ.q p1 (σ.q p1 ++ [w]) p → a ∈ σ.q p ∨ (a = w ∧ p = p1) := by
      intro p a ha
      by_cases hx : p = p1
      · subst hx
        rw [upd_same] at ha
        rcases List.mem_append.1 ha with h | h
        · exact Or.inl h
        · exact Or.inr ⟨by simpa using h, rfl⟩
      · rw [upd_ne _ _ hx] at ha
        exact Or.inl ha
    constructor
    · intro p a hr ha hl
      rcases hmem p a ha with h | ⟨rfl, rfl⟩
      · -- An older word's token was already live, or it would not have words.
        have hl0 : σ.live a.tok = true := by
          by_cases hx : a.tok = w.tok
          · cases hlw : σ.live w.tok with
            | true => rw [hx]; exact hlw
            | false =>
              have := Iu p a h
              rw [hx, hfresh hlw] at this
              simp at this
          · have : upd σ.live w.tok true a.tok = true := hl
            rwa [upd_ne _ _ hx] at this
        obtain ⟨h1, h2, h3⟩ := Ia p a hr h hl0
        refine ⟨h1, h2, ?_⟩
        show answersSrc c ((if σ.live w.tok = true then σ.src else upd σ.src w.tok (some p1)) a.tok)
          a.dest = true
        split
        · exact h3
        · rename_i hlw
          have hx : a.tok ≠ w.tok := fun e => hlw (e ▸ hl0)
          rw [upd_ne _ _ hx]
          exact h3
      · obtain ⟨h0, h1, h2, h3⟩ := hresp hr
        refine ⟨h1, h2, ?_⟩
        show answersSrc c ((if σ.live a.tok = true then σ.src else upd σ.src a.tok (some p)) a.tok) a.dest = true
        rw [if_pos h0]
        exact h3
    · intro p a ha
      show upd σ.used w.tok true a.tok = true
      by_cases hx : a.tok = w.tok
      · rw [hx, upd_same]
      · rw [upd_ne _ _ hx]
        rcases hmem p a ha with h | ⟨rfl, _⟩
        · exact Iu p a h
        · exact absurd rfl hx

/-- A response is never an invention: on every legal run, a word in a response slot
    answers its own requester, after its whole request has arrived. -/
theorem answers_run {c : Config} (wf : WF c) (hre : ∀ p, isResponse c p = true → p ∈ c.endpoints)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1))) (t : Nat) :
    AnswersRequest c (s t) := by
  have key : AnswersRequest c (s t) ∧ WordsUsed (s t) := by
    induction t with
    | zero =>
      rw [h0]
      exact ⟨fun _ _ _ h => by simp [St.empty] at h, fun _ _ h => by simp [St.empty] at h⟩
    | succ n ih => exact answers_step wf hre (hs n) ih
  exact key.1

end Wormhole
