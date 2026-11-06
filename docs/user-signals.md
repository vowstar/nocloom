# Attributes and user signals

## Target attributes

Targets with attribute signals declare `attributes: amba` or `attributes: zero`.
The object form maps `read_cache`, `write_cache`, `prot` and `hprot` bit by bit.
Bit lists run from least significant to most significant.

An attribute bit selects a constant or a named AMBA attribute, optionally
inverted. Attribute names are `bufferable`, `modifiable`, `allocate`, `lookup`,
`privileged`, `nonsecure` and `instruction`.

## User signals

`transport.user_flags` declares named request bits. A driver selects an AMBA
attribute, AxUSER bit or constant, optionally inverted. AXI4, AXI3 and AXI4-Lite
targets map flags into `user`.

`user_width` enables initiator AWUSER and ARUSER ports. A target's `user` list
sets its AWUSER and ARUSER width. `response_user_width` enables BUSER and RUSER
ports. All nonzero user port widths are 1 to 256.

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
`user_flags`. `user[i]` selects an initiator AxUSER bit within its declared
width. Target user lists accept constants or declared flag names, with optional
inversion, low bit first.

Other target protocols reject `user`. Unknown flags produce `NCL_USER_FLAGS`.
Flags remain unchanged across packet splitting, width conversion and crossings.

Response user fields truncate or zero-extend to `transport.response_user_bits`.
A target without response user ports and every local response supply zero.
AXI4-Lite packets use their first word's BUSER. Every read packet repeats its
first RUSER.

Split reads use each packet's RUSER. A split write returns the final packet's
BUSER. WUSER and per-byte user fields are unsupported. Unknown configuration
fields produce `NCL_SCHEMA_UNKNOWN_FIELD`.
