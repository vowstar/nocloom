/-
  GenericNoC.WormholeLive: the timed obligation W3 for the wormhole transport.

  `Fair` is the environment condition.  It is an assumption about the arbiter,
  the endpoints and the target contexts, and each clause says what breaks without
  it.  It must not hide a hang: `Instances/TwoTargets.lean` proves that the
  single-hop hanging run of the holding policy satisfies `Fair`.
  `Instances/MultiHop.lean` also gives a hanging run satisfying `Fair`: the
  `cont` clause permits a stalled head at an upstream slot while the holder
  waits for its next word.  See the README.
-/
import GenericNoC.WormholeRules
import GenericNoC.Theorems

namespace Wormhole

/-- A permanent stall from time `τ`. -/
def StallAt (s : Nat → St) (τ : Nat) (h : Hold) : Prop :=
  ∀ t, τ ≤ t → (own (s t) h).isSome = true ∧ ¬ prog (s t) (s (t + 1)) h

/-- The environment condition on a run. -/
structure Fair (c : Config) (s : Nat → St) : Prop where
  /-- The arbiter is fair: a head word that stays, and finds its output free
      again and again, is granted.  A fixed-priority arbiter can break this. -/
  arb_fair : ∀ τ p d o, c.route p d = some o → (∀ t, τ ≤ t → headIs (s t) p d) →
    (∀ a, τ ≤ a → ∃ t, a ≤ t ∧ (s t).grant o = none) → ∃ t, τ ≤ t ∧ (s t).grant o = some p
  /-- An enabled word passes: a holder whose head goes through the output, and
      which finds room again and again, fires. -/
  fire_fair : ∀ τ p d o, c.route p d = some o →
    (∀ t, τ ≤ t → (s t).grant o = some p ∧ headIs (s t) p d) →
    (∀ a, τ ≤ a → ∃ t, a ≤ t ∧ fitsHead c (s t) p o) → ∃ t, τ ≤ t ∧ (s (t + 1)).ev = some (p, o)
  /-- Segments are finite: a holder does not pass words forever without
      releasing the output. -/
  seg_finite : ∀ τ o p, (∀ t, τ ≤ t → (s t).grant o = some p) →
    ∃ a, τ ≤ a ∧ ∀ t, a ≤ t → (s (t + 1)).ev ≠ some (p, o)
  /-- The next word of a stream comes: a holder at an endpoint, or one that can
      change destination, gets its next word at the head, or a head at or upstream
      of it is stuck while it waits.  A holder inside a coherent stream needs no such
      clause: `holder_gets_word` proves it from the endpoint's case. -/
  cont : ∀ τ o p, (p ∈ c.endpoints ∨ c.cross p = true) →
    (∀ t, τ ≤ t → (s t).grant o = some p ∧ (s (t + 1)).ev ≠ some (p, o)) →
    (∃ t, τ ≤ t ∧ headRoutes c (s t) p o) ∨
    (∃ t r d', τ ≤ t ∧ Upstream c r p ∧ (r = p ∨ c.cross p = true) ∧ ¬ headRoutes c (s t) p o ∧
      StallAt s t (.head r d'))
  /-- Contexts free up: a pool that stays full has a stuck response head. -/
  pool_fair : ∀ τ o d r, c.next o = none → c.pool o d = some r →
    (∀ t, τ ≤ t → ¬ room c (s t) o d) → ∃ t d', τ ≤ t ∧ StallAt s t (.head r d')
  /-- A target answers: a transaction that waits forever for words has a full
      response slot with a stuck head. -/
  answer : ∀ τ k, StallAt s τ (.wait k) →
    ∃ t r d', τ ≤ t ∧ ((s t).home k = some r ∨ (s t).src k = some r) ∧
      ¬ CanAdd c (s t) k ∧ StallAt s t (.head r d')

/-- A slot's length stays within capacity. -/
theorem length_le_cap {c : Config} {s : Nat → St} (h0 : s 0 = St.empty)
    (hs : ∀ t, Step c (s t) (s (t + 1))) (r t : Nat) :
    ((s t).q r).length ≤ c.cap r := by
  induction t with
  | zero => rw [h0]; simp [St.empty]
  | succ t ih =>
    have hst := hs t
    generalize s (t + 1) = σ' at hst ⊢
    generalize s t = σ at hst ih
    cases hst with
    | stutter => exact ih
    | arb => exact ih
    | cut p w _ hh _ _ =>
      show (upd σ.q p ({ w with tail := true } :: (σ.q p).tail) r).length ≤ c.cap r
      by_cases hx : r = p
      · subst hx
        rw [upd_same]
        cases hq : σ.q r with
        | nil => rw [hq] at hh; simp at hh
        | cons a as =>
          rw [hq] at ih
          simpa using ih
      · rw [upd_ne _ _ hx]; exact ih
    | fire p o w hh hr hg hroom hloop hcut =>
      rw [fire_q hloop]
      split
      · rename_i hn
        have : (σ.q r).length < c.cap r := by
          rcases hroom with h | ⟨h, _⟩
          · simpa only [room, hn] using h
          · rw [hn] at h; simp at h
        simp only [List.length_append, List.length_cons, List.length_nil]
        omega
      · split
        · rename_i hx
          subst hx
          simp only [List.length_tail]
          omega
        · exact ih
    | inject p w _ _ hcap _ _ _ _ _ _ _ _ _ _ =>
      show (upd σ.q p (σ.q p ++ [w]) r).length ≤ c.cap r
      by_cases hx : r = p
      · subst hx
        rw [upd_same]
        simp only [List.length_append, List.length_cons, List.length_nil]
        omega
      · rw [upd_ne _ _ hx]; exact ih

/-- A fire reports its slot and output, and goes through the head's route. -/
theorem fired_route {c : Config} {σ σ' : St} (hs : Step c σ σ') {p o : Nat}
    (he : σ'.ev = some (p, o)) :
    ∃ w, (σ.q p).head? = some w ∧ c.route p w.dest = some o ∧ c.next o ≠ some p ∧
      (σ'.q p) = (σ.q p).tail := by
  cases hs with
  | stutter => simp at he
  | arb => simp at he
  | cut => simp at he
  | inject => simp [injected] at he
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    rw [fire_ev] at he
    simp only [Option.some.injEq, Prod.mk.injEq] at he
    obtain ⟨rfl, rfl⟩ := he
    refine ⟨w, hh, hr, hloop, ?_⟩
    rw [fire_q hloop]
    simp [hloop]

/-- A taken output stays with its holder unless it becomes free. -/
theorem grant_keep {c : Config} {σ σ' : St} (hs : Step c σ σ') {o p : Nat}
    (hg : σ.grant o = some p) (hn : σ'.grant o ≠ none) : σ'.grant o = some p := by
  cases hs with
  | stutter => exact hg
  | cut => exact hg
  | arb p1 o1 w hh hr hf =>
    have ho : o ≠ o1 := by
      intro e; subst e; rw [hf] at hg; simp at hg
    show upd σ.grant o1 (some p1) o = some p
    rw [upd_ne _ _ ho]; exact hg
  | fire p1 o1 w hh hr hg1 hroom hloop hcut =>
    rw [fire_grant] at hn ⊢
    split at hn
    · exact absurd rfl hn
    · rw [if_neg (by assumption)]; exact hg
  | inject => exact hg

/-- A grant whose holder does not fire through it stays. -/
theorem grant_stay {c : Config} {s : Nat → St} (hs : ∀ t, Step c (s t) (s (t + 1)))
    {o p u : Nat} (hg : (s u).grant o = some p)
    (hno : ∀ t, u ≤ t → (s (t + 1)).ev ≠ some (p, o)) :
    ∀ t, u ≤ t → (s t).grant o = some p := by
  intro t ht
  induction ht with
  | refl => exact hg
  | @step t ht ih =>
    have hown := own_stable_step c (hs t) (h := .grant o p) (own_grant_isSome.2 ih)
      (fun hp => hno t ht hp.2)
    have hs' : (own (s (t + 1)) (.grant o p)).isSome = true := by
      rw [hown]; exact own_grant_isSome.2 ih
    exact own_grant_isSome.1 hs'

/-- From "not again and again" to "from some time on, never". -/
theorem eventually_never {P : Nat → Prop} {τ : Nat}
    (h : ¬ ∀ a, τ ≤ a → ∃ t, a ≤ t ∧ P t) : ∃ a, τ ≤ a ∧ ∀ t, a ≤ t → ¬ P t := by
  apply Classical.byContradiction
  intro hno
  apply h
  intro a ha
  apply Classical.byContradiction
  intro hne
  exact hno ⟨a, ha, fun t ht hp => hne ⟨t, ht, hp⟩⟩

/-- An output with no room from some time on shows a permanently stalled blocker. -/
theorem blocker_of_no_room {c : Config} (hcap : ∀ r, 0 < c.cap r) {s : Nat → St}
    (h0 : s 0 = St.empty) (hs : ∀ t, Step c (s t) (s (t + 1))) (F : Fair c s)
    {o d a : Nat} (hnr : ∀ t, a ≤ t → ¬ room c (s t) o d) :
    ∃ t r d', a ≤ t ∧ blockerOf c o d = some r ∧ StallAt s t (.head r d') := by
  cases hn : c.next o with
  | none =>
    cases hp : c.pool o d with
    | none =>
      exfalso
      apply hnr a (Nat.le_refl a)
      simp only [room, hn, hp]
    | some r =>
      obtain ⟨t, d', ht, hst⟩ := F.pool_fair a o d r hn hp hnr
      exact ⟨t, r, d', ht, by simp only [blockerOf, hn]; exact hp, hst⟩
  | some r =>
    have hfull : ∀ t, a ≤ t → c.cap r ≤ ((s t).q r).length := by
      intro t ht
      have := hnr t ht
      simp only [room, hn] at this
      omega
    obtain ⟨w, hw⟩ : ∃ w, ((s a).q r).head? = some w := by
      have hlen := hfull a (Nat.le_refl a)
      have hc := hcap r
      cases hq : (s a).q r with
      | nil => rw [hq] at hlen; simp at hlen; omega
      | cons x xs => exact ⟨x, by simp⟩
    have hnopop : ∀ t, a ≤ t → ∀ o', (s (t + 1)).ev ≠ some (r, o') := by
      intro t ht o' he
      obtain ⟨x, hx, _, _, hq⟩ := fired_route (hs t) he
      have h1 := hfull (t + 1) (Nat.le_succ_of_le ht)
      have h2 := length_le_cap h0 hs r t
      rw [hq] at h1
      have hpos : 0 < ((s t).q r).length := by
        cases hq' : (s t).q r with
        | nil => rw [hq'] at hx; simp at hx
        | cons _ _ => simp
      have hc := hcap r
      simp only [List.length_tail] at h1
      omega
    have hheld : ∀ t, a ≤ t → headIs (s t) r w.dest := by
      intro t ht
      induction ht with
      | refl => exact ⟨w, hw, rfl⟩
      | @step t ht ih =>
        have hown := own_stable_step c (hs t) (h := .head r w.dest) (own_head_isSome.2 ih)
          (fun hp => by
            obtain ⟨_, o', he⟩ := hp
            exact hnopop t ht o' he)
        have hs' : (own (s (t + 1)) (.head r w.dest)).isSome = true := by
          rw [hown]; exact own_head_isSome.2 ih
        exact own_head_isSome.1 hs'
    refine ⟨a, r, w.dest, Nat.le_refl a, by simp only [blockerOf, hn], ?_⟩
    intro t ht
    refine ⟨own_head_isSome.2 (hheld t ht), ?_⟩
    rintro ⟨_, o', he⟩
    exact hnopop t ht o' he

/-! ### A word behind the head -/

/-- How many words are ahead of the transaction's first word. -/
def tokPos (k : Nat) : List Word → Nat
  | [] => 0
  | w :: ws => if w.tok = k then 0 else tokPos k ws + 1

theorem tokPos_push {k : Nat} {w : Word} :
    ∀ {l : List Word}, (∃ a, a ∈ l ∧ a.tok = k) → tokPos k (l ++ [w]) = tokPos k l := by
  intro l
  induction l with
  | nil => rintro ⟨a, ha, _⟩; simp at ha
  | cons x xs ih =>
    rintro ⟨a, ha, hak⟩
    simp only [List.cons_append, tokPos]
    split
    · rfl
    · rename_i hx
      rcases List.mem_cons.1 ha with rfl | hm
      · exact absurd hak hx
      · rw [ih ⟨a, hm, hak⟩]

/-- Words ahead of a stalled transaction's word never increase, and a pop of the
    head lowers them. -/
theorem tokPos_step {c : Config} {σ σ' : St} (hs : Step c σ σ') {k p : Nat}
    (hheld : hasTok σ p k) (hnp : ¬ prog σ σ' (.ctx k p)) :
    tokPos k (σ'.q p) ≤ tokPos k (σ.q p) ∧
      ∀ o, σ'.ev = some (p, o) → tokPos k (σ'.q p) < tokPos k (σ.q p) := by
  cases hs with
  | stutter => exact ⟨Nat.le_refl _, fun o he => by simp at he⟩
  | arb => exact ⟨Nat.le_refl _, fun o he => by simp at he⟩
  | cut p1 w _ hh _ _ =>
    refine ⟨?_, fun o he => by simp at he⟩
    show tokPos k (upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p) ≤ _
    by_cases hx : p = p1
    · subst hx
      rw [upd_same]
      cases hq : σ.q p with
      | nil => rw [hq] at hh; simp at hh
      | cons a as =>
        rw [hq] at hh
        have : a = w := by simpa using hh
        subst this
        simp [tokPos]
    · rw [upd_ne _ _ hx]; exact Nat.le_refl _
  | inject p1 w _ _ _ _ _ _ _ _ _ _ _ _ _ =>
    refine ⟨?_, fun o he => by simp [injected] at he⟩
    show tokPos k (upd σ.q p1 (σ.q p1 ++ [w]) p) ≤ _
    by_cases hx : p = p1
    · subst hx
      rw [upd_same, tokPos_push hheld]
      exact Nat.le_refl _
    · rw [upd_ne _ _ hx]; exact Nat.le_refl _
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    rw [fire_ev]
    by_cases hn : c.next o1 = some p
    · have hpp : p ≠ p1 := fun e => hloop (by rw [hn, e])
      rw [fire_q hloop, if_pos hn, tokPos_push hheld]
      refine ⟨Nat.le_refl _, fun o he => ?_⟩
      simp only [Option.some.injEq, Prod.mk.injEq] at he
      exact absurd he.1.symm hpp
    · rw [fire_q hloop, if_neg hn]
      by_cases hx : p = p1
      · subst hx
        simp only [if_true]
        have hne : w.tok ≠ k := fun e => hnp ⟨⟨w, hh, e⟩, o1, fire_ev⟩
        cases hq : σ.q p with
        | nil => rw [hq] at hh; simp at hh
        | cons a as =>
          rw [hq] at hh
          have : a = w := by simpa using hh
          subst this
          simp only [List.tail_cons, tokPos, if_neg hne]
          exact ⟨Nat.le_succ _, fun _ _ => Nat.lt_succ_self _⟩
      · simp only [if_neg hx]
        refine ⟨Nat.le_refl _, fun o he => ?_⟩
        simp only [Option.some.injEq, Prod.mk.injEq] at he
        exact absurd he.1.symm hx

/-- The blocker of a transaction's stalled word. -/
theorem ctx_blocker {c : Config} {s : Nat → St} (hs : ∀ t, Step c (s t) (s (t + 1)))
    (F : Fair c s) {τ k p : Nat} (hst : StallAt s τ (.ctx k p)) :
    ∀ n t, τ ≤ t → tokPos k ((s t).q p) = n →
      ∃ u, t ≤ u ∧ ∃ x, (own (s u) x).isSome = true ∧ need c (s u) x (.ctx k p) ∧
        ¬ permit c (s u) x (.ctx k p) ∧ StallAt s u x := by
  intro n
  induction n using Nat.strongRecOn with
  | ind n ih =>
    intro t ht hpos
    have hheld : ∀ j, τ ≤ j → hasTok (s j) p k := fun j hj => own_ctx_isSome.1 (hst j hj).1
    have hstep : ∀ j, τ ≤ j → tokPos k ((s (j + 1)).q p) ≤ tokPos k ((s j).q p) :=
      fun j hj => (tokPos_step (hs j) (hheld j hj) (hst j hj).2).1
    have hmono : ∀ j, t ≤ j → tokPos k ((s j).q p) ≤ n := by
      intro j hj
      induction hj with
      | refl => exact Nat.le_of_eq hpos
      | @step j hj ih' => exact Nat.le_trans (hstep j (Nat.le_trans ht hj)) ih'
    by_cases hpop : ∃ j o, t ≤ j ∧ (s (j + 1)).ev = some (p, o)
    · obtain ⟨j, o, hj, he⟩ := hpop
      have hτj : τ ≤ j := Nat.le_trans ht hj
      have hlt := (tokPos_step (hs j) (hheld j hτj) (hst j hτj).2).2 o he
      have hm := hmono j hj
      obtain ⟨u, hu, rest⟩ := ih _ (Nat.lt_of_lt_of_le hlt hm) (j + 1) (Nat.le_succ_of_le hτj) rfl
      exact ⟨u, Nat.le_trans (Nat.le_succ_of_le hj) hu, rest⟩
    · have hnopop : ∀ j o, t ≤ j → (s (j + 1)).ev ≠ some (p, o) :=
        fun j o hj he => hpop ⟨j, o, hj, he⟩
      obtain ⟨a, ha, hak⟩ := hheld t ht
      obtain ⟨w, hw⟩ : ∃ w, ((s t).q p).head? = some w := by
        cases hq : (s t).q p with
        | nil => rw [hq] at ha; simp at ha
        | cons x xs => exact ⟨x, by simp⟩
      have hheadAt : ∀ j, t ≤ j → headIs (s j) p w.dest := by
        intro j hj
        induction hj with
        | refl => exact ⟨w, hw, rfl⟩
        | @step j hj ih' =>
          have hown := own_stable_step c (hs j) (h := .head p w.dest) (own_head_isSome.2 ih')
            (fun hp => by
              obtain ⟨_, o', he⟩ := hp
              exact hnopop j o' hj he)
          have hs' : (own (s (j + 1)) (.head p w.dest)).isSome = true := by
            rw [hown]; exact own_head_isSome.2 ih'
          exact own_head_isSome.1 hs'
      have hxs : ∀ u, t ≤ u → StallAt s u (.head p w.dest) := by
        intro u hu j hj
        refine ⟨own_head_isSome.2 (hheadAt j (Nat.le_trans hu hj)), ?_⟩
        rintro ⟨_, o', he⟩
        exact hnopop j o' (Nat.le_trans hu hj) he
      -- Some time from `t` on, the word is not free to pass.
      have hnotfree : ∃ u, t ≤ u ∧ ¬ permit c (s u) (.head p w.dest) (.ctx k p) := by
        apply Classical.byContradiction
        intro hall
        have hperm : ∀ u, t ≤ u → permit c (s u) (.head p w.dest) (.ctx k p) := by
          intro u hu
          apply Classical.byContradiction
          intro hn
          exact hall ⟨u, hu, hn⟩
        obtain ⟨w1, hw1, hd1⟩ := hheadAt t (Nat.le_refl t)
        obtain ⟨_, o, hr, _, _⟩ := hperm t (Nat.le_refl t) rfl w1 hw1 hd1
        have hgrant : ∀ u, t ≤ u → (s u).grant o = some p ∧ fitsHead c (s u) p o := by
          intro u hu
          obtain ⟨w2, hw2, hd2⟩ := hheadAt u hu
          obtain ⟨_, o2, hr2, hg2, hroom2⟩ := hperm u hu rfl w2 hw2 hd2
          rw [hr] at hr2
          have : o = o2 := by simpa using hr2
          subst this
          exact ⟨hg2, hroom2⟩
        obtain ⟨n', hn', he⟩ := F.fire_fair t p w.dest o hr
          (fun u hu => ⟨(hgrant u hu).1, hheadAt u hu⟩)
          (fun a ha => ⟨a, Nat.le_refl a, (hgrant a ha).2⟩)
        exact hnopop n' o hn' he
      obtain ⟨u, hu, hnp⟩ := hnotfree
      exact ⟨u, hu, .head p w.dest, own_head_isSome.2 (hheadAt u hu),
        ⟨rfl, hheadAt u hu, hheld u (Nat.le_trans ht hu)⟩, hnp, hxs u hu⟩

/-- Strictly increasing. -/
def up : List Nat → Bool
  | [] => true
  | [_] => true
  | a :: b :: r => decide (a < b) && up (b :: r)

theorem up_tail {a : Nat} {l : List Nat} (h : up (a :: l) = true) : up l = true := by
  cases l with
  | nil => rfl
  | cons b r =>
    simp only [up, Bool.and_eq_true] at h
    exact h.2

theorem up_lt : ∀ {a : Nat} {l : List Nat}, up (a :: l) = true → ∀ x, x ∈ l → a < x := by
  intro a l
  induction l generalizing a with
  | nil => intro _ x hx; simp at hx
  | cons b r ih =>
    intro h x hx
    simp only [up, Bool.and_eq_true, decide_eq_true_eq] at h
    rcases List.mem_cons.1 hx with rfl | hm
    · exact h.1
    · exact Nat.lt_trans h.1 (ih h.2 x hm)

/-- The hop table is right for the routes. -/
def hopsOk (c : Config) : Bool :=
  up c.slots && c.routes.all fun r =>
    c.slots.contains r.1 && decide (0 < c.hops r.1 r.2.1) && decide (c.hops r.1 r.2.1 < c.hopMax) &&
      (match c.next r.2.2 with
        | some x => decide (c.hops x r.2.1 < c.hops r.1 r.2.1)
        | none => true)

structure HopFacts (c : Config) : Prop where
  up_slots : up c.slots = true
  mem : ∀ {p d o}, c.route p d = some o → p ∈ c.slots
  pos : ∀ {p d o}, c.route p d = some o → 0 < c.hops p d
  lt_max : ∀ {p d o}, c.route p d = some o → c.hops p d < c.hopMax
  down : ∀ {p d o r}, c.route p d = some o → c.next o = some r → c.hops r d < c.hops p d

theorem hopFacts {c : Config} (h : hopsOk c = true) : HopFacts c := by
  simp only [hopsOk, Bool.and_eq_true, List.all_eq_true] at h
  obtain ⟨h1, h2⟩ := h
  have hrow : ∀ {p d o}, c.route p d = some o →
      p ∈ c.slots ∧ 0 < c.hops p d ∧ c.hops p d < c.hopMax ∧
        ∀ r, c.next o = some r → c.hops r d < c.hops p d := by
    intro p d o hr
    have := h2 (p, d, o) (Config.route_mem hr)
    simp only [decide_eq_true_eq, List.contains_iff_mem] at this
    obtain ⟨⟨⟨hm, hp⟩, hl⟩, hn⟩ := this
    refine ⟨hm, hp, hl, ?_⟩
    intro r hr'
    rw [hr'] at hn
    simpa using hn
  exact ⟨h1, fun hr => (hrow hr).1, fun hr => (hrow hr).2.1, fun hr => (hrow hr).2.2.1,
    fun hr hn => (hrow hr).2.2.2 _ hn⟩

/-! ### A holder inside a coherent stream gets its next word -/

/-- A fire places its word in the slot behind the output. -/
theorem fired_pushes {c : Config} {σ σ' : St} (hs : Step c σ σ') {p o x : Nat}
    (he : σ'.ev = some (p, o)) (hn : c.next o = some x) : σ'.q x ≠ [] := by
  cases hs with
  | stutter => simp at he
  | arb => simp at he
  | cut => simp at he
  | inject => simp [injected] at he
  | fire p1 o1 w hh hr hg hroom hloop hcut =>
    rw [fire_ev] at he
    simp only [Option.some.injEq, Prod.mk.injEq] at he
    obtain ⟨rfl, rfl⟩ := he
    rw [fire_q hloop, if_pos hn]
    simp

/-- With no word through its output, a coherent holder stays empty and its feeder
    waits too.  So the wait climbs the stream to the endpoint, whose answer is the
    only assumption used, and from there the word comes down. -/
theorem holder_gets_word {c : Config} (wf : WF c) (hf : HopFacts c) (hcap : ∀ r, 0 < c.cap r)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1))) (F : Fair c s) :
    ∀ n p o τ, c.cross p = false →
      (∀ d1, (s τ).openOut p = some d1 → c.hopMax - c.hops p d1 ≤ n) →
      (∀ t, τ ≤ t → (s t).grant o = some p ∧ (s (t + 1)).ev ≠ some (p, o)) →
      ∃ t, τ ≤ t ∧ headRoutes c (s t) p o := by
  have I : ∀ t, Inv c (s t) := inv_run wf h0 hs
  intro n
  induction n using Nat.strongRecOn with
  | ind n ih =>
    intro p o τ hc hn hall
    apply Classical.byContradiction
    intro hno
    have hnone : ∀ t, τ ≤ t → ¬ headRoutes c (s t) p o := fun t ht h => hno ⟨t, ht, h⟩
    by_cases hep : p ∈ c.endpoints
    · -- The endpoint's own case is the assumption.
      rcases F.cont τ o p (Or.inl hep) hall with h | ⟨t, r, d', ht, _, hwho, hnot, hxs⟩
      · exact hno h
      · have hrp : r = p := by
          rcases hwho with h | h
          · exact h
          · rw [hc] at h; simp at h
        subst hrp
        obtain ⟨w, hw, hwd⟩ := own_head_isSome.1 (hxs t (Nat.le_refl t)).1
        have hne : c.route r w.dest ≠ some o := fun e => hnot ⟨w, hw, e⟩
        have := (hold_across_needs_cross (I t) (hall t ht).1 ⟨w, hw, rfl⟩ hne).1
        rw [hc] at this
        simp at this
    · -- Inside the stream: the holder stays empty, and its feeder waits.
      have hempty : ∀ t, τ ≤ t → (s t).q p = [] := by
        intro t ht
        cases hq : (s t).q p with
        | nil => rfl
        | cons a as =>
          exfalso
          have hflow := ((I t).flow p hc (Or.inl hep)).1
          rw [hq] at hflow
          rcases (I t).coh o p hc (hall t ht).1 with ⟨w, hw, _, hrw⟩ | ⟨d2, hd2, hr2⟩
          · exact hnone t ht ⟨w, hw, hrw⟩
          · have had := hflow.1 d2 hd2
            refine hnone t ht ⟨a, by rw [hq]; rfl, ?_⟩
            rw [had]; exact hr2
      obtain ⟨d1, hop, hr1⟩ : ∃ d1, (s τ).openOut p = some d1 ∧ c.route p d1 = some o := by
        rcases (I τ).coh o p hc (hall τ (Nat.le_refl τ)).1 with ⟨w, hw, _, hrw⟩ | h
        · exact absurd ⟨w, hw, hrw⟩ (hnone τ (Nat.le_refl τ))
        · exact h
      have hin : (s τ).openIn p = some d1 := by
        have := ((I τ).flow p hc (Or.inl hep)).2
        rw [hempty τ (Nat.le_refl τ), hop] at this
        exact this.symm
      obtain ⟨o', p', hn', hg', hop'⟩ := (I τ).feed p d1 hc hep hin
      obtain ⟨d0, hd0⟩ := (I τ).grant_route o' p' hg'
      have hc' : c.cross p' = false := wf.feed_coherent o' p p' d0 hn' hc hd0
      have hr' : c.route p' d1 = some o' := by
        rcases (I τ).coh o' p' hc' hg' with ⟨_, _, hnone', _⟩ | ⟨d3, hd3, hr3⟩
        · rw [hnone'] at hop'; simp at hop'
        · rw [hop'] at hd3
          have : d1 = d3 := by simpa using hd3
          rw [this]; exact hr3
      have hnofire : ∀ t, τ ≤ t → (s (t + 1)).ev ≠ some (p', o') := by
        intro t ht he
        exact fired_pushes (hs t) he hn' (hempty (t + 1) (Nat.le_succ_of_le ht))
      have hkeep := grant_stay hs hg' hnofire
      have hdown := hf.down hr' hn'
      have hlt := hf.lt_max hr'
      -- The stream's destination has more steps left at the feeder, so the measure falls.
      have hmeasure : c.hopMax - c.hops p' d1 < n := by
        have := hn d1 hop
        omega
      obtain ⟨u, hu, w, hw, hrw⟩ := ih _ hmeasure p' o' τ hc'
        (fun d2 hd2 => by
          rw [hop'] at hd2
          have : d1 = d2 := by simpa using hd2
          subst this
          exact Nat.le_refl _)
        (fun t ht => ⟨hkeep t ht, hnofire t ht⟩)
      -- That word has room below it, so it passes, which contradicts the wait.
      have hheld : ∀ j, u ≤ j → headIs (s j) p' w.dest := by
        intro j hj
        induction hj with
        | refl => exact ⟨w, hw, rfl⟩
        | @step j hj ihj =>
          have hown := own_stable_step c (hs j) (h := .head p' w.dest) (own_head_isSome.2 ihj)
            (fun hp => by
              obtain ⟨_, o2, he⟩ := hp
              obtain ⟨x, hx, hrx, _, _⟩ := fired_route (hs j) he
              obtain ⟨y, hy, hyd⟩ := ihj
              rw [hx] at hy
              have : x = y := by simpa using hy
              subst this
              rw [hyd, hrw] at hrx
              have : o' = o2 := by simpa using hrx
              subst this
              exact hnofire j (Nat.le_trans hu hj) he)
          have hs' : (own (s (j + 1)) (.head p' w.dest)).isSome = true := by
            rw [hown]; exact own_head_isSome.2 ihj
          exact own_head_isSome.1 hs'
      have hroom : ∀ a, u ≤ a → ∃ j, a ≤ j ∧ fitsHead c (s j) p' o' := by
        intro a ha
        refine ⟨a, Nat.le_refl a, fitsHead_of_room fun _ => ?_⟩
        simp only [room, hn', hempty a (Nat.le_trans hu ha), List.length_nil]
        exact hcap p
      obtain ⟨j, hj, he⟩ := F.fire_fair u p' w.dest o' hrw
        (fun v hv => ⟨hkeep v (Nat.le_trans hu hv), hheld v hv⟩) hroom
      exact hnofire j (Nat.le_trans hu hj) he

section Main

variable {c : Config} {t : EdgeTable} {node : Hold → Nat}

/-- W3 for the wormhole transport, under `Fair`. -/
def instanceLemmas (wf : WF c) (hf : HopFacts c) (hcap : ∀ r, 0 < c.cap r)
    (hrank : t.checkRank = true) :
    GenericNoC.InstanceLemmas (noc c t node (Fair c) hrank) where
  rank_stable := by
    intro _ _ _ _ _ _ _ _ _
    rfl
  eventual_blocker := by
    intro _ s hlegal F τ y _ hstall
    have h0 : s 0 = St.empty := hlegal.1
    have hs : ∀ n, Step c (s n) (s (n + 1)) := hlegal.2
    have F' : Fair c s := F
    cases y with
    | ctx k p =>
      have hst : StallAt s τ (.ctx k p) := hstall
      obtain ⟨u, hu, x, hxo, hn, hnp, hxs⟩ :=
        ctx_blocker hs F' hst _ τ (Nat.le_refl τ) rfl
      exact ⟨u, hu, x, ⟨hxo, (hst u hu).1, hn, hnp⟩, hxs⟩
    | wait k =>
      have hst : StallAt s τ (.wait k) := hstall
      obtain ⟨u, r, d', hu, hhome, hfull, hxs⟩ := F'.answer τ k hst
      exact ⟨u, hu, .head r d', ⟨(hxs u (Nat.le_refl u)).1, (hst u hu).1, hhome, hfull⟩, hxs⟩
    | head p d =>
      have hst : ∀ n, τ ≤ n → headIs (s n) p d ∧ ∀ o', (s (n + 1)).ev ≠ some (p, o') := by
        intro n hn
        obtain ⟨h1, h2⟩ := hstall n hn
        have hi : headIs (s n) p d := own_head_isSome.1 h1
        exact ⟨hi, fun o' he => h2 ⟨hi, o', he⟩⟩
      have I : Inv c (s τ) := inv_run wf h0 hs τ
      obtain ⟨o, hr⟩ := Option.isSome_iff_exists.1 (head_routed I (hst τ (Nat.le_refl τ)).1)
      by_cases hown : ∃ u, τ ≤ u ∧ (s u).grant o = some p
      · -- The word has its grant, so room is what is missing.
        obtain ⟨u, hu, hgu⟩ := hown
        have hkeep := grant_stay hs hgu (fun n hn => (hst n (Nat.le_trans hu hn)).2 o)
        have hnoroom : ¬ ∀ a, u ≤ a → ∃ n, a ≤ n ∧ fitsHead c (s n) p o := by
          intro hio
          obtain ⟨n, hn, he⟩ := F'.fire_fair u p d o hr
            (fun n hn => ⟨hkeep n hn, (hst n (Nat.le_trans hu hn)).1⟩) hio
          exact (hst n (Nat.le_trans hu hn)).2 o he
        obtain ⟨a, ha, hnr⟩ := eventually_never hnoroom
        obtain ⟨n, r, d', hn, hb, hxs⟩ := blocker_of_no_room (d := d) hcap h0 hs F'
          (fun t ht hrm => hnr t ht (fun w hw => Or.inl (by
            obtain ⟨y, hy, hyd⟩ := (hst t (Nat.le_trans hu (Nat.le_trans ha ht))).1
            rw [hw] at hy
            have : w = y := by simpa using hy
            subst this
            rw [hyd]
            exact hrm)))
        have han : u ≤ n := Nat.le_trans ha hn
        refine ⟨n, Nat.le_trans hu han, .head r d', ⟨(hxs n (Nat.le_refl n)).1,
          own_head_isSome.2 (hst n (Nat.le_trans hu han)).1,
          ⟨(hst n (Nat.le_trans hu han)).1, o, hr, hkeep n han, hb⟩, ?_⟩, hxs⟩
        intro hperm
        exact hnr n hn (hperm o hr (hkeep n han) hb)
      · -- The word has no grant.
        have hnever : ∀ u, τ ≤ u → (s u).grant o ≠ some p := fun u hu he => hown ⟨u, hu, he⟩
        have hnofree : ¬ ∀ a, τ ≤ a → ∃ n, a ≤ n ∧ (s n).grant o = none := by
          intro hio
          obtain ⟨n, hn, he⟩ := F'.arb_fair τ p d o hr (fun n hn => (hst n hn).1) hio
          exact hnever n hn he
        obtain ⟨a, ha, hnf⟩ := eventually_never hnofree
        obtain ⟨p', hp'⟩ : ∃ p', (s a).grant o = some p' := by
          cases hg : (s a).grant o with
          | none => exact absurd hg (hnf a (Nat.le_refl a))
          | some p' => exact ⟨p', rfl⟩
        have hkeep : ∀ n, a ≤ n → (s n).grant o = some p' := by
          intro n hn
          induction hn with
          | refl => exact hp'
          | @step n hn ih => exact grant_keep (hs n) ih (hnf (n + 1) (Nat.le_succ_of_le hn))
        obtain ⟨b, hb, hno⟩ := F'.seg_finite a o p' hkeep
        have hne : p' ≠ p := by
          intro e; subst e; exact hnever a ha hp'
        have hτb : τ ≤ b := Nat.le_trans ha hb
        refine ⟨b, hτb, .grant o p', ⟨own_grant_isSome.2 (hkeep b hb),
          own_head_isSome.2 (hst b hτb).1, ⟨(hst b hτb).1, hr, hne⟩, ?_⟩, ?_⟩
        · intro hperm
          exact hperm (hkeep b hb)
        · intro n hn
          refine ⟨own_grant_isSome.2 (hkeep n (Nat.le_trans hb hn)), ?_⟩
          rintro ⟨_, he⟩
          exact hno n hn he
    | grant o p =>
      have hst : ∀ n, τ ≤ n → (s n).grant o = some p ∧ (s (n + 1)).ev ≠ some (p, o) := by
        intro n hn
        obtain ⟨h1, h2⟩ := hstall n hn
        have hg : (s n).grant o = some p := own_grant_isSome.1 h1
        exact ⟨hg, fun he => h2 ⟨hg, he⟩⟩
      have hnext : (∃ u, τ ≤ u ∧ headRoutes c (s u) p o) ∨
          (∃ u r d', τ ≤ u ∧ Upstream c r p ∧ (r = p ∨ c.cross p = true) ∧
            ¬ headRoutes c (s u) p o ∧ StallAt s u (.head r d')) := by
        by_cases hcase : p ∈ c.endpoints ∨ c.cross p = true
        · exact F'.cont τ o p hcase hst
        · have hc : c.cross p = false := by
            cases hx : c.cross p with
            | false => rfl
            | true => exact absurd (Or.inr hx) hcase
          exact Or.inl (holder_gets_word wf hf hcap h0 hs F' c.hopMax p o τ hc
            (fun _ _ => Nat.sub_le _ _) hst)
      rcases hnext with ⟨u, hu, w, hw, hr⟩ | ⟨u, r, d', hu, hup, hwho, hno, hxs⟩
      · -- The holder's next word is at the head and does not pass.
        have hheld : ∀ n, u ≤ n → headIs (s n) p w.dest := by
          intro n hn
          induction hn with
          | refl => exact ⟨w, hw, rfl⟩
          | @step n hn ih =>
            have hown := own_stable_step c (hs n) (h := .head p w.dest) (own_head_isSome.2 ih)
              (fun hp => by
                obtain ⟨_, o', he⟩ := hp
                obtain ⟨x, hx, hrx, _, _⟩ := fired_route (hs n) he
                obtain ⟨y, hy, hyd⟩ := ih
                rw [hx] at hy
                have : x = y := by simpa using hy
                subst this
                rw [hyd, hr] at hrx
                have : o = o' := by simpa using hrx
                subst this
                exact (hst n (Nat.le_trans hu hn)).2 he)
            have hs' : (own (s (n + 1)) (.head p w.dest)).isSome = true := by
              rw [hown]; exact own_head_isSome.2 ih
            exact own_head_isSome.1 hs'
        have hnoroom : ¬ ∀ a, u ≤ a → ∃ n, a ≤ n ∧ fitsHead c (s n) p o := by
          intro hio
          obtain ⟨n, hn, he⟩ := F'.fire_fair u p w.dest o hr
            (fun n hn => ⟨(hst n (Nat.le_trans hu hn)).1, hheld n hn⟩) hio
          exact (hst n (Nat.le_trans hu hn)).2 he
        obtain ⟨a, ha, hnr⟩ := eventually_never hnoroom
        obtain ⟨n, r, d', hn, hb, hxs⟩ := blocker_of_no_room (d := w.dest) hcap h0 hs F'
          (fun t ht hrm => hnr t ht (fun x hx => Or.inl (by
            obtain ⟨y, hy, hyd⟩ := hheld t (Nat.le_trans ha ht)
            rw [hx] at hy
            have : x = y := by simpa using hy
            subst this
            rw [hyd]
            exact hrm)))
        have hτn : τ ≤ n := Nat.le_trans hu (Nat.le_trans ha hn)
        refine ⟨n, hτn, .head r d', ⟨(hxs n (Nat.le_refl n)).1,
          own_grant_isSome.2 (hst n hτn).1,
          ⟨(hst n hτn).1, Or.inl ⟨w.dest, hheld n (Nat.le_trans ha hn), hr, hb⟩⟩, ?_⟩, hxs⟩
        intro hperm
        exact hnr n hn (hperm.1 ⟨w.dest, hheld n (Nat.le_trans ha hn), hr, hb⟩)
      · -- A head at or upstream of the holder is stuck while the holder waits.
        have hi : headIs (s u) r d' := own_head_isSome.1 (hxs u (Nat.le_refl u)).1
        have ha : Awaits c (s u) r d' o p := ⟨hup, hi, hno, hwho⟩
        refine ⟨u, hu, .head r d', ⟨(hxs u (Nat.le_refl u)).1,
          own_grant_isSome.2 (hst u hu).1, ⟨(hst u hu).1, Or.inr ha⟩, ?_⟩, hxs⟩
        intro hperm
        exact hperm.2 ha

/-- The instance result.  On every legal fair run of a checked instance, a held
    point makes progress, and its owner does not change before that step.  This
    is a statement about the model, the table and `Fair`, not about RTL. -/
theorem checked_instance_advances (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r)
    (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node)
    (hheads : headsInRange c t node = true)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1)))
    (F : Fair c s) (τ : Nat) (h : Hold) {tok : Nat} (hown : own (s τ) h = some tok) :
    ∃ u, τ ≤ u ∧ prog (s u) (s (u + 1)) h ∧ ∀ j, τ ≤ j → j ≤ u → own (s j) h = some tok :=
  GenericNoC.advances_on_every_compliant_run (noc c t node (Fair c) hrank)
    (structural hwf hrank hcov hheads)
    (instanceLemmas (wfCheck_sound hwf) (hopFacts hhops) hcap hrank)
    ⟨h0, hs⟩ F τ h hown

/-- W7: accepted work is the live transactions, by token. -/
noncomputable def liveWork (hrank : t.checkRank = true) (env : (Nat → St) → Prop) :
    GenericNoC.AcceptedWorkInventory (noc c t node env hrank) where
  Work := Nat
  pending := fun σ k => St.live σ k = true
  tok := fun k => k
  token_unique := fun _ _ _ _ _ _ h => h

/-- The pending-held assumption is proved for this model: every live transaction
    has a word in some slot, or it waits for one, and both are hold points. -/
theorem liveWork_held (hrank : t.checkRank = true) (env : (Nat → St) → Prop) :
    GenericNoC.PendingHeld (liveWork (c := c) (node := node) hrank env) where
  held := by
    intro σ _ k hlive
    by_cases hany : ∃ p, hasTok σ p k
    · obtain ⟨p, hp⟩ := hany
      exact ⟨.ctx k p, own_ctx_some hp⟩
    · exact ⟨.wait k, own_wait_some ⟨hlive, fun p hp => hany ⟨p, hp⟩⟩⟩

/-- On every legal fair run of a checked instance, a live transaction holds a
    point, and that point makes progress with the same owner.  This is one step
    of progress, not completion, and it is about the model, not about RTL. -/
theorem checked_instance_live_work_advances (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r)
    (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node)
    (hheads : headsInRange c t node = true)
    {s : Nat → St} (h0 : s 0 = St.empty) (hs : ∀ n, Step c (s n) (s (n + 1)))
    (F : Fair c s) (τ k : Nat) (hlive : (s τ).live k = true) :
    ∃ h u, own (s τ) h = some k ∧ τ ≤ u ∧ prog (s u) (s (u + 1)) h ∧
      ∀ j, τ ≤ j → j ≤ u → own (s j) h = some k := by
  have hheld : ∃ h, own (s τ) h = some k := by
    by_cases hany : ∃ p, hasTok (s τ) p k
    · obtain ⟨p, hp⟩ := hany
      exact ⟨.ctx k p, own_ctx_some hp⟩
    · exact ⟨.wait k, own_wait_some ⟨hlive, fun p hp => hany ⟨p, hp⟩⟩⟩
  obtain ⟨h, hown⟩ := hheld
  obtain ⟨u, hu, hp, hsame⟩ :=
    checked_instance_advances hwf hcap hhops hrank hcov hheads h0 hs F τ h hown
  exact ⟨h, u, hown, hu, hp, hsame⟩

end Main

end Wormhole
