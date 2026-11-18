# TileLink TL-UL

This page covers TL-UL message handling, errors and mappings to other protocols.

Set `protocol: tl_ul` on an initiator or target and use the shared
[fields](tilelink.md#endpoint-fields) and
[port rules](tilelink.md#clock-reset-and-ports).

| Field | TL-UL values |
| --- | --- |
| `max_transfer_bytes` | 4 bytes, the default |
| `size_width` | Default 2. Any allowed width still limits legal size values to 0, 1 and 2 for 1, 2 and 4 bytes |
| Target `max_outstanding` | One message in flight, the default |
| Target `source_width` | Defaults to zero because capacity is one. Each bit of a wider source port is zero |

```yaml
initiators:
  - {name: cpu, clock_domain: main, protocol: tl_ul, data_width: 32,
     source_width: 4, max_outstanding: 4}
targets:
  - {name: regs, clock_domain: main, protocol: tl_ul, data_width: 32,
     mappings: [{base: '0x0', size: '0x1000'}]}
```

## Initiators

An external master sends A requests and accepts D responses. The initiator
captures each request before it enters the network. An unaccepted request can
be withdrawn or changed (section 4.1).

`a_source` maps to the network transaction ID and returns as `d_source`.
Requests with different sources can be in flight together and complete in
any order. The master must wait until after the D handshake edge to reuse
its source (sections 6.4 and 6.5).

Network read and write responses share D. Each accepted network response
gives priority to the other direction. A waiting write response can be
preceded by at most one read response. A lone response uses the next free D
slot. D backpressure can extend the wait.

| Request field | Behavior | TileLink sections |
| --- | --- | --- |
| Get | Size and naturally aligned address select the read bytes. The initiator ignores the mask and returns one AccessAckData | 4.5, 7.2.1, 7.2.5 |
| PutFullData | One write beat with its size mask and one AccessAck | 4.5, 7.2.2, 7.2.4 |
| PutPartialData | One write beat with any subset of the size mask and one AccessAck | 4.5, 7.2.3, 7.2.4 |
| Data and mask lanes | Little-endian byte order. Each mask bit selects one byte in the active lanes | 4.5 |
| Get and Put A param | The initiator ignores reserved values | 7.2 |

Use natural alignment. The initiator clears the low address bits according
to the size before sending an accepted request to the network.

Only Get and Put opcodes are valid on TL-UL. The initiator rejects all other
opcodes regardless of param. It also rejects sizes above 2 and writes with
`a_corrupt` set. A rejected message makes no target access. The initiator
consumes its A beats, then returns the original size and source. D param is
zero on every response.

| Rejected request | Response | TileLink sections |
| --- | --- | --- |
| Get, ArithmeticData or LogicalData | Denied AccessAckData with zero data and corrupt set on every beat | 7.2.1, 8.2.1, 8.2.2 |
| PutFullData or PutPartialData | Denied AccessAck | 4.4, 7.2.2, 7.2.3 |
| Intent, for param 0 through 7 | Denied HintAck | 8.2.3, 8.2.4 |
| Opcode 6 or 7 | One denied AccessAck outside the TL-C protocol | |

## Targets

The target sends A requests and accepts D responses. It reuses source zero
after the response handshake. The external slave must support Get,
PutFullData and PutPartialData for sizes 0 through 2 at every mapped address.

A network packet becomes naturally aligned single-beat messages. A read
interval becomes aligned blocks.

A full target word write uses one
PutFullData. A partial word can use several smaller PutPartialData messages
in increasing byte order. A zero-strobe write makes no target access.
An AXI packet with several TileLink writes combines their results.
By default, the target rejects a FIXED burst and returns an error without
accessing the external slave. Set `fixed_bursts: single` to allow the burst.

Packet formation can merge narrow AXI reads into a larger interval. See
[read side effects](packets.md#read-side-effects) to keep eligible
non-modifiable reads within their original byte range.

`a_param` and `a_corrupt` are zero. Get data and unselected write data bytes
are zero. Protection, cache, QoS and user information have no TileLink ports.
The target can accept a D response on the A handshake cycle. D ready depends
on local response storage.

## Error responses

The error mapping follows section 4.4.

| Event at a TL-UL initiator | D denied | D corrupt | Read data |
| --- | --- | --- | --- |
| Local rejection of a data-returning request | 1 | 1 on every beat | Zero |
| Network read error, including decode miss or timeout completion | 0 | 1 | Zero |
| Any write error | 1 | 0 | Not used |
| Successful read or write | 0 | 0 | Read result |

On a TL-UL initiator, D corrupt is zero on every AccessAck, including replies
to reserved opcodes 6 and 7.

Read denied is sent only when the port has accessed no target. Write denied
means that the write failed. Part of a write can already have reached a
target. Completed accesses and their side effects remain.

At a TL-UL target, denied or corrupt read data becomes a target error with
zero data. A denied write becomes a target error. An AXI initiator receives
SLVERR for these errors with either value of `slverr_only`.

See the shared [progress and timeout rules](tilelink.md#errors-and-progress).

## Mapping to other endpoints

From a TL-UL initiator to another protocol's target:

| TileLink request or result | AXI4 | APB | AHB-Lite |
| --- | --- | --- | --- |
| Get | One beat with AxSIZE equal to A size before width conversion | A physical-word read | One or more aligned reads |
| PutFullData, PutPartialData | One W beat with WSTRB from A mask before width conversion | PSTRB on APB4. APB3 and APB2 reject partial physical words | Writes shaped to active bytes |
| Source | Target IDs are assigned separately from the network ID | No bus ID | No bus ID |
| Target read error | RRESP error gives corrupt AccessAckData | PSLVERR gives corrupt AccessAckData | ERROR gives corrupt AccessAckData |
| Target write error | BRESP error gives denied AccessAck | PSLVERR gives denied AccessAck | ERROR gives denied AccessAck |

Requests supply PROT `010`, CACHE `0000` and LOCK zero, with no user or QoS
information. With `attributes: amba`, the values feed the corresponding AXI,
APB and AHB outputs. Other attribute mappings can change them.

From another protocol's initiator to a TL-UL target:

| Incoming behavior | TL-UL target behavior |
| --- | --- |
| AXI read or write burst | Uses the [packet conversion rules](#targets) for each packet |
| AXI WSTRB or APB4 PSTRB | Selects the write bytes for packet conversion |
| AHB-Lite transfer | Aligned Get or Put messages for its bytes |
| Denied or corrupt read response | Source sees the [target read error](#error-responses) |
| Denied write response | Source sees the [target write error](#error-responses) |
| ID and attribute information | See the [TL-UL target port rules](#targets) |

A byte or halfword Get through an APB target still accesses its physical word.
The external device must permit that read granularity. See
[width conversion](width-conversion.md) for mixed endpoint widths and
[AHB transfers](ahb.md#ahb-target-transfers) for partial-write shaping.
