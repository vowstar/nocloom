#include "core/axi4_packetizer.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

// Emits the bounded packet splitter alone: generate_axi4_packetizer <dir>.
// One 32-bit source, one window over the low 64 KiB whose packets hold at
// most 16 bytes from their start (four 4-byte beats).
int main(int argc, char **argv) {
  if (argc != 2 && argc != 3)
    return 2;
  using namespace nocloom::detail;
  Axi4PacketizerProfile p;
  p.name = "probe";
  p.id_bits = 2;
  p.data_bits = 32;
  p.windows = {{{0x0, 0x10000}, 16, 4096, 4}};
  if (argc == 3) {
    if (std::string(argv[2]) != "multi")
      return 2;
    p.max_outstanding = 4;
    p.tag_bits = 6;
    p.split_narrow = true;
  }
  auto splitter = lower_axi4_packetizer(p);
  if (!splitter) {
    for (const auto &d : splitter.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (!verify(splitter.value().ir))
    return 1;
  const std::filesystem::path path = argv[1];
  std::filesystem::create_directories(path);
  std::ofstream rtl(path / "network.v"), ports(path / "ports.tsv");
  rtl << emit_verilog(splitter.value().ir);
  for (const auto &port : splitter.value().ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
