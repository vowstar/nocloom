# Test selection and evidence

The full set is the configured CTest inventory. `suite_profiles.json` defines
the daily integration selection.
Use an explicit policy file for a focused change, with `--profile daily --policy
FILE`. Add entries with `--include NAME`.

Select each simulation engine as [SIMULATION_ENGINES.md](SIMULATION_ENGINES.md)
describes. Icarus provides portability checks. Use identical inputs when
comparing engines.
Record tool limitations and diagnose functional differences. A failing entry
never changes engines automatically.

## Select checks

| Change | Selection |
| --- | --- |
| Prose, links or metadata | Consistency, link and scope checks |
| Local implementation | Affected build targets, unit and configuration checks, and static analysis |
| Feature or fix | Affected contracts, legal boundaries, transactions, fault detection and interacting cases |
| Shared behavior | Affected consumers, configuration branches and representative interactions |
| Release requiring full execution | Every entry in the fixed inventory and the release's artifact obligations |

Shared IR, emitter, template, adapter and compiler changes require impact
analysis. Expand the selection when the affected set is uncertain.
Record changed contracts, selected names, fixture closure, new checks and
omitted coverage before execution.

Identify the primary engine selection and auxiliary checks. Include an Icarus
representative for changed benches when supported.
Absent tool support is an open portability obligation. It does not count as
PASS. C++ checks need one execution.

Fix the source revision and record compiler, simulator and driver configuration.
Include uncommitted inputs in the candidate manifest.
Retain failures, their causes and recovery attempts as separate records. Do not
weaken assertions to obtain a pass.

Required boundary cases stay required. Small instances cannot replace width,
capacity or clock-crossing boundaries that affect the changed behavior.
Reduce duplicate compilation or share immutable fixtures before reducing
coverage. Choose coverage reductions before reading results.

A changed checker needs a legal witness, its designated failing fault and
recovery on the same inputs.
Prior results require an unchanged, explicitly bound dependency and input
closure. Timestamps and test names do not establish that closure.

Byte equality establishes equality only for the compared artifacts. It does not
establish behavioral or physical equivalence.

## Full execution

Run `--profile full` when the release requires the complete inventory. Fix one
source revision before the run.
Retain its inventory, source manifest, dependency versions, tool versions and
resource plan. Results belong to the revision that ran.

| Evidence | Required record |
| --- | --- |
| Inventory | Every entry classified as PASS, FAIL, TIMEOUT or NOT_RUN per selected engine |
| Engine differences | Required engine, actual engine, reason and original failure |
| Public behavior | Relevant byte gates, generation, installation, consumer and rejection checks |
| Fault detection | Designated fault, triggering input and checker result |
| Bindings | Source, DUT, bench, stimulus, checker and result hashes |
| Solver outcomes | Proof, reachability, unknown and timeout stated separately |
| Non-passes | Product, harness, environment, resource and budget causes with evidence |

An unreachable guard does not establish scenario coverage. A solver timeout does
not establish a proof.
After a fix, execute its reproducer and affected checks. Rebind inherited
results explicitly.

A combined record identifies every source revision. It cannot claim complete
execution on the final revision unless that execution occurred.
Further execution requires changed inputs, a diagnostic hypothesis or another
concrete information gain.

## Persistent attempt records

`run_suite.py` records live non-passes and imports final JUnit results into
`suite_history.py`.
Use `--history` on persistent storage. The default is
`$XDG_STATE_HOME/nocloom/suites` or `~/.local/state/nocloom/suites`.

`--candidate-manifest FILE` and `--environment-manifest FILE` bind revision and
tool configuration. The producer must check their input closure.
Without bindings, differing outcomes do not establish instability on one
candidate.

History is append-only. Identical reimports are idempotent. Conflicting records
under one run identifier fail.

Reports include attempt counts, non-pass rate, time median and p95, sampled peak
RSS and attempt references.
`NOT_RUN`, exclusions and provisional observations do not enter the
executed-attempt denominator.

A recurring non-pass flag requires five attempts, two non-passes and a rate of
at least 20%. It is a triage signal.

Causes begin as `UNTRIAGED`. Assign `FUNCTIONAL_ASSERTION`, `TEST_HARNESS`,
`TOOLCHAIN_ENV`, `TIMEOUT_BUDGET`, `RESOURCE_INTERRUPTION` or `INFRASTRUCTURE`
from evidence.
A failed fault-detection entry does not establish that its injected fault was
caught. Retain the raw log and status.

```sh
python3 tests/suite_history.py --store results.local/history ingest --run results.local/run
python3 tests/suite_history.py --store results.local/history report --output results.local/statistics-new
python3 tests/suite_history.py --store results.local/history triage --file results.local/triage.json
```

Triage records require `run_id`, `engine`, `name`, `cause`, `reason`,
`reviewer`, `recorded_utc` and `evidence` path/hash anchors.
Corrections append records. Keep synthetic runner-contract results outside real
project history.

## Test audit records

Each finished invocation queues at most 16 entries. Half prioritize oldest or
unseen entries, and half prioritize non-passes and cost.
A queue creates no test change or acceptance decision.

| Record field | Question |
| --- | --- |
| Retain | Which contract, reachable scenario and defect witness depend on this entry? |
| Simplify or remove | Which work duplicates another entry, and what evidence supports equivalence? |
| Add | Which defect escapes the checks, and what minimal stimulus detects it? |
| Adversary | Which source or trace evidence contradicts either argument? |

Decisions are `KEEP`, `SIMPLIFY`, `MERGE`, `ADD`, `RECLASSIFY` or `REMOVE`.
Record owner, reviewer, evidence, risk and acceptance criteria. Changes require
a validation plan and a coverage-preservation argument.

Keep legal baseline, fault and recovery witnesses when simplifying checks.
Duration and pass rate alone do not justify removal.

```sh
python3 tests/audit_suite.py --store results.local/history rotate \
  --inventory results.local/run/iverilog-inventory.json \
  --policy tests/suite_profiles.json --output results.local/review-new --limit 16
python3 tests/audit_suite.py --store results.local/history record --file results.local/review.json
```

`queue.json` and `REVIEW.md` contain questions and context. Records bind
`batch_id`, `name` and all four nonempty view fields.
They also require `decision`, `owner`, `reviewer`, `recorded_utc`, `acceptance`,
`risk` and path/hash `evidence`.

Changes require `validation_plan` and `coverage_preservation`.

## Resources and storage

Use the shared CPU and memory allocation described in [SUITES.md](SUITES.md).
Increase admission only with sustained headroom.
Reduce or pause admission under pressure. Preserve entry timeouts, fixtures and
dependencies. Distinguish kernel caps from sampled guards.

Collect summary hashes and required case inputs, logs, events and
counterexamples before removing owned build artifacts.
History and audit tools do not delete evidence. Statistical reports do not
trigger simulation.
