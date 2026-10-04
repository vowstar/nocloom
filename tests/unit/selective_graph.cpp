#include "../public_artifacts.hpp"
#include "core/axi4_initiator.hpp"
#include <catch2/catch_test_macros.hpp>
#include <nocloom/generation.hpp>

TEST_CASE("Selective graphs bind exactly inverse request and return pairs", "[graph-selective]") {
  using namespace nocloom;
  // Each source reaches its own and the next target; both directions carry exactly those pairs.
  const auto d = graph_example_definition(true);
  const auto a = public_artifacts(d);
  CHECK(a.map["network"]["request_routes"].size() == 6);
  CHECK(a.map["network"]["response_routes"].size() == 6);
  std::set<std::pair<std::string, std::string>> request, response;
  for (const auto &r : a.map["network"]["request_routes"])
    request.emplace(r["entry"], r["exit"]);
  for (const auto &r : a.map["network"]["response_routes"])
    response.emplace(r["exit"], r["entry"]);
  CHECK(request == response);
  CHECK(request.contains({"s0", "t1"}));
  CHECK_FALSE(request.contains({"s0", "t2"}));
  auto bad = d;
  bad.connectivity->pairs.push_back(bad.connectivity->pairs.front());
  CHECK_FALSE(validate(bad));
  bad = d;
  bad.connectivity->pairs.front().source = "absent";
  CHECK_FALSE(validate(bad));
}

TEST_CASE("Public selective pairs normalize order and reject unused endpoint sets",
          "[graph-selective]") {
  using namespace nocloom;
  auto d = graph_example_definition(true);
  REQUIRE(validate(d));
  auto normal = generate(d);
  REQUIRE(normal);
  std::ranges::reverse(d.connectivity->pairs);
  auto reordered = generate(d);
  REQUIRE(reordered);
  REQUIRE(normal.value() == reordered.value());
  d.connectivity->pairs.clear();
  REQUIRE_FALSE(validate(d));
  d = graph_example_definition(true);
  std::erase_if(d.connectivity->pairs, [](const auto &r) { return r.source == "s0"; });
  REQUIRE_FALSE(validate(d));
}

TEST_CASE("Source decode admission adds no completion state", "[graph-selective]") {
  using namespace nocloom::detail;
  Axi4InitiatorProfile p{"source", 32, 32, 4, 8};
  auto base = lower_axi4_initiator(p);
  REQUIRE(base);
  p.admitted_windows = std::vector<nocloom::AddressWindow>{{0, 4096}, {8192, 4096}};
  auto limited = lower_axi4_initiator(p);
  REQUIRE(limited);
  REQUIRE(base.value().ir.states.size() == limited.value().ir.states.size());
  for (const auto *key : {"wr.route_mapped", "rr.route_mapped"})
    REQUIRE(limited.value().bindings.contains(key));
  // With a miss window a miss leaves as an error request, still without extra state.
  p.miss_window = nocloom::AddressWindow{0, 4096};
  auto missing = lower_axi4_initiator(p);
  REQUIRE(missing);
  REQUIRE(base.value().ir.states.size() == missing.value().ir.states.size());
  for (const auto *key : {"wr.missing", "rr.missing"})
    REQUIRE(missing.value().bindings.contains(key));
  p.miss_window.reset();
  p.admitted_windows->front().base = 1;
  REQUIRE_FALSE(lower_axi4_initiator(p));
  p.admitted_windows = std::vector<nocloom::AddressWindow>{{0xfffff000ULL, 8192}};
  REQUIRE_FALSE(lower_axi4_initiator(p));
}
