#include "core/link_pipeline.hpp"
#include "../public_artifacts.hpp"
#include "core/cone_walk.hpp"
#include "core/exchange_composition.hpp"
#include "core/ir_cone.hpp"
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
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
  for (const auto mode : {LinkPipeline{PipeSlice::Forward}, LinkPipeline{PipeSlice::Backward}})
    CHECK(stages(public_artifacts(profile(mode)), 1) == 2);
  for (unsigned word : {4U, 8U, 16U, 32U}) {
    auto d = profile({PipeSlice::Full});
    for (auto *graph : {&d.topology->request, &d.topology->response})
      for (auto &node : graph->nodes)
        node.transport_word_bytes = word;
    CHECK(stages(public_artifacts(d), 2) == 2);
  }
}
TEST_CASE("Public link pipelines generate with and without stages", "[link-pipeline]") {
  auto d = profile({PipeSlice::Full});
  const auto staged = generate(d);
  REQUIRE(staged);
  d.topology->request.links[0].pipeline = {PipeSlice::Forward};
  d.topology->response.links[0].pipeline = {PipeSlice::Backward};
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
    graph->links[0].sender_pipeline = {PipeSlice::Forward};
    graph->links[0].pipeline = {PipeSlice::Backward};
    graph->entries[0].pipeline = graph->exits[0].pipeline = {PipeSlice::Full};
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
  d.initiators[0].pipes = {{PipeSlice::Full}, {PipeSlice::Forward}, true, {PipeSlice::Full}};
  d.targets[0].pipes = {{PipeSlice::Backward}, true, {PipeSlice::Full}, {PipeSlice::Full}};
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
  unregistered_r.initiators[0].pipes.response.slice = PipeSlice::Backward;
  unregistered_ar.targets[0].pipes.request.slice = PipeSlice::Backward;
  lite.initiators[0].protocol = Protocol::Axi4Lite;
  timed.targets[0].timeout = TargetTimeout{1024, false};
  // A response pipe on a non-AXI4 source must be rejected, not silently dropped.
  lite_response.initiators[0].protocol = Protocol::Axi4Lite;
  lite_response.initiators[0].pipes = {};
  lite_response.initiators[0].pipes.response = {PipeSlice::Full};
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
  auto half = profile({.slice = PipeSlice::Half});
  half.initiators[0].qos.levels = {.read = 2, .write = 3};
  const auto x = public_artifacts(half);
  CHECK(stages(x, 1) == 2);
  for (const auto &r : x.map["network"]["resources"]) {
    if (r["role"] != "link_pipeline")
      continue;
    CHECK(r["slice"] == "half");
    CHECK(r["capacity"] == 1);
  }

  auto bypass = profile({.slice = PipeSlice::Full, .bypass_port = "pipe_bypass"});
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
       {LinkPipeline{.slice = PipeSlice::Backward, .bypass_port = "pipe_bypass"},
        LinkPipeline{.bypass_port = "pipe_bypass"}, LinkPipeline{.ungated = true}})
    CHECK(rejected_with(generate(profile(mode)), "NCL_AXI4_TOPOLOGY"));

  auto collision = profile({.slice = PipeSlice::Forward, .bypass_port = "reset_n"});
  CHECK(rejected_with(generate(collision), "NCL_NAME_COLLISION"));
  for (bool target : {false, true}) {
    auto d = profile({});
    auto &pipe = target ? d.targets[0].pipes.arrival : d.initiators[0].pipes.packets;
    pipe = {.slice = PipeSlice::Half};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
    pipe = {.slice = PipeSlice::Forward, .bypass_port = "pipe_bypass"};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
    pipe = {.slice = PipeSlice::Forward, .ungated = true};
    CHECK(rejected_with(generate(d), "NCL_ENDPOINT_PIPE"));
  }
}

TEST_CASE("Ungated link pipes keep their state outside the switch gate", "[link-pipeline]") {
  auto plain = profile({.slice = PipeSlice::Forward});
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
}

TEST_CASE("Minimal keeps link pipe fields unreset and resets the flags", "[link-pipeline]") {
  const LinkPipeline half{PipeSlice::Half};
  for (const auto &mode : {LinkPipeline{PipeSlice::Forward}, LinkPipeline{PipeSlice::Backward},
                           LinkPipeline{PipeSlice::Full}, half}) {
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

namespace {
// The profile with a in main at 16 bytes and b in far at 8 bytes. The request link a>b passes a
// 4-byte link word: its sender positions pass 16, 8 and 4 bytes, its receiver positions 4 and 8.
// The response link b>a keeps an 8-byte word: sender 8, receiver 8 and 16.
Definition crossed() {
  auto d = profile({});
  d.clock_domains.push_back({"far", "clk_far", "reset_n", "test"});
  for (auto *graph : {&d.topology->request, &d.topology->response})
    for (auto &node : graph->nodes) {
      node.clock_domain = node.name == "a" ? "main" : "far";
      node.transport_word_bytes = node.name == "a" ? 16U : 8U;
    }
  auto &request = d.topology->request.links[0];
  request.transport_word_bytes = 4;
  request.sender_pipeline = {PipeSlice::Forward};
  request.pipeline = {PipeSlice::Backward};
  request.stages = {
      {.side = LinkSide::Receiver, .pipes = {{PipeSlice::Half}}},
      {.side = LinkSide::Sender,
       .word_bytes = 8,
       .clock_domain = "main",
       .pipes = {{PipeSlice::Full}}},
      {.side = LinkSide::Sender, .pipes = {{PipeSlice::Forward}, {PipeSlice::Backward}}},
      {.side = LinkSide::Receiver,
       .word_bytes = 4,
       .clock_domain = "far",
       .pipes = {{.slice = PipeSlice::Forward, .ungated = true}}}};
  d.topology->response.links[0].stages = {
      {.side = LinkSide::Sender, .pipes = {{PipeSlice::Full}, {PipeSlice::Full}}},
      {.side = LinkSide::Receiver, .word_bytes = 8, .pipes = {{PipeSlice::Forward}}},
      {.side = LinkSide::Receiver,
       .pipes = {{.slice = PipeSlice::Forward, .bypass_port = "stage_bypass"}}}};
  return d;
}
// The diagnostics of a failed generation with code at field.
std::vector<Diagnostic> found(const Result<ArtifactSet> &r, const std::string &code,
                              const std::string &field) {
  std::vector<Diagnostic> out;
  if (!r)
    std::ranges::copy_if(r.diagnostics(), std::back_inserter(out),
                         [&](const auto &x) { return x.code == code && x.field == field; });
  return out;
}
} // namespace
TEST_CASE("Stage lists place ordered pipes at link positions on both sides of a crossing",
          "[link-pipeline]") {
  const auto x = public_artifacts(crossed());
  const auto &records = x.map["network"]["link_stages"];
  REQUIRE(records.size() == 2);
  const auto &request = records[0];
  CHECK(request["link"] == "req/req_node0>req_node1");
  CHECK(request["from"] == "a");
  CHECK(request["to"] == "b");
  CHECK(request["sender_words"] == nlohmann::json{16, 8, 4});
  CHECK(request["receiver_words"] == nlohmann::json{4, 8});
  const std::string stem = "req/req_node0>req_node1";
  // name, kind, side, domain, word (pipes) and origin.
  using Row = std::tuple<std::string, std::string, std::string, std::string, unsigned, std::string>;
  const std::vector<Row> order{
      {"pipeline/" + stem + "/out", "pipe", "sender", "main", 16, "sender_pipeline"},
      {"pipeline/" + stem + "/sender/16/0", "pipe", "sender", "main", 16, "stages"},
      {"pipeline/" + stem + "/sender/16/1", "pipe", "sender", "main", 16, "stages"},
      {"adapter/" + stem + "/link-in/0", "serializer", "sender", "main", 0, ""},
      {"pipeline/" + stem + "/sender/8/0", "pipe", "sender", "main", 8, "stages"},
      {"adapter/" + stem + "/link-in/1", "serializer", "sender", "main", 0, ""},
      {"cut/" + stem, "crossing", "receiver", "far", 0, ""},
      {"pipeline/" + stem + "/receiver/4/0", "pipe", "receiver", "far", 4, "stages"},
      {"adapter/" + stem + "/link-out/0", "deserializer", "receiver", "far", 0, ""},
      {"pipeline/" + stem + "/receiver/8/0", "pipe", "receiver", "far", 8, "stages"},
      {"pipeline/" + stem, "pipe", "receiver", "far", 8, "pipeline"}};
  std::vector<Row> listed;
  std::ranges::transform(request["stages"], std::back_inserter(listed), [](const auto &s) {
    return Row{s["name"].template get<std::string>(),
               s["kind"].template get<std::string>(),
               s["side"].template get<std::string>(),
               s["clock_domain"].template get<std::string>(),
               s.value("word_bytes", 0U),
               s.value("origin", std::string())};
  });
  CHECK(listed == order);
  // The lists keep their order and their pipes their place, slots and options.
  const auto &rows = request["stages"];
  CHECK(rows[1]["list"] == 2);
  CHECK(rows[1]["index"] == 0);
  CHECK(rows[1]["slice"] == "forward");
  CHECK(rows[2]["index"] == 1);
  CHECK(rows[2]["slice"] == "backward");
  CHECK(rows[4]["slots"] == 2);
  CHECK(rows[7]["ungated"] == true);
  CHECK(rows[9]["slice"] == "half");
  CHECK(rows[9]["slots"] == 1);
  // Every listed pipe stores its slots of fragments at its word on its side's clock; a pipe
  // at the 4-byte link word stores fewer payload bits than one at 16 bytes.
  std::map<unsigned, unsigned> per_slot;
  for (const auto &s : rows) {
    if (s["kind"] != "pipe")
      continue;
    const auto name = s["name"].get<std::string>();
    const auto slots = s["slots"].get<unsigned>();
    CHECK(x.storage_bits(name, "control") == slots);
    CHECK((*x.storage(name))["clock"] == (s["side"] == "sender" ? "clk_main" : "clk_far"));
    const auto payload = x.storage_bits(name, "payload");
    CHECK(payload % slots == 0);
    per_slot[s["word_bytes"].get<unsigned>()] = payload / slots;
    CHECK(x.resource(name)->at("payload_bits") == payload / slots);
  }
  CHECK(per_slot.at(4) < per_slot.at(8));
  CHECK(per_slot.at(8) < per_slot.at(16));

  const auto &response = records[1];
  CHECK(response["sender_words"] == nlohmann::json{8});
  CHECK(response["receiver_words"] == nlohmann::json{8, 16});
  std::vector<std::string> names;
  std::ranges::transform(response["stages"], std::back_inserter(names),
                         [](const auto &s) { return s["name"].template get<std::string>(); });
  const std::string back = "rsp/rsp_node1>rsp_node0";
  CHECK(names == std::vector<std::string>{
                     "pipeline/" + back + "/sender/8/0", "pipeline/" + back + "/sender/8/1",
                     "cut/" + back, "pipeline/" + back + "/receiver/8/0",
                     "adapter/" + back + "/link-out/0", "pipeline/" + back + "/receiver/16/0"});
  CHECK(x.has_port("stage_bypass"));
  // The definition record keeps the lists as written.
  const auto &written = x.map["definition"]["topology"]["request"]["links"][0]["stages"];
  REQUIRE(written.size() == 4);
  CHECK(written[0] == nlohmann::json{{"side", "receiver"}, {"pipes", {{{"slice", "half"}}}}});
  CHECK(written[3]["clock_domain"] == "far");
  CHECK(written[3]["pipes"][0]["ungated"] == true);
}
TEST_CASE("YAML and C++ stage lists generate the same artifacts", "[link-pipeline]") {
  const auto d = crossed();
  const auto yaml = R"(schema_version: 10
name: pipe_public
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}
  - {name: far, clock_port: clk_far, reset_port: reset_n, test_port: test}
initiators: [{name: s, clock_domain: main, protocol: axi4, data_width: 32}]
targets:
  - {name: t, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
topology:
  request:
    nodes:
      - {name: a, clock_domain: main, transport_word_bytes: 16}
      - {name: b, clock_domain: far, transport_word_bytes: 8}
    links:
      - from: a
        to: b
        transport_word_bytes: 4
        sender_pipeline: {slice: forward}
        pipeline: {slice: backward}
        stages:
          - {side: receiver, pipes: [{slice: half}]}
          - {side: sender, word_bytes: 8, clock_domain: main, pipes: [{slice: full}]}
          - {side: sender, pipes: [{slice: forward}, {slice: backward}]}
          - {side: receiver, word_bytes: 4, clock_domain: far,
             pipes: [{slice: forward, ungated: true}]}
    entries: [{endpoint: s, node: a}]
    exits: [{endpoint: t, node: b}]
  response:
    nodes:
      - {name: a, clock_domain: main, transport_word_bytes: 16}
      - {name: b, clock_domain: far, transport_word_bytes: 8}
    links:
      - from: b
        to: a
        stages:
          - {side: sender, pipes: [{slice: full}, {slice: full}]}
          - {side: receiver, word_bytes: 8, pipes: [{slice: forward}]}
          - {side: receiver, pipes: [{slice: forward, bypass_port: stage_bypass}]}
    entries: [{endpoint: t, node: b}]
    exits: [{endpoint: s, node: a}]
)";
  const auto parsed = config::parse(yaml, "stages.yaml");
  REQUIRE(parsed);
  CHECK(parsed.value().definition.topology == d.topology);
  const auto from_yaml = generate(parsed.value().definition);
  const auto from_cpp = generate(d);
  REQUIRE(from_yaml);
  REQUIRE(from_cpp);
  CHECK(from_yaml.value() == from_cpp.value());
  // An empty list of stage lists changes nothing.
  auto bare = d;
  for (auto *graph : {&bare.topology->request, &bare.topology->response})
    graph->links[0].stages.clear();
  const auto x = public_artifacts(bare);
  CHECK_FALSE(x.map["network"].contains("link_stages"));
  CHECK_FALSE(x.map["definition"]["topology"]["request"]["links"][0].contains("stages"));
}
TEST_CASE("Stage list positions, clocks and options give field diagnostics", "[link-pipeline]") {
  const std::string at = "topology.request.links[0].stages";
  auto unknown = crossed();
  unknown.topology->request.links[0].stages[0].word_bytes = 16;
  auto r = generate(unknown);
  auto hits = found(r, "NCL_LINK_STAGE_POSITION", at + "[0].word_bytes");
  REQUIRE(hits.size() == 1);
  CHECK(hits[0].message == "The receiver side of this link has no position of 16-byte words. Its "
                           "positions pass 4, 8 bytes");
  // The switch end and its explicit word are one position.
  auto twice = crossed();
  twice.topology->request.links[0].stages.push_back(
      {.side = LinkSide::Sender, .word_bytes = 16, .pipes = {{PipeSlice::Forward}}});
  CHECK(found(generate(twice), "NCL_LINK_STAGE_POSITION", at + "[4]").size() == 1);
  auto clock = crossed();
  clock.topology->request.links[0].stages[3].clock_domain = "main";
  hits = found(generate(clock), "NCL_LINK_STAGE_CLOCK", at + "[3].clock_domain");
  REQUIRE(hits.size() == 1);
  CHECK(hits[0].message == "The position is in clock domain far, not main");
  for (const auto *name : {"nowhere", ""}) {
    auto undeclared = crossed();
    undeclared.topology->request.links[0].stages[1].clock_domain = name;
    CHECK(found(generate(undeclared), "NCL_LINK_STAGE_CLOCK", at + "[1].clock_domain").size() == 1);
  }
  auto ladder = crossed();
  ladder.topology->request.links[0].stages[1].word_bytes = 2;
  CHECK(found(generate(ladder), "NCL_LINK_STAGE_POSITION", at + "[1].word_bytes").size() == 1);
  auto side = crossed();
  side.topology->request.links[0].stages[1].side = static_cast<LinkSide>(2);
  CHECK(found(generate(side), "NCL_LINK_STAGE_POSITION", at + "[1].side").size() == 1);
  auto empty = crossed();
  empty.topology->request.links[0].stages[1].pipes.clear();
  CHECK(found(generate(empty), "NCL_LINK_STAGE_LIST", at + "[1].pipes").size() == 1);
  auto many = crossed();
  many.topology->request.links[0].stages[1].pipes.assign(65, {PipeSlice::Forward});
  CHECK(found(generate(many), "NCL_LINK_STAGE_LIST", at + "[1].pipes").size() == 1);
  many.topology->request.links[0].stages[1].pipes.resize(64);
  CHECK(generate(many));
  auto none = crossed();
  none.topology->request.links[0].stages[2].pipes[1] = {};
  CHECK(found(generate(none), "NCL_LINK_STAGE_LIST", at + "[2].pipes[1].slice").size() == 1);
  auto bypass = crossed();
  bypass.topology->request.links[0].stages[2].pipes[1].bypass_port = "stage_bypass";
  CHECK(found(generate(bypass), "NCL_AXI4_TOPOLOGY", at + "[2].pipes[1].bypass_port").size() == 1);
  auto collision = crossed();
  collision.topology->request.links[0].stages[2].pipes[0].bypass_port = "clk_far";
  CHECK(rejected_with(generate(collision), "NCL_NAME_COLLISION"));
}
TEST_CASE("YAML stage lists are strict and mirrors set them only by override", "[link-pipeline]") {
  const auto base = R"(schema_version: 10
name: mirrored
address_width: 32
reset_ports: [{name: reset_n}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, gated: false}]
initiators: [{name: s, clock_domain: main, protocol: axi4, data_width: 32}]
targets:
  - {name: t, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
topology:
  request:
    nodes: [{name: a}, {name: b}]
    links:
      - {from: a, to: b, stages: [@LIST@]}
    entries: [{endpoint: s, node: a}]
    exits: [{endpoint: t, node: b}]
  response: @RESPONSE@
)";
  const auto text = [&](const std::string &list, const std::string &response) {
    std::string out = base;
    out.replace(out.find("@LIST@"), 6, list);
    out.replace(out.find("@RESPONSE@"), 10, response);
    return out;
  };
  const std::string good = "{side: sender, pipes: [{slice: forward}]}";
  const auto mirrored = config::parse(text(good, "mirror"));
  REQUIRE(mirrored);
  CHECK(mirrored.value().definition.topology->response.links[0].stages.empty());
  const auto overridden = config::parse(
      text(good, "{mirror: {link_overrides: [{from: b, to: a, stages: [{side: receiver, pipes: "
                 "[{slice: full}]}]}]}}"));
  REQUIRE(overridden);
  CHECK(overridden.value().definition.topology->response.links[0].stages ==
        std::vector<LinkStages>{{.side = LinkSide::Receiver, .pipes = {{PipeSlice::Full}}}});
  for (const auto &[list, code, field] :
       std::vector<std::tuple<std::string, std::string, std::string>>{
           {"{side: middle, pipes: [{slice: forward}]}", "NCL_SCHEMA_TYPE", "[0].side"},
           {"{pipes: [{slice: forward}]}", "NCL_SCHEMA_MISSING_FIELD", "[0].side"},
           {"{side: sender}", "NCL_SCHEMA_MISSING_FIELD", "[0].pipes"},
           {"{side: sender, pipes: {slice: forward}}", "NCL_SCHEMA_TYPE", "[0].pipes"},
           {"{side: sender, word_bytes: '4', pipes: [{slice: forward}]}", "NCL_SCHEMA_TYPE",
            "[0].word_bytes"},
           {"{side: sender, pipes: [{slice: forward, depth: 2}]}", "NCL_SCHEMA_UNKNOWN_FIELD",
            "[0].pipes[0].depth"},
           {"{side: sender, order: 1, pipes: [{slice: forward}]}", "NCL_SCHEMA_UNKNOWN_FIELD",
            "[0].order"},
           {"{side: sender, pipes: [{slice: none}]}", "NCL_AXI4_TOPOLOGY", "[0].pipes[0].slice"}}) {
    const auto r = config::parse(text(list, "mirror"));
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == code);
    CHECK(r.diagnostics().front().field == "topology.request.links[0].stages" + field);
  }
  // A plan diagnostic points at its YAML field.
  const auto wrong =
      config::parse(text("{side: sender, word_bytes: 8, pipes: [{slice: full}]}", "mirror"));
  REQUIRE(wrong);
  const auto r = generate(wrong.value().definition, {}, &wrong.value().sources);
  const auto hits =
      found(r, "NCL_LINK_STAGE_POSITION", "topology.request.links[0].stages[0].word_bytes");
  REQUIRE(hits.size() == 1);
  REQUIRE(hits[0].location);
  CHECK(hits[0].location->line == 16);
}
TEST_CASE("Each listed pipe cuts the combinational paths of its slice", "[link-pipeline]") {
  const auto d = crossed();
  const auto h = lowered_pipe(d);
  // Whether value `to` reaches `from` without passing a register.
  const auto through = [&](const std::string &to, const std::string &from) {
    const auto target = h.bindings.at(from).index;
    bool reached = false;
    detail::walk_cone(
        h.ir, h.bindings.at(to), [](detail::ValueId) {}, [](detail::ValueId) {},
        [&](detail::ValueId v) {
          reached = reached || v.index == target;
          return reached;
        });
    return reached;
  };
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  unsigned checked = 0;
  for (const auto &link : plan.value().links)
    for (const auto &stage : link.stages) {
      if (!stage.listed)
        continue;
      ++checked;
      const auto &id = stage.name;
      const auto slice = stage.pipe.slice;
      // A static bypass input may restore the forward path; the check covers the others.
      const bool forward = slice != PipeSlice::Backward && stage.pipe.bypass_port.empty();
      const bool backward = slice != PipeSlice::Forward;
      CHECK(through(id + ".output.valid", id + ".input.valid") == !forward);
      CHECK(through(id + ".output.field0", id + ".input.field0") == !forward);
      CHECK(through(id + ".input.ready", id + ".output.ready") == !backward);
    }
  CHECK(checked == 9);
  // A list whose pipes register both directions cuts both paths from end to end: the sender list
  // at 16 bytes is forward then backward.
  const std::string stem = "pipeline/req/req_node0>req_node1/sender/16/";
  CHECK_FALSE(through(stem + "1.output.valid", stem + "0.input.valid"));
  CHECK_FALSE(through(stem + "0.input.ready", stem + "1.output.ready"));
}
