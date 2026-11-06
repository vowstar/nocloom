# Interface timing

These timing rules assume supported AXI profiles without additional optional
pipes or stalls. Endpoint capacity, width conversion and response arbitration
can delay a transfer. See [protocols](protocols.md) and
[capacity](transactions.md).

## Initiator requests

An idle AXI4, AXI4-Lite or APB initiator offers its request to the network
during the address handshake cycle. If first W data is available, it travels
with the write head in that cycle. AWREADY and ARREADY depend on local registers
rather than combinational network ready.

AXI4 W data leaves its engine in the cycle it is accepted, so engine WREADY
follows network ready. The default backward W port pipe registers external
WREADY. AXI4-Lite and APB retain one W word until AW and derive ready from their
own storage. Under these conditions, an AXI4 target sees AW, AR and W one cycle
after the initiator handshake.

Single-packet bursts can issue back to back with sufficient
[capacity](transactions.md#capacity-and-ordering-boundaries). See
[split-packet limits](packets.md#bounded-packets).

## AXI target requests

FIXED, INCR and WRAP use the same request slots. Burst type adds no waiting
cycle. AW and W can handshake in either order.

A target W entry can accept a word while empty or while its previous word
leaves. Network W ready can therefore follow WREADY in that cycle. A backward
request pipe adds storage and registers that ready path. At any supported data
width, the target can move one W and one R beat per cycle when neither side
stalls.

A multi-burst target can accept the next write while a previous AW still waits
for AWREADY. Continuously supplied single-beat writes can reach the target once
per cycle. Its completed write responses can also move once per cycle when
response storage and the network are ready.

## AXI target responses

With several bursts, R and B have separate input stages followed by shared
response storage. RREADY and BREADY have no combinational dependency on network
ready. A word becomes valid when a successor is visible, it is final, or one
cycle after no successor was found. RVALID and BVALID can affect network
response valid in that cycle.

A read-only target without per-target merging uses a two-entry registered-ready
R stage. Behind merging with several bursts, one entry holds its R word and
permits replacement when the merge takes it. The registered data prevents target
RVALID from directly reaching network valid. A write-only target can pass B in
its BREADY cycle while its response storage is empty. It therefore completes a
write one cycle earlier than the corresponding read/write target join.

## Width and packet stages

Per-target merging, initiator width conversion and packet splitting sustain one
word per cycle on their narrower side. A wide initiator beat emits one target
word per cycle. Narrow initiator beats fill one target word per cycle. The merge
accepts a completing W beat when the target W entry accepts that completed word.

Its ready follows the target adapter for that beat. Other partial-word beats can
enter immediately.

Initiator conversion and packet splitting do not retain an accepted W beat after
its final output portion leaves. Their W ready can follow downstream ready. A
gathered R beat leaves with its final contributing word and can hold that word
for RREADY. Requests and words are offered as soon as their input fields are
available, without waiting for downstream ready. These stages add no idle cycle
while all required data and capacity are available.

A target merge retains a read descriptor until its final upstream initiator beat
leaves, even if its last target word already arrived. A saved WRAP start word
returns after that read's last target word when its low lanes are still needed.
An unaligned same-width narrow WRAP replay can hold RREADY low for one cycle.

## Shared response words

A widening response link gathers a packet's target words into one wider
transfer. The first initiator beat can wait until the end of that word is
available. Words flush when full, at packet, segment and wrap boundaries, and
immediately for FIXED or subword beats.

The words of one transfer share one response code. A word with a different code
starts a new transfer after the words gathered before it leave. An error
therefore affects only the initiator beats that use the failed target word. A
response fragment split by a narrower link can gather again into its original
word.

A narrower initiator consumes one initiator word per cycle. A wider initiator
gathers separate pending reads by response tag.

A narrow INCR read can return one gathered initiator word across several
initiator beats. Those beats use the whole gathered word, with only their
requested lanes defined. Different response IDs can interleave between completed
words or split packets, never within the same-ID ordering stream.
