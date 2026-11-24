# Network

Requests and responses travel through separate directed graphs. Without
`topology`, NocLoom generates one switch per direction from connectivity. With
`topology`, you declare the physical graphs and their endpoint attachments.

The C++ [mesh helper](library.md#mesh-topology-helper) builds these graphs and
explicit X-then-Y routes from grid dimensions and endpoint coordinates.

See [read and write paths](read-write-paths.md) for separate graphs.

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
| `links` | `from`, `to`, optional `transport_word_bytes`, `pipeline`, `sender_pipeline`, `stages` | Directed switch links |
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

A physical graph can contain cycles. A route can revisit a node only through
another link. NocLoom rejects routes that form a cycle of waits. See
[route cycle report](#route-cycle-report). Routes remain fixed during
operation.

See [explicit routes](../examples/explicit_routes.yaml).

## Route cycle report

NocLoom rejects a cycle of waits with `NCL_SWITCH_ROUTE_CYCLE`.

| Term | Meaning |
| --- | --- |
| Wait | Two successive links of a route. A packet that holds the first link waits for the second link |
| Cycle | A closed chain of waits. Each wait ends on the link that starts the next wait |
| Contributor | A route that takes both links of a wait, the second directly after the first |

NocLoom examines the request graph and the response graph separately. Each
graph has the routes of all connected flow pairs, one route for each flow pair.
A flow pair without an explicit route in a direction takes its inferred route.
This is the only route over the declared links between its two attachment
nodes. See [routes](#routes).

Each reported cycle has one diagnostic at `topology.request.links` or
`topology.response.links`, with one note per wait in cycle order. Request
diagnostics come before response diagnostics.

In this one-way ring of four nodes, `n0` to `n3`, all pairs connect.
Each initiator and target attaches to the node with its number.

```text
NCL_SWITCH_ROUTE_CYCLE [topology.request.links]: Routes chain the links of the cycle n0 -> n1 -> n2 -> n3 -> n0; no deadlock claim is made
note: n0 -> n1 waits for n1 -> n2 in 3 request routes: s0 to t2, s0 to t3, s3 to t2
note: n1 -> n2 waits for n2 -> n3 in 3 request routes: s0 to t3, s1 to t0, s1 to t3
note: n2 -> n3 waits for n3 -> n0 in 3 request routes: s1 to t0, s2 to t0, s2 to t1
note: n3 -> n0 waits for n0 -> n1 in 3 request routes: s2 to t1, s3 to t1, s3 to t2
```

The CLI prints the file, line and column of the links list before the code.
A cycle of waits alone does not establish a deadlock.

| Part of a note | Meaning |
| --- | --- |
| `n0 -> n1 waits for n1 -> n2` | The held link, then the next link |
| `in 3 request routes` | The number of contributors of the wait. The number is exact when the note lists fewer. The direction is `request` or `response` |
| `s0 to t2` | A contributor. The initiator is first in both directions. Explicit and inferred routes have the same form |
| `s0 flow 1 to t2` | A contributor with a flow index. Only an endpoint with more than one flow shows its index |
| `and 97 more` | The number of contributors that the note does not list |

To break a cycle, remove all contributors of one wait. Use `topology.routes`
to reroute each flow pair around the wait, or disconnect the pair.
The smallest contributor count gives the fewest flow pairs to change for this
cycle. The graph determines which alternative paths are available.

| Item | Order |
| --- | --- |
| Cycle name | The node list in the message. It starts with the link that is first in byte order, by `from` and then by `to` |
| Cycles | The cycle with the fewest links is first. Cycles of equal length follow the byte order of their names |
| Notes | The order of the waits around the cycle, from the first link of the name |
| Contributors | The byte order of their names. `t10` is before `t2` |

The report does not depend on the order of the declarations.

The CLI takes both limits in `validate` and `generate`. In C++ both are members
of `ValidationOptions`.

| CLI option | Member | Default | Values |
| --- | --- | --- | --- |
| `--cycle-limit` | `cycle_limit` | 4 | The cycles reported for each direction, 1 to 1024. 0 and values above 1024 are rejected with `NCL_VALIDATION_OPTION` |
| `--contributor-limit` | `contributor_limit` | 8 | The contributors listed in each note. 0 lists all contributors |

The report takes at most one extra cycle in report order per direction to check
for truncation. If it finds one, the last diagnostic for that direction gets a
note such as `more request cycles are present; the cycle limit is 4`. The note
gives only the direction and the limit.

A limit in range affects only the report. Validation and generated artifacts
are independent of that limit.

A route that takes a link twice forms a cycle from its own waits. A link from a
node to itself is also rejected with `NCL_SWITCH_ROUTE_CYCLE`. Its message
starts with `The link of the cycle`, and it has no notes.

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
with exits. It does not copy node arbitration or stage lists.

The object form `response: {mirror: ...}` accepts `rename_suffix` and
`link_overrides`. `rename_suffix` has `from` and `to`, and every request node
must have that suffix. An override selects one mirrored link by response node
names. It can replace width, `pipeline`, `sender_pipeline` or `stages`. Unknown,
duplicate, empty and redundant overrides are rejected.

See the [short topology example](../examples/scale_axi_12_short.yaml).

## Connectivity validation and misses

`connectivity` is required even for a single pair. C++ callers explicitly set
all pairs or a list. Unknown endpoints, duplicate admitted flow pairs and `all`
combined with entries are rejected. Every connected target flow needs a mapping
below its initiator's address limit. Every aligned mapping block must be
reachable by at least one connected initiator.

Flow indices must fit their endpoint. An explicit route for an unconnected flow
pair is rejected. Connectivity entry order does not affect generation. The map
lists admitted flow pairs and each route's flow indices.

A miss request uses one flow pair, regardless of its initiator flow. Without a
named [default target](address-map.md#decode-misses), it takes the mapping that
owns the selected aligned block. With a named default, it takes that target's
smallest reached mapping. Both cases use that mapping's target flow and the
lowest initiator flow reaching it. The answer returns over its AxID's flow and
that target flow. If the matrix leaves out that pair, the answer returns over
the miss's pair.

## Route and word behavior

Every node and link must carry a route. Attachments must be known and unique.
Each `request` and `response` node list must be nonempty and connect its
endpoint attachments.

One flow pair can have only one route entry. Request and response paths are
independent. A route cannot take a link twice. All routes together must avoid
cycles of waits between successive links. An unused physical cycle is accepted
when no route continues around it. See
[route cycle report](#route-cycle-report).

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

See [Link pipes](pipes.md#link-pipes), [stage lists](pipes.md#stage-lists) and
[Arbitration](arbitration.md#arbitration).
