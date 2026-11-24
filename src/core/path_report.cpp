#include "path_report.hpp"
#include "diagnostic.hpp"
#include "network_plan_profiles.hpp"
#include "nocloom/generation.hpp"
#include "width_adapter.hpp"
#include <algorithm>
#include <array>
#include <limits>
#include <map>
#include <nlohmann/json.hpp>
#include <set>
#include <stdexcept>
#include <tuple>

namespace nocloom::detail {
namespace {
using Json = nlohmann::json;
MaybePs plus(MaybePs a, MaybePs b) { return a && b ? MaybePs{*a + *b} : std::nullopt; }
bool registers_forward(const LinkPipeline &p) { return p.forward_stage() && p.bypass_port.empty(); }
bool passes_forward(const LinkPipeline &p) { return !registers_forward(p); }
bool launches_forward(const LinkPipeline &p) { return p.forward_stage(); }
// A half pipe's bypass also opens its ready path; a full pipe keeps its backward slot.
bool registers_ready(const LinkPipeline &p) {
  return p.backward_stage() && !(p.slice == PipeSlice::Half && !p.bypass_port.empty());
}
bool passes_ready(const LinkPipeline &p) { return !registers_ready(p); }
bool launches_ready(const LinkPipeline &p) { return p.backward_stage(); }
std::string side_name(bool receiver) { return receiver ? "receiver" : "sender"; }
std::string graph_name(bool response) { return response ? "response" : "request"; }
const char *slice_name(PipeSlice s) {
  static constexpr std::array table{"none", "forward", "backward", "full", "half"};
  return table.at(static_cast<std::size_t>(s));
}
const char *limit_name(TimingLimit limit) {
  static constexpr std::array table{"period",          "sender.forward",   "sender.ready",
                                    "sender.return",   "receiver.forward", "receiver.ready",
                                    "through.forward", "through.ready"};
  return table.at(static_cast<std::size_t>(limit));
}
const char *signal_name(TimingSignal s) {
  static constexpr std::array table{"forward", "ready", "turn"};
  return table.at(static_cast<std::size_t>(s));
}
MaybePs allowance(const LinkAllowances &b, TimingLimit limit) {
  switch (limit) {
  case TimingLimit::SenderForward:
    return b.sender.forward_ps;
  case TimingLimit::SenderReady:
    return b.sender.ready_ps;
  case TimingLimit::SenderReturn:
    return b.sender.return_ps;
  case TimingLimit::ReceiverForward:
    return b.receiver.forward_ps;
  case TimingLimit::ReceiverReady:
    return b.receiver.ready_ps;
  case TimingLimit::ThroughForward:
    return b.through.forward_ps;
  case TimingLimit::ThroughReady:
    return b.through.ready_ps;
  case TimingLimit::Period:
    break;
  }
  return std::nullopt;
}
MaybePs bound_of(const Chain &c, TimingLimit limit, bool receiver) {
  if (limit == TimingLimit::Period)
    return receiver ? c.receiver_period : c.sender_period;
  return allowance(c.allowances, limit);
}
// The pipe a slot choice adds; none adds no pipe.
LinkPipeline added_pipe(PipeSlice s) { return LinkPipeline{.slice = s}; }
std::string added_name(const Chain &c, unsigned position) {
  const auto &p = c.positions.at(position);
  return "pipeline/" + c.link + "/" + side_name(p.receiver) + "/" + std::to_string(p.word) + "/" +
         std::to_string(p.listed);
}

// The chain with the slots filled: pipes, spans and the crossing in data flow order.
struct Element {
  ChainItem::Kind kind = ChainItem::Kind::Span;
  bool receiver = false;
  LinkPipeline pipe{};
  std::string name;
  const ChainItem *span = nullptr;
};
std::vector<Element> elements(const Chain &c, const SlotChoice &added) {
  std::vector<Element> out;
  for (const auto &item : c.items) {
    if (item.kind == ChainItem::Kind::Slot) {
      const auto s = added.at(*item.position);
      if (s != PipeSlice::None)
        out.push_back({ChainItem::Kind::Pipe, item.receiver, added_pipe(s),
                       added_name(c, *item.position), nullptr});
      continue;
    }
    out.push_back({item.kind, item.receiver, item.pipe, item.name, &item});
  }
  return out;
}
const std::string sender_end = "sender_end", receiver_end = "receiver_end";
ChainItem item_of(ChainItem::Kind kind, bool receiver, std::string name = {},
                  std::optional<unsigned> position = std::nullopt) {
  ChainItem item;
  item.kind = kind;
  item.receiver = receiver;
  item.name = std::move(name);
  item.position = position;
  return item;
}
} // namespace

Chain link_chain(const NetworkPlan &plan, const Definition &definition, const LinkPlan &link,
                 const LinkDelays *delays, std::string_view field) {
  Chain c;
  c.link = link.name;
  c.response = link.response;
  const auto profile = transport_burst(plan);
  const auto slot_bits = [&](unsigned word) {
    std::uint64_t bits = 1;
    for (const auto &[name, width] :
         fragment_fields(link.response, word, plan.source_bits, profile))
      bits += width;
    return bits;
  };
  for (bool receiver : {false, true})
    for (const auto word : receiver ? link.receiver_words : link.sender_words) {
      const auto listed = std::ranges::count_if(link.stages, [&](const StagePlan &s) {
        return s.kind == StageKind::Pipe && s.listed && s.receiver == receiver &&
               s.wide_bytes == word;
      });
      c.positions.push_back({receiver, word, static_cast<unsigned>(listed), slot_bits(word)});
    }
  const auto senders = static_cast<unsigned>(link.sender_words.size());
  const auto receivers = static_cast<unsigned>(link.receiver_words.size());
  const bool crossing = std::ranges::any_of(
      link.stages, [](const StagePlan &s) { return s.kind == StageKind::Crossing; });
  // The span delays of the entry of a side and word, with that entry's field.
  const auto span = [&](ChainItem &item, bool receiver, unsigned word) {
    if (!delays)
      return;
    const auto end = receiver ? link.to_bytes : link.from_bytes;
    for (std::size_t j = 0; j < delays->spans.size(); ++j) {
      const auto &s = delays->spans[j];
      if ((s.side == LinkSide::Receiver) == receiver && s.word_bytes.value_or(end) == word) {
        item.forward = s.forward_ps;
        item.ready = s.ready_ps;
        item.turn = s.turn_ps;
        item.field = std::string(field) + ".spans[" + std::to_string(j) + "]";
        return;
      }
    }
    item.field = std::string(field) + ".spans";
  };
  const auto stage_of = [&](bool listed, bool receiver, unsigned word) {
    std::vector<ChainItem> out;
    for (const auto &s : link.stages)
      if (s.kind == StageKind::Pipe && s.listed.has_value() == listed && s.receiver == receiver &&
          (!listed || s.wide_bytes == word)) {
        out.push_back(item_of(ChainItem::Kind::Pipe, receiver, s.name));
        out.back().pipe = s.pipe;
      }
    return out;
  };
  const auto append = [&](std::vector<ChainItem> items) {
    std::ranges::move(items, std::back_inserter(c.items));
  };
  append(stage_of(false, false, 0));
  for (unsigned i = 0; i < senders + receivers; ++i) {
    const bool receiver = i >= senders;
    if (i == senders && crossing) {
      c.items.push_back(item_of(ChainItem::Kind::Crossing, true, "cut/" + link.name));
      auto after = item_of(ChainItem::Kind::Span, true, "crossing");
      if (delays) {
        after.field = std::string(field) + ".crossing";
        if (delays->crossing) {
          after.forward = delays->crossing->forward_ps;
          after.ready = delays->crossing->ready_ps;
        }
      }
      c.items.push_back(after);
    }
    const auto word = c.positions[i].word;
    append(stage_of(true, receiver, word));
    c.items.push_back(item_of(ChainItem::Kind::Slot, receiver, {}, i));
    // A span after each position but the last: an adapter between the positions of one side, or
    // the link wire after the last sender position.
    if (i + 1 < senders + receivers) {
      auto next = item_of(ChainItem::Kind::Span, receiver,
                          side_name(receiver) + "/" + std::to_string(word), i);
      next.adapter = i + 1 != senders;
      span(next, receiver, word);
      c.items.push_back(next);
    }
  }
  append(stage_of(false, true, 0));
  // The chain must follow the lowered stages: their pipes, adapters and crossing in order.
  std::vector<std::string> lowered, modeled;
  std::ranges::transform(link.stages, std::back_inserter(lowered), [](const StagePlan &s) {
    return s.kind == StageKind::Pipe       ? s.name
           : s.kind == StageKind::Crossing ? std::string("crossing")
                                           : std::string("adapter");
  });
  for (const auto &item : c.items)
    if (item.kind == ChainItem::Kind::Pipe)
      modeled.push_back(item.name);
    else if (item.kind == ChainItem::Kind::Crossing)
      modeled.push_back("crossing");
    else if (item.kind == ChainItem::Kind::Span && item.adapter)
      modeled.push_back("adapter");
  if (lowered != modeled)
    throw std::logic_error("The timing chain of " + link.name + " differs from its stages");
  if (crossing) {
    const auto cut = std::ranges::find(plan.cuts, "cut/" + link.name, &ClockCut::name);
    c.unmodeled = cut == plan.cuts.end() || !std::holds_alternative<AsyncClockCut>(cut->options);
  }
  // Periods: the link_timing entry of the domain, else the placed domain's own.
  const LinkTiming none{};
  const auto &timing = definition.link_timing ? *definition.link_timing : none;
  const auto period = [&](const std::string &domain) -> MaybePs {
    if (const auto p = std::ranges::find(timing.periods, domain, &DomainPeriod::clock_domain);
        p != timing.periods.end())
      return p->period_ps;
    if (!plan.placement)
      return std::nullopt;
    const auto d = std::ranges::find(plan.placement->domains, domain, &DomainPlan::name);
    if (d == plan.placement->domains.end() || !d->effective_period_ps)
      return std::nullopt;
    return static_cast<Ps>(d->effective_period_ps->whole());
  };
  const auto period_field = [&](const std::string &domain) -> std::string {
    if (const auto p = std::ranges::find(timing.periods, domain, &DomainPeriod::clock_domain);
        p != timing.periods.end())
      return "link_timing.periods[" + std::to_string(p - timing.periods.begin()) + "].period_ps";
    const auto d = std::ranges::find(definition.clock_domains, domain, &ClockDomain::name);
    if (plan.placement && d != definition.clock_domains.end() && d->period_ps)
      return "clock_domains[" + std::to_string(d - definition.clock_domains.begin()) +
             "].period_ps";
    return "link_timing.periods";
  };
  c.sender_period = period(link.from_domain);
  c.receiver_period = period(link.to_domain);
  c.sender_period_field = period_field(link.from_domain);
  c.receiver_period_field = period_field(link.to_domain);
  if (definition.link_timing)
    c.stage = definition.link_timing->stage;
  if (delays)
    c.allowances = delays->allowances;
  return c;
}

ChainResult evaluate_chain(const Chain &c, const SlotChoice &added) {
  const auto e = elements(c, added);
  const auto n = static_cast<int>(e.size());
  const auto &st = c.stage;
  ChainResult result;
  for (const auto &x : e)
    if (x.kind == ChainItem::Kind::Pipe && x.pipe.forward_stage())
      ++result.cycles;
  for (std::size_t i = 0; i < added.size(); ++i)
    result.added_bits += added_pipe(added[i]).slot_count() * c.positions[i].slot_bits;
  const auto record = [&](TimingSignal signal, const std::string &from, const std::string &to,
                          const std::string &via, MaybePs delay, TimingLimit limit, bool receiver) {
    result.paths.push_back(
        {signal, from, to, via, delay, limit, receiver, bound_of(c, limit, receiver)});
  };
  // A forward walk from element `from` (n for the sender end) with the delay at its start: the
  // captures it reaches and, at each turn, its arrival.
  struct Reach {
    int at = 0;
    MaybePs delay = std::nullopt;
    bool end = false;
  };
  const auto walk_forward = [&](int from, MaybePs delay, std::vector<Reach> &captures,
                                std::vector<Reach> &turns) {
    for (int i = from + 1; i < n; ++i) {
      const auto &x = e[i];
      if (x.kind == ChainItem::Kind::Span) {
        if (x.span->adapter)
          turns.push_back({i, delay, false});
        delay = plus(delay, x.span->forward);
      } else if (x.kind == ChainItem::Kind::Pipe) {
        captures.push_back({i, plus(delay, st.capture_ps), false});
        if (!passes_forward(x.pipe))
          return;
        delay = plus(delay, st.forward_pass_ps);
      } else {
        captures.push_back({i, delay, false});
        return;
      }
    }
    captures.push_back({n, delay, true});
  };
  const auto walk_ready = [&](int from, MaybePs delay, std::vector<Reach> &captures) {
    for (int i = from - 1; i >= 0; --i) {
      const auto &x = e[i];
      if (x.kind == ChainItem::Kind::Span)
        delay = plus(delay, x.span->ready);
      else if (x.kind == ChainItem::Kind::Pipe) {
        captures.push_back({i, plus(delay, st.capture_ps), false});
        if (!passes_ready(x.pipe))
          return;
        delay = plus(delay, st.ready_pass_ps);
      } else {
        captures.push_back({i, delay, false});
        return;
      }
    }
    captures.push_back({-1, delay, true});
  };
  const auto name = [&](int i) {
    return i < 0 ? sender_end : i >= n ? receiver_end : e[static_cast<std::size_t>(i)].name;
  };
  // Forward starts: the sender end, each pipe that registers forward, the crossing.
  for (int s = -1; s < n; ++s) {
    MaybePs launch;
    bool receiver = false;
    if (s < 0)
      launch = 0;
    else if (e[s].kind == ChainItem::Kind::Pipe && launches_forward(e[s].pipe)) {
      launch = st.launch_ps;
      receiver = e[s].receiver;
    } else if (e[s].kind == ChainItem::Kind::Crossing) {
      launch = 0;
      receiver = true;
    } else
      continue;
    std::vector<Reach> captures, turns;
    walk_forward(s, launch, captures, turns);
    for (const auto &r : captures)
      record(TimingSignal::Forward, name(s), name(r.at), {}, r.delay,
             s < 0 ? (r.end ? TimingLimit::ThroughForward : TimingLimit::SenderForward)
                   : (r.end ? TimingLimit::ReceiverForward : TimingLimit::Period),
             receiver);
    for (const auto &t : turns) {
      std::vector<Reach> back;
      walk_ready(t.at, 0, back);
      for (const auto &r : back)
        record(TimingSignal::Turn, name(s), name(r.at), e[t.at].span->name,
               plus(plus(t.delay, e[t.at].span->turn), r.delay),
               s < 0 ? (r.end ? TimingLimit::SenderReturn : TimingLimit::SenderForward)
                     : (r.end ? TimingLimit::SenderReady : TimingLimit::Period),
               receiver);
    }
  }
  // Ready starts: the receiver end, each pipe that registers ready, the crossing.
  for (int s = n; s >= 0; --s) {
    MaybePs launch;
    bool receiver = true;
    if (s == n)
      launch = 0;
    else if (e[s].kind == ChainItem::Kind::Pipe && launches_ready(e[s].pipe)) {
      launch = st.launch_ps;
      receiver = e[s].receiver;
    } else if (e[s].kind == ChainItem::Kind::Crossing) {
      launch = 0;
      receiver = false;
    } else
      continue;
    std::vector<Reach> captures;
    walk_ready(s, launch, captures);
    for (const auto &r : captures)
      record(TimingSignal::Ready, name(s), name(r.at), {}, r.delay,
             s == n ? (r.end ? TimingLimit::ThroughReady : TimingLimit::ReceiverReady)
                    : (r.end ? TimingLimit::SenderReady : TimingLimit::Period),
             receiver);
  }
  result.met = !c.unmodeled;
  for (const auto &p : result.paths) {
    if (!p.delay || !p.bound) {
      result.met = false;
      continue;
    }
    const auto slack = static_cast<std::int64_t>(*p.bound) - static_cast<std::int64_t>(*p.delay);
    result.met = result.met && slack >= 0;
    if (!result.slack || slack < *result.slack)
      result.slack = slack;
  }
  if (std::ranges::any_of(result.paths, [](const TimedPath &p) { return !p.delay || !p.bound; }))
    result.slack.reset();
  return result;
}

std::vector<std::string> missing_delays(const Chain &c) {
  std::vector<std::string> out;
  const auto need = [&](const MaybePs &value, const std::string &at) {
    if (!value && std::ranges::find(out, at) == out.end())
      out.push_back(at);
  };
  const std::string timing = "link_timing.stage.";
  need(c.stage.launch_ps, timing + "launch_ps");
  need(c.stage.capture_ps, timing + "capture_ps");
  need(c.stage.forward_pass_ps, timing + "forward_pass_ps");
  need(c.stage.ready_pass_ps, timing + "ready_pass_ps");
  for (const auto &item : c.items) {
    if (item.kind != ChainItem::Kind::Span)
      continue;
    const bool entry = item.field.ends_with("]") || item.field.ends_with(".crossing");
    const auto at = [&](const char *key) { return entry ? item.field + "." + key : item.field; };
    need(item.forward, at("forward_ps"));
    need(item.ready, at("ready_ps"));
    if (item.adapter)
      need(item.turn, at("turn_ps"));
  }
  need(c.sender_period, c.sender_period_field);
  need(c.receiver_period, c.receiver_period_field);
  return out;
}

namespace {
// The dynamic program over the chain. The state at a point is what the paths through it still
// need: the forward delay from the last in-link launch and from the sender end, and the ready
// delay back to the farthest in-link capture and to the sender end. These decide every later
// check, so the best choice per state is kept, by forward registers, bits and slack.
class ChainPlanner {
public:
  explicit ChainPlanner(const Chain &c) : c_(c) {
    const auto &st = c.stage;
    if (!st.launch_ps || !st.capture_ps || !st.forward_pass_ps || !st.ready_pass_ps)
      throw std::logic_error("Planning a chain without stage delays");
    launch_ = *st.launch_ps, capture_ = *st.capture_ps, fpass_ = *st.forward_pass_ps,
    rpass_ = *st.ready_pass_ps;
    states_[State{std::nullopt, Ps{0}, std::nullopt, Ps{0}}] =
        Best{0, 0, std::numeric_limits<std::int64_t>::max(), SlotChoice(c.positions.size())};
  }

  std::optional<SlotChoice> run() {
    for (const auto &item : c_.items) {
      std::map<State, Best> next;
      for (const auto &[s, best] : states_)
        step(item, s, best, next);
      states_ = std::move(next);
    }
    return finish();
  }

private:
  using State = std::tuple<MaybePs, MaybePs, MaybePs, MaybePs>; // fcut, fend, rcap, rend
  struct Best {
    unsigned cycles = 0;
    std::uint64_t bits = 0;
    std::int64_t slack = std::numeric_limits<std::int64_t>::max();
    SlotChoice choice;
    bool better(const Best &o) const {
      return std::tuple{cycles, bits, -slack} < std::tuple{o.cycles, o.bits, -o.slack};
    }
  };
  using States = std::map<State, Best>;

  static MaybePs add(const MaybePs &a, Ps b) { return a ? MaybePs{*a + b} : std::nullopt; }
  static MaybePs max_of(const MaybePs &a, const MaybePs &b) {
    if (!a)
      return b;
    if (!b)
      return a;
    return std::max(*a, *b);
  }
  static void keep(States &into, const State &s, Best b) {
    const auto it = into.find(s);
    if (it == into.end() || b.better(it->second))
      into[s] = std::move(b);
  }

  // A check: false when the value exceeds its bound or the bound is absent.
  bool fits(Best &b, const MaybePs &value, TimingLimit limit, bool side) const {
    if (!value)
      return true;
    const auto bound = bound_of(c_, limit, side);
    if (!bound || *value > *bound)
      return false;
    b.slack = std::min(b.slack, static_cast<std::int64_t>(*bound - *value));
    return true;
  }

  // One pipe at the current point: its captures, launches and passes.
  std::optional<State> through(const State &s, Best &b, const LinkPipeline &pipe, bool side) const {
    auto [fc, fe, rc, re] = s;
    if (!fits(b, add(fc, capture_), TimingLimit::Period, side) ||
        !fits(b, add(fe, capture_), TimingLimit::SenderForward, side))
      return std::nullopt;
    if (launches_ready(pipe) && (!fits(b, add(rc, launch_), TimingLimit::Period, side) ||
                                 !fits(b, add(re, launch_), TimingLimit::SenderReady, side)))
      return std::nullopt;
    const MaybePs fc_pass = add(fc, fpass_), fe_pass = add(fe, fpass_);
    if (!passes_forward(pipe))
      fc = launch_, fe = std::nullopt;
    else
      fc = launches_forward(pipe) ? max_of(MaybePs{launch_}, fc_pass) : fc_pass, fe = fe_pass;
    if (!passes_ready(pipe))
      rc = capture_, re = std::nullopt;
    else
      rc = max_of(MaybePs{capture_}, add(rc, rpass_)), re = add(re, rpass_);
    return State{fc, fe, rc, re};
  }

  void step(const ChainItem &item, const State &s, const Best &best, States &next) const {
    if (item.kind == ChainItem::Kind::Pipe) {
      auto b = best;
      if (const auto t = through(s, b, item.pipe, item.receiver))
        keep(next, *t, std::move(b));
    } else if (item.kind == ChainItem::Kind::Slot) {
      slot(item, s, best, next);
    } else if (item.kind == ChainItem::Kind::Crossing) {
      crossing(s, best, next);
    } else {
      span(item, s, best, next);
    }
  }

  // A free position: no pipe, or one pipe of each slice kind.
  void slot(const ChainItem &item, const State &s, const Best &best, States &next) const {
    for (const auto slice :
         {PipeSlice::None, PipeSlice::Forward, PipeSlice::Backward, PipeSlice::Full}) {
      auto b = best;
      if (slice == PipeSlice::None) {
        keep(next, s, std::move(b));
        continue;
      }
      b.choice.at(*item.position) = slice;
      b.cycles += added_pipe(slice).forward_stage() ? 1 : 0;
      b.bits += added_pipe(slice).slot_count() * c_.positions.at(*item.position).slot_bits;
      if (const auto t = through(s, b, added_pipe(slice), item.receiver))
        keep(next, *t, std::move(b));
    }
  }

  // A clock crossing ends every path of the sender side.
  void crossing(const State &s, const Best &best, States &next) const {
    auto b = best;
    const auto &[fc, fe, rc, re] = s;
    if (fits(b, fc, TimingLimit::Period, false) && fits(b, fe, TimingLimit::SenderForward, false) &&
        fits(b, rc, TimingLimit::Period, false) && fits(b, re, TimingLimit::SenderReady, false))
      keep(next, State{Ps{0}, std::nullopt, Ps{0}, std::nullopt}, std::move(b));
  }

  // A wire span, with the turn path of a width adapter.
  void span(const ChainItem &item, const State &s, const Best &best, States &next) const {
    auto b = best;
    const auto &[fc, fe, rc, re] = s;
    if (item.adapter) {
      const Ps turn = item.turn.value();
      const auto via = [&](const MaybePs &a, const MaybePs &z) {
        return a && z ? MaybePs{*a + turn + *z} : std::nullopt;
      };
      if (!fits(b, via(fc, rc), TimingLimit::Period, item.receiver) ||
          !fits(b, via(fc, re), TimingLimit::SenderReady, item.receiver) ||
          !fits(b, via(fe, rc), TimingLimit::SenderForward, item.receiver) ||
          !fits(b, via(fe, re), TimingLimit::SenderReturn, item.receiver))
        return;
    }
    const Ps f = item.forward.value(), r = item.ready.value();
    keep(next, State{add(fc, f), add(fe, f), add(rc, r), add(re, r)}, std::move(b));
  }

  std::optional<SlotChoice> finish() const {
    std::optional<Best> best;
    for (const auto &[s, b] : states_) {
      auto end = b;
      const auto &[fc, fe, rc, re] = s;
      if (fits(end, fc, TimingLimit::ReceiverForward, true) &&
          fits(end, fe, TimingLimit::ThroughForward, true) &&
          fits(end, rc, TimingLimit::ReceiverReady, true) &&
          fits(end, re, TimingLimit::ThroughReady, true) && (!best || end.better(*best)))
        best = std::move(end);
    }
    if (!best)
      return std::nullopt;
    return best->choice;
  }

  const Chain &c_;
  Ps launch_ = 0, capture_ = 0, fpass_ = 0, rpass_ = 0;
  States states_;
};
} // namespace

std::optional<SlotChoice> plan_chain(const Chain &c) {
  if (c.unmodeled)
    return std::nullopt;
  return ChainPlanner(c).run();
}
} // namespace nocloom::detail

namespace nocloom::detail {
namespace {
// The definition link of a switch edge plan, with its index in its graph.
std::pair<std::vector<ExchangeLink> *, std::size_t> definition_link(Definition &d,
                                                                    const LinkPlan &link) {
  auto &links = link.response ? d.topology->response.links : d.topology->request.links;
  const auto at = std::ranges::find_if(links, [&](const ExchangeLink &l) {
    return l.from == link.from_name && l.to == link.to_name;
  });
  if (at == links.end())
    throw std::logic_error("No definition link for " + link.name);
  return {&links, static_cast<std::size_t>(at - links.begin())};
}
// Whether a link_timing entry names the configured link of a switch edge plan.
bool timed_by(const LinkPlan &link, const LinkDelays &delays) {
  return link.response == (delays.graph == LinkGraph::Response) && !link.from_name.empty() &&
         link.from_name == delays.from && link.to_name == delays.to;
}
// The hardware links of a link_timing entry in plan order: one with shared paths, the write link
// and then the read link where the configured link has separate read and write paths.
std::vector<const LinkPlan *> timed_links(const NetworkPlan &plan, const LinkDelays &delays) {
  std::vector<const LinkPlan *> out;
  for (const auto &link : plan.links)
    if (timed_by(link, delays))
      out.push_back(&link);
  return out;
}
// The packets a link carries: both directions, or the writes or the reads of separate paths.
Carried carried_by(const LinkPlan &link) {
  for (const auto carried : {Carried::Writes, Carried::Reads})
    if (link.name.starts_with(graph_scope(link.response, carried)))
      return carried;
  return !link.split ? Carried::Both : link.read_lane ? Carried::Reads : Carried::Writes;
}
const char *carried_name(Carried carried) {
  static constexpr std::array table{"both", "writes", "reads"};
  return table.at(static_cast<std::size_t>(carried));
}
// The stage lists of a link with the chosen pipes appended at their positions.
std::vector<LinkStages> with_added(const Chain &c, const LinkPlan &link,
                                   std::vector<LinkStages> lists, const SlotChoice &added) {
  for (unsigned i = 0; i < added.size(); ++i) {
    if (added[i] == PipeSlice::None)
      continue;
    const auto &p = c.positions[i];
    const auto side = p.receiver ? LinkSide::Receiver : LinkSide::Sender;
    const auto end = p.receiver ? link.to_bytes : link.from_bytes;
    const auto at = std::ranges::find_if(lists, [&](const LinkStages &l) {
      return l.side == side && l.word_bytes.value_or(end) == p.word;
    });
    if (at != lists.end())
      at->pipes.push_back(added_pipe(added[i]));
    else
      lists.push_back({side, p.word, std::nullopt, {added_pipe(added[i])}});
  }
  return lists;
}
Json pipe_json(const LinkPipeline &p) {
  Json row{{"slice", slice_name(p.slice)}};
  if (!p.bypass_port.empty())
    row["bypass_port"] = p.bypass_port;
  if (p.ungated)
    row["ungated"] = true;
  return row;
}
Json lists_json(const std::vector<LinkStages> &lists) {
  Json out = Json::array();
  for (const auto &l : lists) {
    Json row{{"side", side_name(l.side == LinkSide::Receiver)}, {"pipes", Json::array()}};
    if (l.word_bytes)
      row["word_bytes"] = *l.word_bytes;
    if (l.clock_domain)
      row["clock_domain"] = *l.clock_domain;
    for (const auto &p : l.pipes)
      row["pipes"].push_back(pipe_json(p));
    out.push_back(std::move(row));
  }
  return out;
}
// The definition field of a path's bound.
std::string bound_field(const Chain &c, const TimedPath &p, const std::string &field) {
  if (p.limit == TimingLimit::Period)
    return p.receiver ? c.receiver_period_field : c.sender_period_field;
  const std::string name = limit_name(p.limit);
  return field + ".allowances." + name + "_ps";
}
Json path_json(const TimedPath &p) {
  Json row{{"signal", signal_name(p.signal)},
           {"from", p.from},
           {"to", p.to},
           {"limit", limit_name(p.limit)}};
  if (!p.via.empty())
    row["via"] = p.via;
  row["delay_ps"] = p.delay ? Json(*p.delay) : Json(nullptr);
  row["bound_ps"] = p.bound ? Json(*p.bound) : Json(nullptr);
  row["slack_ps"] =
      p.delay && p.bound
          ? Json(static_cast<std::int64_t>(*p.bound) - static_cast<std::int64_t>(*p.delay))
          : Json(nullptr);
  return row;
}
// The configured name of a link end object and its kind.
std::pair<std::string, std::string> end_name(const NetworkPlan &plan, const std::string &object) {
  const auto slash = object.find('/');
  const auto kind = object.substr(0, slash), name = object.substr(slash + 1);
  if (kind == "source")
    return {name, "initiator"};
  if (kind == "target")
    return {name, "target"};
  return {plan.node(name).name, "switch"};
}
struct Flow {
  std::string initiator, target;
  std::vector<unsigned> initiator_flows;
  std::optional<unsigned> target_flow;
  auto operator<=>(const Flow &) const = default;
};
struct RoutePath {
  bool response = false;
  Carried carried = Carried::Both;
  std::vector<std::string> links;
  std::vector<Flow> flows;
};
// Every route's links, each distinct sequence once with the flows that take it. A graph of
// separate paths gives the paths of its direction. A route of shared graphs that passes a link
// with separate paths gives a write path and a read path, which takes the read links there.
std::vector<RoutePath> route_paths(const NetworkPlan &plan) {
  std::map<std::tuple<bool, Carried, std::vector<std::string>>, std::set<Flow>> grouped;
  const auto planned = [&](const std::string &name) -> const LinkPlan & {
    const auto at = std::ranges::find(plan.links, name, &LinkPlan::name);
    if (at == plan.links.end())
      throw std::logic_error("A route passes an unplanned link " + name);
    return *at;
  };
  for (const auto *graph : plan.exchange.graphs()) {
    const bool response = graph->graph.response;
    const std::string prefix = graph->scope();
    for (const auto &r : graph->routes) {
      std::vector<std::string> links{prefix + r.entry + ">" + r.nodes.front()};
      for (std::size_t i = 0; i + 1 < r.nodes.size(); ++i)
        links.push_back(prefix + r.nodes[i] + ">" + r.nodes[i + 1]);
      links.push_back(prefix + r.nodes.back() + ">" + r.exit);
      auto reads = links;
      bool split = false;
      for (auto &name : reads)
        if (planned(name).split) {
          split = true;
          name = planned(path_lane(name, true)).name;
        }
      const Flow flow{response ? r.exit : r.entry, response ? r.entry : r.exit, r.flows,
                      r.target_path};
      grouped[{response, split ? Carried::Writes : graph->graph.carried, links}].insert(flow);
      if (split)
        grouped[{response, Carried::Reads, reads}].insert(flow);
    }
  }
  std::vector<RoutePath> out;
  for (auto &[key, flows] : grouped)
    out.push_back(
        {std::get<0>(key), std::get<1>(key), std::get<2>(key), {flows.begin(), flows.end()}});
  return out;
}
// The forward registering pipes of a link by domain, with those a bypass can open.
std::map<std::string, unsigned> link_cycles(const LinkPlan &link, unsigned *bypassed = nullptr) {
  std::map<std::string, unsigned> out;
  for (const auto &s : link.stages)
    if (s.kind == StageKind::Pipe && s.pipe.forward_stage()) {
      ++out[s.domain];
      if (bypassed && !s.pipe.bypass_port.empty())
        ++*bypassed;
    }
  return out;
}
// One hardware link of a planned entry, with its chain and the paths the plan claims for it.
struct PlannedLane {
  const LinkPlan *link = nullptr;
  Chain chain;
  ChainResult result;
};
// A planned link_timing entry: the pipes added to its configured link and each hardware link
// that takes them (timed_links).
struct PlannedLink {
  std::size_t entry = 0;
  SlotChoice added;
  std::vector<PlannedLane> lanes;
  const LinkPlan &link() const { return *lanes.front().link; }
};
struct PlanOutcome {
  std::vector<PlannedLink> planned;
  std::vector<Diagnostic> problems;
  Definition applied;
  bool requested = false;
};
// The name of a path in a message: request or response, with the direction of separate paths.
std::string path_label(const RoutePath &path) {
  const auto graph = graph_name(path.response);
  return path.carried == Carried::Both     ? graph
         : path.carried == Carried::Writes ? "write " + graph
                                           : "read " + graph;
}
std::string link_label(const LinkDelays &d) {
  return "Link " + d.from + " -> " + d.to + " (" + graph_name(d.graph == LinkGraph::Response) + ")";
}
// Each hardware link of a planned entry gives the claimed paths in the planned definition.
void check_planned(const NetworkPlan &rebuilt, const Definition &applied, const LinkDelays &delays,
                   const PlannedLink &p) {
  const auto links = timed_links(rebuilt, delays);
  if (links.size() != p.lanes.size())
    throw std::logic_error("A planned link is missing after planning");
  const auto field = "link_timing.links[" + std::to_string(p.entry) + "]";
  for (std::size_t i = 0; i < links.size(); ++i) {
    const auto &claimed = p.lanes[i].result;
    const auto chain = link_chain(rebuilt, applied, *links[i], &delays, field);
    const auto result = evaluate_chain(chain, SlotChoice(chain.positions.size()));
    if (links[i]->name != p.lanes[i].link->name || !result.met || result.paths != claimed.paths ||
        result.cycles != claimed.cycles || result.slack != claimed.slack)
      throw std::logic_error("The planned stage lists of " + p.lanes[i].link->name +
                             " do not give the claimed paths");
  }
}
PlanOutcome plan_outcome(const NetworkPlan &plan, const Definition &definition) {
  PlanOutcome out;
  out.applied = definition;
  if (!definition.link_timing)
    return out;
  const auto &timing = *definition.link_timing;
  out.requested = !timing.links.empty() || !timing.latency_limits.empty();
  for (std::size_t k = 0; k < timing.links.size(); ++k) {
    const auto &delays = timing.links[k];
    const auto field = "link_timing.links[" + std::to_string(k) + "]";
    const auto lanes = timed_links(plan, delays);
    if (lanes.empty())
      throw std::logic_error("A checked link_timing entry names no switch link");
    // The lanes of one configured link have the same words, pipes, crossing and delays, so the
    // first one plans them all.
    const auto *link = lanes.front();
    auto chain = link_chain(plan, definition, *link, &delays, field);
    if (chain.unmodeled) {
      out.problems.push_back(error("NCL_STAGE_PLAN_UNKNOWN", field,
                                   link_label(delays) +
                                       " has a clock crossing other than an asynchronous FIFO, "
                                       "whose paths the planner does not model"));
      continue;
    }
    const auto missing = missing_delays(chain);
    for (const auto &at : missing)
      out.problems.push_back(error("NCL_STAGE_PLAN_UNKNOWN", at,
                                   link_label(delays) + " needs this estimate to be planned"));
    if (!missing.empty())
      continue;
    const auto choice = plan_chain(chain);
    if (!choice) {
      // Every placement fails: report the bounds that even a full pipe at each position misses.
      const auto most = evaluate_chain(chain, SlotChoice(chain.positions.size(), PipeSlice::Full));
      std::map<std::string, TimedPath> missed;
      for (const auto &p : most.paths) {
        if (p.bound && p.delay && *p.delay <= *p.bound)
          continue;
        const auto at = bound_field(chain, p, field);
        const auto over = [](const TimedPath &x) {
          return x.bound ? static_cast<std::int64_t>(*x.delay) - static_cast<std::int64_t>(*x.bound)
                         : std::numeric_limits<std::int64_t>::max();
        };
        if (!missed.contains(at) || over(p) > over(missed.at(at)))
          missed.insert_or_assign(at, p);
      }
      if (missed.empty())
        throw std::logic_error("No placement plans " + link->name + " but every bound holds");
      for (const auto &[at, p] : missed)
        out.problems.push_back(
            error("NCL_STAGE_PLAN_UNMET", at,
                  link_label(delays) + ": with a full pipe at every position, the " +
                      signal_name(p.signal) + " path from " + p.from + " to " + p.to +
                      (p.via.empty() ? "" : " through " + p.via) + " takes " +
                      std::to_string(*p.delay) + " ps" +
                      (p.bound ? ", over its " + std::string(limit_name(p.limit)) + " bound of " +
                                     std::to_string(*p.bound) + " ps"
                               : ", and the link has no " + std::string(limit_name(p.limit)) +
                                     " allowance")));
      continue;
    }
    PlannedLink planned{k, *choice, {}};
    for (const auto *lane : lanes) {
      auto lane_chain = link_chain(plan, definition, *lane, &delays, field);
      auto result = evaluate_chain(lane_chain, *choice);
      if (!result.met)
        throw std::logic_error("The plan of " + lane->name + " misses a bound");
      planned.lanes.push_back({lane, std::move(lane_chain), std::move(result)});
    }
    if (std::ranges::any_of(*choice, [](PipeSlice s) { return s != PipeSlice::None; })) {
      const auto [links, at] = definition_link(out.applied, *link);
      (*links)[at].stages = with_added(chain, *link, (*links)[at].stages, *choice);
    }
    out.planned.push_back(std::move(planned));
  }
  // Latency limits over every path with the planned pipes.
  std::map<std::string, unsigned> added;
  for (const auto &p : out.planned)
    for (const auto &lane : p.lanes)
      added[lane.link->name] = static_cast<unsigned>(std::ranges::count_if(
          p.added, [](PipeSlice s) { return added_pipe(s).forward_stage(); }));
  const auto paths = route_paths(plan);
  for (std::size_t i = 0; i < timing.latency_limits.size(); ++i) {
    const auto &limit = timing.latency_limits[i];
    for (bool response : {false, true}) {
      const auto bound = response ? limit.response_cycles : limit.request_cycles;
      if (!bound)
        continue;
      for (const auto &path : paths) {
        if (path.response != response || std::ranges::none_of(path.flows, [&](const Flow &f) {
              return f.initiator == limit.initiator && f.target == limit.target;
            }))
          continue;
        unsigned cycles = 0;
        std::string adding;
        for (const auto &name : path.links) {
          const auto &link = *std::ranges::find(plan.links, name, &LinkPlan::name);
          unsigned here = added.contains(name) ? added.at(name) : 0;
          for (const auto &[domain, n] : link_cycles(link))
            here += n;
          cycles += here;
          if (here)
            adding += (adding.empty() ? "" : ", ") + name + " " + std::to_string(here);
        }
        if (cycles > *bound)
          out.problems.push_back(error("NCL_STAGE_PLAN_UNMET",
                                       "link_timing.latency_limits[" + std::to_string(i) + "]." +
                                           (response ? "response_cycles" : "request_cycles"),
                                       "The " + path_label(path) + " path of " + limit.initiator +
                                           " and " + limit.target + " takes " +
                                           std::to_string(cycles) + " pipe cycles (" + adding +
                                           "), over its limit of " + std::to_string(*bound)));
      }
    }
  }
  // The planned definition must plan again and give the claimed paths on every planned link.
  if (std::ranges::any_of(out.planned, [](const PlannedLink &p) {
        return std::ranges::any_of(p.added, [](PipeSlice s) { return s != PipeSlice::None; });
      })) {
    auto again = make_plan(out.applied);
    if (!again)
      throw std::logic_error("The planned stage lists fail to plan: " +
                             again.diagnostics().front().message);
    auto rebuilt = build_network_plan(out.applied);
    if (!rebuilt)
      throw std::logic_error("The planned definition fails to build");
    for (const auto &p : out.planned)
      check_planned(rebuilt.value(), out.applied, timing.links[p.entry], p);
  }
  return out;
}
} // namespace

namespace {
std::string report_text(const NetworkPlan &plan, const Definition &definition) {
  const auto outcome = plan_outcome(plan, definition);
  Json report{{"format", "nocloom.path_report"}, {"format_version", 1}, {"module", plan.name}};
  const LinkTiming none{};
  const auto &timing = definition.link_timing ? *definition.link_timing : none;
  Json links = Json::array();
  for (const auto &link : plan.links) {
    const auto [from, from_kind] = end_name(plan, link.from);
    const auto [to, to_kind] = end_name(plan, link.to);
    Json row{{"link", link.name},
             {"graph", graph_name(link.response)},
             {"carries", carried_name(carried_by(link))},
             {"from", from},
             {"from_kind", from_kind},
             {"to", to},
             {"to_kind", to_kind},
             {"sender_domain", link.from_domain},
             {"receiver_domain", link.to_domain}};
    Json stages = Json::array();
    const auto profile = transport_burst(plan);
    for (const auto &s : link.stages) {
      Json st{{"name", s.name}, {"side", side_name(s.receiver)}, {"clock_domain", s.domain}};
      if (s.kind == StageKind::Crossing)
        st["kind"] = "crossing";
      else if (s.kind == StageKind::Adapter) {
        st["kind"] = s.serializer ? "serializer" : "deserializer";
        st["wide_bytes"] = s.wide_bytes;
        st["narrow_bytes"] = s.narrow_bytes;
      } else {
        std::uint64_t bits = 1;
        for (const auto &[name, width] :
             fragment_fields(link.response, s.wide_bytes, plan.source_bits, profile))
          bits += width;
        st["kind"] = "pipe";
        st["word_bytes"] = s.wide_bytes;
        st["slice"] = slice_name(s.pipe.slice);
        st["slots"] = s.pipe.slot_count();
        st["bits"] = bits * s.pipe.slot_count();
        if (!s.pipe.bypass_port.empty())
          st["bypass_port"] = s.pipe.bypass_port;
      }
      stages.push_back(std::move(st));
    }
    row["stages"] = std::move(stages);
    unsigned bypassed = 0;
    Json cycles = Json::object();
    for (const auto &[domain, n] : link_cycles(link, &bypassed))
      cycles[domain] = n;
    row["pipe_cycles"] = std::move(cycles);
    row["bypass_cycles"] = bypassed;
    for (std::size_t k = 0; k < timing.links.size(); ++k) {
      if (!timed_by(link, timing.links[k]))
        continue;
      const auto field = "link_timing.links[" + std::to_string(k) + "]";
      const auto chain = link_chain(plan, definition, link, &timing.links[k], field);
      const auto result = evaluate_chain(chain, SlotChoice(chain.positions.size()));
      Json t{{"delays", field}};
      if (chain.unmodeled) {
        t["status"] = "unknown";
        t["unmodeled_crossing"] = true;
      } else {
        const bool unknown = std::ranges::any_of(result.paths, [](const TimedPath &p) {
          return !p.delay || (!p.bound && p.limit == TimingLimit::Period);
        });
        t["status"] = result.met ? "met" : unknown ? "unknown" : "unmet";
        t["missing"] = missing_delays(chain);
        t["paths"] = Json::array();
        for (const auto &p : result.paths)
          t["paths"].push_back(path_json(p));
      }
      row["timing"] = std::move(t);
    }
    links.push_back(std::move(row));
  }
  report["links"] = std::move(links);
  Json paths = Json::array();
  for (const auto &path : route_paths(plan)) {
    Json row{{"graph", graph_name(path.response)},
             {"carries", carried_name(path.carried)},
             {"links", path.links}};
    Json flows = Json::array();
    for (const auto &f : path.flows) {
      Json flow{{"initiator", f.initiator}, {"target", f.target}};
      if (!f.initiator_flows.empty())
        flow["initiator_flows"] = f.initiator_flows;
      if (f.target_flow)
        flow["target_flow"] = *f.target_flow;
      flows.push_back(std::move(flow));
    }
    row["flows"] = std::move(flows);
    std::map<std::string, unsigned> cycles;
    unsigned bypassed = 0, crossings = 0;
    for (const auto &name : path.links) {
      const auto &link = *std::ranges::find(plan.links, name, &LinkPlan::name);
      for (const auto &[domain, n] : link_cycles(link, &bypassed))
        cycles[domain] += n;
      crossings += static_cast<unsigned>(std::ranges::count_if(
          link.stages, [](const StagePlan &s) { return s.kind == StageKind::Crossing; }));
    }
    row["pipe_cycles"] = cycles;
    row["bypass_cycles"] = bypassed;
    row["crossings"] = crossings;
    paths.push_back(std::move(row));
  }
  report["paths"] = std::move(paths);
  Json proposal{{"status", !outcome.requested         ? "none"
                           : outcome.problems.empty() ? "met"
                                                      : "unmet"},
                {"links", Json::array()},
                {"unmet", Json::array()}};
  for (const auto &p : outcome.planned) {
    if (std::ranges::all_of(p.added, [](PipeSlice s) { return s == PipeSlice::None; }))
      continue;
    auto copy = outcome.applied;
    const auto [list, at] = definition_link(copy, p.link());
    Json row{{"graph", graph_name(p.link().response)},
             {"from", p.link().from_name},
             {"to", p.link().to_name},
             {"stages", lists_json((*list)[at].stages)},
             {"added", Json::array()}};
    // Each hardware link of the configured link takes the added pipes.
    std::uint64_t bits = 0;
    for (const auto &lane : p.lanes) {
      for (unsigned i = 0; i < p.added.size(); ++i)
        if (p.added[i] != PipeSlice::None)
          row["added"].push_back(
              {{"name", added_name(lane.chain, i)}, {"slice", slice_name(p.added[i])}});
      bits += lane.result.added_bits;
    }
    const auto &first = p.lanes.front().result;
    row["pipe_cycles_added"] = static_cast<unsigned>(
        std::ranges::count_if(p.added, [](PipeSlice s) { return added_pipe(s).forward_stage(); }));
    row["bits_added"] = bits;
    row["slack_ps"] = first.slack ? Json(*first.slack) : Json(nullptr);
    proposal["links"].push_back(std::move(row));
  }
  for (const auto &d : outcome.problems)
    proposal["unmet"].push_back({{"code", d.code}, {"field", d.field}, {"message", d.message}});
  report["plan"] = std::move(proposal);
  return report.dump(2) + "\n";
}

} // namespace

Result<std::string> path_report(const NetworkPlan &plan, const Definition &definition) {
  try {
    return Result<std::string>::success(report_text(plan, definition));
  } catch (const std::logic_error &e) {
    return Result<std::string>::failure(
        error("NCL_INTERNAL_PLAN", "link_timing", std::string("Path report fault: ") + e.what()));
  }
}

Result<Definition> link_stage_plan(const NetworkPlan &plan, const Definition &definition) {
  try {
    auto outcome = plan_outcome(plan, definition);
    if (!outcome.problems.empty())
      return Result<Definition>::failure(std::move(outcome.problems));
    return Result<Definition>::success(std::move(outcome.applied));
  } catch (const std::logic_error &e) {
    return Result<Definition>::failure(
        error("NCL_INTERNAL_PLAN", "link_timing", std::string("Stage plan fault: ") + e.what()));
  }
}
} // namespace nocloom::detail
