// AXI4-Lite sources with shared read/write transaction capacity: public
// definition, YAML, validation and map contract; the Lite initiator's single
// ordering key; the deadlock model's order-head rows for a Lite source.
// Transaction behavior is checked by run_lite_outstanding.py.
#include "../endpoint_ports.hpp"
#include "core/axi4_initiator.hpp"
#include "core/ir_cone.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>

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
// cpu0 is an AXI4-Lite source, cpu1 a full AXI4 source with two AxID bits.
Definition lite_funnel(unsigned capacity) {
  Definition d;
  d.schema_version = 10;
  d.connectivity = nocloom::Connectivity{.all = true};
  d.name = "litequeue";
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
  set_protocol(d.initiators[0], Protocol::Axi4Lite);
  for (auto &t : d.targets) {
    t.protocol = Protocol::Axi4;
    t.data_width = 32;
  }
  d.transport = TransportOptions{.max_outstanding = capacity, .transport_word_bytes = 4};
  endpoint_ports(d, 2, 8);
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
bool has_port(const nlohmann::json &map, const std::string &name) {
  return std::ranges::any_of(map["ports"], [&](const auto &port) { return port["name"] == name; });
}
struct Lowered {
  NodeHardware n;
  std::size_t state(const std::string &key) const {
    return n.ir.values.at(n.bindings.at(key).index).state.index;
  }
  bool port(const std::string &name) const {
    return std::ranges::find(n.ir.ports, name, &Port::name) != n.ir.ports.end();
  }
  std::size_t next(const std::string &key) const { return n.ir.states.at(state(key)).next.index; }
};
// A standalone Lite initiator with four transactions per direction, two
// destinations and local misses (admission windows).
Lowered lite_initiator() {
  Axi4InitiatorProfile p;
  p.name = "q";
  p.lite = true;
  p.id_bits = 2;
  p.source_bits = 2;
  p.transport_address_bits = 32;
  p.address_bits = 32;
  p.max_outstanding = 4;
  p.tag_bits = 2;
  p.destinations = {{0x10000, 0x10000}, {0x20000, 0x10000}};
  p.admitted_windows = p.destinations;
  auto lowered = lower_axi4_initiator(p);
  REQUIRE(lowered);
  return {lowered.value()};
}
} // namespace

TEST_CASE("AXI4-Lite sources keep several transactions in flight", "[lite-outstanding]") {
  const auto artifacts = generate(lite_funnel(4));
  REQUIRE(artifacts);
  const auto map = map_of(artifacts.value());
  const auto &definition = map["definition"];
  CHECK(definition["outstanding"]["initiators"] == nlohmann::json({{"cpu0", 4}, {"cpu1", 4}}));
  // Lite defaults to its unconstrained two direction keys and adds no table; the AXI4 source's
  // table holds its four transactions.
  CHECK(definition["outstanding"]["initiator_id_tables"] == nlohmann::json({{"cpu1", 4}}));
  CHECK(definition["contract"]["lite_ingress"]["outstanding"] ==
        "one_ordering_key_per_direction_same_destination_pipelines_another_destination_waits_"
        "until_every_earlier_transaction_completes_responses_in_issue_order");
  for (const auto *signal : {"awid", "bid", "arid", "rid", "rlast", "wlast", "awlen", "arlen"})
    CHECK_FALSE(has_port(map, std::string("nl_i_cpu0_") + signal));
  CHECK(has_port(map, "nl_i_cpu1_rid"));
  // Its own value overrides the global one; one written explicitly keeps the
  // single-transaction bytes and adds no outstanding contract.
  auto own = lite_funnel(1);
  own.initiators[0].max_outstanding = 8;
  const auto mixed = generate(own);
  REQUIRE(mixed);
  CHECK(map_of(mixed.value())["definition"]["outstanding"]["initiators"] ==
        nlohmann::json({{"cpu0", 8}, {"cpu1", 1}}));
  auto explicit_one = lite_funnel(1);
  explicit_one.initiators[0].max_outstanding = 1;
  const auto one = generate(explicit_one);
  REQUIRE(one);
  CHECK(one.value() == generate(lite_funnel(1)).value());
  const auto plain = map_of(one.value())["definition"];
  CHECK_FALSE(plain.contains("outstanding"));
  CHECK_FALSE(plain["contract"]["lite_ingress"].contains("outstanding"));
  // YAML parses to the same definition and bytes.
  const std::string yaml = R"(schema_version: 10
connectivity: all
name: litequeue
address_width: 32
arbitration: {policy: round_robin}
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu0, clock_domain: main, protocol: axi4_lite, data_width: 32}
  - {name: cpu1, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}
targets:
  - {name: bank0, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x0', size: '0x1000'}], len_width: 8, attributes: amba}
  - {name: bank1, clock_domain: main, protocol: axi4, data_width: 32, mappings: [{base: '0x1000', size: '0x1000'}], len_width: 8, attributes: amba}
transport: {max_outstanding: 4, transport_word_bytes: 4}
output: {backend: verilog, language: verilog_2001}
)";
  const auto parsed = config::parse(yaml);
  REQUIRE(parsed);
  CHECK(generate(parsed.value().definition).value() == artifacts.value());
}

TEST_CASE("AXI4-Lite outstanding transactions reject what does not apply", "[lite-outstanding]") {
  {
    // A Lite table has one key per direction: one entry makes reads and writes mutually exclusive,
    // two is the unconstrained bound, and larger tables do not exist.
    for (const unsigned entries : {0U, 3U}) {
      auto d = lite_funnel(4);
      d.initiators[0].id_table_entries = entries;
      rejected(make_plan(d), "NCL_AXI4_ID_CAPACITY", "initiators[0].id_table_entries");
    }
    for (const unsigned entries : {1U, 2U}) {
      auto d = lite_funnel(4);
      d.initiators[0].id_table_entries = entries;
      const auto artifacts = generate(d);
      REQUIRE(artifacts);
      const auto ids =
          map_of(artifacts.value())["definition"]["outstanding"]["initiator_id_tables"];
      if (entries == 1)
        CHECK(ids == nlohmann::json({{"cpu0", 1}, {"cpu1", 4}}));
      else {
        CHECK(ids == nlohmann::json({{"cpu1", 4}}));
        auto unconstrained = d;
        unconstrained.initiators[0].id_table_entries.reset();
        CHECK(generate(unconstrained).value() == artifacts.value());
      }
    }
    auto one = lite_funnel(1);
    one.initiators[0].id_table_entries = 1;
    CHECK(generate(one).value() == generate(lite_funnel(1)).value());
  }
  {
    // The global table size reaches Lite and full AXI sources above one.
    auto d = lite_funnel(1);
    d.initiators[0].max_outstanding = 4;
    d.transport->id_table_entries = 1;
    const auto lite = generate(d);
    REQUIRE(lite);
    CHECK(map_of(lite.value())["definition"]["outstanding"]["initiator_id_tables"] ==
          nlohmann::json({{"cpu0", 1}}));
    d.initiators[1].max_outstanding = 4;
    const auto both = generate(d);
    REQUIRE(both);
    CHECK(map_of(both.value())["definition"]["outstanding"]["initiator_id_tables"] ==
          nlohmann::json({{"cpu0", 1}, {"cpu1", 1}}));
    d.transport->id_table_entries = 2;
    REQUIRE(make_plan(d));
  }
  {
    // A Lite source bypasses the packet cutter: bounded packets keep its
    // capacity alongside a full AXI4 source with bounded packets.
    auto d = lite_funnel(1);
    d.initiators[0].max_outstanding = 2;
    REQUIRE(generate(d));
    d.initiators[1].max_outstanding = 2;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
  {
    // A Lite face has no AxLOCK: exclusive targets see its plain accesses.
    for (const auto mode : {ExclusiveAccess::Network, ExclusiveAccess::Target}) {
      auto d = lite_funnel(1);
      d.initiators[0].max_outstanding = 2;
      d.targets[1].exclusive = mode;
      owner_flag(d, d.targets[1]);
      REQUIRE(generate(d));
    }
  }
  {
    // A multi-burst AXI4 target reached by the Lite source, each with
    // several transfers in flight, converted to the target width or not.
    auto d = lite_funnel(4);
    d.targets[0].max_outstanding = 4;
    const auto artifacts = generate(d);
    REQUIRE(artifacts);
    CHECK(map_of(artifacts.value())["definition"]["target_ids"]["source_paths"] ==
          nlohmann::json({{"cpu0", {0}}, {"cpu1", {1}}}));
    d.initiators[0].data_width = 64;
    REQUIRE(generate(d));
  }
  {
    // An APB initiator on the same single-transfer engine keeps one: a global
    // capacity the Lite source inherits is its own diagnostic for the APB one.
    auto d = lite_funnel(4);
    set_protocol(d.initiators[1], Protocol::Apb4);
    const auto apb = make_plan(d);
    rejected(apb, "NCL_AXI4_CAPACITY_COMBINATION", "transport.max_outstanding");
    CHECK(std::ranges::all_of(apb.diagnostics(), [](const auto &diagnostic) {
      return diagnostic.message.find("APB initiator cpu1") != std::string::npos;
    }));
    d.initiators[1].max_outstanding = 1;
    const auto lite_with_apb = generate(d);
    REQUIRE(lite_with_apb);
    const auto shown = map_of(lite_with_apb.value())["definition"];
    CHECK(shown["outstanding"]["initiators"] == nlohmann::json({{"cpu0", 4}, {"cpu1", 1}}));
    CHECK(shown["contract"]["lite_ingress"].contains("outstanding"));
  }
  {
    auto d = lite_funnel(1);
    d.initiators[0].max_outstanding = 65;
    rejected(make_plan(d), "NCL_AXI4_CAPACITY", "initiators[0].max_outstanding");
  }
}

TEST_CASE("The Lite initiator issues under one ordering key", "[lite-outstanding]") {
  const auto t = lite_initiator();
  const auto &n = t.n;
  // One ID table entry per direction; no AXI ID, length or last ports.
  for (const auto *name : {"wr.i0.count", "rr.i0.dest", "wr.s3.valid", "rr.s3.valid", "rr.tag"})
    CHECK(n.bindings.contains(name));
  CHECK_FALSE(n.bindings.contains("wr.i1.count"));
  CHECK_FALSE(n.bindings.contains("rr.i1.count"));
  for (const auto *port :
       {"nl_axi_rid", "nl_axi_bid", "nl_axi_rlast", "nl_axi_arid", "nl_axi_wlast"})
    CHECK_FALSE(t.port(port));
  for (const auto *prefix : {"wr", "rr"}) {
    const std::string p(prefix);
    const auto issue = n.bindings.at(p + ".issue").index;
    // A transaction to the second destination (address 0x20000, index 1).
    auto words = [&](std::uint64_t count, std::uint64_t dest) {
      return std::map<std::size_t, std::uint64_t>{{t.state(p + ".state"), 1},
                                                  {t.state(p + ".desc.addr"), 0x20000},
                                                  {t.state(p + ".desc.burst"), 1},
                                                  {t.state(p + ".i0.count"), count},
                                                  {t.state(p + ".i0.dest"), dest}};
    };
    CHECK(evaluate_cone(n.ir, issue, words(0, 0)) == 1U); // nothing in flight
    CHECK(evaluate_cone(n.ir, issue, words(3, 1)) == 1U); // same destination pipelines
    CHECK(evaluate_cone(n.ir, issue, words(1, 0)) == 0U); // another destination waits
    CHECK(evaluate_cone(n.ir, issue, words(2, 2)) == 0U); // a local completion waits too
  }
  // A local write (unmapped address) with its beat captured completes only
  // once earlier writes to another destination have drained.
  auto local = [&](std::uint64_t count) {
    return evaluate_cone(
        n.ir, t.next("wr.state"),
        {{t.state("wr.state"), 1},
         {t.state("wr.desc.addr"), 0x70000},
         {t.state("wr.desc.burst"), 1},
         {t.state("wr.beat_full"), 1},
         {t.state("wr.i0.count"), count},
         {t.state("wr.i0.dest"), 0}},
        {{static_cast<std::size_t>(std::ranges::find(n.ir.ports, "nl_reset_n", &Port::name) -
                                   n.ir.ports.begin()),
          1}});
  };
  CHECK(local(1) == 1U); // waits in the issue stage
  CHECK(local(0) == 2U); // completes locally
}

TEST_CASE("Wide Lite sources compose with single-burst narrow targets", "[lite-outstanding]") {
  for (unsigned capacity : {1U, 3U, 64U}) {
    auto d = lite_funnel(capacity);
    d.initiators[0].data_width = 64;
    REQUIRE(make_plan(d));
    const auto artifacts = generate(d);
    for (const auto &diagnostic : artifacts.diagnostics())
      UNSCOPED_INFO(diagnostic.code << " " << diagnostic.message);
    REQUIRE(artifacts);
    d.targets[1].data_width = 64;
    REQUIRE(make_plan(d));
    REQUIRE(generate(d));
  }
}
