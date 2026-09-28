#include "apb_public_definition.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
int main(int argc, char **argv) {
  if (argc != 3 || (std::string(argv[2]) != "funnel" && std::string(argv[2]) != "graph" &&
                    std::string(argv[2]) != "clocked"))
    return 2;
  auto generated = nocloom::generate(apb_public_definition(argv[2]));
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
