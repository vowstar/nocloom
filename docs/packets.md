# Packets

## Bounded packets

Packet limits depend on both endpoints and the target window.
`transport.packet_limits` can narrow them for selected connected AXI pairs. Each
entry needs an AXI4 or AXI3 initiator and has `source`, `target`, and at least
one of `max_bytes` or `boundary_bytes`. Values must be powers of two from the
pair's alignment word through its default. A pair can appear only once.

The table gives each target's baseline limits. Field names in the table refer
to that target.

| Target | Baseline packet bytes | Boundary |
| --- | --- | --- |
| AXI4, AXI3 | `min(2^len_width * data_width / 8, 4096)` | At most 4 KiB and local address space |
| AHB, TL-UL | Smaller of 16 target words and 1 KiB | At most 1 KiB and local address space |
| APB, AXI4-Lite, Wishbone | 128 | Local address space |
| TL-UH | `max_transfer_bytes` | Local address space |

Window alignment can further reduce the boundary. The alignment word is the
larger endpoint word, with APB targets counting as 4 bytes. An initiator cannot
reach a window smaller than that alignment word.

For each pair, the effective default packet byte limit is the larger of the
target's baseline limit and the alignment word. A TL-UH target's
`max_transfer_bytes` still limits each TileLink message. For example, a 128-bit
AXI4 initiator with a 4-byte TL-UH target maximum has a 16-byte packet limit.
The target can split that packet into four 4-byte messages.

A packet keeps its selected target and route. A burst can cross mapping
boundaries and send later packets to another target. Before a packet changes
target, it waits for all earlier packets of that burst to complete. For the same
target, later packets need not wait for earlier responses when capacity and the
[split-packet limit](#split-packet-limit) permit.

A write still returns one B, and a read retains its original beat count and last
beat.

WRAP packets follow the initiator and target wrap limits. A WRAP that needs
decomposition becomes INCR runs in wrap address order. A narrow WRAP read folds
only when its container fits one physical target word and one initiator word. A
narrow WRAP write folds when its container fits one initiator word. Paths to
[TL-UH targets](tilelink.md#tl-uh-targets) keep separate tail and head runs.

An exclusive or locked burst stays in one packet. A shape beyond its packet
or window boundary returns an error without target access.

Narrow INCR transfers normally pack into the narrower endpoint word.
`narrow_bursts: merge_modifiable` sends each narrow non-modifiable INCR beat
as a separate packet when AxLOCK is clear. Modifiable bursts can still pack into
words. This option changes non-modifiable
transaction shapes beyond AXI A4.3 permissions. Use it only with targets that
accept those transformations.

## Split-packet limit

Each initiator that sends bursts can have four counted packets in flight for
reads and four for writes. The count is fixed. `transport.packet_limits` and
`max_outstanding` do not change it.

A counted burst is a burst that the initiator splits into several packets,
folds, or packs from several beats. Its packets are counted packets. A packet is
in flight from the time its request leaves until its response completes. All
counted bursts of one initiator share the count, with any IDs and targets. A
burst that leaves unchanged as one packet is not counted. The packets that
`merge_modifiable` makes are not counted, although that option also splits
bursts.

While four counted packets of one direction are in flight, later packets of that
direction wait until one of the four completes:

| Direction | Waits | Does not wait |
| --- | --- | --- |
| Read | Every later read packet, counted or not, to any target | Packets that `merge_modifiable` makes, unless an earlier read of this initiator waits |
| Write | Every later packet of a counted burst | A write that is not counted, unless an earlier write of this initiator waits |

Other initiators do not wait. The other direction of the same initiator waits in
one case. A burst that has sent a packet and waits before a later packet keeps
the initiator request path until its last packet leaves.

Data, responses and per-ID order do not change. If a target never answers a
counted packet, the wait does not end. A
[target timeout](timeout-and-progress.md#target-timeouts) with
`completion: true` answers that packet and ends the wait.

A separate limit counts bursts. For each direction, an initiator can have four
unfinished bursts that are counted or that `merge_modifiable` splits. A
fifth such burst waits until one completes.

## Packet shape rules

A packet ends at its alignment-word start plus the byte limit, or at the next
target-window boundary, whichever comes first. The boundary cannot exceed the
largest power of two dividing both window base and size. Packet overrides
require a connected full AXI initiator, valid values and at least one specified
limit. Each limit can equal or be less than the pair's default. No option
disables packet splitting.

An initiator WRAP limit is the larger of two beats and the smaller of 64 bytes
and `min(16, 2^len_width)` beats. A zero length width permits no WRAP. The
target WRAP bound covers the largest WRAP container of its reaching initiators.
Its packet limit and 16 AXI target words also bound it. The largest fitting
power of two above one target word is supported. Otherwise WRAP is decomposed.

Each pair then applies its own packet limit and the source's WRAP bound.

If any reached target requires WRAP decomposition, the initiator enables that
handling for all targets. A WRAP starting at its container base becomes INCR. An
oversized unaligned container becomes two INCR runs, then ordinary packets.

A folded narrow WRAP writes by byte strobe. A folded read repeats its single
target word's data and result for the initiator beats it contains. Other reads
gather only the target words that contribute to each initiator beat. A failed
target word affects only the initiator beats that use it.

An AxLOCK WRAP always becomes INCR from its start. An exclusive or locked burst
requiring a cut returns SLVERR without target access. FIXED and unmapped
sub-page tail portions remain single packets.

Narrow packing keeps the raw address, counts covered words and chooses full
packing-word size, or the smallest size covering one word's requested bytes. The
packing word is the smaller endpoint word. APB reads use the physical APB word.

Narrow AHB INCR reads pack only from an aligned packing-word address. Other AHB
INCR reads keep separate initiator beats. Zero strobes are retained in the
packed word. Returned words answer all initiator beats they contain, with data
outside requested bytes unspecified. When a packed INCR read word answers
several beats, its bytes beyond the burst end and below an unaligned start are
zero. See [width conversion](width-conversion.md#width-transformation-details).

## Read side effects

`read_side_effects` defaults to `false`. Set it to `true` on the target.

The initiator then keeps eligible non-modifiable INCR accesses within their
requested byte range. It decomposes each request into naturally aligned
power-of-two blocks. This rule applies when AxCACHE's modifiable bit and AxLOCK
are clear. The first address selects the rule for the whole burst.

Eligible blocks are limited by the pair's byte limit and boundary. Only an
unaligned first beat can need blocks smaller than its beat size. Writes repeat
that first beat with block-specific strobes. Reads merge its block results
into one initiator beat.

Decode misses do not use exact blocks. An AHB target can read a block narrower
than its word with that HSIZE. AXI shapers reject any connected target with read
side effects.

For TL-UH targets, eligible WRAP tail and head runs also use exact blocks.
FIXED, modifiable and exclusive bursts keep their packet rules. WRAP uses
the selected target's packet policy. APB and AXI4-Lite initiators remain
single aligned transfers. A physical target that only supports whole-word
reads still reads that whole word. This option cannot combine with
`merge_modifiable` on a reaching initiator.
