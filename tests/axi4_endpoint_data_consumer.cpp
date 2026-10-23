#include "endpoint_ports.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>

int main(int argc, char **argv) {
  // Arguments: out-dir width0 width1 full|lite.
  if (argc != 5)
    return 2;
  const int at = 2;
  nocloom::Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "axi4_public";
  d.address_width = 32;
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  for (auto &i : d.initiators)
    i.protocol = nocloom::Protocol::Axi4;
  d.initiators[0].data_width = static_cast<unsigned>(std::stoul(argv[at]));
  d.initiators[1].data_width = static_cast<unsigned>(std::stoul(argv[at + 1]));
  if (std::string(argv[at + 2]) == "lite")
    d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
  d.targets = {{"t0", "main", {{0, 32768}}}, {"t1", "main", {{65536, 32768}}}};
  for (auto &t : d.targets) {
    t.protocol = nocloom::Protocol::Axi4;
    t.data_width = 32;
  }
  d.transport = nocloom::TransportOptions{.max_outstanding = 1, .transport_word_bytes = 32};
  endpoint_ports(d, 4, 8);
  auto generated = nocloom::generate(d);
  if (!generated) {
    for (const auto &e : generated.diagnostics())
      std::cerr << e.code << ": " << e.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(argv[1]);
  for (const auto &artifact : generated.value()) {
    std::ofstream f(std::filesystem::path(argv[1]) / artifact.path);
    f << artifact.text;
    if (!f)
      return 1;
  }
}
