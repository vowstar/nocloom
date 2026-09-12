#pragma once
#include "hardware_builder.hpp"
#include <algorithm>
#include <nocloom/definition.hpp>
#include <span>
#include <vector>

namespace nocloom::detail {
// A total numeric partition. path is the declaration index of a route class.
struct IdClassInterval {
  unsigned first = 0, last = 0, path = 0;
  bool operator==(const IdClassInterval &) const = default;
};
// Called after validation. Sweep endpoints to merge duplicates and fill default holes without
// expanding the table into individual IDs.
inline std::vector<IdClassInterval> class_partition(const Initiator &source) {
  if (source.route_classes.empty())
    return {};
  const auto limit = 1U << source.id_width.value_or(4);
  const auto index = [&](const std::string &name) {
    return unsigned(std::ranges::find(source.route_classes, name) - source.route_classes.begin());
  };
  auto ranges = source.id_ranges;
  std::ranges::sort(ranges, {}, &IdRange::first);
  std::vector<IdClassInterval> result;
  const auto append = [&](unsigned first, unsigned last, unsigned path) {
    if (!result.empty() && result.back().path == path)
      result.back().last = last;
    else
      result.push_back({first, last, path});
  };
  unsigned cursor = 0;
  const auto fallback = index(*source.default_class);
  for (const auto &range : ranges) {
    if (range.first > cursor)
      append(cursor, range.first - 1, fallback);
    if (range.last < cursor)
      continue;
    append(std::max(cursor, range.first), range.last, index(range.route_class));
    cursor = range.last + 1;
  }
  if (cursor < limit)
    append(cursor, limit - 1, fallback);
  return result;
}
// Balanced boundary decisions over a total partition. Values can project a class into a route,
// a target ID constant or an admission bit. Constant subtrees collapse before RTL construction.
inline ValueId class_value(Builder &b, ValueId id, std::span<const IdClassInterval> ranges,
                           std::span<const ValueId> values) {
  const auto same = [&](ValueId a, ValueId c) {
    const auto &x = b.ir.values.at(a.index), &y = b.ir.values.at(c.index);
    return a.index == c.index || (x.op == Op::Constant && y.op == Op::Constant &&
                                  x.width == y.width && x.literal == y.literal);
  };
  const auto first = values[ranges.front().path];
  if (std::ranges::all_of(ranges, [&](const auto &r) { return same(values[r.path], first); }))
    return first;
  const auto half = ranges.size() / 2;
  const auto left = class_value(b, id, ranges.first(half), values);
  const auto right = class_value(b, id, ranges.subspan(half), values);
  if (same(left, right))
    return left;
  const auto width = b.ir.values.at(id.index).width;
  return b.select(b.binary(Op::UnsignedLess, id, b.constant(width, ranges[half].first)), left,
                  right);
}
} // namespace nocloom::detail
