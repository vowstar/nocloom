#include "hardware.hpp"
#include "diagnostic.hpp"
#include <algorithm>
#include <charconv>
#include <deque>
#include <iterator>
#include <set>

namespace nocloom::detail {
std::string decimal(std::uint64_t value) {
  std::array<char, 32> buffer{};
  auto result = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  return {buffer.data(), result.ptr};
}
namespace {
// Internal AXI burst endpoint payloads use up to 128 bits. Public profile
// admission remains separate from this structural ceiling. Literals and
// reset values are uint64_t and zero-extend into wider containers.
constexpr unsigned max_signal_width = 1024;
// The operands an operation reads, by enum order from Input to ClockGate, or nullopt for a value
// outside the enum.
std::optional<unsigned> arity(Op op) {
  constexpr std::array<unsigned, 14> count{0, 0, 1, 2, 1, 2, 2, 3, 2, 0, 2, 2, 2, 3};
  static_assert(count.size() == static_cast<std::size_t>(Op::ClockGate) + 1);
  const auto at = static_cast<std::size_t>(op);
  return at < count.size() ? std::optional<unsigned>(count[at]) : std::nullopt;
}
// A structural violation, thrown as the diagnostic that verify_structure returns.
[[noreturn]] void ir_fault(std::string field, std::string message) {
  throw error("NCL_INTERNAL_IR", std::move(field), std::move(message));
}
Status ir_fail(const std::string &field, const std::string &message) {
  return Status::failure(error("NCL_INTERNAL_IR", field, message));
}
bool port_is(const HardwareIR &ir, ValueId id, std::string_view endpoint, std::string_view signal) {
  const auto &v = ir.values[id.index];
  return v.op == Op::Input && v.input_port.index < ir.ports.size() &&
         ir.ports[v.input_port.index].endpoint.starts_with(endpoint) &&
         ir.ports[v.input_port.index].signal == signal;
}
// The width rules of an operation over its count (1 to 3) operands.
void check_operand_widths(const HardwareIR &ir, const Value &v, unsigned count) {
  const auto a = ir.values[v.operands[0].index].width;
  if ((v.op == Op::ZeroExtend && a >= v.width) || (v.op == Op::BitNot && a != v.width))
    ir_fault("hardware.values", "Invalid unary width");
  if (v.op == Op::Slice) {
    const auto &offset = ir.values[v.operands[1].index];
    if (offset.op != Op::Constant || offset.literal + v.width > a)
      ir_fault("hardware.values", "A slice needs a constant in-range offset");
  } else if (v.op == Op::Concat) {
    if (v.width != a + ir.values[v.operands[1].index].width)
      ir_fault("hardware.values", "A concatenation's width is the sum of its operands");
  } else if (count == 2) {
    auto width_b = ir.values[v.operands[1].index].width;
    if (a != width_b || v.width != ((v.op == Op::UnsignedLess || v.op == Op::Equal) ? 1 : a))
      ir_fault("hardware.values", "Invalid binary width");
  }
  if (count == 3 && (a != 1 || ir.values[v.operands[1].index].width != v.width ||
                     ir.values[v.operands[2].index].width != v.width))
    ir_fault("hardware.values", "Invalid select width");
}
// One value: its width, the state, port or literal of a leaf, operand order and operand widths.
void check_value(const HardwareIR &ir, std::size_t i) {
  const auto &v = ir.values[i];
  if (v.width == 0 || v.width > max_signal_width)
    ir_fault("hardware.values", "Invalid value width");
  if (v.op == Op::State &&
      (v.state.index >= ir.states.size() || ir.states[v.state.index].output.index != i))
    ir_fault("hardware.state", "Invalid state reference");
  if (v.op == Op::Input &&
      (v.input_port.index >= ir.ports.size() || !ir.ports[v.input_port.index].input ||
       ir.ports[v.input_port.index].width != v.width))
    ir_fault("hardware.values", "Invalid input reference");
  if (v.op == Op::Constant && v.width < 64 && v.literal >= (std::uint64_t{1} << v.width))
    ir_fault("hardware.values", "Constant exceeds its width");
  const auto count = arity(v.op);
  if (!count)
    ir_fault("hardware.values", "Unknown operation");
  for (unsigned j = 0; j < *count; ++j)
    if (v.operands[j].index >= i ||
        (ir.values[v.operands[j].index].op == Op::ClockGate && !(v.op == Op::ClockGate && j == 0)))
      ir_fault("hardware.values", "Operands must precede their user and not be clocks");
  if (v.op == Op::ClockGate) {
    // The gated clock: a domain clock input, or the gate of one (a domain's
    // division gate and then its region gates). The test operand is the
    // domain's test port, or a constant zero where the domain names none.
    auto clock = v.operands[0];
    while (ir.values[clock.index].op == Op::ClockGate)
      clock = ir.values[clock.index].operands[0];
    const auto &t = ir.values[v.operands[2].index];
    if (v.width != 1 || !port_is(ir, clock, "domain:", "clock") ||
        ir.values[v.operands[1].index].width != 1 ||
        !(port_is(ir, v.operands[2], "test:", "test") || (t.op == Op::Constant && t.literal == 0)))
      ir_fault("hardware.values", "A clock gate gates a domain clock under a test port");
  }
  if (*count != 0)
    check_operand_widths(ir, v, *count);
}
void check_states(const HardwareIR &ir) {
  std::set<std::size_t> state_outputs;
  for (std::size_t i = 0; i < ir.states.size(); ++i) {
    const auto &state = ir.states[i];
    if (state.output.index >= ir.values.size() || state.next.index >= ir.values.size() ||
        state.clock.index >= ir.values.size() ||
        (state.reset && state.reset->signal.index >= ir.values.size()))
      ir_fault("hardware.state", "Undefined state reference");
    const auto &q = ir.values[state.output.index];
    const auto &clock = ir.values[root_clock(ir, state.clock).index];
    if (q.op != Op::State || q.state.index != i ||
        !state_outputs.insert(state.output.index).second ||
        q.width != ir.values[state.next.index].width ||
        (state.reset && q.width < 64 && state.reset->value >= (std::uint64_t{1} << q.width)))
      ir_fault("hardware.state", "Invalid state width, binding or reset value");
    if (clock.op != Op::Input || clock.width != 1 ||
        (state.reset && ir.values[state.reset->signal.index].width != 1))
      ir_fault("hardware.state", "State clock must be an input bit and reset must be one bit");
    const auto &clock_port = ir.ports[clock.input_port.index];
    if (clock_port.signal != "clock" || !clock_port.endpoint.starts_with("domain:"))
      ir_fault("hardware.state", "Invalid state clock binding");
  }
}
} // namespace

unsigned operand_count(Op op) { return arity(op).value_or(2); }

std::vector<std::size_t> canonicalize(HardwareIR &ir, const std::vector<ValueId> &kept,
                                      const std::vector<ValueId> &observed) {
  const auto &old = ir.values;
  std::vector<std::size_t> mapped(old.size(), removed_value);
  std::vector<unsigned char> color(old.size(), 0);
  std::vector<std::size_t> order;
  std::deque<std::size_t> roots;
  // Iterative post-order: an explicit stack keeps deep operand chains off the
  // call stack. Each frame holds a value and its next operand position.
  std::vector<std::pair<std::size_t, unsigned>> stack;
  auto close = [&] {
    while (!roots.empty()) {
      const auto root = roots.front();
      roots.pop_front();
      if (color.at(root))
        continue;
      color[root] = 1;
      stack.push_back({root, 0});
      while (!stack.empty()) {
        const auto at = stack.back().first;
        const auto &v = old[at];
        if (stack.back().second < operand_count(v.op)) {
          const auto operand = v.operands[stack.back().second++].index;
          if (color.at(operand) == 1)
            return false;
          if (color[operand] == 0) {
            color[operand] = 1;
            stack.push_back({operand, 0});
          }
          continue;
        }
        stack.pop_back();
        color[at] = 2;
        mapped[at] = order.size();
        order.push_back(at);
        if (v.op == Op::State) {
          const auto &s = ir.states.at(v.state.index);
          roots.push_back(s.clock.index);
          if (s.reset)
            roots.push_back(s.reset->signal.index);
          roots.push_back(s.next.index);
        }
      }
    }
    return true;
  };
  auto drivers = ir.drivers;
  std::ranges::stable_sort(drivers, {}, [](const Driver &d) { return d.port.index; });
  const auto index_of = [](ValueId id) { return id.index; };
  std::ranges::transform(drivers, std::back_inserter(roots), index_of, &Driver::value);
  std::ranges::transform(kept, std::back_inserter(roots), index_of);
  if (!close())
    return {};
  const auto emitted = order.size();
  std::ranges::transform(observed, std::back_inserter(roots), index_of);
  if (!close())
    return {};
  auto remap = [&](ValueId &id) { id.index = mapped.at(id.index); };
  std::vector<Value> values;
  std::vector<State> states;
  values.reserve(order.size());
  for (const auto index : order) {
    auto v = old[index];
    const auto count = operand_count(v.op);
    for (unsigned k = 0; k < v.operands.size(); ++k)
      if (k < count)
        remap(v.operands[k]);
      else
        v.operands[k] = {};
    if (v.op == Op::State) {
      auto s = ir.states.at(v.state.index);
      remap(s.output);
      remap(s.next);
      remap(s.clock);
      if (s.reset)
        remap(s.reset->signal);
      v.state = {states.size()};
      states.push_back(s);
    }
    values.push_back(v);
  }
  ir.values = std::move(values);
  ir.states = std::move(states);
  ir.emitted = emitted;
  ir.drivers = std::move(drivers);
  for (auto &driver : ir.drivers)
    remap(driver.value);
  std::erase_if(ir.synchronizer_stages,
                [&](ValueId id) { return mapped.at(id.index) == removed_value; });
  for (auto &stage : ir.synchronizer_stages)
    remap(stage);
  std::ranges::sort(ir.synchronizer_stages, {}, &ValueId::index);
  return mapped;
}

Status verify_structure(const HardwareIR &ir) {
  try {
    if (ir.ports.empty() || ir.values.empty() || !ir.name.starts_with("nocloom_") ||
        !valid_name(std::string_view(ir.name).substr(8)))
      ir_fault("hardware", "Invalid module");
    std::set<std::string> names;
    std::set<std::pair<std::string, std::string>> bindings;
    if (std::any_of(ir.ports.begin(), ir.ports.end(), [&](const auto &port) {
          return port.width == 0 || port.width > max_signal_width ||
                 !names.insert(port.name).second ||
                 !bindings.emplace(port.endpoint, port.signal).second ||
                 !(port.name.starts_with("nl_") || valid_name(port.name)) ||
                 port.name.find_first_not_of(
                     "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_") !=
                     std::string::npos;
        }))
      ir_fault("hardware.ports", "Invalid port width, name or interface binding");
    for (std::size_t i = 0; i < ir.values.size(); ++i)
      check_value(ir, i);
    check_states(ir);
    std::vector<unsigned> driven(ir.ports.size(), 0);
    for (const auto &driver : ir.drivers) {
      if (driver.port.index >= ir.ports.size() || driver.value.index >= ir.values.size())
        ir_fault("hardware.drivers", "Undefined driver reference");
      const auto &port = ir.ports[driver.port.index];
      if (port.input || port.width != ir.values[driver.value.index].width ||
          ++driven[driver.port.index] != 1)
        ir_fault("hardware.drivers", "Invalid or duplicate output driver");
    }
    for (std::size_t i = 0; i < ir.ports.size(); ++i)
      if (!ir.ports[i].input && driven[i] != 1)
        ir_fault("hardware.drivers", "Undriven output");
  } catch (Diagnostic &d) {
    return Status::failure(std::move(d));
  }
  return Status::success({});
}

Status verify(const HardwareIR &ir) {
  if (auto status = verify_structure(ir); !status)
    return status;
  if (std::ranges::any_of(ir.states, [](const auto &state) { return !state.reset; }))
    return ir_fail("hardware.state", "This hardware profile requires reset on every state");
  for (const auto &state : ir.states) {
    const auto &reset = ir.values[state.reset->signal.index];
    if (reset.op != Op::Input)
      return ir_fail("hardware.state", "Single-domain reset must be an input");
    const auto &clock = ir.values[state.clock.index];
    const auto &clock_port = ir.ports[clock.input_port.index];
    const auto &reset_port = ir.ports[reset.input_port.index];
    if (reset_port.signal != "reset_n" || clock_port.endpoint != reset_port.endpoint ||
        state.clock.index != ir.states.front().clock.index ||
        state.reset->signal.index != ir.states.front().reset->signal.index)
      return ir_fail("hardware.state", "State clock and reset must belong to one domain");
  }
  if (!ir.synchronizer_stages.empty())
    return ir_fail("hardware.state", "Single-domain hardware has no synchronizer stages");
  return Status::success({});
}
} // namespace nocloom::detail
