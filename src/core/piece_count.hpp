#pragma once
#include "logic.hpp"
#include <string>

namespace nocloom::detail::piece_count {
// Pieces in flight. The bounded packet splitter and the wide-to-narrow
// cutter send the next piece (packet or target burst) of a burst without
// waiting for the previous one's response: a piece is counted when it is
// sent and uncounted when its response packet ends (a B, or the last R).
// At most kInFlight pieces are in flight per direction — except the pieces
// of a narrow-split burst, which the source's shared in-flight pool counts
// instead: a counter that may hold those takes a wider width. Every piece
// of one segment reaches one target over one route; a route splitter drains
// each routing segment before switching targets. A target that receives
// pieces serves one burst per direction in arrival order, so the responses
// come back in the order the pieces left. Every piece request produces
// exactly one response packet (message.hpp contract point 9).
inline constexpr unsigned kInFlight = 4;
inline constexpr unsigned kBits = 3;

inline ValueId counter(logic::Logic &l, const std::string &name, unsigned bits = kBits) {
  return l.state(name, bits);
}
// Each cycle the counter adds up and subtracts down, never below zero (a reset of this side).
inline void step(logic::Logic &l, ValueId count, ValueId up, ValueId down) {
  const unsigned bits = l.width(count);
  const auto one = l.num(bits, 1);
  down = l.both(down, l.inv(l.eq(count, 0)));
  const auto plus = l.plus(count, one), minus = l.minus(count, one);
  const auto next =
      l.pick(l.both(up, l.inv(down)), plus, l.pick(l.both(down, l.inv(up)), minus, count));
  l.b.update(count, next);
}
// Room for one more piece.
inline ValueId room(logic::Logic &l, ValueId count) {
  return l.below(count, l.num(kBits, kInFlight));
}
// Two internal result codes merged: network_miss (1) first, then target_error (2).
inline ValueId merge(logic::Logic &l, ValueId a, ValueId b) {
  return l.pick(l.either(l.eq(a, 1), l.eq(b, 1)), l.num(2, 1),
                l.pick(l.either(l.inv(l.eq(a, 0)), l.inv(l.eq(b, 0))), l.num(2, 2), l.num(2, 0)));
}
} // namespace nocloom::detail::piece_count
