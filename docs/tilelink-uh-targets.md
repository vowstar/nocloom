# TL-UH targets

This page covers TileLink TL-UH target fields, packet contexts and message handling.

Set `protocol: tl_uh` on a target. Use the shared
[fields](tilelink.md#endpoint-fields), [size encoding](tilelink.md#tl-uh-size-encoding)
and [port rules](tilelink.md#clock-reset-and-ports).

| Field | Values | Default |
| --- | --- | --- |
| `max_outstanding` | 1 to 16 packets in flight, reads and writes combined | 1 |
| `source_width` | At least ceil(log2(max_outstanding)), at most the shared width limit | ceil(log2(max_outstanding)), zero for one context |

The [target example](../examples/tilelink_uh_target.yaml) connects a 128-bit
AXI4 initiator to a TL-UH target with three packet contexts and a 4096-byte
message limit. Generate it from the repository root:

```sh
build/nocloom generate examples/tilelink_uh_target.yaml --out-dir build/uh_target
```

`max_transfer_bytes` limits one TileLink message. `max_outstanding` limits
accepted network packets. One packet can require several messages, so these
fields control different resources.

| Packet contexts | Minimum and default source width | TileLink source values |
| --- | --- | --- |
| 1 | 0 bits, no source port | Implicit zero |
| 3 | 2 bits | 0 to 2 |
| 16 | 4 bits | 0 to 15 |

An explicit `source_width: 16` widens the port without adding contexts.
The target drives zero in the extra high bits. D responses must return the
complete source value received on A.

Mappings must align their base, size and local address to `max_transfer_bytes`.
A target rejects exclusive monitors. Unaligned mappings and exclusive monitors
produce `NCL_TILELINK_PROFILE`. See the shared
[configuration limits](tilelink.md#configuration-limits).

## Packet contexts

Each packet owns one context until its last network response is accepted.
The context number is its TileLink source.
Each packet has at most one TileLink message in flight. The source remains
reserved between that packet's messages and through network backpressure.

Independent read keys can issue Get messages concurrently. Reads with the same
network entry, flow and AXI ID wait for the preceding packet to retire. Writes
keep their acceptance order across all messages of each packet. Only the
earliest unfinished write packet can send Put messages. A Put owns A through its
final beat. While a write waits for Ack, independent Gets can use A.

The first accepted D beat selects its owner through the final D beat. D
messages from independent sources can complete in any order. Beats from
different D messages must not interleave. Read backpressure can stall data
reception. While no read message owns D, the target can accept an Ack even
when read response storage is full.

With more than one context, read responses from independent packets can
alternate at TileLink message boundaries. All response words of one message
stay together, including gaps between valid words. A write response waits
until that group ends. Write responses keep packet acceptance order.

For example, packet P needs a 4-byte message followed by an 8-byte message.
Packet Q needs one 32-byte message. Q can return between P's messages if
P's second D message is delayed. Width conversion preserves each packet's
ID, byte lanes and final response boundary, including a partly filled word
at a message boundary.

## Messages

The target sends Get, PutFullData and PutPartialData. It sends no Intent or
atomic operations.

The target covers each received byte interval with aligned power-of-two
messages. Each block fits the remaining interval, mapping and maximum size.
Messages use bytes from one network packet. A 4096-byte-aligned 128-bit AXI4
request with 256 full-width beats can form one 4096-byte target message.
A 4096-byte message from a TL-UH initiator first splits into four network
transactions and can form four 1024-byte target messages.

The following intervals assume a 4096-byte mapping boundary and no earlier
packet split:

| Received byte interval | Target message limit | Ordered target messages |
| --- | --- | --- |
| `0x1000..0x1fff` | 4096 | One 4096-byte message, size 12 |
| `0x1000..0x1fff` | 2048 | Two 2048-byte messages, size 11 |
| `0x1004..0x100f` | 4096 | 4 bytes at `0x1004`, then 8 bytes at `0x1008` |

Reads access no bytes outside that interval. Set `read_side_effects: true`
to preserve byte ranges of [eligible non-modifiable AXI reads][read-side-effects]
during packet formation.
An unaligned INCR interval ends at the beat-aligned start plus the original beat
count times the beat size. Packet formation and target messages keep the WRAP
tail before the head.

With `fixed_bursts: single`, every repeated beat still causes its own access.
With read side effects enabled, eligible WRAP runs use exact blocks.

A single-beat write with all bytes of its size selected uses PutFullData.
Other writes use PutPartialData. With packet transfers smaller than 4 bytes,
each word uses separate messages. Packet transfers of 4 bytes can use
multibeat messages.

An independent zero-mask word causes no target access. Zero-mask beats inside
a multibeat Put are sent. `a_param` and `a_corrupt` are zero. Unselected data
bytes are zero.

The target accepts AccessAck before the last A beat, including the first A
handshake cycle. It stores the Ack independently of read data. The target sends
the remaining A beats after an early denied Ack. D ready does not depend on A
ready. The next message starts after both A and D complete.

## Error responses

For a denied read, return denied on every D beat. The target receives all
expected beats and returns zero data and a target error. A read beat with
corrupt set and denied clear fails independently. Width conversion
combines the errors of all network words in one source response beat. A 128-bit
AXI response that includes one failed 32-bit word has SLVERR and zero data for
that whole AXI beat. Other source beats keep their data and status.

Write denied merges into the packet's write response. Response length follows
the saved request, independent of the received D size or opcode. The external
slave must return the response opcode, size, source and beat count required
by its accepted A message.

## Watchdog

When configured, each packet context has a watchdog. It counts target clock
cycles while the slave delays an A beat that the context offers, or a D beat
after the last A beat.

Each A or D handshake of the context restarts the count. Waiting for network
write data, response space or the D message of another context also restarts
it.

Expiry latches the [timeout signal](tilelink.md#errors-and-progress) until
common reset. Pending and later work completes when the slave responds.
The protocol clock gate can close again when no work is pending.

[read-side-effects]: packets.md#read-side-effects
