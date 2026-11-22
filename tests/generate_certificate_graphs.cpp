#include "core/network_plan.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <nocloom/config.hpp>
#include <optional>
#include <string_view>

using namespace nocloom::detail;

namespace {
bool write_graph(const std::filesystem::path &out, const std::string &name, DependencyGraph graph,
                 std::optional<bool> cycle) {
  const auto report = check_dependency_graph(graph);
  if (cycle && report.findings.empty() == *cycle) {
    std::cerr << "Unexpected cycle status for " << name << '\n';
    return false;
  }
  std::ofstream file(out / (name + ".json"));
  file << dependency_graph_json(graph, report, "certificate_graph", "", "", "");
  file.close();
  return bool(file);
}
} // namespace

// Usage: INPUT OUT [ordinary | held]. With "ordinary" only the product graph is written, so INPUT
// need not name the endpoints s0 and t0 that the other graphs edit. "held" adds held.json: the
// graph of the same plan whose response grants never end a segment before the last word, the
// fault that tests/run_liveness_formal.py injects into the RTL of its hang cases.
int main(int argc, char **argv) {
  const std::string_view mode = argc == 4 ? argv[3] : "";
  if (argc != 3 && (argc != 4 || (mode != "ordinary" && mode != "held")))
    return 2;
  std::ifstream file(argv[1]);
  const std::string text{std::istreambuf_iterator<char>(file), {}};
  const auto parsed = nocloom::config::parse(text, "certificate_graph");
  if (!parsed)
    return 1;
  const auto plan = build_network_plan(parsed.value().definition);
  if (!plan)
    return 1;
  const auto out = std::filesystem::path(argv[2]);
  std::filesystem::create_directories(out);
  auto facts = dependency_facts(plan.value());
  const auto ordinary = build_dependency_graph(plan.value(), facts);
  if (ordinary.edges.empty() || !write_graph(out, "ordinary", ordinary, false))
    return 1;
  if (mode == "held") {
    facts.segment_ends.assign(facts.segment_ends.size(), false);
    return write_graph(out, "held", build_dependency_graph(plan.value(), facts), std::nullopt) ? 0
                                                                                               : 1;
  }
  if (argc == 4)
    return 0;
  const auto &edge = ordinary.edges.front();
  const auto &blocked = ordinary.nodes[edge.to], &blocker = ordinary.nodes[edge.from];
  facts.escapes.push_back(
      {{blocked.kind, blocked.key}, {blocker.kind, blocker.key}, {"env.consumer", "s0"}});
  if (!write_graph(out, "grouped_dag", build_dependency_graph(plan.value(), facts), false))
    return 1;
  facts = dependency_facts(plan.value());
  facts.edges.push_back({{"endpoint.target.req", "t0"}, {"endpoint.target.rsp", "t0"}});
  if (!write_graph(out, "cycle", build_dependency_graph(plan.value(), facts), true))
    return 1;
  facts.escapes.push_back(
      {{"endpoint.target.rsp", "t0"}, {"endpoint.target.req", "t0"}, {"env.consumer", "s0"}});
  return write_graph(out, "cycle_escape", build_dependency_graph(plan.value(), facts), false) ? 0
                                                                                              : 1;
}
