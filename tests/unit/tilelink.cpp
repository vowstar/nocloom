#include "core/cone_walk.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include "core/tilelink_uh_outlet.hpp"
#include <bit>
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
    if (fields[k] == "initiators[0].user_width")
      CHECK(result.diagnostics()[k].message == "TL-UL has no user ports");
    if (fields[k] == "initiators[0].response_user_width")
      CHECK(result.diagnostics()[k].message == "TL-UL has no response user ports");
  }
}

TEST_CASE("TileLink UH validates message size and target packet capacity", "[tilelink]") {
  auto d = tilelink_definition();
  d.initiators[0].protocol = Protocol::TlUh;
  SECTION("default profile") {
    const auto p = build_network_plan(d);
    REQUIRE(p);
    CHECK(p.value().sources[0].tl_size_bits == 3);
    CHECK(p.value().sources[0].tl_max_transfer_bytes == 64);
  }
  SECTION("explicit size encoding") {
    d.initiators[0].max_transfer_bytes = 1024;
    d.initiators[0].size_width = 3;
    const auto r = generate(d);
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_TILELINK_PROFILE");
    CHECK(r.diagnostics().front().field == "initiators[0].size_width");
  }
  SECTION("target protocol") {
    d.targets[0].protocol = Protocol::TlUh;
    const auto r = generate(d);
    REQUIRE(r);
  }
  SECTION("target capacity limit") {
    d.targets[0].protocol = Protocol::TlUh;
    d.targets[0].max_outstanding = 17;
    const auto r = generate(d);
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_TILELINK_PROFILE");
    CHECK(r.diagnostics().front().field == "targets[0].max_outstanding");
  }
}

TEST_CASE("TL-UH target sources encode the shared packet capacity", "[tilelink]") {
  for (unsigned capacity = 1; capacity <= 16; ++capacity) {
    auto d = tilelink_definition();
    d.targets[0].protocol = Protocol::TlUh;
    d.targets[0].max_outstanding = capacity;
    const auto bits = static_cast<unsigned>(std::bit_width(capacity - 1));
    const auto plan = build_network_plan(d);
    REQUIRE(plan);
    CHECK(plan.value().targets[0].tl_source_bits == bits);
    CHECK(plan.value().targets[0].outstanding == capacity);
    if (bits) {
      d.targets[0].source_width = bits - 1;
      const auto invalid = generate(d);
      REQUIRE_FALSE(invalid);
      CHECK(invalid.diagnostics().front().code == "NCL_TILELINK_PROFILE");
      CHECK(invalid.diagnostics().front().field == "targets[0].source_width");
    }
  }
}

TEST_CASE("TL-UH large targets keep AXI and packet lengths separate", "[tilelink]") {
  auto d = tilelink_definition();
  auto &source = d.initiators[0];
  source.protocol = Protocol::Axi4;
  source.data_width = 128;
  source.id_width = 4;
  source.len_width = 8;
  auto &target = d.targets[0];
  target.protocol = Protocol::TlUh;
  target.max_transfer_bytes = 4096;
  target.max_outstanding = 3;
  target.size_width = 4;
  REQUIRE(generate(d));
  const auto plan = build_network_plan(d);
  REQUIRE(plan);
  CHECK(plan.value().sources[0].axi_len_bits == 8);
  CHECK(plan.value().packet_len_bits == 10);

  TileLinkUhOutletProfile profile;
  profile.max_transfer_bytes = 4096;
  profile.size_bits = 3;
  const auto invalid = lower_tilelink_uh_outlet(profile);
  REQUIRE_FALSE(invalid);
  CHECK(invalid.diagnostics().front().code == "NCL_TILELINK_PROFILE");
}

TEST_CASE("TL-UH outlet watchdog rejects an out-of-range count", "[tilelink]") {
  TileLinkUhOutletProfile profile;
  for (unsigned cycles : {15U, 1048577U}) {
    profile.packet.timeout_cycles = cycles;
    const auto rejected = lower_tilelink_uh_outlet(profile);
    REQUIRE_FALSE(rejected);
    CHECK(rejected.diagnostics().front().code == "NCL_TILELINK_PROFILE");
    CHECK(rejected.diagnostics().front().field == "timeout_cycles");
  }
}
