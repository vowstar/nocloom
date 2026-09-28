#include "endpoint_ports.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
int main(int argc, char **argv) {
  if (argc != 4 && argc != 5)
    return 2;
  const unsigned selected = static_cast<unsigned>(std::stoul(argv[2]));
  const unsigned word = static_cast<unsigned>(std::stoul(argv[3]));
  nocloom::Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "ahb_public";
  d.address_width = 32;
  const unsigned source_width = argc == 5 ? static_cast<unsigned>(std::stoul(argv[4])) : 32;
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  for (auto &i : d.initiators) {
    i.protocol = nocloom::Protocol::Axi4;
    i.data_width = source_width;
  }
  d.targets = {{"t0", "main", {{0x1000, 4096}}}, {"t1", "main", {{0x5000, 4096}}}};
  for (auto &t : d.targets) {
    t.protocol = nocloom::Protocol::Axi4;
    t.data_width = 32;
  }
  if (selected > 3)
    return 2;
  for (unsigned i = 0; i < 2; ++i)
    if (selected >= 2 || selected == i)
      d.targets[i].protocol = nocloom::Protocol::AhbV3;
  // Selection 3 shapes the bursts: complete writes without undefined-length
  // INCR at t0, and no undefined-length INCR at t1, whose HRESP is the 2-bit
  // AMBA 2 encoding. The other selections keep the default 1-bit HRESP, which
  // the YAML of selection 2 spells out.
  if (selected == 3) {
    d.targets[0].write_bursts = nocloom::WriteBursts::Complete;
    d.targets[0].undefined_length_bursts = false;
    d.targets[1].undefined_length_bursts = false;
    d.targets[1].hresp_width = 2;
  }
  d.transport = nocloom::TransportOptions{.max_outstanding = 1, .transport_word_bytes = word};
  endpoint_ports(d, 4, 8);
  auto result = nocloom::generate(d);
  if (!result) {
    for (const auto &e : result.diagnostics())
      std::cerr << e.code << ": " << e.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(argv[1]);
  for (const auto &artifact : result.value()) {
    std::ofstream f(std::filesystem::path(argv[1]) / artifact.path);
    f << artifact.text;
    if (!f)
      return 1;
  }
  return 0;
}
