#include "core/axi4_width_clamp.hpp"
#include <catch2/catch_test_macros.hpp>

using namespace nocloom::detail;
namespace {
Axi4WidthCutProfile narrow_cut(unsigned id = 4, unsigned address = 32, unsigned source = 0,
                               unsigned data = 128) {
  Axi4WidthCutProfile p;
  p.name = "unit";
  p.id_bits = id;
  p.address_bits = address;
  p.source_bits = source;
  p.data_bits = data;
  p.down_bits = 32;
  p.fixed_width = 2;
  return p;
}
} // namespace

TEST_CASE("Fixed-width burst cutting has bounded geometry and deterministic structure",
          "[axi4-width-cut]") {
  for (unsigned data : {64U, 128U, 256U, 512U, 1024U})
    for (unsigned address : {32U, 40U})
      for (unsigned id : {0U, 8U})
        for (unsigned source : {0U, 7U}) {
          const auto p = narrow_cut(id, address, source, data);
          auto h = lower_axi4_width_cut(p);
          REQUIRE(h);
          REQUIRE(verify(h.value().ir));
          CHECK(emit_verilog(h.value().ir) == emit_verilog(lower_axi4_width_cut(p).value().ir));
          for (const auto &port : h.value().ir.ports) {
            if (port.signal == "data")
              CHECK(port.width == (port.endpoint.starts_with("up_") ? data : 32));
            if (port.signal == "address" || port.signal == "start_address")
              CHECK(port.width == address);
          }
        }
  REQUIRE_FALSE(lower_axi4_width_cut(narrow_cut(4, 32, 0, 2048)));
  auto bad = narrow_cut();
  bad.name = "bad/name";
  REQUIRE_FALSE(lower_axi4_width_cut(bad));
  REQUIRE(lower_axi4_width_cut(narrow_cut(16)));
  REQUIRE_FALSE(lower_axi4_width_cut(narrow_cut(17)));
  REQUIRE_FALSE(lower_axi4_width_cut(narrow_cut(4, 31)));
  REQUIRE(lower_axi4_width_cut(narrow_cut(4, 48)));
  REQUIRE_FALSE(lower_axi4_width_cut(narrow_cut(4, 49)));
  REQUIRE(lower_axi4_width_cut(narrow_cut(4, 32, 9)));
  REQUIRE_FALSE(lower_axi4_width_cut(narrow_cut(4, 32, 10)));
}

TEST_CASE("Width clamp cutter has bounded geometry and deterministic structure",
          "[axi4-width-cut]") {
  for (unsigned data : {64U, 128U, 256U, 512U, 1024U})
    for (unsigned len : {4U, 8U})
      for (bool exclusive : {false, true}) {
        Axi4WidthClampProfile p{"unit", 3, len, 32, 2, data};
        p.windows = {{{0x1000, 0x1000}, 2}, {{0x10000, 0x10000}, 3}};
        p.fixed_reject = {{0x1000, 0x1000}};
        p.exclusive = exclusive;
        auto h = lower_axi4_width_clamp(p);
        REQUIRE(h);
        REQUIRE(verify(h.value().ir));
        CHECK(emit_verilog(h.value().ir) == emit_verilog(lower_axi4_width_clamp(p).value().ir));
        // Both faces keep the source layout.
        for (const auto &port : h.value().ir.ports)
          if (port.signal == "data")
            CHECK(port.width == data);
        const auto bits = axi4_width_clamp_state_bits(p);
        CHECK(bits.first > 0);
        CHECK(bits.second > 0);
      }
  // The FIXED refusals add no state.
  Axi4WidthClampProfile p{"unit", 3, 8, 32, 0, 256};
  p.windows = {{{0x1000, 0x1000}, 2}};
  const auto plain = axi4_width_clamp_state_bits(p);
  p.fixed_reject = {{0x1000, 0x1000}};
  CHECK(axi4_width_clamp_state_bits(p) == plain);
  auto cut = narrow_cut(4, 32, 0, 128);
  const auto cut_plain = direction_state_bits(lower_axi4_width_cut(cut));
  cut.fixed_reject = {{0x1000, 0x1000}};
  CHECK(direction_state_bits(lower_axi4_width_cut(cut)) == cut_plain);
  auto bad = p;
  bad.windows = {{{0x1000, 0x1000}, 1}};
  REQUIRE_FALSE(lower_axi4_width_clamp(bad));
  bad = p;
  bad.fixed_reject = {{0, 0}};
  REQUIRE_FALSE(lower_axi4_width_clamp(bad));
  bad = p;
  bad.len_bits = 9;
  REQUIRE_FALSE(lower_axi4_width_clamp(bad));
  cut = narrow_cut();
  cut.fixed_width.reset();
  REQUIRE_FALSE(lower_axi4_width_cut(cut));
  cut.fixed_width = 2;
  REQUIRE(lower_axi4_width_cut(cut));
}
