#include "core/axi4_outlet.hpp"
#include "core/exchange_composition.hpp"
#include "core/ir_cone.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include "nocloom/config.hpp"
#include <algorithm>
#include <bit>
#include <catch2/catch_test_macros.hpp>
#include <iterator>
#include <map>
#include <numeric>
#include <set>
#include <string>
#include <tuple>
#include <vector>

using namespace nocloom;
using namespace nocloom::detail;

namespace {
// One AXI4 target t beside an APB target u. Initiator k is named by its letter, so name order is
// index order. An initiator of several route classes sends AxID c < classes - 1 in class c and
// every other AxID in its last class.
struct Shape {
  std::vector<unsigned> id_bits, classes;
  unsigned flows = 1;
  std::optional<unsigned> port_bits;
  // Class 0 of the first initiator reaches only u.
  bool unconnected_class = false;
  // The bursts t holds in flight.
  unsigned capacity = 4;
};
std::string initiator_name(unsigned k) { return std::string(1, static_cast<char>('a' + k)) + "0"; }
Definition definition(const Shape &shape) {
  std::string text =
      "schema_version: 10\nname: ids\naddress_width: 16\nreset_ports: [{name: reset_n}]\n"
      "test_ports: [{name: test}]\nclock_domains: [{name: main, clock_port: clk, reset_port: "
      "reset_n, test_port: test}]\ninitiators:\n";
  for (unsigned k = 0; k < shape.id_bits.size(); ++k) {
    text += "  - {name: " + initiator_name(k) +
            ", clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, "
            "max_outstanding: 4, id_width: " +
            std::to_string(shape.id_bits[k]);
    if (const auto classes = shape.classes[k]; classes > 1) {
      text += ", route_classes: [";
      for (unsigned c = 0; c < classes; ++c)
        text += (c ? ", c" : "c") + std::to_string(c);
      text += "], default_class: c" + std::to_string(classes - 1) + ", id_ranges: [";
      for (unsigned c = 0; c + 1 < classes; ++c)
        text += std::string(c ? ", " : "") + "{first: " + std::to_string(c) +
                ", last: " + std::to_string(c) + ", class: c" + std::to_string(c) + "}";
      text += "]";
    }
    text += "}\n";
  }
  text += "targets:\n  - {name: t, clock_domain: main, protocol: axi4, data_width: 32, len_width: "
          "8, attributes: amba, max_outstanding: " +
          std::to_string(shape.capacity) + ", flows: " + std::to_string(shape.flows);
  if (shape.port_bits)
    text += ", id_width: " + std::to_string(*shape.port_bits);
  text += ", mappings: [";
  for (unsigned g = 0; g < shape.flows; ++g)
    text += std::string(g ? ", " : "") + "{base: '0x" + std::to_string(g + 1) +
            "000', size: '0x1000', flow: " + std::to_string(g) + "}";
  text += "]}\n  - {name: u, clock_domain: main, protocol: apb4, data_width: 32, attributes: "
          "amba, mappings: [{base: '0x8000', size: '0x1000'}]}\n";
  if (shape.unconnected_class) {
    text += "connectivity:\n";
    for (unsigned k = 0; k < shape.id_bits.size(); ++k) {
      text += "  " + initiator_name(k) + ": [";
      for (unsigned c = 1; k == 0 && c < shape.classes[k]; ++c)
        text += "{target: t, source_path: " + std::to_string(c) + "}, ";
      text += k == 0 ? "u]\n" : "t, u]\n";
    }
  } else {
    text += "connectivity: all\n";
  }
  text += "output: {backend: verilog, language: verilog_2001}\n";
  auto parsed = config::parse(text);
  REQUIRE(parsed);
  return parsed.value().definition;
}
unsigned class_of(const Shape &shape, unsigned k, unsigned axid) {
  return std::min(axid, shape.classes[k] - 1);
}
// The rule, written apart from the generator: the initiator flows that reach t in order, and the
// port value of a key.
struct Rule {
  unsigned flow_bits = 0, index_bits = 0, id_bits = 0;
  std::map<std::pair<unsigned, unsigned>, unsigned> index;
  explicit Rule(const Shape &shape)
      : flow_bits(static_cast<unsigned>(std::bit_width(shape.flows - 1))) {
    for (unsigned k = 0; k < shape.id_bits.size(); ++k) {
      for (unsigned c = 0; c < shape.classes[k]; ++c)
        if (!(shape.unconnected_class && k == 0 && c == 0))
          index.emplace(std::pair{k, c}, static_cast<unsigned>(index.size()));
      id_bits = std::max(id_bits, shape.id_bits[k]);
    }
    index_bits = static_cast<unsigned>(std::bit_width(index.size() - 1));
  }
  std::uint64_t port(unsigned flow, unsigned at, unsigned axid) const {
    return ((std::uint64_t{axid} << index_bits | at) << flow_bits) | flow;
  }
};

// The transaction word uses global flow indices. t is first in target name order.
std::uint64_t folded_port(const Shape &shape, const Rule &rule, unsigned width, unsigned source,
                          unsigned axid, unsigned flow, bool write) {
  const auto sequence_bits = width - rule.flow_bits;
  if (!sequence_bits)
    return flow;
  const auto before = shape.classes.begin() + source;
  const auto first = std::accumulate(shape.classes.begin(), before, 0U);
  const auto count = std::accumulate(shape.classes.begin(), shape.classes.end(), 0U);
  const auto source_path = first + class_of(shape, source, axid);
  auto route = std::uint64_t{flow} << (1 + rule.id_bits + std::bit_width(count - 1)) |
               std::uint64_t{source_path} << (1 + rule.id_bits) | (axid << 1) | write;
  const auto mask = (std::uint64_t{1} << sequence_bits) - 1;
  std::uint64_t high = 0;
  for (; route; route >>= sequence_bits)
    high ^= route & mask;
  return high << rule.flow_bits | flow;
}
struct Outlet {
  NodeHardware node;
  std::size_t state(const std::string &key) const {
    return node.ir.values.at(node.bindings.at(key).index).state.index;
  }
  std::size_t driven(const std::string &name) const {
    const auto at = static_cast<std::size_t>(std::ranges::find(node.ir.ports, name, &Port::name) -
                                             node.ir.ports.begin());
    return std::ranges::find_if(node.ir.drivers, [&](const auto &d) { return d.port.index == at; })
        ->value.index;
  }
  // The AWID of a write in the main entry and the ARID of the read at the issue position.
  std::uint64_t awid(unsigned source, unsigned axid, std::uint64_t address) const {
    return *evaluate_cone(node.ir, driven("nl_axi_awid"),
                          {{state("w.descriptor.source"), 2 * source},
                           {state("w.descriptor.id"), axid},
                           {state("w.descriptor.address"), address}});
  }
  std::uint64_t arid(unsigned source, unsigned axid, std::uint64_t address) const {
    return *evaluate_cone(node.ir, driven("nl_axi_arid"),
                          {{state("r.issue"), 0},
                           {state("r.slot0.source"), 2 * source + 1},
                           {state("r.slot0.id"), axid},
                           {state("r.slot0.address"), address}});
  }
};
// Every key of a shape at its target: the layout of the plan and, where the port holds the
// fields, the port value of both directions in the lowered outlet, every key with its own value.
// A narrower port must give every request its folded value in each direction.
void check_shape(const Shape &shape) {
  const auto built = build_network_plan(definition(shape));
  REQUIRE(built);
  const auto &plan = built.value();
  const auto ti = plan.target_index("t");
  const Rule rule(shape);
  const auto &layout = plan.route_index.target_id_layouts[ti];
  REQUIRE(layout.flow_bits == rule.flow_bits);
  REQUIRE(layout.index_bits == rule.index_bits);
  REQUIRE(layout.id_bits == rule.id_bits);
  REQUIRE(layout.flows.size() == rule.index.size());
  for (const auto &[flow, at] : rule.index)
    REQUIRE(layout.flows[at] == TargetIdLayout::Flow{flow.first, flow.second});
  const auto width = plan.targets[ti].id_bits.value();
  auto profile = axi4_outlet_profile(plan, ti);
  const bool folds = width < rule.flow_bits + rule.index_bits + rule.id_bits;
  REQUIRE(folds_target_id(plan, ti) == folds);
  REQUIRE(profile.id_route.has_value() == folds);
  // The key does not depend on the capacity: an outlet of several bursts computes the ID of each
  // entry from the fields it keeps, where the cones below read it.
  profile.max_outstanding = 4;
  auto lowered = lower_axi4_outlet(profile);
  REQUIRE(lowered);
  const Outlet outlet{std::move(lowered.value())};
  std::set<std::uint64_t> values;
  unsigned keys = 0;
  for (unsigned k = 0; k < shape.id_bits.size(); ++k)
    for (unsigned axid = 0; axid < 1U << shape.id_bits[k]; ++axid) {
      const auto at = rule.index.find({k, class_of(shape, k, axid)});
      if (at == rule.index.end())
        continue;
      for (unsigned g = 0; g < shape.flows; ++g) {
        const std::uint64_t address = 0x1000 * (g + 1) + 0x40;
        const auto want = rule.port(g, at->second, axid);
        const auto write = outlet.awid(k, axid, address);
        REQUIRE(write == (folds ? folded_port(shape, rule, width, k, axid, g, true) : want));
        const auto read = outlet.arid(k, axid, address);
        REQUIRE(read == (folds ? folded_port(shape, rule, width, k, axid, g, false) : want));
        if (!folds)
          REQUIRE(outlet.arid(k, axid, address + 4) == want);
        REQUIRE((write & ((1U << rule.flow_bits) - 1)) == g);
        values.insert(write);
        ++keys;
      }
    }
  // Every key has its own value.
  if (!folds)
    REQUIRE(values.size() == keys);
}
} // namespace

TEST_CASE("A target ID is the flow, the initiator flow index and AxID where the port holds them",
          "[target-id]") {
  const std::vector<std::pair<std::vector<unsigned>, std::vector<unsigned>>> initiators{
      {{2}, {1}},
      {{3}, {3}},
      {{2, 2}, {1, 1}},
      {{1, 3}, {2, 1}},
      {{2, 2, 2}, {1, 1, 1}},
      {{3, 2, 2}, {3, 1, 2}},
      {{2, 0, 1, 3, 2}, {1, 1, 1, 2, 1}}};
  for (const auto &[id_bits, classes] : initiators)
    for (const unsigned flows : {1U, 2U, 3U})
      for (const unsigned capacity : {1U, 4U}) {
        const Rule rule(Shape{id_bits, classes, flows});
        const auto whole = rule.flow_bits + rule.index_bits + rule.id_bits;
        // Every port width from the flow bits (at least one bit) to one above the three fields.
        for (unsigned width = std::max(1U, rule.flow_bits); width <= whole + 1; ++width)
          check_shape({id_bits, classes, flows, width, false, capacity});
        // The default width.
        check_shape({id_bits, classes, flows, std::nullopt, false, capacity});
      }
}

TEST_CASE("An initiator flow that does not reach a target takes no index there", "[target-id]") {
  for (const unsigned capacity : {1U, 4U})
    for (const unsigned width : {2U, 4U, 6U})
      check_shape({{3, 2, 2}, {3, 1, 2}, 2, width, true, capacity});
}

TEST_CASE("A target's ID layout depends only on the initiators connected to it", "[target-id]") {
  // The two initiators of t stay; one more reaches only another target.
  auto d = definition({{2, 3}, {1, 2}, 2, 6});
  const auto before = build_network_plan(d);
  REQUIRE(before);
  auto other = d.initiators.back();
  other.name = "a1";
  other.route_classes.clear();
  other.default_class.reset();
  other.id_ranges.clear();
  other.id_width = 6;
  d.initiators.push_back(other);
  d.connectivity = Connectivity{};
  for (const auto &initiator : d.initiators)
    for (const auto &target : d.targets)
      if (initiator.name != "a1" || target.name == "u")
        d.connectivity->pairs.push_back({initiator.name, target.name});
  const auto after = build_network_plan(d);
  REQUIRE(after);
  const auto layout = [](const NetworkPlan &plan) {
    auto out = plan.route_index.target_id_layouts[plan.target_index("t")];
    // Source positions move with the name order of all initiators; the names behind them stay.
    std::vector<std::pair<std::string, unsigned>> names;
    std::ranges::transform(out.flows, std::back_inserter(names), [&](const auto &flow) {
      return std::pair{plan.sources[flow.source].name, flow.flow};
    });
    return std::tuple{out.flow_bits, out.index_bits, out.id_bits, names};
  };
  CHECK(layout(before.value()) == layout(after.value()));
}
