#include "async_fifo.hpp"
#include "cone_walk.hpp"
#include "diagnostic.hpp"
#include "ir_cone.hpp"
#include <algorithm>
#include <bit>
#include <functional>
#include <map>
#include <set>

namespace nocloom::detail {
namespace {
bool in_stage_range(unsigned n) { return n >= 2 && n <= 4; }
// Two distinct valid domain names, 2 to 4 stages, a depth in range and a valid fragment layout.
bool config_ok(const std::string &from, const std::string &to, unsigned stages, unsigned depth,
               const std::vector<std::pair<std::string, unsigned>> &fields) {
  if (!valid_name(from) || !valid_name(to) || from == to || !in_stage_range(stages) ||
      depth < async_fifo_min_depth || depth > async_fifo_max_depth || fields.empty() ||
      fields.size() > 128)
    return false;
  std::set<std::string> names;
  for (const auto &[name, width] : fields)
    if (!valid_name(name) || !names.insert(name).second || width == 0 || width > 1024)
      return false;
  return true;
}
std::uint64_t mask(unsigned width) {
  return width >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << width) - 1;
}
std::uint64_t gray(std::uint64_t binary) { return binary ^ (binary >> 1); }
std::uint64_t binary(std::uint64_t code) {
  std::uint64_t result = 0;
  for (; code; code >>= 1)
    result ^= code;
  return result;
}

// Builder helpers. The IR has no exclusive-or or shift, so both are composed.
ValueId exclusive(Builder &b, ValueId x, ValueId y) {
  const auto width = b.ir.values.at(x.index).width;
  return b.binary(Op::BitAnd, b.binary(Op::BitOr, x, y),
                  b.unary(Op::BitNot, b.binary(Op::BitAnd, x, y), width));
}
ValueId to_gray(Builder &b, ValueId value, unsigned width) {
  return exclusive(b, value, b.concat(b.constant(1, 0), b.slice(value, 1, width - 1)));
}
ValueId from_gray(Builder &b, ValueId code, unsigned width) {
  auto bit = b.slice(code, width - 1, 1);
  auto result = bit;
  for (unsigned i = width - 1; i-- > 0;) {
    bit = exclusive(b, bit, b.slice(code, i, 1));
    result = b.concat(result, bit);
  }
  return result;
}
// value + 1 modulo 2^width, as value - (2^width - 1).
ValueId increment(Builder &b, ValueId value, unsigned width) {
  return b.binary(Op::Subtract, value, b.constant(width, mask(width)));
}
ValueId wrap_increment(Builder &b, ValueId index, unsigned width, unsigned depth) {
  return b.select(b.binary(Op::Equal, index, b.constant(width, depth - 1)), b.constant(width, 0),
                  increment(b, index, width));
}
ValueId gray_increment(Builder &b, ValueId code, unsigned width) {
  return to_gray(b, increment(b, from_gray(b, code, width), width), width);
}
std::vector<ValueId> chain(Builder &b, ValueId source, unsigned count, ValueId clock,
                           ValueId reset) {
  std::vector<ValueId> result;
  const auto width = b.ir.values.at(source.index).width;
  for (unsigned i = 0; i < count; ++i) {
    auto q = b.state(width, clock, reset);
    b.update(q, source);
    b.ir.synchronizer_stages.push_back(q);
    result.push_back(q);
    source = q;
  }
  return result;
}
ValueId both(Builder &b, ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); }
ValueId negate(Builder &b, ValueId x) { return b.unary(Op::BitNot, x, 1); }

// Evaluates a combinational cone over bound leaf values (by value index). Any
// state or input that is not a bound leaf, and any value wider than 64 bits,
// makes the cone unknown.
std::optional<std::uint64_t> eval(const HardwareIR &ir, ValueId root,
                                  const std::map<std::size_t, std::uint64_t> &in) {
  if (root.index >= ir.values.size())
    return std::nullopt;
  return evaluate_cone(ir, root.index, {}, {}, in, true);
}
struct Leaf {
  ValueId id;
  std::uint64_t count = 0; // the leaf takes every value in [0, count)
};
// True when root equals expected for every leaf assignment.
bool exhaustive(const HardwareIR &ir, ValueId root, const std::vector<Leaf> &leaves,
                const std::function<std::uint64_t(const std::vector<std::uint64_t> &)> &expected) {
  std::vector<std::uint64_t> at(leaves.size(), 0);
  while (true) {
    std::map<std::size_t, std::uint64_t> bound;
    for (std::size_t i = 0; i < leaves.size(); ++i)
      bound[leaves[i].id.index] = at[i];
    auto got = eval(ir, root, bound);
    if (!got || *got != expected(at))
      return false;
    std::size_t i = 0;
    for (; i < leaves.size(); ++i) {
      if (++at[i] < leaves[i].count)
        break;
      at[i] = 0;
    }
    if (i == leaves.size())
      return true;
  }
}
// A FIFO violation, thrown as the diagnostic that verify_async_fifo returns.
[[noreturn]] void fault(std::string message) {
  throw error("NCL_INTERNAL_ASYNC_FIFO", "hardware.fifo", std::move(message));
}
constexpr std::optional<std::uint64_t> zero_reset{0};

// The checks of a lowered FIFO in the order verify_async_fifo reports them; each fails by fault().
struct FifoAudit {
  explicit FifoAudit(const AsyncFifo &fifo) : f(fifo) {}
  void run() {
    shape();
    ports();
    handshake();
    behavior();
    if (f.fields.size() != f.layout.size())
      fault("Incomplete payload bundle");
    for (std::size_t i = 0; i < f.fields.size(); ++i)
      payload(i);
    if (expected_ports.size() != ir.ports.size())
      fault("Unbound FIFO port");
    if (owned.size() != ir.states.size())
      fault("FIFO contains unbound storage or control state");
    std::set<std::size_t> attributes;
    for (auto q : ir.synchronizer_stages)
      if (!attributes.insert(q.index).second)
        fault("Repeated synchronizer preservation binding");
    if (attributes != synchronized)
      fault("Synchronizer preservation must bind every actual stage");
    cones();
  }

  const AsyncFifo &f;
  const HardwareIR &ir = f.ir;
  const FifoDomain &a = f.domains[0], &z = f.domains[1];
  const unsigned w = f.count_bits, iw = f.index_bits, depth = f.depth;
  const bool hs = f.reset_handshake;
  const std::optional<std::uint64_t> count_reset = hs ? std::nullopt : zero_reset;
  const ValueId pa = hs ? f.handshake[0].enable : ValueId{},
                pz = hs ? f.handshake[1].enable : ValueId{};
  std::set<std::size_t> owned, synchronized, expected_ports;
  std::map<std::size_t, std::size_t> input_clocks;
  std::set<std::pair<std::size_t, std::size_t>> allowed_crossings;
  std::map<std::size_t, std::size_t> stage_successors;

  bool id_ok(ValueId id) const { return id.index < ir.values.size(); }
  bool width_is(ValueId id, unsigned width) const {
    return id_ok(id) && ir.values[id.index].width == width;
  }
  const State *state(ValueId id) const {
    return id_ok(id) && ir.values[id.index].op == Op::State
               ? &ir.states[ir.values[id.index].state.index]
               : nullptr;
  }
  bool input(ValueId id, unsigned width) const {
    return id_ok(id) && ir.values[id.index].op == Op::Input && ir.values[id.index].width == width;
  }
  bool constant(ValueId id, std::uint64_t literal) const {
    return id_ok(id) && ir.values[id.index].op == Op::Constant &&
           ir.values[id.index].literal == literal;
  }
  bool operation(ValueId id, Op op, std::initializer_list<ValueId> args) const {
    if (!id_ok(id) || ir.values[id.index].op != op)
      return false;
    return std::equal(args.begin(), args.end(), ir.values[id.index].operands.begin(),
                      [](auto x, auto y) { return x.index == y.index; });
  }
  // Every control register is clocked and reset by its own side; with the handshake the counts
  // have no reset and the startup clear pipeline resets with every stage occupied.
  bool control(ValueId id, const FifoDomain &d, unsigned width,
               std::optional<std::uint64_t> value = 0) {
    const auto *s = state(id);
    return s && width_is(id, width) && s->clock.index == d.clock.index &&
           s->reset.has_value() == value.has_value() &&
           (!s->reset || (s->reset->signal.index == d.reset.index && s->reset->value == *value)) &&
           owned.insert(id.index).second;
  }
  bool checked_chain(const std::vector<ValueId> &v, unsigned length, ValueId source,
                     const FifoDomain &d) {
    if (v.size() != length || !id_ok(source))
      return false;
    const auto width = ir.values[source.index].width;
    for (auto q : v) {
      if (!control(q, d, width) || state(q)->next.index != source.index)
        return false;
      synchronized.insert(q.index);
      if (q.index != v.front().index)
        stage_successors.emplace(source.index, q.index);
      if (const auto *s = state(source); s && s->clock.index != d.clock.index)
        allowed_crossings.emplace(source.index, q.index);
      source = q;
    }
    return true;
  }
  bool port_is(ValueId value, bool is_input, const std::string &endpoint,
               const std::string &signal) {
    for (std::size_t i = 0; i < ir.ports.size(); ++i) {
      const auto &p = ir.ports[i];
      if (p.input != is_input || p.endpoint != endpoint || p.signal != signal)
        continue;
      if (!expected_ports.insert(i).second)
        return false;
      if (is_input)
        return id_ok(value) && ir.values[value.index].op == Op::Input &&
               ir.values[value.index].input_port.index == i;
      return std::any_of(ir.drivers.begin(), ir.drivers.end(), [&](const auto &driver) {
        return driver.port.index == i && driver.value.index == value.index;
      });
    }
    return false;
  }
  bool stream(ValueId value, bool is_input, const std::string &signal) {
    return port_is(value, is_input, "stream", signal);
  }
  // A cone over leaves that the handshake also qualifies by a side's enable (a last leaf): it is
  // then zero while the enable is low.
  template <typename Expected>
  bool gated(ValueId root, std::vector<Leaf> leaves, ValueId enable, Expected expected) {
    if (hs)
      leaves.push_back({enable, 2});
    return exhaustive(ir, root, leaves, [&](const std::vector<std::uint64_t> &x) {
      return hs && !x.back() ? 0 : std::uint64_t(expected(x));
    });
  }
  template <typename Step>
  bool counter(ValueId q, ValueId event, std::uint64_t range, ValueId enable, Step step) {
    return gated(state(q)->next, {{event, 2}, {q, range}}, enable,
                 [&](const auto &x) { return x[0] ? step(x[1]) : x[1]; });
  }
  bool is_full(std::uint64_t wc, std::uint64_t rc) const {
    return ((binary(wc) - binary(rc)) & mask(w)) == depth;
  }

  void shape() {
    if (!config_ok(a.plan.name, z.plan.name, f.stages, depth, f.layout))
      fault("Invalid FIFO domains, depth or fragment layout");
    // The count range must exceed the depth so that a full FIFO differs from an empty one.
    if (w != async_fifo_count_bits(depth) || iw != async_fifo_index_bits(depth) ||
        (std::uint64_t{1} << w) <= depth)
      fault("Count or index width does not match the depth");
    if (!input(a.reset, 1) || !input(z.reset, 1) || !input(a.clock, 1) || !input(z.clock, 1) ||
        a.clock.index == z.clock.index || a.reset.index == z.reset.index)
      fault("Invalid FIFO resets or clocks");
    // Count registers are reset Gray codes; each crosses only through its own chain,
    // taken directly from the register with no logic in between.
    if (!control(f.write_index, a, iw) || !control(f.write_count, a, w, count_reset) ||
        !control(f.read_index, z, iw) || !control(f.read_count, z, w, count_reset) ||
        !control(f.out_valid, z, 1) ||
        !checked_chain(f.write_count_sync, f.stages, f.write_count, z) ||
        !checked_chain(f.read_count_sync, f.stages, f.read_count, a))
      fault("Invalid count, index or synchronizer ownership");
  }
  void ports() {
    for (const auto *d : {&a, &z})
      if (!port_is(d->clock, true, "domain:" + d->plan.name, "clock") ||
          !port_is(d->reset, true, "domain:" + d->plan.name, "reset_n"))
        fault("Reset or clock port is unbound");
    if (!input(f.valid, 1) || !input(f.out_ready, 1) || !stream(f.valid, true, "valid") ||
        !stream(f.out_ready, true, "out_ready") || !stream(f.ready, false, "ready") ||
        !stream(f.output_valid, false, "out_valid"))
      fault("Handshake port does not match its transfer binding");
    input_clocks[f.valid.index] = a.clock.index;
    input_clocks[f.out_ready.index] = z.clock.index;
  }
  // Handshake: announce rises after the clear pipeline empties with acknowledge low.
  // Completion follows a high acknowledge. Enable combines synchronized peer announce and on.
  void handshake() {
    const auto wait_bits = f.stages;
    for (unsigned d = 0; hs && d < 2; ++d) {
      const auto &h = f.handshake[d], &other = f.handshake[1 - d];
      const auto &side = d ? z : a;
      if (!control(h.announce, side, 1) || !control(h.on, side, 1) ||
          !control(h.wait, side, wait_bits, (1U << f.stages) - 1) || other.peer_sync.empty() ||
          !checked_chain(h.peer_sync, f.stages, other.announce, side) ||
          !checked_chain(h.ack_sync, f.stages, other.peer_sync.back(), side))
        fault("Invalid reset handshake registers or synchronizer chains");
      const auto ack = h.ack_sync.back();
      const std::uint64_t waits = std::uint64_t{1} << wait_bits;
      if (!exhaustive(ir, h.enable, {{h.peer_sync.back(), 2}, {h.on, 2}},
                      [](const auto &x) { return x[0] && x[1]; }) ||
          !exhaustive(ir, state(h.wait)->next, {{h.wait, waits}},
                      [](const auto &x) { return x[0] >> 1; }) ||
          !exhaustive(ir, state(h.announce)->next, {{h.announce, 2}, {ack, 2}, {h.wait, waits}},
                      [](const auto &x) { return x[0] || (!x[1] && !x[2]); }) ||
          !exhaustive(ir, state(h.on)->next, {{h.on, 2}, {h.announce, 2}, {ack, 2}},
                      [](const auto &x) { return x[0] || (x[1] && x[2]); }))
        fault("Reset handshake must announce after the wait and turn on on acknowledge");
    }
  }
  // Control behavior, checked over every value of each cone's bound leaves.
  void behavior() {
    const std::uint64_t counts = std::uint64_t{1} << w;
    const auto wc = f.write_count, rs = f.read_count_sync.back();
    const auto rc = f.read_count, ws = f.write_count_sync.back();
    if (!width_is(f.full, 1) || !exhaustive(ir, f.full, {{wc, counts}, {rs, counts}},
                                            [&](const auto &x) { return is_full(x[0], x[1]); }))
      fault("Full must compare the binary count distance with the depth");
    if (!width_is(f.ready, 1) || !gated(f.ready, {{wc, counts}, {rs, counts}}, pa,
                                        [&](const auto &x) { return !is_full(x[0], x[1]); }))
      fault("Ready requires a free entry");
    if (!width_is(f.write, 1) || !exhaustive(ir, f.write, {{f.valid, 2}, {f.ready, 2}},
                                             [](const auto &x) { return x[0] && x[1]; }))
      fault("Write must be valid and ready");
    if (!width_is(f.nonempty, 1) || !exhaustive(ir, f.nonempty, {{rc, counts}, {ws, counts}},
                                                [](const auto &x) { return x[0] != x[1]; }))
      fault("Nonempty requires unequal Gray counts");
    if (!width_is(f.read, 1) ||
        !gated(f.read, {{f.nonempty, 2}, {f.out_valid, 2}, {f.out_ready, 2}}, pz,
               [](const auto &x) { return x[0] && (!x[1] || x[2]); }))
      fault("The forward register must load whenever it is empty or drained");
    auto gray_step = [&](std::uint64_t code) { return gray((binary(code) + 1) & mask(w)); };
    auto index_step = [&](std::uint64_t index) { return (index + 1) % depth; };
    if (!counter(f.write_count, f.write, counts, pa, gray_step) ||
        !counter(f.read_count, f.read, counts, pz, gray_step))
      fault("Counts must advance one Gray code step per transfer");
    if (!counter(f.write_index, f.write, depth, pa, index_step) ||
        !counter(f.read_index, f.read, depth, pz, index_step))
      fault("Indexes must advance modulo the depth per transfer");
    if (!gated(state(f.out_valid)->next, {{f.read, 2}, {f.out_valid, 2}, {f.out_ready, 2}}, pz,
               [](const auto &x) { return x[0] || (x[1] && !x[2]); }))
      fault("Out valid must hold until drained and set on every load");
    if (hs ? !exhaustive(ir, f.output_valid, {{f.out_valid, 2}, {pz, 2}},
                         [](const auto &x) { return x[0] && x[1]; })
           : f.output_valid.index != f.out_valid.index)
      fault("Output valid must be out valid qualified by the receiver enable");
  }
  // One payload field: its ports, slots, read selection and receiver forward register.
  void payload(std::size_t i) {
    const auto &field = f.fields[i];
    const auto width = f.layout[i].second;
    if (field.name != f.layout[i].first || field.width != width || !input(field.input, width) ||
        field.storage.size() != depth || !stream(field.input, true, "in_f" + decimal(i)) ||
        !stream(field.output, false, "out_f" + decimal(i)))
      fault("Payload port does not match its field binding");
    input_clocks[field.input.index] = a.clock.index;
    storage(field, width);
    if (!control(field.received, z, width, f.reset_all ? zero_reset : std::nullopt) ||
        !operation(state(field.received)->next, Op::Select,
                   {f.read, field.selected, field.received}))
      fault("The receiver forward register must load the selected slot on read");
    const auto &out = ir.values[field.output.index];
    if (out.op != Op::Select || out.operands[0].index != f.output_valid.index ||
        out.operands[1].index != field.received.index || !constant(out.operands[2], 0))
      fault("Payload outputs must be zero unless out valid");
    for (auto slot : field.storage)
      allowed_crossings.emplace(slot.index, field.received.index);
  }
  // Each slot is a sender register written only at its index, reset iff the payload resets.
  void storage(const FifoField &field, unsigned width) {
    std::map<std::size_t, unsigned> slot_of;
    for (unsigned s = 0; s < depth; ++s) {
      const auto slot = field.storage[s];
      const auto *st = state(slot);
      if (!control(slot, a, width, f.reset_all ? zero_reset : std::nullopt))
        fault("Payload slots are sender-clocked registers with a reset iff reset_all");
      const auto &n = ir.values[st->next.index];
      if (n.op != Op::Select || n.operands[1].index != field.input.index ||
          n.operands[2].index != slot.index ||
          !exhaustive(ir, n.operands[0], {{f.write, 2}, {f.write_index, depth}},
                      [&](const auto &x) { return x[0] && x[1] == s; }))
        fault("A payload slot may change only when written at its index");
      slot_of[slot.index] = s;
    }
    // The read selection uses only the receiver index; walk it for every index.
    for (unsigned r = 0; r < depth; ++r) {
      auto at = field.selected;
      const std::map<std::size_t, std::uint64_t> bound{{f.read_index.index, r}};
      for (unsigned steps = 0; !slot_of.contains(at.index); ++steps) {
        if (steps > depth || !id_ok(at) || ir.values[at.index].op != Op::Select)
          fault("The read selection must be a mux over slots by read index");
        auto c = eval(ir, ir.values[at.index].operands[0], bound);
        if (!c)
          fault("The read selection may depend only on the read index");
        at = ir.values[at.index].operands[*c ? 1 : 2];
      }
      if (slot_of.at(at.index) != r)
        fault("The read selection must pick the slot at the read index");
    }
  }
  // Visits the states of root's cone depth first; each input in it must be sampled in clock.
  template <typename Visit>
  void walk(ValueId root, std::size_t clock, const char *message, Visit visit) const {
    walk_cone(ir, root, visit, [&](ValueId id) {
      if (!input_clocks.contains(id.index) || input_clocks.at(id.index) != clock)
        fault(message);
    });
  }
  // Actual register cones, independent of the declarations above.
  void cones() const {
    std::set<std::pair<std::size_t, std::size_t>> actual_crossings;
    for (const auto &s : ir.states)
      walk(s.next, s.clock.index, "Unbound input in a register data cone", [&](ValueId id) {
        if (stage_successors.contains(id.index) && stage_successors.at(id.index) != s.output.index)
          fault("Intermediate synchronization stage has a functional consumer");
        if (state(id)->clock.index != s.clock.index)
          actual_crossings.emplace(id.index, s.output.index);
      });
    if (actual_crossings != allowed_crossings)
      fault("Actual crossings differ from synchronizer chains and slot reads");
    // Outputs are combinational in one domain only.
    for (const auto &driver : ir.drivers) {
      const auto clock = (driver.value.index == f.ready.index ? a.clock : z.clock).index;
      walk(driver.value, clock, "An output depends on an input of another domain", [&](ValueId id) {
        if (state(id)->clock.index != clock)
          fault("An output mixes clock domains");
      });
    }
  }
};
} // namespace

unsigned async_fifo_count_bits(unsigned depth) {
  return static_cast<unsigned>(std::bit_width(depth));
}
unsigned async_fifo_index_bits(unsigned depth) {
  return std::max(1U, static_cast<unsigned>(std::bit_width(depth - 1)));
}

Result<CrossingDepth> default_crossing_depth(unsigned stages,
                                             std::optional<std::uint64_t> sender_period_ps,
                                             std::optional<std::uint64_t> receiver_period_ps) {
  if (!in_stage_range(stages))
    return Result<CrossingDepth>::failure(error("NCL_CDC_STAGES_UNSUPPORTED", "synchronizer_stages",
                                                "Synchronizer stages must be 2 to 4"));
  for (auto period : {sender_period_ps, receiver_period_ps})
    if (period && (*period < crossing_min_period_ps || *period > crossing_max_period_ps))
      return Result<CrossingDepth>::failure(
          error("NCL_CDC_PERIOD_RANGE", "clock_domains.period_ps",
                "A clock period must be 100 to 1000000000 picoseconds"));
  if (!sender_period_ps || !receiver_period_ps)
    return Result<CrossingDepth>::success(
        {async_fifo_fallback_depth, CrossingDepthSource::Fallback});
  const auto ta = *sender_period_ps, tb = *receiver_period_ps;
  const auto loop = (std::uint64_t{stages} + 1) * (ta + tb);
  const auto slow = std::max(ta, tb);
  const auto depth = (loop + slow - 1) / slow;
  // The loop spans more than n + 1 and at most 2n + 2 slow periods.
  if (depth < async_fifo_min_depth || depth > async_fifo_max_depth)
    return Result<CrossingDepth>::failure(
        error("NCL_INTERNAL_ASYNC_FIFO", "hardware.fifo", "Default depth left its proven range"));
  return Result<CrossingDepth>::success(
      {static_cast<unsigned>(depth), CrossingDepthSource::Formula});
}

Result<CrossingDepth> resolve_crossing_depth(std::optional<unsigned> requested, unsigned stages,
                                             std::optional<std::uint64_t> sender_period_ps,
                                             std::optional<std::uint64_t> receiver_period_ps) {
  auto fallback = default_crossing_depth(stages, sender_period_ps, receiver_period_ps);
  if (!fallback || !requested)
    return fallback;
  if (*requested < async_fifo_min_depth || *requested > async_fifo_max_depth)
    return Result<CrossingDepth>::failure(error("NCL_CDC_DEPTH_RANGE", "clock_crossings.depth",
                                                "A crossing depth must be 2 to 10 entries"));
  return Result<CrossingDepth>::success({*requested, CrossingDepthSource::Explicit});
}

Result<AsyncFifo> lower_async_fifo(const DomainPlan &from, const DomainPlan &to, unsigned depth,
                                   unsigned stages,
                                   const std::vector<std::pair<std::string, unsigned>> &fields,
                                   bool reset_handshake, bool reset_all) {
  if (!config_ok(from.name, to.name, stages, depth, fields))
    return Result<AsyncFifo>::failure(error("NCL_ASYNC_FIFO_CONFIG", "hardware.fifo",
                                            "Invalid domains, depth or fragment layout"));
  Builder b;
  b.ir.name = "nocloom_async_fifo";
  AsyncFifo f{};
  f.depth = depth;
  f.stages = stages;
  f.count_bits = async_fifo_count_bits(depth);
  f.index_bits = async_fifo_index_bits(depth);
  f.layout = fields;
  f.reset_handshake = reset_handshake;
  f.reset_all = reset_all;
  const auto w = f.count_bits, iw = f.index_bits;
  auto one = b.constant(1, 1);
  for (unsigned d = 0; d < 2; ++d) {
    auto &domain = f.domains[d];
    domain.plan = d == 0 ? from : to;
    domain.clock = b.input("nl_clk_" + decimal(d), 1, "domain:" + domain.plan.name, "clock");
    domain.reset = b.input("nl_reset_n_" + decimal(d), 1, "domain:" + domain.plan.name, "reset_n");
  }
  const auto &a = f.domains[0], &z = f.domains[1];
  auto in = [&](const std::string &name, unsigned width) {
    return b.input("nl_stream_" + name, width, "stream", name);
  };
  f.valid = in("valid", 1);
  f.out_ready = in("out_ready", 1);
  // With the handshake a count clears synchronously, one cycle after its side's enable falls,
  // which the other side sees no later than its own enable falling.
  auto maybe_reset = [&](const FifoDomain &d, unsigned width, bool with_reset) {
    return with_reset ? b.state(width, d.clock, d.reset) : b.held_state(width, d.clock);
  };
  f.write_index = b.state(iw, a.clock, a.reset);
  f.write_count = maybe_reset(a, w, !reset_handshake);
  f.read_index = b.state(iw, z.clock, z.reset);
  f.read_count = maybe_reset(z, w, !reset_handshake);
  f.out_valid = b.state(1, z.clock, z.reset);
  f.write_count_sync = chain(b, f.write_count, stages, z.clock, z.reset);
  f.read_count_sync = chain(b, f.read_count, stages, a.clock, a.reset);
  if (reset_handshake) {
    const auto wait_bits = stages;
    auto reg = [&](unsigned d, unsigned width, std::uint64_t value = 0) {
      return b.state(width, f.domains[d].clock, f.domains[d].reset, value);
    };
    auto sync = [&](unsigned d, ValueId source) {
      return chain(b, source, stages, f.domains[d].clock, f.domains[d].reset);
    };
    for (auto d : {0U, 1U})
      f.handshake[d] = {reg(d, 1), reg(d, 1), reg(d, wait_bits, (1U << stages) - 1)};
    for (auto d : {0U, 1U})
      f.handshake[d].peer_sync = sync(d, f.handshake[1 - d].announce);
    for (auto d : {0U, 1U}) {
      auto &h = f.handshake[d];
      h.ack_sync = sync(d, f.handshake[1 - d].peer_sync.back());
      const auto ack = h.ack_sync.back();
      const auto waited = b.binary(Op::Equal, h.wait, b.constant(wait_bits, 0));
      b.update(h.wait, b.concat(b.constant(1, 0), b.slice(h.wait, 1, wait_bits - 1)));
      b.update(h.announce, b.binary(Op::BitOr, h.announce, both(b, negate(b, ack), waited)));
      b.update(h.on, b.binary(Op::BitOr, h.on, both(b, h.announce, ack)));
      h.enable = both(b, h.peer_sync.back(), h.on);
    }
  }
  auto gate = [&](ValueId x, unsigned d) {
    return reset_handshake ? both(b, x, f.handshake[d].enable) : x;
  };
  auto cleared = [&](ValueId next, unsigned d) {
    return reset_handshake ? b.select(f.handshake[d].enable, next,
                                      b.constant(b.ir.values.at(next.index).width, 0))
                           : next;
  };

  // Sender: occupancy is the binary count difference modulo 2^w; depth < 2^w.
  f.full = b.binary(Op::Equal,
                    b.binary(Op::Subtract, from_gray(b, f.write_count, w),
                             from_gray(b, f.read_count_sync.back(), w)),
                    b.constant(w, depth));
  f.ready = gate(negate(b, f.full), 0);
  f.write = both(b, f.valid, f.ready);
  b.update(
      f.write_index,
      cleared(b.select(f.write, wrap_increment(b, f.write_index, iw, depth), f.write_index), 0));
  b.update(f.write_count,
           cleared(b.select(f.write, gray_increment(b, f.write_count, w), f.write_count), 0));

  // Receiver: the forward register loads whenever it is empty or being drained.
  f.nonempty = negate(b, b.binary(Op::Equal, f.read_count, f.write_count_sync.back()));
  f.read = both(b, f.nonempty, gate(b.binary(Op::BitOr, negate(b, f.out_valid), f.out_ready), 1));
  b.update(f.read_index,
           cleared(b.select(f.read, wrap_increment(b, f.read_index, iw, depth), f.read_index), 1));
  b.update(f.read_count,
           cleared(b.select(f.read, gray_increment(b, f.read_count, w), f.read_count), 1));
  b.update(f.out_valid,
           cleared(b.select(f.read, one, both(b, f.out_valid, negate(b, f.out_ready))), 1));
  f.output_valid = gate(f.out_valid, 1);

  std::vector<ValueId> slot_write, slot_read;
  for (unsigned i = 0; i < depth; ++i) {
    slot_write.push_back(both(b, f.write, b.binary(Op::Equal, f.write_index, b.constant(iw, i))));
    slot_read.push_back(b.binary(Op::Equal, f.read_index, b.constant(iw, i)));
  }
  for (std::size_t i = 0; i < fields.size(); ++i) {
    FifoField field{};
    field.name = fields[i].first;
    field.width = fields[i].second;
    field.input = in("in_f" + decimal(i), field.width);
    for (unsigned s = 0; s < depth; ++s) {
      auto slot = maybe_reset(a, field.width, reset_all);
      b.update(slot, b.select(slot_write[s], field.input, slot));
      field.storage.push_back(slot);
    }
    field.selected = field.storage.back();
    for (unsigned s = depth - 1; s-- > 0;)
      field.selected = b.select(slot_read[s], field.storage[s], field.selected);
    field.received = maybe_reset(z, field.width, reset_all);
    b.update(field.received, b.select(f.read, field.selected, field.received));
    field.output = b.select(f.output_valid, field.received, b.constant(field.width, 0));
    f.fields.push_back(std::move(field));
  }
  b.output("nl_stream_ready", f.ready, "stream", "ready");
  b.output("nl_stream_out_valid", f.output_valid, "stream", "out_valid");
  for (std::size_t i = 0; i < f.fields.size(); ++i)
    b.output("nl_stream_out_f" + decimal(i), f.fields[i].output, "stream", "out_f" + decimal(i));
  f.ir = std::move(b.ir);
  if (auto status = verify_async_fifo(f); !status)
    return Result<AsyncFifo>::failure(status.diagnostics());
  return Result<AsyncFifo>::success(std::move(f));
}

Status verify_async_fifo(const AsyncFifo &f) {
  if (auto status = verify_structure(f.ir); !status)
    return status;
  try {
    FifoAudit(f).run();
    return Status::success({});
  } catch (Diagnostic &d) {
    return Status::failure(std::move(d));
  }
}
} // namespace nocloom::detail
