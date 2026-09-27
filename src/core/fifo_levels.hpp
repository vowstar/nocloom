#pragma once
#include "hardware_builder.hpp"
#include <span>
#include <vector>

namespace nocloom::detail {
inline ValueId fifo_binary_count(Builder &b, ValueId gray) {
  const auto bits = b.ir.values.at(gray.index).width;
  auto result = b.slice(gray, bits - 1, 1);
  for (unsigned k = bits - 1; k-- > 0;) {
    const auto a = b.slice(result, 0, 1), z = b.slice(gray, k, 1);
    const auto different = b.binary(Op::BitAnd, b.binary(Op::BitOr, a, z),
                                    b.unary(Op::BitNot, b.binary(Op::BitAnd, a, z), 1));
    result = b.concat(result, different);
  }
  return result;
}
inline ValueId fifo_level_max(Builder &b, std::span<const ValueId> levels) {
  if (levels.size() == 1)
    return levels.front();
  const auto half = levels.size() / 2;
  const auto a = fifo_level_max(b, levels.first(half));
  const auto z = fifo_level_max(b, levels.subspan(half));
  return b.select(b.binary(Op::UnsignedLess, a, z), z, a);
}
struct FifoLevels {
  ValueId count{}, level{};
  std::vector<ValueId> active;
};
// Count wrap and physical slot wrap have separate moduli. Only the last synchronized
// write count can publish slots. The FIFO owns their stability until release returns.
inline FifoLevels lower_fifo_levels(Builder &b, ValueId published_write, ValueId local_read,
                                    ValueId read_index, ValueId enabled,
                                    std::span<const ValueId> slots) {
  const auto depth = static_cast<unsigned>(slots.size());
  const auto bits = b.ir.values.at(read_index.index).width;
  FifoLevels result;
  result.count = b.binary(Op::Subtract, fifo_binary_count(b, published_write),
                          fifo_binary_count(b, local_read));
  const auto count_bits = b.ir.values.at(result.count.index).width;
  std::vector<ValueId> levels;
  for (unsigned i = 0; i < depth; ++i) {
    // Widen before subtraction so a power-of-two depth is representable too.
    const auto r = b.unary(Op::ZeroExtend, read_index, bits + 1);
    const auto distance =
        b.binary(Op::Subtract,
                 b.select(b.binary(Op::UnsignedLess, b.constant(bits, i), read_index),
                          b.constant(bits + 1, i + depth), b.constant(bits + 1, i)),
                 r);
    const auto distance_count = count_bits < bits + 1 ? b.slice(distance, 0, count_bits)
                                : count_bits == bits + 1
                                    ? distance
                                    : b.unary(Op::ZeroExtend, distance, count_bits);
    const auto active =
        b.binary(Op::BitAnd, enabled, b.binary(Op::UnsignedLess, distance_count, result.count));
    result.active.push_back(active);
    levels.push_back(b.select(active, slots[i], b.constant(3, 0)));
  }
  result.level = fifo_level_max(b, levels);
  return result;
}
} // namespace nocloom::detail
