# AXI5 interfaces

`protocol: axi5` gives an initiator or a target an AXI5 port. The basis is the
AMBA AXI Protocol Specification, IHI 0022 Issue L. An `axi5` endpoint has the
fields and the ports of an `axi4` endpoint. Its `axi5` object declares the AXI5
properties of the port. Without the object, the endpoint takes the defaults and
the generated Verilog is the same as for `axi4`.

```yaml
initiators:
  - {name: cpu, clock_domain: main, protocol: axi5, data_width: 64,
     axi5: {max_transaction_bytes: 64}}
```

The `axi5` object on a protocol other than `axi5` and `ace5_lite` gives
`NCL_AXI5_PROPERTIES`. An `ace5_lite` endpoint takes the same object with other
defaults, see [ACE5-Lite interfaces](ace5-lite.md).

## Fields

Each field declares one or more properties of the specification. A value that
the network does not support gives `NCL_AXI5_UNSUPPORTED`. A value outside the
range gives `NCL_AXI5_PROPERTIES`.

| Field | Property | Values | Default |
| --- | --- | --- | --- |
| `mpam` | MPAM_Support, MPAM_WIDTH | `none`, `partid_9`, `partid_12` | `none` |
| `subsystem_id_width` | SUBSYSID_WIDTH | 0 to 8 | 0 |
| `subsystem_id` | None. The AxSUBSYSID value of an initiator without the port | 0 to 255 | None |
| `nsaid` | NSAccess_Identifiers | Boolean | `false` |
| `pbha` | PBHA_Support | Boolean | `false` |
| `trace` | Trace_Signals | Boolean | `false` |
| `poison` | Poison | Boolean | `false` |
| `unique_id` | Unique_ID_Support | Boolean | `false` |
| `write_loop_width` | Loopback_Signals, LOOP_W_WIDTH | 0 to 8 | 0 |
| `read_loop_width` | Loopback_Signals, LOOP_R_WIDTH | 0 to 8 | 0 |
| `prot` | PROT_Present | Boolean | `true` |
| `priv_inst` | INSTPRIV_Present | Boolean | `false` |
| `pas_width` | PAS_WIDTH | 0 to 3 | 0 |
| `cache` | CACHE_Present | Boolean | `true` |
| `shareable` | Shareable_Transactions | Boolean | `false`, `true` on `ace5_lite` |
| `write_snoop_width` | AWSNOOP_WIDTH | 0, 4 or 5 | 0, 4 on `ace5_lite` |
| `read_snoop_width` | ARSNOOP_WIDTH | 0 or 4 | 0, 4 on `ace5_lite` |
| `cache_line_bytes` | Cache_Line_Size | Power of two from 16 to 2048 | 64 |
| `consistent_decerr` | Consistent_DECERR | Boolean | `false` |
| `max_transaction_bytes` | Max_Transaction_Bytes | Power of two from 64 to 4096 | 4096 |
| `regular_only` | Regular_Transactions_Only | Boolean | `false` |
| `wrap_line_only` | Wrap_CLS_Modifiable | Boolean | `false` |
| `no_fixed_bursts` | Fixed_Burst_Disable | Boolean | `false` |
| `device_normal_independence` | Device_Normal_Independence | Boolean | `false` |
| `wakeup` | Wakeup_Signals | Boolean | `false` |
| `qos_accept` | QoS_Accept | Boolean | `false` |
| `busy` | Busy_Support | Boolean | `false` |
| `read_chunking` | Read_Data_Chunking, RCHUNKNUM_WIDTH, RCHUNKSTRB_WIDTH | Boolean | `false` |
| `mte` | MTE_Support | `none`, `basic`, `simplified`, `standard` | `none` |
| `interface_check` | Check_Type | `none`, `data`, `all` | `none` |
| `atomic` | Atomic_Transactions | Boolean | `false` |
| `untranslated` | Untranslated_Transactions, SID_WIDTH, SSID_WIDTH, SECSID_WIDTH, MMUFLOW_Present, UnstashTranslation_Transaction | `none`, `v1`, `v2`, `v3`, `v4` | `none` |
| `rme` | RME_Support | Boolean | `false` |
| `gdi` | GDI_Support | Boolean | `false` |
| `mec` | MEC_Support, MECID_WIDTH | Boolean | `false` |
| `cache_stash` | Cache_Stash_Transactions, STASHNID_Present, STASHLPID_Present | `none`, `basic`, `full` | `none` |
| `cmo_on_read` | CMO_On_Read | Boolean | `false` |
| `cmo_on_write` | CMO_On_Write, AWCMO_WIDTH | Boolean | `false` |
| `write_plus_cmo` | Write_Plus_CMO | Boolean | `false` |
| `persist_cmo` | Persist_CMO | Boolean | `false` |
| `storage_cmo` | Storage_CMO | Boolean | `false` |
| `prefetch` | Prefetch_Transaction | Boolean | `false` |
| `write_zero` | WriteZero_Transaction | Boolean | `false` |
| `write_deferrable` | WriteDeferrable_Transaction | Boolean | `false` |
| `write_no_snoop_full` | WriteNoSnoopFull_Transaction | Boolean | `false` |
| `invalidate_hint` | InvalidateHint_Transaction | Boolean | `false` |
| `deallocation` | DeAllocation_Transactions | Boolean | `false` |
| `shareable_cache` | Shareable_Cache_Support | Boolean | `false` |
| `dvm` | DVM_Message_Support, DVM_v8, DVM_v8.1, DVM_v8.4, DVM_v9.2 | `none`, `receiver` | `none` |
| `coherency_connection` | Coherency_Connection_Signals | Boolean | `false` |
| `flow_control` | AXI_Transport, Credit_Control, Num_RP_AR, Num_RP_AWW, Shared_Credits_AR, Shared_Credits_AW, Shared_Credits_W | `ready`, `credited` | `ready` |
| `act` | ACT_Support, ACT_R_WIDTH, ACT_W_WIDTH | Boolean | `false` |

`prot: true` excludes `priv_inst` and `pas_width` (Issue L, A4.5.1).
`shareable`, `write_snoop_width` and `read_snoop_width` add the AxDOMAIN and
AxSNOOP ports of [shareable transactions](ace5-lite.md).

## Request attributes

The request attributes go with each packet of a request. They keep their values
through width conversion, packet splitting, pipes and clock domain crossings.

| Field | Initiator ports | Target ports | Value at the target |
| --- | --- | --- | --- |
| `mpam` | AWMPAM and ARMPAM, 11 bits for `partid_9` and 14 bits for `partid_12` | The same | MPAM_NS, PARTID and PMG of the request. A 9-bit PARTID gets zeros above it in a 14-bit port |
| `subsystem_id_width` | AWSUBSYSID and ARSUBSYSID | The same | The subsystem ID of the request, with zeros above it |
| `subsystem_id` | None. The value of every request of the initiator | Not allowed | |
| `nsaid` | AWNSAID and ARNSAID, 4 bits | The same | The NSAID of the request |
| `pbha` | AWPBHA and ARPBHA, 4 bits | The same | The PBHA of the request |
| `prot: false` | No AWPROT and ARPROT | No AWPROT and ARPROT | |
| `priv_inst` | AWPRIV, AWINST, ARPRIV and ARINST | The same | AxPRIV is protection bit 0 and AxINST is bit 2 after the `attributes` mapping |
| `pas_width: 1` | AWPAS and ARPAS, 1 bit | The same | AxPAS is protection bit 1 after the `attributes` mapping |
| `cache: false` | No AWCACHE and ARCACHE | No AWCACHE and ARCACHE | |

The table gives the target values when the initiator has no attribute port.

| Initiator | NSAID, PBHA, subsystem ID | MPAM | Protection | AxCACHE |
| --- | --- | --- | --- | --- |
| Without the port | 0, or the `subsystem_id` value | MPAM_NS is AxPROT[1]. PARTID and PMG are 0 | `prot: false` without AxPRIV, AxINST or AxPAS gives 0 for those bits | 0 with `cache: false` |
| APB, AHB, Wishbone, TileLink, AXI4, AXI3, AXI4-Lite | 0 | MPAM_NS is AxPROT[1]. PARTID and PMG are 0 | Its protection | Its AxCACHE |

Each target port must hold the attributes of every initiator that reaches it.
A subsystem ID port narrower than the subsystem ID of such an initiator, or a
`partid_9` port that a `partid_12` initiator reaches, gives
`NCL_AXI5_PROPERTIES`. `subsystem_id` needs `subsystem_id_width: 0` on an
initiator. An `attributes` mapping of a signal that the target omits gives
`NCL_TARGET_ATTRIBUTES`.

## Data side

`data_user_width` gives an `axi5` endpoint WUSER and the data part of RUSER.
The width is the number of data bytes times `transport.data_user_bits_per_byte`,
as for APB5 (see [data user groups](user-signals.md#data-user-groups)). RUSER
holds the response user bits of `response_user_width` in its low bits and the
data user bits above them (Issue L, A12.5.3).

```yaml
transport: {data_user_bits_per_byte: 2}
initiators:
  - {name: cpu, clock_domain: main, protocol: axi5, data_width: 64,
     data_user_width: 16, response_user_width: 3}
```

This initiator has a 16-bit WUSER and a 19-bit RUSER. RUSER bits 0 to 2 are the
response user bits and bits 3 to 18 are the data user bits.

Each data byte has its own group of data user bits. Byte b uses bits `k*b` to
`k*b+k-1` of WUSER, or of the data part of RUSER, where `k` is
`transport.data_user_bits_per_byte`. The group stays with its byte through width
conversion, packet splitting, pipes and clock domain crossings.

| Case | Data user bits |
| --- | --- |
| A write byte with its WSTRB bit low | Zero at the target |
| A read transfer with SLVERR or DECERR | Zero |
| An initiator without the ports writes | Zero at the target |
| A target without the ports is read | Zero |
| A target without the ports is written | The network drops them |
| RUSER bytes outside the bytes of the read transfer | Not specified, as RDATA |

`poison: true` gives WPOISON and RPOISON with one bit for each 8 bytes of data
(Issue L, A16.1). A 32-bit port has one bit. The network carries the bits when
some initiator and some target have the ports.

Each data byte carries a poison bit. An initiator gives each strobed byte the
WPOISON bit of its 8-byte granule. A target gets the OR of the bits of the
strobed bytes of each granule of its write transfer.

Each byte of a target's read transfer takes the RPOISON bit of its granule.
An initiator gets the OR of the bits of the bytes in each granule of its read
word. A poisoned byte keeps its mark through every width conversion.

| Case | Poison bits |
| --- | --- |
| A granule of a write transfer without a strobed byte | Zero at the target |
| A read transfer with SLVERR or DECERR | Zero |
| An initiator without the ports writes | Zero at the target |
| A target without the ports is read | Zero |
| A target without the ports is written | The network drops them, so the target does not see that the data is poisoned |
| RPOISON bits of granules outside the bytes of the read transfer | Can be set, as Issue L permits |

An `axi5` endpoint with data user or poison ports is 32, 64 or 128 bits wide.
Other widths give `NCL_AXI5_DATA_WIDTH`. An APB5 endpoint and an `axi5`
endpoint exchange data user bits by the same rules.

## Response echoes

`unique_id` and the loop widths give an initiator values that come back with
the response of each request. The initiator stores them with the request and
returns them itself. The values do not travel to the target.

| Field | Initiator ports | Returned value |
| --- | --- | --- |
| `unique_id` | AWIDUNQ, ARIDUNQ, BIDUNQ and RIDUNQ | BIDUNQ is the AWIDUNQ of the write. RIDUNQ is the ARIDUNQ of the read, on every read transfer |
| `write_loop_width` | AWLOOP and BLOOP | BLOOP is the AWLOOP of the write |
| `read_loop_width` | ARLOOP and RLOOP | RLOOP is the ARLOOP of the read, on every read transfer |

The values return with every response, including a target response and a
local response for a decode miss or a refused burst. For a split request, the
values return in the single B response or R stream.

Each request in flight keeps its own values. Several requests can be in flight.
Their responses can return in any order that the AXI ordering rules allow.

An `axi5` target with these fields has the same ports. The network drives
AWIDUNQ, ARIDUNQ, AWLOOP and ARLOOP zero and does not read BIDUNQ, RIDUNQ,
BLOOP and RLOOP. A target ID can join requests of several initiators, so the
unique ID indicator of one initiator does not hold at the target. The network
does not use AWIDUNQ and ARIDUNQ for ordering.

## Target AxREGION and AxQOS

`axi4`, `axi5` and `ace5_lite` targets can drive AxREGION and AxQOS. The network
computes both values at the target, so they need no initiator port.

| Field | Place | Values | Result |
| --- | --- | --- | --- |
| `region_port` | Target | Boolean, default `false` | AWREGION and ARREGION ports of 4 bits |
| `region` | Each mapping of a target | 0 to 15, default 0 | The AxREGION of each request that the mapping decodes |
| `axqos_levels` | Target | 8 values from 0 to 15 that never decrease. Empty by default | AWQOS and ARQOS ports. Value L is the AxQOS of a request at scheduling level L |

Two mappings with different global windows can reach one local window. Their
regions tell the requests apart. A packet never crosses a mapping, so each
packet of a split request has the region of its own address.

The level of a request is its [scheduling level](scheduling.md) at the target.
It comes from a fixed level, an AxQOS map or a bandwidth scheduler. In a network
without scheduling levels, each request uses level 0 and drives
`axqos_levels[0]`.

A nonzero `region` without `region_port`, a region above 15 and
`region_port` on another protocol give `NCL_TARGET_REGION`. An initiator has no
AxREGION port, and `region_port` on an initiator gives `NCL_TARGET_REGION`.
`axqos_levels` that is not 8 values of 0 to 15 that never decrease gives
`NCL_QOS_MAP`.

## Constraints

Some properties are rules about the transactions. The validator checks them.

| Field | At an initiator | At a target |
| --- | --- | --- |
| `consistent_decerr` | The initiator returns DECERR on all read transfers of a 64-byte cache line or on none. Each target that it reaches must declare `consistent_decerr: true` or be a target whose errors the initiator receives as SLVERR. Each mapping must be at least 64 bytes. `slverr_only: true` also meets the property | The target declares that its subordinate meets the property |
| `device_normal_independence` | A declaration. The network keeps the order of one ID | Each initiator that reaches the target must declare `true` |
| `max_transaction_bytes` | A declaration. The network accepts every legal burst | Only 4096 |
| `regular_only`, `wrap_line_only`, `no_fixed_bursts` | A declaration | Only `false`. Packet splitting and burst merging change the shape of a burst, and an AXI target receives FIXED bursts |

APB, AHB, Wishbone and TileLink targets give SLVERR for their errors. AXI4,
AXI3 and AXI4-Lite targets can give DECERR. See
[error responses](address-map.md#error-responses).

The [AXI5 property table](axi5-properties.md) gives the class of each property of
the specification.
