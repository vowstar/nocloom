# Transactions

Related manuals: [Read response interleaving](read-responses.md),
[Packets](packets.md), [Progress and target timeouts](timeout-and-progress.md),
[Exclusive access](exclusive-access.md).

## Initiator capacity

| Initiator | `max_outstanding` |
| --- | --- |
| AXI4, AXI3, AXI4-Lite | 1 to 64, reads and writes combined |
| APB, AHB-Lite, AHB5, Wishbone | 1 |

An initiator's `max_outstanding` overrides `transport.max_outstanding`. A burst
takes one capacity entry when its first packet leaves and releases it when its
final response packet returns. A burst completed locally takes no capacity
entry. Each single-beat piece of a narrow-split burst uses its own entry.

An ordering key is direction plus AxID, or just direction for AXI4-Lite. It can
have several bursts at one destination but drains them before changing
destination. Different IDs can answer in either order.

`id_table_entries` bounds the number of active ordering keys across reads and
writes. Its default is the smaller of initiator capacity and the number of
available keys. A new key waits while the table is full.

An entry frees when its last response reaches the initiator.

## Target capacity

AXI4 and AXI3 targets accept `max_outstanding` from 1 to 16. Reads and writes
share that packet capacity. AXI4-Lite targets accept `max_outstanding` from 1 to
256 packets per direction.

APB, AHB-Lite, AHB5 and Wishbone targets accept only `max_outstanding: 1`. An
AHB target can accept another packet before the previous packet's final response
leaves. The default is 1. Targets do not inherit `transport.max_outstanding`.

AXI4 and AXI3 requests hold their entry until their final response leaves the
target adapter. Their writes can overlap across target IDs. Read overlap follows
target interleaving and width conversion constraints. AXI4-Lite changes
direction only after all earlier packets have answered.

Saved WRAP words do not reduce admission capacity. See
[WRAP conversion](width-conversion.md#width-transformation-details).

## Target identifiers

The ID port places the target flow in its lowest F bits. If the port holds all
three fields, the initiator flow index and initiator AxID follow. The map lists
the layout in `definition.target_ids.ports.<target>` when any target ID port has
bits above its flow. Otherwise each port carries only the target flow.

| Field, low to high | Width | Content |
| --- | --- | --- |
| `flow` | F = `bit_width(flows - 1)` | Target flow of the mapping that holds the burst |
| `source_index` | P = `bit_width(c - 1)` for c reaching initiator flows, or zero for at most one | Index of the initiator flow of the request |
| `axid` | W, the widest ID of the initiators that reach the target | Initiator AxID, zero-extended |

The index counts from 0. It takes the initiators that reach the target in byte
order of their names, and the flows of each initiator in local order. A flow
that does not reach the target has no index.

| Initiator | ID bits that count for W |
| --- | --- |
| AXI4, AXI3 | `id_width` |
| TL-UL | `source_width` |
| TL-UH | `ceil(log2(max_outstanding))`, at least 1 |
| APB, AHB, AXI4-Lite, Wishbone | 0 |

| Map field in `ports.<target>` | Meaning |
| --- | --- |
| `width` | Width of the ID port |
| `fields` | Fields on the port with `lsb` and `width`. Zero-width fields are absent |
| `sources` | Initiator and `source_path` of each index, in index order |
| `folded` | True when the port carries the folded form |

Target port widths come from `id_width` or the
[default width](width-conversion.md#axi-targets). A default width for several
bursts holds all three fields when each connected initiator has one flow.

| Port width | What the port carries |
| --- | --- |
| At least F + P + W | The three fields, zeros above them |
| Less than F + P + W | The [folded form](#folded-target-identifiers) |

A normal key consists of the target flow, initiator flow and source AxID.
Equal keys share a field ID and distinct keys have distinct field IDs.
AXI orders reads and writes separately, so a field ID has no direction bit.

## Folded target identifiers

A port that cannot hold the three fields uses a folded ID. Its transaction word
uses the compact flow indices of the whole network.
Sources and targets have separate index spaces. Endpoints use name order, with
each endpoint's flows in increasing local index order and no padding.

| Map field in `definition.target_ids` | Meaning |
| --- | --- |
| `source_paths`, `target_paths` | Global index of each endpoint flow |
| `route_bits` | Transaction field widths. `region_index` is zero |
| `route_fields` | Transaction fields with `lsb` and `width` |
| `record_fields` | Observation fields with `lsb` and `width` |

The transaction fields, from low to high, are direction, `sequence` (source
AxID), source flow and target flow. Zero-width fields are absent. The AxID field
has the widest ID width across all connected initiators in the network.
Direction is present when the network has a connected initiator other than APB.
It is 1 for a write and 0 for a read. APB initiators contribute zero to both
fields.

The map includes `route_bits` and `route_fields` if any port is folded or every
ID port carries just its flow.
Mapping block indices occupy no transaction bits.

With more than 16 address selectors, `record_fields` sizes `region_index` for
the largest block index of any connected initiator and target. A source that
reaches one target reserves the next index if its map reports a possible
hole. Other misses use index zero. A field for indices 0 through B needs the
bit width of B. Reserving one index adds at most one bit.

The local target flow occupies the lowest `F` bits. Let `S` be the number of
bits above it. If `S` is zero, the flow is the entire ID.

Otherwise, split the transaction word into `S`-bit groups from its low end.
A last group with fewer than `S` bits adds only its own bits.
Each bit above the flow is the XOR of the bits at that position in all groups.
Changing just one group changes the ID.

See [ID guarantees](#target-id-guarantees) for ordering and exclusive accesses.

Error log records have their own path, sequence and region fields in
`definition.error_logs`. They are not target port AxID values. See
[error logging](error-logging.md).

## Capacity and ordering boundaries

At capacity one, reads and writes share one admitted burst. The next starts as
the previous final response reaches the initiator staging. A staged R or B can
wait at the external port while another burst is admitted. AW and AR port slots
can each hold another burst beyond that admitted capacity. The full AXI engine
can also collect two additional AW descriptors and one AR descriptor behind the
active request.

AWREADY and ARREADY depend on local state, without a combinational path from
network ready.

The full AXI request merger selects a direction before waiting for an ordering
key or destination change. A selected waiting request blocks the other
direction. A waiting write can also keep WREADY low. AXI4-Lite selects eligible
offers and excludes directions without free capacity. In a burst spanning
several targets, its ID binds to its last beat's destination. A following burst
of that ID starts immediately only when it starts there.

Local completions count as a destination and obey same-ID ordering.

An explicit ID table has 1 through the smaller of capacity and available
direction-ID keys. A full AXI initiator with capacity one rejects
`id_table_entries`. A global ID table value that no multi-burst initiator uses
is rejected with `NCL_AXI4_ID_CAPACITY`. A Lite read/write table of one key
blocks switching direction until that key frees. Two keys remove that table
restriction.

More than 256 routing windows with any multi-burst initiator produces
`NCL_AXI4_CAPACITY_COMBINATION`.

A target capacity entry returns in the cycle after its final response leaves.
Target reads issue AR in arrival order and match the oldest pending read of RID.
Writes issue AW and W in arrival order. AW can precede completion of the
preceding write's W.

B completes the oldest write of BID. Different target IDs can answer in either
order. An unmatched R produces SLVERR for its beat. An unmatched B is held with
BREADY low until it matches a pending write.

Refused directions still occupy target capacity entries until their local
responses leave. A target-local write completion precedes later writes of its
target ID. A target-local read completion waits until earlier reads are
received. APB, AHB and Wishbone reject multi-packet configured capacity. Lite
can overlap packets in one direction only.

Splitting, conversion and crossings preserve per-ID order. Split bursts progress
independently of unrelated earlier bursts. Later bursts follow once their
requests and W beats leave. See [packet capacity](packets.md#bounded-packets). A
converted packet requiring several target bursts instead waits for earlier
responses and excludes later packets until its final response returns.

## Target ID guarantees

| Target ID rule | Guarantee |
| --- | --- |
| Stable key | Initiator flow, AxID and target flow. A folded ID also depends on the direction |
| Sharing, fields | Equal normal keys share an ID. Distinct normal keys have distinct IDs |
| Sharing, folded | Different keys can share an ID |
| Flow | Lowest `bit_width(flows - 1)` bits |
| Target-monitor owner | Upper bits, first flag name in byte order at the top |

Requests sharing a target ID obey that ID's AXI order, including no read
interleaving within that ID. The field IDs of a target depend only on the
initiators connected to it: their names, flows and ID widths. Changes elsewhere
in the network do not change them. Folded IDs depend on the whole network.
Renaming endpoints or changing mappings, connectivity or initiator ID widths can
change all folded IDs.

Target flows share one engine and capacity. AXI without an ID port permits only
one target flow. APB selects only the active flow's PSEL and PENABLE while
broadcasting payload signals. AHB selects HSEL for the address flow and samples
responses from the flow of the accepted data phase.

With no data-phase flow selected, the selected ready value is high.
The adapter broadcasts this value on `hready_<flow>` outside
reset. Other outputs except HSEL are also broadcast. See
[AHB target ready connections](ahb.md#target-ready-connections).

Lite selects AWVALID, WVALID and ARVALID by flow and drains
outstanding packets before switching flow. Repeated target port names end in
`_<flow>`. AXI uses one port set and returns the flow in BID and RID.

## Related topics

See [Target timeouts](timeout-and-progress.md#target-timeouts).

See [Exclusive access](exclusive-access.md#exclusive-access).

See
[Shared request and response paths](timeout-and-progress.md#shared-request-and-response-paths).

See [Bounded packets](packets.md#bounded-packets).

See [Interleaved read data](read-responses.md#interleaved-read-data).
