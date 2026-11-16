#include "tilelink_uh_outlet.hpp"
#include "burst_faces.hpp"
#include "diagnostic.hpp"
#include "mapping_decode.hpp"
#include <bit>
#include <iterator>
#include <numeric>

namespace nocloom::detail {
ValueId tilelink_block_size(logic::Logic &l, ValueId address, ValueId available, unsigned size_bits,
                            unsigned maximum) {
  auto size = l.num(size_bits, 0);
  for (unsigned n = 1; n < static_cast<unsigned>(std::bit_width(maximum)); ++n) {
    const auto fits = l.inv(l.below(available, l.num(l.width(available), std::uint64_t{1} << n)));
    const auto aligned = l.eq(l.b.slice(address, 0, n), 0);
    size = l.pick(l.both(fits, aligned), l.num(size_bits, n), size);
  }
  return size;
}
namespace {
using logic::Fields;
using logic::Logic;
struct TileLinkUhOutlet {
  const TileLinkUhOutletProfile &p;
  const BurstOutletProfile &b;
  unsigned bits;
  Logic l;
  Fields w, r, packet, word;
  ValueId busy, write, cursor, left, first;
  ValueId message, base, size, a_left, d_left, a_done, d_done, ack_error;
  ValueId wfull, rfull, dfull, ddata, dmask, derror, assembly, assembled, read_error;
  ValueId response_address, response_last, response_first, response_data, response_code;
  ValueId packet_error, local_code, advance, finish_word, message_start, chosen_size;
  ValueId a_take, d_take, w_take, r_take, accept, release, response_take;
  ValueId requested_mask, partial_mask, consumed, read_step, read_complete, write_step;
  ValueId next_cursor, word_end, step_bytes, message_complete;
  explicit TileLinkUhOutlet(const TileLinkUhOutletProfile &profile);
  ValueId mask(ValueId address, ValueId bytes);
  ValueId clean(ValueId data, ValueId enables);
  ValueId code(const Fields &f, ValueId address);
  ValueId aligned(ValueId address, ValueId exponent);
  ValueId after_word(ValueId address);
  void context_allocate();
  void select_block();
  void a_schedule();
  void d_receive();
  void packet_response();
  void retire();
};
TileLinkUhOutlet::TileLinkUhOutlet(const TileLinkUhOutletProfile &profile)
    : p(profile), b(p.packet), bits(b.transport_address_bits ? b.transport_address_bits : 32) {
  l.block = b.arrival;
  l.block_bits = bits;
  BurstProfile shape{b.id_bits, b.packet_len_bits, true, bits};
  shape.exclusive = b.exclusive;
  shape.tag_bits = b.tag_bits;
  shape.user_bits = b.user_bits;
  shape.response_user_bits = b.response_user_bits;
  shape.data_user_bits_per_byte = b.data_user_bits_per_byte;
  w = l.arrive(l.face("wreq", burst_request_fields(32, shape, b.source_bits)));
  r = l.arrive(l.face("rreq", burst_request_fields(32, shape, b.source_bits)));
  busy = l.state("tl.busy", 1);
  write = l.state("tl.write", 1);
  cursor = l.address_state("tl.cursor", bits);
  left = l.state("tl.words_left", b.packet_len_bits + 1);
  first = l.state("tl.first", 1);
  message = l.state("tl.message", 1);
  base = l.state("tl.base", b.address_bits);
  size = l.state("tl.size", p.size_bits);
  a_left = l.state("tl.a_left", 11);
  d_left = l.state("tl.d_left", 11);
  a_done = l.state("tl.a_done", 1);
  d_done = l.state("tl.d_done", 1);
  ack_error = l.state("tl.ack_error", 1);
  wfull = l.state("tl.wfull", 1);
  rfull = l.state("tl.rfull", 1);
  dfull = l.state("tl.dfull", 1);
  ddata = l.state("tl.ddata", 32);
  dmask = l.state("tl.dmask", 4);
  derror = l.state("tl.derror", 1);
  assembly = l.state("tl.assembly", 32);
  assembled = l.state("tl.assembled", 4);
  read_error = l.state("tl.read_error", 1);
  packet_error = l.state("tl.packet_error", 2);
  local_code = l.state("tl.local_code", 2);
  for (const auto *field : {"address", "start_address", "length", "size", "burst", "error"})
    packet[field] = l.state_like("tl.packet." + std::string(field), r.at(field));
  for (const auto *field : {"id", "source", "tag"})
    if (r.contains(field))
      packet[field] = l.state_like("tl.packet." + std::string(field), r.at(field));
  for (const auto *field : {"data", "be", "last"})
    word[field] = l.state_like("tl.word." + std::string(field), w.at(field));
  response_address = l.address_state("tl.response.address", bits);
  response_data = l.state("tl.response.data", 32);
  response_code = l.state("tl.response.code", 2);
  response_first = l.state("tl.response.first", 1);
  response_last = l.state("tl.response.last", 1);
  context_allocate();
  select_block();
  a_schedule();
  d_receive();
  packet_response();
  retire();
}
ValueId TileLinkUhOutlet::aligned(ValueId address, ValueId exponent) {
  return l.per_size(exponent, 2, [&](unsigned n) {
    return l.both(address,
                  l.num(bits, ((std::uint64_t{1} << bits) - 1) ^ ((std::uint64_t{1} << n) - 1)));
  });
}
ValueId TileLinkUhOutlet::mask(ValueId address, ValueId bytes) {
  const auto offset = l.b.slice(address, 0, 2);
  std::vector<ValueId> lanes;
  for (unsigned k = 0; k < 4; ++k)
    lanes.push_back(l.both(l.inv(l.below(l.num(3, k), l.fit(offset, 3))),
                           l.below(l.num(3, k), l.plus(l.fit(offset, 3), bytes))));
  return std::accumulate(std::next(lanes.begin()), lanes.end(), lanes.front(),
                         [&](ValueId value, ValueId lane) { return l.b.concat(lane, value); });
}
ValueId TileLinkUhOutlet::clean(ValueId data, ValueId enables) {
  auto result = l.num(32, 0);
  for (unsigned k = 0; k < 4; ++k) {
    const auto byte = l.pick(l.b.slice(enables, k, 1), l.b.slice(data, k * 8, 8), l.num(8, 0));
    const auto shifted = k ? l.b.concat(byte, l.num(k * 8, 0)) : byte;
    result = l.either(result, l.fit(shifted, 32));
  }
  return result;
}
ValueId TileLinkUhOutlet::code(const Fields &f, ValueId address) {
  auto miss = f.at("error");
  if (!b.mappings.empty())
    miss = l.either(miss, l.inv(mapping_decode::any(l, address, bits, b.mappings)));
  auto refused = l.bit(0);
  if (!b.fixed_single)
    refused = l.eq(f.at("burst"), 0);
  refused = l.either(refused, l.below(l.num(3, 2), f.at("size")));
  if (b.read_only || b.write_only)
    refused = l.either(refused, b.read_only ? write : l.inv(write));
  return l.pick(miss, l.num(2, 1), l.pick(refused, l.num(2, 2), l.num(2, 0)));
}
void TileLinkUhOutlet::context_allocate() {
  const auto tail = l.both(write, l.both(l.inv(wfull), l.inv(l.eq(left, 0))));
  const auto wr = l.active(l.either(l.inv(busy), tail));
  const auto rr = l.active(l.both(l.inv(busy), l.inv(w.at("valid"))));
  l.out("wreq", "ready", wr);
  l.out("rreq", "ready", rr);
  w_take = l.bind("tl.w_take", l.both(w.at("valid"), wr));
  r_take = l.bind("tl.r_take", l.both(r.at("valid"), rr));
  accept = l.bind("tl.accept", l.both(l.inv(busy), l.either(w_take, r_take)));
  const auto take_write = l.both(accept, w_take);
  l.b.update(write, l.pick(accept, w_take, write));
  for (const auto &[field, state] : packet)
    l.b.update(state, l.pick(accept, l.pick(take_write, w.at(field), r.at(field)), state));
  for (const auto &[field, state] : word)
    l.b.update(state, l.pick(w_take, w.at(field), state));
}
void TileLinkUhOutlet::select_block() {
  const auto stride = l.pow2(packet.at("size"), bits, 2);
  word_end = l.plus(aligned(cursor, packet.at("size")), stride);
  const auto offset = l.minus(cursor, aligned(cursor, packet.at("size")));
  const auto remaining = l.minus(l.shl(l.fit(left, bits), packet.at("size"), 2), offset);
  auto available = l.pick(l.eq(packet.at("burst"), 0), l.minus(word_end, cursor), remaining);
  const auto narrow_write = l.both(write, l.below(packet.at("size"), l.num(3, 2)));
  available = l.pick(narrow_write, l.lesser(available, l.minus(word_end, cursor)), available);
  // A WRAP message stops at the container top before continuing at its base.
  const auto span = l.shl(l.inc(l.fit(packet.at("length"), bits)), packet.at("size"), 2);
  const auto wrap_base = l.floor(packet.at("address"), span);
  const auto wrap_top = l.plus(wrap_base, span);
  available = l.pick(l.eq(packet.at("burst"), 2), l.lesser(available, l.minus(wrap_top, cursor)),
                     available);
  if (!b.mappings.empty()) {
    const auto end = mapping_decode::end(l, l.fit(cursor, bits + 1), bits + 1, b.mappings);
    available = l.lesser(available, l.fit(l.minus(end, l.fit(cursor, bits + 1)), bits));
  }
  chosen_size = tilelink_block_size(l, cursor, available, p.size_bits, p.max_transfer_bytes);
  const auto selection_code = code(packet, cursor);
  const auto prepared = l.both(busy, l.both(l.inv(message), l.inv(l.eq(left, 0))));
  const auto writable = l.either(l.inv(write), wfull);
  const auto skip = l.both(l.eq(selection_code, 0), l.both(write, l.eq(word.at("be"), 0)));
  message_start =
      l.both(prepared, l.both(writable, l.both(l.inv(skip), l.both(l.inv(dfull), l.inv(rfull)))));
  if (p.scheduled) {
    l.bind("tl.candidate", message_start);
    message_start = l.both(message_start, l.in("hint", "start", 1));
  }
  l.bind("tl.message_start", message_start);
  l.b.update(local_code, l.pick(message_start, selection_code, local_code));
  l.b.update(size, l.pick(message_start, chosen_size, size));
  auto address = cursor;
  if (!b.mappings.empty())
    address = mapping_decode::local(l, cursor, bits, b.mappings);
  l.b.update(base, l.pick(message_start, l.fit(address, b.address_bits), base));
}
void TileLinkUhOutlet::a_schedule() {
  step_bytes = l.per_size(size, 12, [&](unsigned n) { return l.num(bits, n < 2 ? 1U << n : 4); });
  requested_mask = mask(cursor, l.fit(step_bytes, 3));
  partial_mask = l.both(requested_mask, word.at("be"));
  auto valid = l.both(message, l.both(l.inv(a_done), l.eq(local_code, 0)));
  valid = l.active(l.both(valid, l.either(l.inv(write), wfull)));
  a_take = l.bind("tl.a_take", l.both(valid, l.in("tl", "a_ready", 1)));
  const auto full = l.both(l.eq(a_left, 1), l.same(partial_mask, requested_mask));
  const auto single = l.inv(l.below(l.num(p.size_bits, 2), size));
  l.out("tl", "a_valid", valid);
  l.out("tl", "a_opcode",
        l.pick(write, l.pick(l.both(single, full), l.num(3, 0), l.num(3, 1)), l.num(3, 4)));
  l.out("tl", "a_param", l.num(3, 0));
  l.out("tl", "a_size", size);
  l.out("tl", "a_address", base);
  l.out("tl", "a_mask", l.pick(write, partial_mask, requested_mask));
  l.out("tl", "a_data", l.pick(write, clean(word.at("data"), partial_mask), l.num(32, 0)));
  l.out("tl", "a_corrupt", l.bit(0));
  if (p.source_bits)
    l.out("tl", "a_source", l.num(p.source_bits, 0));
}
void TileLinkUhOutlet::d_receive() {
  const auto source = p.source_bits ? l.in("tl", "d_source", p.source_bits) : l.bit(0);
  const auto ready = l.active(l.both(l.both(message, l.inv(d_done)),
                                     l.both(l.eq(source, 0), l.either(write, l.inv(dfull)))));
  l.out("tl", "d_ready", ready);
  d_take = l.bind("tl.d_take", l.both(ready, l.in("tl", "d_valid", 1)));
  const auto denied = l.in("tl", "d_denied", 1);
  const auto corrupt = l.in("tl", "d_corrupt", 1);
  const auto data = l.in("tl", "d_data", 32);
  l.in("tl", "d_opcode", 3);
  l.in("tl", "d_param", 2);
  l.in("tl", "d_size", p.size_bits);
  if (p.sink_bits)
    l.in("tl", "d_sink", p.sink_bits);
  l.b.update(ack_error,
             l.pick(message_start, l.bit(0), l.either(ack_error, l.both(d_take, denied))));
  const auto incoming = l.both(d_take, l.inv(write));
  l.b.update(ddata, l.pick(incoming, data, ddata));
  l.b.update(derror, l.pick(incoming, l.either(denied, corrupt), derror));
  // D data are held independently of the A handshake and of network readiness.
}
ValueId TileLinkUhOutlet::after_word(ValueId address) {
  auto next = l.plus(aligned(address, packet.at("size")), l.pow2(packet.at("size"), bits, 2));
  const auto span = l.shl(l.inc(l.fit(packet.at("length"), bits)), packet.at("size"), 2);
  const auto bottom = l.floor(packet.at("address"), span);
  next =
      l.pick(l.both(l.eq(packet.at("burst"), 2), l.same(next, l.plus(bottom, span))), bottom, next);
  return l.pick(l.eq(packet.at("burst"), 0), packet.at("start_address"), next);
}
void TileLinkUhOutlet::packet_response() {
  response_take = l.bind("tl.response_take", l.both(rfull, l.in("rrsp", "ready", 1)));
  const auto response_at = l.address_state("tl.response_cursor", bits);
  const auto need = mask(response_at, l.fit(l.minus(l.plus(aligned(response_at, packet.at("size")),
                                                           l.pow2(packet.at("size"), bits, 2)),
                                                    response_at),
                                            3));
  consumed = l.both(dmask, need);
  read_step = l.both(dfull, l.inv(rfull));
  const auto joined = l.either(assembled, consumed);
  read_complete = l.both(read_step, l.same(joined, need));
  const auto local =
      l.both(message, l.both(l.inv(write), l.both(l.inv(rfull), l.inv(l.eq(local_code, 0)))));
  const auto push = l.either(read_complete, local);
  const auto merged = l.either(assembly, clean(ddata, consumed));
  const auto failed = l.either(read_error, derror);
  l.b.update(assembly, l.pick(push, l.num(32, 0), l.pick(read_step, merged, assembly)));
  l.b.update(assembled, l.pick(push, l.num(4, 0), l.pick(read_step, joined, assembled)));
  l.b.update(read_error, l.pick(push, l.bit(0), l.either(read_error, l.both(read_step, derror))));
  l.b.update(response_data,
             l.pick(push, l.pick(l.either(local, failed), l.num(32, 0), merged), response_data));
  l.b.update(response_code,
             l.pick(push, l.pick(local, local_code, l.pick(failed, l.num(2, 2), l.num(2, 0))),
                    response_code));
  l.b.update(response_address,
             l.pick(push, aligned(response_at, packet.at("size")), response_address));
  l.b.update(response_first, l.pick(push, first, response_first));
  l.b.update(response_last, l.pick(push, l.eq(left, 1), response_last));
  l.b.update(rfull, l.either(push, l.both(rfull, l.inv(response_take))));
  l.b.update(response_at,
             l.pick(accept, l.pick(w_take, w.at("start_address"), r.at("start_address")),
                    l.pick(push, after_word(response_at), response_at)));
  Fields out;
  BurstProfile shape{b.id_bits, b.packet_len_bits, false, bits};
  shape.exclusive = b.exclusive;
  shape.tag_bits = b.tag_bits;
  shape.response_user_bits = b.response_user_bits;
  shape.data_user_bits_per_byte = b.data_user_bits_per_byte;
  for (const auto &[name, width] : burst_response_fields(32, shape, b.source_bits))
    out[name] = packet.contains(name) ? packet.at(name) : l.num(width, 0);
  out["address"] = response_address;
  out["result"] = response_code;
  out["count"] = l.pow2(packet.at("size"), burst_count_bits(32), 2);
  out["data"] = response_data;
  out["first"] = response_first;
  out["last"] = response_last;
  l.drive("rrsp", out, burst_response_fields(32, shape, b.source_bits));
  l.out("rrsp", "valid", l.active(rfull));
  l.bind("wrap_end", burst_run_end(l, packet, response_address, 32,
                                   b.response_run_bits ? b.response_run_bits : 2));
  auto cut = l.bit(0);
  if (p.segmented) {
    cut = l.state("tl.response.cut", 1);
    l.b.update(
        cut,
        l.pick(push, l.both(l.both(d_done, l.same(consumed, dmask)), l.inv(l.eq(left, 1))), cut));
  }
  l.bind("cut", p.segmented ? l.both(cut, l.bit(1)) : cut);
  if (b.response_run_bits)
    l.out("hint", "wrap_end", l.bindings.at("wrap_end"));
  const auto write_ready = l.both(busy, l.both(write, l.both(l.eq(left, 0), l.inv(message))));
  out["address"] = packet.at("address");
  out["result"] = packet_error;
  out["count"] = l.num(burst_count_bits(32), 4);
  out["data"] = l.num(32, 0);
  out["first"] = out["last"] = l.bit(1);
  l.drive("wrsp", out, burst_response_fields(32, shape, b.source_bits));
  l.out("wrsp", "valid", l.active(write_ready));
  release = l.bind("tl.release", l.either(l.both(write_ready, l.in("wrsp", "ready", 1)),
                                          l.both(response_take, response_last)));
}
void TileLinkUhOutlet::retire() {
  const auto local = l.inv(l.eq(local_code, 0));
  const auto skipping = l.both(
      l.eq(code(packet, cursor), 0),
      l.both(busy, l.both(write, l.both(wfull, l.both(l.inv(message), l.eq(word.at("be"), 0))))));
  const auto local_write = l.both(message, l.both(write, l.both(local, wfull)));
  write_step = l.either(l.both(a_take, write), l.either(skipping, local_write));
  const auto read_advance = l.both(d_take, l.inv(write));
  advance = l.either(write_step, read_advance);
  const auto incremented = l.plus(cursor, step_bytes);
  const auto span = l.shl(l.inc(l.fit(packet.at("length"), bits)), packet.at("size"), 2);
  const auto bottom = l.floor(packet.at("address"), span);
  const auto wrapped =
      l.pick(l.both(l.eq(packet.at("burst"), 2), l.same(incremented, l.plus(bottom, span))), bottom,
             incremented);
  const auto container_end = l.inv(l.below(step_bytes, l.minus(word_end, cursor)));
  finish_word = l.both(write_step, l.either(container_end, l.either(skipping, local_write)));
  const auto fixed_end = l.both(l.eq(packet.at("burst"), 0), container_end);
  next_cursor = l.pick(l.either(finish_word, fixed_end), after_word(cursor), wrapped);
  l.b.update(cursor, l.pick(accept, l.pick(w_take, w.at("start_address"), r.at("start_address")),
                            l.pick(advance, next_cursor, cursor)));
  const auto popped = l.either(finish_word, l.both(l.inv(write), l.both(rfull, response_take)));
  l.b.update(left, l.pick(accept,
                          l.inc(l.fit(l.pick(w_take, w.at("length"), r.at("length")),
                                      b.packet_len_bits + 1)),
                          l.pick(popped, l.dec(left), left)));
  l.b.update(first,
             l.pick(accept, l.bit(1), l.both(first, l.inv(l.either(finish_word, response_take)))));
  l.b.update(wfull, l.either(w_take, l.both(wfull, l.inv(finish_word))));
  message_complete = l.bind("tl.message_complete", l.both(message, l.both(a_done, d_done)));
  l.b.update(message, l.either(message_start, l.both(message, l.inv(message_complete))));
  const auto beats = l.per_size(chosen_size, 12,
                                [&](unsigned n) { return l.num(11, n <= 2 ? 1 : 1U << (n - 2)); });
  l.b.update(a_left, l.pick(message_start, l.pick(write, beats, l.num(11, 1)),
                            l.pick(a_take, l.dec(a_left), a_left)));
  l.b.update(d_left, l.pick(message_start, l.pick(write, l.num(11, 1), beats),
                            l.pick(d_take, l.dec(d_left), d_left)));
  // Local errors use the packet response path without an external message.
  const auto selected_local = l.inv(l.eq(code(packet, cursor), 0));
  l.b.update(a_done, l.pick(message_start, selected_local,
                            l.either(a_done, l.both(a_take, l.eq(a_left, 1)))));
  l.b.update(d_done, l.pick(message_start, selected_local,
                            l.either(d_done, l.both(d_take, l.eq(d_left, 1)))));
  const auto incoming = l.both(d_take, l.inv(write));
  l.b.update(dmask,
             l.pick(incoming, requested_mask,
                    l.pick(read_step, l.both(dmask, l.b.unary(Op::BitNot, consumed, 4)), dmask)));
  l.b.update(dfull, l.pick(incoming, l.bit(1),
                           l.both(dfull, l.inv(l.both(read_step, l.same(consumed, dmask))))));
  const auto failure = l.pick(local, local_code, l.pick(ack_error, l.num(2, 2), l.num(2, 0)));
  l.b.update(packet_error,
             l.pick(accept, l.num(2, 0),
                    l.pick(l.both(message_complete, write),
                           l.pick(l.eq(packet_error, 0), failure, packet_error), packet_error)));
  l.b.update(busy, l.either(accept, l.both(busy, l.inv(release))));
  if (b.timeout_cycles) {
    const auto timed_out = l.state("tl.timed_out", 1);
    const auto expected_a = l.pick(
        write,
        l.per_size(size, 12, [&](unsigned n) { return l.num(11, n <= 2 ? 1 : 1U << (n - 2)); }),
        l.num(11, 1));
    const auto started = l.either(a_done, l.below(a_left, expected_a));
    const auto a_wait = l.both(l.inv(a_done), l.either(l.inv(write), wfull));
    const auto d_wait = l.both(started, l.both(l.inv(d_done), l.either(write, l.inv(dfull))));
    const auto waiting = l.both(message, l.either(a_wait, d_wait));
    l.watchdog(timed_out, b.timeout_cycles, waiting,
               l.either(l.inv(waiting), l.either(a_take, d_take)));
    l.out("status", "timed_out", timed_out);
  }
}
} // namespace
Result<NodeHardware> lower_tilelink_uh_outlet(const TileLinkUhOutletProfile &p) {
  if (const auto cycles = p.packet.timeout_cycles; cycles && !valid_timeout_cycles(cycles))
    return Result<NodeHardware>::failure(
        error("NCL_TILELINK_PROFILE", "timeout_cycles", "Timeout cycles must be 16 to 1048576"));
  const auto capacity = p.packet.max_outstanding;
  if (capacity < 1 || capacity > 16 || p.source_bits > 16 ||
      p.source_bits < static_cast<unsigned>(std::bit_width(capacity - 1)) || p.size_bits < 2 ||
      p.size_bits > 4 ||
      static_cast<unsigned>(std::bit_width(p.max_transfer_bytes)) > (1U << p.size_bits) ||
      p.sink_bits > 16 || p.max_transfer_bytes < 4 || p.max_transfer_bytes > 4096 ||
      !std::has_single_bit(p.max_transfer_bytes) || p.packet.timeout_completion ||
      p.packet.exclusive_target.mode != ExclusiveAccess::None)
    return Result<NodeHardware>::failure(
        {error("NCL_TILELINK_PROFILE", "tilelink_target",
               "Expected 1 to 16 packet contexts, sufficient source bits, a 4 to 4096 byte maximum "
               "and no exclusive monitor or "
               "timeout completion")});
  if (capacity > 1)
    return lower_tilelink_uh_contexts(p);
  TileLinkUhOutlet outlet(p);
  return outlet.l.finish("nocloom_tl_uh_outlet_" + p.packet.name);
}
} // namespace nocloom::detail
