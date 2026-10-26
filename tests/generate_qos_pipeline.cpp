#include "core/qos_pipeline.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char **argv) {
  if (argc < 3 || argc > 4)
    return 2;
  using namespace nocloom::detail;
  const std::string mode = argv[2];
  const std::string mutant = argc == 4 ? argv[3] : "";
  if ((mode != "forward" && mode != "both") ||
      (!mutant.empty() && mutant != "level_hint_no_head" && mutant != "source_level_live_only"))
    return 2;
  const std::filesystem::path path = argv[1];
  std::filesystem::create_directories(path);
  Builder b;
  b.ir.name = "nocloom_qos_pipeline";
  const auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  const auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  const auto valid = b.input("nl_valid", 1, "input", "valid");
  const auto ready = b.input("nl_ready", 1, "input", "ready");
  const auto in_ready = b.input("nl_in_ready", 1, "input", "in_ready");
  const auto out_valid = b.input("nl_out_valid", 1, "input", "out_valid");
  const auto idle = b.input("nl_idle", 1, "input", "idle");
  const auto head = b.input("nl_head", 3, "input", "head");
  const auto level_hint = b.input("nl_level_hint", 3, "input", "level_hint");
  const auto source_level = b.input("nl_source_level", 3, "input", "source_level");
  const auto stages = nocloom::LinkPipeline{mode == "forward" || mode == "both", mode == "both"};
  const auto zero = b.constant(3, 0);
  const auto p =
      lower_qos_level_hint(b, clock, reset, valid, mutant == "level_hint_no_head" ? zero : head,
                           level_hint, ready, stages);
  const auto h = lower_qos_level(b, clock, reset, mode == "both" ? 2 : 1, source_level, idle, valid,
                                 in_ready, out_valid, ready);
  b.output("nl_out_level_hint", p.value, "output", "level_hint");
  b.output("nl_out_source_level", mutant == "source_level_live_only" ? source_level : h.value,
           "output", "source_level");
  if (const auto checked = verify(b.ir); !checked) {
    for (const auto &d : checked.diagnostics())
      std::cerr << d.code << " " << d.field << " " << d.message << "\n";
    return 1;
  }
  std::ofstream rtl(path / "network.v");
  rtl << emit_verilog(b.ir);
  return rtl ? 0 : 1;
}
