#include "../tree_topology_fixture.hpp"
#include "core/switch_graph.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <tuple>
using namespace nocloom;

namespace {
detail::DirectedSwitchGraph structure(const ExchangeGraph &graph) {
  detail::DirectedSwitchGraph g;
  for (const auto &n : graph.nodes)
    g.nodes.push_back(n.name);
  for (const auto &e : graph.links)
    g.edges.push_back({e.from, e.to});
  for (const auto &e : graph.entries)
    g.entries.push_back({e.endpoint, e.node});
  for (const auto &e : graph.exits)
    g.exits.push_back({e.endpoint, e.node});
  return g;
}
void rejected(const Result<ExchangeTopology> &result, const std::string &code,
              const std::string &field) {
  REQUIRE_FALSE(result);
  REQUIRE_FALSE(result.diagnostics().empty());
  CHECK(result.diagnostics().front().code == code);
  CHECK(result.diagnostics().front().field == field);
}
} // namespace

TEST_CASE("Tree helper preserves explicit roles and every directed route", "[tree-topology]") {
  const auto nodes = tree_topology_fixture::nodes();
  const auto result = tree_topology(nodes, {"dma", "cpu"});
  REQUIRE(result);
  CHECK(result.value() == tree_topology_fixture::explicit_graphs());
  const auto plan =
      detail::plan_exchange(structure(result.value().request), structure(result.value().response));
  REQUIRE(plan);
  REQUIRE(plan.value().request.routes.size() == 8);
  REQUIRE(plan.value().response.routes.size() == 8);
  const std::vector<std::vector<std::string>> paths{{"root_node"},
                                                    {"root_node", "branch_a"},
                                                    {"root_node", "branch_a", "leaf_a"},
                                                    {"root_node", "branch_b"}};
  for (const auto &source : {"cpu", "dma"})
    for (unsigned i = 0; i < paths.size(); ++i) {
      const auto target = "t" + std::to_string(i);
      const auto request = std::ranges::find_if(plan.value().request.routes, [&](const auto &r) {
        return r.entry == source && r.exit == target;
      });
      const auto response = std::ranges::find_if(plan.value().response.routes, [&](const auto &r) {
        return r.entry == target && r.exit == source;
      });
      REQUIRE(request != plan.value().request.routes.end());
      REQUIRE(response != plan.value().response.routes.end());
      CHECK(request->nodes == paths[i]);
      auto reversed = paths[i];
      std::ranges::reverse(reversed);
      CHECK(response->nodes == reversed);
    }
  SECTION("minimal tree and separate role namespaces") {
    const auto same = tree_topology({{"shared", std::nullopt, {"shared"}}}, {"shared"});
    REQUIRE(same);
    CHECK(same.value().request.links.empty());
    CHECK(same.value().request.entries == std::vector<ExchangeAttachment>{{"shared", "shared"}});
    CHECK(same.value().response.exits == same.value().request.entries);
  }
  SECTION("the public node boundary is checked before graph planning") {
    std::vector<TreeNode> chain;
    for (unsigned i = 0; i < 128; ++i)
      chain.push_back({"node" + std::to_string(i),
                       i ? std::optional("node" + std::to_string(i - 1)) : std::nullopt,
                       {}});
    chain.back().targets = {"target"};
    REQUIRE(tree_topology(chain, {"source"}));
    chain.back().targets.clear();
    chain.push_back({"node128", "node127", {"target"}});
    rejected(tree_topology(chain, {"source"}), "NCL_TREE_NODE_COUNT", "tree.nodes");
  }
}

TEST_CASE("Tree helper canonicalizes permutations without changing inputs", "[tree-topology]") {
  auto nodes = tree_topology_fixture::nodes();
  nodes[0].clock_domain = "target_clock";
  nodes[2].clock_domain = "source_clock";
  nodes[3].targets.push_back("another_target");
  const auto original = nodes;
  const std::vector<std::string> sources{"dma", "cpu"};
  const auto expected = tree_topology(nodes, sources);
  REQUIRE(expected);
  for (const auto *graph : {&expected.value().request, &expected.value().response})
    for (const auto &n : graph->nodes) {
      const auto found = std::ranges::find(original, n.name, &TreeNode::name);
      REQUIRE(found != original.end());
      CHECK(n.clock_domain == found->clock_domain);
      CHECK_FALSE(n.transport_word_bytes);
      CHECK(n.arbitration.empty());
    }
  for (std::size_t i = 0; i < nodes.size(); ++i)
    CHECK(std::tie(nodes[i].name, nodes[i].parent, nodes[i].targets, nodes[i].clock_domain) ==
          std::tie(original[i].name, original[i].parent, original[i].targets,
                   original[i].clock_domain));
  std::ranges::sort(nodes, {}, &TreeNode::name);
  unsigned permutations = 0;
  do {
    for (auto &node : nodes)
      std::ranges::reverse(node.targets);
    auto reordered_sources = sources;
    std::ranges::reverse(reordered_sources);
    const auto actual = tree_topology(nodes, reordered_sources);
    REQUIRE(actual);
    CHECK(actual.value() == expected.value());
    ++permutations;
  } while (std::next_permutation(nodes.begin(), nodes.end(),
                                 [](const auto &a, const auto &b) { return a.name < b.name; }));
  CHECK(permutations == 24);
}

TEST_CASE("Tree helper reports original parameter indices and rejects empty branches",
          "[tree-topology]") {
  const std::vector<TreeNode> good{{"leaf", "root_node", {"target"}},
                                   {"root_node", std::nullopt, {}}};
  auto nodes = good;
  SECTION("empty arguments") {
    rejected(tree_topology({}, {"cpu"}), "NCL_TREE_NODE_COUNT", "tree.nodes");
    rejected(tree_topology(good, {}), "NCL_TREE_INITIATOR_COUNT", "tree.initiators");
    nodes[0].targets.clear();
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_TARGET_COUNT", "tree.nodes");
  }
  SECTION("invalid or reserved names") {
    nodes[1].name = "module";
    rejected(tree_topology(nodes, {"cpu"}), "NCL_NAME_INVALID", "tree.nodes[1].name");
    rejected(tree_topology(good, {"cpu", "nl_reserved"}), "NCL_NAME_INVALID", "tree.initiators[1]");
    nodes = good;
    nodes[0].targets = {"target", "bad-name"};
    rejected(tree_topology(nodes, {"cpu"}), "NCL_NAME_INVALID", "tree.nodes[0].targets[1]");
  }
  SECTION("duplicate roles") {
    nodes.push_back(good.front());
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_NODE_DUPLICATE", "tree.nodes[2].name");
    rejected(tree_topology(good, {"cpu", "cpu"}), "NCL_TREE_INITIATOR_DUPLICATE",
             "tree.initiators[1]");
    nodes = good;
    nodes[0].targets.push_back("target");
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_TARGET_DUPLICATE",
             "tree.nodes[0].targets[1]");
    nodes = good;
    nodes[1].targets.push_back("target");
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_TARGET_DUPLICATE",
             "tree.nodes[1].targets[0]");
  }
  SECTION("root and parent errors") {
    nodes[0].parent.reset();
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_ROOT_COUNT", "tree.nodes");
    nodes = good;
    nodes[1].parent = "leaf";
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_ROOT_COUNT", "tree.nodes");
    nodes = good;
    nodes[0].parent = "absent";
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_PARENT_MISSING", "tree.nodes[0].parent");
    nodes[0].parent = "leaf";
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_PARENT_SELF", "tree.nodes[0].parent");
  }
  SECTION("one root does not hide a disconnected cycle") {
    nodes.push_back({"a", "b", {}});
    nodes.push_back({"b", "a", {}});
    rejected(tree_topology(nodes, {"cpu"}), "NCL_TREE_CYCLE", "tree.nodes[2].parent");
  }
  SECTION("graph planner rejects a declared but unused branch") {
    nodes.push_back({"empty_branch", "root_node", {}});
    rejected(tree_topology(nodes, {"cpu"}), "NCL_SWITCH_UNUSED_OBJECT", "switches");
  }
}

TEST_CASE("Tree helper and independent explicit graphs emit the same products", "[tree-topology]") {
  auto d = tree_topology_fixture::definition();
  const auto topology = tree_topology(tree_topology_fixture::nodes(), {"dma", "cpu"});
  REQUIRE(topology);
  d.topology = topology.value();
  const auto plan = make_plan(d);
  REQUIRE(plan);
  const auto generated = emit(plan.value());
  REQUIRE(generated);
  d.topology = tree_topology_fixture::explicit_graphs();
  const auto handwritten = generate(d);
  REQUIRE(handwritten);
  CHECK(generated.value() == handwritten.value());
  REQUIRE(generated.value().size() == 6);
  for (const auto &artifact : generated.value())
    CHECK_FALSE(artifact.text.empty());
  SECTION("endpoint declarations remain the complete definition's responsibility") {
    auto nodes = tree_topology_fixture::nodes();
    nodes[0].targets = {"unknown_target"};
    const auto altered = tree_topology(nodes, {"cpu", "dma"});
    REQUIRE(altered);
    d.topology = altered.value();
    const auto checked = make_plan(d);
    REQUIRE_FALSE(checked);
    CHECK(checked.diagnostics().front().code == "NCL_SWITCH_PAIR");
    CHECK(checked.diagnostics().front().field == "connectivity");
  }
}
