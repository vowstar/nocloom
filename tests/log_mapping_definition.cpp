#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>

namespace {
nocloom::Definition definition(bool cross, bool shadow) {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.name = "test_bus";
  d.address_width = 32;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"logger_control", "main"}};
  d.initiators[0].protocol = Protocol::Apb4;
  d.initiators[0].data_width = 32;
  d.targets = {{"ram", "main", {{0, 0x1000}}}, {"csr", "main", {{0x100000, 0x100000, 0x1000}}}};
  for (auto &t : d.targets) {
    t.protocol = Protocol::Apb4;
    t.data_width = 32;
    t.attributes = TargetAttributes::amba();
  }
  d.targets[1].log_registers = true;
  d.error_logging.logs = {{"errors", {"ram"}, std::nullopt, false, {{.offset = 0x1000}}}};
  if (cross)
    d.error_logging.logs[0].loggers = {
        {.offset = 0x1000}, {.offset = 0x1200}, {.offset = 0x4000}, {.offset = 0xfff00}};
  if (shadow) {
    d.targets[0].mappings = {{0x100000, 0x1000}, {0x108000, 0x1000}};
    d.error_logging.logs[0].loggers = {
        {.offset = 0x2000}, {.offset = 0x2200}, {.offset = 0x4000}, {.offset = 0xfff00}};
  }
  d.connectivity = Connectivity{.all = true};
  return d;
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 2)
    return 1;
  for (const std::string group : {"local", "cross", "shadow"}) {
    const auto result = nocloom::generate(definition(group == "cross", group == "shadow"));
    if (!result) {
      for (const auto &d : result.diagnostics())
        std::cerr << d.code << ": " << d.message << '\n';
      return 2;
    }
    const auto folder = std::filesystem::path(argv[1]) / group;
    if (!std::filesystem::create_directories(folder))
      return 3;
    for (const auto &a : result.value()) {
      std::ofstream stream(folder / a.path, std::ios::binary);
      stream << a.text;
      stream.close();
      if (!stream)
        return 4;
    }
  }
}
