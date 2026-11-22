# AXI5 data user bits and poison

See [AXI5 data side](axi5.md#data-side) for port width limits and
[trace](axi5.md#trace) for the trace signals.

## Data user bits

`data_user_width` gives an `axi5` endpoint WUSER and the data part of RUSER.
The width is the number of data bytes times `transport.data_user_bits_per_byte`,
as for APB5 (see [data user groups](user-signals.md#data-user-groups)). RUSER
holds the response user bits of `response_user_width` in its low bits and the
data user bits above them (Issue L, A12.5.3).

```yaml
transport: {data_user_bits_per_byte: 2}
initiators:
  - {name: cpu, clock_domain: main, protocol: axi5, data_width: 64,
     data_user_width: 16, response_user_width: 3}
```

This initiator has a 16-bit WUSER and a 19-bit RUSER. RUSER bits 0 to 2 are the
response user bits and bits 3 to 18 are the data user bits.

Each data byte has its own group of data user bits. Byte b uses bits `k*b` to
`k*b+k-1` of WUSER, or of the data part of RUSER, where `k` is
`transport.data_user_bits_per_byte`. The group stays with its byte through width
conversion, packet splitting, pipes and clock domain crossings.

| Case | Data user bits |
| --- | --- |
| A write byte with its WSTRB bit low | Zero at the target |
| A read transfer with SLVERR or DECERR | Zero |
| An initiator without the ports writes | Zero at the target |
| A target without the ports is read | Zero |
| A target without the ports is written | The network drops them |
| RUSER bytes outside the bytes of the read transfer | Not specified, as RDATA |

## Poison

`poison: true` gives WPOISON and RPOISON with one bit for each 8 bytes of data
(Issue L, A16.1). A 32-bit port has one bit. The network carries the bits when
some initiator and some target have the ports.

Each data byte carries a poison bit. An initiator gives each strobed byte the
WPOISON bit of its 8-byte granule. A target gets the OR of the bits of the
strobed bytes of each granule of its write transfer.

Each byte of a target's read transfer takes the RPOISON bit of its granule.
An initiator gets the OR of the bits of the bytes in each granule of its read
word. A poisoned byte keeps its mark through every width conversion.

| Case | Poison bits |
| --- | --- |
| A granule of a write transfer without a strobed byte | Zero at the target |
| A read transfer with SLVERR or DECERR | Zero |
| An initiator without the ports writes | Zero at the target |
| A target without the ports is read | Zero |
| A target without the ports is written | The network drops them, so the target does not see that the data is poisoned |
| RPOISON bits of granules outside the bytes of the read transfer | Can be set, as Issue L permits |
