#include "core/axi4_output.hpp"
#include "core/cone_walk.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/register_bank.hpp"
#include "nocloom/config.hpp"
#include "nocloom/generation.hpp"
#include "nocloom/ipxact.hpp"
#include <catch2/catch_test_macros.hpp>
#include <catch2/generators/catch_generators.hpp>
#include <nlohmann/json.hpp>
#include <set>
#include <sstream>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
Definition definition(const std::string &ram_options = "", const std::string &extra = "") {
  const auto text =
      "schema_version: 10\nname: o\naddress_width: 32\nreset_ports: [{name: reset_n}]\n"
      "test_ports: [{name: test}]\nclock_domains: [{name: main, clock_port: clk, reset_port: "
      "reset_n, test_port: test}]\ninitiators:\n"
      "  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}\n"
      "  - {name: data, clock_domain: main, protocol: apb4, data_width: 32}\n"
      "targets:\n  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, attributes: "
      "amba, mappings: [{base: '0x0', size: '0x1000'}]" +
      ram_options +
      "}\n  - {name: csr, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, "
      "mappings: [{base: '0x100000', size: '0x100000'}]}\n" +
      extra +
      "connectivity:\n  cfg: [csr]\n  data: [ram]\noutput: {backend: verilog, language: "
      "verilog_2001}\n";
  auto parsed = config::parse(text);
  REQUIRE(parsed);
  return parsed.value().definition;
}
InternalNetwork logger(std::vector<std::string> targets = {"ram"},
                       std::vector<unsigned> ignored = {}) {
  InternalNetwork internal;
  internal.service_targets["csr"] = error_logger_bank("log", 0, {1, 32, 0, 0, 2, 0});
  internal.fault_monitors.push_back({"errors", "csr", std::move(targets), std::move(ignored)});
  return internal;
}
std::string rejection(const Result<NetworkPlan> &plan) {
  return plan ? std::string{} : plan.diagnostics().front().message;
}
// Every state of a value's combinational cone (states are its leaves).
std::set<std::size_t> cone_states(const HardwareIR &ir, ValueId root) {
  std::set<std::size_t> out;
  walk_cone(ir, root, [&](ValueId state) { out.insert(state.index); }, [](ValueId) {});
  return out;
}
} // namespace

TEST_CASE("An error logger's plan follows the route numbering and the transport's address",
          "[observer]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto &observer = plan.value().fault_monitors.at(0);
  // {initiator, target, sub-range}: data 1 beside cfg, ram 1 beside csr, its sub-range and
  // aperture.
  CHECK(observer.logger.shape.route_bits == 3);
  // One-target sources keep whole addresses, so the record's address has all 32 bits.
  CHECK(observer.logger.shape.address_bits == 32);
  // Three route bits, write, wrap, partial, three error code bits and 32 address bits.
  CHECK(error_record_layout(observer.logger).bits == 41);
  CHECK(observer.logger.serial_words == 2);
  const auto &row = observer.observed.at(0).sources.at(0);
  CHECK(row.route == 6);
  CHECK(row.miss_range == 1);
  CHECK(row.miss_mask == ~std::uint64_t{0});
  const auto service = plan.value().target("csr").service;
  REQUIRE(service);
  const auto *bank = &service->banks.at(0).plan;
  CHECK(std::ranges::any_of(bank->fields, [](const RegisterField &f) {
    return f.name == "record_route_low" && f.width == 3;
  }));
}

TEST_CASE("Unobserved shapes are rejected", "[observer]") {
  CHECK(rejection(build_network_plan(definition(), logger({"ram", "ram"}))).find("once") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(definition(), logger({"csr"}))).find("once") !=
        std::string::npos);
  CHECK(!rejection(build_network_plan(definition(", timeout: {cycles: 64}"), logger())).empty());
  // Ignored codes need error codes in the network and a known event source.
  CHECK(rejection(build_network_plan(definition("", "transport: {response_codes: uniform}\n"),
                                     logger({"ram"}, {CodeTarget})))
            .find("error codes") != std::string::npos);
  auto codes = definition();
  CHECK(build_network_plan(codes, logger({"ram"}, {CodeTarget})));
  CHECK(build_network_plan(codes, logger({"ram"}, {CodeDecode})));
  CHECK(rejection(build_network_plan(codes, logger({"ram"}, {6}))).find("target and decode") !=
        std::string::npos);
  auto two = logger();
  two.fault_monitors.push_back({"more", "csr", {"ram"}, {}});
  two.fault_monitors[1].route = 1;
  two.fault_monitors[1].base = 0x100;
  CHECK(rejection(build_network_plan(definition(), two)).find("once") != std::string::npos);
  auto other = logger();
  other.fault_monitors[0].service = "ram";
  CHECK(rejection(build_network_plan(definition(), other)).find("window") != std::string::npos);
}

namespace {
// {cfg, data} over {csr, ram, rom}: the two AXI4 targets of 2^46 bytes hold the service window.
Definition axi_definition(unsigned contexts = 2) {
  const auto bursts = std::to_string(contexts);
  const auto text =
      "schema_version: 10\nname: k\naddress_width: 48\ntransport: {response_codes: distinct, "
      "user_flags: [b, a]}\nreset_ports: [{name: reset_n}]\ntest_ports: [{name: test}]\n"
      "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]\n"
      "initiators:\n  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32, "
      "address_width: 32, user_flags: {b: 0, a: 0}}\n  - {name: data, clock_domain: main, "
      "protocol: "
      "axi4, data_width: 32, id_width: 1, len_width: 8, max_outstanding: " +
      bursts +
      ", user_width: 2, user_flags: {b: 'user[0]', a: 'user[1]'}}\ntargets:\n"
      "  - {name: csr, clock_domain: main, protocol: apb4, data_width: 32, address_width: 20, "
      "attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}\n"
      "  - {name: ram, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: "
      "8, attributes: amba, address_width: 48, max_outstanding: " +
      bursts +
      ", user: [b, a], mappings: [{base: '0x0', size: '0x400000000000'}]}\n"
      "  - {name: rom, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: "
      "8, attributes: amba, address_width: 48, user: [b, a], mappings: [{base: '0x400000000000', "
      "size: '0x400000000000'}]}\nconnectivity:\n  cfg: [csr]\n  data: [ram, rom]\n"
      "output: {backend: verilog, language: verilog_2001}\n";
  auto parsed = config::parse(text);
  REQUIRE(parsed);
  return parsed.value().definition;
}
InternalNetwork keeping() {
  InternalNetwork internal;
  internal.service_targets["csr"] = error_logger_bank("log", 0, {1, 32, 0, 0, 2, 0});
  internal.fault_monitors.push_back({"errors", "csr", {"ram", "rom"}, {}, true});
  return internal;
}
} // namespace

TEST_CASE("A keeping logger records the transport's whole address and every flag by name",
          "[observer]") {
  const auto plan = build_network_plan(axi_definition(), keeping());
  REQUIRE(plan);
  const auto &observer = plan.value().fault_monitors.at(0);
  CHECK(observer.keep_fields);
  CHECK(observer.logger.shape.user_bits == 2);
  CHECK(observer.logger.shape.address_bits == 46);
  const auto &shape = observer.logger.shape;
  CHECK(observer.logger.serial_words ==
        (shape.route_bits + 3 + shape.length_bits + 1 + 46 + 3 + 2 + 31) / 32);
  // The record's length comes from the source: a length and a lock bit.
  CHECK(plan.value().burst_record_bits == shape.length_bits + 1);
  const auto service = plan.value().target("csr").service;
  REQUIRE(service);
  const auto *bank = &service->banks.at(0).plan;
  CHECK(std::ranges::any_of(bank->fields, [](const RegisterField &f) {
    return f.name == "record_user_word0" && f.width == 2;
  }));
  CHECK(std::ranges::any_of(bank->fields, [](const RegisterField &f) {
    return f.name == "record_address_high" && f.width == 14;
  }));
  // A network without a logger carries no record field.
  const auto plain = build_network_plan(axi_definition());
  REQUIRE(plain);
  CHECK(plain.value().burst_record_bits == 0);
}

TEST_CASE("A keeping logger's filter takes user flags by name", "[observer]") {
  auto internal = keeping();
  using S = AttributeSource;
  internal.security.flags = {"s"};
  internal.security.drivers["cfg"]["s"] = {S::Zero};
  internal.security.drivers["data"]["s"] = {S::One};
  internal.fault_monitors[0].user_filter = {{"a", true}, {"b", false}};
  const auto plan = build_network_plan(axi_definition(), internal);
  REQUIRE(plan);
  CHECK(plan.value().fault_monitors.at(0).logger.user_filter ==
        std::vector<RecordFilterBit>{{1, true}, {0, false}});
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  if (!hardware)
    FAIL(hardware.diagnostics().front().message);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  // A driver above the source's AxUSER port.
  auto high = internal;
  high.security.drivers["data"]["s"] = {S::User, false, 2};
  CHECK(rejection(build_network_plan(axi_definition(), high)).find("AxUSER port") !=
        std::string::npos);
  high.security.drivers["data"]["s"] = {S::User, true, 1};
  CHECK(build_network_plan(axi_definition(), high));
}

TEST_CASE("A record has room for user flags without keeping them", "[observer]") {
  auto internal = keeping();
  internal.fault_monitors[0].keep_fields = false;
  const auto plan = build_network_plan(axi_definition(), internal);
  REQUIRE(plan);
  const auto &observer = plan.value().fault_monitors.at(0);
  CHECK(!observer.keep_fields);
  CHECK(observer.logger.shape.user_bits == 2);
  // Nothing to filter on: only a miss fills them.
  internal.fault_monitors[0].user_filter = {{"a", true}};
  CHECK(rejection(build_network_plan(axi_definition(), internal)).find("A filter names") !=
        std::string::npos);
  // The evidence for this reaches AXI4 targets only.
  auto parsed = config::parse(
      "schema_version: 10\nname: o\naddress_width: 32\ntransport: {user_flags: [f]}\n"
      "reset_ports: [{name: reset_n}]\ntest_ports: [{name: test}]\nclock_domains: [{name: main, "
      "clock_port: clk, reset_port: reset_n, test_port: test}]\ninitiators:\n  - {name: cfg, "
      "clock_domain: main, protocol: apb4, data_width: 32, user_flags: {f: 0}}\n  - {name: data, "
      "clock_domain: main, protocol: apb4, data_width: 32, user_flags: {f: 1}}\ntargets:\n  - "
      "{name: ram, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, mappings: "
      "[{base: '0x0', size: '0x1000'}]}\n  - {name: csr, clock_domain: main, protocol: apb4, "
      "data_width: 32, attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}\n"
      "connectivity:\n  cfg: [csr]\n  data: [ram]\noutput: {backend: verilog, language: "
      "verilog_2001}\n");
  REQUIRE(parsed);
  CHECK(rejection(build_network_plan(parsed.value().definition, logger())).find("Only AXI4") !=
        std::string::npos);
}

TEST_CASE("A target that keeps a record's fields has sources of its protocol", "[observer]") {
  auto apb = logger();
  apb.fault_monitors[0].keep_fields = true;
  CHECK(build_network_plan(definition(), apb));
  auto parsed = config::parse(
      "schema_version: 10\nname: o\naddress_width: 32\nreset_ports: [{name: reset_n}]\n"
      "test_ports: [{name: test}]\nclock_domains: [{name: main, clock_port: clk, reset_port: "
      "reset_n, test_port: test}]\ninitiators:\n  - {name: cfg, clock_domain: main, protocol: "
      "apb4, data_width: 32}\n  - {name: data, clock_domain: main, protocol: axi4, data_width: 32, "
      "id_width: 1, len_width: 8}\ntargets:\n  - {name: ram, clock_domain: main, protocol: apb4, "
      "data_width: 32, attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}\n  - {name: "
      "csr, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, mappings: "
      "[{base: "
      "'0x100000', size: '0x100000'}]}\nconnectivity:\n  cfg: [csr]\n  data: [ram]\noutput: "
      "{backend: verilog, language: verilog_2001}\n");
  REQUIRE(parsed);
  CHECK(rejection(build_network_plan(parsed.value().definition, apb)).find("its protocol") !=
        std::string::npos);
}

TEST_CASE("Several contexts of an observed AXI4 target hold the record's fields", "[observer]") {
  for (const unsigned contexts : {1U, 2U, 4U}) {
    const auto plan = build_network_plan(axi_definition(contexts), keeping());
    REQUIRE(plan);
    const auto hardware = lower_axi4_exchange_composition(plan.value());
    REQUIRE(hardware);
    CHECK(verify_axi4_exchange_composition(hardware.value()));
    const auto &bindings = hardware.value().bindings;
    CHECK(bindings.contains("observe/rom/address"));
    CHECK(bindings.contains("observe/ram/context1.user") == (contexts > 1));
    CHECK(bindings.contains("observe/ram/context3.user") == (contexts > 2));
    CHECK(bindings.contains("observe/ram/context4.user") == false);
    CHECK(plan.value().dependency_report.cycle_sets == 0);
  }
}

TEST_CASE("A target no initiator shares with a larger mapping stays out of its decode",
          "[observer]") {
  auto d = axi_definition();
  REQUIRE(validate(d));
  const auto layout = layout_mappings(d);
  CHECK(layout.overlaps.empty());
  CHECK(layout.mappings[1][0].first.size() == 1);
  // An initiator reaching both still decodes the smaller one, so one reaching only the larger is
  // refused.
  d.connectivity = nocloom::Connectivity{
      .pairs = {{"cfg", "csr"}, {"data", "ram"}, {"data", "rom"}, {"data", "csr"}}};
  CHECK(validate(d));
  d.connectivity->pairs[0].target = "ram";
  CHECK(!validate(d));
}

TEST_CASE("A logger's nodes stay apart from the data path", "[observer]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto &graph = plan.value().dependency;
  std::set<std::uint32_t> mine;
  for (std::uint32_t k = 0; k < graph.nodes.size(); ++k)
    if (graph.nodes[k].component == "observe/errors")
      mine.insert(k);
  CHECK(mine.size() == 3);
  std::size_t own = 0;
  for (const auto &e : graph.edges) {
    CHECK(mine.contains(e.from) == mine.contains(e.to));
    own += mine.contains(e.from) ? 1 : 0;
  }
  CHECK(own == 2);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
}

// The logger's state reaches its bank's field reads and nothing else: no ready, no valid of the
// data path takes a logger state in its combinational cone, and the data path's clock gates and
// registers read none of them.
namespace {
// The logger over {cfg, data}, with three security flags out of name order and a filter of them.
InternalNetwork secured() {
  auto internal = logger();
  using S = AttributeSource;
  internal.security.flags = {"zeta", "Alpha", "mid"};
  internal.security.drivers["cfg"] = {{"zeta", {S::Zero}}, {"Alpha", {S::Zero}}, {"mid", {S::One}}};
  internal.security.drivers["data"] = {
      {"zeta", {S::Privileged}}, {"Alpha", {S::Nonsecure, true}}, {"mid", {S::Instruction}}};
  internal.fault_monitors[0].security_filter = {{"Alpha", true}, {"zeta", false}};
  return internal;
}
} // namespace

TEST_CASE("Security flags travel in name order and fill the record's word", "[observer]") {
  const auto plain = build_network_plan(definition(), logger());
  const auto plan = build_network_plan(definition(), secured());
  REQUIRE(plain);
  REQUIRE(plan);
  CHECK(plain.value().burst_security_bits == 0);
  const auto &p = plan.value();
  CHECK(p.security_flags == std::vector<std::string>{"zeta", "Alpha", "mid"});
  CHECK(p.burst_security_bits == 3);
  const auto &data = *std::ranges::find(p.sources, "data", &SourcePlan::name);
  CHECK(data.security_flags[1] == AttributeBit{AttributeSource::Nonsecure, true});
  CHECK(data.security_flags[0] == AttributeBit{AttributeSource::Privileged});
  const auto &log = p.fault_monitors.at(0).logger;
  CHECK(log.shape.security_bits == 3);
  CHECK(log.security_filter == std::vector<RecordFilterBit>{{1, true}, {0, false}});
  const auto &bank =
      std::ranges::find(p.targets, "csr", &TargetPlan::name)->service->banks.at(0).plan;
  const auto word = std::ranges::find(bank.fields, "record_audit_flags", &RegisterField::name);
  REQUIRE(word != bank.fields.end());
  CHECK(word->width == 3);
  CHECK(word->word == 10);
  // Only with flags does a request carry them.
  CHECK(!plain.value().fault_monitors.at(0).logger.shape.security_bits);
}

TEST_CASE("Security flags take a record's serial width", "[observer]") {
  auto codes = definition();
  const auto without = build_network_plan(codes, logger());
  auto internal = logger();
  using S = AttributeSource;
  for (const auto *name : {"a", "b", "c", "d", "e", "f", "g", "h"}) {
    internal.security.flags.emplace_back(name);
    internal.security.drivers["cfg"][name] = {S::Zero};
    internal.security.drivers["data"][name] = {S::One};
  }
  const auto with = build_network_plan(codes, internal);
  REQUIRE(without);
  REQUIRE(with);
  // Optional flags add only their actual width.
  CHECK(error_record_layout(with.value().fault_monitors.at(0).logger).bits ==
        error_record_layout(without.value().fault_monitors.at(0).logger).bits + 8);
  CHECK(without.value().fault_monitors.at(0).logger.serial_words == 2);
  CHECK(with.value().fault_monitors.at(0).logger.serial_words == 2);
}

TEST_CASE("Security flags outside their scope are rejected", "[observer]") {
  using S = AttributeSource;
  const auto refused = [](const Definition &d, const InternalNetwork &internal) {
    return rejection(build_network_plan(d, internal));
  };
  auto none = secured();
  none.fault_monitors.clear();
  CHECK(refused(definition(), none).find("serve an error log") != std::string::npos);
  auto many = logger();
  for (const auto *name : {"a", "b", "c", "d", "e", "f", "g", "h", "i"}) {
    many.security.flags.emplace_back(name);
    many.security.drivers["cfg"][name] = {S::Zero};
    many.security.drivers["data"][name] = {S::Zero};
  }
  CHECK(refused(definition(), many).find("One to eight") != std::string::npos);
  auto twice = secured();
  twice.security.flags.push_back("mid");
  CHECK(refused(definition(), twice).find("One to eight") != std::string::npos);
  auto undriven = secured();
  undriven.security.drivers["data"].erase("mid");
  CHECK(refused(definition(), undriven).find("drives every") != std::string::npos);
  auto silent = secured();
  silent.security.drivers.erase("cfg");
  CHECK(refused(definition(), silent).find("drives every") != std::string::npos);
  auto stranger = secured();
  stranger.security.drivers["other"] = {};
  CHECK(refused(definition(), stranger).find("No such source") != std::string::npos);
  // No driver of a cache flag, and an APB port has no user bit.
  for (const auto kind : {S::Bufferable, S::Modifiable, S::Allocate, S::Lookup, S::User}) {
    auto driven = secured();
    driven.security.drivers["data"]["mid"] = {kind};
    CHECK(refused(definition(), driven).find("constant, an AxPROT flag") != std::string::npos);
  }
  // A filter names a flag a record keeps.
  auto unknown = secured();
  unknown.fault_monitors[0].security_filter["other"] = true;
  CHECK(refused(definition(), unknown).find("A filter names") != std::string::npos);
  // Not keeping its fields, a record has no user flags to filter.
  auto user = secured();
  user.fault_monitors[0].user_filter["a"] = true;
  CHECK(refused(definition(), user).find("A filter names") != std::string::npos);
  // Other source protocols have no driver matrix.
  auto text = [](const std::string &protocol, const std::string &options) {
    auto parsed = config::parse(
        "schema_version: 10\nname: o\naddress_width: 32\nreset_ports: [{name: reset_n}]\n"
        "test_ports: [{name: test}]\nclock_domains: [{name: main, clock_port: clk, reset_port: "
        "reset_n, test_port: test}]\ninitiators:\n"
        "  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}\n"
        "  - {name: data, clock_domain: main, protocol: " +
        protocol + ", data_width: 32" + options +
        "}\ntargets:\n  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, "
        "attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}\n  - {name: csr, "
        "clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, mappings: [{base: "
        "'0x100000', size: '0x100000'}]}\nconnectivity:\n  cfg: [csr]\n  data: [ram]\noutput: "
        "{backend: verilog, language: verilog_2001}\n");
    REQUIRE(parsed);
    return parsed.value().definition;
  };
  for (const auto &[protocol, options] :
       std::vector<std::pair<std::string, std::string>>{{"apb3", ""},
                                                        {"axi3", ", id_width: 1, len_width: 4"},
                                                        {"axi4_lite", ""},
                                                        {"ahb_v3", ""}})
    CHECK(refused(text(protocol, options), secured()).find("Only APB4 and AXI4") !=
          std::string::npos);
}

TEST_CASE("No combinational path leads from a logger into the data path", "[observer]") {
  const auto plan = build_network_plan(definition(), GENERATE(false, true) ? secured() : logger());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  const auto &h = hardware.value();
  std::set<std::size_t> logger_states;
  for (const auto &[name, id] : h.bindings)
    if (name.starts_with("observe/") && h.ir.values[id.index].op == Op::State)
      logger_states.insert(id.index);
  REQUIRE(logger_states.size() > 10);
  std::size_t checked = 0;
  for (const auto &port : h.ir.ports) {
    if (port.input || port.endpoint.starts_with("error_log:"))
      continue;
    const auto driver = std::ranges::find_if(
        h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
    REQUIRE(driver != h.ir.drivers.end());
    const auto reached = cone_states(h.ir, driver->value);
    const bool service = port.name.starts_with("nl_i_cfg_");
    for (const auto state : reached)
      if (logger_states.contains(state)) {
        INFO(port.name);
        // Only the configuration source's read data reads the logger, through the bank's registers.
        CHECK(service);
      }
    ++checked;
  }
  CHECK(checked > 10);
  // A state of the data path takes no logger state in its next value either: the records reach the
  // bank's read path and the path's response word, and stop there.
  std::set<std::size_t> allowed;
  for (const auto &[name, id] : h.bindings)
    if (h.ir.values[id.index].op == Op::State &&
        (name.starts_with("observe/") || name.starts_with("burst_service/csr")))
      allowed.insert(id.index);
  for (const auto &state : h.ir.states) {
    if (allowed.contains(state.output.index))
      continue;
    for (const auto seen : cone_states(h.ir, state.next))
      CHECK(!logger_states.contains(seen));
  }
  // Ready and valid of data traffic: one source and one target, every bit of the cone.
  for (const auto &port : h.ir.ports)
    if (port.name == "nl_i_data_pready" || port.name == "nl_t_ram_psel") {
      const auto driver = std::ranges::find_if(
          h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
      REQUIRE(driver != h.ir.drivers.end());
      for (const auto state : cone_states(h.ir, driver->value))
        CHECK(!logger_states.contains(state));
    }
}

// The same over an AXI4 data path with contexts: a logger's state reaches no ready and no valid of
// data traffic, and no state of it.
TEST_CASE("No combinational path leads from a keeping logger into an AXI4 data path",
          "[observer]") {
  const auto plan = build_network_plan(axi_definition(4), keeping());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  const auto &h = hardware.value();
  std::set<std::size_t> logger_states, allowed;
  for (const auto &[name, id] : h.bindings)
    if (h.ir.values[id.index].op == Op::State) {
      if (name.starts_with("observe/"))
        logger_states.insert(id.index);
      if (name.starts_with("observe/") || name.starts_with("burst_service/csr"))
        allowed.insert(id.index);
    }
  REQUIRE(logger_states.size() > 40);
  std::size_t ports = 0;
  for (const auto &port : h.ir.ports) {
    if (port.input || port.endpoint.starts_with("error_log:") || port.name.starts_with("nl_i_cfg_"))
      continue;
    const auto driver = std::ranges::find_if(
        h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
    REQUIRE(driver != h.ir.drivers.end());
    for (const auto state : cone_states(h.ir, driver->value)) {
      INFO(port.name);
      CHECK(!logger_states.contains(state));
    }
    ++ports;
  }
  CHECK(ports > 40);
  for (const auto &state : h.ir.states) {
    if (allowed.contains(state.output.index))
      continue;
    for (const auto seen : cone_states(h.ir, state.next))
      CHECK(!logger_states.contains(seen));
  }
}

TEST_CASE("A map describes service targets and error loggers", "[observer]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto artifacts = emit_axi4_network(plan.value());
  REQUIRE(artifacts);
  const auto &files = artifacts.value();
  const auto map =
      std::ranges::find_if(files, [](const auto &a) { return a.path.ends_with(".map.json"); });
  REQUIRE(map != files.end());
  CHECK(map->text.find("\"service\"") != std::string::npos);
  CHECK(map->text.find("\"error_logs\"") != std::string::npos);
  CHECK(map->text.find("nl_o_errors_alarm") != std::string::npos);
  CHECK(map->text.find("\"endpoint\": \"error_log:errors\"") != std::string::npos);
}

namespace {
// Two error logs on two data targets, their banks where the bench reads them.
Definition two_targets() {
  auto d = definition("", "  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, "
                          "attributes: amba, mappings: [{base: '0x10000', size: '0x1000'}]}\n");
  d.connectivity->pairs.push_back({"data", "rom"});
  return d;
}
InternalNetwork two_banks(unsigned second_route = 2, std::uint64_t second_base = 0x100) {
  auto internal = logger();
  internal.fault_monitors[0].route = 5;
  internal.fault_monitors[0].base = 0x300;
  FaultMonitorRequest other{"more", "csr", {"rom"}, {}};
  other.route = second_route;
  other.base = second_base;
  internal.fault_monitors.push_back(std::move(other));
  return internal;
}
} // namespace

TEST_CASE("Banks of a service target hang on a switch, each with its place", "[observer]") {
  const auto plan = build_network_plan(two_targets(), two_banks());
  REQUIRE(plan);
  const auto &service = plan.value().target("csr").service;
  REQUIRE(service);
  REQUIRE(service->banks.size() == 2);
  CHECK(service->banks[0].plan.route == 5);
  CHECK(service->banks[0].base == 0x300);
  CHECK(service->banks[1].plan.route == 2);
  CHECK(service->banks[1].base == 0x100);
  CHECK(service->banks[1].branch == 1);
  CHECK(plan.value().fault_monitors[1].bank == 1);
  // The switch's outputs stand in the graph, and the network stays free of cycles.
  const auto outputs = std::ranges::count_if(
      plan.value().dependency.nodes, [](const auto &n) { return n.kind == "service.output"; });
  CHECK(outputs == 2);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
}

TEST_CASE("Service bank places are rejected unless they keep apart", "[observer]") {
  CHECK(rejection(build_network_plan(two_targets(), two_banks(5))).find("share route") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(two_targets(), two_banks(2, 0x300))).find("apart") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(two_targets(), two_banks(2, 0x84))).find("apart") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(two_targets(), two_banks(2, 0x100000))).find("1 MiB") !=
        std::string::npos);
  auto twice = two_banks();
  twice.fault_monitors[1].targets = {"ram"};
  CHECK(rejection(build_network_plan(two_targets(), twice)).find("once") != std::string::npos);
  auto named = two_banks();
  named.fault_monitors[1].name = "errors";
  CHECK(rejection(build_network_plan(two_targets(), named)).find("distinct") != std::string::npos);
}

TEST_CASE("No combinational path leads from a bank's logger into data traffic beside a switch",
          "[observer]") {
  const auto plan = build_network_plan(two_targets(), two_banks());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  const auto &h = hardware.value();
  std::set<std::size_t> logger_states;
  for (const auto &[name, id] : h.bindings)
    if (name.starts_with("observe/") && h.ir.values[id.index].op == Op::State)
      logger_states.insert(id.index);
  REQUIRE(logger_states.size() > 20);
  for (const auto &port : h.ir.ports) {
    if (port.input || port.endpoint.starts_with("error_log:") || port.name.starts_with("nl_i_cfg_"))
      continue;
    const auto driver = std::ranges::find_if(
        h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
    REQUIRE(driver != h.ir.drivers.end());
    for (const auto state : cone_states(h.ir, driver->value)) {
      INFO(port.name);
      CHECK(!logger_states.contains(state));
    }
  }
}

namespace {
InternalNetwork chain_of_two(bool forward = false) {
  auto internal = logger();
  auto &first = internal.fault_monitors[0];
  first.route = 5;
  first.base = 0x300;
  first.forward = forward;
  first.chain.push_back({{}, {}, {}, 2, 0x100, false});
  return internal;
}
} // namespace

TEST_CASE("A chain's loggers have a bank each on one branch", "[observer]") {
  const auto plan = build_network_plan(definition(), chain_of_two(true));
  REQUIRE(plan);
  const auto &service = plan.value().target("csr").service;
  REQUIRE(service);
  REQUIRE(service->banks.size() == 2);
  CHECK(service->banks[0].branch == 0);
  CHECK(service->banks[1].branch == 0);
  const auto &log = plan.value().fault_monitors.at(0).logger;
  CHECK(log.pass_always);
  REQUIRE(log.chain.size() == 1);
  CHECK_FALSE(log.chain[0].pass_always);
  // No switch on one branch.
  const auto &nodes = plan.value().dependency.nodes;
  CHECK(std::ranges::none_of(nodes, [](const auto &n) { return n.kind == "service.output"; }));
  CHECK(std::ranges::count_if(nodes, [](const auto &n) { return n.kind == "logger.slot"; }) == 2);
}

TEST_CASE("A chain's wait stays inside its error logger", "[observer]") {
  const auto plan = build_network_plan(definition(), chain_of_two());
  REQUIRE(plan);
  const auto &graph = plan.value().dependency;
  std::set<std::uint32_t> mine;
  for (std::uint32_t k = 0; k < graph.nodes.size(); ++k)
    if (graph.nodes[k].component == "observe/errors")
      mine.insert(k);
  CHECK(mine.size() == 5);
  // The first logger's slot waits on the next's room, the head on the first.
  std::size_t own = 0;
  for (const auto &e : graph.edges) {
    CHECK(mine.contains(e.from) == mine.contains(e.to));
    own += mine.contains(e.from) ? 1 : 0;
  }
  CHECK(own == 4);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
}

TEST_CASE("No combinational path leads from a chain into data traffic", "[observer]") {
  const auto plan = build_network_plan(definition(), chain_of_two());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  const auto &h = hardware.value();
  std::set<std::size_t> logger_states, allowed;
  for (const auto &[name, id] : h.bindings)
    if (h.ir.values[id.index].op == Op::State) {
      if (name.starts_with("observe/"))
        logger_states.insert(id.index);
      if (name.starts_with("observe/") || name.starts_with("burst_service/csr"))
        allowed.insert(id.index);
    }
  REQUIRE(logger_states.size() > 20);
  for (const auto &port : h.ir.ports) {
    if (port.input || port.endpoint.starts_with("error_log:") || port.name.starts_with("nl_i_cfg_"))
      continue;
    const auto driver = std::ranges::find_if(
        h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
    REQUIRE(driver != h.ir.drivers.end());
    for (const auto state : cone_states(h.ir, driver->value)) {
      INFO(port.name);
      CHECK(!logger_states.contains(state));
    }
  }
  for (const auto &state : h.ir.states) {
    if (allowed.contains(state.output.index))
      continue;
    for (const auto seen : cone_states(h.ir, state.next))
      CHECK(!logger_states.contains(seen));
  }
}

TEST_CASE("A chain keeps to its shape", "[observer]") {
  auto long_chain = chain_of_two();
  for (unsigned n = 1; n < 8; ++n)
    long_chain.fault_monitors[0].chain.push_back({{}, {}, {}, 10 + n, 0x100ULL * (n + 8), false});
  CHECK(rejection(build_network_plan(definition(), long_chain)).find("eight") != std::string::npos);
  auto repeated = chain_of_two();
  repeated.fault_monitors[0].chain[0].route = 5;
  CHECK(rejection(build_network_plan(definition(), repeated)).find("share route") !=
        std::string::npos);
  auto named = chain_of_two();
  named.fault_monitors[0].chain[0].security_filter["nothing"] = true;
  CHECK(rejection(build_network_plan(definition(), named)).find("names flags") !=
        std::string::npos);
}

namespace {
// A definition of two clock domains, all its endpoints and switches in the first.
Definition two_domains(const std::string &second_reset = "reset_n", const std::string &more = "",
                       const std::string &ram_domain = "main",
                       const std::string &ram_protocol = "protocol: apb4, data_width: 32") {
  const auto text =
      "schema_version: 10\nname: o\naddress_width: 32\nreset_ports: [{name: reset_n}, {name: "
      "other_n}]\ntest_ports: [{name: test}]\nclock_domains:\n"
      "  - {name: main, clock_port: clk, reset_port: reset_n, test_port: test, period_ps: 1250}\n"
      "  - {name: slow, clock_port: clk_slow, reset_port: " +
      second_reset + ", test_port: test, period_ps: 3333}\n" + more +
      "initiators:\n  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}\n"
      "  - {name: data, clock_domain: " +
      ram_domain +
      ", protocol: apb4, data_width: 32}\ntargets:\n  - {name: ram, clock_domain: " + ram_domain +
      ", " + ram_protocol +
      ", attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}\n  - {name: csr, "
      "clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, mappings: [{base: "
      "'0x100000', size: '0x100000'}]}\nconnectivity:\n  cfg: [csr]\n  data: [ram]\ntopology:\n"
      "  request:\n    nodes:\n      - {name: cq, clock_domain: main}\n      - {name: dq, "
      "clock_domain: " +
      ram_domain +
      "}\n    links: []\n    entries:\n      - {endpoint: cfg, node: cq}\n      - {endpoint: data, "
      "node: dq}\n    exits:\n      - {endpoint: csr, node: cq}\n      - {endpoint: ram, node: "
      "dq}\n  response:\n    nodes:\n      - {name: cp, clock_domain: main}\n      - {name: dp, "
      "clock_domain: " +
      ram_domain +
      "}\n    links: []\n    entries:\n      - {endpoint: csr, node: cp}\n      - {endpoint: ram, "
      "node: dp}\n    exits:\n      - {endpoint: cfg, node: cp}\n      - {endpoint: data, node: "
      "dp}\noutput: {backend: verilog, language: verilog_2001}\n";
  auto parsed = config::parse(text);
  REQUIRE(parsed);
  return parsed.value().definition;
}
InternalNetwork crossed_logger(const std::string &domain = "slow") {
  auto internal = logger();
  internal.fault_monitors[0].clock_domain = domain;
  return internal;
}
} // namespace

TEST_CASE("A logger in a domain of its own crosses the serial path and the record", "[observer]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger());
  REQUIRE(plan);
  const auto &p = plan.value();
  CHECK(p.target("csr").service->domain == "slow");
  CHECK(p.fault_monitors[0].logger.crossed);
  std::set<std::string> cuts;
  for (const auto &cut : p.cuts) {
    cuts.insert(cut.name);
    const auto &options = std::get<AsyncClockCut>(cut.options);
    // (2 + 1) * (1250 + 3333) / 3333 rounded up, with the synchronizers of two stages.
    CHECK(options.depth.depth == 5);
    CHECK(options.count_bits == 3);
    CHECK(options.synchronizer_stages == 2);
    CHECK_FALSE(options.reset_handshake);
  }
  CHECK(cuts == std::set<std::string>{"cut/service/csr/request", "cut/service/csr/response",
                                      "cut/service/observe/errors/ram"});
  // Each crossing stands in the graph, and the graph stays free of cycles.
  std::set<std::string> nodes;
  for (const auto &n : p.dependency.nodes)
    if (n.kind == "occupancy.crossing")
      nodes.insert(n.binding);
  CHECK(nodes == cuts);
  CHECK(p.dependency_report.cycle_sets == 0);
}

TEST_CASE("A sender slower than its logger keeps the configured word width", "[observer]") {
  auto text = two_domains();
  // The periods of the two clocks turned round: the crossings' depth follows them too.
  std::ranges::find(text.clock_domains, "main", &ClockDomain::name)->period_ps = 3333;
  std::ranges::find(text.clock_domains, "slow", &ClockDomain::name)->period_ps = 1250;
  const auto plan = build_network_plan(text, crossed_logger());
  REQUIRE(plan);
  const auto &logged = plan.value().fault_monitors[0].logger;
  CHECK(error_record_word_bits(logged) == 32);
  // Without the periods the rule has nothing to go by.
  auto untimed = two_domains();
  for (auto &domain : untimed.clock_domains)
    domain.period_ps.reset();
  CHECK(rejection(build_network_plan(untimed, crossed_logger())).find("periods") !=
        std::string::npos);
}

TEST_CASE("A record's words carry the whole record", "[observer]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger());
  REQUIRE(plan);
  const auto &logged = plan.value().fault_monitors[0].logger;
  const auto bits = error_record_word_bits(logged);
  CHECK(bits == 32);
  const auto &shape = logged.shape;
  CHECK(bits * logged.serial_words >= shape.route_bits + 2 + shape.address_bits);
}

TEST_CASE("Serial crossings keep to plain clocks of one reset", "[observer]") {
  const auto refused = [](const Definition &d, const InternalNetwork &internal) {
    return rejection(build_network_plan(d, internal));
  };
  CHECK(refused(two_domains("other_n"), crossed_logger()).find("one reset") != std::string::npos);
  CHECK(refused(two_domains("reset_n", "clock_crossings:\n  - {from_domain: main, to_domain: slow, "
                                       "synchronizer_stages: 3}\n"),
                crossed_logger())
            .find("no declaration") != std::string::npos);
  CHECK(refused(two_domains(), crossed_logger("elsewhere")).find("elsewhere") != std::string::npos);
  CHECK(refused(definition(), crossed_logger()).find("clocked network") != std::string::npos);
  auto kept = crossed_logger();
  kept.fault_monitors[0].keep_fields = true;
  CHECK_FALSE(refused(two_domains(), kept).empty());
  auto apart = crossed_logger();
  apart.fault_monitors.push_back({"others", "csr", {"ram"}, {}});
  CHECK(refused(two_domains(), apart).find("share a domain") != std::string::npos);
}

TEST_CASE("A crossed logger leaves no combinational path into data traffic", "[observer]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger());
  REQUIRE(plan);
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  // The clock audit reads every register's cone: only crossings cross.
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  const auto &h = hardware.value();
  std::set<std::size_t> logger_states;
  for (const auto &[name, id] : h.bindings)
    if (name.starts_with("observe/errors/logger") && h.ir.values[id.index].op == Op::State)
      logger_states.insert(id.index);
  REQUIRE_FALSE(logger_states.empty());
  for (const auto &port : h.ir.ports) {
    if (port.input || port.endpoint.starts_with("error_log:") || port.name.starts_with("nl_i_cfg_"))
      continue;
    const auto driver = std::ranges::find_if(
        h.ir.drivers, [&](const auto &d) { return h.ir.ports[d.port.index].name == port.name; });
    REQUIRE(driver != h.ir.drivers.end());
    for (const auto state : cone_states(h.ir, driver->value)) {
      INFO(port.name);
      CHECK_FALSE(logger_states.contains(state));
    }
  }
}

TEST_CASE("A clocked network keeps its banks in their target's domain unless told", "[observer]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger("main"));
  // The second domain owns nothing then: declared domains must own an endpoint or a switch.
  CHECK(rejection(plan).find("own an endpoint") != std::string::npos);
}

namespace {
// A configuration of a data source, a configuration source, ram and a window of registers.
std::string logged_config(const std::string &logging, const std::string &extra_source = "",
                          const std::string &transport = "") {
  return "schema_version: 10\nname: o\naddress_width: 32\nreset_ports: [{name: reset_n}]\n"
         "test_ports: [{name: test}]\n" +
         transport +
         "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]\n"
         "initiators:\n  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32" +
         extra_source + "}\n  - {name: data, clock_domain: main, protocol: apb4, data_width: 32" +
         extra_source +
         "}\ntargets:\n  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, "
         "attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}\n" +
         logging +
         "connectivity:\n  cfg: [csr]\n  data: [ram]\noutput: {backend: verilog, language: "
         "verilog_2001}\n";
}
const std::string logged_window = "error_logging:\n  registers: {name: csr, base: '0x100000', "
                                  "size: '0x100000', clock_domain: main}\n";
Definition parsed_definition(const std::string &yaml) {
  auto result = config::parse(yaml);
  if (!result)
    FAIL(result.diagnostics().front().message);
  REQUIRE(result);
  return result.value().definition;
}
std::string network_text(const ArtifactSet &files) {
  return std::ranges::find_if(files, [](const auto &a) { return a.path == "nocloom_o.v"; })->text;
}
ArtifactSet generated_from(const Definition &d) {
  auto plan = build_network_plan(d);
  REQUIRE(plan);
  auto artifacts = emit_axi4_network(plan.value());
  REQUIRE(artifacts);
  return std::move(artifacts).value();
}
} // namespace

TEST_CASE("A configuration opens error logging on the same network", "[observer]") {
  const auto d = parsed_definition(
      logged_config(logged_window + "  logs:\n    - {name: errors, watch: [ram]}\n"));
  CHECK(std::ranges::find(d.targets, "csr", &Target::name)->log_registers);
  CHECK(validate(d));
  const auto from_config = generated_from(d);
  // The internal request of the same shape: the one target and the one logger.
  auto plain = d;
  plain.error_logging = {};
  std::ranges::find(plain.targets, "csr", &Target::name)->log_registers = false;
  InternalNetwork internal;
  internal.service_targets["csr"] = error_logger_bank("csr", 0, {1, 32, 0, 0, 2, 0});
  FaultMonitorRequest request{"errors", "csr", {"ram"}, {}};
  request.route = 0;
  internal.fault_monitors.push_back(std::move(request));
  auto planned = build_network_plan(plain, internal);
  REQUIRE(planned);
  const auto direct = emit_axi4_network(planned.value());
  REQUIRE(direct);
  CHECK(network_text(from_config) == network_text(direct.value()));
  // IP-XACT lists no bus interface for the window.
  const auto ipxact = add_ipxact(d, from_config, {});
  REQUIRE(ipxact);
  const auto xml = std::ranges::find_if(
      ipxact.value(), [](const auto &a) { return a.path.ends_with(".ipxact.xml"); });
  REQUIRE(xml != ipxact.value().end());
  CHECK(xml->text.find(">csr<") == std::string::npos);
  CHECK(xml->text.find(">ram<") != std::string::npos);
}

TEST_CASE("Explicit same-domain logs match the omitted domain", "[observer]") {
  const auto text = logged_config(logged_window + "  logs:\n    - {name: errors, watch: [ram]}\n");
  for (const auto topology : {0, 1, 2}) {
    auto omitted = parsed_definition(text);
    if (topology) {
      ExchangeGraph request, response;
      request.nodes = {{"rq"}};
      request.entries = {{"cfg", "rq"}, {"data", "rq"}};
      request.exits = {{"csr", "rq"}, {"ram", "rq"}};
      response.nodes = {{"rp"}};
      response.entries = {{"csr", "rp"}, {"ram", "rp"}};
      response.exits = {{"cfg", "rp"}, {"data", "rp"}};
      if (topology == 2) {
        request.nodes[0].clock_domain = "main";
        response.nodes[0].clock_domain = "main";
      }
      omitted.topology = ExchangeTopology{request, response};
    }
    auto explicit_domain = omitted;
    explicit_domain.error_logging.logs[0].clock_domain = "main";
    const auto a = generate(omitted), b = generate(explicit_domain);
    REQUIRE(a);
    REQUIRE(b);
    CHECK(a.value() == b.value());
    const auto parsed = config::parse(logged_config(
        logged_window + "  logs:\n    - {name: errors, watch: [ram], clock_domain: main}\n"));
    REQUIRE(parsed);
    CHECK(parsed.value().definition.error_logging.logs[0].clock_domain == "main");
  }
}

TEST_CASE("A configuration opens filters, chains and whole records", "[observer]") {
  const auto chain = parsed_definition(logged_config(
      logged_window + "  audit_flags: [zeta, alpha]\n  logs:\n    - name: errors\n"
                      "      watch: [ram]\n      loggers:\n"
                      "        - {skip: [decode], match: {alpha: true}, pass_on: always}\n"
                      "        - {offset: '0x300'}\n",
      ", audit_flags: {zeta: 0, alpha: 1}", "transport: {response_codes: distinct}\n"));
  CHECK(validate(chain));
  const auto plan = build_network_plan(chain);
  REQUIRE(plan);
  const auto &log = plan.value().fault_monitors.at(0).logger;
  CHECK(log.ignored_codes == std::vector<unsigned>{CodeDecode});
  CHECK(log.pass_always);
  CHECK(log.security_filter == std::vector<RecordFilterBit>{{1, true}});
  REQUIRE(log.chain.size() == 1);
  CHECK(plan.value().security_flags == std::vector<std::string>{"zeta", "alpha"});
  const auto &banks = plan.value().target("csr").service->banks;
  REQUIRE(banks.size() == 2);
  CHECK(banks[0].base == 0);
  CHECK(banks[1].base == 0x300);
  CHECK(banks[1].plan.route == 1);
}

TEST_CASE("A configuration names its mistakes", "[observer]") {
  const auto rejected = [&](const std::string &yaml) {
    const auto status = validate(parsed_definition(yaml));
    return status ? std::string{} : status.diagnostics().front().code;
  };
  CHECK(rejected(logged_config(logged_window + "  logs: []\n")) == "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(rejected(logged_config(logged_window + "  logs:\n    - {name: e, watch: [nope]}\n")) ==
        "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(rejected(logged_config(logged_window + "  logs:\n    - {name: e, watch: [csr]}\n")) ==
        "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(rejected(logged_config(logged_window + "  logs:\n    - {name: e, watch: [ram], loggers: "
                                               "[{skip: [timeout]}]}\n")) ==
        "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(rejected(logged_config(logged_window + "  logs:\n    - {name: e, watch: [ram], "
                                               "clock_domain: other}\n")) ==
        "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(rejected(logged_config(logged_window + "  logs:\n    - {name: e, watch: [ram], loggers: "
                                               "[{match: {nope: true}}]}\n")) ==
        "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK_FALSE(config::parse(
      logged_config(logged_window + "  logs:\n    - {name: e, watch: [ram], steer: 2}\n")));
  CHECK_FALSE(config::parse(logged_config(logged_window + "  logs:\n    - {name: e, watch: [ram], "
                                                          "loggers: [{pass_on: sometimes}]}\n")));
}

TEST_CASE("Record words cover exact field boundaries", "[observer]") {
  for (const unsigned width : {8U, 16U, 32U}) {
    for (const unsigned bits : {width - 1, width, width + 1}) {
      ErrorLoggerPlan plan;
      plan.name = "boundary";
      plan.shape = {1, bits - 4, 0, 0, 2, 0};
      plan.fixed_length = 3;
      plan.crossed = true;
      plan.serial_word_bits = width;
      plan.serial_words = bits <= width ? 1 : 2;
      const auto layout = error_record_layout(plan);
      CHECK(layout.bits == bits);
      CHECK(layout.fields == std::vector<RecordLayout::Field>{{"route", 1, 0},
                                                              {"write", 1, 1},
                                                              {"wrap", 1, 2},
                                                              {"partial", 1, 3},
                                                              {"address", bits - 4, 4}});
      REQUIRE(lower_error_sender(plan, 0));
      REQUIRE(lower_error_logger(plan));
      CHECK(error_record_word_bits(plan) == width);
      ++plan.serial_words;
      CHECK_FALSE(lower_error_logger(plan));
    }
  }
}

TEST_CASE("Stored record fields omit only static or absent values", "[observer]") {
  ErrorLoggerPlan plan;
  plan.shape = {5, 32, 7, 2, 8, 3};
  CHECK(error_record_layout(plan).bits == 61);
  plan.fixed_length = 3;
  CHECK(error_record_layout(plan).bits == 52);
  plan.shape.code_bits = 0;
  plan.shape.user_bits = 0;
  plan.shape.security_bits = 0;
  CHECK(error_record_layout(plan).bits == 40);
}

TEST_CASE("Log word width is explicit in definitions and maps", "[observer]") {
  for (const unsigned width : {8U, 16U, 32U}) {
    auto d = definition();
    d.targets[1].log_registers = true;
    d.error_logging.logs.push_back(ErrorLog{"errors", {"ram"}});
    d.error_logging.logs.back().serial_word_bits = width;
    const auto plan = build_network_plan(d);
    REQUIRE(plan);
    const auto &log = plan.value().fault_monitors.at(0).logger;
    CHECK(log.serial_word_bits == width);
    CHECK(log.serial_words == (41 + width - 1) / width);
  }
  auto internal = logger();
  internal.fault_monitors[0].serial_word_bits = 11;
  CHECK_FALSE(build_network_plan(definition(), internal));
  const auto bad = config::parse(logged_config(
      logged_window + "  logs: [{name: errors, watch: [ram], serial_word_bits: 11}]\n"));
  REQUIRE(bad);
  CHECK_FALSE(validate(bad.value().definition));
  for (const unsigned width : {8U, 16U, 32U}) {
    const auto parsed = config::parse(
        logged_config(logged_window + "  logs: [{name: errors, watch: [ram], serial_word_bits: " +
                      std::to_string(width) + "}]\n"));
    REQUIRE(parsed);
    CHECK(parsed.value().definition.error_logging.logs[0].serial_word_bits == width);
  }
}

TEST_CASE("The revision register holds a given value or the generator version", "[observer]") {
  const auto config = [](const std::string &revision) {
    return logged_config(logged_window + revision + "  logs: [{name: errors, watch: [ram]}]\n");
  };
  // The network's RTL, and what the map states for the bank's revision field.
  struct Seen {
    std::string rtl, description;
    std::uint64_t value = 0;
  };
  const auto seen = [](const Definition &d) {
    const auto files = generate(d);
    REQUIRE(files);
    const auto map = nlohmann::json::parse(
        std::ranges::find(files.value(), "nocloom_o.map.json", &Artifact::path)->text);
    Seen out{network_text(files.value()), "", 0};
    unsigned found = 0;
    for (const auto &r : map["definition"]["log_registers"]["csr"]["registers"])
      if (r["name"] == "revision_generator") {
        ++found;
        out.value = r["value"].get<std::uint64_t>();
        out.description = r["description"].get<std::string>();
        CHECK(r["offset"] == 4);
        CHECK(r["lsb"] == 0);
        CHECK(r["width"] == 24);
        CHECK(r["access"] == "constant");
      }
    CHECK(found == 1);
    return out;
  };
  // Absent: major, minor and patch of the generator version, one byte each.
  std::uint32_t generator = 0;
  std::istringstream text{std::string(version())};
  for (std::string part; std::getline(text, part, '.');)
    generator = generator << 8 | static_cast<std::uint32_t>(std::stoul(part));
  const auto plain = parsed_definition(config(""));
  CHECK_FALSE(plain.error_logging.revision);
  const auto absent = seen(plain);
  CHECK(absent.value == generator);
  CHECK(absent.description.starts_with("Generator version"));
  std::set<std::string> networks{absent.rtl};
  for (const std::uint32_t value : {0U, 0x5A3C01U, 0xFFFFFFU}) {
    const auto parsed = parsed_definition(config("  revision: " + std::to_string(value) + "\n"));
    CHECK(parsed.error_logging.revision == value);
    // A definition built in C++ is the parsed one, and gives the same files.
    auto direct = plain;
    direct.error_logging.revision = value;
    CHECK(direct == parsed);
    CHECK(generate(direct).value() == generate(parsed).value());
    const auto given = seen(parsed);
    CHECK(given.value == value);
    CHECK(given.description == "The definition's error_logging.revision");
    // Each value is a network of its own, and none is the network of the generator version.
    CHECK(networks.insert(given.rtl).second);
  }
  // The generator version given as a value is the same network.
  auto same = plain;
  same.error_logging.revision = generator;
  CHECK(seen(same).rtl == absent.rtl);

  const auto refused = [](const auto &result, const std::string &code, const std::string &field) {
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(), [&](const Diagnostic &diagnostic) {
      return diagnostic.code == code && diagnostic.field == field;
    }));
  };
  auto wide = plain;
  wide.error_logging.revision = 0x1000000U;
  refused(validate(wide), "NCL_UNSUPPORTED_TOPOLOGY", "error_logging.revision");
  refused(generate(wide), "NCL_UNSUPPORTED_TOPOLOGY", "error_logging.revision");
  const auto wide_text = config::parse(config("  revision: 16777216\n"));
  REQUIRE(wide_text);
  CHECK(wide_text.value().definition == wide);
  const auto located = validate(wide_text.value().definition, {}, &wide_text.value().sources);
  refused(located, "NCL_UNSUPPORTED_TOPOLOGY", "error_logging.revision");
  CHECK(located.diagnostics().front().location);
  // Negative, fractional, hexadecimal, quoted and oversized spellings are not integers here.
  for (const auto *spelling : {"-1", "1.5", "0x10", "'7'", "seven", "4294967296", "[1]"})
    refused(config::parse(config("  revision: " + std::string(spelling) + "\n")), "NCL_SCHEMA_TYPE",
            "error_logging.revision");
  // The field belongs to error logging with a window, and to nothing else.
  refused(config::parse(
              config("").replace(config("").find("watch: [ram]"), 12, "watch: [ram], revision: 1")),
          "NCL_SCHEMA_UNKNOWN_FIELD", "error_logging.logs[0].revision");
  refused(config::parse(logged_config(logged_window + "  logs: [{name: errors, watch: [ram]}]\n"
                                                      "revision: 1\n")),
          "NCL_SCHEMA_UNKNOWN_FIELD", "revision");
  auto no_window = definition();
  no_window.error_logging.revision = 1;
  refused(validate(no_window), "NCL_UNSUPPORTED_TOPOLOGY", "error_logging.registers");
}
