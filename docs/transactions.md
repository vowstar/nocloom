# Transactions

Related manuals: [Read response interleaving](read-responses.md),
[Packets](packets.md), [Progress and target timeouts](timeout-and-progress.md),
[Exclusive access](exclusive-access.md).

## Initiator capacity

| Initiator | `max_outstanding` |
| --- | --- |
| AXI4, AXI3, AXI4-Lite | 1 to 64, reads and writes combined |
| APB, AHB-Lite | 1 |

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

APB and AHB-Lite targets accept only `max_outstanding: 1`. An AHB target can
accept another packet before the previous packet's final response leaves. The
default is 1. Targets do not inherit `transport.max_outstanding`.

AXI4 and AXI3 requests hold their entry until their final response leaves the
target adapter. Their writes can overlap across target IDs. Read overlap follows
target interleaving and width conversion constraints. AXI4-Lite changes
direction only after all earlier packets have answered.

Saved WRAP words do not reduce admission capacity. See
[WRAP conversion](width-conversion.md#width-transformation-details).

## Target identifiers

When a target has an ID port, read target IDs and flow indices from
`definition.target_ids` in the map. Sources and targets have separate compact
index spaces. Endpoints use name order, with each endpoint's flows in increasing
local index order and no padding.

| Map field | Meaning |
| --- | --- |
| `source_paths`, `target_paths` | Global index of each endpoint flow |
| `route_bits` | Transaction field widths. `region_index` is zero |
| `route_fields` | Transaction fields with `lsb` and `width` |
| `record_fields` | Observation fields with `lsb` and `width` |

The transaction fields, from low to high, are direction, `sequence` (source AxID),
source flow and target flow. Zero-width fields are absent. The AxID field has the
widest connected source ID width. Direction is present when a connected source
uses AXI, AXI4-Lite or AHB. APB sources contribute zero to both fields. Mapping
block indices occupy no transaction bits.

Let `F` be the target's flow bits and `S` its remaining ID bits.
The sum of the `S`-bit slices of the transaction word modulo `2^S` is the sequence
value.
The port places the local target flow in its lowest `F` bits and reverses the
sequence bits above it. If `S` is zero, only the flow is present. Target port
widths come from `id_width` or the target's default width rule.

See [ID guarantees](#target-id-guarantees) for ordering and exclusive accesses.

Error log route fields have their own layout in `definition.error_logs`. They
are not target port AxID values. See [error logging](error-logging.md).

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
received. APB and AHB reject multi-packet configured capacity. Lite can overlap
packets in one direction only.

Splitting, conversion and crossings preserve per-ID order. Split bursts progress
independently of unrelated earlier bursts. Later bursts follow once their
requests and W beats leave. See [packet capacity](packets.md#bounded-packets). A
converted packet requiring several target bursts instead waits for earlier
responses and excludes later packets until its final response returns.

## Target ID guarantees

| Target ID rule | Guarantee |
| --- | --- |
| Stable key | Initiator, initiator flow, AxID, direction and target flow |
| Sharing | Different keys can share an ID even with a wider port |
| Flow | Lowest `bit_width(flows - 1)` bits |
| Target-monitor owner | Upper bits, first flag name in byte order at the top |

Requests sharing a target ID obey that ID's AXI order, including no read
interleaving within that ID. IDs depend on the whole network. Renaming endpoints
or changing mappings, connectivity or initiator ID widths can change all target
IDs.

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
