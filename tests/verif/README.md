# Transaction-level verification

This directory contains bus functional models and online monitors in `sv/`, plus
the tape interpreter, offline model and checker in `nlverif/`.
`nlverif/run.py` drives RTL and injected-fault cases. Verdicts come from exit
codes and `results.json`.

## Register cases

Register each case with `nocloom_verif_case()` in `cases.cmake`.
Declare one layer, one tier, bounded `TIMEOUT`, a `MEMORY_MB` estimate and
`CONTRACTS` for RTL and fault-detection entries.

Use the shared drivers and bench generator for new cases.

## Drivers and case files

| Driver | Layer | Case file |
| --- | --- | --- |
| `nlverif/run.py` | `rtl`, `negative` | `CASE` defines DUT, BFMs, model and faults. `scenario(tape, seed, ...)` supplies stimulus |
| `install.py` | `install` | `ENTRIES` checks a relocated installation. `Context.verify` checks generated RTL using a `CASE`-shaped dictionary and scenario |
| `config.py` | `config`, `structure` | `CASES` defines CLI inputs, acceptance or rejection, diagnostics and artifact checks |

Accepted configuration cases repeat generation from another directory and
compare bytes. A second compiler's CLI can provide another comparison.

A case reads `--param KEY=VALUE` values from `PARAMS`. Registration loads the
case with those values.
A `CASE` can list `seeds` for additional tapes on one build and `configs` for
companion DUTs using the same scenario.

Fault checks use the first configuration unless `negative_faults_all` selects
each configuration. Each configuration can declare its own `faults`.

DUTs come from the CLI or a recipe in `fixture/nlv_fixture.cpp`. Recipes can
declare probe groups for clock-by-clock logging.
`probe_checks` replays those probes against a model, such as
`tests/treadle_model.py`. CLI cases can name internal signals with `map_probes`.

Text-fault probes read the module copy selected by `+NLV_FAULT`.
A case's `oracle` checks additional behavior from the recorded events.
`cases/ahb_shape.py` models AHB transfer shape independently.

## Faults and offline checks

| Fault kind | Operations |
| --- | --- |
| IR | `set_next`, `set_operand`, `replace`, `set_driver`, `variant` through fixture recipes |
| Text | `text` regular expressions with exact match counts, one module copy per fault |
| Scenario | `scenario` supplies tape parameters that violate an endpoint premise without changing RTL |

`tape.check(kind=...)` supports `interpose`, `first`, `reorder`, `stream_beats`,
`rate`, `span` and `reach`.
Every run checks attribution, footprints, byte-write multisets, unstrobed bytes,
read data, responses, causality, target bursts and source read-ID order.

Read bursts with different IDs can interleave. Every strobed target byte must
appear exactly once.

A fault declares the online rule or offline mismatch kinds that can detect it.
`KIND:w` and `KIND:r` restrict detection to one direction.

A `TAPE.*` rule requires the designated wait label, such as
`TAPE.WAIT_TIMEOUT:PROGRESS`.
Timeouts, tool errors and bench self-check failures do not count as detected
design faults.

A timed-out AXI wait first logs channel state as a `stall` event. The first rule
can name an endpoint premise violation.

| Rule | Endpoint behavior |
| --- | --- |
| `LIVE.E1` | The next W beat of a started write waits for R while that source has an unanswered read |
| `LIVE.E2` | The source holds RREADY low while its write waits for B |
| `LIVE.E3` | A target holds WREADY low while waiting for its own reads |

The verdict retains the wait label as `hang_rule`. It must match
`TAPE.WAIT_TIMEOUT:<label>`.
A scenario fault is detected only when every declared expectation matches,
including its liveness rule and designated timed-out wait.

## Checks and complexity limits

| Entry | Tier | Scope |
| --- | --- | --- |
| `verif_selftest` | `quick` | Burst model, tape encoding, offline mismatch kinds, X/Z repair, resets, fault classification and bench generation |
| `verif_registry` | `quick` | Labels, limits, orphan files, case contracts, online and offline rule coverage, wait labels and retained-family records |
| `verif_rulecheck` | `focused` | Each online rule fails first in its designated `sv/nlv_rule_stub.v` mode. Legal input passes monitors and offline checks |
