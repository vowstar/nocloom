#pragma once

#include "nocloom/definition.hpp"
#include <map>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <vector>

namespace nocloom::detail {
// Private directed exchange planning objects.
struct SwitchEdge {
  std::string from, to;
  bool operator==(const SwitchEdge &) const = default;
};
struct SwitchAttachment {
  std::string endpoint, node;
  bool operator==(const SwitchAttachment &) const = default;
};
struct SwitchPair {
  std::string entry, exit;
  bool operator==(const SwitchPair &) const = default;
};
// The route of an admitted pair: its switches in order, a switch passed again entered by another
// link. flows lists the initiator flows that take it, ascending, for an initiator of several
// flows; it is empty for an initiator of one. target_path is the target flow it carries
// for a target of several flows, absent for a target of one.
struct SwitchRoute {
  std::string entry, exit;
  std::vector<std::string> nodes;
  std::vector<unsigned> flows = {};
  std::optional<unsigned> target_path = std::nullopt;
  // The response path number among the lexicographically ordered paths into this route's exit.
  unsigned return_path = 0;
  bool operator==(const SwitchRoute &) const = default;
};
struct DirectedSwitchGraph {
  std::vector<std::string> nodes;
  std::vector<SwitchEdge> edges;
  std::vector<SwitchAttachment> entries, exits;
  std::optional<std::vector<SwitchPair>> admitted_pairs = std::nullopt;
  // Explicit routes of admitted pairs, each of one flow (flows holds it for an initiator of
  // several); the other flows of a pair take its unique route.
  std::vector<SwitchRoute> fixed_routes = {};
  // The response graph: its exits are the initiators.
  bool response = false;
  // The flow count of every initiator, and of every target, with more than one.
  std::map<std::string, unsigned> flows = {}, target_paths = {};
  // The flow pairs (initiator, target, initiator flow, target flow) of admitted pairs that the
  // connectivity matrix leaves out: they take no route.
  std::set<std::tuple<std::string, std::string, unsigned, unsigned>> unconnected = {};
  bool operator==(const DirectedSwitchGraph &) const = default;
};
struct SwitchOutputPlan {
  std::string node, output;
  bool endpoint = false;
  // Only inputs with an admitted route to this physical output are candidates.
  std::vector<std::string> inputs, destinations;
  bool operator==(const SwitchOutputPlan &) const = default;
};
struct SwitchGraphPlan {
  DirectedSwitchGraph graph;
  // Topological when the graph is acyclic; a cyclic graph keeps the sorted switches.
  std::vector<std::string> topological_order;
  std::vector<SwitchRoute> routes;
  std::vector<SwitchOutputPlan> outputs;
  // Counts physical incoming edges and local attachments, not logical AXI flows.
  std::map<std::string, unsigned> incoming_candidates;
  bool operator==(const SwitchGraphPlan &) const = default;
};
struct ExchangePlan {
  SwitchGraphPlan request, response;
  bool operator==(const ExchangePlan &) const = default;
};

// Each flow pair (initiator flow, target flow) of an admitted entry/exit pair takes its fixed route
// (a path over declared edges between its attachment nodes) or else the pair's one simple directed
// route, and no cycle of route waits (one edge held while the next is awaited) may form: a route
// taking an edge twice forms one.
Result<SwitchGraphPlan> plan_switch_graph(const DirectedSwitchGraph &graph);
// The return path of a response route: the index of its switches among the distinct switch
// sequences of the routes into its initiator, in sequence order.
unsigned return_path(const SwitchGraphPlan &response, const SwitchRoute &route);
// Whether some switch input leads the routes of one exit and target flow, from one entry alike,
// over different outputs: the request switches then compare the initiator flow (message path
// field).
bool flow_split(const SwitchGraphPlan &request);
// Whether some switch input leads the routes of one exit over different outputs.
bool exit_split(const SwitchGraphPlan &plan);
// The response graph is supplied independently, never inferred by reversing a tree.
Result<ExchangePlan> plan_exchange(const DirectedSwitchGraph &request,
                                   const DirectedSwitchGraph &response);
} // namespace nocloom::detail
