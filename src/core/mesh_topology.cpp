#include "diagnostic.hpp"
#include "nocloom/topology.hpp"
#include <map>
#include <set>
#include <tuple>

namespace nocloom {
namespace {
using Endpoints = std::map<std::string, MeshEndpoint>;
using FlowPair = std::tuple<std::string, std::string, unsigned, unsigned>;

Status mesh_endpoints(const std::vector<MeshEndpoint> &endpoints, unsigned rows, unsigned columns,
                      const std::string &field, Endpoints &named) {
  if (endpoints.empty() || endpoints.size() > 256)
    return Status::failure(
        detail::error("NCL_MESH_ENDPOINT", field, "Expected 1 to 256 endpoint attachments"));
  for (std::size_t i = 0; i < endpoints.size(); ++i) {
    const auto &endpoint = endpoints[i];
    const auto at = field + "[" + std::to_string(i) + "]";
    if (!detail::valid_name(endpoint.name))
      return Status::failure(
          detail::error("NCL_NAME_INVALID", at + ".name", "Expected a valid endpoint name"));
    if (!named.emplace(endpoint.name, endpoint).second)
      return Status::failure(detail::error("NCL_MESH_ENDPOINT", at + ".name",
                                           "Each endpoint attaches exactly once per role"));
    if (endpoint.row >= rows || endpoint.column >= columns)
      return Status::failure(
          detail::error("NCL_MESH_ENDPOINT", at, "Endpoint coordinates must lie inside the grid"));
    if (endpoint.flows == 0 || endpoint.flows > 32)
      return Status::failure(
          detail::error("NCL_MESH_ENDPOINT", at + ".flows", "Expected 1 to 32 endpoint flows"));
  }
  return Status::success({});
}

Result<std::set<FlowPair>> mesh_pairs(const Endpoints &sources, const Endpoints &targets,
                                      const Connectivity &connectivity) {
  const auto fail = [](const std::string &field, const std::string &message) {
    return Result<std::set<FlowPair>>::failure(
        detail::error("NCL_MESH_CONNECTIVITY", field, message));
  };
  if (connectivity.all && !connectivity.pairs.empty())
    return fail("mesh.connectivity", "All connectivity takes no pair list");
  std::set<FlowPair> pairs;
  const auto expand = [&](const MeshEndpoint &source, const MeshEndpoint &target,
                          std::optional<unsigned> f, std::optional<unsigned> g) {
    bool unique = true;
    for (unsigned i = 0; i < source.flows; ++i)
      for (unsigned j = 0; j < target.flows; ++j)
        if (f.value_or(i) == i && g.value_or(j) == j)
          unique = pairs.emplace(source.name, target.name, i, j).second && unique;
    return unique;
  };
  if (connectivity.all) {
    for (const auto &[name, source] : sources)
      for (const auto &[target_name, target] : targets)
        expand(source, target, {}, {});
  } else {
    for (std::size_t i = 0; i < connectivity.pairs.size(); ++i) {
      const auto &pair = connectivity.pairs[i];
      const auto field = "mesh.connectivity.pairs[" + std::to_string(i) + "]";
      const auto source = sources.find(pair.source), target = targets.find(pair.target);
      if (source == sources.end() || target == targets.end())
        return fail(field, "Connectivity must name attached endpoints");
      if ((pair.source_path && *pair.source_path >= source->second.flows) ||
          (pair.target_path && *pair.target_path >= target->second.flows))
        return fail(field, "Connectivity flow indices must fit their endpoints");
      if (!expand(source->second, target->second, pair.source_path, pair.target_path))
        return fail(field, "Each admitted flow pair must occur exactly once");
    }
  }
  std::set<std::string> used_sources, used_targets;
  for (const auto &[source, target, f, g] : pairs) {
    used_sources.insert(source);
    used_targets.insert(target);
  }
  if (used_sources.size() != sources.size() || used_targets.size() != targets.size())
    return fail("mesh.connectivity", "Every attached endpoint must occur in an admitted pair");
  return Result<std::set<FlowPair>>::success(std::move(pairs));
}

std::string mesh_node(unsigned row, unsigned column) {
  return "mesh_r" + std::to_string(row) + "_c" + std::to_string(column);
}

std::vector<std::string> mesh_path(const MeshEndpoint &source, const MeshEndpoint &target) {
  auto row = source.row, column = source.column;
  std::vector<std::string> path{mesh_node(row, column)};
  while (column != target.column) {
    column = column < target.column ? column + 1 : column - 1;
    path.push_back(mesh_node(row, column));
  }
  while (row != target.row) {
    row = row < target.row ? row + 1 : row - 1;
    path.push_back(mesh_node(row, column));
  }
  return path;
}

ExchangeGraph mesh_graph(const std::vector<ExchangeRoute> &routes, const Endpoints &entries,
                         const Endpoints &exits, bool response,
                         const std::optional<std::string> &domain) {
  std::set<std::string> nodes;
  std::set<std::pair<std::string, std::string>> links;
  for (const auto &route : routes) {
    const auto &path = response ? route.response : route.request;
    nodes.insert(path.begin(), path.end());
    for (std::size_t i = 1; i < path.size(); ++i)
      links.emplace(path[i - 1], path[i]);
  }
  ExchangeGraph graph;
  for (const auto &node : nodes)
    graph.nodes.push_back({node, std::nullopt, domain});
  for (const auto &[from, to] : links)
    graph.links.push_back({from, to});
  for (const auto &[name, endpoint] : entries)
    graph.entries.push_back({name, mesh_node(endpoint.row, endpoint.column)});
  for (const auto &[name, endpoint] : exits)
    graph.exits.push_back({name, mesh_node(endpoint.row, endpoint.column)});
  return graph;
}
} // namespace

Result<ExchangeTopology> mesh_topology(unsigned rows, unsigned columns,
                                       const std::vector<MeshEndpoint> &initiators,
                                       const std::vector<MeshEndpoint> &targets,
                                       const Connectivity &connectivity,
                                       std::optional<std::string> clock_domain) {
  if (rows == 0 || columns == 0 || rows > 128 || columns > 128 / rows)
    return Result<ExchangeTopology>::failure(
        detail::error("NCL_MESH_DIMENSIONS", "mesh",
                      "Expected a grid of 1 to 128 cells with nonzero dimensions"));
  Endpoints sources, sinks;
  if (auto checked = mesh_endpoints(initiators, rows, columns, "mesh.initiators", sources);
      !checked)
    return Result<ExchangeTopology>::failure(checked.diagnostics());
  if (auto checked = mesh_endpoints(targets, rows, columns, "mesh.targets", sinks); !checked)
    return Result<ExchangeTopology>::failure(checked.diagnostics());
  const auto pairs = mesh_pairs(sources, sinks, connectivity);
  if (!pairs)
    return Result<ExchangeTopology>::failure(pairs.diagnostics());
  ExchangeTopology topology;
  for (const auto &[source, target, f, g] : pairs.value()) {
    const auto &start = sources.at(source), &end = sinks.at(target);
    topology.routes.push_back({source, target, mesh_path(start, end), mesh_path(end, start), f, g});
  }
  topology.request = mesh_graph(topology.routes, sources, sinks, false, clock_domain);
  topology.response = mesh_graph(topology.routes, sinks, sources, true, clock_domain);
  return Result<ExchangeTopology>::success(std::move(topology));
}
} // namespace nocloom
