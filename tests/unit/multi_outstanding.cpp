// Multiple outstanding bursts: public definition, YAML, validation and map
// contract; the initiator slot and ID tables, the Weft tag and the
// destination decode; the multi-burst AXI4 outlet. Transaction behavior is checked by
// run_multi_outstanding.py.
#include "../endpoint_ports.hpp"
#include "../public_artifacts.hpp"
#include "core/ahb_outlet.hpp"
#include "core/axi4_initiator.hpp"
#include "core/axi4_outlet.hpp"
#include "core/axi4_upsize.hpp"
#include "core/ir_cone.hpp"
#include "core/message.hpp"
#include "core/rate_limiter.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <numeric>

using namespace nocloom;
using namespace nocloom::detail;

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
Definition funnel() {
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "queued";
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"cpu0", "main"}, {"cpu1", "main"}};
  d.targets = {{"bank0", "main", {{0, 4096}}}, {"bank1", "main", {{4096, 4096}}}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
  }
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  d.transport = TransportOptions{.max_outstanding = 1, .transport_word_bytes = 4};
  endpoint_ports(d, 2, 8);
  return d;
}
// Two AXI4 sources (two AxID bits, four bursts each) and two AXI4 targets at
// 0x10000 and 0x20000 over one switch per direction, or two-level graphs.
Definition queued(bool two_level) {
  auto d = funnel();
  d.initiators = {{"s0", "main"}, {"s1", "main"}};
  d.targets = {{"t0", "main", {{0x10000, 0x10000}}}, {"t1", "main", {{0x20000, 0x10000}}}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
  }
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  endpoint_ports(d, 2, 8);
  d.transport->max_outstanding = 4;
  ExchangeTopology topology;
  if (two_level) {
    topology.request = {{{"a"}, {"b"}, {"m"}},
                        {{"a", "m"}, {"b", "m"}},
                        {{"s0", "a"}, {"s1", "b"}},
                        {{"t0", "m"}, {"t1", "m"}}};
    topology.response = {{{"x"}, {"y"}, {"n"}},
                         {{"x", "n"}, {"y", "n"}},
                         {{"t0", "x"}, {"t1", "y"}},
                         {{"s0", "n"}, {"s1", "n"}}};
  } else {
    topology.request = {{{"q"}}, {}, {}, {}};
    topology.response = {{{"p"}}, {}, {}, {}};
    for (const auto &i : d.initiators) {
      topology.request.entries.push_back({i.name, "q"});
      topology.response.exits.push_back({i.name, "p"});
    }
    for (const auto &t : d.targets) {
      topology.request.exits.push_back({t.name, "q"});
      topology.response.entries.push_back({t.name, "p"});
    }
  }
  d.topology = topology;
  return d;
}
// Sources and the request switch in "near", targets and the response switch in "far".
Definition near_far(Definition d) {
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"near", "clk_near", "reset_n", "test"},
                     {"far", "clk_far", "reset_n", "test"}};
  for (auto &i : d.initiators)
    i.clock_domain = "near";
  for (auto &t : d.targets)
    t.clock_domain = "far";
  for (auto &n : d.topology->request.nodes)
    n.clock_domain = "near";
  for (auto &n : d.topology->response.nodes)
    n.clock_domain = "far";
  return d;
}
template <typename T> void rejected(const Result<T> &r, const char *code, const char *field) {
  REQUIRE_FALSE(r);
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field);
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
} // namespace

TEST_CASE("Outstanding capacities are explicit in the definition and the map",
          "[multi-outstanding]") {
  auto d = funnel();
  d.initiators[0].max_outstanding = 4;
  d.targets[1].max_outstanding = 2;
  auto artifacts = generate(d);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  const auto &definition = map["definition"];
  CHECK(definition["transport"]["max_outstanding"] == 1);
  CHECK(definition["outstanding"]["initiators"] == nlohmann::json({{"cpu0", 4}, {"cpu1", 1}}));
  CHECK(definition["outstanding"]["targets"] == nlohmann::json({{"bank0", 1}, {"bank1", 2}}));
  CHECK(definition["contract"]["capacity"] ==
        "source_bursts_per_direction_as_many_transactions_shared_by_reads_and_writes_target_"
        "bursts_shared_by_reads_and_writes_see_outstanding");
  // A decode miss answers DECERR from the default target.
  CHECK(definition["contract"]["ordering"]["decode_miss"] ==
        "DECERR_from_the_default_target_without_target_access");
  // bank1's default ID port has room for the two AxID bits and the source; bank0 keeps the default
  // 4-bit ID port.
  CHECK(definition["target_id_bits"] == nlohmann::json({{"bank1", 3}}));
  for (const auto *signal : {"awid", "bid", "arid", "rid"}) {
    CHECK(port_width(map, std::string("nl_t_bank1_") + signal) == 3);
    CHECK(port_width(map, std::string("nl_t_bank0_") + signal) == 4);
    CHECK(port_width(map, std::string("nl_i_cpu0_") + signal) == 2);
  }
  // The global value is the default of every AXI4 source.
  auto global = funnel();
  global.transport->max_outstanding = 3;
  global.initiators[1].max_outstanding = 1;
  auto planned = generate(global);
  REQUIRE(planned);
  CHECK(map_of(planned.value())["definition"]["outstanding"]["initiators"] ==
        nlohmann::json({{"cpu0", 3}, {"cpu1", 1}}));
  // Writing capacity one explicitly keeps the single-outstanding bytes.
  auto explicit_one = funnel();
  for (auto &i : explicit_one.initiators)
    i.max_outstanding = 1;
  for (auto &t : explicit_one.targets)
    t.max_outstanding = 1;
  CHECK(generate(explicit_one).value() == generate(funnel()).value());
}

TEST_CASE("Outstanding capacities parse from YAML like the direct API", "[multi-outstanding]") {
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: queued
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, max_outstanding: 4, len_width: 8, id_width: 2}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], max_outstanding: 2, len_width: 8, attributes: amba}
transport: {max_outstanding: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
  auto parsed = config::parse(yaml);
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  CHECK(d.initiators[0].max_outstanding == 4U);
  CHECK_FALSE(d.initiators[1].max_outstanding);
  CHECK(d.targets[1].max_outstanding == 2U);
  auto api = funnel();
  api.initiators[0].max_outstanding = 4;
  api.targets[1].max_outstanding = 2;
  REQUIRE(generate(d).value() == generate(api).value());
  // A queued target's ID port may be narrower than the default (3 bits here): requests share its
  // IDs. A target ID holds the flow number, so a single flow takes any width.
  const auto with_bank1 = [&](const std::string &fields) {
    auto text = yaml;
    text.insert(text.find("max_outstanding: 2, len_width: 8"), fields);
    auto out = config::parse(text, "queued.yaml");
    REQUIRE(out);
    return out;
  };
  CHECK(make_plan(with_bank1("id_width: 0, ").value().definition));
  CHECK(make_plan(with_bank1("id_width: 2, ").value().definition));
  CHECK(make_plan(with_bank1("id_width: 3, ").value().definition));
  // A valid ID width beside an invalid length names the length.
  auto bad_len = with_bank1("id_width: 3, ").value().definition;
  bad_len.targets[1].len_width = 9;
  rejected(make_plan(bad_len), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[1].len_width");
  bad_len.targets[1].id_width = 32;
  rejected(make_plan(bad_len), "NCL_ENDPOINT_TRANSACTION_WIDTH", "targets[1].id_width");
}

TEST_CASE("Outstanding capacities reject unsupported combinations with their field",
          "[multi-outstanding]") {
  {
    auto d = funnel();
    d.initiators[0].max_outstanding = 65;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "initiators[0].max_outstanding");
  }
  {
    auto d = funnel();
    d.targets[0].max_outstanding = 17;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "targets[0].max_outstanding");
  }
  {
    // A Lite source inherits a global capacity above one (one ordering key,
    // lite_outstanding.cpp) or keeps one by its own value.
    auto d = funnel();
    d.transport->max_outstanding = 2;
    set_protocol(d.initiators[1], Protocol::Axi4Lite);
    REQUIRE(make_plan(d));
    d.initiators[1].max_outstanding = 1;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
  {
    auto d = funnel();
    d.initiators[0].max_outstanding = 2;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
  {
    auto d = funnel();
    for (auto &i : d.initiators)
      i.data_width = 128;
    d.initiators[0].max_outstanding = 2;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
  {
    auto d = funnel();
    set_protocol(d.targets[0], Protocol::Apb4);
    d.targets[0].max_outstanding = 2;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY_COMBINATION", "targets[0].max_outstanding");
  }
  {
    // One watchdog serves every burst in flight at the target.
    auto d = funnel();
    d.targets[0].max_outstanding = 2;
    d.targets[0].timeout = TargetTimeout{1024};
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
  {
    // An AXI4-Lite source (AxID 0) may reach a multi-burst target, with one or
    // several transfers, and so may a source converted to the target width.
    auto lite = funnel();
    lite.targets[0].max_outstanding = 2;
    set_protocol(lite.initiators[1], Protocol::Axi4Lite);
    REQUIRE(make_plan(lite));
    lite.initiators[1].max_outstanding = 4;
    REQUIRE(generate(lite));
    auto converted = funnel();
    converted.initiators[1].data_width = 64;
    converted.targets[0].max_outstanding = 2;
    REQUIRE(make_plan(converted));
    REQUIRE(generate(converted));
    // An 8-bit APB target elsewhere gives bank0 no width merge of its own.
    set_protocol(converted.targets[1], Protocol::Apb4);
    converted.targets[1].data_width = 8;
    REQUIRE(generate(converted));
  }
  {
    auto d = funnel();
    d.targets[0].max_outstanding = 0;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "targets[0].max_outstanding");
  }
  {
    // A multi-burst source records one of at most 256 routing windows per burst: 256 pass, 257
    // are rejected at the capacity that asks for the table, before generation.
    auto d = funnel();
    d.targets[1].mappings.clear();
    for (std::uint64_t k = 1; k < 256; ++k)
      d.targets[1].mappings.push_back({k * 8192, 4096});
    d.initiators[0].max_outstanding = 2;
    REQUIRE(generate(d));
    d.targets[1].mappings.push_back({256 * 8192, 4096});
    rejected(make_plan(d), "NCL_AXI4_CAPACITY_COMBINATION", "initiators[0].max_outstanding");
    rejected(validate(d), "NCL_AXI4_CAPACITY_COMBINATION", "initiators[0].max_outstanding");
    d.initiators[0].max_outstanding.reset();
    REQUIRE(generate(d));
    d.transport->max_outstanding = 4;
    set_protocol(d.initiators[1], Protocol::Axi4Lite);
    rejected(make_plan(d), "NCL_AXI4_CAPACITY_COMBINATION", "transport.max_outstanding");
  }
  {
    // A target at the network width is not a per-target width.
    auto d = funnel();
    d.targets[0].data_width = 32;
    d.targets[0].max_outstanding = 2;
    d.initiators[0].max_outstanding = 4;
    REQUIRE(make_plan(d));
  }
}

TEST_CASE("Bounded packets keep independent target capacity and source burst credits",
          "[multi-outstanding]") {
  for (const auto capacity : {2U, 4U, 16U}) {
    auto d = funnel();
    d.initiators[0].max_outstanding = 4;
    d.initiators[0].narrow_bursts = NarrowBursts::SplitNonModifiable;
    d.targets[0].max_outstanding = capacity;
    auto generated = generate(d);
    REQUIRE(generated);
    const auto map = map_of(generated.value());
    CHECK(map["definition"]["outstanding"]["targets"]["bank0"] == capacity);
    CHECK(map["definition"]["outstanding"]["initiators"]["cpu0"] == 4);
    CHECK(map["definition"]["contract"]["target_outstanding"]["packets"] ==
          "each_packet_uses_one_pool_entry_same_source_tag_keeps_separate_ordered_contexts_"
          "source_burst_capacity_and_four_packet_splitter_limit_are_independent");
    // Per-target mode also equips the equal-width target with a merge.
    d.targets[1].data_width = 64;
    REQUIRE(generate(d));
    d.targets[1].data_width = 32;
    // A read-only target keeps its pool for reads; writes answer locally.
    d.targets[0].access = Access::ReadOnly;
    REQUIRE(generate(d));
  }
}

TEST_CASE("A dedicated AXI4-Lite target pipelines packets of one direction",
          "[multi-outstanding]") {
  {
    auto d = funnel();
    set_protocol(d.targets[0], Protocol::Axi4Lite);
    d.targets[0].max_outstanding = 4;
    d.initiators[0].max_outstanding = 4;
    auto generated = generate(d);
    REQUIRE(generated);
    const auto map = map_of(generated.value());
    CHECK(map["definition"]["outstanding"]["targets"]["bank0"] == 4);
    CHECK(map["definition"]["contract"]["target_outstanding"]["dedicated_lite"] ==
          "packets_of_one_direction_in_flight_up_to_the_capacity_words_of_a_burst_issue_back_"
          "to_back_without_waiting_for_B_or_R_a_direction_change_waits_for_every_earlier_"
          "packet_response_one_B_after_the_last_words_B");
    // No ID port widens for a dedicated Lite target.
    const auto &definition = map["definition"];
    CHECK((!definition.contains("target_id_bits") ||
           !definition["target_id_bits"].contains("bank0")));
  }
  {
    auto d = funnel();
    set_protocol(d.targets[0], Protocol::Axi4Lite);
    // Generate through the exchange at the count-width boundary and the
    // capacity ceiling, not only through the plan.
    d.targets[0].max_outstanding = 8;
    REQUIRE(generate(d));
    d.targets[0].max_outstanding = 256;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
    d.targets[0].max_outstanding = 257;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "targets[0].max_outstanding");
    d.targets[0].max_outstanding = 0;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "targets[0].max_outstanding");
  }
  {
    // Capacity above one takes a timer; a read-only target answers its
    // writes locally, and a network monitor combines with it.
    auto d = funnel();
    set_protocol(d.targets[0], Protocol::Axi4Lite);
    d.targets[0].max_outstanding = 2;
    d.targets[0].timeout = TargetTimeout{1024};
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
    d.targets[0].timeout.reset();
    d.targets[0].access = Access::ReadOnly;
    REQUIRE(generate(d));
    d.targets[0].access.reset();
    d.targets[0].exclusive = ExclusiveAccess::Network;
    owner_flag(d, d.targets[0]);
    REQUIRE(generate(d));
  }
}

TEST_CASE("The queued initiator validates its destination decode", "[multi-outstanding]") {
  Axi4InitiatorProfile p;
  p.name = "q";
  p.id_bits = 2;
  p.source_bits = 2;
  p.transport_address_bits = 32;
  p.address_bits = 32;
  p.max_outstanding = 4;
  auto code = [](const Result<NodeHardware> &r) {
    REQUIRE_FALSE(r);
    return r.diagnostics().front().code;
  };
  CHECK(code(lower_axi4_initiator(p)) == "NCL_AXI4_INITIATOR_CAPACITY"); // no destinations
  p.destinations = {{0, 4096}, {4096, 4096}};
  CHECK(code(lower_axi4_initiator(p)) == "NCL_AXI4_INITIATOR_CAPACITY"); // no miss rule
  p.miss_destination = 0;
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  for (const auto *name : {"wr.i3.count", "wr.s3.poison", "rr.i0.dest", "rr.s3.valid", "rr.beat",
                           "wr.b_full", "wr.rsp_id", "rr.rsp_id", "wr.tag", "rr.tag"})
    CHECK(lowered.value().bindings.contains(name));
  // A Lite face has no AXI ID: its table holds one entry.
  auto lite = p;
  lite.lite = true;
  const auto lite_lowered = lower_axi4_initiator(lite);
  REQUIRE(lite_lowered);
  CHECK(lite_lowered.value().bindings.contains("wr.i0.count"));
  CHECK_FALSE(lite_lowered.value().bindings.contains("wr.i1.count"));
  auto both = p;
  both.admitted_windows = std::vector<AddressWindow>{{0, 4096}};
  CHECK(code(lower_axi4_initiator(both)) == "NCL_AXI4_INITIATOR_CAPACITY");
  p.max_outstanding = 65;
  CHECK(code(lower_axi4_initiator(p)) == "NCL_AXI4_INITIATOR_CAPACITY");
}

TEST_CASE("The multi-burst outlet widens its ID and counts its tables", "[multi-outstanding]") {
  Axi4OutletProfile p{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  p.max_outstanding = 4;
  p.id_sources = {0, 1};
  CHECK(axi4_outlet_id_bits(p) == 3);
  auto lowered = lower_axi4_outlet(p);
  REQUIRE(lowered);
  unsigned id_ports = 0;
  for (const auto &port : lowered.value().ir.ports)
    if (port.endpoint == "axi" && (port.signal == "awid" || port.signal == "bid" ||
                                   port.signal == "arid" || port.signal == "rid")) {
      CHECK(port.width == 3);
      ++id_ports;
    }
  CHECK(id_ports == 4);
  // The planned payload equals the lowered tables (flags, counters and
  // pointers are control).
  const auto bits = axi4_outlet_outstanding_bits(p);
  // Composed: two reaching sources with two AxID bits need three target ID bits.
  {
    auto composed = queued(false);
    composed.transport->max_outstanding = 1;
    composed.targets[0].max_outstanding = 4;
    const auto a = public_artifacts(composed);
    CHECK(a.port_width("nl_t_t0_awid") == 3);
  }
  CHECK(bits.first > 0);
  CHECK(bits.second > 0);
  // One source reaching it keeps the AxID width.
  auto single = p;
  single.id_sources = {1};
  CHECK(axi4_outlet_id_bits(single) == 2);
  REQUIRE(lower_axi4_outlet(single));
  auto timed = p;
  timed.timeout_cycles = 1024;
  REQUIRE(lower_axi4_outlet(timed));
  auto unsourced = p;
  unsourced.id_sources.clear();
  CHECK_FALSE(lower_axi4_outlet(unsourced));
  auto large = p;
  large.max_outstanding = 17;
  CHECK_FALSE(lower_axi4_outlet(large));
}

TEST_CASE("The network destination check evaluates the switches' own selection",
          "[multi-outstanding]") {
  // The generator checks every queued source's decode against the switches' own selection.
  for (const bool two_level : {false, true})
    REQUIRE(generate(queued(two_level)));
}

TEST_CASE("A multi-burst outlet answers a source's writes in their order", "[multi-outstanding]") {
  Axi4OutletProfile p{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  p.max_outstanding = 4;
  p.id_sources = {0, 1};
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
  auto driven = [&](const std::string &name) {
    const auto at = port(name);
    return std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  };
  // Slot 0: a network write of source 0 (stamp 0, AxID 1); slot 1: a local
  // completion of the same source behind it. The B names slot 0's target ID, 1, reversed on the
  // port.
  const std::map<std::size_t, std::uint64_t> inputs{
      {port("nl_reset_n"), 1}, {port("nl_axi_bvalid"), 1}, {port("nl_axi_bid"), 0b100}};
  auto with = [&](std::uint64_t local_ahead) {
    return std::map<std::size_t, std::uint64_t>{
        {state("w.slot0.valid"), 1}, {state("w.slot0.id"), 1},
        {state("w.slot1.valid"), 1}, {state("w.slot1.local"), 1},
        {state("w.slot1.id"), 1},    {state("w.slot1.ahead"), local_ahead}};
  };
  const auto local_fire = n.bindings.at("w.local_fire").index;
  // Behind the network write the local slot waits and the B is taken.
  CHECK(evaluate_cone(n.ir, local_fire, with(1), inputs) == 0U);
  CHECK(evaluate_cone(n.ir, driven("nl_axi_bready"), with(1), inputs) == 1U);
  // A local slot whose turn has come completes first; BREADY waits.
  CHECK(evaluate_cone(n.ir, local_fire, with(0), inputs) == 1U);
  CHECK(evaluate_cone(n.ir, driven("nl_axi_bready"), with(0), inputs) == 0U);
  // A B naming no write in flight (another source's target ID) waits.
  auto other = inputs;
  other[port("nl_axi_bid")] = 0b101;
  CHECK(evaluate_cone(n.ir, driven("nl_axi_bready"), with(1), other) == 0U);
  // Responses leave through two entries: a B is taken while the first
  // waits for the network and only a full spare entry holds BREADY low.
  auto staged = with(1);
  staged[state("w.response_full")] = 1;
  CHECK(evaluate_cone(n.ir, driven("nl_axi_bready"), staged, inputs) == 1U);
  staged[state("w.response_spare")] = 1;
  CHECK(evaluate_cone(n.ir, driven("nl_axi_bready"), staged, inputs) == 0U);
  auto local = with(0);
  local[state("w.response_spare")] = 1;
  CHECK(evaluate_cone(n.ir, local_fire, local, inputs) == 0U);
}

TEST_CASE("Behind the response merger an outlet's BREADY and RREADY read only registers",
          "[multi-outstanding]") {
  Axi4OutletProfile p{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  p.max_outstanding = 4;
  p.id_sources = {0, 1};
  p.merger_hints = true;
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
  auto driven = [&](const std::string &name) {
    const auto at = port(name);
    return std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  };
  // Slot 0: a network write of source 0 with AxID 1; read entry 0 issued to the target. On the port
  // target ID 1 reads 0b100.
  const std::map<std::size_t, std::uint64_t> base{{state("w.slot0.valid"), 1},
                                                  {state("w.slot0.id"), 1},
                                                  {state("r.slot0.valid"), 1},
                                                  {state("r.slot0.issued"), 1}};
  auto inputs = [&](std::uint64_t bid, std::uint64_t write_picked, std::uint64_t read_picked) {
    return std::map<std::size_t, std::uint64_t>{{port("nl_reset_n"), 1},
                                                {port("nl_axi_bvalid"), 1},
                                                {port("nl_axi_bid"), bid},
                                                {port("nl_hint_write_picked"), write_picked},
                                                {port("nl_hint_read_picked"), read_picked}};
  };
  const auto bready = driven("nl_axi_bready"), rready = driven("nl_axi_rready");
  const auto fire = n.bindings.at("w.b_fire").index;
  // BREADY reads no BID: a B naming slot 0 is taken, one naming no write is taken and parked.
  CHECK(evaluate_cone(n.ir, bready, base, inputs(0b100, 0, 0)) == 1U);
  CHECK(evaluate_cone(n.ir, fire, base, inputs(0b100, 0, 0)) == 1U);
  CHECK(evaluate_cone(n.ir, bready, base, inputs(0b101, 0, 0)) == 1U);
  CHECK(evaluate_cone(n.ir, fire, base, inputs(0b101, 0, 0)) == 0U);
  // A parked B holds BREADY low and completes from the register once it names slot 0.
  auto parked = base;
  parked[state("w.b_parked")] = 1;
  parked[state("w.b_parked_id")] = 1;
  CHECK(evaluate_cone(n.ir, bready, parked, inputs(0b101, 0, 0)) == 0U);
  CHECK(evaluate_cone(n.ir, fire, parked, inputs(0b101, 0, 0)) == 1U);
  parked[state("w.b_parked_id")] = 0b101;
  CHECK(evaluate_cone(n.ir, fire, parked, inputs(0b100, 0, 0)) == 0U);
  // A staged B the merger does not offer holds BREADY low; offered (taken or not) it does not.
  auto staged = base;
  staged[state("w.response_full")] = 1;
  CHECK(evaluate_cone(n.ir, bready, staged, inputs(0b100, 0, 0)) == 0U);
  CHECK(evaluate_cone(n.ir, bready, staged, inputs(0b100, 1, 0)) == 1U);
  // Likewise a staged R beat the merger does not offer holds RREADY low.
  CHECK(evaluate_cone(n.ir, rready, base, inputs(0b100, 0, 0)) == 1U);
  auto beat = base;
  beat[state("r.full")] = 1;
  CHECK(evaluate_cone(n.ir, rready, beat, inputs(0b100, 0, 0)) == 0U);
  CHECK(evaluate_cone(n.ir, rready, beat, inputs(0b100, 0, 1)) == 1U);
}

TEST_CASE("A multi-burst outlet takes the next write head once its AW stage frees",
          "[multi-outstanding]") {
  Axi4OutletProfile p{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  p.max_outstanding = 4;
  p.id_sources = {0, 1};
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
  auto driven = [&](const std::string &name) {
    const auto at = port(name);
    return std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  };
  // A head of source 0, AxID 1 with AWREADY low and WREADY low: the W entry is empty in every
  // case below.
  std::map<std::size_t, std::uint64_t> head{{port("nl_reset_n"), 1},
                                            {port("nl_wreq_source"), 0},
                                            {port("nl_wreq_id"), 1},
                                            {port("nl_axi_awready"), 0},
                                            {port("nl_axi_wready"), 0}};
  const auto ready = driven("nl_wreq_ready");
  // Every state word is given and the request's source, AxID, AWREADY and WREADY besides the
  // reset: a known result in strict mode proves the ready reads no other port. The main burst
  // has every beat and still owes its AW: the head waits (its beat may enter the W entry first).
  std::map<std::size_t, std::uint64_t> main;
  for (std::size_t k = 0; k < n.ir.states.size(); ++k)
    main[k] = 0;
  main[state("w.busy")] = 1;
  main[state("w.done")] = 1;
  main[state("w.aw_pending")] = 1;
  main[state("w.descriptor.id")] = 1;
  main[state("w.descriptor.burst")] = 1;
  main[state("pool.count")] = 1;
  CHECK(evaluate_cone(n.ir, ready, main, head, {}, true) == 0U);
  // The main burst's AW is accepted in this cycle: the head takes the AW stage.
  auto accepted = head;
  accepted[port("nl_axi_awready")] = 1;
  CHECK(evaluate_cone(n.ir, ready, main, accepted, {}, true) == 1U);
  // While the main burst still collects beats the ready takes them.
  auto collecting = main;
  collecting[state("w.done")] = 0;
  collecting[state("w.remaining")] = 2;
  CHECK(evaluate_cone(n.ir, ready, collecting, head, {}, true) == 1U);
  // With its AW sent, another AxID of the source in the main entry or in a slot does not hold
  // the head back (writes of every target ID overlap).
  auto sent = main;
  sent[state("w.aw_pending")] = 0;
  CHECK(evaluate_cone(n.ir, ready, sent, head, {}, true) == 1U);
  auto clash = sent;
  clash[state("w.descriptor.id")] = 2;
  CHECK(evaluate_cone(n.ir, ready, clash, head, {}, true) == 1U);
  auto slot_clash = sent;
  slot_clash[state("w.descriptor.source")] = 2;
  slot_clash[state("w.slot0.valid")] = 1;
  slot_clash[state("w.slot0.source")] = 0;
  slot_clash[state("w.slot0.id")] = 2;
  slot_clash[state("pool.count")] = 2;
  CHECK(evaluate_cone(n.ir, ready, slot_clash, head, {}, true) == 1U);
  // A head needs the pool below the capacity: the three slotted writes and the main burst fill
  // it, unless its beat already entered (the pool admitted it then).
  auto one_free = sent;
  for (unsigned k = 0; k < 3; ++k) {
    one_free[state("w.slot" + std::to_string(k) + ".valid")] = 1;
    one_free[state("w.slot" + std::to_string(k) + ".source")] = 2;
  }
  one_free[state("pool.count")] = 4;
  CHECK(evaluate_cone(n.ir, ready, one_free, head, {}, true) == 0U);
  one_free[state("w.head_beat_in")] = 1;
  one_free[state("w.full")] = 1;
  CHECK(evaluate_cone(n.ir, ready, one_free, head, {}, true) == 1U);
}

TEST_CASE("Three sources number the target ID with unused index codes", "[multi-outstanding]") {
  // Three reaching sources with their route numbers in the sum.
  Axi4OutletProfile p{
      "t", 32, 2, 8, 3, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  p.max_outstanding = 4;
  p.id_sources = {0, 2, 3};
  p.id_route.direction_bits = 1;
  for (const unsigned number : {0U, 1U, 2U})
    p.id_route.sources.push_back({{std::uint64_t{number} << 3}, {{}}, true});
  CHECK(axi4_outlet_id_bits(p) == 4);
  auto lowered = lower_axi4_outlet(p);
  REQUIRE(lowered);
  const auto &n = lowered.value();
  auto state = [&](const std::string &key) {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  };
  auto driven = [&](const std::string &name) {
    const auto at = static_cast<std::size_t>(std::ranges::find(n.ir.ports, name, &Port::name) -
                                             n.ir.ports.begin());
    return std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  };
  // Write stamps are 2 * source: source 3 (stamp 6) has number 2. route word {number, AxID 1, write
  // 1} is 0b00011 + number * 8; the 4-bit slices of number 2's 0b10011 add up to 3 + 1. The port
  // reverses each: 3, 11 and 4 read 12, 13 and 2.
  for (const auto &[stamp, sum] :
       std::vector<std::pair<std::uint64_t, std::uint64_t>>{{0, 12}, {4, 13}, {6, 2}})
    CHECK(evaluate_cone(n.ir, driven("nl_axi_awid"),
                        {{state("w.descriptor.source"), stamp}, {state("w.descriptor.id"), 1}}) ==
          sum);
  // A composed three-source network: the default target ID width distinguishes
  // all three source indices and their AXI IDs, and the destination checks hold.
  auto three = queued(false);
  three.initiators.push_back(three.initiators[1]);
  three.initiators[2].name = "s2";
  three.topology->request.entries.push_back({"s2", "q"});
  three.topology->response.exits.push_back({"s2", "p"});
  three.transport->max_outstanding = 3;
  three.targets[0].max_outstanding = 4;
  CHECK(public_artifacts(three).port_width("nl_t_t0_awid") == 4);
}

// ---------------- per-ID tracking (several destinations in flight) ----------------

TEST_CASE("The ID table size is explicit, bounded and mapped", "[multi-outstanding]") {
  auto d = funnel();
  d.initiators[0].max_outstanding = 4;
  auto plain = generate(d);
  REQUIRE(plain);
  // Two AxID bits, two directions and four transactions: four entries by default, a table that
  // never fills, so none is built.
  CHECK(map_of(plain.value())["definition"]["outstanding"]["initiator_id_tables"] ==
        nlohmann::json({{"cpu0", 4}}));
  CHECK(map_of(plain.value())["definition"]["contract"]["ordering"]["id_table"].is_string());
  auto own = d;
  own.initiators[0].id_table_entries = 2;
  auto owned = generate(own);
  REQUIRE(owned);
  CHECK(map_of(owned.value())["definition"]["outstanding"]["initiator_id_tables"] ==
        nlohmann::json({{"cpu0", 2}}));
  CHECK(owned.value() != plain.value());
  auto global = d;
  global.transport->id_table_entries = 3;
  auto inherited = generate(global);
  REQUIRE(inherited);
  CHECK(map_of(inherited.value())["definition"]["outstanding"]["initiator_id_tables"] ==
        nlohmann::json({{"cpu0", 3}}));
  // The bound counts both directions: one AxID bit still allows four keys.
  auto narrow = d;
  narrow.initiators[0].id_width = 1;
  CHECK(map_of(generate(narrow).value())["definition"]["outstanding"]["initiator_id_tables"] ==
        nlohmann::json({{"cpu0", 4}}));
  narrow.initiators[0].id_table_entries = 3;
  REQUIRE(make_plan(narrow));
  // A one-way source has one direction.
  narrow.initiators[0].access = nocloom::Access::ReadOnly;
  rejected(make_plan(narrow), "NCL_AXI4_ID_CAPACITY", "initiators[0].id_table_entries");
  auto zero = d;
  zero.initiators[0].id_table_entries = 0;
  rejected(make_plan(zero), "NCL_AXI4_ID_CAPACITY", "initiators[0].id_table_entries");
  auto above = d;
  above.initiators[0].id_table_entries = 5;
  rejected(make_plan(above), "NCL_AXI4_ID_CAPACITY", "initiators[0].id_table_entries");
  auto global_above = d;
  global_above.transport->id_table_entries = 5;
  rejected(make_plan(global_above), "NCL_AXI4_ID_CAPACITY", "transport.id_table_entries");
  // A table belongs to a source with several bursts in flight.
  auto single = d;
  single.initiators[1].id_table_entries = 1;
  rejected(make_plan(single), "NCL_AXI4_ID_CAPACITY", "initiators[1].id_table_entries");
  auto unused = funnel();
  unused.transport->id_table_entries = 1;
  rejected(make_plan(unused), "NCL_AXI4_ID_CAPACITY", "transport.id_table_entries");
}

TEST_CASE("The ID table size parses from YAML like the direct API", "[multi-outstanding]") {
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: queued
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4, data_width: 32, max_outstanding: 4, id_table_entries: 2, len_width: 8, id_width: 2}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, max_outstanding: 2, len_width: 8, id_width: 2}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
transport: {max_outstanding: 1, id_table_entries: 1, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
  auto parsed = config::parse(yaml);
  REQUIRE(parsed);
  const auto &d = parsed.value().definition;
  CHECK(d.initiators[0].id_table_entries == 2U);
  CHECK(d.transport->id_table_entries == 1U);
  auto api = funnel();
  api.initiators[0].max_outstanding = 4;
  api.initiators[0].id_table_entries = 2;
  api.initiators[1].max_outstanding = 2;
  api.transport->id_table_entries = 1;
  const auto artifacts = generate(d);
  REQUIRE(artifacts);
  REQUIRE(artifacts.value() == generate(api).value());
  CHECK(map_of(artifacts.value())["definition"]["outstanding"]["initiator_id_tables"] ==
        nlohmann::json({{"cpu0", 2}, {"cpu1", 1}}));
}

TEST_CASE("The Weft tag exists only with several bursts in flight", "[multi-outstanding]") {
  // A crossing FIFO carries every request and response field, the tag included.
  auto crossed = [](unsigned global, std::map<std::string, unsigned> own) {
    auto d = queued(false);
    d.transport->max_outstanding = global;
    for (auto &i : d.initiators)
      if (own.contains(i.name))
        i.max_outstanding = own.at(i.name);
    d.clock_domains.push_back({"far", "clk_far", "reset_n", "test"});
    for (auto &t : d.targets)
      t.clock_domain = "far";
    d.topology->request.nodes[0].clock_domain = "main";
    d.topology->response.nodes[0].clock_domain = "far";
    return public_artifacts(d);
  };
  // The payload bits of each crossing, by segment: the request and the response bundles.
  auto bits = [](const PublicArtifacts &a) {
    std::map<std::string, unsigned> out;
    for (const auto &cut : a.map["network"]["cdc"]["cuts"])
      out[cut["segment"]] = std::accumulate(
          cut["fields"].begin(), cut["fields"].end(), 0U,
          [](unsigned sum, const auto &f) { return sum + f["width"].template get<unsigned>(); });
    return out;
  };
  const auto single = crossed(1, {});
  CHECK(single.cut_field_widths("tag").empty());
  CHECK(crossed(2, {}).cut_field_widths("tag") == std::set<unsigned>{1});
  CHECK(crossed(1, {{"s1", 3}}).cut_field_widths("tag") == std::set<unsigned>{2});
  const auto wide = crossed(4, {{"s1", 64}});
  CHECK(wide.cut_field_widths("tag") == std::set<unsigned>{6});
  // Each control bundle grows by the tag, which precedes id (contract 12).
  const auto narrow = bits(single);
  REQUIRE(narrow.size() == 4);
  REQUIRE(narrow.contains("req/req_node0>t0"));
  REQUIRE(narrow.contains("rsp/rsp_node0>s0"));
  for (const auto &[segment, width] : bits(wide)) {
    INFO(segment);
    CHECK(width == narrow.at(segment) + 6);
  }
  BurstProfile profile{2, 8, true, 32};
  profile.tag_bits = 3;
  for (const auto &fields :
       {burst_request_fields(32, profile, 2), burst_response_fields(32, profile, 2)}) {
    const auto tag = std::ranges::find(fields, "tag", &std::pair<std::string, unsigned>::first);
    REQUIRE(tag != fields.end());
    CHECK(tag->second == 3);
    CHECK((tag + 1)->first == "id");
  }
  profile.tag_bits = 0;
  CHECK(std::ranges::none_of(burst_request_fields(32, profile, 2),
                             [](const auto &f) { return f.first == "tag"; }));
}

namespace {
// A standalone tracked initiator: four bursts, two AxID bits, two
// destinations, misses to the first target.
struct Tracked {
  NodeHardware n;
  std::size_t state(const std::string &key) const {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  }
  std::size_t port(const std::string &name) const {
    return static_cast<std::size_t>(std::ranges::find(n.ir.ports, name, &Port::name) -
                                    n.ir.ports.begin());
  }
  std::size_t driven(const std::string &name) const {
    const auto at = port(name);
    return std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  }
};
Tracked tracked(unsigned id_bits = 2) {
  Axi4InitiatorProfile p;
  p.name = "q";
  p.id_bits = id_bits;
  p.source_bits = 2;
  p.transport_address_bits = 32;
  p.address_bits = 32;
  p.max_outstanding = 4;
  p.tag_bits = 3;
  p.destinations = {{0x10000, 0x10000}, {0x20000, 0x10000}};
  p.miss_destination = 0;
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  return {lowered.value()};
}
} // namespace

TEST_CASE("The tracked initiator issues per AXI ID and destination", "[multi-outstanding]") {
  const auto t = tracked();
  const auto &n = t.n;
  for (const auto *prefix : {"wr", "rr"}) {
    const std::string p(prefix);
    const auto issue = n.bindings.at(p + ".issue").index;
    // A burst of ID 1 to the second target (address 0x20000).
    auto words = [&](std::uint64_t count0, std::uint64_t id0, std::uint64_t dest0,
                     std::uint64_t count1) {
      return std::map<std::size_t, std::uint64_t>{
          {t.state(p + ".state"), 1},         {t.state(p + ".desc.addr"), 0x20000},
          {t.state(p + ".desc.burst"), 1},    {t.state(p + ".desc.id"), 1},
          {t.state(p + ".i0.count"), count0}, {t.state(p + ".i0.id"), id0},
          {t.state(p + ".i0.dest"), dest0},   {t.state(p + ".i1.count"), count1},
          {t.state(p + ".i1.id"), 3}};
    };
    CHECK(evaluate_cone(n.ir, issue, words(0, 0, 0, 0)) == 1U); // idle ID, free entry
    CHECK(evaluate_cone(n.ir, issue, words(2, 1, 1, 0)) == 1U); // bound to its destination
    CHECK(evaluate_cone(n.ir, issue, words(2, 1, 0, 0)) == 0U); // bound elsewhere: waits
    CHECK(evaluate_cone(n.ir, issue, words(2, 2, 0, 0)) == 1U); // another ID elsewhere
    CHECK(evaluate_cone(n.ir, issue, words(1, 1, 1, 1)) == 1U); // bound here
    // A burst bound elsewhere shows as waiting to the request merger (with its W beat).
    const auto waiting = t.driven(p == "wr" ? "nl_hint_write_waiting" : "nl_hint_read_waiting");
    CHECK(evaluate_cone(n.ir, waiting, words(2, 1, 0, 0),
                        {{t.port("nl_reset_n"), 1}, {t.port("nl_axi_wvalid"), 1}}) == 1U);
    CHECK(evaluate_cone(n.ir, waiting, words(2, 1, 1, 0),
                        {{t.port("nl_reset_n"), 1}, {t.port("nl_axi_wvalid"), 1}}) == 0U);
    // A burst at the port of a fresh engine with every slot taken shows as waiting too: the merger
    // chooses it whatever the engine holds.
    std::map<std::size_t, std::uint64_t> taken;
    for (unsigned k = 0; k < 4; ++k)
      taken[t.state(p + ".s" + std::to_string(k) + ".valid")] = 1;
    const std::map<std::size_t, std::uint64_t> at_port{
        {t.port("nl_reset_n"), 1},
        {t.port("nl_axi_wvalid"), 1},
        {t.port(p == "wr" ? "nl_axi_awvalid" : "nl_axi_arvalid"), 1}};
    CHECK(evaluate_cone(n.ir, waiting, taken, at_port) == 1U);
    CHECK(evaluate_cone(n.ir, waiting, {}, at_port) == 0U);
  }
  // The request tag is the burst's slot, zero-extended to the field.
  CHECK(evaluate_cone(n.ir, t.driven("nl_rreq_tag"),
                      {{t.state("rr.tag"), 3}, {t.state("rr.state"), 1}}) == 3U);
  CHECK(evaluate_cone(n.ir, t.driven("nl_wreq_tag"),
                      {{t.state("wr.tag"), 2}, {t.state("wr.state"), 1}}) == 2U);
  // BID and RID come from the staged response's own ID, which the response packet carried back.
  for (const std::uint64_t id : {0U, 1U, 2U, 3U}) {
    std::map<std::size_t, std::uint64_t> b;
    b[t.state("wr.rsp_id")] = id;
    b[t.state("wr.b_full")] = 1;
    CHECK(evaluate_cone(n.ir, t.driven("nl_axi_bid"), b) == id);
    std::map<std::size_t, std::uint64_t> r;
    r[t.state("rr.rsp_id")] = id;
    r[t.state("rr.rsp_full")] = 1;
    CHECK(evaluate_cone(n.ir, t.driven("nl_axi_rid"), r) == id);
    // A local completion waiting in the issue stage answers with its descriptor's ID.
    std::map<std::size_t, std::uint64_t> local;
    local[t.state("rr.desc.id")] = id;
    local[t.state("rr.state")] = 3;
    CHECK(evaluate_cone(n.ir, t.driven("nl_axi_rid"), local) == id);
  }
  // The response staging takes a beat whatever the tables hold.
  std::map<std::size_t, std::uint64_t> busy{{t.state("rr.state"), 3}};
  for (unsigned k = 0; k < 4; ++k)
    busy[t.state("rr.s" + std::to_string(k) + ".valid")] = 1;
  CHECK(evaluate_cone(n.ir, t.driven("nl_rrsp_ready"), busy, {{t.port("nl_reset_n"), 1}}) == 1U);
  CHECK(evaluate_cone(n.ir, t.driven("nl_wrsp_ready"), {{t.state("wr.state"), 2}},
                      {{t.port("nl_reset_n"), 1}}) == 1U);
  // No address handshake without a free slot.
  auto full = busy;
  full[t.state("rr.state")] = 0;
  CHECK(evaluate_cone(n.ir, t.driven("nl_axi_arready"), full, {{t.port("nl_reset_n"), 1}}) == 0U);
  full[t.state("rr.s2.valid")] = 0;
  CHECK(evaluate_cone(n.ir, t.driven("nl_axi_arready"), full, {{t.port("nl_reset_n"), 1}}) == 1U);
}

TEST_CASE("The tracked initiator takes addresses on registers and B through one register",
          "[multi-outstanding]") {
  const auto t = tracked();
  const auto &n = t.n;
  // Every state word is given and only the common reset among the ports: a
  // known result in strict mode proves the cone reads no other port.
  std::map<std::size_t, std::uint64_t> words;
  for (std::size_t k = 0; k < n.ir.states.size(); ++k)
    words[k] = 0;
  const std::map<std::size_t, std::uint64_t> reset{{t.port("nl_reset_n"), 1}};
  for (const auto *prefix : {"wr", "rr"}) {
    const std::string p(prefix);
    const auto ready = t.driven(p == "wr" ? "nl_axi_awready" : "nl_axi_arready");
    // An idle engine with a free slot takes the next address; a burst in service holds it.
    CHECK(evaluate_cone(n.ir, ready, words, reset, {}, true) == 1U);
    auto busy = words;
    busy[t.state(p + ".state")] = 1;
    CHECK(evaluate_cone(n.ir, ready, busy, reset, {}, true) == 0U);
  }
  // The B register takes a response while it is empty or its B leaves in this cycle (BREADY).
  auto staged = words;
  auto held = reset;
  held[t.port("nl_axi_bready")] = 0;
  CHECK(evaluate_cone(n.ir, t.driven("nl_wrsp_ready"), staged, held, {}, true) == 1U);
  staged[t.state("wr.b_full")] = 1;
  CHECK(evaluate_cone(n.ir, t.driven("nl_wrsp_ready"), staged, held, {}, true) == 0U);
  auto leaving = held;
  leaving[t.port("nl_axi_bready")] = 1;
  CHECK(evaluate_cone(n.ir, t.driven("nl_wrsp_ready"), staged, leaving, {}, true) == 1U);
}

TEST_CASE("Every target engine returns the request tag", "[multi-outstanding]") {
  auto echoes = [](const NodeHardware &n, const std::string &face, const std::string &latch,
                   std::map<std::size_t, std::uint64_t> words = {}) {
    const auto at = static_cast<std::size_t>(
        std::ranges::find(n.ir.ports, "nl_" + face + "_tag", &Port::name) - n.ir.ports.begin());
    REQUIRE(at < n.ir.ports.size());
    const auto driver =
        std::ranges::find_if(n.ir.drivers, [&](const auto &d) { return d.port.index == at; });
    REQUIRE(driver != n.ir.drivers.end());
    words[n.ir.values.at(n.bindings.at(latch).index).state.index] = 5;
    CHECK(evaluate_cone(n.ir, driver->value.index, words) == 5U);
  };
  Axi4OutletProfile outlet{
      "t", 32, 2, 8, 2, true, std::vector<nocloom::AddressMapping>{{0x10000, 0x10000}}};
  outlet.tag_bits = 3;
  auto single = lower_axi4_outlet(outlet);
  REQUIRE(single);
  echoes(single.value(), "wrsp", "w.descriptor.tag");
  echoes(single.value(), "rrsp", "r.slot0.tag");
  outlet.max_outstanding = 4;
  outlet.id_sources = {0, 1};
  auto multi = lower_axi4_outlet(outlet);
  REQUIRE(multi);
  echoes(multi.value(), "wrsp", "w.rsp.tag");
  // The read queue head's tag (head pointer zero selects slot 0).
  echoes(multi.value(), "rrsp", "r.slot0.tag");
  AhbOutletProfile ahb{"a", 2, 8, 2, std::vector<nocloom::AddressMapping>{{0x10000, 0x1000}},
                       32,  32};
  ahb.tag_bits = 3;
  auto dedicated = lower_ahb_outlet(ahb);
  REQUIRE(dedicated);
  // Queued requests may already carry other tags while these responses wait (a waiting B is
  // the stored one).
  const auto &ah = dedicated.value();
  auto state = [&](const std::string &name) {
    return ah.ir.values.at(ah.bindings.at(name).index).state.index;
  };
  echoes(ah, "wrsp", "w.response.tag",
         {{state("w.response_full"), 1},
          {state("w.context0.descriptor.tag"), 2},
          {state("w.context1.descriptor.tag"), 3},
          {state("w.context2.descriptor.tag"), 4}});
  echoes(ah, "rrsp", "r.metadata0.tag",
         {{state("r.descriptor.tag"), 2}, {state("r.metadata1.tag"), 3}});
}

TEST_CASE("A tracked source composes with a converted single-burst source", "[multi-outstanding]") {
  // The converted source shares one burst slot across reads and writes and passes the tag.
  auto d = funnel();
  d.initiators[1].data_width = 128;
  d.initiators[0].max_outstanding = 4;
  d.initiators[0].data_width = 32;
  auto artifacts = generate(d);
  for (const auto &diagnostic : artifacts.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(artifacts);
}

TEST_CASE("Several bursts per source combine with per-target data widths", "[multi-outstanding]") {
  // A source attaches at its own width; the merge in front of each AXI4
  // target carries the tag. Only a clamp to a narrower target converts it.
  auto d = funnel();
  d.targets[1].data_width = 128;
  d.initiators[0].max_outstanding = 4;
  auto artifacts = generate(d);
  for (const auto &diagnostic : artifacts.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  CHECK(map["definition"]["outstanding"]["initiators"]["cpu0"] == 4);
  CHECK(map["definition"]["target_data_widths"]["bank1"] == 128);
  // A wide source that reaches only the wide target is not clamped.
  auto wide = d;
  wide.initiators[0].data_width = 128;
  wide.connectivity =
      nocloom::Connectivity{.pairs = {{"cpu0", "bank1"}, {"cpu1", "bank0"}, {"cpu1", "bank1"}}};
  REQUIRE(generate(wide));
  // Reaching the 32-bit target clamps its bursts.
  auto clamp = d;
  clamp.initiators[0].data_width = 128;
  REQUIRE(make_plan(clamp));
  REQUIRE(generate(clamp));
  // Each accepted target packet retains its own merge context.
  auto target = d;
  target.targets[1].max_outstanding = 2;
  auto target_plan = make_plan(target);
  REQUIRE(target_plan);
  auto merged = generate(target);
  REQUIRE(merged);
  CHECK(map_of(merged.value())["definition"]["contract"]["target_outstanding"].contains(
      "merge_contexts"));
  // The merge: the read descriptor's tag goes down with the request and back
  // with every merged beat; without a tag the faces keep their fields.
  Axi4UpsizeProfile up;
  up.name = "t";
  up.id_bits = 2;
  up.source_bits = 2;
  up.data_bits = 128;
  up.tag_bits = 3;
  auto merge = lower_axi4_upsize(up);
  REQUIRE(merge);
  auto cone = [&](const std::string &port, const std::map<std::size_t, std::uint64_t> &words) {
    const auto &n = merge.value();
    const auto at = static_cast<std::size_t>(std::ranges::find(n.ir.ports, port, &Port::name) -
                                             n.ir.ports.begin());
    REQUIRE(at < n.ir.ports.size());
    const auto driver =
        std::ranges::find_if(n.ir.drivers, [&](const auto &x) { return x.port.index == at; });
    REQUIRE(driver != n.ir.drivers.end());
    return evaluate_cone(n.ir, driver->value.index, words);
  };
  auto state = [&](const char *name) {
    return merge.value().ir.values.at(merge.value().bindings.at(name).index).state.index;
  };
  CHECK(cone("nl_down_rreq_tag", {{state("r.descriptor.tag"), 5}}) == 5U);
  CHECK(cone("nl_up_rrsp_tag", {{state("r.descriptor.tag"), 5}, {state("r.mode"), 1}}) == 5U);
  up.tag_bits = 0;
  auto plain = lower_axi4_upsize(up);
  REQUIRE(plain);
  CHECK(std::ranges::none_of(plain.value().ir.ports,
                             [](const auto &port) { return port.name.ends_with("_tag"); }));
  up.tag_bits = 7;
  REQUIRE_FALSE(lower_axi4_upsize(up));
}

TEST_CASE("Several bursts per source combine with exclusive support", "[multi-outstanding]") {
  // The monitors key on the owner flags at the target; the source tracks
  // exclusive bursts like any other (exclusive.cpp checks the admission).
  for (const auto mode : {ExclusiveAccess::Network, ExclusiveAccess::Target}) {
    auto d = funnel();
    d.initiators[0].max_outstanding = 2;
    d.targets[0].exclusive = mode;
    owner_flag(d, d.targets[0]);
    auto artifacts = generate(d);
    REQUIRE(artifacts);
    const auto map = map_of(artifacts.value());
    CHECK(map["definition"]["outstanding"]["initiators"]["cpu0"] == 2);
    CHECK(map["definition"]["exclusive_targets"].contains("bank0"));
    CHECK(map["definition"]["contract"]["ordering"]["exclusive"] ==
          "monitors_key_on_exclusive_id_flags_exclusive_bursts_follow_the_per_ID_order");
    CHECK_FALSE(map["definition"]["contract"]["ordering"].contains("crossings"));
    // Packet splitting serializes only bursts needing shared reassembly.
    REQUIRE(generate(d));
  }
  // A network monitor keeps several bursts in flight at an AXI4 target.
  auto d = funnel();
  d.initiators[0].max_outstanding = 2;
  d.targets[0].exclusive = ExclusiveAccess::Network;
  owner_flag(d, d.targets[0]);
  d.targets[0].max_outstanding = 2;
  REQUIRE(generate(d));
}

TEST_CASE("Clocked graphs carry the response tag through every crossing", "[multi-outstanding]") {
  // Sources and the request switch in one domain, targets and the response
  // switch in another: every request and response segment crosses once.
  auto d = near_far(queued(false));
  d.targets[0].max_outstanding = 2;
  const auto a = public_artifacts(d);
  CHECK(a.map["definition"]["outstanding"]["initiators"].size() == 2);
  CHECK(a.map["definition"]["outstanding"]["targets"]["t0"] == 2);
  const auto &cuts = a.map["network"]["cdc"]["cuts"];
  REQUIRE(cuts.size() == 4);
  for (const auto &c : cuts)
    CHECK(std::ranges::count_if(c["fields"], [](const auto &f) {
            return f["name"] == "tag" && f["width"] == 2;
          }) == 1);
  // Every resource, the slot tables and the multi-burst outlet included, has an owning domain.
  for (const auto &r : a.map["network"]["resources"])
    CHECK(r.contains("clock_domain"));
  CHECK((*a.resource("source/s0/read"))["clock_domain"] == "near");
  CHECK((*a.resource("burst_service/t0/read"))["clock_domain"] == "far");
}

TEST_CASE("The map echoes transport.id_table_entries only when written", "[multi-outstanding]") {
  auto d = funnel();
  d.initiators[0].max_outstanding = 4;
  CHECK_FALSE(map_of(generate(d).value())["definition"]["transport"].contains("id_table_entries"));
  d.transport->id_table_entries = 2;
  CHECK(map_of(generate(d).value())["definition"]["transport"]["id_table_entries"] == 2);
}

TEST_CASE("ID width extends values without extending outstanding tables", "[multi-outstanding]") {
  auto d = funnel();
  d.initiators[0].id_width = 16;
  d.initiators[0].max_outstanding = 4;
  d.initiators[0].id_table_entries = 2;
  d.initiators[1].id_width = 9;
  d.targets[0].max_outstanding = 4;
  auto artifacts = generate(d);
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  CHECK(port_width(map, "nl_i_cpu0_awid") == 16);
  CHECK(port_width(map, "nl_i_cpu1_arid") == 9);
  CHECK(port_width(map, "nl_t_bank0_awid") == 17);
  CHECK(port_width(map, "nl_t_bank0_bid") == 17);
  for (unsigned bits : {8U, 16U}) {
    Axi4InitiatorProfile p;
    p.name = "id_table";
    p.id_bits = bits;
    p.max_outstanding = 4;
    p.destinations = {{0, 4096}};
    p.miss_destination = 0;
    const auto h = lower_axi4_initiator(p);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    for (const auto *direction : {"wr", "rr"}) {
      CHECK(h.value().bindings.contains(std::string(direction) + ".i3.id"));
      CHECK_FALSE(h.value().bindings.contains(std::string(direction) + ".i4.id"));
      CHECK(h.value().bindings.contains(std::string(direction) + ".s3.valid"));
      CHECK_FALSE(h.value().bindings.contains(std::string(direction) + ".s4.valid"));
      CHECK(h.value()
                .ir.values.at(h.value().bindings.at(std::string(direction) + ".i1.id").index)
                .width == bits);
    }
  }
}

TEST_CASE("Extended ID matching keeps high-bit ordering keys distinct",
          "[multi-outstanding][id16-match]") {
  const auto t = tracked(16);
  for (const auto *prefix : {"wr", "rr"}) {
    const std::string p(prefix);
    const auto hit = t.n.bindings.at(p + ".id_hit").index;
    auto states = std::map<std::size_t, std::uint64_t>{{t.state(p + ".state"), 1},
                                                       {t.state(p + ".desc.id"), 0x80ff},
                                                       {t.state(p + ".i0.count"), 1},
                                                       {t.state(p + ".i0.id"), 0x00ff},
                                                       {t.state(p + ".i1.count"), 0}};
    INFO("ID_HIGH_KEY_FRESH " << prefix);
    CHECK(evaluate_cone(t.n.ir, hit, states) == 0U);
    states[t.state(p + ".i0.id")] = 0x80ff;
    CHECK(evaluate_cone(t.n.ir, hit, states) == 1U);
    states[t.state(p + ".i0.id")] = 0xffff;
    CHECK(evaluate_cone(t.n.ir, hit, states) == 0U);
  }
}

TEST_CASE("Target source indices extend a sixteen-bit ID to twenty-two bits",
          "[multi-outstanding][id16-match]") {
  Axi4OutletProfile p;
  p.name = "id22";
  p.id_bits = 16;
  p.source_bits = 7;
  p.max_outstanding = 4;
  for (unsigned source = 0; source < 64; ++source)
    p.id_sources.push_back(source);
  const auto h = lower_axi4_outlet(p);
  REQUIRE(h);
  REQUIRE(verify(h.value().ir));
  unsigned id_ports = 0;
  for (const auto &port : h.value().ir.ports)
    if (port.name == "nl_axi_awid" || port.name == "nl_axi_arid" || port.name == "nl_axi_bid" ||
        port.name == "nl_axi_rid") {
      CHECK(port.width == 22);
      ++id_ports;
    }
  CHECK(id_ports == 4);
  for (const auto *direction : {"w", "r"}) {
    CHECK(h.value().bindings.contains(std::string(direction) + ".slot3.id"));
    CHECK_FALSE(h.value().bindings.contains(std::string(direction) + ".slot4.id"));
  }
}

TEST_CASE("Target merge capacities share one plan and state budget", "[upsize-context]") {
  auto d = queued(false);
  d.transport->max_outstanding = 1;
  d.targets[0].data_width = 128;
  d.targets[0].max_outstanding = 4;
  const auto plain = public_artifacts(d);
  CHECK(plain.map["definition"]["outstanding"]["targets"]["t0"] == 4);
  CHECK(plain.map["definition"]["contract"]["target_outstanding"].contains("merge_contexts"));
  // A 64-bit source merges into the 128-bit target and is clamped to the 32-bit one.
  auto equal_target = d;
  equal_target.initiators[1].data_width = 64;
  equal_target.targets[1].max_outstanding = 4;
  const auto equal = public_artifacts(equal_target);
  CHECK(equal.map["definition"]["outstanding"]["targets"]["t1"] == 4);
  CHECK(equal.map["definition"]["endpoint_data_widths"]["initiators"]["s1"] == 64);
  for (unsigned bad : {0U, 17U}) {
    auto invalid = d;
    invalid.targets[0].max_outstanding = bad;
    CHECK(rejected_with(make_plan(invalid), "NCL_AXI4_CAPACITY"));
  }
  // Clock placement keeps the outlets' merge budgets.
  const auto clocked = public_artifacts(near_far(d));
  for (const auto *resource : {"burst_service/t0/write", "burst_service/t0/read",
                               "burst_service/t1/write", "burst_service/t1/read"})
    CHECK((*clocked.resource(resource))["payload_bits"] ==
          (*plain.resource(resource))["payload_bits"]);
}

TEST_CASE("A one-burst source staged B carries its WLAST poison", "[multi-outstanding]") {
  Axi4InitiatorProfile p;
  p.name = "q";
  p.id_bits = 2;
  p.source_bits = 2;
  p.transport_address_bits = 32;
  p.address_bits = 32;
  p.max_outstanding = 1;
  p.port_pipes = true;
  p.destinations = {{0x10000, 0x10000}};
  p.miss_destination = 0;
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  const Tracked t{lowered.value()};
  const auto next = t.n.ir.states.at(t.state("wr.bresp")).next.index;
  const auto staged = [&](std::uint64_t poisoned) {
    return evaluate_cone(t.n.ir, next,
                         {{t.state("wr.state"), 2}, {t.state("wr.poisoned"), poisoned}},
                         {{t.port("nl_reset_n"), 1},
                          {t.port("nl_wrsp_valid"), 1},
                          {t.port("nl_wrsp_first"), 1},
                          {t.port("nl_wrsp_last"), 1},
                          {t.port("nl_axi_bready"), 1}});
  };
  CHECK(staged(0) == 0U);
  CHECK(staged(1) == 2U);
}

TEST_CASE("A committed upgrade drains before the next selection", "[multi-outstanding]") {
  Builder b;
  const auto clock = b.input("clk", 1, "domain:main", "clock");
  const auto reset = b.input("reset", 1, "domain:main", "reset_n");
  const auto zero = b.constant(1, 0), one = b.constant(1, 1);
  const std::vector<ValueId> live{one}, routes{zero}, finished{zero};
  const auto u =
      lower_upgrade_ledger(b, clock, reset, zero, one, zero, one, 0, live, routes, finished);
  std::map<std::size_t, std::uint64_t> words;
  for (std::size_t i = 0; i < b.ir.states.size(); ++i)
    words[i] = 0;
  words[b.ir.values[u.valid.index].state.index] = 1;
  words[b.ir.values[u.head_sent[0].index].state.index] = 1;
  words[b.ir.values[u.wanted[0].index].state.index] = 7;
  CHECK(evaluate_cone(b.ir, u.load.index, words) == 0U);
  CHECK(evaluate_cone(b.ir, u.sent.index, words) == 1U);
  const auto next = b.ir.states[b.ir.values[u.valid.index].state.index].next;
  CHECK(evaluate_cone(b.ir, next.index, words) == 0U);
}
