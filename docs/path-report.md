# Path report

The path report lists the stages, clock domains and pipe cycles of every link
and every route path. With delay estimates and limits in `link_timing`, it
checks the paths of each estimated switch link and proposes
[stage lists](pipes.md#stage-lists) that meet the limits. The generator checks
a proposed plan like any other stage list before it returns it.

Related manuals: [Pipes](pipes.md), [Network](network.md).

## Request the report

| Interface | Request |
| --- | --- |
| CLI | `generate --path-report` |
| C++ | `GenerationOptions::path_report = true` |

The report is `nocloom_<name>.path_report.json`. The bundle lists it with role
`path_report`. A request for the report changes no other output file, and the
bundle changes only by that entry. The `link_timing` estimates never change
the hardware, the map or the dependency file. `validate --path-report`
checks the definition without writing a report.

When the plan leaves limits unmet, `generate` still writes the files. Both
commands print a line with the number of unmet limits.

## Estimates and limits

All times are integers in ps, from 0 to 1000000000. A period is at least 1.
An absent value is unknown. The example adds estimates to [the AXI4 graph](../examples/axi4_graph.yaml).

```yaml
link_timing:
  periods: [{clock_domain: main, period_ps: 1000}]
  stage: {launch_ps: 40, capture_ps: 30, forward_pass_ps: 20, ready_pass_ps: 10}
  links:
    - graph: request
      from: merge
      to: branch
      spans:
        - {side: sender, word_bytes: 32, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 16, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 8, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 4, forward_ps: 500, ready_ps: 500}
        - {side: receiver, word_bytes: 4, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: receiver, word_bytes: 8, forward_ps: 300, ready_ps: 300, turn_ps: 300}
      allowances:
        sender: {forward_ps: 300, ready_ps: 300, return_ps: 300}
        receiver: {forward_ps: 300, ready_ps: 300}
  latency_limits:
    - {initiator: s0, target: t0, request_cycles: 4}
```

| Field | Meaning |
| --- | --- |
| `periods` | The clock period of each listed domain. A domain without an entry takes its `clock_domains` `period_ps` in an explicitly clocked graph. Otherwise its period is unknown. An entry must match a `period_ps` that the domain declares |
| `stage` | The delays of a pipe. `launch_ps` is from its register to its output. `capture_ps` is from its input into its registers. `forward_pass_ps` and `ready_pass_ps` are through a pipe in a direction that it does not register |
| `links` | Estimates for switch links, at most one entry for each link. `graph` is `request` or `response`. `from` and `to` name the switches |
| `spans` | One entry for each span, named by the position where it starts. `side` and `word_bytes` name a position as in a stage list |
| `crossing` | `forward_ps` and `ready_ps` of the span from the clock crossing to the first receiver position |
| `allowances` | The time that a path can use in the link when it leaves or enters at a switch end |
| `latency_limits` | The most link pipe cycles that each path of an initiator and target pair can take, per direction. Each entry sets `request_cycles`, `response_cycles` or both, from 0 to 4096 |

A span runs from its position to the next point of the link: the next
position, the clock crossing or the receiving switch.

A span between two positions of one side passes one width adapter. Its
`turn_ps` is the delay from the forward signals at its start, through the
adapter, to its ready at its start. The span after the last sender position
is the link wire. It has no adapter and no `turn_ps`. No span follows the
last receiver position.

| Allowance | Paths |
| --- | --- |
| `sender.forward_ps` | A forward path that enters at the sending switch and ends at a register of the link |
| `sender.ready_ps` | A ready path that starts at a register of the link and leaves at the sending switch |
| `sender.return_ps` | A path that enters forward at the sending switch, turns in an adapter and leaves as ready at the sending switch |
| `receiver.forward_ps` | A forward path that starts at a register of the link and leaves at the receiving switch |
| `receiver.ready_ps` | A ready path that enters at the receiving switch and ends at a register of the link |
| `through.forward_ps`, `through.ready_ps` | A path that crosses the whole link |

A path that needs an absent allowance does not meet its limit. At each switch,
the allowances of its links and the delay of the switch must fit the period.
`link_timing` has no switch delay, so the report does not check this sum.

## Paths

Each pipe captures the forward and ready signals at its input. A pipe that
registers a direction starts that direction again. A pipe that does not
register a direction passes it on with its pass delay.

| Element | Forward | Ready |
| --- | --- | --- |
| `forward` pipe | Registers | Passes |
| `backward` pipe | Passes | Registers |
| `full` and `half` pipes | Registers | Registers |
| Pipe with `bypass_port` | Registers and passes | Like the pipe without it, except that a `half` pipe also passes ready |
| Asynchronous clock crossing | Registers | Registers |

A forward path runs in data flow order from a register or the sending switch
to each capture it reaches. A ready path runs the other way. A turn path runs
forward into an adapter and back out as its ready. A path with both ends in
the link must fit the period of its domain. Other paths must fit their
allowance.

The delays of a crossing belong to the spans next to it. The model has no
position inside a span, so a span whose delay alone does not fit the period
cannot be planned.

| Out of the model | Report |
| --- | --- |
| Clock crossings other than an asynchronous FIFO | The link timing status is `unknown`. The plan fails with `NCL_STAGE_PLAN_UNKNOWN` |
| Paths into clock gate enables | Not listed |
| Endpoint attachments | Listed with their stages and cycles, not planned |

## Plan

The planner adds at most one pipe at each position of each estimated link. It
adds a `forward`, `backward` or `full` pipe after the pipes that the position
already has. It never adds a `half` pipe or a bypass port. It takes the
placement that meets every bound of the link with the fewest pipe cycles. Ties
go to the fewest pipe bits, then to the largest least slack.

A pipe cycle is one pipe that registers the forward direction. The cycles of a
path are the sum over its links. The planner then checks each latency limit
against every path of its pair, with the planned pipes.

Before the plan is returned, the generator plans the changed definition again
and checks that each planned link gives the claimed paths.

| C++ | Result |
| --- | --- |
| `plan_link_stages(plan, sources)` | The definition with the planned pipes in its link `stages`, or the diagnostics below |

| Diagnostic | Field |
| --- | --- |
| `NCL_STAGE_PLAN_UNKNOWN` | An estimate that a planned link needs, a missing span entry, or a link with a crossing out of the model |
| `NCL_STAGE_PLAN_UNMET` | A period or allowance that a path misses with a `full` pipe at every position, or a latency limit that a path exceeds |

## Report fields

| Field | Content |
| --- | --- |
| `format`, `format_version` | `nocloom.path_report` and 1 |
| `module` | The network name |
| `links` | Every link of every graph once. Each has its end names and kinds, what it `carries`, its sender and receiver domains, its stages, its `pipe_cycles` per domain and its `bypass_cycles` |
| `links[].timing` | For a link with estimates: `delays` (its `link_timing` entry), `status` (`met`, `unmet` or `unknown`), the `missing` estimate fields and every path in `paths`. A link with a crossing out of the model has `unmodeled_crossing` instead of `missing` and `paths` |
| `paths` | Each route path once, by graph, `carries` and link names, with the flows that take it, its `pipe_cycles` per domain, its `bypass_cycles` and its `crossings` |
| `plan` | `status` (`none`, `met` or `unmet`), the planned `links` and each `unmet` diagnostic. A planned link has its complete `stages`, the `added` pipes, `pipe_cycles_added`, `bits_added` and its least `slack_ps` |

A `paths` entry of a link has `signal` (`forward`, `ready` or `turn`), `from`,
`to`, `via` for a turn, `delay_ps`, `limit`, `bound_ps` and `slack_ps`. An
unknown value is `null`. `bypass_cycles` counts the pipe cycles that a bypass
port can remove.

## Separate read and write paths

`carries` is `both` for a link or path of shared paths. With
[separate paths](read-write-paths.md) it is `writes` or `reads` for a link or
path of one direction. A configured link with separate paths is a write link
and a read link in the report.

One `link_timing` entry gives the estimates of both links, and the planner
adds the same pipes to both. `added` lists the pipes of each link, and
`bits_added` counts both. A latency limit applies to the write paths and to
the read paths of its pair.
