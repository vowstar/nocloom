# Read response interleaving

## Interleaved read data

An AXI4 target can set `read_interleave_bytes`.

| Value | Target promise |
| --- | --- |
| `0` | Complete each read before another ID's read data |
| One target word | IDs can alternate after every word |
| Several target words, at most 32 bytes | Keep that block together before changing ID |

A read starting inside a block is split at the block boundary. The default is
one target word, or 0 when the endpoint cannot interleave. Unsupported block
shapes are rejected. The network restores each initiator's response order after
target interleaving.

Explicit `read_interleave_bytes` requires:

| Scope | Requirement |
| --- | --- |
| Network | No `topology`, clock gates, one clock domain and no `transport` configuration |
| Endpoints | One or two AXI4 initiators and one or two AXI4 targets, each with 32 or 64 data bits |
| Endpoint fields | `len_width: 8`, at most four ID bits and capacity at most four |
| Targets | Equal data widths and access modes. All omit `max_outstanding`, or all set it to the same value. Each has one flow and one mapping |
| Target exclusions | No exclusives, timeouts, pipes, user fields or read side effects |
| Initiators | Equal data widths. All omit `max_outstanding`, or all set it to the same value. Every initiator reaches every target |
| Initiator exclusions | No access, narrow-burst, ID-table, default-target, user, scheduling, flow or pipe configuration |
| Block size | Zero or a power of two from one target word through 32 bytes |
| Larger blocks | A block larger than one target word must contain a whole initiator word |
| Access | A read-only target without an ID port accepts only zero. Write-only targets reject the field |

## Read response segments

AXI4 and AXI3 accept interleaved IDs by default. Wider initiators gather each
pending read separately. Reads sharing a target ID cannot interleave. See
[saved WRAP words](width-conversion.md#width-transformation-details).

A waiting B can precede a new R packet or a change of read.

A response word leaves when the next word is shown, it is the packet tail, or
one cycle after no successor was found. An R gap can therefore delay a word by
one cycle. A segment ends before a B, a different read, or after two cycles
without a next word. Every switch releases its grant at that segment end. The
next segment competes again.

The initiator ends a read only on its packet-final word, not on a segment
boundary.

With `read_interleave_bytes: 0`, the response join holds each read through
completion. With a larger block, words inside the block do not wait for
successors, and B waits for the block boundary. An unaligned read splits at the
first block boundary. A WRAP larger than the block splits if its start lies
inside one.

RID matching still applies. The network does not check whether the target honors
this promise.
