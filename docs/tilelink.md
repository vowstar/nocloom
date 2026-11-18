# TileLink

This page covers TileLink endpoint setup, port integration and progress.

Both endpoint roles use channels A and D with 32-bit data on a 32-bit
network. The rules use [TileLink 1.8.1][spec], sections 3, 4, 6, 7 and 8.

| Protocol value | Setup and behavior |
| --- | --- |
| `tl_ul` | [TL-UL initiators and targets](tilelink-ul.md) |
| `tl_uh` | [TL-UH initiators](tilelink-uh-initiators.md) and [TL-UH targets](tilelink-uh-targets.md) |

## Endpoint fields

These fields apply to both protocol values. The role pages give the
profile-specific fields and defaults.

| Field | Meaning | Values and defaults |
| --- | --- | --- |
| `data_width` | A and D data bits | Required in YAML |
| `source_width` | A and D source bits | 0 to 16 bits. Initiators default to 4. Target defaults depend on capacity |
| `size_width` | A and D size encoding bits | 2 to 4 bits |
| `sink_width` | D sink bits | 0 to 16 bits, default 0 |
| Initiator `max_outstanding` | Shared capacity for reads and writes | 1 to 64 messages. Inherits `transport.max_outstanding`, which defaults to 1 |
| Target `flows` | Target port sets | 1, the default |
| Target `fixed_bursts` | Incoming FIXED packet handling | `reject` (default) or `single` |
| Target `read_side_effects` | Preserve eligible non-modifiable read byte ranges | Boolean, default `false`. See [read side effects](packets.md#read-side-effects) |

Zero source or sink width omits that signal. An omitted source has value
zero. Initiators drive D sink to zero. Targets ignore it.

Addresses are byte addresses. Initiators accept 2 to 48 address bits, up to
the global width. Targets accept 2 to 32 local address bits. Each mapping
must hold at least one target word.

## TL-UH size encoding

`max_transfer_bytes` is a power of two from 4 to 4096 bytes and defaults to
64. The size value is the base-two logarithm of the message bytes. A 64-byte
message has size 6. Every configuration accepts byte and halfword accesses.

`size_width` defaults to 3 regardless of the maximum message size. Both
initiator and target ports must encode their configured maximum message size.
A 2-bit port can encode up to size 3 (8 bytes). A 3-bit port can encode up to
size 7 (128 bytes). Use 4 bits for larger messages up to the 4096-byte limit.

## Configuration limits

| Applies to | Rejected settings |
| --- | --- |
| Both roles | `id_width`, `len_width`, user ports and nonzero scheduling levels. TileLink uses `source_width` and has no length port |
| Initiators | `access`, `narrow_bursts`, changes to port pipes, shapers and schedulers |
| Targets | `attributes` and `timeout.completion: true` |

Invalid TileLink fields produce
[`NCL_TILELINK_PROFILE`](diagnostics.md#tilelink). Other limits produce the
applicable [diagnostic](diagnostics.md).

A 32-bit APB initiator can use a shaper at scheduling level zero to limit
traffic to a TL-UL target.

Networks with error logs reject TileLink initiators. Error logs cannot
observe TileLink targets. TileLink initiators cannot drive audit flags or
access error log registers.

## Errors and progress

A TileLink target watchdog with `completion: false` reports expiry through
`nl_t_<name>_timed_out` for system recovery. The watchdog reports status only.
See the [TL-UH watchdog](tilelink-uh-targets.md#watchdog) for packet context
timing.

Section 4.3 requires timeouts when bridging to buses that do not guarantee
progress. NocLoom accepts non-TileLink targets without such a timeout. Forward
progress then depends on those targets completing requests. A completing
[timeout](timeout-and-progress.md#target-timeouts) can provide error completion
when a non-TileLink target stops. A master must accept D responses in finite
time without waiting for later A handshakes. See the shared
[progress requirements](timeout-and-progress.md#shared-request-and-response-paths).

## Clock, reset and ports

Both ends of each TileLink interface must use the endpoint domain's clock,
reset and power. The network reset is active low. Invert the domain reset when
connecting an external active-high TileLink reset. Hold valid low for at least
100 reset cycles, as section 3.2.2 requires.

Reset all domains together. An isolated reset requires no traffic across its
boundary. Internal clock domain crossings use the network's crossing
and reset rules. The domain clock can be gated through the endpoint's normal
`clock_gating` setting.

Ports use `nl_i_<name>_a_opcode` and `nl_t_<name>_a_opcode`. The same prefix
applies to other A and D signals.

Initiator D outputs come from local state or constants. Handshake outputs
have no combinational dependency on external handshake inputs. TL-UH
initiator A ready uses the offered opcode, size and source to check admission.
TL-UH target D ready uses the offered source when the source port is present.

| Output | TileLink information |
| --- | --- |
| Map ports | Signal widths and directions |
| Map `definition.tilelink` | Each endpoint's effective source, size and sink widths, maximum message size and capacity |
| IP-XACT | Plain model ports, without an AMBA bus interface mapping |

## Option table

| Level | Option | Support |
| --- | --- | --- |
| TL-UL | A and D operations | [Supported operations and local responses](tilelink-ul.md#initiators) |
| TL-UH | Initiator A and D operations | [Supported operations and local responses](tilelink-uh-initiators.md#messages) |
| TL-UH | Target A and D operations | [Supported messages and packet handling](tilelink-uh-targets.md#messages) |
| TL-C | B, C and E channels and coherent operations | No protocol value or ports. Unknown fields produce diagnostics (section 9) |

[spec]: https://sifive.cdn.prismic.io/sifive/7bef6f5c-ed3a-4712-866a-1a2e0c6b7b13_tilelink_spec_1.8.1.pdf
