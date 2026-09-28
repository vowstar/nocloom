#include "../graph_clocked_definition.hpp"
#include "../public_artifacts.hpp"
#include "core/capability.hpp"
#include "core/exchange_composition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <iostream>
#include <nocloom/generation.hpp>
#include <numeric>
#include <set>
using namespace nocloom::detail;

namespace {
// The public three-source graph in domains a and b: s1, t1 and the request branch in b, every
// other endpoint and switch in a. a -> b takes the global two stages, b -> a its own four.
nocloom::Definition placed() {
  auto d = graph_example_definition();
  d.clock_domains = {{"a", "clk_a", "reset_n", "test"}, {"b", "clk_b", "reset_n", "test"}};
  d.synchronizer_stages = 2;
  d.clock_crossings = {{"b", "a", std::nullopt, 4}};
  for (auto &i : d.initiators)
    i.clock_domain = i.name == "s1" ? "b" : "a";
  for (auto &t : d.targets)
    t.clock_domain = t.name == "t1" ? "b" : "a";
  for (auto &n : d.topology->request.nodes)
    n.clock_domain = n.name == "branch" ? "b" : "a";
  for (auto &n : d.topology->response.nodes)
    n.clock_domain = "a";
  return d;
}
// The hardware identity of a configured switch of the "req" or "rsp" graph; endpoints keep their
// names.
std::string hw(const PublicArtifacts &x, const std::string &graph, const std::string &name) {
  const auto &nodes =
      x.map["definition"]["topology"][graph == "req" ? "request" : "response"]["nodes"];
  const auto it = std::ranges::find_if(nodes, [&](const auto &n) { return n["name"] == name; });
  return it == nodes.end() ? name : (*it)["hardware_node"].get<std::string>();
}
// A configured segment ("req/merge>branch") in hardware identities.
std::string segment(const PublicArtifacts &x, const std::string &key) {
  const auto graph = key.substr(0, 3), edge = key.substr(4);
  const auto split = edge.find('>');
  return graph + "/" + hw(x, graph, edge.substr(0, split)) + ">" +
         hw(x, graph, edge.substr(split + 1));
}
const nlohmann::json &cut(const PublicArtifacts &x, const std::string &key) {
  const auto &cuts = x.map.at("network").at("cdc").at("cuts");
  const auto it =
      std::ranges::find_if(cuts, [&](const auto &c) { return c["segment"] == segment(x, key); });
  REQUIRE(it != cuts.end());
  return *it;
}
unsigned data_bits(const nlohmann::json &c) {
  return std::accumulate(c["fields"].begin(), c["fields"].end(), 0U,
                         [](unsigned sum, const auto &f) {
                           return sum + (f["name"].template get<std::string>().starts_with("data")
                                             ? f["width"].template get<unsigned>()
                                             : 0U);
                         });
}
std::string domain_of(const nlohmann::json *entry) {
  REQUIRE(entry != nullptr);
  return (*entry)["clock_domain"].get<std::string>();
}
// The (depth, source) every cut sent from one domain takes.
std::pair<unsigned, std::string> depth_of(const PublicArtifacts &x, const std::string &from) {
  std::set<std::pair<unsigned, std::string>> depths;
  for (const auto &c : x.map["network"]["cdc"]["cuts"])
    if (c["from_domain"] == from)
      depths.emplace(c["depth"].get<unsigned>(), c["depth_source"].get<std::string>());
  REQUIRE(depths.size() == 1);
  return *depths.begin();
}

// The planned and composed public graph, for the checks of corrupted hardware:
// build_network_plan, lower_axi4_exchange_composition, then verify_axi4_exchange_composition.
ExchangeHardware lowered(const nocloom::Definition &d) {
  REQUIRE(nocloom::validate(d));
  auto plan = build_network_plan(d);
  REQUIRE(plan);
  auto result = lower_axi4_exchange_composition(plan.value());
  for (const auto &diagnostic : result.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(result);
  REQUIRE(verify_axi4_exchange_composition(result.value()));
  return std::move(result.value());
}
} // namespace

TEST_CASE("Clock placement derives independent directed cuts at actual link words",
          "[exchange-clocking]") {
  auto d = placed();
  d.initiators[1].data_width = 256;
  d.address_width = 40;
  const auto x = public_artifacts(d);
  const auto &cdc = x.map["network"]["cdc"];
  std::set<std::string> crossing, expected;
  for (const auto *key : {"req/s1>leaf1", "req/merge>branch", "req/branch>tail", "req/branch>t0",
                          "req/tail>t1", "rsp/t1>merge", "rsp/branch>s1"})
    expected.insert(segment(x, key));
  unsigned payload = 0;
  for (const auto &c : cdc["cuts"]) {
    crossing.insert(c["segment"].get<std::string>());
    CHECK(c["synchronizer_stages"] == (c["from_domain"] == "a" ? 2 : 4));
    // Without clock periods every crossing takes the fallback depth.
    CHECK(c["depth"] == 6);
    CHECK(c["depth_source"] == "fallback");
    CHECK(c["count_bits"] == 3);
    CHECK(c["index_bits"] == 3);
    for (const auto &f : c["fields"]) {
      CHECK(f["storage"].size() == 6);
      payload += f["width"].get<unsigned>() * 7;
    }
  }
  REQUIRE(crossing == expected);
  // With the default all every payload register resets; with minimal the registers without a
  // reset are exactly the FIFO slots and receive registers (six slots and one per field) and the
  // slots of every link and endpoint pipe.
  auto held_bits = [](const nlohmann::json &regs) {
    return std::accumulate(regs.begin(), regs.end(), 0U, [](unsigned sum, const auto &r) {
      return sum +
             (r["async_reset"].template get<bool>() ? 0U : r["width"].template get<unsigned>());
    });
  };
  CHECK(held_bits(cdc["registers"]) == 0);
  auto minimal = d;
  minimal.reset_coverage = nocloom::ResetCoverage::Minimal;
  const auto artifacts = public_artifacts(minimal);
  for (const auto &item : artifacts.map["network"]["storage"])
    if (const auto name = item["resource"].get<std::string>();
        name.starts_with("pipeline/") || name.find("/pipe/") != std::string::npos)
      payload += artifacts.storage_bits(name, "payload");
  CHECK(held_bits(artifacts.map["network"]["cdc"]["registers"]) == payload);
  // Reset release chains 2 + 2; count chains 3 * 2 * 2 (a -> b) and 4 * 2 * 4 (b -> a).
  CHECK(cdc["synchronizer_stages"].size() == 4 + 3 * 4 + 4 * 8);
  // Crossings carry the link word, else the narrower end word; the 256-bit external width of s1
  // is not its transport word.
  CHECK(data_bits(cut(x, "req/merge>branch")) == 32);
  CHECK(data_bits(cut(x, "req/branch>tail")) == 64);
  CHECK(data_bits(cut(x, "req/s1>leaf1")) == 32);
  const auto adapter = [&](const std::string &key, const std::string &hop) {
    return domain_of(x.entry("adapters", "name", "adapter/" + segment(x, key) + hop));
  };
  CHECK(adapter("req/merge>branch", "/link-in/0") == "a");
  CHECK(adapter("req/merge>branch", "/link-out/0") == "b");
  CHECK(adapter("req/branch>tail", "/0") == "b");
  CHECK(domain_of(x.resource("source/s1/read/return")) == "b");
  CHECK(domain_of(x.resource("burst_service/t1/write")) == "b");
  CHECK(domain_of(x.resource("graph/req/" + hw(x, "req", "merge") + ">node/" +
                             hw(x, "req", "branch"))) == "a");
  for (const auto *list : {"adapters", "resources"})
    for (const auto &entry : x.map["network"][list])
      CHECK((entry["clock_domain"] == "a" || entry["clock_domain"] == "b"));
}

TEST_CASE("Clock scopes distinguish reused request and response node names",
          "[exchange-clocking]") {
  auto d = placed();
  for (auto &n : d.topology->response.nodes)
    if (n.name == "merge")
      n.clock_domain = "b";
  const auto x = public_artifacts(d);
  const auto &objects = x.map["network"]["cdc"]["objects"];
  CHECK(objects["req/" + hw(x, "req", "merge")] == "a");
  CHECK(objects["rsp/" + hw(x, "rsp", "merge")] == "b");
  CHECK(domain_of(x.resource("graph/req/" + hw(x, "req", "merge") + ">node/" +
                             hw(x, "req", "branch"))) == "a");
  CHECK(domain_of(x.resource("graph/rsp/" + hw(x, "rsp", "merge") + ">node/" +
                             hw(x, "rsp", "branch"))) == "b");
}

TEST_CASE("A definition plans one network whatever the order of its lists", "[exchange-clocking]") {
  for (const auto &d :
       {graph_example_definition(), graph_clocked_definition(), graph_clocked_definition(true)}) {
    const auto plan = build_network_plan(d);
    REQUIRE(plan);
    CHECK(build_network_plan(d).value() == plan.value());
    auto reversed = d;
    std::ranges::reverse(reversed.initiators);
    std::ranges::reverse(reversed.targets);
    std::ranges::reverse(reversed.connectivity->pairs);
    for (auto *g : {&reversed.topology->request, &reversed.topology->response}) {
      std::ranges::reverse(g->nodes);
      std::ranges::reverse(g->links);
      std::ranges::reverse(g->entries);
      std::ranges::reverse(g->exits);
    }
    REQUIRE(reversed.initiators.front().name != d.initiators.front().name);
    const auto again = build_network_plan(reversed);
    REQUIRE(again);
    CHECK(again.value() == plan.value());
  }
}

// Each capability rule is named once and judged after planning: one definition breaking several
// rules hears about each, with configuration paths and no rule names.
TEST_CASE("Capability rules collect every violation of a planned network", "[exchange-clocking]") {
  std::set<std::string_view> ids;
  for (const auto &rule : capability_rules())
    CHECK(ids.insert(rule.id).second);
  auto d = placed();
  d.clock_domains[0].period_ps = 10000; // a timed, b untimed: both directions are partial
  d.clock_domains.push_back({"c", "clk_c", "reset_n", "test"}); // owns no endpoint or switch
  const auto plan = nocloom::make_plan(d);
  REQUIRE_FALSE(plan);
  std::multiset<std::string> codes;
  for (const auto &diagnostic : plan.diagnostics()) {
    codes.insert(diagnostic.code);
    for (const auto id : ids)
      CHECK(diagnostic.field.find(id) == std::string::npos);
  }
  CHECK(codes == std::multiset<std::string>{"NCL_CDC_PERIOD_PARTIAL", "NCL_CDC_PERIOD_PARTIAL",
                                            "NCL_EXCHANGE_CLOCK_ASSIGNMENT"});
  CHECK(build_network_plan(d).diagnostics().size() == 3);
}

TEST_CASE("Same-domain placement never allocates a crossing FIFO", "[exchange-clocking]") {
  auto d = placed();
  d.clock_domains.resize(1);
  d.clock_crossings.clear();
  d.synchronizer_stages.reset();
  for (auto &i : d.initiators)
    i.clock_domain = "a";
  for (auto &t : d.targets)
    t.clock_domain = "a";
  for (auto *g : {&d.topology->request, &d.topology->response})
    for (auto &n : g->nodes)
      n.clock_domain = "a";
  const auto x = public_artifacts(d);
  CHECK(x.map["network"]["cdc"]["cuts"].empty());
  // Only the reset release chain of a.
  CHECK(x.map["network"]["cdc"]["synchronizer_stages"].size() == 2);
  REQUIRE_FALSE(x.map["network"]["adapters"].empty());
  for (const auto &entry : x.map["network"]["adapters"])
    CHECK(entry["clock_domain"] == "a");
}

TEST_CASE("Twelve domains each own a crossing; names and stages are checked",
          "[exchange-clocking]") {
  const auto x = public_artifacts(graph_clocked_definition(true));
  REQUIRE(x.map["definition"]["clock_domains"].size() == 12);
  std::set<std::string> used;
  for (const auto &c : x.map["network"]["cdc"]["cuts"]) {
    used.insert(c["from_domain"].get<std::string>());
    used.insert(c["to_domain"].get<std::string>());
    CHECK(c["depth_source"] == "fallback");
    CHECK(c["synchronizer_stages"] == 3);
  }
  CHECK(used.size() == 12);
  auto d = placed();
  d.clock_domains[0].name = "bad/domain";
  CHECK(rejected_with(nocloom::validate(d), "NCL_NAME_INVALID"));
  d = placed();
  std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[0].options).synchronizer_stages = 5;
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_STAGES_UNSUPPORTED"));
  d = placed();
  d.synchronizer_stages = 1;
  CHECK(rejected_with(nocloom::validate(d), "NCL_CDC_STAGES_UNSUPPORTED"));
  d = placed();
  d.clock_crossings = {{"a", "b"}};
  CHECK(rejected_with(nocloom::validate(d), "NCL_SCHEMA_MISSING_FIELD"));
}

TEST_CASE("Crossing depth follows periods, stages and explicit pair overrides",
          "[exchange-clocking]") {
  auto d = placed();
  SECTION("one known period is rejected unless both directions are explicit") {
    d.clock_domains[1].period_ps = 1000;
    auto checked = nocloom::validate(d);
    REQUIRE_FALSE(checked);
    REQUIRE(checked.diagnostics().size() == 2); // a to b and b to a, once each
    REQUIRE(checked.diagnostics().front().code == "NCL_CDC_PERIOD_PARTIAL");
    REQUIRE(checked.diagnostics().front().field == "clock_domains[1].period_ps");
    d.clock_crossings = {{"a", "b", 4}};
    checked = nocloom::validate(d);
    REQUIRE(rejected_with(checked, "NCL_CDC_PERIOD_PARTIAL"));
    REQUIRE(checked.diagnostics().size() == 1);
    d.clock_crossings.push_back({"b", "a", 5});
    const auto x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{4U, std::string("explicit")});
    REQUIRE(depth_of(x, "b") == std::pair{5U, std::string("explicit")});
  }
  SECTION("both periods select the loop formula per direction") {
    // T_a = 1000, T_b = 3000. a->b (n=2): ceil(3 * 4000 / 3000) = 4;
    // b->a (n=4): ceil(5 * 4000 / 3000) = 7. Equal clocks: 2n + 2.
    d.clock_domains[0].period_ps = 1000;
    d.clock_domains[1].period_ps = 3000;
    auto x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{4U, std::string("formula")});
    REQUIRE(depth_of(x, "b") == std::pair{7U, std::string("formula")});
    d.clock_domains[1].period_ps = 1000;
    x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{6U, std::string("formula")});
    REQUIRE(depth_of(x, "b") == std::pair{10U, std::string("formula")});
    for (const auto &c : x.map["network"]["cdc"]["cuts"])
      CHECK(c["count_bits"] == (c["from_domain"] == "a" ? 3 : 4));
    // The global stages reach every crossing without its own: 3 here, so
    // a->b at 1:1 becomes 8 while b->a keeps 4 stages and 10 entries.
    d.synchronizer_stages = 3;
    x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{8U, std::string("formula")});
    REQUIRE(depth_of(x, "b") == std::pair{10U, std::string("formula")});
    // 7:5 with n=2 on both directions: ceil(3 * 12 / 7) = 6.
    d.synchronizer_stages = 2;
    d.clock_crossings.clear();
    d.clock_domains[0].period_ps = 7000;
    d.clock_domains[1].period_ps = 5000;
    x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{6U, std::string("formula")});
  }
  SECTION("an explicit pair depth applies to one direction only") {
    d.clock_crossings = {{"a", "b", 2}};
    d.synchronizer_stages.reset();
    const auto x = public_artifacts(d);
    REQUIRE(depth_of(x, "a") == std::pair{2U, std::string("explicit")});
    REQUIRE(depth_of(x, "b") == std::pair{6U, std::string("fallback")});
    for (const auto &c : x.map["network"]["cdc"]["cuts"])
      CHECK(c["count_bits"] == (c["from_domain"] == "a" ? 2 : 3));
    for (const auto &r : x.map["network"]["resources"])
      CHECK(r["capacity"] == (r["role"] != "crossing_fifo" ? 1 : r["clock_domain"] == "a" ? 2 : 6));
  }
}

TEST_CASE("A link without its own word crosses at the narrower word the slower end fills",
          "[exchange-clocking]") {
  // Equal or unknown frequencies take the narrower end word, whichever end it is.
  CHECK(edge_word(4, 8, std::nullopt, std::nullopt) == 4);
  CHECK(edge_word(16, 8, std::nullopt, std::nullopt) == 8);
  CHECK(edge_word(4, 8, 1000, std::nullopt) == 4);
  CHECK(edge_word(4, 8, 1000, 1000) == 4);
  CHECK(edge_word(16, 8, 3000, 3000) == 8);
  // A faster narrow end keeps the wider word: 16 bytes at period 2 against 8 bytes at period 1
  // match, so the 16-byte sender of a twice as fast 8-byte receiver crosses at 16. A slower
  // narrow end takes its own word.
  CHECK(edge_word(16, 8, 2000, 1000) == 16);
  CHECK(edge_word(16, 8, 1000, 2000) == 8);
  CHECK(edge_word(4, 8, 1000, 3000) == 8);
  CHECK(edge_word(4, 8, 3000, 1000) == 4);
  CHECK(edge_word(8, 8, 1000, 3000) == 8);

  // req/branch>tail runs from 16-byte branch in b to 8-byte tail in a; rsp/t1>merge from the
  // 4-byte target t1 in b to the 8-byte response merge in a. req/merge>branch keeps its 4-byte
  // link word whatever the periods.
  auto d = placed();
  const auto widths = [&] {
    const auto x = public_artifacts(d);
    return std::tuple{data_bits(cut(x, "req/branch>tail")), data_bits(cut(x, "rsp/t1>merge")),
                      data_bits(cut(x, "req/merge>branch"))};
  };
  SECTION("unknown periods") {
    CHECK(widths() == std::tuple{64U, 32U, 32U});
    // Only the narrow side adapts after a narrow crossing, in the receiver domain.
    const auto x = public_artifacts(d);
    CHECK(domain_of(x.entry("adapters", "name",
                            "adapter/" + segment(x, "rsp/t1>merge") + "/link-out/0")) == "a");
    CHECK(x.entry("adapters", "name", "adapter/" + segment(x, "rsp/t1>merge") + "/0") == nullptr);
  }
  SECTION("one period with explicit depths") {
    d.clock_domains[0].period_ps = 1000;
    d.clock_crossings = {{"a", "b", 4}, {"b", "a", 5}};
    CHECK(widths() == std::tuple{64U, 32U, 32U});
  }
  SECTION("equal periods") {
    d.clock_domains[0].period_ps = 2000;
    d.clock_domains[1].period_ps = 2000;
    CHECK(widths() == std::tuple{64U, 32U, 32U});
  }
  SECTION("a faster than b") {
    d.clock_domains[0].period_ps = 1000;
    d.clock_domains[1].period_ps = 2000;
    CHECK(widths() == std::tuple{128U, 32U, 32U});
  }
  SECTION("b faster than a") {
    d.clock_domains[0].period_ps = 2000;
    d.clock_domains[1].period_ps = 1000;
    CHECK(widths() == std::tuple{64U, 64U, 32U});
  }
}

TEST_CASE("Clocked graph verification rejects corrupted obligations and synchronizers",
          "[exchange-clocking]") {
  const auto h = lowered(placed());
  REQUIRE(h.plan.cuts.size() == 7);
  auto corrupted = h;
  std::erase_if(corrupted.plan.graph.waits, [](const auto &w) {
    return w.from.starts_with("cut/") && w.from.ends_with("/receive") &&
           w.kind == CrossWaitKind::Capacity;
  });
  REQUIRE_FALSE(verify_axi4_exchange_composition(corrupted));
  corrupted = h;
  corrupted.ir.synchronizer_stages.pop_back();
  REQUIRE_FALSE(verify_axi4_exchange_composition(corrupted));
  corrupted = h;
  // Clearing a payload slot's reset disagrees with the freshly lowered FIFO (all mode).
  {
    const auto store = std::ranges::find_if(corrupted.storage, [](const auto &s) {
      return s.resource.starts_with("cut/") && !s.payload.empty();
    });
    REQUIRE(store != corrupted.storage.end());
    auto &slot = corrupted.ir.states[corrupted.ir.values[store->payload.front().index].state.index];
    slot.reset = std::nullopt;
  }
  REQUIRE_FALSE(verify_axi4_exchange_composition(corrupted));
  corrupted = h;
  corrupted.ir.states.front().clock = h.bindings.at("domain/b.clock");
  REQUIRE_FALSE(verify_axi4_exchange_composition(corrupted));
}

TEST_CASE("Clocked graph obligations reject cycles and dangling waits", "[exchange-clocking]") {
  const auto h = lowered(placed());
  REQUIRE(order_cross_domain_graph(h.plan.graph));
  auto rejected = [](const ExchangeHardware &fault, const std::string &code) {
    REQUIRE_FALSE(order_cross_domain_graph(fault.plan.graph));
    CHECK(order_cross_domain_graph(fault.plan.graph).diagnostics().front().code == code);
    const auto status = verify_axi4_exchange_composition(fault);
    REQUIRE_FALSE(status);
    CHECK(status.diagnostics().front().code == code);
  };
  // Two distinct storages that wait on each other close a resource cycle.
  const auto &resources = h.plan.graph.resources;
  const auto first = std::ranges::find_if(
      resources, [](const auto &r) { return r.id.starts_with("source/") && r.storage == r.id; });
  REQUIRE(first != resources.end());
  const auto second = std::ranges::find_if(resources, [&](const auto &r) {
    return r.id.starts_with("burst_service/") && r.storage == r.id;
  });
  REQUIRE(second != resources.end());
  {
    auto fault = h;
    fault.plan.graph.waits.push_back(
        {first->id, second->id, "injected forward wait", CrossWaitKind::Capacity});
    fault.plan.graph.waits.push_back(
        {second->id, first->id, "injected backward wait", CrossWaitKind::Capacity});
    rejected(fault, "NCL_RESOURCE_CYCLE");
  }
  // A startup wait of a resource on itself is a one-node cycle.
  {
    auto fault = h;
    fault.plan.graph.waits.push_back(
        {first->id, first->id, "injected startup self wait", CrossWaitKind::Startup});
    rejected(fault, "NCL_RESOURCE_CYCLE");
  }
  // A wait that names an absent resource is a binding fault.
  {
    auto fault = h;
    fault.plan.graph.waits.push_back(
        {first->id, "absent/resource", "injected dangling wait", CrossWaitKind::Capacity});
    rejected(fault, "NCL_CDC_BINDING");
  }
}

TEST_CASE("Each domain resets from its own port or release chain", "[exchange-clocking]") {
  auto d = placed();
  // b resets straight from its port.
  d.reset_ports = {{"reset_a"}, {"reset_b", nocloom::ResetRelease::AllClocksOff}};
  d.test_ports = {{"test"}};
  d.clock_domains[0].reset_port = "reset_a";
  d.clock_domains[1].reset_port = "reset_b";
  const auto h = lowered(d);
  const auto local_a = h.bindings.at("domain/a.reset"), local_b = h.bindings.at("domain/b.reset");
  // a releases through two registers on its clock; b resets straight from its port.
  REQUIRE(local_b.index == h.bindings.at("domain/b.reset_port").index);
  REQUIRE(local_a.index != h.bindings.at("domain/a.reset_port").index);
  REQUIRE(h.ir.values.at(local_a.index).op == Op::State);
  for (const auto &q : h.ir.states)
    if (q.reset && q.clock.index == h.bindings.at("domain/b.clock").index)
      REQUIRE(q.reset->signal.index == local_b.index);
  auto corrupted = h;
  auto wrong = std::ranges::find_if(corrupted.ir.states, [&](const auto &q) {
    return q.reset && q.reset->signal.index == local_b.index;
  });
  REQUIRE(wrong != corrupted.ir.states.end());
  wrong->reset->signal = local_a;
  REQUIRE_FALSE(verify_axi4_exchange_composition(corrupted));
}

TEST_CASE("Divided, edge-qualified and ungate domain hardware verifies", "[exchange-clocking]") {
  auto state_of = [](ExchangeHardware &x, ValueId id) -> State & {
    return x.ir.states.at(x.ir.values.at(id.index).state.index);
  };
  auto rejected = [](const ExchangeHardware &x, const std::string &message) {
    const auto status = verify_axi4_exchange_composition(x);
    REQUIRE_FALSE(status);
    INFO(status.diagnostics().front().message);
    REQUIRE(status.diagnostics().front().message.find(message) != std::string::npos);
  };
  auto d = placed();
  d.enable_ports = {{"clk_en_a"}};
  d.ungate_ports = {{"scan_ug"}};
  d.clock_domains[0].clock_type = nocloom::ClockType::EdgeQualified;
  d.clock_domains[0].enable_port = "clk_en_a";
  d.clock_domains[0].ungate_port = "scan_ug";
  d.clock_domains[1].clock_type = nocloom::ClockType::InternalDivider;
  d.clock_domains[1].ratio = 4;
  d.clock_domains[1].ungate_port = "scan_ug";
  const auto h = lowered(d);
  // b divides its clock port by four: the counter runs on the port, resets to
  // ratio - 1 with the domain reset, and its zero opens the divider gate.
  const auto &gate_b = h.ir.values.at(h.bindings.at("domain/b.clock").index);
  REQUIRE(gate_b.op == Op::ClockGate);
  const auto &input_b = h.ir.values.at(gate_b.operands[0].index);
  REQUIRE(input_b.op == Op::Input);
  CHECK(h.ir.ports.at(input_b.input_port.index).name == "clk_b");
  const auto count = h.bindings.at("domain/b/divider.count");
  const auto &q = h.ir.states.at(h.ir.values.at(count.index).state.index);
  CHECK(h.ir.values.at(count.index).width == 2);
  CHECK(q.reset->value == 3);
  CHECK(q.clock.index == gate_b.operands[0].index);
  CHECK(q.reset->signal.index == h.bindings.at("domain/b.reset").index);
  // a gates its clock port by the enable port; every gate's enable ors the ungate port.
  const auto &gate_a = h.ir.values.at(h.bindings.at("domain/a.clock").index);
  REQUIRE(gate_a.op == Op::ClockGate);
  const auto &or_a = h.ir.values.at(gate_a.operands[1].index);
  REQUIRE(or_a.op == Op::BitOr);
  const auto &en_a = h.ir.values.at(or_a.operands[0].index);
  REQUIRE(en_a.op == Op::Input);
  CHECK(h.ir.ports.at(en_a.input_port.index).name == "clk_en_a");
  const auto &ug_a = h.ir.values.at(or_a.operands[1].index);
  REQUIRE(ug_a.op == Op::Input);
  CHECK(h.ir.ports.at(ug_a.input_port.index).name == "scan_ug");
  const auto &en_b = h.ir.values.at(gate_b.operands[1].index);
  REQUIRE(en_b.op == Op::BitOr);
  CHECK(en_b.operands[0].index == h.bindings.at("domain/b/divider.enable").index);
  const auto &ug_b = h.ir.values.at(en_b.operands[1].index);
  REQUIRE(ug_b.op == Op::Input);
  CHECK(h.ir.ports.at(ug_b.input_port.index).name == "scan_ug");
  const auto artifacts = nocloom::generate(d);
  REQUIRE(artifacts);
  const auto it = std::ranges::find_if(artifacts.value(),
                                       [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(it != artifacts.value().end());
  const auto domains = nlohmann::json::parse(it->text)["definition"]["clock_domains"];
  REQUIRE(domains.size() == 2);
  CHECK(domains[0].value("clock_type", "") == "edge_qualified");
  CHECK(domains[0].value("enable_port", "") == "clk_en_a");
  CHECK(domains[0].value("ungate_port", "") == "scan_ug");
  CHECK(domains[1].value("clock_type", "") == "internal_divider");
  CHECK(domains[1].value("ratio", 0) == 4);
  // The gate wrapper keeps its three pins; the ungate port joins the enable.
  const auto wrapper = std::ranges::find_if(
      artifacts.value(), [](const auto &a) { return a.path == "nocloom_clock_gate.v"; });
  REQUIRE(wrapper != artifacts.value().end());
  CHECK(wrapper->text.find("latched <= enable | test;") != std::string::npos);
  SECTION("the divider count runs on the clock port") {
    auto corrupted = h;
    state_of(corrupted, count).clock = h.bindings.at("domain/b.clock");
    rejected(corrupted, "static divider count runs on the clock port");
  }
  SECTION("the divider count restarts at ratio - 1") {
    auto corrupted = h;
    state_of(corrupted, count).reset->value = 2;
    rejected(corrupted, "static divider count runs on the clock port");
  }
  SECTION("the divider gate opens on the counter zero") {
    auto corrupted = h;
    const auto enable = corrupted.bindings.at("domain/b/divider.enable");
    corrupted.ir.values.at(corrupted.ir.values.at(enable.index).operands[1].index).literal = 1;
    rejected(corrupted, "static divider compares its count with zero");
  }
  SECTION("the divider count reloads its ratio") {
    auto corrupted = h;
    const auto next = state_of(corrupted, count).next;
    corrupted.ir.values.at(corrupted.ir.values.at(next.index).operands[1].index).literal = 2;
    rejected(corrupted, "static divider count reloads its ratio");
  }
  SECTION("the edge-qualified gate opens on the enable port") {
    auto corrupted = h;
    corrupted.ir.values.at(corrupted.bindings.at("domain/a.clock").index).operands[1] =
        h.bindings.at("domain/b/divider.enable");
    rejected(corrupted, "divider gate takes the ungate port of its domain");
  }
}

TEST_CASE("Clocked hardware verification rejects altered crossing FIFOs", "[exchange-clocking]") {
  auto d = placed();
  d.clock_domains[0].period_ps = 10000;
  d.clock_domains[1].period_ps = 25000;
  const auto h = lowered(d);
  const auto &cut = h.plan.cuts.front();
  auto state_of = [](ExchangeHardware &x, ValueId id) -> State & {
    return x.ir.states.at(x.ir.values.at(id.index).state.index);
  };
  auto rejected = [](const ExchangeHardware &x, const std::string &message) {
    const auto status = verify_axi4_exchange_composition(x);
    REQUIRE_FALSE(status);
    INFO(status.diagnostics().front().message);
    REQUIRE(status.diagnostics().front().message.find(message) != std::string::npos);
  };
  auto corrupted = h;
  SECTION("first count stage samples the index instead of the Gray count") {
    // The write count chain's first stage is the receiver state whose next is the count.
    const auto count = h.bindings.at(cut.name + "/write_count");
    auto stage = std::ranges::find_if(corrupted.ir.states, [&](const auto &q) {
      return q.next.index == count.index && q.clock.index != state_of(corrupted, count).clock.index;
    });
    REQUIRE(stage != corrupted.ir.states.end());
    stage->next = h.bindings.at(cut.name + "/read_count");
    rejected(corrupted, "FIFO storage update/reset/clock changed");
  }
  SECTION("receive register clocked by the sender") {
    const auto read = h.bindings.at(cut.name + "/read");
    auto received = std::ranges::find_if(corrupted.ir.states, [&](const auto &q) {
      const auto &next = corrupted.ir.values.at(q.next.index);
      return next.op == Op::Select && next.operands[0].index == read.index &&
             next.operands[2].index == q.output.index;
    });
    REQUIRE(received != corrupted.ir.states.end());
    received->clock = state_of(corrupted, h.bindings.at(cut.name + "/write_count")).clock;
    rejected(corrupted, "FIFO storage update/reset/clock changed");
  }
  SECTION("Gray count register loses its reset") {
    state_of(corrupted, h.bindings.at(cut.name + "/read_count")).reset.reset();
    rejected(corrupted, "FIFO storage update/reset/clock changed");
  }
  SECTION("full compares with a different depth") {
    // The only constant of the count width equal to the depth feeds full.
    const auto count = h.bindings.at(cut.name + "/write_count");
    const auto width = corrupted.ir.values.at(count.index).width;
    bool changed = false;
    for (std::size_t i = 0; i < h.ir.values.size() && !changed; ++i) {
      const auto &v = h.ir.values[i];
      if (v.op != Op::Equal)
        continue;
      const auto &k = h.ir.values.at(v.operands[1].index);
      const auto &lhs = h.ir.values.at(v.operands[0].index);
      if (k.op == Op::Constant && k.width == width &&
          k.literal == std::get<AsyncClockCut>(cut.options).depth.depth && lhs.op == Op::Subtract) {
        corrupted.ir.values[v.operands[1].index].literal =
            std::get<AsyncClockCut>(cut.options).depth.depth - 1;
        changed = true;
      }
    }
    REQUIRE(changed);
    rejected(corrupted, "FIFO constant changed");
  }
  SECTION("FIFO resource capacity differs from the depth") {
    auto fifo = std::ranges::find(corrupted.plan.graph.resources, cut.name, &CrossResource::id);
    REQUIRE(fifo != corrupted.plan.graph.resources.end());
    ++fifo->capacity;
    rejected(corrupted, "Resources or waits differ from a fresh build of the plan");
  }
  SECTION("map binding of the write count names another register") {
    corrupted.bindings[cut.name + "/write_count"] = h.bindings.at(cut.name + "/read_count");
    rejected(corrupted, "FIFO map binding differs from the lowered FIFO");
  }
  SECTION("planned explicit depths disagree with the crossing declarations") {
    corrupted.plan.explicit_depths[{cut.from_domain, cut.to_domain}] =
        std::get<AsyncClockCut>(cut.options).depth.depth == 2 ? 3 : 2;
    rejected(corrupted, "Crossing depth or count width differs from its declarations");
  }
  SECTION("planned depth disagrees with the declared periods") {
    // Keep the resource inventory consistent so the declaration check fires.
    std::get<AsyncClockCut>(corrupted.plan.cuts.front().options).depth.depth += 1;
    std::ranges::find(corrupted.plan.graph.resources, cut.name, &CrossResource::id)->capacity += 1;
    rejected(corrupted, "Crossing depth or count width differs from its declarations");
  }
}

TEST_CASE("Each switch of a gated domain gets one clock gate", "[exchange-clocking]") {
  // Sources in main, switches in middle, targets in target: only middle is gated.
  auto d = graph_clocked_definition();
  d.test_ports = {{"scan"}};
  for (auto &domain : d.clock_domains)
    if (domain.name == "middle")
      domain.test_port = "scan";
    else
      domain.gated = false, domain.test_port.reset();
  const auto h = lowered(d);
  std::set<std::string> gates, switches;
  for (const auto &[key, id] : h.bindings)
    if (key.starts_with("gate/") && key.ends_with(".clock"))
      gates.insert(key.substr(5, key.size() - 11));
  for (const auto &node : h.plan.nodes)
    switches.insert(node.id.substr(0, 3) + "/" + node.id);
  CHECK(gates == switches);
  for (const auto &gate : gates) {
    const auto remaining = h.bindings.at("gate/" + gate + ".remaining");
    const auto &value = h.ir.values.at(remaining.index);
    REQUIRE(value.op == Op::State);
    CHECK(value.width == 2);
    const auto &state = h.ir.states.at(value.state.index);
    REQUIRE(state.reset);
    CHECK(state.reset->value == 3);
    CHECK(state.clock.index == h.bindings.at("gate/" + gate + ".clock").index);
    auto cleared = h;
    cleared.ir.states.at(value.state.index).reset->value = 0;
    CHECK_FALSE(verify_axi4_exchange_composition(cleared));
  }
  // Every middle register is gated except the synchronizer stages (the release chain here).
  const auto middle = h.bindings.at("domain/middle.clock");
  std::set<std::size_t> stages;
  for (auto q : h.ir.synchronizer_stages)
    stages.insert(q.index);
  for (const auto &q : h.ir.states)
    if (root_clock(h.ir, q.clock).index == middle.index)
      CHECK((q.clock.index != middle.index) == !stages.contains(q.output.index));
  auto corrupted = h;
  const auto stage = std::ranges::find_if(corrupted.ir.states, [&](const State &q) {
    return q.clock.index == middle.index && stages.contains(q.output.index);
  });
  REQUIRE(stage != corrupted.ir.states.end());
  stage->clock = h.bindings.at("gate/" + *gates.begin() + ".clock");
  CHECK_FALSE(verify_axi4_exchange_composition(corrupted));
  // The public artifacts: the cell model leads the bundle sources; one map record per gate.
  const auto artifacts = nocloom::generate(d);
  REQUIRE(artifacts);
  auto file = [&](const std::string &suffix) {
    const auto it = std::ranges::find_if(artifacts.value(),
                                         [&](const auto &a) { return a.path.ends_with(suffix); });
    REQUIRE(it != artifacts.value().end());
    return it->text;
  };
  const auto bundle = nlohmann::json::parse(file(".bundle.json"));
  CHECK(file("nocloom_clock_gate.v").find("module nocloom_clock_gate (") != std::string::npos);
  for (const auto *key : {"synthesis_sources", "simulation_sources"})
    CHECK(bundle[key].front() == "nocloom_clock_gate.v");
  const auto map = nlohmann::json::parse(file(".map.json"));
  CHECK(map["network"]["clock_gates"].size() == gates.size());
  // Every domain gated: each endpoint gets a protocol and a transport gate, and every register
  // but the synchronizer stages runs on a gated clock.
  for (auto &domain : d.clock_domains)
    domain.gated = true, domain.test_port = "scan";
  const auto all = lowered(d);
  std::set<std::string> sides;
  for (const auto &[key, id] : all.bindings)
    if (key.starts_with("gate/") && key.ends_with(".clock") && !key.starts_with("gate/req/") &&
        !key.starts_with("gate/rsp/"))
      sides.insert(key.substr(5, key.size() - 11));
  std::set<std::string> expected;
  for (const auto &s : all.plan.sources)
    expected.insert({"source/" + s.name, "source/" + s.name + "/protocol"});
  for (const auto &t : all.plan.targets)
    expected.insert({"target/" + t.name, "target/" + t.name + "/protocol"});
  CHECK(sides == expected);
  std::set<std::size_t> ungated;
  for (auto q : all.ir.synchronizer_stages)
    ungated.insert(q.index);
  for (const auto &q : all.ir.states)
    CHECK((all.ir.values.at(q.clock.index).op == Op::ClockGate) !=
          ungated.contains(q.output.index));
  const auto gated = nocloom::generate(d);
  REQUIRE(gated);
  const auto gated_map =
      nlohmann::json::parse(std::ranges::find_if(gated.value(), [](const auto &a) {
                              return a.path.ends_with(".map.json");
                            })->text);
  CHECK(std::ranges::count_if(gated_map["network"]["clock_gates"], [](const auto &g) {
          return g.contains("zone");
        }) == static_cast<std::ptrdiff_t>(expected.size()));
}

TEST_CASE("A per-tag clamp's response readiness reads only its own response head",
          "[exchange-clocking]") {
  // A 64-bit source with several read IDs reaching a 32-bit target with several bursts in flight.
  auto d = placed();
  d.initiators[1].data_width = 64;
  d.initiators[1].id_width = 2;
  d.initiators[1].max_outstanding = 4;
  for (auto &target : d.targets) {
    target.data_width = 32;
    target.max_outstanding = 4;
  }
  const auto h = lowered(d);
  std::string flow;
  for (const auto &[name, id] : h.bindings)
    if (name.ends_with("/read.response_tag"))
      flow = name.substr(0, name.size() - std::string("/read.response_tag").size());
  REQUIRE_FALSE(flow.empty());
  const auto anchor = flow + "/read.response_ready";
  REQUIRE(h.bindings.contains(anchor));
  REQUIRE(verify_axi4_exchange_composition(h));
  // Without either head anchor the readiness reaches the storage on the response path.
  for (const auto *field : {"/read.response_tag", "/read.response_head_address"}) {
    auto corrupted = h;
    corrupted.bindings.erase(flow + field);
    CHECK_FALSE(verify_axi4_exchange_composition(corrupted));
  }
  // Any other state in the readiness fails with the head anchors in place.
  const auto with = [&](const std::function<bool(const std::string &)> &owner) {
    auto other = h;
    std::optional<ValueId> state;
    for (const auto &store : other.storage)
      if (owner(store.resource) && !store.control.empty())
        state = store.control.front();
    REQUIRE(state);
    Builder b;
    b.ir = std::move(other.ir);
    other.bindings[anchor] = b.binary(Op::BitAnd, other.bindings.at(anchor), *state);
    other.ir = std::move(b.ir);
    return other;
  };
  // Another flow's engine.
  CHECK_FALSE(verify_axi4_exchange_composition(with([&](const std::string &resource) {
    return resource.starts_with("source/") && resource.ends_with("/read") &&
           !resource.starts_with(flow);
  })));
  // A target's service state.
  CHECK_FALSE(verify_axi4_exchange_composition(
      with([](const std::string &resource) { return resource.starts_with("burst_service/"); })));
  // A head anchor that reads the readiness would close a combinational loop.
  auto looped = h;
  {
    Builder b;
    b.ir = std::move(looped.ir);
    looped.bindings[flow + "/read.response_tag"] =
        b.binary(Op::BitAnd, looped.bindings.at(anchor), looped.bindings.at(anchor));
    looped.ir = std::move(b.ir);
  }
  CHECK_FALSE(verify_axi4_exchange_composition(looped));
}

TEST_CASE("A clamp's walk of a response run reads only its own response head",
          "[exchange-clocking]") {
  auto d = placed();
  d.initiators[1].data_width = 256;
  d.address_width = 40;
  const auto h = lowered(d);
  std::string flow;
  for (const auto &[name, id] : h.bindings)
    if (name.ends_with("/read.response_head_count"))
      flow = name.substr(0, name.size() - std::string("/read.response_head_count").size());
  REQUIRE_FALSE(flow.empty());
  REQUIRE(verify_axi4_exchange_composition(h));
  // Without the reach's anchor the readiness reaches the storage on the response path.
  auto corrupted = h;
  corrupted.bindings.erase(flow + "/read.response_head_count");
  CHECK_FALSE(verify_axi4_exchange_composition(corrupted));
  // A reach that reads the readiness would close a combinational loop.
  auto looped = h;
  {
    Builder b;
    b.ir = std::move(looped.ir);
    const auto ready = looped.bindings.at(flow + "/read.response_ready");
    looped.bindings[flow + "/read.response_head_count"] = b.binary(Op::BitAnd, ready, ready);
    looped.ir = std::move(b.ir);
  }
  CHECK_FALSE(verify_axi4_exchange_composition(looped));
}

TEST_CASE("Clock audit restricts FIFO levels to their confirmed reduction", "[exchange-clocking]") {
  auto d = placed();
  d.initiators[0].qos.levels = {3, 1};
  const auto h = lowered(d);
  const auto &cut = h.plan.cuts.front();
  const auto prefix = "fifo_levels/" + cut.name + "/";
  auto corrupted = h;
  SECTION("a naked sender level cannot enter receiver state") {
    const auto held = h.bindings.at("level_hint/" + cut.name + "/forward");
    const auto slot = h.bindings.at("level_hint/" + cut.name + "/slot0");
    auto &q = corrupted.ir.states.at(corrupted.ir.values.at(held.index).state.index);
    q.next = slot;
    REQUIRE(verify_structure(corrupted.ir));
    const auto checked = verify_exchange_clock_hardware(corrupted);
    REQUIRE_FALSE(checked);
    CHECK(checked.diagnostics().front().message == "Unqualified register crossing");
  }
  SECTION("a reduction cannot publish every slot") {
    const auto active = corrupted.bindings.at(prefix + "active0");
    corrupted.ir.values.at(active.index) = {1, Op::Constant, {}, 1};
    REQUIRE(verify_structure(corrupted.ir));
    const auto checked = verify_exchange_clock_hardware(corrupted);
    REQUIRE_FALSE(checked);
    CHECK(checked.diagnostics().front().message == "FIFO confirmed level interval changed");
  }
  SECTION("slot levels must be written with the accepted fragment") {
    const auto slot = corrupted.bindings.at("level_hint/" + cut.name + "/slot0");
    auto &q = corrupted.ir.states.at(corrupted.ir.values.at(slot.index).state.index);
    q.next = slot;
    REQUIRE(verify_structure(corrupted.ir));
    const auto checked = verify_exchange_clock_hardware(corrupted);
    REQUIRE_FALSE(checked);
    CHECK(checked.diagnostics().front().message == "FIFO confirmed level interval changed");
  }
}
