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
#include <string>
#include <utility>

namespace nocloom::detail {
// The stored fields of a record as a target's probe holds them, in crossing order.
RecordLayout error_record_layout(const ErrorLoggerPlan &p) {
  const auto &s = p.shape;
  RecordLayout layout;
  for (const auto &[name, width] : std::array<std::pair<const char *, unsigned>, 10>{
           {{"route", s.route_bits},
            {"write", 1},
            {"wrap", 1},
            {"length", p.fixed_length ? 0U : s.length_bits},
            {"lock", p.fixed_length ? 0U : 1U},
            {"partial", 1},
            {"address", s.address_bits},
            {"code", s.code_bits},
            {"user", s.user_bits},
            {"security", s.security_bits}}})
    if (width) {
      layout.fields.push_back({name, width, layout.bits});
      layout.bits += width;
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
    l.out(face,
          k == 0   ? std::string("record_user_word0")
          : k == 1 ? std::string("record_user_word1")
                   : "record_user_word" + std::to_string(k),
          l.b.slice(user, lsb, std::min(bus_bits, bits - lsb)));
  }
}
// A logger of a chain: its filter in one form, its controls and what the chain settles for it.
struct Slot {
  std::string suffix;
  const std::vector<unsigned> *ignored = nullptr;
  const std::vector<RecordFilterBit> *user_filter = nullptr, *security_filter = nullptr;
  bool pass_always = false;
  ValueId clear{}, alarm_enable{}, serial_hold_when_full{}, valid{}, hold{}, accepts{}, offered{};
  ValueId capture{}, dropped{}, reached{}, status{};
  std::string scope() const { return "logger" + suffix + "."; }
};
struct Record {
  ValueId route, write, wrap, length, lock, partial, address, code, user, security;
};
// Packet tracking and event ownership have independent flags.
class Probe {
public:
  Probe(logic::Logic &logic, const ErrorLoggerPlan &plan) : l(logic), p(plan) {}
  ValueId tracking{}, wanting{};
  Record held{};
  void declare(const std::string &probe) {
    const auto &shape = p.shape;
    const bool with_code = shape.code_bits != 0;
    const auto stored_length = [&](const std::string &name) {
      return p.fixed_length ? l.num(shape.length_bits, p.fixed_length)
                            : l.state(name, shape.length_bits);
    };
    tracking = l.state(probe + "tracking", 1);
    wanting = l.state(probe + "pending", 1);
    held = {l.state(probe + "route", shape.route_bits), l.state(probe + "write", 1),
            l.state(probe + "wrap", 1), stored_length(probe + "length"),
            p.fixed_length ? l.bit(0) : l.state(probe + "lock", 1), l.state(probe + "partial", 1),
            l.state(probe + "address", shape.address_bits),
            // A filter takes a network without codes for one whose failures are the target's.
            with_code ? l.state(probe + "code", std::max(shape.code_bits, 3U))
                      : l.num(std::max(shape.code_bits, 3U), CodeTarget),
            shape.user_bits != 0 ? l.state(probe + "user", shape.user_bits) : ValueId{},
            shape.security_bits != 0 ? l.state(probe + "security", shape.security_bits)
                                     : ValueId{}};
  }
  // The probe's next state: done says the record has left.
  void update(const std::string &face, ValueId done) {
    const auto &shape = p.shape;
    Builder &b = l.b;
    const auto moved = l.in(face, "moved", 1), head = l.in(face, "head", 1);
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
    b.update(wanting, l.either(new_fault, l.both(hold, l.inv(done))));
    b.update(tracking, l.either(l.both(available, good_head),
                                l.both(word, l.inv(l.both(moved, l.either(failed, tail))))));
    const auto sample = l.both(at_head, available);
    const auto keep = [&](ValueId state, const char *signal, unsigned bits) {
      b.update(state, l.pick(sample, l.in(face, signal, bits), state));
    };
    keep(held.route, "route", shape.route_bits);
    keep(held.write, "write", 1);
    keep(held.wrap, "wrap", 1);
    if (!p.fixed_length) {
      keep(held.length, "length", shape.length_bits);
      keep(held.lock, "lock", 1);
    }
    keep(held.partial, "partial", 1);
    keep(held.address, "address", shape.address_bits);
    if (shape.code_bits != 0)
      b.update(held.code, l.pick(l.either(sample, new_fault),
                                 l.in(face, "code", std::max(shape.code_bits, 3U)), held.code));
    if (shape.user_bits != 0)
      keep(held.user, "user", shape.user_bits);
    if (shape.security_bits != 0)
      keep(held.security, "security", shape.security_bits);
  }

private:
  logic::Logic &l;
  const ErrorLoggerPlan &p;
};

class Logger {
public:
  explicit Logger(const ErrorLoggerPlan &plan)
      : p(plan), shape(plan.shape), with_code(shape.code_bits != 0),
        code_bits(std::max(shape.code_bits, 3U)), with_user(shape.user_bits != 0),
        with_security(shape.security_bits != 0), frame_words(plan.crossed ? plan.serial_words : 1),
        count_bits(std::max(1U, static_cast<unsigned>(std::bit_width(frame_words - 1)))) {}

  Result<NodeHardware> build() {
    controls();
    probes();
    served = wanting;
    selected = held[0];
    moving = wanting[0];
    slots[0].hold = l.bind("logger.hold", l.both(slots[0].serial_hold_when_full, slots[0].valid));
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
      record_slot(s);
    if (p.crossed)
      word_faces();
    else
      for (unsigned k = 0; k < p.targets; ++k)
        probe_update(k, l.both(taken, served[k]));
    return l.finish("nocloom_error_logger_" + p.name);
  }

private:
  const ErrorLoggerPlan &p;
  const ErrorRecordShape &shape;
  const bool with_code;
  const unsigned code_bits;
  const bool with_user, with_security;
  const unsigned frame_words, count_bits;
  logic::Logic l;
  Builder &b = l.b;
  std::vector<Slot> slots;
  ValueId count{}, last{}, ready{}, moving{};
  std::vector<Record> held;
  std::vector<ValueId> wanting, served, gather, word_data, word_last;
  std::vector<Probe> probe_of;
  Record selected{};

  bool chained() const { return !p.chain.empty(); }

  void control(Slot &s) {
    const auto face = "control" + s.suffix;
    s.clear = l.bind(s.scope() + "clear", l.in(face, "record_clear_valid", 1));
    s.alarm_enable = l.in(face, "alarm_enable", 1);
    s.serial_hold_when_full = l.in(face, "serial_hold_when_full", 1);
  }
  void controls() {
    slots.push_back({"", &p.ignored_codes, &p.user_filter, &p.security_filter, p.pass_always});
    for (std::size_t n = 0; n < p.chain.size(); ++n) {
      const auto &next = p.chain[n];
      slots.push_back({std::to_string(n + 1), &next.ignored_codes, &next.user_filter,
                       &next.security_filter, next.pass_always});
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
    for (unsigned k = 0; k < p.targets; ++k) {
      if (p.crossed) {
        words(k);
        continue;
      }
      auto &probe = probe_of.emplace_back(l, p);
      probe.declare("probe" + std::to_string(k) + ".");
      wanting.push_back(probe.wanting);
      held.push_back(probe.held);
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
                    named("length", l.num(shape.length_bits, p.fixed_length)),
                    named("lock", l.bit(0)), by_name.at("partial"), by_name.at("address"),
                    named("code", l.num(code_bits, CodeTarget)), named("user", ValueId{}),
                    named("security", ValueId{})});
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
    std::vector<ValueId> ignored;
    std::ranges::transform(*s.ignored, std::back_inserter(ignored),
                           [&](unsigned code) { return l.eq(from.code, code); });
    std::vector<ValueId> filtered;
    if (with_user)
      filter_terms(l, from.user, *s.user_filter, filtered);
    if (with_security)
      filter_terms(l, from.security, *s.security_filter, filtered);
    auto wanted = l.inv(l.any(ignored));
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
        s.hold = l.bind(s.scope() + "hold", l.both(s.serial_hold_when_full, s.valid));
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
                  zero(selected.lock),
                  zero(selected.partial),
                  zero(selected.address),
                  zero(selected.code),
                  with_user ? zero(selected.user) : ValueId{},
                  with_security ? zero(selected.security) : ValueId{}};
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
                                  add(joined.lock, held[k].lock);
                                  add(joined.partial, held[k].partial);
                                  add(joined.address, held[k].address);
                                  add(joined.code, held[k].code);
                                  if (with_user)
                                    add(joined.user, held[k].user);
                                  if (with_security)
                                    add(joined.security, held[k].security);
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
  void captures(ValueId taken) {
    if (!chained()) {
      auto &first = slots[0];
      verdict(first, selected);
      first.offered = l.bind("logger.offered", l.both(taken, first.accepts));
      first.capture = l.bind("logger.capture", l.both(first.offered, l.inv(first.valid)));
      return;
    }
    for (auto &s : slots) {
      s.offered = l.bind(s.scope() + "offered", l.every(std::array{taken, s.reached, s.accepts}));
      s.capture = l.bind(s.scope() + "capture", l.both(s.offered, l.inv(s.valid)));
    }
  }
  void record_slot(const Slot &s) {
    const auto valid_next = l.pick(s.clear, l.bit(0), l.either(s.valid, s.capture));
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
    l.out("field" + s.suffix, "record_valid", s.valid);
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
    l.out(face, "record_exclusive", p.fixed_length ? l.bit(0) : keep("lock", selected.lock));
    l.out(face, "record_address_partial", keep("partial", selected.partial));
    l.out(face, "record_bytes_minus_one", keep("length", selected.length));
    if (with_code)
      l.out(face, "record_error_code", keep("code", b.slice(selected.code, 0, shape.code_bits)));
    const auto split = [&](const std::string &name, ValueId value, unsigned bits) {
      l.out(face, name + "_low", bits > bus_bits ? b.slice(value, 0, bus_bits) : value);
      if (bits > bus_bits)
        l.out(face, name + "_high", b.slice(value, bus_bits, bits - bus_bits));
    };
    split("record_route", route, shape.route_bits);
    split("record_address", address, shape.address_bits);
    if (with_security)
      l.out(face, "record_audit_flags", keep("security", selected.security));
    if (with_user)
      user_words(l, face, keep("user", selected.user), shape.user_bits);
  }
  void probe_update(unsigned k, ValueId done) {
    probe_of[k].update("event" + std::to_string(k), done);
  }
};
} // namespace

Result<NodeHardware> lower_error_sender(const ErrorLoggerPlan &p, unsigned target) {
  logic::Logic l;
  auto &b = l.b;
  Probe probe(l, p);
  probe.declare("probe.");
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
    return name == "route"     ? r.route
           : name == "write"   ? r.write
           : name == "wrap"    ? r.wrap
           : name == "length"  ? r.length
           : name == "lock"    ? r.lock
           : name == "partial" ? r.partial
           : name == "address" ? r.address
           : name == "code"    ? r.code
           : name == "user"    ? r.user
                               : r.security;
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
  probe.update("event", l.bind("sender.done", l.both(moved, last)));
  return l.finish("nocloom_error_sender_" + p.name + "_" + std::to_string(target));
}

Result<NodeHardware> lower_error_logger(const ErrorLoggerPlan &p) {
  const auto &shape = p.shape;
  if (!valid_name(p.name) || p.targets == 0 ||
      (p.serial_word_bits != 8 && p.serial_word_bits != 16 && p.serial_word_bits != 32) ||
      p.serial_words != error_record_layout(p).words(p.serial_word_bits) || shape.route_bits == 0 ||
      shape.address_bits == 0 || shape.length_bits == 0)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "error_logger." + p.name, "Bad logger geometry"));
  return Logger(p).build();
}
} // namespace nocloom::detail
