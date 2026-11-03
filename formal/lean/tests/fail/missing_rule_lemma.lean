-- Must not compile: a rule cover without its per-rule lemmas is not a rule cover.
import GenericNoC.WormholeRules
open Wormhole
example (c : Config) (t : EdgeTable) (node : Hold → Nat) (env : (Nat → St) → Prop)
    (h : t.checkRank = true) : GenericNoC.RuleCover (noc c t node env h) where
  Rule := Rule
  fires := fun r _ x y => (x, y) ∈ required c r
  exhaustive := by
    intro s hl τ x y hb
    sorry
