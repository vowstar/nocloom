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
} // namespace nocloom
