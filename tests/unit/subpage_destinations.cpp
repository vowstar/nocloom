#include "../graph_example_definition.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan_profiles.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/generation.hpp>

TEST_CASE("Destination tracking excludes unreachable windows in an admitted page",
          "[subpage-destinations]") {
  auto d = graph_example_definition();
  d.topology.reset();
  d.initiators.resize(2);
  d.targets.resize(2);
  d.connectivity = nocloom::Connectivity{.all = false, .pairs = {{"s0", "t0"}, {"s1", "t1"}}};
  d.initiators[1].max_outstanding = 4;
  d.targets[0].mappings = {{0x1400, 0x200}};
  d.targets[1].mappings = {{0x1800, 0x400}};
  SECTION("separate targets") {}
  SECTION("separate target flows") {
    d.targets.resize(1);
    d.targets[0].flows = 2;
    d.targets[0].mappings.push_back({0x1800, 0x400, 0, {}, 1});
    d.connectivity->pairs[0].target_path = 0;
    d.connectivity->pairs[1].target = "t0";
    d.connectivity->pairs[1].target_path = 1;
  }
  REQUIRE(nocloom::validate(d));
  const auto plan = nocloom::detail::build_network_plan(d);
  REQUIRE(plan);
  const auto profile = nocloom::detail::initiator_profile(plan.value(), 1);
  REQUIRE(profile.destinations.size() == 2);
  CHECK(profile.hidden_destinations == std::vector<unsigned>{0});
  const auto hardware = nocloom::detail::lower_axi4_exchange_composition(plan.value());
  for (const auto &diagnostic : hardware.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(hardware);
  CHECK(nocloom::generate(d));
}
