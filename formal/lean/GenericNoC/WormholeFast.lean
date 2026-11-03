/-
  GenericNoC.WormholeFast: rule coverage in time linear in the required pairs.

  `required` scans the whole route list for each route, which is quadratic in
  the routes before a single pair is checked.  Here the candidate lists are
  grouped once.  The grouping is checked and not trusted: `Groups.ok` confirms
  that each route is in its groups, so a wrong grouping fails the check.  The
  pairs checked are a superset of `required`, so passing implies `Covered`.
-/
import GenericNoC.WormholeRules

namespace Wormhole

/-- Route lists grouped by output and by slot. -/
structure Groups where
  feed : Array (List Nat)
  dests : Array (List Nat)
  others : Array (List (Nat × Nat))

/-- Group the routes. -/
def mkGroups (c : Config) : Groups :=
  let top := c.routes.foldl (fun m r => max m (max r.1 r.2.2)) 0 + 1
  let base : Groups :=
    ⟨Array.replicate top [], Array.replicate top [], Array.replicate top []⟩
  c.routes.foldl (fun g r =>
    { feed := g.feed.modify r.2.2 (fun l => if l.contains r.1 then l else r.1 :: l)
      dests := g.dests.modify r.1 (fun l => r.2.1 :: l)
      others := g.others.modify r.1 (fun l => (r.2.1, r.2.2) :: l) }) base

/-- Every route is in its groups. -/
def Groups.ok (g : Groups) (c : Config) : Bool :=
  c.routes.all fun r =>
    (g.feed.getD r.2.2 []).contains r.1 &&
    (g.dests.getD r.1 []).contains r.2.1 &&
    (g.others.getD r.1 []).contains (r.2.1, r.2.2)

/-! ### Contention through helper nodes

Direct edges from every held grant to every other input's head grow with the
square of the inputs.  A graph can state the same waits with helper nodes per
input, in input order: `before` is reached by every earlier holder and `after` by
every later one, and each reaches `other`, which reaches that input's heads.  Each
helper edge is checked once, and the path for each pair then follows by proof, so
the check grows with the edges and not with the pairs. -/

/-- The helper nodes of one input of an output. -/
structure Row where
  input : Nat
  before : Nat
  after : Nat
  other : Nat

section Rows

variable {t : EdgeTable} (ix : EdgeTable.Index t)

/-- Earlier holders reach `before`, and through it each later `other`. -/
def beforeOk : List Row → Bool
  | [] => true
  | [_] => true
  | a :: b :: rest =>
    ix.mem (a.before, b.before) && ix.mem (a.before, b.other) && beforeOk (b :: rest)

/-- Later holders reach `after`, and through it each earlier `other`. -/
def afterOk : List Row → Bool
  | [] => true
  | [_] => true
  | a :: b :: rest =>
    ix.mem (b.after, a.after) && ix.mem (b.after, a.other) && afterOk (b :: rest)

variable {ix}

theorem before_path : ∀ {a : Row} {rest : List Row}, beforeOk ix (a :: rest) = true →
    ∀ b, b ∈ rest → t.Path a.before b.other := by
  intro a rest
  induction rest generalizing a with
  | nil => intro _ b hb; simp at hb
  | cons x xs ih =>
    intro h b hb
    simp only [beforeOk, Bool.and_eq_true] at h
    obtain ⟨⟨h1, h2⟩, h3⟩ := h
    rcases List.mem_cons.1 hb with rfl | hm
    · exact .single (EdgeTable.Index.mem_sound h2)
    · exact EdgeTable.Path.cons (EdgeTable.Index.mem_sound h1) (ih h3 b hm)

theorem after_step : ∀ {y : Row} {ys : List Row}, afterOk ix (y :: ys) = true →
    ∀ a, a ∈ ys → t.Path a.after y.after := by
  intro y ys
  induction ys generalizing y with
  | nil => intro _ a ha; simp at ha
  | cons z zs ih =>
    intro h a ha
    simp only [afterOk, Bool.and_eq_true] at h
    obtain ⟨⟨h1, _⟩, h3⟩ := h
    rcases List.mem_cons.1 ha with rfl | hm
    · exact .single (EdgeTable.Index.mem_sound h1)
    · exact (ih h3 a hm).trans (EdgeTable.Index.mem_sound h1)

theorem after_path : ∀ {x : Row} {xs : List Row}, afterOk ix (x :: xs) = true →
    ∀ a, a ∈ xs → t.Path a.after x.other := by
  intro x xs h a ha
  cases xs with
  | nil => simp at ha
  | cons y ys =>
    simp only [afterOk, Bool.and_eq_true] at h
    obtain ⟨⟨_, h2⟩, h3⟩ := h
    rcases List.mem_cons.1 ha with rfl | hm
    · exact .single (EdgeTable.Index.mem_sound h2)
    · exact (after_step h3 a hm).trans (EdgeTable.Index.mem_sound h2)

/-- Any two different rows have an order, and the chains carry it. -/
theorem row_paths : ∀ {rows : List Row}, beforeOk ix rows = true → afterOk ix rows = true →
    ∀ a b, a ∈ rows → b ∈ rows → a ≠ b →
      t.Path a.before b.other ∨ t.Path a.after b.other := by
  intro rows
  induction rows with
  | nil => intro _ _ a _ ha; simp at ha
  | cons x xs ih =>
    intro hb haft a b ha hb' hne
    have htb : beforeOk ix xs = true := by
      cases xs with
      | nil => rfl
      | cons y ys =>
        simp only [beforeOk, Bool.and_eq_true] at hb
        exact hb.2
    have hta : afterOk ix xs = true := by
      cases xs with
      | nil => rfl
      | cons y ys =>
        simp only [afterOk, Bool.and_eq_true] at haft
        exact haft.2
    rcases List.mem_cons.1 ha with rfl | hma
    · rcases List.mem_cons.1 hb' with rfl | hmb
      · exact absurd rfl hne
      · exact Or.inl (before_path hb b hmb)
    · rcases List.mem_cons.1 hb' with rfl | hmb
      · exact Or.inr (after_path haft a hma)
      · exact ih htb hta a b hma hmb hne

end Rows

/-- The helper rows of an output, in input order.  An output with no rows uses
    direct edges. -/
abbrev Rows := Array (List Row)

/-- The helper chains of one output, with its holders' entry edges. -/
def outputOk (g : Groups) {t : EdgeTable} (ix : EdgeTable.Index t) (node : Hold → Nat)
    (o : Nat) (rows : List Row) : Bool :=
  rows.isEmpty || (beforeOk ix rows && afterOk ix rows &&
  (g.feed.getD o []).all fun p' =>
    rows.any fun r =>
      r.input == p' && ix.mem (node (.grant o p'), r.before) && ix.mem (node (.grant o p'), r.after))

/-- The arbitration pairs of one route. -/
def arbOk (g : Groups) (cont : Rows) {t : EdgeTable} (ix : EdgeTable.Index t)
    (hints : Hints) (node : Hold → Nat) (p d o : Nat) : Bool :=
  match cont.getD o [] with
  | [] => (g.feed.getD o []).all fun p' =>
      p' == p || connected ix hints (node (.grant o p')) (node (.head p d))
  | rows =>
    outputOkAt rows
where
  outputOkAt (rows : List Row) : Bool :=
    rows.any fun r => r.input == p && connected ix hints r.other (node (.head p d))

/-- The pairs checked for one route. -/
def Groups.routeOk (g : Groups) (c : Config) (cont : Rows) {t : EdgeTable}
    (ix : EdgeTable.Index t)
    (hints : Hints) (node : Hold → Nat) (e : Nat × Nat × Nat) : Bool :=
  let p := e.1
  let d := e.2.1
  let o := e.2.2
  let link (x y : Hold) := connected ix hints (node x) (node y)
  (c.bound d || arbOk g cont ix hints node p d o) &&
  (match c.next o with
    | some r => (g.dests.getD r []).all fun d' =>
        link (.head r d') (.head p d) && link (.head r d') (.grant o p)
    | none =>
      match c.pool o d with
      | some r => (g.dests.getD r []).all fun d' =>
          link (.head r d') (.head p d) && link (.head r d') (.grant o p)
      | none => true) &&
  (!(c.cross p && c.mayHold p d) ||
    (c.ups p).all fun r => (g.others.getD r []).all fun f =>
      (r == p && f.2 == o) || link (.head r f.1) (.grant o p))

/-- Coverage, grouped.  `outs` lists the outputs that have rows. -/
def checkCoverageFast (c : Config) (cont : Rows) {t : EdgeTable} (ix : EdgeTable.Index t)
    (hints : Hints) (node : Hold → Nat) : Bool :=
  let g := mkGroups c
  g.ok c &&
  (List.range cont.size).all (fun o => outputOk g ix node o (cont.getD o [])) &&
  c.routes.all (g.routeOk c cont ix hints node)

theorem arb_path {g : Groups} {cont : Rows} {t : EdgeTable} {ix : EdgeTable.Index t}
    {hints : Hints} {node : Hold → Nat} {p d o p' : Nat}
    (hout : ∀ o, o < cont.size → outputOk g ix node o (cont.getD o []) = true)
    (harb : arbOk g cont ix hints node p d o = true)
    (hfeed : p' ∈ g.feed.getD o []) (hne : p' ≠ p) :
    t.Path (node (.grant o p')) (node (.head p d)) := by
  unfold arbOk at harb
  cases hrows : cont.getD o [] with
  | nil =>
    rw [hrows] at harb
    simp only [List.all_eq_true, Bool.or_eq_true, beq_iff_eq] at harb
    rcases harb p' hfeed with h | h
    · exact absurd h hne
    · exact connected_sound h
  | cons r0 rs =>
    rw [hrows] at harb
    simp only [arbOk.outputOkAt, List.any_eq_true, Bool.and_eq_true, beq_iff_eq] at harb
    obtain ⟨b, hb, hbp, hbo⟩ := harb
    have hsize : o < cont.size := by
      apply Classical.byContradiction
      intro hge
      have : cont.getD o [] = [] := by simp [Array.getD, hge]
      rw [this] at hrows
      simp at hrows
    have hok := hout o hsize
    rw [hrows] at hok
    simp only [outputOk, List.isEmpty_cons, Bool.false_or, Bool.and_eq_true, List.all_eq_true,
      List.any_eq_true, beq_iff_eq] at hok
    obtain ⟨⟨h1, h2⟩, h3⟩ := hok
    obtain ⟨a, ha, ⟨hap, hbe⟩, haf⟩ := h3 p' hfeed
    have hab : a ≠ b := by
      intro e
      subst e
      exact hne (hap.symm.trans hbp)
    have htail := connected_sound hbo
    rcases row_paths h1 h2 a b ha hb hab with hp | hp
    · exact (EdgeTable.Path.cons (EdgeTable.Index.mem_sound hbe) hp).comp htail
    · exact (EdgeTable.Path.cons (EdgeTable.Index.mem_sound haf) hp).comp htail

theorem checkCoverageFast_covered {c : Config} {cont : Rows} {t : EdgeTable}
    {ix : EdgeTable.Index t}
    {hints : Hints} {node : Hold → Nat}
    (h : checkCoverageFast c cont ix hints node = true) : Covered c t node := by
  simp only [checkCoverageFast, Bool.and_eq_true, Groups.ok, List.all_eq_true] at h
  obtain ⟨⟨hg, hout0⟩, hrow⟩ := h
  generalize mkGroups c = g at hg hout0 hrow
  have hout : ∀ o, o < cont.size → outputOk g ix node o (cont.getD o []) = true :=
    fun o ho => hout0 o (List.mem_range.2 ho)
  have hfeed : ∀ r, r ∈ c.routes → r.1 ∈ g.feed.getD r.2.2 [] := by
    intro r hr
    have := (hg r hr).1.1
    simpa using this
  have hdest : ∀ r, r ∈ c.routes → r.2.1 ∈ g.dests.getD r.1 [] := by
    intro r hr
    have := (hg r hr).1.2
    simpa using this
  have hother : ∀ r, r ∈ c.routes → (r.2.1, r.2.2) ∈ g.others.getD r.1 [] := by
    intro r hr
    have := (hg r hr).2
    simpa using this
  have hparts : ∀ e, e ∈ c.routes →
      (c.bound e.2.1 = false → ∀ p', p' ∈ g.feed.getD e.2.2 [] → p' ≠ e.1 →
        t.Path (node (.grant e.2.2 p')) (node (.head e.1 e.2.1))) ∧
      (∀ r d', (c.next e.2.2 = some r ∨ (c.next e.2.2 = none ∧ c.pool e.2.2 e.2.1 = some r)) →
        d' ∈ g.dests.getD r [] →
        t.Path (node (.head r d')) (node (.head e.1 e.2.1)) ∧
        t.Path (node (.head r d')) (node (.grant e.2.2 e.1))) ∧
      (c.cross e.1 = true → c.mayHold e.1 e.2.1 = true →
        ∀ r, r ∈ c.ups e.1 → ∀ f, f ∈ g.others.getD r [] → ¬ (r = e.1 ∧ f.2 = e.2.2) →
        t.Path (node (.head r f.1)) (node (.grant e.2.2 e.1))) := by
    intro e he
    have := hrow e he
    simp only [Groups.routeOk, Bool.and_eq_true, List.all_eq_true, Bool.or_eq_true,
      beq_iff_eq, Bool.not_eq_true'] at this
    obtain ⟨⟨h1, h2⟩, h3⟩ := this
    refine ⟨?_, ?_, ?_⟩
    · intro hfree p' hp' hne
      rcases h1 with h | h
      · rw [hfree] at h; simp at h
      · exact arb_path hout h hp' hne
    · intro r d' hr hd'
      rcases hr with hn | ⟨hn, hp⟩
      · simp only [hn, List.all_eq_true, Bool.and_eq_true] at h2
        exact ⟨connected_sound (h2 d' hd').1, connected_sound (h2 d' hd').2⟩
      · simp only [hn, hp, List.all_eq_true, Bool.and_eq_true] at h2
        exact ⟨connected_sound (h2 d' hd').1, connected_sound (h2 d' hd').2⟩
    · intro hc hm r hr f hf hne
      rcases h3 with h | h
      · simp [hc, hm] at h
      · rcases h r hr f hf with h' | h'
        · exact absurd h' hne
        · exact connected_sound h'
  intro r x y hmem
  cases r with
  | context => simp [required] at hmem
  | arbitration =>
    simp only [required, List.mem_flatMap, List.mem_filterMap] at hmem
    obtain ⟨e, he, p', hp', hif⟩ := hmem
    split at hif
    · simp at hif
    · rename_i hcond
      have hne : p' ≠ e.1 := fun h => hcond (Or.inl h)
      have hfree : c.bound e.2.1 = false := by
        cases hb : c.bound e.2.1 with
        | false => rfl
        | true => exact absurd (Or.inr hb) hcond
      simp only [Option.some.injEq, Prod.mk.injEq] at hif
      obtain ⟨rfl, rfl⟩ := hif
      simp only [feeders, List.mem_map, List.mem_filter, beq_iff_eq] at hp'
      obtain ⟨r', ⟨hr', hro⟩, rfl⟩ := hp'
      have := hfeed r' hr'
      rw [hro] at this
      exact (hparts e he).1 hfree _ this hne
  | nextWord =>
    simp only [required, List.mem_flatMap] at hmem
    obtain ⟨e, he, hin⟩ := hmem
    cases hn : c.next e.2.2 with
    | none => simp [hn] at hin
    | some r =>
      simp only [hn, List.mem_map] at hin
      obtain ⟨d', hd', hxy⟩ := hin
      simp only [Prod.mk.injEq] at hxy
      obtain ⟨rfl, rfl⟩ := hxy
      simp only [destsOf, List.mem_map, List.mem_filter, beq_iff_eq] at hd'
      obtain ⟨r', ⟨hr', hrp⟩, rfl⟩ := hd'
      have := hdest r' hr'
      rw [hrp] at this
      exact ((hparts e he).2.1 r _ (Or.inl hn) this).1
  | nextGrant =>
    simp only [required, List.mem_flatMap] at hmem
    obtain ⟨e, he, hin⟩ := hmem
    cases hn : c.next e.2.2 with
    | none => simp [hn] at hin
    | some r =>
      simp only [hn, List.mem_map] at hin
      obtain ⟨d', hd', hxy⟩ := hin
      simp only [Prod.mk.injEq] at hxy
      obtain ⟨rfl, rfl⟩ := hxy
      simp only [destsOf, List.mem_map, List.mem_filter, beq_iff_eq] at hd'
      obtain ⟨r', ⟨hr', hrp⟩, rfl⟩ := hd'
      have := hdest r' hr'
      rw [hrp] at this
      exact ((hparts e he).2.1 r _ (Or.inl hn) this).2
  | poolWord =>
    simp only [required, List.mem_flatMap] at hmem
    obtain ⟨e, he, hin⟩ := hmem
    cases hn : c.next e.2.2 with
    | some _ => simp [hn] at hin
    | none =>
      cases hp : c.pool e.2.2 e.2.1 with
      | none => simp [hn, hp] at hin
      | some r =>
        simp only [hn, hp, List.mem_map] at hin
        obtain ⟨d', hd', hxy⟩ := hin
        simp only [Prod.mk.injEq] at hxy
        obtain ⟨rfl, rfl⟩ := hxy
        simp only [destsOf, List.mem_map, List.mem_filter, beq_iff_eq] at hd'
        obtain ⟨r', ⟨hr', hrp⟩, rfl⟩ := hd'
        have := hdest r' hr'
        rw [hrp] at this
        exact ((hparts e he).2.1 r _ (Or.inr ⟨hn, hp⟩) this).1
  | poolGrant =>
    simp only [required, List.mem_flatMap] at hmem
    obtain ⟨e, he, hin⟩ := hmem
    cases hn : c.next e.2.2 with
    | some _ => simp [hn] at hin
    | none =>
      cases hp : c.pool e.2.2 e.2.1 with
      | none => simp [hn, hp] at hin
      | some r =>
        simp only [hn, hp, List.mem_map] at hin
        obtain ⟨d', hd', hxy⟩ := hin
        simp only [Prod.mk.injEq] at hxy
        obtain ⟨rfl, rfl⟩ := hxy
        simp only [destsOf, List.mem_map, List.mem_filter, beq_iff_eq] at hd'
        obtain ⟨r', ⟨hr', hrp⟩, rfl⟩ := hd'
        have := hdest r' hr'
        rw [hrp] at this
        exact ((hparts e he).2.1 r _ (Or.inr ⟨hn, hp⟩) this).2
  | holdAcross =>
    simp only [required, List.mem_flatMap] at hmem
    obtain ⟨e, he, hin⟩ := hmem
    split at hin
    · rename_i hcm
      simp only [List.mem_flatMap, List.mem_map, List.mem_filter, Bool.and_eq_true, beq_iff_eq,
        Bool.or_eq_true, bne_iff_ne] at hin
      obtain ⟨r, hr, f, ⟨hf, hfp, hfo⟩, hxy⟩ := hin
      simp only [Prod.mk.injEq] at hxy
      obtain ⟨rfl, rfl⟩ := hxy
      have := hother f hf
      rw [hfp] at this
      refine (hparts e he).2.2 hcm.1 hcm.2 r hr _ this ?_
      rintro ⟨h1, h2⟩
      rcases hfo with h | h
      · exact h h1
      · exact h h2
    · simp at hin

end Wormhole
