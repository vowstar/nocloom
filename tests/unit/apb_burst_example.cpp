#include "../endpoint_ports.hpp"
#include "../graph_clocked_definition.hpp"
#include "../public_artifacts.hpp"
#include "core/ahb_outlet.hpp"
#include "core/axi4_outlet.hpp"
#include "core/ir_cone.hpp"
#include "core/width_adapter.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <numeric>

namespace {
// One owner flag per source, its value the source's position: an exclusive access names its owner.
void owner_flag(nocloom::Definition &d, nocloom::Target &t) {
  using nocloom::AttributeSource;
  d.transport->user_flags = {"owner"};
  for (std::size_t i = 0; i < d.initiators.size(); ++i)
    d.initiators[i].user_flags["owner"] = {i % 2 ? AttributeSource::One : AttributeSource::Zero};
  t.exclusive_id_flags = {"owner"};
}
} // namespace

namespace {
// Every endpoint of a schema 10 definition at one data width.
void endpoint_widths(nocloom::Definition &d, unsigned bits) {
  for (auto &i : d.initiators)
    i.data_width = bits;
  for (auto &t : d.targets)
    t.data_width = bits;
}
nocloom::Definition public_apb() {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "burst_apb";
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu0", "main"}, {"cpu1", "main"}};
  d.targets = {{"bank0", "main", {{0x1000, 4096}}}, {"bank1", "main", {{0x5000, 4096}}}};
  for (auto &i : d.initiators)
    i.protocol = Protocol::Axi4;
  for (auto &t : d.targets)
    t.protocol = Protocol::Axi4;
  endpoint_widths(d, 32);
  d.targets[1].protocol = Protocol::Apb4;
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  endpoint_ports(d, 4, 8);
  return d;
}
// The shared APB grant of a dedicated target: its declared payload, the storage bound to it and
// the physical APB ports.
void apb_storage(const PublicArtifacts &a, const std::string &target) {
  const auto id = "burst_service/" + target + "/bus";
  const auto *resource = a.resource(id);
  REQUIRE(resource != nullptr);
  CHECK((*resource)["role"] == "burst_apb_grant");
  // The window's address bits, mask and protection; a serial outlet drives PWDATA from its word
  // buffer.
  CHECK((*resource)["payload_bits"] == 19);
  CHECK(a.storage_bits(id, "payload") == 19);
  // Phase and owner, the arrival order (a 3-bit packet count and its direction) and, at a default
  // target, the decode miss head seen, a miss being answered and its response having left.
  CHECK(a.storage_bits(id, "control") == 10);
  for (const auto *signal : {"psel", "penable", "pready", "pslverr", "paddr", "pwrite", "pwdata",
                             "pstrb", "pprot", "prdata"})
    CHECK(a.has_port("nl_t_" + target + "_" + signal));
}
} // namespace

TEST_CASE("Public burst APB4 funnel emits real ports and the same API and YAML artifacts",
          "[apb-burst-example]") {
  const auto d = public_apb();
  const auto api = nocloom::generate(d);
  REQUIRE(api);
  REQUIRE(api.value().size() == 6);
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  const auto yaml = nocloom::generate(parsed.value().definition);
  REQUIRE(yaml);
  CHECK(api.value() == yaml.value());
  const auto rtl = std::ranges::find_if(api.value(), [](const auto &a) {
    return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
           a.path != "nocloom_synchronizer.v";
  });
  REQUIRE(rtl != api.value().end());
  CHECK(rtl->text.find("nl_t_bank1_psel") != std::string::npos);
  CHECK(rtl->text.find("nl_t_bank1_awaddr") == std::string::npos);
  const auto map = std::ranges::find_if(
      api.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != api.value().end());
  CHECK(map->text.find("apb_targets") != std::string::npos);
  CHECK(map->text.find("PSLVERR_at_completion_to_SLVERR_later_beats_continue") !=
        std::string::npos);
}

TEST_CASE("Burst APB4 graph and clocked graph bind the actual shared grant",
          "[apb-burst-example]") {
  using nocloom::Protocol;
  for (unsigned count : {1U, 3U}) {
    auto d = graph_example_endpoints(count);
    for (unsigned i = 0; i < count; ++i)
      d.targets[i].mappings = {{4096ULL * i, 4096}};
    set_protocol(d.targets[0], Protocol::Apb4);
    if (count > 1)
      set_protocol(d.targets[2], Protocol::AhbV3);
    apb_storage(public_artifacts(d), "t0");
    // The APB target in a clock domain of its own: the grant lives there.
    const auto clocked = public_artifacts(target_domain(d));
    apb_storage(clocked, "t0");
    CHECK((*clocked.resource("burst_service/t0/bus"))["clock_domain"] == "io");
  }
}

TEST_CASE("Burst APB4 rejects unsupported public widths dialects and windows",
          "[apb-burst-example]") {
  using D = nocloom::Definition;
  for (const auto &change : std::vector<std::function<void(D &)>>{
           [](D &d) { endpoint_widths(d, 64); }, [](D &d) { d.address_width = 40; },
           [](D &d) { d.targets[1].address_width = 11; },
           [](D &d) { d.targets[1].mappings[0].size = 3072; },
           [](D &d) { d.targets[1].mappings[0].base += 4; },
           [](D &d) { d.targets[0].mappings[0].size = 384; }}) {
    auto d = public_apb();
    change(d);
    CHECK_FALSE(nocloom::make_plan(d));
  }
  // Power-of-two sub-page windows and narrow local ports under a 40-bit
  // system address are admitted.
  for (const auto &change :
       std::vector<std::function<void(D &)>>{[](D &d) { d.targets[1].mappings[0].size = 256; },
                                             [](D &d) {
                                               d.address_width = 40;
                                               d.targets[1].address_width = 8;
                                               d.targets[1].mappings[0].size = 256;
                                             }}) {
    auto d = public_apb();
    change(d);
    CHECK(nocloom::make_plan(d));
  }
}

TEST_CASE("Public graph normalization carries mixed APB4 targets through clock placement",
          "[apb-burst-example]") {
  for (bool clocked : {false, true}) {
    auto d = clocked ? graph_clocked_definition() : graph_example_definition();
    set_protocol(d.targets[0], nocloom::Protocol::Apb4);
    set_protocol(d.targets[1], nocloom::Protocol::AhbV3);
    endpoint_ports(d, 4, 8);
    const auto result = nocloom::generate(d);
    REQUIRE(result);
    const auto rtl = std::ranges::find_if(result.value(), [](const auto &a) {
      return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(rtl != result.value().end());
    CHECK(rtl->text.find("nl_t_t0_psel") != std::string::npos);
    CHECK(rtl->text.find("nl_t_t1_htrans") != std::string::npos);
    CHECK(rtl->text.find("nl_t_t2_awaddr") != std::string::npos);
  }
}

TEST_CASE("Sub-page APB4 windows under a 40-bit address match YAML and size the grant",
          "[apb-burst-example]") {
  auto d = public_apb();
  d.address_width = 40;
  d.targets[1].mappings = {{0xa0f0400000, 256}};
  d.targets[1].address_width = 8;
  const auto api = nocloom::generate(d);
  REQUIRE(api);
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 40
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, attributes: amba, address_width: 8,
     mappings: [{base: '0xa0f0400000', size: '0x100'}]}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  const auto yaml = nocloom::generate(parsed.value().definition);
  REQUIRE(yaml);
  CHECK(api.value() == yaml.value());
  const auto map = std::ranges::find_if(
      api.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != api.value().end());
  CHECK(map->text.find("each_beat_DECERR_no_target_access_read_data_zero") != std::string::npos);

  auto g = graph_example_endpoints(1);
  g.address_width = 40;
  g.targets[0].mappings = {{0xa0f0400000, 256}};
  set_protocol(g.targets[0], nocloom::Protocol::Apb4);
  g.targets[0].address_width = 8;
  const auto lowered = public_artifacts(g);
  const auto *grant = lowered.resource("burst_service/t0/bus");
  REQUIRE(grant != nullptr);
  CHECK((*grant)["payload_bits"] == 15);
  CHECK(lowered.storage_bits("burst_service/t0/bus", "payload") == 15);
  // An AXI4 target takes the sub-page window as well.
  set_protocol(g.targets[0], nocloom::Protocol::Axi4);
  g.targets[0].len_width = 8;
  g.targets[0].address_width.reset();
  CHECK(nocloom::make_plan(g));
}

TEST_CASE("APB3, APB2 and AXI4-Lite targets share the dedicated outlet engine",
          "[apb-burst-example]") {
  // APB rejects 64-bit data; Lite accepts it. All reject a 40-bit global
  // address without a local port width.
  for (const auto &[protocol, code] : std::vector<std::pair<nocloom::Protocol, std::string>>{
           {nocloom::Protocol::Apb2, "NCL_APB_BURST_OUTLET_PROFILE"},
           {nocloom::Protocol::Apb3, "NCL_APB_BURST_OUTLET_PROFILE"},
           {nocloom::Protocol::Axi4Lite, "NCL_AXI_LITE_OUTLET_PROFILE"}})
    for (bool wide_data : {false, true}) {
      auto d = public_apb();
      set_protocol(d.targets[1], protocol);
      if (wide_data)
        endpoint_widths(d, 64);
      else
        d.address_width = 40;
      const auto plan = nocloom::make_plan(d);
      if (wide_data && protocol == nocloom::Protocol::Axi4Lite) {
        REQUIRE(plan);
      } else {
        REQUIRE_FALSE(plan);
        CHECK(std::ranges::any_of(plan.diagnostics(),
                                  [&](const auto &diagnostic) { return diagnostic.code == code; }));
      }
    }
  using namespace nocloom::detail;
  for (auto protocol :
       {nocloom::Protocol::Apb3, nocloom::Protocol::Apb2, nocloom::Protocol::Axi4Lite}) {
    auto g = graph_example_endpoints(1);
    g.targets[0].mappings = {{0x10000, 0x10000}};
    set_protocol(g.targets[0], protocol);
    const auto h = public_artifacts(g);
    const auto *grant = h.resource("burst_service/t0/bus");
    REQUIRE(grant != nullptr);
    const bool lite = protocol == nocloom::Protocol::Axi4Lite;
    CHECK((*grant)["role"] == (lite ? "burst_axi_lite_grant" : "burst_apb_grant"));
    CHECK((*grant)["payload_bits"] == (lite ? 71U : 20U)); // PADDR of the window, PSTRB lanes
    const auto has = [&](const std::string &signal) { return h.has_port("nl_t_t0_" + signal); };
    if (lite) {
      for (const auto *signal : {"awaddr", "awprot", "wstrb", "bresp", "araddr", "rresp"})
        CHECK(has(signal));
      CHECK_FALSE(has("awlen"));
    } else {
      CHECK(has("paddr"));
      CHECK_FALSE(has("pstrb"));
      CHECK_FALSE(has("pprot"));
      CHECK(has("pready") == (protocol == nocloom::Protocol::Apb3));
      CHECK(has("pslverr") == (protocol == nocloom::Protocol::Apb3));
    }
  }
  auto bad = lower_apb_burst_outlet([] {
    ApbBurstOutletProfile p{"probe", 0, 8, 0,
                            std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    p.dialect = nocloom::Protocol::AhbV3;
    return p;
  }());
  CHECK_FALSE(bad);
}

TEST_CASE("Schema 10 error codes default to distinct and allow explicit uniform",
          "[apb-burst-example]") {
  const std::string base = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4)";
  const auto absent = nocloom::config::parse(base + "}\n");
  const auto off = nocloom::config::parse(base + ", response_codes: uniform}\n");
  const auto on = nocloom::config::parse(base + ", response_codes: distinct}\n");
  REQUIRE(absent);
  REQUIRE(off);
  REQUIRE(on);
  CHECK(absent.value().definition.transport->response_codes == nocloom::ResponseCodes::Distinct);
  CHECK(off.value().definition.transport->response_codes == nocloom::ResponseCodes::Uniform);
  CHECK(nocloom::TransportOptions{}.response_codes == nocloom::ResponseCodes::Distinct);
  CHECK(on.value().definition.transport->response_codes == nocloom::ResponseCodes::Distinct);
  const auto no_transport = nocloom::config::parse(base.substr(0, base.find("transport:")));
  REQUIRE(no_transport);
  REQUIRE_FALSE(no_transport.value().definition.transport);
  CHECK(nocloom::generate(no_transport.value().definition).value() ==
        nocloom::generate(on.value().definition).value());

  const auto plain = nocloom::generate(absent.value().definition);
  REQUIRE(plain);
  CHECK(plain.value() != nocloom::generate(off.value().definition).value());
  const auto codes = nocloom::generate(on.value().definition);
  REQUIRE(codes);
  CHECK(plain.value() == codes.value());
  CHECK(nocloom::generate(public_apb()).value() == plain.value());
  const auto bad = nocloom::config::parse(base + ", response_codes: decerr}\n");
  REQUIRE_FALSE(bad);
  CHECK(bad.diagnostics().front().code == "NCL_SCHEMA_TYPE");
}

TEST_CASE("Packets cut every AXI4 burst; pair limits narrow them within their range",
          "[apb-burst-example]") {
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
)";
  const std::string tail = R"(targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, attributes: amba, address_width: 3,
     mappings: [{base: '0x5000', size: '0x8'}]}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4)";
  const std::string narrow = "initiators: [{name: cpu0, clock_domain: main, protocol: axi4, "
                             "len_width: 8, data_width: 32}, {name: cpu1, clock_domain: main, "
                             "protocol: axi4, len_width: 8, data_width: 32}]\n";
  const auto plain = nocloom::config::parse(head + narrow + tail + "}\n");
  REQUIRE(plain);
  const auto a = nocloom::generate(plain.value().definition);
  REQUIRE(a);
  const auto map_of = [](const auto &artifacts) {
    return std::ranges::find_if(
               artifacts, [](const auto &artifact) { return artifact.path.ends_with(".map.json"); })
        ->text;
  };
  const auto map = map_of(a.value());
  CHECK(map.find("up_to_four_packets_of_one_burst_in_flight_per_direction") != std::string::npos);
  CHECK(map.find("reads_and_writes_share_max_outstanding_transactions_in_flight_the_last_"
                 "unanswered_response_of_a_burst_frees_its_unit_narrow_split_pieces_count_one_"
                 "each") != std::string::npos);
  CHECK(map.find("exclusive_WRAP_sent_as_INCR") != std::string::npos);
  CHECK(map.find("packet_limits") == std::string::npos);
  // A pair limit narrows the packets of one pair and changes the product.
  const auto limited = nocloom::config::parse(
      head + narrow + tail +
      ", packet_limits: [{source: cpu0, target: bank0, max_bytes: 64, boundary_bytes: 1024}]}\n");
  REQUIRE(limited);
  REQUIRE(limited.value().definition.transport->packet_limits.size() == 1);
  CHECK(limited.value().definition.transport->packet_limits[0].max_bytes == 64U);
  const auto b = nocloom::generate(limited.value().definition);
  REQUIRE(b);
  CHECK(a.value() != b.value());
  CHECK(map_of(b.value()).find("\"packet_limits\"") != std::string::npos);
  // A window that is not a power of two cuts at its 4 KiB granule.
  auto np2 = plain.value().definition;
  np2.targets[0].mappings[0].size = 0x3000;
  CHECK(nocloom::generate(np2));
  const auto outcome = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    if (!plan)
      return plan.diagnostics().front().code + " " + plan.diagnostics().front().field;
    const auto generated = nocloom::generate(d);
    return generated
               ? std::string{}
               : generated.diagnostics().front().code + " " + generated.diagnostics().front().field;
  };
  const auto with_limit = [&](nocloom::PacketLimit limit) {
    auto d = plain.value().definition;
    d.transport->packet_limits = {std::move(limit)};
    return outcome(d);
  };
  const std::string at = "NCL_AXI4_PACKETS transport.packet_limits[0]";
  CHECK(with_limit({"cpu0", "bank0", 4, std::nullopt}).empty());
  CHECK(with_limit({"cpu1", "bank1", std::nullopt, 4}).empty());
  CHECK(with_limit({"cpu0", "bank0", 48, std::nullopt}) == at);
  CHECK(with_limit({"cpu0", "bank0", 2, std::nullopt}) == at);    // below the 4-byte word
  CHECK(with_limit({"cpu0", "bank0", 2048, std::nullopt}) == at); // above 256 words
  CHECK(with_limit({"cpu0", "bank1", 256, std::nullopt}) == at);  // above 128 bytes
  CHECK(with_limit({"cpu0", "bank1", std::nullopt, 16}) == at);   // above the 8-byte window
  CHECK(with_limit({"cpu0", "nowhere", 64, std::nullopt}) == at);
  CHECK(with_limit({"cpu0", "bank0", std::nullopt, std::nullopt}) == at);
  {
    auto d = plain.value().definition;
    d.transport->packet_limits = {{"cpu0", "bank0", 64, std::nullopt},
                                  {"cpu0", "bank0", 32, std::nullopt}};
    CHECK(outcome(d) == "NCL_AXI4_PACKETS transport.packet_limits[1]");
    d.connectivity = nocloom::Connectivity{false, {{"cpu0", "bank1"}, {"cpu1", "bank0"}}};
    d.transport->packet_limits = {{"cpu0", "bank0", 64, std::nullopt}};
    CHECK(outcome(d) == at);
  }
  // Narrow splitting needs no packet mode; a window smaller than the 16-byte
  // alignment word of a 128-bit source is rejected.
  auto d = plain.value().definition;
  d.initiators[0].narrow_bursts = nocloom::NarrowBursts::SplitNonModifiable;
  CHECK(outcome(d).empty());
  const auto split = nocloom::generate(d);
  REQUIRE(split);
  CHECK(map_of(split.value()).find("decompose_exact_narrow_beats") != std::string::npos);
  d.initiators[0].data_width = 128;
  CHECK(outcome(d).starts_with("NCL_AXI4_PACKETS"));
}

TEST_CASE("Target attributes parse presets and bit lists and reject what does not apply",
          "[apb-burst-example]") {
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
targets:
)";
  const auto parse = [&](const std::string &bank0, const std::string &bank1) {
    return nocloom::config::parse(
        head +
        "  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, "
        "mappings: [{base: '0x1000', size: '0x1000'}]" +
        bank0 + "}\n  - {name: bank1, clock_domain: main, data_width: 32, protocol: " + bank1 +
        ", mappings: [{base: '0x5000', size: '0x1000'}]}\n");
  };
  using S = nocloom::AttributeSource;
  const auto amba = parse(", attributes: amba", "apb4, attributes: zero");
  REQUIRE(amba);
  CHECK(amba.value().definition.targets[0].attributes == nocloom::TargetAttributes::amba());
  CHECK(amba.value().definition.targets[1].attributes == nocloom::TargetAttributes::zero());
  const auto custom = parse(", attributes: {preset: amba, prot: [~privileged, 1, instruction]}",
                            "ahb_v3, attributes: {hprot: [lookup, 0, allocate, ~modifiable]}");
  REQUIRE(custom);
  const auto &axi = *custom.value().definition.targets[0].attributes;
  CHECK(axi.read_cache == nocloom::TargetAttributes::amba().read_cache);
  CHECK(axi.prot[0] == nocloom::AttributeBit{S::Privileged, true});
  CHECK(axi.prot[1] == nocloom::AttributeBit{S::One, false});
  CHECK(custom.value().definition.targets[1].attributes->hprot[3] ==
        nocloom::AttributeBit{S::Modifiable, true});
  // Every AXI4, AXI4-Lite, APB4 and AHB target states its attributes.
  for (const auto &[bank0, bank1, field] :
       std::vector<std::tuple<std::string, std::string, std::string>>{
           {"", "apb4, attributes: amba", "targets[0].attributes"},
           {", attributes: amba", "apb4", "targets[1].attributes"},
           {", attributes: amba", "ahb_v3", "targets[1].attributes"},
           {", attributes: amba", "axi4_lite", "targets[1].attributes"}}) {
    const auto missing = parse(bank0, bank1);
    REQUIRE(missing);
    const auto plan = nocloom::make_plan(missing.value().definition);
    REQUIRE_FALSE(plan);
    CHECK(plan.diagnostics().front().code == "NCL_TARGET_ATTRIBUTES");
    CHECK(plan.diagnostics().front().field == field);
  }
  REQUIRE(parse("", "apb3"));
  const auto plain = parse(", attributes: amba", "apb4, attributes: amba");
  REQUIRE(plain);
  const auto a = nocloom::generate(plain.value().definition);
  const auto b = nocloom::generate(amba.value().definition);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() != b.value());
  const auto map = std::ranges::find_if(
      b.value(), [](const auto &artifact) { return artifact.path.ends_with(".map.json"); });
  REQUIRE(map != b.value().end());
  CHECK(map->text.find("\"target_attributes\"") != std::string::npos);
  // Through the API a signal the protocol lacks keeps a preset's bits.
  auto api = custom.value().definition;
  api.targets[1].attributes->prot = {{{S::One}, {S::One}, {S::One}}};
  CHECK(nocloom::make_plan(api).diagnostics().front().code == "NCL_TARGET_ATTRIBUTES");
  api.targets[1].attributes->prot = nocloom::TargetAttributes::amba().prot;
  CHECK(nocloom::make_plan(api));
  // Unknown names, wrong lengths, signals the protocol lacks and APB3/APB2.
  for (const auto &[bank0, bank1] : std::vector<std::pair<std::string, std::string>>{
           {", attributes: full", "apb4"},
           {", attributes: {prot: [0, 1]}", "apb4"},
           {", attributes: {prot: [0, 1, secure]}", "apb4"},
           {", attributes: {hprot: [0, 0, 0, 0]}", "apb4"},
           {"", "apb4, attributes: {read_cache: [0, 0, 0, 0]}"},
           {"", "apb3, attributes: zero"}}) {
    const auto bad = parse(bank0, bank1);
    const auto code =
        !bad ? bad.diagnostics().front().code
             : (nocloom::make_plan(bad.value().definition)
                    ? std::string{}
                    : nocloom::make_plan(bad.value().definition).diagnostics().front().code);
    CHECK(code == "NCL_TARGET_ATTRIBUTES");
  }
}

TEST_CASE("AHB targets may end at the top of the address space", "[apb-burst-example]") {
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: ahb_v3, attributes: amba, address_width: 12,
     mappings: [{base: '0xfffff000', size: '0x1000'}]}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
)");
  REQUIRE(parsed);
  CHECK(nocloom::generate(parsed.value().definition));
}

TEST_CASE("The map contract reports the error encoding the sources observe",
          "[apb-burst-example]") {
  auto d = public_apb();
  const auto map_of = [&] {
    const auto result = nocloom::generate(d);
    REQUIRE(result);
    const auto map = std::ranges::find_if(
        result.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
    REQUIRE(map != result.value().end());
    return map->text;
  };
  const auto plain = map_of();
  CHECK(plain.find("\"error_codes\"") != std::string::npos);
  CHECK(plain.find("DECERR") != std::string::npos);
  d.transport->response_codes = nocloom::ResponseCodes::Uniform;
  const auto codes = map_of();
  CHECK(codes.find("\"error_responses\"") != std::string::npos);
  CHECK(codes.find("\"response_codes\": \"uniform\"") != std::string::npos);
  CHECK(codes.find("DECERR") == std::string::npos);
}

TEST_CASE("Source transaction widths and one-direction endpoints validate and match YAML",
          "[apb-burst-example]") {
  using nocloom::Access;
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  auto base = public_apb();
  base.initiators[0].id_width = 2;
  base.initiators[1].len_width = 4;
  base.initiators[1].access = Access::ReadOnly;
  base.targets[0].access = Access::ReadOnly;
  REQUIRE(nocloom::make_plan(base));
  auto d = base;
  d.initiators[0].id_width = 17;
  CHECK(code_of(d) == "NCL_ENDPOINT_TRANSACTION_WIDTH");
  d = base;
  d.initiators[1].len_width = 9;
  CHECK(code_of(d) == "NCL_ENDPOINT_TRANSACTION_WIDTH");
  d = base;
  d.initiators[0].protocol = nocloom::Protocol::Axi4Lite;
  CHECK(code_of(d) == "NCL_ENDPOINT_TRANSACTION_WIDTH");
  // Every target protocol may be read-only (the APB target keeps PWRITE).
  d = base;
  d.targets[1].access = Access::ReadOnly;
  CHECK(code_of(d).empty());
  const auto api = nocloom::generate(base);
  REQUIRE(api);
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 4, access: read_only}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, access: read_only, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  const auto yaml = nocloom::generate(parsed.value().definition);
  REQUIRE(yaml);
  CHECK(api.value() == yaml.value());
  const auto rtl = std::ranges::find_if(api.value(), [](const auto &a) {
    return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
           a.path != "nocloom_synchronizer.v";
  });
  REQUIRE(rtl != api.value().end());
  CHECK(rtl->text.find("nl_i_cpu1_awvalid") == std::string::npos);
  CHECK(rtl->text.find("nl_t_bank0_awvalid") == std::string::npos);
  CHECK(rtl->text.find("nl_t_bank0_arvalid") != std::string::npos);
  // Write-only endpoints mirror read-only ones: no AR/R ports, the same bytes from YAML and C++.
  auto writes = base;
  writes.initiators[1].access = Access::WriteOnly;
  writes.targets[0].access = Access::WriteOnly;
  writes.targets[1].access = Access::WriteOnly;
  const auto writes_api = nocloom::generate(writes);
  REQUIRE(writes_api);
  const auto writes_rtl = std::ranges::find_if(writes_api.value(), [](const auto &a) {
    return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
           a.path != "nocloom_synchronizer.v";
  });
  REQUIRE(writes_rtl != writes_api.value().end());
  CHECK(writes_rtl->text.find("nl_i_cpu1_arvalid") == std::string::npos);
  CHECK(writes_rtl->text.find("nl_i_cpu1_rready") == std::string::npos);
  CHECK(writes_rtl->text.find("nl_i_cpu1_awvalid") != std::string::npos);
  CHECK(writes_rtl->text.find("nl_t_bank0_arvalid") == std::string::npos);
  CHECK(writes_rtl->text.find("nl_t_bank0_awvalid") != std::string::npos);
  // An APB target keeps its bus: PWRITE selects each transfer, reads never reach it.
  CHECK(writes_rtl->text.find("nl_t_bank1_prdata") != std::string::npos);
  d = writes;
  d.targets[0].exclusive = nocloom::ExclusiveAccess::Target;
  owner_flag(d, d.targets[0]);
  CHECK(code_of(d) == "NCL_TARGET_EXCLUSIVE");
  const auto write_only = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 4, access: write_only}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, access: write_only, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, access: write_only, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(write_only);
  const auto writes_yaml = nocloom::generate(write_only.value().definition);
  REQUIRE(writes_yaml);
  CHECK(writes_api.value() == writes_yaml.value());
  const auto bad = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: x
address_width: 32
reset_ports: [{name: reset_n}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, gated: false}]
initiators:
  - {name: a, clock_domain: main, protocol: axi4, data_width: 32, access: write}
targets:
  - {name: t, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE_FALSE(bad);
  CHECK(bad.diagnostics().front().code == "NCL_ENDPOINT_ACCESS");
}

TEST_CASE("Targets with read side effects match YAML and exclude narrow splitting",
          "[apb-burst-example]") {
  auto d = public_apb();
  const auto plain = nocloom::generate(d);
  REQUIRE(plain);
  for (auto &t : d.targets)
    t.read_side_effects = true;
  const auto api = nocloom::generate(d);
  REQUIRE(api);
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, read_side_effects: true, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, read_side_effects: true, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  const auto yaml = nocloom::generate(parsed.value().definition);
  REQUIRE(yaml);
  CHECK(api.value() == yaml.value());
  CHECK(api.value() != plain.value());
  const auto map = std::ranges::find_if(
      api.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != api.value().end());
  CHECK(map->text.find("\"read_side_effects_targets\"") != std::string::npos);
  // The source keeps its AxLOCK hint for the splitter; false keeps the plain bytes.
  auto off = public_apb();
  for (auto &t : off.targets)
    t.read_side_effects = false;
  const auto same = nocloom::generate(off);
  REQUIRE(same);
  CHECK(same.value() == plain.value());
  d.initiators[0].narrow_bursts = nocloom::NarrowBursts::SplitNonModifiable;
  const auto split = nocloom::make_plan(d);
  REQUIRE_FALSE(split);
  CHECK(split.diagnostics().front().code == "NCL_AXI4_NARROW_BURSTS");
  CHECK(split.diagnostics().front().field == "initiators[0].narrow_bursts");
}

TEST_CASE("Endpoint transaction fields require schema 10 and default spellings keep bytes",
          "[apb-burst-example]") {
  using nocloom::Access;
  auto plain = public_apb();
  auto spelled = plain;
  spelled.initiators[0].id_width = 4; // the default spelled out
  spelled.initiators[0].len_width = 8;
  spelled.initiators[0].access = Access::ReadWrite;
  spelled.targets[0].access = Access::ReadWrite;
  CHECK(nocloom::generate(plain).value() == nocloom::generate(spelled).value());
  auto lite = public_apb();
  set_protocol(lite.initiators[1], nocloom::Protocol::Axi4Lite);
  lite.initiators[1].access = Access::ReadOnly;
  const auto result = nocloom::generate(lite);
  REQUIRE(result);
  const auto rtl = std::ranges::find_if(result.value(), [](const auto &a) {
    return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
           a.path != "nocloom_synchronizer.v";
  });
  REQUIRE(rtl != result.value().end());
  CHECK(rtl->text.find("nl_i_cpu1_awvalid") == std::string::npos);
  CHECK(rtl->text.find("nl_i_cpu1_arvalid") != std::string::npos);
}

TEST_CASE("Narrow burst modes: merge is the default, Lite sources take none",
          "[apb-burst-example]") {
  // mode: the narrow_bursts spelling of cpu0 ("" omits it); widths gives mem
  // its own 128-bit data width; routes keeps cpu0 off the AXI4 target.
  const auto yaml = [](const std::string &mode, bool widths, bool routes) {
    // A mode that names a protocol replaces cpu0's default axi4.
    return std::string(R"(schema_version: 10
name: narrow_modes
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, data_width: 32)") +
           (mode.find("protocol:") == std::string::npos
                ? ", protocol: axi4, id_width: 3, len_width: 8"
                : "") +
           (mode.empty() ? "" : ", narrow_bursts: " + mode) + R"(}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 3}
targets:
  - {name: mem, clock_domain: main, protocol: axi4, len_width: 8, attributes: amba, data_width: )" +
           (widths ? "128" : "32") + R"(, mappings: [{base: '0x10000', size: '0x10000'}]}
  - {name: regs, clock_domain: main, protocol: apb4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
transport:
  max_outstanding: 1
)" + (routes ? "connectivity: {cpu0: [regs], cpu1: [mem, regs]}\n" : "connectivity: all\n") +
           R"(topology:
  request:
    nodes: [{name: q0}]
    links: []
    entries: [{endpoint: cpu0, node: q0}, {endpoint: cpu1, node: q0}]
    exits: [{endpoint: mem, node: q0}, {endpoint: regs, node: q0}]
  response:
    nodes: [{name: p0}]
    links: []
    entries: [{endpoint: mem, node: p0}, {endpoint: regs, node: p0}]
    exits: [{endpoint: cpu0, node: p0}, {endpoint: cpu1, node: p0}]
output: {backend: verilog, language: verilog_2001}
)";
  };
  const auto definition = [&](const std::string &mode, bool widths, bool routes = false) {
    auto parsed = nocloom::config::parse(yaml(mode, widths, routes));
    REQUIRE(parsed);
    return parsed.value().definition;
  };
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    if (!plan)
      return plan.diagnostics().front().code;
    const auto generated = nocloom::generate(d);
    return generated ? std::string{} : generated.diagnostics().front().code;
  };
  const auto map_of = [](const auto &artifacts) {
    return std::ranges::find_if(
               artifacts, [](const auto &artifact) { return artifact.path.ends_with(".map.json"); })
        ->text;
  };
  // The default merges every burst whatever its AxCACHE; merge spells the
  // default and changes nothing.
  const auto merged = nocloom::generate(definition("", true));
  REQUIRE(merged);
  const auto spelled = nocloom::config::parse(yaml("merge_modifiable", true, false));
  REQUIRE_FALSE(spelled);
  CHECK(spelled.diagnostics().front().code == "NCL_AXI4_NARROW_BURSTS");
  CHECK(map_of(merged.value()).find("\"narrow_bursts_default\": \"merge\"") != std::string::npos);
  CHECK(map_of(merged.value()).find("merge_narrow") == std::string::npos);
  const auto merge = nocloom::generate(definition("merge", true));
  REQUIRE(merge);
  CHECK(merge.value() == merged.value());
  // merge needs no per-target widths and no AXI4 target merge.
  CHECK(code_of(definition("merge", false)).empty());
  CHECK(code_of(definition("merge", true, true)).empty());
  CHECK(code_of(definition("", true, true)).empty());
  // An AXI4-Lite source issues single beats and takes no narrow_bursts, on
  // both the YAML and the C++ path.
  const auto rejected_at = [](const std::string &text, const nocloom::Definition *direct) {
    const auto parsed = nocloom::config::parse(text);
    REQUIRE(parsed);
    const auto from_yaml = nocloom::make_plan(parsed.value().definition, &parsed.value().sources);
    const auto from_api = nocloom::make_plan(direct ? *direct : parsed.value().definition);
    REQUIRE_FALSE(from_yaml);
    REQUIRE_FALSE(from_api);
    CHECK(from_yaml.diagnostics().front().field == from_api.diagnostics().front().field);
    CHECK(from_yaml.diagnostics().front().code == from_api.diagnostics().front().code);
    return from_api.diagnostics().front().code + " " + from_api.diagnostics().front().field;
  };
  const auto lite_yaml = yaml("merge, protocol: axi4_lite", true, false);
  auto lite = definition("", true);
  set_protocol(lite.initiators[0], nocloom::Protocol::Axi4Lite);
  auto lite_merge = lite;
  lite_merge.initiators[0].narrow_bursts = nocloom::NarrowBursts::Merge;
  CHECK(rejected_at(lite_yaml, &lite_merge) ==
        "NCL_AXI4_NARROW_BURSTS initiators[0].narrow_bursts");
  REQUIRE(nocloom::generate(lite));
  REQUIRE(nocloom::generate(definition("", false)));
  // Unknown spellings and values.
  const auto bad = nocloom::config::parse(yaml("merge_all", true, false));
  REQUIRE_FALSE(bad);
  CHECK(bad.diagnostics().front().code == "NCL_AXI4_NARROW_BURSTS");
  auto d = definition("", true);
  d.initiators[0].narrow_bursts = static_cast<nocloom::NarrowBursts>(7);
  CHECK(code_of(d) == "NCL_AXI4_NARROW_BURSTS");
}

TEST_CASE("Per-target data widths attach endpoints at their own width", "[apb-burst-example]") {
  auto g = graph_example_endpoints(3);
  for (unsigned i = 0; i < 3; ++i)
    g.targets[i].mappings = {{0x10000ULL * (i + 1), 0x10000}};
  g.targets[1].data_width = 128;
  g.targets[2].data_width = 256;
  // Sources stay 32 bits: every route is narrow to wide.
  const auto lowered = public_artifacts(g);
  CHECK(lowered.endpoint_bytes("t1") == 16);
  CHECK(lowered.endpoint_bytes("t2") == 32);
  CHECK(lowered.endpoint_bytes("s0") == 4);
  CHECK(lowered.port_width("nl_t_t2_wdata") == 256);
  CHECK(lowered.port_width("nl_t_t1_rdata") == 128);
  // A source wider than a target it reaches is clamped; its engines hold the storage they
  // declare.
  auto wide_source = g;
  wide_source.initiators[0].data_width = 256;
  const auto clamped = public_artifacts(wide_source);
  for (const auto *resource : {"source/s0/write", "source/s0/read"})
    CHECK(clamped.storage_bits(resource, "payload") ==
          (*clamped.resource(resource))["payload_bits"].get<unsigned>());
  CHECK((*clamped.resource("source/s0/write"))["payload_bits"] !=
        (*lowered.resource("source/s0/write"))["payload_bits"]);
  auto bad = g;
  bad.targets[1].data_width = 96;
  CHECK_FALSE(nocloom::make_plan(bad));
  // Uniform-width configurations keep one endpoint word, and a response's reach is as wide as its
  // word's byte offset.
  auto plain = g;
  for (auto &t : plain.targets)
    t.data_width = 32;
  const auto old = public_artifacts(target_domain(plain));
  CHECK_FALSE(old.map["definition"].contains("endpoint_data_widths"));
  CHECK(old.cut_field_widths("reach") == std::set<unsigned>{2});
}

TEST_CASE("Public widths admit 512- and 1024-bit AXI4 endpoints", "[apb-burst-example]") {
  // mem_bits: the AXI4 target's width; cpu0_bits: the source's; regs keeps a
  // dedicated APB4 target at 32 bits unless regs_bits says otherwise.
  const auto yaml = [](unsigned mem_bits, unsigned cpu0_bits, unsigned regs_bits) {
    // The network stays 32 bits wide whatever the widest target is.
    return R"(schema_version: 10
connectivity: all
name: wide_endpoints
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, len_width: 8, id_width: 3, data_width: )" +
           std::to_string(cpu0_bits) + R"(}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 3}
targets:
  - {name: mem, clock_domain: main, protocol: axi4, len_width: 8, attributes: amba, data_width: )" +
           std::to_string(mem_bits) + R"(, mappings: [{base: '0x10000', size: '0x10000'}]}
  - {name: regs, clock_domain: main, protocol: apb4, attributes: amba, data_width: )" +
           std::to_string(regs_bits) + R"(, mappings: [{base: '0x1000', size: '0x1000'}]}
transport:
  data_width: 32
  max_outstanding: 1
topology:
  request:
    nodes: [{name: q0}]
    links: []
    entries: [{endpoint: cpu0, node: q0}, {endpoint: cpu1, node: q0}]
    exits: [{endpoint: mem, node: q0}, {endpoint: regs, node: q0}]
  response:
    nodes: [{name: p0}]
    links: []
    entries: [{endpoint: mem, node: p0}, {endpoint: regs, node: p0}]
    exits: [{endpoint: cpu0, node: p0}, {endpoint: cpu1, node: p0}]
output: {backend: verilog, language: verilog_2001}
)";
  };
  const auto outcome = [&](unsigned mem_bits, unsigned cpu0_bits, unsigned regs_bits = 32) {
    const auto parsed = nocloom::config::parse(yaml(mem_bits, cpu0_bits, regs_bits));
    REQUIRE(parsed);
    const auto plan = nocloom::make_plan(parsed.value().definition, &parsed.value().sources);
    if (!plan)
      return plan.diagnostics().front().code + " " + plan.diagnostics().front().field;
    const auto generated = nocloom::generate(parsed.value().definition);
    return generated ? std::string{} : generated.diagnostics().front().code;
  };
  for (unsigned mem : {512U, 1024U})
    for (unsigned cpu0 : {32U, 256U, 512U, 1024U})
      CHECK(outcome(mem, cpu0).empty());
  // A 1024-bit source with a narrower AXI4 target clamps to it.
  CHECK(outcome(128, 1024).empty());
  CHECK(outcome(2048, 32) == "NCL_ENDPOINT_DATA_WIDTH targets[0].data_width");
  CHECK(outcome(1024, 2048) == "NCL_ENDPOINT_DATA_WIDTH initiators[0].data_width");
  // APB targets admit 8/16/32 physical bits, but no wider APB face.
  CHECK(outcome(1024, 32, 1024) == "NCL_ENDPOINT_DATA_WIDTH targets[1].data_width");
}

TEST_CASE("Per-target widths reach 512- and 1024-bit endpoints", "[apb-burst-example]") {
  using namespace nocloom::detail;
  auto g = graph_example_endpoints(3);
  for (unsigned i = 0; i < 3; ++i)
    g.targets[i].mappings = {{0x10000ULL * (i + 1), 0x10000}};
  g.targets[1].data_width = 512;
  g.targets[2].data_width = 1024;
  // 32-bit sources: the response links of the 1024-bit target's 128-byte word stay at the
  // transport word below its attachment.
  const auto lowered = public_artifacts(target_domain(g));
  CHECK(lowered.endpoint_bytes("t1") == 64);
  CHECK(lowered.endpoint_bytes("t2") == 128);
  CHECK(lowered.cut_field_widths("reach") == std::set<unsigned>{2});
  // The attachment ladder steps 128 -> 64 -> 32 bytes by 2:1 hops.
  CHECK(std::ranges::any_of(lowered.map["network"]["adapters"], [](const auto &a) {
    return a["wide_bytes"] == 128 && a["narrow_bytes"] == 64;
  }));
  CHECK(lowered.port_width("nl_t_t2_wdata") == 1024);
  CHECK(lowered.port_width("nl_t_t2_wstrb") == 128);
  CHECK(lowered.port_width("nl_t_t1_rdata") == 512);
  // A 1024-bit source clamps to the 32-bit target and to the 512-bit one.
  auto wide_source = g;
  wide_source.initiators.front().data_width = 1024;
  wide_source.initiators.back().data_width = 512;
  const auto clamped = public_artifacts(wide_source);
  CHECK(clamped.port_width("nl_i_s0_wdata") == 1024);
  for (const auto *resource : {"source/s0/write", "source/s2/read"})
    CHECK(clamped.storage_bits(resource, "payload") ==
          (*clamped.resource(resource))["payload_bits"].get<unsigned>());
  // A 512-bit-widest composition likewise.
  auto half = g;
  half.targets[2].data_width = 256;
  CHECK(public_artifacts(target_domain(half)).cut_field_widths("reach") == std::set<unsigned>{2});
  // AxSIZE tops out at 128 bytes: 2048-bit endpoints stay rejected.
  auto beyond = g;
  beyond.targets[2].data_width = 2048;
  CHECK_FALSE(nocloom::make_plan(beyond));
  beyond = g;
  beyond.initiators.front().data_width = 2048;
  CHECK_FALSE(nocloom::make_plan(beyond));
  // The 2:1 hop family stops at the occupancy: a 128-byte word needs eight bits.
  CHECK_FALSE(plan_width_adapter({128, 64, false, 0, BurstProfile{0, 8, true, 32, 7}}));
  CHECK(plan_width_adapter({128, 64, false, 0, BurstProfile{0, 8, true, 32, 8}}));
  CHECK_FALSE(plan_width_adapter({64, 32, false, 0, std::nullopt}));
}

TEST_CASE("FIXED burst handling is per dedicated target and default spellings keep bytes",
          "[apb-burst-example]") {
  using nocloom::FixedBursts;
  using nocloom::Protocol;
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  const auto text_of = [](const nocloom::Definition &d, const char *suffix) {
    const auto generated = nocloom::generate(d);
    REQUIRE(generated);
    const auto artifact = std::ranges::find_if(generated.value(), [&](const auto &a) {
      return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(artifact != generated.value().end());
    return artifact->text;
  };
  for (auto protocol :
       {Protocol::Apb4, Protocol::Apb3, Protocol::Apb2, Protocol::Axi4Lite, Protocol::AhbV3}) {
    auto plain = public_apb();
    set_protocol(plain.targets[1], protocol);
    auto spelled = plain;
    spelled.targets[1].fixed_bursts = FixedBursts::Reject;
    auto single = plain;
    single.targets[1].fixed_bursts = FixedBursts::Single;
    // The explicit default keeps every artifact byte; single changes the RTL
    // and lists the target in the map.
    CHECK(nocloom::generate(plain).value() == nocloom::generate(spelled).value());
    CHECK(text_of(plain, ".v") != text_of(single, ".v"));
    const auto map = text_of(single, ".map.json");
    CHECK(map.find("\"fixed_bursts_single\": [\n      \"bank1\"\n    ]") != std::string::npos);
    CHECK(map.find("each_beat_served_alone_at_the_repeated_address") != std::string::npos);
    CHECK(map.find("except_fixed_bursts_single_targets") != std::string::npos);
    CHECK(text_of(plain, ".map.json").find("fixed_bursts") == std::string::npos);
  }
  // Without error codes the merged write result names each cause once.
  auto uniform = public_apb();
  uniform.transport->response_codes = nocloom::ResponseCodes::Uniform;
  uniform.targets[1].fixed_bursts = FixedBursts::Single;
  const auto uniform_map = text_of(uniform, ".map.json");
  CHECK(uniform_map.find("one_B_SLVERR_if_any_beat_failed_or_the_start_is_outside_the_window") !=
        std::string::npos);
  CHECK(uniform_map.find("SLVERR_first_then_SLVERR") == std::string::npos);
  // AXI4 targets receive FIXED unchanged: both spellings are rejected there.
  for (auto value : {FixedBursts::Reject, FixedBursts::Single}) {
    auto d = public_apb();
    d.targets[0].fixed_bursts = value;
    CHECK(code_of(d) == "NCL_TARGET_FIXED_BURSTS");
  }
  auto bad = public_apb();
  bad.targets[1].fixed_bursts = static_cast<FixedBursts>(7);
  CHECK(code_of(bad) == "NCL_TARGET_FIXED_BURSTS");
  // YAML spells the same definition and rejects unknown values.
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, attributes: amba, mappings: [{base: '0x5000', size: '0x1000'}])";
  const auto yaml = nocloom::config::parse(head + ", fixed_bursts: single}\n");
  REQUIRE(yaml);
  auto api = public_apb();
  api.targets[1].fixed_bursts = FixedBursts::Single;
  CHECK(yaml.value().definition.targets[1].fixed_bursts == FixedBursts::Single);
  CHECK(nocloom::generate(yaml.value().definition).value() == nocloom::generate(api).value());
  const auto unknown = nocloom::config::parse(head + ", fixed_bursts: split}\n");
  REQUIRE_FALSE(unknown);
  CHECK(unknown.diagnostics().front().code == "NCL_TARGET_FIXED_BURSTS");
}

TEST_CASE("Target timeouts are schema 10 options that change nothing when absent",
          "[apb-burst-example]") {
  using nocloom::Protocol;
  using nocloom::TargetTimeout;
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  const auto text_of = [](const nocloom::Definition &d, const char *suffix) {
    const auto generated = nocloom::generate(d);
    REQUIRE(generated);
    const auto artifact = std::ranges::find_if(generated.value(), [&](const auto &a) {
      return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(artifact != generated.value().end());
    return artifact->text;
  };
  for (auto protocol : {Protocol::Apb4, Protocol::Apb3, Protocol::Axi4Lite, Protocol::AhbV3}) {
    auto plain = public_apb();
    set_protocol(plain.targets[1], protocol);
    auto timed = plain;
    timed.targets[1].timeout = TargetTimeout{1024};
    const auto rtl = text_of(timed, ".v"), map = text_of(timed, ".map.json");
    CHECK(rtl.find("output wire nl_t_bank1_timed_out") != std::string::npos);
    CHECK(rtl.find("nl_t_bank0_timed_out") == std::string::npos);
    CHECK(map.find("\"target_timeouts\": {\n      \"bank1\": {\n        \"completion\": false,\n"
                   "        \"cycles\": 1024") != std::string::npos);
    CHECK(map.find("target_failed_until_common_reset") != std::string::npos);
    // Completion changes the outlet: the report-only outlet drops responses.
    auto completing = timed;
    completing.targets[1].timeout->completion = true;
    CHECK(text_of(completing, ".v") != rtl);
    CHECK(text_of(completing, ".map.json").find("\"completion\": true") != std::string::npos);
    for (const auto *suffix : {".v", ".map.json", ".bundle.json"})
      CHECK(text_of(plain, suffix).find("timed_out") == std::string::npos);
  }
  // An AXI4 outlet timer adds the frozen address channels to its engines.
  auto axi = public_apb();
  axi.targets[0].timeout = TargetTimeout{4096};
  CHECK(text_of(axi, ".v").find("output wire nl_t_bank0_timed_out") != std::string::npos);
  axi.targets[0].timeout = TargetTimeout{2048, true};
  CHECK(nocloom::generate(axi));
  axi.targets[0].timeout = TargetTimeout{1024};
  CHECK(nocloom::generate(axi));
  for (unsigned cycles : {0U, 8U, 1000U, 1536U, 1U << 21}) {
    auto d = public_apb();
    d.targets[1].timeout = TargetTimeout{cycles};
    CHECK(code_of(d) == "NCL_TARGET_TIMEOUT");
  }
  auto apb2 = public_apb();
  set_protocol(apb2.targets[1], Protocol::Apb2);
  apb2.targets[1].timeout = TargetTimeout{1024};
  CHECK(code_of(apb2) == "NCL_TARGET_TIMEOUT");
  // YAML spells the same definition; other keys are unknown.
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, attributes: amba, mappings: [{base: '0x5000', size: '0x1000'}])";
  const auto yaml = nocloom::config::parse(head + ", timeout: {cycles: 1024}}\n");
  REQUIRE(yaml);
  auto api = public_apb();
  api.targets[1].timeout = TargetTimeout{1024};
  CHECK(yaml.value().definition.targets[1].timeout == TargetTimeout{1024});
  CHECK(nocloom::generate(yaml.value().definition).value() == nocloom::generate(api).value());
  const auto completing_yaml =
      nocloom::config::parse(head + ", timeout: {cycles: 2048, completion: true}}\n");
  REQUIRE(completing_yaml);
  CHECK(completing_yaml.value().definition.targets[1].timeout == TargetTimeout{2048, true});
  for (const auto *extra :
       {", timeout: {cycles: 1024, external_enable: true}}\n",
        ", timeout: {cycles: 1024, time_base: 1}}\n", ", timeout: 1024}\n",
        ", timeout: {cycles: 1024, completion: 1}}\n", ", timeout: {completion: true}}\n"}) {
    const auto rejected = nocloom::config::parse(head + extra);
    REQUIRE_FALSE(rejected);
    CHECK((rejected.diagnostics().front().code == "NCL_SCHEMA_UNKNOWN_FIELD" ||
           rejected.diagnostics().front().code == "NCL_SCHEMA_TYPE" ||
           rejected.diagnostics().front().code == "NCL_SCHEMA_MISSING_FIELD"));
  }
  // The outlets refuse invalid timers themselves.
  using namespace nocloom::detail;
  BurstOutletProfile outlet{"probe", 4, 8, 0,
                            std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  const auto untimed = lower_apb_burst_outlet(outlet);
  REQUIRE(untimed);
  for (unsigned cycles : {8U, 1536U, 1U << 21}) {
    outlet.timeout_cycles = cycles;
    CHECK_FALSE(lower_apb_burst_outlet(outlet));
    CHECK_FALSE(lower_ahb_outlet(outlet));
    CHECK_FALSE(lower_axi_lite_outlet(outlet));
  }
  // Completion needs a timer.
  outlet.timeout_cycles = 0;
  outlet.timeout_completion = true;
  CHECK_FALSE(lower_apb_burst_outlet(outlet));
  CHECK_FALSE(lower_ahb_outlet(outlet));
  CHECK_FALSE(lower_axi_lite_outlet(outlet));
  outlet.timeout_completion = false;
  outlet.timeout_cycles = 1024;
  outlet.dialect = Protocol::Apb2;
  CHECK_FALSE(lower_apb_burst_outlet(outlet));
  outlet.dialect = Protocol::Apb3;
  const auto timed = lower_apb_burst_outlet(outlet);
  REQUIRE(timed);
  CHECK(std::ranges::any_of(timed.value().ir.ports,
                            [](const auto &p) { return p.name == "nl_status_timed_out"; }));
  CHECK_FALSE(std::ranges::any_of(untimed.value().ir.ports,
                                  [](const auto &p) { return p.name == "nl_status_timed_out"; }));
  Axi4OutletProfile master{"probe"};
  master.timeout_cycles = 12;
  CHECK_FALSE(lower_axi4_outlet(master));
  master.timeout_cycles = 0;
  master.timeout_completion = true;
  CHECK_FALSE(lower_axi4_outlet(master));
  master.timeout_cycles = 1024;
  master.read_only = true;
  CHECK(lower_axi4_outlet(master));
}

TEST_CASE("AHB burst shaping is a schema 10 AHB option whose defaults keep bytes",
          "[apb-burst-example]") {
  using nocloom::Protocol;
  using nocloom::WriteBursts;
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  const auto text_of = [](const nocloom::Definition &d, const char *suffix) {
    const auto generated = nocloom::generate(d);
    REQUIRE(generated);
    const auto artifact = std::ranges::find_if(generated.value(), [&](const auto &a) {
      return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(artifact != generated.value().end());
    return artifact->text;
  };
  auto plain = public_apb();
  set_protocol(plain.targets[1], Protocol::AhbV3);
  // Explicit defaults keep every artifact byte and add nothing to the map.
  auto spelled = plain;
  spelled.targets[1].write_bursts = WriteBursts::Packet;
  spelled.targets[1].undefined_length_bursts = true;
  CHECK(nocloom::generate(plain).value() == nocloom::generate(spelled).value());
  CHECK(text_of(plain, ".map.json").find("ahb_bursts") == std::string::npos);
  std::set<std::string> rtl{text_of(plain, ".v")};
  for (const auto &[write, undefined, listed] :
       {std::tuple{WriteBursts::Complete, true, "\"write_bursts\": \"complete\""},
        std::tuple{WriteBursts::Packet, false, "\"undefined_length_bursts\": false"},
        std::tuple{WriteBursts::Complete, false, "\"write_bursts\": \"complete\""}}) {
    auto shaped = plain;
    shaped.targets[1].write_bursts = write;
    shaped.targets[1].undefined_length_bursts = undefined;
    rtl.insert(text_of(shaped, ".v"));
    const auto map = text_of(shaped, ".map.json");
    CHECK(map.find("\"ahb_bursts\": {\n      \"bank1\": {") != std::string::npos);
    CHECK(map.find(listed) != std::string::npos);
    CHECK(map.find("HBURST_never_INCR") != std::string::npos);
    CHECK(map.find("fixed_length_write_burst_ends_early") != std::string::npos);
  }
  // Both write modes share the safe streaming outlet.
  CHECK(rtl.size() == 2);
  // Other targets have no HBURST: even the defaults are refused there.
  for (auto protocol :
       {Protocol::Axi4, Protocol::Apb4, Protocol::Apb3, Protocol::Apb2, Protocol::Axi4Lite}) {
    auto d = public_apb();
    set_protocol(d.targets[1], protocol);
    d.targets[1].write_bursts = WriteBursts::Packet;
    CHECK(code_of(d) == "NCL_TARGET_AHB_BURSTS");
    d.targets[1].write_bursts.reset();
    d.targets[1].undefined_length_bursts = true;
    CHECK(code_of(d) == "NCL_TARGET_AHB_BURSTS");
  }
  auto bad = plain;
  bad.targets[1].write_bursts = static_cast<WriteBursts>(5);
  CHECK(code_of(bad) == "NCL_TARGET_AHB_BURSTS");
  // YAML spells the same definition and rejects other spellings.
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: ahb_v3, attributes: amba, mappings: [{base: '0x5000', size: '0x1000'}])";
  const auto yaml =
      nocloom::config::parse(head + ", write_bursts: complete, undefined_length_bursts: false}\n");
  REQUIRE(yaml);
  auto api = plain;
  api.targets[1].write_bursts = WriteBursts::Complete;
  api.targets[1].undefined_length_bursts = false;
  CHECK(yaml.value().definition.targets[1].write_bursts == WriteBursts::Complete);
  CHECK(yaml.value().definition.targets[1].undefined_length_bursts == false);
  CHECK(nocloom::generate(yaml.value().definition).value() == nocloom::generate(api).value());
  for (const auto &[extra, code] :
       {std::pair{", write_bursts: split}\n", "NCL_TARGET_AHB_BURSTS"},
        std::pair{", write_bursts: true}\n", "NCL_SCHEMA_TYPE"},
        std::pair{", undefined_length_bursts: 'false'}\n", "NCL_SCHEMA_TYPE"},
        std::pair{", undefined_length_bursts: no}\n", "NCL_SCHEMA_TYPE"},
        std::pair{", undefined_length_bursts: 0}\n", "NCL_SCHEMA_TYPE"}}) {
    const auto rejected = nocloom::config::parse(head + extra);
    REQUIRE_FALSE(rejected);
    CHECK(rejected.diagnostics().front().code == code);
  }
  // The outlets refuse shaping away from AHB as well.
  using namespace nocloom::detail;
  BurstOutletProfile outlet{"probe", 4, 8, 0,
                            std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  for (bool complete : {true, false}) {
    outlet.complete_write_bursts = complete;
    outlet.undefined_length_bursts = complete;
    CHECK_FALSE(lower_apb_burst_outlet(outlet));
    CHECK_FALSE(lower_axi_lite_outlet(outlet));
    const auto ahb = lower_ahb_outlet(outlet);
    REQUIRE(ahb);
    CHECK(verify(ahb.value().ir));
  }
}

TEST_CASE("AHB HRESP width is 1 bit unless an AHB target selects the AMBA 2 encoding",
          "[apb-burst-example]") {
  using nocloom::Protocol;
  const auto code_of = [](const nocloom::Definition &d) {
    const auto plan = nocloom::make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  const auto text_of = [](const nocloom::Definition &d, const char *suffix) {
    const auto generated = nocloom::generate(d);
    REQUIRE(generated);
    const auto artifact = std::ranges::find_if(generated.value(), [&](const auto &a) {
      return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(artifact != generated.value().end());
    return artifact->text;
  };
  auto plain = public_apb();
  set_protocol(plain.targets[1], Protocol::AhbV3);
  auto one = plain;
  one.targets[1].hresp_width = 1;
  auto two = plain;
  two.targets[1].hresp_width = 2;
  // Absent and an explicit 1 are the same 1-bit port; 2 widens the port.
  CHECK(nocloom::generate(plain).value() == nocloom::generate(one).value());
  const auto rtl1 = text_of(plain, ".v"), rtl2 = text_of(two, ".v");
  CHECK(rtl1 != rtl2);
  CHECK(rtl1.find("input wire nl_t_bank1_hresp") != std::string::npos);
  CHECK(rtl2.find("input wire [1:0] nl_t_bank1_hresp") != std::string::npos);
  CHECK(text_of(plain, ".map.json").find("\"ahb_hresp_widths\": {\n      \"bank1\": 1\n") !=
        std::string::npos);
  CHECK(text_of(two, ".map.json").find("\"ahb_hresp_widths\": {\n      \"bank1\": 2\n") !=
        std::string::npos);
  CHECK(text_of(two, ".map.json")
            .find("RETRY_SPLIT_abort_packet_target_error_1bit_ERROR_2bit_AMBA2") !=
        std::string::npos);
  // Only a 2-bit target adds the cancellation contract.
  CHECK(text_of(two, ".map.json").find("\"amba2_hresp\": \"RETRY_SPLIT_two_cycle_cancel") !=
        std::string::npos);
  CHECK(text_of(plain, ".map.json").find("amba2_hresp") == std::string::npos);
  // No AHB target, no width record.
  CHECK(text_of(public_apb(), ".map.json").find("ahb_hresp_widths") == std::string::npos);
  for (unsigned width : {0U, 3U, 32U}) {
    auto d = plain;
    d.targets[1].hresp_width = width;
    CHECK(code_of(d) == "NCL_TARGET_AHB_HRESP");
  }
  // Other targets have no HRESP: even the default is refused there, and a
  // schema 10 target always states its protocol.
  for (auto protocol :
       {Protocol::Axi4, Protocol::Apb4, Protocol::Apb3, Protocol::Apb2, Protocol::Axi4Lite}) {
    for (unsigned width : {1U, 2U}) {
      auto d = public_apb();
      set_protocol(d.targets[1], protocol);
      d.targets[1].hresp_width = width;
      CHECK(code_of(d) == "NCL_TARGET_AHB_HRESP");
    }
  }
  auto absent = public_apb();
  absent.targets[1].protocol.reset();
  absent.targets[1].hresp_width = 2;
  CHECK(code_of(absent) == "NCL_SCHEMA_MISSING_FIELD");
  // YAML spells the same definition and rejects other values and targets.
  const std::string head = R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8}
output: {backend: verilog, language: verilog_2001}
transport: {max_outstanding: 1, transport_word_bytes: 4}
targets:
  - {name: bank0, clock_domain: main, data_width: 32@BANK0@, attributes: amba, mappings: [{base: '0x1000', size: '0x1000'}]}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: ahb_v3, attributes: amba, mappings: [{base: '0x5000', size: '0x1000'}])";
  const auto yaml_of = [&](const std::string &bank0, const std::string &bank1) {
    auto text = head;
    text.replace(text.find("@BANK0@"), 7,
                 bank0.find("protocol:") == std::string::npos
                     ? ", protocol: axi4, len_width: 8" + bank0
                     : bank0);
    return nocloom::config::parse(text + bank1 + "}\n");
  };
  const auto yaml = yaml_of("", ", hresp_width: 2");
  REQUIRE(yaml);
  CHECK(yaml.value().definition.targets[1].hresp_width == 2U);
  CHECK(nocloom::generate(yaml.value().definition).value() == nocloom::generate(two).value());
  const auto spelled = yaml_of("", ", hresp_width: 1");
  REQUIRE(spelled);
  CHECK(nocloom::generate(spelled.value().definition).value() == nocloom::generate(plain).value());
  for (const auto *extra : {", hresp_width: two", ", hresp_width: '2'", ", hresp_width: [2]",
                            ", hresp_width: 2.0", ", hresp_width: -1"}) {
    const auto rejected = yaml_of("", extra);
    REQUIRE_FALSE(rejected);
    CHECK(rejected.diagnostics().front().code == "NCL_SCHEMA_TYPE");
  }
  const auto checked = [&](const nocloom::Result<nocloom::ParsedDefinition> &parsed) {
    return parsed ? code_of(parsed.value().definition) : parsed.diagnostics().front().code;
  };
  CHECK(checked(yaml_of("", ", hresp_width: 0")) == "NCL_TARGET_AHB_HRESP");
  CHECK(checked(yaml_of("", ", hresp_width: 3")) == "NCL_TARGET_AHB_HRESP");
  CHECK(checked(yaml_of(", hresp_width: 1", "")) == "NCL_TARGET_AHB_HRESP");
  CHECK(checked(yaml_of(", protocol: apb4, hresp_width: 2", "")) == "NCL_TARGET_AHB_HRESP");
  // The outlets refuse a 2-bit HRESP away from AHB.
  using namespace nocloom::detail;
  BurstOutletProfile outlet{"probe", 4, 8, 0,
                            std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  const auto width_of = [](const NodeHardware &h) {
    const auto port = std::ranges::find(h.ir.ports, std::string{"nl_ahb_hresp"}, &Port::name);
    REQUIRE(port != h.ir.ports.end());
    return port->width;
  };
  const auto default_width = lower_ahb_outlet(outlet);
  REQUIRE(default_width);
  CHECK(width_of(default_width.value()) == 1);
  for (unsigned width : {0U, 3U}) {
    outlet.hresp_width = width;
    CHECK_FALSE(lower_ahb_outlet(outlet));
  }
  outlet.hresp_width = 2;
  CHECK_FALSE(lower_apb_burst_outlet(outlet));
  CHECK_FALSE(lower_axi_lite_outlet(outlet));
  const auto amba2 = lower_ahb_outlet(outlet);
  REQUIRE(amba2);
  CHECK(verify(amba2.value().ir));
  CHECK(width_of(amba2.value()) == 2);
}

TEST_CASE("Dedicated Lite targets preserve 32 and 64 bit endpoint widths", "[lite-target64]") {
  using namespace nocloom;
  using namespace nocloom::detail;
  for (unsigned bits : {32U, 64U}) {
    BurstOutletProfile p{"probe", 2, 8, 3, std::vector<nocloom::AddressMapping>{{0x1000, 256}}, 8};
    p.data_bits = bits;
    const auto h = lower_axi_lite_outlet(p);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    for (const auto &[name, width] :
         std::vector<std::pair<std::string, unsigned>>{{"nl_axil_rdata", bits},
                                                       {"nl_axil_wdata", bits},
                                                       {"nl_axil_wstrb", bits / 8},
                                                       {"nl_rrsp_data", bits}}) {
      const auto port = std::ranges::find(h.value().ir.ports, name, &Port::name);
      REQUIRE(port != h.value().ir.ports.end());
      CHECK(port->width == width);
    }
    auto d = public_apb();
    set_protocol(d.targets[1], Protocol::Axi4Lite);
    d.targets[1].data_width = bits;
    d.targets[1].mappings[0].size = bits / 8;
    const auto generated = generate(d);
    REQUIRE(generated);
    // A Lite target keeps 1 to 256 packets in flight.
    d.targets[1].max_outstanding = 256;
    REQUIRE(generate(d));
    d.targets[1].max_outstanding = 257;
    REQUIRE_FALSE(generate(d));
    d.targets[1].max_outstanding = 1;
    if (bits == 64) {
      d.targets[1].mappings[0].size = 4;
      REQUIRE_FALSE(generate(d));
    }
  }
  for (unsigned bits : {8U, 16U, 128U}) {
    BurstOutletProfile p{"probe"};
    p.data_bits = bits;
    REQUIRE_FALSE(lower_axi_lite_outlet(p));
    auto d = public_apb();
    set_protocol(d.targets[1], Protocol::Axi4Lite);
    d.targets[1].data_width = bits;
    REQUIRE_FALSE(generate(d));
  }
}

TEST_CASE("A 64 bit Lite target admits a sixteen word exclusive network packet",
          "[lite-target64]") {
  using namespace nocloom;
  auto d = graph_example_endpoints(1);
  d.transport->transport_word_bytes.reset();
  d.initiators[0].data_width = 64;
  d.initiators[0].len_width = 4;
  set_protocol(d.targets[0], Protocol::Axi4Lite);
  d.targets[0].data_width = 64;
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  d.targets[0].exclusive_monitors = 1;
  d.transport->exclusive_granule = 128;
  const auto h = public_artifacts(d);
  const auto *bus = h.resource("burst_service/t0/bus");
  REQUIRE(bus != nullptr);
  CHECK((*bus)["payload_bits"] == 107);
}

TEST_CASE("Public narrow APB targets keep legal container widths", "[apb-burst-example]") {
  using nocloom::Protocol;
  for (auto protocol : {Protocol::Apb4, Protocol::Apb3, Protocol::Apb2})
    for (unsigned width : {8U, 16U}) {
      auto d = public_apb();
      set_protocol(d.targets[1], protocol);
      d.targets[1].data_width = width;
      const auto p = nocloom::make_plan(d);
      REQUIRE(p);
      const auto generated = nocloom::generate(d);
      REQUIRE(generated);
      const auto artifact = std::ranges::find_if(
          generated.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
      REQUIRE(artifact != generated.value().end());
      const auto &text = artifact->text;
      CHECK(text.find("apb_target_data_widths") != std::string::npos);
      CHECK(text.find("physical_subwords_from_container_byte_lanes") != std::string::npos);
      // Narrow source and target faces both contribute a 32-bit engine to
      // default width selection, never an 8/16-bit AXI engine.
      for (auto &source : d.initiators) {
        set_protocol(source, Protocol::Apb4);
        source.data_width = 16;
      }
      REQUIRE(nocloom::generate(d));
      d.transport->data_width = 64;
      CHECK_FALSE(nocloom::make_plan(d));
    }
}

TEST_CASE("Public APB back-to-back mode is explicit and preserves its default", "[apb-cadence]") {
  using namespace nocloom;
  auto d = public_apb();
  const auto baseline = generate(d);
  REQUIRE(baseline);
  d.targets[1].apb_back_to_back = false;
  const auto explicit_default = generate(d);
  REQUIRE(explicit_default);
  CHECK(explicit_default.value() == baseline.value());
  d.targets[1].apb_back_to_back = true;
  for (auto dialect : {Protocol::Apb4, Protocol::Apb3, Protocol::Apb2}) {
    set_protocol(d.targets[1], dialect);
    const auto generated = generate(d);
    INFO(static_cast<unsigned>(dialect));
    INFO([&] {
      std::string message;
      for (const auto &d : generated.diagnostics())
        message += d.code + ": " + d.message + "\n";
      return message;
    }());
    REQUIRE(generated);
  }
  // Narrow APB targets take back-to-back access as well.
  for (auto width : {8U, 16U}) {
    d.targets[1].data_width = width;
    CHECK(generate(d));
  }
  d.targets[1].data_width = 32;
  set_protocol(d.targets[1], Protocol::Axi4Lite);
  CHECK_FALSE(generate(d));
}

TEST_CASE("Network user flags drive target AxUSER ports, match YAML and reject other shapes",
          "[apb-burst-example]") {
  using namespace nocloom;
  using S = AttributeSource;
  const auto code_of = [](const Definition &d) {
    const auto plan = make_plan(d);
    return plan ? std::string{} : plan.diagnostics().front().code;
  };
  auto base = public_apb();
  base.transport->user_flags = {"tagged", "secure"};
  base.initiators[0].user_width = 2;
  base.initiators[0].user_flags = {{"tagged", {S::User, false, 0}}, {"secure", {S::User, true, 1}}};
  base.initiators[1].user_flags = {{"tagged", {S::Modifiable}}, {"secure", {S::Zero}}};
  base.targets[0].user = {{"secure"}, {"tagged", false, true}, {"", true}};
  base.transport->response_user_bits = 2;
  base.initiators[0].response_user_width = 3;
  base.targets[0].response_user_width = 1;
  const auto api = generate(base);
  REQUIRE(api);
  const auto rtl = std::ranges::find_if(api.value(), [](const auto &a) {
    return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
           a.path != "nocloom_synchronizer.v";
  });
  REQUIRE(rtl != api.value().end());
  for (const auto *port :
       {"input wire [1:0] nl_i_cpu0_awuser", "input wire [1:0] nl_i_cpu0_aruser",
        "output wire [2:0] nl_t_bank0_awuser", "output wire [2:0] nl_t_bank0_aruser",
        "output wire [2:0] nl_i_cpu0_buser", "output wire [2:0] nl_i_cpu0_ruser",
        "input wire nl_t_bank0_buser", "input wire nl_t_bank0_ruser"})
    CHECK(rtl->text.find(port) != std::string::npos);
  CHECK(rtl->text.find("nl_i_cpu1_awuser") == std::string::npos);
  const auto parsed = config::parse(R"(schema_version: 10
connectivity: all
name: burst_apb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 4, len_width: 8, user_width: 2, response_user_width: 3, user_flags: {tagged: 'user[0]', secure: '~user[1]'}}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, id_width: 4, len_width: 8, user_flags: {tagged: modifiable, secure: 0}}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba, user: [secure, '~tagged', 1], response_user_width: 1}
  - {name: bank1, clock_domain: main, data_width: 32, protocol: apb4, mappings: [{base: '0x5000', size: '0x1000'}], attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4, user_flags: [tagged, secure], response_user_bits: 2}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  CHECK(generate(parsed.value().definition).value() == api.value());
  auto d = base;
  d.initiators[1].user_flags.erase("secure");
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.initiators[0].user_flags["secure"].index = 2;
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.initiators[1].user_flags["other"] = {S::One};
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.initiators[1].user_flags["tagged"] = {S::User};
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.targets[1].user = {{"tagged"}};
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.targets[0].user[0].flag = "unknown";
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.transport->user_flags.push_back("tagged");
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.targets[1].response_user_width = 1;
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.transport->response_user_bits = 257;
  CHECK(code_of(d) == "NCL_USER_FLAGS");
  d = base;
  d.targets[0].attributes->prot[0] = {S::User};
  CHECK(code_of(d) == "NCL_TARGET_ATTRIBUTES");
  const std::string unknown_transport = R"(schema_version: 10
connectivity: all
name: x
address_width: 32
reset_ports: [{name: reset_n}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, gated: false}]
initiators:
  - {name: a, clock_domain: main, protocol: axi4, data_width: 32}
targets:
  - {name: t, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
transport: {KEY: 1}
output: {backend: verilog, language: verilog_2001}
)";
  auto text = unknown_transport;
  text.replace(text.find("KEY"), 3, "unknown_option");
  const auto unknown = config::parse(text);
  REQUIRE_FALSE(unknown);
  CHECK(unknown.diagnostics().front().code == "NCL_SCHEMA_UNKNOWN_FIELD");
  CHECK(unknown.diagnostics().front().field == "transport.unknown_option");
}
