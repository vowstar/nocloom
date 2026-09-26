#include "axi4_outlet.hpp"
#include "attribute_map.hpp"
#include "beat_stage.hpp"
#include "burst_faces.hpp"
#include "decode_packet.hpp"
#include "diagnostic.hpp"
#include "exclusive_monitor.hpp"
#include "logic.hpp"
#include "mapping_decode.hpp"
#include "message.hpp"
#include "strobe_lanes.hpp"
#include <algorithm>
#include <bit>
#include <functional>
#include <iterator>
#include <numeric>

namespace nocloom::detail {
namespace {
using logic::Logic;
using Fields = std::map<std::string, ValueId>;

// sizes bounds AxSIZE: 4 serves the 32/64-bit outlets; 5 to 8 add
// the 16- to 128-byte beats of 128- to 1024-bit outlets.
ValueId advance(Logic &l, ValueId address, const Fields &d, unsigned sizes) {
  const auto bits = l.b.ir.values.at(address.index).width;
  auto zero = l.b.constant(bits, 0);
  auto stride = l.pow2(d.at("size"), bits, sizes - 1);
  auto next = l.b.binary(Op::Subtract, address, l.b.binary(Op::Subtract, zero, stride));
  auto span = l.b.unary(Op::ZeroExtend, d.at("length"), bits);
  span = l.b.binary(Op::Subtract, span, l.b.constant(bits, (std::uint64_t{1} << bits) - 1));
  auto scaled = span;
  for (unsigned i = 1; i < sizes; ++i) {
    span = l.b.binary(Op::Subtract, span, l.b.binary(Op::Subtract, zero, span));
    scaled = l.b.select(l.eq(d.at("size"), i), span, scaled);
  }
  auto mask = l.dec(scaled);
  auto wrapped = l.b.binary(
      Op::BitOr, l.b.binary(Op::BitAnd, d.at("address"), l.b.unary(Op::BitNot, mask, bits)),
      l.b.binary(Op::BitAnd, next, mask));
  return l.b.select(l.eq(d.at("burst"), 0), address,
                    l.b.select(l.eq(d.at("burst"), 2), wrapped, next));
}

// RESP to the internal result: OKAY success, DECERR network miss, anything
// else a target error. EXOKAY succeeds only for an exclusive burst (lock)
// passed to the target; to a normal burst it is a protocol error.
ValueId result(Logic &l, ValueId response, ValueId fault,
               std::optional<ValueId> lock = std::nullopt) {
  if (!lock)
    return l.b.select(
        fault, l.b.constant(2, 2),
        l.b.select(l.eq(response, 3), l.b.constant(2, 1),
                   l.b.select(l.eq(response, 0), l.b.constant(2, 0), l.b.constant(2, 2))));
  const auto success = l.either(l.eq(response, 0), l.both(*lock, l.eq(response, 1)));
  return l.b.select(fault, l.b.constant(2, 2),
                    l.b.select(l.eq(response, 3), l.b.constant(2, 1),
                               l.b.select(success, l.b.constant(2, 0), l.b.constant(2, 2))));
}
// The target's EXOKAY to an exclusive burst passed to it.
ValueId exclusive_success(Logic &l, ValueId response, ValueId fault, ValueId lock) {
  return l.both(lock, l.both(l.eq(response, 1), l.inv(fault)));
}

// The lowest index whose condition holds (index 0 when none does).
ValueId first_of(Logic &l, const std::vector<ValueId> &conditions, unsigned width) {
  auto value = l.b.constant(width, 0);
  for (auto k = static_cast<unsigned>(conditions.size()); k-- > 0;)
    value = l.b.select(conditions[k], l.b.constant(width, k), value);
  return value;
}
// The first index whose condition holds in ring order from start (start when none does).
ValueId first_from(Logic &l, const std::vector<ValueId> &conditions, ValueId start,
                   unsigned width) {
  const auto n = static_cast<unsigned>(conditions.size());
  std::vector<ValueId> by_start;
  for (unsigned s = 0; s < n; ++s) {
    auto value = l.b.constant(width, s);
    for (unsigned j = n; j-- > 0;)
      value = l.b.select(conditions[(s + j) % n], l.b.constant(width, (s + j) % n), value);
    by_start.push_back(value);
  }
  auto value = by_start.front();
  for (unsigned s = 1; s < n; ++s)
    value = l.b.select(l.eq(start, s), by_start[s], value);
  return value;
}
ValueId any_of(Logic &l, const std::vector<ValueId> &conditions) {
  return std::accumulate(conditions.begin(), conditions.end(), l.bit(0),
                         [&](ValueId value, ValueId c) { return l.either(value, c); });
}
// Field k of a slot table selected by index.
ValueId pick(Logic &l, const std::vector<ValueId> &values, ValueId index) {
  auto value = values.front();
  for (unsigned k = 1; k < values.size(); ++k)
    value = l.b.select(l.eq(index, k), values[k], value);
  return value;
}

using beat_stage::Stage;
using beat_stage::stage;
using beat_stage::stage_field;
using beat_stage::staged;

struct WriteSlot {
  ValueId valid, local, bad, ahead, source, id, address, size, code, lock, tag, user;
  // Network monitor: the failed exclusive flag and the range fields.
  ValueId excluded, length, burst;
};

// The outlet engine for K = max_outstanding bursts in flight, reads and writes sharing one pool of
// K entries from a burst's head until its last response word leaves. Writes: the main entry (the
// AW stage) sends AW and every beat enters the one-entry W stage, each beat with its WLAST and drop
// flag; a head's AW and first beat enter apart. One burst keeps the main entry until its B leaves.
// With several, the main burst moves to one of K write slots once AW is accepted and its last beat
// taken; a slot keeps the response fields, the local code and the count of older writes of its
// target ID in flight (ahead), and completes on the B of its target ID, or locally, with ahead
// zero. Reads: a ring of K descriptors taken in order while at most one waits for its AR; AR
// issues in ring order, R fills the oldest issued entry of its RID (with one burst, the only
// entry) through the two-entry R stage, whose beats carry their entry, and a rejected entry
// streams its local beats once every older read is received (also in the cycle it is passed
// over).
struct EngineLowering {
  EngineLowering(const Axi4OutletProfile &p, unsigned address_bits, unsigned sizes);

  logic::Logic l;
  const Axi4OutletProfile &p;
  const unsigned address_bits, sizes;
  const unsigned depth;
  const bool several;
  // Several bursts use the joined response stage. One burst keeps the ordinary outlet and adds
  // only segment detection: no second read can interleave with it (Axi4OutletProfile::interleave).
  const bool ilv, segment;
  // Behind a target merge without segments R beats wait in one entry; RREADY is its room.
  const bool single_entry;
  const std::uint64_t address_space;
  const unsigned index_bits, count_bits, sources;
  const bool network;
  // Target mode: AxLOCK carries lock and EXOKAY answers it.
  const bool passthrough;
  // Target mode with owner flags: an exclusive access takes them as its sequence ID.
  const bool owned;
  // Several bursts in flight and target mode answer a request of a source outside id_sources
  // locally.
  const bool indexed;
  const unsigned target_id_bits, flow_bits, timeout;
  const ValueId zero, one;
  // Target timeout: one watchdog for every burst in flight; failed holds until the
  // common reset; without completion a failed target absorbs every response.
  const ValueId failed;
  const bool report_only, write_timer;
  const ValueId silent;
  const bool windowed;
  BurstProfile profile;
  // The in-flight pool shared by both directions: pool counts admitted bursts (heads) minus those
  // whose last response word left (a B pop or the last popped R beat). A head of either direction
  // enters while the pool is below K. Behind a target merge the two request faces may both offer
  // a head; the older takes the last free entry (the network delivers one packet at a time): a
  // read that waited while no write head did (read_first), else an offered write head.
  const unsigned pool_bits, ahead_bits;
  const ValueId pool, cap, pool_free, read_first, pool_w;
  ValueId pool_r{};
  // With several bursts an entry returns to the pool in the cycle after its last response word
  // left (a read's, a write's); empty counts those releases as done.
  const ValueId read_release, write_release, pool_empty;

  ValueId in(const std::string &face, const std::string &field, unsigned width) {
    return l.in(face, field, width);
  }
  void out(const std::string &face, const std::string &field, ValueId value) {
    l.out(face, field, value);
  }
  ValueId active(ValueId value) { return l.both(l.reset, value); }
  // Holds a port value from the cycle before expiry on.
  ValueId frozen(const std::string &name, ValueId value) {
    return l.b.select(failed, l.latch(name, value, l.inv(failed)), value);
  }
  ValueId miss(ValueId address) {
    if (!windowed)
      return zero;
    return l.inv(p.mappings.front().size == address_space
                     ? one
                     : mapping_decode::any(l, address, address_bits, p.mappings));
  }
  Fields request(const std::string &face) {
    return l.arrive(l.face(face, burst_request_fields(p.data_bits, profile, p.source_bits)));
  }
  // The low bits of the sum of the slices of a route value: the slices of a constant.
  static std::uint64_t slices(std::uint64_t route, unsigned width) {
    const auto mask = (std::uint64_t{1} << width) - 1;
    std::uint64_t sum = 0;
    for (; route; route >>= width)
      sum += route & mask;
    return sum & mask;
  }
  // The sum (Axi4OutletProfile::id_route) below the flow number of a request: the slices of AxID in
  // its place, plus a constant by source, initiator flow and direction.
  ValueId id_sum(ValueId source, std::optional<ValueId> id, unsigned direction, unsigned width) {
    auto constant = l.b.constant(width, 0);
    for (unsigned k = sources; k-- > 0;) {
      const auto &entry = p.id_route.sources[k];
      // The source field's direction is 0 for a write, the route word's 1.
      const std::uint64_t written = entry.direction && direction == 0 ? 1 : 0;
      const auto value = [&](unsigned f) {
        return l.b.constant(width, (slices(entry.flows[f], width) + written) &
                                       ((std::uint64_t{1} << width) - 1));
      };
      auto flow = value(0);
      if (id && !entry.ranges.empty()) {
        std::vector<ValueId> values;
        for (unsigned f = 0; f < entry.flows.size(); ++f)
          values.push_back(value(f));
        flow = class_value(l.b, *id, entry.ranges, values);
      }
      constant =
          !p.source_bits
              ? flow
              : l.b.select(l.b.binary(Op::Equal, source,
                                      l.b.constant(p.source_bits, 2 * p.id_sources[k] + direction)),
                           flow, constant);
    }
    if (!id)
      return constant;
    const auto below = p.id_route.direction_bits;
    const auto placed = below ? l.b.concat(*id, l.b.constant(below, 0)) : *id;
    auto sum = constant;
    for (unsigned at = 0; at < p.id_bits + below; at += width)
      sum =
          l.plus(sum, l.fit(l.b.slice(placed, at, std::min(width, p.id_bits + below - at)), width));
    return sum;
  }
  // The internal target ID before id_to_port: flow above sequence, the sum or, for an
  // exclusive access with the target's monitor, its owner flags.
  ValueId target_id(ValueId source, std::optional<ValueId> id, unsigned direction, ValueId address,
                    std::optional<std::pair<ValueId, ValueId>> owner) {
    const auto width = target_id_bits - flow_bits;
    std::optional<ValueId> sequence;
    if (width) {
      auto sum = id_sum(source, id, direction, width);
      if (!p.id_route.target_paths.empty()) {
        auto number = l.b.constant(width, slices(p.id_route.target_paths.front(), width));
        for (unsigned g = 1; g < p.id_route.target_paths.size(); ++g)
          number = l.b.select(
              l.eq(mapping_decode::flow(l, address, address_bits, p.mappings, flow_bits), g),
              l.b.constant(width, slices(p.id_route.target_paths[g], width)), number);
        sum = l.plus(sum, number);
      }
      if (owner) {
        const auto &flags = p.exclusive_target.id_flags;
        const auto value = std::accumulate(
            flags.begin() + 1, flags.end(), l.b.slice(owner->second, flags.front(), 1),
            [&](ValueId low, unsigned flag) {
              return l.b.concat(l.b.slice(owner->second, flag, 1), low);
            });
        sum = l.b.select(owner->first, l.fit(value, width), sum);
      }
      sequence = sum;
    }
    if (!flow_bits)
      return sequence.value_or(zero);
    const auto flow = mapping_decode::flow(l, address, address_bits, p.mappings, flow_bits);
    return sequence ? l.b.concat(flow, *sequence) : flow;
  }
  // The target ID on the port: the flow number in the lowest bits, so a target finds it whatever
  // the ID width, then the sequence ID with its bits reversed. Bit k of a sum depends on the bits
  // of every slice up to k, so reversal gives a target that indexes by its low ID bits the best
  // mixed ones. It only moves wires. Everything inside compares the unmoved value.
  ValueId id_to_port(ValueId id) {
    const auto sequence = target_id_bits - flow_bits;
    std::optional<ValueId> port;
    if (flow_bits)
      port = l.b.slice(id, sequence, flow_bits);
    for (unsigned k = sequence; k-- > 0;) {
      const auto bit = l.b.slice(id, k, 1);
      port = port ? l.b.concat(bit, *port) : bit;
    }
    return *port;
  }
  ValueId id_from_port(ValueId port) {
    const auto sequence = target_id_bits - flow_bits;
    std::optional<ValueId> id;
    for (unsigned k = 0; k < sequence; ++k) {
      const auto bit = l.b.slice(port, target_id_bits - 1 - k, 1);
      id = id ? l.b.concat(bit, *id) : bit;
    }
    if (flow_bits) {
      const auto flow = l.b.slice(port, 0, flow_bits);
      id = id ? l.b.concat(flow, *id) : flow;
    }
    return *id;
  }
  // Whether a head meets an earlier access of its source and AxID with either one exclusive.
  ValueId owner_order(const Fields &earlier, ValueId earlier_lock, const Fields &arriving) {
    auto same = l.either(earlier_lock, arriving.at("lock"));
    if (p.source_bits)
      same = l.both(same, l.b.binary(Op::Equal, earlier.at("source"), arriving.at("source")));
    if (p.id_bits)
      same = l.both(same, l.b.binary(Op::Equal, earlier.at("id"), arriving.at("id")));
    return same;
  }
  // The target ID of a descriptor (no source field without sources).
  ValueId descriptor_id(const Fields &d, unsigned direction) {
    if (d.contains("target_id"))
      return d.at("target_id");
    return target_id(d.contains("source") ? d.at("source") : zero,
                     p.id_bits ? std::optional{d.at("id")} : std::nullopt, direction,
                     d.at("address"),
                     owned ? std::optional{std::pair{d.at("lock"), d.at("user")}} : std::nullopt);
  }
  ValueId write_slot_id(const WriteSlot &slot) {
    return target_id(slot.source, p.id_bits ? std::optional{slot.id} : std::nullopt, 0,
                     slot.address,
                     owned ? std::optional{std::pair{slot.lock, slot.user}} : std::nullopt);
  }
  // A request from a source outside id_sources has no target ID: it is an
  // error request answered locally with SLVERR, never aliased to source 0.
  ValueId unknown_source(ValueId source, unsigned direction) {
    const auto known = std::accumulate(
        p.id_sources.begin(), p.id_sources.end(), zero, [&](ValueId any, unsigned si) {
          return l.either(
              any, l.b.binary(Op::Equal, source, l.b.constant(p.source_bits, 2 * si + direction)));
        });
    return l.inv(known);
  }
  // With a timer, a failed target sees the frozen channel: held reports an
  // address still waiting for its ready from before expiry.
  void address_face(const std::string &prefix, const Fields &d, ValueId valid, ValueId id,
                    std::optional<ValueId> held) {
    out("axi", prefix + "valid", held ? l.b.select(failed, *held, active(valid)) : active(valid));
    for (const auto &[signal, field] :
         std::vector<std::pair<std::string, std::string>>{{"addr", "address"},
                                                          {"len", "length"},
                                                          {"size", "size"},
                                                          {"burst", "burst"},
                                                          {"cache", "cache"},
                                                          {"prot", "protection"}}) {
      auto value = d.at(field == "address" && p.start_address ? "start_address" : field);
      if (field == "address" && p.align_wrap && prefix == "ar")
        value = l.b.select(aligning(d), d.at("address"), value);
      // Absent attributes take the conventional AMBA mapping.
      if (const auto map = p.attributes.value_or(TargetAttributes::amba());
          field == "cache" || field == "protection") {
        const auto write = l.b.constant(1, prefix == "aw" ? 1 : 0);
        value = field == "cache"
                    ? map_attributes(l, prefix == "aw" ? map.write_cache : map.read_cache,
                                     d.at("cache"), d.at("protection"), write)
                    : map_attributes(l, map.prot, d.at("cache"), d.at("protection"), write);
      }
      if (field == "address" && windowed)
        value = mapping_decode::local(l, value, address_bits, p.mappings);
      if (field == "address" && p.address_bits < address_bits)
        value = l.b.slice(value, 0, p.address_bits);
      if (held)
        value = frozen("timeout." + prefix + signal, value);
      if (field == "length")
        value = l.fit(value, p.port_len_bits.value_or(p.len_bits));
      out("axi", prefix + signal, value);
    }
    const auto lock = passthrough ? d.at("lock") : zero;
    out("axi", prefix + "lock", p.axi3 ? l.b.concat(zero, lock) : lock);
    if (!p.user.empty()) {
      const auto user = map_attributes(l, p.user, d.at("cache"), d.at("protection"),
                                       l.b.constant(1, prefix == "aw" ? 1 : 0), d.at("user"));
      out("axi", prefix + "user", held ? frozen("timeout." + prefix + "user", user) : user);
    }
    if (target_id_bits)
      out("axi", prefix + "id", id_to_port(held ? frozen("timeout." + prefix + "id", id) : id));
  }
  void response_face(const std::string &face, const Fields &d, ValueId full, ValueId address,
                     ValueId code, ValueId first, ValueId last, ValueId data,
                     ValueId exclusive_okay, std::optional<ValueId> user) {
    out(face, "valid", active(full));
    out(face, "address", address);
    out(face, "result", code);
    if (p.exclusive)
      out(face, "exclusive_okay", exclusive_okay);
    if (p.response_user_bits)
      out(face, "user", user.value_or(l.b.constant(p.response_user_bits, 0)));
    const auto beat_count = l.pow2(d.at("size"), burst_count_bits(p.data_bits), sizes - 1);
    // A packet's one word stays one fragment on every link.
    out(face, "count",
        p.error_codes && face == "rrsp"
            ? l.b.select(local_error_on, l.b.constant(burst_count_bits(p.data_bits), 4), beat_count)
            : beat_count);
    out(face, "error", p.decode_flag && face == "rrsp" ? local_error_on : zero);
    out(face, "first", first);
    out(face, "last", last);
    out(face, "data", data);
    if (p.tag_bits)
      out(face, "tag", d.at("tag"));
    if (p.id_bits)
      out(face, "id", d.at("id"));
    if (p.source_bits)
      out(face, "source", d.at("source"));
  }
  // Only a local miss that never issued AR may replace its beats with a length word.
  // Native target responses always keep their own result and carry no descriptor mark.
  void local_error_states() { local_error_on = p.error_codes ? l.state("r.local_error", 1) : zero; }
  ValueId local_error_word() {
    return p.error_codes
               ? l.both(recv_rejecting, l.both(recv_miss, l.inv(l.eq(recv.at("length"), 0))))
               : zero;
  }
  void local_error_load(ValueId kept, const Stage &stage) {
    if (!p.error_codes)
      return;
    if (single_entry) {
      l.b.update(local_error_on, l.b.select(kept, local_error_now, local_error_on));
      return;
    }
    const auto spare = l.state("r.spare_local_error", 1);
    l.b.update(spare, l.b.select(stage.to_spare, local_error_now, spare));
    l.b.update(local_error_on,
               l.b.select(l.either(stage.to_main, stage.shift),
                          l.b.select(rspare, spare, local_error_now), local_error_on));
  }
  void local_error_face(const Fields &d, ValueId &code, ValueId &data, ValueId &first,
                        ValueId &address) {
    if (!p.error_codes)
      return;
    code = l.b.select(local_error_on, l.b.constant(2, p.decode_flag ? 1 : 3), code);
    data =
        l.b.select(local_error_on, decode_packet::word_data(l, d.at("length"), p.data_bits), data);
    first = l.b.select(local_error_on, one, first);
    address = l.b.select(local_error_on, d.at("address"), address);
  }
  // The target's BUSER or RUSER as the response user field, zero where a beat or B did not come
  // from the target; none without the field or the port.
  std::optional<ValueId> target_user(const char *signal, ValueId taken) {
    if (!p.response_user_bits || !p.response_user_port_bits)
      return std::nullopt;
    return l.b.select(taken,
                      l.fit(in("axi", signal, p.response_user_port_bits), p.response_user_bits),
                      l.b.constant(p.response_user_bits, 0));
  }
  // A read the outlet aligns (Axi4OutletProfile::align_wrap): a WRAP whose start address is not
  // on a word.
  ValueId aligning(const Fields &d) {
    if (!p.align_wrap)
      return zero;
    const unsigned lw = static_cast<unsigned>(std::bit_width(p.data_bits / 8)) - 1;
    return l.both(l.both(l.eq(d.at("burst"), 2), l.inv(l.eq(d.at("length"), 0))),
                  l.inv(l.eq(l.b.slice(d.at("start_address"), 0, lw), 0)));
  }
  // The port inputs, taken from the handshakes that already read them.
  ValueId operand(ValueId value) { return l.b.ir.values.at(value.index).operands.at(1); }

  // A field of the main entry, loaded by the head.
  void entry_field(ValueId main, ValueId value) { l.b.update(main, l.b.select(head, value, main)); }

  // Write-side handles handed between the write phases; the read side and the
  // pool epilogue read some of them. Phase-assigned handles start at {}.
  Fields w;
  ValueId wb{}, wf{}, wd{}, wp{}, reject{}, bad{}, remaining{}, bf{};
  ValueId collecting{}, wr_ready{}, take_w{}, head{}, head_beat{}, head_pending{}, head_in{};
  ValueId w_load{}, admit{}, main_beat{}, in_last{};
  // The write a B completed in the previous cycle (owner_order).
  ValueId owner_gone{}, gone_lock{};
  Fields gone;
  ValueId aw_fire{}, push{}, drop{}, beat{}, aw_id{}, wlast{}, wdrop{}, wvalid{}, w_fire{};
  ValueId beat_error{};
  ValueId head_reject{}, head_missed{};
  ValueId failing{}, seen{}, missed_w{}, excluded{}, bdone{}, wsilent{};
  std::vector<std::string> descriptor;
  Fields aw;
  std::vector<WriteSlot> ws;
  std::vector<ValueId> free_slot;
  std::optional<ExclusiveMonitor> monitor;
  // Read-side handles the pool epilogue reads.
  Fields r;
  ValueId rr_ready{}, take_r{}, pop{}, read_claim{};
  // Each descriptor owns a replay slot until its last response leaves.
  ValueId align_word(const std::string &name, ValueId value, ValueId save, ValueId into,
                     ValueId from) {
    if (name.find("address") != std::string::npos)
      value = l.global(value);
    std::vector<ValueId> words;
    for (unsigned k = 0; k < depth; ++k)
      words.push_back(l.latch("r.slot" + std::to_string(k) + ".saved_" + name, value,
                              l.both(save, l.eq(into, k))));
    return pick(l, words, from);
  }
  ValueId align_save{}, align_into{};
  void align_valid() {
    if (!p.align_wrap)
      return;
    for (unsigned k = 0; k < depth; ++k) {
      const auto valid = l.state("r.slot" + std::to_string(k) + ".saved_valid", 1);
      const auto done = l.both(pop, l.eq(several ? rout : zero, k));
      const auto save = l.both(align_save, l.eq(align_into, k));
      l.b.update(valid, l.b.select(done, zero, l.b.select(save, one, valid)));
    }
  }

  // Read-side handles handed between the read phases.
  std::vector<Fields> rs;
  std::vector<ValueId> rejects, misses;
  ValueId count{}, pending{}, qhead{}, qtail{}, qissue{}, rf{}, rk{}, ra{}, rspare{};
  ValueId qrecv{}, received{}, recv_miss{}, recv_rejecting{}, rmiss{}, rreject{};
  bool issue_built = false;
  ValueId arvalid{}, ar_fire{}, held_ar{}, issue_ready{};
  ValueId skip{}, issued{}, rpush{}, rlast{}, current{}, rpop{}, rexclusive{};
  ValueId stored_result{}, stored_data{}, stored_first{}, stored_last{}, stored_addr{};
  ValueId response_offered{}, response_cut{}, block_count{};
  // A tagged length word belongs only to a locally rejected miss.
  ValueId local_error_on{}, local_error_now{}, aw_shown{};
  std::optional<ValueId> stored_user, bstored_user;
  ValueId complete{};
  Fields front, recv;
  // Several bursts: per entry allocated (valid), passed to the target or over (issued) and
  // received in full (received); the entry receiving a burst, the entry selected for this cycle's
  // beat and whether it streams local beats.
  std::vector<ValueId> rvalid, rissued, rdone;
  ValueId rcur{}, rsel{}, rlocal{}, rout{};
  // WRAP read alignment: the held answer is the next main entry load (purging).
  ValueId purging{};
  // The write slot fields echoed on the B: the AxID and the response steering tag.
  std::vector<std::pair<std::string, ValueId WriteSlot::*>> echoed_slot_fields() const {
    std::vector<std::pair<std::string, ValueId WriteSlot::*>> out;
    for (const auto &[name, member, bits] : {std::tuple{"id", &WriteSlot::id, p.id_bits},
                                             std::tuple{"tag", &WriteSlot::tag, p.tag_bits}})
      if (bits)
        out.emplace_back(name, member);
    return out;
  }
  Fields select_fields(ValueId index) {
    Fields d;
    for (const auto &name : descriptor) {
      std::vector<ValueId> values;
      std::ranges::transform(rs, std::back_inserter(values),
                             [&](const Fields &s) { return s.at(name); });
      d[name] = pick(l, values, index);
    }
    return d;
  }

  // Interleave mode: a backward pipe's full flag, the word pushed into it now and that word's
  // fields; the joined stage's registers (full, holding a B, the last flag) and its events.
  struct Offer {
    ValueId full{}, push{};
    Fields live;
  };
  Offer boffer, roffer;
  ValueId jfull{}, jwrite{}, jlast{}, b_held{}, bsilent_done{};
  std::vector<ValueId> rbeat; // each read entry's beats received
  // Interleave mode with several bursts: reads take any free entry (ralloc, the lowest) and the
  // entries' ages order them, age[i][j] (i < j) set while entry i is older than entry j.
  ValueId ralloc{};
  std::vector<std::vector<ValueId>> age;
  ValueId oldest_of(const std::vector<ValueId> &set);
  ValueId beat_address(const Fields &d, ValueId index);
  void join_states();
  void read_receive_joined();
  void join_responses();
  void read_update_joined();

  void write_accept();
  void write_reject();
  void write_data();
  void write_respond();
  // The B face of the write side: its fields, address, stored result, exclusive flag, and a
  // write-only outlet's B offered in its BREADY cycle.
  struct WriteFace {
    Fields fields;
    ValueId address{}, stored{}, exclusive{}, through{};
  };
  ValueId write_local_code();
  void write_respond_several(ValueId bresp, ValueId bid, WriteFace &face);
  void write_respond_single(ValueId bresp, ValueId bid, WriteFace &face);
  // A B parked behind the response merger (Axi4OutletProfile::merger_hints): its flag, ID and
  // result registers, and the B in hand (the parked one, else the port's).
  struct ParkedB {
    ValueId parked{}, id{}, resp{}, bid{}, bresp{};
    std::optional<ValueId> id_reg{}, resp_reg{};
  };
  ParkedB park_b(ValueId bid, ValueId bresp);
  // BREADY and the B taken into the stage (fire), and the B that parks in this cycle (load).
  std::pair<ValueId, ValueId> take_b(const ParkedB &b, ValueId spare, ValueId any_local,
                                     ValueId any_match);
  // The BUSER of a B taken (fire), a parked one's from its register; none without the port.
  std::optional<ValueId> b_user(const ParkedB &b, ValueId fire, ValueId load);
  void read_accept();
  ValueId issue_ready_now();
  void read_issue();
  void segment_response(ValueId kept);
  void update_segment_last(const Stage &stage, ValueId moving, ValueId mload, ValueId inject);
  ValueId inside_block();
  void count_block_word(ValueId entering, ValueId last);
  void read_receive();
  void read_update();
  void count_pool();
  void build();
  Result<NodeHardware> finish();
};

EngineLowering::EngineLowering(const Axi4OutletProfile &p, unsigned address_bits, unsigned sizes)
    : p(p), address_bits(address_bits), sizes(sizes), depth(p.max_outstanding), several(depth > 1),
      ilv(p.interleave && (several || p.response_direct)), segment(p.interleave),
      single_entry(p.merged && !p.interleave), address_space(std::uint64_t{1} << address_bits),
      index_bits(static_cast<unsigned>(std::bit_width(depth - 1))),
      count_bits(static_cast<unsigned>(std::bit_width(depth))),
      sources(static_cast<unsigned>(p.id_route.sources.size())),
      network(p.exclusive_target.mode == ExclusiveAccess::Network),
      passthrough(p.exclusive_target.mode == ExclusiveAccess::Target),
      owned(passthrough && !p.exclusive_target.id_flags.empty()), indexed(several || passthrough),
      target_id_bits(axi4_outlet_id_bits(p)),
      flow_bits(static_cast<unsigned>(std::bit_width(p.flows - 1))), timeout(p.timeout_cycles),
      zero(l.bind("c.zero", l.bit(0))), one(l.bind("c.one", l.bit(1))),
      failed(timeout ? l.state("timeout.failed", 1) : zero),
      report_only(timeout && !p.timeout_completion), write_timer(timeout && !p.read_only),
      silent(report_only ? failed : zero), windowed(!p.mappings.empty()),
      profile{p.id_bits, p.len_bits, p.start_address, address_bits}, pool_bits(count_bits),
      ahead_bits(std::max(1U, index_bits)), pool(l.state("pool.count", pool_bits)),
      cap(l.b.constant(pool_bits, depth)), pool_free(l.below(pool, cap)),
      read_first(p.merged ? l.state("pool.read_first", 1) : zero),
      pool_w(l.bind("pool.write_ok", p.merged ? l.below(l.plus(l.fit(pool, pool_bits + 1),
                                                               l.fit(read_first, pool_bits + 1)),
                                                        l.b.constant(pool_bits + 1, depth))
                                              : pool_free)),
      read_release(several ? l.state("pool.read_release", 1) : zero),
      write_release(several ? l.state("pool.write_release", 1) : zero),
      pool_empty(
          l.bind("pool.empty", l.b.binary(Op::Equal, l.minus(pool, l.fit(read_release, pool_bits)),
                                          l.fit(write_release, pool_bits)))) {
  profile.exclusive = p.exclusive;
  profile.tag_bits = p.tag_bits;
  profile.user_bits = p.user_bits;
  profile.response_user_bits = p.response_user_bits;
  l.block = p.arrival;
  l.block_bits = address_bits;
}

void EngineLowering::write_accept() {
  // ---------------- writes ----------------
  w = request("wreq");
  wb = l.state("w.busy", 1);
  wf = l.state("w.full", 1);
  wd = l.state("w.done", 1);
  wp = l.state("w.aw_pending", 1);
  reject = l.state("w.reject", 1);
  bad = l.state("w.bad", 1);
  remaining = l.state("w.remaining", p.len_bits);
  // Interleave mode with several bursts: the B pipe is w.response_spare.
  bf = ilv && several ? zero : l.state("w.response_full", 1);
  ws.resize(several ? depth : 0);
  for (unsigned k = 0; k < ws.size(); ++k) {
    const auto at = "w.slot" + std::to_string(k) + ".";
    ws[k].valid = l.state(at + "valid", 1);
    ws[k].local = l.state(at + "local", 1);
    ws[k].bad = l.state(at + "bad", 1);
    ws[k].ahead = l.state(at + "ahead", ahead_bits);
    ws[k].source = l.state(at + "source", p.source_bits);
    if (p.id_bits)
      ws[k].id = l.state(at + "id", p.id_bits);
    ws[k].address = l.address_state(at + "address", address_bits);
    ws[k].size = l.state(at + "size", 3);
    ws[k].code = l.state(at + "code", 2);
    ws[k].lock = passthrough || network ? l.state(at + "lock", 1) : zero;
    if (owned)
      ws[k].user = l.state(at + "user", p.user_bits);
    if (p.tag_bits)
      ws[k].tag = l.state(at + "tag", p.tag_bits);
    if (network) {
      ws[k].excluded = l.state(at + "exclusive_fail", 1);
      ws[k].length = l.state(at + "length", p.len_bits);
      ws[k].burst = l.state(at + "burst", 2);
    }
  }
  std::ranges::transform(ws, std::back_inserter(free_slot),
                         [&](const WriteSlot &slot) { return l.inv(slot.valid); });
  // The main entry (w.busy, w.descriptor.*) is the AW stage: it sends AW and collects its burst's
  // beats; with several bursts the burst moves on to a write slot once its AW is accepted and its
  // last beat taken.
  collecting = l.bind("w.collecting", l.both(wb, l.inv(wd)));
  descriptor = {"address", "length", "size", "burst", "cache", "protection"};
  if (p.source_bits)
    descriptor.emplace_back("source");
  if (p.start_address)
    descriptor.emplace_back("start_address");
  if (p.id_bits)
    descriptor.emplace_back("id");
  if (passthrough || network)
    descriptor.emplace_back("lock");
  if (!p.user.empty() || (owned && several))
    descriptor.emplace_back("user");
  // One burst keeps its target ID in its entry: no slots compare it, and its sum leaves the head's
  // path to the AW and AR ports. Several compute it per entry from the fields they keep.
  if (target_id_bits && !several) {
    w["target_id"] = descriptor_id(w, 0);
    descriptor.emplace_back("target_id");
  }
  if (p.tag_bits)
    descriptor.emplace_back("tag");
  for (const auto &name : descriptor)
    aw[name] = l.state_like("w.descriptor." + name, w.at(name));
  // A packet head enters only below the pool capacity (which leaves a slot: every in-flight write
  // is in the main entry, a slot or the response stage); beats follow until the burst's last beat
  // is taken, each W entry beat with its own WLAST and drop flag. The W entry is one forward slot:
  // a beat enters while it is empty or leaves in this cycle, so the ready follows WREADY.
  wlast = l.state("w.beat_last", 1);
  wdrop = l.state("w.beat_drop", 1);
  const auto dropping = timeout ? l.either(wdrop, failed) : wdrop;
  aw_shown = wp;
  // A beat already shown stays shown.
  const auto w_shown = l.state("w.fixed_shown", 1);
  wvalid = l.bind("w.valid", active(l.both(wf, l.inv(dropping))));
  const auto target_wready = in("axi", "wready", 1);
  w_fire = l.both(wvalid, target_wready);
  l.b.update(w_shown, l.both(wvalid, l.inv(w_fire)));
  beat = l.bind("w.beat", l.either(w_fire, l.both(active(wf), dropping)));
  // With several bursts the main burst moves to a slot once its AW is accepted and its last beat
  // taken, possibly in that same cycle; staged beats follow on W. The push only updates registers,
  // so no port depends on it. One burst leaves the main entry when its B leaves (below). A failed
  // target owes no AW: its burst moves on and completes locally.
  const auto target_awready = in("axi", "awready", 1);
  aw_fire = l.both(active(aw_shown), target_awready);
  // READY releases a pending obligation. Its common path needs no VALID qualifier.
  const auto aw_ready = active(target_awready);
  const auto aw_owed = l.either(l.inv(wp), timeout ? l.either(aw_ready, failed) : aw_ready);
  // The pool admits a head word once; then its AW and its first beat enter apart, each when it
  // has room: the AW takes the main entry (the AW stage) once it is free or its burst moves on (all
  // its beats taken, its AW accepted); the beat takes the W entry once it has room. The word leaves
  // the network face when both are in: a beat in first waits there for its AW (w.head_beat_in,
  // later beats wait behind it), an AW in first leaves the word as its burst's first beat
  // (w.head_pending). A refused head (answered here) takes a pool entry like any other head: the
  // target never sees it.
  // An occupied slot can refill on READY or a local drop.
  // The registered slot occupancy selects immediate admission or refill.
  const auto room = l.b.select(wf, l.either(target_wready, dropping), one);
  if (!several) {
    // One burst: the pool admits a head only with the burst before it answered, so the main entry
    // and the W entry are free and the head word enters whole.
    const auto head_ok = p.admitted ? one : pool_w;
    wr_ready = active(l.both(room, l.b.select(collecting, one, head_ok)));
    out("wreq", "ready", wr_ready);
    take_w = l.bind("w.accept", l.both(w.at("valid"), wr_ready));
    head = l.bind("w.head", l.both(take_w, l.inv(collecting)));
    admit = head_beat = head;
    head_pending = zero;
    w_load = l.bind("w.load", take_w);
  } else {
    head_in = l.state("w.head_beat_in", 1);
    // With owner flags an exclusive write waits for older writes of its source and AxID, and any
    // write for the exclusive ones: they would share or lose their target ID's order.
    auto owner_wait = owned ? l.both(wb, owner_order(aw, aw.at("lock"), w)) : zero;
    if (owned)
      owner_wait = std::accumulate(
          ws.begin(), ws.end(), owner_wait, [&](ValueId any, const WriteSlot &slot) {
            return l.either(
                any, l.both(slot.valid,
                            owner_order({{"source", slot.source}, {"id", slot.id}}, slot.lock, w)));
          });
    if (owned) {
      // The access still counts in the cycle after its B completes it.
      owner_gone = l.state("w.owner_gone", 1);
      gone = {{"source", l.state("w.gone.source", p.source_bits)},
              {"id", p.id_bits ? l.state("w.gone.id", p.id_bits) : zero}};
      gone_lock = l.state("w.gone.lock", 1);
      owner_wait = l.either(owner_wait, l.both(owner_gone, owner_order(gone, gone_lock, w)));
    }
    const auto pool_head = l.both(p.admitted ? one : l.either(head_in, pool_w), l.inv(owner_wait));
    const auto moving = l.both(l.both(wb, wd), aw_owed);
    const auto head_ok = l.both(pool_head, l.b.select(wb, moving, one));
    const auto beat_room = l.both(l.both(room, l.inv(head_in)), pool_head);
    wr_ready = active(l.b.select(collecting, room, l.both(head_ok, l.either(head_in, room))));
    out("wreq", "ready", wr_ready);
    take_w = l.bind("w.accept", l.both(w.at("valid"), wr_ready));
    const auto head_word = active(l.both(w.at("valid"), l.inv(collecting)));
    head = l.bind("w.head", l.both(head_word, head_ok));
    // The pool takes the head at its first part in.
    admit = l.both(l.both(head_word, l.inv(head_in)), l.either(head_ok, beat_room));
    // The W entry takes a beat of the main burst, or a head word's beat not yet in.
    w_load = l.bind("w.load", l.either(l.both(take_w, collecting), l.both(head_word, beat_room)));
    head_beat = l.both(head, l.either(head_in, room));
    head_pending = l.state("w.head_pending", 1);
    l.b.update(head_pending, l.b.select(l.both(head, l.inv(l.either(head_in, room))), one,
                                        l.b.select(take_w, zero, head_pending)));
    l.b.update(head_in,
               l.b.select(take_w, zero, l.b.select(l.both(head_word, beat_room), one, head_in)));
  }
  // A beat after the head belongs to the main burst.
  main_beat = l.both(take_w, collecting);
  // The beat taken is its burst's last: a one-beat head, or the beat that
  // counts the remaining beats down to one (a waiting first beat counts its length).
  const auto one_beat = l.eq(w.at("length"), 0);
  const auto counted = l.eq(remaining, 1);
  in_last =
      l.bind("w.input_last",
             l.b.select(collecting, several ? l.b.select(head_pending, one_beat, counted) : counted,
                        one_beat));
  push = several
             ? l.bind("w.push",
                      active(l.both(wb, l.both(aw_owed, l.either(wd, l.both(main_beat, in_last))))))
             : zero;
  for (const auto &name : descriptor)
    entry_field(aw.at(name), w.at(name));
}

void EngineLowering::write_reject() {
  auto wmiss = miss(w.at("address"));
  auto wreject = p.read_only ? one : windowed ? l.either(w.at("error"), wmiss) : w.at("error");
  if (indexed)
    wreject = l.either(wreject, unknown_source(w.at("source"), 0));
  // Network monitor: an exclusive write without a matching reservation goes to the target with zero
  // WDATA and WSTRB and answers OKAY without exclusive_okay, whatever its B.
  failing = zero;
  if (network) {
    monitor.emplace(l, "exclusive",
                    ExclusiveMonitorProfile{p.exclusive_target.monitors, p.exclusive_target.granule,
                                            address_bits, p.exclusive_target.id_flags});
    const auto region = monitor->region(w.at("address"), w.at("length"), w.at("size"));
    failing = l.bind("w.exclusive_failing",
                     l.both(w.at("lock"), l.both(l.inv(monitor->holds(monitor->key(w), region)),
                                                 l.inv(wreject))));
  }
  // A write timer exists only where writes reach the target. The monitor
  // sees a write whether or not the target has failed.
  seen = wreject;
  head_reject = wreject;
  head_missed = l.either(wmiss, w.at("error"));
  if (write_timer)
    wreject = l.either(wreject, failed);
  missed_w = zero;
  if (windowed) {
    missed_w = l.state("w.miss", 1);
    entry_field(missed_w, l.either(wmiss, w.at("error")));
  }
  excluded = network ? l.latch("w.exclusive_fail", failing, head) : zero;
  // A field of the main entry: the head loads it, beats may update it in place.
  const auto entry = [&](ValueId main, ValueId head_value, ValueId now) {
    l.b.update(main, l.b.select(head, head_value, now));
  };
  beat_error = w.at("error");
  entry(reject, wreject, reject);
  // done: the burst's last beat is taken.
  entry(wd, l.both(in_last, head_beat), l.b.select(main_beat, in_last, wd));
  // AW pending: an accepted burst owes AW.
  entry(wp, l.inv(wreject), l.both(wp, l.inv(aw_fire)));
  entry(bad, beat_error, l.either(bad, l.both(main_beat, beat_error)));
  // The beats of a rejected burst drain locally, and so do those of a failed target.
  drop = l.b.select(collecting, reject, wreject);
  // remaining: beats still to take after the head's beat.
  l.b.update(remaining,
             l.b.select(several ? l.either(head, l.both(take_w, head_pending)) : head,
                        w.at("length"),
                        l.b.select(several ? main_beat : take_w, l.dec(remaining), remaining)));
}

void EngineLowering::write_data() {
  l.b.update(wf, l.b.select(w_load, one, l.b.select(beat, zero, wf)));
  // An error beat carries no strobes, and neither does a beat of a failing exclusive write; its
  // data is cleared as well. After expiry the W payload stays with the beat left waiting.
  const auto void_beat =
      network ? l.either(beat_error, l.b.select(collecting, excluded, failing)) : beat_error;
  const auto hold = write_timer ? std::optional{failed} : std::nullopt;
  const auto load = hold ? l.both(w_load, l.inv(*hold)) : w_load;
  auto wdata =
      l.latch("w.data", l.b.select(void_beat, l.b.constant(p.data_bits, 0), w.at("data")), load);
  auto strobes = l.b.select(void_beat, l.b.constant(p.data_bits / 8, 0), w.at("be"));
  auto wstrb = l.latch("w.strobes", strobes, load);
  l.b.update(wlast, l.b.select(load, in_last, wlast));
  l.b.update(wdrop, l.b.select(load, drop, wdrop));
  aw_id = target_id_bits ? descriptor_id(aw, 0) : zero;
  ValueId held_aw = zero, held_w = zero;
  if (write_timer) {
    held_aw = l.state("timeout.awvalid", 1);
    held_w = l.state("timeout.wvalid", 1);
  }
  address_face("aw", aw, aw_shown, aw_id, write_timer ? std::optional{held_aw} : std::nullopt);
  out("axi", "wvalid", write_timer ? l.b.select(failed, held_w, wvalid) : wvalid);
  out("axi", "wdata", wdata);
  out("axi", "wstrb", wstrb);
  out("axi", "wlast", write_timer ? frozen("timeout.wlast", wlast) : wlast);
  if (p.axi3 && target_id_bits) {
    // With one burst the W entry beat belongs to the main entry, so WID is its AWID; with several
    // the beat keeps the ID of its burst: the head's own, else the main entry's.
    auto wid = aw_id;
    if (several)
      wid =
          l.latch("w.id", l.b.select(collecting, descriptor_id(aw, 0), descriptor_id(w, 0)), load);
    out("axi", "wid", id_to_port(write_timer ? frozen("timeout.wid", wid) : wid));
  }
  if (write_timer) {
    const auto awready = operand(aw_fire), wready = operand(w_fire);
    l.b.update(held_aw, l.b.select(failed, l.b.select(awready, zero, held_aw),
                                   l.both(active(aw_shown), l.inv(aw_fire))));
    l.b.update(held_w,
               l.b.select(failed, l.b.select(wready, zero, held_w), l.both(wvalid, l.inv(w_fire))));
  }
}

EngineLowering::ParkedB EngineLowering::park_b(ValueId bid, ValueId bresp) {
  ParkedB b{zero, bid, bresp, bid, bresp};
  if (!p.merger_hints && !ilv)
    return b;
  b.parked = l.state("w.b_parked", 1);
  if (target_id_bits) {
    b.id_reg = l.state_like("w.b_parked_id", bid);
    b.id = l.b.select(b.parked, *b.id_reg, bid);
  }
  b.resp_reg = l.state_like("w.b_parked_resp", bresp);
  b.resp = l.b.select(b.parked, *b.resp_reg, bresp);
  return b;
}

// Without the merger BREADY takes only a B that names the oldest write of its target ID; behind
// it BREADY reads no BID, and a B that names no write whose turn has come parks and is matched
// from the register. After expiry BREADY stays high and a late B is taken and discarded.
std::pair<ValueId, ValueId> EngineLowering::take_b(const ParkedB &b, ValueId spare,
                                                   ValueId any_local, ValueId any_match) {
  // Behind the merger a B in the main entry that the merger does not offer holds BREADY low too.
  if (!p.merger_hints && !ilv) {
    const auto bready = active(l.both(l.inv(spare), l.both(l.inv(any_local), any_match)));
    out("axi", "bready", timeout ? l.either(bready, active(failed)) : bready);
    return {l.bind("w.b_fire", l.both(bready, in("axi", "bvalid", 1))), zero};
  }
  // In interleave mode the B pipe's full flag alone holds BREADY low.
  const auto unoffered =
      ilv ? zero : l.both(bf, l.inv(l.b.input("nl_hint_write_picked", 1, "hint", "write_picked")));
  const auto room = l.both(l.inv(l.either(spare, unoffered)), l.inv(any_local));
  const auto bready = active(l.both(room, l.inv(b.parked)));
  out("axi", "bready", timeout ? l.either(bready, active(failed)) : bready);
  const auto arriving = l.both(bready, in("axi", "bvalid", 1));
  const auto fire =
      l.bind("w.b_fire", active(l.both(l.either(b.parked, arriving), l.both(room, any_match))));
  const auto load = l.both(arriving, l.inv(fire));
  const auto keep = l.both(l.either(b.parked, arriving), l.inv(fire));
  l.b.update(b.parked, timeout ? l.both(keep, l.inv(failed)) : keep);
  if (b.id_reg)
    l.b.update(*b.id_reg, l.b.select(load, b.bid, *b.id_reg));
  l.b.update(*b.resp_reg, l.b.select(load, b.bresp, *b.resp_reg));
  return {fire, load};
}

std::optional<ValueId> EngineLowering::b_user(const ParkedB &b, ValueId fire, ValueId load) {
  if (!p.response_user_bits || !p.response_user_port_bits)
    return std::nullopt;
  const auto port = l.fit(in("axi", "buser", p.response_user_port_bits), p.response_user_bits);
  const auto user = l.b.select(fire, port, l.b.constant(p.response_user_bits, 0));
  return p.merger_hints || ilv
             ? l.b.select(l.both(fire, b.parked), l.latch("w.b_parked_user", port, load), user)
             : user;
}

// The local code of a write: DECERR for a window miss or an error request, else SLVERR; a failed
// exclusive write answers OKAY, a target timeout included.
ValueId EngineLowering::write_local_code() {
  auto code =
      windowed ? l.b.select(missed_w, l.b.constant(2, 1), l.b.constant(2, 2)) : l.b.constant(2, 2);
  return network ? l.b.select(excluded, l.b.constant(2, 0), code) : code;
}

void EngineLowering::write_respond() {
  if (ilv)
    join_states();
  const auto bresp = in("axi", "bresp", 2);
  const auto bid = target_id_bits ? id_from_port(in("axi", "bid", target_id_bits)) : zero;
  WriteFace face{aw, aw.at("address"), {}, zero, zero};
  if (several)
    write_respond_several(bresp, bid, face);
  else
    write_respond_single(bresp, bid, face);
  if (ilv)
    return;
  response_face("wrsp", face.fields, p.write_only ? l.either(bf, face.through) : bf, face.address,
                face.stored, one, one, l.b.constant(p.data_bits, 0), face.exclusive, bstored_user);
}

// Several bursts: the write slots, the B match and the response stage.
void EngineLowering::write_respond_several(ValueId bresp, ValueId bid, WriteFace &face) {
  ValueId bpop{};
  // The main burst's error flag including a beat taken in the push cycle.
  const auto bad_now = l.either(bad, l.both(main_beat, beat_error));
  const auto alloc = first_of(l, free_slot, std::max(1U, index_bits));
  // Responses: a B selects the slot of its target ID whose older writes of
  // the same source are done; a local slot completes when its turn comes.
  // A failed target answers every slot locally (in the same order) and matches no B.
  std::vector<ValueId> network_slot, matched, eligible_network, eligible_local, slot_ids;
  const auto held_b = park_b(bid, bresp);
  bresp = held_b.resp;
  for (const auto &slot : ws) {
    const auto turn = l.eq(slot.ahead, 0);
    const auto local = timeout ? l.either(slot.local, failed) : slot.local;
    network_slot.push_back(l.both(slot.valid, l.inv(local)));
    eligible_network.push_back(l.both(network_slot.back(), turn));
    eligible_local.push_back(l.both(slot.valid, l.both(local, turn)));
    slot_ids.push_back(target_id_bits ? write_slot_id(slot) : zero);
    matched.push_back(
        l.both(eligible_network.back(),
               target_id_bits ? l.b.binary(Op::Equal, slot_ids.back(), held_b.id) : one));
  }
  // A local slot whose turn has come completes first, and a B completes only the oldest write of
  // its target ID: a B for a burst still in the issue stage, or for no burst at all, waits
  // (take_b; a protocol error stalls rather than completing another burst). A slot freed in
  // the cycle the stage pushes is never the slot pushed, and the pushed slot's count of older
  // writes leaves the freed one out.
  const auto any_local = any_of(l, eligible_local);
  const auto any_match = l.bind("w.id_match", any_of(l, matched));
  // Responses leave face.through a two-entry stage (beat_stage.hpp): a completion enters while the
  // spare entry is empty, so BREADY reads that register and one B passes per cycle.
  auto bspare = l.state("w.response_spare", 1);
  const auto [b_fire, load] = take_b(held_b, bspare, any_local, any_match);
  if (!ilv)
    bdone = bpop = l.both(active(bf), in("wrsp", "ready", 1));
  const auto b_slot = first_of(l, matched, std::max(1U, index_bits));
  const auto local_slot = first_of(l, eligible_local, std::max(1U, index_bits));
  const auto local_fire = l.bind("w.local_fire", active(l.both(l.inv(bspare), any_local)));
  const auto freed = l.b.select(local_fire, local_slot, b_slot);
  const auto releasing = l.either(b_fire, local_fire);
  // Whether a slot's target ID is this one: the key of the write response order.
  const auto same_id = [&](unsigned k, ValueId id) {
    return target_id_bits ? l.b.binary(Op::Equal, slot_ids[k], id) : one;
  };
  // A refused one-word head answers as it enters when nothing else competes for the stage and no
  // older write of its target ID waits; it then takes no main entry and no slot.
  const auto head_id = target_id_bits ? descriptor_id(w, 0) : zero;
  auto older = zero;
  for (unsigned k = 0; k < depth; ++k)
    older = l.either(older, l.both(ws[k].valid, same_id(k, head_id)));
  const auto refusable = timeout ? l.both(head_reject, l.inv(failed)) : head_reject;
  const auto refused =
      l.bind("w.refused",
             active(l.both(
                 l.both(take_w, l.both(l.inv(wb), l.both(in_last, refusable))),
                 l.both(l.inv(older), l.both(l.inv(bspare), l.inv(l.either(any_local, b_fire)))))));
  l.b.update(wb, l.b.select(head, l.inv(refused), l.b.select(push, zero, wb)));
  const auto answered = [&](const std::string &name, ValueId value) {
    return l.b.select(releasing, value, w.at(name));
  };
  // A silent completion frees its slot without a response; the pool counts it apart from bpop.
  wsilent = report_only ? l.both(local_fire, silent) : zero;
  auto field = [&](auto member) {
    std::vector<ValueId> values;
    std::ranges::transform(ws, std::back_inserter(values),
                           [&](const WriteSlot &s) { return s.*member; });
    return values;
  };
  const auto freed_source = pick(l, field(&WriteSlot::source), freed);
  const auto freed_bad = pick(l, field(&WriteSlot::bad), freed);
  const auto freed_lock = passthrough || network ? pick(l, field(&WriteSlot::lock), freed) : zero;
  const auto freed_excluded = network ? pick(l, field(&WriteSlot::excluded), freed) : zero;
  auto bcode = l.bind("w.result", result(l, bresp, freed_bad,
                                         passthrough ? std::optional{freed_lock} : std::nullopt));
  if (network)
    bcode = l.b.select(freed_excluded, l.b.constant(2, 0), bcode);
  // A write-only target offers a B to the network in the cycle it arrives while the stage is
  // empty; the B enters the stage only when the network refuses it.
  auto bpush = l.either(report_only ? l.both(releasing, l.inv(wsilent)) : releasing, refused);
  if (p.write_only) {
    face.through = l.both(b_fire, l.inv(bf));
    const auto passed = l.both(face.through, operand(bpop));
    bpush = l.both(bpush, l.inv(passed));
    bdone = l.either(bpop, passed);
  }
  const auto offer = [&](ValueId main, ValueId now) {
    return p.write_only ? l.b.select(bf, main, now) : main;
  };
  const auto refused_code = windowed
                                ? l.b.select(head_missed, l.b.constant(2, 1), l.b.constant(2, 2))
                                : l.b.constant(2, 2);
  const auto bnow = l.b.select(
      b_fire, bcode, l.b.select(local_fire, pick(l, field(&WriteSlot::code), freed), refused_code));
  const auto answer_address =
      l.global(l.b.select(releasing, pick(l, field(&WriteSlot::address), freed), w.at("address")));
  if (ilv) {
    // The B pipe takes the completion; the joined stage (join_responses) takes it on.
    Fields live{{"result", bnow}};
    if (passthrough)
      live["response_exclusive"] =
          l.both(b_fire, exclusive_success(l, bresp, freed_bad, freed_lock));
    else if (network)
      live["response_exclusive"] = l.both(l.both(freed_lock, l.inv(freed_excluded)), l.eq(bnow, 0));
    if (const auto user = b_user(held_b, b_fire, load))
      live["user"] = *user;
    live["size"] = answered("size", pick(l, field(&WriteSlot::size), freed));
    live["source"] = answered("source", freed_source);
    for (const auto &[name, member] : echoed_slot_fields())
      live[name] = answered(name, pick(l, field(member), freed));
    live["address"] = answer_address;
    boffer = {bspare, bpush, live};
  }
  const auto bs_stage = ilv ? Stage{} : stage(l, bf, bspare, bpush, bpop);
  if (!ilv)
    face.stored = offer(staged(l, bs_stage, "w.response", "w.rsp_spare.response", bnow), bnow);
  if (ilv) {
  } else if (passthrough)
    face.exclusive = staged(l, bs_stage, "w.response_exclusive", "w.rsp_spare.response_exclusive",
                            l.both(b_fire, exclusive_success(l, bresp, freed_bad, freed_lock)));
  else if (network)
    face.exclusive =
        l.both(staged(l, bs_stage, "w.response_exclusive", "w.rsp_spare.response_exclusive",
                      l.both(freed_lock, l.inv(freed_excluded))),
               l.eq(face.stored, 0));
  const auto held = [&](const std::string &name, const std::string &spare, ValueId value) {
    return offer(staged(l, bs_stage, name, spare, value), value);
  };
  face.fields.clear();
  if (!ilv) {
    if (const auto user = b_user(held_b, b_fire, load))
      bstored_user = held("w.rsp.user", "w.rsp_spare.user", *user);
    face.fields["size"] = held("w.rsp.size", "w.rsp_spare.size",
                               answered("size", pick(l, field(&WriteSlot::size), freed)));
    face.fields["source"] =
        held("w.rsp.source", "w.rsp_spare.source", answered("source", freed_source));
    // The echoed fields of a write in flight (id, tag) that the network carries.
    for (const auto &[name, member] : echoed_slot_fields())
      face.fields[name] = held("w.rsp." + name, "w.rsp_spare." + name,
                               answered(name, pick(l, field(member), freed)));
    face.address = held("w.rsp.address", "w.rsp_spare.address", answer_address);
  }
  if (owned) {
    l.b.update(owner_gone, releasing);
    l.b.update(gone.at("source"), freed_source);
    if (p.id_bits)
      l.b.update(gone.at("id"), pick(l, field(&WriteSlot::id), freed));
    l.b.update(gone_lock, freed_lock);
  }
  // Older writes of the pushed burst's target ID still in flight.
  const auto freed_id = target_id_bits ? pick(l, slot_ids, freed) : zero;
  auto ahead = l.b.constant(ahead_bits, 0);
  for (unsigned k = 0; k < depth; ++k) {
    const auto freeing = l.both(releasing, l.eq(freed, k));
    ahead = l.b.select(l.both(ws[k].valid, l.both(same_id(k, aw_id), l.inv(freeing))), l.inc(ahead),
                       ahead);
  }
  for (unsigned k = 0; k < depth; ++k) {
    const auto write = l.both(push, l.eq(alloc, k));
    const auto freeing = l.both(releasing, l.eq(freed, k));
    const auto behind =
        l.both(l.both(ws[k].valid, l.inv(freeing)), l.both(releasing, same_id(k, freed_id)));
    l.b.update(ws[k].valid, l.b.select(write, one, l.b.select(freeing, zero, ws[k].valid)));
    l.b.update(ws[k].ahead,
               l.b.select(write, ahead, l.b.select(behind, l.dec(ws[k].ahead), ws[k].ahead)));
    l.b.update(ws[k].local, l.b.select(write, reject, ws[k].local));
    l.b.update(ws[k].bad, l.b.select(write, bad_now, ws[k].bad));
    l.b.update(ws[k].source, l.b.select(write, aw.at("source"), ws[k].source));
    if (owned)
      l.b.update(ws[k].user, l.b.select(write, aw.at("user"), ws[k].user));
    for (const auto &[name, member] : echoed_slot_fields())
      l.b.update(ws[k].*member, l.b.select(write, aw.at(name), ws[k].*member));
    l.b.update(ws[k].address, l.b.select(write, aw.at("address"), ws[k].address));
    l.b.update(ws[k].size, l.b.select(write, aw.at("size"), ws[k].size));
    l.b.update(ws[k].code, l.b.select(write, write_local_code(), ws[k].code));
    if (passthrough || network)
      l.b.update(ws[k].lock, l.b.select(write, aw.at("lock"), ws[k].lock));
    if (network) {
      l.b.update(ws[k].excluded, l.b.select(write, excluded, ws[k].excluded));
      l.b.update(ws[k].length, l.b.select(write, aw.at("length"), ws[k].length));
      l.b.update(ws[k].burst, l.b.select(write, aw.at("burst"), ws[k].burst));
    }
  }
}

// One burst: its B or local answer in the response register.
void EngineLowering::write_respond_single(ValueId bresp, ValueId bid, WriteFace &face) {
  ValueId bpop{};
  // One burst: BREADY waits until AW is accepted and every beat has left the stage, and takes any
  // B (another BID answers SLVERR); a rejected burst answers locally with its last drained beat.
  // A failed target drains the burst in flight locally and keeps BREADY high: a late B is
  // accepted and discarded.
  const auto rejecting = timeout ? l.either(reject, failed) : reject;
  const auto left = l.both(wd, l.inv(wf));
  // In interleave mode the burst's B is held from its pipe until it leaves the joined stage.
  b_held = ilv ? l.either(bf, l.both(jfull, jwrite)) : bf;
  auto bready =
      active(l.both(wb, l.both(left, l.both(l.inv(wp), l.both(l.inv(rejecting), l.inv(b_held))))));
  out("axi", "bready", timeout ? l.either(bready, active(failed)) : bready);
  auto b_fire = l.both(bready, in("axi", "bvalid", 1));
  if (!ilv)
    bdone = bpop = l.both(active(bf), in("wrsp", "ready", 1));
  const auto bfault =
      target_id_bits ? l.either(bad, l.inv(l.b.binary(Op::Equal, bid, aw_id))) : bad;
  auto bcode =
      l.bind("w.result",
             result(l, bresp, bfault, passthrough ? std::optional{aw.at("lock")} : std::nullopt));
  // A refused burst answers as its last word enters, by the head's own decode when that word is
  // the head; a burst drained after the target failed answers with its last drained beat.
  const auto refused =
      l.both(l.either(l.both(head, head_reject),
                      l.both(l.inv(head), timeout ? l.both(reject, l.inv(failed)) : reject)),
             l.both(take_w, in_last));
  const auto drained =
      timeout
          ? l.both(failed, l.both(l.either(left, l.both(beat, wlast)), l.both(wb, l.inv(b_held))))
          : zero;
  const auto local_b = active(l.either(refused, drained));
  const auto bpush = l.either(b_fire, local_b);
  const auto bfired = network ? l.b.select(excluded, l.b.constant(2, 0), bcode) : bcode;
  if (ilv) {
    // The B pipe takes the B (or the local one); the joined stage takes it on, and the burst
    // keeps the main entry until its B leaves (join_responses).
    const auto bnow = l.b.select(local_b, write_local_code(), bfired);
    Fields live{{"result", bnow}};
    if (network)
      live["response_exclusive"] = l.both(aw.at("lock"), l.both(l.inv(excluded), l.eq(bnow, 0)));
    else if (passthrough)
      live["response_exclusive"] =
          l.both(l.inv(local_b), exclusive_success(l, bresp, bfault, aw.at("lock")));
    if (const auto buser = target_user("buser", l.inv(local_b)))
      live["user"] = *buser;
    const auto store = report_only ? l.both(bpush, l.inv(silent)) : bpush;
    bsilent_done = report_only ? l.both(bpush, silent) : zero;
    boffer = {bf, store, live};
    return;
  }
  // A refused burst's code is its own decode, kept in the main entry; only a B or a drained answer
  // is stored.
  const auto answer = l.either(b_fire, drained);
  face.stored =
      l.b.select(reject, write_local_code(),
                 l.latch("w.response", l.b.select(b_fire, bfired, write_local_code()), answer));
  if (const auto buser = target_user("buser", l.inv(local_b))) {
    const auto held_user = l.latch("w.response_user", *buser, bpush);
    bstored_user = p.write_only ? l.b.select(bf, held_user, *buser) : held_user;
  }
  // A write-only target offers its B to the network in the cycle it arrives while no response
  // waits (no read response to alternate with); it is stored only when the network refuses it.
  if (p.write_only) {
    face.through = report_only ? l.both(b_fire, l.inv(silent)) : b_fire;
    face.stored = l.b.select(bf, face.stored, bfired);
  }
  // exclusive_okay of the B: a network exclusive write that passed its
  // monitor and completed without error, or the target's own EXOKAY.
  if (network)
    face.exclusive = l.both(aw.at("lock"), l.both(l.inv(excluded), l.eq(face.stored, 0)));
  else if (passthrough)
    face.exclusive =
        l.latch("w.response_exclusive",
                l.both(l.inv(local_b), exclusive_success(l, bresp, bfault, aw.at("lock"))), bpush);
  // A silent B is dropped here and ends its burst at once.
  auto bstore = report_only ? l.both(bpush, l.inv(silent)) : bpush;
  if (report_only)
    bdone = l.either(bpop, l.both(bpush, silent));
  if (p.write_only) {
    const auto passed = l.both(face.through, operand(bpop));
    bstore = l.both(bstore, l.inv(passed));
    bdone = l.either(bdone, passed);
  }
  const auto head_busy = report_only ? l.inv(l.both(local_b, silent)) : one;
  l.b.update(wb, l.b.select(head, head_busy, l.b.select(bdone, zero, wb)));
  l.b.update(bf, l.b.select(bstore, one, l.b.select(bpop, zero, bf)));
}

void EngineLowering::read_accept() {
  // ---------------- reads ----------------
  r = request("rreq");
  if (target_id_bits && !several)
    r["target_id"] = descriptor_id(r, 1);
  rs.resize(depth);
  for (unsigned k = 0; k < depth; ++k) {
    const auto at = "r.slot" + std::to_string(k) + ".";
    for (const auto &name : descriptor)
      rs[k][name] = l.state_like(at + name, r.at(name));
    rejects.push_back(l.state(at + "reject", 1));
    misses.push_back(l.state(at + "miss", 1));
  }
  count = l.state("r.count", count_bits);
  pending = l.state("r.unissued", count_bits);
  qtail = ilv && several ? zero : l.state("r.tail", std::max(1U, index_bits));
  qissue = l.state("r.issue", std::max(1U, index_bits));
  // In interleave mode r.full is the joined stage's, and with several bursts each entry counts
  // its own beats.
  rf = ilv ? jfull : l.state("r.full", 1);
  if (!(ilv && several)) {
    rk = l.state("r.beat", p.len_bits);
    ra = l.address_state("r.address", address_bits);
  }
  rspare = single_entry ? zero : l.state("r.spare", 1);
  // A write head word offered here takes the last free entry before a read (one request transport
  // never offers both; behind a merge an older waiting read goes first, read_first).
  // Heads admitted ahead are counted already, so no head here claims an entry.
  auto claim = p.admitted ? zero : l.both(w.at("valid"), l.inv(collecting));
  if (several && !p.admitted)
    claim = l.both(claim, l.inv(head_in));
  read_claim = claim;
  const auto bits = pool_bits + 1;
  const auto claimed = p.one_head ? zero : l.both(claim, l.inv(read_first));
  pool_r = l.below(l.plus(l.fit(pool, bits), l.fit(claimed, bits)), l.b.constant(bits, depth));
  pool_r = l.bind("pool.read_ok", pool_r);
  // Heads admitted ahead of the outlet are counted already.
  // A refused request (answered here) takes an entry like any other head.
  const auto read_ok = p.admitted ? one : pool_r;
  rr_ready = active(read_ok);
  if (several) {
    for (unsigned k = 0; k < depth; ++k) {
      const auto at = "r.slot" + std::to_string(k) + ".";
      rvalid.push_back(l.state(at + "valid", 1));
      rissued.push_back(l.state(at + "issued", 1));
      rdone.push_back(l.state(at + "received", 1));
    }
    rcur = l.state("r.current", std::max(1U, index_bits));
    // A read takes the next entry in ring order once it is free and no other read waits for its
    // AR, or the one waiting is passed to the target in this cycle (one AR entry).
    // The occupancy selects empty admission or refill of the sole pending AR.
    // In the refill arm, pending == 1 already supplies the issue qualification.
    const auto ar_room =
        l.b.select(l.eq(pending, 0), one, l.both(l.eq(pending, 1), issue_ready_now()));
    auto room = ilv ? ar_room : l.both(l.both(read_ok, l.inv(pick(l, rvalid, qtail))), ar_room);
    if (ilv) {
      // Interleave mode: reads complete in any order, so a read takes the lowest free entry.
      std::vector<ValueId> free;
      std::ranges::transform(rvalid, std::back_inserter(free), [&](ValueId v) { return l.inv(v); });
      ralloc = first_of(l, free, std::max(1U, index_bits));
      room = l.both(room, l.both(read_ok, any_of(l, free)));
      age.resize(depth);
      for (unsigned i = 0; i < depth; ++i)
        for (unsigned j = 0; j < depth; ++j)
          age[i].push_back(i < j ? l.state("r.age" + std::to_string(i) + "_" + std::to_string(j), 1)
                                 : zero);
    }
    // With owner flags an exclusive read waits for older reads of its source and AxID, and any read
    // for the exclusive ones.
    if (owned)
      for (unsigned k = 0; k < depth; ++k)
        room = l.both(room, l.inv(l.both(rvalid[k], owner_order(rs[k], rs[k].at("lock"), r))));
    rr_ready = active(room);
  } else {
    // The entry receiving beats runs ahead of the head, whose beat waits in
    // the stage: received counts the entries whose last beat is staged.
    qhead = l.state("r.head", 1);
    qrecv = l.state("r.receive", 1);
    received = l.state("r.received", count_bits);
    front = select_fields(qhead);
    recv = select_fields(qrecv);
    const auto recv_reject = pick(l, rejects, qrecv);
    recv_miss = pick(l, misses, qrecv);
    // A failed target answers every beat left locally.
    recv_rejecting = timeout ? l.either(recv_reject, failed) : recv_reject;
  }
  rr_ready = l.bind("r.ready", rr_ready);
  out("rreq", "ready", rr_ready);
  take_r = l.bind("r.accept", l.both(r.at("valid"), rr_ready));
  if (p.merged)
    l.b.update(read_first,
               l.b.select(take_r, zero,
                          l.b.select(l.both(r.at("valid"), l.inv(read_claim)), one, read_first)));
  rmiss = miss(r.at("address"));
  rreject = p.write_only ? one : windowed ? l.either(r.at("error"), rmiss) : r.at("error");
  if (indexed)
    rreject = l.either(rreject, unknown_source(r.at("source"), 1));
  // An exclusive read records its reservation at its head; every write that goes to the target
  // drops the reservations it overlaps. With one burst the in-flight pool admits the read only once
  // no write is in flight, so no write can overlap its head. With several, a read changes no
  // reservation while a write that reaches the target overlaps it: the head arriving now, the issue
  // stage's entries and the slots until their B. A write answered locally (failed exclusive, window
  // miss, error, timed-out target) never reaches the target.
  if (network) {
    const auto region = monitor->region(r.at("address"), r.at("length"), r.at("size"));
    const auto incoming =
        packet_range(l, w.at("address"), w.at("length"), w.at("size"), w.at("burst"));
    // A failing exclusive write changes nothing: it neither records nor clears.
    const auto reaching = l.both(head, l.both(l.inv(seen), l.inv(failing)));
    const auto read = l.bind("r.exclusive_read", l.both(take_r, r.at("lock")));
    const auto writes = l.bind("w.exclusive_clear", reaching);
    auto record = read;
    if (several) {
      const auto over = [&](ValueId held, const Fields &f, ValueId address, ValueId size) {
        return l.both(
            held, monitor->overlaps(packet_range(l, address, f.at("length"), size, f.at("burst")),
                                    region));
      };
      auto contaminated = l.both(reaching, monitor->overlaps(incoming, region));
      contaminated = l.either(contaminated, over(l.both(wb, l.inv(l.either(reject, excluded))), aw,
                                                 aw.at("address"), aw.at("size")));
      contaminated = std::accumulate(
          ws.begin(), ws.end(), contaminated, [&](ValueId any, const WriteSlot &slot) {
            return l.either(any,
                            over(l.both(slot.valid, l.inv(l.either(slot.local, slot.excluded))),
                                 {{"length", slot.length}, {"burst", slot.burst}}, slot.address,
                                 slot.size));
          });
      record = l.both(read, l.inv(l.bind("r.exclusive_contaminated", contaminated)));
    }
    monitor->update(record, monitor->key(r), region, writes, incoming);
  }
  if (timeout)
    rreject = l.either(rreject, failed);
}

ValueId EngineLowering::issue_ready_now() {
  // AR in queue order; a rejected entry is passed over without AR, and so is
  // an entry still owing AR at a failed target.
  if (issue_built)
    return issue_ready;
  issue_built = true;
  const auto issuing_reject = pick(l, rejects, qissue);
  const auto has_pending = l.inv(l.eq(pending, 0));
  arvalid = l.both(has_pending, l.inv(issuing_reject));
  if (timeout)
    held_ar = l.state("timeout.arvalid", 1);
  const auto target_arready = in("axi", "arready", 1);
  ar_fire = l.both(active(arvalid), target_arready);
  skip = active(l.both(has_pending, timeout ? l.either(issuing_reject, failed) : issuing_reject));
  // Rejection advances without AR, absorbing the VALID qualifier on READY.
  const auto ready = l.either(target_arready, issuing_reject);
  issue_ready = active(timeout ? l.either(ready, failed) : ready);
  issued = l.bind("r.issued", l.both(has_pending, issue_ready));
  return issue_ready;
}

void EngineLowering::read_issue() {
  issue_ready_now();
  const auto issuing = select_fields(qissue);
  address_face("ar", issuing, arvalid, target_id_bits ? descriptor_id(issuing, 1) : zero,
               timeout ? std::optional{held_ar} : std::nullopt);
  if (timeout)
    l.b.update(held_ar, l.b.select(failed, l.b.select(operand(ar_fire), zero, held_ar),
                                   l.both(active(arvalid), l.inv(ar_fire))));
}

void EngineLowering::segment_response(ValueId kept) {
  stored_last = l.state("r.response_last", 1);
  purging = zero;
  if (p.align_wrap)
    purging = l.state("r.align_purge", 1);
  beat_stage::ResponseSegment window{l, l.state("r.segment_phase", 2)};
  const auto shown = l.either(l.either(rspare, kept), purging);
  const auto inside = inside_block();
  response_offered = window.offered(rf, stored_last, shown, inside);
  response_cut = window.cut(stored_last, shown, zero, inside);
  rpop = l.both(active(response_offered), in("rrsp", "ready", 1));
  window.advance(rpop, shown, rf, inside);
}

void EngineLowering::update_segment_last(const Stage &stage, ValueId moving, ValueId mload,
                                         ValueId inject) {
  const auto entering =
      p.align_wrap ? l.either(inject, mload) : l.either(stage.to_main, stage.shift);
  const auto tail = p.align_wrap ? l.either(inject, moving) : moving;
  l.b.update(stored_last, l.pick(entering, tail, stored_last));
  if (p.interleave_beats > 1)
    count_block_word(entering, tail);
}

// Position zero is a block boundary. A packet tail resets the position.
ValueId EngineLowering::inside_block() {
  if (p.interleave_beats < 2)
    return zero;
  block_count = l.state("r.join_block", std::bit_width(p.interleave_beats) - 1);
  return l.inv(l.eq(block_count, 0));
}

void EngineLowering::count_block_word(ValueId entering, ValueId last) {
  l.b.update(block_count,
             l.pick(entering, l.pick(last, l.num(l.width(block_count), 0), l.inc(block_count)),
                    block_count));
}

// WRAP read alignment sits between the stage's spare and main entries: a beat moving into the
// main entry (from the spare entry, else as it arrives) that is an aligned read's first beat
// answers its lanes from the start address up and holds the lanes below, with its result,
// address, entry and user field; after that read's last target beat the next main entry load
// is the held answer, the packet's last word, while the spare entry and the arriving beat wait.
void EngineLowering::read_receive() {
  local_error_states();
  // The receiving entry's beats: R from the target once issued, or local
  // beats, while the stage's spare entry is free (behind a merge: while its one entry is free or
  // leaves in this cycle).
  ValueId receiving{}, target_ready = one;
  const auto rid = target_id_bits ? id_from_port(in("axi", "rid", target_id_bits)) : zero;
  if (several) {
    // Out of order across target IDs: a burst's beats arrive together, from the target (R of its
    // ID: the oldest issued entry of that ID; an R of no such ID fills the oldest issued entry and
    // answers SLVERR) or locally (a rejected entry, or any entry of a failed target, once every
    // older entry is received; also in the cycle it is passed over).
    const unsigned width = std::max(1U, index_bits);
    std::vector<ValueId> waiting, from_target, matching, rejecting;
    for (unsigned k = 0; k < depth; ++k) {
      const auto open = l.both(rvalid[k], l.inv(rdone[k]));
      const auto passed = l.both(skip, l.eq(qissue, k));
      rejecting.push_back(timeout ? l.either(rejects[k], failed) : rejects[k]);
      waiting.push_back(l.both(open, l.either(rissued[k], passed)));
      from_target.push_back(l.both(l.both(open, rissued[k]), l.inv(rejecting.back())));
      matching.push_back(
          target_id_bits
              ? l.both(from_target.back(), l.b.binary(Op::Equal, descriptor_id(rs[k], 1), rid))
              : from_target.back());
    }
    const auto busy = l.inv(l.eq(rk, 0));
    const auto oldest = first_from(l, waiting, qtail, width);
    rlocal = l.bind("r.local", l.b.select(busy, pick(l, rejecting, rcur),
                                          l.both(any_of(l, waiting), pick(l, rejecting, oldest))));
    const auto by_id = l.b.select(any_of(l, matching), first_from(l, matching, qtail, width),
                                  first_from(l, from_target, qtail, width));
    rsel = l.bind("r.select", l.b.select(busy, rcur, l.b.select(rlocal, oldest, by_id)));
    recv = select_fields(rsel);
    recv_miss = pick(l, misses, rsel);
    recv_rejecting = rlocal;
    receiving = l.either(busy, any_of(l, waiting));
    target_ready = l.either(busy, any_of(l, from_target));
  } else {
    receiving =
        l.both(l.inv(l.b.binary(Op::Equal, count, received)), l.either(l.eq(pending, 0), skip));
  }
  if (!segment) {
    rpop = l.both(active(rf), l.both(in("rrsp", "ready", 1), l.inv(zero)));
  }
  // Behind the response merger an R beat in the main entry that it does not offer also holds
  // RREADY low.
  const auto held =
      several && p.merger_hints && !p.merged
          ? l.either(rspare,
                     l.both(rf, l.inv(l.b.input("nl_hint_read_picked", 1, "hint", "read_picked"))))
          : rspare;
  auto read_room =
      active(l.both(receiving, single_entry ? l.either(l.inv(rf), rpop) : l.inv(held)));
  auto rready = l.both(read_room, l.both(target_ready, l.inv(recv_rejecting)));
  out("axi", "rready", timeout ? l.either(rready, active(failed)) : rready);
  auto r_fire = l.both(rready, in("axi", "rvalid", 1));
  // With several bursts a silent beat waits for the stage to empty, so that the burst it completes
  // is the queue head and its drop never meets the pop of a staged last beat.
  const auto local_room =
      several && report_only ? l.both(read_room, l.inv(l.both(silent, rf))) : read_room;
  rpush = l.bind("r.push", l.either(r_fire, l.both(local_room, recv_rejecting)));
  local_error_now = local_error_word();
  rlast = l.bind("r.last", l.either(local_error_now, l.b.binary(Op::Equal, rk, recv.at("length"))));
  auto rlast_bad = l.inv(l.b.binary(Op::Equal, in("axi", "rlast", 1), rlast));
  auto rid_bad = target_id_bits ? l.inv(l.b.binary(Op::Equal, rid, descriptor_id(recv, 1))) : zero;
  if (timeout) {
    l.watchdog(failed, timeout, l.either(l.either(w.at("valid"), r.at("valid")), l.inv(pool_empty)),
               l.either(bf, rf));
    out("status", "timed_out", failed);
  }
  // Target mode keeps the response and fault for the EXOKAY decision.
  const auto rresp = in("axi", "rresp", 2);
  const auto rfault = l.either(recv_rejecting, l.either(rid_bad, rlast_bad));
  auto rcode = l.bind("r.result", passthrough ? result(l, rresp, rfault, recv.at("lock"))
                                              : result(l, rresp, rfault));
  if (windowed)
    rcode = l.b.select(recv_miss, l.b.constant(2, 1), rcode);
  const auto data_ok =
      l.both(l.inv(recv_miss),
             l.both(l.inv(rfault),
                    l.either(l.eq(rresp, 0),
                             passthrough ? l.both(recv.at("lock"), l.eq(rresp, 1)) : zero)));
  auto rdata = in("axi", "rdata", p.data_bits);
  const auto first = l.eq(rk, 0);
  current = l.b.select(first, recv.at("address"), ra);
  // A silent beat is dropped here; the burst still advances.
  auto kept = report_only ? l.both(rpush, l.inv(silent)) : rpush;
  Stage rs_stage{};
  ValueId aligned_first{}, aligned_tail{}, mload{}, inject{};
  if (segment)
    segment_response(kept);
  if (single_entry)
    l.b.update(rf, l.b.select(kept, one, l.b.select(rpop, zero, rf)));
  else if (!p.align_wrap)
    rs_stage = stage(l, rf, rspare, kept, rpop);
  else {
    if (!segment)
      purging = l.state("r.align_purge", 1);
    const auto opening = l.either(l.inv(rf), rpop);
    inject = l.both(opening, purging);
    const auto shift = l.both(opening, l.both(l.inv(purging), rspare));
    const auto to_main = l.both(opening, l.both(l.inv(purging), l.both(l.inv(rspare), kept)));
    const auto to_spare = l.both(kept, l.inv(to_main));
    rs_stage = Stage{to_main, to_spare, shift};
    mload = l.either(shift, to_main);
    l.b.update(rf, l.b.select(l.either(inject, mload), one, l.b.select(rpop, zero, rf)));
    l.b.update(rspare, l.b.select(to_spare, one, l.b.select(shift, zero, rspare)));
  }
  local_error_load(kept, rs_stage);
  // The value moving into the main entry: the spare entry's, else the arriving one.
  const auto candidate = [&](const std::string &spare_name, ValueId value) {
    return l.b.select(rspare, l.latch(spare_name, value, rs_stage.to_spare), value);
  };
  ValueId below{};
  ValueId align_index = zero, purge_index = zero;
  if (p.align_wrap) {
    const unsigned lanes = p.data_bits / 8, lw = static_cast<unsigned>(std::bit_width(lanes)) - 1;
    const auto arriving_below =
        l.b.unary(Op::BitNot,
                  l.shl(l.b.unary(Op::BitNot, l.num(lanes, 0), lanes),
                        l.b.slice(recv.at("start_address"), 0, lw), lanes - 1),
                  lanes);
    const auto aligned = l.both(aligning(recv), l.inv(recv_rejecting));
    below = strobe_lanes(l.b, candidate("r.spare_align_below", arriving_below), lanes);
    const auto cand_aligned = candidate("r.spare_aligned", aligned);
    aligned_first = l.both(cand_aligned, candidate("r.spare_align_first", first));
    aligned_tail = l.both(cand_aligned, candidate("r.spare_align_tail", rlast));
    l.b.update(purging,
               l.b.select(inject, zero, l.b.select(l.both(mload, aligned_tail), one, purging)));
    align_index = several ? candidate("r.spare_align_index", rsel) : zero;
    purge_index = l.latch("r.align_index", align_index, l.both(mload, aligned_tail));
    align_save = l.both(mload, aligned_first);
    align_into = align_index;
  }
  // A main entry field: the held answer's value on injection (held: latched from the aligned
  // first beat), else the candidate (transformed by load) as it moves in.
  const auto entry = [&](const std::string &name, const std::string &spare, ValueId value,
                         std::optional<ValueId> held = std::nullopt,
                         const std::function<ValueId(ValueId)> &load = {}) {
    if (single_entry)
      return l.latch(name, value, kept);
    if (segment && name == "r.response_last") {
      auto moving = candidate(spare, value);
      if (load && p.align_wrap)
        moving = load(moving);
      update_segment_last(rs_stage, moving, mload, inject);
      return stored_last;
    }
    if (!p.align_wrap)
      return staged(l, rs_stage, name, spare, value);
    const auto main = l.state_like(name, value);
    auto moving = candidate(spare, value);
    auto inject_value = held.value_or(zero);
    if (!held) {
      // Held in the field's own form (a global address keeps its stored width).
      const auto captured = name == "r.data" ? l.both(moving, below) : moving;
      inject_value = align_word(name, captured, align_save, align_index, purge_index);
    }
    if (load)
      moving = load(moving);
    l.b.update(main, l.b.select(inject, inject_value, l.b.select(mload, moving, main)));
    return main;
  };
  rexclusive = passthrough ? entry("r.response_exclusive", "r.spare_exclusive",
                                   exclusive_success(l, rresp, rfault, recv.at("lock")), zero)
                           : zero;
  stored_result = entry("r.response", "r.spare_response", rcode);
  stored_data =
      entry("r.data", "r.spare_data", l.b.select(data_ok, rdata, l.b.constant(p.data_bits, 0)),
            std::nullopt, [&](ValueId moving) {
              // The aligned first beat keeps its lanes below the start address for the held answer.
              return l.b.select(aligned_first,
                                l.both(moving, l.b.unary(Op::BitNot, below, p.data_bits)), moving);
            });
  stored_first = entry("r.response_first", "r.spare_first", first, zero);
  // An aligned read's last target beat is not its packet's last word: the held answer is.
  stored_last = entry("r.response_last", "r.spare_last", rlast, one,
                      [&](ValueId moving) { return l.both(moving, l.inv(aligned_tail)); });
  stored_addr = entry("r.response_address", "r.spare_address", l.global(current));
  // The entry of each staged beat names its response fields.
  if (several) {
    rout = entry("r.response_index", "r.spare_index", rsel);
    front = select_fields(rout);
  }
  // Every beat of a packet carries the target's RUSER of its first beat.
  if (const auto user = target_user("ruser", r_fire)) {
    const auto user_head = l.state("r.user_head", p.response_user_bits);
    l.b.update(user_head, l.b.select(l.both(rpush, first), *user, user_head));
    stored_user = entry("r.response_user", "r.spare_user", l.b.select(first, *user, user_head));
  }
  complete = l.both(rpush, rlast);
  pop = l.bind("r.pop", l.both(rpop, stored_last));
  if (report_only)
    pop = l.either(pop, l.both(complete, silent));
}

void EngineLowering::read_update() {
  align_valid();
  auto advance_pointer = [&](ValueId pointer) {
    return l.b.select(l.eq(pointer, depth - 1),
                      l.b.constant(l.b.ir.values.at(pointer.index).width, 0), l.inc(pointer));
  };
  for (unsigned k = 0; k < depth; ++k) {
    const auto write = l.both(take_r, l.eq(qtail, k));
    for (const auto &name : descriptor)
      l.b.update(rs[k].at(name), l.b.select(write, r.at(name), rs[k].at(name)));
    l.b.update(rejects[k], l.b.select(write, rreject, rejects[k]));
    l.b.update(misses[k], l.b.select(write, l.either(rmiss, r.at("error")), misses[k]));
  }
  l.b.update(qtail, l.b.select(take_r, advance_pointer(qtail), qtail));
  l.b.update(qissue, l.b.select(issued, advance_pointer(qissue), qissue));
  const auto bump = [&](ValueId reg, ValueId up, ValueId down) {
    l.b.update(reg, l.b.select(l.both(up, l.inv(down)), l.inc(reg),
                               l.b.select(l.both(down, l.inv(up)), l.dec(reg), reg)));
  };
  bump(count, take_r, pop);
  bump(pending, take_r, issued);
  if (several) {
    // An entry frees as its last staged beat leaves, or as a silent burst completes.
    const auto popped = l.both(rpop, stored_last);
    const auto silent_done = report_only ? l.both(complete, silent) : zero;
    for (unsigned k = 0; k < depth; ++k) {
      const auto write = l.both(take_r, l.eq(qtail, k));
      const auto freed =
          l.either(l.both(popped, l.eq(rout, k)), l.both(silent_done, l.eq(rsel, k)));
      l.b.update(rvalid[k], l.b.select(write, one, l.b.select(freed, zero, rvalid[k])));
      l.b.update(
          rissued[k],
          l.b.select(write, zero, l.b.select(l.both(issued, l.eq(qissue, k)), one, rissued[k])));
      l.b.update(rdone[k], l.b.select(write, zero,
                                      l.b.select(l.both(complete, l.eq(rsel, k)), one, rdone[k])));
    }
    l.b.update(rcur, l.b.select(rpush, rsel, rcur));
  } else {
    l.b.update(qhead, l.b.select(pop, advance_pointer(qhead), qhead));
    l.b.update(qrecv, l.b.select(complete, advance_pointer(qrecv), qrecv));
    bump(received, complete, pop);
  }
  l.b.update(rk, l.b.select(rpush, l.b.select(rlast, l.b.constant(p.len_bits, 0), l.inc(rk)), rk));
  l.b.update(ra, l.b.select(rpush, advance(l, current, recv, sizes), ra));
  auto coded = stored_result, coded_data = stored_data;
  auto face_first = stored_first, face_address = stored_addr;
  local_error_face(front, coded, coded_data, face_first, face_address);
  // exclusive_okay of each beat: a network exclusive read beat without
  // error, or the target's own EXOKAY.
  if (network)
    rexclusive = l.both(front.at("lock"), l.eq(coded, 0));
  response_face("rrsp", front, segment ? response_offered : rf, face_address, coded, face_first,
                stored_last, coded_data, rexclusive, stored_user);
  if (segment)
    l.b.output("nl_hint_cut", l.both(response_offered, response_cut), "hint", "cut");
  if (p.response_run_bits)
    l.b.output("nl_hint_wrap_end",
               burst_run_end(l, front, face_address, p.data_bits, p.response_run_bits), "hint",
               "wrap_end");
}

// Interleave mode: the oldest entry of a set (index 0 when the set is empty).
ValueId EngineLowering::oldest_of(const std::vector<ValueId> &set) {
  const auto older = [&](unsigned j, unsigned k) { return j < k ? age[j][k] : l.inv(age[k][j]); };
  std::vector<ValueId> first;
  for (unsigned k = 0; k < depth; ++k) {
    auto any_older = zero;
    for (unsigned j = 0; j < depth; ++j)
      if (j != k)
        any_older = l.either(any_older, l.both(set[j], older(j, k)));
    first.push_back(l.both(set[k], l.inv(any_older)));
  }
  return first_of(l, first, std::max(1U, index_bits));
}

// Interleave mode: the joined stage's registers that the B and R sides read before it is built:
// it holds a word (r.full), a B (r.out_write), and the word's last flag (r.response_last, one for
// a B).
void EngineLowering::join_states() {
  if (p.response_direct) {
    // No stage: the merge's word is the response word (join_responses gives it its last flag).
    jfull = jwrite = zero;
    local_error_on = zero;
    return;
  }
  local_error_states();
  jfull = l.state("r.full", 1);
  jwrite = l.state("r.out_write", 1);
  jlast = l.state("r.response_last", 1);
}

// The address of beat `index` of the read descriptor d: its start for FIXED, start plus beat
// times the beat size for INCR, the same inside the wrap container for WRAP (as advance()
// steps it beat by beat).
ValueId EngineLowering::beat_address(const Fields &d, ValueId index) {
  const auto start = d.at("address");
  const auto bits = l.b.ir.values.at(start.index).width;
  const auto zero_word = l.b.constant(bits, 0);
  const auto offset = l.shl(l.fit(index, bits), d.at("size"), sizes - 1);
  const auto next = l.b.binary(Op::Subtract, start, l.b.binary(Op::Subtract, zero_word, offset));
  auto span = l.b.unary(Op::ZeroExtend, d.at("length"), bits);
  span = l.b.binary(Op::Subtract, span, l.b.constant(bits, (std::uint64_t{1} << bits) - 1));
  auto scaled = span;
  for (unsigned i = 1; i < sizes; ++i) {
    span = l.b.binary(Op::Subtract, span, l.b.binary(Op::Subtract, zero_word, span));
    scaled = l.b.select(l.eq(d.at("size"), i), span, scaled);
  }
  const auto mask = l.dec(scaled);
  const auto wrapped =
      l.b.binary(Op::BitOr, l.b.binary(Op::BitAnd, start, l.b.unary(Op::BitNot, mask, bits)),
                 l.b.binary(Op::BitAnd, next, mask));
  return l.global(
      l.b.select(l.eq(d.at("burst"), 0), start, l.b.select(l.eq(d.at("burst"), 2), wrapped, next)));
}

// Interleave mode, read side: an R beat names its read by RID (the oldest read of that target ID
// not yet received), whatever read the previous beat belonged to; each read entry counts its own
// beats. A rejected read streams its local beats, from its first to its last, once every older
// read is received (also in the cycle it is passed over). Beats enter the R pipe.
void EngineLowering::read_receive_joined() {
  ValueId receiving{}, target_ready = one, beat_now{};
  const auto rid = target_id_bits ? id_from_port(in("axi", "rid", target_id_bits)) : zero;
  if (several) {
    std::vector<ValueId> waiting, from_target, matching, rejecting;
    for (unsigned k = 0; k < depth; ++k) {
      const auto open = l.both(rvalid[k], l.inv(rdone[k]));
      const auto passed = l.both(skip, l.eq(qissue, k));
      rejecting.push_back(timeout ? l.either(rejects[k], failed) : rejects[k]);
      waiting.push_back(l.both(open, l.either(rissued[k], passed)));
      from_target.push_back(l.both(l.both(open, rissued[k]), l.inv(rejecting.back())));
      matching.push_back(
          target_id_bits
              ? l.both(from_target.back(), l.b.binary(Op::Equal, descriptor_id(rs[k], 1), rid))
              : from_target.back());
      rbeat.push_back(l.state("r.slot" + std::to_string(k) + ".beat", p.len_bits));
    }
    const auto oldest = oldest_of(waiting);
    // A local stream in progress keeps the entry it started.
    const auto streaming = l.both(l.inv(l.eq(pick(l, rbeat, rcur), 0)), pick(l, rejecting, rcur));
    rlocal = l.bind("r.local",
                    l.either(streaming, l.both(any_of(l, waiting), pick(l, rejecting, oldest))));
    const auto by_id = l.b.select(any_of(l, matching), oldest_of(matching), oldest_of(from_target));
    rsel = l.bind("r.select", l.b.select(streaming, rcur, l.b.select(rlocal, oldest, by_id)));
    recv = select_fields(rsel);
    recv_miss = pick(l, misses, rsel);
    recv_rejecting = rlocal;
    receiving = l.either(streaming, any_of(l, waiting));
    target_ready = any_of(l, from_target);
    beat_now = pick(l, rbeat, rsel);
  } else {
    receiving =
        l.both(l.inv(l.b.binary(Op::Equal, count, received)), l.either(l.eq(pending, 0), skip));
    beat_now = rk;
  }
  purging = zero;
  if (p.align_wrap)
    purging = l.state("r.align_purge", 1);
  // Behind a merge with several reads, its stored beats hold the R stage in the same way.
  const auto held_back = p.merged && several && !p.response_direct
                             ? l.b.input("nl_hint_read_stored", 1, "hint", "read_stored")
                             : purging;
  auto read_room = active(l.both(receiving, l.inv(l.either(rspare, held_back))));
  auto rready = l.both(read_room, l.both(target_ready, l.inv(recv_rejecting)));
  out("axi", "rready", timeout ? l.either(rready, active(failed)) : rready);
  auto r_fire = l.both(rready, in("axi", "rvalid", 1));
  // With several bursts a silent beat waits until no R waits in the pipe or the joined stage.
  const auto local_room =
      several && report_only
          ? l.both(read_room, l.inv(l.both(silent, l.either(rspare, l.both(jfull, l.inv(jwrite))))))
          : read_room;
  rpush = l.bind("r.push", l.either(r_fire, l.both(local_room, recv_rejecting)));
  local_error_now = local_error_word();
  rlast = l.bind("r.last",
                 l.either(local_error_now, l.b.binary(Op::Equal, beat_now, recv.at("length"))));
  auto rlast_bad = l.inv(l.b.binary(Op::Equal, in("axi", "rlast", 1), rlast));
  auto rid_bad = target_id_bits ? l.inv(l.b.binary(Op::Equal, rid, descriptor_id(recv, 1))) : zero;
  if (timeout) {
    l.watchdog(failed, timeout, l.either(l.either(w.at("valid"), r.at("valid")), l.inv(pool_empty)),
               l.either(jfull, l.either(rspare, boffer.full)));
    out("status", "timed_out", failed);
  }
  const auto rresp = in("axi", "rresp", 2);
  const auto rfault = l.either(recv_rejecting, l.either(rid_bad, rlast_bad));
  auto rcode = l.bind("r.result", passthrough ? result(l, rresp, rfault, recv.at("lock"))
                                              : result(l, rresp, rfault));
  if (windowed)
    rcode = l.b.select(recv_miss, l.b.constant(2, 1), rcode);
  const auto data_ok =
      l.both(l.inv(recv_miss),
             l.both(l.inv(rfault),
                    l.either(l.eq(rresp, 0),
                             passthrough ? l.both(recv.at("lock"), l.eq(rresp, 1)) : zero)));
  const auto rdata = in("axi", "rdata", p.data_bits);
  const auto first = l.eq(beat_now, 0);
  current = several ? beat_address(recv, beat_now) : l.b.select(first, recv.at("address"), ra);
  auto kept = report_only ? l.both(rpush, l.inv(silent)) : rpush;
  const auto entry_of = [&](unsigned k) { return several ? l.eq(rsel, k) : one; };
  const auto code = rcode;
  Fields live{{"result", code},
              {"data", l.b.select(p.error_codes ? l.eq(code, 0) : data_ok, rdata,
                                  l.b.constant(p.data_bits, 0))},
              {"first", first},
              {"last", rlast},
              {"address", l.global(current)}};
  if (several)
    live["index"] = rsel;
  if (p.error_codes)
    live["local_error"] = local_error_now;
  if (passthrough)
    live["exclusive"] = exclusive_success(l, rresp, rfault, recv.at("lock"));
  else if (network)
    live["exclusive"] = l.both(recv.at("lock"), l.eq(code, 0));
  // Every beat of a read carries the target's RUSER of its first beat.
  if (const auto user = target_user("ruser", r_fire)) {
    std::vector<ValueId> user_heads;
    for (unsigned k = 0; k < (several ? depth : 1); ++k)
      user_heads.push_back(
          l.state(several ? "r.slot" + std::to_string(k) + ".user_head" : "r.user_head",
                  p.response_user_bits));
    const auto head_user = several ? pick(l, user_heads, rsel) : user_heads.front();
    for (unsigned k = 0; k < user_heads.size(); ++k)
      l.b.update(user_heads[k],
                 l.b.select(l.both(l.both(rpush, first), entry_of(k)), *user, user_heads[k]));
    live["user"] = l.b.select(first, *user, head_user);
  }
  if (p.align_wrap) {
    const auto aligned = l.both(aligning(recv), l.inv(recv_rejecting));
    const auto aligned_first = l.both(rpush, l.both(first, aligned));
    const auto aligned_tail = l.both(rpush, l.both(rlast, aligned));
    const unsigned lanes = p.data_bits / 8;
    const unsigned lw = static_cast<unsigned>(std::bit_width(lanes)) - 1;
    const auto below =
        strobe_lanes(l.b,
                     l.b.unary(Op::BitNot,
                               l.shl(l.b.unary(Op::BitNot, l.num(lanes, 0), lanes),
                                     l.b.slice(recv.at("start_address"), 0, lw), lanes - 1),
                               lanes),
                     lanes);
    const auto raw_data = live.at("data");
    live["data"] =
        l.b.select(l.both(first, aligned),
                   l.both(raw_data, l.b.unary(Op::BitNot, below, p.data_bits)), raw_data);
    live["last"] = l.both(live.at("last"), l.inv(aligned));
    const auto inject = l.both(purging, l.inv(rspare));
    l.b.update(purging, l.b.select(inject, zero, l.b.select(aligned_tail, one, purging)));
    const auto index = several ? rsel : zero;
    const auto purge_index = l.latch("r.align_index", index, aligned_tail);
    align_save = aligned_first;
    align_into = index;
    Fields offered;
    for (const auto &[name, value] : live) {
      if (name == "first" || name == "last" || name == "local_error") {
        offered[name] = l.b.select(inject, name == "last" ? one : zero, value);
        continue;
      }
      const auto captured = name == "data" ? l.both(raw_data, below) : value;
      const auto held = align_word(name, captured, aligned_first, index, purge_index);
      offered[name] = l.b.select(inject, held, value);
      if (name == "address")
        offered[name] = l.global(offered[name]);
    }
    roffer = {rspare, l.either(kept, inject), offered};
  } else {
    roffer = {rspare, kept, live};
  }
  complete = l.both(rpush, rlast);
}

void EngineLowering::join_responses() {
  struct Pipe {
    ValueId valid{};
    Fields word;
    std::vector<std::pair<ValueId, ValueId>> regs; // register, live value
  };
  auto piped = [&](const std::string &stem, const Offer &o) {
    Pipe q{l.either(o.full, o.push), {}, {}};
    for (const auto &[name, v] : o.live) {
      const auto reg = l.state_like(stem + name, v);
      q.word[name] =
          name == "address" ? l.global(l.b.select(o.full, reg, v)) : l.b.select(o.full, reg, v);
      q.regs.emplace_back(reg, v);
    }
    return q;
  };
  const auto rq = piped("r.spare_", roffer);
  const auto bq = piped("w.rsp_spare.", boffer);
  const auto mid = l.state("r.join_mid", 1);
  auto starts = l.inv(mid);
  std::optional<ValueId> conn;
  if (several) {
    conn = l.state("r.join_index", std::max(1U, index_bits));
    starts = l.either(starts, l.inv(l.b.binary(Op::Equal, rq.word.at("index"), *conn)));
  }
  using enum beat_stage::ResponsePhase;
  const auto phase = l.state("r.merge_phase", 3);
  const bool direct = p.response_direct;
  const bool holding = direct || p.interleave_beats > 1;
  const auto available = l.inv(l.both(l.eq(phase, ReadBoundary), l.both(bq.valid, starts)));
  const auto choosing_read =
      l.pick(beat_stage::read_owned(l.b, phase), one,
             l.pick(l.eq(phase, WriteResponse), zero, l.both(rq.valid, available)));
  const auto read = l.bind("r.join_read", l.both(rq.valid, choosing_read));
  const auto shown =
      holding ? l.either(read, l.both(l.inv(choosing_read), bq.valid)) : l.either(read, bq.valid);
  std::optional<beat_stage::ResponseSegment> window;
  if (!direct)
    window.emplace(l, l.state("r.segment_phase", 2));
  std::optional<ValueId> cur_index;
  if (several && !direct)
    cur_index = l.state("r.response_index", std::max(1U, index_bits));
  auto other = l.inv(read);
  if (cur_index)
    other = l.either(other, l.inv(l.b.binary(Op::Equal, rq.word.at("index"), *cur_index)));
  if (direct) {
    jwrite = l.inv(read);
    jlast = l.bind("r.response_last", l.either(jwrite, rq.word.at("last")));
  }
  const bool blocks = p.interleave_beats > 1;
  const auto inside = inside_block();
  const auto cut = direct ? zero : window->cut(jlast, shown, other, inside);
  const auto offered = direct ? shown : window->offered(jfull, jlast, shown, inside);
  const auto ready = l.b.select(jwrite, in("wrsp", "ready", 1), in("rrsp", "ready", 1));
  if (direct && p.error_codes)
    local_error_on = l.both(read, rq.word.at("local_error"));
  const auto leave = l.bind("r.join_leave", l.both(l.both(active(offered), ready), l.inv(zero)));
  const auto room = direct ? leave : l.either(l.inv(jfull), leave);
  const auto take = l.both(active(shown), room);
  const auto r_taken = l.bind("r.join_take", l.both(take, read));
  const auto b_taken = l.bind("w.join_take", l.both(take, l.inv(read)));
  for (const auto &[q, o, taken] : {std::tuple{&rq, &roffer, r_taken}, {&bq, &boffer, b_taken}}) {
    const auto load = l.both(l.both(o->push, l.inv(o->full)), l.inv(taken));
    for (const auto &[reg, v] : q->regs)
      l.b.update(reg, l.b.select(load, v, reg));
    l.b.update(o->full, l.both(l.either(o->full, o->push), l.inv(taken)));
  }
  const auto block_end = p.interleave_beats > 1 ? l.eq(block_count, p.interleave_beats - 1) : zero;
  const auto num = [&](beat_stage::ResponsePhase p) { return l.num(3, p); };
  const auto boundary = l.pick(r_taken, num(ReadBoundary), num(Available));
  const auto reading = holding ? l.pick(r_taken,
                                        l.pick(l.either(rq.word.at("last"), block_end),
                                               num(ReadBoundary), num(ReadTransfer)),
                                        num(ReadPacket))
                               : boundary;
  const auto writing = l.pick(bq.valid, l.pick(room, num(Available), num(WriteResponse)),
                              l.pick(l.eq(phase, WriteResponse), phase, num(Available)));
  l.b.update(phase, l.pick(choosing_read, reading, writing));
  if (!direct) {
    l.b.update(jfull, l.b.select(take, one, l.b.select(leave, zero, jfull)));
    l.b.update(jwrite, l.b.select(take, l.inv(read), jwrite));
    window->advance(leave, shown, jfull, inside);
  }
  if (blocks)
    count_block_word(take, l.either(l.inv(read), rq.word.at("last")));
  l.b.update(mid, l.b.select(r_taken, l.inv(rq.word.at("last")), mid));
  if (conn)
    l.b.update(*conn, l.b.select(r_taken, rq.word.at("index"), *conn));
  const auto stage_of = [&](const std::string &name, ValueId value, ValueId enable) {
    return direct ? value : l.latch(name, value, enable);
  };
  const auto both_ways = [&](const std::string &name, const std::string &field,
                             std::optional<ValueId> b_value) {
    const auto value = rq.word.at(field);
    return stage_of(name, l.b.select(read, value, b_value.value_or(value)), take);
  };
  const auto b_field = [&](const std::string &field) -> std::optional<ValueId> {
    return bq.word.contains(field) ? std::optional{bq.word.at(field)} : std::nullopt;
  };
  const auto code = both_ways("r.response", "result", bq.word.at("result"));
  const auto address = stage_of(
      "r.response_address",
      l.global(l.b.select(read, rq.word.at("address"),
                          bq.word.contains("address") ? bq.word.at("address") : aw.at("address"))),
      take);
  const auto first = stage_of("r.response_first", l.both(read, rq.word.at("first")), take);
  if (!direct)
    l.b.update(jlast, l.b.select(take, l.either(l.inv(read), rq.word.at("last")), jlast));
  if (!direct && p.error_codes)
    l.b.update(local_error_on,
               l.b.select(take, l.both(read, rq.word.at("local_error")), local_error_on));
  const auto data = stage_of("r.data", rq.word.at("data"), r_taken);
  ValueId exclusive = zero;
  if (rq.word.contains("exclusive"))
    exclusive = both_ways("r.response_exclusive", "exclusive", b_field("response_exclusive"));
  std::optional<ValueId> user;
  if (rq.word.contains("user"))
    user = both_ways("r.response_user", "user", b_field("user"));
  Fields front_fields;
  if (cur_index) {
    l.b.update(*cur_index, l.b.select(r_taken, rq.word.at("index"), *cur_index));
    rout = *cur_index;
    front_fields = select_fields(rout);
  } else if (several) {
    rout = rq.word.at("index");
    front_fields = select_fields(rout);
  } else {
    front_fields = select_fields(qhead);
  }
  front = front_fields;
  Fields wfields = aw;
  if (several) {
    wfields.clear();
    for (const auto &name : {"size", "source", "id", "tag", "level"})
      if (bq.word.contains(name))
        wfields[name] = stage_of(std::string("w.rsp.") + name, bq.word.at(name), b_taken);
  }
  auto face_code = code, face_data = data, face_first = first, face_address = address;
  local_error_face(front, face_code, face_data, face_first, face_address);
  response_face("rrsp", front, l.both(offered, l.inv(jwrite)), face_address, face_code, face_first,
                jlast, face_data, exclusive, user);
  response_face("wrsp", wfields, l.both(offered, jwrite), address, code, one, one,
                l.b.constant(p.data_bits, 0), exclusive, user);
  l.b.output("nl_hint_cut", l.both(offered, cut), "hint", "cut");
  if (p.response_run_bits)
    l.b.output("nl_hint_wrap_end",
               burst_run_end(l, front, face_address, p.data_bits, p.response_run_bits), "hint",
               "wrap_end");
  rpop = l.both(leave, l.inv(jwrite));
  pop = l.bind("r.pop", l.both(rpop, jlast));
  if (report_only)
    pop = l.either(pop, l.both(complete, silent));
  bdone = l.both(leave, jwrite);
  if (!several) {
    bdone = l.either(bdone, bsilent_done);
    l.b.update(wb, l.b.select(head, one, l.b.select(bdone, zero, wb)));
  }
}

// Interleave mode: the read queue, the entries and their beat counts.
void EngineLowering::read_update_joined() {
  align_valid();
  auto advance_pointer = [&](ValueId pointer) {
    return l.b.select(l.eq(pointer, depth - 1),
                      l.b.constant(l.b.ir.values.at(pointer.index).width, 0), l.inc(pointer));
  };
  const auto taken_into = [&](unsigned k) {
    return l.both(take_r, several ? l.eq(ralloc, k) : l.eq(qtail, k));
  };
  for (unsigned k = 0; k < depth; ++k) {
    const auto write = taken_into(k);
    for (const auto &name : descriptor)
      l.b.update(rs[k].at(name), l.b.select(write, r.at(name), rs[k].at(name)));
    l.b.update(rejects[k], l.b.select(write, rreject, rejects[k]));
    l.b.update(misses[k], l.b.select(write, l.either(rmiss, r.at("error")), misses[k]));
  }
  if (several) {
    // The read taken now waits for its AR in the entry it took, younger than every other.
    l.b.update(qissue, l.b.select(take_r, ralloc, qissue));
    for (unsigned i = 0; i < depth; ++i)
      for (unsigned j = i + 1; j < depth; ++j)
        l.b.update(age[i][j],
                   l.b.select(taken_into(j), one, l.b.select(taken_into(i), zero, age[i][j])));
  } else {
    l.b.update(qtail, l.b.select(take_r, advance_pointer(qtail), qtail));
    l.b.update(qissue, l.b.select(issued, advance_pointer(qissue), qissue));
  }
  const auto bump = [&](ValueId reg, ValueId up, ValueId down) {
    l.b.update(reg, l.b.select(l.both(up, l.inv(down)), l.inc(reg),
                               l.b.select(l.both(down, l.inv(up)), l.dec(reg), reg)));
  };
  bump(count, take_r, pop);
  bump(pending, take_r, issued);
  if (several) {
    // An entry frees as its last word leaves the joined stage, or as a silent burst completes.
    const auto popped = l.both(rpop, jlast);
    const auto silent_done = report_only ? l.both(complete, silent) : zero;
    for (unsigned k = 0; k < depth; ++k) {
      const auto write = taken_into(k);
      const auto here = l.eq(rsel, k);
      const auto freed = l.either(l.both(popped, l.eq(rout, k)), l.both(silent_done, here));
      l.b.update(rvalid[k], l.b.select(write, one, l.b.select(freed, zero, rvalid[k])));
      l.b.update(
          rissued[k],
          l.b.select(write, zero, l.b.select(l.both(issued, l.eq(qissue, k)), one, rissued[k])));
      l.b.update(rdone[k],
                 l.b.select(write, zero, l.b.select(l.both(complete, here), one, rdone[k])));
      l.b.update(rbeat[k], l.b.select(write, l.b.constant(p.len_bits, 0),
                                      l.b.select(l.both(rpush, here),
                                                 l.b.select(rlast, l.b.constant(p.len_bits, 0),
                                                            l.inc(rbeat[k])),
                                                 rbeat[k])));
    }
    l.b.update(rcur, l.b.select(rpush, rsel, rcur));
  } else {
    l.b.update(qhead, l.b.select(pop, advance_pointer(qhead), qhead));
    l.b.update(qrecv, l.b.select(complete, advance_pointer(qrecv), qrecv));
    bump(received, complete, pop);
    l.b.update(rk,
               l.b.select(rpush, l.b.select(rlast, l.b.constant(p.len_bits, 0), l.inc(rk)), rk));
    l.b.update(ra, l.b.select(rpush, advance(l, current, recv, sizes), ra));
  }
}

void EngineLowering::build() {
  write_accept();
  write_reject();
  write_data();
  write_respond();
  read_accept();
  read_issue();
  if (ilv) {
    read_receive_joined();
    join_responses();
    read_update_joined();
  } else {
    read_receive();
    read_update();
  }
  count_pool();
}

void EngineLowering::count_pool() {
  const auto pool_in =
      p.admitted
          ? l.plus(l.fit(l.b.input("nl_hint_write_admit", 1, "hint", "write_admit"), pool_bits),
                   l.fit(l.b.input("nl_hint_read_admit", 1, "hint", "read_admit"), pool_bits))
          : l.plus(l.fit(admit, pool_bits), l.fit(take_r, pool_bits));
  auto popped = pop, answered = bdone;
  if (several) {
    // Behind a target merge a read's entry returns after the merge's last word of it left (the
    // hint input read_tail), as a wider target's last response word leaves in narrower words.
    l.b.update(read_release,
               p.merged ? l.b.input("nl_hint_read_tail", 1, "hint", "read_tail") : pop);
    l.b.update(write_release, bdone);
    popped = read_release;
    answered = write_release;
  } else if (p.merged && segment) {
    // One burst behind a merge: the entry returns as the merge's last word of the read leaves (a
    // WRAP's saved start word answers after the last target word left the stage).
    popped = l.b.input("nl_hint_read_tail", 1, "hint", "read_tail");
    if (report_only)
      popped = l.either(popped, l.both(complete, silent));
  }
  auto pool_out = l.plus(l.fit(answered, pool_bits), l.fit(popped, pool_bits));
  if (several && report_only)
    pool_out = l.plus(pool_out, l.fit(wsilent, pool_bits));
  l.b.update(pool, l.minus(l.plus(pool, pool_in), pool_out));
}

Result<NodeHardware> EngineLowering::finish() { return l.finish("nocloom_axi4_outlet_" + p.name); }

Result<NodeHardware> lower_engine(const Axi4OutletProfile &p, unsigned address_bits,
                                  unsigned sizes) {
  EngineLowering engine(p, address_bits, sizes);
  engine.build();
  return engine.finish();
}
} // namespace

// Flows: 1..32, mappings of them, and their number in the ID bits.
static bool valid_flows(const Axi4OutletProfile &p) {
  return p.flows && p.flows <= 32 && (p.flows == 1 || !p.mappings.empty()) &&
         axi4_outlet_id_bits(p) >= static_cast<unsigned>(std::bit_width(p.flows - 1)) &&
         std::ranges::none_of(p.mappings,
                              [&](const AddressMapping &m) { return m.flow >= p.flows; });
}
Result<NodeHardware> lower_axi4_outlet(const Axi4OutletProfile &p) {
  if (!valid_name(p.name) ||
      (p.data_bits != 32 && p.data_bits != 64 && p.data_bits != 128 && p.data_bits != 256 &&
       p.data_bits != 512 && p.data_bits != 1024) ||
      p.id_bits > 16 || p.len_bits < 4 || p.len_bits > 8 || p.source_bits > max_source_bits ||
      p.address_bits < 2 || p.address_bits > 48 ||
      (p.transport_address_bits &&
       (p.transport_address_bits < std::max(32U, p.address_bits) || p.transport_address_bits > 48)))
    return Result<NodeHardware>::failure(
        {{"NCL_AXI4_OUTLET_PROFILE",
          "axi4_outlet",
          "Expected a valid name, 32/64/128/256/512/1024 data bits, 0..16 ID bits, "
          "4..8 length bits and 0..9 source bits",
          {},
          {}}});
  if (!valid_flows(p))
    return Result<NodeHardware>::failure(
        {{"NCL_AXI4_OUTLET_PROFILE",
          "axi4_outlet.flows",
          "Expected 1..32 flows, each a mapping's, whose number fits the ID port",
          {},
          {}}});
  const auto address_bits =
      p.transport_address_bits ? p.transport_address_bits : std::max(32U, p.address_bits);
  if ((p.mappings.empty() && p.address_bits < address_bits) ||
      !mapping_decode::valid(p.mappings, address_bits, p.address_bits, p.data_bits / 8) ||
      !mapping_decode::holds(p.arrival, p.mappings, address_bits))
    return Result<NodeHardware>::failure(error(
        "NCL_AXI4_OUTLET_WINDOW", "mappings",
        "Outlet mappings must be disjoint whole 4 KiB pages or sub-pages of at least one word in "
        "base order inside the transport address space whose local addresses all fit a narrowed "
        "port"));
  const unsigned timeout = p.timeout_cycles;
  if ((timeout && !valid_timeout_cycles(timeout)) || (!timeout && p.timeout_completion))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_OUTLET_PROFILE", "timeout_cycles",
              "Expected a power of two from 16 to 1048576 timeout cycles"));
  const auto mode = p.exclusive_target.mode;
  const bool sources_known =
      !p.id_sources.empty() && p.source_bits && std::ranges::none_of(p.id_sources, [&](unsigned s) {
        return 2 * s + 1 >= (1U << p.source_bits);
      });
  if ((mode != ExclusiveAccess::None && (!p.exclusive || p.read_only || p.write_only)) ||
      (mode == ExclusiveAccess::Network &&
       (p.exclusive_target.monitors == 0 || p.exclusive_target.monitors > 16 ||
        !std::has_single_bit(p.exclusive_target.granule) || p.exclusive_target.granule > 128)) ||
      (mode == ExclusiveAccess::Target && !sources_known))
    return Result<NodeHardware>::failure(error(
        "NCL_AXI4_OUTLET_PROFILE", "exclusive_target",
        "Exclusive support needs the Weft exclusive fields and a read-write target; a network "
        "monitor needs 1..16 reservations and a power-of-two granule of 1..128 bytes; target "
        "mode needs the indices of the reaching sources"));
  if (p.max_outstanding == 0 ||
      (p.max_outstanding != 1 && (p.max_outstanding > 16 || !sources_known)))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_OUTLET_PROFILE", "max_outstanding",
              "Multiple outstanding bursts need 2..16 in flight over both directions and the "
              "indices of the reaching sources within the source field"));
  // AxSIZE values the outlet steps by: 0..3 for 32/64-bit outlets, then up to
  // the bus width (AxSIZE 7, 128 bytes, at 1024 bits).
  const unsigned sizes = p.data_bits <= 64 ? 4 : std::bit_width(p.data_bits / 8);
  return lower_engine(p, address_bits, sizes);
}

unsigned axi4_outlet_id_bits(const Axi4OutletProfile &p) {
  if (p.port_id_bits)
    return *p.port_id_bits;
  const auto sources = static_cast<unsigned>(p.id_sources.size());
  const bool extended = p.max_outstanding > 1 || p.exclusive_target.mode == ExclusiveAccess::Target;
  return p.id_bits +
         (extended && sources > 1 ? static_cast<unsigned>(std::bit_width(sources - 1)) : 0U) +
         static_cast<unsigned>(std::bit_width(p.flows - 1));
}
std::pair<unsigned, unsigned> axi4_outlet_outstanding_bits(const Axi4OutletProfile &p) {
  const bool owned =
      p.exclusive_target.mode == ExclusiveAccess::Target && !p.exclusive_target.id_flags.empty();
  const unsigned address = p.arrival.keep             ? p.arrival.keep
                           : p.transport_address_bits ? p.transport_address_bits
                                                      : std::max(32U, p.address_bits);
  // Write: the main entry's descriptor and the W entry's data and strobes; one burst keeps its B
  // result, several the two staged responses (code, size, source[, id], address) and per slot the
  // source, [id,] address, size and local code.
  const unsigned descriptor =
      address * (p.start_address ? 2 : 1) + p.len_bits + 12 + p.id_bits + p.source_bits +
      (p.user.empty() && !(owned && p.max_outstanding > 1) ? 0 : p.user_bits) +
      (p.max_outstanding > 1 ? 0 : axi4_outlet_id_bits(p));
  const unsigned staging = 2 + 3 + p.source_bits + p.id_bits + address;
  const unsigned write_slot =
      p.source_bits + p.id_bits + address + 3 + 2 +
      (p.exclusive_target.mode == ExclusiveAccess::Network ? p.len_bits + 2 : 0U) +
      (owned ? p.user_bits : 0U);
  // The response user field of a target with BUSER and RUSER: staged with B and each R beat,
  // and the head of a read packet.
  const unsigned user = p.response_user_port_bits ? p.response_user_bits : 0U;
  const bool joined = p.interleave && p.max_outstanding > 1;
  // An AXI3 target with several bursts keeps the W entry beat's WID; behind the response merger
  // the parked B keeps its ID, result and user field.
  const unsigned parked = p.merger_hints || joined ? axi4_outlet_id_bits(p) + 2 + user : 0U;
  // Interleave mode with several bursts: the B pipe holds one staged response, and the joined
  // stage keeps a B's size, source, ID and level (its result, address and user ride with the
  // read side's).
  const unsigned b_staged = p.response_direct ? staging
                            : joined          ? staging + 3 + p.source_bits + p.id_bits
                                              : 2 * staging;
  const unsigned write = p.max_outstanding < 2
                             ? descriptor + p.data_bits + p.data_bits / 8 + 2 + user
                             : descriptor + p.data_bits + p.data_bits / 8 + b_staged +
                                   p.max_outstanding * write_slot + (p.interleave ? 1 : 2) * user +
                                   (p.axi3 ? axi4_outlet_id_bits(p) : 0U) + parked +
                                   (owned ? p.source_bits + p.id_bits + 1 : 0U);
  // Read: per queue entry the descriptor, then the beat address and the staged beats (result,
  // data, address): two, or one behind a merge. In interleave mode with several bursts each entry
  // keeps its first beat's user and no beat address (the R pipe and the joined stage hold two).
  // Each descriptor owns a held answer. A pending injection names its descriptor.
  const unsigned index = static_cast<unsigned>(std::bit_width(p.max_outstanding - 1));
  const unsigned several = p.max_outstanding > 1 ? std::max(1U, index) : 0U;
  const unsigned align =
      !p.align_wrap
          ? 0U
          : p.max_outstanding *
                    (p.data_bits + 2 + address + several + user +
                     (joined && p.exclusive_target.mode != ExclusiveAccess::None ? 1 : 0)) +
                1 + std::max(1U, index) +
                (joined || p.response_direct ? 0 : p.data_bits / 8 + 3 + several);
  const unsigned beats = (p.response_direct || (p.merged && !p.interleave) ? 1 : 2) *
                         (2 + p.data_bits + address + user);
  const unsigned read = (joined ? p.max_outstanding * (descriptor + user) + beats
                                : p.max_outstanding * descriptor + address + beats + user) +
                        align;
  return {write, read};
}
} // namespace nocloom::detail
