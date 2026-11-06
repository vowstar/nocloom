# Pipes

## Link pipes

A pipe object has Boolean fields `forward`, `backward`, `half_rate` and
`ungated`, plus an optional `bypass_port`. The Boolean fields default to
`false`.

| Setting | Effect |
| --- | --- |
| `forward` | Register valid and payload in one slot. Ready passes through |
| `backward` | Register ready. One slot retains a word if the downstream stalls |
| Both | Cut both directions with two slots |
| `half_rate` | Require both stages. Use one slot that cannot replace its word in the same cycle |
| `bypass_port` | When high, bypass the forward slot, or the single half-rate slot. Any backward slot stays active |
| `ungated` | Keep pipe storage clocked |

`sender_pipeline` sits at the sending switch before width conversion and clock
crossing. `pipeline` sits at the receiving switch after those adapters. An entry
pipe follows the endpoint-to-switch crossing. An exit pipe precedes conversion
to the endpoint word and its crossing. Both run on their switch clock.

A node can declare `output_links` and `input_links` defaults for link pipes. An
explicit link pipe replaces the whole inherited pipe. A pipe inherited from both
nodes is rejected. An unused node default is also rejected.

## Endpoint pipes

`pipes.issue` and `pipes.dispatch` are Boolean forward stages, enabled with
`true` and disabled by default.

| Initiator field | Position and restriction |
| --- | --- |
| `pipes.request` | AXI AW, W and AR ports. Backward defaults on. Both stages cannot be off |
| `pipes.packets` | After packet splitting and width conversion, per direction |
| `pipes.issue` | Forward stage after read/write merge and admission |
| `pipes.response` | AXI R and B ports. Forward defaults on and cannot be disabled |

| Target field | Position and restriction |
| --- | --- |
| `pipes.arrival` | Network exit |
| `pipes.dispatch` | Forward stage before the direction split |
| `pipes.response` | After response merge |
| `pipes.request` | AXI AW, W and AR ports. Forward defaults on and cannot be disabled |

Port pipes apply to the supported AXI profiles. AHB initiators reject request
and response port pipes. A target timeout excludes backward target request
pipes.

## Pipe operation

Set a bypass input before reset release and keep it unchanged during operation.
A bypass input requires a forward slot. Forward, backward and two-slot pipes
sustain one fragment per cycle. Forward and two-slot pipes add one cycle.

Half-rate pipes transfer once every two valid domain edges. No stages means
direct wiring. Pipe resets cancel occupancy without replay.

An ungated pipe uses the domain clock before its region gate, including any
division or enable qualification. It stops when the whole domain clock stops. An
occupied ordinary pipe keeps its region awake. An ungated pipe wakes its
consumer from its output valid.

Pipe slots do not change outstanding capacity or arbitration. Endpoint pipes
reject half-rate, bypass and ungated configuration with `NCL_ENDPOINT_PIPE`.

Port pipes belong to the protocol clock region. Other endpoint pipes belong to
its transport region. A burst in an AW or AR port slot is not admitted yet. Each
slot can hold an additional burst beyond admitted capacity. The W port can hold
one beat ahead of its AW.

The default backward request pipe registers AWREADY, WREADY and ARREADY. The
default forward response and target request stages are required endpoint
registers. If you write them explicitly, latency does not change.

A multi-packet write-only AXI target always has a backward B pipe, including
with a timeout. Its BREADY does not depend combinationally on BID. The watchdog
restarts when a B leaves that pipe. Non-AXI endpoints reject nondefault AXI port
pipes.

The map lists pipe place, owner, clock and physical slots separately from
outstanding capacity.
