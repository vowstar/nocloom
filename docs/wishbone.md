# Wishbone cycles

Set `protocol: wishbone` on an initiator or target. Each endpoint requires
`address_unit: byte` or `word`. There is no default address unit.
`cycles` selects `classic` or `pipelined` and defaults to `classic`.

```yaml
initiators:
  - {name: cpu, clock_domain: main, protocol: wishbone, data_width: 32,
     address_unit: byte}
targets:
  - {name: peripheral, clock_domain: main, protocol: wishbone, data_width: 32,
     address_unit: word, mappings: [{base: '0x1000', size: '0x1000'}]}
```

## Initiators

An external Wishbone MASTER drives this port. The port is a Wishbone SLAVE.
In classic mode, a transfer starts with CYC and STB high.
It completes with one ACK or ERR pulse and keeps one transfer in flight.
Pipelined mode also requires STALL low to accept a request.

STB can remain high between completed transfers, and CYC can remain high
across independent transfers.
Neither signal reserves a network path or provides atomic read-modify-write.

Writes use SEL as byte strobes. A zero-SEL write enters the network with
zero strobes. AXI targets can receive a zero-strobe access, while APB and
Wishbone targets skip it.

Reads cover the smallest naturally aligned block that contains every selected
byte. For example, SEL `0010` reads one byte and
`0110` reads the whole word. SEL zero reads the whole word.
A read with disjoint selected bytes, or no selected bytes, can read additional
bytes with side effects. Returned bytes keep their positions in the 32-bit data
word.

With byte addressing, the port ignores ADR bits 1:0. SEL alone selects the
byte lanes. Word addressing has two fewer ADR wires and appends two zero
bits internally. `address_width` always counts byte-address bits. Initiators
accept 12 to 48 byte-address bits.

Requests use ID zero, LOCK zero, CACHE `0000` and PROT `010`. Decode misses,
target errors, refused directions and timeout completion produce ERR.
Failed reads return zero data. There is always an ERR output. A manager
without ERR_I must combine ERR with ACK externally. Ignoring ERR can leave
that manager waiting indefinitely.

In classic mode, dropping CYC or STB before completion abandons the response.
The admitted
request still reaches the target and its side effects remain. The port
drains that response before it accepts another transfer.

ACK and ERR are registered values gated by CYC and, in classic mode, STB.
Dropping either qualifying input immediately clears both outputs.
A suppressed response is discarded and is not replayed when activity resumes.

Pipelined responses can arrive with STB low while CYC remains high.
The paths from CYC and classic STB to termination outputs are combinational.
Include these paths in integration timing checks.

## Targets

This port is a Wishbone MASTER. In classic mode, each target word uses one
transfer. STB stays high, with ADR, WE, SEL and write data stable, until ACK,
ERR or an enabled RTY is sampled on a rising edge. It then stays low for at
least one cycle. CYC remains high within a packet and falls when no work
remains. There are no combinational paths from response inputs to outputs.

A classic target accepts combinational ACK, registered ACK and ACK that stays
high, as permitted by B4 RULE 3.55 and PERMISSION 3.30. Only a rising edge
with STB high completes a transfer. ERR marks that word as failed, and remaining
words continue. Completed side effects are not rolled back. A failed read word
returns zero data.

Write SEL carries byte strobes, including disjoint byte lanes. Zero-SEL
words make no bus access. Read SEL marks the requested bytes and is never
zero during a transfer. A device without SEL cannot preserve partial writes.
Such a device needs external handling of byte strobes.

Byte-address targets drive ADR bits 1:0 to zero. Word-address targets drive
the local byte address divided by four. Targets use 2 to 32 byte-address
bits, or 3 to 32 with word addressing. Mappings select and translate the
local address as described in [address maps](address-map.md).

`retry: error` adds an RTY input. RTY ends the word with an error response.
The default has no RTY pin. There is no automatic retry.

Packets contain at most 128 bytes. Longer AXI bursts split into packets,
and each packet uses its own CYC.

The target accepts `timeout`, `fixed_bursts`, `read_side_effects` and
`access`. Capacity and flow count are one. A refused direction makes no
bus access.

`fixed_bursts: reject` is the default. With `single`, each FIXED
beat uses a separate transfer at the same address. A FIXED beat wider than
the target fails.

A network exclusive monitor can serve AXI exclusive accesses.
`exclusive: target` is rejected.

A timeout uses the [target timer](timeout-and-progress.md#target-timeouts).
In classic mode, it does not withdraw an asserted STB or CYC.
A late termination is drained,
and the target remains failed until common reset. Without timeout completion,
an unresponsive device can stop progress.

## B4 options

The interface uses the [WISHBONE B4 specification][wishbone-spec]. The table
states the implemented subset. Rejected fields produce diagnostics before
any output is written.

| Option | Class | Reason or behavior |
| --- | --- | --- |
| Classic SINGLE read and write | Supported | One request and one termination per target word |
| Classic BLOCK read and write | Supported | Each phase is independent, with STB gaps at targets |
| Pipelined cycles and STALL | Supported | `cycles: pipelined`, ordered responses |
| Registered feedback bursts, CTI and BTE | Rejected | `burst_tags`, `cti` and `bte` are unknown fields. There are no tag pins |
| SEL | Supported | Four byte enables on writes, requested byte lanes on reads |
| ACK and wait states | Supported | Classic termination requires STB high. Pipelined termination completes the oldest accepted request |
| ERR | Supported | Word failure becomes an error response. Later words continue |
| Target RTY | Supported | Optional `retry: error` input maps to an error |
| Initiator RTY | Not applicable | No RTY output, each transfer returns ACK or ERR |
| Automatic retry | Rejected | `retry` accepts only `error` on targets |
| Target LOCK | Not applicable | No LOCK output, connect an external LOCK_I to zero |
| Initiator LOCK | Rejected | No lock pin, `lock` is an unknown field |
| Atomic RMW | Not applicable | A read and write in one CYC remain two independent operations |
| User TGA, TGD and TGC tags | Rejected | No user tag pins, tag fields are unknown |
| Address unit | Supported | Explicit `byte` or `word`, no default |
| 32-bit data | Supported | Four little-endian byte lanes |
| 8, 16, 64 or wider data | Rejected | `NCL_ENDPOINT_DATA_WIDTH` |
| 8-bit granularity | Supported | Each SEL bit selects one byte |
| Other granularity | Rejected | `granularity` is an unknown field |
| Little-endian data | Supported | SEL bit zero selects data bits 7:0 |
| Other byte order | Rejected | `endianness` is an unknown field. External lane conversion is required |
| Partial address decode | Supported | Mappings select local address windows |
| Cycle abort at an initiator | Supported | Immediately suppress termination and drain the response |
| Cycle abort at a target | Not applicable | An issued transfer waits for termination, including after timeout |
| SYSCON clock and reset outputs | Not applicable | Integration supplies both |

## DATASHEET

This table supplies the interface information of RULE 2.00 and RULE 2.15.

| Item | Initiator port | Target port |
| --- | --- | --- |
| Specification revision | B4 | B4 |
| Interface type | SLAVE | MASTER |
| Signal names | Signal table below | Signal table below |
| ERR | Decode, target or local failure, zero read data | Ends the word with an error, continues the packet |
| RTY | Absent | Optional input, handled as ERR |
| Termination timing | Registered ACK and ERR gated by CYC, and by STB in classic mode | Response inputs are sampled on rising edges |
| TAG signals | Absent | Absent |
| Port size | 32 bits | 32 bits |
| Port granularity | 8 bits | 8 bits |
| Maximum operand size | 32 bits | 32 bits per transfer |
| Data ordering | LITTLE ENDIAN | LITTLE ENDIAN |
| Transfer sequence | External address order | Packet address order, including WRAP order |
| CLK_I constraints | Endpoint clock domain and reset requirements below | Same requirements, including valid clock edges and clock source duty cycle |

## Signal names

Ports use `nl_i_<name>_` for initiators and `nl_t_<name>_` for targets.
The map lists their widths and directions. Wishbone signals are active high.
IP-XACT lists Wishbone signals as plain model ports without a bus abstraction.

The map and bundle record each address unit and enabled retry input under
`definition.wishbone`. Pipelined endpoints also record `cycles: pipelined`.
An omitted `cycles` means classic mode.

| Suffix | Direction at network initiator | Direction at network target |
| --- | --- | --- |
| `cyc` | Input from MASTER CYC_O | Output to SLAVE CYC_I |
| `stb` | Input from MASTER STB_O | Output to SLAVE STB_I |
| `we` | Input from MASTER WE_O | Output to SLAVE WE_I |
| `adr` | Input from MASTER ADR_O | Output to SLAVE ADR_I |
| `sel` | Input from MASTER SEL_O | Output to SLAVE SEL_I |
| `dat_w` | Input from MASTER DAT_O | Output to SLAVE DAT_I |
| `dat_r` | Output to MASTER DAT_I | Input from SLAVE DAT_O |
| `ack` | Output to MASTER ACK_I | Input from SLAVE ACK_O |
| `err` | Output to MASTER ERR_I | Input from SLAVE ERR_O |
| `rty` | Absent | Optional SLAVE RTY_O input |
| `stall` | Output to MASTER STALL_I, pipelined only | Input from SLAVE STALL_O, pipelined only |
| Domain `clock_port` | MASTER CLK_I | SLAVE CLK_I |
| Domain reset | External RST_I integration | External RST_I integration |

## Clocks and reset integration

There is no per-endpoint CLK or RST output. Connect the external device to
the endpoint's clock domain. Internal clock gates are not exported. With an
edge-qualified or divided domain, the external device must use the same valid
edges. The external clock source must satisfy B4 RULE 5.10, including its
40% to 60% duty cycle.

Network reset inputs are active low. Supply the external device with its
required high-active synchronous reset, as described by B4 RULE 3.00. The
external SLAVE must leave reset no later than the network domain.

Assert reset across the whole network together. Common reset cancels pending
work and drives target CYC and STB and initiator ACK and ERR low. Keep
synchronous network reset asserted for at least two rising edges. See
[clocks and resets](clocks-and-resets.md).

## Protocol mappings

| Wishbone behavior | AXI | APB | AHB-Lite |
| --- | --- | --- | --- |
| Read transfer | One naturally aligned beat | One or more physical reads after width conversion | One SINGLE or shaped read |
| Write and SEL | One beat and WSTRB | PSTRB on APB4 | Transfer shaping for selected bytes |
| ACK | OKAY | PSLVERR low | OKAY |
| ERR or enabled RTY | SLVERR | PSLVERR high | ERROR |
| Source traffic to a Wishbone target | Burst words use the target cycle mode in packet order | No bursts, one transfer per requested word | Shaped accesses use the target cycle mode |
| Protection at a Wishbone source | PROT `010`, CACHE `0000` | PPROT `010` on APB4 | HPROT from the request attributes |
| Attributes at a Wishbone target | ID, CACHE, PROT, QoS and user are not bus signals | No protection signal | No HPROT signal |
| Exclusive or locked sequence | Only a network monitor can preserve AXI exclusive semantics | No atomic RMW | No atomic RMW |

[wishbone-spec]: https://opencores.org/howto/wishbone
