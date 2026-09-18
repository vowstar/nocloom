#include "core/switch_graph.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <iterator>
using namespace nocloom::detail;

namespace {
DirectedSwitchGraph requests() {
  return {{"a", "b", "merge"},
          {{"a", "merge"}, {"b", "merge"}},
          {{"s0", "a"}, {"s1", "b"}},
          {{"t0", "merge"}, {"t1", "merge"}}};
}
DirectedSwitchGraph responses() {
  return {{"x", "y", "return_merge"},
          {{"x", "return_merge"}, {"y", "return_merge"}},
          {{"t0", "x"}, {"t1", "y"}},
          {{"s0", "return_merge"}, {"s1", "return_merge"}}};
}
} // namespace

TEST_CASE("Explicit exchange routing rejects the directed single-fault counterexamples",
          "[switch-graph]") {
  const auto q = requests(), r = responses();
  const auto baseline = plan_exchange(q, r);
  REQUIRE(baseline);
  REQUIRE(baseline.value().request.routes.size() == 4);
  REQUIRE(baseline.value().response.routes.size() == 4);
  REQUIRE(baseline.value().request.routes[2].nodes == std::vector<std::string>{"b", "merge"});
  REQUIRE(baseline.value().response.routes[2].nodes ==
          std::vector<std::string>{"y", "return_merge"});
  REQUIRE(baseline.value().request.incoming_candidates.at("merge") == 2);
  std::vector<SwitchOutputPlan> merged;
  std::ranges::copy_if(baseline.value().request.outputs, std::back_inserter(merged),
                       [](const auto &output) { return output.node == "merge"; });
  REQUIRE(merged.size() == 2);
  REQUIRE(merged[0].inputs == std::vector<std::string>{"node/a", "node/b"});
  REQUIRE(merged[1].inputs == merged[0].inputs);
  REQUIRE(merged[0].destinations == std::vector<std::string>{"t0"});
  REQUIRE(merged[1].destinations == std::vector<std::string>{"t1"});
  auto reject = [&](const DirectedSwitchGraph &fault, const std::string &code) {
    auto checked = plan_switch_graph(fault);
    REQUIRE_FALSE(checked);
    REQUIRE(checked.diagnostics().front().code == code);
    REQUIRE(plan_exchange(q, r));
  };
  SECTION("a physical edge cannot silently run backwards") {
    auto fault = q;
    std::swap(fault.edges[1].from, fault.edges[1].to);
    reject(fault, "NCL_SWITCH_ROUTE_MISSING");
  }
  SECTION("a valid-looking directed diamond does not provide a unique output") {
    auto fault = q;
    fault.edges.push_back({"a", "b"});
    reject(fault, "NCL_SWITCH_ROUTE_AMBIGUOUS");
  }
  SECTION("a cycle that no route chains is admitted; a chained one has concrete edge waits") {
    auto open = q;
    open.edges.push_back({"merge", "a"});
    open.exits.push_back({"t2", "a"});
    const auto admitted = plan_switch_graph(open);
    REQUIRE(admitted);
    REQUIRE(admitted.value().topological_order == std::vector<std::string>{"a", "b", "merge"});
    auto fault = open;
    fault.exits.push_back({"t3", "b"});
    fault.entries.push_back({"s2", "merge"});
    fault.edges.push_back({"a", "b"});
    fault.edges.erase(fault.edges.begin());
    reject(fault, "NCL_SWITCH_ROUTE_CYCLE");
    REQUIRE(plan_switch_graph(fault).diagnostics().front().notes.size() == 3);
    auto twice = q;
    twice.edges.push_back({"a", "a"});
    reject(twice, "NCL_SWITCH_ROUTE_CYCLE");
  }
  SECTION("a disconnected ghost switch is not a reached resource") {
    auto fault = q;
    fault.nodes.push_back("ghost");
    reject(fault, "NCL_SWITCH_UNUSED_OBJECT");
  }
  SECTION("an edge must reference a real switch") {
    auto fault = q;
    fault.edges[0].from = "missing";
    reject(fault, "NCL_SWITCH_REFERENCE");
  }
  SECTION("duplicating an edge cannot inflate physical input count") {
    auto fault = q;
    fault.edges.push_back(fault.edges.front());
    reject(fault, "NCL_SWITCH_EDGE_DUPLICATE");
  }
  SECTION("response endpoint identities cannot be inferred from a reversed tree") {
    auto fault = r;
    fault.exits[1].endpoint = "s2";
    auto checked = plan_exchange(q, fault);
    REQUIRE_FALSE(checked);
    REQUIRE(checked.diagnostics().front().code == "NCL_SWITCH_RETURN_ENDPOINT");
  }
  SECTION("input declaration order does not change derived routes") {
    auto permuted = q;
    std::ranges::reverse(permuted.nodes);
    std::ranges::reverse(permuted.edges);
    std::ranges::reverse(permuted.entries);
    std::ranges::reverse(permuted.exits);
    auto checked = plan_switch_graph(permuted);
    REQUIRE(checked);
    REQUIRE(checked.value().routes == baseline.value().request.routes);
    REQUIRE(checked.value().outputs == baseline.value().request.outputs);
    REQUIRE(checked.value().topological_order == baseline.value().request.topological_order);
  }
}

TEST_CASE("Fixed routes select one of several simple paths", "[switch-graph]") {
  // A diamond a -> {b, c} -> d with links both ways between b and c: every pair has several
  // paths, so each takes a fixed route; b -> c and c -> b both carry one, and no route continues
  // around them.
  DirectedSwitchGraph g{{"a", "b", "c", "d"},
                        {{"a", "b"}, {"a", "c"}, {"b", "c"}, {"c", "b"}, {"b", "d"}, {"c", "d"}},
                        {{"s0", "a"}, {"s1", "a"}},
                        {{"t0", "d"}, {"t1", "d"}}};
  g.fixed_routes = {{"s0", "t0", {"a", "b", "d"}},
                    {"s0", "t1", {"a", "c", "d"}},
                    {"s1", "t0", {"a", "c", "b", "d"}},
                    {"s1", "t1", {"a", "b", "c", "d"}}};
  const auto planned = plan_switch_graph(g);
  REQUIRE(planned);
  REQUIRE(planned.value().routes == g.fixed_routes);
  auto reject = [](const DirectedSwitchGraph &fault, const std::string &code) {
    auto checked = plan_switch_graph(fault);
    REQUIRE_FALSE(checked);
    REQUIRE(checked.diagnostics().front().code == code);
    return checked.diagnostics().front();
  };
  SECTION("without a fixed route the pair is ambiguous") {
    auto fault = g;
    fault.fixed_routes.pop_back();
    reject(fault, "NCL_SWITCH_ROUTE_AMBIGUOUS");
  }
  SECTION("a fixed route follows declared links between its attachment nodes") {
    auto fault = g;
    fault.fixed_routes[1].nodes = {"a", "d"};
    REQUIRE(reject(fault, "NCL_ROUTE_PATH").field == "switches.routes[1]");
    fault.fixed_routes[1].nodes = {"b", "d"};
    reject(fault, "NCL_ROUTE_PATH");
    fault.fixed_routes[1].nodes = {"a", "x", "d"};
    reject(fault, "NCL_ROUTE_REFERENCE");
    fault = g;
    fault.fixed_routes.push_back(fault.fixed_routes.front());
    reject(fault, "NCL_ROUTE_DUPLICATE");
    fault = g;
    fault.admitted_pairs = std::vector<SwitchPair>{{"s0", "t0"}, {"s0", "t1"}, {"s1", "t1"}};
    reject(fault, "NCL_ROUTE_REFERENCE");
  }
  SECTION("a route passes a switch again over another link; taking a link twice chains a wait "
          "cycle") {
    auto again = g;
    again.fixed_routes[2].nodes = {"a", "b", "c", "b", "d"};
    REQUIRE(plan_switch_graph(again));
    again.fixed_routes[2].nodes = {"a", "b", "c", "b", "c", "d"};
    reject(again, "NCL_SWITCH_ROUTE_CYCLE");
  }
  SECTION("routes toward one exit split at a switch input by their entry") {
    auto split = g;
    split.nodes.push_back("m");
    split.edges.push_back({"m", "a"});
    split.entries = {{"s0", "m"}, {"s1", "m"}};
    for (auto &route : split.fixed_routes)
      route.nodes.insert(route.nodes.begin(), "m");
    const auto merged = plan_switch_graph(split);
    REQUIRE(merged);
    REQUIRE(exit_split(merged.value()));
    REQUIRE_FALSE(flow_split(merged.value()));
  }
  SECTION("each initiator flow of a pair takes its fixed route or the pair's unique route") {
    auto flows = g;
    flows.flows = {{"s0", 2}};
    flows.fixed_routes[0].flows = {0};
    flows.fixed_routes[1].flows = {0};
    // Flow 1 of s0 needs routes of its own: the pairs are ambiguous without them.
    reject(flows, "NCL_SWITCH_ROUTE_AMBIGUOUS");
    flows.fixed_routes.push_back({"s0", "t0", {"a", "c", "d"}, {1}});
    flows.fixed_routes.push_back({"s0", "t1", {"a", "c", "d"}, {1}});
    const auto parted = plan_switch_graph(flows);
    REQUIRE(parted);
    REQUIRE(flow_split(parted.value()));
    std::vector<SwitchRoute> s0;
    std::ranges::copy_if(parted.value().routes, std::back_inserter(s0),
                         [](const SwitchRoute &r) { return r.entry == "s0"; });
    REQUIRE(s0 == std::vector<SwitchRoute>{{"s0", "t0", {"a", "b", "d"}, {0}},
                                           {"s0", "t0", {"a", "c", "d"}, {1}},
                                           {"s0", "t1", {"a", "c", "d"}, {0, 1}}});
    auto fault = flows;
    fault.fixed_routes.back().flows = {2};
    reject(fault, "NCL_ROUTE_REFERENCE");
    fault = flows;
    fault.fixed_routes.push_back(flows.fixed_routes.back());
    reject(fault, "NCL_ROUTE_DUPLICATE");
    // The response graph keys flows by its exits; return paths number the distinct sequences.
    DirectedSwitchGraph back{{"r"}, {}, {{"t0", "r"}, {"t1", "r"}}, {{"s0", "r"}}};
    back.response = true;
    back.flows = {{"s0", 2}};
    const auto returned = plan_switch_graph(back);
    REQUIRE(returned);
    REQUIRE(returned.value().routes.size() == 2);
    REQUIRE(returned.value().routes.front().flows == std::vector<unsigned>{0, 1});
    REQUIRE(return_path(returned.value(), returned.value().routes.back()) == 0);
  }
}
