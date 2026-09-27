#pragma once
#include <nocloom/definition.hpp>

// A 32-bit AXI4 schema 10 endpoint in the main domain.
inline nocloom::Initiator graph_source(const std::string &name) {
  nocloom::Initiator i{name, "main"};
  i.protocol = nocloom::Protocol::Axi4;
  i.data_width = 32;
  i.len_width = 8;
  return i;
}
inline nocloom::Target graph_target(const std::string &name, nocloom::AddressWindow window) {
  nocloom::Target t{name, "main", {{window.base, window.size}}};
  t.protocol = nocloom::Protocol::Axi4;
  t.data_width = 32;
  t.len_width = 8;
  t.attributes = nocloom::TargetAttributes::amba();
  return t;
}
inline nocloom::Definition graph_example_definition(bool selective = false, bool mixed = false) {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.name = "graph";
  // The graph examples omit arbitration: the auto default (None).
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  ExchangeTopology topology;
  topology.request.nodes = {{"merge", 32}, {"branch", 16}, {"tail", 8}};
  topology.request.links = {{"merge", "branch", 4}, {"branch", "tail"}};
  // Names may be reused in the independent response graph.
  topology.response.nodes = {{"merge", 8}, {"branch", 32}};
  topology.response.links = {{"merge", "branch", 16}};
  for (unsigned i = 0; i < 3; ++i) {
    const auto source = "s" + std::to_string(i), target = "t" + std::to_string(i);
    const auto leaf = "leaf" + std::to_string(i);
    d.initiators.push_back(graph_source(source));
    d.targets.push_back(graph_target(target, {65536ULL * i, 65536}));
    topology.request.nodes.push_back({leaf});
    topology.request.links.push_back({leaf, "merge"});
    topology.request.entries.push_back({source, leaf});
    topology.request.exits.push_back({target, i == 0 ? "branch" : "tail"});
    topology.response.entries.push_back({target, "merge"});
    topology.response.exits.push_back({source, "branch"});
  }
  if (mixed) {
    d.initiators[0].data_width = 128;
    d.initiators[1].data_width = 32;
    d.initiators[1].protocol = Protocol::Axi4Lite;
    d.initiators[1].len_width.reset();
    d.initiators[2].data_width = 256;
  }
  d.connectivity = Connectivity{.all = !selective && !mixed};
  if (selective || mixed)
    for (unsigned i = 0; i < 3; ++i)
      for (unsigned j = 0; j < 2; ++j)
        d.connectivity->pairs.push_back(
            {"s" + std::to_string(i), "t" + std::to_string((i + j) % 3)});
  d.topology = std::move(topology);
  return d;
}
