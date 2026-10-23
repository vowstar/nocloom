#include "core/axi4_initiator.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>

namespace {
// Single-fault injection on named observation bindings for the designated
// negative controls; each fault breaks exactly one packetization
// responsibility and must be caught by its designated bench checker.
bool inject(nocloom::detail::NodeHardware &hardware, const std::string &fault) {
  using nocloom::detail::Op;
  auto &ir = hardware.ir;
  auto &bindings = hardware.bindings;
  auto require = [&](const std::string &key) -> nocloom::detail::Value & {
    return ir.values.at(bindings.at(key).index);
  };
  if (fault == "write_high_lost" || fault == "read_high_lost" || fault == "strobe_high_lost") {
    const auto name = fault == "write_high_lost"  ? "nl_wreq_data"
                      : fault == "read_high_lost" ? "nl_axi_rdata"
                                                  : "nl_wreq_be";
    for (auto &driver : ir.drivers) {
      const auto &port = ir.ports.at(driver.port.index);
      if (port.name != name)
        continue;
      if (port.width != (fault == "strobe_high_lost" ? 16U : 128U))
        return false;
      const nocloom::detail::ValueId mask{ir.values.size()};
      ir.values.push_back(
          {port.width,
           Op::Constant,
           {},
           fault == "strobe_high_lost" ? 0x7fffU : std::numeric_limits<std::uint64_t>::max()});
      const nocloom::detail::ValueId filtered{ir.values.size()};
      ir.values.push_back({port.width, Op::BitAnd, {driver.value, mask}});
      driver.value = filtered;
      return true;
    }
    return false;
  }
  if (fault == "premature_last") {
    // The fragment last marker compares the remaining counter with itself, so
    // every beat fragment closes the packet.
    auto &value = require("wr.frag_last");
    if (value.op != Op::Equal)
      return false;
    value.operands[1] = value.operands[0];
    return true;
  }
  if (fault == "count_load_one") {
    // The burst counter loads the constant one instead of AxLEN, so the packet
    // closes after the second beat regardless of the descriptor: the AxLEN of a
    // burst passing straight through (wr.left) and of a queued one (the
    // counter's load).
    auto &left = require("wr.left");
    auto &counter = require("wr.remaining");
    auto &load = ir.values.at(ir.states.at(counter.state.index).next.index);
    if (left.op != Op::Select || load.op != Op::Select)
      return false;
    left.operands[1] = bindings.at("wr.beat_step");
    load.operands[1] = bindings.at("wr.beat_step");
    return true;
  }
  if (fault == "address_hold") {
    // INCR bursts stop advancing the beat container base.
    auto &value = require("wr.addr_next_mode");
    if (value.op != Op::Select)
      return false;
    value.operands[2] = bindings.at("wr.beat_addr");
    return true;
  }
  if (fault == "wrap_turn_stuck") {
    // The wrap fold fires on every beat, so WRAP bursts stick to the region
    // base after the first advance.
    auto &value = require("wr.wrap_turn");
    if (value.op != Op::Equal)
      return false;
    value.operands[1] = value.operands[0];
    return true;
  }
  if (fault == "wlast_blind") {
    // The WLAST mismatch detector is tied off: poisoned bursts complete with
    // the network result instead of SLVERR.
    auto &value = require("wr.wlast_mismatch");
    if (value.op != Op::BitOr)
      return false;
    value.operands[0] = bindings.at("c.zero");
    value.operands[1] = bindings.at("c.zero");
    return true;
  }
  if (fault == "span_blow") {
    // The span of a size-0 burst is computed as eight bytes per beat, so a
    // legal burst that exactly fits the 4 KiB region is falsely rejected. The span selects by the
    // AxSIZE bits, high bit first: sizes 0..3 under the false arm, size 0 under two more false
    // arms, size 3 under two true arms of the sizes 2..3 select.
    auto &value = require("wr.span");
    if (value.op != Op::Select)
      return false;
    const auto low = ir.values.at(value.operands[2].index);
    if (low.op != Op::Select || ir.values.at(low.operands[1].index).op != Op::Select)
      return false;
    auto &low01 = ir.values.at(low.operands[2].index);
    if (low01.op != Op::Select)
      return false;
    low01.operands[2] = ir.values.at(low.operands[1].index).operands[1];
    return true;
  }
  if (fault == "early_w") {
    // WREADY ignores whether a burst issues: a W beat is taken while no AW is in service, ahead
    // of its AW.
    auto &value = require("wr.refill");
    if (value.op != Op::BitAnd)
      return false;
    value.operands[1] = value.operands[0];
    return true;
  }
  if (fault == "rlast_drop") {
    // RLAST is tied low: read bursts never terminate at the AXI face.
    for (auto &driver : ir.drivers) {
      if (ir.ports.at(driver.port.index).name == "nl_axi_rlast") {
        driver.value = bindings.at("c.zero");
        return true;
      }
    }
    return false;
  }
  return false;
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 6 && argc != 7)
    return 2;
  nocloom::detail::Axi4InitiatorProfile profile;
  profile.name = argv[2];
  profile.data_bits = std::stoul(argv[3]);
  profile.len_bits = std::stoul(argv[4]);
  profile.id_bits = std::stoul(argv[5]);
  auto lowered = nocloom::detail::lower_axi4_initiator(profile);
  if (!lowered) {
    for (const auto &d : lowered.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  if (argc == 7 && std::string(argv[6]) == "print_bindings") {
    for (const auto &[name, id] : lowered.value().bindings)
      std::cout << name << " nl_internal_v" << id.index << '\n';
    return 2;
  }
  if (argc == 7 && !inject(lowered.value(), argv[6]))
    return 2;
  const std::filesystem::path out = argv[1];
  std::filesystem::create_directories(out);
  std::ofstream rtl(out / "network.v");
  rtl << nocloom::detail::emit_verilog(lowered.value().ir);
  rtl.close();
  return rtl ? 0 : 1;
}
