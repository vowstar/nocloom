#pragma once
#include "logic.hpp"
#include <optional>
#include <string>

namespace nocloom::detail::beat_stage {
// Two-entry beat stage with registered producer ready and one beat per cycle.
struct Stage {
  ValueId to_main, to_spare, shift;
};
inline Stage stage(logic::Logic &l, ValueId full, ValueId spare, ValueId push, ValueId pop) {
  const Stage s{l.both(push, l.either(l.inv(full), pop)), l.both(push, l.both(full, l.inv(pop))),
                l.both(pop, spare)};
  l.b.update(full, l.b.select(l.either(push, spare), l.bit(1), l.b.select(pop, l.bit(0), full)));
  l.b.update(spare, l.b.select(s.to_spare, l.bit(1), l.b.select(s.shift, l.bit(0), spare)));
  return s;
}
inline ValueId stage_field(logic::Logic &l, const Stage &s, ValueId main,
                           const std::string &spare_name, ValueId value,
                           std::optional<ValueId> hold = std::nullopt) {
  const auto spare = l.latch(spare_name, value, s.to_spare);
  auto load = l.either(s.to_main, s.shift);
  if (hold)
    load = l.both(load, l.inv(*hold));
  l.b.update(main, l.b.select(load, l.b.select(s.shift, spare, value), main));
  return spare;
}
inline ValueId staged(logic::Logic &l, const Stage &s, const std::string &name,
                      const std::string &spare_name, ValueId value,
                      std::optional<ValueId> hold = std::nullopt) {
  const auto main = l.state_like(name, value);
  stage_field(l, s, main, spare_name, value, hold);
  return main;
}
// Offer the main entry while full, otherwise the arriving field.
inline ValueId offered(logic::Logic &l, const Stage &s, ValueId full, const std::string &name,
                       const std::string &spare_name, ValueId value) {
  return l.b.select(full, staged(l, s, name, spare_name, value), value);
}
// offered() over the caller's spare entry register, which other_load fills only while it is empty.
inline ValueId offered_over(logic::Logic &l, const Stage &s, ValueId full, const std::string &name,
                            ValueId spare, ValueId value, ValueId other_load, ValueId other) {
  const auto main = l.state(name, l.width(value));
  l.b.update(spare, l.pick(s.to_spare, value, l.pick(other_load, other, spare)));
  l.b.update(main, l.pick(l.either(s.to_main, s.shift), l.pick(s.shift, spare, value), main));
  return l.pick(full, main, value);
}
enum ResponsePhase : unsigned {
  Available = 0,
  ReadBoundary = 1,
  ReadPacket = 2,
  ReadTransfer = 3,
  WriteResponse = 4
};
// Bit 1 reserves the read side while a packet or transfer is held.
inline ValueId read_owned(Builder &b, ValueId phase) { return b.slice(phase, 1, 1); }
struct ResponseSegment {
  enum Phase : unsigned { Fresh, Watching, Deadline }; // Counts elapsed wait stages.
  logic::Logic &l;
  ValueId phase;
  ValueId fresh() { return l.eq(phase, Fresh); }
  ValueId offered(ValueId full, ValueId last, ValueId successor, ValueId inside) {
    const auto deliverable = l.either(l.either(last, inside), l.either(successor, l.inv(fresh())));
    return l.both(full, deliverable);
  }
  ValueId cut(ValueId last, ValueId successor, ValueId different, ValueId inside) {
    const auto other = l.both(l.inv(inside), l.both(successor, different));
    const auto expired =
        l.pick(fresh(), l.bit(0), l.pick(successor, l.eq(phase, Deadline), l.bit(1)));
    return l.pick(last, l.bit(0), l.either(other, expired));
  }
  void advance(ValueId transferred, ValueId successor, ValueId full, ValueId inside) {
    const auto start = l.pick(l.both(full, l.inv(inside)), l.num(2, Watching), phase);
    l.b.update(phase, l.pick(transferred, l.num(2, Fresh),
                             l.pick(successor, phase, l.pick(fresh(), start, l.num(2, Deadline)))));
  }
};
} // namespace nocloom::detail::beat_stage
