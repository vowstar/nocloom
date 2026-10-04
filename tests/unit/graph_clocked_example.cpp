#include "../graph_clocked_definition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>

TEST_CASE("Clocked public graph validates ownership and domain depths", "[graph-clocked-example]") {
  using namespace nocloom;
  const auto base = graph_clocked_definition();
  REQUIRE(validate(base));
  REQUIRE(validate(graph_clocked_definition(true)));
  auto rejects = [](const auto &d) {
    REQUIRE_FALSE(validate(d));
    REQUIRE_FALSE(generate(d));
  };
  auto d = base;
  d.topology->request.nodes[0].clock_domain.reset();
  rejects(d);
  d = base;
  d.topology->response.nodes[0].clock_domain = "absent";
  rejects(d);
  d = base;
  d.initiators[0].clock_domain = "absent";
  rejects(d);
  d = base;
  d.targets[0].clock_domain = "absent";
  rejects(d);
  d = base;
  d.clock_domains.push_back({"unused", "clk_unused", "reset_n", "test"});
  rejects(d);
  d = base;
  d.clock_domains.push_back(d.clock_domains.front());
  rejects(d);
  d = base;
  d.clock_domains[0].name = "common";
  rejects(d);
  for (unsigned value : {0U, 1U, 5U}) {
    d = base;
    d.synchronizer_stages = value;
    rejects(d);
    d = base;
    std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[0].options).synchronizer_stages = value;
    rejects(d);
  }
  // Absent global stages default to 2; the base declares 2 explicitly.
  d = base;
  d.synchronizer_stages.reset();
  REQUIRE(validate(d));
  REQUIRE(generate(d).value() == generate(base).value());
  // A crossing entry sets a depth, stages or both.
  d = base;
  std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[0].options).synchronizer_stages.reset();
  rejects(d);
  // Global stages that no crossing takes are rejected as unused.
  d = base;
  d.clock_crossings.push_back({"middle", "main", std::nullopt, 3});
  REQUIRE(validate(d).diagnostics().front().code == "NCL_CDC_CROSSING_UNUSED");
  d.synchronizer_stages.reset();
  REQUIRE(validate(d));
  auto rejects_with = [](const auto &d, const std::string &code, const std::string &field) {
    const auto checked = validate(d);
    REQUIRE_FALSE(checked);
    REQUIRE(checked.diagnostics().front().code == code);
    REQUIRE(checked.diagnostics().front().field == field);
    REQUIRE_FALSE(generate(d));
  };
  for (std::uint64_t period : {99ULL, 1000000001ULL}) {
    d = base;
    d.clock_domains[1].period_ps = period;
    rejects_with(d, "NCL_CDC_PERIOD_RANGE", "clock_domains[1].period_ps");
  }
  for (unsigned depth : {0U, 1U, 11U}) {
    d = base;
    d.clock_crossings = {{"main", "middle", depth}};
    rejects_with(d, "NCL_CDC_DEPTH_RANGE", "clock_crossings[0].depth");
  }
  d = base;
  d.clock_crossings = {{"main", "middle", 4}, {"main", "middle", 5}};
  rejects_with(d, "NCL_CDC_CROSSING_DUPLICATE", "clock_crossings[1]");
  d = base;
  d.clock_crossings = {{"main", "absent", 4}};
  rejects_with(d, "NCL_DOMAIN_REFERENCE", "clock_crossings[0].to_domain");
  // Sources and targets never share a segment: main sends only to middle.
  d = base;
  d.clock_crossings = {{"main", "target", 4}};
  rejects_with(d, "NCL_CDC_CROSSING_UNUSED", "clock_crossings[0]");
  d = base;
  d.clock_crossings = {{"middle", "middle", 4}};
  rejects_with(d, "NCL_CDC_CROSSING_SAME_DOMAIN", "clock_crossings[0]");
  // target -> middle has no explicit depth, so middle's period alone is rejected.
  d = base;
  d.clock_domains[2].period_ps.reset();
  rejects_with(d, "NCL_CDC_PERIOD_PARTIAL", "clock_domains[1].period_ps");
  std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[2].options).depth = 5;
  REQUIRE(validate(d));
  // A pair override and periods change only the crossings they select.
  d = base;
  d.clock_crossings = {{"middle", "target", 3}};
  d.clock_domains[0].period_ps = 10000;
  d.clock_domains[1].period_ps = 14000;
  REQUIRE(validate(d));
  REQUIRE(generate(d));
  REQUIRE(generate(d).value() != generate(base).value());
  // The reset handshake needs two reset ports; with them it is on by default and a pair
  // entry may turn it off, alone or with other overrides.
  d = base;
  std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[1].options).reset_handshake = true;
  rejects_with(d, "NCL_RESET_PORT", "clock_crossings[1].reset_handshake");
  auto handshakes = [](const nocloom::Definition &definition) {
    const auto generated = generate(definition);
    REQUIRE(generated);
    const auto map = std::ranges::find_if(generated.value(), [](const auto &artifact) {
      return artifact.path.ends_with(".map.json");
    });
    const auto json = nlohmann::json::parse(map->text);
    std::map<std::string, bool> result;
    for (const auto &cut : json["network"]["cdc"]["cuts"]) {
      const bool on = cut.value("reset_handshake", false);
      REQUIRE(cut.contains("handshake") == on);
      result[cut["from_domain"].get<std::string>() + ">" + cut["to_domain"].get<std::string>()] =
          on;
    }
    return std::pair{result, json["definition"]["contract"]["reset"].get<std::string>()};
  };
  const auto [single, single_contract] = handshakes(base);
  REQUIRE(std::ranges::none_of(single, [](const auto &cut) { return cut.second; }));
  d = base;
  d.reset_ports.push_back({"reset_target_n"});
  d.clock_domains[2].reset_port = "reset_target_n";
  const auto [split, split_contract] = handshakes(d);
  REQUIRE(std::ranges::all_of(split, [](const auto &cut) { return cut.second; }));
  REQUIRE(split_contract != single_contract);
  d.clock_crossings.push_back({"middle", "main"});
  rejects_with(d, "NCL_SCHEMA_MISSING_FIELD", "clock_crossings[3]");
  d.clock_crossings.back() = {"target", "main", std::nullopt, std::nullopt, false};
  rejects_with(d, "NCL_CDC_CROSSING_UNUSED", "clock_crossings[3]");
  d.clock_crossings.pop_back();
  std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[1].options).reset_handshake = false;
  const auto [mixed, mixed_contract] = handshakes(d);
  for (const auto &[pair, on] : mixed)
    REQUIRE(on == (pair != "middle>target"));
  // At most 64 domains: 65 are rejected by count; 64 only because most stay unused.
  d = graph_clocked_definition(true);
  while (d.clock_domains.size() < 65)
    d.clock_domains.push_back({"extra" + std::to_string(d.clock_domains.size()),
                               "clk_extra" + std::to_string(d.clock_domains.size()), "reset_n",
                               "test"});
  rejects_with(d, "NCL_UNSUPPORTED_TOPOLOGY", "clock_domains");
  d.clock_domains.pop_back();
  const auto at_limit = validate(d);
  REQUIRE_FALSE(at_limit);
  REQUIRE(std::ranges::none_of(at_limit.diagnostics(),
                               [](const auto &x) { return x.code == "NCL_UNSUPPORTED_TOPOLOGY"; }));
  d = base;
  d.topology.reset();
  d.initiators.resize(2);
  d.targets.resize(2);
  rejects(d);
}

TEST_CASE("Clocked graph normalization preserves scoped ownership and determinism",
          "[graph-clocked-example]") {
  using namespace nocloom;
  auto d = graph_clocked_definition(true);
  // Occupy an internal-looking name; public switch names overlap by direction.
  d.initiators[0].name = "req_node0";
  d.topology->request.entries[0].endpoint = "req_node0";
  d.topology->response.exits[0].endpoint = "req_node0";
  for (auto &route : d.connectivity->pairs)
    if (route.source == "s0")
      route.source = "req_node0";
  auto first = generate(d);
  REQUIRE(first);
  std::ranges::reverse(d.clock_domains);
  std::ranges::reverse(d.initiators);
  std::ranges::reverse(d.targets);
  for (auto *g : {&d.topology->request, &d.topology->response}) {
    std::ranges::reverse(g->nodes);
    std::ranges::reverse(g->links);
    std::ranges::reverse(g->entries);
    std::ranges::reverse(g->exits);
  }
  std::ranges::reverse(d.connectivity->pairs);
  auto second = generate(d);
  REQUIRE(second);
  REQUIRE(first.value() == second.value());
}

TEST_CASE("Explicit same-domain placement is distinct from the implicit main domain",
          "[graph-clocked-example]") {
  using namespace nocloom;
  auto d = graph_clocked_definition();
  d.clock_domains.resize(1);
  d.clock_domains[0].period_ps.reset();
  d.clock_crossings.clear();
  d.synchronizer_stages.reset();
  for (auto &target : d.targets)
    target.clock_domain = "main";
  for (auto *g : {&d.topology->request, &d.topology->response})
    for (auto &node : g->nodes)
      node.clock_domain = "main";
  REQUIRE(validate(d));
  auto explicit_clock = generate(d);
  REQUIRE(explicit_clock);
  // Stages without a crossing are rejected, clocked or not.
  auto stages = d;
  stages.synchronizer_stages = 2;
  REQUIRE(validate(stages).diagnostics().front().code == "NCL_CDC_CROSSING_UNUSED");
  for (auto *g : {&d.topology->request, &d.topology->response})
    for (auto &node : g->nodes)
      node.clock_domain.reset();
  auto implicit_main = generate(d);
  REQUIRE(implicit_main);
  REQUIRE(explicit_clock.value() != implicit_main.value());
  // Crossing parameters need an explicitly clocked graph with a crossing.
  auto period = d;
  period.clock_domains[0].period_ps = 10000;
  REQUIRE(validate(period).diagnostics().front().code == "NCL_CDC_PERIOD_UNUSED");
  auto crossing = d;
  crossing.clock_crossings = {{"main", "main", 4}};
  REQUIRE(validate(crossing).diagnostics().front().code == "NCL_CDC_CROSSING_UNUSED");
  stages = d;
  stages.synchronizer_stages = 2;
  REQUIRE(validate(stages).diagnostics().front().code == "NCL_SCHEMA_FIELD_VERSION");
}

TEST_CASE("YAML clock periods and pair depths reach the crossing FIFOs",
          "[graph-clocked-example]") {
  const std::string head = R"(schema_version: 10
name: yclk
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk_main, reset_port: reset_n, test_port: test, period_ps: 10000}
  - {name: far, clock_port: clk_far, reset_port: reset_n, test_port: test, period_ps: 30000}
synchronizer_stages: 3
)";
  const std::string tail = R"(initiators:
  - {name: s0, clock_domain: main, protocol: axi4, data_width: 32}
targets:
  - {name: t0, clock_domain: far, data_width: 32, protocol: axi4, mappings: [{base: '0x0', size: '0x10000'}], len_width: 8, attributes: amba}
transport: {max_outstanding: 1}
connectivity: all
topology:
  request:
    nodes: [{name: n, clock_domain: main}]
    links: []
    entries: [{endpoint: s0, node: n}]
    exits: [{endpoint: t0, node: n}]
  response:
    nodes: [{name: r, clock_domain: far}]
    links: []
    entries: [{endpoint: t0, node: r}]
    exits: [{endpoint: s0, node: r}]
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = nocloom::config::parse(
      head + "clock_crossings:\n  - {from_domain: far, to_domain: main, depth: 3}\n" + tail);
  for (const auto &e : parsed.diagnostics())
    UNSCOPED_INFO(e.code + " " + e.field + ": " + e.message);
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  REQUIRE(d.clock_domains[0].period_ps == 10000);
  REQUIRE(d.synchronizer_stages == 3U);
  REQUIRE(d.clock_crossings.size() == 1);
  REQUIRE(std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[0].options).depth == 3U);
  REQUIRE_FALSE(
      std::get<nocloom::AsyncClockCrossing>(d.clock_crossings[0].options).synchronizer_stages);
  const auto generated = nocloom::generate(d);
  for (const auto &e : generated.diagnostics())
    UNSCOPED_INFO(e.code + " " + e.field + ": " + e.message);
  REQUIRE(generated);
  const auto map = std::ranges::find(generated.value(), std::string("nocloom_yclk.map.json"),
                                     &nocloom::Artifact::path);
  REQUIRE(map != generated.value().end());
  const auto json = nlohmann::json::parse(map->text);
  std::map<std::string, std::pair<unsigned, std::string>> depths;
  for (const auto &cut : json["network"]["cdc"]["cuts"]) {
    depths[cut["from_domain"].get<std::string>() + ">" + cut["to_domain"].get<std::string>()] = {
        cut["depth"].get<unsigned>(), cut["depth_source"].get<std::string>()};
    REQUIRE(cut["obligations"].size() == 3);
    REQUIRE(cut["fields"][0]["storage"].size() == cut["depth"].get<unsigned>());
  }
  // main (10 ns) to far (30 ns), n = 3: ceil(4 * (10 + 30) / 30) = 6.
  REQUIRE(depths.at("main>far") == std::pair{6U, std::string("formula")});
  REQUIRE(depths.at("far>main") == std::pair{3U, std::string("explicit")});
  for (const auto &cut : json["network"]["cdc"]["cuts"])
    REQUIRE(cut["synchronizer_stages"] == 3);
  // Domains are listed by name: far, then main; stages are global.
  const auto &domains = json["definition"]["clock_domains"];
  REQUIRE(json["definition"]["synchronizer_stages"] == 3);
  REQUIRE_FALSE(domains[0].contains("synchronizer_stages"));
  REQUIRE(domains[1]["name"] == "main");
  REQUIRE(domains[1]["period_ps"] == 10000);
  REQUIRE(json["definition"]["clock_crossings"][0]["depth"] == 3);
  const auto off = nocloom::config::parse(
      head + "clock_crossings:\n  - {from_domain: far, to_domain: main, reset_handshake: false}\n" +
      tail);
  REQUIRE(off);
  REQUIRE(std::get<nocloom::AsyncClockCrossing>(off.value().definition.clock_crossings[0].options)
              .reset_handshake == false);
  const std::vector<std::pair<std::string, std::string>> bad{
      {"{to_domain: main, depth: 3}", "NCL_SCHEMA_MISSING_FIELD"},
      {"{from_domain: far, to_domain: main, synchronizer_stages: '3'}", "NCL_SCHEMA_TYPE"},
      {"{from_domain: far, to_domain: main, depth: 3, x: 1}", "NCL_SCHEMA_UNKNOWN_FIELD"},
      {"{from_domain: far, to_domain: main, depth: '3'}", "NCL_SCHEMA_TYPE"},
      {"{from_domain: far, to_domain: main, reset_handshake: 1}", "NCL_SCHEMA_TYPE"}};
  for (const auto &[entry, code] : bad) {
    const auto rejected =
        nocloom::config::parse(head + "clock_crossings:\n  - " + entry + "\n" + tail);
    REQUIRE_FALSE(rejected);
    REQUIRE(rejected.diagnostics().front().code == code);
  }
}

TEST_CASE("CDC constraints are an explicit, deterministic addition", "[graph-clocked-example]") {
  using namespace nocloom;
  using nlohmann::json;
  auto d = graph_clocked_definition();
  d.reset_ports.push_back({"reset_target_n"});
  d.clock_domains[2].reset_port = "reset_target_n";
  const auto off = generate(d);
  REQUIRE(off);
  REQUIRE(generate(d, GenerationOptions{}).value() == off.value());
  GenerationOptions on_options;
  on_options.cdc_constraints = true;
  const auto on = generate(d, on_options);
  REQUIRE(on);
  REQUIRE(generate(d, on_options).value() == on.value());
  const auto text = [](const ArtifactSet &set, const std::string &suffix) {
    const auto it =
        std::ranges::find_if(set, [&](const auto &a) { return a.path.ends_with(suffix); });
    REQUIRE(it != set.end());
    return it->text;
  };
  // Every artifact but the bundle is unchanged; the bundle only lists the two new files.
  REQUIRE(on.value().size() == off.value().size() + 2);
  for (const auto &a : off.value())
    if (!a.path.ends_with(".bundle.json"))
      REQUIRE(std::ranges::find(on.value(), a) != on.value().end());
  auto bundle = json::parse(text(on.value(), ".bundle.json"));
  const auto files = bundle["files"];
  REQUIRE(files[files.size() - 2] ==
          json{{"path", "nocloom_graph.cdc.sdc"}, {"role", "cdc_constraints"}});
  REQUIRE(files.back() ==
          json{{"path", "nocloom_graph.cdc_preserve.txt"}, {"role", "cdc_preserve_list"}});
  bundle["files"].erase(bundle["files"].size() - 1);
  bundle["files"].erase(bundle["files"].size() - 1);
  REQUIRE(bundle.dump(2) + "\n" == text(off.value(), ".bundle.json"));

  const auto sdc = text(on.value(), ".cdc.sdc"), keep = text(on.value(), ".cdc_preserve.txt");
  const auto rtl = text(on.value(), "nocloom_graph.v");
  const auto map = json::parse(text(on.value(), ".map.json"));
  const auto count = [](const std::string &s, const std::string &what) {
    std::size_t n = 0;
    for (auto at = s.find(what); at != std::string::npos; at = s.find(what, at + 1))
      ++n;
    return n;
  };
  // Only the bundle records the generator version; every other file names the generator alone.
  for (const auto &a : on.value()) {
    if (a.path.ends_with(".v")) {
      REQUIRE(a.text.starts_with("// Generated by NocLoom\n// SPDX-License-Identifier: "));
    } else if (a.path.ends_with(".json")) {
      const auto record = json::parse(a.text);
      REQUIRE(record["generator"] == "NocLoom");
      REQUIRE(record.contains("version") == a.path.ends_with(".bundle.json"));
    } else {
      REQUIRE(count(a.text, "\n# Generated by NocLoom\n") == 1);
    }
  }
  REQUIRE(bundle["version"] == version());
  // Per cut: two Gray counts, the slots and, with the reset handshake, four handshake bits
  // whose bound is the period of the driving side.
  std::size_t expected = 0, handshakes = 0;
  std::map<std::string, std::size_t> driven_by;
  for (const auto &cut : map["network"]["cdc"]["cuts"]) {
    expected += 3;
    if (!cut.value("reset_handshake", false))
      continue;
    expected += 4;
    ++handshakes;
    driven_by[cut["from_domain"]] += 2;
    driven_by[cut["to_domain"]] += 2;
  }
  REQUIRE(handshakes > 0);
  REQUIRE(count(sdc, "\nset_max_delay ") == expected);
  REQUIRE(count(sdc, "\nset_min_delay 0 -ignore_clock_latency -from [get_cells {") == expected);
  REQUIRE(count(sdc, "\nset_clock_groups -asynchronous -allow_paths -group [get_clocks {main}] "
                     "-group [get_clocks {middle}] -group [get_clocks {target}]\n") == 1);
  for (const auto *line :
       {"\nset sdc_version 2.1\n", "\nset nocloom_period_main 10.000\n",
        "\nset nocloom_period_middle 14.000\n", "\nset nocloom_period_target 22.000\n"})
    REQUIRE(count(sdc, line) == 1);
  // middle and target exchange six cuts; slots wait 3 target cycles (3 stages into target) and
  // 4 middle cycles (4 stages from main).
  REQUIRE(count(sdc, "set_max_delay [expr {min($nocloom_period_middle, $nocloom_period_target)}]"
                     " -ignore_clock_latency -from") == 6);
  REQUIRE(count(sdc, "set_max_delay [expr {min($nocloom_period_target, $nocloom_period_middle)}]"
                     " -ignore_clock_latency -from") == 6);
  REQUIRE(count(sdc, "set_max_delay [expr {3 * $nocloom_period_target}] ") == 3);
  REQUIRE(count(sdc, "set_max_delay [expr {4 * $nocloom_period_middle}] ") == 3);
  for (const auto &[domain, n] : driven_by)
    REQUIRE(count(sdc, "set_max_delay $nocloom_period_" + domain + " -ignore_clock_latency") == n);
  // The driver is the other side: a handshake bit into middle from target waits T_target.
  std::size_t into_middle = 0;
  for (std::size_t at = 0, end; (end = sdc.find('\n', at)) != std::string::npos; at = end + 1) {
    const auto line = sdc.substr(at, end - at);
    into_middle += line.starts_with("set_max_delay $nocloom_period_target ") &&
                   line.ends_with("-to [get_clocks {middle}]");
  }
  REQUIRE(into_middle == 12);
  for (const auto *forbidden : {"get_attribute", "dont_touch", "size_only", "datapath_only",
                                "set_bus_skew", "set_units", "/home", "/tmp"})
    REQUIRE(count(sdc + keep, forbidden) == 0);
  // Every named register and synchronizer instance exists in the RTL.
  std::size_t names = 0;
  for (const auto &s : {sdc, keep})
    for (auto at = s.find("nl_internal_v"); at != std::string::npos;
         at = s.find("nl_internal_v", at + 1), ++names) {
      const auto end = s.find_first_of("*/ \n", at);
      const auto name = s.substr(at, end - at);
      if (s.compare(end, 1, "*") == 0) {
        REQUIRE(name.ends_with("_reg"));
        const auto reg = name.substr(0, name.size() - 4);
        REQUIRE(count(rtl, "] " + reg + ";\n") + count(rtl, "reg " + reg + ";\n") == 1);
        continue;
      }
      REQUIRE(count(rtl, ") " + name + " (") == 1);
      // A handshake source is the last stage of its synchronizer.
      if (s.compare(end, 11, "/stage_reg[") == 0) {
        const auto stage = s.substr(end + 11, s.find(']', end) - end - 11);
        const auto stages = std::stoul(stage) + 1;
        REQUIRE(count(rtl, ".STAGES(" + std::to_string(stages) + ")) " + name + " (") == 1);
      }
    }
  REQUIRE(names > 100);
  REQUIRE(count(sdc, "_sync/stage_reg[") == handshakes * 2 * 2);
  REQUIRE(count(keep, "\ninstance ") > 0);
  REQUIRE(count(keep, "\nregister ") == 12 * 2 + handshakes * 2);

  // A domain without a period becomes a variable the reader sets; bound cells change the names.
  const auto twelve = generate(graph_clocked_definition(true), on_options);
  REQUIRE(twelve);
  const auto twelve_sdc = text(twelve.value(), ".cdc.sdc");
  REQUIRE(count(twelve_sdc, "# main has no period_ps: set nocloom_period_main (ns) before") == 1);
  REQUIRE(count(twelve_sdc, "\nset nocloom_period_") == 0);
  d.cells.synchronizer = SynchronizerBinding{"lib_dff", 1, "CK", "D", "RN", true, "Q"};
  const auto chained = text(generate(d, on_options).value(), ".cdc.sdc");
  REQUIRE(count(chained, ".u_cell}") == handshakes * 2 * 2);
  REQUIRE(count(chained, "/stage_reg[") == 0);
  // An integrated synchronizer cell has one stage count: every chain takes two stages.
  d.cells.synchronizer = SynchronizerBinding{"lib_sync2", 2, "CK", "D", "RN", true, "Q"};
  for (auto &crossing : d.clock_crossings)
    std::get<nocloom::AsyncClockCrossing>(crossing.options).synchronizer_stages = 2;
  const auto integrated_set = generate(d, on_options);
  if (!integrated_set)
    FAIL(integrated_set.diagnostics().front().code + " " +
         integrated_set.diagnostics().front().message);
  const auto integrated = text(integrated_set.value(), ".cdc.sdc");
  REQUIRE(count(integrated, "_sync/u_cell}") == handshakes * 2 * 2);

  // Without a crossing there is nothing to constrain: rejected, not an empty file. That holds
  // for a single-domain network and for a clocked graph placed in one domain.
  auto one_domain = graph_clocked_definition();
  one_domain.clock_domains.resize(1);
  one_domain.clock_domains[0].period_ps.reset();
  one_domain.clock_crossings.clear();
  one_domain.synchronizer_stages.reset();
  for (auto &target : one_domain.targets)
    target.clock_domain = "main";
  for (auto *g : {&one_domain.topology->request, &one_domain.topology->response})
    for (auto &node : g->nodes)
      node.clock_domain = "main";
  REQUIRE(generate(one_domain));
  for (const auto &definition : {graph_example_definition(true, true), one_domain}) {
    const auto single = generate(definition, on_options);
    REQUIRE_FALSE(single);
    REQUIRE(single.diagnostics().front().code == "NCL_UNSUPPORTED_BACKEND");
    REQUIRE(single.diagnostics().front().field == "output.cdc_constraints");
  }
}
