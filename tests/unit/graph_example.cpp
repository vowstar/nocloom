#include "../graph_example_definition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>

TEST_CASE("Public graph planning rejects invalid topology before generation", "[graph-example]") {
  using namespace nocloom;
  const auto base = graph_example_definition();
  REQUIRE(validate(base));
  auto good = generate(base);
  REQUIRE(good);
  auto bad = base;
  bad.topology->request.links.push_back({"tail", "merge"});
  REQUIRE_FALSE(validate(bad));
  REQUIRE_FALSE(generate(bad));
  bad = base;
  bad.topology->request.links.push_back({"leaf0", "tail"});
  REQUIRE_FALSE(validate(bad));
  bad = base;
  bad.topology->request.links[0].transport_word_bytes = 3;
  REQUIRE_FALSE(validate(bad));
  bad = base;
  bad.topology->response.entries.pop_back();
  REQUIRE_FALSE(validate(bad));
  bad = base;
  bad.topology->request.nodes.push_back({"merge", 4});
  REQUIRE_FALSE(validate(bad));
  bad = base;
  bad.topology->request.entries[0].node = "absent";
  REQUIRE_FALSE(validate(bad));
  // Without topology the matrix generates one switch per direction.
  bad = base;
  bad.topology.reset();
  REQUIRE(validate(bad));
  bad.connectivity.reset();
  REQUIRE_FALSE(validate(bad));
  auto reordered = base;
  std::ranges::reverse(reordered.initiators);
  std::ranges::reverse(reordered.targets);
  std::ranges::reverse(reordered.topology->request.nodes);
  std::ranges::reverse(reordered.topology->request.links);
  std::ranges::reverse(reordered.topology->response.entries);
  auto reverse = generate(reordered);
  REQUIRE(reverse);
  REQUIRE(good.value() == reverse.value());
}

TEST_CASE("Public graph identities cannot collide with endpoint identities", "[graph-example]") {
  using namespace nocloom;
  auto d = graph_example_definition();
  d.initiators[0].name = "req_node0";
  d.topology->request.entries[0].endpoint = "req_node0";
  d.topology->response.exits[0].endpoint = "req_node0";
  REQUIRE(validate(d));
  REQUIRE(generate(d));
}

TEST_CASE("Public graph count bounds and single-node routes are explicit", "[graph-example]") {
  using namespace nocloom;
  auto d = graph_example_definition();
  d.initiators.resize(1);
  d.targets.resize(1);
  ExchangeTopology topology;
  topology.request = {{{"node", 4}}, {}, {{"s0", "node"}}, {{"t0", "node"}}};
  topology.response = {{{"node", 8}}, {}, {{"t0", "node"}}, {{"s0", "node"}}};
  d.topology = topology;
  REQUIRE(validate(d));
  REQUIRE(generate(d));
  const auto one = d;
  d.initiators.clear();
  REQUIRE_FALSE(validate(d));
  d = one;
  d.targets.clear();
  REQUIRE_FALSE(validate(d));
  d = one;
  for (unsigned i = 1; i < 64; ++i) {
    const auto source = "s" + std::to_string(i), target = "t" + std::to_string(i);
    d.initiators.push_back(graph_source(source));
    d.targets.push_back(graph_target(target, {65536ULL * i, 65536}));
    d.topology->request.entries.push_back({source, "node"});
    d.topology->request.exits.push_back({target, "node"});
    d.topology->response.entries.push_back({target, "node"});
    d.topology->response.exits.push_back({source, "node"});
  }
  // Planning checks the boundary; this is not a 64-endpoint simulation claim.
  REQUIRE(validate(d));
  auto excessive = d;
  excessive.initiators.push_back(graph_source("s64"));
  REQUIRE_FALSE(validate(excessive));
  excessive = d;
  excessive.targets.push_back(graph_target("t64", {65536ULL * 64, 65536}));
  REQUIRE_FALSE(validate(excessive));
  d = one;
  d.topology->request.nodes[0].transport_word_bytes = 0;
  REQUIRE_FALSE(validate(d));
  d = one;
  d.topology->request.entries.push_back({"s0", "node"});
  REQUIRE_FALSE(validate(d));
  d = one;
  d.topology->response.entries[0].endpoint = "s0";
  REQUIRE_FALSE(validate(d));
  d = one;
  for (unsigned i = 0; i < 128; ++i)
    d.topology->request.nodes.push_back({"extra" + std::to_string(i)});
  REQUIRE_FALSE(validate(d));
}

TEST_CASE("Public graph combines source protocols, widths and selective routes",
          "[graph-example]") {
  using namespace nocloom;
  auto d = graph_example_definition(true, true);
  REQUIRE(validate(d));
  auto result = generate(d);
  REQUIRE(result);
  std::ranges::reverse(d.initiators);
  std::ranges::reverse(d.targets);
  std::ranges::reverse(d.connectivity->pairs);
  auto reordered = generate(d);
  REQUIRE(reordered);
  REQUIRE(result.value() == reordered.value());
  d = graph_example_definition(true, true);
  d.initiators[1].data_width = 128;
  REQUIRE_FALSE(validate(d));
  d = graph_example_definition(true, true);
  d.initiators[0].clock_domain = "other";
  d.clock_domains.push_back({"other", "clk_other", "reset_n", "test"});
  REQUIRE_FALSE(validate(d));
}

namespace {
// Three AXI4 sources and targets; s0 reaches t0 and t1, s1 t1 and t2, s2 t2 and t0.
std::string loop_yaml(const std::string &request, const std::string &response,
                      const std::string &extra = "") {
  std::string yaml = "schema_version: 10\nname: graph_loops\naddress_width: 32\n"
                     "reset_ports: [{name: reset_n}]\ntest_ports: [{name: test}]\n"
                     "clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, "
                     "test_port: test}]\ninitiators:\n";
  for (const char *s : {"s0", "s1", "s2"})
    yaml += std::string("  - {name: ") + s +
            ", clock_domain: main, data_width: 32, protocol: axi4, len_width: 8}\n";
  yaml += "targets:\n";
  for (const char *t : {"t0", "t1", "t2"})
    yaml += std::string("  - {name: ") + t +
            ", clock_domain: main, data_width: 32, protocol: axi4, len_width: 8, "
            "attributes: amba, mappings: [{base: '0x" +
            t[1] + "0000', size: '0x10000'}]}\n";
  return yaml +
         "transport: {max_outstanding: 1, transport_word_bytes: 4}\n"
         "connectivity: {s0: [t0, t1], s1: [t1, t2], s2: [t2, t0]}\n" +
         extra + "topology:\n  request:\n" + request + "  response:\n" + response +
         "output: {backend: verilog, language: verilog_2001}\n";
}
// One switch per direction, and the acyclic request merge of the selective example.
const std::string one_request = R"(    nodes: [{name: hub}]
    links: []
    entries: [{endpoint: s0, node: hub}, {endpoint: s1, node: hub}, {endpoint: s2, node: hub}]
    exits: [{endpoint: t0, node: hub}, {endpoint: t1, node: hub}, {endpoint: t2, node: hub}]
)";
const std::string one_response = R"(    nodes: [{name: hub}]
    links: []
    entries: [{endpoint: t0, node: hub}, {endpoint: t1, node: hub}, {endpoint: t2, node: hub}]
    exits: [{endpoint: s0, node: hub}, {endpoint: s1, node: hub}, {endpoint: s2, node: hub}]
)";
const std::string merge_request =
    R"(    nodes: [{name: a}, {name: b}, {name: c}, {name: m}, {name: d}]
    links: [{from: a, to: m}, {from: b, to: m}, {from: c, to: m}, {from: m, to: d}]
    entries: [{endpoint: s0, node: a}, {endpoint: s1, node: b}, {endpoint: s2, node: c}]
    exits: [{endpoint: t0, node: m}, {endpoint: t1, node: d}, {endpoint: t2, node: d}]
)";
// Exactly one diagnostic, before any planning, and no artifacts.
void loop_rejected(const std::string &yaml, const char *code, const std::string &field,
                   const std::string &message) {
  auto parsed = nocloom::config::parse(yaml);
  std::vector<nocloom::Diagnostic> diagnostics;
  if (parsed) {
    auto valid = nocloom::validate(parsed.value().definition);
    REQUIRE_FALSE(valid);
    REQUIRE_FALSE(nocloom::generate(parsed.value().definition));
    diagnostics = valid.diagnostics();
  } else {
    diagnostics = parsed.diagnostics();
  }
  REQUIRE(diagnostics.size() == 1);
  CHECK(diagnostics.front().code == code);
  CHECK(diagnostics.front().field == field);
  CHECK(diagnostics.front().message.find(message) != std::string::npos);
}
} // namespace

// Routing loops and endpoint dependency loops written as configurations: a cycle that the
// routes chain (each route holding one link while it waits for the next) or a link back into its
// own switch is rejected in configured names; a cycle that no route chains is accepted; an
// endpoint dependency declaration is no configuration field (premise E3 excludes such loops).
TEST_CASE("Public graphs reject routing loops and endpoint dependency groups", "[graph-example]") {
  using namespace nocloom;
  // Positive controls: the same pairs over one switch and over an acyclic merge.
  for (const auto *request : {&one_request, &merge_request}) {
    auto parsed = config::parse(loop_yaml(*request, one_response));
    REQUIRE(parsed);
    REQUIRE(validate(parsed.value().definition));
    REQUIRE(generate(parsed.value().definition));
  }
  // A request ring whose two-hop routes chain all three links.
  const std::string ring = R"(    nodes: [{name: a}, {name: b}, {name: c}]
    links: [{from: a, to: b}, {from: b, to: c}, {from: c, to: a}]
    entries: [{endpoint: s0, node: a}, {endpoint: s1, node: b}, {endpoint: s2, node: c}]
    exits: [{endpoint: t0, node: b}, {endpoint: t1, node: c}, {endpoint: t2, node: a}]
)";
  loop_rejected(loop_yaml(ring, one_response), "NCL_SWITCH_ROUTE_CYCLE", "topology.request.links",
                "cycle a -> b -> c -> a");
  // The same ring in the response graph.
  const std::string response_ring = R"(    nodes: [{name: x}, {name: y}, {name: z}]
    links: [{from: x, to: y}, {from: y, to: z}, {from: z, to: x}]
    entries: [{endpoint: t0, node: x}, {endpoint: t1, node: y}, {endpoint: t2, node: z}]
    exits: [{endpoint: s0, node: z}, {endpoint: s1, node: x}, {endpoint: s2, node: y}]
)";
  loop_rejected(loop_yaml(one_request, response_ring), "NCL_SWITCH_ROUTE_CYCLE",
                "topology.response.links", "cycle x -> y -> z -> x");
  // A link back into its own switch: a route over it passes that switch twice.
  const std::string twice = R"(    nodes: [{name: hub}]
    links: [{from: hub, to: hub}]
    entries: [{endpoint: s0, node: hub}, {endpoint: s1, node: hub}, {endpoint: s2, node: hub}]
    exits: [{endpoint: t0, node: hub}, {endpoint: t1, node: hub}, {endpoint: t2, node: hub}]
)";
  loop_rejected(loop_yaml(twice, one_response), "NCL_SWITCH_ROUTE_CYCLE", "topology.request.links",
                "cycle hub -> hub");
  // Cycles that no route chains are accepted: two crossed links where every route takes at most
  // one hop, and the ring whose routes chain a -> b -> c and b -> c -> a but never c -> a -> b.
  const std::string crossed = R"(    nodes: [{name: a}, {name: b}]
    links: [{from: a, to: b}, {from: b, to: a}]
    entries: [{endpoint: s0, node: a}, {endpoint: s1, node: a}, {endpoint: s2, node: b}]
    exits: [{endpoint: t0, node: a}, {endpoint: t1, node: b}, {endpoint: t2, node: b}]
)";
  const std::string open_ring = R"(    nodes: [{name: a}, {name: b}, {name: c}]
    links: [{from: a, to: b}, {from: b, to: c}, {from: c, to: a}]
    entries: [{endpoint: s0, node: a}, {endpoint: s1, node: b}, {endpoint: s2, node: c}]
    exits: [{endpoint: t0, node: a}, {endpoint: t1, node: c}, {endpoint: t2, node: a}]
)";
  const std::string crossed_response = R"(    nodes: [{name: x}, {name: y}]
    links: [{from: x, to: y}, {from: y, to: x}]
    entries: [{endpoint: t0, node: x}, {endpoint: t1, node: y}, {endpoint: t2, node: y}]
    exits: [{endpoint: s0, node: x}, {endpoint: s1, node: y}, {endpoint: s2, node: y}]
)";
  for (const auto &[request, response] :
       {std::pair{&crossed, &one_response}, std::pair{&open_ring, &one_response},
        std::pair{&crossed, &crossed_response}}) {
    auto parsed = config::parse(loop_yaml(*request, *response));
    REQUIRE(parsed);
    REQUIRE(validate(parsed.value().definition));
    REQUIRE(generate(parsed.value().definition));
  }
  // A component that answers on t0 by issuing on s0, which reaches t0 again.
  loop_rejected(loop_yaml(one_request, one_response,
                          "dependencies: {groups: [{initiators: [s0], targets: [t0]}]}\n"),
                "NCL_SCHEMA_UNKNOWN_FIELD", "dependencies", "Unknown field");
}
