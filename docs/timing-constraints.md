# CDC timing constraints

## CDC constraints

This output requires a clocked network with at least one crossing. Otherwise
generation rejects the CDC request.

The files describe crossing paths, synchronizer stages, slot-read delays and
related-clock transfer windows. Before reading the SDC, define one clock per
domain, named after that domain. Define a divided domain as a generated clock
with that name. Use nanoseconds for time values. If a domain omits `period_ps`,
set `nocloom_period_<domain>` before reading the SDC.

Supply technology constraints separately.

## CDC path obligations

The generated file uses SDC 2.1 without tool-specific attributes. It expects
register cells named from the emitted register plus `_reg`, with vector-bit
suffixes. Synchronizers use wrapper stage paths or bound library instance paths
listed in the map. If synthesis changes these names, preserve the binding or
adapt the constraints to the surviving cells.

| Asynchronous crossing path | Maximum delay |
| --- | --- |
| Write or read Gray count to first opposite synchronizer stage | Smaller of sender and receiver periods |
| Payload slots to receive register | Receiver period times synchronizer stage count |
| Reset-handshake bit to opposite side | Period of the driving side |

Each path also receives minimum delay zero, ignoring clock latency. Gray
bit-to-bit skew must stay below both clock periods. Payload must settle within
write-count synchronization latency. Reset-release chains have no exception.
Check recovery and removal at their final stage.

The SDC records required reset hold durations in comments.

Asynchronous groups use `-allow_paths` so crossing paths remain timed. Do not
apply another asynchronous clock group without `-allow_paths` to these paths,
because it disables these delay exceptions. Related crossings stay timed rather
than grouped as asynchronous. Their payload and return paths use the smaller
clock period. Fast returned-count paths retain the additional map obligation.

The SDC creates no clocks, clock uncertainty, I/O delays or reset networks.

Apply the preserve list during synthesis. Disable merging and removal of
synchronizers, Gray counts and handshake announce registers. Disable boundary
optimization for synchronizers. Keep equivalent handshake registers separate.

The RTL has one synthesis attribute: `ASYNC_REG = "TRUE"` on the stage register
of the behavioral synchronizer model. The top module and the wrappers of bound
cells have no attributes.
