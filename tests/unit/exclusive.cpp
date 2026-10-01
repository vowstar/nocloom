// Exclusive access (AXI A7.2): public definition, YAML, validation and map
// contract; the Weft fields; the ingress restrictions; the outlet monitor's
// matching rules. Transaction behavior is checked by run_exclusive.py.
#include "../endpoint_ports.hpp"
#include "core/ahb_outlet.hpp"
#include "core/axi4_initiator.hpp"
#include "core/axi4_outlet.hpp"
#include "core/exclusive_monitor.hpp"
#include "core/ir_cone.hpp"
#include "core/message.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
// Two AXI4 sources with two AxID bits on a one-node graph; three targets.
Initiator source(const std::string &name) {
  Initiator i{name, "main"};
  i.protocol = Protocol::Axi4;
  i.data_width = 32;
  i.id_width = 2;
  i.len_width = 8;
  return i;
}
// One owner flag per source, its value the source's position: an exclusive access names its owner.
void owner_flag(Definition &d, Target &t) {
  d.transport->user_flags = {"owner"};
  for (std::size_t i = 0; i < d.initiators.size(); ++i)
    d.initiators[i].user_flags["owner"] = {i % 2 ? AttributeSource::One : AttributeSource::Zero};
  t.exclusive_id_flags = {"owner"};
}
Definition graph() {
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "excl";
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {source("cpu0"), source("cpu1")};
  d.targets = {{"mem", "main", {{0x10000, 0x1000}}},
               {"apb", "main", {{0x20000, 0x1000}}},
               {"ext", "main", {{0x30000, 0x1000}}}};
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  d.targets[1].protocol = Protocol::Apb4;
  ExchangeTopology topology;
  topology.request = {{{"q0"}},
                      {},
                      {{"cpu0", "q0"}, {"cpu1", "q0"}},
                      {{"mem", "q0"}, {"apb", "q0"}, {"ext", "q0"}}};
  topology.response = {{{"p0"}},
                       {},
                       {{"mem", "p0"}, {"apb", "p0"}, {"ext", "p0"}},
                       {{"cpu0", "p0"}, {"cpu1", "p0"}}};
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  d.topology = topology;
  endpoint_ports(d, 2, 8);
  return d;
}
template <typename T>
void rejected(const Result<T> &r, const char *code, const std::string &field) {
  REQUIRE_FALSE(r);
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field << " " << d.message);
  REQUIRE(std::ranges::any_of(r.diagnostics(),
                              [&](const auto &d) { return d.code == code && d.field == field; }));
}
nlohmann::json map_of(const ArtifactSet &artifacts) {
  const auto it =
      std::ranges::find_if(artifacts, [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(it != artifacts.end());
  return nlohmann::json::parse(it->text);
}
unsigned port_width(const nlohmann::json &map, const std::string &name) {
  const auto &ports = map["ports"];
  const auto it = std::find_if(ports.begin(), ports.end(),
                               [&](const auto &port) { return port["name"] == name; });
  REQUIRE(it != ports.end());
  return (*it)["width"].get<unsigned>();
}
std::vector<std::string> names(const std::vector<std::pair<std::string, unsigned>> &fields) {
  std::vector<std::string> out;
  for (const auto &[name, width] : fields)
    out.push_back(name);
  return out;
}
} // namespace

TEST_CASE("Exclusive support is per target, explicit in the map", "[exclusive]") {
  auto d = graph();
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  d.targets[1].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[1]);
  d.transport->exclusive_granule = 8;
  d.targets[2].exclusive = ExclusiveAccess::Target;
  owner_flag(d, d.targets[2]);
  auto artifacts = generate(d);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  const auto &definition = map["definition"];
  // Default reservations: the values of the one owner flag.
  const auto owner = nlohmann::json::array({"owner"});
  CHECK(
      definition["exclusive_targets"] ==
      nlohmann::json(
          {{"apb", {{"mode", "network"}, {"monitors", 2}, {"granule", 8}, {"id_flags", owner}}},
           {"ext", {{"mode", "target"}, {"id_flags", owner}}},
           {"mem", {{"mode", "network"}, {"monitors", 2}, {"granule", 8}, {"id_flags", owner}}}}));
  CHECK(definition["contract"]["exclusive_access"]["key"] == "exclusive_id_flags");
  // The default port width of target mode keeps room for the widest source ID and the sources; the
  // network mode target keeps its default 4-bit ID port.
  CHECK(definition["target_id_bits"] == nlohmann::json({{"ext", 3}}));
  for (const auto *signal : {"awid", "bid", "arid", "rid"}) {
    CHECK(port_width(map, std::string("nl_t_ext_") + signal) == 3);
    CHECK(port_width(map, std::string("nl_t_mem_") + signal) == 4);
  }
  // A definition without exclusive support states the default and writing
  // exclusive: none keeps its bytes.
  auto plain = graph();
  auto none = graph();
  for (auto &t : none.targets)
    t.exclusive = ExclusiveAccess::None;
  const auto plain_artifacts = generate(plain);
  REQUIRE(plain_artifacts);
  REQUIRE(plain_artifacts.value() == generate(none).value());
  const auto plain_map = map_of(plain_artifacts.value());
  CHECK(plain_map["definition"]["contract"]["exclusive"] ==
        "AxLOCK_ignored_normal_access_OKAY_never_EXOKAY_AXI_A7_2_5");
  CHECK_FALSE(plain_map["definition"].contains("exclusive_targets"));
}

TEST_CASE("Default reservations count the values of the owner flags", "[exclusive]") {
  auto monitors = [](const Definition &d) {
    auto artifacts = generate(d);
    REQUIRE(artifacts);
    return map_of(artifacts.value())["definition"]["exclusive_targets"]["mem"]["monitors"]
        .get<unsigned>();
  };
  auto d = graph();
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  CHECK(monitors(d) == 2);
  // One flag per bit, at most 16 reservations.
  auto wide = d;
  wide.transport->user_flags = {"a", "b", "c", "d", "e"};
  wide.targets[0].exclusive_id_flags = wide.transport->user_flags;
  for (auto &i : wide.initiators) {
    i.user_flags.clear();
    for (const auto &flag : wide.transport->user_flags)
      i.user_flags[flag] = {AttributeSource::Zero};
  }
  CHECK(monitors(wide) == 16);
}

TEST_CASE("Exclusive support rejects unsupported combinations with their field", "[exclusive]") {
  auto check = [](auto edit, const char *code, const std::string &field) {
    auto d = graph();
    edit(d);
    rejected(make_plan(d), code, field);
  };
  check([](Definition &d) { d.targets[0].exclusive = static_cast<ExclusiveAccess>(7); },
        "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive");
  check([](Definition &d) { d.targets[0].exclusive_monitors = 2; }, "NCL_TARGET_EXCLUSIVE",
        "targets[0].exclusive_monitors");
  // The global granule needs a network monitor somewhere.
  check(
      [](Definition &d) {
        d.targets[2].exclusive = ExclusiveAccess::Target;
        owner_flag(d, d.targets[2]);
        d.transport->exclusive_granule = 4;
      },
      "NCL_TARGET_EXCLUSIVE", "transport.exclusive_granule");
  check([](Definition &d) { d.targets[1].exclusive = ExclusiveAccess::Target; },
        "NCL_TARGET_EXCLUSIVE", "targets[1].exclusive");
  check(
      [](Definition &d) {
        d.targets[0].exclusive = ExclusiveAccess::Network;
        owner_flag(d, d.targets[0]);
        d.targets[0].access = Access::ReadOnly;
      },
      "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive");
  for (unsigned value : {0U, 17U, 9U})
    check(
        [&](Definition &d) {
          d.targets[0].exclusive = ExclusiveAccess::Network;
          owner_flag(d, d.targets[0]);
          d.targets[0].exclusive_monitors = value;
        },
        "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive_monitors");
  for (unsigned value : {0U, 3U, 256U})
    check(
        [&](Definition &d) {
          d.targets[0].exclusive = ExclusiveAccess::Network;
          owner_flag(d, d.targets[0]);
          d.transport->exclusive_granule = value;
        },
        "NCL_TARGET_EXCLUSIVE", "transport.exclusive_granule");
  check(
      [](Definition &d) {
        d.targets[0].exclusive = ExclusiveAccess::Network;
        owner_flag(d, d.targets[0]);
        set_protocol(d.initiators[0], Protocol::Axi4Lite);
        d.initiators[1].access = Access::ReadOnly;
      },
      "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive");
  // Exclusive access needs distinct owner flags of the transport, and only then.
  check([](Definition &d) { d.targets[0].exclusive = ExclusiveAccess::Network; },
        "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive_id_flags");
  check([](Definition &d) { d.targets[2].exclusive = ExclusiveAccess::Target; },
        "NCL_TARGET_EXCLUSIVE", "targets[2].exclusive_id_flags");
  check([](Definition &d) { d.targets[0].exclusive_id_flags = {"owner"}; }, "NCL_TARGET_EXCLUSIVE",
        "targets[0].exclusive_id_flags");
  check(
      [](Definition &d) {
        d.targets[0].exclusive = ExclusiveAccess::Network;
        owner_flag(d, d.targets[0]);
        d.targets[0].exclusive_id_flags = {"other"};
      },
      "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive_id_flags[0]");
  check(
      [](Definition &d) {
        d.targets[0].exclusive = ExclusiveAccess::Network;
        owner_flag(d, d.targets[0]);
        d.targets[0].exclusive_id_flags = {"owner", "owner"};
      },
      "NCL_TARGET_EXCLUSIVE", "targets[0].exclusive_id_flags[1]");
  // Target mode takes a timer, with one or several bursts in flight.
  auto timed = graph();
  timed.targets[2].exclusive = ExclusiveAccess::Target;
  owner_flag(timed, timed.targets[2]);
  timed.targets[2].timeout = TargetTimeout{1024, true};
  REQUIRE(make_plan(timed));
  REQUIRE(generate(timed));
  timed.targets[2].max_outstanding = 4;
  REQUIRE(make_plan(timed));
  REQUIRE(generate(timed));
}

TEST_CASE("YAML exclusive keys equal the API definition", "[exclusive]") {
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: excl
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, data_width: 32, protocol: axi4, len_width: 8, id_width: 2, user_flags: {owner: 0}}
  - {name: cpu1, clock_domain: main, data_width: 32, protocol: axi4, len_width: 8, id_width: 2, user_flags: {owner: 1}}
targets:
  - {name: mem, clock_domain: main, data_width: 32, protocol: axi4, mappings: [{base: '0x10000', size: '0x1000'}], exclusive: network, exclusive_monitors: 2, exclusive_id_flags: [owner], len_width: 8, attributes: amba}
  - {name: apb, clock_domain: main, data_width: 32, mappings: [{base: '0x20000', size: '0x1000'}], protocol: apb4, attributes: amba}
  - {name: ext, clock_domain: main, data_width: 32, protocol: axi4, mappings: [{base: '0x30000', size: '0x1000'}], exclusive: target, exclusive_id_flags: [owner], len_width: 8, attributes: amba}
transport:
  max_outstanding: 1
  transport_word_bytes: 4
  exclusive_granule: 16
  user_flags: [owner]
topology:
  request: {nodes: [{name: q0}], links: [], entries: [{endpoint: cpu0, node: q0}, {endpoint: cpu1, node: q0}], exits: [{endpoint: mem, node: q0}, {endpoint: apb, node: q0}, {endpoint: ext, node: q0}]}
  response: {nodes: [{name: p0}], links: [], entries: [{endpoint: mem, node: p0}, {endpoint: apb, node: p0}, {endpoint: ext, node: p0}], exits: [{endpoint: cpu0, node: p0}, {endpoint: cpu1, node: p0}]}
output: {backend: verilog, language: verilog_2001}
)";
  auto parsed = config::parse(yaml);
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  CHECK(d.targets[0].exclusive == ExclusiveAccess::Network);
  CHECK(d.targets[0].exclusive_monitors == 2U);
  CHECK(d.transport->exclusive_granule == 16U);
  CHECK_FALSE(d.targets[1].exclusive);
  CHECK(d.targets[2].exclusive == ExclusiveAccess::Target);
  auto api = graph();
  api.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(api, api.targets[0]);
  api.targets[0].exclusive_monitors = 2;
  api.transport->exclusive_granule = 16;
  api.targets[2].exclusive = ExclusiveAccess::Target;
  owner_flag(api, api.targets[2]);
  REQUIRE(generate(d).value() == generate(api).value());
  auto bad = yaml;
  bad.replace(bad.find("exclusive: target"), 17, "exclusive: always");
  rejected(config::parse(bad), "NCL_TARGET_EXCLUSIVE", "targets[2].exclusive");
}

TEST_CASE("Weft exclusive fields follow cache and result", "[exclusive]") {
  BurstProfile plain{2, 8, true, 32};
  BurstProfile exclusive = plain;
  exclusive.exclusive = true;
  CHECK(names(burst_request_fields(32, plain, 2)) ==
        std::vector<std::string>{"address", "write", "protection", "length", "size", "burst",
                                 "cache", "start_address", "id", "source", "count", "ends",
                                 "wrap_end", "error", "first", "last", "data", "be"});
  CHECK(names(burst_request_fields(32, exclusive, 2)) ==
        std::vector<std::string>{"address", "write", "protection", "length", "size", "burst",
                                 "cache", "lock", "start_address", "id", "source", "count", "ends",
                                 "wrap_end", "error", "first", "last", "data", "be"});
  CHECK(names(burst_response_fields(32, exclusive, 2)) ==
        std::vector<std::string>{"address", "result", "exclusive_okay", "id", "source", "count",
                                 "error", "first", "last", "data"});
  CHECK(names(burst_response_fields(32, plain, 2)) ==
        std::vector<std::string>{"address", "result", "id", "source", "count", "error", "first",
                                 "last", "data"});
}

TEST_CASE("The ingress applies the exclusive envelope inside exclusive windows only",
          "[exclusive]") {
  Axi4InitiatorProfile p{"s", 32, 32, 2, 8, true, true, 2};
  p.exclusive = true;
  // A network window (128 bytes) and a strobeless window (no narrow beats).
  p.exclusive_windows = {{{0x10000, 0x1000}, 128, 0}, {{0x20000, 0x1000}, 64, 2}};
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  const auto &n = lowered.value();
  auto state = [&](const std::string &key) {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  };
  auto evaluate = [&](const std::string &key, std::uint64_t addr, std::uint64_t len,
                      std::uint64_t size, std::uint64_t burst, std::uint64_t lock) {
    return evaluate_cone(n.ir, n.bindings.at(key).index,
                         {{state("rr.state"), 1},
                          {state("rr.desc.addr"), addr},
                          {state("rr.desc.len"), len},
                          {state("rr.desc.size"), size},
                          {state("rr.desc.burst"), burst},
                          {state("rr.desc.lock"), lock}});
  };
  auto admitted = [&](std::uint64_t addr, std::uint64_t len, std::uint64_t size,
                      std::uint64_t burst) {
    return evaluate("rr.exclusive_ok", addr, len, size, burst, 1) == 1U;
  };
  // AXI A7.2.4 permits 1, 2, 4, 8 or 16 beats, aligned to their total bytes.
  for (std::uint64_t len : {0U, 1U, 3U, 7U, 15U})
    CHECK(admitted(0x10000, len, 2, 1));
  for (std::uint64_t len : {2U, 4U, 5U, 16U, 31U})
    CHECK_FALSE(admitted(0x10000, len, 2, 1));
  CHECK_FALSE(admitted(0x10000, 63, 2, 1));  // 256 bytes over the 128-byte limit
  CHECK_FALSE(admitted(0x10080, 127, 0, 1)); // 128 one-byte beats
  CHECK(admitted(0x10080, 15, 3, 1));        // 16 eight-byte beats, 128 bytes
  CHECK_FALSE(admitted(0x10040, 15, 3, 1));  // not aligned to 128 bytes
  CHECK_FALSE(admitted(0x10000, 15, 4, 1));  // 16 beats, 256 bytes
  CHECK_FALSE(admitted(0x10008, 3, 2, 1));   // 16 bytes at an 8-byte offset
  CHECK(admitted(0x10010, 3, 2, 1));
  CHECK(admitted(0x10002, 0, 1, 1));       // halfword
  CHECK_FALSE(admitted(0x10001, 0, 1, 1)); // unaligned halfword
  CHECK_FALSE(admitted(0x10000, 0, 2, 0)); // FIXED
  CHECK(admitted(0x10000, 1, 2, 2));       // an aligned WRAP is an INCR
  // The window's byte limit and minimum beat size.
  CHECK(admitted(0x20000, 15, 2, 1));
  CHECK_FALSE(admitted(0x20000, 15, 3, 1)); // 128 bytes over a 64-byte limit
  CHECK_FALSE(admitted(0x20002, 0, 1, 1));  // narrow beat at a strobeless target
  CHECK(admitted(0x20004, 0, 2, 1));
  // Outside every exclusive window AxLOCK is ignored: no lock is sent and
  // no restriction applies.
  CHECK(evaluate("rr.locked", 0x40000, 2, 2, 1, 1) == 0U);
  CHECK(evaluate("rr.exclusive_ok", 0x40000, 2, 2, 0, 1) == 1U);
  CHECK(evaluate("rr.locked", 0x10000, 0, 2, 1, 1) == 1U);
  CHECK(evaluate("rr.locked", 0x10000, 0, 2, 1, 0) == 0U);
  // Without exclusive windows the ingress has no exclusive check at all.
  Axi4InitiatorProfile plain{"s", 32, 32, 2, 8, true, true, 2};
  auto plain_lowered = lower_axi4_initiator(plain);
  REQUIRE(plain_lowered);
  CHECK(plain_lowered.value()
            .ir.values.at(plain_lowered.value().bindings.at("rr.exclusive_ok").index)
            .op == Op::Constant);
}

TEST_CASE("The network monitor keys by the owner flags", "[exclusive]") {
  Axi4OutletProfile p{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x1000}}};
  p.exclusive = true;
  p.user_bits = 3;
  // Flags 2 and 0 of the user field: flag 2 is the low bit of the key.
  p.exclusive_target = {ExclusiveAccess::Network, 2, 8, {2, 0}};
  auto lowered = lower_axi4_outlet(p);
  REQUIRE(lowered);
  const auto &n = lowered.value();
  auto state = [&](const std::string &key) {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  };
  auto port = [&](const std::string &name) {
    return static_cast<std::size_t>(std::ranges::find(n.ir.ports, name, &Port::name) -
                                    n.ir.ports.begin());
  };
  // Slot 0 holds key 0b10, an 8-byte granule at 0x10040.
  const std::map<std::size_t, std::uint64_t> held{{state("exclusive.slot0.valid"), 1},
                                                  {state("exclusive.slot0.key"), 0b10},
                                                  {state("exclusive.slot0.base"), 0x10040},
                                                  {state("exclusive.slot0.log"), 3}};
  auto failing = [&](std::uint64_t source, std::uint64_t id, std::uint64_t user, std::uint64_t addr,
                     std::uint64_t len, std::uint64_t size) {
    return evaluate_cone(n.ir, n.bindings.at("w.exclusive_failing").index, held,
                         {{port("nl_wreq_lock"), 1},
                          {port("nl_wreq_source"), source},
                          {port("nl_wreq_id"), id},
                          {port("nl_wreq_user"), user},
                          {port("nl_wreq_address"), addr},
                          {port("nl_wreq_length"), len},
                          {port("nl_wreq_size"), size}});
  };
  // Flag 0 set, flags 1 free and 2 clear: key 0b10, whatever the source and AxID.
  CHECK(failing(2, 2, 0b001, 0x10040, 0, 2) == 0U);
  CHECK(failing(2, 2, 0b011, 0x10044, 0, 2) == 0U); // same 8-byte granule, flag 1 outside the key
  CHECK(failing(2, 1, 0b001, 0x10040, 0, 2) == 0U); // another AxID
  CHECK(failing(0, 2, 0b001, 0x10040, 0, 2) == 0U); // another source
  CHECK(failing(2, 2, 0b101, 0x10040, 0, 2) == 1U); // flag 2: another key
  CHECK(failing(2, 2, 0b000, 0x10040, 0, 2) == 1U); // flag 0: another key
  CHECK(failing(2, 2, 0b001, 0x10048, 0, 2) == 1U); // another granule
  CHECK(failing(2, 2, 0b001, 0x10040, 3, 2) == 1U); // 16 bytes: another region
  // Every monitor state word is named for the outlet's storage records.
  for (const auto &name :
       ExclusiveMonitor::state_names("exclusive", ExclusiveMonitorProfile{2, 8, 32, {2, 0}}))
    CHECK(n.bindings.contains(name));
}

TEST_CASE("Target mode with a single source keeps the AxID width", "[exclusive]") {
  // One source: a one-bit source field (stamps 0 and 1) and no index bits.
  auto d = graph();
  d.initiators = {source("cpu0")};
  d.topology->request.entries = {{"cpu0", "q0"}};
  d.topology->response.exits = {{"cpu0", "p0"}};
  d.targets[2].exclusive = ExclusiveAccess::Target;
  owner_flag(d, d.targets[2]);
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  auto artifacts = generate(d);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  CHECK(map["definition"]["target_id_bits"] == nlohmann::json({{"ext", 2}}));
  CHECK(port_width(map, "nl_t_ext_awid") == 2);
  // The outlet answers a request of an unknown source locally: with one
  // source the whole one-bit field names the stamp.
  Axi4OutletProfile p{
      "t", 32, 2, 8, 1, true, std::vector<nocloom::AddressMapping>{{0x30000, 0x1000}}};
  p.exclusive = true;
  p.exclusive_target = {ExclusiveAccess::Target};
  p.id_sources = {0};
  CHECK(lower_axi4_outlet(p));
}

TEST_CASE("Clocked graphs keep exclusive support", "[exclusive]") {
  auto d = graph();
  for (auto *graph_part : {&d.topology->request, &d.topology->response})
    for (auto &node : graph_part->nodes)
      node.clock_domain = "main";
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  auto artifacts = generate(d);
  REQUIRE(artifacts);
  CHECK(map_of(artifacts.value())["definition"]["exclusive_targets"].contains(d.targets[0].name));
}

TEST_CASE("Outlets reject exclusive profiles they cannot serve", "[exclusive]") {
  Axi4OutletProfile multi{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x1000}}};
  multi.exclusive = true;
  multi.exclusive_target = {ExclusiveAccess::Network, 2, 1};
  multi.max_outstanding = 2;
  multi.id_sources = {0, 1};
  CHECK(lower_axi4_outlet(multi));
  multi.exclusive_target.monitors = 17;
  CHECK_FALSE(lower_axi4_outlet(multi));
  Axi4OutletProfile timed{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x1000}}};
  timed.exclusive = true;
  timed.exclusive_target = {ExclusiveAccess::Target};
  timed.id_sources = {0, 1};
  timed.timeout_cycles = 1024;
  CHECK(lower_axi4_outlet(timed));
  timed.max_outstanding = 2;
  CHECK(lower_axi4_outlet(timed));
  BurstOutletProfile apb{"t", 2, 8, 2, std::vector<nocloom::AddressMapping>{{0x20000, 0x1000}}};
  apb.exclusive = true;
  apb.exclusive_target = {ExclusiveAccess::Target};
  CHECK_FALSE(lower_apb_burst_outlet(apb));
  apb.exclusive_target = {ExclusiveAccess::Network, 4, 1};
  CHECK(lower_apb_burst_outlet(apb));
  apb.exclusive = false;
  CHECK_FALSE(lower_apb_burst_outlet(apb));
}
