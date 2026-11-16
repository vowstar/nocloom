# TileLink

Set `protocol: tl_ul` on an initiator or target. The interface has channels A
and D with 32-bit data. It supports single-beat Get, PutFullData and
PutPartialData, with AccessAck and AccessAckData responses. The rules use
[TileLink 1.8.1][spec], sections 3, 4, 6 and 7.

| Protocol value | Roles | Operations |
| --- | --- | --- |
| `tl_ul` | Initiator or target | Single-beat Get, PutFullData and PutPartialData of up to 4 bytes, with AccessAck and AccessAckData |
| `tl_uh` | Initiator or target | Get, PutFullData and PutPartialData of up to `max_transfer_bytes`. Initiators also complete Intent locally and deny atomic operations |

The field table and the Initiators, Targets and Mapping to other endpoints
sections describe TL-UL. The clock, reset and port rules apply to both
protocols. The option table covers both. See
[TL-UH initiators](#tl-uh-initiators) and [TL-UH targets](#tl-uh-targets)
for their fields and behavior.

## Endpoint fields

This table describes TL-UL endpoints.

| Field | Meaning | Values | Default |
| --- | --- | --- | --- |
| `data_width` | A and D data bits | 32 bits | Required |
| `source_width` | A and D source bits | 0 to 16 bits | Initiator: 4. Target: 0, derived from its capacity of one |
| `size_width` | A and D size encoding bits | 2 to 4 bits | 2 |
| `sink_width` | D sink bits | 0 to 16 bits | 0 |
| `max_transfer_bytes` | Largest supported message | 4 bytes | 4 |
| Initiator `max_outstanding` | Shared read and write capacity | 1 to 64 requests, reads and writes combined | `transport.max_outstanding`, which defaults to 1 |
| Target `max_outstanding` | In-flight message capacity | 1 | 1 |
| Target `flows` | Target port sets | 1 | 1 |
| Target `fixed_bursts` | Incoming FIXED packet handling | `reject` or `single` | `reject` |
| Target `read_side_effects` | Preserve non-modifiable read byte ranges | Boolean | `false` |

Zero source or sink width omits that signal. The implicit source is then zero.
A wider target source port drives zero in all bits. D sink is zero on an
initiator and ignored on a target. A larger size port does not permit larger
TL-UL messages. Legal sizes remain 0, 1 and 2, for 1, 2 and 4 bytes.

The address is a byte address. Initiators accept 2 to 48 address bits. Targets
accept 2 to 32 local address bits. A mapping must hold at least one target
word. The network data width is 32 bits.

`id_width` and `len_width` are rejected. TL-UL uses `source_width` and has no
length port. Initiators reject `access`, `narrow_bursts`, user ports and changes
to port pipes. Targets reject `attributes` and user ports.

Nonzero scheduling levels with TileLink endpoints are rejected. TL-UL
initiators reject shapers and schedulers. An APB shaper can limit traffic to a
TL-UL target at level zero.
A network with error logs cannot have TL-UL initiators. Error logs cannot
observe TL-UL targets.
TL-UL sources cannot drive audit flags or access error log registers.

Unsupported field combinations produce `NCL_TILELINK_PROFILE` or the
applicable generic diagnostic.

```yaml
initiators:
  - {name: cpu, clock_domain: main, protocol: tl_ul, data_width: 32,
     source_width: 4, max_outstanding: 4}
targets:
  - {name: regs, clock_domain: main, protocol: tl_ul, data_width: 32,
     mappings: [{base: '0x0', size: '0x1000'}]}
```

## Initiators

A TL-UL external master sends A requests and accepts D responses. The initiator
captures each request before it enters the network. A request that has not
handshaken can be withdrawn or changed, as section 4.1 permits.

`a_source` maps to the network transaction ID and returns as `d_source`.
Different sources can complete in any order. The external master must use a
source only once until its D response is accepted, including that clock edge.
Requests with different sources can remain in flight together. These rules
follow sections 6.4 and 6.5.

Network read and write responses share D. Each accepted response gives
priority to the other direction. A waiting write response can be preceded by
at most one read response. A lone response uses the next free D slot.
D backpressure can extend the wait.

| Request field | Behavior |
| --- | --- |
| Get size and address | Select the read bytes. Get ignores its mask |
| Put mask | Selects write bytes within the active lanes. Sparse masks are supported |
| Data lanes | Little-endian byte order |
| Get and Put A param | Reserved bits are ignored |
| Attributes sent to other protocols | PROT `010`, CACHE `0000`, LOCK zero, no user or QoS information |

TL-UL includes only Get and Put operations. An external master that sends any
other opcode, including Intent, violates the TL-UL port protocol. The port
returns denied without target access for every such opcode and param value.
Sizes above 2 and writes marked `a_corrupt` are also denied without access.

A rejected message is consumed before its response. The response size and
source copy the request, and param is zero.

| Rejected request | Response |
| --- | --- |
| Get, ArithmeticData or LogicalData | Denied AccessAckData with zero data and corrupt set on every beat |
| PutFullData or PutPartialData | Denied AccessAck |
| Intent, for param 0 through 7 | Denied HintAck |
| Opcode 6 or 7 | One denied AccessAck. This is not a TL-C completion |

## Targets

The target sends A requests and accepts D responses. It keeps at most one
TileLink message in flight, with source zero. It reuses source only after the
response handshake. Increasing `source_width` does not increase capacity.
The external slave must support Get, PutFullData and PutPartialData for sizes
0 through 2 at every mapped address.

A network packet becomes naturally aligned single-beat messages.

| Packet content | Target behavior |
| --- | --- |
| Read byte interval | Split into aligned blocks |
| Full target word write | One PutFullData |
| Partial word write | Can use several smaller PutPartialData messages, in increasing byte order |
| Zero-strobe write | No target access |
| AXI packet with several TileLink writes | Write response combines their results |
| FIXED burst | Rejected without access unless `fixed_bursts: single` is set |

Packet formation can merge narrow AXI reads into a larger interval. To retain
the original byte range of non-modifiable reads, set `read_side_effects: true`.
See [read side effects](packets.md#read-side-effects).

`a_param` and `a_corrupt` are zero. Get data is zero. Unselected write data
bytes are zero. Protection, cache, QoS and user information have no TileLink
ports.
The port accepts a response in the same cycle as its A handshake. D ready
depends on local response storage, with no combinational dependency on A ready.

## Errors and progress

The error mapping follows section 4.4, with the write limitation below.

| Event at a TL-UL initiator | D denied | D corrupt | Read data |
| --- | --- | --- | --- |
| Local rejection of a data-returning request | 1 | 1 on every beat | Zero |
| Network read error, including decode miss or timeout completion | 0 | 1 | Zero |
| Any write error | 1 | 0 | Not used |
| Successful read or write | 0 | 0 | Read result |

Read denied is sent only when the port has accessed no target. Write denied
means that the write failed. Part of a write can already have reached a
target. Write denied does not guarantee that there were no side effects.
The network does not roll back completed accesses.

At a TileLink target, denied or corrupt read data becomes a target error with
zero data. A denied write becomes a target error. An AXI initiator receives
SLVERR for these errors with either value of `slverr_only`.

TileLink targets reject `timeout: {completion: true, ...}`. A watchdog with
`completion: false` can report expiry through `nl_t_<name>_timed_out` for
system recovery. It does not insert a replacement response.

Section 4.3 requires timeouts when bridging to buses that do not guarantee
progress. NocLoom accepts non-TileLink targets without such a timeout. Forward
progress then depends on those targets completing requests. A completing
[timeout](timeout-and-progress.md#target-timeouts) can provide error completion
when a non-TileLink target stops. A master must accept D responses in finite
time without waiting for later A handshakes. See the shared
[progress requirements](timeout-and-progress.md#shared-request-and-response-paths).

## Clock, reset and ports

Both ends of each TileLink interface must use the endpoint domain's clock,
reset and power. The network reset is active low. Invert the domain reset when
connecting an external active-high TileLink reset. Hold valid low for at least
100 reset cycles, as section 3.2.2 requires.

Reset all domains together. An isolated reset requires no traffic across its
boundary. Internal clock domain crossings use the network's crossing
and reset rules. The domain clock can be gated through the endpoint's normal
`clock_gating` setting.

Ports use `nl_i_<name>_a_opcode` and `nl_t_<name>_a_opcode`. The same prefix
applies to other A and D signals.

D outputs come from local state or constants. Handshake outputs have no
combinational dependency on external handshake inputs. TL-UH A ready also
uses the offered opcode, size and source to check admission.

| Output | TileLink information |
| --- | --- |
| Map ports | Signal widths and directions |
| Map `definition.tilelink` | Each endpoint's effective widths, maximum message size and capacity |
| IP-XACT | Plain model ports, without an AMBA bus interface mapping |

## Option table

Supported means implemented within the limits above. Rejected means that the
definition has a diagnostic. Not applicable describes a constant or runtime
response for behavior that has no matching network operation.

TL-UH initiators deny atomic operations and do not conform to TL-UH.
TL-UH targets send only Get, PutFullData and PutPartialData messages.

| Level | Option | Class | Reason or behavior | Sections |
| --- | --- | --- | --- | --- |
| TL-UL | Get | Supported | Single-beat read | 7.2.1 |
| TL-UL | PutFullData | Supported | Single-beat write with its size mask | 7.2.2 |
| TL-UL | PutPartialData | Supported | Single-beat write with any subset of its size mask | 7.2.3 |
| TL-UL | AccessAck | Supported | One write response | 7.2.4 |
| TL-UL | AccessAckData | Supported | One read response | 7.2.5 |
| TL-UL | Source | Supported | Initiator transaction ID. Target has one source in flight | 6.4 |
| TL-UL | Size | Supported | 0, 1 or 2 with natural alignment | 4.5, 7.2 |
| TL-UL | Mask | Supported | One bit per byte, within the active lanes | 4.5 |
| TL-UL | A corrupt | Supported | A corrupt write is denied without access. Target drives zero | 4.4, 7.2.2, 7.2.3 |
| TL-UL | D corrupt | Supported | Read error flag. Zero on AccessAck | 4.4, 7.2.4, 7.2.5 |
| TL-UL | Denied | Supported | Error mapping with the write side-effect limitation above | 4.4 |
| TL-UL | Sink | Not applicable | Initiator drives zero. Target ignores it | 6.4, 7.2.4, 7.2.5 |
| TL-UL | Param | Not applicable | Outputs are zero. Reserved input values are ignored | 7.2 |
| TL-UL | Multiple beats | Not applicable | Larger runtime requests are denied without access | 7.2 |
| TL-UL | ArithmeticData, LogicalData | Not applicable | Runtime requests are denied without access | 8.2.1, 8.2.2 |
| TL-UL | Intent, HintAck | Not applicable | Runtime Intent with any param is denied with HintAck without access | 8.2.3, 8.2.4 |
| TL-UH | Get | Supported | One A beat and size-dependent D beats | 7.2.1, 8.3 |
| TL-UH | PutFullData | Supported | Size-dependent A beats and one AccessAck | 7.2.2, 8.3 |
| TL-UH | PutPartialData | Supported | Per-beat masks and one AccessAck | 7.2.3, 8.3 |
| TL-UH | ArithmeticData | Not applicable | Local denied AccessAckData after all A beats | 8.2.1 |
| TL-UH | LogicalData | Not applicable | Local denied AccessAckData after all A beats | 8.2.2 |
| TL-UH | Intent | Supported on initiators | Local HintAck, param 0 or 1 succeeds | 8.2.3 |
| TL-UH | AccessAck, AccessAckData | Supported | One message owns D until its last handshake | 7.2.4, 7.2.5 |
| TL-UH | HintAck | Supported on initiators | One response to a local Intent | 8.2.4 |
| TL-UH | Source and size | Supported | Initiators retain source through the last D handshake. Targets retain source through packet retirement | 6.4, 8.3 |
| TL-UH | Mask | Supported | Per-beat byte mask, including zero on PutPartialData | 4.5, 8.3 |
| TL-UH | Corrupt and denied | Supported | Initiators map network read errors to corrupt without denied. Targets receive denied and corrupt as target errors | 4.4 |
| TL-UH | Bursts | Supported | Naturally aligned power-of-two messages | 8.3 |
| TL-C | B, C and E channels and coherent operations | Rejected | No protocol value or ports. Unknown fields are rejected | 9 |

## Mapping to other endpoints

From a TL-UL initiator to another protocol's target:

| TileLink request or result | AXI4 | APB | AHB-Lite |
| --- | --- | --- | --- |
| Get | One beat with AxSIZE equal to A size | A physical-word read | One or more aligned reads |
| PutFullData, PutPartialData | One W beat with WSTRB from A mask | PSTRB on APB4. APB3 and APB2 reject partial physical words | Writes shaped to active bytes |
| Source | Network transaction ID. Target IDs are assigned separately | No bus ID | No bus ID |
| Target read error | RRESP error gives corrupt AccessAckData | PSLVERR gives corrupt AccessAckData | ERROR gives corrupt AccessAckData |
| Target write error | BRESP error gives denied AccessAck | PSLVERR gives denied AccessAck | ERROR gives denied AccessAck |

Requests supply PROT `010`, CACHE `0000` and LOCK zero to the target attribute
mapping. With `attributes: amba`, these become the corresponding AXI, APB and
AHB outputs. Other attribute mappings can change them.

From another protocol's initiator to a TL-UL target:

| Incoming behavior | TL-UL target behavior |
| --- | --- |
| AXI read or write burst | Aligned single-beat messages for each packet |
| AXI WSTRB or APB4 PSTRB | Write only selected bytes. A sparse word can use several messages |
| AHB-Lite transfer | Aligned Get or Put messages for its bytes |
| Denied or corrupt read response | Source receives a target error, with zero read data |
| Denied write response | Source receives a write error |
| ID and attribute information | Target source is zero. Attribute information has no bus ports |

A byte or halfword Get through an APB target still accesses its physical word.
The external device must permit that read granularity. See
[width conversion](width-conversion.md) for mixed endpoint widths and
[AHB transfers](ahb.md#ahb-target-transfers) for partial-write shaping.

[spec]: https://sifive.cdn.prismic.io/sifive/7bef6f5c-ed3a-4712-866a-1a2e0c6b7b13_tilelink_spec_1.8.1.pdf

## TL-UH initiators

Set `protocol: tl_uh` on an initiator. The port and the network use 32-bit
data. Use the shared [clock, reset and port rules](#clock-reset-and-ports).
For target fields, see [TL-UH targets](#tl-uh-targets).

| Field | Values | Default |
| --- | --- | --- |
| `data_width` | 32 bits | Required |
| `max_transfer_bytes` | Powers of two from 4 to 4096 bytes | 64 |
| `size_width` | 2 to 4 bits. It must encode the size of `max_transfer_bytes` | 3 |
| `source_width` | 0 to 16 bits | 4 |
| `sink_width` | 0 to 16 bits | 0 |
| `max_outstanding` | 1 to 64 messages | `transport.max_outstanding`, which defaults to 1 |

The size value is the base-two logarithm of the message bytes. For example,
a 64-byte message has size 6. A 3-bit size port encodes up to 128 bytes.
A larger `max_transfer_bytes` needs `size_width: 4`. The default size width
does not change with `max_transfer_bytes`. Every configuration accepts
1-byte and 2-byte accesses.

TL-UH initiators reject `id_width`, `len_width`, `access`, `narrow_bursts`,
user ports, pipe changes, a 64-bit network, nonzero scheduling levels,
shapers and error logs. Invalid TileLink fields produce
`NCL_TILELINK_PROFILE`. Other limits produce the applicable generic diagnostic.

```yaml
initiators:
  - {name: dma, clock_domain: main, protocol: tl_uh, data_width: 32,
     max_transfer_bytes: 256, size_width: 4, max_outstanding: 4}
```

Get has one A beat and `max(1, bytes / 4)` D beats. Put has that many A
beats and one AccessAck. All A control fields stay constant across a Put.
Data, mask and corrupt are sampled only on each handshake. An unaccepted
beat can be withdrawn or changed.

Each corrupt Put beat sends zero write strobes and makes the final AccessAck
denied. The port does not roll back the other writes.

For byte and halfword Puts, only mask bits within the addressed bytes
become write strobes. Other mask bits are ignored.

Messages of up to 4 bytes use one AXI INCR beat with size equal to A size.
Larger messages use full-word AXI INCR transactions.

| Message bytes | Transactions | Beats per transaction |
| --- | --- | --- |
| 8 to 1024 | 1 | Message bytes divided by 4 |
| 2048 | 2 | 256 |
| 4096 | 4 | 256 |

Each transaction starts 1024 bytes after the previous one. All write
results merge into one AccessAck, with the original source and size.
Reads retain their requested byte range through packet formation and width
conversion, subject to the target's physical read granularity.

| Local request | Response | Network access |
| --- | --- | --- |
| Intent param 0 or 1 with supported size | One successful HintAck | None |
| Intent with another param or unsupported size | One denied HintAck | None |
| ArithmeticData or LogicalData, all params | All size-dependent AccessAckData beats, denied and corrupt, with zero data | None |
| Get or Put larger than `max_transfer_bytes`, or not aligned to its size | Denied response after all A beats. Get returns all size-dependent beats with corrupt set and zero data | None |
| Opcode 6 or 7 | One denied AccessAck | None |

Atomic responses start after all A data beats have arrived. AccessAck and
HintAck always clear corrupt. Network read failures clear denied and set
corrupt with zero data only on each failed beat.

A D message owns the channel until its final handshake, including gaps in
network data. Write responses enter independent completion storage even
when D is stalled. A message slot and its saved source remain allocated
until the final D handshake. Different sources can complete out of order.
The next D owner is selected in round-robin slot order, starting after the
previous owner. A network read can be selected when its next response beat
is at the front of the internal read-response queue.

Several single-beat Gets can be in flight. A multibeat Get waits for all
other Gets to retire, and blocks other Gets until it retires.

A multibeat D message blocks new first A beats from its selection through its
final handshake. This block does not apply to AccessAck or HintAck responses.
A Put already in progress can continue. A master can present a source again
after its first D handshake. The port holds that new first A until the
original message retires.

Reads and writes share an internal network transaction capacity of
`min(64, max(4, max_outstanding))`, recorded as
`definition.tilelink.initiators.<name>.internal_transaction_capacity` in the
map. The internal AXI engine has that many request slots per direction.
Requests in these slots can wait for shared network capacity.

A 4096-byte message to one destination can issue four transactions without
waiting for write responses. The transactions of one message complete in
order. A change of destination can wait for earlier transactions to drain.
A Get D message ends only after all its read beats have handshaken.

## TL-UH targets

Set `protocol: tl_uh` on a target. The port and the network use 32-bit data.
The target sends Get, PutFullData and PutPartialData. It sends no Intent or
atomic operations. Use the shared
[clock, reset and port rules](#clock-reset-and-ports).

| Field | Values | Default |
| --- | --- | --- |
| `data_width` | 32 bits | Required |
| `max_transfer_bytes` | Powers of two from 4 to 4096 bytes | 64 |
| `max_outstanding` | 1 to 16 packets in flight, reads and writes combined | 1 |
| `source_width` | At least ceil(log2(max_outstanding)), at most 16 bits | ceil(log2(max_outstanding)), zero for one context |
| `size_width` | 2 to 4 bits, sufficient to encode the maximum size | 3 |
| `sink_width` | 0 to 16 bits | 0 |
| `flows` | 1 | 1 |
| `fixed_bursts` | `reject` or `single` | `reject` |
| `read_side_effects` | Boolean | `false` |

Set `size_width: 4` for a maximum above 128 bytes.

Addresses are byte addresses of 2 to 32 local address bits. Mappings must
align their base, size and local address to `max_transfer_bytes`.
Targets reject `id_width`, `len_width`, exclusive monitors, attribute ports,
user ports and nonzero scheduling levels. Error logs cannot observe TL-UH
targets. Invalid TileLink fields, unaligned mappings and exclusive monitors
produce `NCL_TILELINK_PROFILE`. Other limits produce the applicable generic
diagnostic.

Each packet owns one context until its last network response is accepted.
The context number is its TileLink source, with zeroes in any extra high bits.
Each packet has at most one TileLink message in flight. The source remains
reserved between that packet's messages and through network backpressure.

Independent read keys can issue Get messages concurrently. Reads with the same
network entry, flow and AXI ID wait for the preceding packet to retire. Writes
keep their acceptance order across all messages of each packet. Only the
earliest unfinished write packet can send Put messages. A Put owns A through its
final beat. While a write waits for Ack, independent Gets can use A.

The first accepted D beat selects its owner through the final D beat.

With more than one context, read responses from independent packets can
alternate at TileLink message boundaries. All response words of one message
stay together, including gaps between valid words. A write response waits
until that group ends.

The target covers each received byte interval with aligned power-of-two
messages. Each block fits the remaining interval, mapping and maximum size.
Messages use bytes from one network packet.

An aligned AXI128 request with 256 beats can form one 4096-byte target
message. A TL32 4096-byte ingress message first splits into four internal
transactions and can form four 1024-byte target messages. Reads access no
bytes outside that interval.

Set `read_side_effects: true` to preserve non-modifiable AXI read byte ranges
during packet formation. An unaligned INCR interval ends at the beat-aligned
start plus the original beat count times the beat size.

Packet formation and target messages keep the WRAP tail before the head. With
`fixed_bursts: single`, every repeated beat still causes its own access. With
read side effects enabled, non-modifiable WRAP runs use exact blocks.

A single complete active write range can use PutFullData. Other writes use
PutPartialData. Writes with packet transfers smaller than 4 bytes keep each
word in separate messages. Writes with 4-byte packet transfers can use
multibeat messages.

An independent zero-mask word causes no target access. Zero-mask beats inside
a multibeat Put are sent. `a_param` and `a_corrupt` are zero. Unselected data
bytes are zero.

The target accepts AccessAck before the last A beat, including the first A
handshake cycle. It stores the Ack independently of read data. The target sends
the remaining A beats after an early denied Ack. D ready does not depend on A
ready. The next message starts after both A and D complete.

A denied read receives all expected D beats and returns zero data and a
target error. Corrupt without denied fails only that beat. Write denied
merges into the packet's write response. Response length follows the saved
request, independent of the received D size or opcode. Interface checkers
report invalid external responses.

`timeout.completion: true` is rejected. With `completion: false`, expiry
sets `timed_out` and retains all pending work until normal completion or
common reset. It inserts no replacement response.
