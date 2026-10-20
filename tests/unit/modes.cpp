// Conditional mappings: the C++ definition equals the YAML one and generates the same bytes,
// the plan places each condition in its own transport plane above the global address space, the
// address table and the IP-XACT component carry the conditions, a source sampling an address input
// of another domain is rejected, and conditions IP-XACT remap states cannot express are rejected.
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include <algorithm>
#include <array>
#include <catch2/catch_test_macros.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <nocloom/ipxact.hpp>
#include <random>
#include <set>
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

unsigned enumerated_holes(const std::vector<detail::ApertureBlock> &blocks, unsigned selectors) {
  if (blocks.empty())
    return 0;
  std::set<std::uint32_t> seen;
  unsigned holes = 0;
  for (std::uint32_t flags = 0; flags < (std::uint32_t{1} << selectors); ++flags) {
    std::uint32_t visible = 0;
    for (std::size_t i = 0; i < blocks.size(); ++i)
      if ((flags & blocks[i].required) == blocks[i].required && !(flags & blocks[i].excluded))
        visible |= std::uint32_t{1} << i;
    if (!seen.insert(visible).second)
      continue;
    std::array<bool, 64> mapped{};
    for (std::size_t i = 0; i < blocks.size(); ++i)
      if (visible & (std::uint32_t{1} << i)) {
        const auto &w = blocks[i].window;
        for (auto address = w.base; address < w.base + w.size && address < mapped.size(); ++address)
          mapped[address] = true;
      }
    holes += std::ranges::find(mapped, false) != mapped.end();
  }
  return holes;
}
} // namespace

TEST_CASE("Distinct visible block sets match Boolean enumeration", "[modes][apertures]") {
  constexpr auto count_holes = detail::ApertureQuery::Count;
  std::mt19937 random(0x16aa);
  for (unsigned trial = 0; trial < 4096; ++trial) {
    const auto selectors = trial < 4076 ? trial % 12 : 12 + trial % 5;
    const auto mask = (std::uint32_t{1} << selectors) - 1;
    std::vector<detail::ApertureBlock> blocks;
    const auto count = trial % 25;
    for (unsigned i = 0; i < count; ++i) {
      const auto required = static_cast<std::uint32_t>(random()) & mask;
      const auto excluded = static_cast<std::uint32_t>(random()) & mask;
      const auto size = std::uint64_t{1} << (random() % 8);
      blocks.push_back({{(random() % 64) & ~(size - 1), size},
                        i % 7 == 0 ? 0 : required,
                        i % 7 == 0 ? 0 : (i % 7 == 1 ? excluded : excluded & ~required)});
    }
    // Use each selector in a chain, including the high bits in the largest trials.
    if (trial % 3 == 0) {
      blocks.clear();
      for (unsigned i = 1; i < selectors; ++i)
        blocks.push_back({{2 * i, 4}, std::uint32_t{1} << i, std::uint32_t{1} << (i - 1)});
    }
    CAPTURE(trial, selectors);
    CHECK(detail::error_apertures(blocks, 6, count_holes).count ==
          enumerated_holes(blocks, selectors));
  }

  std::vector<detail::ApertureBlock> independent;
  for (unsigned i = 0; i < 16; ++i)
    independent.push_back({{i, 1}, std::uint32_t{1} << i});
  CHECK(detail::error_apertures(independent, 6, count_holes).count ==
        enumerated_holes(independent, 16));
}

TEST_CASE("Conditional mappings from YAML and C++", "[modes]") {
  const auto d = parsed(yaml);
  REQUIRE(d == built());
  const auto a = generate(d), b = generate(built());
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(a.value() == b.value());
  // The conditions for strap values 0 and 1 take planes 1 and 2 above the global address.
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

TEST_CASE("Equal predicates use selector declaration order for planes", "[modes]") {
  auto d = built();
  d.address_selectors[1].equals = d.address_selectors[0].equals;
  d.targets[1].mappings[0].base = 0x20000;
  const auto &first = d.targets[0].mappings[0];
  const auto &second = d.targets[1].mappings[0];
  REQUIRE(detail::mapping_plane(d, first) == 1);
  REQUIRE(detail::mapping_plane(d, second) == 2);
  std::ranges::reverse(d.address_selectors);
  REQUIRE(detail::mapping_plane(d, first) == 2);
  REQUIRE(detail::mapping_plane(d, second) == 1);
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
  REQUIRE(refused.diagnostics().front().code == "NCL_ADDRESS_CONDITION");
  REQUIRE(refused.diagnostics().front().field == "targets[1].mappings[0].when");

  const GenerationOptions options{.address_table = true, .ipxact = true};
  REQUIRE(validate(d, options));
  const auto plan = make_plan(d);
  REQUIRE(plan);
  const auto emitted = emit(plan.value(), options);
  const auto with_ipxact = add_ipxact(d, set.value());
  REQUIRE(emitted);
  REQUIRE(with_ipxact);
  REQUIRE(emitted.value() == with_ipxact.value());

  const auto validated = validate(negated, options);
  const auto generated = generate(negated, options);
  REQUIRE_FALSE(validated);
  REQUIRE_FALSE(generated);
  for (const auto *diagnostics : {&validated.diagnostics(), &generated.diagnostics()}) {
    REQUIRE(diagnostics->size() == 1);
    REQUIRE(diagnostics->front().code == refused.diagnostics().front().code);
    REQUIRE(diagnostics->front().field == refused.diagnostics().front().field);
    REQUIRE(diagnostics->front().message == refused.diagnostics().front().message);
  }
}

TEST_CASE("Mode plane order preserves the decode miss path", "[modes]") {
  auto d = built();
  d.targets = {d.targets[1]};
  d.address_selectors = {{"earlier", {{"strap", 1}}}, {"later", {{"strap", 0}}}};
  auto &target = d.targets.front();
  target.flows = 2;
  target.mappings = {{0x1000, 0x1000, 0, {{"earlier", true}}, 0},
                     {0x1000, 0x1000, 0, {{"later", true}}, 1},
                     {0x3000, 0x1000, 0, {{"later", true}}, 0}};
  REQUIRE(detail::mapping_plane(d, target.mappings.front()) == 2);
  for (const auto named : {false, true}) {
    if (named)
      d.initiators.front().default_target = target.name;
    const auto plan = detail::build_network_plan(d);
    REQUIRE(plan);
    const auto miss = detail::miss_route(plan.value(), d.initiators.front().name);
    REQUIRE(miss);
    CHECK(miss->target_path == 0);
    CHECK(miss->window == AddressWindow{0x200001000, 0x1000});
  }
}

TEST_CASE("A named default orders miss windows by condition before base", "[modes]") {
  auto d = built();
  d.address_inputs = {{"a_mode", 1, "main"}, {"b_mode", 1, "main"}};
  d.targets.resize(1);
  auto &target = d.targets.front();
  target.address_width = 32;
  auto &source = d.initiators.front();
  source.protocol = Protocol::Apb4;
  source.id_width = 0;
  source.len_width = 0;
  source.default_target = target.name;

  for (const bool swap_names : {false, true}) {
    CAPTURE(swap_names);
    const auto *first = swap_names ? "z_sel" : "a_sel";
    const auto *second = swap_names ? "a_sel" : "z_sel";
    d.address_selectors = {{first, {{"a_mode", 1}}}, {second, {{"b_mode", 1}}}};
    target.mappings = {{0x1000, 0x1000, 0, {{first, true}}}, {0x8000, 0x2000, 0, {{second, true}}}};
    const auto plan = detail::build_network_plan(d);
    REQUIRE(plan);
    const auto miss = detail::miss_route(plan.value(), source.name);
    REQUIRE(miss);
    CHECK(miss->target_path == 0);
    const auto expected =
        swap_names ? AddressWindow{0x100008000, 0x2000} : AddressWindow{0x200001000, 0x1000};
    CHECK(miss->window == expected);
  }
}

TEST_CASE("Address conditions use the full 32-bit selector mask", "[modes]") {
  for (const unsigned count : {17U, 28U, 32U, 33U}) {
    CAPTURE(count);
    auto d = built();
    d.address_inputs.clear();
    d.address_selectors.clear();
    d.targets.resize(1);
    d.targets[0].address_width = 20;
    d.targets[0].mappings.clear();
    AddressMapping all{0x80000, 4096};
    for (unsigned i = 0; i < count; ++i) {
      const auto name = "sel" + std::to_string(100 + count - 1 - i);
      const auto input = "mode" + std::to_string(i);
      // Input declaration order determines the mask, even when names sort the other way.
      d.address_inputs.insert(d.address_inputs.begin(), {input, 1, "main"});
      d.address_selectors.push_back({name, {{input, 1}}});
      all.when.emplace(name, true);
    }
    const auto &first = d.address_selectors.front().name;
    const auto &last = d.address_selectors.back().name;
    d.targets[0].mappings.push_back(std::move(all));
    d.targets[0].mappings.push_back({0, 4096, 4096, {{first, true}, {last, true}}});
    d.targets[0].mappings.push_back({0, 4096, 8192, {{first, true}, {last, false}}});
    if (count == 33) {
      const auto invalid = generate(d);
      REQUIRE_FALSE(invalid);
      CHECK(invalid.diagnostics().front().code == "NCL_ADDRESS_SELECTOR");
      CHECK(invalid.diagnostics().front().field == "address_selectors");
      continue;
    }
    REQUIRE(generate(d));
    const auto plan = detail::build_network_plan(d);
    REQUIRE(plan);
    const auto profile = detail::initiator_profile(plan.value(), 0);
    CHECK(profile.address_selector_bits == count);
    CHECK(plan.value().route_index.target_ids.source_holes.at(d.initiators.front().name).count ==
          1);
    CHECK(plan.value().route_index.target_ids.source_holes.at(d.initiators.front().name).exact);
    const auto combined = (std::uint32_t{1} << (count - 1)) | 1U;
    for (const auto value : {1U, combined})
      CHECK(std::ranges::any_of(profile.mode_windows, [&](const auto &window) {
        return window.care == combined && window.value == value;
      }));
  }
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
