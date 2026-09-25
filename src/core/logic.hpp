#pragma once

#include "component.hpp"
#include <algorithm>
#include <bit>
#include <set>
#include <span>
#include <tuple>
#include <vector>

// A small register-transfer DSL over Builder used by every generated
// component: one clock and active-low reset and named state bindings.
namespace nocloom::detail::logic {
using Fields = std::map<std::string, ValueId>;
struct Logic {
  Builder b;
  std::map<std::string, ValueId> bindings;
  ValueId clock = b.input("nl_clk", 1, "domain:main", "clock");
  ValueId reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  ValueId bit(unsigned n) { return b.constant(1, n); }
  ValueId both(ValueId a, ValueId c) { return b.binary(Op::BitAnd, a, c); }
  ValueId either(ValueId a, ValueId c) { return b.binary(Op::BitOr, a, c); }
  ValueId inv(ValueId a) { return b.unary(Op::BitNot, a, 1); }
  ValueId pick(ValueId c, ValueId t, ValueId f) { return b.select(c, t, f); }
  ValueId num(unsigned w, std::uint64_t n) { return b.constant(w, n); }
  ValueId eq(ValueId a, std::uint64_t n) { return b.binary(Op::Equal, a, b.constant(width(a), n)); }
  // Word arithmetic over equal widths: equality, unsigned order, modular
  // difference and sum, the smaller word, zero extension, a ~(span - 1) mask.
  unsigned width(ValueId a) const { return b.ir.values.at(a.index).width; }
  ValueId same(ValueId a, ValueId c) { return b.binary(Op::Equal, a, c); }
  ValueId below(ValueId a, ValueId c) { return b.binary(Op::UnsignedLess, a, c); }
  ValueId minus(ValueId a, ValueId c) { return b.binary(Op::Subtract, a, c); }
  // One carry chain: a + c == ~(~a - c) modulo 2^width, and a constant addend
  // subtracts its two's complement.
  ValueId plus(ValueId a, ValueId c) {
    const auto w = width(a);
    if (const auto &v = b.ir.values.at(c.index); v.op == Op::Constant)
      return minus(a, b.constant(w, (0 - v.literal) & (w >= 64 ? ~0ULL : (1ULL << w) - 1)));
    return b.unary(Op::BitNot, minus(b.unary(Op::BitNot, a, w), c), w);
  }
  // The offset of an address in its 4 KiB page, 13 bits wide so that the page top fits.
  ValueId page_offset(ValueId a) { return zext(b.slice(a, 0, 12), 13); }
  // a + step for a step below one 4 KiB page (as wide as a): the sum of the page offsets, and the
  // page bits incremented beside it, taken on the carry out of the page.
  ValueId page_plus(ValueId a, ValueId step) {
    const auto w = width(a);
    if (w <= 12)
      return plus(a, step);
    const auto sum = plus(page_offset(a), page_offset(step));
    const auto page = b.slice(a, 12, w - 12);
    return b.concat(pick(b.slice(sum, 12, 1), inc(page), page), b.slice(sum, 0, 12));
  }
  ValueId inc(ValueId a) {
    return minus(a, b.constant(width(a), width(a) >= 64 ? ~0ULL : (1ULL << width(a)) - 1));
  }
  ValueId dec(ValueId a) { return minus(a, b.constant(width(a), 1)); }
  // c == a + 1 modulo 2^width without a carry chain: x = a ^ c is set at bit 0, and every bit of
  // a & ~c below the top equals the bit of x above it (the run of carried ones ends at a zero).
  ValueId follows(ValueId a, ValueId c) {
    const auto w = width(a);
    const auto x = both(either(a, c), b.unary(Op::BitNot, both(a, c), w));
    if (w == 1)
      return x;
    const auto carried = both(a, b.unary(Op::BitNot, c, w));
    return both(b.slice(x, 0, 1), same(b.slice(carried, 0, w - 1), b.slice(x, 1, w - 1)));
  }
  ValueId lesser(ValueId a, ValueId c) { return b.select(below(a, c), a, c); }
  ValueId zext(ValueId a, unsigned w) { return b.unary(Op::ZeroExtend, a, w); }
  // Balanced OR and AND of a list (zero and one when empty): one level per halving.
  ValueId any(std::span<const ValueId> v) {
    if (v.size() < 2)
      return v.empty() ? bit(0) : v[0];
    return either(any(v.first(v.size() / 2)), any(v.subspan(v.size() / 2)));
  }
  ValueId every(std::span<const ValueId> v) {
    if (v.size() < 2)
      return v.empty() ? bit(1) : v[0];
    return both(every(v.first(v.size() / 2)), every(v.subspan(v.size() / 2)));
  }
  // arms[field] for a field of n bits and 2^n arms: one select level per field bit.
  ValueId by_index(ValueId field, std::span<const ValueId> arms) {
    if (arms.size() == 1)
      return arms[0];
    const auto half = arms.size() / 2;
    const auto bit_n = static_cast<unsigned>(std::countr_zero(arms.size())) - 1;
    return pick(b.slice(field, bit_n, 1), by_index(field, arms.subspan(half)),
                by_index(field, arms.first(half)));
  }
  // The value of the first hit in list order, else outside, as a balanced
  // tree: each level picks the lower half when it holds a hit, so the depth
  // grows with log2 of the list, not with its length.
  ValueId first_hit(std::span<const ValueId> hits, std::span<const ValueId> values,
                    ValueId outside) {
    if (hits.empty())
      return outside;
    // Given a hit in the range: the value of its first one.
    auto hit = [&](auto &self, std::size_t lo, std::size_t hi) -> ValueId {
      if (hi - lo == 1)
        return values[lo];
      const auto mid = lo + (hi - lo) / 2;
      return pick(any(hits.subspan(lo, mid - lo)), self(self, lo, mid), self(self, mid, hi));
    };
    return pick(any(hits), hit(hit, 0, hits.size()), outside);
  }
  // Whether `address` lies in [lo, hi): the range as aligned power-of-two blocks, each an equality
  // of the address bits above the block (no carry chain), joined in a tree.
  ValueId within(ValueId address, std::uint64_t lo, std::uint64_t hi) {
    const auto w = width(address);
    std::vector<ValueId> blocks;
    while (lo < hi) {
      auto size = lo ? std::uint64_t{1} << std::countr_zero(lo) : std::bit_floor(hi);
      while (size > hi - lo)
        size >>= 1;
      const auto k = static_cast<unsigned>(std::countr_zero(size));
      blocks.push_back(
          k >= w ? bit(1)
                 : b.binary(Op::Equal, b.slice(address, k, w - k), b.constant(w - k, lo >> k)));
      lo += size;
    }
    return any(blocks);
  }
  // The value of the first window in list order holding `address`, else outside; window k takes
  // values[cls[k]]. Each class owns disjoint address ranges, so the class hits are exclusive and
  // join in one AND-OR (depth log2 of the ranges and classes).
  ValueId window_class(ValueId address, std::span<const AddressWindow> windows,
                       std::span<const unsigned> cls, std::span<const ValueId> values,
                       ValueId outside) {
    std::vector<std::uint64_t> cuts;
    for (const auto &x : windows)
      cuts.insert(cuts.end(), {x.base, x.base + x.size});
    std::ranges::sort(cuts);
    cuts.erase(std::unique(cuts.begin(), cuts.end()), cuts.end());
    // [lo, hi) and its class, for each run of ranges sharing the first window's class.
    std::vector<std::tuple<std::uint64_t, std::uint64_t, unsigned>> runs;
    for (std::size_t i = 0; i + 1 < cuts.size(); ++i) {
      const auto at = std::ranges::find_if(windows, [&](const AddressWindow &x) {
        return x.base <= cuts[i] && cuts[i + 1] <= x.base + x.size;
      });
      if (at == windows.end())
        continue;
      const auto c = cls[static_cast<std::size_t>(at - windows.begin())];
      if (!runs.empty() && std::get<1>(runs.back()) == cuts[i] && std::get<2>(runs.back()) == c)
        std::get<1>(runs.back()) = cuts[i + 1];
      else
        runs.emplace_back(cuts[i], cuts[i + 1], c);
    }
    std::vector<std::vector<ValueId>> hits(values.size());
    std::vector<ValueId> owned;
    for (const auto &[lo, hi, c] : runs)
      hits[c].push_back(within(address, lo, hi));
    std::vector<ValueId> terms;
    const auto zero_value = b.constant(width(outside), 0);
    for (std::size_t c = 0; c < values.size(); ++c)
      if (!hits[c].empty()) {
        owned.push_back(any(hits[c]));
        terms.push_back(pick(owned.back(), values[c], zero_value));
      }
    terms.push_back(pick(any(owned), zero_value, outside));
    return any(terms);
  }
  // An address moved into the miss window w: its base, keeping the page offset when w holds whole
  // pages (miss_page_mask).
  static std::uint64_t miss_page_mask(const AddressWindow &w) {
    return w.size >= 4096 && w.base % 4096 == 0 ? 4095 : 0;
  }
  ValueId moved_to_miss(const AddressWindow &w, ValueId a) {
    return either(num(width(a), w.base), both(a, num(width(a), miss_page_mask(w))));
  }
  // Per field of two plans (Plan::refs), miss's where e is set, else own's.
  template <class Plan> Plan choose(ValueId e, Plan miss, Plan own) {
    const auto to = own.refs(), from = miss.refs();
    for (std::size_t k = 0; k < to.size(); ++k)
      *to[k] = pick(e, *from[k], *to[k]);
    return own;
  }
  // Truncated or zero-extended to w > 0 bits.
  ValueId fit(ValueId a, unsigned w) {
    return w == width(a) ? a : w < width(a) ? b.slice(a, 0, w) : zext(a, w);
  }
  ValueId floor(ValueId a, ValueId span) {
    return b.binary(Op::BitAnd, a,
                    b.unary(Op::BitNot, minus(span, b.constant(width(span), 1)), width(span)));
  }
  // arm(size) for size 1..top and arm(0) otherwise: a select tree over the size bits (a chain
  // of equality selects for a field wider than four bits).
  template <class F> ValueId per_size(ValueId size, unsigned top, const F &arm) {
    if (width(size) > 4) {
      ValueId out = arm(0);
      for (unsigned n = 1; n <= top; ++n)
        out = b.select(eq(size, n), arm(n), out);
      return out;
    }
    std::vector<ValueId> arms;
    for (unsigned n = 0; n < (1U << width(size)); ++n)
      arms.push_back(arm(n >= 1 && n <= top ? n : 0));
    return by_index(size, arms);
  }
  // 2^size as a w-bit word and value << size, value >> size, for size 0..top.
  ValueId pow2(ValueId size, unsigned w, unsigned top) {
    return per_size(size, top, [&](unsigned n) { return b.constant(w, std::uint64_t{1} << n); });
  }
  ValueId shl(ValueId value, ValueId size, unsigned top) {
    return per_size(size, top, [&](unsigned n) {
      return n ? b.concat(b.slice(value, 0, width(value) - n), b.constant(n, 0)) : value;
    });
  }
  ValueId shr(ValueId value, ValueId size, unsigned top) {
    return per_size(size, top, [&](unsigned n) {
      return n ? zext(b.slice(value, n, width(value) - n), width(value)) : value;
    });
  }
  ValueId bind(const std::string &name, ValueId value) {
    bindings.emplace(name, value);
    return value;
  }
  ValueId state(const std::string &name, unsigned bits) {
    return bind(name, b.state(bits, clock, reset));
  }
  // A latch of a global address (arrive) keeps only the bits that vary inside the block.
  ValueId latch(const std::string &name, ValueId value, ValueId enable) {
    auto s = state_like(name, value);
    b.update(s, b.select(enable, value, s));
    return s;
  }
  // The block that holds every global address reaching a target engine (AddressBlock), for
  // addresses of block_bits bits. arrive() fixes the bits above block.keep of a face address to
  // the block's, and an address state stores only the low block.keep bits and reads back with
  // the block's high bits: every address the engine derives stays inside the block.
  AddressBlock block{};
  unsigned block_bits = 0;
  std::set<std::size_t> global_addresses;
  bool narrows(unsigned w) const { return block.keep && w == block_bits && block.keep < w; }
  ValueId block_high(unsigned w) { return num(w - block.keep, block.base >> block.keep); }
  ValueId arrive(ValueId a) {
    if (!narrows(width(a)))
      return a;
    const auto high = block_high(width(a));
    const auto pinned = b.concat(high, b.slice(a, 0, block.keep));
    global_addresses.insert(pinned.index);
    return pinned;
  }
  ValueId address_state(const std::string &name, unsigned w) {
    if (!narrows(w))
      return state(name, w);
    const auto low = state(name, block.keep);
    const auto view = b.widened(low, block_high(w));
    global_addresses.insert(view.index);
    return view;
  }
  // Marks a derived value as a global address of the block (for state_like and latch).
  ValueId global(ValueId a) {
    if (narrows(width(a)))
      global_addresses.insert(a.index);
    return a;
  }
  // The address fields of a face (arrive) or of a derived view (global).
  Fields arrive(Fields f) { return addresses(std::move(f), &Logic::arrive); }
  Fields global(Fields f) { return addresses(std::move(f), &Logic::global); }
  Fields addresses(Fields f, ValueId (Logic::*mark)(ValueId)) {
    for (const auto *name : {"address", "start_address"})
      if (f.contains(name))
        f[name] = (this->*mark)(f.at(name));
    return f;
  }
  // A state shaped like value: an address state when value is a global address.
  ValueId state_like(const std::string &name, ValueId value) {
    return global_addresses.contains(value.index) ? address_state(name, width(value))
                                                  : state(name, width(value));
  }
  // Ports nl_<face>_<field>; a burst face is the fields of a (field, width)
  // layout plus valid.
  ValueId in(const std::string &face, const std::string &field, unsigned bits) {
    return b.input("nl_" + face + "_" + field, bits, face, field);
  }
  void out(const std::string &face, const std::string &field, ValueId value) {
    b.output("nl_" + face + "_" + field, value, face, field);
  }
  ValueId active(ValueId v) { return both(reset, v); }
  // A target watchdog (one per outlet), log2(cycles) bits: counts cycles after one with a request
  // offered, a packet held or a response offered, while no response is offered;
  // a response restarts it; `cycles` counts set `failed` until common reset.
  ValueId watchdog(ValueId failed, unsigned cycles, ValueId pending, ValueId responding) {
    const auto count = state("timeout.count", static_cast<unsigned>(std::countr_zero(cycles)));
    const auto was = state("timeout.pending", 1);
    b.update(was, active(either(pending, responding)));
    const auto counting = bind("timeout.waiting", both(was, inv(either(responding, failed))));
    b.update(count, pick(responding, num(width(count), 0),
                         pick(counting, plus(count, num(width(count), 1)), count)));
    const auto expiry = both(counting, eq(count, cycles - 1));
    b.update(failed, either(failed, expiry));
    return expiry;
  }
  template <class Layout> Fields face(const std::string &f, const Layout &layout) {
    Fields v;
    for (const auto &[name, bits] : layout)
      v[name] = in(f, name, bits);
    v["valid"] = in(f, "valid", 1);
    return v;
  }
  template <class Layout> void drive(const std::string &f, const Fields &v, const Layout &layout) {
    for (const auto &[name, bits] : layout)
      out(f, name, v.at(name));
  }
  // Every field but valid (and `skip`) latched as <prefix>.<field>.
  Fields latch_fields(const std::string &prefix, const Fields &f, ValueId enable,
                      const std::vector<std::string> &skip = {}) {
    Fields d;
    for (const auto &[name, v] : f)
      if (name != "valid" && std::ranges::find(skip, name) == skip.end())
        d[name] = latch(prefix + "." + name, v, enable);
    return d;
  }
  Result<NodeHardware> finish(std::string name) {
    b.ir.name = std::move(name);
    auto checked = verify(b.ir);
    if (!checked)
      return Result<NodeHardware>::failure(checked.diagnostics());
    return Result<NodeHardware>::success({std::move(b.ir), std::move(bindings)});
  }
};
} // namespace nocloom::detail::logic
