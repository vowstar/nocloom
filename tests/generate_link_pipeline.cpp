#include "core/clock_gating.hpp"
#include "core/link_pipeline.hpp"
#include <exception>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char **argv) {
  // out [forward|backward|both|half] [0|1] [ungated-consumer]; the optional value drives bypass.
  if (argc < 2 || argc > 5)
    return 2;
  using namespace nocloom::detail;
  const std::string mode = argc >= 3 ? argv[2] : "both";
  if (mode != "forward" && mode != "backward" && mode != "both" && mode != "half")
    return 2;
  if ((argc >= 4 && std::string(argv[3]) != "0" && std::string(argv[3]) != "1") ||
      (argc == 5 && std::string(argv[4]) != "1"))
    return 2;
  const std::filesystem::path path = argv[1];
  std::filesystem::create_directories(path);
  Builder b;
  b.ir.name = "nocloom_link_pipeline";
  const auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  const auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  const auto valid = b.input("nl_in_valid", 1, "input", "valid");
  const auto ready = b.input("nl_out_ready", 1, "output", "ready");
  const auto data = b.input("nl_in_data", 257, "input", "data");
  const auto last = b.input("nl_in_last", 1, "input", "last");
  const auto head = b.input("nl_in_head", 1, "input", "head");
  const auto bypass = argc >= 4 ? b.input("nl_bypass", 1, "bypass:static", "bypass") : ValueId{};
  const auto p =
      lower_link_pipeline(b, clock, reset, valid, {data, last, head}, ready,
                          {mode != "backward", mode != "forward", mode == "half", {}, argc == 5},
                          true, argc >= 4 ? std::optional{bypass} : std::nullopt);
  b.output("nl_in_ready", p.ready, "input", "ready");
  b.output("nl_out_valid", p.valid, "output", "valid");
  b.output("nl_out_data", p.data[0], "output", "data");
  b.output("nl_out_last", p.data[1], "output", "last");
  b.output("nl_out_head", p.data[2], "output", "head");
  // The forward slot and the backward slot; a stage without one reads zero.
  b.output("nl_full", mode != "backward" ? p.control.front() : b.constant(1, 0), "debug", "full");
  b.output("nl_spare", mode == "backward" || mode == "both" ? p.control.back() : b.constant(1, 0),
           "debug", "spare");
  if (argc == 5) {
    const auto consumer_first = b.ir.states.size();
    const auto busy = b.state(1, clock, reset);
    b.update(busy, b.binary(Op::BitAnd, p.valid, b.unary(Op::BitNot, ready, 1)));
    GateRegion region;
    region.domain = "main";
    region.claim(b, consumer_first);
    region.idle.push_back(b.unary(Op::BitNot, busy, 1));
    region.wake.push_back(p.valid);
    try {
      const auto g = lower_region_gate(b, clock, reset, b.constant(1, 0), std::nullopt, region);
      b.output("nl_region_enable", g.enable, "debug", "region_enable");
    } catch (const std::exception &e) {
      std::cerr << e.what() << "\n";
      return 1;
    }
  }
  if (const auto checked = verify_structure(b.ir); !checked) {
    for (const auto &d : checked.diagnostics())
      std::cerr << d.code << " " << d.field << " " << d.message << "\n";
    return 1;
  }
  std::ofstream rtl(path / "network.v");
  for (const auto &cell : cell_files(b.ir))
    rtl << cell.text;
  rtl << emit_verilog(b.ir);
  return rtl ? 0 : 1;
}
