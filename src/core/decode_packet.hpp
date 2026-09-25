#pragma once
#include "logic.hpp"
#include <vector>

namespace nocloom::detail::decode_packet {
// A known local read miss with no target access may travel as one length word (first and last).
// Native target responses never use this descriptor mark: result 3, or the dedicated error flag
// in decode_flag profiles with result network_miss. Readiness can inspect the mark without
// reading data. The length (beats after the first) occupies the low bits of every four-byte
// group, so serialization and lane placement preserve it.
inline ValueId marked(logic::Logic &l, bool flag, ValueId result, ValueId error) {
  return flag ? error : l.eq(result, 3);
}
// The result of a word marked when more beats follow, network_miss otherwise (more is a value).
inline ValueId mark_result(logic::Logic &l, bool flag, ValueId more) {
  return flag ? l.num(2, 1) : l.pick(more, l.num(2, 3), l.num(2, 1));
}
inline ValueId word_data(logic::Logic &l, ValueId length, unsigned data_bits) {
  auto group = l.fit(length, 32);
  auto data = group;
  for (unsigned g = 32; g < data_bits; g += 32)
    data = l.b.concat(group, data);
  return data;
}
// The length a word's data says; the groups other than those on the word's lanes are zero.
inline ValueId length_of(logic::Logic &l, ValueId data, unsigned len_bits) {
  auto length = l.b.slice(data, 0, len_bits);
  for (unsigned g = 32; g < l.width(data); g += 32)
    length = l.either(length, l.b.slice(data, g, len_bits));
  return length;
}
} // namespace nocloom::detail::decode_packet
