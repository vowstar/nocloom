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

The `axi5` object on another protocol gives `NCL_AXI5_PROPERTIES`.

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
| `shareable` | Shareable_Transactions | Boolean | `false` |
| `write_snoop_width` | AWSNOOP_WIDTH | 0, 4 or 5 | 0 |
| `read_snoop_width` | ARSNOOP_WIDTH | 0 or 4 | 0 |
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

## Property table

The specification lists 97 interface properties in its property matrix (table
B2.4). This table gives the class of each property and the reason.

| Class | Meaning |
| --- | --- |
| Pass through | The network does not use the value. It carries the value from end to end by a fixed rule |
| Interpreted | The network uses the property, generates its value, or checks it as a constraint |
| Rejected | The network refuses a definition that enables the property |

A property passes through only when it has a fixed rule for width conversion,
packet splitting and clock domain crossings.

## Pass-through properties

| Property | Reason |
| --- | --- |
| USER_REQ_WIDTH | Each AWUSER or ARUSER bit can drive a named request flag. Each packet of a request carries the flags. See [user signals](user-signals.md) |
| USER_RESP_WIDTH | BUSER and RUSER use the response user field. A write that the network splits returns the BUSER of its last packet |

## Interpreted properties

| Property | Reason |
| --- | --- |
| ADDR_WIDTH | The network decodes the address. `address_width` sets the port width |
| DATA_WIDTH | `data_width` sets the port width from 32 to 1024 bits. The network converts between widths |
| ID_W_WIDTH | `id_width` sets one width for the write and read ID ports. The network uses the ID to order responses |
| ID_R_WIDTH | `id_width` sets one width for the write and read ID ports |
| LEN_Present | An initiator with `len_width: 0` has no AxLEN port. A target always has AxLEN, because the network sends bursts |
| SIZE_Present | AxSIZE is always present. The network converts transfer sizes between data widths |
| BURST_Present | AxBURST is always present. The network splits and merges bursts |
| WSTRB_Present | WSTRB is always present. The network uses the strobes to select bytes |
| WLAST_Present | WLAST is always present. An initiator checks it against the burst length |
| RLAST_Present | RLAST is always present. The network uses it to find the end of a read |
| BRESP_WIDTH | BRESP has 2 bits. The 3-bit codes belong to rejected properties |
| RRESP_WIDTH | RRESP has 2 bits. The 3-bit codes belong to rejected properties |
| PROT_Present | Only `prot: true`. The three protection bits go with each packet |
| CACHE_Present | Only `cache: true`. AxCACHE goes with each packet |
| QOS_Present | An initiator maps AxQOS to a scheduling level with `qos.axqos_map`. A target has no AxQOS port |
| Exclusive_Accesses | The target field `exclusive` selects how the network serves exclusive accesses |
| Consistent_DECERR | The validator checks each target that an initiator with `consistent_decerr: true` reaches |
| Max_Transaction_Bytes | An initiator can have any value. A target has 4096 |
| Regular_Transactions_Only | An initiator can have any value. A target must have `false`, because packet splitting changes the shape of a burst |
| Wrap_CLS_Modifiable | An initiator can have any value. A target must have `false`, because the network does not limit WRAP bursts to cache lines |
| Fixed_Burst_Disable | An initiator can have any value. A target must have `false`, because an AXI target receives FIXED bursts |
| Device_Normal_Independence | The network keeps the order of one ID. A target with `true` needs the same value at each initiator that reaches it |
| Ordered_Write_Observation | The property has no field. An initiator port of the network does not give this guarantee |
| Multi_Copy_Atomicity | The property has no field. The network has no cache, and each address reaches one target |
| Read_Interleaving_Disabled | The property has no field in the `axi5` object. A target sets `read_interleaving_disabled`. An initiator port can interleave the read data of different IDs |

## Rejected properties

| Property | Reason |
| --- | --- |
| USER_DATA_WIDTH | The network rejects this property. `data_user_width` applies to APB5 endpoints |
| Poison | The network rejects this property |
| Trace_Signals | The network rejects this property |
| MPAM_Support | The network rejects this property |
| MPAM_WIDTH | The property follows MPAM_Support |
| SUBSYSID_WIDTH | The network rejects this property |
| NSAccess_Identifiers | The network rejects this property |
| PBHA_Support | The network rejects this property |
| INSTPRIV_Present | The network rejects this property |
| PAS_WIDTH | The network rejects this property |
| REGION_Present | The network rejects this property. No port has AxREGION |
| Unique_ID_Support | The network rejects this property |
| Loopback_Signals | The network rejects this property |
| LOOP_W_WIDTH | The property follows Loopback_Signals |
| LOOP_R_WIDTH | The property follows Loopback_Signals |
| Shareable_Transactions | The network rejects this property |
| AWSNOOP_WIDTH | The network rejects this property |
| ARSNOOP_WIDTH | The network rejects this property |
| Cache_Line_Size | Only 64 bytes |
| Wakeup_Signals | The network has no clock or power control that uses AWAKEUP |
| QoS_Accept | The network has no function that uses VAWQOSACCEPT or VARQOSACCEPT |
| Busy_Support | The network has no rule to combine the BBUSY and RBUSY values of split packets |
| Read_Data_Chunking | Width conversion uses the order of the read transfers. Chunks can arrive in a different order |
| RCHUNKNUM_WIDTH | The property follows Read_Data_Chunking |
| RCHUNKSTRB_WIDTH | The property follows Read_Data_Chunking |
| MTE_Support | A tag belongs to 16 bytes of data, and a write can have two response transfers |
| Check_Type | The network has no parity check and no parity generation at its bus ports |
| Atomic_Transactions | Some atomic transactions return read data and a write response for one request |
| Untranslated_Transactions | The request carries a virtual address. The address map holds physical addresses |
| SID_WIDTH | The property follows Untranslated_Transactions |
| SSID_WIDTH | The property follows Untranslated_Transactions |
| SECSID_WIDTH | The property follows Untranslated_Transactions |
| MMUFLOW_Present | The property follows Untranslated_Transactions |
| UnstashTranslation_Transaction | The property follows Untranslated_Transactions |
| RME_Support | The request names more address spaces than the three protection bits hold |
| GDI_Support | The property needs the 3-bit AxPAS codes |
| MEC_Support | The network has no field for AxMECID |
| MECID_WIDTH | The property follows MEC_Support |
| Cache_Stash_Transactions | A stash request can have no data and can name a stash target |
| STASHNID_Present | The property follows Cache_Stash_Transactions |
| STASHLPID_Present | The property follows Cache_Stash_Transactions |
| CMO_On_Read | A cache maintenance operation carries no data |
| CMO_On_Write | A cache maintenance operation on the write channels carries no data |
| AWCMO_WIDTH | The property follows CMO_On_Write |
| Write_Plus_CMO | The request combines a write with a cache maintenance operation |
| Persist_CMO | The property adds a cache maintenance operation for persistent memory |
| Storage_CMO | The property adds a cache maintenance operation to the point of physical storage |
| Prefetch_Transaction | A Prefetch request has no data transfer |
| WriteZero_Transaction | The write must be one full cache line. Packet splitting can break that shape |
| WriteDeferrable_Transaction | The transaction needs the 3-bit response code and the 5-bit AWSNOOP |
| WriteNoSnoopFull_Transaction | The write must be one full cache line. Packet splitting can break that shape |
| InvalidateHint_Transaction | The transaction needs the 5-bit AWSNOOP |
| DeAllocation_Transactions | The legal transaction set has no deallocating read |
| Shareable_Cache_Support | The property needs the 3-bit read response code |
| DVM_Message_Support | The messages use the AC and CR channels. The network has no such channels |
| DVM_v8 | The property follows DVM_Message_Support |
| DVM_v8.1 | The property follows DVM_Message_Support |
| DVM_v8.4 | The property follows DVM_Message_Support |
| DVM_v9.2 | The property follows DVM_Message_Support |
| Coherency_Connection_Signals | The signals control the delivery of DVM messages |
| AXI_Transport | The network ports use VALID and READY flow control |
| Credit_Control | The property follows AXI_Transport |
| Num_RP_AR | The property follows AXI_Transport |
| Num_RP_AWW | The property follows AXI_Transport |
| Shared_Credits_AR | The property follows AXI_Transport |
| Shared_Credits_AW | The property follows AXI_Transport |
| Shared_Credits_W | The property follows AXI_Transport |
| ACT_Support | The network does not carry the AxACT values |
| ACT_R_WIDTH | The property follows ACT_Support |
| ACT_W_WIDTH | The property follows ACT_Support |

## Interface classes

| Protocol value | Result |
| --- | --- |
| `axi5` | Accepted |
| `ace5_lite` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `axi5_lite` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `ace5_lite_dvm` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `ace5_lite_acp` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |

Issue L has no barrier transactions. No interface has AxBAR ports.
