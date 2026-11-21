#include "error_logger.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "treadle.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <iterator>
#include <map>
#include <numeric>
#include <optional>
#include <string>
#include <utility>

namespace nocloom::detail {
// The stored fields of a record as a target's probe holds them, in crossing order.
RecordLayout error_record_layout(const ErrorLoggerPlan &p) {
  const auto &s = p.shape;
  RecordLayout layout;
  for (const auto &[name, width] : std::array<std::pair<const char *, unsigned>, 10>{
           {{"class", s.class_bits},
            {"write", 1},
            {"wrap", 1},
            {"exclusive", p.fixed_length ? 0U : 1U},
            {"address_partial", 1},
            {"address", s.address_bits},
            {"last_offset", p.fixed_length ? 0U : s.length_bits},
            {"route", s.route_bits},
            {"audit_flags", s.audit_bits},
            {"user_flags", s.user_bits}}})
    if (width) {
      layout.fields.push_back({name, width, layout.bits});
      layout.bits += width;
    }
  // A crossed record says whether its probe dropped a failure while the record waited. The flag
  // is the top bit, so it travels in the last word, which leaves the sender with the record.
  if (p.crossed) {
    layout.fields.push_back({"probe_overflow", 1, layout.bits});
    layout.bits += 1;
  }
  return layout;
}
namespace {
constexpr unsigned bus_bits = 32;
// One term per wanted bit of a record's field.
void filter_terms(logic::Logic &l, ValueId field, const std::vector<RecordFilterBit> &wanted,
                  std::vector<ValueId> &terms) {
  std::ranges::transform(wanted, std::back_inserter(terms), [&](const RecordFilterBit &want) {
    const auto bit = l.b.slice(field, want.bit, 1);
    return want.value ? bit : l.inv(bit);
  });
}
// The bank fields of a record's user flags, a word each.
void user_words(logic::Logic &l, const std::string &face, ValueId user, unsigned bits) {
  const auto words = (bits + bus_bits - 1) / bus_bits;
  for (unsigned k = 0; k < words; ++k) {
    const auto lsb = k * bus_bits;
    l.out(face, "record_user_flags" + std::to_string(k),
          l.b.slice(user, lsb, std::min(bus_bits, bits - lsb)));
  }
}
// A logger of a chain: its filter in one form, its controls and what the chain settles for it.
struct Slot {
  std::string suffix;
  const std::vector<unsigned> *skipped = nullptr;
  const std::vector<RecordFilterBit> *user_match = nullptr, *audit_match = nullptr;
  bool pass_always = false;
  ValueId clear{}, clear_overflow{}, alarm_enable{}, wait_when_full{}, valid{}, hold{};
  ValueId accepts{}, offered{}, capture{}, dropped{}, reached{}, status{};
  std::string scope() const { return "logger" + suffix + "."; }
};
struct Record {
  ValueId route, write, wrap, length, exclusive, partial, address, error_class, user, audit;
  ValueId probe_overflow{};
};
// Packet tracking and event ownership have independent flags.
class Probe {
public:
  Probe(logic::Logic &logic, const ErrorLoggerPlan &plan) : l(logic), p(plan) {}
  ValueId tracking{}, wanting{};
  Record held{};
  void declare(const std::string &probe, ValueId clock, ValueId reset, ValueId target_reset,
               bool related = false) {
    target_live = target_reset;
    const auto &shape = p.shape;
    const auto state = [&](const std::string &name, unsigned width) {
      return l.bind(name, l.b.state(width, clock, reset));
    };
    const auto stored_length = [&](const std::string &name) {
      return p.fixed_length ? l.num(shape.length_bits, p.fixed_length)
                            : state(name, shape.length_bits);
    };
    tracking = l.bind(probe + "tracking", l.b.state(1, clock, target_live));
    wanting = state(probe + "pending", 1);
    held = {state(probe + "route", shape.route_bits),
            state(probe + "write", 1),
            state(probe + "wrap", 1),
            stored_length(probe + "last_offset"),
            p.fixed_length ? l.bit(0) : state(probe + "exclusive", 1),
            state(probe + "address_partial", 1),
            state(probe + "address", shape.address_bits),
            shape.class_bits != 0 ? state(probe + "class", shape.class_bits) : ValueId{},
            shape.user_bits != 0 ? state(probe + "user_flags", shape.user_bits) : ValueId{},
            shape.audit_bits != 0 ? state(probe + "audit_flags", shape.audit_bits) : ValueId{}};
    if (related)
      held.probe_overflow = state(probe + "overflow", 1);
  }
  // The probe's next state: done says the record has left.
  void update(const std::string &face, ValueId done) {
    const auto moved = l.in(face, "moved", 1);
    const auto head = l.in(face, "head", 1);
    const auto tail = l.in(face, "tail", 1), failed = l.in(face, "failed", 1);
    const auto at_head = l.both(moved, head);
    const auto bad_head = l.both(at_head, failed);
    const auto good_head = l.every(std::array{at_head, l.inv(failed), l.inv(tail)});
    const auto word = tracking, hold = wanting;
    const auto idle = l.inv(l.either(tracking, wanting));
    // A head while a packet is tracked starts a packet of its own, such as a write response
    // between the words of a read. The tracked packet ends there.
    const auto available = l.any(std::array{idle, l.both(hold, done), l.both(word, at_head)});
    const auto new_fault =
        l.bind(face + ".accepted",
               l.either(l.both(available, bad_head), l.every(std::array{word, moved, failed})));
    l.bind(face + ".dropped", l.both(bad_head, l.inv(available)));
    l.b.update(wanting, l.either(new_fault, l.both(hold, l.inv(done))));
    l.b.update(tracking, l.either(l.both(available, good_head),
                                  l.both(word, l.inv(l.both(moved, l.either(failed, tail))))));
    const auto sample = l.both(at_head, available);
    store(face, sample, l.either(sample, new_fault));
  }
  void update_related(const std::string &face, ValueId done, ValueId released, ValueId open) {
    take_or_drop(face, l.both(l.in(face, "moved", 1), target_live), done, released, open);
  }
  // A sender's probe: every first failure of a packet is taken or dropped, as at a related probe
  // whose log is always open. A drop rides on the waiting record to the first logger.
  void update_sender(const std::string &face, ValueId done) {
    take_or_drop(face, l.in(face, "moved", 1), done, std::nullopt, std::nullopt);
  }

private:
  logic::Logic &l;
  const ErrorLoggerPlan &p;
  ValueId target_live{};
  // A failure waits while the probe keeps a record that is released and not leaving. A related
  // probe also needs an open log edge to take one.
  void take_or_drop(const std::string &face, ValueId moved, ValueId done,
                    std::optional<ValueId> released, std::optional<ValueId> open) {
    const auto head = l.in(face, "head", 1), tail = l.in(face, "tail", 1);
    const auto failed = l.in(face, "failed", 1);
    const auto event =
        l.bind(face + ".event", l.every(std::array{moved, failed, l.either(head, tracking)}));
    // Track the first failure independently of the log's reset and queue. The target supplies
    // the packet fields again on the failed word, including when its head preceded log release.
    l.b.update(tracking,
               l.pick(moved,
                      l.every(std::array{l.either(head, tracking), l.inv(failed), l.inv(tail)}),
                      tracking));
    const auto waiting = released ? l.every(std::array{wanting, *released, l.inv(done)})
                                  : l.both(wanting, l.inv(done));
    const auto allowed = open ? l.both(event, *open) : event;
    const auto accepted = l.bind(face + ".accepted", l.both(allowed, l.inv(waiting)));
    const auto dropped = l.bind(face + ".dropped", l.both(allowed, waiting));
    l.b.update(wanting, l.either(accepted, waiting));
    const auto overflow = held.probe_overflow;
    l.b.update(overflow, l.either(dropped, l.both(waiting, overflow)));
    store(face, accepted, accepted);
  }
  void store(const std::string &face, ValueId sample, ValueId sample_class) {
    const auto &shape = p.shape;
    Builder &b = l.b;
    const auto keep = [&](ValueId state, const char *signal, unsigned bits) {
      b.update(state, l.pick(sample, l.in(face, signal, bits), state));
    };
    keep(held.route, "route", shape.route_bits);
    keep(held.write, "write", 1);
    keep(held.wrap, "wrap", 1);
    if (!p.fixed_length) {
      keep(held.length, "last_offset", shape.length_bits);
      keep(held.exclusive, "exclusive", 1);
    }
    keep(held.partial, "address_partial", 1);
    keep(held.address, "address", shape.address_bits);
    if (shape.class_bits != 0)
      b.update(held.error_class,
               l.pick(sample_class, l.in(face, "class", shape.class_bits), held.error_class));
    if (shape.user_bits != 0)
      keep(held.user, "user_flags", shape.user_bits);
    if (shape.audit_bits != 0)
      keep(held.audit, "audit_flags", shape.audit_bits);
  }
};

class Logger {
public:
  Logger(const ErrorLoggerPlan &plan, std::span<const unsigned> related, bool selected_edges)
      : p(plan), shape(plan.shape), related_probes(related), selected_log_edges(selected_edges),
        with_class(shape.class_bits != 0), with_user(shape.user_bits != 0),
        with_audit(shape.audit_bits != 0), frame_words(plan.crossed ? plan.serial_words : 1),
        count_bits(std::max(1U, static_cast<unsigned>(std::bit_width(frame_words - 1)))) {}

  Result<NodeHardware> build() {
    controls();
    probes();
    served = wanting;
    selected = held[0];
    moving = wanting[0];
    slots[0].hold = l.bind("logger.hold", l.both(slots[0].wait_when_full, slots[0].valid));
    if (!chained())
      ready = l.either(l.inv(last), l.inv(slots[0].hold));
    arbitrate();
    auto end = l.bit(1);
    if (p.crossed) {
      end = l.bit(0);
      for (unsigned k = 0; k < p.targets; ++k)
        end = l.either(end, l.both(served[k], word_last[k]));
    }
    const auto taken = l.bind("logger.taken", l.every(std::array{moving, last, ready, end}));
    b.update(count, l.pick(l.every(std::array{moving, ready, l.inv(last)}), l.inc(count),
                           l.pick(taken, l.num(count_bits, 0), count)));
    captures(taken);
    for (const auto &s : slots)
      record_slot(s, taken);
    if (p.crossed)
      word_faces();
    else
      for (unsigned k = 0; k < p.targets; ++k)
        probe_update(k, l.both(taken, served[k]));
    auto name = "nocloom_error_logger_" + p.name;
    if (related_probes.empty())
      return l.finish(std::move(name));
    b.ir.name = std::move(name);
    if (const auto checked = verify_structure(b.ir); !checked)
      return Result<NodeHardware>::failure(checked.diagnostics());
    return Result<NodeHardware>::success({std::move(b.ir), std::move(l.bindings)});
  }

private:
  const ErrorLoggerPlan &p;
  const ErrorRecordShape &shape;
  const std::span<const unsigned> related_probes;
  const bool selected_log_edges;
  const bool with_class, with_user, with_audit;
  const unsigned frame_words, count_bits;
  logic::Logic l;
  Builder &b = l.b;
  std::vector<Slot> slots;
  ValueId count{}, last{}, ready{}, moving{};
  ValueId log_edge{}, probe_open{};
  std::vector<Record> held;
  std::vector<ValueId> wanting, served, gather, word_data, word_last;
  std::vector<Probe> probe_of;
  Record selected{};

  bool chained() const { return !p.chain.empty(); }
  // The records carry their probe's drop flag: related probes and senders across a crossing.
  bool probe_drops() const { return p.crossed || !related_probes.empty(); }

  void control(Slot &s) {
    const auto face = "control" + s.suffix;
    s.clear = l.bind(s.scope() + "clear", l.in(face, "record_held_clear", 1));
    s.clear_overflow = l.in(face, "record_overflow_clear", 1);
    s.alarm_enable = l.in(face, "alarm_enable", 1);
    s.wait_when_full = l.in(face, "wait_when_full", 1);
  }
  void controls() {
    slots.push_back({"", &p.skipped_classes, &p.user_match, &p.audit_match, p.pass_always});
    for (std::size_t n = 0; n < p.chain.size(); ++n) {
      const auto &next = p.chain[n];
      slots.push_back({std::to_string(n + 1), &next.skipped_classes, &next.user_match,
                       &next.audit_match, next.pass_always});
    }
    control(slots[0]);
    count = l.state("logger.serial", count_bits);
    for (std::size_t n = 0; n < slots.size(); ++n) {
      auto &s = slots[n];
      if (n != 0)
        control(s);
      if (chained()) {
        s.status = l.state(s.scope() + "status", 2);
        s.valid = l.bind(s.scope() + "valid", b.slice(s.status, 0, 1));
      } else
        s.valid = l.state(s.scope() + "valid", 1);
    }
    last = l.bind("logger.serial_last", l.eq(count, frame_words - 1));
  }
  void probes() {
    auto root = l.clock, reset_port = l.reset;
    if (!related_probes.empty()) {
      root = b.input("nl_root_clk", 1, "domain:root", "clock");
      reset_port = l.in("handover", "reset_port", 1);
      log_edge = selected_log_edges ? l.in("handover", "log_edge", 1) : l.bit(1);
      probe_open = log_edge;
      if (selected_log_edges) {
        const auto started = l.bind("logger.probe_started", b.state(1, root, reset_port));
        // While reset is effective, keep only the edge just sampled. This permits an event
        // on the edge that releases reset without using a synchronizer's intermediate stage.
        probe_open = l.either(log_edge, l.both(started, l.reset));
        b.update(started, probe_open);
      }
    }
    for (unsigned k = 0; k < p.targets; ++k) {
      if (p.crossed) {
        words(k);
        continue;
      }
      auto &probe = probe_of.emplace_back(l, p);
      const auto index = std::to_string(k);
      const bool related = std::ranges::binary_search(related_probes, k);
      const auto target_reset = related ? l.in("event" + index, "target_live", 1) : l.reset;
      probe.declare("probe" + index + ".", related ? root : l.clock, related ? reset_port : l.reset,
                    target_reset, related);
      wanting.push_back(related ? l.both(probe.wanting, l.reset) : probe.wanting);
      held.push_back(probe.held);
      if (!related && !related_probes.empty())
        held.back().probe_overflow = l.bit(0);
    }
  }
  // The record of a target past a crossing: the words before the last in the gather registers, and
  // the word on the face.
  void words(unsigned k) {
    const auto face = "word" + std::to_string(k);
    const auto bits = error_record_word_bits(p);
    if (gather.empty())
      for (unsigned n = 0; n + 1 < p.serial_words; ++n)
        gather.push_back(l.state("logger.gather" + std::to_string(n), bits));
    wanting.push_back(l.in(face, "valid", 1));
    word_data.push_back(l.in(face, "data", bits));
    word_last.push_back(l.in(face, "last", 1));
    const auto whole =
        std::accumulate(gather.rbegin(), gather.rend(), word_data.back(),
                        [&](ValueId upper, ValueId word) { return b.concat(upper, word); });
    std::map<std::string, ValueId> by_name;
    for (const auto &[name, width, lsb] : error_record_layout(p).fields)
      by_name[name] = b.slice(whole, lsb, width);
    const auto named = [&](const char *name, ValueId otherwise) {
      const auto it = by_name.find(name);
      return it == by_name.end() ? otherwise : it->second;
    };
    held.push_back({by_name.at("route"), by_name.at("write"), by_name.at("wrap"),
                    named("last_offset", l.num(shape.length_bits, p.fixed_length)),
                    named("exclusive", l.bit(0)), by_name.at("address_partial"),
                    by_name.at("address"), named("class", ValueId{}),
                    named("user_flags", ValueId{}), named("audit_flags", ValueId{}),
                    by_name.at("probe_overflow")});
  }
  // The word on the path goes to the gather registers; the path's ready goes to the face of the
  // target it serves.
  void word_faces() {
    auto word = word_data[0];
    if (p.targets > 1) {
      word = l.num(l.width(word), 0);
      for (unsigned k = 0; k < p.targets; ++k)
        word = l.either(word, l.pick(served[k], word_data[k], l.num(l.width(word), 0)));
    }
    for (unsigned n = 0; n < gather.size(); ++n)
      b.update(gather[n], l.pick(l.both(moving, l.eq(count, n)), word, gather[n]));
    for (unsigned k = 0; k < p.targets; ++k)
      l.out("word" + std::to_string(k), "ready", p.targets > 1 ? l.both(served[k], ready) : ready);
  }
  // A logger's verdict on the record on the path.
  void verdict(Slot &s, const Record &from) {
    // A skipped class is one bit of the record's class.
    std::vector<ValueId> skipped;
    if (with_class)
      std::ranges::transform(*s.skipped, std::back_inserter(skipped), [&](unsigned one) {
        return b.slice(from.error_class, static_cast<unsigned>(std::countr_zero(one)), 1);
      });
    std::vector<ValueId> filtered;
    if (with_user)
      filter_terms(l, from.user, *s.user_match, filtered);
    if (with_audit)
      filter_terms(l, from.audit, *s.audit_match, filtered);
    auto wanted = l.inv(l.any(skipped));
    if (!filtered.empty())
      wanted = l.both(wanted, l.bind(s.scope() + "filter_match", l.every(filtered)));
    s.accepts = l.bind(s.scope() + "accept", wanted);
  }
  // A transaction visits the prefix ending at the first consuming filter.
  // All visited slots must release their holds before the transaction commits.
  ValueId settle_chain(const Record &from) {
    auto reached = l.bit(1);
    std::vector<ValueId> blocked;
    for (auto &s : slots) {
      verdict(s, from);
      if (s.suffix != "")
        s.hold = l.bind(s.scope() + "hold", l.both(s.wait_when_full, s.valid));
      s.reached = reached;
      s.dropped = l.bind(s.scope() + "drop", s.pass_always ? l.bit(0) : s.accepts);
      blocked.push_back(l.both(reached, s.hold));
      reached = l.both(reached, l.inv(s.dropped));
    }
    const auto commit_ready = l.bind("logger.ready", l.inv(l.any(blocked)));
    return l.either(l.inv(last), commit_ready);
  }
  // One record on the serial path at a time: the target granted holds it from its first word to its
  // last, and the grant moves on as an output of a network switch does.
  void arbitrate() {
    if (p.targets == 1) {
      if (chained())
        ready = settle_chain(selected);
      return;
    }
    std::vector<treadle::Candidate> candidates;
    std::ranges::transform(wanting, std::back_inserter(candidates),
                           [&](ValueId w) { return treadle::Candidate{w, last}; });
    const auto zero = [&](ValueId like) { return l.num(l.width(like), 0); };
    Record joined{zero(selected.route),
                  zero(selected.write),
                  zero(selected.wrap),
                  zero(selected.length),
                  zero(selected.exclusive),
                  zero(selected.partial),
                  zero(selected.address),
                  with_class ? zero(selected.error_class) : ValueId{},
                  with_user ? zero(selected.user) : ValueId{},
                  with_audit ? zero(selected.audit) : ValueId{}};
    if (probe_drops())
      joined.probe_overflow = l.bit(0);
    treadle::Spec spec;
    spec.policy = treadle::Policy::RotatingPriority;
    const auto arbiter =
        treadle::lower_arbiter(b, l.clock, l.reset, spec, candidates,
                               {[] {},
                                [&](unsigned k, ValueId term) {
                                  served[k] = term;
                                  const auto add = [&](ValueId &to, ValueId from) {
                                    to = l.either(to, l.pick(term, from, zero(from)));
                                  };
                                  add(joined.route, held[k].route);
                                  add(joined.write, held[k].write);
                                  add(joined.wrap, held[k].wrap);
                                  add(joined.length, held[k].length);
                                  add(joined.exclusive, held[k].exclusive);
                                  add(joined.partial, held[k].partial);
                                  add(joined.address, held[k].address);
                                  if (with_class)
                                    add(joined.error_class, held[k].error_class);
                                  if (with_user)
                                    add(joined.user, held[k].user);
                                  if (with_audit)
                                    add(joined.audit, held[k].audit);
                                  if (probe_drops())
                                    add(joined.probe_overflow, held[k].probe_overflow);
                                },
                                [&](ValueId any_valid) {
                                  moving = any_valid;
                                  if (chained())
                                    ready = settle_chain(joined);
                                  return std::pair{any_valid, ready};
                                }});
    for (std::size_t k = 0; k < arbiter.registers.size(); ++k)
      l.bind("logger.arbiter_register" + std::to_string(k), arbiter.registers[k]);
    selected = joined;
  }
  // A logger takes a record when it holds none, and in the cycle its record is cleared: the
  // record that arrives then stays, and nothing is lost.
  ValueId room(const Slot &s) { return l.either(l.inv(s.valid), s.clear); }
  void captures(ValueId taken) {
    if (!chained()) {
      auto &first = slots[0];
      verdict(first, selected);
      first.offered = l.bind("logger.offered", l.both(taken, first.accepts));
      first.capture = l.bind("logger.capture", l.both(first.offered, room(first)));
      return;
    }
    for (auto &s : slots) {
      s.offered = l.bind(s.scope() + "offered", l.every(std::array{taken, s.reached, s.accepts}));
      s.capture = l.bind(s.scope() + "capture", l.both(s.offered, room(s)));
    }
  }
  void record_slot(const Slot &s, ValueId taken) {
    const auto valid_next = l.either(s.capture, l.both(s.valid, l.inv(s.clear)));
    const auto alarm_next = l.both(s.valid, s.alarm_enable);
    ValueId alarm;
    if (chained()) {
      b.update(s.status, b.concat(alarm_next, valid_next));
      alarm = l.bind(s.scope() + "alarm", b.slice(s.status, 1, 1));
    } else {
      b.update(s.valid, valid_next);
      alarm = l.state(s.scope() + "alarm", 1);
      b.update(alarm, alarm_next);
    }
    b.output("nl_alarm" + s.suffix + "_out", alarm, "alarm" + s.suffix, "out");
    // A record that passed the filter of a logger that holds one is lost there.
    const auto overflow = l.state(s.scope() + "overflow", 1);
    auto lost = l.both(s.offered, l.inv(room(s)));
    if (s.suffix.empty() && probe_drops())
      lost = l.either(lost, l.bind("logger.probe_lost", l.both(taken, selected.probe_overflow)));
    l.bind(s.scope() + "lost", lost);
    b.update(overflow, l.either(lost, l.both(overflow, l.inv(s.clear_overflow))));
    l.out("field" + s.suffix, "record_held", s.valid);
    l.out("field" + s.suffix, "record_overflow", overflow);
    hold_fields(s);
  }
  // The record's fields of a logger.
  void hold_fields(const Slot &s) {
    const auto face = "field" + s.suffix;
    const auto keep = [&](const char *name, ValueId from) {
      return l.latch("record" + s.suffix + "." + name, from, s.capture);
    };
    const auto route = keep("route", selected.route);
    const auto address = keep("address", selected.address);
    l.out(face, "record_wrap", keep("wrap", selected.wrap));
    l.out(face, "record_write", keep("write", selected.write));
    l.out(face, "record_exclusive",
          p.fixed_length ? l.bit(0) : keep("exclusive", selected.exclusive));
    l.out(face, "record_address_partial", keep("address_partial", selected.partial));
    // The bytes of the access: the offset of its last byte and one.
    l.out(face, "record_bytes",
          keep("bytes", l.inc(l.fit(selected.length, shape.length_bits + 1))));
    if (with_class)
      l.out(face, "record_class", keep("class", selected.error_class));
    // The parts of the route a bank shows, lowest first.
    unsigned at = 0;
    for (const auto &[name, bits] : std::array<std::pair<const char *, unsigned>, 4>{
             {{"record_region_index", shape.region_bits},
              {"record_target_path", shape.target_path_bits},
              {"record_source_path", shape.source_path_bits},
              {"record_sequence", shape.sequence_bits}}}) {
      if (bits != 0)
        l.out(face, name, b.slice(route, at, bits));
      at += bits;
    }
    l.out(face, "record_address_low",
          shape.address_bits > bus_bits ? b.slice(address, 0, bus_bits) : address);
    if (shape.address_bits > bus_bits)
      l.out(face, "record_address_high", b.slice(address, bus_bits, shape.address_bits - bus_bits));
    if (with_audit)
      l.out(face, "record_audit_flags", keep("audit_flags", selected.audit));
    if (with_user)
      user_words(l, face, keep("user_flags", selected.user), shape.user_bits);
  }
  void probe_update(unsigned k, ValueId done) {
    const auto face = "event" + std::to_string(k);
    if (std::ranges::binary_search(related_probes, k))
      probe_of[k].update_related(face, l.both(done, log_edge), l.reset, probe_open);
    else
      probe_of[k].update(face, done);
  }
};
} // namespace

Result<NodeHardware> lower_error_sender(const ErrorLoggerPlan &p, unsigned target) {
  logic::Logic l;
  auto &b = l.b;
  Probe probe(l, p);
  probe.declare("probe.", l.clock, l.reset, l.reset, true);
  l.bindings.erase("probe.pending");
  const auto words = p.serial_words;
  const auto ready = l.in("word", "ready", 1);
  const auto moved = l.bind("sender.moved", l.both(probe.wanting, ready));
  auto count = l.num(1, 0), last = l.bit(1);
  if (words > 1) {
    const auto count_bits = static_cast<unsigned>(std::bit_width(words - 1));
    count = l.state("sender.serial", count_bits);
    last = l.bind("sender.serial_last", l.eq(count, words - 1));
    b.update(count, l.pick(l.both(moved, l.inv(last)), l.inc(count),
                           l.pick(moved, l.num(count_bits, 0), count)));
  }
  // The stored fields, lowest word first; the last word fills up with zero.
  const auto value_of = [&](const std::string &name) {
    const auto &r = probe.held;
    return name == "route"             ? r.route
           : name == "write"           ? r.write
           : name == "wrap"            ? r.wrap
           : name == "last_offset"     ? r.length
           : name == "exclusive"       ? r.exclusive
           : name == "address_partial" ? r.partial
           : name == "address"         ? r.address
           : name == "class"           ? r.error_class
           : name == "user_flags"      ? r.user
           : name == "audit_flags"     ? r.audit
                                       : r.probe_overflow;
  };
  const auto fields = error_record_layout(p).fields;
  auto whole = l.fit(value_of(fields.front().name), fields.front().width);
  for (std::size_t n = 1; n < fields.size(); ++n)
    whole = b.concat(l.fit(value_of(fields[n].name), fields[n].width), whole);
  const auto bits = error_record_word_bits(p), total = l.width(whole);
  auto word = l.num(bits, 0);
  for (unsigned n = words; n-- > 0;) {
    const auto lsb = n * bits;
    const auto piece = lsb >= total          ? l.num(bits, 0)
                       : total - lsb >= bits ? b.slice(whole, lsb, bits)
                                             : b.concat(l.num(bits - (total - lsb), 0),
                                                        b.slice(whole, lsb, total - lsb));
    word = l.pick(l.eq(count, n), piece, word);
  }
  l.out("word", "valid", l.bind("sender.valid", probe.wanting));
  l.out("word", "data", l.bind("sender.word", word));
  l.out("word", "last", last);
  probe.update_sender("event", l.bind("sender.done", l.both(moved, last)));
  return l.finish("nocloom_error_sender_" + p.name + "_" + std::to_string(target));
}

Result<NodeHardware> lower_error_logger(const ErrorLoggerPlan &p,
                                        std::span<const unsigned> related_probes,
                                        bool selected_log_edges) {
  const auto &shape = p.shape;
  if (!valid_name(p.name) || p.targets == 0 ||
      (p.serial_word_bits != 8 && p.serial_word_bits != 16 && p.serial_word_bits != 32) ||
      p.serial_words != error_record_layout(p).words(p.serial_word_bits) || shape.route_bits == 0 ||
      shape.address_bits == 0 || shape.length_bits == 0 ||
      shape.region_bits + shape.target_path_bits + shape.source_path_bits + shape.sequence_bits >
          shape.route_bits)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "error_logger." + p.name, "Bad logger geometry"));
  return Logger(p, related_probes, selected_log_edges).build();
}
} // namespace nocloom::detail
