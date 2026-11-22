# Definition

Use YAML or C++ `nocloom::Definition`. Both use the same validator.

## Input rules

| Rule | Contract |
| --- | --- |
| Schema | `schema_version: 10` is required |
| Names | Names start with a letter and contain only letters, digits and underscores, with at most 128 characters. Names cannot start with `nl_` or `nocloom_` or be a Verilog-2001 keyword. Keywords are case-sensitive. Names within one kind are unique |
| Generated names | The network module is `nocloom_<name>` in `nocloom_<name>.v`. A name that gives the module of a generated cell file, such as `clock_gate`, is rejected (`NCL_NAME_OUTPUT_COLLISION`). Names that differ only in letter case give distinct modules and files, see [letter case in file names](cli.md#letter-case-in-file-names) |
| Required endpoint fields | `name`, `clock_domain`, `protocol`, `data_width`. Targets also need `mappings`. [Full AXI](protocols.md), AXI4-Lite, APB5, APB4, AHB-Lite and AHB5 targets also need `attributes` |
| Unknown fields | The YAML parser rejects unknown fields with `NCL_SCHEMA_UNKNOWN_FIELD` |
| Duplicate YAML keys | The YAML parser rejects duplicate keys |
| Integers | Unquoted decimal integers without leading zeros, such as `64`. Other forms produce `NCL_SCHEMA_TYPE` |
| Addresses and sizes | Single-line quoted strings, decimal without leading zeros or hexadecimal with a lowercase `0x` prefix, such as `'4096'` or `'0x1000'`. An unquoted value produces `NCL_SCHEMA_TYPE`. Other invalid text produces `NCL_ADDRESS_RANGE` |
| Input size | At most 32 MiB |
| YAML nesting | At most 32 levels |
| YAML nodes | At most 4194304 |
| YAML aliases, anchors, merge keys, explicit tags, null values and more than one document | Rejected |
| Output order | Equal definitions, versions and generation configuration produce identical paths and bytes |
| Diagnostics | See [diagnostics](diagnostics.md) for codes and source locations. Source labels affect diagnostics only |

## Top-level fields

| Field | Type and purpose | Default or limit |
| --- | --- | --- |
| `schema_version` | Integer | Required in YAML, must be 10. C++ defaults to 10 |
| `name` | Network name | Required |
| `address_width` | Global address bits | Required in YAML, 2 to 48. C++ defaults to 32 |
| `reset_ports` | Active-low reset inputs | Required |
| `test_ports` | Active-high gate test inputs | Required for gated domains |
| `enable_ports` | Valid-edge inputs of clock domains | Empty |
| `ungate_ports` | Inputs that force clocks on | Empty |
| `clock_roots` | Explicit shared physical roots | Empty |
| `clock_domains` | Endpoint and switch clocks | 1 to 64 |
| `clock_crossings` | Overrides per directed domain pair | Empty |
| `synchronizer_stages` | Crossing synchronizer length | 2, range 2 to 4 |
| `clock_gating` | Enable automatic clock gates | `true` |
| `reset_coverage` | `all` or `minimal` | `all` |
| `cells` | Gate and synchronizer cell bindings | Behavioral wrappers |
| `address_inputs` | Address selection input ports | Empty |
| `address_selectors` | Conditions over address inputs | Empty |
| `initiators` | Request endpoints | 1 to 256 |
| `targets` | Response endpoints | 1 to 256 |
| `connectivity` | Allowed endpoint pairs | Required |
| `topology` | Explicit request and response graphs | One generated switch per direction |
| `arbitration` | Object with `policy` | Round robin |
| `transport` | Network widths, capacity and sideband fields | Defaults below |
| `error_logging` | Register window and error logs | Absent |
| `output` | Backend, language and register reset style | Required in YAML, with `backend` and `language` |
| `templates` | YAML endpoint templates | Empty |
| `groups` | YAML connectivity groups | Empty |

The [clocks manual](clocks-and-resets.md) describes clock and reset fields. The
[network manual](network.md) describes topology and connectivity. The
[address manual](address-map.md) describes selectors and mappings.

## Endpoint clock gating

Both `initiators[]` and `targets[]` accept a `clock_gating` object.
It applies to all flows of the endpoint. See [clock gates](clocks-and-resets.md#clock-gates)
for the fields and allowed combinations. The corresponding C++ fields hold
an optional `EndpointClockGates`.

## Transport fields

Endpoint `data_width` sets physical bus width. Nodes and links can override the
[network word size](network.md#graph-objects).

| Field | Meaning | Default or range |
| --- | --- | --- |
| `data_width` | Network width | 32 or 64 bits. The default is the smallest endpoint width, capped at 64 bits. APB engines count as 32 bits |
| `max_outstanding` | Default initiator capacity | 1, range 1 to 64 |
| `id_table_entries` | Default initiator ordering-key capacity | Derived from capacity and ID width |
| `transport_word_bytes` | Default word size of switches and links, in bytes | 4, 8, 16 or 32. Defaults to `transport.data_width` divided by 8 |
| `packet_limits` | Narrower limits for connected endpoint pairs | Empty |
| `user_flags` | Named one-bit request fields | Empty, at most 256 |
| `response_user_bits` | Response user field width | 0, at most 256 |
| `data_user_bits_per_byte` | Data user bits per data byte | 0 to 4, default 0 |
| `compact_failure_responses` | Use one internal failure class | `true` or `false`, default `false`. Set `true` only if every AXI initiator sets `slverr_only: true` and no log records failure classes |

APB and Wishbone endpoints require a 32-bit network. Endpoint widths can
differ from the network width under the [protocol rules](protocols.md). The
[transactions manual](transactions.md) describes capacity and ordering keys.

APB5 uses endpoint `user_width`, `user_flags`, `user` and `response_user_width`
for [user signals](user-signals.md#apb5-user-ports). The request port is at most
128 bits and the response port at most 16 bits. Both default to zero.

Endpoint `data_user_width` enables PWUSER and PRUSER. It defaults to 0. On APB5
endpoints it is 0 or `transport.data_user_bits_per_byte * data_width / 8`.
A nonzero width requires a transport value from 1 to 4. Other protocols accept
zero. The C++ fields have the same names, defaults and limits.

## Initiator error responses

`initiators[].slverr_only` accepts `true` or `false` and defaults to `false`.
It applies to full AXI and AXI4-Lite initiators.
The C++ field is `Initiator::slverr_only`. See
[error responses](address-map.md#error-responses).

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

## TileLink endpoint fields

TL-UL and TL-UH endpoints use 32-bit data and take `source_width`,
`size_width`, `sink_width` and `max_transfer_bytes`. Other protocols reject
these fields. C++ uses `Protocol::TlUl` and `Protocol::TlUh`.

See [TL-UL fields](tilelink-ul.md),
[TL-UH initiators](tilelink-uh-initiators.md#tl-uh-initiators) and
[TL-UH targets](tilelink-uh-targets.md#tl-uh-targets) for ranges, defaults
and limits.

## AXI5 endpoint fields

`axi5` and `ace5_lite` endpoints take the `axi4` fields and an optional `axi5`
object of [AXI5 properties](axi5.md) and [ACE5-Lite rules](ace5-lite.md).
Other protocols reject the object. C++ endpoints use `Protocol::Axi5` or
`Protocol::Ace5Lite` and optional `Axi5Properties axi5`.

## AHB5 endpoint fields

`protocol: ahb5` has four-bit HPROT by default. The optional `ahb5` object
accepts `hprot_width: 4` or `7`, `hnonsec: false` or `true`, and
`hmaster_width` from 0 to 32. HNONSEC defaults to false and HMASTER width to 0.
Use `exclusive: false` and zero for `hauser_width`, `hwuser_width`,
`hruser_width` and `hbuser_width`. Other values produce `NCL_AHB5_PROPERTIES`.
Other protocols reject the object, including explicit defaults.

AHB5 targets require `attributes: amba` and `hresp_width: 1`, or an omitted
`hresp_width`. See [AHB5 basic properties](ahb.md#ahb5-basic-properties).
C++ endpoints use `Protocol::Ahb5` and optional `Ahb5Properties ahb5`.

```yaml
protocol: ahb5
ahb5: {hprot_width: 7, hnonsec: true, hmaster_width: 32}
```

## Wishbone endpoint fields

`protocol: wishbone` requires `address_unit: byte` or `word` on both endpoint
roles. There is no default. Targets can set `retry: error` to add RTY as an
error input. Its default is no RTY port. Other protocols reject both fields.

C++ uses `Protocol::Wishbone`. Set both `Initiator::address_unit` and
`Target::address_unit` to `AddressUnit::Byte` or `AddressUnit::Word`.
Target `retry` optionally takes `WishboneRetry::Error`.

Data and network widths must be 32 bits. `cycles` defaults to `classic` and
also accepts `pipelined`. The C++ endpoint field takes `WishboneCycles::Classic`
or `WishboneCycles::Pipelined`.

Pipelined initiators accept `max_outstanding` from 1 to 64 and inherit the
transport default of 1. Targets and classic initiators have capacity 1.

`burst_tags` is an optional boolean on both roles. `true` adds CTI and BTE
ports in classic mode. Omission and `false` omit them. C++ uses optional
`bool burst_tags` on `Initiator` and `Target`.

These cases produce `NCL_WISHBONE_OPTION`: the field on another protocol,
including `false`, `burst_tags: true` with `cycles: pipelined`, and a tagged
target with `timeout`. There are no `granularity` or `endianness` fields.
See [Wishbone](wishbone.md).

## Endpoint templates

A template contains endpoint fields under `templates.initiators` or
`templates.targets`. An endpoint selects one template with `use: cpu` or several
with `use: [cpu, fast]`. A template cannot set `name` or `use`. Templates and
the endpoint must set disjoint fields. Unknown templates and unused templates
are rejected.

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

Missing endpoint `protocol` or `data_width` produces `NCL_SCHEMA_MISSING_FIELD`.
These fields belong on each endpoint. `id_table_entries` limits active
ordering keys.

Only `schema_version: 10` is accepted. Values 1 through 9 produce
`NCL_SCHEMA_REMOVED`. Other values produce `NCL_SCHEMA_VERSION`.

A derived invalid network width also reports `NCL_NETWORK_DATA_WIDTH` at
`transport.data_width`. With a 64-bit network, every initiator must be 64 bits.
A 32-bit endpoint makes the derived network width 32. Set
`transport.data_width: 64` to use a 64-bit network with 32-bit AXI targets.
Full AXI, AHB and Wishbone initiator addresses are 12 to 48 bits. AXI4-Lite
initiator addresses are 2 to 48 bits.

APB initiator addresses are 1 to 32 bits. Initiator widths cannot exceed the
global width. Omitted endpoint address widths inherit the global width.

Every YAML map, sequence, key and value counts toward the node limit. Size,
depth and node limit violations produce `NCL_RESOURCE_LIMIT`. Empty templates
produce `NCL_TEMPLATE_FIELD`.

Expanded diagnostics point to the template or group name that produced the field
or pair.
