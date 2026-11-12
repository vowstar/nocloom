#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
#include <nocloom/ipxact.hpp>
#include <nocloom/topology.hpp>
#ifdef NOCLOOM_BASIC_IO
#include <nocloom/io.hpp>
#endif
#include <string_view>

int main(int argc, char **argv) {
  // The optional fourth argument selects IP-XACT or C headers.
  if (argc != 4 && !(argc == 5 && (std::string_view(argv[4]) == "ipxact" ||
                                   std::string_view(argv[4]) == "c-header")))
    return 1;
  const std::string_view mode(argv[1]);
  if (mode != "apb_simple" && mode != "apb_shared" && mode != "axi_lite_apb" &&
      mode != "apb_domains")
    return 1;
  nocloom::Definition d;
  d.schema_version = 10;
  d.name = mode == "apb_simple"     ? "peripheral_bus"
           : mode == "apb_shared"   ? "shared_peripherals"
           : mode == "axi_lite_apb" ? "control_bridge"
                                    : "peripheral_domains";
  d.address_width = 32;
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  const bool crossing = mode == "apb_domains";
  const std::string source_clock = crossing ? "source" : "main";
  const std::string target_clock = crossing ? "target" : "main";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  if (crossing) {
    d.clock_domains = {{"source", "clk_source", "reset_n", "test"},
                       {"target", "clk_target", "reset_n", "test"}};
    d.synchronizer_stages = 2;
  } else
    d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  if (mode == "apb_simple") {
    d.initiators = {{"control", source_clock}};
    d.targets = {{"registers", target_clock, {{0x1000, 0x100}}},
                 {"counters", target_clock, {{0x2000, 0x100}}},
                 {"scratch", target_clock, {{0xfffff000, 0x1000}}}};
  } else {
    d.initiators = {{"cpu", source_clock}, {"dma", source_clock}};
    d.targets = {{"bank0", target_clock, {{0, 4096}}}, {"bank1", target_clock, {{4096, 4096}}}};
  }
  std::vector<std::string> sources, targets;
  for (auto &source : d.initiators) {
    source.protocol =
        mode == "axi_lite_apb" ? nocloom::Protocol::Axi4Lite : nocloom::Protocol::Apb4;
    source.data_width = 32;
    sources.push_back(source.name);
  }
  for (auto &target : d.targets) {
    target.protocol = nocloom::Protocol::Apb4;
    target.attributes = nocloom::TargetAttributes::amba();
    target.data_width = 32;
    targets.push_back(target.name);
  }
  // The API uses the tree helper; YAML spells out both independent graphs.
  const std::vector<nocloom::TreeNode> nodes =
      crossing ? std::vector<nocloom::TreeNode>{{"entry", {}, {}, source_clock},
                                                {"remote", "entry", targets, target_clock}}
               : std::vector<nocloom::TreeNode>{{"entry", {}, targets, source_clock}};
  const auto topology = nocloom::tree_topology(nodes, sources);
  if (!topology)
    return 2;
  d.topology = topology.value();
  d.connectivity = nocloom::Connectivity{.all = true};
#ifdef NOCLOOM_BASIC_IO
  const auto loaded = nocloom::io::load_definition(argv[3]);
  if (!loaded)
    return 3;
  d = loaded.value().definition;
#endif
  const bool c_header = argc == 5 && std::string_view(argv[4]) == "c-header";
  auto generated = nocloom::generate(d, {.c_header = c_header});
  if (generated && argc == 5 && !c_header)
    generated = nocloom::add_ipxact(d, generated.value());
  if (!generated) {
    for (const auto &diagnostic : generated.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 4;
  }
  if (generated.value().size() != (c_header    ? 6U + d.initiators.size()
                                   : argc == 5 ? 7U
                                               : 6U) ||
      !std::filesystem::create_directory(argv[2]))
    return 5;
  for (const auto &artifact : generated.value()) {
    std::ofstream stream(std::filesystem::path(argv[2]) / artifact.path, std::ios::binary);
    stream << artifact.text;
    stream.close();
    if (!stream)
      return 6;
  }
  return 0;
}
