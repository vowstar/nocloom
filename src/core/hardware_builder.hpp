#pragma once
#include "hardware.hpp"
#include <bit>
#include <cstdint>
#include <map>
#include <optional>
#include <utility>

namespace nocloom::detail {
class Builder {
public:
  HardwareIR ir;
  ValueId add(const Value &value) {
    const ValueId id{ir.values.size()};
    ir.values.push_back(value);
    return id;
  }
  ValueId input(std::string name, unsigned width, std::string endpoint, std::string signal) {
    const PortId id{ir.ports.size()};
    ir.ports.push_back({std::move(name), true, width, std::move(endpoint), std::move(signal)});
    return add({width, Op::Input, {}, 0, id});
  }
  void output(std::string name, ValueId value, std::string endpoint, std::string signal) {
    const PortId id{ir.ports.size()};
    ir.ports.push_back({std::move(name), false, ir.values.at(value.index).width,
                        std::move(endpoint), std::move(signal)});
    ir.drivers.push_back({id, value});
  }
  ValueId state(unsigned width, ValueId clock, ValueId reset_n, std::uint64_t initial = 0) {
    Value value{width, Op::State};
    value.state = {ir.states.size()};
    auto state_value = add(value);
    ir.states.push_back({state_value, state_value, clock, AsyncReset{reset_n, initial}});
    return state_value;
  }
  ValueId held_state(unsigned width, ValueId clock) {
    Value value{width, Op::State};
    value.state = {ir.states.size()};
    auto held = add(value);
    ir.states.push_back({held, held, clock, std::nullopt});
    return held;
  }
  // A state read back as {high, stored}; update() of the returned value stores the low bits of the
  // next value. high is a constant, or bits of other state that the next value's high bits always
  // equal (the caller's invariant).
  ValueId widened(ValueId stored, ValueId high) {
    const auto view = concat(high, stored);
    widened_states.emplace(view.index, std::pair{stored, 0U});
    return view;
  }
  // A state read back as {state, low zero bits}; update() of the returned value stores the bits
  // of the next value above them.
  ValueId zero_low(ValueId stored, unsigned low) {
    const auto view = concat(stored, constant(low, 0));
    widened_states.emplace(view.index, std::pair{stored, low});
    return view;
  }
  // A state of one bit read back as width copies of it, for a value whose bits are always equal;
  // update() of the returned value stores bit 0 of the next value.
  ValueId replicated(ValueId stored, unsigned width) {
    if (width == 1)
      return stored;
    auto view = stored;
    for (unsigned k = 1; k < width; ++k)
      view = concat(stored, view);
    widened_states.emplace(view.index, std::pair{stored, 0U});
    return view;
  }
  // A state of one bit read back as one of two width-bit constants lo != hi, for a value that only
  // takes those two: the bits where they differ follow the stored bit or its inverse; update() of
  // the returned value stores the lowest such bit of the next value (its value in hi when set).
  ValueId two_valued(ValueId stored, unsigned width, std::uint64_t lo, std::uint64_t hi) {
    const auto low = static_cast<unsigned>(std::countr_zero(lo ^ hi));
    const auto set = (hi >> low) & 1U;
    std::optional<ValueId> view;
    for (unsigned k = width; k-- > 0;) {
      const auto bit = ((lo ^ hi) >> k & 1U) == 0 ? constant(1, lo >> k & 1U)
                       : ((hi >> k & 1U) == set)  ? stored
                                                  : unary(Op::BitNot, stored, 1);
      view = view ? concat(*view, bit) : bit;
    }
    if (view->index != stored.index)
      widened_states.emplace(view->index, std::pair{stored, low});
    return *view;
  }
  void update(ValueId reg, ValueId next) {
    if (const auto it = widened_states.find(reg.index); it != widened_states.end()) {
      const auto [stored, low] = it->second;
      return update(stored, slice(next, low, ir.values.at(stored.index).width));
    }
    ir.states.at(ir.values.at(reg.index).state.index).next = next;
  }
  ValueId constant(unsigned width, std::uint64_t value) {
    return add({width, Op::Constant, {}, value, {}});
  }
  ValueId unary(Op op, ValueId arg, unsigned width) { return add({width, op, {arg, {}, {}}}); }
  ValueId binary(Op op, ValueId a, ValueId b) {
    return add({(op == Op::UnsignedLess || op == Op::Equal) ? 1 : ir.values.at(a.index).width,
                op,
                {a, b, {}}});
  }
  ValueId clock_gate(ValueId clock, ValueId enable, ValueId test) {
    return add({1, Op::ClockGate, {clock, enable, test}});
  }
  ValueId select(ValueId c, ValueId t, ValueId f) {
    return add({ir.values.at(t.index).width, Op::Select, {c, t, f}});
  }
  // Field extraction for endpoint lane framing: value[offset +: width].
  ValueId slice(ValueId value, unsigned offset, unsigned width) {
    return add({width, Op::Slice, {value, constant(32, offset), {}}});
  }
  // Field packing for endpoint lane framing: {hi, lo}.
  ValueId concat(ValueId hi, ValueId lo) {
    return add(
        {ir.values.at(hi.index).width + ir.values.at(lo.index).width, Op::Concat, {hi, lo, {}}});
  }

private:
  // A widened, zero_low or replicated view: its state and the bits below it.
  std::map<std::size_t, std::pair<ValueId, unsigned>> widened_states;
};
} // namespace nocloom::detail
