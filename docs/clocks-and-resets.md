# Clocks and resets

Port names in reset, test, enable and clock declarations are used as written.

Related manuals: [Technology cells](technology-cells.md).

## Domains

| Field | Meaning | Default or range |
| --- | --- | --- |
| `name` | Domain identifier | Required |
| `clock_port` | Input clock name | Required |
| `reset_port` | Declared active-low reset | Required |
| `test_port` | Declared gate test input | Required when the domain is gated |
| `period_ps` | Clock period in picoseconds | Optional, 100 to 1000000000 |
| `gated` | Enable domain gating | `true` |
| `clock_type` | Clock source and edge selection | `direct` |
| `ratio` | Internal division ratio | Required for `internal_divider`, 1 to 64 |
| `enable_port` | Valid-edge input | Required for `edge_qualified` |
| `clock_root` | Explicit physical root | Required when roots are declared |
| `edge_parent` | Parent domain in the same root | Required for `internal_divider` and `edge_qualified` with explicit roots |
| `ungate_port` | Force clocks on | Optional. If one domain uses it, every domain must use one |

| `clock_type` | Input source | Valid domain edges |
| --- | --- | --- |
| `direct` | Input clock | Every input rising edge |
| `external_divider` | Clock divided outside the network | Every input rising edge, without an edge qualifier |
| `internal_divider` | Input clock | Every `ratio`-th rising edge, selected inside the network |
| `edge_qualified` | Input clock and `enable_port` | Input rising edges selected by `enable_port` |

For a changing clock, `period_ps` describes the shortest allowed period. Equal
periods do not establish a common physical root. A root declaration contains
`name`, `clock_port` and optional `period_ps`.

See [clock domains](../examples/apb_domains.yaml) and
[clocked graphs](../examples/axi4_graph_clocked.yaml). The
[SoC example](../examples/soc_mixed.yaml) combines AXI4, AHB-Lite, APB4 and
APB3 across two clock domains.

## Reset release

Each reset declaration has `name`, `release` and optional `release_domain`.

| `release` | System requirement and network behavior |
| --- | --- |
| `resynchronized` | Release passes through two registers in each domain. This is the default |
| `all_clocks_off` | The system releases reset while all affected clocks are stopped. The port drives registers directly |
| `on_clock_edge` | The system releases reset on a rising edge of `release_domain`. Only that domain uses this port |

`reset_coverage: all` resets data and control registers.
`reset_coverage: minimal` resets control state and leaves crossing payload
storage unchanged. Reset-handshake counters always clear synchronously while
handshake enable is low.

`output.register_reset: asynchronous` clears registers as soon as reset is low.
With `synchronous`, registers clear on rising clock edges while reset is low. In
synchronous mode, every clock must run during reset. Clock gates pass their
clocks while their domains are in reset.

Common reset asserts every reset port together and cancels in-flight
transactions. Reset one domain alone only without boundary-crossing traffic. For
each crossing with a reset handshake, hold a one-sided reset low for at least
`n + 2` opposite-clock cycles. Here, `n` is that crossing's synchronizer stage
count.

Without a reset handshake, both sides must assert reset together. They can
release separately.

If a reset cuts a packet, reset every domain that packet touched together before
further use. Queued words of a cut packet can start an access or join a later
packet. A cut packet can hold its occupied output, link or target until that
common reset. Paths sharing none of those resources continue.

A complete target request can finish after its initiator resets. The initiator
drops a response with no waiting transaction.

## Clock gates

Top-level `clock_gating: none` disables automatic gates.
`per_switch_and_endpoint` gives each gated switch one gate. A domain with
`gated: false` keeps its clock open.

Each initiator and target can set its own `clock_gating` field. The selection
covers all flows of that endpoint.

| Endpoint `clock_gating` | Gates per endpoint | Gated zones | Sleep and wake |
| --- | --- | --- | --- |
| `separate` | At most 2 | Protocol and transport, each with its own gate | Each side sleeps when idle and wakes for its incoming work |
| `shared` | 1 | Protocol and transport together | Both sides must be idle to sleep. Incoming work or activity on either side keeps the gate open |
| `transport_only` | At most 1 | Transport | The transport side sleeps when idle and wakes for incoming work. The protocol side always uses the domain clock |

A zone with no registers has no gate. The map reports the generated gate count.
The default is `separate`. Explicit endpoint modes require top-level
`per_switch_and_endpoint` and a domain with `gated: true`. An inactive gate
selection produces `NCL_ENDPOINT_CLOCK_GATING`, including explicit `separate`.

Synchronizers and crossing reset handshakes use the domain clock in every
mode. An internal divider or valid-edge qualifier still selects domain edges.
The protocol side of `transport_only` uses those edges without an automatic
endpoint gate. Explicitly ungated pipes and the exclusive monitor's free-running
state also use the domain clock.

A clock gate opens for incoming work and closes only while all its zones are
idle. A two-bit remaining-edge count keeps it open for three edges after reset
and two edges after sampled activity. Idle edges without a wake decrement the
count to zero. Incoming work requests the next complete domain-clock pulse.
The gated output passes a full pulse without a rising edge inside a high phase.
The map records this count in `network.clock_gates[].remaining`.

Source protocol logic wakes for bus requests and returned responses. Target
protocol logic wakes for network requests and target bus responses. An offered
response keeps its receiving region active while the consumer applies back
pressure.

A test or ungate input held high forces the relevant gates open. Those inputs do
not change transaction results.

## Asynchronous crossings

A crossing override contains `from_domain`, `to_domain`, optional `kind` and its
options. The default `kind` is `async`. An override applies to every crossing in
that direction between those two domains.

| Option | Contract |
| --- | --- |
| `depth` | Explicit FIFO depth, 2 to 10 |
| `synchronizer_stages` | 2 to 4. Inherits the top-level configuration, otherwise 2 |
| `reset_handshake` | Reset coordination. Defaults on when the network has several reset ports |

Without declared periods, the default depth is 6. The generated map records the
chosen depth and its origin.

A stopped domain retains its buffered words. A reset handshake blocks traffic
until both sides have completed their reset exchange. It does not recover a
packet that only one domain reset has cut.

## Related clocks

Related crossings require explicit roots, a fixed edge relationship and one
common reset port. Only `direct` and `internal_divider` domains support these
modes.

| `kind` | Options and behavior |
| --- | --- |
| `related` | Buffered credit crossing. `depth` is 2 to 4 |
| `related` with `fast: true` | Requires depth 2. Removes a local credit-return register |
| `edge_aligned` | Common-edge transfer. `slow_pipeline` defaults to `true` |

For `related`, the default depth is 3 when periods are unknown. `edge_aligned`
holds transfers from the declared slow edge until the receiving edge accepts
them.

## Timing constraints

Generate `--cdc-constraints` and apply the
[SDC and preserve list](timing-constraints.md#cdc-constraints).

## Reset timing

With synchronous reset, hold each reset port low for at least two rising edges
of every domain clock it resets. For an `internal_divider` domain, count
divided-clock edges. For an `edge_qualified` domain, count edges selected by its
enable input. One-sided resets must also meet the crossing duration in
[reset release](#reset-release). A
stopped clock leaves registers unchanged, including unknown power-up values.

A resynchronized reset enters on the first input-clock edge and clears domain
registers on the next domain edge in synchronous mode. A directly driven reset
clears on the first domain edge. Outputs can change before those edges, even
while the reset port is low. A resynchronized domain releases on its second
input-clock rising edge after the reset port rises. Until release, endpoint
ready and valid stay low.

For `internal_divider` and `edge_qualified`, reset resynchronization uses the
input clock. The domain logic uses the selected clock.

A domain still in reset accepts nothing. Traffic toward it waits in crossing
storage or ahead of it. With `reset_coverage: all`, a selected word can acquire
cleared payload during the one-sided-reset synchronization window. With
`minimal`, pipe and crossing payloads retain their values while occupancy
clears.

Related crossings reset their local and returned counts in both coverage modes
to prevent stale-word replay.

## Clock and binding validation

Clock and reset ports must be distinct legal Verilog identifiers without the
`nl_` or `nocloom_` prefix. Verilog keywords are rejected. Unknown domain ports
produce `NCL_DOMAIN_REFERENCE`. Every reset port must serve a domain.
`release_domain` is required only for `on_clock_edge` and rejected with other
release modes.

An `internal_divider` clock requires `ratio`. Other clock types reject it. Only
an `edge_qualified` clock accepts and requires `enable_port`. Every enable port
must serve a domain.
An unused test or ungate port is still an input without a load.

Several domains can share reset, test, enable and ungate ports.

Cell modules and pins must be legal identifiers without reserved prefixes. Roles
and `tie` cannot share pins. Bindings require distinct module names and must be
used. Tie values are 0 or 1. Unlisted input pins stay unconnected.

Wrappers preserve public ports. The map lists bound instance paths.

## Crossing depth and validation

For asynchronous crossings, let `Ta` and `Tb` be the sender and receiver
periods, and `n` the synchronizer stage count. The default FIFO depth is
`ceil((n + 1) * (Ta + Tb) / max(Ta, Tb))`. With equal aligned clocks, this is
`2n + 2` slots. A smaller depth `D` transfers `D` words per `2n + 2` cycles in
that case.

The default supports one word per slower-clock cycle. The receive register adds
one receiver cycle.

Without a pair override, periods must be present on both crossing domains or
neither. A period on only one side produces `NCL_CDC_PERIOD_PARTIAL`. An unused
period, unused override or unused global synchronizer setting is rejected.
Same-domain and duplicate directed overrides are rejected.

An `async` override must set `depth`, `synchronizer_stages` or
`reset_handshake`.
For `related` and `edge_aligned` overrides, setting `kind` alone is sufficient.
Omitted options use their defaults. Asynchronous options do not apply to related
crossings, and conversely.
A single-reset network rejects
`reset_handshake: true`.

Explicit roots require every domain to name its root and use that root's clock
port. `internal_divider` and `edge_qualified` domains name a same-root parent.
`direct` and `external_divider` domains have no parent. Parent relations must be
acyclic. Only node clock bindings select a
clocked graph. Every node then needs a domain, and every declared domain must be
used.

Without those bindings, the generated switches and an unclocked graph require
one domain. Stopped clocks stall their routes. Progress requires participating
clocks and consumers to resume.

For `related`, the default depth with both periods is
`ceil(2 * (Ta + Tb) / max(Ta, Tb))`. A partial period declaration requires an
explicit depth. For `edge_aligned`, one domain must be an ancestor of the
other through fixed integer division ratios.

A ratio of one still uses the adapter. The default slow-side pipe has two slots.
Related paths stay timed, and fast returned-count paths need the timing
obligations in the generated map.
