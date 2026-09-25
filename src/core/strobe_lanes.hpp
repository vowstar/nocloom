#pragma once

#include "hardware_builder.hpp"

namespace nocloom::detail {
// Bit mask of the strobed byte lanes: byte q is all ones when strobe bit q is
// set, else zero. strobes has one bit per byte of a bytes-wide word.
inline ValueId strobe_lanes(Builder &b, ValueId strobes, unsigned bytes) {
  auto lane = [&](unsigned q) {
    return b.select(b.slice(strobes, q, 1), b.constant(8, 0xff), b.constant(8, 0));
  };
  ValueId bits = lane(0);
  for (unsigned q = 1; q < bytes; ++q)
    bits = b.concat(lane(q), bits);
  return bits;
}

// Write data with every byte whose strobe is clear forced to zero. Sources
// apply it when they capture a W beat, so an unstrobed byte reaches every
// target protocol as zero and later width or packet stages keep it zero.
inline ValueId strobed_data(Builder &b, ValueId data, ValueId strobes, unsigned bytes) {
  return b.binary(Op::BitAnd, data, strobe_lanes(b, strobes, bytes));
}
} // namespace nocloom::detail
