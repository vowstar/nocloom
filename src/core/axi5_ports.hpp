#pragma once

namespace nocloom::detail {
// The AXI5 request attributes in the request user field (message.hpp point 13). They sit above the
// user flags and the AHB5 metadata, from low to high: NSAID, PBHA, the subsystem ID, MPAM, the
// trace bit (AxTRACE), the domain and the snoop bit. Each field has the width of its widest
// source, and a zero width omits it. The MPAM field keeps MPAM_NS in bit 0, PARTID above it and PMG
// in its top bit (IHI 0022 Issue L, A12.1.2). The domain field is AxDOMAIN. The snoop field is
// AWSNOOP bit 0 of a WriteUniqueFull that is one Regular cache line at its source: the only nonzero
// opcode bit of a transaction the network admits (docs/ace5-lite.md). Domain and snoop are the two
// top fields. A field added later goes below domain.
struct Axi5Attributes {
  unsigned nsaid = 0, pbha = 0, subsystem_id = 0, mpam = 0, trace = 0;
  unsigned domain = 0, snoop = 0;
  unsigned bits() const { return nsaid + pbha + subsystem_id + mpam + trace + domain + snoop; }
  bool operator==(const Axi5Attributes &) const = default;
};
// The AXI5 request ports of one endpoint (docs/axi5.md). The defaults are the AXI4 ports.
struct Axi5RequestPorts {
  unsigned mpam = 0;            // AxMPAM bits: 0, 11 or 14
  unsigned subsystem_id = 0;    // AxSUBSYSID bits, 0..8
  unsigned subsystem_value = 0; // the AxSUBSYSID value of an initiator without the port
  bool nsaid = false, pbha = false, trace = false;
  bool prot = true, priv_inst = false, pas = false, cache = true;
  // Shareable transactions: AWDOMAIN and ARDOMAIN, and the four-bit AWSNOOP and ARSNOOP.
  bool domain = false, write_snoop = false, read_snoop = false;
  bool operator==(const Axi5RequestPorts &) const = default;
};
// Whether an initiator checks the AxSNOOP and AxDOMAIN pair of each request: with any of their
// ports. Its descriptor then keeps the result.
inline bool coherence_checked(const Axi5RequestPorts &ports) {
  return ports.domain || ports.write_snoop || ports.read_snoop;
}
// The AXI5 data side ports of one endpoint (docs/axi5.md): WUSER and the data part of RUSER, of
// `user` bits, WPOISON and RPOISON, WTRACE and RTRACE.
struct Axi5DataPorts {
  unsigned user = 0;
  bool poison = false, trace = false;
  bool any() const { return user || poison || trace; }
  bool operator==(const Axi5DataPorts &) const = default;
};
// The values an AXI5 initiator returns with each response of a request
// (docs/axi5.md#response-echoes): AxIDUNQ on BIDUNQ and RIDUNQ, AxLOOP on BLOOP and RLOOP. A target
// drives the request signals zero and ignores the response ones. With trace the request's AxTRACE
// is stored too: BTRACE and RTRACE hold it OR the trace bits of the response. bits() is the width
// of one direction's stored value: the unique bit lowest, the loop value above it, the trace bit
// on top.
struct Axi5EchoPorts {
  bool unique = false;
  unsigned write_loop = 0, read_loop = 0;
  bool trace = false;
  unsigned loop(bool write) const { return write ? write_loop : read_loop; }
  unsigned bits(bool write) const { return (unique ? 1U : 0U) + loop(write) + (trace ? 1U : 0U); }
  bool operator==(const Axi5EchoPorts &) const = default;
};
// The group of bits beside each data byte (message.hpp point 20): the data user bits from bit 0,
// then the poison bit and the trace bit. The data_user payload field holds one group per byte,
// byte b's at bits() * b.
struct ByteGroups {
  unsigned user = 0; // transport.data_user_bits_per_byte
  bool poison = false, trace = false;
  unsigned bits() const { return user + (poison ? 1U : 0U) + (trace ? 1U : 0U); }
  unsigned poison_bit() const { return user; }
  unsigned trace_bit() const { return user + (poison ? 1U : 0U); }
  bool operator==(const ByteGroups &) const = default;
};
} // namespace nocloom::detail
