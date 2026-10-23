// The AXI4 burst exchange on the public path: rejection matrix, transport layout
// pinning and determinism.
#include "../endpoint_ports.hpp"
#include "../public_artifacts.hpp"
#include "core/message.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/width_adapter.hpp"
#include <algorithm>
#include <bit>
#include <catch2/catch_test_macros.hpp>
#include <string>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
// Two AXI4 sources and targets over two-level graphs; laddered switch nodes carry wider words.
Definition axi4_def(unsigned data_bits = 32, unsigned id_bits = 4, unsigned len_bits = 8,
                    bool laddered = true) {
  Definition d;
  d.connectivity = Connectivity{.all = true};
  d.name = "axi4x";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  d.targets = {{"t0", "main", {{0, 65536}}}, {"t1", "main", {{65536, 65536}}}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = data_bits;
  }
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = data_bits;
  }
  endpoint_ports(d, id_bits, len_bits);
  const auto word = [&](unsigned bytes) { return laddered ? std::optional(bytes) : std::nullopt; };
  ExchangeTopology topology;
  topology.request = {{{"a", word(8)}, {"b", word(8)}, {"m", word(16)}},
                      {{"a", "m"}, {"b", "m"}},
                      {{"s0", "a"}, {"s1", "b"}},
                      {{"t0", "m"}, {"t1", "m"}}};
  topology.response = {{{"x", word(8)}, {"y", word(8)}, {"n", word(16)}},
                       {{"x", "n"}, {"y", "n"}},
                       {{"t0", "x"}, {"t1", "y"}},
                       {{"s0", "n"}, {"s1", "n"}}};
  d.topology = topology;
  return d;
}
void rejected(const Definition &d, const std::string &code) {
  CHECK(rejected_with(make_plan(d), code));
}
} // namespace

TEST_CASE("Multi-burst sources compose with bounded packets", "[axi4-exchange]") {
  auto d = axi4_def();
  d.transport = TransportOptions{.max_outstanding = 2};
  REQUIRE(generate(d));
}

TEST_CASE("Read interleave follows the target's default contexts", "[axi4-exchange]") {
  auto d = axi4_def();
  d.targets[0].access = Access::ReadOnly;
  d.targets[0].id_width = 0;
  auto plan = build_network_plan(d);
  REQUIRE(plan);
  CHECK_FALSE(interleaved_target(plan.value(), plan.value().targets[0]));
  CHECK(interleaved_target(plan.value(), plan.value().targets[1]));
  d.targets[0].id_width = 1;
  plan = build_network_plan(d);
  REQUIRE(plan);
  CHECK(interleaved_target(plan.value(), plan.value().targets[0]));
}

TEST_CASE("Read interleave reaches a merged target with any bursts in flight", "[axi4-exchange]") {
  auto d = axi4_def();
  d.targets[0].data_width = 64;
  auto plan = build_network_plan(d);
  REQUIRE(plan);
  REQUIRE(upsize_targets(plan.value()).contains(d.targets[0].name));
  REQUIRE(plan.value().targets[0].outstanding == 1);
  CHECK(interleaved_target(plan.value(), plan.value().targets[0]));
  CHECK(axi4_outlet_profile(plan.value(), 0).interleave);
  CHECK(upsize_profile(plan.value(), 0).joined);
  CHECK(axi4_outlet_profile(plan.value(), 0).one_head);
  d.targets[0].max_outstanding = 2;
  plan = build_network_plan(d);
  REQUIRE(plan);
  CHECK(interleaved_target(plan.value(), plan.value().targets[0]));
  CHECK(upsize_profile(plan.value(), 0).joined);
  CHECK_FALSE(axi4_outlet_profile(plan.value(), 0).one_head);
}

TEST_CASE("AXI4 exchange rejection matrix", "[axi4-exchange]") {
  SECTION("data width") {
    auto d = axi4_def();
    for (auto &i : d.initiators)
      i.data_width = 16;
    for (auto &t : d.targets)
      t.data_width = 16;
    rejected(d, "NCL_ENDPOINT_DATA_WIDTH");
  }
  SECTION("id width") {
    auto d = axi4_def();
    d.initiators[0].id_width = 17;
    rejected(d, "NCL_ENDPOINT_TRANSACTION_WIDTH");
  }
  SECTION("len width") {
    auto d = axi4_def();
    d.initiators[0].len_width = 9;
    rejected(d, "NCL_ENDPOINT_TRANSACTION_WIDTH");
  }
  SECTION("capacity zero") {
    auto d = axi4_def();
    d.transport = TransportOptions{.max_outstanding = 0};
    rejected(d, "NCL_AXI4_CAPACITY");
  }
  SECTION("capacity beyond 64 shared bursts") {
    auto d = axi4_def();
    d.transport = TransportOptions{.max_outstanding = 65};
    rejected(d, "NCL_AXI4_CAPACITY");
    d.transport.reset();
    d.initiators[0].max_outstanding = 0;
    rejected(d, "NCL_AXI4_CAPACITY");
  }
  SECTION("multi-burst targets keep at most sixteen bursts") {
    auto d = axi4_def();
    d.targets[0].max_outstanding = 17;
    rejected(d, "NCL_AXI4_CAPACITY");
  }
  SECTION("multi-burst targets admit equal-width bounded packets") {
    auto d = axi4_def();
    d.transport = TransportOptions{.max_outstanding = 2};
    d.targets[0].max_outstanding = 2;
    const auto a = public_artifacts(d);
    CHECK(a.map["definition"]["outstanding"]["targets"]["t0"] == 2);
    CHECK(a.map["definition"]["outstanding"]["targets"]["t1"] == 1);
  }
  SECTION("window base must be 4 KiB aligned") {
    auto d = axi4_def();
    d.targets[0].mappings[0].base = 2048;
    rejected(d, "NCL_ADDRESS_RANGE");
  }
  SECTION("window size must be whole 4 KiB blocks or a power of two") {
    auto d = axi4_def();
    d.targets[0].mappings[0].size = 3072;
    rejected(d, "NCL_ADDRESS_RANGE");
  }
  SECTION("window overlap of one size") {
    auto d = axi4_def();
    d.targets[1].mappings = {{0, 65536}};
    rejected(d, "NCL_ADDRESS_OVERLAP");
  }
  SECTION("bad transport word") {
    auto d = axi4_def();
    d.topology->request.nodes[2].transport_word_bytes = 12;
    rejected(d, "NCL_AXI4_EXCHANGE_PROFILE");
  }
  SECTION("a disconnected switch graph leaves a pair without a route") {
    auto d = axi4_def();
    d.topology->request.links.pop_back();
    rejected(d, "NCL_SWITCH_ROUTE_MISSING");
  }
  SECTION("source count") {
    auto d = axi4_def();
    d.initiators.push_back(d.initiators[0]);
    d.initiators.back().name = "s2";
    rejected(d, "NCL_SWITCH_PAIR");
  }
}

TEST_CASE("Burst transport layout matches the endpoint contract field for field",
          "[axi4-exchange]") {
  // At the endpoint word the transport fragment layout is the burst contract
  // layout with the wide payload split into 32-bit lanes.
  for (unsigned data : {32U, 64U})
    for (unsigned id : {0U, 4U})
      for (unsigned len : {4U, 8U}) {
        const BurstProfile burst{id, len};
        const unsigned word = data / 8;
        for (bool response : {false, true}) {
          const auto transport = fragment_fields(response, word, 2, burst);
          const auto contract = response ? burst_response_fields(data, burst, 2)
                                         : burst_request_fields(data, burst, 2);
          // Same envelope: control bundle, count, error, first, last in the
          // contract's order; a request run carries its extent (a byte offset in the word, as
          // wide as a byte offset there) in the count's place, then its ends and wrap_end
          // flags as the contract's.
          std::size_t ci = 0;
          for (const auto &[name, width] : transport) {
            if (name.starts_with("data") || name.starts_with("be"))
              continue;
            INFO("field " + name + " at " + std::to_string(ci));
            REQUIRE(ci < contract.size());
            if (name == "wrap_end" && response)
              continue;
            const bool reach = name == (response ? "reach" : "extent");
            REQUIRE(contract[ci].first == (reach ? "count" : name));
            REQUIRE((reach ? std::countr_zero(word) : contract[ci].second) == width);
            ++ci;
          }
          REQUIRE(ci + (response ? 1 : 2) == contract.size());
          // The payload lanes reassemble to the contract's wide field.
          unsigned lane_bits = 0;
          for (const auto &[name, width] : transport)
            if (name.starts_with("data"))
              lane_bits += width;
          REQUIRE(lane_bits == data);
        }
      }
}

TEST_CASE("AXI4 exchange lowering is deterministic", "[axi4-exchange]") {
  const auto first = generate(axi4_def());
  REQUIRE(first);
  const auto second = generate(axi4_def());
  REQUIRE(second);
  CHECK(first.value() == second.value());
}

TEST_CASE("Real AXI target composition preserves raw addresses through adapters",
          "[axi4-exchange]") {
  for (unsigned width : {32U, 64U})
    for (bool ladder : {false, true}) {
      const auto a = public_artifacts(axi4_def(width, 4, 8, ladder));
      for (const auto *target : {"t0", "t1"}) {
        const auto prefix = "nl_t_" + std::string(target) + "_";
        CHECK(a.has_port(prefix + "awaddr"));
        CHECK(a.inputs.count(prefix + "awaddr") == 0);
        CHECK(a.outputs.count(prefix + "awaddr") == 1);
        CHECK(std::ranges::none_of(a.map["ports"], [&](const auto &p) {
          return p["name"].template get<std::string>().starts_with(prefix + "svc");
        }));
      }
      CHECK(std::ranges::none_of(a.map["network"]["resources"],
                                 [](const auto &r) { return r["role"] == "burst_service_grant"; }));
    }
}

TEST_CASE("Lite and APB source protocols are checked", "[axi4-exchange]") {
  auto d = axi4_def();
  set_protocol(d.initiators[1], Protocol::Axi4Lite);
  REQUIRE(generate(d));
  // An APB2 face is never an initiator.
  set_protocol(d.initiators[0], Protocol::Apb2);
  rejected(d, "NCL_UNSUPPORTED_PROTOCOL");
}

TEST_CASE("AXI4 source width bindings govern real ports and storage accounting",
          "[axi4-exchange]") {
  auto d = axi4_def();
  d.initiators[1].data_width = 256;
  const auto a = public_artifacts(d);
  CHECK(a.port_width("nl_i_s0_rdata") == 32);
  CHECK(a.port_width("nl_i_s1_rdata") == 256);
  // The storage bound to each source resource holds the payload the resource declares.
  for (const auto *resource :
       {"source/s0/write", "source/s0/read", "source/s1/write", "source/s1/read"}) {
    INFO(resource);
    const auto *r = a.resource(resource);
    REQUIRE(r != nullptr);
    CHECK(a.storage_bits(resource, "payload") == (*r)["payload_bits"].get<unsigned>());
  }
  CHECK((*a.resource("source/s1/read"))["payload_bits"] !=
        (*a.resource("source/s0/read"))["payload_bits"]);
}

TEST_CASE("Exchange validates small source addresses by protocol", "[axi4-exchange]") {
  for (auto protocol : {Protocol::Apb4, Protocol::Axi4Lite, Protocol::Axi4}) {
    auto d = axi4_def();
    set_protocol(d.initiators[0], protocol);
    const unsigned minimum = protocol == Protocol::Apb4       ? 1U
                             : protocol == Protocol::Axi4Lite ? 2U
                                                              : 12U;
    d.initiators[0].address_width = minimum;
    // t1 lies above the small address spaces.
    d.connectivity = Connectivity{.pairs = {{"s0", "t0"}, {"s1", "t0"}, {"s1", "t1"}}};
    INFO(minimum);
    CHECK(make_plan(d));
    d.initiators[0].address_width = minimum - 1;
    rejected(d, "NCL_ENDPOINT_ADDRESS_WIDTH");
  }
}

TEST_CASE("Exchange mappings are whole pages or size-aligned sub-pages", "[axi4-exchange]") {
  auto d = axi4_def(32, 0, 8, false);
  set_protocol(d.initiators[0], Protocol::Axi4Lite);
  set_protocol(d.initiators[1], Protocol::Axi4Lite);
  set_protocol(d.targets[0], Protocol::Apb4);
  set_protocol(d.targets[1], Protocol::Axi4Lite);
  d.targets[0].mappings = {{0x1000, 0x100}};
  d.targets[1].mappings = {{0x2000, 0x80}};
  // The map keeps each sub-page window; the outlet decodes it inside its page.
  const auto a = public_artifacts(d);
  CHECK(a.map["definition"]["targets"][0]["begin"] == "4096");
  CHECK(a.map["definition"]["targets"][0]["end"] == "4352");
  for (const auto &window :
       {AddressWindow{0x1040, 0x80}, AddressWindow{0x2000, 0x90}, AddressWindow{0x2100, 4096}}) {
    d.targets[1].mappings = {{window.base, window.size}};
    CHECK_FALSE(make_plan(d));
  }
  // An AXI4 target takes sub-pages of at least one of its words.
  d.targets[1].mappings = {{0x2000, 0x80}};
  set_protocol(d.targets[1], Protocol::Axi4);
  d.targets[1].len_width = 8;
  CHECK(make_plan(d));
  d.targets[1].mappings = {{0x2000, 4}};
  d.targets[1].data_width = 64;
  rejected(d, "NCL_ADDRESS_RANGE");
}
