#include "cross_domain.hpp"
#include "cone_walk.hpp"
#include "diagnostic.hpp"
#include <map>
#include <ranges>
#include <set>

namespace nocloom::detail {
namespace {
Diagnostic graph_error(std::string code, std::string text) {
  return error(std::move(code), "network.resources", std::move(text));
}
} // namespace

Result<std::vector<std::string>> order_cross_domain_graph(const CrossGraph &g) {
  std::map<std::string, const CrossResource *> ids;
  std::map<std::string, std::set<std::string>> edges;
  for (const auto &r : g.resources) {
    if (r.id.empty() || r.storage.empty() || r.domain.empty() || r.role.empty() ||
        r.owner.empty() || r.release.empty() || !ids.emplace(r.id, &r).second)
      return Result<std::vector<std::string>>::failure(graph_error(
          "NCL_CDC_BINDING", "A resource requires a unique identity and release event"));
    edges[r.storage];
  }
  for (const auto &w : g.waits) {
    if (!ids.contains(w.from) || !ids.contains(w.to) || w.condition.empty() ||
        static_cast<unsigned>(w.kind) > static_cast<unsigned>(CrossWaitKind::Environment))
      return Result<std::vector<std::string>>::failure(
          graph_error("NCL_CDC_BINDING", "A dependency must reference existing objects"));
    if (w.kind != CrossWaitKind::ReservedWrite)
      edges[ids.at(w.from)->storage].insert(ids.at(w.to)->storage);
  }
  std::vector<std::string> ordered, cycle;
  if (!topo_visit(
          std::views::keys(edges),
          [&](const std::string &name) -> const auto & { return edges.at(name); }, ordered,
          cycle)) {
    auto diagnostic =
        graph_error("NCL_RESOURCE_CYCLE",
                    "Conservative joint dependencies contain a cycle; reachability is not proven");
    for (std::size_t i = 1; i < cycle.size(); ++i)
      for (const auto &w : g.waits)
        if (w.kind != CrossWaitKind::ReservedWrite && ids.at(w.from)->storage == cycle[i - 1] &&
            ids.at(w.to)->storage == cycle[i])
          diagnostic.notes.push_back(w.from + " waits for " + w.to + ": " + w.condition);
    return Result<std::vector<std::string>>::failure(std::move(diagnostic));
  }
  return Result<std::vector<std::string>>::success(std::move(ordered));
}

} // namespace nocloom::detail
