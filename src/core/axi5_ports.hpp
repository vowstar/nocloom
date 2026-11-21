#pragma once

namespace nocloom::detail {
// The AXI5 request attributes in the request user field (message.hpp point 13). They sit above the
// user flags and the AHB5 metadata, from low to high: NSAID, PBHA, the subsystem ID and MPAM. Each
// field has the width of its widest source, and a zero width omits it. The MPAM field keeps MPAM_NS
// in bit 0, PARTID above it and PMG in its top bit (IHI 0022 Issue L, A12.1.2).
struct Axi5Attributes {
  unsigned nsaid = 0, pbha = 0, subsystem_id = 0, mpam = 0;
  unsigned bits() const { return nsaid + pbha + subsystem_id + mpam; }
  bool operator==(const Axi5Attributes &) const = default;
};
// The AXI5 request ports of one endpoint (docs/axi5.md). The defaults are the AXI4 ports.
struct Axi5RequestPorts {
  unsigned mpam = 0;            // AxMPAM bits: 0, 11 or 14
  unsigned subsystem_id = 0;    // AxSUBSYSID bits, 0..8
  unsigned subsystem_value = 0; // the AxSUBSYSID value of an initiator without the port
  bool nsaid = false, pbha = false;
  bool prot = true, priv_inst = false, pas = false, cache = true;
  bool operator==(const Axi5RequestPorts &) const = default;
};
} // namespace nocloom::detail
