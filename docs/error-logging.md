# Error logging

Error logs capture failed target transactions in software-readable register
banks. `error_logging` declares one register window, audit flags and logs. The
window is at most 1 MiB and aligns to its size. The register window has no
external target bus ports. APB initiators read and clear its banks through the
network.

## Supported networks

The register window requires a 32-bit network without scheduling levels. Its
access initiators use APB4 in one domain. Inside the register window, internal
switches route requests to banks. Each switch has at most four branches. Logs
observe supported 32-bit APB4 and AXI4 targets.

An observed APB4 target permits one packet in flight. AXI4 targets can permit
multiple packets.

## Configuration

| Object | Fields |
| --- | --- |
| `error_logging.registers` | `name`, `base`, `size`, `clock_domain` |
| `error_logging.audit_flags` | Named request bits recorded with errors |
| `error_logging.logs` | Log definitions in declaration order |
| `error_logging.revision` | Optional integer from 0 to 16777215 for the revision register |
| Each log | `name`, `watch`, optional `clock_domain`, `whole_record`, `serial_word_bits`, `loggers` |
| Each logger | Optional `skip`, `match`, `pass_on`, `offset` |

`watch` lists the data targets to observe. Its order is their arbitration order
at that log. The log uses the register window's domain when `clock_domain` is
omitted or explicitly names that domain. All logs of that window share their
domain. `whole_record` defaults to `false`.

`whole_record: true` retains every user flag and as many address bits as the
record field holds.

Each initiator drives every declared audit flag through its `audit_flags`
mapping. Audit flags record information and grant no access rights. At most 8
audit flags fit the record word.

`revision` sets bits 23:0 of the register at offset `0x04` in every bank.
Without `revision`, these bits hold the generator version, with major in bits
23:16, minor in bits 15:8 and patch in bits 7:0. The register and the RTL then
change with the NocLoom version. Set `revision` to keep the RTL independent of
the NocLoom version. The map states the value in use.

An empty `loggers` list creates one logger with default configuration. A chain
has at most 8 loggers. Each bank occupies 256 bytes. `offset` is its byte offset
in the window. Without an offset, it uses the next free bank position.

## Filters and forwarding

| Field | Behavior |
| --- | --- |
| `skip` | Drop `target` or `decode` error categories. Requires `transport.response_codes: distinct` |
| `match` | Require specified audit or user flag values. Unlisted flags can have either value |
| `pass_on: unless_logged` | Forward a record only when this logger does not capture it. This is the default |
| `pass_on: always` | Forward every record to the next logger |

A logger retains its first accepted record until software clears valid. While
full, later records follow its hold and forwarding configuration. The first
alarm output is `nl_o_<log>_alarm`. Additional chain loggers use
`nl_o_<log>_alarm<n>`. With alarm enabled, it follows record valid one cycle
later.

## Register layout

Offsets are bytes within each bank. Absent fields retain their reserved
positions. Unused bank addresses read zero and ignore writes without error.
Addresses within the register window but outside every bank return an error.

| Offset | Field | Behavior |
| --- | --- | --- |
| `0x00` | `identity_checksum`, `identity_type` | Table checksum in bits 23:0, type 1 in bits 31:24 |
| `0x04` | `revision_generator`, `revision_reserved` | `error_logging.revision` or the generator version in bits 23:0, zero above |
| `0x08` | `alarm_enable` | Bit 0 enables the alarm |
| `0x0c` | `serial_hold_when_full` | Bit 0 makes incoming records wait while this logger is full |
| `0x10` | `record_clear_valid`, `record_valid` | Write one to bit 0 under byte 0 strobe to clear valid. Bit 1 reports valid |
| `0x14` | Record header | Length, error and transaction properties |
| `0x18`, `0x1c` | `record_address_low`, `record_address_high` | Captured address |
| `0x20`, `0x24` | `record_route_low`, `record_route_high` | Captured route fields |
| `0x28` | `record_audit_flags` | First declared flag in the lowest bit |
| `0x2c` through `0x48` | `record_user_word0` through `record_user_word7` | User flags, first declared lowest |

| Starting bit | Field |
| --- | --- |
| 0 | `record_bytes_minus_one` |
| 16 | `record_error_code` |
| 24 | `record_write` |
| 25 | `record_exclusive` |
| 26 | `record_wrap` |
| 27 | `record_address_partial` |
| 28 and above | Reserved |

The generated map gives the actual width of each present field. Error code is
present only in distinct mode. It starts at zero, with one for target error and
two for decode miss. The partial bit indicates that the captured address
contains only retained low bits. Clearing valid does not erase the captured
field values.

Read `definition.log_registers` and `definition.error_logs` in the map for bank
offsets, field access modes, reset values and route field positions. The log's
`route_values` gives the compact source and target flow indices. Endpoints use
name order and flows use increasing local index order, without padding.

| Captured route order, low to high | Value |
| --- | --- |
| `region_index` | Index of the decoded mapping block or miss aperture |
| `target_path` | Global target flow index |
| `source_path` | Global source flow index |
| `sequence` | Source AxID, zero for APB4 |
| `direction` | One for writes from non-APB sources, zero otherwise |

Zero-width fields are absent. `route_fields` gives their bit positions. The
mapping block index belongs to the observation record and occupies no target
AxID bits. Captured route values and target port AxID values use different
encodings.

For consistent multiword reads, coordinate capture, valid status and clearing in
software.

## Capture behavior

A failed target packet creates an event on its first failed response word. A
decode miss creates an event on its last word. A log sees the responses of a
target in the order they leave its target adapter. The first failed response in
that order makes the record. This also holds when a read word and a write
response fail in the same target cycle. A write response is a packet of its own,
also between the words of a read burst.

Each watched target holds one waiting record. Another failure while that record
waits is lost. A full logger drops records unless its hold configuration makes
them wait.

A full logger or a waiting record does not block data requests, transaction
responses or register accesses.

Without `whole_record`, a hit retains the low 8 address bits and sets the
partial bit. A decode miss clears the partial bit but still uses the initiator
miss mask and record address width. A clear partial bit does not guarantee the
complete original address.

A new recorded packet can start at a read word that follows a gap of two or more
cycles. It can also start after a word of another burst or after a write
response. Such a record keeps only the low 8 address bits of its first failed
word and sets partial. All words of a FIXED burst have the burst address. A
clear concurrent with capture leaves the logger empty while retaining the newly
captured fields.

## Logger limits and software rules

The register window must be reachable in `connectivity`, like a target. C++
declares a target with `log_registers` and sets `Definition::error_logging`. A
log must watch at least one data target. No data target can be watched twice
across the logs.

All logs must use the same register window. A bank offset must be a multiple of
256 and fit within the window without overlapping another bank.

Observed targets reject flows, one-direction access, timeouts, endpoint pipes,
exclusive support and upsizing. An observed APB4 target also rejects
back-to-back mode. Reaching initiators must be plain 32-bit APB4 or AXI4, with
one flow and no explicit default target. The network rejects link pipes and
address selection mappings. With user flags and no whole record, both the target
and its reaching initiators must use AXI4.

With a whole record, target and initiator protocols must match.

Audit flags must have distinct valid names, with one driver per initiator per
flag. Drivers accept constants, PROT attributes or AxUSER bits, optionally
inverted. CACHE attributes are rejected for audit flags. Sources wider than
their targets and targets wider than transport are rejected with audit flags.
User filters require fields available in the record.

For a log in another domain, observed targets must share a distinct domain and
use APB4. Both clocks must declare periods. Whole records and other network link
crossings are rejected in that configuration. The service crossing requires a
common reset and non-divided clocks. Apply the
[timing constraints](timing-constraints.md#cdc-constraints) to the generated
crossing bindings.

The exclusive record bit describes an exclusive access on a decode miss.

Target errors without whole records have zero user flags. Decode misses can
retain those flags. Flags appear in declaration order, first declared lowest.

`serial_word_bits` selects 8, 16 or 32 bits per crossing word. Its default
is 32. The stored fields define `record_bits` in the map. The map lists each
stored field in `record_fields`, lowest bit first.

`serial_words` is `ceil(record_bits / serial_word_bits)`. `transfer_words`
reports words per delivery. Same-domain delivery has no serial wait. Crossed
delivery holds one snapshot through stalls until its final word enters the FIFO.
Delivery completes when the receiver accepts that word. Reset discards
incomplete records.

Capture requires matching filters and an empty logger on a forwarding path that
reaches it. A concurrent clear still leaves valid low. Held forwarding loggers
delay delivery.

A permanent software hold has no finite latency bound. The dependency graph
records this assumption as `env.logger_hold` with identifier `F6`. A held chain
accepts a record only when every logger on its forwarding path can accept it.
