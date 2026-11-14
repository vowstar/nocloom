#include "core/wishbone.hpp"
#include "../public_artifacts.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <nocloom/ipxact.hpp>

namespace {
const char *const yaml = R"(schema_version: 10
name: wb
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: wishbone, data_width: 32, address_unit: byte}
targets:
  - {name: mem, clock_domain: main, protocol: wishbone, data_width: 32, address_unit: word,
     mappings: [{base: '0x0', size: '0x1000'}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
nocloom::Definition definition() {
  const auto parsed = nocloom::config::parse(yaml, "wb.yaml");
  REQUIRE(parsed);
  return parsed.value().definition;
}
} // namespace
TEST_CASE("Wishbone options are checked through the public definition", "[wishbone]") {
  using namespace nocloom;
  auto d = definition();
  REQUIRE(generate(d));
  const auto fails = [&](auto edit, const std::string &code) {
    auto invalid = d;
    edit(invalid);
    const auto result = generate(invalid);
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(), [&](const auto &x) { return x.code == code; }));
  };
  fails([](auto &x) { x.initiators[0].address_unit.reset(); }, "NCL_WISHBONE_ADDRESS_UNIT");
  fails([](auto &x) { x.targets[0].address_unit.reset(); }, "NCL_WISHBONE_ADDRESS_UNIT");
  fails([](auto &x) { x.initiators[0].address_unit = static_cast<AddressUnit>(9); },
        "NCL_WISHBONE_ADDRESS_UNIT");
  fails([](auto &x) { x.initiators[0].retry = WishboneRetry::Error; }, "NCL_WISHBONE_OPTION");
  fails([](auto &x) { x.initiators[0].id_width = 0; }, "NCL_WISHBONE_OPTION");
  fails([](auto &x) { x.initiators[0].max_outstanding = 2; }, "NCL_AXI4_CAPACITY_COMBINATION");
  fails([](auto &x) { x.targets[0].max_outstanding = 2; }, "NCL_AXI4_CAPACITY_COMBINATION");
  fails([](auto &x) { x.targets[0].attributes = TargetAttributes{}; }, "NCL_TARGET_ATTRIBUTES");
  for (const auto bits : {8U, 16U, 64U, 128U}) {
    fails([&](auto &x) { x.initiators[0].data_width = bits; }, "NCL_ENDPOINT_DATA_WIDTH");
    fails([&](auto &x) { x.targets[0].data_width = bits; }, "NCL_ENDPOINT_DATA_WIDTH");
  }
  d.targets[0].retry = WishboneRetry::Error;
  REQUIRE(generate(d));
  const auto outputs = public_artifacts(d).map["definition"]["contract"]["wishbone"]["outputs"];
  CHECK(outputs["initiators"] == "registered_terminations_gated_by_bus");
  CHECK(outputs["targets"] == "registered");
}

TEST_CASE("Wishbone termination outputs qualify registers with bus activity", "[wishbone]") {
  using namespace nocloom::detail;
  Axi4InitiatorProfile source;
  source.name = "source";
  source.port_pipes = false;
  BurstOutletProfile target;
  target.name = "target";
  target.wishbone = true;
  for (bool word : {false, true}) {
    for (const auto &node :
         {lower_wishbone_initiator(source, word), lower_wishbone_target(target, word, true),
          lower_wishbone_initiator(source, word, true),
          lower_wishbone_target(target, word, true, true)}) {
      REQUIRE(node);
      for (const auto &driver : node.value().ir.drivers) {
        const auto &port = node.value().ir.ports.at(driver.port.index);
        if (port.endpoint != "wb")
          continue;
        const bool termination = port.signal == "ack" || port.signal == "err";
        CHECK(node.value().ir.values.at(driver.value.index).op ==
              (termination ? Op::BitAnd : Op::State));
      }
    }
  }
}
TEST_CASE("Wishbone optional artifacts have no unresolved bus links", "[wishbone]") {
  using namespace nocloom;
  auto d = definition();
  d.initiators[0].protocol = Protocol::Axi4;
  d.initiators[0].address_unit.reset();
  const auto generated = generate(d);
  REQUIRE(generated);
  const auto table = add_address_table(generated.value());
  REQUIRE(table);
  const auto with_xml = add_ipxact(d, table.value());
  REQUIRE(with_xml);
  for (const auto &file : with_xml.value())
    if (file.path.ends_with(".ipxact.xml")) {
      CHECK(file.text.find("nl_t_mem_adr") != std::string::npos);
      CHECK(file.text.find("masterRef=\"mem\"") == std::string::npos);
    }
}

TEST_CASE("Wishbone rejects unknown cycle and tag fields", "[wishbone]") {
  for (const auto *field : {"cycles: invalid", "burst_tags: true", "granularity: 16",
                            "endianness: big", "lock: true", "cti: 0", "bte: 0", "tags: true"}) {
    std::string text = yaml;
    const auto at = text.find("address_unit: byte");
    text.insert(at, std::string(field) + ", ");
    const auto parsed = nocloom::config::parse(text, "wb.yaml");
    REQUIRE_FALSE(parsed);
    CHECK(parsed.diagnostics().front().code == (std::string(field).starts_with("cycles:")
                                                    ? "NCL_WISHBONE_CYCLES"
                                                    : "NCL_SCHEMA_UNKNOWN_FIELD"));
  }
}

TEST_CASE("APB scheduling levels require APB target protocols", "[wishbone][fixed-qos]") {
  using namespace nocloom;
  for (const auto *qos : {"qos: {levels: {read: 1, write: 1}}",
                          "qos: {shaper: {bytes_per_1024_cycles: 64, burst_bytes: 64}, "
                          "scheduler: {under_level: 3, over_level: 0, threshold_percent: 50}}"}) {
    std::string text = yaml;
    const auto at = text.find("protocol: wishbone, data_width: 32, address_unit: byte");
    text.replace(at, std::string("protocol: wishbone, data_width: 32, address_unit: byte").size(),
                 std::string("protocol: apb4, data_width: 32, ") + qos);
    const auto parsed = config::parse(text, "wb.yaml");
    REQUIRE(parsed);
    auto d = parsed.value().definition;
    const auto rejected = generate(d);
    REQUIRE_FALSE(rejected);
    CHECK(std::ranges::any_of(rejected.diagnostics(), [](const auto &x) {
      return x.code == "NCL_QOS_PROFILE" && x.field == "exchange";
    }));
    d.targets[0].protocol = Protocol::Apb4;
    d.targets[0].address_unit.reset();
    d.targets[0].attributes = TargetAttributes{};
    REQUIRE(generate(d));
    auto wb = parsed.value().definition.targets[0];
    wb.name = "wb_mem";
    wb.mappings[0].base = 0x1000;
    d.targets.push_back(wb);
    CHECK_FALSE(generate(d));
    d.initiators[0].qos = {};
    d.initiators[0].qos.shaper = QosShaper{64, 64};
    REQUIRE(generate(d));
  }
}

TEST_CASE("Wishbone diagnostics state its width address and capacity limits", "[wishbone]") {
  using namespace nocloom;
  const auto message = [](const auto &d, const std::string &code, const std::string &field) {
    const auto result = generate(d);
    REQUIRE_FALSE(result);
    const auto at = std::ranges::find_if(
        result.diagnostics(), [&](const auto &x) { return x.code == code && x.field == field; });
    REQUIRE(at != result.diagnostics().end());
    return at->message;
  };
  for (const auto bits : {8U, 16U, 64U, 128U}) {
    auto d = definition();
    d.initiators[0].data_width = bits;
    CHECK(message(d, "NCL_ENDPOINT_DATA_WIDTH", "initiators[0].data_width") ==
          "Wishbone requires 32-bit data and network widths");
    d = definition();
    d.targets[0].data_width = bits;
    CHECK(message(d, "NCL_ENDPOINT_DATA_WIDTH", "targets[0].data_width") ==
          "Wishbone requires 32-bit data and network widths");
  }
  auto d = definition();
  d.initiators[0].address_width = 11;
  CHECK(message(d, "NCL_ENDPOINT_ADDRESS_WIDTH", "initiators[0].address_width").find("Wishbone") !=
        std::string::npos);
  d = definition();
  d.targets[0].max_outstanding = 2;
  CHECK(message(d, "NCL_AXI4_CAPACITY_COMBINATION", "targets[0].max_outstanding") ==
        "Dedicated APB, AHB and Wishbone targets keep one burst in flight");
  d = definition();
  d.initiators[0].id_table_entries = 1;
  CHECK(message(d, "NCL_AXI4_ID_CAPACITY", "initiators[0].id_table_entries").find("Wishbone") !=
        std::string::npos);
  d = definition();
  d.targets[0].exclusive = ExclusiveAccess::Target;
  CHECK(message(d, "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive").find("Wishbone") !=
        std::string::npos);
  for (const bool source : {false, true}) {
    std::string text = yaml;
    const auto at = source ? text.find("protocol: wishbone") : text.rfind("protocol: wishbone");
    text.replace(at, std::string("protocol: wishbone").size(), "protocol: unknown");
    const auto parsed = config::parse(text, "wb.yaml");
    REQUIRE_FALSE(parsed);
    CHECK(parsed.diagnostics().front().code == "NCL_UNSUPPORTED_PROTOCOL");
    CHECK(parsed.diagnostics().front().message.find("wishbone") != std::string::npos);
  }
}

TEST_CASE("Attribute diagnostics retain the APB message", "[wishbone]") {
  auto d = definition();
  d.targets[0].protocol = nocloom::Protocol::Apb3;
  d.targets[0].address_unit.reset();
  d.targets[0].attributes = nocloom::TargetAttributes{};
  const auto result = nocloom::generate(d);
  REQUIRE_FALSE(result);
  const auto at = std::ranges::find_if(result.diagnostics(), [](const auto &diagnostic) {
    return diagnostic.code == "NCL_TARGET_ATTRIBUTES";
  });
  REQUIRE(at != result.diagnostics().end());
  CHECK(at->message == "APB3 and APB2 targets have no PPROT to map");
}

TEST_CASE("Wishbone pipeline capacity and ports are explicit", "[wishbone]") {
  using namespace nocloom;
  auto d = definition();
  const auto classic = generate(d);
  REQUIRE(classic);
  d.initiators[0].cycles = WishboneCycles::Classic;
  d.targets[0].cycles = WishboneCycles::Classic;
  const auto explicit_classic = generate(d);
  REQUIRE(explicit_classic);
  REQUIRE(classic.value().size() == explicit_classic.value().size());
  for (std::size_t i = 0; i < classic.value().size(); ++i)
    CHECK(classic.value()[i].text == explicit_classic.value()[i].text);
  d.initiators[0].cycles = WishboneCycles::Pipelined;
  d.targets[0].cycles = WishboneCycles::Pipelined;
  for (unsigned capacity : {1U, 2U, 4U, 64U}) {
    d.initiators[0].max_outstanding = capacity;
    const auto result = generate(d);
    REQUIRE(result);
    for (const auto *port : {"nl_i_cpu_stall", "nl_t_mem_stall"})
      CHECK(std::ranges::any_of(result.value(), [&](const auto &file) {
        return file.path.ends_with(".v") && file.text.find(port) != std::string::npos;
      }));
  }
  for (const bool target : {false, true}) {
    auto invalid = definition();
    invalid.initiators[0].max_outstanding = 1;
    auto &cycles = target ? invalid.targets[0].cycles : invalid.initiators[0].cycles;
    cycles = static_cast<WishboneCycles>(9);
    const auto result = generate(invalid);
    REQUIRE_FALSE(result);
    const auto *field = target ? "targets[0].cycles" : "initiators[0].cycles";
    CHECK(std::ranges::any_of(result.diagnostics(), [&](const auto &x) {
      return x.code == "NCL_WISHBONE_CYCLES" && x.field == field;
    }));
  }
}

TEST_CASE("Pipelined Wishbone retains external protocol limits", "[wishbone]") {
  using namespace nocloom;
  auto d = definition();
  d.initiators[0].cycles = WishboneCycles::Pipelined;
  d.targets[0].cycles = WishboneCycles::Pipelined;
  for (const bool source : {false, true}) {
    auto invalid = d;
    if (source)
      invalid.initiators[0].data_width = 64;
    else
      invalid.targets[0].data_width = 64;
    const auto result = generate(invalid);
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(), [](const auto &x) {
      return x.code == "NCL_ENDPOINT_DATA_WIDTH" &&
             x.message == "Wishbone requires 32-bit data and network widths";
    }));
  }
  d.initiators[0].protocol = Protocol::Apb4;
  d.initiators[0].cycles.reset();
  d.initiators[0].address_unit.reset();
  for (const bool scheduled : {false, true}) {
    d.initiators[0].qos = {};
    if (scheduled) {
      d.initiators[0].qos.shaper = QosShaper{64, 64};
      d.initiators[0].qos.scheduler = QosScheduler{3, 0, 50};
    } else
      d.initiators[0].qos.levels = QosLevels{1, 1};
    const auto result = generate(d);
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(),
                              [](const auto &x) { return x.code == "NCL_QOS_PROFILE"; }));
  }
}
