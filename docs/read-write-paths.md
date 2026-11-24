# Read and write paths

By default (`read_write: shared`) reads and writes share the request graph and
the response graph of the [network](network.md). With `read_write: separate`
and no local overrides, NocLoom builds the topology twice. Write paths carry
write requests and B responses. Read paths carry read requests and R responses.

The write paths and the read paths each have their own switches, output
arbiters, links, link pipes, width adapters and clock crossings. Both take the
node and link settings, the arbitration overrides and the routes of the one
topology. A graph of the write paths carries no read, and a graph of the read
paths carries no write.

| Hardware node | Shared paths | Write paths | Read paths |
| --- | --- | --- | --- |
| Request switch | `req_node<k>` | `wreq_node<k>` | `rreq_node<k>` |
| Response switch | `rsp_node<k>` | `wrsp_node<k>` | `rrsp_node<k>` |

## Endpoint attachment

| Endpoint | Attachment |
| --- | --- |
| Initiator | Writes enter the write request graph and reads the read request graph. A started write does not hold back a read of the same initiator. Responses arrive on the response graph of their direction |
| [full AXI](protocols.md) target | Each request graph has its own request input into the target adapter. The two directions share the target capacity. B leaves on the write response graph and R on the read response graph |
| APB, AHB or AXI4-Lite target | A join at the target takes the two request graphs into one request input. The join is a packet arbiter. It takes the graphs by turns, a read first after reset, and keeps a packet from its first to its last fragment. The target then serves the packets in arrival order, as with shared paths. B and R leave on their own response graphs |

## Capacity

An initiator's `max_outstanding` still counts reads and writes together, as in
[initiator capacity](transactions.md#initiator-capacity). In one cycle at most
one packet of an initiator takes an entry. A packet keeps its turn from its
offer until it moves on. While a read cannot move on, for example at a target
that holds AR, the next write of its initiator waits in the initiator.
With four entries at each end, 16-beat writes beside 16-beat reads reach about
0.87 W beats per cycle, as reads take most entries.

A full AXI target can receive a write and a read in the same cycle. It can
also receive a read while a write packet is still arriving. When the
[target capacity](transactions.md#target-capacity) has one free entry, the two
directions take it by turns.

| Contention for one entry | Direction that goes first |
| --- | --- |
| First time after reset | Read |
| Later | The direction that did not take the last entry |

The other packets of a started burst do not wait for the other direction. A
request that waits for its ordering key or destination blocks only its own
direction.

## Decode misses

A full AXI target that answers [decode misses](address-map.md#decode-misses)
takes a miss only while it is empty. Until that miss response leaves, it takes
no other request. When a miss waits at one request input and a request waits
at the other, the two inputs take turns.

| Contention at the default target | Input that goes first |
| --- | --- |
| First time after reset | Write |
| Later | The input that did not send the last request into the target |

A request of the other direction thus waits for at most one miss, also when an
initiator sends misses back to back. The wait counts from the cycle the request
reaches the input of the target.

## Progress requirements

A read does not wait for a write on a switch output, link or crossing, and B
does not wait for R there. A direction can still wait for the other one at the
endpoints. This table gives what the initiators and targets outside the network
must still do. It changes the requirements of
[shared paths](timeout-and-progress.md#shared-request-and-response-paths).

| Requirement with shared paths | With separate paths |
| --- | --- |
| W must not wait for the initiator's own read response | Required only when that read waits for an entry. A read waits for an entry when the initiator's `max_outstanding` or ID table is full. It also waits at a [full AXI](protocols.md) target with no free entry, and at an APB, AHB or AXI4-Lite target |
| R acceptance must not wait for B | Required. A [full AXI](protocols.md) target that answers multi-beat reads keeps R and B in one response register. An APB, AHB or AXI4-Lite target serves one transfer at a time |
| The target accepts W without waiting for its own read completion | Required only when that read waits for an entry. It waits at a [full AXI](protocols.md) target with no free entry. It also waits at an APB, AHB or AXI4-Lite target, whose join keeps a write packet until its last W fragment |
| Supply every accepted write burst without depending on responses from other transactions | Required only for responses whose requests wait for an entry, as in the first row |
| A DMA buffers the whole write burst before its first W beat | Required only when its read can wait for an entry, as in the first row |
| Accept responses without waiting for a later address or data handshake | Required |
| The target eventually completes every accepted transfer | Required |

## Mixed paths

Set `read_write: shared` or `read_write: separate` on a node or link in
`topology.request` or `topology.response`. Each setting overrides the
top-level default for that item. Endpoint attachments follow their switch.
A mirrored response graph copies the request nodes' and links' settings.

NocLoom fixes these choices at generation. Both paths use the same topology
routes. Address mapping and access checks follow the endpoint configuration.

```yaml
# In topology.request
nodes:
  - {name: fabric, read_write: separate}
  - {name: peripheral}
links:
  - {from: fabric, to: peripheral}
```

With the default `shared`, this link takes one stream from two switch outputs.
A boundary is a place where shared paths meet separate paths. A join takes the
write path and the read path into a shared path. A split does the opposite. An
endpoint connects to the write paths and to the read paths when either of its
attachments uses separate paths. A boundary then joins or splits them at its
shared attachment.

A split selects the output path with the packet's direction field. A join
uses round robin with the read path first after reset. A request grant holds
through the last fragment of its packet. A response grant holds through the
last fragment of its packet or segment. Both paths of a separate link take
the configured widths, pipes and clock crossing. Each arbiter of a separate
switch uses the configured output policy.

Keep the [shared progress requirements](timeout-and-progress.md#shared-request-and-response-paths)
at any shared switch, link or join that can block one direction behind the
other. Supply W independently of read
completion. Accept R independently of B. Accept W at the target
independently of read completion. Apply the separate path table above
when both routes for an endpoint pair are separate throughout. Other traffic
can still occupy a shared endpoint's entries.

The map sets `network.read_write` to `mixed`. The topology and each route
record its effective mode. `network.path_boundaries` names the side, clock
domain and kind of each boundary. A join also names its packet grant resource.
Read path resources of a separate switch or link have the suffix `/read`.

The dependency file includes shared resources, separate resources and join
waits. The mixed transport model is omitted, so the completion theorems do
not apply. Fixed priority outputs still report their unmet fairness premise.

## Unsupported options

NocLoom rejects these options with `NCL_READ_WRITE_PATHS` in a network that
uses separate paths, including a network with mixed paths.

| Option | Field |
| --- | --- |
| Scheduling levels, AxQOS maps, shapers and schedulers | `initiators[].qos` |
| TileLink and Wishbone endpoints | `initiators[].protocol`, `targets[].protocol` |
| Error logs and their register window | `error_logging.logs`, `error_logging.registers` |
| An issue pipe | `initiators[].pipes.issue` |
| An arrival, dispatch or response pipe of a target | `targets[].pipes` |

## Outputs

The map records `network.read_write` as `merged` for shared paths, `separate`
or `mixed`. The topology record `definition.topology` keeps the configuration
values. Its `read_write` is `separate` or `mixed`. With mixed paths each node
and link has `shared` or `separate`. See [map fields](map-fields.md).

With separate paths, `network.read_request_nodes`,
`network.read_response_nodes`, `network.read_request_routes` and
`network.read_response_routes` describe the read graphs. Each topology node
also has its `read_hardware_node`. Each arbitrated output names the paths it
serves in `carries`, as `writes` or `reads`. The
[path report](path-report.md) uses the same values for its links and paths.

The [dependency file](outputs.md#dependency-graph) gives these
`model_omitted` reasons. Neither mode has a transport model or an exclusion
that names a liveness check. In `completion.unmet`, the read arbiter of an
output has the subject of its write arbiter with the suffix `/read`.

| Mode | `model_omitted` |
| --- | --- |
| `separate` | `separate read and write paths` |
| `mixed` | `mixed read and write paths` |

## Area

The second copy costs area. These counts compare both settings of the same
examples. The cost comes mostly from the second copy of each switch, width
adapter and clock crossing. Each initiator loses its request merger and each
target its response merger. The counts are from a Yosys 0.69 synthesis to
generic gates with flattened hierarchy.

| Example in `examples/` | Flip-flop bits, shared | Flip-flop bits, separate | Logic gates, shared | Logic gates, separate |
| --- | ---: | ---: | ---: | ---: |
| `axi_lite_apb.yaml` | 638 | 652 | 2186 | 2210 |
| `axi4_apb_targets.yaml` | 1452 | 1471 | 9802 | 9717 |
| `axi4_outstanding.yaml` | 2705 | 2736 | 15134 | 15348 |
| `axi4_exchange.yaml` | 2943 | 3240 | 16110 | 16890 |
| `axi4_graph.yaml` | 4296 | 4888 | 23389 | 24323 |
| `soc_mixed.yaml` | 4742 | 5115 | 27040 | 28208 |
| `axi4_graph_clocked.yaml` | 16482 | 19493 | 45422 | 51392 |

The join of an APB, AHB or AXI4-Lite target adds logic in front of that
target. In `axi_lite_apb.yaml` the longest path between two flip-flops grows
from 36 to 44 gate levels.
