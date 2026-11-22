# Protocols

Byte lanes use little-endian ordering. The manuals use full AXI for AXI5,
ACE5-Lite, AXI4 and AXI3, and Lite for AXI4-Lite. An `axi5` or `ace5_lite`
endpoint follows the `axi4` rules of these manuals. [AXI5 interfaces](axi5.md)
gives its properties.

Related manuals: [AXI5](axi5.md), [ACE5-Lite](ace5-lite.md), [Wishbone](wishbone.md),
[TileLink](tilelink.md),
[AHB-Lite and AHB5](ahb.md),
[APB and AXI4-Lite targets](dedicated-targets.md),
[Widths and AXI target interfaces](width-conversion.md),
[Attributes and user signals](user-signals.md).

## Protocol names and widths

| YAML value | Role | Data width in bits |
| --- | --- | --- |
| `tl_ul` | Initiator or target | 32 |
| `tl_uh` | Initiator or target | 32 |
| `axi5` | Initiator or target with the properties of its `axi5` object | 32, 64, 128, 256, 512, 1024 |
| `ace5_lite` | Initiator or target with shareable transactions | 32, 64, 128, 256, 512, 1024 |
| `axi4` | Initiator or target | 32, 64, 128, 256, 512, 1024 |
| `axi3` | Initiator or target | 32, 64, 128, 256, 512, 1024 |
| `axi4_lite` | Initiator or target | 32, 64 |
| `apb5` | Initiator or target | 8, 16, 32 |
| `apb4` | Initiator or target | 8, 16, 32 |
| `apb3` | Initiator or target | 8, 16, 32 |
| `wishbone` | Initiator or target, classic or pipelined cycles | 32 |
| `apb2` | Target only | 8, 16, 32 |
| `ahb_lite` | AHB-Lite initiator or target | 32, 64, 128, 256, 512, 1024 |
| `ahb5` | AHB5 initiator or target with basic properties | 32, 64, 128, 256, 512, 1024 |

APB, Wishbone and TileLink endpoints require a 32-bit network.
Wishbone requires an explicit `address_unit: byte` or `word`. Optional
`burst_tags: true` adds CTI and BTE to classic endpoints. See
[Wishbone cycles](wishbone.md) for fields, signals, rejected combinations and
reset integration.

AHB targets require a 32-bit network. APB, AHB, Wishbone, TileLink and AXI4-Lite
targets support at most 32 local address bits. When the global address width
exceeds 32, set each such target's `address_width` to 32 or less.

## Endpoint ports

Initiator bus ports use `nl_i_<name>_`. Target bus ports use `nl_t_<name>_`. The
map lists every port, width, direction and endpoint. A zero-width optional
signal has no port.

AXI endpoints can select `access: read_write`, `read_only` or `write_only`. The
default is `read_write`. A read-only AXI initiator or full AXI target omits AW,
W and B. A write-only AXI initiator or full AXI target omits AR and R. APB, AHB,
Wishbone and AXI4-Lite targets retain their signals but never access the refused
direction. APB, AHB and Wishbone initiators reject this option.

A target refuses the direction it does not support without bus access. Refused
writes return one error after consuming W. Refused reads return zero data and an
error on every beat.

AHB5 supports four-bit or seven-bit HPROT, optional HNONSEC and HMASTER. See
[AHB5 basic properties](ahb.md#ahb5-basic-properties) for mappings and limits.

## AXI4 initiators

| Field | Contract |
| --- | --- |
| `id_width` | 0 to 16, default 4 |
| `len_width` | 0 to 8, default 4 |
| `max_outstanding` | 1 to 64 bursts, reads and writes combined |
| `narrow_bursts` | `merge` or `merge_modifiable`, default `merge` |
| `user_width`, `response_user_width` | Optional widths from 1 to 256 |

Unstrobed write bytes are zero at every target. Protocol and width conversion
preserve the [initiator ordering rules](transactions.md#initiator-capacity).

An exclusive burst follows
[exclusive access](exclusive-access.md#exclusive-access). A burst that violates
an endpoint conversion limit returns an error without unsupported target
transfers.

## AXI4-Lite initiators

Later writes can enter while earlier responses wait. See
[capacity and ordering](transactions.md#initiator-capacity) and
[width conversion](width-conversion.md).

## AXI3

| Signal or rule | Initiator | Target |
| --- | --- | --- |
| AxLEN | 0 to 4 bits, default 4 | Exactly 4 bits |
| AxLOCK | Two bits | Two bits |
| WID | Same width as AWID | Same width as AWID |
| Write interleaving | Not supported | Not produced |

AxLOCK `00` is normal and `01` is exclusive. An initiator burst with AxLOCK `10`
or `11` returns an error without target access. W beats follow AW order. A WID
mismatch still performs the write and marks its response as an error. An AXI3
target receives at most 16 target words per packet.

With `exclusive: target`, an exclusive access to an AXI3 target is
non-cacheable. After attribute mapping, the network clears AxCACHE bits 3 to 1
(write allocate, read allocate and cacheable) and keeps bit 0 (bufferable).
User flags in AxUSER still follow the initiator attributes.
Normal accesses keep their mapped attributes. With `exclusive: network`, the
target receives normal accesses and keeps their mapped attributes.

## APB5

APB5 uses APB4 transfer timing, PPROT and PSTRB. PAUSER carries request user
bits. PBUSER carries response user bits for both reads and writes. Configure
these ports as [user signals](user-signals.md#apb5-user-ports).

PWUSER and PRUSER carry 1 to 4 user bits per data byte when enabled.
PWAKEUP, parity check signals and RME PNSE are unsupported and have no ports.

The protocol rules follow Arm AMBA APB IHI 0024E, sections 3.8 and 4.1.

## APB initiators

APB3 has no PSTRB or PPROT ports. Writes select all bytes in the APB word.
Its network requests use PROT `010`.

An APB initiator issues from SETUP and returns PREADY in ACCESS one cycle after
the response arrives. Every transfer has at least one wait state. The input
address is rounded down to the containing initiator word. When present, PSTRB
selects bytes within that word.

A failed read returns zero data. PRDATA and PSLVERR retain the most recent
completed result between transfers. A completed write leaves PRDATA zero. After
reset, both values are zero. APB5 PBUSER is valid with PREADY in ACCESS.

APB initiators reject ID, length, access, narrow-burst and `id_table_entries`
configuration. APB2 initiators are unsupported.

## AXI port and admission details

A length width `w` permits `2^w` beats. Zero removes AxLEN and permits one beat,
with no WRAP. AXI initiator IDs zero-extend into the network and return at the
original width. Zero ID width removes ID ports. AXI targets need enough ID bits
for their flow and exclusive owner fields.

For a full AXI target, `2^len_width` target words must hold one full-width beat
of every connected initiator.

Malformed burst descriptors return SLVERR locally. Reads return the declared
zero-data beats with their original ID and RLAST. Writes consume the declared
beats before B. A WLAST mismatch marks B as SLVERR but does not change target
descriptor length. The AXI engine accepts W only after capturing AW.

The default W port pipe can accept one W beat before AW and then backpressures
later beats.

AXI3 targets receive AxLOCK `01` for exclusive accesses using their own monitor,
and `00` otherwise. Their WID repeats AWID in write order. Locked writes and
write interleaving are not generated. Initiator AxQOS exists only with an
[AxQOS map](scheduling.md#axqos-maps). AXI4, AXI5 and ACE5-Lite targets can
drive [AxREGION and AxQOS](axi5.md#target-axregion-and-axqos).
Initiators have no AxREGION port.

AXI4-Lite aligns addresses to its bus word before decode and admission, and
reads the whole word. Its network request uses full-width INCR with ID, CACHE
and LOCK zero, preserving PROT and WSTRB.

Common reset cancels a partially collected AW/W pair. An APB request similarly
has INCR, initiator-word size, ID, CACHE and LOCK zero, with PPROT carried as
PROT. APB reads ignore PSTRB.

## Related topics

See [Target attributes](user-signals.md#target-attributes).

See [Width conversion](width-conversion.md#width-conversion).

See
[APB and AXI4-Lite
targets](dedicated-targets.md#dedicated-apb-and-axi4-lite-targets).

See [Beat rates](width-conversion.md#beat-rates).

See [AHB-Lite initiators](ahb.md#ahb-lite-initiators).

## TileLink TL-UL

TL-UL supports single-beat Get and Put on A, with AccessAck and
AccessAckData on D. Initiators support multiple sources in flight. Targets
support one message in flight. See [TL-UL](tilelink-ul.md) for message and
error behavior and [TileLink](tilelink.md) for shared fields and progress.

## TileLink TL-UH initiators

TL-UH initiators accept multibeat Get and Put and complete Intent locally.
`max_transfer_bytes` is a power of two from 4 to 4096, with default 64.
`size_width` is 2 to 4, with default 3, and must encode the largest message.
Atomic operations return denied AccessAckData without network access.
Initiator `address_width` must be at least `log2(max_transfer_bytes)`.
See [TL-UH initiators](tilelink-uh-initiators.md#tl-uh-initiators).

## TL-UH targets

TL-UH targets use 32-bit data and 1 to 16 packet contexts shared between
reads and writes. `max_outstanding` defaults to 1. Message limits, size and
source widths, mapping alignment and rejected fields are in
[TL-UH targets](tilelink-uh-targets.md#tl-uh-targets).
