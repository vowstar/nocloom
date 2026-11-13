#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <tuple>

namespace {
// Schema 10: every endpoint states its protocol and data width.
void widths(nocloom::Definition &d, unsigned source, unsigned target) {
  for (auto &i : d.initiators)
    i.data_width = source;
  for (auto &t : d.targets)
    t.data_width = target;
}
nocloom::Definition profile(unsigned width = 32, unsigned target = 0) {
  nocloom::Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "burst_input";
  d.arbitration = nocloom::ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu0", "main"}, {"cpu1", "main"}};
  d.targets = {{"bank0", "main", {{0, 4096}}}, {"bank1", "main", {{4096, 4096}}}};
  for (auto &i : d.initiators)
    i.protocol = nocloom::Protocol::Axi4;
  for (auto &t : d.targets) {
    t.protocol = nocloom::Protocol::Axi4;
    t.attributes = nocloom::TargetAttributes::amba();
  }
  widths(d, width, target ? target : width);
  return d;
}
// Sets the ID and length ports of every initiator and the length port of every
// target, and returns the transport options.
nocloom::TransportOptions transport(nocloom::Definition &d, unsigned id, unsigned len,
                                    unsigned outstanding, unsigned word) {
  for (auto &i : d.initiators) {
    i.id_width = id;
    i.len_width = len;
  }
  for (auto &t : d.targets)
    t.len_width = len;
  return {.max_outstanding = outstanding, .transport_word_bytes = word};
}
void no_ports(nocloom::Initiator &i) {
  i.id_width.reset();
  i.len_width.reset();
}
template <typename T> void rejected(const nocloom::Result<T> &r, const char *code) {
  REQUIRE_FALSE(r);
  INFO("expected " << code);
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field);
  REQUIRE(std::any_of(r.diagnostics().begin(), r.diagnostics().end(),
                      [&](const auto &d) { return d.code == code; }));
}
} // namespace

TEST_CASE("AXI4 admitted profiles emit deterministic artifacts", "[axi4-config]") {
  for (unsigned width : {32U, 64U})
    for (unsigned id : {0U, 8U})
      for (unsigned len : {4U, 8U})
        for (unsigned word : {4U, 8U, 16U, 32U}) {
          auto d = profile(width);
          d.transport = transport(d, id, len, 1, word);
          auto plan = nocloom::make_plan(d);
          REQUIRE(plan);
          auto artifacts = nocloom::emit(plan.value());
          REQUIRE(artifacts);
          CHECK(artifacts.value().size() == 6);
          auto repeated = nocloom::generate(d);
          REQUIRE(repeated);
          CHECK(repeated.value() == artifacts.value());
        }
  REQUIRE(nocloom::make_plan(profile()));
}

TEST_CASE("AXI4 direct API cannot bypass the admitted input envelope", "[axi4-config]") {
  using D = nocloom::Definition;
  const std::vector<std::pair<std::function<void(D &)>, const char *>> cases = {
      {[](D &d) { d.initiators[0].id_width = 17; }, "NCL_ENDPOINT_TRANSACTION_WIDTH"},
      {[](D &d) { d.initiators[0].len_width = 9; }, "NCL_ENDPOINT_TRANSACTION_WIDTH"},
      {[](D &d) { d.targets[0].id_width = 32; }, "NCL_ENDPOINT_TRANSACTION_WIDTH"},
      {[](D &d) { d.targets[0].len_width = 3; }, "NCL_ENDPOINT_TRANSACTION_WIDTH"},
      {[](D &d) { d.targets[0].len_width = 9; }, "NCL_ENDPOINT_TRANSACTION_WIDTH"},
      {[](D &d) { d.targets[0].attributes.reset(); }, "NCL_TARGET_ATTRIBUTES"},
      {[](D &d) { d.transport->max_outstanding = 0; }, "NCL_AXI4_CAPACITY"},
      {[](D &d) { d.transport->max_outstanding = 65; }, "NCL_AXI4_CAPACITY"},
      {[](D &d) { d.transport->transport_word_bytes = 3; }, "NCL_AXI4_TRANSPORT_WORD"},
      {[](D &d) { d.transport->data_width = 128; }, "NCL_UNSUPPORTED_WIDTH"},
      {[](D &d) { widths(d, 128, 128); }, "NCL_WIDTH_CONVERSION"},
      // 32-bit sources make the network 32 bits; a 64-bit APB4 target does not fit.
      {[](D &d) {
         widths(d, 32, 64);
         d.targets[0].protocol = nocloom::Protocol::Apb4;
       },
       "NCL_ENDPOINT_DATA_WIDTH"},
      {[](D &d) {
         widths(d, 32, 64);
         d.targets[0].protocol = nocloom::Protocol::Apb4;
       },
       "NCL_NETWORK_DATA_WIDTH"},
      {[](D &d) { d.schema_version = 9; }, "NCL_SCHEMA_VERSION"},
      {[](D &d) { d.schema_version = 1; }, "NCL_SCHEMA_VERSION"},
      {[](D &d) { d.schema_version = 11; }, "NCL_SCHEMA_VERSION"},
      {[](D &d) { d.initiators[1].protocol.reset(); }, "NCL_SCHEMA_MISSING_FIELD"},
      {[](D &d) { d.targets[0].data_width.reset(); }, "NCL_SCHEMA_MISSING_FIELD"},
      {[](D &d) { d.initiators[0].protocol = nocloom::Protocol::Apb2; },
       "NCL_UNSUPPORTED_PROTOCOL"},
      {[](D &d) { d.initiators.clear(); }, "NCL_UNSUPPORTED_TOPOLOGY"},
      {[](D &d) { d.targets.clear(); }, "NCL_UNSUPPORTED_TOPOLOGY"},
      {[](D &d) { d.connectivity.reset(); }, "NCL_CONNECTIVITY_REQUIRED"},
      {[](D &d) { d.targets[0].mappings[0].size = 4095; }, "NCL_ADDRESS_RANGE"},
      {[](D &d) { d.targets[1].mappings[0].base = 4097; }, "NCL_ADDRESS_RANGE"},
      {[](D &d) { d.targets[1].mappings[0].base = 0; }, "NCL_ADDRESS_OVERLAP"},
      {[](D &d) { d.address_width = 1; }, "NCL_ADDRESS_WIDTH"},
      {[](D &d) { d.address_width = 64; }, "NCL_ADDRESS_WIDTH"},
      {[](D &d) { d.synchronizer_stages = 2; }, "NCL_SCHEMA_FIELD_VERSION"},
      {[](D &d) { d.targets[0].clock_domain = "other"; }, "NCL_DOMAIN_REFERENCE"},
  };
  for (const auto &[change, code] : cases) {
    INFO(code);
    auto d = profile();
    d.transport = nocloom::TransportOptions{};
    change(d);
    rejected(nocloom::make_plan(d), code);
  }
  for (unsigned schema = 1; schema <= 9; ++schema) {
    auto d = profile();
    d.schema_version = schema;
    rejected(nocloom::make_plan(d), "NCL_SCHEMA_VERSION");
  }
}

TEST_CASE("AXI4 YAML options agree with the direct API", "[axi4-config]") {
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: burst_input
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 64, id_width: 5, len_width: 4}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 64, id_width: 5, len_width: 4}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 64, mappings: [{base: '0x0', size: '0x1000'}], len_width: 4, attributes: amba}
  - {name: bank1, clock_domain: main, protocol: axi4, data_width: 64, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 4, attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
  auto parsed = nocloom::config::parse(yaml);
  REQUIRE(parsed);
  // Only schema 10 is accepted.
  auto other = yaml;
  other.replace(other.find("schema_version: 10"), 18, "schema_version: 9");
  rejected(nocloom::config::parse(other), "NCL_SCHEMA_VERSION");
  // Endpoint keys at the top level are unknown fields.
  for (const auto *key : {"protocol: axi4\n", "data_width: 64\n", "unknown_option: 1\n"}) {
    auto unknown_key = yaml;
    unknown_key.insert(unknown_key.find("address_width:"), key);
    rejected(nocloom::config::parse(unknown_key), "NCL_SCHEMA_UNKNOWN_FIELD");
  }
  auto missing = yaml;
  missing.replace(missing.find(", protocol: axi4, data_width: 64, mappings"), 16, "");
  rejected(nocloom::config::parse(missing), "NCL_SCHEMA_MISSING_FIELD");
  auto api = profile(64);
  api.transport = transport(api, 5, 4, 1, 4);
  const auto &d = parsed.value().definition;
  CHECK(d.transport == api.transport);
  CHECK(d.topology == api.topology);
  for (std::size_t i = 0; i < 2; ++i) {
    CHECK(d.initiators[i].protocol == api.initiators[i].protocol);
    CHECK(d.initiators[i].data_width == api.initiators[i].data_width);
    CHECK(d.initiators[i].id_width == api.initiators[i].id_width);
    CHECK(d.targets[i].len_width == api.targets[i].len_width);
    CHECK(d.targets[i].attributes == api.targets[i].attributes);
    CHECK(d.targets[i].protocol == api.targets[i].protocol);
    CHECK(d.targets[i].data_width == api.targets[i].data_width);
  }
  REQUIRE(nocloom::make_plan(d));
  REQUIRE(nocloom::make_plan(api));
  auto yaml_artifacts = nocloom::generate(d);
  auto api_artifacts = nocloom::generate(api);
  REQUIRE(yaml_artifacts);
  REQUIRE(api_artifacts);
  CHECK(yaml_artifacts.value() == api_artifacts.value());
}

TEST_CASE("AXI4 address ports respect the admitted width", "[axi4-config]") {
  auto d = profile();
  d.address_width = 13;
  auto generated = nocloom::generate(d);
  REQUIRE(generated);
  const auto &rtl = std::ranges::find_if(generated.value(), [](const auto &a) {
                      return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
                             a.path != "nocloom_synchronizer.v";
                    })->text;
  CHECK(rtl.find("[12:0] nl_i_cpu0_awaddr") != std::string::npos);
  CHECK(rtl.find("[12:0] nl_t_bank1_araddr") != std::string::npos);
  d.address_width = 12;
  rejected(nocloom::generate(d), "NCL_ADDRESS_RANGE");
}

TEST_CASE("AXI4 wide addresses distinguish windows above bit31", "[axi4-config]") {
  for (unsigned bits : {33U, 36U, 40U}) {
    auto d = profile();
    d.address_width = bits;
    const auto limit = std::uint64_t{1} << bits;
    d.targets[0].mappings = {{std::uint64_t{1} << 32, 4096}};
    d.targets[1].mappings = {{limit - 4096, 4096}};
    auto artifacts = nocloom::generate(d);
    REQUIRE(artifacts);
    CHECK(artifacts.value().size() == 6);
    d.targets[1].mappings[0].size += 4096;
    rejected(nocloom::generate(d), "NCL_ADDRESS_RANGE");
    d.targets[1].mappings = {{limit, 4096}};
    rejected(nocloom::generate(d), "NCL_ADDRESS_RANGE");
  }
  auto d = profile();
  d.address_width = 48;
  REQUIRE(nocloom::generate(d));
  d.address_width = 49;
  rejected(nocloom::generate(d), "NCL_ADDRESS_WIDTH");
}

TEST_CASE("AXI4 endpoint widths separate global source and local target addresses",
          "[axi4-config]") {
  auto d = profile();
  d.address_width = 40;
  d.targets[1].mappings[0].base = std::uint64_t{1} << 32;
  d.initiators[0].address_width = 40;
  d.initiators[1].address_width = 32;
  d.targets[0].address_width = 12;
  d.targets[1].address_width = 16;
  // bank1 lies above cpu1's address space.
  d.connectivity =
      nocloom::Connectivity{.pairs = {{"cpu0", "bank0"}, {"cpu0", "bank1"}, {"cpu1", "bank0"}}};
  auto generated = nocloom::generate(d);
  REQUIRE(generated);
  const auto &rtl = std::ranges::find_if(generated.value(), [](const auto &a) {
                      return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
                             a.path != "nocloom_synchronizer.v";
                    })->text;
  CHECK(rtl.find("[39:0] nl_i_cpu0_awaddr") != std::string::npos);
  CHECK(rtl.find("[31:0] nl_i_cpu1_araddr") != std::string::npos);
  CHECK(rtl.find("[11:0] nl_t_bank0_awaddr") != std::string::npos);
  CHECK(rtl.find("[15:0] nl_t_bank1_araddr") != std::string::npos);
  for (unsigned bad : {0U, 1U, 41U, 64U}) {
    auto changed = d;
    changed.initiators[1].address_width = bad;
    rejected(nocloom::generate(changed), "NCL_ENDPOINT_ADDRESS_WIDTH");
    changed = d;
    changed.targets[1].address_width = bad;
    rejected(nocloom::generate(changed), "NCL_ENDPOINT_ADDRESS_WIDTH");
  }
  for (unsigned bad : {2U, 11U}) {
    auto changed = d;
    changed.initiators[1].address_width = bad;
    rejected(nocloom::generate(changed), "NCL_ENDPOINT_ADDRESS_WIDTH");
  }
  d.targets[0].mappings[0].size = 8192;
  rejected(nocloom::generate(d), "NCL_ENDPOINT_ADDRESS_WIDTH");
  d = profile();
  const auto implicit = nocloom::generate(d);
  REQUIRE(implicit);
  for (auto &source : d.initiators)
    source.address_width = d.address_width;
  for (auto &target : d.targets)
    target.address_width = d.address_width;
  auto explicit_widths = nocloom::generate(d);
  REQUIRE(explicit_widths);
  CHECK(explicit_widths.value() == implicit.value());
}

TEST_CASE("AXI4 YAML endpoint widths are explicit and versioned", "[axi4-config]") {
  const std::string yaml = R"(schema_version: 10
connectivity: {cpu0: [bank0, bank1], cpu1: [bank0]}
name: burst_input
address_width: 40
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
 - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, address_width: 40}
 - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, address_width: 32}
targets:
 - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, address_width: 12, mappings: [{base: '0x0', size: '0x1000'}], attributes: amba}
 - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, address_width: 16, mappings: [{base: '0x100000000', size: '0x1000'}], attributes: amba}
output: {backend: verilog, language: verilog_2001}
)";
  auto parsed = nocloom::config::parse(yaml);
  REQUIRE(parsed);
  CHECK(parsed.value().sources.contains("initiators[1].address_width"));
  CHECK(parsed.value().sources.contains("targets[1].address_width"));
  auto api = profile();
  api.address_width = 40;
  api.initiators[0].address_width = 40;
  api.initiators[1].address_width = 32;
  api.targets[0].address_width = 12;
  api.targets[1].address_width = 16;
  api.targets[1].mappings[0].base = std::uint64_t{1} << 32;
  api.connectivity =
      nocloom::Connectivity{.pairs = {{"cpu0", "bank0"}, {"cpu0", "bank1"}, {"cpu1", "bank0"}}};
  auto from_yaml = nocloom::generate(parsed.value().definition);
  auto from_api = nocloom::generate(api);
  REQUIRE(from_yaml);
  REQUIRE(from_api);
  CHECK(from_yaml.value() == from_api.value());
  for (unsigned schema = 1; schema <= 8; ++schema) {
    auto other = yaml;
    other.replace(other.find("schema_version: 10"), 18,
                  "schema_version: " + std::to_string(schema));
    REQUIRE_FALSE(nocloom::config::parse(other));
  }
}

TEST_CASE("Schema 10 endpoint protocols preserve full defaults and expose real Lite ports",
          "[axi4-config]") {
  for (unsigned width : {32U, 64U}) {
    auto d = profile(width);
    d.transport = transport(d, 4, 4, 1, 4);
    const auto baseline = nocloom::generate(d);
    REQUIRE(baseline);
    for (auto &source : d.initiators)
      source.protocol = nocloom::Protocol::Axi4;
    const auto redundant = nocloom::generate(d);
    REQUIRE(redundant);
    CHECK(redundant.value() == baseline.value());
    for (unsigned lite_count : {1U, 2U}) {
      d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
      no_ports(d.initiators[1]);
      if (lite_count == 2) {
        d.initiators[0].protocol = nocloom::Protocol::Axi4Lite;
        no_ports(d.initiators[0]);
      }
      const auto mixed = nocloom::generate(d);
      REQUIRE(mixed);
      const auto rtl = std::ranges::find_if(mixed.value(), [](const auto &a) {
        return a.path.ends_with(".v") && a.path != "nocloom_clock_gate.v" &&
               a.path != "nocloom_synchronizer.v";
      });
      REQUIRE(rtl != mixed.value().end());
      CHECK(rtl->text.find("nl_i_cpu1_awaddr") != std::string::npos);
      CHECK(rtl->text.find("nl_i_cpu1_wstrb") != std::string::npos);
      for (const auto *signal :
           {"awlen", "arlen", "awsize", "arsize", "awid", "bid", "arid", "rid", "wlast", "rlast"})
        CHECK(rtl->text.find(std::string("nl_i_cpu1_") + signal) == std::string::npos);
      CHECK((rtl->text.find("nl_i_cpu0_awlen") == std::string::npos) == (lite_count == 2));
      CHECK(rtl->text.find("nl_t_bank0_awlen") != std::string::npos);
    }
    for (auto protocol : {nocloom::Protocol::Apb2, static_cast<nocloom::Protocol>(99)}) {
      d.initiators[1].protocol = protocol;
      rejected(nocloom::generate(d), "NCL_UNSUPPORTED_PROTOCOL");
    }
    // APB initiators reject a physical width above 32 bits.
    for (auto protocol : {nocloom::Protocol::Apb4, nocloom::Protocol::Apb3}) {
      d.initiators[1].protocol = protocol;
      if (width == 32)
        REQUIRE(nocloom::generate(d));
      else
        rejected(nocloom::generate(d), "NCL_ENDPOINT_DATA_WIDTH");
    }
  }
}

TEST_CASE("Endpoint protocol YAML matches Core and rejects other schema versions",
          "[axi4-config]") {
  std::string yaml = R"(schema_version: 10
connectivity: all
name: burst_input
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
 - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32}
 - {name: cpu1, clock_domain: main, data_width: 32, protocol: axi4_lite}
targets:
 - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], attributes: amba}
 - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = nocloom::config::parse(yaml);
  REQUIRE(parsed);
  CHECK(parsed.value().sources.contains("initiators[1].protocol"));
  auto d = profile();
  d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
  auto a = nocloom::generate(parsed.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  auto invalid = yaml;
  invalid.replace(invalid.find("protocol: axi4_lite"), 19, "protocol: unsupported");
  rejected(nocloom::config::parse(invalid), "NCL_UNSUPPORTED_PROTOCOL");
  // APB2 parses and is rejected with its reason by validation.
  auto apb2 = yaml;
  apb2.replace(apb2.find("protocol: axi4_lite"), 19, "protocol: apb2");
  const auto parsed_apb2 = nocloom::config::parse(apb2);
  REQUIRE(parsed_apb2);
  rejected(nocloom::generate(parsed_apb2.value().definition), "NCL_UNSUPPORTED_PROTOCOL");
  for (unsigned schema = 1; schema <= 9; ++schema) {
    auto other = yaml;
    other.replace(other.find("schema_version: 10"), 18,
                  "schema_version: " + std::to_string(schema));
    rejected(nocloom::config::parse(other), "NCL_SCHEMA_VERSION");
  }
}

TEST_CASE("Wide AXI ingress is converted only to the bounded AXI32 target profile",
          "[axi4-config]") {
  for (unsigned width : {64U, 128U, 256U, 512U, 1024U}) {
    auto d = profile(width, 32);
    d.transport = transport(d, 4, 8, 1, 32);
    for (unsigned address : {32U, 40U})
      for (unsigned id : {0U, 4U, 8U})
        for (unsigned word : {4U, 32U}) {
          d.address_width = address;
          for (auto &i : d.initiators)
            i.id_width = id;
          d.transport->transport_word_bytes = word;
          auto generated = nocloom::generate(d);
          REQUIRE(generated);
          CHECK(generated.value().size() == 6);
          CHECK(generated.value() == nocloom::generate(d).value());
        }
    d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
    no_ports(d.initiators[1]);
    if (width == 64)
      REQUIRE(nocloom::generate(d));
    else
      rejected(nocloom::generate(d), "NCL_UNSUPPORTED_PROTOCOL");
    d.initiators[1].protocol = nocloom::Protocol::Axi4;
    // A four-bit target AxLEN carries a source beat of at most 16 target
    // words; the source ports and the network field do not limit it.
    for (auto &t : d.targets)
      t.len_width = 4;
    if (width == 1024)
      rejected(nocloom::generate(d), "NCL_ENDPOINT_TRANSACTION_WIDTH");
    else
      CHECK(nocloom::generate(d));
    // APB4 and AXI4-Lite targets take 128-byte packets whatever the source.
    for (auto protocol : {nocloom::Protocol::Apb4, nocloom::Protocol::Axi4Lite}) {
      auto dedicated = d;
      dedicated.address_width = 32;
      for (auto &t : dedicated.targets) {
        t.protocol = protocol;
        t.len_width.reset();
      }
      CHECK(nocloom::generate(dedicated));
    }
    for (auto &t : d.targets)
      t.len_width = 8;
    // Other target widths: a 64-bit network takes no wider source, and a
    // 128-bit AXI4 target beside the 64-bit network attaches at its own width.
    widths(d, width, width == 64 ? 128 : 64);
    if (width == 64)
      CHECK(nocloom::generate(d));
    else
      rejected(nocloom::generate(d), "NCL_WIDTH_CONVERSION");
  }
}

TEST_CASE("Schema 10 source data widths are independent and deterministic", "[axi4-config]") {
  for (unsigned width : {32U, 64U, 128U, 256U, 512U, 1024U}) {
    auto d = profile(width, 32);
    d.transport = transport(d, 4, 8, 1, 32);
    const auto baseline = nocloom::generate(d);
    REQUIRE(baseline);
    REQUIRE(nocloom::generate(d));
    CHECK(nocloom::generate(d).value() == baseline.value());
    d.initiators[1].data_width = 32;
    REQUIRE(nocloom::generate(d));
    d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
    no_ports(d.initiators[1]);
    REQUIRE(nocloom::generate(d));
    d.initiators[1].data_width = 128;
    rejected(nocloom::generate(d), "NCL_UNSUPPORTED_PROTOCOL");
  }
  auto d = profile();
  d.initiators[1].data_width = 1024;
  d.transport = transport(d, 0, 4, 1, 4);
  rejected(nocloom::generate(d), "NCL_ENDPOINT_TRANSACTION_WIDTH");
  for (auto &t : d.targets)
    t.len_width = 8;
  REQUIRE(nocloom::generate(d));
  for (unsigned width : {0U, 8U, 16U, 96U, 2048U}) {
    d.initiators[1].data_width = width;
    rejected(nocloom::generate(d), "NCL_ENDPOINT_DATA_WIDTH");
  }
}

TEST_CASE("Schema 10 YAML preserves each source data width", "[axi4-config]") {
  const auto parsed = nocloom::config::parse(R"(schema_version: 10
connectivity: all
name: independent_widths
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
 - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 128}
 - {name: cpu1, clock_domain: main, data_width: 32, protocol: axi4_lite}
targets:
 - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], attributes: amba}
 - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], attributes: amba}
output: {backend: verilog, language: verilog_2001}
)");
  REQUIRE(parsed);
  CHECK(parsed.value().sources.contains("initiators[0].data_width"));
  auto d = profile();
  d.name = "independent_widths";
  d.initiators[0].data_width = 128;
  d.initiators[1].data_width = 32;
  d.initiators[1].protocol = nocloom::Protocol::Axi4Lite;
  const auto a = nocloom::generate(parsed.value().definition);
  const auto b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
}

TEST_CASE("Schema 10 APB initiators expose APB faces over single-beat sources", "[axi4-config]") {
  using nocloom::Protocol;
  auto base = profile();
  base.transport = transport(base, 2, 8, 1, 4);
  no_ports(base.initiators[1]);
  auto artifact = [](const std::vector<nocloom::Artifact> &set, const char *suffix) {
    const auto it = std::ranges::find_if(set, [&](const auto &a) {
      return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v" &&
             a.path != "nocloom_synchronizer.v";
    });
    REQUIRE(it != set.end());
    return it->text;
  };
  for (auto protocol : {Protocol::Apb4, Protocol::Apb3}) {
    auto d = base;
    d.initiators[1].protocol = protocol;
    const auto first = nocloom::generate(d), again = nocloom::generate(d);
    REQUIRE(first);
    REQUIRE(again);
    CHECK(first.value() == again.value());
    const auto rtl = artifact(first.value(), ".v");
    for (const auto *signal :
         {"psel", "penable", "pwrite", "paddr", "pwdata", "pready", "prdata", "pslverr"})
      CHECK(rtl.find(std::string("nl_i_cpu1_") + signal) != std::string::npos);
    for (const auto *signal : {"pstrb", "pprot"})
      CHECK((rtl.find(std::string("nl_i_cpu1_") + signal) != std::string::npos) ==
            (protocol == Protocol::Apb4));
    for (const auto *signal : {"awaddr", "awvalid", "wdata", "bvalid", "araddr", "rdata"})
      CHECK(rtl.find(std::string("nl_i_cpu1_") + signal) == std::string::npos);
    CHECK(rtl.find("nl_i_cpu0_awlen") != std::string::npos);
    const auto map = artifact(first.value(), ".map.json");
    CHECK(map.find(protocol == Protocol::Apb4 ? R"("cpu1": "apb4")" : R"("cpu1": "apb3")") !=
          std::string::npos);
    CHECK(map.find("apb_ingress") != std::string::npos);
    // Only an AXI4-Lite initiator adds the Lite ingress contract.
    CHECK(map.find("lite_ingress") == std::string::npos);
  }
  // YAML names the same initiators.
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: burst_input
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
 - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 8}
 - {name: cpu1, clock_domain: main, data_width: 32, protocol: apb3}
targets:
 - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], len_width: 8, attributes: amba}
 - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = nocloom::config::parse(yaml);
  REQUIRE(parsed);
  auto direct = base;
  direct.initiators[1].protocol = Protocol::Apb3;
  const auto from_yaml = nocloom::generate(parsed.value().definition);
  const auto from_api = nocloom::generate(direct);
  REQUIRE(from_yaml);
  REQUIRE(from_api);
  CHECK(from_yaml.value() == from_api.value());

  using D = nocloom::Definition;
  const std::vector<std::tuple<std::function<void(D &)>, const char *, const char *>> cases = {
      {[](D &d) { d.initiators[1].data_width = 24; }, "NCL_ENDPOINT_DATA_WIDTH",
       "initiators[1].data_width"},
      {[](D &d) { d.initiators[1].data_width = 64; }, "NCL_ENDPOINT_DATA_WIDTH",
       "initiators[1].data_width"},
      {[](D &d) { widths(d, 64, 64); }, "NCL_ENDPOINT_DATA_WIDTH", "initiators[1].data_width"},
      {[](D &d) {
         d.transport->data_width = 64;
         for (auto &t : d.targets)
           t.data_width = 64;
         d.initiators[0].data_width = 64;
       },
       "NCL_ENDPOINT_DATA_WIDTH", "initiators[1].data_width"},
      {[](D &d) { d.initiators[1].id_width = 1; }, "NCL_APB_INITIATOR", "initiators[1].id_width"},
      {[](D &d) { d.initiators[1].len_width = 4; }, "NCL_APB_INITIATOR", "initiators[1].len_width"},
      {[](D &d) { d.initiators[1].access = nocloom::Access::ReadOnly; }, "NCL_APB_INITIATOR",
       "initiators[1].access"},
      {[](D &d) { d.initiators[1].narrow_bursts = nocloom::NarrowBursts::Merge; },
       "NCL_APB_INITIATOR", "initiators[1].narrow_bursts"},
      {[](D &d) { d.initiators[1].max_outstanding = 2; }, "NCL_AXI4_CAPACITY_COMBINATION",
       "initiators[1].max_outstanding"},
      {[](D &d) {
         d.transport->max_outstanding = 2;
         d.initiators[0].max_outstanding = 2;
       },
       "NCL_AXI4_CAPACITY_COMBINATION", "transport.max_outstanding"},
      {[](D &d) { d.address_width = 40; }, "NCL_ENDPOINT_ADDRESS_WIDTH",
       "initiators[1].address_width"},
      {[](D &d) { d.initiators[1].id_table_entries = 1; }, "NCL_AXI4_ID_CAPACITY",
       "initiators[1].id_table_entries"},
  };
  for (const auto &[change, code, field] : cases) {
    auto d = base;
    d.initiators[1].protocol = Protocol::Apb4;
    change(d);
    CAPTURE(code, field);
    const auto plan = nocloom::make_plan(d);
    rejected(plan, code);
    // One diagnostic names the offending field.
    CHECK(std::ranges::count_if(plan.diagnostics(),
                                [&](const auto &x) { return x.field == field; }) == 1);
    // Other diagnostics may only name the network width the endpoints imply.
    for (const auto &x : plan.diagnostics())
      UNSCOPED_INFO(x.code << " " << x.field << " " << x.message);
    CHECK(std::ranges::all_of(plan.diagnostics(), [&](const auto &x) {
      return x.field == field || x.code == "NCL_NETWORK_DATA_WIDTH";
    }));
  }
  // The physical APB face can be narrower than its 32-bit internal engine.
  for (auto protocol : {Protocol::Apb3, Protocol::Apb4})
    for (unsigned bits : {8U, 16U})
      for (bool uniform : {false, true}) {
        auto d = base;
        d.transport->data_width.reset();
        d.initiators[1].protocol = protocol;
        d.initiators[1].data_width = bits;
        if (uniform) {
          d.initiators[0].protocol = protocol;
          d.initiators[0].data_width = bits;
          no_ports(d.initiators[0]);
        }
        CAPTURE(protocol, bits, uniform);
        const auto generated = nocloom::generate(d);
        REQUIRE(generated);
        const auto map = std::ranges::find_if(
            generated.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
        REQUIRE(map != generated.value().end());
        CHECK(map->text.find("source_word_address_selects_32bit_byte_lanes") != std::string::npos);
      }
  // A shared capacity error names each APB initiator.
  {
    auto d = base;
    d.initiators[0].protocol = Protocol::Apb4;
    d.initiators[1].protocol = Protocol::Apb3;
    d.transport->max_outstanding = 2;
    const auto plan = nocloom::make_plan(d);
    rejected(plan, "NCL_AXI4_CAPACITY_COMBINATION");
    for (const auto *name : {"cpu0", "cpu1"})
      CHECK(std::ranges::count_if(plan.diagnostics(), [&](const auto &x) {
              return x.message.find(std::string("APB initiator ") + name + " ") !=
                     std::string::npos;
            }) == 1);
  }
  // A network width cannot repair an APB2 or invalid-width APB initiator: no
  // transport.data_width hint.
  for (const auto &[protocol, width] :
       {std::pair{Protocol::Apb2, 32U}, std::pair{Protocol::Apb4, 24U},
        std::pair{Protocol::Apb3, 128U}, std::pair{Protocol::Apb4, 64U}}) {
    auto d = base;
    d.transport->data_width.reset();
    d.initiators[1].protocol = protocol;
    d.initiators[1].data_width = width;
    const auto plan = nocloom::make_plan(d);
    REQUIRE_FALSE(plan);
    CHECK(std::ranges::none_of(plan.diagnostics(),
                               [](const auto &x) { return x.code == "NCL_NETWORK_DATA_WIDTH"; }));
  }
  // A narrower APB address port inside a 40-bit network is accepted.
  auto narrow = base;
  narrow.address_width = 40;
  narrow.initiators[1].protocol = Protocol::Apb4;
  narrow.initiators[1].address_width = 20;
  REQUIRE(nocloom::make_plan(narrow));
}

TEST_CASE("Single-transfer sources admit small address ports", "[axi4-config]") {
  for (auto protocol :
       {nocloom::Protocol::Apb4, nocloom::Protocol::Apb3, nocloom::Protocol::Axi4Lite}) {
    const unsigned minimum = protocol == nocloom::Protocol::Axi4Lite ? 2U : 1U;
    for (unsigned bits : {minimum, 11U, 32U}) {
      auto d = profile();
      d.initiators[0].protocol = protocol;
      d.initiators[0].address_width = bits;
      // bank1 lies above the small address spaces.
      d.connectivity =
          nocloom::Connectivity{.pairs = {{"cpu0", "bank0"}, {"cpu1", "bank0"}, {"cpu1", "bank1"}}};
      CAPTURE(protocol, bits);
      REQUIRE(nocloom::generate(d));
      d.initiators[0].address_width = minimum - 1;
      rejected(nocloom::make_plan(d), "NCL_ENDPOINT_ADDRESS_WIDTH");
    }
    auto d = profile();
    d.address_width = 2;
    d.initiators.resize(1);
    d.initiators[0].protocol = protocol;
    d.targets.resize(1);
    d.targets[0].protocol = nocloom::Protocol::Apb4;
    d.targets[0].mappings = {{0, 4}};
    REQUIRE(nocloom::generate(d));
    d.address_width = 1;
    CHECK_FALSE(nocloom::generate(d));
  }
  auto d = profile();
  d.address_width = 40;
  d.initiators[0].protocol = nocloom::Protocol::Axi4Lite;
  d.initiators[0].address_width = 40;
  REQUIRE(nocloom::generate(d));
}

TEST_CASE("Mappings are whole pages or size-aligned power-of-two sub-pages", "[axi4-config]") {
  using nocloom::Protocol;
  auto d = profile();
  for (auto &i : d.initiators)
    i.protocol = Protocol::Axi4Lite;
  d.targets[0].protocol = Protocol::Apb4;
  d.targets[1].protocol = Protocol::Axi4Lite;
  d.targets[0].mappings = {{0x1000, 0x100}};
  d.targets[1].mappings = {{0x2000, 0x80}};
  REQUIRE(nocloom::generate(d));
  // Sub-pages of two targets share a page; one target has several mappings, a
  // local address relocates one.
  d.targets[1].mappings = {{0x1100, 0x80}, {0x2080, 0x80, 0x400}};
  REQUIRE(nocloom::generate(d));
  // A smaller mapping inside another decodes first, also of the same target.
  d.targets[1].mappings = {{0x1080, 0x40}};
  REQUIRE(nocloom::generate(d));
  d.targets[0].mappings = {{0x1000, 0x100}, {0x10c0, 0x10, 0x400}};
  REQUIRE(nocloom::generate(d));
  // A base or local address off the size, a size that is not a power of two, an
  // overlap of one size, a word-sized hole an AXI4-Lite mapping keeps and no mapping
  // at all are rejected.
  d.targets[1].data_width = 64;
  for (const auto &[mappings, code] :
       std::vector<std::pair<std::vector<nocloom::AddressMapping>, const char *>>{
           {{{0x2040, 0x80}}, "NCL_ADDRESS_RANGE"},
           {{{0x2000, 0x80, 0x40}}, "NCL_ADDRESS_RANGE"},
           {{{0x2000, 0x90}}, "NCL_ADDRESS_RANGE"},
           {{{0x1000, 0x100}}, "NCL_ADDRESS_OVERLAP"},
           {{{0x10c0, 0x40}, {0x10c0, 0x10}}, "NCL_ADDRESS_OVERLAP"},
           {{}, "NCL_ADDRESS_RANGE"}}) {
    d.targets[1].mappings = mappings;
    rejected(nocloom::make_plan(d), code);
  }
  d.targets[0].mappings = {{0x2004, 0x4}};
  d.targets[1].mappings = {{0x2000, 0x10}};
  rejected(nocloom::make_plan(d), "NCL_ADDRESS_RANGE");
  // An initiator reaching a mapping reaches every smaller one inside it.
  d.targets[0].mappings = {{0x2008, 0x8}};
  d.connectivity =
      nocloom::Connectivity{.pairs = {{"cpu0", "bank0"}, {"cpu0", "bank1"}, {"cpu1", "bank1"}}};
  rejected(nocloom::make_plan(d), "NCL_ADDRESS_OVERLAP");
  d.connectivity->pairs[2].target = "bank0";
  REQUIRE(nocloom::generate(d));
}

TEST_CASE("Full AXI4 sources reach shared pages whole", "[axi4-config]") {
  using nocloom::Protocol;
  auto d = profile();
  d.transport = transport(d, 2, 8, 1, 4);
  for (auto &t : d.targets) {
    t.protocol = Protocol::Apb4;
    t.len_width.reset();
  }
  // bank0 and bank1 share page 0; bank0 also maps a relocated page.
  d.targets[0].mappings = {{0x0, 0x100}, {0x2000, 0x1000, 0x1000}};
  d.targets[1].mappings = {{0x100, 0x100, 0x400}};
  d.targets[0].address_width = d.targets[1].address_width = 13;
  const auto generated = nocloom::generate(d);
  for (const auto &diagnostic : generated.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.field << " " << diagnostic.message);
  REQUIRE(generated);
  const auto map = std::ranges::find_if(
      generated.value(), [](const nocloom::Artifact &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != generated.value().end());
  CHECK(map->text.find("\"local\": \"1024\"") != std::string::npos);
  // A source missing a page neighbour takes its mappings as decode misses, and
  // one with several bursts in flight binds an ID to the last packet's target;
  // a local interval past the port is rejected.
  auto partial = d;
  partial.connectivity =
      nocloom::Connectivity{.pairs = {{"cpu0", "bank0"}, {"cpu1", "bank0"}, {"cpu1", "bank1"}}};
  REQUIRE(nocloom::generate(partial));
  auto narrow = d;
  narrow.targets[1].address_width = 10;
  rejected(nocloom::make_plan(narrow), "NCL_ENDPOINT_ADDRESS_WIDTH");
  auto queued = d;
  queued.transport->max_outstanding = 4;
  REQUIRE(nocloom::generate(queued));
}

TEST_CASE("Extended AXI IDs retain the source limits", "[axi4-config]") {
  auto d = profile();
  d.transport = transport(d, 16, 8, 1, 4);
  d.initiators[1].id_width = 9;
  const auto artifacts = nocloom::generate(d);
  REQUIRE(artifacts);
  CHECK(artifacts.value().size() == 6);
  d.initiators[1].id_width = 17;
  rejected(nocloom::make_plan(d), "NCL_ENDPOINT_TRANSACTION_WIDTH");
}

TEST_CASE("APB5 user widths follow endpoint signal limits", "[axi4-config][apb5]") {
  using namespace nocloom;
  auto d = profile();
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Apb5;
    i.user_width = 128;
    i.response_user_width = 16;
  }
  for (auto &t : d.targets) {
    t.protocol = Protocol::Apb5;
    t.user.resize(128);
    t.response_user_width = 16;
  }
  REQUIRE(validate(d));
  SECTION("request input overflow") {
    d.initiators[0].user_width = 129;
    rejected(validate(d), "NCL_APB5_USER_WIDTH");
  }
  SECTION("response output overflow") {
    d.initiators[0].response_user_width = 17;
    rejected(validate(d), "NCL_APB5_USER_WIDTH");
  }
  SECTION("request output overflow") {
    d.targets[0].user.resize(129);
    rejected(validate(d), "NCL_APB5_USER_WIDTH");
  }
  SECTION("response input overflow") {
    d.targets[0].response_user_width = 17;
    rejected(validate(d), "NCL_APB5_USER_WIDTH");
  }
  SECTION("explicit zero widths") {
    for (auto &i : d.initiators) {
      i.user_width = 0;
      i.response_user_width = 0;
    }
    for (auto &t : d.targets) {
      t.user.clear();
      t.response_user_width = 0;
    }
    REQUIRE(validate(d));
    REQUIRE(generate(d));
  }
  SECTION("APB initiator options remain restricted") {
    d.initiators[0].id_width = 1;
    rejected(validate(d), "NCL_APB_INITIATOR");
  }
}

TEST_CASE("Target option diagnostics list APB5", "[axi4-config][apb5]") {
  using namespace nocloom;
  auto d = profile();
  d.targets[0].apb_back_to_back = true;
  d.targets[0].fixed_bursts = FixedBursts::Single;
  const auto result = validate(d);
  REQUIRE_FALSE(result);
  for (const auto &[code, field, message] :
       std::vector<std::tuple<const char *, const char *, const char *>>{
           {"NCL_TARGET_APB_ACCESS", "targets[0].apb_back_to_back",
            "Back-to-back access requires an APB5, APB4, APB3 or APB2 target"},
           {"NCL_TARGET_FIXED_BURSTS", "targets[0].fixed_bursts",
            "AXI4 targets receive FIXED bursts unchanged; fixed_bursts applies to dedicated apb5, "
            "apb4, apb3, apb2, axi4_lite and AHB targets"}}) {
    CAPTURE(code);
    const auto at = std::ranges::find_if(result.diagnostics(), [&](const auto &diag) {
      return diag.code == code && diag.field == field;
    });
    REQUIRE(at != result.diagnostics().end());
    CHECK(at->message == message);
  }
  d.targets[0].protocol = Protocol::Apb5;
  REQUIRE(validate(d));
}
