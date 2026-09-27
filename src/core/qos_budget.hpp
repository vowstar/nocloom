#pragma once
#include "qos_pipeline.hpp"
#include "treadle.hpp"
#include "upgrade_ledger.hpp"
#include <algorithm>
#include <bit>
#include <iterator>
#include <map>
#include <nocloom/definition.hpp>
#include <utility>
#include <vector>

namespace nocloom::detail {
// A budget counter: 17 bits of whole bytes above a fraction of a byte. The fraction has 10 bits
// for a rate in whole bytes per 1024 cycles, and 15 bits for finer rates.
struct BudgetScale {
  unsigned byte_at = 10, bits = 27; // the first bit of the whole bytes, the counter width
  std::uint64_t rate = 0;           // units of the fraction drained per cycle
};
inline BudgetScale budget_scale(const QosShaper &shaper) {
  const auto rate =
      shaper.bytes_per_32768_cycles.value_or(32 * shaper.bytes_per_1024_cycles.value_or(0));
  if (rate % 32 == 0)
    return {10, 27, rate / 32};
  return {15, 32, rate};
}
// A counter takes a charge only while its whole bytes do not exceed burst_bytes, 65535 at most,
// and a charge holds at most 256 AXI transfers of 128 bytes: the sum stays in the 17 bits.
static_assert(65535 + 1 + 256 * 128 <= 1 << 17);
// A charge travels as its bytes less one: a legal AXI burst has at most 4096 bytes, so a
// scheduler keeps it in budget_charge_bits, and a larger count is kept as the largest value.
inline constexpr unsigned budget_charge_bits = 12;
// The numbers a scheduling budget compares against, from QosShaper and QosScheduler.
struct BudgetLevels {
  BudgetScale scale;
  unsigned bytes = 0;     // whole bytes of the burst
  unsigned threshold = 0; // whole bytes up to which the level is under
  unsigned under = 0, over = 0;
};
inline BudgetLevels budget_levels(const QosShaper &shaper, const QosScheduler &scheduler) {
  return {budget_scale(shaper), shaper.burst_bytes,
          shaper.burst_bytes * scheduler.threshold_percent / 100, scheduler.under_level,
          scheduler.over_level};
}
// A source's request shaper (QosShaper alone) on one packet channel. The budget counter is
// charged as a packet head enters the network (last_byte is the bytes less one, a constant or
// the head's own), and every cycle drains the rate, stopping at zero. A packet may
// start while the counter's whole bytes do not exceed burst_bytes; once its head is offered it
// stays offered, and its later fragments pass, up to its tail.
struct Shaper {
  ValueId valid, ready;          // the output valid, the input ready
  ValueId counter, inside, held; // control: the budget, inside a packet, a head offered
  ValueId charge;                // a head entered the network this cycle
};
inline Shaper lower_shaper(Builder &b, ValueId clock, ValueId reset, ValueId valid, ValueId last,
                           ValueId ready, const QosShaper &shaper, ValueId last_byte) {
  const auto scale = budget_scale(shaper);
  const unsigned bits = scale.bits;
  const auto both = [&](ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); };
  const auto inv = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  Shaper r{};
  r.counter = b.state(bits, clock, reset);
  r.inside = b.state(1, clock, reset);
  r.held = b.state(1, clock, reset);
  const auto open =
      b.binary(Op::UnsignedLess, r.counter,
               b.constant(bits, (std::uint64_t{shaper.burst_bytes} + 1) << scale.byte_at));
  const auto pass = b.binary(Op::BitOr, r.held, open);
  r.valid = both(valid, pass);
  r.ready = both(ready, pass);
  const auto taken = both(r.valid, ready);
  r.charge = both(taken, inv(r.inside));
  // counter + charge - rate, at zero when negative. The charge is (last_byte + 1) whole bytes:
  // the whole bytes of the counter less the complement of last_byte, one subtraction. The rate
  // then leaves either sum, whose one bit more is the sign.
  const unsigned high = bits + 1 - scale.byte_at;
  const auto wide = b.unary(Op::ZeroExtend, r.counter, bits + 1);
  const auto fixed = b.ir.values.at(last_byte.index);
  ValueId minus;
  if (fixed.op == Op::Constant)
    minus = b.constant(high, (std::uint64_t{1} << high) - (fixed.literal + 1));
  else
    minus = b.unary(Op::BitNot,
                    fixed.width > high    ? b.slice(last_byte, 0, high)
                    : fixed.width == high ? last_byte
                                          : b.unary(Op::ZeroExtend, last_byte, high),
                    high);
  const auto charged = b.concat(b.binary(Op::Subtract, b.slice(wide, scale.byte_at, high), minus),
                                b.slice(wide, 0, scale.byte_at));
  const auto rate = b.constant(bits + 1, scale.rate);
  const auto drained = [&](ValueId x) {
    const auto left = b.binary(Op::Subtract, x, rate);
    return b.select(b.slice(left, bits, 1), b.constant(bits, 0), b.slice(left, 0, bits));
  };
  b.update(r.counter, b.select(r.charge, drained(charged), drained(wide)));
  b.update(r.inside, b.select(taken, inv(last), r.inside));
  b.update(r.held, b.select(r.valid, inv(both(last, ready)), r.held));
  return r;
}

// A completion-charged scheduling budget with a path upgrade ledger.
// Request head levels remain frozen from first offer through acceptance.
struct Scheduler {
  ValueId valid{}, ready{};      // the output valid, the input ready
  ValueId raise{};               // the output offers a raise packet
  ValueId level{};               // arbitration level of the output, zero while it is invalid
  ValueId head_level{};          // the level of the packet offered: its request's, or the raise's
  ValueId route{};               // the address of the transfer in flight (of the raise packet)
  std::vector<ValueId> words{};  // control words, the counter first
  std::vector<ValueId> routes{}; // payload words: the routes of the contexts and the raise slot
  ValueId charged{};             // the counter takes a charge this cycle
  ValueId idle{};                // nothing to count, no transfer in flight, no raise packet due
  ValueId pressure{};            // thermometer pressure when a source pipe separates the stages
  ValueId alloc_start{};         // a head allocated its context before the issue pipe
  std::vector<ValueId> allocation{}; // one-hot context carried through that pipe
  std::map<std::string, ValueId> bindings;
  ValueId conflict{}; // a context holds the head's key on another path
};
// The scheduler counter (lower_scheduler): X = counter - rate (+ charge when charged), zero
// when negative, saturated at burst_bytes. `charge` holds the bytes of the finished transfer
// less one. The rate and that one byte are constants: X is one subtraction from the counter on
// either side, and a rate from one byte per cycle up, or of zero, first corrects the charge.
inline void count_scheduler(Builder &b, ValueId counter, ValueId charged, ValueId charge,
                            const BudgetLevels &scheduler) {
  const unsigned bits = scheduler.scale.bits, at = scheduler.scale.byte_at;
  const auto burst_bytes = scheduler.bytes;
  const std::uint64_t rate = scheduler.scale.rate, part = rate & ((std::uint64_t{1} << at) - 1);
  // counter + ((charge + 1) << at) - rate = counter + ((charge - whole) << at) + below.
  const std::uint64_t whole = (rate >> at) - (part ? 0 : 1), below = part ? (1ULL << at) - part : 0;
  const unsigned high = bits + 1 - at;
  auto bytes = b.unary(Op::ZeroExtend, charge, high);
  if (whole)
    bytes = b.binary(Op::Subtract, bytes, b.constant(high, whole & ((1ULL << high) - 1)));
  const auto wide = b.unary(Op::ZeroExtend, counter, bits + 1);
  // wide + owed with one subtraction: ~(~wide - owed).
  const auto owed = b.concat(bytes, b.constant(at, below));
  const auto paid = b.unary(
      Op::BitNot, b.binary(Op::Subtract, b.unary(Op::BitNot, wide, bits + 1), owed), bits + 1);
  const auto sum =
      b.select(charged, paid, b.binary(Op::Subtract, wide, b.constant(bits + 1, rate)));
  const auto over =
      b.binary(Op::UnsignedLess, b.constant(bits - at, burst_bytes), b.slice(sum, at, bits - at));
  b.update(counter, b.select(b.slice(sum, bits, 1), b.constant(bits, 0),
                             b.select(over, b.constant(bits, std::uint64_t{burst_bytes} << at),
                                      b.slice(sum, 0, bits))));
}

struct SchedulerFront {
  ValueId presented{}, taken{};
  ValueId counter{}, charged{}, charge{}, retained{}, held{};
  ValueId now{}, request_level{};
  std::vector<ValueId> used{};
  std::optional<ValueId> conflict{}; // the merger's late ID binding check
  std::optional<ValueId> answered{}; // a burst's last response, with SourcePlan::request_limit
  unsigned request_limit = 0;
};

inline SchedulerFront lower_scheduler_front(Builder &b, ValueId clock, ValueId reset,
                                            ValueId presented, ValueId taken,
                                            const BudgetLevels &scheduler, unsigned count) {
  constexpr unsigned level_bits = 3;
  const unsigned bits = scheduler.scale.bits, at = scheduler.scale.byte_at;
  const auto inv = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  const auto less = [&](ValueId x, ValueId y) { return b.binary(Op::UnsignedLess, x, y); };
  SchedulerFront f{presented,
                   taken,
                   b.state(bits, clock, reset),
                   b.state(1, clock, reset),
                   b.state(budget_charge_bits, clock, reset),
                   b.state(1, clock, reset),
                   b.state(level_bits, clock, reset)};
  for (unsigned i = 0; i < count; ++i)
    f.used.push_back(b.state(1, clock, reset));
  const auto within =
      inv(less(b.constant(bits - at, scheduler.threshold), b.slice(f.counter, at, bits - at)));
  f.now = b.select(within, b.constant(level_bits, scheduler.under),
                   b.constant(level_bits, scheduler.over));
  f.request_level = b.select(f.retained, f.held, f.now);
  b.update(f.held, f.request_level);
  b.update(f.retained, b.select(f.presented, inv(f.taken), f.retained));
  return f;
}

// A reservation is either empty or an encoded context index, consumed by allocation.
inline std::vector<ValueId> offer_free_context(Builder &b, const std::vector<ValueId> &used,
                                               ValueId reservation, ValueId cursor, ValueId start) {
  const auto width = b.ir.values.at(cursor.index).width;
  const auto active = b.slice(reservation, 0, 1);
  const auto not_ = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  std::vector<ValueId> candidates, pointer;
  std::ranges::transform(used, std::back_inserter(candidates), not_);
  const auto [found, index] =
      treadle::rotating_choice(b, candidates, cursor, 0, static_cast<unsigned>(used.size()));
  const auto load = b.binary(Op::BitAnd, not_(active), found);
  b.update(reservation, b.select(start, b.constant(width + 1, 0),
                                 b.select(load, b.concat(index, b.constant(1, 1)), reservation)));
  b.update(cursor,
           b.select(load, b.binary(Op::Subtract, cursor, b.constant(width, (1ULL << width) - 1)),
                    cursor));
  const auto selected = b.select(active, b.slice(reservation, 1, width), index);
  const auto available = b.binary(Op::BitOr, active, found);
  for (unsigned i = 0; i < used.size(); ++i)
    pointer.push_back(
        b.binary(Op::BitAnd, available, b.binary(Op::Equal, selected, b.constant(width, i))));
  return pointer;
}

// Reasons that prevent the request head from entering the network.
struct SchedulerWait {
  ValueId head_waits{},
      lacks_unit{};          // a head waits for its ID or a table entry; a request for a unit
  std::optional<ValueId> id; // the request's AXI ID (the waiting head's while it waits)
};
struct SchedulerCompletion {
  ValueId valid, tag;
};
struct SchedulerPipe {
  const SchedulerFront *front{};
  ValueId head_level{}, source_level{}, level_hint{}, network_level_hint{};
  ValueId alloc_valid{}, alloc_first{}, alloc_ready{};
  ValueId alloc_route{}, alloc_write{}, alloc_tag{}, alloc_charge{};
  std::vector<ValueId> offered_context{};
  bool issue = false, delayed = false;
};
inline void lower_scheduler_pressure(Builder &b, ValueId clock, ValueId reset, Scheduler &r,
                                     ValueId offered, ValueId raise_level, ValueId pressed,
                                     ValueId head, ValueId head_level, const SchedulerPipe &pipe,
                                     ValueId level_hint_in) {
  const PressurePath path{b, b.ir.values.at(pipe.network_level_hint.index).width};
  const auto on = b.constant(1, 1);
  auto passed = path.join({{path.encode(pipe.source_level), on}, {level_hint_in, on}});
  if (pipe.delayed) {
    const auto delay = b.state(path.bits, clock, reset);
    b.update(delay, passed);
    passed = delay;
    r.words.push_back(delay);
  }
  r.pressure = path.join({{pipe.network_level_hint, on},
                          {passed, on},
                          {path.encode(raise_level), offered},
                          {path.encode(pressed), on},
                          {path.encode(head_level), head}});
  r.level = b.select(r.valid, path.rank(r.pressure), b.constant(3, 0));
}
// The registers that take the pressure and the wait terms across the issue pipe stage.
struct SchedulerLevels {
  ValueId now{}, request_level{}, pressing{}, level_hint_in{};
  std::optional<ValueId> event;
};
inline SchedulerLevels lower_scheduler_levels(Builder &b, ValueId clock, ValueId reset,
                                              const SchedulerPipe &pipe, unsigned path_bits,
                                              Scheduler &r) {
  const auto width = b.ir.values.at(pipe.level_hint.index).width;
  SchedulerLevels u{pipe.source_level, pipe.head_level, {}, pipe.level_hint, std::nullopt};
  if (pipe.issue) {
    u.event = b.state(2 + path_bits, clock, reset);
    u.level_hint_in = b.state(width, clock, reset);
    b.update(u.level_hint_in, pipe.level_hint);
    r.words.insert(r.words.end(), {u.level_hint_in, *u.event});
  }
  u.pressing = PressurePath{b, width}.rank(u.level_hint_in);
  return u;
}
// Contexts keep billing identity and route until completion. Only launched heads may upgrade.
struct SchedulerState {
  ValueId counter, charged, charge, retained, held, free_cursor, reservation, head_waited,
      lacked_unit, event;
  std::optional<ValueId> waited_path;
  std::vector<ValueId> used, writes, tags, routes, charges, ids;
};
inline SchedulerState lower_scheduler_state(Builder &b, ValueId clock, ValueId reset,
                                            const SchedulerPipe *pipe, unsigned path_bits,
                                            unsigned bits, unsigned count, bool with_ids,
                                            unsigned id_bits, unsigned route_bits,
                                            unsigned tag_bits, Scheduler &r) {
  const auto state = [&](unsigned width) { return b.state(width, clock, reset); };
  SchedulerState s;
  if (pipe) {
    s.counter = pipe->front->counter;
    s.charged = pipe->front->charged;
    s.charge = pipe->front->charge;
    s.retained = pipe->front->retained;
    s.held = pipe->front->held;
    s.used = pipe->front->used;
  } else {
    s.counter = state(bits);
    s.charged = state(1);
    s.charge = state(budget_charge_bits);
    s.retained = state(1);
    s.held = state(3);
  }
  s.free_cursor = state(std::max(1u, static_cast<unsigned>(std::bit_width(count - 1))));
  s.reservation = state(b.ir.values.at(s.free_cursor.index).width + 1);
  s.event = state(2 + path_bits);
  s.head_waited = b.slice(s.event, 0, 1);
  s.lacked_unit = b.slice(s.event, 1, 1);
  if (path_bits)
    s.waited_path = b.slice(s.event, 2, path_bits);
  r.words = {s.counter, s.charged,     s.retained,    s.held,
             s.charge,  s.free_cursor, s.reservation, s.event};
  for (unsigned i = 0; i < count; ++i) {
    if (!pipe)
      s.used.push_back(state(1));
    s.writes.push_back(state(1));
    s.tags.push_back(state(tag_bits));
    s.routes.push_back(state(route_bits));
    s.charges.push_back(state(budget_charge_bits));
    r.words.insert(r.words.end(), {s.used[i], s.writes[i], s.tags[i], s.charges[i]});
    if (with_ids) {
      s.ids.push_back(state(id_bits));
      r.words.push_back(s.ids.back());
    }
  }
  r.routes = s.routes;
  r.charged = s.charged;
  return s;
}
inline Scheduler lower_scheduler_contexts(Builder &b, ValueId clock, ValueId reset, ValueId valid,
                                          ValueId last, ValueId route, unsigned path_bits,
                                          ValueId write, ValueId tag, const SchedulerWait &wait,
                                          ValueId ready, SchedulerCompletion read_done,
                                          SchedulerCompletion write_done,
                                          const BudgetLevels &scheduler, ValueId bytes,
                                          unsigned count, const SchedulerPipe *pipe = nullptr,
                                          std::optional<ValueId> lock = std::nullopt) {
  constexpr unsigned level_bits = 3;
  const unsigned bits = scheduler.scale.bits, at = scheduler.scale.byte_at;
  const auto both = [&](ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); };
  const auto either = [&](ValueId x, ValueId y) { return b.binary(Op::BitOr, x, y); };
  const auto inv = [&](ValueId x) { return b.unary(Op::BitNot, x, 1); };
  const auto less = [&](ValueId x, ValueId y) { return b.binary(Op::UnsignedLess, x, y); };
  const auto eq = [&](ValueId x, ValueId y) { return b.binary(Op::Equal, x, y); };
  const auto higher_level = [&](ValueId x, ValueId y) { return b.select(less(x, y), y, x); };
  const auto level = [&](unsigned v) { return b.constant(level_bits, v); };
  const auto width = [&](ValueId x) { return b.ir.values.at(x.index).width; };
  const auto any = [&](const std::vector<ValueId> &v) {
    auto x = v[0];
    for (std::size_t k = 1; k < v.size(); ++k)
      x = either(x, v[k]);
    return x;
  };
  const auto route_bits = width(route), tag_bits = width(tag);
  Scheduler r{};
  const auto *issue = pipe && pipe->issue ? pipe : nullptr;
  const auto [counter, charged, charge, retained, held, free_cursor, reservation, head_waited,
              lacked_unit, wait_event, waited_path, used, writes, tags, routes, charges, ids] =
      lower_scheduler_state(b, clock, reset, pipe, path_bits, bits, count, wait.id.has_value(),
                            wait.id ? width(*wait.id) : 0, route_bits, tag_bits, r);
  // The counter (lower_scheduler), charged the finished transfer's bytes a cycle later.
  count_scheduler(b, counter, charged, charge, scheduler);
  // Match both directions before the completion selects one. A read keeps
  // priority when both directions finish in the same cycle.
  const auto completing = [&](ValueId read_match, ValueId write_match) {
    return b.select(read_done.valid, read_match, both(write_done.valid, write_match));
  };
  std::vector<ValueId> finished, read_matches, write_matches;
  auto finished_charge = b.constant(budget_charge_bits, 0);
  for (unsigned i = 0; i < count; ++i) {
    read_matches.push_back(both(both(used[i], inv(writes[i])), eq(tags[i], read_done.tag)));
    write_matches.push_back(both(both(used[i], writes[i]), eq(tags[i], write_done.tag)));
    finished.push_back(completing(read_matches.back(), write_matches.back()));
    finished_charge = b.select(finished[i], charges[i], finished_charge);
  }
  b.update(charged, completing(any(read_matches), any(write_matches)));
  b.update(charge, finished_charge);
  const auto waiting = either(wait.head_waits, wait.lacks_unit);
  ValueId now, request_level, pressing, level_hint_in{};
  std::optional<SchedulerLevels> piped_levels;
  if (pipe) {
    piped_levels = lower_scheduler_levels(b, clock, reset, *pipe, path_bits, r);
    now = piped_levels->now;
    request_level = piped_levels->request_level;
    pressing = piped_levels->pressing;
    level_hint_in = piped_levels->level_hint_in;
  } else {
    const auto within =
        inv(less(b.constant(bits - at, scheduler.threshold), b.slice(counter, at, bits - at)));
    now = b.select(within, level(scheduler.under), level(scheduler.over));
    request_level = b.select(retained, held, now);
    b.update(held, request_level);
    pressing = b.select(waiting, higher_level(now, request_level), now);
  }
  const auto upgrades = lower_upgrade_ledger(b, clock, reset, valid, last, route, ready, path_bits,
                                             used, routes, finished, lock);
  const auto inside = upgrades.inside, offered = upgrades.valid, raise_level = upgrades.level;
  r.raise = upgrades.raise;
  r.valid = either(r.raise, valid);
  r.ready = both(ready, inv(r.raise));
  const auto taken = upgrades.taken;
  if (!pipe)
    b.update(retained, b.select(either(valid, waiting), inv(taken), retained));
  const auto head = both(both(valid, inv(inside)), inv(r.raise));
  const auto start = both(head, ready);
  const auto alloc_start =
      issue ? both(both(issue->alloc_valid, issue->alloc_first), issue->alloc_ready) : start;
  r.alloc_start = alloc_start;
  // The free context offered to the next head: a search result kept until a head takes it.
  const auto pointer = offer_free_context(b, used, reservation, free_cursor, alloc_start);
  r.allocation = pointer;
  // The raise slot's path, compared with each context's.
  const auto path = [&](ValueId x) { return b.slice(x, route_bits - path_bits, path_bits); };
  auto pressed = level(0);
  auto bound_path = path_bits ? b.constant(path_bits, 0) : ValueId{};
  const bool piped = pipe != nullptr;
  auto conflict = piped ? b.constant(1, 0) : ValueId{};
  const auto head_route = issue ? issue->alloc_route : route;
  for (unsigned i = 0; i < count; ++i) {
    const auto taken_here = both(alloc_start, pointer[i]);
    const auto launched = issue ? both(start, issue->offered_context[i]) : taken_here;
    b.update(used[i], both(either(used[i], taken_here), inv(finished[i])));
    b.update(writes[i], b.select(taken_here, issue ? issue->alloc_write : write, writes[i]));
    b.update(tags[i], b.select(taken_here, issue ? issue->alloc_tag : tag, tags[i]));
    b.update(routes[i], b.select(taken_here, issue ? issue->alloc_route : route, routes[i]));
    b.update(charges[i], b.select(taken_here, issue ? issue->alloc_charge : bytes, charges[i]));
    // The path of the waiting request's ID: that of a context in flight with its key.
    auto key = both(used[i], eq(writes[i], issue ? issue->alloc_write : write));
    if (wait.id) {
      b.update(ids[i], b.select(taken_here, *wait.id, ids[i]));
      key = both(key, eq(ids[i], *wait.id));
    }
    if (path_bits) {
      bound_path = b.select(key, path(routes[i]), bound_path);
      if (piped)
        conflict = either(conflict, both(key, inv(eq(path(routes[i]), path(head_route)))));
    }
    const auto on_path = path_bits ? eq(path(routes[i]), *waited_path) : b.constant(1, 1);
    const auto pressure = either(lacked_unit, both(head_waited, on_path));
    const auto required = higher_level(now, b.select(pressure, pressing, level(0)));
    update_upgrade_context(b, upgrades, i, used[i], taken_here, launched, finished[i],
                           request_level, required);
    pressed = higher_level(pressed, b.select(upgrades.dirty[i], upgrades.wanted[i], level(0)));
  }
  r.conflict = conflict;
  auto event = b.concat(wait.lacks_unit, wait.head_waits);
  if (path_bits)
    event = b.concat(bound_path, event);
  if (issue) {
    b.update(*piped_levels->event, event);
    event = *piped_levels->event;
  }
  b.update(wait_event, event);
  r.bindings = {{"counter", counter},
                {"charged", charged},
                {"keep", retained},
                {"held", held},
                {"control_valid", upgrades.valid},
                {"control_level", upgrades.level},
                {"control_sent", upgrades.sent},
                {"cursor", upgrades.cursor},
                {"data_held", upgrades.data_held}};
  for (unsigned i = 0; i < count; ++i) {
    r.bindings.emplace("wanted" + std::to_string(i), upgrades.wanted[i]);
    r.bindings.emplace("sent" + std::to_string(i), upgrades.acknowledged[i]);
    r.bindings.emplace("head_sent" + std::to_string(i), upgrades.head_sent[i]);
    r.bindings.emplace("participant" + std::to_string(i), upgrades.participants[i]);
  }
  r.route = upgrades.route;
  r.routes.push_back(upgrades.route);
  r.words.insert(r.words.end(), upgrades.words.begin(), upgrades.words.end());
  r.head_level = b.select(r.raise, raise_level, request_level);
  if (pipe) {
    lower_scheduler_pressure(b, clock, reset, r, offered, raise_level, pressed,
                             both(valid, inv(inside)), request_level, *pipe, level_hint_in);
  } else {
    const PressurePath demand_path{b, 7};
    const auto on = b.constant(1, 1);
    const auto demand =
        demand_path.join({{demand_path.encode(pressing), on},
                          {demand_path.encode(request_level), both(valid, inv(inside))},
                          {demand_path.encode(raise_level), offered},
                          {demand_path.encode(pressed), on}});
    r.level = b.select(r.valid, demand_path.rank(demand), level(0));
  }
  r.idle = both(eq(counter, b.constant(bits, 0)),
                inv(either(either(charged, any(used)),
                           either(upgrades.data_held, either(upgrades.present, offered)))));
  return r;
}
inline Scheduler lower_scheduler(Builder &b, ValueId clock, ValueId reset, ValueId valid,
                                 ValueId last, ValueId address, ValueId ready, ValueId done,
                                 const BudgetLevels &scheduler, ValueId bytes,
                                 std::optional<ValueId> lock = std::nullopt) {
  const auto zero = b.constant(1, 0);
  return lower_scheduler_contexts(b, clock, reset, valid, last, address, 0, zero, zero,
                                  {zero, zero, std::nullopt}, ready, {done, zero}, {zero, zero},
                                  scheduler, bytes, 1, nullptr, lock);
}
} // namespace nocloom::detail
