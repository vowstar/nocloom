#include "core/async_fifo.hpp"
#include "core/fifo_levels.hpp"
#include "core/ir_cone.hpp"
#include <algorithm>
#include <array>
#include <catch2/catch_test_macros.hpp>
#include <cmath>

using namespace nocloom::detail;
namespace {
const std::vector<std::pair<std::string, unsigned>> layout{{"data", 32}, {"tag", 3}};
AsyncFifo fifo(unsigned depth = 6, unsigned n = 2, bool handshake = false, bool all = false) {
  auto result = lower_async_fifo({"a"}, {"b"}, depth, n, layout, handshake, all);
  REQUIRE(result);
  return std::move(result).value();
}
State &state(HardwareIR &ir, ValueId id) {
  return ir.states.at(ir.values.at(id.index).state.index);
}
unsigned depth_of(unsigned n, std::uint64_t ta, std::uint64_t tb) {
  auto result = default_crossing_depth(n, ta, tb);
  REQUIRE(result);
  REQUIRE(result.value().source == CrossingDepthSource::Formula);
  return result.value().depth;
}
} // namespace

TEST_CASE("Crossing depth follows the turnaround formula", "[async_fifo]") {
  // (n + 1)(Ta + Tb) / max, the per-n table in the design.
  const std::array<std::array<unsigned, 3>, 3> table{{{6, 5, 4}, {8, 6, 5}, {10, 8, 7}}};
  for (unsigned n = 2; n <= 4; ++n)
    for (unsigned r = 0; r < 3; ++r) {
      const std::uint64_t slow = std::uint64_t{1000} << r, fast = 1000;
      CAPTURE(n, r);
      CHECK(depth_of(n, fast, slow) == table[n - 2][r]);
      CHECK(depth_of(n, slow, fast) == table[n - 2][r]);
    }
  CHECK(depth_of(2, 1000, 64000) == 4);
  CHECK(depth_of(4, 64000, 1000) == 6);
  CHECK(depth_of(3, 1250, 1250) == 8);
  CHECK(depth_of(4, 1000, 3000) == 7);
  CHECK(depth_of(2, 7000, 5000) == 6);
  // Integer ceil agrees with the real-valued formula and stays in [n + 2, 2n + 2].
  for (unsigned n = 2; n <= 4; ++n)
    for (std::uint64_t ta : {100ULL, 333ULL, 1000ULL, 1001ULL, 4096ULL, 99991ULL, 1000000000ULL})
      for (std::uint64_t tb : {100ULL, 777ULL, 1000ULL, 2000ULL, 65537ULL, 1000000000ULL}) {
        const auto d = depth_of(n, ta, tb);
        const double real = std::ceil((n + 1.0) * double(ta + tb) / double(std::max(ta, tb)));
        CAPTURE(n, ta, tb);
        CHECK(d == static_cast<unsigned>(real));
        CHECK(d >= n + 2);
        CHECK(d <= 2 * n + 2);
      }
}

TEST_CASE("Crossing depth rounds up the synchronization loop", "[async_fifo]") {
  struct DepthCase {
    unsigned n;
    std::uint64_t ta, tb;
  };
  // Exact division, remainder one and remainder denominator minus one.
  // Periods are in picoseconds. Swapping sides must preserve the result.
  const std::array<DepthCase, 10> depth_cases{{{2, 137, 137},
                                               {2, 142, 213},
                                               {2, 213, 142},
                                               {2, 101, 151},
                                               {2, 151, 101},
                                               {2, 100, 301},
                                               {2, 301, 100},
                                               {3, 199, 199},
                                               {4, 127, 127},
                                               {4, 123, 307}}};
  const auto sufficient = [](const DepthCase &p, unsigned d) {
    const auto period = std::max(p.ta, p.tb);
    const auto turnaround = (p.n + 1) * (p.ta + p.tb);
    return d > 0 && d * period >= turnaround && (d - 1) * period < turnaround;
  };
  for (const auto &p : depth_cases) {
    CAPTURE(p.n, p.ta, p.tb);
    CHECK(sufficient(p, depth_of(p.n, p.ta, p.tb)));
  }
  // These deliberately wrong rules must each violate the same integer conditions.
  using Rule = unsigned (*)(const DepthCase &);
  const std::array<Rule, 4> near_misses{
      [](const DepthCase &p) { return unsigned((p.n + 1) * (p.ta + p.tb) / std::max(p.ta, p.tb)); },
      [](const DepthCase &p) {
        const auto fast = std::min(p.ta, p.tb);
        return unsigned(((p.n + 1) * (p.ta + p.tb) + fast - 1) / fast);
      },
      [](const DepthCase &p) {
        const auto slow = std::max(p.ta, p.tb);
        return unsigned((p.n * (p.ta + p.tb) + slow - 1) / slow);
      },
      [](const DepthCase &) { return async_fifo_fallback_depth; }};
  for (std::size_t i = 0; i < near_misses.size(); ++i) {
    CAPTURE(i);
    CHECK(std::any_of(depth_cases.begin(), depth_cases.end(),
                      [&](const DepthCase &p) { return !sufficient(p, near_misses[i](p)); }));
  }
}

TEST_CASE("Crossing depth falls back, overrides and rejects", "[async_fifo]") {
  for (auto [ta, tb] : {std::pair<std::optional<std::uint64_t>, std::optional<std::uint64_t>>{
                            std::nullopt, std::nullopt},
                        {1000, std::nullopt},
                        {std::nullopt, 1000}}) {
    auto d = default_crossing_depth(4, ta, tb);
    REQUIRE(d);
    CHECK(d.value() == CrossingDepth{6, CrossingDepthSource::Fallback});
  }
  auto explicit_depth = resolve_crossing_depth(2, 2, 1000, 1000);
  REQUIRE(explicit_depth);
  CHECK(explicit_depth.value() == CrossingDepth{2, CrossingDepthSource::Explicit});
  auto defaulted = resolve_crossing_depth(std::nullopt, 3, 1000, 2000);
  REQUIRE(defaulted);
  CHECK(defaulted.value() == CrossingDepth{6, CrossingDepthSource::Formula});
  for (unsigned bad : {0U, 1U, 11U}) {
    auto r = resolve_crossing_depth(bad, 2, std::nullopt, std::nullopt);
    REQUIRE_FALSE(r);
    CHECK(r.diagnostics().front().code == "NCL_CDC_DEPTH_RANGE");
  }
  CHECK(default_crossing_depth(2, 99, 1000).diagnostics().front().code == "NCL_CDC_PERIOD_RANGE");
  CHECK(default_crossing_depth(2, 1000, 1000000001).diagnostics().front().code ==
        "NCL_CDC_PERIOD_RANGE");
  CHECK(default_crossing_depth(1, 1000, 1000).diagnostics().front().code ==
        "NCL_CDC_STAGES_UNSUPPORTED");
  CHECK(default_crossing_depth(5, std::nullopt, std::nullopt).diagnostics().front().code ==
        "NCL_CDC_STAGES_UNSUPPORTED");
  // An explicit depth still needs valid stages for the chains.
  CHECK(resolve_crossing_depth(4, 5, std::nullopt, std::nullopt).diagnostics().front().code ==
        "NCL_CDC_STAGES_UNSUPPORTED");
}

TEST_CASE("Async FIFO lowers every depth and stage combination", "[async_fifo]") {
  for (unsigned depth = 2; depth <= 10; ++depth)
    for (unsigned n = 2; n <= 4; ++n) {
      CAPTURE(depth, n);
      auto f = fifo(depth, n);
      REQUIRE(verify_async_fifo(f));
      CHECK((1U << f.count_bits) > depth);
      CHECK((1U << (f.count_bits - 1)) <= depth);
      CHECK((1U << f.index_bits) >= depth);
      CHECK(f.write_count_sync.size() == n);
      CHECK(f.read_count_sync.size() == n);
      // Both count chains and five control registers.
      const std::size_t control = 5 + 2 * n;
      const std::size_t payload = layout.size() * (depth + 1);
      CHECK(f.ir.states.size() == control + payload);
      CHECK(std::count_if(f.ir.states.begin(), f.ir.states.end(), [](const auto &s) {
              return !s.reset;
            }) == static_cast<std::ptrdiff_t>(payload));
      CHECK(f.ir.synchronizer_stages.size() == 2 * n);
      CHECK(emit_verilog(f.ir) == emit_verilog(fifo(depth, n).ir));
      // The reset handshake adds per side announce, on, the wait counter and two chains; the
      // counts lose their reset.
      auto h = fifo(depth, n, true);
      REQUIRE(verify_async_fifo(h));
      CHECK(h.ir.states.size() == control + payload + 2 * (3 + 2 * n));
      CHECK(std::count_if(h.ir.states.begin(), h.ir.states.end(), [](const auto &s) {
              return !s.reset;
            }) == static_cast<std::ptrdiff_t>(payload + 2));
      CHECK(h.ir.synchronizer_stages.size() == 6 * n);
      CHECK(emit_verilog(h.ir) == emit_verilog(fifo(depth, n, true).ir));
      // All mode resets the payload registers too; the handshake counts keep no reset pin.
      auto a = fifo(depth, n, false, true);
      REQUIRE(verify_async_fifo(a));
      CHECK(std::count_if(a.ir.states.begin(), a.ir.states.end(),
                          [](const auto &s) { return !s.reset; }) == 0);
      auto ha = fifo(depth, n, true, true);
      REQUIRE(verify_async_fifo(ha));
      CHECK(std::count_if(ha.ir.states.begin(), ha.ir.states.end(),
                          [](const auto &s) { return !s.reset; }) == 2);
      CHECK(emit_verilog(ha.ir) == emit_verilog(fifo(depth, n, true, true).ir));
    }
}

TEST_CASE("Async FIFO verifier rejects a broken reset handshake", "[async_fifo]") {
  auto f = fifo(6, 2, true);
  REQUIRE(verify_async_fifo(f));
  Builder b;
  b.ir = std::move(f.ir);
  auto &h = f.handshake[1];
  std::string expected;
  SECTION("enable ignores the other side") {
    expected = "Reset handshake must announce after the wait and turn on on acknowledge";
    b.ir.values[h.enable.index].operands[0] = h.on;
  }
  SECTION("on without acknowledge") {
    expected = "Reset handshake must announce after the wait and turn on on acknowledge";
    b.update(h.on, b.binary(Op::BitOr, h.on, h.announce));
  }
  SECTION("announce without the wait") {
    expected = "Reset handshake must announce after the wait and turn on on acknowledge";
    b.update(h.announce,
             b.binary(Op::BitOr, h.announce, b.unary(Op::BitNot, h.ack_sync.back(), 1)));
  }
  SECTION("count with a reset") {
    expected = "Invalid count, index or synchronizer ownership";
    state(b.ir, f.write_count).reset = AsyncReset{f.domains[0].reset, 0};
  }
  SECTION("count kept while disabled") {
    expected = "Counts must advance one Gray code step per transfer";
    b.update(f.read_count, b.ir.values[state(b.ir, f.read_count).next.index].operands[1]);
  }
  SECTION("ready ignores the enable") {
    expected = "Ready requires a free entry";
    auto &ready = b.ir.values[f.ready.index];
    ready.operands[1] = ready.operands[0];
  }
  SECTION("output valid not qualified") {
    expected = "Handshake port does not match its transfer binding";
    f.output_valid = f.out_valid;
  }
  SECTION("control output reads a payload slot") {
    // The CDC fault class the port-cone walk guards: the out_valid cone gains a read of a
    // sender payload slot behind an always-true condition. The value-level checks evaluate
    // only the taken select branch, so the cone reads identically to the good design; the
    // walk visits both operands, so the slot read must be reported as a domain mix. The
    // payload outputs keep their pinned structure through fresh output values that select
    // the doctored valid.
    expected = "An output mixes clock domains";
    const auto &field = f.fields[0];
    const auto slot_bit = b.slice(field.storage[1], 0, 1);
    const auto tautology = b.binary(Op::Equal, f.out_valid, f.out_valid);
    const auto mixed = b.select(tautology, f.output_valid, slot_bit);
    for (auto &fld : f.fields) {
      const auto out = b.select(mixed, fld.received, b.constant(fld.width, 0));
      for (auto &driver : b.ir.drivers)
        if (driver.value.index == fld.output.index)
          driver.value = out;
      fld.output = out;
    }
    for (auto &driver : b.ir.drivers)
      if (driver.value.index == f.output_valid.index)
        driver.value = mixed;
    f.output_valid = mixed;
  }
  SECTION("short acknowledge chain") {
    expected = "Invalid reset handshake registers or synchronizer chains";
    h.ack_sync.pop_back();
  }
  f.ir = std::move(b.ir);
  REQUIRE(verify_structure(f.ir));
  const auto status = verify_async_fifo(f);
  REQUIRE_FALSE(status);
  CHECK(status.diagnostics().front().message == expected);
}

TEST_CASE("Async FIFO rejects bad parameters", "[async_fifo]") {
  DomainPlan a{"a"}, b{"b"};
  unsigned depth = 6, stages = 2;
  auto fields = layout;
  SECTION("depth one") { depth = 1; }
  SECTION("depth eleven") { depth = 11; }
  SECTION("same domain") { b.name = "a"; }
  SECTION("one stage") { stages = 1; }
  SECTION("five stages") { stages = 5; }
  SECTION("no fields") { fields.clear(); }
  SECTION("repeated field") { fields.push_back(fields.front()); }
  SECTION("empty field") { fields.front().second = 0; }
  auto r = lower_async_fifo(a, b, depth, stages, fields);
  REQUIRE_FALSE(r);
  CHECK(r.diagnostics().front().code == "NCL_ASYNC_FIFO_CONFIG");
}

TEST_CASE("Async FIFO verifier rejects broken structure", "[async_fifo]") {
  auto f = fifo(6, 3);
  REQUIRE(verify_async_fifo(f));
  Builder b;
  b.ir = std::move(f.ir);
  const auto &field = f.fields[0];
  std::string expected;
  SECTION("binary write count") {
    expected = "Counts must advance one Gray code step per transfer";
    // Counting in binary keeps occupancy right but lets several bits cross at once.
    const auto w = f.count_bits;
    b.update(f.write_count,
             b.select(f.write, b.binary(Op::Subtract, f.write_count, b.constant(w, (1U << w) - 1)),
                      f.write_count));
  }
  SECTION("full one entry late") {
    expected = "Full must compare the binary count distance with the depth";
    b.ir.values[b.ir.values[f.full.index].operands[1].index].literal = f.depth + 1;
  }
  SECTION("full one entry early") {
    expected = "Full must compare the binary count distance with the depth";
    b.ir.values[b.ir.values[f.full.index].operands[1].index].literal = f.depth - 1;
  }
  SECTION("no receive register") {
    expected = "Payload outputs must be zero unless out valid";
    b.ir.values[field.output.index].operands[1] = field.selected;
  }
  SECTION("receive register always loads") {
    expected = "The receiver forward register must load the selected slot on read";
    state(b.ir, field.received).next = field.selected;
  }
  SECTION("unqualified payload output") {
    expected = "Payload outputs must be zero unless out valid";
    b.ir.values[field.output.index].operands[0] = f.nonempty;
  }
  SECTION("reset payload slot") {
    expected = "Payload slots are sender-clocked registers with a reset iff reset_all";
    state(b.ir, field.storage[2]).reset = AsyncReset{f.domains[0].reset, 0};
  }
  SECTION("slot written at every index") {
    expected = "A payload slot may change only when written at its index";
    auto next = b.select(f.write, field.input, field.storage[1]);
    state(b.ir, field.storage[1]).next = next;
  }
  SECTION("read selection by write index") {
    expected = "The read selection may depend only on the read index";
    // The slot-0 write enable is write & (write_index == 0); reuse its comparison.
    const auto enable = b.ir.values[state(b.ir, field.storage[0]).next.index].operands[0];
    b.ir.values[field.selected.index].operands[0] = b.ir.values[enable.index].operands[1];
  }
  SECTION("read selection skips a slot") {
    expected = "The read selection must pick the slot at the read index";
    auto &selected = b.ir.values[field.selected.index];
    selected.operands[1] = field.storage[1];
  }
  SECTION("ready ignores full") {
    expected = "Ready requires a free entry";
    b.ir.values[f.ready.index].operands[0] = f.valid;
  }
  SECTION("nonempty reads an intermediate stage") {
    expected = "Nonempty requires unequal Gray counts";
    b.ir.values[b.ir.values[f.nonempty.index].operands[0].index].operands[1] =
        f.write_count_sync.front();
  }
  SECTION("read while stalled") {
    expected = "The forward register must load whenever it is empty or drained";
    b.ir.values[f.read.index].operands[1] = f.nonempty;
  }
  SECTION("out valid drops while stalled") {
    expected = "Out valid must hold until drained and set on every load";
    b.update(f.out_valid, f.read);
  }
  SECTION("logic before synchronizer") {
    expected = "Invalid count, index or synchronizer ownership";
    auto next = b.binary(Op::BitOr, f.write_count, f.write_count);
    state(b.ir, f.write_count_sync.front()).next = next;
  }
  SECTION("short write count chain") {
    expected = "Invalid count, index or synchronizer ownership";
    f.write_count_sync.pop_back();
  }
  SECTION("declared stages differ") {
    expected = "Invalid count, index or synchronizer ownership";
    f.stages = 4;
  }
  SECTION("missing preservation attribute") {
    expected = "Synchronizer preservation must bind every actual stage";
    b.ir.synchronizer_stages.pop_back();
  }
  SECTION("intermediate stage feeds out valid") {
    expected = "Out valid must hold until drained and set on every load";
    auto early =
        b.unary(Op::BitNot, b.binary(Op::Equal, f.read_count, f.write_count_sync.front()), 1);
    b.update(f.out_valid, b.binary(Op::BitOr, early, f.read));
  }
  SECTION("unsynchronized count") {
    expected = "Invalid count, index or synchronizer ownership";
    state(b.ir, f.read_count_sync.front()).next = f.write_count;
  }
  SECTION("index without wrap") {
    expected = "Indexes must advance modulo the depth per transfer";
    b.update(f.read_index,
             b.select(f.read, b.binary(Op::Subtract, f.read_index, b.constant(f.index_bits, 7)),
                      f.read_index));
  }
  SECTION("count width mismatch") {
    expected = "Count or index width does not match the depth";
    f.count_bits += 1;
  }
  SECTION("receiver register reset by the sender") {
    expected = "Invalid count, index or synchronizer ownership";
    state(b.ir, f.read_index).reset = AsyncReset{f.domains[0].reset, 0};
  }
  SECTION("sender chain reset by the receiver") {
    expected = "Invalid count, index or synchronizer ownership";
    state(b.ir, f.read_count_sync.front()).reset = AsyncReset{f.domains[1].reset, 0};
  }
  SECTION("unbound state") {
    expected = "FIFO contains unbound storage or control state";
    b.held_state(4, f.domains[0].clock);
  }
  f.ir = std::move(b.ir);
  REQUIRE(verify_structure(f.ir));
  const auto status = verify_async_fifo(f);
  REQUIRE_FALSE(status);
  CHECK(status.diagnostics().front().code == "NCL_INTERNAL_ASYNC_FIFO");
  CHECK(status.diagnostics().front().message == expected);
}

TEST_CASE("Async FIFO verifier checks the payload reset mode", "[async_fifo]") {
  auto f = fifo(4, 2, false, true);
  REQUIRE(verify_async_fifo(f));
  Builder b;
  b.ir = std::move(f.ir);
  const auto &field = f.fields[0];
  std::string expected;
  SECTION("payload slot without reset") {
    expected = "Payload slots are sender-clocked registers with a reset iff reset_all";
    state(b.ir, field.storage[1]).reset = std::nullopt;
  }
  SECTION("payload slot reset by the receiver") {
    expected = "Payload slots are sender-clocked registers with a reset iff reset_all";
    state(b.ir, field.storage[0]).reset = AsyncReset{f.domains[1].reset, 0};
  }
  SECTION("receive register without reset") {
    expected = "The receiver forward register must load the selected slot on read";
    state(b.ir, field.received).reset = std::nullopt;
  }
  f.ir = std::move(b.ir);
  REQUIRE(verify_structure(f.ir));
  const auto status = verify_async_fifo(f);
  REQUIRE_FALSE(status);
  CHECK(status.diagnostics().front().code == "NCL_INTERNAL_ASYNC_FIFO");
  CHECK(status.diagnostics().front().message == expected);
}

TEST_CASE("Confirmed levels cover the physical FIFO interval", "[async_fifo]") {
  for (unsigned depth = 2; depth <= 10; ++depth) {
    Builder b;
    b.ir.name = "nocloom_confirmed_levels";
    const auto cb = async_fifo_count_bits(depth), ib = async_fifo_index_bits(depth);
    const auto wc = b.input("wc", cb, "probe", "wc"), rc = b.input("rc", cb, "probe", "rc");
    const auto ri = b.input("ri", ib, "probe", "ri"),
               enabled = b.input("enabled", 1, "probe", "enabled");
    std::vector<ValueId> slots;
    for (unsigned i = 0; i < depth; ++i)
      slots.push_back(b.input("slot" + std::to_string(i), 3, "probe", "slot" + std::to_string(i)));
    const auto levels = lower_fifo_levels(b, wc, rc, ri, enabled, slots);
    REQUIRE(verify_structure(b.ir));
    const unsigned modulus = 1U << cb;
    for (unsigned read = 0; read < modulus; ++read)
      for (unsigned index = 0; index < depth; ++index)
        for (unsigned count = 0; count <= depth; ++count) {
          const auto write = (read + count) % modulus;
          std::map<std::size_t, std::uint64_t> values{{wc.index, write ^ (write >> 1)},
                                                      {rc.index, read ^ (read >> 1)},
                                                      {ri.index, index},
                                                      {enabled.index, 1}};
          for (unsigned i = 0; i < depth; ++i) {
            values[slots[i].index] = (i + read) % 8;
            const auto expected = (i + depth - index) % depth < count;
            CAPTURE(depth, read, index, count, i);
            CHECK(evaluate_cone(b.ir, levels.active[i].index, {}, values) == expected);
          }
          unsigned expected = 0;
          for (unsigned offset = 0; offset < count; ++offset)
            expected = std::max(expected, unsigned(values[slots[(index + offset) % depth].index]));
          CHECK(evaluate_cone(b.ir, levels.level.index, {}, values) == expected);
          values[enabled.index] = 0;
          CHECK(evaluate_cone(b.ir, levels.level.index, {}, values) == 0);
        }
  }
}
