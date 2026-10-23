#include "endpoint_ports.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>

int main(int argc, char **argv) {
  if (argc != 4 && argc != 5)
    return 2;
  const auto width = static_cast<unsigned>(std::stoul(argv[2]));
  nocloom::Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "axi4_public";
  d.address_width = static_cast<unsigned>(std::stoul(argv[3]));
  unsigned target_width = width >= 128 ? 32 : width;
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  const bool downsize = argc == 5 && std::string(argv[4]) == "downsize";
  if (downsize)
    target_width = 32;
  for (auto &i : d.initiators) {
    i.protocol = nocloom::Protocol::Axi4;
    i.data_width = width;
  }
  const bool heterogeneous = argc == 5 && std::string(argv[4]) == "heterogeneous";
  if (argc == 5 && std::string(argv[4]) == "mixed")
    d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
  const bool high = d.address_width > 32 && !heterogeneous;
  const auto base0 = high ? std::uint64_t{1} << 32 : 0;
  const auto base1 = high ? (width == 32 ? std::uint64_t{1} << (d.address_width - 1)
                                         : (std::uint64_t{1} << d.address_width) - 65536)
                          : 65536;
  const auto size = high && width == 64 ? 65536U : 32768U;
  d.targets = {{"t0", "main", {{base0, size}}}, {"t1", "main", {{base1, size}}}};
  for (auto &t : d.targets) {
    t.protocol = nocloom::Protocol::Axi4;
    t.data_width = target_width;
  }
  if (heterogeneous) {
    d.initiators[0].address_width = 40;
    d.initiators[1].address_width = 32;
    d.targets[0].address_width = 16;
    d.targets[1].address_width = 32;
    d.targets[1].mappings[0].base = std::uint64_t{1} << 32;
    // t1 lies above s1's address space.
    d.connectivity = nocloom::Connectivity{.pairs = {{"s0", "t0"}, {"s0", "t1"}, {"s1", "t0"}}};
  }
  d.transport = nocloom::TransportOptions{.max_outstanding = 1,
                                          .transport_word_bytes = width == 64 ? 4U : 32U};
  endpoint_ports(d, width == 64 ? 0U : 4U, width == 64 ? 4U : 8U);
  if (downsize) {
    d.transport = nocloom::TransportOptions{.max_outstanding = 1, .transport_word_bytes = 32};
    endpoint_ports(d, 4, 8);
  }
  for (auto &t : d.targets)
    t.id_width = width == 64 && !downsize ? 0U : 4U;
  auto generated = nocloom::generate(d);
  if (!generated) {
    for (const auto &e : generated.diagnostics())
      std::cerr << e.code << ": " << e.message << '\n';
    return 1;
  }
  std::filesystem::path out(argv[1]);
  std::filesystem::create_directories(out);
  for (const auto &artifact : generated.value()) {
    std::ofstream file(out / artifact.path);
    file << artifact.text;
    if (!file)
      return 1;
  }
  return 0;
}
