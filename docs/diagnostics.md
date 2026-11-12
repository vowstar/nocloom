# Diagnostics

A diagnostic contains a code, field path and message. It can also contain a
source line and column, and notes.

This page lists the codes that a definition, a command line or an input file
can produce. Any other code, for example a code that starts with
`NCL_INTERNAL_`, reports a failed internal check of the generator. Report it as
a defect and include the definition.

## Input and output

| Code | Condition |
| --- | --- |
| `NCL_SCHEMA_SYNTAX` | The YAML text cannot be parsed |
| `NCL_SCHEMA_DUPLICATE_KEY` | A mapping repeats a key |
| `NCL_SCHEMA_YAML_FEATURE` | Anchors, aliases, merge keys, explicit tags or more than one YAML document |
| `NCL_SCHEMA_VERSION` | Unsupported schema version |
| `NCL_SCHEMA_MISSING_FIELD` | Required field missing, including endpoint protocol or width |
| `NCL_SCHEMA_UNKNOWN_FIELD` | Unknown input key, including arbitration fields other than `policy`, or unsupported top-level field |
| `NCL_SCHEMA_TYPE` | Wrong value type or enum value |
| `NCL_SCHEMA_FIELD_VERSION` | `synchronizer_stages` is set without an explicitly clocked graph |
| `NCL_RESOURCE_LIMIT` | YAML size, depth or node count exceeds its limit, or a map that `diff` reads exceeds the size or depth limit |
| `NCL_NAME_INVALID`, `NCL_NAME_COLLISION` | Invalid or conflicting identifier |
| `NCL_INPUT_FORMAT`, `NCL_DIFF_MAP` | Unsupported file extension or invalid comparison map |
| `NCL_UNSUPPORTED_BACKEND` | Unsupported backend or a CDC output request without crossings |
| `NCL_IO_PATH` | Unsafe output directory or artifact path, including symbolic links |
| `NCL_IO_EXISTS`, `NCL_IO_READ`, `NCL_IO_WRITE` | Output already present, unreadable input or output write failure |
| `NCL_C_HEADER_NAME` | Two generated C macros or include guards have the same name |
| `NCL_C_HEADER_RESERVED` | A generated C macro or include guard is reserved in C11 or C++20 |
| `NCL_ADDRESS_TABLE_INPUT` | Artifact set has missing, duplicate or conflicting table inputs |
| `NCL_IPXACT_INPUT` | Invalid IP-XACT vendor, library or version text, or an artifact set that is not from this definition |

See [definition](definition.md), [CLI](cli.md) and [output formats](outputs.md).
The codes of the C++ tree helper are in
[Tree topology helper](#tree-topology-helper).

## Endpoints and transactions

| Code | Condition |
| --- | --- |
| `NCL_ENDPOINT_DATA_WIDTH`, `NCL_NETWORK_DATA_WIDTH` | Unsupported endpoint or resolved network width |
| `NCL_ENDPOINT_ADDRESS_WIDTH` | Endpoint address port width outside its protocol limits, or a mapping's local interval outside that width, including `error_logging.registers.local_address` |
| `NCL_ENDPOINT_TRANSACTION_WIDTH` | Unsupported ID or length width for the endpoint protocol |
| `NCL_UNSUPPORTED_WIDTH`, `NCL_WIDTH_CONVERSION` | `transport.data_width` is not 32 or 64, or an endpoint width conversion that the network does not support |
| `NCL_AXI4_TRANSPORT_WORD` | `transport_word_bytes` is not 4, 8, 16 or 32 |
| `NCL_TARGET_TIMEOUT` | `timeout.cycles` is not a power of two from 16 to 1048576, or the target protocol has no timeout |
| `NCL_TARGET_APB_ACCESS`, `NCL_TARGET_FIXED_BURSTS` | Back-to-back access on a target that is not APB, or an invalid `fixed_bursts` value |
| `NCL_AHB5_PROPERTIES` | The `ahb5` object requires AHB5. HPROT width must be 4 or 7. HMASTER width must be 0 to 32. A nonzero target width must hold each reaching source. Exclusive and user signals are unsupported. Targets accept only `attributes: amba`, without per-bit overrides |
| `NCL_TARGET_AHB_BURSTS`, `NCL_TARGET_AHB_HRESP` | Burst shaping or `hresp_width` on a target that is not AHB, an invalid value, or two-bit HRESP on AHB5 |
| `NCL_ENDPOINT_ACCESS` | `access` is not `read_write`, `read_only` or `write_only` |
| `NCL_APB_INITIATOR`, `NCL_AHB_INITIATOR` | Unsupported options on a dedicated initiator |
| `NCL_UNSUPPORTED_PROTOCOL` | Unsupported protocol or endpoint role |
| `NCL_AHB_OUTLET_PROFILE`, `NCL_APB_BURST_OUTLET_PROFILE` | An AHB-Lite or APB target needs a 32-bit network and at most 32 local address bits |
| `NCL_AXI_LITE_OUTLET_PROFILE` | An AXI4-Lite target needs 32-bit or 64-bit data and at most 32 local address bits |
| `NCL_AXI4_CAPACITY` | Capacity outside the initiator or target range |
| `NCL_AXI4_CAPACITY_COMBINATION` | Unsupported capacity combination, including a multi-burst initiator with more than 256 routing windows |
| `NCL_AXI4_ID_CAPACITY` | Invalid, unused or unsupported ID table configuration |
| `NCL_AXI4_EXCHANGE_ID_WIDTH` | Target ID cannot hold required flow or exclusive-owner bits |
| `NCL_AXI4_NARROW_BURSTS` | Unsupported narrow mode or conflict with target read side effects |
| `NCL_AXI4_PACKETS` | Invalid pair packet limits or an unsupported initiator for those limits |
| `NCL_TARGET_ATTRIBUTES` | Missing, unsupported or invalid target attribute mapping |
| `NCL_USER_FLAGS` | Invalid user flags |
| `NCL_TARGET_EXCLUSIVE` | Invalid exclusive mode, owner flags, monitors or reaching initiators |
| `NCL_TARGET_READ_INTERLEAVE` | Explicit interleave size or its network profile is unsupported |
| `NCL_UNSUPPORTED_TOPOLOGY` | Initiator or target count outside 1 to 256. Clock domain count outside 1 to 64 in a clocked graph, or other than 1 without one. An unsupported error-log shape or revision value |

See [protocols](protocols.md), [transactions](transactions.md),
[packets](packets.md) and [error logs](error-logging.md).

## Connectivity and routes

| Code | Condition |
| --- | --- |
| `NCL_CONNECTIVITY_REQUIRED` | No explicit connectivity choice |
| `NCL_CONNECTIVITY_REFERENCE` | Unknown or unreachable endpoint, invalid flow index or no mapping within the initiator address limit |
| `NCL_CONNECTIVITY_DUPLICATE`, `NCL_CONNECTIVITY_OVERLAP` | Repeated flow pair or overlapping shorthand initiator rows |
| `NCL_CONNECTIVITY_UNUSED_SOURCE`, `NCL_CONNECTIVITY_UNUSED_TARGET` | Unused endpoint or mapping block |
| `NCL_SOURCE_PATH`, `NCL_TARGET_PATH` | Invalid flow definitions or mapping flow selections |
| `NCL_ROUTE_REFERENCE`, `NCL_ROUTE_DUPLICATE`, `NCL_ROUTE_PATH` | Invalid route endpoint, repeated flow pair or invalid path |
| `NCL_SWITCH_ROUTE_MISSING`, `NCL_SWITCH_ROUTE_AMBIGUOUS` | Inferred route absent or not unique |
| `NCL_SWITCH_ROUTE_CYCLE`, `NCL_SWITCH_UNUSED_OBJECT` | Route wait cycle or unused node or link |
| `NCL_UNSUPPORTED_ARBITRATION` | Unknown arbitration policy |
| `NCL_ARBITRATION_OUTPUT` | Unknown, repeated or single-candidate output override |
| `NCL_ARBITRATION_PRIORITY` | Invalid priority permutation or priority on another policy |
| `NCL_SWITCH_GRAPH` | A topology direction has no `entries` or no `exits` |
| `NCL_SWITCH_IDENTITY` | A topology direction attaches an endpoint more than once in `entries` or in `exits`, or an attachment has an invalid endpoint name |
| `NCL_SWITCH_EDGE_DUPLICATE` | Two links of one topology direction have the same `from` and `to` |
| `NCL_SWITCH_PAIR` | A connected endpoint has no attachment in a topology direction, or no connected pair uses an attachment |
| `NCL_AXI4_TOPOLOGY` | A topology direction has no node or more than 128 nodes. A node name is invalid or repeated. A node of a clocked graph has no `clock_domain`. A link or attachment names an absent node. A link or attachment pipe sets `half_rate` without `forward` and `backward`, or `bypass_port` without `forward` |
| `NCL_AXI4_EXCHANGE_PROFILE` | `transport_word_bytes` of a node or link is not 4, 8, 16 or 32 |
| `NCL_ENDPOINT_PIPE` | Unsupported endpoint pipe configuration |
| `NCL_NODE_PIPE_CONFLICT`, `NCL_NODE_PIPE_UNUSED` | Ambiguous or unused node pipe defaults |

See [network](network.md), [arbitration](arbitration.md) and [pipes](pipes.md).

## Address selection and shorthand

| Code | Condition |
| --- | --- |
| `NCL_ADDRESS_OVERLAP`, `NCL_MODE_CONDITION` | Conflicting mappings or invalid selection conditions |
| `NCL_ADDRESS_INPUT`, `NCL_ADDRESS_SELECTOR` | Invalid or unused address inputs or selectors |
| `NCL_TEMPLATE_CONFLICT`, `NCL_TEMPLATE_FIELD` | Overlapping template fields, empty template or forbidden template field |
| `NCL_TEMPLATE_REFERENCE`, `NCL_TEMPLATE_UNUSED` | Unknown or unused template |
| `NCL_GROUP_NAME`, `NCL_GROUP_REFERENCE` | Ambiguous group name or unknown member |
| `NCL_GROUP_DUPLICATE`, `NCL_GROUP_UNUSED` | Repeated group member or unused group |
| `NCL_MIRROR_RENAME`, `NCL_MIRROR_OVERRIDE` | Invalid suffix transformation or invalid mirrored link override |
| `NCL_ADDRESS_WIDTH` | Global `address_width` outside 2 to 48 |
| `NCL_ADDRESS_RANGE` | Invalid address string or mapping range, alignment or size. Includes `error_logging.registers.local_address` |

See [address maps](address-map.md) and [definition shorthand](definition.md).

## Clocks and scheduling

| Code | Condition |
| --- | --- |
| `NCL_DOMAIN_REFERENCE`, `NCL_RESET_PORT` | Unknown or unused clock/reset binding, invalid release mode or invalid reset handshake |
| `NCL_CLOCK_DIVISION` | Unsupported `clock_type`, or missing, unused or incompatible ratio, enable or ungate fields |
| `NCL_CLOCK_ROOT` | Unknown or missing clock root, or clock roots without a clocked graph |
| `NCL_CLOCK_EDGE` | Unknown, self-referencing, cyclic or cross-root `edge_parent` |
| `NCL_EXCHANGE_CLOCK_ASSIGNMENT` | A node names an undeclared clock domain, or a declared domain of a clocked graph has no endpoint and no node |
| `NCL_CDC_KIND` | Crossing `kind` is not `async`, `related` or `edge_aligned` |
| `NCL_CDC_PERIOD_RANGE`, `NCL_CDC_DEPTH_RANGE`, `NCL_CDC_STAGES_UNSUPPORTED` | Unsupported period, depth or synchronizer chain length |
| `NCL_CDC_PERIOD_PARTIAL`, `NCL_CDC_PERIOD_UNUSED` | Incomplete or unused crossing period declaration |
| `NCL_CDC_CROSSING_SAME_DOMAIN`, `NCL_CDC_CROSSING_DUPLICATE`, `NCL_CDC_CROSSING_UNUSED` | Same-domain, duplicate or unused crossing override |
| `NCL_CDC_KIND_FIELD` | An option does not apply to the selected crossing kind |
| `NCL_CDC_RELATED_DYNAMIC`, `NCL_CDC_RELATED_ROOT`, `NCL_CDC_RELATED_RESET` | Unsupported clock type, root relationship or independent reset for related clocks |
| `NCL_CDC_LOW_LATENCY_CREDIT_DEPTH` | Fast related crossing depth differs from two |
| `NCL_CDC_EDGE_ALIGNED_RELATION` | Missing fixed-ratio ancestry or integer edge ratio |
| `NCL_QOS_LEVEL`, `NCL_QOS_MAP` | Invalid fixed level or AxQOS map, or incompatible initiator options |
| `NCL_QOS_SHAPER`, `NCL_QOS_SCHEDULER` | Invalid byte budgets, levels, threshold or required companion configuration |
| `NCL_QOS_PROFILE` | Unsupported endpoint, width, whole-burst or scheduler-pipe combination. AHB sources accept fixed levels only, to full AXI targets when nonzero |

See [clocks and resets](clocks-and-resets.md) and [scheduling](scheduling.md).

## Tree topology helper

The C++ function `tree_topology` returns these codes. It also returns
`NCL_NAME_INVALID` for an invalid name and `NCL_SWITCH_UNUSED_OBJECT` for a
node that is on no route.

| Code | Condition |
| --- | --- |
| `NCL_TREE_NODE_COUNT` | Node count outside 1 to 128 |
| `NCL_TREE_INITIATOR_COUNT` | No initiator name |
| `NCL_TREE_TARGET_COUNT` | No node has a target |
| `NCL_TREE_NODE_DUPLICATE` | A node name occurs more than once |
| `NCL_TREE_INITIATOR_DUPLICATE` | An initiator name occurs more than once |
| `NCL_TREE_TARGET_DUPLICATE` | A target name occurs more than once |
| `NCL_TREE_ROOT_COUNT` | No node or more than one node is without a parent |
| `NCL_TREE_PARENT_MISSING` | A parent names an absent node |
| `NCL_TREE_PARENT_SELF` | A node is its own parent |
| `NCL_TREE_CYCLE` | The parent links contain a cycle |

See the [tree topology helper](library.md#tree-topology-helper).

## Mesh topology helper

The C++ function `mesh_topology` returns these codes. It also returns
`NCL_NAME_INVALID` for an invalid endpoint name.

| Code | Condition |
| --- | --- |
| `NCL_MESH_DIMENSIONS` | A zero dimension or a grid with more than 128 cells |
| `NCL_MESH_ENDPOINT` | An endpoint list outside 1 to 256. Duplicate attachments within a role, out-of-grid coordinates or flow counts outside 1 to 32 |
| `NCL_MESH_CONNECTIVITY` | `all` with a pair list, an unknown endpoint, an invalid flow index, a repeated admitted flow pair or an endpoint without an admitted pair |

See the [mesh topology helper](library.md#mesh-topology-helper).
