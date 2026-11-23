-- Extend the proof generated for examples/axi4_exchange.yaml.
namespace Generated.Ordinary

open Wormhole

private def firstWord : Word := ⟨0, false, false, 0⟩

private theorem admits_short_request :
    Step config St.empty (injected St.empty 0 firstWord 1) :=
  Step.inject St.empty 0 firstWord 1
    (by decide) (by decide) (Or.inr (Or.inl (by decide)))
    (by simp [St.empty]) (by decide) (by decide) (by decide) (by decide) (by decide)
    (by decide) (by simp [firstWord]) (by simp [config, Import.natSet, firstWord])

private theorem source_has_no_free_sink (d : Nat) : ¬ EndsFree config 0 d := by
  intro ends
  cases ends with
  | sink hr _ hp =>
    have hm := Config.route_mem hr
    simp [config] at hm
    rcases hm with ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ <;> contradiction
  | step hr hn _ =>
    have hm := Config.route_mem hr
    simp [config] at hm
    rcases hm with ⟨rfl, rfl⟩ | ⟨rfl, rfl⟩ <;> contradiction

/-- The rules and rank checks pass for this model, but its short request has
    no fair continuation. -/
theorem no_service_continuation :
    ¬ GenericNoC.ServiceContinuation (noc config table node (Fair config) certificate) :=
  no_serviceContinuation_of_short_request wf_ok (Import.capOf_pos caps_ok) hops_ok certificate
    (checkCoverageFast_covered covered_ok) heads_ok rfl rfl admits_short_request
    source_has_no_free_sink (by intro p; simp [config, Import.pairSet, firstWord])

theorem no_universal_finite_drain :
    ¬ (∀ σ, (noc config table node (Fair config) certificate).Reachable σ →
      FiniteDrain config σ) := by
  intro drain
  exact no_service_continuation (serviceContinuation_of_finiteDrain certificate
    (fun _ _ r _ => Import.capOf_pos (rows := poolCapRows) (by decide) r) drain)

#print axioms no_service_continuation
#print axioms no_universal_finite_drain

end Generated.Ordinary
