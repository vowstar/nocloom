#pragma once
#include "hardware_builder.hpp"
#include <map>

namespace nocloom::detail {
// Import only reachable logic. State updates are deferred until boundaries are connected.
class IrComponent {
public:
  IrComponent(Builder &destination, const HardwareIR &source);
  void replace(ValueId original, ValueId replacement);
  void width(ValueId original, unsigned bits);
  ValueId get(ValueId original, unsigned bits = 0);
  void finish();

private:
  Builder &b_;
  const HardwareIR &ir_;
  std::map<std::size_t, ValueId> replacements_;
  std::map<std::size_t, unsigned> widths_;
  std::map<std::pair<std::size_t, unsigned>, ValueId> values_;
  struct Pending {
    ValueId original, imported;
    unsigned bits = 0;
  };
  std::vector<Pending> states_;
};
} // namespace nocloom::detail
