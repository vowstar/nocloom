#include "../endpoint_ports.hpp"
#include "../packet_lengths_profile.hpp"
#include "core/ahb_outlet.hpp"
#include "core/axi4_initiator.hpp"
#include "core/axi4_outlet.hpp"
#include "core/burst_faces.hpp"
#include "core/decode_packet.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/width_adapter.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/generation.hpp>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
unsigned port_bits(const NodeHardware &h, const std::string &name) {
  const auto it = std::ranges::find(h.ir.ports, name, &Port::name);
  REQUIRE(it != h.ir.ports.end());
  return it->width;
}
void checked(const Result<NodeHardware> &h) {
  REQUIRE(h);
  REQUIRE(verify(h.value().ir));
}
} // namespace

TEST_CASE("Packet lengths keep AXI ports within their own domain", "[packet-lengths]") {
  for (unsigned bits : {9U, 10U}) {
    Axi4InitiatorProfile source;
    source.name = "lengths";
    source.packet_len_bits = bits;
    for (unsigned data : {32U, 64U, 128U, 256U, 512U, 1024U}) {
      source.data_bits = data;
      const auto h = lower_axi4_initiator(source);
      checked(h);
      CHECK(port_bits(h.value(), "nl_axi_arlen") == 8);
      CHECK(port_bits(h.value(), "nl_rreq_length") == bits);
    }
    source.axi_len_bits = 9;
    CHECK_FALSE(lower_axi4_initiator(source));
    source.axi_len_bits = 8;
    source.packet_len_bits = 7;
    CHECK_FALSE(lower_axi4_initiator(source));
    Axi4OutletProfile target;
    target.name = "lengths";
    target.packet_len_bits = bits;
    CHECK_FALSE(lower_axi4_outlet(target));
    target.axi_len_bits = 8;
    const auto out = lower_axi4_outlet(target);
    checked(out);
    CHECK(port_bits(out.value(), "nl_axi_arlen") == 8);
    CHECK(port_bits(out.value(), "nl_rreq_length") == bits);
    target.axi_len_bits = 9;
    CHECK_FALSE(lower_axi4_outlet(target));
  }
}

TEST_CASE("Network profiles propagate packet widths without widening AXI", "[packet-lengths]") {
  Definition d;
  d.name = "lengths";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  d.initiators = {{"source", "main"}};
  d.targets = {{"target", "main", {{0, 65536}}}};
  d.initiators[0].protocol = d.targets[0].protocol = Protocol::Axi4;
  d.initiators[0].data_width = 128;
  d.initiators[0].default_target = "target";
  d.targets[0].data_width = 32;
  d.connectivity = Connectivity{.all = true};
  endpoint_ports(d, 0, 8);
  auto result = build_network_plan(d);
  REQUIRE(result);
  auto plan = result.value();
  CHECK(network_len_bits(plan) == 8);
  CHECK(pair_limits(plan, plan.pairs[0]).max_bytes == 1024);
  for (unsigned bits : {9U, 10U}) {
    plan.packet_len_bits = bits;
    CHECK(initiator_profile(plan, 0).axi_len_bits == 8);
    CHECK(initiator_profile(plan, 0).packet_bits() == bits);
    CHECK(packet_profile(plan, 0).packet_len_bits == bits);
    CHECK(clamp_profile(plan, 0).packet_len_bits == bits);
    CHECK(axi4_outlet_profile(plan, 0).packet_len_bits == bits);
    CHECK(axi4_outlet_profile(plan, 0).axi_len_bits == 8);
    CHECK(endpoint_burst(plan).packet_len_bits == bits);
    CHECK(transport_burst(plan).packet_len_bits == bits);
    CHECK(pair_limits(plan, plan.pairs[0]).max_bytes == 1024);
    const auto packets = packet_profile(plan, 0);
    REQUIRE(packets.fallback);
    CHECK(packets.fallback->max_bytes == (4U << bits));
    const auto clamp = clamp_profile(plan, 0);
    REQUIRE(clamp.piece_len_bits.size() == 1);
    CHECK(clamp.piece_len_bits[0].second == 8);
    plan.sources[0].clamp = axi4_width_clamp_state_bits(clamp);
    if (plan.sources[0].packets)
      plan.sources[0].packets = axi4_packetizer_state_bits(packet_profile(plan, 0));
    if (plan.targets[0].upsize)
      plan.targets[0].upsize = axi4_upsize_state_bits(upsize_profile(plan, 0));
    plan.graph = build_axi4_exchange_obligations(plan);
    const auto hardware = lower_axi4_exchange_composition(plan);
    if (!hardware)
      for (const auto &diagnostic : hardware.diagnostics())
        UNSCOPED_INFO(diagnostic.code << ": " << diagnostic.field << ": " << diagnostic.message);
    REQUIRE(hardware);
    CHECK(verify_network_decode(hardware.value()));
    CHECK(verify_axi4_exchange_composition(hardware.value()));
  }
}

namespace {
// One 64-bit AXI4 initiator whose misses go to a 32-bit AXI4 target at 0 to 64 KiB.
Definition miss_definition() {
  Definition d;
  d.name = "misses";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  d.initiators = {{"cpu", "main"}};
  d.targets = {{"ram", "main", {{0, 0x10000}}}};
  d.initiators[0].protocol = d.targets[0].protocol = Protocol::Axi4;
  d.initiators[0].data_width = 64;
  d.initiators[0].default_target = "ram";
  d.targets[0].data_width = 32;
  d.connectivity = Connectivity{.all = true};
  endpoint_ports(d, 4, 8);
  return d;
}
// The packet length width the map reports and the miss window limits the planner selects.
std::tuple<unsigned, std::uint64_t, std::uint64_t> miss_limits(const Definition &d) {
  const auto generated = generate(d);
  for (const auto &diagnostic : generated.diagnostics())
    UNSCOPED_INFO(diagnostic.code << ": " << diagnostic.field << ": " << diagnostic.message);
  REQUIRE(generated);
  const auto map = std::ranges::find_if(
      generated.value(), [](const Artifact &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != generated.value().end());
  const auto len_bits =
      nlohmann::json::parse(map->text)["definition"]["transport"]["len_bits"].get<unsigned>();
  const auto plan = build_network_plan(d);
  REQUIRE(plan);
  CHECK(plan.value().packet_len_bits == len_bits);
  const auto packets = packet_profile(plan.value(), 0);
  REQUIRE(packets.fallback);
  return {len_bits, packets.fallback->max_bytes, packets.fallback->boundary_bytes};
}
} // namespace

// The examples in docs/packets.md#miss-packet-limit.
TEST_CASE("Named default miss limits follow the packet length width", "[packet-lengths]") {
  auto d = miss_definition();
  CHECK(miss_limits(d) == std::tuple{8U, 1024U, 4096U});
  d.targets.push_back({"dma", "main", {{0x10000, 0x10000}}});
  set_protocol(d.targets[1], Protocol::TlUh);
  d.targets[1].attributes.reset();
  d.targets[1].data_width = 32;
  d.targets[1].max_transfer_bytes = 4096;
  d.targets[1].size_width = 4;
  CHECK(miss_limits(d) == std::tuple{9U, 2048U, 4096U});
  // A packet limit of another pair lowers the width and with it the miss limit.
  d.transport = TransportOptions{};
  d.transport->packet_limits = {{"cpu", "dma", 1024, std::nullopt}};
  CHECK(miss_limits(d) == std::tuple{8U, 1024U, 4096U});
}

TEST_CASE("Named default miss limits take the burst limit and alignment floor",
          "[packet-lengths]") {
  auto d = miss_definition();
  SECTION("16-word initiator bursts") {
    d.initiators[0].len_width = 4;
    CHECK(miss_limits(d) == std::tuple{5U, 128U, 4096U});
  }
  SECTION("a 4-byte mapping boundary below an 8-byte target word") {
    d.initiators[0].data_width = 32;
    d.targets[0].data_width = 64;
    d.targets.push_back(d.targets[0]);
    d.targets[1].data_width = 32;
    d.targets[1].name = "csr";
    d.targets[1].mappings = {{0x10004, 4}};
    CHECK(miss_limits(d) == std::tuple{8U, 8U, 8U});
  }
}

TEST_CASE("Packet consumers retain nine and ten length bits", "[packet-lengths]") {
  for (unsigned bits : {9U, 10U}) {
    CAPTURE(bits);
    for (const auto *kind : {"packetizer", "cut", "clamp", "upsize"}) {
      CAPTURE(kind);
      const auto h = packet_length_hardware(kind, bits);
      checked(h);
      CHECK(port_bits(h.value(), "nl_up_rreq_length") == bits);
      CHECK(port_bits(h.value(), "nl_down_rreq_length") == bits);
      unsigned counters = 0;
      for (const auto &[name, value] : h.value().bindings)
        if (name.ends_with("remaining") && h.value().ir.values[value.index].op == Op::State) {
          CHECK(h.value().ir.values[value.index].width >= bits);
          ++counters;
        }
      CHECK(counters > 0);
      CHECK_FALSE(packet_length_hardware(kind, 11));
    }
    BurstOutletProfile bus{"lengths", 0, bits};
    checked(lower_ahb_outlet(bus));
    checked(lower_apb_burst_outlet(bus));
    BurstProfile burst{0, bits};
    burst.segments = burst.response_runs = true;
    for (unsigned bytes : {8U, 16U, 32U}) {
      WidthAdapterParameters p{bytes, bytes / 2, false, 0, burst};
      const auto request = lower_width_serializer(p);
      checked(request);
      CHECK(port_bits(request.value(), "nl_wide_length") == bits);
      checked(lower_width_deserializer(p));
      p.response = true;
      const auto response = lower_width_deserializer(p);
      checked(response);
      CHECK(port_bits(response.value(), "nl_narrow_cut") == 1);
      CHECK(port_bits(response.value(), "nl_narrow_wrap_end") == 1);
      p.burst->packet_len_bits = 11;
      CHECK_FALSE(plan_width_adapter(p));
    }
    logic::Logic l;
    const auto length = l.in("test", "length", bits);
    const auto encoded = decode_packet::word_data(l, length, 128);
    const auto decoded = decode_packet::length_of(l, encoded, bits);
    CHECK(l.width(decoded) == bits);
    CHECK(l.width(wrap_end_of(l, l.bit(1), l.num(2, 2), length, l.num(3, 2),
                              l.in("test", "address", 32))) == 1);
  }
}
