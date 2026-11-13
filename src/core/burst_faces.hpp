#pragma once
#include "logic.hpp"
#include "message.hpp"
#include <bit>
#include <string>

namespace nocloom::detail {
// A beat at `address` of burst d ends a response run (message.hpp point 18): every beat of a FIXED
// burst, one narrower than the 2^word_bits bytes of the attachment's word, a WRAP beat at its
// container's top. Only the low address bits matter: a run ends at its word anyway.
inline ValueId burst_run_end(logic::Logic &l, const logic::Fields &d, ValueId address,
                             unsigned data_bits, unsigned run_bits) {
  const auto word_bits = static_cast<unsigned>(std::countr_zero(data_bits / 8));
  // A WRAP's length is a low mask (beats less one): its beat at the top has all of them set.
  const auto length =
      l.b.slice(l.shl(l.fit(d.at("length"), run_bits + 7), d.at("size"), 7), 0, run_bits);
  const auto top = l.same(l.both(l.fit(address, run_bits), length), length);
  return l.either(l.either(l.eq(d.at("burst"), 0), l.below(d.at("size"), l.num(3, word_bits))),
                  l.both(l.eq(d.at("burst"), 2), top));
}
// The burst face prologue shared by the width-converter lowerings (width cut,
// packetizer, upsize): reset gating, the face port helpers and the request
// and response field layouts of one burst profile at a given data width.
struct BurstFaces {
  logic::Logic &l;
  const BurstProfile &burst;
  unsigned source_bits = 0;
  ValueId active(ValueId v) { return l.active(v); }
  template <class... A> ValueId in(const A &...a) { return l.in(a...); }
  template <class... A> void out(const A &...a) { l.out(a...); }
  auto fields(unsigned data, bool request) const {
    return request ? burst_request_fields(data, burst, source_bits)
                   : burst_response_fields(data, burst, source_bits);
  }
  logic::Fields inputs(const std::string &face, unsigned data, bool request) {
    return l.face(face, fields(data, request));
  }
  void fields_out(const std::string &face, const logic::Fields &f, unsigned data, bool request) {
    auto output = f;
    if (burst.data_user_bits_per_byte && !output.contains("data_user"))
      output["data_user"] = l.num(data / 8 * burst.data_user_bits_per_byte, 0);
    if (burst.data_user_bits_per_byte && output.contains("data_user"))
      output["data_user"] = l.fit(output.at("data_user"), data / 8 * burst.data_user_bits_per_byte);
    l.drive(face, output, fields(data, request));
  }
};
// Per-field views and register loads of a held field map, walked in the held
// map's own order. view_fields takes the face value where `take` holds;
// load_fields loads registers field-wise.
using logic::Fields;
inline Fields view_fields(logic::Logic &l, const Fields &held, const Fields &face, ValueId take) {
  Fields view;
  for (const auto &[name, value] : held)
    view[name] = l.b.select(take, face.at(name), value);
  return view;
}
inline void load_fields(logic::Logic &l, const Fields &held, const Fields &next, ValueId go) {
  for (const auto &[name, value] : held)
    l.b.update(value, l.b.select(go, next.at(name), value));
}
// A WRAP write beat that is its container's last (message.hpp point 4, wrap_end): of length + 1
// beats (a power of two) its index, the address bits from its size up, has every length bit set;
// zero for reads and other bursts. The address bit under each length bit is chosen by size first,
// so the length passes only the final levels.
inline ValueId wrap_end_of(logic::Logic &l, ValueId write, ValueId burst, ValueId length,
                           ValueId size, ValueId address) {
  const unsigned aw = l.width(address);
  std::vector<ValueId> holds;
  for (unsigned j = 0; j < l.width(length); ++j) {
    std::vector<ValueId> arms;
    for (unsigned s = 0; s < 8; ++s)
      arms.push_back(s + j < aw ? l.b.slice(address, s + j, 1) : l.bit(0));
    holds.push_back(l.either(l.inv(l.b.slice(length, j, 1)), l.by_index(size, arms)));
  }
  return l.both(l.both(write, l.eq(burst, 2)), l.every(holds));
}
// Whether a beat of 2^size bytes at `address` (aligned to its size) ends at the last byte of a
// word of `bytes` (message.hpp point 4, ends).
inline ValueId beat_ends(logic::Logic &l, ValueId address, ValueId size, unsigned bytes) {
  std::vector<ValueId> ones;
  for (unsigned i = 0; (1U << i) < bytes; ++i)
    ones.push_back(l.either(l.b.slice(address, i, 1), l.below(l.num(3, i), size)));
  return l.every(ones);
}
// The page offset (13 bits, the page top included) past the run a burst's beats walk from start:
// a WRAP's (kind 2) container top, a FIXED (kind 0) beat's container end, an INCR's last beat end.
// Sizes above largest count as largest. The width cut plans its pieces up to it; the packet
// splitter hands it over beside a staged packet.
inline ValueId run_end(logic::Logic &l, ValueId start, ValueId size, ValueId length, ValueId kind,
                       unsigned largest) {
  constexpr unsigned bits = 13;
  auto per_size = [&](auto arm) {
    std::vector<ValueId> arms;
    for (unsigned k = 0; k < 8; ++k)
      arms.push_back(arm(std::min(k, largest)));
    return l.by_index(size, arms);
  };
  const auto len = l.zext(length, bits);
  // span - 1 is len << size with the size's low bits set (no carry chain); every end is one
  // carry chain: an aligned base plus span is base - ~(span - 1), and the beat-aligned start plus
  // span is (start | 2^size - 1) - ~(len << size).
  const auto len_span = per_size(
      [&](unsigned n) { return n ? l.b.concat(l.b.slice(len, 0, bits - n), l.num(n, 0)) : len; });
  const auto beat_mask = per_size([&](unsigned n) { return l.num(bits, (1U << n) - 1); });
  const auto outside = l.b.unary(Op::BitNot, l.either(len_span, beat_mask), bits);
  const auto at = l.page_offset(start);
  const auto beat_last = l.either(at, beat_mask);
  return l.pick(l.eq(kind, 2), l.minus(l.both(at, outside), outside),
                l.pick(l.eq(kind, 0), l.inc(beat_last),
                       l.minus(beat_last, l.b.unary(Op::BitNot, len_span, bits))));
}
} // namespace nocloom::detail
