#pragma once

#include "nocloom/definition.hpp"
#include "nocloom/result.hpp"

namespace nocloom {
// Convert a rooted tree to explicit request and reverse response graphs.
// Accepts 1..128 nodes, matching the explicit graph structure limit.
// Every named initiator attaches to the unique root. Targets may attach to
// any node; every declared node must carry a route. Input order does not
// affect the result, and clock-domain fields are copied without defaults.
// This checks structure only. The complete Definition still validates actual
// endpoints, clock domains, connectivity, protocols and resource limits.
// No file I/O or console side effects.
Result<ExchangeTopology> tree_topology(const std::vector<TreeNode> &nodes,
                                       const std::vector<std::string> &initiators);

// A zero-based grid attachment. Initiator flows match route_classes (one
// when absent), and target flows match Target::flows.
struct MeshEndpoint {
  std::string name;
  unsigned row = 0, column = 0;
  unsigned flows = 1;
  bool operator==(const MeshEndpoint &) const = default;
};
// Convert a grid of 1..128 cells to explicit graphs and routes. Columns are
// X and rows are Y. Both directions independently traverse X before Y.
// Only routed nodes and directed links are retained. Each admitted flow pair
// gets a route. Endpoint and connectivity order do not affect the result.
// clock_domain is copied to every retained node, without a default domain.
// Flow counts must match the endpoint declarations. Validate the complete
// Definition to check routes, clocks, protocols, mappings and resource limits. No file I/O or
// console effects.
Result<ExchangeTopology> mesh_topology(unsigned rows, unsigned columns,
                                       const std::vector<MeshEndpoint> &initiators,
                                       const std::vector<MeshEndpoint> &targets,
                                       const Connectivity &connectivity,
                                       std::optional<std::string> clock_domain = std::nullopt);
} // namespace nocloom
