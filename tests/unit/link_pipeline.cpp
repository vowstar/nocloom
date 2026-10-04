#include "core/link_pipeline.hpp"
#include "../public_artifacts.hpp"
#include "core/exchange_composition.hpp"
#include "core/ir_cone.hpp"
#include <catch2/catch_test_macros.hpp>
#include <nocloom/generation.hpp>
using namespace nocloom;
TEST_CASE("Pressure follows connected positions and samples each register boundary",
          "[link-pipeline]") {
  using namespace nocloom::detail;
  Builder b;
  const auto clk = b.input("clk", 1, "main", "clock");
  const auto rst = b.input("rst", 1, "main", "reset_n");
  const auto hint = b.input("hint", 3, "in", "pressure");
  const auto ready = b.input("ready", 1, "in", "ready");
  const auto active = b.input("active", 3, "in", "active");
  const auto open = b.input("open", 1, "in", "open");
  const PressurePath path{b, 3};
  const std::vector<PressurePosition> positions{
      {{b.constant(3, 1), b.slice(active, 0, 1)}, open, false},
      {{b.constant(3, 2), b.slice(active, 1, 1)}, b.constant(1, 0), true},
      {{b.constant(3, 4), b.slice(active, 2, 1)}, b.constant(1, 1), true}};
  const auto sample = [&](ValueId next) {
    const auto q = b.state(3, clk, rst);
    b.update(q, next);
    return q;
  };
  const auto direct = path.follow(hint, ready, {}, sample);
  CHECK(b.ir.states.empty());
  const auto output = path.follow(hint, ready, positions, sample);
  REQUIRE(b.ir.states.size() == 2);
  struct Cycle {
    unsigned hint, ready, active, open, output, first_next, second_next;
  };
  // Local demands cross two boundaries independently, including a bubble and an open link.
  const std::vector<Cycle> cycles{{0, 1, 7, 0, 4, 1, 2}, {7, 0, 7, 0, 6, 7, 3},
                                  {0, 1, 6, 0, 7, 0, 7}, {0, 1, 7, 1, 7, 0, 2},
                                  {0, 1, 5, 0, 6, 1, 0}, {0, 1, 7, 0, 4, 1, 3}};
  ConeEvaluator evaluate(b.ir);
  ConeEvaluator::Words states;
  for (const auto &cycle : cycles) {
    const ConeEvaluator::Words inputs{{b.ir.values[hint.index].input_port.index, cycle.hint},
                                      {b.ir.values[ready.index].input_port.index, cycle.ready},
                                      {b.ir.values[active.index].input_port.index, cycle.active},
                                      {b.ir.values[open.index].input_port.index, cycle.open}};
    CHECK(evaluate(direct.index, states, inputs) == cycle.hint);
    CHECK(evaluate(output.index, states, inputs) == cycle.output);
    const auto first = evaluate(b.ir.states[0].next.index, states, inputs);
    const auto second = evaluate(b.ir.states[1].next.index, states, inputs);
    REQUIRE(first == cycle.first_next);
    REQUIRE(second == cycle.second_next);
    states = {{0, *first}, {1, *second}};
  }
}
namespace {
// One source and one target over a two-switch graph whose only internal links, req a>b and
// rsp b>a, carry the given stages.
Definition profile(LinkPipeline mode) {
  Definition d;
  d.schema_version = 10;
  d.name = "pipe_public";
  d.connectivity = Connectivity{.all = true};
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s", "main"}};
  d.targets = {{"t", "main", {{0, 4096}}}};
  d.initiators[0].protocol = Protocol::Axi4;
  d.initiators[0].data_width = 32;
  d.targets[0].protocol = Protocol::Axi4;
  d.targets[0].attributes = TargetAttributes::amba();
  d.targets[0].data_width = 32;
  d.topology = ExchangeTopology{
      {{{"a"}, {"b"}}, {{"a", "b", std::nullopt, mode}}, {{"s", "a"}}, {{"t", "b"}}},
      {{{"a"}, {"b"}}, {{"b", "a", std::nullopt, mode}}, {{"t", "b"}}, {{"s", "a"}}}};
  return d;
}
// Every link stage resource of a generation: one slot flag per stage (full forward, spare
// backward) and one fragment of payload per slot.
unsigned stages(const PublicArtifacts &x, unsigned slots) {
  unsigned count = 0;
  for (const auto &r : x.map["network"]["resources"]) {
    if (r["role"] != "link_pipeline")
      continue;
    ++count;
    const auto name = r["name"].get<std::string>();
    CHECK(x.storage_bits(name, "control") == slots);
    CHECK(x.storage_bits(name, "payload") == slots * r["payload_bits"].get<unsigned>());
  }
  return count;
}
detail::ExchangeHardware lowered_pipe(const Definition &d) {
  REQUIRE(validate(d));
  auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  auto result = detail::lower_axi4_exchange_composition(plan.value());
  REQUIRE(result);
  return result.value();
}
} // namespace
TEST_CASE("Link stages store one fragment per slot in both directions", "[link-pipeline]") {
  for (const auto mode : {LinkPipeline{true, false}, LinkPipeline{false, true}})
    CHECK(stages(public_artifacts(profile(mode)), 1) == 2);
  for (unsigned word : {4U, 8U, 16U, 32U}) {
    auto d = profile({true, true});
    for (auto *graph : {&d.topology->request, &d.topology->response})
      for (auto &node : graph->nodes)
        node.transport_word_bytes = word;
    CHECK(stages(public_artifacts(d), 2) == 2);
  }
}
TEST_CASE("Public link pipelines generate with and without stages", "[link-pipeline]") {
  auto d = profile({true, true});
  const auto staged = generate(d);
  REQUIRE(staged);
  d.topology->request.links[0].pipeline = {true, false};
  d.topology->response.links[0].pipeline = {false, true};
  const auto single = generate(d);
  REQUIRE(single);
  CHECK(staged.value() != single.value());
  d.topology->request.links[0].pipeline = {};
  d.topology->response.links[0].pipeline = {};
  const auto direct = generate(d);
  REQUIRE(direct);
  CHECK(staged.value() != direct.value());
  CHECK(single.value() != direct.value());
}
TEST_CASE("Link pipes sit at each switch on either side of a crossing", "[link-pipeline]") {
  // a in main, b in far: each internal link has a sender pipe before its crossing and a
  // receiver pipe after it; every attachment pipe runs with its switch.
  auto d = profile({});
  d.clock_domains.push_back({"far", "clk_far", "reset_n", "test"});
  for (auto *graph : {&d.topology->request, &d.topology->response}) {
    for (auto &node : graph->nodes)
      node.clock_domain = node.name == "a" ? "main" : "far";
    graph->links[0].sender_pipeline = {true, false};
    graph->links[0].pipeline = {false, true};
    graph->entries[0].pipeline = graph->exits[0].pipeline = {true, true};
  }
  const auto x = public_artifacts(d);
  std::map<std::string, std::string> clocks;
  for (const auto &s : x.map["network"]["storage"])
    if (const auto name = s["resource"].get<std::string>(); name.starts_with("pipeline/"))
      clocks[name] = s["clock"];
  const std::map<std::string, std::string> placed{
      {"pipeline/req/req_node0>req_node1", "clk_far"},
      {"pipeline/req/req_node0>req_node1/out", "clk_main"},
      {"pipeline/req/req_node1>t/out", "clk_far"},
      {"pipeline/req/s>req_node0", "clk_main"},
      {"pipeline/rsp/rsp_node0>s/out", "clk_main"},
      {"pipeline/rsp/rsp_node1>rsp_node0", "clk_main"},
      {"pipeline/rsp/rsp_node1>rsp_node0/out", "clk_far"},
      {"pipeline/rsp/t>rsp_node1", "clk_far"}};
  CHECK(clocks == placed);
}
TEST_CASE("Endpoint pipes store one word per slot in their endpoint's domain", "[link-pipeline]") {
  // s and both switches in main, t in far: the pipes of s run on clk_main, those of t on
  // clk_far; each holds one word of its payload per slot.
  auto d = profile({});
  d.clock_domains.push_back({"far", "clk_far", "reset_n", "test"});
  d.targets[0].clock_domain = "far";
  for (auto *graph : {&d.topology->request, &d.topology->response})
    for (auto &node : graph->nodes)
      node.clock_domain = "main";
  // The forward stages of the source's R and B and the target's AW, W and AR are the endpoints'
  // own registers: only the backward pipes there are pipes of their own.
  d.initiators[0].pipes = {{true, true}, {true, false}, true, {true, true}};
  d.targets[0].pipes = {{false, true}, true, {true, true}, {true, true}};
  const auto x = public_artifacts(d);
  std::map<std::string, std::pair<std::string, unsigned>> placed;
  for (const auto &r : x.map["network"]["resources"]) {
    if (r["role"] != "endpoint_pipeline")
      continue;
    const auto name = r["name"].get<std::string>();
    const auto slots = x.storage_bits(name, "control");
    CHECK(x.storage_bits(name, "payload") == slots * r["payload_bits"].get<unsigned>());
    placed[name] = {(*x.storage(name))["clock"], slots};
  }
  const std::map<std::string, std::pair<std::string, unsigned>> expected{
      {"source/s/pipe/aw", {"clk_main", 2}},
      {"source/s/pipe/w", {"clk_main", 2}},
      {"source/s/pipe/ar", {"clk_main", 2}},
      {"source/s/pipe/r", {"clk_main", 1}},
      {"source/s/pipe/b", {"clk_main", 1}},
      {"source/s/pipe/packets/write", {"clk_main", 1}},
      {"source/s/pipe/packets/read", {"clk_main", 1}},
      {"source/s/pipe/issue", {"clk_main", 1}},
      {"target/t/pipe/arrival", {"clk_far", 1}},
      {"target/t/pipe/dispatch", {"clk_far", 1}},
      {"target/t/pipe/aw", {"clk_far", 1}},
      {"target/t/pipe/w", {"clk_far", 1}},
      {"target/t/pipe/ar", {"clk_far", 1}},
      {"target/t/pipe/response", {"clk_far", 2}}};
  CHECK(placed == expected);
  // AXI channel pipes need AXI4 ports and an outlet without a timer, and the forward stages that
  // are endpoint registers stay on.
  auto lite = d, timed = d, lite_response = d, unregistered_r = d, unregistered_ar = d;
  unregistered_r.initiators[0].pipes.response.forward = false;
  unregistered_ar.targets[0].pipes.request.forward = false;
  lite.initiators[0].protocol = Protocol::Axi4Lite;
  timed.targets[0].timeout = TargetTimeout{1024, false};
  // A response pipe on a non-AXI4 source must be rejected, not silently dropped.
  lite_response.initiators[0].protocol = Protocol::Axi4Lite;
  lite_response.initiators[0].pipes = {};
  lite_response.initiators[0].pipes.response = {true, true};
  for (const auto &definition : {lite, timed, lite_response, unregistered_r, unregistered_ar}) {
    const auto r = generate(definition);
    REQUIRE_FALSE(r);
    CHECK(std::ranges::any_of(r.diagnostics(),
                              [](const auto &x) { return x.code == "NCL_ENDPOINT_PIPE"; }));
  }
}
TEST_CASE("AXI4 ports keep their readiness registered by default", "[link-pipeline]") {
  // Default pipes: a backward pipe on each of an AXI4 source's AW, W and AR, and on B of a
  // write-only AXI4 target with several bursts in flight (its BREADY looks for the slot of BID;
  // other targets park such a B in their outlet).
  const auto pipes = [](const Definition &d) {
    std::map<std::string, unsigned> slots;
    const auto x = public_artifacts(d);
    for (const auto &r : x.map["network"]["resources"])
      if (r["role"] == "endpoint_pipeline")
        slots[r["name"]] = x.storage_bits(r["name"], "control");
    return slots;
  };
  auto d = profile({});
  d.targets[0].max_outstanding = 4;
  CHECK(pipes(d) == std::map<std::string, unsigned>{
                        {"source/s/pipe/aw", 1}, {"source/s/pipe/w", 1}, {"source/s/pipe/ar", 1}});
  auto one = d, read_only = d, lite = d;
  one.targets[0].max_outstanding = 1;
  read_only.targets[0] = one.targets[0];
  read_only.targets[0].access = Access::ReadOnly;
  read_only.initiators[0].access = Access::ReadOnly;
  lite.initiators[0].protocol = Protocol::Axi4Lite;
  lite.targets[0].max_outstanding = 1;
  CHECK(pipes(one).size() == 3);
  CHECK(pipes(read_only) == std::map<std::string, unsigned>{{"source/s/pipe/ar", 1}});
  auto write_only = read_only;
  write_only.targets[0].access = Access::WriteOnly;
  write_only.initiators[0].access = Access::WriteOnly;
  CHECK(pipes(write_only) ==
        std::map<std::string, unsigned>{{"source/s/pipe/aw", 1}, {"source/s/pipe/w", 1}});
  write_only.targets[0].max_outstanding = 4;
  CHECK(pipes(write_only) == std::map<std::string, unsigned>{{"source/s/pipe/aw", 1},
                                                             {"source/s/pipe/w", 1},
                                                             {"target/t/pipe/b", 1}});
  CHECK(pipes(lite).empty());
}

TEST_CASE("Half-rate and static bypass are public link pipeline modes", "[link-pipeline]") {
  auto half = profile({.forward = true, .backward = true, .half_rate = true});
  half.initiators[0].qos.levels = {.read = 2, .write = 3};
  const auto x = public_artifacts(half);
  CHECK(stages(x, 1) == 2);
  for (const auto &r : x.map["network"]["resources"]) {
    if (r["role"] != "link_pipeline")
      continue;
    CHECK(r["half_rate"] == true);
    CHECK(r["capacity"] == 1);
  }

  auto bypass = profile({.forward = true, .backward = true, .bypass_port = "pipe_bypass"});
  const auto y = public_artifacts(bypass);
  CHECK(stages(y, 2) == 2);
  CHECK(y.has_port("pipe_bypass"));
  CHECK(y.port_width("pipe_bypass") == 1);
  for (const auto &r : y.map["network"]["resources"]) {
    if (r["role"] != "link_pipeline")
      continue;
    CHECK(r["bypass_port"] == "pipe_bypass");
    CHECK(r["bypass_forward"] == true);
    CHECK(r["capacity"] == 2);
  }
  for (const auto &p : y.map["ports"])
    if (p["name"] == "pipe_bypass")
      CHECK(p["clock"] == "");
}

TEST_CASE("Extended link pipe combinations are checked", "[link-pipeline]") {
  for (const auto &mode :
       {LinkPipeline{.forward = true, .half_rate = true},
        LinkPipeline{.backward = true, .half_rate = true}, LinkPipeline{.half_rate = true},
        LinkPipeline{.backward = true, .bypass_port = "pipe_bypass"},
        LinkPipeline{.bypass_port = "pipe_bypass"}})
    CHECK(rejected_with(generate(profile(mode)), "NCL_AXI4_TOPOLOGY"));

  auto collision = profile({.forward = true, .bypass_port = "reset_n"});
  CHECK(rejected_with(generate(collision), "NCL_NAME_COLLISION"));
  for (bool target : {false, true}) {
    auto d = profile({});
    auto &pipe = target ? d.targets[0].pipes.arrival : d.initiators[0].pipes.packets;
    pipe = {.forward = true, .backward = true, .half_rate = true};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
    pipe = {.forward = true, .bypass_port = "pipe_bypass"};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
    pipe = {.forward = true, .ungated = true};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
  }
}

TEST_CASE("Ungated link pipes keep their state outside the switch gate", "[link-pipeline]") {
  auto plain = profile({.forward = true});
  for (auto *graph : {&plain.topology->request, &plain.topology->response})
    for (auto &node : graph->nodes)
      node.clock_domain = "main";
  auto ungated = plain;
  for (auto *graph : {&ungated.topology->request, &ungated.topology->response})
    graph->links[0].pipeline.ungated = true;
  const auto after = public_artifacts(ungated);
  for (const auto &r : after.map["network"]["resources"]) {
    if (r["role"] != "link_pipeline")
      continue;
    CHECK(r["ungated"] == true);
    CHECK(r["capacity"] == 1);
  }

  const auto h = lowered_pipe(ungated);
  const auto domain = h.bindings.at("domain/main.clock");
  const auto state_clock = [&](const detail::ExchangeHardware &x, detail::ValueId q) {
    return x.ir.states.at(x.ir.values.at(q.index).state.index).clock;
  };
  for (const auto &s : h.storage) {
    if (!s.resource.starts_with("pipeline/"))
      continue;
    CHECK(s.clock.index == domain.index);
    for (const auto *part : {&s.control, &s.payload})
      for (const auto q : *part)
        CHECK(state_clock(h, q).index == domain.index);
  }
  const auto normal = lowered_pipe(plain);
  const auto &pipe = *std::ranges::find_if(
      normal.storage, [](const auto &s) { return s.resource.starts_with("pipeline/"); });
  CHECK(state_clock(normal, pipe.control.front()).index != domain.index);

  auto corrupted = h;
  auto &slot = *std::ranges::find_if(
      corrupted.storage, [](const auto &s) { return s.resource.starts_with("pipeline/"); });
  const auto gate = std::ranges::find_if(corrupted.bindings, [](const auto &binding) {
    return binding.first.starts_with("gate/") && binding.first.ends_with(".clock");
  });
  REQUIRE(gate != corrupted.bindings.end());
  corrupted.ir.states.at(corrupted.ir.values.at(slot.control.front().index).state.index).clock =
      gate->second;
  CHECK_FALSE(verify_axi4_exchange_composition(corrupted));

  auto empty = profile({.ungated = true});
  const auto direct = public_artifacts(empty);
  CHECK(direct.resource("pipeline/req/req_node0>req_node1") == nullptr);
  CHECK(direct.resource("pipeline/rsp/rsp_node1>rsp_node0") == nullptr);
}

TEST_CASE("Minimal keeps link pipe fields unreset and resets the flags", "[link-pipeline]") {
  LinkPipeline half{true, true};
  half.half_rate = true;
  for (const auto &mode :
       {LinkPipeline{true, false}, LinkPipeline{false, true}, LinkPipeline{true, true}, half}) {
    for (const auto registers : {ResetCoverage::All, ResetCoverage::Minimal}) {
      auto d = profile(mode);
      d.reset_coverage = registers;
      const auto artifacts = public_artifacts(d);
      // Only a reset branch assigns a register from a literal.
      const auto has_reset = [&](const nlohmann::json &value) {
        const auto assign = "nl_internal_v" + value.get<std::string>().substr(6) + " <= ";
        const auto at = artifacts.rtl.find(assign);
        REQUIRE(at != std::string::npos);
        return std::isdigit(static_cast<unsigned char>(artifacts.rtl[at + assign.size()])) != 0;
      };
      unsigned pipes = 0;
      for (const auto &item : artifacts.map["network"]["storage"]) {
        if (!item["resource"].get<std::string>().starts_with("pipeline/"))
          continue;
        ++pipes;
        for (const auto &flag : item["control"])
          CHECK(has_reset(flag));
        REQUIRE_FALSE(item["payload"].empty());
        for (const auto &slot : item["payload"])
          CHECK(has_reset(slot) == (registers == ResetCoverage::All));
      }
      CHECK(pipes == 2);
    }
  }
}
