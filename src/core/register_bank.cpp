#include "register_bank.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "nocloom/generation.hpp"
#include "treadle.hpp"
#include <array>
#include <charconv>
#include <optional>

namespace nocloom::detail {
namespace {
constexpr unsigned word_bits = register_frame_word_bits, key_bits = register_key_bits;
constexpr unsigned index_bits = register_index_bits, bus_bits = 32, strobe_bit = 8;

// A frame is taken at its header, then passed to its tail or served byte by byte.
enum Phase : unsigned { Idle, Pass, Transfer };

Status bad(const std::string &field, const std::string &message) {
  return Status::failure(error("NCL_INTERNAL_PLAN", "register_bank." + field, message));
}
std::uint64_t ones(unsigned bits) { return bits >= 64 ? ~0ULL : (1ULL << bits) - 1; }

// major.minor.patch as one byte each.
std::uint64_t version_bits() {
  std::uint64_t bits = 0;
  auto text = version();
  for (unsigned part = 0; part < 3; ++part) {
    unsigned number = 0;
    const auto [end, code] = std::from_chars(text.data(), text.data() + text.size(), number);
    bits = bits << 8 | (code == std::errc{} ? number & 0xFFU : 0U);
    text.remove_prefix(static_cast<std::size_t>(end - text.data()));
    if (!text.empty() && text.front() == '.')
      text.remove_prefix(1);
  }
  return bits;
}
} // namespace

const char *register_access_name(RegisterAccess access) {
  switch (access) {
  case RegisterAccess::RoFixed:
  case RegisterAccess::Ro:
    return "ro";
  case RegisterAccess::Rw:
    return "rw";
  case RegisterAccess::Rw1c:
    return "rw1c";
  }
  return "unknown";
}

Status check_register_bank(const RegisterBankPlan &plan) {
  if (!derived_name(plan.name))
    return bad("name", "Expected a valid name");
  if (plan.key >> key_bits != 0)
    return bad("key", "A key has 5 bits");
  if (plan.fields.empty())
    return bad("fields", "A bank needs at least one field");
  if (plan.address_bits < 2 || plan.address_bits > index_bits + 2)
    return bad("address_bits", "A bank spans at least a word and at most 16");
  std::map<unsigned, std::uint64_t> used;
  std::set<std::string> names;
  for (const auto &f : plan.fields) {
    if (!valid_name(f.name) || !names.insert(f.name).second)
      return bad(f.name, "Field names must be valid and unique");
    if (f.width == 0 || f.lsb + f.width > bus_bits || f.word >= (1ULL << plan.address_bits) / 4)
      return bad(f.name, "A field must fit one word of the bank");
    if (f.value > ones(f.width))
      return bad(f.name, "The value does not fit the field");
    if ((f.access == RegisterAccess::Ro || f.access == RegisterAccess::Rw1c) && f.value != 0)
      return bad(f.name, "Only fixed and rw fields carry a value");
    const auto bits = ones(f.width) << f.lsb;
    if ((used[f.word] & bits) != 0)
      return bad(f.name, "Fields of a word must not overlap");
    used[f.word] |= bits;
  }
  return Status::success({});
}

RegisterBankPlan error_logger_bank(std::string name, const ErrorRecordShape &shape,
                                   const ErrorLogPlace &place) {
  // One word each: what the bank is, its state, four words of the event, the audit flags and the
  // revision. The user flags fill the second half.
  enum : unsigned {
    Id,
    State = error_log_state_word,
    Address,
    Extent,
    Path,
    Region,
    Audit,
    Revision,
    User
  };
  constexpr unsigned user_words = 8, half = 16;
  RegisterBankPlan plan{std::move(name), place.logger, {}, error_log_bank_address_bits};
  using A = RegisterAccess;
  const auto add = [&plan](const std::string &field, unsigned word, unsigned lsb, unsigned width,
                           A access, std::uint64_t value, const char *description,
                           const char *unit = "") {
    if (width != 0)
      plan.fields.push_back({field, word, lsb, width, access, value, unit, description});
  };
  const auto ro = [&add](const std::string &field, unsigned word, unsigned lsb, unsigned width,
                         const char *description, const char *unit = "") {
    add(field, word, lsb, width, A::Ro, 0, description, unit);
  };
  add("capability_audit_flags", Id, 0, 1, A::RoFixed, shape.audit_bits != 0,
      "The definition declares audit flags");
  add("capability_user_flags", Id, 1, 1, A::RoFixed, shape.user_bits != 0,
      "The network has user flags");
  add("capability_target_user_flags", Id, 2, 1, A::RoFixed, place.target_user_flags,
      "The record of a target error keeps its user flags");
  add("capability_next_logger", Id, 3, 1, A::RoFixed, place.next_logger,
      "Another logger follows in the chain");
  add("capability_given_revision", Id, 4, 1, A::RoFixed, place.given_revision.has_value(),
      "The revision is the definition's error_logging.revision");
  add("logger_index", Id, 8, 5, A::RoFixed, place.logger, "The logger's place in its chain");
  add("log_index", Id, 16, 6, A::RoFixed, place.log, "The log's place in error_logging.logs");
  add("layout", Id, 24, 8, A::RoFixed, error_log_layout, "The layout of this register table");
  add("record_held", State, 0, 1, A::Rw1c, 0,
      "The logger holds a record. A written one clears it. The fields keep their values");
  add("record_overflow", State, 1, 1, A::Rw1c, 0,
      "The logger dropped a record that passed its filter while it held one. A written one "
      "clears it");
  add("alarm_enable", State, 8, 1, A::Rw, 0,
      "The alarm output follows record_held one cycle later while set");
  add("wait_when_full", State, 9, 1, A::Rw, 0,
      "A record that finds the logger full waits on its path until the logger is cleared");
  ro("record_class", State, 4, shape.class_bits, "One bit per class, see error_classes");
  ro("record_write", State, 12, 1, "The access was a write");
  ro("record_wrap", State, 13, 1, "The access was a WRAP burst");
  ro("record_exclusive", State, 14, 1, "An exclusive access, on a decode miss only");
  ro("record_address_partial", State, 15, 1,
     "The address holds only the kept low bits of the access");
  ro("record_sequence", State, half, shape.sequence_bits, "The source AxID");
  ro("record_address_low", Address, 0, std::min(shape.address_bits, bus_bits),
     "The address the record keeps");
  ro("record_address_high", Extent, 0,
     shape.address_bits > bus_bits ? shape.address_bits - bus_bits : 0,
     "The address the record keeps");
  ro("record_bytes", Extent, half, shape.length_bits + 1, "The bytes of the access", "bytes");
  ro("record_source_path", Path, 0, shape.source_path_bits, "The source flow, see route_values");
  ro("record_target_path", Path, half, shape.target_path_bits, "The target flow, see route_values");
  ro("record_region_index", Region, 0, shape.region_bits,
     "The mapping block the access hit, or the gap it missed in");
  ro("record_audit_flags", Audit, 0, shape.audit_bits, "The audit flags, first declared lowest");
  add("revision", Revision, 0, bus_bits, A::RoFixed,
      place.given_revision ? *place.given_revision : version_bits(),
      place.given_revision ? "The definition's error_logging.revision"
                           : "Generator version: major in 23:16, minor in 15:8, patch in 7:0");
  for (unsigned k = 0; k < user_words; ++k) {
    const auto low = k * bus_bits;
    ro("record_user_flags" + std::to_string(k), User + k, 0,
       shape.user_bits > low ? std::min(shape.user_bits - low, bus_bits) : 0,
       "User flags, first declared lowest");
  }
  return plan;
}

std::vector<RegisterField> error_log_layout_fields() {
  // Every part at the width its limit gives: 48 address bits, 32768 bytes, 256 user flags, 32
  // audit flags, four classes, 8192 flows a side, a 16-bit AxID and a 32-bit region count.
  auto fields = error_logger_bank("layout", {74, 48, 256, 32, 15, 4, 32, 13, 13, 16}).fields;
  std::ranges::sort(fields, {}, [](const RegisterField &f) { return std::pair{f.word, f.lsb}; });
  return fields;
}

namespace {
// The parser selects an operation. A byte cursor drives its register access and response.
class Bank {
public:
  Bank(const RegisterBankPlan &plan, bool shared_read) : plan_(plan), shared_read_(shared_read) {}

  Result<NodeHardware> build() {
    parse();
    bus();
    registers();
    answer();
    return l_.finish("nocloom_register_bank_" + plan_.name);
  }

private:
  const RegisterBankPlan &plan_;
  bool shared_read_;
  logic::Logic l_;
  ValueId valid_{}, data_{}, tail_{}, phase_{}, room_{}, took_{}, own_{}, start_{}, address_{};
  ValueId writing_{}, reading_{}, held_{}, out_valid_{}, read_data_{};
  ValueId bit_enable_{}, write_data_{}, command_{}, byte_{};

  ValueId at(unsigned phase) { return l_.eq(phase_, phase); }
  ValueId num(unsigned phase) { return l_.num(2, phase); }

  void parse() {
    auto &b = l_.b;
    valid_ = l_.in("request", "valid", 1);
    data_ = l_.in("request", "data", word_bits);
    tail_ = l_.in("request", "tail", 1);
    const auto out_ready = l_.in("response", "ready", 1);
    phase_ = l_.state("state", 2);
    command_ = shared_read_ ? l_.bit(1) : l_.state("operation.write", 1);
    byte_ = l_.state("operation.byte", 2);
    out_valid_ = l_.state("response.valid", 1);
    writing_ = l_.bind("access.write_active", l_.both(at(Transfer), command_));
    reading_ = l_.bind("access.read_active", l_.both(at(Transfer), l_.inv(command_)));
    held_ = l_.both(out_ready, l_.inv(writing_));
    room_ = l_.bind("response.room", l_.either(l_.inv(out_valid_), held_));
    took_ = l_.both(valid_, room_);
    // The header of a frame for this bank.
    own_ = l_.bind("request.own",
                   l_.both(at(Idle), l_.eq(b.slice(data_, index_bits, key_bits), plan_.key)));
    start_ = l_.both(own_, took_);

    std::vector<ValueId> moves, terms;
    const auto move = [&](ValueId when, ValueId to) {
      moves.push_back(when);
      terms.push_back(b.select(when, to, num(Idle)));
    };
    const auto reading_header = shared_read_ ? l_.both(start_, tail_) : l_.bit(0);
    move(l_.both(start_, l_.inv(reading_header)), num(Transfer));
    move(l_.every(std::array{at(Idle), took_, l_.inv(own_), l_.inv(tail_)}), num(Pass));
    move(l_.every(std::array{at(Pass), took_, tail_}), num(Idle));
    const auto advance = l_.either(l_.both(reading_, room_), l_.both(writing_, valid_));
    move(l_.both(advance, l_.eq(byte_, 0)), num(Idle));
    // A header alone asks for a read; data words after it make a write.
    if (!shared_read_)
      b.update(command_, l_.pick(start_, l_.inv(tail_), command_));
    b.update(byte_,
             l_.pick(start_, l_.num(2, 3), l_.pick(advance, l_.minus(byte_, l_.num(2, 1)), byte_)));
    terms.push_back(b.select(l_.any(moves), num(Idle), phase_));
    b.update(phase_, treadle::join(b, terms));
    l_.out("request", "ready",
           l_.bind("request.ready", b.select(reading_, l_.bit(0), l_.either(writing_, room_))));
  }

  ValueId lane(unsigned index, ValueId enabled) { return l_.both(enabled, l_.eq(byte_, index)); }

  void bus() {
    auto &b = l_.b;
    address_ = l_.state("access.word_index", index_bits);
    b.update(address_, b.select(start_, b.slice(data_, 0, index_bits), address_));
    const auto strobe = l_.both(valid_, b.slice(data_, strobe_bit, 1));
    const auto byte = b.slice(data_, 0, 8);
    const std::array phases{lane(0, writing_), lane(1, writing_), lane(2, writing_),
                            lane(3, writing_)};
    std::optional<ValueId> enable, wide, lanes;
    for (unsigned k = 0; k < 4; ++k) {
      const auto on = l_.both(phases[k], strobe);
      lanes = lanes ? b.concat(phases[k], *lanes) : phases[k];
      auto mask = on;
      for (unsigned n = 1; n < 8; ++n)
        mask = b.concat(on, mask);
      enable = enable ? b.concat(mask, *enable) : mask;
      wide = wide ? b.concat(byte, *wide) : byte;
    }
    l_.bind("bus.lanes", *lanes);
    bit_enable_ = l_.bind("access.write_mask", *enable);
    write_data_ = l_.bind("access.write_value", *wide);
    l_.bind("access.complete", l_.both(l_.eq(byte_, 0), l_.either(l_.both(reading_, room_),
                                                                  l_.both(writing_, valid_))));
  }

  // The fields, and the read multiplexer over the words they name.
  void registers() {
    auto &b = l_.b;
    std::map<unsigned, std::vector<const RegisterField *>> words;
    for (const auto &f : plan_.fields)
      words[f.word].push_back(&f);
    std::vector<ValueId> terms;
    for (auto &[word, fields] : words) {
      const auto index = l_.num(index_bits, word);
      const auto hit = l_.same(address_, index);
      const auto selected = l_.both(hit, writing_);
      std::ranges::sort(fields, {}, &RegisterField::lsb);
      std::optional<ValueId> composed;
      unsigned top = 0;
      const auto append = [&](ValueId part) {
        composed = composed ? b.concat(part, *composed) : part;
      };
      for (const auto *f : fields) {
        if (f->lsb != top)
          append(l_.num(f->lsb - top, 0));
        append(field(*f, selected));
        top = f->lsb + f->width;
      }
      if (top != bus_bits)
        append(l_.num(bus_bits - top, 0));
      const auto read_hit = shared_read_ ? l_.same(b.slice(data_, 0, index_bits), index) : hit;
      terms.push_back(b.select(read_hit, *composed, l_.num(bus_bits, 0)));
    }
    read_data_ = l_.bind("access.read_value", treadle::join(b, terms));
    if (shared_read_) {
      const auto sample = l_.bind("access.read_sample", l_.both(start_, tail_));
      l_.out("read", "sample", sample);
      l_.out("read", "word", b.select(sample, read_data_, l_.num(bus_bits, 0)));
    }
  }

  // The field's read value; a port for what leaves the bank.
  ValueId field(const RegisterField &f, ValueId selected) {
    auto &b = l_.b;
    const auto written = b.slice(write_data_, f.lsb, f.width);
    const auto enabled = b.slice(bit_enable_, f.lsb, f.width);
    switch (f.access) {
    case RegisterAccess::RoFixed:
      return l_.num(f.width, f.value);
    case RegisterAccess::Ro:
      return l_.in("field", f.name, f.width);
    case RegisterAccess::Rw1c: {
      auto ones = b.binary(Op::BitAnd, written, enabled);
      ones = b.select(selected, ones, l_.num(f.width, 0));
      l_.out("field", f.name + "_clear", l_.bind("field." + f.name + "_clear", ones));
      return l_.in("field", f.name, f.width);
    }
    case RegisterAccess::Rw:
      break;
    }
    const auto stored = l_.bind("field." + f.name, b.state(f.width, l_.clock, l_.reset, f.value));
    const auto merged =
        b.binary(Op::BitOr, b.binary(Op::BitAnd, written, enabled),
                 b.binary(Op::BitAnd, stored, b.unary(Op::BitNot, enabled, f.width)));
    b.update(stored, b.select(selected, merged, stored));
    l_.out("field", f.name, stored);
    return stored;
  }

  // The header goes back as it came, a read's bytes after it. A write's header waits in the
  // register for its data words. A word of another frame passes.
  void answer() {
    auto &b = l_.b;
    const auto offered = l_.either(reading_, l_.both(valid_, l_.inv(writing_)));
    const auto passing = l_.inv(l_.either(own_, at(Transfer)));
    const auto last = l_.bind(
        "response.last",
        l_.any(std::array{lane(0, reading_), shared_read_ ? own_ : l_.both(own_, l_.inv(tail_)),
                          l_.both(passing, tail_)}));
    const auto load = l_.both(offered, room_);
    b.update(out_valid_, l_.either(offered, l_.both(out_valid_, l_.inv(held_))));
    const auto out_data = l_.state("response.data", word_bits);
    std::vector<ValueId> sources;
    const auto source = [&](ValueId when, ValueId word) {
      sources.push_back(b.select(when, word, l_.num(word_bits, 0)));
    };
    if (!shared_read_) {
      const auto first_byte = l_.both(lane(3, reading_), room_);
      const auto remainder = l_.latch("response.remainder", b.slice(read_data_, 0, 24), first_byte);
      for (unsigned k = 0; k < 3; ++k)
        source(l_.both(lane(k, reading_), room_),
               b.concat(l_.bit(0), b.slice(remainder, 8 * k, 8)));
      source(first_byte, b.concat(l_.bit(0), b.slice(read_data_, 24, 8)));
    }
    source(l_.both(load, l_.inv(reading_)), data_);
    source(l_.inv(load), out_data);
    b.update(out_data, treadle::join(b, sources));
    const auto out_tail = l_.latch("response.tail", last, load);
    l_.out("response", "valid", l_.bind("response.offer", l_.both(out_valid_, l_.inv(writing_))));
    l_.out("response", "data", out_data);
    l_.out("response", "tail", out_tail);
  }
};
} // namespace

Result<NodeHardware> lower_register_bank(const RegisterBankPlan &plan, bool shared_read) {
  if (auto checked = check_register_bank(plan); !checked)
    return Result<NodeHardware>::failure(checked.diagnostics());
  return Bank(plan, shared_read).build();
}
} // namespace nocloom::detail
