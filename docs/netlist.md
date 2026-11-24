# Netlist

`--netlist` adds `nocloom_<name>.netlist.txt`. It lists the hardware that
`nocloom_<name>.v` prints, one record per line. The bundle lists the file with
role `netlist`. The flag adds nothing else to the other files.
The [format](#format) section at the end lists the records.

## Lean check

The Lean checker in `formal/lean/NetlistCheck` reads the top module and the
netlist file. It reads the module as the Verilog-2001 subset that NocLoom
prints, and rejects any other text.

| Part of the module | The check |
| --- | --- |
| Text | Printing the parsed module gives back the text, byte for byte |
| Ports and declarations | Equal to the ports and values of the netlist |
| Continuous assignments | One per value and output, in the form of its operation. The theorem `check_sound` proves that each net and output port then carries the netlist value of the module inputs, registers and cell outputs |
| Registers, clock gates, synchronizer cells | Compared one by one with the netlist |

The check covers the top module and netlist. Cell files are outside its
scope. CTest runs `netlist_roundtrip` on every example. See the
[read refinement map](read-refinement.md) for the transport step checker.

## Format

Lines that start with `#` are license comments. The other lines are records,
in the order below. Ports, values and registers are numbered from 0 in line
order. Register k is the k-th value with operation `state`.

| Record | Meaning |
| --- | --- |
| `netlist 1` | Format version |
| `module <name>` | Top module name |
| `reset asynchronous` or `reset synchronous` | When registers clear |
| `port input <width> <name>` or `port output <width> <name>` | One port, in module order |
| `value <width> <operation> <fields>` | One value. It comes after the values it reads |
| `state <value> <next> <clock> [<reset> <reset value>]` | One register: output value, next value, clock and optional low-active reset |
| `driver <port> <value>` | The value of an output port |
| `synchronizer <value>` | A register that is a synchronizer stage |
| `end` | End of the records |

| Operation | Fields | Value |
| --- | --- | --- |
| `input` | port | The input port |
| `constant` | literal | The literal |
| `zero_extend` | a | a with zero high bits |
| `unsigned_less` | a b | 1 when a is less than b |
| `equal` | a b | 1 when a equals b |
| `bit_not`, `bit_and`, `bit_or` | a, or a b | The bitwise operation |
| `subtract` | a b | a minus b, modulo 2 to the power of the width |
| `select` | c a b | a when c is 1, else b |
| `slice` | a offset | `width` bits of a from bit L up, where L is the literal of constant value offset |
| `concat` | a b | a in the high bits, b in the low bits |
| `state` | register | The register output |
| `clock_gate` | clock enable test | The clock, gated by enable or test |
