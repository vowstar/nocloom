#include "core/axi4_initiator.hpp"
#include "core/axi4_width_clamp.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
  if (argc != 4)
    return 2;
  nocloom::detail::Axi4WidthClampProfile p;
  p.name = "refused";
  p.data_bits = std::stoul(argv[2]);
  const auto mode = std::stoul(argv[3]);
  p.max_outstanding = mode == 1 ? 1 : 3;
  p.tag_bits = 2;
  p.reassembly = mode == 4;
  p.windows = {{{0, 65536}, 2}};
  p.fixed_reject = {{0, 65536}};
  p.decode_packets = true;
  const auto hardware = nocloom::detail::lower_axi4_width_clamp(p);
  if (!hardware) {
    for (const auto &d : hardware.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  const std::filesystem::path work = argv[1];
  std::filesystem::create_directories(work);
  std::ofstream rtl(work / "network.v"), ports(work / "ports.tsv");
  rtl << nocloom::detail::emit_verilog(hardware.value().ir);
  for (const auto &port : hardware.value().ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  nocloom::detail::Axi4InitiatorProfile source;
  source.name = "refused_source";
  source.data_bits = p.data_bits;
  source.tag_bits = p.tag_bits;
  source.start_address = true;
  const auto ingress = nocloom::detail::lower_axi4_initiator(source);
  if (!ingress)
    return 1;
  rtl << nocloom::detail::emit_verilog(ingress.value().ir);
  std::ofstream source_ports(work / "source_ports.tsv"), module(work / "source_module.txt");
  module << ingress.value().ir.name;
  for (const auto &port : ingress.value().ir.ports)
    source_ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports && source_ports && module ? 0 : 1;
}
