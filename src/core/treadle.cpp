#include "treadle.hpp"
#include <algorithm>
#include <bit>
#include <numeric>
#include <span>
#include <stdexcept>
#include <tuple>

namespace nocloom::detail::treadle {
ValueId join(Builder &b, std::span<const ValueId> terms) {
  if (terms.size() == 1)
    return terms[0];
  const auto half = terms.size() / 2;
  return b.binary(Op::BitOr, join(b, terms.first(half)), join(b, terms.subspan(half)));
}

std::pair<ValueId, ValueId> rotating_choice(Builder &b, std::span<const ValueId> requests,
                                            ValueId cursor, unsigned first, unsigned size) {
  if (size == 1)
    return {requests[first], b.constant(b.ir.values.at(cursor.index).width, first)};
  const unsigned low = size / 2;
  const auto [low_found, low_choice] = rotating_choice(b, requests, cursor, first, low);
  const auto [high_found, high_choice] =
      rotating_choice(b, requests, cursor, first + low, size - low);
  const auto upper = b.slice(cursor, static_cast<unsigned>(std::bit_width(size - 1)) - 1, 1);
  const auto choice = b.select(upper, b.select(high_found, high_choice, low_choice),
                               b.select(low_found, low_choice, high_choice));
  return {b.binary(Op::BitOr, low_found, high_found), choice};
}
namespace {
// Balanced valid maximum. Invalid candidates contribute zero, the lowest legal level.
ValueId maximum_level(Builder &b, std::span<const Candidate> in) {
  const auto width = b.ir.values.at(in[0].level->index).width;
  if (in.size() == 1)
    return b.select(in[0].valid, *in[0].level, b.constant(width, 0));
  const auto half = in.size() / 2;
  const auto left = maximum_level(b, in.first(half));
  const auto right = maximum_level(b, in.subspan(half));
  return b.select(b.binary(Op::UnsignedLess, left, right), right, left);
}
std::vector<ValueId> priority_requests(Builder &b, const std::vector<Candidate> &in) {
  const auto width = b.ir.values.at(in[0].level->index).width;
  if (std::ranges::any_of(in,
                          [&](auto &c) { return b.ir.values.at(c.level->index).width != width; }))
    throw std::logic_error("Priority levels must have one common width");
  const auto top = maximum_level(b, in);
  std::vector<ValueId> requests;
  std::ranges::transform(in, std::back_inserter(requests), [&](const Candidate &candidate) {
    return b.binary(Op::BitAnd, candidate.valid, b.binary(Op::Equal, *candidate.level, top));
  });
  return requests;
}

class Lowering {
public:
  Lowering(Builder &builder, ValueId clock, ValueId reset, const Spec &spec,
           const std::vector<Candidate> &candidates)
      : b(builder), clock(clock), reset(reset), spec(spec), in(candidates),
        count(static_cast<unsigned>(candidates.size())),
        bits(std::max(1U, static_cast<unsigned>(std::bit_width(count - 1)))) {}

  Arbiter run(const Datapath &datapath) {
    check();
    Arbiter a;
    if (in.front().level)
      a.eligible = eligible = priority_requests(b, in);
    if (spec.policy == Policy::RoundRobin || spec.policy == Policy::RotatingPriority) {
      cursor = b.state(bits, clock, reset);
      a.registers.push_back(*cursor);
    }
    if (spec.policy == Policy::LevelRoundRobin)
      for (unsigned k = 0; k < spec.levels; ++k)
        a.registers.push_back(lasts.emplace_back(b.state(bits, clock, reset)));
    a.owner = b.state(bits, clock, reset);
    a.held = b.state(1, clock, reset);
    a.registers.insert(a.registers.end(), {a.owner, a.held});
    if (cursor)
      a.state.emplace_back(".cursor", *cursor);
    for (unsigned k = 0; k < lasts.size(); ++k)
      a.state.emplace_back(".level_winner" + std::to_string(k), lasts[k]);
    if (!lasts.empty())
      level_cursor();
    if (spec.policy == Policy::Fifo)
      fifo_state(a);
    const auto [choice, grant] = choose();
    a.winner = b.select(a.held, a.owner, choice);
    output(a, datapath, grant);
    return a;
  }

private:
  Builder &b;
  ValueId clock, reset;
  const Spec &spec;
  const std::vector<Candidate> &in;
  unsigned count, bits;
  std::optional<ValueId> cursor;
  std::vector<ValueId> eligible;
  // LevelRoundRobin: the last winner of each level and the highest valid level.
  std::vector<ValueId> lasts;
  std::optional<ValueId> bucket;
  // Fifo: ahead[i][j] (i < j) is set while candidate i is ahead of j. Every
  // order bit resets to zero, which puts the highest index first.
  std::vector<std::vector<ValueId>> ahead;

  ValueId bit(unsigned n) { return b.constant(1, n); }
  ValueId and_(ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); }
  ValueId or_(ValueId x, ValueId y) { return b.binary(Op::BitOr, x, y); }
  ValueId not_(ValueId x) { return b.unary(Op::BitNot, x, 1); }
  ValueId eq(ValueId x, std::uint64_t v) {
    return b.binary(Op::Equal, x, b.constant(b.ir.values.at(x.index).width, v));
  }
  // A level's code: binary, or with Spec::thermometer its k low bits set.
  std::uint64_t level_code(unsigned k) const {
    return spec.thermometer ? (std::uint64_t{1} << k) - 1 : k;
  }

  void check() const {
    if (count < 2)
      throw std::logic_error("An arbiter serves two or more candidates");
    if (std::ranges::any_of(in, [&](const auto &c) {
          return !c.last || c.level.has_value() != in.front().level.has_value();
        }))
      throw std::logic_error("Arbiter candidates need a tail marker, and a level on all or none");
    if (spec.policy == Policy::FixedPriority) {
      std::vector<unsigned> sorted = spec.rank;
      std::ranges::sort(sorted);
      std::vector<unsigned> expected(count);
      std::iota(expected.begin(), expected.end(), 0U);
      if (sorted != expected)
        throw std::logic_error("Fixed priority ranks must be a permutation of the candidates");
    } else if (!spec.rank.empty()) {
      throw std::logic_error("Only fixed priority takes candidate ranks");
    }
  }

  ValueId request(unsigned i) const { return eligible.empty() ? in[i].valid : eligible[i]; }

  // The scan cursor of a level round robin: the candidate after the last winner of the highest
  // valid level (level 0 without levels).
  void level_cursor() {
    auto last = lasts[0];
    if (in.front().level) {
      const auto width = b.ir.values.at(in.front().level->index).width;
      bucket = std::accumulate(in.begin(), in.end(), b.constant(width, 0),
                               [&](ValueId top, const Candidate &c) {
                                 const auto above = b.binary(Op::UnsignedLess, top, *c.level);
                                 return b.select(and_(c.valid, above), *c.level, top);
                               });
      for (unsigned k = 1; k < lasts.size(); ++k)
        last = b.select(eq(*bucket, level_code(k)), lasts[k], last);
    }
    cursor = successor(last, count);
  }

  // Unheld choice as {index, grant}, the grant one-hot in every state. With no valid candidate
  // the choice is unused; its value only fixes the netlist. A request filter (such as a priority
  // level filter) belongs in front of the choice: scan, rotating_choice, fixed_choice and
  // fifo_choice read the filtered requests, while fifo_update keeps recording arrivals from the
  // unfiltered valid.
  std::pair<ValueId, std::vector<ValueId>> choose() {
    // Two rotating candidates: the tree is the cursor itself, as in the scan.
    if (spec.policy == Policy::RoundRobin || spec.policy == Policy::LevelRoundRobin ||
        (spec.policy == Policy::RotatingPriority && count == 2))
      return scan();
    std::vector<ValueId> requests;
    for (unsigned i = 0; i < count; ++i)
      requests.push_back(request(i));
    const auto choice = spec.policy == Policy::RotatingPriority
                            ? rotating_choice(b, requests, *cursor, 0, count).second
                        : spec.policy == Policy::FixedPriority ? fixed_choice()
                                                               : fifo_choice();
    std::vector<ValueId> grant;
    for (unsigned i = 0; i < count; ++i)
      grant.push_back(eq(choice, i));
    return {choice, grant};
  }
  // Inclusive parallel prefix: bit i says that some request in [0, i] is valid.
  std::vector<ValueId> prefix(std::vector<ValueId> terms) {
    for (unsigned step = 1; step < count; step *= 2) {
      auto previous = terms;
      for (unsigned i = step; i < count; ++i)
        terms[i] = or_(previous[i - step], previous[i]);
    }
    return terms;
  }
  std::pair<ValueId, std::vector<ValueId>> scan() {
    if (count == 2) {
      const auto no0 = not_(request(0));
      const auto choice = or_(and_(request(1), no0), and_(*cursor, or_(no0, request(1))));
      return {choice, {not_(choice), choice}};
    }
    std::vector<ValueId> requests, after;
    for (unsigned i = 0; i < count; ++i) {
      requests.push_back(request(i));
      after.push_back(
          and_(requests.back(), not_(b.binary(Op::UnsignedLess, b.constant(bits, i), *cursor))));
    }
    const auto all_prefix = prefix(requests), after_prefix = prefix(after);
    const auto in_range = b.binary(Op::UnsignedLess, b.unary(Op::ZeroExtend, *cursor, bits + 1),
                                   b.constant(bits + 1, count));
    std::vector<ValueId> grant, encoded;
    for (unsigned i = 0; i < count; ++i) {
      const auto first = i ? and_(requests[i], not_(all_prefix[i - 1])) : requests[i];
      const auto next = i ? and_(after[i], not_(after_prefix[i - 1])) : after[i];
      const auto selected = or_(next, and_(not_(after_prefix.back()), first));
      // Preserve the idle choice and out-of-range cursor decode in every state.
      grant.push_back(b.select(
          in_range, or_(selected, and_(not_(all_prefix.back()), eq(*cursor, i))), bit(i == 0)));
      encoded.push_back(b.select(grant.back(), b.constant(bits, i), b.constant(bits, 0)));
    }
    // The datapath consumes the one-hot grant directly; only state needs its index.
    return {join(b, encoded), grant};
  }
  // The valid candidate of smallest rank.
  ValueId fixed_choice() {
    std::vector<unsigned> by_rank(count);
    for (unsigned i = 0; i < count; ++i)
      by_rank[spec.rank[i]] = i;
    auto choice = b.constant(bits, by_rank.front());
    for (unsigned r = count; r > 0; --r)
      choice = b.select(request(by_rank[r - 1]), b.constant(bits, by_rank[r - 1]), choice);
    return choice;
  }
  void fifo_state(Arbiter &a) {
    ahead.assign(count, std::vector<ValueId>(count));
    for (unsigned i = 0; i < count; ++i)
      for (unsigned j = i + 1; j < count; ++j) {
        ahead[i][j] = b.state(1, clock, reset);
        a.registers.push_back(ahead[i][j]);
        a.state.emplace_back(".order" + std::to_string(i) + "_" + std::to_string(j), ahead[i][j]);
      }
  }
  // Candidate j is ahead of candidate i in the stored order.
  ValueId before(unsigned j, unsigned i) { return j < i ? ahead[j][i] : not_(ahead[i][j]); }
  // The valid candidate that no other valid candidate is ahead of.
  ValueId fifo_choice() {
    auto choice = b.constant(bits, 0);
    for (unsigned i = count; i > 0; --i) {
      auto first = request(i - 1);
      for (unsigned j = 0; j < count; ++j)
        if (j != i - 1)
          first = and_(first, not_(and_(request(j), before(j, i - 1))));
      choice = b.select(first, b.constant(bits, i - 1), choice);
    }
    return choice;
  }
  // Every cycle the order is stably partitioned into: requesting, idle,
  // released. A request therefore queues behind every request already present
  // and ahead of every idle candidate (requests never retract before they are
  // served); requests arriving in the same cycle keep their relative order;
  // the released winner moves to the back.
  void fifo_update(const Arbiter &a) {
    std::vector<ValueId> released, requesting, idle;
    for (unsigned i = 0; i < count; ++i) {
      released.push_back(and_(a.release, eq(a.winner, i)));
      const auto kept = not_(released.back());
      requesting.push_back(and_(in[i].valid, kept));
      idle.push_back(and_(not_(in[i].valid), kept));
    }
    auto sorts_before = [&](unsigned i, unsigned j) {
      return or_(and_(requesting[i], not_(requesting[j])), and_(idle[i], released[j]));
    };
    for (unsigned i = 0; i < count; ++i)
      for (unsigned j = i + 1; j < count; ++j) {
        auto set = sorts_before(i, j);
        auto clear = sorts_before(j, i);
        b.update(ahead[i][j], or_(set, and_(ahead[i][j], not_(clear))));
      }
  }
  // Value plus one modulo wrap.
  ValueId successor(ValueId value, unsigned wrap) {
    auto next = b.constant(bits, 0);
    for (unsigned i = 0; i < wrap; ++i)
      next = b.select(eq(value, i), b.constant(bits, (i + 1) % wrap), next);
    return next;
  }
  // Policy state moves after the hold terms. Round robin passes the released
  // winner (next: the index after it, from the output multiplexer); rotating
  // priority steps once per release and wraps at 2^ceil(log2 n), the full
  // counter; fixed priority keeps no state; the FIFO order updates every cycle.
  void advance(const Arbiter &a, std::optional<ValueId> next) {
    switch (spec.policy) {
    case Policy::RoundRobin:
      b.update(*cursor, b.select(a.release, *next, *cursor));
      return;
    case Policy::RotatingPriority:
      b.update(*cursor, b.select(a.release, successor(*cursor, std::bit_ceil(count)), *cursor));
      return;
    case Policy::FixedPriority:
      return;
    case Policy::Fifo:
      fifo_update(a);
      return;
    case Policy::LevelRoundRobin: {
      // An unheld choice records its winner under the level it served.
      const auto chosen = and_(or_(a.transfer, a.engage), not_(a.held));
      for (unsigned k = 0; k < lasts.size(); ++k)
        b.update(lasts[k], b.select(bucket ? and_(chosen, eq(*bucket, level_code(k))) : chosen,
                                    a.winner, lasts[k]));
      return;
    }
    }
  }

  // Graph output: tail, level and the round robin successor accumulate inside
  // the output multiplexer.
  void output(Arbiter &a, const Datapath &datapath, const std::vector<ValueId> &grant) {
    const auto choice = a.winner;
    std::vector<ValueId> owner_valid, valid, granted, tails, next;
    std::optional<ValueId> level = in[0].level;
    datapath.open();
    for (unsigned i = 0; i < count; ++i) {
      // At most one selected in every state (the held owner's decode, else the one-hot grant),
      // so valid, tail and the round robin successor join as AND-OR.
      const auto selected = b.select(a.held, eq(a.owner, i), grant[i]);
      owner_valid.push_back(and_(eq(a.owner, i), in[i].valid));
      valid.push_back(in[i].valid);
      granted.push_back(and_(selected, in[i].valid));
      tails.push_back(and_(selected, *in[i].last));
      if (spec.policy == Policy::RoundRobin)
        next.push_back(b.select(selected, b.constant(bits, (i + 1) % count), b.constant(bits, 0)));
      if (level)
        level = b.select(selected, *in[i].level, *level);
      datapath.candidate(i, selected);
    }
    const auto [sink_valid, sink_ready] =
        datapath.close(spec.direct_valid ? b.select(a.held, join(b, owner_valid), join(b, valid))
                                         : join(b, granted));
    const auto tail = join(b, tails);
    auto accepted = and_(sink_valid, sink_ready), release = and_(accepted, tail);
    auto engage = and_(sink_valid, or_(not_(sink_ready), not_(tail)));
    a.transfer = accepted;
    a.tail = tail;
    a.level = level;
    a.release = release;
    a.engage = engage;
    b.update(a.owner, b.select(and_(engage, not_(a.held)), choice, a.owner));
    b.update(a.held, b.select(release, bit(0), b.select(engage, bit(1), a.held)));
    advance(a, next.empty() ? std::nullopt : std::optional{join(b, next)});
  }
};
} // namespace

Arbiter lower_arbiter(Builder &b, ValueId clock, ValueId reset, const Spec &spec,
                      const std::vector<Candidate> &candidates, const Datapath &datapath) {
  return Lowering(b, clock, reset, spec, candidates).run(datapath);
}

std::vector<Arbiter> lower_output(Builder &b, ValueId clock, ValueId reset, const Spec &spec,
                                  const std::vector<Candidate> &in, const Datapath &datapath) {
  return {lower_arbiter(b, clock, reset, spec, in, datapath)};
}
} // namespace nocloom::detail::treadle
