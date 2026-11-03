/-
  GenericNoC.Instances.TwoTargets: two targets answering two sources through
  one crossbar, where each target has one head word for both sources.

  Slots 0 and 1 are the targets.  Outputs and destinations 0 and 1 are the
  sources.  Three release policies:

  | Policy | Non-final word | Result |
  | --- | --- | --- |
  | `perWord` | every word ends its segment | checks, W2 proved |
  | `contiguous` | may hold, next word has the same destination | checks, W2 proved |
  | `holding` | may hold, next word may go elsewhere | no graph can pass |

  `holding` is the shape behind two real hangs: read data held toward a source
  while the next word of that target goes to the other source.  The second hang
  has write responses in that position.  `holding_unprovable` shows W2 fails for
  every graph and rank, so no certificate can repair it.
-/
import GenericNoC.WormholeDone

namespace Wormhole.TwoTargets

open GenericNoC

/-- The shared topology. -/
def base : Config where
  routes := [(0, 0, 0), (0, 1, 1), (1, 0, 0), (1, 1, 1)]
  nexts := []
  pools := []
  cap := fun _ => 4
  poolCap := fun _ => 0
  endpoints := [0, 1]
  mayHold := fun _ _ => false
  contig := fun _ => true
  cross := fun _ => false
  bound := fun _ => false
  slots := [0, 1]
  hops := fun _ _ => 1
  hopMax := 2
  ups := fun p => [p]
  answers := fun _ _ => false
  ahead := fun _ _ => false

/-- Each word ends its segment unless the next word is behind it, for the same source. -/
def perWord : Config := { base with ahead := fun _ _ => true, contig := fun _ => false }

def contiguous : Config := { base with mayHold := fun _ _ => true }

def holding : Config :=
  { base with mayHold := fun _ _ => true, contig := fun _ => false, cross := fun _ => true }

/-- Node numbers: heads 0..3, grants 4..7. -/
def nodeOf : Hold → Nat
  | .head p d => 2 * p + d
  | .grant o p => 4 + 2 * o + p
  | _ => 0

/-- The four arbitration edges, sorted: a grant held by the other target blocks
    a head. -/
def arbEdges : List (Nat × Nat) :=
  [ (nodeOf (.grant 0 0), nodeOf (.head 1 0)),
    (nodeOf (.grant 0 1), nodeOf (.head 0 0)),
    (nodeOf (.grant 1 0), nodeOf (.head 1 1)),
    (nodeOf (.grant 1 1), nodeOf (.head 0 1)) ]

/-- The four hold-across edges: a head going elsewhere blocks a held grant. -/
def holdEdges : List (Nat × Nat) :=
  [ (nodeOf (.head 0 1), nodeOf (.grant 0 0)),
    (nodeOf (.head 0 0), nodeOf (.grant 1 0)),
    (nodeOf (.head 1 1), nodeOf (.grant 0 1)),
    (nodeOf (.head 1 0), nodeOf (.grant 1 1)) ]

/-- The graph of a per-word or contiguous policy, with its rank. -/
def goodTable : EdgeTable := ⟨8, arbEdges, EdgeTable.rankArr #[1, 1, 1, 1, 0, 0, 0, 0]⟩

theorem goodTable_rank : goodTable.checkRank = true := by decide +kernel

theorem goodIndex_ok : goodTable.checkIndex arbEdges.toArray = true := by decide +kernel

def goodIndex : EdgeTable.Index goodTable :=
  EdgeTable.indexOfCheck arbEdges.toArray goodIndex_ok

/-! ### Positive instances -/

theorem perWord_covered : checkCoverage perWord goodIndex #[] nodeOf = true := by decide +kernel

theorem contiguous_covered : checkCoverage contiguous goodIndex #[] nodeOf = true := by
  decide +kernel

theorem perWord_structural (env : (Nat → St) → Prop) :
    StructuralInstanceLemmas (noc perWord goodTable nodeOf env goodTable_rank) :=
  structural (by decide +kernel) goodTable_rank (checkCoverage_covered perWord_covered)
    (by decide +kernel)

theorem contiguous_structural (env : (Nat → St) → Prop) :
    StructuralInstanceLemmas (noc contiguous goodTable nodeOf env goodTable_rank) :=
  structural (by decide +kernel) goodTable_rank (checkCoverage_covered contiguous_covered)
    (by decide +kernel)

/-! ### Negative controls: every one of these must evaluate to `false` -/

/-- The honest graph of the holding policy, with both edge sets. -/
def holdingTable (rank : Nat → Nat) : EdgeTable := ⟨8, arbEdges ++ holdEdges, rank⟩

/-- That graph has a cycle. -/
theorem holdingTable_cycle (rank : Nat → Nat) :
    (holdingTable rank).Path (nodeOf (.head 0 1)) (nodeOf (.head 0 1)) := by
  have e : ∀ {u v}, (u, v) ∈ arbEdges ++ holdEdges → (holdingTable rank).edgeRel u v :=
    fun h => h
  have e1 : (holdingTable rank).edgeRel (nodeOf (.head 0 1)) (nodeOf (.grant 0 0)) :=
    e (by decide)
  have e2 : (holdingTable rank).edgeRel (nodeOf (.grant 0 0)) (nodeOf (.head 1 0)) :=
    e (by decide)
  have e3 : (holdingTable rank).edgeRel (nodeOf (.head 1 0)) (nodeOf (.grant 1 1)) :=
    e (by decide)
  have e4 : (holdingTable rank).edgeRel (nodeOf (.grant 1 1)) (nodeOf (.head 0 1)) :=
    e (by decide)
  exact .trans (.trans (.trans (.single e1) e2) e3) e4

/-- A graph with a cycle fails the checker for every rank. -/
theorem holdingTable_rejected (rank : Nat → Nat) : (holdingTable rank).checkRank = false :=
  EdgeTable.checkRank_false_of_cycle (holdingTable_cycle rank)

/-- A wrong rank certificate fails on an acyclic graph. -/
theorem wrongRank_rejected : (EdgeTable.mk 8 arbEdges (EdgeTable.rankArr #[0, 0, 0, 0, 1, 1, 1, 1])).checkRank = false := by
  decide +kernel

/-- An edge out of range fails, whatever the rank says. -/
theorem outOfRange_rejected :
    (EdgeTable.mk 8 [(0, 8)] (EdgeTable.rankArr #[0, 0, 0, 0, 0, 0, 0, 0, 1])).checkRank = false := by
  decide +kernel

/-- The good graph does not cover the holding policy: the certificate passes but
    the hold-across rule does not. -/
theorem holding_goodGraph_rejected :
    checkRule holding goodIndex #[] nodeOf .holdAcross = false := by decide +kernel

/-- The other rules do pass on that pair, so the rejection is that rule's. -/
theorem holding_goodGraph_arb_ok :
    checkRule holding goodIndex #[] nodeOf .arbitration = true := by decide +kernel

/-- A graph with one rule edge removed fails coverage. -/
def missingEdgeTable : EdgeTable := ⟨8, arbEdges.tail, EdgeTable.rankArr #[1, 1, 1, 1, 0, 0, 0, 0]⟩

theorem missingEdgeIndex_ok : missingEdgeTable.checkIndex arbEdges.tail.toArray = true := by
  decide +kernel

def missingEdgeIndex : EdgeTable.Index missingEdgeTable :=
  EdgeTable.indexOfCheck arbEdges.tail.toArray missingEdgeIndex_ok

theorem missingEdge_rejected :
    checkCoverage perWord missingEdgeIndex #[] nodeOf = false := by decide +kernel

/-! ### The holding policy cannot be proved, by any graph -/

/-- Word for `d`, not ending its segment. -/
def nonFinal (d tok : Nat) : Word := ⟨d, false, false, tok⟩

/-- Word for `d`, ending its segment. -/
def final (d tok : Nat) : Word := ⟨d, true, true, tok⟩

/-- States of the run.  `second` is the word each target presents after its
    held first word.  Read data and a write response give the same block. -/
def addWord (σ : St) (p : Nat) (w : Word) (b : Nat) : St := injected σ p w b

/-- The words a one-word answer declares: none after it if it is the last. -/
def budgetOf (w : Word) : Nat := if w.last then 0 else 1

def take (σ : St) (p o : Nat) : St :=
  { σ with grant := upd σ.grant o (some p), ev := none }

def pass (σ : St) (p o : Nat) (w : Word) : St := place holding (pop σ p o w) o w

def s0 : St := addWord St.empty 0 (nonFinal 0 1) 2
def s1 : St := addWord s0 1 (nonFinal 1 2) 2
def s2 : St := take s1 0 0
def s3 : St := take s2 1 1
def s4 : St := pass s3 0 0 (nonFinal 0 1)
def s5 : St := pass s4 1 1 (nonFinal 1 2)
def s6 (second0 : Word) : St := addWord s5 0 second0 (budgetOf second0)
def s7 (second0 second1 : Word) : St := addWord (s6 second0) 1 second1 (budgetOf second1)
/-- Each held read's next word, behind the head that goes elsewhere. -/
def s8 (second0 second1 : Word) : St := addWord (s7 second0 second1) 0 (nonFinal 0 1) 0
def s9 (second0 second1 : Word) : St := addWord (s8 second0 second1) 1 (nonFinal 1 2) 0

/-- The run, stuttering at the end. -/
def run (second0 second1 : Word) : Nat → St
  | 0 => St.empty
  | 1 => s0
  | 2 => s1
  | 3 => s2
  | 4 => s3
  | 5 => s4
  | 6 => s5
  | 7 => s6 second0
  | 8 => s7 second0 second1
  | 9 => s8 second0 second1
  | _ => s9 second0 second1

/-- The run is a legal run of the holding policy.  Each later word goes to the
    other source and is either read data or a final write response. -/
theorem run_steps (x0 x1 : Bool) (τ : Nat) :
    Step holding (run (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4) τ)
      (run (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4) (τ + 1)) := by
  have hnc : ∀ {P : Prop}, holding.contig 0 = true → P := fun h => absurd h (by decide)
  have hnc1 : ∀ {P : Prop}, holding.contig 1 = true → P := fun h => absurd h (by decide)
  have hnb : ∀ {P : Prop} {d : Nat}, holding.bound d = true → P := fun h => absurd h (by simp [holding, base])
  have hend : ∀ p d, d ≤ 1 → p ≤ 1 → EndsFree holding p d := by
    intro p d hd hp
    match p, d, hp, hd with
    | 0, 0, _, _ => exact .sink (o := 0) rfl rfl rfl
    | 0, 1, _, _ => exact .sink (o := 1) rfl rfl rfl
    | 1, 0, _, _ => exact .sink (o := 0) rfl rfl rfl
    | 1, 1, _, _ => exact .sink (o := 1) rfl rfl rfl
  have hfin : ∀ {w : Word} {p}, w.last = w.tail → w.dest ≤ 1 → p ≤ 1 →
      w.last = true → w.tail = true ∧ EndsFree holding p w.dest :=
    fun e hd hp hl => ⟨e ▸ hl, hend _ _ hd hp⟩
  match τ with
  | 0 =>
    exact (Step.inject (c := holding) St.empty 0 (nonFinal 0 1) 2 (by decide) (by decide)
      (Or.inr (Or.inl rfl)) hnc rfl (by decide) (by decide) (by decide) (by decide) (by decide)
      (fun h => by simp [nonFinal] at h) hnb : Step holding St.empty s0)
  | 1 =>
    exact (Step.inject (c := holding) s0 1 (nonFinal 1 2) 2 (by decide) (by decide)
      (Or.inr (Or.inl rfl)) hnc1 rfl (by decide) (by decide) (by decide) (by decide) (by decide)
      (fun h => by simp [nonFinal] at h) hnb : Step holding s0 s1)
  | 2 => exact (Step.arb (c := holding) s1 0 0 (nonFinal 0 1) rfl rfl rfl : Step holding s1 s2)
  | 3 => exact (Step.arb (c := holding) s2 1 1 (nonFinal 1 2) rfl rfl rfl : Step holding s2 s3)
  | 4 =>
    exact (Step.fire (c := holding) s3 0 0 (nonFinal 0 1) rfl rfl rfl (Or.inl trivial) (by decide)
      (fun h => absurd h (by decide)) : Step holding s3 s4)
  | 5 =>
    exact (Step.fire (c := holding) s4 1 1 (nonFinal 1 2) rfl rfl rfl (Or.inl trivial) (by decide)
      (fun h => absurd h (by decide)) : Step holding s4 s5)
  | 6 =>
    exact (Step.inject (c := holding) s5 0 (Word.mk 1 x0 x0 3) (budgetOf (Word.mk 1 x0 x0 3))
      (by decide) (by decide) (Or.inr (Or.inl rfl)) hnc rfl
      (by cases x0 <;> decide) (by cases x0 <;> decide) (fun h => absurd h (by decide))
      (by cases x0 <;> decide) (by cases x0 <;> decide)
      (hfin rfl (show (1 : Nat) ≤ 1 by decide) (by decide)) hnb :
      Step holding s5 (s6 (Word.mk 1 x0 x0 3)))
  | 7 =>
    exact (Step.inject (c := holding) (s6 (Word.mk 1 x0 x0 3)) 1 (Word.mk 0 x1 x1 4)
      (budgetOf (Word.mk 0 x1 x1 4))
      (by decide) (show (0 : Nat) < 4 by decide) (Or.inr (Or.inl rfl)) hnc1 rfl
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (fun h => absurd h (by decide))
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (hfin rfl (show (0 : Nat) ≤ 1 by decide) (by decide)) hnb :
      Step holding (s6 (Word.mk 1 x0 x0 3)) (s7 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)))
  | 8 =>
    exact (Step.inject (c := holding) (s7 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)) 0 (nonFinal 0 1) 0
      (by decide) (show (1 : Nat) < 4 by decide) (Or.inr (Or.inl rfl)) hnc rfl
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (fun h => absurd h (by decide))
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (fun h => by simp [nonFinal] at h) hnb :
      Step holding (s7 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4))
        (s8 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)))
  | 9 =>
    exact (Step.inject (c := holding) (s8 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)) 1 (nonFinal 1 2) 0
      (by decide) (show (1 : Nat) < 4 by decide) (Or.inr (Or.inl rfl)) hnc1 rfl
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (fun h => absurd h (by decide))
      (by cases x0 <;> cases x1 <;> decide) (by cases x0 <;> cases x1 <;> decide)
      (fun h => by simp [nonFinal] at h) hnb :
      Step holding (s8 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4))
        (s9 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)))
  | n + 10 =>
    exact (Step.stutter (s9 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)) :
      Step holding (s9 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4))
        (s9 (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)))

/-- In the final state the four points form a closed wait: both outputs are held,
    each waits for its holder's next word, and that word is behind a head that
    needs the other output. -/
theorem holding_unprovable (t : EdgeTable) (hrank : t.checkRank = true) (node : Hold → Nat)
    (env : (Nat → St) → Prop) (x0 x1 : Bool) :
    ¬ StructuralInstanceLemmas (noc holding t node env hrank) := by
  intro str
  let w0 : Word := ⟨1, x0, x0, 3⟩
  let w1 : Word := ⟨0, x1, x1, 4⟩
  let s := run w0 w1
  have hlegal : (noc holding t node env hrank).Legal s :=
    ⟨rfl, fun τ => run_steps x0 x1 τ⟩
  have hσ : s 10 = s9 w0 w1 := rfl
  have hhead0 : ((s9 w0 w1).q 0).head? = some w0 := rfl
  have hhead1 : ((s9 w0 w1).q 1).head? = some w1 := rfl
  have hg0 : (s9 w0 w1).grant 0 = some 0 := rfl
  have hg1 : (s9 w0 w1).grant 1 = some 1 := rfl
  have hi0 : headIs (s9 w0 w1) 0 1 := ⟨w0, hhead0, rfl⟩
  have hi1 : headIs (s9 w0 w1) 1 0 := ⟨w1, hhead1, rfl⟩
  have hno0 : ¬ headRoutes holding (s9 w0 w1) 0 0 := by
    rintro ⟨a, ha, hr⟩
    rw [hhead0] at ha
    have : w0 = a := by simpa using ha
    subst this
    exact absurd hr (show ¬ holding.route 0 1 = some 0 by decide)
  have hno1 : ¬ headRoutes holding (s9 w0 w1) 1 1 := by
    rintro ⟨a, ha, hr⟩
    rw [hhead1] at ha
    have : w1 = a := by simpa using ha
    subst this
    exact absurd hr (show ¬ holding.route 1 0 = some 1 by decide)
  let W : Hold → Prop := fun h =>
    h = .grant 0 0 ∨ h = .head 0 1 ∨ h = .grant 1 1 ∨ h = .head 1 0
  have hclosed : ∀ y, W y → ∃ x, W x ∧ (noc holding t node env hrank).blocked (s 10) x y := by
    intro y hy
    rw [hσ]
    rcases hy with rfl | rfl | rfl | rfl
    · -- Output 0, held by target 0, waits behind target 0's word for source 1.
      refine ⟨.head 0 1, Or.inr (Or.inl rfl), own_head_isSome.2 hi0, own_grant_isSome.2 hg0,
        ⟨hg0, Or.inr ⟨.refl 0, hi0, hno0, Or.inl rfl⟩⟩, ?_⟩
      intro hp
      exact hp.2 ⟨.refl 0, hi0, hno0, Or.inl rfl⟩
    · -- Target 0's word for source 1 needs output 1, held by target 1.
      refine ⟨.grant 1 1, Or.inr (Or.inr (Or.inl rfl)), own_grant_isSome.2 hg1,
        own_head_isSome.2 hi0, ⟨hi0, rfl, by decide⟩, ?_⟩
      intro hp
      exact hp hg1
    · -- Output 1, held by target 1, waits behind target 1's word for source 0.
      refine ⟨.head 1 0, Or.inr (Or.inr (Or.inr rfl)), own_head_isSome.2 hi1,
        own_grant_isSome.2 hg1, ⟨hg1, Or.inr ⟨.refl 1, hi1, hno1, Or.inl rfl⟩⟩, ?_⟩
      intro hp
      exact hp.2 ⟨.refl 1, hi1, hno1, Or.inl rfl⟩
    · -- Target 1's word for source 0 needs output 0, held by target 0.
      refine ⟨.grant 0 0, Or.inl rfl, own_grant_isSome.2 hg0,
        own_head_isSome.2 hi1, ⟨hi1, rfl, by decide⟩, ?_⟩
      intro hp
      exact hp hg0
  exact noClosedResourceWait _ str hlegal 10 (y₀ := .grant 0 0) (W := W) (Or.inl rfl) hclosed

/-- The second hang, with write responses (`tail` and `last` set) behind the held
    read words.  The same theorem, instantiated. -/
theorem holding_write_responses_unprovable (t : EdgeTable) (hrank : t.checkRank = true)
    (node : Hold → Nat) (env : (Nat → St) → Prop) :
    ¬ StructuralInstanceLemmas (noc holding t node env hrank) :=
  holding_unprovable t hrank node env true true

/-! ### `Fair` does not hide the hang

An environment assumption that is too strong can make a false system look proved.
This run hangs, and it satisfies `Fair`, so `Fair` does not exclude it. -/

theorem run_late (w0 w1 : Word) {t : Nat} (h : 10 ≤ t) : run w0 w1 t = s9 w0 w1 := by
  match t, h with
  | n + 10, _ => rfl

theorem s9_grant (w0 w1 : Word) (o p : Nat) (h : (s9 w0 w1).grant o = some p) : o = p ∧ o ≤ 1 := by
  match o, h with
  | 0, h =>
    have h' : some (0 : Nat) = some p := h
    have : (0 : Nat) = p := by simpa using h'
    exact ⟨this, by decide⟩
  | 1, h =>
    have h' : some (1 : Nat) = some p := h
    have : (1 : Nat) = p := by simpa using h'
    exact ⟨this, by decide⟩
  | n + 2, h => exact absurd h (by simp [s9, s8, s7, s6, s5, s4, s3, s2, s1, s0, addWord, injected, pass, take,
      pop, place, nonFinal, nextOpen, St.empty, upd, holding, base, Config.next, Config.pool])

/-- The route of this topology: the output is the destination. -/
theorem route_is_dest {p d o : Nat} (h : holding.route p d = some o) :
    o = d ∧ p ≤ 1 ∧ d ≤ 1 := by
  have hm := Config.route_mem h
  simp only [holding, base, List.mem_cons, Prod.mk.injEq, List.not_mem_nil, or_false] at hm
  omega

theorem hang_run_fair (x0 x1 : Bool) :
    Fair holding (run (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)) := by
  let w0 : Word := Word.mk 1 x0 x0 3
  let w1 : Word := Word.mk 0 x1 x1 4
  have hl : ∀ t, 10 ≤ t → run w0 w1 t = s9 w0 w1 := fun _ h => run_late w0 w1 h
  have hgrant : ∀ o, o ≤ 1 → (s9 w0 w1).grant o = some o := by
    intro o ho
    match o, ho with
    | 0, _ => rfl
    | 1, _ => rfl
  have hhead : ∀ p, p ≤ 1 → ∃ w, ((s9 w0 w1).q p).head? = some w ∧ w.dest = 1 - p := by
    intro p hp
    match p, hp with
    | 0, _ => exact ⟨w0, rfl, rfl⟩
    | 1, _ => exact ⟨w1, rfl, rfl⟩
  have hev : ∀ t, 10 ≤ t → (run w0 w1 (t + 1)).ev = none := by
    intro t ht
    rw [hl (t + 1) (by omega)]
    rfl
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_⟩
  · -- `arb_fair`: the output is never free again, so the premise is false.
    intro τ p d o hr _ hfree
    obtain ⟨t, ht, hf⟩ := hfree (τ + 10) (by omega)
    rw [hl t (by omega), hgrant o (by have := route_is_dest hr; omega)] at hf
    simp at hf
  · -- `fire_fair`: a holder's head never goes through its held output.
    intro τ p d o hr hall _
    have ⟨hg, hi⟩ := hall (τ + 10) (by omega)
    rw [hl (τ + 10) (by omega)] at hg hi
    obtain ⟨rfl, ho⟩ := s9_grant w0 w1 o p hg
    obtain ⟨w, hw, hd⟩ := hhead o ho
    obtain ⟨w', hw', hd'⟩ := hi
    rw [hw] at hw'
    have : w = w' := by simpa using hw'
    subst this
    have := route_is_dest hr
    omega
  · -- `seg_finite`: nothing fires after the last injection.
    intro τ o p _
    refine ⟨τ + 10, by omega, fun t ht he => ?_⟩
    rw [hev t (by omega)] at he
    simp at he
  · -- `cont`: the holder's head is stuck on a word going elsewhere.
    intro τ o p _ hall
    have hg := (hall (τ + 10) (by omega)).1
    rw [hl (τ + 10) (by omega)] at hg
    obtain ⟨rfl, ho⟩ := s9_grant w0 w1 o p hg
    obtain ⟨w, hw, hd⟩ := hhead o ho
    refine Or.inr ⟨τ + 10, o, 1 - o, by omega, .refl o, Or.inl rfl, ?_, ?_⟩
    · rw [hl (τ + 10) (by omega)]
      rintro ⟨a, ha, hr⟩
      rw [hw] at ha
      have : w = a := by simpa using ha
      subst this
      rw [hd] at hr
      have := route_is_dest hr
      omega
    · intro t ht
      refine ⟨?_, ?_⟩
      · rw [hl t (by omega)]
        exact own_head_isSome.2 ⟨w, hw, hd⟩
      · rintro ⟨_, o', he⟩
        rw [hev t (by omega)] at he
        simp at he
  · -- `pool_fair`: this topology has no pool.
    intro τ o d r _ hp _
    simp [holding, base, Config.pool] at hp
  · -- `answer`: every live transaction has a word in a slot, so none waits.
    intro τ k hst
    exfalso
    have hw := own_wait_isSome.1 (hst (τ + 10) (by omega)).1
    rw [hl (τ + 10) (by omega)] at hw
    obtain ⟨hlive, hno⟩ := hw
    have hk : k = 1 ∨ k = 2 ∨ k = 3 ∨ k = 4 := by
      apply Classical.byContradiction
      intro hk
      have h1 : k ≠ 1 := fun e => hk (Or.inl e)
      have h2 : k ≠ 2 := fun e => hk (Or.inr (Or.inl e))
      have h3 : k ≠ 3 := fun e => hk (Or.inr (Or.inr (Or.inl e)))
      have h4 : k ≠ 4 := fun e => hk (Or.inr (Or.inr (Or.inr e)))
      simp [s9, s8, s7, s6, s5, s4, s3, s2, s1, s0, addWord, pass, take, pop, place, nonFinal,
        St.empty, injected, upd, holding, base, Config.next, Config.pool, h1, h2, h3, h4, w0, w1] at hlive
    rcases hk with rfl | rfl | rfl | rfl
    · exact hno 0 ⟨nonFinal 0 1, by simp [s9, s8, addWord, injected, upd], rfl⟩
    · exact hno 1 ⟨nonFinal 1 2, by simp [s9, addWord, injected, upd], rfl⟩
    · exact hno 0 ⟨w0, by simp [s9, s8, s7, s6, addWord, injected, upd, s5, s4, s3, s2, s1, s0, pass, take, pop, place, nonFinal, St.empty, holding, base, Config.next, Config.pool], rfl⟩
    · exact hno 1 ⟨w1, by simp [s9, s8, s7, s6, addWord, injected, upd, s5, s4, s3, s2, s1, s0, pass, take, pop, place, nonFinal, St.empty, holding, base, Config.next, Config.pool], rfl⟩

/-- That run has a permanent stall, so `Fair` alone proves nothing here.  The
    hold-across rule is what fails. -/
theorem hang_run_stalls (x0 x1 : Bool) :
    StallAt (run (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4)) 10 (.grant 0 0) := by
  intro t ht
  have hl := run_late (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4) ht
  have hl' := run_late (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4) (show 10 ≤ t + 1 by omega)
  refine ⟨?_, ?_⟩
  · rw [hl]; exact own_grant_isSome.2 rfl
  · rintro ⟨_, he⟩
    rw [hl'] at he
    simp [s9, addWord, injected] at he

/-! ### `Fair` is not empty on the good policy

One word is delivered and the transaction ends.  The run is legal and fair, so
the theorems about checked instances do not hold vacuously. -/

def d1 : St := addWord St.empty 0 (final 0 7) 0
def d2 : St := take d1 0 0
def d3 : St := place perWord (pop d2 0 0 (final 0 7)) 0 (final 0 7)
def d4 : St := { d3 with ev := none }

def delivered : Nat → St
  | 0 => St.empty
  | 1 => d1
  | 2 => d2
  | 3 => d3
  | _ => d4

theorem delivered_steps (τ : Nat) : Step perWord (delivered τ) (delivered (τ + 1)) := by
  match τ with
  | 0 =>
    exact (Step.inject (c := perWord) St.empty 0 (final 0 7) 0 (by decide) (by decide)
      (Or.inl rfl) (fun _ _ h => by simp [St.empty] at h) rfl (by decide) (by decide) (by decide) (by decide) (by decide)
      (fun _ => ⟨rfl, .sink (o := 0) rfl rfl rfl⟩) (fun h => absurd h (by simp [perWord, base])) :
      Step perWord St.empty d1)
  | 1 => exact (Step.arb (c := perWord) d1 0 0 (final 0 7) rfl rfl rfl : Step perWord d1 d2)
  | 2 =>
    exact (Step.fire (c := perWord) d2 0 0 (final 0 7) rfl rfl rfl (Or.inl trivial) (by decide)
      (fun _ h => absurd h (by decide)) :
      Step perWord d2 d3)
  | 3 => exact (Step.stutter d3 : Step perWord d3 d4)
  | n + 4 => exact (Step.stutter d4 : Step perWord d4 d4)

theorem delivered_late {t : Nat} (h : 4 ≤ t) : delivered t = d4 := by
  match t, h with
  | n + 4, _ => rfl

theorem d4_q (p : Nat) : d4.q p = [] := by
  match p with
  | 0 => rfl
  | n + 1 => rfl

theorem d4_grant (o : Nat) : d4.grant o = none := by
  match o with
  | 0 => rfl
  | n + 1 => rfl

theorem d4_live (k : Nat) : d4.live k = false := by
  by_cases hk : k = 7
  · subst hk; rfl
  · simp [d4, d3, d2, d1, addWord, injected, take, pop, place, final, St.empty, upd, perWord, base,
      Config.next, Config.pool, hk]

theorem delivered_fair : Fair perWord delivered := by
  have hl : ∀ t, 4 ≤ t → delivered t = d4 := fun _ h => delivered_late h
  have hnohead : ∀ p d, ¬ headIs d4 p d := by
    rintro p d ⟨w, hw, _⟩
    rw [d4_q] at hw
    simp at hw
  refine ⟨?_, ?_, ?_, ?_, ?_, ?_⟩
  · intro τ p d o _ hh _
    have := hh (τ + 4) (by omega)
    rw [hl _ (by omega)] at this
    exact absurd this (hnohead p d)
  · intro τ p d o _ hh _
    have := (hh (τ + 4) (by omega)).2
    rw [hl _ (by omega)] at this
    exact absurd this (hnohead p d)
  · intro τ o p hh
    have := hh (τ + 4) (by omega)
    rw [hl _ (by omega), d4_grant] at this
    simp at this
  · intro τ o p _ hh
    have := (hh (τ + 4) (by omega)).1
    rw [hl _ (by omega), d4_grant] at this
    simp at this
  · intro τ o d r _ hp _
    simp [perWord, base, Config.pool] at hp
  · intro τ k hst
    have hw := own_wait_isSome.1 (hst (τ + 4) (by omega)).1
    rw [hl _ (by omega)] at hw
    have := hw.1
    rw [d4_live] at this
    simp at this

/-- That run ends its transaction. -/
theorem delivered_ends : (delivered 4).live 7 = false := rfl

/-! ### Completion and one-target binding -/

/-- With per-word release, every transaction ends on every fair run. -/
theorem perWord_completes {s : Nat → St} (h0 : s 0 = St.empty)
    (hs : ∀ n, Step perWord (s n) (s (n + 1))) (F : Fair perWord s) (τ k : Nat) :
    ∃ u, τ ≤ u ∧ (s u).live k = false :=
  checked_transaction_completes (t := goodTable) (node := nodeOf) (by decide +kernel)
    (fun _ => by show 0 < 4; decide) (by decide +kernel) goodTable_rank
    (checkCoverage_covered perWord_covered) (by decide +kernel) h0 hs F τ k

/-- The hop table is checked, not trusted: a table that does not fall along the
    routes or stay below the maximum fails. -/
theorem zeroHops_rejected : hopsOk { perWord with hops := fun _ _ => 0 } = false := by
  decide +kernel

theorem lowMax_rejected : hopsOk { perWord with hopMax := 1 } = false := by decide +kernel

/-- The hanging run never ends its first transaction, and it is fair.  So
    completion is a real claim: it fails where the rule lemmas fail. -/
theorem hang_run_never_ends (x0 x1 : Bool) (t : Nat) (h : 10 ≤ t) :
    (run (Word.mk 1 x0 x0 3) (Word.mk 0 x1 x1 4) t).live 1 = true := by
  rw [run_late _ _ h]
  rfl

/-- The safety rule of ending segments ahead: a word without a cut cannot leave when
    the next word goes elsewhere. -/
theorem no_cut_needs_next_word (rest : List Word) (a : Word) (h : a.dest ≠ 0) :
    ¬ ∃ b, (nonFinal 0 1 :: a :: rest).tail.head? = some b ∧ b.dest = (nonFinal 0 1).dest := by
  rintro ⟨b, hb, hd⟩
  simp at hb
  subst hb
  exact h hd

/-- Holding across words, with each source bound to one target at a time: the
    answer to a source that has one ID and one direction. -/
def holdingBound : Config := { holding with bound := fun _ => true }

/-- That policy needs the hold-across edges and no arbitration edges, because no
    two targets ever compete for a source. -/
def boundTable : EdgeTable :=
  ⟨8, [ (nodeOf (.head 0 0), nodeOf (.grant 1 0)),
        (nodeOf (.head 0 1), nodeOf (.grant 0 0)),
        (nodeOf (.head 1 0), nodeOf (.grant 1 1)),
        (nodeOf (.head 1 1), nodeOf (.grant 0 1)) ],
    EdgeTable.rankArr #[0, 0, 0, 0, 1, 1, 1, 1]⟩

theorem boundTable_rank : boundTable.checkRank = true := by decide +kernel

theorem boundIndex_ok : boundTable.checkIndex boundTable.edges.toArray = true := by decide +kernel

def boundIndex : EdgeTable.Index boundTable :=
  EdgeTable.indexOfCheck boundTable.edges.toArray boundIndex_ok

theorem holdingBound_covered : checkCoverage holdingBound boundIndex #[] nodeOf = true := by
  decide +kernel

theorem holdingBound_structural (env : (Nat → St) → Prop) :
    StructuralInstanceLemmas (noc holdingBound boundTable nodeOf env boundTable_rank) :=
  structural (by decide +kernel) boundTable_rank (checkCoverage_covered holdingBound_covered)
    (by decide +kernel)

/-- The same table does not cover the policy without the binding. -/
theorem holding_boundGraph_rejected :
    checkRule holding boundIndex #[] nodeOf .arbitration = false := by decide +kernel

/-- The binding is what stops the hang: in the state where the hang needs its next
    word, the other target still holds that source, so the word cannot be added. -/
theorem binding_stops_the_hang :
    touches holdingBound s5 1 1 ∧ touches holdingBound s5 0 0 :=
  ⟨Or.inr ⟨1, rfl, rfl⟩, Or.inr ⟨0, rfl, rfl⟩⟩

#print axioms Wormhole.checked_transaction_completes
#print axioms Wormhole.waiting_has_budget
#print axioms perWord_completes
#print axioms holdingBound_structural
#print axioms hang_run_never_ends
#print axioms hang_run_fair
#print axioms delivered_fair
#print axioms Wormhole.liveWork_held
#print axioms Wormhole.checked_instance_live_work_advances
#print axioms Wormhole.instanceLemmas
#print axioms GenericNoC.advances_on_every_compliant_run
#print axioms EdgeTable.checkRank_iff_checkEdgeRank
#print axioms EdgeTable.checkRank_false_of_cycle
#print axioms Wormhole.inv_run
#print axioms Wormhole.hold_across_needs_cross
#print axioms Wormhole.structural
#print axioms perWord_structural
#print axioms contiguous_structural
#print axioms holding_unprovable
#print axioms holdingTable_rejected

end Wormhole.TwoTargets
