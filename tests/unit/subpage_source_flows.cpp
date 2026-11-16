#include "../graph_example_definition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/generation.hpp>

namespace {
nocloom::Definition flow_pages() {
  auto d = graph_example_definition();
  d.topology.reset();
  d.initiators.resize(2);
  d.targets.resize(2);
  auto &s = d.initiators[0];
  s.max_outstanding = 4;
  s.id_width = 2;
  s.route_classes = {"even", "odd"};
  s.default_class = "odd";
  s.id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  d.targets[0].mappings = {{0x1400, 0x200}};
  d.targets[1].mappings = {{0x1800, 0x400}};
  d.connectivity = nocloom::Connectivity{
      .pairs = {{"s0", "t0", 0}, {"s0", "t1", 1}, {"s1", "t0"}, {"s1", "t1"}}};
  return d;
}
} // namespace

TEST_CASE("Source flows with conflicting sub-page admission are rejected",
          "[subpage-source-flows]") {
  auto d = flow_pages();
  SECTION("implicit default") {}
  SECTION("explicit default") { d.initiators[0].default_target = "t1"; }
  SECTION("AXI3 source") {
    d.initiators[0].protocol = nocloom::Protocol::Axi3;
    d.initiators[0].len_width = 4;
  }
  SECTION("read-only source") { d.initiators[0].access = nocloom::Access::ReadOnly; }
  SECTION("write-only source") { d.initiators[0].access = nocloom::Access::WriteOnly; }
  SECTION("overlapping flow masks") { d.connectivity->pairs.push_back({"s0", "t0", 1}); }
  SECTION("routing sub-pages of a larger mapping") { d.targets[0].mappings = {{0x1000, 0x1000}}; }
  SECTION("32 source flows") {
    auto &s = d.initiators[0];
    s.id_width = 5;
    for (unsigned f = 2; f < 32; ++f) {
      s.route_classes.push_back("class" + std::to_string(f));
      d.connectivity->pairs.push_back({"s0", "t0", f});
    }
    s.default_class = s.route_classes.back();
    s.id_ranges.clear();
    for (unsigned f = 0; f < 31; ++f)
      s.id_ranges.push_back({f, f, s.route_classes[f]});
  }
  SECTION("target flow selection") {
    d.targets[1].flows = 2;
    d.targets[1].mappings.push_back({0x4000, 0x1000, 0, {}, 1});
    d.connectivity->pairs[1].target_path = 0;
  }
  SECTION("highest page") {
    for (auto &t : d.targets)
      t.mappings[0].base += 0xFFFFE000ULL;
  }
  SECTION("wide address") {
    d.address_width = 48;
    for (auto &t : d.targets)
      t.mappings[0].base += 1ULL << 40;
  }
  SECTION("definition order") {
    std::ranges::reverse(d.targets);
    std::ranges::reverse(d.initiators);
    std::ranges::reverse(d.connectivity->pairs);
  }
  const auto valid = nocloom::validate(d);
  REQUIRE_FALSE(valid);
  REQUIRE(valid.diagnostics().size() == 1);
  const auto &diagnostic = valid.diagnostics()[0];
  CHECK(diagnostic.code == "NCL_SOURCE_PATH");
  CHECK(diagnostic.field == "connectivity.s0");
  for (const auto *text : {"s0", "flow 0 (even)", "flow 1 (odd)", "t0 flow 0 window",
                           "t1 flow 0 window", "4 KiB page", "max_outstanding"})
    CHECK(diagnostic.message.find(text) != std::string::npos);
  const auto generated = nocloom::generate(d);
  REQUIRE_FALSE(generated);
  CHECK(generated.diagnostics()[0].code == diagnostic.code);
  CHECK(generated.diagnostics()[0].message == diagnostic.message);
}

TEST_CASE("Source flow admission keeps supported page combinations", "[subpage-source-flows]") {
  auto d = flow_pages();
  SECTION("one outstanding burst") { d.initiators[0].max_outstanding = 1; }
  SECTION("whole pages") {
    d.targets[0].mappings = {{0x1000, 0x1000}};
    d.targets[1].mappings = {{0x2000, 0x1000}};
  }
  SECTION("sub-pages in different pages") { d.targets[1].mappings[0].base = 0x2800; }
  SECTION("equal masks") {
    d.connectivity->pairs[0].source_path.reset();
    d.connectivity->pairs[1].source_path.reset();
  }
  SECTION("only default target has a smaller mask") {
    d.connectivity->pairs[1].source_path.reset();
  }
  SECTION("named default target has a smaller mask") {
    d.initiators[0].default_target = "t1";
    d.connectivity->pairs[0].source_path.reset();
  }
  SECTION("target flows at one default target") {
    d.targets[0].flows = 2;
    d.targets[0].mappings.push_back({0x1800, 0x400, 0, {}, 1});
    d.targets.resize(1);
    d.connectivity->pairs = {{"s0", "t0", 0, 0}, {"s0", "t0", 1, 1}, {"s1", "t0"}};
  }
  SECTION("globally unreachable window in the page") {
    d.targets.push_back(graph_target("t2", {0x1C00, 0x100}));
    d.connectivity->pairs[1].source_path.reset();
    d.connectivity->pairs.push_back({"s1", "t2"});
  }
  SECTION("one direct source flow") {
    d.initiators[0].route_classes.clear();
    d.initiators[0].default_class.reset();
    d.initiators[0].id_ranges.clear();
    d.connectivity->pairs[0].source_path.reset();
    d.connectivity->pairs[1].source_path.reset();
  }
  const auto valid = nocloom::validate(d);
  for (const auto &diagnostic : valid.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(valid);
  const auto generated = nocloom::generate(d);
  for (const auto &diagnostic : generated.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  CHECK(generated);
}
