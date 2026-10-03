#include "core/width_adapter.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

namespace {
// Single-fault injection on named observation bindings for the designated
// negative controls; each fault breaks exactly one transport responsibility.
bool inject(nocloom::detail::NodeHardware &serializer, nocloom::detail::NodeHardware &deserializer,
            const std::string &fault) {
  using nocloom::detail::Op;
  if (fault == "premature_last") {
    // ser.is_last compares the current group with itself, so the wide
    // fragment is released and `last` is marked on the first sub-fragment.
    auto &value = serializer.ir.values.at(serializer.bindings.at("ser.is_last").index);
    if (value.op != Op::Equal)
      return false;
    value.operands[1] = value.operands[0];
    return true;
  }
  if (fault == "lane_shift") {
    // The slot-0 group mux feeds the high group's first lane where the low
    // group's first lane belongs.
    auto &value = serializer.ir.values.at(serializer.bindings.at("ser.slot0.group_data").index);
    if (value.op != Op::Select)
      return false;
    std::swap(value.operands[1], value.operands[2]);
    return true;
  }
  if (fault == "drop_err") {
    // The completing fragment's error is dropped from the OR accumulation.
    auto &value = deserializer.ir.values.at(deserializer.bindings.at("des.error_or").index);
    if (value.op != Op::BitOr)
      return false;
    value.operands[1] = value.operands[0];
    return true;
  }
  return false;
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 4 && argc != 5)
    return 2;
  const unsigned wide = std::stoul(argv[2]);
  const unsigned narrow = std::stoul(argv[3]);
  const nocloom::detail::WidthAdapterParameters parameters{wide, narrow, false, 0, std::nullopt};
  auto serializer = nocloom::detail::lower_width_serializer(parameters);
  auto deserializer = nocloom::detail::lower_width_deserializer(parameters);
  if (!serializer || !deserializer) {
    for (const auto &d : serializer.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    for (const auto &d : deserializer.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (argc == 5 && !inject(serializer.value(), deserializer.value(), argv[4]))
    return 2;
  const std::filesystem::path out = argv[1];
  std::filesystem::create_directories(out);
  std::ofstream rtl(out / "adapter.v");
  rtl << nocloom::detail::emit_verilog(serializer.value().ir) << '\n'
      << nocloom::detail::emit_verilog(deserializer.value().ir);
  rtl.close();
  return rtl ? 0 : 1;
}
