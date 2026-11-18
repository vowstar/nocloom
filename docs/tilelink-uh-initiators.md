# TL-UH initiators

This page covers TileLink TL-UH initiator fields, message admission and completion.

Set `protocol: tl_uh` on an initiator. Use the shared
[fields](tilelink.md#endpoint-fields), [size encoding](tilelink.md#tl-uh-size-encoding)
and [port rules](tilelink.md#clock-reset-and-ports).
The initiator denies atomic operations, so it does not conform to TL-UH.

The effective `address_width` must be at least `log2(max_transfer_bytes)`.
For example, an 8-bit address port permits a maximum of 256 bytes. A smaller
address width within the allowed range produces `NCL_TILELINK_PROFILE`
before generation.

```yaml
initiators:
  - {name: dma, clock_domain: main, protocol: tl_uh, data_width: 32,
     max_transfer_bytes: 256, size_width: 4, max_outstanding: 4}
```

## Messages

Get has one A beat and `max(1, bytes / 4)` D beats. Put has that many A
beats and one AccessAck. Keep A control fields constant across a Put.
Data, mask and corrupt are sampled only on each handshake. An unaccepted
beat can be withdrawn or changed.

Each corrupt Put beat sends zero write strobes and makes the final AccessAck
denied. The port does not roll back the other writes.

For byte and halfword Puts, only mask bits within the addressed bytes
become write strobes. Other mask bits are ignored.

Get, PutFullData and PutPartialData use naturally aligned power-of-two
messages (sections 7.2.1 to 7.2.3 and 8.3). PutPartialData accepts a separate
byte mask on every beat, including zero (section 4.5).

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

## Local responses

| Local request | Response | Network access |
| --- | --- | --- |
| Intent param 0 or 1 with supported size | One successful HintAck | None |
| Intent with another param or unsupported size | One denied HintAck | None |
| ArithmeticData or LogicalData, all params | D starts only after the last A beat handshake. All size-dependent AccessAckData beats are denied and corrupt with zero data | None |
| Get or Put larger than `max_transfer_bytes`, or not aligned to its size | Denied response after all A beats. Get returns all size-dependent beats with corrupt set and zero data | None |
| Opcode 6 or 7 | One denied AccessAck | None |

The Intent and HintAck operations are in sections 8.2.3 and 8.2.4.
ArithmeticData and LogicalData are in sections 8.2.1 and 8.2.2.

AccessAck and HintAck always clear corrupt. For a network read error, the
failed beats have zero data and corrupt set. Denied stays clear (section 4.4).

## Ordering and capacity

Each response message owns D until its final handshake, including gaps in
network data. Sections 7.2.4 and 7.2.5 describe AccessAck and AccessAckData.

Write responses enter independent completion storage even when D is stalled.
A message slot and its saved source remain allocated until the final D
handshake (sections 6.4 and 8.3). Different sources can complete out of order.

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
