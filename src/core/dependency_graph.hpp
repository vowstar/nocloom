#pragma once
#include "nocloom/result.hpp"
#include <array>
#include <cstdint>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

namespace nocloom::detail {
struct NetworkPlan;
// The dependency graph of a network plan (docs/outputs.md#dependency-graph). An edge from -> to
// means a stalled or occupied from can stall to: blocker to blocked. The rules and node kinds name
// the waits of the generated hardware; the rule table in dependency_graph.cpp states each one.
enum class DependencyRule : std::uint8_t {
  TargetBlocksSource,   // S1
  ResponseBlocksAccept, // S2
  TargetBlocksReturn,   // S3
  RequestPath,          // S4
  ResponsePath,         // S5
  External,             // S6: a dependency outside the network
  Contention,           // M1: another input holds the output
  Downstream,           // M2: the next stage does not take the word
  Continuation,         // M3: the holder's next word cannot come
  QueueOrder,           // M4: a target's next word waits behind its front word
  PoolWait,             // M6: a target takes a request while it has room
  SourceData,           // M7: a request grant waits for the source's write data
  SourceUnit,           // M8, M9, M10: a source head waits for a unit or an ID entry
  ReturnStage,          // M11, M12, M13: a source takes a response word
  MergeOrder,           // M14: a merge takes one read at a time
  Occupancy,            // M15: a stage waits for the one after it
  Environment,          // M16: a wait on what is outside the network
  StoredRequest,        // M17: occupied replay descriptors keep a full pool from accepting a head
  StoredWord,           // M18: a stored word's beats keep the target's next word and B waiting
  Escape,               // a test's alternative to another edge of one group
};
struct DependencyRuleInfo {
  std::string_view id, name, level;
};
const DependencyRuleInfo &dependency_rule(DependencyRule rule);

struct DependencyNode {
  std::string kind, key, component, domain, binding;
  unsigned rank = 0;
  bool operator==(const DependencyNode &) const = default;
};
struct DependencyEdge {
  std::uint32_t from = 0, to = 0;
  DependencyRule rule = DependencyRule::Environment;
  // Zero stands alone. Edges of one nonzero group are alternatives: any one of them is enough.
  std::uint32_t group = 0;
  bool operator==(const DependencyEdge &) const = default;
};
// A relation left out of the graph: the rule that excludes it, the object, the edges it would
// have added, and the liveness shape (tests/run_liveness_formal.py) that checks the exclusion.
struct DependencyExclusion {
  std::string rule, subject;
  std::uint64_t edges = 0;
  std::string liveness;
  bool operator==(const DependencyExclusion &) const = default;
};
// The transport tables of the Lean model (formal/lean/GenericNoC/Wormhole.lean): routes (slot,
// destination, output), nexts and pools (output, slot), the holds (slot and destination, or output
// and slot, with a node) and the names of the numbers. Absent, with the reason, where the model
// cannot state the network.
struct DependencyModel {
  bool present = false;
  std::string omitted;
  std::vector<std::tuple<std::uint32_t, std::uint32_t, std::uint32_t>> routes, heads, grants;
  std::vector<std::tuple<std::uint32_t, std::uint32_t>> nexts, may_hold, answers, ahead;
  std::vector<std::tuple<std::uint32_t, std::uint32_t, std::uint32_t>> pools;
  std::vector<std::uint32_t> endpoints, contig, cross, bound;
  // Per output, in input order: output, input slot, before, after, other.
  std::vector<std::array<std::uint32_t, 5>> contention;
  std::vector<std::pair<std::uint32_t, std::string>> slots, outputs, destinations;
  unsigned pool_cap = 1;
  bool operator==(const DependencyModel &) const = default;
};
struct DependencyGraph {
  std::vector<DependencyNode> nodes; // sorted by kind, then key
  std::vector<DependencyEdge> edges; // sorted by from, to, rule
  std::vector<DependencyExclusion> excluded;
  DependencyModel model;
  bool operator==(const DependencyGraph &) const = default;
};
// One cycle: its nodes in order, each blocking the next and the last the first, and the rule of
// each of those edges.
struct DependencyFinding {
  std::string level;
  std::vector<std::uint32_t> cycle;
  std::vector<DependencyRule> rules;
  bool operator==(const DependencyFinding &) const = default;
};
struct DependencyReport {
  std::vector<DependencyFinding> findings; // the first of each cycle set, at most a bounded count
  std::uint64_t cycle_sets = 0;
  bool operator==(const DependencyReport &) const = default;
};
// A node by kind and key, for the facts below.
struct DependencyNodeRef {
  std::string kind, key;
};
struct DependencyRawEdge {
  DependencyNodeRef from, to;
};
// Blocked waits for blocker or for alternative, either will do.
struct DependencyEscape {
  DependencyNodeRef blocked, blocker, alternative;
};
// What the graph takes from outside its routes. Product builds use dependency_facts(plan); the
// lists are empty there, and tests set them for negative controls.
struct DependencyFacts {
  std::vector<bool> segment_ends;       // by source: an interleaving target's cut
  std::vector<bool> reassembles;        // by source: clamp_reassembles
  std::vector<bool> interleaved;        // by target: interleaved_target
  std::vector<DependencyRawEdge> edges; // dependencies outside the network
  std::vector<DependencyEscape> escapes;
};
DependencyFacts dependency_facts(const NetworkPlan &plan);
DependencyGraph build_dependency_graph(const NetworkPlan &plan, const DependencyFacts &facts);
// A graph with nothing stuck also takes its ranks: every edge used rises in rank.
DependencyReport check_dependency_graph(DependencyGraph &graph);
// What a cycle means and what the user can change, one line each.
std::vector<std::string> dependency_explanation(const DependencyGraph &graph,
                                                const DependencyFinding &finding);
std::string dependency_graph_json(const DependencyGraph &graph, const DependencyReport &report,
                                  std::string_view module, std::string_view generator,
                                  std::string_view rtl_fingerprint,
                                  std::string_view plan_fingerprint);
std::string fnv1a64(std::string_view text);
// The one rollout switch: a report that fails planning once rejecting is on.
Status dependency_gate(const DependencyGraph &graph, const DependencyReport &report,
                       bool rejecting);
inline constexpr bool dependency_rejects = false;
} // namespace nocloom::detail
