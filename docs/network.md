# Network

Requests and responses travel through separate directed graphs. Without
`topology`, NocLoom generates one switch per direction from connectivity. With
`topology`, you declare the physical graphs and their endpoint attachments.

The C++ [mesh helper](library.md#mesh-topology-helper) builds these graphs and
explicit X-then-Y routes from grid dimensions and endpoint coordinates.

Related manuals: [Arbitration](arbitration.md), [Pipes](pipes.md).

## Connectivity

`connectivity: all` connects every initiator and target pair. A mapping form
lists the targets each initiator can reach.

An endpoint flow groups transactions for route selection. Initiator
`route_classes` and target `flows` define these groups. `source_path` and
`target_path` select the initiator and target flows by index, starting at zero.
In field names, path means flow. See [endpoint flows](#endpoint-flows).

Each YAML row lists target names or objects with `target`, optional
`source_path` and optional `target_path`. C++ uses `ConnectivityEntry` with
`source`, `target` and the optional flow indices.

Every initiator and target must occur in at least one admitted pair. Duplicate
pairs are rejected unless they select distinct flow pairs. Each omitted
connectivity flow index independently admits every flow of that endpoint.

In `topology.routes`, each omitted index instead defaults to 0. An unconnected
pair gets an error response even when the physical graph has a route.

## Graph objects

Each of `topology.request` and `topology.response` contains:

| List | Entry fields | Meaning |
| --- | --- | --- |
| `nodes` | `name`, optional `clock_domain`, `transport_word_bytes`, `arbitration` | 1 to 128 switches |
| `links` | `from`, `to`, optional `transport_word_bytes`, `pipeline`, `sender_pipeline` | Directed switch links |
| `entries` | `endpoint`, `node`, optional `pipeline` | Endpoints that send packets into this graph |
| `exits` | `endpoint`, `node`, optional `pipeline` | Endpoints that receive packets from this graph |

Request entries are initiators and request exits are targets. Response entries
are targets and response exits are initiators. Node names are local to each
graph. Node and link word overrides are 4, 8, 16 or 32 bytes. Every endpoint has
one attachment in each graph. Links cannot be duplicated or connect a node to
itself.

In a clocked graph, every node names a declared clock domain. A link between
domains receives the crossing configured for that directed domain pair. See
[clocks and resets](clocks-and-resets.md).

## Routes

`topology.routes` entries contain:

| Field | Meaning |
| --- | --- |
| `initiator`, `target` | Endpoint names |
| `source_path`, `target_path` | Flow indices, default 0 |
| `request` | Node names from the initiator attachment to the target attachment |
| `response` | Node names from the target attachment to the initiator attachment |

Each route follows declared directed links. At least one direction is explicit.
An omitted direction must have one unique route. Each connected flow pair
requires its own explicit entry or a unique inferred route. An ambiguous
inferred route is rejected.

A physical graph can contain cycles when its explicit routes and resource waits
are legal. A route can revisit a node only through another link. NocLoom rejects
route dependency cycles detected by topology validation. Routes remain fixed
during operation.

See [explicit routes](../examples/explicit_routes.yaml).

## Endpoint flows

An AXI4 or AXI3 initiator can declare named `route_classes`. `source_path` is
the declaration index, starting at zero. `default_class` must name a declared
class and receives every ID outside `id_ranges`. It can name a class at any
declaration index. Each range has inclusive integer `first` and `last` endpoints
and a `class` name.

Ranges of different classes must not overlap. Adjacent or overlapping ranges of
the same class merge during generation. Class declaration order does not set a
matching priority.

```yaml
route_classes: [ordinary, urgent]
default_class: ordinary
id_ranges:
  - {first: 4, last: 7, class: urgent}
  - {first: 12, last: 12, class: urgent}
```

Every declared class must receive at least one ID. There are at most 32 classes
and at most as many classes as ID values. The validator sets no per-class range
count limit. A zero-bit ID permits one direct class.

Without declared classes, the source has one direct class. All classes share the
endpoint's ports, capacity and ID ordering.

The captured AW or AR ID selects the class without a queue or capacity wait. W,
every burst packet and its responses keep that class.

A target sets `flows` to a count from 1 to 32. Every target flow has at least
one mapping that names it with `flow`. APB, AHB and AXI4-Lite targets have one
complete port set per flow. AXI4 and AXI3 targets identify flows in their AxID
values. See [target identifiers](transactions.md#target-identifiers).

## Mirrored response graph

`response: mirror` reverses request links and swaps their sender and receiver
pipes. It retains node names, widths, clocks and order, and exchanges entries
with exits. It does not copy node arbitration.

The object form `response: {mirror: ...}` accepts `rename_suffix` and
`link_overrides`. `rename_suffix` has `from` and `to`, and every request node
must have that suffix. An override selects one mirrored link by response node
names. It can replace width, `pipeline` or `sender_pipeline`. Unknown,
duplicate, empty and redundant overrides are rejected.

See the [short topology example](../examples/scale_axi_12_short.yaml).

## Connectivity validation and misses

`connectivity` is required even for a single pair. C++ callers explicitly set
all pairs or a list. Unknown endpoints, duplicate admitted flow pairs and `all`
combined with entries are rejected. Every connected target flow needs a mapping
below its initiator's address limit. Every aligned mapping block must be
reachable by at least one connected initiator.

Flow indices must fit their endpoint. An explicit route for an unconnected flow
pair is rejected. Connectivity entry order does not affect generation.

A miss uses one flow pair for both routes, regardless of the original initiator
flow. Without a named default target, it takes the mapping that owns the
selected aligned block. With a named default target, it takes that target's
smallest reached mapping. Both cases use that mapping's target flow and the
lowest initiator flow reaching it. The map lists admitted flow pairs and each
route's flow indices. See
[default-target selection](address-map.md#decode-misses).

## Route and word behavior

Every node and link must carry a route. Attachments must be known and unique.
Each `request` and `response` node list must be nonempty and connect its
endpoint attachments.

One flow pair can have only one route entry. Request and response paths are
independent. A route cannot take a link twice. All routes together must avoid
cycles of waits between successive links. An unused physical cycle is accepted
when no route continues around it.

Without a link override, NocLoom selects the narrower endpoint word that still
carries the slower endpoint's bandwidth. Endpoint bandwidth is word bytes
divided by clock period. Same-domain endpoints, or endpoints without both
periods, use the narrower word. Conversions precede a crossing in the sender and
follow it in the receiver.

Write beats gather until a word fills, the packet ends or address progression
stops at FIXED or the WRAP container end. Narrowing sends successive portions of
each word. Packing requires full-word beats or a target that accepts several
beats per transfer word. See
[response gathering](interface-timing.md#shared-response-words).

## Related topics

See [Link pipes](pipes.md#link-pipes).

See [Arbitration](arbitration.md#arbitration).
