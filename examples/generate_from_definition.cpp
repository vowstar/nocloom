#include <iostream>
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>
int main() {
  nocloom::Definition definition;
  definition.schema_version = 10;
  definition.name = "embedded_bus";
  definition.reset_ports.push_back({"reset_n"});
  definition.test_ports.push_back({"test"});
  definition.clock_domains.push_back({"main", "clk_main", "reset_n", "test"});
  definition.initiators.push_back({"control", "main"});
  definition.targets.push_back({"registers", "main", {{0x1000, 0x100}}});
  definition.initiators.front().protocol = nocloom::Protocol::Apb4;
  definition.initiators.front().data_width = 32;
  definition.targets.front().protocol = nocloom::Protocol::Apb4;
  definition.targets.front().attributes = nocloom::TargetAttributes::amba();
  definition.targets.front().data_width = 32;
  const auto topology = nocloom::tree_topology({{"entry", {}, {"registers"}}}, {"control"});
  if (!topology)
    return 1;
  definition.topology = topology.value();
  definition.connectivity = nocloom::Connectivity{.all = true};
  auto artifacts = nocloom::generate(definition);
  if (!artifacts) {
    std::cerr << artifacts.diagnostics().front().message << '\n';
    return 1;
  }
  std::cout << artifacts.value().size() << " artifacts generated in memory\n";
  return artifacts.value().size() == 6 ? 0 : 1;
}
