#pragma once
#include "hardware.hpp"
#include <cstdint>
#include <map>
#include <optional>
#include <vector>

namespace nocloom::detail {
// Evaluates combinational cones of one IR with the given state words and
// input ports (by state and port index; absent ones read zero). Only the
// values a cone reaches are computed. A value wider than 64 bits, or a
// slice of one, inside the cone makes the result unknown (nullopt): callers
// use it on control, ID and address decode cones. A pinned value is
// taken as given, cutting off its own cone. In strict mode an absent state
// or input is unknown instead of zero, so a result proves the cone depends
// only on the given leaves. The memo is a vector stamped per call, so an
// evaluator reused across calls allocates once.
class ConeEvaluator {
public:
  using Words = std::map<std::size_t, std::uint64_t>;
  explicit ConeEvaluator(const HardwareIR &hardware)
      : ir(hardware), stamp(hardware.values.size()), word(stamp.size()) {}
  std::optional<std::uint64_t> operator()(std::size_t root, const Words &state_words,
                                          const Words &input_words = {}, const Words &pinned = {},
                                          bool strict_leaves = false) {
    ++call;
    states = &state_words;
    inputs = &input_words;
    strict = strict_leaves;
    for (const auto &[index, value] : pinned)
      if (index < stamp.size())
        remember(index, value);
    if (const auto it = pinned.find(root); it != pinned.end())
      return it->second;
    return at(root);
  }

private:
  const HardwareIR &ir;
  std::vector<std::uint64_t> stamp; // the call that computed each value, 0 never
  std::vector<std::optional<std::uint64_t>> word;
  std::uint64_t call = 0; // does not wrap
  const Words *states = nullptr, *inputs = nullptr;
  bool strict = false;

  std::optional<std::uint64_t> remember(std::size_t index, std::optional<std::uint64_t> value) {
    stamp[index] = call;
    return word[index] = value;
  }
  std::optional<std::uint64_t> leaf(const Words &words, std::size_t index) const {
    if (const auto it = words.find(index); it != words.end())
      return it->second;
    return strict ? std::nullopt : std::optional<std::uint64_t>{0};
  }
  std::optional<std::uint64_t> at(std::size_t index) {
    if (stamp.at(index) == call)
      return word[index];
    const auto &v = ir.values[index];
    if (v.width > 64)
      return remember(index, std::nullopt);
    const auto mask = v.width == 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << v.width) - 1;
    auto operand = [&](unsigned k) { return at(v.operands[k].index); };
    std::optional<std::uint64_t> result;
    switch (v.op) {
    case Op::Input:
      result = leaf(*inputs, v.input_port.index);
      break;
    case Op::Constant:
      result = v.literal;
      break;
    case Op::State:
      result = leaf(*states, v.state.index);
      break;
    case Op::ZeroExtend:
      result = operand(0);
      break;
    case Op::BitNot:
      if (auto a = operand(0))
        result = ~*a;
      break;
    case Op::Select:
      if (const auto c = operand(0))
        result = *c ? operand(1) : operand(2);
      break;
    case Op::UnsignedLess:
    case Op::BitAnd:
    case Op::BitOr:
    case Op::Equal:
    case Op::Subtract: {
      const auto a = operand(0), b = operand(1);
      if (!a || !b)
        break;
      result = v.op == Op::UnsignedLess ? std::uint64_t{*a < *b}
               : v.op == Op::BitAnd     ? *a & *b
               : v.op == Op::BitOr      ? *a | *b
               : v.op == Op::Equal      ? std::uint64_t{*a == *b}
                                        : *a - *b;
      break;
    }
    case Op::Slice: {
      // value[offset +: width] of an operand that fits 64 bits.
      const auto &operand0 = ir.values.at(v.operands[0].index);
      const auto a = operand0.width <= 64 ? operand(0) : std::nullopt;
      const auto offset = operand(1);
      if (a && offset && *offset < 64)
        result = *a >> *offset;
      break;
    }
    case Op::Concat: {
      // {hi, lo} with a result of at most 64 bits (checked above).
      const auto hi = operand(0), lo = operand(1);
      if (hi && lo)
        result = (*hi << ir.values.at(v.operands[1].index).width) | *lo;
      break;
    }
    case Op::ClockGate: // a clock carries no data value
      break;
    }
    return remember(index, result ? std::optional{*result & mask} : std::nullopt);
  }
};
// One evaluation with a fresh evaluator.
inline std::optional<std::uint64_t>
evaluate_cone(const HardwareIR &ir, std::size_t root,
              const std::map<std::size_t, std::uint64_t> &states,
              const std::map<std::size_t, std::uint64_t> &inputs = {},
              const std::map<std::size_t, std::uint64_t> &pinned = {}, bool strict = false) {
  return ConeEvaluator(ir)(root, states, inputs, pinned, strict);
}
} // namespace nocloom::detail
