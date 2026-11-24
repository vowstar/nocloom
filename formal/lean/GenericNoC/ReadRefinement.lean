import GenericNoC.Refinement
import GenericNoC.Wormhole

namespace Wormhole.ReadRefinement

/-- The two routes of a direct request and response path. -/
structure Path where
  requestSlot : Nat
  requestDest : Nat
  requestOut : Nat
  responseSlot : Nat
  responseDest : Nat
  responseOut : Nat

/-- Facts about the imported instance needed by the read checker. -/
structure Path.Valid (p : Path) (c : Config) : Prop where
  requestRoute : c.route p.requestSlot p.requestDest = some p.requestOut
  responseRoute : c.route p.responseSlot p.responseDest = some p.responseOut
  requestEnd : c.next p.requestOut = none
  responseEnd : c.next p.responseOut = none
  requestPool : c.pool p.requestOut p.requestDest = some p.responseSlot
  responseFree : c.pool p.responseOut p.responseDest = none
  requestEndpoint : p.requestSlot ∈ c.endpoints
  responseEndpoint : p.responseSlot ∈ c.endpoints
  requestCap : 0 < c.cap p.requestSlot
  responseCap : 0 < c.cap p.responseSlot
  poolCap : 0 < c.poolCap p.responseSlot
  requestIsNotResponse : isResponse c p.requestSlot = false
  answers : c.answers p.requestSlot p.responseDest = true
  requestUnbound : c.bound p.requestDest = false
  responseUnbound : c.bound p.responseDest = false

/-- A cycle at a transport with no queues between its request and response
    boundaries. An occupancy bit observes the target's read context. -/
structure Sample where
  occupied : Bool
  request : Bool
  response : Bool
  nextOccupied : Bool
  deriving DecidableEq

/-- The accepted cycle patterns. An idle cycle keeps the physical occupancy. -/
def stepOK : Sample → Bool
  | ⟨false, true, false, true⟩ | ⟨true, false, true, false⟩ => true
  | ⟨before, false, false, after⟩ => before == after
  | _ => false

/-- The target context holds the current logical transaction, if occupied.
    Completed tokens stay in the model history and future tokens remain fresh. -/
structure Matches (p : Path) (σ : St) (occupied : Bool) (token : Nat) : Prop where
  queues : ∀ s, σ.q s = []
  grants : ∀ o, σ.grant o = none
  openIn : ∀ s, σ.openIn s = none
  holders : σ.holders = if occupied then [token] else []
  live : σ.live token = occupied
  used : σ.used token = occupied
  done : σ.reqDone token = occupied
  budget : occupied = true → σ.budget token = 1
  src : occupied = true → σ.src token = some p.requestSlot
  home : occupied = true → σ.home token = some p.responseSlot
  future : ∀ k, token < k → σ.live k = false ∧ σ.used k = false ∧ σ.reqDone k = false

theorem matches_reset (p : Path) : Matches p St.empty false 0 where
  queues := fun _ => rfl
  grants := fun _ => rfl
  openIn := fun _ => rfl
  holders := rfl
  live := rfl
  used := rfl
  done := rfl
  budget := by simp
  src := by simp
  home := by simp
  future := fun _ _ => ⟨rfl, rfl, rfl⟩

private def granted (σ : St) (s o : Nat) : St :=
  { σ with grant := upd σ.grant o (some s), ev := none }

private def requestWord (p : Path) (k : Nat) : Word := ⟨p.requestDest, true, false, k⟩
private def responseWord (p : Path) (k : Nat) : Word := ⟨p.responseDest, true, true, k⟩

private def requestResult (p : Path) (c : Config) (σ : St) (k : Nat) : St :=
  let w := requestWord p k
  let a := injected σ p.requestSlot w 1
  let g := granted a p.requestSlot p.requestOut
  place c (pop g p.requestSlot p.requestOut w) p.requestOut w

private theorem request_steps {p : Path} {c : Config} (cfg : p.Valid c) {σ : St} {k : Nat}
    (h : Matches p σ false k) : ∃ mid, GenericNoC.Steps (Step c) σ mid (requestResult p c σ k) := by
  let w := requestWord p k
  let a := injected σ p.requestSlot w 1
  let g := granted a p.requestSlot p.requestOut
  have head : (a.q p.requestSlot).head? = some w := by simp [a, injected, h.queues]
  refine ⟨[a, g], .cons (Step.inject σ p.requestSlot w 1 ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_)
    (.cons (Step.arb a p.requestSlot p.requestOut w head cfg.requestRoute (h.grants _))
      (.last (Step.fire g p.requestSlot p.requestOut w head cfg.requestRoute ?_ ?_ ?_ ?_)))⟩
  · exact cfg.requestEndpoint
  · simpa [h.queues] using cfg.requestCap
  · exact Or.inl rfl
  · simp [h.openIn]
  · simp [w, requestWord, cfg.requestRoute]
  · intro _; exact h.used
  · simp [w, requestWord, h.live]
  · simp [cfg.requestIsNotResponse]
  · simp [w, requestWord, h.live]
  · simp [w, requestWord, newBudget, h.live]
  · simp [w, requestWord]
  · simp [w, requestWord, cfg.requestUnbound]
  · simp [g, granted]
  · apply Or.inl
    simpa [room, w, requestWord, cfg.requestEnd, cfg.requestPool, poolUse, g, a, granted,
      injected, h.holders] using cfg.poolCap
  · simp [cfg.requestEnd]
  · simp [w, requestWord]

private theorem request_matches {p : Path} {c : Config} (cfg : p.Valid c) {σ : St} {k : Nat}
    (h : Matches p σ false k) : Matches p (requestResult p c σ k) true k := by
  constructor
  · intro s
    by_cases hs : s = p.requestSlot <;>
      simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
        cfg.requestPool, h.queues, upd, hs]
  · intro o
    simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, upd, h.grants]
  · intro s
    simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, nextOpen, upd, h.openIn]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, h.holders, h.done]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd, cfg.requestPool]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd, cfg.requestPool]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd, cfg.requestPool]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, newBudget, h.live]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, h.live]
  · simp [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, h.done]
  · intro j hj
    have ne : j ≠ k := Nat.ne_of_gt hj
    simpa [requestResult, requestWord, granted, injected, pop, place, cfg.requestEnd,
      cfg.requestPool, upd, ne] using h.future j hj

private def responseResult (p : Path) (c : Config) (σ : St) (k : Nat) : St :=
  let w := responseWord p k
  let a := injected σ p.responseSlot w 0
  let g := granted a p.responseSlot p.responseOut
  place c (pop g p.responseSlot p.responseOut w) p.responseOut w

private theorem response_steps {p : Path} {c : Config} (cfg : p.Valid c) {σ : St} {k : Nat}
    (h : Matches p σ true k) : ∃ mid, GenericNoC.Steps (Step c) σ mid (responseResult p c σ k) := by
  let w := responseWord p k
  let a := injected σ p.responseSlot w 0
  let g := granted a p.responseSlot p.responseOut
  have head : (a.q p.responseSlot).head? = some w := by simp [a, injected, h.queues]
  refine ⟨[a, g], .cons (Step.inject σ p.responseSlot w 0 ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_ ?_)
    (.cons (Step.arb a p.responseSlot p.responseOut w head cfg.responseRoute (h.grants _))
      (.last (Step.fire g p.responseSlot p.responseOut w head cfg.responseRoute ?_ ?_ ?_ ?_)))⟩
  · exact cfg.responseEndpoint
  · simpa [h.queues] using cfg.responseCap
  · exact Or.inl rfl
  · simp [h.openIn]
  · simp [w, responseWord, cfg.responseRoute]
  · simp [w, responseWord, h.live]
  · exact fun _ => Or.inr (h.home rfl)
  · exact fun _ => ⟨h.live, h.home rfl, h.done, by
      simp [w, responseWord, answersSrc, h.src rfl, cfg.answers]⟩
  · simp [w, responseWord, h.budget rfl]
  · simp [w, responseWord, newBudget, h.live, h.budget rfl]
  · exact fun _ => ⟨rfl, .sink cfg.responseRoute cfg.responseEnd cfg.responseFree⟩
  · simp [w, responseWord, cfg.responseUnbound]
  · simp [g, granted]
  · exact Or.inl (by simp [room, w, responseWord, cfg.responseEnd, cfg.responseFree])
  · simp [cfg.responseEnd]
  · simp [w, responseWord]

private theorem response_matches {p : Path} {c : Config} (cfg : p.Valid c) {σ : St} {k : Nat}
    (h : Matches p σ true k) : Matches p (responseResult p c σ k) false (k + 1) := by
  have next := h.future (k + 1) (Nat.lt_succ_self k)
  constructor
  · intro s
    by_cases hs : s = p.responseSlot <;>
      simp [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
        cfg.responseFree, h.queues, upd, hs]
  · intro o
    simp [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, upd, h.grants]
  · intro s
    simp [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, nextOpen, upd, h.openIn]
  · simp [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, h.holders, h.home rfl]
  · simpa [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, upd] using next.1
  · simpa [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, upd] using next.2.1
  · simpa [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, upd] using next.2.2
  · simp
  · simp
  · simp
  · intro j hj
    have lt : k < j := by omega
    simpa [responseResult, responseWord, granted, injected, pop, place, cfg.responseEnd,
      cfg.responseFree, upd, Nat.ne_of_gt lt] using h.future j lt

/-- A checked edge lifts to zero or more steps of the imported Wormhole
    instance. The ghost token advances only on a completed response. -/
theorem step_sound {p : Path} {c : Config} (cfg : p.Valid c) {sample : Sample} {σ : St} {k : Nat}
    (h : Matches p σ sample.occupied k) (ok : stepOK sample = true) :
    ∃ σ', Matches p σ' sample.nextOccupied (if sample.response then k + 1 else k) ∧
      (σ' = σ ∨ ∃ mid, GenericNoC.Steps (Step c) σ mid σ') := by
  rcases sample with ⟨before, req, rsp, after⟩
  cases before <;> cases req <;> cases rsp <;> cases after <;> simp [stepOK] at ok ⊢
  · exact ⟨σ, h, Or.inl rfl⟩
  · exact ⟨_, request_matches cfg h, Or.inr (request_steps cfg h)⟩
  · exact ⟨σ, h, Or.inl rfl⟩
  · exact ⟨_, response_matches cfg h, Or.inr (response_steps cfg h)⟩

end Wormhole.ReadRefinement
