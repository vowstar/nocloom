#pragma once
#include "logic.hpp"
#include <bit>
#include <string>

namespace nocloom::detail::serial_class {
// Burst admission around a single reassembly context, independent of the pieces of a burst. A
// serial burst waits for older bursts to complete, then keeps later bursts out until it completes.
struct Gate {
  ValueId count, serial;
  unsigned capacity = 0, bits = 0;
};
inline Gate make(logic::Logic &l, const std::string &prefix, unsigned capacity,
                 const std::string &counter = "bursts") {
  const auto bits = static_cast<unsigned>(std::bit_width(capacity));
  return {l.state(prefix + "." + counter, bits), l.state(prefix + ".serial", 1), capacity, bits};
}
inline ValueId admit(logic::Logic &l, const Gate &g, ValueId serial) {
  const auto room = l.b.binary(Op::UnsignedLess, g.count, l.b.constant(g.bits, g.capacity));
  return l.both(l.inv(g.serial), l.b.select(serial, l.eq(g.count, 0), room));
}
inline void step(logic::Logic &l, const Gate &g, ValueId accepted, ValueId serial,
                 ValueId completed) {
  const auto one = l.b.constant(g.bits, 1), zero = l.b.constant(g.bits, 0);
  const auto plus = l.b.binary(Op::Subtract, g.count, l.b.binary(Op::Subtract, zero, one));
  const auto minus = l.b.binary(Op::Subtract, g.count, one);
  l.b.update(g.count, l.b.select(l.both(accepted, l.inv(completed)), plus,
                                 l.b.select(l.both(completed, l.inv(accepted)), minus, g.count)));
  l.b.update(g.serial, l.b.select(accepted, serial, l.b.select(completed, l.bit(0), g.serial)));
}
} // namespace nocloom::detail::serial_class
