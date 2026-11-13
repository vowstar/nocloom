#include "axi4_packetizer.hpp"
#include "burst_faces.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "message.hpp"
#include "piece_count.hpp"
#include "strobe_lanes.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <map>
#include <numeric>
#include <tuple>

namespace nocloom::detail {
namespace {
using Fields = std::map<std::string, ValueId>;
using logic::Logic;

// One packetizer lowering: the builder, the profile, the shared derived
// constants and the burst-plan helpers, plus the handles the write phases
// hand to each other.
struct PacketizerLowering {
  PacketizerLowering(logic::Logic &logic, const Axi4PacketizerProfile &profile)
      : l(logic), p(profile), bytes(p.data_bits / 8), lw(std::bit_width(bytes) - 1),
        ab(p.address_bits), lb(p.len_bits), wide(ab + 1), zero(l.bind("c.zero", l.bit(0))),
        one(l.bind("c.one", l.bit(1))), burst{p.id_bits, lb, true, ab},
        multi(p.max_outstanding > 1), io{l, burst, p.source_bits}, word_size(l.num(3, lw)),
        count_bits(burst_count_bits(p.data_bits)),
        piece_bits(
            std::max(piece_count::kBits, static_cast<unsigned>(std::bit_width(p.max_outstanding)))),
        misses(p.fallback ||
               std::ranges::any_of(p.windows, [](const auto &w) { return !!w.miss; })),
        refusals(p.write &&
                 std::ranges::any_of(p.windows, [](const auto &w) { return w.refused; })),
        exact_any(std::ranges::any_of(p.windows, [](const auto &w) { return w.exact; })),
        granule_any(std::ranges::any_of(p.windows, [](const auto &w) { return w.read_granule; })),
        align_any(std::ranges::any_of(p.windows, [](const auto &w) { return w.align_wrap; }) ||
                  (p.fallback && p.fallback->align_wrap)),
        groups(std::accumulate(
            p.windows.begin(), p.windows.end(), p.fallback ? p.fallback->group + 1 : 0U,
            [](unsigned most, const auto &w) { return std::max(most, w.group + 1); })),
        gb(std::bit_width(groups)), unlimited(l.num(wide, (std::uint64_t{1} << ab) * 2 - 1)),
        contexts(multi ? std::min(p.max_outstanding, piece_count::kInFlight) : 1) {
    burst.exclusive = p.exclusive;
    burst.tag_bits = p.tag_bits;
    burst.user_bits = p.user_bits;
    burst.record_bits = p.record_bits;
    burst.security_bits = p.security_bits;
    burst.response_user_bits = p.response_user_bits;
    burst.data_user_bits_per_byte = p.data_user_bits_per_byte;
    burst.level_bits = p.level_bits;
    if (p.split_narrow || exact_any) {
      if (p.write)
        write_lock_hint = l.b.input("nl_hint_write_lock", 1, "hint", "write_lock");
      read_lock_hint = l.b.input("nl_hint_read_lock", 1, "hint", "read_lock");
    }
    if (p.source_miss) {
      if (p.write) {
        write_start_hint = l.b.input("nl_hint_write_start", ab, "hint", "write_start");
        write_address_hint = l.b.input("nl_hint_write_address", ab, "hint", "write_address");
        write_length_hint = l.b.input("nl_hint_write_length", lb, "hint", "write_length");
      }
      read_start_hint = l.b.input("nl_hint_read_start", ab, "hint", "read_start");
    }
    if (p.write && p.miss_drop)
      write_dropping = l.b.input("nl_hint_write_dropping", 1, "hint", "write_dropping");
    if (p.write && p.miss_drop && p.miss_lag && p.max_outstanding == 1)
      miss_entered = l.b.input("nl_hint_write_miss_entered", 1, "hint", "write_miss_entered");
  }

  logic::Logic &l;
  const Axi4PacketizerProfile &p;
  const unsigned bytes, lw, ab, lb;
  const unsigned wide; // packet ends may reach the top of the address space
  const ValueId zero, one;
  BurstProfile burst;
  const bool multi;
  BurstFaces io;
  const ValueId word_size;
  const unsigned count_bits;
  // A narrow-split burst's pieces in flight are bounded by the source's shared pool of
  // max_outstanding transactions, each piece one of them, not by piece_count::kInFlight: a
  // context's piece counter may have to hold that many.
  const unsigned piece_bits;
  const bool misses;
  const bool refusals;    // a window of a target that refuses writes (Axi4PacketWindow::refused)
  const bool exact_any;   // a window with exact packets
  const bool granule_any; // a window with a read granule (Axi4PacketWindow::read_granule)
  const bool align_any;   // a window whose target aligns WRAP reads (Axi4PacketWindow::align_wrap)
  const unsigned groups, gb;
  const ValueId unlimited;
  const unsigned contexts;
  // The source's AxLOCK of the burst on the up face, from the initiator
  // descriptor, whether or not the Weft lock field exists: an exclusive burst
  // is not split into single narrow beats, whichever target serves it.
  std::optional<ValueId> write_lock_hint, read_lock_hint;
  // With source_miss: the source's own addresses and write AxLEN, in place of the up face's.
  std::optional<ValueId> write_start_hint, write_address_hint, write_length_hint, read_start_hint;

  // stop: the page offset past the packet's last byte. split: it falls short of the burst's end.
  // limited: the end applies (else unlimited). first_last: the start's beat is its last.
  // Exact packets: block, the packet's bytes less one (13 bits); sub: the block is smaller than
  // the beat, one beat of sub_size; partial: the block ends before the start's beat.
  struct Packet {
    ValueId end, beats, stop, length, split, limited, first_last, block, sub, sub_size, partial;
    auto refs() {
      return std::vector{&end,        &beats, &stop, &length,   &split,  &limited,
                         &first_last, &block, &sub,  &sub_size, &partial};
    }
  };
  // A WRAP's container at an address: its base, its top (exclusive) and its last byte.
  struct Container {
    ValueId base, top, last;
    auto refs() { return std::vector{&base, &top, &last}; }
  };
  ValueId widen(ValueId v) { return l.zext(v, wide); }
  ValueId narrow_address(ValueId v) { return l.b.slice(v, 0, ab); }
  // 2^size - 1 over the wide address width (size never exceeds lw), a constant per size (no carry
  // chain).
  ValueId low_mask(ValueId size) {
    return l.per_size(size, lw,
                      [&](unsigned n) { return l.num(wide, (std::uint64_t{1} << n) - 1); });
  }
  ValueId floor_to(ValueId address, ValueId size) {
    return l.both(address, l.b.unary(Op::BitNot, low_mask(size), wide));
  }
  ValueId count_of(ValueId size) { return l.pow2(size, count_bits, lw); }
  // A WRAP's container bytes less one, ((len + 1) << size) - 1 over the wide address width: len <<
  // size with the size's low bits set, so no carry chain.
  ValueId container_of(const Fields &f) {
    return l.either(l.shl(l.zext(f.at("length"), wide), f.at("size"), lw), low_mask(f.at("size")));
  }
  // The container holding `address` for the mask of container_of: base address & ~mask, top base
  // + mask + 1 (base - ~mask), last byte address | mask.
  Container top_of(ValueId address, ValueId mask) {
    const auto a = widen(address), outside = l.b.unary(Op::BitNot, mask, wide);
    const auto base = l.both(a, outside);
    return Container{base, l.minus(base, outside), l.either(a, mask)};
  }
  // What a window decode reads: the address (with source_miss the source's own one), the
  // request's error flag and held, a register address taken while its flag is set.
  struct Key {
    ValueId address{};
    std::optional<ValueId> error{};
    std::optional<std::pair<ValueId, ValueId>> held{};
    Key at(ValueId a, std::optional<std::pair<ValueId, ValueId>> h = std::nullopt) const {
      return Key{a, error, h ? h : held};
    }
  };
  // A value per routing window, chosen by the window holding the key's address, else the
  // fallback's (outside without one); windows with equal keys share one value (it depends only on
  // the parameters the key names).
  template <class F, class K>
  ValueId per_window(const Key &at, ValueId outside, const F &value, const K &key) {
    std::vector<AddressWindow> windows;
    std::vector<unsigned> classes;
    std::vector<ValueId> values;
    std::map<decltype(key(p.windows.front())), unsigned> seen;
    for (const auto &w : p.windows) {
      windows.push_back(w.window);
      const auto [it, fresh] = seen.try_emplace(key(w), static_cast<unsigned>(values.size()));
      if (fresh)
        values.push_back(value(w));
      classes.push_back(it->second);
    }
    // Beyond every window: the fallback's, except for a request the source admitted (error flag
    // clear).
    auto beyond = p.fallback ? value(*p.fallback) : outside;
    if (p.fallback && at.error)
      beyond = l.pick(*at.error, beyond, outside);
    // A moved request misses in a window only some flows reach.
    std::vector<AddressWindow> whole;
    std::vector<unsigned> kept;
    for (std::size_t k = 0; k < p.windows.size(); ++k)
      if (!p.windows[k].partial) {
        whole.push_back(windows[k]);
        kept.push_back(classes[k]);
      }
    const bool partial = at.error && whole.size() < windows.size();
    auto decode = [&](ValueId address) {
      const auto all = l.window_class(widen(address), windows, classes, values, beyond);
      return partial ? l.pick(*at.error,
                              l.window_class(widen(address), whole, kept, values, beyond), all)
                     : all;
    };
    const auto now = decode(at.address);
    return at.held ? l.pick(at.held->first, decode(at.held->second), now) : now;
  }
  // The group of the window holding the key's address, one past the largest outside every window.
  ValueId group_of(const Key &at) {
    return per_window(
        at, l.num(gb, groups), [&](const Axi4PacketWindow &w) { return l.num(gb, w.group); },
        [](const Axi4PacketWindow &w) { return w.group; });
  }
  static auto limit_key(const Axi4PacketWindow &w) {
    return std::tuple{w.align_bytes, w.max_bytes, w.boundary_bytes};
  }
  static auto miss_key(const Axi4PacketWindow &w) {
    return w.miss ? std::tuple{true, w.miss->base, w.miss->size}
                  : std::tuple{false, std::uint64_t{0}, std::uint64_t{0}};
  }
  // Whether a packet starting at `start` (window decode `at`) is a decode miss, and its address.
  std::pair<ValueId, ValueId> missed(ValueId start, const Key &at) {
    return std::pair{
        per_window(
            at, zero, [&](const Axi4PacketWindow &w) { return w.miss ? one : zero; }, miss_key),
        per_window(
            at, start,
            [&](const Axi4PacketWindow &w) {
              if (!w.miss)
                return start;
              const std::uint64_t kept = p.miss_kept_bits.value_or(w.miss->size < 4096 ? 0 : 4095);
              return l.either(l.num(ab, w.miss->base & ~kept), l.both(start, l.num(ab, kept)));
            },
            miss_key)};
  }
  // An address the source moved into its miss window.
  ValueId moved_by_source(ValueId a) {
    if (!p.miss_kept_bits)
      return l.moved_to_miss(*p.source_miss, a);
    return l.either(l.num(l.width(a), p.source_miss->base & ~*p.miss_kept_bits),
                    l.both(a, l.num(l.width(a), *p.miss_kept_bits)));
  }
  // A non-modifiable narrow INCR of a splitting source without AxLOCK goes one beat per packet.
  ValueId split_one(ValueId kind, ValueId size, ValueId cache, ValueId hint) {
    return l.both(l.both(l.eq(kind, 1), l.below(size, word_size)),
                  l.both(l.inv(l.b.slice(cache, 1, 1)), l.inv(hint)));
  }
  // Exact packets: an admitted INCR with AxCACHE[1] and AxLOCK clear whose start lies in a window
  // of a target with read side effects.
  ValueId exact_of(const Fields &f, const Key &at, std::optional<ValueId> hint) {
    if (!exact_any)
      return zero;
    auto e = l.both(l.both(l.eq(f.at("burst"), 1), l.inv(l.b.slice(f.at("cache"), 1, 1))),
                    l.inv(f.at("error")));
    if (hint)
      e = l.both(e, l.inv(*hint));
    if (p.exclusive)
      e = l.both(e, l.inv(f.at("lock")));
    return l.both(e, per_window(
                         at, zero, [&](const Axi4PacketWindow &w) { return w.exact ? one : zero; },
                         [](const Axi4PacketWindow &w) { return w.exact; }));
  }
  // value >> n over its own width.
  ValueId shift_down(ValueId value, unsigned n) {
    return l.zext(l.b.slice(value, n, l.width(value) - n), l.width(value));
  }
  // End (exclusive) of the packet starting at `start` and its source beats,
  // min(remaining, beats from floor(start, size) to the end); a split WRAP runs
  // as INCR and its first run stops at the container top. A burst never crosses its 4 KiB page,
  // so the end is planned as an offset in the start's page (13 bits): a limit at or past the page
  // top leaves every remaining beat to this packet, whatever its full address. The full end joins
  // the page to the offset (the next page for an offset past the top).
  Packet packet_of(ValueId start, const Key &at, ValueId size, ValueId burst_kind, ValueId cache,
                   ValueId remaining, std::optional<ValueId> lock, std::optional<ValueId> lock_hint,
                   ValueId cut, const Container &box, ValueId exact, bool read) {
    constexpr unsigned pw = 13;
    const auto s = widen(start);
    const auto o = l.page_offset(s);
    const auto past_top = l.num(pw, 4096);
    // floor(start, unit) + bytes within the page, or the page top when it reaches past it.
    auto from = [&](std::uint64_t unit, std::uint64_t bytes_) {
      if (unit >= 4096 || bytes_ >= 4096)
        return past_top;
      return l.plus(l.both(o, l.num(pw, 8191 & ~(unit - 1))), l.num(pw, bytes_));
    };
    const auto offset_mask = l.b.slice(low_mask(size), 0, pw);
    // An exclusive burst stays one packet, as do FIXED and unsplit WRAP bursts.
    auto limited = l.either(l.eq(burst_kind, 1), cut);
    if (lock)
      limited = l.both(limited, l.inv(*lock));
    // The packet ends at the least of the window end, a split WRAP's container top and the burst's
    // own end, compared side by side. Every end is a multiple of 2^size, so the beats less one are
    // (end - (floor + 2^size)) >> size.
    const auto top = l.pick(cut, l.inc(l.page_offset(box.last)), l.num(pw, 8191));
    const auto floor_offset = l.both(o, l.b.unary(Op::BitNot, offset_mask, pw));
    const auto next_floor = l.inc(l.either(o, offset_mask));
    const auto burst_stop = l.plus(floor_offset, l.shl(l.zext(remaining, pw), size, lw));
    auto end = per_window(
        at, past_top,
        [&](const Axi4PacketWindow &w) {
          return l.lesser(from(w.align_bytes, w.max_bytes),
                          from(w.boundary_bytes, w.boundary_bytes));
        },
        limit_key);
    // The first beat (ending at next_floor) ends the packet when it reaches the window end, the
    // top or (split one per packet) always; each window class compares its limits beside the
    // decode.
    const auto reaches = [&](ValueId limit) { return l.inv(l.below(next_floor, limit)); };
    auto first_last = l.either(
        reaches(top), per_window(
                          at, reaches(past_top),
                          [&](const Axi4PacketWindow &w) {
                            return l.either(reaches(from(w.align_bytes, w.max_bytes)),
                                            reaches(from(w.boundary_bytes, w.boundary_bytes)));
                          },
                          limit_key));
    if (read && granule_any) {
      // A read starting inside a granule block stops at its top: the target counts from the head.
      const auto block_top = per_window(
          at, past_top,
          [&](const Axi4PacketWindow &w) {
            if (!w.read_granule)
              return past_top;
            const auto mask = l.num(pw, w.read_granule - 1);
            return l.pick(l.eq(l.both(o, mask), 0), past_top, l.inc(l.either(o, mask)));
          },
          [](const Axi4PacketWindow &w) { return w.read_granule; });
      end = l.lesser(end, block_top);
      first_last = l.either(first_last, reaches(block_top));
    }
    if (p.split_narrow) {
      const auto one_beat = split_one(burst_kind, size, cache, *lock_hint);
      end = l.pick(one_beat, next_floor, end);
      first_last = l.either(one_beat, first_last);
    }
    // An exact packet is the largest naturally aligned power-of-two block from its start: at most
    // the start's alignment, the bytes left (rounded down to a power of two) and the window's
    // packet limit and boundary. Masks of the block less one combine them.
    ValueId block = zero, sub = zero, sub_size = zero, partial = zero;
    if (exact_any) {
      sub_size = l.num(3, 0);
      auto left = l.minus(burst_stop, o);
      for (const unsigned n : {1U, 2U, 4U, 8U})
        left = l.either(left, shift_down(left, n));
      const auto cap = per_window(
          at, l.num(pw, 4095),
          [&](const Axi4PacketWindow &w) {
            return l.num(pw, std::min({w.max_bytes, w.boundary_bytes, std::uint64_t{4096}}) - 1);
          },
          [](const Axi4PacketWindow &w) { return std::min(w.max_bytes, w.boundary_bytes); });
      block =
          l.both(l.both(l.both(l.dec(o), l.b.unary(Op::BitNot, o, pw)), shift_down(left, 1)), cap);
      const auto block_end = l.inc(l.either(o, block));
      end = l.pick(exact, block_end, end);
      first_last = l.pick(exact, l.inv(l.below(next_floor, block_end)), first_last);
      sub = l.both(exact, l.below(block, offset_mask));
      partial = l.both(exact, l.below(block_end, next_floor));
      for (unsigned q = 0; q < lw; ++q)
        sub_size = l.pick(l.b.slice(block, q, 1), l.num(3, q + 1), sub_size);
    }
    const auto before_top = l.below(end, top);
    const auto short_of = l.pick(before_top, l.below(end, burst_stop), l.below(top, burst_stop));
    const auto words =
        l.shr(l.pick(before_top, l.minus(end, floor_offset), l.minus(top, floor_offset)), size, lw);
    const auto less_one =
        l.shr(l.pick(before_top, l.minus(end, next_floor), l.minus(top, next_floor)), size, lw);
    end = l.pick(before_top, end, top);
    const auto split = l.both(limited, short_of);
    const auto page = l.b.slice(s, 12, wide - 12);
    const auto whole =
        l.b.concat(l.pick(l.b.slice(end, 12, 1), l.inc(page), page), l.b.slice(end, 0, 12));
    return Packet{l.pick(limited, whole, unlimited),
                  l.pick(split, l.b.slice(words, 0, lb + 1), remaining),
                  l.pick(short_of, end, burst_stop),
                  l.pick(split, l.b.slice(less_one, 0, lb), length_of(remaining)),
                  split,
                  limited,
                  l.both(limited, first_last),
                  block,
                  sub,
                  sub_size,
                  partial};
  }
  ValueId merge(ValueId a, ValueId b) { return piece_count::merge(l, a, b); }
  static ValueId start_of(const Fields &f) { return f.at("start_address"); }
  ValueId length_of(ValueId beats) { return l.b.slice(l.minus(beats, l.num(lb + 1, 1)), 0, lb); }
  // Narrow WRAP folding: a WRAP narrower than the source whose container (len + 1) << size fits one
  // source word. Only multi-beat WRAP reaches here (the initiator answers other lengths locally).
  static std::uint64_t read_pack(const Axi4PacketWindow &w) {
    return w.read_pack_bytes ? w.read_pack_bytes : w.pack_bytes;
  }
  ValueId folds(const Fields &f, const Key &at, bool read) {
    const auto len = l.zext(f.at("length"), 16);
    ValueId fits = zero;
    for (unsigned n = 0; n < lw; ++n)
      fits = l.either(fits, l.both(l.eq(f.at("size"), n), l.below(len, l.num(16, bytes >> n))));
    // A read fold must remain one physical target word, so its one response code covers
    // exactly the source beats it supplies. Writes use the packing rule above.
    if (read) {
      const auto container = container_of(f);
      // Validated packet boundaries are at least one source word.
      const auto one_target_word = per_window(
          at, one,
          [&](const Axi4PacketWindow &w) {
            return read_pack(w) >= bytes ? one : l.below(container, l.num(wide, read_pack(w)));
          },
          [](const Axi4PacketWindow &w) { return read_pack(w); });
      fits = l.both(fits, one_target_word);
    }
    return l.both(l.both(l.eq(f.at("burst"), 2), l.inv(l.eq(f.at("length"), 0))), fits);
  }
  // WRAP cutting: a WRAP that is not folded runs as INCR when it starts at
  // its container base or its container exceeds the WRAP limit of the window
  // holding its start. An exclusive burst is never cut.
  ValueId cut_of(const Fields &f, const Key &at, bool read) {
    if (!p.decompose_wrap_bursts)
      return zero;
    const auto mask = container_of(f);
    const auto s = widen(start_of(f));
    const auto inside_block =
        !read || !granule_any
            ? zero
            : per_window(
                  at, zero,
                  [&](const Axi4PacketWindow &w) {
                    if (!w.read_granule)
                      return zero;
                    const auto block = l.num(wide, w.read_granule - 1);
                    return l.both(l.inv(l.below(mask, l.num(wide, w.read_granule))),
                                  l.inv(l.eq(l.both(s, block), 0)));
                  },
                  [](const Axi4PacketWindow &w) { return w.read_granule; });
    const auto limit = per_window(
        at, l.num(wide, std::uint64_t{1} << ab),
        [&](const Axi4PacketWindow &w) { return l.num(wide, w.wrap_container_limit); },
        [](const Axi4PacketWindow &w) { return w.wrap_container_limit; });
    const auto aligned = l.eq(l.both(s, mask), 0);
    // limit < span is limit <= span - 1.
    const auto keeps_whole = granule_any ? l.either(aligned, inside_block) : aligned;
    auto cut =
        l.both(l.both(l.eq(f.at("burst"), 2), l.either(keeps_whole, l.inv(l.below(mask, limit)))),
               l.inv(folds(f, at, read)));
    if (p.exclusive)
      cut = l.both(cut, l.inv(f.at("lock")));
    return cut;
  }
  // Reads at a window whose target aligns WRAP reads: a narrow WRAP over more than one packing
  // word, neither folded nor cut nor exclusive, goes out in packing words as a WRAP over its
  // container, AxADDR the start word (the source AxADDR stays the start address).
  ValueId wrap_packs(const Fields &f, ValueId cut, const Key &at) {
    if (!align_any)
      return zero;
    auto packs = l.both(l.both(l.eq(f.at("burst"), 2), l.inv(l.either(folds(f, at, true), cut))),
                        l.below(f.at("size"), pack_of(at, true)));
    if (p.exclusive)
      packs = l.both(packs, l.inv(f.at("lock")));
    return l.both(packs, per_window(
                             at, zero,
                             [&](const Axi4PacketWindow &w) { return w.align_wrap ? one : zero; },
                             [](const Axi4PacketWindow &w) { return w.align_wrap; }));
  }
  // The packing word (log2 bytes) of the window holding the burst's start:
  // the narrower of the source and target words.
  ValueId pack_of(const Key &at, bool read = false) {
    return per_window(
        at, word_size,
        [&](const Axi4PacketWindow &w) {
          return l.num(3, std::bit_width(read ? read_pack(w) : w.pack_bytes) - 1);
        },
        [&](const Axi4PacketWindow &w) { return read ? read_pack(w) : w.pack_bytes; });
  }
  // Packing: an INCR (or a split WRAP run) narrower than the packing word, or of
  // one beat of it, goes out in packing words: AxADDR kept, AxLEN the words it
  // covers, AxSIZE the packing word for several words or the smallest size
  // covering the bytes of one. A folded WRAP covers its container from its base.
  ValueId packs_of(const Fields &f, ValueId cut, const Key &at, bool read) {
    const auto word = pack_of(at, read);
    auto eligible = l.either(l.below(f.at("size"), word),
                             l.both(l.eq(f.at("length"), 0), l.same(f.at("size"), word)));
    if (read) {
      const auto aligned = l.eq(l.both(widen(start_of(f)), low_mask(word)), 0);
      eligible =
          l.both(eligible,
                 per_window(
                     at, one,
                     [&](const Axi4PacketWindow &w) { return w.read_pack_aligned ? aligned : one; },
                     [](const Axi4PacketWindow &w) { return w.read_pack_aligned; }));
    }
    return l.either(folds(f, at, read), l.both(l.either(l.eq(f.at("burst"), 1), cut), eligible));
  }
  // The size of a folded WRAP's one beat: the smallest, at most the word, covering its container
  // (packed_of). The container's base and last byte (top_of) differ in exactly the bits of its
  // mask (container_of), so size q covers it when no mask bit lies at or above q: bit q of
  // covered_below is set when one does.
  ValueId covered_below(ValueId mask) {
    std::vector<ValueId> bits(lw);
    ValueId any = zero;
    for (unsigned q = 12; q-- > 0;) {
      any = l.either(any, l.b.slice(mask, q, 1));
      if (q < lw)
        bits[q] = any;
    }
    ValueId out = l.num(13 - lw, 0);
    for (unsigned q = lw; q-- > 0;)
      out = l.b.concat(out, bits[q]);
    return out;
  }
  ValueId folded_size(ValueId mask) {
    ValueId size = l.num(3, 0);
    for (unsigned q = 0; q < lw; ++q)
      size = l.pick(l.b.slice(mask, q, 1), l.num(3, q + 1), size);
    return l.pick(l.eq(l.b.slice(mask, lw, wide - lw), 0), size, word_size);
  }
  // The smallest size, at most `word`, whose container holds [from, to).
  ValueId cover_of(ValueId from, ValueId to, ValueId word) {
    ValueId cover = word;
    const auto w = l.width(from);
    const auto last_byte = l.minus(to, l.num(w, 1));
    for (unsigned q = lw; q-- > 0;)
      cover = l.pick(l.both(l.below(l.num(3, q), word),
                            l.same(l.b.slice(from, q, w - q), l.b.slice(last_byte, q, w - q))),
                     l.num(3, q), cover);
    return cover;
  }
  // The packed packet: length, size and start for `beats` source beats from
  // `start` (a folded WRAP: its container from the base).
  struct Packed {
    ValueId length, size, start, stop;
    auto refs() { return std::vector{&length, &size, &start, &stop}; }
  };
  // The packet lies in its start's 4 KiB page: the bytes it covers are planned as offsets in the
  // page (13 bits, the top included).
  // The packed packet of `packet` (its stop is the end of the bytes it covers); whole: it covers
  // its WRAP container (a folded WRAP, or a packed one from its start).
  Packed packed_of(const Fields &f, ValueId start, const Packet &packet, ValueId fold,
                   const Key &window, ValueId whole, bool read = false) {
    constexpr unsigned pw = 13;
    const auto mask_of = [&](ValueId k) { return l.b.slice(low_mask(k), 0, pw); };
    const auto box = top_of(start_of(f), container_of(f));
    const auto at = l.page_offset(widen(start));
    const auto from = l.pick(whole, l.page_offset(box.base), at);
    const auto to = l.pick(whole, l.inc(l.page_offset(box.last)), packet.stop);
    // A folded WRAP is one source word: the width conversion gathers it back.
    const auto word = l.pick(fold, word_size, pack_of(window, read));
    // Words from the word holding `from` to the one holding the last byte: to's word index,
    // rounded up, less from's (known before `to`), as two differences picked by the round-up.
    const auto top_word = l.shr(to, word, lw), first_word = l.shr(from, word, lw);
    const auto words = l.pick(l.eq(l.both(to, mask_of(word)), 0), l.minus(top_word, first_word),
                              l.minus(top_word, l.dec(first_word)));
    // More than one word: a word count above one. The length is the count less one in its low
    // bits.
    const auto several = l.inv(l.eq(l.b.slice(words, 1, pw - 1), 0));
    // A folded WRAP's container lies in one source word, so the fold moves only the bits below it.
    const auto wide_start = widen(start);
    const auto word_low = l.pick(fold, l.b.slice(box.base, 0, lw), l.b.slice(wide_start, 0, lw));
    return Packed{l.minus(l.b.slice(words, 0, lb), l.num(lb, 1)),
                  l.pick(several, word, cover_of(from, to, word)),
                  narrow_address(l.b.concat(l.b.slice(wide_start, lw, wide - lw), word_low)), to};
  }
  // The run end (run_end) of a packet that begins now: a whole WRAP's container top, a FIXED
  // beat's container end, else the end of the bytes it covers (a packed INCR's words fit the
  // width cut's target word, so no piece plan reads its own end).
  ValueId packet_end(ValueId kind, ValueId whole_wrap, ValueId start, ValueId size,
                     const Container &box, const Packed &packed) {
    return l.pick(
        whole_wrap, l.inc(l.page_offset(box.last)),
        l.pick(l.eq(kind, 0),
               l.inc(l.either(l.page_offset(widen(start)), l.b.slice(low_mask(size), 0, 13))),
               packed.stop));
  }
  ValueId serial_request(const Fields &f, std::optional<ValueId> lock_hint, const Key &at,
                         bool read) {
    const auto beats = l.plus(l.zext(f.at("length"), lb + 1), l.num(lb + 1, 1));
    const auto cut = cut_of(f, at, read);
    const auto packet =
        packet_of(start_of(f), at, f.at("size"), f.at("burst"), f.at("cache"), beats,
                  p.exclusive ? std::optional{f.at("lock")} : std::nullopt, lock_hint, cut,
                  top_of(start_of(f), container_of(f)), exact_of(f, at, lock_hint), read);
    // Splitting, folding and packing several beats need a reassembly context.
    auto packs = packs_of(f, cut, at, read);
    if (read && align_any)
      packs = l.either(packs, wrap_packs(f, cut, at));
    return l.either(packet.split, l.both(packs, l.inv(l.eq(f.at("length"), 0))));
  }

  // The plan of the beat on the up face from request fields v: the packet a beginning beat starts,
  // its packing, and the beat's end.
  struct WritePlan {
    ValueId cut{}, fold{}, pack{}, start{}, beat_end{}, page{}, fold_size{}, exact{};
    Container box{};
    Packet packet{};
    Packed packed{};
    auto refs() {
      return with_parts({&cut, &fold, &pack, &start, &beat_end, &page, &fold_size, &exact}, box,
                        packet, packed);
    }
  };
  // A plan's own fields, then those of its container, packet and packed packet (Logic::choose).
  static std::vector<ValueId *> with_parts(std::vector<ValueId *> out, Container &box,
                                           Packet &packet, Packed &packed) {
    for (const auto &part : {box.refs(), packet.refs(), packed.refs()})
      out.insert(out.end(), part.begin(), part.end());
    return out;
  }
  WritePlan plan_write(const Fields &v, const Key &start_key, const Key &beat_key, ValueId opening,
                       ValueId remaining) {
    WritePlan w;
    // A packet begins at the burst's start or, after an open packet (a register), at the beat's
    // address; the start does not wait for the up face's first flag.
    w.start = l.pick(opening, v.at("address"), start_of(v));
    // The next block of a partly sent first beat starts where the last one ended.
    if (exact_any)
      w.start = l.pick(*write_sub, *write_sub_start, w.start);
    w.exact = exact_any
                  ? l.pick(v.at("first"), exact_of(v, start_key, write_lock_hint), *write_exact)
                  : zero;
    // A beginning beat's remaining beats: the burst's at its first beat, else the register.
    const auto rem =
        l.pick(opening, remaining, l.plus(l.zext(v.at("length"), lb + 1), l.num(lb + 1, 1)));
    w.cut = cut_of(v, start_key, false);
    w.fold = folds(v, start_key, false);
    w.pack = packs_of(v, w.cut, start_key, false);
    w.box = top_of(w.start, container_of(v));
    w.packet = packet_of(w.start, beat_key.at(w.start), v.at("size"), v.at("burst"), v.at("cache"),
                         rem, p.exclusive ? std::optional{v.at("lock")} : std::nullopt,
                         write_lock_hint, w.cut, w.box, w.exact, false);
    w.packed = packed_of(v, w.start, w.packet, w.fold, start_key, w.fold);
    w.fold_size = folded_size(container_of(v));
    // The beat's end as a page offset, and its page.
    w.beat_end = l.inc(l.zext(
        l.either(l.b.slice(v.at("address"), 0, 12), l.b.slice(low_mask(v.at("size")), 0, 12)), 13));
    w.page = l.b.slice(widen(v.at("address")), 12, wide - 12);
    return w;
  }
  // The plan of the read packet on the down face (read_request).
  struct ReadPlan {
    ValueId cut_now{}, pack_now{}, cut_eff{}, pack_plan{}, cur_start{}, resumed{}, end{},
        fold_size{}, exact{}, wrap_now{}, wrap_plan{};
    Container box{};
    Fields view;
    Packet packet{};
    Packed packed{};
    // The view's fields an admission miss moves, and the rest of the plan.
    auto refs() {
      return with_parts({&cut_now, &pack_now, &cut_eff, &pack_plan, &cur_start, &resumed, &end,
                         &fold_size, &exact, &wrap_now, &wrap_plan, &view.at("start_address"),
                         &view.at("address")},
                        box, packet, packed);
    }
  };
  // With source_miss, a request's own addresses (and write AxLEN) from the hint face in place of
  // the up face's, which the source moves on an admission miss.
  Fields own_fields(Fields f, bool write) {
    if (write && write_length_hint) {
      f["start_address"] = *write_start_hint, f["address"] = *write_address_hint;
      f["length"] = *write_length_hint;
    } else if (!write && read_start_hint && p.source_miss) {
      f["start_address"] = *read_start_hint;
      f["address"] = narrow_address(floor_to(widen(*read_start_hint), f.at("size")));
    }
    return f;
  }
  // The down face of a write beat: its fields, the WRAP container mark taken from its own
  // descriptor (a packet, a pack, a block or a miss may change it; message.hpp point 4).
  Fields down_beat(const Fields &wn) {
    Fields wo;
    for (const auto &[name, width] : io.fields(p.data_bits, true))
      wo[name] = wn.at(name);
    wo["wrap_end"] = wrap_end_of(l, wn.at("write"), wn.at("burst"), wn.at("length"), wn.at("size"),
                                 wn.at("address"));
    wo["ends"] = beat_ends(l, wn.at("address"), wn.at("size"), bytes);
    return wo;
  }
  // A packet counts as its last beat leaves, a decode miss or refused packet as its head does (or,
  // with miss_entered, as it passes the network entry); the fragments after a head need no room.
  // Without miss_drop the splitter drops them itself (dropped) and the head is the whole packet.
  ValueId miss_counts(ValueId begins, ValueId gap, ValueId &offer) {
    const bool drops = misses || refusals;
    dropped = drops && !p.miss_drop ? l.both(l.inv(begins), gap) : zero;
    if (!drops)
      return packet_last;
    // A miss head with fragments behind it, counted at the network entry with miss_entered.
    miss_head = l.both(l.both(begins, gap), l.inv(packet_last));
    offer = l.either(offer, io.active(l.both(l.inv(begins), gap)));
    return l.pick(gap, miss_entered ? l.both(begins, packet_last) : begins, packet_last);
  }
  // A write packet (not a decode miss) into a target that refuses writes: it leaves as its first
  // beat alone with AxLEN 0, its other beats dropped as a decode miss's are.
  ValueId refused(ValueId gap, const Key &at) {
    if (!refusals)
      return zero;
    return l.both(l.inv(gap),
                  per_window(
                      at, zero, [&](const Axi4PacketWindow &w) { return w.refused ? one : zero; },
                      [](const Axi4PacketWindow &w) { return w.refused; }));
  }
  // Without miss_drop, w.swallow marks a miss packet whose later beats the splitter drops.
  void swallow(ValueId gap) {
    if (!(misses || refusals) || p.miss_drop)
      return;
    write_dropping = l.state("w.swallow", 1);
    l.b.update(*write_dropping, l.pick(load, l.both(gap, l.inv(packet_last)), *write_dropping));
  }
  // The write plan; with the source's hints a moved request (the error flag) plans from the
  // source's own addresses, each packet a decode miss through the fallback.
  WritePlan write_outcomes(const Key &start_key, const Key &beat_key, ValueId opening,
                           ValueId remaining) {
    const auto plan = plan_write(wi, start_key, beat_key, opening, remaining);
    write_key = plan.start;
    write_moved = write_length_hint ? wi.at("error") : zero;
    return plan;
  }
  // The down packet's plan fields on the hint face (Axi4WidthCutProfile::write_hints, read_hints).
  void hand_plan(const std::string &direction, std::initializer_list<ValueId> values) {
    static constexpr std::array names{"start", "key",  "moved",  "end",
                                      "size",  "beat", "length", "address"};
    for (std::size_t k = 0; const auto v : values)
      io.out("hint", direction + "_packet_" + names[k++], v);
  }
  // The write side's: the packet beginning now (only a packet's first beat is read). A folded
  // start shares its window with the packet's start; a packed word's address differs from its
  // beat's, but its words fit the target word.
  // A block smaller than the beat is a one-beat burst of its size.
  // A decode miss is one four-byte fragment: its run ends after the word holding its start; a
  // refused packet is its first beat, of the size handed (a folded or sub-beat size included).
  void hand_write_plan(ValueId beat_start, const WritePlan &plan, ValueId gap, ValueId refusing,
                       ValueId sub) {
    const auto folded = l.pick(plan.fold, plan.fold_size, wi.at("size"));
    const auto size = exact_any ? l.pick(sub, plan.packet.sub_size, folded) : folded;
    const auto beat = exact_any ? l.pick(sub, plan.packet.sub_size, wi.at("size")) : wi.at("size");
    const auto whole_wrap = l.both(l.eq(wi.at("burst"), 2), l.inv(l.either(plan.cut, plan.pack)));
    auto end =
        packet_end(wi.at("burst"), whole_wrap, plan.start, wi.at("size"), plan.box, plan.packed);
    if (refusals) {
      // low_mask(size) as a page offset; a folded size's from its container (folded_size).
      const auto mask_of = [&](ValueId s) { return l.fit(low_mask(s), 13); };
      auto mask = l.pick(plan.fold, covered_below(container_of(wi)), mask_of(wi.at("size")));
      if (exact_any)
        mask = l.pick(sub, mask_of(plan.packet.sub_size), mask);
      end = l.pick(refusing, l.inc(l.either(l.page_offset(widen(beat_start)), mask)), end);
    }
    end = l.pick(gap, l.inc(l.either(l.page_offset(widen(beat_start)), l.num(13, 3))), end);
    const auto none = refusals ? l.either(gap, refusing) : gap;
    const auto length = exact_any ? l.pick(sub, l.num(lb, 0), wi.at("length")) : wi.at("length");
    hand_plan("write", {beat_start, write_key, write_moved, end, l.pick(gap, l.num(3, 2), size),
                        l.pick(gap, l.num(3, 2), beat), l.pick(none, l.num(lb, 0), length),
                        exact_any ? l.pick(sub, plan.start, wi.at("address")) : wi.at("address")});
  }
  // A block smaller than the beat: one beat of its size at its start, with its own strobes.
  void sub_beat(Fields &wn, const Packet &packet, ValueId start, ValueId sub) {
    // The block's lanes from lane 0: 2^sub_size strobes, shifted to its start.
    const auto ones = l.per_size(packet.sub_size, lw, [&](unsigned n) {
      const unsigned k = 1U << n;
      return l.fit(l.b.constant(k, k >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << k) - 1),
                   bytes);
    });
    const auto be = l.both(wn.at("be"), l.shl(ones, l.b.slice(start, 0, lw), bytes - 1));
    wn["be"] = l.pick(sub, be, wn.at("be"));
    wn["data"] = l.pick(sub, l.both(wn.at("data"), strobe_lanes(l.b, be, bytes)), wn.at("data"));
    wn["length"] = l.pick(sub, l.num(lb, 0), wn.at("length"));
    wn["size"] = l.pick(sub, packet.sub_size, wn.at("size"));
    wn["address"] = l.pick(sub, start, wn.at("address"));
    wn["count"] = count_of(wn.at("size"));
  }
  // Exact packets: a first beat partly sent (its next block's start) and the burst's mark.
  std::optional<ValueId> write_sub, write_sub_start, write_exact;
  // Write-side handles handed from write_request to write_response.
  Fields wi, wb;
  // The packet start before a miss moves it (the key handed to the width cut), and the move.
  ValueId write_key{}, write_moved{};
  ValueId up_b_ready{};
  ValueId w_count{};
  std::vector<ValueId> w_used, w_opened, w_split, w_pieces;
  // counted: the beat's packet counts as it leaves (its last beat; a decode miss packet's head).
  ValueId needs{}, wsplit_now{}, load{}, fills{}, packet_last{}, counted{}, dropped{}, sent{},
      miss_head{};

  // Read-side handles handed from read_request to read_response.
  Fields ri, rr, d;
  std::vector<ValueId> r_used;
  ValueId up_r_ready{}, busy{}, first_packet{}, r_count{}, rsent{};
  ValueId rcut_now{}, rfold_now{}, rpack_now{}, rsplit_now{}, replay_now{};
  ValueId serial_now{}, rsplit{}, rcut_eff{}, pack_plan{}, rwrap_now{}, wrap_plan{};
  ValueId len_now{}, size_now{}, start_now{}, final_now{}, end_now{};
  ValueId rexact_now{}, rsub{}, rsub_size{};
  ValueId rq_valid{}, rq_fire{}, ri_ready{}, rload{}, rr_valid{};
  // The window decodes of the burst's start and of the packet on the down face.
  Key rstart_key{}, cur_key{};
  // The plan view (the up face while idle, else the descriptor), the fold mark and the decode miss.
  Fields d_view;
  ValueId rfold_held{}, read_gap{}, read_fold_size{};
  ValueId read_key{}, read_moved{};
  // The beats after a decode miss packet's head are being dropped: at the network entry
  // (Axi4PacketizerProfile::miss_drop), else here (w.swallow); the entry passes such a head
  // (miss_entered: with one burst at a time the packet counts there, so that a B waits only while
  // its packet is alone in flight past the entry).
  std::optional<ValueId> write_dropping, miss_entered;

  void write_request();
  void write_response();
  void read_request();
  ValueId read_word_data(ValueId data, const Fields &c, ValueId offset, ValueId answered,
                         ValueId position) {
    const unsigned width = lb + lw + 1;
    const auto here = l.zext(offset, width);
    const auto before = l.shl(l.zext(answered, width), c.at("size"), lw);
    const auto left = l.minus(c.at("length"), answered);
    const auto low = l.dec(l.pow2(c.at("size"), width, lw));
    const auto begin = l.pick(l.below(here, before), l.num(width, 0), l.minus(here, before));
    const auto end = l.plus(l.floor(here, l.inc(low)),
                            l.either(l.shl(l.zext(left, width), c.at("size"), lw), low));
    const auto lane = [&](unsigned k) {
      if (exact_any) {
        // Block-capable contexts retain the original byte interval.
        const auto address = l.plus(l.floor(position, l.num(13, bytes)), l.num(13, k));
        return l.both(l.inv(l.below(address, c.at("start"))), l.below(address, c.at("end")));
      }
      return l.both(l.inv(l.below(l.num(width, k), begin)), l.inv(l.below(end, l.num(width, k))));
    };
    auto lanes = lane(0);
    for (unsigned k = 1; k < bytes; ++k)
      lanes = l.b.concat(lane(k), lanes);
    return l.both(data, strobe_lanes(l.b, lanes, bytes));
  }
  void read_response();
  void read_packet();
};

void PacketizerLowering::write_request() {
  // ---------------- write: a beat or packed word leaves in the cycle it is offered and the up
  // face waits for the down face's ready; packets follow each other without waiting for their B
  wi = own_fields(io.inputs("up_wreq", p.data_bits, true), true);
  wb = io.inputs("down_wrsp", p.data_bits, false);
  const auto down_ready = io.in("down_wreq", "ready", 1);
  up_b_ready = io.in("up_wrsp", "ready", 1);
  // count: context packets whose last beat has left and whose B has not come back.
  w_count = piece_count::counter(l, "w.count");
  auto opening = l.state("w.opening", 1); // the next beat starts a packet
  auto end = l.state("w.end", wide), remaining = l.state("w.remaining", lb + 1);
  auto plen = l.state("w.length", lb), pstart = l.state("w.start", ab);
  auto psize = l.state("w.size", 3);
  // Reassembly contexts as on the read side (at capacity one every burst takes the one): a burst
  // needing one waits for a vacant context and packet room, never for older bursts' B. A context
  // is open while its beats leave; a packet counts once its last beat has left, so a full count
  // never stops a packet halfway.
  w_used = std::vector<ValueId>(contexts);
  w_opened = std::vector<ValueId>(contexts);
  w_split = std::vector<ValueId>(contexts, zero);
  ValueId vacant = zero, open_any = zero, open_split = zero;
  for (unsigned k = 0; k < contexts; ++k) {
    const auto name = "w.context" + std::to_string(k);
    vacant = l.either(vacant, l.inv(w_used[k] = l.state(name + ".used", 1)));
    open_any = l.either(open_any, w_opened[k] = l.state(name + ".open", 1));
    if (multi) {
      w_split[k] = l.state(name + ".split", 1);
      open_split = l.either(open_split, l.both(w_opened[k], w_split[k]));
    }
  }
  // The burst's start and the beat's address decode from the source's own addresses (Key).
  const auto start_key = Key{start_of(wi), wi.at("error")};
  const auto beat_key = Key{wi.at("address"), wi.at("error")};
  needs =
      multi ? l.bind("w.serial_class", serial_request(wi, write_lock_hint, start_key, false)) : one;
  const auto room = piece_count::room(l, w_count);
  // A narrow-split burst's pieces take no piece room: the shared pool at the request merge
  // counts every such piece on its own.
  wsplit_now = multi && p.split_narrow
                   ? split_one(wi.at("burst"), wi.at("size"), wi.at("cache"), *write_lock_hint)
                   : zero;
  const auto admitted =
      l.bind("w.burst_admit", l.either(l.inv(needs), l.both(vacant, l.either(room, wsplit_now))));
  // A later packet of the burst into another group waits for the earlier packets' B (several
  // bursts: those of its open context).
  ValueId group = zero, apart = zero, waiting = l.inv(l.eq(w_count, 0));
  w_pieces = std::vector<ValueId>(contexts, w_count);
  if (p.serialize) {
    group = l.state("w.group", gb);
    for (unsigned k = 0; multi && k < contexts; ++k) {
      w_pieces[k] = piece_count::counter(l, "w.context" + std::to_string(k) + ".count", piece_bits);
      const auto open_k = l.both(w_opened[k], l.inv(l.eq(w_pieces[k], 0)));
      waiting = k ? l.either(waiting, open_k) : open_k;
    }
    apart = l.bind("w.apart",
                   l.both(l.both(opening, waiting), l.inv(l.same(group_of(beat_key), group))));
  }
  // Exact packets: the first beat goes out once per block before it is taken.
  if (exact_any) {
    write_sub = l.state("w.sub", 1), write_sub_start = l.state("w.sub_start", ab);
    write_exact = l.state("w.exact", 1);
  }
  // The burst's first packet (not a later block of its first beat).
  const auto heading = exact_any ? l.both(wi.at("first"), l.inv(*write_sub)) : wi.at("first");
  // The up face may hand a beat over (the down face's ready aside).
  auto offer = io.active(
      l.both(p.serialize ? l.inv(apart) : one,
             multi ? l.pick(heading, admitted, l.pick(open_any, l.either(room, open_split), one))
                   : l.both(room, l.either(open_any, vacant))));
  const auto begins = l.either(wi.at("first"), opening);
  const auto rem =
      l.pick(wi.at("first"), l.plus(l.zext(wi.at("length"), lb + 1), l.num(lb + 1, 1)), remaining);
  const auto plan = write_outcomes(start_key, beat_key, opening, remaining);
  const auto start = plan.start;
  const auto wcut = l.bind("w.cut", plan.cut);
  const auto wfold = plan.fold, wpack = plan.pack;
  const auto packet = plan.packet;
  const auto packed = plan.packed;
  // The packet of the beat: its end and its descriptor.
  const auto length_now = l.pick(wpack, packed.length, packet.length);
  const auto psize_now = l.pick(wpack, packed.size, wi.at("size"));
  // Only a folded WRAP, always packed, moves its start: the packed start needs no packing decision.
  const auto pstart_now = packed.start;
  const auto beat_length = l.pick(begins, length_now, plen);
  const auto beat_size = l.pick(begins, psize_now, psize);
  const auto beat_start = l.pick(begins, pstart_now, pstart);
  const auto beat_end = plan.beat_end;
  // A later beat ends the packet when its end reaches the registered end, compared as page offsets.
  const auto end_offset =
      l.b.concat(l.inv(l.same(l.b.slice(end, 12, wide - 12), plan.page)), l.b.slice(end, 0, 12));
  packet_last =
      l.either(wi.at("last"),
               l.pick(begins, packet.first_last,
                      l.both(l.inv(l.same(end, unlimited)), l.inv(l.below(beat_end, end_offset)))));
  const auto inner_now = l.both(packet_last, l.inv(wi.at("last")));
  // Packed beats collect into one source word by strobe; the word goes out when the next beat
  // leaves it (a folded WRAP: after its last beat) or the packet ends.
  const auto wword = pack_of(start_key);
  // Every beat of a decode miss packet leaves at once as one aligned four-byte fragment (the first
  // its head, see miss_counts). The window: the packet start's decode as it begins, else pstart's.
  const auto window_key = beat_key.at(pstart_now, std::pair{l.inv(begins), pstart});
  const auto [wgap, wmoved] = misses ? missed(beat_start, window_key) : std::pair{zero, beat_start};
  const auto wrefused = refused(wgap, window_key),
             wrefuse = refusals ? l.either(wgap, wrefused) : wgap;
  const auto word_done =
      l.either(l.either(packet_last, wrefuse),
               l.both(l.inv(wfold), l.eq(l.both(beat_end, l.b.slice(low_mask(wword), 0, 13)), 0)));
  // The beat fills its word (or needs none): it leaves now, so the up face waits for the down
  // face's ready; a beat that only adds to the open word is taken at once.
  counted = miss_counts(begins, wrefuse, offer);
  const auto fills_now = l.both(l.either(l.inv(wpack), word_done), l.inv(dropped));
  // A block ending inside the first beat leaves without taking the beat.
  const auto partial = exact_any ? l.both(begins, packet.partial) : zero;
  auto ready = l.both(offer, l.either(l.inv(fills_now), down_ready));
  if (exact_any)
    ready = l.both(ready, l.inv(partial));
  load = l.bind("w.load", l.both(wi.at("valid"), ready));
  fills = l.both(load, fills_now);
  const auto starting = l.both(load, begins);
  if (p.serialize)
    l.b.update(group, l.pick(starting, group_of(beat_key.at(start)), group));
  l.b.update(end, l.pick(starting, packet.end, end));
  l.b.update(plen, l.pick(starting, length_now, plen));
  l.b.update(psize, l.pick(starting, psize_now, psize));
  l.b.update(pstart, l.pick(starting, pstart_now, pstart));
  l.b.update(remaining, l.pick(load, l.minus(rem, l.num(lb + 1, 1)), remaining));
  auto open = l.state("w.open", 1), opened_first = l.state("w.open_first", 1);
  auto acc_data = l.state("w.word_data", p.data_bits), acc_be = l.state("w.word_be", bytes);
  auto acc_err = l.state("w.word_error", 1);
  const auto lanes = strobe_lanes(l.b, wi.at("be"), bytes);
  const auto kept_data = l.pick(open, acc_data, l.num(p.data_bits, 0));
  const auto word_data = l.either(l.both(kept_data, l.b.unary(Op::BitNot, lanes, p.data_bits)),
                                  l.both(wi.at("data"), lanes));
  const auto word_be = l.either(l.pick(open, acc_be, l.num(bytes, 0)), wi.at("be"));
  const auto word_err = l.either(l.both(open, acc_err), wi.at("error"));
  const auto word_first = l.either(begins, l.both(open, opened_first));
  const auto keep = l.both(load, l.inv(l.either(fills, dropped)));
  l.b.update(open, l.pick(load, keep, open));
  l.b.update(opened_first, l.pick(keep, word_first, opened_first));
  l.b.update(acc_err, l.pick(keep, word_err, acc_err));
  l.b.update(acc_data, l.pick(keep, word_data, acc_data));
  l.b.update(acc_be, l.pick(keep, word_be, acc_be));
  // The beat or word leaving now.
  Fields wn = wi;
  wn.erase("valid");
  wn["length"] = refusals ? l.pick(wrefused, l.num(lb, 0), beat_length) : beat_length;
  wn["start_address"] = beat_start;
  wn["first"] = word_first;
  wn["last"] = (misses || refusals) && !p.miss_drop ? l.either(packet_last, wrefuse) : packet_last;
  wn["burst"] = l.pick(l.either(wcut, wpack), l.num(2, 1), wi.at("burst"));
  wn["size"] = beat_size;
  wn["count"] = count_of(beat_size);
  wn["data"] = l.pick(wpack, word_data, wi.at("data"));
  wn["be"] = l.pick(wpack, word_be, wi.at("be"));
  wn["error"] = l.pick(wpack, word_err, wi.at("error"));
  // A packing word sits at its word; one narrower word at its covering size's container.
  wn["address"] = l.pick(
      wpack,
      narrow_address(l.pick(l.same(beat_size, wword), floor_to(widen(wi.at("address")), wword),
                            floor_to(widen(beat_start), beat_size))),
      wi.at("address"));
  if (misses) {
    for (auto [name, value] : {std::pair{"length", l.num(lb, 0)},
                               {"be", l.num(bytes, 0)},
                               {"size", l.num(3, 2)},
                               {"address", narrow_address(floor_to(widen(wmoved), l.num(3, 2)))}})
      wn[name] = l.pick(wgap, value, wn.at(name));
    wn["error"] = l.either(wgap, wn.at("error")), wn["start_address"] = wmoved;
    wn["count"] = count_of(wn.at("size"));
  }
  const auto sub = exact_any ? l.both(begins, packet.sub) : zero;
  if (exact_any)
    sub_beat(wn, packet, start, sub);
  // The burst's last packet: no source beat remains after this packet's.
  const auto final_packet_now = l.inv(packet.split);
  const auto final_packet = l.state("w.final_packet", 1);
  l.b.update(final_packet, l.pick(starting, final_packet_now, final_packet));
  const auto beat_final = l.pick(begins, final_packet_now, final_packet);
  const auto valid = l.both(wi.at("valid"), l.both(offer, fills_now));
  // The down beat leaves: a block of a first beat leaves without taking it.
  sent = exact_any ? l.both(valid, down_ready) : fills;
  if (exact_any) {
    l.b.update(*write_sub, l.pick(sent, partial, *write_sub));
    l.b.update(*write_sub_start,
               l.pick(l.both(sent, partial), narrow_address(packet.end), *write_sub_start));
    l.b.update(*write_exact,
               l.pick(l.both(l.either(load, sent), wi.at("first")), plan.exact, *write_exact));
  }
  // Exported marker: the valid down beat belongs to its burst's last packet.
  l.bind("w.burst_final_packet", l.both(valid, beat_final));
  const auto wo = down_beat(wn);
  l.b.update(opening, l.pick(load, l.both(l.either(fills, dropped), inner_now), opening));
  swallow(wrefuse);
  if (p.packet_hints)
    hand_write_plan(beat_start, plan, wgap, wrefused, sub);
  io.out("up_wreq", "ready", ready);
  io.out("down_wreq", "valid", valid);
  io.fields_out("down_wreq", wo, p.data_bits, true);
}

void PacketizerLowering::write_response() {
  // Responses: the B of one context come back in packet order; its final one finds it closed
  // with one packet left and carries the merge (network miss first) of the earlier ones. A B of
  // no context passes. With several bursts the down ready is the up face's, never the tag's. While
  // a decode miss packet's beats are dropped (write_dropping), w.miss_hold marks that a B of the
  // context with one packet in flight past the entry (with several bursts, any B) waits for the
  // drop to end; the source holds it ahead of any width conversion.
  const auto b_valid = io.active(wb.at("valid"));
  // A burst's first packet opens its context; every packet sent counts.
  const auto first_beat =
      exact_any ? l.both(l.both(l.either(load, sent), wi.at("first")), l.inv(*write_sub))
                : l.both(load, wi.at("first"));
  const auto staged = l.both(sent, counted);
  std::vector<ValueId> load_of(contexts), hit_of(contexts), done_of(contexts);
  ValueId mine = zero, final_any = zero, earlier = one, waits = zero;
  // With miss_entered a miss packet counts at the network entry: from its head leaving here until
  // then (w.miss_ahead) no B of its burst is the final one.
  const auto ahead = miss_entered ? l.state("w.miss_ahead", 1) : zero;
  if (miss_entered)
    l.b.update(ahead, l.pick(*miss_entered, zero, l.pick(l.both(sent, miss_head), one, ahead)));
  for (unsigned k = 0; k < contexts; ++k) {
    const auto name = "w.context" + std::to_string(k);
    load_of[k] = l.both(l.both(first_beat, needs), l.both(l.inv(w_used[k]), earlier));
    const auto tag = multi ? l.latch(name + ".tag", wi.at("tag"), load_of[k]) : zero;
    hit_of[k] = multi ? l.both(w_used[k], l.same(wb.at("tag"), tag)) : w_used[k];
    if (multi && !p.serialize)
      w_pieces[k] = piece_count::counter(l, name + ".count", piece_bits);
    const auto alone = l.both(hit_of[k], l.eq(w_pieces[k], 1));
    done_of[k] = l.both(alone, l.inv(l.either(w_opened[k], ahead)));
    waits = l.either(waits, alone);
    earlier = l.both(earlier, w_used[k]);
    mine = l.either(mine, hit_of[k]), final_any = l.either(final_any, done_of[k]);
  }
  auto b_ready = io.active(multi ? up_b_ready : l.either(l.inv(final_any), up_b_ready));
  // With several bursts the hold reads no response field: every B waits. The splitter's own drop
  // (w.swallow, ahead of a width conversion) holds the B here, as the conversion may answer it.
  auto b_passes = b_valid;
  if (write_dropping) {
    const auto hold = l.both(*write_dropping, multi ? one : waits);
    if (p.miss_drop)
      l.bind("w.miss_hold", hold);
    else
      b_ready = l.both(b_ready, l.inv(hold)), b_passes = l.both(b_passes, l.inv(hold));
  }
  ValueId held = l.num(2, 0), held_error = zero;
  ValueId current = zero, current_split = zero, hit_split = zero;
  for (unsigned k = 0; k < contexts; ++k) {
    const auto name = "w.context" + std::to_string(k);
    const auto load_k = load_of[k], hit = hit_of[k], done = done_of[k];
    if (multi)
      l.b.update(w_split[k], l.pick(load_k, wsplit_now, w_split[k]));
    const auto acc = l.state(name + ".result", 2), acc_error = l.state(name + ".error", 1);
    const auto fire = l.both(l.both(b_valid, b_ready), hit), absorb = l.both(fire, l.inv(done));
    const auto cur = l.either(load_k, w_opened[k]);
    if (multi) {
      piece_count::step(l, w_pieces[k], l.both(staged, cur), fire);
      current_split = l.either(current_split, l.both(cur, l.pick(load_k, wsplit_now, w_split[k])));
      hit_split = l.either(hit_split, l.both(hit, w_split[k]));
    }
    l.b.update(w_opened[k], l.pick(l.both(load, wi.at("last")), zero, cur));
    l.b.update(w_used[k], l.pick(load_k, one, l.pick(l.both(fire, done), zero, w_used[k])));
    l.b.update(acc, l.pick(load_k, l.num(2, 0), l.pick(absorb, merge(acc, wb.at("result")), acc)));
    l.b.update(acc_error, l.pick(load_k, zero,
                                 l.pick(absorb, l.either(acc_error, wb.at("error")), acc_error)));
    current = l.either(current, cur);
    held = l.pick(hit, acc, held), held_error = l.either(held_error, l.both(hit, acc_error));
  }
  const auto final_b = l.either(l.inv(mine), final_any), b_fire = l.both(b_valid, b_ready);
  auto counts = l.both(l.both(staged, current), l.inv(current_split));
  if (miss_entered)
    counts = l.either(counts, *miss_entered);
  piece_count::step(l, w_count, counts, l.both(l.both(b_fire, mine), l.inv(hit_split)));
  Fields wr = wb;
  wr["result"] = merge(held, wb.at("result"));
  wr["error"] = l.either(held_error, wb.at("error"));
  io.out("up_wrsp", "valid", l.both(b_passes, final_b));
  io.out("down_wrsp", "ready", b_ready);
  io.fields_out("up_wrsp", wr, p.data_bits, false);
}

void PacketizerLowering::read_request() {
  // ---------------- read: packets leave back to back; R passes through, its first/last counted
  ri = own_fields(io.inputs("up_rreq", p.data_bits, true), false);
  rr = io.inputs("down_rrsp", p.data_bits, false);
  const auto down_r_ready = io.in("down_rreq", "ready", 1);
  up_r_ready = io.in("up_rrsp", "ready", 1);
  busy = l.state("r.busy", 1);
  auto pending = l.state("r.pending", 1);
  // first_packet: the request on the down face is the burst's first packet. r.start/r.remaining
  // are the resume point: the start of the packet on the face and the beats from it on.
  first_packet = l.state("r.first_packet", 1);
  auto rstart = l.state("r.start", ab), rrem = l.state("r.remaining", lb + 1);
  r_count = piece_count::counter(l, "r.count"); // packets whose last R is out
  rstart_key = Key{start_of(ri), ri.at("error")};
  rfold_now = folds(ri, rstart_key, true);
  // A narrow-split burst's pieces take no piece room at a multi-burst source: the shared pool at
  // the request merge counts every such piece on its own. rsplit_now stays raw for replay_now.
  rsplit_now = p.split_narrow
                   ? split_one(ri.at("burst"), ri.at("size"), ri.at("cache"), *read_lock_hint)
                   : zero;
  // Reassembly contexts, one per burst needing one and at most one per packet in flight; a burst
  // needing one waits for a vacant context.
  r_used = std::vector<ValueId>(contexts, one);
  ValueId vacant = zero, admitted = one;
  serial_now = one;
  for (unsigned k = 0; multi && k < contexts; ++k)
    vacant =
        l.either(vacant, l.inv(r_used[k] = l.state("r.context" + std::to_string(k) + ".used", 1)));
  if (multi) {
    serial_now = l.bind("r.serial_class", serial_request(ri, read_lock_hint, rstart_key, true));
    admitted = l.bind("r.burst_admit", l.either(l.inv(serial_now), vacant));
  }
  ri_ready = multi ? io.active(l.both(l.inv(busy), admitted)) : io.active(l.inv(busy));
  rload = l.bind("r.load", l.both(ri.at("valid"), ri_ready));
  // The burst's descriptor; a read request carries no payload.
  d = l.latch_fields("r.descriptor", ri, rload, {"data", "be"});
  rr_valid = io.active(rr.at("valid"));
  // The packet on the down face is computed combinationally from the resume point: the accepted
  // request's fields in its load cycle, the r.start/r.remaining registers after it. The view
  // switches on the idle register, not on the load: an idle side loads exactly when the up face
  // is offered and taken, and offers nothing otherwise, so the plan starts from the up fields
  // without waiting for the handshake.
  const auto ridle = l.inv(busy);
  const auto rcut = l.state("r.cut", 1);
  const auto rfold = l.latch("r.fold", rfold_now, rload), rpack = l.state("r.pack", 1);
  const auto rwrap = align_any ? l.state("r.wrap_pack", 1) : zero;
  rsplit = multi ? l.latch("r.split", rsplit_now, rload) : zero;
  const auto rbox = top_of(start_of(d), container_of(d));
  const auto whole = l.plus(l.zext(ri.at("length"), lb + 1), l.num(lb + 1, 1));
  const auto cur_rem = l.pick(ridle, whole, rrem);
  // Later packets replan after the initiator may hold the next descriptor,
  // so the hint is kept with the burst.
  std::optional<ValueId> rlock_hint;
  if (read_lock_hint)
    rlock_hint = l.pick(ridle, *read_lock_hint, l.latch("r.lock_hint", *read_lock_hint, rload));
  // The plan of the down face's packet: from the up face's fields v while idle, else the held
  // burst.
  const auto rexact = exact_any ? l.state("r.exact", 1) : zero;
  auto read_plan = [&](const Fields &v, const Key &start_key) {
    ReadPlan r;
    r.exact = exact_any ? l.pick(ridle, exact_of(v, start_key, rlock_hint), rexact) : zero;
    r.cut_now = cut_of(v, start_key, true);
    r.wrap_now = wrap_packs(v, r.cut_now, start_key);
    r.pack_now = packs_of(v, r.cut_now, start_key, true);
    if (align_any)
      r.pack_now = l.either(r.pack_now, r.wrap_now);
    const auto box = top_of(start_of(v), container_of(v));
    r.box = {l.pick(ridle, box.base, rbox.base), l.pick(ridle, box.top, rbox.top),
             l.pick(ridle, box.last, rbox.last)};
    r.cur_start = l.pick(ridle, start_of(v), rstart);
    r.cut_eff = l.pick(ridle, r.cut_now, rcut);
    r.view = d;
    for (const auto *name : {"start_address", "length", "size", "burst", "address"})
      r.view[name] = l.pick(ridle, v.at(name), d.at(name));
    // The packet's window: the burst start's own decode while idle, else the resume point's.
    r.packet = packet_of(
        r.cur_start, start_key.at(start_of(v), std::pair{busy, rstart}), r.view.at("size"),
        r.view.at("burst"), l.pick(ridle, v.at("cache"), d.at("cache")), cur_rem,
        p.exclusive ? std::optional{l.pick(ridle, v.at("lock"), d.at("lock"))} : std::nullopt,
        rlock_hint, r.cut_eff, r.box, r.exact, true);
    r.wrap_plan = align_any ? l.pick(ridle, r.wrap_now, rwrap) : zero;
    const auto fold_plan = l.pick(ridle, rfold_now, rfold);
    r.packed =
        packed_of(r.view, r.cur_start, r.packet, fold_plan,
                  start_key.at(v.at("start_address"), std::pair{busy, d.at("start_address")}),
                  align_any ? l.either(fold_plan, r.wrap_plan) : fold_plan, true);
    r.pack_plan = l.pick(ridle, r.pack_now, rpack);
    r.fold_size = folded_size(container_of(r.view));
    r.end = packet_end(r.view.at("burst"),
                       l.both(l.eq(r.view.at("burst"), 2), l.inv(l.either(r.cut_eff, r.pack_plan))),
                       r.cur_start, r.view.at("size"), r.box, r.packed);
    // The resume point: the packet's end, or the container base after a split WRAP's top.
    r.resumed = narrow_address(
        l.pick(l.both(r.cut_eff, l.same(r.packet.end, r.box.top)), r.box.base, r.packet.end));
    return r;
  };
  // As on the write side, a moved request plans from the source's own addresses.
  // A held decode reads the error flag of the burst being sent.
  rstart_key.error = l.pick(ridle, ri.at("error"), d.at("error"));
  const auto rp = read_plan(ri, rstart_key);
  read_key = rp.cur_start;
  read_moved =
      read_start_hint && p.source_miss ? l.pick(ridle, ri.at("error"), d.at("error")) : zero;
  rcut_now = rp.cut_now, rpack_now = rp.pack_now, rcut_eff = rp.cut_eff, pack_plan = rp.pack_plan;
  rwrap_now = rp.wrap_now, wrap_plan = rp.wrap_plan;
  l.b.update(rcut, l.pick(rload, rcut_now, rcut));
  l.b.update(rpack, l.pick(rload, rpack_now, rpack));
  if (align_any)
    l.b.update(rwrap, l.pick(rload, rwrap_now, rwrap));
  if (exact_any)
    l.b.update(rexact, l.pick(rload, rp.exact, rexact));
  rexact_now = rp.exact;
  // A packed burst of several beats replays each returned word, unless split one per packet.
  replay_now = l.both(l.both(rpack_now, l.inv(l.eq(ri.at("length"), 0))),
                      p.split_narrow ? l.inv(rsplit_now) : one);
  const auto rpacket = rp.packet;
  const auto rpacked = rp.packed;
  // The down face's packet: the burst start's window while idle, else the resume point's.
  cur_key = rstart_key.at(start_of(ri), std::pair{busy, rstart});
  d_view = rp.view, rfold_held = rfold, read_fold_size = rp.fold_size;
  len_now = l.pick(pack_plan, rpacked.length, rpacket.length);
  size_now = l.pick(pack_plan, rpacked.size, d_view.at("size"));
  start_now = rpacked.start; // as on the write side, the packing decision leaves the start alone
  end_now = rp.end;
  // A block smaller than the beat: one beat of its size at its start.
  rsub = exact_any ? rpacket.sub : zero, rsub_size = rpacket.sub_size;
  if (exact_any)
    len_now = l.pick(rsub, l.num(lb, 0), len_now), size_now = l.pick(rsub, rsub_size, size_now);
  const auto after = l.minus(cur_rem, rpacket.beats);
  final_now = l.inv(rpacket.split); // the packet on the face ends the burst
  auto room = piece_count::room(l, r_count);
  // The load-cycle offer asks what a registered packet would (a narrow-split piece needs no room).
  const auto head_room = multi ? l.either(room, rsplit_now) : room;
  // r.group: the group of the packet sent last; a later packet's decodes from the r.start register.
  const auto rgroup = p.serialize ? l.state("r.group", gb) : zero;
  const auto rapart =
      p.serialize ? l.bind("r.apart", l.inv(l.same(group_of(Key{rstart}), rgroup))) : zero;
  // Several bursts: the packets of the burst being sent, counted from its load until their last R.
  rsent = p.serialize && multi ? piece_count::counter(l, "r.burst_count", piece_bits) : r_count;
  // A later packet into another group waits until the earlier packets are answered.
  if (p.serialize)
    room = l.both(room, l.either(l.inv(rapart), l.eq(rsent, 0)));
  // An idle side offers in the load cycle: the terms of the load, off its registers' fanout.
  rq_valid = io.active(l.pick(busy, l.both(pending, l.either(room, rsplit)),
                              l.both(ri.at("valid"), l.both(admitted, head_room))));
  rq_fire = l.both(rq_valid, down_r_ready);
  const auto resumed = rp.resumed;
  if (p.serialize)
    l.b.update(rgroup, l.pick(l.either(rq_fire, rload), group_of(cur_key), rgroup));
  l.b.update(rstart, l.pick(rq_fire, resumed, l.pick(rload, start_of(ri), rstart)));
  l.b.update(rrem, l.pick(rq_fire, after, l.pick(rload, whole, rrem)));
  l.b.update(first_packet, l.pick(rload, l.inv(rq_fire), l.pick(rq_fire, zero, first_packet)));
  l.b.update(pending, l.pick(rq_fire, l.inv(final_now), l.pick(rload, one, pending)));
}

void PacketizerLowering::read_response() {
  // Each context counts its burst's beats on its own state, never on the returning beat; others
  // pass between its packets and words. A packed word answers every source beat it holds (a folded
  // WRAP: all), followed by the beat's byte in its word. A word to answer again holds the down
  // face with one burst in flight, else waits in a register: the ready never depends on the tag.
  Fields up = rr, shape{{"word", pack_of(rstart_key, true)}};
  if (multi) {
    // Playback: 0 pass, 1 word replay, 2 folded WRAP, 3 packed INCR.
    const auto playback =
        l.pick(rfold_now, l.num(2, 2), l.pick(l.eq(ri.at("burst"), 1), l.num(2, 3), l.num(2, 1)));
    shape["playback"] = l.pick(replay_now, playback, l.num(2, 0));
  } else {
    // A sole response keeps its request descriptor until its last beat leaves.
    shape["fold"] = rfold_now, shape["replay"] = replay_now;
  }
  for (const auto *name : {"length", "size"})
    shape[name] = ri.at(name);
  ValueId full = zero, up_valid = rr_valid;
  if (multi) {
    full = l.state("r.word_full", 1);
    for (const auto &[name, v] : l.latch_fields("r.word", rr, l.inv(full)))
      up[name] = l.pick(full, v, rr.at(name));
    up_valid = l.either(rr_valid, io.active(full));
    shape["tag"] = ri.at("tag");
    shape["split"] = rsplit_now;
  }
  // Exact packets: the burst's bytes [start, end) as page offsets; a beat whose blocks are not done
  // gathers their data, results and error flags (only the first beat has several).
  constexpr unsigned pw = 13;
  const auto start_offset = exact_any ? l.page_offset(widen(start_of(ri))) : zero;
  if (exact_any) {
    shape["exact"] = rexact_now, shape["start"] = start_offset;
    shape["end"] = l.plus(
        l.both(start_offset, l.b.unary(Op::BitNot, l.b.slice(low_mask(ri.at("size")), 0, pw), pw)),
        l.shl(l.zext(l.plus(l.zext(ri.at("length"), lb + 1), l.num(lb + 1, 1)), pw), ri.at("size"),
              lw));
  }
  const auto r_fire = l.both(up_valid, up_r_ready);
  ValueId mine = zero, counted_last = zero, counted_first = zero, again = zero, earlier = one;
  ValueId split_hit = zero, gathering = zero;
  auto gathered = std::pair{up.at("data"), up.at("result")};
  auto gathered_error = up.at("error");
  for (unsigned k = 0; k < contexts; ++k) {
    const auto name = "r.context" + std::to_string(k);
    const auto load_k =
        multi ? l.both(l.both(rload, serial_now), l.both(l.inv(r_used[k]), earlier)) : rload;
    earlier = l.both(earlier, r_used[k]);
    const auto c = l.latch_fields(name, shape, load_k);
    const auto playing = multi ? l.inv(l.eq(c.at("playback"), 0)) : c.at("replay");
    const auto folding = multi ? l.eq(c.at("playback"), 2) : c.at("fold");
    // Only INCR and WRAP replay, so their captured burst high bit distinguishes them.
    const auto clipping =
        multi ? l.eq(c.at("playback"), 3) : l.both(playing, l.inv(l.b.slice(d.at("burst"), 1, 1)));
    auto answered = l.state(name + ".answered", lb);
    // Exact packets keep the beat's page offset; its low bits are the offset in the word.
    const auto position = exact_any ? l.state(name + ".position", pw) : zero;
    const auto offset = exact_any ? l.b.slice(position, 0, lw) : l.state(name + ".offset", lw);
    const auto hit = multi ? l.both(r_used[k], l.same(up.at("tag"), c.at("tag"))) : one;
    const auto here = l.zext(offset, lw + 1), stride = l.pow2(c.at("size"), lw + 1, lw);
    // Without block gathering, preserve the byte bias when advancing the replay mask.
    const auto next = l.plus(exact_any ? l.floor(here, stride) : here, stride);
    const auto fire = l.both(r_fire, hit);
    const auto last = l.same(answered, c.at("length"));
    // absorb: a block inside the beat that another block of the beat follows; its response is
    // gathered, not answered. cut: the next beat starts another block (a word answers no beat
    // past its block).
    ValueId absorb = zero, cut = zero;
    if (exact_any) {
      const auto span = l.pow2(c.at("size"), pw, lw), inside = l.dec(span);
      const auto lowest = [&](ValueId v) { return l.both(v, l.minus(l.num(pw, 0), v)); };
      const auto after = l.plus(position, lowest(position));
      const auto merging =
          l.both(l.both(hit, c.at("exact")), l.inv(l.eq(l.both(position, inside), 0)));
      absorb = l.both(merging, l.inv(l.eq(l.both(after, inside), 0)));
      const auto following = l.plus(l.floor(position, span), span), step = lowest(following);
      cut = l.both(c.at("exact"), l.either(l.below(l.minus(following, step), c.at("start")),
                                           l.below(c.at("end"), l.plus(following, step))));
      l.b.update(position, l.pick(load_k, start_offset,
                                  l.pick(fire, l.pick(absorb, after, following), position)));
      // Lanes from the beat's offset on come from this response, lower ones from earlier blocks.
      const auto lanes = strobe_lanes(
          l.b, l.shl(l.b.unary(Op::BitNot, l.num(bytes, 0), bytes), offset, bytes - 1), bytes);
      const auto data = l.state(name + ".gather", p.data_bits);
      const auto result = l.state(name + ".gather_result", 2);
      const auto flag = l.state(name + ".gather_error", 1);
      const auto joined = l.either(l.both(up.at("data"), lanes),
                                   l.both(data, l.b.unary(Op::BitNot, lanes, p.data_bits)));
      const auto joined_result = merge(result, up.at("result"));
      const auto joined_error = l.either(flag, up.at("error"));
      const auto take = l.both(fire, absorb);
      l.b.update(data, l.pick(load_k, l.num(p.data_bits, 0), l.pick(take, joined, data)));
      l.b.update(result, l.pick(load_k, l.num(2, 0), l.pick(take, joined_result, result)));
      l.b.update(flag, l.pick(load_k, zero, l.pick(take, joined_error, flag)));
      gathered = {l.pick(merging, joined, gathered.first),
                  l.pick(merging, joined_result, gathered.second)};
      gathered_error = l.pick(merging, joined_error, gathered_error);
      gathering = l.either(gathering, absorb);
    }
    gathered.first =
        l.pick(l.both(hit, clipping), read_word_data(gathered.first, c, offset, answered, position),
               gathered.first);
    const auto answer = exact_any ? l.both(fire, l.inv(absorb)) : fire;
    l.b.update(answered, l.pick(load_k, l.num(lb, 0), l.pick(answer, l.inc(answered), answered)));
    if (!exact_any)
      l.b.update(offset, l.pick(load_k, l.b.slice(start_of(ri), 0, lw),
                                l.pick(fire, l.b.slice(next, 0, lw), offset)));
    if (multi) {
      l.b.update(r_used[k], l.pick(load_k, one, l.pick(l.both(answer, last), zero, r_used[k])));
      split_hit = l.either(split_hit, l.both(hit, c.at("split")));
    }
    mine = l.either(mine, hit);
    counted_last = l.either(counted_last, l.both(hit, last));
    counted_first = l.either(counted_first, l.both(hit, l.eq(answered, 0)));
    auto replay = l.both(
        l.both(hit, l.both(playing, l.inv(last))),
        l.either(folding, l.same(l.shr(next, c.at("word"), lw), l.shr(here, c.at("word"), lw))));
    if (exact_any)
      replay = l.both(replay, l.inv(l.either(absorb, cut)));
    again = l.either(again, replay);
  }
  up["data"] = gathered.first;
  if (exact_any) {
    up["result"] = gathered.second, up["error"] = gathered_error;
    up_valid = l.both(up_valid, l.inv(gathering));
  }
  const auto hold = multi ? full : again;
  if (multi)
    l.b.update(full, l.pick(r_fire, l.both(mine, again), full));
  const auto taken = l.both(rr_valid, l.both(up_r_ready, l.inv(hold)));
  up["first"] = l.pick(mine, counted_first, up.at("first"));
  up["last"] = l.pick(mine, counted_last, up.at("last"));
  const auto req_serial =
      multi ? l.pick(rload, serial_now, l.latch("r.req_serial", serial_now, rload)) : one;
  // A packet leaving in the load cycle takes the new burst's narrow-split mark, not the held one.
  const auto sent_split = multi ? l.pick(rload, rsplit_now, rsplit) : zero;
  piece_count::step(l, r_count, l.both(l.both(rq_fire, req_serial), l.inv(sent_split)),
                    l.both(l.both(taken, l.both(mine, l.inv(split_hit))), rr.at("last")));
  if (p.serialize && multi) {
    // A load clears the count to whether the burst's first packet left in it.
    const auto rdec =
        l.both(l.both(l.both(taken, rr.at("last")), l.same(rr.at("tag"), d.at("tag"))),
               l.inv(l.eq(rsent, 0)));
    l.b.update(rsent,
               l.pick(rload, l.zext(rq_fire, piece_bits),
                      l.pick(l.both(rq_fire, l.inv(rdec)), l.plus(rsent, l.num(piece_bits, 1)),
                             l.pick(l.both(rdec, l.inv(rq_fire)),
                                    l.minus(rsent, l.num(piece_bits, 1)), rsent))));
  }
  const auto release =
      multi ? l.both(rq_fire, final_now)
            : l.both(exact_any ? l.both(r_fire, l.inv(gathering)) : r_fire, up.at("last"));
  l.b.update(busy, l.pick(rload, l.inv(multi ? l.both(rq_fire, final_now) : zero),
                          l.pick(release, zero, busy)));
  read_packet();
  io.out("up_rrsp", "valid", up_valid);
  io.out("down_rrsp", "ready", l.both(io.active(up_r_ready), l.inv(hold)));
  io.fields_out("up_rrsp", up, p.data_bits, false);
}

void PacketizerLowering::read_packet() {
  Fields ro = d;
  ro["address"] = l.pick(pack_plan, narrow_address(floor_to(widen(start_now), size_now)),
                         l.pick(first_packet, d.at("address"), start_now));
  ro["start_address"] = start_now;
  ro["length"] = len_now;
  ro["size"] = size_now;
  // A packed WRAP stays a WRAP; any other packed or cut packet is an INCR.
  const auto incr = align_any ? l.both(pack_plan, l.inv(wrap_plan)) : pack_plan;
  ro["burst"] = l.pick(l.either(rcut_eff, incr), l.num(2, 1), d.at("burst"));
  Fields now = ri; // the request of the load cycle
  now["address"] =
      l.pick(rpack_now, narrow_address(floor_to(widen(start_now), size_now)), ri.at("address"));
  now["start_address"] = start_now, now["length"] = len_now, now["size"] = size_now;
  const auto incr_now = align_any ? l.both(rpack_now, l.inv(rwrap_now)) : rpack_now;
  now["burst"] = l.pick(l.either(rcut_now, incr_now), l.num(2, 1), ri.at("burst"));
  ro = view_fields(l, ro, now, l.inv(busy));
  if (exact_any)
    ro["address"] = l.pick(rsub, start_now, ro.at("address"));
  if (misses) { // as on the write side, of four-byte beats
    // The down face's packet: the load cycle's, else the resume point's.
    const auto [rgap, rmoved] = missed(ro.at("start_address"), cur_key.at(ro.at("start_address")));
    // A moved request keeps its own beat address, moved likewise, at a four-byte word.
    read_gap = rgap;
    ro["size"] = l.pick(rgap, l.num(3, 2), ro.at("size"));
    auto address = rmoved;
    if (p.source_miss)
      address = l.pick(
          ro.at("error"),
          narrow_address(floor_to(widen(moved_by_source(ro.at("address"))), l.num(3, 2))), rmoved);
    ro["address"] = l.pick(rgap, address, ro.at("address"));
    ro["error"] = l.either(rgap, ro.at("error")), ro["start_address"] = rmoved;
  }
  ro["count"] = count_of(ro.at("size"));
  ro["wrap_end"] = l.num(1, 0); // a read (message.hpp point 4)
  ro["data"] = l.num(p.data_bits, 0);
  ro["be"] = l.num(bytes, 0);
  // Exported marker: the valid down request is its burst's last packet.
  l.bind("r.burst_final_packet", l.both(rq_valid, final_now));
  // A read of a granule window leaves the port pipe with its first packet, others with their last.
  if (granule_any) {
    const auto granular = per_window(
        cur_key, zero, [&](const Axi4PacketWindow &w) { return w.read_granule ? one : zero; },
        [](const Axi4PacketWindow &w) { return w.read_granule != 0; });
    l.bind("r.burst_taken_packet",
           l.both(rq_valid, l.pick(granular, l.either(rload, first_packet), final_now)));
  }
  io.out("up_rreq", "ready", ri_ready);
  io.out("down_rreq", "valid", rq_valid);
  io.fields_out("down_rreq", ro, p.data_bits, true);
  if (p.packet_hints) {
    auto size =
        l.pick(l.pick(l.inv(busy), rfold_now, rfold_held), read_fold_size, d_view.at("size"));
    auto beat = d_view.at("size");
    if (misses)
      size = l.pick(read_gap, l.num(3, 2), size), beat = l.pick(read_gap, l.num(3, 2), beat);
    auto length = d_view.at("length");
    if (exact_any) {
      size = l.pick(rsub, rsub_size, size), length = l.pick(rsub, l.num(lb, 0), length);
      beat = l.pick(rsub, rsub_size, beat);
    }
    hand_plan("read", {start_now, read_key, read_moved, end_now, size, beat, length});
  }
}

} // namespace

// Whether a window's limits break the profile contract (Axi4PacketWindow), for `bytes` source
// bytes.
static bool bad_limits(const Axi4PacketWindow &w, unsigned bytes) {
  return !std::has_single_bit(w.max_bytes) || !std::has_single_bit(w.align_bytes) ||
         w.align_bytes < bytes || w.max_bytes < w.align_bytes || w.boundary_bytes < w.align_bytes ||
         !std::has_single_bit(w.wrap_container_limit) || w.wrap_container_limit > 2048 ||
         !std::has_single_bit(w.pack_bytes) || w.pack_bytes > bytes || w.pack_bytes < 4 ||
         (w.read_pack_bytes &&
          (!std::has_single_bit(w.read_pack_bytes) || w.read_pack_bytes > w.pack_bytes));
}

Result<NodeHardware> lower_axi4_packetizer(const Axi4PacketizerProfile &p) {
  const unsigned bytes = p.data_bits / 8;
  const unsigned ab = p.address_bits;
  const unsigned tag_need = std::bit_width(p.max_outstanding - 1);
  if (!valid_name(p.name) || p.id_bits > 16 || (p.len_bits < 4 || p.len_bits > 8) ||
      p.source_bits > max_source_bits || p.tag_bits > 6 || p.max_outstanding < 1 ||
      p.max_outstanding > 64 || (p.max_outstanding > 1 && p.tag_bits < tag_need) || ab < 32 ||
      ab > 48 || !std::has_single_bit(p.data_bits) || p.data_bits < 32 || p.data_bits > 1024 ||
      std::ranges::any_of(p.windows,
                          [&](const auto &w) {
                            return !w.window.size || !std::has_single_bit(w.boundary_bytes) ||
                                   w.window.base % w.boundary_bytes ||
                                   w.window.size % w.boundary_bytes ||
                                   w.window.base + w.window.size > (std::uint64_t{1} << ab) ||
                                   w.boundary_bytes > w.window.size || bad_limits(w, bytes);
                          }) ||
      (p.fallback &&
       (!p.source_miss || !p.fallback->miss || p.fallback->exact ||
        !std::has_single_bit(p.fallback->boundary_bytes) || bad_limits(*p.fallback, bytes))))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_PACKETIZER_PROFILE", "axi4_packetizer",
              "Expected 32..48 address bits, 0..16 ID bits, 4..8 length bits, 0..9 source bits, "
              "32..1024 data bits, and windows made of whole boundary units whose packet limit "
              "and boundary are at least the alignment word, whose WRAP limit is a power of "
              "two up to 2048 bytes and whose packing word is 4 bytes to the source word; a "
              "fallback is a miss window of such limits beside a source miss window"));
  Logic l;
  PacketizerLowering engine(l, p);
  if (p.write) {
    engine.write_request();
    engine.write_response();
  }
  engine.read_request();
  engine.read_response();
  return l.finish("nocloom_axi4_packetizer_" + p.name);
}

std::pair<unsigned, unsigned> axi4_packetizer_state_bits(const Axi4PacketizerProfile &profile) {
  return direction_state_bits(lower_axi4_packetizer(profile));
}
} // namespace nocloom::detail
