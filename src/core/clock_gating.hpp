#pragma once
#include "hardware_builder.hpp"
#include <algorithm>
#include <map>
#include <numeric>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace nocloom::detail {
// A rest rule list: space-separated state name patterns ('#' stands for a decimal index), each
// with an optional ':mask' of the values its states rest at (bit v: value v; default zero). f
// takes each matching name with its mask.
template <class F>
void each_rest(std::string_view rules, const std::map<std::string, ValueId> &named, F f) {
  for (std::size_t at = 0; at < rules.size();) {
    auto rule = rules.substr(at, rules.find(' ', at) - at);
    at += rule.size() + 1;
    const auto colon = rule.find(':');
    const unsigned mask = colon == rule.npos ? 1 : std::stoul(std::string(rule.substr(colon + 1)));
    for (const auto &[name, q] : named) {
      auto pattern = rule.substr(0, colon);
      std::string_view rest = name;
      while (!pattern.empty() && !rest.empty()) {
        const auto digits = pattern.front() == '#' ? rest.find_first_not_of("0123456789") : 1;
        if (digits == 0 || (pattern.front() != '#' && pattern.front() != rest.front()))
          break;
        pattern.remove_prefix(1);
        rest.remove_prefix(std::min(digits, rest.size()));
      }
      if (pattern.empty() && rest.empty())
        f(q, mask);
    }
  }
}
// One clock gate region, a switch or an endpoint zone of a gated domain: the states its gate
// clocks and the terms that keep the gate open. A region state changes only while a valid enters
// the region (a wake term) or while the region holds a word (an idle term reads low).
// Synchronizer and reset handshake registers stay on the domain clock outside every region.
struct GateRegion {
  std::string domain;
  std::vector<std::size_t> states;
  std::vector<ValueId> idle, wake;
  // Claims every state created since first but the named ones that match a free rule.
  void claim(const Builder &b, std::size_t first, const std::map<std::string, ValueId> &named = {},
             std::string_view free = {}) {
    for (auto i = first; i < b.ir.states.size(); ++i)
      states.push_back(i);
    each_rest(free, named, [&](ValueId q, unsigned) {
      std::erase(states, b.ir.values.at(q.index).state.index);
    });
  }
};
struct RegionGate {
  ValueId clock, enable, idle, wake, remaining;
};
// The remaining open edges include three after reset and two after sampled activity.
// The deadline advances on the gated clock, saturating at zero until a wake opens the gate.
// Test and ungate force the cell open. Synchronous reset also opens it to reach every state.
inline RegionGate lower_region_gate(Builder &b, ValueId clock, ValueId reset, ValueId test,
                                    std::optional<ValueId> ungate, const GateRegion &region,
                                    bool synchronous_reset = false) {
  auto fold = [&](const std::vector<ValueId> &terms, Op op, unsigned none) {
    if (terms.empty())
      return b.constant(1, none);
    return std::accumulate(terms.begin() + 1, terms.end(), terms.front(),
                           [&](ValueId all, ValueId term) { return b.binary(op, all, term); });
  };
  auto either = [&](ValueId x, ValueId y) { return b.binary(Op::BitOr, x, y); };
  RegionGate g{};
  g.idle = fold(region.idle, Op::BitAnd, 1);
  g.wake = fold(region.wake, Op::BitOr, 0);
  g.remaining = b.state(2, clock, reset, 3);
  const auto closed = b.binary(Op::Equal, g.remaining, b.constant(2, 0));
  const auto countdown =
      b.select(closed, g.remaining, b.binary(Op::Subtract, g.remaining, b.constant(2, 1)));
  const auto activity = either(b.unary(Op::BitNot, g.idle, 1), g.wake);
  b.update(g.remaining, b.select(activity, b.constant(2, 2), countdown));
  g.enable = either(b.unary(Op::BitNot, closed, 1), g.wake);
  if (synchronous_reset)
    g.enable = either(g.enable, b.unary(Op::BitNot, reset, 1));
  g.clock = b.clock_gate(clock, ungate ? b.binary(Op::BitOr, g.enable, *ungate) : g.enable, test);
  std::vector<std::size_t> gated = region.states;
  gated.push_back(b.ir.values.at(g.remaining.index).state.index);
  for (auto i : gated) {
    if (b.ir.states.at(i).clock.index != clock.index)
      throw std::logic_error("A gated state is off its region's domain clock");
    b.ir.states[i].clock = g.clock;
  }
  return g;
}
// The idle term of a zone's component states (binding name -> state): every ruled state at rest.
inline ValueId at_rest(Builder &b, const std::map<std::string, ValueId> &states,
                       std::string_view rules) {
  auto idle = b.constant(1, 1);
  each_rest(rules, states, [&](ValueId q, unsigned mask) {
    auto any = b.constant(1, 0);
    for (unsigned v = 0; mask >> v; ++v)
      if (mask >> v & 1)
        any = b.binary(Op::BitOr, any,
                       b.binary(Op::Equal, q, b.constant(b.ir.values.at(q.index).width, v)));
    idle = b.binary(Op::BitAnd, idle, any);
  });
  return idle;
}
// Rest rules of the endpoint components: the states that hold a transaction step a zone takes
// without a wake, or a word it offers. An engine waiting only for a valid (a response, the next
// beat) rests; an initiator with one burst per direction rests in its response wait (state 2).
// A target with a timer rests only with nothing pending, and never once it has failed.
inline constexpr std::string_view initiator_rest =
    "wr.state:5 wr.beat_full wr.b_full rr.state:5 rr.rsp_full ahb.phase ahb.err ahb.end";
inline constexpr std::string_view initiator_multi_rest =
    "wr.state wr.beat_full wr.b_full wr.next.valid rr.state rr.rsp_full rr.offered rr.next.valid";
inline constexpr std::string_view packetizer_rest = "r.pending r.word_full w.miss_ahead";
inline constexpr std::string_view width_cut_rest = "w.local_response r.busy r.queued r.answering";
inline constexpr std::string_view upsize_rest =
    "w.context#.valid r.busy r.full r.spare r.pending r.queue_count r.context#.used r.drain";
inline constexpr std::string_view outlet_rest =
    "w.busy w.head_pending w.head_beat_in w.full w.spare w.response_full w.response_spare "
    "w.b_parked r.merge_phase:5 "
    "w.slot#.valid r.count "
    "r.full r.spare timeout.awvalid timeout.wvalid timeout.arvalid w.response_tail_full r.busy "
    "r.tail_full bus.phase w.packet.t#.count w.rsp.full w.rsp.spare w.buf.full w.buf.spare "
    "r.packet.t#.count r.rsp.full r.rsp.spare bus.open w.context#.valid bus.address_phase "
    "bus.data_phase r.inflight r.issuing w.next_full w.flight timeout.pending timeout.failed "
    "bus.step bus.local r.unissued";
// States that change without a wake while their zone rests stay on the domain clock: an exclusive
// monitor's LFSR steps.
inline constexpr std::string_view outlet_free = "exclusive.lfsr";
} // namespace nocloom::detail
