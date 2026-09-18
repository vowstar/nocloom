#include "diagnostic.hpp"
#include "nocloom/topology.hpp"
#include "switch_graph.hpp"
#include <algorithm>
#include <map>
#include <set>
#include <tuple>

namespace nocloom {
Result<ExchangeTopology> tree_topology(const std::vector<TreeNode> &nodes,
                                       const std::vector<std::string> &initiators) {
  const auto fail = [](std::string code, std::string field, std::string message) {
    return Result<ExchangeTopology>::failure(
        detail::error(std::move(code), std::move(field), std::move(message)));
  };
  if (nodes.empty() || nodes.size() > 128)
    return fail("NCL_TREE_NODE_COUNT", "tree.nodes",
                "Expected 1 to 128 tree nodes, matching the explicit graph limit");
  if (initiators.empty())
    return fail("NCL_TREE_INITIATOR_COUNT", "tree.initiators",
                "At least one explicit initiator name is required");
  const auto node_field = [](std::size_t i) { return "tree.nodes[" + std::to_string(i) + "]"; };
  std::map<std::string, std::size_t> indices;
  std::set<std::string> sources, targets;
  std::vector<std::size_t> roots;
  for (std::size_t i = 0; i < nodes.size(); ++i) {
    const auto &node = nodes[i];
    if (!detail::valid_name(node.name))
      return fail("NCL_NAME_INVALID", node_field(i) + ".name", "Expected a valid node name");
    if (!indices.emplace(node.name, i).second)
      return fail("NCL_TREE_NODE_DUPLICATE", node_field(i) + ".name", "Duplicate tree node");
    if (!node.parent)
      roots.push_back(i);
    for (std::size_t j = 0; j < node.targets.size(); ++j) {
      const auto field = node_field(i) + ".targets[" + std::to_string(j) + "]";
      if (!detail::valid_name(node.targets[j]))
        return fail("NCL_NAME_INVALID", field, "Expected a valid target name");
      if (!targets.insert(node.targets[j]).second)
        return fail("NCL_TREE_TARGET_DUPLICATE", field, "Each target attaches exactly once");
    }
  }
  for (std::size_t i = 0; i < initiators.size(); ++i) {
    const auto field = "tree.initiators[" + std::to_string(i) + "]";
    if (!detail::valid_name(initiators[i]))
      return fail("NCL_NAME_INVALID", field, "Expected a valid initiator name");
    if (!sources.insert(initiators[i]).second)
      return fail("NCL_TREE_INITIATOR_DUPLICATE", field, "Each initiator attaches exactly once");
  }
  if (roots.size() != 1)
    return fail("NCL_TREE_ROOT_COUNT", "tree.nodes", "The tree requires exactly one root");
  if (targets.empty())
    return fail("NCL_TREE_TARGET_COUNT", "tree.nodes",
                "At least one target attachment is required");
  for (std::size_t i = 0; i < nodes.size(); ++i) {
    const auto &parent = nodes[i].parent;
    if (parent && !indices.contains(*parent))
      return fail("NCL_TREE_PARENT_MISSING", node_field(i) + ".parent",
                  "Parent references an absent tree node");
    if (parent && *parent == nodes[i].name)
      return fail("NCL_TREE_PARENT_SELF", node_field(i) + ".parent",
                  "A tree node cannot be its own parent");
  }
  // A single root does not rule out a disconnected cycle. Follow each parent
  // chain once, preserving original indices for diagnostics.
  std::vector<unsigned> color(nodes.size(), 0);
  for (std::size_t i = 0; i < nodes.size(); ++i) {
    std::vector<std::size_t> path;
    auto current = i;
    while (!color[current]) {
      color[current] = 1;
      path.push_back(current);
      if (!nodes[current].parent)
        break;
      current = indices.at(*nodes[current].parent);
      if (color[current] == 1)
        return fail("NCL_TREE_CYCLE", node_field(current) + ".parent",
                    "Parent links contain a cycle disconnected from the root");
    }
    for (const auto index : path)
      color[index] = 2;
  }
  ExchangeTopology topology;
  for (const auto &[name, index] : indices) {
    const auto &node = nodes[index];
    topology.request.nodes.push_back({name, std::nullopt, node.clock_domain});
    topology.response.nodes.push_back({name, std::nullopt, node.clock_domain});
    if (node.parent) {
      topology.request.links.push_back({*node.parent, name});
      topology.response.links.push_back({name, *node.parent});
    }
    for (const auto &target : node.targets) {
      topology.request.exits.push_back({target, name});
      topology.response.entries.push_back({target, name});
    }
  }
  for (const auto &source : sources) {
    topology.request.entries.push_back({source, nodes[roots.front()].name});
    topology.response.exits.push_back({source, nodes[roots.front()].name});
  }
  for (auto *graph : {&topology.request, &topology.response}) {
    std::ranges::sort(graph->links, [](const auto &a, const auto &b) {
      return std::tie(a.from, a.to) < std::tie(b.from, b.to);
    });
    for (auto *attachments : {&graph->entries, &graph->exits})
      std::ranges::sort(*attachments, {}, &ExchangeAttachment::endpoint);
  }
  const auto structure = [](const ExchangeGraph &graph) {
    detail::DirectedSwitchGraph result;
    for (const auto &node : graph.nodes)
      result.nodes.push_back(node.name);
    for (const auto &link : graph.links)
      result.edges.push_back({link.from, link.to});
    for (const auto &entry : graph.entries)
      result.entries.push_back({entry.endpoint, entry.node});
    for (const auto &exit : graph.exits)
      result.exits.push_back({exit.endpoint, exit.node});
    return result;
  };
  if (auto checked =
          detail::plan_exchange(structure(topology.request), structure(topology.response));
      !checked)
    return Result<ExchangeTopology>::failure(checked.diagnostics());
  return Result<ExchangeTopology>::success(std::move(topology));
}
} // namespace nocloom
