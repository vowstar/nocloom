# Attributes and user signals

## Target attributes

AHB5 targets require `attributes: amba`. Their HPROT and optional HNONSEC
follow the [AHB5 mappings](ahb.md#ahb5-basic-properties).

Other targets with attribute signals declare `attributes: amba` or
`attributes: zero`.
The object form maps `read_cache`, `write_cache`, `prot` and `hprot` bit by bit.
Bit lists run from least significant to most significant.

An attribute bit selects a constant or a named AMBA attribute, optionally
inverted. Attribute names are `bufferable`, `modifiable`, `allocate`, `lookup`,
`privileged`, `nonsecure` and `instruction`.

## User signals

`transport.user_flags` declares named request bits. A driver selects an AMBA
attribute, user port bit or constant, optionally inverted. AXI4, AXI3,
AXI4-Lite and APB5 targets map flags into `user`.

On AXI endpoints, `user_width` enables initiator AWUSER and ARUSER ports. A
target's `user` list sets its AWUSER and ARUSER width. `response_user_width`
enables BUSER and RUSER ports. AXI user port widths are 1 to 256.

`transport.response_user_bits` is 0 to 256 and sizes the response field.
Initiators receive low configured response bits, zero-extended.

## Attribute and user encodings

The object form of `attributes` has optional `preset`, default `zero`. Only
signal lists present on that target are accepted. APB3 and APB2 reject attribute
mappings. Constants are 0 and 1. Prefix a driver with `~` to invert it.

Bufferable is CACHE bit 0, modifiable bit 1, allocate read bit 2 or write bit 3,
and lookup is CACHE bit 2 OR bit 3. Privileged, nonsecure and instruction are
PROT bits 0, 1 and 2.

The AMBA preset drives read CACHE as bufferable, modifiable, allocate and lookup
from low to high. Write CACHE uses bufferable, modifiable, lookup and allocate.
PROT keeps its three initiator attributes. AHB maps instruction, privileged,
bufferable and modifiable before inverting its instruction output. The zero
preset therefore gives HPROT `0001`.

Missing required target attributes produce `NCL_TARGET_ATTRIBUTES`.

Flag names must be distinct. Each initiator drives every flag through
`user_flags`. `user[i]` selects an initiator AxUSER or PAUSER bit within its
declared width. Target user lists accept constants or declared flag names, with
optional inversion, low bit first.

Other target protocols reject `user`. Unknown flags produce `NCL_USER_FLAGS`.
Flags remain unchanged across packet splitting, width conversion and crossings.

Response user fields truncate or zero-extend to `transport.response_user_bits`.
A target without response user ports and every local response supply zero.
AXI4-Lite packets use their first word's BUSER. Every AXI read packet repeats
its first RUSER.

Split AXI reads use each packet's RUSER. A split write returns the final
packet's BUSER. WUSER and per-byte user fields are unsupported. Unknown
configuration fields produce `NCL_SCHEMA_UNKNOWN_FIELD`.

## APB5 user ports

| Field | APB5 port | Range and default |
| --- | --- | --- |
| Initiator `user_width` | PAUSER input | 0 to 128, default 0 |
| Target `user` | PAUSER output, low bit first | 0 to 128 entries, default empty |
| Endpoint `response_user_width` | PBUSER output at an initiator, input at a target | 0 to 16, default 0 |

Zero width omits the port. PAUSER uses the same named request flags and driver
syntax as AxUSER. PAUSER remains stable through SETUP and ACCESS, including
wait cycles. A target PAUSER bit can select a constant or a named flag.

PBUSER passes through `transport.response_user_bits`, which defaults to zero.
With zero transport width, initiator response user ports output zero. The field
keeps low bits when narrowed and adds zero high bits when widened. The
initiator applies the same rule to its response port. AXI BUSER and the
response portion of RUSER use this field. No data user bits are carried.

Each packet takes PBUSER from its first completed APB5 target access.
Completion requires PSEL, PENABLE and PREADY, including accesses with PSLVERR.
Later completed accesses in the packet keep that first value.

| Response | Response user |
| --- | --- |
| Read, including a timeout response after an access completed | First completed access's PBUSER |
| Write without a timeout | First completed access's PBUSER |
| Write completed by a timeout | Zero, including when an earlier access completed |

A packet with no completed target access returns zero. Each new packet starts
with zero response user. Common reset clears an unfinished packet's value.
Split reads use each packet's value. Split writes return the final packet's
value.

[The APB5 example](../examples/apb5_user.yaml) maps seven PAUSER bits through
named flags and enables a 16-bit PBUSER return path. A target that completes a
transfer with PBUSER `0x8de2` returns that value to the initiator.

A target without response user ports supplies zero. Local error responses also
supply zero. A protocol without request user ports can drive named flags from
constants or attributes. A target without request user ports discards them.

Widths above the APB5 limits produce `NCL_APB5_USER_WIDTH`. Data user width
fields, including `data_user_width`, `pwuser_width` and `pruser_width`, produce
`NCL_SCHEMA_UNKNOWN_FIELD`.
