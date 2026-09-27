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
constexpr unsigned word_bits = 10, route_bits = 11, bus_address_bits = 18, bus_bits = 32;
constexpr unsigned command_write = 2, status_request = 1, status_response = 3;
constexpr unsigned key_low_bits = 9, strobe_bit = 9;
constexpr std::uint64_t identity_error_logger = 1;

// Parsing and byte transfer advance independently.
enum Phase : unsigned { Idle, RouteRest, Drain, AddressLow, AddressHigh, Transfer };

Status bad(const std::string &field, const std::string &message) {
  return Status::failure(error("NCL_INTERNAL_PLAN", "register_bank." + field, message));
}
std::uint64_t ones(unsigned bits) { return bits >= 64 ? ~0ULL : (1ULL << bits) - 1; }

// FNV-1a over the table, folded to 24 bits. The identity word itself stays out.
std::uint32_t table_checksum(const std::vector<RegisterField> &fields) {
  std::uint32_t hash = 2166136261U;
  const auto mix = [&hash](std::uint64_t value) {
    for (unsigned k = 0; k < 8; ++k, value >>= 8)
      hash = (hash ^ static_cast<std::uint32_t>(value & 0xFFU)) * 16777619U;
  };
  for (const auto &f : fields) {
    for (const auto c : f.name)
      mix(static_cast<unsigned char>(c));
    for (const std::uint64_t v : {std::uint64_t{f.word}, std::uint64_t{f.lsb},
                                  std::uint64_t{f.width}, static_cast<std::uint64_t>(f.access)})
      mix(v);
  }
  return ((hash >> 24) ^ hash) & 0xFFFFFFU;
}
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
  case RegisterAccess::Constant:
    return "constant";
  case RegisterAccess::Control:
    return "control";
  case RegisterAccess::Status:
    return "status";
  case RegisterAccess::Pulse:
    return "pulse";
  }
  return "unknown";
}

Status check_register_bank(const RegisterBankPlan &plan) {
  if (!valid_name(plan.name))
    return bad("name", "Expected a valid name");
  if (plan.route >> route_bits != 0)
    return bad("route", "A route key has 11 bits");
  if (plan.fields.empty())
    return bad("fields", "A bank needs at least one field");
  if (plan.address_bits < 2 || plan.address_bits > bus_address_bits + 2)
    return bad("address_bits", "A bank spans at least a word and at most its bus");
  std::map<unsigned, std::uint64_t> used;
  std::set<std::string> names;
  for (const auto &f : plan.fields) {
    if (!valid_name(f.name) || !names.insert(f.name).second)
      return bad(f.name, "Field names must be valid and unique");
    if (f.width == 0 || f.lsb + f.width > bus_bits || f.word >> bus_address_bits != 0 ||
        f.word >= (1ULL << plan.address_bits) / 4)
      return bad(f.name, "A field must fit one word of the bank");
    if (f.value > ones(f.width))
      return bad(f.name, "The value does not fit the field");
    if ((f.access == RegisterAccess::Status || f.access == RegisterAccess::Pulse) && f.value != 0)
      return bad(f.name, "Status and pulse fields carry no value");
    const auto bits = ones(f.width) << f.lsb;
    if ((used[f.word] & bits) != 0)
      return bad(f.name, "Fields of a word must not overlap");
    used[f.word] |= bits;
  }
  return Status::success({});
}

RegisterBankPlan error_logger_bank(std::string name, unsigned route, const ErrorRecordShape &shape,
                                   std::optional<std::uint32_t> given_revision) {
  // Register offsets are fixed. An absent field retains its slot.
  // The identity checksum covers all fields except the identity itself.
  constexpr unsigned identity = 0, revision = 1, alarm = 2, hold = 3, status = 4, record_head = 5,
                     record_address = 6, record_route = 8, record_audit = 10, record_user = 11,
                     user_words = 8;
  RegisterBankPlan plan{std::move(name), route, {}, error_log_bank_address_bits};
  using A = RegisterAccess;
  auto &f = plan.fields;
  const auto add = [&f](const std::string &field, unsigned word, unsigned lsb, unsigned width,
                        A access, std::uint64_t value, const char *unit, const char *description) {
    if (width != 0)
      f.push_back({field, word, lsb, width, access, value, unit, description});
  };
  // A value of up to 64 bits spread over two words, its low bits first.
  const auto split = [&](const std::string &field, unsigned word, unsigned width,
                         const char *description) {
    add(field + "_low", word, 0, std::min(width, bus_bits), A::Status, 0, "", description);
    add(field + "_high", word + 1, 0, width > bus_bits ? width - bus_bits : 0, A::Status, 0, "",
        description);
  };
  add("revision_generator", revision, 0, 24, A::Constant,
      given_revision ? *given_revision : version_bits(), "",
      given_revision ? "The definition's error_logging.revision"
                     : "Generator version: major in 23:16, minor in 15:8, patch in 7:0");
  add("revision_reserved", revision, 24, 8, A::Constant, 0, "", "Reads zero");
  add("alarm_enable", alarm, 0, 1, A::Control, 0, "",
      "The alarm output follows record_valid one cycle later while set");
  add("serial_hold_when_full", hold, 0, 1, A::Control, 0, "",
      "A record that finds the logger full waits on the serial path until the logger is cleared");
  add("record_clear_valid", status, 0, 1, A::Pulse, 0, "",
      "Writing one under byte 0's strobe clears record_valid. The fields keep their values");
  add("record_valid", status, 1, 1, A::Status, 0, "", "The logger holds a record");
  // The widest field lowest, each at its own width's alignment, the single bits above them.
  add("record_bytes_minus_one", record_head, 0, shape.length_bits, A::Status, 0, "bytes",
      "The burst's bytes less one");
  add("record_error_code", record_head, 16, shape.code_bits, A::Status, 0, "", "See error_codes");
  add("record_write", record_head, 24, 1, A::Status, 0, "", "The access was a write");
  add("record_exclusive", record_head, 25, 1, A::Status, 0, "",
      "An exclusive access, on a decode miss only");
  add("record_wrap", record_head, 26, 1, A::Status, 0, "", "The access was a WRAP burst");
  add("record_address_partial", record_head, 27, 1, A::Status, 0, "",
      "The address holds only the kept low bits of the access");
  split("record_address", record_address, shape.address_bits, "The address the record keeps");
  split("record_route", record_route, shape.route_bits, "The route word, split into route_fields");
  add("record_audit_flags", record_audit, 0, shape.security_bits, A::Status, 0, "",
      "The audit flags, first declared lowest");
  for (unsigned k = 0; k < user_words; ++k) {
    const auto low = k * bus_bits;
    add("record_user_word" + std::to_string(k), record_user + k, 0,
        shape.user_bits > low ? std::min(shape.user_bits - low, bus_bits) : 0, A::Status, 0, "",
        "User flags, first declared lowest");
  }
  // The identity's words stay out of its own checksum.
  const auto checksum = table_checksum(f);
  add("identity_checksum", identity, 0, 24, A::Constant, checksum, "", "A checksum of this table");
  add("identity_type", identity, 24, 8, A::Constant, identity_error_logger, "",
      "1 for an error logger's bank");
  return plan;
}

namespace {
// The parser selects an operation. A byte cursor drives its register access and response.
class Bank {
public:
  explicit Bank(const RegisterBankPlan &plan) : plan_(plan) {}

  Result<NodeHardware> build() {
    parse();
    bus();
    registers();
    answer();
    return l_.finish("nocloom_register_bank_" + plan_.name);
  }

private:
  const RegisterBankPlan &plan_;
  logic::Logic l_;
  ValueId valid_{}, data_{}, tail_{}, phase_{}, room_{}, took_{}, key_rest_{}, address_{};
  ValueId writing_{}, reading_{}, held_{}, out_valid_{}, read_data_{};
  ValueId bit_enable_{}, write_data_{}, command_{}, byte_{};

  ValueId at(unsigned phase) { return l_.eq(phase_, phase); }
  ValueId num(unsigned phase) { return l_.num(3, phase); }

  void parse() {
    auto &b = l_.b;
    valid_ = l_.in("request", "valid", 1);
    data_ = l_.in("request", "data", word_bits);
    l_.in("request", "head", 1); // framing is carried by the phases
    tail_ = l_.in("request", "tail", 1);
    const auto out_ready = l_.in("response", "ready", 1);
    phase_ = l_.state("state", 3);
    command_ = l_.state("operation.write", 1);
    byte_ = l_.state("operation.byte", 2);
    out_valid_ = l_.state("response.valid", 1);
    writing_ = l_.bind("access.write_active", l_.both(at(Transfer), command_));
    reading_ = l_.bind("access.read_active", l_.both(at(Transfer), l_.inv(command_)));
    held_ = l_.both(out_ready, l_.inv(writing_));
    room_ = l_.bind("response.room", l_.either(l_.inv(out_valid_), held_));
    took_ = l_.both(valid_, room_);
    const auto key_first = l_.eq(b.slice(data_, 0, key_low_bits), plan_.route & 0x1FFU);
    key_rest_ = l_.both(l_.eq(b.slice(data_, 4, 2), plan_.route >> key_low_bits),
                        l_.eq(b.slice(data_, 0, 2), status_request));
    const auto is_write = l_.eq(b.slice(data_, 6, 4), command_write);

    std::vector<ValueId> moves, terms;
    const auto move = [&](ValueId when, ValueId to) {
      moves.push_back(when);
      terms.push_back(b.select(when, to, num(Idle)));
    };
    const auto step = [&](unsigned from, unsigned to, ValueId moved) {
      move(l_.both(at(from), moved), num(to));
    };
    move(l_.both(at(Idle), took_), l_.pick(key_first, num(RouteRest), num(Drain)));
    move(l_.both(at(RouteRest), took_), l_.pick(key_rest_, num(AddressLow), num(Drain)));
    move(l_.every(std::array{at(Drain), took_, tail_}), num(Idle));
    step(AddressLow, AddressHigh, took_);
    step(AddressHigh, Transfer, took_);
    const auto advance = l_.either(l_.both(reading_, room_), l_.both(writing_, valid_));
    move(l_.both(advance, l_.eq(byte_, 0)), num(Idle));
    b.update(command_, l_.pick(l_.both(at(RouteRest), took_), is_write, command_));
    b.update(byte_, l_.pick(l_.both(at(AddressHigh), took_), l_.num(2, 3),
                            l_.pick(advance, l_.minus(byte_, l_.num(2, 1)), byte_)));
    terms.push_back(b.select(l_.any(moves), num(Idle), phase_));
    b.update(phase_, treadle::join(b, terms));
    l_.out("request", "ready",
           l_.bind("request.ready", b.select(reading_, l_.bit(0), l_.either(writing_, room_))));
  }

  ValueId lane(unsigned index, ValueId enabled) { return l_.both(enabled, l_.eq(byte_, index)); }

  void bus() {
    auto &b = l_.b;
    const auto high = l_.state("bus.address_high", word_bits);
    const auto low = l_.state("bus.address_low", bus_address_bits - word_bits);
    address_ = l_.bind("access.word_index", b.concat(high, low));
    b.update(low, b.select(l_.both(at(AddressLow), took_),
                           b.slice(data_, 2, bus_address_bits - word_bits), low));
    b.update(high, b.select(l_.both(at(AddressHigh), took_), data_, high));
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
      const auto hit = l_.same(address_, l_.num(bus_address_bits, word));
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
      terms.push_back(b.select(hit, *composed, l_.num(bus_bits, 0)));
    }
    read_data_ = l_.bind("access.read_value", treadle::join(b, terms));
  }

  // The field's read value; a port for what leaves the bank.
  ValueId field(const RegisterField &f, ValueId selected) {
    auto &b = l_.b;
    const auto written = b.slice(write_data_, f.lsb, f.width);
    const auto enabled = b.slice(bit_enable_, f.lsb, f.width);
    switch (f.access) {
    case RegisterAccess::Constant:
      return l_.num(f.width, f.value);
    case RegisterAccess::Status:
      return l_.in("field", f.name, f.width);
    case RegisterAccess::Pulse: {
      auto pulse = b.binary(Op::BitAnd, written, enabled);
      pulse = b.select(selected, pulse, l_.num(f.width, 0));
      l_.out("field", f.name, l_.bind("field." + f.name, pulse));
      return l_.num(f.width, 0);
    }
    case RegisterAccess::Control:
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

  void answer() {
    auto &b = l_.b;
    const auto header = l_.bind(
        "response.header", b.concat(b.slice(data_, 2, word_bits - 2), l_.num(2, status_response)));
    const auto start = l_.both(at(RouteRest), key_rest_);
    const auto offered = l_.either(reading_, l_.both(valid_, l_.inv(writing_)));
    const auto last = l_.any(std::array{lane(0, reading_), l_.both(at(AddressHigh), command_),
                                        l_.both(at(Drain), tail_)});
    const auto load = l_.both(offered, room_);
    b.update(out_valid_, l_.either(offered, l_.both(out_valid_, l_.inv(held_))));
    const auto out_data = l_.state("response.data", word_bits);
    const auto fresh = l_.both(load, l_.inv(reading_));
    std::vector<ValueId> sources;
    const auto source = [&](ValueId when, ValueId word) {
      sources.push_back(b.select(when, word, l_.num(word_bits, 0)));
    };
    const std::array read_phases{lane(0, reading_), lane(1, reading_), lane(2, reading_),
                                 lane(3, reading_)};
    for (unsigned k = 0; k < 4; ++k)
      source(l_.both(read_phases[k], room_), b.concat(l_.num(2, 2), b.slice(read_data_, 8 * k, 8)));
    source(l_.both(fresh, start), header);
    source(l_.both(fresh, l_.inv(start)), data_);
    source(l_.inv(load), out_data);
    b.update(out_data, treadle::join(b, sources));
    const auto out_head = l_.latch("response.head", at(Idle), load);
    const auto out_tail = l_.latch("response.tail", last, load);
    l_.out("response", "valid", l_.bind("response.offer", l_.both(out_valid_, l_.inv(writing_))));
    l_.out("response", "data", out_data);
    l_.out("response", "head", out_head);
    l_.out("response", "tail", out_tail);
  }
};
} // namespace

Result<NodeHardware> lower_register_bank(const RegisterBankPlan &plan) {
  if (auto checked = check_register_bank(plan); !checked)
    return Result<NodeHardware>::failure(checked.diagnostics());
  return Bank(plan).build();
}
} // namespace nocloom::detail
