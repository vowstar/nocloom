#include "core/width_adapter.hpp"
#include "core/ir_cone.hpp"
#include <algorithm>
#include <catch2/catch_message.hpp>
#include <catch2/catch_test_macros.hpp>
#include <map>
#include <set>
#include <tuple>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
WidthAdapterParameters hop(unsigned wide, unsigned narrow, bool response = false,
                           unsigned source_bits = 0) {
  return {wide, narrow, response, source_bits, std::nullopt};
}
void bound_states_zeroed(const NodeHardware &h) {
  std::set<std::size_t> actual;
  for (const auto &[name, id] : h.bindings)
    if (h.ir.values.at(id.index).op == Op::State)
      actual.insert(id.index);
  REQUIRE(actual.size() == h.ir.states.size());
  for (const auto &s : h.ir.states) {
    REQUIRE(actual.contains(s.output.index));
    REQUIRE(s.reset.has_value());
    REQUIRE(s.reset->value == 0);
  }
}
const Port *find_port(const HardwareIR &ir, const std::string &name) {
  auto it = std::ranges::find_if(ir.ports, [&](const auto &p) { return p.name == name; });
  return it != ir.ports.end() ? &*it : nullptr;
}
bool has_diagnostic(const std::vector<Diagnostic> &diagnostics, const std::string &code) {
  return std::ranges::any_of(diagnostics, [&](const auto &d) { return d.code == code; });
}
} // namespace

TEST_CASE("Width adapter plans derive from the word width difference", "[width-adapter]") {
  {
    auto plan = plan_width_adapter(hop(32, 16));
    REQUIRE(plan);
    REQUIRE(!plan.value().pass_through);
    REQUIRE(plan.value().lanes_wide == 8);
    REQUIRE(plan.value().lanes_narrow == 4);
    REQUIRE(plan.value().count_bits == 6);
  }
  // The remaining ladder hops share the same parameterization.
  {
    auto plan = plan_width_adapter(hop(16, 8));
    REQUIRE(plan);
    REQUIRE(!plan.value().pass_through);
    REQUIRE(plan.value().lanes_wide == 4);
    REQUIRE(plan.value().lanes_narrow == 2);
    REQUIRE(plan.value().count_bits == 6);
  }
  {
    auto plan = plan_width_adapter(hop(8, 4));
    REQUIRE(plan);
    REQUIRE(!plan.value().pass_through);
    REQUIRE(plan.value().lanes_wide == 2);
    REQUIRE(plan.value().lanes_narrow == 1);
    REQUIRE(plan.value().count_bits == 6);
  }
  // Equal word widths derive zero instances: a pure pass-through, no module.
  for (unsigned bytes : {4u, 8u, 16u, 32u}) {
    auto plan = plan_width_adapter(hop(bytes, bytes));
    REQUIRE(plan);
    REQUIRE(plan.value().pass_through);
    REQUIRE(plan.value().lanes_wide == plan.value().lanes_narrow);
    auto serializer = lower_width_serializer(hop(bytes, bytes));
    REQUIRE(!serializer);
    REQUIRE(has_diagnostic(serializer.diagnostics(), "NCL_WIDTH_ADAPTER_PASS_THROUGH"));
    auto deserializer = lower_width_deserializer(hop(bytes, bytes));
    REQUIRE(!deserializer);
    REQUIRE(has_diagnostic(deserializer.diagnostics(), "NCL_WIDTH_ADAPTER_PASS_THROUGH"));
  }
  // Only 2:1 hops exist; wider jumps and malformed shapes are diagnostics.
  for (const auto &[wide, narrow, code] :
       std::vector<std::tuple<unsigned, unsigned, std::string>>{{32, 8, "NCL_WIDTH_ADAPTER_RATIO"},
                                                                {32, 4, "NCL_WIDTH_ADAPTER_RATIO"},
                                                                {16, 4, "NCL_WIDTH_ADAPTER_RATIO"},
                                                                {16, 32, "NCL_WIDTH_ADAPTER_RATIO"},
                                                                {24, 12, "NCL_WIDTH_ADAPTER_WORD"},
                                                                {64, 32, "NCL_WIDTH_ADAPTER_WORD"},
                                                                {2, 1, "NCL_WIDTH_ADAPTER_WORD"},
                                                                {0, 0, "NCL_WIDTH_ADAPTER_WORD"}}) {
    auto plan = plan_width_adapter(hop(wide, narrow));
    INFO("hop " + std::to_string(wide) + " vs " + std::to_string(narrow));
    REQUIRE(!plan);
    REQUIRE(has_diagnostic(plan.diagnostics(), code));
    REQUIRE(!lower_width_serializer(hop(wide, narrow)));
    REQUIRE(!lower_width_deserializer(hop(wide, narrow)));
  }
}

TEST_CASE("Request serializer 32x16 structure and observation bindings", "[width-adapter]") {
  auto result = lower_width_serializer(hop(32, 16));
  REQUIRE(result);
  const auto &h = result.value();
  REQUIRE(verify_structure(h.ir));
  REQUIRE(h.ir.name == "nocloom_request_serializer_32x16");
  bound_states_zeroed(h);
  // A single one-bit occupation register: the first sub-fragment was sent.
  REQUIRE(h.ir.states.size() == 1);
  REQUIRE(h.bindings.contains("ser.started"));
  for (const auto *key : {"ser.low_used", "ser.high_used", "ser.group", "ser.is_last", "ser.accept",
                          "ser.wide_accept", "ser.hold", "ser.slot0.group_data",
                          "ser.slot0.group_used", "ser.slot3.group_used"})
    REQUIRE(h.bindings.contains(key));
  const auto *clock = find_port(h.ir, "nl_clk");
  const auto *reset = find_port(h.ir, "nl_reset_n");
  REQUIRE(clock != nullptr);
  REQUIRE(reset != nullptr);
  REQUIRE(clock->endpoint == "domain:main");
  REQUIRE(reset->signal == "reset_n");
  const auto *data7 = find_port(h.ir, "nl_wide_data7");
  REQUIRE(data7 != nullptr);
  REQUIRE(data7->input);
  REQUIRE(data7->width == 32);
  REQUIRE(data7->endpoint == "wide");
  const auto *be3 = find_port(h.ir, "nl_narrow_be3");
  REQUIRE(be3 != nullptr);
  REQUIRE(!be3->input);
  REQUIRE(be3->width == 4);
  REQUIRE(find_port(h.ir, "nl_wide_base") == nullptr);
  const auto *count = find_port(h.ir, "nl_wide_count");
  REQUIRE(count != nullptr);
  REQUIRE(count->width == 6);
  for (const auto *name :
       {"nl_wide_valid", "nl_wide_first", "nl_wide_last", "nl_wide_error", "nl_wide_address",
        "nl_wide_write", "nl_wide_protection", "nl_narrow_valid", "nl_narrow_ready",
        "nl_narrow_first", "nl_narrow_last", "nl_narrow_error", "nl_narrow_address",
        "nl_narrow_count", "nl_narrow_data0", "nl_narrow_be0", "nl_wide_ready"})
    REQUIRE(find_port(h.ir, name) != nullptr);
  REQUIRE(find_port(h.ir, "nl_wide_result") == nullptr);
  REQUIRE(find_port(h.ir, "nl_wide_source") == nullptr);
  REQUIRE(find_port(h.ir, "nl_narrow_data4") == nullptr);
  // Deterministic lowering: repeated generation is byte-identical.
  auto second = lower_width_serializer(hop(32, 16));
  REQUIRE(second);
  REQUIRE(emit_verilog(h.ir) == emit_verilog(second.value().ir));
}

TEST_CASE("Request deserializer 32x16 structure and gather storage", "[width-adapter]") {
  auto result = lower_width_deserializer(hop(32, 16));
  REQUIRE(result);
  const auto &h = result.value();
  REQUIRE(verify_structure(h.ir));
  REQUIRE(h.ir.name == "nocloom_request_deserializer_32x16");
  bound_states_zeroed(h);
  // Gather state: have_low, error accumulator, first marker, four control
  // registers (address/write/protection/count) and one stored narrow
  // fragment of four lanes with data and byte enables.
  REQUIRE(h.ir.states.size() == 15);
  for (const auto *key :
       {"des.have_low",      "des.err_accum",          "des.first_saved",   "des.control.address",
        "des.control.write", "des.control.protection", "des.control.count", "des.lane_data0",
        "des.lane_data3",    "des.lane_be0",           "des.lane_be3",      "des.low_used",
        "des.high_used",     "des.this_group",         "des.completes",     "des.error_or",
        "des.error_merge",   "des.low_accept",         "des.wide_accept",   "des.hold"})
    REQUIRE(h.bindings.contains(key));
  for (const auto *name :
       {"nl_narrow_valid", "nl_narrow_ready", "nl_narrow_first", "nl_narrow_last",
        "nl_narrow_error", "nl_narrow_address", "nl_narrow_data3", "nl_narrow_be3", "nl_wide_valid",
        "nl_wide_ready", "nl_wide_first", "nl_wide_last", "nl_wide_error", "nl_wide_address",
        "nl_wide_data7", "nl_wide_be7", "nl_wide_count"})
    REQUIRE(find_port(h.ir, name) != nullptr);
  REQUIRE(find_port(h.ir, "nl_narrow_data4") == nullptr);
  auto second = lower_width_deserializer(hop(32, 16));
  REQUIRE(second);
  REQUIRE(emit_verilog(h.ir) == emit_verilog(second.value().ir));
}

TEST_CASE("Response adapters carry result and data-only lanes", "[width-adapter]") {
  auto serializer = lower_width_serializer(hop(32, 16, true));
  REQUIRE(serializer);
  REQUIRE(serializer.value().ir.name == "nocloom_response_serializer_32x16");
  REQUIRE(find_port(serializer.value().ir, "nl_wide_result") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_address") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_be0") == nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_narrow_be0") == nullptr);
  bound_states_zeroed(serializer.value());
  auto deserializer = lower_width_deserializer(hop(32, 16, true));
  REQUIRE(deserializer);
  REQUIRE(find_port(deserializer.value().ir, "nl_wide_result") != nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_wide_be3") == nullptr);
  // Gather state: have_low, error, first, three control registers
  // (address/result/count) and four data-only lane registers.
  REQUIRE(deserializer.value().ir.states.size() == 10);
  REQUIRE(deserializer.value().bindings.contains("des.control.result"));
  REQUIRE(deserializer.value().bindings.contains("des.control.address"));
  bound_states_zeroed(deserializer.value());
  REQUIRE(emit_verilog(serializer.value().ir) ==
          emit_verilog(lower_width_serializer(hop(32, 16, true)).value().ir));
}

TEST_CASE("Optional source identity travels in the control bundle", "[width-adapter]") {
  auto serializer = lower_width_serializer(hop(32, 16, false, 2));
  REQUIRE(serializer);
  const auto *source = find_port(serializer.value().ir, "nl_wide_source");
  REQUIRE(source != nullptr);
  REQUIRE(source->width == 2);
  REQUIRE(find_port(serializer.value().ir, "nl_narrow_source") != nullptr);
  auto deserializer = lower_width_deserializer(hop(32, 16, true, 2));
  REQUIRE(deserializer);
  REQUIRE(find_port(deserializer.value().ir, "nl_wide_source") != nullptr);
  REQUIRE(deserializer.value().bindings.contains("des.control.source"));
}

TEST_CASE("Remaining ladder hops share the parameterized lowering", "[width-adapter]") {
  // Structural reservation for the next hops; behavioral evidence this step
  // covers only the 32x16 instance through the RTL bench.
  auto serializer = lower_width_serializer(hop(16, 8));
  REQUIRE(serializer);
  REQUIRE(serializer.value().ir.name == "nocloom_request_serializer_16x8");
  REQUIRE(find_port(serializer.value().ir, "nl_wide_data3") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_data4") == nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_narrow_data1") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_narrow_data2") == nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_base") == nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_count")->width == 6);
  auto deserializer = lower_width_deserializer(hop(8, 4));
  REQUIRE(deserializer);
  REQUIRE(deserializer.value().ir.name == "nocloom_request_deserializer_8x4");
  REQUIRE(find_port(deserializer.value().ir, "nl_wide_data1") != nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_wide_data2") == nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_data0") != nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_be0") != nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_be1") == nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_base") == nullptr);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_count")->width == 6);
  bound_states_zeroed(serializer.value());
  bound_states_zeroed(deserializer.value());
  // Both directions lower deterministically for every remaining hop.
  for (const auto wide_narrow : {std::pair{16u, 8u}, std::pair{8u, 4u}}) {
    for (bool response : {false, true}) {
      const auto parameters = hop(wide_narrow.first, wide_narrow.second, response);
      auto first_serializer = lower_width_serializer(parameters);
      auto second_serializer = lower_width_serializer(parameters);
      REQUIRE(first_serializer);
      REQUIRE(second_serializer);
      REQUIRE(emit_verilog(first_serializer.value().ir) ==
              emit_verilog(second_serializer.value().ir));
      auto first_deserializer = lower_width_deserializer(parameters);
      auto second_deserializer = lower_width_deserializer(parameters);
      REQUIRE(first_deserializer);
      REQUIRE(second_deserializer);
      REQUIRE(emit_verilog(first_deserializer.value().ir) ==
              emit_verilog(second_deserializer.value().ir));
      bound_states_zeroed(first_serializer.value());
      bound_states_zeroed(first_deserializer.value());
    }
  }
}

TEST_CASE("Burst carriage extends the control bundle and widens the occupancy cone",
          "[width-adapter]") {
  const BurstProfile burst{4, 8};
  // The word-message layouts are untouched when no burst profile is given.
  REQUIRE(fragment_fields(false, 8, 2, std::nullopt) == fragment_fields(false, 8, 2));
  // The burst control bundle carries the descriptor in the contract's order.
  const auto req = fragment_fields(false, 8, 2, burst);
  const std::vector<std::string> req_names = {
      "address", "write",  "protection", "length", "size",     "burst", "cache",
      "id",      "source", "extent",     "ends",   "wrap_end", "error", "first",
      "last",    "data0",  "be0",        "data1",  "be1"};
  REQUIRE(req.size() == req_names.size());
  for (std::size_t i = 0; i < req.size(); ++i)
    REQUIRE(req[i].first == req_names[i]);
  REQUIRE(req[3].second == 8); // length carries len_bits
  REQUIRE(req[7].second == 4); // id carries id_bits
  REQUIRE(req[9].second == 3); // a run's extent: a byte offset in the 8-byte word
  // The extent is as wide as the word's byte offset at every ladder level.
  for (const auto &[bytes, bits] : {std::pair{4U, 2U}, std::pair{16U, 4U}, std::pair{32U, 5U}})
    REQUIRE(fragment_fields(false, bytes, 2, burst)[9].second == bits);
  const auto rsp = fragment_fields(true, 8, 2, burst);
  const std::vector<std::string> rsp_names = {"address", "result", "id",   "source", "reach",
                                              "error",   "first",  "last", "data0",  "data1"};
  REQUIRE(rsp.size() == rsp_names.size());
  for (std::size_t i = 0; i < rsp.size(); ++i)
    REQUIRE(rsp[i].first == rsp_names[i]);
  // The burst adapters lower with the descriptor ports on both faces.
  const WidthAdapterParameters ser{8, 4, false, 2, burst};
  auto serializer = lower_width_serializer(ser);
  REQUIRE(serializer);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_length") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_wide_id") != nullptr);
  REQUIRE(find_port(serializer.value().ir, "nl_narrow_cache") != nullptr);
  const WidthAdapterParameters des{8, 4, false, 2, burst};
  auto deserializer = lower_width_deserializer(des);
  REQUIRE(deserializer);
  REQUIRE(find_port(deserializer.value().ir, "nl_narrow_size") != nullptr);
  bound_states_zeroed(serializer.value());
  bound_states_zeroed(deserializer.value());
  // The widened occupancy cone is a strict superset cone (the word rule plus
  // the lane-covering term), present exactly when the burst profile is set.
  const auto burst_text = emit_verilog(serializer.value().ir);
  const auto plain_text = emit_verilog(lower_width_serializer(hop(8, 4, false, 2)).value().ir);
  REQUIRE(burst_text != plain_text); // the burst cone carries the added term
  // Planning discipline is unchanged: equal widths stay a pass-through.
  auto same = plan_width_adapter({4, 4, false, 2, burst});
  REQUIRE(same);
  REQUIRE(same.value().pass_through);
}

namespace {
// Cycle stepper for an adapter whose values are at most 64 bits wide; inputs start at zero.
struct Stepper {
  const HardwareIR &ir;
  std::map<std::size_t, std::uint64_t> state, in;
  explicit Stepper(const HardwareIR &h) : ir(h) {
    for (std::size_t s = 0; s < ir.states.size(); ++s)
      state[s] = ir.states[s].reset ? ir.states[s].reset->value : 0;
    for (std::size_t p = 0; p < ir.ports.size(); ++p)
      in[p] = 0;
    set("nl_reset_n", 1);
  }
  std::size_t index(const std::string &name) const {
    const auto *p = find_port(ir, name);
    REQUIRE(p != nullptr);
    return static_cast<std::size_t>(p - ir.ports.data());
  }
  void set(const std::string &name, std::uint64_t value) { in[index(name)] = value; }
  std::uint64_t out(const std::string &name) {
    const auto at = index(name);
    const auto d =
        std::ranges::find_if(ir.drivers, [&](const auto &x) { return x.port.index == at; });
    REQUIRE(d != ir.drivers.end());
    const auto v = evaluate_cone(ir, d->value.index, state, in);
    REQUIRE(v);
    return *v;
  }
  void tick() {
    std::map<std::size_t, std::uint64_t> next;
    for (std::size_t s = 0; s < ir.states.size(); ++s) {
      const auto v = evaluate_cone(ir, ir.states[s].next.index, state, in);
      REQUIRE(v);
      const auto w = ir.values[ir.states[s].output.index].width;
      next[s] = w >= 64 ? *v : *v & ((std::uint64_t{1} << w) - 1);
    }
    state = std::move(next);
  }
};
// One request run of 4-byte beats on a face of `word` bytes: burst 0 FIXED, 1 INCR, 2 WRAP. Its
// reach is its last byte in the word (clipped to it), carried as its extent (the reach's
// difference from its first beat's last byte), and a WRAP beat at its container's end carries
// wrap_end, as the endpoint framing derives them.
void offer(Stepper &s, const std::string &side, unsigned word, std::uint64_t address,
           std::uint64_t count, std::uint64_t burst, std::uint64_t length, bool first, bool last) {
  const auto reach = std::min<std::uint64_t>(address % word + count, word) - 1;
  bool wrap_end = burst == 2;
  for (unsigned j = 0; j < 8; ++j)
    wrap_end = wrap_end && (!((length >> j) & 1) || ((address >> (2 + j)) & 1));
  s.set("nl_" + side + "_valid", 1);
  s.set("nl_" + side + "_address", address);
  s.set("nl_" + side + "_extent", reach ^ ((address | 3) % word));
  s.set("nl_" + side + "_ends", reach == word - 1);
  s.set("nl_" + side + "_wrap_end", wrap_end);
  s.set("nl_" + side + "_burst", burst);
  s.set("nl_" + side + "_length", length);
  s.set("nl_" + side + "_size", 2);
  s.set("nl_" + side + "_first", first);
  s.set("nl_" + side + "_last", last);
}
// The reach a face drives for 4-byte beats: its extent over its first beat's last byte.
std::uint64_t reach(Stepper &s, const std::string &side, unsigned word) {
  return s.out("nl_" + side + "_extent") ^ ((s.out("nl_" + side + "_address") | 3) % word);
}
} // namespace

TEST_CASE("Request runs join up to the word boundary and split at the half boundary",
          "[width-adapter]") {
  const BurstProfile burst{4, 8};
  SECTION("INCR beats join, a FIXED burst and a closed merge gate do not") {
    for (const auto &[kind, gated, open] :
         {std::tuple{1U, false, true}, std::tuple{0U, false, true}, std::tuple{1U, true, false},
          std::tuple{1U, true, true}}) {
      WidthAdapterParameters des{8, 4, false, 2, burst};
      des.merge_gate = gated;
      const auto h = lower_width_deserializer(des);
      REQUIRE(h);
      Stepper s(h.value().ir);
      s.set("nl_wide_ready", 1);
      if (gated)
        s.set("nl_narrow_merge", open);
      const bool joins = kind == 1 && open;
      offer(s, "narrow", 4, 0x100, 4, kind, 1, true, false);
      s.set("nl_narrow_data0", 0x11111111);
      CHECK(s.out("nl_narrow_ready") == 1);
      CHECK(s.out("nl_wide_valid") == (joins ? 0 : 1));
      s.tick();
      offer(s, "narrow", 4, kind ? 0x104 : 0x100, 4, kind, 1, false, true);
      s.set("nl_narrow_data0", 0x22222222);
      CHECK(s.out("nl_wide_valid") == 1);
      // The completing run's reach in the wide word: the high half's last byte, or the low one's.
      CHECK(reach(s, "wide", 8) == (joins || kind ? 7 : 3));
      CHECK(s.out("nl_wide_extent") == (joins ? 4 : 0)); // a single beat's extent is zero
      CHECK(s.out("nl_wide_ends") == (joins || kind ? 1 : 0));
      CHECK(s.out("nl_wide_address") == (joins || !kind ? 0x100 : 0x104));
      CHECK(s.out("nl_wide_first") == (joins ? 1 : 0));
      CHECK(s.out("nl_wide_data0") == (joins ? 0x11111111 : (kind ? 0 : 0x22222222)));
      CHECK(s.out("nl_wide_data1") == (kind ? 0x22222222 : 0));
    }
  }
  SECTION("The halves of a split beat rejoin even in a FIXED burst") {
    const auto h = lower_width_deserializer({8, 4, false, 2, burst});
    REQUIRE(h);
    Stepper s(h.value().ir);
    s.set("nl_wide_ready", 1);
    offer(s, "narrow", 4, 0x100, 8, 0, 1, true, false); // an 8-byte beat's low half
    s.set("nl_narrow_size", 3);
    CHECK(s.out("nl_wide_valid") == 0);
    CHECK(s.out("nl_narrow_ready") == 1);
  }
  SECTION("A run that stops short of the half boundary leaves at once") {
    const auto h = lower_width_deserializer({16, 8, false, 2, burst});
    REQUIRE(h);
    Stepper s(h.value().ir);
    s.set("nl_wide_ready", 1);
    offer(s, "narrow", 8, 0x100, 4, 1, 3, true, false);
    CHECK(s.out("nl_wide_valid") == 1);
    CHECK(reach(s, "wide", 16) == 3);
    CHECK(s.out("nl_wide_ends") == 0);
  }
  SECTION("A WRAP joins only where its container continues past the boundary") {
    const auto h = lower_width_deserializer({16, 8, false, 2, burst});
    REQUIRE(h);
    for (const auto &[length, joins] : {std::pair{1U, false}, std::pair{3U, true}}) {
      Stepper s(h.value().ir);
      s.set("nl_wide_ready", 1);
      offer(s, "narrow", 8, 0x104, 4, 2, length, true, false);
      CHECK(s.out("nl_wide_valid") == (joins ? 0 : 1));
    }
  }
  SECTION("A run over both halves leaves as its two parts") {
    const auto h = lower_width_serializer({8, 4, false, 2, burst});
    REQUIRE(h);
    Stepper s(h.value().ir);
    s.set("nl_narrow_ready", 1);
    offer(s, "wide", 8, 0x100, 8, 1, 1, true, true);
    s.set("nl_wide_wrap_end", 1);
    CHECK(s.out("nl_narrow_address") == 0x100);
    CHECK(reach(s, "narrow", 4) == 3); // the low part ends at its word's last byte
    CHECK(s.out("nl_narrow_ends") == 1);
    CHECK(s.out("nl_narrow_wrap_end") == 0); // the run goes on past it
    CHECK(s.out("nl_narrow_last") == 0);
    CHECK(s.out("nl_wide_ready") == 0);
    s.tick();
    CHECK(s.out("nl_narrow_address") == 0x104);
    CHECK(reach(s, "narrow", 4) == 3);
    CHECK(s.out("nl_narrow_wrap_end") == 1);
    CHECK(s.out("nl_narrow_last") == 1);
    CHECK(s.out("nl_wide_ready") == 1);
  }
  SECTION("A run in one half keeps its reach in the narrow word") {
    const auto h = lower_width_serializer({16, 8, false, 2, burst});
    REQUIRE(h);
    for (const auto &[address, last_byte] : {std::pair{0x104U, 7U}, std::pair{0x10cU, 7U},
                                             std::pair{0x100U, 3U}, std::pair{0x108U, 3U}}) {
      Stepper s(h.value().ir);
      s.set("nl_narrow_ready", 1);
      offer(s, "wide", 16, address, 4, 1, 0, true, true);
      CHECK(reach(s, "narrow", 8) == last_byte);
      CHECK(s.out("nl_narrow_ends") == (last_byte == 7 ? 1 : 0));
      CHECK(s.out("nl_narrow_last") == 1);
      CHECK(s.out("nl_wide_ready") == 1);
    }
  }
  SECTION("Only request deserializers take a merge gate") {
    WidthAdapterParameters response{8, 4, true, 2, burst};
    response.merge_gate = true;
    const auto refused = plan_width_adapter(response);
    REQUIRE_FALSE(refused);
    CHECK(has_diagnostic(refused.diagnostics(), "NCL_WIDTH_ADAPTER_MERGE"));
  }
}
