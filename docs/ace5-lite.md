# ACE5-Lite interfaces

`protocol: ace5_lite` gives an initiator or a target an ACE5-Lite port. The
basis is the AMBA AXI Protocol Specification, IHI 0022 Issue L. That issue
defines ACE5-Lite as an AXI5 interface with constraints on its properties. An
`ace5_lite` endpoint has the fields of an `axi5` endpoint and takes the same
`axi5` object. See [AXI5 interfaces](axi5.md).

```yaml
transport: {data_width: 32}
initiators:
  - {name: gpu, clock_domain: main, protocol: ace5_lite, data_width: 128}
targets:
  - {name: hn, clock_domain: main, protocol: ace5_lite, data_width: 64,
     attributes: amba, mappings: [{base: '0x0', size: '0x40000000'}]}
```

An `axi5` endpoint can also have shareable transactions. Set `shareable: true`
in its `axi5` object. The ports and the rules on this page are the same for both
protocols. Only the defaults differ.

## Fields and constraints

| Field | `axi5` default | `ace5_lite` default | `ace5_lite` rule |
| --- | --- | --- | --- |
| `shareable` | `false` | `true` | Only `true` |
| `write_snoop_width` | 0 | 4 | 4 on an initiator. 0 or 4 on a target |
| `read_snoop_width` | 0 | 4 | 4 on an initiator. 0 or 4 on a target |
| `priv_inst` | `false` | `false` | Only `false` |
| `pas_width` | 0 | 0 | Only 0 |
| `prot` | `true` | `true` | `true` on a target. An initiator can have `false` |
| `cache_line_bytes` | 64 | 64 | Only 64 |

A value that breaks an `ace5_lite` rule gives `NCL_AXI5_PROPERTIES`. On both
protocols, `write_snoop_width` and `read_snoop_width` are 0 or 4.
`write_snoop_width: 5` and other values of `cache_line_bytes` give
`NCL_AXI5_UNSUPPORTED`. The other fields of the `axi5` object have the same
values and rules as on `axi5`. See [data side ports](axi5.md#data-side)
and [transaction byte limits](axi5.md#constraints).

## Ports

| Field | Initiator ports | Target ports |
| --- | --- | --- |
| `shareable: true` | AWDOMAIN and ARDOMAIN inputs, 2 bits | AWDOMAIN and ARDOMAIN outputs, 2 bits |
| `write_snoop_width: 4` | AWSNOOP input, 4 bits | AWSNOOP output, 4 bits |
| `read_snoop_width: 4` | ARSNOOP input, 4 bits | ARSNOOP output, 4 bits. Always `0000` |

No interface has AxBAR ports. Issue L has no barrier transactions.

## Legal transactions

An initiator admits the transactions in the table below. NSH is Non-shareable
(`00`), SH is Shareable (`01` or `10`) and SYS is System (`11`).

| Channel | Transaction | AxSNOOP | AxDOMAIN | Condition |
| --- | --- | --- | --- | --- |
| AR | ReadNoSnoop | `0000` | NSH, SYS | None |
| AR | ReadOnce | `0000` | SH | ARLOCK is 0, not FIXED |
| AR | ReadExclusive | `0000` | NSH, SYS | ARLOCK is 1 |
| AW | WriteNoSnoop | `0000` | NSH, SYS | None |
| AW | WriteUniquePtl | `0000` | SH | AWLOCK is 0, not FIXED |
| AW | WriteUniqueFull | `0001` | SH | AWLOCK is 0, not FIXED |
| AW | WriteExclusive | `0000` | NSH, SYS | AWLOCK is 1 |

The initiator answers every other request itself with SLVERR and does not send
it to a target. This includes every other AxSNOOP value, `0001` in NSH or SYS
(WriteNoSnoopFull), and an SH request with AxLOCK 1 or a FIXED burst. The
answer has the same form as for other refused bursts. A read gets its declared
number of beats with data 0. A write gives its B after the initiator takes all
of its beats. See [AXI port and admission details](protocols.md#axi-port-and-admission-details).

The network does not check AxDOMAIN against the memory type of AxCACHE. The
initiator must send a legal pair of table A8.7, for example SYS for a Device
request. A target with
AxDOMAIN ports and AxCACHE ports needs `attributes: amba`, so that the network
keeps the memory type of AxCACHE. Another mapping gives `NCL_TARGET_ATTRIBUTES`.

The initiator carries AxDOMAIN with each packet of the request. The value
reaches the target unchanged, and `01` and `10` stay apart. AxSNOOP needs one
bit in the network, because WriteUniqueFull is the only legal nonzero opcode.

## WriteUniqueFull

A WriteUniqueFull must be one Regular write of a full 64-byte cache line. The
network keeps the opcode only while the request has that shape.

| Place | Check | When the check fails |
| --- | --- | --- |
| Initiator | The address is aligned to 64 bytes. The beats are full width with 64 bytes in total, or one beat of 64 bytes on a wider port | The request travels as a WriteUniquePtl |
| Target | The packet still holds the whole line in full-width beats, or in one beat of 64 bytes on a wider port | AWSNOOP is `0000`, which is a WriteUniquePtl |

A WRAP burst that starts inside the line travels as a WriteUniquePtl. A packet
limit can split a line, and then each packet is a WriteUniquePtl. A
WriteUniquePtl to a Shareable location is always legal. It only loses the
hint that the whole line is written. The network does not check that all
write strobes are set. That is a rule for the initiator.

## Initiators and targets without the signals

| Case | Result |
| --- | --- |
| The initiator has no AxDOMAIN port | The domain comes from AxCACHE. AxCACHE[3:2] at `00` gives SYS (`11`). Other values give NSH (`00`). This follows the rule of Issue L, A8.3.4, for an interface without Domain signals. A note under table A8.5 says Shareable for Cacheable requests, and the network does not follow that note |
| The initiator has no AxSNOOP port | The opcode is `0000` |
| The target has no AxDOMAIN port | The target gets the request as a normal read or write. The domain and the WriteUniqueFull hint are not sent |
| The target has no AxSNOOP port | The target gets the domain. A WriteUniqueFull arrives as a WriteUniquePtl |

APB, AHB, Wishbone, TileLink, AXI3, AXI4 and AXI4-Lite initiators have no
AxDOMAIN port. Their requests follow the first row. An APB initiator sends
AxCACHE `0000`, so its requests are SYS.

A request without AxDOMAIN is never SH. An initiator that accesses memory that
other initiators mark as Shareable must have the AxDOMAIN ports. Otherwise
the coherent part of the system does not see its accesses as Shareable.

## Legal targets

A legal transaction can go to any target that the initiator reaches.

| Target | What it receives |
| --- | --- |
| `ace5_lite`, or `axi5` with `shareable: true` | AxDOMAIN and AxSNOOP as above |
| `axi5` without `shareable`, `axi4`, `axi3` | A normal read or write |
| `axi4_lite`, APB, AHB, Wishbone and TileLink | A normal read or write after the protocol conversion of the target |

## Refused definitions

| Definition | Diagnostic |
| --- | --- |
| `dvm`, `coherency_connection` | `NCL_AXI5_UNSUPPORTED` |
| `cmo_on_read`, `cmo_on_write`, `write_plus_cmo`, `persist_cmo`, `storage_cmo` | `NCL_AXI5_UNSUPPORTED` |
| `cache_stash`, `prefetch`, `write_zero`, `write_deferrable`, `write_no_snoop_full`, `invalidate_hint`, `deallocation`, `shareable_cache` | `NCL_AXI5_UNSUPPORTED` |
| `protocol: ace5_lite_dvm`, `ace5_lite_acp`, `ace5`, `ace` or `ace_lite` | `NCL_UNSUPPORTED_PROTOCOL` |

DVM messages and cache maintenance operations are therefore not available. The
specification gives CMO_On_Read the default True. The network has `false`. A
component whose CMO_On_Read is True must not send read channel cache
maintenance operations to the network. If it does, the initiator answers them
with SLVERR.

The [AXI5 property table](axi5-properties.md) gives the class of each property.
