-- The theorems whose axioms the checks read.  A theorem removed from the library
-- breaks this file, and `tools/run_checks.py` needs every one of them listed.
import GenericNoC

#print axioms GenericNoC.noClosedResourceWait
#print axioms GenericNoC.no_stall
#print axioms GenericNoC.deadlock_free
#print axioms GenericNoC.advances_on_every_compliant_run
#print axioms EdgeTable.checkRank_iff_checkEdgeRank
#print axioms EdgeTable.checkRank_false_of_cycle
#print axioms EdgeTable.checkRank_mono
#print axioms Wormhole.inv_run
#print axioms Wormhole.hold_across_needs_cross
#print axioms Wormhole.structural
#print axioms Wormhole.row_paths
#print axioms Wormhole.holder_gets_word
#print axioms Wormhole.instanceLemmas
#print axioms Wormhole.liveWork_held
#print axioms Wormhole.checked_instance_live_work_advances
#print axioms Wormhole.checked_transaction_completes
#print axioms Wormhole.waiting_has_budget
#print axioms Wormhole.bound_no_contention
#print axioms Wormhole.TwoTargets.holding_unprovable
#print axioms Wormhole.TwoTargets.hang_run_fair
#print axioms Wormhole.TwoTargets.delivered_fair
#print axioms Wormhole.MultiHop.unprovable
#print axioms Wormhole.MultiHop.hang_run_fair
#print axioms Wormhole.MultiHop.goal_false
#print axioms Wormhole.checked_transaction_delivered
#print axioms Wormhole.Requests.no_leak
#print axioms Wormhole.answers_run
#print axioms Wormhole.no_restart
#print axioms Wormhole.Requests.no_phantom
#print axioms ToyChain.toy_deadlockFree
