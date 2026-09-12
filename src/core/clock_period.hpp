#pragma once
#include <compare>
#include <cstdint>
#include <numeric>
#include <string>
#include <utility>

namespace nocloom::detail {
// The largest declared period times a full divider product fits in 128 bits.
// An effective period times a transport word fits even with a 64-bit denominator.
class ClockPeriod {
public:
  using Numerator = __uint128_t;

  explicit ClockPeriod(Numerator numerator, std::uint64_t denominator = 1) {
    const auto divisor = std::gcd(static_cast<std::uint64_t>(numerator % denominator), denominator);
    numerator_ = numerator / divisor;
    denominator_ = denominator / divisor;
  }

  auto whole() const { return numerator_ / denominator_; }
  auto remainder() const { return static_cast<std::uint64_t>(numerator_ % denominator_); }
  auto denominator() const { return denominator_; }

  bool operator==(const ClockPeriod &) const = default;
  std::strong_ordering operator<=>(const ClockPeriod &other) const {
    const auto order = whole() <=> other.whole();
    if (order != 0)
      return order;
    return Numerator{remainder()} * other.denominator_ <=>
           Numerator{other.remainder()} * denominator_;
  }

  ClockPeriod operator*(std::uint64_t factor) const {
    return ClockPeriod{numerator_ * factor, denominator_};
  }

  std::string text() const {
    if (denominator_ == 1)
      return digits(numerator_);
    return digits(numerator_) + "/" + std::to_string(denominator_);
  }

  // Truncate to a decimal in units of picoseconds_per_unit picoseconds.
  std::string decimal(unsigned picoseconds_per_unit, unsigned places) const {
    const Numerator unit = Numerator{denominator_} * picoseconds_per_unit;
    auto out = digits(numerator_ / unit);
    if (places != 0)
      out += '.';
    auto rest = numerator_ % unit;
    for (unsigned i = 0; i < places; ++i) {
      rest *= 10;
      out += static_cast<char>('0' + rest / unit);
      rest %= unit;
    }
    return out;
  }

private:
  static std::string digits(Numerator value) {
    std::string out;
    do {
      out.insert(out.begin(), static_cast<char>('0' + value % 10));
      value /= 10;
    } while (value != 0);
    return out;
  }

  Numerator numerator_;
  std::uint64_t denominator_;
};

// ceil(rounds * (Ta + Tb) / max(Ta, Tb)), with positive periods and small rounds.
inline unsigned clock_round_trip(unsigned rounds, ClockPeriod faster, ClockPeriod slower) {
  if (faster > slower)
    std::swap(faster, slower);
  const auto return_time = faster * rounds;
  unsigned extra = 1;
  while (slower * extra < return_time)
    ++extra;
  return rounds + extra;
}
} // namespace nocloom::detail
