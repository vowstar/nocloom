/-
  GenericNoC.Instances.MultiHop: hold-and-wait across hops.

  Slots 0 and 1 are targets.  Slots 2 to 5 are the links behind two nodes.
  Destinations 0 and 1 are two sources.  Each target holds a last-hop output for
  one read while that read's next word is behind a word for the other source, and
  that word waits for the other last hop.

  The holders of the last hops are empty.  What they wait for is upstream, so a
  rule that looks only at the holder's own slot misses this hang.  With the
  upstream rule, `unprovable` shows W2 fails for every graph and rank.  The run is
  `Fair`, so `Fair` does not hide it, and from its dead state nothing can move.
-/
import GenericNoC.WormholeDone
import GenericNoC.WormholeFast
open Wormhole GenericNoC
namespace Wormhole.MultiHop

def cfg : Config where
  routes := [(0,0,0),(0,1,1),(1,0,2),(1,1,3),(2,0,4),(3,1,5),(4,0,4),(5,1,5)]
  nexts := [(0,2),(1,3),(2,4),(3,5)]
  pools := []
  cap := fun r => if r < 2 then 2 else 1
  poolCap := fun _ => 0
  endpoints := [0,1]
  mayHold := fun _ _ => true
  contig := fun _ => false
  cross := fun _ => true
  bound := fun _ => false
  slots := [0,1,2,3,4,5]
  hops := fun p _ => if p < 2 then 2 else 1
  hopMax := 3
  ups := fun p => match p with
    | 2 => [2, 0]
    | 3 => [3, 0]
    | 4 => [4, 1]
    | 5 => [5, 1]
    | p => [p]
  answers := fun _ _ => false
  ahead := fun _ _ => false

/- nodes: heads and grants -/
def nodeOf : Hold → Nat
  | .head 0 0 => 0 | .head 0 1 => 1 | .head 1 0 => 2 | .head 1 1 => 3
  | .head 2 0 => 4 | .head 3 1 => 5 | .head 4 0 => 6 | .head 5 1 => 7
  | .grant 0 0 => 8 | .grant 1 0 => 9 | .grant 2 1 => 10 | .grant 3 1 => 11
  | .grant 4 2 => 12 | .grant 4 4 => 13 | .grant 5 3 => 14 | .grant 5 5 => 15
  | _ => 0

/-- A table with the same-slot hold edges only. -/
def edges : List (Nat × Nat) :=
  -- sorted: arbitration at the last hop (12..15 -> 4..7), next word and next grant
  -- (4..7 -> 0..3, 8..11), hold across in the same slot (0..3 -> 8..11)
  [ (0,9),(1,8),(2,11),(3,10),(4,0),(4,8),(5,1),(5,9),(6,2),(6,10),(7,3),(7,11),
    (12,6),(13,4),(14,7),(15,5) ]
def rank : Array Nat := #[2,2,2,2,1,1,1,1,3,3,3,3,0,0,0,0]
def table : EdgeTable := ⟨16, edges, EdgeTable.rankArr rank⟩

theorem h_rank : table.checkRank = true := by decide +kernel
theorem ix_ok : table.checkIndex edges.toArray = true := by decide +kernel
def ix : EdgeTable.Index table := EdgeTable.indexOfCheck edges.toArray ix_ok
/-- The table with only same-slot hold edges does not cover the rule. -/
theorem same_slot_table_rejected : checkRule cfg ix #[] nodeOf .holdAcross = false := by
  decide +kernel
theorem h_wf : wfCheck cfg = true := by decide +kernel
theorem h_hops : hopsOk cfg = true := by decide +kernel
theorem h_heads : headsInRange cfg table nodeOf = true := by decide +kernel
theorem h_cap : ∀ r, 0 < cfg.cap r := by
  intro r; simp only [cfg]; split <;> decide

/-! The run: reads hold the last-hop grant, writes' responses fill the other links. -/
def R1 : Word := ⟨0,false,false,1⟩   -- t0 read word for s0, keeps its grants
def R1' : Word := ⟨0,true,true,1⟩    -- its last word, behind the responses
def R2 : Word := ⟨1,false,false,2⟩
def R2' : Word := ⟨1,true,true,2⟩
def B1 : Word := ⟨1,true,true,3⟩     -- t0: response for s1
def B3 : Word := ⟨0,true,true,4⟩     -- t1: response for s0
def B2 : Word := ⟨1,true,true,5⟩     -- second response at t0
def B4 : Word := ⟨0,true,true,6⟩     -- second response at t1

def add (σ : St) (p : Nat) (w : Word) (b : Nat) : St := injected σ p w b
def take (σ : St) (p o : Nat) : St := { σ with grant := upd σ.grant o (some p), ev := none }
def pass (σ : St) (p o : Nat) (w : Word) : St := place cfg (pop σ p o w) o w

def a0 := add St.empty 0 R1 1
def a1 := take a0 0 0
def a2 := pass a1 0 0 R1
def a3 := take a2 2 4
def a4 := pass a3 2 4 R1
def b0 := add a4 1 R2 1
def b1 := take b0 1 3
def b2 := pass b1 1 3 R2
def b3 := take b2 5 5
def b4 := pass b3 5 5 R2
def c0 := add b4 0 B1 0
def c1 := take c0 0 1
def c2 := pass c1 0 1 B1
def d0 := add c2 1 B3 0
def d1 := take d0 1 2
def d2 := pass d1 1 2 B3
def e0 := add d2 0 B2 0
def e1 := take e0 0 1
def f0 := add e1 1 B4 0
def f1 := take f0 1 2
def g0 := add f1 0 R1' 0
def g1 := add g0 1 R2' 0
def dead : St := { g1 with ev := none }

def run : Nat → St
  | 0 => St.empty | 1 => a0 | 2 => a1 | 3 => a2 | 4 => a3 | 5 => a4
  | 6 => b0 | 7 => b1 | 8 => b2 | 9 => b3 | 10 => b4
  | 11 => c0 | 12 => c1 | 13 => c2 | 14 => d0 | 15 => d1 | 16 => d2
  | 17 => e0 | 18 => e1 | 19 => f0 | 20 => f1 | 21 => g0 | 22 => g1
  | _ => dead

theorem ends0 : EndsFree cfg 0 0 := .step (o:=0) (r:=2) rfl rfl (.sink (o:=4) rfl rfl rfl)
theorem ends1 : EndsFree cfg 0 1 := .step (o:=1) (r:=3) rfl rfl (.sink (o:=5) rfl rfl rfl)
theorem ends2 : EndsFree cfg 1 0 := .step (o:=2) (r:=4) rfl rfl (.sink (o:=4) rfl rfl rfl)
theorem ends3 : EndsFree cfg 1 1 := .step (o:=3) (r:=5) rfl rfl (.sink (o:=5) rfl rfl rfl)

theorem nb : ∀ {P : Prop} {d : Nat}, cfg.bound d = true → P := fun h => absurd h (by simp [cfg])
theorem nc : ∀ {P : Prop} {p : Nat}, cfg.contig p = true → P := fun h => absurd h (by simp [cfg])

theorem run_steps (τ : Nat) : Step cfg (run τ) (run (τ + 1)) := by
  match τ with
  | 0 => exact (Step.inject St.empty 0 R1 1 (by decide) (by decide) (Or.inr (Or.inl rfl)) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (by simp [R1]) nb)
  | 1 => exact (Step.arb a0 0 0 R1 rfl rfl rfl)
  | 2 => exact (Step.fire a1 0 0 R1 rfl rfl rfl (Or.inl (show (a1.q 2).length < cfg.cap 2 by decide)) (by decide) (fun h => absurd h (by decide)))
  | 3 => exact (Step.arb a2 2 4 R1 rfl rfl rfl)
  | 4 => exact (Step.fire a3 2 4 R1 rfl rfl rfl (Or.inl trivial) (by decide) (fun h => absurd h (by decide)))
  | 5 => exact (Step.inject a4 1 R2 1 (by decide) (by decide) (Or.inr (Or.inl rfl)) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (by simp [R2]) nb)
  | 6 => exact (Step.arb b0 1 3 R2 rfl rfl rfl)
  | 7 => exact (Step.fire b1 1 3 R2 rfl rfl rfl (Or.inl (show (b1.q 5).length < cfg.cap 5 by decide)) (by decide) (fun h => absurd h (by decide)))
  | 8 => exact (Step.arb b2 5 5 R2 rfl rfl rfl)
  | 9 => exact (Step.fire b3 5 5 R2 rfl rfl rfl (Or.inl trivial) (by decide) (fun h => absurd h (by decide)))
  | 10 => exact (Step.inject b4 0 B1 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends1⟩) nb)
  | 11 => exact (Step.arb c0 0 1 B1 rfl rfl rfl)
  | 12 => exact (Step.fire c1 0 1 B1 rfl rfl rfl (Or.inl (show (c1.q 3).length < cfg.cap 3 by decide)) (by decide) (fun h => absurd h (by decide)))
  | 13 => exact (Step.inject c2 1 B3 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends2⟩) nb)
  | 14 => exact (Step.arb d0 1 2 B3 rfl rfl rfl)
  | 15 => exact (Step.fire d1 1 2 B3 rfl rfl rfl (Or.inl (show (d1.q 4).length < cfg.cap 4 by decide)) (by decide) (fun h => absurd h (by decide)))
  | 16 => exact (Step.inject d2 0 B2 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends1⟩) nb)
  | 17 => exact (Step.arb e0 0 1 B2 rfl rfl rfl)
  | 18 => exact (Step.inject e1 1 B4 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends2⟩) nb)
  | 19 => exact (Step.arb f0 1 2 B4 rfl rfl rfl)
  | 20 => exact (Step.inject f1 0 R1' 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends0⟩) nb)
  | 21 => exact (Step.inject g0 1 R2' 0 (by decide) (by decide) (Or.inl rfl) nc rfl (by decide) (by decide) (by decide)
      (by decide) (by decide) (fun _ => ⟨rfl, ends3⟩) nb)
  | 22 => exact (Step.stutter g1)
  | n + 23 => exact (Step.stutter dead)

theorem noroom1 : ¬ room cfg dead 1 1 := fun h => absurd (show (dead.q 3).length < cfg.cap 3 from h) (by decide)
theorem noroom2 : ¬ room cfg dead 2 0 := fun h => absurd (show (dead.q 4).length < cfg.cap 4 from h) (by decide)

theorem noroom1' {d : Nat} : ¬ room cfg dead 1 d :=
  fun h => absurd (show (dead.q 3).length < cfg.cap 3 from h) (by decide)
theorem noroom2' {d : Nat} : ¬ room cfg dead 2 d :=
  fun h => absurd (show (dead.q 4).length < cfg.cap 4 from h) (by decide)

theorem noFit1 (w : Word) : ¬ fits cfg dead 1 w := by
  rintro (h | ⟨h, _⟩)
  · exact noroom1' h
  · exact absurd h (by decide)

theorem noFit2 (w : Word) : ¬ fits cfg dead 2 w := by
  rintro (h | ⟨h, _⟩)
  · exact noroom2' h
  · exact absurd h (by decide)

theorem run_late {t : Nat} (h : 23 ≤ t) : run t = dead := by
  match t, h with
  | n + 23, _ => rfl

/-! What the dead state looks like. -/
example : dead.grant 4 = some 2 := rfl
example : dead.grant 5 = some 5 := rfl
example : dead.q 2 = [] := rfl
example : dead.q 5 = [] := rfl
example : dead.q 3 = [B1] := rfl
example : dead.q 4 = [B3] := rfl
example : dead.q 0 = [B2, R1'] := rfl
example : dead.q 1 = [B4, R2'] := rfl
example : dead.live 1 = true := rfl
example : dead.live 2 = true := rfl

/-- The state is really dead: the only step the model allows is a stutter.
    No environment action exists: endpoints are full, every output is taken,
    and no holder has both a head for its output and room. -/
theorem dead_is_dead (σ' : St) (h : Step cfg dead σ') : σ' = { dead with ev := none } := by
  cases h with
  | stutter => rfl
  | cut p w _ _ hahead _ => simp [cfg] at hahead
  | arb p o w hh hr hf =>
    exfalso
    have hm := Config.route_mem hr
    simp only [cfg, List.mem_cons, Prod.mk.injEq, List.not_mem_nil, or_false] at hm
    have : o = 0 ∨ o = 1 ∨ o = 2 ∨ o = 3 ∨ o = 4 ∨ o = 5 := by omega
    rcases this with rfl | rfl | rfl | rfl | rfl | rfl <;> exact absurd hf (by decide)
  | fire p o w hh hr hg hroom hloop =>
    exfalso
    have hm := Config.route_mem hr
    simp only [cfg, List.mem_cons, Prod.mk.injEq, List.not_mem_nil, or_false] at hm
    rcases hm with ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩ |
        ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩ | ⟨rfl, hd, rfl⟩
    · have : dead.q 0 = [B2, R1'] := rfl
      rw [this] at hh; simp at hh; subst hh; simp [B2] at hd
    · exact noFit1 _ hroom
    · exact noFit2 _ hroom
    · have : dead.q 1 = [B4, R2'] := rfl
      rw [this] at hh; simp at hh; subst hh; simp [B4] at hd
    · have : dead.q 2 = [] := rfl
      rw [this] at hh; simp at hh
    · exact absurd hg (by decide)
    · exact absurd hg (by decide)
    · have : dead.q 5 = [] := rfl
      rw [this] at hh; simp at hh
  | inject p w b he hcap _ _ _ _ _ _ _ _ =>
    exfalso
    simp only [cfg, List.mem_cons, List.not_mem_nil, or_false] at he
    rcases he with rfl | rfl <;> exact absurd hcap (by decide)

/-- These clauses of `Fair` hold on this run. -/
theorem five_clauses :
    (∀ τ p d o, cfg.route p d = some o → (∀ t, τ ≤ t → headIs (run t) p d) →
      (∀ a, τ ≤ a → ∃ t, a ≤ t ∧ (run t).grant o = none) → ∃ t, τ ≤ t ∧ (run t).grant o = some p) ∧
    (∀ τ p d o, cfg.route p d = some o →
      (∀ t, τ ≤ t → (run t).grant o = some p ∧ headIs (run t) p d) →
      (∀ a, τ ≤ a → ∃ t, a ≤ t ∧ fitsHead cfg (run t) p o) → ∃ t, τ ≤ t ∧ (run (t + 1)).ev = some (p, o)) ∧
    (∀ τ o p, (∀ t, τ ≤ t → (run t).grant o = some p) →
      ∃ a, τ ≤ a ∧ ∀ t, a ≤ t → (run (t + 1)).ev ≠ some (p, o)) ∧
    (∀ τ o d r, cfg.next o = none → cfg.pool o d = some r →
      (∀ t, τ ≤ t → ¬ room cfg (run t) o d) → ∃ t d', τ ≤ t ∧ StallAt run t (.head r d')) ∧
    (∀ τ k, StallAt run τ (.wait k) →
      ∃ t r d', τ ≤ t ∧ ((run t).home k = some r ∨ (run t).src k = some r) ∧
        ¬ CanAdd cfg (run t) k ∧ StallAt run t (.head r d')) := by
  have hl : ∀ t, 23 ≤ t → run t = dead := fun _ h => run_late h
  refine ⟨?_, ?_, ?_, ?_, ?_⟩
  · intro τ p d o hr _ hfree
    obtain ⟨t, ht, hf⟩ := hfree (τ + 23) (by omega)
    rw [hl t (by omega)] at hf
    have hm := Config.route_mem hr
    simp only [cfg, List.mem_cons, Prod.mk.injEq, List.not_mem_nil, or_false] at hm
    have : o = 0 ∨ o = 1 ∨ o = 2 ∨ o = 3 ∨ o = 4 ∨ o = 5 := by omega
    rcases this with rfl | rfl | rfl | rfl | rfl | rfl <;> exact absurd hf (by decide)
  · intro τ p d o hr hall hio
    exfalso
    obtain ⟨hg, hi⟩ := hall (τ + 23) (by omega)
    obtain ⟨t, ht, hrm⟩ := hio (τ + 23) (by omega)
    rw [hl (τ + 23) (by omega)] at hg hi
    rw [hl t (by omega)] at hrm
    obtain ⟨w, hw, hd⟩ := hi
    have hm := Config.route_mem hr
    simp only [cfg, List.mem_cons, Prod.mk.injEq, List.not_mem_nil, or_false] at hm
    rcases hm with ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩ |
        ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩ | ⟨rfl, rfl, rfl⟩
    · have : dead.q 0 = [B2, R1'] := rfl
      rw [this] at hw; simp at hw; subst hw; simp [B2] at hd
    · exact noFit1 _ (hrm w hw)
    · exact noFit2 _ (hrm w hw)
    · have : dead.q 1 = [B4, R2'] := rfl
      rw [this] at hw; simp at hw; subst hw; simp [B4] at hd
    · have : dead.q 2 = [] := rfl
      rw [this] at hw; simp at hw
    · exact absurd hg (by decide)
    · exact absurd hg (by decide)
    · have : dead.q 5 = [] := rfl
      rw [this] at hw; simp at hw
  · intro τ o p _
    refine ⟨τ + 23, by omega, fun t ht he => ?_⟩
    rw [hl (t + 1) (by omega)] at he
    have : dead.ev = none := rfl
    rw [this] at he; simp at he
  · intro τ o d r _ hp _
    simp [cfg, Config.pool] at hp
  · intro τ k hst
    exfalso
    have hw := own_wait_isSome.1 (hst (τ + 23) (by omega)).1
    rw [hl _ (by omega)] at hw
    obtain ⟨hlive, hno⟩ := hw
    have hk : k = 1 ∨ k = 2 ∨ k = 3 ∨ k = 4 ∨ k = 5 ∨ k = 6 := by
      apply Classical.byContradiction
      intro hk
      have h1 : k ≠ 1 := fun e => hk (Or.inl e)
      have h2 : k ≠ 2 := fun e => hk (Or.inr (Or.inl e))
      have h3 : k ≠ 3 := fun e => hk (Or.inr (Or.inr (Or.inl e)))
      have h4 : k ≠ 4 := fun e => hk (Or.inr (Or.inr (Or.inr (Or.inl e))))
      have h5 : k ≠ 5 := fun e => hk (Or.inr (Or.inr (Or.inr (Or.inr (Or.inl e)))))
      have h6 : k ≠ 6 := fun e => hk (Or.inr (Or.inr (Or.inr (Or.inr (Or.inr e)))))
      have hd : dead.live = upd (upd (upd (upd (upd (upd (upd (upd St.empty.live 1 true) 2 true) 3 true) 4 true) 5 true) 6 true) 1 true) 2 true := rfl
      rw [hd] at hlive
      simp [upd, St.empty, h1, h2, h3, h4, h5, h6] at hlive
    have q0 : dead.q 0 = [B2, R1'] := rfl
    have q1 : dead.q 1 = [B4, R2'] := rfl
    have q3 : dead.q 3 = [B1] := rfl
    have q4 : dead.q 4 = [B3] := rfl
    rcases hk with rfl | rfl | rfl | rfl | rfl | rfl
    · exact hno 0 ⟨R1', by simp [q0], rfl⟩
    · exact hno 1 ⟨R2', by simp [q1], rfl⟩
    · exact hno 3 ⟨B1, by simp [q3], rfl⟩
    · exact hno 4 ⟨B3, by simp [q4], rfl⟩
    · exact hno 0 ⟨B2, by simp [q0], rfl⟩
    · exact hno 1 ⟨B4, by simp [q1], rfl⟩

theorem dead_q2 : dead.q 2 = [] := rfl
theorem dead_q5 : dead.q 5 = [] := rfl
theorem dead_grant_0 : dead.grant 0 = some 0 := rfl
theorem dead_grant_1 : dead.grant 1 = some 0 := rfl
theorem dead_grant_2 : dead.grant 2 = some 1 := rfl
theorem dead_grant_3 : dead.grant 3 = some 1 := rfl
theorem dead_grant_4 : dead.grant 4 = some 2 := rfl
theorem dead_grant_5 : dead.grant 5 = some 5 := rfl
theorem dead_grant_high (n : Nat) : dead.grant (n + 6) = none := rfl

/-- The run never ends its first transaction. -/
theorem run_never_ends (t : Nat) (h : 23 ≤ t) : (run t).live 1 = true := by
  rw [run_late h]; rfl

/-- The holder's next word is upstream, behind a stuck head. -/
theorem up_0_2 : Upstream cfg 0 2 := .step (d := 0) (o := 0) rfl rfl (.refl 2)
theorem up_1_5 : Upstream cfg 1 5 := .step (d := 1) (o := 3) rfl rfl (.refl 5)

/-- Heads in the dead state never move. -/
theorem dead_head_stalls {p d : Nat} (hi : headIs dead p d) (τ : Nat) (hτ : 23 ≤ τ) :
    StallAt run τ (.head p d) := by
  intro t ht
  refine ⟨?_, ?_⟩
  · rw [run_late (by omega : 23 ≤ t)]; exact own_head_isSome.2 hi
  · rintro ⟨_, o, he⟩
    rw [run_late (by omega : 23 ≤ t + 1)] at he
    simp [dead] at he

/-- This run is fair and hangs.  So `Fair` does not hide the multi-hop hang. -/
theorem hang_run_fair : Fair cfg run := by
  obtain ⟨h1, h2, h3, h4, h5⟩ := five_clauses
  refine ⟨h1, h2, h3, ?_, h4, h5⟩
  intro τ o p _ hall
  have hg := (hall (τ + 23) (by omega)).1
  rw [run_late (by omega : 23 ≤ τ + 23)] at hg
  -- Six outputs are held in the dead state, each by a slot with a known wait.
  have hcases : (o = 4 ∧ p = 2) ∨ (o = 5 ∧ p = 5) ∨ (o = 1 ∧ p = 0) ∨ (o = 2 ∧ p = 1) ∨
      (o = 0 ∧ p = 0) ∨ (o = 3 ∧ p = 1) := by
    match o, hg with
    | 0, h => exact Or.inr (Or.inr (Or.inr (Or.inr (Or.inl ⟨rfl, by simpa [dead_grant_0] using h.symm⟩))))
    | 1, h => exact Or.inr (Or.inr (Or.inl ⟨rfl, by simpa [dead_grant_1] using h.symm⟩))
    | 2, h => exact Or.inr (Or.inr (Or.inr (Or.inl ⟨rfl, by simpa [dead_grant_2] using h.symm⟩)))
    | 3, h => exact Or.inr (Or.inr (Or.inr (Or.inr (Or.inr ⟨rfl, by simpa [dead_grant_3] using h.symm⟩))))
    | 4, h => exact Or.inl ⟨rfl, by simpa [dead_grant_4] using h.symm⟩
    | 5, h => exact Or.inr (Or.inl ⟨rfl, by simpa [dead_grant_5] using h.symm⟩)
    | n + 6, h => simp [dead_grant_high] at h
  have hat : run (τ + 23) = dead := run_late (by omega)
  rcases hcases with ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩
  · refine Or.inr ⟨τ + 23, 0, 1, by omega, up_0_2, Or.inr rfl, ?_, dead_head_stalls ⟨B2, rfl, rfl⟩ _ (by omega)⟩
    rw [hat]; rintro ⟨w, hw, _⟩; simp [dead_q2] at hw
  · refine Or.inr ⟨τ + 23, 1, 0, by omega, up_1_5, Or.inr rfl, ?_, dead_head_stalls ⟨B4, rfl, rfl⟩ _ (by omega)⟩
    rw [hat]; rintro ⟨w, hw, _⟩; simp [dead_q5] at hw
  · -- Target 0 holds output 1 and its head goes through it, with no room below.
    exact Or.inl ⟨τ + 23, by omega, by rw [hat]; exact ⟨B2, rfl, rfl⟩⟩
  · exact Or.inl ⟨τ + 23, by omega, by rw [hat]; exact ⟨B4, rfl, rfl⟩⟩
  · -- Target 0 still holds its first hop, and its head goes elsewhere.
    refine Or.inr ⟨τ + 23, 0, 1, by omega, .refl 0, Or.inl rfl, ?_,
      dead_head_stalls ⟨B2, rfl, rfl⟩ _ (by omega)⟩
    rw [hat]
    rintro ⟨w, hw, hr⟩
    have hb : (dead.q 0).head? = some B2 := rfl
    rw [hb] at hw
    have : B2 = w := by simpa using hw
    subst this
    exact absurd hr (show ¬ cfg.route 0 1 = some 0 by decide)
  · refine Or.inr ⟨τ + 23, 1, 0, by omega, .refl 1, Or.inl rfl, ?_,
      dead_head_stalls ⟨B4, rfl, rfl⟩ _ (by omega)⟩
    rw [hat]
    rintro ⟨w, hw, hr⟩
    have hb : (dead.q 1).head? = some B4 := rfl
    rw [hb] at hw
    have : B4 = w := by simpa using hw
    subst this
    exact absurd hr (show ¬ cfg.route 1 0 = some 3 by decide)

/-- In the dead state six points form a closed wait, for every graph and rank. -/
theorem unprovable (t : EdgeTable) (hrank : t.checkRank = true) (node : Hold → Nat)
    (env : (Nat → St) → Prop) : ¬ StructuralInstanceLemmas (noc cfg t node env hrank) := by
  intro str
  have hlegal : (noc cfg t node env hrank).Legal run := ⟨rfl, run_steps⟩
  have hσ : run 23 = dead := rfl
  have hh0 : headIs dead 0 1 := ⟨B2, rfl, rfl⟩
  have hh1 : headIs dead 1 0 := ⟨B4, rfl, rfl⟩
  have hh3 : headIs dead 3 1 := ⟨B1, rfl, rfl⟩
  have hh4 : headIs dead 4 0 := ⟨B3, rfl, rfl⟩
  have hg4 : dead.grant 4 = some 2 := rfl
  have hg5 : dead.grant 5 = some 5 := rfl
  have hg1 : dead.grant 1 = some 0 := rfl
  have hg2 : dead.grant 2 = some 1 := rfl
  have hw2 : ¬ headRoutes cfg dead 2 4 := by rintro ⟨w, hw, _⟩; simp [dead_q2] at hw
  have hw5 : ¬ headRoutes cfg dead 5 5 := by rintro ⟨w, hw, _⟩; simp [dead_q5] at hw
  let W : Hold → Prop := fun h =>
    h = .grant 4 2 ∨ h = .head 0 1 ∨ h = .head 3 1 ∨ h = .grant 5 5 ∨ h = .head 1 0 ∨ h = .head 4 0
  have hclosed : ∀ y, W y → ∃ x, W x ∧ (noc cfg t node env hrank).blocked (run 23) x y := by
    intro y hy
    rw [hσ]
    rcases hy with rfl | rfl | rfl | rfl | rfl | rfl
    · -- The last-hop holder is empty, and its next word is behind target 0's head.
      have ha : Awaits cfg dead 0 1 4 2 := ⟨up_0_2, hh0, hw2, Or.inr rfl⟩
      exact ⟨.head 0 1, Or.inr (Or.inl rfl), own_head_isSome.2 hh0, own_grant_isSome.2 hg4,
        ⟨hg4, Or.inr ha⟩, fun hp => hp.2 ha⟩
    · -- Target 0's head has its grant and no room below.
      refine ⟨.head 3 1, Or.inr (Or.inr (Or.inl rfl)), own_head_isSome.2 hh3, own_head_isSome.2 hh0,
        ⟨hh0, 1, rfl, hg1, rfl⟩, ?_⟩
      intro hp
      exact noFit1 _ (hp 1 rfl hg1 rfl B2 rfl)
    · -- The link head waits for the last hop the other slot holds.
      refine ⟨.grant 5 5, Or.inr (Or.inr (Or.inr (Or.inl rfl))), own_grant_isSome.2 hg5,
        own_head_isSome.2 hh3, ⟨hh3, rfl, by decide⟩, ?_⟩
      intro hp
      exact hp hg5
    · have ha : Awaits cfg dead 1 0 5 5 := ⟨up_1_5, hh1, hw5, Or.inr rfl⟩
      exact ⟨.head 1 0, Or.inr (Or.inr (Or.inr (Or.inr (Or.inl rfl)))), own_head_isSome.2 hh1,
        own_grant_isSome.2 hg5, ⟨hg5, Or.inr ha⟩, fun hp => hp.2 ha⟩
    · refine ⟨.head 4 0, Or.inr (Or.inr (Or.inr (Or.inr (Or.inr rfl)))), own_head_isSome.2 hh4,
        own_head_isSome.2 hh1, ⟨hh1, 2, rfl, hg2, rfl⟩, ?_⟩
      intro hp
      exact noFit2 _ (hp 2 rfl hg2 rfl B4 rfl)
    · refine ⟨.grant 4 2, Or.inl rfl, own_grant_isSome.2 hg4, own_head_isSome.2 hh4,
        ⟨hh4, rfl, by decide⟩, ?_⟩
      intro hp
      exact hp hg4
  exact noClosedResourceWait _ str hlegal 23 (y₀ := .grant 4 2) (W := W) (Or.inl rfl) hclosed

/-- From the dead state every legal continuation stays there. -/
theorem stays {f : Nat → St} (h0 : f 0 = dead) (hs : ∀ j, Step cfg (f j) (f (j + 1))) (j : Nat) :
    f j = dead := by
  induction j with
  | zero => exact h0
  | succ j ih =>
    have := hs j
    rw [ih] at this
    exact dead_is_dead _ this

/-- The final goal `DeadlockFree` is false on this instance, for `Fair` or any other
    environment.  This agrees with `unprovable`: W2 cannot be proved here. -/
theorem goal_false (env : (Nat → St) → Prop) :
    ¬ (noc cfg table nodeOf env h_rank).DeadlockFree (liveWork (c := cfg) (node := nodeOf) h_rank env) := by
  intro hdf
  have hreach : (noc cfg table nodeOf env h_rank).Reachable dead :=
    ⟨run, ⟨rfl, run_steps⟩, 23, rfl⟩
  obtain ⟨h, f, t, _, hf0, hstep, _, _, hprog⟩ := hdf dead hreach (1 : Nat) rfl
  have hall := stays (f := f) hf0 hstep
  have hp : prog dead dead h := by
    have := hprog
    simp only [noc] at this
    rw [hall t, hall (t + 1)] at this
    exact this
  have hev : dead.ev = none := rfl
  cases h with
  | head p d => obtain ⟨_, o, he⟩ := hp; rw [hev] at he; simp at he
  | grant o p => obtain ⟨_, he⟩ := hp; rw [hev] at he; simp at he
  | ctx k p => obtain ⟨_, o, he⟩ := hp; rw [hev] at he; simp at he
  | wait k => obtain ⟨⟨_, hno⟩, p, hx⟩ := hp; exact hno p hx

#print axioms goal_false
#print axioms unprovable
#print axioms hang_run_fair
#print axioms dead_is_dead
end Wormhole.MultiHop
