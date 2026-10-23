#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <string>

namespace {
const char *const yaml = R"(schema_version: 10
name: axi3_source
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: axi3, data_width: 32, id_width: 3,
     user_flags: {owner: 0}}
  - {name: dma, clock_domain: main, protocol: axi3, data_width: 64, id_width: 2, len_width: 4,
     max_outstanding: 2, user_flags: {owner: 1}}
  - {name: dbg, clock_domain: main, protocol: axi4, data_width: 32, user_flags: {owner: 0}}
targets:
  - {name: ram, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, attributes: amba,
     exclusive: network, exclusive_id_flags: [owner], mappings: [{base: '0x0', size: '0x10000'}]}
  - {name: uart, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba,
     mappings: [{base: '0x10000', size: '0x1000'}]}
transport: {data_width: 32, user_flags: [owner]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";
} // namespace

TEST_CASE("AXI3 initiators are AXI4 sources behind AXI3 ports", "[axi4-config]") {
  using namespace nocloom;
  const auto parsed = config::parse(yaml, "axi3_source.yaml");
  REQUIRE(parsed);
  const auto base = parsed.value().definition;
  CHECK(base.initiators[0].protocol == Protocol::Axi3);
  const auto generated = generate(base);
  REQUIRE(generated);
  CHECK(generated.value() == generate(base).value());
  const auto artifact = [&](const std::string &suffix) {
    const auto at = std::ranges::find_if(
        generated.value(), [&](const auto &a) { return a.path == "nocloom_axi3_source" + suffix; });
    REQUIRE(at != generated.value().end());
    return at->text;
  };
  const auto rtl = artifact(".v"), map = artifact(".map.json");
  // Two-bit AxLOCK, four-bit AxLEN and WID of the AxID width; no AxQOS or AxREGION.
  for (const auto *port :
       {"[1:0] nl_i_cpu_awlock,", "[1:0] nl_i_cpu_arlock,", "[3:0] nl_i_cpu_awlen,",
        "[2:0] nl_i_cpu_wid,", "[1:0] nl_i_dma_wid,", "[63:0] nl_i_dma_wdata,"})
    CHECK(rtl.find(port) != std::string::npos);
  CHECK(rtl.find(" nl_i_dbg_awlock,") != std::string::npos);
  CHECK(rtl.find("nl_i_dbg_wid") == std::string::npos);
  CHECK(rtl.find("qos") == std::string::npos);
  CHECK(map.find(R"("cpu": "axi3")") != std::string::npos);
  CHECK(map.find(R"("dbg": "axi4")") != std::string::npos);
  CHECK(map.find(R"("axi3_ingress")") != std::string::npos);
  // A read-only AXI3 initiator has no W channel, WID included; an ID width of zero has no WID.
  auto one_way = base;
  one_way.initiators[0].access = Access::ReadOnly;
  one_way.initiators[1].id_width = 0;
  const auto narrowed = generate(one_way);
  REQUIRE(narrowed);
  const auto &text = std::ranges::find_if(narrowed.value(), [](const auto &a) {
                       return a.path == "nocloom_axi3_source.v";
                     })->text;
  CHECK(text.find("nl_i_cpu_wid") == std::string::npos);
  CHECK(text.find("nl_i_dma_wid") == std::string::npos);
  CHECK(text.find("nl_i_dma_awlock") != std::string::npos);
  const auto fails = [&](auto edit, const std::string &code, const std::string &field) {
    auto d = base;
    edit(d);
    const auto r = generate(d);
    REQUIRE_FALSE(r);
    CHECK(std::ranges::any_of(r.diagnostics(),
                              [&](const auto &x) { return x.code == code && x.field == field; }));
  };
  // AXI3 AxLEN has four bits.
  fails([](Definition &d) { d.initiators[0].len_width = 5; }, "NCL_ENDPOINT_TRANSACTION_WIDTH",
        "initiators[0].len_width");
  fails([](Definition &d) { d.initiators[0].id_width = 17; }, "NCL_ENDPOINT_TRANSACTION_WIDTH",
        "initiators[0].id_width");
  fails([](Definition &d) { d.initiators[0].data_width = 16; }, "NCL_ENDPOINT_DATA_WIDTH",
        "initiators[0].data_width");
  fails([](Definition &d) { d.initiators[0].address_width = 11; }, "NCL_ENDPOINT_ADDRESS_WIDTH",
        "initiators[0].address_width");
  // The AXI4 source options apply: port pipes, packet limits, narrow splitting, user ports.
  auto options = base;
  options.initiators[0].pipes.request = {.forward = true, .backward = true};
  options.initiators[0].narrow_bursts = NarrowBursts::SplitNonModifiable;
  options.initiators[0].user_width = 2;
  options.initiators[0].response_user_width = 1;
  options.transport->packet_limits = {{.source = "cpu", .target = "ram", .max_bytes = 16}};
  CHECK(generate(options));
  auto wide = base;
  wide.initiators[1].data_width = 1024;
  CHECK(generate(wide));
}

TEST_CASE("AXI3 targets are AXI4 targets behind AXI3 ports", "[axi4-config]") {
  using namespace nocloom;
  auto text = std::string(yaml);
  text.replace(text.find("  - {name: uart"), 0,
               "  - {name: sram, clock_domain: main, protocol: axi3, data_width: 64, id_width: 6,\n"
               "     attributes: amba, exclusive: target, exclusive_id_flags: [owner], mappings: "
               "[{base: '0x20000', size: "
               "'0x1000'}]}\n"
               "  - {name: rom, clock_domain: main, protocol: axi3, data_width: 32, access: "
               "read_only,\n"
               "     max_outstanding: 2, attributes: amba, mappings: [{base: '0x21000', size: "
               "'0x1000'}]}\n");
  const auto parsed = config::parse(text, "axi3_target.yaml");
  REQUIRE(parsed);
  const auto base = parsed.value().definition;
  CHECK(base.targets[1].protocol == Protocol::Axi3);
  const auto generated = generate(base);
  REQUIRE(generated);
  const auto file = [&](const std::string &path) {
    const auto at =
        std::ranges::find_if(generated.value(), [&](const auto &a) { return a.path == path; });
    REQUIRE(at != generated.value().end());
    return at->text;
  };
  const auto rtl = file("nocloom_axi3_source.v"), map = file("nocloom_axi3_source.map.json");
  for (const auto *port :
       {"[1:0] nl_t_sram_awlock,", "[1:0] nl_t_sram_arlock,", "[3:0] nl_t_sram_awlen,",
        "[5:0] nl_t_sram_wid,", "[5:0] nl_t_sram_awid,"})
    CHECK(rtl.find(port) != std::string::npos);
  CHECK(rtl.find("nl_t_rom_wid") == std::string::npos);
  CHECK(rtl.find("nl_t_rom_arlock") != std::string::npos);
  CHECK(map.find(R"("sram": "axi3")") != std::string::npos);
  CHECK(map.find(R"("ram": "axi4")") != std::string::npos);
  CHECK(map.find(R"("axi3_egress")") != std::string::npos);
  const auto fails = [&](auto edit, const std::string &code, const std::string &field) {
    auto d = base;
    edit(d);
    const auto r = generate(d);
    REQUIRE_FALSE(r);
    CHECK(std::ranges::any_of(r.diagnostics(),
                              [&](const auto &x) { return x.code == code && x.field == field; }));
  };
  // AXI3 AxLEN has four bits.
  fails([](Definition &d) { d.targets[1].len_width = 5; }, "NCL_ENDPOINT_TRANSACTION_WIDTH",
        "targets[1].len_width");
  fails([](Definition &d) { d.targets[1].id_width = 32; }, "NCL_ENDPOINT_TRANSACTION_WIDTH",
        "targets[1].id_width");
  fails([](Definition &d) { d.targets[1].fixed_bursts = FixedBursts::Single; },
        "NCL_TARGET_FIXED_BURSTS", "targets[1].fixed_bursts");
  // The AXI4 target options apply: port pipes, a timeout, user ports, per-target widths.
  auto options = base;
  options.targets[1].pipes.request = {.forward = true, .backward = true};
  options.targets[2].timeout = TargetTimeout{.cycles = 1024, .completion = true};
  options.targets[1].response_user_width = 2;
  options.targets[2].data_width = 128;
  CHECK(generate(options));
}
