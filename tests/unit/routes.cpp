// Explicit routes: the C++ definition equals the YAML one and generates the same bytes, the map
// records the routes as written, and a malformed entry is named by its index and direction.
// Initiator route classes: YAML equals C++, routes split by class and unreachable classes are
// rejected. Target flows likewise: YAML equals C++, an APB target
// takes a port set per flow and a mapping of a missing flow is named.
#include "core/id_class.hpp"
#include "core/network_plan.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <string>

using namespace nocloom;

namespace {
const char *const yaml = R"(schema_version: 10
name: routed
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk, reset_port: reset_n, test_port: test}
initiators:
  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 4}
targets:
  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, address_width: 16,
     attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}
  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, address_width: 16,
     attributes: amba, mappings: [{base: '0x1000', size: '0x1000'}]}
connectivity: all
topology:
  request:
    nodes: [{name: a}, {name: b}, {name: c}]
    links: [{from: a, to: b}, {from: b, to: c}, {from: a, to: c}]
    entries: [{endpoint: cpu, node: a}]
    exits: [{endpoint: ram, node: c}, {endpoint: rom, node: c}]
  response:
    nodes: [{name: x}]
    links: []
    entries: [{endpoint: ram, node: x}, {endpoint: rom, node: x}]
    exits: [{endpoint: cpu, node: x}]
  routes:
    - {initiator: cpu, target: ram, request: [a, b, c]}
    - {initiator: cpu, target: rom, request: [a, c]}
output: {backend: verilog, language: verilog_2001}
)";

Definition built() {
  Definition d;
  d.name = "routed";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  Initiator cpu{"cpu", "main"};
  cpu.protocol = Protocol::Axi4;
  cpu.data_width = 32;
  cpu.id_width = 2;
  cpu.len_width = 4;
  d.initiators = {cpu};
  Target ram{"ram", "main", {{0x0, 0x1000}}, 16};
  ram.protocol = Protocol::Apb4;
  ram.data_width = 32;
  ram.attributes = TargetAttributes::amba();
  Target rom{"rom", "main", {{0x1000, 0x1000}}, 16};
  rom.protocol = Protocol::Apb4;
  rom.data_width = 32;
  rom.attributes = TargetAttributes::amba();
  d.targets = {ram, rom};
  d.connectivity = Connectivity{.all = true};
  ExchangeTopology topology;
  topology.request.nodes = {{"a"}, {"b"}, {"c"}};
  topology.request.links = {{"a", "b"}, {"b", "c"}, {"a", "c"}};
  topology.request.entries = {{"cpu", "a"}};
  topology.request.exits = {{"ram", "c"}, {"rom", "c"}};
  topology.response.nodes = {{"x"}};
  topology.response.entries = {{"ram", "x"}, {"rom", "x"}};
  topology.response.exits = {{"cpu", "x"}};
  topology.routes = {{"cpu", "ram", {"a", "b", "c"}}, {"cpu", "rom", {"a", "c"}}};
  d.topology = topology;
  return d;
}
} // namespace

TEST_CASE("Explicit routes from YAML and C++", "[routes]") {
  auto parsed = config::parse(yaml, "routed.yaml");
  REQUIRE(parsed);
  REQUIRE(parsed.value().definition == built());
  const auto a = generate(parsed.value().definition), b = generate(built());
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  const auto map = std::ranges::find_if(
      a.value(), [](const Artifact &f) { return f.path.ends_with(".map.json"); });
  REQUIRE(map != a.value().end());
  REQUIRE(nlohmann::json::parse(map->text)["definition"]["topology"]["routes"] ==
          nlohmann::json::parse(R"([{"initiator":"cpu","request":["a","b","c"],"target":"ram"},
                                    {"initiator":"cpu","request":["a","c"],"target":"rom"}])"));
  // A path against a link's direction is named by its entry and direction.
  auto missing = built();
  missing.topology->routes.front().request = {"a", "c", "b"};
  const auto rejected = validate(missing);
  REQUIRE_FALSE(rejected);
  REQUIRE(rejected.diagnostics().front().code == "NCL_ROUTE_PATH");
  REQUIRE(rejected.diagnostics().front().field == "topology.routes[0].request");
}

TEST_CASE("Routes of initiator flows from YAML and C++", "[routes]") {
  std::string text = yaml;
  const auto at = text.find("len_width: 4}");
  text.replace(at, 13,
               "len_width: 4, route_classes: [even, odd], default_class: odd, id_ranges: [{first: "
               "0, last: 0, class: even}, {first: 2, last: 2, class: even}]}");
  const auto routes = text.find("    - {initiator: cpu, target: rom, request: [a, c]}");
  text.insert(routes, "    - {initiator: cpu, source_path: 1, target: ram, request: [a, c]}\n"
                      "    - {initiator: cpu, source_path: 1, target: rom, request: [a, c]}\n");
  auto d = built();
  d.initiators.front().route_classes = {"even", "odd"};
  d.initiators.front().default_class = "odd";
  d.initiators.front().id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  d.topology->routes.insert(d.topology->routes.begin() + 1,
                            {{"cpu", "ram", {"a", "c"}, {}, 1}, {"cpu", "rom", {"a", "c"}, {}, 1}});
  auto parsed = config::parse(text, "routed.yaml");
  REQUIRE(parsed);
  REQUIRE(parsed.value().definition == d);
  const auto a = generate(parsed.value().definition), b = generate(d), plain = generate(built());
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  // Flow 1 of cpu to ram parts from flow 0 at a: the network differs from the one-flow network.
  REQUIRE(a.value() != plain.value());
  // Every flow on one path changes no byte.
  auto same = d;
  same.topology->routes[1].request = {"a", "b", "c"};
  const auto c = generate(same);
  REQUIRE(c);
  const auto rtl = [](const std::vector<Artifact> &files) {
    return std::ranges::find_if(files, [](const Artifact &f) { return f.path.ends_with(".v"); })
        ->text;
  };
  REQUIRE(rtl(c.value()) == rtl(plain.value()));
  auto unreached = d;
  unreached.initiators.front().id_ranges = {{0, 3, "even"}};
  const auto rejected = validate(unreached);
  REQUIRE_FALSE(rejected);
  REQUIRE(rejected.diagnostics().front().code == "NCL_SOURCE_PATH");
  REQUIRE(rejected.diagnostics().front().field == "initiators[0].route_classes");
}

TEST_CASE("Routes of target flows from YAML and C++", "[routes]") {
  std::string text = yaml;
  const std::string ram = "attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}";
  text.replace(text.find(ram), ram.size(),
               "attributes: amba, flows: 2, mappings: [{base: '0x0', size: '0x1000'},\n"
               "     {base: '0x2000', size: '0x1000', flow: 1}]}");
  const auto routes = text.find("    - {initiator: cpu, target: rom, request: [a, c]}");
  text.insert(routes, "    - {initiator: cpu, target: ram, target_path: 1, request: [a, c]}\n");
  auto d = built();
  d.targets.front().flows = 2;
  d.targets.front().mappings.push_back({.base = 0x2000, .size = 0x1000, .flow = 1});
  d.topology->routes.insert(d.topology->routes.begin() + 1, {"cpu", "ram", {"a", "c"}, {}, 0, 1});
  auto parsed = config::parse(text, "routed.yaml");
  REQUIRE(parsed);
  REQUIRE(parsed.value().definition == d);
  const auto a = generate(parsed.value().definition), b = generate(d);
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  // The APB target has a port set per flow.
  const auto rtl = std::ranges::find_if(a.value(), [](const Artifact &f) {
                     return f.path.ends_with("nocloom_routed.v");
                   })->text;
  REQUIRE(rtl.find("nl_t_ram_psel_1") != std::string::npos);
  REQUIRE(rtl.find("nl_t_ram_psel,") == std::string::npos);
  // A mapping of a flow the target lacks is named by its index.
  auto lost = d;
  lost.targets.front().mappings.back().flow = 2;
  const auto rejected = validate(lost);
  REQUIRE_FALSE(rejected);
  REQUIRE(rejected.diagnostics().front().code == "NCL_TARGET_PATH");
  REQUIRE(rejected.diagnostics().front().field == "targets[0].mappings[1].flow");
}

TEST_CASE("Connectivity by flow pair from YAML and C++", "[routes]") {
  // cpu flow 1 does not reach ram: it takes no route there and the map lists the pairs' flows.
  std::string text = yaml;
  text.replace(text.find("len_width: 4}"), 13,
               "len_width: 4, route_classes: [even, odd], default_class: odd, id_ranges: [{first: "
               "0, last: 0, class: even}, {first: 2, last: 2, class: even}]}");
  text.replace(text.find("connectivity: all"), 17,
               "connectivity:\n  cpu: [{target: ram, source_path: 0}, rom]");
  text.insert(text.find("output:"),
              "    - {initiator: cpu, source_path: 1, target: rom, request: [a, c]}\n");
  auto d = built();
  d.initiators.front().route_classes = {"even", "odd"};
  d.initiators.front().default_class = "odd";
  d.initiators.front().id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  d.connectivity = Connectivity{.pairs = {{"cpu", "ram", 0U, std::nullopt}, {"cpu", "rom"}}};
  d.topology->routes.push_back({"cpu", "rom", {"a", "c"}, {}, 1});
  auto parsed = config::parse(text, "routed.yaml");
  REQUIRE(parsed);
  REQUIRE(parsed.value().definition == d);
  const auto a = generate(parsed.value().definition), b = generate(d);
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  const auto map = std::ranges::find_if(
      a.value(), [](const Artifact &f) { return f.path.ends_with(".map.json"); });
  REQUIRE(nlohmann::json::parse(map->text)["definition"]["connectivity"] ==
          nlohmann::json::parse(R"([{"source":"cpu","target":"ram","flow_pairs":[[0,0]]},
                                    {"source":"cpu","target":"rom","flow_pairs":[[0,0],[1,0]]}])"));
  // A route of the flow pair left out, and a flow the initiator lacks, are named.
  auto routed = d;
  routed.topology->routes.push_back({"cpu", "ram", {"a", "c"}, {}, 1});
  const auto unrouted = validate(routed);
  REQUIRE_FALSE(unrouted);
  REQUIRE(unrouted.diagnostics().front().code == "NCL_ROUTE_REFERENCE");
  REQUIRE(unrouted.diagnostics().front().field == "topology.routes[3]");
  auto lost = d;
  lost.connectivity->pairs.front().source_path = 2;
  const auto rejected = validate(lost);
  REQUIRE_FALSE(rejected);
  REQUIRE(rejected.diagnostics().front().code == "NCL_CONNECTIVITY_REFERENCE");
  REQUIRE(rejected.diagnostics().front().field == "connectivity.cpu[0].source_path");
}

TEST_CASE("ID intervals are total and independent of declaration order", "[routes]") {
  auto d = built();
  auto &source = d.initiators.front();
  source.id_width = 5;
  source.route_classes = {"scattered", "fallback", "upper"};
  source.default_class = "fallback";
  source.id_ranges = {{27, 31, "upper"}, {28, 30, "upper"}};
  for (unsigned id = 0; id < 24; id += 2)
    source.id_ranges.push_back({id, id, "scattered"});
  // All classes take the same automatically planned paths here.
  d.topology.reset();
  REQUIRE(validate(d));
  const auto partition = nocloom::detail::class_partition(source);
  REQUIRE(partition.size() <= 32);
  nocloom::detail::Builder b;
  const auto id = b.input("id", 5, "class", "id");
  std::vector<nocloom::detail::ValueId> values;
  for (unsigned path = 0; path < 3; ++path)
    values.push_back(b.constant(2, path));
  const auto result = nocloom::detail::class_value(b, id, partition, values);
  for (unsigned input = 0; input < 32; ++input) {
    std::vector<std::uint64_t> evaluated;
    for (const auto &v : b.ir.values) {
      using nocloom::detail::Op;
      const auto arg = [&](unsigned k) { return evaluated.at(v.operands[k].index); };
      switch (v.op) {
      case Op::Input:
        evaluated.push_back(input);
        break;
      case Op::Constant:
        evaluated.push_back(v.literal);
        break;
      case Op::UnsignedLess:
        evaluated.push_back(arg(0) < arg(1));
        break;
      case Op::Select:
        evaluated.push_back(arg(0) ? arg(1) : arg(2));
        break;
      default:
        FAIL("Unexpected operation in a pure interval tree");
      }
    }
    const unsigned expected = input >= 27 ? 2 : input < 24 && input % 2 == 0 ? 0 : 1;
    REQUIRE(evaluated[result.index] == expected);
  }
  // An overlapping different class and an unreachable default are both invalid.
  source.id_ranges.push_back({2, 3, "upper"});
  REQUIRE_FALSE(validate(d));
  source.id_ranges = {{0, 31, "scattered"}};
  REQUIRE_FALSE(validate(d));
}
