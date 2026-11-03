/-
  GenericNoC.EdgeTable: the dependency graph as an explicit edge list with a
  rank certificate, and a checker that is linear in the number of edges.

  `FinGraph.checkEdgeRank` enumerates every node pair, which cannot handle
  large instances.  `EdgeTable.checkRank` visits each edge once and is proved
  equivalent to the pair enumeration (`checkRank_iff_checkEdgeRank`).

  Only the certificate direction is used: a passing rank proves that the graph
  has no cycle.  The checker does not search for a cycle and does not compute
  a rank.
-/
import GenericNoC.Checkers

/-- A dependency graph as a list of `(blocker, blocked)` pairs on nodes
    `0 .. size - 1`, with a claimed rank per node. -/
structure EdgeTable where
  size : Nat
  edges : List (Nat × Nat)
  rank : Nat → Nat

namespace EdgeTable

/-- The claimed rank of a node. -/
def rankOf (t : EdgeTable) (u : Nat) : Nat := t.rank u

/-- A rank given as an array.  A node past the end has rank 0. -/
def rankArr (a : Array Nat) : Nat → Nat := fun u => a.getD u 0

/-- One edge of the certificate: both ends in range, rank strictly increases
    from blocker to blocked. -/
def edgeOk (t : EdgeTable) (e : Nat × Nat) : Bool :=
  decide (e.1 < t.size) && decide (e.2 < t.size) && decide (t.rankOf e.1 < t.rankOf e.2)

/-- The linear certificate check. -/
def checkRank (t : EdgeTable) : Bool :=
  t.edges.all t.edgeOk

/-- Edge relation of the table. -/
def edgeRel (t : EdgeTable) (u v : Nat) : Prop := (u, v) ∈ t.edges

theorem checkRank_spec {t : EdgeTable} :
    t.checkRank = true ↔
      ∀ u v, t.edgeRel u v → u < t.size ∧ v < t.size ∧ t.rankOf u < t.rankOf v := by
  simp only [checkRank, List.all_eq_true]
  constructor
  · intro h u v he
    have := h (u, v) he
    simp only [edgeOk, Bool.and_eq_true, decide_eq_true_eq] at this
    exact ⟨this.1.1, this.1.2, this.2⟩
  · intro h e he
    obtain ⟨u, v⟩ := e
    have := h u v he
    simp only [edgeOk, Bool.and_eq_true, decide_eq_true_eq]
    exact ⟨⟨this.1, this.2.1⟩, this.2.2⟩

/-- The certificate gives the strict rank step along each edge. -/
theorem rank_step {t : EdgeTable} (h : t.checkRank = true) {u v : Nat}
    (he : t.edgeRel u v) : t.rankOf u < t.rankOf v :=
  (checkRank_spec.1 h u v he).2.2

/-- Positive-length path in the table. -/
inductive Path (t : EdgeTable) : Nat → Nat → Prop where
  | single {u v} : t.edgeRel u v → Path t u v
  | trans {u v w} : Path t u v → t.edgeRel v w → Path t u w

theorem Path.rank_lt {t : EdgeTable} (h : t.checkRank = true) {u v : Nat}
    (p : t.Path u v) : t.rankOf u < t.rankOf v := by
  induction p with
  | single he => exact rank_step h he
  | trans _ he ih => exact Nat.lt_trans ih (rank_step h he)

/-- A table with a cycle fails the check, whatever rank it claims. -/
theorem checkRank_false_of_cycle {t : EdgeTable} {u : Nat} (p : t.Path u u) :
    t.checkRank = false := by
  cases hc : t.checkRank with
  | false => rfl
  | true => exact absurd (Path.rank_lt hc p) (Nat.lt_irrefl _)

/-- The same table as a `FinGraph`. -/
def toFinGraph (t : EdgeTable) : FinGraph :=
  ⟨t.size, fun u v => t.edges.contains (u.val, v.val)⟩

/-- Well-formedness that the pair enumeration cannot see: it never looks at an
    edge whose ends are out of range. -/
def wellFormed (t : EdgeTable) : Prop :=
  ∀ u v, t.edgeRel u v → u < t.size ∧ v < t.size

/-- Specification of the pair enumeration in `Checkers.lean`. -/
theorem checkEdgeRank_spec {g : FinGraph} {r : Fin g.size → Nat} :
    g.checkEdgeRank r = true ↔ ∀ u v, g.edge u v = true → r u < r v := by
  have hfin : ∀ {n : Nat} (i : Fin n), i ∈ List.finRange n := fun i => by simp [List.finRange, List.mem_ofFn]
  simp only [FinGraph.checkEdgeRank, List.all_eq_true, Bool.or_eq_true, Bool.not_eq_true',
    decide_eq_true_eq]
  constructor
  · intro h u v he
    cases h u (hfin u) v (hfin v) with
    | inl hf => rw [he] at hf; exact absurd hf (by decide)
    | inr hr => exact hr
  · intro h u _ v _
    cases he : g.edge u v with
    | false => exact Or.inl rfl
    | true => exact Or.inr (h u v he)

/-- The linear check is equivalent to the existing pair enumeration on the same
    graph, together with the well-formedness the enumeration does not read. -/
theorem checkRank_iff_checkEdgeRank {t : EdgeTable} :
    t.checkRank = true ↔
      t.wellFormed ∧ t.toFinGraph.checkEdgeRank (fun u => t.rankOf u.val) = true := by
  rw [checkEdgeRank_spec, checkRank_spec]
  have hmem : ∀ (u v : Nat), t.edges.contains (u, v) = true ↔ t.edgeRel u v := by
    intro u v
    simp [edgeRel]
  constructor
  · intro h
    refine ⟨fun u v he => ⟨(h u v he).1, (h u v he).2.1⟩, fun u v he => ?_⟩
    exact (h u.val v.val ((hmem _ _).1 he)).2.2
  · rintro ⟨hr, h⟩ u v he
    obtain ⟨hu, hv⟩ := hr u v he
    exact ⟨hu, hv, h ⟨u, hu⟩ ⟨v, hv⟩ ((hmem u v).2 he)⟩

/-! ### Fast membership and path witnesses

Rule coverage asks whether a pair of nodes is connected in the table.  On large
instances the edges are sorted and looked up by binary search.  Soundness does
not depend on the sort: a lookup succeeds only when it reads the exact edge,
so an unsorted table can only make the check fail. -/

/-- Lexicographic order on pairs, used only to steer the search. -/
def pairLt (a b : Nat × Nat) : Bool :=
  a.1 < b.1 || (a.1 == b.1 && a.2 < b.2)

/-- Binary search for `e` in `arr` between `lo` and `hi`. -/
def searchIn (arr : Array (Nat × Nat)) (e : Nat × Nat) : Nat → Nat → Nat → Bool
  | 0, _, _ => false
  | fuel + 1, lo, hi =>
    if lo < hi then
      let mid := (lo + hi) / 2
      match arr[mid]? with
      | none => false
      | some m =>
        if m == e then true
        else if pairLt m e then searchIn arr e fuel (mid + 1) hi
        else searchIn arr e fuel lo mid
    else false

theorem searchIn_sound {arr : Array (Nat × Nat)} {e : Nat × Nat} :
    ∀ {fuel lo hi}, searchIn arr e fuel lo hi = true → e ∈ arr := by
  intro fuel
  induction fuel with
  | zero => intro lo hi h; simp [searchIn] at h
  | succ fuel ih =>
    intro lo hi h
    simp only [searchIn] at h
    split at h
    · split at h
      · simp at h
      · rename_i m hm
        split at h
        · rename_i heq
          have : m = e := by simpa using heq
          subst this
          exact Array.mem_of_getElem? hm
        · split at h
          · exact ih h
          · exact ih h
    · simp at h

/-- Sorted copy of the edges for the fast lookup. -/
structure Index (t : EdgeTable) where
  arr : Array (Nat × Nat)
  /-- Every indexed pair is an edge of the table. -/
  sound : ∀ e, e ∈ arr → t.edgeRel e.1 e.2

/-- Fast membership. -/
def Index.mem {t : EdgeTable} (ix : Index t) (e : Nat × Nat) : Bool :=
  searchIn ix.arr e 64 0 ix.arr.size

theorem Index.mem_sound {t : EdgeTable} {ix : Index t} {u v : Nat}
    (h : ix.mem (u, v) = true) : t.edgeRel u v :=
  ix.sound (u, v) (searchIn_sound h)

/-- Index built from the table itself: slow to prove for huge tables, so
    generated instances use this only through `checkIndex`. -/
def checkIndex (t : EdgeTable) (arr : Array (Nat × Nat)) : Bool :=
  arr.toList == t.edges

/-- An array equal to the edge list is a valid index. -/
def indexOfCheck {t : EdgeTable} (arr : Array (Nat × Nat)) (h : t.checkIndex arr = true) :
    Index t where
  arr := arr
  sound := by
    intro e he
    have heq : arr.toList = t.edges := by simpa [checkIndex] using h
    show (e.1, e.2) ∈ t.edges
    rw [← heq]
    simpa using he

/-- Every step of an explicit path witness is an edge.  `via` lists the nodes
    between `u` and `v`. -/
def pathOk {t : EdgeTable} (ix : Index t) : Nat → List Nat → Nat → Bool
  | u, [], v => ix.mem (u, v)
  | u, w :: ws, v => ix.mem (u, w) && pathOk ix w ws v

/-- A path from the left, used to build `Path` from a witness. -/
theorem Path.cons {t : EdgeTable} {u v w : Nat} (he : t.edgeRel u v) (p : t.Path v w) :
    t.Path u w := by
  induction p with
  | single h => exact Path.trans (Path.single he) h
  | trans _ h ih => exact Path.trans ih h

/-- Paths compose. -/
theorem Path.comp {t : EdgeTable} {u v w : Nat} (p : t.Path u v) (q : t.Path v w) :
    t.Path u w := by
  induction q with
  | single h => exact Path.trans p h
  | trans _ h ih => exact Path.trans ih h

/-- Fewer edges with the same ranks still pass.  So an edge that no rule needs
    only constrains the rank: removing it leaves every theorem about the table
    true, and keeping it can only cause a rejection. -/
theorem checkRank_mono {t t' : EdgeTable} (hs : t'.size = t.size) (hr : t'.rank = t.rank)
    (he : ∀ e, e ∈ t'.edges → e ∈ t.edges) (h : t.checkRank = true) : t'.checkRank = true := by
  refine checkRank_spec.2 fun u v huv => ?_
  have := checkRank_spec.1 h u v (he (u, v) huv)
  simp only [rankOf, hs, hr]
  exact this

theorem pathOk_sound {t : EdgeTable} {ix : Index t} :
    ∀ {via : List Nat} {u v : Nat}, pathOk ix u via v = true → t.Path u v := by
  intro via
  induction via with
  | nil =>
    intro u v h
    exact Path.single (Index.mem_sound h)
  | cons w ws ih =>
    intro u v h
    simp only [pathOk, Bool.and_eq_true] at h
    exact Path.cons (Index.mem_sound h.1) (ih h.2)

end EdgeTable
