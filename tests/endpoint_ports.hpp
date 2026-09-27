#pragma once
#include <nocloom/definition.hpp>

// Endpoint fields that follow each endpoint's protocol: id and len port widths
// on full AXI4 initiators, the len port width on AXI4 targets, and the amba
// attributes on every target that takes attributes. Other endpoints lose them.
inline void endpoint_ports(nocloom::Definition &d, unsigned id, unsigned len) {
  using nocloom::Protocol;
  for (auto &i : d.initiators) {
    const bool axi = i.protocol == Protocol::Axi4;
    i.id_width = axi ? std::optional(id) : std::nullopt;
    i.len_width = axi ? std::optional(len) : std::nullopt;
  }
  for (auto &t : d.targets) {
    const bool axi = t.protocol == Protocol::Axi4;
    t.len_width = axi ? std::optional(len) : std::nullopt;
    if (!axi)
      t.id_width.reset();
    if (t.protocol == Protocol::Apb3 || t.protocol == Protocol::Apb2)
      t.attributes.reset();
    else if (!t.attributes)
      t.attributes = nocloom::TargetAttributes::amba();
  }
}
// Sets a target's protocol and the fields that follow it, as above.
inline void set_protocol(nocloom::Target &t, nocloom::Protocol protocol) {
  using nocloom::Protocol;
  t.protocol = protocol;
  if (protocol != Protocol::Axi4) {
    t.id_width.reset();
    t.len_width.reset();
  }
  if (protocol == Protocol::Apb3 || protocol == Protocol::Apb2)
    t.attributes.reset();
  else if (!t.attributes)
    t.attributes = nocloom::TargetAttributes::amba();
}
// Sets an initiator's protocol; only a full AXI4 one keeps ID and length ports.
inline void set_protocol(nocloom::Initiator &i, nocloom::Protocol protocol) {
  i.protocol = protocol;
  if (protocol != nocloom::Protocol::Axi4) {
    i.id_width.reset();
    i.len_width.reset();
  }
}
