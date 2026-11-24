# Lean transport model

This directory holds a Lean model of the NocLoom transport and theorems about
that model. It also checks the rank certificate of each generated dependency
file. Ranks that rise along every edge exclude a wait cycle. The tables also
list the solver checks and the netlist check of generated Verilog.

For dependency files above 3000 distinct edges, `native_decide` trusts the
Lean compiler as well as the kernel. `lean-toolchain` pins the Lean version. See
[Fair and generated Verilog](#fair-and-generated-verilog) for run assumptions.

## What is checked

| Claim | Object | Check | Limit |
| --- | --- | --- | --- |
| On a legal run that satisfies `Fair`, every live transaction ends and its words leave the network | The model built from the model table of one dependency file | `Wormhole.checked_transaction_completes` and `Wormhole.checked_transaction_delivered`, test `formal_lean` | The file must pass the rule check |
| Every reachable state has a legal run through it that satisfies `Fair` on every suffix | The Wormhole model | `Wormhole.serviceContinuation_of_finiteDrain`, test `formal_lean` | Each reachable state needs a finite legal path to empty queues, free grants and no live transactions. Each response pool needs positive capacity. No model of a generated network is known to meet the drain premise, see the next row |
| There is no fair continuation through the state after a non-tail request word is accepted with a declared count of one remaining word | The model of `examples/axi4_exchange.yaml` | `Wormhole.no_serviceContinuation_of_short_request`, test `dependency_certificate_lean` | Its tables pass the rank and rule checks and its `completion.applies` is true |
| A dependency file whose ranks rise along every edge has no wait cycle, and its model table passes the rule check | `nocloom_<name>.dependency.json` | `tools/gen_instance.py` and `lake env lean`, tests `dependency_certificate_lean` and `formal_lean_examples` | Every example and the 11 liveness shapes. A file without a model table gets the certificate only |
| A dependency file sets `completion.applies` to false for four conditions that break `Fair`. With a model table, the Lean file then states `completion_withdrawn` and not `completes` | The `completion` field of the dependency file | The `[dependency]` cases of tests `unit_*`, test `dependency_certificate_import` | The generator decides the field from the network plan. Lean does not check it |
| Each `cross` slot can cross in the hardware. A `bound` source has one direction and no ID. The response release equals its release cone | The netlist that the generator prints as Verilog | Test `dependency_release_cone` and the `[dependency]` cases of tests `unit_*` | A structural comparison in one direction. Routes over a link with a pipe, width adapter or clock crossing are skipped, and so are the `scale_*` examples |
| While a request is open, some handshake occurs within 10 cycles | Generated Verilog of 11 AXI4 network shapes | Tests `liveness_formal_*` with SymbiYosys | A bounded check to depth 24 by default. A case name gives another depth, and L4 and L8_merged_contexts stop before most traffic ends. On L1 and L2, F1 to F4 are each needed, keep the states they must allow, and lose them in a stronger form. Data is not checked |
| A known hang is detected | The `holding` release policy of `GenericNoC/Instances/TwoTargets.lean`, which keeps a grant across segments, the same fault in generated Verilog, and its graph form | `GenericNoC/Instances/TwoTargets.lean`, `GenericNoC/Instances/MultiHop.lean`, tests `liveness_formal_hang_*` and `hang_controls` | One fault on four shapes. Its graph form is a cycle on the same shapes. This does not describe every hang |
| Each net and output port of the printed top module carries the netlist value of the module inputs, registers and cell outputs | Generated Verilog and its netlist, see [netlist](../../docs/netlist.md) | `NetlistCheck.check_sound`, test `netlist_roundtrip` | Every example and liveness shape. Registers, clock gates and synchronizer cells are compared one by one. Cell files are not read |
| A checked edge takes zero or more transport steps and preserves the matching invariant | The [AXI4 read refinement map](../../docs/read-refinement.md) and its model | `NetlistCheck.ReadRefinement.edge_sound`, test `read_refinement` | Single-beat reads on a direct path. The test checks an idle edge and a context fault |

## What has no proof

| Statement | Status | Work needed for a proof |
| --- | --- | --- |
| The generated Verilog of a direct AXI4 pair passes the edge check on every edge | Test `read_refinement` proves the cycle assertions with SymbiYosys on two designs, without a depth bound but under endpoint assumptions. The test rewrites the assertions for the solver. No checked link carries the result into Lean | A Lean statement of the solver result, or a refinement proof of the Verilog |
| The full generated network refines the model | No proof of `CircuitRefinementInv` for a network | A proof covering all transport paths and hardware features of the network |
| Every generated Verilog network is free of deadlock | The checked AXI4 exchange model has a reachable state with no finite drain | A service continuation proof for the intended model states and refinement of the full network |
| A network with unmet clauses in its `completion` field completes its transactions | No completion theorem is stated. The statement does not hold for every run | None. See [output files](../../docs/outputs.md#dependency-graph) for the settings that remove each unmet clause |
| The four conditions of the `completion` field are all the ways generated logic breaks `Fair` | Each clause has a reason in the table below, not a proof | A proof of each clause for the arbiters, targets and clocks of generated Verilog |
| A network without a model table completes its transactions | No model table, so no rule check and no theorem | A model that states more than 4096 endpoint pairs, several outputs for one destination, and bound sources with several paths |
| Slot capacities and context counts equal the storage of generated Verilog | A rule derives them from the network plan. They are not compared with the Verilog | A comparison with the storage of generated Verilog |
| Generated Verilog answers every request without a depth bound | Bounded checks only | Unbounded checks on the small shapes |
| Data correctness and timing bounds | Outside the model | Transaction simulation checks data on its configurations. See [test suites](../../tests/SUITES.md) |

## Build and check

```sh
lake build
python3 tools/run_checks.py
```

`run_checks.py` rejects missing `lean` or `lake` and another Lean version. It
writes beneath `.lake/build/checks`, or `--work DIR`, and deletes nothing. CTest
registers `formal_lean` and `netlist_roundtrip` in tier `release`.

`dependency_certificate_import` (`quick`, `ci`) checks edge import without
Lean. `dependency_certificate_lean` (`formal`, `release`) checks certificates
and rank rejection with Lean, and fails without Lean or with another version.

## Model and assumptions

`Wormhole.lean` defines a transition system. Each slot presents one head word.
An output belongs to one input until a word with `tail` set passes.

Downstream storage is a bounded slot, a bounded target-context pool or a sink.
A word without `tail` retains the grant. The holder needs its next word to keep
the same destination.

| Declaration | Meaning | Evidence and limitation |
| --- | --- | --- |
| `cross` | Words can change destination while the slot holds a grant | `check_declarations.py` compares interleaving targets and their consumers with the generated map. It cannot detect a wrong plan and map |
| `contig` | Injection keeps the destination of the open stream | Declaration checks exclude conflicting target behavior. They do not check request streams |
| `ahead` | A segment continues only when its next word is present for the same destination | Declaration checks and two response-release shapes provide limited RTL evidence |
| `bound` | A source waits for its answers before requesting another target | Declaration checks inspect ID width and direction. They do not inspect the source's request-hold logic |

`Step.fire` continues a segment only with a following word for the same
destination. `Step.cut` ends the segment otherwise. An endpoint lacking
`contig` and `ahead` requires `mayHold`, `cross` and the matching graph edges.

## Fair and generated Verilog

`Fair` has six clauses. `room` is the model test for space behind an output.
Each clause, and each place where `room` finds space without a check, needs a
source in generated Verilog or in the endpoints. `tests/run_liveness_formal.py`
assumes F1 to F4 about the endpoints and checks the rest on the Verilog.

| Label | Endpoint assumption of the liveness test |
| --- | --- |
| F1 | Sources raise RREADY and BREADY within 1 cycle, 4 cycles on L4 |
| F2 | W comes within 1 cycle of its AW |
| F3 | Targets raise ARREADY, AWREADY and WREADY within 1 cycle while they have room |
| F4 | A target with open reads offers a word within 1 cycle, and passes over an open read that can go at most two times |

| Clause or `room` case | Given by | In the liveness test |
| --- | --- | --- |
| `arb_fair` | An output arbiter with a wait bound. Test `arbiter_library_formal` proves the bound for `round_robin`, `fifo` and `rotating_priority` with 2 to 5 candidates, `fifo` also with 8 | Checked on Verilog with `round_robin` |
| `fire_fair` | Valid and ready handshakes while every clock runs | Checked on Verilog |
| `seg_finite` | Packets of bounded length | Checked on Verilog |
| `cont` | Write data from sources and response words from targets | Assumed: F2, F4 |
| `pool_fair` | Targets that answer, so that their contexts free | Assumed: F4 |
| `answer` | Targets that answer | Assumed: F4 |
| `room` at a target context | Targets that take a request while they have room | Assumed: F3 |
| `room` at a source | Sources that take responses | Assumed: F1 |

The `completion` field of the dependency file names the clauses that a network
does not meet. See [output files](../../docs/outputs.md#dependency-graph).

The graph and model tables come from one plan. Rule coverage checks their
consistency, without comparing their behavior against RTL.
`wfCheck` requires response coverage in `answers`.

## Graph rules

Each blocking condition must have its required graph connection. The rule lemmas
establish required pairs, and `checkCoverage` checks their connections.

| Rule | Blocking resource | Waiting resource | Lemma |
| --- | --- | --- | --- |
| `arbitration` | Another input's grant | Head word | `block_arbitration` |
| `nextWord` | Downstream head | Head word | `block_nextWord` |
| `nextGrant` | Downstream head | Held grant | `block_nextGrant` |
| `poolWord` | Response head | Head word | `block_poolWord` |
| `poolGrant` | Response head | Held grant | `block_poolGrant` |
| `holdAcross` | Head at or upstream of a holder that can change destination | Held grant | `block_holdAcross` |
| `context` | Slot head | Transaction in or owed by the slot | `head_routed`, `head_in_range` |

Context points rank above graph nodes, block no resource and need no edge. A
live transaction holds a `ctx` point while a word occupies a slot, or a `wait`
point otherwise.

Each transaction event reduces its variant: remaining uninjected words times
`hopMax`, plus remaining transport steps.
Under `Fair`, held points eventually advance and the transaction completes.

`tools/edge_use.py` classifies graph edges as `used`, `helper`, `environment`,
`summary` or `outside`. Unused edges still constrain rank. An unrequired edge
between hold points is `unexplained` and fails the check.

## Theorem scope

| Theorem | Statement |
| --- | --- |
| `noClosedResourceWait` | W2 excludes a closed blocked set on a legal run |
| `no_stall` | W2 and W3 exclude a held point stalling forever on a compliant run |
| `advances_on_every_compliant_run` | A held point advances with its owner under W2 and W3 |
| `deadlock_free` | W2, W3, W6 and W7 give pending work a compliant advancing run |
| `Wormhole.checked_deadlock_free_of_finiteDrain` | Checked tables, positive response pool capacities and a finite drain from each reachable state give pending work a fair advancing run. No model of a generated network is known to meet the drain premise |
| `Wormhole.structural` | A checked instance satisfies W2 |
| `Wormhole.instanceLemmas` | `Fair` supplies W3 |
| `Wormhole.liveWork_held` | Live work holds a point |
| `Wormhole.checked_transaction_completes` | Every live transaction ends on a legal `Fair` run |
| `Wormhole.checked_transaction_delivered` | An accepted token ends with no words in slots. Words leave only through sinks |
| `Wormhole.answers_run` | Response words belong to live transactions whose requests arrived and return to their requesters |
| `Wormhole.no_restart` | An ended token cannot be accepted again |

A token denotes one transaction, not a reusable bus ID. `Step.inject` declares
its word count and requires the last word to end it. Response injection requires
the whole request to arrive first. Context accounting excludes ended
transactions by definition, so it cannot expose a context leak. A target that
never answers violates `Fair`.

`CircuitRefinementInv` maps each circuit step to one or more model steps in an invariant.

`map_legal` and `stall_map` carry a legal circuit run and a circuit stall to
the model. `CircuitRefinement` uses one model step per circuit step.
`Toy` satisfies `ServiceContinuation`.

| Hardware detail | Model limitation |
| --- | --- |
| Per-target K and separate read/write pools | One context count per response slot. AXI4 and AXI3 targets have K. Other targets free a context when the bus completes, so they have K plus the slot capacity. AXI4-Lite direction changes are not modeled |
| FIFO occupancy and pipeline stages | One capacity per slot: the link pipe slots in front of its head and a target's response pipe, at least 1. Width adapters and crossings do not count |
| W channel | Request words follow `Step.inject` rules without an RTL comparison |
| Split packets | Pieces of one burst have no shared relation |
| Source pools | Uninjected words hold no resource. Source grant retention while waiting for a pool is unchecked |
| Gather, merge, ID tables, gating, wake-up, resets, width conversion and locked transfers | No corresponding model behavior |

## Trust and instances

`GenericNoC/` contains no `sorry` or `axiom`. Library theorems use `propext`,
`Classical.choice` and `Quot.sound`.

| Edge-table size and option | Certificate | Rule coverage |
| --- | --- | --- |
| Up to 3000 edges | Kernel chunks | Kernel |
| Larger, default | `native_decide` | `native_decide` |
| Larger, `--kernel` | Kernel chunks | `native_decide` |

In the kernel, edges, ranks and node keys are numbers of 32-bit fields, read
with big-number arithmetic. The file checks one theorem at a time.

`native_decide` adds trust in `Lean.ofReduceBool` and `Lean.trustCompiler`.
Contention pairs grow cubically without helper nodes. With `contention` rows,
`Wormhole.row_paths` derives pairs from checked helper edges.

`tools/gen_instance.py` converts a `nocloom.dependency` artifact to Lean. With a
`model` table in the artifact or a `--model` file, it also checks the transport
rules. Without a model, or with `--graph-only`, it checks graph acyclicity only.

Use the CLI output `nocloom_<name>.dependency.json` as `GRAPH.json`, see
[output files](../../docs/outputs.md). From this directory, run:

```sh
python3 tools/gen_instance.py GRAPH.json Network OUT.lean
lake env lean OUT.lean
```

| Import field | Supported value |
| --- | --- |
| `format` | `nocloom.dependency` |
| `format_version` | JSON integer `2`. The importer rejects booleans and floats |
| `orientation` | `blocker_to_blocked` |
| `result.status` | `no_cycle` |
| `completion` | `applies`, `theorems` `["completes", "delivered"]` and `unmet` rows of `clause`, `subject` and `reason` |
| `nodes` | Nodes with a nonnegative integer `rank`, checked by Lean |
| `edges` | Four columns: `[source, destination, rule, group]` |
| Edge `group` | Nonnegative integer. Only `0`, an ordinary edge, is supported |
| Top-level `groups` | Absent or empty |

Each edge points from a blocker to a blocked node. Source and destination are
node indexes. The rule identifies the wait relation.
The importer sorts and removes duplicate source and destination pairs.
It checks every fourth column before it removes duplicates.

An edge with a nonzero group fails with `NCL_CERTIFICATE_ALTERNATIVE_GROUP`. An
edge with a wrong column count or a group that is not a nonnegative integer
fails with `NCL_CERTIFICATE_EDGE_FORMAT`. A nonempty top-level `groups` or a
status other than `no_cycle` also fails conversion.

A missing, malformed or contradictory `completion` field fails with
`NCL_CERTIFICATE_SCOPE`. With a model table, `caps` and `pool_caps` must give a
positive value for exactly each slot and response slot. Each failure in this
section returns exit code `3` before it creates or changes the proof source,
also with `--graph-only` and `--kernel`. Exit code `0` means the source was
written. Exit code `2` reports argument parsing errors.

Conversion does not validate a certificate. Lean must accept the generated
proof. Ranks that do not increase along an edge fail a certificate theorem
(`chunk` in the kernel, `certificate` with `native_decide`) even when the
artifact claims `no_cycle`.

`GenericNoC/Instances/TwoTargets.lean` compares `perWord`, `contiguous` and
`holding` release policies. The first two satisfy the rules. `holding` cannot
satisfy W2 with any graph. `GenericNoC/Instances/MultiHop.lean` gives a
corresponding multi-hop hang.

`GenericNoC/Instances/Requests.lean` contains a two-word request and its answer.
Files in `tests/fail` and designated `tests/data` cases must fail for the reason
checked by `run_checks.py`.

Alternative groups have no model representation. A timeout rule requires timer
expiry in `env` and a corresponding rule lemma.
During the timer, the slow blocker contributes a bounded `ready` delay. Only the
certificate-selected branch contributes a `need`.
