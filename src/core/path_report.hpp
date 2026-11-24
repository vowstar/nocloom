#pragma once
#include "network_plan.hpp"
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

// The timing model of the path report and the stage planner (docs/path-report.md).
//
// A switch link is a chain of points: its positions (LinkPlan::sender_words, receiver_words),
// with the clock crossing between the sides when there is one. The spans between consecutive
// points have delays given in Definition::link_timing. Every pipe captures both directions at
// its input; a pipe that registers a direction launches it again, and one that does not passes
// it on with a pass delay. A crossing registers both directions; its own delays belong to the
// spans next to it. A path runs from a launch to each capture it reaches: forward paths left to
// right, ready paths right to left, and turn paths forward into a width adapter and back out as
// its ready. A path with both ends inside the link fits the clock period of its side; a path
// that leaves or enters the link at a switch end fits the link allowance of that end, so the
// allowances of neighbouring links compose as the designer allocated them.
namespace nocloom::detail {
using Ps = std::uint64_t;
using MaybePs = std::optional<Ps>;

// The bound a path is checked against.
enum class TimingLimit : std::uint8_t {
  Period,
  SenderForward,
  SenderReady,
  SenderReturn,
  ReceiverForward,
  ReceiverReady,
  ThroughForward,
  ThroughReady
};
enum class TimingSignal : std::uint8_t { Forward, Ready, Turn };

struct ChainItem {
  enum class Kind : std::uint8_t { Pipe, Slot, Span, Crossing };
  Kind kind = Kind::Span;
  bool receiver = false;
  // Pipe: the configured pipe and its stage name.
  LinkPipeline pipe{};
  std::string name;
  // Slot and Span: the position it follows; a crossing span has none.
  std::optional<unsigned> position = std::nullopt;
  // Span: through a width adapter (it then has a turn), its delays and their definition field.
  bool adapter = false;
  MaybePs forward = std::nullopt, ready = std::nullopt, turn = std::nullopt;
  std::string field;
};
struct ChainPosition {
  bool receiver = false;
  unsigned word = 0, listed = 0;
  // The bits of one slot at this word: the fragment fields and the slot's flag.
  std::uint64_t slot_bits = 0;
};
struct Chain {
  std::string link;
  bool response = false;
  std::vector<ChainItem> items;
  std::vector<ChainPosition> positions;
  MaybePs sender_period, receiver_period;
  std::string sender_period_field, receiver_period_field;
  StageDelays stage;
  LinkAllowances allowances;
  // A crossing whose paths the model does not cover (not an asynchronous FIFO).
  bool unmodeled = false;
};
struct TimedPath {
  TimingSignal signal = TimingSignal::Forward;
  // Stage names, or sender_end and receiver_end; via names the span of a turn.
  std::string from, to, via;
  MaybePs delay = std::nullopt;
  TimingLimit limit = TimingLimit::Period;
  bool receiver = false;
  MaybePs bound = std::nullopt;
  bool operator==(const TimedPath &) const = default;
};
struct ChainResult {
  std::vector<TimedPath> paths;
  // Pipes that register the forward direction, and the bits of the added pipes.
  unsigned cycles = 0;
  std::uint64_t added_bits = 0;
  // Every path known and within its bound.
  bool met = false;
  // The least bound minus delay over the paths, when every path is known and bounded.
  std::optional<std::int64_t> slack = std::nullopt;
};
// The pipe the planner adds at each position (PipeSlice::None for none).
using SlotChoice = std::vector<PipeSlice>;
// The chain of a switch link, with the delays of its link_timing entry when it has one.
Chain link_chain(const NetworkPlan &plan, const Definition &definition, const LinkPlan &link,
                 const LinkDelays *delays, std::string_view field);
// Every path of the chain with the given pipes added, checked against its bound.
ChainResult evaluate_chain(const Chain &chain, const SlotChoice &added);
// The added pipes with the fewest forward registers, then the fewest bits, then the largest
// slack, among those that meet every bound; none when no choice does. Requires every delay.
std::optional<SlotChoice> plan_chain(const Chain &chain);
// The delays the chain lacks: definition fields, empty when every delay is given.
std::vector<std::string> missing_delays(const Chain &chain);

// The JSON text of <top>.path_report.json.
Result<std::string> path_report(const NetworkPlan &plan, const Definition &definition);
// The planned stage lists applied to the definition, or the unmet and unknown diagnostics
// (unlocated).
Result<Definition> link_stage_plan(const NetworkPlan &plan, const Definition &definition);
} // namespace nocloom::detail
