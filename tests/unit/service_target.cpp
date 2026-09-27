#include "core/dependency_graph.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/register_bank.hpp"
#include "nocloom/config.hpp"
#include <catch2/catch_test_macros.hpp>
#include <sstream>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
Definition definition(const std::string &extra_initiators = "",
                      const std::string &service_options = "") {
  const auto text =
      "schema_version: 10\nname: s\naddress_width: 32\nreset_ports: [{name: reset_n}]\n"
      "test_ports: [{name: test}]\nclock_domains: [{name: main, clock_port: clk, reset_port: "
      "reset_n, test_port: test}]\ninitiators:\n"
      "  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}\n"
      "  - {name: data, clock_domain: main, protocol: apb4, data_width: 32}\n" +
      extra_initiators +
      "targets:\n  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, attributes: "
      "amba, mappings: [{base: '0x0', size: '0x1000'}]}\n"
      "  - {name: service, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, " +
      service_options + "mappings: [{base: '0x100000', size: '0x100000'}]}\n" +
      "output: {backend: verilog, language: verilog_2001}\n";
  auto parsed = config::parse(text);
  REQUIRE(parsed);
  return parsed.value().definition;
}
InternalNetwork logger() {
  InternalNetwork internal;
  internal.service_targets["service"] =
      error_logger_bank("logger", 0, ErrorRecordShape{3, 32, 0, 0, 2, 0});
  return internal;
}
std::string rejection(const Result<NetworkPlan> &plan) {
  REQUIRE_FALSE(plan);
  return plan.diagnostics().front().message;
}
} // namespace

TEST_CASE("A service target takes the numbers an APB4 target of its shape takes", "[service]") {
  const auto d = definition();
  const auto plain = build_network_plan(d);
  const auto served = build_network_plan(d, logger());
  REQUIRE(plain);
  REQUIRE(served);
  CHECK(plain.value().route_index.target_ids == served.value().route_index.target_ids);
  // ram, then service by name, each of one flow; the data and configuration sources number apart.
  const auto &ids = served.value().route_index.target_ids;
  CHECK(ids.targets.at("ram") == 0);
  CHECK(ids.targets.at("service") == 1);
  CHECK(served.value().target("service").service.has_value());
  CHECK_FALSE(plain.value().target("service").service.has_value());
}

TEST_CASE("A service target serves one packet from APB4 sources", "[service]") {
  auto cases = logger();
  SECTION("an unknown name") {
    InternalNetwork other;
    other.service_targets["nothing"] = cases.service_targets.at("service");
    CHECK(rejection(build_network_plan(definition(), other)) == "No such target");
  }
  SECTION("an AXI source reaching it") {
    const auto extra = "  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 32, "
                       "id_width: 2, len_width: 8}\n";
    CHECK(rejection(build_network_plan(definition(extra), cases)) ==
          "Only 32-bit APB4 sources reach a window");
  }
  SECTION("an AXI4-Lite source reaching it") {
    const auto extra =
        "  - {name: lite, clock_domain: main, protocol: axi4_lite, data_width: 32}\n";
    CHECK(rejection(build_network_plan(definition(extra), cases)) ==
          "Only 32-bit APB4 sources reach a window");
  }
  SECTION("a target with options") {
    CHECK(rejection(build_network_plan(definition("", "access: read_only, "), cases)) ==
          "A window has one packet, one flow and no options");
  }
  SECTION("two service targets") {
    cases.service_targets["ram"] = cases.service_targets.at("service");
    CHECK(rejection(build_network_plan(definition(), cases)) == "Expected one window of registers");
  }
  SECTION("a bank larger than the window") {
    auto d = definition();
    d.targets.back().mappings = {{0x100000, 0x40}};
    CHECK(rejection(build_network_plan(d, cases)).starts_with("A window holds its banks"));
  }
}

TEST_CASE("APB sources need no direction field in transaction identities", "[service]") {
  auto d = definition();
  d.targets.front().protocol = Protocol::Axi4;
  d.targets.front().len_width = 8;
  d.targets.front().id_width = 4;
  d.targets.front().max_outstanding = 4;
  for (const auto &window :
       {nocloom::AddressMapping{0x10000000, 0x10000000}, nocloom::AddressMapping{0x0, 0x1000}}) {
    d.targets.front().mappings = {window};
    d.targets.back().mappings = {{0x100000, 0x100000}};
    const auto plan = build_network_plan(d, logger());
    REQUIRE(plan);
    CHECK(plan.value().route_index.target_ids.direction_bits == 0);
  }
}

TEST_CASE("A service target is one head with the bank as its environment", "[service]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  auto graph = plan.value().dependency;
  const auto bank = std::ranges::find_if(graph.nodes, [](const DependencyNode &n) {
    return n.kind == "env.bank_done" && n.key == "service";
  });
  REQUIRE(bank != graph.nodes.end());
  const auto index = static_cast<std::uint32_t>(bank - graph.nodes.begin());
  CHECK(std::ranges::none_of(graph.edges, [&](const DependencyEdge &e) { return e.to == index; }));
  CHECK(std::ranges::any_of(graph.edges, [&](const DependencyEdge &e) {
    return e.from == index && graph.nodes[e.to].kind == "target.head";
  }));
  CHECK(std::ranges::none_of(graph.nodes, [](const DependencyNode &n) {
    return (n.kind == "env.target_request" || n.kind == "env.target_response") &&
           n.key == "service";
  }));
  CHECK(plan.value().dependency_report.findings.empty());
  CHECK(plan.value().dependency_report.cycle_sets == 0);
}

TEST_CASE("A network with a service target lowers and passes its composition checks", "[service]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  CHECK(std::ranges::none_of(hardware.value().ir.ports, [](const Port &port) {
    return port.name.starts_with("nl_t_service_");
  }));
}
