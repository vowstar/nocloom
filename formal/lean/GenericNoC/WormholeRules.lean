/-
  GenericNoC.WormholeRules: the wormhole transport as a `GenericNoC`, and W2
  split into one lemma per graph rule.

  Each rule has a list of required graph connections computed from the instance
  tables, a lemma that a real block of that shape is on the list, and a
  decidable check that the graph connects every pair on the list.

  | Rule | Blocker | Blocked | Why |
  | --- | --- | --- | --- |
  | `arbitration` | grant held by another input | head word | one holder per output |
  | `nextWord` | downstream head | head word | no room downstream |
  | `nextGrant` | downstream head | held grant | no room downstream |
  | `poolWord` | response head | head word | target contexts full |
  | `poolGrant` | response head | held grant | target contexts full |
  | `holdAcross` | holder's head going elsewhere | held grant | grant survives past a word and the next word is not next |
  | `context` | a slot's head | a transaction in that slot or owed by it | first in, first out, and bounded room |

  `holdAcross` is the only rule whose list depends on the release policy.  Its
  lemma is `hold_across_needs_cross`: for a slot the instance declares coherent
  the block cannot occur at all.
-/
import GenericNoC.Wormhole

namespace Wormhole

/-- Graph rules of this model. -/
inductive Rule where
  | arbitration | nextWord | nextGrant | poolWord | poolGrant | holdAcross
  /-- A slot's head holds up a transaction's word in that slot, or a transaction
      waiting for that response slot.  The blocked point has rank above every
      graph node and blocks nothing, so this needs no edge in the artifact. -/
  | context
  deriving DecidableEq, Repr

/-- Inputs that can hold an output. -/
def feeders (c : Config) (o : Nat) : List Nat :=
  (c.routes.filter (fun r => r.2.2 == o)).map (fun r => r.1)

/-- Destinations routed at a slot. -/
def destsOf (c : Config) (r : Nat) : List Nat :=
  (c.routes.filter (fun e => e.1 == r)).map (fun e => e.2.1)

/-- The connections each rule requires. -/
def required (c : Config) : Rule → List (Hold × Hold)
  | .arbitration => c.routes.flatMap fun e =>
      (feeders c e.2.2).filterMap fun p' =>
        if p' = e.1 ∨ c.bound e.2.1 = true then none
        else some (Hold.grant e.2.2 p', Hold.head e.1 e.2.1)
  | .nextWord => c.routes.flatMap fun e =>
      match c.next e.2.2 with
      | some r => (destsOf c r).map fun d' => (Hold.head r d', Hold.head e.1 e.2.1)
      | none => []
  | .nextGrant => c.routes.flatMap fun e =>
      match c.next e.2.2 with
      | some r => (destsOf c r).map fun d' => (Hold.head r d', Hold.grant e.2.2 e.1)
      | none => []
  | .poolWord => c.routes.flatMap fun e =>
      match c.next e.2.2, c.pool e.2.2 e.2.1 with
      | none, some r => (destsOf c r).map fun d' => (Hold.head r d', Hold.head e.1 e.2.1)
      | _, _ => []
  | .poolGrant => c.routes.flatMap fun e =>
      match c.next e.2.2, c.pool e.2.2 e.2.1 with
      | none, some r => (destsOf c r).map fun d' => (Hold.head r d', Hold.grant e.2.2 e.1)
      | _, _ => []
  | .holdAcross => c.routes.flatMap fun e =>
      if c.cross e.1 = true ∧ c.mayHold e.1 e.2.1 = true then
        (c.ups e.1).flatMap fun r =>
          (c.routes.filter (fun f => f.1 == r && (r != e.1 || f.2.2 != e.2.2))).map
            fun f => (Hold.head r f.2.1, Hold.grant e.2.2 e.1)
      else []
  | .context => []

/-- Required pairs are made of heads and grants only. -/
theorem required_kinds {c : Config} {r : Rule} {x y : Hold} (h : (x, y) ∈ required c r) :
    (∀ k p, x ≠ .ctx k p) ∧ (∀ k, x ≠ .wait k) ∧ (∀ k p, y ≠ .ctx k p) ∧ (∀ k, y ≠ .wait k) := by
  have hshape : (∃ a b, x = .head a b ∨ x = .grant a b) ∧ (∃ a b, y = .head a b ∨ y = .grant a b) := by
    cases r with
    | context => simp [required] at h
    | arbitration =>
      simp only [required, List.mem_flatMap, List.mem_filterMap] at h
      obtain ⟨e, _, p', _, hif⟩ := h
      split at hif
      · simp at hif
      · simp only [Option.some.injEq, Prod.mk.injEq] at hif
        obtain ⟨rfl, rfl⟩ := hif
        exact ⟨⟨_, _, Or.inr rfl⟩, ⟨_, _, Or.inl rfl⟩⟩
    | nextWord | nextGrant =>
      simp only [required, List.mem_flatMap] at h
      obtain ⟨e, _, hin⟩ := h
      split at hin
      · simp only [List.mem_map, Prod.mk.injEq] at hin
        obtain ⟨_, _, rfl, rfl⟩ := hin
        first
          | exact ⟨⟨_, _, Or.inl rfl⟩, ⟨_, _, Or.inl rfl⟩⟩
          | exact ⟨⟨_, _, Or.inl rfl⟩, ⟨_, _, Or.inr rfl⟩⟩
      · simp at hin
    | poolWord | poolGrant =>
      simp only [required, List.mem_flatMap] at h
      obtain ⟨e, _, hin⟩ := h
      split at hin
      · simp only [List.mem_map, Prod.mk.injEq] at hin
        obtain ⟨_, _, rfl, rfl⟩ := hin
        first
          | exact ⟨⟨_, _, Or.inl rfl⟩, ⟨_, _, Or.inl rfl⟩⟩
          | exact ⟨⟨_, _, Or.inl rfl⟩, ⟨_, _, Or.inr rfl⟩⟩
      · simp at hin
    | holdAcross =>
      simp only [required, List.mem_flatMap] at h
      obtain ⟨e, _, hin⟩ := h
      split at hin
      · simp only [List.mem_flatMap, List.mem_map, Prod.mk.injEq] at hin
        obtain ⟨_, _, _, _, rfl, rfl⟩ := hin
        exact ⟨⟨_, _, Or.inl rfl⟩, ⟨_, _, Or.inr rfl⟩⟩
      · simp at hin
  obtain ⟨⟨a, b, hx⟩, ⟨a', b', hy⟩⟩ := hshape
  refine ⟨?_, ?_, ?_, ?_⟩
  · intro k p e; subst e; rcases hx with h | h <;> cases h
  · intro k e; subst e; rcases hx with h | h <;> cases h
  · intro k p e; subst e; rcases hy with h | h <;> cases h
  · intro k e; subst e; rcases hy with h | h <;> cases h

/-! ### Membership helpers -/

theorem mem_feeders {c : Config} {p d o : Nat} (h : c.route p d = some o) : p ∈ feeders c o := by
  have hm := Config.route_mem h
  simp only [feeders, List.mem_map, List.mem_filter]
  exact ⟨(p, d, o), ⟨hm, by simp⟩, rfl⟩

theorem mem_destsOf {c : Config} {r d : Nat} (h : (c.route r d).isSome = true) :
    d ∈ destsOf c r := by
  obtain ⟨o, ho⟩ := Option.isSome_iff_exists.1 h
  have hm := Config.route_mem ho
  simp only [destsOf, List.mem_map, List.mem_filter]
  exact ⟨(r, d, o), ⟨hm, by simp⟩, rfl⟩

/-- A held slot's head is routed. -/
theorem head_routed {c : Config} {σ : St} (I : Inv c σ) {r d : Nat} (h : headIs σ r d) :
    (c.route r d).isSome = true := by
  obtain ⟨w, hw, rfl⟩ := h
  exact I.q_route r w (List.mem_of_head? hw)

/-! ### One lemma per rule: a real block is on the rule's list -/

/-- An input waits for an output another input holds. -/
theorem block_arbitration {c : Config} {σ : St} (I : Inv c σ) {o p' p d : Nat}
    (hr : c.route p d = some o) (hne : p' ≠ p) (hg : σ.grant o = some p')
    (hfree : c.bound d = false) :
    (Hold.grant o p', Hold.head p d) ∈ required c .arbitration := by
  obtain ⟨d', hd'⟩ := I.grant_route o p' hg
  simp only [required, List.mem_flatMap, List.mem_filterMap]
  refine ⟨(p, d, o), Config.route_mem hr, p', mem_feeders hd', ?_⟩
  simp [hne, hfree]

/-- A bound destination is never held up by another input: one slot at a time
    touches it. -/
theorem bound_no_contention {c : Config} (wf : WF c) {σ : St} (I : Inv c σ) {o p' p d : Nat}
    (hb : c.bound d = true) (hr : c.route p d = some o) (hh : headIs σ p d)
    (hg : σ.grant o = some p') : p' = p := by
  obtain ⟨d', hd'⟩ := I.grant_route o p' hg
  have hdd := wf.bound_own p d o p' d' hb hr hd'
  subst hdd
  obtain ⟨w, hw, hwd⟩ := hh
  exact I.bound_one d' p' p hb (Or.inr ⟨o, hd', hg⟩) (Or.inl ⟨w, List.mem_of_head? hw, hwd⟩)

/-- A word with its grant waits for room in the next slot. -/
theorem block_nextWord {c : Config} {σ : St} (I : Inv c σ) {o r p d d' : Nat}
    (hr : c.route p d = some o) (hn : c.next o = some r) (hx : headIs σ r d') :
    (Hold.head r d', Hold.head p d) ∈ required c .nextWord := by
  simp only [required, List.mem_flatMap]
  refine ⟨(p, d, o), Config.route_mem hr, ?_⟩
  simp only [hn, List.mem_map]
  exact ⟨d', mem_destsOf (head_routed I hx), rfl⟩

/-- A held grant waits for room in the next slot. -/
theorem block_nextGrant {c : Config} {σ : St} (I : Inv c σ) {o r p d d' : Nat}
    (hd : c.route p d = some o) (hn : c.next o = some r) (hx : headIs σ r d') :
    (Hold.head r d', Hold.grant o p) ∈ required c .nextGrant := by
  simp only [required, List.mem_flatMap]
  refine ⟨(p, d, o), Config.route_mem hd, ?_⟩
  simp only [hn, List.mem_map]
  exact ⟨d', mem_destsOf (head_routed I hx), rfl⟩

/-- A word with its grant waits for a target context. -/
theorem block_poolWord {c : Config} {σ : St} (I : Inv c σ) {o r p d d' : Nat}
    (hr : c.route p d = some o) (hn : c.next o = none) (hp : c.pool o d = some r)
    (hx : headIs σ r d') :
    (Hold.head r d', Hold.head p d) ∈ required c .poolWord := by
  simp only [required, List.mem_flatMap]
  refine ⟨(p, d, o), Config.route_mem hr, ?_⟩
  simp only [hn, hp, List.mem_map]
  exact ⟨d', mem_destsOf (head_routed I hx), rfl⟩

/-- A held grant waits for a target context. -/
theorem block_poolGrant {c : Config} {σ : St} (I : Inv c σ) {o r p d d' : Nat}
    (hd : c.route p d = some o) (hn : c.next o = none) (hp : c.pool o d = some r)
    (hx : headIs σ r d') :
    (Hold.head r d', Hold.grant o p) ∈ required c .poolGrant := by
  simp only [required, List.mem_flatMap]
  refine ⟨(p, d, o), Config.route_mem hd, ?_⟩
  simp only [hn, hp, List.mem_map]
  exact ⟨d', mem_destsOf (head_routed I hx), rfl⟩

/-- A held grant waits for its holder's next word, behind a word going
    elsewhere.  This is the only hold-and-wait across words. -/
theorem block_holdAcross {c : Config} (wf : WF c) {σ : St} (I : Inv c σ) {o p r d' : Nat}
    (hg : σ.grant o = some p) (ha : Awaits c σ r d' o p) :
    (Hold.head r d', Hold.grant o p) ∈ required c .holdAcross := by
  obtain ⟨hup, hx, hno, hwho⟩ := ha
  -- A head at the holder means it goes elsewhere; an empty holder has no head to read.
  have hwhy : ∃ d, c.route p d = some o ∧ c.mayHold p d = true := by
    rcases I.held_why o p hg with ⟨a, ha, hra⟩ | hright
    · exact absurd ⟨a, ha, hra⟩ hno
    · exact hright
  have hcross : c.cross p = true := by
    rcases hwho with rfl | h
    · obtain ⟨w, hw, rfl⟩ := hx
      have hne : c.route r w.dest ≠ some o := fun e => hno ⟨w, hw, e⟩
      exact (hold_across_needs_cross I hg ⟨w, hw, rfl⟩ hne).1
    · exact h
  obtain ⟨d, hd, hhold⟩ := hwhy
  obtain ⟨o', ho'⟩ := Option.isSome_iff_exists.1 (head_routed I hx)
  simp only [required, List.mem_flatMap]
  refine ⟨(p, d, o), Config.route_mem hd, ?_⟩
  simp only [hcross, hhold, and_self, if_true, List.mem_flatMap, List.mem_map, List.mem_filter]
  refine ⟨r, ups_complete wf hcross hd hup, (r, d', o'), ⟨Config.route_mem ho', ?_⟩, rfl⟩
  by_cases hrp : r = p
  · subst hrp
    obtain ⟨w, hw, rfl⟩ := hx
    have hne : o' ≠ o := fun e => hno ⟨w, hw, e ▸ ho'⟩
    simp [hne]
  · simp [hrp]

/-! ### The model as a `GenericNoC` -/

section Noc

variable (c : Config) (t : EdgeTable) (node : Hold → Nat)
  (env : (Nat → St) → Prop)

theorem own_head_congr {σ σ' : St} {p : Nat} (h : (σ'.q p).head? = (σ.q p).head?) (d : Nat) :
    own σ' (.head p d) = own σ (.head p d) := by
  simp only [own, h]

theorem own_grant_congr {σ σ' : St} {o : Nat} (h : σ'.grant o = σ.grant o) (p : Nat) :
    own σ' (.grant o p) = own σ (.grant o p) := by
  simp only [own, h]

/-- A held point that makes no progress keeps its
    owner. -/
theorem own_stable_step {σ σ' : St} {h : Hold} (hs : Step c σ σ')
    (hheld : (own σ h).isSome = true) (hnp : ¬ prog σ σ' h) : own σ' h = own σ h := by
  cases hs with
  | stutter => cases h <;> rfl
  | cut p w _ hh _ _ =>
    have hhd : ∀ x a, (σ.q x).head? = some a →
        (upd σ.q p ({ w with tail := true } :: (σ.q p).tail) x).head? = some (if x = p then { w with tail := true } else a) := by
      intro x a ha
      by_cases hx : x = p
      · subst hx; simp
      · simp [upd_ne, hx, ha]
    cases h with
    | grant o' p' => rfl
    | head x d =>
      obtain ⟨a, ha, had⟩ := own_head_isSome.1 hheld
      have h1 := hhd x a ha
      by_cases hx : x = p
      · subst hx
        rw [hh] at ha
        have : w = a := by simpa using ha
        subst this
        simp only [if_true] at h1
        simp only [own, hh]
        show (match (upd σ.q x ({ w with tail := true } :: (σ.q x).tail) x).head? with
          | some w_1 => if w_1.dest = d then some w_1.tok else none
          | none => none) = _
        rw [h1]
      · simp only [if_neg hx] at h1
        simp only [own, ha]
        show (match (upd σ.q p ({ w with tail := true } :: (σ.q p).tail) x).head? with
          | some w_1 => if w_1.dest = d then some w_1.tok else none
          | none => none) = _
        rw [h1]
    | ctx k x =>
      obtain ⟨a, ha, hak⟩ := own_ctx_isSome.1 hheld
      have hi' : hasTok { σ with
          q := upd σ.q p ({ w with tail := true } :: (σ.q p).tail)
          ev := none } x k := by
        by_cases hx : x = p
        · subst hx
          cases hq : σ.q x with
          | nil => rw [hq] at ha; simp at ha
          | cons b bs =>
            rw [hq] at ha hh
            have hb : b = w := by simpa using hh
            subst hb
            rcases List.mem_cons.1 ha with rfl | hm
            · exact ⟨{ a with tail := true }, by simp [upd], hak⟩
            · exact ⟨a, by simp [upd, hm], hak⟩
        · exact ⟨a, by simp [upd, hx, ha], hak⟩
      rw [own_ctx_some hi', own_ctx_some ⟨a, ha, hak⟩]
    | wait k =>
      obtain ⟨hl, hno⟩ := own_wait_isSome.1 hheld
      have hw' : waiting { σ with
          q := upd σ.q p ({ w with tail := true } :: (σ.q p).tail)
          ev := none } k := by
        refine ⟨hl, ?_⟩
        rintro x ⟨a, ha, hak⟩
        have ha' : a ∈ upd σ.q p ({ w with tail := true } :: (σ.q p).tail) x := ha
        by_cases hx : x = p
        · subst hx
          rw [upd_same] at ha'
          rcases List.mem_cons.1 ha' with rfl | hm
          · exact hno x ⟨w, List.mem_of_head? hh, hak⟩
          · exact hno x ⟨a, List.mem_of_mem_tail hm, hak⟩
        · rw [upd_ne _ _ hx] at ha'
          exact hno x ⟨a, ha', hak⟩
      rw [own_wait_some hw', own_wait_some ⟨hl, hno⟩]
  | arb p o w hh hr hf =>
    cases h with
    | head x d => rfl
    | ctx k x => rfl
    | wait k => rfl
    | grant o' p' =>
      have hg := own_grant_isSome.1 hheld
      have ho : o' ≠ o := by
        intro e; subst e; rw [hf] at hg; simp at hg
      apply own_grant_congr
      show upd σ.grant o (some p) o' = σ.grant o'
      exact upd_ne _ _ ho
  | fire p o w hh hr hg hroom hloop hcut =>
    cases h with
    | head x d =>
      have hi := own_head_isSome.1 hheld
      by_cases hx : x = p
      · subst hx
        exact absurd ⟨hi, o, fire_ev⟩ hnp
      · obtain ⟨a, ha, _⟩ := hi
        apply own_head_congr
        rw [ha, fire_q hloop]
        split
        · exact head?_push ha
        · simpa [hx] using ha
    | grant o' p' =>
      have hg' := own_grant_isSome.1 hheld
      by_cases ho : o' = o
      · subst ho
        rw [hg] at hg'
        have : p = p' := by simpa using hg'
        subst this
        exact absurd ⟨hg, fire_ev⟩ hnp
      · apply own_grant_congr
        rw [fire_grant, if_neg (fun h => ho h.2)]
    | ctx k x =>
      have hi := own_ctx_isSome.1 hheld
      have hi' : hasTok (place c (pop σ p o w) o w) x k := by
        obtain ⟨a, ha, hak⟩ := hi
        rw [hasTok, fire_q hloop]
        split
        · exact ⟨a, List.mem_append.2 (Or.inl ha), hak⟩
        · split
          · rename_i hx
            subst hx
            have hne : w.tok ≠ k := fun e =>
              hnp ⟨⟨w, hh, e⟩, o, fire_ev⟩
            cases hq : σ.q x with
            | nil => rw [hq] at ha; simp at ha
            | cons b bs =>
              rw [hq] at ha hh
              have hb : b = w := by simpa using hh
              subst hb
              rcases List.mem_cons.1 ha with rfl | hm
              · exact absurd hak hne
              · exact ⟨a, hm, hak⟩
          · exact ⟨a, ha, hak⟩
      rw [own_ctx_some hi', own_ctx_some hi]
    | wait k =>
      obtain ⟨hl, hno⟩ := own_wait_isSome.1 hheld
      have hne : w.tok ≠ k := fun e => hno p ⟨w, List.mem_of_head? hh, e⟩
      have hl' : (place c (pop σ p o w) o w).live k = true := by
        rw [place_live_other hne]
        exact hl
      have hno' : ∀ x, ¬ hasTok (place c (pop σ p o w) o w) x k := by
        intro x hx
        obtain ⟨a, ha, hak⟩ := hx
        rcases mem_fire_q hloop ha with hm | ⟨rfl, _⟩
        · exact hno x ⟨a, hm, hak⟩
        · exact hne hak
      rw [own_wait_some (show waiting _ k from ⟨hl', hno'⟩), own_wait_some ⟨hl, hno⟩]
  | inject p w b _ _ _ _ _ _ _ _ _ _ _ _ =>
    cases h with
    | ctx k x =>
      have hi := own_ctx_isSome.1 hheld
      obtain ⟨a, ha, hak⟩ := hi
      have hi' : hasTok (injected σ p w b) x k := by
        refine ⟨a, ?_, hak⟩
        show a ∈ upd σ.q p (σ.q p ++ [w]) x
        by_cases hx : x = p
        · subst hx; rw [upd_same]; exact List.mem_append.2 (Or.inl ha)
        · rw [upd_ne _ _ hx]; exact ha
      rw [own_ctx_some hi', own_ctx_some ⟨a, ha, hak⟩]
    | wait k =>
      obtain ⟨hl, hno⟩ := own_wait_isSome.1 hheld
      have hne : w.tok ≠ k := by
        intro e
        apply hnp
        refine ⟨⟨hl, hno⟩, p, w, ?_, e⟩
        show w ∈ upd σ.q p (σ.q p ++ [w]) p
        rw [upd_same]; simp
      have hw' : waiting (injected σ p w b) k := by
        refine ⟨?_, ?_⟩
        · show upd σ.live w.tok true k = true
          rw [upd_ne _ _ (fun e => hne e.symm)]
          exact hl
        · rintro x ⟨a, ha, hak⟩
          have ha' : a ∈ upd σ.q p (σ.q p ++ [w]) x := ha
          by_cases hx : x = p
          · subst hx
            rw [upd_same] at ha'
            rcases List.mem_append.1 ha' with hm | hm
            · exact hno x ⟨a, hm, hak⟩
            · have : a = w := by simpa using hm
              subst this
              exact hne hak
          · rw [upd_ne _ _ hx] at ha'
            exact hno x ⟨a, ha', hak⟩
      rw [own_wait_some hw', own_wait_some ⟨hl, hno⟩]
    | head x d =>
      obtain ⟨a, ha, _⟩ := own_head_isSome.1 hheld
      apply own_head_congr
      rw [ha]
      show (upd σ.q p (σ.q p ++ [w]) x).head? = some a
      by_cases hx : x = p
      · subst hx; rw [upd_same]; exact head?_push ha
      · rw [upd_ne _ _ hx]; exact ha
    | grant o' p' => rfl

/-- Progress implies the point was held and every need was permitted. -/
theorem prog_step {σ σ' : St} {h : Hold} (hs : Step c σ σ') (hp : prog σ σ' h) :
    (own σ h).isSome = true ∧ ∀ x, need c σ x h → permit c σ x h := by
  cases h with
  | head p d =>
    obtain ⟨hi, o', he⟩ := hp
    refine ⟨own_head_isSome.2 hi, ?_⟩
    cases hs with
    | stutter => simp at he
    | arb => simp at he
    | cut => simp at he
    | inject => simp [injected] at he
    | fire p1 o1 w hh hr hg hroom hloop hcut =>
      rw [fire_ev] at he
      simp only [Option.some.injEq, Prod.mk.injEq] at he
      obtain ⟨rfl, rfl⟩ := he
      obtain ⟨a, ha, rfl⟩ := hi
      rw [hh] at ha
      have : w = a := by simpa using ha
      subst this
      intro x hn
      cases x with
      | grant o2 p2 =>
        obtain ⟨_, hr2, hne⟩ := hn
        rw [hr] at hr2
        have : o1 = o2 := by simpa using hr2
        subst this
        show σ.grant o1 ≠ some p2
        rw [hg]
        intro e
        exact hne (by simpa using e.symm)
      | ctx _ _ => exact hn.elim
      | wait _ => exact hn.elim
      | head r d'' =>
        intro o3 hr3 _ _
        rw [hr] at hr3
        have : o1 = o3 := by simpa using hr3
        subst this
        intro a ha
        rw [hh] at ha
        have : w = a := by simpa using ha
        subst this
        exact hroom
  | grant o p =>
    obtain ⟨hg0, he⟩ := hp
    refine ⟨own_grant_isSome.2 hg0, ?_⟩
    cases hs with
    | stutter => simp at he
    | arb => simp at he
    | cut => simp at he
    | inject => simp [injected] at he
    | fire p1 o1 w hh hr hg hroom hloop hcut =>
      rw [fire_ev] at he
      simp only [Option.some.injEq, Prod.mk.injEq] at he
      obtain ⟨rfl, rfl⟩ := he
      intro x hn
      cases x with
      | grant o2 p2 => exact hn.elim
      | head r d' =>
        refine ⟨fun _ a ha => ?_, ?_⟩
        · rw [hh] at ha
          have : w = a := by simpa using ha
          subst this
          exact hroom
        rintro ⟨_, _, hno, _⟩
        exact hno ⟨w, hh, hr⟩
      | ctx _ _ => exact hn.elim
      | wait _ => exact hn.elim
  | ctx k p =>
    obtain ⟨⟨w0, hw0, htk⟩, o', he⟩ := hp
    refine ⟨own_ctx_isSome.2 ⟨w0, List.mem_of_head? hw0, htk⟩, ?_⟩
    cases hs with
    | stutter => simp at he
    | arb => simp at he
    | cut => simp at he
    | inject => simp [injected] at he
    | fire p1 o1 w hh hr hg hroom hloop hcut =>
      rw [fire_ev] at he
      simp only [Option.some.injEq, Prod.mk.injEq] at he
      obtain ⟨rfl, rfl⟩ := he
      rw [hh] at hw0
      have : w = w0 := by simpa using hw0
      subst this
      intro x hn
      cases x with
      | grant _ _ => exact hn.elim
      | ctx _ _ => exact hn.elim
      | wait _ => exact hn.elim
      | head r d' =>
        intro _ a ha hd
        rw [hh] at ha
        have : w = a := by simpa using ha
        subst this
        refine ⟨htk, o1, hd ▸ hr, hg, fun a ha => ?_⟩
        rw [hh] at ha
        have : w = a := by simpa using ha
        subst this
        exact hroom
  | wait k =>
    obtain ⟨hw, p', hx⟩ := hp
    refine ⟨own_wait_isSome.2 hw, ?_⟩
    obtain ⟨hl, hno⟩ := hw
    cases hs with
    | stutter => exact absurd hx (hno p')
    | arb => exact absurd hx (hno p')
    | cut p1 w _ hh _ _ =>
      exfalso
      obtain ⟨a, ha, hak⟩ := hx
      have ha' : a ∈ upd σ.q p1 ({ w with tail := true } :: (σ.q p1).tail) p' := ha
      by_cases hx' : p' = p1
      · subst hx'
        rw [upd_same] at ha'
        rcases List.mem_cons.1 ha' with rfl | hm
        · exact hno p' ⟨w, List.mem_of_head? hh, hak⟩
        · exact hno p' ⟨a, List.mem_of_mem_tail hm, hak⟩
      · rw [upd_ne _ _ hx'] at ha'
        exact hno p' ⟨a, ha', hak⟩
    | fire p1 o1 w hh hr hg hroom hloop hcut =>
      exfalso
      obtain ⟨a, ha, hak⟩ := hx
      rcases mem_fire_q hloop ha with hm | ⟨rfl, _⟩
      · exact hno p' ⟨a, hm, hak⟩
      · exact hno p1 ⟨a, List.mem_of_head? hh, hak⟩
    | inject p1 w _ _ hcap _ _ _ _ hwhere _ _ _ _ _ =>
      obtain ⟨a, ha, hak⟩ := hx
      have ha' : a ∈ upd σ.q p1 (σ.q p1 ++ [w]) p' := ha
      have hwk : w.tok = k := by
        by_cases hx : p' = p1
        · subst hx
          rw [upd_same] at ha'
          rcases List.mem_append.1 ha' with hm | hm
          · exact absurd ⟨a, hm, hak⟩ (hno p')
          · have : a = w := by simpa using hm
            subst this
            exact hak
        · rw [upd_ne _ _ hx] at ha'
          exact absurd ⟨a, ha', hak⟩ (hno p')
      intro x hn
      cases x with
      | grant _ _ => exact hn.elim
      | ctx _ _ => exact hn.elim
      | wait _ => exact hn.elim
      | head r d' =>
        have hor := hwhere (hwk ▸ hl)
        rw [hwk] at hor
        rcases hor with ⟨h, _⟩ | h
        · exact ⟨p1, Or.inr h, hcap⟩
        · exact ⟨p1, Or.inl h, hcap⟩

/-- A rank above every node of the table. -/
def topRank (t : EdgeTable) : Nat :=
  (List.range t.size).foldr (fun u m => max (t.rankOf u) m) 0 + 1

theorem rank_lt_top {t : EdgeTable} {u : Nat} (h : u < t.size) : t.rankOf u < topRank t := by
  have key : ∀ l : List Nat, u ∈ l → t.rankOf u ≤ l.foldr (fun u m => max (t.rankOf u) m) 0 := by
    intro l
    induction l with
    | nil => intro hm; simp at hm
    | cons a as ih =>
      intro hm
      simp only [List.foldr_cons]
      rcases List.mem_cons.1 hm with rfl | hm
      · omega
      · have := ih hm
        omega
  have := key (List.range t.size) (List.mem_range.2 h)
  simp only [topRank]
  omega

/-- The node of a hold point.  Transaction points share one node above the
    whole table: they block nothing, so they can never close a cycle. -/
def cls (t : EdgeTable) (node : Hold → Nat) : Hold → Nat
  | .ctx _ _ => t.size
  | .wait _ => t.size
  | h => node h

/-- The rank of a node, with the top node above the table. -/
def rankWith (t : EdgeTable) (u : Nat) : Nat := if u = t.size then topRank t else t.rankOf u

/-- The wormhole model with a table graph and its rank certificate. -/
noncomputable def noc (hrank : t.checkRank = true) : GenericNoC where
  State := St
  Hold := Hold
  Node := Nat
  Component := Nat
  Owner := Nat
  init := fun σ => σ = St.empty
  step := Step c
  own := own
  prog := prog
  need := need c
  permit := permit c
  ready := fun _ _ => True
  en := fun σ y => True ∧ ∀ x, need c σ x y → permit c σ x y
  en_iff := fun _ _ => Iff.rfl
  prog_held_en := by
    intro σ σ' h hs hp
    obtain ⟨h1, h2⟩ := prog_step c hs hp
    exact ⟨h1, trivial, h2⟩
  own_stable := fun hs hheld hnp => own_stable_step c hs hheld hnp
  componentOf := fun _ => 0
  classOf := cls t node
  edge := fun u v => t.edgeRel u v ∨ (u < t.size ∧ v = t.size)
  graphRank := rankWith t
  localRank := fun _ _ => 0
  edge_rank := by
    intro u v he
    rcases he with he | ⟨hu, rfl⟩
    · obtain ⟨hu, hv, hr⟩ := EdgeTable.checkRank_spec.1 hrank u v he
      simp only [rankWith, Nat.ne_of_lt hu, Nat.ne_of_lt hv, if_false]
      exact hr
    · simp only [rankWith, Nat.ne_of_lt hu, if_false, if_true]
      exact rank_lt_top hu
  env := env

/-- A table path is a model path. -/
theorem reaches_of_path (hrank : t.checkRank = true) {u v : Nat} (p : t.Path u v) :
    (noc c t node env hrank).Reaches u v := by
  induction p with
  | single he => exact GenericNoC.Reaches.single (Or.inl he)
  | trans _ he ih => exact GenericNoC.Reaches.trans ih (Or.inl he)

end Noc

/-! ### Coverage check -/

/-- Path witnesses `(from, via, to)`, sorted by `(from, to)`.  An unsorted list can
    only fail to find a witness. -/
abbrev Hints := Array (Nat × List Nat × Nat)

/-- Binary search for the witness of a pair. -/
def findHint (hints : Hints) (u v : Nat) : Nat → Nat → Nat → List Nat
  | 0, _, _ => []
  | fuel + 1, lo, hi =>
    if lo < hi then
      let mid := (lo + hi) / 2
      match hints[mid]? with
      | none => []
      | some h =>
        if h.1 == u && h.2.2 == v then h.2.1
        else if h.1 < u || (h.1 == u && h.2.2 < v) then findHint hints u v fuel (mid + 1) hi
        else findHint hints u v fuel lo mid
    else []

/-- Graph connection between two nodes, directly or through a path witness.  The
    witness is only a suggestion: every step of it is checked. -/
def connected {t : EdgeTable} (ix : EdgeTable.Index t)
    (hints : Hints) (u v : Nat) : Bool :=
  ix.mem (u, v) || EdgeTable.pathOk ix u (findHint hints u v 64 0 hints.size) v

theorem connected_sound {t : EdgeTable} {ix : EdgeTable.Index t}
    {hints : Hints} {u v : Nat}
    (h : connected ix hints u v = true) : t.Path u v := by
  simp only [connected, Bool.or_eq_true] at h
  rcases h with h | h
  · exact EdgeTable.Path.single (EdgeTable.Index.mem_sound h)
  · exact EdgeTable.pathOk_sound h

/-- The check of one rule: every required pair is connected. -/
def checkRule (c : Config) {t : EdgeTable} (ix : EdgeTable.Index t)
    (hints : Hints) (node : Hold → Nat) (r : Rule) : Bool :=
  (required c r).all fun e => connected ix hints (node e.1) (node e.2)

/-- All rules. -/
def allRules : List Rule :=
  [.arbitration, .nextWord, .nextGrant, .poolWord, .poolGrant, .holdAcross, .context]

theorem mem_allRules (r : Rule) : r ∈ allRules := by
  cases r <;> simp [allRules]

/-- Decidable coverage of every rule. -/
def checkCoverage (c : Config) {t : EdgeTable} (ix : EdgeTable.Index t)
    (hints : Hints) (node : Hold → Nat) : Bool :=
  allRules.all (checkRule c ix hints node)

/-- Decidable instance conditions. -/
def wfCheck (c : Config) : Bool :=
  c.endpoints.all (fun p => c.cross p || c.contig p ||
    c.routes.all (fun r => r.1 != p || c.ahead p r.2.1)) &&
  c.routes.all (fun r => !c.ahead r.1 r.2.1 || !c.contig r.1) &&
  -- A slot whose words can hold says how: no endpoint claims that every word ends.
  c.routes.all (fun r => !c.endpoints.contains r.1 || c.mayHold r.1 r.2.1 || c.ahead r.1 r.2.1) &&
  c.nexts.all (fun e =>
    c.routes.all (fun r => r.2.2 != e.1 || c.cross e.2 || !c.cross r.1) &&
    !c.endpoints.contains e.2 &&
    c.nexts.all (fun f => f.2 != e.2 || f.1 == e.1) &&
    c.routes.all (fun r => r.2.2 != e.1 ||
      ((c.route e.2 r.2.1).isSome &&
        (!(c.mayHold r.1 r.2.1 || c.ahead r.1 r.2.1) || c.mayHold e.2 r.2.1)))) &&
  c.routes.all (fun r => !c.cross r.1 ||
    ((c.ups r.1).contains r.1 &&
      (c.ups r.1).all fun x => c.routes.all fun f => c.next f.2.2 != some x || (c.ups r.1).contains f.1)) &&
  -- Answers exist and cover the responses, or `Fair` could only hold with no traffic.
  c.pools.all (fun e => c.endpoints.contains e.2.2 && c.routes.all fun r =>
    !(c.endpoints.contains r.1 && !isResponse c r.1 && r.2.1 == e.2.1) ||
      c.routes.any fun f => f.1 == e.2.2 && c.answers r.1 f.2.1) &&
  c.routes.all (fun r => !isResponse c r.1 ||
    c.endpoints.any fun s => c.answers s r.2.1) &&
  c.routes.all (fun r => !c.bound r.2.1 ||
    ((c.next r.2.2).isNone && c.routes.all (fun f => f.2.2 != r.2.2 || f.2.1 == r.2.1)))

theorem wfCheck_sound {c : Config} (h : wfCheck c = true) : WF c := by
  simp only [wfCheck, Bool.and_eq_true, List.all_eq_true, Bool.or_eq_true, bne_iff_ne,
    Bool.not_eq_true', beq_iff_eq, ne_eq] at h
  obtain ⟨⟨⟨⟨⟨⟨⟨h1, hah⟩, _⟩, h2⟩, hups⟩, _⟩, _⟩, h3⟩ := h
  have hrow : ∀ {o r}, c.next o = some r → _ := fun {o r} hn => h2 (o, r) (Config.lookup_mem hn)
  have hbound : ∀ {p d o}, c.bound d = true → c.route p d = some o →
      (c.next o).isNone = true ∧
        ∀ f, f ∈ c.routes → f.2.2 ≠ o ∨ f.2.1 = d := by
    intro p d o hb hr
    rcases h3 (p, d, o) (Config.route_mem hr) with h | h
    · simp only at h
      rw [hb] at h
      simp at h
    · exact ⟨h.1, h.2⟩
  have hup : ∀ {p d o}, c.cross p = true → c.route p d = some o →
      p ∈ c.ups p ∧ ∀ x, x ∈ c.ups p → ∀ f, f ∈ c.routes → c.next f.2.2 ≠ some x ∨ f.1 ∈ c.ups p := by
    intro p d o hc hr
    rcases hups (p, d, o) (Config.route_mem hr) with h | h
    · simp only at h; rw [hc] at h; simp at h
    · simpa [List.contains_iff_mem] using h
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_, ?_⟩
  · intro p hp hc
    rcases h1 p hp with (h | h) | h
    · rw [hc] at h; simp at h
    · exact Or.inl h
    · refine Or.inr fun d o hr => ?_
      rcases h (p, d, o) (Config.route_mem hr) with h' | h'
      · exact absurd rfl h'
      · exact h'
  · intro p d o hr ha
    rcases hah (p, d, o) (Config.route_mem hr) with h | h
    · simp only at h; rw [ha] at h; simp at h
    · exact h
  · intro o r p d hn hc hr
    have := (hrow hn).1.1.1 (p, d, o) (Config.route_mem hr)
    simp only at this
    rcases this with (h | h) | h
    · exact absurd trivial h
    · rw [hc] at h; simp at h
    · exact h
  · intro o r hn hmem
    have := (hrow hn).1.1.2
    simp only at this
    rw [List.contains_iff_mem.2 hmem] at this
    simp at this
  · intro o o' r hn hn'
    have := (hrow hn).1.2 (o', r) (Config.lookup_mem hn')
    simp only at this
    rcases this with h | h
    · exact absurd trivial h
    · exact h.symm
  · intro p d o r hr hn
    have := (hrow hn).2 (p, d, o) (Config.route_mem hr)
    simp only at this
    rcases this with h | h
    · exact absurd trivial h
    · exact h.1
  · intro p d o r hr hn hhold
    have := (hrow hn).2 (p, d, o) (Config.route_mem hr)
    simp only at this
    rcases this with h | h
    · exact absurd trivial h
    · rcases h.2 with h0 | h'
      · rcases hhold with hm | hm
        · simp [hm] at h0
        · simp [hm] at h0
      · exact h'
  · intro p d o hb hr
    have := (hbound hb hr).1
    simpa using this
  · intro p d o p' d' hb hr hr'
    rcases (hbound hb hr).2 (p', d', o) (Config.route_mem hr') with h | h
    · exact absurd rfl h
    · exact h
  · intro p d o hc hr
    exact (hup hc hr).1
  · intro p d o r d' o' x hc hr hx hr' hn
    rcases (hup hc hr).2 x hx (r, d', o') (Config.route_mem hr') with h | h
    · exact absurd hn h
    · exact h

/-- What the rule lemmas need from a graph: each required pair is connected. -/
def Covered (c : Config) (t : EdgeTable) (node : Hold → Nat) : Prop :=
  ∀ r x y, (x, y) ∈ required c r → t.Path (node x) (node y)

/-- Heads of routed destinations are nodes of the table, so the top node is above
    them.  The `context` rule rests on this. -/
def headsInRange (c : Config) (t : EdgeTable) (node : Hold → Nat) : Bool :=
  c.routes.all fun e => decide (node (.head e.1 e.2.1) < t.size)

theorem head_in_range {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (h : headsInRange c t node = true) {p d : Nat} (hr : (c.route p d).isSome = true) :
    node (.head p d) < t.size := by
  obtain ⟨o, ho⟩ := Option.isSome_iff_exists.1 hr
  have := List.all_eq_true.1 h _ (Config.route_mem ho)
  simpa using this

theorem checkCoverage_covered {c : Config} {t : EdgeTable} {ix : EdgeTable.Index t}
    {hints : Hints} {node : Hold → Nat}
    (h : checkCoverage c ix hints node = true) : Covered c t node := by
  intro r x y hmem
  have hall := List.all_eq_true.1 h r (mem_allRules r)
  exact connected_sound (List.all_eq_true.1 hall (x, y) hmem)

section Main

variable {c : Config} {t : EdgeTable} {node : Hold → Nat} {env : (Nat → St) → Prop}

/-- The graph path of a required pair.  Required pairs are heads and grants, whose
    class is their node. -/
theorem block_reaches (hrank : t.checkRank = true)
    (hcov : Covered c t node) {r : Rule} {x y : Hold}
    (hmem : (x, y) ∈ required c r) :
    (noc c t node env hrank).Reaches (cls t node x) (cls t node y) := by
  have hkinds : (∀ k p, x ≠ .ctx k p) ∧ (∀ k, x ≠ .wait k) ∧
      (∀ k p, y ≠ .ctx k p) ∧ (∀ k, y ≠ .wait k) := required_kinds hmem
  have hx : cls t node x = node x := by
    cases x with
    | ctx k p => exact absurd rfl (hkinds.1 k p)
    | wait k => exact absurd rfl (hkinds.2.1 k)
    | head _ _ => rfl
    | grant _ _ => rfl
  have hy : cls t node y = node y := by
    cases y with
    | ctx k p => exact absurd rfl (hkinds.2.2.1 k p)
    | wait k => exact absurd rfl (hkinds.2.2.2 k)
    | head _ _ => rfl
    | grant _ _ => rfl
  rw [hx, hy]
  exact reaches_of_path c t node env hrank (hcov r x y hmem)

/-- W2 by rule.  `covered` reads each rule's check; `exhaustive` is the case
    split over block shapes, each case going through its own rule lemma. -/
def ruleCover (wf : WF c) (hrank : t.checkRank = true)
    (hcov : Covered c t node) (hheads : headsInRange c t node = true) :
    GenericNoC.RuleCover (noc c t node env hrank) where
  Rule := Rule
  fires := fun r _ x y =>
    match r with
    | .context =>
      (∃ p d, x = .head p d ∧ (c.route p d).isSome = true) ∧
        ((∃ k p, y = .ctx k p) ∨ ∃ k, y = .wait k)
    | r => (x, y) ∈ required c r
  exhaustive := by
    intro s hlegal τ x y hb
    obtain ⟨hx, hy, hn, hnp⟩ := hb
    have I : Inv c (s τ) := inv_run wf hlegal.1 hlegal.2 τ
    cases x with
    | grant o p' =>
      cases y with
      | grant _ _ => exact hn.elim
      | ctx _ _ => exact hn.elim
      | wait _ => exact hn.elim
      | head p d =>
        obtain ⟨hhead, hr, hne⟩ := hn
        have hg : (s τ).grant o = some p' := Classical.byContradiction hnp
        cases hbd : c.bound d with
        | true => exact absurd (bound_no_contention wf I hbd hr hhead hg) hne
        | false => exact ⟨.arbitration, block_arbitration I hr hne hg hbd⟩
    | ctx _ _ => cases y <;> exact hn.elim
    | wait _ => cases y <;> exact hn.elim
    | head r d' =>
      have hxi : headIs (s τ) r d' := own_head_isSome.1 hx
      cases y with
      | ctx k p =>
        exact ⟨.context, ⟨r, d', rfl, head_routed I hxi⟩, Or.inl ⟨k, p, rfl⟩⟩
      | wait k =>
        exact ⟨.context, ⟨r, d', rfl, head_routed I hxi⟩, Or.inr ⟨k, rfl⟩⟩
      | head p d =>
        have hex : ∃ o, c.route p d = some o ∧ (s τ).grant o = some p ∧
            blockerOf c o d = some r := by
          obtain ⟨_, o, h1, h2, h3⟩ := hn
          exact ⟨o, h1, h2, h3⟩
        obtain ⟨o, hr, _, hb⟩ := hex
        cases hnx : c.next o with
        | some r' =>
          have : r' = r := by
            simp only [blockerOf, hnx] at hb
            simpa using hb
          subst this
          exact ⟨.nextWord, block_nextWord I hr hnx hxi⟩
        | none =>
          have hpo : c.pool o d = some r := by
            simpa only [blockerOf, hnx] using hb
          exact ⟨.poolWord, block_poolWord I hr hnx hpo hxi⟩
      | grant o p =>
        obtain ⟨hg, hwhy⟩ := hn
        by_cases hacross : Awaits c (s τ) r d' o p
        · exact ⟨.holdAcross, block_holdAcross wf I hg hacross⟩
        · obtain ⟨d, _, hr, hb⟩ : ∃ d, headIs (s τ) p d ∧ c.route p d = some o ∧
              blockerOf c o d = some r := by
            rcases hwhy with h | h
            · exact h
            · exact absurd h hacross
          cases hnx : c.next o with
          | some r' =>
            have : r' = r := by
              simp only [blockerOf, hnx] at hb
              simpa using hb
            subst this
            exact ⟨.nextGrant, block_nextGrant I hr hnx hxi⟩
          | none =>
            have hpo : c.pool o d = some r := by
              simpa only [blockerOf, hnx] using hb
            exact ⟨.poolGrant, block_poolGrant I hr hnx hpo hxi⟩
  covered := by
    intro r _ x y hmem
    cases r with
    | context =>
      obtain ⟨⟨p, d, rfl, hr⟩, hy⟩ := hmem
      have hrange : node (.head p d) < t.size := head_in_range hheads hr
      rcases hy with ⟨k, p', rfl⟩ | ⟨k, rfl⟩
      · exact GenericNoC.Reaches.single (Or.inr ⟨hrange, rfl⟩)
      · exact GenericNoC.Reaches.single (Or.inr ⟨hrange, rfl⟩)
    | arbitration => exact block_reaches hrank hcov hmem
    | nextWord => exact block_reaches hrank hcov hmem
    | nextGrant => exact block_reaches hrank hcov hmem
    | poolWord => exact block_reaches hrank hcov hmem
    | poolGrant => exact block_reaches hrank hcov hmem
    | holdAcross => exact block_reaches hrank hcov hmem

/-- The structural obligation of a checked instance.  This is a theorem about
    the model and the table: it says nothing about RTL. -/
theorem structural (wf : wfCheck c = true) (hrank : t.checkRank = true)
    (hcov : Covered c t node) (hheads : headsInRange c t node = true) :
    GenericNoC.StructuralInstanceLemmas (noc c t node env hrank) :=
  (ruleCover (wfCheck_sound wf) hrank hcov hheads).toStructural

end Main

end Wormhole
