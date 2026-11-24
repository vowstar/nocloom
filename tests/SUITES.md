# Test tiers

Configure and build NocLoom with tests enabled before running a tier.
Set the number of jobs to suit the available processors and memory.

| When | Command | Selection |
| --- | --- | --- |
| After editing | `ctest --test-dir build -L '^edit$' --output-on-failure` | Quick checks and short simulations |
| Before a commit | `python3 tests/run_tier.py build precommit --area '^tilelink_uh_target_watchdog'` | Edit checks, unit and configuration tests, then focused cases that match the area |
| Before pushing | `ctest --test-dir build -L '^ci$' --output-on-failure` | The Build workflow selection |
| At release | `ctest --test-dir build --output-on-failure` | The full configured inventory, including formal, soak, scale and install tests |

Replace the area expression with a CTest name regex for the changed area.
The runner skips tests already in `precommit` and tests marked `release`
when it selects the additional focused checks.
Run a release check directly by name when a change affects its scope.

The `release` label identifies tests reserved for release. Run the full
inventory without a label filter for each release engine configuration.
Power, performance and area tests use a separate setup.

The CI selection runs the AHB5 property matrix and the liveness proof driver
self-test. It also runs asynchronous FIFO transactions with clock skew,
jitter and one-sided resets, and the separate read and write path cases. The
release run adds the full FIFO clock ratio matrix and the comparison of
generated files from two C++ compilers. It also runs the separate path cases
with timeouts or clocked graphs.

## Memory reservations

A test that needs much memory sets the CTest property
`RESOURCE_GROUPS "memory_mib:N"`. N covers the largest memory of its process
tree under Icarus Verilog, Verilator and an external simulator, with a margin.
CTest first runs `test_memory_specification`, which writes the capacity in MiB.

| Item | Rule |
| --- | --- |
| Capacity | The available memory, or the room under the cgroup memory limit when that is smaller, less the reserve |
| Reserve | The larger of 10% of physical memory and 2 GiB |
| Admission | A test waits while its reservation and those of the running tests exceed the capacity |
| Largest test | When the largest reservation is above the capacity, the capacity becomes that reservation |
| Override | `NOCLOOM_TEST_MEMORY_MIB` sets the capacity. With little memory, also run fewer jobs |
| Specification file | The build tree supplies it, so CTest rejects `--resource-spec-file`. These tests need CMake 3.28 or newer |

## Daily and full verification

Configure and build each simulation engine in a separate directory. Use one
source revision and compatible toolchains.
The runner selects CTest entries and executes them. It does not install tools,
copy sources, build NocLoom, or delete files.

`suite_profiles.json` defines the daily selection. The full profile selects
every entry in the configured CTest inventory. Missing daily entries fail
selection. New entries belong to full until the policy includes them in daily.
Fixtures and dependencies join the selection automatically.

Use an explicit policy for a focused change. Include its affected full-only
entries with `--include NAME`.
The [verification policy](VERIFICATION_POLICY.md) describes selection and
evidence requirements.

```json
{
  "version": 1,
  "daily": [{
    "purpose": "Width-adapter checks",
    "tests": ["width_adapter", "width_adapter_roundtrip_8x4", "width_adapter_roundtrip_16x8"]
  }]
}
```

This example selects entries. A width change also needs its affected protocol
boundaries, transactions and fault-detection checks.
There is no `--profile feature` option.

## Plan and run

```sh
python3 tests/run_suite.py --profile daily \
  --build build/first --engine iverilog \
  --build build/second --engine external --sim-config config.local/adapter.json \
  --output build/suite-plan
```

One `--build` and `--engine` pair selects one engine. Omit `--run` to write
`plan.json` without execution. The plan records selected and excluded entries,
budgets, hashes, engines and build directories. Both engine inventories require
identical test names. Bind their source inputs separately with a source
manifest.

Obtain a delegated cgroup v2 directory with the memory controller enabled before
using the default protection.

```sh
python3 tests/run_suite.py --profile daily \
  --build build/first --engine iverilog \
  --build build/second --engine external --sim-config config.local/adapter.json \
  --output build/suite-daily --cgroup-root "$DELEGATED_CGROUP" --run
```

Use `--profile full` for the complete inventory. Choose a fresh output directory
for each invocation.
The runner refuses to overwrite output or execute cgroup protection without
delegation. It requires no elevated privileges.

The owner of the delegated directory removes the runner's child cgroups after
the runner and its test processes exit. The child names follow
`nocloom-<run_id>-<engine>`. See `plan.json` for the run ID and engine names.
This also applies to groups created before a setup failure.

`--protection monitored` selects CPU affinity, process address-space limits,
aggregate RSS sampling, and memory and disk reserves.
Sampling cannot enforce an aggregate memory cap between observations. Resource
interruptions count as non-passes.

A process supervisor or `nohup` can retain a background invocation. Redirect
stdin and retain its driver log.
Read `status.json` for progress and `SUMMARY.md` or `results.json` for the final
result. JUnit determines the verdict.

The graph throughput and arbiter tests keep each invocation's artifacts in a
fresh subdirectory. Read `latest.txt` under the work directory to find it.
For the arbiter test, `--prepare` creates inputs without running the simulator
or prover. Pass the prepared directory to `--check` to evaluate its logs.

## Resource limits

Both engines share one allocation. CPU affinity, cgroup quotas, host load,
memory availability and cgroup headroom constrain it.

| Resource | Default rule |
| --- | --- |
| CPU | At most half the available capacity, reduced by host load, with at most 16 CTest slots per engine |
| Memory reserve | Larger of 16 GiB and 10% of effective total RAM |
| Invocation memory | At most half the available memory after reserve, with a 256 GiB ceiling |
| Slot estimate | 8 GiB per slot, also accounting for the largest selected `PROCESSORS` value |
| Nested builds | `CMAKE_BUILD_PARALLEL_LEVEL` at most four within inherited CPU affinity |
| Stop conditions | RSS exceeds 85% of allocation, memory reserve breached, scratch space below 16 GiB, or 48 hours elapsed |
| Cgroup memory | Per-engine `memory.max` and `memory.swap.max=0` |

The monitor samples once per second. OOM events retain their
resource-interruption classification. Adjust `--max-jobs`,
`--memory-per-slot-gib`, `--max-memory-gib`, `--cpu-fraction`, `--reserve-gib`
and `--disk-reserve-gib` from observed headroom. Insufficient resources stop
admission. A user-wide lock prevents overlapping invocations of this runner.
Account for other jobs separately.

CTest timeouts, processors, locks, environment, fixtures and commands retain
their configured meaning.

## Adaptive scheduling

`--scheduling adaptive` starts at most `--initial-jobs 2` per engine.
After `--ramp-seconds 30` of low CPU use and safe projected memory, admission
increases by one slot per engine.

Startup CPU allocation, `--max-jobs` and shared memory allocation remain
ceilings. CPU pressure reduces admission. A projected reserve breach within 30
seconds or RSS above 70% pauses admission. Recovery starts at one slot. RSS
estimates use a 1.5 safety factor per declared processor and never shrink during
execution.

Slot planning uses at most 65% of each engine's memory allocation. The 85%
emergency guard remains active. Admission changes occur between CTest batches.
Dependencies and all members of one fixture stay together. An entry whose
`PROCESSORS` exceeds admission can execute alone. Each engine executes one batch
at a time.

`--scheduling fixed` uses one batch under the resource guards.

`parallelism.jsonl` records decisions. Each `waves/*/command.json` records
actual concurrency, arguments and build settings.
Each batch retains JUnit and LastTest logs. Aggregate JUnit rejects duplicate
and unexpected names. A later pass cannot replace a failure.

## Results and resource records

Each CTest entry receives a resource record with arguments, working directory,
elapsed time, CPU time and process-tree RSS samples.
It also records process count and child `ru_maxrss`. Shared pages can be counted
twice, and sampling can miss short processes.

The child maximum describes reaped children. It is not the simultaneous RSS sum
of their process tree.
Missing samples produce JSON `null` with a status. Partial records have
`final=false`.

The wrapper sets `NOCLOOM_SIM_METRICS_DIR` and `NOCLOOM_SIM_CASE`. Instrumented
compile and simulation calls write resource records in separate subdirectories.
`results.json` lists them under `stage_resources` with paths and hashes. Do not
add their resource values to parent CTest values.

`NO_RECORDS_OBSERVED` means no stage records were found. Record presence does
not establish coverage of every nested invocation.

Unreadable resource data produces `TELEMETRY_ERROR` and
`MEASUREMENT_INCOMPLETE`. The invocation becomes `NONPASS` and retains its JUnit
verdicts.
`manifest-errors.json` lists unreadable files and rejected symbolic links. Their
targets are not collected.

`durations.json` and `durations.tsv` classify elapsed time as FAST up to 30
seconds, MEDIUM up to 120, and SLOW up to 600.
Longer entries are VERY_SLOW. `NOT_RUN` is UNMEASURED. A timeout gives a lower
bound. These classes do not change profile membership.

`SUMMARY.md` lists counts, non-passes and the ten largest time and
sampled-memory values per engine. `results.json` binds each result to its
resource record. Missing, duplicate or unexpected JUnit entries prevent success.
`NOT_RUN` and profile exclusions never count as PASS.

Copy the entire result directory and check `sha256sum -c MANIFEST.txt`. Collect
required RTL, benches, checkers, logs, events and counterexample waveforms
separately before removing build artifacts. The summary archive does not contain
every nested case artifact.

## Runner checks

```sh
cmake -S tests/suite_runner -B build/suite-runner-contract
ctest --test-dir build/suite-runner-contract --output-on-failure
```

These checks cover selection, fixtures, resource refusal, JUnit outcomes,
argument preservation, RSS sampling and a two-engine toy execution.
They do not execute the complete HDL inventory.

`--history` selects persistent attempt storage. Each invocation exports
`history-summary/` and queues at most 16 entries in `next-audit/`.
Use candidate and environment manifests to bind statistics to their inputs. See
the verification policy for record formats.

## Local safety checks

`formal2_prepare` creates deterministic width-adapter inputs.
`formal2_adapter_axioms` checks local hold, reset and transfer safety, occupied
episodes, designated faults and baseline recovery.

`arbiter_library_formal` checks graph output arbiters. Each solver call has a
60-second budget.
Unknown, timeout, tool error and missing counterexamples fail their expected
result. They do not count as detected faults.

Run `ctest --test-dir build -R '^formal2_' --output-on-failure` after changing
these local rules.
CTest includes the preparation fixture automatically.

These checks do not establish arbitrary-topology liveness, CDC convergence or a
network drain bound.
