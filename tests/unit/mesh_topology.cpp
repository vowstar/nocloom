#include "core/network_plan.hpp"
#include <algorithm>
#include <catch2/catch_message.hpp>
#include <catch2/catch_test_macros.hpp>
#include <limits>
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>
#include <set>

using namespace nocloom;
namespace {
std::vector<MeshEndpoint> endpoints(unsigned rows, unsigned columns, const std::string &prefix) {
  std::vector<MeshEndpoint> result;
  for (unsigned row = 0; row < rows; ++row)
    for (unsigned column = 0; column < columns; ++column)
      result.push_back({prefix + std::to_string(row) + "_" + std::to_string(column), row, column});
  return result;
}
Definition definition(const std::vector<MeshEndpoint> &sources,
                      const std::vector<MeshEndpoint> &targets, const Connectivity &connected) {
  Definition d;
  d.schema_version = 10;
  d.name = "mesh_test";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  for (const auto &source : sources) {
    d.initiators.push_back({source.name, "main"});
    auto &i = d.initiators.back();
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
    i.id_width = 2;
    i.len_width = 8;
    if (source.flows > 1) {
      for (unsigned f = 0; f < source.flows; ++f) {
        i.route_classes.push_back("f" + std::to_string(f));
        i.id_ranges.push_back({f, f, "f" + std::to_string(f)});
      }
      i.default_class = "f0";
    }
  }
  unsigned base = 0;
  for (const auto &target : targets) {
    d.targets.push_back({target.name, "main", {}});
    auto &t = d.targets.back();
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
    t.id_width = 8;
    t.len_width = 8;
    t.attributes = TargetAttributes::amba();
    t.flows = target.flows;
    for (unsigned f = 0; f < target.flows; ++f) {
      t.mappings.push_back({base, 4096});
      t.mappings.back().flow = f;
      base += 4096;
    }
  }
  d.connectivity = connected;
  const auto topology = mesh_topology(3, 3, sources, targets, connected, "main");
  REQUIRE(topology);
  d.topology = topology.value();
  return d;
}
void rejected(const Result<ExchangeTopology> &result, const std::string &code,
              const std::string &field) {
  REQUIRE_FALSE(result);
  REQUIRE(result.diagnostics().size() == 1);
  CHECK(result.diagnostics()[0].code == code);
  CHECK(result.diagnostics()[0].field == field);
}
void shape(const std::vector<std::string> &path, const MeshEndpoint &from, const MeshEndpoint &to) {
  REQUIRE(path.size() == 1 + std::max(from.row, to.row) - std::min(from.row, to.row) +
                             std::max(from.column, to.column) - std::min(from.column, to.column));
  const auto name = [](unsigned r, unsigned c) {
    return "mesh_r" + std::to_string(r) + "_c" + std::to_string(c);
  };
  CHECK(path.front() == name(from.row, from.column));
  CHECK(path.back() == name(to.row, to.column));
  const unsigned x_hops = std::max(from.column, to.column) - std::min(from.column, to.column);
  for (unsigned hop = 0; hop < path.size(); ++hop) {
    const unsigned x = std::min(hop, x_hops), y = hop - x;
    CHECK(path[hop] == name(from.row < to.row ? from.row + y : from.row - y,
                            from.column < to.column ? from.column + x : from.column - x));
  }
}
} // namespace

TEST_CASE("Mesh routes traverse X then Y independently in both graphs", "[mesh-topology]") {
  for (unsigned side : {1U, 3U, 4U}) {
    const auto sources = endpoints(side, side, "i"), targets = endpoints(side, side, "t");
    const auto result = mesh_topology(side, side, sources, targets, {.all = true});
    REQUIRE(result);
    const auto &topology = result.value();
    for (const auto *graph : {&topology.request, &topology.response}) {
      CHECK(graph->nodes.size() == side * side);
      CHECK(graph->links.size() == 4 * side * (side - 1));
      CHECK(graph->entries.size() == side * side);
      CHECK(graph->exits.size() == side * side);
      for (const auto &node : graph->nodes) {
        CHECK_FALSE(node.clock_domain);
        CHECK_FALSE(node.transport_word_bytes);
        CHECK(node.arbitration.empty());
      }
    }
    CHECK(topology.routes.size() == side * side * side * side);
    for (const auto &route : topology.routes) {
      const auto source = std::ranges::find(sources, route.initiator, &MeshEndpoint::name);
      const auto target = std::ranges::find(targets, route.target, &MeshEndpoint::name);
      REQUIRE(source != sources.end());
      REQUIRE(target != targets.end());
      shape(route.request, *source, *target);
      shape(route.response, *target, *source);
    }
  }
  const auto sources = endpoints(3, 3, "i"), targets = endpoints(3, 3, "t");
  auto d = definition(sources, targets, {.all = true});
  const auto planned = detail::build_network_plan(d);
  REQUIRE(planned);
  CHECK(planned.value().dependency_report.cycle_sets == 0);
  CHECK(planned.value().dependency_report.findings.empty());
  REQUIRE(make_plan(d));
}

TEST_CASE("Mesh prunes unused objects and expands only admitted flow pairs", "[mesh-topology]") {
  std::vector<MeshEndpoint> sources{{"i", 0, 0, 2}, {"j", 2, 2}};
  std::vector<MeshEndpoint> targets{{"a", 0, 0, 2}, {"b", 2, 2}};
  const Connectivity connected{.pairs = {{"i", "a", 1, {}}, {"j", "b"}}};
  const auto result = mesh_topology(3, 3, sources, targets, connected, "main");
  REQUIRE(result);
  CHECK(result.value().routes.size() == 3);
  for (const auto *graph : {&result.value().request, &result.value().response}) {
    CHECK(graph->nodes.size() == 2);
    CHECK(graph->links.empty());
    for (const auto &node : graph->nodes)
      CHECK(node.clock_domain == "main");
  }
  CHECK(result.value().routes[0].source_path == 1);
  CHECK(result.value().routes[0].target_path == 0);
  CHECK(result.value().routes[1].target_path == 1);
  const auto sparse_plan = make_plan(definition(sources, targets, connected));
  INFO((sparse_plan ? "" : sparse_plan.diagnostics().front().message));
  REQUIRE(sparse_plan);
  const auto all = mesh_topology(3, 3, sources, targets, {.all = true});
  REQUIRE(all);
  CHECK(all.value().routes.size() == 9);
  REQUIRE(make_plan(definition(sources, targets, {.all = true})));
  // An omitted index expands that side independently.
  const auto partial =
      mesh_topology(1, 1, {{"i", 0, 0, 3}}, {{"t", 0, 0, 2}}, {.pairs = {{"i", "t", {}, 1}}});
  REQUIRE(partial);
  CHECK(partial.value().routes.size() == 3);
  for (const auto &route : partial.value().routes)
    CHECK(route.target_path == 1);
  const auto line = mesh_topology(1, 128, {{"i", 0, 0}}, {{"t", 0, 127}}, {.all = true});
  REQUIRE(line);
  CHECK(line.value().request.nodes.size() == 128);
  CHECK(line.value().request.links.size() == 127);
  const auto column = mesh_topology(128, 1, {{"i", 0, 0}}, {{"t", 127, 0}}, {.all = true});
  REQUIRE(column);
  CHECK(column.value().response.nodes.size() == 128);
}

TEST_CASE("Mesh input permutations preserve topology and artifact bytes", "[mesh-topology]") {
  auto sources = std::vector<MeshEndpoint>{{"i", 0, 0}, {"j", 2, 2}};
  auto targets = std::vector<MeshEndpoint>{{"a", 0, 2}, {"b", 2, 0}};
  Connectivity connected{.pairs = {{"j", "b"}, {"i", "a"}, {"i", "b"}, {"j", "a"}}};
  const auto original = mesh_topology(3, 3, sources, targets, connected, "main");
  REQUIRE(original);
  auto d = definition(sources, targets, connected);
  const auto expected = generate(d);
  REQUIRE(expected);
  for (unsigned permutation = 0; permutation < 8; ++permutation) {
    if (permutation & 1)
      std::ranges::reverse(sources);
    if (permutation & 2)
      std::ranges::reverse(targets);
    std::ranges::rotate(connected.pairs, connected.pairs.begin() + 1);
    const auto reordered = mesh_topology(3, 3, sources, targets, connected, "main");
    REQUIRE(reordered);
    CHECK(reordered.value() == original.value());
    d.topology = reordered.value();
    const auto generated = generate(d);
    REQUIRE(generated);
    CHECK(generated.value() == expected.value());
  }
}

TEST_CASE("Mesh accepts up to 256 attachments per endpoint role", "[mesh-topology]") {
  for (const bool initiators : {true, false})
    for (const unsigned count : {255U, 256U, 257U}) {
      DYNAMIC_SECTION((initiators ? "initiators" : "targets") << " count " << count) {
        std::vector<MeshEndpoint> attachments;
        for (unsigned i = 0; i < count; ++i)
          attachments.push_back({"endpoint_" + std::to_string(i), 0, 0});
        const std::vector<MeshEndpoint> single{{"single", 0, 0}};
        const auto result = mesh_topology(1, 1, initiators ? attachments : single,
                                          initiators ? single : attachments, {.all = true});
        if (count <= 256) {
          REQUIRE(result);
          CHECK(result.value().routes.size() == count);
        } else {
          rejected(result, "NCL_MESH_ENDPOINT", initiators ? "mesh.initiators" : "mesh.targets");
        }
      }
    }
}

TEST_CASE("Mesh rejects invalid dimensions attachments and connectivity", "[mesh-topology]") {
  const std::vector<MeshEndpoint> sources{{"i", 0, 0}}, targets{{"t", 0, 0}};
  for (const auto &[rows, columns] :
       {std::pair{0U, 1U}, {1U, 0U}, {3U, 43U}, {std::numeric_limits<unsigned>::max(), 2U}})
    rejected(mesh_topology(rows, columns, sources, targets, {.all = true}), "NCL_MESH_DIMENSIONS",
             "mesh");
  rejected(mesh_topology(1, 1, {}, targets, {.all = true}), "NCL_MESH_ENDPOINT", "mesh.initiators");
  rejected(mesh_topology(1, 1, sources, {}, {.all = true}), "NCL_MESH_ENDPOINT", "mesh.targets");
  rejected(mesh_topology(1, 1, {{"i", 1, 0}}, targets, {.all = true}), "NCL_MESH_ENDPOINT",
           "mesh.initiators[0]");
  rejected(mesh_topology(1, 1, sources, {{"t", 0, 1}}, {.all = true}), "NCL_MESH_ENDPOINT",
           "mesh.targets[0]");
  rejected(mesh_topology(1, 1, {{"i"}, {"i"}}, targets, {.all = true}), "NCL_MESH_ENDPOINT",
           "mesh.initiators[1].name");
  rejected(mesh_topology(1, 1, sources, {{"t"}, {"t"}}, {.all = true}), "NCL_MESH_ENDPOINT",
           "mesh.targets[1].name");
  rejected(mesh_topology(1, 1, {{"module"}}, targets, {.all = true}), "NCL_NAME_INVALID",
           "mesh.initiators[0].name");
  rejected(mesh_topology(1, 1, sources, {{"nl_reserved"}}, {.all = true}), "NCL_NAME_INVALID",
           "mesh.targets[0].name");
  for (unsigned flows : {0U, 33U})
    rejected(mesh_topology(1, 1, {{"i", 0, 0, flows}}, targets, {.all = true}), "NCL_MESH_ENDPOINT",
             "mesh.initiators[0].flows");
  for (const auto &connected :
       {Connectivity{.all = true, .pairs = {{"i", "t"}}}, Connectivity{},
        Connectivity{.pairs = {{"unknown", "t"}}}, Connectivity{.pairs = {{"i", "unknown"}}},
        Connectivity{.pairs = {{"i", "t", 1, {}}}}, Connectivity{.pairs = {{"i", "t", {}, 1}}},
        Connectivity{.pairs = {{"i", "t"}, {"i", "t"}}}}) {
    const auto result = mesh_topology(1, 1, sources, targets, connected);
    REQUIRE_FALSE(result);
    CHECK(result.diagnostics()[0].code == "NCL_MESH_CONNECTIVITY");
  }
  const auto max_flows =
      mesh_topology(1, 1, {{"same", 0, 0, 32}}, {{"same", 0, 0, 32}}, {.all = true});
  REQUIRE(max_flows);
  CHECK(max_flows.value().routes.size() == 1024);
}

TEST_CASE("Mesh route wait validation rejects a closed rectangle", "[mesh-topology]") {
  auto d = definition(endpoints(3, 3, "i"), endpoints(3, 3, "t"), {.all = true});
  REQUIRE(validate(d));
  for (const std::string direction : {"request", "response"}) {
    auto altered = d;
    const auto replace = [&](const std::string &source, const std::string &target,
                             std::vector<std::string> path) {
      auto route = std::ranges::find_if(altered.topology->routes, [&](const auto &r) {
        return r.initiator == source && r.target == target;
      });
      REQUIRE(route != altered.topology->routes.end());
      (direction == "request" ? route->request : route->response) = std::move(path);
    };
    // One Y-before-X turn alone does not close a channel wait cycle.
    replace(direction == "request" ? "i0_1" : "i1_0", direction == "request" ? "t1_0" : "t0_1",
            {"mesh_r0_c1", "mesh_r1_c1", "mesh_r1_c0"});
    REQUIRE(validate(altered));
    replace(direction == "request" ? "i1_0" : "i0_1", direction == "request" ? "t0_1" : "t1_0",
            {"mesh_r1_c0", "mesh_r0_c0", "mesh_r0_c1"});
    const auto checked = validate(altered);
    REQUIRE_FALSE(checked);
    CHECK(checked.diagnostics().front().code == "NCL_SWITCH_ROUTE_CYCLE");
  }
}
