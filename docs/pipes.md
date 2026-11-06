# Pipes

## Link pipes

A link or attachment pipe has an optional `slice`, an optional `bypass_port`
and a Boolean `ungated`. The `ungated` field defaults to `false`.

| `slice` | Slots | Effect |
| --- | --- | --- |
| Omitted | 0 | Direct wiring |
| `forward` | 1 | Register valid and payload. Ready passes through |
| `backward` | 1 | Register ready. The slot retains a word when the downstream stalls |
| `full` | 2 | Register both directions |
| `half` | 1 | Register both directions with one slot. The slot cannot replace its word in the same cycle |

Set `sender_pipeline` for the sending switch, before width conversion and clock
crossing. Set `pipeline` for the receiving switch, after those adapters.
An entry pipe follows the endpoint crossing. An exit pipe precedes width
conversion and crossing to the endpoint. These pipes use their switch clocks.

Node `output_links` and `input_links` can set default link pipes. A link's
explicit pipe replaces the entire default. Use `{}` for direct wiring on
that link. A link can inherit each pipe from only one node. Each node default
must serve a link.

## Endpoint pipes

| Initiator field | Position | Values | Default |
| --- | --- | --- | --- |
| `pipes.request` | AXI AW, W and AR ports | `backward`, `forward`, `full` | `backward` |
| `pipes.packets` | After packet splitting and width conversion, per direction | Omitted, `forward`, `backward`, `full` | Omitted |
| `pipes.issue` | Forward stage after read/write merge and admission | `true` or `false` | `false` |
| `pipes.response` | AXI R and B ports | `forward`, `full` | `forward` |

| Target field | Position | Values | Default |
| --- | --- | --- | --- |
| `pipes.arrival` | Network exit | Omitted, `forward`, `backward`, `full` | Omitted |
| `pipes.dispatch` | Forward stage before the direction split | `true` or `false` | `false` |
| `pipes.response` | After response merge | Omitted, `forward`, `backward`, `full` | Omitted |
| `pipes.request` | AXI AW, W and AR ports | `forward`, `full`. Only `forward` with a timeout | `forward` |

Set `slice` in a pipe object, for example `pipes: {request: {slice: full}}`.
Omission keeps the default for the position. Endpoint pipes reject
`slice: half`, a named `bypass_port` and `ungated: true` with
`NCL_ENDPOINT_PIPE`. Omission and `ungated: false` have the same effect.

Each pipe position is a boundary between stages of the endpoint. The
initiator splits and resizes packets, then merges directions and admits
bursts. The target takes packets from the network, splits them by direction,
and merges outgoing responses. Issue and dispatch are forward stages.

On an AXI initiator port, the default stages register AWREADY, WREADY,
ARREADY and R and B outputs. On an AXI target port, they register AW, W and
AR outputs. A `full` pipe also registers the inputs of its channel group.
Port pipe overrides require AXI4 or AXI3.

## Pipe operation

Use a nonempty `bypass_port` name with `slice: forward`, `full` or `half`.
An empty name gives `NCL_NAME_INVALID`. Set the input before reset
release and keep it fixed. When high, it bypasses the forward or half-rate
slot. Any backward slot stays active.

With bypass inactive and no back pressure, `forward`, `backward` and `full`
can pass a fragment each cycle. `forward` and `full` add one cycle.
`half` transfers once every two valid domain edges. Reset clears slot occupancy.

`ungated: true` requires `slice`. The pipe follows the domain clock, including
division or valid-edge selection. A stopped domain clock stops this pipe too.
A pipe on its region clock keeps that region awake while occupied. An ungated
pipe wakes its consumer through output valid.

Port pipes use the protocol zone's clock. Other endpoint pipes use the
transport zone's clock. Slots do not change outstanding capacity or arbitration.
AW and AR slots can each hold a burst before admission. A W port slot can hold
one beat ahead of its AW.

A write-only AXI target with multiple packets in flight has a backward B pipe.
BREADY then has no combinational dependency on BID. If the target has a
watchdog, it restarts when B leaves that pipe.

The map lists pipe position, owner, clock and physical slots separately from
outstanding capacity.
