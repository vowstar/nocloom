import GenericNoC.WormholeDone

namespace Wormhole

/-- No words, held outputs or live transactions remain. Used tokens can stay
    recorded after the network drains. -/
structure Quiescent (σ : St) : Prop where
  empty : ∀ p, σ.q p = []
  free : ∀ o, σ.grant o = none
  idle : ∀ k, σ.live k = false

/-- A finite legal path from a state to quiescence. -/
def FiniteDrain (c : Config) (σ : St) : Prop :=
  ∃ (f : Nat → St) (len : Nat), f 0 = σ ∧
    (∀ n, n < len → Step c (f n) (f (n + 1))) ∧ Quiescent (f len)

/-- A run that stays quiescent after a finite prefix satisfies all six clauses. -/
theorem fair_of_eventually_quiescent {c : Config} {s : Nat → St} {a : Nat}
    (poolPos : ∀ o d r, c.pool o d = some r → 0 < c.poolCap r)
    (quiet : ∀ n, a ≤ n → Quiescent (s n)) : Fair c s := by
  have atMax (τ : Nat) := quiet (max a τ) (Nat.le_max_left _ _)
  constructor
  · intro τ p d o _ hhead _
    obtain ⟨w, hw, _⟩ := hhead (max a τ) (Nat.le_max_right _ _)
    simp [(atMax τ).empty p] at hw
  · intro τ p d o _ hhold _
    have hg := (hhold (max a τ) (Nat.le_max_right _ _)).1
    simp [(atMax τ).free o] at hg
  · intro τ o p hhold
    have hg := hhold (max a τ) (Nat.le_max_right _ _)
    simp [(atMax τ).free o] at hg
  · intro τ o p _ hhold
    have hg := (hhold (max a τ) (Nat.le_max_right _ _)).1
    simp [(atMax τ).free o] at hg
  · intro τ o d r hn hp hfull
    have hfilter : ((s (max a τ)).holders.filter
        (fun k => (s (max a τ)).live k && (s (max a τ)).home k == some r)) = [] := by
      apply List.filter_eq_nil_iff.2
      intro k _
      simp [(atMax τ).idle k]
    have hzero : poolUse (s (max a τ)) r = 0 := by simp [poolUse, hfilter]
    exfalso
    exact (hfull (max a τ) (Nat.le_max_right _ _)) (by
      simpa only [room, hn, hp, hzero] using poolPos o d r hp)
  · intro τ k hstall
    have hw := (hstall (max a τ) (Nat.le_max_right _ _)).1
    simp [own, waiting, (atMax τ).idle k] at hw

/-- A finite drain from each reachable state supplies a fair continuation.
    Follow a legal prefix through the state, take its drain, then stutter. -/
theorem serviceContinuation_of_finiteDrain {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (hrank : t.checkRank = true)
    (poolPos : ∀ o d r, c.pool o d = some r → 0 < c.poolCap r)
    (drain : ∀ σ, (noc c t node (Fair c) hrank).Reachable σ → FiniteDrain c σ) :
    GenericNoC.ServiceContinuation (noc c t node (Fair c) hrank) where
  extend := by
    intro σ hσ
    obtain ⟨f, len, hf0, hfstep, hfquiet⟩ := drain σ hσ
    obtain ⟨s, hs, j, hj⟩ := hσ
    let stopped : St := { f len with ev := none }
    let g (n : Nat) : St :=
      if n ≤ j then s n else if n ≤ j + len then f (n - j) else stopped
    have hat : g j = σ := by simpa [g] using hj
    have hstop (n : Nat) (hn : j + len < n) : g n = stopped := by
      simp [g, show ¬ n ≤ j by omega, show ¬ n ≤ j + len by omega]
    have hmid (n : Nat) (h₁ : j ≤ n) (h₂ : n ≤ j + len) : g n = f (n - j) := by
      by_cases h : n ≤ j
      · have : n = j := by omega
        subst n
        simpa using hat.trans hf0.symm
      · simp [g, h, h₂]
    have hg : (noc c t node (Fair c) hrank).Legal g := by
      refine ⟨?_, ?_⟩
      · simpa [g] using hs.1
      · intro n
        change Step c (g n) (g (n + 1))
        if h : n < j then
          simpa [g, Nat.le_of_lt h, show n + 1 ≤ j by omega] using hs.2 n
        else if hn : n < j + len then
          rw [hmid n (by omega) (by omega), hmid (n + 1) (by omega) (by omega),
            show n + 1 - j = n - j + 1 by omega]
          exact hfstep _ (by omega)
        else if hn' : n = j + len then
          rw [hmid n (by omega) (by omega), hstop (n + 1) (by omega),
            show n - j = len by omega]
          exact Step.stutter _
        else
          rw [hstop n (by omega), hstop (n + 1) (by omega)]
          exact Step.stutter stopped
    have hfair (i : Nat) : Fair c (fun n => g (n + i)) := by
      apply fair_of_eventually_quiescent (a := j + len + 1) poolPos
      intro n hn
      rw [hstop (n + i) (by omega)]
      exact ⟨hfquiet.empty, hfquiet.free, hfquiet.idle⟩
    refine ⟨g, j, hg, ?_, hat, hfair⟩
    simpa using hfair 0

/-- With a drain from each reachable state, checked tables give every pending
    transaction a fair execution on which one of its hold points advances. -/
theorem checked_deadlock_free_of_finiteDrain {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r) (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node) (hheads : headsInRange c t node = true)
    (poolPos : ∀ o d r, c.pool o d = some r → 0 < c.poolCap r)
    (drain : ∀ σ, (noc c t node (Fair c) hrank).Reachable σ → FiniteDrain c σ) :
    (noc c t node (Fair c) hrank).DeadlockFree (liveWork hrank (Fair c)) :=
  GenericNoC.deadlock_free _
    (structural hwf hrank hcov hheads)
    (instanceLemmas (wfCheck_sound hwf) (hopFacts hhops) hcap hrank)
    (serviceContinuation_of_finiteDrain hrank poolPos drain) _
    (liveWork_held hrank (Fair c))

/-- One word remains in the budget, but the unfinished request has not
    reached its tail. -/
private structure ShortRequest (c : Config) (σ : St) (k src dest : Nat) : Prop where
  live : σ.live k = true
  budget : σ.budget k = 1
  source : σ.src k = some src
  awaiting : σ.reqDone k = false
  home : ∀ p, σ.home k = some p → isResponse c p = true
  words : ∀ p w, w ∈ σ.q p → w.tok = k → w.dest = dest ∧ w.tail = false ∧ w.last = false

private theorem pool_isResponse {c : Config} {o d p : Nat} (hp : c.pool o d = some p) :
    isResponse c p = true := by
  obtain ⟨e, he, rfl⟩ := Option.map_eq_some_iff.1 hp
  exact List.any_eq_true.2 ⟨e, List.mem_of_find?_eq_some he, by simp⟩

private theorem ShortRequest.step {c : Config} {σ σ' : St} {k src dest : Nat}
    (h : ShortRequest c σ k src dest)
    (noFree : ∀ d, ¬ EndsFree c src d)
    (noCut : ∀ p, c.ahead p dest = false)
    (hs : Step c σ σ') : ShortRequest c σ' k src dest := by
  cases hs with
  | stutter => exact ⟨h.live, h.budget, h.source, h.awaiting, h.home, h.words⟩
  | arb => exact ⟨h.live, h.budget, h.source, h.awaiting, h.home, h.words⟩
  | cut p w _ hh ha _ =>
    have hne : w.tok ≠ k := by
      intro hk
      have hd := (h.words p w (List.mem_of_head? hh) hk).1
      rw [hd, noCut p] at ha
      exact Bool.noConfusion ha
    refine ⟨h.live, h.budget, h.source, h.awaiting, h.home, ?_⟩
    intro q a hm hk
    change a ∈ upd σ.q p ({w with tail := true} :: (σ.q p).tail) q at hm
    by_cases hq : q = p
    · subst q
      rw [upd_same, List.mem_cons] at hm
      rcases hm with rfl | hm
      · exact (hne hk).elim
      · exact h.words p a (List.mem_of_mem_tail hm) hk
    · rw [upd_ne _ _ hq] at hm
      exact h.words q a hm hk
  | inject p w b _ _ _ _ _ _ whereWord resp _ last final _ =>
    have hne : w.tok ≠ k := by
      intro hk
      have hlive : σ.live w.tok = true := hk.symm ▸ h.live
      rcases whereWord hlive with ⟨hsrc, _⟩ | hhome
      · rw [hk, h.source] at hsrc
        cases hsrc
        have hlast : w.last = true := last.2 (by simp [newBudget, hk, h.live, h.budget])
        exact noFree w.dest (final hlast).2
      · have hdone := (resp (h.home p (by simpa only [hk] using hhome))).2.2.1
        simp [hk, h.awaiting] at hdone
    refine ⟨?_, ?_, ?_, h.awaiting, h.home, ?_⟩
    · simpa [injected, upd, Ne.symm hne] using h.live
    · simpa [injected, upd, Ne.symm hne] using h.budget
    · unfold injected
      split <;> simpa [upd, Ne.symm hne] using h.source
    · intro q a hm hk
      change a ∈ upd σ.q p (σ.q p ++ [w]) q at hm
      by_cases hq : q = p
      · subst q
        rw [upd_same, List.mem_append, List.mem_singleton] at hm
        rcases hm with hm | rfl
        · exact h.words p a hm hk
        · exact (hne hk).elim
      · rw [upd_ne _ _ hq] at hm
        exact h.words q a hm hk
  | fire p o w hh _ _ _ hloop _ =>
    have hflags (hk : w.tok = k) :=
      (h.words p w (List.mem_of_head? hh) hk).2
    have lastNe (hl : w.last = true) : k ≠ w.tok :=
      fun hk => by simp [(hflags hk.symm).2] at hl
    have tailNe (ht : w.tail = true) : k ≠ w.tok :=
      fun hk => by simp [(hflags hk.symm).1] at ht
    refine ⟨?_, ?_, ?_, ?_, ?_, ?_⟩
    · unfold place
      split
      · exact h.live
      · split
        · exact h.live
        · by_cases hl : w.last = true
          · simpa [pop, hl, upd_ne _ _ (lastNe hl)] using h.live
          · simpa [pop, hl] using h.live
    · unfold place
      split
      · exact h.budget
      · split <;> exact h.budget
    · unfold place
      split
      · exact h.source
      · split <;> exact h.source
    · unfold place
      split
      · exact h.awaiting
      · split
        · by_cases ht : w.tail = true
          · simpa [pop, ht, upd_ne _ _ (tailNe ht)] using h.awaiting
          · simpa [pop, ht] using h.awaiting
        · exact h.awaiting
    · intro q hq
      unfold place at hq
      split at hq
      · exact h.home q hq
      · split at hq
        · rename_i r hp
          by_cases hk : k = w.tok
          · by_cases hd : σ.reqDone w.tok = true
            · exact h.home q (by simpa [pop, hd] using hq)
            · have : r = q := by simpa [pop, hd, upd, hk] using hq
              subst q
              exact pool_isResponse hp
          · cases hd : σ.reqDone w.tok <;> exact h.home q (by simpa [pop, upd, hk, hd] using hq)
        · apply h.home q
          by_cases hl : w.last = true
          · simpa [pop, hl, upd_ne _ _ (lastNe hl)] using hq
          · simpa [pop, hl] using hq
    · intro q a hm hk
      rw [fire_q hloop] at hm
      split at hm
      · rw [List.mem_append, List.mem_singleton] at hm
        rcases hm with hm | rfl
        · exact h.words q a hm hk
        · exact h.words p a (List.mem_of_head? hh) hk
      · split at hm
        · rename_i hq
          subst q
          exact h.words p a (List.mem_of_mem_tail hm) hk
        · exact h.words q a hm hk

/-- A checked instance cannot satisfy service continuation if it accepts an
    unterminated request with exactly one word left in its declared count.
    The source has no route to a free sink, the request cannot be cut, and a
    response cannot enter the network until the request ends. -/
theorem no_serviceContinuation_of_short_request
    {c : Config} {t : EdgeTable} {node : Hold → Nat}
    (hwf : wfCheck c = true) (hcap : ∀ r, 0 < c.cap r) (hhops : hopsOk c = true)
    (hrank : t.checkRank = true) (hcov : Covered c t node) (hheads : headsInRange c t node = true)
    {src : Nat} {w : Word} (htail : w.tail = false) (hlast : w.last = false)
    (admit : Step c St.empty (injected St.empty src w 1))
    (noFree : ∀ d, ¬ EndsFree c src d) (noCut : ∀ p, c.ahead p w.dest = false) :
    ¬ GenericNoC.ServiceContinuation (noc c t node (Fair c) hrank) := by
  let σ := injected St.empty src w 1
  let f : Nat → St
    | 0 => St.empty
    | _ + 1 => σ
  have reach : (noc c t node (Fair c) hrank).Reachable σ := by
    refine ⟨f, ⟨rfl, ?_⟩, 1, rfl⟩
    intro n
    cases n with
    | zero => exact admit
    | succ n => exact Step.stutter σ
  have short : ShortRequest c σ w.tok src w.dest := by
    refine ⟨by simp [σ, injected], by simp [σ, injected, newBudget, St.empty],
      by simp [σ, injected, St.empty], rfl, by simp [σ, injected, St.empty], ?_⟩
    intro p a hm _
    have : a = w := by
      by_cases hp : p = src
      · subst p
        simpa [σ, injected, St.empty] using hm
      · simp [σ, injected, St.empty, upd_ne, hp] at hm
    subst a
    exact ⟨rfl, htail, hlast⟩
  intro service
  obtain ⟨g, j, hg, fair, hgj, _⟩ := service.extend σ reach
  have stillShort (n : Nat) : ShortRequest c (g (j + n)) w.tok src w.dest := by
    induction n with
    | zero => simpa [hgj] using short
    | succ n ih => exact ih.step noFree noCut (hg.2 (j + n))
  obtain ⟨u, hu, ended⟩ :=
    checked_transaction_completes hwf hcap hhops hrank hcov hheads hg.1 hg.2 fair j w.tok
  have alive := (stillShort (u - j)).live
  rw [Nat.add_sub_of_le hu] at alive
  simp [alive] at ended

end Wormhole
