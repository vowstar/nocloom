# Protocols

Byte lanes use little-endian ordering. The manuals use full AXI for AXI4 and
AXI3, and Lite for AXI4-Lite.

Related manuals: [AHB-Lite and AHB5](ahb.md),
[APB and AXI4-Lite targets](dedicated-targets.md),
[Widths and AXI target interfaces](width-conversion.md),
[Attributes and user signals](user-signals.md).

## Protocol names and widths

| YAML value | Role | Data width in bits |
| --- | --- | --- |
| `axi4` | Initiator or target | 32, 64, 128, 256, 512, 1024 |
| `axi3` | Initiator or target | 32, 64, 128, 256, 512, 1024 |
| `axi4_lite` | Initiator or target | 32, 64 |
| `apb5` | Initiator or target | 8, 16, 32 |
| `apb4` | Initiator or target | 8, 16, 32 |
| `apb3` | Initiator or target | 8, 16, 32 |
| `apb2` | Target only | 8, 16, 32 |
| `ahb_v3` | AHB-Lite initiator or target | 32, 64, 128, 256, 512, 1024 |
| `ahb5` | AHB5 initiator or target with basic properties | 32, 64, 128, 256, 512, 1024 |

APB endpoints require a 32-bit network.

AHB targets require a 32-bit network. APB, AHB and AXI4-Lite targets support at
most 32 local address bits. When the global address width exceeds 32, set each
such target's `address_width` to 32 or less.

## Endpoint ports

Initiator bus ports use `nl_i_<name>_`. Target bus ports use `nl_t_<name>_`. The
map lists every port, width, direction and endpoint. A zero-width optional
signal has no port.

AXI endpoints can select `access: read_write`, `read_only` or `write_only`. The
default is `read_write`. A read-only AXI initiator or full AXI target omits AW,
W and B. A write-only AXI initiator or full AXI target omits AR and R. APB, AHB
and AXI4-Lite targets retain their signals but never access the refused
direction. APB and AHB initiators reject this option.

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
| `narrow_bursts` | `merge` or `split_non_modifiable`, default `merge` |
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

## APB5

APB5 uses APB4 transfer timing, PPROT and PSTRB. PAUSER carries request user
bits. PBUSER carries response user bits for both reads and writes. Configure
these ports as [user signals](user-signals.md#apb5-user-ports).

PWUSER, PRUSER, PWAKEUP, parity check signals and RME PNSE are unsupported.
These ports are absent. Data user width fields produce
`NCL_SCHEMA_UNKNOWN_FIELD`.

The protocol rules follow Arm AMBA APB IHI 0024E, sections 3.8 and 4.1.

## APB initiators

APB3 omits PSTRB and PPROT, uses full write strobes and supplies PPROT `010`.

It issues from SETUP and returns PREADY in ACCESS one cycle after the response
arrives. Every transfer has at least one wait state. The input address is
rounded down to the containing initiator word. PSTRB selects bytes within that
word.

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

The target's length capacity must hold one beat of every reaching initiator.

Malformed burst descriptors return SLVERR locally. Reads return the declared
zero-data beats with their original ID and RLAST. Writes consume the declared
beats before B. A WLAST mismatch marks B as SLVERR but does not change target
descriptor length. The AXI engine accepts W only after capturing AW.

The default W port pipe can accept one W beat before AW and then backpressures
later beats.

AXI3 targets receive AxLOCK `01` for exclusive accesses using their own monitor,
and `00` otherwise. Their WID repeats AWID in write order. Locked writes and
write interleaving are not generated. AXI has no AxREGION ports. Initiator AxQOS
exists only with an [AxQOS map](scheduling.md#axqos-maps), and target AxQOS is
absent.

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
