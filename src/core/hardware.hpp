#pragma once
#include "nocloom/definition.hpp"
#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace nocloom::detail {
struct ValueId {
  std::size_t index = 0;
};
struct PortId {
  std::size_t index = 0;
};
struct StateId {
  std::size_t index = 0;
};
enum class Op {
  Input,
  Constant,
  ZeroExtend,
  UnsignedLess,
  BitNot,
  BitAnd,
  BitOr,
  Select,
  Equal,
  State,
  Subtract,
  // Slice extracts [offset + width - 1 : offset] of its value operand; the
  // offset operand must be a Constant (the burst composition's lane framing).
  // Concat joins {operand 0, operand 1} with operand 0 in the high bits.
  Slice,
  Concat,
  // ClockGate gates operand 0 — a domain clock input or another gate rooted at one (a divided
  // domain gates its port once for the division and once per region) — by enable (operand 1)
  // ored with a test port input (operand 2), latched while the clock is low; its output only
  // clocks states. Emitted as an instance of the clock gate cell (clock_gate_cell), never gated
  // again beyond the division cascade.
  ClockGate
};
struct Value {
  unsigned width = 0;
  Op op = Op::Input;
  std::array<ValueId, 3> operands{};
  std::uint64_t literal = 0;
  PortId input_port{};
  StateId state{};
};
struct Port {
  std::string name;
  bool input = false;
  unsigned width = 0;
  std::string endpoint;
  std::string signal;
};
struct Driver {
  PortId port;
  ValueId value;
};
struct AsyncReset {
  ValueId signal;
  std::uint64_t value = 0;
};
struct State {
  ValueId output;
  ValueId next;
  ValueId clock;
  std::optional<AsyncReset> reset;
};
struct HardwareIR {
  std::string name;
  std::vector<Port> ports;
  std::vector<Value> values;
  std::vector<Driver> drivers;
  std::vector<State> states;
  // Every register of every synchronizer chain (ASYNC_REG in the emitted RTL).
  std::vector<ValueId> synchronizer_stages;
  // The emitted registers clear on a rising edge of their clock while their reset is low, instead
  // of at once (RegisterReset::Synchronous).
  bool synchronous_reset = false;
  // Set by canonicalize(): values [0, emitted) are the hardware. Later values
  // serve analysis and observation only and the Verilog backend leaves them
  // out. A caller that rewires the IR afterwards resets it.
  std::optional<std::size_t> emitted;
};
Status verify_structure(const HardwareIR &ir);
Status verify(const HardwareIR &ir);
std::string emit_verilog(const HardwareIR &ir);
// A synchronizer chain of the emitted hardware: stages registers of width bits on one clock,
// cleared by one reset, from head (sampling source) to tail. It is emitted as width one-bit
// synchronizer cell instances, instance(bit).
struct SynchronizerChain {
  std::size_t head = 0, tail = 0;
  unsigned stages = 0, width = 0;
  ValueId source, clock, reset;
  std::string instance(unsigned bit) const;
};
std::vector<SynchronizerChain> synchronizer_chains(const HardwareIR &ir);
// The cell wrapper files the emitted hardware instantiates (clock gate, synchronizer), each
// instantiating its bound library cell or holding the behavioral model, then a behavioral model
// of each bound library cell in use (library).
struct CellFile {
  std::string path, text;
  bool library = false;
};
std::vector<CellFile> cell_files(const HardwareIR &ir, const TechnologyCells &cells = {});
// The operand slots an operation reads; later slots hold no value.
unsigned operand_count(Op op);
// The domain clock behind a state clock: through every gate to the clock input.
inline ValueId root_clock(const HardwareIR &ir, ValueId clock) {
  while (ir.values.at(clock.index).op == Op::ClockGate)
    clock = ir.values.at(clock.index).operands[0];
  return clock;
}
// Marks a value removed by canonicalize().
inline constexpr std::size_t removed_value = static_cast<std::size_t>(-1);
// Rewrites the IR into a numbering that depends only on its structure, not on
// the order in which the Builder calls ran (C++ leaves argument evaluation
// order unspecified, and GCC and Clang choose differently), and removes logic
// that nothing uses. A root is visited depth-first, operands in operand order,
// numbering values in post-order. A state is a leaf there; its clock, reset
// and next value become later roots in that order. The hardware roots are the
// output drivers in port order, then `kept` in the given order; their
// closure is the emitted prefix [0, emitted). The `observed` roots (probes and
// analysis anchors) are closed over after it, so they never change the
// hardware numbering and the backend leaves their extra logic out. Values no
// root reaches are removed with the states they own; removed synchronizer
// stages leave the stage list. Returns the old-to-new index map
// (removed_value for removed values), or an empty vector when the reachable
// logic has a combinational cycle; the IR is then unchanged.
std::vector<std::size_t> canonicalize(HardwareIR &ir, const std::vector<ValueId> &kept,
                                      const std::vector<ValueId> &observed);
std::string decimal(std::uint64_t value);
} // namespace nocloom::detail
