#include "core/hardware_builder.hpp"
#include <catch2/catch_test_macros.hpp>

using namespace nocloom::detail;
namespace {
struct Built {
  HardwareIR ir;
  ValueId unused;
};
// One circuit, built in two creation orders the way two compilers may
// evaluate nested Builder arguments. The ports are created in one order.
Built circuit(bool reversed) {
  Builder b;
  b.ir.name = "nocloom_canonical";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto a = b.input("nl_a", 4, "initiator:a", "data");
  auto c = b.input("nl_c", 4, "initiator:a", "mask");
  auto held = b.state(4, clock, reset);
  ValueId both, either, unused;
  if (reversed) {
    unused = b.binary(Op::Subtract, a, c);
    either = b.binary(Op::BitOr, a, c);
    both = b.binary(Op::BitAnd, a, c);
  } else {
    both = b.binary(Op::BitAnd, a, c);
    either = b.binary(Op::BitOr, a, c);
    unused = b.binary(Op::Subtract, a, c);
  }
  auto pick = b.binary(Op::Equal, both, b.constant(4, 0));
  b.update(held, b.select(pick, either, held));
  b.output("nl_q", b.select(pick, held, both), "target:t", "data");
  return {std::move(b.ir), unused};
}
} // namespace

TEST_CASE("Canonical numbering ignores the Builder call order", "[canonical]") {
  auto forward = circuit(false), backward = circuit(true);
  REQUIRE(forward.ir.values.size() == backward.ir.values.size());
  REQUIRE(emit_verilog(forward.ir) != emit_verilog(backward.ir));
  const auto fm = canonicalize(forward.ir, {}, {forward.unused});
  const auto bm = canonicalize(backward.ir, {}, {backward.unused});
  REQUIRE(!fm.empty());
  REQUIRE(!bm.empty());
  REQUIRE(emit_verilog(forward.ir) == emit_verilog(backward.ir));
  REQUIRE(verify_structure(forward.ir));
  // The observed subtraction is kept after the emitted prefix and left out
  // of the RTL; it maps to the same canonical index in both builds.
  REQUIRE(fm[forward.unused.index] == bm[backward.unused.index]);
  REQUIRE(fm[forward.unused.index] == *forward.ir.emitted);
  REQUIRE(forward.ir.values.size() == *forward.ir.emitted + 1);
  REQUIRE(emit_verilog(forward.ir).find(" - ") == std::string::npos);
}

TEST_CASE("Canonicalization removes unused logic and rejects cycles", "[canonical]") {
  auto built = circuit(false);
  const auto before = built.ir.values.size();
  const auto mapped = canonicalize(built.ir, {}, {});
  REQUIRE(mapped[built.unused.index] == removed_value);
  REQUIRE(built.ir.values.size() == before - 1);
  REQUIRE(*built.ir.emitted == built.ir.values.size());
  // Operands precede their users, so the result passes the ordered contract.
  REQUIRE(verify_structure(built.ir));

  auto cyclic = circuit(false);
  const auto driven = cyclic.ir.drivers.front().value;
  // Two selects feeding each other: the driver and a copy of it.
  auto copy = cyclic.ir.values.at(driven.index);
  copy.operands[2] = driven;
  cyclic.ir.values.at(driven.index).operands[2] = {cyclic.ir.values.size()};
  cyclic.ir.values.push_back(copy);
  const auto snapshot = cyclic.ir.values.size();
  REQUIRE(canonicalize(cyclic.ir, {}, {}).empty());
  REQUIRE(cyclic.ir.values.size() == snapshot);
}

namespace {
struct Pair {
  HardwareIR ir;
  ValueId first, second, idle;
};
// Two registers feeding each other and two outputs, with every Builder call
// order reversed between the builds: state creation, next-value logic,
// output drivers and the synchronizer stage list. The ports keep one order;
// they are the interface. A third register is a synchronizer stage that
// nothing reads.
Pair pair(bool reversed) {
  Builder b;
  b.ir.name = "nocloom_canonical_pair";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto a = b.input("nl_a", 4, "initiator:a", "data");
  b.ir.ports.push_back({"nl_x", false, 4, "target:t", "x"});
  b.ir.ports.push_back({"nl_y", false, 4, "target:t", "y"});
  Pair p;
  if (reversed) {
    p.idle = b.state(4, clock, reset);
    p.second = b.state(4, clock, reset, 3);
    p.first = b.state(4, clock, reset, 5);
    b.update(p.second, b.binary(Op::BitOr, p.first, a));
    b.update(p.first, b.binary(Op::BitAnd, a, p.second));
  } else {
    p.first = b.state(4, clock, reset, 5);
    p.second = b.state(4, clock, reset, 3);
    p.idle = b.state(4, clock, reset);
    b.update(p.first, b.binary(Op::BitAnd, a, p.second));
    b.update(p.second, b.binary(Op::BitOr, p.first, a));
  }
  b.update(p.idle, a);
  const PortId x{3}, y{4};
  if (reversed) {
    b.ir.drivers.push_back({y, b.binary(Op::Subtract, p.second, p.first)});
    b.ir.drivers.push_back({x, p.first});
    b.ir.synchronizer_stages = {p.second, p.idle, p.first};
  } else {
    b.ir.drivers.push_back({x, p.first});
    b.ir.drivers.push_back({y, b.binary(Op::Subtract, p.second, p.first)});
    b.ir.synchronizer_stages = {p.first, p.idle, p.second};
  }
  p.ir = std::move(b.ir);
  return p;
}
} // namespace

TEST_CASE("Canonical numbering ignores state, driver and stage order", "[canonical]") {
  auto forward = pair(false), backward = pair(true);
  REQUIRE(verify_structure(forward.ir));
  REQUIRE(verify_structure(backward.ir));
  // The stage list here forms no synchronizer chain the backend could emit; the RTL shows the
  // numbering alone.
  const auto emit_verilog = [](HardwareIR ir) {
    ir.synchronizer_stages.clear();
    return nocloom::detail::emit_verilog(ir);
  };
  REQUIRE(emit_verilog(forward.ir) != emit_verilog(backward.ir));
  const auto fm = canonicalize(forward.ir, {}, {});
  const auto bm = canonicalize(backward.ir, {}, {});
  REQUIRE(!fm.empty());
  REQUIRE(!bm.empty());
  REQUIRE(emit_verilog(forward.ir) == emit_verilog(backward.ir));
  REQUIRE(verify_structure(forward.ir));
  // Each register lands on one index in both builds.
  REQUIRE(fm[forward.first.index] == bm[backward.first.index]);
  REQUIRE(fm[forward.second.index] == bm[backward.second.index]);
  // The unread stage is removed with its state; the stage list keeps the
  // two live registers in index order.
  REQUIRE(fm[forward.idle.index] == removed_value);
  REQUIRE(bm[backward.idle.index] == removed_value);
  REQUIRE(forward.ir.states.size() == 2);
  REQUIRE(forward.ir.synchronizer_stages.size() == 2);
  REQUIRE(forward.ir.synchronizer_stages[0].index < forward.ir.synchronizer_stages[1].index);
  for (std::size_t i = 0; i < 2; ++i)
    REQUIRE(forward.ir.synchronizer_stages[i].index == backward.ir.synchronizer_stages[i].index);
  // Drivers come out in port order whatever order they were attached in.
  REQUIRE(backward.ir.drivers.front().port.index == 3);
}
