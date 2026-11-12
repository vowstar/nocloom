# AHB-Lite and AHB5

## AHB-Lite initiators

An external AHB-Lite manager drives this initiator. The initiator port acts as
its subordinate. HREADY is an input that samples the address phase. With one
manager and one subordinate, connect HREADY to HREADYOUT. The response is
one-bit HRESP.

| Input transfer | Network behavior |
| --- | --- |
| SINGLE | One transfer of HSIZE at HADDR |
| INCR4, INCR8, INCR16 | An INCR piece of that length |
| WRAP4, WRAP8, WRAP16 | A WRAP piece of that length |
| Undefined INCR | One piece for each transfer committed to its data phase |

A piece starts in its first data phase. If a manager ends a fixed length piece
early, unused write beats have zero strobes and unused read results are
discarded. Undefined INCR never reads ahead of committed transfers. Each piece
stays within the 1 KiB boundary. BUSY retains an unfinished burst.

A bufferable write completes as its data enters the network and discards its B
result. Other writes wait for the piece's response on the last beat.

Failed reads and non-bufferable writes return two-cycle ERROR. The manager can
cancel or continue the burst.

Requests are Non-secure. Locked transfers, an HSIZE wider than the bus and
narrow non-SINGLE bursts return ERROR without target access. AHB5 signals,
multi-manager arbitration, RETRY and SPLIT responses are not available on this
input.

Its address width is at least 12 bits. It rejects ID, length, access,
narrow-burst and port-pipe configuration.

## AHB-Lite initiator ordering

IDLE and BUSY outside a data phase complete without wait states. A piece waits
in its first data phase until the network accepts it. Ending an undefined INCR
waits only for its committed transfer before starting another data phase. HRDATA
is valid only on successful read completion.

With target `attributes: amba`, HPROT bits 3:2 select the cache values below.

| HPROT[3:2] | ARCACHE | AWCACHE |
| --- | --- | --- |
| `00` | `0000` | `0000` |
| `01` | `0001` | `0001` |
| `10` | `1010` | `0110` |
| `11` | `1011` | `0111` |

AxPROT is `{~HPROT[0], 1, HPROT[1]}`. AxID and AxLOCK are zero. A request
waits for earlier write responses, so reads observe earlier bufferable writes.

Bufferable writes never report target errors. Other writes report the piece's
accumulated error on its last beat only. Holding BUSY inside a write holds its
request path.

An AHB initiator rejects pair packet-limit overrides and any capacity above one.
An `ahb_v3` initiator has no AHB5 security or manager-ID ports. Common reset
cancels its burst, and the external manager must restart it.

## Dedicated AHB targets

| Field | Behavior |
| --- | --- |
| `write_bursts: packet` | Multi-beat incrementing writes of at least 32 bits use undefined INCR. Other writes use SINGLE |
| `write_bursts: complete` | Same write shapes and bus ownership as `packet` |
| `undefined_length_bursts: true` | Permit INCR. This is the default |
| `undefined_length_bursts: false` | Split undefined INCR reads into INCR16, INCR8, INCR4 and SINGLE blocks. Keep WRAP reads. Writes use SINGLE |
| `hresp_width: 1` | AMBA 3 AHB-Lite ERROR and OKAY |
| `hresp_width: 2` | AMBA 2 responses. ERROR fails. RETRY and SPLIT cancel the next address and fail the current packet |

Partial-write shaping and response handling follow the
[transfer rules](#ahb-target-transfers).

## Target ready connections

Target port names use `nl_t_<target>_<signal>`. Multiple target flows add
`_<flow>` after the signal name.

| Network target port | Direction at network | External slave connection |
| --- | --- | --- |
| `hreadyout` | Input | Slave HREADYOUT, or the data-phase response multiplexor output |
| `hready` | Output | Slave HREADY input |

With one slave, connect its HREADYOUT to `hreadyout` and `hready` to its HREADY.
With several slaves on one port set, decode HSEL for each slave and select
HREADYOUT, HRDATA and HRESP using the accepted address phase. Feed the selected
response into the network and distribute `hready` to every slave.

With multiple target flows, the adapter selects `hreadyout_<flow>` for the
current data-phase flow. It records that flow when its address phase is
accepted. It broadcasts the selected ready value on every `hready_<flow>`.
With no data-phase flow selected, the selected ready value is high.
During common reset, `hready` follows HREADYOUT and HTRANS is IDLE.

These connections follow the slave and multiplexor signals in
[AMBA 3 AHB-Lite, IHI 0033A, sections 2.3 and 2.5](https://documentation-service.arm.com/static/5f914801f86e16515cdc2a27).

## AHB target transfers

| Packet | Bus shape |
| --- | --- |
| Reads of 4, 8 or 16 beats of at least 32 bits | Matching INCR or WRAP |
| One-beat reads, two-beat WRAP reads, and byte or halfword reads | SINGLE |
| WRAP reads past the mapping window end, and 2 KiB WRAP16 reads | SINGLE |
| Other reads of at least 32 bits, and INCR reads across a boundary or window end | Undefined INCR |
| Multi-beat incrementing writes of at least 32 bits | Undefined INCR, or SINGLE when undefined bursts are disabled |
| Single-beat, byte, halfword and WRAP writes | SINGLE |

Undefined INCR restarts with NONSEQ at each 1 KiB boundary.

Missing data or response space holds BUSY inside a burst. A ready next packet
follows directly. Otherwise the bus becomes IDLE.

With continuous supply and no waits, transfers sustain one per cycle within a
burst. Consecutive single writes have at most two cycles between NONSEQ
transfers. INCR4 reads sustain at least 80% occupancy across packet boundaries.
Network arbitration, capacity, conversion and stalls can reduce these target
adapter rates.

The target adapter can retain two read responses, including issued bus
transfers, and one write word besides HWDATA. A completing write address can
accept its successor word in that cycle, so network ready can follow HREADY.
Other target inputs do not directly drive network outputs.

HSIZE follows the arriving beat size. HADDR aligns to it. Single and FIXED reads
cover only the start address through the size-container end, using
largest-fitting aligned transfers. Their combined response is zero on any
failure. Longer reads access each beat's container without coalescing.

Byte and halfword packets use SINGLE. A 2 KiB WRAP16 at 1024 bits also uses
SINGLE. HPROT and HMASTLOCK stay constant for the packet.

A full-strobe word uses its size container. An exact lower or upper target-word
half uses one halfword-sized transfer. A single or FIXED start block can also
use one aligned power-of-two transfer. Other partial masks issue one byte
transfer per enabled lane, lowest first. Zero strobes make no access.

From the first partial or zero-strobe word, all remaining write transfers are
NONSEQ SINGLE. A 32-bit beat strobed `0x3` on a 64-bit target therefore uses two
byte transfers.

`write_bursts` defaults to `packet`. With no undefined bursts, all writes are
SINGLE. Complete writes issue from the word buffer without waiting for a whole
burst to arrive. Without undefined bursts, each INCR read block is the longest
of 16, 8, 4 or 1 beats fitting the remaining bytes and boundaries.

An INCR or WRAP packet retains bus ownership across its blocks until its first
partial word. A gap between blocks is IDLE, never BUSY after a completed block
or SINGLE.

`hresp_width` defaults to 1. Use 2 for AMBA 2 targets. RETRY and SPLIT fail the
current packet.

For RETRY or SPLIT, the adapter records an abort at the first response edge,
while HREADY is low. In the second response cycle, it drives HTRANS to IDLE and
clears HSEL. The adapter holds HWDATA stable until the data phase ends.

For ERROR, the adapter preserves the next address and lets it complete in the
second response cycle. After RETRY or SPLIT, the adapter drains the packet's
remaining write fragments and returns one error response. It preserves read
responses already offered and returns zero data with errors for the remaining
words of that packet. Other packets keep their results. A cancelled address of
another packet restarts with NONSEQ.

Use two-cycle responses. An `ahb_v3` target has no HMASTER port. HSPLIT and
full RETRY/SPLIT arbitration are unsupported.

ERROR becomes SLVERR. Remaining burst transfers continue and side effects are
not rolled back. The outgoing HPROT low bit is inverted instruction, bit 1
privileged, and bits 3:2 modifiable and bufferable. The shaping and HRESP fields
are rejected on non-AHB targets, including explicit defaults. Wide AHB targets
use the same merging and clamping rules as AXI, with at most 16 words and 1 KiB
per packet.

A FIXED beat wider than the target fails even with `fixed_bursts: single`.

## AHB5 basic properties

Set `protocol: ahb5` on either endpoint. AHB5 follows the transfer, burst,
wait, ordering and two-cycle ERROR rules on this page. Targets require
`attributes: amba` and one-bit HRESP.

| Field in the optional `ahb5` object | Values | Default |
| --- | --- | --- |
| `hprot_width` | 4 or 7 bits | 4 |
| `hnonsec` | Boolean, includes HNONSEC when true | `false` |
| `hmaster_width` | 0 to 32 bits | 0, no HMASTER port |
| `exclusive` | `false` only | `false` |
| `hauser_width`, `hwuser_width`, `hruser_width`, `hbuser_width` | 0 only | 0 |

Other protocols reject the object. Unsupported values and per-bit attribute
maps produce `NCL_AHB5_PROPERTIES`. HEXCL, HEXOKAY and user ports are absent.
HMASTLOCK returns ERROR without target access. Multi-manager arbitration,
HSPLIT and full RETRY/SPLIT are unsupported.

HMASTER is an initiator input and a target output, preserved through packet
splitting, width conversion and clock crossings. A nonzero target width must
hold all reaching initiator values, with zero extension where needed.
Endpoints with width zero omit the port, and sources without HMASTER supply
zero. HMASTER does not select routes or arbitration and does not use AXI IDs.

HNONSEC is an initiator input and a target output. It maps to AxPROT[1],
with 0 for Secure and 1 for Non-secure. AHB sources without an HNONSEC input
supply 1. AXI sources supply AxPROT[1].

AHB5 pairs preserve HPROT. Four-bit sources extend HPROT[4] from HPROT[3]
and set HPROT[6:5] to zero. Four-bit targets use the low four bits.

| Attribute | AHB to AXI4 | AXI4 to AHB5 |
| --- | --- | --- |
| HPROT[0], Data | Invert into AxPROT[2] | Invert AxPROT[2] |
| HPROT[1], Privileged | AxPROT[0] | AxPROT[0] |
| HPROT[2], Bufferable | AxCACHE[0] | AxCACHE[0]. Seven-bit targets use zero for AxCACHE `0011` |
| HPROT[3], Modifiable | AxCACHE[1] | AxCACHE[1] |
| HPROT[4], Lookup | ARCACHE[3], AWCACHE[2] | AxCACHE[2] OR AxCACHE[3] |
| HPROT[5], Allocate | ARCACHE[2], AWCACHE[3] | ARCACHE[2] on reads, AWCACHE[3] on writes |
| HPROT[6], Shareable | No AXI4 port | Zero |

For four-bit AHB, Lookup follows HPROT[3] and Allocate is zero.
Seven-bit HPROT[6:2] permits only the memory types below. Other encodings
must not be driven. Shareable writes wait for target completion and errors.

| Memory type | HPROT[6:2] (decimal) |
| --- | --- |
| Device without early response | 0 |
| Device with early response | 1 |
| Normal non-cacheable | 2, 18 |
| Write-through | 6, 14, 22, 30 |
| Write-back | 7, 15, 23, 31 |

On seven-bit targets, AxCACHE `0011` maps to Normal non-cacheable.
With AxPROT `000`, it gives HPROT `0001001`. Four-bit targets map
AxCACHE[1:0] directly to HPROT[3:2], giving `1101` for that request.
Reads use ARCACHE and ARPROT. Writes use AWCACHE and AWPROT.

The map records the endpoint protocol as `ahb5`. Under
`definition.ahb5_properties`, `initiators` and `targets` list each endpoint's
`hprot_width`, `hnonsec` and `hmaster_width` by name.
IP-XACT uses the AMBA3 AHBLite subset and maps four-bit HPROT on the bus.
Seven-bit HPROT, HNONSEC and HMASTER are plain model ports outside that mapping.

The rules follow [AMBA AHB, IHI 0033C, sections 3.8 to 3.10][ahb5-spec]
and [AMBA AXI, IHI 0022E, table A4-5][axi-memory].

[ahb5-spec]: https://documentation-service.arm.com/static/6141bf0d674a052ae36ca811
[axi-memory]: https://documentation-service.arm.com/static/5f915b62f86e16515cdc3b1c
