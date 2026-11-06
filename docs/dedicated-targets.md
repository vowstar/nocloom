# APB and AXI4-Lite targets

## Dedicated APB and AXI4-Lite targets

Dedicated targets are APB4, APB3, APB2, AXI4-Lite and AHB-Lite targets.
AXI4-Lite issues one access per touched target word.

| Write strobes | Target behavior |
| --- | --- |
| All zero at APB or AHB | Complete without bus access |
| Partial at APB4 | Drive the selected byte strobes |
| Partial at APB3 or APB2 | Reject that physical word. Retain the packet error and completed effects. Execute later full words, including those in the same beat |
| Zero or partial at AXI4 or AXI4-Lite | Drive WSTRB with those bits |

APB packet capacity is one per direction.

## APB target transfer details

| `apb_back_to_back` | No-stall physical-word interval |
| --- | --- |
| `false` (default) | Three target cycles, including one idle cycle |
| `true` | Two target cycles, ACCESS directly to SETUP |

Only APB targets accept this option. Gaps, packet boundaries and response
pressure can add idle cycles.

An 8-bit or 16-bit APB target visits enabled physical words of its 32-bit
internal container in ascending byte order. Addresses align to each physical
word. Reads gather only requested bytes into one internal response. Any physical
access failure zeros that whole response. Later accesses still execute.

A default APB target adapter starts another word after the current write
completes or the current read response leaves. It starts the next packet after
the previous packet's final response leaves. Simultaneous read and write heads
take the write first. Back-to-back mode reserves response space before SETUP and
can buffer the following write while the current one runs.

Two read response slots include any issued read. A full queue stops new reads
before SETUP. Packets of opposite directions still obey arrival order.

APB drops CACHE. APB3 and APB2 also drop protection. APB2 completes after one
SETUP and one ACCESS and cannot report a target error. Reset cancels gathers,
bus phases and pending responses. See
[reset coordination](clocks-and-resets.md#reset-release).

## AXI4-Lite target transfer details

AW and W are offered together and accepted independently at a word-aligned local
byte address. PROT and WSTRB pass through, including all-zero writes. The target
adapter waits for every issued word's B before returning one packet B. Words
issue back to back without waiting for earlier B or R.

The next write word enters when AW and W storage is free or both current
handshakes complete. Its network ready can depend combinationally on AWREADY and
WREADY.

A response passes to the network in its arrival cycle if the response slot is
empty. A stalled response occupies that slot and backpressures the next R or
packet-final B. No bus access occurs for window misses, refused shapes, failed
exclusives or post-timeout work. INCR writes combine consecutive beats within
each target word, including full-width beats of a narrower initiator. Packet
boundaries end the combination. An all-zero word still issues one write with
WSTRB zero.

## FIXED bursts at dedicated targets

| `fixed_bursts` | Behavior |
| --- | --- |
| `reject` (default) | Consume the burst, return errors without target access |
| `single` | One repeated-address access per beat, one R per initiator beat or one combined B |

AXI targets receive supported FIXED bursts directly and reject this option.

## Dedicated FIXED limits

The window decode precedes FIXED refusal. Unmapped accesses return decode errors
without a transfer. With `single`, an APB beat visits enabled physical words,
AXI4-Lite uses an aligned word and AHB uses strobe-shaped SINGLE transfers. A
target error does not stop later FIXED beats.

A beat wider than the target engine fails without access. The APB engine word is
32 bits. FIXED is never packetized, and dedicated targets accept at most 16
beats within their packet limit.
