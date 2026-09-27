#include "core/hardware.hpp"
#include "core/hardware_builder.hpp"
#include "core/ir_component.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <fstream>
#include <future>
#include <iostream>
#include <limits>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <nocloom/io.hpp>
#include <sstream>
#include <tuple>
#include <vector>

namespace {
nocloom::Target apb_target(std::string name, nocloom::AddressWindow window) {
  nocloom::Target t{std::move(name), "main", {{window.base, window.size}}};
  t.protocol = nocloom::Protocol::Apb4;
  t.attributes = nocloom::TargetAttributes::amba();
  t.data_width = 32;
  return t;
}
nocloom::Definition example() {
  nocloom::Definition d;
  d.name = "peripheral_bus";
  d.connectivity = nocloom::Connectivity{.all = true};
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.initiators = {{"control", "main"}};
  d.initiators[0].protocol = nocloom::Protocol::Apb4;
  d.initiators[0].data_width = 32;
  d.targets = {apb_target("registers", {0x1000, 0x100}), apb_target("counters", {0x2000, 0x80}),
               apb_target("scratch", {0xfffff000, 0x1000})};
  return d;
}
// The YAML form of example().
std::string yaml() {
  return R"(schema_version: 10
name: peripheral_bus
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - name: main
    clock_port: clk_main
    reset_port: reset_n
    test_port: test
initiators:
  - name: control
    clock_domain: main
    protocol: apb4
    data_width: 32
targets:
  - name: registers
    clock_domain: main
    protocol: apb4
    attributes: amba
    data_width: 32
    mappings: [{base: "0x1000", size: "0x100"}]
  - name: counters
    clock_domain: main
    protocol: apb4
    attributes: amba
    data_width: 32
    mappings: [{base: "0x2000", size: "0x80"}]
  - name: scratch
    clock_domain: main
    protocol: apb4
    attributes: amba
    data_width: 32
    mappings: [{base: "0xfffff000", size: "0x1000"}]
connectivity: all
output:
  backend: verilog
  language: verilog_2001
)";
}
// A small single-domain IR: clock and reset, a 2-bit input, two 2-bit
// registers and two outputs; the last value is a binary operator.
nocloom::detail::HardwareIR probe_ir() {
  using namespace nocloom::detail;
  Builder b;
  b.ir.name = "nocloom_probe";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto in = b.input("nl_in", 2, "probe", "in");
  auto first = b.state(2, clock, reset);
  auto second = b.state(2, clock, reset);
  b.update(first, in);
  b.update(second, first);
  b.output("nl_first", first, "probe", "first");
  b.output("nl_mixed", b.binary(Op::BitOr, b.binary(Op::BitAnd, second, in), b.constant(2, 1)),
           "probe", "mixed");
  return b.ir;
}
std::string replaced(std::string text, const std::string &from, const std::string &to) {
  auto pos = text.find(from);
  REQUIRE(pos != std::string::npos);
  text.replace(pos, from.size(), to);
  return text;
}
template <typename T> void fails(const nocloom::Result<T> &result, const std::string &code) {
  REQUIRE_FALSE(result);
  REQUIRE(std::any_of(result.diagnostics().begin(), result.diagnostics().end(),
                      [&](const auto &d) { return d.code == code; }));
  REQUIRE_THROWS_AS(result.value(), std::bad_variant_access);
}
} // namespace
TEST_CASE("YAML and direct API produce identical artifacts") {
  auto p = nocloom::config::parse(yaml(), "logical_input");
  REQUIRE(p);
  auto a = nocloom::generate(p.value().definition, p.value().options, &p.value().sources);
  auto b = nocloom::generate(example());
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  CHECK(a.value().size() == 6);
  for (const auto &artifact : a.value())
    CHECK(artifact.text.find("logical_input") == std::string::npos);
  auto d = example();
  std::reverse(d.targets.begin(), d.targets.end());
  auto reversed = nocloom::generate(d);
  REQUIRE(reversed);
  CHECK(reversed.value() == b.value());
  SECTION("a single domain takes any legal name") {
    auto renamed = [](std::string text) {
      text = replaced(text, "- name: main\n    clock_port: clk_main",
                      "- name: fabric\n    clock_port: clk_fabric");
      for (std::string::size_type at = 0;
           (at = text.find("clock_domain: main", at)) != std::string::npos;)
        text.replace(at, 18, "clock_domain: fabric");
      return text;
    };
    auto named = nocloom::config::parse(renamed(yaml()), "renamed.yaml");
    REQUIRE(named);
    auto e = example();
    e.clock_domains = {{"fabric", "clk_fabric", "reset_n", "test"}};
    for (auto &initiator : e.initiators)
      initiator.clock_domain = "fabric";
    for (auto &target : e.targets)
      target.clock_domain = "fabric";
    auto x =
        nocloom::generate(named.value().definition, named.value().options, &named.value().sources);
    auto y = nocloom::generate(e);
    REQUIRE(x);
    REQUIRE(y);
    CHECK(x.value() == y.value());
    auto text = [&](const std::string &suffix) {
      return std::ranges::find_if(x.value(),
                                  [&](const auto &artifact) {
                                    return artifact.path.ends_with(suffix) &&
                                           artifact.path != "nocloom_clock_gate.v" &&
                                           artifact.path != "nocloom_synchronizer.v";
                                  })
          ->text;
    };
    CHECK(text(".v").find("input wire clk_fabric,") != std::string::npos);
    CHECK(text(".map.json").find("\"domain\": \"fabric\"") != std::string::npos);
    CHECK(text(".map.json").find("\"name\": \"domain/fabric/init\"") != std::string::npos);
  }
}
TEST_CASE("Reset ports and their release parse from YAML like the direct API") {
  const auto edge =
      replaced(yaml(), "reset_ports: [{name: reset_n}]",
               "reset_ports: [{name: reset_n, release: on_clock_edge, release_domain: main}]");
  auto p = nocloom::config::parse(edge, "edge.yaml");
  REQUIRE(p);
  auto d = example();
  d.reset_ports[0] = {"reset_n", nocloom::ResetRelease::OnClockEdge, "main"};
  CHECK(p.value().definition.reset_ports == d.reset_ports);
  auto a = nocloom::generate(p.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  // The port resets the flip-flops directly: no release synchronizer.
  auto text = [](const nocloom::ArtifactSet &set, const std::string &suffix) {
    return std::ranges::find_if(set,
                                [&](const auto &x) {
                                  return x.path.ends_with(suffix) &&
                                         x.path != "nocloom_clock_gate.v" &&
                                         x.path != "nocloom_synchronizer.v";
                                })
        ->text;
  };
  auto count = [](const std::string &rtl) {
    std::size_t n = 0;
    for (auto at = rtl.find("nocloom_synchronizer #(.STAGES(2))"); at != std::string::npos;
         at = rtl.find("nocloom_synchronizer #(", at + 1))
      ++n;
    return n;
  };
  auto resynchronized = nocloom::generate(example());
  REQUIRE(resynchronized);
  CHECK(count(text(a.value(), ".v")) == 0);
  CHECK(count(text(resynchronized.value(), ".v")) == 1);
  CHECK(text(a.value(), ".map.json").find("\"release\": \"on_clock_edge\"") != std::string::npos);
  fails(nocloom::config::parse(
            replaced(yaml(), "{name: reset_n}", "{name: reset_n, release: late}"), "bad.yaml"),
        "NCL_RESET_PORT");
  fails(nocloom::config::parse(
            replaced(yaml(), "{name: reset_n}", "{name: reset_n, polarity: low}"), "bad.yaml"),
        "NCL_SCHEMA_UNKNOWN_FIELD");
  fails(
      nocloom::config::parse(replaced(yaml(), "reset_ports: [{name: reset_n}]\n", ""), "bad.yaml"),
      "NCL_SCHEMA_MISSING_FIELD");
  fails(nocloom::config::parse(replaced(yaml(), "    clock_port: clk_main\n", ""), "bad.yaml"),
        "NCL_SCHEMA_MISSING_FIELD");
}
TEST_CASE("A test port that no domain names is an input without load") {
  auto p = nocloom::config::parse(
      replaced(yaml(), "test_ports: [{name: test}]", "test_ports: [{name: test}, {name: spare}]"),
      "spare.yaml");
  REQUIRE(p);
  auto d = example();
  d.test_ports.push_back({"spare"});
  CHECK(p.value().definition.test_ports == d.test_ports);
  auto a = nocloom::generate(p.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  auto text = [&](const std::string &path) {
    return std::ranges::find(a.value(), path, &nocloom::Artifact::path)->text;
  };
  CHECK(text("nocloom_peripheral_bus.map.json").find("\"endpoint\": \"test:spare\"") !=
        std::string::npos);
  const auto rtl = text("nocloom_peripheral_bus.v");
  const auto at = rtl.find("input wire spare,");
  REQUIRE(at != std::string::npos);
  CHECK(rtl.find("spare", at + 17) == std::string::npos);
  d.reset_ports.push_back({"spare_n"});
  fails(nocloom::make_plan(d), "NCL_RESET_PORT");
}
TEST_CASE("Unsupported topology and direct API values cannot bypass validation") {
  auto d = example();
  SECTION("protocol") {
    d.initiators[0].protocol = static_cast<nocloom::Protocol>(42);
    fails(nocloom::generate(d), "NCL_UNSUPPORTED_PROTOCOL");
  }
  SECTION("data") {
    d.initiators[0].data_width = 64;
    fails(nocloom::make_plan(d), "NCL_ENDPOINT_DATA_WIDTH");
  }
  SECTION("address") {
    d.address_width = 64;
    fails(nocloom::make_plan(d), "NCL_ADDRESS_WIDTH");
  }
  SECTION("initiators") {
    for (int i = 0; i < 256; ++i) {
      d.initiators.push_back(d.initiators.front());
      d.initiators.back().name = "control" + std::to_string(i);
    }
    d.connectivity = nocloom::Connectivity{true, {}};
    fails(nocloom::make_plan(d), "NCL_UNSUPPORTED_TOPOLOGY");
  }
  SECTION("targets") {
    for (int i = 0; i < 256; ++i) {
      d.targets.push_back(d.targets.front());
      d.targets.back().name = "bank" + std::to_string(i);
      d.targets.back().mappings = {{0x100000 + 0x1000 * static_cast<std::uint64_t>(i), 0x1000}};
    }
    d.connectivity = nocloom::Connectivity{true, {}};
    fails(nocloom::make_plan(d), "NCL_UNSUPPORTED_TOPOLOGY");
  }
  SECTION("domains") {
    d.clock_domains.push_back(d.clock_domains.front());
    fails(nocloom::make_plan(d), "NCL_UNSUPPORTED_TOPOLOGY");
  }
  SECTION("no targets") {
    d.targets.clear();
    fails(nocloom::make_plan(d), "NCL_UNSUPPORTED_TOPOLOGY");
  }
  SECTION("unknown reset port") {
    d.clock_domains[0].reset_port = "other_n";
    fails(nocloom::make_plan(d), "NCL_DOMAIN_REFERENCE");
  }
  SECTION("unused reset port") {
    d.reset_ports.push_back({"spare_n"});
    fails(nocloom::make_plan(d), "NCL_RESET_PORT");
  }
  SECTION("edge release without its domain") {
    d.reset_ports[0].release = nocloom::ResetRelease::OnClockEdge;
    fails(nocloom::make_plan(d), "NCL_RESET_PORT");
  }
  SECTION("release domain without an edge release") {
    d.reset_ports[0].release_domain = "main";
    fails(nocloom::make_plan(d), "NCL_RESET_PORT");
  }
  SECTION("edge release of another domain") {
    d.reset_ports[0] = {"reset_n", nocloom::ResetRelease::OnClockEdge, "other"};
    fails(nocloom::make_plan(d), "NCL_RESET_PORT");
  }
  SECTION("clock and reset share a port name") {
    d.clock_domains[0].clock_port = "reset_n";
    fails(nocloom::make_plan(d), "NCL_NAME_COLLISION");
  }
  SECTION("reserved port name") {
    d.clock_domains[0].clock_port = "nl_clk_main";
    fails(nocloom::make_plan(d), "NCL_NAME_INVALID");
  }
  SECTION("reference") {
    d.targets[0].clock_domain = "other";
    fails(nocloom::make_plan(d), "NCL_DOMAIN_REFERENCE");
  }
  SECTION("name") {
    d.targets[0].name = "module";
    fails(nocloom::make_plan(d), "NCL_NAME_INVALID");
  }
  SECTION("duplicate") {
    d.targets[1].name = d.targets[0].name;
    fails(nocloom::make_plan(d), "NCL_NAME_COLLISION");
  }
  SECTION("backend") {
    fails(nocloom::generate(d, {static_cast<nocloom::Backend>(7)}), "NCL_UNSUPPORTED_BACKEND");
  }
}
TEST_CASE("Address arithmetic checks before addition") {
  auto d = example();
  SECTION("overflow") {
    d.targets[0].mappings = {{1, std::numeric_limits<std::uint64_t>::max()}};
    fails(nocloom::make_plan(d), "NCL_ADDRESS_RANGE");
  }
  SECTION("zero") {
    d.targets[0].mappings[0].size = 0;
    fails(nocloom::make_plan(d), "NCL_ADDRESS_RANGE");
  }
  SECTION("end") {
    d.targets[2].mappings[0].size += 1;
    fails(nocloom::make_plan(d), "NCL_ADDRESS_RANGE");
  }
  SECTION("base") {
    d.targets[0].mappings[0].base = std::numeric_limits<std::uint64_t>::max();
    fails(nocloom::make_plan(d), "NCL_ADDRESS_RANGE");
  }
  SECTION("overlap") {
    d.targets[1].mappings[0] = {0x1000, 0x100};
    fails(nocloom::make_plan(d), "NCL_ADDRESS_OVERLAP");
  }
  SECTION("full space") {
    for (unsigned width : {2U, 8U, 16U, 31U, 32U}) {
      d.address_width = width;
      d.targets = {apb_target("registers", {0, std::uint64_t{1} << width})};
      REQUIRE(nocloom::generate(d));
    }
  }
}
TEST_CASE("YAML rejects ambiguity and unsupported syntax") {
  const auto text = yaml();
  for (const auto &[from, to, code] : std::vector<std::array<std::string, 3>>{
           {"name: peripheral_bus", "name: peripheral_bus\nname: duplicate",
            "NCL_SCHEMA_DUPLICATE_KEY"},
           {"protocol: apb4", "protocol: apb5", "NCL_UNSUPPORTED_PROTOCOL"},
           {"address_width: 32", "address_width: '32'", "NCL_SCHEMA_TYPE"},
           {"address_width: 32", "address_width: 32.0", "NCL_SCHEMA_TYPE"},
           {"address_width: 32", "address_width: true", "NCL_SCHEMA_TYPE"},
           {"address_width: 32", "address_width: 0x20", "NCL_SCHEMA_TYPE"},
           {"name: peripheral_bus", "name: true", "NCL_SCHEMA_TYPE"},
           {"protocol: apb4", "protocol: !!str apb4", "NCL_SCHEMA_YAML_FEATURE"},
           {"protocol: apb4", "protocol: &p apb4", "NCL_SCHEMA_YAML_FEATURE"},
           {"protocol: apb4", "protocol: *p", "NCL_SCHEMA_SYNTAX"},
           {"protocol: apb4", "protocol: null", "NCL_SCHEMA_TYPE"},
           {"protocol: apb4", "protocol: apb4\n    pipeline: true", "NCL_SCHEMA_UNKNOWN_FIELD"},
           {"\"0x1000\"", "4096", "NCL_SCHEMA_TYPE"},
           {"\"0x1000\"", "\"-1\"", "NCL_ADDRESS_RANGE"},
           {"\"0x1000\"", "\"18446744073709551616\"", "NCL_ADDRESS_RANGE"},
           {"\"0x1000\"", "\"0x1_000\"", "NCL_ADDRESS_RANGE"},
           {"size: \"0x100\"", "size: \"0x100\", base: \"0\"", "NCL_SCHEMA_DUPLICATE_KEY"},
           {"backend: verilog", "backend: zaozi", "NCL_UNSUPPORTED_BACKEND"},
           {"language: verilog_2001", "language: systemverilog", "NCL_UNSUPPORTED_BACKEND"}}) {
    INFO(to);
    fails(nocloom::config::parse(replaced(text, from, to)), code);
  }
  fails(nocloom::config::parse(text + "\n---\nname: second\n"), "NCL_SCHEMA_YAML_FEATURE");
  fails(nocloom::config::parse("? [a, b]\n: c\n"), "NCL_SCHEMA_TYPE");
  fails(nocloom::config::parse("<<: {}\n"), "NCL_SCHEMA_YAML_FEATURE");
  fails(nocloom::config::parse(
            replaced(text, "mappings: [{base: \"0x1000\", size: \"0x100\"}]",
                     "mappings:\n      - base: |\n          0x1000\n        size: \"0x100\"")),
        "NCL_SCHEMA_TYPE");
}
TEST_CASE("YAML source diagnostics and parse resource gates") {
  auto p = nocloom::config::parse(replaced(yaml(), "data_width: 32", "data_width: 64"), "example");
  REQUIRE(p);
  auto status = nocloom::validate(p.value().definition, {}, &p.value().sources);
  fails(status, "NCL_ENDPOINT_DATA_WIDTH");
  REQUIRE(status.diagnostics()[0].location);
  CHECK(status.diagnostics()[0].location->line == 15);
  CHECK(status.diagnostics()[0].location->label == "example");
  fails(nocloom::config::parse(std::string(nocloom::config::max_input_bytes + 1, ' ')),
        "NCL_RESOURCE_LIMIT");
  fails(nocloom::config::parse(std::string(10000, '[') + "x" + std::string(10000, ']')),
        "NCL_RESOURCE_LIMIT");
}
TEST_CASE("YAML size limit accepts its last byte and rejects the next") {
  using nocloom::config::max_input_bytes;
  const auto padded = [](std::size_t size) {
    auto text = yaml() + "\n#";
    REQUIRE(text.size() <= size);
    text.append(size - text.size(), 'x');
    return text;
  };
  const auto at_limit = padded(max_input_bytes);
  CHECK(at_limit.size() == max_input_bytes);
  CHECK(nocloom::config::parse(at_limit));
  const auto over = nocloom::config::parse(padded(max_input_bytes + 1));
  fails(over, "NCL_RESOURCE_LIMIT");
  CHECK(over.diagnostics().front().message.find(std::to_string(max_input_bytes / (1024 * 1024)) +
                                                " MiB") != std::string::npos);
  // The file adapter reads up to the same limit: written into the unit working directory of the
  // build tree.
  const std::string name = "nocloom-yaml-limit.yaml";
  const auto load = [&](const std::string &text) {
    std::ofstream(name, std::ios::binary) << text;
    auto result = nocloom::io::load_definition(name);
    const auto work = std::filesystem::canonical(std::filesystem::current_path());
    REQUIRE_FALSE(std::filesystem::is_symlink(name));
    REQUIRE(std::filesystem::weakly_canonical(work / name).parent_path() == work);
    std::filesystem::remove(name);
    return result;
  };
  CHECK(load(at_limit));
  fails(load(padded(max_input_bytes + 1)), "NCL_RESOURCE_LIMIT");
}
TEST_CASE("YAML node limit accepts its last node and rejects the next") {
  using nocloom::config::max_nodes;
  // A sequence counts one node and each item one. A second document fails after the node count of
  // the first and before the tree is built, so the two codes tell the limit from what follows it.
  const auto nodes = [](std::size_t count) {
    std::string text;
    for (std::size_t i = 1; i < count; ++i)
      text += "- a\n";
    return text + "---\nb\n";
  };
  REQUIRE(nodes(max_nodes + 1).size() <= nocloom::config::max_input_bytes);
  fails(nocloom::config::parse(nodes(max_nodes)), "NCL_SCHEMA_YAML_FEATURE");
  fails(nocloom::config::parse(nodes(max_nodes + 1)), "NCL_RESOURCE_LIMIT");
}
TEST_CASE("Plan owns its snapshot and concurrent calls are isolated") {
  auto d = example();
  auto plan = nocloom::make_plan(d);
  REQUIRE(plan);
  auto expected = nocloom::emit(plan.value());
  REQUIRE(expected);
  d.name = "changed";
  d.targets.clear();
  CHECK(nocloom::emit(plan.value()).value() == expected.value());
  std::vector<std::future<nocloom::Result<nocloom::ArtifactSet>>> calls;
  for (int i = 0; i < 8; ++i)
    calls.push_back(std::async(std::launch::async, [&] { return nocloom::emit(plan.value()); }));
  for (auto &call : calls) {
    auto result = call.get();
    REQUIRE(result);
    CHECK(result.value() == expected.value());
  }
  auto moved = std::move(plan.value());
  fails(nocloom::emit(plan.value()), "NCL_INTERNAL_PLAN");
  REQUIRE(nocloom::emit(moved));
}
TEST_CASE("Generation has no console side effects on success or configuration failure") {
  std::ostringstream output, errors;
  struct Restore {
    std::streambuf *output;
    std::streambuf *errors;
    ~Restore() {
      std::cout.rdbuf(output);
      std::cerr.rdbuf(errors);
    }
  };
  bool success = false, rejected = false;
  {
    Restore restore{std::cout.rdbuf(output.rdbuf()), std::cerr.rdbuf(errors.rdbuf())};
    success = static_cast<bool>(nocloom::generate(example()));
    auto invalid = example();
    invalid.targets.clear();
    rejected = !nocloom::generate(invalid);
  }
  CHECK(success);
  CHECK(rejected);
  CHECK(output.str().empty());
  CHECK(errors.str().empty());
}
TEST_CASE("Hardware verifier rejects corrupted representations") {
  auto ir = probe_ir();
  REQUIRE(nocloom::detail::verify(ir));
  SECTION("forward reference") { ir.values.back().operands[0] = {ir.values.size()}; }
  SECTION("width") { ir.values.back().width = 0; }
  SECTION("constant") {
    auto it = std::find_if(ir.values.begin(), ir.values.end(),
                           [](const auto &v) { return v.op == nocloom::detail::Op::Constant; });
    it->literal = std::numeric_limits<std::uint64_t>::max();
  }
  SECTION("missing driver") { ir.drivers.pop_back(); }
  SECTION("duplicate driver") { ir.drivers.push_back(ir.drivers.back()); }
  SECTION("unknown op") { ir.values.back().op = static_cast<nocloom::detail::Op>(100); }
  SECTION("binding") {
    ir.ports[1].endpoint = ir.ports[0].endpoint;
    ir.ports[1].signal = ir.ports[0].signal;
  }
  fails(nocloom::detail::verify(ir), "NCL_INTERNAL_IR");
}
TEST_CASE("slice and concat operators verify and emit", "[hardware-ops]") {
  // The burst composition's lane framing uses these two operators; pin the
  // structural rules and the emitted text.
  using namespace nocloom::detail;
  Builder b;
  b.ir.name = "nocloom_ops";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto wide = b.input("nl_wide", 64, "probe", "wide");
  b.output("nl_low", b.slice(wide, 0, 32), "probe", "low");
  b.output("nl_high", b.slice(wide, 32, 32), "probe", "high");
  b.output("nl_joined", b.concat(b.slice(wide, 32, 32), b.slice(wide, 0, 32)), "probe", "joined");
  b.output("nl_bit", b.slice(wide, 7, 1), "probe", "bit");
  auto single = b.input("nl_single", 1, "probe", "single");
  b.output("nl_single_out", b.slice(single, 0, 1), "probe", "single_out");
  b.output("nl_bit_of_bit", b.slice(b.slice(wide, 7, 1), 0, 1), "probe", "bit_of_bit");
  auto held = b.state(64, clock, reset);
  b.update(held, wide);
  REQUIRE(verify_structure(b.ir));
  const auto text = emit_verilog(b.ir);
  REQUIRE(text.find("[31:0]") != std::string::npos);
  REQUIRE(text.find("[63:32]") != std::string::npos);
  REQUIRE(text.find("[7:7]") != std::string::npos);
  // Verilog-2001 has no part select of a scalar: a one-bit net is used whole.
  REQUIRE(text.find("[0:0]") == std::string::npos);
  // Values: 0 clock, 1 reset, 2 wide, 3 const 0, 4 slice low, 5 const 32,
  // 6 slice high, 7..10 concat legs, 11 concat, 12..13 bit slice, 14 state.
  const auto slice_low = b.ir.values[4];
  REQUIRE(slice_low.op == Op::Slice);
  REQUIRE(slice_low.operands[1].index == 3);
  SECTION("slice offset must be a constant") {
    b.ir.values[4].operands[1] = wide;
    fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
  }
  SECTION("slice must stay inside the operand") {
    b.ir.values[3].literal = 40;
    fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
  }
  SECTION("concat width is the operand sum") {
    REQUIRE(b.ir.values[11].op == Op::Concat);
    b.ir.values[11].width = 63;
    fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
  }
}
TEST_CASE("I/O preflight rejects traversal and duplicates before creating output") {
  fails(nocloom::io::write_artifacts({{"../bad.v", ""}}, "preflight-output"), "NCL_IO_PATH");
  fails(nocloom::io::write_artifacts({{"same.v", ""}, {"same.v", ""}}, "preflight-output"),
        "NCL_IO_PATH");
  CHECK_FALSE(std::filesystem::exists("preflight-output"));
}

TEST_CASE("State verification rejects invalid sequential hardware") {
  using namespace nocloom::detail;
  auto ir = probe_ir();
  REQUIRE(verify(ir));
  REQUIRE(ir.states.size() == 2);
  SECTION("undefined next") { ir.states[0].next.index = ir.values.size(); }
  SECTION("width mismatch") { ir.states[0].next = ir.states[0].clock; }
  SECTION("reset overflow") { ir.states[0].reset.value().value = 4; }
  SECTION("wrong clock role") { ir.states[0].clock = ir.states[0].reset.value().signal; }
  SECTION("combinational clock") { ir.states[0].clock = ir.states[0].output; }
  SECTION("duplicate state output") { ir.states[1].output = ir.states[0].output; }
  SECTION("undefined state") {
    ir.values[ir.states[0].output.index].state.index = ir.states.size();
  }
  fails(verify(ir), "NCL_INTERNAL_IR");
}

TEST_CASE("Component import preserves slice and concatenation widths", "[hardware-ops]") {
  using namespace nocloom::detail;
  Builder source;
  source.ir.name = "nocloom_import_source";
  auto wide = source.input("nl_wide", 64, "probe", "wide");
  auto low = source.slice(wide, 3, 17);
  auto high = source.slice(wide, 32, 32);
  auto joined = source.concat(high, low);
  REQUIRE(verify_structure(source.ir));
  Builder copy;
  copy.ir.name = "nocloom_import_copy";
  auto input = copy.input("nl_wide", 64, "probe", "wide");
  IrComponent importer(copy, source.ir);
  importer.replace(wide, input);
  auto imported_low = importer.get(low);
  auto imported_joined = importer.get(joined);
  importer.finish();
  CHECK(copy.ir.values.at(imported_low.index).width == 17);
  CHECK(copy.ir.values.at(imported_joined.index).width == 49);
  const auto &slice = copy.ir.values.at(imported_low.index);
  CHECK(slice.op == Op::Slice);
  CHECK(copy.ir.values.at(slice.operands[1].index).literal == 3);
  REQUIRE(verify_structure(copy.ir));
  SECTION("inconsistent explicit concatenation resizing is rejected") {
    CHECK_THROWS_AS(importer.get(joined, 48), std::logic_error);
  }
  SECTION("nonconstant source slice offsets are rejected") {
    source.ir.values.at(low.index).operands[1] = wide;
    IrComponent invalid(copy, source.ir);
    invalid.replace(wide, input);
    CHECK_THROWS_AS(invalid.get(low), std::logic_error);
  }
}

TEST_CASE("Hardware literal and reset limits include the 64-bit literals in wider containers",
          "[hardware-ops]") {
  using namespace nocloom::detail;
  for (unsigned width : {1U, 32U, 63U, 64U, 65U, 128U, 256U, 512U, 1024U}) {
    const auto maximum =
        width >= 64 ? std::numeric_limits<std::uint64_t>::max() : (std::uint64_t{1} << width) - 1;
    for (auto value : {std::uint64_t{0}, std::uint64_t{1}, maximum}) {
      CAPTURE(width, value);
      Builder b;
      b.ir.name = "nocloom_literal_limits";
      const auto clock = b.input("nl_clock", 1, "domain:main", "clock");
      const auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
      b.output("nl_constant", b.constant(width, value), "probe", "constant");
      b.output("nl_state", b.state(width, clock, reset, value), "probe", "state");
      REQUIRE(verify_structure(b.ir));
      const auto rtl = emit_verilog(b.ir);
      CHECK(rtl.find(std::to_string(width) + "'d" + std::to_string(value)) != std::string::npos);
    }
    if (width < 64) {
      Builder b;
      b.ir.name = "nocloom_literal_overflow";
      const auto clock = b.input("nl_clock", 1, "domain:main", "clock");
      const auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
      const auto constant = b.constant(width, std::uint64_t{1} << width);
      b.output("nl_constant", constant, "probe", "constant");
      fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
      b.ir.values[constant.index].literal = 0;
      const auto state = b.state(width, clock, reset, std::uint64_t{1} << width);
      b.output("nl_state", state, "probe", "state");
      fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
    }
  }
  Builder wider;
  wider.ir.name = "nocloom_width_limit";
  wider.output("nl_constant", wider.constant(1025, 0), "probe", "constant");
  fails(verify_structure(wider.ir), "NCL_INTERNAL_IR");
}

TEST_CASE("Unknown configuration fields are rejected with their path", "[config]") {
  const std::string text = yaml();
  REQUIRE(nocloom::config::parse(text, "unknown.yaml"));
  for (const auto &[from, to, field] :
       std::vector<std::tuple<std::string, std::string, std::string>>{
           {"    reset_port: reset_n\n", "    reset_port: reset_n\n    unknown_option: 1\n",
            "clock_domains[0].unknown_option"},
           {"    protocol: apb4\n    data_width: 32\ntargets:\n",
            "    protocol: apb4\n    data_width: 32\n    unknown_option: 1\ntargets:\n",
            "initiators[0].unknown_option"},
           {"    mappings: [{base: \"0x1000\", size: \"0x100\"}]\n",
            "    mappings: [{base: \"0x1000\", size: \"0x100\"}]\n    unknown_option: 1\n",
            "targets[0].unknown_option"}}) {
    INFO(field);
    const auto parsed = nocloom::config::parse(replaced(text, from, to), "unknown.yaml");
    REQUIRE_FALSE(parsed);
    CHECK(parsed.diagnostics().front().code == "NCL_SCHEMA_UNKNOWN_FIELD");
    CHECK(parsed.diagnostics().front().field == field);
  }
}

TEST_CASE("Clock gates verify, emit as cell instances and bring their cell model",
          "[hardware-ops]") {
  using namespace nocloom::detail;
  Builder b;
  b.ir.name = "nocloom_gated";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto test = b.input("test", 1, "test:test", "test");
  auto enable = b.input("nl_enable", 1, "probe", "enable");
  auto gated = b.clock_gate(clock, enable, test);
  // A gate of a gate clocks the domain (a divided domain's region gates take
  // its divider output).
  auto cascaded = b.clock_gate(gated, enable, test);
  auto count = b.state(4, cascaded, reset);
  b.update(count, b.binary(Op::Subtract, count, b.constant(4, 15)));
  b.output("nl_count", count, "probe", "count");
  REQUIRE(verify_structure(b.ir));
  auto canonical = b.ir;
  REQUIRE_FALSE(canonicalize(canonical, {}, {}).empty());
  REQUIRE(verify_structure(canonical));
  const auto rtl = emit_verilog(canonical);
  const auto cells = cell_files(canonical);
  REQUIRE(cells.size() == 1);
  CHECK(cells[0].path == "nocloom_clock_gate.v");
  CHECK(cells[0].text.find("module nocloom_clock_gate (") != std::string::npos);
  CHECK(cells[0].text.find("if (!clock_in)\n    latched <= enable | test;") != std::string::npos);
  const auto gate = std::ranges::find_if(canonical.values, [&](const Value &v) {
    return v.op == Op::ClockGate && canonical.values.at(v.operands[0].index).op == Op::ClockGate;
  });
  REQUIRE(gate != canonical.values.end());
  const auto at = std::to_string(gate - canonical.values.begin());
  const auto inner = std::to_string(gate->operands[0].index);
  CHECK(rtl.find("nocloom_clock_gate nl_internal_v" + at + "_gate (.clock_in(nl_internal_v" +
                 inner + ")") != std::string::npos);
  CHECK(rtl.find("always @(posedge nl_internal_v" + at + " or negedge") != std::string::npos);
  CHECK(emit_verilog(canonical) == rtl);
  CHECK(cell_files(probe_ir()).empty());
  SECTION("the gated clock is not data") {
    b.output("nl_leak", b.binary(Op::BitAnd, gated, enable), "probe", "leak");
  }
  SECTION("a cascaded gate roots at a domain clock") {
    b.state(1, b.clock_gate(b.clock_gate(reset, enable, test), enable, test), reset);
  }
  SECTION("the test operand is a test port") { b.ir.values[gated.index].operands[2] = enable; }
  SECTION("the gated operand is a domain clock") { b.clock_gate(reset, enable, test); }
  SECTION("the enable is one bit") {
    b.ir.values[gated.index].operands[1] = b.input("nl_wide", 2, "probe", "wide");
  }
  fails(verify_structure(b.ir), "NCL_INTERNAL_IR");
}

TEST_CASE("Test ports and clock gating parse like the direct API and validate", "[config]") {
  // Gating is on by default: without a test port the default definition is rejected.
  CHECK(nocloom::Definition{}.clock_gating == nocloom::ClockGating::PerSwitchAndEndpoint);
  auto untested = example();
  untested.test_ports.clear();
  untested.clock_domains[0].test_port.reset();
  fails(nocloom::generate(untested), "NCL_DOMAIN_REFERENCE");
  auto text = replaced(yaml(), "test_ports: [{name: test}]\n",
                       "clock_gating: none\ntest_ports: [{name: scan_mode}]\n");
  text = replaced(text, "    test_port: test\n", "    test_port: scan_mode\n");
  auto p = nocloom::config::parse(text, "test.yaml");
  REQUIRE(p);
  auto d = example();
  d.clock_gating = nocloom::ClockGating::None;
  d.test_ports = {{"scan_mode"}};
  d.clock_domains[0].test_port = "scan_mode";
  CHECK(p.value().definition.test_ports == d.test_ports);
  CHECK(nocloom::config::parse(yaml(), "default.yaml").value().definition.clock_gating ==
        nocloom::ClockGating::PerSwitchAndEndpoint);
  auto a = nocloom::generate(p.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  // Without gates the test port is a module input that nothing reads, and there is no gate file.
  CHECK(a.value().size() == 5);
  auto file = [&](const std::string &suffix) {
    return std::ranges::find_if(a.value(),
                                [&](auto &x) {
                                  return x.path.ends_with(suffix) &&
                                         x.path != "nocloom_clock_gate.v" &&
                                         x.path != "nocloom_synchronizer.v";
                                })
        ->text;
  };
  CHECK(file(".v").find("input wire scan_mode,") != std::string::npos);
  CHECK(file(".map.json").find("\"endpoint\": \"test:scan_mode\"") != std::string::npos);
  CHECK(file(".map.json").find("\"test_port\": \"scan_mode\"") != std::string::npos);
  auto gated = d;
  gated.clock_gating = nocloom::ClockGating::PerSwitchAndEndpoint;
  SECTION("a gated domain without a test port") {
    gated.clock_domains[0].test_port.reset();
    gated.test_ports.clear();
    fails(nocloom::generate(gated), "NCL_DOMAIN_REFERENCE");
  }
  SECTION("a gated single domain gates its endpoints") {
    const auto artifacts = nocloom::generate(gated);
    REQUIRE(artifacts);
    const auto map = std::ranges::find_if(
        artifacts.value(), [](const auto &a) { return a.path.ends_with(".map.json"); });
    REQUIRE(map != artifacts.value().end());
    CHECK(map->text.find("\"zone\": \"protocol\"") != std::string::npos);
    CHECK(map->text.find("\"zone\": \"transport\"") != std::string::npos);
  }
  SECTION("an ungated domain needs neither") {
    gated.clock_domains[0].test_port.reset();
    gated.test_ports.clear();
    gated.clock_domains[0].gated = false;
    REQUIRE(nocloom::generate(gated));
  }
  SECTION("unknown and colliding test ports") {
    d.clock_domains[0].test_port = "other";
    fails(nocloom::generate(d), "NCL_DOMAIN_REFERENCE");
    d.clock_domains[0].test_port = "reset_n";
    d.test_ports = {{"reset_n"}};
    fails(nocloom::generate(d), "NCL_NAME_COLLISION");
  }
  fails(nocloom::config::parse(replaced(text, "clock_gating: none", "clock_gating: sometimes"),
                               "bad.yaml"),
        "NCL_SCHEMA_TYPE");
}

TEST_CASE("Clock division, enable and ungate ports parse and validate", "[config]") {
  // An internal_divider domain divides its clock port by ratio through a counter and a gate.
  const auto text = replaced(yaml(), "    test_port: test\n",
                             "    test_port: test\n    clock_type: internal_divider\n    ratio: 4\n"
                             "    ungate_port: scan_ug\n");
  auto p = nocloom::config::parse(
      replaced(text, "test_ports: [{name: test}]",
               "test_ports: [{name: test}]\nungate_ports: [{name: scan_ug}]"),
      "div.yaml");
  REQUIRE(p);
  auto d = example();
  d.ungate_ports = {{"scan_ug"}};
  d.clock_domains[0].clock_type = nocloom::ClockType::InternalDivider;
  d.clock_domains[0].ratio = 4;
  d.clock_domains[0].ungate_port = "scan_ug";
  CHECK(p.value().definition.clock_domains == d.clock_domains);
  CHECK(p.value().definition.reset_ports == d.reset_ports);
  CHECK(p.value().definition.ungate_ports == d.ungate_ports);
  auto a = nocloom::generate(p.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  auto file = [&](const nocloom::ArtifactSet &set, const std::string &suffix) {
    return std::ranges::find_if(set,
                                [&](auto &x) {
                                  return x.path.ends_with(suffix) &&
                                         x.path != "nocloom_clock_gate.v" &&
                                         x.path != "nocloom_synchronizer.v";
                                })
        ->text;
  };
  // The gate cell model keeps its three pins (the ungate port joins each gate's enable);
  // the RTL has the divider counter.
  const auto gate_file = std::ranges::find_if(
      a.value(), [](const auto &x) { return x.path == "nocloom_clock_gate.v"; });
  REQUIRE(gate_file != a.value().end());
  CHECK(gate_file->text.find("latched <= enable | test;") != std::string::npos);
  CHECK(file(a.value(), ".v").find("input wire scan_ug,") != std::string::npos);
  CHECK(file(a.value(), ".map.json").find("\"clock_type\": \"internal_divider\"") !=
        std::string::npos);
  CHECK(file(a.value(), ".map.json").find("\"ratio\": 4") != std::string::npos);
  CHECK(file(a.value(), ".map.json").find("\"ungate_port\": \"scan_ug\"") != std::string::npos);

  // An edge_qualified domain gates its clock port by an enable port.
  auto dyn = example();
  dyn.enable_ports = {{"clk_en"}};
  dyn.clock_domains[0].clock_type = nocloom::ClockType::EdgeQualified;
  dyn.clock_domains[0].enable_port = "clk_en";
  auto y = nocloom::config::parse(
      replaced(replaced(yaml(), "test_ports: [{name: test}]",
                        "test_ports: [{name: test}]\nenable_ports: [{name: clk_en}]"),
               "    test_port: test\n",
               "    test_port: test\n    clock_type: edge_qualified\n    enable_port: clk_en\n"),
      "dyn.yaml");
  REQUIRE(y);
  CHECK(y.value().definition.clock_domains == dyn.clock_domains);
  CHECK(y.value().definition.enable_ports == dyn.enable_ports);
  auto da = nocloom::generate(dyn), db = nocloom::generate(y.value().definition);
  REQUIRE(da);
  REQUIRE(db);
  CHECK(da.value() == db.value());
  CHECK(file(da.value(), ".v").find("input wire clk_en,") != std::string::npos);
  CHECK(file(da.value(), ".map.json").find("\"clock_type\": \"edge_qualified\"") !=
        std::string::npos);
  for (const auto &[kind, type] :
       {std::pair{"direct", nocloom::ClockType::Direct},
        std::pair{"external_divider", nocloom::ClockType::ExternalDivider}}) {
    CAPTURE(kind);
    auto parsed = nocloom::config::parse(
        replaced(yaml(), "    test_port: test\n",
                 std::string("    test_port: test\n    clock_type: ") + kind + "\n"));
    REQUIRE(parsed);
    CHECK(parsed.value().definition.clock_domains[0].clock_type == type);
    auto direct = example();
    direct.clock_domains[0].clock_type = type;
    auto generated = nocloom::generate(direct);
    REQUIRE(generated);
    CHECK(generated.value() == nocloom::generate(parsed.value().definition).value());
    CHECK(file(generated.value(), ".v") == file(nocloom::generate(example()).value(), ".v"));
    const auto map = file(generated.value(), ".map.json");
    if (type == nocloom::ClockType::Direct)
      CHECK(map.find("clock_type") == std::string::npos);
    else
      CHECK(map.find("\"clock_type\": \"external_divider\"") != std::string::npos);
    direct.clock_domains[0].ratio = 2;
    fails(nocloom::generate(direct), "NCL_CLOCK_DIVISION");
    direct.clock_domains[0].ratio.reset();
    direct.clock_domains[0].enable_port = "clk_en";
    direct.enable_ports = {{"clk_en"}};
    fails(nocloom::generate(direct), "NCL_CLOCK_DIVISION");
  }
  fails(nocloom::config::parse(replaced(yaml(), "    test_port: test\n",
                                        "    test_port: test\n    clock_type: unknown_clock\n")),
        "NCL_CLOCK_DIVISION");
  // Rejections.
  auto bad = example();
  bad.clock_domains[0].ratio = 4;
  fails(nocloom::generate(bad), "NCL_CLOCK_DIVISION");
  bad = example();
  bad.clock_domains[0].clock_type = nocloom::ClockType::InternalDivider;
  fails(nocloom::generate(bad), "NCL_CLOCK_DIVISION");
  bad.clock_domains[0].ratio = 65;
  fails(nocloom::generate(bad), "NCL_CLOCK_DIVISION");
  bad = example();
  bad.clock_domains[0].clock_type = nocloom::ClockType::EdgeQualified;
  fails(nocloom::generate(bad), "NCL_CLOCK_DIVISION");
  bad.clock_domains[0].enable_port = "loose";
  fails(nocloom::generate(bad), "NCL_DOMAIN_REFERENCE");
  bad.enable_ports = {{"loose"}};
  bad.clock_domains[0].enable_port.reset();
  bad.clock_domains[0].clock_type = nocloom::ClockType::Direct;
  fails(nocloom::generate(bad), "NCL_CLOCK_DIVISION");
  bad = example();
  bad.clock_domains[0].ungate_port = "loose";
  fails(nocloom::generate(bad), "NCL_DOMAIN_REFERENCE");
  bad = example();
  bad.reset_ports.push_back({"reset_ret_n"});
  const auto unused_reset = nocloom::generate(bad);
  REQUIRE_FALSE(unused_reset);
  CHECK(unused_reset.diagnostics().front().code == "NCL_RESET_PORT");
  fails(nocloom::config::parse(replaced(yaml(), "    test_port: test\n",
                                        "    test_port: test\n    clock_type: copy_of\n"),
                               "copy.yaml"),
        "NCL_CLOCK_DIVISION");
}

TEST_CASE("Reset coverage parses like the direct API", "[config]") {
  CHECK(nocloom::Definition{}.reset_coverage == nocloom::ResetCoverage::All);
  auto all = nocloom::config::parse(replaced(yaml(), "output:", "reset_coverage: all\noutput:"),
                                    "all.yaml");
  REQUIRE(all);
  CHECK(all.value().definition.reset_coverage == nocloom::ResetCoverage::All);
  auto p = nocloom::config::parse(replaced(yaml(), "output:", "reset_coverage: minimal\noutput:"),
                                  "minimal.yaml");
  REQUIRE(p);
  CHECK(p.value().definition.reset_coverage == nocloom::ResetCoverage::Minimal);
  auto d = example();
  d.reset_coverage = nocloom::ResetCoverage::Minimal;
  auto a = nocloom::generate(p.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  fails(nocloom::config::parse(replaced(yaml(), "output:", "reset_coverage: some\noutput:"),
                               "bad.yaml"),
        "NCL_SCHEMA_TYPE");
}

TEST_CASE("Register reset parses like the direct API", "[config]") {
  CHECK(nocloom::GenerationOptions{}.register_reset == nocloom::RegisterReset::Asynchronous);
  const auto parse = [](const std::string &value) {
    return nocloom::config::parse(
        replaced(yaml(), "  language: verilog_2001\n",
                 "  language: verilog_2001\n  register_reset: " + value + "\n"),
        "reset.yaml");
  };
  auto sync = parse("synchronous"), async = parse("asynchronous");
  REQUIRE(sync);
  REQUIRE(async);
  CHECK(sync.value().options.register_reset == nocloom::RegisterReset::Synchronous);
  CHECK(async.value().options.register_reset == nocloom::RegisterReset::Asynchronous);
  nocloom::GenerationOptions options;
  options.register_reset = nocloom::RegisterReset::Synchronous;
  auto a = nocloom::generate(sync.value().definition, sync.value().options);
  auto b = nocloom::generate(example(), options);
  auto plain = nocloom::generate(example()), stated = nocloom::generate(example(), {});
  REQUIRE(a);
  REQUIRE(b);
  REQUIRE(plain);
  CHECK(a.value() == b.value());
  CHECK(plain.value() ==
        nocloom::generate(async.value().definition, async.value().options).value());
  CHECK(plain.value() == stated.value());
  // Every register clears on its clock edge: the resynchronized reset release instantiates the
  // synchronous synchronizer cell, and no event control names a reset edge.
  std::string text;
  for (const auto &artifact : b.value())
    text += artifact.text;
  CHECK(text.find("negedge") == std::string::npos);
  CHECK(text.find("nocloom_synchronizer_sync_reset #(.STAGES(2)) ") != std::string::npos);
  CHECK(text.find("\"register_reset\": \"synchronous\"") != std::string::npos);
  CHECK(std::ranges::any_of(b.value(), [](const auto &artifact) {
    return artifact.path == "nocloom_synchronizer_sync_reset.v";
  }));
  fails(parse("sync"), "NCL_SCHEMA_TYPE");
}

TEST_CASE("Synchronizer chains emit as cell instances, one per bit", "[hardware-ops]") {
  using namespace nocloom::detail;
  Builder b;
  b.ir.name = "nocloom_synchronized";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto in = b.input("nl_in", 2, "probe", "in");
  std::vector<ValueId> chain;
  for (auto source = in; chain.size() < 3; source = chain.back()) {
    chain.push_back(b.state(2, clock, reset));
    b.update(chain.back(), source);
    b.ir.synchronizer_stages.push_back(chain.back());
  }
  b.output("nl_out", chain.back(), "probe", "out");
  REQUIRE_FALSE(canonicalize(b.ir, {}, {}).empty());
  const auto chains = synchronizer_chains(b.ir);
  REQUIRE(chains.size() == 1);
  const auto &c = chains[0];
  CHECK((c.stages == 3 && c.width == 2 && c.source.index != c.head));
  const auto rtl = emit_verilog(b.ir);
  const auto q = "nl_internal_v" + std::to_string(c.tail);
  for (unsigned bit = 0; bit < 2; ++bit)
    CHECK(rtl.find("nocloom_synchronizer #(.STAGES(3)) " + c.instance(bit) + " (.clock(") !=
          std::string::npos);
  CHECK(c.instance(1) == "nl_internal_v" + std::to_string(c.head) + "_sync1");
  CHECK(rtl.find(".q(" + q + "[1]));") != std::string::npos);
  CHECK(rtl.find("wire [1:0] " + q + ";") != std::string::npos);
  CHECK(rtl.find("always @") == std::string::npos);
  CHECK(rtl.find("ASYNC_REG") == std::string::npos);
  const auto cells = cell_files(b.ir);
  REQUIRE(cells.size() == 1);
  CHECK(cells[0].path == "nocloom_synchronizer.v");
  CHECK(cells[0].text.find("stage <= {stage[STAGES - 2:0], d};") != std::string::npos);
  auto broken = b.ir;
  SECTION("a stage set by its reset") {
    broken.states[broken.values[c.head].state.index].reset->value = 1;
  }
  SECTION("a one-stage chain") { broken.synchronizer_stages = {ValueId{c.head}}; }
  SECTION("a stage on another clock") {
    broken.states[broken.values[c.tail].state.index].clock = c.reset;
  }
  CHECK_THROWS_AS(emit_verilog(broken), std::logic_error);
}

TEST_CASE("Technology cells bind library cells behind the cell wrappers", "[config]") {
  const auto text = replaced(yaml(), "clock_domains:\n",
                             "cells:\n"
                             "  clock_gate: {module: lib_icg, clock: CP, enable: E, test: TE, "
                             "output: Q, tie: {RN: 1}}\n"
                             "  synchronizer: {module: lib_dff, stages: 1, clock: CK, data: D, "
                             "reset: RN, reset_active: low, output: Q, tie: {SE: 0, SI: 0}}\n"
                             "clock_domains:\n");
  auto parsed = nocloom::config::parse(text, "cells.yaml");
  REQUIRE(parsed);
  auto d = example();
  d.cells.clock_gate = nocloom::ClockGateCell{"lib_icg", "CP", "E", "TE", "Q", {{"RN", true}}};
  d.cells.synchronizer = nocloom::SynchronizerBinding{
      "lib_dff", 1, "CK", "D", "RN", true, "Q", {{"SE", false}, {"SI", false}}};
  CHECK(parsed.value().definition.cells == d.cells);
  const auto a = nocloom::generate(parsed.value().definition), b = nocloom::generate(d);
  REQUIRE(a);
  REQUIRE(b);
  CHECK(a.value() == b.value());
  auto file = [](const nocloom::ArtifactSet &set, const std::string &path) {
    const auto it = std::ranges::find(set, path, &nocloom::Artifact::path);
    REQUIRE(it != set.end());
    return it->text;
  };
  const auto &set = a.value();
  CHECK(set.size() == 8);
  CHECK(file(set, "nocloom_clock_gate.v")
            .find("lib_icg u_cell (.CP(clock_in), .E(enable), .TE(test), .Q(clock_out), "
                  ".RN(1'b1));") != std::string::npos);
  CHECK(file(set, "nocloom_synchronizer.v")
            .find("begin : u_stage\n    lib_dff u_cell (.CK(clock), .RN(reset_n), .D(chain[k]), "
                  ".Q(chain[k + 1]), .SE(1'b0), .SI(1'b0));") != std::string::npos);
  CHECK(file(set, "lib_icg.v").find("module lib_icg (\n  input wire CP,") != std::string::npos);
  CHECK(file(set, "lib_dff.v").find("always @(posedge CK or negedge RN)") != std::string::npos);
  const auto bundle = file(set, "nocloom_peripheral_bus.bundle.json");
  CHECK(bundle.find("\"library_cell_models\": [\n    \"lib_icg.v\",\n    \"lib_dff.v\"\n  ]") !=
        std::string::npos);
  CHECK(bundle.find("\"role\": \"cell_wrapper\"") != std::string::npos);
  CHECK(bundle.find("\"simulation_sources\": [\n    \"nocloom_clock_gate.v\",\n    "
                    "\"nocloom_synchronizer.v\",\n    \"nocloom_peripheral_bus.v\"\n  ]") !=
        std::string::npos);
  const auto map = file(set, "nocloom_peripheral_bus.map.json");
  CHECK(map.find("/u_stage[1].u_cell\"") != std::string::npos);
  CHECK(map.find("_gate/u_cell\"") != std::string::npos);
  // No test pin: the enable pin takes enable | test. An integrated synchronizer of the release
  // chain's two stages with an active-high clear.
  auto other = d;
  other.cells.clock_gate->test.reset();
  other.cells.synchronizer =
      nocloom::SynchronizerBinding{"lib_sync2", 2, "CK", "D", "R", false, "Q"};
  const auto c = nocloom::generate(other);
  REQUIRE(c);
  CHECK(file(c.value(), "nocloom_clock_gate.v").find(".E(enable | test), .Q(clock_out)") !=
        std::string::npos);
  CHECK(file(c.value(), "nocloom_synchronizer.v")
            .find("lib_sync2 u_cell (.CK(clock), .R(~reset_n), .D(d), .Q(q));") !=
        std::string::npos);
  SECTION("invalid names") {
    auto bad = d;
    SECTION("module prefix") { bad.cells.clock_gate->module = "nocloom_icg"; }
    SECTION("pin name") { bad.cells.synchronizer->data = "D-in"; }
    fails(nocloom::generate(bad), "NCL_NAME_INVALID");
  }
  SECTION("unused cells") {
    auto bad = d;
    SECTION("no gated domain") { bad.clock_gating = nocloom::ClockGating::None; }
    SECTION("no synchronizer chain") {
      bad.reset_ports[0] = {"reset_n", nocloom::ResetRelease::OnClockEdge, "main"};
    }
    fails(nocloom::generate(bad), "NCL_DOMAIN_REFERENCE");
  }
  SECTION("colliding pins and modules") {
    auto bad = d;
    SECTION("a pin in two roles") { bad.cells.clock_gate->test = "E"; }
    SECTION("a tie on a role pin") { bad.cells.synchronizer->tie["CK"] = false; }
    SECTION("one module for both") { bad.cells.synchronizer->module = "lib_icg"; }
    fails(nocloom::generate(bad), "NCL_NAME_COLLISION");
  }
  SECTION("stages") {
    auto bad = d;
    SECTION("beyond four") { bad.cells.synchronizer->stages = 5; }
    SECTION("an integrated synchronizer of another length") { bad.cells.synchronizer->stages = 3; }
    fails(nocloom::generate(bad), "NCL_CDC_STAGES_UNSUPPORTED");
  }
  for (const auto &[from, to, code] :
       std::vector<std::tuple<std::string, std::string, std::string>>{
           {"tie: {RN: 1}", "tie: {RN: 2}", "NCL_SCHEMA_TYPE"},
           {"reset_active: low", "reset_active: rising", "NCL_SCHEMA_TYPE"},
           {"output: Q, tie: {RN", "tie: {RN", "NCL_SCHEMA_MISSING_FIELD"},
           {"stages: 1,", "stages: 1, edge: rising,", "NCL_SCHEMA_UNKNOWN_FIELD"}}) {
    INFO(to);
    fails(nocloom::config::parse(replaced(text, from, to), "bad.yaml"), code);
  }
}
