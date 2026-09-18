#include "switch_graph.hpp"
#include "cone_walk.hpp"
#include "diagnostic.hpp"
#include <algorithm>
#include <ranges>
#include <set>
#include <tuple>

namespace nocloom::detail {
namespace {
Diagnostic invalid(std::string code, std::string field, std::string message) {
  return error(std::move(code), std::move(field), std::move(message));
}
// The initiator of an admitted pair's route and its flow count.
std::pair<std::string, unsigned> initiator_of(const DirectedSwitchGraph &g,
                                              const std::string &entry, const std::string &exit) {
  const auto &initiator = g.response ? exit : entry;
  const auto at = g.flows.find(initiator);
  return {initiator, at == g.flows.end() ? 1U : at->second};
}
// The target flow count of an admitted pair's route.
unsigned target_paths_of(const DirectedSwitchGraph &g, const std::string &entry,
                         const std::string &exit) {
  const auto at = g.target_paths.find(g.response ? entry : exit);
  return at == g.target_paths.end() ? 1U : at->second;
}
using FlowKey = std::tuple<std::string, std::string, unsigned, unsigned>;
// The fixed routes by pair and flow pair, each naming a connected pair's flow pair once and running
// from its entry's node to its exit's node over declared edges.
std::optional<Diagnostic>
fixed_routes(const DirectedSwitchGraph &g, std::map<std::string, std::vector<std::string>> &next,
             const std::set<std::pair<std::string, std::string>> &admitted,
             std::map<FlowKey, const SwitchRoute *> &fixed) {
  std::map<std::string, std::string> entries, exits;
  for (const auto &a : g.entries)
    entries.emplace(a.endpoint, a.node);
  for (const auto &a : g.exits)
    exits.emplace(a.endpoint, a.node);
  for (std::size_t k = 0; k < g.fixed_routes.size(); ++k) {
    const auto &f = g.fixed_routes[k];
    const auto field = "switches.routes[" + std::to_string(k) + "]";
    const auto name = "The route " + f.entry + " -> " + f.exit;
    if (!entries.contains(f.entry) || !exits.contains(f.exit) ||
        (g.admitted_pairs && !admitted.contains({f.entry, f.exit})))
      return invalid("NCL_ROUTE_REFERENCE", field, name + " names no connected pair");
    const auto flows = initiator_of(g, f.entry, f.exit).second;
    const unsigned flow = f.flows.empty() ? 0 : f.flows.front();
    if (f.flows.size() > 1 || flow >= flows)
      return invalid("NCL_ROUTE_REFERENCE", field,
                     name + " names flow " + std::to_string(flow) + " of " + std::to_string(flows));
    const auto target_paths = target_paths_of(g, f.entry, f.exit);
    const unsigned target_path = f.target_path.value_or(0);
    if (target_path >= target_paths)
      return invalid("NCL_ROUTE_REFERENCE", field,
                     name + " names target flow " + std::to_string(target_path) + " of " +
                         std::to_string(target_paths));
    if (g.unconnected.contains({initiator_of(g, f.entry, f.exit).first,
                                g.response ? f.entry : f.exit, flow, target_path}))
      return invalid("NCL_ROUTE_REFERENCE", field, name + " names no connected flow pair");
    if (!fixed.emplace(FlowKey{f.entry, f.exit, flow, target_path}, &f).second)
      return invalid("NCL_ROUTE_DUPLICATE", field, name + " is given twice");
    if (const auto n = std::ranges::find_if(
            f.nodes, [&](const std::string &node) { return !next.contains(node); });
        n != f.nodes.end())
      return invalid("NCL_ROUTE_REFERENCE", field, name + " names " + *n + ", no node here");
    if (f.nodes.empty() || f.nodes.front() != entries.at(f.entry) ||
        f.nodes.back() != exits.at(f.exit))
      return invalid("NCL_ROUTE_PATH", field,
                     name + " must start at node " + entries.at(f.entry) + " and end at node " +
                         exits.at(f.exit));
    for (std::size_t i = 1; i < f.nodes.size(); ++i)
      if (std::ranges::find(next[f.nodes[i - 1]], f.nodes[i]) == next[f.nodes[i - 1]].end())
        return invalid("NCL_ROUTE_PATH", field,
                       name + " takes " + f.nodes[i - 1] + " -> " + f.nodes[i] +
                           ", no declared link");
  }
  return std::nullopt;
}
// The route of each flow of each admitted (entry, exit) pair: its fixed route, else the pair's
// breadth-first path, unique when no edge of it can be avoided (another simple path leaves it at
// some edge). Flows that take one path share one route. Every switch and edge carries one.
std::optional<Diagnostic>
plan_routes(SwitchGraphPlan &plan, std::map<std::string, std::vector<std::string>> &next,
            const std::set<std::pair<std::string, std::string>> &admitted) {
  const auto &g = plan.graph;
  std::map<FlowKey, const SwitchRoute *> fixed;
  if (auto fault = fixed_routes(g, next, admitted, fixed))
    return fault;
  std::set<std::string> used;
  std::set<std::pair<std::string, std::string>> used_edges;
  // The switches reached from a node without taking the edge cut.
  auto reach = [&](const std::string &from, const std::pair<std::string, std::string> &cut,
                   std::map<std::string, std::string> *predecessor) {
    std::set<std::string> seen{from};
    for (std::vector<std::string> todo{from}; !todo.empty();) {
      std::vector<std::string> wave;
      for (const auto &n : todo)
        for (const auto &to : next[n])
          if (std::pair{n, to} != cut && seen.insert(to).second) {
            wave.push_back(to);
            if (predecessor)
              (*predecessor)[to] = n;
          }
      todo = std::move(wave);
    }
    return seen;
  };
  for (const auto &entry : g.entries) {
    std::map<std::string, std::string> predecessor;
    const auto reached = reach(entry.node, {}, &predecessor);
    std::map<std::pair<std::string, std::string>, std::set<std::string>> avoiding;
    for (const auto &exit : g.exits) {
      if (g.admitted_pairs && !admitted.contains({entry.endpoint, exit.endpoint}))
        continue;
      const auto flows = initiator_of(g, entry.endpoint, exit.endpoint).second;
      const auto target_paths = target_paths_of(g, entry.endpoint, exit.endpoint);
      // The node path of each connected flow pair, by target flow then initiator flow; the
      // unique path is found once, when a flow pair needs it.
      std::vector<std::optional<std::vector<std::string>>> paths;
      std::optional<std::vector<std::string>> unique;
      const auto initiator = initiator_of(g, entry.endpoint, exit.endpoint).first;
      const auto target = g.response ? entry.endpoint : exit.endpoint;
      for (unsigned pair = 0; pair < flows * target_paths; ++pair) {
        if (g.unconnected.contains({initiator, target, pair % flows, pair / flows})) {
          paths.emplace_back();
          continue;
        }
        if (const auto at = fixed.find({entry.endpoint, exit.endpoint, pair % flows, pair / flows});
            at != fixed.end()) {
          paths.push_back(at->second->nodes);
          continue;
        }
        if (!unique) {
          if (!reached.contains(exit.node))
            return invalid("NCL_SWITCH_ROUTE_MISSING", "switches.routes",
                           "No route from " + entry.endpoint + " to " + exit.endpoint);
          std::vector<std::string> nodes{exit.node};
          while (nodes.back() != entry.node)
            nodes.push_back(predecessor.at(nodes.back()));
          std::ranges::reverse(nodes);
          for (std::size_t i = 1; i < nodes.size(); ++i) {
            const std::pair edge{nodes[i - 1], nodes[i]};
            if (!avoiding.contains(edge))
              avoiding[edge] = reach(entry.node, edge, nullptr);
            if (avoiding[edge].contains(exit.node))
              return invalid("NCL_SWITCH_ROUTE_AMBIGUOUS", "switches.routes",
                             "Multiple routes from " + entry.endpoint + " to " + exit.endpoint +
                                 "; an explicit route selects one");
          }
          unique = std::move(nodes);
        }
        paths.push_back(*unique);
      }
      // One route per target flow and distinct path, in the order of its first initiator flow.
      std::vector<SwitchRoute> routes;
      for (unsigned target_path = 0; target_path < target_paths; ++target_path) {
        const auto first = routes.size();
        for (unsigned flow = 0; flow < flows; ++flow) {
          if (!paths[target_path * flows + flow])
            continue;
          const auto &path = *paths[target_path * flows + flow];
          auto at = std::find_if(routes.begin() + static_cast<std::ptrdiff_t>(first), routes.end(),
                                 [&](const SwitchRoute &r) { return r.nodes == path; });
          if (at == routes.end()) {
            at = routes.insert(routes.end(), {entry.endpoint, exit.endpoint, path, {}});
            if (target_paths > 1)
              at->target_path = target_path;
          }
          if (flows > 1)
            at->flows.push_back(flow);
        }
      }
      for (auto &route : routes) {
        for (std::size_t i = 1; i < route.nodes.size(); ++i)
          used_edges.emplace(route.nodes[i - 1], route.nodes[i]);
        used.insert(route.nodes.begin(), route.nodes.end());
        plan.routes.push_back(std::move(route));
      }
    }
  }
  if (used.size() != g.nodes.size() || used_edges.size() != g.edges.size())
    return invalid("NCL_SWITCH_UNUSED_OBJECT", "switches",
                   "Every declared switch and edge must carry an admitted endpoint route");
  return std::nullopt;
}
// A packet holding one edge waits for the next edge of its route: a cycle of these waits, not a
// cycle of edges alone, can stall the graph.
std::optional<Diagnostic> check_route_cycle(const SwitchGraphPlan &plan) {
  std::map<std::string, std::set<std::string>> waits;
  for (const auto &edge : plan.graph.edges)
    waits[edge.from + ">" + edge.to];
  for (const auto &route : plan.routes)
    for (std::size_t i = 2; i < route.nodes.size(); ++i)
      waits[route.nodes[i - 2] + ">" + route.nodes[i - 1]].insert(route.nodes[i - 1] + ">" +
                                                                  route.nodes[i]);
  std::vector<std::string> order, cycle;
  if (topo_visit(
          std::views::keys(waits),
          [&](const std::string &e) -> const auto & { return waits.at(e); }, order, cycle))
    return std::nullopt;
  std::string loop = cycle.front().substr(0, cycle.front().find('>'));
  auto diagnostic = invalid("NCL_SWITCH_ROUTE_CYCLE", "switches.edges", "");
  for (std::size_t i = 1; i < cycle.size(); ++i) {
    loop += " -> " + cycle[i].substr(0, cycle[i].find('>'));
    diagnostic.notes.push_back(cycle[i - 1] + " waits for " + cycle[i]);
  }
  diagnostic.message =
      "Routes chain the links of the cycle " + loop + "; no deadlock claim is made";
  return diagnostic;
}
// The physical switch outputs: each with its candidate inputs and reachable destinations.
void plan_outputs(SwitchGraphPlan &plan) {
  std::map<std::tuple<std::string, bool, std::string>,
           std::pair<std::set<std::string>, std::set<std::string>>>
      outputs;
  for (const auto &route : plan.routes)
    for (std::size_t i = 0; i < route.nodes.size(); ++i) {
      const bool endpoint = i + 1 == route.nodes.size();
      const auto output = endpoint ? route.exit : route.nodes[i + 1];
      auto &[inputs, destinations] = outputs[{route.nodes[i], endpoint, output}];
      inputs.insert(i == 0 ? "endpoint/" + route.entry : "node/" + route.nodes[i - 1]);
      destinations.insert(route.exit);
    }
  for (const auto &[key, paths] : outputs) {
    const auto &[node, endpoint, output] = key;
    plan.outputs.push_back({node,
                            output,
                            endpoint,
                            {paths.first.begin(), paths.first.end()},
                            {paths.second.begin(), paths.second.end()}});
  }
}
} // namespace

Result<SwitchGraphPlan> plan_switch_graph(const DirectedSwitchGraph &input) {
  auto fail = [](std::string code, std::string field, std::string message) {
    return Result<SwitchGraphPlan>::failure(
        invalid(std::move(code), std::move(field), std::move(message)));
  };
  if (input.nodes.empty() || input.entries.empty() || input.exits.empty())
    return fail("NCL_SWITCH_GRAPH", "switches", "Nodes and endpoint attachments are required");
  SwitchGraphPlan plan;
  plan.graph = input;
  auto &g = plan.graph;
  std::ranges::sort(g.nodes);
  if (std::ranges::any_of(g.nodes, [](const auto &n) { return !valid_name(n); }) ||
      std::adjacent_find(g.nodes.begin(), g.nodes.end()) != g.nodes.end())
    return fail("NCL_SWITCH_IDENTITY", "switches", "Switch names must be valid and unique");
  std::map<std::string, std::vector<std::string>> next;
  for (const auto &n : g.nodes) {
    next[n];
    plan.incoming_candidates[n] = 0;
  }
  auto by_edge = [](const auto &a, const auto &b) {
    return std::tie(a.from, a.to) < std::tie(b.from, b.to);
  };
  std::ranges::sort(g.edges, by_edge);
  for (std::size_t i = 0; i < g.edges.size(); ++i) {
    const auto &edge = g.edges[i];
    if (!next.contains(edge.from) || !next.contains(edge.to))
      return fail("NCL_SWITCH_REFERENCE", "switches.edges", "Edge references an absent switch");
    if (i && edge == g.edges[i - 1])
      return fail("NCL_SWITCH_EDGE_DUPLICATE", "switches.edges", "Duplicate physical edge");
    if (edge.from == edge.to)
      return fail("NCL_SWITCH_ROUTE_CYCLE", "switches.edges",
                  "The link of the cycle " + edge.from + " -> " + edge.to +
                      " returns to its node: a route over it passes the node twice");
    next[edge.from].push_back(edge.to);
    ++plan.incoming_candidates[edge.to];
  }
  for (auto *attachments : {&g.entries, &g.exits}) {
    std::ranges::sort(*attachments, {}, &SwitchAttachment::endpoint);
    std::set<std::string> endpoints;
    for (const auto &a : *attachments) {
      if (!valid_name(a.endpoint) || !endpoints.insert(a.endpoint).second)
        return fail("NCL_SWITCH_IDENTITY", "switches.attachments",
                    "Endpoint attachments must be valid and unique");
      if (!next.contains(a.node))
        return fail("NCL_SWITCH_REFERENCE", "switches.attachments",
                    "Attachment references an absent switch");
    }
  }
  std::set<std::pair<std::string, std::string>> admitted;
  if (g.admitted_pairs) {
    std::set<std::string> entries, exits, used_entries, used_exits;
    for (const auto &a : g.entries)
      entries.insert(a.endpoint);
    for (const auto &a : g.exits)
      exits.insert(a.endpoint);
    std::ranges::sort(*g.admitted_pairs, [](const auto &a, const auto &b) {
      return std::tie(a.entry, a.exit) < std::tie(b.entry, b.exit);
    });
    for (const auto &pair : *g.admitted_pairs) {
      if (!entries.contains(pair.entry) || !exits.contains(pair.exit) ||
          !admitted.emplace(pair.entry, pair.exit).second)
        return fail("NCL_SWITCH_PAIR", "connectivity",
                    "Every connected pair must name endpoints attached to the graph, once");
      used_entries.insert(pair.entry);
      used_exits.insert(pair.exit);
    }
    if (used_entries != entries || used_exits != exits)
      return fail("NCL_SWITCH_PAIR", "connectivity",
                  "Every attached endpoint must take part in at least one connected pair");
  }
  for (const auto &a : g.entries)
    ++plan.incoming_candidates[a.node];

  // Switches in topological order; a cyclic graph keeps the sorted switches.
  std::vector<std::string> cycle;
  if (topo_visit(
          g.nodes, [&](const std::string &n) -> const auto & { return next[n]; },
          plan.topological_order, cycle))
    std::ranges::reverse(plan.topological_order);
  else
    plan.topological_order = g.nodes;

  if (auto missing = plan_routes(plan, next, admitted))
    return Result<SwitchGraphPlan>::failure(std::move(*missing));
  if (plan.graph.response) {
    std::map<std::string, std::set<std::vector<std::string>>> paths;
    for (const auto &route : plan.routes)
      paths[route.exit].insert(route.nodes);
    for (auto &route : plan.routes) {
      const auto &exit_paths = paths.at(route.exit);
      route.return_path =
          static_cast<unsigned>(std::distance(exit_paths.begin(), exit_paths.find(route.nodes)));
    }
  }
  if (auto loop = check_route_cycle(plan))
    return Result<SwitchGraphPlan>::failure(std::move(*loop));
  plan_outputs(plan);
  return Result<SwitchGraphPlan>::success(std::move(plan));
}

unsigned return_path(const SwitchGraphPlan &, const SwitchRoute &route) {
  return route.return_path;
}
namespace {
// Whether two routes that enter one switch over one input toward one exit (and, with by_entry,
// from one entry toward one target flow) leave it over different outputs.
bool splits(const SwitchGraphPlan &plan, bool by_entry) {
  std::map<std::tuple<std::string, std::string, std::string, std::string, unsigned>, std::string>
      leaves;
  for (const auto &route : plan.routes)
    for (std::size_t i = 0; i < route.nodes.size(); ++i) {
      const auto input = i ? "node/" + route.nodes[i - 1] : "endpoint/" + route.entry;
      const auto output =
          i + 1 < route.nodes.size() ? "node/" + route.nodes[i + 1] : "endpoint/" + route.exit;
      const auto [at, fresh] =
          leaves.try_emplace({route.nodes[i], input, route.exit, by_entry ? route.entry : "",
                              by_entry ? route.target_path.value_or(0) : 0U},
                             output);
      if (!fresh && at->second != output)
        return true;
    }
  return false;
}
} // namespace
bool flow_split(const SwitchGraphPlan &request) { return splits(request, true); }
bool exit_split(const SwitchGraphPlan &plan) { return splits(plan, false); }

Result<ExchangePlan> plan_exchange(const DirectedSwitchGraph &request,
                                   const DirectedSwitchGraph &response) {
  auto q = plan_switch_graph(request);
  if (!q)
    return Result<ExchangePlan>::failure(q.diagnostics());
  auto r = plan_switch_graph(response);
  if (!r)
    return Result<ExchangePlan>::failure(r.diagnostics());
  auto names = [](const auto &attachments) {
    std::set<std::string> result;
    for (const auto &a : attachments)
      result.insert(a.endpoint);
    return result;
  };
  if (names(request.entries) != names(response.exits) ||
      names(request.exits) != names(response.entries))
    return Result<ExchangePlan>::failure(invalid(
        "NCL_SWITCH_RETURN_ENDPOINT", "switches.response",
        "Response attachments must return from every target to the original source identities"));
  std::set<std::pair<std::string, std::string>> forward, backward;
  for (const auto &route : q.value().routes)
    forward.emplace(route.entry, route.exit);
  for (const auto &route : r.value().routes)
    backward.emplace(route.exit, route.entry);
  if (forward != backward)
    return Result<ExchangePlan>::failure(
        invalid("NCL_SWITCH_RETURN_PAIR", "switches.response",
                "Response routes must return exactly the admitted request pairs"));
  return Result<ExchangePlan>::success({std::move(q.value()), std::move(r.value())});
}
} // namespace nocloom::detail
