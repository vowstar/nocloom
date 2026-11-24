# Error logging

`error_logging` declares a register window, audit flags and logs of failed
transactions. See the [register layout](error-log-registers.md#register-layout)
and [driver rules](error-log-registers.md#driver-rules).

| Term | Meaning |
| --- | --- |
| Register window | The address range of all banks. `error_logging.registers` declares it |
| Log | One entry of `error_logging.logs`. It watches a list of targets |
| Logger | One entry of `loggers` of a log. It holds one record and has one bank |
| Chain | The loggers of one log, in declaration order |
| Bank | The 64 bytes of registers of one logger |
| Record | The data that a logger keeps about one failed transaction |

| Size of the register window | Rule |
| --- | --- |
| 64 to 2048 bytes | A power of two |
| 4 KiB to 1 MiB | A multiple of 4 KiB |

The global base aligns to the window size. At 64 bytes per bank, 64 logs of
32 loggers fit in 128 KiB.

## Supported networks

A network with error logs has one register window with one unconditional
mapping. It requires a 32-bit network without scheduling levels. Access
initiators use APB4 in one domain. A window has at most 64 logs. Logs observe
supported 32-bit APB4 and AXI4 targets. Error logs exclude Wishbone and TL-UL initiators.

Observed APB4 targets allow one packet in flight and reject back-to-back
mode. AXI4 targets can allow several packets. Observed targets reject flows,
one-direction access, timeouts, endpoint pipes, exclusive support and upsizing.
Their initiators use 32-bit APB4 or AXI4 with one flow and no explicit default
target. The network rejects link pipes and address selection mappings.

With user flags and no whole record, both the target and its reaching initiators
must use AXI4. With a whole record, target and initiator protocols must match.

A log in the register window's domain can watch a target in another domain
only when both clocks share one root. A log outside the window's domain can
observe targets outside its own domain. Those targets must share one
clock domain and use APB4, and no observed target can be in the domain of the
log. Both clocks need known periods. Whole records and other network link
crossings are rejected in that configuration.

This last configuration requires a common reset and non-divided clocks. Apply the
[timing constraints](timing-constraints.md#cdc-constraints) to the crossing bindings.

## Configuration

| Object | Fields |
| --- | --- |
| `error_logging.registers` | `name`, `base`, `size`, `clock_domain` |
| `error_logging.registers.local_address` | Optional target local byte address at `base`, default zero |
| `error_logging.audit_flags` | Named request bits recorded with errors |
| `error_logging.logs` | Log definitions in declaration order |
| `error_logging.revision` | Optional unquoted decimal integer from 0 to 4294967295 for the revision register |
| `error_logging.capture_class` | Boolean, default `true`. Stores the [class](error-log-registers.md#error-classes) seen at the target before the initiator applies `slverr_only` |
| Each log | `name`, `watch`, optional `clock_domain`, `whole_record`, `serial_word_bits`, `loggers` |
| Each logger | Optional `skip`, `match`, `pass_on`, `offset` |

This definition has one log with a chain of two loggers.

```yaml
schema_version: 10
name: logged
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk, reset_port: reset_n, test_port: test}
initiators:
  - name: cfg
    clock_domain: main
    protocol: apb4
    data_width: 32
    audit_flags: {audit: privileged}
targets:
  - name: ram0
    clock_domain: main
    protocol: apb4
    data_width: 32
    attributes: amba
    mappings: [{base: "0x0", size: "0x1000"}]
error_logging:
  registers: {name: csr, base: "0x100000", size: "0x1000", clock_domain: main}
  audit_flags: [audit]
  logs:
    - name: fault
      watch: [ram0]
      loggers:
        - {skip: [decode], match: {audit: true}}
        - {}
connectivity: all
output: {backend: verilog, language: verilog_2001}
```

The first logger captures target errors with `audit` set. Every other record
goes to the second logger.

`watch` lists the data targets to observe. Its order is their arbitration order
at that log. The log and its banks use the clock and reset of `clock_domain`.
If omitted, they use the register window's domain. All logs of the window
share one domain. `whole_record` defaults to `false`.

`whole_record: true` retains every user flag and as many address bits as the
address of the record holds.

A definition has at most 32 audit flags, one per bit in the audit word. Names
must be valid and distinct. Each initiator drives every flag through
`audit_flags`, using a constant, a PROT attribute or an AxUSER bit, optionally
inverted. Audit flags grant no access rights. They require sources no wider
than their targets and targets no wider than the transport.

`revision` sets the register at offset `0x1c` in every bank. Without
`revision`, the register contains the generator version. Major is in bits
23:16, minor in bits 15:8 and patch in bits 7:0. Bits 31:24 are zero. The RTL,
the map and the dependency file then change with the NocLoom version.

Set `revision` for RTL independent of the generator version. The map gives the
value and `capability_given_revision` reads 1. Give each build its own revision.

An empty `loggers` list creates one default logger. A chain has at most 32.
An explicit `offset` fixes the bank's local byte address on the 64-byte grid
for this layout, even if a mapping covers some words. The map and C header
show which words initiators can reach.

By default, an omitted offset puts the first bank at zero or a later bank
immediately after its predecessor. Placement changes only if a mapping takes
a window address from a connected initiator. The generator then reserves
explicit positions and ranks free positions on the 64-byte grid.

A position is class A if all connected initiators can reach every word.
A position is class B if at least one can reach both `ID` and `STATE`.
Class A is part of class B. If class B has enough positions, the generator
takes as many class A positions as possible and the lowest addresses within
each class. If class B has too few positions, the default placement applies.

The generator gives those addresses in ascending order to banks without
offsets in declaration order.

| Bank | Name |
| --- | --- |
| First logger of the first log | The name of the register window |
| First logger of a later log | The name of the log |
| A later logger of a log, with `logger_index` `n` | `<log>_<n>` |

Bank names must be distinct (`NCL_NAME_OUTPUT_COLLISION`).

`registers.local_address` is the target local byte address at the mapping base.
It uses an [address string](address-map.md#target-mappings) and defaults to
zero. The mapped interval must fit the target address width, which is the
network address width for the register window. Whole-page mappings require
4 KiB alignment. A sub-page mapping requires alignment to its size.

Each piece uses `local = global - base + local_address`. Only the mapped interval
is reachable. Each bank must fit in the window. The offsets in the
[C headers](c-headers.md#error-log-header) use each initiator's window bases.

For `base: "0x100000"`, `local_address: "0x1000"` and `offset: "0x1000"`,
register offset `0x10` is at global address `0x100010`. The C++ field
`AddressMapping::local_address` has the same checks.

| Invalid `local_address` | Diagnostic |
| --- | --- |
| Unquoted value or a value other than a single-line string | `NCL_SCHEMA_TYPE` |
| Invalid address string or an integer above the `uint64_t` range | `NCL_ADDRESS_RANGE` |
| Misaligned local start | `NCL_ADDRESS_RANGE` |
| Local start or mapped interval outside the target address width | `NCL_ENDPOINT_ADDRESS_WIDTH` |

## Filters and forwarding

| Field | Behavior |
| --- | --- |
| `skip` | Reject records of the `target` or `decode` error class, or both. Requires `error_logging.capture_class: true` |
| `match` | Require specified audit or user flag values. Unlisted flags can have either value. A user flag requires `whole_record: true` |
| `pass_on: unless_logged` | Forward a record only when the filters of this logger reject it. This is the default |
| `pass_on: always` | Forward every record to the next logger |

A logger captures a record when its filters accept it and `record_held` is 0.
Software clears `record_held` to release the record. With `pass_on: unless_logged`,
a full logger stops records its filters accept. See [Capture behavior](#capture-behavior).

The first alarm is `nl_o_<log>_alarm` and the others use `nl_o_<log>_alarm<n>`.
An alarm waits for its clock to run. `record_overflow` does not drive alarms.

## Capture behavior

A failed target packet creates an event on its first failed response word.
A decode miss creates one on its last word. The log records the first failed
response in adapter output order, including when a read and a write fail in the
same cycle. A write response is its own packet and can arrive inside a read burst.

Each watched target holds one waiting record. The table shows new overflows.

| Case | Record | `record_overflow` |
| --- | --- | --- |
| A probe holds a waiting record, and more failures arrive before the record leaves | The probe drops the later failures | Set at the first logger when the chain takes the waiting record, even if that logger rejects it |
| The waiting record of a probe leaves as a new failure arrives | The probe holds the new failure | No new overflow |
| A full logger has `wait_when_full` set | The record waits. A later failure is dropped at the probe as above | No overflow from the logger itself |
| A logger accepts a record in the cycle software clears its held bit | The new record is held | No overflow from the logger |
| A logger rejects a record that does not wait | The record passes to the next logger if one follows | No overflow from the logger |
| A full logger accepts a record with `wait_when_full` clear | The logger drops the record. `pass_on: always` still forwards it | Set at that logger |

A record moves through a chain when no logger on its path makes it wait. A full
logger or a waiting record does not block data traffic or register accesses.

Without `whole_record`, a record of a target error keeps the low 8 address bits
and sets `record_address_partial`. On a decode miss that bit is 0, and the
address uses the initiator miss mask and the record address width. A
`record_address_partial` of 0 does not guarantee the complete original address.
Partial target records have zero user flags. Decode misses can retain them.

A new recorded packet can start at a read word that follows a gap of two or more
cycles. It can also start after a word of another burst or after a write
response. Such a record keeps only the low 8 address bits of its first failed
word and sets `record_address_partial`. All words of a FIXED burst have the
burst address.

## Serial delivery

`serial_word_bits` selects 8, 16 or 32 bits per crossing word. Its default
is 32. `serial_words` is `ceil(record_bits / serial_word_bits)`.
`transfer_words` reports words per delivery.

Targets in the log's domain deliver directly. On clocks with a shared root,
the probe holds events from target edges until a log edge takes the record.
The first selected log edge at or after effective reset release opens capture.
Events before that edge or during target reset are discarded.

The sender holds the record until its last word enters the asynchronous FIFO.
Delivery ends on receipt of that word. Reset discards incomplete records.

## Logger limits and software rules

The register window must be reachable in `connectivity`, like a target. C++
declares a target with `log_registers` and sets `Definition::error_logging`. A
log must watch at least one data target. No data target can be watched twice
across the logs. All logs must use the same register window.

A definition above the limit of logs, loggers, audit flags or register window
size produces `NCL_UNSUPPORTED_TOPOLOGY`. So does a bank that is
misaligned, overlaps another bank or does not fit the register window.

If software never clears a full logger with `wait_when_full` set, delivery has
no finite latency bound. The dependency graph records this assumption as
`env.logger_wait` with identifier `F6`.
