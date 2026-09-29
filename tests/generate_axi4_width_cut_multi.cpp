#include "core/axi4_width_clamp.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

// Internal packet fixture: bounded packets can share one source burst tag.
int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  using namespace nocloom::detail;
  Axi4WidthClampProfile p{"probe", 3, 4, 32, 2, 64};
  p.windows = {{{0, 65536}, 2}, {{65536, 65536}, 3}};
  p.fixed_reject = {{0, 65536}};
  p.tag_bits = 6;
  p.max_outstanding = 64;
  p.packet_depth = 4;
  auto cut = lower_axi4_width_clamp(p);
  if (!cut) {
    for (const auto &d : cut.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (!verify(cut.value().ir))
    return 1;
  const std::filesystem::path path = argv[1];
  std::filesystem::create_directories(path);
  std::ofstream rtl(path / "network.v"), ports(path / "ports.tsv");
  rtl << emit_verilog(cut.value().ir);
  for (const auto &port : cut.value().ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
