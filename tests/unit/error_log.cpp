#include "core/axi4_output.hpp"
#include "core/cone_walk.hpp"
#include "core/exchange_composition.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/register_bank.hpp"
#include "nocloom/config.hpp"
#include "nocloom/generation.hpp"
#include "nocloom/ipxact.hpp"
#include <algorithm>
#include <array>
#include <catch2/catch_test_macros.hpp>
#include <catch2/generators/catch_generators.hpp>
#include <iterator>
#include <nlohmann/json.hpp>
#include <numeric>
#include <random>
#include <set>
#include <span>
#include <sstream>
#include <tuple>

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
                       std::vector<unsigned> skipped = {}) {
  InternalNetwork internal;
  internal.service_targets["csr"] = error_logger_bank("log", {1, 32, 0, 0, 2, 0});
  internal.error_logs.push_back({"errors", "csr", std::move(targets), std::move(skipped)});
  return internal;
}
std::string rejection(const Result<NetworkPlan> &plan) {
  return plan ? std::string{} : plan.diagnostics().front().message;
}
// A bank's field by name, or nothing.
const RegisterField *field_of(const RegisterBankPlan &bank, const std::string &name) {
  const auto at = std::ranges::find(bank.fields, name, &RegisterField::name);
  return at == bank.fields.end() ? nullptr : &*at;
}
unsigned width_of(const RegisterBankPlan &bank, const std::string &name) {
  const auto *field = field_of(bank, name);
  return field ? field->width : 0;
}
std::uint64_t value_of(const RegisterBankPlan &bank, const std::string &name) {
  const auto *field = field_of(bank, name);
  REQUIRE(field);
  return field->value;
}
// Every state of a value's combinational cone (states are its leaves).
std::set<std::size_t> cone_states(const HardwareIR &ir, ValueId root) {
  std::set<std::size_t> out;
  walk_cone(ir, root, [&](ValueId state) { out.insert(state.index); }, [](ValueId) {});
  return out;
}
} // namespace

TEST_CASE("An error logger's plan follows the route numbering and the transport's address",
          "[error-log]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto &log = plan.value().error_logs.at(0);
  // {initiator, target, sub-range}: data 1 beside cfg, ram 1 beside csr, its sub-range and
  // aperture.
  CHECK(log.logger.shape.route_bits == 3);
  // One-target sources keep whole addresses, so the record's address has all 32 bits.
  CHECK(log.logger.shape.address_bits == 32);
  // Two class bits, write, wrap, address_partial, 32 address bits and three route bits.
  CHECK(error_record_layout(log.logger).bits == 40);
  CHECK(log.logger.serial_words == 2);
  const auto &row = log.observed.at(0).sources.at(0);
  CHECK(row.route == 6);
  CHECK(row.miss_range == 1);
  CHECK(row.miss_mask == ~std::uint64_t{0});
  const auto service = plan.value().target("csr").service;
  REQUIRE(service);
  const auto *bank = &service->banks.at(0).plan;
  // The bank shows the route's three bits as its parts, and APB sources have no AxID.
  const auto &shape = log.logger.shape;
  CHECK(shape.region_bits + shape.target_path_bits + shape.source_path_bits == 3);
  CHECK(shape.sequence_bits == 0);
  for (const auto &[name, word, lsb] :
       std::vector<std::tuple<std::string, unsigned, unsigned>>{{"record_region_index", 5, 0},
                                                                {"record_target_path", 4, 16},
                                                                {"record_source_path", 4, 0}}) {
    INFO(name);
    const auto *part = field_of(*bank, name);
    REQUIRE(part);
    CHECK(part->width == 1);
    CHECK(part->word == word);
    CHECK(part->lsb == lsb);
    CHECK(part->access == RegisterAccess::Ro);
  }
  CHECK(field_of(*bank, "record_sequence") == nullptr);
  // The class has its two bits, and one APB word is up to four bytes.
  CHECK(width_of(*bank, "record_class") == 2);
  CHECK(width_of(*bank, "record_bytes") == 3);
  CHECK(bank->address_bits == 6);
}

TEST_CASE("Unobserved shapes are rejected", "[error-log]") {
  CHECK(rejection(build_network_plan(definition(), logger({"ram", "ram"}))).find("once") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(definition(), logger({"csr"}))).find("once") !=
        std::string::npos);
  CHECK(!rejection(build_network_plan(definition(", timeout: {cycles: 64}"), logger())).empty());
  // The record and its filter keep both classes even without AXI initiators.
  auto codes = definition();
  for (const auto &skipped :
       {std::vector<unsigned>{ClassTarget}, {ClassDecode}, {ClassTarget, ClassDecode}}) {
    const auto classified = build_network_plan(codes, logger({"ram"}, skipped));
    REQUIRE(classified);
    CHECK(classified.value().failure_classes);
    CHECK(classified.value().error_logs.front().logger.shape.class_bits == 2);
  }
  auto classless = logger();
  classless.error_logs.front().capture_class = false;
  const auto classified = build_network_plan(codes, classless);
  REQUIRE(classified);
  CHECK(classified.value().failure_classes);
  CHECK(classified.value().error_logs.front().logger.shape.class_bits == 0);
  codes.transport = TransportOptions{.compact_failure_responses = true};
  const auto compact = build_network_plan(codes, classless);
  REQUIRE(compact);
  CHECK_FALSE(compact.value().failure_classes);
  CHECK(compact.value().error_logs.front().logger.shape.class_bits == 0);
  const auto refused_class = build_network_plan(codes, logger());
  REQUIRE_FALSE(refused_class);
  CHECK(refused_class.diagnostics().front().field == "transport.compact_failure_responses");
  CHECK(refused_class.diagnostics().front().message == "Error log errors records failure classes");
  codes.transport.reset();
  // A skipped class is one of the two, never a mask of them.
  for (const unsigned unknown : {0U, 3U, 4U, 6U}) {
    const auto refused = build_network_plan(codes, logger({"ram"}, {unknown}));
    CHECK(rejection(refused).find("skips target or decode") != std::string::npos);
    REQUIRE_FALSE(refused);
    CHECK(refused.diagnostics().front().field == "error_logging.skip");
  }
  auto two = logger();
  two.error_logs.push_back({"more", "csr", {"ram"}, {}});
  two.error_logs[1].base = 0x100;
  CHECK(rejection(build_network_plan(definition(), two)).find("once") != std::string::npos);
  auto other = logger();
  other.error_logs[0].service = "ram";
  CHECK(rejection(build_network_plan(definition(), other)).find("window") != std::string::npos);
}

namespace {
// {cfg, data} over {csr, ram, rom}: the two AXI4 targets of 2^46 bytes hold the service window.
Definition axi_definition(unsigned contexts = 2) {
  const auto bursts = std::to_string(contexts);
  const auto text =
      "schema_version: 10\nname: k\naddress_width: 48\ntransport: {"
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
  internal.service_targets["csr"] = error_logger_bank("log", {1, 32, 0, 0, 2, 0});
  internal.error_logs.push_back({"errors", "csr", {"ram", "rom"}, {}, true});
  return internal;
}
} // namespace

TEST_CASE("A keeping logger records the transport's whole address and every flag by name",
          "[error-log]") {
  const auto plan = build_network_plan(axi_definition(), keeping());
  REQUIRE(plan);
  const auto &log = plan.value().error_logs.at(0);
  CHECK(log.whole_record);
  CHECK(log.logger.shape.user_bits == 2);
  CHECK(log.logger.shape.address_bits == 46);
  const auto &shape = log.logger.shape;
  CHECK(log.logger.serial_words ==
        (shape.route_bits + 3 + shape.length_bits + 1 + 46 + 2 + 2 + 31) / 32);
  // The record's length comes from the source: a length and a lock bit.
  CHECK(plan.value().burst_record_bits == shape.length_bits + 1);
  const auto service = plan.value().target("csr").service;
  REQUIRE(service);
  const auto *bank = &service->banks.at(0).plan;
  CHECK(std::ranges::any_of(bank->fields, [](const RegisterField &f) {
    return f.name == "record_user_flags0" && f.width == 2 && f.word == 8;
  }));
  CHECK(std::ranges::any_of(bank->fields, [](const RegisterField &f) {
    return f.name == "record_address_high" && f.width == 14 && f.word == 3 && f.lsb == 0;
  }));
  // The bytes of the longest burst: one more bit than the offset of its last byte.
  CHECK(width_of(*bank, "record_bytes") == shape.length_bits + 1);
  CHECK(field_of(*bank, "record_bytes")->lsb == 16);
  // The source's AxID bit stands in the record's route and in the bank.
  CHECK(width_of(*bank, "record_sequence") == 1);
  CHECK(value_of(*bank, "capability_user_flags") == 1);
  CHECK(value_of(*bank, "capability_target_user_flags") == 1);
  CHECK(value_of(*bank, "capability_audit_flags") == 0);
  // A network without a logger carries no record field.
  const auto plain = build_network_plan(axi_definition());
  REQUIRE(plain);
  CHECK(plain.value().burst_record_bits == 0);
}

TEST_CASE("A keeping logger's filter takes user flags by name", "[error-log]") {
  auto internal = keeping();
  using S = AttributeSource;
  internal.audit.flags = {"s"};
  internal.audit.drivers["cfg"]["s"] = {S::Zero};
  internal.audit.drivers["data"]["s"] = {S::One};
  internal.error_logs[0].user_match = {{"a", true}, {"b", false}};
  const auto plan = build_network_plan(axi_definition(), internal);
  REQUIRE(plan);
  CHECK(plan.value().error_logs.at(0).logger.user_match ==
        std::vector<RecordFilterBit>{{1, true}, {0, false}});
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  if (!hardware)
    FAIL(hardware.diagnostics().front().message);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
  // A driver above the source's AxUSER port.
  auto high = internal;
  high.audit.drivers["data"]["s"] = {S::User, false, 2};
  CHECK(rejection(build_network_plan(axi_definition(), high)).find("AxUSER port") !=
        std::string::npos);
  high.audit.drivers["data"]["s"] = {S::User, true, 1};
  CHECK(build_network_plan(axi_definition(), high));
}

TEST_CASE("A record has room for user flags without keeping them", "[error-log]") {
  auto internal = keeping();
  internal.error_logs[0].whole_record = false;
  const auto plan = build_network_plan(axi_definition(), internal);
  REQUIRE(plan);
  const auto &log = plan.value().error_logs.at(0);
  CHECK(!log.whole_record);
  CHECK(log.logger.shape.user_bits == 2);
  // Nothing to filter on: only a miss fills them.
  internal.error_logs[0].user_match = {{"a", true}};
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

TEST_CASE("A target that keeps a record's fields has sources of its protocol", "[error-log]") {
  auto apb = logger();
  apb.error_logs[0].whole_record = true;
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

TEST_CASE("Several contexts of an observed AXI4 target hold the record's fields", "[error-log]") {
  for (const unsigned contexts : {1U, 2U, 4U}) {
    const auto plan = build_network_plan(axi_definition(contexts), keeping());
    REQUIRE(plan);
    const auto hardware = lower_axi4_exchange_composition(plan.value());
    REQUIRE(hardware);
    CHECK(verify_axi4_exchange_composition(hardware.value()));
    const auto &bindings = hardware.value().bindings;
    CHECK(bindings.contains("observe/rom/address"));
    CHECK(bindings.contains("observe/ram/context1.user_flags") == (contexts > 1));
    CHECK(bindings.contains("observe/ram/context3.user_flags") == (contexts > 2));
    CHECK(bindings.contains("observe/ram/context4.user_flags") == false);
    CHECK(plan.value().dependency_report.cycle_sets == 0);
  }
}

TEST_CASE("A target no initiator shares with a larger mapping stays out of its decode",
          "[error-log]") {
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

TEST_CASE("A logger's nodes stay apart from the data path", "[error-log]") {
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
// The logger over {cfg, data}, with three audit flags out of name order and a filter of them.
InternalNetwork secured() {
  auto internal = logger();
  using S = AttributeSource;
  internal.audit.flags = {"zeta", "Alpha", "mid"};
  internal.audit.drivers["cfg"] = {{"zeta", {S::Zero}}, {"Alpha", {S::Zero}}, {"mid", {S::One}}};
  internal.audit.drivers["data"] = {
      {"zeta", {S::Privileged}}, {"Alpha", {S::Nonsecure, true}}, {"mid", {S::Instruction}}};
  internal.error_logs[0].audit_match = {{"Alpha", true}, {"zeta", false}};
  return internal;
}
// The logger with so many audit flags a0, a1 and so on: cfg drives each to zero, data to one.
InternalNetwork flagged(unsigned flags) {
  auto internal = logger();
  for (unsigned k = 0; k < flags; ++k) {
    const auto name = "a" + std::to_string(k);
    internal.audit.flags.push_back(name);
    internal.audit.drivers["cfg"][name] = {AttributeSource::Zero};
    internal.audit.drivers["data"][name] = {AttributeSource::One};
  }
  return internal;
}
} // namespace

TEST_CASE("Audit flags travel in name order and fill the record's word", "[error-log]") {
  const auto plain = build_network_plan(definition(), logger());
  const auto plan = build_network_plan(definition(), secured());
  REQUIRE(plain);
  REQUIRE(plan);
  CHECK(plain.value().burst_audit_bits == 0);
  const auto &p = plan.value();
  CHECK(p.audit_flags == std::vector<std::string>{"zeta", "Alpha", "mid"});
  CHECK(p.burst_audit_bits == 3);
  const auto &data = *std::ranges::find(p.sources, "data", &SourcePlan::name);
  CHECK(data.audit_flags[1] == AttributeBit{AttributeSource::Nonsecure, true});
  CHECK(data.audit_flags[0] == AttributeBit{AttributeSource::Privileged});
  const auto &log = p.error_logs.at(0).logger;
  CHECK(log.shape.audit_bits == 3);
  CHECK(log.audit_match == std::vector<RecordFilterBit>{{1, true}, {0, false}});
  const auto &bank =
      std::ranges::find(p.targets, "csr", &TargetPlan::name)->service->banks.at(0).plan;
  const auto word = std::ranges::find(bank.fields, "record_audit_flags", &RegisterField::name);
  REQUIRE(word != bank.fields.end());
  CHECK(word->width == 3);
  CHECK(word->word == 6);
  CHECK(value_of(bank, "capability_audit_flags") == 1);
  // Only with flags does a request carry them.
  CHECK(!plain.value().error_logs.at(0).logger.shape.audit_bits);
}

TEST_CASE("Audit flags take a record's serial width", "[error-log]") {
  auto codes = definition();
  const auto without = build_network_plan(codes, logger());
  auto internal = logger();
  using S = AttributeSource;
  for (const auto *name : {"a", "b", "c", "d", "e", "f", "g", "h"}) {
    internal.audit.flags.emplace_back(name);
    internal.audit.drivers["cfg"][name] = {S::Zero};
    internal.audit.drivers["data"][name] = {S::One};
  }
  const auto with = build_network_plan(codes, internal);
  REQUIRE(without);
  REQUIRE(with);
  // Optional flags add only their actual width.
  CHECK(error_record_layout(with.value().error_logs.at(0).logger).bits ==
        error_record_layout(without.value().error_logs.at(0).logger).bits + 8);
  CHECK(without.value().error_logs.at(0).logger.serial_words == 2);
  CHECK(with.value().error_logs.at(0).logger.serial_words == 2);
}

TEST_CASE("A record's word holds 32 audit flags", "[error-log]") {
  const auto plan = build_network_plan(definition(), flagged(32));
  REQUIRE(plan);
  const auto &p = plan.value();
  REQUIRE(p.audit_flags.size() == 32);
  CHECK(p.audit_flags.front() == "a0");
  CHECK(p.audit_flags.back() == "a31");
  CHECK(p.burst_audit_bits == 32);
  const auto &log = p.error_logs.at(0).logger;
  CHECK(log.shape.audit_bits == 32);
  // The 40 bits of a record without flags and the 32 flags: three words.
  CHECK(error_record_layout(log).bits == 72);
  CHECK(log.serial_words == 3);
  const auto &bank = p.target("csr").service->banks.at(0).plan;
  const auto *word = field_of(bank, "record_audit_flags");
  REQUIRE(word);
  CHECK(word->width == 32);
  CHECK(word->lsb == 0);
  CHECK(word->word == 6);
  CHECK(check_register_bank(bank));
  const auto &data = *std::ranges::find(p.sources, "data", &SourcePlan::name);
  REQUIRE(data.audit_flags.size() == 32);
  CHECK(data.audit_flags.back() == AttributeBit{AttributeSource::One});
  const auto hardware = lower_axi4_exchange_composition(p);
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
}

TEST_CASE("Audit flags outside their scope are rejected", "[error-log]") {
  using S = AttributeSource;
  const auto refused = [](const Definition &d, const InternalNetwork &internal) {
    return rejection(build_network_plan(d, internal));
  };
  auto none = secured();
  none.error_logs.clear();
  CHECK(refused(definition(), none).find("serve an error log") != std::string::npos);
  const auto many = build_network_plan(definition(), flagged(33));
  REQUIRE_FALSE(many);
  CHECK(many.diagnostics().front().code == "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(many.diagnostics().front().field == "error_logging.audit_flags.flags");
  CHECK(many.diagnostics().front().message == "One to 32 distinct audit flags with valid names");
  auto twice = secured();
  twice.audit.flags.push_back("mid");
  CHECK(refused(definition(), twice).find("One to 32") != std::string::npos);
  auto undriven = secured();
  undriven.audit.drivers["data"].erase("mid");
  CHECK(refused(definition(), undriven).find("drives every") != std::string::npos);
  auto silent = secured();
  silent.audit.drivers.erase("cfg");
  CHECK(refused(definition(), silent).find("drives every") != std::string::npos);
  auto stranger = secured();
  stranger.audit.drivers["other"] = {};
  CHECK(refused(definition(), stranger).find("No such source") != std::string::npos);
  // No driver of a cache flag, and an APB port has no user bit.
  for (const auto kind : {S::Bufferable, S::Modifiable, S::Allocate, S::Lookup, S::User}) {
    auto driven = secured();
    driven.audit.drivers["data"]["mid"] = {kind};
    CHECK(refused(definition(), driven).find("constant, an AxPROT flag") != std::string::npos);
  }
  // A filter names a flag a record keeps.
  auto unknown = secured();
  unknown.error_logs[0].audit_match["other"] = true;
  CHECK(refused(definition(), unknown).find("A filter names") != std::string::npos);
  // Not keeping its fields, a record has no user flags to filter.
  auto user = secured();
  user.error_logs[0].user_match["a"] = true;
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
                                                        {"ahb_lite", ""}})
    CHECK(refused(text(protocol, options), secured()).find("Only APB4 and AXI4") !=
          std::string::npos);
}

TEST_CASE("No combinational path leads from a logger into the data path", "[error-log]") {
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
          "[error-log]") {
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

TEST_CASE("A map describes service targets and error loggers", "[error-log]") {
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
  // The bank's record states what its first word holds: the layout, its place and its parts.
  const auto bank = nlohmann::json::parse(map->text).at("definition").at("log_registers").at("csr");
  CHECK(bank.at("layout") == 2);
  CHECK(bank.at("log_index") == 0);
  CHECK(bank.at("logger_index") == 0);
  CHECK(bank.at("capabilities") == nlohmann::json::array());
  CHECK(bank.at("bank") == "log");
  CHECK(bank.at("base") == 0);
  CHECK_FALSE(bank.contains("banks"));
}

namespace {
// Two error logs on two data targets, their banks where the bench reads them.
Definition two_targets() {
  auto d = definition("", "  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, "
                          "attributes: amba, mappings: [{base: '0x10000', size: '0x1000'}]}\n");
  d.connectivity->pairs.push_back({"data", "rom"});
  return d;
}
InternalNetwork two_banks(std::uint64_t second_base = 0x100) {
  auto internal = logger();
  internal.error_logs[0].base = 0x300;
  ErrorLogRequest other{"more", "csr", {"rom"}, {}};
  other.base = second_base;
  internal.error_logs.push_back(std::move(other));
  return internal;
}
} // namespace

TEST_CASE("Banks of a service target hang on a switch, each with its place", "[error-log]") {
  const auto plan = build_network_plan(two_targets(), two_banks());
  REQUIRE(plan);
  const auto &service = plan.value().target("csr").service;
  REQUIRE(service);
  REQUIRE(service->banks.size() == 2);
  // Each log is a branch of its own, and its one logger is the first of its chain.
  CHECK(service->banks[0].plan.key == 0);
  CHECK(service->banks[0].base == 0x300);
  CHECK(service->banks[0].branch == 0);
  CHECK(service->banks[1].plan.key == 0);
  CHECK(service->banks[1].base == 0x100);
  CHECK(service->banks[1].branch == 1);
  CHECK(plan.value().error_logs[1].bank == 1);
  // Each bank states its log, and a log of one logger has no next one.
  for (const unsigned log : {0U, 1U}) {
    const auto &bank = service->banks[log].plan;
    CHECK(value_of(bank, "log_index") == log);
    CHECK(value_of(bank, "logger_index") == 0);
    CHECK(value_of(bank, "capability_next_logger") == 0);
    CHECK(value_of(bank, "layout") == 2);
    CHECK(bank.address_bits == 6);
  }
  // The switch's outputs stand in the graph, and the network stays free of cycles.
  const auto outputs = std::ranges::count_if(
      plan.value().dependency.nodes, [](const auto &n) { return n.kind == "service.output"; });
  CHECK(outputs == 2);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
}

TEST_CASE("Service bank places are rejected unless they keep apart", "[error-log]") {
  CHECK(rejection(build_network_plan(two_targets(), two_banks(0x300))).find("apart") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(two_targets(), two_banks(0x84))).find("apart") !=
        std::string::npos);
  // A bank is 64 bytes: the blocks beside it are free, a place inside it is not a block.
  CHECK(build_network_plan(two_targets(), two_banks(0x2c0)));
  CHECK(build_network_plan(two_targets(), two_banks(0x340)));
  CHECK(rejection(build_network_plan(two_targets(), two_banks(0x320))).find("apart") !=
        std::string::npos);
  CHECK(build_network_plan(two_targets(), two_banks(0xfffc0)));
  CHECK(rejection(build_network_plan(two_targets(), two_banks(0xfffe0))).find("apart") !=
        std::string::npos);
  CHECK(rejection(build_network_plan(two_targets(), two_banks(0x100000))).find("1 MiB") !=
        std::string::npos);
  auto twice = two_banks();
  twice.error_logs[1].targets = {"ram"};
  CHECK(rejection(build_network_plan(two_targets(), twice)).find("once") != std::string::npos);
  auto named = two_banks();
  named.error_logs[1].name = "errors";
  CHECK(rejection(build_network_plan(two_targets(), named)).find("distinct") != std::string::npos);
}

TEST_CASE("No combinational path leads from a bank's logger into data traffic beside a switch",
          "[error-log]") {
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
InternalNetwork chain_of_two(bool pass_always = false) {
  auto internal = logger();
  auto &first = internal.error_logs[0];
  first.base = 0x300;
  first.pass_always = pass_always;
  first.chain.push_back({{}, {}, {}, 0x100, false});
  return internal;
}
} // namespace

TEST_CASE("A chain's loggers have a bank each on one branch", "[error-log]") {
  const auto plan = build_network_plan(definition(), chain_of_two(true));
  REQUIRE(plan);
  const auto &service = plan.value().target("csr").service;
  REQUIRE(service);
  REQUIRE(service->banks.size() == 2);
  CHECK(service->banks[0].branch == 0);
  CHECK(service->banks[1].branch == 0);
  // Both banks state the one log, each its place in the chain and whether a logger follows.
  for (const unsigned logger : {0U, 1U}) {
    const auto &bank = service->banks[logger].plan;
    CHECK(value_of(bank, "log_index") == 0);
    CHECK(value_of(bank, "logger_index") == logger);
    CHECK(bank.key == logger);
    CHECK(value_of(bank, "capability_next_logger") == (logger == 0 ? 1 : 0));
  }
  const auto &log = plan.value().error_logs.at(0).logger;
  CHECK(log.pass_always);
  REQUIRE(log.chain.size() == 1);
  CHECK_FALSE(log.chain[0].pass_always);
  // No switch on one branch.
  const auto &nodes = plan.value().dependency.nodes;
  CHECK(std::ranges::none_of(nodes, [](const auto &n) { return n.kind == "service.output"; }));
  CHECK(std::ranges::count_if(nodes, [](const auto &n) { return n.kind == "logger.slot"; }) == 2);
}

TEST_CASE("A bank's key is its logger's place and its branch is its log's place", "[error-log]") {
  // Three loggers on ram and two on rom, their banks out of order in the window.
  auto internal = logger();
  auto &first = internal.error_logs[0];
  first.base = 0x300;
  first.chain.push_back({{}, {}, {}, 0x100, false});
  first.chain.push_back({{}, {}, {}, 0x140, false});
  ErrorLogRequest other{"more", "csr", {"rom"}, {}};
  other.base = 0x200;
  other.chain.push_back({{}, {}, {}, 0x0, false});
  internal.error_logs.push_back(std::move(other));
  const auto plan = build_network_plan(two_targets(), internal);
  REQUIRE(plan);
  const auto &banks = plan.value().target("csr").service->banks;
  // The log and the logger of each bank, in the plan's order.
  const std::vector<std::pair<unsigned, unsigned>> places{{0, 0}, {0, 1}, {0, 2}, {1, 0}, {1, 1}};
  REQUIRE(banks.size() == places.size());
  const auto profile = service_outlet_profile(plan.value(), plan.value().target_index("csr"));
  REQUIRE(profile.places.size() == places.size());
  for (std::size_t k = 0; k < places.size(); ++k) {
    const auto [log, place] = places[k];
    CHECK(banks[k].branch == log);
    CHECK(banks[k].plan.key == place);
    CHECK(value_of(banks[k].plan, "log_index") == log);
    CHECK(value_of(banks[k].plan, "logger_index") == place);
    CHECK(check_register_bank(banks[k].plan));
    // The outlet sends each bank's frames under that key on that branch.
    CHECK(profile.places[k].base == banks[k].base);
    CHECK(profile.places[k].key == place);
    CHECK(profile.places[k].branch == log);
  }
  CHECK(lower_service_outlet(profile));
  const auto hardware = lower_axi4_exchange_composition(plan.value());
  REQUIRE(hardware);
  CHECK(verify_axi4_exchange_composition(hardware.value()));
}

TEST_CASE("A chain's wait stays inside its error logger", "[error-log]") {
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

TEST_CASE("No combinational path leads from a chain into data traffic", "[error-log]") {
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

TEST_CASE("A chain keeps to its shape", "[error-log]") {
  // 32 loggers are a chain: each bank's key is its logger's place, all on the log's branch.
  auto long_chain = chain_of_two();
  for (unsigned n = 1; n < 31; ++n)
    long_chain.error_logs[0].chain.push_back({{}, {}, {}, 0x100ULL * (n + 8), false});
  const auto longest = build_network_plan(definition(), long_chain);
  REQUIRE(longest);
  const auto &banks = longest.value().target("csr").service->banks;
  REQUIRE(banks.size() == 32);
  for (unsigned place = 0; place < 32; ++place) {
    CHECK(banks[place].plan.key == place);
    CHECK(banks[place].branch == 0);
    CHECK(value_of(banks[place].plan, "logger_index") == place);
    CHECK(value_of(banks[place].plan, "capability_next_logger") == (place < 31 ? 1 : 0));
    CHECK(check_register_bank(banks[place].plan));
  }
  CHECK(banks[31].base == 0x2600);
  long_chain.error_logs[0].chain.push_back({{}, {}, {}, 0x100ULL * 39, false});
  const auto longer = build_network_plan(definition(), long_chain);
  REQUIRE_FALSE(longer);
  CHECK(longer.diagnostics().front().code == "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(longer.diagnostics().front().field == "error_logging.errors");
  CHECK(longer.diagnostics().front().message == "A log has at most 32 loggers");
  auto named = chain_of_two();
  named.error_logs[0].chain[0].audit_match["nothing"] = true;
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
  internal.error_logs[0].clock_domain = domain;
  return internal;
}
} // namespace

TEST_CASE("A logger in a domain of its own crosses the serial path and the record", "[error-log]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger());
  REQUIRE(plan);
  const auto &p = plan.value();
  CHECK(p.target("csr").service->domain == "slow");
  CHECK(p.error_logs[0].logger.crossed);
  std::set<std::string> cuts;
  for (const auto &cut : p.cuts) {
    cuts.insert(cut.name);
    const auto &options = std::get<AsyncClockCut>(cut.options);
    // (2 + 1) * (1250 + 3333) / 3333 rounded up, with the synchronizers of two stages.
    CHECK(options.depth.depth == 5);
    CHECK(options.count_bits == 3);
    CHECK(options.synchronizer_stages == 2);
    CHECK_FALSE(options.reset_handshake);
    // A frame's word and its tail mark cross, each way.
    if (cut.name.starts_with("cut/service/csr/")) {
      CHECK(cut.fields == std::vector<std::pair<std::string, unsigned>>{
                              {"data", register_frame_word_bits}, {"tail", 1}});
      CHECK(cut.payload_bits == 10);
    }
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

TEST_CASE("A sender slower than its logger keeps the configured word width", "[error-log]") {
  auto text = two_domains();
  // The periods of the two clocks turned round: the crossings' depth follows them too.
  std::ranges::find(text.clock_domains, "main", &ClockDomain::name)->period_ps = 3333;
  std::ranges::find(text.clock_domains, "slow", &ClockDomain::name)->period_ps = 1250;
  const auto plan = build_network_plan(text, crossed_logger());
  REQUIRE(plan);
  const auto &logged = plan.value().error_logs[0].logger;
  CHECK(error_record_word_bits(logged) == 32);
  // Without the periods the rule has nothing to go by.
  auto untimed = two_domains();
  for (auto &domain : untimed.clock_domains)
    domain.period_ps.reset();
  CHECK(rejection(build_network_plan(untimed, crossed_logger())).find("periods") !=
        std::string::npos);
}

TEST_CASE("A crossed log uses the periods of its clock inputs", "[error-log]") {
  auto d = two_domains();
  for (auto &domain : d.clock_domains) {
    domain.clock_root = domain.name;
    d.clock_roots.push_back({domain.name, domain.clock_port, domain.period_ps});
  }
  const auto outputs = [&](const Definition &cfg) {
    auto plan = build_network_plan(cfg, crossed_logger());
    REQUIRE(plan);
    auto emitted = emit_axi4_network(plan.value());
    REQUIRE(emitted);
    for (auto &file : emitted.value())
      if (file.path.ends_with(".map.json")) {
        auto map = nlohmann::json::parse(file.text);
        map.erase("clock_period_sources");
        file.text = map.dump();
      }
    return emitted.value();
  };
  const auto declared = outputs(d);
  for (auto &domain : d.clock_domains)
    domain.period_ps.reset();
  CHECK(outputs(d) == declared);
  d.clock_roots.back().period_ps.reset();
  CHECK(rejection(build_network_plan(d, crossed_logger())).find("known periods") !=
        std::string::npos);
}

TEST_CASE("A record's words carry the whole record", "[error-log]") {
  const auto plan = build_network_plan(two_domains(), crossed_logger());
  REQUIRE(plan);
  const auto &logged = plan.value().error_logs[0].logger;
  const auto bits = error_record_word_bits(logged);
  CHECK(bits == 32);
  const auto &shape = logged.shape;
  CHECK(bits * logged.serial_words >= shape.route_bits + 2 + shape.address_bits);
}

TEST_CASE("Serial crossings keep to plain clocks of one reset", "[error-log]") {
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
  kept.error_logs[0].whole_record = true;
  CHECK_FALSE(refused(two_domains(), kept).empty());
  auto apart = crossed_logger();
  apart.error_logs.push_back({"others", "csr", {"ram"}, {}});
  CHECK(refused(two_domains(), apart).find("share a domain") != std::string::npos);
}

TEST_CASE("A crossed logger leaves no combinational path into data traffic", "[error-log]") {
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

TEST_CASE("A clocked network keeps its banks in their target's domain unless told", "[error-log]") {
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

TEST_CASE("A configuration opens error logging on the same network", "[error-log]") {
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
  internal.service_targets["csr"] = error_logger_bank("csr", {1, 32, 0, 0, 2, 0});
  internal.error_logs.push_back(ErrorLogRequest{"errors", "csr", {"ram"}, {}});
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

TEST_CASE("Explicit same-domain logs match the omitted domain", "[error-log]") {
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

TEST_CASE("A configuration opens filters, chains and whole records", "[error-log]") {
  const auto chain = parsed_definition(logged_config(
      logged_window + "  audit_flags: [zeta, alpha]\n  logs:\n    - name: errors\n"
                      "      watch: [ram]\n      loggers:\n"
                      "        - {skip: [decode], match: {alpha: true}, pass_on: always}\n"
                      "        - {offset: '0x300'}\n",
      ", audit_flags: {zeta: 0, alpha: 1}", ""));
  CHECK(validate(chain));
  const auto plan = build_network_plan(chain);
  REQUIRE(plan);
  const auto &log = plan.value().error_logs.at(0).logger;
  CHECK(log.skipped_classes == std::vector<unsigned>{ClassDecode});
  CHECK(log.pass_always);
  CHECK(log.audit_match == std::vector<RecordFilterBit>{{1, true}});
  REQUIRE(log.chain.size() == 1);
  CHECK(plan.value().audit_flags == std::vector<std::string>{"zeta", "alpha"});
  const auto &banks = plan.value().target("csr").service->banks;
  REQUIRE(banks.size() == 2);
  CHECK(banks[0].base == 0);
  CHECK(banks[1].base == 0x300);
  CHECK(banks[0].plan.key == 0);
  CHECK(banks[1].plan.key == 1);
  CHECK(banks[0].branch == 0);
  CHECK(banks[1].branch == 0);
  CHECK(value_of(banks[0].plan, "capability_audit_flags") == 1);
  CHECK(value_of(banks[0].plan, "capability_next_logger") == 1);
  CHECK(value_of(banks[1].plan, "logger_index") == 1);
  // A logger without an offset takes the 64 bytes after the one before it.
  const auto placed = build_network_plan(parsed_definition(
      logged_config(logged_window + "  logs:\n    - name: errors\n      watch: [ram]\n"
                                    "      loggers: [{}, {offset: '0x300'}, {}]\n")));
  REQUIRE(placed);
  const auto &places = placed.value().target("csr").service->banks;
  REQUIRE(places.size() == 3);
  CHECK(places[0].base == 0);
  CHECK(places[1].base == 0x300);
  CHECK(places[2].base == 0x340);
  // An offset is a block of a bank.
  CHECK(rejection(build_network_plan(parsed_definition(logged_config(
                      logged_window + "  logs:\n    - name: errors\n      watch: [ram]\n"
                                      "      loggers: [{offset: '0x20'}]\n"))))
            .find("aligned and apart") != std::string::npos);
}

TEST_CASE("A configuration names its mistakes", "[error-log]") {
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

TEST_CASE("Record words cover exact field boundaries", "[error-log]") {
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
      CHECK(layout.fields == std::vector<RecordLayout::Field>{{"write", 1, 0},
                                                              {"wrap", 1, 1},
                                                              {"address_partial", 1, 2},
                                                              {"address", bits - 4, 3},
                                                              {"route", 1, bits - 1}});
      REQUIRE(lower_error_sender(plan, 0));
      REQUIRE(lower_error_logger(plan));
      CHECK(error_record_word_bits(plan) == width);
      ++plan.serial_words;
      CHECK_FALSE(lower_error_logger(plan));
    }
  }
}

TEST_CASE("Stored record fields omit only static or absent values", "[error-log]") {
  ErrorLoggerPlan plan;
  plan.shape = {5, 32, 7, 2, 8, 2};
  CHECK(error_record_layout(plan).bits == 60);
  plan.fixed_length = 3;
  CHECK(error_record_layout(plan).bits == 51);
  plan.shape.class_bits = 0;
  plan.shape.user_bits = 0;
  plan.shape.audit_bits = 0;
  CHECK(error_record_layout(plan).bits == 40);
}

TEST_CASE("Log word width is explicit in definitions and maps", "[error-log]") {
  for (const unsigned width : {8U, 16U, 32U}) {
    auto d = definition();
    d.targets[1].log_registers = true;
    d.error_logging.logs.push_back(ErrorLog{"errors", {"ram"}});
    d.error_logging.logs.back().serial_word_bits = width;
    const auto plan = build_network_plan(d);
    REQUIRE(plan);
    const auto &log = plan.value().error_logs.at(0).logger;
    CHECK(log.serial_word_bits == width);
    CHECK(log.serial_words == (40 + width - 1) / width);
  }
  auto internal = logger();
  internal.error_logs[0].serial_word_bits = 11;
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

TEST_CASE("The revision register holds a given value or the generator version", "[error-log]") {
  const auto config = [](const std::string &revision) {
    return logged_config(logged_window + revision + "  logs: [{name: errors, watch: [ram]}]\n");
  };
  // The network's RTL, and what the map states for the bank's revision field.
  struct Seen {
    std::string rtl, description;
    std::uint64_t value = 0;
    bool given = false;
    nlohmann::json registers = {};
    nlohmann::json capabilities = {};
  };
  const auto seen = [](const Definition &d) {
    const auto files = generate(d);
    REQUIRE(files);
    const auto map = nlohmann::json::parse(
        std::ranges::find(files.value(), "nocloom_o.map.json", &Artifact::path)->text);
    const auto &bank = map["definition"]["log_registers"]["csr"];
    Seen out{network_text(files.value()), "", 0, false, bank["registers"], bank["capabilities"]};
    // The map states the parts of the first word beside the registers.
    CHECK(bank["layout"] == 2);
    CHECK(bank["log_index"] == 0);
    CHECK(bank["logger_index"] == 0);
    unsigned found = 0, flags = 0;
    for (const auto &r : out.registers) {
      if (r["name"] == "revision") {
        ++found;
        out.value = r["value"].get<std::uint64_t>();
        out.description = r["description"].get<std::string>();
        CHECK(r["offset"] == 28);
        CHECK(r["lsb"] == 0);
        CHECK(r["width"] == 32);
        CHECK(r["access"] == "ro");
      }
      // The first word says which of the two the revision is.
      if (r["name"] == "capability_given_revision") {
        ++flags;
        out.given = r["value"].get<std::uint64_t>() != 0;
        CHECK(r["offset"] == 0);
        CHECK(r["lsb"] == 4);
        CHECK(r["width"] == 1);
        CHECK(r["access"] == "ro");
      }
    }
    CHECK(found == 1);
    CHECK(flags == 1);
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
  CHECK_FALSE(absent.given);
  CHECK(absent.capabilities == nlohmann::json::array());
  CHECK(absent.description.starts_with("Generator version"));
  std::set<std::string> networks{absent.rtl};
  // Every 32-bit value is a revision: 4294967295 is the largest.
  for (const std::uint32_t value : {0U, 0x5A3C01U, 0xFFFFFFU, 0x1000000U, 0xFFFFFFFFU}) {
    const auto parsed = parsed_definition(config("  revision: " + std::to_string(value) + "\n"));
    CHECK(parsed.error_logging.revision == value);
    // A definition built in C++ is the parsed one, and gives the same files.
    auto direct = plain;
    direct.error_logging.revision = value;
    CHECK(direct == parsed);
    CHECK(generate(direct).value() == generate(parsed).value());
    const auto given = seen(parsed);
    CHECK(given.value == value);
    CHECK(given.given);
    CHECK(given.capabilities == nlohmann::json::array({"given_revision"}));
    CHECK(given.description == "The definition's error_logging.revision");
    // Each value is a network of its own, and none is the network of the generator version.
    CHECK(networks.insert(given.rtl).second);
  }
  // The generator version given as a value: only the first word says that it was given.
  auto same = plain;
  same.error_logging.revision = generator;
  const auto stated = seen(same);
  CHECK(stated.value == generator);
  CHECK(stated.given);
  CHECK(stated.capabilities == nlohmann::json::array({"given_revision"}));
  CHECK(stated.rtl != absent.rtl);
  auto expected = absent.registers;
  for (auto &r : expected) {
    if (r["name"] == "capability_given_revision")
      r["value"] = 1;
    if (r["name"] == "revision")
      r["description"] = "The definition's error_logging.revision";
  }
  CHECK(stated.registers == expected);

  const auto refused = [](const auto &result, const std::string &code, const std::string &field) {
    REQUIRE_FALSE(result);
    CHECK(std::ranges::any_of(result.diagnostics(), [&](const Diagnostic &diagnostic) {
      return diagnostic.code == code && diagnostic.field == field;
    }));
  };
  // The largest value in a definition's text, and the same value built in C++.
  auto largest = plain;
  largest.error_logging.revision = 0xFFFFFFFFU;
  CHECK(validate(largest));
  const auto largest_text = config::parse(config("  revision: 4294967295\n"));
  REQUIRE(largest_text);
  CHECK(largest_text.value().definition == largest);
  CHECK(validate(largest_text.value().definition, {}, &largest_text.value().sources));
  // One more is no 32-bit value: the reader says so at the value's place in the text.
  const auto beyond = config::parse(config("  revision: 4294967296\n"));
  REQUIRE_FALSE(beyond);
  CHECK(beyond.diagnostics().front().code == "NCL_SCHEMA_TYPE");
  CHECK(beyond.diagnostics().front().field == "error_logging.revision");
  CHECK(beyond.diagnostics().front().message == "Integer is out of range");
  CHECK(beyond.diagnostics().front().location);
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

namespace {
// A configuration source, one target per log and the window: so many logs of so many loggers
// each, every bank at the next free place.
Definition window_of(unsigned logs, unsigned loggers,
                     const AddressMapping &window = {0x100000, 0x20000}) {
  Definition d;
  d.schema_version = 10;
  d.name = "o";
  d.address_width = 32;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk", "reset_n", "test"}};
  d.initiators = {{"cfg", "main"}};
  d.initiators[0].protocol = Protocol::Apb4;
  d.initiators[0].data_width = 32;
  for (unsigned k = 0; k < logs; ++k) {
    const auto ram = "ram" + std::to_string(k);
    d.targets.push_back({ram, "main", {{k * 0x1000ULL, 0x1000}}});
    d.error_logging.logs.push_back(
        {"log" + std::to_string(k), {ram}, std::nullopt, false, std::vector<ErrorLogger>(loggers)});
  }
  d.targets.push_back({"csr", "main", {window}});
  for (auto &t : d.targets) {
    t.protocol = Protocol::Apb4;
    t.data_width = 32;
    t.attributes = TargetAttributes::amba();
  }
  d.targets.back().log_registers = true;
  d.connectivity = Connectivity{.all = true};
  return d;
}
// The window's banks in a plan.
const std::vector<ServiceBank> &banks_of(const Result<NetworkPlan> &plan) {
  REQUIRE(plan);
  REQUIRE(plan.value().target("csr").service);
  return plan.value().target("csr").service->banks;
}
// A bank's first word: its fixed fields at their bits.
std::uint32_t id_word(const RegisterBankPlan &bank) {
  return std::accumulate(bank.fields.begin(), bank.fields.end(), std::uint32_t{0},
                         [](std::uint32_t word, const RegisterField &field) {
                           const auto bits = static_cast<std::uint32_t>(field.value << field.lsb);
                           return field.word == 0 ? word | bits : word;
                         });
}
// A plan refused with this code, at this path, in these words.
void refused_as(const Result<NetworkPlan> &plan, const std::string &field,
                const std::string &message) {
  REQUIRE_FALSE(plan);
  const auto &first = plan.diagnostics().front();
  CHECK(first.code == "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(first.field == field);
  CHECK(first.message == message);
}
const std::string window_path = "error_logging.registers.csr";
const std::string window_message = "A window holds its banks in an aligned block of at most 1 MiB";
const std::string places_message = "The banks of a window are aligned and apart";
} // namespace

namespace {
using BankOffsets = std::vector<std::optional<std::uint64_t>>;

Definition placement_window(std::uint64_t size, const std::vector<AddressMapping> &coverings,
                            const BankOffsets &offsets, unsigned narrow_bits,
                            unsigned wide_bits = 32, std::uint64_t origin = 0,
                            std::uint64_t local = 0) {
  auto d = window_of(1, static_cast<unsigned>(offsets.size()), {origin, size, local});
  d.targets[0].mappings = coverings.empty() ? std::vector<AddressMapping>{{0x4000, 4}} : coverings;
  d.initiators[0].address_width = wide_bits;
  auto narrow = d.initiators[0];
  narrow.name = "narrow";
  narrow.address_width = narrow_bits;
  d.initiators.push_back(narrow);
  for (std::size_t i = 0; i < offsets.size(); ++i)
    d.error_logging.logs[0].loggers[i].offset = offsets[i];
  return d;
}

std::vector<std::uint64_t> bases_of(const Result<NetworkPlan> &plan) {
  std::vector<std::uint64_t> bases;
  std::ranges::transform(banks_of(plan), std::back_inserter(bases), &ServiceBank::base);
  return bases;
}

std::vector<AddressMapping> hide_ids(std::uint64_t size, std::uint64_t origin = 0) {
  std::vector<AddressMapping> coverings;
  for (std::uint64_t at = 0; at < size; at += 64)
    coverings.push_back({origin + at, 4});
  return coverings;
}

std::vector<std::uint64_t> expected_placement(unsigned size,
                                              const std::vector<AddressMapping> &coverings,
                                              const BankOffsets &offsets,
                                              const std::array<unsigned, 2> &limits) {
  std::vector<std::uint64_t> sequential;
  auto next = std::uint64_t{0};
  for (const auto &offset : offsets) {
    sequential.push_back(offset.value_or(next));
    next = sequential.back() + 64;
  }
  // Mark the bytes owned by another mapping, then ask each source about each word.
  std::vector<bool> covered(size);
  for (const auto &mapping : coverings)
    for (auto byte = mapping.base; byte < mapping.base + mapping.size; ++byte)
      covered.at(byte) = true;
  if (std::ranges::none_of(limits, [&](unsigned limit) {
        return std::ranges::any_of(covered.begin(), covered.begin() + std::min(size, limit),
                                   [](bool byte) { return byte; });
      }))
    return sequential;

  std::vector<std::pair<unsigned, std::uint64_t>> ranked;
  for (unsigned pos = 0; pos < size; pos += 64) {
    if (std::ranges::find(offsets, std::optional<std::uint64_t>{pos}) != offsets.end())
      continue;
    unsigned full = 0, control = 0;
    for (unsigned limit : limits) {
      const auto readable = [&](unsigned offset, unsigned bytes) {
        return pos + offset + bytes <= limit &&
               std::ranges::none_of(covered.begin() + pos + offset,
                                    covered.begin() + pos + offset + bytes,
                                    [](bool byte) { return byte; });
      };
      full += readable(0, 64);
      control += readable(0, 4) && readable(4, 4);
    }
    if (control)
      ranked.emplace_back(full == limits.size() ? 0 : 1, pos);
  }
  const auto needed = std::ranges::count(offsets, std::optional<std::uint64_t>{});
  if (std::ssize(ranked) < needed)
    return sequential;
  std::ranges::sort(ranked);
  std::vector<std::uint64_t> chosen;
  for (const auto &[priority, pos] : std::span(ranked).first(static_cast<std::size_t>(needed)))
    chosen.push_back(pos);
  std::ranges::sort(chosen);
  auto pick = chosen.begin();
  for (std::size_t i = 0; i < offsets.size(); ++i)
    if (!offsets[i])
      sequential[i] = *pick++;
  return sequential;
}
} // namespace

TEST_CASE("Error logging requires one register window with one mapping", "[error-log]") {
  auto d = window_of(1, 1, {0x200000, 0x100000});
  REQUIRE(validate(d));
  auto &window = d.targets.back();
  auto field = window_path;
  std::string message;
  SECTION("two windows") {
    auto second = window;
    second.name = "csr_extra";
    second.mappings.front().base = 0x400000;
    d.targets.push_back(second);
    field = "error_logging.registers";
    message = "Expected one window of registers";
  }
  SECTION("two mappings") {
    window.mappings.push_back({0x400000, 0x100000, 0x100000});
    message = "A window has one unconditional mapping";
  }
  SECTION("a window larger than 1 MiB") {
    window.mappings.front().size = 0x200000;
    message = window_message;
  }
  const auto checked = validate(d);
  REQUIRE_FALSE(checked);
  const auto &diagnostic = checked.diagnostics().front();
  CHECK(diagnostic.code == "NCL_UNSUPPORTED_TOPOLOGY");
  CHECK(diagnostic.field == field);
  CHECK(diagnostic.message == message);
}

TEST_CASE("Default log placement selects a whole set of reachable banks", "[error-log]") {
  const auto check = [](const Definition &d, const std::vector<std::uint64_t> &bases) {
    CHECK(bases_of(build_network_plan(d)) == bases);
  };
  SECTION("A covered PATH leaves just enough positions for both banks") {
    check(placement_window(128, {{0x10, 4}}, {{}, {}}, 7), {0, 0x40});
    check(placement_window(128, {{0x1c, 4}}, {{}, {}}, 7), {0, 0x40});
  }
  SECTION("All IDs covered, including a bank with only read and clear") {
    auto covers = hide_ids(256);
    covers.push_back({8, 4});
    check(placement_window(256, covers, {{}}, 8), {0});
    auto live = hide_ids(256, 0x100000);
    live.push_back({0x100008, 4});
    const auto plan = build_network_plan(placement_window(256, live, {{}}, 31, 32, 0x100000));
    const auto &bank = banks_of(plan).at(0);
    CHECK(bank.base == 0);
    CHECK(bank.state_reachable);
    CHECK(bank.unreachable_words.at("cfg") == std::vector<unsigned>{0, 8});
  }
  SECTION("One complete site cannot displace either bank on fallback") {
    auto covers = hide_ids(512);
    covers.erase(covers.begin());
    covers.erase(covers.begin() + 3);
    covers.insert(covers.end(), {{4, 4}, {16, 4}});
    check(placement_window(512, covers, {{}, {}}, 9), {0, 0x40});
  }
  SECTION("Both banks keep their default offsets when no ID is reachable") {
    auto covers = hide_ids(512);
    covers.push_back({0x110, 4});
    const auto plan = build_network_plan(placement_window(512, covers, {{}, {}}, 9));
    CHECK(bases_of(plan) == std::vector<std::uint64_t>{0, 0x40});
    for (const auto &bank : banks_of(plan))
      CHECK(bank.state_reachable);
  }
  SECTION("Even the only position can have its STATE covered") {
    auto d = placement_window(64, {{4, 4}}, {{}}, 6);
    auto other = d.targets[0];
    other.name = "other";
    other.mappings = {{0x10, 4}};
    d.targets.push_back(other);
    const auto plan = build_network_plan(d);
    const auto &bank = banks_of(plan).at(0);
    CHECK(bank.base == 0);
    CHECK_FALSE(bank.state_reachable);
    CHECK(bank.unreachable_words.at("cfg") == std::vector<unsigned>{4, 0x10});
  }
  SECTION("Coverage outside every source address range leaves placement intact") {
    check(placement_window(256, {{0xc0, 4}}, {0x40, {}}, 6, 7), {0x40, 0x80});
  }
  SECTION("An uncovered window with a local base keeps its default") {
    check(placement_window(8192, {{0x3000, 4}}, {{}}, 16, 32, 0, 4096), {0});
  }
}

TEST_CASE("Log placement uses initiator width and reserves later explicit banks", "[error-log]") {
  const std::vector<AddressMapping> covers{{0, 4}, {0x9c, 4}};
  const auto plan = build_network_plan(placement_window(256, covers, {{}, {}}, 7));
  CHECK(bases_of(plan) == std::vector<std::uint64_t>{0x40, 0x80});
  const auto &partial = banks_of(plan).at(1);
  CHECK(banks_of(plan).at(0).unreachable_words.empty());
  CHECK(partial.state_reachable);
  CHECK(partial.unreachable_words.at("cfg") == std::vector<unsigned>{0x1c});
  CHECK(partial.unreachable_words.at("narrow") ==
        std::vector<unsigned>{0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60});

  const auto mixed = build_network_plan(placement_window(256, covers, {{}, 0x40, {}}, 8));
  CHECK(bases_of(mixed) == std::vector<std::uint64_t>{0x80, 0x40, 0xc0});
  auto split = placement_window(256, covers, {{}, 0x40}, 8);
  split.targets.insert(split.targets.begin() + 1, split.targets[0]);
  split.targets[1].name = "extra";
  split.targets[1].mappings = {{0x4000, 4}};
  split.error_logging.logs.push_back({"extra_log", {"extra"}, std::nullopt, false, {{}}});
  CHECK(bases_of(build_network_plan(split)) == std::vector<std::uint64_t>{0x80, 0x40, 0xc0});
}

TEST_CASE("Log placement follows the rule across random windows", "[error-log]") {
  std::mt19937 random(0xc05264);
  const auto draw = [&](unsigned upper) { return static_cast<unsigned>(random() % upper); };
  for (unsigned trial = 0; trial < 3000; ++trial) {
    const unsigned bits = 6U + draw(7);
    const unsigned size = 1U << bits;
    const unsigned count = 1U + draw(std::min(8U, size / 64));
    const unsigned narrow = 5U + draw(bits - 4);
    const unsigned wide = std::max(narrow + 1, bits);

    std::vector<unsigned> slots(size / 64);
    std::iota(slots.begin(), slots.end(), 0);
    std::shuffle(slots.begin(), slots.end(), random);
    slots.resize(count);
    std::ranges::sort(slots);
    BankOffsets offsets(count);
    for (unsigned i = 0; i < count; ++i)
      if (draw(2) == 0)
        offsets[i] = slots[i] * 64;

    std::vector<AddressMapping> coverings;
    const unsigned wanted = draw(5);
    for (unsigned attempt = 0; attempt < 12 && coverings.size() < wanted; ++attempt) {
      const unsigned bytes = 4U << (draw(std::min(5U, bits - 2)));
      const unsigned start = draw(size / bytes) * bytes;
      if (std::ranges::none_of(coverings, [&](const AddressMapping &other) {
            return start < other.base + other.size && other.base < start + bytes;
          }))
        coverings.push_back({start, bytes});
    }

    CAPTURE(trial, size, count, narrow, wide);
    const auto expected = expected_placement(size, coverings, offsets, {1U << narrow, 1U << wide});
    const auto plan = build_network_plan(placement_window(size, coverings, offsets, narrow, wide));
    const auto actual = bases_of(plan);
    CHECK(actual == expected);
    CHECK(std::set<std::uint64_t>(actual.begin(), actual.end()).size() == count);
    for (const auto at : actual)
      CHECK((at % 64 == 0 && at <= size - 64));
  }
}

TEST_CASE("A window has at most 64 logs", "[error-log]") {
  const auto plan = build_network_plan(window_of(64, 1));
  const auto &banks = banks_of(plan);
  REQUIRE(banks.size() == 64);
  REQUIRE(plan.value().error_logs.size() == 64);
  // Each log is a branch of the switch, its one bank 64 bytes after the bank before it.
  for (unsigned log = 0; log < 64; ++log) {
    CHECK(banks[log].branch == log);
    CHECK(banks[log].plan.key == 0);
    CHECK(banks[log].base == 64ULL * log);
    CHECK(plan.value().error_logs[log].bank == log);
    CHECK(id_word(banks[log].plan) == (0x02000000U | log << 16));
    CHECK(check_register_bank(banks[log].plan));
  }
  const auto outputs = std::ranges::count_if(
      plan.value().dependency.nodes, [](const auto &n) { return n.kind == "service.output"; });
  CHECK(outputs == 64);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
  const auto profile = service_outlet_profile(plan.value(), plan.value().target_index("csr"));
  REQUIRE(profile.places.size() == 64);
  CHECK(profile.places.back().branch == 63);
  CHECK(profile.places.back().base == 0xfc0);
  CHECK(lower_service_outlet(profile));
  refused_as(build_network_plan(window_of(65, 1)), window_path, "A window has at most 64 logs");
}

TEST_CASE("A log has at most 32 loggers", "[error-log]") {
  const auto plan = build_network_plan(window_of(1, 32));
  const auto &banks = banks_of(plan);
  REQUIRE(banks.size() == 32);
  for (unsigned place = 0; place < 32; ++place) {
    CHECK(banks[place].branch == 0);
    CHECK(banks[place].plan.key == place);
    CHECK(banks[place].base == 64ULL * place);
    // Every logger but the last states that one follows it.
    CHECK(id_word(banks[place].plan) == (0x02000000U | place << 8 | (place < 31 ? 8U : 0U)));
  }
  REQUIRE(plan.value().error_logs.size() == 1);
  CHECK(plan.value().error_logs[0].logger.chain.size() == 31);
  CHECK(plan.value().dependency_report.cycle_sets == 0);
  refused_as(build_network_plan(window_of(1, 33)), "error_logging.log0",
             "A log has at most 32 loggers");
  // The limit is each log's own.
  auto second = window_of(2, 32);
  second.error_logging.logs[1].loggers.emplace_back();
  refused_as(build_network_plan(second), "error_logging.log1", "A log has at most 32 loggers");
}

TEST_CASE("A register stage follows every eighth bank of a chain that goes on", "[error-log]") {
  // The register stages of a window's chains by the names of their places, the logs given by
  // their logger counts.
  const auto stages = [](const std::vector<unsigned> &logs) {
    auto d = window_of(static_cast<unsigned>(logs.size()), 1);
    for (std::size_t k = 0; k < logs.size(); ++k)
      d.error_logging.logs[k].loggers.resize(logs[k]);
    const auto plan = build_network_plan(d);
    REQUIRE(plan);
    const auto hardware = lower_axi4_exchange_composition(plan.value());
    REQUIRE(hardware);
    CHECK(verify_axi4_exchange_composition(hardware.value()));
    const std::string stem = "burst_service/csr/", mark = "/stage.valid";
    std::set<std::string> found;
    for (const auto &[name, id] : hardware.value().bindings)
      if (name.starts_with(stem) && name.ends_with(mark))
        found.insert(name.substr(stem.size(), name.size() - stem.size() - mark.size()));
    return found;
  };
  using Places = std::set<std::string>;
  CHECK(stages({1}).empty());
  CHECK(stages({8}).empty());
  CHECK(stages({9}) == Places{"stage1"});
  CHECK(stages({16}) == Places{"stage1"});
  CHECK(stages({17}) == Places{"stage1", "stage2"});
  CHECK(stages({24}) == Places{"stage1", "stage2"});
  CHECK(stages({25}) == Places{"stage1", "stage2", "stage3"});
  CHECK(stages({32}) == Places{"stage1", "stage2", "stage3"});
  // Each log's chain has its own stages.
  CHECK(stages({8, 9, 17}) == Places{"branch1/stage1", "branch2/stage1", "branch2/stage2"});
}

TEST_CASE("A window of 128 KiB holds 64 logs of 32 loggers", "[error-log]") {
  const auto plan = build_network_plan(window_of(64, 32));
  const auto &banks = banks_of(plan);
  REQUIRE(banks.size() == 2048);
  // The banks stand one after another: 32 of a log, then the next log's.
  for (unsigned k = 0; k < 2048; ++k)
    if (banks[k].base != 64ULL * k || banks[k].branch != k / 32 || banks[k].plan.key != k % 32)
      FAIL("bank " << k << " at " << banks[k].base << " on branch " << banks[k].branch
                   << " under key " << banks[k].plan.key);
  const auto &last = banks.back();
  CHECK(last.branch == 63);
  CHECK(last.plan.key == 31);
  CHECK(last.base == 0x1ffc0);
  CHECK(last.base + (1ULL << last.plan.address_bits) == 0x20000);
  // The ID word: layout 2, log 63, logger 31, and no capability.
  CHECK(value_of(last.plan, "layout") == 2);
  CHECK(value_of(last.plan, "log_index") == 63);
  CHECK(value_of(last.plan, "logger_index") == 31);
  CHECK(value_of(last.plan, "capability_next_logger") == 0);
  CHECK(id_word(last.plan) == 0x023f1f00U);
  CHECK(check_register_bank(last.plan));
  // The first bank states the logger after it.
  CHECK(id_word(banks.front().plan) == 0x02000008U);
  const auto profile = service_outlet_profile(plan.value(), plan.value().target_index("csr"));
  CHECK(profile.window_bits == 17);
  REQUIRE(profile.places.size() == 2048);
  CHECK(profile.places.back().base == 0x1ffc0);
  CHECK(profile.places.back().key == 31);
  CHECK(profile.places.back().branch == 63);
  // The window is full: a bank given the 64 bytes after its end stands past it.
  auto beyond = window_of(64, 32);
  beyond.error_logging.logs[63].loggers.pop_back();
  CHECK(banks_of(build_network_plan(beyond)).size() == 2047);
  beyond.error_logging.logs[63].loggers.push_back({.offset = 0x20000});
  refused_as(build_network_plan(beyond), window_path, window_message);
}

TEST_CASE("A window is an aligned block of 64 bytes to 1 MiB", "[error-log]") {
  // One bank is a window.
  const auto least = build_network_plan(window_of(1, 1, {0x100000, 0x40}));
  REQUIRE(banks_of(least).size() == 1);
  CHECK(banks_of(least)[0].base == 0);
  CHECK(service_outlet_profile(least.value(), least.value().target_index("csr")).window_bits == 6);
  refused_as(build_network_plan(window_of(1, 2, {0x100000, 0x40})), window_path, window_message);
  // 1 MiB is the largest: 4 KiB more is refused at a base that aligns to it, and so is the next
  // power of two.
  const auto largest = build_network_plan(window_of(1, 1, {0x100000, 0x100000}));
  REQUIRE(largest);
  CHECK(service_outlet_profile(largest.value(), largest.value().target_index("csr")).window_bits ==
        20);
  refused_as(build_network_plan(window_of(1, 1, {0x1010000, 0x101000})), window_path,
             window_message);
  refused_as(build_network_plan(window_of(1, 1, {0x200000, 0x200000})), window_path,
             window_message);
  // The base aligns to the size.
  refused_as(build_network_plan(window_of(1, 1, {0x110000, 0x20000})), window_path, window_message);
  CHECK(build_network_plan(window_of(1, 1, {0x120000, 0x20000})));
  // A bank lies inside the window: its last 64 bytes are a place, the 64 bytes after it are not.
  const auto placed = [](std::uint64_t offset,
                         const AddressMapping &window = {0x100000, 0x100000}) {
    auto d = window_of(1, 1, window);
    d.error_logging.logs[0].loggers[0].offset = offset;
    return build_network_plan(d);
  };
  REQUIRE(banks_of(placed(0xfffc0)).size() == 1);
  CHECK(banks_of(placed(0xfffc0))[0].base == 0xfffc0);
  refused_as(placed(0x100000), window_path, window_message);
  refused_as(placed(0xffffffffffffffc0ULL), window_path, window_message);
  CHECK(placed(0x1ffc0, {0x100000, 0x20000}));
  refused_as(placed(0x20000, {0x100000, 0x20000}), window_path, window_message);
  CHECK(placed(0xfc0, {0x100000, 0x1000}));
  refused_as(placed(0x1000, {0x100000, 0x1000}), window_path, window_message);
  // An offset is a multiple of 64, and two banks keep apart.
  refused_as(placed(0x20), window_path, places_message);
  refused_as(placed(0xfffe0), window_path, places_message);
  auto twice = window_of(1, 2);
  twice.error_logging.logs[0].loggers[1].offset = 0;
  refused_as(build_network_plan(twice), window_path, places_message);
}

TEST_CASE("The region index of a record has a register word", "[error-log]") {
  const auto plan = build_network_plan(definition(), logger());
  REQUIRE(plan);
  const auto request = logger().error_logs.at(0);
  REQUIRE(plan.value().route_index.target_ids.range_bits == 1);
  // Every count of regions that a plan can hold fits: the count is a 32-bit number.
  for (const unsigned bits : {1U, 16U, 17U, 32U}) {
    auto wide = plan.value();
    wide.route_index.target_ids.range_bits = bits;
    const auto planned = plan_error_log(wide, request);
    REQUIRE(planned);
    CHECK(planned.value().logger.shape.region_bits == bits);
    const auto bank = error_logger_bank("bank", planned.value().logger.shape);
    CHECK(check_register_bank(bank));
    const auto field = std::ranges::find(bank.fields, "record_region_index", &RegisterField::name);
    REQUIRE(field != bank.fields.end());
    CHECK(field->word == 5);
    CHECK(field->lsb == 0);
    CHECK(field->width == bits);
  }
}
