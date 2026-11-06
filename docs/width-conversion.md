# Widths and AXI target interfaces

## AXI targets

AXI4 targets accept ID widths from 0 to 31. Without `id_width`, a single-packet
target defaults to 4 unless it uses `exclusive: target`. Otherwise, NocLoom
derives the width from initiator IDs and initiator count, with enough bits for
target flows and exclusive owners. Read each AXI target's ID width from
`definition.endpoint_transactions.targets.<target>.id_width` in the map. AXI4
target length widths are 4 to 8 bits, default 4.

AXI3 targets require a length width of 4. Capacity and read interleaving are
described in [transactions](transactions.md).

AXI targets preserve supported FIXED, INCR and WRAP accesses within packet and
conversion limits. A packet can become a separate target burst. The initiator
still receives its original response shape.

## Width conversion

APB initiators place their physical word in a 32-bit container.

A wider initiator splits into target words. A narrower transfer can merge into a
target word. Write strobes preserve the selected bytes, and unstrobed bytes
reach the target as zero. Read words gather into each initiator beat as soon as
that beat is complete. A failed subresponse makes that entire initiator beat
zero.

Target errors do not cancel the remaining accesses.

A FIXED burst whose transfer is wider than the target is refused. INCR and WRAP
pieces retain their byte address sequence under the packet rules. A wide
physical port does not permit an AxSIZE transfer wider than 128 bytes. Read
values outside the requested byte lanes are unspecified, except as stated for
[packed INCR reads](#width-transformation-details).

See [axi4_endpoint_widths.yaml](../examples/axi4_endpoint_widths.yaml) and
[bounded packets](packets.md#bounded-packets).

## Width transformation details

The AXI target merge combines narrower bursts regardless of AxCACHE. It can
change non-modifiable transaction addresses, sizes, lengths and burst types
beyond the transformations AXI A4.3 permits. Use this profile only with targets
that accept those transformations. A uniform-width network passes narrow INCR
unchanged after initiator packing rules.

A merged INCR keeps its start address and counts covered target words. Several
words use full target size. A single word uses the smallest size covering its
bytes, including an unaligned full-width initiator beat. A narrow WRAP fitting
one word becomes one INCR at the container base with the container size.
Replayed packed INCR read words contain zero in bytes beyond the source burst's
end. These words also keep bytes below an unaligned start at zero.

A larger WRAP becomes full-width target WRAP. An unaligned write sends the
completed start word last after wrapping. Full-width WRAP and FIXED pass
unchanged when no other conversion rule applies.

Downsizing keeps the first piece's raw start address. Later pieces start at
their first target word. An unaligned first beat sends no word below its start
word. WRAP containers of 2, 4, 8 or 16 target words stay WRAP. Larger containers
become two INCR runs in wrap order.

Each run respects packet length limits. A burst wider than its target never
converts FIXED into pieces. Decode precedes width refusal. Unmapped FIXED
returns a decode error.

Conversion pipelines up to four pieces per direction. Local completions wait for
prior converted responses. Responses retain one accumulated B and the original
RLAST, including 64-bit Lite to 32-bit targets.

Width conversion creates no additional outstanding entry.

A narrow nonexclusive AXI4 WRAP read can become full-word WRAP when initiator
and target widths match. This requires no dispatch pipe, folding, packet cut or
read side effects, and does not apply to AXI3. For an unaligned start, the saved
start word's low lanes return after the last target word. RREADY pauses for that
replay cycle. Each read descriptor owns its saved-word storage until its final
response leaves.

Read replay storage grows with `K` times the target data width, where `K` is the
target capacity.

Different target IDs can retain saved words concurrently.

## Beat rates

See [interface timing](interface-timing.md) for cycle and ready-path guarantees.

Request headers share links with W, and R shares links with B. Packet
boundaries, capacity and competing traffic reduce the
[interface rates](interface-timing.md). See
[half-rate pipes](pipes.md#pipe-operation).
