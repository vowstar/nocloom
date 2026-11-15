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

## Pipelined cycles

Set `cycles: pipelined` on each pipelined endpoint. Only these ports have
STALL. Requests are accepted on rising edges with CYC and STB high and
STALL low. Responses terminate requests in acceptance order. STB can be
low when ACK or ERR arrives. ERR ends one request and the sequence continues.

| Property | Initiator port | Target port |
| --- | --- | --- |
| Capacity | `max_outstanding`, 1 to 64, inherits `transport.max_outstanding` (default 1) | One accepted request |
| Full capacity | STALL is high | No new STB until the response is stored |
| Same-cycle response | The new request responds later. An older response can coincide with acceptance | ACK, ERR or enabled RTY can complete a request on its acceptance edge |
| Direction or destination change | The held request waits for earlier network responses to drain | Direction changes after the accepted request completes |
| Reset | STALL is high, ACK and ERR are low | CYC and STB are low |

The initiator port holds one request before the network accepts it.
`max_outstanding` counts this request and accepted requests in the network
whose responses have not completed. Responses have no backpressure.
Dropping STB inserts a request gap. Dropping CYC abandons responses and
blocks new requests until the accepted work drains.

A target timeout keeps CYC high until common reset and discards late responses.
A request already blocked by STALL remains stable until acceptance.
No further STB is issued. Timeout completion follows the target
[timer rules](timeout-and-progress.md#target-timeouts).

An external classic slave connects to a pipelined target through a wrapper.
The wrapper holds its request until termination, asserts STALL while busy and
returns one response per accepted request.
An external slave can connect directly if each accepted request produces one
response, including when STB is low.

## Registered feedback bursts

Both roles accept optional `burst_tags: true` in classic mode. Omission or
`false` omits CTI and BTE and uses the classic transfer rules. Other protocols
reject this field, including explicit `false`. Tags with `cycles: pipelined`
produce `NCL_WISHBONE_OPTION`.
A tagged target also rejects its own `timeout` with this code.
See [wishbone_registered_feedback.yaml][burst-example] for a complete
definition.

| Interface | Behavior with tags |
| --- | --- |
| Initiator | The port accepts every CTI and BTE value and ignores it. Each accepted word is a classic transfer and one network request at its supplied address. RULE 4.10 covers CTI 001 and reserved values |
| Target INCR | At least two consecutive full words form a linear burst. CTI is 010 before the last word and 111 on the last word. BTE is 00 |
| Target WRAP | Complete physical WRAP4, WRAP8 and WRAP16 with full SEL use BTE 01, 10 and 11. Nonfinal transfers have CTI 010. The final data transfer has CTI 111 |
| Target FIXED | With `fixed_bursts: single`, at least two 32-bit physical transfers with constant nonzero SEL use CTI 001 and a final 111. BTE is 00 |
| Other target shapes | Classic CTI 000 and BTE 00. A full-word INCR prefix can form a burst. Its remaining partial words stay classic. Zero-SEL write words make no bus access |

Burst tags apply after packet splitting and width conversion. Packets carry
at most 128 bytes. Narrow INCR and split WRAP packets also hold at most 32
physical words.

The target buffers complete write packets in two banks of 32 physical words.
The active write packet holds one bank until its result is stored. The other
bank can receive the next packet while the bus serves the active packet.
Reads do not use a write bank. The bus serves packets in arrival order,
one packet at a time.

Within a tagged burst, data and read response space allow the target to keep
STB high and complete one word per clock edge. A resource wait between words
lowers STB and keeps CYC high. A cycle with CYC and STB low separates bursts,
classic transfers and packets. Until a word terminates, the target holds
address, data, SEL, WE, CTI and BTE stable and keeps STB high. The target does
not accept a termination while STB is low.

ERR and enabled RTY end only the current word. Later words continue. Each tagged
burst ends with a data transfer with CTI 111. Failed reads return zero. Writes
merge the errors into one packet response and do not roll back completed writes.

RTY does not retry. Without a response from the slave, the transfer can wait
indefinitely.

The following example shows a three-word read burst. Signals are the values
before each rising edge. The network returns read response space at edge C.
The slave keeps ACK high in the gap, as permitted by RULE 4.15.

| Edge | CYC | STB | CTI | BTE | ACK | Result |
| --- | --- | --- | --- | --- | --- | --- |
| A | 1 | 1 | 010 | 00 | 1 | First word completes |
| B | 1 | 1 | 010 | 00 | 1 | Second word completes, read response slots become full |
| C | 1 | 0 | 010 | 00 | 1 | No bus transfer, a read response leaves |
| D | 1 | 1 | 111 | 00 | 1 | Third word completes |
| E | 0 | 0 | 111 | 00 | 1 | Boundary cycle, no bus transfer |

A slow slave keeps the current word active until ACK, ERR or enabled RTY.
When no read response slot is free, the target pauses between words.
Write packet collection can overlap the preceding packet, but a write packet
starts only after its last physical word has entered a bank.

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

Reads cover the smallest naturally aligned block containing all selected bytes.
SEL `0010` reads one byte. SEL `0110` or zero reads the whole word.
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
The admitted request still reaches the target and its side effects remain.
The port drains that response before it accepts another transfer.

ACK and ERR are registered values gated by CYC and, in classic mode, STB.
Dropping either qualifying input immediately clears both outputs.
A suppressed response is discarded and is not replayed when activity resumes.

Pipelined responses can arrive with STB low while CYC remains high.
The paths from CYC and classic STB to termination outputs are combinational.
Include these paths in integration timing checks.

## Targets

This port is a Wishbone MASTER. In classic mode, each target word uses one
transfer. STB stays high, with ADR, WE, SEL and write data stable, until ACK,
ERR or an enabled RTY is sampled on a rising edge. There are no combinational
paths from response inputs to outputs.

Without burst tags, STB then stays low for at least one cycle. CYC stays high
within a packet and falls when no work remains. For tagged targets, see
[registered feedback bursts](#registered-feedback-bursts).

A classic target accepts combinational ACK, registered ACK and held ACK,
as permitted by B4 RULE 3.55 and PERMISSION 3.30. Only a rising edge
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

A [timeout](timeout-and-progress.md#target-timeouts) holds classic STB and CYC.
Late termination is drained. The target stays failed until common reset.
Without timeout completion, an unresponsive device can stop progress.

## B4 options

The interface uses the [WISHBONE B4 specification][wishbone-spec]. The table
states the implemented subset. Rejected fields produce diagnostics before
any output is written.

| Option | Class | Reason or behavior |
| --- | --- | --- |
| Classic SINGLE read and write | Supported | One request and one termination per target word |
| Classic BLOCK read and write | Supported | Each phase is independent. Targets without tags insert STB gaps |
| Pipelined cycles and STALL | Supported | `cycles: pipelined`, ordered responses |
| Registered feedback bursts, CTI and BTE | Supported | `burst_tags: true` in classic mode. A tagged target rejects its own timeout. Types and fallback rules are listed above |
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
| TAG signals | Optional CTI and BTE | Optional CTI and BTE |
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
An omitted `cycles` means classic mode. Tagged endpoints record `burst_tags:
true`.

Tagged targets also record these values at
`definition.wishbone.targets.<name>.buffering` in the map and bundle.

| Field | Value | Meaning |
| --- | --- | --- |
| `write_banks` | 2 | Complete write packets can wait in two banks |
| `physical_words_per_bank` | 32 | Each bank holds at most 32 physical words, including zero-SEL words |
| `read_response_slots` | 2 | A bus read reserves space before STB is asserted |
| `active_packets` | 1 | One packet at a time owns the bus |

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
| Read transfer | One naturally aligned beat | One or more physical reads after width conversion | One SINGLE or shaped read |
| Write and SEL | One beat and WSTRB | PSTRB on APB4 | Transfer shaping for selected bytes |
| ACK | OKAY | PSLVERR low | OKAY |
| ERR or enabled RTY | SLVERR | PSLVERR high | ERROR |
| Source traffic to a Wishbone target | Burst words use the target cycle mode in packet order | No bursts, one transfer per requested word | Shaped accesses use the target cycle mode |
| Protection at a Wishbone source | PROT `010`, CACHE `0000` | PPROT `010` on APB4 | HPROT from the request attributes |
| Attributes at a Wishbone target | ID, CACHE, PROT, QoS and user are not bus signals | No protection signal | No HPROT signal |
| Exclusive or locked sequence | Only a network monitor can preserve AXI exclusive semantics | No atomic RMW | No atomic RMW |

[wishbone-spec]: https://opencores.org/howto/wishbone
[burst-example]: ../examples/wishbone_registered_feedback.yaml
