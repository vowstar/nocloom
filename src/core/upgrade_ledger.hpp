#pragma once
#include "hardware_builder.hpp"
#include <algorithm>
#include <bit>
#include <span>
#include <vector>

namespace nocloom::detail {
struct UpgradeLedger {
  ValueId valid{}, route{}, level{}, cursor{}, selected{};
  ValueId raise{}, sent{}, taken{}, inside{}, data_held{}, prefer_control{}, control_held{};
  ValueId load{}, present{};
  std::vector<ValueId> wanted, acknowledged, head_sent, participants, dirty, words;
};
// A single tournament searches cyclically from cursor. Each subtree carries its first
// present index and whether that index is at or after cursor.
inline ValueId upgrade_pick(Builder &b, std::span<const ValueId> dirty, ValueId cursor,
                            ValueId &present) {
  const auto width = b.ir.values.at(cursor.index).width;
  const auto both = [&](ValueId a, ValueId z) { return b.binary(Op::BitAnd, a, z); };
  const auto either = [&](ValueId a, ValueId z) { return b.binary(Op::BitOr, a, z); };
  const auto inv = [&](ValueId a) { return b.unary(Op::BitNot, a, 1); };
  struct Pick {
    ValueId index, valid, after;
  };
  const auto search = [&](auto &self, unsigned first, unsigned size) -> Pick {
    if (size == 1) {
      const auto index = b.constant(width, first);
      return {index, dirty[first],
              both(dirty[first], inv(b.binary(Op::UnsignedLess, index, cursor)))};
    }
    const auto half = size / 2;
    const auto a = self(self, first, half), z = self(self, first + half, size - half);
    const auto right = either(both(inv(a.valid), z.valid), both(inv(a.after), z.after));
    return {b.select(right, z.index, a.index), either(a.valid, z.valid), either(a.after, z.after)};
  };
  const auto picked = search(search, 0, static_cast<unsigned>(dirty.size()));
  present = picked.valid;
  return picked.index;
}
inline UpgradeLedger lower_upgrade_ledger(Builder &b, ValueId clock, ValueId reset,
                                          ValueId data_valid, ValueId data_last, ValueId data_route,
                                          ValueId ready, unsigned path_bits,
                                          std::span<const ValueId> live,
                                          std::span<const ValueId> routes,
                                          std::span<const ValueId> finished,
                                          std::optional<ValueId> lock = std::nullopt) {
  const auto both = [&](ValueId a, ValueId z) { return b.binary(Op::BitAnd, a, z); };
  const auto either = [&](ValueId a, ValueId z) { return b.binary(Op::BitOr, a, z); };
  const auto inv = [&](ValueId a) { return b.unary(Op::BitNot, a, 1); };
  const auto eq = [&](ValueId a, ValueId z) { return b.binary(Op::Equal, a, z); };
  const auto state = [&](unsigned width) { return b.state(width, clock, reset); };
  const auto count = static_cast<unsigned>(live.size());
  const auto bits = std::max(1u, static_cast<unsigned>(std::bit_width(count - 1)));
  const auto route_bits = b.ir.values.at(data_route.index).width;
  UpgradeLedger u;
  u.valid = state(1);
  u.route = state(route_bits);
  u.level = state(3);
  u.cursor = state(bits);
  u.selected = state(bits);
  u.inside = state(1);
  u.data_held = state(1);
  u.prefer_control = state(1);
  u.control_held = state(1);
  u.words = {u.valid,  u.level,     u.cursor,         u.selected,
             u.inside, u.data_held, u.prefer_control, u.control_held};
  const auto path = [&](ValueId route) {
    return path_bits ? b.slice(route, route_bits - path_bits, path_bits) : b.constant(1, 0);
  };
  for (unsigned i = 0; i < count; ++i) {
    u.wanted.push_back(state(3));
    u.acknowledged.push_back(state(3));
    u.head_sent.push_back(state(1));
    u.participants.push_back(state(1));
    u.dirty.push_back(both(both(live[i], u.head_sent[i]),
                           b.binary(Op::UnsignedLess, u.acknowledged[i], u.wanted[i])));
    u.words.insert(u.words.end(),
                   {u.wanted[i], u.acknowledged[i], u.head_sent[i], u.participants[i]});
  }
  const auto picked = upgrade_pick(b, u.dirty, u.cursor, u.present);
  u.load = both(inv(u.valid), u.present);
  auto route = b.constant(route_bits, 0), level = b.constant(3, 0);
  for (unsigned i = 0; i < count; ++i) {
    const auto select = eq(picked, b.constant(bits, i));
    route = b.select(select, routes[i], route);
    level = b.select(select, u.wanted[i], level);
  }
  // Once submitted, a control word precedes a new head on that path. A data packet
  // already offered holds its class through its tail and any locked continuation.
  const auto same_path = both(data_valid, eq(path(data_route), path(u.route)));
  const auto choose = either(u.prefer_control, either(inv(data_valid), same_path));
  u.raise = either(u.control_held, both(both(inv(u.data_held), u.valid), choose));
  u.sent = both(u.raise, ready);
  u.taken = both(both(data_valid, inv(u.raise)), ready);
  const auto release = lock ? both(data_last, inv(*lock)) : data_last;
  b.update(u.inside, b.select(u.taken, inv(data_last), u.inside));
  const auto offered_data = both(data_valid, inv(u.raise));
  const auto data_end = both(u.taken, release);
  b.update(u.data_held, b.select(data_end, b.constant(1, 0), either(u.data_held, offered_data)));
  b.update(u.control_held, both(u.raise, inv(ready)));
  b.update(u.prefer_control, b.select(either(data_end, u.sent), data_end, u.prefer_control));
  b.update(u.valid, b.select(u.sent, b.constant(1, 0), either(u.valid, u.load)));
  b.update(u.route, b.select(u.load, route, u.route));
  b.update(u.level, b.select(u.load, level, u.level));
  b.update(u.selected, b.select(u.load, picked, u.selected));
  const auto next =
      b.select(eq(u.selected, b.constant(bits, count - 1)), b.constant(bits, 0),
               b.binary(Op::Subtract, u.selected, b.constant(bits, (1ULL << bits) - 1)));
  b.update(u.cursor, b.select(u.sent, next, u.cursor));
  for (unsigned i = 0; i < count; ++i) {
    const auto member = both(both(live[i], u.head_sent[i]), eq(path(routes[i]), path(route)));
    b.update(u.participants[i],
             both(inv(finished[i]), b.select(u.load, member, u.participants[i])));
  }
  return u;
}
inline void update_upgrade_context(Builder &b, const UpgradeLedger &u, unsigned i, ValueId live,
                                   ValueId allocated, ValueId launched, ValueId finished,
                                   ValueId head_level, ValueId required) {
  const auto both = [&](ValueId a, ValueId z) { return b.binary(Op::BitAnd, a, z); };
  const auto either = [&](ValueId a, ValueId z) { return b.binary(Op::BitOr, a, z); };
  const auto inv = [&](ValueId a) { return b.unary(Op::BitNot, a, 1); };
  const auto max = [&](ValueId a, ValueId z) {
    return b.select(b.binary(Op::UnsignedLess, a, z), z, a);
  };
  const auto active = both(live, u.head_sent[i]);
  b.update(u.head_sent[i],
           both(inv(finished), b.select(allocated, launched, either(u.head_sent[i], launched))));
  const auto wanted = b.select(launched, max(head_level, required),
                               b.select(active, max(u.wanted[i], required), u.wanted[i]));
  b.update(u.wanted[i], b.select(finished, b.constant(3, 0), wanted));
  const auto ack = both(both(u.sent, u.participants[i]), both(live, inv(finished)));
  b.update(u.acknowledged[i],
           b.select(launched, head_level,
                    b.select(ack, max(u.acknowledged[i], u.level), u.acknowledged[i])));
}
} // namespace nocloom::detail
