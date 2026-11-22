# AXI5 property table

The AMBA AXI Protocol Specification, IHI 0022 Issue L, lists 97 interface
properties in its property matrix (table B2.4). This page gives the class of
each property and the reason. [AXI5 interfaces](axi5.md) describes the fields
of the `axi5` object.

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
| MPAM_Support | AWMPAM and ARMPAM go with each packet of the request |
| MPAM_WIDTH | The width follows `mpam`. It is 11 bits for a 9-bit PARTID and 14 bits for a 12-bit PARTID |
| SUBSYSID_WIDTH | AWSUBSYSID and ARSUBSYSID go with each packet. A target port must hold the widest value that reaches it |
| NSAccess_Identifiers | AWNSAID and ARNSAID go with each packet of the request |
| PBHA_Support | AWPBHA and ARPBHA go with each packet of the request |
| USER_DATA_WIDTH | `data_user_width` gives WUSER and the data part of RUSER. Each data byte carries its own group of user bits. See [data side](axi5.md#data-side) |

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
| PROT_Present | `prot` selects the AxPROT ports. The three protection bits go with each packet |
| INSTPRIV_Present | `priv_inst` selects the AxPRIV and AxINST ports. They map to protection bits 0 and 2 |
| PAS_WIDTH | `pas_width` is 0 or 1. AxPAS bit 0 maps to protection bit 1. Wider values need RME or GDI, which are rejected |
| CACHE_Present | `cache` selects the AxCACHE ports. An initiator without them sends `0000` |
| QOS_Present | An initiator maps AxQOS to a scheduling level with `qos.axqos_map`. A target drives AxQOS from the level of the request with `axqos_levels` |
| REGION_Present | A target drives AxREGION from the address map window that the request hits. An initiator has no AxREGION port |
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
| Shareable_Transactions | `shareable` selects the AxDOMAIN ports. AxDOMAIN goes with each packet. An initiator admits only the transactions of the [legal set](ace5-lite.md#legal-transactions) |
| AWSNOOP_WIDTH | 0 or 4. The network carries a WriteUniqueFull while it stays one cache line, and gives other opcodes SLVERR. 5 bits only serve rejected opcodes |
| ARSNOOP_WIDTH | 0 or 4. The network admits only the read opcode `0000` |
| Cache_Line_Size | Only 64 bytes. The network checks the WriteUniqueFull shape against this size |

## Rejected properties

| Property | Reason |
| --- | --- |
| Poison | The network rejects this property |
| Trace_Signals | The network rejects this property |
| Unique_ID_Support | The network rejects this property |
| Loopback_Signals | The network rejects this property |
| LOOP_W_WIDTH | The property follows Loopback_Signals |
| LOOP_R_WIDTH | The property follows Loopback_Signals |
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
| `ace5_lite` | Accepted. See [ACE5-Lite interfaces](ace5-lite.md) |
| `axi5_lite` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `ace5_lite_dvm` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `ace5_lite_acp` | Refused with `NCL_UNSUPPORTED_PROTOCOL` |
| `ace5`, `ace`, `ace_lite` | Refused with `NCL_UNSUPPORTED_PROTOCOL`. Issue L does not describe these classes |

Issue L has no barrier transactions. No interface has AxBAR ports.
