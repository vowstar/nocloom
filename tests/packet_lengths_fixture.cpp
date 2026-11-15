#include "packet_lengths_profile.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
  if (argc != 4)
    return 2;
  const auto hardware = packet_length_hardware(argv[2], std::stoul(argv[3]));
  if (!hardware) {
    for (const auto &d : hardware.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (!nocloom::detail::verify(hardware.value().ir))
    return 1;
  const std::filesystem::path work = argv[1];
  std::filesystem::create_directories(work);
  std::ofstream rtl(work / "network.v"), ports(work / "ports.tsv");
  rtl << nocloom::detail::emit_verilog(hardware.value().ir);
  for (const auto &port : hardware.value().ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
