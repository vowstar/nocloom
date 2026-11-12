# C headers

`--c-header` adds one address header per initiator. The library option
`GenerationOptions::c_header` defaults to `false`. Bundle role is `c_header`.
Headers contain only macros and compile as C11 or C++20. Each has an include
guard, generation source and output license.

| Header | Macro pattern |
| --- | --- |
| `<top>.<initiator>.addresses.h` | `<TOP>_<INITIATOR>_<TARGET>_WINDOW_<N>_BASE` and `_SIZE` |
| `<top>.error_logs.h` | The macros of the [error log header](#error-log-header) |

`top` is the module name, including `nocloom_`. Names use uppercase ASCII.
Nonletters and nondigits become underscores. `N` starts at zero per initiator
and target pair in [address table order](outputs.md#address-table-format).
An address window is one row of that table. The first `ram` window of `cpu` in
`nocloom_soc` has `NOCLOOM_SOC_CPU_RAM_WINDOW_0_BASE` and
`NOCLOOM_SOC_CPU_RAM_WINDOW_0_SIZE`.

Windows follow address table JSON selection. Aliases and separate pieces stay
distinct. Conditions and target flows can select windows at the same base.
Connectivity excludes unreachable flow pairs. Narrow initiators clip windows
to their address limits. Each pair has its complete table row as a comment.

Rows include local base, condition, flow, source paths and default-target
status when present. Conditions and flows state when a window applies. Macros
do not sample mode inputs or select flows. Default targets add no gap windows.

`_BASE` is the window's global byte address as seen by the initiator. `_SIZE` is
its byte size. Software accesses byte offset `d` at `_BASE + d`. The target
receives `local_base + d`, with `local_base` from the row comment. There is no
local-address macro.

For a mapping with `base: "0x100000"`, `size: "0x10000"` and
`local_address: "0x1000"`, `_BASE` is `(0x100000U)` and `_SIZE` is `(0x10000U)`.
At offset `0x10`, software uses `0x100010` and the target receives `0x1010`.

## Error log header

| Macro | Value |
| --- | --- |
| `<TOP>_ERROR_LOG_LAYOUT` | `(0x2U)` |
| `<TOP>_ERROR_LOG_BANK_BYTES` | `(0x40U)` |
| `<TOP>_ERROR_LOG_<FIELD>_OFFSET` | Byte offset of the field's word in a bank |
| `<TOP>_ERROR_LOG_<FIELD>_SHIFT` | Position of the field's lowest bit |
| `<TOP>_ERROR_LOG_<FIELD>_MASK` | Mask of the field in its word, at the field's position |
| `<TOP>_ERROR_LOG_CLASS_DECODE`, `<TOP>_ERROR_LOG_CLASS_TARGET` | `(0x1U)`, `(0x2U)` |
| `<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>_<BANK>_OFFSET` | Byte offset of the bank from the global base of that address window |
| `<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>_<BANK>_ID_VALUE` | Expected value of the whole word at `0x00` |
| `<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>_<BANK>_REVISION_VALUE` | Expected value of the whole word at `0x1c`. Only with `error_logging.revision` |
| `<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>_<BANK>_<FIELD>_OFFSET` | Byte offset of the field's word from the global base of that address window. Only for a bank that the address window cuts |

`FIELD` is the uppercase name of a field of
[register layout 2](error-log-registers.md#register-layout), for example
`RECORD_HELD`. The field `layout` has `<TOP>_ERROR_LOG_LAYOUT_OFFSET`,
`_LAYOUT_SHIFT` and `_LAYOUT_MASK`. Every bank has the same layout, so a header
has one set of field macros. Software computes `(word & MASK) >> SHIFT`. The
mask has the width of the field in layout 2, also when the field of this design
is narrower.

`REGISTERS` is the name of the register window of
[error logging](error-logging.md). `BANK` is the bank name in
`definition.log_registers` of the map. The
[configuration](error-logging.md#configuration) of error logging lists the
bank names. Each bank macro pairs with the `_BASE` that has the same
`<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>` prefix.

Software reaches a field at `_BASE + _<BANK>_OFFSET + _<FIELD>_OFFSET`. A
driver built with this header can compare the word at `0x00` of a bank with
`_ID_VALUE`. See the [driver rules](error-log-registers.md#driver-rules).

For a register window with `base: "0x100000"`, `size: "0x10000"` and
`local_address: "0x1000"`, a bank at local `0x1040` has
`_<BANK>_OFFSET = (0x40U)`. `RECORD_HELD` is in the word at `0x04`, so
`_ERROR_LOG_RECORD_HELD_OFFSET` is `(0x4U)`. Software adds `_BASE` and both
offsets to get `0x100044`.

Each reached address window has its own bank macros. A bank whose 64 bytes all
lie in one address window gets `_OFFSET` and `_ID_VALUE`. A bank that the
address window cuts gets one `_<BANK>_<FIELD>_OFFSET` for each of its fields
that the address window reaches. Address window selection and
numbering use the same address table rows as `_BASE`. A smaller mapping of
another target can split the register window into more than one address window.

Each initiator gets its own macro prefix. A comment names each bank whose
`STATE` word no initiator can reach.

The register window accepts one unconditional mapping. Aliases of the register
window and conditional mappings in a network with error logs are rejected with
`NCL_UNSUPPORTED_TOPOLOGY`.

For `nocloom_soc`, register window `csr`, two initiators and a smaller mapping
can give these pairs. The register mapping is `base: "0x100000"`,
`size: "0x10000"`, with `local_address: "0x1000"`. Another target occupies
`0x100800` through `0x100fff`.

The first log is `fault`. Bank `csr` is its first logger and starts at local
`0x1000`. Bank `fault_1` is its second logger and starts at `0x2040`. The table
uses `P = NOCLOOM_SOC` to keep macro names short.
`P_ERROR_LOG_RECORD_HELD_OFFSET` is `0x4`.

| Base macro suffix after `P` | Value | Bank macro suffix after `P` | Value | Address of `RECORD_HELD` |
| --- | --- | --- | --- | --- |
| `_CPU_CSR_WINDOW_0_BASE` | `0x100000` | `_CPU_CSR_WINDOW_0_CSR_OFFSET` | `0x0` | `0x100004` |
| `_CPU_CSR_WINDOW_1_BASE` | `0x101000` | `_CPU_CSR_WINDOW_1_FAULT_1_OFFSET` | `0x40` | `0x101044` |
| `_DEBUG_CSR_WINDOW_0_BASE` | `0x100000` | `_DEBUG_CSR_WINDOW_0_CSR_OFFSET` | `0x0` | `0x100004` |
| `_DEBUG_CSR_WINDOW_1_BASE` | `0x101000` | `_DEBUG_CSR_WINDOW_1_FAULT_1_OFFSET` | `0x40` | `0x101044` |

No error log header is produced without error logs.

## Names and constants

Include guards use `<TOP>_<INITIATOR>_ADDRESS_H` and `<TOP>_ERROR_LOGS_H`.
All macro and guard names share one namespace across the generated headers.
A collision produces `NCL_C_HEADER_NAME` and generation returns no artifacts.
Consecutive underscores or an initial underscore reserve a generated uppercase
name and produce `NCL_C_HEADER_RESERVED` with no artifacts. This includes a
final underscore joined to a separator, such as target `ram_`. Names are
checked only when C headers are enabled.

Hexadecimal constants use parentheses, with `U` through `0xffffffff` and `ULL`
above it, including full 32-bit sizes and 48-bit addresses. Headers contain no
generator version or timestamps.
