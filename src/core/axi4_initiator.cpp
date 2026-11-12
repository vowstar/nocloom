#include "axi4_initiator.hpp"
#include "attribute_map.hpp"
#include "beat_stage.hpp"
#include "burst_faces.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "network_plan.hpp"
#include "strobe_lanes.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <map>
#include <numeric>
#include <ranges>
#include <tuple>

namespace nocloom::detail {
namespace {

Diagnostic initiator_error(std::string code, std::string field, std::string message) {
  return error(std::move(code), std::move(field), std::move(message));
}

// 8-way constant select over the 3-bit AxSIZE encoding. Arms above the admitted bus size are
// unreachable on admitted bursts (size_ok gates them); they retain a defined fallback value.
ValueId select_size(logic::Logic &l, ValueId size, const std::array<std::uint64_t, 8> &arms,
                    unsigned width) {
  std::array<ValueId, 8> values{};
  for (unsigned k = 0; k < 8; ++k)
    values[k] = l.b.constant(width, arms[k]);
  return l.by_index(size, values);
}

// The bytes of each AxSIZE as an address step: 2^size up to the larger of 8 bytes and the word.
std::array<std::uint64_t, 8> size_steps(unsigned data_bits) {
  std::array<std::uint64_t, 8> out{};
  for (unsigned k = 0; k < 8; ++k)
    out[k] = std::min<std::uint64_t>(1U << k, std::max(8U, data_bits / 8));
  return out;
}

// Same shape for computed arms.
ValueId select_size(logic::Logic &l, ValueId size, const std::array<ValueId, 8> &arms) {
  return l.by_index(size, arms);
}

struct Descriptor {
  ValueId addr; // captured, zero-extended to the transport address width
  ValueId len, size, burst, lock, cache, prot, id;
  ValueId user;     // the network user flags the burst drives
  ValueId security; // the security flags the burst drives
  ValueId level;    // the scheduling level from the AxQOS map
};

// The face inputs of one address channel (AW or AR), in port order.
struct AddressFace {
  ValueId id, addr, len, size, burst, lock, cache, prot, valid;
  ValueId user;     // the network user flags, with Axi4InitiatorProfile::user_bits
  ValueId security; // the security flags, with Axi4InitiatorProfile::security_bits
  ValueId level;    // the AxQOS map's level, with Axi4InitiatorProfile::level_bits
};

// Admission checks over a captured descriptor. Every violation, and a route miss without a miss
// window, completes the burst locally with SLVERR and emits no request fragment.
struct Admission {
  ValueId admitted;
  // locked: an exclusive burst inside an exclusive window (the lock field).
  ValueId size_ok, exclusive_ok, locked, burst_ok, wrap_ok, fixed_ok, incr_ok;
  ValueId span_cross, budget, span;
  // A well-formed burst outside every admission window that leaves as an error request.
  ValueId missing{};
  // Outside every admission window (with a miss window): the request fields of a burst that leaves
  // follow it without waiting for the other checks, since a burst failing them sends nothing.
  ValueId unmapped{};
  // The burst goes to the network, admitted or as an error request: admitted | missing. With a
  // miss window the route decode cancels out of that, so it stays off the request valid.
  ValueId sent{};
};

// Admission projects the captured transaction ID into a mask of route classes.
ValueId flow_in(logic::Logic &l, const std::vector<IdClassInterval> &ranges, ValueId id,
                std::uint64_t mask) {
  std::vector<ValueId> values;
  for (unsigned k = 0; k < 32; ++k)
    values.push_back(l.bit((mask >> k) & 1));
  return class_value(l.b, id, ranges, values);
}
Admission check_admission(logic::Logic &l, const Axi4InitiatorProfile &p, const Descriptor &d,
                          const std::string &prefix) {
  const unsigned transport_address_bits =
      p.transport_address_bits ? p.transport_address_bits : std::max(32U, p.address_bits);
  Admission a;
  // AxSIZE must stay within the bus width: size < log2(data bytes) + 1.
  const unsigned size_limit = (p.data_bits == 1024  ? 7
                               : p.data_bits == 512 ? 6
                               : p.data_bits == 256 ? 5
                               : p.data_bits == 128 ? 4
                               : p.data_bits == 64  ? 3
                                                    : 2) +
                              1;
  a.size_ok = l.bind(prefix + ".size_ok",
                     p.data_bits == 1024
                         ? l.bit(1)
                         : l.b.binary(Op::UnsignedLess, d.size, l.b.constant(3, size_limit)));
  // AXI A7.2.4: at most 16 power-of-two beats, at most 128 bytes, aligned to total bytes.
  // Window limits and minimum beat sizes also apply. Outside these windows AxLOCK is ignored.
  if (p.exclusive_windows.empty()) {
    a.locked = l.bind(prefix + ".locked", l.bit(0));
    a.exclusive_ok = l.bind(prefix + ".exclusive_ok", l.bit(1));
  } else {
    // A power-of-two beat count has len & (len + 1) == 0 (len is all ones below log2(beats)).
    const auto len_wide = l.b.unary(Op::ZeroExtend, d.len, p.len_bits + 1);
    const auto beats_ok = l.both(
        p.len_bits < 4 ? l.bit(1)
                       : l.b.binary(Op::UnsignedLess, len_wide, l.num(p.len_bits + 1, 16)),
        l.eq(l.b.binary(Op::BitAnd, len_wide, l.plus(len_wide, l.num(p.len_bits + 1, 1))), 0));
    // log2 of the beat count, defined on the power-of-two counts beats_ok admits.
    auto log_beats = l.b.constant(4, 0);
    for (unsigned k = 1; k <= p.len_bits; ++k)
      log_beats = l.b.select(l.eq(d.len, (1U << k) - 1), l.b.constant(4, k), log_beats);
    // log2 of the total bytes: up to 8 + 7.
    const auto log_total = l.bind(prefix + ".exclusive_log_bytes",
                                  l.b.binary(Op::Subtract, log_beats,
                                             l.b.binary(Op::Subtract, l.b.constant(4, 0),
                                                        l.b.unary(Op::ZeroExtend, d.size, 4))));
    std::array<std::uint64_t, 16> masks{};
    for (unsigned t = 0; t < 16; ++t)
      masks[t] = t > 7 ? 0 : (std::uint64_t{1} << t) - 1;
    auto total_mask = l.b.constant(transport_address_bits, masks[15]);
    for (unsigned t = 15; t-- > 0;)
      total_mask = l.b.select(l.eq(log_total, t), l.b.constant(transport_address_bits, masks[t]),
                              total_mask);
    const auto aligned = l.eq(l.b.binary(Op::BitAnd, d.addr, total_mask), 0);
    const auto shape = l.bind(prefix + ".exclusive_shape",
                              l.both(beats_ok, l.both(aligned, l.inv(l.eq(d.burst, 0)))));
    std::vector<ValueId> insides, fitting;
    for (const auto &w : p.exclusive_windows) {
      const auto hit = l.within(d.addr, w.window.base, w.window.base + w.window.size);
      const auto limit = static_cast<unsigned>(std::bit_width(w.max_bytes) - 1);
      auto ok = l.b.binary(Op::UnsignedLess, log_total, l.b.constant(4, limit + 1));
      if (w.min_size)
        ok = l.both(ok, l.inv(l.b.binary(Op::UnsignedLess, d.size, l.b.constant(3, w.min_size))));
      insides.push_back(hit);
      fitting.push_back(l.both(hit, ok));
    }
    const auto inside = l.bind(prefix + ".exclusive_window", l.any(insides));
    const auto fits = l.any(fitting);
    a.locked = l.bind(prefix + ".locked", l.both(d.lock, inside));
    a.exclusive_ok =
        l.bind(prefix + ".exclusive_ok", l.either(l.inv(a.locked), l.both(shape, fits)));
  }
  a.burst_ok = l.bind(prefix + ".burst_ok", l.inv(l.eq(d.burst, 3)));
  // WRAP: length 2/4/8/16 beats and the start address aligned to the beat size.
  auto wrap_len_ok =
      l.either(l.either(l.eq(d.len, 1), l.eq(d.len, 3)), l.either(l.eq(d.len, 7), l.eq(d.len, 15)));
  auto align_mask =
      select_size(l, d.size,
                  {0, 1, 3, 7, p.data_bits >= 128 ? 15U : 0U, p.data_bits >= 256 ? 31U : 0U,
                   p.data_bits >= 512 ? 63U : 0U, p.data_bits == 1024 ? 127U : 0U},
                  transport_address_bits);
  auto wrap_aligned = l.eq(l.b.binary(Op::BitAnd, d.addr, align_mask), 0);
  a.wrap_ok = l.bind(prefix + ".wrap_ok",
                     l.either(l.inv(l.eq(d.burst, 2)), l.both(wrap_len_ok, wrap_aligned)));
  // FIXED stays within 16 beats: AXI4 extends the burst length for INCR only.
  a.fixed_ok =
      l.bind(prefix + ".fixed_ok",
             p.len_bits <= 4
                 ? l.bit(1)
                 : l.either(l.inv(l.eq(d.burst, 0)),
                            l.b.binary(Op::UnsignedLess, d.len, l.b.constant(p.len_bits, 16))));
  // INCR span must not cross a 4 KiB boundary. With the aligned offset a = addr & mask and mask =
  // 4096 - 2^size, the burst crosses exactly when (len * 2^size) > mask - a; both sides fit the
  // transport address width. len * 2^size is len shifted left by size, modulo the transport
  // address space: wiring selected by size, no carry chain.
  const auto span0 = l.b.unary(Op::ZeroExtend, d.len, transport_address_bits);
  // Sizes above the bus width (refused by size_ok) keep the shift of the widest size, at least 3.
  const unsigned widest = std::max(3U, size_limit - 1);
  std::array<ValueId, 8> spans{};
  spans[0] = span0;
  for (unsigned k = 1; k < 8; ++k) {
    const unsigned n = std::min(k, widest);
    spans[k] = l.b.concat(l.b.slice(span0, 0, transport_address_bits - n), l.num(n, 0));
  }
  a.span = l.bind(prefix + ".span", select_size(l, d.size, spans));
  const auto size4_mask = p.data_bits >= 128 ? 4080U : 4088U;
  const auto size5_mask = p.data_bits >= 256 ? 4064U : size4_mask;
  const auto size6_mask = p.data_bits >= 512 ? 4032U : size5_mask;
  const auto wide_mask = p.data_bits == 1024 ? 3968U : size6_mask;
  auto mask = select_size(l, d.size,
                          {4095, 4094, 4092, 4088, size4_mask, size5_mask, size6_mask, wide_mask},
                          transport_address_bits);
  auto offset = l.b.binary(Op::BitAnd, d.addr, mask);
  a.budget = l.bind(prefix + ".budget", l.b.binary(Op::Subtract, mask, offset));
  a.span_cross = l.bind(prefix + ".span_cross", l.b.binary(Op::UnsignedLess, a.budget, a.span));
  a.incr_ok = l.bind(prefix + ".incr_ok", l.either(l.inv(l.eq(d.burst, 1)), l.inv(a.span_cross)));
  // A locked INCR past floor(start, align) + max_bytes or the next boundary of the splitter window
  // holding its start would be cut: it fails whole. An admitted INCR stays in its page, so with
  // m = 2^size - 1 the last byte less floor(start, align) is ((start | m) & (align - 1)) + span,
  // which reaches a power-of-two limit when a bit at or above it is set (no compare).
  const unsigned w = transport_address_bits + 1;
  const auto start = l.zext(d.addr, w);
  constexpr unsigned run = 14;
  const auto beat_last =
      l.zext(l.either(l.b.slice(d.addr, 0, 12),
                      l.per_size(d.size, 7, [&](unsigned n) { return l.num(12, (1U << n) - 1); })),
             run);
  const auto span = l.b.slice(a.span, 0, run);
  // The first splitter window holding the start decides; windows of equal limits share one test.
  std::vector<AddressWindow> windows;
  std::vector<unsigned> classes;
  std::vector<ValueId> cuts;
  std::map<std::tuple<bool, std::uint64_t, std::uint64_t, std::uint64_t>, unsigned> seen;
  for (const auto &x : p.lock_windows) {
    const auto past = [&](std::uint64_t align, std::uint64_t limit) {
      const auto k = static_cast<unsigned>(std::countr_zero(limit));
      if (k >= run)
        return l.bit(0);
      const auto reach = l.plus(l.both(beat_last, l.num(run, (align - 1) & 4095)), span);
      return l.inv(l.eq(l.b.slice(reach, k, run - k), 0));
    };
    windows.push_back(x.window);
    const auto [at, fresh] =
        seen.try_emplace({x.checked, x.align_bytes, x.max_bytes, x.boundary_bytes},
                         static_cast<unsigned>(cuts.size()));
    if (fresh)
      cuts.push_back(x.checked ? l.either(past(x.align_bytes, x.max_bytes),
                                          past(x.boundary_bytes, x.boundary_bytes))
                               : l.bit(0));
    classes.push_back(at->second);
  }
  const auto cut = l.window_class(start, windows, classes, cuts, l.bit(0));
  const auto lock_ok =
      l.bind(prefix + ".lock_ok", l.inv(l.both(d.lock, l.both(l.eq(d.burst, 1), cut))));
  const std::array checks{a.size_ok, lock_ok,    a.exclusive_ok, a.burst_ok,
                          a.wrap_ok, a.fixed_ok, a.incr_ok};
  auto admitted = l.every(checks);
  const auto legal = admitted;
  a.missing = a.unmapped = l.bit(0);
  if (p.admitted_windows) {
    // Adjacent admitted windows are compared as one interval; a bound at the edge of the transport
    // address space needs no comparison.
    // Windows of one class mask merge among themselves and admit the burst only when its
    // captured AxID selects a class in that mask.
    std::map<std::uint64_t, std::vector<AddressWindow>> by_mask;
    for (std::size_t k = 0; k < p.admitted_windows->size(); ++k)
      by_mask[p.admitted_flows.empty() ? 0 : p.admitted_flows[k]].push_back(
          (*p.admitted_windows)[k]);
    std::vector<ValueId> maps;
    for (auto &[flow_mask, intervals] : by_mask) {
      std::ranges::sort(intervals, {}, &AddressWindow::base);
      std::vector<AddressWindow> merged;
      for (const auto &window : intervals)
        if (!merged.empty() && merged.back().base + merged.back().size == window.base)
          merged.back().size += window.size;
        else
          merged.push_back(window);
      std::vector<ValueId> held;
      std::ranges::transform(merged, std::back_inserter(held), [&](const AddressWindow &window) {
        return l.within(d.addr, window.base, window.base + window.size);
      });
      if (p.admitted_flows.empty()) {
        maps = std::move(held);
        break;
      }
      maps.push_back(l.both(l.any(held), flow_in(l, p.flows, d.id, flow_mask)));
    }
    const auto mapped = l.bind(prefix + ".route_mapped", l.any(maps));
    if (p.miss_window) {
      a.missing = l.bind(prefix + ".missing", l.both(admitted, l.inv(mapped)));
      a.unmapped = l.inv(mapped);
    }
    admitted = l.both(admitted, mapped);
  }
  a.admitted = l.bind(prefix + ".admitted", admitted);
  a.sent = p.miss_window ? legal : a.admitted;
  return a;
}

// Internal result {0 success, 1 network_miss, 2 target_error} plus the transport error fold mapped
// to the AXI response encoding. A success with exclusive_okay (when carried) is EXOKAY.
ValueId map_result(logic::Logic &l, ValueId result, ValueId error_fold, bool uniform,
                   std::optional<ValueId> exclusive_okay = std::nullopt) {
  const auto okay = exclusive_okay
                        ? l.b.select(*exclusive_okay, l.b.constant(2, 1), l.b.constant(2, 0))
                        : l.b.constant(2, 0);
  const auto slverr = l.b.constant(2, 2);
  if (uniform)
    return l.b.select(l.either(error_fold, l.inv(l.eq(result, 0))), slverr, okay);
  const auto decerr = l.b.constant(2, 3);
  auto bad = l.either(error_fold, l.inv(l.eq(result, 0)));
  return l.b.select(l.eq(result, 1), decerr, l.b.select(bad, slverr, okay));
}

// Several bursts in flight on one direction (max_outstanding > 1). The slot table holds one slot
// per burst accepted at the AXI face and not yet answered; the slot number is the burst's Weft tag.
// The ID table holds, per active AXI ID, its destination and the count of its bursts issued and not
// yet answered: every burst of one ID in flight goes to one destination.
struct Tracker {
  unsigned slots = 0, tag_bits = 0;
  std::vector<ValueId> valid;
  std::vector<std::map<std::string, ValueId>> fields;
  std::vector<ValueId> count, ids, dests;
  ValueId room, alloc;
};
Tracker open_tracker(logic::Logic &l, const std::string &prefix, unsigned slots, unsigned entries,
                     unsigned id_bits, unsigned dest_bits,
                     const std::vector<std::pair<std::string, unsigned>> &fields) {
  Tracker t;
  t.slots = slots;
  t.tag_bits = static_cast<unsigned>(std::bit_width(slots - 1));
  std::vector<ValueId> vacant;
  for (unsigned k = 0; k < slots; ++k) {
    const auto at = prefix + ".s" + std::to_string(k) + ".";
    t.valid.push_back(l.state(at + "valid", 1));
    std::map<std::string, ValueId> slot;
    for (const auto &[name, width] : fields)
      slot[name] = l.state(at + name, width);
    t.fields.push_back(std::move(slot));
    vacant.push_back(l.inv(t.valid.back()));
  }
  const unsigned count_bits = static_cast<unsigned>(std::bit_width(slots));
  for (unsigned j = 0; j < entries; ++j) {
    const auto at = prefix + ".i" + std::to_string(j) + ".";
    t.count.push_back(l.state(at + "count", count_bits));
    if (id_bits)
      t.ids.push_back(l.state(at + "id", id_bits));
    t.dests.push_back(l.state(at + "dest", dest_bits));
  }
  // The lowest free slot takes the next captured burst.
  auto room = l.bit(0);
  auto alloc = l.b.constant(t.tag_bits, 0);
  for (unsigned k = slots; k-- > 0;) {
    room = l.either(room, vacant[k]);
    alloc = l.b.select(vacant[k], l.b.constant(t.tag_bits, k), alloc);
  }
  t.room = l.bind(prefix + ".room", room);
  t.alloc = l.bind(prefix + ".alloc", alloc);
  return t;
}
// A slot field selected by tag; an empty name selects the slot's taken flag.
ValueId slot_field(logic::Logic &l, const Tracker &t, const std::string &name, ValueId tag) {
  auto at = [&](unsigned k) { return name.empty() ? t.valid[k] : t.fields[k].at(name); };
  auto value = at(0);
  for (unsigned k = 1; k < t.slots; ++k)
    value = l.b.select(l.eq(tag, k), at(k), value);
  return value;
}
// The ID table entries that hold id: at most one, or without AXI IDs the single one, every burst's.
std::vector<ValueId> id_matches(logic::Logic &l, const Tracker &t, ValueId id) {
  std::vector<ValueId> out;
  for (std::size_t j = 0; j < t.count.size(); ++j) {
    auto match = l.inv(l.eq(t.count[j], 0));
    if (!t.ids.empty())
      match = l.both(match, l.b.binary(Op::Equal, t.ids[j], id));
    out.push_back(match);
  }
  return out;
}
// The issue rule: a burst leaves the issue stage when its ID is bound to the burst's own
// destination, or is idle while the ID table has a free entry.
ValueId tracker_admits(logic::Logic &l, const Tracker &t, const std::string &prefix, ValueId id,
                       ValueId dest) {
  const auto match = id_matches(l, t, id);
  auto hit = l.bit(0), same = l.bit(0), vacant = l.bit(0);
  for (std::size_t j = 0; j < match.size(); ++j) {
    hit = l.either(hit, match[j]);
    same = l.either(same, l.both(match[j], l.b.binary(Op::Equal, t.dests[j], dest)));
    vacant = l.either(vacant, l.eq(t.count[j], 0));
  }
  hit = l.bind(prefix + ".id_hit", hit);
  same = l.bind(prefix + ".id_same", same);
  vacant = l.bind(prefix + ".id_vacant", vacant);
  return l.either(same, l.both(l.inv(hit), vacant));
}
// capture: an address handshake takes slot alloc (its tag) and writes the captured fields. issue:
// the burst of slot issue_tag leaves the issue stage, counted under its ID, binding a new ID to
// dest (with rebind, every ID it issues under), and writes the issued fields. frees: (fire, tag)
// ID triples, each a burst whose answer entered the engine (its last response packet, or a local
// completion taking the face or the staging), released with its slot from its ID's count; two fire
// together only for different IDs. A burst's response beat staged for the AXI face keeps its own
// ID, so a slot lives only as long as the network holds its burst.
void close_tracker(logic::Logic &l, const Tracker &t, ValueId capture,
                   const std::map<std::string, ValueId> &captured, ValueId issue, ValueId issue_tag,
                   const std::map<std::string, ValueId> &issued, ValueId id, ValueId dest,
                   bool rebind, const std::vector<std::tuple<ValueId, ValueId, ValueId>> &frees) {
  for (unsigned k = 0; k < t.slots; ++k) {
    const auto fill = l.both(capture, l.eq(t.alloc, k));
    const auto mark = l.both(issue, l.eq(issue_tag, k));
    auto release = l.bit(0);
    for (const auto &[fire, tag, freed_id] : frees)
      release = l.either(release, l.both(fire, l.eq(tag, k)));
    l.b.update(t.valid[k], l.b.select(fill, l.bit(1), l.b.select(release, l.bit(0), t.valid[k])));
    for (const auto &[name, reg] : t.fields[k]) {
      auto next = reg;
      if (const auto it = captured.find(name); it != captured.end())
        next = l.b.select(fill, it->second, next);
      if (const auto it = issued.find(name); it != issued.end())
        next = l.b.select(mark, it->second, next);
      l.b.update(reg, next);
    }
  }
  const auto match = id_matches(l, t, id);
  std::vector<std::vector<ValueId>> finished;
  for (const auto &[fire, tag, freed_id] : frees)
    finished.push_back(id_matches(l, t, t.ids.empty() ? id : freed_id));
  const auto hit = std::accumulate(match.begin(), match.end(), l.bit(0),
                                   [&](ValueId any, ValueId m) { return l.either(any, m); });
  // A new ID takes the lowest free entry.
  auto earlier = l.bit(0);
  for (std::size_t j = 0; j < t.count.size(); ++j) {
    const auto vacant = l.eq(t.count[j], 0);
    const auto take = l.both(l.inv(hit), l.both(vacant, l.inv(earlier)));
    earlier = l.either(earlier, vacant);
    const auto up = l.both(issue, l.either(match[j], take));
    auto down = l.bit(0);
    for (std::size_t f = 0; f < frees.size(); ++f)
      down = l.either(down, l.both(std::get<0>(frees[f]), finished[f][j]));
    l.b.update(t.count[j],
               l.b.select(l.both(up, l.inv(down)), l.inc(t.count[j]),
                          l.b.select(l.both(down, l.inv(up)), l.dec(t.count[j]), t.count[j])));
    const auto bind = l.both(issue, rebind ? l.either(match[j], take) : take);
    if (!t.ids.empty())
      l.b.update(t.ids[j], l.b.select(bind, id, t.ids[j]));
    l.b.update(t.dests[j], l.b.select(bind, dest, t.dests[j]));
  }
}
// Destination of a captured burst: the target whose routing window holds the start address, the
// network's miss target when no window holds it, and the local completion (index
// destinations.size()) for an admission failure. The same windows and miss rule drive the network's
// request routing. With admission windows an admitted address lies in one of them, so only the
// windows inside them are compared, and the last one needs no comparison unless an admitted page
// is shared (crossing), where last is the destination of the burst's last beat.
std::pair<ValueId, ValueId> destination(logic::Logic &l, const Axi4InitiatorProfile &p,
                                        const Descriptor &d, const Admission &a,
                                        unsigned transport_address_bits,
                                        const std::string &prefix) {
  const auto targets = static_cast<unsigned>(p.destinations.size());
  const unsigned width = std::max(1U, static_cast<unsigned>(std::bit_width(targets)));
  std::vector<unsigned> compared;
  for (unsigned k = 0; k < targets; ++k)
    if (const auto &w = p.destinations[k];
        std::ranges::find(p.hidden_destinations, k) == p.hidden_destinations.end() &&
        (!p.admitted_windows || std::ranges::any_of(*p.admitted_windows, [&](const auto &x) {
          return w.base >= x.base && w.base + w.size <= x.base + x.size;
        })))
      compared.push_back(k);
  unsigned fallback = p.miss_destination ? *p.miss_destination : targets;
  if (p.admitted_windows && !compared.empty() && !p.crossing) {
    fallback = compared.back();
    compared.pop_back();
  }
  const unsigned w = transport_address_bits + 1;
  auto decode = [&](ValueId address) {
    const auto wide = l.zext(address, w);
    std::vector<ValueId> hits, values;
    for (auto k : compared) {
      const auto &x = p.destinations[k];
      hits.push_back(
          l.both(l.inv(l.below(wide, l.num(w, x.base))), l.below(wide, l.num(w, x.base + x.size))));
      values.push_back(l.b.constant(width, k));
    }
    return l.first_hit(hits, values, l.b.constant(width, fallback));
  };
  // A miss sent as an error request goes to the miss window's target.
  auto other = l.b.constant(width, targets);
  if (p.miss_window)
    other = l.b.select(a.missing, l.b.constant(width, *p.miss_destination), other);
  const auto first = l.bind(prefix + ".dest", l.b.select(a.admitted, decode(d.addr), other));
  auto last_dest = first;
  if (p.crossing) {
    // The last beat: FIXED at the start, INCR span bytes past the aligned start, WRAP that within
    // its container (span plus one beat).
    const auto start = l.zext(d.addr, w), span = l.zext(a.span, w), bytes = l.pow2(d.size, w, 7);
    const auto mask = l.pick(l.eq(d.burst, 2), l.either(span, l.minus(bytes, l.num(w, 1))),
                             l.num(w, (std::uint64_t{1} << w) - 1));
    const auto ahead = l.both(l.plus(l.floor(start, bytes), span), mask);
    const auto last = l.pick(l.eq(d.burst, 0), start,
                             l.either(l.both(start, l.b.unary(Op::BitNot, mask, w)), ahead));
    last_dest =
        l.bind(prefix + ".last_dest",
               l.pick(a.admitted, decode(l.b.slice(last, 0, transport_address_bits)), other));
  }
  return {first, last_dest};
}

// The destination an ID binds to in the engine. With a late binding only a local completion differs
// from the network: the merger keeps the order between network destinations.
std::pair<ValueId, ValueId> bound_destination(logic::Logic &l, const Axi4InitiatorProfile &p,
                                              const std::pair<ValueId, ValueId> &dest) {
  if (!p.late_id_binding)
    return dest;
  const auto targets = static_cast<unsigned>(p.destinations.size());
  const auto local = l.eq(dest.first, targets);
  const auto width = l.width(dest.first);
  const auto kind = l.b.select(local, l.b.constant(width, targets), l.b.constant(width, 0));
  return {kind, kind};
}

// One direction's captured descriptor: each field in a "<dir>.desc.<f>" register loaded by the
// address handshake (or the start of a held burst) and viewed as the face value while `pass` holds
// — an idle or fresh engine passes a handshake straight through: the request reads the face in the
// handshake cycle (it depends on the source side only, never on a ready), and a request not taken
// waits in the descriptor registers loaded by that handshake. The tracked burst's slot tag gets
// the same treatment ("<dir>.tag").
struct CapturedDescriptor {
  Descriptor d{};
  // The AxID register of the burst in service (the face value while `pass` holds is in d.id).
  ValueId id_reg{};
  ValueId addr32{}, tag{}, addr_reg{}, burst_reg{};
};
CapturedDescriptor capture_descriptor(logic::Logic &l, const Axi4InitiatorProfile &p,
                                      const Tracker &track, const std::string &dir, ValueId fire,
                                      std::optional<ValueId> pass, bool multi,
                                      const AddressFace &face) {
  CapturedDescriptor cap;
  if (multi) {
    cap.tag = l.state(dir + ".tag", track.tag_bits);
    l.b.update(cap.tag, l.b.select(fire, track.alloc, cap.tag));
  }
  const unsigned transport_address_bits =
      p.transport_address_bits ? p.transport_address_bits : std::max(32U, p.address_bits);
  cap.addr32 = p.address_bits < transport_address_bits
                   ? l.b.unary(Op::ZeroExtend, face.addr, transport_address_bits)
                   : face.addr;
  // An AXI4-Lite access always uses the full bus width (AMBA AXI IHI0022H
  // B1.1): the address bits below the bus width are cleared before any
  // admission, window or 4 KiB check and before the start address is carried,
  // so an unaligned AxADDR addresses its containing word. WSTRB alone selects
  // the written bytes (A3.4) and is not checked against the cleared bits; a
  // read returns the whole word.
  if (p.lite)
    cap.addr32 = l.b.binary(
        Op::BitAnd, cap.addr32,
        l.b.constant(transport_address_bits, (std::uint64_t{1} << transport_address_bits) -
                                                 (p.apb ? p.apb_data_bits / 8 : p.data_bits / 8)));
  // An APB manager holds its transfer on the face until PREADY (AMBA APB IHI0024 3.1), and the
  // engine answers it only after the burst has left: the face is the descriptor.
  auto capture = [&](const std::string &field, unsigned width, ValueId value) {
    if (p.apb && !multi)
      return value;
    const auto reg = l.state(dir + ".desc." + field, width);
    if (field == "id")
      cap.id_reg = reg;
    if (field == "addr")
      cap.addr_reg = reg;
    if (field == "burst")
      cap.burst_reg = reg;
    l.b.update(reg, l.b.select(fire, value, reg));
    return pass ? l.b.select(*pass, value, reg) : value;
  };
  cap.d.addr = capture("addr", transport_address_bits, cap.addr32);
  cap.d.len = capture("len", p.len_bits, face.len);
  cap.d.size = capture("size", 3, face.size);
  cap.d.burst = capture("burst", 2, face.burst);
  cap.d.lock = capture("lock", 1, face.lock);
  cap.d.cache = capture("cache", 4, face.cache);
  cap.d.prot = capture("prot", 3, face.prot);
  cap.d.id = p.id_bits ? capture("id", p.id_bits, face.id) : face.id;
  if (!p.id_bits)
    cap.id_reg = face.id;

  if (p.user_bits)
    cap.d.user = capture("user", p.user_bits, face.user);
  if (p.security_bits)
    cap.d.security = capture("security", p.security_bits, face.security);
  if (p.level_bits)
    cap.d.level = p.axqos_map.empty() ? face.level : capture("level", p.level_bits, face.level);
  return cap;
}

// A decode miss sent as an error request: its address moved into the miss window, keeping the
// offset in a 4 KiB page when the window holds whole pages.
ValueId missed(logic::Logic &l, const Axi4InitiatorProfile &p, ValueId missing, ValueId address,
               ValueId value) {
  if (!p.miss_window)
    return value;
  if (p.miss_kept) {
    const auto width = l.width(address);
    const auto low =
        l.num(width, p.miss_kept->bits &
                         (width >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << width) - 1));
    return l.pick(
        missing,
        l.either(l.num(width, p.miss_kept->base & ~p.miss_kept->bits), l.both(address, low)),
        value);
  }
  return l.pick(missing, l.moved_to_miss(*p.miss_window, address), value);
}

// A request's record field (Axi4InitiatorProfile::record_bits). A FIXED burst counts whole words.
ValueId record_field(logic::Logic &l, const Axi4InitiatorProfile &p, const Descriptor &d,
                     const Admission &a) {
  const auto width = p.record_bits - 1;
  const auto word_size = l.num(3, static_cast<unsigned>(std::countr_zero(p.data_bits / 8)));
  const auto fixed = l.eq(d.burst, 0);
  const auto size = l.pick(fixed, word_size, d.size);
  auto wide = l.fit(d.len, width), low = l.num(width, 0);
  for (unsigned n = 1; n < 8; ++n) {
    const auto shifted = n < width
                             ? l.b.concat(l.b.slice(l.fit(d.len, width), 0, width - n), l.num(n, 0))
                             : l.num(width, 0);
    wide = l.pick(l.eq(size, n), shifted, wide);
    low = l.pick(l.eq(size, n), l.num(width, (std::uint64_t{1} << std::min(n, width)) - 1), low);
  }
  const auto offset = l.pick(fixed, l.num(width, 0), l.both(l.fit(d.addr, width), low));
  return l.b.concat(l.both(a.unmapped, d.lock), l.minus(l.either(wide, low), offset));
}

// The request fields carrying the captured descriptor, in contract order (message.hpp). The tag
// is the burst's slot, zero-extended to the Weft field; zero with one burst per direction.
void request_descriptor_fields(logic::Logic &l, const Axi4InitiatorProfile &p,
                               std::map<std::string, ValueId> &rq, const Descriptor &d,
                               const Admission &a, ValueId write, ValueId length,
                               std::optional<ValueId> tag_slot) {
  rq["write"] = write;
  rq["protection"] = d.prot;
  rq["length"] = length;
  rq["size"] = d.size;
  rq["burst"] = d.burst;
  rq["cache"] = d.cache;
  if (p.exclusive)
    rq["lock"] = a.locked;
  if (p.user_bits)
    rq["user"] = d.user;
  if (p.record_bits)
    rq["record"] = record_field(l, p, d, a);
  if (p.security_bits)
    rq["security"] = d.security;
  if (p.start_address)
    rq["start_address"] = missed(l, p, a.unmapped, d.addr, d.addr);
  if (p.tag_bits)
    rq["tag"] = tag_slot ? (p.tag_bits > l.b.ir.values.at(tag_slot->index).width
                                ? l.b.unary(Op::ZeroExtend, *tag_slot, p.tag_bits)
                                : *tag_slot)
                         : l.b.constant(p.tag_bits, 0);
  if (p.id_bits)
    rq["id"] = d.id;
  if (p.source_bits)
    rq["source"] = l.b.constant(p.source_bits, 0);
  if (p.level_bits)
    rq["level"] = d.level;
  rq["count"] = select_size(l, d.size, size_steps(p.data_bits), burst_count_bits(p.data_bits));
  rq["ends"] = beat_ends(l, rq.at("address"), d.size, p.data_bits / 8); // message.hpp point 4
  rq["wrap_end"] = l.b.constant(1, 0); // a write beat's is set beside its address
  rq["error"] = a.unmapped;
}

void initiator_width_errors(const Axi4InitiatorProfile &profile, std::vector<Diagnostic> &errors) {
  if (!valid_name(profile.name))
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_NAME", "name",
                                     "The module name suffix must be a valid identifier"));
  if ((profile.data_bits != 32 && profile.data_bits != 64 && profile.data_bits != 128 &&
       profile.data_bits != 256 && profile.data_bits != 512 && profile.data_bits != 1024) ||
      (profile.lite && profile.data_bits > 64))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_DATA_WIDTH", "data_bits",
        "AXI4 ingress supports 32/64/128/256/512/1024 data bits; Lite remains 32/64"));
  const unsigned address_minimum = profile.apb ? 1U : profile.lite ? 2U : 1U;
  if (profile.address_bits < address_minimum || profile.address_bits > 48)
    errors.push_back(
        initiator_error("NCL_AXI4_INITIATOR_ADDRESS_WIDTH", "address_bits",
                        "Ingress addresses support 1..48 bits; AXI4-Lite requires at least 2"));
  if (profile.transport_address_bits &&
      (profile.address_bits < (profile.apb    ? 1U
                               : profile.lite ? 2U
                                              : 12U) ||
       profile.transport_address_bits < std::max(32U, profile.address_bits) ||
       profile.transport_address_bits > 48))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_ADDRESS_WIDTH", "transport_address_bits",
        "Explicit transport requires the protocol source minimum (APB 1, Lite 2, AXI4 12) "
        "and a containing 32..48-bit path"));
  if (profile.id_bits > 16)
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_ID_WIDTH", "id_bits",
                                     "The full AXI4 ingress profile supports 0..16 ID bits"));
  if (profile.len_bits < 4 || profile.len_bits > 8)
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_LEN_WIDTH", "len_bits",
                                     "The AxLEN field width must be 4 to 8"));
}

void initiator_capacity_errors(const Axi4InitiatorProfile &profile,
                               std::vector<Diagnostic> &errors) {
  if (!profile.read)
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_DIRECTION", "read",
                                     "Write-only initiators are unsupported; only read-write "
                                     "and read-only sockets are provided"));
  if (profile.max_outstanding == 0 || profile.max_outstanding > 64)
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_CAPACITY", "max_outstanding",
                                     "The outstanding capacity must be 1 to 64"));
  else if ((profile.miss_window && !profile.admitted_windows) ||
           (profile.max_outstanding > 1 &&
            (profile.destinations.empty() || profile.destinations.size() > max_destinations ||
             profile.miss_destination.has_value() !=
                 (!profile.admitted_windows || profile.miss_window) ||
             (profile.miss_destination &&
              *profile.miss_destination >= profile.destinations.size()))))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_CAPACITY", "max_outstanding",
        "A miss window needs admission windows; multiple outstanding bursts need 1..256 "
        "destination windows and a miss destination exactly when misses go to the network"));
  if (profile.tag_bits > 6 ||
      (profile.max_outstanding > 1 && profile.max_outstanding <= 64 && profile.tag_bits &&
       profile.tag_bits < static_cast<unsigned>(std::bit_width(profile.max_outstanding - 1))))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_CAPACITY", "tag_bits",
        "The Weft tag field must number every burst in flight and is at most 6 bits"));
  if (profile.apb &&
      (!profile.lite || !profile.read || !profile.write || profile.data_bits != 32 ||
       profile.max_outstanding != 1 || profile.address_bits > 32 ||
       (profile.apb_data_bits != 8 && profile.apb_data_bits != 16 && profile.apb_data_bits != 32) ||
       (*profile.apb != Protocol::Apb4 && *profile.apb != Protocol::Apb3)))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_APB", "apb",
        "An APB face needs a read-write 32-bit Lite engine with at most 32 address bits and "
        "APB4 or APB3"));
  if (profile.ahb && (profile.lite || profile.apb || !profile.read || !profile.write ||
                      profile.max_outstanding != 1 || profile.address_bits < 12))
    errors.push_back(initiator_error(
        "NCL_AXI4_INITIATOR_AHB", "ahb",
        "An AHB face needs a read-write full AXI4 engine with one burst per direction and at "
        "least 12 address bits"));
  if (profile.axi3 && (profile.lite || profile.apb || profile.ahb))
    errors.push_back(initiator_error("NCL_AXI4_INITIATOR_AXI3", "axi3",
                                     "AXI3 ports need a full AXI4 engine without another face"));
}

void initiator_window_errors(const Axi4InitiatorProfile &profile, std::vector<Diagnostic> &errors) {
  const unsigned transport_address_bits = profile.transport_address_bits
                                              ? profile.transport_address_bits
                                              : std::max(32U, profile.address_bits);
  if (profile.admitted_windows) {
    const auto limit = std::uint64_t{1} << transport_address_bits;
    const auto alignment =
        profile.lite ? (profile.apb ? profile.apb_data_bits : profile.data_bits) / 8 : 4096U;
    if (std::ranges::any_of(*profile.admitted_windows, [&](const auto &w) {
          return !w.size || w.base >= limit || w.size > limit - w.base || w.base % alignment ||
                 w.size % alignment;
        }))
      errors.push_back(
          initiator_error("NCL_AXI4_INITIATOR_ROUTE_WINDOW", "admitted_windows",
                          "Admission windows must fit the transport address range and align to "
                          "source words for APB/AXI4-Lite or 4 KiB blocks for full AXI4"));
  }
}

// One initiator lowering: the builder, the resolved profile and the shared derived constants,
// plus the APB face values the face capture hands to the tail.
struct InitiatorLowering {
  InitiatorLowering(logic::Logic &logic, const Axi4InitiatorProfile &profile)
      : l(logic), p(profile),
        transport_address_bits(p.transport_address_bits ? p.transport_address_bits
                                                        : std::max(32U, p.address_bits)),
        largest_size(p.data_bits == 1024  ? 7
                     : p.data_bits == 512 ? 6
                     : p.data_bits == 256 ? 5
                     : p.data_bits == 128 ? 4
                                          : 3),
        be_bits(p.data_bits / 8), face_bytes(p.apb ? p.apb_data_bits / 8 : be_bits),
        lite_size(p.apb ? std::countr_zero(face_bytes) : (p.data_bits == 64 ? 3U : 2U)),
        one(l.bit(1)), zero(l.bit(0)),
        burst{p.id_bits, p.len_bits, p.start_address, transport_address_bits},
        multi(p.max_outstanding > 1), held(!p.lite && !p.apb && !p.ahb && p.port_pipes),
        dest_bits(std::max(1U, static_cast<unsigned>(std::bit_width(p.destinations.size())))),
        // An AXI4-Lite face carries no AXI ID: all its transactions share one ordering key per
        // direction. A full face binds every ID it can have in flight.
        id_entries(p.bound_ids
                       ? p.bound_ids
                       : std::min(p.max_outstanding, p.lite ? 1U : 1U << std::min(p.id_bits, 6U))) {
    burst.exclusive = p.exclusive;
    burst.tag_bits = p.tag_bits;
    burst.user_bits = p.user_bits;
    burst.record_bits = p.record_bits;
    burst.security_bits = p.security_bits;
    burst.response_user_bits = p.response_user_bits;
    burst.level_bits = p.level_bits;
    // Bound constants double as designated fault-injection anchors.
    l.bind("c.zero", zero);
    l.bind("c.one", one);
  }

  logic::Logic &l;
  const Axi4InitiatorProfile &p;
  const unsigned transport_address_bits, largest_size;
  const unsigned be_bits, face_bytes, lite_size;
  const ValueId one, zero;
  BurstProfile burst;
  // Multiple outstanding bursts: a slot table and an ID table per direction.
  const bool multi;
  // A full AXI4 or AXI3 port engine leaves AW and AR in the port pipe until the burst leaves the
  // issue stage: AR as its request goes, AW with its last W beat. The engine starts the burst
  // from the face (start; with several bursts it takes its slot then), and with one burst per
  // direction starts the next one in the cycle the previous burst's B or last R beat leaves.
  const bool held;
  const unsigned dest_bits, id_entries;
  // The engine's AXI inputs and outputs behind an APB or AHB face.
  std::map<std::string, ValueId> face_in, face_out;
  ValueId apb_access{}, apb_read_address{};
  // AHB face values handed from ahb_face to ahb_tail.
  struct AhbFace {
    ValueId hsel{}, htrans{}, hready{}, phase{}, write{}, sent{}, done{}, err{}, end{}, left{};
    ValueId flight{}, open_write{}, bad{}, serve{}, posted{}, len{}, beat_last{}, awvalid{};
    ValueId arvalid{}, wvalid{}, rready{};
  } ahb{};

  // Write-side handles handed from write_issue to write_complete.
  AddressFace w_aw{};
  ValueId w_wdata{}, w_wstrb{}, w_wlast{}, w_wid{}, w_bready{}, w_wrsp_valid{};
  std::map<std::string, ValueId> w_wrsp_in;
  ValueId w_wr_state{}, w_remaining{}, w_beat_addr{}, w_poisoned{};
  ValueId w_beat_full{}, w_beat_data{}, w_beat_be{}, w_bresp_reg{};
  ValueId w_idle{}, w_waiting{}, w_respond{};
  ValueId w_awready{}, w_aw_fire{}, w_straight{}, w_start{};
  Tracker w_track{};
  CapturedDescriptor w_cap{};
  Admission w_admission{};
  ValueId w_load_len{}, w_issue_tag{}, w_aligned_input{}, w_beat_address{}, w_addr_next{};
  ValueId w_left{}, w_frag_last{}, w_frag_first{}, w_sent{}, w_carry{}, w_wrq_valid{}, w_transfer{};
  ValueId w_accept{}, w_wlast_mismatch{}, w_last_transfer{};
  ValueId w_absorbed{}, w_last_absorb{}, w_wready{};
  std::pair<ValueId, ValueId> w_dest{};

  // Read-side handles handed from read_issue to read_response.
  AddressFace r_ar{};
  ValueId r_rready{}, r_rrsp_valid{};
  std::map<std::string, ValueId> r_rrsp_in;
  ValueId r_rr_state{}, r_remaining{};
  ValueId r_rsp_full{}, r_rsp_data{}, r_rsp_result{}, r_rsp_error{}, r_rsp_last{};
  // The staged beat's exclusive_okay (EXOKAY), when the profile carries it.
  std::optional<ValueId> r_rsp_exclusive;
  // The response user field of the staged R beat and of the B (with response_user_bits).
  std::optional<ValueId> r_rsp_user, w_buser_reg;
  // BUSER or RUSER: the low bits of the response user field, zero above and without it.
  void user_output(const std::string &signal, std::optional<ValueId> user, ValueId local) {
    if (p.response_user_port_bits)
      axi_output(signal, l.b.select(local, l.b.constant(p.response_user_port_bits, 0),
                                    user ? l.fit(*user, p.response_user_port_bits)
                                         : l.b.constant(p.response_user_port_bits, 0)));
  }
  ValueId r_idle{}, r_emitting{}, r_stream{}, r_errstream{};
  Tracker r_track{};
  ValueId r_arready{}, r_ar_fire{}, r_start{}, r_fresh{}, r_taken{};
  CapturedDescriptor r_cap{};
  ValueId r_issue_tag{}, r_sent{}, r_emit_fire{};
  std::pair<ValueId, ValueId> r_dest{};
  ValueId r_issuing{};
  bool reuse_read_count() const {
    return !multi && !p.lite && !p.apb && !p.ahb && std::countr_zero(be_bits) <= int(p.len_bits);
  }
  ValueId initial_read_count() {
    if (!reuse_read_count())
      return r_ar.len;
    // Local errors need their beat count. Network responses carry their own last marker.
    const auto prefix =
        l.pick(l.eq(r_ar.burst, 1), l.b.slice(r_ar.addr, 0, std::countr_zero(be_bits)),
               l.num(std::countr_zero(be_bits), 0));
    return l.pick(r_sent, l.fit(prefix, p.len_bits), r_ar.len);
  }
  void update_read_count(ValueId step) {
    if (!p.apb)
      l.b.update(r_remaining,
                 l.pick(r_start, initial_read_count(),
                        l.pick(step, l.minus(r_remaining, l.num(p.len_bits, 1)), r_remaining)));
  }

  // ~(2^size - 1) over the transport address: aligns down, and as a subtrahend adds 2^size.
  ValueId size_nmask(ValueId size) {
    auto masks = size_steps(p.data_bits);
    std::ranges::transform(masks, masks.begin(), [&](std::uint64_t step) {
      return (std::uint64_t{1} << transport_address_bits) - step;
    });
    return select_size(l, size, masks, transport_address_bits);
  }
  ValueId next_write_address(ValueId address, ValueId size) {
    const auto last = static_cast<unsigned>(std::countr_zero(std::max(8U, p.data_bits / 8)));
    std::vector<ValueId> selected(last + 1);
    for (unsigned step = 0; step <= last; ++step)
      selected[step] = step == last ? l.inv(l.below(size, l.num(3, step))) : l.eq(size, step);
    ValueId next{};
    // Adding a single power of two flips a bit exactly when every bit between
    // the selected step and that bit is set. Keep the carry terms parallel.
    for (unsigned index = 0; index < transport_address_bits; ++index) {
      std::vector<ValueId> toggles;
      for (unsigned step = 0; step <= std::min(index, last); ++step)
        toggles.push_back(step == index
                              ? selected[step]
                              : l.both(selected[step], l.eq(l.b.slice(address, step, index - step),
                                                            (1ULL << (index - step)) - 1)));
      const auto bit = l.inv(l.same(l.b.slice(address, index, 1), l.any(toggles)));
      next = index == 0 ? bit : l.b.concat(bit, next);
    }
    return next;
  }
  // The response's exclusive_okay, when the profile carries it.
  std::optional<ValueId> exclusive_okay(const std::map<std::string, ValueId> &f) {
    return p.exclusive ? std::optional{f.at("exclusive_okay")} : std::nullopt;
  }
  // The own slot bits of a response's tag, and whether that slot is taken.
  ValueId slot_of(const std::map<std::string, ValueId> &in, const Tracker &t) {
    return p.tag_bits > t.tag_bits ? l.b.slice(in.at("tag"), 0, t.tag_bits) : in.at("tag");
  }
  // The AXI ID a response packet carries back.
  ValueId response_id(const std::map<std::string, ValueId> &in) {
    return l.width(in.at("id")) > p.id_bits ? l.b.slice(in.at("id"), 0, p.id_bits) : in.at("id");
  }
  ValueId owns(const std::map<std::string, ValueId> &in, const Tracker &t) {
    return slot_field(l, t, "", slot_of(in, t));
  }
  ValueId axi_input(const std::string &signal, unsigned width) {
    return p.apb || p.ahb ? face_in.at(signal)
                          : l.b.input("nl_axi_" + signal, width, "axi", signal);
  }
  void axi_output(const std::string &signal, ValueId value) {
    if (p.apb || p.ahb)
      face_out[signal] = value;
    else
      l.b.output("nl_axi_" + signal, value, "axi", signal);
  }
  // One address channel's face inputs ("aw" or "ar"), in port order; Lite ties the rest constant.
  AddressFace address_face(const std::string &ch) {
    AddressFace f{};
    f.id =
        p.id_bits ? (p.lite ? l.b.constant(p.id_bits, 0) : axi_input(ch + "id", p.id_bits)) : one;
    f.addr = axi_input(ch + "addr", p.address_bits);
    f.len = p.lite ? l.b.constant(p.len_bits, 0) : axi_input(ch + "len", p.len_bits);
    f.size = p.lite ? l.b.constant(3, lite_size) : axi_input(ch + "size", 3);
    f.burst = p.lite ? l.b.constant(2, 1) : axi_input(ch + "burst", 2);
    // AXI3 AxLOCK: 0b01 exclusive; a locked (0b10) or reserved (0b11) burst takes the reserved
    // AxBURST, which admission refuses.
    const auto lock = p.lite ? l.b.constant(1, 0) : axi_input(ch + "lock", p.axi3 ? 2 : 1);
    f.lock = p.axi3 ? l.b.slice(lock, 0, 1) : lock;
    // An exclusive WRAP goes out as INCR from its start address (a legal one starts at its
    // container base), whatever the target.
    if (!p.lite)
      f.burst = l.b.select(l.both(f.lock, l.eq(f.burst, 2)), l.b.constant(2, 1), f.burst);
    if (p.axi3)
      f.burst = l.b.select(l.b.slice(lock, 1, 1), l.b.constant(2, 3), f.burst);
    f.cache = p.lite ? l.b.constant(4, 0) : axi_input(ch + "cache", 4);
    f.prot = axi_input(ch + "prot", 3);
    // The AxQOS map's level of the burst (AXI4 A8.1 AxQOS, after AxPROT in port order).
    if (p.level_bits) {
      f.level = l.b.constant(p.level_bits, 0);
      if (!p.axqos_map.empty()) {
        const auto qos = axi_input(ch + "qos", std::countr_zero(p.axqos_map.size()));
        std::vector<ValueId> arms(p.axqos_map.size());
        std::ranges::transform(p.axqos_map, arms.begin(),
                               [&](unsigned v) { return l.b.constant(p.level_bits, v); });
        f.level = l.by_index(qos, arms);
      }
    }
    // Each network user flag from its driver: an AxUSER bit, an AxCACHE or AxPROT flag, a constant.
    const auto write_channel = l.b.constant(1, ch == "aw" ? 1 : 0);
    const auto user_port = (p.user_bits || p.security_bits) && p.user_port_bits
                               ? std::optional{axi_input(ch + "user", p.user_port_bits)}
                               : std::nullopt;
    if (p.user_bits)
      f.user = map_attributes(l, p.user_flags, f.cache, f.prot, write_channel, user_port);
    if (p.security_bits)
      f.security = map_attributes(l, p.security_flags, f.cache, f.prot, write_channel, user_port);
    f.valid = axi_input(ch + "valid", 1);
    return f;
  }

  // The mode plane of a direction's burst in service (Axi4InitiatorProfile::mode_windows): the
  // live decode of its global start address, or with hold the plane stored once the burst
  // commits.
  struct Plane {
    bool on = false, hold = false;
    ValueId held{}, stored{}, value{};
  };
  Plane open_plane(const std::string &dir, ValueId address, bool hold) {
    if (p.mode_windows.empty())
      return {};
    if (!address_selectors)
      address_selectors = l.b.input("nl_hint_address_selectors", p.address_selector_bits, "hint",
                                    "address_selectors");
    std::vector<ValueId> hits, planes;
    for (const auto &m : p.mode_windows) {
      const auto read =
          l.b.binary(Op::BitAnd, *address_selectors, l.num(p.address_selector_bits, m.care));
      hits.push_back(l.both(l.within(address, m.window.base, m.window.base + m.window.size),
                            l.b.binary(Op::Equal, read, l.num(p.address_selector_bits, m.value))));
      planes.push_back(l.num(p.plane_bits, m.plane));
    }
    const auto live = l.first_hit(hits, planes, l.num(p.plane_bits, 0));
    if (!hold)
      return {true, false, {}, {}, l.bind(dir + ".plane_now", live)};
    Plane out{
        true, true, l.state(dir + ".plane_held", 1), l.state(dir + ".plane", p.plane_bits), {}};
    out.value = l.bind(dir + ".plane_now", l.b.select(out.held, out.stored, live));
    return out;
  }
  // A global address of the burst in its plane.
  ValueId planed(const Plane &plane, ValueId address) {
    if (!plane.on)
      return address;
    const auto high = l.b.concat(plane.value, l.num(p.plane_shift, 0));
    return l.b.binary(Op::BitOr, address, l.fit(high, transport_address_bits));
  }
  // The plane holds from the commit cycle until the burst leaves the issue stage.
  void hold_plane(const Plane &plane, ValueId commit, ValueId release) {
    if (!plane.hold)
      return;
    l.b.update(plane.held, l.both(l.inv(release), l.either(plane.held, commit)));
    l.b.update(plane.stored, plane.value);
  }
  std::optional<ValueId> address_selectors;
  Plane w_plane{}, r_plane{};

  void apb_face();
  void ahb_face();
  void write_issue();
  void write_waiting(ValueId issuing, ValueId admits, ValueId fresh, ValueId wvalid);
  void write_complete();
  void read_issue();
  void stage_beat(ValueId load);
  void read_response();
  void apb_tail();
  void ahb_tail();
};
void InitiatorLowering::apb_face() {
  // The APB face drives the Lite engine's AXI inputs and takes its outputs. A transfer offers AW
  // and W (or AR) while PSEL is high. The idle engine takes them in SETUP and stays busy until the
  // B or R that completes the transfer, so each is taken once.
  if (p.apb) {
    auto apb_input = [&](const std::string &signal, unsigned width) {
      return l.b.input("nl_apb_" + signal, width, "apb", signal);
    };
    const bool apb4 = *p.apb == Protocol::Apb4;
    const auto psel = apb_input("psel", 1);
    const auto penable = apb_input("penable", 1);
    const auto pwrite = apb_input("pwrite", 1);
    const auto paddr = apb_input("paddr", p.address_bits);
    auto pwdata = apb_input("pwdata", p.apb_data_bits);
    auto pstrb =
        apb4 ? apb_input("pstrb", face_bytes) : l.b.constant(face_bytes, (1U << face_bytes) - 1);
    if (p.apb_data_bits < 32) {
      const auto lane_address = p.address_bits < 2 ? l.b.unary(Op::ZeroExtend, paddr, 2) : paddr;
      const auto offset = l.b.slice(lane_address, 0, 2);
      auto data = l.b.constant(32, 0), strobes = l.b.constant(4, 0);
      for (unsigned lane = 0; lane < 4; lane += face_bytes) {
        const auto word_lane = l.b.binary(Op::BitAnd, offset, l.b.constant(2, 4 - face_bytes));
        auto place = [&](ValueId value, unsigned unit) {
          if (lane + face_bytes < 4)
            value = l.b.concat(l.b.constant((4 - lane - face_bytes) * unit, 0), value);
          if (lane)
            value = l.b.concat(value, l.b.constant(lane * unit, 0));
          return value;
        };
        const auto chosen = l.eq(word_lane, lane);
        data = l.b.select(chosen, place(pwdata, 8), data);
        strobes = l.b.select(chosen, place(pstrb, 1), strobes);
      }
      pwdata = data;
      pstrb = strobes;
    }
    // An APB3 manager states no protection: its transfers are unprivileged, Non-secure data
    // accesses, the least privileged attributes.
    const auto pprot = apb4 ? apb_input("pprot", 3) : l.b.constant(3, 2);
    apb_access = l.both(psel, penable);
    const auto write = l.both(psel, pwrite), read = l.both(psel, l.inv(pwrite));
    face_in = {{"awaddr", paddr}, {"awprot", pprot}, {"awvalid", write},     {"wdata", pwdata},
               {"wstrb", pstrb},  {"wvalid", write}, {"bready", apb_access}, {"araddr", paddr},
               {"arprot", pprot}, {"arvalid", read}, {"rready", apb_access}};
  }
}

void InitiatorLowering::ahb_face() {
  // The AHB face drives a full AXI4 engine of its width. A data phase opens when HREADY samples
  // NONSEQ or SEQ with HSEL; its beat issues the AW or AR of a new piece (a NONSEQ, or a SEQ
  // behind a finished piece) and then moves one W or R beat.
  if (!p.ahb)
    return;
  auto &a = ahb;
  const auto input = [&](const std::string &signal, unsigned width) {
    return l.b.input("nl_ahb_" + signal, width, "ahb", signal);
  };
  const auto word = static_cast<unsigned>(std::countr_zero(be_bits));
  a.hsel = input("hsel", 1);
  const auto haddr = input("haddr", p.address_bits);
  a.htrans = input("htrans", 2);
  const auto hwrite = input("hwrite", 1);
  const auto hsize = input("hsize", 3);
  const auto hburst = input("hburst", 3);
  const auto hprot = input("hprot", 4);
  const auto hmastlock = input("hmastlock", 1);
  const auto hwdata = input("hwdata", p.data_bits);
  a.hready = input("hready", 1);
  const auto take = l.both(a.hsel, l.b.slice(a.htrans, 1, 1));
  const auto sample = l.both(a.hready, take);
  a.phase = l.state("ahb.phase", 1);
  l.b.update(a.phase, l.b.select(a.hready, take, a.phase));
  const auto latch = [&](const std::string &name, ValueId face) {
    const auto q = l.state("ahb." + name, l.width(face));
    l.b.update(q, l.b.select(sample, face, q));
    return q;
  };
  const auto addr = latch("addr", haddr);
  a.write = latch("write", hwrite);
  const auto size = latch("size", hsize);
  const auto kind = latch("burst", hburst);
  const auto prot = latch("prot", hprot);
  const auto lock = latch("lock", hmastlock);
  const auto first = latch("first", l.eq(a.htrans, 2));
  a.sent = l.state("ahb.sent", 1);
  a.done = l.state("ahb.done", 1);
  a.err = l.state("ahb.err", 1);
  a.end = l.state("ahb.end", 1);
  a.left = l.state("ahb.left", 5);
  a.flight = l.state("ahb.flight", 1);
  a.open_write = l.state("ahb.open_write", 1);

  // A locked transfer, a beat wider than the bus and a narrow beat of a burst have no AXI form.
  const auto legal =
      l.bind("ahb.legal", l.both(l.inv(lock), l.both(l.inv(l.below(l.num(3, word), size)),
                                                     l.either(l.eq(size, word), l.eq(kind, 0)))));
  a.bad = l.both(a.phase, l.inv(legal));
  a.serve = l.both(l.both(a.phase, legal), l.inv(l.either(a.end, a.err)));
  const auto fresh =
      l.bind("ahb.fresh", l.both(l.both(a.serve, l.inv(a.sent)), l.either(first, l.eq(a.left, 0))));
  a.posted = l.both(a.write, l.b.slice(prot, 2, 1));
  // Undefined INCR admits only the transfer already in its data phase. This avoids
  // speculative target reads when the manager ends the burst. Fixed lengths retain their size.
  const auto undefined = l.num(4, 0);
  const auto beats = l.b.slice(kind, 1, 2);
  a.len = l.b.select(l.eq(kind, 0), l.num(4, 0),
                     l.b.select(l.eq(kind, 1), undefined,
                                l.b.select(l.eq(beats, 1), l.num(4, 3),
                                           l.b.select(l.eq(beats, 2), l.num(4, 7), l.num(4, 15)))));
  const auto wrap = l.both(l.inv(l.b.slice(kind, 0, 1)), l.inv(l.eq(beats, 0)));
  a.beat_last = l.b.select(fresh, l.eq(a.len, 0), l.eq(a.left, 1));
  a.awvalid = l.both(fresh, a.write);
  a.arvalid = l.both(fresh, l.both(l.inv(a.write), l.inv(a.flight)));
  const auto pad = l.both(a.end, l.both(a.open_write, l.inv(l.eq(a.left, 0))));
  a.wvalid = l.either(l.both(a.serve, l.both(a.write, l.inv(a.done))), pad);
  a.rready = l.either(l.both(a.serve, l.inv(a.write)), l.both(a.end, l.inv(a.open_write)));
  // Byte lanes of the beat: its 2^HSIZE bytes at the address, aligned to HSIZE.
  auto lanes = l.per_size(size, word, [&](unsigned n) {
    const auto ones =
        l.b.unary(Op::BitNot, l.num(1U << std::min(n, word), 0), 1U << std::min(n, word));
    return n >= word ? ones : l.b.concat(l.num(be_bits - (1U << n), 0), ones);
  });
  const auto aligned =
      l.b.binary(Op::BitAnd, l.b.slice(addr, 0, word), l.per_size(size, word, [&](unsigned n) {
        return l.num(word, ((1U << word) - 1) & ~((1U << n) - 1));
      }));
  for (unsigned k = 0; k < word; ++k)
    lanes =
        l.b.select(l.b.slice(aligned, k, 1),
                   l.b.concat(l.b.slice(lanes, 0, be_bits - (1U << k)), l.num(1U << k, 0)), lanes);
  const auto axprot =
      l.b.concat(l.b.concat(l.inv(l.b.slice(prot, 0, 1)), one), l.b.slice(prot, 1, 1));
  for (const auto *ch : {"aw", "ar"}) {
    const std::string c = ch;
    if (p.id_bits)
      face_in[c + "id"] = l.num(p.id_bits, 0);
    face_in[c + "addr"] = addr;
    face_in[c + "len"] = l.fit(a.len, p.len_bits);
    face_in[c + "size"] = size;
    face_in[c + "burst"] = l.b.select(wrap, l.num(2, 2), l.num(2, 1));
    face_in[c + "lock"] = zero;
    face_in[c + "cache"] =
        l.b.concat(ch == std::string("aw") ? l.b.concat(zero, l.b.slice(prot, 3, 1))
                                           : l.b.concat(l.b.slice(prot, 3, 1), zero),
                   l.b.slice(prot, 2, 2));
    face_in[c + "prot"] = axprot;
  }
  face_in["awvalid"] = a.awvalid;
  face_in["arvalid"] = a.arvalid;
  face_in["wdata"] = l.b.select(a.end, l.num(p.data_bits, 0), hwdata);
  face_in["wstrb"] = l.b.select(a.end, l.num(be_bits, 0), lanes);
  face_in["wlast"] = l.b.select(a.end, l.eq(a.left, 1), a.beat_last);
  face_in["wvalid"] = a.wvalid;
  face_in["bready"] = one;
  face_in["rready"] = a.rready;
}

// What a write shows the request merger while its engine cannot issue it (hint write_waiting).
void InitiatorLowering::write_waiting(ValueId issuing, ValueId admits, ValueId fresh,
                                      ValueId wvalid) {
  // A request at the port keeps the source's zones open while it competes: the plain valid, off the
  // decode the waiting hint reads.
  l.b.output("nl_hint_write_port", w_aw.valid, "hint", "write_port");
  if (multi) {
    // A burst bound for the network that waits for its ID shows as waiting to the request merger
    // (its W beat at hand), which then holds the other direction behind it.
    l.b.output(
        "nl_hint_write_waiting",
        l.both(l.reset, l.either(l.both(l.both(issuing, l.inv(admits)),
                                        l.both(l.either(w_admission.admitted, w_admission.missing),
                                               p.lite ? l.either(w_beat_full, wvalid) : wvalid)),
                                 l.both(l.both(fresh, l.both(w_aw.valid, l.inv(w_track.room))),
                                        l.both(l.either(w_admission.admitted, w_admission.missing),
                                               p.lite ? l.either(w_beat_full, wvalid) : wvalid)))),
        "hint", "write_waiting");
    return;
  }
  // Only a burst bound for the network: a local answer would leave the merger waiting for it.
  l.b.output("nl_hint_write_waiting",
             p.mode_windows.empty()
                 ? l.both(l.both(l.reset, l.inv(fresh)),
                          l.both(l.both(w_aw.valid, wvalid),
                                 l.either(w_admission.admitted, w_admission.missing)))
                 : zero,
             "hint", "write_waiting");
}
void InitiatorLowering::write_issue() {
  w_aw = address_face("aw");
  w_wdata = axi_input("wdata", p.data_bits);
  w_wstrb = axi_input("wstrb", be_bits);
  w_wlast = p.lite ? one : axi_input("wlast", 1);
  if (p.axi3 && p.id_bits)
    w_wid = axi_input("wid", p.id_bits);
  auto wvalid = axi_input("wvalid", 1);
  w_bready = axi_input("bready", 1);
  auto wrq_ready = l.b.input("nl_wreq_ready", 1, "wreq", "ready");
  w_wrsp_valid = l.b.input("nl_wrsp_valid", 1, "wrsp", "valid");
  for (const auto &[field, width] : burst_response_fields(p.data_bits, burst, p.source_bits))
    w_wrsp_in[field] = l.b.input("nl_wrsp_" + field, width, "wrsp", field);

  w_wr_state = l.state("wr.state", 2);
  // A Lite (or APB) burst has one beat: its count left is 0 or, past that beat, all ones.
  // An APB burst counts nothing: it leaves whole, its beat stays on the face and carries no WLAST.
  w_remaining = p.apb    ? l.b.constant(p.len_bits, 0)
                : p.lite ? l.b.replicated(l.state("wr.remaining", 1), p.len_bits)
                         : l.state("wr.remaining", p.len_bits);
  // Beat containers are aligned to the size: a Lite (or APB) face of one size keeps the bits below
  // it zero, so they are not stored (plan_obligations.cpp counts the same).
  if (!p.apb)
    w_beat_addr =
        p.lite && lite_size
            ? l.b.zero_low(l.state("wr.beat_addr", transport_address_bits - lite_size), lite_size)
            : l.state("wr.beat_addr", transport_address_bits);
  w_poisoned = p.apb ? zero : l.state("wr.poisoned", 1);
  // A full AXI4 engine passes W beats through; a Lite engine holds one beat until its AW.
  if (p.lite) {
    w_beat_full = l.state("wr.beat_full", 1);
    if (!p.apb) {
      w_beat_data = l.state("wr.beat_data", p.data_bits);
      w_beat_be = l.state("wr.beat_be", be_bits);
    }
  } else
    w_beat_full = zero;
  w_bresp_reg = l.state("wr.bresp", 2);
  if (p.response_user_bits)
    w_buser_reg = l.state("wr.b_user", p.response_user_bits);

  w_idle = l.eq(w_wr_state, 0);
  const auto beats = l.eq(w_wr_state, 1);
  w_waiting = l.eq(w_wr_state, 2);
  w_respond = l.eq(w_wr_state, 3);

  // With several bursts in flight the request engine issues (states 0 and 1) and holds a local
  // completion until the B staging takes it (state 2). Slot: ID and the WLAST mismatch poison.
  std::vector<std::pair<std::string, unsigned>> slot_fields{{"poison", 1}};
  w_track = multi ? open_tracker(l, "wr", p.max_outstanding, id_entries, p.id_bits, dest_bits,
                                 slot_fields)
                  : Tracker{};
  // Full bursts capture AW before W; Lite uses the existing beat slot independently.
  w_awready = held    ? ValueId{}
              : multi ? l.both(l.reset, l.both(w_idle, w_track.room))
                      : l.both(l.reset, w_idle);
  if (!held)
    w_aw_fire = l.bind("wr.aw_fire", l.both(w_aw.valid, w_awready));
  // An engine with nothing in service (fresh) passes a write straight through: the
  // AW of its handshake cycle enters service at once and the request reads the AW and W faces in
  // that cycle (it depends on the source side only, never on a ready); a beat not taken waits on
  // the W face (a Lite engine's in its beat buffer) and the descriptor in its registers.
  // A held engine with one burst is fresh also in the cycle its B leaves; with several a burst
  // starts only while a slot is free.
  const auto fresh = w_idle;
  w_start =
      held ? l.bind("wr.start",
                    l.both(l.reset, l.both(l.both(fresh, w_aw.valid), multi ? w_track.room : one)))
           : w_aw_fire;
  w_straight = l.bind("wr.straight", l.both(fresh, w_start));
  // A held engine with one burst reads its descriptor from the face alone: the AW stays there until
  // the burst leaves the issue stage, and nothing issues after that until the next AW is wanted. A
  // stored mode plane samples the address every cycle, so with mode windows the descriptor follows
  // the engine going idle. With several bursts the face holds the burst in issue only while the
  // engine is fresh.
  const auto w_pass = held && !multi && p.mode_windows.empty() ? std::optional<ValueId>{}
                                                               : std::optional<ValueId>{fresh};
  w_cap = capture_descriptor(l, p, w_track, "wr", w_start, w_pass, multi, w_aw);
  w_plane = open_plane("wr", w_cap.d.addr, true);
  w_cap.d.addr = planed(w_plane, w_cap.d.addr);
  const auto &d = w_cap.d;
  w_admission = check_admission(l, p, d, "wr");
  // A burst enters service from the AW face.
  const auto load_addr = w_cap.addr32;
  const auto load_size = w_aw.size;
  w_load_len = w_aw.len;
  // The slot of the burst in issue: one taken by an AW passing straight through, else its own.
  w_issue_tag = multi ? l.b.select(fresh, w_track.alloc, w_cap.tag) : w_cap.tag;

  // Beat container bases: aligned start, then +2^size per beat. WRAP folds the advance
  // back to the region base when the next address would leave the region.
  auto align_nmask = size_nmask(load_size);
  w_aligned_input = l.b.binary(Op::BitAnd, load_addr, align_nmask);
  w_beat_address = p.apb ? w_aligned_input : l.b.select(fresh, w_aligned_input, w_beat_addr);
  auto incr_next = l.bind("wr.addr_incr", next_write_address(w_beat_address, d.size));
  // WRAP region = (len+1) * 2^size for len in {1,3,7,15}: 16 constants by length class and size.
  std::array<ValueId, 4> region_masks;
  for (unsigned cls = 0; cls < 4; ++cls) {
    const unsigned log2_beats = cls + 1;
    std::array<std::uint64_t, 8> masks{};
    for (unsigned s = 0; s < 8; ++s)
      masks[s] = (std::uint64_t{1} << (log2_beats + std::min(s, largest_size))) - 1;
    region_masks[cls] = select_size(l, d.size, masks, transport_address_bits);
  }
  auto wrap_mask = region_masks[3];
  const std::array<std::uint64_t, 4> wrap_lens{1, 3, 7, 15};
  for (unsigned cls = 3; cls-- > 0;)
    wrap_mask = l.b.select(l.eq(d.len, wrap_lens[cls]), region_masks[cls], wrap_mask);
  wrap_mask = l.bind("wr.wrap_mask", wrap_mask);
  auto wrap_nmask = l.b.unary(Op::BitNot, wrap_mask, transport_address_bits);
  auto wrap_base = l.bind("wr.wrap_base", l.b.binary(Op::BitAnd, d.addr, wrap_nmask));
  auto wrap_turn = l.bind("wr.wrap_turn", l.eq(l.b.binary(Op::BitAnd, incr_next, wrap_mask), 0));
  auto wrap_next = l.b.select(wrap_turn, wrap_base, incr_next);
  auto addr_next_mode =
      l.bind("wr.addr_next_mode", l.b.select(l.eq(d.burst, 2), wrap_next, incr_next));
  w_addr_next =
      l.bind("wr.addr_next", l.b.select(l.eq(d.burst, 0), w_beat_address, addr_next_mode));

  // The packet fields derive from the descriptor counters, never WLAST; fresh means first beat.
  w_left = l.bind("wr.left", l.b.select(fresh, w_load_len, w_remaining));
  w_frag_last = l.bind("wr.frag_last", l.eq(w_left, 0));
  w_frag_first = l.bind("wr.frag_first", l.either(fresh, l.same(w_remaining, d.len)));
  // A tracked burst issues only when its ID is idle or bound to its own destination; until then
  // neither W nor the request packet moves.
  ValueId issuing = l.either(beats, w_straight);
  if (multi) {
    w_dest = destination(l, p, d, w_admission, transport_address_bits, "wr");
    w_dest = bound_destination(l, p, w_dest);
    const auto admits = tracker_admits(l, w_track, "wr", d.id, w_dest.first);
    write_waiting(issuing, admits, fresh, wvalid);
    issuing = l.bind("wr.issue", l.both(issuing, admits));
  } else if (held) {
    // A burst at the port while the one in service is not done competes for the request merger.
    write_waiting(issuing, one, fresh, wvalid);
  }
  // A W beat leaves in the cycle it is taken (a Lite engine's from its beat buffer). A burst sent
  // to the network (sent) carries all its beats, or, as a decode miss, only its last one as an
  // error request; with address hints the packet splitter behind cuts a missed burst, so it
  // carries all its beats too.
  const auto offered = p.lite ? l.bind("wr.offered", l.either(w_beat_full, wvalid)) : wvalid;
  w_sent = w_admission.sent;
  w_carry = p.address_hints
                ? w_sent
                : l.either(w_admission.admitted, l.both(w_admission.missing, w_frag_last));
  w_wrq_valid = l.both(l.reset, l.both(issuing, l.both(w_carry, offered)));
  w_transfer = l.bind("wr.transfer", l.both(w_wrq_valid, wrq_ready));
  // A beat is taken only while its burst issues (a tracked burst while it may, issuing is beats
  // without tracking): as it leaves to the network, or at once when the burst completes locally.
  // The next burst's first W beat waits until its AW is in service.
  w_wready =
      l.both(l.reset, l.bind("wr.refill", l.both(l.b.select(w_carry, wrq_ready, one), issuing)));
  if (p.lite)
    w_wready = l.both(l.reset, l.both(l.either(w_idle, beats), l.inv(w_beat_full)));
  w_accept = l.bind("wr.accept", l.both(wvalid, w_wready));
  // The accepted beat's WLAST must mark the final beat of the burst (a Lite beat taken while its
  // buffered predecessor drains sits one position ahead).
  auto wlast_expected =
      l.bind("wr.wlast_expected", l.b.select(w_beat_full, l.eq(w_left, 1), l.eq(w_left, 0)));
  auto mismatch =
      l.either(l.both(w_wlast, l.inv(wlast_expected)), l.both(l.inv(w_wlast), wlast_expected));
  // An AXI3 beat must carry its burst's AWID: without write interleaving the beat belongs to the
  // burst in service.
  if (p.axi3 && p.id_bits)
    mismatch = l.either(mismatch, l.inv(l.b.binary(Op::Equal, w_wid, d.id)));
  w_wlast_mismatch = l.bind("wr.wlast_mismatch", mismatch);
  w_last_transfer = l.both(w_transfer, w_frag_last);
  // A local burst's beats: accepted from W.
  w_absorbed = w_accept;
  // A tracked Lite write completes locally only once its issue gate opens, after every earlier
  // write (issuing is beats without tracking).
  w_last_absorb = p.lite ? l.both(issuing, l.both(l.inv(w_sent), w_beat_full))
                         : l.both(l.inv(w_sent), l.both(w_absorbed, l.eq(w_left, 0)));
  if (p.address_hints) {
    l.b.output("nl_hint_write_start", d.addr, "hint", "write_start");
    l.b.output("nl_hint_write_address", planed(w_plane, w_beat_address), "hint", "write_address");
    l.b.output("nl_hint_write_length", d.len, "hint", "write_length");
  }
  // A write commits its plane when the network takes its first fragment or, completing locally,
  // its first beat is taken (a Lite beat may wait in its buffer before its AW).
  hold_plane(w_plane, p.lite ? w_transfer : w_accept, l.either(w_last_transfer, w_last_absorb));
  if (held) {
    // The AW leaves the port pipe as the burst leaves the issue stage.
    w_awready = l.both(l.reset, l.either(w_last_transfer, w_last_absorb));
    w_aw_fire = l.bind("wr.aw_fire", l.both(w_aw.valid, w_awready));
  }
}

void InitiatorLowering::write_complete() {
  const auto &d = w_cap.d;

  // Write response: one aggregated single-fragment packet after the last beat; a network-side
  // breach of that shape folds into SLVERR. A tracked engine stages B: a response packet whenever
  // the staging has room (with its burst's ID and poison), else the local completion held in the
  // issue stage. The staging never waits for anything but BREADY.
  // A held engine with one burst has its own B staging too: the burst is done once its answer is
  // staged, and the next burst issues while that B waits for BREADY.
  const bool staged = multi || held;
  ValueId b_full{}, b_id{};
  if (staged) {
    b_full = l.state("wr.b_full", 1);
    if (p.id_bits)
      b_id = l.state("wr.rsp_id", p.id_bits);
  }
  // The B register takes the next B while it is empty or its B leaves in this cycle (a forward
  // register: the network's B ready follows BREADY).
  const auto b_room = staged ? l.either(l.inv(b_full), w_bready) : ValueId{};
  // A response of no burst in flight (an idle engine; a free slot) is taken and dropped.
  auto wrsp_ready = multi  ? l.both(l.reset, b_room)
                    : held ? l.both(l.reset, l.either(w_idle, l.both(w_waiting, b_room)))
                           : l.both(l.reset, l.either(w_waiting, w_idle));
  auto wrsp_fire = l.bind("wr.wrsp_fire", l.both(l.both(w_wrsp_valid, wrsp_ready),
                                                 multi ? owns(w_wrsp_in, w_track) : w_waiting));
  auto rsp_bad = l.inv(l.both(w_wrsp_in.at("first"), w_wrsp_in.at("last")));
  auto wrsp_mapped = map_result(l, w_wrsp_in.at("result"), l.either(w_wrsp_in.at("error"), rsp_bad),
                                p.uniform_slverr, exclusive_okay(w_wrsp_in));
  // The WLAST poison of a tracked burst folds into its staged response.
  if (multi)
    wrsp_mapped = l.b.select(slot_field(l, w_track, "poison", slot_of(w_wrsp_in, w_track)),
                             l.b.constant(2, 2), wrsp_mapped);
  else if (held)
    wrsp_mapped = l.b.select(w_poisoned, l.b.constant(2, 2), wrsp_mapped);

  // A local completion waits in state 2 until the staging has room and no response packet
  // competes; responses in flight are bounded by the slot table, and nothing else issues
  // meanwhile, so it is never starved.
  const auto local_fill =
      multi  ? l.bind("wr.local_fill",
                      l.both(l.reset, l.both(w_waiting, l.both(b_room, l.inv(w_wrsp_valid)))))
      : held ? l.bind("wr.local_fill", l.both(l.reset, l.both(w_respond, b_room)))
             : ValueId{};
  const auto load = w_start;
  // A burst passing straight through may also end in its handshake cycle.
  const auto done_now = l.both(w_straight, l.either(w_last_transfer, w_last_absorb));
  if (multi)
    l.b.update(
        w_wr_state,
        l.b.select(l.both(load, l.inv(done_now)), l.b.constant(2, 1),
                   l.b.select(w_last_transfer, l.b.constant(2, 0),
                              l.b.select(w_last_absorb, l.b.constant(2, 2),
                                         l.b.select(local_fill, l.b.constant(2, 0), w_wr_state)))));
  else if (held)
    l.b.update(w_wr_state,
               l.b.select(l.both(w_start, l.inv(done_now)), l.b.constant(2, 1),
                          l.b.select(w_last_transfer, l.b.constant(2, 2),
                                     l.b.select(w_last_absorb, l.b.constant(2, 3),
                                                l.b.select(l.either(wrsp_fire, local_fill),
                                                           l.b.constant(2, 0), w_wr_state)))));
  else
    l.b.update(
        w_wr_state,
        l.b.select(l.both(w_start, l.inv(done_now)), l.b.constant(2, 1),
                   l.b.select(w_last_transfer, l.b.constant(2, 2),
                              l.b.select(w_last_absorb, l.b.constant(2, 3),
                                         l.b.select(wrsp_fire, l.b.constant(2, 3),
                                                    l.b.select(l.both(w_respond, w_bready),
                                                               l.b.constant(2, 0), w_wr_state))))));
  auto beat_step = l.bind("wr.beat_step", l.b.constant(p.len_bits, 1));
  auto decrement = l.b.select(w_carry, w_transfer, w_absorbed);
  // A burst passing straight through counts its first beat from the AW face.
  const auto fresh_load = l.both(load, l.inv(w_straight));
  if (!p.apb) {
    l.b.update(
        w_remaining,
        l.b.select(fresh_load, w_load_len,
                   l.b.select(decrement, l.b.binary(Op::Subtract, w_left, beat_step), w_left)));
    l.b.update(w_beat_addr, l.b.select(fresh_load, w_aligned_input,
                                       l.b.select(w_transfer, w_addr_next, w_beat_address)));
  }
  const auto taken = l.both(w_accept, w_carry);
  if (!p.apb)
    l.b.update(
        w_poisoned,
        p.lite ? zero
               : l.b.select(w_start, l.both(taken, w_wlast_mismatch),
                            l.b.select(taken, l.either(w_poisoned, w_wlast_mismatch), w_poisoned)));
  if (p.lite) {
    // An unstrobed byte is captured as zero, so it reaches every target as zero.
    l.b.update(w_beat_full, l.b.select(l.either(w_last_transfer, w_last_absorb), zero,
                                       l.b.select(w_accept, one, w_beat_full)));
    if (!p.apb) {
      l.b.update(w_beat_data,
                 l.b.select(w_accept, strobed_data(l.b, w_wdata, w_wstrb, be_bits), w_beat_data));
      l.b.update(w_beat_be, l.b.select(w_accept, w_wstrb, w_beat_be));
    }
  }
  // The burst's WLAST poison when it leaves the issue stage, its last beat included when that
  // beat passes straight from the W face (zero for a local completion).
  const auto push_poison =
      l.both(w_sent, l.either(l.both(l.inv(w_straight), w_poisoned),
                              l.both(l.both(w_accept, w_carry), w_wlast_mismatch)));
  // A response packet takes the B staging with its mapped result and its burst's ID; the local
  // completion with its own code and ID. Either releases its slot and its ID count.
  const auto b_pop =
      staged ? l.bind("wr.pop", l.both(l.both(l.reset, b_full), w_bready)) : ValueId{};
  if (staged) {
    l.b.update(w_bresp_reg, l.b.select(wrsp_fire, wrsp_mapped,
                                       l.b.select(local_fill, l.b.constant(2, 2), w_bresp_reg)));
    if (w_buser_reg)
      l.b.update(
          *w_buser_reg,
          l.b.select(wrsp_fire, w_wrsp_in.at("user"),
                     l.b.select(local_fill, l.b.constant(p.response_user_bits, 0), *w_buser_reg)));
    if (p.id_bits)
      l.b.update(b_id, l.b.select(wrsp_fire, multi ? response_id(w_wrsp_in) : w_cap.id_reg,
                                  l.b.select(local_fill, w_cap.id_reg, b_id)));
    l.b.update(b_full,
               l.b.select(l.either(wrsp_fire, local_fill), one, l.b.select(b_pop, zero, b_full)));
  } else {
    l.b.update(w_bresp_reg, l.b.select(wrsp_fire, wrsp_mapped,
                                       l.b.select(w_last_absorb, l.b.constant(2, 2), w_bresp_reg)));
    if (w_buser_reg)
      l.b.update(*w_buser_reg,
                 l.b.select(wrsp_fire, w_wrsp_in.at("user"),
                            l.b.select(w_last_absorb, l.b.constant(p.response_user_bits, 0),
                                       *w_buser_reg)));
  }
  if (multi) {
    // The poison is known when the burst leaves the issue stage
    // (zero for a local completion).
    const std::map<std::string, ValueId> captured{{"poison", zero}};
    close_tracker(
        l, w_track, w_start, captured, l.bind("wr.push", l.either(w_last_transfer, w_last_absorb)),
        w_issue_tag, {{"poison", push_poison}}, d.id, w_dest.second, p.crossing,
        {{l.either(wrsp_fire, local_fill),
          l.b.select(wrsp_fire, slot_of(w_wrsp_in, w_track), w_cap.tag),
          p.id_bits ? l.b.select(wrsp_fire, response_id(w_wrsp_in), w_cap.id_reg) : ValueId{}}});
  }

  // Fragment fields in contract order (message.hpp): the descriptor is replicated on every
  // fragment; first/last derive from the beat position.
  std::map<std::string, ValueId> wrq;
  // A decode miss sends its last beat alone (with address hints every beat), no strobes, as error.
  const auto beat_address = planed(w_plane, w_beat_address);
  wrq["address"] = missed(l, p, w_admission.unmapped, beat_address, beat_address);
  request_descriptor_fields(
      l, p, wrq, d, w_admission, one,
      p.address_hints ? d.len
                      : l.b.select(w_admission.unmapped, l.b.constant(p.len_bits, 0), d.len),
      multi ? std::optional{w_issue_tag} : std::nullopt);
  wrq["wrap_end"] = wrap_end_of(l, wrq.at("write"), wrq.at("burst"), wrq.at("length"),
                                wrq.at("size"), wrq.at("address"));
  wrq["first"] = p.address_hints ? w_frag_first : l.either(w_frag_first, w_admission.unmapped);
  wrq["last"] = w_frag_last;
  // The W face's beat (unstrobed bytes zero), a Lite engine's from its buffer once it holds it.
  const auto face_data = strobed_data(l.b, w_wdata, w_wstrb, be_bits);
  const auto buffered = p.lite && !p.apb;
  wrq["data"] = buffered ? l.b.select(w_beat_full, w_beat_data, face_data) : face_data;
  wrq["be"] = l.b.select(w_admission.unmapped, l.b.constant(be_bits, 0),
                         buffered ? l.b.select(w_beat_full, w_beat_be, w_wstrb) : w_wstrb);
  l.b.output("nl_wreq_valid", w_wrq_valid, "wreq", "valid");
  for (const auto &[field, width] : burst_request_fields(p.data_bits, burst, p.source_bits))
    l.b.output("nl_wreq_" + field, wrq.at(field), "wreq", field);
  l.b.output("nl_wrsp_ready", wrsp_ready, "wrsp", "ready");

  if (p.source_lock)
    l.b.output("nl_hint_write_lock", d.lock, "hint", "write_lock");
  axi_output("awready", w_awready);
  axi_output("wready", w_wready);
  if (staged) {
    axi_output("bvalid", l.both(l.reset, b_full));
    axi_output("bresp", w_bresp_reg);
    if (p.id_bits && !p.lite)
      axi_output("bid", b_id);
    user_output("buser", w_buser_reg, zero);
  } else {
    axi_output("bvalid", l.both(l.reset, w_respond));
    axi_output("bresp", l.b.select(w_poisoned, l.b.constant(2, 2), w_bresp_reg));
    if (p.id_bits && !p.lite)
      axi_output("bid", d.id);
    user_output("buser", w_buser_reg, zero);
  }
}
void InitiatorLowering::read_issue() {
  r_ar = address_face("ar");
  r_rready = axi_input("rready", 1);
  auto rrq_ready = l.b.input("nl_rreq_ready", 1, "rreq", "ready");
  r_rrsp_valid = l.b.input("nl_rrsp_valid", 1, "rrsp", "valid");
  for (const auto &[field, width] : burst_response_fields(p.data_bits, burst, p.source_bits))
    r_rrsp_in[field] = l.b.input("nl_rrsp_" + field, width, "rrsp", field);

  r_rr_state = l.state("rr.state", 2);
  // A held engine's request is taken when its final packet enters the network (the composition's
  // request merger takes it); only then does its AR leave the port pipe.
  if (held)
    r_taken = l.b.input("nl_hint_read_taken", 1, "hint", "read_taken");
  // The tracked engine counts the face burst's delivered beats instead.
  r_remaining = multi || p.apb ? ValueId{} : l.state("rr.remaining", p.len_bits);
  r_rsp_full = l.state("rr.rsp_full", 1);
  r_rsp_data = l.state("rr.rsp_data", p.data_bits);
  r_rsp_result = l.state("rr.rsp_result", 2);
  r_rsp_error = l.state("rr.rsp_error", 1);
  r_rsp_last = l.state("rr.rsp_last", 1);
  // The staged beat's exclusive_okay (EXOKAY), when the profile carries it.
  if (p.exclusive)
    r_rsp_exclusive = l.state("rr.rsp_exclusive_okay", 1);
  if (p.response_user_bits)
    r_rsp_user = l.state("rr.rsp_user", p.response_user_bits);

  r_idle = l.eq(r_rr_state, 0);
  r_emitting = l.eq(r_rr_state, 1);
  r_stream = l.eq(r_rr_state, 2);
  r_errstream = l.eq(r_rr_state, 3);

  std::vector<std::pair<std::string, unsigned>> slot_fields;
  if (!p.lite && !p.apb && !p.ahb)
    slot_fields.emplace_back("prefix", std::countr_zero(be_bits));
  r_track = multi ? open_tracker(l, "rr", p.max_outstanding, id_entries, p.id_bits, dest_bits,
                                 slot_fields)
                  : Tracker{};
  r_arready = held    ? ValueId{}
              : multi ? l.both(l.reset, l.both(r_idle, r_track.room))
                      : l.both(l.reset, r_idle);
  if (!held)
    r_ar_fire = l.bind("rr.ar_fire", l.both(r_ar.valid, r_arready));
  // A held engine with one burst is done once its last R beat is staged.
  r_fresh = r_idle;
  r_start = held ? l.bind("rr.start", l.both(l.reset, l.both(l.both(r_fresh, r_ar.valid),
                                                             multi ? r_track.room : one)))
                 : r_ar_fire;
  // A held engine with one burst reads its descriptor from the face alone: the AR stays there until
  // the network takes the request or the burst turns local, and nothing issues after that until
  // the next AR is wanted, so the descriptor never waits on the engine state or the beat count.
  const auto r_pass = held && !multi ? std::optional<ValueId>{} : std::optional<ValueId>{r_fresh};
  r_cap = capture_descriptor(l, p, r_track, "rr", r_start, r_pass, multi, r_ar);
  // A read request is one fragment: its plane follows the flags until the network takes it.
  r_plane = open_plane("rr", r_cap.d.addr, false);
  r_cap.d.addr = planed(r_plane, r_cap.d.addr);
  const auto &d = r_cap.d;
  if (p.apb && p.apb_data_bits < 32)
    apb_read_address = d.addr;
  // The slot of the burst being issued: one taken by the handshake that passes straight
  // through, else the burst's own.
  r_issue_tag = multi ? l.b.select(r_idle, r_track.alloc, r_cap.tag) : r_cap.tag;
  auto admission = check_admission(l, p, d, "rr");

  auto align_nmask = size_nmask(d.size);
  auto aligned_base = l.b.binary(Op::BitAnd, d.addr, align_nmask);

  // Single-fragment read request carrying the descriptor.
  std::map<std::string, ValueId> rrq;
  // A decode miss leaves as an error request at the miss window.
  rrq["address"] = missed(l, p, admission.unmapped, aligned_base, aligned_base);
  request_descriptor_fields(l, p, rrq, d, admission, zero, d.len,
                            multi ? std::optional{r_issue_tag} : std::nullopt);
  rrq["first"] = one;
  rrq["last"] = one;
  rrq["data"] = l.b.constant(p.data_bits, 0);
  rrq["be"] = l.b.constant(be_bits, 0);
  // The descriptor registers load on the same edge that leaves IDLE, so the admission decision is
  // taken in EMIT: a rejected burst moves to ERRSTREAM without ever asserting rrq_valid. A
  // tracked burst leaves EMIT only when its ID is idle or bound to its own destination.
  r_issuing = r_emitting;
  // An admitted AR handshake of an idle engine issues at once.
  auto straight = l.both(r_fresh, r_start);
  if (multi) {
    r_dest = destination(l, p, d, admission, transport_address_bits, "rr");
    r_dest = bound_destination(l, p, r_dest);
    const auto admits = tracker_admits(l, r_track, "rr", d.id, r_dest.first);
    l.b.output(
        "nl_hint_read_waiting",
        l.both(l.reset, l.either(l.both(l.both(l.either(r_emitting, straight), l.inv(admits)),
                                        l.either(admission.admitted, admission.missing)),
                                 l.both(l.both(r_fresh, l.both(r_ar.valid, l.inv(r_track.room))),
                                        l.either(admission.admitted, admission.missing)))),
        "hint", "read_waiting");
    l.b.output("nl_hint_read_port", r_ar.valid, "hint", "read_port");
    r_issuing = l.bind("rr.issue", l.both(r_emitting, admits));
    straight = l.both(straight, admits);
  } else if (held) {
    l.b.output("nl_hint_read_port", r_ar.valid, "hint", "read_port");
    l.b.output("nl_hint_read_waiting",
               l.both(l.both(l.reset, l.inv(r_fresh)),
                      l.both(r_ar.valid, l.either(admission.admitted, admission.missing))),
               "hint", "read_waiting");
  }
  r_sent = admission.sent;
  straight = l.bind("rr.straight", l.both(straight, r_sent));
  auto rrq_valid = l.both(l.reset, l.either(straight, l.both(r_issuing, r_sent)));
  r_emit_fire = l.bind("rr.emit_fire", l.both(rrq_valid, rrq_ready));
  if (p.source_lock)
    l.b.output("nl_hint_read_lock", d.lock, "hint", "read_lock");
  if (p.address_hints)
    l.b.output("nl_hint_read_start", d.addr, "hint", "read_start");
  l.b.output("nl_rreq_valid", rrq_valid, "rreq", "valid");
  for (const auto &[field, width] : burst_request_fields(p.data_bits, burst, p.source_bits))
    l.b.output("nl_rreq_" + field, rrq.at(field), "rreq", field);
}

// A taken response beat loads the staging.
void InitiatorLowering::stage_beat(ValueId load) {
  std::vector<std::pair<ValueId, std::string>> staged{
      {r_rsp_data, "data"}, {r_rsp_result, "result"}, {r_rsp_error, "error"}, {r_rsp_last, "last"}};
  if (r_rsp_exclusive)
    staged.emplace_back(*r_rsp_exclusive, "exclusive_okay");
  if (r_rsp_user)
    staged.emplace_back(*r_rsp_user, "user");
  for (const auto &[reg, field] : staged) {
    auto value = r_rrsp_in.at(field);
    if (field == "data" && !p.lite && !p.apb && !p.ahb) {
      // The first INCR beat has no requested bytes below the captured start. Later beats may
      // replay earlier bytes of a gathered word, so only the first one clears this prefix.
      const auto offset = multi ? slot_field(l, r_track, "prefix", slot_of(r_rrsp_in, r_track))
                          : reuse_read_count()
                              ? l.b.slice(r_remaining, 0, std::countr_zero(be_bits))
                              : l.pick(l.eq(r_cap.burst_reg, 1),
                                       l.b.slice(r_cap.addr_reg, 0, std::countr_zero(be_bits)),
                                       l.num(std::countr_zero(be_bits), 0));
      const auto lanes =
          l.shl(l.b.unary(Op::BitNot, l.num(be_bits, 0), be_bits), offset, be_bits - 1);
      const auto first = l.both(r_rrsp_in.at("first"), l.both(l.eq(r_rrsp_in.at("result"), 0),
                                                              l.inv(r_rrsp_in.at("error"))));
      value = l.pick(first, l.both(value, strobe_lanes(l.b, lanes, be_bits)), value);
    }
    l.b.update(reg, l.b.select(load, value, reg));
  }
}

void InitiatorLowering::read_response() {
  const auto &d = r_cap.d;
  if (multi) {
    // The R face passes response beats in arrival order (bursts of different IDs interleave as
    // the network delivers them); a beat's tag names its burst's slot, its last marker is RLAST.
    // The local completion in the issue stage (state 3) counts its beats against its descriptor
    // and takes the face when no beat is staged (nothing else issues meanwhile). An offered beat
    // stays until accepted (AXI A3.2.1); the staging refills when it is empty or drains.
    const auto rsp_id = p.id_bits ? l.state("rr.rsp_id", p.id_bits) : ValueId{};
    const auto beat = l.state("rr.beat", p.len_bits);
    const auto local_burst = l.state("rr.local_burst", 1);
    const auto offered = l.state("rr.offered", 1);
    const auto face_local = l.bind(
        "rr.face_local", l.b.select(offered, local_burst, l.both(l.inv(r_rsp_full), r_errstream)));
    const auto last_beat = l.bind(
        "rr.last_beat", l.b.select(face_local, l.b.binary(Op::Equal, beat, d.len), r_rsp_last));
    auto rvalid = l.both(l.reset, l.b.select(face_local, r_errstream, r_rsp_full));
    auto raccept = l.bind("rr.accept", l.both(rvalid, r_rready));
    auto rrsp_ready =
        l.both(l.reset, l.either(l.inv(r_rsp_full), l.both(raccept, l.inv(face_local))));
    auto rrsp_load =
        l.bind("rr.rsp_load", l.both(l.both(r_rrsp_valid, rrsp_ready), owns(r_rrsp_in, r_track)));
    const auto pop = l.bind("rr.pop", l.both(raccept, last_beat));
    const auto local_issue = l.both(r_issuing, l.inv(r_sent));
    const auto push = l.bind("rr.push", l.either(r_emit_fire, local_issue));
    const auto enter = r_start;
    // A request passed straight through leaves the engine idle; a held one waits in state 2.
    const auto sent =
        held ? l.b.select(r_taken, l.b.constant(2, 0), l.b.constant(2, 2)) : l.b.constant(2, 0);
    l.b.update(
        r_rr_state,
        l.b.select(
            l.both(r_emit_fire, r_idle), sent,
            l.b.select(enter, l.b.constant(2, 1),
                       l.b.select(r_emit_fire, sent,
                                  l.b.select(local_issue, l.b.constant(2, 3),
                                             l.b.select(l.either(l.both(pop, face_local),
                                                                 held ? l.both(l.eq(r_rr_state, 2),
                                                                               r_taken)
                                                                      : zero),
                                                        l.b.constant(2, 0), r_rr_state))))));
    l.b.update(beat, l.b.select(l.both(pop, face_local), l.b.constant(p.len_bits, 0),
                                l.b.select(l.both(raccept, face_local), l.inc(beat), beat)));
    l.b.update(local_burst, l.b.select(rvalid, face_local, local_burst));
    l.b.update(offered, l.both(rvalid, l.inv(r_rready)));
    l.b.update(r_rsp_full,
               l.b.select(rrsp_load, one,
                          l.b.select(l.both(raccept, l.inv(face_local)), zero, r_rsp_full)));
    if (p.id_bits)
      l.b.update(rsp_id, l.b.select(rrsp_load, response_id(r_rrsp_in), rsp_id));
    stage_beat(rrsp_load);
    std::map<std::string, ValueId> captured;
    if (!p.lite && !p.apb && !p.ahb)
      captured["prefix"] =
          l.pick(l.eq(r_ar.burst, 1), l.b.slice(r_ar.addr, 0, std::countr_zero(be_bits)),
                 l.num(std::countr_zero(be_bits), 0));
    close_tracker(l, r_track, r_start, captured, push, r_issue_tag, {}, d.id, r_dest.second,
                  p.crossing,
                  {{l.both(rrsp_load, r_rrsp_in.at("last")), slot_of(r_rrsp_in, r_track),
                    p.id_bits ? response_id(r_rrsp_in) : ValueId{}},
                   {l.both(pop, face_local), r_cap.tag, r_cap.id_reg}});
    l.b.output("nl_rrsp_ready", rrsp_ready, "rrsp", "ready");
    if (held) {
      // The AR leaves the port pipe as the network takes its request, or on a local answer.
      r_arready = l.both(l.reset, l.either(r_taken, local_issue));
      r_ar_fire = l.bind("rr.ar_fire", l.both(r_ar.valid, r_arready));
    }
    axi_output("arready", r_arready);
    axi_output("rvalid", rvalid);
    axi_output("rdata", l.b.select(face_local, l.b.constant(p.data_bits, 0), r_rsp_data));
    axi_output("rresp", l.b.select(face_local, l.b.constant(2, 2),
                                   map_result(l, r_rsp_result, r_rsp_error, p.uniform_slverr,
                                              r_rsp_exclusive)));
    // An AXI4-Lite face has neither RLAST nor RID.
    const auto rlast = l.bind("rr.rlast", last_beat);
    if (!p.lite)
      axi_output("rlast", rlast);
    if (p.id_bits && !p.lite)
      axi_output("rid", l.bind("rr.face_id", l.b.select(face_local, r_cap.id_reg, rsp_id)));
    user_output("ruser", r_rsp_user, face_local);
    // A tracked engine never reaches the APB tail (an APB face holds at most one
    // burst), so this return is the final finish of lower_axi4_initiator.
    return;
  }
  if (held) {
    // The R face passes staged beats and owns its ID: the burst's end at the staging lets the next
    // burst issue while its last beat waits for RREADY. A local answer takes the face once no beat
    // is staged.
    const auto rsp_id = p.id_bits ? l.state("rr.rsp_id", p.id_bits) : ValueId{};
    const auto face_local = l.both(r_errstream, l.inv(r_rsp_full));
    const auto rvalid = l.both(l.reset, l.either(r_rsp_full, r_errstream));
    const auto raccept = l.bind("rr.accept", l.both(rvalid, r_rready));
    const auto rrsp_ready =
        l.both(l.reset, l.either(r_idle, l.both(r_stream, l.either(l.inv(r_rsp_full), raccept))));
    const auto rrsp_load =
        l.bind("rr.rsp_load", l.both(l.both(r_rrsp_valid, rrsp_ready), r_stream));
    const auto last_beat = l.eq(r_remaining, 0);
    const auto network_last = reuse_read_count() ? r_rrsp_in.at("last") : last_beat;
    const auto done =
        l.either(l.both(rrsp_load, network_last), l.both(l.both(raccept, face_local), last_beat));
    l.b.update(
        r_rr_state,
        l.b.select(r_start, l.b.select(r_emit_fire, l.b.constant(2, 2), l.b.constant(2, 1)),
                   l.b.select(l.both(r_emitting, l.inv(r_sent)), l.b.constant(2, 3),
                              l.b.select(r_emit_fire, l.b.constant(2, 2),
                                         l.b.select(done, l.b.constant(2, 0), r_rr_state)))));
    const auto count_step = reuse_read_count() ? l.both(raccept, face_local)
                                               : l.either(rrsp_load, l.both(raccept, face_local));
    update_read_count(count_step);
    l.b.update(r_rsp_full,
               l.b.select(rrsp_load, one,
                          l.b.select(l.both(raccept, l.inv(face_local)), zero, r_rsp_full)));
    stage_beat(rrsp_load);
    if (p.id_bits)
      l.b.update(rsp_id, l.b.select(rrsp_load, r_cap.id_reg, rsp_id));
    l.b.output("nl_rrsp_ready", rrsp_ready, "rrsp", "ready");
    r_arready = l.both(l.reset, l.either(r_taken, l.both(r_emitting, l.inv(r_sent))));
    r_ar_fire = l.bind("rr.ar_fire", l.both(r_ar.valid, r_arready));
    axi_output("arready", r_arready);
    axi_output("rvalid", rvalid);
    axi_output("rdata", l.b.select(face_local, l.b.constant(p.data_bits, 0), r_rsp_data));
    axi_output("rresp", l.b.select(face_local, l.b.constant(2, 2),
                                   map_result(l, r_rsp_result, r_rsp_error, p.uniform_slverr,
                                              r_rsp_exclusive)));
    axi_output("rlast", l.bind("rr.rlast", l.b.select(face_local, last_beat, r_rsp_last)));
    if (p.id_bits)
      axi_output("rid", l.b.select(face_local, r_cap.id_reg, rsp_id));
    user_output("ruser", r_rsp_user, face_local);
    return;
  }
  // Response beat skid buffer with flow-through; RLAST is forwarded from the fragment's last
  // marker. The local error stream counts rejected burst beats on the same valid/accept cone.
  auto rvalid = l.both(l.reset, l.either(l.both(r_stream, r_rsp_full), r_errstream));
  auto raccept = l.bind("rr.accept", l.both(rvalid, r_rready));
  // An idle engine takes and drops a beat (see wr.wrsp_fire).
  auto rrsp_ready =
      l.both(l.reset, l.either(r_idle, l.both(r_stream, l.either(l.inv(r_rsp_full), raccept))));
  auto rrsp_load = l.bind("rr.rsp_load", l.both(l.both(r_rrsp_valid, rrsp_ready), r_stream));
  // An APB burst has one beat.
  auto last_beat = p.apb ? one : l.eq(r_remaining, 0);
  const auto final = reuse_read_count() ? l.pick(r_errstream, last_beat, r_rsp_last) : last_beat;
  auto stream_done = l.both(raccept, final);

  l.b.update(
      r_rr_state,
      l.b.select(r_start, l.b.select(r_emit_fire, l.b.constant(2, 2), l.b.constant(2, 1)),
                 l.b.select(l.both(r_emitting, l.inv(r_sent)), l.b.constant(2, 3),
                            l.b.select(r_emit_fire, l.b.constant(2, 2),
                                       l.b.select(stream_done, l.b.constant(2, 0), r_rr_state)))));
  update_read_count(reuse_read_count() ? l.both(raccept, r_errstream) : raccept);
  l.b.update(r_rsp_full,
             l.b.select(stream_done, zero,
                        l.b.select(rrsp_load, one, l.b.select(raccept, zero, r_rsp_full))));
  stage_beat(rrsp_load);
  l.b.output("nl_rrsp_ready", rrsp_ready, "rrsp", "ready");

  axi_output("arready", r_arready);
  axi_output("rvalid", rvalid);
  axi_output("rdata", l.b.select(r_errstream, l.b.constant(p.data_bits, 0), r_rsp_data));
  axi_output("rresp", l.b.select(r_errstream, l.b.constant(2, 2),
                                 map_result(l, r_rsp_result, r_rsp_error, p.uniform_slverr,
                                            r_rsp_exclusive)));
  const auto last = l.bind("rr.rlast", l.b.select(r_errstream, last_beat, r_rsp_last));
  if (!p.lite)
    axi_output("rlast", last);
  if (p.id_bits && !p.lite)
    axi_output("rid", d.id);
  user_output("ruser", r_rsp_user, r_errstream);
}
void InitiatorLowering::apb_tail() {
  if (p.apb) {
    // PREADY is the registered B or R valid: one transfer held, answered no sooner than ACCESS.
    const auto bvalid = face_out.at("bvalid"), rvalid = face_out.at("rvalid");
    const auto bfail = l.b.slice(face_out.at("bresp"), 1, 1);
    const auto rfail = l.b.slice(face_out.at("rresp"), 1, 1);
    l.bind("wr.b_take", l.both(bvalid, apb_access));
    const auto answer = l.either(bvalid, rvalid);
    l.b.output("nl_apb_pready", answer, "apb", "pready");
    // PRDATA is zero unless the last answer is a read without error; its word stays in the staging,
    // which only the next read loads.
    const auto read_kept = l.state("rr.prdata_read", 1);
    const auto read_shown = l.b.select(answer, l.both(rvalid, l.inv(rfail)), read_kept);
    l.b.update(read_kept, read_shown);
    auto read_data = r_rsp_data;
    if (p.apb_data_bits < 32) {
      const auto lane_kept = l.state("rr.prdata_lane", 2);
      const auto lane = l.b.select(answer, l.b.slice(apb_read_address, 0, 2), lane_kept);
      l.b.update(lane_kept, lane);
      auto word = l.b.constant(p.apb_data_bits, 0);
      for (unsigned offset = 0; offset < 4; offset += face_bytes)
        word =
            l.b.select(l.eq(lane, offset), l.b.slice(read_data, offset * 8, p.apb_data_bits), word);
      read_data = word;
    }
    l.b.output("nl_apb_prdata", l.b.select(read_shown, read_data, l.b.constant(p.apb_data_bits, 0)),
               "apb", "prdata");
    // PSLVERR keeps the last answer until the next one.
    const auto kept = l.state("rr.pslverr", 1);
    const auto pslverr =
        l.b.select(answer, l.either(l.both(bvalid, bfail), l.both(rvalid, rfail)), kept);
    l.b.update(kept, pslverr);
    l.b.output("nl_apb_pslverr", pslverr, "apb", "pslverr");
  }
}

} // namespace

void InitiatorLowering::ahb_tail() {
  if (!p.ahb)
    return;
  const auto &a = ahb;
  const auto aw_fire = l.both(a.awvalid, face_out.at("awready"));
  const auto request = l.either(aw_fire, l.both(a.arvalid, face_out.at("arready")));
  const auto w_fire = l.both(a.wvalid, face_out.at("wready"));
  const auto bvalid = face_out.at("bvalid"), rvalid = face_out.at("rvalid");
  const auto r_fire = l.both(rvalid, a.rready);
  const auto bfail = l.b.slice(face_out.at("bresp"), 1, 1);
  const auto rfail = l.b.slice(face_out.at("rresp"), 1, 1);
  // B is always taken: a bufferable write ignores it, any other write ends on it.
  l.bind("wr.b_take", bvalid);
  const auto beat_w =
      l.both(l.both(a.serve, a.write), l.both(l.inv(a.done), w_fire)); // this beat's W
  const auto hold = l.both(l.inv(a.posted), a.beat_last);
  const auto answered = l.both(a.done, bvalid);
  const auto beat_r = l.both(l.both(a.serve, l.inv(a.write)), r_fire);
  const auto ok = l.either(l.either(l.both(beat_w, l.inv(hold)), l.both(answered, l.inv(bfail))),
                           l.both(beat_r, l.inv(rfail)));
  // The first ERROR cycle holds HREADYOUT low; the second (ahb.err) completes the beat.
  const auto fail =
      l.bind("ahb.fail", l.either(l.either(l.both(a.bad, l.inv(a.err)), l.both(answered, bfail)),
                                  l.both(beat_r, rfail)));
  const auto ready = l.bind("ahb.ready", l.either(l.inv(a.phase), l.either(a.err, ok)));
  l.b.output("nl_ahb_hreadyout", ready, "ahb", "hreadyout");
  l.b.output("nl_ahb_hresp", l.either(a.err, fail), "ahb", "hresp");
  l.b.output("nl_ahb_hrdata", face_out.at("rdata"), "ahb", "hrdata");
  l.b.update(a.err, fail);
  l.b.update(a.sent, l.both(l.inv(l.both(a.phase, ready)), l.either(a.sent, request)));
  l.b.update(a.done, l.b.select(answered, zero, l.either(a.done, l.both(beat_w, hold))));
  const auto left = l.minus(l.b.select(request, l.plus(l.zext(a.len, 5), l.num(5, 1)), a.left),
                            l.zext(l.either(w_fire, r_fire), 5));
  l.b.update(a.left, left);
  l.b.update(a.open_write, l.b.select(request, a.write, a.open_write));
  l.b.update(a.flight, l.b.select(aw_fire, one, l.b.select(bvalid, zero, a.flight)));
  // A piece with beats left ends when HREADY samples anything but a SEQ or BUSY of its burst: its
  // W beats go out unstrobed, its R beats are dropped.
  const auto continuing = l.both(a.hsel, l.either(l.eq(a.htrans, 3), l.eq(a.htrans, 1)));
  l.b.update(a.end,
             l.both(l.inv(l.eq(left, 0)), l.either(a.end, l.both(a.hready, l.inv(continuing)))));
}
Result<NodeHardware> lower_axi4_initiator(const Axi4InitiatorProfile &profile) {
  std::vector<Diagnostic> errors;
  initiator_width_errors(profile, errors);
  initiator_capacity_errors(profile, errors);
  // The route windows are checked only once the profile itself is clean.
  if (errors.empty())
    initiator_window_errors(profile, errors);
  if (!errors.empty())
    return Result<NodeHardware>::failure(std::move(errors));

  // Zero tag bits with several bursts in flight derive the narrowest field.
  auto resolved = profile;
  if (resolved.max_outstanding > 1 && !resolved.tag_bits)
    resolved.tag_bits = static_cast<unsigned>(std::bit_width(resolved.max_outstanding - 1));
  const auto &p = resolved;
  logic::Logic l;
  InitiatorLowering engine(l, p);
  engine.apb_face();
  engine.ahb_face();

  // ---------------- write direction ----------------
  if (p.write) {
    engine.write_issue();
    engine.write_complete();
  }

  // ---------------- read direction ----------------
  if (p.read) {
    engine.read_issue();
    engine.read_response();
  }

  engine.apb_tail();
  engine.ahb_tail();
  return l.finish("nocloom_axi4_initiator_" + p.name);
}

} // namespace nocloom::detail
