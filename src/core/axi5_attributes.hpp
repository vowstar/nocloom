#pragma once
#include "axi5_ports.hpp"
#include "logic.hpp"
#include <iterator>
#include <numeric>
#include <optional>
#include <vector>

namespace nocloom::detail {
namespace axi5 {
// An MPAM value of 11 or 14 bits in the other layout: PARTID zero-extends or keeps its low 9 bits,
// MPAM_NS stays in bit 0 and PMG in the top bit.
inline ValueId mpam_resize(logic::Logic &l, ValueId value, unsigned bits) {
  const auto from = l.width(value);
  if (from == bits)
    return value;
  const auto pmg = l.b.slice(value, from - 1, 1);
  const auto low = l.b.slice(value, 0, 10);
  return l.b.concat(pmg, bits > from ? l.b.concat(l.num(bits - from, 0), low) : low);
}
// The request attributes of a source, none without attribute bits. port(name, bits) reads an AXI5
// port of the address channel. A declared port exists even when no target takes its field. A source
// without MPAM ports sends MPAM_NS from AxPROT[1] (A12.1.3) and zero above it.
template <class Port>
std::optional<ValueId> source_attributes(logic::Logic &l, const Axi5Attributes &a,
                                         const Axi5RequestPorts &ports, ValueId prot,
                                         const Port &port) {
  const auto read = [&](bool present, const char *name, unsigned bits) {
    return present ? std::optional{port(name, bits)} : std::nullopt;
  };
  const auto nsaid = read(ports.nsaid, "nsaid", 4), pbha = read(ports.pbha, "pbha", 4);
  const auto subsystem = read(ports.subsystem_id, "subsysid", ports.subsystem_id);
  const auto mpam = read(ports.mpam, "mpam", ports.mpam);
  std::vector<ValueId> fields;
  if (a.nsaid)
    fields.push_back(nsaid.value_or(l.num(4, 0)));
  if (a.pbha)
    fields.push_back(pbha.value_or(l.num(4, 0)));
  if (a.subsystem_id)
    fields.push_back(subsystem ? l.fit(*subsystem, a.subsystem_id)
                               : l.num(a.subsystem_id, ports.subsystem_value));
  if (a.mpam)
    fields.push_back(mpam ? mpam_resize(l, *mpam, a.mpam)
                          : l.b.concat(l.num(a.mpam - 1, 0), l.b.slice(prot, 1, 1)));
  if (fields.empty())
    return std::nullopt;
  return std::accumulate(std::next(fields.begin()), fields.end(), fields.front(),
                         [&](ValueId block, ValueId field) { return l.b.concat(field, block); });
}
// The AXI5 request ports of a target from the attributes at bit `offset` of the request user field.
// emit(name, value) drives one port. A target port wider than its field takes zeros above it.
// Without a field, NSAID, PBHA and the subsystem ID are zero and MPAM carries MPAM_NS from
// protection bit 1.
template <class Emit>
void target_attributes(logic::Logic &l, const Axi5Attributes &a, const Axi5RequestPorts &ports,
                       unsigned offset, ValueId user, ValueId protection, Emit emit) {
  unsigned at = offset;
  const auto field = [&](unsigned bits) {
    const auto value = bits ? std::optional{l.b.slice(user, at, bits)} : std::nullopt;
    at += bits;
    return value;
  };
  const auto nsaid = field(a.nsaid), pbha = field(a.pbha);
  const auto subsystem = field(a.subsystem_id), mpam = field(a.mpam);
  if (ports.nsaid)
    emit("nsaid", nsaid.value_or(l.num(4, 0)));
  if (ports.pbha)
    emit("pbha", pbha.value_or(l.num(4, 0)));
  if (ports.subsystem_id)
    emit("subsysid",
         subsystem ? l.fit(*subsystem, ports.subsystem_id) : l.num(ports.subsystem_id, 0));
  if (ports.mpam)
    emit("mpam", mpam ? mpam_resize(l, *mpam, ports.mpam)
                      : l.b.concat(l.num(ports.mpam - 1, 0), l.b.slice(protection, 1, 1)));
}
} // namespace axi5
} // namespace nocloom::detail
