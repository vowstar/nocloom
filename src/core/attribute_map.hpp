#pragma once
#include "logic.hpp"
#include <array>
#include <nocloom/definition.hpp>
#include <optional>

namespace nocloom::detail {
// Attribute bits of a burst: bit i follows bits[i], a constant or a flag of the
// burst's AxCACHE, AxPROT and direction (write is 1 for writes), or bit index of
// user (an initiator's AxUSER, or the network user flags at a target).
template <class Bits>
ValueId map_attributes(logic::Logic &l, const Bits &bits, ValueId cache, ValueId prot,
                       ValueId write, std::optional<ValueId> user = std::nullopt) {
  auto flag = [&](const AttributeBit &bit) {
    ValueId value;
    switch (bit.source) {
    case AttributeSource::One:
      value = l.b.constant(1, 1);
      break;
    case AttributeSource::Bufferable:
      value = l.b.slice(cache, 0, 1);
      break;
    case AttributeSource::Modifiable:
      value = l.b.slice(cache, 1, 1);
      break;
    case AttributeSource::Allocate:
      value = l.b.select(write, l.b.slice(cache, 3, 1), l.b.slice(cache, 2, 1));
      break;
    case AttributeSource::Lookup: {
      const auto read_allocate = l.b.slice(cache, 2, 1);
      value = l.either(read_allocate, l.b.slice(cache, 3, 1));
      break;
    }
    case AttributeSource::Privileged:
      value = l.b.slice(prot, 0, 1);
      break;
    case AttributeSource::Nonsecure:
      value = l.b.slice(prot, 1, 1);
      break;
    case AttributeSource::Instruction:
      value = l.b.slice(prot, 2, 1);
      break;
    case AttributeSource::User:
      value = l.b.slice(user.value(), bit.index, 1);
      break;
    case AttributeSource::Zero:
    default:
      value = l.b.constant(1, 0);
      break;
    }
    return bit.invert ? l.inv(value) : value;
  };
  ValueId result = flag(bits[bits.size() - 1]);
  for (std::size_t i = bits.size() - 1; i-- > 0;) {
    const auto low = flag(bits[i]);
    result = l.b.concat(result, low);
  }
  return result;
}
} // namespace nocloom::detail
