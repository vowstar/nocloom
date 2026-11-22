// The emitted hardware as plain text records, for tools that check the Verilog against it.
#include "backends/output_license.hpp"
#include "core/hardware.hpp"
#include <stdexcept>

namespace nocloom::detail {
namespace {
const char *op_name(Op op) {
  switch (op) {
  case Op::Input:
    return "input";
  case Op::Constant:
    return "constant";
  case Op::ZeroExtend:
    return "zero_extend";
  case Op::UnsignedLess:
    return "unsigned_less";
  case Op::BitNot:
    return "bit_not";
  case Op::BitAnd:
    return "bit_and";
  case Op::BitOr:
    return "bit_or";
  case Op::Select:
    return "select";
  case Op::Equal:
    return "equal";
  case Op::State:
    return "state";
  case Op::Subtract:
    return "subtract";
  case Op::Slice:
    return "slice";
  case Op::Concat:
    return "concat";
  case Op::ClockGate:
    return "clock_gate";
  }
  throw std::logic_error("Unverified hardware operation");
}
} // namespace

// Values, states and synchronizer stages are the emitted prefix [0, emitted) that emit_verilog
// prints. Values and ports are numbered by line order from 0; states are numbered by the order of
// their values, so state k is the k-th value whose operation is state.
std::string emit_netlist(const HardwareIR &ir) {
  const auto count = ir.emitted.value_or(ir.values.size());
  std::string out = output_license_comment("#") + "netlist 1\nmodule " + ir.name + "\nreset " +
                    (ir.synchronous_reset ? "synchronous" : "asynchronous") + "\n";
  for (const auto &port : ir.ports)
    out += std::string("port ") + (port.input ? "input " : "output ") + decimal(port.width) + " " +
           port.name + "\n";
  std::vector<std::size_t> state_value;
  for (std::size_t i = 0; i < count; ++i) {
    const auto &v = ir.values[i];
    out += "value " + decimal(v.width) + " " + op_name(v.op);
    if (v.op == Op::Input)
      out += " " + decimal(v.input_port.index);
    else if (v.op == Op::Constant)
      out += " " + decimal(v.literal);
    else if (v.op == Op::State) {
      out += " " + decimal(state_value.size());
      state_value.push_back(i);
    } else
      for (unsigned k = 0; k < operand_count(v.op); ++k)
        out += " " + decimal(v.operands[k].index);
    out += "\n";
  }
  for (const auto i : state_value) {
    const auto &s = ir.states.at(ir.values[i].state.index);
    out += "state " + decimal(s.output.index) + " " + decimal(s.next.index) + " " +
           decimal(s.clock.index);
    if (s.reset)
      out += " " + decimal(s.reset->signal.index) + " " + decimal(s.reset->value);
    out += "\n";
  }
  for (const auto &driver : ir.drivers)
    out += "driver " + decimal(driver.port.index) + " " + decimal(driver.value.index) + "\n";
  for (const auto stage : ir.synchronizer_stages)
    if (stage.index < count)
      out += "synchronizer " + decimal(stage.index) + "\n";
  return out + "end\n";
}
} // namespace nocloom::detail
