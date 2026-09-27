#include "axi4_width_clamp.hpp"
#include "burst_faces.hpp"
#include "decode_packet.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "message.hpp"
#include "packet_context.hpp"
#include "piece_count.hpp"
#include "serial_class.hpp"
#include "strobe_lanes.hpp"
#include <algorithm>
#include <bit>
#include <functional>
#include <type_traits>

namespace nocloom::detail {
namespace {
using Fields = std::map<std::string, ValueId>;
using logic::Logic;

bool source_width(unsigned bits) {
  return bits == 64 || bits == 128 || bits == 256 || bits == 512 || bits == 1024;
}

// Whether the FIXED refusal windows are exactly the routing windows narrower than the source, the
// routing windows disjoint: a burst wider than its target then lies in a refusal window.
bool reject_by_width(const Axi4WidthCutProfile &p) {
  const unsigned lw = std::bit_width(p.data_bits / 8) - 1;
  std::vector<AddressWindow> narrow, all;
  for (const auto &[window, bits] : p.windows) {
    all.push_back(window);
    if (bits < lw)
      narrow.push_back(window);
  }
  auto order = [](std::vector<AddressWindow> v) {
    std::ranges::sort(v, {}, [](const auto &w) { return std::pair{w.base, w.size}; });
    return v;
  };
  const auto sorted = order(all);
  for (std::size_t k = 1; k < sorted.size(); ++k)
    if (sorted[k].base < sorted[k - 1].base + sorted[k - 1].size)
      return false;
  return !p.fixed_width && order(narrow) == order(p.fixed_reject);
}

// One width-cut lowering: the builder, the profile and the shared derived
// constants, plus the handles the phases hand each other.
struct WidthCutLowering {
  WidthCutLowering(Logic &logic, const Axi4WidthCutProfile &profile)
      : l(logic), p(profile), bytes(p.data_bits / 8), lw(std::bit_width(bytes) - 1),
        ab(p.address_bits), lb(p.len_bits), wide(ab + 1), narrow_face(p.down_bits != p.data_bits),
        multi(p.max_outstanding > 1), contexts(p.max_outstanding * p.packet_depth),
        zero(l.bind("c.zero", l.bit(0))), one(l.bind("c.one", l.bit(1))),
        burst{p.id_bits, lb, true, ab}, count_bits(burst_count_bits(p.data_bits)),
        down_count_bits(burst_count_bits(p.down_bits)), io{l, burst, p.source_bits},
        by_width(reject_by_width(p)) {
    burst.exclusive = p.exclusive;
    burst.tag_bits = p.tag_bits;
    burst.user_bits = p.user_bits;
    burst.response_user_bits = p.response_user_bits;
  }

  Logic &l;
  const Axi4WidthCutProfile &p;
  const unsigned bytes, lw, ab, lb;
  const unsigned wide; // run ends may reach the top of the address space
  const bool narrow_face, multi;
  const unsigned contexts;
  const ValueId zero, one;
  BurstProfile burst;
  const unsigned count_bits, down_count_bits;
  BurstFaces io;
  // The FIXED refusal follows from the window width (reject_by_width).
  const bool by_width;

  ValueId widen(ValueId v) { return l.zext(v, wide); }
  // Offsets in a 4 KiB page, the page top included.
  static constexpr unsigned page_bits = 13;
  // Value tables over a small field: select(field == k, table(k), ...).
  // A select tree over the field bits (a chain for a field wider than four bits).
  template <class F> ValueId table(ValueId field, unsigned last, F &&arm) {
    if (l.width(field) <= 4) {
      std::vector<ValueId> arms;
      for (unsigned k = 0; k < (1U << l.width(field)); ++k)
        arms.push_back(arm(std::min(k, last)));
      return l.by_index(field, arms);
    }
    ValueId value = arm(last);
    for (unsigned k = last; k-- > 0;)
      value = l.pick(l.eq(field, k), arm(k), value);
    return value;
  }
  ValueId low_mask(ValueId k, unsigned width) { // 2^k - 1
    return table(k, lw, [&](unsigned n) { return l.num(width, (std::uint64_t{1} << n) - 1); });
  }
  ValueId power(ValueId k, unsigned width) {
    return table(k, lw, [&](unsigned n) { return l.num(width, std::uint64_t{1} << n); });
  }
  ValueId floor_to(ValueId address, ValueId k) {
    const auto width = l.width(address);
    return l.both(address, l.b.unary(Op::BitNot, low_mask(k, width), width));
  }
  ValueId shift_left(ValueId value, ValueId k) {
    const auto width = l.width(value);
    return table(k, lw, [&](unsigned n) {
      return n ? l.b.concat(l.b.slice(value, 0, width - n), l.num(n, 0)) : value;
    });
  }
  ValueId shift_right(ValueId value, ValueId k) {
    const auto width = l.width(value);
    return table(k, lw, [&](unsigned n) {
      return n ? l.zext(l.b.slice(value, n, width - n), width) : value;
    });
  }
  ValueId merge(ValueId a, ValueId b) { return piece_count::merge(l, a, b); }
  // The offset in its source beat of a burst's first target word.
  ValueId start_offset(ValueId start, ValueId c, ValueId size) {
    return l.b.slice(l.both(floor_to(start, c), low_mask(size, ab)), 0, lw);
  }
  // The target word width of a burst: the routing window of its start.
  ValueId width_of(ValueId start) {
    if (p.fixed_width)
      return l.num(3, *p.fixed_width);
    // The first window holding the start, its width shared by the windows of that width.
    std::vector<AddressWindow> windows;
    std::vector<unsigned> classes;
    std::vector<ValueId> widths;
    for (const auto &[window, bits] : p.windows) {
      windows.push_back(window);
      classes.push_back(bits);
    }
    for (unsigned bits = 0; bits <= lw; ++bits)
      widths.push_back(l.num(3, bits));
    return l.window_class(widen(start), windows, classes, widths, l.num(3, lw));
  }
  // The target word widths width_of gives: the fixed one, else the windows' and the source's own.
  std::vector<unsigned> width_set() const {
    if (p.fixed_width)
      return {*p.fixed_width};
    std::vector<unsigned> out{lw};
    for (const auto &[window, bits] : p.windows)
      out.push_back(bits);
    std::ranges::sort(out);
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
  }
  // A held target width: one bit when width_of gives two widths.
  ValueId width_state(const std::string &name) {
    const auto set = width_set();
    return set.size() == 2 ? l.b.two_valued(l.state(name, 1), 3, set[0], set[1]) : l.state(name, 3);
  }
  // A held target word address (floor_to of a target width): the bits below the narrowest
  // target word are zero.
  ValueId word_state(const std::string &name, unsigned width) {
    const auto low = std::min(width_set().front(), width);
    return low ? l.b.zero_low(l.state(name, width - low), low) : l.state(name, width);
  }
  // Burst plan, taken from a burst's first fragment (the descriptor and the
  // raw start travel in every fragment). mode 0 pass, 1 cut, 2 local SLVERR.
  // end is the end of the run the pieces walk up to (the burst end for INCR,
  // the beat container end for FIXED, the container top for WRAP), as an
  // offset in the burst's 4 KiB page (13 bits, the page top included); a piece
  // below the start word ends at the start word instead (the second run of a
  // WRAP). restart is where the next run starts once a run ends.
  struct Plan {
    ValueId c, mode, wrap, wrap_words, end, start_word, restart, reject;
    auto refs() { return std::vector{&c, &mode, &wrap, &wrap_words, &end, &start_word, &restart}; }
  };
  // A packet's plan fields from the packet splitter beside the up face (write_hints, read_hints):
  // start, run end (run_end), beat size and the burst's length; key: the start before the source
  // moves an admission miss and moved its move, which give the target width; the write beat's
  // address.
  // beat: the size before a fold, which only a WRAP takes and which never refuses a burst.
  struct Handed {
    ValueId start{}, end{}, size{}, length{}, key{}, moved{}, beat{};
    std::optional<ValueId> address{};
  };
  Handed handed_plan(const std::string &direction) {
    auto hint = [&](const std::string &field, unsigned width) {
      const auto signal = direction + "_packet_" + field;
      return l.b.input("nl_hint_" + signal, width, "hint", signal);
    };
    Handed out{hint("start", ab), hint("end", page_bits), hint("size", 3), hint("length", lb),
               hint("key", ab),   hint("moved", 1),       hint("beat", 3)};
    if (direction == "write")
      out.address = hint("address", ab);
    return out;
  }
  Plan plan_of(const Fields &f, std::optional<Handed> handed = std::nullopt) {
    Plan r;
    const auto start = handed ? handed->start : f.at("start_address"),
               size = handed ? handed->size : f.at("size"),
               length = handed ? handed->length : f.at("length"), kind = f.at("burst");
    r.c = width_of(start);
    if (handed) {
      r.c = width_of(handed->key);
      if (p.source_miss)
        r.c = l.pick(handed->moved, width_of(l.moved_to_miss(*p.source_miss, handed->key)), r.c);
    }
    const auto split = l.below(r.c, size);
    // Beats, and below their target words, in a width that holds len + 1 shifted by any size.
    const unsigned beat_bits = lb + 1 + lw;
    const auto beats = l.plus(l.zext(length, beat_bits), l.num(beat_bits, 1));
    const auto is_wrap = l.eq(kind, 2), is_fixed = l.eq(kind, 0);
    r.end = handed ? handed->end : run_end(l, start, size, length, kind, lw);
    const auto wrap_base = l.both(
        start, l.b.unary(Op::BitNot,
                         l.either(shift_left(l.zext(length, ab), size), low_mask(size, ab)), ab));
    r.start_word = floor_to(start, r.c);
    r.restart = l.pick(is_wrap, wrap_base, r.start_word);
    // WRAP over its container: 2^(size - c) target words per beat.
    const auto words = shift_left(beats, l.minus(size, r.c));
    const auto wrap_ok = l.either(l.either(l.eq(words, 2), l.eq(words, 4)),
                                  l.either(l.eq(words, 8), l.eq(words, 16)));
    r.wrap = l.both(is_wrap, wrap_ok);
    r.wrap_words = l.b.slice(words, 0, lb + 1);
    // A FIXED burst wider than its target is never cut: in a target window
    // it fails locally; outside them (decode first) its pieces miss.
    std::vector<ValueId> fixed_hits;
    if (!by_width)
      std::ranges::transform(p.fixed_reject, std::back_inserter(fixed_hits), [&](const auto &w) {
        return l.both(l.inv(l.below(widen(start), l.num(wide, w.base))),
                      l.below(widen(start), l.num(wide, w.base + w.size)));
      });
    // The size before a fold decides the refusal: a fold's size and its beat's are both within the
    // source word, and only a WRAP folds. An exclusive burst's refusal reads the cut itself.
    const auto beat = handed && !p.exclusive ? handed->beat : size;
    const auto beat_split = l.below(r.c, beat);
    ValueId reject = l.below(l.num(3, lw), beat);
    if (by_width && !p.fixed_reject.empty())
      reject = l.either(reject, l.both(is_fixed, beat_split));
    else if (!fixed_hits.empty())
      reject = l.either(reject, l.both(l.both(is_fixed, beat_split), l.any(fixed_hits)));
    // An exclusive burst is never cut: it fits one target burst or fails.
    if (p.exclusive) {
      const auto cut = l.pick(
          is_wrap, l.inv(wrap_ok),
          l.either(
              l.inv(page_piece(r.start_word, r.c, zero, r.wrap_words, r.end, r.start_word).fits),
              l.both(is_fixed, l.inv(l.eq(length, 0)))));
      reject = l.either(reject, l.both(f.at("lock"), l.both(split, cut)));
    }
    r.reject = reject;
    r.mode = l.pick(reject, l.num(2, 2), l.pick(split, l.num(2, 1), l.num(2, 0)));
    return r;
  }
  // The piece that starts at `at`: a whole kept WRAP, else the target words up to the run end (the
  // start word for a piece below it, else `end`), at most 2^len_bits, in page offsets one bit
  // wider. The offsets added to `from` are known before the run end, so each answer is one
  // difference or compare after it. length: AxLEN; to == from_next: one word (never for a kept
  // WRAP); fits: the run ends with this piece; next: the page offset past it (low 12 bits).
  struct Piece {
    ValueId length, to, from_next, fits, next;
  };
  static constexpr unsigned run_bits = page_bits + 1;
  // AxLEN of a kept WRAP of `words` target words, a power of two: words - 1 sets every bit below
  // the one set.
  ValueId wrap_length(ValueId words) {
    std::vector<ValueId> bits;
    for (unsigned k = 0; k < lb; ++k)
      bits.push_back(l.inv(l.eq(l.b.slice(words, k + 1, lb - k), 0)));
    ValueId out = bits.back();
    for (unsigned k = lb - 1; k-- > 0;)
      out = l.b.concat(out, bits[k]);
    return out;
  }
  // A value per AxLEN width of the target holding `address`: a piece must fit the target's AxLEN,
  // which may be shorter than the network's.
  template <class F> ValueId by_piece_len(ValueId address, F &&make) {
    std::vector<unsigned> lens{lb};
    for (const auto &[window, len] : p.piece_len_bits)
      lens.push_back(len);
    std::ranges::sort(lens);
    lens.erase(std::unique(lens.begin(), lens.end()), lens.end());
    if (lens.size() == 1)
      return make(lens.front());
    std::vector<AddressWindow> windows;
    std::vector<unsigned> classes;
    for (const auto &[window, len] : p.piece_len_bits) {
      windows.push_back(window);
      classes.push_back(len);
    }
    std::vector<ValueId> values;
    for (unsigned len = 0; len <= lb; ++len)
      values.push_back(l.num(4, len));
    const auto len_of = l.window_class(widen(address), windows, classes, values, l.num(4, lb));
    auto value = make(lens.back());
    for (std::size_t k = lens.size() - 1; k-- > 0;)
      value = l.pick(l.eq(len_of, lens[k]), make(lens[k]), value);
    return value;
  }
  ValueId piece_words(ValueId address) {
    return by_piece_len(address, [&](unsigned len) { return l.num(lb + 1, 1U << len); });
  }
  Piece page_piece(ValueId at, ValueId c, ValueId wrap, ValueId wrap_words, ValueId end,
                   ValueId start_word) {
    const auto from = l.page_offset(at), first_word = l.page_offset(start_word);
    const auto to = l.zext(l.pick(l.below(from, first_word), first_word, end), run_bits);
    const auto wide_from = l.zext(from, run_bits);
    auto past = [&](auto bytes) {
      return l.plus(wide_from, table(c, lw, [&](unsigned n) {
                      return l.num(run_bits, bytes(n) & ((std::uint64_t{1} << run_bits) - 1));
                    }));
    };
    const auto from_next = past([](unsigned n) { return std::uint64_t{1} << n; });
    const auto limit = by_piece_len(start_word, [&](unsigned len) {
      return past(
          [&](unsigned n) { return std::min(std::uint64_t{1} << (len + n), std::uint64_t{8191}); });
    });
    const auto most = by_piece_len(start_word, [&](unsigned len) {
      return past([&](unsigned n) { return std::uint64_t{1} << (len + n); });
    });
    const auto over = l.below(limit, to);
    const auto longest =
        by_piece_len(start_word, [&](unsigned len) { return l.num(lb, (1U << len) - 1); });
    const auto run_length =
        l.pick(over, longest, l.b.slice(shift_right(l.minus(to, from_next), c), 0, lb));
    return {l.pick(wrap, wrap_length(wrap_words), run_length),
            l.pick(wrap, l.num(run_bits, (1U << run_bits) - 1), to),
            l.pick(wrap, l.num(run_bits, 0), from_next), l.inv(over),
            l.b.slice(l.pick(over, most, to), 0, 12)};
  }
  ValueId serial_request(const Fields &f, const Plan &plan) {
    const auto piece =
        page_piece(plan.start_word, plan.c, plan.wrap, plan.wrap_words, plan.end, plan.start_word);
    const auto last =
        l.either(plan.wrap, l.both(piece.fits, l.both(l.inv(l.both(l.eq(f.at("burst"), 0),
                                                                   l.inv(l.eq(f.at("length"), 0)))),
                                                      l.same(plan.restart, plan.start_word))));
    return l.either(l.eq(plan.mode, 2), l.both(l.eq(plan.mode, 1), l.inv(last)));
  }
  // The plan registers loaded as a burst starts, in Plan order (the write
  // side holds no restart).
  template <class... R> void load_plan(ValueId go, const Plan &plan, R... reg) {
    const ValueId from[] = {plan.c,   plan.mode,       plan.wrap,   plan.wrap_words,
                            plan.end, plan.start_word, plan.restart};
    unsigned k = 0;
    (l.b.update(reg, l.pick(go, from[k++], reg)), ...);
  }
  // The byte-enable constant with bits [low, low + count) set. Up to 64 bits
  // it is one literal; the 64- and 128-byte enables of 512- and 1024-bit
  // sources join literals of at most 64 bits.
  ValueId run_mask(unsigned low, unsigned count) {
    if (bytes <= 64)
      return l.num(bytes, (count == 64 ? ~std::uint64_t{0} : ((std::uint64_t{1} << count) - 1))
                              << low);
    std::vector<std::pair<unsigned, bool>> runs; // (width, ones), most significant first
    if (low + count < bytes)
      runs.emplace_back(bytes - low - count, false);
    for (unsigned left = count; left;) {
      const unsigned piece = std::min(left, 64U);
      runs.emplace_back(piece, true);
      left -= piece;
    }
    if (low)
      runs.emplace_back(low, false);
    std::optional<ValueId> mask;
    for (const auto &[width, ones] : runs) {
      const auto part = l.num(
          width, ones ? (width == 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << width) - 1) : 0);
      mask = mask ? l.b.concat(*mask, part) : part;
    }
    return *mask;
  }
  // Byte lanes of [address, address + 2^c) within the source bus.
  ValueId lanes(ValueId address, ValueId c) {
    ValueId mask = run_mask(0, 0);
    for (unsigned k = 2; k <= lw; ++k)
      for (unsigned i = 0; i < (bytes >> k); ++i) {
        auto hit = l.eq(c, k);
        if (k < lw)
          hit = l.both(hit, l.eq(l.b.slice(address, k, lw - k), i));
        mask = l.pick(hit, run_mask(i << k, 1U << k), mask);
      }
    return mask;
  }
  // Byte lanes of the four-byte groups from `low` (a group boundary or below) to the byte `end`.
  ValueId run_lanes(ValueId low, ValueId end) {
    std::optional<ValueId> mask;
    for (unsigned g = 0; g < bytes / 4; ++g) {
      const auto at = l.num(lw, 4 * g);
      const auto in = l.both(l.inv(l.below(at, low)), l.inv(l.below(end, at)));
      const auto group = l.pick(in, l.num(4, 15), l.num(4, 0));
      mask = mask ? l.b.concat(group, *mask) : group;
    }
    return *mask;
  }
  // Byte lanes from `address` to the end of its 2^size beat container within the source bus: a
  // four-byte group is in when it lies in that container at or above the address.
  ValueId beat_rest(ValueId address, ValueId size) {
    const auto offset = l.b.slice(address, 0, lw),
               outside = l.b.unary(Op::BitNot, low_mask(size, lw), lw);
    std::optional<ValueId> mask;
    for (unsigned g = 0; g < bytes / 4; ++g) {
      const auto at = l.num(lw, 4 * g);
      const auto in =
          l.both(l.same(l.both(at, outside), l.both(offset, outside)), l.inv(l.below(at, offset)));
      const auto group = l.pick(in, l.num(4, 15), l.num(4, 0));
      mask = mask ? l.b.concat(group, *mask) : group;
    }
    return *mask;
  }
  ValueId expand(ValueId byte_mask) { return strobe_lanes(l.b, byte_mask, bytes); }
  ValueId count_of(ValueId c, unsigned width) {
    return table(c, lw, [&](unsigned n) { return l.num(width, std::uint64_t{1} << n); });
  }
  ValueId narrow_count(ValueId count) {
    return l.width(count) == down_count_bits ? count : l.b.slice(count, 0, down_count_bits);
  }
  // A 32-bit down face: the word of a source beat at `address`, and a target
  // word placed on every lane of the source bus.
  ValueId lane_of(ValueId value, ValueId address, unsigned lane_bits) {
    const auto index = l.b.slice(address, 2, lw - 2);
    ValueId picked = l.num(lane_bits, 0);
    for (unsigned i = 0; i < bytes / 4; ++i)
      picked = l.pick(l.eq(index, i), l.b.slice(value, i * lane_bits, lane_bits), picked);
    return picked;
  }
  ValueId replicate(ValueId word) {
    std::vector<ValueId> parts(bytes / 4, word);
    while (parts.size() > 1) {
      std::vector<ValueId> joined;
      for (std::size_t i = 0; i < parts.size(); i += 2)
        joined.push_back(l.b.concat(parts[i + 1], parts[i]));
      parts = std::move(joined);
    }
    return parts.front();
  }

  // Write-side handles handed from write_request to write_response.
  Fields up_w, wb;
  ValueId up_b_ready{}, local_b{}, cut_w{}, vserial{}, acc{}, acc_error{}, wcount{}, transfer{},
      burst_last{}, accept{}, write_class{}, wbegin{};
  Fields w, wo, write_metadata;
  std::vector<ValueId> ends;
  std::optional<serial_class::Gate> write_gate;
  std::optional<packet_context::Table> write_context;
  std::optional<Handed> write_handed, read_handed;

  // Read-side handles handed from read_issue to read_response.
  Fields ri, rr;
  ValueId up_r_ready{}, busy{}, queued{}, rmode{}, rc{}, rend{}, rrestart{}, rcount{};
  ValueId size_end{};
  ValueId answering{}, gathered{}, amode{}, ac{}, asize{}, gather{}, rfirst{}, racc{}, code{},
      bad{}, remaining{}, gather_left{}, reject_address{};
  ValueId rload{}, idle_issue{}, rq_fire{}, pass_r{}, reject_r{}, last_piece{}, read_class{},
      response_present{};
  // A response run's walk (walk_run).
  bool walks = false;
  ValueId walk{}, walking{}, part_done{}, part_end{}, run_size{};
  Plan rplan{};
  Fields d, read_metadata, response_metadata;
  std::optional<serial_class::Gate> read_gate;
  std::optional<packet_context::Table> read_context;
  // Response views (read_views): the arriving packet's descriptor while it is handed over (fresh),
  // else the registers.
  ValueId handed_now{}, mode_v{}, ac_v{}, asize_v{}, aoffset{}, astart{}, afixed{}, aoffset_v{},
      astart_v{}, afixed_v{}, word_step{}, beat_end{}, beat_end_held{}, gather_v{}, gathered_v{},
      gather_left_v{}, remaining_v{}, rfirst_v{};
  ValueId view(ValueId reg, ValueId in) { return l.pick(idle_issue, in, reg); }

  void write_request();
  void write_response();
  void read_issue();
  void read_views();
  void walk_run(ValueId size);
  void walk_step(ValueId part_taken);
  ValueId part_lanes(ValueId word_address, ValueId width);
  ValueId gather_exclusive(ValueId first, ValueId take) {
    if (!p.exclusive)
      return zero;
    const auto exclusive_acc = l.state("r.gather_exclusive_okay", 1);
    const auto value =
        l.pick(first, rr.at("exclusive_okay"), l.both(exclusive_acc, rr.at("exclusive_okay")));
    l.b.update(exclusive_acc, l.pick(take, value, exclusive_acc));
    return value;
  }
  void read_response();
  // A word that is a DECERR packet (on): whether it ends a beat (ends) and leaves part of one
  // (tail), the beats it ends (ended) and those after the first (beats), the offset in the beat the
  // tail reaches (place).
  struct DecodeWord {
    ValueId on, ends, tail, beats, ended, place;
  };
  DecodeWord decode_word(const std::function<ValueId()> &offset_of, ValueId c, ValueId size);
  // A value that a local miss descriptor's one word replaces: other, a value or what makes it.
  template <class Other>
  ValueId unless_decoded(const DecodeWord &word, ValueId value, const Other &other) {
    if (!p.decode_packets)
      return value;
    if constexpr (std::is_invocable_v<Other>)
      return l.pick(word.on, other(), value);
    else
      return l.pick(word.on, other, value);
  }
  // A packet's mark stays on the word: a part left of its last beat holds network_miss.
  ValueId arrived_result(const DecodeWord &word) {
    return p.decode_flag ? rr.at("result")
                         : unless_decoded(word, rr.at("result"), [&] { return l.num(2, 1); });
  }
  void gather_step(const DecodeWord &decoded, ValueId loaded, ValueId take, ValueId start_in,
                   ValueId length_in, ValueId own_pop);
  void read_reassembly();
  // Passed responses: a 32-bit down face places its word on the beat's lane.
  Fields passed_response(ValueId count) {
    Fields passed = rr;
    passed.erase("valid");
    if (narrow_face) {
      const auto lane_data =
          l.both(replicate(rr.at("data")), expand(lanes(rr.at("address"), l.num(3, 2))));
      const auto pass_failed = l.either(rr.at("error"), l.inv(l.eq(rr.at("result"), 0)));
      passed["data"] =
          p.zero_failed_beats ? l.pick(pass_failed, l.num(p.data_bits, 0), lane_data) : lane_data;
      passed["count"] = count;
    }
    return passed;
  }
  // The source tags' own state: one register per tag, read and updated at the tag of the word
  // arriving now (an unchanged next value where no word arrives).
  struct PerTag {
    std::vector<ValueId> regs;
    ValueId now{};
  };
  PerTag per_tag(const std::string &name, unsigned width) {
    PerTag out;
    out.now = l.num(width, 0);
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      out.regs.push_back(l.state("r.t" + std::to_string(k) + "." + name, width));
      out.now = l.pick(l.eq(rr.at("tag"), k), out.regs.back(), out.now);
    }
    return out;
  }
  void update(const PerTag &state, ValueId next) {
    for (unsigned k = 0; k < p.max_outstanding; ++k)
      l.b.update(state.regs[k], l.pick(l.eq(rr.at("tag"), k), next, state.regs[k]));
  }
};

void WidthCutLowering::write_request() {
  // ---------------- write: the up face's beat, one target word per transfer.
  // A beat leaves word by word from the up face (sub_address names the word due next) and is
  // taken with its last word, a beat passed whole in the cycle it leaves; a rejected burst's
  // beats are taken and dropped once every earlier B has come back. Nothing is held here, so
  // target words leave every cycle across source beats, pieces and bursts.
  up_w = io.inputs("up_wreq", p.data_bits, true);
  wb = io.inputs("down_wrsp", p.down_bits, false);
  const auto down_w_ready = io.in("down_wreq", "ready", 1);
  up_b_ready = io.in("up_wrsp", "ready", 1);
  Fields wi = up_w;
  wi.erase("valid");
  // mid: the up face's beat has sent some of its words.
  const auto mid = l.state("w.mid", 1);
  local_b = l.state("w.local_response", 1);
  auto wmode = l.state("w.mode", 2), wc = width_state("w.width");
  auto wwrap = l.state("w.wrap", 1), wwrap_words = l.state("w.wrap_words", lb + 1);
  auto wend = l.state("w.end", page_bits), wstart_word = word_state("w.start_word", ab);
  auto sa = l.state("w.sub_address", ab), open = l.state("w.open", 1);
  auto left = l.state("w.left", lb + 1), plen = l.state("w.piece_length", lb);
  auto pstart = l.state("w.piece_start", ab);
  acc = l.state("w.result", 2);
  acc_error = l.state("w.error", 1);
  // Pieces sent whose B has not come back, oldest first; ends[k] marks a
  // piece that ends its burst, so its B is the burst's final one.
  wcount = piece_count::counter(l, "w.count");
  for (unsigned k = 0; k < piece_count::kInFlight; ++k)
    ends.push_back(l.state("w.ends" + std::to_string(k), 1));
  if (p.write_hints)
    write_handed = handed_plan("write");
  write_class = one;
  ValueId write_admit = one;
  if (multi) {
    write_gate = serial_class::make(l, "w", contexts, "packets");
    write_class = l.bind("w.serial_class", serial_request(up_w, plan_of(up_w, write_handed)));
    write_admit = serial_class::admit(l, *write_gate, write_class);
    write_metadata = {{"address", floor_to(up_w.at("start_address"), up_w.at("size"))},
                      {"count", up_w.at("count")}};
    write_context =
        packet_context::make(l, "w.context", p.max_outstanding, p.packet_depth, write_metadata);
    write_admit = l.both(write_admit, packet_context::room(l, *write_context, up_w.at("tag")));
  }
  // The beat's plan: the up face's, with the handed fields.
  const auto wplan = write_handed ? plan_of(up_w, write_handed) : plan_of(wi);
  const auto entering_start = write_handed ? write_handed->start : wi.at("start_address");
  const auto entering_address = write_handed ? *write_handed->address : wi.at("address");
  cut_w = l.eq(wmode, 1);
  // The beat on the up face, its burst's first one admitted. Its view takes the burst's plan
  // from the up face while the burst's first beat has sent nothing, else from the registers.
  const auto fresh = l.inv(mid);
  const auto admit_first = multi ? l.pick(l.both(fresh, up_w.at("first")), write_admit, one) : one;
  const auto entering = io.active(l.both(l.inv(local_b), l.both(up_w.at("valid"), admit_first)));
  const auto start_plan = l.both(fresh, wi.at("first"));
  const auto start_now = l.both(start_plan, entering);
  auto plan_view = [&](ValueId reg, ValueId in) { return l.pick(start_plan, in, reg); };
  const auto vmode = plan_view(wmode, wplan.mode), vc = plan_view(wc, wplan.c);
  const auto vwrap = plan_view(wwrap, wplan.wrap);
  const auto vwrap_words = plan_view(wwrap_words, wplan.wrap_words);
  const auto vend = plan_view(wend, wplan.end),
             vstart_word = plan_view(wstart_word, wplan.start_word);
  const auto cut_v = l.eq(vmode, 1);
  const auto reject_v = plan_view(l.eq(wmode, 2), wplan.reject);
  // Each source beat starts at its own container, except that an INCR first
  // beat and every FIXED beat start at the start word.
  const auto load_c = l.pick(wi.at("first"), wplan.c, wc);
  const auto entry_sa = l.pick(l.either(wi.at("first"), l.eq(wi.at("burst"), 0)),
                               floor_to(entering_start, load_c), entering_address);
  const auto vsa = l.pick(mid, sa, entry_sa);
  const auto vopen = l.pick(start_now, zero, open);
  // A burst entering now takes its serial class at this edge.
  vserial = multi ? l.pick(start_now, write_class, write_gate->serial) : zero;
  // A full count holds only whole pieces (a piece counts from its last
  // word), so it never stops a piece halfway.
  const auto write_piece_room =
      multi ? l.pick(vserial, piece_count::room(l, wcount), one) : piece_count::room(l, wcount);
  auto w_valid = io.active(l.both(entering, l.both(l.inv(reject_v), write_piece_room)));
  transfer = l.bind("w.transfer", l.both(w_valid, down_w_ready));
  // The word at vsa and the beat's size as the splitter hands it over, else its own.
  const auto cut_size = write_handed ? write_handed->size : wi.at("size");
  const auto beat_mask = low_mask(cut_size, ab);
  const auto head = l.bind("w.head", l.inv(l.pick(start_plan, zero, open)));
  const auto head_piece = page_piece(vsa, vc, vwrap, vwrap_words, vend, vstart_word);
  // A transfer carries a run of target words (width_adapter.hpp request runs): the rest of the
  // source beat from vsa when the piece holds it all (whole), else one word; one word on a 32-bit
  // down face and in a kept WRAP. The transport ladder splits it into target words on the way.
  // after: the words of the beat after vsa's; the piece holds them when its words left after the
  // head (AxLEN) or in all (left) exceed them.
  const auto after =
      l.fit(shift_right(l.both(l.b.unary(Op::BitNot, vsa, ab), beat_mask), vc), lb + 1);
  // A head transfer of an INCR takes the rest of the beat when the run ends past the beat's last
  // byte (as page offsets, beat_last) and those words fit a piece; a later one when the words
  // left exceed the beat's after it.
  const auto beat_last = l.page_offset(l.either(vsa, beat_mask));
  // Only an INCR's head qualifies, and a later transfer reads the burst's held plan: no whole run
  // waits for the plan of a burst entering now to tell whether it is a kept WRAP.
  ValueId whole = zero;
  if (!narrow_face)
    whole =
        l.pick(head,
               l.both(l.eq(wi.at("burst"), 1),
                      l.both(l.below(beat_last, vend), l.below(after, piece_words(vstart_word)))),
               l.both(l.inv(wwrap), l.below(after, left)));
  whole = l.bind("w.run_whole", whole);
  const auto next_sa = l.pick(whole, l.page_plus(floor_to(vsa, cut_size), power(cut_size, ab)),
                              l.page_plus(vsa, power(vc, ab)));
  // The word at vsa ends its source beat when every address bit from the word up to the beat is
  // set (no sum); a whole run always does.
  auto sub_end = l.bind(
      "w.sub_end",
      l.either(whole, l.same(l.both(l.either(vsa, low_mask(vc, ab)), beat_mask), beat_mask)));
  // The piece's last transfer: a head piece of one word, or one word left, or a whole run that
  // holds every word left.
  const auto left_after = l.minus(left, l.num(lb + 1, 1));
  auto piece_last = l.same(l.pick(head, head_piece.to, l.zext(left, run_bits)),
                           l.pick(head, head_piece.from_next, l.num(run_bits, 1)));
  if (!narrow_face)
    piece_last = l.pick(
        whole,
        l.pick(head, l.same(l.minus(vend, l.num(13, 1)), beat_last), l.same(left_after, after)),
        piece_last);
  piece_last = l.bind("w.piece_last", piece_last);
  burst_last = l.both(wi.at("last"), sub_end);
  // A rejected burst completes locally once every earlier B has come back.
  const auto settled = l.eq(wcount, 0);
  // The up face's beat is taken with its last word, or dropped when its burst is rejected.
  const auto wi_ready = io.active(
      l.both(l.inv(local_b),
             l.both(admit_first, l.pick(reject_v, settled,
                                        l.both(sub_end, l.both(write_piece_room, down_w_ready))))));
  accept = l.bind("w.accept", l.both(up_w.at("valid"), wi_ready));
  // Behind the packet splitter (packet_depth > 1) each packet's burst-final marker comes on
  // the hint face with its beats; the down face exports it on the word that ends the burst,
  // so the source merger holds its grant over the whole burst.
  ValueId w_final = one;
  if (p.packet_depth > 1)
    w_final = l.b.input("nl_hint_write_final", 1, "hint", "write_final");
  l.bind("w.burst_final_packet", l.both(w_final, burst_last));
  // A burst begins as its first beat sends its first word (or is dropped).
  wbegin = l.both(start_plan, l.either(transfer, accept));
  load_plan(wbegin, wplan, wc, wmode, wwrap, wwrap_words, wend, wstart_word);
  // The burst's own fields for a cut or local B.
  for (const auto *name : {"tag", "id", "source", "start_address", "size", "count"})
    if (wi.contains(name))
      w[name] = l.latch(std::string("w.burst_") + name, wi.at(name), wbegin);
  l.b.update(mid, l.pick(transfer, l.inv(sub_end), mid));
  l.b.update(sa, l.pick(transfer, next_sa, sa));
  const auto cut_transfer = l.both(transfer, cut_v);
  l.b.update(open, l.pick(cut_transfer, l.inv(piece_last), vopen));
  // Words left after this transfer: AxLEN after a head word (less the rest of a whole run).
  const auto after_head = l.pick(head, l.zext(head_piece.length, lb + 1), left_after);
  l.b.update(left,
             l.pick(cut_transfer, l.pick(whole, l.minus(after_head, after), after_head), left));
  const auto head_start = l.pick(l.same(vsa, vstart_word), wi.at("start_address"), vsa);
  l.b.update(plen, l.pick(l.both(cut_transfer, head), head_piece.length, plen));
  l.b.update(pstart, l.pick(l.both(cut_transfer, head), head_start, pstart));
  const auto local_push = l.both(l.both(accept, reject_v), wi.at("last"));
  l.b.update(local_b, l.pick(local_push, one, l.pick(l.both(local_b, up_b_ready), zero, local_b)));
  wo = wi;
  const auto beat_address = l.pick(cut_v, vsa, wi.at("address"));
  wo["address"] = beat_address;
  wo["size"] = l.pick(cut_v, vc, wi.at("size"));
  wo["length"] = l.pick(cut_v, l.pick(head, head_piece.length, plen), wi.at("length"));
  wo["burst"] = l.pick(cut_v, l.pick(vwrap, l.num(2, 2), l.num(2, 1)), wi.at("burst"));
  wo["start_address"] = l.pick(cut_v, l.pick(head, head_start, pstart), wi.at("start_address"));
  wo["first"] = l.pick(cut_v, head, wi.at("first"));
  wo["last"] = l.pick(cut_v, piece_last, wi.at("last"));
  const auto be = l.pick(
      cut_v,
      l.both(wi.at("be"), narrow_face ? lanes(vsa, vc)
                                      : l.pick(whole, beat_rest(vsa, cut_size), lanes(vsa, vc))),
      wi.at("be"));
  // Sources already clear every unstrobed byte (strobe_lanes.hpp), and a
  // target word keeps the source strobes of its lanes, so it stays clear.
  if (narrow_face) {
    wo["data"] = lane_of(wi.at("data"), beat_address, 32);
    wo["be"] = lane_of(be, beat_address, 4);
    wo["count"] = l.pick(cut_v, l.num(down_count_bits, 4), narrow_count(wi.at("count")));
  } else {
    wo["be"] = be;
    // A whole run's bytes: the beat's 2^size less vsa's offset in it.
    const auto rest =
        l.fit(l.minus(power(cut_size, lw + 1), l.fit(l.both(vsa, low_mask(cut_size, ab)), lw + 1)),
              count_bits);
    wo["count"] = l.pick(cut_v, l.pick(whole, rest, count_of(vc, count_bits)), wi.at("count"));
  }
  // A split WRAP is one kept WRAP piece of its words (its head's length, then the latched one);
  // an uncut beat keeps the up face's mark (message.hpp point 4).
  wo["wrap_end"] =
      l.pick(cut_v,
             l.both(vwrap, wrap_end_of(l, one, l.num(2, 2),
                                       l.pick(head, wrap_length(vwrap_words), plen), vc, vsa)),
             wi.at("wrap_end"));
  // A whole run ends where its source beat does.
  wo["ends"] = beat_ends(l, wo.at("address"), l.pick(l.both(cut_v, whole), cut_size, wo.at("size")),
                         p.down_bits / 8);
  io.out("up_wreq", "ready", wi_ready);
  io.out("down_wreq", "valid", w_valid);
  io.fields_out("down_wreq", wo, p.down_bits, true);
}

void WidthCutLowering::write_response() {
  // Pieces of a serial packet return in order; passthrough packets may
  // return in any tag order. An intermediate serial B is absorbed regardless
  // of requests; the final B carries the merge and waits for the up face.
  // A B while no packet is in flight (after a reset of this side alone) is taken and dropped.
  const auto stray = l.bind("w.stray", l.eq(multi ? write_gate->count : wcount, 0));
  const auto final_b = l.both(
      l.inv(stray), multi ? l.either(l.inv(write_gate->serial), ends.front()) : ends.front());
  const auto b_valid = io.active(wb.at("valid"));
  const auto b_ready = io.active(l.both(l.inv(local_b), l.either(l.inv(final_b), up_b_ready)));
  const auto b_fire = l.both(l.both(b_valid, b_ready), l.inv(stray));
  const auto absorb = l.bind("w.absorb", l.both(b_fire, l.inv(final_b)));
  const auto sent = l.both(transfer, wo.at("last"));
  const auto serial_sent = multi ? l.both(sent, vserial) : sent;
  const auto serial_b = multi ? l.both(b_fire, write_gate->serial) : b_fire;
  piece_count::step(l, wcount, serial_sent, serial_b);
  // A sent piece joins the queue behind the pieces still in flight.
  const auto at = l.minus(wcount, l.zext(serial_b, piece_count::kBits));
  for (unsigned k = 0; k < piece_count::kInFlight; ++k) {
    const auto shifted =
        l.pick(serial_b, k + 1 < piece_count::kInFlight ? ends[k + 1] : zero, ends[k]);
    l.b.update(ends[k], l.pick(l.both(serial_sent, l.eq(at, k)), burst_last, shifted));
  }
  const auto closed = l.both(b_fire, final_b);
  l.b.update(acc, l.pick(closed, l.num(2, 0), l.pick(absorb, merge(acc, wb.at("result")), acc)));
  l.b.update(acc_error,
             l.pick(closed, zero, l.pick(absorb, l.either(acc_error, wb.at("error")), acc_error)));
  Fields wr;
  for (const auto *name : {"tag", "id", "source"})
    if (wb.contains(name))
      wr[name] = l.pick(local_b, w.at(name), wb.at(name));
  // A cut or local B answers in the source burst's terms (the held beat's,
  // which is still the burst's last one unless a later burst has entered).
  const auto own_b = l.either(local_b, cut_w);
  wr["address"] = l.pick(own_b, floor_to(w.at("start_address"), w.at("size")), wb.at("address"));
  wr["result"] = l.pick(local_b, l.num(2, 2), merge(acc, wb.at("result")));
  wr["error"] = l.pick(local_b, zero, l.either(acc_error, wb.at("error")));
  if (p.exclusive)
    wr["exclusive_okay"] = l.both(l.inv(local_b), wb.at("exclusive_okay"));
  // The B of a burst's final piece carries its response user field.
  if (p.response_user_bits)
    wr["user"] = l.pick(local_b, l.num(p.response_user_bits, 0), wb.at("user"));
  wr["first"] = l.pick(local_b, one, wb.at("first"));
  wr["last"] = l.pick(local_b, one, wb.at("last"));
  wr["count"] = narrow_face ? w.at("count") : l.pick(own_b, w.at("count"), wb.at("count"));
  wr["data"] =
      narrow_face ? l.num(p.data_bits, 0) : l.pick(local_b, l.num(p.data_bits, 0), wb.at("data"));
  if (multi) {
    const auto response_tag = l.pick(local_b, w.at("tag"), wb.at("tag"));
    const auto metadata = packet_context::front(l, *write_context, response_tag);
    wr["address"] = metadata.at("address");
    wr["count"] = metadata.at("count");
    wr["result"] = l.pick(local_b, l.num(2, 2),
                          l.pick(write_gate->serial, merge(acc, wb.at("result")), wb.at("result")));
    wr["error"] =
        l.pick(local_b, zero,
               l.pick(write_gate->serial, l.either(acc_error, wb.at("error")), wb.at("error")));
    const auto first = wbegin;
    const auto completed = l.either(l.both(io.active(local_b), up_b_ready), closed);
    packet_context::step(l, *write_context, first, up_w.at("tag"), write_metadata, completed,
                         response_tag);
    serial_class::step(l, *write_gate, first, write_class, completed);
  }
  io.out("up_wrsp", "valid", l.either(io.active(local_b), l.both(b_valid, final_b)));
  io.out("down_wrsp", "ready", b_ready);
  io.fields_out("up_wrsp", wr, p.data_bits, false);
}

void WidthCutLowering::read_issue() {
  // ---------------- read: the issue side sends a burst's piece requests,
  // each once the previous one is accepted, and takes the next burst once
  // its requests are out. With multiple source slots, the response tag
  // selects a reserved packet descriptor; one shared gather owns that packet
  // through final delivery. The single-slot path hands bursts on in order
  // with one waiting descriptor. A gathered beat leaves with the word
  // that completes it, which waits for the up face's ready.
  ri = io.inputs("up_rreq", p.data_bits, true);
  rr = io.inputs("down_rrsp", p.down_bits, false);
  const auto down_r_ready = io.in("down_rreq", "ready", 1);
  up_r_ready = io.in("up_rrsp", "ready", 1);
  // Issue side. queued: its burst still waits for the response side, which
  // copies the descriptor from here.
  busy = l.state("r.busy", 1);
  const auto pending = l.state("r.pending", 1);
  queued = l.state("r.queued", 1);
  rmode = l.state("r.mode", 2);
  rc = width_state("r.width");
  auto rwrap = l.state("r.wrap", 1), rwrap_words = l.state("r.wrap_words", lb + 1);
  rend = l.state("r.end", page_bits);
  // Pieces, the restart and the start word share the burst's 4 KiB page: the restart and the start
  // word store only their page offsets and read back the piece's page.
  const auto piece = l.state("r.piece", ab);
  // The start word's bits below the narrowest target word are zero (word_state).
  auto in_page = [&](const std::string &name, bool word) {
    const auto bits = std::min(ab, 12U);
    const auto offset = word ? word_state(name, bits) : l.state(name, bits);
    return ab > 12 ? l.b.widened(offset, l.b.slice(piece, 12, ab - 12)) : offset;
  };
  const auto rstart_word = in_page("r.start_word", true);
  rrestart = in_page("r.restart", false);
  // A cut FIXED burst walks its container once per beat: runs still to go.
  auto runs = l.state("r.runs", lb);
  rcount = piece_count::counter(l, "r.count");
  // Response side: the burst being answered (mode, target width, beat size
  // and the fields of its own beats).
  answering = l.state("r.answering", 1);
  rfirst = l.state("r.first", 1);
  // remaining counts the beats still to answer, gather_left the beats still
  // to gather (a local stream answers without gathering).
  remaining = l.state("r.remaining", lb);
  reject_address = l.state("r.reject_address", ab);
  if (!p.reassembly) {
    gathered = l.state("r.gathered", 1);
    amode = l.state("r.answer_mode", 2);
    ac = width_state("r.answer_width");
    asize = l.state("r.answer_size", 3);
    gather = l.state("r.gather", 1);
    racc = l.state("r.gather_data", p.data_bits);
    code = l.state("r.gather_result", 2);
    bad = l.state("r.gather_error", 1);
    gather_left = l.state("r.gather_left", lb);
  }
  std::optional<Plan> incoming_plan;
  if (p.read_hints)
    read_handed = handed_plan("read");
  read_class = one;
  ValueId read_admit = one;
  response_present = one;
  if (multi) {
    incoming_plan = plan_of(ri, read_handed);
    read_gate = serial_class::make(l, "r", contexts, "packets");
    read_class = l.bind("r.serial_class", serial_request(ri, *incoming_plan));
    read_admit = serial_class::admit(l, *read_gate, read_class);
    read_metadata = {
        {"mode", incoming_plan->mode}, {"width", incoming_plan->c}, {"size", ri.at("size")}};
    if (!p.reassembly) {
      read_metadata["length"] = ri.at("length");
      read_metadata["start"] =
          start_offset(ri.at("start_address"), incoming_plan->c, ri.at("size"));
      read_metadata["fixed"] = l.eq(ri.at("burst"), 0);
    }
    for (const auto *name : {"tag", "id", "source"})
      if (ri.contains(name))
        read_metadata[name] = ri.at(name);
    read_context =
        packet_context::make(l, "r.context", p.max_outstanding, p.packet_depth, read_metadata);
    read_admit = l.both(read_admit, packet_context::room(l, *read_context, ri.at("tag")));
    response_metadata = packet_context::front(l, *read_context, rr.at("tag"));
    response_present =
        l.bind("r.context_present", packet_context::valid(l, *read_context, rr.at("tag")));
  }
  auto ri_ready =
      multi ? io.active(l.both(l.inv(busy), read_admit)) : io.active(l.inv(l.either(busy, queued)));
  rload = l.bind("r.load", l.both(ri.at("valid"), ri_ready));
  // A read request carries no payload.
  d = l.latch_fields("r.descriptor", ri, rload, {"data", "be"});
  rplan = multi ? *incoming_plan : plan_of(ri, read_handed);
  // A burst taken to be answered here (mode 2) never leaves as a request.
  l.bind("r.local_take", l.both(rload, l.eq(rplan.mode, 2)));
  load_plan(rload, rplan, rc, rmode, rwrap, rwrap_words, rend, rstart_word, rrestart);
  // An idle issue side passes a burst's first piece request straight idle_issue in the cycle it
  // takes the burst (the descriptor registers still load), so the conversion adds no cycle
  // to an idle request; a burst that is not sent at once, and its later pieces, issue from the
  // registers. The view of the piece being issued: the registers, else the burst entering now.
  idle_issue = l.inv(busy);
  ValueId r_final = one, r_taken_packet = zero;
  if (p.packet_depth > 1) {
    const auto hint = l.b.input("nl_hint_read_final", 1, "hint", "read_final");
    r_final = l.pick(idle_issue, hint, l.latch("r.final", hint, rload));
    if (p.read_granule_taken) {
      const auto taken = l.b.input("nl_hint_read_taken_packet", 1, "hint", "read_taken_packet");
      r_taken_packet = l.pick(idle_issue, taken, l.latch("r.taken_packet", taken, rload));
    }
  }
  const Fields rvd = view_fields(l, d, ri, idle_issue);
  const auto rvmode = view(rmode, rplan.mode), rvc = view(rc, rplan.c);
  const auto rvwrap = view(rwrap, rplan.wrap), rvwrap_words = view(rwrap_words, rplan.wrap_words);
  const auto rvend = view(rend, rplan.end), rvstart_word = view(rstart_word, rplan.start_word);
  const auto rvrestart = view(rrestart, rplan.restart);
  const auto rvpiece =
      view(piece, floor_to(read_handed ? read_handed->start : ri.at("start_address"), rplan.c));
  const auto rvruns =
      view(runs, l.pick(l.eq(ri.at("burst"), 0),
                        read_handed ? read_handed->length : ri.at("length"), l.num(lb, 0)));
  pass_r = l.eq(rvmode, 0);
  const auto cut_r = l.eq(rvmode, 1);
  reject_r = view(l.eq(rmode, 2), rplan.reject);
  const auto read_piece_room =
      multi ? l.pick(view(read_gate->serial, read_class), piece_count::room(l, rcount), one)
            : piece_count::room(l, rcount);
  // An idle issue side offers in the load cycle: the terms of the load, off its registers' fanout.
  const auto idle_take = l.both(ri.at("valid"), multi ? read_admit : l.inv(queued));
  auto rq_valid = io.active(
      l.both(l.pick(idle_issue, idle_take, pending), l.both(l.inv(reject_r), read_piece_room)));
  rq_fire = l.bind("r.request", l.both(rq_valid, down_r_ready));
  const auto rpiece = page_piece(rvpiece, rvc, rvwrap, rvwrap_words, rvend, rvstart_word);
  Fields ro = rvd;
  ro["address"] = l.pick(cut_r, rvpiece, rvd.at("address"));
  ro["start_address"] =
      l.pick(cut_r, l.pick(l.same(rvpiece, rvstart_word), rvd.at("start_address"), rvpiece),
             rvd.at("start_address"));
  ro["size"] = l.pick(cut_r, rvc, rvd.at("size"));
  ro["length"] = l.pick(cut_r, rpiece.length, rvd.at("length"));
  ro["burst"] = l.pick(cut_r, l.pick(rvwrap, l.num(2, 2), l.num(2, 1)), rvd.at("burst"));
  // A read request has no payload, so at most one word carries it on every link (message.hpp point
  // 4); a beat narrower than a word keeps its own bytes, which lie inside one.
  const auto within_word = l.below(ro.at("size"), l.num(3, 2));
  ro["count"] =
      narrow_face ? l.pick(within_word, narrow_count(rvd.at("count")), l.num(down_count_bits, 4))
                  : l.pick(within_word, count_of(ro.at("size"), count_bits), l.num(count_bits, 4));
  ro["wrap_end"] = zero; // a read (message.hpp point 4)
  ro["data"] = l.num(p.down_bits, 0);
  ro["be"] = l.num(p.down_bits / 8, 0);
  io.out("up_rreq", "ready", ri_ready);
  io.out("down_rreq", "valid", rq_valid);
  io.fields_out("down_rreq", ro, p.down_bits, true);
  // The piece walk: a piece ends its run (the burst end, the WRAP container
  // top, or the start word for the WRAP run below it, or a FIXED beat's
  // container end); the burst's last piece ends the last run.
  // Pieces and the start word share the burst's page.
  const auto in_second = l.below(l.b.slice(rvpiece, 0, 12), l.b.slice(rvstart_word, 0, 12));
  // The next piece in the page: past the run end only at the page top, where the restart (or the
  // burst end) takes over.
  const auto advanced = l.b.concat(l.b.slice(rvpiece, 12, ab - 12), rpiece.next);
  const auto run_done = rpiece.fits;
  last_piece = l.either(rvwrap, l.both(l.both(run_done, l.eq(rvruns, 0)),
                                       l.either(in_second, l.same(rvrestart, rvstart_word))));
  l.bind("r.burst_final_packet", l.both(r_final, l.either(l.inv(cut_r), last_piece)));
  if (p.read_granule_taken)
    l.bind("r.burst_taken_packet", l.both(r_taken_packet, l.either(l.inv(cut_r), last_piece)));
  auto next_piece = l.bind("r.next_piece", l.both(rq_fire, l.both(cut_r, l.inv(last_piece))));
  l.b.update(piece,
             l.pick(next_piece, l.pick(l.both(run_done, l.inv(in_second)), rvrestart, advanced),
                    l.pick(rload, rvpiece, piece)));
  l.b.update(runs, l.pick(l.both(next_piece, l.both(run_done, l.inv(l.eq(rvruns, 0)))),
                          l.minus(rvruns, l.num(lb, 1)), l.pick(rload, rvruns, runs)));
  // A burst taken now owes its first piece unless that piece left at once.
  l.b.update(pending, l.pick(l.either(next_piece, l.both(rload, l.inv(rq_fire))), one,
                             l.pick(rq_fire, zero, pending)));
}

// A local miss descriptor's one word (decode_packet.hpp) covers its target words at once: from the
// byte offset in its source beat, offset, it ends beats, leaving a part of one to the next piece.
WidthCutLowering::DecodeWord
WidthCutLowering::decode_word(const std::function<ValueId()> &offset_of, ValueId c, ValueId size) {
  DecodeWord word{zero, zero, zero, zero, zero, zero};
  if (!p.decode_packets)
    return word;
  const auto length = decode_packet::length_of(l, rr.at("data"), lb);
  word.on = decode_packet::marked(l, p.decode_flag, rr.at("result"), rr.at("error"));
  const unsigned bits = lb + lw + 2;
  const auto words = l.plus(l.zext(length, bits), l.num(bits, 1));
  const auto spans = shift_left(words, c);
  // Whole target words: no byte below the four-byte groups is read, the transport need not carry
  // it.
  const auto group = l.b.unary(Op::BitNot, l.num(lw, 3), lw);
  const auto offset = l.both(offset_of(), group);
  const auto total = l.plus(l.zext(l.both(offset, low_mask(size, lw)), bits), spans);
  const auto ended = shift_right(total, size);
  word.ends = l.inv(l.eq(ended, 0));
  word.ended = l.b.slice(ended, 0, lb);
  word.tail = l.inv(l.eq(l.both(total, low_mask(size, bits)), 0));
  word.beats = l.b.slice(l.minus(ended, l.num(bits, 1)), 0, lb);
  word.place = l.b.slice(l.both(total, low_mask(size, bits)), 0, lw);
  return word;
}

// A response run holds source beats (message.hpp point 18): it stays in front while its parts
// leave, each up to the end of its source beat. Below, a part is a word that ends at part_end.
void WidthCutLowering::walk_run(ValueId size) {
  walks = p.response_runs && !narrow_face;
  part_done = one;
  if (!walks)
    return;
  walk = l.state("r.walk", lw), walking = l.state("r.walking", 1);
  const auto address = rr.at("address");
  const auto at = l.pick(walking, walk, l.b.slice(address, 0, lw));
  // A run holds whole target words: it is walked by its four-byte groups (a narrower beat is a
  // fragment of its own and done at once), so no byte below them is read.
  const auto group = l.num(lw, 3);
  const auto reach = l.either(l.fit(rr.at("count"), lw), group);
  const auto beat_last = l.either(l.either(at, group), low_mask(size, lw));
  part_done = l.bind("r.part_done", l.inv(l.below(beat_last, reach)));
  part_end = l.pick(part_done, reach, beat_last);
  rr["address"] = l.b.concat(l.b.slice(address, lw, l.width(address) - lw), at);
  rr["last"] = l.both(rr.at("last"), part_done);
}
void WidthCutLowering::walk_step(ValueId part_taken) {
  if (!walks)
    return;
  l.b.update(walking, l.pick(part_taken, l.inv(part_done), walking));
  l.b.update(walk, l.pick(part_taken, l.plus(part_end, l.num(lw, 1)), walk));
}
ValueId WidthCutLowering::part_lanes(ValueId word_address, ValueId width) {
  // The groups are whole four-byte words: no byte below them is read.
  return walks ? run_lanes(l.both(l.b.slice(word_address, 0, lw),
                                  l.b.unary(Op::BitNot, l.num(lw, 3), lw)),
                           part_end)
               : lanes(word_address, width);
}

void WidthCutLowering::read_views() {
  // With several slots a packet is handed over as its first response word arrives, and that word
  // is served in the same cycle: the views read the arriving tag's descriptor (handed_now) where
  // the registers hold it from the next edge.
  handed_now =
      multi ? l.bind("r.fresh", l.both(l.inv(answering), l.both(rr.at("valid"), response_present)))
            : zero;
  const auto viewed = [&](ValueId state, const char *field) {
    return multi ? l.pick(handed_now, response_metadata.at(field), state) : state;
  };
  mode_v = viewed(amode, "mode"), ac_v = viewed(ac, "width"), asize_v = viewed(asize, "size");
  // Whether a word is in front does not matter to its walk: its readiness asks only about the word.
  run_size = multi ? l.pick(answering, asize, response_metadata.at("size")) : asize;
  walk_run(run_size);
  // The byte offset in its source beat of the word due next: a cut burst's words arrive in order,
  // so the word that completes a beat is known before it arrives. It leaves with the beat and
  // waits for the up face's ready; the other words are gathered at once.
  aoffset = l.state("r.answer_offset", lw), astart = l.state("r.answer_start", lw);
  afixed = l.state("r.answer_fixed", 1);
  aoffset_v = viewed(aoffset, "start"), astart_v = viewed(astart, "start");
  afixed_v = viewed(afixed, "fixed");
  const auto end_of = [&](ValueId offset, ValueId c, ValueId size) {
    const auto next = l.plus(l.zext(offset, lw + 1), power(c, lw + 1));
    return std::pair{next, l.eq(l.both(next, low_mask(size, lw + 1)), 0)};
  };
  // The down face's readiness reads the registers only (this side's own state); a part's end says
  // where a run's beat ends.
  gather_left_v = viewed(gather_left, "length");
  const auto walked_size_end =
      walks ? l.same(l.both(part_end, low_mask(run_size, lw)), low_mask(run_size, lw)) : zero;
  beat_end_held = walks ? walked_size_end : end_of(aoffset, ac, asize).second;
  const auto stepped = end_of(aoffset_v, ac_v, asize_v);
  size_end = walks ? walked_size_end : stepped.second;
  word_step = stepped.first;
  beat_end = l.bind("r.beat_end", walks ? beat_end_held : stepped.second);
  const auto unless_fresh = [&](ValueId v) { return multi ? l.both(v, l.inv(handed_now)) : v; };
  gather_v = unless_fresh(gather), gathered_v = unless_fresh(gathered);
  remaining_v = viewed(remaining, "length");
  rfirst_v = multi ? l.either(rfirst, handed_now) : rfirst;
}

// The beat, word and packet counts of the gather after a word (a local miss descriptor's word: all
// of its).
void WidthCutLowering::gather_step(const DecodeWord &decoded, ValueId loaded, ValueId take,
                                   ValueId start_in, ValueId length_in, ValueId own_pop) {
  l.b.update(aoffset,
             l.pick(loaded, start_in,
                    l.pick(take,
                           unless_decoded(decoded,
                                          l.pick(beat_end, l.pick(afixed_v, astart_v, l.num(lw, 0)),
                                                 l.b.slice(word_step, 0, lw)),
                                          [&] {
                                            return l.pick(decoded.tail, decoded.place,
                                                          l.pick(afixed_v, astart_v, l.num(lw, 0)));
                                          }),
                           aoffset_v)));
  const auto all_gathered = l.eq(gather_left_v, 0);
  l.b.update(gathered,
             l.pick(loaded, zero,
                    l.pick(l.both(take, unless_decoded(decoded, l.both(beat_end, all_gathered),
                                                       [&] {
                                                         return l.both(
                                                             decoded.ends,
                                                             l.same(gather_left_v, decoded.beats));
                                                       })),
                           one, gathered_v)));
  l.b.update(
      gather_left,
      l.pick(loaded, length_in,
             l.pick(l.both(take, unless_decoded(decoded, size_end, one)),
                    l.minus(gather_left_v, unless_decoded(decoded, l.num(lb, 1), decoded.ended)),
                    gather_left_v)));
  l.b.update(remaining, l.pick(loaded, length_in,
                               l.pick(own_pop,
                                      l.minus(remaining_v,
                                              unless_decoded(decoded, l.num(lb, 1), decoded.ended)),
                                      remaining_v)));
}

void WidthCutLowering::read_response() {
  // Response side. Cut responses: gather the target words of one source
  // beat; words of the next burst wait until this one's are all gathered.
  read_views();
  const auto viewed = [&](ValueId state, const char *field) {
    return multi ? l.pick(handed_now, response_metadata.at(field), state) : state;
  };
  const auto decoded = decode_word(
      [&] { return walks ? l.b.slice(rr.at("address"), 0, lw) : aoffset_v; }, ac_v, asize_v);
  const auto a_pass = l.eq(amode, 0), a_cut = l.eq(amode, 1), a_reject = l.eq(amode, 2);
  const auto pass_v = l.eq(mode_v, 0), cut_v = l.eq(mode_v, 1);
  const auto word_address = floor_to(rr.at("address"), ac_v);
  const auto unless_fresh = [&](ValueId v) { return multi ? l.both(v, l.inv(handed_now)) : v; };
  const auto low_ready =
      io.active(l.both(l.both(l.both(answering, a_cut), l.inv(gathered)),
                       l.either(l.inv(unless_decoded(decoded, beat_end_held, one)), up_r_ready)));
  // A packet handed over now is served unless a waiting packet answers locally; a served word
  // leaves at once, so it waits for the up face's ready.
  const auto idle =
      multi ? l.both(l.inv(answering), packet_context::no_front(l, *read_context, "mode", 2))
            : zero;
  const auto idle_ready = multi ? io.active(l.both(idle, up_r_ready)) : zero;
  const auto serving = multi ? l.either(answering, l.both(handed_now, idle)) : answering;
  const auto fresh_take = multi ? l.both(handed_now, l.both(idle_ready, cut_v)) : zero;
  auto take =
      l.bind("r.take", l.both(rr.at("valid"), multi ? l.either(low_ready, fresh_take) : low_ready));
  const auto placed = narrow_face ? replicate(rr.at("data")) : rr.at("data");
  const auto masked = l.both(placed, expand(part_lanes(word_address, ac_v)));
  const auto first_sub = l.inv(gather_v);
  // The beat gathered so far including the word taken now.
  const auto gathered_data = l.either(l.pick(first_sub, l.num(p.data_bits, 0), racc), masked);
  const auto arrived_code = arrived_result(decoded);
  const auto gathered_code = l.pick(first_sub, arrived_code, merge(code, arrived_code));
  // With decode packets the flag marks those alone: it fails nothing by itself.
  const auto gathered_error =
      p.decode_flag ? zero : l.pick(first_sub, rr.at("error"), l.either(bad, rr.at("error")));
  l.b.update(code, l.pick(take, gathered_code, code));
  l.b.update(bad, l.pick(take, gathered_error, bad));
  const auto gathered_exclusive = gather_exclusive(first_sub, take);
  // A complete beat leaves with the word that completes it.
  l.b.update(racc, l.pick(take, gathered_data, racc));
  const auto cut_valid =
      io.active(l.both(l.both(serving, l.both(cut_v, l.inv(gathered_v))),
                       l.both(rr.at("valid"), unless_decoded(decoded, beat_end, decoded.ends))));
  if (p.decode_packets)
    l.bind("r.up_wake", l.either(answering, rr.at("valid")));
  const auto out_data = gathered_data, out_code = gathered_code, out_error = gathered_error;
  const auto out_exclusive = p.exclusive ? gathered_exclusive : zero;
  const auto out_address = floor_to(word_address, asize_v);
  // Local SLVERR stream for a rejected read (the issue side keeps its
  // descriptor until the stream ends).
  const auto reject_valid = io.active(l.both(answering, a_reject));
  const auto own_valid = l.either(reject_valid, cut_valid);
  const auto own_pop = l.both(own_valid, up_r_ready);
  const auto own_last = unless_decoded(decoded, l.eq(remaining_v, 0),
                                       [&] { return l.same(remaining_v, decoded.beats); });
  const auto step = l.plus(reject_address, power(asize, ab));
  const auto next_reject = l.pick(
      l.eq(d.at("burst"), 0), reject_address,
      l.pick(l.both(l.eq(d.at("burst"), 2),
                    l.same(l.plus(l.page_offset(reject_address), power(asize, page_bits)), rend)),
             rrestart, step));
  const auto passing = l.both(serving, pass_v);
  const auto pass_pop = l.both(rr.at("valid"), l.both(up_r_ready, io.active(passing)));
  const auto answered =
      l.bind("r.answered", l.either(l.both(own_pop, own_last), l.both(pass_pop, rr.at("last"))));
  const auto piece_answered = l.both(l.either(take, pass_pop), rr.at("last"));
  piece_count::step(l, rcount,
                    multi ? l.both(rq_fire, view(read_gate->serial, read_class)) : rq_fire,
                    multi ? l.both(piece_answered, read_gate->serial) : piece_answered);
  // The issue side ends a burst with its last request; a rejected burst
  // ends with its local stream.
  // A burst whose last piece left as it was taken never holds the issue side.
  const auto issued_all = l.both(rq_fire, l.either(pass_r, last_piece));
  l.b.update(busy, l.pick(rload, l.inv(issued_all),
                          l.pick(l.either(issued_all, l.both(reject_r, l.both(answered, a_reject))),
                                 zero, busy)));
  // Handoff: the response side takes a burst at load when it is free (or
  // frees in that cycle), else the queued burst from the issue side.
  const auto free_now = l.either(l.inv(answering), answered);
  const auto local_handoff = multi ? l.both(rload, l.eq(rplan.mode, 2)) : zero;
  const auto take_in = multi ? local_handoff : l.both(rload, free_now);
  const auto take_queued = multi ? handed_now : l.both(queued, free_now);
  const auto handoff = l.bind("r.handoff", l.either(take_in, take_queued));
  l.b.update(queued, multi ? zero
                           : l.pick(l.both(rload, l.inv(free_now)), one,
                                    l.pick(take_queued, zero, queued)));
  // A packet handed over and answered in the same cycle is never held.
  if (multi)
    l.b.update(answering, l.pick(l.both(handoff, l.inv(l.both(handed_now, answered))), one,
                                 l.pick(l.either(handoff, answered), zero, answering)));
  else
    l.b.update(answering, l.pick(handoff, one, l.pick(answered, zero, answering)));
  auto handed = [&](ValueId from_in, ValueId from_issue, const std::string &field = "") {
    const auto from_response = multi && !field.empty() ? response_metadata.at(field) : from_issue;
    return l.pick(take_in, from_in, from_response);
  };
  auto copy = [&](ValueId state, ValueId from_in, ValueId from_issue, const std::string &field) {
    l.b.update(state, l.pick(handoff, handed(from_in, from_issue, field), state));
  };
  copy(amode, rplan.mode, rmode, "mode");
  copy(ac, rplan.c, rc, "width");
  copy(asize, ri.at("size"), d.at("size"), "size");
  Fields a, a_v;
  for (const auto *name : {"tag", "id", "source"})
    if (d.contains(name)) {
      a[name] = l.state(std::string("r.answer_") + name, l.width(d.at(name)));
      copy(a.at(name), ri.at(name), d.at(name), name);
      a_v[name] = viewed(a.at(name), name);
    }
  const auto length_in = handed(ri.at("length"), d.at("length"), "length");
  if (multi) {
    packet_context::step(l, *read_context, rload, ri.at("tag"), read_metadata, answered,
                         a_v.at("tag"));
    serial_class::step(l, *read_gate, rload, read_class, answered);
  }
  // A handover at load replaces the registers; one at a response's arrival starts from the views
  // and steps with the word served at once.
  const auto loaded = unless_fresh(handoff);
  l.b.update(gather, l.pick(loaded, zero,
                            l.pick(take, unless_decoded(decoded, l.inv(beat_end), decoded.tail),
                                   gather_v)));
  // A FIXED burst's beats restart at its start word, another's next beat at the beat's base.
  const auto start_of_in = start_offset(ri.at("start_address"), rplan.c, ri.at("size")),
             start_of_held = start_offset(d.at("start_address"), rc, d.at("size"));
  const auto start_in = handed(start_of_in, start_of_held, "start");
  copy(astart, start_of_in, start_of_held, "start");
  copy(afixed, l.eq(ri.at("burst"), 0), l.eq(d.at("burst"), 0), "fixed");
  gather_step(decoded, loaded, take, start_in, length_in, own_pop);
  l.b.update(reject_address,
             l.pick(handoff,
                    handed(floor_to(ri.at("start_address"), ri.at("size")),
                           floor_to(d.at("start_address"), d.at("size"))),
                    l.pick(l.both(own_pop, a_reject), next_reject, reject_address)));
  l.b.update(rfirst, l.pick(loaded, one, l.pick(l.either(own_pop, pass_pop), zero, rfirst_v)));
  Fields own;
  for (const auto *name : {"tag", "id", "source"})
    if (a_v.contains(name))
      own[name] = a_v.at(name);
  const auto failed = l.either(out_error, l.inv(l.eq(out_code, 0)));
  const auto reject_v = l.eq(mode_v, 2);
  own["address"] = l.pick(reject_v, reject_address, out_address);
  // A request's count is 2^size (message.hpp point 4), so is its response beats'.
  own["count"] = count_of(asize_v, count_bits);
  own["result"] =
      l.pick(reject_v, l.num(2, 2), unless_decoded(decoded, out_code, [&] {
               return decode_packet::mark_result(l, p.decode_flag, l.inv(l.eq(remaining_v, 0)));
             }));
  // A local miss descriptor continues while beats remain.
  own["error"] =
      l.pick(reject_v, zero,
             unless_decoded(decoded, out_error, [&] { return l.inv(l.eq(remaining_v, 0)); }));
  if (p.exclusive)
    own["exclusive_okay"] = l.both(l.inv(reject_v), out_exclusive);
  // A gathered beat carries the response user field of the word that completes it.
  if (p.response_user_bits)
    own["user"] = l.pick(reject_v, l.num(p.response_user_bits, 0), rr.at("user"));
  own["first"] = rfirst_v;
  own["last"] = own_last;
  own["data"] = l.pick(l.either(reject_v, p.zero_failed_beats ? failed : zero),
                       l.num(p.data_bits, 0), out_data);
  if (p.decode_packets)
    own["data"] = l.pick(l.both(decoded.on, cut_v),
                         decode_packet::word_data(l, decoded.beats, p.data_bits), own.at("data"));
  const Fields passed = passed_response(own.at("count"));
  const Fields up = view_fields(l, own, passed, pass_v);
  // A word while no read is in flight (after a reset of this side alone) is taken and dropped.
  const auto stray = l.bind("r.stray", multi ? l.eq(read_gate->count, 0) : l.inv(answering));
  walk_step(l.either(take, pass_pop));
  if (walks)
    io.out("down_rrsp", "ready",
           l.either(l.both(part_done,
                           l.either(multi ? l.either(low_ready, idle_ready) : low_ready,
                                    io.active(l.both(answering, l.both(a_pass, up_r_ready))))),
                    io.active(stray)));
  else
    io.out("down_rrsp", "ready",
           l.either(multi ? l.either(low_ready, idle_ready) : low_ready,
                    io.active(l.either(stray, l.both(answering, l.both(a_pass, up_r_ready))))));
  io.out("up_rrsp", "valid", l.either(own_valid, io.active(l.both(passing, rr.at("valid")))));
  io.fields_out("up_rrsp", up, p.data_bits, false);
}
void WidthCutLowering::read_reassembly() {
  // Response side where words of different tags may alternate: each tag gathers its own source
  // beat. A word that does not complete a beat is always taken, and one that does leaves with it
  // and waits only for the up face's ready, so no tag's word waits for another tag's. Each word
  // says what it is: its tag names the read, its address where that read's beat ends, and last
  // the end of a packet of one piece. A serial burst (several pieces, or answered here) is alone
  // in flight and counts its beats in the one remaining counter.
  const auto &front = response_metadata;
  const auto word_mode = front.at("mode"), width = front.at("width"), size = front.at("size");
  const auto own_gather = per_tag("gather", 1), own_started = per_tag("started", 1);
  // A word that reaches the top byte of the source word always ends its beat and leaves, so the
  // top of the narrowest target word is never stored.
  const unsigned stored_bits = p.data_bits - (8U << width_set().front());
  const auto own_data = per_tag("data", stored_bits);
  const auto own_code = per_tag("result", 2), own_error = per_tag("error", 1);
  const auto serial = read_gate->serial;
  const auto rejecting = answering;
  const auto first_v = l.inv(own_started.now);
  const auto pass_v = l.eq(word_mode, 0), cut_v = l.eq(word_mode, 1);
  const auto size_mask = low_mask(size, lw);
  walk_run(size);
  const auto decoded = decode_word([&] { return l.b.slice(rr.at("address"), 0, lw); }, width, size);
  const auto word_end = l.same(
      l.both(walks ? part_end : l.either(l.b.slice(rr.at("address"), 0, lw), low_mask(width, lw)),
             size_mask),
      size_mask);
  const auto ends_beat = l.bind("r.beat_end", word_end);
  const auto serving = l.both(l.inv(rejecting), response_present);
  const auto cut_ready = io.active(
      l.both(serving,
             l.both(cut_v, l.either(l.inv(unless_decoded(decoded, ends_beat, one)), up_r_ready))));
  const auto pass_ready = io.active(l.both(serving, l.both(pass_v, up_r_ready)));
  const auto take = l.bind("r.take", l.both(rr.at("valid"), cut_ready));
  const auto pass_pop = l.both(rr.at("valid"), pass_ready);
  const auto word_address = floor_to(rr.at("address"), width);
  const auto placed = narrow_face ? replicate(rr.at("data")) : rr.at("data");
  const auto masked = l.both(placed, expand(part_lanes(word_address, width)));
  const auto gathers = own_gather.now;
  const auto gathered_data =
      l.either(l.pick(gathers, l.zext(own_data.now, p.data_bits), l.num(p.data_bits, 0)), masked);
  const auto arrived_code = arrived_result(decoded);
  const auto gathered_code = l.pick(gathers, merge(own_code.now, arrived_code), arrived_code);
  const auto gathered_error =
      p.decode_flag ? zero
                    : l.pick(gathers, l.either(own_error.now, rr.at("error")), rr.at("error"));
  ValueId gathered_exclusive = zero;
  if (p.exclusive) {
    const auto okay = per_tag("exclusive_okay", 1);
    gathered_exclusive =
        l.pick(gathers, l.both(okay.now, rr.at("exclusive_okay")), rr.at("exclusive_okay"));
    update(okay, l.pick(take, gathered_exclusive, okay.now));
  }
  const auto gathering = l.both(take, unless_decoded(decoded, l.inv(ends_beat), decoded.tail));
  update(own_data, l.pick(gathering, l.b.slice(gathered_data, 0, stored_bits), own_data.now));
  update(own_code, l.pick(gathering, gathered_code, own_code.now));
  update(own_error, l.pick(gathering, gathered_error, own_error.now));
  update(own_gather,
         l.pick(take, unless_decoded(decoded, l.inv(ends_beat), decoded.tail), gathers));
  const auto cut_valid = io.active(l.both(
      serving,
      l.both(cut_v, l.both(rr.at("valid"), unless_decoded(decoded, ends_beat, decoded.ends)))));
  if (p.decode_packets)
    l.bind("r.up_wake", l.either(answering, rr.at("valid")));
  // Local SLVERR stream for a rejected read: the issue side keeps its descriptor until it ends.
  const auto reject_valid = io.active(rejecting);
  const auto reject_pop = l.both(reject_valid, up_r_ready);
  const auto no_beat_left = l.eq(remaining, 0);
  const auto cut_pop = l.both(cut_valid, up_r_ready);
  const auto own_last = l.pick(
      serial,
      unless_decoded(decoded, no_beat_left, [&] { return l.same(remaining, decoded.beats); }),
      rr.at("last"));
  const auto reject_done = l.both(reject_pop, no_beat_left);
  const auto answered =
      l.bind("r.answered", l.either(l.both(cut_pop, own_last), l.both(pass_pop, rr.at("last"))));
  const auto completed = l.either(answered, reject_done);
  const auto word = l.either(take, pass_pop);
  walk_step(word);
  update(own_started,
         l.pick(answered, zero, l.pick(l.either(cut_pop, pass_pop), one, own_started.now)));
  piece_count::step(l, rcount, l.both(rq_fire, view(serial, read_class)),
                    l.both(l.both(word, rr.at("last")), serial));
  // The issue side ends a burst with its last request; a rejected burst ends with its stream.
  const auto issued_all = l.both(rq_fire, l.either(pass_r, last_piece));
  const auto local_handoff = l.both(rload, l.eq(rplan.mode, 2));
  l.b.update(busy, l.pick(rload, l.inv(issued_all),
                          l.pick(l.either(issued_all, reject_done), zero, busy)));
  l.b.update(queued, zero);
  l.b.update(answering, l.pick(local_handoff, one, l.pick(reject_done, zero, answering)));
  packet_context::step(l, *read_context, rload, ri.at("tag"), read_metadata, completed,
                       l.pick(rejecting, d.at("tag"), rr.at("tag")));
  serial_class::step(l, *read_gate, rload, read_class, completed);
  const auto reject_size = d.at("size");
  const auto next_reject = l.pick(
      l.eq(d.at("burst"), 0), reject_address,
      l.pick(l.both(l.eq(d.at("burst"), 2),
                    l.same(l.plus(l.page_offset(reject_address), power(reject_size, page_bits)),
                           rend)),
             rrestart, l.plus(reject_address, power(reject_size, ab))));
  l.b.update(remaining,
             l.pick(l.both(rload, read_class), ri.at("length"),
                    l.pick(l.either(reject_pop, l.both(cut_pop, serial)),
                           l.minus(remaining, unless_decoded(decoded, l.num(lb, 1), decoded.ended)),
                           remaining)));
  l.b.update(reject_address, l.pick(local_handoff, floor_to(ri.at("start_address"), ri.at("size")),
                                    l.pick(reject_pop, next_reject, reject_address)));
  l.b.update(rfirst, l.pick(local_handoff, one, l.pick(reject_pop, zero, rfirst)));
  const auto failed = l.either(gathered_error, l.inv(l.eq(gathered_code, 0)));
  Fields own;
  for (const auto *name : {"tag", "id", "source"})
    if (d.contains(name))
      own[name] =
          l.pick(rejecting, d.at(name), read_metadata.contains(name) ? front.at(name) : d.at(name));
  const auto own_size = l.pick(rejecting, reject_size, size);
  const auto own_address = floor_to(word_address, size);
  own["address"] = l.pick(rejecting, reject_address, own_address);
  own["count"] = count_of(own_size, count_bits);
  own["result"] = l.pick(rejecting, l.num(2, 2), unless_decoded(decoded, gathered_code, [&] {
                           return decode_packet::mark_result(l, p.decode_flag, one);
                         }));
  own["error"] =
      l.pick(rejecting, zero,
             p.decode_flag ? unless_decoded(decoded, gathered_error, one) : gathered_error);
  if (p.exclusive)
    own["exclusive_okay"] = l.both(l.inv(rejecting), gathered_exclusive);
  if (p.response_user_bits)
    own["user"] = l.pick(rejecting, l.num(p.response_user_bits, 0), rr.at("user"));
  own["first"] = l.pick(rejecting, rfirst, first_v);
  own["last"] = l.pick(rejecting, no_beat_left, own_last);
  own["data"] = l.pick(l.either(rejecting, p.zero_failed_beats ? failed : zero),
                       l.num(p.data_bits, 0), gathered_data);
  if (p.decode_packets)
    own["data"] = l.pick(l.both(decoded.on, l.both(cut_v, l.inv(rejecting))),
                         decode_packet::word_data(l, decoded.beats, p.data_bits), own.at("data"));
  const Fields passed = passed_response(own.at("count"));
  const Fields up = view_fields(l, own, passed, l.both(l.inv(rejecting), pass_v));
  // A word while no read is in flight (after a reset of this side alone) is taken and dropped.
  const auto stray = l.bind("r.stray", l.eq(read_gate->count, 0));
  io.out("down_rrsp", "ready",
         l.either(walks ? l.both(part_done, l.either(cut_ready, pass_ready))
                        : l.either(cut_ready, pass_ready),
                  io.active(stray)));
  io.out("up_rrsp", "valid",
         l.either(l.either(reject_valid, cut_valid),
                  io.active(l.both(serving, l.both(pass_v, rr.at("valid"))))));
  io.fields_out("up_rrsp", up, p.data_bits, false);
}
} // namespace

Result<NodeHardware> lower_axi4_width_cut(const Axi4WidthCutProfile &p) {
  const unsigned bytes = p.data_bits / 8;
  const unsigned lw = std::bit_width(bytes) - 1;
  const unsigned ab = p.address_bits, lb = p.len_bits;
  const bool narrow_face = p.down_bits != p.data_bits;
  const bool multi = p.max_outstanding > 1;
  if (!valid_name(p.name) || p.id_bits > 16 || (lb < 4 || lb > 8) ||
      p.source_bits > max_source_bits || ab < 32 || ab > 48 || !source_width(p.data_bits) ||
      (p.down_bits != p.data_bits && p.down_bits != 32) || p.tag_bits > 8 ||
      (p.reassembly && !multi) || p.max_outstanding < 1 || p.max_outstanding > 64 ||
      (p.packet_depth != 1 && p.packet_depth != 4) ||
      ((p.write_hints || p.read_hints) && p.packet_depth == 1) ||
      (multi && p.tag_bits < unsigned(std::bit_width(p.max_outstanding - 1))) ||
      (p.fixed_width && (*p.fixed_width < 2 || *p.fixed_width > lw)) ||
      std::ranges::any_of(p.windows,
                          [&](const auto &w) {
                            return w.second < 2 || w.second > lw || !w.first.size ||
                                   w.first.base + w.first.size > (std::uint64_t{1} << ab);
                          }) ||
      std::ranges::any_of(
          p.fixed_reject,
          [&](const auto &w) { return !w.size || w.base + w.size > (std::uint64_t{1} << ab); }) ||
      (narrow_face && (!p.fixed_width || *p.fixed_width != 2)))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_WIDTH_CUT_PROFILE", "axi4_width_cut",
              "Expected 32..48 address bits, 0..16 ID bits, 4..8 length bits, 0..9 source bits, "
              "64/128/256/512/1024 data bits, target widths between 32 bits and the source "
              "width (a 32-bit down face takes every burst at 32 bits), and packet hints only "
              "behind the packet splitter"));
  Logic l;
  WidthCutLowering engine(l, p);
  engine.write_request();
  engine.write_response();
  engine.read_issue();
  if (p.reassembly)
    engine.read_reassembly();
  else
    engine.read_response();
  return l.finish(p.module_prefix + p.name);
}

Result<NodeHardware> lower_axi4_width_clamp(const Axi4WidthClampProfile &p) {
  const unsigned lw = std::bit_width(p.data_bits / 8) - 1;
  if (!valid_name(p.name) || p.id_bits > 16 || (p.len_bits < 4 || p.len_bits > 8) ||
      p.source_bits > max_source_bits || p.address_bits < 32 || p.address_bits > 48 ||
      !source_width(p.data_bits) || std::ranges::any_of(p.windows, [&](const auto &w) {
        return w.second < 2 || w.second > lw || !w.first.size ||
               w.first.base + w.first.size > (std::uint64_t{1} << p.address_bits);
      }))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_WIDTH_CLAMP_PROFILE", "axi4_width_clamp",
              "Expected 32..48 address bits, 0..16 ID bits, 4..8 length bits, 0..9 source bits, "
              "64/128/256/512/1024 data "
              "bits and windows whose target width lies between 32 bits and the source width"));
  Axi4WidthCutProfile cut;
  cut.name = p.name;
  cut.id_bits = p.id_bits;
  cut.len_bits = p.len_bits;
  cut.address_bits = p.address_bits;
  cut.source_bits = p.source_bits;
  cut.data_bits = p.data_bits;
  cut.down_bits = p.data_bits;
  cut.windows = p.windows;
  cut.fixed_reject = p.fixed_reject;
  cut.piece_len_bits = p.piece_len_bits;
  cut.zero_failed_beats = p.zero_failed_beats;
  cut.decode_packets = p.decode_packets;
  cut.decode_flag = p.decode_flag;
  cut.response_runs = p.response_runs;
  cut.exclusive = p.exclusive;
  cut.tag_bits = p.tag_bits;
  cut.user_bits = p.user_bits;
  cut.response_user_bits = p.response_user_bits;
  cut.max_outstanding = p.max_outstanding;
  cut.packet_depth = p.packet_depth;
  cut.read_granule_taken = p.read_granule_taken;
  cut.write_hints = p.write_hints;
  cut.read_hints = p.read_hints;
  cut.source_miss = p.source_miss;
  cut.reassembly = p.reassembly;
  return lower_axi4_width_cut(cut);
}

std::pair<unsigned, unsigned> axi4_width_clamp_state_bits(const Axi4WidthClampProfile &profile) {
  return direction_state_bits(lower_axi4_width_clamp(profile));
}
} // namespace nocloom::detail
