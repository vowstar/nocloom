#pragma once
#include "logic.hpp"
#include <algorithm>
#include <bit>
#include <iterator>
#include <nocloom/definition.hpp>
#include <numeric>
#include <optional>
#include <vector>

namespace nocloom::detail::mapping_decode {
// Whether mappings are disjoint in base order inside the transport space (`bits` wide) with every
// local interval inside the port (`local_bits` wide): whole 4 KiB pages, or with a sub-page
// minimum a power of two from it below 4 KiB, base and local address aligned to it.
inline bool valid(const std::vector<AddressMapping> &mappings, unsigned bits, unsigned local_bits,
                  std::optional<std::uint64_t> subpage) {
  const auto space = std::uint64_t{1} << bits, local = std::uint64_t{1} << local_bits;
  for (std::size_t k = 0; k < mappings.size(); ++k) {
    const auto &m = mappings[k];
    const bool page = m.size % 4096 == 0;
    const auto align = page ? 4096 : m.size;
    if (!m.size || m.base >= space || m.size > space - m.base || m.base % align ||
        m.local_address % align || m.local_address >= local || m.size > local - m.local_address ||
        (!page && (!subpage || m.size < *subpage || !std::has_single_bit(m.size))) ||
        (k && mappings[k - 1].base + mappings[k - 1].size > m.base))
      return false;
  }
  return true;
}
// Whether an arrival block (AddressBlock) is at least a page, aligned inside the transport space
// and holds every mapping; a block without keep bits always holds.
inline bool holds(const AddressBlock &block, const std::vector<AddressMapping> &mappings,
                  unsigned bits) {
  if (!block.keep)
    return true;
  const auto size = std::uint64_t{1} << block.keep;
  const auto inside = [&](const AddressMapping &m) {
    return m.base >= block.base && m.base - block.base + m.size <= size;
  };
  return block.keep >= 12 && block.keep < bits && block.base % size == 0 &&
         block.base < (std::uint64_t{1} << bits) && std::ranges::all_of(mappings, inside);
}
// The decode of a target's address mappings (definition.hpp) at its outlet, over addresses of
// `bits` bits; mappings in base order, never overlapping. Inside an arrival block (Logic::block)
// the offset takes only the block's low bits.
inline ValueId hit(logic::Logic &l, ValueId address, unsigned bits, const AddressMapping &m) {
  if (const auto k = l.block.keep; l.narrows(bits) && m.size < std::uint64_t{1} << k) {
    const auto low = l.b.slice(address, 0, k);
    const auto offset = l.b.binary(Op::Subtract, low, l.b.constant(k, m.base - l.block.base));
    return l.b.binary(Op::UnsignedLess, offset, l.b.constant(k, m.size));
  }
  const auto offset = l.b.binary(Op::Subtract, address, l.b.constant(bits, m.base));
  return l.b.binary(Op::UnsignedLess, offset, l.b.constant(bits, m.size));
}
inline ValueId any(logic::Logic &l, ValueId address, unsigned bits,
                   const std::vector<AddressMapping> &mappings) {
  return std::accumulate(std::next(mappings.begin()), mappings.end(),
                         hit(l, address, bits, mappings.front()),
                         [&](ValueId out, const AddressMapping &m) {
                           return l.either(out, hit(l, address, bits, m));
                         });
}
// The local address: address - base + local_address of the mapping holding it, else of the last.
// With kept, its low kept bits alone.
inline ValueId local(logic::Logic &l, ValueId address, unsigned bits,
                     const std::vector<AddressMapping> &mappings, unsigned kept = 0) {
  const auto width = kept ? kept : bits;
  const auto mask = (std::uint64_t{1} << width) - 1;
  const auto low = kept ? l.b.slice(address, 0, kept) : address;
  auto shift = [&](const AddressMapping &m) {
    return l.b.binary(Op::Subtract, low, l.b.constant(width, (m.base - m.local_address) & mask));
  };
  auto out = shift(mappings.back());
  for (auto k = mappings.size() - 1; k-- > 0;)
    out = l.pick(hit(l, address, bits, mappings[k]), shift(mappings[k]), out);
  return out;
}
// The target flow (AddressMapping::flow, `width` bits) of the mapping holding address, else of the
// last.
inline ValueId flow(logic::Logic &l, ValueId address, unsigned bits,
                    const std::vector<AddressMapping> &mappings, unsigned width) {
  auto out = l.b.constant(width, mappings.back().flow);
  for (auto k = mappings.size() - 1; k-- > 0;)
    out = l.pick(hit(l, address, bits, mappings[k]), l.b.constant(width, mappings[k].flow), out);
  return out;
}
// The end of the first mapping that ends above address (`wide` bits, one more than the address
// space), else of the last.
inline ValueId end(logic::Logic &l, ValueId address, unsigned wide,
                   const std::vector<AddressMapping> &mappings) {
  auto out = l.b.constant(wide, mappings.back().base + mappings.back().size);
  for (auto k = mappings.size() - 1; k-- > 0;) {
    const auto e = l.b.constant(wide, mappings[k].base + mappings[k].size);
    out = l.pick(l.below(address, e), e, out);
  }
  return out;
}
} // namespace nocloom::detail::mapping_decode
