# C headers

`--c-header` adds one address header per initiator. The library option
`GenerationOptions::c_header` defaults to `false`. Bundle role is `c_header`.
Headers contain only macros and compile as C11 or C++20. Each has an include
guard, generation source and output license.

| Header | Macro pattern |
| --- | --- |
| `<top>.<initiator>.addresses.h` | `<TOP>_<INITIATOR>_<TARGET>_WINDOW_<N>_BASE` and `_SIZE` |
| `<top>.error_logs.h` | `<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>_<BANK>_<FIELD>_OFFSET` |

`top` is the module name, including `nocloom_`. Names use uppercase ASCII.
Nonletters and nondigits become underscores. `N` starts at zero per initiator
and target pair in [address table order](outputs.md#address-table-format).
The first
`ram` window of `cpu` in `nocloom_soc` has `NOCLOOM_SOC_CPU_RAM_WINDOW_0_BASE`
and `NOCLOOM_SOC_CPU_RAM_WINDOW_0_SIZE`.

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

`REGISTERS` names the register target, `BANK` the map bank and `FIELD` the map
register field. Each `_OFFSET` pairs with the `_BASE` that has the same
`<TOP>_<INITIATOR>_<REGISTERS>_WINDOW_<N>` prefix. It is the register's byte
offset from that window's global base. Fields in one word have equal offsets.

For the mapping above, bank base `0x1000` and field offset `0x10` in map
`definition.log_registers` give `_OFFSET = (0x10U)`. Software adds `_BASE` and
`_OFFSET` to get `0x100010`.

Each reached window has its own offsets, only for fields within that window.
Window selection and numbering use the same address table rows as `_BASE`.
Smaller target mappings can split a register mapping into separate windows.
Each initiator gets its own macro prefix, including when their values match.
Unreachable or address-width-clipped fields have no macros. Error log targets
accept one unconditional mapping, so aliases and mode-dependent mappings are
rejected with `NCL_UNSUPPORTED_TOPOLOGY`.

For `nocloom_soc`, register target `csr`, two initiators and a smaller mapping
can give these pairs.
The register mapping is `base: "0x100000"`, `size: "0x10000"`, with
`local_address: "0x1000"`. Another target occupies `0x100800` through
`0x100fff`. Bank `csr` starts at local `0x1000`, and bank `fault_1` at `0x2000`.
The table uses `P = NOCLOOM_SOC` to keep macro names short.

| Base macro suffix after `P` | Value | Offset macro suffix after `P` | Value | Sum |
| --- | --- | --- | --- | --- |
| `_CPU_CSR_WINDOW_0_BASE` | `0x100000` | `_CPU_CSR_WINDOW_0_CSR_RECORD_VALID_OFFSET` | `0x10` | `0x100010` |
| `_CPU_CSR_WINDOW_1_BASE` | `0x101000` | `_CPU_CSR_WINDOW_1_FAULT_1_RECORD_VALID_OFFSET` | `0x10` | `0x101010` |
| `_DEBUG_CSR_WINDOW_0_BASE` | `0x100000` | `_DEBUG_CSR_WINDOW_0_CSR_RECORD_VALID_OFFSET` | `0x10` | `0x100010` |
| `_DEBUG_CSR_WINDOW_1_BASE` | `0x101000` | `_DEBUG_CSR_WINDOW_1_FAULT_1_RECORD_VALID_OFFSET` | `0x10` | `0x101010` |

No error log header is produced without error logs.

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
