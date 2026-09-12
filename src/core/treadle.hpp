#pragma once
#include "hardware_builder.hpp"
#include <bit>
#include <functional>
#include <optional>
#include <span>
#include <string>
#include <utility>
#include <vector>

// Treadle: the shared output arbiter of the switch graph lowering.
//
// An arbiter selects one candidate per physical output and holds that grant (owner/held) from the
// packet head until its tail transfers. Hold and release are common to every policy; a policy
// decides only the unheld choice and how its own state moves. The output multiplexer stays with the
// caller, which supplies it as callbacks so the netlist keeps a fixed construction order.
namespace nocloom::detail::treadle {
enum class Policy {
  // Search starts after the last released winner.
  RoundRobin,
  // A priority tree read from a counter of ceil(log2 n) bits that steps once per release, whoever
  // won, and wraps at 2^ceil(log2 n). A set of size s splits into a lower half (the floor(s/2)
  // smallest indexes) and an upper half; counter bit ceil(log2 s) - 1 puts the upper half first,
  // and each half orders itself the same way from the lower counter bits. With two candidates the
  // counter is the index searched first.
  RotatingPriority,
  // The valid candidate of smallest rank wins; no fairness bound.
  FixedPriority,
  // First come, first served: the earliest waiting request wins. Requests
  // arriving in the same cycle keep their previous relative order (highest
  // index first after reset); a released winner queues behind every other.
  Fifo,
  // One last winner per scheduling level (Spec::levels of them, all candidate 0 after reset): an
  // unheld choice searches from the candidate after the last winner of the highest valid level
  // and records its winner there; without levels every choice uses level 0.
  LevelRoundRobin,
};
struct Spec {
  Policy policy = Policy::RoundRobin;
  // FixedPriority only: rank[i] of candidate i, 0 highest; a permutation.
  std::vector<unsigned> rank;
  // LevelRoundRobin only: the levels a candidate may take, 0 to levels - 1.
  unsigned levels = 1;
  // Candidate levels are thermometer codes (level k: the k low bits set) rather than binary.
  bool thermometer = false;
  // The output's valid does not wait for the grant: the grant takes a valid candidate whenever
  // there is one. For a merge whose grant fans out to a wide datapath behind a late valid.
  bool direct_valid = false;
  bool operator==(const Spec &) const = default;
};
// Eventual service of one arbiter over n candidates, the fairness premise of the deadlock argument
// (a held grant releases on its own tail transfer; a waiting candidate must then be chosen
// eventually). Structural policies bound the releases that pass a continuously waiting candidate: n
// - 1 for round robin and FIFO, 2^ceil(log2 n) for rotating priority. Every candidate is first in
// the tree for at least one counter value, and a deepest one for exactly one, so a candidate can
// wait 2^ceil(log2 n) - 1 decisions plus the release of a grant decided just before it arrived, at
// the counter value that would have chosen it. Fixed priority has no bound: a lower rank is served
// only at a grant decision where no higher rank requests, so its eventual service needs higher
// ranks to yield such a decision eventually, an environment premise rather than a property of the
// arbiter. Every output has one global policy state.
struct Service {
  bool structural = true;
  unsigned bound = 0; // structural only: releases that may pass a waiting candidate
};
inline Service service(Policy policy, unsigned candidates) {
  if (policy == Policy::FixedPriority)
    return {false, 0};
  switch (policy) {
  case Policy::RoundRobin:
  case Policy::Fifo:
  case Policy::LevelRoundRobin:
    return {true, candidates - 1};
  case Policy::RotatingPriority:
    return {true, std::bit_ceil(candidates)};
  case Policy::FixedPriority:
    break;
  }
  return {false, 0};
}
// Configuration name of a policy (round_robin, fifo, rotating_priority, fixed_priority,
// level_round_robin).
inline const char *policy_name(Policy policy) {
  switch (policy) {
  case Policy::RoundRobin:
    return "round_robin";
  case Policy::RotatingPriority:
    return "rotating_priority";
  case Policy::FixedPriority:
    return "fixed_priority";
  case Policy::Fifo:
    return "fifo";
  case Policy::LevelRoundRobin:
    return "level_round_robin";
  }
  return "unknown";
}
struct Candidate {
  ValueId valid;
  // Packet tail marker (required).
  std::optional<ValueId> last;
  // Priority level, on every candidate or none, one common width: only valid
  // candidates of the highest valid level are eligible for an unheld choice.
  // Arrival ordering and a held packet continue to use the original valid.
  std::optional<ValueId> level = std::nullopt;
};
// Caller-owned output multiplexer, invoked at fixed construction points.
struct Datapath {
  // After the arbiter's accumulators exist, before the first candidate.
  std::function<void()> open;
  // Once per candidate with its selection term (winner == index).
  std::function<void(unsigned, ValueId)> candidate;
  // Receives the OR of selected valids; returns the sink {valid, ready}.
  std::function<std::pair<ValueId, ValueId>(ValueId)> close;
};
struct Arbiter {
  ValueId winner, transfer, release, engage, owner, held;
  std::optional<ValueId> tail, level; // of the selected candidate
  std::vector<ValueId> eligible;      // per candidate, given levels
  // Policy state with its binding suffix: ".cursor", or the FIFO order bits ".order<i>_<j>" (i <
  // j), each set while candidate i is queued ahead of j. Every register resets to zero.
  std::vector<std::pair<std::string, ValueId>> state;
  // Every register of this arbiter in creation order, for ownership records.
  std::vector<ValueId> registers;
};
// Balanced OR of terms of one width (at least one): the join of an AND-OR multiplexer, whose
// terms are each a candidate's value masked by a selection that is one-hot or zero.
ValueId join(Builder &b, std::span<const ValueId> terms);
// Rotating priority over requests [first, first + size): {any request, its index}, the index of
// the cursor's width. The lower half holds the size / 2 smallest indexes; cursor bit
// ceil(log2 size) - 1 set puts the upper half first.
std::pair<ValueId, ValueId> rotating_choice(Builder &b, std::span<const ValueId> requests,
                                            ValueId cursor, unsigned first, unsigned size);
// Lowers one arbiter over two or more candidates. Throws std::logic_error on a malformed
// candidate set or spec. Tail,
// level and the round robin successor are accumulated inside the output multiplexer.
Arbiter lower_arbiter(Builder &b, ValueId clock, ValueId reset, const Spec &spec,
                      const std::vector<Candidate> &candidates, const Datapath &datapath);
// One global arbiter per output. The datapath sees each candidate with its selection term.
std::vector<Arbiter> lower_output(Builder &b, ValueId clock, ValueId reset, const Spec &spec,
                                  const std::vector<Candidate> &candidates,
                                  const Datapath &datapath);
} // namespace nocloom::detail::treadle
