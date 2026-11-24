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

## Stage lists

A stage list is an ordered list of pipes at one position of a switch link.
Put the stage lists of a link in its `stages`. The pipes follow data flow
order.

| Field | Values |
| --- | --- |
| `side` | `sender` or `receiver`. Required |
| `word_bytes` | 4, 8, 16 or 32. The word width at the position. Omitted means the word width of the sending switch for `sender` and of the receiving switch for `receiver` |
| `clock_domain` | Optional check. If set, it must equal the domain of the position |
| `pipes` | 1 to 64 pipe objects. Each sets `slice` and can set `bypass_port` and `ungated` |

Each side has a position where its chain of width adapters begins and after
each adapter. Positions on the sender side use the clock and the
[clock gate](clocks-and-resets.md#clock-gates) of the sending switch.
Positions on the receiver side use those of the receiving switch. The table
shows the order of elements on a link.

| Order | Element | Side |
| --- | --- | --- |
| 1 | `sender_pipeline` | Sender |
| 2 | Stage list at the word width of the sending switch | Sender |
| 3 | Each sender adapter, then the stage list at its output width | Sender |
| 4 | Clock crossing, if the switches are in different domains | Receiver |
| 5 | Stage list at the crossing word width | Receiver |
| 6 | Each receiver adapter, then the stage list at its output width | Receiver |
| 7 | `pipeline` | Receiver |

Take a request link from a 32-byte switch to a 16-byte switch with a 4-byte
link word. Its sender positions are at 32, 16, 8 and 4 bytes. Its receiver
positions are at 4, 8 and 16 bytes. A response link widens in one step, so
the same response link has receiver positions at 4 and 16 bytes. A pipe at a
narrow position stores fewer bits.

This example puts two pipes at the 4-byte sender position and one pipe at the
16-byte receiver position.

```yaml
links:
  - from: hub
    to: mem
    transport_word_bytes: 4
    stages:
      - {side: sender, word_bytes: 4, pipes: [{slice: backward}, {slice: forward}]}
      - {side: receiver, pipes: [{slice: full}]}
```

In C++, set `ExchangeLink::stages` to `LinkStages` values. A `LinkSide`
selects the side.

```cpp
link.stages = {{.side = nocloom::LinkSide::Sender,
                .word_bytes = 4,
                .pipes = {{nocloom::PipeSlice::Backward}, {nocloom::PipeSlice::Forward}}}};
```

| Condition | Diagnostic |
| --- | --- |
| `side` in YAML other than `sender` or `receiver` | `NCL_SCHEMA_TYPE` |
| A `LinkSide` value other than `Sender` or `Receiver`, or `word_bytes` other than 4, 8, 16 or 32 | `NCL_LINK_STAGE_POSITION` |
| No position of the side at `word_bytes`, or two lists at one position | `NCL_LINK_STAGE_POSITION` |
| `clock_domain` that is not the domain of the position | `NCL_LINK_STAGE_CLOCK` |
| No pipes, more than 64 pipes, or a pipe without `slice` | `NCL_LINK_STAGE_LIST` |

For a position that does not exist, the message lists the word widths of the
positions of that side. Node defaults and `response: mirror` do not copy
stage lists. A mirror `link_overrides` entry can set `stages`.

The map lists each link with stage lists in `network.link_stages`. The record
has `link`, the switch names `from` and `to`, and the word widths of the
positions of each side in `sender_words` and `receiver_words`. Its `stages`
list every element in data flow order.

| Field | Element | Meaning |
| --- | --- | --- |
| `name`, `kind` | All | Resource name. `kind` is `pipe`, `serializer`, `deserializer` or `crossing` |
| `side`, `clock_domain` | All | Side of the link and clock domain |
| `wide_bytes`, `narrow_bytes` | Adapter | Word widths of the adapter |
| `word_bytes`, `slice`, `slots` | Pipe | Word width, pipe mode and slot count |
| `bypass_port`, `ungated` | Pipe | Present when set |
| `origin` | Pipe | `sender_pipeline`, `pipeline` or `stages` |
| `list`, `index` | Listed pipe | Place of the stage list in `stages` and of the pipe in its list |

The [path report](path-report.md) lists the stages and cycles of every path
and proposes stage lists from delay estimates.

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
