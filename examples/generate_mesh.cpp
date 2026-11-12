#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>

namespace {
// Serialize this example's explicit topology with ordinary schema fields.
void write_yaml(std::ostream &out, const nocloom::Definition &d) {
  out << "schema_version: 10\nname: mesh_axi\naddress_width: 32\n"
         "reset_ports: [{name: reset_n}]\ntest_ports: [{name: test}]\n"
         "clock_domains:\n"
         "  - {name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}\n"
         "arbitration: {policy: round_robin}\ninitiators:\n";
  for (const auto &source : d.initiators)
    out << "  - {name: " << source.name
        << ", clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 8}\n";
  out << "targets:\n";
  for (const auto &target : d.targets)
    out << "  - {name: " << target.name
        << ", clock_domain: main, protocol: axi4, data_width: 32, id_width: 4, len_width: 8, "
           "attributes: amba, mappings: [{base: \""
        << target.mappings.front().base << "\", size: \"4096\"}]}\n";
  out << "connectivity: all\noutput: {backend: verilog, language: verilog_2001}\ntopology:\n";
  for (const auto &[name, graph] :
       {std::pair{"request", &d.topology->request}, std::pair{"response", &d.topology->response}}) {
    out << "  " << name << ":\n    nodes:\n";
    for (const auto &node : graph->nodes)
      out << "      - {name: " << node.name << ", clock_domain: main}\n";
    out << "    links:\n";
    for (const auto &link : graph->links)
      out << "      - {from: " << link.from << ", to: " << link.to << "}\n";
    for (const auto &[role, attachments] :
         {std::pair{"entries", &graph->entries}, std::pair{"exits", &graph->exits}}) {
      out << "    " << role << ":\n";
      for (const auto &attachment : *attachments)
        out << "      - {endpoint: " << attachment.endpoint << ", node: " << attachment.node
            << "}\n";
    }
  }
  out << "  routes:\n";
  for (const auto &route : d.topology->routes) {
    out << "    - initiator: " << route.initiator << "\n      target: " << route.target << '\n';
    for (const auto &[name, path] :
         {std::pair{"request", &route.request}, std::pair{"response", &route.response}}) {
      out << "      " << name << ": [";
      for (std::size_t i = 0; i < path->size(); ++i)
        out << (i ? ", " : "") << (*path)[i];
      out << "]\n";
    }
  }
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  nocloom::Definition d;
  d.schema_version = 10;
  d.name = "mesh_axi";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  std::vector<nocloom::MeshEndpoint> sources, targets;
  for (unsigned row = 0; row < 3; ++row)
    for (unsigned column = 0; column < 3; ++column) {
      const auto suffix = std::to_string(row) + std::to_string(column);
      targets.push_back({"t" + suffix, row, column});
      d.targets.push_back({"t" + suffix, "main", {{(row * 3 + column) * 4096, 4096}}});
      if (row != 1 && column != 1) {
        sources.push_back({"i" + suffix, row, column});
        d.initiators.push_back({"i" + suffix, "main"});
      }
    }
  for (auto &source : d.initiators) {
    source.protocol = nocloom::Protocol::Axi4;
    source.data_width = 32;
    source.id_width = 2;
    source.len_width = 8;
  }
  for (auto &target : d.targets) {
    target.protocol = nocloom::Protocol::Axi4;
    target.data_width = 32;
    target.id_width = 4;
    target.len_width = 8;
    target.attributes = nocloom::TargetAttributes::amba();
  }
  d.connectivity = nocloom::Connectivity{.all = true};
  const auto topology = nocloom::mesh_topology(3, 3, sources, targets, *d.connectivity, "main");
  if (!topology) {
    for (const auto &diagnostic : topology.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  d.topology = topology.value();
  const auto result = nocloom::generate(d);
  if (!result) {
    for (const auto &diagnostic : result.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  const std::filesystem::path directory(argv[1]);
  if (!std::filesystem::create_directory(directory))
    return 3;
  for (const auto &artifact : result.value()) {
    std::ofstream stream(directory / artifact.path, std::ios::binary);
    stream << artifact.text;
    stream.close();
    if (!stream)
      return 3;
  }
  std::ofstream yaml(directory / "mesh_axi.yaml");
  write_yaml(yaml, d);
  yaml.close();
  return yaml ? 0 : 3;
}
