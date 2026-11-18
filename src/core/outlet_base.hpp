#pragma once
#include "ahb_outlet.hpp"
#include "burst_faces.hpp"
#include "exclusive_monitor.hpp"
#include "logic.hpp"
#include "mapping_decode.hpp"
#include "message.hpp"
#include <algorithm>
#include <iterator>
#include <numeric>
#include <optional>

namespace nocloom::detail::outlet {
using logic::Fields;
using logic::Logic;
// 2^size bytes for sizes up to max_size (2 on a 32-bit word).
inline ValueId stride(Logic &l, ValueId size, unsigned bits, unsigned max_size = 2) {
  if (max_size == 2)
    return l.b.select(l.eq(size, 2), l.b.constant(bits, 4),
                      l.b.select(l.eq(size, 1), l.b.constant(bits, 2), l.b.constant(bits, 1)));
  return l.pow2(size, bits, max_size);
}
// value << n in its own width.
inline ValueId shift_left(Logic &l, ValueId value, unsigned n) {
  const unsigned width = l.width(value);
  if (n == 0)
    return value;
  if (n >= width)
    return l.b.constant(width, 0);
  return l.b.concat(l.b.slice(value, 0, width - n), l.b.constant(n, 0));
}
// A constant of any width (literals hold 64 bits) whose bit i is bit(i).
template <typename Bit> ValueId pattern(Logic &l, unsigned width, Bit bit) {
  std::optional<ValueId> out;
  for (unsigned low = 0; low < width; low += 64) {
    const unsigned n = std::min(64U, width - low);
    std::uint64_t value = 0;
    for (unsigned i = 0; i < n; ++i)
      if (bit(low + i))
        value |= std::uint64_t{1} << i;
    const auto chunk = l.b.constant(n, value);
    out = out ? l.b.concat(chunk, *out) : chunk;
  }
  return *out;
}
inline ValueId merge_error(Logic &l, ValueId a, ValueId b) {
  return l.b.select(l.either(l.eq(a, 1), l.eq(b, 1)), l.b.constant(2, 1),
                    l.b.select(l.either(l.inv(l.eq(a, 0)), l.inv(l.eq(b, 0))), l.b.constant(2, 2),
                               l.b.constant(2, 0)));
}
inline ValueId next_address(Logic &l, ValueId address, const Fields &d, unsigned bits,
                            unsigned max_size = 2) {
  auto next = l.plus(address, stride(l, d.at("size"), bits, max_size));
  // The WRAP container: (length + 1) beats of 2^size bytes, less one byte: the length above the
  // size's low bits, all set; no carry to wait for.
  const auto length = l.b.unary(Op::ZeroExtend, d.at("length"), bits);
  auto mask = length;
  for (unsigned k = 1; k <= max_size; ++k)
    mask = l.b.select(l.eq(d.at("size"), k),
                      l.b.binary(Op::BitOr, shift_left(l, length, k),
                                 l.b.constant(bits, (std::uint64_t{1} << k) - 1)),
                      mask);
  auto wrapped = l.b.binary(
      Op::BitOr, l.b.binary(Op::BitAnd, d.at("address"), l.b.unary(Op::BitNot, mask, bits)),
      l.b.binary(Op::BitAnd, next, mask));
  return l.b.select(l.eq(d.at("burst"), 0), address,
                    l.b.select(l.eq(d.at("burst"), 2), wrapped, next));
}
// The network faces, window decode and response framing shared by the
// APB/AXI4-Lite engine and the AHB engine.
struct OutletBase {
  const BurstOutletProfile &p;
  const unsigned bits, word_bits, lanes, max_size;
  const bool wide_bus;
  const std::uint64_t space;
  const bool subpage;
  Logic l;
  const ValueId zero, one, ok, target_error;
  // Target timeout: failed holds until the common reset (Logic::watchdog);
  // without completion a failed target absorbs every response it produces.
  const unsigned timeout;
  const bool report_only, monitored;
  const ValueId failed;
  std::optional<ExclusiveMonitor> monitor;
  explicit OutletBase(const BurstOutletProfile &profile)
      : p(profile), bits(p.transport_address_bits ? p.transport_address_bits : 32),
        word_bits(p.data_bits), lanes(word_bits / 8),
        max_size(static_cast<unsigned>(std::countr_zero(lanes))), wide_bus(word_bits != 32),
        space(std::uint64_t{1} << bits),
        subpage(std::ranges::any_of(p.mappings,
                                    [](const AddressMapping &m) { return m.size % 4096 != 0; })),
        zero(l.bind("c.zero", l.bit(0))), one(l.bind("c.one", l.bit(1))), ok(l.b.constant(2, 0)),
        target_error(l.b.constant(2, 2)), timeout(p.timeout_cycles),
        report_only(timeout && !p.timeout_completion),
        monitored(p.exclusive_target.mode == ExclusiveAccess::Network),
        failed(timeout ? l.state("timeout.failed", 1) : zero) {
    l.block = p.arrival;
    l.block_bits = bits;
  }
  // A failed target turns every word that would reach it into SLVERR.
  ValueId isolate(ValueId code) {
    return timeout ? l.b.select(l.both(failed, l.eq(code, 0)), target_error, code) : code;
  }
  ValueId watchdog(ValueId waiting) {
    const auto expiry = l.watchdog(failed, timeout, waiting);
    l.out("status", "timed_out", failed);
    return expiry;
  }
  // Network exclusive monitor: an exclusive write without a matching
  // reservation is failing, so it reaches the target without strobes.
  ValueId watch(const Fields &w) {
    monitor.emplace(l, "exclusive",
                    ExclusiveMonitorProfile{p.exclusive_target.monitors, p.exclusive_target.granule,
                                            bits, p.exclusive_target.id_flags, p.source_bits,
                                            p.id_bits});
    const auto region = monitor->region(localized(w.at("address")), w.at("length"), w.at("size"));
    return l.bind("w.exclusive_failing",
                  l.both(w.at("lock"), l.inv(monitor->holds(monitor->key(w), region))));
  }
  // A write in flight that an exclusive read must not overlap: while held,
  // unless it failed its own check, over the packet range of its fields.
  struct Held {
    ValueId held, fail;
    const Fields *packet = nullptr;
  };
  // An exclusive read records its reservation at its head unless a write in
  // flight overlaps the region (then it changes no reservation): the write
  // head reaching the target now, then the held writes (the head last when
  // incoming_last). Every write head that reaches the target (clearing) drops
  // the reservations it overlaps.
  void reserve(const Fields &w, const Fields &r, ValueId rt, ValueId reaching, ValueId clearing,
               const std::vector<Held> &writes, bool incoming_last = false) {
    const auto region = monitor->region(localized(r.at("address")), r.at("length"), r.at("size"));
    const auto incoming =
        packet_range(l, localized(w.at("address")), w.at("length"), w.at("size"), w.at("burst"));
    std::vector<ValueId> overlaps{l.both(reaching, monitor->overlaps(incoming, region))};
    for (const auto &h : writes) {
      const auto &f = *h.packet;
      overlaps.push_back(
          l.both(l.both(h.held, l.inv(h.fail)),
                 monitor->overlaps(packet_range(l, localized(f.at("address")), f.at("length"),
                                                f.at("size"), f.at("burst")),
                                   region)));
    }
    if (incoming_last)
      std::ranges::rotate(overlaps, overlaps.begin() + 1);
    const auto contaminated =
        std::accumulate(std::next(overlaps.begin()), overlaps.end(), overlaps.front(),
                        [&](ValueId a, ValueId b) { return l.either(a, b); });
    const auto read = l.bind("r.exclusive_read", l.both(rt, r.at("lock")));
    monitor->update(l.both(read, l.inv(l.bind("r.exclusive_contaminated", contaminated))),
                    monitor->key(r), region, l.bind("w.exclusive_clear", clearing), incoming);
  }
  // After expiry the read packet held at the failure keeps reaching the target from an orphan copy
  // (timeout.orphan.*): its words load from the live packet in the first failed cycle (capture),
  // its packet fields are latched then, and the words move with the orphan's own steps.
  struct Orphan {
    Fields word, packet;
    ValueId taken{}, capture{};
  };
  Orphan orphan(std::initializer_list<std::pair<const char *, unsigned>> words, const Fields &live,
                const std::vector<const char *> &fields) {
    Orphan c;
    c.taken = l.state("timeout.orphan.taken", 1);
    for (const auto &[name, width] : words)
      c.word[name] = std::string_view(name) == "address"
                         ? l.address_state("timeout.orphan.address", width)
                         : l.state("timeout.orphan." + std::string(name), width);
    c.capture = l.both(failed, l.inv(c.taken));
    for (const auto *name : fields)
      c.packet[name] =
          l.latch("timeout.orphan.packet." + std::string(name), live.at(name), c.capture);
    l.b.update(c.taken, l.either(c.taken, c.capture));
    return c;
  }
  // An orphan word: the live value at capture, moved at step, else stay
  // (the word itself by default).
  void move(const Orphan &c, const char *name, ValueId live, ValueId step, ValueId moved,
            std::optional<ValueId> stay = std::nullopt) {
    const auto word = c.word.at(name);
    l.b.update(word, l.b.select(c.capture, live, l.b.select(step, moved, stay.value_or(word))));
  }
  ValueId active(ValueId value) { return l.both(l.reset, value); }
  // The target's BUSER or RUSER as the response user field where taken is set, else zero; none
  // without the field or the port.
  std::optional<ValueId> target_user(const char *signal, ValueId taken) {
    if (!p.response_user_bits || !p.response_user_port_bits)
      return std::nullopt;
    return l.b.select(taken,
                      l.fit(l.in("axil", signal, p.response_user_port_bits), p.response_user_bits),
                      l.b.constant(p.response_user_bits, 0));
  }
  Fields request(const std::string &face) {
    BurstProfile profile{p.id_bits, p.packet_len_bits, true, bits};
    profile.exclusive = p.exclusive;
    profile.tag_bits = p.tag_bits;
    profile.user_bits = p.user_bits;
    profile.response_user_bits = p.response_user_bits;
    profile.data_user_bits_per_byte = p.data_user_bits_per_byte;
    Fields f = l.arrive(l.face(face, burst_request_fields(word_bits, profile, p.source_bits)));
    // A read-only target denies every write word, a write-only one every read word
    // (local_result). The engine sees a denied packet's first fragment one cycle after it
    // arrives, so its local answer leaves as late as an AXI4 target's.
    if ((p.read_only && face == "wreq") || (p.write_only && face == "rreq")) {
      f["denied"] = one;
      const auto held = l.state(face == "wreq" ? "w.denied_held" : "r.denied_held", 1);
      const auto open = l.either(l.inv(f.at("first")), held);
      denials.emplace(face, Denial{f.at("valid"), f.at("first"), held, open});
      f["valid"] = l.both(f.at("valid"), open);
    }
    return f;
  }
  // A denied face's ready also waits for its held first fragment (request); others pass.
  struct Denial {
    ValueId valid, first, held, open;
  };
  std::map<std::string, Denial> denials;
  void out(const std::string &face, const std::string &field, ValueId value) {
    if (const auto denial = denials.find(face); field == "ready" && denial != denials.end()) {
      const auto &d = denial->second;
      value = l.both(value, d.open);
      const auto taken = l.both(l.both(d.valid, d.open), value);
      l.b.update(d.held, l.b.select(taken, zero, l.both(d.valid, d.first)));
    }
    l.out(face, field, value);
  }
  std::vector<const char *> captured() const {
    std::vector<const char *> fields{"address", "length", "size", "burst", "cache", "protection"};
    if (p.ahb5)
      fields.push_back("user");
    return fields;
  }
  Fields capture(const std::string &prefix, const Fields &f, ValueId take) {
    Fields d;
    for (const auto *name : captured())
      d[name] = l.latch(prefix + "." + name, f.at(name), take);
    if (p.id_bits)
      d["id"] = l.latch(prefix + ".id", f.at("id"), take);
    if (p.source_bits)
      d["source"] = l.latch(prefix + ".source", f.at("source"), take);
    if (p.tag_bits)
      d["tag"] = l.latch(prefix + ".tag", f.at("tag"), take);
    return d;
  }
  // capture() split in two: the descriptor registers now, their load later.
  Fields hold(const std::string &prefix, const Fields &f) {
    Fields d;
    for (const auto *name : captured())
      d[name] = l.state_like(prefix + "." + name, f.at(name));
    if (p.id_bits)
      d["id"] = l.state(prefix + ".id", l.b.ir.values.at(f.at("id").index).width);
    if (p.source_bits)
      d["source"] = l.state(prefix + ".source", l.b.ir.values.at(f.at("source").index).width);
    if (p.tag_bits)
      d["tag"] = l.state(prefix + ".tag", l.b.ir.values.at(f.at("tag").index).width);
    return d;
  }
  void load(const Fields &d, const Fields &f, ValueId take) { load_fields(l, d, f, take); }
  ValueId mapped(ValueId address) { return mapping_decode::any(l, address, bits, p.mappings); }
  // The local address of a global one, before the port narrows it.
  ValueId localized(ValueId address) {
    return p.mappings.empty() ? address : mapping_decode::local(l, address, bits, p.mappings);
  }
  // Its low kept bits alone.
  ValueId localized(ValueId address, unsigned kept) {
    return mapping_decode::local(l, address, bits, p.mappings, kept);
  }
  // The code of a read packet's beat at following: a sub-page window
  // re-decodes every later beat, so a WRAP burst that starts in the page
  // remainder still reaches the window; a network or request error sticks.
  ValueId next_code(ValueId initial, ValueId following, const Fields &r, ValueId rt) {
    auto code = initial;
    if (subpage) {
      const auto hit = mapped(following);
      const auto sticks =
          l.either(l.eq(initial, 2),
                   l.b.select(rt, r.at("error"), l.latch("r.error_request", r.at("error"), rt)));
      code = l.b.select(sticks, initial, l.b.select(hit, ok, l.b.constant(2, 1)));
    }
    return isolate(code);
  }
  ValueId local_result(ValueId address, ValueId net_error, ValueId burst, ValueId size,
                       bool denied) {
    // An error request is a decode miss the source sent here: DECERR without access.
    auto code = l.b.select(net_error, l.b.constant(2, 1), ok);
    if (!p.mappings.empty() && p.mappings.front().size != space) {
      // Named first: IR creation order must not depend on argument evaluation.
      const auto hit = mapped(address);
      code = l.b.select(l.both(l.inv(hit), l.inv(net_error)), l.b.constant(2, 1), code);
    }
    // A write to a read-only target, or a read of a write-only one, answers SLVERR without
    // access, after the window decode.
    if (denied)
      code = l.b.select(l.eq(code, 0), target_error, code);
    // APB, AHB and AXI-Lite targets take no fixed-address burst: by default a
    // FIXED burst, single beats included, completes with SLVERR and no target
    // access, after the window decode. fixed_single serves its beats instead.
    if (!p.fixed_single)
      code = l.b.select(l.both(l.eq(burst, 0), l.eq(code, 0)), target_error, code);
    // Defensive: the source width clamp never delivers a beat wider than a
    // wide AHB word; one that did would answer SLVERR without access.
    if (wide_bus && max_size < 7) {
      const auto oversize = l.b.binary(Op::UnsignedLess, l.b.constant(3, max_size), size);
      code = l.b.select(l.both(oversize, l.eq(code, 0)), target_error, code);
    }
    return code;
  }
  ValueId local_result(const Fields &f) {
    return local_result(f.at("address"), f.at("error"), f.at("burst"), f.at("size"),
                        f.contains("denied"));
  }
  void response(const std::string &face, const Fields &d, ValueId full, ValueId address,
                ValueId code, ValueId first, ValueId last, ValueId data, ValueId exclusive_okay) {
    if (face == "rrsp" && p.response_run_bits)
      l.b.output("nl_hint_wrap_end", d.at("run_end"), "hint", "wrap_end");
    l.out(face, "valid", active(full));
    l.out(face, "address", address);
    l.out(face, "result", code);
    if (p.exclusive)
      l.out(face, "exclusive_okay", exclusive_okay);
    // The response user field: an AXI4-Lite target's own (response_user), zero elsewhere.
    if (p.response_user_bits)
      l.out(face, "user",
            d.contains("response_user") ? d.at("response_user")
                                        : l.b.constant(p.response_user_bits, 0));
    l.out(face, "count", stride(l, d.at("size"), burst_count_bits(word_bits), max_size));
    l.out(face, "first", first);
    l.out(face, "last", last);
    l.out(face, "error", zero);
    l.out(face, "data", data);
    if (p.data_user_bits_per_byte)
      l.out(face, "data_user",
            d.contains("data_user") ? d.at("data_user")
                                    : l.num(word_bits / 8 * p.data_user_bits_per_byte, 0));
    if (p.tag_bits)
      l.out(face, "tag", d.at("tag"));
    if (p.id_bits)
      l.out(face, "id", d.at("id"));
    if (p.source_bits)
      l.out(face, "source", d.at("source"));
  }
  // The byte lanes of the AxSIZE container that holds address.
  ValueId read_mask(ValueId address, ValueId size) {
    if (wide_bus) {
      const auto offset = l.b.slice(address, 0, max_size);
      ValueId container = zero;
      for (unsigned k = 0; k <= max_size; ++k) {
        auto m = pattern(l, lanes, [&](unsigned i) { return i < (1U << k); });
        // Shift by the container offset; the bits below k are ignored.
        for (unsigned j = k; j < max_size; ++j)
          m = l.b.select(l.b.slice(offset, j, 1), shift_left(l, m, 1U << j), m);
        container = k == 0 ? m : l.b.select(l.eq(size, k), m, container);
      }
      return container;
    }
    auto lane = l.b.slice(address, 0, 2);
    auto mask = l.b.constant(4, 1);
    for (unsigned i = 1; i < 4; ++i)
      mask = l.b.select(l.eq(lane, i), l.b.constant(4, 1U << i), mask);
    return l.b.select(
        l.eq(size, 2), l.b.constant(4, 15),
        l.b.select(l.eq(size, 1),
                   l.b.select(l.b.slice(address, 1, 1), l.b.constant(4, 12), l.b.constant(4, 3)),
                   mask));
  }
};

} // namespace nocloom::detail::outlet
