# AXI4 transport refinement

Use `--refinement` to generate a read refinement map and its netlist. The
bundle lists the map as `refinement_map`.

The map requires one AXI4 initiator and one AXI4 target on a direct shared
transport. Each port supports reads and has a single flow and an outstanding
limit of one. The transport uses one ungated clock and one direct reset.
The paths have no link pipes, width conversion or scheduling level bits.
The target has no endpoint pipes, upsize adapter, read interleaving or timeout.

The dependency model has one target pool and two routes. The routes end at
their first outputs. The model has no bound destinations.

## Map fields

| Field | Meaning |
| --- | --- |
| `format_version` | Map format version 1 |
| `module` | Top module |
| `initiator`, `target` | Port owners |
| `clock`, `reset` | Netlist value indices of the clock and active-low reset inputs |
| `context_count` | Netlist value index of the target read context count |
| `request`, `response` | Slot, destination and output indices in the dependency model |
| `boundaries` | Netlist value indices at the entry and exit of each transport path |

Each boundary gives the `valid`, `ready` and `last` indices. It also gives the
`write` index on a request path or the `source` flow index on a response path.

## Step checker

The Lean checker tests one rising edge against the imported transport model.
An accepted edge preserves the matching invariant and takes zero or more
model steps. The target context count represents occupancy at each edge. The
model maintains a logical transaction token and empty queues between edges.

CTest `read_refinement` uses two direct AXI4 designs: 32-bit data with an
asynchronous reset and 64-bit data with a synchronous reset. Lean evaluates
the edge check on one idle edge with an occupied context, and rejects a
netlist that clears that context. The test turns the Lean guard into cycle
assertions on the generated Verilog. They assume single-beat reads at address
zero with at most one read in flight.

The test rewrites each assertion as a clocked immediate assertion. SymbiYosys
with `abc pdr` proves them for every state that the design reaches from reset
under these assumptions, without a depth bound. Each cover statement must be
reached within 16 cycles. On the 32-bit design the solver must find a
counterexample within 12 cycles for five changes. Three change the context:
cleared, never cleared or never changed. Two are wrong maps: every boundary on
a constant, and request and response entries swapped.

## Read and write observations

For a direct pair with both directions enabled, the
map can include a `transactions` object. This object requires an observable
write context and data lanes of equal width at both ends of each path.

| Field | Meaning |
| --- | --- |
| `write_count` | Netlist value index of the target write context bit |
| `request_payload` | Entry and exit index pairs for write data and byte enables |
| `response_payload` | Entry and exit index pairs for read data |

The transaction checker extends the edge check to one outstanding single-beat
read or write at a time. It checks the read and write context bits, transfer
kind and payload equality on accepted transfers. The transport model tracks
control and tokens. The payload equality relates the observed netlist values.

CTest `transaction_refinement` checks the map and the Lean edge theorem on
the same two designs. Its cycle assertions use full-width transfers at address
zero, and SymbiYosys proves them as above. On the 32-bit design the solver must
find a counterexample for six changes. Three change the write context: cleared,
never cleared or never changed. One flips a bit on each of four output ports.
Two are wrong maps: read and write contexts swapped, and a payload pair on an
unrelated value.

## Completion with endpoint progress

`Wormhole.ReadRefinement.checked_port_completes` gives a later initiator
response for an accepted request on a checked direct trace. The requests are
serialized until the initiator accepts its response. The conclusion follows
from five progress premises alone, as `port_completes_of_progress` shows. The
theorem also requires the dependency checks, but does not need them.

The transport takes the source request and issues it at the target port. The
target answers, and the transport takes the answer and returns it to the
source. Only the answer is target behavior. `answer_alone_does_not_complete`
gives a trace on which the target answers every request and no response comes.
Neither the trace nor the model supplies the transport steps.

CTest `transaction_completion` checks these theorems with the two direct
designs. It adds assertions for the five progress steps and proves them with
SymbiYosys as above. The assumptions give each endpoint handshake at most two
cycles and each target answer one to three cycles. Each step must then occur
within eight cycles.

On the 32-bit design the solver must find a counterexample for five changes
that each break one step. Four silence valid signals, and one removes the
answer assumption of the target. No checked link carries the solver result into
the premises of the Lean theorem.
