# Wishbone cycles

This page covers Wishbone endpoint setup, signals and integration.

Set `protocol: wishbone` on an initiator or target. Each endpoint requires
`address_unit: byte` or `word`. There is no default address unit. Use 32-bit
data and a 32-bit network.
`cycles` selects `classic` or `pipelined` and defaults to `classic`.

```yaml
initiators:
  - {name: cpu, clock_domain: main, protocol: wishbone, data_width: 32,
     address_unit: byte}
targets:
  - {name: peripheral, clock_domain: main, protocol: wishbone, data_width: 32,
     address_unit: word, mappings: [{base: '0x1000', size: '0x1000'}]}
```

| Mode | Setup and behavior |
| --- | --- |
| Classic cycles | [Single transfers and classic packet timing](wishbone-classic.md) |
| Pipelined cycles | [STALL, request capacity and ordered responses](wishbone-pipelined.md) |
| Registered feedback bursts | [Classic endpoints with CTI and BTE](wishbone-registered-feedback.md) |

## Addresses and byte lanes

`address_width` counts byte-address bits. Initiators accept 12 to 48 bits, up
to the global width. Targets accept 2 to 32 bits with byte addressing or 3 to
32 bits with word addressing. [Mappings](address-map.md) select and translate
the local target address.

With byte addressing, an initiator ignores ADR bits 1:0. A target drives
those bits to zero. With word addressing, the port uses two fewer ADR wires.
An initiator appends two zero bits to get the byte address. A target divides
the local byte address by four.

Each 32-bit port has four little-endian byte lanes. Each SEL bit selects
one byte. SEL bit zero selects data bits 7:0.

Writes use SEL as byte strobes. A zero-SEL write enters the network with
zero strobes. AXI targets can receive a zero-strobe access, while APB and
Wishbone targets skip it.

Reads cover the smallest naturally aligned block containing all selected bytes.
SEL `0010` reads one byte. SEL `0110` or zero reads the whole word.
A read with disjoint selected bytes, or no selected bytes, can read additional
bytes with side effects. Returned bytes keep their positions in the 32-bit data
word.

Target write SEL carries byte strobes, including disjoint byte lanes.
Target read SEL marks the requested bytes and stays nonzero during a transfer.
A device without SEL needs external byte-strobe handling to preserve partial
writes.

## Responses and target options

Decode misses, target errors, refused directions and timeout completion
produce ERR at an initiator. Failed reads return zero data. There is always
an ERR output. A master without ERR_I must combine ERR with ACK externally.
Ignoring ERR can leave that master waiting indefinitely.

The target accepts `fixed_bursts`, `read_side_effects` and `access`. Configured
capacity and flow count are one. A refused direction makes no bus access.
Targets without burst tags also accept `timeout`.

A packet holds at most 128 bytes, or 32 full 4-byte target words. The
packetizer splits longer AXI bursts.

`fixed_bursts: reject` is the default. With `single`, each FIXED
beat uses a separate transfer at the same address. A FIXED beat wider than
the target fails.

A network exclusive monitor can serve AXI exclusive accesses.
`exclusive: target` is rejected.

`retry: error` adds an RTY input at a target. RTY ends the current word with
an error, as ERR does. By default the port has no RTY pin.

After a word failure, later words in the packet continue and completed side
effects remain. Failed read words return zero. Writes combine word errors in
the packet response.

## B4 options

The interface uses the [WISHBONE B4 specification][wishbone-spec]. The table
lists options that affect integration. Rejected fields produce diagnostics
before the generator writes output.

| Option | Class | Reason or behavior |
| --- | --- | --- |
| Classic SINGLE and BLOCK reads and writes | Supported | See [classic cycles](wishbone-classic.md) |
| Pipelined cycles and STALL | Supported | See [pipelined cycles](wishbone-pipelined.md) |
| Registered feedback bursts, CTI and BTE | Supported | See [registered feedback bursts](wishbone-registered-feedback.md) |
| SEL | Supported | See [byte lanes](#addresses-and-byte-lanes) |
| ACK and wait states | Supported | See [classic](wishbone-classic.md#targets) and [pipelined](wishbone-pipelined.md) timing |
| ERR and target RTY | Supported | See [responses and target options](#responses-and-target-options) |
| Initiator RTY | Not applicable | No RTY output, each transfer returns ACK or ERR |
| Automatic retry | Rejected | `retry` accepts only `error` on targets |
| Target LOCK | Not applicable | No LOCK output, connect an external LOCK_I to zero |
| Initiator LOCK | Rejected | No lock pin, `lock` is an unknown field |
| Atomic RMW | Not applicable | A read and write in one CYC remain two independent operations |
| User TGA, TGD and TGC tags | Rejected | No user tag pins, tag fields are unknown |
| Address unit | Supported | See [endpoint setup](#wishbone-cycles) |
| 32-bit data, 8-bit granularity and little-endian data | Supported | See [byte lanes](#addresses-and-byte-lanes) |
| 8, 16, 64 or wider data | Rejected | `NCL_ENDPOINT_DATA_WIDTH` |
| Other granularity | Rejected | `granularity` is an unknown field |
| Other byte order | Rejected | `endianness` is an unknown field. External lane conversion is required |
| Partial address decode | Supported | See [address mapping](#addresses-and-byte-lanes) |
| Cycle abort at an initiator | Supported | See [classic](wishbone-classic.md#initiators) and [pipelined](wishbone-pipelined.md) termination |
| Cycle abort at a target | Not applicable | An issued transfer waits for termination, including after timeout |
| SYSCON clock and reset outputs | Not applicable | Integration supplies both |

## DATASHEET

This table supplies the interface information of RULE 2.00 and RULE 2.15.

| Item | Initiator port | Target port |
| --- | --- | --- |
| Specification revision | B4 | B4 |
| Interface type | SLAVE | MASTER |
| Signal names | [Signal names](#signal-names) | [Signal names](#signal-names) |
| ERR | See [error responses](#responses-and-target-options) | See [error responses](#responses-and-target-options) |
| RTY | Absent | See [target RTY](#responses-and-target-options) |
| Termination timing | See [classic](wishbone-classic.md#initiators) and [pipelined](wishbone-pipelined.md) timing | Response inputs are sampled on rising edges |
| TAG signals | See [CTI and BTE](wishbone-registered-feedback.md) | See [CTI and BTE](wishbone-registered-feedback.md) |
| Port size | See [byte lanes](#addresses-and-byte-lanes) | See [byte lanes](#addresses-and-byte-lanes) |
| Port granularity | See [byte lanes](#addresses-and-byte-lanes) | See [byte lanes](#addresses-and-byte-lanes) |
| Maximum operand size | 32 bits | 32 bits per transfer |
| Data ordering | See [byte lanes](#addresses-and-byte-lanes) | See [byte lanes](#addresses-and-byte-lanes) |
| Transfer sequence | External address order | Packet address order, including WRAP order |
| CLK_I constraints | [Clocks and reset integration](#clocks-and-reset-integration) | [Clocks and reset integration](#clocks-and-reset-integration) |

## Signal names

Ports use `nl_i_<name>_` for initiators and `nl_t_<name>_` for targets.
The map lists their widths and directions. Wishbone signals are active high.
IP-XACT lists Wishbone signals as plain model ports without a bus abstraction.

The map and bundle record the address unit under `definition.wishbone`.
They also record `retry: error`, `cycles: pipelined` and `burst_tags: true`
where enabled. An omitted `cycles` means classic mode. See [tagged target
buffering](wishbone-registered-feedback.md#buffering) for the buffer fields.

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
| `cti` | Optional 3-bit input from MASTER CTI_O | Optional 3-bit output to SLAVE CTI_I |
| `bte` | Optional 2-bit input from MASTER BTE_O | Optional 2-bit output to SLAVE BTE_I |
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
| Read transfer | One naturally aligned beat before width conversion | One or more physical reads after width conversion | One SINGLE or shaped read |
| Write and SEL | One beat and WSTRB before width conversion | PSTRB on APB4 | Transfer shaping for selected bytes |
| ACK | OKAY | PSLVERR low | OKAY |
| ERR or enabled RTY | SLVERR | PSLVERR high | ERROR |
| Source traffic to a Wishbone target | Burst words use the target cycle mode in packet order | No bursts, one transfer per requested word | Shaped accesses use the target cycle mode |
| Protection at a Wishbone source | ID zero, LOCK zero, CACHE `0000` and PROT `010` | PPROT `010` on APB4 | HPROT from the request attributes |
| Attributes at a Wishbone target | ID, CACHE, PROT, QoS and user are not bus signals | No protection signal | No HPROT signal |
| Exclusive or locked sequence | Only a network monitor can preserve AXI exclusive semantics | No atomic RMW | No atomic RMW |

[wishbone-spec]: https://opencores.org/howto/wishbone
