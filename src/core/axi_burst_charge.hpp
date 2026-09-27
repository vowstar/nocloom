#pragma once

#include "hardware_builder.hpp"
#include <algorithm>
#include <vector>

namespace nocloom::detail {
// The charged bytes less one. INCR loses the unused positions of its first
// beat. FIXED repeats those positions on each beat and has at most 16 beats.
inline ValueId axi_burst_charge(Builder &b, ValueId length, ValueId size, ValueId burst,
                                ValueId start, unsigned max_size) {
  const auto len_bits = b.ir.values.at(length.index).width;
  const auto width = len_bits + max_size;
  const auto extend = [&](ValueId v, unsigned w) {
    return b.ir.values.at(v.index).width == w ? v : b.unary(Op::ZeroExtend, v, w);
  };
  const auto invert = [&](ValueId v, unsigned w) { return b.unary(Op::BitNot, v, w); };
  const auto equal = [&](ValueId v, unsigned n) {
    return b.binary(Op::Equal, v, b.constant(b.ir.values.at(v.index).width, n));
  };
  const auto plus = [&](ValueId x, ValueId y, unsigned w) {
    return invert(b.binary(Op::Subtract, invert(x, w), y), w);
  };
  auto last = extend(length, width);
  auto beat_last = b.constant(max_size, 0);
  for (unsigned k = 1; k <= max_size; ++k) {
    const auto at_size = equal(size, k);
    const auto low = invert(b.slice(start, 0, k), k);
    last = b.select(at_size, extend(b.concat(length, low), width), last);
    beat_last = b.select(at_size, extend(low, max_size), beat_last);
  }
  const auto fixed_len_bits = std::min(4u, len_bits);
  const auto fixed_len = b.slice(length, 0, fixed_len_bits);
  const auto fixed_width = fixed_len_bits + max_size;
  const auto low = extend(beat_last, fixed_width);
  // (length + 1) * (beat_last + 1) - 1 = length + beat_last + length * beat_last.
  std::vector<ValueId> terms{extend(fixed_len, fixed_width), low};
  for (unsigned k = 0; k < fixed_len_bits; ++k) {
    const auto shifted = k ? extend(b.concat(beat_last, b.constant(k, 0)), fixed_width) : low;
    terms.push_back(b.select(b.slice(fixed_len, k, 1), shifted, b.constant(fixed_width, 0)));
  }
  for (unsigned stride = 1; stride < terms.size(); stride *= 2)
    for (unsigned k = 0; k + stride < terms.size(); k += 2 * stride)
      terms[k] = plus(terms[k], terms[k + stride], fixed_width);
  return b.select(equal(burst, 0), extend(terms.front(), width), last);
}
} // namespace nocloom::detail
