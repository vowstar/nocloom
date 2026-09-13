#include "ir_component.hpp"
#include <stdexcept>

namespace nocloom::detail {
IrComponent::IrComponent(Builder &destination, const HardwareIR &source)
    : b_(destination), ir_(source) {}
void IrComponent::replace(ValueId id, ValueId replacement) {
  replacements_[id.index] = replacement;
}
void IrComponent::width(ValueId id, unsigned bits) { widths_[id.index] = bits; }
ValueId IrComponent::get(ValueId id, unsigned bits) {
  if (auto r = replacements_.find(id.index); r != replacements_.end())
    return r->second;
  const auto &v = ir_.values.at(id.index);
  if (widths_.contains(id.index))
    bits = widths_.at(id.index);
  if (!bits)
    bits = v.width;
  const auto key = std::pair{id.index, bits};
  if (values_.contains(key))
    return values_.at(key);
  ValueId result{};
  switch (v.op) {
  case Op::Input: {
    const auto &p = ir_.ports.at(v.input_port.index);
    if (!p.endpoint.starts_with("target:"))
      throw std::logic_error("Unconnected component input");
    result = b_.input(p.name, bits, p.endpoint, p.signal);
    break;
  }
  case Op::State: {
    const auto &s = ir_.states.at(v.state.index);
    auto clock = get(s.clock);
    result = s.reset ? b_.state(bits, clock, get(s.reset->signal), s.reset->value)
                     : b_.held_state(bits, clock);
    states_.push_back({id, result, bits});
    break;
  }
  case Op::Constant:
    result = b_.constant(bits, v.literal);
    break;
  case Op::ClockGate:
    result = b_.clock_gate(get(v.operands[0]), get(v.operands[1]), get(v.operands[2]));
    break;
  case Op::Select: {
    auto condition = get(v.operands[0]);
    auto yes = get(v.operands[1], bits), no = get(v.operands[2], bits);
    result = b_.select(condition, yes, no);
    break;
  }
  case Op::Slice: {
    const auto &offset = ir_.values.at(v.operands[1].index);
    if (offset.op != Op::Constant)
      throw std::logic_error("Component slice offset must be constant");
    result = b_.slice(get(v.operands[0]), static_cast<unsigned>(offset.literal), bits);
    break;
  }
  case Op::Concat: {
    auto hi = get(v.operands[0]), lo = get(v.operands[1]);
    if (bits != b_.ir.values.at(hi.index).width + b_.ir.values.at(lo.index).width)
      throw std::logic_error("Component concat width must equal its operand widths");
    result = b_.concat(hi, lo);
    break;
  }
  case Op::BitNot:
  case Op::ZeroExtend:
    result = b_.unary(v.op, get(v.operands[0]), bits);
    break;
  default: {
    auto a = get(v.operands[0]), c = get(v.operands[1]);
    result = b_.binary(v.op, a, c);
    break;
  }
  }
  values_[key] = result;
  return result;
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
