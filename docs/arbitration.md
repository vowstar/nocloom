# Arbitration

An output chooses the highest
[scheduling pressure](scheduling.md#scheduling-pressure-rules) before applying
its policy among equal candidates.

| Unit | Meaning |
| --- | --- |
| Fragment | One transfer on a network channel |
| Packet | One or more fragments |
| Segment | A consecutive part of an interleaved response packet |

A request packet keeps its output until the receiver accepts its final fragment.
An interleaved response releases its output at segment boundaries. Stalls and
gaps within a segment do not release the output.

| Policy | Equal-level selection |
| --- | --- |
| `round_robin` | Rotate after service |
| `fifo` | Oldest request arrival. Simultaneous arrivals use the output's deterministic candidate order |
| `rotating_priority` | Rotate priority among candidates |
| `level_round_robin` | Keep a separate round-robin position per level |
| `fixed_priority` | Use the declared candidate order |

A node can override its individual outputs with another explicit policy.

The top-level `arbitration` object has an optional `policy`. The default is `round_robin`.
A node's `arbitration` field is a list of output overrides. Each entry requires
`output` and `policy`.

`fixed_priority` is valid only in this list. Its `priority` list must contain
every candidate once, highest first. An output is named `node/name` or
`endpoint/name`. Candidate names use `node/name` for predecessors and
`endpoint/name` for entries.

## Arbitration order and bounds

Candidates sort by endpoint or predecessor node name in byte order. A node sorts
before an endpoint with the same name. Reads and writes share the output arbiter
state. A single-candidate output has no arbiter and rejects an override. Unknown
outputs, repeated overrides and incomplete or duplicate priority entries are
rejected.

Only fixed priority accepts `priority`.

| Policy | Other packets served while one candidate waits, with `n` candidates |
| --- | --- |
| `round_robin`, `fifo` | At most `n - 1` |
| `rotating_priority` | At most `2^ceil(log2(n))` |
| `level_round_robin` | At most `n - 1` at the same level |
| `fixed_priority` | No bound. Higher-priority candidates must eventually yield |

FIFO simultaneous arrivals keep their previous order, highest candidate first
after reset. A served candidate goes last. A withdrawn request loses its place.
Round robin starts at candidate 0 after reset and searches after the last
released candidate. Its cursor advances only when the packet or segment tail
transfers. Level round robin starts each level at candidate 0 and searches
after the last candidate served at that level.

Serving one level does not move another level's turn.

Rotating priority advances a wrapping `ceil(log2(n))`-bit counter after every
packet, regardless of the winner. Its priority tree recursively splits into a
lower `floor(n/2)` half and an upper half. The highest counter bit selects the
upper half first, and lower bits order each half. The reset counter is zero. For
three candidates, successive orders are `0,1,2 / 0,2,1 / 1,2,0 / 2,1,0`.

The map records effective policies, candidate order, override origin, fixed
priority order and service bounds. Bounds count completed competing packets and
require eventual consumer acceptance. The level round robin bound applies while
a request stays valid at one level. A packet counts at the level that received
its grant. Interleaved response segments compete again after releasing their grants.

The full AXI request merger prefers reads after reset. Reads have no source
data to wait for. Selecting a write first can delay a simultaneous read by tens
of cycles while the write waits for W data. After a burst ends, the other
direction gets priority.

A selected direction keeps its grant through capacity and ordering waits.
A started burst holds the path until the next stage accepts its final packet.
This keeps its packets together while the two directions share state for
capacity, burst pieces and target order.

An AXI4-Lite transfer has one beat at the initiator. The merger checks shared
capacity before selection and holds the grant until the receiver accepts the
transfer's final packet. The directions can compete again after each
single-beat transfer. A write competes with its data ready, so serving it first
in a reset tie lets the buffered write data leave sooner.
Each release flips the preference bit directly, keeping the selected
direction out of the preference update. When writes arrive first, writes
then average 7.66 cycles, against 7.75 cycles when the winner sets the
preference.

A full AXI read or write that stays eligible waits for at most one competing
burst. With AXI4-Lite, either direction waits for at most two competing
transfers while it stays eligible. These bounds count accepted final packets,
including any competing grant already held. The receiver must eventually
accept each grant. Data, capacity, ordering and backpressure can extend the
wait in cycles.
