# Error log registers

The [error logging configuration](error-logging.md#configuration) defines the
register window and its banks. APB initiators access them through the network.

## Register layout

This section describes register layout 2. A bank has 16 words of 32 bits.
Offsets are bytes within each bank.

| Offset | Word | Content |
| --- | --- | --- |
| `0x00` | `ID` | Layout number, log index, logger index and capabilities |
| `0x04` | `STATE` | The `rw1c` and `rw` fields, and the class, access properties and sequence of the record |
| `0x08` | `ADDRESS` | Low 32 bits of the address |
| `0x0c` | `EXTENT` | High 16 bits of the address, and the byte count |
| `0x10` | `PATH` | Source flow and target flow |
| `0x14` | `REGION` | Region index |
| `0x18` | `AUDIT` | Audit flags |
| `0x1c` | `REVISION` | Revision value |
| `0x20` to `0x3c` | `USER0` to `USER7` | User flags |

The word names are names of this manual. The map and the C header name fields.
The record words are the words at `0x08` to `0x18` and `0x20` to `0x3c`. A
record field is a field with a name that starts with `record_`, except
`record_held` and `record_overflow`.

| Offset | Bits | Field | Access | Meaning |
| --- | --- | --- | --- | --- |
| `0x00` | 31:24 | `layout` | `ro` | Register layout number, which is 2 |
| `0x00` | 21:16 | `log_index` | `ro` | Index of the log in `error_logging.logs`, 0 to 63 |
| `0x00` | 12:8 | `logger_index` | `ro` | Index of the logger in its chain, 0 to 31 |
| `0x00` | 4 | `capability_given_revision` | `ro` | 1 when `REVISION` contains `error_logging.revision`. 0 when it contains the generator version |
| `0x00` | 3 | `capability_next_logger` | `ro` | 1 when another logger follows this logger in the same chain |
| `0x00` | 2 | `capability_target_user_flags` | `ro` | Equals `whole_record` of the log. If `capability_user_flags` is also 1, records of target errors carry user flags |
| `0x00` | 1 | `capability_user_flags` | `ro` | 1 when the network has user flags. The `USER` words then have content |
| `0x00` | 0 | `capability_audit_flags` | `ro` | 1 when the definition declares audit flags. The `AUDIT` word then has content |
| `0x04` | 31:16 | `record_sequence` | `ro` | Source AxID, zero for APB4 |
| `0x04` | 15 | `record_address_partial` | `ro` | The address contains only the retained low bits |
| `0x04` | 14 | `record_exclusive` | `ro` | Exclusive access. Set only on a decode miss |
| `0x04` | 13 | `record_wrap` | `ro` | The access is a WRAP burst |
| `0x04` | 12 | `record_write` | `ro` | The access is a write |
| `0x04` | 9 | `wait_when_full` | `rw` | When set, a record that reaches this logger while it is full waits until software clears the logger |
| `0x04` | 8 | `alarm_enable` | `rw` | When set, the alarm output follows `record_held` one cycle later |
| `0x04` | 7:4 | `record_class` | `ro` | One bit per [error class](#error-classes) |
| `0x04` | 1 | `record_overflow` | `rw1c` | Reports a loss at this logger or at a related-clock probe that feeds it as the first logger |
| `0x04` | 0 | `record_held` | `rw1c` | The logger holds a record. Writing one does not erase the record fields |
| `0x08` | 31:0 | `record_address_low` | `ro` | Address bits 31:0 |
| `0x0c` | 31:16 | `record_bytes` | `ro` | Byte count of the access, 1 to 32768 |
| `0x0c` | 15:0 | `record_address_high` | `ro` | Address bits 47:32 |
| `0x10` | 28:16 | `record_target_path` | `ro` | Target flow index. `route_values.target_path` in the map names the target and flow of each value |
| `0x10` | 12:0 | `record_source_path` | `ro` | Source flow index. `route_values.source_path` in the map names the initiator and flow of each value |
| `0x14` | 31:0 | `record_region_index` | `ro` | Index of the decoded mapping block or miss aperture |
| `0x18` | 31:0 | `record_audit_flags` | `ro` | Audit flags, first declared in the lowest bit |
| `0x1c` | 31:0 | `revision` | `ro` | `error_logging.revision`, or the generator version |
| `0x20 + 4k` | 31:0 | `record_user_flags<k>`, `k` from 0 to 7 | `ro` | User flags `32k` to `32k + 31`, first declared lowest |

Reserved bits are `ID` 23:22, 15:13 and 7:5, `STATE` 11:10 and 3:2, and
`PATH` 31:29 and 15:13. Bits 7:6 of `STATE` are reserved for error classes.

| Access | Read | Write |
| --- | --- | --- |
| `ro` | A value | No effect |
| `rw` | The value written | Each byte follows its write strobe |
| `rw1c` | A hardware value | Writing one under the strobe of its byte clears the field. Writing zero does nothing |

The fields of the word at `0x00` and `revision` are fixed at generation and
have no flip-flop. The map gives their `value`. Every other `ro` field reads a
hardware value. Every field that is not fixed at generation resets to zero. The
map gives the reset value of each `rw` field.

An address inside the register window but outside every bank returns an error.
A field is absent when its width in this design is zero. Every bit of a bank
that no present field occupies reads zero and ignores writes without an error.
This includes the position of an absent field and the upper bits of a field
that is narrower in this design. The generated map gives the actual width of
each present field.

A value wider than 32 bits starts at bit 0 of a word and continues in the
following words, low word first. The address occupies `ADDRESS` and the low 16
bits of `EXTENT`. Read `0x08` and `0x0c` as one little-endian 64-bit value.
Bits 47:0 are the address and bits 63:48 are the byte count. The user flags
occupy `USER0` to `USER7`. User flag `n` is bit `n % 32` of the word at
`0x20 + 4 * (n / 32)`.

In `STATE`, byte 0 contains the `rw1c` fields and byte 1 the `rw` fields.
In `STATE`, a write changes only bits 0, 1, 8 and 9.

Each field of more than one bit starts at a hexadecimal digit and shares no
digit with another field. A register dump therefore reads digit by digit.

| Word | Dump | Content |
| --- | --- | --- |
| `ID` | `0x0202030b` | Layout 2, log index 2, logger index 3. The definition declares audit flags, the network has user flags and another logger follows |
| `STATE` | `0x00051121` | The logger holds a record of a write with AxID 5 and class `target`. The alarm is enabled |

`definition.log_registers` in the map describes the registers. Each bank has
its name, its local address (`base`) and its fields. Each field has its offset,
lowest bit, width, access kind and description. Use these positions for
register access.

For a bank with inaccessible words, `unreachable_words` gives their byte
offsets by initiator. An empty list means the initiator can reach every word.
`state_reachable` is true if any initiator can reach `STATE`. If both keys
are absent, every word is reachable.

`definition.error_logs` describes each log. Its `record_fields` lists the
stored fields that `record_bits` counts, lowest bit first. These positions are
positions in the stored record and not register positions. `record_fields`
omits stored fields of zero width. The stored field `last_offset` is
`record_bytes` minus one.

`record_fields` can also list `direction`, which is one for writes from non-APB
sources and zero otherwise. No register field contains `direction`. The log's
`route_values` gives the source and target flow indices. Endpoints use name
order and flows use increasing local index order, without padding. Record
values and target port AxID values use different encodings.

A related-clock probe reports a drop when the first logger in its chain takes
the waiting record. If that logger's filters reject the record while it is
empty, `record_overflow` can be set with `record_held` clear. Clearing
`record_held` alone also leaves a pending overflow set. Software cannot use
the overflow bit to distinguish a probe drop from a drop at a full logger.

## Error classes

`record_class` has one bit per error class. The map key is `error_classes`.

| Class | Bit of `STATE` | Value in `record_class` | Event |
| --- | --- | --- | --- |
| `decode` | 4 | 1 | Address decode or connectivity miss, or a target DECERR |
| `target` | 5 | 2 | Other target failures |
| Reserved | 7:6 | 0 | Reserved for future classes |

With `error_logging.capture_class: false`, the field reads zero.

## Driver rules

The bank address is `_BASE + _<BANK>_OFFSET` from the
[C headers](c-headers.md#error-log-header). For a bank cut by an address window,
use the field offsets from that window's base.

| Step | Rule |
| --- | --- |
| 1 | Check the map or header for reachable words. Read `ID` at `0x00` if reachable. Otherwise use the layout and capabilities from the map |
| 2 | Compare `layout`. If the driver does not know the layout number, do not use the bank |
| 3 | Read the capability bits. Ignore each set bit that the driver does not know |
| 4 | Identify the log and the logger with `log_index` and `logger_index`. `capability_next_logger` tells whether another logger follows |
| 5 | Read `REVISION` at `0x1c` if reachable. If the header gives `_REVISION_VALUE`, compare the word with it |
| 6 | If `STATE` is unreachable, software can read reachable fields but cannot check or clear `record_held`, enable the alarm or set `wait_when_full`. Otherwise read `STATE`. If `record_held` is 1, read the reachable record words. Then write byte 0 of the value read back to byte 0 of `STATE`. This clears `record_held` and `record_overflow` when they read 1 |
| 7 | If `record_class` has a bit that the driver does not know, treat the words at `0x08` to `0x14` as opaque values |
| 8 | If `STATE` is reachable, change `alarm_enable` or `wait_when_full` with zero to byte 0 of `STATE` or strobe only byte 1 |

A driver built with the header of one design can compare the whole word at
`0x00` with `_ID_VALUE` in place of steps 2 to 4.

Each read returns the whole word as sampled when its bank accepts the read
request. A capture, clear or overflow cannot mix bytes from different cycles.
Software that shares a bank must coordinate its clears across multiword reads.
The bank samples an access on the log's clock. A window in another domain
sends requests to the bank and receives responses through clock crossings.

The record words do not change while `record_held` is 1. Read them before the
write that clears `record_held`. If the logger admits a record in that cycle,
it captures the new words, keeps `record_held` set and does not set
`record_overflow` for that record. A full logger with `wait_when_full` set
admits the waiting record after the clear. A set `record_overflow`
stays set until software writes one to that bit.

## Layout rule

| Change in a later NocLoom version | Layout number |
| --- | --- |
| A new capability bit, a definition for a reserved bit, or a new error class | Stays 2 |
| A new class with its own meaning of the words at `0x08` to `0x14`. A capability bit declares that meaning | Stays 2 |
| A different position or meaning of a field of layout 2, or a different bank size | Changes |
| A different meaning of the words at `0x08` to `0x14` for a class of layout 2 | Changes |

In every layout, `layout` occupies bits 31:24 of the word at `0x00`. The values
0 and 1 are not layout numbers.

## Sizes

| Field | Bits | Limit |
| --- | --- | --- |
| `record_address_low`, `record_address_high` | 48 | Address width of at most 48 bits |
| `record_bytes` | 16 | At most 256 beats of 128 bytes, which are 32768 bytes |
| `record_source_path` | 13 | 256 initiators with at most 32 flows each |
| `record_target_path` | 13 | 256 targets with at most 32 flows each |
| `record_sequence` | 16 | `id_width` of at most 16 |
| `record_region_index` | 32 | At most 4294967296 mapping blocks and miss apertures for one flow pair |
| `record_user_flags0` to `record_user_flags7` | 256 | At most 256 `transport.user_flags` |

## Access time

An APB access to a bank takes the clock cycles below in the clock domain of the
register window. The count runs from the clock edge with `PENABLE` to the clock
edge with `PREADY`. The first edge counts as 1. These counts apply without
contention or source pipes.

A forward stage in `pipes.packets` adds one cycle. With only the backward
slot, access takes the same number of cycles without contention.
`pipes.issue` adds one cycle.

| Access | Cycles |
| --- | --- |
| Read a bank in a same-domain chain of `L` loggers | `L + 2` |
| Write a bank in a same-domain chain of `L` loggers | `L + 6` |
| Each register stage of that chain | 1 more |
| A register window with more than one log | 1 more |
| An address inside the register window but outside every bank | 2 |
| An address that no mapping decodes | 3 |

A chain has one register stage after every eighth bank that another bank
follows. A chain of 8 loggers or fewer has none and a chain of 32 loggers has
three. The count is the same for every bank of the chain. One log of 32 loggers
takes 37 cycles to read and 41 to write.

## Access frames

A request starts with one 9-bit header. Bits 8:4 select the logger in the
chain. Bits 3:0 select the register word in that bank. A read ends at its header.

A write follows with four byte words, most significant first. Bit 8 of
each data word is its byte strobe. A separate tail signal marks the last word.

In the register window's domain, banks return a header when the access
completes, and the window stores the sampled read word. Across domains, the
chain's serializer sends the header and the four sampled read bytes, most
significant first. A write returns only the header. Crossed access time
includes the two FIFO crossings and the serializer cycle.
