#pragma once
#include "axi5_ports.hpp"
#include "logic.hpp"
#include <optional>
#include <vector>

namespace nocloom::detail {
namespace groups {
// The `bytes` groups of `from` bits in value as groups of `to` bits: each keeps its low bits and
// takes zeros above them. The value itself when the widths match.
inline ValueId regroup(logic::Logic &l, ValueId value, unsigned bytes, unsigned from, unsigned to) {
  if (from == to)
    return value;
  std::optional<ValueId> out;
  for (unsigned b = 0; b < bytes; ++b) {
    auto group = l.b.slice(value, b * from, std::min(from, to));
    if (to > from)
      group = l.zext(group, to);
    out = out ? l.b.concat(group, *out) : group;
  }
  return *out;
}
// Groups as a word of the data width, group b in the low bits of byte b, so that a byte transform
// of the data word moves every group with its byte; gather takes them back.
inline ValueId spread(logic::Logic &l, ValueId value, unsigned bytes, unsigned bits) {
  return regroup(l, value, bytes, bits, 8);
}
inline ValueId gather(logic::Logic &l, ValueId word, unsigned bytes, unsigned bits) {
  return regroup(l, word, bytes, 8, bits);
}
// Each byte's `n` bits at `offset` of its group, byte 0 lowest.
inline ValueId part(logic::Logic &l, ValueId value, unsigned bytes, unsigned bits, unsigned offset,
                    unsigned n) {
  std::optional<ValueId> out;
  for (unsigned b = 0; b < bytes; ++b) {
    const auto piece = l.b.slice(value, b * bits + offset, n);
    out = out ? l.b.concat(piece, *out) : piece;
  }
  return *out;
}
// The OR of one bit of the groups of each aligned eight-byte granule (one bit when narrower).
inline ValueId granules(logic::Logic &l, ValueId value, unsigned bytes, unsigned bits,
                        unsigned bit) {
  const auto flags = part(l, value, bytes, bits, bit, 1);
  std::optional<ValueId> out;
  for (unsigned g = 0; g < bytes; g += 8) {
    std::vector<ValueId> members;
    for (unsigned b = g; b < std::min(bytes, g + 8); ++b)
      members.push_back(l.b.slice(flags, b, 1));
    const auto any = l.any(members);
    out = out ? l.b.concat(any, *out) : any;
  }
  return *out;
}
// The OR of one bit over every group.
inline ValueId any_bit(logic::Logic &l, ValueId value, unsigned bytes, unsigned bits,
                       unsigned bit) {
  const auto flags = part(l, value, bytes, bits, bit, 1);
  std::vector<ValueId> members;
  for (unsigned b = 0; b < bytes; ++b)
    members.push_back(l.b.slice(flags, b, 1));
  return l.any(members);
}
// The groups of a data word from a port value per part: user (k bits per byte, or none for zero),
// poison (one bit per eight-byte granule) and trace (one bit), each byte's group masked by its
// strobe.
inline ValueId compose(logic::Logic &l, const ByteGroups &layout, unsigned bytes,
                       std::optional<ValueId> user, std::optional<ValueId> poison,
                       std::optional<ValueId> trace, ValueId strobes) {
  std::optional<ValueId> out;
  for (unsigned b = 0; b < bytes; ++b) {
    std::optional<ValueId> group;
    const auto add = [&](ValueId v) { group = group ? l.b.concat(v, *group) : v; };
    if (layout.user)
      add(user ? l.b.slice(*user, b * layout.user, layout.user) : l.num(layout.user, 0));
    if (layout.poison)
      add(poison ? l.b.slice(*poison, b / 8, 1) : l.bit(0));
    if (layout.trace)
      add(trace ? *trace : l.bit(0));
    const auto bits = layout.bits();
    const auto kept = l.both(
        *group, l.pick(l.b.slice(strobes, b, 1), l.num(bits, (1ULL << bits) - 1), l.num(bits, 0)));
    out = out ? l.b.concat(kept, *out) : kept;
  }
  return *out;
}
} // namespace groups
} // namespace nocloom::detail
