# Clocks and resets

## Domains

| Field | Meaning | Default or range |
| --- | --- | --- |
| `name` | Domain identifier | Required |
| `clock_port` | Input clock name. Use the physical root's port when given. | Required |
| `reset_port` | Declared active-low reset | Required |
| `reset_synchronizer` | Release reset through two registers on the domain's input clock | `true` |
| `test_port` | Declared gate test input | Required when the domain is gated |
| `period_ps` | Exact period or minimum edge spacing in whole picoseconds | Optional unquoted decimal integer. 100 to 576460752303423487 |
| `gated` | Enable domain gating | `true` |
| `ratio` | Use one of every `ratio` rising edges of the parent or input clock | Optional. At least 1 |
| `enable_port` | Use the rising edges marked by this input | Optional |
| `clock_root` | Explicit physical root. Equal periods do not establish a common root | Required when roots are declared |
| `edge_parent` | Parent domain in the same root. Parent relations must be acyclic. | Required with `ratio` or `enable_port` when roots are declared |
| `ungate_port` | Hold this domain's clock gates open | Optional |

Use `ratio` or `enable_port`, or omit both to use every input edge.
The 32-bit counter holds 0 to `ratio - 1`. The ratio product must fit in 64 bits.

A root has `name`, `clock_port` and optional `period_ps` (100 to 1000000000).
If omitted, declarations on fixed chains supply the root period and must agree.

| Where | Period rule |
| --- | --- |
| Fixed ratios only, with a known root period | Multiply the root period by the chain ratio. A declaration must equal this value. |
| Fixed ratios only, without an explicit root | Use the domain's own declaration if present. |
| Includes an enable port | A declaration promises a minimum spacing between successive edges. |
| Minimum for a promise | Take the largest of the known derived period and each ancestor's promise times the ratios below it. Use this minimum if the declaration is omitted. |
| Only a promise is known | Use the promise for planning. The physical root period stays unknown. |
| No period can be derived and none is declared | The domain period is unknown. |
| Maximum domain declaration | Divide the largest unsigned 64-bit integer by the maximum word size of 32 bytes. A derived period can reach this limit times the chain ratio. |

Crossing depths and link word widths use nominal periods and spacing promises.
Timing bounds use the shortest possible physical periods across clock modes.
See [clock domains](../examples/apb_domains.yaml), [clocked graphs](../examples/axi4_graph_clocked.yaml)
and the mixed protocol [SoC example](../examples/soc_mixed.yaml).

## Reset release

Each reset declaration has `name`. A domain uses two registers on its input
clock to release reset by default. Set `reset_synchronizer: false` for a
direct connection. The system must meet recovery and removal times against
the clock of each directly reset register.

Domains can share a reset port and choose synchronization separately.
If unrelated clocks share a direct reset port, the system must release reset
at a time that is safe for all of them.

AHB initiator address phase registers use their domain's `reset_port` directly.
With the reset synchronizer, deassert this port synchronously after a rising
HCLK edge. A direct connection follows the timing conditions above. HREADYOUT
is high during reset. The first address phase after the reset port deasserts
can be accepted. With the synchronizer, the data phase waits for domain reset
release.

`reset_coverage: all` resets payload and control registers. With `minimal`,
pipe and clock crossing payload registers retain their contents and occupancy
clears. After a common reset, a stored payload becomes valid after a new write.
Use `minimal` to reduce area or `all` to clear unused payload. Both modes clear
reset-handshake counters synchronously while handshake enable is low.

`output.register_reset: asynchronous` clears registers when reset goes low.
With `synchronous`, registers clear on rising edges while reset is low.
Every clock must run during synchronous reset. Clock gates pass their clocks
while their domains are in reset.

Common reset asserts every reset port together and cancels in-flight
transactions. Reset one domain alone only without boundary-crossing traffic. For
each crossing with a reset handshake, hold a one-sided reset low for at least
`n + 2` opposite-clock cycles. Use the synchronizer stage count for `n`.

Without a reset handshake, both sides must assert reset together. They can
release separately.

If a reset cuts a packet, reset every domain that packet touched together before
further use. Queued words of a cut packet can start an access or join a later
packet. A cut packet can hold its occupied output, link or target until that
common reset. Paths sharing none of those resources continue.

A complete target request can finish after its initiator resets. The initiator
drops a response with no waiting transaction.

## Clock gates

Top-level `clock_gating: false` disables automatic gates. With the default
`true`, each switch in a gated domain has one gate. A domain with
`gated: false` uses its domain clock for all switches and endpoints.

An endpoint's optional `clock_gating` object applies to all its flows.

| Field | Meaning | Default |
| --- | --- | --- |
| `protocol` | Gate the protocol zone | `true` |
| `transport` | Gate the transport zone | `true` |
| `single_gate` | Share one gate between both zones | `false` |

| `protocol` | `transport` | `single_gate` | Gates |
| --- | --- | --- | --- |
| `true` | `true` | `false` | One per zone |
| `true` | `true` | `true` | One for both zones |
| `false` | `true` | `false` | Transport only |
| `true` | `false` | `false` | Protocol only |
| `false` | `false` | `false` | None |

A zone without a gate uses the domain clock. A zone with no registers needs no
gate. The map reports the generated gate count. An explicit endpoint setting
requires top-level `clock_gating: true` and a domain with `gated: true`.
Invalid combinations produce `NCL_ENDPOINT_CLOCK_GATING`.

Synchronizers for crossings, reset handshakes, explicitly ungated pipes and
the exclusive monitor's free-running state use the domain clock.

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

After a target timeout, the target protocol zone keeps its clock gate open
until common reset. The status port `nl_t_<target>_timed_out` stays high for
the same time. A TL-UH target is an exception. Its status stays high, but its
gate can close when no work is pending. See
[TL-UH targets](tilelink-uh-targets.md#tl-uh-targets).

A high test or ungate input opens its gates.

## Asynchronous crossings

A crossing override contains `from_domain`, `to_domain`, optional `kind` and its
options. The default `kind` is `async`. An override applies to every crossing in
that direction between those two domains.

| Option | Contract |
| --- | --- |
| `depth` | Explicit FIFO depth, 2 to 10 |
| `synchronizer_stages` | 2 to 4. Inherits the top-level configuration, otherwise 2 |
| `reset_handshake` | Reset coordination. Defaults on when the network has several reset ports |

If either period is unknown, depth defaults to 6. The map records the depth and
its origin.

A stopped domain retains buffered words. The reset handshake blocks traffic
until both sides finish their reset exchange. A cut packet still needs common reset.

## Related clocks

Related crossings require one explicit root and one common reset port. Their
endpoints cannot have an enable port.

| `kind` | Options and behavior |
| --- | --- |
| `related` | Buffered credit crossing. `depth` is 2 to 4 |
| `related` with `fast: true` | Requires depth 2. Removes a local credit-return register |
| `edge_aligned` | Common-edge transfer. `slow_pipeline` defaults to `true` |

If either period is unknown, the `related` depth defaults to 3. An `edge_aligned`
crossing holds a transfer from the slow edge until the receiving edge accepts it.

## Timing constraints

Use `--cdc-constraints` to generate the [SDC and preserve list](timing-constraints.md#cdc-constraints).

## Reset timing

With synchronous reset, hold each reset port low for at least two rising edges
of every domain clock it resets. For a domain with `ratio`, count
divided-clock edges. With `enable_port`, count the edges the input selects.
One-sided resets also follow the duration in [reset release](#reset-release).
A stopped clock leaves registers unchanged, including unknown power-up values.

With the reset synchronizer, reset enters on the first input-clock edge and
clears domain registers on the next domain edge in synchronous mode. AHB
address phase registers use the direct reset timing in [reset release](#reset-release).
A directly driven reset clears on the first domain edge. Outputs can change
before those edges, even while the reset port is low.

With the reset synchronizer, a domain releases on its second input-clock rising
edge after the reset port rises. Endpoint ready and valid stay low until release.
AHB HREADYOUT is the exception. An AHB initiator drives it high while its reset
port is low. Its HRESP stays low until release.

For a domain with `ratio` or `enable_port`, reset resynchronization uses the
input clock. The domain logic uses the selected clock.

Traffic waits in or before the crossing until domain reset releases. An AHB
initiator can capture its first address phase during [reset release](#reset-release).
With `reset_coverage: all`, a selected word can acquire cleared payload during
the one-sided-reset synchronization window. With `minimal`, pipe and crossing
payloads retain their values while occupancy clears.

Related crossings reset their local and returned counts in both coverage modes
to prevent stale-word replay.

## Clock and binding validation

Clock and reset ports must be distinct legal Verilog identifiers without the
`nl_` or `nocloom_` prefix. Verilog-2001 keywords are rejected.
Unknown domain ports give `NCL_DOMAIN_REFERENCE`. Resets must serve a domain.

Domains can share reset, test, enable and ungate ports. Every enable port must
serve a domain. Unused test or ungate ports remain inputs without loads.

See [Technology cells](technology-cells.md) for binding rules and wrapper ports.

## Crossing depth and validation

For asynchronous crossings, let `Ta` and `Tb` be the sender and receiver
periods, and `n` the synchronizer stage count. The default FIFO depth is
`ceil((n + 1) * (Ta + Tb) / max(Ta, Tb))`. With equal aligned clocks, this is
`2n + 2` slots. A smaller depth `D` transfers `D` words per `2n + 2` cycles in
that case.

The default supports one word per slower-clock cycle. The receive register adds
one receiver cycle.

A period on a domain without a root needs a crossing. Unused crossing
overrides and synchronizer settings are rejected.
Same-domain and duplicate directed overrides are rejected.

An `async` override must set `depth`, `synchronizer_stages` or `reset_handshake`.
For `related` and `edge_aligned`, `kind` alone is sufficient. Each kind takes
its own options. A single-reset network rejects `reset_handshake: true`.

Every divided or qualified domain takes the root clock through one gate.
A divider's counter advances on its parent's selected edges. Test and ungate
inputs pass all parent edges, or all input edges for a domain without a parent.

Node clock bindings select a clocked graph. Each node then needs a domain,
and each declared domain must be used.

Without node bindings, the network requires one domain. Stopped clocks stall
their routes until participating clocks and consumers resume.

For `related`, the default depth with both periods is
`ceil(2 * (Ta + Tb) / max(Ta, Tb))`. For `edge_aligned`, one domain must be an
edge ancestor of the other. Intermediate domains can select edges with an
enable port.

A ratio of one still uses the adapter. The default slow-side pipe has two slots.
Time related paths and apply the map's obligations for fast returned counts.
