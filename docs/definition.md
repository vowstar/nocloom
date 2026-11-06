# Definition

Use YAML or C++ `nocloom::Definition`. Both use the same validator.

## Input rules

| Rule | Contract |
| --- | --- |
| Schema | `schema_version: 10` is required |
| Names | Names start with a letter and contain only letters, digits and underscores, with at most 128 characters. Names cannot start with `nl_` or `nocloom_` and cannot be a Verilog-2001 keyword. Names within one kind are unique |
| Required endpoint fields | `name`, `clock_domain`, `protocol`, `data_width`. Targets also need `mappings`. `axi4`, `axi3`, `axi4_lite`, `apb4` and `ahb_v3` targets also need `attributes` |
| Unknown fields | The YAML parser rejects unknown fields with `NCL_SCHEMA_UNKNOWN_FIELD` |
| Duplicate YAML keys | The YAML parser rejects duplicate keys |
| Integers | Unquoted decimal integers without leading zeros, such as `64`. Other forms produce `NCL_SCHEMA_TYPE` |
| Addresses and sizes | Single-line quoted strings, decimal without leading zeros or hexadecimal with a lowercase `0x` prefix, such as `'4096'` or `'0x1000'`. An unquoted value produces `NCL_SCHEMA_TYPE`. Other invalid text produces `NCL_ADDRESS_RANGE` |
| Input size | At most 32 MiB |
| YAML nesting | At most 32 levels |
| YAML nodes | At most 4194304 |
| YAML aliases, anchors, merge keys, explicit tags, null values and more than one document | Rejected |
| Output order | Equal definitions, versions and generation configuration produce identical paths and bytes |

See [diagnostics](diagnostics.md) for codes and source locations. Source labels
affect diagnostics only.

## Top-level fields

| Field | Type and purpose | Default or limit |
| --- | --- | --- |
| `schema_version` | Integer | Required in YAML, must be 10. C++ defaults to 10 |
| `name` | Network name | Required |
| `address_width` | Global address bits | Required in YAML, 2 to 48. C++ defaults to 32 |
| `reset_ports` | Active-low reset inputs | Required |
| `test_ports` | Active-high gate test inputs | Required for gated domains |
| `enable_ports` | Valid-edge inputs for `edge_qualified` clocks | Empty |
| `ungate_ports` | Inputs that force clocks on | Empty |
| `clock_roots` | Explicit shared physical roots | Empty |
| `clock_domains` | Endpoint and switch clocks | 1 to 64 |
| `clock_crossings` | Overrides per directed domain pair | Empty |
| `synchronizer_stages` | Crossing synchronizer length | 2, range 2 to 4 |
| `clock_gating` | `none` or `per_switch_and_endpoint` | `per_switch_and_endpoint` |
| `reset_coverage` | `all` or `minimal` | `all` |
| `cells` | Gate and synchronizer cell bindings | Behavioral wrappers |
| `address_inputs` | Address selection input ports | Empty |
| `address_selectors` | Conditions over address inputs | Empty |
| `initiators` | Request endpoints | 1 to 256 |
| `targets` | Response endpoints | 1 to 256 |
| `connectivity` | Allowed endpoint pairs | Required |
| `topology` | Explicit request and response graphs | One generated switch per direction |
| `arbitration` | Object with `policy` | Round robin, including `auto` |
| `transport` | Network widths, capacity and sideband fields | Defaults below |
| `error_logging` | Register window and error logs | Absent |
| `output` | Backend, language and register reset style | Required in YAML, with `backend` and `language` |
| `templates` | YAML endpoint templates | Empty |
| `groups` | YAML connectivity groups | Empty |

The [clocks manual](clocks-and-resets.md) describes clock and reset fields. The
[network manual](network.md) describes topology and connectivity. The
[address manual](address-map.md) describes selectors and mappings.

## Transport fields

Endpoint `data_width` sets physical bus width. Nodes and links can override the
[network word size](network.md#graph-objects).

| Field | Meaning | Default or range |
| --- | --- | --- |
| `data_width` | Network width | 32 or 64 bits. The default is the smallest endpoint width, capped at 64 bits. APB engines count as 32 bits |
| `max_outstanding` | Default initiator capacity | 1, range 1 to 64 |
| `id_table_entries` | Default initiator ordering-key capacity | Derived from capacity and ID width |
| `transport_word_bytes` | Default word size of switches and links, in bytes | 4, 8, 16 or 32. Defaults to `transport.data_width` divided by 8 |
| `response_codes` | Error response selection | `uniform` or `distinct`, default `distinct` |
| `packet_limits` | Narrower limits for connected endpoint pairs | Empty |
| `exclusive_granule` | Network monitor reservation granule in bytes | 1, powers of two through 128 |
| `user_flags` | Named one-bit request fields | Empty, at most 256 |
| `response_user_bits` | Response user field width | 0, at most 256 |

An APB endpoint requires a 32-bit network. Endpoint widths can differ from the
network width under the [protocol rules](protocols.md). The
[transactions manual](transactions.md) describes capacity and ordering keys.

## Output configuration

| YAML field | Values | Default | C++ option |
| --- | --- | --- | --- |
| `output.backend` | `verilog` | Required | `GenerationOptions::backend` |
| `output.language` | `verilog_2001` | Required | `Backend::Verilog2001` |
| `output.register_reset` | `asynchronous`, `synchronous` | `asynchronous` | `GenerationOptions::register_reset` |

YAML without `output`, `output.backend` or `output.language` produces
`NCL_SCHEMA_MISSING_FIELD`. In C++, a default `GenerationOptions` selects
Verilog-2001 and asynchronous reset.

CLI switches select optional output files. See [output files](outputs.md).

## Endpoint templates

A template contains endpoint fields under `templates.initiators` or
`templates.targets`. An endpoint selects one template with `use: cpu` or several
with `use: [cpu, fast]`. A template cannot set `name` or `use`. Templates and
the endpoint must set disjoint fields. Unknown templates and unused templates
are rejected.

A diagnostic for a template field points to that template.

The [short AXI example](../examples/scale_axi_12_short.yaml) uses templates and
a mirrored response graph. Its expanded form is
[scale_axi_12.yaml](../examples/scale_axi_12.yaml). C++ callers construct the
expanded types directly.

## Connectivity groups

`groups.initiators` and `groups.targets` map group names to endpoint name lists.
A connectivity row accepts an initiator name or group name. Its value is a
target list, `all`, or `{all_except: [name]}`. Target lists and exclusions
accept target groups. An initiator can occur in only one row.

Groups expand in their written order. `all` and `all_except` use target
declaration order. Unknown members, duplicate members, unused groups and
ambiguous names are rejected. A group cannot use the name of an endpoint or a
group of the other kind.

## C++ types

| YAML object | Public C++ type |
| --- | --- |
| Root | `Definition` |
| Initiator | `Initiator` |
| Target | `Target` |
| Mapping | `AddressMapping` |
| Clock domain | `ClockDomain` |
| Crossing | `ClockCrossing` |
| Connectivity | `Connectivity`, `ConnectivityEntry` |
| Topology | `ExchangeTopology`, `ExchangeGraph` |
| Route | `ExchangeRoute` |
| Generation configuration | `GenerationOptions` |

The complete field types are in
[definition.hpp](../include/nocloom/definition.hpp). The
[library manual](library.md) describes parsing, validation and generation calls.

## Required fields and diagnostics

Each endpoint sets its own `protocol` and `data_width`. The root has no protocol
or width field. Use `id_table_entries` to limit active ordering keys.

A missing required endpoint field produces `NCL_SCHEMA_MISSING_FIELD`.

Only `schema_version: 10` is accepted. Other values produce
`NCL_SCHEMA_VERSION`.

A derived invalid network width also reports `NCL_NETWORK_DATA_WIDTH` at
`transport.data_width`. With a 64-bit network, every initiator must be 64 bits.
A 32-bit endpoint makes the derived network width 32. Set
`transport.data_width: 64` to use a 64-bit network with 32-bit AXI targets. Full
AXI and AHB initiator addresses are 12 to 48 bits. AXI4-Lite initiator addresses
are 2 to 48 bits.

APB initiator addresses are 1 to 32 bits. Initiator widths cannot exceed the
global width. Omitted endpoint address widths inherit the global width.

Every YAML map, sequence, key and value counts toward the node limit. Exceeding
input size, nesting or node limits produces `NCL_RESOURCE_LIMIT`. Empty
templates are rejected.

Expanded diagnostics point to the template or group name that produced the field
or pair.
