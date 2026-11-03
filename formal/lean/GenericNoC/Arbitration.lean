import Std.Tactic

namespace GenericNoC.Arbitration

/-- A balanced tree combines candidate ranks with an associative minimum. -/
inductive Search where
  | leaf (rank : Nat)
  | branch (left right : Search)

def minimum : Search → Nat
  | .leaf rank => rank
  | .branch left right => min (minimum left) (minimum right)

def ranks : Search → List Nat
  | .leaf rank => [rank]
  | .branch left right => ranks left ++ ranks right

/-- Tree shape cannot change the minimum eligible rank. Invalid leaves use the sentinel n. -/
theorem minimum_le (tree : Search) (rank : Nat) (member : rank ∈ ranks tree) :
    minimum tree ≤ rank := by
  induction tree with
  | leaf value =>
    simp only [ranks, List.mem_singleton] at member
    subst rank
    exact Nat.le_refl _
  | branch left right ihl ihr =>
    simp only [ranks, List.mem_append] at member
    rcases member with hl | hr
    · exact Nat.le_trans (Nat.min_le_left _ _) (ihl hl)
    · exact Nat.le_trans (Nat.min_le_right _ _) (ihr hr)

theorem minimum_member (tree : Search) : minimum tree ∈ ranks tree := by
  induction tree with
  | leaf value => simp [minimum, ranks]
  | branch left right ihl ihr =>
    simp only [minimum, ranks, List.mem_append]
    by_cases h : minimum left ≤ minimum right
    · exact Or.inl (by simpa [Nat.min_eq_left h] using ihl)
    · exact Or.inr (by simpa [Nat.min_eq_right (Nat.le_of_not_ge h)] using ihr)

/-- Circular rank measured from the scan cursor. -/
def distance (n cursor index : Nat) : Nat :=
  if cursor ≤ index then index - cursor else n - cursor + index

def successor (n winner : Nat) : Nat := if winner + 1 < n then winner + 1 else 0

theorem distance_bound {n cursor index : Nat} (hc : cursor < n) (hi : index < n) :
    distance n cursor index < n := by
  unfold distance
  split <;> omega

/-- Each competing release reduces the persistent eligible candidate's rank.
    A held packet at arrival is accounted separately. -/
theorem release_decreases {n cursor winner waiting : Nat}
    (hc : cursor < n) (hw : winner < n) (ht : waiting < n) (hne : winner ≠ waiting)
    (selected : distance n cursor winner ≤ distance n cursor waiting) :
    distance n (successor n winner) waiting < distance n cursor waiting := by
  unfold distance successor at *
  split <;> split at selected <;> split at selected <;>
    split <;> omega

/-- A strictly decreasing rank below n permits at most n - 1 competing releases. -/
theorem release_count_bound (rank : Nat → Nat) {n releases : Nat}
    (initial : rank 0 < n) (decreases : ∀ k, k < releases → rank (k + 1) < rank k) :
    releases ≤ n - 1 := by
  have count : ∀ k, k ≤ releases → rank k + k ≤ rank 0 := by
    intro k
    induction k with
    | zero => simp
    | succ k ih =>
      intro hk
      have prior := ih (by omega)
      have step := decreases k (by omega)
      omega
  have final := count releases (by omega)
  omega
end GenericNoC.Arbitration
