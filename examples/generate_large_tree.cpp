#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <nocloom/generation.hpp>
#include <nocloom/topology.hpp>
#include <string_view>

int main(int argc, char **argv) {
  if ((argc != 4 && argc != 5) ||
      (std::string_view(argv[1]) != "apb" && std::string_view(argv[1]) != "axi") ||
      (std::string_view(argv[2]) != "12" && std::string_view(argv[2]) != "32" &&
       std::string_view(argv[2]) != "64h1" && std::string_view(argv[2]) != "64d64" &&
       std::string_view(argv[2]) != "256" && std::string_view(argv[2]) != "c64"))
    return 2;
  if (argc == 5 && std::string_view(argv[4]) != "plan" && std::string_view(argv[4]) != "permuted")
    return 2;
  const bool axi = std::string_view(argv[1]) == "axi";
  const bool expanded = std::string_view(argv[2]).starts_with("64");
  // c64: 32 sources and 32 targets, each endpoint in a clock domain of its own (src00 in main).
  const bool clocked = std::string_view(argv[2]) == "c64";
  // 256: 256 sources and 256 targets on a balanced 64-node tree.
  const bool largest = std::string_view(argv[2]) == "256";
  const unsigned inputs = largest                             ? 256
                          : expanded                          ? 64
                          : std::string_view(argv[2]) == "12" ? 12
                                                              : 32;
  const unsigned nodes = largest ? 64 : expanded ? 32 : 16;
  const unsigned targets = largest    ? 256
                           : expanded ? (std::string_view(argv[2]) == "64h1" ? 1 : 64)
                           : clocked  ? 32
                                      : 65 - inputs;
  const auto name = [](const char *prefix, unsigned i) {
    return std::string(prefix) + (i < 10 ? "0" : "") + std::to_string(i);
  };
  const auto domain = [&](const char *kind, unsigned i) {
    return !clocked || (kind == std::string_view("s") && i == 0) ? std::string("main")
                                                                 : name(kind, i).insert(0, "d");
  };
  nocloom::Definition d;
  d.schema_version = 10;
  const bool chain = expanded && targets == 1;
  d.name = "scale_" + std::string(argv[1]) + "_n" + std::to_string(nodes) + "_s" +
           std::to_string(inputs) +
           (chain     ? "_chain"
            : clocked ? "_clocked"
                      : "_balanced");

  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  for (const auto *kind : {"s", "t"})
    for (unsigned i = 0; i < (clocked ? 32U : 1U); ++i)
      if (const auto n = domain(kind, i); n != "main" || d.clock_domains.empty())
        d.clock_domains.push_back({n, "clk_" + n, "reset_n", "test"});
  for (unsigned i = 0; i < inputs; ++i) {
    d.initiators.push_back({name("src", i), domain("s", i)});
    d.initiators.back().protocol = axi ? nocloom::Protocol::Axi4Lite : nocloom::Protocol::Apb4;
    d.initiators.back().data_width = 32;
  }
  // 256 axi: the first source keeps two transfers in flight and records each one's
  // destination among the 256 routing windows.
  if (largest && axi)
    d.initiators.front().max_outstanding = 2;
  std::vector<nocloom::TreeNode> tree;
  for (unsigned i = 0; i < nodes; ++i)
    tree.push_back({name("node", i),
                    i ? std::optional(name("node", chain ? i - 1 : (i - 1) / 2)) : std::nullopt,
                    {}});
  for (unsigned t = 0; t < targets; ++t) {
    d.targets.push_back({name("bank", t), domain("t", t), {{t * 4096U, 4096}}});
    d.targets.back().protocol = nocloom::Protocol::Apb4;
    d.targets.back().attributes = nocloom::TargetAttributes::amba();
    d.targets.back().data_width = 32;
    tree[chain ? 31 : 1 + t % (nodes - 1)].targets.push_back(name("bank", t));
  }
  d.connectivity = nocloom::Connectivity{.all = true};
  if (argc == 5 && std::string_view(argv[4]) == "permuted") {
    std::ranges::reverse(d.initiators);
    std::ranges::reverse(d.targets);
    std::ranges::reverse(tree);
    for (auto &node : tree)
      std::ranges::reverse(node.targets);
  }
  std::vector<std::string> source_names;
  std::ranges::transform(d.initiators, std::back_inserter(source_names),
                         [](const auto &source) { return source.name; });
  const auto topology = nocloom::tree_topology(tree, source_names);
  if (!topology) {
    for (const auto &diagnostic : topology.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  d.topology = topology.value();
  // c64: request node i in the domain of source 2i, response node i in that of target 2i.
  for (auto *graph : {&d.topology->request, &d.topology->response})
    for (auto &node : graph->nodes)
      if (clocked)
        node.clock_domain = domain(graph == &d.topology->request ? "s" : "t",
                                   2 * static_cast<unsigned>(std::stoul(node.name.substr(4))));
  const auto result = [&] {
    if (argc == 4 || std::string_view(argv[4]) == "permuted")
      return nocloom::generate(d);
    const auto plan = nocloom::make_plan(d);
    if (!plan)
      return nocloom::Result<nocloom::ArtifactSet>::failure(plan.diagnostics());
    // Changing the source definition cannot affect the validated snapshot.
    d.name.clear();
    d.topology.reset();
    d.initiators.clear();
    return nocloom::emit(plan.value());
  }();
  if (!result) {
    for (const auto &diagnostic : result.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  const std::filesystem::path directory(argv[3]);
  if (!std::filesystem::create_directory(directory))
    return 3;
  for (const auto &artifact : result.value()) {
    std::ofstream stream(directory / artifact.path, std::ios::binary);
    stream << artifact.text;
    stream.close();
    if (!stream)
      return 3;
  }
  return 0;
}
