#pragma once
#include "hardware_builder.hpp"
#include "nocloom/result.hpp"
#include <map>
#include <string>
#include <utility>

namespace nocloom::detail {
// One lowered component: its own IR and the private bindings that identify
// actual generated values for verification and composition.
struct NodeHardware {
  HardwareIR ir;
  std::map<std::string, ValueId> bindings;
};
// The aligned block of 2^keep bytes at base holding every global address that reaches a target
// engine; keep zero leaves addresses whole.
struct AddressBlock {
  std::uint64_t base = 0;
  unsigned keep = 0;
  bool operator==(const AddressBlock &) const = default;
};
// The identity and burst transport widths that the per-endpoint component
// profiles (width cut and clamp, packetizer, upsize) take from the network
// plan (burst_fields, network_plan_profiles.cpp).
struct ComponentProfileBase {
  std::string name;
  unsigned id_bits = 0;
  unsigned len_bits = 8; // transport AxLEN width: 4..8
  unsigned address_bits = 32;
  unsigned source_bits = 0;
};
// Write ("w.") and read ("r.") state bits of a lowered component, for
// budgets; zero when the lowering failed.
inline std::pair<unsigned, unsigned> direction_state_bits(const Result<NodeHardware> &lowered) {
  std::pair<unsigned, unsigned> bits{};
  if (!lowered)
    return bits;
  const auto &ir = lowered.value().ir;
  for (const auto &[name, id] : lowered.value().bindings)
    if (ir.values.at(id.index).op == Op::State &&
        (name.starts_with("w.") || name.starts_with("r.")))
      (name.starts_with("w.") ? bits.first : bits.second) += ir.values.at(id.index).width;
  return bits;
}
} // namespace nocloom::detail
