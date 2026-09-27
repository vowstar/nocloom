#include "core/axi4_initiator.hpp"
#include "core/axi4_packetizer.hpp"
#include "core/axi4_upsize.hpp"
#include "core/axi4_width_clamp.hpp"
#include "core/ir_cone.hpp"
#include <algorithm>
#include <bit>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <map>
#include <numeric>
#include <set>
#include <string>
#include <vector>

using namespace nocloom::detail;
using nocloom::AddressWindow;

namespace {
// Input ports in the combinational cone of a value (the walk stops at
// states).
std::set<std::string> cone_inputs(const HardwareIR &ir, std::size_t root) {
  std::set<std::string> found;
  std::set<std::size_t> seen;
  std::function<void(std::size_t)> walk = [&](std::size_t index) {
    if (!seen.insert(index).second)
      return;
    const auto &v = ir.values.at(index);
    unsigned operands = 0;
    switch (v.op) {
    case Op::Input:
      found.insert(ir.ports.at(v.input_port.index).name);
      return;
    case Op::Constant:
    case Op::State:
      return;
    case Op::ZeroExtend:
    case Op::BitNot:
      operands = 1;
      break;
    case Op::Select:
      operands = 3;
      break;
    default:
      operands = 2;
    }
    for (unsigned k = 0; k < operands; ++k)
      walk(v.operands[k].index);
  };
  walk(root);
  return found;
}

// The write beat ready of the up face is a register (reset only), or, with `merge` (the target
// merge, whose one word entry passes a word straight through, and the width cut and splitter,
// which hold no beat), it follows the up-face write beat and the down-face write ready. Every
// down-face request output (address, beat and valid) is a register too, or, with `through`, may
// follow the up-face request that arrives at an empty stage in the same cycle; no ready of either
// face and nothing of the down face reaches it.
unsigned check_faces(const NodeHardware &h, bool through = false, bool merge = false) {
  const auto &ir = h.ir;
  REQUIRE(verify(ir));
  unsigned checked = 0;
  for (const auto &driver : ir.drivers) {
    const auto &port = ir.ports.at(driver.port.index);
    if (port.name != "nl_up_wreq_ready" && port.endpoint != "down_wreq" &&
        port.endpoint != "down_rreq")
      continue;
    INFO("output " << port.name);
    const auto inputs = cone_inputs(ir, driver.value.index);
    for (const auto &input : inputs) {
      INFO("input " << input);
      const bool request = input.starts_with("nl_up_wreq_") || input.starts_with("nl_up_rreq_") ||
                           input.starts_with("nl_hint_");
      const bool up_ready = port.name == "nl_up_wreq_ready";
      const bool merged_ready =
          merge && up_ready &&
          (input == "nl_down_wreq_ready" || (request && !input.ends_with("_ready")));
      CHECK((input == "nl_reset_n" || merged_ready ||
             (through && !up_ready && request && !input.ends_with("_ready"))));
    }
    ++checked;
  }
  return checked;
}
} // namespace

// The upsize merge registers its up-face ready; the width cut and the packet splitter pass the
// down face's ready back to the up face (a beat leaves in the cycle it is taken).
TEST_CASE("Width converter write stages register their up-face ready", "[converter-stages]") {
  for (unsigned data : {32U, 64U, 1024U})
    for (bool exclusive : {false, true}) {
      INFO("upsize data " << data << " exclusive " << exclusive);
      Axi4UpsizeProfile p;
      p.name = "unit";
      p.id_bits = 3;
      p.data_bits = data;
      p.source_bits = 2;
      p.exclusive = exclusive;
      const auto h = lower_axi4_upsize(p);
      REQUIRE(h);
      CHECK(check_faces(h.value(), true, true) > 10);
    }
  for (unsigned data : {64U, 128U, 1024U})
    for (bool exclusive : {false, true}) {
      INFO("clamp data " << data << " exclusive " << exclusive);
      Axi4WidthClampProfile p{"unit", 3, 8, 32, 2, data};
      p.windows = {{{0x1000, 0x1000}, 2}, {{0x10000, 0x10000}, 3}};
      p.fixed_reject = {{0x1000, 0x1000}};
      p.exclusive = exclusive;
      const auto clamp = lower_axi4_width_clamp(p);
      REQUIRE(clamp);
      CHECK(check_faces(clamp.value(), true, true) > 10);
      INFO("fixed-width cut");
      Axi4WidthCutProfile d;
      d.name = "unit";
      d.id_bits = 4;
      d.source_bits = 2;
      d.data_bits = data;
      d.down_bits = 32;
      d.fixed_width = 2;
      d.exclusive = exclusive;
      d.tag_bits = 3;
      const auto down = lower_axi4_width_cut(d);
      REQUIRE(down);
      CHECK(check_faces(down.value(), true, true) > 10);
    }
  for (unsigned data : {32U, 128U})
    for (bool fold : {false, true}) {
      INFO("packetizer data " << data << " fold " << fold);
      Axi4PacketizerProfile p;
      p.name = "unit";
      p.id_bits = 2;
      p.data_bits = data;
      p.source_bits = 1;
      p.split_narrow = fold;
      p.windows = {{{0x1000, 0x1000}, 1024, 4096, data / 8}};
      const auto h = lower_axi4_packetizer(p);
      REQUIRE(h);
      CHECK(check_faces(h.value(), true, true) > 10);
    }
}

TEST_CASE("Converted packet contexts refuse unallocated tag codes",
          "[converter-stages][serial-class]") {
  for (unsigned capacity : {3U, 64U})
    for (unsigned depth : {1U, 4U}) {
      Axi4WidthClampProfile p{"unit", 3, 8, 32, 2, 64};
      p.windows = {{{0, 65536}, 2}};
      p.tag_bits = 6;
      p.max_outstanding = capacity;
      p.packet_depth = depth;
      const auto h = lower_axi4_width_clamp(p);
      REQUIRE(h);
      // Non-power-of-two slot domains must refuse unallocated tag codes.
      if (capacity == 3) {
        std::map<std::size_t, std::uint64_t> states, inputs;
        for (std::size_t k = 0; k < h.value().ir.states.size(); ++k)
          states[k] = 0;
        for (std::size_t k = 0; k < h.value().ir.ports.size(); ++k) {
          const auto &name = h.value().ir.ports[k].name;
          inputs[k] = name == "nl_reset_n" || name == "nl_up_wreq_first";
          if (name == "nl_up_wreq_tag" || name == "nl_up_rreq_tag")
            inputs[k] = 3;
        }
        for (const auto &driver : h.value().ir.drivers)
          if (const auto &name = h.value().ir.ports[driver.port.index].name;
              name == "nl_up_wreq_ready" || name == "nl_up_rreq_ready") {
            const auto ready = evaluate_cone(h.value().ir, driver.value.index, states, inputs);
            REQUIRE(ready);
            CHECK(*ready == 0);
          }
      }
    }
}

namespace {
// Cycle stepper for a lowered component whose values are at most 64 bits.
struct Stepper {
  const HardwareIR &ir;
  std::map<std::size_t, std::uint64_t> state, in;
  std::map<std::string, std::size_t> port;
  explicit Stepper(const HardwareIR &h) : ir(h) {
    for (std::size_t p = 0; p < ir.ports.size(); ++p)
      port[ir.ports[p].name] = p;
    for (std::size_t s = 0; s < ir.states.size(); ++s)
      state[s] = ir.states[s].reset ? ir.states[s].reset->value : 0;
  }
  void set(const std::string &name, std::uint64_t value) {
    if (port.contains(name))
      in[port.at(name)] = value;
  }
  std::uint64_t out(const std::string &name) {
    const auto p = port.at(name);
    const auto d =
        std::ranges::find_if(ir.drivers, [&](const auto &x) { return x.port.index == p; });
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
} // namespace

TEST_CASE("Read prefixes survive staging and local errors retain every beat",
          "[converter-stages]") {
  for (const bool held : {false, true}) {
    Axi4InitiatorProfile p;
    p.name = "prefix";
    p.data_bits = 32;
    p.id_bits = 0;
    p.len_bits = 4;
    p.write = false;
    p.port_pipes = held;
    p.admitted_windows = std::vector<AddressWindow>{{0, 0x1000}};
    const auto lowered = lower_axi4_initiator(p);
    REQUIRE(lowered);
    Stepper s(lowered.value().ir);
    s.set("nl_reset_n", 1);
    s.set("nl_rreq_ready", 1);
    s.set("nl_hint_read_taken", 1);
    s.set("nl_axi_arvalid", 1);
    s.set("nl_axi_araddr", 0x101);
    s.set("nl_axi_arlen", 2);
    s.set("nl_axi_arsize", 0);
    s.set("nl_axi_arburst", 1);
    s.tick();
    s.set("nl_axi_arvalid", 0);
    s.set("nl_axi_araddr", 0x103);
    s.set("nl_rrsp_valid", 1);
    s.set("nl_rrsp_first", 1);
    s.set("nl_rrsp_data", 0x44332211);
    s.tick();
    CHECK(s.out("nl_axi_rvalid") == 1);
    CHECK(s.out("nl_axi_rdata") == 0x44332200);
    s.set("nl_rrsp_valid", 0);
    s.tick();
    CHECK(s.out("nl_axi_rdata") == 0x44332200);
    s.set("nl_axi_rready", 1);
    s.set("nl_rrsp_valid", 1);
    s.set("nl_rrsp_first", 0);
    s.set("nl_rrsp_data", 0x88776655);
    s.tick();
    CHECK(s.out("nl_axi_rdata") == 0x88776655);
    s.set("nl_rrsp_last", 1);
    s.set("nl_rrsp_data", 0xccbbaa99);
    s.tick();
    CHECK(s.out("nl_axi_rdata") == 0xccbbaa99);
    CHECK(s.out("nl_axi_rlast") == 1);
    s.set("nl_rrsp_valid", 0);
    if (!held)
      s.tick();
    s.set("nl_axi_rready", 0);
    s.set("nl_axi_arvalid", 1);
    s.set("nl_axi_araddr", 0x102);
    s.set("nl_axi_arlen", 0);
    s.tick();
    if (held) {
      CHECK(s.out("nl_axi_rdata") == 0xccbbaa99);
      CHECK(s.out("nl_axi_rlast") == 1);
    }
    s.set("nl_axi_arvalid", 0);
    s.set("nl_axi_rready", 1);
    s.set("nl_rrsp_valid", 1);
    s.set("nl_rrsp_first", 1);
    s.set("nl_rrsp_data", 0x11223344);
    s.tick();
    CHECK(s.out("nl_axi_rdata") == 0x11220000);
    CHECK(s.out("nl_axi_rlast") == 1);
    s.set("nl_rrsp_valid", 0);
    s.tick();
    s.set("nl_axi_arvalid", 1);
    s.set("nl_axi_araddr", 0x2000);
    s.set("nl_axi_arlen", 3);
    s.tick();
    s.tick();
    s.set("nl_axi_arvalid", 0);
    for (unsigned beat = 0; beat < 4; ++beat) {
      CHECK(s.out("nl_axi_rvalid") == 1);
      CHECK(s.out("nl_axi_rresp") == 2);
      CHECK(s.out("nl_axi_rlast") == (beat == 3));
      s.tick();
    }
    CHECK(s.out("nl_axi_rvalid") == 0);
  }
}

// A 130-beat 64-bit INCR becomes two 32-bit write pieces (256 and 4 words).
// The first piece's B arrives in the very cycle the second piece's last word
// leaves, and the second piece's B shortly after: the source sees exactly
// one B, after the second piece's B, carrying the first piece's error.
TEST_CASE("A piece sent while an earlier piece's B arrives keeps one final B",
          "[converter-stages][piece-pipeline]") {
  Axi4WidthCutProfile d;
  d.name = "unit";
  d.id_bits = 4;
  d.source_bits = 2;
  d.data_bits = 64;
  d.down_bits = 32;
  d.fixed_width = 2;
  const auto h = lower_axi4_width_cut(d);
  REQUIRE(h);
  Stepper s(h.value().ir);
  s.set("nl_reset_n", 1);
  const unsigned beats = 130;
  unsigned up = 0, words = 0, pieces_sent = 0, up_b = 0, b_given = 0, b1_at = 0, b2_at = 0;
  for (unsigned cycle = 0; cycle < 600 && up_b < 2; ++cycle) {
    const bool beat = up < beats;
    s.set("nl_up_wreq_valid", beat);
    s.set("nl_up_wreq_address", up * 8);
    s.set("nl_up_wreq_start_address", 0);
    s.set("nl_up_wreq_length", beats - 1);
    s.set("nl_up_wreq_size", 3);
    s.set("nl_up_wreq_burst", 1);
    s.set("nl_up_wreq_count", 8);
    s.set("nl_up_wreq_first", up == 0);
    s.set("nl_up_wreq_last", up == beats - 1);
    s.set("nl_up_wreq_write", 1);
    s.set("nl_up_wreq_id", 5);
    s.set("nl_up_wreq_data", 0x0123456789abcdefULL + up);
    s.set("nl_up_wreq_be", 0xff);
    s.set("nl_down_wreq_ready", 1);
    s.set("nl_up_wrsp_ready", 1);
    const bool word = s.out("nl_down_wreq_valid");
    const bool piece_end = word && s.out("nl_down_wreq_last");
    // B1 waits for the second piece's last word; B2 follows three cycles later.
    bool b = false;
    if (b_given == 0 && pieces_sent == 1 && piece_end)
      b = true, b1_at = cycle;
    if (b_given == 1 && cycle == b1_at + 3)
      b = true, b2_at = cycle;
    s.set("nl_down_wrsp_valid", b);
    s.set("nl_down_wrsp_result", b_given == 0 ? 2 : 0);
    s.set("nl_down_wrsp_first", 1);
    s.set("nl_down_wrsp_last", 1);
    s.set("nl_down_wrsp_count", 4);
    s.set("nl_down_wrsp_id", 5);
    if (b) {
      INFO("cycle " << cycle);
      CHECK(s.out("nl_down_wrsp_ready") == 1);
    }
    if (s.out("nl_up_wrsp_valid")) {
      INFO("cycle " << cycle << " B count " << b_given);
      CHECK((b && b_given == 1)); // the final B passes in the cycle B2 arrives
      CHECK(s.out("nl_up_wrsp_result") == 2);
      ++up_b;
    }
    if (beat && s.out("nl_up_wreq_ready"))
      ++up;
    if (word)
      ++words;
    if (piece_end)
      ++pieces_sent;
    if (b)
      ++b_given;
    s.tick();
  }
  CHECK(words == 260);
  CHECK(pieces_sent == 2);
  CHECK(b1_at > 0);
  CHECK(b2_at > b1_at);
  CHECK(b_given == 2);
  CHECK(up_b == 1);
}

TEST_CASE("A source lock belongs to the current write descriptor",
          "[converter-stages][serial-class]") {
  Axi4InitiatorProfile p;
  p.name = "unit";
  p.id_bits = 2;
  p.tag_bits = 2;
  p.max_outstanding = 4;
  p.source_lock = true;
  p.exclusive = true;
  p.destinations = {{0, 65536}};
  p.miss_destination = 0;
  const auto h = lower_axi4_initiator(p);
  REQUIRE(h);
  Stepper s(h.value().ir);
  s.set("nl_reset_n", 1);
  s.set("nl_axi_awvalid", 1);
  s.set("nl_axi_awaddr", 256);
  s.set("nl_axi_awlen", 3);
  s.set("nl_axi_awsize", 1);
  s.set("nl_axi_awburst", 1);
  s.set("nl_axi_awcache", 2);
  s.set("nl_axi_awid", 1);
  s.set("nl_axi_awlock", 1);
  REQUIRE(s.out("nl_axi_awready"));
  s.tick();
  s.set("nl_axi_awvalid", 0);
  for (unsigned i = 0; i < 5; ++i)
    s.tick();
  // Offer an address with different identity and attributes while the current burst has no W
  // yet (the engine is busy, so it is not taken); then change the AXI address face again.
  s.set("nl_axi_awvalid", 1);
  s.set("nl_axi_awaddr", 512);
  s.set("nl_axi_awlen", 7);
  s.set("nl_axi_awsize", 0);
  s.set("nl_axi_awcache", 0);
  s.set("nl_axi_awid", 2);
  s.set("nl_axi_awlock", 0);
  CHECK(s.out("nl_axi_awready") == 0);
  s.tick();
  s.set("nl_axi_awvalid", 0);
  s.set("nl_axi_awid", 3);
  s.set("nl_axi_awlock", 1);
  s.set("nl_axi_wvalid", 1);
  s.set("nl_axi_wstrb", 3);
  // The beat passes through: it stays on the W face while the network does not take it.
  CHECK(s.out("nl_axi_wready") == 0);
  for (unsigned i = 0; i < 8; ++i) {
    s.set("nl_axi_awlock", i % 2);
    REQUIRE(s.out("nl_wreq_valid"));
    CHECK(s.out("nl_wreq_id") == 1);
    CHECK(s.out("nl_wreq_tag") == 0);
    CHECK(s.out("nl_wreq_length") == 3);
    CHECK(s.out("nl_wreq_size") == 1);
    CHECK(s.out("nl_wreq_cache") == 2);
    CHECK(s.out("nl_hint_write_lock") == 1);
    CHECK(s.out("nl_wreq_lock") == 0); // ordinary target: Weft lock is cleared
    s.tick();
  }
}

// A 64-byte INCR read cut into four 16-byte packets: the first packet leaves in the very cycle
// the up face accepts the burst, the rest follow back to back, and only the last one carries the
// burst-final marker. A packet offered but not taken holds its fields from the resume registers.
TEST_CASE("A cut read burst's first packet leaves in its accept cycle",
          "[converter-stages][packetizer-plan]") {
  Axi4PacketizerProfile p;
  p.name = "unit";
  p.id_bits = 2;
  p.data_bits = 32;
  p.source_bits = 1;
  p.windows = {{{0x0, 0x10000}, 16, 4096, 4}};
  const auto h = lower_axi4_packetizer(p);
  REQUIRE(h);
  REQUIRE(verify(h.value().ir));
  // The down-face valid reads the up-face valid combinationally (the first packet never
  // waits a cycle in a register).
  for (const auto &driver : h.value().ir.drivers)
    if (h.value().ir.ports.at(driver.port.index).name == "nl_down_rreq_valid")
      CHECK(cone_inputs(h.value().ir, driver.value.index).contains("nl_up_rreq_valid"));
  Stepper s(h.value().ir);
  s.set("nl_reset_n", 1);
  s.tick();
  const auto marker = h.value().bindings.at("r.burst_final_packet");
  auto marker_now = [&] {
    const auto v = evaluate_cone(h.value().ir, marker.index, s.state, s.in);
    REQUIRE(v);
    return *v;
  };
  auto offer = [&](unsigned ready, unsigned address) {
    s.set("nl_up_rreq_valid", 1);
    s.set("nl_up_rreq_address", address);
    s.set("nl_up_rreq_start_address", address);
    s.set("nl_up_rreq_length", 15);
    s.set("nl_up_rreq_size", 2);
    s.set("nl_up_rreq_burst", 1);
    s.set("nl_up_rreq_cache", 3);
    s.set("nl_up_rreq_count", 4);
    s.set("nl_up_rreq_first", 1);
    s.set("nl_up_rreq_last", 1);
    s.set("nl_down_rreq_ready", ready);
  };
  auto check_packet = [&](unsigned address, bool last) {
    INFO("packet at " << address);
    CHECK(s.out("nl_down_rreq_valid") == 1);
    CHECK(s.out("nl_down_rreq_address") == address);
    CHECK(s.out("nl_down_rreq_length") == 3);
    CHECK(s.out("nl_down_rreq_size") == 2);
    CHECK(s.out("nl_down_rreq_burst") == 1);
    CHECK(marker_now() == (last ? 1 : 0));
  };
  // Burst one, offered into a stalled down face: the offer shows at once and waits; not taken,
  // it shows again from the resume registers, unchanged.
  offer(0, 0x100);
  CHECK(s.out("nl_up_rreq_ready") == 1);
  check_packet(0x100, false);
  s.tick();
  s.set("nl_up_rreq_valid", 0);
  check_packet(0x100, false);
  s.set("nl_down_rreq_ready", 1);
  s.tick();
  // Taken now; the remaining packets leave back to back.
  for (unsigned k = 0; k < 3; ++k) {
    check_packet(0x110 + 16 * k, k == 2);
    s.tick();
  }
  CHECK(s.out("nl_down_rreq_valid") == 0);
  // The burst's sixteen R beats free it; packet lasts close the four in-flight packets.
  s.set("nl_down_rrsp_valid", 1);
  s.set("nl_up_rrsp_ready", 1);
  s.set("nl_down_rrsp_count", 4);
  for (unsigned k = 0; k < 16; ++k) {
    s.set("nl_down_rrsp_last", k % 4 == 3 ? 1 : 0);
    s.tick();
  }
  s.set("nl_down_rrsp_valid", 0);
  // Burst two, accepted with the down face ready: its first packet leaves in the accept cycle.
  offer(1, 0x200);
  REQUIRE(s.out("nl_up_rreq_ready") == 1);
  check_packet(0x200, false);
  s.tick();
  s.set("nl_up_rreq_valid", 0);
  for (unsigned k = 0; k < 3; ++k) {
    check_packet(0x210 + 16 * k, k == 2);
    s.tick();
  }
}

// The packet plan reads the up face while the read side is idle (a view switched by the idle
// register): no down-face field waits for the up-face handshake (valid or the admission behind
// the ready), only the down-face valid does.
TEST_CASE("A packet's down-face fields never wait for the up-face handshake",
          "[converter-stages][packetizer-plan]") {
  for (const bool cut : {false, true}) {
    Axi4PacketizerProfile p;
    p.name = "unit";
    p.id_bits = 2;
    p.data_bits = 64;
    p.source_bits = 1;
    p.decompose_wrap_bursts = cut;
    p.split_narrow = cut;
    p.max_outstanding = cut ? 4 : 1;
    p.tag_bits = cut ? 2 : 0;
    p.windows = {{{0x0, 0x10000}, 16, 4096, 8, 8, 4, 0}, {{0x10000, 0x1000}, 64, 4096, 8, 1, 8, 1}};
    const auto h = lower_axi4_packetizer(p);
    REQUIRE(h);
    unsigned fields = 0;
    for (const auto &driver : h.value().ir.drivers) {
      const auto &port = h.value().ir.ports.at(driver.port.index);
      if (port.endpoint != "down_rreq" || port.name == "nl_down_rreq_valid")
        continue;
      INFO("field " << port.name);
      CHECK_FALSE(cone_inputs(h.value().ir, driver.value.index).contains("nl_up_rreq_valid"));
      ++fields;
    }
    CHECK(fields > 8);
  }
}

// A burst that leaves carries request fields that follow the window decode alone: the other
// admission checks (length, lock, 4 KiB span) decide whether it leaves, never what its fields
// hold, so no field waits for them.
TEST_CASE("Initiator request fields do not wait for the admission checks",
          "[converter-stages][initiator-fields]") {
  Axi4InitiatorProfile p;
  p.name = "unit";
  p.data_bits = 64;
  p.id_bits = 4;
  p.start_address = true;
  p.admitted_windows = std::vector<AddressWindow>{{0x0, 0x10000}};
  p.miss_window = AddressWindow{0x0, 0x1000};
  const auto h = lower_axi4_initiator(p);
  REQUIRE(h);
  const auto &ir = h.value().ir;
  unsigned checked = 0;
  for (const auto &driver : ir.drivers) {
    const auto &name = ir.ports.at(driver.port.index).name;
    if (name != "nl_rreq_address" && name != "nl_rreq_start_address" && name != "nl_rreq_error" &&
        name != "nl_wreq_address" && name != "nl_wreq_error")
      continue;
    INFO("field " << name);
    const auto inputs = cone_inputs(ir, driver.value.index);
    for (const auto *check : {"nl_axi_arlen", "nl_axi_arlock", "nl_axi_awlen", "nl_axi_awlock"})
      CHECK_FALSE(inputs.contains(check));
    ++checked;
  }
  CHECK(checked == 5);
}

// The width cut takes the entering beat's piece plan whatever the beat's valid (without a beat
// nothing leaves), and behind the packet splitter it plans from the splitter's handed fields: the
// piece-last test waits neither for the up face's valid nor for the packet's own address, size
// or length.
TEST_CASE("A cut piece's plan waits neither for the up-face valid nor for the packet's fields",
          "[converter-stages][piece-plan]") {
  for (const bool hints : {false, true}) {
    INFO("hints " << hints);
    Axi4WidthClampProfile p{"unit", 3, 8, 32, 2, 128};
    p.windows = {{{0x0, 0x1000}, 2}, {{0x1000, 0x1000}, 3}};
    p.packet_depth = 4;
    p.write_hints = p.read_hints = hints;
    const auto h = lower_axi4_width_clamp(p);
    REQUIRE(h);
    const auto inputs = cone_inputs(h.value().ir, h.value().bindings.at("w.piece_last").index);
    CHECK_FALSE(inputs.contains("nl_up_wreq_valid"));
    for (const auto *field :
         {"nl_up_wreq_start_address", "nl_up_wreq_address", "nl_up_wreq_size", "nl_up_wreq_length"})
      CHECK(inputs.contains(field) != hints);
  }
}

// With the source's own addresses and length on the hint face, the splitter plans a packet for
// both admission outcomes from them: the plan it hands the width cut never reads the up face's
// addresses (which a miss moves) or its length (which a miss clears).
TEST_CASE("A packet plan does not wait for the source's admission decode",
          "[converter-stages][packet-outcomes]") {
  Axi4PacketizerProfile p;
  p.name = "unit";
  p.id_bits = 2;
  p.data_bits = 64;
  p.source_bits = 1;
  p.windows = {{{0x0, 0x10000}, 256, 4096, 8, 64, 8, 0}, {{0x10000, 0x100}, 128, 256, 8, 64, 8, 1}};
  p.source_miss = AddressWindow{0x10000, 0x100};
  p.packet_hints = true;
  const auto h = lower_axi4_packetizer(p);
  REQUIRE(h);
  const auto &ir = h.value().ir;
  unsigned checked = 0;
  for (const auto &driver : ir.drivers) {
    const auto &name = ir.ports.at(driver.port.index).name;
    if (!name.starts_with("nl_hint_") ||
        (!name.ends_with("_packet_end") && !name.ends_with("_packet_size") &&
         !name.ends_with("_packet_key")))
      continue;
    INFO("hint " << name);
    const auto inputs = cone_inputs(ir, driver.value.index);
    for (const auto *field : {"nl_up_wreq_start_address", "nl_up_wreq_address", "nl_up_wreq_length",
                              "nl_up_rreq_start_address", "nl_up_rreq_address"})
      CHECK_FALSE(inputs.contains(field));
    ++checked;
  }
  CHECK(checked == 6);
}

TEST_CASE("Read words retain later beats and clip only the burst boundaries", "[upsize-context]") {
  Axi4UpsizeProfile p;
  p.name = "mask";
  p.data_bits = 32;
  p.joined = true;
  const auto h = lower_axi4_upsize(p);
  REQUIRE(h);
  for (unsigned size : {0U, 1U, 2U})
    for (unsigned length : {0U, 4U, 8U, 255U}) {
      const unsigned start = 0x101, step = 1U << size;
      const unsigned end = (start & ~(step - 1)) + (length + 1) * step;
      Stepper s(h.value().ir);
      s.set("nl_reset_n", 1);
      s.set("nl_down_rreq_ready", 1);
      s.set("nl_up_rreq_valid", 1);
      s.set("nl_up_rreq_start_address", start);
      s.set("nl_up_rreq_address", start);
      s.set("nl_up_rreq_length", length);
      s.set("nl_up_rreq_size", size);
      s.set("nl_up_rreq_burst", 1);
      REQUIRE(s.out("nl_up_rreq_ready") == 1);
      s.tick();
      s.set("nl_up_rreq_valid", 0);
      unsigned word = start & ~3U, beats = 0;
      for (unsigned cycle = 0; cycle < 1600 && beats <= length; ++cycle) {
        const bool ready = cycle % 3 != 1;
        s.set("nl_up_rrsp_ready", ready);
        s.set("nl_down_rrsp_valid", word < end);
        s.set("nl_down_rrsp_address", word);
        s.set("nl_down_rrsp_data", 0x7f7f7f7f);
        s.set("nl_down_rrsp_count", 4);
        s.set("nl_down_rrsp_first", word == (start & ~3U));
        s.set("nl_down_rrsp_last", word + 4 >= end);
        if (s.out("nl_up_rrsp_valid")) {
          const auto beat_address = beats ? (start & ~(step - 1)) + beats * step : start;
          CHECK(s.out("nl_up_rrsp_address") == beat_address);
          const auto address = beat_address & ~3U;
          std::uint64_t expected = 0;
          for (unsigned lane = 0; lane < 4; ++lane)
            if (address + lane >= start && address + lane < end)
              expected |= 0x7fULL << (lane * 8);
          INFO("size " << size << " length " << length << " beat " << beats);
          CHECK(s.out("nl_up_rrsp_data") == expected);
          CHECK(s.out("nl_up_rrsp_last") == (beats == length));
          if (ready)
            ++beats;
        }
        if (word < end && s.out("nl_down_rrsp_ready"))
          word += 4;
        s.tick();
      }
      CHECK(beats == length + 1);
      CHECK(word >= end);
    }
}

TEST_CASE("Target merges retain bounded packet contexts", "[upsize-context]") {
  for (unsigned capacity : {2U, 3U, 16U}) {
    Axi4UpsizeProfile p;
    p.name = "contexts";
    p.id_bits = 3;
    p.source_bits = 2;
    p.tag_bits = 2;
    p.data_bits = 128;
    p.max_outstanding = capacity;
    auto h = lower_axi4_upsize(p);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    CHECK(check_faces(h.value(), true, true) > 10);
    CHECK(h.value().bindings.contains("w.context" + std::to_string(capacity - 1) + ".valid"));
    CHECK_FALSE(h.value().bindings.contains("w.context" + std::to_string(capacity) + ".valid"));
    CHECK(h.value().bindings.contains("r.context" + std::to_string(capacity - 1) + ".tag"));
    CHECK(h.value().bindings.contains("r.received_last"));
    const auto bits = axi4_upsize_state_bits(p);
    unsigned write = 0, read = 0;
    for (const auto &[name, value] : h.value().bindings)
      if (h.value().ir.values.at(value.index).op == Op::State) {
        if (name.starts_with("w."))
          write += h.value().ir.values.at(value.index).width;
        if (name.starts_with("r."))
          read += h.value().ir.values.at(value.index).width;
      }
    CHECK(bits.first == write);
    CHECK(bits.second == read);
  }
  Axi4UpsizeProfile invalid;
  invalid.name = "invalid";
  for (unsigned capacity : {0U, 17U}) {
    invalid.max_outstanding = capacity;
    CHECK_FALSE(lower_axi4_upsize(invalid));
  }
}

TEST_CASE("A merged read preserves the requested first byte", "[upsize-context]") {
  for (unsigned bits : {32U, 64U, 128U})
    for (bool joined : {false, true}) {
      Axi4UpsizeProfile p;
      p.name = "first_byte";
      p.data_bits = bits;
      p.joined = joined;
      const auto h = lower_axi4_upsize(p);
      REQUIRE(h);
      const auto &ir = h.value().ir;
      const auto port = [&](const char *name) {
        const auto it = std::ranges::find(ir.ports, name, &Port::name);
        REQUIRE(it != ir.ports.end());
        return static_cast<std::size_t>(it - ir.ports.begin());
      };
      const auto state = [&](const char *name) {
        return ir.values.at(h.value().bindings.at(name).index).state.index;
      };
      const auto output = [&](const char *name) {
        const auto at = port(name);
        const auto it =
            std::ranges::find_if(ir.drivers, [&](const auto &d) { return d.port.index == at; });
        REQUIRE(it != ir.drivers.end());
        return it->value.index;
      };
      const auto size = std::countr_zero(bits / 8);
      for (unsigned offset = 1; offset < bits / 8; ++offset) {
        const unsigned address = 0x1000 + offset;
        const ConeEvaluator::Words inputs{{port("nl_reset_n"), 1},
                                          {port("nl_up_rreq_valid"), 1},
                                          {port("nl_down_rreq_ready"), 1},
                                          {port("nl_up_rreq_start_address"), address},
                                          {port("nl_up_rreq_address"), address},
                                          {port("nl_up_rreq_size"), size},
                                          {port("nl_up_rreq_length"), 1},
                                          {port("nl_up_rreq_burst"), 1}};
        INFO("bits " << bits << " joined " << joined << " offset " << offset);
        const auto first = evaluate_cone(ir, ir.states.at(state("r.beat")).next.index, {}, inputs);
        REQUIRE(first);
        CHECK(*first == address);
        const ConeEvaluator::Words states{
            {state("r.beat"), address}, {state("r.descriptor.size"), size}, {state("r.mode"), 1}};
        CHECK(evaluate_cone(ir, output("nl_up_rrsp_address"), states) == address);
        const auto count = evaluate_cone(ir, output("nl_up_rrsp_count"), states);
        REQUIRE(count);
        CHECK(*count == bits / 8 - offset);
      }
    }
}
