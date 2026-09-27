#pragma once
#include "hardware_builder.hpp"
#include "qos_pipeline.hpp"
#include <functional>
#include <nocloom/definition.hpp>
#include <vector>

namespace nocloom::detail {
using PipeDemand = std::function<ValueId(const std::vector<ValueId> &)>;
struct PipeStage {
  ValueId ready, valid, push, pop;
  std::vector<ValueId> control, data, payload;
  std::vector<PressurePosition> positions{};
};
inline PipeStage lower_link_pipeline(Builder &b, ValueId clock, ValueId reset, ValueId valid,
                                     const std::vector<ValueId> &data, ValueId ready,
                                     const LinkPipeline &mode, bool reset_payload,
                                     std::optional<ValueId> bypass = std::nullopt,
                                     const PipeDemand &demand = {}) {
  const auto both = [&](ValueId a, ValueId c) { return b.binary(Op::BitAnd, a, c); };
  const auto inv = [&](ValueId a) { return b.unary(Op::BitNot, a, 1); };
  const auto position = [&](PipeStage &p, ValueId downstream, bool registered) {
    if (demand)
      p.positions.push_back({{demand(p.data), p.valid}, downstream, registered});
  };
  const auto slot = [&](ValueId load, ValueId value, ValueId held, PipeStage &p) {
    const auto width = b.ir.values.at(value.index).width;
    const auto q = reset_payload ? b.state(width, clock, reset) : b.held_state(width, clock);
    b.update(q, b.select(load, value, q));
    p.payload.push_back(q);
    return held.index == value.index ? q : b.select(held, q, value);
  };
  if (mode.half_rate) {
    PipeStage p{ready, valid, {}, {}, {}, data, {}};
    const auto full = b.state(1, clock, reset);
    const auto available = inv(full);
    p.ready = both(reset, bypass ? b.select(*bypass, ready, available) : available);
    const auto load = both(valid, p.ready);
    const auto stored_valid = both(reset, full);
    b.update(full, b.select(load, b.constant(1, 1),
                            b.select(both(stored_valid, ready), b.constant(1, 0), full)));
    std::vector<ValueId> stored;
    for (const auto value : data) {
      const auto q = slot(load, value, value, p);
      stored.push_back(bypass ? b.select(*bypass, value, q) : q);
    }
    p.control.push_back(full);
    p.valid = bypass ? b.select(*bypass, valid, stored_valid) : stored_valid;
    p.data = std::move(stored);
    p.push = both(valid, p.ready);
    p.pop = both(p.valid, ready);
    position(p, ready, true);
    return p;
  }
  PipeStage p{ready, valid, {}, {}, {}, data, {}};
  const auto full = mode.forward ? b.state(1, clock, reset) : ValueId{};
  const auto inner =
      !mode.forward
          ? ready
          : both(reset, bypass ? b.select(*bypass, ready, b.binary(Op::BitOr, inv(full), ready))
                               : b.binary(Op::BitOr, inv(full), ready));
  p.ready = inner;
  if (mode.backward) {
    const auto spare = b.state(1, clock, reset);
    p.ready = both(reset, inv(spare));
    p.valid = both(reset, b.binary(Op::BitOr, spare, valid));
    for (auto &value : p.data)
      value = slot(both(valid, p.ready), value, spare, p);
    b.update(spare, both(p.valid, inv(inner)));
    p.control.push_back(spare);
    position(p, inner, false);
  }
  const auto direct_valid = p.valid;
  const auto direct_data = p.data;
  if (mode.forward) {
    const auto load = both(p.valid, inner);
    p.valid = both(reset, full);
    b.update(full, b.select(load, b.constant(1, 1),
                            b.select(both(p.valid, ready), b.constant(1, 0), full)));
    for (auto &value : p.data)
      value = slot(load, value, value, p);
    p.control.insert(p.control.begin(), full);
  }
  if (bypass) {
    p.valid = b.select(*bypass, direct_valid, p.valid);
    for (std::size_t i = 0; i < p.data.size(); ++i)
      p.data[i] = b.select(*bypass, direct_data[i], p.data[i]);
  }
  p.push = both(valid, p.ready);
  p.pop = both(p.valid, ready);
  if (mode.forward)
    position(p, ready, true);
  return p;
}
} // namespace nocloom::detail
