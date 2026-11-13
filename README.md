# NocLoom

![NocLoom. Weave your interconnect.](docs/image/logo.svg)

[![Build](https://github.com/vowstar/nocloom/actions/workflows/build.yml/badge.svg)](https://github.com/vowstar/nocloom/actions/workflows/build.yml)

An open-source on-chip interconnect generator.

NocLoom is a command-line tool (CLI) and a C++20 library. You define a network
of AXI4, AXI3, AXI4-Lite, APB, AHB-Lite and AHB5 interfaces in YAML or C++.
NocLoom generates synthesizable Verilog-2001 with switches, protocol adapters,
width converters and clock domain crossings (CDC).

NocLoom 0.1.0 is the first public release.

![A mixed-protocol SoC network with two clock domains.][soc]

![A 4x4 mesh network from a declared graph and explicit routes.][mesh]

[soc]: https://github.com/user-attachments/assets/04c77b25-39e9-4ff9-aaf0-ee98ecb714a3
[mesh]: https://github.com/user-attachments/assets/a66c797c-92ee-4203-b017-703c9379dd02

## Quick start

Run all commands from the repository root. The build needs CMake, Ninja, a C++20
compiler, nlohmann_json, yaml-cpp and CLI11. The packages of Ubuntu 24.04,
Debian 13 and Fedora 43 are new enough. See the
[build requirements](docs/library.md#build-requirements) for the versions and
the install commands.

```sh
cmake -S . -B build -G Ninja -DNOCLOOM_BUILD_TESTS=OFF -DNOCLOOM_BUILD_EXAMPLES=OFF
cmake --build build
```

To install the library and the CLI, run
`cmake --install build --prefix "$PWD/build/install"`, or name another prefix.
See [Build and install](docs/library.md#build-and-install).

Write this definition to `build/soc_fabric.yaml`. An initiator connects to an
external bus manager, and a target connects to an external bus subordinate.

```yaml
schema_version: 10
name: soc_fabric
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 64, len_width: 8}
  - {name: dbg, clock_domain: main, protocol: axi4_lite, data_width: 32}
targets:
  - {name: mem, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8,
     attributes: amba, mappings: [{base: '0x0', size: '0x10000'}]}
  - {name: uart, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x10000', size: '0x1000'}]}
connectivity:
  cpu: [mem, uart]
  dbg: [uart]
output: {backend: verilog, language: verilog_2001}
```

The `cpu` initiator is 64 bits wide and the `mem` target is 32 bits wide, so the
network converts the data width. The network also converts AXI4 and AXI4-Lite
requests to APB for `uart`.

| Field | Meaning |
| --- | --- |
| `schema_version` | The schema version must be 10 |
| `reset_ports` | `reset_n` is the active-low reset input of the network |
| `test_ports` | `test` is an input that opens the clock gates during testing. Domains have clock gates by default |
| `id_width` | The example does not set it. The AXI ID ports then have the default width of 4 bits |
| `len_width` | The width of the AxLEN ports. The default is 4 bits. A standard AXI4 port has 8 bits |
| `attributes` | `axi4`, `axi3`, `axi4_lite`, `apb5`, `apb4`, `ahb_v3` and `ahb5` targets must set it. With `amba`, the CACHE, PROT or HPROT outputs of the target follow the Arm® AMBA® attributes of the request. AHB5 requires `amba`. Other targets can use `zero` for constant outputs |

Validate the definition. Then generate the output files into a new directory.

```sh
build/nocloom validate build/soc_fabric.yaml
build/nocloom generate build/soc_fabric.yaml --out-dir build/generated
```

`validate` prints `Definition is valid`. `generate` prints `Generated 6 files`.
If the output directory exists, `generate` stops with `NCL_IO_EXISTS` and exit
code 3. Remove that directory or use another one. See
[CLI](docs/cli.md#validation-and-generation) for the output path rules.

The top module `nocloom_soc_fabric` is in
`build/generated/nocloom_soc_fabric.v`. A simulation also needs
`nocloom_clock_gate.v` and `nocloom_synchronizer.v` from the same directory.
Initiator ports have names such as `nl_i_cpu_awaddr`, and target ports have
names such as `nl_t_mem_awaddr`.

More definitions are in [examples](examples). Start with `apb_domains.yaml` for
two clock domains and `axi4_graph.yaml` for a declared topology.

## Integration

The bundle `nocloom_<name>.bundle.json` lists the output files. Compile its
`simulation_sources` for simulation and its `synthesis_sources` for synthesis.
See [Output files](docs/outputs.md) for each output file and its metadata.

Without `cells` bindings, both lists contain behavioral models of the clock gate
and the synchronizer. These models are for simulation only. For synthesis, bind
the cells of your technology library as
[Technology cells](docs/technology-cells.md) describes. A simulation with bound
cells also needs the `library_cell_models` of the bundle or the models of your
library.

The top-level field `clock_gating: none` removes the clock gates. It does not
remove the synchronizer. Set `clock_gating: none` for an FPGA, because the
behavioral clock gate model contains a latch.

The top module is one flat module. Its only instances are the clock gate and
synchronizer cells, and its internal signals have generated names of the form
`nl_internal_v<N>`. To find the signals of an initiator, a target or a switch,
read the map. `value/<N>` in the map is the signal `nl_internal_v<N>`, except
for a stage inside a synchronizer. For example, `network.storage` lists the
signals of each resource, such as `source/cpu/pipe/aw`. See
[Map fields](docs/map-fields.md).

With its default warning settings, Verilator reports `CMPCONST` and `UNSIGNED`
warnings for most generated networks and exits with a nonzero code. Add
`-Wno-CMPCONST -Wno-UNSIGNED` to the Verilator command.

If the network has clock domain crossings, run `generate` with
`--cdc-constraints` and apply the
[CDC constraints](docs/timing-constraints.md#cdc-constraints).

The external managers and subordinates must meet the
[progress requirements](docs/timeout-and-progress.md#shared-request-and-response-paths).
For example, a manager must deliver every W beat of a write that it started,
and it must accept R beats in finite time. A violation can stop the network
until reset.

Reset all domains together. If you reset one domain alone, make sure that no
traffic crosses its boundary.

## Features

| Area | Supported |
| --- | --- |
| Initiator protocols | `axi4`, `axi3`, `axi4_lite`, `apb5`, `apb4`, `apb3`, `ahb_v3` (AHB-Lite) and `ahb5` |
| Target protocols | `axi4`, `axi3`, `axi4_lite`, `apb5`, `apb4`, `apb3`, `apb2`, `ahb_v3` (AHB-Lite) and `ahb5` |
| AHB5 properties | HPROT is 4 bits by default or 7 bits. HNONSEC is optional and defaults to absent. HMASTER is 0 to 32 bits |
| Endpoint count | 1 to 256 initiators, 1 to 256 targets |
| Topology | One generated request switch and one generated response switch, or declared request and response graphs of 1 to 128 switches each |
| Endpoint data width | AXI4, AXI3, AHB-Lite and AHB5: 32, 64, 128, 256, 512 or 1024 bits. AXI4-Lite: 32 or 64 bits. APB: 8, 16 or 32 bits |
| Network data width | 32 or 64 bits (`transport.data_width`). The default is the narrowest endpoint data width, where APB counts as 32 bits and the maximum is 64 bits. A 64-bit network accepts only 64-bit initiators, and no APB endpoint or AHB target. Other definitions need a 32-bit network. Therefore, an initiator wider than 64 bits needs `transport.data_width: 32`, unless a 32-bit or APB endpoint already makes 32 bits the default |
| Address width | 2 to 48 bits. AXI4, AXI3 and AHB initiators need at least 12 bits. APB initiators use at most 32 bits. APB, AHB and AXI4-Lite targets use at most 32 bits |
| Clock domains | 1 to 64. More than one domain needs a `topology` whose switches name their clock domains. Clock domain crossings connect asynchronous clocks, or related clocks divided from one root |
| Transactions | Several AXI transactions in flight, exclusive access, target timeouts and an error response on a decode miss |
| Scheduling | Fixed levels from 0 to 7 on AXI4, AXI3, AHB-Lite and AHB5 sources to full AXI4 or AXI3 targets, or on 32-bit APB networks. AXI4 and AXI3 sources also support AxQOS maps. AXI4, AXI3 and 32-bit APB sources support bandwidth limits |
| Routing | NocLoom fixes each route at generation |
| Output files | Always: Verilog-2001 RTL and three JSON files, which are the map, the bundle and the dependency graph. Optional: IP-XACT (`--ipxact`), address table (`--address-table`), CDC constraints (`--cdc-constraints`) and C headers (`--c-header`) |
| C++ library | In-memory generation, tree and mesh topology helpers, and a CMake package. The core library does no file I/O and prints nothing |

See [Documentation](#documentation) for field definitions and limits.

## Limits

| Not supported | Detail |
| --- | --- |
| Other protocols | For example AXI5, ACE and APB2 initiators |
| AHB5 optional signals | HEXCL, HEXOKAY and HAUSER, HWUSER, HRUSER and HBUSER are unsupported. See [AHB5 properties](docs/ahb.md#ahb5-basic-properties) |
| AHB arbitration and responses | No multi-manager arbitration or HSPLIT. RETRY and SPLIT on two-bit `ahb_v3` targets fail the packet |
| Other RTL languages | The output is Verilog-2001 only |
| Adaptive routing | A route does not change with the load |
| Scheduling | Nonzero levels exclude AHB targets, other mixed protocol networks, exclusive access, target timeouts and targets wider than the network. AXI graphs also exclude one-direction endpoints. AHB sources have no AxQOS map, shaper or scheduler |
| APB5 optional signals | PAUSER and PBUSER only. PWUSER, PRUSER, PWAKEUP, parity check signals and RME PNSE are unsupported |
| AxREGION | AXI endpoints have no AxREGION ports |
| AXI3 locked writes and write data interleaving | An AXI3 target receives neither |
| Unchanged non-modifiable narrow bursts | With the default `narrow_bursts: merge`, an AXI target that is wider than the beats of a burst merges them regardless of AxCACHE. This can change non-modifiable transactions beyond the transformations that AXI A4.3 permits. See [width transformation details](docs/width-conversion.md#width-transformation-details) |

The CLI rejects an invalid definition before it writes files. It reports a
diagnostic code, a field path and a message, with the file line and column when
known. [Diagnostics](docs/diagnostics.md) lists the codes.

## Documentation

| Manual | Content |
| --- | --- |
| [Definition](docs/definition.md) | Top-level fields, YAML shorthand, C++ types and value limits |
| [Clocks and resets](docs/clocks-and-resets.md) | Domains, resets, gates, cells, crossings and CDC constraints |
| [Network](docs/network.md) | Connectivity, graphs, routes, flows, arbitration and pipes |
| [Address map](docs/address-map.md) | Mappings, address inputs, decode misses and error responses |
| [Protocols](docs/protocols.md) | Initiator and target protocols, width conversion and packets |
| [Transactions](docs/transactions.md) | Bursts in flight, target IDs, read interleaving, exclusive access and timeouts |
| [Scheduling](docs/scheduling.md) | Levels, AxQOS maps, bandwidth shapers and bandwidth schedulers |
| [Output files](docs/outputs.md) | Map, bundle, dependency graph, IP-XACT, address table, CDC constraints and C headers |
| [C headers](docs/c-headers.md) | Address and error log register macros |
| [Error logging](docs/error-logging.md) | Register windows, capture, filters and alarms |
| [Library](docs/library.md) | Build requirements, CMake packages, C++ API, tree and mesh topology helpers |
| [CLI](docs/cli.md) | Validation, generation, semantic diff and exit codes |
| [Diagnostics](docs/diagnostics.md) | Diagnostic codes and the conditions that produce them |

## Development

The test build also needs Catch2, Python 3, Make, Icarus Verilog, Yosys,
Verilator, clang-format, cppcheck and a second C++20 compiler. One test builds
the CLI with the second compiler and compares the output files.
`-DNOCLOOM_DETERMINISM_COMPILERS=OFF` removes that test and the need for the
second compiler.

```sh
cmake -S . -B build -G Ninja -DNOCLOOM_BUILD_TESTS=ON -DNOCLOOM_BUILD_EXAMPLES=ON
cmake --build build
ctest --test-dir build -L quick --output-on-failure
cmake --build build --target clang-format
cmake --build build --target cppcheck
```

The `quick` label selects a short subset of the tests. The `ci` label selects
the sample that the Build workflow runs. `ctest --test-dir build` runs all
tests. The tests with the `formal` label also need SymbiYosys (`sby`) with the
Z3 solver, and Lean 4.24.0 with `lake`. The `clang-format` target reformats the
sources in place.

[tests/SUITES.md](tests/SUITES.md) describes the daily and the full test runs
and their resource limits. The suite runner plans 8 GiB of memory for each test
slot and keeps 16 GiB or more free.
[tests/VERIFICATION_POLICY.md](tests/VERIFICATION_POLICY.md) describes which
tests a change needs, and [formal/lean/README.md](formal/lean/README.md)
describes the Lean model of the transport and its theorems.

Commit messages follow
[Conventional Commits](https://www.conventionalcommits.org/). Add a
`Signed-off-by` line to each commit with `git commit -s`. This line states that
you agree to the
[Developer Certificate of Origin](https://developercertificate.org/).

Contributions are licensed under the Apache License 2.0. A contribution to the
content that NocLoom supplies in the output files is also licensed under the
NocLoom Output Permission 1.0.

## License

The generator is under the [Apache License 2.0](LICENSE). The content that
NocLoom supplies in the output files is under the
[NocLoom Output Permission 1.0](LICENSES/LicenseRef-NocLoom-Output-1.0.txt).
That permission lets you use, modify and distribute the content under terms of
your choice, without attribution or notices.

Dependency licenses and trademark notices are in
[Attribution](docs/attribution.md).
