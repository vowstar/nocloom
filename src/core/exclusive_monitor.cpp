#include "exclusive_monitor.hpp"
#include <numeric>

namespace nocloom::detail {
namespace {
// 2^n as a value of the given width, for a small n operand (0..15).
ValueId power(logic::Logic &l, ValueId n, unsigned width) {
  return l.pow2(n, width, std::min((1U << std::min(4U, l.width(n))) - 1, width - 1));
}
constexpr unsigned lfsr_bits = 5;
} // namespace

ExclusiveRange packet_range(logic::Logic &l, ValueId address, ValueId length, ValueId size,
                            ValueId burst) {
  const unsigned bits = l.width(address) + 1;
  const auto base = l.b.unary(Op::ZeroExtend, address, bits);
  const auto beat = power(l, size, bits);
  // (length + 1) << size: the beats times the beat bytes.
  const auto beats = l.plus(l.b.unary(Op::ZeroExtend, length, bits), l.b.constant(bits, 1));
  auto span = beats;
  for (unsigned k = 1; k < 8; ++k)
    span = l.b.select(l.eq(size, k), l.b.concat(l.b.slice(beats, 0, bits - k), l.b.constant(k, 0)),
                      span);
  const auto container = l.b.binary(
      Op::BitAnd, base,
      l.b.unary(Op::BitNot, l.b.binary(Op::Subtract, span, l.b.constant(bits, 1)), bits));
  const auto fixed = l.eq(burst, 0), wrap = l.eq(burst, 2);
  const auto low = l.b.select(wrap, container, base);
  const auto bytes = l.b.select(fixed, beat, span);
  return {low, l.plus(low, bytes)};
}

ExclusiveMonitor::ExclusiveMonitor(logic::Logic &l, std::string prefix,
                                   const ExclusiveMonitorProfile &p)
    : l_(l), prefix_(std::move(prefix)), p_(p) {
  key_bits_ = static_cast<unsigned>(p.key_flags.size());
  for (unsigned k = 0; k < p.monitors; ++k) {
    const auto at = prefix_ + ".slot" + std::to_string(k) + ".";
    Slot s;
    s.valid = l.state(at + "valid", 1);
    s.key = key_bits_ ? l.state(at + "key", key_bits_) : ValueId{};
    s.base = l.state(at + "base", p.address_bits);
    s.log = l.state(at + "log", 3);
    slots_.push_back(s);
  }
  lfsr_ = l.state(prefix_ + ".lfsr", lfsr_bits);
}

std::vector<std::string> ExclusiveMonitor::state_names(const std::string &prefix,
                                                       const ExclusiveMonitorProfile &p) {
  std::vector<std::string> names;
  const bool keyed = !p.key_flags.empty();
  for (unsigned k = 0; k < p.monitors; ++k) {
    const auto at = prefix + ".slot" + std::to_string(k) + ".";
    names.push_back(at + "valid");
    if (keyed)
      names.push_back(at + "key");
    names.push_back(at + "base");
    names.push_back(at + "log");
  }
  names.push_back(prefix + ".lfsr");
  return names;
}

ValueId ExclusiveMonitor::key(const std::map<std::string, ValueId> &request) const {
  if (p_.key_flags.empty())
    return l_.bit(0);
  auto value = l_.b.slice(request.at("user"), p_.key_flags.front(), 1);
  for (std::size_t k = 1; k < p_.key_flags.size(); ++k)
    value = l_.b.concat(l_.b.slice(request.at("user"), p_.key_flags[k], 1), value);
  return value;
}

std::pair<ValueId, ValueId> ExclusiveMonitor::region(ValueId address, ValueId length,
                                                     ValueId size) const {
  auto log_beats = l_.b.constant(4, 0);
  for (unsigned k = 1; k < 8 && k <= l_.width(length); ++k)
    log_beats = l_.b.select(l_.eq(length, (1U << k) - 1), l_.b.constant(4, k), log_beats);
  const auto log_total = l_.plus(log_beats, l_.b.unary(Op::ZeroExtend, size, 4));
  const auto granule = l_.b.constant(4, static_cast<unsigned>(std::bit_width(p_.granule) - 1));
  const auto log_region = l_.b.slice(
      l_.b.select(l_.b.binary(Op::UnsignedLess, log_total, granule), granule, log_total), 0, 3);
  const auto mask = l_.b.binary(Op::Subtract, power(l_, log_region, p_.address_bits),
                                l_.b.constant(p_.address_bits, 1));
  const auto base = l_.b.binary(Op::BitAnd, address, l_.b.unary(Op::BitNot, mask, p_.address_bits));
  return {base, log_region};
}

ValueId ExclusiveMonitor::holds(ValueId owner, std::pair<ValueId, ValueId> bounds) const {
  auto hit = l_.bit(0);
  for (const auto &s : slots_) {
    auto same = l_.both(s.valid, l_.both(l_.b.binary(Op::Equal, s.base, bounds.first),
                                         l_.b.binary(Op::Equal, s.log, bounds.second)));
    if (key_bits_)
      same = l_.both(same, l_.b.binary(Op::Equal, s.key, owner));
    hit = l_.either(hit, same);
  }
  return hit;
}

ValueId ExclusiveMonitor::overlaps(ExclusiveRange range, std::pair<ValueId, ValueId> bounds) const {
  const unsigned bits = p_.address_bits + 1;
  const auto low = l_.b.unary(Op::ZeroExtend, bounds.first, bits);
  const auto high = l_.plus(low, power(l_, bounds.second, bits));
  return l_.both(l_.b.binary(Op::UnsignedLess, range.low, high),
                 l_.b.binary(Op::UnsignedLess, low, range.high));
}

void ExclusiveMonitor::update(ValueId record, ValueId read_key,
                              std::pair<ValueId, ValueId> read_region, ValueId write,
                              ExclusiveRange write_range) {
  auto &l = l_;
  const auto n = static_cast<unsigned>(slots_.size());
  const unsigned index_bits = std::max(1U, static_cast<unsigned>(std::bit_width(n - 1)));
  // The slot an exclusive read records into: its key's own slot, else the
  // lowest free slot, else the LFSR's pick (value mod n).
  std::vector<ValueId> same, overlap;
  for (const auto &s : slots_) {
    same.push_back(key_bits_ ? l.both(s.valid, l.b.binary(Op::Equal, s.key, read_key)) : s.valid);
    overlap.push_back(l.both(s.valid, l.both(write, overlaps(write_range, {s.base, s.log}))));
  }
  auto random = l.b.constant(index_bits, 0);
  for (unsigned v = (1U << lfsr_bits); v-- > 1;)
    random = l.b.select(l.eq(lfsr_, v), l.b.constant(index_bits, v % n), random);
  auto victim = random;
  for (unsigned k = n; k-- > 0;)
    victim = l.b.select(l.inv(slots_[k].valid), l.b.constant(index_bits, k), victim);
  for (unsigned k = n; k-- > 0;)
    victim = l.b.select(same[k], l.b.constant(index_bits, k), victim);
  victim = l.bind(prefix_ + ".victim", victim);
  for (unsigned k = 0; k < n; ++k) {
    const auto &s = slots_[k];
    const auto fill = l.both(record, l.eq(victim, k));
    l.b.update(s.valid, l.b.select(fill, l.bit(1), l.b.select(overlap[k], l.bit(0), s.valid)));
    if (key_bits_)
      l.b.update(s.key, l.b.select(fill, read_key, s.key));
    l.b.update(s.base, l.b.select(fill, read_region.first, s.base));
    l.b.update(s.log, l.b.select(fill, read_region.second, s.log));
  }
  // XNOR LFSR x^5 + x^3 + 1 from the reset value zero; 0b11111 is never reached.
  const auto feedback = l.b.binary(Op::Equal, l.b.slice(lfsr_, 4, 1), l.b.slice(lfsr_, 2, 1));
  l.b.update(lfsr_, l.b.concat(l.b.slice(lfsr_, 0, lfsr_bits - 1), feedback));
}
} // namespace nocloom::detail
