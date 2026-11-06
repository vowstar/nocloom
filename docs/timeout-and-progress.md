# Progress and target timeouts

## Shared request and response paths

A started write burst holds the initiator request path until its last W beat.
The initiator's reads cannot pass that unfinished write. A read response keeps
its output until the packet ends or an interleaving segment ends. APB, AHB and
AXI4-Lite targets serve directions in arrival order.

| Endpoint | Required behavior |
| --- | --- |
| Initiator | Deliver every W beat after starting a write. W must not wait for its own read response |
| Initiator | Accept R in finite time. R acceptance must not wait for B |
| Target | Accept W in finite time. W acceptance must not wait for its own read completion |
| Initiator | Supply every accepted write burst without depending on responses from other transactions |
| DMA | Buffer the whole write burst before its first W beat when its data comes from reads |
| Consumer | Accept responses without waiting for a later address or data handshake |
| Target | Eventually complete every accepted transfer |

Violations can stop the network until reset.

Completed read beats forward without waiting for the whole burst. A stopped R
consumer can block shared response paths.

## Endpoint progress duties

Analyze dependencies through components that have both target and initiator
ports separately. The definition does not model them.

Unlimited backpressure provides no cross-initiator progress bound. A waiting
ordering key or full ID table holds later requests, and a busy target can occupy
shared request switches. A stalled multibeat R can block B behind it. In the
single-domain automatic 32-bit profile, a stopped R consumer buffers two
initiator beats from APB4 or three from AXI4.

A wider initiator can additionally hold a partly gathered beat. AHB can hold two
read responses, and explicit graphs can add storage. Timeouts count target
waits, not consumer stalls. Common reset cancels all upstream and target state.

## Target timeouts

`timeout` contains `cycles` and optional `completion`. `cycles` is a power of
two from 16 to 1048576. `completion` defaults to `false`. APB2 targets reject
timeouts.

One watchdog covers both directions and all target packets in flight. It counts
target clock cycles while work is waiting and no response is offered to the
network. Every offered B or R beat restarts the count, even when the initiator
has not accepted it. Slow initiator write data can therefore contribute to
expiry. A stopped target clock is not detected.

Expiry raises `nl_t_<target>_timed_out` until common reset.

| Setting | Responses after expiry |
| --- | --- |
| `completion: false` | Unanswered and later work receives no response |
| `completion: true` | Unanswered and later work receives errors, with zero read data |

Responses already held by the target adapter can still leave. Completed target
accesses are not rolled back. Late responses are discarded. Unfinished reads
continue at the target. Unfinished writes receive no further data.

Unfinished AHB write blocks stay BUSY. Issued AHB reads finish and leave IDLE.
Only common reset clears the failed state.

## Timeout edge cases

The watchdog starts counting cycles after a request is offered or accepted work
is still unanswered. An idle target does not count, except the cycle after its
final response. After expiry, system logic must observe the status output if
completion is disabled.

With completion enabled, failed reads continue from their current beat with zero
data. Writes return one B after consuming remaining W. Window decode still
distinguishes misses in distinct mode. AXI writes retain each initiator's order,
AXI reads complete in queue order, and Lite completes packets in order. Issued
bus valid and payload stay stable until the late target handshake, then its
response is discarded.

An already driven AHB address phase can complete once when HREADY returns. No
other write transfer starts. A failed default target applies its timeout
behavior to later misses too.
