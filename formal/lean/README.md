# Lean transport model

This directory contains machine-checked theorems about a model of the NocLoom
transport.
It uses Lean core with the version pinned in `lean-toolchain`.

The theorems cover the model and its certificate. They do not establish
equivalence between generated RTL and the model.
Simulation and bounded solver results on RTL provide separate evidence.

## Build and check

```sh
lake build
python3 tools/run_checks.py
```

`run_checks.py` rejects missing `lean` or `lake`, and any Lean version that
differs from the pinned version.
It writes beneath `.lake/build/checks`, or `--work DIR`, and deletes nothing.
CTest registers it as `formal_lean` in tier `release`.

`dependency_certificate_import` checks generated four-column edges without
Lean and has the `quick` and `ci` labels.
`dependency_certificate_lean` checks generated certificates and rank rejection
with Lean. It has the `formal` and `release` labels and fails if Lean or lake
is missing or the Lean version differs from the pinned version.

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
destination. `Step.cut` ends the segment otherwise.
An endpoint lacking `contig` and `ahead` requires `mayHold`, `cross` and the
corresponding graph edges.

`Fair` assumes fair arbitration, enabled transfers, finite segments, endpoint
answers and eventual context availability.
Some clauses state that a wait ends or identifies a stuck head. These are
assumptions about a run.

`holder_gets_word` derives progress for a holder inside a coherent stream.

## Fair and generated Verilog

Each clause of `Fair`, and each sink that `room` treats as free, needs a source
in generated Verilog. The bounded liveness test `tests/run_liveness_formal.py`
assumes F1 to F4 about the endpoints and checks the rest on the Verilog.

| Clause | Given by | Liveness test |
| --- | --- | --- |
| `arb_fair` | An output arbiter with a wait bound. Test `arbiter_library_formal` proves the bound for `round_robin`, `fifo` and `rotating_priority`. The completion scope withdraws the theorems for `fixed_priority` and scheduling levels | Checked. The shapes use `round_robin` |
| `fire_fair` | Valid and ready handshakes while every clock runs | Checked |
| `seg_finite` | Packets of bounded length | Checked |
| `cont` | Write data from sources and response words from targets | F2, F4 |
| `pool_fair` | Targets that answer, so that their contexts free | F4 |
| `answer` | Targets that answer. The completion scope withdraws the theorems for a timeout without `completion` | F4 |
| `room` at a target context | Targets that take a request while they have room | F3 |
| `room` at a source sink | Sources that take responses | F1 |

The graph and model tables come from one plan. Rule coverage checks their
consistency, without comparing their behavior against RTL.
`wfCheck` requires response coverage in `answers`. The hanging examples
demonstrate two hangs satisfying `Fair`, without characterizing every possible
hang.

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

Context points rank above graph nodes and block no resource. They need no
artifact edge.
A live transaction holds a `ctx` point while a word occupies a slot, or a `wait`
point otherwise.

Each transaction event reduces its variant: remaining uninjected words times
`hopMax`, plus remaining transport steps.
Under `Fair`, held points eventually advance and the transaction completes.

`tools/edge_use.py` classifies graph edges as `used`, `helper`, `environment`,
`summary` or `outside`.
Unused edges still constrain rank. Removing one does not establish that its
hardware wait is irrelevant.

An unrequired edge between hold points is `unexplained` and fails the check. The
tool checks three artifacts in `tests/data`.

## Theorem scope

| Theorem | Statement |
| --- | --- |
| `noClosedResourceWait` | W2 excludes a closed blocked set on a legal run |
| `no_stall` | W2 and W3 exclude a held point stalling forever on a compliant run |
| `advances_on_every_compliant_run` | A held point advances with its owner under W2 and W3 |
| `deadlock_free` | W2, W3, W6 and W7 give pending work a compliant advancing run |
| `Wormhole.structural` | A checked instance satisfies W2 |
| `Wormhole.instanceLemmas` | `Fair` supplies W3 |
| `Wormhole.liveWork_held` | Live work holds a point |
| `Wormhole.checked_transaction_completes` | Every live transaction ends on a legal `Fair` run |
| `Wormhole.checked_transaction_delivered` | An accepted token ends with no words in slots. Words leave only through sinks |
| `Wormhole.answers_run` | Response words belong to live transactions whose requests arrived and return to their requesters |
| `Wormhole.no_restart` | An ended token cannot be accepted again |

A token denotes one transaction, not a reusable bus ID. `Step.inject` declares
its word count and requires the last word to end it.
Response injection requires the whole request to arrive first. Context
accounting excludes ended transactions by definition, so it cannot expose a
context leak.

`CircuitRefinementInv` in `Refinement.lean` states refinement from a circuit.
One circuit step maps to one or more model steps inside a circuit invariant.

`map_legal` and `stall_map` carry a legal circuit run and a circuit stall to
the model. `CircuitRefinement` is the case of one model step per circuit step.
Neither has an instance that connects this model to RTL.
`ServiceContinuation` holds for `Toy`. It is an open obligation for `Wormhole`,
so `deadlock_free` cannot establish its RTL deadlock freedom.

`advances_on_every_compliant_run` does not require `ServiceContinuation`.

| Hardware detail | Model limitation |
| --- | --- |
| Per-target K and separate read/write pools | One context count per response slot. AXI4 and AXI3 targets have K. Other targets free a context when the bus completes, so they have K plus the slot capacity. AXI4-Lite direction changes are not modeled |
| FIFO occupancy and pipeline stages | One capacity per slot: the link pipe slots in front of its head and a target's response pipe, at least 1. Width adapters and crossings do not count |
| W channel | Request words follow `Step.inject` rules without an RTL comparison |
| Split packets | Pieces of one burst have no shared relation |
| Source pools | Uninjected words hold no resource. Source grant retention while waiting for a pool is unchecked |
| Gather, merge, ID tables, gating, wake-up, resets, width conversion and locked transfers | No corresponding model behavior |

These theorems establish neither data correctness nor timing bounds. A target
that never answers violates `Fair`.

## Trust and instances

`GenericNoC/` contains no `sorry` or `axiom`. Library theorems use `propext`,
`Classical.choice` and `Quot.sound`.

| Edge-table size and option | Certificate | Rule coverage |
| --- | --- | --- |
| Up to 3000 edges | Kernel chunks | Kernel |
| Larger, default | `native_decide` | `native_decide` |
| Larger, `--kernel` | Kernel chunks | `native_decide` |

In the kernel, edges, ranks and node keys are numbers of 32-bit fields. The
kernel reads them with big-number arithmetic, and the file checks one theorem
at a time.

`native_decide` adds trust in `Lean.ofReduceBool` and `Lean.trustCompiler`.
Contention pairs grow cubically without helper nodes. With `contention` rows,
`Wormhole.row_paths` derives pairs from checked helper edges.

`tools/gen_instance.py` converts a `nocloom.dependency` artifact to Lean. With a
`model` table in the artifact or a `--model` file, it also checks the transport
rules. Without a model, or with `--graph-only`, it checks graph acyclicity only.

Use the CLI output `nocloom_<name>.dependency.json` as `GRAPH.json`.
See [output files](../../docs/outputs.md).
From this directory, convert the artifact and check the proof with Lean:

```sh
python3 tools/gen_instance.py GRAPH.json Network OUT.lean
lake env lean OUT.lean
```

| Import field | Supported value |
| --- | --- |
| `format` | `nocloom.dependency` |
| `format_version` | JSON integer `1`. The importer rejects booleans and floats |
| `orientation` | `blocker_to_blocked` |
| `result.status` | `no_cycle` |
| `completion` | `applies`, `theorems` `["completes", "delivered"]` and `unmet` rows of `clause`, `subject` and `reason` |
| `nodes` | Nodes with a `rank` checked by Lean |
| `edges` | Four columns: `[source, destination, rule, group]` |
| Edge `group` | Nonnegative integer. Only `0`, an ordinary edge, is supported |
| Top-level `groups` | Absent or empty |

Each edge points from a blocker to a blocked node. Source and destination are
node indexes. The rule identifies the wait relation.
The importer sorts and removes duplicate source and destination pairs.
It checks every fourth column before it removes duplicates.

An edge with a nonzero group fails with
`NCL_CERTIFICATE_ALTERNATIVE_GROUP`. An edge with a wrong column count or a
group that is not a nonnegative integer fails with
`NCL_CERTIFICATE_EDGE_FORMAT`. A nonempty top-level `groups` or a status other
than `no_cycle` also fails conversion. These failures return exit code `3`
before creating or changing the proof source, including with `--graph-only`
and `--kernel`. Exit code `0` means the source was written. Exit code `2`
reports argument parsing errors.

Conversion does not validate a certificate. Lean must accept the generated
proof. Ranks that do not increase along an edge fail a certificate theorem
(`chunk` in the kernel, `certificate` with `native_decide`) even when the
artifact claims `no_cycle`.

`Instances/TwoTargets.lean` compares `perWord`, `contiguous` and `holding`
release policies. The first two satisfy the rules.
`holding` cannot satisfy W2 with any graph. `Instances/MultiHop.lean` gives a
corresponding multi-hop hang.

`Instances/Requests.lean` contains a two-word request and its answer.
Files in `tests/fail` and designated `tests/data` cases must fail for the reason
checked by `run_checks.py`.

Alternative groups have no model representation. A timeout rule requires timer
expiry in `env` and a corresponding rule lemma.
During the timer, the slow blocker contributes a bounded `ready` delay. Only the
certificate-selected branch contributes a `need`.
