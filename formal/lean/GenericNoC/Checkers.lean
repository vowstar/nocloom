/-
  GenericNoC.Checkers: decidable finite checks on explicit data: a reachability
  fixpoint, a cycle criterion, a rank check over all node pairs, and binding
  checks for unique ownership and instance fingerprints.

  Theorems use only the certificate direction: a passing rank excludes a
  positive-length path from a node to itself (`GenericNoC.Reaches.no_self`).
  Completeness of the fixpoint is not proved, so `hasCycle` is an engineering
  check.  `EdgeTable.lean` has the linear version for large instances.
-/

/-- A finite directed graph on `Fin size` with a boolean edge function. -/
structure FinGraph where
  size : Nat
  edge : Fin size → Fin size → Bool

namespace FinGraph

/-- R₁(u): immediate successors. -/
def successors (g : FinGraph) (u : Fin g.size) : List (Fin g.size) :=
  (List.finRange g.size).filter (fun v => g.edge u v)

/-- One expansion step: R ∪ ⋃_{w ∈ R} R₁(w). -/
def expand (g : FinGraph) (r : List (Fin g.size)) : List (Fin g.size) :=
  (r ++ r.flatMap g.successors).eraseDups

/-- R_{|V|}(u): the iterated expansion stabilizes within |V| steps, giving the positive-length reachability set. -/
def reach (g : FinGraph) (u : Fin g.size) : List (Fin g.size) :=
  go g.size (g.successors u)
where
  go : Nat → List (Fin g.size) → List (Fin g.size)
    | 0, r => r
    | k + 1, r => go k (g.expand r)

/-- Some node reaches itself in positive length. -/
def hasCycle (g : FinGraph) : Bool :=
  (List.finRange g.size).any (fun u => (g.reach u).contains u)

/-- Certificate check: the rank strictly increases along every edge
    (blocker → blocked). -/
def checkEdgeRank (g : FinGraph) (rank : Fin g.size → Nat) : Bool :=
  (List.finRange g.size).all fun u =>
    (List.finRange g.size).all fun v =>
      !(g.edge u v) || decide (rank u < rank v)

end FinGraph

/-- Every hold point is owned by exactly one component
    (each identity occurs exactly once across the per-component lists). -/
def uniqueOwnership [BEq α] (owners : List (List α)) : Bool :=
  let all := owners.flatten
  all.all fun h => all.count h == 1

/-- Two-document binding: identical instance sets with no duplicates
    and identical (implementation, parameter) fingerprint pairs; witnesses
    must not carry extra instances. -/
def docBinding [BEq α] [BEq β] (topology witnesses : List (α × β × β)) : Bool :=
  topology.length == witnesses.length &&
  (topology.map Prod.fst).all (fun i => (topology.map Prod.fst).count i == 1) &&
  topology.all (fun e => witnesses.contains e) &&
  witnesses.all (fun e => topology.contains e)

/-- Two-node chain: node 0 blocks node 1 (blocker → blocked). -/
abbrev chainGraph : FinGraph := ⟨2, fun u v => u.val == 0 && v.val == 1⟩

/-- Three-node ring 0 → 1 → 2 → 0 (negative control). -/
abbrev ringGraph : FinGraph := ⟨3, fun u v => v.val == (u.val + 1) % 3⟩

/-- Ring edge relation at the Prop level (for the impossibility proof). -/
abbrev ringEdge (u v : Fin 3) : Prop := v.val = (u.val + 1) % 3

-- Chain: acyclic, and the identity rank is a valid certificate.
example : chainGraph.hasCycle = false := by native_decide
example : chainGraph.checkEdgeRank Fin.val = true := by native_decide
example : (chainGraph.reach 0).contains 0 = false := by native_decide

-- Ring: the fixpoint cycle criterion fires.
example : ringGraph.hasCycle = true := by native_decide
example : (ringGraph.reach 0).contains 0 = true := by native_decide

-- Ring: candidate certificates fail the per-edge check …
example : ringGraph.checkEdgeRank Fin.val = false := by native_decide
example : ringGraph.checkEdgeRank (fun _ => 0) = false := by native_decide

-- … and no rank certificate can exist at all: the ring would force
-- ψ 0 < ψ 1 < ψ 2 < ψ 0.  (Negative control, certificate impossibility.)
theorem ring_no_rank (ψ : Fin 3 → Nat) (h : ∀ u v, ringEdge u v → ψ u < ψ v) :
    False := by
  have h01 : ψ 0 < ψ 1 := h 0 1 (by decide)
  have h12 : ψ 1 < ψ 2 := h 1 2 (by decide)
  have h20 : ψ 2 < ψ 0 := h 2 0 (by decide)
  omega

-- Two-document binding and unique ownership.
example : uniqueOwnership [[1, 2], [3]] = true := by native_decide
example : uniqueOwnership [[1, 2], [2, 3]] = false := by native_decide
example : docBinding [(1, 100, 200), (2, 101, 201)] [(2, 101, 201), (1, 100, 200)] = true := by
  native_decide
example : docBinding [(1, 100, 200)] [(1, 100, 201)] = false := by native_decide
example : docBinding [(1, 100, 200)] [(1, 100, 200), (2, 0, 0)] = false := by native_decide
example : docBinding [(1, 100, 200), (1, 100, 200)] [(1, 100, 200), (1, 100, 200)] = false := by
  native_decide
