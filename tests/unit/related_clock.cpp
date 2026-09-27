#include "core/related_clock.hpp"
#include <catch2/catch_test_macros.hpp>

using namespace nocloom::detail;
namespace {
const std::vector<std::pair<std::string, unsigned>> layout{{"data", 32}, {"tag", 3}, {"status", 2}};
}

TEST_CASE("Related Sync keeps Gray data buffers and local counter returns", "[related-clock]") {
  for (unsigned depth = 2; depth <= 4; ++depth) {
    CAPTURE(depth);
    auto lowered = lower_related_sync({"a"}, {"b"}, depth, false, layout, true);
    REQUIRE(lowered);
    const auto &f = lowered.value();
    REQUIRE(verify_related_adapter(f, false));
    CHECK(f.read_count_sync.size() == 1);
    CHECK(f.write_count_sync.size() == 1);
    CHECK(f.ir.synchronizer_stages.empty());
    CHECK(f.ir.states.size() == 6 + layout.size() * depth);
    for (const auto &field : f.fields) {
      CHECK(field.storage.size() == depth);
      CHECK(field.received.index == field.selected.index);
    }
    CHECK(emit_verilog(f.ir) ==
          emit_verilog(lower_related_sync({"a"}, {"b"}, depth, false, layout, true).value().ir));
  }
  auto fast = lower_related_sync({"a"}, {"b"}, 2, true, layout, true);
  REQUIRE(fast);
  CHECK(fast.value().read_count_sync.empty());
  CHECK(fast.value().write_count_sync.empty());
  CHECK(fast.value().returned_read_count().index == fast.value().read_count.index);
  CHECK(fast.value().returned_write_count().index == fast.value().write_count.index);
  CHECK(fast.value().ir.states.size() == 4 + layout.size() * 2);
  CHECK(fast.value().fields[0].storage.size() == 2);
  CHECK_FALSE(lower_related_sync({"a"}, {"b"}, 3, true, layout, true));
  CHECK_FALSE(lower_related_sync({"a"}, {"b"}, 1, false, layout, true));
  CHECK_FALSE(lower_related_sync({"a"}, {"b"}, 5, false, layout, true));
}

TEST_CASE("EdgeAligned is control-only with an optional two-slot slow pipe", "[related-clock]") {
  for (bool slow_from_sender : {false, true})
    for (bool pipe : {false, true}) {
      CAPTURE(slow_from_sender, pipe);
      auto lowered = lower_edge_aligned({"a"}, {"b"}, slow_from_sender, pipe, layout, true);
      REQUIRE(lowered);
      const auto &f = lowered.value();
      REQUIRE(verify_related_adapter(f, true));
      CHECK(f.depth == 0);
      CHECK(f.ir.synchronizer_stages.empty());
      for (const auto &field : f.fields)
        CHECK(field.storage.empty());
      CHECK(f.pipeline_payload.size() == (pipe ? 2 * layout.size() : 0));
      CHECK(f.pipeline_control.size() == (pipe ? 2U : 0U));
      CHECK((f.admission_open.index != 0) == (slow_from_sender || !pipe));
      CHECK(f.ir.states.size() == (slow_from_sender ? 1U
                                   : !pipe          ? 1U
                                                    : 0U) +
                                      (pipe ? 2 + 2 * layout.size() : 0));
    }
}
