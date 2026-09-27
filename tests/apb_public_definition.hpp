#pragma once
#include "endpoint_ports.hpp"
#include <nocloom/definition.hpp>

inline nocloom::Definition apb_public_definition(const std::string &mode) {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "apb_public";
  d.address_width = 32;
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
  }
  d.targets = {{"t0", "main", {{0x1000, 4096}}},
               {"t1", "main", {{0x5000, 4096}}},
               {"t2", "main", {{0x9000, 4096}}}};
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  d.targets[1].protocol = Protocol::Apb4;
  d.targets[2].protocol = Protocol::AhbV3;
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  endpoint_ports(d, 4, 8);
  if (mode == "funnel")
    d.targets.resize(2);
  if (mode != "funnel") {
    ExchangeTopology t;
    t.request.nodes = {{"q0", 16}, {"q1", 8}};
    t.request.links = {{"q0", "q1", 4}};
    t.request.entries = {{"s0", "q0"}, {"s1", "q0"}};
    t.request.exits = {{"t0", "q1"}, {"t1", "q1"}, {"t2", "q1"}};
    t.response.nodes = {{"p0", 8}, {"p1", 16}};
    t.response.links = {{"p0", "p1", 4}};
    t.response.entries = {{"t0", "p0"}, {"t1", "p0"}, {"t2", "p0"}};
    t.response.exits = {{"s0", "p1"}, {"s1", "p1"}};
    if (mode == "clocked") {
      d.reset_ports = {{"reset_n"}};
      d.test_ports = {{"test"}};
      d.clock_domains = {{"main", "clk_main", "reset_n", "test"},
                         {"middle", "clk_middle", "reset_n", "test"},
                         {"target", "clk_target", "reset_n", "test"}};
      d.clock_crossings = {{"main", "middle", std::nullopt, 4},
                           {"middle", "target", std::nullopt, 3}};
      d.targets[1].clock_domain = "target";
      t.request.nodes[0].clock_domain = "main";
      t.request.nodes[1].clock_domain = "middle";
      t.response.nodes[0].clock_domain = "middle";
      t.response.nodes[1].clock_domain = "main";
    }
    d.topology = std::move(t);
  }
  return d;
}
