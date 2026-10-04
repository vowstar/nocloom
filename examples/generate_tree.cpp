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
  if (mode != "apb" && mode != "axi" && mode != "apb16" && mode != "axi64")
    return 2;
  const bool axi = mode == "axi" || mode == "axi64";
  const bool small = mode == "apb16" || mode == "axi64";
  const unsigned width = mode == "apb16" ? 16 : mode == "axi64" ? 64 : 32;
  nocloom::Definition d;
  d.schema_version = 10;
  d.name = "control_tree_" + std::string(mode);
  d.address_width = 32;
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu", "main"}, {"dma", "main"}};
  d.targets = {{"bank00", "main", {{0, 4096}}}, {"bank01", "main", {{4096, 4096}}}};
  if (!small) {
    d.targets.push_back({"bank02", "main", {{16384, 8192}}});
    d.targets.push_back({"bank03", "main", {{0xfffff000, 4096}}});
  }
  for (auto &source : d.initiators) {
    source.protocol = axi ? nocloom::Protocol::Axi4Lite : nocloom::Protocol::Apb4;
    source.data_width = width;
  }
  for (auto &target : d.targets) {
    target.protocol = mode == "axi64" ? nocloom::Protocol::Axi4Lite : nocloom::Protocol::Apb4;
    target.attributes = nocloom::TargetAttributes::amba();
    target.data_width = width;
  }
  const std::vector<nocloom::TreeNode> nodes =
      small ? std::vector<nocloom::TreeNode>{{"entry", {}, {}},
                                             {"control", "entry", {"bank00"}},
                                             {"leaf", "entry", {"bank01"}}}
            : std::vector<nocloom::TreeNode>{{"entry", {}, {}},
                                             {"control", "entry", {"bank00", "bank03"}},
                                             {"branch", "entry", {}},
                                             {"leaf", "branch", {"bank01", "bank02"}}};
  const auto topology = nocloom::tree_topology(nodes, {"cpu", "dma"});
  if (!topology) {
    for (const auto &diagnostic : topology.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  d.topology = topology.value();
  d.connectivity = nocloom::Connectivity{.all = true};
  const auto result = nocloom::generate(d);
  if (!result) {
    for (const auto &diagnostic : result.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  // File I/O belongs to this application, outside the generation library.
  const std::filesystem::path directory(argv[2]);
  if (!std::filesystem::create_directory(directory))
    return 3;
  for (const auto &artifact : result.value()) {
    std::ofstream stream(directory / artifact.path, std::ios::binary);
    stream << artifact.text;
    stream.close();
    if (!stream)
      return 3;
  }
  return 0;
}
