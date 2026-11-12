#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>

namespace {
nocloom::Definition definition(const std::string &group) {
  const bool logs = group != "wide";
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.name = "test_bus";
  d.address_width = logs ? 32 : 48;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{logs ? "logger_control" : "control", "main"}};
  if (logs)
    d.targets = {{"ram", "main", {{0, 0x1000}}}, {"csr", "main", {{0x100000, 0x100000}}}};
  else
    d.targets = {
        {"wide", "main", {{0x100000, 0x1000, 0x1000}, {0x123450000000ULL, 0x1000, 0x2000}}}};
  d.initiators[0].data_width = 32;
  d.initiators[0].protocol = logs ? Protocol::Apb4 : Protocol::Axi4;
  if (!logs)
    d.initiators[0].len_width = 8;
  for (auto &t : d.targets) {
    t.data_width = 32;
    t.protocol = d.initiators[0].protocol;
    t.attributes = TargetAttributes::amba();
    if (!logs)
      t.len_width = 8;
  }
  if (logs) {
    d.targets[1].log_registers = true;
    if (group == "local" || group == "cross")
      d.targets[1].mappings[0].local_address = 0x1000;
    d.error_logging.logs = {
        {"errors",
         {"ram"},
         std::nullopt,
         false,
         {{.offset = 0x200}, {.offset = 0x500}, {.offset = 0x1000}, {.offset = 0xfff00}}}};
  }
  if (group == "local")
    d.error_logging.logs[0].loggers = {{.offset = 0x1000}};
  if (group == "cross")
    d.error_logging.logs[0].loggers = {
        {.offset = 0x1000}, {.offset = 0x1200}, {.offset = 0x2000}, {.offset = 0xfff00}};
  d.connectivity = Connectivity{.all = true};
  return d;
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 2)
    return 1;
  for (const std::string group : {"wide", "logs", "local", "cross"}) {
    const auto result = nocloom::generate(definition(group), {.c_header = true});
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
