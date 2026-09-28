#pragma once
#include "graph_example_definition.hpp"

inline nocloom::Definition graph_clocked_definition(bool twelve = false) {
  using namespace nocloom;
  auto d = graph_example_definition(true, true);
  // Matches examples/axi4_graph_clocked.yaml, including the clock periods and
  // the explicit middle-to-target depth.
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test", 10000},
                     {"middle", "clk_middle", "reset_n", "test", 14000},
                     {"target", "clk_target", "reset_n", "test", 22000}};
  d.synchronizer_stages = 2;
  d.clock_crossings = {{"main", "middle", std::nullopt, 4},
                       {"middle", "target", 4, 3},
                       {"target", "middle", std::nullopt, 3}};
  for (auto &t : d.targets)
    t.clock_domain = "target";
  for (auto *graph : {&d.topology->request, &d.topology->response})
    for (auto &node : graph->nodes)
      node.clock_domain = "middle";
  if (twelve) {
    // Twelve domains without periods or overrides: every crossing takes the
    // fallback depth and three synchronizer stages.
    d.clock_crossings.clear();
    d.synchronizer_stages = 3;
    d.reset_ports = {{"reset_n"}};
    d.test_ports = {{"test"}};
    d.clock_domains = {{"main", "clk_main", "reset_n", "test"},
                       {"source1", "clk_source1", "reset_n", "test"},
                       {"source2", "clk_source2", "reset_n", "test"},
                       {"target", "clk_target", "reset_n", "test"}};
    for (unsigned i = 1; i < 3; ++i)
      d.initiators[i].clock_domain = "source" + std::to_string(i);
    unsigned index = 0;
    for (auto *graph : {&d.topology->request, &d.topology->response})
      for (auto &node : graph->nodes) {
        const auto name = "node" + std::to_string(index);
        d.clock_domains.push_back({name, "clk_" + name, "reset_n", "test"});
        node.clock_domain = name;
        ++index;
      }
  }
  return d;
}
