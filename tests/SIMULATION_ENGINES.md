# Simulation engines

`tests/verif/install.py` accepts `--sim verilator|iverilog|external` and
`--sim-config FILE`. Icarus is the default behavioral engine.
`NOCLOOM_SIM` selects the engine for CTest and behavioral calls routed through
`sim_process`. An explicit `--sim` takes precedence.

Failures never change engines automatically. `--sim-config` requires `external`,
selected by argument or environment.
Syntax and synthesis checks use their own commands. Artifact preparation
executes no simulation.

`NOCLOOM_SIM_RUNNER` selects one executable receiving `WORK [PLUSARGS...]` in
working directory `WORK`.
Setting it with `NOCLOOM_SIM` is an error. It must compile, execute simulation,
return its status and print the bench output.

The output is retained as `simulation.log`.

## External adapter

Set `NOCLOOM_SIM=external` and `NOCLOOM_SIM_CONFIG` to a private version 1 JSON
adapter.
Keep tool paths, environment setup, license settings and command templates in an
ignored `*.local/` directory.

The following schema describes command fields. Supply the tool's executable
names and arguments.

```json
{
  "version": 1,
  "compile": ["<EXTERNAL_SIM>", "<COMPILE_OPTION>", "{sources}"],
  "run": ["<EXTERNAL_RUN>", "{options}"],
  "compile_timeout": 600,
  "run_timeout": 180,
  "failure_patterns": ["<ADDITIONAL_FAILURE_REGEX>"]
}
```

Commands are argument arrays executed without a shell. Put environment setup in
a private wrapper or the invoking environment.

| Substitution | Meaning |
| --- | --- |
| `{work}`, `{build}` | Absolute execution and compile directories |
| `{tb}`, `{rtl}` | Absolute bench and RTL paths |
| `{top}` | Top module `tb` |
| `{sources}` | Complete ordered source list, expanded from one standalone argument |
| `{options}` | Original plusargs, expanded from exactly one standalone run argument |
| `{{`, `}}` | Literal braces |

Compilation runs in `{build}`. Simulation and event output use `{work}`.
Compile exactly the supplied sources with the bench's timing semantics. Supply
required language and timescale options in the private adapter.

The full suite requires `{sources}`, since component entries can compile several
RTL modules.
Timeouts must be finite and positive. Outer CTest timeouts also apply.

External calls compile afresh. Explicit Verilator cross-fixture calls can reuse
their source-hash cache. Icarus and external calls do not share that cache.

## Verdicts and logs

Compilation requires exit zero. Simulation requires exit zero, its completion
marker and no recognized failure marker.
Completion markers include `MATRIX_PASS` and the matching `CROSS_*_PASS`.

Built-in failures include `ASSERT_FAIL:`, `CHECK_FAIL`, line-leading `Fatal:` or
`Error:`, and `%Error`.
Adapters can add failure patterns. They cannot remove built-in patterns.

A missing completion marker or a failure marker causes failure despite exit
zero.

Compile errors, missing executables and timeouts fail execution. They do not
establish detection of an injected RTL fault.
Each behavioral checker also requires its designated assertions, reachability,
counters and events.

`sim-engine.json` records raw status, markers, selection, input and adapter
hashes, arguments, directory, timeout and log hash.
`compile.log` and `simulation.log` contain merged stdout and stderr, including
partial output after timeout.

These records can contain private commands. Keep execution directories and logs
private.

## Commands and driver routing

| Caller | Verilator command | Alternatives |
| --- | --- | --- |
| Cross APB/AXI, reset, error logging and multi-domain | `verilator --binary --timing -Wno-fatal --top-module tb --build-jobs 2 -o sim` with supplied bench and RTL | Icarus, external adapter, or `NOCLOOM_SIM_RUNNER` |
| Installed network, large and expanded CLI/API | `verilator --binary --timing --top-module tb -Wno-fatal --build-jobs 8 --output-split 10000 -o sim` with supplied bench and RTL | Icarus or external adapter |

Icarus compilation uses `iverilog -g2012 -s tb -o BUILD/sim.vvp` with the
supplied sources. Execution uses `vvp BUILD/sim.vvp` and plusargs.
Syntax-only checks use `iverilog -g2001`. Behavioral engine selection does not
replace installed synthesis checks.

| Caller | Compile timeout | Run timeout |
| --- | --- | --- |
| Cross | 1800 seconds | 1800 seconds |
| Installed network | 120 seconds | 120 seconds |
| Installed large | 120 seconds | 300 seconds |
| Installed expanded | 120 seconds | 1800 seconds |

`sim_process.run` and `sim_process.Popen` preserve subprocess arguments and
streams while routing behavioral calls.
Unknown routed compile options fail. Syntax and formal or synthesis commands
pass through.

The generated launcher retains the driver's executable path and plusargs.
Compile failure cannot leave a stale launcher.
`simulation-process.jsonl` records each compile and execution, including engine,
arguments, hashes and raw status.

The caller determines behavioral and fault-detection outcomes.

## Failure scanning

Routed stdout and stderr retain separate byte streams. A worker scans their
complete spooled contents as UTF-8 with replacement.
Regular expressions retain full-string semantics across stream chunks.

The default limit is 256 MiB per stream, with a 30-second scan timeout.
Set positive `NOCLOOM_FAILURE_SCAN_MAX_BYTES` and `NOCLOOM_FAILURE_SCAN_TIMEOUT`
values to change these budgets.

The byte limit is not an RSS cap. The outer timeout includes execution and
scanning.
Budget exhaustion, I/O errors and scanner errors produce
`FAILURE_SCAN_INCOMPLETE`. Incomplete scans retain their spool.

Receipts retain raw status, scan status, budgets and elapsed time.

Resource wrappers preserve signal termination, including its meaning for CTest
`WILL_FAIL`.
Receipts distinguish child termination from wrapper interruption. A killed
wrapper can leave a partial record.

## Integral parameters

Routed `-GNAME=value` and `-Ptb.NAME=value` overrides become named integral
parameters in compile receipts.
Icarus receives `-Ptb.NAME=value`. Verilator retains its command. Duplicate
names, hierarchical targets and unsupported expressions fail.

An external adapter handling overrides requires a `parameter` argument template
using `{name}` and `{value}`, optionally `{top}`.
Its compile array also requires exactly one standalone `{parameters}`. Expansion
preserves parameter order without shell evaluation.

With no overrides, `{parameters}` expands to no arguments. Adapters without
parameter support reject parameterized entries.

## Resource records

Include `sim_metrics.py` when copying engine helpers.
`NOCLOOM_SIM_METRICS_DIR` selects a record directory and `NOCLOOM_SIM_CASE`
identifies the entry.

Without that directory variable, records use `simulation-metrics` beneath the
call's working directory. Cached compiles produce no execution record.

Records contain stage, command, elapsed time, CPU time and maximum resident
memory. A fresh helper isolates child resource accounting.
The RSS maximum describes the largest individual reaped child, including routing
helpers. It is not a simultaneous process-tree sum.

`tree_rss_peak_bytes` is null. Linux and Darwin units are normalized.
Unsupported platforms report absent data.

`EXITED` means exit zero. The caller's assertions determine PASS.
A timeout killing the helper produces `TIMEOUT`, partial elapsed time and null
CPU/RSS. Errors before execution also have null CPU/RSS.

Helper startup contributes to elapsed time and timeout. Raw logs and
process-group cleanup retain their configured behavior.
