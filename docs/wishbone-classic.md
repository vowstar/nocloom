# Classic cycles

This page covers classic Wishbone initiator and target timing.

Set `cycles: classic` or omit `cycles`. See [Wishbone](wishbone.md) for
common fields, data lanes, address units and signal names.

## Initiators

The network initiator port is a Wishbone SLAVE. A transfer starts with CYC
and STB high and completes with one ACK or ERR pulse. A classic initiator
keeps one transfer in flight.

STB can remain high between completed transfers. CYC can remain high across
independent transfers. These signals provide no path reservation or atomic
read-modify-write operation.

Dropping CYC or STB before completion abandons the response. The admitted
request stays active and can still cause target side effects. The port drains
its response before accepting another transfer.

ACK and ERR are registered values gated by CYC and STB. Dropping either input
immediately clears both outputs. A suppressed response is discarded. Include
the combinational paths from CYC and STB to ACK and ERR in timing checks.

## Targets

The network target port is a Wishbone MASTER. In classic SINGLE and BLOCK
cycles, each target word uses one independent transfer. STB stays high with
ADR, WE, SEL and write data stable until a rising edge samples ACK, ERR or
enabled RTY. There are no combinational paths from response inputs to outputs.

Without burst tags, STB stays low for at least one cycle after the transfer.
CYC stays high within a packet and falls when no work remains. See
[registered feedback bursts](wishbone-registered-feedback.md#registered-feedback-bursts)
for timing with CTI and BTE.

A classic target accepts combinational ACK, registered ACK and held ACK,
as permitted by B4 RULE 3.55 and PERMISSION 3.30. Only a rising edge with STB
high completes a transfer. See [word errors](wishbone.md#responses-and-target-options).

Each packet of a [split AXI burst](wishbone.md#responses-and-target-options)
uses its own CYC.

A [timeout](timeout-and-progress.md#target-timeouts) holds classic STB and
CYC for the issued transfer until it terminates. The target drains late
termination and stays failed until common reset. Without timeout completion,
an unresponsive device can stop progress.
