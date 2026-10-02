// Schema 10 connectivity matrix: YAML and C++ forms, every diagnostic, byte
// equivalences and the structure of the generated sparse crossbar. Transaction
// behavior (unconnected pairs get DECERR through the default target adapter without a target
// bus handshake) is checked by tests/verif/cases/connectivity.py.
#include "../endpoint_ports.hpp"
#include "../graph_example_definition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <set>

using namespace nocloom;

namespace {
// N sources s<i> and M targets t<j> (64 KiB windows) without topology.
Definition crossbar(unsigned sources, unsigned targets) {
  Definition d;
  d.schema_version = 10;
  d.name = "matrix";
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  for (unsigned i = 0; i < sources; ++i)
    d.initiators.push_back(graph_source("s" + std::to_string(i)));
  for (unsigned j = 0; j < targets; ++j)
    d.targets.push_back(graph_target("t" + std::to_string(j), {65536ULL * j, 65536}));
  endpoint_ports(d, 2, 8);
  return d;
}
Connectivity pairs(std::vector<ConnectivityEntry> entries) {
  return Connectivity{.pairs = std::move(entries)};
}
template <typename T>
void rejected(const Result<T> &r, const std::string &code, const std::string &field) {
  REQUIRE_FALSE(r);
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field << ": " << d.message);
  REQUIRE(std::ranges::any_of(
      r.diagnostics(), [&](const Diagnostic &d) { return d.code == code && d.field == field; }));
}
nlohmann::json map_of(const ArtifactSet &artifacts) {
  const auto it = std::ranges::find_if(
      artifacts, [](const auto &artifact) { return artifact.path.ends_with(".map.json"); });
  REQUIRE(it != artifacts.end());
  return nlohmann::json::parse(it->text);
}
std::string rtl_of(const ArtifactSet &artifacts) {
  const auto it = std::ranges::find_if(artifacts, [](const auto &artifact) {
    return artifact.path.ends_with(".v") && artifact.path != "nocloom_clock_gate.v" &&
           artifact.path != "nocloom_synchronizer.v";
  });
  REQUIRE(it != artifacts.end());
  return it->text;
}
const std::string yaml_head = R"(schema_version: 10
name: matrix
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}
  - {name: s1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}
  - {name: s2, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}
targets:
  - {name: t0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x10000'}], len_width: 8, attributes: amba}
  - {name: t1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x10000', size: '0x10000'}], len_width: 8, attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
} // namespace

TEST_CASE("Connectivity is required for every network", "[connectivity]") {
  auto d = crossbar(2, 2);
  rejected(make_plan(d), "NCL_CONNECTIVITY_REQUIRED", "connectivity");
  // One source or one target still writes the matrix; all and the full
  // list are the same network.
  for (const auto &[n, m] : {std::pair{1U, 3U}, std::pair{3U, 1U}, std::pair{1U, 1U}}) {
    auto omitted = crossbar(n, m);
    rejected(make_plan(omitted), "NCL_CONNECTIVITY_REQUIRED", "connectivity");
    REQUIRE_FALSE(generate(omitted));
    auto all = omitted;
    all.connectivity = Connectivity{.all = true};
    auto listed = omitted;
    listed.connectivity.emplace();
    for (const auto &i : omitted.initiators)
      for (const auto &t : omitted.targets)
        listed.connectivity->pairs.push_back({i.name, t.name});
    const auto b = generate(all), c = generate(listed);
    REQUIRE(b);
    REQUIRE(c);
    CHECK(b.value() == c.value());
  }
}

TEST_CASE("Connectivity diagnostics name the matrix entry", "[connectivity]") {
  auto d = crossbar(2, 3);
  d.connectivity = pairs({{"s0", "t0"}, {"s0", "t1"}, {"s1", "t2"}});
  REQUIRE(make_plan(d));
  auto bad = d;
  bad.connectivity->pairs.push_back({"s9", "t0"});
  rejected(make_plan(bad), "NCL_CONNECTIVITY_REFERENCE", "connectivity.s9");
  bad = d;
  bad.connectivity->pairs.push_back({"s1", "t9"});
  rejected(make_plan(bad), "NCL_CONNECTIVITY_REFERENCE", "connectivity.s1[1]");
  bad = d;
  bad.connectivity->pairs.push_back({"s0", "t1"});
  rejected(make_plan(bad), "NCL_CONNECTIVITY_DUPLICATE", "connectivity.s0[2]");
  bad = d;
  bad.connectivity->all = true;
  rejected(make_plan(bad), "NCL_CONNECTIVITY_DUPLICATE", "connectivity");
  bad = d;
  std::erase_if(bad.connectivity->pairs, [](const auto &p) { return p.source == "s1"; });
  rejected(make_plan(bad), "NCL_CONNECTIVITY_UNUSED_SOURCE", "connectivity");
  rejected(make_plan(bad), "NCL_CONNECTIVITY_UNUSED_TARGET", "connectivity");
  bad = d;
  bad.connectivity->pairs.clear();
  rejected(make_plan(bad), "NCL_CONNECTIVITY_UNUSED_SOURCE", "connectivity");

  // An explicit topology must carry every connected pair on a route.
  auto graph = graph_example_definition(true);
  graph.connectivity->pairs.push_back({"s0", "t2"});
  REQUIRE(make_plan(graph));
  graph.topology->response.nodes.push_back({"lonely"});
  graph.topology->response.exits[0].node = "lonely";
  REQUIRE_FALSE(make_plan(graph));
}

TEST_CASE("YAML connectivity rows match the C++ matrix", "[connectivity]") {
  const auto parsed =
      config::parse(yaml_head + "connectivity:\n  s0: [t0, t1]\n  s1: [t1]\n  s2: [t0]\n");
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  REQUIRE(d.connectivity);
  CHECK_FALSE(d.connectivity->all);
  CHECK(d.connectivity->pairs ==
        std::vector<ConnectivityEntry>{{"s0", "t0"}, {"s0", "t1"}, {"s1", "t1"}, {"s2", "t0"}});
  CHECK(parsed.value().sources.contains("connectivity.s1[0]"));
  const auto all = config::parse(yaml_head + "connectivity: all\n");
  REQUIRE(all);
  CHECK(all.value().definition.connectivity == Connectivity{.all = true});
  // Diagnostics carry the YAML location of the entry.
  const auto unknown = config::parse(yaml_head + "connectivity:\n  s0: [t0, t7]\n  s1: [t1]\n"
                                                 "  s2: [t0]\n");
  REQUIRE(unknown);
  const auto plan = make_plan(unknown.value().definition, &unknown.value().sources);
  rejected(plan, "NCL_CONNECTIVITY_REFERENCE", "connectivity.s0[1]");
  const auto located =
      std::ranges::find(plan.diagnostics(), std::string("connectivity.s0[1]"), &Diagnostic::field);
  REQUIRE(located->location);
  CHECK(located->location->line == 18);
  const auto missing = config::parse(yaml_head);
  REQUIRE(missing);
  rejected(make_plan(missing.value().definition), "NCL_CONNECTIVITY_REQUIRED", "connectivity");
  for (const auto *text : {"connectivity: some\n", "connectivity: [t0]\n",
                           "connectivity:\n  s0: t0\n", "connectivity:\n"})
    CHECK_FALSE(config::parse(yaml_head + text));
  // A route entry names an initiator, not a source: the matrix carries the pairs.
  auto routed = config::parse(yaml_head + R"(topology:
  routes: [{source: s0, target: t0}]
  request: {nodes: [{name: q}], links: [], entries: [], exits: []}
  response: {nodes: [{name: p}], links: [], entries: [], exits: []}
)");
  REQUIRE_FALSE(routed);
  CHECK(routed.diagnostics().front().code == "NCL_SCHEMA_UNKNOWN_FIELD");
  CHECK(routed.diagnostics().front().field == "topology.routes[0].source");
}

TEST_CASE("Connectivity bytes are independent of how the matrix is written", "[connectivity]") {
  auto d = crossbar(3, 3);
  d.connectivity = Connectivity{.all = true};
  const auto all = generate(d);
  REQUIRE(all);
  d.connectivity = Connectivity{};
  for (unsigned j = 3; j-- > 0;)
    for (unsigned i = 0; i < 3; ++i)
      d.connectivity->pairs.push_back({"s" + std::to_string(i), "t" + std::to_string(j)});
  const auto listed = generate(d);
  REQUIRE(listed);
  CHECK(all.value() == listed.value());
  // A sparse matrix in two orders.
  d.connectivity = pairs({{"s0", "t0"}, {"s1", "t1"}, {"s2", "t2"}, {"s2", "t0"}});
  const auto sparse = generate(d);
  REQUIRE(sparse);
  std::ranges::reverse(d.connectivity->pairs);
  std::ranges::reverse(d.initiators);
  const auto reordered = generate(d);
  REQUIRE(reordered);
  CHECK(sparse.value() == reordered.value());
  CHECK(rtl_of(sparse.value()) != rtl_of(all.value()));
}

TEST_CASE("The generated crossbar carries exactly the connected pairs", "[connectivity]") {
  // A banded 4x4 matrix, a single column and a single row of a 3x3 network.
  const std::vector<std::vector<ConnectivityEntry>> matrices = {
      {{"s0", "t0"},
       {"s0", "t1"},
       {"s1", "t1"},
       {"s1", "t2"},
       {"s2", "t2"},
       {"s2", "t3"},
       {"s3", "t3"}},
      {{"s0", "t0"},
       {"s1", "t0"},
       {"s2", "t0"},
       {"s3", "t0"},
       {"s3", "t1"},
       {"s3", "t2"},
       {"s3", "t3"}},
      {{"s0", "t0"}, {"s1", "t1"}, {"s2", "t2"}, {"s3", "t3"}},
  };
  for (const auto &matrix : matrices) {
    auto d = crossbar(4, 4);
    d.connectivity = pairs(matrix);
    const auto artifacts = generate(d);
    REQUIRE(artifacts);
    const auto map = map_of(artifacts.value());
    std::set<std::pair<std::string, std::string>> want, request, response, listed;
    for (const auto &p : matrix)
      want.emplace(p.source, p.target);
    for (const auto &r : map["network"]["request_routes"]) {
      request.emplace(r["entry"], r["exit"]);
      CHECK(r["hardware_nodes"] == nlohmann::json::array({"req_node0"}));
    }
    for (const auto &r : map["network"]["response_routes"])
      response.emplace(r["exit"], r["entry"]);
    for (const auto &p : map["definition"]["connectivity"])
      listed.emplace(p["source"], p["target"]);
    CHECK(request == want);
    CHECK(response == want);
    CHECK(listed == want);
    CHECK(map["definition"]["topology"]["generated"] == "one_switch_per_direction");
    CHECK(map["definition"]["contract"]["routing"] ==
          "generated_one_switch_per_direction_connected_inputs_only");
    // Every arbitrated request output of the generated switch lists exactly
    // its connected sources as candidates.
    for (const auto &o : map["definition"]["contract"]["arbitration"]["outputs"]) {
      if (o["graph"] != "request")
        continue;
      CHECK(o["node"] == "crossbar");
      const std::string target = o["output"].get<std::string>().substr(9);
      std::set<std::string> candidates, connected;
      for (const auto &c : o["candidates"])
        candidates.insert(c.get<std::string>());
      for (const auto &p : want)
        if (p.second == target)
          connected.insert("endpoint/" + p.first);
      CHECK(candidates == connected);
    }
    // A switch output arbitrates only when two or more connected sources (or
    // targets on the return) share it.
    std::set<std::string> grants;
    for (const auto &r : map["network"]["resources"])
      if (r["role"] == "packet_grant")
        grants.insert(r["name"]);
    for (unsigned k = 0; k < 4; ++k) {
      const auto t = "t" + std::to_string(k), s = "s" + std::to_string(k);
      const auto into = std::ranges::count_if(want, [&](const auto &p) { return p.second == t; });
      const auto back = std::ranges::count_if(want, [&](const auto &p) { return p.first == s; });
      CHECK(grants.contains("graph/req/req_node0>endpoint/" + t) == (into > 1));
      CHECK(grants.contains("graph/rsp/rsp_node0>endpoint/" + s) == (back > 1));
    }
  }
}

TEST_CASE("The generated crossbar equals an explicit one-switch graph", "[connectivity]") {
  auto generated = crossbar(3, 2);
  generated.connectivity = pairs({{"s0", "t0"}, {"s1", "t0"}, {"s1", "t1"}, {"s2", "t1"}});
  auto explicit_graph = generated;
  ExchangeTopology topology;
  topology.request.nodes = {{"q"}};
  topology.response.nodes = {{"p"}};
  for (const auto &i : generated.initiators) {
    topology.request.entries.push_back({i.name, "q"});
    topology.response.exits.push_back({i.name, "p"});
  }
  for (const auto &t : generated.targets) {
    topology.request.exits.push_back({t.name, "q"});
    topology.response.entries.push_back({t.name, "p"});
  }
  explicit_graph.topology = topology;
  const auto a = generate(generated), b = generate(explicit_graph);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(rtl_of(a.value()) == rtl_of(b.value()));
}

TEST_CASE("Unconnected and unmapped accesses answer at the default target", "[connectivity]") {
  auto d = crossbar(2, 2);
  d.connectivity = pairs({{"s0", "t0"}, {"s1", "t0"}, {"s1", "t1"}});
  const auto artifacts = generate(d);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  const auto &contract = map["definition"]["contract"]["connectivity"];
  CHECK(contract["unconnected_pair_or_address_outside_routing_windows"] ==
        "DECERR_from_the_reached_target_with_the_smallest_window_by_error_request_without_target_"
        "handshake");
  {
    auto codes = d;
    codes.transport = TransportOptions{};
    codes.transport->response_codes = ResponseCodes::Uniform;
    const auto coded = generate(codes);
    REQUIRE(coded);
    CHECK(map_of(coded.value())["definition"]["contract"]["connectivity"]
                               ["unconnected_pair_or_address_outside_routing_windows"] ==
          "SLVERR_from_the_reached_target_with_the_smallest_window_by_error_request_without_target_"
          "handshake");
  }
  // Every source has its admission match, whatever the matrix.
  REQUIRE(map["network"]["admission"].size() == 2);
  d.connectivity = Connectivity{.all = true};
  const auto all = generate(d);
  REQUIRE(all);
  CHECK(map_of(all.value())["network"]["admission"].size() == 2);
}

TEST_CASE("The default target owns the lowest based of the smallest mappings", "[connectivity]") {
  // t1 and t2 hold the smallest mappings; t2's is lower, so t2 answers misses
  // although t1 comes first by name. A larger mapping never wins.
  auto d = crossbar(1, 3);
  d.targets[0].mappings = {{0x40000, 0x20000, 0}};
  d.targets[1].mappings = {{0x20000, 0x10000, 0}};
  d.targets[2].mappings = {{0x10000, 0x10000, 0}};
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  CHECK(detail::default_target(plan.value(), "s0") == 2U);
  d.targets[2].mappings = {{0x80000, 0x20000, 0}};
  const auto larger = detail::build_network_plan(d);
  REQUIRE(larger);
  CHECK(detail::default_target(larger.value(), "s0") == 1U);
}

TEST_CASE("A block above a source's address space answers none of its misses", "[connectivity]") {
  // t0's 4 KiB block is the smallest overall but lies above s0, whose misses go to t1.
  auto d = crossbar(2, 2);
  d.address_width = 32;
  d.initiators[0].address_width = 16;
  d.targets[0].mappings = {{0x8000, 0x4000, 0}, {0x10000, 0x1000, 0}};
  d.targets[1].mappings = {{0x2000, 0x2000, 0}};
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  CHECK(detail::default_target(plan.value(), "s0") == 1U);
  CHECK(detail::default_target(plan.value(), "s1") == 0U);
}

TEST_CASE("Equal default target blocks use address order independently of access properties",
          "[connectivity]") {
  // The lowest of three equal blocks answers misses, regardless of side effects or limits.
  auto d = crossbar(1, 3);
  for (unsigned j = 0; j < 3; ++j)
    d.targets[j].mappings = {{0x2000ULL * j, 0x2000, 0}};
  d.targets[0].read_side_effects = true;
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  CHECK(detail::default_target(plan.value(), "s0") == 0U);
  d.transport->packet_limits = {{"s0", "t1", std::nullopt, 1024}};
  const auto narrowed = detail::build_network_plan(d);
  REQUIRE(narrowed);
  CHECK(detail::default_target(narrowed.value(), "s0") == 0U);
  // A mapping counts as its aligned power-of-two blocks: 12 KiB at 4 KiB holds a 4 KiB block.
  d.targets[2].mappings = {{0x5000, 0x3000, 0}};
  const auto blocks = detail::build_network_plan(d);
  REQUIRE(blocks);
  CHECK(detail::default_target(blocks.value(), "s0") == 2U);
}

TEST_CASE("Named defaults use only mappings within each source's address space",
          "[connectivity][miss-address-space]") {
  auto d = crossbar(2, 1);
  d.initiators[0].address_width = 16;
  for (auto &source : d.initiators)
    source.default_target = "t0";
  d.targets[0].mappings = {{0, 0x1000, 0}, {0x10100, 0x100, 0}};
  d.connectivity = Connectivity{.all = true};
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  const auto narrow = detail::packet_profile(plan.value(), 0);
  const auto wide = detail::packet_profile(plan.value(), 1);
  REQUIRE(narrow.fallback);
  REQUIRE(wide.fallback);
  CHECK(narrow.fallback->boundary_bytes == 4096);
  CHECK(narrow.fallback->max_bytes == 1024);
  CHECK(wide.fallback->boundary_bytes == 256);
  CHECK(wide.fallback->max_bytes == 256);
}

TEST_CASE("Named defaults choose their flow from an addressable mapping",
          "[connectivity][miss-address-space]") {
  auto d = crossbar(2, 1);
  d.initiators[0].address_width = 16;
  for (auto &source : d.initiators)
    source.default_target = "t0";
  d.targets[0].flows = 2;
  d.targets[0].mappings = {
      {0, 0x1000, 0, {}, 0}, {0x2000, 0x2000, 0, {}, 1}, {0x10100, 0x100, 0, {}, 1}};
  d.connectivity = Connectivity{.all = true};
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  const auto narrow = detail::miss_route(plan.value(), "s0");
  const auto wide = detail::miss_route(plan.value(), "s1");
  REQUIRE(narrow);
  REQUIRE(wide);
  CHECK(narrow->target_path == 0);
  CHECK(wide->target_path == 1);
}

// A mapping holding another keeps the inner mapping's alignment as its packet boundary only in
// the aligned power-of-two block that holds it: the rest of the mapping keeps the target's own
// boundary. The splitter decodes its windows first hit first.
TEST_CASE("A held mapping bounds packets only in its own block", "[connectivity][packet-blocks]") {
  auto d = crossbar(1, 2);
  d.targets[0].mappings = {{0x0, 0x30000, 0}};
  d.targets[1].mappings = {{0x10800, 0x800, 0}};
  const auto plan = detail::build_network_plan(d);
  REQUIRE(plan);
  const auto packets = detail::packet_profile(plan.value(), 0);
  auto boundary_at = [&](std::uint64_t address) {
    const auto it = std::ranges::find_if(packets.windows, [&](const auto &w) {
      return w.window.base <= address && address < w.window.base + w.window.size;
    });
    REQUIRE(it != packets.windows.end());
    return std::pair{it->group, it->boundary_bytes};
  };
  CHECK(boundary_at(0x10900) == std::pair{1U, std::uint64_t{2048}});
  CHECK(boundary_at(0x8000) == std::pair{0U, std::uint64_t{2048}});
  CHECK(boundary_at(0x28000) == std::pair{0U, std::uint64_t{4096}});
  // The locked-burst check of the source follows the same windows in the same order.
  const auto initiator = detail::initiator_profile(plan.value(), 0);
  REQUIRE(initiator.lock_windows.size() == packets.windows.size());
  for (std::size_t k = 0; k < packets.windows.size(); ++k)
    CHECK(initiator.lock_windows[k].window == packets.windows[k].window);
}

TEST_CASE("Connectivity that leaves a clocked graph segment unused is rejected", "[connectivity]") {
  // s0 and t0 in main, s1 and t1 in far; the request link a -> b and the
  // response link r -> p cross domains and carry only the pair s0 -> t1.
  auto d = crossbar(2, 2);
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"},
                     {"far", "clk_far", "reset_n", "test"}};
  d.initiators[1].clock_domain = "far";
  d.targets[1].clock_domain = "far";
  ExchangeTopology t;
  t.request = {{{"a", std::nullopt, "main"}, {"b", std::nullopt, "far"}},
               {{"a", "b"}},
               {{"s0", "a"}, {"s1", "b"}},
               {{"t0", "a"}, {"t1", "b"}}};
  t.response = {{{"p", std::nullopt, "main"}, {"r", std::nullopt, "far"}},
                {{"r", "p"}},
                {{"t0", "p"}, {"t1", "r"}},
                {{"s0", "p"}, {"s1", "r"}}};
  d.topology = t;
  d.connectivity = pairs({{"s0", "t0"}, {"s0", "t1"}, {"s1", "t1"}});
  REQUIRE(make_plan(d));
  REQUIRE(generate(d));
  // Without s0 -> t1 the crossing links carry no pair: the unused graph
  // object is reported, so no clock crossing is left without traffic.
  d.connectivity = pairs({{"s0", "t0"}, {"s1", "t1"}});
  const auto plan = make_plan(d);
  REQUIRE_FALSE(plan);
  CHECK(plan.diagnostics().front().code == "NCL_SWITCH_UNUSED_OBJECT");
}

TEST_CASE("WRAP packet defaults cover all sources reaching a target", "[connectivity]") {
  auto d = crossbar(2, 1);
  d.targets[0].data_width = 1024;
  d.initiators[0].data_width = 32;
  d.initiators[1].data_width = 32;
  const auto small = detail::build_network_plan(d);
  REQUIRE(small);
  const auto before = detail::packet_profile(small.value(), 0);
  d.initiators[1].data_width = 1024;
  const auto wide = detail::build_network_plan(d);
  REQUIRE(wide);
  const auto after = detail::packet_profile(wide.value(), 0);
  REQUIRE(before.windows.size() == after.windows.size());
  CHECK(before.decompose_wrap_bursts);
  CHECK_FALSE(after.decompose_wrap_bursts);
  for (unsigned i = 0; i < before.windows.size(); ++i) {
    CHECK(before.windows[i].wrap_container_limit == 1);
    CHECK(after.windows[i].wrap_container_limit == 64);
  }
}
