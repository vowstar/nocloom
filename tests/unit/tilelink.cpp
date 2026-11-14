#include "core/cone_walk.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include <catch2/catch_test_macros.hpp>
#include <nocloom/generation.hpp>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
Definition tilelink_definition() {
  Definition d;
  d.name = "tilelink_ports";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s", "main"}};
  d.targets = {{"t", "main", {{0, 4096}}}};
  d.initiators[0].protocol = Protocol::TlUl;
  d.initiators[0].data_width = 32;
  d.initiators[0].max_outstanding = 4;
  d.targets[0].protocol = Protocol::TlUl;
  d.targets[0].data_width = 32;
  d.connectivity = Connectivity{.all = true};
  return d;
}
} // namespace

TEST_CASE("TileLink outputs have no combinational protocol input paths", "[tilelink]") {
  const auto plan = build_network_plan(tilelink_definition());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  const auto &ir = hardware.value().ir;
  unsigned checked = 0;
  for (const auto &driver : ir.drivers) {
    const auto &port = ir.ports.at(driver.port.index);
    if (port.endpoint != "initiator:s" && port.endpoint != "target:t")
      continue;
    bool protocol_input = false;
    walk_cone(
        ir, driver.value, [](ValueId) {},
        [&](ValueId leaf) {
          const auto &input = ir.ports.at(ir.values.at(leaf.index).input_port.index);
          protocol_input |= input.endpoint == "initiator:s" || input.endpoint == "target:t";
        });
    INFO(port.name);
    CHECK_FALSE(protocol_input);
    ++checked;
  }
  CHECK(checked >= 16);
}

TEST_CASE("TileLink direct definitions reject unsupported target capacity", "[tilelink]") {
  auto d = tilelink_definition();
  d.targets[0].max_outstanding = 2;
  const auto result = generate(d);
  REQUIRE_FALSE(result);
  REQUIRE_FALSE(result.diagnostics().empty());
  CHECK(result.diagnostics().front().code == "NCL_TILELINK_PROFILE");
  CHECK(result.diagnostics().front().field == "targets[0].max_outstanding");
}

TEST_CASE("TileLink diagnostics identify each unsupported user field", "[tilelink]") {
  auto d = tilelink_definition();
  std::vector<std::string> fields;
  SECTION("initiator request user") {
    d.initiators[0].user_width = 1;
    fields = {"initiators[0].user_width"};
  }
  SECTION("initiator response user") {
    d.initiators[0].response_user_width = 1;
    fields = {"initiators[0].response_user_width"};
  }
  SECTION("both initiator user fields") {
    d.initiators[0].user_width = 1;
    d.initiators[0].response_user_width = 1;
    fields = {"initiators[0].user_width", "initiators[0].response_user_width"};
  }
  SECTION("target attributes") {
    d.targets[0].attributes = TargetAttributes::amba();
    fields = {"targets[0].attributes"};
  }
  SECTION("target user") {
    d.targets[0].user = {{"", true}};
    fields = {"targets[0].user"};
  }
  SECTION("both target user fields") {
    d.targets[0].attributes = TargetAttributes::amba();
    d.targets[0].user = {{"", true}};
    fields = {"targets[0].attributes", "targets[0].user"};
  }
  const auto result = generate(d);
  REQUIRE_FALSE(result);
  REQUIRE(result.diagnostics().size() == fields.size());
  for (unsigned k = 0; k < fields.size(); ++k) {
    CHECK(result.diagnostics()[k].code == "NCL_TILELINK_PROFILE");
    CHECK(result.diagnostics()[k].field == fields[k]);
  }
}
