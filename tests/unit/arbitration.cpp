#include "core/axi_burst_charge.hpp"
#include "core/exchange_composition.hpp"
#include "core/ir_cone.hpp"
#include "core/network_plan.hpp"
#include "core/qos_budget.hpp"
#include "core/treadle.hpp"
#include "core/upgrade_ledger.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <cmath>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <set>
#include <string>
#include <tuple>
#include <vector>

// Switch output arbitration: the common default, the global policy,
// per-output overrides, their diagnostics and the map contract.
namespace {
using nocloom::ArbitrationPolicy;
// n AXI4 sources on one request node "hub" in front of the targets (outputs
// of n candidates); the response node "back" gives every source one candidate
// per target.
nocloom::Definition hub(unsigned n, unsigned targets = 2) {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "hub";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  for (unsigned i = 0; i < n; ++i) {
    d.initiators.push_back({"s" + std::to_string(i), "main"});
    d.initiators.back().protocol = Protocol::Axi4;
    d.initiators.back().data_width = 32;
  }
  d.targets = {{"t0", "main", {{0, 0x1000}}}, {"t1", "main", {{0x1000, 0x1000}}}};
  d.targets.resize(targets);
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.attributes = TargetAttributes::amba();
    t.data_width = 32;
  }
  ExchangeTopology topology;
  topology.request.nodes = {{"hub"}};
  topology.response.nodes = {{"back"}};
  for (const auto &i : d.initiators) {
    topology.request.entries.push_back({i.name, "hub"});
    topology.response.exits.push_back({i.name, "back"});
  }
  for (const auto &t : d.targets) {
    topology.request.exits.push_back({t.name, "hub"});
    topology.response.entries.push_back({t.name, "back"});
  }
  d.topology = topology;
  return d;
}
nlohmann::json contract(const nocloom::ArtifactSet &artifacts) {
  const auto map =
      std::ranges::find_if(artifacts, [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != artifacts.end());
  return nlohmann::json::parse(map->text)["definition"]["contract"]["arbitration"];
}
nlohmann::json output(const nlohmann::json &c, const std::string &graph, const std::string &out) {
  const auto &outputs = c["outputs"];
  const auto it = std::find_if(outputs.begin(), outputs.end(), [&](const auto &o) {
    return o["graph"] == graph && o["output"] == out;
  });
  if (it == outputs.end())
    FAIL("no arbitrated output " << graph << " " << out);
  return *it;
}
template <typename T>
void rejected(const nocloom::Result<T> &r, const char *code, const std::string &field) {
  REQUIRE_FALSE(r);
  INFO("expected " << code << " at " << field);
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field << " " << d.message);
  REQUIRE(std::ranges::any_of(r.diagnostics(),
                              [&](const auto &d) { return d.code == code && d.field == field; }));
}
const char *yaml_hub = R"(schema_version: 10
connectivity: all
name: hub
address_width: 32
ARBITRATION
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32}
  - {name: s1, clock_domain: main, protocol: axi4, data_width: 32}
  - {name: s2, clock_domain: main, protocol: axi4, data_width: 32}
targets:
  - {name: t0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], attributes: amba}
  - {name: t1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
topology:
  request:
    nodes: [{name: hub, arbitration: [{output: endpoint/t0, policy: fixed_priority, priority: [endpoint/s2, endpoint/s0, endpoint/s1]}]}]
    links: []
    entries: [{endpoint: s0, node: hub}, {endpoint: s1, node: hub}, {endpoint: s2, node: hub}]
    exits: [{endpoint: t0, node: hub}, {endpoint: t1, node: hub}]
  response:
    nodes: [{name: back}]
    links: []
    entries: [{endpoint: t0, node: back}, {endpoint: t1, node: back}]
    exits: [{endpoint: s0, node: back}, {endpoint: s1, node: back}, {endpoint: s2, node: back}]
output: {backend: verilog, language: verilog_2001}
)";
std::string with(const std::string &arbitration) {
  std::string text = yaml_hub;
  text.replace(text.find("ARBITRATION"), 11, arbitration);
  return text;
}
} // namespace

TEST_CASE("Omitted arbitration uses round robin", "[arbitration-config]") {
  auto d = hub(3);
  const auto omitted = nocloom::generate(d);
  REQUIRE(omitted);
  CHECK(d.arbitration == ArbitrationPolicy::RoundRobin);
  const auto implicit = nocloom::config::parse(with(""));
  const auto explicit_policy = nocloom::config::parse(with("arbitration: {policy: round_robin}"));
  REQUIRE(implicit);
  REQUIRE(explicit_policy);
  CHECK(implicit.value().definition == explicit_policy.value().definition);
  const auto c = contract(omitted.value());
  REQUIRE(c["default"] == "round_robin");
  // Every output has an arbiter: t0 and t1 of the request hub (three
  // sources each) and s0 to s2 of the response node (two targets each).
  REQUIRE(c["outputs"].size() == 5);
  const auto s1 = output(c, "response", "endpoint/s1");
  REQUIRE(s1["policy"] == "round_robin");
  REQUIRE(s1["candidates"] == nlohmann::json({"endpoint/t0", "endpoint/t1"}));
  REQUIRE(s1["service"] == "bounded_wait_at_most_1_releases");
  // With one target every response output has one candidate and no arbiter.
  const auto single = nocloom::generate(hub(3, 1));
  REQUIRE(single);
  REQUIRE(contract(single.value())["outputs"].size() == 1);
  const auto t0 = output(c, "request", "endpoint/t0");
  REQUIRE(t0["policy"] == "round_robin");
  REQUIRE(t0["candidates"] == nlohmann::json({"endpoint/s0", "endpoint/s1", "endpoint/s2"}));
  REQUIRE(t0["service"] == "bounded_wait_at_most_2_releases");
  REQUIRE(t0["set_by"] == "default");
  REQUIRE(c["service"] == "every_output_bounded_wait");
}

TEST_CASE("Candidates are numbered by the names they come from", "[arbitration-config]") {
  // Request: s0 and s1 reach hub through a_up and z_up, s2 is attached to hub. Response: t0
  // reaches back through a_dn, t1 is attached to back.
  auto d = hub(3);
  auto &q = d.topology->request, &r = d.topology->response;
  q.nodes = {{"hub"}, {"z_up"}, {"a_up"}};
  q.links = {{"z_up", "hub"}, {"a_up", "hub"}};
  q.entries = {{"s0", "a_up"}, {"s1", "z_up"}, {"s2", "hub"}};
  r.nodes = {{"back"}, {"a_dn"}};
  r.links = {{"a_dn", "back"}};
  r.entries = {{"t0", "a_dn"}, {"t1", "back"}};
  const auto generated = nocloom::generate(d);
  REQUIRE(generated);
  const auto c = contract(generated.value());
  REQUIRE(output(c, "request", "endpoint/t0")["candidates"] ==
          nlohmann::json({"node/a_up", "endpoint/s2", "node/z_up"}));
  REQUIRE(output(c, "response", "endpoint/s1")["candidates"] ==
          nlohmann::json({"node/a_dn", "endpoint/t1"}));
}

TEST_CASE("Every candidate count uses the same default", "[arbitration-config]") {
  for (unsigned n : {2U, 8U, 9U, 16U, 17U, 32U}) {
    const auto artifacts = nocloom::generate(hub(n));
    REQUIRE(artifacts);
    REQUIRE(output(contract(artifacts.value()), "request", "endpoint/t0")["policy"] ==
            "round_robin");
  }
}

TEST_CASE("A global policy applies to every output with one owner above sixteen",
          "[arbitration-config]") {
  for (const auto &[policy, name] :
       {std::pair{ArbitrationPolicy::RoundRobin, "round_robin"},
        std::pair{ArbitrationPolicy::Fifo, "fifo"},
        std::pair{ArbitrationPolicy::RotatingPriority, "rotating_priority"},
        std::pair{ArbitrationPolicy::LevelRoundRobin, "level_round_robin"}}) {
    auto d = hub(3);
    d.arbitration = policy;
    const auto artifacts = nocloom::generate(d);
    REQUIRE(artifacts);
    const auto c = contract(artifacts.value());
    REQUIRE(c["default"] == name);
    REQUIRE_FALSE(c.contains("by_candidates"));
    for (const auto &o : c["outputs"])
      REQUIRE(o["policy"] == name);
  }
  // Explicit policies retain one global state above sixteen candidates.
  for (const auto &[n, policy, bound] :
       {std::tuple{16U, ArbitrationPolicy::Fifo, 15U},
        std::tuple{17U, ArbitrationPolicy::Fifo, 16U},
        std::tuple{32U, ArbitrationPolicy::Fifo, 31U},
        std::tuple{17U, ArbitrationPolicy::RotatingPriority, 32U}}) {
    auto d = hub(n, 1);
    d.arbitration = policy;
    const auto artifacts = nocloom::generate(d);
    REQUIRE(artifacts);
    REQUIRE(output(contract(artifacts.value()), "request", "endpoint/t0")["service"] ==
            "bounded_wait_at_most_" + std::to_string(bound) + "_releases");
  }
}

TEST_CASE("Large outputs keep one global owner and cursor", "[arbitration-config]") {
  using namespace nocloom::detail;
  for (unsigned n : {2U, 16U, 17U, 32U, 256U}) {
    Builder b;
    const auto clock = b.input("c", 1, "d", "c"), reset = b.input("r", 1, "d", "r");
    std::vector<treadle::Candidate> many(n, {clock, reset});
    treadle::Datapath none{[] {}, [](unsigned, ValueId) {},
                           [&](ValueId v) { return std::pair{v, v}; }};
    const auto arbiters = treadle::lower_output(b, clock, reset, {}, many, none);
    REQUIRE(arbiters.size() == 1);
    REQUIRE(arbiters[0].registers.size() == 3);
    REQUIRE(treadle::service(treadle::Policy::RoundRobin, n).bound == n - 1);
  }
}

TEST_CASE("Per-output fixed priority follows the written order in YAML and C++",
          "[arbitration-config]") {
  const auto parsed = nocloom::config::parse(with(""), "hub.yaml");
  REQUIRE(parsed);
  auto d = hub(3);
  d.topology->request.nodes[0].arbitration = {{"endpoint/t0",
                                               ArbitrationPolicy::FixedPriority,
                                               {"endpoint/s2", "endpoint/s0", "endpoint/s1"}}};
  REQUIRE(parsed.value().definition.topology == d.topology);
  const auto from_yaml = nocloom::generate(parsed.value().definition);
  const auto from_api = nocloom::generate(d);
  REQUIRE(from_yaml);
  REQUIRE(from_api);
  REQUIRE(from_yaml.value() == from_api.value());
  const auto c = contract(from_api.value());
  const auto t0 = output(c, "request", "endpoint/t0");
  REQUIRE(t0["policy"] == "fixed_priority");
  REQUIRE(t0["set_by"] == "output");
  REQUIRE(t0["priority"] == nlohmann::json({"endpoint/s2", "endpoint/s0", "endpoint/s1"}));
  REQUIRE(t0["service"] == "lower_priority_served_when_higher_priority_candidates_yield");
  REQUIRE(output(c, "request", "endpoint/t1")["policy"] == "round_robin");
  REQUIRE(c["service"] ==
          "fixed_priority_outputs_assume_higher_priority_candidates_eventually_yield");

  rejected(nocloom::config::parse(with("arbitration: {by_candidates: {2: fifo}}"), "hub.yaml"),
           "NCL_SCHEMA_UNKNOWN_FIELD", "arbitration.by_candidates");
  const auto global = nocloom::config::parse(with("arbitration: {policy: fifo}"), "hub.yaml");
  REQUIRE(global);
  REQUIRE(global.value().definition.arbitration == ArbitrationPolicy::Fifo);
  rejected(nocloom::config::parse(with("arbitration: {policy: not_a_policy}"), "hub.yaml"),
           "NCL_UNSUPPORTED_ARBITRATION", "arbitration.policy");
}

TEST_CASE("Arbitration settings that cannot be built are rejected", "[arbitration-config]") {
  using P = ArbitrationPolicy;
  const std::string node = "topology.request.nodes[0].arbitration[0]";
  auto override_with = [](nocloom::OutputArbitration o) {
    auto d = hub(3);
    d.topology->request.nodes[0].arbitration = {std::move(o)};
    return d;
  };
  rejected(nocloom::make_plan(override_with({"endpoint/t0", P::FixedPriority, {}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority");
  rejected(nocloom::make_plan(
               override_with({"endpoint/t0", P::FixedPriority, {"endpoint/s0", "endpoint/s1"}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority");
  rejected(nocloom::make_plan(override_with(
               {"endpoint/t0", P::FixedPriority, {"endpoint/s0", "endpoint/s1", "endpoint/t1"}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority");
  rejected(nocloom::make_plan(override_with(
               {"endpoint/t0", P::FixedPriority, {"endpoint/s0", "endpoint/s0", "endpoint/s1"}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority");
  rejected(nocloom::make_plan(override_with({"endpoint/t0", P::Fifo, {"endpoint/s0"}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority");
  rejected(nocloom::make_plan(override_with(
               {"endpoint/t0", P::FixedPriority, {"node/nowhere", "endpoint/s0", "endpoint/s1"}})),
           "NCL_ARBITRATION_PRIORITY", node + ".priority[0]");
  rejected(nocloom::make_plan(override_with({"endpoint/t0", static_cast<P>(-1), {}})),
           "NCL_UNSUPPORTED_ARBITRATION", node + ".policy");
  {
    // Fixed priority serves any number of candidates, flat.
    auto ranked = [](unsigned n) {
      auto d = hub(n);
      std::vector<std::string> order;
      for (unsigned i = n; i-- > 0;)
        order.push_back("endpoint/s" + std::to_string(i));
      d.topology->request.nodes[0].arbitration = {{"endpoint/t0", P::FixedPriority, order}};
      return d;
    };
    for (const unsigned n : {4U, 5U, 17U, 32U})
      REQUIRE(nocloom::make_plan(ranked(n)));
  }
  rejected(nocloom::make_plan(override_with({"endpoint/t9", P::Fifo, {}})),
           "NCL_ARBITRATION_OUTPUT", node + ".output");
  rejected(nocloom::make_plan(override_with({"node/nowhere", P::Fifo, {}})),
           "NCL_ARBITRATION_OUTPUT", node + ".output");
  rejected(nocloom::make_plan(override_with({"t0", P::Fifo, {}})), "NCL_ARBITRATION_OUTPUT",
           node + ".output");
  {
    auto d = override_with({"endpoint/t0", P::Fifo, {}});
    d.topology->request.nodes[0].arbitration.push_back({"endpoint/t0", P::RoundRobin, {}});
    rejected(nocloom::make_plan(d), "NCL_ARBITRATION_OUTPUT",
             "topology.request.nodes[0].arbitration[1].output");
  }
  {
    // A single-candidate output has no arbiter to configure.
    auto d = hub(3, 1);
    d.topology->response.nodes[0].arbitration = {{"endpoint/s0", P::Fifo, {}}};
    rejected(nocloom::make_plan(d), "NCL_ARBITRATION_OUTPUT",
             "topology.response.nodes[0].arbitration[0].output");
  }
  auto d = hub(3);
  d.arbitration = P::FixedPriority;
  rejected(nocloom::make_plan(d), "NCL_ARBITRATION_PRIORITY", "arbitration.policy");
  d.arbitration = static_cast<P>(99);
  rejected(nocloom::make_plan(d), "NCL_UNSUPPORTED_ARBITRATION", "arbitration.policy");
  d.arbitration = P::Fifo;
}

TEST_CASE("Fixed network levels preserve the default and bound their profile", "[fixed-qos]") {
  using namespace nocloom;
  auto d = hub(2);
  const auto original = generate(d);
  REQUIRE(original);
  d.initiators[0].qos.levels = {0, 0};
  const auto zero = generate(d);
  REQUIRE(zero);
  REQUIRE(original.value().size() == zero.value().size());
  for (std::size_t i = 0; i < original.value().size(); ++i) {
    CHECK(original.value()[i].path == zero.value()[i].path);
    CHECK(original.value()[i].text == zero.value()[i].text);
  }
  d.initiators[0].qos.levels = {7, 1};
  d.initiators[1].qos.levels = {1, 7};
  const auto active = generate(d);
  REQUIRE(active);
  const auto c = contract(active.value());
  CHECK(c["qos"]["sources"]["s0"]["levels"]["read"] == 7);
  CHECK(c["qos"]["fifo"] == "arrival_order_records_unfiltered_requests");
  CHECK(c["service"] == "lower_levels_require_higher_levels_to_yield_no_global_wait_bound");
  auto reject_profile = [&](const Definition &bad, const std::string &code = "NCL_QOS_PROFILE") {
    const auto result = generate(bad);
    REQUIRE_FALSE(result);
    for (const auto &diagnostic : result.diagnostics())
      UNSCOPED_INFO(diagnostic.code << " " << diagnostic.field << " " << diagnostic.message);
    CHECK(std::ranges::any_of(result.diagnostics(),
                              [&](const auto &diag) { return diag.code == code; }));
  };
  auto bad = d;
  bad.initiators[0].qos.levels.write = 8;
  reject_profile(bad, "NCL_QOS_LEVEL");
  bad = d;
  bad.initiators[0].protocol = Protocol::Axi4Lite;
  reject_profile(bad);
  bad = d;
  bad.targets[0].protocol = Protocol::Apb4;
  reject_profile(bad);
  bad = d;
  bad.initiators[0].max_outstanding = 2;
  REQUIRE(generate(bad));
  bad = d;
  bad.targets[0].max_outstanding = 2;
  REQUIRE(generate(bad));
  bad = d;
  bad.transport.emplace();
  REQUIRE(generate(bad));
  for (auto &source : bad.initiators)
    source.max_outstanding = 4;
  for (auto &target : bad.targets)
    target.max_outstanding = 4;
  REQUIRE(generate(bad));
  // A wide source's link serializer passes the pressure on; a target wider than the transport
  // merges narrow bursts and stays out of the profile.
  bad = d;
  bad.initiators[0].data_width = 64;
  bad.transport.emplace();
  REQUIRE(generate(bad));
  bad = d;
  bad.targets[0].data_width = 64;
  reject_profile(bad);
  // Levels cross clock placement: the pressure passes the crossings (message.hpp point 15).
  bad = d;
  for (auto *graph : {&bad.topology->request, &bad.topology->response})
    for (auto &node : graph->nodes)
      node.clock_domain = "main";
  REQUIRE(generate(bad));
  for (auto &i : d.initiators)
    i.data_width = 64;
  for (auto &t : d.targets)
    t.data_width = 64;
  REQUIRE(generate(d));
}

TEST_CASE("Fixed levels compose with registered links and bursts in flight", "[fixed-qos]") {
  using namespace nocloom;
  // Two sources join on a, cross the link a>b to the target and return over b>a.
  auto d = hub(2, 1);
  d.name = "qos_links";
  d.topology->request = {{{"a"}, {"b"}},
                         {{"a", "b", std::nullopt, {PipeSlice::Full}}},
                         {{"s0", "a"}, {"s1", "a"}},
                         {{"t0", "b"}}};
  d.topology->response = {{{"a"}, {"b"}}, {{"b", "a"}}, {{"t0", "b"}}, {{"s0", "a"}, {"s1", "a"}}};
  d.initiators[0].qos.levels = {7, 1};
  d.initiators[1].qos.levels = {1, 7};
  const auto generated = generate(d);
  for (const auto &diagnostic : generated.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(generated);
  d.transport = TransportOptions{.max_outstanding = 4};
  d.targets[0].max_outstanding = 4;
  REQUIRE(generate(d));
  d.initiators[0].qos.levels.read = 8;
  const auto level = generate(d);
  REQUIRE_FALSE(level);
  CHECK(level.diagnostics().front().code == "NCL_QOS_LEVEL");
}

TEST_CASE("The pressure of levels crosses pipes and clock domains", "[fixed-qos]") {
  using namespace nocloom;
  const auto network = [](const ArtifactSet &artifacts) {
    const auto map = std::ranges::find_if(
        artifacts, [](const auto &a) { return a.path.ends_with(".map.json"); });
    return nlohmann::json::parse(map->text)["network"];
  };
  // Two sources join on a in clock main; the link a>b crosses into clock fast to t0, t1 stays on
  // a, and the responses of both meet again on a (each source's output arbitrates them).
  auto d = hub(2, 2);
  d.name = "qos_level_hint";
  d.clock_domains = {{"main", "clk_main", "reset_n", "test", 10000},
                     {"fast", "clk_fast", "reset_n", "test", 4000}};
  d.topology->request = {{{"a", std::nullopt, "main"}, {"b", std::nullopt, "fast"}},
                         {{"a", "b", std::nullopt, {PipeSlice::Full}}},
                         {{"s0", "a"}, {"s1", "a"}},
                         {{"t0", "b"}, {"t1", "a"}}};
  d.topology->response = {{{"a", std::nullopt, "main"}, {"b", std::nullopt, "fast"}},
                          {{"b", "a"}},
                          {{"t0", "b"}, {"t1", "a"}},
                          {{"s0", "a"}, {"s1", "a"}}};
  d.targets[0].clock_domain = "fast";
  d.targets[0].max_outstanding = 2;
  const auto plain = generate(d);
  REQUIRE(plain);
  d.initiators[0].qos.levels = {3, 1};
  d.initiators[1].qos.shaper = QosShaper{260, 64};
  d.initiators[1].qos.scheduler = QosScheduler{3, 0, 50};
  d.initiators[1].max_outstanding = 1;
  const auto pressed = generate(d);
  REQUIRE(pressed);
  const auto c = contract(pressed.value());
  CHECK(c["qos"]["pressure"]["crossing"] ==
        "synchronized_pressure_and_head_levels_of_written_slots");
  CHECK_FALSE(contract(plain.value()).contains("qos"));
  // Each crossing synchronizes its two-bit pressure too (two stages by default).
  const auto stages = [&](const ArtifactSet &a) {
    return network(a)["cdc"]["synchronizer_stages"].size();
  };
  CHECK(stages(pressed.value()) > stages(plain.value()));
  // An AXI source with a scheduler compensates its levels through its own packets pipe for any
  // burst count: when its pipes can hold as many requests as its count's bound, a gate before them
  // keeps the rest at the merger.
  for (const auto &[outstanding, gated] : std::vector<std::tuple<unsigned, bool>>{{1, false},
                                                                                  {2, false},
                                                                                  {3, true},
                                                                                  {4, true},
                                                                                  {5, true},
                                                                                  {6, false},
                                                                                  {7, false},
                                                                                  {8, true}}) {
    d.initiators[1].max_outstanding = outstanding;
    d.initiators[1].pipes.packets = {gated ? PipeSlice::Full : PipeSlice::Forward};
    REQUIRE(generate(d));
    // Requests past the merger stop at the bound once the pipe can hold that many; a slot for each.
    const auto plan = nocloom::detail::build_network_plan(d);
    REQUIRE(plan);
    const auto &s1 = plan.value().sources[1];
    const unsigned behind = outstanding + 1U + unsigned(gated);
    const unsigned bound = (1U << std::bit_width(outstanding)) - 1;
    CHECK(s1.request_limit == (behind >= bound ? bound : 0U));
    CHECK(s1.engine_slots == (outstanding > 1 ? std::min(behind, bound) : 1U));
  }
  for (const auto mode : {LinkPipeline{}, LinkPipeline{PipeSlice::Forward},
                          LinkPipeline{PipeSlice::Backward}, LinkPipeline{PipeSlice::Full}}) {
    d.targets[0].pipes.arrival = mode;
    REQUIRE(generate(d));
  }
}

TEST_CASE("Shapers bind 32-bit APB sources and keep YAML and C++ equal", "[fixed-qos]") {
  using namespace nocloom;
  auto d = hub(2);
  for (auto &i : d.initiators)
    i.protocol = Protocol::Apb4;
  for (auto &t : d.targets)
    t.protocol = Protocol::Apb4;
  const auto plain = generate(d);
  REQUIRE(plain);
  d.initiators[0].qos.levels.read = 3;
  d.initiators[0].qos.shaper = QosShaper{260, 64};
  d.initiators[1].qos.levels = {2, 2};
  const auto shaped = generate(d);
  REQUIRE(shaped);
  const auto c = contract(shaped.value());
  CHECK(c["qos"]["sources"]["s0"]["shaper"]["bytes_per_1024_cycles"] == 260);
  CHECK(c["qos"]["sources"]["s0"]["shaper"]["burst_bytes"] == 64);
  CHECK(c["qos"]["sources"]["s1"]["levels"]["read"] == 2);
  CHECK_FALSE(c["qos"]["sources"]["s1"].contains("shaper"));
  const auto map = std::ranges::find_if(
      shaped.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  const auto network = nlohmann::json::parse(map->text)["network"];
  const auto store = std::ranges::find_if(
      network["storage"], [](const auto &s) { return s["resource"] == "source/s0/shaper"; });
  REQUIRE(store != network["storage"].end());
  CHECK((*store)["control"].size() == 3);
  CHECK(std::ranges::count_if(network["resources"],
                              [](const auto &r) { return r["role"] == "shaper"; }) == 1);
  // A shaper alone needs no levels and keeps the plain service contract.
  auto alone = d;
  alone.initiators[0].qos = {};
  alone.initiators[0].qos.shaper = QosShaper{0, 0};
  alone.initiators[1].qos = {};
  const auto bare = generate(alone);
  REQUIRE(bare);
  CHECK(contract(bare.value())["service"] == contract(plain.value())["service"]);
  alone.initiators[0].qos.shaper = QosShaper{65535, 65535};
  REQUIRE(generate(alone));
  // The YAML form parses to the same definition.
  const std::string yaml = R"(schema_version: 10
name: hub
address_width: 32
connectivity: all
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: s0, clock_domain: main, protocol: apb4, data_width: 32,
     qos: {levels: {read: 3}, shaper: {bytes_per_1024_cycles: 260, burst_bytes: 64}}}
  - {name: s1, clock_domain: main, protocol: apb4, data_width: 32,
     qos: {levels: {read: 2, write: 2}}}
targets:
  - {name: t0, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
  - {name: t1, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x1000', size: '0x1000'}]}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = config::parse(yaml, "hub.yaml");
  REQUIRE(parsed);
  CHECK(parsed.value().definition.initiators[0].qos == d.initiators[0].qos);
  CHECK(parsed.value().definition.initiators[1].qos == d.initiators[1].qos);
  for (const auto &[from, to, code, field] :
       std::vector<std::tuple<std::string, std::string, const char *, const char *>>{
           {"burst_bytes: 64", "burst_bytes: 64, depth: 1", "NCL_SCHEMA_UNKNOWN_FIELD",
            "initiators[0].qos.shaper.depth"},
           {"bytes_per_1024_cycles: 260, ", "", "NCL_SCHEMA_MISSING_FIELD",
            "initiators[0].qos.shaper.bytes_per_1024_cycles"},
           {"bytes_per_1024_cycles: 260", "bytes_per_1024_cycles: 65536", "NCL_QOS_SHAPER",
            "initiators[0].qos.shaper"},
           {"burst_bytes: 64", "burst_bytes: 65536", "NCL_QOS_SHAPER", "initiators[0].qos.shaper"},
           {"burst_bytes: 64", "burst_bytes: 64, bytes_per_32768_cycles: 0", "NCL_QOS_SHAPER",
            "initiators[0].qos.shaper"},
           {"bytes_per_1024_cycles: 260", "bytes_per_32768_cycles: 2097152", "NCL_QOS_SHAPER",
            "initiators[0].qos.shaper"},
           {"apb4, data_width: 32,\n     qos: {levels: {read: 3}",
            "axi4_lite, data_width: 32,\n     qos: {levels: {read: 3}", "NCL_QOS_PROFILE",
            "initiators[0].qos.shaper"},
           {"apb4, data_width: 32,\n     qos: {levels: {read: 3}",
            "apb3, data_width: 16,\n     qos: {levels: {read: 3}", "NCL_QOS_PROFILE",
            "initiators[0].qos.shaper"}}) {
    auto text = yaml;
    text.replace(text.find(from), from.size(), to);
    INFO(text);
    const auto result = config::parse(text, "hub.yaml");
    if (!result)
      rejected(result, code, field);
    else
      rejected(generate(result.value().definition), code, field);
  }
  // A finer rate can be specified without the other rate.
  const std::string coarse_rate = "bytes_per_1024_cycles: 260";
  auto fine_text = yaml;
  fine_text.replace(fine_text.find(coarse_rate), coarse_rate.size(),
                    "bytes_per_32768_cycles: 8328");
  const auto fine = config::parse(fine_text, "hub.yaml");
  REQUIRE(fine);
  CHECK(fine.value().definition.initiators[0].qos.shaper == QosShaper{std::nullopt, 64, 8328});
  const auto fine_made = generate(fine.value().definition);
  REQUIRE(fine_made);
  const auto fine_shaper = contract(fine_made.value())["qos"]["sources"]["s0"]["shaper"];
  CHECK(fine_shaper["bytes_per_32768_cycles"] == 8328);
  CHECK_FALSE(fine_shaper.contains("bytes_per_1024_cycles"));
  CHECK_FALSE(c["qos"]["sources"]["s0"]["shaper"].contains("bytes_per_32768_cycles"));
  const auto fine_zero = nocloom::detail::budget_scale(QosShaper{std::nullopt, 0, 0});
  CHECK(fine_zero.rate == 0);
  CHECK(fine_zero.byte_at == 10);
  const auto equivalent = nocloom::detail::budget_scale(QosShaper{std::nullopt, 64, 8320});
  CHECK(equivalent.rate == 260);
  CHECK(equivalent.byte_at == 10);
  // Levels need every endpoint to be 32-bit APB without a timeout, or full AXI4.
  auto mixed = d;
  mixed.targets[1].protocol = Protocol::Axi4;
  rejected(generate(mixed), "NCL_QOS_PROFILE", "exchange");
  mixed = d;
  mixed.targets[1].data_width = 16;
  rejected(generate(mixed), "NCL_QOS_PROFILE", "exchange");
  mixed = d;
  mixed.targets[1].timeout = TargetTimeout{1024};
  rejected(generate(mixed), "NCL_QOS_PROFILE", "exchange");
  mixed = d;
  mixed.targets[1].protocol = Protocol::Apb3;
  mixed.targets[1].attributes.reset();
  mixed.initiators[1].protocol = Protocol::Apb3;
  REQUIRE(generate(mixed));
}

TEST_CASE("Schedulers bind 32-bit APB sources and pass link pipes", "[fixed-qos]") {
  using namespace nocloom;
  auto d = hub(2);
  for (auto &i : d.initiators)
    i.protocol = Protocol::Apb4;
  for (auto &t : d.targets)
    t.protocol = Protocol::Apb4;
  d.initiators[0].qos.shaper = QosShaper{260, 64};
  d.initiators[0].qos.scheduler = QosScheduler{3, 0, 50};
  d.initiators[1].qos.levels = {2, 2};
  const auto scheduled = generate(d);
  REQUIRE(scheduled);
  const auto c = contract(scheduled.value());
  const auto &r = c["qos"]["sources"]["s0"]["scheduler"];
  CHECK(c["qos"]["sources"]["s0"]["shaper"]["bytes_per_1024_cycles"] == 260);
  CHECK(c["qos"]["sources"]["s0"]["shaper"]["burst_bytes"] == 64);
  CHECK(r["under_level"] == 3);
  CHECK(r["over_level"] == 0);
  CHECK(r["threshold_percent"] == 50);
  CHECK(c["qos"]["pressure"]["source"] == "packet_head_level_and_scheduler_levels");
  CHECK(c["service"] == "lower_levels_require_higher_levels_to_yield_no_global_wait_bound");
  const auto map = std::ranges::find_if(
      scheduled.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  const auto network = nlohmann::json::parse(map->text)["network"];
  const auto store = std::ranges::find_if(
      network["storage"], [](const auto &s) { return s["resource"] == "source/s0/scheduler"; });
  REQUIRE(store != network["storage"].end());
  CHECK((*store)["control"].size() == 24);
  CHECK((*store)["payload"].size() == 2);
  CHECK(std::ranges::count_if(network["resources"],
                              [](const auto &r) { return r["role"] == "scheduler"; }) == 1);
  CHECK(network["formal"]["sources"]["s0"]["scheduler_counter"] == (*store)["control"][0]);
  CHECK(network["formal"]["sources"]["s0"].contains("request_raise"));
  CHECK_FALSE(network["formal"]["sources"]["s1"].contains("request_raise"));
  // Level round robin keeps one next search start per level 0..3 beside owner and held.
  auto turns = d;
  turns.arbitration = ArbitrationPolicy::LevelRoundRobin;
  const auto levelled = generate(turns);
  REQUIRE(levelled);
  const auto lmap = std::ranges::find_if(
      levelled.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  const auto lnet = nlohmann::json::parse(lmap->text)["network"];
  const auto grant = std::ranges::find_if(lnet["storage"], [](const auto &s) {
    return s["resource"].template get<std::string>().starts_with("graph/req/");
  });
  REQUIRE(grant != lnet["storage"].end());
  CHECK((*grant)["control"].size() == 6);
  // A scheduler alone sets levels: the level profile applies.
  auto alone = d;
  alone.initiators[1].qos = {};
  REQUIRE(generate(alone));
  alone.targets[1].protocol = Protocol::Axi4;
  rejected(generate(alone), "NCL_QOS_PROFILE", "exchange");
  // The YAML form parses to the same definition.
  const std::string yaml = R"(schema_version: 10
name: hub
address_width: 32
connectivity: all
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: s0, clock_domain: main, protocol: apb4, data_width: 32,
     qos: {shaper: {bytes_per_1024_cycles: 260, burst_bytes: 64}, scheduler: {under_level: 3, over_level: 0, threshold_percent: 50}}}
  - {name: s1, clock_domain: main, protocol: apb4, data_width: 32,
     qos: {levels: {read: 2, write: 2}}}
targets:
  - {name: t0, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
  - {name: t1, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x1000', size: '0x1000'}]}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = config::parse(yaml, "hub.yaml");
  REQUIRE(parsed);
  CHECK(parsed.value().definition.initiators[0].qos == d.initiators[0].qos);
  for (const auto &[from, to, code, field] :
       std::vector<std::tuple<std::string, std::string, const char *, const char *>>{
           {"under_level: 3", "under_level: 3, depth: 1", "NCL_SCHEMA_UNKNOWN_FIELD",
            "initiators[0].qos.scheduler.depth"},
           {"over_level: 0, ", "", "NCL_SCHEMA_MISSING_FIELD",
            "initiators[0].qos.scheduler.over_level"},
           {"threshold_percent: 50", "threshold_percent: 101", "NCL_QOS_SCHEDULER",
            "initiators[0].qos.scheduler"},
           {"under_level: 3", "under_level: 8", "NCL_QOS_SCHEDULER", "initiators[0].qos.scheduler"},
           {"qos: {shaper", "qos: {levels: {read: 1}, shaper", "NCL_QOS_SCHEDULER",
            "initiators[0].qos.scheduler"},
           {"shaper: {bytes_per_1024_cycles: 260, burst_bytes: 64}, ", "", "NCL_QOS_SCHEDULER",
            "initiators[0].qos.scheduler"},
           {"apb4, data_width: 32,\n     qos: {shaper", "apb3, data_width: 16,\n     qos: {shaper",
            "NCL_QOS_PROFILE", "initiators[0].qos.shaper"}}) {
    auto text = yaml;
    text.replace(text.find(from), from.size(), to);
    INFO(text);
    const auto result = config::parse(text, "hub.yaml");
    if (!result)
      rejected(result, code, field);
    else
      rejected(generate(result.value().definition), code, field);
  }
  // Link pipes carry the levels and raise packets; an APB source with a scheduler, one burst at a
  // time, takes no packets or issue pipe.
  auto piped = hub(2, 1);
  for (auto &i : piped.initiators)
    i.protocol = Protocol::Apb4;
  piped.targets[0].protocol = Protocol::Apb4;
  piped.initiators[0].qos.shaper = QosShaper{260, 64};
  piped.initiators[0].qos.scheduler = QosScheduler{3, 0, 50};
  piped.topology->request = {{{"a"}, {"b"}},
                             {{"a", "b", std::nullopt, {PipeSlice::Full}}},
                             {{"s0", "a"}, {"s1", "a"}},
                             {{"t0", "b"}}};
  piped.topology->response = {
      {{"a"}, {"b"}}, {{"b", "a"}}, {{"t0", "b"}}, {{"s0", "a"}, {"s1", "a"}}};
  REQUIRE(generate(piped));
  piped.initiators[0].pipes.issue = true;
  rejected(generate(piped), "NCL_QOS_PROFILE", "initiators[0].pipes");
  piped.initiators[0].pipes.issue = false;
  piped.initiators[0].pipes.packets = {PipeSlice::Full};
  rejected(generate(piped), "NCL_QOS_PROFILE", "initiators[0].pipes");
  piped.initiators[0].pipes.packets = {};
  piped.initiators[1].pipes.issue = true;
  REQUIRE(generate(piped));
}

TEST_CASE("AxQOS maps carry per-burst levels in request packets", "[fixed-qos]") {
  using namespace nocloom;
  auto d = hub(2);
  d.name = "qos_map";
  const auto plain = generate(d);
  REQUIRE(plain);
  d.initiators[0].qos.axqos_map = {0, 0, 1, 1, 2, 2, 3, 3};
  d.initiators[1].qos.levels = {2, 1};
  const auto mapped = generate(d);
  REQUIRE(mapped);
  const auto c = contract(mapped.value());
  CHECK(c["qos"]["sources"]["s0"]["axqos_map"] == nlohmann::json::array({0, 0, 1, 1, 2, 2, 3, 3}));
  CHECK(c["qos"]["physical_AxQOS"] == "AWQOS_ARQOS_of_mapped_sources_only");
  CHECK(c["qos"]["mapped_levels"] == "request_packet_level_field");
  CHECK(c["qos"]["pressure"]["switch_output"] == "or_of_its_input_pressures_while_valid");
  const auto file = [](const auto &artifacts, const char *suffix) {
    return std::ranges::find_if(artifacts, [&](const auto &a) { return a.path.ends_with(suffix); })
        ->text;
  };
  const auto rtl = file(mapped.value(), "qos_map.v");
  CHECK(rtl.find("nl_i_s0_awqos") != std::string::npos);
  CHECK(rtl.find("input wire [2:0] nl_i_s0_arqos") != std::string::npos);
  CHECK(rtl.find("nl_i_s1_awqos") == std::string::npos);
  CHECK(file(plain.value(), "qos_map.v").find("_awqos") == std::string::npos);
  const auto network = nlohmann::json::parse(file(mapped.value(), ".map.json"))["network"];
  CHECK(network["formal"]["sources"]["s0"].contains("request_level"));
  CHECK_FALSE(network["formal"]["sources"]["s1"].contains("request_level"));
  // Several bursts in flight and a level round robin keep the levels.
  auto deep = d;
  for (auto &i : deep.initiators)
    i.max_outstanding = 4;
  deep.targets[0].max_outstanding = 2;
  deep.arbitration = ArbitrationPolicy::LevelRoundRobin;
  REQUIRE(generate(deep));
  // A mapped source wider than its targets keeps its level through the width clamp: the request
  // level at its network entry follows its AxQOS ports, with one burst or several in flight.
  for (const unsigned outstanding : {1U, 4U}) {
    auto wide = d;
    wide.initiators[0].data_width = 64;
    wide.initiators[0].max_outstanding = outstanding;
    const auto plan = nocloom::detail::build_network_plan(wide);
    REQUIRE(plan);
    REQUIRE(plan.value().sources[0].clamp);
    const auto hardware = nocloom::detail::lower_axi4_exchange_composition(plan.value());
    REQUIRE(hardware);
    const auto &ir = hardware.value().ir;
    std::set<std::string> inputs;
    std::set<std::size_t> seen;
    std::vector<nocloom::detail::ValueId> todo{
        hardware.value().bindings.at("source/s0/request.packet_level")};
    while (!todo.empty()) {
      const auto id = todo.back();
      todo.pop_back();
      if (!seen.insert(id.index).second)
        continue;
      const auto &v = ir.values.at(id.index);
      if (v.op == nocloom::detail::Op::State)
        todo.push_back(ir.states.at(v.state.index).next);
      else if (v.op == nocloom::detail::Op::Input)
        inputs.insert(ir.ports.at(v.input_port.index).name);
      else
        for (unsigned i = 0; i < nocloom::detail::operand_count(v.op); ++i)
          todo.push_back(v.operands[i]);
    }
    CHECK(inputs.contains("nl_i_s0_awqos"));
    CHECK(inputs.contains("nl_i_s0_arqos"));
  }
  // Map domain: 2, 4, 8 or 16 nondecreasing levels of 0..7 on a full AXI initiator alone.
  for (const auto &map : std::vector<std::vector<unsigned>>{
           {1}, {0, 1, 2}, {0, 8}, {1, 0}, std::vector<unsigned>(32, 0)}) {
    auto bad = d;
    bad.initiators[0].qos.axqos_map = map;
    rejected(generate(bad), "NCL_QOS_MAP", "initiators[0].qos.axqos_map");
  }
  auto bad = d;
  bad.initiators[1].qos.axqos_map = {0, 1};
  rejected(generate(bad), "NCL_QOS_MAP", "initiators[1].qos.axqos_map");
  bad = d;
  bad.initiators[0].protocol = Protocol::Axi4Lite;
  rejected(generate(bad), "NCL_QOS_MAP", "initiators[0].qos.axqos_map");
  // The level profile applies: equal-width full AXI4 endpoints only.
  bad = d;
  bad.targets[1].protocol = Protocol::Apb4;
  rejected(generate(bad), "NCL_QOS_PROFILE", "exchange");
  // The YAML form parses to the same definition.
  const std::string yaml = R"(schema_version: 10
name: qos_map
address_width: 32
connectivity: all
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32,
     qos: {axqos_map: [0, 0, 1, 1, 2, 2, 3, 3]}}
  - {name: s1, clock_domain: main, protocol: axi4, data_width: 32,
     qos: {levels: {read: 2, write: 1}}}
targets:
  - {name: t0, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = config::parse(yaml, "qos_map.yaml");
  REQUIRE(parsed);
  CHECK(parsed.value().definition.initiators[0].qos == d.initiators[0].qos);
  for (const auto &[from, to, code, field] :
       std::vector<std::tuple<std::string, std::string, const char *, const char *>>{
           {"[0, 0, 1, 1, 2, 2, 3, 3]", "[0, 0, 1, 1, 2, 2, 3, x]", "NCL_SCHEMA_TYPE",
            "initiators[0].qos.axqos_map[7]"},
           {"[0, 0, 1, 1, 2, 2, 3, 3]", "3", "NCL_SCHEMA_TYPE", "initiators[0].qos.axqos_map"},
           {"[0, 0, 1, 1, 2, 2, 3, 3]", "[3, 2]", "NCL_QOS_MAP", "initiators[0].qos.axqos_map"},
           {"qos: {axqos_map", "qos: {levels: {read: 1}, axqos_map", "NCL_QOS_MAP",
            "initiators[0].qos.axqos_map"}}) {
    auto text = yaml;
    text.replace(text.find(from), from.size(), to);
    INFO(text);
    const auto result = config::parse(text, "qos_map.yaml");
    if (!result)
      rejected(result, code, field);
    else
      rejected(generate(result.value().definition), code, field);
  }
}

TEST_CASE("AXI sources shape and schedule whole bursts", "[fixed-qos]") {
  using namespace nocloom;
  auto d = hub(2);
  d.name = "qos_axi";
  d.initiators[0].qos.shaper = QosShaper{64, 64};
  d.initiators[0].max_outstanding = 4;
  d.initiators[1].qos.levels = {2, 2};
  d.initiators[1].max_outstanding = 4;
  const auto shaped = generate(d);
  REQUIRE(shaped);
  CHECK(contract(shaped.value())["qos"]["sources"]["s0"]["shaper"]["bytes_per_1024_cycles"] == 64);
  // A burst cut into packets would be charged twice: the whole-burst rule refuses it.
  auto cut = d;
  cut.transport = TransportOptions{};
  cut.transport->packet_limits = {{"s0", "t0", 64, std::nullopt}};
  cut.initiators[0].len_width = 8; // 256 beats of 4 bytes exceed a 64-byte packet
  rejected(generate(cut), "NCL_QOS_PROFILE", "exchange");
  cut.initiators[0].len_width = 4; // 16 beats of 4 bytes fit it
  REQUIRE(generate(cut));
  // A scheduler keeps one context per burst in flight.
  auto reg = d;
  reg.initiators[0].qos = {};
  reg.initiators[0].qos.shaper = QosShaper{260, 64};
  reg.initiators[0].qos.scheduler = QosScheduler{3, 0, 50};
  const auto store_of = [](const ArtifactSet &artifacts) {
    const auto map = std::ranges::find_if(
        artifacts, [](const auto &a) { return a.path.ends_with(".map.json"); });
    const auto network = nlohmann::json::parse(map->text)["network"];
    const auto store = std::ranges::find_if(
        network["storage"], [](const auto &s) { return s["resource"] == "source/s0/scheduler"; });
    REQUIRE(store != network["storage"].end());
    return *store;
  };
  const auto four = generate(reg);
  REQUIRE(four);
  // Packed budget and wait state with four context ledgers, including reuse masks.
  CHECK(store_of(four.value())["control"].size() == 52);
  CHECK(store_of(four.value())["payload"].size() == 5);
  reg.initiators[0].max_outstanding = 1;
  const auto scheduled = generate(reg);
  REQUIRE(scheduled);
  CHECK(store_of(scheduled.value())["control"].size() == 24);
  // An AXI-Lite or AHB initiator takes neither.
  auto lite = d;
  lite.initiators[0].protocol = Protocol::Axi4Lite;
  rejected(generate(lite), "NCL_QOS_PROFILE", "initiators[0].qos.shaper");
}

// The budget counters stepped against integers that have no width. A counter has 17 bits of
// whole bytes above 10 or 15 bits of a fraction: a step that wrapped would differ from the
// integers. The cases are the corners of the legal shaper range with charges from one byte to
// the largest burst.
TEST_CASE("Budget counters charge bytes and hold every legal charge", "[fixed-qos]") {
  using namespace nocloom;
  using namespace nocloom::detail;
  for (const unsigned fine : {0U, 1U, 31U})
    for (const unsigned burst : {0U, 1U, 15U, 16U, 18U, 100U, 65535U})
      for (const unsigned coarse : {0U, 1U, 65U, 1024U, 65535U}) {
        CAPTURE(fine, burst, coarse);
        const unsigned bits = fine ? 32 : 27;
        const std::uint64_t unit = fine ? 32768 : 1024, top = std::uint64_t{1} << bits;
        const std::uint64_t rate = fine ? 32ULL * coarse + fine : coarse;
        const std::uint64_t open_below = (std::uint64_t{burst} + 1) * unit;
        const QosShaper shaper =
            fine ? QosShaper{std::nullopt, burst, unsigned(rate)} : QosShaper{coarse, burst};
        Builder b;
        const auto input = [&](const std::string &name, unsigned width) {
          return b.input(name, width, "probe", name);
        };
        const auto port = [&](ValueId id) { return b.ir.values[id.index].input_port.index; };
        const auto clock = b.input("clock", 1, "domain:main", "clock");
        const auto reset = b.input("reset", 1, "domain:main", "reset_n");
        const auto valid = input("valid", 1), last = input("last", 1), ready = input("ready", 1);
        // A charge is given as its bytes less one: an input, and the constant of 4 bytes.
        const auto less_one = input("less_one", 16), done = input("done", 12);
        const auto shaped = lower_shaper(b, clock, reset, valid, last, ready, shaper, less_one);
        const auto fixed =
            lower_shaper(b, clock, reset, valid, last, ready, shaper, b.constant(16, 3));
        // The scheduler's counter with the level it selects at half of the burst.
        const auto levels = budget_levels(shaper, QosScheduler{3, 1, 50});
        const auto front =
            lower_scheduler_front(b, clock, reset, b.constant(1, 0), b.constant(1, 0), levels, 1);
        count_scheduler(b, front.counter, front.charged, done, levels);
        REQUIRE(b.ir.states.size() == 12);
        REQUIRE(b.ir.values[shaped.counter.index].width == bits);
        const auto state = [&](ValueId id) { return b.ir.values[id.index].state.index; };
        ConeEvaluator evaluate(b.ir);
        for (const std::uint64_t bytes : {1U, 4U, 16U, 4096U, 32768U}) {
          const std::uint64_t charge = bytes * unit;
          // A shaper counter is below open_below when it takes a charge: the reachable ones.
          for (const auto counter : {std::uint64_t{0}, rate, rate + 1, open_below - 1, open_below,
                                     open_below + charge - 1})
            for (unsigned bitset = 0; bitset < 32; ++bitset) {
              const bool v = bitset & 1, l = bitset & 2, r = bitset & 4, inside = bitset & 8,
                         held = bitset & 16;
              // A head is offered only while the counter admits it, and the counter only falls
              // while that head waits: a head held over a closed counter does not occur.
              if (held && !inside && counter >= open_below)
                continue;
              CAPTURE(bytes, counter, v, l, r, inside, held);
              const ConeEvaluator::Words states{
                  {state(shaped.counter), counter}, {state(shaped.inside), inside},
                  {state(shaped.held), held},       {state(fixed.counter), counter},
                  {state(fixed.inside), inside},    {state(fixed.held), held}};
              const ConeEvaluator::Words inputs{
                  {port(valid), v}, {port(last), l}, {port(ready), r}, {port(less_one), bytes - 1}};
              const bool offered = v && (held || counter < open_below);
              const bool charged = offered && r && !inside;
              const std::uint64_t sum = counter + (charged ? charge : 0);
              REQUIRE(sum < top);
              const std::uint64_t next = sum < rate ? 0 : sum - rate;
              CHECK(evaluate(shaped.valid.index, states, inputs) == std::uint64_t{offered});
              CHECK(evaluate(shaped.charge.index, states, inputs) == std::uint64_t{charged});
              CHECK(evaluate(b.ir.states[state(shaped.counter)].next.index, states, inputs) ==
                    next);
              if (bytes == 4)
                CHECK(evaluate(b.ir.states[state(fixed.counter)].next.index, states, inputs) ==
                      next);
            }
          // A scheduler counter never holds more whole bytes than the burst, and a context
          // keeps at most 4096 bytes.
          if (bytes > 4096)
            continue;
          for (const auto counter : {std::uint64_t{0}, rate, rate + 1,
                                     open_below - charge % open_below - 1, open_below - 1})
            for (const bool paid : {false, true}) {
              if (counter >= open_below)
                continue;
              CAPTURE(bytes, counter, paid);
              const ConeEvaluator::Words states{{state(front.counter), counter},
                                                {state(front.charged), paid}};
              const ConeEvaluator::Words inputs{{port(done), bytes - 1}};
              const std::uint64_t sum = counter + (paid ? charge : 0);
              REQUIRE(sum < top);
              const std::uint64_t left = sum < rate ? 0 : sum - rate;
              const std::uint64_t next = left / unit > burst ? burst * unit : left;
              REQUIRE(next < open_below);
              CHECK(evaluate(b.ir.states[state(front.counter)].next.index, states, inputs) == next);
              CHECK(evaluate(front.now.index, states, inputs) ==
                    (counter / unit <= std::uint64_t{burst} * 50 / 100 ? 3U : 1U));
            }
        }
      }
  // The limits of the shaper fields are those the counter was sized for.
  auto d = hub(2);
  d.initiators[0].qos.shaper = QosShaper{std::nullopt, 65535, 2097151};
  REQUIRE(generate(d));
  d.initiators[0].qos.shaper = QosShaper{65535, 65536};
  rejected(generate(d), "NCL_QOS_SHAPER", "initiators[0].qos.shaper");
  d.initiators[0].qos.shaper = QosShaper{0, 0, 0};
  rejected(generate(d), "NCL_QOS_SHAPER", "initiators[0].qos.shaper");
  d.initiators[0].qos.shaper = QosShaper{std::nullopt, 0, 2097152};
  rejected(generate(d), "NCL_QOS_SHAPER", "initiators[0].qos.shaper");
}

TEST_CASE("Scheduler charges only a matching completion", "[fixed-qos]") {
  using namespace nocloom;
  using namespace nocloom::detail;
  Builder b;
  const auto clock = b.input("clock", 1, "domain:main", "clock");
  const auto reset = b.input("reset", 1, "domain:main", "reset_n");
  const auto done = b.input("done", 1, "probe", "done");
  const auto zero = b.constant(1, 0), one = b.constant(1, 1);
  const auto r = lower_scheduler(b, clock, reset, zero, one, zero, one, done,
                                 budget_levels(QosShaper{0, 64}, QosScheduler{}),
                                 b.constant(budget_charge_bits, 3));
  const auto state = [&](ValueId v) { return b.ir.values[v.index].state.index; };
  const auto next = b.ir.states[state(r.charged)].next.index;
  const auto used = state(r.words[8]); // first context, after the scheduler controls
  const auto dir = state(r.words[9]);
  const auto port = b.ir.values[done.index].input_port.index;
  ConeEvaluator evaluate(b.ir);
  CHECK(evaluate(next, {}, {{port, 1}}) == 0);
  CHECK(evaluate(next, {{used, 1}, {dir, 1}}, {{port, 1}}) == 0);
  CHECK(evaluate(next, {{used, 1}}, {{port, 0}}) == 0);
  CHECK(evaluate(next, {{used, 1}}, {{port, 1}}) == 1);
}

TEST_CASE("AXI charges count the active byte positions", "[fixed-qos]") {
  using namespace nocloom::detail;
  for (unsigned max_size : {2u, 7u}) {
    Builder b;
    const auto input = [&](const char *name, unsigned width) {
      return b.input(name, width, "probe", name);
    };
    const auto len = input("len", 8), size = input("size", 3);
    const auto burst = input("burst", 2), start = input("start", max_size);
    const auto charge = axi_burst_charge(b, len, size, burst, start, max_size);
    const auto port = [&](ValueId v) { return b.ir.values[v.index].input_port.index; };
    ConeEvaluator evaluate(b.ir);
    for (unsigned n = 0; n <= max_size; ++n)
      for (unsigned offset = 0; offset < (1u << n); ++offset)
        for (unsigned beats : {1u, 2u, 4u, 7u, 15u, 16u, 256u})
          for (const bool fixed : {true, false}) {
            if (fixed && beats > 16)
              continue;
            const ConeEvaluator::Words req{{port(len), beats - 1},
                                           {port(size), n},
                                           {port(burst), !fixed},
                                           {port(start), offset}};
            CAPTURE(max_size, n, offset, beats, fixed);
            CHECK(evaluate(charge.index, {}, req) ==
                  beats * (1u << n) - offset * (fixed ? beats : 1) - 1);
          }
  }
}

TEST_CASE("Scheduler completion selects a context by direction and tag", "[fixed-qos]") {
  using namespace nocloom;
  using namespace nocloom::detail;
  Builder b;
  const auto clock = b.input("clock", 1, "domain:main", "clock");
  const auto reset = b.input("reset", 1, "domain:main", "reset_n");
  const auto input = [&](const char *name, unsigned width) {
    return b.input(name, width, "probe", name);
  };
  const SchedulerCompletion rd{input("r_done", 1), input("r_tag", 2)};
  const SchedulerCompletion wr{input("w_done", 1), input("w_tag", 2)};
  const auto zero = b.constant(1, 0), one = b.constant(1, 1);
  const auto r = lower_scheduler_contexts(
      b, clock, reset, zero, one, zero, 0, zero, b.constant(2, 0), {zero, zero, {}}, one, rd, wr,
      budget_levels(QosShaper{0, 64}, QosScheduler{}), b.constant(budget_charge_bits, 3), 2);
  const auto state = [&](ValueId v) { return b.ir.values[v.index].state.index; };
  const auto next = [&](ValueId v) { return b.ir.states[state(v)].next.index; };
  const auto port = [&](ValueId v) { return b.ir.values[v.index].input_port.index; };
  const ConeEvaluator::Words active_read{
      {state(r.words[8]), 1}, {state(r.words[10]), 2}, {state(r.words[11]), 11}};
  ConeEvaluator evaluate(b.ir);
  for (unsigned tag : {0u, 2u}) {
    const ConeEvaluator::Words read{{port(rd.valid), 1}, {port(rd.tag), tag}};
    const ConeEvaluator::Words write{{port(wr.valid), 1}, {port(wr.tag), tag}};
    CHECK(evaluate(next(r.charged), active_read, read) == (tag == 2));
    CHECK(evaluate(next(r.words[4]), active_read, read) == (tag == 2 ? 11 : 0));
    CHECK(evaluate(next(r.charged), active_read, write) == 0);
  }
}

TEST_CASE("Upgrade obligations rotate and retain newer requirements", "[fixed-qos]") {
  using namespace nocloom::detail;
  for (unsigned count : {1U, 2U, 3U, 7U, 16U, 64U}) {
    Builder b;
    b.ir.name = "nocloom_upgrade_test";
    const auto input = [&](const std::string &name, unsigned bits) {
      return b.input(name, bits, "probe", name);
    };
    const auto clock = b.input("clock", 1, "domain:main", "clock");
    const auto reset = b.input("reset", 1, "domain:main", "reset_n");
    const auto valid = input("valid", 1), last = input("last", 1), ready = input("ready", 1);
    const auto bits = std::max(1u, unsigned(std::bit_width(count)));
    const auto route = input("route", bits);
    std::vector<ValueId> live, routes, finished, launch, required;
    for (unsigned i = 0; i < count; ++i) {
      live.push_back(input("live" + std::to_string(i), 1));
      routes.push_back(input("route" + std::to_string(i), bits));
      finished.push_back(input("finish" + std::to_string(i), 1));
      launch.push_back(input("launch" + std::to_string(i), 1));
      required.push_back(input("required" + std::to_string(i), 3));
    }
    const auto u = lower_upgrade_ledger(b, clock, reset, valid, last, route, ready, bits, live,
                                        routes, finished);
    for (unsigned i = 0; i < count; ++i)
      update_upgrade_context(b, u, i, live[i], launch[i], launch[i], finished[i], b.constant(3, 0),
                             required[i]);
    REQUIRE(verify_structure(b.ir));
    std::map<std::size_t, std::uint64_t> states, inputs;
    for (unsigned i = 0; i < b.ir.states.size(); ++i)
      states[i] = 0;
    const auto set = [&](ValueId id, unsigned value) {
      inputs[b.ir.values[id.index].input_port.index] = value;
    };
    const auto get = [&](ValueId id) {
      const auto value = evaluate_cone(b.ir, id.index, states, inputs);
      REQUIRE(value);
      return *value;
    };
    const auto tick = [&] {
      auto next = states;
      for (unsigned i = 0; i < b.ir.states.size(); ++i)
        next[i] = get(b.ir.states[i].next);
      states = std::move(next);
    };
    set(route, count);
    set(last, 1);
    set(ready, 1);
    set(valid, 1);
    for (unsigned i = 0; i < count; ++i) {
      set(routes[i], i);
      set(route, i);
      set(launch[i], 1);
      tick();
      set(launch[i], 0);
      set(live[i], 1);
    }
    set(route, count);
    set(valid, 0);
    set(ready, 0);
    for (auto id : required)
      set(id, 3);
    tick();
    tick();
    REQUIRE(get(u.valid) == 1);
    const auto frozen_route = get(u.route), frozen_level = get(u.level);
    for (unsigned i = 0; i < count; ++i)
      set(required[i], 7);
    for (unsigned n = 0; n < 5; ++n) {
      tick();
      CHECK(get(u.route) == frozen_route);
      CHECK(get(u.level) == frozen_level);
      CHECK(get(u.cursor) == 0);
    }
    set(ready, 1);
    set(valid, 1);
    std::vector<unsigned> confirmations(count, 0);
    unsigned data_gap = 0;
    for (unsigned n = 0; n < 4 * (count + 1) + 2; ++n) {
      if (get(u.sent)) {
        const auto selected = get(u.route);
        REQUIRE(selected < count);
        if (get(u.level) == 7)
          ++confirmations[selected];
      }
      data_gap = get(u.taken) ? 0 : data_gap + 1;
      CHECK(data_gap <= 2);
      tick();
    }
    for (unsigned i = 0; i < count; ++i) {
      CAPTURE(count, i);
      CHECK(get(u.wanted[i]) == 7);
      CHECK(get(u.acknowledged[i]) == 7);
      CHECK(confirmations[i] == 1);
    }
  }
}

TEST_CASE("AHB fixed levels use the port protocol and retain zero-level graphs",
          "[fixed-qos][axi4-config]") {
  using namespace nocloom;
  for (const auto protocol : {Protocol::AhbLite, Protocol::Ahb5}) {
    auto d = hub(2);
    d.initiators[0].protocol = protocol;
    for (unsigned read = 0; read < 8; ++read)
      for (unsigned write = 0; write < 8; ++write) {
        d.initiators[0].qos.levels = {read, write};
        REQUIRE(generate(d));
      }
    d.initiators[0].qos = {};
    d.initiators[0].qos.axqos_map = {0, 7};
    rejected(generate(d), "NCL_QOS_MAP", "initiators[0].qos.axqos_map");
    d.initiators[0].qos = {};
    d.initiators[0].qos.shaper = QosShaper{64, 64};
    rejected(generate(d), "NCL_QOS_PROFILE", "initiators[0].qos.shaper");
    d.initiators[0].qos.scheduler = QosScheduler{3, 0, 50};
    rejected(generate(d), "NCL_QOS_PROFILE", "initiators[0].qos.scheduler");
    d.initiators[0].qos = {};
    d.targets[0].protocol = protocol;
    const auto plain = generate(d);
    REQUIRE(plain);
    d.initiators[0].qos.levels = {0, 0};
    REQUIRE(generate(d));
    CHECK(generate(d).value() == plain.value());
    d.initiators[0].qos.levels = {1, 0};
    rejected(generate(d), "NCL_QOS_PROFILE", "targets[0].protocol");
    d.targets[0].protocol = Protocol::Axi4;
    for (const auto option :
         {"axqos_map: [0, 7]", "shaper: {bytes_per_1024_cycles: 64, burst_bytes: 64}",
          "scheduler: {under_level: 3, over_level: 0, threshold_percent: 50}"}) {
      auto yaml = with("arbitration: {policy: round_robin}");
      const std::string source = "protocol: axi4, data_width: 32}";
      yaml.replace(yaml.find(source), source.size(),
                   std::string("protocol: ") + (protocol == Protocol::Ahb5 ? "ahb5" : "ahb_lite") +
                       ", data_width: 32, qos: {" + std::string(option) + "}}");
      const auto parsed = config::parse(yaml, "hub.yaml");
      REQUIRE(parsed);
      const auto result = generate(parsed.value().definition);
      REQUIRE_FALSE(result);
      CHECK(std::ranges::any_of(result.diagnostics(), [](const auto &diag) {
        return diag.field.starts_with("initiators[0].qos.");
      }));
    }
  }
}
