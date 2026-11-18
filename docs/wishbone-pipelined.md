# Pipelined cycles

This page covers pipelined Wishbone admission, response order and timeout behavior.

See [Wishbone](wishbone.md) for common fields, data lanes and signal names.

Set `cycles: pipelined` on each pipelined endpoint. Only these ports have
STALL. Requests are accepted on rising edges with CYC and STB high and
STALL low. Responses terminate requests in acceptance order. STB can be
low when ACK or ERR arrives. ERR terminates one request and the sequence
continues.

| Property | Initiator port | Target port |
| --- | --- | --- |
| Capacity | `max_outstanding`, 1 to 64, inherits `transport.max_outstanding` (default 1) | One accepted request |
| Full capacity | STALL is high | No new STB until the response is stored |
| Same-cycle response | The new request responds later. An older response can coincide with acceptance | ACK, ERR or enabled RTY can complete a request on its acceptance edge |
| Direction or destination change | The held request waits for earlier network responses to drain | Direction changes after the accepted request completes |
| Reset | STALL is high, ACK and ERR are low | CYC and STB are low |

## Initiator flow control

The initiator port holds one request before the network accepts it.
`max_outstanding` counts this request and accepted requests in the network
whose responses have not completed. Responses have no backpressure.
Dropping STB inserts a request gap. Dropping CYC abandons responses and
blocks new requests until the accepted work drains.

Initiator ACK and ERR are registered values gated by CYC. Lowering CYC
clears them through a combinational path. Include that path in timing checks.
A suppressed response is discarded.

## Target timeout

A target timeout keeps CYC high until common reset and discards late responses.
A request already blocked by STALL remains stable until acceptance.
The target issues no new STB. Timeout completion follows the target
[timer rules](timeout-and-progress.md#target-timeouts).

An external classic slave connects to a pipelined target through a wrapper.
The wrapper holds its request until termination, asserts STALL while busy and
returns one response per accepted request.
An external slave can connect directly if each accepted request produces one
response, including when STB is low.
