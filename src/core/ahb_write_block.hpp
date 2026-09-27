#pragma once
#include "logic.hpp"
#include <bit>
#include <numeric>
#include <string>
#include <vector>

namespace nocloom::detail {
struct AhbWriteBlock {
  ValueId lane, size, mask;
};

// Every node covers a naturally aligned power-of-two block. Mark the nodes
// starting at the lowest strobed byte, then consume the largest full one that
// fits the transfer container.
inline AhbWriteBlock ahb_write_block(logic::Logic &l, ValueId strobes, ValueId size_limit) {
  const unsigned lanes = l.width(strobes);
  const unsigned levels = std::countr_zero(lanes);
  const auto zero = l.bit(0);
  std::vector<ValueId> any(lanes * 2, zero), full = any;
  for (unsigned i = 0; i < lanes; ++i)
    any[lanes + i] = full[lanes + i] = l.b.slice(strobes, i, 1);
  for (unsigned i = lanes - 1; i > 0; --i) {
    any[i] = l.either(any[i * 2], any[i * 2 + 1]);
    full[i] = l.both(full[i * 2], full[i * 2 + 1]);
  }
  std::vector<ValueId> first(lanes * 2, zero), clear = first;
  first[1] = l.bit(1);
  auto lane = zero;
  std::vector<ValueId> found(levels, zero);
  for (unsigned count = 1; count <= lanes; count *= 2) {
    const unsigned log = levels - std::countr_zero(count);
    const auto fits = log == 0 ? l.bit(1)
                               : l.bind("bus.write_fits." + std::to_string(log),
                                        l.inv(l.below(size_limit, l.num(3, log))));
    std::vector<ValueId> blocks, right;
    for (unsigned i = count; i < count * 2; ++i) {
      const auto block = l.both(first[i], l.both(full[i], fits));
      clear[i] = l.either(clear[i / 2], block);
      if (log != 0) {
        blocks.push_back(block);
        const auto low = any[i * 2];
        first[i * 2] = first[i];
        first[i * 2 + 1] = l.both(first[i], l.inv(low));
        right.push_back(l.both(first[i * 2 + 1], any[i * 2 + 1]));
      }
    }
    if (log != 0) {
      found[log - 1] = l.any(blocks);
      const auto bit = l.any(right);
      lane = log == levels ? bit : l.b.concat(lane, bit);
    }
  }
  auto size = l.num(3, 0);
  for (unsigned log = 1; log <= levels; ++log)
    size = l.b.select(found[log - 1], l.num(3, log), size);
  const auto mask =
      std::accumulate(clear.begin() + lanes + 1, clear.end(), clear[lanes],
                      [&](ValueId mask, ValueId bit) { return l.b.concat(bit, mask); });
  return {lane, size, mask};
}
} // namespace nocloom::detail
