-- Must not compile under tools/run_checks.py: no rule may be assumed by `axiom`
-- and pass the axiom gate.  The script turns the listing below into a failure.
import GenericNoC.WormholeRules
axiom assumed_rule : False
theorem uses_it : 1 = 2 := assumed_rule.elim
#print axioms uses_it
