#pragma once
#include "hardware_builder.hpp"
#include <nocloom/definition.hpp>
#include <vector>
namespace nocloom::detail {
struct PressureSlot {
  ValueId demand, active;
};
struct PressurePosition {
  PressureSlot local;
  ValueId ready;           // Downstream blocking is !ready.
  bool registered = false; // Sample incoming pressure before adding local demand.
};
struct PressurePath {
  Builder &b;
  unsigned bits = 0;
  ValueId mask(ValueId value, ValueId active) const {
    return b.select(active, value, b.constant(bits, 0));
  }
  ValueId join(const std::vector<PressureSlot> &slots) const {
    const auto reduce = [&](auto &&self, std::size_t first, std::size_t end) -> ValueId {
      if (first == end)
        return b.constant(bits, 0);
      if (end - first == 1)
        return mask(slots[first].demand, slots[first].active);
      const auto mid = first + (end - first) / 2;
      const auto left = self(self, first, mid), right = self(self, mid, end);
      return b.binary(Op::BitOr, left, right);
    };
    return reduce(reduce, 0, slots.size());
  }
  ValueId follow(ValueId value, ValueId ready, const std::vector<PressurePosition> &positions,
                 auto sample) const {
    for (const auto &position : positions) {
      value = mask(value, b.unary(Op::BitNot, ready, 1));
      if (position.registered)
        value = sample(value);
      value = join({{value, position.local.active}, position.local});
      ready = position.ready;
    }
    return value;
  }
  ValueId encode(ValueId level) const {
    auto value = b.constant(bits, (1ULL << bits) - 1);
    for (unsigned k = bits; k-- > 0;)
      value = b.select(b.binary(Op::Equal, level, b.constant(3, k)),
                       b.constant(bits, (1ULL << k) - 1), value);
    return value;
  }
  ValueId rank(ValueId value) const {
    auto level = b.constant(3, 0);
    for (unsigned k = 0; k < bits; ++k)
      level = b.select(b.slice(value, k, 1), b.constant(3, k + 1), level);
    return level;
  }
};
struct QosLevel {
  ValueId value{};
  std::vector<ValueId> words{};
};

inline QosLevel lower_qos_level_hint(Builder &b, ValueId clock, ValueId reset, ValueId valid,
                                     ValueId head, ValueId level_hint, ValueId ready,
                                     const LinkPipeline &mode) {
  const auto either = [&](ValueId x, ValueId y) { return b.binary(Op::BitOr, x, y); };
  const auto inv = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  const auto width = b.ir.values.at(level_hint.index).width;
  const PressurePath path{b, width};
  const auto live = path.join({{head, valid}, {level_hint, valid}});
  if (!mode.forward)
    return {mode.backward ? live : level_hint, {}};
  if (!mode.backward) {
    const auto output = b.state(width, clock, reset);
    b.update(output, live);
    return {output, {output}};
  }
  // Occupancy is empty, one resident, or one resident with a reserved successor.
  const auto occupancy = b.state(2, clock, reset);
  const auto buffered = b.binary(Op::Equal, occupancy, b.constant(2, 2));
  const auto resident = inv(b.binary(Op::Equal, occupancy, b.constant(2, 0)));
  const auto kept = b.state(width, clock, reset), output = b.state(width, clock, reset);
  const auto present = either(buffered, valid), one = b.constant(2, 1);
  b.update(occupancy,
           b.select(ready, b.select(present, one, b.constant(2, 0)),
                    b.select(present, b.select(resident, b.constant(2, 2), one), occupancy)));
  const auto packet = b.select(buffered, kept, head);
  b.update(kept, packet);
  b.update(output, path.join({{packet, present}, {live, present}}));
  return {output, {occupancy, kept, output}};
}

// Each open reservation records crossed level thresholds. Rotating one-hot tokens select
// the next reservation and the current departure, independently of backpressure.
inline QosLevel lower_qos_level(Builder &b, ValueId clock, ValueId reset, unsigned stages,
                                ValueId level, ValueId idle, ValueId in_valid, ValueId in_ready,
                                ValueId out_valid, ValueId out_ready) {
  if (!stages)
    return {level, {}};
  const unsigned slots = stages + 1, thresholds = (1U << b.ir.values.at(level.index).width) - 1;
  const auto zero = b.constant(thresholds + 1, 0);
  const auto and_ = [&](ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); };
  const auto not_ = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  const auto pending = b.state(1, clock, reset);
  const auto arrival = b.state(slots - 1, clock, reset),
             departure = b.state(slots - 1, clock, reset);
  const auto opens = and_(in_valid, not_(pending)), closes = and_(out_valid, out_ready);
  b.update(pending, b.select(in_ready, b.constant(1, 0), b.binary(Op::BitOr, pending, in_valid)));
  const auto at = [&](ValueId token, unsigned i) {
    return i ? b.slice(token, i - 1, 1) : b.binary(Op::Equal, token, b.constant(slots - 1, 0));
  };
  for (const auto &[token, event] : {std::pair{arrival, opens}, std::pair{departure, closes}}) {
    const auto next =
        slots == 2 ? not_(token) : b.concat(b.slice(token, 0, slots - 2), at(token, 0));
    b.update(token, b.select(event, next, token));
  }
  auto crossed = b.constant(1, 1);
  for (unsigned k = 0; k < thresholds; ++k)
    crossed = b.concat(b.binary(Op::UnsignedLess, b.constant(3, k),
                                b.select(and_(not_(in_valid), idle), b.constant(3, 0), level)),
                       crossed);
  QosLevel result{{}, {pending, arrival, departure}};
  auto front = zero;
  for (unsigned i = 0; i < slots; ++i) {
    const auto entry = b.state(thresholds + 1, clock, reset);
    const auto active = b.slice(entry, 0, 1);
    const auto reserve = and_(at(arrival, i), opens);
    const auto release = and_(at(departure, i), closes);
    b.update(entry, b.select(release, zero,
                             b.select(active, b.binary(Op::BitOr, entry, crossed),
                                      b.select(reserve, crossed, zero))));
    front = b.binary(Op::BitOr, front, b.select(at(departure, i), entry, zero));
    result.words.push_back(entry);
  }
  result.value = b.select(and_(not_(in_valid), idle), b.constant(3, 0), level);
  for (unsigned k = 0; k < thresholds; ++k) {
    const auto higher = b.binary(Op::UnsignedLess, result.value, b.constant(3, k + 1));
    result.value =
        b.select(and_(b.slice(front, k + 1, 1), higher), b.constant(3, k + 1), result.value);
  }
  return result;
}
} // namespace nocloom::detail
