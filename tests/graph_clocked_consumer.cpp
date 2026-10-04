#include "graph_clocked_definition.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
int main(int argc, char **argv) {
  if (argc != 3 || (std::string(argv[2]) != "endpoints" && std::string(argv[2]) != "twelve"))
    return 2;
  auto generated = nocloom::generate(graph_clocked_definition(std::string(argv[2]) == "twelve"));
  if (!generated) {
    for (const auto &d : generated.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(argv[1]);
  for (const auto &a : generated.value()) {
    std::ofstream f(std::filesystem::path(argv[1]) / a.path);
    f << a.text;
    if (!f)
      return 1;
  }
  return 0;
}
