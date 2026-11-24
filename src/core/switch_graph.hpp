#pragma once

#include "nocloom/definition.hpp"
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>

namespace nocloom::detail {
// The packets a transport graph carries: both directions over shared paths, or, with separate read
// and write paths (ReadWritePaths::Separate), the writes or the reads.
enum class Carried : std::uint8_t { Both, Writes, Reads };
// The scope of the switches and links of a graph: req/ and rsp/ for shared paths, wreq/ and wrsp/
// for the write paths, rreq/ and rrsp/ for the read paths.
std::string graph_scope(bool response, Carried carried);
// Whether a scoped object, link or segment name lies in a response graph.
bool response_scope(std::string_view name);
// Whether a name starts with the scope of some graph.
bool graph_scoped(std::string_view name);
inline std::string path_lane(const std::string &name, bool read) {
  return read ? name + "/read" : name;
}
enum class PathMode { Shared, Separate, Mixed };
PathMode path_mode(const Definition &d);
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
  Carried carried = Carried::Both;
  std::set<std::string> separate_nodes = {};
  std::set<std::pair<std::string, std::string>> separate_links = {};
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
  std::string scope() const { return graph_scope(graph.response, graph.carried); }
  bool split_at(const std::string &endpoint, bool entry) const;
  PathMode route_mode(const SwitchRoute &route) const;
  // Topological when the graph is acyclic; a cyclic graph keeps the sorted switches.
  std::vector<std::string> topological_order;
  std::vector<SwitchRoute> routes;
  std::vector<SwitchOutputPlan> outputs;
  // Counts physical incoming edges and local attachments, not logical AXI flows.
  std::map<std::string, unsigned> incoming_candidates;
  bool operator==(const SwitchGraphPlan &) const = default;
};
struct ExchangePlan {
  // Shared paths: both directions. Separate paths: the writes.
  SwitchGraphPlan request, response;
  // Separate paths: the reads, on graphs of the same topology; absent with shared paths.
  std::optional<SwitchGraphPlan> read_request = std::nullopt, read_response = std::nullopt;
  bool separate() const { return read_request.has_value(); }
  bool mixed = false;
  bool split_endpoint(const std::string &name, bool source) const {
    return separate() ||
           (mixed && (request.split_at(name, source) || response.split_at(name, !source)));
  }
  // Every graph: the request graphs (writes before reads), then the response graphs.
  std::vector<const SwitchGraphPlan *> graphs() const {
    std::vector<const SwitchGraphPlan *> all{&request};
    if (read_request)
      all.push_back(&*read_request);
    all.push_back(&response);
    if (read_response)
      all.push_back(&*read_response);
    return all;
  }
  std::vector<SwitchGraphPlan *> graphs() {
    std::vector<SwitchGraphPlan *> all{&request};
    if (read_request)
      all.push_back(&*read_request);
    all.push_back(&response);
    if (read_response)
      all.push_back(&*read_response);
    return all;
  }
  // The graph of a scope (graph_scope).
  const SwitchGraphPlan &graph(std::string_view scope) const {
    for (const auto *g : {&request, &response})
      if (g->scope() == scope)
        return *g;
    for (const auto *g : {&read_request, &read_response})
      if (*g && (*g)->scope() == scope)
        return **g;
    throw std::out_of_range("No graph " + std::string(scope));
  }
  bool operator==(const ExchangePlan &) const = default;
};

// Each flow pair (initiator flow, target flow) of an admitted entry/exit pair takes its fixed route
// (a path over declared edges between its attachment nodes) or else the pair's one simple directed
// route, and no cycle of route waits (one edge held while the next is awaited) may form: a route
// taking an edge twice forms one. A failure for such cycles is their report.
Result<SwitchGraphPlan> plan_switch_graph(const DirectedSwitchGraph &graph,
                                          const ValidationOptions &limits = {});
// The report of the cycles of route waits in a planned direction that has one
// (docs/network.md#route-cycle-report): one diagnostic for each cycle, the shortest first and
// cycles of one length in the order of their names, up to limits.cycle_limit (at least 1). A note
// for each wait lists its contributors, up to limits.contributor_limit when that is not 0. The
// last diagnostic has one more note when the direction has more cycles.
std::vector<Diagnostic> route_cycle_report(const SwitchGraphPlan &plan,
                                           const ValidationOptions &limits);
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
