#pragma once
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>

namespace tree_topology_fixture {
inline std::vector<nocloom::TreeNode> nodes() {
  return {{"branch_b", "root_node", {"t3"}},
          {"leaf_a", "branch_a", {"t2"}},
          {"root_node", std::nullopt, {"t0"}},
          {"branch_a", "root_node", {"t1"}}};
}
// Written independently of tree_topology: two directions of a four-node tree.
inline nocloom::ExchangeTopology explicit_graphs() {
  return {{{{"branch_a"}, {"branch_b"}, {"leaf_a"}, {"root_node"}},
           {{"branch_a", "leaf_a"}, {"root_node", "branch_a"}, {"root_node", "branch_b"}},
           {{"cpu", "root_node"}, {"dma", "root_node"}},
           {{"t0", "root_node"}, {"t1", "branch_a"}, {"t2", "leaf_a"}, {"t3", "branch_b"}}},
          {{{"branch_a"}, {"branch_b"}, {"leaf_a"}, {"root_node"}},
           {{"branch_a", "root_node"}, {"branch_b", "root_node"}, {"leaf_a", "branch_a"}},
           {{"t0", "root_node"}, {"t1", "branch_a"}, {"t2", "leaf_a"}, {"t3", "branch_b"}},
           {{"cpu", "root_node"}, {"dma", "root_node"}}}};
}
inline nocloom::Definition definition() {
  nocloom::Definition d;
  d.schema_version = 10;
  d.name = "tree_topology";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu", "main"}, {"dma", "main"}};
  d.targets = {{"t0", "main", {{0, 4096}}},
               {"t1", "main", {{4096, 4096}}},
               {"t2", "main", {{8192, 4096}}},
               {"t3", "main", {{12288, 4096}}}};
  for (auto &i : d.initiators) {
    i.protocol = nocloom::Protocol::Apb4;
    i.data_width = 32;
  }
  for (auto &t : d.targets) {
    t.protocol = nocloom::Protocol::Apb4;
    t.attributes = nocloom::TargetAttributes::amba();
    t.data_width = 32;
  }
  d.connectivity = nocloom::Connectivity{.all = true};
  return d;
}
} // namespace tree_topology_fixture
