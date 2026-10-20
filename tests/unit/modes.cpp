// Mode-dependent mappings: the C++ definition equals the YAML one and generates the same bytes,
// the plan places each condition in its own transport plane above the global address space, the
// address table and the IP-XACT component carry the conditions, a source sampling an address input
// of another domain is rejected, and conditions IP-XACT remap states cannot express are rejected.
#include "core/network_plan.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <nocloom/ipxact.hpp>
#include <string>

using namespace nocloom;

namespace {
const char *const yaml = R"(schema_version: 10
name: boot
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
address_inputs: [{name: strap, width: 2, clock_domain: main}]
address_selectors: [{name: rom, equals: {strap: 1}}, {name: ram, equals: {strap: 0}}]
clock_domains:
  - {name: main, clock_port: clk, reset_port: reset_n, test_port: test}
initiators:
  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 4}
targets:
  - {name: flash, clock_domain: main, protocol: apb4, data_width: 32, address_width: 16,
     attributes: amba, mappings: [{base: '0x0', size: '0x1000', when: {rom: true}},
                                  {base: '0x10000', size: '0x1000'}]}
  - {name: sram, clock_domain: main, protocol: axi4, data_width: 32, address_width: 16,
     len_width: 4, attributes: amba, mappings: [{base: '0x0', size: '0x2000', when: {ram: true}}]}
connectivity: all
output: {backend: verilog, language: verilog_2001}
)";

Definition parsed(const std::string &text) {
  auto p = config::parse(text, "boot.yaml");
  REQUIRE(p);
  return p.value().definition;
}
Definition built() {
  Definition d;
  d.name = "boot";
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.address_inputs = {{"strap", 2, "main"}};
  d.address_selectors = {{"rom", {{"strap", 1}}}, {"ram", {{"strap", 0}}}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  Initiator cpu{"cpu", "main"};
  cpu.protocol = Protocol::Axi4;
  cpu.data_width = 32;
  cpu.id_width = 2;
  cpu.len_width = 4;
  d.initiators = {cpu};
  Target flash{"flash", "main", {{0x0, 0x1000, 0, {{"rom", true}}}, {0x10000, 0x1000}}, 16};
  flash.protocol = Protocol::Apb4;
  flash.data_width = 32;
  flash.attributes = TargetAttributes::amba();
  Target sram{"sram", "main", {{0x0, 0x2000, 0, {{"ram", true}}}}, 16};
  sram.protocol = Protocol::Axi4;
  sram.data_width = 32;
  sram.len_width = 4;
  sram.attributes = TargetAttributes::amba();
  d.targets = {flash, sram};
  d.connectivity = Connectivity{.all = true};
  return d;
}
const std::string &text(const ArtifactSet &set, std::string_view suffix) {
  const auto at =
      std::ranges::find_if(set, [&](const Artifact &a) { return a.path.ends_with(suffix); });
  REQUIRE(at != set.end());
  return at->text;
}
} // namespace

TEST_CASE("Mode-dependent mappings from YAML and C++", "[modes]") {
  const auto d = parsed(yaml);
  REQUIRE(d == built());
  const auto a = generate(d), b = generate(built());
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  // Conditions in name order take planes 1 and 2 above the 32 global address bits.
  REQUIRE(detail::mode_planes(d).size() == 2);
  REQUIRE(detail::mapping_plane(d, d.targets[1].mappings[0]) == 1);
  REQUIRE(detail::mapping_plane(d, d.targets[0].mappings[0]) == 2);
  REQUIRE(detail::mapping_plane(d, d.targets[0].mappings[1]) == 0);
  const auto layout = detail::layout_mappings(d);
  REQUIRE(layout.mappings[0][0].first == std::vector<AddressWindow>{{0x200000000, 0x1000}});
  REQUIRE(layout.mappings[1][0].first == std::vector<AddressWindow>{{0x100000000, 0x2000}});
  REQUIRE(layout.overlaps.empty());
  const auto &rtl = text(a.value(), ".v");
  REQUIRE(rtl.find("input wire [1:0] strap,") != std::string::npos);
}

TEST_CASE("Mode conditions in the address table and IP-XACT", "[modes]") {
  const auto d = parsed(yaml);
  auto set = add_address_table(generate(d).value());
  REQUIRE(set);
  const auto &csv = text(set.value(), ".address_table.csv");
  REQUIRE(
      csv ==
      "# SPDX-License-Identifier: LicenseRef-NocLoom-Output-1.0\n"
      "# NocLoom supplied content only. See NocLoom LICENSES/LicenseRef-NocLoom-Output-1.0.txt\n"
      "initiator,base,size,target,local_base,access,exclusive,default_target,when\n"
      "cpu,0x00000000,0x00002000,sram,0x00000000,read_write,none,false,ram\n"
      "cpu,0x00000000,0x00001000,flash,0x00000000,read_write,none,true,rom\n"
      "cpu,0x00010000,0x00001000,flash,0x00000000,read_write,none,true,\n");
  const auto component = add_ipxact(d, generate(d).value());
  REQUIRE(component);
  const auto &xml = text(component.value(), ".ipxact.xml");
  for (const auto *part :
       {"<spirit:name>rom</spirit:name>", "<spirit:remapPort spirit:portNameRef=\"strap\">0x1",
        "<spirit:remapPort spirit:portNameRef=\"strap\">0x0",
        "<spirit:memoryRemap spirit:state=\"ram\">", "<spirit:name>cpu_map_rom</spirit:name>"})
    REQUIRE(xml.find(part) != std::string::npos);
  // A false flag of a two-bit port is no single port value.
  auto negated = d;
  negated.targets[1].mappings[0].when = {{"rom", false}};
  negated.address_selectors.pop_back();
  const auto plain = generate(negated);
  REQUIRE(plain);
  const auto refused = add_ipxact(negated, plain.value());
  REQUIRE_FALSE(refused);
  REQUIRE(refused.diagnostics().front().code == "NCL_MODE_CONDITION");
  REQUIRE(refused.diagnostics().front().field == "targets[1].mappings[0].when");
}

TEST_CASE("A source samples its address inputs in its own domain", "[modes]") {
  // A clocked graph: the flash in a slow domain whose clock samples the address input.
  auto text = std::string(yaml);
  text.replace(text.find("clock_domain: main}]"), 20, "clock_domain: slow}]");
  text.replace(text.find("  - {name: main, clock_port: clk"), 0,
               "  - {name: slow, clock_port: clk_slow, reset_port: reset_n, test_port: test}\n");
  text.replace(text.find("  - {name: flash, clock_domain: main"), 36,
               "  - {name: flash, clock_domain: slow");
  text.replace(text.find("connectivity: all"), 0,
               "topology:\n"
               "  request: {nodes: [{name: q, clock_domain: main}], links: [],\n"
               "            entries: [{endpoint: cpu, node: q}],\n"
               "            exits: [{endpoint: flash, node: q}, {endpoint: sram, node: q}]}\n"
               "  response: mirror\n");
  const auto d = parsed(text);
  const auto r = make_plan(d);
  REQUIRE_FALSE(r);
  REQUIRE(std::ranges::any_of(r.diagnostics(), [](const Diagnostic &x) {
    return x.code == "NCL_ADDRESS_INPUT" && x.field == "targets[0].mappings[0].when.rom";
  }));
  // The same graph with the port in the source's domain plans.
  auto same = d;
  same.address_inputs[0].clock_domain = "main";
  REQUIRE(make_plan(same));
}
