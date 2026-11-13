#include "axi4_upsize.hpp"
#include "beat_stage.hpp"
#include "burst_faces.hpp"
#include "decode_packet.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "message.hpp"
#include "strobe_lanes.hpp"
#include <bit>
#include <numeric>

namespace nocloom::detail {
namespace {
using Fields = std::map<std::string, ValueId>;
using logic::Logic;

// One upsize lowering: the builder, the profile, the shared derived constants
// and the burst-plan helpers, plus the handles the write phases hand to each
// other.
struct UpsizeLowering {
  UpsizeLowering(logic::Logic &logic, const Axi4UpsizeProfile &profile)
      : l(logic), p(profile), bytes(p.data_bits / 8), t(std::bit_width(bytes) - 1),
        ab(p.address_bits), zero(l.bind("c.zero", l.bit(0))), one(l.bind("c.one", l.bit(1))),
        lb(p.len_bits), count_bits(burst_count_bits(p.data_bits)), burst{p.id_bits, lb, true, ab},
        io{l, burst, p.source_bits}, word(l.num(ab, bytes)),
        slot_bits(std::max(1U, static_cast<unsigned>(std::bit_width(p.max_outstanding - 1)))),
        occupancy_bits(std::bit_width(p.max_outstanding)) {
    burst.exclusive = p.exclusive;
    burst.tag_bits = p.tag_bits;
    burst.user_bits = p.user_bits;
    burst.response_user_bits = p.response_user_bits;
    burst.data_user_bits_per_byte = p.data_user_bits_per_byte;
    l.block = p.arrival;
    l.block_bits = ab;
  }

  logic::Logic &l;
  const Axi4UpsizeProfile &p;
  const unsigned bytes, t, ab;
  const ValueId zero, one;
  const unsigned lb, count_bits;
  BurstProfile burst;
  BurstFaces io;
  const ValueId word;
  const unsigned slot_bits, occupancy_bits;

  // 2^size bytes over the address width (size never exceeds t).
  ValueId beat_bytes(ValueId size) { return l.pow2(size, ab, t); }
  ValueId mask_of(ValueId span) { return l.minus(span, l.num(ab, 1)); } // span - 1
  ValueId floor_by(ValueId address, ValueId span) { return l.floor(address, span); }
  ValueId floor_word(ValueId address) { return floor_by(address, word); }
  ValueId word_offset(ValueId address) { return l.both(address, l.num(ab, bytes - 1)); }
  // A decode packet's word leaves marked while beats remain after its first (left).
  void mark_decode(Fields &up, ValueId decoded, ValueId left) {
    if (!p.decode_packets)
      return;
    const auto more = l.inv(l.eq(left, 0));
    up["result"] =
        l.pick(decoded, decode_packet::mark_result(l, p.decode_flag, more), up.at("result"));
    if (p.decode_flag)
      up["error"] = l.both(decoded, more);
  }
  // A WRAP read whose start is not on a word and whose whole words need the word store. A container
  // of less than a word stays inside it: its beats keep their order in one word.
  ValueId unaligned(const Fields &d, ValueId mode) {
    return l.both(l.eq(mode, 2), l.inv(l.eq(word_offset(d.at("start_address")), 0)));
  }
  // floor_by(address, beat_bytes(size)): the 2^size - 1 masks are constants picked by size.
  ValueId floor_size(ValueId address, ValueId size) {
    const auto w = l.width(address);
    const auto low = l.per_size(size, t, [&](unsigned n) { return l.num(w, (1ULL << n) - 1); });
    return l.both(address, l.b.unary(Op::BitNot, low, w));
  }
  ValueId expand(ValueId byte_mask) { return strobe_lanes(l.b, byte_mask, bytes); }
  ValueId read_word_data(ValueId data, const Fields &d, ValueId address, ValueId left) {
    // Keep every requested byte of an INCR word, including its later source beats.
    // In beat units, the last requested byte is in the current word's beat
    // offset plus left. One carry bit keeps a suffix extending past this word.
    const auto upper = l.per_size(d.at("size"), t, [&](unsigned n) {
      if (n == t)
        return l.b.unary(Op::BitNot, l.num(bytes, 0), bytes);
      const unsigned reach = std::max(lb + 1, t - n + 1);
      const auto last = l.plus(l.zext(l.b.slice(address, n, t - n), reach), l.zext(left, reach));
      auto lanes = one;
      for (unsigned byte = 1; byte < bytes; ++byte)
        lanes = l.b.concat(l.inv(l.below(last, l.num(reach, byte >> n))), lanes);
      return lanes;
    });
    const auto start = d.at("start_address");
    const auto index = l.b.slice(address, t, 12 - t), lo = l.b.slice(start, t, 12 - t);
    const auto after = l.below(lo, index), first = l.same(index, lo);
    const auto low = l.b.slice(start, 0, t);
    ValueId mask = l.num(1, 0);
    for (unsigned byte = 0; byte < bytes; ++byte) {
      const auto lane = l.num(t, byte);
      const auto from = l.either(after, l.both(first, l.inv(l.below(lane, low))));
      const auto keep = l.both(from, l.b.slice(upper, byte, 1));
      mask = byte ? l.b.concat(keep, mask) : keep;
    }
    return l.pick(l.eq(d.at("burst"), 1), l.both(data, expand(mask)), data);
  }
  ValueId count_of(ValueId size) { return l.pow2(size, count_bits, t); }
  // Burst plan from a descriptor: mode 0 pass, 1 merge into one INCR (or a
  // one-word WRAP), 2 target WRAP; the target descriptor and the WRAP
  // container.
  struct Plan {
    ValueId mode, length, size, start, kind, container, base;
  };
  // The burst's bytes span N * B from the start floored to B (B = 2^size, N = AxLEN + 1); its
  // mask N * B - 1 is (N - 1) * B + B - 1, the length shifted with B - 1 below it. The word count
  // and the covering size use only the offset in the start word: a burst spans at most 2^lb * T
  // bytes, so they need lb + t + 2 bits, not the whole address.
  Plan plan_of(const Fields &f, bool write) {
    Plan r;
    const auto size = f.at("size");
    const auto a = f.at("start_address");
    const auto length = l.zext(f.at("length"), ab);
    const auto beats = l.plus(length, l.num(ab, 1));
    ValueId span = beats, mask = length; // N * B and N * B - 1 (B = 1 byte at size 0)
    for (unsigned n = 1; n <= t; ++n) {
      span = l.pick(l.eq(size, n), l.b.concat(l.b.slice(beats, 0, ab - n), l.num(n, 0)), span);
      mask = l.pick(l.eq(size, n),
                    l.b.concat(l.b.slice(length, 0, ab - n), l.num(n, (1ULL << n) - 1)), mask);
    }
    const auto narrow = l.below(size, l.num(3, t));
    const auto fixed = l.eq(f.at("burst"), 0), wrap = l.eq(f.at("burst"), 2);
    // Every INCR goes through the merge (a full-width single word still
    // takes its covering size); WRAP only when narrower than the target.
    const auto fits = l.inv(l.below(l.num(3, t), size)); // wider beats never arrive
    const auto merge = l.both(l.both(l.inv(fixed), fits), l.either(narrow, l.inv(wrap)));
    const auto big_wrap = l.both(wrap, l.inv(l.below(mask, word)));
    // INCR: target words from the start word to the burst end, and the size covering the bytes of
    // a burst inside one word.
    const auto start = floor_size(a, size);
    // The profile keeps lb + t + 2 at most 17 bits, below the 32-bit minimum address.
    const unsigned reach = lb + t + 2;
    const auto offset = l.b.slice(word_offset(start), 0, reach);
    const auto words = l.plus(l.plus(offset, l.b.slice(span, 0, reach)), l.num(reach, bytes - 1));
    const auto word_count = l.b.slice(words, t, reach - t);
    const auto several = l.below(l.num(reach - t, 1), word_count);
    // Inside one word only the bits below t can differ between the start and the last byte.
    const auto last = l.plus(l.b.slice(offset, 0, t), l.b.slice(mask, 0, t));
    ValueId cover = l.num(3, t);
    for (unsigned q = t; q-- > 0;)
      cover = l.pick(l.same(l.b.slice(a, q, t - q), l.b.slice(last, q, t - q)), l.num(3, q), cover);
    const auto incr_size = l.pick(several, l.num(3, t), cover);
    // WRAP: the container base, one word or C / T words. The base has no bit of the mask, so its
    // last byte is base | mask and a container inside one word is covered by the size above the
    // mask's highest bit.
    const auto base = l.both(a, l.b.unary(Op::BitNot, mask, ab));
    ValueId wrap_cover = l.num(3, t);
    for (unsigned q = t; q-- > 0;)
      wrap_cover = l.pick(l.eq(l.b.slice(mask, q, ab - q), 0), l.num(3, q), wrap_cover);
    ValueId shifted = floor_word(a);
    if (write) {
      // The word after the start word, inside the container: only the mask's bits matter.
      const auto after =
          l.zext(l.plus(l.b.slice(floor_word(a), 0, reach), l.num(reach, bytes)), ab);
      shifted = l.pick(l.eq(word_offset(a), 0), floor_word(a), l.either(base, l.both(mask, after)));
    }
    r.mode = l.pick(merge, l.pick(big_wrap, l.num(2, 2), l.num(2, 1)), l.num(2, 0));
    const auto incr_len = l.minus(l.b.slice(word_count, 0, lb), l.num(lb, 1));
    const auto big_len = l.minus(l.b.slice(span, t, lb), l.num(lb, 1));
    r.length = l.pick(wrap, l.pick(big_wrap, big_len, l.num(lb, 0)), incr_len);
    // A WRAP container inside one word is read or written at its own size.
    r.size = l.pick(wrap, l.pick(big_wrap, l.num(3, t), wrap_cover), incr_size);
    r.start = l.pick(wrap, l.pick(big_wrap, shifted, base), a);
    r.kind = l.pick(big_wrap, l.num(2, 2), l.num(2, 1));
    r.container = span;
    r.base = base;
    return r;
  }
  // Next source beat address: INCR steps, WRAP turns inside its container.
  ValueId next_beat(ValueId address, ValueId size, ValueId kind, ValueId span, ValueId base) {
    const auto stepped = l.plus(floor_size(address, size), beat_bytes(size));
    const auto turned = l.either(base, l.both(stepped, mask_of(span)));
    return l.pick(l.eq(kind, 2), turned, stepped);
  }
  // Two read descriptors name the same {source, AxID} (the outlet's order key).
  ValueId same_key(const Fields &a, const Fields &b) {
    auto same = l.bit(1);
    for (const auto *name : {"source", "id"})
      if (a.contains(name) && b.contains(name))
        same = l.both(same, l.same(a.at(name), b.at(name)));
    return same;
  }
  ValueId advance(ValueId pointer) {
    return l.pick(l.eq(pointer, p.max_outstanding - 1), l.num(slot_bits, 0),
                  l.plus(pointer, l.num(slot_bits, 1)));
  }

  struct WriteContext {
    ValueId valid, ahead, merged, address;
    Fields key;
  };
  // Write-side handles handed from write_words to write_response.
  Fields wi, wb;
  ValueId down_ready{};
  std::vector<WriteContext> contexts;
  ValueId vacant{}, head{}, pass{}, wvalid{};
  Plan wplan{};
  Fields wo;

  // Read-side handles handed from read_request to read_response.
  Fields rr, descriptor, rd;
  ValueId up_r_ready{}, down_r_ready{}, busy{}, pending{};
  ValueId qcount{}, qhead{}, accept{}, rload{};
  Plan rplan{};
  ValueId rmode{}, rsize{}, rkind{}, rspan{}, rbase{}, rstart_word{};
  ValueId sa{}, remaining{}, rfull{}, rword{}, rfirst{};
  ValueId saved{}, saved_data{}, saved_result{}, saved_error{}, next_word{};
  ValueId rpass{}, rbypass{}, rq_fire{};
  // Several reads: per slot taken (used: inside the ring from the head, count long) and answered
  // out of order (done), the gather's slot, its first word taken (started) and the arriving word
  // of the gather's key (match).
  std::vector<ValueId> rused, rdone;
  ValueId rslot{}, rstarted{}, rmatch{}, rroom{};
  ValueId pick_slot(const std::vector<ValueId> &flags, ValueId index) {
    auto value = flags.front();
    for (unsigned k = 1; k < flags.size(); ++k)
      value = l.pick(l.eq(index, k), flags[k], value);
    return value;
  }

  void write_words();
  void write_response();
  void read_request();
  void read_response();
  // One lowering of the read side for several reads behind an outlet with interleaved read data.
  void reads_by_key();
  Fields replay_bank(ValueId capture, ValueId release, ValueId allocated);
};

void UpsizeLowering::write_words() {
  // ---------------- write: merge beats into words; the start word may wait.
  // An emitted word passes to the outlet in the cycle it is completed and is never stored here: a
  // beat that emits a word is taken in the cycle the outlet takes the word, any other beat at once
  // (as a transport word gather holds only its open part, width_adapter.hpp).
  wi = l.arrive(io.inputs("up_wreq", p.data_bits, true));
  wb = l.arrive(io.inputs("down_wrsp", p.data_bits, false));
  down_ready = io.in("down_wreq", "ready", 1);
  vacant = zero;
  ValueId available = zero;
  if (p.max_outstanding > 1) {
    vacant = l.num(slot_bits, 0);
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      const auto name = "w.context" + std::to_string(k) + ".";
      WriteContext entry{l.state(name + "valid", 1),
                         l.state(name + "ahead", slot_bits),
                         l.state(name + "merged", 1),
                         l.address_state(name + "address", ab),
                         {}};
      for (const auto &key : {"source", "id", "tag"})
        if (wi.contains(key))
          entry.key[key] = l.state(name + key, l.b.ir.values.at(wi.at(key).index).width);
      contexts.push_back(entry);
    }
    for (unsigned k = p.max_outstanding; k-- > 0;) {
      vacant = l.pick(l.inv(contexts[k].valid), l.num(slot_bits, k), vacant);
      available = l.either(available, l.inv(contexts[k].valid));
    }
  }
  auto open = io.active(one);
  ValueId receiving_write{};
  if (p.max_outstanding > 1) {
    receiving_write = l.state("w.receiving_packet", 1);
    open = l.both(open, l.either(receiving_write, available));
  }
  // The burst terms latched at its head; a later beat reads them, the head its own plan.
  wplan = plan_of(wi, true);
  const auto shift_now =
      l.both(l.eq(wplan.mode, 2), l.inv(l.eq(word_offset(wi.at("start_address")), 0)));
  std::vector<std::pair<ValueId, ValueId>> at_head;
  auto held = [&](const std::string &name, ValueId value) {
    const auto state = l.state_like(name, value);
    at_head.emplace_back(state, value);
    return state;
  };
  auto mode = held("w.mode", wplan.mode);
  auto length = held("w.length", wplan.length);
  auto size = held("w.size", wplan.size);
  auto start = held("w.start", l.global(wplan.start));
  auto kind = held("w.kind", wplan.kind);
  auto start_word = held("w.start_word", l.global(floor_word(wi.at("start_address"))));
  auto shift = held("w.shift", shift_now);
  auto cur_data = l.state("w.word_data", p.data_bits), cur_be = l.state("w.word_be", bytes);
  auto hold_data = l.state("w.hold_data", p.data_bits), hold_be = l.state("w.hold_be", bytes);
  auto sent = l.state("w.sent", 1), acc_error = l.state("w.error", 1);
  // Mode, shift and start word of the beat being loaded (the head itself or a
  // later beat of the latched burst).
  const auto beat_mode =
      p.joined && p.read_only ? l.num(2, 0) : l.pick(wi.at("first"), wplan.mode, mode);
  const auto beat_shift = l.pick(wi.at("first"), shift_now, shift);
  const auto beat_start_word =
      l.pick(wi.at("first"), floor_word(wi.at("start_address")), start_word);
  pass = l.eq(beat_mode, 0);
  const auto beat_word = floor_word(wi.at("address"));
  const auto data_mask = expand(wi.at("be"));
  auto merge_into = [&](ValueId data, ValueId be, bool fresh) {
    const auto kept = fresh ? l.num(p.data_bits, 0) : data;
    return std::pair{l.either(l.both(kept, l.b.unary(Op::BitNot, data_mask, p.data_bits)),
                              l.both(wi.at("data"), data_mask)),
                     l.either(fresh ? l.num(bytes, 0) : be, wi.at("be"))};
  };
  // A beat ends its word when the next beat leaves it, or at the burst end; a
  // WRAP container inside one word ends only at the last beat. A request run of several beats
  // (width_adapter.hpp) merges like its beats at once: the next beat leaves the word when the run
  // ends at the word's last byte (ends, message.hpp point 4).
  const auto small_wrap = l.both(l.eq(beat_mode, 1), l.eq(wi.at("burst"), 2));
  const auto word_done = l.either(wi.at("last"), l.both(l.inv(small_wrap), wi.at("ends")));
  // The shifted start word collects the first and the final beats.
  const auto in_start = l.both(beat_shift, l.same(beat_word, beat_start_word));
  auto [merged_data, merged_be] = merge_into(cur_data, cur_be, false);
  auto [held_data, held_be] = merge_into(hold_data, hold_be, false);
  const auto emit_hold = l.both(in_start, wi.at("last"));
  const auto to_hold = l.both(in_start, l.inv(wi.at("last")));
  const auto emit_word = l.both(l.inv(in_start), word_done);
  const auto emits = l.either(pass, l.either(emit_hold, emit_word));
  // A word leaves with the beat that ends it: the valid follows the up face only.
  wvalid = l.both(open, l.both(wi.at("valid"), emits));
  const auto ready = l.both(open, l.either(l.inv(emits), down_ready));
  auto load = l.bind("w.load", l.both(wi.at("valid"), ready));
  head = l.both(load, wi.at("first"));
  for (const auto &[state, value] : at_head)
    l.b.update(state, l.b.select(head, value, state));
  if (p.max_outstanding > 1)
    l.b.update(receiving_write, l.pick(load, l.inv(wi.at("last")), receiving_write));
  const auto [fresh_data, fresh_be] = merge_into(cur_data, cur_be, true); // a first beat
  const auto word_data = l.pick(wi.at("first"), fresh_data, merged_data);
  const auto word_be = l.pick(wi.at("first"), fresh_be, merged_be);
  const auto hold_now_data = l.pick(wi.at("first"), fresh_data, held_data);
  const auto hold_now_be = l.pick(wi.at("first"), fresh_be, held_be);
  l.b.update(sent, l.pick(l.both(load, emits), one, l.pick(head, zero, sent)));
  // Partial words and the held start word.
  const auto keep_word =
      l.both(load, l.both(l.inv(pass), l.both(l.inv(in_start), l.inv(word_done))));
  l.b.update(cur_data, l.pick(keep_word, word_data,
                              l.pick(l.both(load, emit_word), l.num(p.data_bits, 0), cur_data)));
  l.b.update(cur_be,
             l.pick(keep_word, word_be, l.pick(l.both(load, emit_word), l.num(bytes, 0), cur_be)));
  l.b.update(hold_data, l.pick(l.both(load, to_hold), hold_now_data,
                               l.pick(l.both(load, emit_hold), l.num(p.data_bits, 0), hold_data)));
  l.b.update(hold_be, l.pick(l.both(load, to_hold), hold_now_be,
                             l.pick(l.both(load, emit_hold), l.num(bytes, 0), hold_be)));
  l.b.update(
      acc_error,
      l.pick(load,
             l.pick(emits, zero, l.either(l.pick(wi.at("first"), zero, acc_error), wi.at("error"))),
             acc_error));
  // The emitted word: pass keeps the source beat, a merged word carries the target descriptor of
  // its burst (the head's plan or the latched one).
  const auto beat_length = l.pick(wi.at("first"), wplan.length, length);
  const auto beat_size = l.pick(wi.at("first"), wplan.size, size);
  const auto beat_start = l.pick(wi.at("first"), wplan.start, start);
  const auto beat_kind = l.pick(wi.at("first"), wplan.kind, kind);
  wo = wi;
  wo.erase("valid");
  wo["data"] = l.pick(pass, wi.at("data"), l.pick(emit_hold, hold_now_data, word_data));
  wo["be"] = l.pick(pass, wi.at("be"), l.pick(emit_hold, hold_now_be, word_be));
  // A merged word sits at its word; a single word narrower than the target
  // at its covering size's container, as the read request.
  wo["address"] = l.pick(pass, wi.at("address"),
                         l.pick(l.eq(beat_size, t), beat_word, floor_size(beat_start, beat_size)));
  wo["first"] = l.pick(pass, wi.at("first"), l.inv(l.pick(wi.at("first"), zero, sent)));
  wo["error"] = l.either(l.pick(wi.at("first"), zero, acc_error), wi.at("error"));
  wo["length"] = l.pick(pass, wi.at("length"), beat_length);
  wo["size"] = l.pick(pass, wi.at("size"), beat_size);
  wo["start_address"] = l.pick(pass, wi.at("start_address"), beat_start);
  wo["burst"] = l.pick(pass, wi.at("burst"), l.pick(l.eq(beat_kind, 2), l.num(2, 2), l.num(2, 1)));
  wo["count"] = l.pick(pass, wi.at("count"), count_of(beat_size));
  wo = l.global(wo);
  io.out("up_wreq", "ready", ready);
  io.out("down_wreq", "valid", wvalid);
  io.fields_out("down_wreq", wo, p.data_bits, true);
}

void UpsizeLowering::write_response() {
  if (p.max_outstanding == 1) {
    // One B per burst; a merged burst answers in the source's terms (its first
    // beat container and size) so the response is not split on its way back.
    // The next burst may load before this B returns, so the burst's terms are
    // kept from the moment its last word leaves (the outlet takes no further
    // beat until the B is consumed).
    const auto source_address = l.latch("w.source_address", wi.at("address"), head);
    const auto word_merged = l.inv(pass);
    const auto word_address = l.global(l.pick(wi.at("first"), wi.at("address"), source_address));
    const auto leaves = l.both(l.both(wvalid, down_ready), wo.at("last"));
    const auto b_merged = l.latch("w.b_merged", word_merged, leaves);
    const auto b_address = l.latch("w.b_address", word_address, leaves);
    Fields wr = wb;
    // A write response has no payload, so one word carries it on every link (message.hpp point 4).
    wr["count"] = l.pick(b_merged, l.num(count_bits, 4), wb.at("count"));
    wr["address"] = l.pick(b_merged, b_address, wb.at("address"));
    io.out("up_wrsp", "valid", wb.at("valid"));
    io.out("down_wrsp", "ready", io.in("up_wrsp", "ready", 1));
    io.fields_out("up_wrsp", wr, p.data_bits, false);
  } else {
    auto matches = [&](const WriteContext &entry, const Fields &message) {
      auto match = one;
      for (const auto &[name, value] : entry.key)
        match = l.both(match, l.same(value, message.at(name)));
      return match;
    };
    auto found = zero;
    auto selected = l.num(slot_bits, 0);
    for (unsigned k = p.max_outstanding; k-- > 0;) {
      const auto match =
          l.both(contexts[k].valid, l.both(l.eq(contexts[k].ahead, 0), matches(contexts[k], wb)));
      selected = l.pick(match, l.num(slot_bits, k), selected);
      found = l.either(found, match);
    }
    auto choose = [&](auto member) {
      auto value = contexts.front().*member;
      for (unsigned k = 1; k < p.max_outstanding; ++k)
        value = l.pick(l.eq(selected, k), contexts[k].*member, value);
      return value;
    };
    const auto bvalid = io.active(l.both(wb.at("valid"), found));
    const auto bready = io.active(l.both(io.in("up_wrsp", "ready", 1), found));
    const auto complete = l.bind("w.complete", l.both(bvalid, bready));
    auto ahead = l.num(slot_bits, 0);
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      const auto older = l.both(contexts[k].valid, matches(contexts[k], wi));
      const auto retiring = l.both(complete, l.eq(selected, k));
      ahead = l.pick(l.both(older, l.inv(retiring)), l.plus(ahead, l.num(slot_bits, 1)), ahead);
    }
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      auto &entry = contexts[k];
      const auto reserve = l.both(head, l.eq(vacant, k));
      const auto retire = l.both(complete, l.eq(selected, k));
      const auto behind = l.both(complete, l.both(entry.valid, matches(entry, wb)));
      l.b.update(entry.valid, l.pick(reserve, one, l.pick(retire, zero, entry.valid)));
      l.b.update(entry.ahead,
                 l.pick(reserve, ahead,
                        l.pick(l.both(behind, l.inv(l.eq(entry.ahead, 0))),
                               l.minus(entry.ahead, l.num(slot_bits, 1)), entry.ahead)));
      l.b.update(entry.merged, l.pick(reserve, l.inv(l.eq(wplan.mode, 0)), entry.merged));
      l.b.update(entry.address, l.pick(reserve, wi.at("address"), entry.address));
      load_fields(l, entry.key, wi, reserve);
    }
    Fields wr = wb;
    const auto merged = choose(&WriteContext::merged);
    wr["count"] = l.pick(merged, l.num(count_bits, 4), wb.at("count"));
    wr["address"] = l.pick(merged, choose(&WriteContext::address), wb.at("address"));
    io.out("up_wrsp", "valid", bvalid);
    io.out("down_wrsp", "ready", bready);
    io.fields_out("up_wrsp", wr, p.data_bits, false);
  }
}

void UpsizeLowering::read_request() {
  // ---------------- read: one descriptor; each word fans out to its beats
  const auto ri = l.arrive(io.inputs("up_rreq", p.data_bits, true));
  rr = l.arrive(io.inputs("down_rrsp", p.data_bits, false));
  down_r_ready = io.in("down_rreq", "ready", 1);
  up_r_ready = io.in("up_rrsp", "ready", 1);
  busy = l.state("r.busy", 1);
  pending = p.joined ? zero : l.state("r.pending", 1);
  descriptor = ri;
  ValueId input_ready{};
  if (p.max_outstanding > 1) {
    qcount = l.state("r.queue_count", occupancy_bits);
    qhead = l.state("r.queue_head", slot_bits);
    const auto qtail = l.state("r.queue_tail", slot_bits);
    // A read enters only as it passes on to the outlet (the outlet takes it in that cycle): the
    // merge keeps the gather contexts of reads in flight, never a request queue.
    rroom = io.active(l.inv(l.eq(qcount, p.max_outstanding)));
    input_ready = l.both(rroom, down_r_ready);
    accept = l.bind("r.accept", l.both(ri.at("valid"), input_ready));
    std::vector<Fields> queue;
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      const auto at = "r.context" + std::to_string(k);
      queue.push_back(l.latch_fields(at, ri, l.both(accept, l.eq(qtail, k))));
      rdone.push_back(l.state(at + ".done", 1));
      // Slot k is in use when its distance from the head in ring order is below the count.
      const auto distance = l.pick(l.below(l.num(slot_bits, k), qhead),
                                   l.plus(l.minus(l.num(slot_bits, k), qhead),
                                          l.num(slot_bits, p.max_outstanding % (1U << slot_bits))),
                                   l.minus(l.num(slot_bits, k), qhead));
      rused.push_back(l.below(l.fit(distance, occupancy_bits), qcount));
    }
    auto select = [&](ValueId index) {
      Fields result = queue.front();
      for (auto &[name, value] : result)
        for (unsigned k = 1; k < p.max_outstanding; ++k)
          value = l.pick(l.eq(index, k), queue[k].at(name), value);
      return result;
    };
    // The first slot in ring order from the head whose condition holds.
    auto first_from = [&](const std::vector<ValueId> &conditions) {
      std::vector<ValueId> by_head;
      for (unsigned h = 0; h < p.max_outstanding; ++h) {
        ValueId value = l.num(slot_bits, h);
        for (unsigned j = p.max_outstanding; j-- > 0;) {
          const auto k = (h + j) % p.max_outstanding;
          value = l.pick(conditions[k], l.num(slot_bits, k), value);
        }
        by_head.push_back(value);
      }
      auto value = by_head.front();
      for (unsigned h = 1; h < p.max_outstanding; ++h)
        value = l.pick(l.eq(qhead, h), by_head[h], value);
      return value;
    };
    // The gather takes the oldest read not yet answered. A target answers reads of different IDs
    // in any order (the beats of one read together): a first word naming another {source, AxID}
    // makes the gather take the oldest open read of that key instead, one cycle later.
    std::vector<ValueId> open, keyed;
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      open.push_back(l.both(rused[k], l.inv(rdone[k])));
      keyed.push_back(l.both(open.back(), same_key(queue[k], rr)));
    }
    rslot = l.state("r.slot", slot_bits);
    rstarted = l.state("r.started", 1);
    // The gather's descriptor is its slot's (the slot is held until the read completes).
    rd = l.global(select(rslot));
    const auto any_open = std::accumulate(open.begin(), open.end(), zero,
                                          [&](ValueId a, ValueId c) { return l.either(a, c); });
    rmatch = l.either(rstarted, same_key(rd, rr));
    const auto reload = l.bind(
        "r.reload",
        io.active(l.both(l.both(busy, rr.at("valid")), l.both(l.inv(rstarted), l.inv(rmatch)))));
    const auto gather = l.pick(reload, first_from(keyed), first_from(open));
    descriptor = l.global(select(gather));
    l.b.update(qtail, l.pick(accept, advance(qtail), qtail));
    rload = l.bind("r.load", l.either(io.active(l.both(l.inv(busy), any_open)), reload));
    l.b.update(rslot, l.pick(rload, gather, rslot));
  } else if (p.joined) {
    // Joined: a read enters only as it passes on, as with several.
    rroom = io.active(l.inv(busy));
    input_ready = l.both(rroom, down_r_ready);
    rload = l.bind("r.load", l.both(ri.at("valid"), input_ready));
  } else {
    rload = l.bind("r.load", l.both(ri.at("valid"), io.active(l.inv(busy))));
  }
  rplan = plan_of(descriptor, false);
  if (p.max_outstanding == 1)
    rd = l.latch_fields("r.descriptor", descriptor, rload);
  rmode = l.latch("r.mode", rplan.mode, rload);
  auto rlength = l.latch("r.length", rplan.length, rload);
  rsize = l.latch("r.size", rplan.size, rload);
  // The held descriptor already determines the low start bits. Its upper bits
  // stay unchanged for both an in-word WRAP base and a wider WRAP start word.
  const auto rstart = l.global(l.b.widened(l.b.slice(plan_of(rd, false).start, 0, t),
                                           l.b.slice(rd.at("start_address"), t, ab - t)));
  rkind = p.max_outstanding == 1
              ? rd.at("burst")
              : l.latch("r.kind", l.pick(l.eq(descriptor.at("burst"), 2), l.num(2, 2), l.num(2, 1)),
                        rload);
  rspan = l.latch("r.container", rplan.container, rload);
  rbase = l.latch("r.base", l.global(rplan.base), rload);
  rstart_word = l.global(floor_word(rd.at("start_address")));
  sa = l.address_state("r.beat", ab);
  remaining = l.state("r.remaining", lb);
  rfull = p.joined ? zero : l.state("r.full", 1);
  rword = p.joined ? zero : l.address_state("r.word", ab);
  rfirst = l.state("r.first", 1);
  saved = l.state("r.saved", 1);
  saved_data = l.state("r.saved_data", p.data_bits);
  saved_result = l.state("r.saved_result", 2);
  saved_error = l.state("r.saved_error", 1);
  next_word = l.address_state("r.next_word", ab);
  rpass = l.eq(rmode, 0);
  // A request with nothing waiting before it leaves in its arrival cycle.
  const bool queued = p.max_outstanding > 1 || p.joined;
  rbypass = l.bind("r.bypass", queued ? l.both(ri.at("valid"), rroom) : rload);
  const auto rq_valid = queued ? rbypass : l.either(rbypass, io.active(l.both(busy, pending)));
  const Fields request = queued ? ri : view_fields(l, rd, ri, rbypass);
  auto rq = p.max_outstanding > 1 ? plan_of(request, false) : rplan;
  if (!queued)
    for (auto [field, held] : {std::pair{&Plan::mode, rmode},
                               {&Plan::length, rlength},
                               {&Plan::size, rsize},
                               {&Plan::start, rstart}})
      rq.*field = l.pick(rbypass, rq.*field, held);
  const auto request_pass = l.eq(rq.mode, 0);
  rq_fire = l.both(rq_valid, down_r_ready);
  Fields ro = request;
  ro["length"] = l.pick(request_pass, request.at("length"), rq.length);
  ro["size"] = l.pick(request_pass, request.at("size"), rq.size);
  ro["start_address"] = l.pick(request_pass, request.at("start_address"), rq.start);
  ro["address"] = l.pick(request_pass, request.at("address"), floor_size(rq.start, rq.size));
  ro["burst"] =
      l.pick(request_pass, request.at("burst"), l.pick(l.eq(rq.mode, 2), l.num(2, 2), l.num(2, 1)));
  ro["count"] = l.pick(request_pass, request.at("count"), count_of(rq.size));
  io.out("up_rreq", "ready", queued ? input_ready : io.active(l.inv(busy)));
  io.out("down_rreq", "valid", rq_valid);
  io.fields_out("down_rreq", ro, p.data_bits, true);
}

void UpsizeLowering::read_response() {
  // The source beat being answered, and where its bytes come from: the held
  // target word, or the saved start word once a WRAP has come back to it.
  const auto sa_word = floor_word(sa);
  const auto from_saved = l.both(saved, l.same(sa_word, rstart_word));
  const auto from_held = p.joined ? zero : l.both(rfull, l.same(sa_word, rword));
  // A word reaching an empty word entry answers at once; kept if beats remain.
  std::optional<ValueId> received_last;
  if (p.max_outstanding > 1)
    received_last = l.state("r.received_last", 1);
  // With several reads a word arrives for the gather only when it names the gather's key.
  const auto rvalid_in = p.max_outstanding > 1 ? l.both(rr.at("valid"), rmatch) : rr.at("valid");
  ValueId arriving = io.active(l.both(l.both(busy, l.inv(rpass)), l.both(l.inv(rfull), rvalid_in)));
  if (received_last)
    arriving = l.both(arriving, l.inv(*received_last));
  // Joined: a word offered is always the one the beat is in (the saved start word answers only
  // after the last target word has left).
  const auto from_arriving = p.joined ? arriving : l.both(arriving, l.same(sa_word, next_word));
  const auto from_word = l.either(from_held, from_arriving);
  const auto can_emit =
      io.active(l.both(busy, l.both(l.inv(rpass), l.either(from_word, from_saved))));
  const auto emit_fire = l.both(can_emit, up_r_ready);
  // A local miss descriptor's one word ends the read at once. The source's entry answers its beats.
  const auto arrived_decode =
      p.decode_packets ? l.both(rvalid_in, decode_packet::marked(l, p.decode_flag, rr.at("result"),
                                                                 rr.at("error")))
                       : zero;
  const auto word_decode = p.decode_packets && !p.joined ? l.state("r.word_decode", 1) : zero;
  const auto decoded = !p.decode_packets ? zero
                       : p.joined        ? arrived_decode
                                         : l.pick(rfull, word_decode, arrived_decode);
  const auto last_beat =
      p.decode_packets ? l.either(l.eq(remaining, 0), decoded) : l.eq(remaining, 0);
  const auto following = next_beat(sa, rd.at("size"), rkind, rspan, rbase);
  auto leaves_word = l.either(last_beat, l.inv(l.same(floor_word(following), sa_word)));
  if (p.joined) {
    // The same from the beat's place in its word: the next beat leaves at the word's last beat,
    // except inside a WRAP container of at most one word.
    const auto low = l.b.slice(sa, 0, t);
    const auto below =
        l.per_size(rd.at("size"), t, [&](unsigned n) { return l.num(t, (1ULL << n) - 1); });
    const auto at_end = l.eq(l.either(low, below), (1ULL << t) - 1);
    const auto inside = l.both(l.eq(rkind, 2), l.eq(rmode, 1));
    leaves_word = l.either(last_beat, l.both(at_end, l.inv(inside)));
  }
  // Take a target word while the word entry is empty (its ready is a register); the next word
  // passes straight to its beats in the cycle after the held one's last beat leaves. Pass mode
  // forwards responses.
  auto take = l.both(io.active(l.both(busy, l.both(l.inv(rpass), l.inv(rfull)))), rvalid_in);
  // Joined: a word is taken as its last beat here leaves.
  if (p.joined) {
    take = l.both(emit_fire, l.both(from_arriving, leaves_word));
    l.b.output("nl_hint_word_end", l.either(rpass, l.both(from_arriving, leaves_word)), "hint",
               "word_end");
  }
  if (received_last) {
    take = l.both(take, l.inv(*received_last));
    l.b.update(*received_last,
               l.pick(rload, zero, l.pick(l.both(take, rr.at("last")), one, *received_last)));
  }
  const auto release = l.both(emit_fire, l.both(from_held, leaves_word));
  const auto spent =
      l.both(emit_fire, l.both(l.both(from_arriving, l.inv(from_held)), leaves_word));
  const auto keep = l.both(take, l.inv(spent));
  if (p.decode_packets && !p.joined)
    l.b.update(word_decode, l.pick(keep, arrived_decode, word_decode));
  if (!p.joined) {
    l.b.update(rfull, l.pick(keep, one, l.pick(release, zero, rfull)));
    l.b.update(rword, l.pick(keep, next_word, rword));
  }
  l.b.update(next_word, l.pick(rload, floor_word(rplan.start),
                               l.pick(take,
                                      next_beat(next_word, rsize,
                                                l.pick(l.eq(rmode, 2), l.num(2, 2), l.num(2, 1)),
                                                rspan, rbase),
                                      next_word)));
  // A field of the word: the held word, else the word arriving now.
  auto word_of = [&](const std::string &name, ValueId value) {
    return p.joined ? value : l.pick(rfull, l.latch(name, value, keep), value);
  };
  const auto rdata = word_of("r.data", rr.at("data"));
  const auto rresult = word_of("r.result", rr.at("result"));
  const auto rerror = word_of("r.error", rr.at("error"));
  // A target word's exclusive_okay answers every source beat it holds.
  ValueId rexclusive = zero, saved_exclusive = zero;
  if (p.exclusive) {
    rexclusive = word_of("r.exclusive_okay", rr.at("exclusive_okay"));
    saved_exclusive = l.state("r.saved_exclusive_okay", 1);
  }
  const auto first_word =
      l.both(l.both(take, l.same(next_word, rstart_word)), unaligned(rd, rmode));
  l.b.update(saved, l.pick(l.either(rload, l.both(emit_fire, last_beat)), zero,
                           l.pick(first_word, one, saved)));
  l.b.update(saved_data, l.pick(first_word, rr.at("data"), saved_data));
  l.b.update(saved_result, l.pick(first_word, rr.at("result"), saved_result));
  l.b.update(saved_error, l.pick(first_word, rr.at("error"), saved_error));
  if (p.exclusive)
    l.b.update(saved_exclusive, l.pick(first_word, rr.at("exclusive_okay"), saved_exclusive));
  l.b.update(sa, l.pick(rload, descriptor.at("start_address"), l.pick(emit_fire, following, sa)));
  l.b.update(remaining, l.pick(rload, descriptor.at("length"),
                               l.pick(emit_fire, l.minus(remaining, l.num(lb, 1)), remaining)));
  l.b.update(rfirst, l.pick(rload, one, l.pick(emit_fire, zero, rfirst)));
  const auto pass_pop = l.both(io.active(l.both(busy, rpass)), l.both(rvalid_in, up_r_ready));
  if (!p.joined)
    l.b.update(pending,
               l.pick(rload, l.inv(l.both(rbypass, down_r_ready)), l.pick(rq_fire, zero, pending)));
  l.b.update(busy,
             l.pick(rload, one,
                    l.pick(l.either(l.both(emit_fire, last_beat), l.both(pass_pop, rr.at("last"))),
                           zero, busy)));
  if (p.max_outstanding > 1) {
    const auto complete = l.bind(
        "r.complete", l.either(l.both(emit_fire, last_beat), l.both(pass_pop, rr.at("last"))));
    auto count_next = [&](ValueId count, ValueId push, ValueId pop) {
      return l.pick(
          l.both(push, l.inv(pop)), l.plus(count, l.num(occupancy_bits, 1)),
          l.pick(l.both(pop, l.inv(push)), l.minus(count, l.num(occupancy_bits, 1)), count));
    };
    // A completed read is answered; the head slot frees once answered (in ring order).
    const auto head_done =
        l.either(l.both(complete, l.same(rslot, qhead)), pick_slot(rdone, qhead));
    const auto head_free = l.both(pick_slot(rused, qhead), head_done);
    for (unsigned k = 0; k < p.max_outstanding; ++k) {
      const auto freed = l.both(head_free, l.eq(qhead, k));
      l.b.update(rdone[k],
                 l.pick(freed, zero, l.pick(l.both(complete, l.eq(rslot, k)), one, rdone[k])));
    }
    l.b.update(rstarted, l.pick(rload, zero, l.pick(l.either(take, pass_pop), one, rstarted)));
    l.b.update(qcount, count_next(qcount, accept, head_free));
    l.b.update(qhead, l.pick(head_free, advance(qhead), qhead));
  }
  const auto word_result = l.pick(from_word, rresult, saved_result);
  const auto word_error = l.pick(from_word, rerror, saved_error);
  const auto word_data_r = l.pick(from_word, rdata, saved_data);
  const auto failed = l.either(word_error, l.inv(l.eq(word_result, 0)));
  Fields up;
  if (p.tag_bits)
    up["tag"] = rd.at("tag");
  if (p.id_bits)
    up["id"] = rd.at("id");
  if (p.source_bits)
    up["source"] = rd.at("source");
  up["address"] = sa;
  const auto count = l.minus(count_of(rd.at("size")),
                             l.fit(l.minus(sa, floor_size(sa, rd.at("size"))), count_bits));
  up["count"] = p.decode_packets ? l.pick(decoded, l.num(count_bits, 4), count) : count;
  up["result"] = word_result;
  up["error"] = word_error;
  mark_decode(up, decoded, remaining);
  if (p.exclusive)
    up["exclusive_okay"] = l.pick(from_word, rexclusive, saved_exclusive);
  // Every word of a packet carries the packet's response user field.
  if (p.response_user_bits)
    up["user"] = word_of("r.user", rr.at("user"));
  up["first"] = rfirst;
  up["last"] = last_beat;
  up["data"] =
      l.pick(failed, l.num(p.data_bits, 0), read_word_data(word_data_r, rd, sa, remaining));
  if (p.decode_packets)
    up["data"] =
        l.pick(decoded, decode_packet::word_data(l, remaining, p.data_bits), up.at("data"));
  if (p.response_run_bits)
    l.b.output("nl_hint_wrap_end",
               burst_run_end(l, rd, l.pick(rpass, l.fit(rr.at("address"), ab), sa), p.data_bits,
                             p.response_run_bits),
               "hint", "wrap_end");
  up = view_fields(l, up, rr, rpass);
  const auto pass_ready =
      l.both(rpass, p.max_outstanding > 1 ? l.both(up_r_ready, rmatch) : up_r_ready);
  io.out("down_rrsp", "ready", l.either(take, io.active(l.both(busy, pass_ready))));
  io.out("up_rrsp", "valid", l.either(can_emit, io.active(l.both(busy, l.both(rpass, rvalid_in)))));
  io.fields_out("up_rrsp", up, p.data_bits, false);
}

// Several reads at an outlet with interleaved read data. A target word names its read: the outlet
// fills the oldest read of its target ID, and reads of one target ID arrive in order, so the oldest
// read of the word's {source, AxID} here is the same read. Each read keeps its own place (the
// beat's offset in its word), its beats left and its plan; the word's high address and its data
// stay in the outlet's response stage until the word's last beat leaves here. Each descriptor
// owns its start-word store until its final source beat is accepted.
void UpsizeLowering::reads_by_key() {
  const auto ri = l.arrive(io.inputs("up_rreq", p.data_bits, true));
  rr = l.arrive(io.inputs("down_rrsp", p.data_bits, false));
  down_r_ready = io.in("down_rreq", "ready", 1);
  up_r_ready = io.in("up_rrsp", "ready", 1);
  const unsigned reads = p.max_outstanding;
  std::vector<Fields> queue;
  std::vector<ValueId> used, modes, offsets, lefts, fresh;
  std::vector<std::vector<ValueId>> age(reads);
  for (unsigned k = 0; k < reads; ++k) {
    const auto at = "r.context" + std::to_string(k);
    used.push_back(l.state(at + ".used", 1));
    modes.push_back(l.state(at + ".mode", 2));
    offsets.push_back(l.state(at + ".offset", t));
    lefts.push_back(l.state(at + ".left", lb));
    fresh.push_back(l.state(at + ".first", 1));
    for (unsigned j = 0; j < reads; ++j)
      age[k].push_back(k < j ? l.state("r.age" + std::to_string(k) + "_" + std::to_string(j), 1)
                             : zero);
  }
  for (unsigned k = 0; k < reads; ++k) {
    Fields held_fields;
    for (const auto &[name, value] : ri)
      if (name != "valid")
        held_fields[name] = l.state_like("r.context" + std::to_string(k) + "." + name, value);
    queue.push_back(held_fields);
  }
  auto lowest_free = l.num(slot_bits, 0);
  auto any_free = zero;
  for (unsigned k = reads; k-- > 0;) {
    lowest_free = l.pick(l.inv(used[k]), l.num(slot_bits, k), lowest_free);
    any_free = l.either(any_free, l.inv(used[k]));
  }
  const auto rq = plan_of(ri, false);
  const auto draining = l.state("r.drain", 1);
  const auto drain_slot = l.state("r.drain_slot", slot_bits);
  // The oldest read of the word's key.
  std::vector<ValueId> keyed, first_keyed;
  for (unsigned k = 0; k < reads; ++k)
    keyed.push_back(l.both(used[k], same_key(queue[k], rr)));
  for (unsigned k = 0; k < reads; ++k) {
    auto any_older = zero;
    for (unsigned j = 0; j < reads; ++j)
      if (j != k)
        any_older = l.either(any_older, l.both(keyed[j], j < k ? age[j][k] : l.inv(age[k][j])));
    first_keyed.push_back(l.both(keyed[k], l.inv(any_older)));
  }
  auto named = l.num(slot_bits, 0);
  for (unsigned k = reads; k-- > 0;)
    named = l.pick(first_keyed[k], l.num(slot_bits, k), named);
  rslot = l.bind("r.slot", l.pick(draining, drain_slot, named));
  rd = queue.front();
  for (auto &[name, value] : rd)
    for (unsigned k = 1; k < reads; ++k)
      value = l.pick(l.eq(rslot, k), queue[k].at(name), value);
  rd = l.global(rd);
  const auto mode = pick_slot(modes, rslot), offset = pick_slot(offsets, rslot);
  const auto left = pick_slot(lefts, rslot);
  rpass = l.eq(mode, 0);
  const auto arriving = io.active(l.both(rr.at("valid"), l.inv(draining)));
  const auto fanning = l.both(arriving, l.inv(rpass));
  const auto emit_fire = l.both(l.either(fanning, io.active(draining)), up_r_ready);
  const auto pass_pop = l.both(arriving, l.both(rpass, up_r_ready));
  // A local miss descriptor's one word ends the read at once. The source's entry answers its beats.
  const auto decoded =
      p.decode_packets ? l.both(arriving, decode_packet::marked(l, p.decode_flag, rr.at("result"),
                                                                rr.at("error")))
                       : zero;
  const auto last_beat = p.decode_packets ? l.either(l.eq(left, 0), decoded) : l.eq(left, 0);
  // The beat's place in its word: the next beat leaves it at the word's last beat. Inside a WRAP
  // container of less than a word the beats turn inside the word; a whole-word container's beats
  // below its start follow from the store.
  const auto below =
      l.per_size(rd.at("size"), t, [&](unsigned n) { return l.num(t, (1ULL << n) - 1); });
  const auto top = l.either(offset, below);
  const auto inside = l.both(l.eq(rd.at("burst"), 2), l.eq(mode, 1));
  const auto storing = unaligned(rd, mode);
  const auto leaves_word =
      l.either(last_beat, l.both(l.eq(top, (1ULL << t) - 1), l.either(l.inv(inside), storing)));
  const auto stepped = l.plus(top, l.num(t, 1));
  const auto mask =
      l.either(l.b.slice(l.shl(l.fit(rd.at("length"), t + 7), rd.at("size"), 7), 0, t), below);
  const auto turned =
      l.either(l.both(l.b.slice(rd.at("start_address"), 0, t), l.b.unary(Op::BitNot, mask, t)),
               l.both(stepped, mask));
  const auto following = l.pick(inside, turned, stepped);
  const auto take = l.both(l.both(fanning, up_r_ready), leaves_word);
  const auto in_store = l.both(take, storing);
  const auto store_first = l.both(in_store, rr.at("first"));
  const auto store_tail = l.both(in_store, rr.at("last"));
  rroom = io.active(any_free);
  const auto input_ready = l.both(rroom, down_r_ready);
  accept = l.bind("r.accept", l.both(ri.at("valid"), input_ready));
  std::vector<ValueId> into;
  for (unsigned k = 0; k < reads; ++k) {
    into.push_back(l.both(accept, l.eq(lowest_free, k)));
    load_fields(l, queue[k], ri, into.back());
  }
  const auto complete =
      l.bind("r.complete", l.either(l.both(emit_fire, last_beat), l.both(pass_pop, rr.at("last"))));
  for (unsigned k = 0; k < reads; ++k) {
    const auto mine = l.eq(rslot, k);
    const auto beat = l.both(emit_fire, mine);
    l.b.update(used[k], l.pick(into[k], one, l.pick(l.both(complete, mine), zero, used[k])));
    l.b.update(modes[k], l.pick(into[k], rq.mode, modes[k]));
    l.b.update(offsets[k], l.pick(into[k], l.b.slice(ri.at("start_address"), 0, t),
                                  l.pick(beat, following, offsets[k])));
    l.b.update(lefts[k], l.pick(into[k], ri.at("length"),
                                l.pick(beat, l.minus(lefts[k], l.num(lb, 1)), lefts[k])));
    l.b.update(fresh[k], l.pick(into[k], one, l.pick(beat, zero, fresh[k])));
    for (unsigned j = k + 1; j < reads; ++j)
      l.b.update(age[k][j], l.pick(into[j], one, l.pick(into[k], zero, age[k][j])));
  }
  const auto drain_start = l.both(store_tail, l.inv(last_beat));
  l.b.update(draining,
             l.pick(drain_start, one, l.pick(l.both(emit_fire, last_beat), zero, draining)));
  l.b.update(drain_slot, l.pick(drain_start, rslot, drain_slot));
  const auto bank = replay_bank(store_first, complete, lowest_free);
  const auto from_store = [&](const std::string &name) {
    return l.pick(draining, bank.at(name), rr.at(name));
  };
  const auto word_result = from_store("result"), word_error = from_store("error");
  const auto failed = l.either(word_error, l.inv(l.eq(word_result, 0)));
  const auto word_address = l.pick(draining, rd.at("start_address"), rr.at("address"));
  const auto beat_address = l.global(l.b.concat(l.b.slice(word_address, t, ab - t), offset));
  Fields up;
  for (const auto *name : {"tag", "id", "source"})
    if (rr.contains(name))
      up[name] = l.pick(draining, rd.at(name), rr.at(name));
  up["address"] = beat_address;
  // An unaligned first beat holds the bytes from its start up, so narrower links carry only those.
  up["count"] = l.minus(count_of(rd.at("size")), l.zext(l.both(offset, below), count_bits));
  if (p.decode_packets)
    up["count"] = l.pick(decoded, l.num(count_bits, 4), up.at("count"));
  up["result"] = word_result;
  up["error"] = word_error;
  mark_decode(up, decoded, left);
  for (const auto *name : {"exclusive_okay", "user"})
    if (rr.contains(name))
      up[name] = from_store(name);
  up["first"] = pick_slot(fresh, rslot);
  up["last"] = last_beat;
  up["data"] = l.pick(failed, l.num(p.data_bits, 0),
                      read_word_data(from_store("data"), rd, beat_address, left));
  if (p.decode_packets)
    up["data"] = l.pick(decoded, decode_packet::word_data(l, left, p.data_bits), up.at("data"));
  up = view_fields(l, up, rr, l.both(rpass, l.inv(draining)));
  l.b.output("nl_hint_word_end", l.either(rpass, l.both(fanning, leaves_word)), "hint", "word_end");
  // The stage waits for the stored word's first beat, as for a word entering a serializer.
  const auto stored_first = l.state("r.drain_first", 1);
  l.b.update(stored_first, l.pick(drain_start, one, l.pick(emit_fire, zero, stored_first)));
  l.b.output("nl_hint_read_stored", stored_first, "hint", "read_stored");
  if (p.response_run_bits)
    l.b.output("nl_hint_wrap_end",
               burst_run_end(l, rd, l.pick(rpass, rr.at("address"), beat_address), p.data_bits,
                             p.response_run_bits),
               "hint", "wrap_end");
  Fields ro = ri;
  const auto request_pass = l.eq(rq.mode, 0);
  ro["length"] = l.pick(request_pass, ri.at("length"), rq.length);
  ro["size"] = l.pick(request_pass, ri.at("size"), rq.size);
  ro["start_address"] = l.pick(request_pass, ri.at("start_address"), rq.start);
  ro["address"] = l.pick(request_pass, ri.at("address"), floor_size(rq.start, rq.size));
  ro["burst"] =
      l.pick(request_pass, ri.at("burst"), l.pick(l.eq(rq.mode, 2), l.num(2, 2), l.num(2, 1)));
  ro["count"] = l.pick(request_pass, ri.at("count"), count_of(rq.size));
  io.out("up_rreq", "ready", input_ready);
  io.out("down_rreq", "valid", l.both(ri.at("valid"), rroom));
  io.fields_out("down_rreq", ro, p.data_bits, true);
  io.out("down_rrsp", "ready", l.either(take, pass_pop));
  io.out("up_rrsp", "valid", l.either(arriving, io.active(draining)));
  io.fields_out("up_rrsp", up, p.data_bits, false);
}

Fields UpsizeLowering::replay_bank(ValueId capture, ValueId release, ValueId allocated) {
  Fields selected;
  for (unsigned k = 0; k < p.max_outstanding; ++k) {
    const auto at = "r.context" + std::to_string(k) + ".";
    const auto mine = l.eq(rslot, k);
    const auto take = l.both(accept, l.eq(allocated, k));
    const auto save = l.both(capture, mine);
    const auto valid = l.state(at + "saved_valid", 1);
    l.b.update(valid,
               l.pick(l.either(take, l.both(release, mine)), zero, l.pick(save, one, valid)));
    for (const auto *name : {"data", "result", "error", "exclusive_okay", "user"}) {
      if (!rr.contains(name))
        continue;
      const auto field = l.latch(at + "saved_" + name, rr.at(name), save);
      selected[name] = k ? l.pick(mine, field, selected.at(name)) : field;
    }
  }
  return selected;
}

} // namespace

Result<NodeHardware> lower_axi4_upsize(const Axi4UpsizeProfile &p) {
  const unsigned ab = p.address_bits;
  if (!valid_name(p.name) || p.id_bits > 16 || (p.len_bits < 4 || p.len_bits > 8) ||
      p.source_bits > max_source_bits || p.tag_bits > 6 || !p.max_outstanding ||
      p.max_outstanding > 16 || ab < 32 || ab > 48 ||
      (p.arrival.keep && (p.arrival.keep < 12 || p.arrival.keep >= ab ||
                          p.arrival.base % (std::uint64_t{1} << p.arrival.keep))) ||
      (p.data_bits != 32 && p.data_bits != 64 && p.data_bits != 128 && p.data_bits != 256 &&
       p.data_bits != 512 && p.data_bits != 1024))
    return Result<NodeHardware>::failure(
        error("NCL_AXI4_UPSIZE_PROFILE", "axi4_upsize",
              "Expected 32..48 address bits, 0..16 ID bits, 4..8 length bits, 0..9 source bits, "
              "0..6 tag bits, a 32-, 64-, 128-, 256-, 512- or 1024-bit target, and 1..16 "
              "outstanding packets per direction"));
  Logic l;
  UpsizeLowering engine(l, p);
  engine.write_words();
  engine.write_response();
  if (p.joined && p.max_outstanding > 1) {
    engine.reads_by_key();
  } else {
    engine.read_request();
    engine.read_response();
  }
  return l.finish("nocloom_axi4_upsize_" + p.name);
}

std::pair<unsigned, unsigned> axi4_upsize_state_bits(const Axi4UpsizeProfile &profile) {
  return direction_state_bits(lower_axi4_upsize(profile));
}
} // namespace nocloom::detail
