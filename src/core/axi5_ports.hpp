#pragma once

namespace nocloom::detail {
// The AXI5 request attributes in the request user field (message.hpp point 13). They sit above the
// user flags and the AHB5 metadata, from low to high: NSAID, PBHA, the subsystem ID, MPAM, the
// domain and the snoop bit. Each field has the width of its widest source, and a zero width omits
// it. The MPAM field keeps MPAM_NS in bit 0, PARTID above it and PMG in its top bit (IHI 0022 Issue
// L, A12.1.2). The domain field is AxDOMAIN. The snoop field is AWSNOOP bit 0 of a WriteUniqueFull
// that is one Regular cache line at its source: the only nonzero opcode bit of a transaction the
// network admits (docs/ace5-lite.md). Domain and snoop are the two top fields. A field added later
// goes below domain.
struct Axi5Attributes {
  unsigned nsaid = 0, pbha = 0, subsystem_id = 0, mpam = 0;
  unsigned domain = 0, snoop = 0;
  unsigned bits() const { return nsaid + pbha + subsystem_id + mpam + domain + snoop; }
  bool operator==(const Axi5Attributes &) const = default;
};
// The AXI5 request ports of one endpoint (docs/axi5.md). The defaults are the AXI4 ports.
struct Axi5RequestPorts {
  unsigned mpam = 0;            // AxMPAM bits: 0, 11 or 14
  unsigned subsystem_id = 0;    // AxSUBSYSID bits, 0..8
  unsigned subsystem_value = 0; // the AxSUBSYSID value of an initiator without the port
  bool nsaid = false, pbha = false;
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
} // namespace nocloom::detail
