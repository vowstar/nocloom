# Registered feedback bursts

This page covers Wishbone CTI and BTE, target burst timing and buffering.

See [Wishbone](wishbone.md) for shared fields and port integration.

Set `burst_tags: true` on a classic initiator or target to add CTI and BTE.
An omitted field or `false` leaves these ports out. Other protocols reject
this field, including explicit `false`. With `cycles: pipelined`, only `false`
is allowed when the field is present.

A tagged target rejects its own
`timeout`. These rejections produce
[`NCL_WISHBONE_OPTION`](diagnostics.md#endpoints-and-transactions).
See [wishbone_registered_feedback.yaml][burst-example] for a complete
definition.

| Interface | Behavior with tags |
| --- | --- |
| Initiator | The port accepts every CTI and BTE value and ignores it. Each accepted word is a classic transfer and one network request at its supplied address. RULE 4.10 covers CTI 001 and reserved values |
| Target INCR | At least two consecutive full words form a linear burst. CTI is 010 before the last word and 111 on the last word. BTE is 00 |
| Target WRAP | Complete physical WRAP4, WRAP8 and WRAP16 with full SEL use BTE 01, 10 and 11. Nonfinal transfers have CTI 010. The final data transfer has CTI 111 |
| Target FIXED | With `fixed_bursts: single`, at least two 32-bit physical transfers with constant nonzero SEL use CTI 001 and a final 111. BTE is 00 |
| Other target shapes | Classic CTI 000 and BTE 00. A full-word INCR prefix can form a burst. Its remaining partial words stay classic. Zero-SEL write words make no bus access |

Burst tags apply after packet splitting and width conversion. The
[packet byte limit](wishbone.md#responses-and-target-options) applies. Narrow
INCR and split WRAP packets also hold at most 32 physical words.

## Buffering

The map and bundle record these fields at
`definition.wishbone.targets.<name>.buffering`.

| Field | Value | Meaning |
| --- | --- | --- |
| `write_banks` | 2 | Complete write packets can wait in two banks |
| `physical_words_per_bank` | 32 | Each bank holds at most 32 physical words, including zero-SEL words |
| `read_response_slots` | 2 | A bus read reserves space before STB is asserted |
| `active_packets` | 1 | One packet at a time owns the bus |

A write packet starts after its last physical word enters a bank. That bank
stays allocated until the result is stored. The other bank can collect a
packet while the bus serves the active packet. Reads use no write bank.
The bus serves accepted packets in arrival order.

## Timing and errors

Within a tagged burst, data and read response space allow the target to keep
STB high and complete one word per clock edge. A resource wait between words
lowers STB and keeps CYC high. A cycle with CYC and STB low separates bursts,
classic transfers and packets. Until a word terminates, the target holds
address, data, SEL, WE, CTI and BTE stable and keeps STB high. The target does
not accept a termination while STB is low.

ERR and enabled RTY follow the shared [word error rules](wishbone.md#responses-and-target-options).
Every tagged burst ends with a data transfer with CTI 111. A transfer can
wait indefinitely for slave termination.

The following example shows a three-word read burst. Signals are the values
before each rising edge. The network returns read response space at edge C.
The slave keeps ACK high in the gap, as permitted by RULE 4.15.

| Edge | CYC | STB | CTI | BTE | ACK | Result |
| --- | --- | --- | --- | --- | --- | --- |
| A | 1 | 1 | 010 | 00 | 1 | First word completes |
| B | 1 | 1 | 010 | 00 | 1 | Second word completes, read response slots become full |
| C | 1 | 0 | 010 | 00 | 1 | No bus transfer, a read response leaves |
| D | 1 | 1 | 111 | 00 | 1 | Third word completes |
| E | 0 | 0 | 111 | 00 | 1 | Boundary cycle, no bus transfer |

[burst-example]: ../examples/wishbone_registered_feedback.yaml
