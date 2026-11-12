#include "../endpoint_ports.hpp"
#include "../public_artifacts.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <string>

TEST_CASE("AHB target admission preserves its bounded public contract", "[ahb-public]") {
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "ahb_public";
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  d.targets = {{"t0", "main", {{0x1000, 4096}}}, {"t1", "main", {{0x5000, 4096}}}};
  const auto widths = [](Definition &x, unsigned source, unsigned target) {
    for (auto &i : x.initiators) {
      i.protocol = Protocol::Axi4;
      i.data_width = source;
    }
    for (auto &t : x.targets)
      t.data_width = target;
  };
  for (auto &t : d.targets)
    t.protocol = Protocol::Axi4;
  d.targets[0].protocol = Protocol::AhbV3;
  // The AXI4 target's AxLEN carries a 1024-bit beat in 32 words.
  endpoint_ports(d, 4, 8);
  for (unsigned width : {32U, 64U, 128U, 256U, 512U, 1024U}) {
    widths(d, width, 32);
    REQUIRE(generate(d));
  }
  widths(d, 32, 32);
  const auto base = d;
  d.address_width = 40;
  REQUIRE_FALSE(generate(d));
  d = base;
  d.targets[0].address_width = 11;
  REQUIRE_FALSE(generate(d));
  // A 40-bit system address with a narrow local port is admitted.
  d = base;
  d.address_width = 40;
  d.targets[0].address_width = 12;
  REQUIRE(generate(d));
  d = base;
  widths(d, 64, 64);
  REQUIRE_FALSE(generate(d));
  d = base;
  set_protocol(d.targets[0], Protocol::Apb3);
  widths(d, 64, 64);
  const auto rejected = generate(d);
  REQUIRE_FALSE(rejected);
  CHECK(std::ranges::any_of(rejected.diagnostics(), [](const auto &diagnostic) {
    return diagnostic.code == "NCL_APB_BURST_OUTLET_PROFILE";
  }));
  // An AHB initiator next to the AHB target.
  d = base;
  d.initiators[0].protocol = Protocol::AhbV3;
  REQUIRE(generate(d));
  d = base;
  d.schema_version = 8;
  REQUIRE_FALSE(generate(d));
}

TEST_CASE("AHB composition accounts for the shared bus and refuses missing waits", "[ahb-public]") {
  using namespace nocloom;
  Definition d;
  d.connectivity = Connectivity{.all = true};
  d.name = "probe";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  d.targets = {{"t0", "main", {{0x1000, 4096}}}, {"t1", "main", {{0x5000, 4096}}}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
  }
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  d.targets[0].protocol = Protocol::AhbV3;
  endpoint_ports(d, 4, 8);
  ExchangeTopology topology;
  topology.request = {{{"a"}, {"b"}, {"m"}},
                      {{"a", "m"}, {"b", "m"}},
                      {{"s0", "a"}, {"s1", "b"}},
                      {{"t0", "m"}, {"t1", "m"}}};
  topology.response = {{{"x"}, {"y"}, {"n"}},
                       {{"x", "n"}, {"y", "n"}},
                       {{"t0", "x"}, {"t1", "y"}},
                       {{"s0", "n"}, {"s1", "n"}}};
  d.topology = topology;
  // The generator's own obligation check passed; the map reports the shared AHB bus resource
  // with its payload bound to storage of the same width.
  const auto a = public_artifacts(d);
  const auto *bus = a.resource("burst_service/t0/bus");
  REQUIRE(bus != nullptr);
  CHECK((*bus)["payload_bits"].get<unsigned>() > 0);
  CHECK(a.storage_bits("burst_service/t0/bus", "payload") ==
        (*bus)["payload_bits"].get<unsigned>());
  CHECK(a.resource("burst_service/t1/bus") == nullptr);
}

TEST_CASE("AHB targets take their own data width in an explicit graph", "[ahb-public]") {
  // ahb0 is an AHB target of ahb_bits; cpu0 a source of cpu0_bits; the
  // topology block is dropped when graph is false.
  const auto yaml = [](unsigned ahb_bits, unsigned cpu0_bits, bool graph,
                       const std::string &extra = "") {
    // The network stays 32 bits wide: an AHB target narrower than that
    // states it, as the narrowest target would otherwise set the network.
    std::string text = R"(schema_version: 10
connectivity: all
name: wide_ahb
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, id_width: 3, len_width: 8, data_width: )" +
                       std::to_string(cpu0_bits) + R"(}
  - {name: cpu1, clock_domain: main, protocol: axi4, id_width: 3, len_width: 8, data_width: 32}
targets:
  - {name: ahb0, clock_domain: main, protocol: ahb_v3, attributes: amba, data_width: )" +
                       std::to_string(ahb_bits) + extra +
                       R"(, mappings: [{base: '0x1000', size: '0x1000'}]}
  - {name: mem, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x10000', size: '0x10000'}], len_width: 8, attributes: amba}
transport:
)" + (ahb_bits < 32 ? "  data_width: 32\n" : "") +
                       R"(  max_outstanding: 1
)";
    if (graph)
      text += R"(topology:
  request:
    nodes: [{name: q0}]
    links: []
    entries: [{endpoint: cpu0, node: q0}, {endpoint: cpu1, node: q0}]
    exits: [{endpoint: ahb0, node: q0}, {endpoint: mem, node: q0}]
  response:
    nodes: [{name: p0}]
    links: []
    entries: [{endpoint: ahb0, node: p0}, {endpoint: mem, node: p0}]
    exits: [{endpoint: cpu0, node: p0}, {endpoint: cpu1, node: p0}]
)";
    return text + "output: {backend: verilog, language: verilog_2001}\n";
  };
  const auto outcome = [&](unsigned ahb_bits, unsigned cpu0_bits, bool graph = true,
                           const std::string &extra = "") {
    const auto parsed = nocloom::config::parse(yaml(ahb_bits, cpu0_bits, graph, extra));
    REQUIRE(parsed);
    const auto generated = nocloom::generate(parsed.value().definition);
    if (!generated)
      return generated.diagnostics().front().code + " " + generated.diagnostics().front().field;
    const auto map = std::ranges::find_if(generated.value(), [](const auto &artifact) {
      return artifact.path.ends_with(".map.json");
    });
    const auto rtl = std::ranges::find_if(generated.value(), [](const auto &artifact) {
      return artifact.path.ends_with(".v") && artifact.path != "nocloom_clock_gate.v" &&
             artifact.path != "nocloom_synchronizer.v";
    });
    REQUIRE(map != generated.value().end());
    REQUIRE(rtl != generated.value().end());
    // The wide data ports and the map contract follow the target width.
    const auto port = "nl_t_ahb0_hwdata";
    CHECK(rtl->text.find("[" + std::to_string(ahb_bits - 1) + ":0] " + port) != std::string::npos);
    CHECK((map->text.find("\"data_width\": \"32_to_1024_bits") != std::string::npos) ==
          (ahb_bits > 32));
    return std::string{};
  };
  for (unsigned bits : {64U, 128U, 256U, 512U, 1024U})
    for (unsigned cpu0 : {32U, bits})
      CHECK(outcome(bits, cpu0).empty());
  // A source wider than the AHB target clamps to it.
  CHECK(outcome(64, 256).empty());
  CHECK(outcome(128, 64, true,
                ", write_bursts: complete, undefined_length_bursts: false, "
                "hresp_width: 2, fixed_bursts: single, timeout: {cycles: 1024}")
            .empty());
  // A width beside the network width also works in the generated switch;
  // 8/16 and 2048 bits are rejected.
  CHECK(outcome(64, 32, false).empty());
  CHECK(outcome(16, 32) == "NCL_ENDPOINT_DATA_WIDTH targets[0].data_width");
  CHECK(outcome(8, 32) == "NCL_ENDPOINT_DATA_WIDTH targets[0].data_width");
  CHECK(outcome(2048, 32) == "NCL_ENDPOINT_DATA_WIDTH targets[0].data_width");
  // Packets cut at 1 KiB: 8 words of a 1024-bit AHB target.
  for (unsigned bits : {64U, 1024U}) {
    auto bounded = nocloom::config::parse(yaml(bits, bits, true));
    REQUIRE(bounded);
    CHECK(nocloom::generate(bounded.value().definition));
  }
  // A sub-page window holds at least one target word.
  auto small = nocloom::config::parse(yaml(1024, 32, true));
  REQUIRE(small);
  auto tiny = small.value().definition;
  tiny.targets[0].mappings[0].size = 64;
  const auto rejected = nocloom::generate(tiny);
  REQUIRE_FALSE(rejected);
  CHECK(rejected.diagnostics().front().code == "NCL_ADDRESS_RANGE");
  tiny.targets[0].mappings[0].size = 128;
  CHECK(nocloom::generate(tiny));
  // A local port narrower than a beat step or a WRAP container: HADDR wraps
  // modulo the port, so a 128-byte window with a 7-bit port generates.
  tiny.targets[0].address_width = 7;
  CHECK(nocloom::generate(tiny));
  // The same at 32 bits: an 8-byte window with a 3-bit port.
  auto narrow = nocloom::config::parse(yaml(32, 32, true));
  REQUIRE(narrow);
  auto eight = narrow.value().definition;
  eight.targets[0].mappings[0].size = 8;
  eight.targets[0].address_width = 3;
  CHECK(nocloom::generate(eight));
  // A wide target outside schema 10 has no data_width at all.
  auto parsed = nocloom::config::parse(yaml(64, 32, true));
  REQUIRE(parsed);
  auto definition = parsed.value().definition;
  definition.schema_version = 8;
  CHECK_FALSE(nocloom::generate(definition));
}

TEST_CASE("AHB initiators are AHB-Lite faces of AXI4 sources", "[ahb-public]") {
  using namespace nocloom;
  const std::string text = R"(schema_version: 10
name: ahb_source
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: mcu, clock_domain: main, protocol: ahb_v3, data_width: 32}
  - {name: dma, clock_domain: main, protocol: ahb_v3, data_width: 64}
targets:
  - {name: ram, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, attributes: amba,
     mappings: [{base: '0x0', size: '0x10000'}]}
  - {name: uart, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x10000', size: '0x1000'}]}
transport: {data_width: 32}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = config::parse(text, "ahb_source.yaml");
  REQUIRE(parsed);
  const auto base = parsed.value().definition;
  CHECK(base.initiators[0].protocol == Protocol::AhbV3);
  const auto generated = generate(base);
  REQUIRE(generated);
  CHECK(generated.value() == generate(base).value());
  const auto artifact = [&](const std::string &suffix) {
    const auto at = std::ranges::find_if(
        generated.value(), [&](const auto &a) { return a.path == "nocloom_ahb_source" + suffix; });
    REQUIRE(at != generated.value().end());
    return at->text;
  };
  const auto rtl = artifact(".v"), map = artifact(".map.json");
  for (const auto *port : {"hsel", "haddr", "htrans", "hwrite", "hsize", "hburst", "hprot",
                           "hmastlock", "hwdata", "hready", "hreadyout", "hresp", "hrdata"})
    CHECK(rtl.find(std::string("nl_i_mcu_") + port + ",") != std::string::npos);
  CHECK(rtl.find("[63:0] nl_i_dma_hwdata") != std::string::npos);
  for (const auto *axi : {"awaddr", "wdata", "bresp", "araddr", "rdata"})
    CHECK(rtl.find(std::string("nl_i_mcu_") + axi) == std::string::npos);
  CHECK(map.find(R"("mcu": "ahb_v3")") != std::string::npos);
  CHECK(map.find(R"("ahb_ingress")") != std::string::npos);
  const auto fails = [&](auto edit, const std::string &code, const std::string &field) {
    auto d = base;
    edit(d);
    const auto r = generate(d);
    REQUIRE_FALSE(r);
    CHECK(std::ranges::any_of(r.diagnostics(),
                              [&](const auto &x) { return x.code == code && x.field == field; }));
  };
  fails([](Definition &d) { d.initiators[0].id_width = 2; }, "NCL_AHB_INITIATOR",
        "initiators[0].id_width");
  fails([](Definition &d) { d.initiators[0].len_width = 4; }, "NCL_AHB_INITIATOR",
        "initiators[0].len_width");
  fails([](Definition &d) { d.initiators[0].access = Access::ReadOnly; }, "NCL_AHB_INITIATOR",
        "initiators[0].access");
  fails([](Definition &d) { d.initiators[0].narrow_bursts = NarrowBursts::Merge; },
        "NCL_AHB_INITIATOR", "initiators[0].narrow_bursts");
  fails([](Definition &d) { d.initiators[1].max_outstanding = 2; }, "NCL_AXI4_CAPACITY_COMBINATION",
        "initiators[1].max_outstanding");
  fails([](Definition &d) { d.initiators[0].data_width = 16; }, "NCL_ENDPOINT_DATA_WIDTH",
        "initiators[0].data_width");
  fails([](Definition &d) { d.initiators[0].address_width = 11; }, "NCL_ENDPOINT_ADDRESS_WIDTH",
        "initiators[0].address_width");
  fails([](Definition &d) { d.initiators[0].pipes.request = {.forward = true, .backward = true}; },
        "NCL_ENDPOINT_PIPE", "initiators[0].pipes.request");
  fails(
      [](Definition &d) {
        d.transport->packet_limits = {{.source = "mcu", .target = "ram", .max_bytes = 64}};
      },
      "NCL_AXI4_PACKETS", "transport.packet_limits[0]");
  // A wider AHB source reaches a 32-bit network through the wide-to-32 conversion.
  auto wide = base;
  wide.initiators[1].data_width = 1024;
  CHECK(generate(wide));
  auto other = text;
  other.replace(other.find("protocol: ahb_v3"), 16, "protocol: ahb_v5");
  const auto unknown = config::parse(other);
  REQUIRE_FALSE(unknown);
  CHECK(unknown.diagnostics().front().code == "NCL_UNSUPPORTED_PROTOCOL");
}

TEST_CASE("AHB5 properties are typed and checked at both endpoints", "[ahb-public]") {
  using namespace nocloom;
  Definition d;
  d.name = "ahb5_public";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"s0", "main"}};
  d.targets = {{"t0", "main", {{0x1000, 4096}}}};
  d.connectivity = Connectivity{.all = true};
  d.initiators[0].protocol = Protocol::Ahb5;
  d.initiators[0].data_width = 32;
  d.targets[0].protocol = Protocol::Ahb5;
  d.targets[0].data_width = 32;
  d.targets[0].attributes = TargetAttributes::amba();
  const auto base = d;
  const auto no_master = public_artifacts(d);
  const std::string writes =
      "bufferable_HPROT2_beats_complete_on_W_acceptance_and_ignore_the_B_unless_"
      "seven_bit_HPROT6_is_shareable_other_writes_complete_their_last_beat_with_the_B_"
      "and_preserve_errors";
  CHECK(no_master.map["definition"]["contract"]["ahb_ingress"]["writes"] == writes);
  CHECK(no_master.port_width("nl_i_s0_hprot") == 4);
  CHECK(no_master.port_width("nl_t_t0_hprot") == 4);
  CHECK_FALSE(no_master.has_port("nl_i_s0_hnonsec"));
  CHECK_FALSE(no_master.has_port("nl_t_t0_hnonsec"));
  CHECK_FALSE(no_master.has_port("nl_i_s0_hmaster"));
  CHECK_FALSE(no_master.has_port("nl_t_t0_hmaster"));
  for (const auto *signal : {"hexcl", "hexokay", "hauser", "hwuser", "hruser", "hbuser"}) {
    CHECK_FALSE(no_master.has_port(std::string("nl_i_s0_") + signal));
    CHECK_FALSE(no_master.has_port(std::string("nl_t_t0_") + signal));
  }
  d.initiators[0].ahb5 = Ahb5Properties{.hprot_width = 7, .hnonsec = true, .hmaster_width = 5};
  d.targets[0].ahb5 = Ahb5Properties{.hprot_width = 7, .hnonsec = true, .hmaster_width = 7};
  REQUIRE(generate(d));
  const auto valid = d;
  CHECK(public_artifacts(valid).map["definition"]["contract"]["ahb_ingress"]["writes"] == writes);
  for (bool source : {false, true}) {
    for (auto protocol : {Protocol::AhbV3, Protocol::Axi4, Protocol::Apb4}) {
      d = valid;
      (source ? d.initiators[0].protocol : d.targets[0].protocol) = protocol;
      (source ? d.initiators[0].ahb5 : d.targets[0].ahb5) = Ahb5Properties{};
      const auto result = generate(d);
      REQUIRE_FALSE(result);
      CHECK(std::ranges::any_of(result.diagnostics(), [&](const auto &diagnostic) {
        return diagnostic.code == "NCL_AHB5_PROPERTIES" &&
               diagnostic.field == (source ? "initiators[0].ahb5" : "targets[0].ahb5") &&
               diagnostic.message == "The ahb5 object requires protocol: ahb5";
      }));
    }
  }
  const auto refused = [&](const Definition &input, const std::string &code) {
    const auto result = generate(input);
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(), [&](const auto &x) { return x.code == code; }));
  };
  for (bool source : {false, true}) {
    for (unsigned option = 0; option < 7; ++option) {
      d = valid;
      auto &p = *(source ? d.initiators[0].ahb5 : d.targets[0].ahb5);
      if (option == 0)
        p.hmaster_width = 33;
      if (option == 1)
        p.exclusive = true;
      if (option == 2)
        p.hauser_width = 1;
      if (option == 3)
        p.hwuser_width = 1;
      if (option == 4)
        p.hruser_width = 1;
      if (option == 5)
        p.hbuser_width = 1;
      if (option == 6) {
        if (source)
          d.initiators[0].protocol = Protocol::AhbV3;
        else
          d.targets[0].protocol = Protocol::AhbV3;
      }
      refused(d, "NCL_AHB5_PROPERTIES");
    }
  }
  for (bool source : {false, true}) {
    for (unsigned width : {0U, 1U, 3U, 5U, 6U, 8U, 32U}) {
      d = valid;
      (source ? d.initiators[0].ahb5 : d.targets[0].ahb5)->hprot_width = width;
      refused(d, "NCL_AHB5_PROPERTIES");
    }
    for (unsigned width : {4U, 7U}) {
      d = valid;
      auto &properties = *(source ? d.initiators[0].ahb5 : d.targets[0].ahb5);
      properties.hprot_width = width;
      for (bool enabled : {false, true}) {
        properties.hnonsec = enabled;
        const auto artifacts = public_artifacts(d);
        const std::string prefix = source ? "nl_i_s0_" : "nl_t_t0_";
        CHECK(artifacts.port_width(prefix + "hprot") == width);
        CHECK(artifacts.has_port(prefix + "hnonsec") == enabled);
      }
    }
  }
  d = valid;
  d.targets[0].ahb5->hmaster_width = 4;
  refused(d, "NCL_AHB5_PROPERTIES");
  d = valid;
  d.targets[0].hresp_width = 2;
  refused(d, "NCL_TARGET_AHB_HRESP");
  d = valid;
  d.targets[0].attributes = TargetAttributes::zero();
  refused(d, "NCL_AHB5_PROPERTIES");
  d = base;
  d.initiators[0].ahb5 = Ahb5Properties{.hprot_width = 7, .hnonsec = true, .hmaster_width = 32};
  d.targets[0].ahb5 = Ahb5Properties{.hprot_width = 7, .hnonsec = true, .hmaster_width = 32};
  REQUIRE(generate(d));
}
