# Library

The C++20 core accepts typed definitions and returns artifacts in memory,
without file I/O or printed diagnostics.

## Build requirements

| Tool | Required version | Known to build with |
| --- | --- | --- |
| CMake | 3.24 or newer | 3.24, 3.28, 3.31 and 4.3 |
| Ninja | No version check | |
| C++ compiler | C++20 | GCC 13 to 16, Clang 18 to 23 |
| nlohmann_json | 3.11.3 or newer | 3.11.3 and 3.12.0 |
| yaml-cpp | 0.8.0 or newer | 0.8.0 and 0.9.0 |
| CLI11 | 2.4.1 or newer | 2.4.1, 2.6.2 and 2.7.2 |

The packages of Ubuntu 24.04, Debian 13 and Fedora 43 meet these requirements.

```sh
# Ubuntu 24.04 and Debian 13
sudo apt install g++ cmake ninja-build nlohmann-json3-dev libyaml-cpp-dev libcli11-dev
# Fedora 43
sudo dnf install gcc-c++ cmake ninja-build json-devel yaml-cpp-devel cli11-devel
```

A build with `NOCLOOM_BUILD_TESTS` also needs these tools.

| Tool | Required version |
| --- | --- |
| Catch2 | 3.4.0 or newer within version 3 |
| C compiler | C11, for generated header checks |
| Python 3, Make, Icarus Verilog, Yosys, Verilator | No version check |
| clang-format, cppcheck | No version check. The project uses clang-format 23.1 and cppcheck 2.21. Older versions can report differences |
| A second C++20 compiler | No version check. `NOCLOOM_DETERMINISM_COMPILERS=OFF` removes this requirement |

Tests with the `formal` label also need SymbiYosys (`sby`) with the Z3 solver,
and Lean 4.24.0 with `lake`. CMake does not check these tools. The tests fail
without them. The `quick` label needs none of them.

## Build and install

```sh
cmake -S . -B build -G Ninja -DNOCLOOM_BUILD_TESTS=OFF -DNOCLOOM_BUILD_EXAMPLES=OFF
cmake --build build
cmake --install build --prefix "$PWD/build/install"
```

The build creates static libraries only. `BUILD_SHARED_LIBS` has no effect. Set
`CMAKE_PREFIX_PATH` to the absolute installation prefix when configuring a
consumer.

| Package component | Imported target | Purpose |
| --- | --- | --- |
| `Core` | `nocloom::core` | Typed definitions, validation and generation |
| `Config` | `nocloom::config` | Strict YAML parsing |
| `IO` | `nocloom::io` | File reads and artifact writes |
| Default package | `nocloom::nocloom` | Available components through one interface |

A consumer that uses all installed components needs these lines in its
`CMakeLists.txt`. The imported targets set the C++20 requirement.

```cmake
cmake_minimum_required(VERSION 3.24)
project(consumer LANGUAGES CXX)
find_package(nocloom CONFIG REQUIRED)
add_executable(consumer main.cpp)
target_link_libraries(consumer PRIVATE nocloom::nocloom)
```

A core-only consumer uses
`find_package(nocloom CONFIG REQUIRED COMPONENTS Core)` without a YAML parser.
Link its target to `nocloom::core`. The
[C++ example](../examples/generate_from_definition.cpp) constructs and generates
a complete definition.

## Build configuration

| CMake variable | Default | Purpose |
| --- | --- | --- |
| `NOCLOOM_BUILD_CONFIG` | `ON` | YAML and file adapters |
| `NOCLOOM_BUILD_CLI` | `ON` for a top-level build | CLI executable |
| `NOCLOOM_BUILD_EXAMPLES` | `ON` for a top-level build | API example programs |
| `NOCLOOM_BUILD_TESTS` | `ON` for a top-level build | Test targets. Also requires `BUILD_TESTING=ON` |
| `NOCLOOM_TEST_DEPS_REQUIRED` | `ON` | Configuration fails when a test tool is missing |
| `NOCLOOM_DETERMINISM_COMPILERS` | `ON` | Registers the test that needs a second C++20 compiler |
| `NOCLOOM_OTHER_CXX` | Empty | Path of that second compiler. Empty searches `PATH` |
| `NOCLOOM_IPXACT_SCHEMA_DIR` | Empty | IEEE 1685-2009 schema directory. Registers the IP-XACT schema test, which also needs `xmllint` |

A top-level build defaults to the `Release` build type. A build with tests
requires clang-format and cppcheck. CMake checks the stated dependency versions.
The CLI requires the configuration adapter. A core-only integration can disable
both. A parent CMake project can use `add_subdirectory` and link
`nocloom::core`.

## API calls

| Call | Input and result |
| --- | --- |
| `config::parse` | YAML text and optional source label. Returns `ParsedDefinition` |
| `validate` | Definition, options and an optional source location map. Returns status |
| `make_plan` | Definition and an optional source location map. Returns an immutable normalized `Plan` |
| `emit` | Plan and generation options. Returns artifacts |
| `generate` | Definition and generation options. Validates, plans and emits artifacts |
| `add_address_table` | Artifact set with one map and bundle. Adds address table files |
| `add_ipxact` | Definition, artifact set and IP-XACT options. Adds a component XML file |
| `version` | Returns the generator version |
| `io::load_definition` | Path to a `.yaml` or `.yml` file. Returns `ParsedDefinition` |
| `io::write_artifacts` | Artifact set and a new output directory. Returns `io::WriteReport`. Its `written_paths` member lists the written artifact paths, relative to the output directory |

`ArtifactSet` contains `Artifact` values with relative `path` and `text` fields.
Failed results contain diagnostics without a value.

`GenerationOptions::c_header` is a boolean that defaults to `false`. Set it to
`true` in `generate` or `emit` to add [C headers](outputs.md#c-headers) in
memory.
It needs no file adapter or address table output. The bundle lists the headers.
Macro name conflicts fail generation with `NCL_C_HEADER_NAME`.

Configuration errors and expected file errors return diagnostics. Allocation
failures and other exceptional runtime failures can throw exceptions. The core
does not terminate the process on a configuration error. Source locations affect
diagnostics only.

`ParsedDefinition::definition` contains the network definition.
`ParsedDefinition::options` contains the generation configuration.
`ParsedDefinition::sources` maps field paths to source locations for
diagnostics. Pass the parsed `definition` and `options` to `generate`. See
[artifact path rules](#artifact-writes) and
[output directory rules](cli.md#validation-and-generation).

## Tree topology helper

`tree_topology` creates directional exchange graphs from a tree description.
Each `TreeNode` has a name, optional parent, target list and optional clock
domain. See [generate_tree.cpp](../examples/generate_tree.cpp) and
[topology.hpp](../include/nocloom/topology.hpp).

## Tree requirements

The helper accepts 1 to 128 nodes and requires initiator names attached to the
unique root. Targets can attach to any node. Parent-to-child request edges have
reversed response edges. It preserves node names and optional domains, and
equivalent input permutations produce identical graphs.

Empty branches are rejected. The helper performs no I/O and selects no
connectivity, arbitration or transport configuration. Validate the complete
definition to check endpoint identities, domains and resource limits.

## Mesh topology helper

`mesh_topology` creates explicit request and response graphs with fixed routes.
See [generate_mesh.cpp](../examples/generate_mesh.cpp) and its generated
[mesh_axi.yaml](../examples/mesh_axi.yaml).

With examples enabled, run `build/nocloom_mesh_example build/mesh-output` from
the repository root. The output directory must be new. It contains the ordinary
artifacts and `mesh_axi.yaml`. The example uses a 3 by 3 grid, four AXI4
initiators at the corners and one AXI4 target at every cell.

```cpp
#include <nocloom/topology.hpp>

const auto topology = nocloom::mesh_topology(
    3, 3, {{"cpu", 0, 0}}, {{"memory", 2, 2}},
    nocloom::Connectivity{.all = true}, "main");
if (topology)
  definition.topology = topology.value();
```

Pass the same connectivity matrix to the helper and `Definition::connectivity`.
The helper returns `Result<ExchangeTopology>`. Failed results contain
diagnostics without a topology. It does no file I/O and prints nothing.

| Parameter | Values and default |
| --- | --- |
| `rows`, `columns` | Nonzero counts with a product from 1 to 128, including cells that carry no route |
| `initiators`, `targets` | Each list has 1 to 256 `MeshEndpoint` attachments, unique by name within its role |
| `MeshEndpoint::name` | A valid endpoint name that matches the complete definition |
| `MeshEndpoint::row`, `column` | Zero-based coordinates within the grid, each defaults to 0 |
| `MeshEndpoint::flows` | 1 to 32, default 1. For an initiator, use its `route_classes` count, or 1 without classes. For a target, use its `flows` count |
| `connectivity` | Required. `all` or admitted pairs with optional flow indices, as in [connectivity](network.md#connectivity). Every attached endpoint must occur in a pair |
| `clock_domain` | Optional common node domain, absent by default. Copied to every retained node |

Columns are X and rows are Y. Node names are `mesh_r<row>_c<column>`, local to
each graph. Each request route moves along X to the target column, then along Y
to the target row. Each response starts at the target and independently moves
along X, then Y to the initiator. A same-cell route contains one node and no
link. Each admitted initiator flow and target flow pair has one route.

Each graph retains only nodes and directed links used by its routes. Links join
adjacent cells. Both link directions are present when routes use both. Endpoint
and connectivity list order do not affect the topology or artifact content.
Node names, links, attachments and route keys are sorted. Widths, arbitration
and pipes keep their ordinary defaults.

The helper checks dimensions, attachments and connectivity. Use flow counts that
match the endpoint declarations. Validate the complete definition to check
routes, endpoint identities, clock domains, mappings, protocols and resource
limits. The helper creates no additional hardware type.

## Artifact writes

Artifact paths must be unique basenames containing only letters, digits,
underscore, hyphen or dot. The file writer rejects empty, dot and parent paths,
and empty artifact sets. If writing fails partway, diagnostics list created
files. The writer does not roll them back.

## Dependency licenses

See [attribution](attribution.md) for licenses and installed materials.
