# Exclusive access

## Exclusive access

A target selects `exclusive: none`, `network` or `target`.

| Mode | Behavior |
| --- | --- |
| `none` | Perform the access normally and return OKAY. An exclusive write therefore reports exclusive failure. An exclusive burst still stays in one packet or returns an error without target access, as [Packets](packets.md#bounded-packets) describes |
| `network` | A monitor in the target adapter records reads and checks writes |
| `target` | An AXI target receives AxLOCK and handles its own reservations |

Exclusive targets require read and write access and a reaching read/write AXI
initiator. Network monitors support the
[configured target capacities](transactions.md#target-capacity).

By default, a network monitor uses the initiator and AxID as the reservation
owner. Each initiator has its own AxID space. Set `exclusive_owner_flags` to
use distinct transport user flags as the owner instead. Each reaching
initiator drives these flags. Target monitors require this field.

Set `exclusive_monitor` on a network target to configure its reservation table.

| Field | Meaning | Default |
| --- | --- | --- |
| `reservations` | Entries from 1 to the number of owner values, capped at 16 | The upper bound |
| `granule_bytes` | Round the reserved region out to a power of two from 1 to 128 bytes | 1 byte |

A matching exclusive write reaches the target and reports EXOKAY when
successful. An unmatched exclusive write reports OKAY. AXI targets receive zero
WSTRB and WDATA. APB and AHB targets receive no transfer. An overlapping
ordinary write clears affected reservations.

Monitor capacity can evict reservations.

Timeout behavior still applies to exclusive traffic. With a network monitor,
reservation rules continue even after target failure. With a target monitor, no
new access reaches the failed target.

## Exclusive shape and monitor rules

An exclusive access to a supporting target requires 1, 2, 4, 8 or 16 beats and
address alignment to total bytes. FIXED is rejected. Total bytes must fit 128
bytes, the target packet limit and its mapping window. APB3 and APB2 exclusive
beats need at least four bytes.

Invalid shapes return SLVERR without target access. An AXI target that handles
its own reservations also requires at most 16 beats after width conversion. An
exclusive converted WRAP of more than 16 target words is refused because it
requires pieces. NocLoom does not check AxCACHE. The system must route exclusive
accesses through the monitor.

With owner flags, the first name in lexical order supplies the low owner bit.
An exclusive write can match a read with another AxID if the flag values
match. AXI4-Lite initiators and initiators with only one direction have no
effective exclusive access.

An exclusive read replaces its owner's reservation at request acceptance,
regardless of its eventual response. Successful read beats report EXOKAY.
Matching exclusive writes report EXOKAY or the target error. Any write that
reaches the target, including a write prevented only by timeout, clears
overlapping reservations, including its own. An unmatched exclusive write
neither clears nor blocks reservations.

An exclusive read overlapping an in-flight write reports EXOKAY but creates no
reservation and leaves its owner's earlier reservation intact. A write is in
flight until its B leaves the target adapter, or arrives from a multi-burst AXI
target.

Only writes through the network are visible. A target with another write path
needs its own monitor. Target-domain reset clears reservations, but
initiator-domain reset alone leaves them.

With a target monitor, owner bits above the flow field must fit the target ID
port. An exclusive EXOKAY returns EXOKAY. EXOKAY for a normal access returns
SLVERR. Normal and exclusive accesses can share a target ID. With no exclusive
support, every exclusive write still performs its write and reports OKAY, so a
retry loop can write repeatedly without succeeding.

With a network monitor after timeout, unmatched exclusive writes return OKAY
only with completion enabled. Otherwise they remain unanswered. With a target
monitor after timeout, completion returns SLVERR without EXOKAY, or no response
when disabled.
