import GenericNoC.ReadRefinement
import GenericNoC.WormholeDone

namespace Wormhole.ReadRefinement

open GenericNoC

/-- One observation per clock, starting from reset and accepting every edge. -/
structure Cycles where
  occupied : Nat → Bool
  request : Nat → Bool
  response : Nat → Bool
  initial : occupied 0 = false
  accepted : ∀ t, stepOK ⟨occupied t, request t, response t, occupied (t + 1)⟩ = true

def Cycles.token (cy : Cycles) : Nat → Nat
  | 0 => 0
  | t + 1 => if cy.response t then cy.token t + 1 else cy.token t

abbrev Cycles.sample (cy : Cycles) (t : Nat) : Sample :=
  ⟨cy.occupied t, cy.request t, cy.response t, cy.occupied (t + 1)⟩

private theorem Cycles.keeps_context (cy : Cycles) {t : Nat}
    (live : cy.occupied t = true) (wait : cy.response t = false) :
    cy.occupied (t + 1) = true := by
  have ok := cy.accepted t
  cases h : cy.request t
  · simp [stepOK, live, wait, h] at ok
    exact ok
  · simp [stepOK, live, wait, h] at ok

structure Boundary (p : Path) (cy : Cycles) (t : Nat) where
  state : St
  matching : Matches p state (cy.occupied t) (cy.token t)
  archived : Archived state (cy.token t)

structure Frame (c : Config) (p : Path) (cy : Cycles) (t : Nat) (a : Boundary p cy t) where
  target : Boundary p cy (t + 1)
  mid : List St
  steps : Steps (Step c) a.state mid target.state

private noncomputable def frame {c : Config} {p : Path} (cfg : p.Valid c) {cy : Cycles}
    (t : Nat) (a : Boundary p cy t) : Frame c p cy t a := by
  have existsFrame := frame_sound (s := cy.sample t) cfg a.matching a.archived (cy.accepted t)
  let state := Classical.choose existsFrame
  have mids := Classical.choose_spec existsFrame
  let mid := Classical.choose mids
  have proof := Classical.choose_spec mids
  exact ⟨⟨state, proof.2.1, proof.2.2⟩, mid, proof.1⟩

noncomputable def boundary {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) :
    (t : Nat) → Boundary p cy t
  | 0 => ⟨St.empty, cy.initial ▸ matches_reset p, fun _ h => (Nat.not_lt_zero _ h).elim⟩
  | t + 1 => (frame cfg t (boundary cfg cy t)).target

noncomputable def middle {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) (t : Nat) :
    List St := (frame cfg t (boundary cfg cy t)).mid

noncomputable def run {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) : Nat → St :=
  flatRun (fun t => (boundary cfg cy t).state) (middle cfg cy)

noncomputable def position {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) : Nat → Nat :=
  flatPos (middle cfg cy)

theorem run_boundary {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) (t : Nat) :
    run cfg cy (position cfg cy t) = (boundary cfg cy t).state := flatRun_flatPos _ _ _

theorem run_init {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) :
    run cfg cy 0 = St.empty := by
  have := run_boundary cfg cy 0
  exact this

theorem run_step {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) (u : Nat) :
    Step c (run cfg cy u) (run cfg cy (u + 1)) :=
  flatRun_step _ _ (τ := 0) (fun t _ => (frame cfg t (boundary cfg cy t)).steps) u
    (Nat.zero_le _)

theorem position_ge {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles) (t : Nat) :
    t ≤ position cfg cy t := by
  induction t with
  | zero => exact Nat.zero_le _
  | succ t ih =>
    have := flatPos_lt (middle cfg cy) t
    change position cfg cy t < position cfg cy (t + 1) at this
    omega

/-- The direct cycle checker maps external returns to response firings. If
    the model run satisfies Fair and the dependency premises, every occupied
    cycle has a response at or after it. -/
theorem checked_context_completes {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles)
    {table : EdgeTable} {node : Hold → Nat}
    (wf : wfCheck c = true) (cap : ∀ r, 0 < c.cap r) (hops : hopsOk c = true)
    (rank : table.checkRank = true) (covered : Covered c table node)
    (heads : headsInRange c table node = true) (fair : Fair c (run cfg cy))
    {t : Nat} (occupied : cy.occupied t = true) :
    ∃ u, t ≤ u ∧ cy.response u = true := by
  classical
  apply Classical.byContradiction
  intro noResponse
  have quiet : ∀ u, t ≤ u → cy.response u = false := by
    intro u hu
    cases h : cy.response u
    · rfl
    · exact False.elim (noResponse ⟨u, hu, h⟩)
  have held : ∀ u, t ≤ u → cy.occupied u = true ∧ cy.token u = cy.token t := by
    intro u hu
    induction hu with
    | refl => exact ⟨occupied, rfl⟩
    | @step u hu ih => exact ⟨cy.keeps_context ih.1 (quiet u hu), by
        simp [Cycles.token, quiet u hu, ih.2]⟩
  obtain ⟨v, hv, ended⟩ := checked_transaction_completes wf cap hops rank covered heads
    (run_init cfg cy) (run_step cfg cy) fair (position cfg cy t) (cy.token t)
  have usedAt : (run cfg cy (position cfg cy t)).used (cy.token t) = true := by
    rw [run_boundary, (boundary cfg cy t).matching.used, occupied]
  have usedLater : ∀ u, position cfg cy t ≤ u → (run cfg cy u).used (cy.token t) = true := by
    intro u hu
    induction hu with
    | refl => exact usedAt
    | @step u _ ih => exact used_step (run_step cfg cy u) ih
  have deadLater : ∀ u, v ≤ u → (run cfg cy u).live (cy.token t) = false := by
    intro u hu
    induction hu with
    | refl => exact ended
    | @step u hu ih => exact no_restart (run_step cfg cy u) ih (usedLater u (Nat.le_trans hv hu))
  let u := max t v
  have hu : t ≤ u := Nat.le_max_left _ _
  have lePos : v ≤ position cfg cy u := Nat.le_trans (Nat.le_max_right _ _) (position_ge cfg cy u)
  have dead := deadLater _ lePos
  rw [run_boundary, ← (held u hu).2, (boundary cfg cy u).matching.live, (held u hu).1] at dead
  contradiction

private theorem checked_events {s : Sample} (ok : stepOK s = true) :
    (s.request = true → s.nextOccupied = true ∧ s.response = false) ∧
    (s.response = true → s.nextOccupied = false) ∧
    (s.nextOccupied = true → s.request = true ∨ s.occupied = true ∧ s.response = false) := by
  rcases s with ⟨before, req, rsp, after⟩
  cases before <;> cases req <;> cases rsp <;> cases after <;> simp_all [stepOK]

theorem Cycles.context_origin (cy : Cycles) {t : Nat} (held : cy.occupied t = true) :
    ∃ r, r < t ∧ cy.request r = true ∧
      ∀ u, r ≤ u → u < t → cy.response u = false := by
  induction t with
  | zero => simp [cy.initial] at held
  | succ t ih =>
    have facts := checked_events (s := cy.sample t) (cy.accepted t)
    rcases facts.2.2 held with request | ⟨still, quiet⟩
    · refine ⟨t, by omega, request, fun u lo hi => ?_⟩
      have : u = t := by omega
      exact this ▸ (facts.1 request).2
    · obtain ⟨r, earlier, request, noResponse⟩ := ih still
      refine ⟨r, by omega, request, fun u lo hi => ?_⟩
      by_cases same : u = t
      · exact same ▸ quiet
      · exact noResponse u lo (by omega)

/-- Progress at the two target port boundaries. These are obligations on the
    circuit and its endpoint environment, independently of the model's Fair. -/
structure EndpointProgress (cy : Cycles) where
  targetRequest : Nat → Bool
  targetResponse : Nat → Bool
  issue : ∀ t, cy.request t = true → ∃ u, t ≤ u ∧ targetRequest u = true
  answer : ∀ t, targetRequest t = true → ∃ u, t ≤ u ∧ targetResponse u = true
  forward : ∀ t, targetResponse t = true → ∃ u, t ≤ u ∧ cy.response u = true

theorem EndpointProgress.vacant (cy : Cycles) (progress : EndpointProgress cy) (t : Nat) :
    ∃ u, t ≤ u ∧ cy.occupied u = false := by
  cases held : cy.occupied t
  · exact ⟨t, Nat.le_refl t, held⟩
  · obtain ⟨r, _, request, noResponse⟩ := cy.context_origin held
    obtain ⟨a, ra, issued⟩ := progress.issue r request
    obtain ⟨b, ab, answered⟩ := progress.answer a issued
    obtain ⟨u, bu, response⟩ := progress.forward b answered
    have ru := Nat.le_trans (Nat.le_trans ra ab) bu
    have tu : t ≤ u := by
      by_cases h : u < t
      · simp [noResponse u ru h] at response
      · omega
    exact ⟨u + 1, by omega, (checked_events (s := cy.sample u) (cy.accepted u)).2.1 response⟩

private theorem Boundary.dead_at_empty {p : Path} {cy : Cycles} {t : Nat} (b : Boundary p cy t)
    (empty : cy.occupied t = false) (k : Nat) : b.state.live k = false := by
  rcases Nat.lt_trichotomy k (cy.token t) with past | same | future
  · exact b.archived k past
  · rw [same, b.matching.live, empty]
  · exact (b.matching.future k future).1

/-- At cycle boundaries every model queue and grant is empty. With recurring
    vacant contexts, neither a full pool nor an unanswered token can persist. -/
theorem fair_of_endpoint_progress {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles)
    (cap : ∀ r, 0 < c.poolCap r) (progress : EndpointProgress cy) :
    Fair c (run cfg cy) := by
  constructor
  · intro τ slot dest _ _ every _
    have head := every (position cfg cy τ) (position_ge cfg cy τ)
    simp [run_boundary, headIs, (boundary cfg cy τ).matching.queues] at head
  · intro τ slot _ out _ every _
    have grant := (every (position cfg cy τ) (position_ge cfg cy τ)).1
    simp [run_boundary, (boundary cfg cy τ).matching.grants] at grant
  · intro τ out _ every
    have grant := every (position cfg cy τ) (position_ge cfg cy τ)
    simp [run_boundary, (boundary cfg cy τ).matching.grants] at grant
  · intro τ out _ _ every
    have grant := (every (position cfg cy τ) (position_ge cfg cy τ)).1
    simp [run_boundary, (boundary cfg cy τ).matching.grants] at grant
  · intro τ out dest r last pool alwaysFull
    obtain ⟨u, hu, empty⟩ := progress.vacant cy τ
    have full := alwaysFull (position cfg cy u) (Nat.le_trans hu (position_ge cfg cy u))
    have holders := (boundary cfg cy u).matching.holders
    simp [run_boundary, room, last, pool, poolUse, holders, empty, cap r] at full
  · intro τ k stalled
    obtain ⟨u, hu, empty⟩ := progress.vacant cy τ
    have owns := (stalled (position cfg cy u) (Nat.le_trans hu (position_ge cfg cy u))).1
    have live := (own_wait_isSome.mp owns).1
    simp [run_boundary, (boundary cfg cy u).dead_at_empty empty k] at live

/-- A response for an accepted request on the direct transport, derived through
    the model's completion theorem. The progress premises also give it alone. -/
theorem checked_request_completes {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles)
    {table : EdgeTable} {node : Hold → Nat}
    (wf : wfCheck c = true) (cap : ∀ r, 0 < c.cap r) (hops : hopsOk c = true)
    (rank : table.checkRank = true) (covered : Covered c table node)
    (heads : headsInRange c table node = true) (poolCap : ∀ r, 0 < c.poolCap r)
    (progress : EndpointProgress cy) {t : Nat} (request : cy.request t = true) :
    ∃ u, t < u ∧ cy.response u = true := by
  have occupied := (checked_events (s := cy.sample t) (cy.accepted t)).1 request
  obtain ⟨u, hu, response⟩ := checked_context_completes cfg cy wf cap hops rank covered heads
    (fair_of_endpoint_progress cfg cy poolCap progress) (t := t + 1) occupied.1
  exact ⟨u, hu, response⟩

/-- The two outer boundaries of the same serialized transaction stream. -/
structure PortProgress (cy : Cycles) extends EndpointProgress cy where
  sourceRequest : Nat → Bool
  sourceResponse : Nat → Bool
  start : ∀ t, sourceRequest t = true → ∃ u, t ≤ u ∧ cy.request u = true
  finish : ∀ t, cy.response t = true → ∃ u, t ≤ u ∧ sourceResponse u = true

/-- The five progress premises chain to the response without the model. -/
theorem port_completes_of_progress (cy : Cycles) (progress : PortProgress cy) {t : Nat}
    (request : progress.sourceRequest t = true) :
    ∃ u, t < u ∧ progress.sourceResponse u = true := by
  obtain ⟨a, ta, sent⟩ := progress.start t request
  obtain ⟨b, ab, issued⟩ := progress.issue a sent
  obtain ⟨d, bd, answered⟩ := progress.answer b issued
  obtain ⟨e, de, response⟩ := progress.forward d answered
  obtain ⟨u, eu, returned⟩ := progress.finish e response
  have quiet := ((checked_events (s := cy.sample a) (cy.accepted a)).1 sent).2
  have ae : a ≠ e := fun same => by simp [same, response] at quiet
  exact ⟨u, by omega, returned⟩

/-- A trace whose target answers every request it receives, while the
    transport never issues the accepted request. `issue` is transport progress
    that the trace and the model do not supply. -/
theorem answer_alone_does_not_complete :
    ∃ (cy : Cycles) (targetRequest targetResponse : Nat → Bool),
      (∀ t, targetRequest t = true → ∃ u, t ≤ u ∧ targetResponse u = true) ∧
      cy.request 0 = true ∧ ∀ u, cy.response u = false := by
  let cy : Cycles := {
    occupied := fun t => t != 0
    request := fun t => t == 0
    response := fun _ => false
    initial := rfl
    accepted := fun t => by cases t <;> simp [stepOK] }
  exact ⟨cy, fun _ => false, fun _ => false, fun _ h => Bool.noConfusion h, rfl, fun _ => rfl⟩

/-- `port_completes_of_progress` gives the same conclusion without the model
    premises. -/
theorem checked_port_completes {c : Config} {p : Path} (cfg : p.Valid c) (cy : Cycles)
    {table : EdgeTable} {node : Hold → Nat}
    (wf : wfCheck c = true) (cap : ∀ r, 0 < c.cap r) (hops : hopsOk c = true)
    (rank : table.checkRank = true) (covered : Covered c table node)
    (heads : headsInRange c table node = true) (poolCap : ∀ r, 0 < c.poolCap r)
    (progress : PortProgress cy) {t : Nat} (request : progress.sourceRequest t = true) :
    ∃ u, t < u ∧ progress.sourceResponse u = true := by
  obtain ⟨inbound, start, req⟩ := progress.start t request
  obtain ⟨outbound, later, rsp⟩ := checked_request_completes cfg cy
    wf cap hops rank covered heads poolCap progress.toEndpointProgress req
  obtain ⟨u, finish, returned⟩ := progress.finish outbound rsp
  exact ⟨u, by omega, returned⟩

end Wormhole.ReadRefinement
