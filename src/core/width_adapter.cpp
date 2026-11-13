#include "width_adapter.hpp"
#include "decode_packet.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "piece_count.hpp"
#include <algorithm>
#include <bit>
#include <stdexcept>

namespace nocloom::detail {
namespace {
constexpr unsigned lane_bytes = 4;
unsigned lane_bits(const std::optional<BurstProfile> &burst) {
  return 32 + (burst ? burst->data_user_bits_per_byte * 4 : 0);
}
std::uint64_t mask(unsigned w) { return w >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << w) - 1; }
// Ladder-global byte occupancy width: six bits hold the 32-byte top of the
// switch ladder. A burst composition with 64- or 128-byte endpoint words
// widens it (BurstProfile::occupancy_bits) for every level at once.
constexpr unsigned default_occupancy_bits = 6;
unsigned occupancy_bits_of(const std::optional<BurstProfile> &burst) {
  return burst ? burst->occupancy_bits : default_occupancy_bits;
}
// The widest word whose occupancy (up to the whole word) fits the count.
unsigned ladder_top_bytes(const std::optional<BurstProfile> &burst) {
  return 1U << (occupancy_bits_of(burst) - 1);
}
Diagnostic adapter_error(std::string code, std::string field, std::string message) {
  return error(std::move(code), std::move(field), std::move(message));
}
Result<WidthAdapterPlan> checked_plan(const WidthAdapterParameters &parameters) {
  auto planned = plan_width_adapter(parameters);
  if (!planned)
    return planned;
  if (planned.value().pass_through)
    return Result<WidthAdapterPlan>::failure({adapter_error(
        "NCL_WIDTH_ADAPTER_PASS_THROUGH", "width_adapter",
        "Equal transport word widths derive zero adapter instances; no module is lowered")});
  return planned;
}
// Control bundle fields in travel order; both directions lead with the full
// address so every level re-derives its used lanes from the low bits. The
// burst variant replicates the Weft burst contract descriptor (and ID) fields
// per fragment, in the contract's order (message.hpp).
std::vector<std::pair<std::string, unsigned>> control_fields(bool response, unsigned source_bits,
                                                             std::optional<BurstProfile> burst) {
  std::vector<std::pair<std::string, unsigned>> fields;
  fields.emplace_back("address", burst ? burst->address_bits : 32);
  if (response) {
    fields.emplace_back("result", 2);
    if (burst && burst->exclusive)
      fields.emplace_back("exclusive_okay", 1);
    if (burst && burst->response_user_bits)
      fields.emplace_back("user", burst->response_user_bits);
  } else {
    fields.emplace_back("write", 1);
    fields.emplace_back("protection", 3);
  }
  if (burst) {
    if (!response) {
      fields.emplace_back("length", burst->len_bits);
      fields.emplace_back("size", 3);
      fields.emplace_back("burst", 2);
      fields.emplace_back("cache", 4);
      if (burst->exclusive)
        fields.emplace_back("lock", 1);
      if (burst->user_bits)
        fields.emplace_back("user", burst->user_bits);
      if (burst->record_bits)
        fields.emplace_back("record", burst->record_bits);
      if (burst->security_bits)
        fields.emplace_back("security", burst->security_bits);
      if (burst->start_offset_bits)
        fields.emplace_back("start_offset", burst->start_offset_bits);
      else if (burst->start_address)
        fields.emplace_back("start_address", burst->address_bits);
    }
    if (burst->tag_bits)
      fields.emplace_back("tag", burst->tag_bits);
    if (burst->id_bits)
      fields.emplace_back("id", burst->id_bits);
  }
  if (source_bits)
    fields.emplace_back("source", source_bits);
  if (burst && burst->path_bits)
    fields.emplace_back("path", burst->path_bits);
  if (burst && (response ? burst->response_level_bits : burst->level_bits))
    fields.emplace_back("level", response ? burst->response_level_bits : burst->level_bits);
  if (burst && burst->target_bits && !response)
    fields.emplace_back("target", burst->target_bits);
  if (burst && burst->raise && !response)
    fields.emplace_back("raise", 1);
  return fields;
}
// The index of a named control field (control_fields order).
std::size_t control_index(const WidthAdapterParameters &parameters, const std::string &name) {
  const auto fields = control_fields(parameters.response, parameters.source_bits, parameters.burst);
  const auto it = std::ranges::find(fields, name, &std::pair<std::string, unsigned>::first);
  if (it == fields.end())
    throw std::logic_error("Unknown adapter control field " + name);
  return static_cast<std::size_t>(it - fields.begin());
}
// Runs (header, message.hpp point 18): every burst fragment is one.
bool runs(const WidthAdapterParameters &parameters) { return parameters.burst.has_value(); }
// A run's `reach` width at a word: the byte offset of its last byte in that word.
unsigned reach_bits(unsigned word_bytes) {
  return static_cast<unsigned>(std::countr_zero(word_bytes));
}
struct Fragment {
  ValueId valid, ready, first, last, error;
  // A response segment's end before its packet's last (message.hpp point 17); none without it.
  std::optional<ValueId> cut;
  std::vector<ValueId> control;
  // The byte count, or a run's extent (requests, with `ends` and `wrap_end`) or reach (responses,
  // with `wrap_end`).
  ValueId count{}, ends{}, wrap_end{};
  std::vector<ValueId> data, be;
};
// Lane i of the wide word covers bytes [4i, 4i+4) and is used when that offset
// lies in [address % word_bytes, address % word_bytes + count). The modulo
// wraps below the offset and never aliases a used byte, because the occupancy
// contract keeps offset + count within the word and both are multiples of four.
// The burst profile carries byte-granular occupancies (count 1 or 2 for narrow
// beats), so its cone widens to interval overlap: a lane is used when it
// contains the offset byte or starts inside the used range. For word-aligned
// occupancies the overlap test selects exactly the word-rule lanes.
std::vector<ValueId> used_lanes(logic::Logic &l, const WidthAdapterPlan &plan, ValueId address,
                                ValueId count) {
  std::vector<ValueId> used;
  address = l.fit(address, 32); // lane selection uses only low address bits
  auto offset = l.b.binary(Op::BitAnd, address, l.b.constant(32, plan.lanes_wide * lane_bytes - 1));
  auto wide_count = l.b.unary(Op::ZeroExtend, count, 32);
  for (unsigned i = 0; i < plan.lanes_wide; ++i) {
    auto lane_start = l.b.constant(32, i * lane_bytes);
    auto distance = l.b.binary(Op::Subtract, lane_start, offset);
    auto started_inside = l.b.binary(Op::UnsignedLess, distance, wide_count);
    if (!plan.parameters.burst) {
      used.push_back(started_inside);
      continue;
    }
    auto covers_offset = l.b.binary(Op::UnsignedLess, l.b.binary(Op::Subtract, offset, lane_start),
                                    l.b.constant(32, lane_bytes));
    used.push_back(l.either(started_inside, covers_offset));
  }
  return used;
}
// Whether a run ends at its word's last byte: every bit of its reach set.
ValueId all_ones(logic::Logic &l, ValueId end) { return l.eq(end, mask(l.width(end))); }
// Bitwise difference (exclusive or) of two equal-width values.
ValueId differ(logic::Logic &l, ValueId a, ValueId c) {
  return l.both(l.either(a, c), l.b.unary(Op::BitNot, l.both(a, c), l.width(a)));
}
// The last byte of a run's first beat in a word of 2^bits bytes: its address with the beat's
// low bits set (address | 2^size - 1), modulo the word.
ValueId first_beat_last(logic::Logic &l, ValueId address, ValueId size, unsigned bits) {
  ValueId out{};
  for (unsigned i = 0; i < bits; ++i) {
    const auto bit = l.either(l.b.slice(address, i, 1), l.below(l.num(3, i), size));
    out = i ? l.b.concat(bit, out) : bit;
  }
  return out;
}
// A run's reach: a response's field, a request's from its extent (width_adapter.hpp request runs).
ValueId reach_of(logic::Logic &l, const WidthAdapterParameters &p, const Fragment &f,
                 unsigned bits) {
  if (p.response)
    return l.b.slice(f.count, 0, bits);
  const auto first = first_beat_last(l, f.control[0], f.control[control_index(p, "size")], bits);
  return differ(l, first, f.count);
}
// The extent of a run of `reach` at `address`: what its reach adds to its first beat's.
ValueId extent_of(logic::Logic &l, const WidthAdapterParameters &p,
                  const std::vector<ValueId> &control, ValueId reach) {
  if (p.response)
    return reach;
  const auto first =
      first_beat_last(l, control[0], control[control_index(p, "size")], l.width(reach));
  return differ(l, first, reach);
}
ValueId any_used(logic::Logic &l, const std::vector<ValueId> &used, unsigned begin, unsigned end) {
  const auto mid = (begin + end) / 2;
  return end - begin == 1 ? used[begin]
                          : l.either(any_used(l, used, begin, mid), any_used(l, used, mid, end));
}
Fragment input_fragment(logic::Logic &l, bool response, unsigned source_bits,
                        const std::optional<BurstProfile> &burst, const std::string &side,
                        unsigned lanes) {
  auto input = [&](const std::string &signal, unsigned width) {
    return l.b.input("nl_" + side + "_" + signal, width, side, signal);
  };
  Fragment f{};
  f.valid = input("valid", 1);
  f.first = input("first", 1);
  f.last = input("last", 1);
  if (response && burst && burst->segments)
    f.cut = input("cut", 1);
  f.error = input("error", 1);
  for (const auto &[signal, width] : control_fields(response, source_bits, burst))
    f.control.push_back(input(signal, width));
  if (burst && !response) {
    f.count = input("extent", reach_bits(lanes * lane_bytes));
    f.ends = input("ends", 1);
    f.wrap_end = input("wrap_end", 1);
  } else if (burst) {
    f.count = input("reach", reach_bits(lanes * lane_bytes));
    f.wrap_end = burst->response_runs ? input("wrap_end", 1) : l.bit(1);
  } else {
    f.count = input("count", occupancy_bits_of(burst));
  }
  for (unsigned i = 0; i < lanes; ++i) {
    auto data = input("data" + std::to_string(i), 32);
    if (burst && burst->data_user_bits_per_byte)
      data = l.b.concat(input("data_user" + std::to_string(i), burst->data_user_bits_per_byte * 4),
                        data);
    f.data.push_back(data);
    if (!response)
      f.be.push_back(input("be" + std::to_string(i), 4));
  }
  return f;
}
void output_fragment(logic::Logic &l, bool response, const std::string &side, const Fragment &f,
                     unsigned source_bits, const std::optional<BurstProfile> &burst) {
  auto output = [&](const std::string &signal, ValueId value) {
    l.b.output("nl_" + side + "_" + signal, value, side, signal);
  };
  output("valid", f.valid);
  output("first", f.first);
  output("last", f.last);
  if (f.cut)
    output("cut", *f.cut);
  output("error", f.error);
  const auto fields = control_fields(response, source_bits, burst);
  for (std::size_t i = 0; i < fields.size(); ++i)
    output(fields[i].first, f.control[i]);
  if (burst && !response) {
    output("extent", f.count);
    output("ends", f.ends);
    output("wrap_end", f.wrap_end);
  } else if (burst) {
    output("reach", f.count);
    if (burst->response_runs)
      output("wrap_end", f.wrap_end);
  } else {
    output("count", f.count);
  }
  for (std::size_t i = 0; i < f.data.size(); ++i) {
    output("data" + std::to_string(i),
           burst && burst->data_user_bits_per_byte ? l.b.slice(f.data[i], 0, 32) : f.data[i]);
    if (burst && burst->data_user_bits_per_byte)
      output("data_user" + std::to_string(i),
             l.b.slice(f.data[i], 32, burst->data_user_bits_per_byte * 4));
    if (!response)
      output("be" + std::to_string(i), f.be[i]);
  }
}
std::string module_name(const WidthAdapterPlan &plan, bool serializer) {
  return std::string(plan.parameters.response ? "nocloom_response_" : "nocloom_request_") +
         (serializer ? "serializer_" : "deserializer_") +
         std::to_string(plan.parameters.wide_word_bytes) + "x" +
         std::to_string(plan.parameters.narrow_word_bytes);
}
} // namespace

Result<WidthAdapterPlan> plan_width_adapter(const WidthAdapterParameters &parameters) {
  std::vector<Diagnostic> errors;
  if (parameters.burst &&
      (parameters.burst->address_bits < 12 || parameters.burst->address_bits > 48))
    errors.push_back(adapter_error("NCL_WIDTH_ADAPTER_ADDRESS", "width_adapter",
                                   "Burst transport requires 12..48 address bits"));
  const bool occupancy_ok = !parameters.burst || (parameters.burst->occupancy_bits >= 6 &&
                                                  parameters.burst->occupancy_bits <= 8);
  if (!occupancy_ok)
    errors.push_back(adapter_error("NCL_WIDTH_ADAPTER_WORD", "width_adapter",
                                   "Burst occupancy requires 6..8 bits"));
  const unsigned top = occupancy_ok ? ladder_top_bytes(parameters.burst) : 32;
  auto word_ok = [&](unsigned bytes) {
    return bytes >= lane_bytes && bytes <= top && std::has_single_bit(bytes);
  };
  const auto word_message =
      "Transport word must be a power of two between 4 and " + std::to_string(top) + " bytes";
  if (!word_ok(parameters.wide_word_bytes))
    errors.push_back(
        adapter_error("NCL_WIDTH_ADAPTER_WORD", "width_adapter.wide_word_bytes", word_message));
  if (!word_ok(parameters.narrow_word_bytes))
    errors.push_back(
        adapter_error("NCL_WIDTH_ADAPTER_WORD", "width_adapter.narrow_word_bytes", word_message));
  if (parameters.merge_gate && (!runs(parameters) || parameters.response))
    errors.push_back(adapter_error("NCL_WIDTH_ADAPTER_MERGE", "width_adapter.merge_gate",
                                   "Only burst request runs have a merge gate"));
  if (!errors.empty())
    return Result<WidthAdapterPlan>::failure(std::move(errors));
  if (parameters.wide_word_bytes < parameters.narrow_word_bytes)
    return Result<WidthAdapterPlan>::failure(
        {adapter_error("NCL_WIDTH_ADAPTER_RATIO", "width_adapter",
                       "The wide side must not be narrower than the narrow side")});
  const bool pass_through = parameters.wide_word_bytes == parameters.narrow_word_bytes;
  // A burst response assembler takes any ratio at once; its serializer stays 2:1 (lowering).
  if (!pass_through && !(parameters.response && parameters.burst) &&
      parameters.wide_word_bytes != 2 * parameters.narrow_word_bytes)
    return Result<WidthAdapterPlan>::failure(
        {adapter_error("NCL_WIDTH_ADAPTER_RATIO", "width_adapter",
                       "Only 2:1 serialization hops are supported; derive the intermediate "
                       "ladder levels instead of skipping them")});
  WidthAdapterPlan plan;
  plan.parameters = parameters;
  plan.lanes_wide = parameters.wide_word_bytes / lane_bytes;
  plan.lanes_narrow = parameters.narrow_word_bytes / lane_bytes;
  plan.count_bits = occupancy_bits_of(parameters.burst);
  plan.pass_through = pass_through;
  return Result<WidthAdapterPlan>::success(plan);
}

std::vector<std::pair<std::string, unsigned>> fragment_fields(bool response, unsigned word_bytes,
                                                              unsigned source_bits,
                                                              std::optional<BurstProfile> burst) {
  auto fields = control_fields(response, source_bits, burst);
  if (burst && !response) {
    fields.emplace_back("extent", reach_bits(word_bytes));
    fields.emplace_back("ends", 1);
    fields.emplace_back("wrap_end", 1);
  } else if (burst) {
    fields.emplace_back("reach", reach_bits(word_bytes));
    if (burst->response_runs)
      fields.emplace_back("wrap_end", 1);
  } else {
    fields.emplace_back("count", occupancy_bits_of(burst));
  }
  fields.emplace_back("error", 1);
  fields.emplace_back("first", 1);
  fields.emplace_back("last", 1);
  if (burst && response && burst->segments)
    fields.emplace_back("cut", 1);
  for (unsigned i = 0; i < word_bytes / lane_bytes; ++i) {
    fields.emplace_back("data" + std::to_string(i), 32);
    if (burst && burst->data_user_bits_per_byte)
      fields.emplace_back("data_user" + std::to_string(i), burst->data_user_bits_per_byte * 4);
    if (!response)
      fields.emplace_back("be" + std::to_string(i), 4);
  }
  return fields;
}

Result<NodeHardware> lower_width_serializer(const WidthAdapterParameters &parameters) {
  auto planned = checked_plan(parameters);
  if (!planned)
    return Result<NodeHardware>::failure(planned.diagnostics());
  const auto &plan = planned.value();
  if (plan.lanes_wide != 2 * plan.lanes_narrow)
    return Result<NodeHardware>::failure(
        {adapter_error("NCL_WIDTH_ADAPTER_RATIO", "width_adapter",
                       "Only 2:1 serialization hops are supported; derive the intermediate "
                       "ladder levels instead of skipping them")});
  logic::Logic l;
  Fragment wide = input_fragment(l, plan.parameters.response, plan.parameters.source_bits,
                                 plan.parameters.burst, "wide", plan.lanes_wide);
  Fragment narrow;
  narrow.ready = l.b.input("nl_narrow_ready", 1, "narrow", "ready");
  // A request run (lanes outside it are zero from the source on) uses the low half when it
  // starts there and the high half when it starts above it or ends beyond the boundary (the top
  // bit of its reach).
  std::vector<ValueId> used;
  ValueId low_used{}, high_used{};
  const unsigned k = reach_bits(plan.lanes_narrow * lane_bytes);
  ValueId reach{};
  if (runs(plan.parameters)) {
    reach = reach_of(l, plan.parameters, wide, k + 1);
    low_used = l.inv(l.b.slice(wide.control[0], k, 1));
    high_used = l.either(l.inv(low_used), l.b.slice(reach, k, 1));
  } else {
    used = used_lanes(l, plan, wide.control[0], wide.count);
    low_used = any_used(l, used, 0, plan.lanes_narrow);
    high_used = any_used(l, used, plan.lanes_narrow, plan.lanes_wide);
  }
  low_used = l.bind("ser.low_used", low_used);
  high_used = l.bind("ser.high_used", high_used);
  // The only occupation state: whether the first of at most two sub-fragments
  // of the held wide fragment has been accepted.
  auto started = l.state("ser.started", 1);
  auto group = l.bind("ser.group", l.either(started, l.inv(low_used)));
  auto is_last = l.bind("ser.is_last", l.b.binary(Op::Equal, group, high_used));
  narrow.valid = l.both(l.reset, wide.valid);
  auto accept = l.bind("ser.accept", l.both(narrow.valid, narrow.ready));
  wide.ready = l.both(l.reset, l.both(is_last, narrow.ready));
  l.b.update(started, l.b.select(accept, l.b.select(is_last, l.bit(0), l.bit(1)), started));
  l.bind("ser.wide_accept", l.both(wide.valid, wide.ready));
  l.bind("ser.hold", l.both(wide.valid, l.inv(wide.ready)));
  narrow.first = l.both(wide.first, l.inv(started));
  narrow.last = l.both(wide.last, is_last);
  if (wide.cut)
    narrow.cut = l.both(*wide.cut, is_last);
  narrow.error = wide.error;
  narrow.control = wide.control;
  narrow.count = wide.count;
  if (runs(plan.parameters)) {
    // A run over both halves leaves as two runs: the low part up to the half boundary (it ends at
    // the narrow word's last byte), then the rest from the boundary (the reach's low bits).
    const auto low_end = l.b.slice(reach, 0, k);
    const auto past = l.both(l.b.slice(reach, k, 1), l.inv(group));
    const auto narrow_reach = l.either(low_end, l.pick(past, l.num(k, mask(k)), l.num(k, 0)));
    if (!plan.parameters.response)
      narrow.ends = all_ones(l, narrow_reach);
    // The low part's last byte is no WRAP container's last: the run went on past it.
    narrow.wrap_end = l.both(wide.wrap_end, l.inv(past));
    const unsigned half = plan.lanes_narrow * lane_bytes;
    auto &address = narrow.control[0];
    const auto aw = l.width(address);
    const auto split = l.bind("ser.split", l.both(low_used, high_used));
    // A started request is the held high half: the first acceptance could not release its
    // wide fragment, so its address and reach stay unchanged until the high half leaves.
    const auto second = plan.parameters.response ? l.both(split, group) : l.both(low_used, started);
    address = l.pick(
        second,
        l.either(l.both(address, l.num(aw, ~std::uint64_t{half - 1} & mask(aw))), l.num(aw, half)),
        address);
    narrow.count = extent_of(l, plan.parameters, narrow.control, narrow_reach);
  }
  const auto zero_data = l.b.constant(lane_bits(plan.parameters.burst), 0);
  const auto zero_be = l.b.constant(4, 0);
  for (unsigned j = 0; j < plan.lanes_narrow; ++j) {
    const unsigned hi = plan.lanes_narrow + j;
    auto data = l.b.select(group, wide.data[hi], wide.data[j]);
    if (j == 0)
      l.bind("ser.slot0.group_data", data);
    auto be = plan.parameters.response ? ValueId{} : l.b.select(group, wide.be[hi], wide.be[j]);
    if (runs(plan.parameters)) {
      narrow.data.push_back(data);
      narrow.be.push_back(be);
      continue;
    }
    auto slot_used = l.b.select(group, used[hi], used[j]);
    l.bind("ser.slot" + std::to_string(j) + ".group_used", slot_used);
    narrow.data.push_back(l.b.select(slot_used, data, zero_data));
    if (!plan.parameters.response)
      narrow.be.push_back(l.b.select(slot_used, be, zero_be));
  }
  output_fragment(l, plan.parameters.response, "narrow", narrow, plan.parameters.source_bits,
                  plan.parameters.burst);
  l.b.output("nl_wide_ready", wide.ready, "wide", "ready");
  return l.finish(module_name(plan, true));
}

namespace {
// The response assembler (message.hpp point 18): the narrow words of a run enter their place in
// the wide word, the last leaves with the wide word it completes. All words of a run share one
// result and error. A word that differs from the stored words waits, and the stored words leave
// first as a run that ends below it. Readiness reads the markers, the word's position and that
// difference. A fragment of a run ends it, so its control (the same within a packet) passes
// through. Only the start, the shared outcome, every word's exclusive success and the words below
// the top are stored.
Result<NodeHardware> lower_response_assembler(const WidthAdapterPlan &plan) {
  logic::Logic l;
  const auto &p = plan.parameters;
  Fragment narrow = input_fragment(l, true, p.source_bits, p.burst, "narrow", plan.lanes_narrow);
  Fragment wide;
  wide.ready = l.b.input("nl_wide_ready", 1, "wide", "ready");
  const unsigned groups = plan.lanes_wide / plan.lanes_narrow;
  const unsigned n = reach_bits(p.narrow_word_bytes), j = reach_bits(groups);
  const auto have = l.state("des.have_low", 1);
  const auto first_saved = l.state("des.first_saved", 1);
  const auto start = l.state("des.control.start", n + j);
  const auto address = narrow.control[0];
  const auto position = l.b.slice(address, n, j);
  const auto result_in = narrow.control[control_index(p, "result")];
  const auto result_reg = l.state("des.control.result", l.width(result_in));
  const auto failed_reg = l.state("des.err_accum", 1);
  // The stored words leave alone when the arriving word answers otherwise. Pieces of one target
  // word answer alike, so a split word still joins again.
  const auto differs = l.either(l.inv(l.b.binary(Op::Equal, result_reg, result_in)),
                                l.inv(l.b.binary(Op::Equal, failed_reg, narrow.error)));
  const auto flush = l.bind("des.flush", l.both(have, differs));
  auto ends = l.either(narrow.last, l.either(narrow.wrap_end, all_ones(l, position)));
  if (narrow.cut)
    ends = l.either(ends, *narrow.cut);
  ends = l.bind("des.completes", ends);
  wide.valid = l.both(l.reset, l.both(narrow.valid, l.either(ends, flush)));
  narrow.ready =
      l.both(l.reset, l.b.select(flush, l.bit(0), l.b.select(ends, wide.ready, l.bit(1))));
  const auto store =
      l.bind("des.low_accept", l.both(narrow.valid, l.both(narrow.ready, l.inv(ends))));
  const auto leave = l.bind("des.wide_accept", l.both(wide.valid, wide.ready));
  l.bind("des.hold", l.both(narrow.valid, l.inv(narrow.ready)));
  l.b.update(have, l.b.select(store, l.bit(1), l.b.select(leave, l.bit(0), have)));
  const auto stored_first = l.both(store, l.inv(have));
  const auto address_width = l.width(address);
  l.b.update(start, l.b.select(stored_first, l.b.slice(address, 0, n + j), start));
  l.b.update(first_saved, l.b.select(stored_first, narrow.first, first_saved));
  // A run answers its words' shared outcome. Its exclusive success needs every word's. A flushed
  // run leaves without the arriving word.
  l.b.update(result_reg, l.b.select(store, result_in, result_reg));
  l.b.update(failed_reg, l.b.select(store, narrow.error, failed_reg));
  wide.control = narrow.control;
  const auto result = l.b.select(flush, result_reg, result_in);
  wide.control[control_index(p, "result")] = result;
  if (p.burst->exclusive) {
    const auto at = control_index(p, "exclusive_okay");
    const auto reg = l.state("des.control.exclusive_okay", 1);
    const auto all = l.b.select(have, l.b.select(flush, reg, l.both(reg, narrow.control[at])),
                                narrow.control[at]);
    l.b.update(reg, l.b.select(store, all, reg));
    wide.control[at] = all;
  }
  wide.error = l.bind("des.error_merge", l.b.select(flush, failed_reg, narrow.error));
  wide.control[0] = l.b.select(
      have, l.b.concat(l.b.slice(address, n + j, address_width - n - j), start), address);
  // A flushed run ends at the top of the word below the arriving one. Its wrap_end keeps a wider
  // assembler from gathering it with that word.
  const auto below = l.b.slice(l.minus(position, l.num(j, 1)), 0, j);
  wide.count =
      l.b.select(flush, l.b.concat(below, l.num(n, mask(n))), l.b.concat(position, narrow.count));
  wide.first = l.b.select(have, first_saved, narrow.first);
  wide.last = l.both(narrow.last, l.inv(flush));
  if (narrow.cut)
    wide.cut = l.both(*narrow.cut, l.inv(flush));
  wide.wrap_end = l.either(narrow.wrap_end, flush);
  // A failed run answers without data; lanes outside the run are zero.
  auto failed = l.either(wide.error, l.inv(l.eq(result, 0)));
  // The local length descriptor keeps its payload. Native target errors carry no such mark.
  if (p.burst->decode_packets)
    failed =
        l.both(failed, l.inv(decode_packet::marked(l, p.burst->decode_flag, result, wide.error)));
  const auto zero_data = l.b.constant(lane_bits(plan.parameters.burst), 0);
  const auto first_group = l.b.slice(start, n, j);
  for (unsigned g = 0; g < groups; ++g) {
    const auto here = l.eq(position, g);
    const auto shown = l.both(here, l.inv(flush));
    const auto kept = g + 1 < groups ? l.both(have, l.both(l.inv(l.below(l.num(j, g), first_group)),
                                                           l.below(l.num(j, g), position)))
                                     : l.bit(0);
    for (unsigned i = 0; i < plan.lanes_narrow; ++i) {
      ValueId stored = zero_data;
      if (g + 1 < groups) {
        const auto lane = l.state("des.lane_data" + std::to_string(g * plan.lanes_narrow + i),
                                  lane_bits(p.burst));
        l.b.update(lane, l.b.select(l.both(store, here), narrow.data[i], lane));
        stored = l.b.select(kept, lane, zero_data);
      }
      const auto payload = l.b.select(shown, narrow.data[i], stored);
      const auto failed_payload =
          p.burst->data_user_bits_per_byte
              ? l.b.concat(l.b.slice(payload, 32, lane_bits(p.burst) - 32), l.num(32, 0))
              : zero_data;
      wide.data.push_back(l.b.select(failed, failed_payload, payload));
    }
  }
  output_fragment(l, true, "wide", wide, p.source_bits, p.burst);
  l.b.output("nl_narrow_ready", narrow.ready, "narrow", "ready");
  return l.finish(module_name(plan, false));
}
} // namespace

Result<NodeHardware> lower_width_deserializer(const WidthAdapterParameters &parameters) {
  auto planned = checked_plan(parameters);
  if (!planned)
    return Result<NodeHardware>::failure(planned.diagnostics());
  const auto &plan = planned.value();
  if (parameters.response && parameters.burst)
    return lower_response_assembler(plan);
  logic::Logic l;
  Fragment narrow = input_fragment(l, plan.parameters.response, plan.parameters.source_bits,
                                   plan.parameters.burst, "narrow", plan.lanes_narrow);
  const auto control =
      control_fields(plan.parameters.response, plan.parameters.source_bits, plan.parameters.burst);
  Fragment wide;
  wide.ready = l.b.input("nl_wide_ready", 1, "wide", "ready");
  // Gather state: one stored narrow fragment (the first group) plus the
  // captured control, first marker and accumulated error.
  auto have_low = l.state("des.have_low", 1);
  auto err_accum = l.state("des.err_accum", 1);
  auto first_saved = l.state("des.first_saved", 1);
  std::vector<ValueId> control_reg;
  for (std::size_t i = 0; i < control.size(); ++i)
    control_reg.push_back(l.state("des.control." + control[i].first, control[i].second));
  // A run's end is the completing run's: the deserializer stores no count of request runs.
  const auto count_reg =
      runs(plan.parameters) ? ValueId{} : l.state("des.control.count", plan.count_bits);
  std::vector<ValueId> lane_data, lane_be;
  for (unsigned j = 0; j < plan.lanes_narrow; ++j) {
    lane_data.push_back(l.state("des.lane_data" + std::to_string(j), lane_bits(parameters.burst)));
    if (!plan.parameters.response)
      lane_be.push_back(l.state("des.lane_be" + std::to_string(j), 4));
  }
  // Derivations use the captured control once a first fragment is stored; a joined run ends
  // where the run that completes it ends.
  auto address_eff = l.b.select(have_low, control_reg[0], narrow.control[0]);
  auto count_eff =
      runs(plan.parameters) ? narrow.count : l.b.select(have_low, count_reg, narrow.count);
  // A request run's lanes outside it are zero already (the source clears them); others mask.
  const auto used =
      runs(plan.parameters) ? std::vector<ValueId>{} : used_lanes(l, plan, address_eff, count_eff);
  ValueId this_group{}, completes{};
  if (runs(plan.parameters)) {
    // A run in the low half that ends at the boundary and is not its packet's last waits for the
    // run from the boundary: the rest of a split beat, or (while merging) the next whole beats.
    const unsigned k = reach_bits(plan.lanes_narrow * lane_bytes);
    const auto a = narrow.control[0];
    const auto at = [&](const char *name) {
      return narrow.control[control_index(plan.parameters, name)];
    };
    const auto in_low = l.inv(l.b.slice(a, k, 1));
    // It reaches the boundary: it ends at its word's last byte.
    const auto narrow_reach = reach_of(l, plan.parameters, narrow, k);
    const auto ends = all_ones(l, narrow_reach);
    const auto size = at("size"), burst = at("burst");
    // Part of a beat: the beats (aligned to their size) are wider than the half.
    const auto partial = l.below(l.num(3, k), size);
    // A WRAP continues past the boundary unless its container ends there (wrap_end: the run's
    // last beat is its container's last).
    auto whole = l.both(l.inv(l.eq(burst, 0)), l.inv(l.both(l.eq(burst, 2), narrow.wrap_end)));
    if (plan.parameters.merge_gate)
      whole = l.both(whole, l.b.input("nl_narrow_merge", 1, "narrow", "merge"));
    const auto waits =
        l.bind("des.waits",
               l.both(l.both(in_low, ends), l.both(l.inv(narrow.last), l.either(partial, whole))));
    this_group = l.bind("des.this_group", l.either(have_low, l.inv(in_low)));
    completes = l.bind("des.completes", l.either(have_low, l.inv(waits)));
  } else {
    auto low_used = l.bind("des.low_used", any_used(l, used, 0, plan.lanes_narrow));
    auto high_used = l.bind("des.high_used", any_used(l, used, plan.lanes_narrow, plan.lanes_wide));
    this_group = l.bind("des.this_group", l.either(have_low, l.inv(low_used)));
    completes = l.bind("des.completes", l.b.binary(Op::Equal, this_group, high_used));
  }
  wide.valid = l.both(l.reset, l.both(narrow.valid, completes));
  narrow.ready = l.both(l.reset, l.b.select(completes, wide.ready, l.bit(1)));
  auto low_accept =
      l.bind("des.low_accept", l.both(narrow.valid, l.both(narrow.ready, l.inv(completes))));
  auto wide_accept = l.bind("des.wide_accept", l.both(wide.valid, wide.ready));
  l.bind("des.hold", l.both(narrow.valid, l.inv(narrow.ready)));
  l.b.update(have_low,
             l.b.select(low_accept, l.bit(1), l.b.select(wide_accept, l.bit(0), have_low)));
  for (std::size_t i = 0; i < control.size(); ++i)
    l.b.update(control_reg[i], l.b.select(low_accept, narrow.control[i], control_reg[i]));
  if (!runs(plan.parameters))
    l.b.update(count_reg, l.b.select(low_accept, narrow.count, count_reg));
  l.b.update(err_accum, l.b.select(low_accept, narrow.error, err_accum));
  l.b.update(first_saved, l.b.select(low_accept, narrow.first, first_saved));
  for (unsigned j = 0; j < plan.lanes_narrow; ++j) {
    l.b.update(lane_data[j], l.b.select(low_accept, narrow.data[j], lane_data[j]));
    if (!plan.parameters.response)
      l.b.update(lane_be[j], l.b.select(low_accept, narrow.be[j], lane_be[j]));
  }
  const auto zero_data = l.b.constant(lane_bits(plan.parameters.burst), 0);
  const auto zero_be = l.b.constant(4, 0);
  auto from_current = l.inv(this_group);
  // Only used lanes leave the gather; slots and stored lanes outside the
  // occupancy range emerge as zero regardless of the padding on the wire.
  for (unsigned i = 0; i < plan.lanes_wide; ++i) {
    const bool high_half = i >= plan.lanes_narrow;
    const unsigned slot = high_half ? i - plan.lanes_narrow : i;
    auto current = high_half ? this_group : from_current;
    auto stored = high_half ? zero_data : l.b.select(have_low, lane_data[i], zero_data);
    auto raw = l.b.select(current, narrow.data[slot], stored);
    wide.data.push_back(used.empty() ? raw : l.b.select(used[i], raw, zero_data));
    if (!plan.parameters.response) {
      auto stored_be = high_half ? zero_be : l.b.select(have_low, lane_be[i], zero_be);
      auto raw_be = l.b.select(current, narrow.be[slot], stored_be);
      wide.be.push_back(used.empty() ? raw_be : l.b.select(used[i], raw_be, zero_be));
    }
  }
  for (std::size_t i = 0; i < control.size(); ++i)
    wide.control.push_back(l.b.select(have_low, control_reg[i], narrow.control[i]));
  wide.count = count_eff;
  if (runs(plan.parameters)) {
    // The completing address names the half. Combine first-beat terms before its extent.
    const unsigned k = reach_bits(plan.lanes_narrow * lane_bytes);
    const auto reach =
        l.b.concat(l.b.slice(narrow.control[0], k, 1), reach_of(l, plan.parameters, narrow, k));
    wide.count = reach;
    if (!plan.parameters.response) {
      const auto size = control_index(plan.parameters, "size");
      const auto first = first_beat_last(l, wide.control[0], wide.control[size], k + 1);
      const auto adjust = differ(l, l.b.slice(first, 0, k),
                                 first_beat_last(l, narrow.control[0], narrow.control[size], k));
      wide.count = l.b.concat(differ(l, l.b.slice(first, k, 1), l.b.slice(narrow.control[0], k, 1)),
                              differ(l, narrow.count, adjust));
    }
    wide.ends = all_ones(l, reach);
    wide.wrap_end = narrow.wrap_end;
  }
  auto error_or = l.bind("des.error_or", l.either(err_accum, narrow.error));
  wide.error = l.bind("des.error_merge", l.b.select(have_low, error_or, narrow.error));
  wide.first = l.b.select(have_low, first_saved, narrow.first);
  wide.last = narrow.last;
  wide.cut = narrow.cut;
  output_fragment(l, plan.parameters.response, "wide", wide, plan.parameters.source_bits,
                  plan.parameters.burst);
  l.b.output("nl_narrow_ready", narrow.ready, "narrow", "ready");
  return l.finish(module_name(plan, false));
}
} // namespace nocloom::detail
