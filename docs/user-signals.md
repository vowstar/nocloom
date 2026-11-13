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
packet's BUSER. AXI WUSER is unsupported. Unknown
configuration fields produce `NCL_SCHEMA_UNKNOWN_FIELD`.

## APB5 user ports

| Field | APB5 port | Range and default |
| --- | --- | --- |
| Initiator `user_width` | PAUSER input | 0 to 128, default 0 |
| Target `user` | PAUSER output, low bit first | 0 to 128 entries, default empty |
| Endpoint `response_user_width` | PBUSER output at an initiator, input at a target | 0 to 16, default 0 |
| Endpoint `data_user_width` | PWUSER and PRUSER | 0 or data bytes times `transport.data_user_bits_per_byte`, default 0 |

PWUSER is an input at an initiator and an output at a target. PRUSER has the
opposite direction.

Zero width omits the port. PAUSER uses the same named request flags and driver
syntax as AxUSER. PAUSER remains stable through SETUP and ACCESS, including
wait cycles. A target PAUSER bit can select a constant or a named flag.

PBUSER passes through `transport.response_user_bits`, which defaults to zero.
With zero transport width, initiator response user ports output zero. The field
keeps low bits when narrowed and adds zero high bits when widened. The
initiator applies the same rule to its response port. AXI BUSER and the
response portion of RUSER use this field. PWUSER and PRUSER use a separate
per-byte field.

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

Widths above the PAUSER and PBUSER limits produce `NCL_APB5_USER_WIDTH`.
`pwuser_width` and `pruser_width` are unknown fields and produce
`NCL_SCHEMA_UNKNOWN_FIELD`.

## Data user groups

`transport.data_user_bits_per_byte` sets the number of user bits per data byte,
called `k`. It defaults to 0 and accepts 0 to 4. Zero carries no data user.

Each APB5 endpoint sets `data_user_width` to 0 or `k * data_width / 8`.
Zero omits both PWUSER and PRUSER. A nonzero width requires nonzero `k`.
Other protocols reject a nonzero `data_user_width`.
Invalid widths produce `NCL_APB5_DATA_USER_WIDTH`.

This definition fragment sets `k` to 2. The 32-bit initiator gets 8-bit PWUSER
and PRUSER ports. The 16-bit target gets 4-bit ports.

```yaml
transport:
  data_user_bits_per_byte: 2
initiators:
  - {name: s, protocol: apb5, data_width: 32, data_user_width: 8}
targets:
  - {name: t, protocol: apb5, data_width: 16, data_user_width: 4}
```

Byte `b` carries user bits `k*b` through `k*b+k-1`.

PWUSER groups travel with written bytes. A byte with PSTRB zero carries a zero
user group. PWUSER stays stable through SETUP and ACCESS, including wait cycles.

PRUSER groups come from the completed target access that supplies each byte.

A source read that completes with PSLVERR set returns zero in every PRUSER bit.
This includes target errors, unmapped addresses and timeouts.
Successful accesses in a failed split read do not contribute user groups.
PRDATA still follows the
[APB read error rules](dedicated-targets.md#apb-target-transfer-details).

| Initiator data bits | Target data bits | Data user rule |
| --- | --- | --- |
| 8, 16 or 32 | Same width | Keep each byte's group in the same position |
| 8 | 16 or 32 | Place the write group in the addressed byte lane. Read that lane's group |
| 16 | 32 | Place the two write groups in the addressed halfword. Read that halfword's groups |
| 16 | 8 | Visit each enabled byte in address order. Read each group from its own access |
| 32 | 8 | Visit each enabled byte in address order. Read each group from its own access |
| 32 | 16 | Visit each enabled halfword in address order. Read each pair of groups from its own access |

Unused lanes carry zero user groups. Packet splitting, internal width changes,
clock crossings and pipes keep every group with its data byte.

| Endpoint without data user ports | Rule |
| --- | --- |
| AXI, AHB or APB4 and earlier initiator writing an APB5 target | PWUSER is zero |
| AXI, AHB or APB4 and earlier initiator reading an APB5 target | Discard PRUSER |
| APB5 initiator writing a target without data user ports | Discard PWUSER |
| APB5 initiator reading a target without data user ports | PRUSER is zero |
| APB5 endpoint with `data_user_width: 0` | Use the same rules as an endpoint without data user ports |

These connections are legal. PAUSER and PBUSER use their own fields.
Data user parity check signals, PWAKEUP and RME PNSE are unsupported and have
no ports.

The limits and signal timing follow
[Arm AMBA APB IHI 0024E][apb-spec],
sections 2.1 and 3.8. The data user limit follows the recommended maximum of
half the data width.

[apb-spec]: https://documentation-service.arm.com/static/63fe2c1356ea36189d4e79f3
