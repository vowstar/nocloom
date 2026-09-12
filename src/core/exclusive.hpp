#pragma once
#include <nocloom/definition.hpp>
#include <vector>

namespace nocloom::detail {
// Resolved exclusive access support of one target (schema 10). monitors and
// granule apply to ExclusiveAccess::Network; they are zero and one otherwise.
struct ExclusiveTarget {
  ExclusiveAccess mode = ExclusiveAccess::None;
  unsigned monitors = 0;
  unsigned granule = 1;
  // Bits of the network user field naming the owner, the low bit of the value first
  // (Target::exclusive_id_flags).
  std::vector<unsigned> id_flags{};
  bool operator==(const ExclusiveTarget &) const = default;
};
} // namespace nocloom::detail
