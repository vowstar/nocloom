# CDC timing constraints

## CDC constraints

Request this output with `--cdc-constraints`. It requires a clocked network
with at least one crossing. Otherwise `validate` and `generate` report
`NCL_UNSUPPORTED_BACKEND`.

The files describe crossing paths, synchronizer stages, slot-read delays and
related-clock transfer windows. Before reading the SDC, define one clock per
domain, named after that domain. Define a divided domain as a generated clock
with that name. Use nanoseconds. If an exception uses
`nocloom_period_<domain>` and the file gives no value, set it before reading
the SDC.

For asynchronous bounds, multiply the physical input period by the ratios of
dividers that always divide. A qualified level or a divider that can open
contributes a ratio of one. For an independent divider with a declared nominal
period, divide that value by its ratio to get the input period. When the input
period is unknown, supply the shortest possible period for each required domain.
The SDC timing bounds exclude [edge spacing promises](clocks-and-resets.md#domains).

If a related path has an unknown root period, set
`nocloom_root_period_<root>` to the input period in ns.

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

For known periods, the generator writes each upper bound as a decimal
number of nanoseconds rounded down to femtosecond precision.
The generator writes a period below one femtosecond as zero, and such a
definition has no practical use.
At 4294967296 ns, binary doubles are about one femtosecond apart.
Above this value, the generator rounds down to whole nanoseconds that a
binary double can represent exactly.

A mixed bound uses a known period and a supplied period.
For a mixed bound, the generator rounds the known period down when conversion
to a binary double increases it.

Asynchronous groups use `-allow_paths` so crossing paths remain timed. Do not
apply another asynchronous clock group without `-allow_paths` to these paths,
because it disables these delay exceptions. Related crossings stay timed rather
than grouped as asynchronous. Fast returned-count paths retain the additional
map obligation.

For fixed divisions with the same release phase, the related-path bound is
`T * gcd(R_sender, R_receiver)`. Edge times differ by integer combinations of
the two periods. Their greatest common divisor is the smallest positive such
difference.

An enable port, forced-open divider or different reset release can change the
spacing. Use `T * R_common` in this case. `R_common` is the ratio product through
the closest shared ancestor whose entire path from the root is fixed. Both
domains select edges from that ancestor. If there is no such domain, use the
physical root with `R_common = 1`. Test, ungate and synchronous reset can open
a divider.

A related log handover uses one physical root period as its maximum delay.
Its probe runs on every root edge even when a divider or enable selects fewer
edges. The generator rounds the bound down if conversion to a binary double
increases it. The SDC selects data and asynchronous reset pins separately
along the handover paths. A synchronous reset enters the data path.

The SDC creates no clocks, clock uncertainty, I/O delays or reset networks.

Apply the preserve list during synthesis. Disable merging and removal of
synchronizers, Gray counts and handshake announce registers. Disable boundary
optimization for synchronizers. Keep equivalent handshake registers separate.

The RTL has one synthesis attribute: `ASYNC_REG = "TRUE"` on the stage register
of the behavioral synchronizer model. The top module and the wrappers of bound
cells have no attributes.
