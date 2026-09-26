#include "core/axi4_outlet.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <set>

using namespace nocloom::detail;
using nocloom::AddressWindow;
using nocloom::ExclusiveAccess;

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
bool network_face(const std::string &endpoint) {
  return endpoint == "wreq" || endpoint == "wrsp" || endpoint == "rreq" || endpoint == "rrsp";
}
} // namespace

TEST_CASE("Burst outlet stages keep the network and target faces apart", "[axi4-outlet]") {
  // One burst per direction (plain, windowed read-only and write-only, timed, network and
  // target exclusive) and several, at the narrowest and widest data.
  std::vector<Axi4OutletProfile> profiles;
  for (unsigned width : {32U, 1024U}) {
    Axi4OutletProfile base{
        "unit", width, 4, 8, 3, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
    profiles.push_back(base);
    auto timed = base;
    timed.timeout_cycles = 1024;
    profiles.push_back(timed);
    auto read_only = timed;
    read_only.read_only = true;
    profiles.push_back(read_only);
    auto write_only = timed;
    write_only.write_only = true;
    profiles.push_back(write_only);
    auto network = base;
    network.exclusive = true;
    network.exclusive_target = {ExclusiveAccess::Network, 4, 8};
    profiles.push_back(network);
    auto target = base;
    target.exclusive = true;
    target.exclusive_target = {ExclusiveAccess::Target, 0, 1};
    target.id_sources = {0, 2};
    profiles.push_back(target);
    auto multi = base;
    multi.max_outstanding = 4;
    multi.id_sources = {0, 1, 3};
    profiles.push_back(multi);
    auto multi_target = multi;
    multi_target.exclusive = true;
    multi_target.exclusive_target = {ExclusiveAccess::Target, 0, 1};
    profiles.push_back(multi_target);
  }
  for (const auto &p : profiles) {
    INFO("data " << p.data_bits << " outstanding " << p.max_outstanding << " timeout "
                 << p.timeout_cycles << " exclusive " << static_cast<int>(p.exclusive_target.mode));
    auto lowered = lower_axi4_outlet(p);
    REQUIRE(lowered);
    const auto &ir = lowered.value().ir;
    REQUIRE(verify(ir));
    std::map<std::string, std::string> face;
    for (const auto &port : ir.ports)
      face[port.name] = port.endpoint;
    unsigned checked = 0;
    for (const auto &driver : ir.drivers) {
      const auto &port = ir.ports.at(driver.port.index);
      const auto inputs = cone_inputs(ir, driver.value.index);
      INFO("output " << port.name);
      for (const auto &input : inputs) {
        INFO("input " << input);
        // Only WREADY reaches the network W ready in the same cycle (the W entry is one forward
        // slot), and with several bursts AWREADY and ARREADY reach the network write and read
        // ready (one AW and one AR stage); a write-only outlet's B reaches its
        // network response; no network port reaches a target output.
        const bool staged = p.max_outstanding > 1;
        const bool passes =
            (port.name == "nl_wreq_ready" && input == "nl_axi_wready") ||
            (staged && port.name == "nl_wreq_ready" && input == "nl_axi_awready") ||
            (staged && port.name == "nl_rreq_ready" && input == "nl_axi_arready") ||
            (p.write_only && port.endpoint == "wrsp" && input.starts_with("nl_axi_b"));
        if (network_face(port.endpoint) && !passes)
          CHECK(face.at(input) != "axi");
        if (port.endpoint == "axi")
          CHECK_FALSE(network_face(face.at(input)));
      }
      // RREADY is a register output; at capacity one the network read ready reads only the
      // offered write head (which takes the last free entry first), and the W ready WREADY
      // besides.
      if (port.name == "nl_axi_rready")
        CHECK(inputs == std::set<std::string>{"nl_reset_n"});
      if (p.max_outstanding == 1 && port.name == "nl_rreq_ready")
        CHECK(inputs == std::set<std::string>{"nl_reset_n", "nl_wreq_valid"});
      if (p.max_outstanding == 1 && port.name == "nl_wreq_ready")
        CHECK(inputs == std::set<std::string>{"nl_reset_n", "nl_axi_wready"});
      if (p.max_outstanding > 1 && (port.name == "nl_wreq_ready" || port.name == "nl_rreq_ready"))
        for (const auto &input : inputs)
          CHECK((input == "nl_reset_n" || input == "nl_axi_wready" || input == "nl_axi_awready" ||
                 input == "nl_axi_arready" || face.at(input) == port.endpoint ||
                 (port.name == "nl_rreq_ready" && input == "nl_wreq_valid")));
      ++checked;
    }
    CHECK(checked > 40);
  }
}

TEST_CASE("Burst outlets expose independent master channels", "[axi4-outlet]") {
  for (unsigned width : {32U, 64U})
    for (unsigned id : {0U, 8U})
      for (unsigned length : {4U, 8U}) {
        const Axi4OutletProfile p{"unit", width, id, length, 7};
        auto h = lower_axi4_outlet(p);
        REQUIRE(h);
        REQUIRE(verify(h.value().ir));
        REQUIRE(emit_verilog(h.value().ir) == emit_verilog(lower_axi4_outlet(p).value().ir));
        for (const auto &port : h.value().ir.ports) {
          REQUIRE(port.name.find("svc") == std::string::npos);
          if (port.name == "nl_axi_wdata") {
            REQUIRE_FALSE(port.input);
            REQUIRE(port.width == width);
          }
          if (port.name == "nl_axi_rdata")
            REQUIRE(port.input);
        }
        auto has = [&](const std::string &n) {
          return std::ranges::any_of(h.value().ir.ports,
                                     [&](const auto &p) { return p.name == n; });
        };
        REQUIRE(has("nl_axi_awid") == (id != 0));
        REQUIRE(has("nl_axi_rid") == (id != 0));
        for (const auto *n : {"nl_axi_awvalid", "nl_axi_wvalid", "nl_axi_bready", "nl_axi_arvalid",
                              "nl_axi_rready"})
          REQUIRE(has(n));
      }
}

TEST_CASE("Burst outlet rejects unsupported geometry", "[axi4-outlet]") {
  Axi4OutletProfile p{"unit"};
  for (unsigned bits : {16U, 96U, 2048U}) {
    p.data_bits = bits;
    REQUIRE_FALSE(lower_axi4_outlet(p));
  }
  // 128- to 1024-bit outlets carry 16- to 128-byte beats (AxSIZE 4 to 7).
  for (unsigned bits : {128U, 256U, 512U, 1024U}) {
    p.data_bits = bits;
    const auto wide = lower_axi4_outlet(p);
    REQUIRE(wide);
    const auto data = std::ranges::find(wide.value().ir.ports, "nl_axi_wdata", &Port::name);
    REQUIRE(data != wide.value().ir.ports.end());
    CHECK(data->width == bits);
  }
  p.data_bits = 32;
  p.id_bits = 17;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.id_bits = 0;
  p.len_bits = 3;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.len_bits = 8;
  p.source_bits = 9;
  REQUIRE(lower_axi4_outlet(p));
  p.source_bits = 10;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.source_bits = 0;
  p.name = "bad/name";
  REQUIRE_FALSE(lower_axi4_outlet(p));
}

TEST_CASE("Burst outlet narrowing cannot truncate local offsets", "[axi4-outlet]") {
  Axi4OutletProfile p{"unit"};
  p.address_bits = 16;
  p.transport_address_bits = 40;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.mappings = {{std::uint64_t{1} << 39, 65536}};
  REQUIRE(lower_axi4_outlet(p));
  p.mappings[0].size += 4096;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.mappings[0].size = 65536;
  p.transport_address_bits = 31;
  REQUIRE_FALSE(lower_axi4_outlet(p));
  p.transport_address_bits = 48;
  REQUIRE(lower_axi4_outlet(p));
  p.transport_address_bits = 49;
  REQUIRE_FALSE(lower_axi4_outlet(p));
}
