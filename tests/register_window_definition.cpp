#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>

int main(int argc, char **argv) {
  if (argc < 6)
    return 1;
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.name = "register_window";
  d.address_width = 32;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  d.initiators = {{"cfg", "main"}};
  d.initiators.front().protocol = Protocol::Apb4;
  d.initiators.front().data_width = 32;
  d.targets = {{"ram", "main", {{0, 0x1000}}},
               {"csr",
                "main",
                {{std::stoull(argv[2], nullptr, 0), std::stoull(argv[3], nullptr, 0),
                  std::stoull(argv[4], nullptr, 0)}}}};
  for (auto &t : d.targets) {
    t.protocol = Protocol::Apb4;
    t.data_width = 32;
    t.attributes = TargetAttributes::amba();
  }
  d.targets.back().log_registers = true;
  d.error_logging.logs = {{"errors", {"ram"}, std::nullopt, false, {}}};
  for (int k = 5; k < argc; ++k)
    d.error_logging.logs.front().loggers.push_back({.offset = std::stoull(argv[k], nullptr, 0)});
  d.connectivity = Connectivity{.all = true};
  const auto generated = generate(d);
  if (!generated) {
    for (const auto &diagnostic : generated.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 2;
  }
  const std::filesystem::path folder(argv[1]);
  if (!std::filesystem::create_directories(folder))
    return 3;
  for (const auto &artifact : generated.value()) {
    std::ofstream output(folder / artifact.path, std::ios::binary);
    output << artifact.text;
    output.close();
    if (!output)
      return 4;
  }
}
