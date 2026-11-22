#pragma once
#include "logic.hpp"
#include <bit>
#include <vector>

// Shareable transactions of AXI5 and ACE5-Lite ports (IHI 0022 Issue L, A7 and A8;
// docs/ace5-lite.md).
namespace nocloom::detail::shareable {
// The AxDOMAIN of a request without AxDOMAIN signals, from its AxCACHE (A8.3.4): System for a
// Device or Non-cacheable request (AxCACHE[3:2] zero), Non-shareable for a Cacheable one.
inline ValueId default_domain(logic::Logic &l, ValueId cache) {
  const auto system = l.eq(l.b.slice(cache, 2, 2), 0);
  return l.b.concat(system, system);
}
// Whether AxDOMAIN is Shareable: 0b01 or 0b10 (table A8.5).
inline ValueId shareable(logic::Logic &l, ValueId domain) {
  return l.inv(l.same(l.b.slice(domain, 0, 1), l.b.slice(domain, 1, 1)));
}
constexpr unsigned line_bytes = 64;
// Whether a burst on a port of `bytes` bytes is one Regular 64-byte cache line by its length and
// size (A3.1.8, A8.2): full-width beats, or one beat of 64 bytes on a wider port.
inline ValueId line_shape(logic::Logic &l, ValueId len, ValueId size, unsigned bytes) {
  const auto word = static_cast<unsigned>(std::countr_zero(bytes));
  if (bytes <= line_bytes) {
    // A length field too narrow for the beats of a line never holds one.
    const unsigned beats = line_bytes / bytes;
    if (static_cast<unsigned>(std::bit_width(beats - 1)) > l.width(len))
      return l.bit(0);
    return l.both(l.eq(size, word), l.eq(len, beats - 1));
  }
  return l.both(l.eq(size, std::countr_zero(line_bytes)), l.eq(len, 0));
}
// Whether a request is one Regular line from an address aligned to the line. An initiator passes
// the WriteUniqueFull opcode only for this shape. A FIXED burst never gets here: a Shareable
// request is not FIXED (A8.3.3).
inline ValueId source_line(logic::Logic &l, ValueId address, ValueId len, ValueId size,
                           unsigned bytes) {
  const unsigned low =
      std::min(l.width(address), static_cast<unsigned>(std::countr_zero(line_bytes)));
  return l.both(line_shape(l, len, size, bytes), l.eq(l.b.slice(address, 0, low), 0));
}
} // namespace nocloom::detail::shareable
