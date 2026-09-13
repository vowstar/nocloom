#include "ir_component.hpp"
#include <stdexcept>
#include <utility>
#include <vector>

namespace nocloom::detail {
IrComponent::IrComponent(Builder &destination, const HardwareIR &source)
    : b_(destination), ir_(source) {}
void IrComponent::replace(ValueId id, ValueId replacement) {
  replacements_[id.index] = replacement;
}
void IrComponent::width(ValueId id, unsigned bits) { widths_[id.index] = bits; }
ValueId IrComponent::get(ValueId id, unsigned bits) {
  // Import the operands depth first, then the value itself. The explicit stack keeps a deep
  // operand chain, such as the decode of a large address map, off the call stack.
  std::vector<Frame> pending;
  Request request{id, bits};
  while (true) {
    ValueId result{};
    if (auto r = replacements_.find(request.id.index); r != replacements_.end()) {
      result = r->second;
    } else {
      if (auto w = widths_.find(request.id.index); w != widths_.end())
        request.bits = w->second;
      if (!request.bits)
        request.bits = ir_.values.at(request.id.index).width;
      const auto key = std::pair{request.id.index, request.bits};
      if (auto v = values_.find(key); v != values_.end()) {
        result = v->second;
      } else {
        auto current = visit(request);
        if (current.count) {
          pending.push_back(current);
          request = current.operands[0];
          continue;
        }
        result = values_.emplace(key, lower(current)).first->second;
      }
    }
    while (!pending.empty()) {
      auto &parent = pending.back();
      parent.imported[parent.next++] = result;
      if (parent.next < parent.count) {
        request = parent.operands[parent.next];
        break;
      }
      result = lower(parent);
      values_.emplace(std::pair{parent.value.id.index, parent.value.bits}, result);
      pending.pop_back();
    }
    if (pending.empty())
      return result;
  }
}

IrComponent::Frame IrComponent::visit(Request value) const {
  const auto &v = ir_.values.at(value.id.index);
  Frame out{value};
  const auto operand = [&](ValueId id, unsigned bits = 0) {
    out.operands[out.count++] = {id, bits};
  };
  switch (v.op) {
  case Op::State: {
    const auto &s = ir_.states.at(v.state.index);
    operand(s.clock);
    if (s.reset)
      operand(s.reset->signal);
    break;
  }
  case Op::ClockGate:
    // The import order numbers the values of the result: a gate imports its test input first.
    operand(v.operands[2]);
    operand(v.operands[1]);
    operand(v.operands[0]);
    break;
  case Op::Select:
    operand(v.operands[0]);
    operand(v.operands[1], value.bits);
    operand(v.operands[2], value.bits);
    break;
  case Op::Slice:
    if (ir_.values.at(v.operands[1].index).op != Op::Constant)
      throw std::logic_error("Component slice offset must be constant");
    operand(v.operands[0]);
    break;
  default:
    for (unsigned i = 0; i < operand_count(v.op); ++i)
      operand(v.operands[i]);
    break;
  }
  return out;
}

ValueId IrComponent::lower(const Frame &frame) {
  const auto [id, bits] = frame.value;
  const auto &v = ir_.values.at(id.index);
  const auto &[a, b, c] = frame.imported;
  switch (v.op) {
  case Op::Input: {
    const auto &p = ir_.ports.at(v.input_port.index);
    if (!p.endpoint.starts_with("target:"))
      throw std::logic_error("Unconnected component input");
    return b_.input(p.name, bits, p.endpoint, p.signal);
  }
  case Op::State: {
    const auto &s = ir_.states.at(v.state.index);
    const auto result = s.reset ? b_.state(bits, a, b, s.reset->value) : b_.held_state(bits, a);
    states_.push_back({id, result, bits});
    return result;
  }
  case Op::Constant:
    return b_.constant(bits, v.literal);
  case Op::ClockGate:
    return b_.clock_gate(c, b, a);
  case Op::Select:
    return b_.select(a, b, c);
  case Op::Slice: {
    const auto &offset = ir_.values.at(v.operands[1].index);
    return b_.slice(a, static_cast<unsigned>(offset.literal), bits);
  }
  case Op::Concat: {
    if (bits != b_.ir.values.at(a.index).width + b_.ir.values.at(b.index).width)
      throw std::logic_error("Component concat width must equal its operand widths");
    return b_.concat(a, b);
  }
  case Op::BitNot:
  case Op::ZeroExtend:
    return b_.unary(v.op, a, bits);
  default:
    return b_.binary(v.op, a, b);
  }
}
void IrComponent::finish() {
  for (std::size_t i = 0; i < states_.size(); ++i) {
    const auto pending = states_[i];
    const auto &old = ir_.states.at(ir_.values.at(pending.original.index).state.index);
    auto next = get(old.next, pending.bits);
    b_.update(pending.imported, next);
  }
}
} // namespace nocloom::detail
