#include "../public_artifacts.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>

namespace {
// The Weft source field widths, seen in the crossing FIFOs.
std::set<unsigned> source_field(const nocloom::Definition &d) {
  return public_artifacts(target_domain(d)).cut_field_widths("source");
}
} // namespace

TEST_CASE("Actual AXI graphs lower branches and three-source packet grants", "[graph-axi]") {
  using namespace nocloom;
  const auto d = graph_example_endpoints(3);
  const auto a = public_artifacts(d);
  CHECK(a.map["network"]["request_routes"].size() == 9);
  // The merge switch (req_node4) grants packets of three sources toward branch (req_node0), with
  // storage bound to the grant.
  CHECK(a.resource("graph/req/req_node4>node/req_node0") != nullptr);
  CHECK(a.storage("graph/req/req_node4>node/req_node0") != nullptr);
  CHECK(source_field(d) == std::set<unsigned>{3});
  auto bad = d;
  bad.topology->request.entries.pop_back();
  bad.topology->response.exits.pop_back();
  CHECK_FALSE(validate(bad));
  bad = d;
  bad.targets[2].mappings[0].base = 0;
  CHECK_FALSE(validate(bad));
}

TEST_CASE("Graph normalization and minimum identity are deterministic", "[graph-axi]") {
  using namespace nocloom;
  const auto single = public_artifacts(graph_example_endpoints(1));
  CHECK(single.map["network"]["request_routes"].size() == 1);
  CHECK(source_field(graph_example_endpoints(1)) == std::set<unsigned>{1});
  auto d = graph_example_endpoints(3);
  const auto forward = generate(d);
  REQUIRE(forward);
  std::ranges::reverse(d.initiators);
  std::ranges::reverse(d.targets);
  std::ranges::reverse(d.topology->request.nodes);
  std::ranges::reverse(d.topology->request.links);
  std::ranges::reverse(d.topology->response.entries);
  const auto reverse = generate(d);
  REQUIRE(reverse);
  CHECK(forward.value() == reverse.value());
}
