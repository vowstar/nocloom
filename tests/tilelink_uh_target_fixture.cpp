#include "core/tilelink_uh_outlet.hpp"
#include <algorithm>
#include <bit>
#include <filesystem>
#include <fstream>
#include <iostream>

int main(int argc, char **argv) {
  if (argc < 4 || argc > 6)
    return 2;
  using namespace nocloom::detail;
  const unsigned maximum = std::stoul(argv[3]);
  const auto result = [&] {
    if (std::string(argv[2]) == "selector") {
      logic::Logic l;
      const auto address = l.in("range", "address", 32);
      const auto available = l.in("range", "available", 32);
      l.out("range", "size", tilelink_block_size(l, address, available, 4, maximum));
      return l.finish("nocloom_selector");
    }
    TileLinkUhOutletProfile p;
    p.packet.name = "probe";
    p.packet.packet_len_bits = maximum > 1024 ? 10 : 8;
    if (std::string(argv[2]) == "contexts") {
      p.packet.max_outstanding = argc > 4 ? std::stoul(argv[4]) : 3;
      p.source_bits = argc > 5 ? std::stoul(argv[5]) : std::bit_width(p.packet.max_outstanding - 1);
      p.packet.id_bits = p.packet.source_bits = p.packet.tag_bits = 4;
    }
    p.packet.fixed_single = true;
    p.packet.timeout_cycles = 16;
    if (std::string(argv[2]) == "footprint" || std::string(argv[2]) == "write" ||
        std::string(argv[2]) == "errors") {
      p.packet.arrival = {0xfffff000, 12};
      p.packet.mappings = {{0xfffff000, 4096, 0}};
    }
    p.max_transfer_bytes = maximum;
    p.size_bits = 4;
    return lower_tilelink_uh_outlet(p);
  }();
  if (!result) {
    for (const auto &d : result.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  auto h = result.value();
  for (const auto &[name, value] : h.bindings) {
    std::string port = "nl_probe_";
    for (const auto ch : name)
      port += ch == '.' ? "_x_" : std::string(1, ch);
    h.ir.ports.push_back({port, false, h.ir.values.at(value.index).width, "probe", name});
    h.ir.drivers.push_back({PortId{h.ir.ports.size() - 1}, value});
  }
  const auto checked = verify(h.ir);
  if (!checked) {
    for (const auto &d : checked.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  const std::filesystem::path work = argv[1];
  std::filesystem::create_directories(work);
  std::ofstream rtl(work / "network.v"), ports(work / "ports.tsv");
  rtl << emit_verilog(h.ir);
  for (const auto &port : h.ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
