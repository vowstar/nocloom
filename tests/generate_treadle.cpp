#include "core/hardware.hpp"
#include "core/treadle.hpp"
#include <algorithm>
#include <bit>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <string>

// Emits one arbiter alone:
//   generate_treadle <dir> output <policy> <candidates> <packet|priority> [ranks]
// ranks: comma-separated rank of each candidate (fixed_priority only).
// Candidate i forwards its own index as payload, so the sink payload names
// the candidate the output multiplexer actually forwards.
int main(int argc, char **argv) {
  using namespace nocloom::detail;
  if (argc != 6 && argc != 7)
    return 2;
  const std::map<std::string, treadle::Policy> policies{
      {"round_robin", treadle::Policy::RoundRobin},
      {"rotating_priority", treadle::Policy::RotatingPriority},
      {"fixed_priority", treadle::Policy::FixedPriority},
      {"fifo", treadle::Policy::Fifo},
      {"level_round_robin", treadle::Policy::LevelRoundRobin}};
  const std::string layout = argv[2], policy = argv[3], framing = argv[5];
  if (layout != "output" || !policies.contains(policy) ||
      (framing != "packet" && framing != "priority"))
    return 2;
  const auto count = static_cast<unsigned>(std::stoul(argv[4]));
  treadle::Spec spec{policies.at(policy), {}};
  // A level round robin keeps a last winner for each of the eight 3-bit levels.
  if (spec.policy == treadle::Policy::LevelRoundRobin)
    spec.levels = framing == "priority" ? 8 : 1;
  if (argc == 7) {
    std::stringstream ranks(argv[6]);
    for (std::string rank; std::getline(ranks, rank, ',');)
      spec.rank.push_back(static_cast<unsigned>(std::stoul(rank)));
  }
  const unsigned bits = std::max(1U, static_cast<unsigned>(std::bit_width(count - 1)));
  Builder b;
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  std::vector<treadle::Candidate> candidates;
  for (unsigned i = 0; i < count; ++i) {
    const auto n = std::to_string(i);
    auto valid = b.input("nl_valid" + n, 1, "arbiter", "valid" + n);
    candidates.push_back({valid, b.input("nl_last" + n, 1, "arbiter", "last" + n)});
  }
  if (framing == "priority")
    for (unsigned i = 0; i < count; ++i)
      candidates[i].level =
          b.input("nl_priority" + std::to_string(i), 3, "arbiter", "priority" + std::to_string(i));
  auto ready = b.input("nl_ready", 1, "arbiter", "ready");
  ValueId payload{};
  std::vector<ValueId> taken(count);
  treadle::Datapath d;
  d.open = [&] { payload = b.constant(bits, 0); };
  d.candidate = [&](unsigned i, ValueId selected) {
    taken[i] = b.binary(Op::BitAnd, selected, ready);
    payload = b.select(selected, b.constant(bits, i), payload);
  };
  d.close = [&](ValueId valid) {
    b.output("nl_sink_valid", valid, "arbiter", "sink_valid");
    return std::pair{valid, ready};
  };
  // One global owner holds the output grant and the payload names the winner.
  std::vector<treadle::Arbiter> arbiters;
  try {
    arbiters = treadle::lower_output(b, clock, reset, spec, candidates, d);
  } catch (const std::logic_error &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
  const auto &a = arbiters.back();
  b.output("nl_sink_payload", payload, "arbiter", "sink_payload");
  for (unsigned i = 0; i < count; ++i)
    b.output("nl_taken" + std::to_string(i), taken[i], "arbiter", "taken" + std::to_string(i));
  b.output("nl_winner", a.winner, "arbiter", "winner");
  b.output("nl_held", a.held, "arbiter", "held");
  b.output("nl_transfer", a.transfer, "arbiter", "transfer");
  b.output("nl_release", a.release, "arbiter", "release");
  b.ir.name = "nocloom_arbiter";
  if (auto checked = verify(b.ir); !checked) {
    for (const auto &e : checked.diagnostics())
      std::cerr << e.code << ": " << e.message << '\n';
    return 1;
  }
  const std::filesystem::path path = argv[1];
  std::filesystem::create_directories(path);
  std::ofstream rtl(path / "network.v"), ports(path / "ports.tsv");
  rtl << emit_verilog(b.ir);
  for (const auto &port : b.ir.ports)
    ports << port.name << ' ' << port.width << ' ' << port.input << '\n';
  return rtl && ports ? 0 : 1;
}
