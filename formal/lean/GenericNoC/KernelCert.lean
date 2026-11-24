/-
  GenericNoC.KernelCert: a rank certificate checked by the kernel on large tables.

  `native_decide` trusts the compiler.  This route does not: each chunk of edges
  is a number holding 32-bit fields, checked by one `decide +kernel` theorem, and
  the kernel does big-number arithmetic natively.  Theorems are chained, so no
  term has to hold the whole table.

  `chunkIndex` searches the same chunks for rule coverage, also with big-number
  arithmetic only.  Rule coverage on tables above the kernel limit still uses
  `native_decide`.
-/
import GenericNoC.EdgeTable

namespace EdgeTable.Kernel

/-- Field `i` of a number of 32-bit fields. -/
def fld (x i : Nat) : Nat := (x >>> (i * 32)) % 4294967296

/-- A chunk: the number of edges and the fields holding them. -/
structure Chunk where
  n : Nat
  lit : Nat

/-- Edges in a chunk, from field position `i`. -/
def decode (c : Nat) : Nat → Nat → List (Nat × Nat)
  | 0, _ => []
  | n + 1, i => (fld c (2 * i), fld c (2 * i + 1)) :: decode c n (i + 1)

/-- The check of one chunk. -/
def chunkOk (size : Nat) (rk : Nat → Nat) (c : Nat) : Nat → Nat → Bool
  | 0, _ => true
  | n + 1, i =>
    Nat.blt (fld c (2 * i)) size && Nat.blt (fld c (2 * i + 1)) size &&
      Nat.blt (rk (fld c (2 * i))) (rk (fld c (2 * i + 1))) && chunkOk size rk c n (i + 1)

/-- Every chunk passes. -/
def AllOk (size : Nat) (rk : Nat → Nat) : List Chunk → Prop
  | [] => True
  | c :: cs => chunkOk size rk c.lit c.n 0 = true ∧ AllOk size rk cs

/-- The rank from pages of 4096 fields. -/
def pageAt : List Nat → Nat → Nat
  | [], _ => 0
  | p :: _, 0 => p
  | _ :: ps, k + 1 => pageAt ps k

def rankOfPages (pages : List Nat) (u : Nat) : Nat :=
  fld (pageAt pages (u >>> 12)) (u % 4096)

theorem blt_lt {a b : Nat} (h : Nat.blt a b = true) : a < b := by
  simpa [Nat.blt_eq] using h

theorem chunkOk_sound {size : Nat} {rk : Nat → Nat} {c : Nat} :
    ∀ {n i : Nat}, chunkOk size rk c n i = true → ∀ e, e ∈ decode c n i →
      e.1 < size ∧ e.2 < size ∧ rk e.1 < rk e.2 := by
  intro n
  induction n with
  | zero => intro i _ e he; simp [decode] at he
  | succ n ih =>
    intro i h e he
    simp only [chunkOk, Bool.and_eq_true] at h
    obtain ⟨⟨⟨h1, h2⟩, h3⟩, h4⟩ := h
    simp only [decode, List.mem_cons] at he
    rcases he with rfl | he
    · exact ⟨blt_lt h1, blt_lt h2, blt_lt h3⟩
    · exact ih h4 e he

/-- The edges of a chunk list. -/
def edgesOf : List Chunk → List (Nat × Nat)
  | [] => []
  | c :: cs => decode c.lit c.n 0 ++ edgesOf cs

/-- Chunks that pass give a table that passes the ordinary check. -/
theorem checkRank_of_allOk {size : Nat} {rk : Nat → Nat} :
    ∀ {l : List Chunk}, AllOk size rk l → (EdgeTable.mk size (edgesOf l) rk).checkRank = true := by
  intro l
  induction l with
  | nil => intro _; rfl
  | cons c cs ih =>
    intro h
    have htail := checkRank_spec.1 (ih h.2)
    refine checkRank_spec.2 ?_
    intro u v he
    rcases List.mem_append.1 he with hm | hm
    · exact chunkOk_sound h.1 (u, v) hm
    · exact htail u v hm

/-- Bisection for `e` among the edges `lo` to `hi` of a chunk.  It only accepts
    an edge it reads, so a chunk out of order can only make it fail. -/
def searchChunk (c : Nat) (e : Nat × Nat) : Nat → Nat → Nat → Bool
  | 0, _, _ => false
  | fuel + 1, lo, hi =>
    if lo < hi then
      let mid := (lo + hi) / 2
      let m := (fld c (2 * mid), fld c (2 * mid + 1))
      if m == e then true
      else if pairLt m e then searchChunk c e fuel (mid + 1) hi
      else searchChunk c e fuel lo mid
    else false

theorem mem_decode {c : Nat} : ∀ {n i k : Nat}, k < n →
    (fld c (2 * (i + k)), fld c (2 * (i + k) + 1)) ∈ decode c n i := by
  intro n
  induction n with
  | zero => intro _ _ h; omega
  | succ n ih =>
    intro i k hk
    cases k with
    | zero => simp [decode]
    | succ k =>
      have := ih (i := i + 1) (k := k) (by omega)
      have he : i + 1 + k = i + (k + 1) := by omega
      rw [he] at this
      exact List.mem_cons_of_mem _ this

theorem searchChunk_sound {c n : Nat} {e : Nat × Nat} :
    ∀ {fuel lo hi}, hi ≤ n → searchChunk c e fuel lo hi = true → e ∈ decode c n 0 := by
  intro fuel
  induction fuel with
  | zero => intro lo hi _ h; simp [searchChunk] at h
  | succ fuel ih =>
    intro lo hi hn h
    simp only [searchChunk] at h
    split at h
    · rename_i hlt
      split at h
      · rename_i heq
        have hm := mem_decode (c := c) (i := 0) (k := (lo + hi) / 2) (n := n) (by omega)
        simp only [Nat.zero_add] at hm
        have : (fld c (2 * ((lo + hi) / 2)), fld c (2 * ((lo + hi) / 2) + 1)) = e := by
          simpa using heq
        rwa [this] at hm
      · split at h
        · exact ih hn h
        · exact ih (by omega) h
    · simp at h

/-- Membership in some chunk. -/
def chunksMem : List Chunk → Nat × Nat → Bool
  | [], _ => false
  | c :: cs, e => searchChunk c.lit e 64 0 c.n || chunksMem cs e

theorem chunksMem_sound : ∀ {l : List Chunk} {e : Nat × Nat}, chunksMem l e = true →
    e ∈ edgesOf l := by
  intro l
  induction l with
  | nil => intro e h; simp [chunksMem] at h
  | cons c cs ih =>
    intro e h
    simp only [chunksMem, Bool.or_eq_true] at h
    simp only [edgesOf, List.mem_append]
    rcases h with h | h
    · exact Or.inl (searchChunk_sound (Nat.le_refl _) h)
    · exact Or.inr (ih h)

/-- The index of a table made of chunks: no list is built or compared. -/
def chunkIndex (size : Nat) (rk : Nat → Nat) (l : List Chunk) :
    Index (EdgeTable.mk size (edgesOf l) rk) where
  mem := chunksMem l
  sound _ h := chunksMem_sound h

end EdgeTable.Kernel
