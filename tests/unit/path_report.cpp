#include "core/path_report.hpp"
#include "core/exchange_composition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <fstream>
#include <functional>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <numeric>
#include <random>
#include <set>
#include <sstream>

using namespace nocloom;
using Json = nlohmann::json;
namespace {
std::string example(const std::string &name) {
  std::ifstream input(std::string(NOCLOOM_SOURCE_DIR) + "/examples/" + name + ".yaml");
  REQUIRE(input);
  std::stringstream text;
  text << input.rdbuf();
  return text.str();
}
ParsedDefinition parsed(const std::string &text) {
  auto p = config::parse(text, "test.yaml");
  if (!p)
    FAIL(p.diagnostics().front().field + ": " + p.diagnostics().front().message);
  return p.value();
}
// The request link merge -> branch of axi4_graph: 32-byte words narrowed to the 4-byte link word
// and widened to 16 bytes (sender positions 32, 16, 8, 4, receiver positions 4, 8, 16).
ExchangeLink &merge_branch(Definition &d) {
  auto &links = d.topology->request.links;
  return *std::ranges::find_if(
      links, [](const ExchangeLink &l) { return l.from == "merge" && l.to == "branch"; });
}
Json report_of(const Definition &d) {
  GenerationOptions options;
  options.path_report = true;
  auto artifacts = generate(d, options);
  if (!artifacts)
    FAIL(artifacts.diagnostics().front().field + ": " + artifacts.diagnostics().front().message);
  const auto it = std::ranges::find_if(
      artifacts.value(), [](const Artifact &a) { return a.path.ends_with(".path_report.json"); });
  REQUIRE(it != artifacts.value().end());
  return Json::parse(it->text);
}
std::vector<Diagnostic> plan_failure(const Definition &d) {
  auto plan = make_plan(d);
  REQUIRE(plan);
  auto planned = plan_link_stages(plan.value());
  REQUIRE_FALSE(planned);
  return planned.diagnostics();
}
bool has(const std::vector<Diagnostic> &ds, const std::string &code, const std::string &field) {
  return std::ranges::any_of(
      ds, [&](const Diagnostic &d) { return d.code == code && d.field == field; });
}
// Delays for every span of merge -> branch, uniform per kind, with roomy end allowances.
LinkDelays uniform(std::uint64_t adapter, std::uint64_t wire) {
  LinkDelays l{LinkGraph::Request, "merge", "branch"};
  for (unsigned w : {32U, 16U, 8U, 4U})
    l.spans.push_back({LinkSide::Sender, w, w == 4 ? wire : adapter, w == 4 ? wire : adapter,
                       w == 4 ? std::nullopt : std::optional<std::uint64_t>{adapter}});
  for (unsigned w : {4U, 8U})
    l.spans.push_back({LinkSide::Receiver, w, adapter, adapter, adapter});
  l.allowances.sender = {300, 300, 300};
  l.allowances.receiver = {300, 300, std::nullopt};
  return l;
}
LinkTiming timing(std::vector<LinkDelays> links, std::uint64_t period = 1000) {
  LinkTiming t;
  t.periods = {{"main", period}};
  t.stage = {40, 30, 20, 10};
  t.links = std::move(links);
  return t;
}
Definition graph() { return parsed(example("axi4_graph")).definition; }
} // namespace

TEST_CASE("A path report alone leaves every other artifact unchanged", "[path-report]") {
  auto d = parsed(example("axi4_graph")).definition;
  d.link_timing = timing({uniform(100, 400)});
  auto plain = generate(d);
  REQUIRE(plain);
  GenerationOptions options;
  options.path_report = true;
  auto with = generate(d, options);
  REQUIRE(with);
  REQUIRE(with.value().size() == plain.value().size() + 1);
  for (const auto &a : plain.value()) {
    const auto b = std::ranges::find(with.value(), a.path, &Artifact::path);
    REQUIRE(b != with.value().end());
    if (a.path.ends_with(".bundle.json")) {
      auto x = Json::parse(a.text), y = Json::parse(b->text);
      auto &files = y["files"];
      const auto report = std::ranges::find_if(files, [](const Json &f) {
        return f["path"].get<std::string>().ends_with(".path_report.json");
      });
      REQUIRE(report != files.end());
      CHECK((*report)["role"] == "path_report");
      files.erase(report);
      CHECK(x == y);
    } else
      CHECK(a.text == b->text);
  }
  // Delays never reach the hardware: without link_timing the RTL and map are the same.
  auto bare = d;
  bare.link_timing.reset();
  auto bare_artifacts = generate(bare);
  REQUIRE(bare_artifacts);
  CHECK(bare_artifacts.value() == plain.value());
  // Same input, same bytes.
  CHECK(generate(d, options).value() == with.value());
  CHECK(validate(d, options));
}

TEST_CASE("The report lists each path once with its links, stages and cycles", "[path-report]") {
  auto d = parsed(example("axi4_graph")).definition;
  merge_branch(d).stages = {
      {LinkSide::Sender, 4U, std::nullopt, {{PipeSlice::Forward}}},
      {LinkSide::Receiver, std::nullopt, std::nullopt, {{PipeSlice::Backward}, {PipeSlice::Full}}}};
  const auto report = report_of(d);
  CHECK(report["format"] == "nocloom.path_report");
  // Every link once, paths by distinct link sequence.
  std::set<std::string> names;
  for (const auto &l : report["links"])
    CHECK(names.insert(l["link"].get<std::string>()).second);
  std::set<std::pair<std::string, std::vector<std::string>>> seen;
  unsigned flows = 0;
  for (const auto &p : report["paths"]) {
    CHECK(seen.insert({p["graph"], p["links"].get<std::vector<std::string>>()}).second);
    for (const auto &l : p["links"])
      CHECK(names.contains(l.get<std::string>()));
    flows += static_cast<unsigned>(p["flows"].size());
  }
  // Three initiators reach three targets in each direction.
  CHECK(flows == 18);
  const auto link = std::ranges::find_if(report["links"], [](const Json &l) {
    return l["graph"] == "request" && l["from"] == "merge" && l["to"] == "branch";
  });
  REQUIRE(link != report["links"].end());
  CHECK((*link)["pipe_cycles"]["main"] == 2);
  CHECK(!link->contains("timing"));
  // The path from s0 to t0 crosses merge -> branch: its cycles count the two forward stages.
  const auto path = std::ranges::find_if(report["paths"], [](const Json &p) {
    return p["graph"] == "request" && std::ranges::any_of(p["flows"], [](const Json &f) {
             return f["initiator"] == "s0" && f["target"] == "t0";
           });
  });
  REQUIRE(path != report["paths"].end());
  CHECK((*path)["pipe_cycles"]["main"] == 2);
  CHECK((*path)["links"].size() == 4);
  CHECK(report["plan"]["status"] == "none");
}

TEST_CASE("A route of several flows is one path with each flow", "[path-report]") {
  const auto d = parsed(example("explicit_routes_conn_flows")).definition;
  const auto report = report_of(d);
  std::map<std::vector<std::string>, unsigned> count;
  for (const auto &p : report["paths"])
    ++count[p["links"].get<std::vector<std::string>>()];
  for (const auto &[links, n] : count)
    CHECK(n == 1);
}

TEST_CASE("Separate paths report the write and the read copy of every link and path",
          "[path-report]") {
  const auto shared = report_of(graph());
  auto d = graph();
  d.read_write = ReadWritePaths::Separate;
  const auto report = report_of(d);
  for (const auto &l : shared["links"])
    CHECK(l["carries"] == "both");
  for (const auto &p : shared["paths"])
    CHECK(p["carries"] == "both");
  // Each link and each path once per direction, with the stages of the shared one.
  REQUIRE(report["links"].size() == 2 * shared["links"].size());
  REQUIRE(report["paths"].size() == 2 * shared["paths"].size());
  std::map<std::string, std::string> carries;
  for (const auto &l : report["links"]) {
    const auto name = l["link"].get<std::string>();
    const bool write = name.starts_with("wreq/") || name.starts_with("wrsp/");
    CHECK((write || name.starts_with("rreq/") || name.starts_with("rrsp/")));
    CHECK(l["carries"] == (write ? "writes" : "reads"));
    CHECK(carries.emplace(name, l["carries"].get<std::string>()).second);
  }
  for (const char *direction : {"writes", "reads"}) {
    std::multiset<std::tuple<std::string, std::string, std::string, std::size_t>> ends, base;
    for (const auto &l : report["links"])
      if (l["carries"] == direction)
        ends.emplace(l["graph"].get<std::string>(), l["from"].get<std::string>(),
                     l["to"].get<std::string>(), l["stages"].size());
    for (const auto &l : shared["links"])
      base.emplace(l["graph"].get<std::string>(), l["from"].get<std::string>(),
                   l["to"].get<std::string>(), l["stages"].size());
    CHECK(ends == base);
  }
  // A path stays on the links of its direction and keeps the flows of the shared path.
  std::multiset<std::pair<std::string, Json>> flows, base_flows;
  for (const auto &p : report["paths"]) {
    for (const auto &l : p["links"])
      CHECK(carries.at(l.get<std::string>()) == p["carries"].get<std::string>());
    if (p["carries"] == "reads")
      flows.emplace(p["graph"].get<std::string>(), p["flows"]);
  }
  for (const auto &p : shared["paths"])
    base_flows.emplace(p["graph"].get<std::string>(), p["flows"]);
  CHECK(flows == base_flows);
}

TEST_CASE("One link_timing entry plans the write and the read link of separate paths",
          "[path-report]") {
  auto base = graph();
  base.link_timing = timing({uniform(300, 500)});
  auto d = base;
  d.read_write = ReadWritePaths::Separate;
  const auto shared = report_of(base), report = report_of(d);
  // Both links take the entry: the same status and the same number of paths.
  std::vector<Json> timed;
  std::copy_if(report["links"].begin(), report["links"].end(), std::back_inserter(timed),
               [](const Json &l) { return l.contains("timing"); });
  REQUIRE(timed.size() == 2);
  CHECK(timed[0]["link"].get<std::string>().starts_with("wreq/"));
  CHECK(timed[1]["link"].get<std::string>().starts_with("rreq/"));
  CHECK(timed[0]["timing"]["status"] == timed[1]["timing"]["status"]);
  CHECK(timed[0]["timing"]["paths"].size() == timed[1]["timing"]["paths"].size());
  // The plan is the plan of the shared network, added to both links.
  REQUIRE(shared["plan"]["links"].size() == 1);
  REQUIRE(report["plan"]["links"].size() == 1);
  const auto &one = shared["plan"]["links"][0], &both = report["plan"]["links"][0];
  CHECK(report["plan"]["status"] == "met");
  CHECK(both["stages"] == one["stages"]);
  CHECK(both["pipe_cycles_added"] == one["pipe_cycles_added"]);
  CHECK(both["slack_ps"] == one["slack_ps"]);
  CHECK(both["bits_added"].get<std::uint64_t>() == 2 * one["bits_added"].get<std::uint64_t>());
  REQUIRE(both["added"].size() == 2 * one["added"].size());
  const auto half = one["added"].size();
  for (std::size_t i = 0; i < half; ++i) {
    CHECK(both["added"][i]["name"].get<std::string>().starts_with("pipeline/wreq/"));
    CHECK(both["added"][half + i]["name"].get<std::string>().starts_with("pipeline/rreq/"));
    CHECK(both["added"][i]["slice"] == one["added"][i]["slice"]);
    CHECK(both["added"][half + i]["slice"] == one["added"][i]["slice"]);
  }
  // The planned definition is the shared plan with separate paths, and both links then meet.
  auto plan = make_plan(d);
  REQUIRE(plan);
  auto planned = plan_link_stages(plan.value());
  REQUIRE(planned);
  auto shared_plan = make_plan(base);
  REQUIRE(shared_plan);
  auto expected = plan_link_stages(shared_plan.value());
  REQUIRE(expected);
  expected.value().read_write = ReadWritePaths::Separate;
  CHECK(planned.value() == expected.value());
  const auto again = report_of(planned.value());
  unsigned met = 0;
  for (const auto &l : again["links"])
    if (l.contains("timing")) {
      CHECK(l["timing"]["status"] == "met");
      ++met;
    }
  CHECK(met == 2);
  CHECK(again["plan"]["links"].empty());
  // A latency limit names the direction of the path that exceeds it.
  auto t = timing({uniform(300, 500)});
  t.latency_limits = {{"s0", "t0", 0U, std::nullopt}};
  d.link_timing = t;
  const auto ds = plan_failure(d);
  const auto message = [&](const std::string &start) {
    return std::ranges::any_of(ds, [&](const Diagnostic &x) {
      return x.code == "NCL_STAGE_PLAN_UNMET" &&
             x.field == "link_timing.latency_limits[0].request_cycles" &&
             x.message.starts_with(start);
    });
  };
  CHECK(message("The write request path of s0 and t0 takes "));
  CHECK(message("The read request path of s0 and t0 takes "));
}

TEST_CASE("Missing delays are unknown and named", "[path-report]") {
  auto d = graph();
  auto l = uniform(100, 400);
  l.spans.erase(l.spans.begin() + 1); // no entry for the sender 16-byte span
  l.spans[0].turn_ps.reset();
  d.link_timing = timing({l});
  const auto report = report_of(d);
  const auto link =
      std::ranges::find_if(report["links"], [](const Json &x) { return x.contains("timing"); });
  REQUIRE(link != report["links"].end());
  CHECK((*link)["timing"]["status"] == "unknown");
  const auto missing = (*link)["timing"]["missing"].get<std::vector<std::string>>();
  CHECK(std::ranges::find(missing, "link_timing.links[0].spans") != missing.end());
  CHECK(std::ranges::find(missing, "link_timing.links[0].spans[0].turn_ps") != missing.end());
  CHECK(std::ranges::any_of((*link)["timing"]["paths"],
                            [](const Json &p) { return p["delay_ps"].is_null(); }));
  const auto ds = plan_failure(d);
  CHECK(has(ds, "NCL_STAGE_PLAN_UNKNOWN", "link_timing.links[0].spans"));
  CHECK(has(ds, "NCL_STAGE_PLAN_UNKNOWN", "link_timing.links[0].spans[0].turn_ps"));
}

TEST_CASE("A related-clock crossing is listed and its link timing is unknown", "[path-report]") {
  const auto d = parsed(R"(schema_version: 10
name: related_link
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_roots: [{name: root, clock_port: clk, period_ps: 1000}]
clock_domains:
  - {name: fast, clock_port: clk, clock_root: root, reset_port: reset_n, test_port: test,
     period_ps: 1000}
  - {name: slow, clock_port: clk, clock_root: root, ratio: 2, edge_parent: fast,
     reset_port: reset_n, test_port: test, period_ps: 2000}
clock_crossings:
  - {from_domain: fast, to_domain: slow, kind: related}
  - {from_domain: slow, to_domain: fast, kind: related}
initiators:
  - {name: s, clock_domain: fast, protocol: axi4, data_width: 32}
targets:
  - {name: t, clock_domain: slow, protocol: axi4, data_width: 32, attributes: amba,
     mappings: [{base: '0x0', size: '0x1000'}]}
transport: {transport_word_bytes: 4}
topology:
  request:
    nodes: [{name: a, clock_domain: fast}, {name: b, clock_domain: slow}]
    links: [{from: a, to: b}]
    entries: [{endpoint: s, node: a}]
    exits: [{endpoint: t, node: b}]
  response:
    nodes: [{name: a, clock_domain: fast}, {name: b, clock_domain: slow}]
    links: [{from: b, to: a}]
    entries: [{endpoint: t, node: b}]
    exits: [{endpoint: s, node: a}]
connectivity: all
link_timing:
  stage: {launch_ps: 40, capture_ps: 30, forward_pass_ps: 20, ready_pass_ps: 10}
  links:
    - graph: request
      from: a
      to: b
      spans: [{side: sender, word_bytes: 4, forward_ps: 100, ready_ps: 100}]
      crossing: {forward_ps: 100, ready_ps: 100}
      allowances: {sender: {forward_ps: 300, ready_ps: 300},
                   receiver: {forward_ps: 300, ready_ps: 300}}
output: {backend: verilog, language: verilog_2001}
)")
                     .definition;
  const auto report = report_of(d);
  // Each switch link lists its crossing as a stage of the receiver side, in the receiver domain.
  unsigned crossed = 0;
  for (const auto &l : report["links"]) {
    if (l["from_kind"] != "switch" || l["to_kind"] != "switch")
      continue;
    REQUIRE(l["stages"].size() == 1);
    CHECK(l["stages"][0]["kind"] == "crossing");
    CHECK(l["stages"][0]["side"] == "receiver");
    CHECK(l["stages"][0]["clock_domain"] == l["receiver_domain"]);
    CHECK(l["sender_domain"] != l["receiver_domain"]);
    ++crossed;
    if (l["graph"] == "response") {
      CHECK_FALSE(l.contains("timing"));
      continue;
    }
    // The timing names its entry and gives no missing fields and no paths.
    std::set<std::string> keys;
    for (const auto &[key, value] : l["timing"].items())
      keys.insert(key);
    CHECK(keys == std::set<std::string>{"delays", "status", "unmodeled_crossing"});
    CHECK(l["timing"]["delays"] == "link_timing.links[0]");
    CHECK(l["timing"]["status"] == "unknown");
    CHECK(l["timing"]["unmodeled_crossing"] == true);
  }
  CHECK(crossed == 2);
  for (const auto &p : report["paths"])
    CHECK(p["crossings"] == 1);
  CHECK(report["plan"]["status"] == "unmet");
  CHECK(report["plan"]["links"].empty());
  REQUIRE(report["plan"]["unmet"].size() == 1);
  CHECK(report["plan"]["unmet"][0]["code"] == "NCL_STAGE_PLAN_UNKNOWN");
  CHECK(has(plan_failure(d), "NCL_STAGE_PLAN_UNKNOWN", "link_timing.links[0]"));
}

TEST_CASE("The planner adds the stages a period needs and the result checks", "[path-report]") {
  // Adapters of 300 ps and a wire of 500 ps in a 1000 ps period: the 2100 ps run needs cuts.
  auto d = graph();
  d.link_timing = timing({uniform(300, 500)});
  auto plan = make_plan(d);
  REQUIRE(plan);
  auto planned = plan_link_stages(plan.value());
  if (!planned)
    FAIL(planned.diagnostics().front().field + ": " + planned.diagnostics().front().message);
  const auto &stages = merge_branch(planned.value()).stages;
  REQUIRE_FALSE(stages.empty());
  // Only the planned link changes, and the planned definition generates.
  auto expected = d;
  merge_branch(expected).stages = stages;
  CHECK(planned.value() == expected);
  REQUIRE(generate(planned.value()));
  // The planned definition meets every bound and plans nothing more.
  const auto again = report_of(planned.value());
  const auto link =
      std::ranges::find_if(again["links"], [](const Json &x) { return x.contains("timing"); });
  REQUIRE(link != again["links"].end());
  CHECK((*link)["timing"]["status"] == "met");
  CHECK(again["plan"]["status"] == "met");
  CHECK(again["plan"]["links"].empty());
  // The first report proposes the same lists.
  const auto first = report_of(d);
  REQUIRE(first["plan"]["links"].size() == 1);
  const auto &proposed = first["plan"]["links"][0];
  CHECK(proposed["from"] == "merge");
  REQUIRE(proposed["stages"].size() == stages.size());
  for (std::size_t i = 0; i < stages.size(); ++i) {
    CHECK(proposed["stages"][i]["word_bytes"] == stages[i].word_bytes.value());
    CHECK(proposed["stages"][i]["pipes"].size() == stages[i].pipes.size());
  }
  CHECK(proposed["added"].size() == stages.size());
}

TEST_CASE("Latency limits and periods that no plan meets are named", "[path-report]") {
  auto d = graph();
  auto t = timing({uniform(300, 500)});
  t.latency_limits = {{"s0", "t0", 0U, std::nullopt}};
  d.link_timing = t;
  auto ds = plan_failure(d);
  CHECK(has(ds, "NCL_STAGE_PLAN_UNMET", "link_timing.latency_limits[0].request_cycles"));
  // A wire longer than the period cannot be cut at any position.
  d.link_timing = timing({uniform(100, 1200)});
  ds = plan_failure(d);
  CHECK(has(ds, "NCL_STAGE_PLAN_UNMET", "link_timing.periods[0].period_ps"));
  // A path that leaves through an end with no allowance names the missing field.
  auto l = uniform(100, 400);
  l.allowances.receiver.forward_ps.reset();
  d.link_timing = timing({l});
  ds = plan_failure(d);
  CHECK(has(ds, "NCL_STAGE_PLAN_UNMET", "link_timing.links[0].allowances.receiver.forward_ps"));
  const auto report = report_of(d);
  CHECK(report["plan"]["status"] == "unmet");
  CHECK(std::ranges::any_of(report["plan"]["unmet"], [](const Json &u) {
    return u["field"] == "link_timing.links[0].allowances.receiver.forward_ps";
  }));
}

TEST_CASE("YAML and C++ link timing are the same definition", "[path-report]") {
  auto text = example("axi4_graph");
  text += R"(link_timing:
  periods: [{clock_domain: main, period_ps: 1000}]
  stage: {launch_ps: 40, capture_ps: 30, forward_pass_ps: 20, ready_pass_ps: 10}
  links:
    - graph: request
      from: merge
      to: branch
      spans:
        - {side: sender, word_bytes: 32, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 16, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 8, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: sender, word_bytes: 4, forward_ps: 500, ready_ps: 500}
        - {side: receiver, word_bytes: 4, forward_ps: 300, ready_ps: 300, turn_ps: 300}
        - {side: receiver, word_bytes: 8, forward_ps: 300, ready_ps: 300, turn_ps: 300}
      allowances:
        sender: {forward_ps: 300, ready_ps: 300, return_ps: 300}
        receiver: {forward_ps: 300, ready_ps: 300}
  latency_limits:
    - {initiator: s0, target: t0, request_cycles: 3}
)";
  const auto yaml = parsed(text).definition;
  auto cpp = parsed(example("axi4_graph")).definition;
  auto t = timing({uniform(300, 500)});
  t.latency_limits = {{"s0", "t0", 3U, std::nullopt}};
  cpp.link_timing = t;
  CHECK(yaml == cpp);
  // Strict fields.
  for (const auto *bad :
       {"link_timing: {stage: {launch: 4}}\n", "link_timing: {links: [{graph: both}]}\n",
        "link_timing: {links: [{graph: request, from: merge, to: branch, allowances: "
        "{receiver: {return_ps: 4}}}]}\n",
        "link_timing: {periods: [{clock_domain: main, period_ps: '5'}]}\n"}) {
    auto p = config::parse(example("axi4_graph") + bad, "bad.yaml");
    CHECK_FALSE(p);
  }
}

TEST_CASE("link_timing names and ranges give field diagnostics", "[path-report]") {
  const auto failure = [](const Definition &d, const std::string &field) {
    const auto status = validate(d);
    REQUIRE_FALSE(status);
    CHECK(has(status.diagnostics(), "NCL_LINK_TIMING", field));
  };
  auto d = graph();
  auto t = timing({uniform(300, 500)});
  t.links[0].to = "tail";
  d.link_timing = t;
  failure(d, "link_timing.links[0]");
  t = timing({uniform(300, 500), uniform(300, 500)});
  d.link_timing = t;
  failure(d, "link_timing.links[1]");
  t = timing({uniform(300, 500)});
  t.links[0].spans[0].word_bytes = 2;
  d.link_timing = t;
  failure(d, "link_timing.links[0].spans[0].word_bytes");
  t = timing({uniform(300, 500)});
  t.links[0].spans[4].word_bytes = 32; // the response side has no 32-byte receiver position
  d.link_timing = t;
  failure(d, "link_timing.links[0].spans[4].word_bytes");
  t = timing({uniform(300, 500)});
  t.links[0].spans.push_back({LinkSide::Receiver, 16U, 1, 1, std::nullopt});
  d.link_timing = t;
  failure(d, "link_timing.links[0].spans[6]");
  t = timing({uniform(300, 500)});
  t.links[0].spans[3].turn_ps = 5; // the wire has no adapter
  d.link_timing = t;
  failure(d, "link_timing.links[0].spans[3].turn_ps");
  t = timing({uniform(300, 500)});
  t.links[0].spans.push_back(t.links[0].spans[1]);
  d.link_timing = t;
  failure(d, "link_timing.links[0].spans[6]");
  t = timing({uniform(300, 500)});
  t.links[0].crossing = CrossingDelays{1, 1};
  d.link_timing = t;
  failure(d, "link_timing.links[0].crossing");
  t = timing({uniform(300, 500)});
  t.periods.push_back({"main", 900});
  t.periods.push_back({"elsewhere", 0});
  d.link_timing = t;
  failure(d, "link_timing.periods[1].clock_domain");
  failure(d, "link_timing.periods[2].clock_domain");
  failure(d, "link_timing.periods[2].period_ps");
  t = timing({uniform(300, 500)});
  t.stage.launch_ps = 1000000001;
  d.link_timing = t;
  failure(d, "link_timing.stage.launch_ps");
  t = timing({uniform(300, 500)});
  t.links[0].allowances.through.return_ps = 3;
  d.link_timing = t;
  failure(d, "link_timing.links[0].allowances.through.return_ps");
  t = timing({});
  t.latency_limits = {{"nobody", "t0", 1U, std::nullopt}, {"s0", "t0"}};
  d.link_timing = t;
  failure(d, "link_timing.latency_limits[0].initiator");
  failure(d, "link_timing.latency_limits[1]");
}

namespace {
// A synthetic chain of the given span kinds: 'a' an adapter span, 'w' the wire, 'c' a crossing
// followed by its span; positions sit between them.
detail::Chain synthetic(const std::string &shape, std::mt19937 &rng, bool allowances) {
  using detail::ChainItem;
  detail::Chain c;
  c.link = "req/x>y";
  std::uniform_int_distribution<unsigned> delay(0, 400), coin(0, 3);
  bool receiver = false;
  unsigned position = 0;
  const auto item = [&](ChainItem::Kind kind, std::string name) {
    ChainItem x;
    x.kind = kind;
    x.receiver = receiver;
    x.name = std::move(name);
    return x;
  };
  const auto slot = [&] {
    c.positions.push_back({receiver, 4U << (position % 4), 0, 50U + 10 * position});
    if (coin(rng) == 0) {
      auto fixed = item(ChainItem::Kind::Pipe, "fixed" + std::to_string(position));
      fixed.pipe.slice = static_cast<PipeSlice>(1 + coin(rng));
      c.items.push_back(fixed);
    }
    auto open = item(ChainItem::Kind::Slot, {});
    open.position = position++;
    c.items.push_back(open);
  };
  slot();
  for (const char k : shape) {
    if (k == 'c') {
      receiver = true;
      c.items.push_back(item(ChainItem::Kind::Crossing, "cut"));
      auto after = item(ChainItem::Kind::Span, "crossing");
      after.forward = delay(rng) / 4;
      after.ready = delay(rng) / 4;
      c.items.push_back(after);
    } else {
      auto span = item(ChainItem::Kind::Span, "span" + std::to_string(position));
      span.position = position - 1;
      span.adapter = k == 'a';
      span.forward = delay(rng);
      span.ready = delay(rng);
      if (span.adapter)
        span.turn = delay(rng);
      c.items.push_back(span);
      if (k == 'w')
        receiver = true;
    }
    slot();
  }
  c.sender_period = 500 + delay(rng);
  c.receiver_period = shape.find('c') == std::string::npos ? *c.sender_period : 500 + delay(rng);
  c.stage = {delay(rng) / 8, delay(rng) / 8, delay(rng) / 8, delay(rng) / 8};
  const auto allowance = [&]() -> std::optional<std::uint64_t> {
    if (!allowances && coin(rng) == 0)
      return std::nullopt;
    return 200 + delay(rng);
  };
  c.allowances.sender = {allowance(), allowance(), allowance()};
  c.allowances.receiver = {allowance(), allowance(), std::nullopt};
  c.allowances.through = {allowance(), allowance(), std::nullopt};
  return c;
}
unsigned forward_pipes(const detail::SlotChoice &choice) {
  return static_cast<unsigned>(std::ranges::count_if(
      choice, [](PipeSlice s) { return s == PipeSlice::Forward || s == PipeSlice::Full; }));
}
} // namespace

TEST_CASE("The planner finds the best placement every enumeration finds", "[path-report]") {
  std::mt19937 rng(20261121);
  const std::vector<std::string> shapes{"aaw", "awa", "wa", "aawa", "aaca", "awaa", "w", "cw"};
  unsigned feasible = 0, infeasible = 0;
  for (unsigned round = 0; round < 240; ++round) {
    const auto c = synthetic(shapes[round % shapes.size()], rng, round % 2 == 0);
    const auto n = c.positions.size();
    // Every choice, by forward registers, bits, then slack.
    std::optional<std::tuple<unsigned, std::uint64_t, std::int64_t>> best;
    detail::SlotChoice choice(n);
    const std::array kinds{PipeSlice::None, PipeSlice::Forward, PipeSlice::Backward,
                           PipeSlice::Full};
    std::function<void(std::size_t)> enumerate = [&](std::size_t i) {
      if (i == n) {
        const auto r = detail::evaluate_chain(c, choice);
        if (!r.met)
          return;
        const auto added = forward_pipes(choice);
        const auto key = std::tuple{added, r.added_bits, -r.slack.value()};
        if (!best || key < *best)
          best = key;
        return;
      }
      for (const auto k : kinds) {
        choice[i] = k;
        enumerate(i + 1);
      }
    };
    enumerate(0);
    const auto planned = detail::plan_chain(c);
    REQUIRE(planned.has_value() == best.has_value());
    if (!planned) {
      ++infeasible;
      continue;
    }
    ++feasible;
    const auto r = detail::evaluate_chain(c, *planned);
    REQUIRE(r.met);
    const auto added = forward_pipes(*planned);
    CHECK(std::tuple{added, r.added_bits, -r.slack.value()} == *best);
  }
  CHECK(feasible > 40);
  CHECK(infeasible > 10);
}

namespace {
// The logic depth of IR values: each logic operation counts one, slices, concatenations and zero
// extension are wires. States, inputs, constants and the values in `starts` begin a path.
struct LogicDepth {
  const detail::HardwareIR &ir;
  std::set<std::size_t> starts = {};
  std::map<std::size_t, unsigned> memo = {};
  unsigned operator()(detail::ValueId id) {
    if (starts.contains(id.index))
      return 0;
    if (const auto it = memo.find(id.index); it != memo.end())
      return it->second;
    const auto &v = ir.values.at(id.index);
    using detail::Op;
    unsigned depth = 0;
    if (v.op != Op::State && v.op != Op::Input && v.op != Op::Constant && v.op != Op::ClockGate) {
      for (unsigned i = 0; i < detail::operand_count(v.op); ++i)
        depth = std::max(depth, (*this)(v.operands[i]));
      if (v.op != Op::Slice && v.op != Op::Concat && v.op != Op::ZeroExtend)
        ++depth;
    }
    memo[id.index] = depth;
    return depth;
  }
};
struct Lowered {
  detail::ExchangeHardware h;
  std::string link;
};
Lowered lower(const Definition &d) {
  REQUIRE(validate(d));
  auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  const auto link = std::ranges::find_if(plan.value().links, [](const detail::LinkPlan &l) {
    return !l.response && l.from_name == "merge" && l.to_name == "branch";
  });
  REQUIRE(link != plan.value().links.end());
  const auto name = link->name;
  auto h = detail::lower_axi4_exchange_composition(plan.value());
  REQUIRE(h);
  return {std::move(h.value()), name};
}
// The bindings of a pipe's forward inputs or outputs: valid and every field.
std::vector<detail::ValueId> faces(const detail::ExchangeHardware &h, const std::string &id,
                                   const std::string &side) {
  std::vector<detail::ValueId> out{h.bindings.at(id + "." + side + ".valid")};
  for (unsigned k = 0; h.bindings.contains(id + "." + side + ".field" + std::to_string(k)); ++k)
    out.push_back(h.bindings.at(id + "." + side + ".field" + std::to_string(k)));
  return out;
}
unsigned deepest(LogicDepth &depth, const std::vector<detail::ValueId> &values) {
  return std::accumulate(values.begin(), values.end(), 0U, [&](unsigned most, detail::ValueId v) {
    return std::max(most, depth(v));
  });
}
// The deepest cone into the registers of a pipe.
unsigned into_registers(const detail::ExchangeHardware &h, LogicDepth &depth,
                        const std::string &id) {
  const auto storage = std::ranges::find(h.storage, id, &detail::CrossStorageBinding::resource);
  REQUIRE(storage != h.storage.end());
  std::vector<detail::ValueId> nexts;
  for (const auto *group : {&storage->payload, &storage->control})
    std::ranges::transform(*group, std::back_inserter(nexts), [&](detail::ValueId q) {
      return h.ir.states.at(h.ir.values.at(q.index).state.index).next;
    });
  return deepest(depth, nexts);
}
const std::vector<std::pair<LinkSide, unsigned>> positions{
    {LinkSide::Sender, 32},  {LinkSide::Sender, 16},  {LinkSide::Sender, 8},
    {LinkSide::Sender, 4},   {LinkSide::Receiver, 4}, {LinkSide::Receiver, 8},
    {LinkSide::Receiver, 16}};
std::string pipe_at(const std::string &link, std::size_t i, unsigned index = 0) {
  return "pipeline/" + link + "/" +
         (positions[i].first == LinkSide::Sender ? "sender/" : "receiver/") +
         std::to_string(positions[i].second) + "/" + std::to_string(index);
}
} // namespace

TEST_CASE("A planned link gives the claimed cuts in the logic depth of its hardware",
          "[path-report]") {
  // Full pipes at both switch ends keep every path of merge -> branch inside the link.
  auto base = graph();
  merge_branch(base).sender_pipeline = {PipeSlice::Full};
  merge_branch(base).pipeline = {PipeSlice::Full};
  // Span delays: a full pipe at every position isolates each span.
  auto probe = base;
  for (const auto &[side, word] : positions)
    merge_branch(probe).stages.push_back({side, word, std::nullopt, {{PipeSlice::Full}}});
  const auto measured = lower(probe);
  const auto &h = measured.h;
  LogicDepth depth{h.ir};
  LinkDelays delays{LinkGraph::Request, "merge", "branch"};
  for (std::size_t i = 0; i + 1 < positions.size(); ++i) {
    const auto here = pipe_at(measured.link, i), next = pipe_at(measured.link, i + 1);
    const auto forward = deepest(depth, faces(h, next, "input"));
    const auto ready = depth(h.bindings.at(here + ".output.ready"));
    delays.spans.push_back({positions[i].first, positions[i].second, forward, ready,
                            i == 3 ? std::nullopt : std::optional<std::uint64_t>{ready}});
  }
  // Each pipe kind's own delays, measured on one pipe of each.
  auto kinds = base;
  merge_branch(kinds).stages = {{LinkSide::Sender, 32U, std::nullopt, {{PipeSlice::Forward}}},
                                {LinkSide::Sender, 16U, std::nullopt, {{PipeSlice::Backward}}},
                                {LinkSide::Sender, 8U, std::nullopt, {{PipeSlice::Full}}}};
  const auto one_each = lower(kinds);
  std::uint64_t launch = 0, capture = 0;
  for (std::size_t i = 0; i < 3; ++i) {
    const auto id = pipe_at(one_each.link, i);
    LogicDepth own{one_each.h.ir};
    launch =
        std::max<std::uint64_t>(launch, std::max(deepest(own, faces(one_each.h, id, "output")),
                                                 own(one_each.h.bindings.at(id + ".input.ready"))));
    LogicDepth from_inputs{one_each.h.ir};
    for (const auto v : faces(one_each.h, id, "input"))
      from_inputs.starts.insert(v.index);
    from_inputs.starts.insert(one_each.h.bindings.at(id + ".output.ready").index);
    capture = std::max<std::uint64_t>(capture, into_registers(one_each.h, from_inputs, id));
  }
  // The deepest path from the given values to others, other sources counting zero.
  const auto through = [&](const std::vector<detail::ValueId> &from,
                           const std::vector<detail::ValueId> &to) {
    LogicDepth d{one_each.h.ir};
    for (const auto v : from)
      d.starts.insert(v.index);
    return deepest(d, to);
  };
  const auto backward = pipe_at(one_each.link, 1), forward = pipe_at(one_each.link, 0);
  const auto forward_pass =
      through(faces(one_each.h, backward, "input"), faces(one_each.h, backward, "output"));
  const auto ready_pass = through({one_each.h.bindings.at(forward + ".output.ready")},
                                  {one_each.h.bindings.at(forward + ".input.ready")});
  delays.allowances.sender = {10000, 10000, 10000};
  delays.allowances.receiver = {10000, 10000, std::nullopt};
  LinkTiming t;
  t.stage = {launch, capture, forward_pass, ready_pass};
  // The forward run from the sender pipe to the receiver pipe before planning, halved as the
  // period.
  const auto run =
      std::accumulate(delays.spans.begin(), delays.spans.end(), launch + capture,
                      [](std::uint64_t sum, const SpanDelays &s) { return sum + *s.forward_ps; });
  const auto period = run / 2;
  t.periods = {{"main", period}};
  t.links = {delays};
  base.link_timing = t;
  const auto before = lower(base);
  LogicDepth before_depth{before.h.ir};
  const auto sender_pipe = "pipeline/" + before.link + "/out",
             receiver_pipe = "pipeline/" + before.link;
  const auto arrival = deepest(before_depth, faces(before.h, receiver_pipe, "input"));
  CHECK(arrival + capture <= run);
  CHECK(arrival + capture > period);
  auto plan = make_plan(base);
  REQUIRE(plan);
  auto planned = plan_link_stages(plan.value());
  if (!planned)
    FAIL(planned.diagnostics().front().field + ": " + planned.diagnostics().front().message);
  // The claims: every path of the planned link, from the chain of the planned definition.
  auto replanned = detail::build_network_plan(planned.value());
  REQUIRE(replanned);
  const auto link = std::ranges::find_if(replanned.value().links, [](const detail::LinkPlan &l) {
    return !l.response && l.from_name == "merge" && l.to_name == "branch";
  });
  const auto chain =
      detail::link_chain(replanned.value(), planned.value(), *link,
                         &planned.value().link_timing->links[0], "link_timing.links[0]");
  const auto claims = detail::evaluate_chain(chain, detail::SlotChoice(chain.positions.size()));
  REQUIRE(claims.met);
  const auto claim = [&](const std::string &to, detail::TimingSignal signal) {
    std::uint64_t most = 0;
    for (const auto &p : claims.paths)
      if (p.to == to && (p.signal == signal || (signal == detail::TimingSignal::Ready &&
                                                p.signal == detail::TimingSignal::Turn)))
        most = std::max(most, *p.delay);
    return most;
  };
  const auto after = lower(planned.value());
  LogicDepth after_depth{after.h.ir};
  unsigned planned_pipes = 0;
  for (const auto &list : merge_branch(planned.value()).stages)
    for (std::size_t k = 0; k < list.pipes.size(); ++k) {
      const auto i = static_cast<std::size_t>(
          std::ranges::find(positions, std::pair{list.side, *list.word_bytes}) - positions.begin());
      const auto id = pipe_at(after.link, i, static_cast<unsigned>(k));
      const auto into = into_registers(after.h, after_depth, id);
      CHECK(into <= std::max(claim(id, detail::TimingSignal::Forward),
                             claim(id, detail::TimingSignal::Ready)));
      CHECK(into <= period);
      ++planned_pipes;
    }
  CHECK(planned_pipes > 0);
  // The receiver pipe now sees a shorter forward run, within its claim.
  const auto cut_arrival = deepest(after_depth, faces(after.h, receiver_pipe, "input"));
  CHECK(cut_arrival + capture <= claim(receiver_pipe, detail::TimingSignal::Forward));
  CHECK(cut_arrival < arrival);
  // And the sender pipe a ready run within its claim.
  CHECK(after_depth(after.h.bindings.at(sender_pipe + ".output.ready")) + capture <=
        claim(sender_pipe, detail::TimingSignal::Ready));
}

TEST_CASE("Planned pipes follow the pipes a position already has", "[path-report]") {
  auto d = graph();
  d.link_timing = timing({uniform(300, 500)});
  merge_branch(d).stages = {
      {LinkSide::Sender, 32U, std::nullopt, {{PipeSlice::Backward}}},
      {LinkSide::Receiver, 16U, std::nullopt, {{PipeSlice::Backward}, {PipeSlice::Backward}}}};
  auto plan = make_plan(d);
  REQUIRE(plan);
  auto planned = plan_link_stages(plan.value());
  if (!planned)
    FAIL(planned.diagnostics().front().field + ": " + planned.diagnostics().front().message);
  const auto &stages = merge_branch(planned.value()).stages;
  REQUIRE(stages.size() >= 2);
  // The written lists keep their place and their pipes; added pipes go after them.
  CHECK(stages[0].pipes.front() == LinkPipeline{PipeSlice::Backward});
  CHECK(stages[1].pipes[0] == LinkPipeline{PipeSlice::Backward});
  CHECK(stages[1].pipes[1] == LinkPipeline{PipeSlice::Backward});
  CHECK(stages[0].pipes.size() + stages[1].pipes.size() > 3);
  const auto again = report_of(planned.value());
  CHECK(again["plan"]["status"] == "met");
  CHECK(again["plan"]["links"].empty());
}
