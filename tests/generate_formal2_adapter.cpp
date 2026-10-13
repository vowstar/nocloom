// Emits the formal2 inputs: the width adapter pair with binding metadata.
// Optional single-fault injection for designated negative controls.
// Deterministic: no timestamps, no host paths.
#include "core/width_adapter.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

using namespace nocloom::detail;

namespace {

nlohmann::json dump_ir(const HardwareIR &ir, const std::map<std::string, ValueId> &bindings) {
  nlohmann::json j;
  j["module"] = ir.name;
  for (const auto &[key, id] : bindings)
    j["bindings"][key] = id.index;
  for (const auto &p : ir.ports)
    j["ports"].push_back({{"name", p.name},
                          {"input", p.input},
                          {"width", p.width},
                          {"endpoint", p.endpoint},
                          {"signal", p.signal}});
  for (const auto &s : ir.states) {
    nlohmann::json v = {{"output", s.output.index},
                        {"next", s.next.index},
                        {"clock", s.clock.index},
                        {"width", ir.values[s.output.index].width}};
    v["reset"] = s.reset ? nlohmann::json(s.reset->signal.index) : nlohmann::json(nullptr);
    j["states"].push_back(v);
  }
  for (const auto &v : ir.values) {
    nlohmann::json row = {
        {"op", static_cast<unsigned>(v.op)},
        {"width", v.width},
        {"literal", v.literal},
        {"args", {v.operands[0].index, v.operands[1].index, v.operands[2].index}}};
    if (v.op == Op::Input)
      row["input_port"] = v.input_port.index;
    if (v.op == Op::State)
      row["state"] = v.state.index;
    j["values"].push_back(std::move(row));
  }
  return j;
}

// IR-level faults for the negative controls.  Each fault breaks exactly one
// hold-point discipline responsibility while keeping episodes reachable (a
// vacuously-true control is worthless).
bool inject_fault(HardwareIR &ir, const std::map<std::string, ValueId> &bindings,
                  const std::string &held_key, const std::string &fault, std::string &detail) {
  auto state_of = [&](const std::string &key) -> StateId {
    return ir.values.at(bindings.at(key).index).state;
  };
  auto const_zero = [&]() -> ValueId {
    Value v;
    v.width = 1;
    v.op = Op::Constant;
    v.literal = 0;
    ir.values.push_back(v);
    return ValueId{ir.values.size() - 1};
  };
  if (fault == "drop_hold_term") {
    // The holder abandons its episode silently: the hold term of the next-state
    // Sel chain is replaced by 0, so a held point with no event clears itself.
    const auto held = bindings.at(held_key);
    const auto st = state_of(held_key);
    const auto next = ir.states.at(st.index).next;
    ValueId at = next;
    while (true) {
      const auto &v = ir.values.at(at.index);
      if (v.op != Op::Select)
        return false;
      if (v.operands[2].index == held.index) {
        const auto zero = const_zero();
        ir.values.at(at.index).operands[2] = zero;
        detail = held_key + " hold term dropped from the next-state select";
        return true;
      }
      at = v.operands[2];
    }
  }
  return false;
}

int run_adapter(unsigned wide, unsigned narrow, const std::string &out_dir,
                const std::string &fault) {
  const WidthAdapterParameters parameters{wide, narrow, false, 0};
  auto serializer = lower_width_serializer(parameters);
  auto deserializer = lower_width_deserializer(parameters);
  if (!serializer || !deserializer) {
    for (const auto &d : serializer.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    for (const auto &d : deserializer.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (!fault.empty()) {
    // Transport-level faults identical to those of the width adapter test,
    // plus hold-episode faults for the formal2 properties.
    if (fault == "premature_last") {
      auto &value =
          serializer.value().ir.values.at(serializer.value().bindings.at("ser.is_last").index);
      if (value.op != Op::Equal)
        return 2;
      value.operands[1] = value.operands[0];
    } else if (fault == "lane_shift") {
      auto &value = serializer.value().ir.values.at(
          serializer.value().bindings.at("ser.slot0.group_data").index);
      if (value.op != Op::Select)
        return 2;
      std::swap(value.operands[1], value.operands[2]);
    } else if (fault == "drop_err") {
      auto &value =
          deserializer.value().ir.values.at(deserializer.value().bindings.at("des.error_or").index);
      if (value.op != Op::BitOr)
        return 2;
      value.operands[1] = value.operands[0];
    } else if (fault == "des_drop_hold") {
      std::string detail;
      if (!inject_fault(deserializer.value().ir, deserializer.value().bindings, "des.have_low",
                        "drop_hold_term", detail))
        return 2;
      std::cerr << "fault: " << fault << ": " << detail << '\n';
    } else {
      std::string detail;
      if (!inject_fault(serializer.value().ir, serializer.value().bindings, "ser.started", fault,
                        detail))
        return 2;
      std::cerr << "fault: " << fault << ": " << detail << '\n';
    }
  }
  const auto out = std::filesystem::path(out_dir);
  std::filesystem::create_directories(out);
  std::ofstream rtl(out / "adapter.v");
  rtl << emit_verilog(serializer.value().ir) << '\n' << emit_verilog(deserializer.value().ir);
  rtl.close();
  nlohmann::json j;
  j["serializer"] = dump_ir(serializer.value().ir, serializer.value().bindings);
  j["deserializer"] = dump_ir(deserializer.value().ir, deserializer.value().bindings);
  std::ofstream(out / "bindings.json") << j.dump(2) << '\n';
  return rtl ? 0 : 1;
}

} // namespace

int main(int argc, char **argv) {
  if (argc < 3)
    return 2;
  const std::string mode = argv[1];
  if (mode == "adapter" && (argc == 5 || argc == 6))
    return run_adapter(std::stoul(argv[2]), std::stoul(argv[3]), argv[4], argc == 6 ? argv[5] : "");
  return 2;
}
