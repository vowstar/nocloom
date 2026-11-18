#include "core/exchange_composition.hpp"
#include "nocloom/io.hpp"
#include <iostream>
#include <nlohmann/json.hpp>
#include <stdexcept>
#include <utility>

namespace {
template <typename T> T take(nocloom::Result<T> result) {
  if (!result) {
    for (const auto &diagnostic : result.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    throw std::runtime_error("Could not lower the network");
  }
  return std::move(result).value();
}

nlohmann::json register_bindings(const nocloom::detail::ExchangeHardware &hardware) {
  using nocloom::detail::Op;
  const auto &ir = hardware.ir;
  auto names = nlohmann::json::object();
  auto inputs = nlohmann::json::object();
  auto storage = nlohmann::json::object();
  auto states = nlohmann::json::object();
  auto gates = nlohmann::json::object();
  for (const auto &[name, id] : hardware.bindings)
    names[name] = id.index;
  const auto owns = [&](const std::string &role,
                        const std::vector<nocloom::detail::ValueId> &words) {
    for (std::size_t i = 0; i < words.size(); ++i)
      storage[role + "/" + std::to_string(i)] = words[i].index;
  };
  for (const auto &item : hardware.storage) {
    owns(item.resource + "/payload", item.payload);
    owns(item.resource + "/control", item.control);
  }
  for (std::size_t id = 0; id < ir.emitted.value_or(ir.values.size()); ++id) {
    const auto &value = ir.values[id];
    if (value.op == Op::Input)
      inputs[std::to_string(id)] = ir.ports.at(value.input_port.index).name;
    if (value.op == Op::ClockGate)
      gates[std::to_string(id)] = value.operands[0].index;
    if (value.op != Op::State)
      continue;
    const auto &state = ir.states.at(value.state.index);
    nlohmann::json reset;
    if (state.reset)
      reset = {state.reset->signal.index, state.reset->value};
    states[std::to_string(id)] = {value.width, state.clock.index, reset};
  }
  return {{"reset_all", hardware.plan.reset_coverage == nocloom::ResetCoverage::All},
          {"inputs", std::move(inputs)},
          {"bindings", std::move(names)},
          {"storage", std::move(storage)},
          {"gates", std::move(gates)},
          {"states", std::move(states)}};
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  try {
    const auto input = take(nocloom::io::load_definition(argv[1]));
    const auto plan = take(nocloom::detail::build_network_plan(input.definition));
    const auto hardware =
        take(nocloom::detail::lower_axi4_exchange_composition(plan, input.options.register_reset));
    std::cout << register_bindings(hardware).dump() << '\n';
  } catch (const std::exception &exception) {
    std::cerr << exception.what() << '\n';
    return 1;
  }
}
