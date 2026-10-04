#include "endpoint_ports.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>
#include <string_view>

int main(int argc, char **argv) {
  if (argc != 3)
    return 2;
  const std::string_view mode(argv[1]);
  if (mode != "apb" && mode != "axi")
    return 2;
  nocloom::Definition d;
  d.schema_version = 10;
  d.name = "cross_tree_" + std::string(mode);
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"alpha", "clk_alpha", "reset_n", "test"},
                     {"beta", "clk_beta", "reset_n", "test"}};
  d.synchronizer_stages = 2;
  d.clock_crossings = {{"beta", "alpha", std::nullopt, 4}};
  d.initiators = {{"host0", "alpha"}, {"host1", "alpha"}};
  d.targets = {{"bank0", "alpha", {{0, 4096}}}, {"bank1", "beta", {{4096, 4096}}}};
  for (auto &source : d.initiators) {
    source.protocol = mode == "apb" ? nocloom::Protocol::Apb4 : nocloom::Protocol::Axi4Lite;
    source.data_width = 32;
  }
  for (auto &target : d.targets) {
    target.protocol = nocloom::Protocol::Apb4;
    target.data_width = 32;
  }
  d.transport = nocloom::TransportOptions{};
  d.transport->transport_word_bytes = 4;
  endpoint_ports(d, 4, 8);
  d.connectivity = nocloom::Connectivity{.all = true};
  const auto graph = nocloom::tree_topology(
      {{"root", {}, {"bank0"}, "alpha"}, {"leaf", "root", {"bank1"}, "beta"}}, {"host0", "host1"});
  if (!graph)
    return 3;
  d.topology = graph.value();
  const auto generated = nocloom::generate(d);
  if (!generated) {
    for (const auto &error : generated.diagnostics())
      std::cerr << error.code << ": " << error.message << '\n';
    return 4;
  }
  if (!std::filesystem::create_directory(argv[2]))
    return 5;
  for (const auto &file : generated.value()) {
    std::ofstream stream(std::filesystem::path(argv[2]) / file.path, std::ios::binary);
    stream << file.text;
    stream.close();
    if (!stream)
      return 6;
  }
  return 0;
}
