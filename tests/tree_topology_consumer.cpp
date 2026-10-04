#include "tree_topology_fixture.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  auto definition = tree_topology_fixture::definition();
  const auto topology = nocloom::tree_topology(tree_topology_fixture::nodes(), {"dma", "cpu"});
  if (!topology || topology.value() != tree_topology_fixture::explicit_graphs())
    return 3;
  definition.topology = topology.value();
  const auto helper = nocloom::generate(definition);
  definition.topology = tree_topology_fixture::explicit_graphs();
  const auto explicit_graphs = nocloom::generate(definition);
  if (!helper || !explicit_graphs || helper.value() != explicit_graphs.value() ||
      helper.value().size() != 6)
    return 4;
  const std::filesystem::path root(argv[1]);
  for (const auto &[label, result] :
       {std::pair{"helper", &helper}, std::pair{"explicit", &explicit_graphs}}) {
    std::filesystem::create_directories(root / label);
    for (const auto &artifact : result->value()) {
      std::ofstream file(root / label / artifact.path, std::ios::binary);
      file << artifact.text;
      if (!file)
        return 5;
    }
  }
  std::cout << "TREE_TOPOLOGY_CORE_PASS 3 identical artifacts\n";
}
