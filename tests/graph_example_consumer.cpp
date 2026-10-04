#include "graph_example_definition.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
int main(int argc, char **argv) {
  if (argc != 2 &&
      (argc != 3 || (std::string(argv[2]) != "selective" && std::string(argv[2]) != "mixed")))
    return 2;
  auto artifacts = nocloom::generate(
      graph_example_definition(argc == 3, argc == 3 && std::string(argv[2]) == "mixed"));
  if (!artifacts) {
    for (const auto &d : artifacts.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(argv[1]);
  for (const auto &a : artifacts.value()) {
    std::ofstream f(std::filesystem::path(argv[1]) / a.path);
    f << a.text;
    if (!f)
      return 1;
  }
  return 0;
}
