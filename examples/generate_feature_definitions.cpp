#include <algorithm>
#include <array>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
#include <string_view>
#include <utility>

namespace {
nocloom::Definition base_definition(std::string name) {
  nocloom::Definition definition;
  definition.schema_version = 10;
  definition.name = std::move(name);
  definition.address_width = 32;
  definition.reset_ports = {{"reset_n"}};
  definition.test_ports = {{"test"}};
  definition.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  return definition;
}

nocloom::Initiator source(std::string name, nocloom::Protocol protocol, unsigned width) {
  nocloom::Initiator result;
  result.name = std::move(name);
  result.clock_domain = "main";
  result.protocol = protocol;
  result.data_width = width;
  return result;
}

nocloom::Target target(std::string name, nocloom::Protocol protocol, unsigned width,
                       std::vector<nocloom::AddressMapping> mappings) {
  nocloom::Target result;
  result.name = std::move(name);
  result.clock_domain = "main";
  result.mappings = std::move(mappings);
  result.protocol = protocol;
  result.data_width = width;
  result.attributes = nocloom::TargetAttributes::amba();
  return result;
}

nocloom::Definition mode_remap() {
  auto definition = base_definition("mode_remap");
  definition.address_inputs = {{"boot", 1, "main"}, {"bank", 2, "main"}};
  definition.address_selectors = {{"rom", {{"boot", 1}}}, {"high", {{"bank", 2}}}};

  auto a0 = source("a0", nocloom::Protocol::Axi4, 32);
  a0.id_width = 2;
  a0.len_width = 8;
  auto m0 = source("m0", nocloom::Protocol::Axi4, 32);
  m0.id_width = 2;
  m0.len_width = 8;
  m0.max_outstanding = 4;
  definition.initiators = {a0, m0, source("l0", nocloom::Protocol::Axi4Lite, 32),
                           source("p0", nocloom::Protocol::Apb4, 32)};

  auto ram = target(
      "ram", nocloom::Protocol::Axi4, 32,
      {{.base = 0, .size = 0x2000, .when = {{"rom", false}}}, {.base = 0x10000, .size = 0x2000}});
  ram.address_width = 16;
  ram.len_width = 8;
  auto rom = target(
      "rom", nocloom::Protocol::Apb4, 32,
      {{.base = 0, .size = 0x1000, .when = {{"rom", true}}}, {.base = 0x20000, .size = 0x1000}});
  rom.address_width = 16;
  auto dev = target("dev", nocloom::Protocol::Axi4Lite, 32,
                    {{.base = 0x4000, .size = 0x1000, .when = {{"rom", true}, {"high", true}}},
                     {.base = 0x30000, .size = 0x1000}});
  dev.address_width = 16;
  definition.targets = {ram, rom, dev};
  definition.connectivity = nocloom::Connectivity{.all = true};
  return definition;
}

nocloom::Definition explicit_routes_conn_flows() {
  auto definition = base_definition("explicit_routes_conn_flows");
  auto i0 = source("i0", nocloom::Protocol::Axi4, 32);
  i0.id_width = 2;
  i0.len_width = 8;
  i0.route_classes = {"even", "odd"};
  i0.default_class = "odd";
  i0.id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  auto i1 = source("i1", nocloom::Protocol::Axi4, 32);
  i1.id_width = 2;
  i1.len_width = 8;
  definition.initiators = {i0, i1};

  auto t0 = target("t0", nocloom::Protocol::Apb4, 32,
                   {{.base = 0, .size = 0x1000}, {.base = 0x1000, .size = 0x1000, .flow = 1}});
  t0.address_width = 16;
  t0.flows = 2;
  auto t1 = target("t1", nocloom::Protocol::Axi4, 32,
                   {{.base = 0x2000, .size = 0x1000}, {.base = 0x3000, .size = 0x1000, .flow = 1}});
  t1.id_width = 3;
  t1.len_width = 8;
  t1.flows = 2;
  definition.targets = {t0, t1};

  definition.connectivity = nocloom::Connectivity{
      .pairs = {{.source = "i0", .target = "t0", .source_path = 0, .target_path = 0},
                {.source = "i0", .target = "t0", .source_path = 1},
                {.source = "i0", .target = "t1", .source_path = 0},
                {.source = "i0", .target = "t1", .source_path = 1, .target_path = 1},
                {.source = "i1", .target = "t0", .target_path = 1},
                {.source = "i1", .target = "t1"}}};
  definition.topology = nocloom::ExchangeTopology{};
  auto &request = definition.topology->request;
  request.nodes = {{{"qa"}}, {{"qb"}}, {{"qc"}}, {{"qd"}}};
  request.links = {{"qa", "qb"}, {"qa", "qc"}, {"qc", "qb"}, {"qb", "qd"}, {"qc", "qd"}};
  request.entries = {{"i0", "qa"}, {"i1", "qa"}};
  request.exits = {{"t0", "qd"}, {"t1", "qd"}};
  auto &response = definition.topology->response;
  response.nodes = {{{"ra"}}, {{"rb"}}, {{"rc"}}, {{"rd"}}};
  response.links = {{"ra", "rb"}, {"ra", "rc"}, {"rb", "rd"}, {"rc", "rd"}, {"rc", "rb"}};
  response.entries = {{"t0", "ra"}, {"t1", "ra"}};
  response.exits = {{"i0", "rd"}, {"i1", "rd"}};
  definition.topology->routes = {{.initiator = "i0",
                                  .target = "t0",
                                  .request = {"qa", "qb", "qd"},
                                  .response = {"ra", "rb", "rd"}},
                                 {.initiator = "i0",
                                  .target = "t0",
                                  .request = {"qa", "qc", "qd"},
                                  .response = {"ra", "rc", "rd"},
                                  .source_path = 1},
                                 {.initiator = "i0",
                                  .target = "t0",
                                  .request = {"qa", "qc", "qd"},
                                  .response = {"ra", "rc", "rd"},
                                  .source_path = 1,
                                  .target_path = 1},
                                 {.initiator = "i0",
                                  .target = "t1",
                                  .request = {"qa", "qc", "qb", "qd"},
                                  .response = {"ra", "rc", "rb", "rd"}},
                                 {.initiator = "i0",
                                  .target = "t1",
                                  .request = {"qa", "qb", "qd"},
                                  .response = {"ra", "rb", "rd"},
                                  .target_path = 1},
                                 {.initiator = "i0",
                                  .target = "t1",
                                  .request = {"qa", "qc", "qd"},
                                  .response = {"ra", "rc", "rd"},
                                  .source_path = 1,
                                  .target_path = 1},
                                 {.initiator = "i1",
                                  .target = "t0",
                                  .request = {"qa", "qc", "qb", "qd"},
                                  .response = {"ra", "rb", "rd"},
                                  .target_path = 1},
                                 {.initiator = "i1",
                                  .target = "t1",
                                  .request = {"qa", "qc", "qd"},
                                  .response = {"ra", "rb", "rd"}},
                                 {.initiator = "i1",
                                  .target = "t1",
                                  .request = {"qa", "qb", "qd"},
                                  .response = {"ra", "rc", "rb", "rd"},
                                  .target_path = 1}};
  return definition;
}

nocloom::Definition axi4_qos_profiles() {
  auto definition = base_definition("axi4_qos_profiles");
  definition.arbitration = nocloom::ArbitrationPolicy::LevelRoundRobin;
  auto s0 = source("s0", nocloom::Protocol::Axi4, 32);
  s0.id_width = 2;
  s0.len_width = 8;
  s0.max_outstanding = 4;
  s0.qos.levels = {.read = 1, .write = 1};
  s0.qos.shaper = nocloom::QosShaper{.bytes_per_1024_cycles = 64, .burst_bytes = 64};
  auto s1 = s0;
  s1.name = "s1";
  s1.qos = {};
  s1.qos.shaper = nocloom::QosShaper{.bytes_per_1024_cycles = 260, .burst_bytes = 64};
  s1.qos.scheduler =
      nocloom::QosScheduler{.under_level = 3, .over_level = 0, .threshold_percent = 50};
  auto s2 = s0;
  s2.name = "s2";
  s2.qos = {};
  s2.qos.levels = {.read = 2, .write = 2};
  definition.initiators = {s0, s1, s2};

  auto t0 = target("t0", nocloom::Protocol::Axi4, 32, {{.base = 0, .size = 0x10000}});
  t0.len_width = 8;
  t0.max_outstanding = 4;
  t0.pipes.arrival.forward = true;
  t0.pipes.response.forward = true;
  definition.targets = {t0};
  definition.connectivity = nocloom::Connectivity{.all = true};
  definition.topology = nocloom::ExchangeTopology{};
  auto &request = definition.topology->request;
  request.nodes = {{{"q0"}}, {{"q1"}}};
  request.links = {{.from = "q0", .to = "q1", .pipeline = {.forward = true, .backward = true}}};
  request.entries = {{"s0", "q0"}, {"s1", "q0"}, {"s2", "q0"}};
  request.exits = {{"t0", "q1"}};
  auto &response = definition.topology->response;
  response.nodes = {{{"p0"}}, {{"p1"}}};
  response.links = {{.from = "p0", .to = "p1", .pipeline = {.forward = true, .backward = true}}};
  response.entries = {{"t0", "p0"}};
  response.exits = {{"s0", "p1"}, {"s1", "p1"}, {"s2", "p1"}};
  return definition;
}

nocloom::Definition axi4_axqos_map() {
  auto definition = base_definition("axi4_axqos_map");
  definition.arbitration = nocloom::ArbitrationPolicy::LevelRoundRobin;
  auto s0 = source("s0", nocloom::Protocol::Axi4, 32);
  s0.id_width = 2;
  s0.len_width = 8;
  s0.max_outstanding = 4;
  s0.qos.axqos_map = {0, 0, 1, 1, 2, 2, 3, 3};
  auto s1 = s0;
  s1.name = "s1";
  s1.qos = {};
  s1.qos.levels = {.read = 1, .write = 1};
  auto s2 = s0;
  s2.name = "s2";
  s2.qos = {};
  s2.qos.levels = {.read = 2, .write = 2};
  definition.initiators = {s0, s1, s2};
  auto t0 = target("t0", nocloom::Protocol::Axi4, 32, {{.base = 0, .size = 0x10000}});
  t0.len_width = 8;
  t0.max_outstanding = 4;
  definition.targets = {t0};
  definition.connectivity = nocloom::Connectivity{.all = true};
  return definition;
}

nocloom::Definition axi4_id_table() {
  auto definition = base_definition("axi4_id_table");
  auto s0 = source("s0", nocloom::Protocol::Axi4, 32);
  s0.id_width = 2;
  s0.len_width = 8;
  s0.max_outstanding = 4;
  s0.id_table_entries = 2;
  definition.initiators = {s0};
  auto t0 = target("t0", nocloom::Protocol::Axi4, 32, {{.base = 0, .size = 0x10000}});
  t0.len_width = 8;
  t0.max_outstanding = 4;
  definition.targets = {t0};
  definition.transport = nocloom::TransportOptions{};
  definition.transport->max_outstanding = 4;
  definition.transport->transport_word_bytes = 4;
  definition.connectivity = nocloom::Connectivity{.all = true};
  return definition;
}

nocloom::Definition axi4_wrap_alignment() {
  auto definition = base_definition("axi4_wrap_alignment");
  auto s0 = source("s0", nocloom::Protocol::Axi4, 64);
  s0.id_width = 2;
  s0.len_width = 8;
  s0.max_outstanding = 4;
  definition.initiators = {s0};
  auto t0 = target("t0", nocloom::Protocol::Axi4, 64, {{.base = 0, .size = 0x10000}});
  t0.len_width = 8;
  t0.max_outstanding = 4;
  definition.targets = {t0};
  definition.transport = nocloom::TransportOptions{};
  definition.transport->max_outstanding = 4;
  definition.transport->transport_word_bytes = 8;
  definition.connectivity = nocloom::Connectivity{.all = true};
  return definition;
}

using Factory = nocloom::Definition (*)();
constexpr std::array<std::pair<std::string_view, Factory>, 6> factories{{
    {"mode_remap", mode_remap},
    {"explicit_routes_conn_flows", explicit_routes_conn_flows},
    {"axi4_qos_profiles", axi4_qos_profiles},
    {"axi4_axqos_map", axi4_axqos_map},
    {"axi4_id_table", axi4_id_table},
    {"axi4_wrap_alignment", axi4_wrap_alignment},
}};
} // namespace

int main(int argc, char **argv) {
  if (argc != 3)
    return 2;
  const std::string_view mode(argv[1]);
  const auto selected = std::find_if(factories.begin(), factories.end(),
                                     [mode](const auto &entry) { return entry.first == mode; });
  if (selected == factories.end())
    return 2;
  const auto generated = nocloom::generate(selected->second());
  if (!generated) {
    for (const auto &diagnostic : generated.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  const std::filesystem::path directory(argv[2]);
  if (!std::filesystem::create_directory(directory))
    return 3;
  for (const auto &artifact : generated.value()) {
    std::ofstream stream(directory / artifact.path, std::ios::binary);
    stream << artifact.text;
    stream.close();
    if (!stream)
      return 3;
  }
  std::cout << "API_FEATURE_CORE_PASS " << mode << ' ' << generated.value().size()
            << " artifacts\n";
  return 0;
}
