# Scheduling

A scheduling level is a request priority from 0 to 7. A larger level has higher
priority. Outputs select the highest [pressure](#scheduling-pressure-rules),
then apply their [arbitration policy](arbitration.md). Reads and writes compete
at switch outputs.

A higher level cannot interrupt a [held grant](arbitration.md#arbitration).

## Fixed levels

An initiator sets `qos.levels.read` and `qos.levels.write`. Both default to
zero. Fixed levels do not read initiator AxQOS or drive target AxQOS. FIFO
arbitration retains request arrival order before the level filter. Continuous
higher-level traffic can starve lower levels.

There is no global wait bound.

Nonzero levels support the following networks. All-zero fixed levels without
other scheduling options use the ordinary protocol capability checks.

| Sources | Targets | Scheduling options |
| --- | --- | --- |
| AXI4 and AXI3 | Full AXI4 or AXI3 | Fixed levels, AxQOS maps, shapers and schedulers |
| AHB-Lite and AHB5, also alongside AXI4 and AXI3 | Full AXI4 or AXI3 | Fixed read and write levels from 0 to 7 |
| 32-bit APB | 32-bit APB | Fixed levels, shapers and schedulers |

Nonzero levels exclude AHB targets, other mixed protocol networks, targets
wider than the network, exclusives and target timeouts. AXI graphs also exclude
one-direction endpoints.
AHB sources have no AWQOS or ARQOS ports and cannot set `qos.axqos_map`.
They cannot set `qos.shaper` or `qos.scheduler`. AHB5 HMASTER identifies a
manager and does not select a scheduling level.

## AxQOS maps

An AXI4 or AXI3 initiator can set `qos.axqos_map`. Its 2, 4, 8 or 16 entries
select initiator AxQOS ports of 1, 2, 3 or 4 bits. Entry q is the level of a
burst whose AWQOS or ARQOS is q. Entries must be nondecreasing integers from 0
to 7. The initiator captures the level with the burst address.

A map cannot combine with fixed levels, a shaper or a scheduler on that
initiator. Initiators without a map have no AxQOS input ports. The generated map
file records scheduling configuration under
`definition.contract.arbitration.qos`. Its `ports` section lists physical ports.

## Bandwidth shapers

`qos.shaper` controls admission against an initiator byte budget.

| Field | Unit and range |
| --- | --- |
| `bytes_per_1024_cycles` | Charged bytes per 1024 initiator cycles, 0 to 65535 |
| `burst_bytes` | Burst budget in bytes, 0 to 65535 |

Transfers are charged in whole 16-byte units, rounded up. A started transfer
completes. After the initiator exceeds its budget, new transfers wait until it
recovers. The overshoot can include one transfer beyond the burst budget. Rate
zero stops admission after the budget is exceeded and never recovers.

A shaper supports a 32-bit APB3, APB4 or APB5 initiator, or an AXI4 or AXI3
initiator.
It can combine with fixed levels. The initiator's transport clock stays open
while it owes bytes.

Each AXI charge covers one whole burst packet under the
[accounting limits](#scheduling-profile-and-accounting). Connected targets
cannot set `read_side_effects: true`.

## Bandwidth schedulers

A `qos.scheduler` beside a shaper selects levels instead of blocking admission.

| Field | Contract |
| --- | --- |
| `under_level` | Level at or below the threshold, 0 to 7 |
| `over_level` | Level above the threshold, 0 to 7 |
| `threshold_percent` | Percentage of burst budget, 0 to 100 |

The transfer charge takes effect after its final response reaches the initiator.
A request retains its offered level until it enters the network. When the
initiator level rises, the network can raise the remaining work of its transfers
at the same target. The scheduler never blocks a request solely for bandwidth.

A scheduler requires a shaper and cannot combine with fixed levels or an AxQOS
map. AXI initiators can use packet and issue pipes with a scheduler. APB
initiators cannot use those scheduler pipes.

## Scheduling profile and accounting

Fixed levels do not arbitrate the initiator's own read/write merge. That merge
chooses by burst before network scheduling. Capacity and ordering waits can
delay higher levels. Split-burst packet boundaries permit preemption.

AXI initiators of any supported width can carry levels. AXI target data width
must equal the network width. An all-APB scheduling graph requires 32-bit
endpoints and no width adapters or target timeout.

| AXI shaper or scheduler | Requirement |
| --- | --- |
| Maximum INCR burst | Fits every connected packet window, capped at 4 KiB |
| Every packet boundary | At least 4 KiB |
| Excluded conversions | Narrow splitting, WRAP decomposition, width conversion |

Scheduler thresholds use whole 16-byte budget units. The transport region stays
awake while bytes are owed or scheduler work is in flight.

After a request head enters, its required level can only rise until completion.
Budget recovery and blocked requests can raise it. Upgrades create no target
data access.

Data and upgrades alternate at packet boundaries, initially preferring data.
Stalls retain that choice. Submitted upgrades precede new request heads on their
path.

A persistent upgrade needs at most one selection per outstanding-capacity entry,
plus one already submitted transmission. A cycle bound requires bounded
downstream service and packet length.

## Scheduling pressure rules

Pressure is the highest level of represented work, including blocked work. It
propagates through the stages below when any configured level exceeds zero.

| Place | Pressure represented |
| --- | --- |
| Source entry | Packet head level. A scheduler also includes its present level and highest waiting transfer level |
| Switch output | Highest pressure among inputs routed to that output, including unselected inputs |
| Pipe | Stored head level and upstream pressure held back by its ready signal. A forward stage registers the latter |
| Narrowing adapter | Input pressure |
| Widening adapter | Input pressure and held output head level |
| Crossing | Synchronized input pressure, every confirmed queued head level and the held output level |
| Target response | In-use context levels and, one cycle later, pressure of blocked requests or requests behind a stalled response |

A response captures its level when first offered and holds it until acceptance.
A request head leaving the arrival pipe occupies target capacity until its
response tail enters the response pipe. Reuse normally takes one cycle, except
immediately after another allocation.

Every confirmed queued head contributes, including heads behind lower-level
fragments. Newly confirmed work can affect the next receiving edge. Stalled
outputs retain their level after FIFO retirement. Pressure can decrease after
high-level work leaves. Permanent input pressure has no finite decay bound.

Map storage records identify shaper, scheduler, pressure and crossing resources
with their clocks.
