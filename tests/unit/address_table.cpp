// The optional address table: every other artifact keeps its bytes, the windows match the
// routing windows of the plan, and the table is deterministic.
#include "../endpoint_ports.hpp"
#include "../graph_example_definition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <iterator>
#include <nlohmann/json.hpp>
#include <nocloom/generation.hpp>
#include <ranges>
#include <string>
#include <utility>
#include <vector>

using namespace nocloom;

namespace {
// One owner flag per source, its value the source's position: an exclusive access names its owner.
void owner_flag(nocloom::Definition &d, nocloom::Target &t) {
  using nocloom::AttributeSource;
  d.transport->user_flags = {"owner"};
  for (std::size_t i = 0; i < d.initiators.size(); ++i)
    d.initiators[i].user_flags["owner"] = {i % 2 ? AttributeSource::One : AttributeSource::Zero};
  t.exclusive_id_flags = {"owner"};
}
} // namespace

namespace {
// s0 reaches every target; s1 is read-only and reaches t0 and the t1 it holds. t0 holds the
// sub-page mapping of t1 and has an alias at a local offset; t2 is read-only; t3 has a network
// exclusive monitor.
Definition definition() {
  Definition d;
  d.schema_version = 10;
  d.name = "ledger";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  for (const auto *name : {"s0", "s1"})
    d.initiators.push_back(graph_source(name));
  d.initiators[1].access = Access::ReadOnly;
  d.targets.push_back(graph_target("t0", {0x0, 0x30000}));
  d.targets[0].mappings.push_back({0x100000, 0x1000, 0x2000});
  d.targets.push_back(graph_target("t1", {0x10800, 0x800}));
  d.targets.push_back(graph_target("t2", {0x40000, 0x10000}));
  d.targets[2].access = Access::ReadOnly;
  d.targets.push_back(graph_target("t3", {0x50000, 0x10000}));
  d.targets[3].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[3]);
  endpoint_ports(d, 2, 8);
  d.connectivity = Connectivity{};
  for (const auto *t : {"t0", "t1", "t2", "t3"})
    d.connectivity->pairs.push_back({"s0", t});
  d.connectivity->pairs.push_back({"s1", "t0"});
  d.connectivity->pairs.push_back({"s1", "t1"});
  return d;
}
const Artifact &named(const ArtifactSet &set, std::string_view suffix) {
  const auto it =
      std::ranges::find_if(set, [&](const Artifact &a) { return a.path.ends_with(suffix); });
  REQUIRE(it != set.end());
  return *it;
}
std::string codes(const Result<ArtifactSet> &r) {
  std::string out;
  for (const auto &d : r.diagnostics())
    out += d.code + " " + d.field + ": " + d.message + "\n";
  return out;
}
ArtifactSet with_table(const Definition &d) {
  auto plain = generate(d);
  INFO(codes(plain));
  REQUIRE(plain);
  auto table = add_address_table(plain.value());
  REQUIRE(table);
  return table.value();
}
} // namespace

TEST_CASE("The address table keeps every other artifact", "[address-table]") {
  const auto d = definition();
  const auto plain = generate(d);
  INFO(codes(plain));
  REQUIRE(plain);
  const auto table = with_table(d);
  REQUIRE(table.size() == plain.value().size() + 2);
  for (const auto &a : plain.value())
    if (!a.path.ends_with(".bundle.json"))
      CHECK(std::ranges::find(table, a) != table.end());
  // The bundle only gains the two table files.
  auto before = nlohmann::json::parse(named(plain.value(), ".bundle.json").text);
  auto after = nlohmann::json::parse(named(table, ".bundle.json").text);
  const nlohmann::json added = {
      {{"path", "nocloom_ledger.address_table.json"}, {"role", "address_table"}},
      {{"path", "nocloom_ledger.address_table.csv"}, {"role", "address_table"}}};
  CHECK(nlohmann::json(std::vector(after["files"].end() - 2, after["files"].end())) == added);
  after["files"].erase(after["files"].end() - 2, after["files"].end());
  CHECK(after == before);
  CHECK(std::ranges::is_sorted(table, {}, &Artifact::path));
}

TEST_CASE("The address table lists each initiator's routing windows", "[address-table]") {
  const auto table = with_table(definition());
  CHECK(named(table, ".address_table.csv").text ==
        "# SPDX-License-Identifier: LicenseRef-NocLoom-Output-1.0\n"
        "# NocLoom supplied content only. See NocLoom LICENSES/LicenseRef-NocLoom-Output-1.0.txt\n"
        "initiator,base,size,target,local_base,access,exclusive,default_target\n"
        "s0,0x00000000,0x00010800,t0,0x00000000,read_write,none,false\n"
        "s0,0x00010800,0x00000800,t1,0x00000000,read_write,none,true\n"
        "s0,0x00011000,0x0001f000,t0,0x00011000,read_write,none,false\n"
        "s0,0x00040000,0x00010000,t2,0x00000000,read_only,none,false\n"
        "s0,0x00050000,0x00010000,t3,0x00000000,read_write,network,false\n"
        "s0,0x00100000,0x00001000,t0,0x00002000,read_write,none,false\n"
        "s1,0x00000000,0x00010800,t0,0x00000000,read_only,none,false\n"
        "s1,0x00010800,0x00000800,t1,0x00000000,read_only,none,true\n"
        "s1,0x00011000,0x0001f000,t0,0x00011000,read_only,none,false\n"
        "s1,0x00100000,0x00001000,t0,0x00002000,read_only,none,false\n");
  const auto json = nlohmann::json::parse(named(table, ".address_table.json").text);
  CHECK(json["module"] == "nocloom_ledger");
  CHECK(json["generator"] == "NocLoom");
  CHECK_FALSE(json.contains("version"));
  CHECK(json["address_width"] == 32);
  CHECK(json["initiators"][0]["default_target"] == "t1");
  CHECK(json["initiators"][1]["windows"].size() == 4);
  CHECK(json["initiators"][0]["windows"][4] == nlohmann::json{{"base", "0x00050000"},
                                                              {"size", "0x00010000"},
                                                              {"target", "t3"},
                                                              {"local_base", "0x00000000"},
                                                              {"access", "read_write"},
                                                              {"exclusive", "network"},
                                                              {"default_target", false}});
}

TEST_CASE("Address table windows are the planned routing windows", "[address-table]") {
  auto d = definition();
  d.initiators[1].default_target = "t0";
  d.connectivity->pairs.push_back({"s1", "t3"});
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  const auto json =
      nlohmann::json::parse(named(with_table(d), ".address_table.json").text)["initiators"];
  for (unsigned s = 0; s < 2; ++s) {
    // The plan's windows of each reached target, contiguous ones of a mapping merged.
    std::vector<std::pair<std::uint64_t, std::string>> expected;
    for (const auto &t : plan.value().targets) {
      if (!detail::reaches(plan.value(), json[s]["name"], t.name))
        continue;
      for (const auto &m : t.mappings) {
        std::uint64_t end = 0;
        for (const auto &r : m.routes) {
          if (!expected.empty() && r.base == end && expected.back().second == t.name) {
            end += r.size;
            continue;
          }
          expected.emplace_back(r.base, t.name);
          end = r.base + r.size;
        }
      }
    }
    std::ranges::sort(expected);
    std::vector<std::pair<std::uint64_t, std::string>> listed;
    std::ranges::transform(json[s]["windows"], std::back_inserter(listed), [](const auto &w) {
      return std::pair{std::stoull(w["base"].template get<std::string>(), nullptr, 16),
                       w["target"].template get<std::string>()};
    });
    CHECK(listed == expected);
    const auto fallback = detail::default_target(plan.value(), json[s]["name"]);
    REQUIRE(fallback);
    CHECK(json[s]["default_target"] == plan.value().targets[*fallback].name);
  }
}

TEST_CASE("The address table is deterministic", "[address-table]") {
  auto d = definition();
  const auto first = with_table(d);
  CHECK(with_table(d) == first);
  std::ranges::reverse(d.initiators);
  std::ranges::reverse(d.targets);
  std::ranges::reverse(d.connectivity->pairs);
  CHECK(with_table(d) == first);
}

TEST_CASE("Address tables report full AXI exclusive modes", "[address-table]") {
  for (const auto protocol : {Protocol::Axi4, Protocol::Axi3})
    for (const auto mode : {ExclusiveAccess::Network, ExclusiveAccess::Target}) {
      auto d = definition();
      d.initiators = {graph_source("s0")};
      d.initiators[0].protocol = protocol;
      d.initiators[0].len_width = protocol == Protocol::Axi3 ? 4 : 8;
      d.targets = {graph_target("t0", {0, 0x10000})};
      d.targets[0].exclusive = mode;
      owner_flag(d, d.targets[0]);
      d.connectivity = Connectivity{.all = true};
      const auto table = with_table(d);
      const auto expected = mode == ExclusiveAccess::Network ? "network" : "target";
      INFO(static_cast<int>(protocol));
      INFO(expected);
      const auto json = nlohmann::json::parse(named(table, ".address_table.json").text);
      CHECK(json["initiators"][0]["windows"][0]["exclusive"] == expected);
      CHECK(named(table, ".address_table.csv")
                .text.find(std::string(",read_write,") + expected + ",true\n") !=
            std::string::npos);
    }
}

TEST_CASE("Address table defaults use reachable aligned blocks", "[address-table]") {
  for (const bool narrow : {false, true}) {
    auto d = definition();
    d.initiators = {graph_source("s0"), graph_source("s1")};
    d.targets = {graph_target("t0", {0, narrow ? 0x800U : 0x3000U}),
                 graph_target("t1", {narrow ? 0x800U : 0x4000U, narrow ? 0x800U : 0x2000U})};
    d.transport->user_flags.clear();
    d.connectivity = Connectivity{.all = true};
    for (auto &source : d.initiators)
      set_protocol(source, Protocol::Apb4);
    for (auto &target : d.targets)
      set_protocol(target, Protocol::Apb4);
    if (narrow) {
      d.initiators[0].address_width = 12;
      d.targets[1].mappings.push_back({0x10000, 4});
    }
    const auto plan = detail::build_network_plan(d);
    REQUIRE(plan);
    const auto table = with_table(d);
    const auto json = nlohmann::json::parse(named(table, ".address_table.json").text);
    INFO(narrow);
    for (unsigned s = 0; s < 2; ++s) {
      const auto expected = narrow && s == 1 ? "t1" : "t0";
      CHECK(json["initiators"][s]["default_target"] == expected);
      const auto fallback = detail::default_target(plan.value(), "s" + std::to_string(s));
      REQUIRE(fallback);
      CHECK(plan.value().targets[*fallback].name == expected);
      for (const auto &window : json["initiators"][s]["windows"])
        CHECK(window["default_target"] == (window["target"] == expected));
    }
    const auto &csv = named(table, ".address_table.csv").text;
    CHECK(csv.find(std::string("s0,0x00000000,") + (narrow ? "0x00000800" : "0x00003000") +
                   ",t0,0x00000000,read_write,none,true\n") != std::string::npos);
  }
}

TEST_CASE("A narrower source lists only the windows it can address", "[address-table]") {
  auto d = definition();
  d.address_width = 40;
  d.initiators[0].address_width = 32;
  // t3 crosses s0's address limit: only the part below it is listed. The wider s1 reaches the rest.
  d.targets[3].mappings = {{0xffff0000, 0x20000, 0}};
  d.targets[3].address_width = 17;
  d.connectivity->pairs.push_back({"s1", "t3"});
  const auto json = nlohmann::json::parse(named(with_table(d), ".address_table.json").text);
  const auto &windows = json["initiators"][0]["windows"];
  REQUIRE(windows.size() == 6);
  CHECK(windows[5]["target"] == "t3");
  CHECK(windows[5]["base"] == "0x00ffff0000");
  CHECK(windows[5]["size"] == "0x0000010000");
  CHECK(windows[0]["base"] == "0x0000000000");
}

TEST_CASE("The address table needs one map and bundle", "[address-table]") {
  const auto rejected = [](const Result<ArtifactSet> &r) {
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_ADDRESS_TABLE_INPUT");
  };
  rejected(add_address_table({}));
  const auto table = with_table(definition());
  rejected(add_address_table(table));
  auto broken = table;
  std::erase_if(
      broken, [](const Artifact &a) { return a.path.find("address_table") != std::string::npos; });
  for (auto &a : broken)
    if (a.path.ends_with(".map.json"))
      a.text = "{}";
  rejected(add_address_table(broken));
}

TEST_CASE("Address table access follows both write-only and read-only sides", "[address-table]") {
  // t1 and t2 become write-only: s0 only writes them; the read-only s1 has no access to t1.
  auto d = definition();
  d.targets[1].access = Access::WriteOnly;
  d.targets[2].access = Access::WriteOnly;
  const auto csv = named(with_table(d), ".address_table.csv").text;
  CHECK(csv.find("s0,0x00010800,0x00000800,t1,0x00000000,write_only,none,true\n") !=
        std::string::npos);
  CHECK(csv.find("s0,0x00040000,0x00010000,t2,0x00000000,write_only,none,false\n") !=
        std::string::npos);
  CHECK(csv.find("s1,0x00010800,0x00000800,t1,0x00000000,none,none,true\n") != std::string::npos);
  CHECK(csv.find("s1,0x00000000,0x00010800,t0,0x00000000,read_only,none,false\n") !=
        std::string::npos);
}
