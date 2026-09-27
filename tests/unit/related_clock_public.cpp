#include "../endpoint_ports.hpp"
#include "../public_artifacts.hpp"
#include "core/exchange_composition.hpp"
#include <catch2/catch_test_macros.hpp>
#include <fstream>
#include <map>
#include <nocloom/config.hpp>
#include <nocloom/topology.hpp>

using namespace nocloom::detail;
namespace {
nocloom::Definition related(unsigned middle_ratio = 2, unsigned target_ratio = 4) {
  nocloom::Definition d;
  d.name = "related_public";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test", 10000},
                     {"middle", "clk_main", "reset_n", "test", 10000 * middle_ratio},
                     {"target", "clk_main", "reset_n", "test", 10000 * target_ratio}};
  d.clock_roots = {{"root", "clk_main", 10000}};
  for (auto &domain : d.clock_domains)
    domain.clock_root = "root";
  d.clock_domains[1].clock_type = nocloom::ClockType::InternalDivider;
  d.clock_domains[1].ratio = middle_ratio;
  d.clock_domains[1].edge_parent = "main";
  d.clock_domains[2].clock_type = nocloom::ClockType::InternalDivider;
  d.clock_domains[2].ratio =
      target_ratio % middle_ratio == 0 ? target_ratio / middle_ratio : target_ratio;
  d.clock_domains[2].edge_parent = target_ratio % middle_ratio == 0 ? "middle" : "main";
  d.initiators = {{"host0", "main"}, {"host1", "main"}};
  d.targets = {{"bank0", "target", {{0, 4096}}}, {"bank1", "target", {{4096, 4096}}}};
  for (auto &source : d.initiators) {
    source.protocol = nocloom::Protocol::Axi4Lite;
    source.data_width = 32;
  }
  for (auto &target : d.targets) {
    target.protocol = nocloom::Protocol::Apb4;
    target.data_width = 32;
  }
  d.transport = nocloom::TransportOptions{};
  d.transport->transport_word_bytes = 4;
  endpoint_ports(d, 4, 8);
  d.connectivity = nocloom::Connectivity{.all = true};
  auto graph = nocloom::tree_topology(
      {{"root", {}, {"bank0"}, "middle"}, {"leaf", "root", {"bank1"}, "middle"}},
      {"host0", "host1"});
  REQUIRE(graph);
  d.topology = std::move(graph.value());
  return d;
}
void aligned_clock_edges(nocloom::Definition &d, bool pipe = true) {
  d.clock_crossings = {{"main", "middle", nocloom::EdgeAlignedCrossing{pipe}},
                       {"middle", "target", nocloom::EdgeAlignedCrossing{pipe}},
                       {"target", "middle", nocloom::EdgeAlignedCrossing{pipe}},
                       {"middle", "main", nocloom::EdgeAlignedCrossing{pipe}}};
  for (const auto &crossing : d.clock_crossings)
    REQUIRE(std::holds_alternative<nocloom::EdgeAlignedCrossing>(crossing.options));
}
void syncs(nocloom::Definition &d, unsigned depth, bool fast = false) {
  d.clock_crossings = {{"main", "middle", nocloom::SyncClockCrossing{depth, fast}},
                       {"middle", "target", nocloom::SyncClockCrossing{depth, fast}},
                       {"target", "middle", nocloom::SyncClockCrossing{depth, fast}},
                       {"middle", "main", nocloom::SyncClockCrossing{depth, fast}}};
  for (const auto &crossing : d.clock_crossings)
    REQUIRE(std::holds_alternative<nocloom::SyncClockCrossing>(crossing.options));
}
std::string sdc(const nocloom::Definition &d) {
  nocloom::GenerationOptions options;
  options.cdc_constraints = true;
  const auto generated = nocloom::generate(d, options);
  REQUIRE(generated);
  return artifact_text(generated.value(), ".cdc.sdc");
}
ExchangeHardware lowered(const nocloom::Definition &d) {
  auto plan = build_network_plan(d);
  REQUIRE(plan);
  auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  REQUIRE(verify_axi4_exchange_composition(hardware.value()));
  return std::move(hardware).value();
}
} // namespace

TEST_CASE("Common roots do not infer related crossings", "[related-clock-public]") {
  auto d = related();
  d.clock_crossings.clear();
  const auto x = public_artifacts(d);
  CHECK_FALSE(x.map["network"]["cdc"]["cuts"].empty());
  for (const auto &cut : x.map["network"]["cdc"]["cuts"]) {
    CHECK_FALSE(cut.contains("kind"));
    CHECK(cut.contains("synchronizer_stages"));
    CHECK(cut.contains("write_count_sync"));
  }
}

TEST_CASE("EdgeAligned covers direct clocks and divided ancestors in both directions",
          "[related-clock-public]") {
  for (const auto ratios : {std::pair{1U, 2U}, {2U, 4U}}) {
    auto d = related(ratios.first, ratios.second);
    aligned_clock_edges(d);
    const auto x = public_artifacts(d);
    CHECK(x.map["definition"]["contract"]["crossing"] ==
          "typed_clock_adapters_common_reset_full_fields");
    REQUIRE(x.map["network"]["cdc"]["cuts"].size() == 8);
    for (const auto &cut : x.map["network"]["cdc"]["cuts"]) {
      CHECK(cut["kind"] == "edge_aligned");
      CHECK(cut["depth"] == 0);
      CHECK(cut["slow_pipeline"] == true);
      CHECK(cut.contains("admission_open") == cut["slow_from_sender"]);
      CHECK(cut["slow_pipeline_storage"].size() == 2 * cut["fields"].size());
      for (const auto &field : cut["fields"])
        CHECK(field["storage"].empty());
    }
    const auto constraints = sdc(d);
    CHECK(constraints.find("Related-clock paths are not in asynchronous clock groups") !=
          std::string::npos);
    CHECK(constraints.find("set_clock_groups -asynchronous") == std::string::npos);
    CHECK(constraints.find("related payload") != std::string::npos);
  }
  auto d = related();
  aligned_clock_edges(d, false);
  const auto artifacts = public_artifacts(d);
  for (const auto &cut : artifacts.map["network"]["cdc"]["cuts"]) {
    CHECK(cut["slow_pipeline"] == false);
    CHECK(cut.contains("admission_open"));
    CHECK_FALSE(cut.contains("slow_pipeline_storage"));
  }
}

TEST_CASE("Related Sync records depth and local return isolation", "[related-clock-public]") {
  auto d = related(2, 3);
  syncs(d, 3);
  const auto plan = build_network_plan(d);
  REQUIRE(plan);
  for (const auto &crossing : plan.value().placement->crossings)
    CHECK(std::holds_alternative<nocloom::SyncClockCrossing>(crossing.options));
  for (const auto &cut : plan.value().cuts) {
    CAPTURE(cut.name, cut.from_domain, cut.to_domain, cut.options.index());
    CHECK(std::holds_alternative<SyncClockCut>(cut.options));
  }
  const auto ordinary = public_artifacts(d);
  for (const auto &cut : ordinary.map["network"]["cdc"]["cuts"]) {
    CHECK(cut["kind"] == "sync");
    CHECK(cut["depth"] == 3);
    CHECK(cut["counter_return_isolation"] == "local_register");
    CHECK(cut["read_count_return"] != cut["read_count"]);
    CHECK(cut["write_count_return"] != cut["write_count"]);
    for (const auto &field : cut["fields"])
      CHECK(field["storage"].size() == 3);
  }
  syncs(d, 2, true);
  const auto fast = public_artifacts(d);
  for (const auto &cut : fast.map["network"]["cdc"]["cuts"]) {
    CHECK(cut["fast"] == true);
    CHECK(cut["counter_return_isolation"] == "direct_related");
    CHECK(cut["read_count_return"] == cut["read_count"]);
    CHECK(cut["write_count_return"] == cut["write_count"]);
    CHECK(cut["receive_valid"] == cut["output_valid"]);
  }
  const auto constraints = sdc(d);
  CHECK(constraints.find("fast_credit_counter_return") == std::string::npos);
  CHECK(constraints.find("related buffered return") != std::string::npos);
}

TEST_CASE("Minimal keeps Sync payload unreset and every count reset", "[related-clock-public]") {
  auto d = related(2, 3);
  syncs(d, 2);
  d.reset_coverage = nocloom::ResetCoverage::Minimal;
  const auto artifacts = public_artifacts(d);
  const auto &cdc = artifacts.map["network"]["cdc"];
  std::map<std::string, bool> has_reset;
  for (const auto &reg : cdc["registers"])
    has_reset[reg["value"].get<std::string>()] = reg.contains("reset");
  REQUIRE(cdc["cuts"].size() == 8);
  for (const auto &cut : cdc["cuts"]) {
    REQUIRE(cut["kind"] == "sync");
    for (const auto *key : {"write_count", "read_count", "write_count_return", "read_count_return",
                            "write_index", "read_index"})
      CHECK(has_reset.at(cut[key].get<std::string>()));
    for (const auto &field : cut["fields"])
      for (const auto &slot : field["storage"])
        CHECK_FALSE(has_reset.at(slot.get<std::string>()));
  }
}

TEST_CASE("Minimal keeps AlignedClockEdges slow pipe fields unreset", "[related-clock-public]") {
  auto d = related();
  aligned_clock_edges(d);
  d.reset_coverage = nocloom::ResetCoverage::Minimal;
  const auto artifacts = public_artifacts(d);
  const auto &cdc = artifacts.map["network"]["cdc"];
  std::map<std::string, bool> has_reset;
  for (const auto &reg : cdc["registers"])
    has_reset[reg["value"].get<std::string>()] = reg.contains("reset");
  REQUIRE_FALSE(cdc["cuts"].empty());
  for (const auto &cut : cdc["cuts"]) {
    REQUIRE_FALSE(cut["slow_pipeline_storage"].empty());
    for (const auto &slot : cut["slow_pipeline_storage"])
      CHECK_FALSE(has_reset.at(slot.get<std::string>()));
  }
}

TEST_CASE("Related clock validation rejects unsupported relations", "[related-clock-public]") {
  auto d = related();
  syncs(d, 3, true);
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_LOW_LATENCY_CREDIT_DEPTH"));
  d = related();
  aligned_clock_edges(d);
  d.clock_domains[1].reset_port = "other_reset_n";
  d.reset_ports.push_back({"other_reset_n"});
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_RELATED_RESET"));
  d = related();
  aligned_clock_edges(d);
  d.clock_domains[1].clock_type = nocloom::ClockType::EdgeQualified;
  d.clock_domains[1].ratio.reset();
  d.clock_domains[1].enable_port = "enable";
  d.enable_ports.push_back({"enable"});
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_RELATED_DYNAMIC"));
  d = related();
  aligned_clock_edges(d);
  d.clock_roots.push_back({"other", "clk_other"});
  d.clock_domains[2].clock_root = "other";
  d.clock_domains[2].clock_port = "clk_other";
  d.clock_domains[2].clock_type = nocloom::ClockType::Direct;
  d.clock_domains[2].ratio.reset();
  d.clock_domains[2].edge_parent.reset();
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_RELATED_ROOT"));
  d = related(2, 3);
  aligned_clock_edges(d);
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_EDGE_ALIGNED_RELATION"));
  d = related();
  aligned_clock_edges(d);
  d.clock_domains[1].edge_parent = "target";
  d.clock_domains[2].edge_parent = "middle";
  CHECK(rejected_with(nocloom::validate(d), "NCL_CLOCK_EDGE"));
}

TEST_CASE("Related cuts carry pressure with the fragment and check admission state",
          "[related-clock-public]") {
  auto d = related();
  for (auto &initiator : d.initiators) {
    initiator.protocol = nocloom::Protocol::Apb4;
    initiator.data_width = 32;
  }
  for (auto &target : d.targets) {
    target.protocol = nocloom::Protocol::Apb4;
    target.data_width = 32;
  }
  d.initiators[0].qos.levels.write = 2;
  aligned_clock_edges(d);
  const auto x = public_artifacts(d);
  bool pressure = false;
  for (const auto &cut : x.map["network"]["cdc"]["cuts"])
    for (const auto &field : cut["fields"])
      if (field["name"] == "pressure") {
        pressure = true;
        CHECK(field["width"] == 2);
      }
  CHECK(pressure);
  auto hardware = lowered(d);
  for (const bool pipelined : {true, false}) {
    if (!pipelined) {
      aligned_clock_edges(d, false);
      hardware = lowered(d);
    }
    auto corrupted = hardware;
    std::optional<ValueId> control;
    for (const auto &[key, id] : corrupted.bindings)
      if (key.ends_with("/admission_open"))
        control = id;
    REQUIRE(control);
    Builder b;
    b.ir = std::move(corrupted.ir);
    b.update(*control, b.constant(1, 0));
    corrupted.ir = std::move(b.ir);
    CHECK_FALSE(verify_axi4_exchange_composition(corrupted));
  }
}

TEST_CASE("Related clock YAML uses the same typed definitions", "[related-clock-public]") {
  std::ifstream input(std::string(NOCLOOM_SOURCE_DIR) + "/examples/cross_tree_axi.yaml");
  std::string text((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
  const auto replace = [&](const std::string &from, const std::string &to) {
    const auto at = text.find(from);
    REQUIRE(at != std::string::npos);
    text.replace(at, from.size(), to);
  };
  replace("clock_domains:\n"
          "  - {name: alpha, clock_port: clk_alpha, reset_port: reset_n, test_port: test}\n"
          "  - {name: beta, clock_port: clk_beta, reset_port: reset_n, test_port: test}\n"
          "synchronizer_stages: 2\n",
          "clock_roots:\n"
          "  - {name: root, clock_port: clk_alpha, period_ps: 1000}\n"
          "clock_domains:\n"
          "  - {name: alpha, clock_port: clk_alpha, reset_port: reset_n, test_port: test, "
          "clock_root: root, period_ps: 1000}\n"
          "  - {name: beta, clock_port: clk_alpha, reset_port: reset_n, test_port: test, "
          "clock_root: root, edge_parent: alpha, clock_type: internal_divider, ratio: 2, "
          "period_ps: 2000}\n");
  replace("  - {from_domain: beta, to_domain: alpha, synchronizer_stages: 4}\n",
          "  - {from_domain: beta, to_domain: alpha, kind: edge_aligned}\n"
          "  - {from_domain: alpha, to_domain: beta, kind: related, depth: 2, fast: true}\n");
  const auto parsed = nocloom::config::parse(text);
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  REQUIRE(d.clock_roots.size() == 1);
  REQUIRE(d.clock_domains[1].edge_parent == "alpha");
  REQUIRE(std::holds_alternative<nocloom::EdgeAlignedCrossing>(d.clock_crossings[0].options));
  REQUIRE(std::holds_alternative<nocloom::SyncClockCrossing>(d.clock_crossings[1].options));
  CHECK(std::get<nocloom::SyncClockCrossing>(d.clock_crossings[1].options).fast);
  REQUIRE(nocloom::validate(d));
  auto bad = text;
  const auto at = bad.find("depth: 2, fast: true");
  REQUIRE(at != std::string::npos);
  bad.replace(at + 7, 1, "3");
  const auto bad_parsed = nocloom::config::parse(bad);
  REQUIRE(bad_parsed);
  CHECK(rejected_with(nocloom::validate(bad_parsed.value().definition),
                      "NCL_CDC_LOW_LATENCY_CREDIT_DEPTH"));
  bad = text;
  const auto kind = bad.find("kind: related");
  REQUIRE(kind != std::string::npos);
  bad.insert(kind + 13, ", synchronizer_stages: 3");
  CHECK(rejected_with(nocloom::config::parse(bad), "NCL_CDC_KIND_FIELD"));
}
