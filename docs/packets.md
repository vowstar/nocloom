# Packets

## Bounded packets

Packet limits depend on both endpoints and the target window.
`transport.packet_limits` can narrow them for selected connected AXI pairs. Each
entry has `source`, `target`, and at least one of `max_bytes` or
`boundary_bytes`. Values must be powers of two from the pair's alignment word
through its default. A pair can appear only once.

| Target | Default packet bytes | Boundary |
| --- | --- | --- |
| AXI4, AXI3 | Smaller of its AxLEN capacity and 4 KiB | At most 4 KiB and local address space |
| AHB | Smaller of 16 target words and 1 KiB | At most 1 KiB and local address space |
| APB, AXI4-Lite | 128 | Local address space |

Window alignment can further reduce the boundary. The alignment word is the
larger endpoint word, with APB targets counting as 4 bytes. An initiator cannot
reach a window smaller than that alignment word.

A packet keeps its selected target and route. A burst can cross mapping
boundaries and send later packets to another target. Before a packet changes
target, it waits for all earlier packets of that burst to complete. At most four
packets per direction of a split burst can await responses. For the same target,
later packets need not wait for earlier responses when capacity permits.

A write still returns one B, and a read retains its original beat count and last
beat.

WRAP packets follow the initiator and target wrap limits. A WRAP that needs
decomposition becomes INCR runs in wrap address order. A narrow WRAP read folds
only when its container fits one physical target word and one initiator word. A
narrow WRAP write folds when its container fits one initiator word. An exclusive
or locked burst is not cut into packets. If its required shape exceeds the
supported packet or window boundary, it returns an error without target access.

Narrow INCR transfers normally pack into the narrower endpoint word.
`narrow_bursts: split_non_modifiable` sends each narrow non-modifiable INCR beat
as a separate packet when AxLOCK is clear. This option changes non-modifiable
transaction shapes beyond AXI A4.3 permissions. Use it only with targets that
accept those transformations.

## Packet shape rules

A packet ends at its alignment-word start plus the byte limit, or at the next
target-window boundary, whichever comes first. The boundary cannot exceed the
largest power of two dividing both window base and size. Packet overrides
require a connected full AXI initiator, valid values and at least one specified
limit. Each limit can equal or be less than the pair's default. No option disables
packet splitting.

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

Read side effects default to `false`. Eligible exact blocks are limited by the
pair's byte limit and boundary. Only an unaligned first beat can need blocks
smaller than its beat size. Writes repeat that first beat with block-specific
strobes. Reads merge its block results into one initiator beat.

Decode misses do not use exact blocks. An AHB target can read a block narrower
than its word with that HSIZE. AXI shapers reject any connected target with read
side effects.

## Read side effects

Set `read_side_effects: true` on the target. This configuration keeps eligible
non-modifiable INCR accesses within their requested byte range. The initiator
decomposes the request into naturally aligned power-of-two blocks. This rule
applies when AxCACHE's modifiable bit and AxLOCK are clear. The first address
selects the rule for the whole burst.

WRAP, FIXED, modifiable and exclusive bursts keep their other packet rules. APB
and AXI4-Lite initiators remain single aligned transfers. A physical target that
only supports whole-word reads still reads that whole word. This option cannot
combine with `split_non_modifiable` on a reaching initiator.
