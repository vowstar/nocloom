#include "core/network_plan.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <nocloom/config.hpp>

using namespace nocloom::detail;

namespace {
bool write_graph(const std::filesystem::path &out, const std::string &name, DependencyGraph graph,
                 bool cycle) {
  const auto report = check_dependency_graph(graph);
  if (report.findings.empty() == cycle) {
    std::cerr << "Unexpected cycle status for " << name << '\n';
    return false;
  }
  std::ofstream file(out / (name + ".json"));
  file << dependency_graph_json(graph, report, "certificate_graph", "", "", "");
  file.close();
  return bool(file);
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 3)
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
