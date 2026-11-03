/-
  GenericNoC.Model: the abstract model: a transition system with hold points,
  owners, progress, need and permit observations, a dependency graph and a rank
  certificate.

  The rank strictly increases along every edge, from blocker to blocked.

  Core only; no external library.
-/
import GenericNoC.Core

/-- W1: a generic fixed-route request–response NoC model.
    Fields up to `env` are the circuit expansion's observations; `en_iff`,
    `prog_held_en`, `own_stable` are the hold-point axioms;
    `edge_rank` is the rank certificate. -/
structure GenericNoC : Type 1 where
  State : Type
  Hold : Type
  Node : Type
  Component : Type
  Owner : Type
  /-- I: initial states. -/
  init : State → Prop
  /-- T: synchronous transition relation. -/
  step : State → State → Prop
  /-- Owner observation; `none` iff the point is unheld. -/
  own : State → Hold → Option Owner
  /-- Progress event on a transition. -/
  prog : State → State → Hold → Prop
  /-- Resource need: `need σ x y` — x is a direct transactional resource
      predecessor of y in state σ. -/
  need : State → Hold → Hold → Prop
  /-- Permission: `permit σ x y` — the combinational availability from x to y. -/
  permit : State → Hold → Hold → Prop
  /-- External readiness of the current owner. -/
  ready : State → Hold → Prop
  /-- Combinational progress enable. -/
  en : State → Hold → Prop
  /-- The enable decomposition, verified by the circuit expansion. -/
  en_iff : ∀ σ y, en σ y ↔ ready σ y ∧ ∀ x, need σ x y → permit σ x y
  /-- Progress implies held and enabled. -/
  prog_held_en : ∀ {σ σ' h}, step σ σ' → prog σ σ' h → (own σ h).isSome ∧ en σ h
  /-- A held point that does not advance keeps its owner. -/
  own_stable : ∀ {σ σ' h}, step σ σ' → (own σ h).isSome → ¬ prog σ σ' h →
    own σ' h = own σ h
  /-- κ: owning circuit instance of a hold point. -/
  componentOf : Hold → Component
  /-- μ: static graph node of a hold point. -/
  classOf : Hold → Node
  /-- E: dependency edges, blocker → blocked. -/
  edge : Node → Node → Prop
  /-- ψ: global rank certificate. -/
  graphRank : Node → Nat
  /-- θ: local rank within a node. -/
  localRank : State → Hold → Nat
  /-- The global rank strictly increases along every edge. -/
  edge_rank : ∀ {u v}, edge u v → graphRank u < graphRank v
  /-- Ext/Env: the external service condition on trajectories. -/
  env : (Nat → State) → Prop

namespace GenericNoC

variable (N : GenericNoC)

/-- Direct blocking in a state — both points held, y needs x,
    and x does not permit y.  The blocker points at the blocked. -/
def blocked (σ : N.State) (x y : N.Hold) : Prop :=
  (N.own σ x).isSome ∧ (N.own σ y).isSome ∧ N.need σ x y ∧ ¬ N.permit σ x y

/-- Legal trajectory: starts in I, every step in T. -/
def Legal (s : Nat → N.State) : Prop :=
  N.init (s 0) ∧ ∀ t, N.step (s t) (s (t + 1))

/-- Reachable state: appears on some legal trajectory. -/
def Reachable (σ : N.State) : Prop :=
  ∃ s, N.Legal s ∧ ∃ t, s t = σ

/-- Positive-length dependency path. -/
inductive Reaches : N.Node → N.Node → Prop where
  | single {u v} : N.edge u v → Reaches u v
  | trans {u v w} : Reaches u v → N.edge v w → Reaches u w

/-- Permanent stall of h on trajectory s from time τ. -/
def Stall (s : Nat → N.State) (τ : Nat) (h : N.Hold) : Prop :=
  ∀ t, τ ≤ t → (N.own (s t) h).isSome ∧ ¬ N.prog (s t) (s (t + 1)) h

/-- The rank strictly increases along every positive-length path. -/
theorem Reaches.rank_lt {u v : N.Node} (h : N.Reaches u v) :
    N.graphRank u < N.graphRank v := by
  induction h with
  | single he => exact N.edge_rank he
  | trans _ he ih => exact Nat.lt_trans ih (N.edge_rank he)

/-- A node never reaches itself (certificate soundness, direction
    "rank ⇒ acyclic"). -/
theorem Reaches.no_self (h : N.Reaches u u) : False :=
  Nat.lt_irrefl _ (Reaches.rank_lt N h)

end GenericNoC
