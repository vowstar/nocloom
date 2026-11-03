// Dependency graph checks cover wait cycles and their bindings to lowered hardware.
// Bounded RTL progress and service assumptions are checked in tests/run_liveness_formal.py.
#include "core/cone_walk.hpp"
#include "core/cross_domain.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include <algorithm>
#include <catch2/catch_message.hpp>
#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers.hpp>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <nocloom/config.hpp>
#include <set>
#include <sstream>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
struct Ends {
  unsigned width = 0, outstanding = 0, id_width = 0, targets_k = 0;
  bool read_only = false, multi_hop = false, pipes = false;
  int granule = -1; // the targets' read_interleave_bytes; negative leaves it out
  unsigned target_width = 32;
};
std::string shape_text(const Ends &e) {
  std::ostringstream y;
  y << "{schema_version: 10, name: shape, address_width: 32,"
    << " arbitration: {policy: round_robin}, reset_ports: [{name: reset_n}],"
    << " test_ports: [{name: test}],"
    << " clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}],"
    << " initiators: [";
  for (const auto *n : {"s0", "s1"})
    y << "{name: " << n << ", clock_domain: main, protocol: axi4, data_width: " << e.width
      << ", max_outstanding: " << e.outstanding << ", id_width: " << e.id_width << ", len_width: 8"
      << (e.read_only ? ", access: read_only" : "") << "},";
  y << "], targets: [";
  for (unsigned i = 0; i < 2; ++i)
    y << "{name: t" << i << ", clock_domain: main, protocol: axi4, data_width: " << e.target_width
      << "," << " attributes: amba, max_outstanding: " << e.targets_k
      << ", id_width: 4, len_width: 8,"
      << (e.granule < 0 ? std::string()
                        : " read_interleave_bytes: " + std::to_string(e.granule) + ",")
      << " mappings: [{base: '0x" << std::hex << i * 0x8000 << std::dec << "', size: '0x8000'}]},";
  y << "], connectivity: all,";
  if (e.multi_hop) {
    const auto pipe = e.pipes ? ", pipeline: {forward: true, backward: true}" : "";
    y << " topology: {request: {nodes: [{name: q}], links: [],"
      << " entries: [{endpoint: s0, node: q}, {endpoint: s1, node: q}],"
      << " exits: [{endpoint: t0, node: q}, {endpoint: t1, node: q}]},"
      << " response: {nodes: [{name: a}, {name: c}, {name: d0}, {name: d1}], links: ["
      << "{from: a, to: d0" << pipe << "}, {from: a, to: d1" << pipe << "},"
      << "{from: c, to: d0" << pipe << "}, {from: c, to: d1" << pipe << "}],"
      << " entries: [{endpoint: t0, node: a}, {endpoint: t1, node: c}],"
      << " exits: [{endpoint: s0, node: d0}, {endpoint: s1, node: d1}]}},";
  }
  y << " output: {backend: verilog, language: verilog_2001}}";
  return y.str();
}
NetworkPlan plan_of(const std::string &text) {
  const auto parsed = config::parse(text, "shape");
  INFO((parsed ? std::string() : parsed.diagnostics().front().message));
  REQUIRE(parsed);
  auto plan = build_network_plan(parsed.value().definition);
  INFO((plan ? std::string() : plan.diagnostics().front().message));
  REQUIRE(plan);
  return std::move(plan).value();
}
NetworkPlan plan_of(const Ends &e) { return plan_of(shape_text(e)); }
// 64-bit single-ID sources crossing reads and writes over two K=4 targets: hung in simulation while
// their response grants held across a read. held() is the plan with that hold back.
const Ends single_id{64, 4, 0, 4};
DependencyFacts held(const NetworkPlan &plan) {
  auto facts = dependency_facts(plan);
  facts.segment_ends.assign(facts.segment_ends.size(), false);
  return facts;
}
// The kinds of a finding's named nodes, in cycle order.
std::multiset<std::string> kinds(const DependencyGraph &g, const DependencyFinding &f) {
  std::multiset<std::string> out;
  for (const auto n : f.cycle)
    out.insert(g.nodes[n].kind);
  return out;
}
bool has_rule(const DependencyFinding &f, DependencyRule rule) {
  return std::ranges::find(f.rules, rule) != f.rules.end();
}
DependencyReport checked(const NetworkPlan &plan, const DependencyFacts &facts,
                         DependencyGraph *out = nullptr) {
  auto g = build_dependency_graph(plan, facts);
  auto report = check_dependency_graph(g);
  if (out)
    *out = std::move(g);
  return report;
}
std::vector<std::filesystem::path> example_files() {
  std::vector<std::filesystem::path> out;
  for (const auto &e :
       std::filesystem::directory_iterator(std::string(NOCLOOM_SOURCE_DIR) + "/examples"))
    if (e.path().extension() == ".yaml")
      out.push_back(e.path());
  std::ranges::sort(out);
  return out;
}
std::string read_text(const std::filesystem::path &path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}
} // namespace

TEST_CASE("Grants held across reads toward a one-read gather form the simulated cycle",
          "[dependency]") {
  const auto plan = plan_of(single_id);
  CHECK(plan.dependency_report.findings.empty());
  DependencyGraph graph;
  const auto report = checked(plan, held(plan), &graph);
  REQUIRE_FALSE(report.findings.empty());
  const auto &finding = report.findings.front();
  CHECK(finding.level == "micro");
  CHECK(has_rule(finding, DependencyRule::Continuation));
  CHECK(has_rule(finding, DependencyRule::Contention));
  const auto named = kinds(graph, finding);
  CHECK(named.count("grant.held") == 2);
  CHECK(named.count("target.head") == 2);
  const auto text = dependency_explanation(graph, finding);
  CHECK(text.back().starts_with("Fix: "));
}

TEST_CASE("The grant stall needs the word's own wait on what follows the output", "[dependency]") {
  // Three hops: the two contended outputs are one hop past the target, so the word at the target's
  // head only stalls through the output in front of it.
  auto multi = single_id;
  multi.multi_hop = true;
  const auto plan = plan_of(multi);
  CHECK(plan.dependency_report.findings.empty());
  DependencyGraph graph;
  const auto report = checked(plan, held(plan), &graph);
  REQUIRE_FALSE(report.findings.empty());
  const auto &finding = report.findings.front();
  CHECK(has_rule(finding, DependencyRule::Downstream));
  CHECK(kinds(graph, finding).count("link.word") == 2);
  // Buffers change how often it hangs (a seed of the piped shape did), not whether it can.
  multi.pipes = true;
  const auto piped = plan_of(multi);
  CHECK_FALSE(checked(piped, held(piped)).findings.empty());
}

TEST_CASE("The fixes the report names remove the cycle", "[dependency]") {
  for (const auto fixed : {Ends{64, 1, 0, 4}, Ends{64, 4, 0, 1}}) {
    const auto plan = plan_of(fixed);
    CHECK(checked(plan, held(plan)).findings.empty());
  }
}

TEST_CASE("One read direction with one ID has only one target answering", "[dependency]") {
  auto read_only = single_id;
  read_only.read_only = true;
  const auto plan = plan_of(read_only);
  DependencyGraph graph;
  CHECK(checked(plan, held(plan), &graph).findings.empty());
  CHECK(std::ranges::count_if(graph.excluded,
                              [](const auto &x) { return x.rule == "X1" && x.edges > 0; }) == 2);
}

TEST_CASE("A read interleave size leaves no cycle in the shapes it takes", "[dependency]") {
  for (const unsigned width : {32U, 64U})
    for (const unsigned target_width : {32U, 64U})
      for (const unsigned outstanding : {1U, 2U, 3U, 4U})
        for (const unsigned id_width : {0U, 1U, 2U, 3U, 4U})
          for (const unsigned k : {1U, 2U, 3U, 4U})
            for (const int granule : {0, 4, 8, 16, 32}) {
              if (granule && static_cast<unsigned>(granule) < target_width / 8)
                continue;
              Ends e{width, outstanding, id_width, k};
              e.granule = granule;
              e.target_width = target_width;
              const auto plan = plan_of(e);
              INFO(width << " " << target_width << " " << outstanding << " " << id_width << " " << k
                         << " " << granule);
              DependencyGraph graph;
              CHECK(checked(plan, dependency_facts(plan), &graph).findings.empty());
            }
}

TEST_CASE("Inside a block the word waits for the next, unlike a word-by-word target",
          "[dependency]") {
  const auto ahead_of = [](int granule) {
    Ends e{32, 4, 2, 4};
    e.granule = granule;
    const auto plan = plan_of(e);
    DependencyGraph graph;
    checked(plan, dependency_facts(plan), &graph);
    return graph.model.ahead.size();
  };
  CHECK(ahead_of(8) < ahead_of(4));
  CHECK(ahead_of(16) == ahead_of(8));
  CHECK(ahead_of(0) == ahead_of(8));
}

TEST_CASE("Undoing per-read gathers brings back the interleaved hang", "[dependency]") {
  const Ends gathered{64, 4, 2, 4};
  const auto plan = plan_of(gathered);
  REQUIRE(plan.dependency_report.findings.empty());
  auto facts = dependency_facts(plan);
  REQUIRE(std::ranges::all_of(facts.segment_ends, [](bool b) { return b; }));
  facts.segment_ends.assign(facts.segment_ends.size(), false);
  DependencyGraph graph;
  const auto report = checked(plan, facts, &graph);
  REQUIRE_FALSE(report.findings.empty());
  CHECK(kinds(graph, report.findings.front()).count("grant.held") == 2);
}

TEST_CASE("A loop through the request and response sides is reported at the endpoints",
          "[dependency]") {
  const auto plan = plan_of(Ends{32, 1, 2, 1});
  REQUIRE(plan.dependency_report.findings.empty());
  auto facts = dependency_facts(plan);
  // The target's answer needs its own request to go on: response blocked by request, against S2.
  facts.edges.push_back({{"endpoint.target.req", "t0"}, {"endpoint.target.rsp", "t0"}});
  DependencyGraph graph;
  auto report = checked(plan, facts, &graph);
  REQUIRE(report.findings.size() == 1);
  CHECK(report.findings.front().level == "endpoint");
  CHECK(has_rule(report.findings.front(), DependencyRule::External));
  CHECK(has_rule(report.findings.front(), DependencyRule::ResponseBlocksAccept));
  // A bridge outside the network: t0 forwards to s1, which reaches t0 again.
  facts = dependency_facts(plan);
  CHECK(checked(plan, facts).findings.empty());
  facts.edges.push_back({{"endpoint.source.req", "s1"}, {"endpoint.target.req", "t0"}});
  CHECK_FALSE(checked(plan, facts).findings.empty());
}

TEST_CASE("An escape counts only when it leaves the cycle", "[dependency]") {
  const Ends gathered{64, 4, 2, 4};
  const auto plan = plan_of(gathered);
  auto facts = dependency_facts(plan);
  facts.segment_ends.assign(facts.segment_ends.size(), false);
  DependencyGraph graph;
  const auto report = checked(plan, facts, &graph);
  REQUIRE_FALSE(report.findings.empty());
  // Every grant of the cycle set may wait for something else instead of the word behind it.
  std::vector<std::pair<DependencyNodeRef, DependencyNodeRef>> entries;
  const auto &nodes = graph.nodes;
  for (const auto &e : graph.edges)
    if (e.rule == DependencyRule::Continuation)
      entries.push_back(
          {{nodes[e.from].kind, nodes[e.from].key}, {nodes[e.to].kind, nodes[e.to].key}});
  REQUIRE_FALSE(entries.empty());
  // Out of the cycle: to a word's consumer, which is outside the network.
  auto out = facts;
  for (const auto &[blocker, blocked] : entries)
    out.escapes.push_back({blocked, blocker, {"env.consumer", "s0"}});
  CHECK(checked(plan, out).findings.empty());
  // Back into it: each to the head of the same stuck target, so nothing frees.
  auto back = facts;
  for (const auto &[blocker, blocked] : entries)
    back.escapes.push_back({blocked, blocker, {"target.head", "t0/s0"}});
  DependencyGraph caught;
  const auto again = checked(plan, back, &caught);
  CHECK_FALSE(again.findings.empty());
  CHECK(std::ranges::any_of(caught.edges, [](const auto &e) { return e.group != 0; }));
  CHECK(std::ranges::all_of(caught.nodes, [](const auto &n) { return n.rank == 0; }));
}

TEST_CASE("Every example has no cycle, ranks that rise and exclusions with a liveness shape",
          "[dependency]") {
  for (const auto &file : example_files()) {
    INFO(file.filename().string());
    const auto plan = plan_of(read_text(file));
    CHECK(plan.dependency_report.findings.empty());
    for (const auto &e : plan.dependency.edges)
      CHECK(plan.dependency.nodes[e.from].rank < plan.dependency.nodes[e.to].rank);
    for (const auto &x : plan.dependency.excluded)
      CHECK_FALSE(x.liveness.empty());
    // The same inputs give the same graph.
    const auto again = plan_of(read_text(file));
    CHECK(again.dependency == plan.dependency);
  }
}

TEST_CASE("The graph's grants and stages are the plan's", "[dependency]") {
  for (const auto &file : example_files()) {
    INFO(file.filename().string());
    const auto plan = plan_of(read_text(file));
    std::set<std::string> planned, graphed;
    for (const auto &r : plan.graph.resources)
      if (r.role == "packet_grant" || r.role == "edge_pipeline" || r.role == "link_pipeline" ||
          r.role == "endpoint_pipeline")
        planned.insert(r.id);
    for (const auto &n : plan.dependency.nodes)
      if (!n.binding.empty())
        graphed.insert(n.binding);
    for (const auto &source : plan.sources)
      if (source.qos.picks()) {
        planned.insert("source/" + source.name + "/regulator.control_valid");
        planned.insert("source/" + source.name + "/regulator.data_held");
      }
    // A crossing has its own record: its stage names its clock cut.
    std::erase_if(graphed, [&](const std::string &b) { return b.starts_with("cut/"); });
    CHECK(planned == graphed);
    std::set<std::string> cuts, cut_nodes;
    for (const auto &cut : plan.cuts)
      cuts.insert(cut.name);
    for (const auto &n : plan.dependency.nodes)
      if (n.kind == "occupancy.crossing")
        cut_nodes.insert(n.binding);
    CHECK(cuts == cut_nodes);
  }
}

TEST_CASE("A response grant's release reads the segment end exactly when the graph says",
          "[dependency]") {
  for (const auto &shape : {Ends{64, 4, 2, 4}, single_id}) {
    const auto plan = plan_of(shape);
    const auto hardware = lower_axi4_exchange_composition(plan);
    REQUIRE(hardware);
    const auto &h = hardware.value();
    const auto ends = dependency_facts(plan).segment_ends;
    const bool expected = std::ranges::any_of(ends, [](bool b) { return b; });
    std::set<std::size_t> cuts;
    for (const auto &[name, id] : h.bindings)
      if (name.ends_with("/hint.cut"))
        cuts.insert(id.index);
    REQUIRE_FALSE(cuts.empty());
    for (const auto &[name, id] : h.bindings)
      if (name.starts_with("graph/rsp/") && name.ends_with(".tail")) {
        INFO(name);
        bool reads = false;
        walk_cone(
            h.ir, id, [](ValueId) {}, [](ValueId) {},
            [&](ValueId v) {
              // An AND with constant zero reads nothing: the mask of a source without segments.
              const auto &value = h.ir.values[v.index];
              const auto zero = [&](ValueId x) {
                return h.ir.values[x.index].op == Op::Constant && h.ir.values[x.index].literal == 0;
              };
              if (value.op == Op::BitAnd && (zero(value.operands[0]) || zero(value.operands[1])))
                return true;
              reads = reads || cuts.contains(v.index);
              return false;
            });
        CHECK(reads == expected);
      }
  }
}

TEST_CASE("A report fails planning only when the rollout switch rejects", "[dependency]") {
  const auto plan = plan_of(single_id);
  DependencyGraph graph;
  const auto report = checked(plan, held(plan), &graph);
  CHECK_FALSE(dependency_rejects);
  CHECK(dependency_gate(graph, report, false));
  const auto gated = dependency_gate(graph, report, true);
  REQUIRE_FALSE(gated);
  CHECK(gated.diagnostics().front().code == "NCL_DEPENDENCY_CYCLE");
  CHECK_FALSE(gated.diagnostics().front().notes.empty());
}

TEST_CASE("The dependency artifact is deterministic and names no path", "[dependency]") {
  const auto plan = plan_of(single_id);
  DependencyGraph graph;
  const auto report = checked(plan, held(plan), &graph);
  const auto text =
      dependency_graph_json(graph, report, "m", "NocLoom", fnv1a64("a"), fnv1a64("b"));
  CHECK(text == dependency_graph_json(graph, report, "m", "NocLoom", fnv1a64("a"), fnv1a64("b")));
  CHECK(text.find("\"status\": \"cycle\"") != std::string::npos);
  CHECK(text.find("\"format_version\": 1") != std::string::npos);
  CHECK(fnv1a64("") == "fnv1a64:cbf29ce484222325");
}

TEST_CASE("Every exclusion names a liveness shape of the formal test", "[dependency]") {
  const auto shapes = read_text(std::string(NOCLOOM_SOURCE_DIR) + "/tests/run_liveness_formal.py");
  std::set<std::string> rules;
  for (const auto &file : example_files())
    for (const auto &x : plan_of(read_text(file)).dependency.excluded) {
      rules.insert(x.rule);
      CHECK(shapes.find("shape('" + x.liveness + "'") != std::string::npos);
    }
  auto one_answer = single_id;
  one_answer.read_only = true;
  const auto one_plan = plan_of(one_answer);
  DependencyGraph one_graph;
  checked(one_plan, held(one_plan), &one_graph);
  for (const auto &x : one_graph.excluded) {
    rules.insert(x.rule);
    CHECK(shapes.find("shape('" + x.liveness + "'") != std::string::npos);
  }
  CHECK(rules.contains("X1"));
  CHECK(rules.contains("X2"));
}

namespace {
std::string number_name(const std::vector<std::pair<std::uint32_t, std::string>> &names,
                        std::uint32_t id) {
  const auto it = std::ranges::find(names, id, [](const auto &row) { return row.first; });
  REQUIRE(it != names.end());
  return it->second;
}
// The endpoint slots whose words may change packet after a word that is not a tail, read from the
// lowered hardware and not from the plan: a segment end and more than one read slot.
std::set<std::string> crossing_in_hardware(const ExchangeHardware &h) {
  std::set<std::string> out;
  for (const auto &t : h.plan.targets)
    if (h.bindings.contains("burst_service/" + t.name + "/hint.cut") &&
        h.bindings.contains("burst_service/" + t.name + "/r.slot1.valid"))
      out.insert("target/" + t.name);
  return out;
}
std::set<std::string> crossing_in_model(const DependencyModel &m) {
  std::set<std::string> out;
  for (const auto at : m.cross)
    if (const auto name = number_name(m.slots, at); std::ranges::binary_search(m.endpoints, at))
      out.insert(name);
  return out;
}
bool has_port(const HardwareIR &ir, const std::string &name) {
  return std::ranges::any_of(ir.ports, [&](const auto &p) { return p.name == name; });
}
std::vector<Ends> model_shapes() {
  auto multi = single_id;
  multi.multi_hop = true;
  auto read_only = single_id;
  read_only.read_only = true;
  return {single_id, multi, read_only, Ends{64, 4, 2, 4}, Ends{64, 4, 2, 1}, Ends{32, 1, 2, 1}};
}
} // namespace

TEST_CASE("Tables the graph searches are sorted, and every grant held across a read has its edge",
          "[dependency]") {
  auto multi = single_id;
  multi.multi_hop = true;
  const auto plan = plan_of(multi);
  DependencyGraph g;
  checked(plan, held(plan), &g);
  const auto &m = g.model;
  REQUIRE(m.present);
  CHECK(std::ranges::is_sorted(m.cross));
  CHECK(std::ranges::is_sorted(m.contig));
  CHECK(std::ranges::is_sorted(m.endpoints));
  const auto node_of = [](const auto &table, std::uint32_t a, std::uint32_t b) {
    const auto it = std::ranges::find_if(
        table, [&](const auto &row) { return std::get<0>(row) == a && std::get<1>(row) == b; });
    REQUIRE(it != table.end());
    return std::get<2>(*it);
  };
  std::size_t checked = 0;
  for (const auto &[at, dest, out] : m.routes)
    if (std::ranges::binary_search(m.cross, at) &&
        std::ranges::binary_search(m.may_hold, std::tuple{at, dest}))
      for (const auto &[at2, dest2, out2] : m.routes)
        if (at2 == at && out2 != out) {
          const auto from = node_of(m.heads, at, dest2), to = node_of(m.grants, out, at);
          CHECK(std::ranges::any_of(g.edges, [&](const auto &e) {
            return e.from == from && e.to == to && e.rule == DependencyRule::Continuation;
          }));
          ++checked;
        }
  CHECK(checked > 0);
}

TEST_CASE("The model's stream labels are what the lowered hardware does", "[dependency]") {
  std::vector<NetworkPlan> plans;
  std::ranges::transform(model_shapes(), std::back_inserter(plans),
                         [](const auto &shape) { return plan_of(shape); });
  for (const auto &file : example_files())
    if (!file.filename().string().starts_with("scale_"))
      plans.push_back(plan_of(read_text(file)));
  for (const auto &plan : plans) {
    INFO(plan.name);
    const auto &m = plan.dependency.model;
    if (!m.present)
      continue;
    const auto hardware = lower_axi4_exchange_composition(plan);
    REQUIRE(hardware);
    const auto &h = hardware.value();
    // A slot said to cross has what it takes in the hardware; the cut decision is checked apart.
    const auto capable = crossing_in_hardware(h);
    for (const auto &name : crossing_in_model(m))
      CHECK(capable.contains(name));
    // A slot is one or the other, and each names its number.
    for (const auto at : m.cross)
      CHECK_FALSE(std::ranges::binary_search(m.contig, at));
    for (const auto at : m.endpoints)
      CHECK(
          (std::ranges::binary_search(m.cross, at) || std::ranges::binary_search(m.contig, at) ||
           std::ranges::any_of(m.ahead, [&](const auto &row) { return std::get<0>(row) == at; })));
    for (std::size_t i = 0; i < m.slots.size(); ++i)
      CHECK(m.slots[i].first == i);
    for (std::size_t i = 0; i < m.destinations.size(); ++i)
      CHECK(m.destinations[i].first == i);
    // A bound destination is a source that cannot have two targets answering: one direction with
    // no AXI ID on its ports, or one transfer.
    for (const auto dest : m.bound) {
      const auto name = number_name(m.destinations, dest);
      const auto s =
          plan.sources.at(std::ranges::find(plan.sources, name.substr(7), &SourcePlan::name) -
                          plan.sources.begin());
      const auto port = "nl_i_" + s.name + "_";
      const bool one_direction =
          !has_port(h.ir, port + "awvalid") || !has_port(h.ir, port + "arvalid");
      const bool no_id = !has_port(h.ir, port + "arid") && !has_port(h.ir, port + "awid");
      CHECK(((one_direction && no_id) || s.outstanding <= 1));
    }
  }
}

TEST_CASE("A bound destination is a source with one direction and no ID at its ports",
          "[dependency]") {
  auto read_only = single_id;
  read_only.read_only = true;
  const auto plan = plan_of(read_only);
  DependencyGraph graph;
  checked(plan, held(plan), &graph);
  const auto hardware = lower_axi4_exchange_composition(plan);
  REQUIRE(hardware);
  std::size_t seen = 0;
  for (const auto &s : plan.sources) {
    const auto port = "nl_i_" + s.name + "_";
    CHECK_FALSE(has_port(hardware.value().ir, port + "awvalid"));
    CHECK_FALSE(has_port(hardware.value().ir, port + "arid"));
    ++seen;
  }
  CHECK(seen == 2);
  CHECK(std::ranges::any_of(graph.excluded, [](const auto &x) { return x.rule == "X1"; }));
}

namespace {
// The nodes standing for a resource of the plan wait list; empty for one with no node.
std::vector<std::uint32_t> nodes_of_resource(const DependencyGraph &g, const std::string &id) {
  std::vector<std::uint32_t> out;
  const auto named = [&](const std::string &kind, const std::string &key) {
    for (std::uint32_t i = 0; i < g.nodes.size(); ++i)
      if (g.nodes[i].kind == kind && g.nodes[i].key == key)
        out.push_back(i);
  };
  const auto part = [&](std::size_t at) {
    const auto end = id.find('/', at);
    return id.substr(at, end == std::string::npos ? end : end - at);
  };
  if (id.starts_with("graph/")) {
    for (std::uint32_t i = 0; i < g.nodes.size(); ++i)
      if (g.nodes[i].binding == id)
        out.push_back(i);
  } else if (id.starts_with("source/") && id.ends_with("/return")) {
    named("source.staging", part(7));
  } else if (id.starts_with("source/") && id.ends_with("/consumer")) {
    named("env.consumer", part(7));
  } else if (id.starts_with("source/") && (id.ends_with("/read") || id.ends_with("/write"))) {
    named("source.merge", part(7));
  } else if (id.starts_with("burst_service/") &&
             (id.ends_with("/read") || id.ends_with("/write"))) {
    named("target.pool", part(14));
  }
  return out;
}
bool reaches_any(const DependencyGraph &g, const std::vector<std::uint32_t> &from,
                 const std::vector<std::uint32_t> &to) {
  std::vector<char> seen(g.nodes.size(), 0);
  std::vector<std::uint32_t> todo = from;
  for (const auto u : from)
    seen[u] = 1;
  while (!todo.empty()) {
    const auto u = todo.back();
    todo.pop_back();
    if (std::ranges::find(to, u) != to.end())
      return true;
    for (auto e = std::ranges::lower_bound(g.edges, u, {}, &DependencyEdge::from);
         e != g.edges.end() && e->from == u; ++e)
      if (!seen[e->to]) {
        seen[e->to] = 1;
        todo.push_back(e->to);
      }
  }
  return false;
}
} // namespace

TEST_CASE("The plan wait list's capacity waits are paths of the graph", "[dependency]") {
  std::size_t mapped = 0;
  std::map<std::string, std::size_t> left_out;
  for (const auto &file : example_files()) {
    if (file.filename().string().starts_with("scale_"))
      continue;
    INFO(file.filename().string());
    const auto plan = plan_of(read_text(file));
    const auto &g = plan.dependency;
    for (const auto &w : plan.graph.waits) {
      const auto waiter = nodes_of_resource(g, w.from), awaited = nodes_of_resource(g, w.to);
      // Startup and reserved-write waits are not stable-state waits: reset release, and a permit
      // held by the reservation. A wait on or from what has no node is listed below.
      if (w.kind == CrossWaitKind::Startup || w.kind == CrossWaitKind::ReservedWrite)
        continue;
      if (waiter.empty() || awaited.empty()) {
        ++left_out[w.to.substr(0, w.to.find('/'))];
        continue;
      }
      INFO(w.from << " waits " << w.to);
      CHECK(reaches_any(g, awaited, waiter));
      ++mapped;
    }
  }
  CHECK(mapped > 100);
  // Stated: a domain clock, a limiter or regulator, a shared bus service and a target's outside
  // are the environment of these resources; the graph takes them as assumptions F3, F4 and F5.
  for (const auto &[kind, count] : left_out) {
    INFO(kind << " " << count);
    CHECK((kind == "domain" || kind == "source" || kind == "burst_service" || kind == "target" ||
           kind == "cut"));
  }
}

TEST_CASE("Each rejection of a wait table is rejected by the graph, or has no form",
          "[dependency]") {
  const auto plan = plan_of(Ends{32, 1, 2, 1});
  // Two storages waiting on each other, as an injected table did (no plan ever produced one).
  auto facts = dependency_facts(plan);
  facts.edges.push_back({{"source.merge", "s0"}, {"target.pool", "t0"}});
  facts.edges.push_back({{"target.pool", "t0"}, {"source.merge", "s0"}});
  CHECK_FALSE(checked(plan, facts).findings.empty());
  // A resource waiting on itself.
  facts = dependency_facts(plan);
  facts.edges.push_back({{"source.merge", "s0"}, {"source.merge", "s0"}});
  CHECK_FALSE(checked(plan, facts).findings.empty());
  // A wait naming an absent resource has no form: the graph's edges come from its own routes, and
  // an alternative naming a node that does not exist stops the build.
  facts = dependency_facts(plan);
  facts.escapes.push_back({{"source.merge", "s0"}, {"target.pool", "t0"}, {"no.such", "node"}});
  CHECK_THROWS_AS(build_dependency_graph(plan, facts), std::logic_error);
}

namespace {
// Reads a response tail's cone for a source: the comparisons with the source field are decided, a
// mask of zero reads nothing, and what stands behind a register is not read (stages are skipped).
class SegmentCone {
public:
  SegmentCone(const ExchangeHardware &h, unsigned source_index) : h(h), flow(2 * source_index + 1) {
    for (const auto &[name, id] : h.bindings)
      if (name.ends_with("/hint.cut"))
        cuts.insert(id.index);
  }
  bool reads(ValueId v) {
    std::set<std::size_t> seen;
    return walk(v, seen);
  }

private:
  const ExchangeHardware &h;
  std::uint64_t flow;
  std::set<std::size_t> cuts;
  // 1 or 0 when known under this source, -1 otherwise.
  int known(ValueId v, unsigned depth = 0) const {
    const auto &value = h.ir.values[v.index];
    if (value.op == Op::Constant)
      return value.literal != 0;
    if (depth > 64)
      return -1;
    if (value.op == Op::Equal) {
      const auto &right = h.ir.values[value.operands[1].index];
      const auto &left = h.ir.values[value.operands[0].index];
      if (right.op == Op::Constant && left.width == h.plan.source_bits && left.width > 1)
        return right.literal == flow;
      return -1;
    }
    if (value.op == Op::BitOr || value.op == Op::BitAnd) {
      const auto a = known(value.operands[0], depth + 1), b = known(value.operands[1], depth + 1);
      const int dominant = value.op == Op::BitOr ? 1 : 0;
      if (a == dominant || b == dominant)
        return dominant;
      return a >= 0 && b >= 0 ? 1 - dominant : -1;
    }
    return -1;
  }
  bool walk(ValueId v, std::set<std::size_t> &seen) {
    if (cuts.contains(v.index))
      return true;
    if (!seen.insert(v.index).second || known(v) == 0)
      return false;
    const auto &value = h.ir.values[v.index];
    if (value.op == Op::State || value.op == Op::Input || value.op == Op::Constant)
      return false;
    for (unsigned i = 0; i < operand_count(value.op); ++i)
      if (walk(value.operands[i], seen))
        return true;
    return false;
  }
};
std::string source_of_destination(const std::string &name) {
  if (name.starts_with("source/"))
    return name.substr(7);
  const auto gt = name.find('>'), end = name.find('/', gt);
  return name.substr(gt + 1, end == std::string::npos ? end : end - gt - 1);
}
struct ConeCount {
  std::size_t compared = 0, released = 0, held = 0;
};
// Whether each response output's candidate releases at a segment end exactly as the table says,
// with the rows the model exports; mismatches by name.
std::vector<std::string> cut_mismatches(const ExchangeHardware &h, const DependencyModel &m,
                                        ConeCount *count = nullptr) {
  std::set<std::pair<std::string, std::string>> ahead;
  for (const auto &[at, dest] : m.ahead)
    ahead.insert(
        {number_name(m.slots, at), source_of_destination(number_name(m.destinations, dest))});
  const auto &plan = h.plan;
  const auto stages = [&](const std::string &from, const std::string &to) {
    const auto link = std::ranges::find(plan.links, "rsp/" + from + ">" + to, &LinkPlan::name);
    return link != plan.links.end() && !link->stages.empty();
  };
  std::vector<std::string> out;
  // A target's own input answers per source; a link between switches carries every packet routed
  // over it, whichever output of the next switch this one is.
  std::map<std::pair<std::string, std::string>, bool> want;
  std::map<std::pair<std::string, std::string>, bool> over_link;
  std::set<std::pair<std::string, std::string>> skipped;
  for (const auto &route : plan.exchange.response.routes) {
    const bool cuts = ahead.contains({"target/" + route.entry, route.exit});
    auto from = route.entry;
    bool staged = false;
    for (std::size_t i = 0; i < route.nodes.size(); ++i) {
      staged = staged || stages(from, route.nodes[i]);
      if (i > 0) {
        auto &row = over_link[{route.nodes[i - 1], route.nodes[i]}];
        row = row || cuts;
      }
      from = route.nodes[i];
    }
    const auto &last = route.nodes.back();
    const auto key = "graph/rsp/" + last + ">endpoint/" + route.exit;
    const auto candidate = route.nodes.size() > 1 ? "node/" + route.nodes[route.nodes.size() - 2]
                                                  : "endpoint/" + route.entry;
    want[{key, candidate}] = false;
    if (staged)
      skipped.insert({key, candidate});
    if (route.nodes.size() == 1)
      want[{key, candidate}] = cuts;
  }
  for (auto &[pair, row] : want)
    if (pair.second.starts_with("node/")) {
      const auto node = pair.first.substr(10, pair.first.find('>') - 10);
      const auto it = over_link.find({pair.second.substr(5), node});
      row = it != over_link.end() && it->second;
    }
  for (const auto &[name, arbiter] : plan.arbiters) {
    if (!arbiter.response || !name.starts_with("rsp/"))
      continue;
    const auto key = "graph/" + name;
    const auto source = name.substr(name.rfind('/') + 1);
    const auto at = std::ranges::find(plan.sources, source, &SourcePlan::name);
    if (at == plan.sources.end())
      continue;
    SegmentCone cone(h, static_cast<unsigned>(at - plan.sources.begin()));
    for (std::size_t i = 0; i < arbiter.candidates.size(); ++i) {
      const auto pair = std::pair{key, arbiter.candidates[i]};
      if (skipped.contains(pair) || !want.contains(pair))
        continue;
      const auto tail = h.bindings.find(key + ".last" + std::to_string(i));
      if (tail == h.bindings.end()) {
        out.push_back(key + " from " + arbiter.candidates[i] + " lacks tail binding");
        continue;
      }
      const bool reads = cone.reads(tail->second);
      if (count) {
        ++count->compared;
        ++(reads ? count->released : count->held);
      }
      if (reads != want.at(pair))
        out.push_back(key + " from " + arbiter.candidates[i] + (reads ? " reads" : " lacks") +
                      " cut");
    }
  }
  return out;
}
} // namespace

TEST_CASE("The exported cut decision is the release cone of the hardware on every example",
          "[.dependency-cone]") {
  ConeCount total;
  for (const auto &file : example_files()) {
    INFO(file.filename().string());
    const auto plan = plan_of(read_text(file));
    if (!plan.dependency.model.present || plan.arbiters.empty())
      continue;
    const auto hardware = lower_axi4_exchange_composition(plan);
    REQUIRE(hardware);
    const auto wrong = cut_mismatches(hardware.value(), plan.dependency.model, &total);
    INFO((wrong.empty() ? std::string() : wrong.front()));
    CHECK(wrong.empty());
  }
  // Interleaving shapes the examples do not all have, including held grants.
  for (const auto &shape : model_shapes()) {
    const auto plan = plan_of(shape);
    const auto hardware = lower_axi4_exchange_composition(plan);
    REQUIRE(hardware);
    const auto wrong = cut_mismatches(hardware.value(), plan.dependency.model, &total);
    INFO((wrong.empty() ? std::string() : wrong.front()));
    CHECK(wrong.empty());
  }
  CHECK(total.compared > 100);
  CHECK(total.released > 0);
  CHECK(total.held > 0);
}

TEST_CASE("A forged or missing cut decision differs from the release cone", "[dependency]") {
  const auto plan = plan_of(Ends{64, 4, 2, 4});
  const auto hardware = lower_axi4_exchange_composition(plan);
  REQUIRE(hardware);
  // A plan that calls its targets non-interleaving says no target cuts.
  auto facts = dependency_facts(plan);
  facts.interleaved.assign(facts.interleaved.size(), false);
  CHECK_FALSE(cut_mismatches(hardware.value(), build_dependency_graph(plan, facts).model).empty());
  auto model = plan.dependency.model;
  REQUIRE_FALSE(model.ahead.empty());
  CHECK(cut_mismatches(hardware.value(), model).empty());
  auto missing_tail = hardware.value();
  const auto response = std::ranges::find_if(plan.arbiters, [](const auto &row) {
    return row.second.response && row.first.starts_with("rsp/") &&
           row.first.find(">endpoint/") != std::string::npos;
  });
  REQUIRE(response != plan.arbiters.end());
  REQUIRE(missing_tail.bindings.erase("graph/" + response->first + ".last0") == 1);
  const std::vector<std::string> missing{"graph/" + response->first + " from " +
                                         response->second.candidates.front() +
                                         " lacks tail binding"};
  CHECK(cut_mismatches(missing_tail, model) == missing);
  model.ahead.pop_back();
  CHECK_FALSE(cut_mismatches(hardware.value(), model).empty());
  // A row saying a shared-bus target ends segments, which its grants do not read.
  const auto bus_plan =
      plan_of(read_text(std::string(NOCLOOM_SOURCE_DIR) + "/examples/axi_lite_apb.yaml"));
  const auto bus_hardware = lower_axi4_exchange_composition(bus_plan);
  REQUIRE(bus_hardware);
  auto forged = bus_plan.dependency.model;
  REQUIRE(forged.present);
  CHECK(cut_mismatches(bus_hardware.value(), forged).empty());
  for (const auto &[at, dest, out] : forged.routes)
    if (number_name(forged.slots, at).starts_with("target/"))
      forged.ahead.push_back({at, dest});
  std::ranges::sort(forged.ahead);
  CHECK_FALSE(cut_mismatches(bus_hardware.value(), forged).empty());
}

TEST_CASE("Replay descriptors retain pool entries and share the return stage", "[dependency]") {
  const auto plan = plan_of(
      R"({schema_version: 10, name: shape, address_width: 32, arbitration: {policy: round_robin},
 reset_ports: [{name: reset_n}], test_ports: [{name: test}],
 clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}],
 initiators: [{name: s0, clock_domain: main, protocol: axi4, data_width: 32, max_outstanding: 2,
   id_width: 2, len_width: 8}, {name: s1, clock_domain: main, protocol: axi4, data_width: 32,
   max_outstanding: 2, id_width: 2, len_width: 8}],
 targets: [{name: t0, clock_domain: main, protocol: axi4, data_width: 64, attributes: amba,
   max_outstanding: 2, id_width: 4, len_width: 8, mappings: [{base: '0x0', size: '0x8000'}]},
   {name: t1, clock_domain: main, protocol: axi4, data_width: 32, attributes: amba,
   max_outstanding: 2, id_width: 4, len_width: 8, mappings: [{base: '0x8000', size: '0x8000'}]}],
 connectivity: all, output: {backend: verilog, language: verilog_2001}})");
  const auto &g = plan.dependency;
  CHECK(plan.dependency_report.findings.empty());
  const auto named = [&](const std::string &kind, const std::string &key) {
    const auto it = std::ranges::find_if(
        g.nodes, [&](const auto &n) { return n.kind == kind && n.key == key; });
    REQUIRE(it != g.nodes.end());
    return static_cast<std::uint32_t>(it - g.nodes.begin());
  };
  const auto stored = named("merge.stored", "t0/s0");
  CHECK(std::ranges::any_of(g.edges, [&](const auto &e) {
    return e.from == stored && e.to == named("target.pool", "t0") &&
           e.rule == DependencyRule::StoredRequest;
  }));
  CHECK(std::ranges::any_of(g.edges, [&](const auto &e) {
    return e.to == stored && e.rule == DependencyRule::Downstream;
  }));
  CHECK(reaches_any(g, {stored}, {named("target.pool", "t0")}));
  CHECK(reaches_any(g, {stored}, {named("target.head", "t0/s1")}));
  // A stored word for s0 does not wait for the next word to s0, or the target for itself.
  CHECK_FALSE(reaches_any(g, {stored}, {named("target.head", "t0/s0")}));
  // With grants held across reads, the stored word's wait closes a cycle like any queued word.
  CHECK_FALSE(checked(plan, held(plan)).findings.empty());
}
