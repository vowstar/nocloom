#pragma once
#include "core/clock_period.hpp"
#include <limits>
#include <nlohmann/json.hpp>
#include <optional>
#include <stdexcept>

namespace nocloom::detail {
inline nlohmann::json write_clock_period(ClockPeriod period) {
  const auto whole = period.whole() > std::numeric_limits<std::uint64_t>::max()
                         ? nlohmann::json(ClockPeriod{period.whole()}.text())
                         : nlohmann::json(static_cast<std::uint64_t>(period.whole()));
  if (period.denominator() == 1)
    return whole;
  return {
      {"whole", whole}, {"numerator", period.remainder()}, {"denominator", period.denominator()}};
}

inline ClockPeriod::Numerator read_period_whole(const nlohmann::json &value) {
  if (!value.is_string())
    return value.get<std::uint64_t>();
  const auto &digits = value.get_ref<const std::string &>();
  ClockPeriod::Numerator whole = 0;
  constexpr auto limit = ~ClockPeriod::Numerator{0};
  if (digits.empty())
    throw std::runtime_error("Empty clock period");
  for (const auto c : digits) {
    const auto digit = static_cast<unsigned>(c - '0');
    if (digit > 9 || whole > (limit - digit) / 10)
      throw std::runtime_error("Invalid whole clock period");
    whole = whole * 10 + digit;
  }
  return whole;
}

inline std::optional<ClockPeriod> read_clock_period(const nlohmann::json &clock) {
  const auto period = clock.find("period_ps");
  if (period == clock.end())
    return std::nullopt;
  if (!period->is_object())
    return ClockPeriod{read_period_whole(*period)};
  const auto whole = read_period_whole(period->at("whole"));
  const auto numerator = period->at("numerator").get<std::uint64_t>();
  const auto denominator = period->at("denominator").get<std::uint64_t>();
  constexpr auto limit = ~ClockPeriod::Numerator{0};
  if (denominator == 0 || whole > (limit - numerator) / denominator)
    throw std::runtime_error("Invalid fractional clock period");
  return ClockPeriod{whole * denominator + numerator, denominator};
}
} // namespace nocloom::detail
