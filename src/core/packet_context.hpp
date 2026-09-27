#pragma once
#include "logic.hpp"
#include <bit>
#include <map>
#include <string>
#include <vector>

namespace nocloom::detail::packet_context {
using Fields = std::map<std::string, ValueId>;
// A source tag identifies a burst, but a packet splitter can put several
// packets of that burst in flight. Responses of one tag are ordered; other
// tags may return first. Each tag therefore owns a FIFO of small descriptors.
// The table holds no payload and reserves each descriptor before issue.
struct Table {
  unsigned tags = 0, depth = 0, count_bits = 0, pointer_bits = 0;
  std::vector<ValueId> count, head;
  std::vector<std::vector<Fields>> slots;
};
inline Table make(logic::Logic &l, const std::string &prefix, unsigned tags, unsigned depth,
                  const Fields &shape) {
  Table t{tags,
          depth,
          static_cast<unsigned>(std::bit_width(depth)),
          static_cast<unsigned>(std::bit_width(depth - 1)),
          {},
          {},
          {}};
  for (unsigned tag = 0; tag < tags; ++tag) {
    const auto name = prefix + ".t" + std::to_string(tag);
    t.count.push_back(l.state(name + ".count", t.count_bits));
    t.head.push_back(depth > 1 ? l.state(name + ".head", t.pointer_bits) : l.bit(0));
    std::vector<Fields> slots;
    for (unsigned slot = 0; slot < depth; ++slot) {
      Fields fields;
      for (const auto &[field, value] : shape)
        fields[field] = l.state_like(name + ".s" + std::to_string(slot) + "." + field, value);
      slots.push_back(std::move(fields));
    }
    t.slots.push_back(std::move(slots));
  }
  return t;
}
inline ValueId room(logic::Logic &l, const Table &t, ValueId tag) {
  auto ready = l.bit(0);
  for (unsigned k = 0; k < t.tags; ++k)
    ready = l.either(ready, l.both(l.eq(tag, k), l.b.binary(Op::UnsignedLess, t.count[k],
                                                            l.b.constant(t.count_bits, t.depth))));
  return ready;
}
inline ValueId valid(logic::Logic &l, const Table &t, ValueId tag) {
  auto present = l.bit(0);
  for (unsigned k = 0; k < t.tags; ++k)
    present = l.either(present, l.both(l.eq(tag, k), l.inv(l.eq(t.count[k], 0))));
  return present;
}
inline Fields front(logic::Logic &l, const Table &t, ValueId tag) {
  Fields result;
  for (const auto &[field, value] : t.slots.front().front()) {
    auto selected = l.b.constant(l.b.ir.values.at(value.index).width, 0);
    for (unsigned k = 0; k < t.tags; ++k) {
      auto head = t.slots[k][0].at(field);
      for (unsigned slot = 1; slot < t.depth; ++slot)
        head = l.b.select(l.eq(t.head[k], slot), t.slots[k][slot].at(field), head);
      selected = l.b.select(l.eq(tag, k), head, selected);
    }
    result[field] = selected;
  }
  return result;
}
// Whether no tag's FIFO holds `value` in `field` at its front: a fact of the table alone, so it
// holds for whichever tag answers next.
inline ValueId no_front(logic::Logic &l, const Table &t, const std::string &field, unsigned value) {
  auto none = l.bit(1);
  for (unsigned k = 0; k < t.tags; ++k) {
    auto head = t.slots[k][0].at(field);
    for (unsigned slot = 1; slot < t.depth; ++slot)
      head = l.b.select(l.eq(t.head[k], slot), t.slots[k][slot].at(field), head);
    none = l.both(none, l.either(l.eq(t.count[k], 0), l.inv(l.eq(head, value))));
  }
  return none;
}
// Read at an arbitrary cursor of each tag's FIFO, the cursor selected by tag.
inline Fields read_at(logic::Logic &l, const Table &t, ValueId tag, ValueId ptr) {
  Fields result;
  for (const auto &[field, value] : t.slots.front().front()) {
    auto selected = l.b.constant(l.b.ir.values.at(value.index).width, 0);
    for (unsigned k = 0; k < t.tags; ++k) {
      auto at = t.slots[k][0].at(field);
      for (unsigned slot = 1; slot < t.depth; ++slot)
        at = l.b.select(l.eq(ptr, slot), t.slots[k][slot].at(field), at);
      selected = l.b.select(l.eq(tag, k), at, selected);
    }
    result[field] = selected;
  }
  return result;
}
// The cursor after ptr, wrapping exactly at depth (any depth, not only
// powers of two like the pointer arithmetic inside step()).
inline ValueId next_ptr(logic::Logic &l, const Table &t, ValueId ptr) {
  return l.b.select(l.eq(ptr, t.depth - 1), l.b.constant(l.width(ptr), 0), l.inc(ptr));
}
// step() with an exactly wrapping head and tail: step() alone keeps the
// pointer arithmetic modulo 2^pointer_bits, which wraps early only for
// power-of-two depths; this variant wraps at any depth. One tag (0) only.
inline void step_exact(logic::Logic &l, Table &t, ValueId push, const Fields &input, ValueId pop) {
  const unsigned n = t.depth;
  const unsigned pb = t.pointer_bits;
  const auto count = t.count[0];
  if (n > 1) {
    // head + count, wrapped at the exact depth; both extended to the width
    // that holds 2n - 2.
    const unsigned wide = std::max(t.count_bits, pb + 1);
    const auto z = [&](ValueId v) {
      return l.width(v) < wide ? l.b.unary(Op::ZeroExtend, v, wide) : v;
    };
    auto tail = l.plus(z(t.head[0]), z(count));
    tail = l.b.select(l.b.binary(Op::UnsignedLess, tail, l.b.constant(wide, n)), tail,
                      l.b.binary(Op::Subtract, tail, l.b.constant(wide, n)));
    for (unsigned slot = 0; slot < n; ++slot)
      for (const auto &[field, state] : t.slots[0][slot])
        l.b.update(state, l.b.select(l.both(push, l.eq(tail, slot)), input.at(field), state));
    l.b.update(t.head[0], l.b.select(pop, next_ptr(l, t, t.head[0]), t.head[0]));
  } else {
    for (const auto &[field, state] : t.slots[0][0])
      l.b.update(state, l.b.select(push, input.at(field), state));
  }
  const unsigned cb = t.count_bits;
  l.b.update(count, l.plus(l.minus(count, l.fit(pop, cb)), l.fit(push, cb)));
}
inline void step(logic::Logic &l, const Table &t, ValueId accept, ValueId input_tag,
                 const Fields &input, ValueId complete, ValueId output_tag) {
  auto add = [&](ValueId a, ValueId b) {
    return l.b.binary(
        Op::Subtract, a,
        l.b.binary(Op::Subtract, l.b.constant(l.b.ir.values.at(a.index).width, 0), b));
  };
  for (unsigned k = 0; k < t.tags; ++k) {
    const auto push = l.both(accept, l.eq(input_tag, k));
    const auto pop = l.both(complete, l.eq(output_tag, k));
    const auto one = l.b.constant(t.count_bits, 1);
    l.b.update(t.count[k],
               l.b.select(l.both(push, l.inv(pop)), add(t.count[k], one),
                          l.b.select(l.both(pop, l.inv(push)),
                                     l.b.binary(Op::Subtract, t.count[k], one), t.count[k])));
    auto tail = l.bit(0);
    if (t.depth > 1) {
      tail = add(t.head[k], l.b.slice(t.count[k], 0, t.pointer_bits));
      l.b.update(t.head[k],
                 l.b.select(pop, add(t.head[k], l.b.constant(t.pointer_bits, 1)), t.head[k]));
    }
    for (unsigned slot = 0; slot < t.depth; ++slot)
      for (const auto &[field, state] : t.slots[k][slot])
        l.b.update(state, l.b.select(l.both(push, l.eq(tail, slot)), input.at(field), state));
  }
}
} // namespace nocloom::detail::packet_context
