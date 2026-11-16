# Address map

Each target declares one or more global address windows. A mapping contains
`base`, `size`, optional `local_address`, optional `when` and optional `flow`.
`local_address` and `flow` default to 0.

## Target mappings

The window includes addresses greater than or equal to `base` and less than
`base + size`. The target byte address is
`local_address + source_address - base`. The local interval must fit the
target's address width.

| Window kind | Rules |
| --- | --- |
| Whole pages | Base and local address are 4 KiB aligned. Size is a multiple of 4 KiB |
| Sub-page | Size is a power of two from 4 through 2048 bytes. Base and local address align to size. AXI4, AXI4-Lite and AHB mappings must contain at least one target data word |
| Conditional | Whole pages only |
| Alias | Another mapping can reach overlapping local target addresses |

Each mapping must fit the global address space without addition overflow.
There is no separate mapping count limit. YAML input still has the
[size and node limits](definition.md#input-rules).

Initiator `address_width` limits global addresses. Target `address_width` limits
local addresses. Every connected target flow needs a mapping base within the
initiator limit. Address tables clip windows to that limit. A routing window is
the part of one mapping that one initiator decodes to its target.

An address in a sub-page tail hole returns an error without accessing a
neighboring window.

Within one 4 KiB page, each [flow](network.md#endpoint-flows) of an initiator
that reaches a sub-page routing window must reach all such windows of the
initiator. The flow need not reach windows of the initiator's
[default target](#decode-misses). This rule applies to AXI4 and AXI3
initiators with `max_outstanding` above 1.

## Address selectors

`address_inputs` declares input ports with `name`, `width` and `clock_domain`.
Each width is 1 to 32 bits. The declared domain samples the input. Every
initiator whose decode uses that input must be in that domain.

`address_selectors` declares named conditions with `name` and `equals`. `equals`
maps input names to unsigned values that fit their widths. At least one input
comparison is required. Unlisted inputs do not affect that selector.
The network supports 32 selectors because the decoder uses one bit per
selector in a 32-bit mask.

A mapping's `when` maps selector names to Boolean results. Every named selector
must have that result for the mapping to decode. Omit `when` to accept any
selection. Each input serves a selector and each selector serves a mapping.

The decoder orders predicates by input position, starting with the last input
declaration, then by comparison value. Equal predicates use selector declaration
order, which can change the widths of mapped target addresses.
IP-XACT output writes each supported mapping condition as a remap state.

With more than 16 selectors in the network, the map reports whether each
initiator can have an unmapped address under some selector assignment.
If the search reaches its planning budget, it records a possible hole.
See [map fields](map-fields.md) for how to read the result.

Keep address inputs stable during affected traffic. See
[selection timing](#address-selection-timing). See
[mode_remap.yaml](../examples/mode_remap.yaml).

## Decode misses

A miss reaches no data target. Each initiator can set `default_target` to a
connected target's name. That target's response path completes the initiator's
decode misses without a target bus access.

Without an explicit selection, NocLoom selects the target of the smallest
reached [aligned block](#overlap-and-packet-boundaries). A block counts only
when it starts inside the initiator's address space and belongs to a mapping of
a target flow that the initiator reaches. Equal block sizes select the lowest
global base, then the target name in lexical order. Target access attributes and
burst boundaries do not affect this choice.

This choice affects response timing and timeout behavior. A default target that
timed out with `completion: false` does not answer later misses. See
[target timeouts](timeout-and-progress.md#target-timeouts).

## Error responses

Each AXI4, AXI3 or AXI4-Lite initiator uses the responses below by default.
Set `slverr_only: true` on an AXI initiator to return SLVERR for every failure,
including local decode misses and DECERR from a target. OKAY and EXOKAY keep
their protocol meanings. A failed exclusive write that completes with OKAY
still returns OKAY.

| Failure | Default AXI response |
| --- | --- |
| Address decode or connectivity miss | DECERR |
| Unmapped part of a decoded page | DECERR |
| Target SLVERR, APB PSLVERR, AHB ERROR | SLVERR |
| Refused transfer or timeout with completion | SLVERR |
| Target DECERR on a read | DECERR on each initiator beat supplied by that target read |
| Target DECERR on a write | DECERR write response |
| AXI4-Lite write with several target words | Response code of the first failed word |

A failed read beat returns zero data. If any target word of a merged initiator
beat fails, that whole initiator beat returns zero.

Each initiator read beat merges only the target sub-reads that supply its bytes.
The priority is DECERR, then SLVERR, then OKAY.

An exclusive beat returns EXOKAY only when every contributing sub-read reports
exclusive success. A target EXOKAY on a non-exclusive read is a protocol error
and returns SLVERR. A failed target word affects only the initiator beats that
use it.

APB initiators report every failure as PSLVERR. AHB initiators report supported
failures as the two-cycle ERROR response. See [protocols](protocols.md) for
bufferable AHB writes. Wishbone uses ERR and TileLink uses denied or corrupt.
Internal responses distinguish decode and target failures by default, even
when every AXI initiator reports failures as SLVERR. A network can use compact
responses only if every AXI initiator sets `slverr_only: true` and no log
records failure classes.

In networks with AXI initiators that all report failures as SLVERR, the
compact format tends to use fewer logic cells. Set
`transport.compact_failure_responses: true` to choose the compact format.

## Overlap and packet boundaries

The decoder splits mappings into aligned power-of-two blocks. Smaller
overlapping blocks decode first. Equal overlapping blocks are rejected with
`NCL_ADDRESS_OVERLAP`. A containing target cannot hide an unreachable smaller
target when another initiator reaches both. The remainder of an AXI or AHB
mapping must contain whole target words.

INCR packets in a 4 KiB page that holds sub-page mappings cut at every reached
mapping edge. A containing mapping also cuts at directly contained mapping
bases, except its own base or another mapping's end. A WRAP container crossing
an edge becomes INCR pieces. Other WRAP containers stay whole unless other
packet limits require cuts. Before changing target, a packet waits for earlier
packets of its burst to answer.

Earlier mapped writes stay committed if later packets miss. Reads return mapped
data followed by error beats. Other bursts can continue during that wait.

Global addresses are zero-extended from the initiator width. Local target
translation preserves high address bits through the supported 48-bit range.
Mappings can end exactly at the address-space limit.

## Default-target behavior

An explicit `default_target` must name a connected target. A miss moves its
address into that target's first routing window and makes no bus access. Its
read returns the declared number of zero-data beats. Its write consumes all W
beats before returning B.

The smallest reachable mapping whose base fits the initiator's address width
selects the target flow, with ties resolved by global base and then condition.
Compare conditions by sorted selector and value pairs, using name order,
`false` before `true`, and the shorter list first if one is a prefix.
On that flow, order mappings with routing windows by condition and then
internal base, and use the first routing window of the first mapping.

Malformed burst shapes return SLVERR before decode. Width-conversion refusal
follows decode, so an unmapped request still returns a decode error.

An automatic default uses the smallest reached aligned block below the initiator
address limit. A named default instead derives miss cuts from the whole address
space. Its boundary is the smaller of protocol boundary and natural alignment of
unconditional reached mapping bases. Zero bases and bases at another mapping's
end do not narrow it. Conditional mappings do not narrow it.

The protocol boundary is 4 KiB for AXI, 1 KiB for AHB, or the local address
space if smaller. APB and AXI4-Lite use the local address space.

A named default's miss packet limit is at most 256 target words, its boundary
and the initiator's longest burst. The longest burst is capped at 4 KiB, or 1
KiB for AHB, and at least one target word. The resulting limit is at least the
pair alignment word. Pair packet overrides do not reduce this miss limit.
Selecting the same target explicitly can therefore change miss packet cuts.

A default target accepts a miss only while empty and blocks other packets until
that miss response leaves. APB, AHB, AXI4-Lite and AXI write misses wait at
least one arrival cycle. An AXI target of capacity one accepts a miss regardless
of the direction's turn. An AXI write miss answers in the following cycle unless
an older write of its ID or occupied response storage delays it.

Misses add no completion slot. They inherit the default target's failed state
and completion configuration.

## Address selection timing

Address inputs default to one bit. Conditions that cannot hold are rejected.
Windows with mutually exclusive conditions can overlap. Other overlapping
windows require the same condition and obey the nested-block rules.

Each distinct mapping condition takes a plane number above the global address.
The global address width plus the bits for plane numbers must fit in 48 bits.

The target is selected when the first request enters the packet path, with the
read request or first W beat. A burst waiting for capacity, its first W beat or
the other direction follows changing address inputs until that decision. After
selection, all its packets keep the decision, and traffic already in the network
completes at its selected target. Keep inputs stable around sampling edges. The
inputs have no synchronizers.

## Read error timing

An initiator read beat becomes available after the response words holding its
target sub-reads arrive and output capacity permits it. A widening response link
can gather several target words into one of those words. A first target DECERR
does not delay that beat until the target burst ends. A local miss makes no
target access and returns the requested number of zero-data beats. See
[error selection](#error-responses). Failed words do not cancel later accesses
or undo completed effects.
