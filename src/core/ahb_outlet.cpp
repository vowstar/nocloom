#include "ahb_outlet.hpp"
#include "attribute_map.hpp"
#include "beat_stage.hpp"
#include "burst_faces.hpp"
#include "diagnostic.hpp"
#include "exclusive_monitor.hpp"
#include "logic.hpp"
#include "mapping_decode.hpp"
#include "message.hpp"
#include "packet_context.hpp"
#include <algorithm>
#include <bit>
#include <functional>
#include <iterator>
#include <map>
#include <numeric>
#include <optional>
#include <string>
#include <vector>

namespace nocloom::detail {
namespace {
using Fields = std::map<std::string, ValueId>;
using logic::Logic;
// 2^size bytes for sizes up to max_size (2 on a 32-bit word).
ValueId stride(Logic &l, ValueId size, unsigned bits, unsigned max_size = 2) {
  if (max_size == 2)
    return l.b.select(l.eq(size, 2), l.b.constant(bits, 4),
                      l.b.select(l.eq(size, 1), l.b.constant(bits, 2), l.b.constant(bits, 1)));
  return l.pow2(size, bits, max_size);
}
// value << n in its own width.
ValueId shift_left(Logic &l, ValueId value, unsigned n) {
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
ValueId merge_error(Logic &l, ValueId a, ValueId b) {
  return l.b.select(l.either(l.eq(a, 1), l.eq(b, 1)), l.b.constant(2, 1),
                    l.b.select(l.either(l.inv(l.eq(a, 0)), l.inv(l.eq(b, 0))), l.b.constant(2, 2),
                               l.b.constant(2, 0)));
}
ValueId next_address(Logic &l, ValueId address, const Fields &d, unsigned bits,
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
} // namespace

namespace {
const char *pick_kind(Protocol kind, const char *apb_code, const char *lite_code,
                      const char *ahb_code) {
  const bool apb = kind == Protocol::Apb4 || kind == Protocol::Apb3 || kind == Protocol::Apb2;
  return apb ? apb_code : kind == Protocol::Axi4Lite ? lite_code : ahb_code;
}
// The profile checks shared by the APB, AXI4-Lite and AHB outlets, one helper per
// field group; profile_error runs them in field order and reports the first miss.
const char *outlet_profile_code(Protocol kind) {
  return pick_kind(kind, "NCL_APB_BURST_OUTLET_PROFILE", "NCL_AXI_LITE_OUTLET_PROFILE",
                   "NCL_AHB_OUTLET_PROFILE");
}
std::optional<Diagnostic> profile_bounds_error(const BurstOutletProfile &p, Protocol kind) {
  const bool apb = kind == Protocol::Apb4 || kind == Protocol::Apb3 || kind == Protocol::Apb2;
  const bool lite = kind == Protocol::Axi4Lite;
  const unsigned bits = p.transport_address_bits ? p.transport_address_bits : 32;
  if (!valid_name(p.name) || p.id_bits > 16 || p.source_bits > max_source_bits ||
      (p.len_bits < 4 || p.len_bits > 8) || bits < 32 || bits > 48 || p.address_bits < 2 ||
      p.address_bits > 32 || (!apb && !lite && kind != Protocol::AhbV3))
    return error(outlet_profile_code(kind),
                 pick_kind(kind, "apb_burst_outlet", "axi_lite_outlet", "ahb_outlet"),
                 "Expected a valid name, 0..16 ID bits, 4..8 length bits, 0..9 source bits, "
                 "32..48 transport and 2..32 local address bits");
  return std::nullopt;
}
std::optional<Diagnostic> profile_timeout_error(const BurstOutletProfile &p, Protocol kind) {
  const unsigned timeout = p.timeout_cycles;
  const bool fixed_ready = kind == Protocol::Apb2;
  if ((timeout && (!valid_timeout_cycles(timeout) || fixed_ready)) ||
      (!timeout && p.timeout_completion))
    return error(outlet_profile_code(kind), "timeout_cycles",
                 "Expected a power of two from 16 to 1048576 timeout cycles on a target that can "
                 "stall; APB2 "
                 "cannot");
  return std::nullopt;
}
std::optional<Diagnostic> profile_shaping_error(const BurstOutletProfile &p, Protocol kind) {
  if ((p.complete_write_bursts || !p.undefined_length_bursts) && kind != Protocol::AhbV3)
    return error(outlet_profile_code(kind), "complete_write_bursts",
                 "Burst shaping applies to AHB targets only");
  return std::nullopt;
}
std::optional<Diagnostic> profile_exclusive_error(const BurstOutletProfile &p, Protocol kind) {
  const bool monitored = p.exclusive_target.mode == ExclusiveAccess::Network;
  if ((p.exclusive_target.mode != ExclusiveAccess::None && !monitored) ||
      (monitored &&
       (!p.exclusive || p.exclusive_target.monitors == 0 || p.exclusive_target.monitors > 16 ||
        !std::has_single_bit(p.exclusive_target.granule) || p.exclusive_target.granule > 128)))
    return error(outlet_profile_code(kind), "exclusive_target",
                 "A dedicated target supports only a network monitor of 1..16 reservations with "
                 "a power-of-two granule of 1..128 bytes, over the Weft exclusive fields");
  return std::nullopt;
}
std::optional<Diagnostic> profile_face_error(const BurstOutletProfile &p, Protocol kind) {
  const bool apb = kind == Protocol::Apb4 || kind == Protocol::Apb3 || kind == Protocol::Apb2;
  const bool lite = kind == Protocol::Axi4Lite;
  if (p.hresp_width != 1 && (p.hresp_width != 2 || kind != Protocol::AhbV3))
    return error(outlet_profile_code(kind), "hresp_width",
                 "Expected a 1-bit HRESP, or 2 bits on an AHB target");
  // Target word: APB carries 32 bits, AXI4-Lite 32 or 64, and AHB 32 to 1024 (AHB-Lite 6.2).
  if ((apb && p.apb_data_bits != 8 && p.apb_data_bits != 16 && p.apb_data_bits != 32) ||
      (!apb && p.apb_data_bits != 32))
    return error(outlet_profile_code(kind), "apb_data_bits",
                 "Expected an 8-, 16- or 32-bit APB face");
  if (p.apb_back_to_back && !apb)
    return error(outlet_profile_code(kind), "apb_back_to_back",
                 "Back-to-back access requires an APB target");
  const unsigned word_bits = p.data_bits;
  if (word_bits != 32 && !(lite && word_bits == 64) &&
      (kind != Protocol::AhbV3 || word_bits < 32 || word_bits > 1024 ||
       !std::has_single_bit(word_bits)))
    return error(outlet_profile_code(kind), "data_bits",
                 "Expected 32 data bits for APB, 32 or 64 for AXI4-Lite, or 32 to 1024 "
                 "(a power of two) for AHB");
  return std::nullopt;
}
std::optional<Diagnostic> profile_outstanding_error(const BurstOutletProfile &p, Protocol kind) {
  const bool lite = kind == Protocol::Axi4Lite;
  // AXI4-Lite pipelines packets of one direction, 1..256; APB and AHB keep
  // one packet per direction.
  if (p.max_outstanding == 0 || p.max_outstanding > 256 || (p.max_outstanding > 1 && !lite))
    return error(outlet_profile_code(kind), "max_outstanding",
                 "Expected 1..256 packets in flight for AXI4-Lite, 1 otherwise");
  // Flows are port sets, each reached by a mapping.
  if (p.flows == 0 || p.flows > 32 ||
      std::ranges::any_of(p.mappings, [&](const AddressMapping &m) { return m.flow >= p.flows; }))
    return error(outlet_profile_code(kind), "flows", "Expected 1..32 flows and mappings of them");
  return std::nullopt;
}
std::optional<Diagnostic> profile_window_error(const BurstOutletProfile &p, Protocol kind) {
  const unsigned bits = p.transport_address_bits ? p.transport_address_bits : 32;
  const unsigned lanes = p.data_bits / 8;
  if ((p.mappings.empty() && p.address_bits < bits) ||
      !mapping_decode::valid(p.mappings, bits, p.address_bits, std::max(4U, lanes)) ||
      !mapping_decode::holds(p.arrival, p.mappings, bits))
    return error(pick_kind(kind, "NCL_APB_BURST_OUTLET_WINDOW", "NCL_AXI_LITE_OUTLET_WINDOW",
                           "NCL_AHB_OUTLET_WINDOW"),
                 "mappings",
                 "Expected disjoint mappings in base order, each nonempty whole 4 KiB pages or a "
                 "power of two below 4 KiB of at least a word, base and local address aligned, "
                 "within the transport space and its arrival block and fitting the local address "
                 "port");
  return std::nullopt;
}
std::optional<Diagnostic> profile_error(const BurstOutletProfile &p, Protocol kind) {
  if (auto bad = profile_bounds_error(p, kind))
    return bad;
  if (auto bad = profile_timeout_error(p, kind))
    return bad;
  if (auto bad = profile_shaping_error(p, kind))
    return bad;
  if (auto bad = profile_exclusive_error(p, kind))
    return bad;
  if (auto bad = profile_face_error(p, kind))
    return bad;
  if (auto bad = profile_outstanding_error(p, kind))
    return bad;
  return profile_window_error(p, kind);
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
  // The timer counts while something is pending and no response is offered.
  ValueId watchdog(ValueId pending, ValueId responding) {
    const auto expiry = l.watchdog(failed, timeout, pending, responding);
    l.out("status", "timed_out", failed);
    return expiry;
  }
  // Network exclusive monitor: an exclusive write without a matching
  // reservation is failing, so it reaches the target without strobes.
  ValueId watch(const Fields &w) {
    monitor.emplace(l, "exclusive",
                    ExclusiveMonitorProfile{p.exclusive_target.monitors, p.exclusive_target.granule,
                                            bits, p.exclusive_target.id_flags});
    const auto region = monitor->region(w.at("address"), w.at("length"), w.at("size"));
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
    const auto region = monitor->region(r.at("address"), r.at("length"), r.at("size"));
    const auto incoming =
        packet_range(l, w.at("address"), w.at("length"), w.at("size"), w.at("burst"));
    std::vector<ValueId> overlaps{l.both(reaching, monitor->overlaps(incoming, region))};
    for (const auto &h : writes) {
      const auto &f = *h.packet;
      overlaps.push_back(l.both(l.both(h.held, l.inv(h.fail)),
                                monitor->overlaps(packet_range(l, f.at("address"), f.at("length"),
                                                               f.at("size"), f.at("burst")),
                                                  region)));
    }
    if (incoming_last)
      std::ranges::rotate(overlaps, overlaps.begin() + 1);
    auto contaminated = overlaps.front();
    for (std::size_t k = 1; k < overlaps.size(); ++k)
      contaminated = l.either(contaminated, overlaps[k]);
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
    BurstProfile profile{p.id_bits, p.len_bits, true, bits};
    profile.exclusive = p.exclusive;
    profile.tag_bits = p.tag_bits;
    profile.user_bits = p.user_bits;
    profile.response_user_bits = p.response_user_bits;
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
  static std::vector<const char *> captured() {
    return {"address", "length", "size", "burst", "cache", "protection"};
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

// The APB engine, split into its write/read acceptance, the bus sequencer and
// the write/read completion sides so that every builder function stays small;
// the sides share the face, descriptor and response state.
struct ApbOutlet {
  ApbOutlet(const BurstOutletProfile &profile, Protocol kind);
  OutletBase o;
  const BurstOutletProfile &p;
  logic::Logic &l;
  // APB3/APB2 carry no PSTRB/PPROT; APB2 has no PREADY/PSLVERR either.
  const bool strobeless, fixed_ready, narrow_apb, continuous, narrow_strobeless;
  // A serial outlet without a timer takes a write head only once the previous B has left and
  // issues a read only once the previous response has left, so it keeps one response per
  // direction; the timer's expiry lifts the serial rule, so a timed outlet keeps two.
  const bool single;
  // A serial outlet of whole words issues a write word in the cycle it takes it: a head waits for
  // every earlier access and a later word for the transfer before it, so the bus is idle at every
  // take and no word waits (ApbOutletForm).
  const bool at_once, write_in_entry, packet_coded;
  const unsigned physical_lanes;
  const ValueId zero, one, ok, target_error;
  const unsigned bits, lanes, max_size;
  const std::uint64_t space;
  const bool subpage;
  const unsigned timeout;
  const bool monitored, report_only;
  const ValueId failed;
  // With several flows (BurstOutletProfile::flows) the APB face is one port set per flow: an
  // input is that of the transfer's flow (bus_flow), PSEL and PENABLE rise on its set alone and the
  // other outputs drive every set.
  ValueId bus_flow = zero;
  ValueId in(const std::string &face, const std::string &field, unsigned width) {
    if (face != "apb" || p.flows < 2)
      return l.in(face, field, width);
    auto value = l.in(face, field + "_0", width);
    for (unsigned k = 1; k < p.flows; ++k)
      value =
          l.b.select(l.eq(bus_flow, k), l.in(face, field + "_" + std::to_string(k), width), value);
    return value;
  }
  void out(const std::string &face, const std::string &field, ValueId v) {
    if (face != "apb" || p.flows < 2) {
      o.out(face, field, v);
      return;
    }
    const bool selects = field == "psel" || field == "penable";
    for (unsigned k = 0; k < p.flows; ++k)
      o.out(face, field + "_" + std::to_string(k), selects ? l.both(v, l.eq(bus_flow, k)) : v);
  }
  ValueId active(ValueId value) { return o.active(value); }
  ValueId isolate(ValueId code) { return o.isolate(code); }
  Fields response_state(const char *stem);
  Fields queue_meta(const char *stem);
  // A word owes a transfer while lanes remain; a word without target access
  // (window miss, failed exclusive write, failed target) owes none.
  ValueId owes() { return l.inv(l.eq(wm, 0)); }

  // Faces and the write side state. Phase-assigned handles start at a defined value.
  Fields w, r;
  ValueId wb = zero, wf = zero, bf = zero, wm = zero, wc = zero, btf = zero, wfl = zero;
  ValueId serial = zero, rb = zero, rf = zero;
  Fields bq, bt;
  ValueId wt = zero, wh = zero, wl = zero;
  Fields wd, wd_now;
  ValueId wf_now = zero, wb_now = zero, wa_now = zero, wdata_now = zero, wl_now = zero;
  ValueId wdata = zero;
  ValueId wm_now = zero, wcode = zero, wmask = zero, finish_w = zero;
  ValueId write_service = zero, local_word = zero;
  ValueId failing = zero, w_fail = zero, w_exclusive = zero, fail_here = zero;
  // Read side state.
  ValueId rm = zero, rc = zero, ra = zero, rn = zero, rfirst = zero, rdata = zero, gathered = zero;
  ValueId oa = zero, oc = zero, ofirst = zero, olast = zero;
  ValueId tail_full = zero, tail_data = zero, tail_address = zero, tail_result = zero;
  ValueId tail_first = zero, tail_last = zero;
  Fields om, otm;
  ValueId used2 = zero, full2 = zero, rt = zero, rpop = zero, rtake = zero, last = zero;
  ValueId rpush = zero, rnext = zero, room = zero;
  ValueId r_exclusive = zero;
  Fields rd_now;
  ValueId initial_code = zero, initial_mask = zero;
  ValueId rb_now = zero, rfirst_now = zero, ra_now = zero, rm_now = zero, rn_now = zero;
  ValueId rc_now = zero, next_ra = zero, next_code = zero;
  // The idle/SETUP/ACCESS bus sequencer and its registers.
  ValueId phase = zero, owner = zero, continuous_ready = zero;
  ValueId choose_w = zero, issue = zero, reject_subword = zero;
  ValueId read_protection = zero, read_cache = zero;
  OutletBase::Orphan orphan;
  ValueId submask = zero, bus_address = zero, bus_mask = zero, bus_data = zero;
  ValueId hrdata = zero, complete = zero, wcomplete = zero, rcomplete = zero, bus_result = zero;

  void write_accept();
  void read_accept();
  void bus_issue();
  void bus_drive();
  void write_update();
  void read_update();
  void build();
  Result<NodeHardware> finish();
};

ApbOutlet::ApbOutlet(const BurstOutletProfile &profile, Protocol kind)
    : o(profile), p(profile), l(o.l), strobeless(kind == Protocol::Apb3 || kind == Protocol::Apb2),
      fixed_ready(kind == Protocol::Apb2), narrow_apb(p.apb_data_bits < 32),
      continuous(p.apb_back_to_back), narrow_strobeless(narrow_apb && strobeless),
      single(apb_outlet_form(p).single), at_once(apb_outlet_form(p).at_once),
      write_in_entry(apb_outlet_form(p).write_in_entry),
      packet_coded(apb_outlet_form(p).packet_coded), physical_lanes(p.apb_data_bits / 8),
      zero(o.zero), one(o.one), ok(o.ok), target_error(o.target_error), bits(o.bits),
      lanes(o.lanes), max_size(o.max_size), space(o.space), subpage(o.subpage), timeout(o.timeout),
      monitored(o.monitored), report_only(o.report_only), failed(o.failed) {}

Fields ApbOutlet::response_state(const char *stem) {
  Fields s;
  s["address"] = l.address_state(std::string(stem) + ".address", bits);
  // An entry loaded at the head shows the packet's running result, final once it is full.
  s["result"] = write_in_entry ? wc : l.state(std::string(stem) + ".result", 2);
  s["size"] = l.state(std::string(stem) + ".size", 3);
  if (p.id_bits)
    s["id"] = l.state(std::string(stem) + ".id", p.id_bits);
  if (p.source_bits)
    s["source"] = l.state(std::string(stem) + ".source", p.source_bits);
  if (p.tag_bits)
    s["tag"] = l.state(std::string(stem) + ".tag", p.tag_bits);
  if (monitored) {
    s["length"] = l.state(std::string(stem) + ".length", p.len_bits);
    s["burst"] = l.state(std::string(stem) + ".burst", 2);
    s["fail"] = l.state(std::string(stem) + ".fail", 1);
    s["exclusive_okay"] = l.state(std::string(stem) + ".exclusive_okay", 1);
  }
  return s;
}

Fields ApbOutlet::queue_meta(const char *stem) {
  Fields s;
  s["size"] = l.state(std::string(stem) + "_size", 3);
  if (p.id_bits)
    s["id"] = l.state(std::string(stem) + "_id", p.id_bits);
  if (p.source_bits)
    s["source"] = l.state(std::string(stem) + "_source", p.source_bits);
  if (p.tag_bits)
    s["tag"] = l.state(std::string(stem) + "_tag", p.tag_bits);
  if (monitored)
    s["exclusive"] = l.state(std::string(stem) + "_exclusive", 1);
  if (p.response_run_bits)
    s["run_end"] = l.state(std::string(stem) + "_run_end", 1);
  return s;
}

// The write side: the faces, the write buffer and response stage registers,
// acceptance, and the per-word local result with its exclusive and strobeless
// adjustments.
void ApbOutlet::write_accept() {
  w = o.request("wreq");
  r = o.request("rreq");
  wb = l.state("w.busy", 1);
  bf = l.state("w.response_full", 1);
  if (!at_once) {
    wf = l.state("w.full", 1);
    // Without PSTRB a full-width word is served whole or not at all: its lanes are all set or all
    // clear, so one bit holds them.
    wm = strobeless && !narrow_apb ? l.b.replicated(l.state("w.mask", 1), lanes)
                                   : l.state("w.mask", lanes);
  }
  wc = l.state("w.result", 2);
  // A completed packet's B is staged while the network takes it: a second slot holds the following
  // packet's, so the head of the next packet does not wait for the previous response to leave.
  btf = single ? zero : l.state("w.response_tail_full", 1);
  bq = response_state("w.response");                    // front
  bt = single ? bq : response_state("w.response_tail"); // tail
  // APB hands a whole word to the bus at issue: the word buffer then holds the next word while
  // w.flight marks the issued transfer until it completes or the target fails. Without a timer that
  // is a bus phase of a write.
  phase = l.state("bus.phase", 2);
  owner = l.state("bus.write", 1);
  wfl = single ? l.bind("w.flight", l.both(l.inv(l.eq(phase, 0)), owner)) : l.state("w.flight", 1);
  // Without back-to-back accesses the outlet serves one access at a time: a packet head waits
  // until every earlier response has been taken, a write word until the transfer before it
  // completed, and a read word until the response before it was taken. The back-to-back outlet
  // keeps its two response slots. apb.serial is its own node, so that a fault can give back the
  // decoupled outlet. Once the target has timed out it sees no new access, so nothing is held for
  // it, and the outlet answers reads within four cycles (AXI.TIMEOUT.EXACT).
  serial = continuous ? zero : l.bind("apb.serial", l.b.constant(1, 1));
  if (!continuous && timeout)
    serial = l.both(serial, l.inv(failed));
  // The serial outlet reads the read side's busy and full flags here, so it creates them first;
  // the back-to-back outlet creates them with the rest of the read side.
  if (!continuous) {
    rb = l.state("r.busy", 1);
    rf = l.state("r.full", 1);
  }
  auto wr_ready = at_once ? l.inv(l.both(bf, btf)) : l.both(l.inv(wf), l.inv(l.both(bf, btf)));
  if (!continuous)
    wr_ready = l.both(wr_ready,
                      l.inv(l.both(serial, l.b.select(wb, wfl, l.either(l.either(rb, rf), bf)))));
  wr_ready = active(wr_ready);
  out("wreq", "ready", wr_ready);
  wt = l.bind("w.accept", l.both(w.at("valid"), wr_ready));
  wh = l.both(wt, l.inv(wb));
  wd = write_in_entry ? Fields{} : o.capture("w.descriptor", w, wh);
  wdata = l.latch("w.data", w.at("data"), wt);
  wl = l.latch("w.last", w.at("last"), wt);
  // A word accepted into an empty buffer is worked on in the cycle it arrives, as if it were
  // already buffered (the registers still load it), so the buffer adds no cycle before SETUP.
  // The *_now values are the buffer as the engine sees it in this cycle.
  wf_now = at_once ? wt : l.either(wf, wt);
  wb_now = l.either(wb, wh);
  wa_now = at_once ? w.at("address")
                   : l.b.select(wt, w.at("address"), l.latch("w.address", w.at("address"), wt));
  wdata_now = l.b.select(wt, w.at("data"), wdata);
  wl_now = l.b.select(wt, w.at("last"), wl);
  if (!write_in_entry)
    wd_now = view_fields(l, wd, w, wh);
  const auto window_code = o.local_result(w);
  wcode = window_code;
  // Network exclusive monitor: a failing write (judged at its packet head
  // and held for the packet) is served with no strobes.
  if (monitored) {
    failing = o.watch(w);
    w_fail = l.latch("w.exclusive_fail", failing, wh);
    w_exclusive = l.b.select(wh, w.at("lock"), l.latch("w.exclusive", w.at("lock"), wh));
    fail_here = l.b.select(wh, failing, w_fail);
  }
  // Without PSTRB reject only an unrepresentable word. Later words still execute,
  // and the packet result accumulates the error. Zero strobes require no access.
  if (strobeless && !narrow_apb) {
    const auto partial = l.both(l.inv(l.eq(w.at("be"), 0)), l.inv(l.eq(w.at("be"), 15)));
    const auto checked = monitored ? l.both(partial, l.inv(fail_here)) : partial;
    wcode = l.b.select(l.both(checked, l.eq(wcode, 0)), target_error, wcode);
  }
  // A failed exclusive write answers OKAY, a target timeout included.
  wcode = timeout && monitored ? l.b.select(fail_here, wcode, isolate(wcode)) : isolate(wcode);
  auto served = l.eq(wcode, 0);
  // A failed exclusive write is not served at all on APB: no transfer, OKAY.
  auto strobed = served;
  if (monitored) {
    strobed = l.both(served, l.inv(fail_here));
    served = strobed;
  }
  wmask = l.b.select(strobed, w.at("be"), l.b.constant(lanes, 0));
  // An empty word still owns a serial service slot. The sequencer retires it without selecting
  // the peripheral, so later words and the packet's response cannot bypass it.
  if (!continuous) {
    write_service = l.both(wt, strobed);
    local_word = l.both(wt, l.inv(strobed));
  }
  wm_now = at_once ? wmask : l.b.select(wt, wmask, wm);
  if (!at_once) {
    // Every served serial word issues, including an empty one. Only an unserved
    // arrival can finish locally. Otherwise completion tests the held mask.
    finish_w =
        active(continuous ? l.both(wf_now, l.eq(wm_now, 0))
                          : l.either(local_word, l.both(l.inv(wt), l.both(wf, l.eq(wm, 0)))));
  }
  // The packet's last word stays buffered until its transfer completes so that the response merges
  // it; the completion edge itself frees it. The packet's last word's buffer slot frees at its
  // completion edge; the snapshot queue below decouples it from the network's B acceptance.
}

// The read side: descriptor capture and acceptance, the offered-response
// registers and the two-entry response queue, then the next-beat values.
void ApbOutlet::read_accept() {
  if (continuous) {
    rb = l.state("r.busy", 1);
    rf = l.state("r.full", 1);
  }
  if (!at_once)
    rm = l.state("r.mask", lanes);
  if (!packet_coded)
    rc = l.state("r.result", 2);
  ra = l.address_state("r.address", bits);
  rn = l.state("r.remaining", p.len_bits);
  rfirst = l.state("r.first", 1);
  rdata = l.state("r.data", o.word_bits);
  gathered = narrow_apb ? l.state("r.gather", 32) : zero;
  // APB keeps the offered response in its own registers (r.full, r.data and these), so the current
  // word advances when its transfer completes instead of when its response is taken. The response
  // entry also carries the packet's descriptor fields, since the next packet may be accepted while
  // its response still waits.
  oa = l.address_state("r.response_address", bits);
  oc = l.state("r.response_result", 2);
  ofirst = l.state("r.response_first", 1);
  olast = l.state("r.response_last", 1);
  // The read response queue keeps two entries, so a completing word pushes
  // its response while the previous one still waits for the network.
  if (!single) {
    tail_full = l.state("r.tail_full", 1);
    tail_data = l.state("r.tail_data", 32);
    tail_address = l.address_state("r.tail_address", bits);
    tail_result = l.state("r.tail_result", 2);
    tail_first = l.state("r.tail_first", 1);
    tail_last = l.state("r.tail_last", 1);
  }
  om = queue_meta("r.response");                 // front
  otm = single ? om : queue_meta("r.tail_meta"); // tail
  used2 = l.plus(l.b.unary(Op::ZeroExtend, rf, 2), l.b.unary(Op::ZeroExtend, tail_full, 2));
  full2 = l.eq(used2, 2);
  auto rr_ready = l.both(l.inv(rb), l.inv(full2));
  if (!continuous)
    // Two heads offered together enter one at a time, the write first (as on the bus).
    rr_ready = l.both(
        rr_ready, l.inv(l.both(serial, l.either(l.either(wb, bf), l.either(rf, w.at("valid"))))));
  rr_ready = active(rr_ready);
  out("rreq", "ready", rr_ready);
  rt = l.bind("r.accept", l.both(r.at("valid"), rr_ready));
  Fields rd;
  if (single) {
    // The next head waits for the last response to leave, so the response entry holds the packet's
    // fields from the head on, and a word's address carries the WRAP base above its container.
    for (const auto *name : {"length", "burst"})
      rd[name] = l.latch(std::string("r.descriptor.") + name, r.at(name), rt);
    if (!packet_coded)
      for (const auto *name : {"cache", "protection"})
        rd[name] = l.latch(std::string("r.descriptor.") + name, r.at(name), rt);
  } else
    rd = o.capture("r.descriptor", r, rt);
  // As on the write side, a read packet accepted now is worked on in this cycle.
  rd_now = view_fields(l, rd, r, rt);
  if (single)
    for (const auto *name : {"size", "id", "source", "tag"})
      if (om.contains(name)) {
        rd_now[name] = l.b.select(rt, r.at(name), om.at(name));
        l.b.update(om.at(name), rd_now.at(name));
      }
  // An exclusive read is contaminated by the write in flight (from its head until its B leaves); a
  // failed exclusive write never reaches the target: it neither contaminates nor clears.
  if (monitored) {
    r_exclusive = l.b.select(rt, r.at("lock"), l.latch("r.exclusive", r.at("lock"), rt));
    const auto reaching = l.both(wh, l.inv(failing));
    o.reserve(w, r, rt, reaching, reaching, {{wb, w_fail, &wd}}, true);
  }
  const auto started = o.local_result(r);
  initial_code = l.b.select(rt, started, l.latch("r.initial_result", started, rt));
  initial_mask = l.b.select(l.eq(isolate(started), 0), o.read_mask(r.at("address"), r.at("size")),
                            l.b.constant(lanes, 0));
  rb_now = l.either(rb, rt);
  rfirst_now = l.either(rfirst, rt);
  ra_now = l.b.select(rt, r.at("address"), ra);
  rn_now = l.b.select(rt, r.at("length"), rn);
  rc_now = packet_coded ? initial_code : l.b.select(rt, isolate(started), rc);
  // A word's lanes while no transfer has cleared them.
  if (at_once)
    rm = l.b.select(l.eq(packet_coded ? initial_code : rc, 0), o.read_mask(ra, rd_now.at("size")),
                    l.b.constant(lanes, 0));
  rm_now = l.b.select(rt, initial_mask, rm);
  last = l.bind("r.last", l.b.select(rt, l.eq(r.at("length"), 0), l.eq(rn, 0)));
  rpop = l.both(active(rf), in("rrsp", "ready", 1));
  // A word advances when its response enters the queue.
  rtake = rpush;
  // The response queue's two entries are full now or freed at this edge. APB
  // cannot stall a completion, so a read is issued only with room: no other
  // response can arrive before its transfer completes.
  room = l.either(l.inv(full2), rpop);
  // One access at a time: the next read word waits for the previous response to be taken.
  if (!continuous)
    room = l.both(room, l.inv(l.both(serial, l.both(rf, l.inv(rpop)))));
  l.bind("r.room", room);
  auto walking = rd_now;
  if (single)
    walking["address"] = ra_now;
  next_ra = next_address(l, ra_now, walking, bits, max_size);
  next_code = o.next_code(initial_code, next_ra, r, rt);
}

// APB SETUP/ACCESS run on an idle/phase1/phase2 sequencer; SETUP advances independently of
// PREADY. One transfer is on the bus at a time and the next is issued only after the current one
// completes (optionally at that edge). APB overlaps word handling with its transfer: the next
// write word is taken while the current one is on the bus, and the next read word starts at the
// completion of the current one, so a word takes an idle, a SETUP and an ACCESS cycle plus PREADY
// wait states.
void ApbOutlet::bus_issue() {
  const auto flow_bits = static_cast<unsigned>(std::bit_width(p.flows - 1));
  if (p.flows > 1)
    bus_flow = l.state("bus.flow", flow_bits);
  const auto word_owes = continuous ? l.both(wt, l.inv(l.eq(wmask, 0))) : write_service;
  const auto wcan = at_once ? word_owes : l.either(l.both(wf, owes()), word_owes);
  auto issue_phase = zero;
  auto read_address = ra_now, read_mask = rm_now, read_room = room;
  // A narrow continuous read needs the physical word in flight before issue.
  const auto early_mask = narrow_apb && continuous ? l.state("bus.mask", 4) : zero;
  if (continuous) {
    continuous_ready = fixed_ready ? one : in("apb", "pready", 1);
    const auto completing = active(l.both(l.eq(phase, 2), continuous_ready));
    const auto completing_read = l.both(completing, l.inv(owner));
    issue_phase = l.either(l.eq(phase, 0), completing);
    // A narrow beat ends with its last physical word; the others leave the
    // beat's remaining words to the next transfer.
    auto ending_read = completing_read;
    auto rest = rm_now;
    if (narrow_apb) {
      rest = l.b.binary(Op::BitAnd, rm_now, l.b.unary(Op::BitNot, early_mask, 4));
      ending_read = l.both(completing_read, l.eq(rest, 0));
    }
    // Completion fills a response slot at this edge. Reserve another slot
    // before starting a read: its future APB completion cannot be stalled.
    const auto after_push_room =
        l.below(l.minus(l.plus(used2, l.b.constant(2, 1)), l.b.unary(Op::ZeroExtend, rpop, 2)),
                l.b.constant(2, 2));
    read_room = l.b.select(ending_read, after_push_room, room);
    auto following_mask = l.b.select(l.eq(next_code, 0), o.read_mask(next_ra, rd_now.at("size")),
                                     l.b.constant(lanes, 0));
    following_mask = l.b.select(last, l.b.constant(lanes, 0), following_mask);
    read_address = l.b.select(ending_read, next_ra, ra_now);
    read_mask = l.b.select(ending_read, following_mask,
                           narrow_apb ? l.b.select(completing_read, rest, rm_now) : rm_now);
  }
  // A packet taken now may issue its first word at once.
  const auto read_owes = continuous ? l.inv(l.eq(read_mask, 0)) : l.inv(l.eq(rm, 0));
  auto rcan = l.either(l.both(rb, l.both(continuous ? read_room : room, read_owes)),
                       l.both(rt, l.both(continuous ? read_room : room,
                                         l.inv(l.eq(continuous ? read_mask : initial_mask, 0)))));
  // The network delivers one direction at a time (arrival order): a write
  // word goes first only when both are offered on their own.
  choose_w = wcan;
  auto can_issue = l.either(wcan, rcan);
  // After expiry the read packet already held keeps reaching the target: the orphan issues its
  // remaining transfers from IDLE and their data is discarded; nothing else starts.
  read_protection = packet_coded ? r.at("protection") : rd_now.at("protection");
  read_cache = packet_coded ? r.at("cache") : rd_now.at("cache");
  if (timeout) {
    orphan = o.orphan({{"address", bits}, {"left", p.len_bits}, {"mask", lanes}}, rd_now,
                      OutletBase::captured());
    read_address = l.b.select(failed, orphan.word.at("address"), read_address);
    read_mask = l.b.select(failed, orphan.word.at("mask"), read_mask);
    read_protection = l.b.select(failed, orphan.packet.at("protection"), read_protection);
    read_cache = l.b.select(failed, orphan.packet.at("cache"), read_cache);
    choose_w = l.both(choose_w, l.inv(failed));
    can_issue = l.b.select(failed, l.both(l.inv(l.eq(orphan.word.at("mask"), 0)), l.eq(phase, 0)),
                           can_issue);
  }
  issue = narrow_strobeless
              ? zero
              : l.bind("bus.issue",
                       active(l.both(continuous ? issue_phase : l.eq(phase, 0), can_issue)));
  auto mask = l.b.select(choose_w, wm_now, read_mask);
  ValueId address = l.b.binary(Op::BitAnd, l.b.select(choose_w, wa_now, read_address),
                               l.b.constant(bits, space - lanes));
  submask = mask;
  if (narrow_apb) {
    // Lowest enabled physical word, independently for the two directions.
    auto offset = l.b.constant(bits, 0);
    submask = l.b.constant(4, 0);
    for (unsigned group = lanes / physical_lanes; group-- > 0;) {
      const unsigned lane = group * physical_lanes;
      const auto selected = l.inv(l.eq(l.b.slice(mask, lane, physical_lanes), 0));
      const auto group_mask =
          l.b.binary(Op::BitAnd, mask, l.b.constant(4, ((1U << physical_lanes) - 1) << lane));
      offset = l.b.select(selected, l.b.constant(bits, lane), offset);
      submask = l.b.select(selected, group_mask, submask);
    }
    address = l.plus(address, offset);
  }
  if (narrow_strobeless) {
    // Judge the next physical word only after earlier words have completed.
    // A partial word makes no access; earlier side effects are not rolled back.
    auto partial = zero;
    for (unsigned lane = 0; lane < lanes; lane += physical_lanes) {
      const auto group = l.b.slice(submask, lane, physical_lanes);
      partial = l.either(
          partial, l.both(l.inv(l.eq(group, 0)), l.inv(l.eq(group, (1U << physical_lanes) - 1))));
    }
    const auto attempt = active(l.both(continuous ? issue_phase : l.eq(phase, 0), can_issue));
    const auto invalid = l.both(choose_w, partial);
    reject_subword = l.both(attempt, invalid);
    issue = l.bind("bus.issue", l.both(attempt, l.inv(invalid)));
  }
  // The transfer's flow: that of the mapping holding its global address.
  if (p.flows > 1)
    l.b.update(
        bus_flow,
        l.b.select(issue, mapping_decode::flow(l, address, bits, p.mappings, flow_bits), bus_flow));
  // The base subtraction and the port's narrowing come down to the stored bits of PADDR.
  const auto port_bits = std::min(p.address_bits, bits);
  if (const auto kept = apb_bus_address_bits(p); kept < port_bits) {
    const auto stored = l.state("bus.address", kept);
    bus_address = l.b.widened(stored, l.b.constant(port_bits - kept, 0));
    l.b.update(stored, l.b.select(issue, o.localized(address, kept), stored));
  } else {
    address = o.localized(address);
    // Narrow only after the full-width decode and base subtraction.
    if (p.address_bits < bits)
      address = l.b.slice(address, 0, p.address_bits);
    bus_address = l.latch("bus.address", address, issue);
  }
  bus_mask = early_mask;
  if (narrow_apb && continuous)
    l.b.update(bus_mask, l.b.select(issue, submask, bus_mask));
  else
    bus_mask = l.latch("bus.mask", submask, issue);
  // A serial outlet takes no write word while a transfer is on the bus, so PWDATA is the word
  // buffer itself during a write transfer.
  bus_data = single ? wdata : l.latch("bus.data", wdata_now, issue);
}

// The bus registers drive the APB faces; a completion merges into the waiting
// word and, after expiry, the orphan steps its remaining transfers.
void ApbOutlet::bus_drive() {
  ValueId bus_prot;
  if (!strobeless) {
    // Every fragment repeats its packet's descriptor (message.hpp point 2), and a write word issues
    // in the cycle it is taken.
    const auto &written = write_in_entry ? w : wd_now;
    auto protection = l.b.select(choose_w, written.at("protection"), read_protection);
    if (p.attributes) {
      const auto cache = l.b.select(choose_w, written.at("cache"), read_cache);
      protection = map_attributes(l, p.attributes->prot, cache, protection, choose_w);
    }
    // A packet-coded read's later words keep the attributes its first word put on the bus.
    bus_prot = l.latch("bus.protection", protection,
                       packet_coded ? l.both(issue, l.either(choose_w, rt)) : issue);
  }
  const auto bus_ready = continuous ? continuous_ready : fixed_ready ? one : in("apb", "pready", 1);
  const auto empty = continuous ? zero : l.eq(bus_mask, 0);
  const auto ready = continuous ? bus_ready : l.either(empty, bus_ready);
  hrdata = in("apb", "prdata", p.apb_data_bits);
  const auto hresp = fixed_ready ? zero : in("apb", "pslverr", 1);
  const auto address_done = active(l.eq(phase, 1));
  complete = l.bind("bus.complete", active(l.both(l.eq(phase, 2), ready)));
  wcomplete = l.both(complete, owner);
  rcomplete = l.both(complete, l.inv(owner));
  bus_result = l.b.select(continuous ? hresp : l.both(hresp, l.inv(empty)), target_error, ok);
  out("apb", "paddr", bus_address);
  if (!strobeless)
    out("apb", "pprot", bus_prot);
  out("apb", "pwrite", owner);
  const auto phase_selected = l.inv(l.eq(phase, 0)), phase_enabled = l.eq(phase, 2);
  out("apb", "psel", active(continuous ? phase_selected : l.both(l.inv(empty), phase_selected)));
  out("apb", "penable", active(continuous ? phase_enabled : l.both(l.inv(empty), phase_enabled)));
  if (narrow_apb) {
    auto physical_data = l.b.slice(bus_data, 0, p.apb_data_bits);
    auto physical_mask = l.b.slice(bus_mask, 0, physical_lanes);
    for (unsigned lane = physical_lanes; lane < lanes; lane += physical_lanes) {
      const auto selected = l.inv(l.eq(l.b.slice(bus_mask, lane, physical_lanes), 0));
      physical_data =
          l.b.select(selected, l.b.slice(bus_data, lane * 8, p.apb_data_bits), physical_data);
      physical_mask =
          l.b.select(selected, l.b.slice(bus_mask, lane, physical_lanes), physical_mask);
    }
    out("apb", "pwdata", physical_data);
    if (!strobeless)
      out("apb", "pstrb", l.b.select(owner, physical_mask, l.b.constant(physical_lanes, 0)));
  } else {
    out("apb", "pwdata", bus_data);
    if (!strobeless)
      out("apb", "pstrb", l.b.select(owner, bus_mask, l.b.constant(4, 0)));
  }
  if (timeout) {
    o.watchdog(l.either(l.either(w.at("valid"), r.at("valid")), l.either(wb, rb)),
               l.either(bf, rf));
    // A late completion is accepted and discarded.
    wcomplete = l.both(wcomplete, l.inv(failed));
    rcomplete = l.both(rcomplete, l.inv(failed));
    // The orphan owes the current beat's lanes not on the bus at expiry and the later beats of a
    // packet that makes accesses; a beat whose lanes are done steps on once the bus is idle.
    const auto left = orphan.word.at("left"), lanes_owed = orphan.word.at("mask");
    const auto spare = [&](ValueId value) {
      return l.b.binary(Op::BitAnd, value, l.b.unary(Op::BitNot, bus_mask, lanes));
    };
    const auto owing =
        l.both(rb_now, subpage ? l.inv(l.eq(initial_code, 2)) : l.eq(initial_code, 0));
    const auto rest =
        l.b.select(l.both(l.inv(l.eq(phase, 0)), l.inv(owner)), spare(rm_now), rm_now);
    const auto step = l.both(l.both(orphan.taken, l.eq(phase, 0)),
                             l.both(l.eq(lanes_owed, 0), l.inv(l.eq(left, 0))));
    const auto following =
        next_address(l, orphan.word.at("address"), orphan.packet, bits, max_size);
    auto lanes_next = o.read_mask(following, orphan.packet.at("size"));
    if (subpage)
      lanes_next = l.b.select(o.mapped(following), lanes_next, l.b.constant(lanes, 0));
    o.move(orphan, "address", ra_now, step, following);
    o.move(orphan, "left", l.b.select(owing, rn_now, l.b.constant(p.len_bits, 0)), step,
           l.dec(left));
    o.move(orphan, "mask", l.b.select(owing, rest, l.b.constant(lanes, 0)), step, lanes_next,
           l.b.select(l.both(complete, l.inv(owner)), spare(lanes_owed), lanes_owed));
  }
  l.b.update(phase, l.b.select(issue, l.b.constant(2, 1),
                               l.b.select(address_done, l.b.constant(2, 2),
                                          l.b.select(complete, ok, phase))));
  l.b.update(owner, l.b.select(issue, choose_w, owner));
}

// Write completion: a word leaves when its transfer completes (or at once
// without one), the packet's B merges the per-word results into the two-entry
// response stage, and the buffer slot frees at the completion edge.
void ApbOutlet::write_update() {
  // The packet's last word stays buffered until its transfer completes so the
  // response merges it; the completion edge itself frees the buffer slot.
  if (!at_once) {
    finish_w = l.both(finish_w, l.inv(l.both(l.both(wl, wfl), l.inv(wcomplete))));
    // A last word taken now waits likewise for a word still in flight.
    finish_w =
        l.both(finish_w, l.inv(l.both(l.both(l.both(wt, w.at("last")), wfl), l.inv(wcomplete))));
  }
  // At once: the completion of a packet's last word, or a last word that owes no transfer.
  const auto bpush = at_once
                         ? l.either(l.bind("w.last_done", l.both(wl, wcomplete)),
                                    continuous ? l.both(l.both(wt, w.at("last")), l.eq(wmask, 0))
                                               : l.both(local_word, w.at("last")))
                         : l.both(finish_w, wl_now);
  // A silent B is dropped here and ends its packet at once.
  const auto bstore = report_only ? l.both(bpush, l.inv(failed)) : bpush;
  // The response stage pops when the network takes the front entry.
  const auto bpop = l.both(active(bf), in("wrsp", "ready", 1));
  // One transfer carries the whole word: issue clears the mask and marks the flight. A word taken
  // while the previous one is on the bus merges its window result in the same cycle as that
  // transfer's result; nothing later than the last word of a packet is taken before its response.
  const auto w_issue = l.both(issue, choose_w);
  const auto after_issue = narrow_apb
                               ? l.b.binary(Op::BitAnd, wm_now, l.b.unary(Op::BitNot, submask, 4))
                               : l.b.constant(4, 0);
  auto next_wm = at_once ? zero
                         : l.b.select(wt, l.b.select(w_issue, after_issue, wmask),
                                      l.b.select(w_issue, after_issue, wm));
  auto taken = l.b.select(wt, l.b.select(wh, wcode, merge_error(l, wc, wcode)), wc);
  if (narrow_strobeless) {
    next_wm =
        l.b.select(reject_subword,
                   l.b.binary(Op::BitAnd, wm_now, l.b.unary(Op::BitNot, submask, lanes)), next_wm);
    taken = l.b.select(reject_subword, merge_error(l, taken, target_error), taken);
  }
  if (timeout) {
    next_wm = l.b.select(failed, l.b.constant(4, 0), next_wm);
    // The transfer the target left waiting and a word not yet issued fail.
    const auto dropped = l.both(failed, l.either(l.inv(l.eq(wm_now, 0)), wfl));
    taken = l.b.select(dropped, merge_error(l, taken, target_error), taken);
  }
  // The write packet's result including a completion at this edge.
  const auto wcode_final = l.b.select(wcomplete, merge_error(l, taken, bus_result), taken);
  l.b.update(wb, l.b.select(bpush, zero, wb_now));
  l.b.update(bf, l.either(l.either(l.both(bf, l.inv(bpop)), btf), bstore));
  if (!single)
    l.b.update(btf, l.either(l.both(btf, l.inv(bpop)),
                             l.both(bstore, l.both(bf, l.either(l.inv(bpop), btf)))));
  {
    const auto to_tail = l.both(bstore, l.both(bf, l.either(l.inv(bpop), btf)));
    const auto to_front = l.both(bstore, l.either(l.inv(bf), l.both(bpop, l.inv(btf))));
    const auto shift = l.both(bpop, btf);
    const auto stage = [&](const char *field, ValueId value) {
      if (write_in_entry)
        return;
      const auto loaded = l.b.select(to_front, value, bq.at(field));
      if (single) {
        l.b.update(bq.at(field), loaded);
        return;
      }
      l.b.update(bq.at(field), l.b.select(shift, bt.at(field), loaded));
      l.b.update(bt.at(field), l.b.select(to_tail, value, bt.at(field)));
    };
    if (write_in_entry) {
      for (const auto &[field, entry] : bq)
        if (field != "result")
          l.b.update(entry, l.b.select(wh, w.at(field), entry));
    } else {
      stage("address", wd_now.at("address"));
      stage("result", wcode_final);
      stage("size", wd_now.at("size"));
      if (p.id_bits)
        stage("id", wd_now.at("id"));
      if (p.source_bits)
        stage("source", wd_now.at("source"));
      if (p.tag_bits)
        stage("tag", wd_now.at("tag"));
    }
    if (monitored) {
      stage("length", wd_now.at("length"));
      stage("burst", wd_now.at("burst"));
      stage("fail", fail_here);
      stage("exclusive_okay", l.both(w_exclusive, l.both(l.inv(fail_here), l.eq(wcode_final, 0))));
    }
  }
  o.response("wrsp", bq, bf, bq.at("address"), bq.at("result"), one, one,
             l.b.constant(o.word_bits, 0), monitored ? bq.at("exclusive_okay") : zero);
  if (continuous) {
    // The bus registers own an issued non-tail word. Free its input slot
    // at issue, so SETUP can accept the following word before ACCESS ends;
    // a narrow word leaves with its last physical word.
    auto issued = l.both(w_issue, l.inv(wl_now));
    if (narrow_apb)
      issued = l.both(issued, l.eq(after_issue, 0));
    const auto leaves = l.either(finish_w, issued);
    l.b.update(wf, l.b.select(leaves, zero, wf_now));
  }
  const auto ends = timeout ? l.either(complete, failed) : complete;
  if (!single)
    l.b.update(wfl, l.b.select(w_issue, one, l.b.select(ends, zero, wfl)));
  if (!at_once)
    l.b.update(wm, next_wm);
  l.b.update(wc, l.b.select(wcomplete, merge_error(l, taken, bus_result), taken));
  if (!continuous && !at_once)
    l.b.update(wf, l.b.select(finish_w, zero, wf_now));
}

// Read completion: a word leaves as a response when its transfer completes,
// or at once when it needs no transfer; either way the next word starts then.
void ApbOutlet::read_update() {
  const auto skip = active(l.both(rb_now, l.both(l.eq(rm_now, 0), room)));
  auto word_complete = rcomplete;
  if (narrow_apb)
    word_complete = l.both(
        rcomplete, l.eq(l.b.binary(Op::BitAnd, rm_now, l.b.unary(Op::BitNot, bus_mask, 4)), 0));
  rpush = l.either(word_complete, skip);
  rnext = l.both(rpush, l.inv(last));
  rtake = rpush;
  l.bind("r.push", rpush);
  l.bind("r.pop", rpop);
  l.bind("r.skip", skip);
  l.bind("bus.read_issue", l.both(issue, l.inv(choose_w)));
  // A silent response is dropped here; the packet still advances.
  const auto rstore = report_only ? l.both(rpush, l.inv(failed)) : rpush;
  l.b.update(rb, l.b.select(l.both(rtake, last), zero, rb_now));
  l.b.update(rf, l.either(l.either(l.both(rf, l.inv(rpop)), tail_full), rstore));
  {
    const auto to_tail = l.both(rstore, l.both(rf, l.either(l.inv(rpop), tail_full)));
    if (!single)
      l.b.update(tail_full, l.either(l.both(tail_full, l.inv(rpop)), to_tail));
  }
  if (!at_once) {
    const auto cleared = l.b.binary(Op::BitAnd, rm_now, l.b.unary(Op::BitNot, bus_mask, lanes));
    const auto next_mask = l.b.select(l.eq(next_code, 0), o.read_mask(next_ra, rd_now.at("size")),
                                      l.b.constant(lanes, 0));
    auto next_rm = l.b.select(rnext, next_mask, l.b.select(rcomplete, cleared, rm_now));
    if (timeout)
      next_rm = l.b.select(failed, l.b.constant(lanes, 0), next_rm);
    const auto stored_mask = rm;
    l.b.update(stored_mask, next_rm);
  }
  l.b.update(ra, l.b.select(rnext, next_ra, ra_now));
  l.b.update(rn, l.b.select(rnext, l.dec(rn_now), rn_now));
  l.b.update(rfirst, l.b.select(rtake, zero, rfirst_now));
  if (timeout) {
    const auto dropped = l.both(failed, l.inv(l.eq(rm_now, 0)));
    l.b.update(rc, l.b.select(rnext, next_code,
                              l.b.select(dropped, merge_error(l, rc_now, target_error),
                                         l.b.select(rcomplete, merge_error(l, rc_now, bus_result),
                                                    rc_now))));
  } else if (!packet_coded) {
    l.b.update(rc, l.b.select(rnext, next_code,
                              l.b.select(rcomplete, merge_error(l, rc_now, bus_result), rc_now)));
  }
  // The transfer returns the whole word: only the requested lanes are kept.
  std::vector<ValueId> bytes;
  for (unsigned i = 0; i < 4; ++i)
    bytes.push_back(l.b.select(l.b.slice(bus_mask, i, 1),
                               l.b.slice(hrdata, (narrow_apb ? i % physical_lanes : i) * 8, 8),
                               l.b.constant(8, 0)));
  auto word = l.b.concat(l.b.concat(bytes[3], bytes[2]), l.b.concat(bytes[1], bytes[0]));
  if (narrow_apb) {
    word = l.b.binary(Op::BitOr, gathered, word);
    // Each group is visited once. The previous offered response is independent.
    l.b.update(gathered, l.b.select(l.either(rt, rnext), l.b.constant(32, 0),
                                    l.b.select(rcomplete, word, gathered)));
  }
  const auto to_tail = l.both(rstore, l.both(rf, l.either(l.inv(rpop), tail_full)));
  const auto to_front = l.both(rstore, l.either(l.inv(rf), l.both(rpop, l.inv(tail_full))));
  const auto shift = l.both(rpop, tail_full);
  const auto queue = [&](ValueId front, ValueId tail, ValueId value) {
    const auto loaded = l.b.select(to_front, value, front);
    if (single) {
      l.b.update(front, loaded);
      return;
    }
    l.b.update(front, l.b.select(shift, tail, loaded));
    l.b.update(tail, l.b.select(to_tail, value, tail));
  };
  queue(rdata, tail_data, l.b.select(rcomplete, word, l.b.constant(32, 0)));
  queue(oa, tail_address, ra_now);
  queue(oc, tail_result, l.b.select(rcomplete, merge_error(l, rc_now, bus_result), rc_now));
  queue(ofirst, tail_first, rfirst_now);
  queue(olast, tail_last, last);
  // The entry carries the packet's descriptor fields: the next packet may
  // be accepted while this response still waits.
  if (!single) {
    queue(om.at("size"), otm.at("size"), rd_now.at("size"));
    if (p.id_bits)
      queue(om.at("id"), otm.at("id"), rd_now.at("id"));
    if (p.source_bits)
      queue(om.at("source"), otm.at("source"), rd_now.at("source"));
    if (p.tag_bits)
      queue(om.at("tag"), otm.at("tag"), rd_now.at("tag"));
  }
  if (monitored)
    queue(om.at("exclusive"), otm.at("exclusive"), r_exclusive);
  if (p.response_run_bits)
    queue(om.at("run_end"), otm.at("run_end"),
          burst_run_end(l, rd_now, ra_now, o.word_bits, p.response_run_bits));
  o.response("rrsp", om, rf, oa, oc, ofirst, olast,
             l.b.select(l.eq(oc, 0), rdata, l.b.constant(32, 0)),
             monitored ? l.both(om.at("exclusive"), l.eq(oc, 0)) : zero);
}

void ApbOutlet::build() {
  write_accept();
  read_accept();
  bus_issue();
  bus_drive();
  write_update();
  read_update();
}

Result<NodeHardware> ApbOutlet::finish() { return l.finish("nocloom_apb_burst_outlet_" + p.name); }

// APB: one transfer on the bus at a time.
Result<NodeHardware> lower_burst_outlet(const BurstOutletProfile &p, Protocol kind) {
  if (auto bad = profile_error(p, kind))
    return Result<NodeHardware>::failure(*bad);
  ApbOutlet engine(p, kind);
  engine.build();
  return engine.finish();
}

// AXI4-Lite: one single-beat transaction per addressed word, pipelined. The words of a burst issue
// back to back without waiting for B or R (AW and W of one word complete independently before the
// next word loads); up to max_outstanding packets of one direction stay in flight, and a direction
// change waits until every earlier packet has been answered. A write packet answers one B once its
// last word is in and the B of every word it issued has returned, with the BUSER of its first B;
// reads forward one R beat per word. A word the outlet answers itself (window miss, rejected FIXED
// or denied beat, failed target) makes no access and joins its packet's tally (one per packet in
// flight), which keeps the first failed word in burst order for the error_codes packet rules.
// After a timeout the held read packet finishes its transfers from a copy taken in the first failed
// cycle; its data is discarded.

// A one-entry backward stage of named fields: the face is the entry, else the push passing the
// empty entry straight through in its own cycle (a push the face does not take enters it). The
// entry takes a push only while empty, so its room is a register.
struct OneEntry {
  ValueId full{};
  Fields main;
};
// The shape keys present in src, copied.
Fields pick(const Fields &src, std::initializer_list<const char *> names) {
  Fields out;
  for (const auto *f : names)
    if (src.contains(f))
      out[f] = src.at(f);
  return out;
}
OneEntry one_entry(logic::Logic &l, const std::string &name, const Fields &shape) {
  OneEntry s{l.state(name + ".full", 1), {}};
  for (const auto &[f, v] : shape)
    s.main[f] = l.state_like(name + "0." + f, v);
  return s;
}
// Returns the face fields; valid is the face valid.
Fields one_entry_face(logic::Logic &l, const OneEntry &s, const Fields &value, ValueId push,
                      ValueId ready, ValueId &valid) {
  valid = l.either(s.full, push);
  const auto keep = l.both(push, l.inv(ready));
  l.b.update(s.full, l.b.select(keep, l.bit(1), l.b.select(ready, l.bit(0), s.full)));
  Fields face;
  for (const auto &[f, reg] : s.main) {
    l.b.update(reg, l.b.select(keep, value.at(f), reg));
    face[f] = l.b.select(s.full, reg, value.at(f));
  }
  return face;
}

// The engine, split into its write and read issue and response sides so that
// every builder function stays small; the sides share the packet tables.
struct LiteOutlet {
  explicit LiteOutlet(const BurstOutletProfile &profile)
      : o(profile), p(profile), l(o.l), zero(o.zero), one(o.one), ok(o.ok),
        target_error(o.target_error), net_miss(l.b.constant(2, 1)), timeout(o.timeout),
        codes(p.error_codes), report_only(o.report_only), monitored(o.monitored),
        cap(std::max(1U, p.max_outstanding)),
        pw(std::max(1U, static_cast<unsigned>(std::bit_width(cap - 1)))),
        cw(std::max(1U, static_cast<unsigned>(std::bit_width(cap)))), kw(p.len_bits + 1),
        failed(o.failed) {}
  OutletBase o;
  const BurstOutletProfile &p;
  logic::Logic &l;
  const ValueId zero, one, ok, target_error, net_miss;
  const unsigned timeout;
  const bool codes, report_only, monitored;
  // kw counts the words of one packet (at most 2^len_bits).
  const unsigned cap, pw, cw, kw;
  const ValueId failed;
  Fields w, r;
  // Write side state. Phase-assigned handles start at a defined value.
  ValueId failing = zero, wcode = zero, face_fail = zero, fail_tail = zero;
  packet_context::Table wpkt{};
  // Per packet slot of wpkt, its completion tally: the words that reached the bus (reals), the
  // local answer of the words that did not (code) and, with error codes, whether the first of them
  // came after a real (after).
  std::vector<Fields> tally;
  OneEntry wrsp;
  // The response faces' valids: a staged response, or one passing an empty stage.
  ValueId wrsp_valid = zero, rrsp_valid = zero;
  ValueId wt = zero, wpkt_push = zero, load = zero, bus_done = zero, w_queued = zero;
  ValueId bus_user{};
  ValueId bus_open = zero, aw_done = zero, w_done = zero, bus_address = zero, bus_prot = zero,
          bus_data = zero, bus_mask = zero;
  ValueId w_open = zero, w_bus_done = zero;
  Fields pkt_head, wentry;
  // Read side state.
  packet_context::Table rpkt{};
  ValueId r_run_end{};
  ValueId r_issue = zero, r_unissued = zero, r_issuing = zero, r_issue_addr = zero,
          r_issue_left = zero, rt = zero;
  ValueId r_on = zero, r_addr = zero, r_left = zero, r_first = zero;
  ValueId r_ar_hs = zero, r_ar_done = zero;
  OneEntry rrsp;
  // With several flows (BurstOutletProfile::flows) the AXI4-Lite face is one port set per flow.
  // The packets in flight share one flow (the arrival order lets a packet of another flow in only
  // when none is in flight): AWVALID and WVALID rise on the set of the bus word's flow (w_flow),
  // ARVALID on that of the issuing packet (r_flow), and their READY comes from it; a B or R comes
  // from any set, its payload from the set offering it; the other outputs drive every set.
  ValueId w_flow = zero, r_flow = zero;
  std::map<std::string, ValueId> set_ports;
  ValueId in(const std::string &face, const std::string &field, unsigned width) {
    if (face != "axil" || p.flows < 2)
      return l.in(face, field, width);
    // A port of set k, declared once (a valid is also read with its payload).
    const auto port = [&](unsigned k, const std::string &name) {
      const auto signal = name + "_" + std::to_string(k);
      if (!set_ports.contains(signal))
        set_ports.emplace(signal, l.in(face, signal, name == field ? width : 1));
      return set_ports.at(signal);
    };
    if (field == "awready" || field == "wready" || field == "arready") {
      const auto flow = field == "arready" ? r_flow : w_flow;
      auto value = port(0, field);
      for (unsigned k = 1; k < p.flows; ++k)
        value = l.b.select(l.eq(flow, k), port(k, field), value);
      return value;
    }
    const auto valid = field.starts_with("b") ? "bvalid" : "rvalid";
    auto value = l.b.constant(width, 0);
    for (unsigned k = 0; k < p.flows; ++k)
      value = l.b.binary(Op::BitOr, value,
                         field == valid
                             ? port(k, field)
                             : l.b.select(port(k, valid), port(k, field), l.b.constant(width, 0)));
    return value;
  }
  void out(const std::string &face, const std::string &field, ValueId v) {
    if (face != "axil" || p.flows < 2) {
      o.out(face, field, v);
      return;
    }
    const bool write = field == "awvalid" || field == "wvalid", read = field == "arvalid";
    for (unsigned k = 0; k < p.flows; ++k)
      o.out(face, field + "_" + std::to_string(k),
            write  ? l.both(v, l.eq(w_flow, k))
            : read ? l.both(v, l.eq(r_flow, k))
                   : v);
  }
  // The flow of the mapping holding a global address.
  ValueId flow_of(ValueId address) {
    return mapping_decode::flow(l, address, o.bits, p.mappings,
                                static_cast<unsigned>(std::bit_width(p.flows - 1)));
  }
  ValueId active(ValueId v) { return o.active(v); }
  ValueId isolate(ValueId code) { return o.isolate(code); }
  // The local byte offset of a packet word: word aligned, window base
  // subtracted, narrowed to the port.
  ValueId bus_offset(ValueId address) {
    auto local = l.b.binary(Op::BitAnd, address, l.b.constant(o.bits, o.space - o.lanes));
    local = o.localized(local);
    if (p.address_bits < o.bits)
      local = l.b.slice(local, 0, p.address_bits);
    return local;
  }
  void write_issue();
  void read_issue();
  void write_responses();
  void read_responses();
  void exclusive_side();
  void build();
  Result<NodeHardware> finish();
};

// The packet table with its completion tallies, the exclusive monitor's issue-side judgment and
// the per-word local result, then the write word on the bus (AW and W complete independently),
// the tally of each accepted word and the packet push.
void LiteOutlet::write_issue() {
  Fields shape{{"address", w.at("address")}, {"size", w.at("size")}};
  if (p.id_bits)
    shape["id"] = w.at("id");
  if (p.source_bits)
    shape["source"] = w.at("source");
  if (p.tag_bits)
    shape["tag"] = w.at("tag");
  if (monitored) {
    // The exclusive monitor compares the packet range of a write in flight.
    shape["length"] = w.at("length");
    shape["burst"] = w.at("burst");
    shape["exclusive"] = zero;
    shape["fail"] = zero;
  }
  wpkt = packet_context::make(l, "w.packet", 1, cap, shape);
  for (unsigned slot = 0; slot < cap; ++slot) {
    const auto at = "w.tally.s" + std::to_string(slot) + ".";
    Fields t{{"reals", l.state(at + "reals", kw)}, {"code", l.state(at + "code", 2)}};
    if (codes)
      t["after"] = l.state(at + "after", 1);
    tally.push_back(std::move(t));
  }
  w_open = l.state("w.open", 1);
  if (monitored) {
    fail_tail = l.state("w.fail_tail", 1);
    failing = o.watch(w);
  }
  // A failed exclusive write answers OKAY whatever the target answers, a target timeout included.
  wcode = isolate(o.local_result(w));
  if (timeout && monitored)
    wcode = l.b.select(failing, o.local_result(w), wcode);
  // The fail flag travels with each word: judged at the head, kept per packet.
  face_fail = monitored ? l.b.select(w.at("first"), failing, fail_tail) : zero;
  bus_open = l.state("bus.open", 1);
  aw_done = l.state("bus.aw_done", 1), w_done = l.state("bus.w_done", 1);
  bus_address = l.state("bus.address", p.address_bits);
  bus_prot = l.state("bus.protection", 3);
  bus_data = l.state("bus.data", o.word_bits);
  bus_mask = l.state("bus.mask", o.lanes);
  if (p.flows > 1)
    w_flow = l.state("bus.flow", static_cast<unsigned>(std::bit_width(p.flows - 1)));
  const auto awvalid = active(l.both(bus_open, l.inv(aw_done)));
  const auto wvalid = active(l.both(bus_open, l.inv(w_done)));
  const auto aw_hs = l.both(awvalid, in("axil", "awready", 1));
  const auto w_hs = l.both(wvalid, in("axil", "wready", 1));
  bus_done = l.both(bus_open, l.both(l.either(aw_done, aw_hs), l.either(w_done, w_hs)));
  const auto bus_take = l.either(l.inv(bus_open), bus_done);
  out("axil", "awaddr", bus_address);
  out("axil", "awprot", bus_prot);
  if (!p.user.empty())
    out("axil", "awuser", bus_user = l.state("bus.user", static_cast<unsigned>(p.user.size())));
  out("axil", "awvalid", awvalid);
  out("axil", "wdata", bus_data);
  out("axil", "wstrb", bus_mask);
  out("axil", "wvalid", wvalid);

  // ---- Acceptance: the bus registers are the word's one forward entry per channel. A word with
  // an access loads them when they are free or complete in this cycle (WREADY and AWREADY reach
  // the network ready in the same cycle); a word without one, or any word after expiry, joins its
  // packet's tally at once. ----
  const auto pkt_room = l.inv(l.eq(wpkt.count[0], cap));
  // A head word also needs a packet slot.
  const auto head_gate = l.either(l.inv(w.at("first")), pkt_room);
  const auto face_access = l.both(l.eq(wcode, 0), l.inv(failed));
  const auto wr_ready = active(l.both(head_gate, l.either(l.inv(face_access), bus_take)));
  out("wreq", "ready", wr_ready);
  wt = l.bind("w.accept", l.both(w.at("valid"), wr_ready));
  if (monitored)
    l.b.update(fail_tail, l.b.select(l.both(wt, w.at("first")), failing, fail_tail));
  load = l.both(wt, face_access);
  auto prot = w.at("protection");
  if (p.attributes)
    prot = map_attributes(l, p.attributes->prot, w.at("cache"), prot, one);
  const auto load_data =
      monitored ? l.b.select(face_fail, l.b.constant(o.word_bits, 0), w.at("data")) : w.at("data");
  const auto load_mask =
      monitored ? l.b.select(face_fail, l.b.constant(o.lanes, 0), w.at("be")) : w.at("be");
  l.b.update(bus_open, l.b.select(load, one, l.b.select(bus_done, zero, bus_open)));
  l.b.update(bus_address, l.b.select(load, bus_offset(w.at("address")), bus_address));
  l.b.update(bus_prot, l.b.select(load, prot, bus_prot));
  if (!p.user.empty())
    l.b.update(bus_user,
               l.b.select(load, map_attributes(l, p.user, w.at("cache"), prot, one, w.at("user")),
                          bus_user));
  l.b.update(bus_data, l.b.select(load, load_data, bus_data));
  l.b.update(bus_mask, l.b.select(load, load_mask, bus_mask));
  if (p.flows > 1)
    l.b.update(w_flow, l.b.select(load, flow_of(w.at("address")), w_flow));
  l.b.update(aw_done, l.b.select(load, zero, l.b.select(aw_hs, one, aw_done)));
  l.b.update(w_done, l.b.select(load, zero, l.b.select(w_hs, one, w_done)));

  // The tally of the packet taking the word: its first word starts one in the free slot, a later
  // word adds to the newest packet's. An issued word counts toward the B's its packet waits for;
  // any other word answers its local code (SLVERR after expiry) without access. Without error
  // codes the local codes merge. With them the first local answer is kept, and whether a real came
  // before it: sources cut packets at every mapping edge, so the words of a packet share one local
  // answer and only an expiry puts one after a real, behind the B's of the reals before it.
  {
    const auto first = w.at("first");
    // The free slot and the newest packet's: head + count and one before it (a later word has a
    // packet in the table), wrapped at cap.
    ValueId slot = zero, newest = zero;
    if (cap > 1) {
      const unsigned wide = std::max(wpkt.count_bits, pw + 1);
      const auto end = l.plus(l.fit(wpkt.head[0], wide), l.fit(wpkt.count[0], wide));
      const auto wrap = [&](ValueId v) {
        return l.fit(l.b.select(l.below(v, l.num(wide, cap)), v, l.minus(v, l.num(wide, cap))), pw);
      };
      newest = wrap(l.dec(end));
      slot = l.b.select(first, wrap(end), newest);
    }
    Fields cur;
    for (const auto &[field, value] : tally.front()) {
      auto v = value;
      for (unsigned s = 1; s < cap; ++s)
        v = l.b.select(l.eq(newest, s), tally[s].at(field), v);
      cur[field] = l.b.select(first, l.num(l.width(value), 0), v);
    }
    const auto local = l.b.select(failed, target_error, wcode);
    Fields next{{"reals", l.plus(cur.at("reals"), l.fit(load, kw))}};
    if (codes) {
      const auto start = l.both(l.inv(load), l.eq(cur.at("code"), 0));
      next["code"] = l.b.select(start, local, cur.at("code"));
      next["after"] = l.b.select(start, l.inv(l.eq(cur.at("reals"), 0)), cur.at("after"));
    } else {
      next["code"] = l.b.select(load, cur.at("code"), merge_error(l, cur.at("code"), local));
    }
    for (unsigned s = 0; s < cap; ++s) {
      const auto take = cap > 1 ? l.both(wt, l.eq(slot, s)) : wt;
      for (const auto &[field, state] : tally[s])
        l.b.update(state, l.b.select(take, next.at(field), state));
    }
    l.b.update(w_open, l.b.select(wt, l.inv(w.at("last")), w_open));
  }

  // The packet queue takes every head word, absorbed ones included; the push
  // lands in write_responses where the pop is known.
  wpkt_push = l.both(wt, w.at("first"));
  wentry = w;
  if (monitored) {
    wentry["exclusive"] = w.at("lock");
    wentry["fail"] = failing;
  }
}

// Write responses: the head packet answers once its last word is in and every B of its reals has
// returned (or the target failed), one B into the one-entry response stage. A B belongs to the head
// packet while it has reals without one; the B completing a sealed packet needs response room.
void LiteOutlet::write_responses() {
  Fields shape{{"address", w.at("address")}, {"size", w.at("size")}, {"result", ok}};
  if (p.id_bits)
    shape["id"] = w.at("id");
  if (p.source_bits)
    shape["source"] = w.at("source");
  if (p.tag_bits)
    shape["tag"] = w.at("tag");
  if (monitored) {
    shape["length"] = w.at("length");
    shape["burst"] = w.at("burst");
    shape["fail"] = zero;
    shape["exclusive_okay"] = zero;
  }
  if (p.response_user_bits && p.response_user_port_bits)
    shape["response_user"] = l.b.constant(p.response_user_bits, 0);
  wrsp = one_entry(l, "w.rsp", shape);
  Fields wface;
  const auto wrsp_room = l.inv(wrsp.full);
  pkt_head = packet_context::front(l, wpkt, zero);
  // The head packet's tally, its B's so far and the first error among them.
  Fields head = tally.front();
  for (const auto &[field, value] : head)
    for (unsigned s = 1; s < cap; ++s)
      head[field] = l.b.select(l.eq(wpkt.head[0], s), tally[s].at(field), head.at(field));
  const auto retired = l.state("w.retired", kw);
  const auto bus_error = l.state("w.bus_error", codes ? 2 : 1);
  const auto have_head = l.inv(l.eq(wpkt.count[0], 0));
  // Sealed: the head packet's last word is in (the newest packet may still take words).
  const auto sealed = l.inv(l.both(w_open, l.eq(wpkt.count[0], 1)));
  const auto reals = head.at("reals");
  const auto owed = l.inv(l.same(retired, reals));
  const auto one_left = l.follows(retired, reals);
  const auto last_b = l.both(sealed, one_left);
  const auto bvalid = in("axil", "bvalid", 1);
  const auto bresp = in("axil", "bresp", 2);
  const auto bcode =
      l.b.select(l.eq(bresp, 0), ok,
                 codes ? l.b.select(l.eq(bresp, 3), net_miss, target_error) : target_error);
  // A B is taken only against a bus completion credit (its word's AW and W both done): a target
  // that offers B early violates the protocol and must not move the response side.
  w_bus_done = l.state("w.bus_done", cw + p.len_bits);
  const auto w_credited = l.inv(l.eq(w_bus_done, 0));
  // After expiry a report-only target drops the answer.
  const auto drop = report_only ? failed : zero;
  const auto b_open =
      l.both(l.both(have_head, w_credited),
             l.both(l.either(l.inv(sealed), owed), l.either(l.inv(last_b), wrsp_room)));
  const auto bt = l.bind("w.b_take", l.both(l.both(bvalid, b_open), l.inv(failed)));
  // The head packet's exclusive failure: answers OKAY whatever the target answered, and never
  // exclusive_okay; after expiry it answers OKAY whatever its words did.
  const auto fail =
      monitored ? l.bind("w.exclusive_fail", l.both(have_head, pkt_head.at("fail"))) : zero;
  const auto fail_now = monitored && timeout ? l.bind("w.fail_now", l.both(failed, fail)) : zero;
  // Framing on the last B, written over bt's inputs so the take gate stays out of the response
  // valid: with the head sealed, its last B owes a real (one_left implies owed), and a drop only
  // follows expiry, which frames anyway.
  const auto frame = l.both(
      l.both(have_head, sealed),
      l.both(l.either(failed, l.either(l.inv(owed), l.both(l.both(bvalid, w_credited), one_left))),
             l.either(wrsp_room, drop)));
  // The first error in word order: a local answer ahead of every real wins, one after a real
  // follows the B errors of the reals before it.
  const auto code = head.at("code");
  const auto local_first = codes ? l.both(l.inv(l.eq(code, 0)), l.inv(head.at("after"))) : zero;
  ValueId result{};
  const auto counted = l.both(bt, l.inv(fail));
  // The framed result is read only on a push, where bt is owed & ~failed (a sealed head framing
  // with a real owed before expiry takes its last B): the take gate stays out of the payload.
  const auto framed_count = l.both(l.both(owed, l.inv(failed)), l.inv(fail));
  // A target that failed answers SLVERR for every real still owed.
  const auto abandoned = l.both(l.both(failed, l.inv(fail)), owed);
  if (codes) {
    const auto bus_with = [&](ValueId taken) {
      const auto fresh = l.both(l.both(taken, l.inv(l.eq(bcode, 0))),
                                l.both(l.eq(bus_error, 0), l.inv(local_first)));
      return l.b.select(fresh, bcode, bus_error);
    };
    l.b.update(bus_error, l.b.select(frame, l.b.constant(2, 0), bus_with(counted)));
    const auto bus_now = bus_with(framed_count);
    result =
        l.b.select(l.inv(l.eq(bus_now, 0)), bus_now,
                   l.b.select(l.both(l.inv(l.eq(code, 0)), l.either(l.inv(abandoned), local_first)),
                              code, l.b.select(abandoned, target_error, ok)));
  } else {
    const auto bus_with = [&](ValueId taken) {
      return l.either(bus_error, l.both(taken, l.inv(l.eq(bcode, 0))));
    };
    l.b.update(bus_error, l.b.select(frame, zero, bus_with(counted)));
    result = merge_error(l, code,
                         l.b.select(l.either(bus_with(framed_count), abandoned), target_error, ok));
  }
  if (monitored && timeout)
    result = l.b.select(fail_now, ok, result);
  const auto frame_push = l.bind("w.frame_push", l.both(frame, l.inv(drop)));
  {
    Fields entry =
        pick(pkt_head, {"address", "size", "id", "source", "tag", "length", "burst", "fail"});
    entry["result"] = result;
    if (monitored)
      entry["exclusive_okay"] =
          l.both(l.both(pkt_head.at("exclusive"), l.inv(fail)), l.eq(result, 0));
    // The packet's B carries the BUSER of its first B (none after expiry or without a B).
    if (const auto user = o.target_user("buser", bt)) {
      const auto first_user = l.state("w.user", p.response_user_bits);
      const auto none = l.eq(retired, 0);
      l.b.update(first_user, l.b.select(l.both(bt, none), *user, first_user));
      entry["response_user"] = l.b.select(failed, l.b.constant(p.response_user_bits, 0),
                                          l.b.select(none, *user, first_user));
    }
    wface = one_entry_face(l, wrsp, entry, frame_push, in("wrsp", "ready", 1), wrsp_valid);
  }
  l.b.update(retired,
             l.b.select(frame, l.b.constant(kw, 0), l.b.select(bt, l.inc(retired), retired)));
  packet_context::step_exact(l, wpkt, wpkt_push, wentry, frame);
  // A B is taken for the head packet, and discarded after expiry.
  out("axil", "bready", active(l.either(failed, b_open)));
  l.b.update(w_bus_done,
             l.plus(l.minus(w_bus_done, l.fit(bt, cw + p.len_bits)),
                    l.fit(timeout ? l.both(bus_done, l.inv(failed)) : bus_done, cw + p.len_bits)));
  o.response("wrsp", wface, wrsp_valid, wface.at("address"), wface.at("result"), one, one,
             l.b.constant(o.word_bits, 0), monitored ? wface.at("exclusive_okay") : zero);
  // A write is in flight from its head word until its B leaves: the packet
  // queue, then the staged response.
  w_queued = l.bind("w.busy", l.inv(l.eq(wpkt.count[0], 0)));
  l.bind("bus.issue", load);
  l.bind("bus.complete", bus_done);
}

// The read packet table: one descriptor per packet in flight. The issue
// cursor walks the head of the not-yet-issued packets; after expiry the
// orphan copy serves the held packet's remaining transfers.
void LiteOutlet::read_issue() {
  Fields shape{{"address", r.at("address")}, {"length", r.at("length")},
               {"size", r.at("size")},       {"burst", r.at("burst")},
               {"cache", r.at("cache")},     {"protection", r.at("protection")},
               {"error", r.at("error")}};
  if (p.id_bits)
    shape["id"] = r.at("id");
  if (p.source_bits)
    shape["source"] = r.at("source");
  if (p.tag_bits)
    shape["tag"] = r.at("tag");
  if (!p.user.empty())
    shape["user"] = r.at("user");
  if (monitored)
    shape["exclusive"] = zero;
  // Whether a beat ends a response run is known ahead: the first beat's with the packet, a later
  // beat's with the step to its address (read_responses).
  if (p.response_run_bits)
    shape["run_end"] = zero;
  rpkt = packet_context::make(l, "r.packet", 1, cap, shape);
  r_issue = l.state("r.packet.issue", pw);
  r_unissued = l.state("r.packet.unissued", cw);
  r_issuing = l.state("r.issuing", 1);
  r_issue_addr = l.address_state("r.issue_address", o.bits);
  r_issue_left = l.state("r.issue_left", p.len_bits + 1);
  const auto issue_entry = packet_context::read_at(l, rpkt, zero, r_issue);
  const auto issue_valid = l.inv(l.eq(r_unissued, 0));
  const auto cur_issue_addr = l.b.select(r_issuing, r_issue_addr, issue_entry.at("address"));
  const auto cur_issue_left = l.b.select(
      r_issuing, r_issue_left,
      l.plus(l.fit(issue_entry.at("length"), p.len_bits + 1), l.b.constant(p.len_bits + 1, 1)));
  const auto issue_code =
      isolate(o.local_result(cur_issue_addr, issue_entry.at("error"), issue_entry.at("burst"),
                             issue_entry.at("size"), p.write_only));
  const auto issue_access = l.eq(issue_code, 0);
  // The packet under the issue cursor (its words, and an orphan copy of it, share its flow).
  if (p.flows > 1)
    r_flow = flow_of(issue_entry.at("address"));
  const auto arready = in("axil", "arready", 1);
  const auto issue_offered = l.both(issue_valid, issue_access);
  ValueId arvalid = active(issue_offered);
  ValueId ar_addr = cur_issue_addr;
  ValueId ar_cache = issue_entry.at("cache"), ar_prot = issue_entry.at("protection");
  std::optional<ValueId> ar_user;
  if (!p.user.empty())
    ar_user = issue_entry.at("user");
  if (timeout) {
    // The held packet is the one under the issue cursor (with one packet, the queue head).
    std::vector<const char *> kept{"address", "length",     "size", "burst",
                                   "cache",   "protection", "error"};
    if (ar_user)
      kept.emplace_back("user");
    const auto c = o.orphan({{"address", o.bits}, {"left", p.len_bits + 1}},
                            cap > 1 ? issue_entry : packet_context::front(l, rpkt, zero), kept);
    const auto capture = c.capture, orphan_address = c.word.at("address"),
               orphan_left = c.word.at("left");
    const auto &orphan_pkt = c.packet;
    // The copy starts past an access whose handshake lands in the capture cycle itself. The live
    // offer shows the un-isolated decode, matching what the cycle before the failure offered.
    const auto live_offer =
        l.both(issue_valid,
               l.eq(o.local_result(cur_issue_addr, issue_entry.at("error"), issue_entry.at("burst"),
                                   issue_entry.at("size"), p.write_only),
                    0));
    const auto live_hs = l.both(live_offer, arready);
    const auto orphan_access =
        l.eq(o.local_result(orphan_address, orphan_pkt.at("error"), orphan_pkt.at("burst"),
                            orphan_pkt.at("size"), p.write_only),
             0);
    const auto orphan_active = l.both(c.taken, l.inv(l.eq(orphan_left, 0)));
    const auto orphan_go = l.both(orphan_active, l.either(l.inv(orphan_access), arready));
    const auto held = l.both(l.inv(l.eq(rpkt.count[0], 0)), issue_valid);
    o.move(c, "address",
           l.b.select(live_hs, next_address(l, cur_issue_addr, issue_entry, o.bits, o.max_size),
                      cur_issue_addr),
           orphan_go, next_address(l, orphan_address, orphan_pkt, o.bits, o.max_size));
    o.move(c, "left",
           l.b.select(held, l.b.select(live_hs, l.dec(cur_issue_left), cur_issue_left),
                      l.b.constant(p.len_bits + 1, 0)),
           orphan_go, l.dec(orphan_left));
    // During the capture cycle itself the live cursor still drives the bus,
    // so a pending AR keeps its payload; the copy drives from the next cycle.
    arvalid = active(
        l.b.select(failed, l.b.select(capture, live_offer, l.both(orphan_active, orphan_access)),
                   issue_offered));
    ar_addr = l.b.select(failed, l.b.select(capture, cur_issue_addr, orphan_address), ar_addr);
    ar_cache = l.b.select(failed, l.b.select(capture, ar_cache, orphan_pkt.at("cache")), ar_cache);
    ar_prot =
        l.b.select(failed, l.b.select(capture, ar_prot, orphan_pkt.at("protection")), ar_prot);
    if (ar_user)
      ar_user = l.b.select(failed, l.b.select(capture, *ar_user, orphan_pkt.at("user")), *ar_user);
  }
  out("axil", "arvalid", arvalid);
  if (p.attributes)
    ar_prot = map_attributes(l, p.attributes->prot, ar_cache, ar_prot, zero);
  out("axil", "araddr", bus_offset(ar_addr));
  out("axil", "arprot", ar_prot);
  if (ar_user)
    out("axil", "aruser", map_attributes(l, p.user, ar_cache, ar_prot, zero, *ar_user));
  const auto ar_hs = l.both(arvalid, arready);
  // A pre-expiry AR handshake credits one R the response side may take; a target that offers R
  // without it violates the protocol and must not move the response cursor.
  r_ar_hs = l.both(ar_hs, l.inv(failed));
  // The issue cursor advances on every issued access and freely over words the outlet answers
  // itself; after expiry it does not move (the orphan copy serves the held packet).
  const auto issue_advance =
      l.both(l.inv(failed), l.both(issue_valid, l.either(l.inv(issue_access), ar_hs)));
  const auto issue_last = l.eq(cur_issue_left, 1);
  const auto issue_done = l.both(issue_advance, issue_last);
  l.b.update(r_issuing, l.b.select(issue_advance, l.inv(issue_last), r_issuing));
  l.b.update(r_issue_addr,
             l.b.select(l.both(issue_advance, l.inv(issue_last)),
                        next_address(l, cur_issue_addr, issue_entry, o.bits, o.max_size),
                        r_issue_addr));
  l.b.update(r_issue_left, l.b.select(issue_advance, l.dec(cur_issue_left), r_issue_left));
  l.b.update(r_issue, l.b.select(issue_done, packet_context::next_ptr(l, rpkt, r_issue), r_issue));
  rt = l.bind("r.accept", l.both(r.at("valid"), active(l.inv(l.eq(rpkt.count[0], cap)))));
  out("rreq", "ready", active(l.inv(l.eq(rpkt.count[0], cap))));
  l.b.update(r_unissued, l.plus(l.minus(r_unissued, l.fit(issue_done, cw)), l.fit(rt, cw)));
}

// The read response cursor answers one beat per word: a word the outlet
// answers itself emits immediately, an accessed beat waits for its R.
void LiteOutlet::read_responses() {
  Fields shape{{"address", r.at("address")}, {"size", r.at("size")}, {"result", ok},
               {"data", w.at("data")},       {"first", one},         {"last", one}};
  if (p.id_bits)
    shape["id"] = r.at("id");
  if (p.source_bits)
    shape["source"] = r.at("source");
  if (p.tag_bits)
    shape["tag"] = r.at("tag");
  if (monitored)
    shape["exclusive_okay"] = zero;
  if (p.response_user_bits && p.response_user_port_bits)
    shape["response_user"] = l.b.constant(p.response_user_bits, 0);
  if (p.response_run_bits)
    shape["run_end"] = zero;
  rrsp = one_entry(l, "r.rsp", shape);
  Fields rface;
  const auto rrsp_room = l.inv(rrsp.full);
  r_on = l.state("r.on", 1);
  r_addr = l.address_state("r.address", o.bits);
  r_left = l.state("r.left", p.len_bits + 1);
  r_first = l.state("r.first", 1);
  if (p.response_run_bits)
    r_run_end = l.state("r.run_end", 1);
  const auto rsp_entry = packet_context::front(l, rpkt, zero);
  const auto rsp_valid = l.inv(l.eq(rpkt.count[0], 0));
  const auto cur_addr = l.b.select(r_on, r_addr, rsp_entry.at("address"));
  const auto cur_left = l.b.select(
      r_on, r_left,
      l.plus(l.fit(rsp_entry.at("length"), p.len_bits + 1), l.b.constant(p.len_bits + 1, 1)));
  const auto cur_first = l.b.select(r_on, r_first, one);
  const auto local = isolate(o.local_result(cur_addr, rsp_entry.at("error"), rsp_entry.at("burst"),
                                            rsp_entry.at("size"), p.write_only));
  const auto beat_void = l.inv(l.eq(local, 0));
  const auto rvalid = in("axil", "rvalid", 1);
  const auto rresp = in("axil", "rresp", 2);
  const auto rdata = in("axil", "rdata", o.word_bits);
  const auto rcode_bus =
      l.b.select(l.eq(rresp, 0), ok,
                 codes ? l.b.select(l.eq(rresp, 3), net_miss, target_error) : target_error);
  const auto beat_code = l.b.select(beat_void, local, rcode_bus);
  const auto emit_code = beat_code;
  const auto dropping = report_only ? failed : zero;
  // Accessed beats answered before expiry against the AR credit: an R is
  // taken only for a beat whose AR handshake already happened.
  r_ar_done = l.state("r.ar_done", cw + p.len_bits + 1);
  const auto r_credited = l.inv(l.eq(r_ar_done, 0));
  const auto beat_ready = l.b.select(beat_void, one, l.both(rvalid, r_credited));
  const auto advance = l.both(rsp_valid, l.both(beat_ready, l.either(rrsp_room, dropping)));
  const auto emit_beat = l.both(advance, l.inv(dropping));
  const auto beat_mask = o.read_mask(cur_addr, rsp_entry.at("size"));
  std::vector<ValueId> bytes;
  for (unsigned i = 0; i < o.lanes; ++i)
    bytes.push_back(
        l.b.select(l.b.slice(beat_mask, i, 1), l.b.slice(rdata, i * 8, 8), l.b.constant(8, 0)));
  auto data = l.b.concat(l.b.concat(bytes[3], bytes[2]), l.b.concat(bytes[1], bytes[0]));
  if (o.lanes == 8) {
    const auto upper = l.b.concat(l.b.concat(bytes[7], bytes[6]), l.b.concat(bytes[5], bytes[4]));
    data = l.b.concat(upper, data);
  }
  {
    Fields entry = pick(rsp_entry, {"size", "id", "source", "tag"});
    entry["address"] = cur_addr;
    entry["result"] = emit_code;
    entry["data"] = l.b.select(l.eq(emit_code, 0), data, l.b.constant(o.word_bits, 0));
    entry["first"] = cur_first;
    entry["last"] = l.eq(cur_left, 1);
    if (p.response_run_bits)
      entry["run_end"] = l.b.select(r_on, r_run_end, rsp_entry.at("run_end"));
    if (monitored)
      entry["exclusive_okay"] = l.both(rsp_entry.at("exclusive"), l.eq(emit_code, 0));
    // Every beat of a packet carries the RUSER of its first word.
    if (const auto user = o.target_user("ruser", l.both(l.inv(beat_void), l.inv(failed)))) {
      const auto head = l.state("r.user_head", p.response_user_bits);
      l.b.update(head, l.b.select(l.both(advance, cur_first), *user, head));
      entry["response_user"] = l.b.select(cur_first, *user, head);
    }
    rface = one_entry_face(l, rrsp, entry, emit_beat, in("rrsp", "ready", 1), rrsp_valid);
  }
  const auto beat_last = l.eq(cur_left, 1);
  const auto rpkt_pop = l.both(advance, beat_last);
  l.b.update(r_on, l.b.select(advance, l.inv(beat_last), r_on));
  const auto next_beat = next_address(l, cur_addr, rsp_entry, o.bits, o.max_size);
  l.b.update(r_addr, l.b.select(l.both(advance, l.inv(beat_last)), next_beat, r_addr));
  if (p.response_run_bits)
    l.b.update(r_run_end,
               l.b.select(advance,
                          burst_run_end(l, rsp_entry, next_beat, o.word_bits, p.response_run_bits),
                          r_run_end));
  l.b.update(r_left, l.b.select(advance, l.dec(cur_left), r_left));
  l.b.update(r_first, l.b.select(advance, l.b.select(beat_last, one, zero), r_first));
  // An R beat is taken when its word advances; after expiry every late beat
  // is accepted and discarded. The AR credit gates the take so a premature R
  // waits; late R's after expiry are absorbed without it.
  out("axil", "rready",
      active(l.either(failed,
                      l.both(rsp_valid, l.both(l.inv(beat_void), l.both(rrsp_room, r_credited))))));
  const auto r_credit_take = l.both(advance, l.inv(beat_void));
  l.b.update(r_ar_done, l.plus(l.minus(r_ar_done, l.fit(r_credit_take, cw + p.len_bits + 1)),
                               l.fit(r_ar_hs, cw + p.len_bits + 1)));
  o.response("rrsp", rface, rrsp_valid, rface.at("address"), rface.at("result"), rface.at("first"),
             rface.at("last"), rface.at("data"), monitored ? rface.at("exclusive_okay") : zero);
  auto rentry = r;
  if (monitored)
    rentry["exclusive"] = r.at("lock");
  if (p.response_run_bits)
    rentry["run_end"] = burst_run_end(l, r, r.at("address"), o.word_bits, p.response_run_bits);
  packet_context::step_exact(l, rpkt, rt, rentry, rpkt_pop);
}

// Exclusive monitor events: an exclusive read records its reservation at its
// head unless a write in flight (from its head until its B leaves) overlaps
// the region: the head being accepted, the head of the packet queue, and the
// staged responses of packets already answered on the bus. Packets queued
// behind the head are not compared: in a network the exchange's direction
// gate opens the read face only once no write packet of the target is between
// acquisition and release, so the outlet never takes a read while one is
// queued, whatever its capacity.
void LiteOutlet::exclusive_side() {
  if (monitored)
    o.reserve(w, r, rt, l.both(wpkt_push, l.inv(failing)), l.both(wpkt_push, l.inv(failing)),
              {{w_queued, pkt_head.at("fail"), &pkt_head},
               {wrsp.full, wrsp.main.at("fail"), &wrsp.main}});
}

void LiteOutlet::build() {
  w = o.request("wreq");
  r = o.request("rreq");
  write_issue();
  read_issue();
  write_responses();
  read_responses();
  exclusive_side();
}

Result<NodeHardware> LiteOutlet::finish() {
  if (timeout)
    o.watchdog(l.either(l.either(w.at("valid"), r.at("valid")),
                        l.either(l.either(l.inv(l.eq(wpkt.count[0], 0)), wrsp.full),
                                 l.inv(l.eq(rpkt.count[0], 0)))),
               l.either(wrsp_valid, rrsp_valid));
  return l.finish("nocloom_axi_lite_outlet_" + p.name);
}

Result<NodeHardware> lower_lite_engine(const BurstOutletProfile &p) {
  if (auto bad = profile_error(p, Protocol::Axi4Lite))
    return Result<NodeHardware>::failure(*bad);
  LiteOutlet engine(p);
  engine.build();
  return engine.finish();
}

// The AHB engine, split into allocation, the bus phases, the write/read issue
// and the retirement/response sides so that every builder function stays
// small; the sides share the packet contexts and the bus state.
struct AhbOutlet {
  explicit AhbOutlet(const BurstOutletProfile &profile);
  OutletBase o;
  const BurstOutletProfile &p;
  logic::Logic &l;
  const ValueId zero, one, ok, target_error;
  const unsigned bits, word_bits, lanes, max_size;
  const bool wide_bus;
  const std::uint64_t space;
  const unsigned timeout;
  const bool monitored, report_only;
  const ValueId failed;
  const unsigned cb;
  // With several flows (BurstOutletProfile::flows) the AHB face is one port set per flow: HSEL of
  // the address phase's flow (bus_flow) alone, the other outputs on every set. The data phase's
  // set (response_select, one bit per flow: the HSEL vector HREADY last took) gives HREADY,
  // HRDATA and HRESP; with no set selected HREADY is high.
  ValueId bus_flow = zero, response_select = zero;
  ValueId in(const std::string &face, const std::string &field, unsigned width) {
    if (face != "ahb" || p.flows < 2)
      return l.in(face, field, width);
    auto value = field == "hreadyout" ? one : l.b.constant(width, 0);
    for (unsigned k = 0; k < p.flows; ++k) {
      const auto chosen = l.b.slice(response_select, k, 1);
      const auto port = l.in(face, field + "_" + std::to_string(k), width);
      value = field == "hreadyout"
                  ? l.both(value, l.either(l.inv(chosen), port))
                  : l.b.binary(Op::BitOr, value, l.b.select(chosen, port, l.b.constant(width, 0)));
    }
    return value;
  }
  void out(const std::string &face, const std::string &field, ValueId v) {
    if (face != "ahb" || p.flows < 2) {
      o.out(face, field, v);
      return;
    }
    for (unsigned k = 0; k < p.flows; ++k)
      o.out(face, field + "_" + std::to_string(k), field == "hsel" ? selected(v, k) : v);
  }
  ValueId selected(ValueId hsel, unsigned flow) { return l.both(hsel, l.eq(bus_flow, flow)); }
  ValueId active(ValueId value) { return o.active(value); }
  ValueId isolate(ValueId code) { return o.isolate(code); }
  ValueId widen(ValueId value, unsigned width) { return l.b.unary(Op::ZeroExtend, value, width); }
  ValueId same(ValueId x, ValueId y) { return l.b.binary(Op::Equal, x, y); }
  ValueId context(const std::string &name, ValueId index) {
    return l.b.select(l.eq(index, 2), contexts[2].at(name),
                      l.b.select(l.eq(index, 1), contexts[1].at(name), contexts[0].at(name)));
  }
  ValueId words_left(const Fields &f) { return widen(f.at("length"), cb); }
  ValueId burst_code(const Fields &f, const std::string &dir);

  // Faces, packet contexts and the write/read state. Phase-assigned handles
  // start at a defined value.
  Fields w, r;
  ValueId win = zero, wout = zero, receiving = zero, wf = zero, bf = zero, wp = zero;
  ValueId wm = zero, sent = zero, leaving = zero;
  std::vector<Fields> contexts;
  Fields wd;
  ValueId wa = zero, wdata = zero, wl = zero;
  ValueId wr_ready = zero, wrsp_ready = zero, rrsp_ready = zero;
  Fields rd;
  ValueId ia = zero, ileft = zero, issuing = zero, im = zero, icode = zero, initial_code = zero;
  ValueId inflight = zero, count = zero;
  std::vector<ValueId> fdata, fresult;
  std::vector<Fields> metadata;
  ValueId ra = zero, rfirst = zero, issue_first = zero;
  ValueId hready = zero, hrdata = zero, hresp = zero;
  ValueId rt = zero, last = zero;
  // The address and data phases and the read response room.
  ValueId a_valid = zero, owner = zero, d_valid = zero, d_write = zero;
  ValueId accept = zero, complete = zero, wcomplete = zero, rcomplete = zero;
  ValueId offered = zero, buffer_room = zero, lost = zero, drain = zero, responding = zero,
          rpop = zero;
  ValueId held = zero, room = zero;
  ValueId a_packet = zero, d_packet = zero, rpacket_now = zero;
  Fields rd_now;
  ValueId issuing_now = zero, issue_first_now = zero, ia_now = zero, ileft_now = zero;
  ValueId im_now = zero, icode_now = zero, initial_now = zero;
  // The write word as the engine sees it, with its packet's context.
  ValueId wt = zero, wh = zero, failing = zero, w_fail = zero, fail_here = zero;
  ValueId wcode = zero, wmask = zero, not_full = zero;
  ValueId enter_now = zero, wf_now = zero, wp_now = zero;
  ValueId wa_now = zero, wl_now = zero, wm_now = zero;
  std::vector<ValueId> head_burst;
  Fields wd_now;
  ValueId w_burst = zero, w_seq = zero, w_left = zero, w_partial = zero;
  ValueId r_burst = zero, r_seq = zero, r_left = zero, open = zero, open_write = zero;
  ValueId w_block = zero, r_block = zero, w_block_left = zero, r_block_left = zero, shown = zero;
  OutletBase::Orphan orphan;
  Fields bus_rd;
  ValueId bus_issuing = zero, bus_ia = zero, bus_ileft = zero, bus_im = zero, live_rt = zero;
  ValueId r_burst_now = zero, r_seq_now = zero, r_left_now = zero;
  // The issued transfer and its shape.
  ValueId owes = zero, choose_w = zero, w_issue = zero, r_issue = zero, issue = zero;
  ValueId peeled = zero, submask = zero, address = zero;
  ValueId bus_address = zero, bus_size = zero;
  ValueId split_read = zero, split_first = zero, split_last = zero, split_done = zero;
  ValueId split_pending = zero, split_cursor = zero, split_started = zero;
  ValueId d_split = zero, d_split_last = zero, raw_read_complete = zero;
  ValueId split_data = zero, split_result = zero, read_data = zero, read_result = zero;
  // Burst sequencing per direction.
  ValueId mode_w = zero, mode_r = zero, mode = zero, left = zero;
  ValueId w_code = zero, w_count = zero, w_more = zero, r_code = zero, r_count = zero;
  ValueId r_more = zero, bus_burst = zero, bus_trans = zero;
  // Write retirement and the B face.
  ValueId bus_result = zero, w_rest = zero, finish_w = zero, bpop = zero, bpush = zero;
  ValueId finish_last = zero, finish_packet = zero;
  ValueId bstore = zero, b_valid = zero;
  ValueId fail_now = zero;
  std::vector<ValueId> results;
  ValueId response_fail = zero, r_exclusive = zero;
  ValueId abort_pending = zero, abort_start = zero, abort_finish = zero;
  ValueId abort_release = zero;
  ValueId cancel_read = zero, discard_address = zero, read_aborted = zero, aborted_address = zero;
  std::vector<ValueId> read_aborts;

  void state();
  void phases();
  void abort_state();
  void write_accept();
  void issue_transfers();
  ValueId read_shape(ValueId single, ValueId block, ValueId offset);
  void gather_read();
  void burst_blocks();
  void retire_write();
  void drive_bus();
  void update_write();
  void respond_write();
  void update_read();
  void build();
  Result<NodeHardware> finish();
};

AhbOutlet::AhbOutlet(const BurstOutletProfile &profile)
    : o(profile), p(profile), l(o.l), zero(o.zero), one(o.one), ok(o.ok),
      target_error(o.target_error), bits(o.bits), word_bits(o.word_bits), lanes(o.lanes),
      max_size(o.max_size), wide_bus(o.wide_bus), space(o.space), timeout(o.timeout),
      monitored(o.monitored), report_only(o.report_only), failed(o.failed), cb(p.len_bits + 1) {}

// The faces, the three write packet contexts, the read descriptor and response
// state, and the target inputs in the order of the outlet's ports.
void AhbOutlet::state() {
  w = o.request("wreq");
  r = o.request("rreq");

  // Three packet contexts feed the current word. A word and each bus phase
  // carry their context index. The B has its own frozen metadata and result;
  // completing a packet can therefore release its context while B waits.
  win = l.state("w.input_packet", 2);
  wout = l.state("w.output_packet", 2);
  receiving = l.state("w.receiving", 1);
  wf = l.state("w.full", 1);
  bf = l.state("w.response_full", 1);
  wp = l.state("w.packet", 2);
  wm = l.state("w.mask", lanes);
  sent = l.state("w.sent", 1);
  for (unsigned k = 0; k < 3; ++k) {
    const auto prefix = "w.context" + std::to_string(k);
    auto fields = o.hold(prefix + ".descriptor", w);
    for (const auto *name : {"valid", "closing", "partial", "seq", "exclusive", "fail"})
      fields[name] = l.state(prefix + "." + name, 1);
    fields["result"] = l.state(prefix + ".result", 2);
    fields["start"] = l.state(prefix + ".start", max_size);
    fields["hburst"] = l.state(prefix + ".hburst", 3);
    fields["left"] = l.state(prefix + ".left", cb);
    if (p.hresp_width == 2)
      fields["abort"] = l.state(prefix + ".abort", 1);
    fields["block"] = l.state(prefix + ".block", 3);
    fields["block_left"] = l.state(prefix + ".block_left", cb);
    contexts.push_back(std::move(fields));
  }
  for (const auto &[name, value] : contexts[0])
    wd[name] = context(name, wp);
  l.bind("w.busy", l.either(contexts[2].at("valid"),
                            l.either(contexts[0].at("valid"), contexts[1].at("valid"))));
  wa = l.address_state("w.address", bits);
  wdata = l.state("w.data", word_bits);
  wl = l.state("w.last", 1);
  wrsp_ready = in("wrsp", "ready", 1);

  // Read packet: descriptor, the issue beat (address, remaining beats, whether it still makes a
  // transfer, its result before the bus), the response beat and the response buffer.
  rd = o.hold("r.descriptor", r);
  ia = l.address_state("r.issue_address", bits);
  ileft = l.state("r.issue_left", p.len_bits);
  issuing = l.state("r.issuing", 1);
  im = l.state("r.transfer", 1);
  icode = l.state("r.result", 2);
  initial_code = l.state("r.initial_result", 2);
  inflight = l.state("r.inflight", 2);
  count = l.state("r.count", 2);
  for (unsigned i = 0; i < 2; ++i) {
    fdata.push_back(l.state("r.buffer" + std::to_string(i) + ".data", word_bits));
    fresult.push_back(l.state("r.buffer" + std::to_string(i) + ".result", 2));
  }
  // Response metadata is reserved with each issued beat. It stays with
  // that beat through target waits and network backpressure, so the issue
  // descriptor may accept the next packet before the previous response ends.
  for (unsigned k = 0; k < 2; ++k) {
    Fields item;
    const auto prefix = "r.metadata" + std::to_string(k) + ".";
    for (const auto *name : {"address", "size", "id", "source", "tag"})
      if (r.contains(name))
        item[name] = l.state_like(prefix + name, r.at(name));
    for (const auto *name : {"first", "last", "exclusive"})
      item[name] = l.state(prefix + name, 1);
    if (p.response_run_bits)
      item["run_end"] = l.state(prefix + "run_end", 1);
    metadata.push_back(std::move(item));
  }
  ra = metadata[0].at("address");
  rfirst = metadata[0].at("first");
  issue_first = l.state("r.issue_first", 1);
  auto rr_ready = active(l.inv(issuing));
  out("rreq", "ready", rr_ready);
  rrsp_ready = in("rrsp", "ready", 1);
  if (p.flows > 1) {
    bus_flow = l.state("bus.flow", static_cast<unsigned>(std::bit_width(p.flows - 1)));
    response_select = l.state("bus.response_select", p.flows);
  }
  // The target inputs, declared in the order of the outlet's ports.
  hready = in("ahb", "hreadyout", 1);
  hrdata = in("ahb", "hrdata", word_bits);
  hresp = in("ahb", "hresp", p.hresp_width);
  if (p.hresp_width == 2) {
    abort_pending = l.state("bus.abort_pending", 1);
    for (unsigned k = 0; k < 2; ++k)
      read_aborts.push_back(l.state("r.packet" + std::to_string(k) + ".abort", 1));
  }
  rt = l.bind("r.accept", l.both(r.at("valid"), rr_ready));
  last = l.bind("r.last", l.eq(metadata[0].at("last"), 1));
}

// The address (A) and data (D) phases, the completion split by direction and
// the read response room, then the issue beat as it loads.
void AhbOutlet::phases() {
  // Bus: the address phase (A) and the data phase (D).
  a_valid = l.state("bus.address_phase", 1);
  owner = l.state("bus.write", 1);
  d_valid = l.state("bus.data_phase", 1);
  d_write = l.state("bus.data_write", 1);
  a_packet = l.state("bus.address_packet", 2);
  d_packet = l.state("bus.data_packet", 2);
  // The transfer phases: HREADY accepts the address phase and completes the
  // data phase. After expiry a late completion is accepted and discarded.
  accept = active(l.both(l.both(a_valid, hready), l.inv(abort_pending)));
  complete = l.bind("bus.complete", active(l.both(d_valid, hready)));
  const auto live = timeout ? l.both(complete, l.inv(failed)) : complete;
  wcomplete = l.both(live, d_write);
  d_split = l.state("r.subread.data_phase", 1);
  d_split_last = l.state("r.subread.data_last", 1);
  split_pending = l.state("r.subread.pending", 1);
  raw_read_complete = l.both(complete, l.inv(d_write));
  rcomplete = l.both(l.both(live, l.inv(d_write)), l.either(l.inv(d_split), d_split_last));
  // Two read response entries; an empty buffer passes a read completion to
  // the face, a local response goes through the buffer, a silent one drops,
  // and after a failure each read left on the bus answers SLVERR.
  offered = l.inv(l.eq(count, 0));
  buffer_room = l.either(l.inv(l.eq(count, 2)), l.both(active(offered), rrsp_ready));
  if (p.hresp_width == 2) {
    abort_start = active(l.both(d_valid, l.both(l.inv(hready), l.b.slice(hresp, 1, 1))));
    abort_finish = l.both(complete, abort_pending);
    const auto unrelated =
        l.both(a_valid, l.inv(l.both(same(owner, d_write), same(a_packet, d_packet))));
    abort_release = l.both(abort_start, l.inv(unrelated));
    const auto a_failed = l.b.select(owner, context("abort", a_packet),
                                     l.b.select(l.eq(a_packet, 0), read_aborts[0], read_aborts[1]));
    aborted_address = a_failed;
    accept = l.both(accept, l.inv(a_failed));
    // The cancelled address already reserved a response entry at issue.
    cancel_read = active(l.both(l.both(a_valid, l.inv(owner)), l.both(a_failed, l.inv(d_valid))));
    discard_address = l.either(cancel_read, l.both(abort_finish, l.both(owner, a_failed)));
    rcomplete = l.either(rcomplete, l.either(cancel_read, l.both(abort_finish, l.inv(d_write))));
  }
  lost = timeout ? active(l.both(failed, l.inv(l.eq(inflight, 0)))) : zero;
  drain = timeout ? l.both(lost, buffer_room) : zero;
  responding = l.either(
      offered, l.both(l.eq(count, 0), report_only ? rcomplete : l.either(rcomplete, lost)));
  rpop = l.both(active(responding), rrsp_ready);
  // Room for one more read: at most two responses, counting the one leaving.
  held = l.plus(widen(count, 3), widen(inflight, 3));
  room = l.bind("r.room", l.either(l.b.binary(Op::UnsignedLess, held, l.b.constant(3, 2)), rpop));
  auto rpacket = l.state("r.issue_packet", 2);
  rpacket_now =
      l.b.select(rt, l.b.select(l.eq(rpacket, 0), l.b.constant(2, 1), l.b.constant(2, 0)), rpacket);
  l.b.update(rpacket, rpacket_now);
  if (p.hresp_width == 2)
    read_aborted = l.b.select(l.eq(rpacket_now, 0), read_aborts[0], read_aborts[1]);
  // A read packet taken while no beat issues issues its first transfer in the same cycle: the
  // engine sees the issue beat and descriptor as they load (the *_now values).
  rd_now = view_fields(l, rd, r, rt);
  const auto started = isolate(o.local_result(r));
  issuing_now = l.either(issuing, rt);
  issue_first_now = l.either(issue_first, rt);
  ia_now = l.b.select(rt, r.at("address"), ia);
  ileft_now = l.b.select(rt, r.at("length"), ileft);
  im_now = l.b.select(rt, l.eq(started, 0), im);
  icode_now = l.b.select(rt, started, icode);
  initial_now = l.b.select(rt, o.local_result(r), initial_code);
  if (p.hresp_width == 2) {
    const auto aborted = l.both(l.inv(rt), read_aborted);
    im_now = l.both(im_now, l.inv(aborted));
    icode_now = l.b.select(aborted, target_error, icode_now);
  }
}

void AhbOutlet::abort_state() {
  if (p.hresp_width != 2)
    return;
  l.b.update(abort_pending,
             l.b.select(abort_start, one, l.b.select(complete, zero, abort_pending)));
  for (unsigned k = 0; k < 3; ++k) {
    const auto head = l.both(wh, l.eq(win, k));
    const auto fault = l.both(abort_start, l.both(d_write, l.eq(d_packet, k)));
    const auto aborted = contexts[k].at("abort");
    l.b.update(aborted, l.b.select(fault, one, l.b.select(head, zero, aborted)));
  }
  for (unsigned k = 0; k < 2; ++k) {
    const auto fault = l.both(abort_start, l.both(l.inv(d_write), l.eq(d_packet, k)));
    const auto head = l.both(rt, l.eq(rpacket_now, k));
    l.b.update(read_aborts[k], l.b.select(fault, one, l.b.select(head, zero, read_aborts[k])));
  }
}

// The packet shape follows its beats at the head: INCR4/8/16 or
// WRAP4/8/16, SINGLE for one beat or a two-beat WRAP, INCR otherwise; byte and halfword packets
// and bursts that run past the window stay single transfers or INCR. HSIZE is the beat's AxSIZE
// and HADDR steps by 2^AxSIZE. The first transfer is NONSEQ and later ones SEQ; while the next
// transfer is not ready the bus shows BUSY and belongs to the burst. Writes cannot promise a
// fixed count before their strobes arrive: incrementing writes use INCR, wrapping writes use
// SINGLE. From a partial word onward, writes use NONSEQ SINGLE transfers (AHB-Lite 3.5.2). A
// block holds HBURST until it completes: BUSY appears only inside a block. The next block's
// NONSEQ follows the last transfer of a block directly when it is ready.
ValueId AhbOutlet::burst_code(const Fields &f, const std::string &dir) {
  const auto is = [&](unsigned n) { return l.eq(f.at("length"), n - 1); };
  const auto code_at = [&](unsigned s) {
    const std::uint64_t beat = std::uint64_t{1} << s;
    const unsigned wide = bits + 1;
    const auto address =
        widen(l.b.binary(Op::BitAnd, f.at("address"), l.b.constant(bits, space - beat)), wide);
    const auto in_kib = l.b.binary(Op::BitAnd, address, l.b.constant(wide, 0x400 - beat));
    const auto window_end = p.mappings.empty() ? l.b.constant(wide, space)
                                               : mapping_decode::end(l, address, wide, p.mappings);
    auto incr = l.b.select(is(1), l.b.constant(3, 0), l.b.constant(3, 1));
    auto wrap = l.b.constant(3, 0);
    // Only these lengths can use a fixed HBURST, so their boundary spans are constants.
    for (unsigned k = 0; k < 3; ++k) {
      const unsigned n = 4U << k;
      const auto span = l.b.constant(wide, n * beat);
      auto past_incr =
          l.b.binary(Op::UnsignedLess, l.b.constant(wide, 0x400), l.plus(in_kib, span));
      auto past_wrap = zero;
      if (!p.mappings.empty()) {
        past_incr = l.either(past_incr, l.below(window_end, l.plus(address, span)));
        const auto container =
            l.both(address, l.b.constant(wide, (space * 2 - 1) & ~(n * beat - 1)));
        past_wrap = l.below(window_end, l.plus(container, span));
      }
      incr = l.b.select(is(n), l.b.select(past_incr, l.num(3, 1), l.num(3, 3 + k * 2)), incr);
      const auto wrapped = l.b.select(past_wrap, l.num(3, 0), l.num(3, 2 + k * 2));
      wrap = l.b.select(is(n), wrapped, wrap);
    }
    if ((16U << s) > 0x400)
      wrap = l.bind(dir + ".wrap_kib", l.b.select(is(16), l.num(3, 0), wrap));
    auto code = l.b.select(l.eq(f.at("burst"), 2), wrap, incr);
    if (p.fixed_single)
      code = l.b.select(l.eq(f.at("burst"), 0), l.b.constant(3, 0), code);
    return code;
  };
  if (!wide_bus)
    return l.b.select(l.eq(f.at("size"), 2), code_at(2), l.b.constant(3, 0));
  auto code = l.b.constant(3, 0);
  for (unsigned s = 2; s <= max_size; ++s)
    code = l.b.select(l.eq(f.at("size"), s), code_at(s), code);
  return code;
}

// Write acceptance: the window result with its exclusive adjustment, the word
// as the engine sees it in the cycle it arrives, and the per-context HBURST a
// head takes; then the read burst state as it loads and the orphan copy.
void AhbOutlet::write_accept() {
  // The current word, whose data drives HWDATA, stays until HREADY accepts the address phase of
  // its last transfer (at once without one, or after expiry); the next word enters in that cycle.
  const auto accepted = l.both(accept, owner);
  leaving = l.b.select(sent, timeout ? l.either(accepted, failed) : accepted, l.eq(wm, 0));
  if (p.hresp_width == 2)
    leaving = l.either(leaving, l.both(context("abort", wp), l.inv(d_valid)));
  wr_ready = active(
      l.both(l.either(l.inv(wf), leaving), l.either(receiving, l.inv(context("valid", win)))));
  out("wreq", "ready", wr_ready);
  wt = l.bind("w.accept", l.both(w.at("valid"), wr_ready));
  wh = l.bind("w.head", l.both(wt, l.inv(receiving)));
  const auto window_code = o.local_result(w);
  // Network exclusive monitor: a failing write (judged at its packet head
  // and held for the packet) is served with no strobes.
  if (monitored) {
    failing = o.watch(w);
    w_fail = context("fail", win);
    fail_here = l.b.select(wh, failing, w_fail);
  }
  // A failed exclusive write answers OKAY, a target timeout included.
  wcode = timeout && monitored ? l.b.select(fail_here, window_code, isolate(window_code))
                               : isolate(window_code);
  auto served = l.eq(wcode, 0);
  if (monitored)
    served = l.both(served, l.inv(fail_here));
  wmask = l.b.select(served, w.at("be"), l.b.constant(lanes, 0));
  if (p.hresp_width == 2)
    wmask = l.b.select(l.both(receiving, context("abort", win)), l.num(lanes, 0), wmask);
  // Effective strobes: a word without target access counts as partial. A
  // wide word is full when its strobes cover the beat's AxSIZE container.
  not_full = wide_bus
                 ? l.inv(l.b.binary(Op::Equal, wmask, o.read_mask(w.at("address"), w.at("size"))))
                 : l.inv(l.eq(wmask, 15));
  // A write word is taken while the current entry is empty or its word leaves in this cycle, and
  // is worked on in the cycle it arrives, as if it were already current (the registers still load
  // it), so the entry adds no cycle before its address phase: the current word and its packet's
  // context as the engine sees them now.
  enter_now = wt;
  const auto head_now = l.both(enter_now, wh);
  wf_now = l.either(wf, enter_now);
  wp_now = l.b.select(enter_now, win, wp);
  wa_now = l.b.select(enter_now, w.at("address"), wa);
  wl_now = l.b.select(enter_now, w.at("last"), wl);
  wm_now = l.b.select(enter_now, wmask, wm);
  if (p.hresp_width == 2)
    wm_now = l.b.select(l.both(context("abort", wp_now), l.inv(wh)), l.num(lanes, 0), wm_now);
  // The HBURST a head takes in each context.
  for (unsigned k = 0; k < 3; ++k)
    head_burst.push_back(burst_code(w, "w" + std::to_string(k)));
  const auto wctx = [&](const std::string &name, ValueId at_head) {
    return l.b.select(head_now, at_head, context(name, wp_now));
  };
  for (const auto &[name, value] : wd)
    wd_now[name] = w.contains(name) ? wctx(name, w.at(name)) : value;
  wd_now["start"] = wctx("start", l.b.slice(w.at("start_address"), 0, max_size));
  // Per direction: the packet's HBURST, whether SEQ is due, transfers left, whether a write word
  // was partial; bus.open holds the bus for an open burst of bus.open_write's direction.
  w_burst = wctx("hburst", l.b.select(l.eq(win, 2), head_burst[2],
                                      l.b.select(l.eq(win, 1), head_burst[1], head_burst[0])));
  w_seq = wctx("seq", zero);
  w_left = wctx("left", widen(w.at("length"), cb));
  // A word entering the current entry adds its own partial strobes to its packet's.
  w_partial =
      l.b.select(enter_now, l.b.select(wh, not_full, l.either(context("partial", win), not_full)),
                 context("partial", wp));
  r_burst = l.state("r.hburst", 3);
  r_seq = l.state("r.seq", 1);
  r_left = l.state("r.left", cb);
  open = l.state("bus.open", 1);
  open_write = l.state("bus.open_write", 1);
  // Each direction keeps its block HBURST and remaining transfers.
  // The bus shows BUSY only inside a block.
  w_block = wctx("block", l.b.constant(3, 0));
  r_block = l.state("r.block", 3);
  w_block_left = wctx("block_left", l.b.constant(cb, 0));
  r_block_left = l.state("r.block_left", cb);
  shown = l.state("bus.burst", 1);

  // After expiry the read packet already held keeps reaching the target: the bus issues its
  // remaining transfers from an orphan copy of the issue beat and descriptor taken in the first
  // failed cycle and discards their data, while the source side walks the live copy. Nothing else
  // starts, and a packet accepted after expiry leaves the bus-only burst state alone.
  bus_rd = rd_now;
  bus_issuing = issuing_now;
  bus_ia = ia_now;
  bus_ileft = ileft_now;
  bus_im = im_now;
  live_rt = rt;
  if (timeout) {
    orphan = o.orphan({{"issuing", 1U}, {"address", bits}, {"left", p.len_bits}, {"transfer", 1U}},
                      rd_now, OutletBase::captured());
    for (const auto *name : OutletBase::captured())
      bus_rd[name] = l.b.select(failed, orphan.packet.at(name), rd_now.at(name));
    orphan.packet["result"] = l.latch("timeout.orphan.result", initial_now, orphan.capture);
    bus_issuing = l.b.select(failed, orphan.word.at("issuing"), issuing_now);
    bus_ia = l.b.select(failed, orphan.word.at("address"), ia_now);
    bus_ileft = l.b.select(failed, orphan.word.at("left"), ileft_now);
    bus_im = l.b.select(failed, orphan.word.at("transfer"), im_now);
    live_rt = l.both(rt, l.inv(failed));
  }
  // The read burst state as it loads (a packet accepted after expiry leaves it alone).
  const auto r_burst_in = burst_code(r, "r");
  r_burst_now = l.b.select(live_rt, r_burst_in, r_burst);
  r_seq_now = l.b.select(live_rt, zero, r_seq);
  r_left_now = l.b.select(live_rt, l.b.unary(Op::ZeroExtend, r.at("length"), cb), r_left);
}

// Transfer issue: a write word with lanes left, or a read beat with lanes
// left and response room, of the direction that holds an open burst, and the
// transfer's address and size from its byte-lane shape.
void AhbOutlet::issue_transfers() {
  owes = l.inv(l.eq(wm_now, 0));
  const auto w_side = l.either(l.inv(open), open_write);
  const auto r_side = l.either(l.inv(open), l.inv(open_write));
  auto wcan = l.both(l.both(wf_now, owes), w_side);
  const auto r_beat = l.both(bus_issuing, bus_im);
  // An orphan transfer needs no response room.
  split_started = l.state("r.subread.started", 1);
  const auto reserved = l.both(split_started, l.inv(live_rt));
  auto rcan = l.both(l.both(r_beat, timeout ? l.either(l.either(room, reserved), failed)
                                            : l.either(room, reserved)),
                     r_side);
  rcan = l.both(rcan, l.inv(split_pending));
  // The network delivers one direction at a time (arrival order): a write
  // word goes first only when both are offered on their own.
  choose_w = wcan;
  if (timeout)
    choose_w = l.both(choose_w, l.inv(failed));
  // A transfer enters the address phase when HREADY accepts the current one or the bus is quiet. A
  // BUSY changes to the next transfer only with HREADY high.
  const auto quiet =
      l.bind("bus.quiet", l.both(l.inv(a_valid), l.both(l.inv(shown), l.inv(d_valid))));
  auto gate = active(l.either(hready, quiet));
  if (p.hresp_width == 2)
    gate = l.both(gate, l.both(l.inv(abort_pending), l.either(l.inv(a_valid), accept)));
  // After expiry only orphan reads, from the cycle after the copy.
  if (timeout)
    gate = l.both(gate, l.either(l.inv(failed), orphan.taken));
  w_issue = l.both(gate, choose_w);
  r_issue = l.both(gate, l.both(l.inv(choose_w), rcan));
  issue = l.bind("bus.issue", l.either(w_issue, r_issue));

  // A beat of a one-beat or FIXED packet covers the bytes from its start
  // address (the beat address is its AxSIZE container) to the container end;
  // block tells whether they form an aligned power-of-two block of size.
  const unsigned lane_bits = max_size + 1;
  struct Shape {
    ValueId single, offset, block, size;
  };
  const auto shape = [&](const Fields &f, ValueId skew) {
    const auto span = stride(l, f.at("size"), lane_bits, max_size);
    const auto offset = l.b.binary(Op::BitAnd, widen(skew, lane_bits), l.dec(span));
    const auto rest = l.b.binary(Op::Subtract, span, offset);
    ValueId size = l.b.constant(3, 0);
    for (unsigned k = 1; k <= max_size; ++k)
      size = l.b.select(l.b.slice(rest, k, 1), l.b.constant(3, k), size);
    return Shape{l.either(l.eq(f.at("length"), 0), l.eq(f.at("burst"), 0)), offset,
                 l.eq(l.b.binary(Op::BitAnd, rest, l.dec(rest)), 0), size};
  };
  // Write, without byte enables on the bus: a word whose strobes are its whole AxSIZE container,
  // the start block of a one-beat or FIXED beat, or exactly the lower or upper half of the target
  // word goes out in one transfer of that size; any other word goes out byte by byte from its
  // lowest strobed lane (w.peeled once it has issued a byte).
  peeled = l.state("w.peeled", 1);
  const auto peeled_now = l.b.select(enter_now, zero, peeled);
  const auto w_size_in = wd_now.at("size");
  const auto w_container = o.read_mask(wa_now, w_size_in);
  const auto full_word = l.b.binary(Op::Equal, wm_now, w_container);
  const auto lowest =
      l.b.binary(Op::BitAnd, wm_now, l.b.binary(Op::Subtract, l.b.constant(lanes, 0), wm_now));
  std::optional<ValueId> lane;
  for (unsigned j = 0; j < max_size; ++j) {
    const auto bit = l.inv(l.eq(
        l.b.binary(Op::BitAnd, lowest, pattern(l, lanes, [&](unsigned i) { return (i >> j) & 1; })),
        0));
    lane = lane ? l.b.concat(bit, *lane) : bit;
  }
  l.bind("bus.lane", *lane);
  const auto ws = shape(wd_now, wd_now.at("start"));
  const auto to_end =
      l.b.binary(Op::BitAnd, w_container, l.b.unary(Op::BitNot, l.dec(lowest), lanes));
  const auto start_lane =
      l.b.binary(Op::BitOr, l.b.slice(wa_now, 0, max_size), l.b.slice(ws.offset, 0, max_size));
  const auto start_block =
      l.bind("bus.start_block",
             l.both(l.both(ws.single, ws.block), l.both(l.b.binary(Op::Equal, *lane, start_lane),
                                                        l.b.binary(Op::Equal, wm_now, to_end))));
  const auto lower = pattern(l, lanes, [&](unsigned i) { return i < lanes / 2; });
  const auto half_word =
      l.either(l.b.binary(Op::Equal, wm_now, lower),
               l.b.binary(Op::Equal, wm_now, l.b.unary(Op::BitNot, lower, lanes)));
  const auto whole_word =
      l.bind("bus.whole_word",
             l.both(l.inv(peeled_now), l.either(l.either(full_word, start_block), half_word)));
  submask = l.b.select(whole_word, wm_now, lowest);
  const auto w_size =
      l.b.select(whole_word,
                 l.b.select(full_word, w_size_in,
                            l.b.select(start_block, ws.size, l.b.constant(3, max_size - 1))),
                 l.b.constant(3, 0));
  const auto w_address =
      l.b.binary(Op::BitOr, l.b.binary(Op::BitAnd, wa_now, l.b.constant(bits, space - lanes)),
                 widen(*lane, bits));
  // A one-beat or FIXED read covers exactly the suffix of its size container.
  // An unaligned suffix uses aligned subreads without touching preceding bytes.
  const auto skew_in = l.b.slice(r.at("start_address"), 0, max_size);
  const auto skew = l.b.select(live_rt, skew_in, l.latch("r.start_offset", skew_in, live_rt));
  const auto rs = shape(bus_rd, skew);
  const auto single = l.bind("r.single", rs.single);
  const auto r_size = read_shape(single, rs.block, rs.offset);
  const auto r_offset = l.b.select(split_read, split_cursor, rs.offset);
  const auto r_address =
      l.b.select(single, l.b.binary(Op::BitOr, bus_ia, widen(r_offset, bits)), bus_ia);
  address = l.b.select(choose_w, w_address, r_address);
  const auto size = l.b.select(choose_w, w_size, r_size);
  // The issued transfer's flow: that of the mapping holding its global address.
  if (p.flows > 1)
    l.b.update(bus_flow,
               l.b.select(issue,
                          mapping_decode::flow(l, address, bits, p.mappings, l.width(bus_flow)),
                          bus_flow));
  address = o.localized(address);
  // Narrow only after the full-width decode and base subtraction.
  if (p.address_bits < bits)
    address = l.b.slice(address, 0, p.address_bits);
  // The address phase registers keep the last issued transfer, whose HADDR a BUSY steps from.
  bus_address = l.latch("bus.address", address, issue);
  bus_size = l.latch("bus.size", size, issue);
}

// Split a read beat into transfers that fit its remaining byte range.
ValueId AhbOutlet::read_shape(ValueId single, ValueId block, ValueId offset) {
  const auto width = max_size + 1;
  split_read = l.both(single, l.inv(block));
  split_first = l.inv(l.both(split_started, l.inv(live_rt)));
  const auto cursor = l.state("r.subread.cursor", width);
  split_cursor = l.b.select(split_first, offset, cursor);
  const auto span = stride(l, bus_rd.at("size"), width, max_size);
  const auto remaining = l.minus(span, split_cursor);
  auto size = l.num(3, 0);
  for (unsigned k = 1; k <= max_size; ++k) {
    const auto aligned = l.eq(l.b.slice(split_cursor, 0, k), 0);
    const auto fits = l.inv(l.below(remaining, l.num(width, 1U << k)));
    size = l.b.select(l.both(aligned, fits), l.num(3, k), size);
  }
  const auto next = l.plus(split_cursor, stride(l, size, width, max_size));
  split_last = l.b.binary(Op::Equal, next, span);
  const auto subissue = l.both(r_issue, split_read);
  split_done = l.both(r_issue, l.either(l.inv(split_read), split_last));
  l.b.update(cursor, l.b.select(subissue, next, cursor));
  l.b.update(split_started,
             l.b.select(subissue, l.inv(split_last), l.b.select(live_rt, zero, split_started)));
  if (p.hresp_width == 2)
    l.b.update(split_started, l.b.select(abort_finish, zero,
                                         l.b.select(subissue, l.inv(split_last),
                                                    l.b.select(live_rt, zero, split_started))));
  l.b.update(split_pending,
             l.b.select(subissue, one, l.b.select(raw_read_complete, zero, split_pending)));
  const auto a_split =
      l.latch("r.subread.address_phase", l.both(split_read, l.inv(choose_w)), issue);
  const auto a_last = l.latch("r.subread.address_last", split_last, issue);
  l.b.update(d_split, l.b.select(accept, a_split, d_split));
  l.b.update(d_split_last, l.b.select(accept, a_last, d_split_last));
  return l.b.select(single, size, bus_rd.at("size"));
}

void AhbOutlet::gather_read() {
  split_data = l.state("r.subread.data", word_bits);
  split_result = l.state("r.subread.result", 2);
  const auto offset = l.latch("r.subread.data_offset", l.b.slice(bus_address, 0, max_size), accept);
  const auto size = l.latch("r.subread.data_size", bus_size, accept);
  const auto mask = o.read_mask(widen(offset, bits), size);
  std::optional<ValueId> gathered;
  for (unsigned lane = 0; lane < lanes; ++lane) {
    const auto byte = l.b.select(l.b.slice(mask, lane, 1), l.b.slice(hrdata, lane * 8, 8),
                                 l.b.slice(split_data, lane * 8, 8));
    gathered = gathered ? l.b.concat(byte, *gathered) : byte;
  }
  const auto code = merge_error(l, split_result, bus_result);
  const auto begins = l.both(r_issue, l.both(split_read, split_first));
  const auto collect = l.both(raw_read_complete, d_split);
  l.b.update(split_data,
             l.b.select(begins, l.num(word_bits, 0), l.b.select(collect, *gathered, split_data)));
  l.b.update(split_result, l.b.select(begins, ok, l.b.select(collect, code, split_result)));
  read_data = l.b.select(d_split, *gathered, hrdata);
  read_result = l.b.select(d_split, code, bus_result);
  if (p.hresp_width == 2) {
    read_data = l.b.select(l.either(cancel_read, abort_pending), l.num(word_bits, 0), read_data);
    read_result = l.b.select(l.either(cancel_read, abort_pending), target_error, read_result);
  }
}

void AhbOutlet::burst_blocks() {
  mode_w = l.both(l.inv(l.eq(w_burst, 0)), l.inv(w_partial));
  mode_r = l.inv(l.eq(r_burst_now, 0));
  mode = l.b.select(choose_w, mode_w, mode_r);
  const auto seq = l.b.select(choose_w, w_seq, r_seq_now);
  left = l.b.select(choose_w, w_left, r_left_now);
  // At a block start (no SEQ due) the block HBURST follows from the packet's.
  // Otherwise the block continues. seq is the block's SEQ state.
  const unsigned wide = bits + 1;
  // A block of k beats of 2^s bytes fits the beats left in the packet,
  // the 1 KiB boundary and the window end.
  const auto fits_at = [&](unsigned s, ValueId address_now, ValueId words_after, unsigned k) {
    const std::uint64_t step = std::uint64_t{1} << s;
    const auto word =
        widen(l.b.binary(Op::BitAnd, address_now, l.b.constant(bits, space - step)), wide);
    const auto in_kib = l.b.binary(Op::BitAnd, word, l.b.constant(wide, 0x400 - step));
    const auto kib_end = l.plus(in_kib, l.b.constant(wide, step * k));
    const auto enough = l.b.binary(Op::UnsignedLess, words_after, l.b.constant(cb, k - 1));
    auto fit = l.both(l.inv(enough),
                      l.inv(l.b.binary(Op::UnsignedLess, l.b.constant(wide, 0x400), kib_end)));
    if (!p.mappings.empty()) {
      const auto end = l.plus(word, l.b.constant(wide, step * k));
      const auto window_end = mapping_decode::end(l, word, wide, p.mappings);
      fit = l.both(fit, l.inv(l.b.binary(Op::UnsignedLess, window_end, end)));
    }
    return fit;
  };
  // Bursts carry beats of 2^2 up to 2^max_size bytes.
  const auto fits = [&](ValueId size_now, ValueId address_now, ValueId words_after, unsigned k) {
    if (max_size == 2)
      return fits_at(2, address_now, words_after, k);
    auto fit = fits_at(2, address_now, words_after, k);
    for (unsigned s = 3; s <= max_size; ++s)
      fit = l.b.select(l.eq(size_now, s), fits_at(s, address_now, words_after, k), fit);
    return fit;
  };
  const auto start = [&](bool write, ValueId packet_code, ValueId size_now, ValueId address_now,
                         ValueId words_after) {
    auto code = packet_code;
    // Complete writes stream without promising a fixed count. Only an incrementing
    // packet may use undefined INCR. A wrap or a port that forbids INCR uses SINGLE.
    const bool stream_write = write;
    if (stream_write) {
      const auto incrementing = l.b.slice(packet_code, 0, 1);
      code = p.undefined_length_bursts
                 ? l.b.select(incrementing, l.b.constant(3, 1), l.b.constant(3, 0))
                 : l.b.constant(3, 0);
    }
    auto n = words_after;
    if (!p.undefined_length_bursts) {
      const auto incr = l.eq(code, 1);
      if (!stream_write) {
        const auto f16 = fits(size_now, address_now, words_after, 16);
        const auto f8 = fits(size_now, address_now, words_after, 8);
        const auto f4 = fits(size_now, address_now, words_after, 4);
        // HBURST INCR16, INCR8, INCR4 or SINGLE, and the transfers after the first.
        const auto incr4 = l.b.constant(3, 3), single = l.b.constant(3, 0);
        const auto blocked =
            l.b.select(f16, l.b.constant(3, 7),
                       l.b.select(f8, l.b.constant(3, 5), l.b.select(f4, incr4, single)));
        const auto after4 = l.b.constant(cb, 3), after1 = l.b.constant(cb, 0);
        const auto blocked_count =
            l.b.select(f16, l.b.constant(cb, 15),
                       l.b.select(f8, l.b.constant(cb, 7), l.b.select(f4, after4, after1)));
        code = l.b.select(incr, blocked, code);
        n = l.b.select(incr, blocked_count, n);
      }
    }
    return std::pair{code, n};
  };
  const auto [w_start, w_start_count] = start(true, w_burst, wd_now.at("size"), wa_now, w_left);
  const auto [r_start, r_start_count] =
      start(false, r_burst_now, bus_rd.at("size"), bus_ia, r_left_now);
  w_code = l.b.select(w_seq, w_block, w_start);
  w_count = l.b.select(w_seq, w_block_left, w_start_count);
  r_code = l.b.select(r_seq_now, r_block, r_start);
  r_count = l.b.select(r_seq_now, r_block_left, r_start_count);
  const auto w_mode = l.both(l.inv(l.eq(w_code, 0)), l.inv(w_partial));
  const auto r_mode = l.inv(l.eq(r_code, 0));
  w_more = l.both(w_mode, l.inv(l.eq(w_count, 0)));
  r_more = l.both(r_mode, l.inv(l.eq(r_count, 0)));
  const auto block_mode = l.b.select(choose_w, w_mode, r_mode);
  const auto block_code = l.b.select(choose_w, w_code, r_code);
  const auto next_burst = l.b.select(block_mode, block_code, l.b.constant(3, 0));
  // An INCR burst (odd HBURST) restarts with NONSEQ at a 1 KiB boundary; a
  // WRAP container never crosses one.
  const unsigned address_width = l.b.ir.values.at(address.index).width;
  const auto incr_burst = l.b.slice(block_code, 0, 1);
  const auto kib =
      address_width >= 10 ? l.both(incr_burst, l.eq(l.b.slice(address, 0, 10), 0)) : zero;
  const auto next_trans = l.b.select(l.both(l.both(block_mode, seq), l.inv(kib)),
                                     l.b.constant(2, 3), l.b.constant(2, 2));
  bus_burst = l.state("bus.hburst", 3);
  bus_trans = l.state("bus.htrans", 2);
  l.b.update(bus_burst, l.b.select(issue, next_burst, bus_burst));
  l.b.update(bus_trans, l.b.select(issue, next_trans, bus_trans));
  if (p.hresp_width == 2)
    l.b.update(bus_trans,
               l.b.select(abort_finish, l.num(2, 2), l.b.select(issue, next_trans, bus_trans)));
}

// Write words and packets retire: the current word leaves when its last
// transfer is issued (or at once without one), the packet's B is framed once
// every transfer of the packet has completed, and the burst releases the bus.
void AhbOutlet::retire_write() {
  bus_result = l.b.select(l.inv(l.eq(hresp, 0)), target_error, ok);

  // Write words. The current word leaves when HREADY accepts the address phase of its last
  // transfer (at once without one, or after expiry) and the next word takes its place; the last
  // word of the packet then closes it, and its B is sent once no transfer of the packet is on the
  // bus.
  w_rest = l.b.binary(Op::BitAnd, wm_now, l.b.unary(Op::BitNot, submask, lanes));
  // finish_w: the held word leaves, or a word taken into an empty entry has no transfer.
  finish_w = active(l.b.select(wf, leaving, l.both(wt, l.eq(wmask, 0))));
  finish_last = l.b.select(wf, wl, w.at("last"));
  finish_packet = l.b.select(wf, wp, win);
  // The oldest context can retire with its final data completion. Another
  // packet on the bus never keeps its B waiting or contributes to its code.
  const auto out_a = l.both(l.both(a_valid, owner), same(a_packet, wout));
  const auto out_d = l.both(l.both(d_valid, d_write), same(d_packet, wout));
  const auto w_raw_left = l.either(out_a, l.both(out_d, l.inv(complete)));
  const auto w_flight =
      l.bind("w.in_flight", timeout ? l.both(w_raw_left, l.inv(failed)) : w_raw_left);
  bpop = l.both(active(bf), wrsp_ready);
  // A last word without a transfer frames its B at once, as on APB; one taken in this cycle
  // closes its packet for the next (its B never answers in the cycle its last word arrives).
  // It reads the held word's leaving only, never the network's write word.
  const auto closes_now =
      l.both(l.both(active(l.both(wf, leaving)), wl), l.both(l.eq(wm, 0), same(wp, wout)));
  const auto framed =
      l.both(context("valid", wout),
             l.both(l.either(context("closing", wout), closes_now), l.inv(w_flight)));
  bpush = l.bind("w.response_push", active(l.both(framed, l.either(l.inv(bf), bpop))));
  // A silent B is dropped here; its context still retires.
  bstore = report_only ? l.both(bpush, l.inv(failed)) : bpush;
  // The B face: the stored B, else the B framed now passing the empty store straight through
  // (its valid never reads the network ready).
  b_valid =
      l.either(bf, active(l.both(l.both(framed, l.inv(bf)), report_only ? l.inv(failed) : one)));
  l.b.update(wp, l.b.select(wt, win, wp));
  l.b.update(wa, l.b.select(wt, w.at("address"), wa));
  // After expiry the data stays with a write still waiting for HREADY on the bus.
  l.b.update(wdata, l.b.select(timeout ? l.both(wt, l.inv(failed)) : wt, w.at("data"), wdata));
  l.b.update(wl, l.b.select(wt, w.at("last"), wl));
  l.b.update(r_burst, r_burst_now);

  // Burst sequencing (per direction, at its issues).
  l.b.update(r_seq, l.b.select(r_issue, r_more, r_seq_now));
  l.b.update(r_block, l.b.select(r_issue, r_code, r_block));
  l.b.update(r_block_left, l.b.select(r_issue, l.dec(r_count), r_block_left));
  l.b.update(r_left, l.b.select(l.both(r_issue, mode_r), l.dec(r_left_now), r_left_now));
  if (p.hresp_width == 2)
    l.b.update(r_seq,
               l.b.select(l.both(abort_start, l.both(l.inv(d_write), same(rpacket_now, d_packet))),
                          zero, l.b.select(r_issue, r_more, r_seq_now)));
  // The burst holds the bus until its last transfer is issued, or its
  // packet ends without one (zero strobes, or beats outside the window).
  const auto more = l.both(mode, l.inv(l.eq(left, 0)));
  const auto w_end = l.both(l.both(finish_w, finish_last), open_write);
  // A read packet relinquishes its issue ownership at its final beat;
  // queued responses do not hold the bus for an already issued packet.
  const auto r_end =
      l.both(l.both(bus_issuing, l.eq(bus_ileft, 0)), l.both(l.eq(bus_im, 0), l.inv(open_write)));
  // A failed target keeps an unfinished write burst in BUSY: it never ends
  // early. An orphan read burst runs to its end.
  auto ends = l.either(timeout ? l.both(w_end, l.inv(failed)) : w_end, r_end);
  // A packet can end without a transfer in any cycle. While the bus shows
  // BUSY with HREADY low the release waits: in a waited cycle a
  // fixed-length burst may change BUSY only to SEQ, never to IDLE.
  auto pending = l.state("bus.release", 1);
  auto release = l.both(l.either(ends, pending), l.either(hready, l.inv(shown)));
  if (timeout)
    release = l.both(release, l.inv(l.both(failed, open_write)));
  l.b.update(pending, l.b.select(issue, zero, l.b.select(release, zero, l.either(pending, ends))));
  l.b.update(open, l.b.select(issue, more, l.b.select(release, zero, open)));
  l.b.update(open_write, l.b.select(issue, choose_w, open_write));
  // BUSY only while the current block owes transfers.
  l.b.update(shown, l.b.select(issue, l.b.select(choose_w, w_more, r_more),
                               l.b.select(release, zero, shown)));
  if (p.hresp_width == 2) {
    l.b.update(open, l.b.select(abort_release, zero,
                                l.b.select(issue, more, l.b.select(release, zero, open))));
    l.b.update(shown, l.b.select(abort_release, zero,
                                 l.b.select(issue, l.b.select(choose_w, w_more, r_more),
                                            l.b.select(release, zero, shown))));
  }
}

// The bus signals. Between the transfers of an open burst the bus shows BUSY
// with the next address of the burst (WRAP turns inside its container).
void AhbOutlet::drive_bus() {
  auto cache = l.b.select(choose_w, wd_now.at("cache"), bus_rd.at("cache"));
  auto prot = l.b.select(choose_w, wd_now.at("protection"), bus_rd.at("protection"));
  ValueId hprot;
  if (p.attributes) {
    // HPROT = {m[3], m[2], m[1], ~m[0]}: the data/opcode bit is inverted.
    const auto m = map_attributes(l, p.attributes->hprot, cache, prot, choose_w);
    hprot = l.b.concat(l.b.slice(m, 1, 3), l.inv(l.b.slice(m, 0, 1)));
  } else {
    hprot = l.b.concat(l.b.slice(cache, 0, 2),
                       l.b.concat(l.b.slice(prot, 0, 1), l.inv(l.b.slice(prot, 2, 1))));
  }
  auto bus_prot = l.latch("bus.protection", hprot, issue);
  const auto driven = active(l.both(a_valid, l.inv(l.either(abort_pending, aborted_address))));
  const auto waiting = active(l.both(l.both(shown, l.inv(a_valid)), l.inv(abort_pending)));
  const unsigned aw = p.address_bits < bits ? p.address_bits : bits;
  // The local port may be narrower than a step or a container: HADDR
  // wraps modulo 2^aw, so every constant is taken modulo 2^aw.
  const std::uint64_t address_mask = aw >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << aw) - 1;
  const auto local = [&](std::uint64_t value) { return l.b.constant(aw, value & address_mask); };
  // A burst steps by its HSIZE; a WRAP container is its beats times 2^HSIZE bytes.
  ValueId next_word;
  if (wide_bus) {
    auto stride_now = local(1);
    for (unsigned s = 1; s <= max_size; ++s)
      stride_now = l.b.select(l.eq(bus_size, s), local(std::uint64_t{1} << s), stride_now);
    next_word = l.plus(bus_address, stride_now);
  } else {
    next_word = l.b.binary(Op::Subtract, bus_address, l.b.binary(Op::Subtract, local(0), local(4)));
  }
  auto container = local(0);
  for (const auto &[code, beats] : {std::pair{2U, 4U}, std::pair{4U, 8U}, std::pair{6U, 16U}}) {
    auto bytes = local(4 * beats - 1);
    if (wide_bus)
      for (unsigned s = 3; s <= max_size; ++s)
        bytes = l.b.select(l.eq(bus_size, s), local((std::uint64_t{beats} << s) - 1), bytes);
    container = l.b.select(l.eq(bus_burst, code), bytes, container);
  }
  const auto wrapped = l.b.binary(
      Op::BitOr, l.b.binary(Op::BitAnd, bus_address, l.b.unary(Op::BitNot, container, aw)),
      l.b.binary(Op::BitAnd, next_word, container));
  const auto busy_address = l.b.select(l.eq(container, 0), next_word, wrapped);
  auto hwdata = l.latch("bus.hwdata", wdata, accept);
  out("ahb", "haddr", l.b.select(waiting, busy_address, bus_address));
  out("ahb", "hsize", bus_size);
  out("ahb", "hburst", bus_burst);
  out("ahb", "hprot", bus_prot);
  out("ahb", "hmastlock", zero);
  out("ahb", "hwrite", owner);
  const auto hsel = l.either(driven, waiting);
  out("ahb", "hsel", hsel);
  if (p.flows > 1) {
    auto vector = selected(hsel, 0);
    for (unsigned k = 1; k < p.flows; ++k)
      vector = l.b.concat(selected(hsel, k), vector);
    l.b.update(response_select, l.b.select(hready, vector, response_select));
  }
  out("ahb", "htrans",
      l.b.select(driven, bus_trans, l.b.select(waiting, l.b.constant(2, 1), l.b.constant(2, 0))));
  out("ahb", "hwdata", hwdata);
  out("ahb", "hready", active(hready));

  fail_now = timeout ? o.watchdog(l.either(l.either(w.at("valid"), r.at("valid")),
                                           l.either(l.bindings.at("w.busy"),
                                                    l.either(issuing, l.inv(l.eq(held, 0))))),
                                  l.either(b_valid, responding))
                     : zero;
  l.b.update(a_valid,
             l.b.select(issue, one, l.b.select(l.either(accept, discard_address), zero, a_valid)));
  l.b.update(owner, l.b.select(issue, choose_w, owner));
  l.b.update(d_valid, l.b.select(accept, one, l.b.select(complete, zero, d_valid)));
  l.b.update(d_write, l.b.select(accept, owner, d_write));
  l.b.update(a_packet, l.b.select(issue, l.b.select(choose_w, wp_now, rpacket_now), a_packet));
  l.b.update(d_packet, l.b.select(accept, a_packet, d_packet));
}

// Each packet's result merges only its accepted words and data phases.
// The response snapshot permits another packet to progress behind a held B.
// A word that enters and leaves in one cycle leaves the current entry empty.
void AhbOutlet::update_write() {
  l.b.update(wf, l.b.select(wt, l.either(wf, l.inv(finish_w)), l.b.select(finish_w, zero, wf)));
  // Every lane of the current word is on the bus: it leaves with its last address phase.
  const auto issued_all = l.both(w_issue, l.eq(w_rest, 0));
  l.b.update(sent, l.b.select(wt, issued_all,
                              l.b.select(finish_w, zero, l.b.select(issued_all, one, sent))));
  l.b.update(receiving, l.b.select(wt, l.inv(w.at("last")), receiving));
  const auto next_packet = [&](ValueId index) {
    return l.b.select(l.eq(index, 2), l.b.constant(2, 0), l.plus(index, l.b.constant(2, 1)));
  };
  l.b.update(win, l.b.select(l.both(wt, w.at("last")), next_packet(win), win));
  l.b.update(wout, l.b.select(bpush, next_packet(wout), wout));
  auto next_wm =
      l.b.select(wt, l.b.select(w_issue, w_rest, wmask), l.b.select(w_issue, w_rest, wm));
  if (timeout)
    next_wm = l.b.select(failed, l.b.constant(lanes, 0), next_wm);
  if (p.hresp_width == 2)
    next_wm = l.b.select(l.both(context("abort", wp_now), l.inv(wh)), l.num(lanes, 0), next_wm);
  l.b.update(wm, next_wm);
  // A word that keeps lanes after a transfer has started going out by bytes.
  const auto peels = l.both(w_issue, l.inv(l.eq(w_rest, 0)));
  l.b.update(peeled, l.b.select(wt, peels, l.b.select(peels, one, peeled)));
  for (unsigned k = 0; k < 3; ++k) {
    const auto &q = contexts[k];
    const auto input_here = l.eq(win, k), word_here = l.eq(wp_now, k);
    const auto head = l.both(wh, input_here), take = l.both(wt, input_here);
    const auto emitting = l.both(w_issue, word_here);
    const auto retire = l.both(bpush, l.eq(wout, k));
    auto result = l.b.select(take, l.b.select(head, wcode, merge_error(l, q.at("result"), wcode)),
                             q.at("result"));
    const auto completed = l.both(wcomplete, l.eq(d_packet, k));
    result = l.b.select(completed, merge_error(l, result, bus_result), result);
    if (timeout) {
      const auto waiting_word = l.both(word_here, owes);
      const auto pending_bus = l.either(l.both(l.both(a_valid, owner), l.eq(a_packet, k)),
                                        l.both(l.both(d_valid, d_write), l.eq(d_packet, k)));
      const auto dropped = l.either(l.both(failed, waiting_word), l.both(fail_now, pending_bus));
      result = l.b.select(dropped, merge_error(l, result, target_error), result);
    }
    results.push_back(result);
    l.b.update(q.at("result"), result);
    l.b.update(q.at("valid"), l.b.select(head, one, l.b.select(retire, zero, q.at("valid"))));
    // A one-word packet may close in the cycle its head enters.
    const auto closes_k = l.both(finish_w, l.both(finish_last, l.eq(finish_packet, k)));
    l.b.update(q.at("closing"), l.b.select(closes_k, one, l.b.select(head, zero, q.at("closing"))));
    for (const auto *name :
         {"address", "length", "size", "burst", "cache", "protection", "source", "id", "tag"})
      if (w.contains(name))
        l.b.update(q.at(name), l.b.select(head, w.at(name), q.at(name)));
    l.b.update(q.at("start"),
               l.b.select(head, l.b.slice(w.at("start_address"), 0, max_size), q.at("start")));
    l.b.update(q.at("hburst"), l.b.select(head, head_burst[k], q.at("hburst")));
    const auto partial = l.both(wt, l.both(input_here, not_full));
    l.b.update(q.at("partial"), l.b.select(head, not_full, l.either(q.at("partial"), partial)));
    // A head may also issue its first transfer as it enters.
    const auto seq_k = l.b.select(head, zero, q.at("seq"));
    l.b.update(q.at("seq"), l.b.select(emitting, w_more, seq_k));
    const auto left_now = l.b.select(head, words_left(w), q.at("left"));
    l.b.update(q.at("left"), l.b.select(l.both(emitting, mode_w), l.dec(left_now), left_now));
    l.b.update(q.at("block"), l.b.select(emitting, w_code, q.at("block")));
    l.b.update(q.at("block_left"), l.b.select(emitting, l.dec(w_count), q.at("block_left")));
    l.b.update(q.at("fail"), l.b.select(head, failing, q.at("fail")));
    l.b.update(q.at("exclusive"),
               l.b.select(head, monitored ? w.at("lock") : zero, q.at("exclusive")));
  }
}

// The B face: the stored B, else the B framed now passing the empty store
// straight through. The read descriptor loads, and the exclusive monitor
// learns the writes in flight.
void AhbOutlet::respond_write() {
  Fields response, b_face;
  for (const auto *name : {"address", "length", "burst", "size", "source", "id", "tag"})
    if (w.contains(name)) {
      const auto at_out =
          std::string_view(name) == "address" ? l.global(context(name, wout)) : context(name, wout);
      response[name] = l.latch("w.response." + std::string(name), at_out, bpush);
      b_face[name] = l.b.select(bf, response.at(name), context(name, wout));
    }
  const auto code_now =
      l.b.select(l.eq(wout, 2), results[2], l.b.select(l.eq(wout, 1), results[1], results[0]));
  const auto response_code =
      l.b.select(bf, l.latch("w.response.result", code_now, bpush), code_now);
  const auto exclusive_ok = l.both(context("exclusive", wout), l.inv(context("fail", wout)));
  const auto response_exclusive =
      l.b.select(bf, l.latch("w.response.exclusive", exclusive_ok, bpush), exclusive_ok);
  response_fail = monitored ? l.latch("w.response.fail", context("fail", wout), bpush) : zero;
  // A B the network takes as it is framed is not stored.
  const auto b_through = l.both(bstore, l.both(l.inv(bf), wrsp_ready));
  l.b.update(bf, l.b.select(l.both(bstore, l.inv(b_through)), one, l.b.select(bpop, zero, bf)));
  o.response("wrsp", b_face, b_valid, b_face.at("address"), response_code, one, one,
             l.b.constant(word_bits, 0), l.both(response_exclusive, l.eq(response_code, 0)));

  // Read packet. An exclusive read is contaminated by a write in flight
  // from its head until its B leaves: a packet context, then the stored B.
  o.load(rd, r, rt);
  if (monitored) {
    r_exclusive = l.b.select(rt, r.at("lock"), l.latch("r.exclusive", r.at("lock"), rt));
    const auto reaching = l.both(wh, l.inv(failing));
    std::vector<OutletBase::Held> writes;
    std::ranges::transform(contexts, std::back_inserter(writes), [](const Fields &q) {
      return OutletBase::Held{q.at("valid"), q.at("fail"), &q};
    });
    writes.push_back({bf, response_fail, &response});
    o.reserve(w, r, rt, reaching, reaching, writes);
  }
}

// The read issue beat: a beat with lanes goes out as one transfer; a beat
// without one (window miss, FIXED, failed target) answers its result at once,
// after the transfers in flight, and so does every beat after a failure. The
// response buffer takes completions in issue order; its head, or a completion
// passing through, drives the face.
void AhbOutlet::update_read() {
  const auto next_ia = next_address(l, ia_now, rd_now, bits, max_size);
  const auto next_code = o.next_code(initial_now, next_ia, r, rt);
  const auto skip = active(l.both(l.both(issuing_now, l.either(l.inv(im_now), failed)),
                                  l.both(l.eq(inflight, 0), buffer_room)));
  // The live issue beat advances with its own transfers only; orphan
  // transfers move the orphan copy.
  const auto reservation = l.both(r_issue, l.either(l.inv(split_read), split_first));
  const auto live_issue = timeout ? l.both(reservation, l.inv(failed)) : reservation;
  const auto finished = timeout ? l.both(split_done, l.inv(failed)) : split_done;
  const auto aborted_subread =
      p.hresp_width == 2
          ? l.both(abort_finish, l.both(l.inv(d_write), l.both(d_split, l.inv(d_split_last))))
          : zero;
  const auto advance = l.either(l.either(finished, skip), aborted_subread);
  const auto ilast = l.eq(ileft_now, 0);
  if (timeout) {
    // The orphan moves at its transfers and passes over beats outside the window without one.
    const auto moving = l.both(l.both(failed, orphan.taken),
                               l.either(split_done, l.both(bus_issuing, l.inv(bus_im))));
    const auto following = next_address(l, bus_ia, orphan.packet, bits, max_size);
    auto code = orphan.packet.at("result");
    if (o.subpage)
      code =
          l.b.select(l.eq(code, 2), code, l.b.select(o.mapped(following), ok, l.b.constant(2, 1)));
    o.move(orphan, "issuing", issuing_now, moving, l.inv(l.eq(bus_ileft, 0)));
    o.move(orphan, "address", ia_now, moving, following);
    o.move(orphan, "left", ileft_now, moving, l.dec(bus_ileft));
    o.move(orphan, "transfer", im_now, moving, l.eq(code, 0));
  }
  l.b.update(issuing, l.b.select(l.both(advance, ilast), zero, issuing_now));
  l.b.update(ia, l.b.select(advance, next_ia, ia_now));
  l.b.update(ileft, l.b.select(advance, l.dec(ileft_now), ileft_now));
  l.b.update(im, l.b.select(advance, l.eq(next_code, 0), im_now));
  l.b.update(icode, l.b.select(advance, next_code, icode_now));
  l.b.update(initial_code, initial_now);
  // Responses enter the buffer in issue order: a completed transfer, a beat
  // without one, or a transfer lost to a failure.
  auto push = l.either(rcomplete, l.either(skip, drain));
  // A silent response is dropped here; the issue beat still advances.
  if (report_only)
    push = l.both(push, l.inv(failed));
  const auto push_data = l.b.select(rcomplete, read_data, l.b.constant(word_bits, 0));
  const auto push_result =
      l.b.select(rcomplete, read_result, l.b.select(drain, target_error, isolate(icode_now)));
  l.b.update(inflight, l.minus(l.plus(inflight, l.zext(live_issue, 2)),
                               l.zext(l.either(rcomplete, drain), 2)));
  l.b.update(count, l.minus(l.plus(count, l.zext(push, 2)), l.zext(rpop, 2)));
  // The free entry: count less this cycle's pop.
  const auto at = l.b.select(rpop, l.dec(count), count);
  for (unsigned i = 0; i < 2; ++i) {
    const auto here = l.both(push, l.eq(at, i));
    l.b.update(fdata[i],
               l.b.select(here, push_data, i ? fdata[1] : l.b.select(rpop, fdata[1], fdata[0])));
    l.b.update(fresult[i], l.b.select(here, push_result,
                                      i ? fresult[1] : l.b.select(rpop, fresult[1], fresult[0])));
  }
  // Metadata enters when a transfer reserves room, before its data can
  // return. A local beat reserves and completes in the same cycle. Shift
  // it only on an actual response transfer, including packet boundaries.
  const auto reserve = l.either(live_issue, skip);
  const auto reserved_at = l.b.select(rpop, l.dec(held), held);
  Fields incoming = rd_now;
  incoming["address"] = ia_now;
  incoming["first"] = issue_first_now;
  incoming["last"] = ilast;
  incoming["exclusive"] = r_exclusive;
  if (p.response_run_bits)
    incoming["run_end"] = burst_run_end(l, rd_now, ia_now, o.word_bits, p.response_run_bits);
  for (unsigned k = 0; k < 2; ++k)
    for (const auto &[name, value] : metadata[k]) {
      const auto keep = k ? value : l.b.select(rpop, metadata[1].at(name), value);
      l.b.update(value, l.b.select(l.both(reserve, l.eq(reserved_at, k)), incoming.at(name), keep));
    }
  l.b.update(issue_first, l.b.select(advance, zero, issue_first_now));
  l.bind("r.busy", l.either(issuing, l.inv(l.eq(held, 0))));
  // The face shows the buffer head, or the completion passing through. Only
  // the lanes of the beat's AxSIZE container leave; an error carries no data.
  const auto face_data = l.b.select(offered, fdata[0], push_data);
  // Without a buffered response count is zero, so buffer_room is true and
  // drain equals lost. Keep READY out of the bypass result's combinational cone.
  const auto bypass_result =
      l.b.select(rcomplete, read_result, l.b.select(lost, target_error, isolate(icode_now)));
  const auto face_result = l.b.select(offered, fresult[0], bypass_result);
  const auto lanes_now = o.read_mask(ra, metadata[0].at("size"));
  std::optional<ValueId> data;
  for (unsigned i = 0; i < lanes; ++i) {
    const auto byte =
        l.b.select(l.b.slice(lanes_now, i, 1), l.b.slice(face_data, i * 8, 8), l.b.constant(8, 0));
    data = data ? l.b.concat(byte, *data) : byte;
  }
  o.response("rrsp", metadata[0], responding, ra, face_result, rfirst, last,
             l.b.select(l.eq(face_result, 0), *data, l.b.constant(word_bits, 0)),
             monitored ? l.both(metadata[0].at("exclusive"), l.eq(face_result, 0)) : zero);
}

void AhbOutlet::build() {
  state();
  phases();
  write_accept();
  abort_state();
  issue_transfers();
  burst_blocks();
  retire_write();
  drive_bus();
  update_write();
  respond_write();
  gather_read();
  update_read();
}

Result<NodeHardware> AhbOutlet::finish() { return l.finish("nocloom_ahb_outlet_" + p.name); }

// AHB: a pipelined manager. The address phase of the next transfer overlaps the data phase of the
// current one (AHB-Lite 3.1), so a burst issues one transfer per HREADY cycle while its words and
// response room last. A write word taken into an empty current entry, and a read packet taken by
// an idle issue beat, issue in the cycle they arrive. bus.address_phase marks the transfer driven
// as NONSEQ or SEQ (its controls and write data in bus.*); bus.data_phase marks the accepted
// transfer whose data phase is on the bus (HWDATA in bus.hwdata). A write word leaves its buffer
// when its last transfer is issued; the packet's B waits until every transfer has completed (and
// passes an empty B register straight to the network). A read transfer reserves one of the two
// response buffer entries with its metadata, so every completion finds room; the buffer, or a
// completion passing an empty one, drives the response face.
Result<NodeHardware> lower_ahb_engine(const BurstOutletProfile &p) {
  if (auto bad = profile_error(p, Protocol::AhbV3))
    return Result<NodeHardware>::failure(*bad);
  AhbOutlet engine(p);
  engine.build();
  return engine.finish();
}
} // namespace

Result<NodeHardware> lower_ahb_outlet(const AhbOutletProfile &profile) {
  return lower_ahb_engine(profile);
}

unsigned apb_bus_address_bits(const BurstOutletProfile &p) {
  const auto port_bits =
      std::min(p.address_bits, p.transport_address_bits ? p.transport_address_bits : 32U);
  if (p.mappings.empty())
    return port_bits;
  const auto top = std::accumulate(p.mappings.begin(), p.mappings.end(), std::uint64_t{0},
                                   [](std::uint64_t highest, const AddressMapping &m) {
                                     return std::max(highest, m.local_address + m.size - 1);
                                   });
  return std::min(port_bits, std::max(1U, static_cast<unsigned>(std::bit_width(top))));
}

ApbOutletForm apb_outlet_form(const BurstOutletProfile &p) {
  const bool single = !p.apb_back_to_back && !p.timeout_cycles;
  const bool at_once = single && p.apb_data_bits == 32;
  const bool subpage =
      std::ranges::any_of(p.mappings, [](const AddressMapping &m) { return m.size % 4096 != 0; });
  return {single, at_once, at_once && p.exclusive_target.mode != ExclusiveAccess::Network,
          at_once && !subpage};
}

Result<NodeHardware> lower_apb_burst_outlet(const ApbBurstOutletProfile &profile) {
  if (profile.dialect != Protocol::Apb4 && profile.dialect != Protocol::Apb3 &&
      profile.dialect != Protocol::Apb2)
    return Result<NodeHardware>::failure(
        error("NCL_APB_BURST_OUTLET_PROFILE", "dialect", "Expected apb4, apb3 or apb2"));
  return lower_burst_outlet(profile, profile.dialect);
}

Result<NodeHardware> lower_axi_lite_outlet(const BurstOutletProfile &profile) {
  return lower_lite_engine(profile);
}
} // namespace nocloom::detail
