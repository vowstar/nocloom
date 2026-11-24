# Read response interleaving

## Interleaved read data

Set these options to describe where the AXI4 target changes read response IDs.

| Value | Target promise |
| --- | --- |
| `read_interleaving_disabled: true` | Complete each read before another ID's read data |
| `read_interleaving_disabled: false` | IDs can alternate after every word |
| `read_block_words: 2`, `4` or `8` | Keep that many target words together before changing ID |

A read starting inside a block is split at the block boundary. The default is
one target word when the interface can interleave. Otherwise each read runs to
completion. The network restores each initiator's response order after target
interleaving.

Explicit read interleaving options have these requirements.

| Scope | Requirement |
| --- | --- |
| Network | No `topology`, clock gates, one clock domain and no `transport` configuration |
| Endpoints | One or two AXI4 initiators and one or two AXI4 targets, each with 32 or 64 data bits |
| Endpoint fields | `len_width: 8`, at most four ID bits and capacity at most four |
| Targets | Equal data widths and access modes. All omit `max_outstanding`, or all set it to the same value. Each has one flow and one mapping |
| Target exclusions | No exclusives, timeouts, pipes, user fields or read side effects |
| Initiators | Equal data widths. All omit `max_outstanding`, or all set it to the same value. Every initiator reaches every target |
| Initiator exclusions | No access, narrow-burst, ID-table, default-target, user, scheduling, flow or pipe configuration |
| Block size | `read_block_words` is 1, 2, 4 or 8, up to 32 bytes. With `read_interleaving_disabled: true`, it can only be 1 |
| Larger blocks | A block larger than one target word must contain a whole initiator word |
| Access | A read-only target without an ID port allows only `read_interleaving_disabled: true`. Write-only targets reject both fields |

## Read response segments

AXI4 and AXI3 accept interleaved IDs by default. Wider initiators gather each
pending read separately. Reads sharing a target ID cannot interleave. See
[saved WRAP words](width-conversion.md#width-transformation-details).

On a shared response path, a waiting B can precede a new R packet or a
change of read. On separate paths, B uses its own response storage.

A response word leaves when the next word is shown, it is the packet tail, or
one cycle after no successor was found. An R gap can therefore delay a word by
one cycle. A segment ends before a different read or after two cycles without
a next word. On shared paths a segment can also end before a B. Every switch
releases its grant at that segment end. The next segment competes again.

The initiator ends a read only on its packet-final word, not on a segment
boundary.

With `read_interleaving_disabled: true`, a shared response join holds each read
through completion. With a larger block, words inside the block do not wait for
successors. On shared paths B waits for the block boundary. An unaligned read
splits at the first block boundary. A WRAP larger than the block splits if its
start lies inside one.

RID matching still applies. The network does not check whether the target honors
this promise.
