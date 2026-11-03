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

`CircuitRefinement` has no instance connecting this model to RTL.
`ServiceContinuation` holds for `Toy`. It is an open obligation for `Wormhole`,
so `deadlock_free` cannot establish its RTL deadlock freedom.

`advances_on_every_compliant_run` does not require `ServiceContinuation`.

| Hardware detail | Model limitation |
| --- | --- |
| Per-target K and separate read/write pools | One capacity per response-slot pool |
| FIFO occupancy and pipeline stages | One capacity per instance. Stages between outputs are not slots |
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
| Up to 3000 edges | Kernel | Kernel |
| Larger, default | `native_decide` | `native_decide` |
| Larger, `--kernel` | Kernel chunks | `native_decide` |

`native_decide` adds trust in `Lean.ofReduceBool` and `Lean.trustCompiler`.
Contention pairs grow cubically without helper nodes. With `contention` rows,
`Wormhole.row_paths` derives pairs from checked helper edges.

`tools/gen_instance.py` converts a `nocloom.dependency` artifact to Lean. With a
model file, it also checks the transport rules.
Without a model file, it checks graph acyclicity only. It rejects cycles and
or-groups.

`Instances/TwoTargets.lean` compares `perWord`, `contiguous` and `holding`
release policies. The first two satisfy the rules.
`holding` cannot satisfy W2 with any graph. `Instances/MultiHop.lean` gives a
corresponding multi-hop hang.

`Instances/Requests.lean` contains a two-word request and its answer.
Files in `tests/fail` and designated `tests/data` cases must fail for the reason
checked by `run_checks.py`.

Or-groups have no model representation. A timeout rule requires timer expiry in
`env` and a corresponding rule lemma.
During the timer, the slow blocker contributes a bounded `ready` delay. Only the
certificate-selected branch contributes a `need`.
