#include "core/ahb_outlet.hpp"
#include "core/axi4_outlet.hpp"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

// Emits one outlet with a target timer for the formal harness:
// generate_timeout_outlet <dir> <apb4|apb3|axi4_lite|ahb_v3|axi4|axi4x2> <cycles>
//   <completion|report> [binding...]
// axi4x2 is the AXI4 outlet with two bursts in flight (one source, index 0).
int main(int argc, char **argv) {
  if (argc < 5)
    return 2;
  using namespace nocloom::detail;
  const std::string kind = argv[2];
  const unsigned cycles = std::stoul(argv[3]);
  const std::string answer = argv[4];
  if (answer != "completion" && answer != "report")
    return 2;
  const nocloom::AddressWindow window{0x1000, 0x1000};
  if (kind != "axi4" && kind != "axi4x2" && kind != "apb4" && kind != "apb3" &&
      kind != "axi4_lite" && kind != "ahb_v3")
    return 2;
  const auto outlet = [&] {
    if (kind.starts_with("axi4") && kind != "axi4_lite") {
      Axi4OutletProfile p{"probe", 32, 0, 4, 0, true, {{window.base, window.size}}, 12};
      p.timeout_cycles = cycles;
      p.timeout_completion = answer == "completion";
      if (kind == "axi4x2") {
        p.source_bits = 1;
        p.max_outstanding = 2;
        p.id_sources = {0};
      }
      return lower_axi4_outlet(p);
    }
    BurstOutletProfile p{"probe", 0, 4, 0, {{window.base, window.size}}, 12};
    p.timeout_cycles = cycles;
    p.timeout_completion = answer == "completion";
    if (kind == "axi4_lite")
      return lower_axi_lite_outlet(p);
    if (kind == "ahb_v3")
      return lower_ahb_outlet(p);
    p.dialect = kind == "apb4" ? nocloom::Protocol::Apb4 : nocloom::Protocol::Apb3;
    return lower_apb_burst_outlet(p);
  }();
  if (!outlet) {
    for (const auto &d : outlet.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  // Probe outputs of the watchdog count and of its counting condition
  // (nl_probe_timeout_count, nl_probe_timeout_waiting) and of any further
  // binding named on the command line (nl_probe_ and the name with dots as
  // underscores), for the harness's helper invariants.
  auto h = outlet.value();
  std::vector<std::string> probes{"timeout.count", "timeout.waiting"};
  probes.insert(probes.end(), argv + 5, argv + argc);
  for (const auto &name : probes) {
    const auto it = h.bindings.find(name);
    if (it == h.bindings.end()) {
      std::cerr << "no binding " << name << '\n';
      return 1;
    }
    auto port = "nl_probe_" + name;
    std::ranges::replace(port, '.', '_');
    h.ir.ports.push_back({port, false, h.ir.values.at(it->second.index).width, "probe", name});
    h.ir.drivers.push_back({PortId{h.ir.ports.size() - 1}, it->second});
  }
  if (!verify(h.ir))
    return 1;
  const std::filesystem::path dir = argv[1];
  std::filesystem::create_directories(dir);
  std::ofstream rtl(dir / "outlet.v"), ports(dir / "ports.tsv");
  rtl << emit_verilog(h.ir);
  for (const auto &port : h.ir.ports)
    ports << port.name << '\t' << port.width << '\t' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
