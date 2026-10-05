// Test-only DUT fixture and fault-build engine for the unified verification
// environment (tests/verif). It links the internal library, builds a
// registered recipe, and optionally rewrites the hardware IR on the test side:
//
//   nlv_fixture <recipe> <out_dir> [--param key=value]... [--faults spec.json]
//               [--list-bindings] [--probes]
//
// Outputs: network.v (unmodified emission, byte-comparable with the product
// path), ports.json (components, modules, ports), and with --faults also
// faulted.v and faults.json. With --probes, probes.json names the internal
// nets of each probe group of a recipe that declares them (white-box
// observation: switch output arbiters, link stages), in network.v and, with
// --faults, in the faulted copy of their component. Faults are anchored on component bindings, port
// names and operator structure, never on generated value numbers. Inline
// faults multiplex the faulty value against the original under a new test
// input nl_nlv_fault_sel; variant faults rebuild a component with changed
// recipe parameters (parameter-level, removed stage, added state) and the
// harness selects that copy's outputs. The product generation path is never
// changed.
#include "core/ahb_outlet.hpp"
#include "core/async_fifo.hpp"
#include "core/axi4_initiator.hpp"
#include "core/axi4_outlet.hpp"
#include "core/axi4_output.hpp"
#include "core/axi4_upsize.hpp"
#include "core/axi4_width_clamp.hpp"
#include "core/exchange_composition.hpp"
#include "core/hardware.hpp"
#include "core/hardware_builder.hpp"
#include "core/register_bank.hpp"
#include "core/treadle.hpp"
#include "nocloom/config.hpp"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {
using namespace nocloom;
using namespace nocloom::detail;
using Json = nlohmann::json;
using Params = std::map<std::string, std::string>;

struct Component {
  std::string label;
  HardwareIR ir;
  std::map<std::string, ValueId> bindings;
  // Probe groups by binding name: [{name, kind, ..., nets: [[field, binding]...]}].
  nlohmann::json probes = nlohmann::json::array();
};

struct FixtureError : std::runtime_error {
  int code;
  FixtureError(int c, const std::string &message) : std::runtime_error(message), code(c) {}
};

std::string param(const Params &p, const std::string &key, const std::string &fallback) {
  const auto it = p.find(key);
  return it == p.end() ? fallback : it->second;
}

template <class T> T take(Result<T> result, const std::string &what) {
  if (!result) {
    std::ostringstream message;
    message << what << " lowering failed:";
    for (const auto &d : result.diagnostics())
      message << ' ' << d.code << ": " << d.message;
    throw FixtureError(1, message.str());
  }
  return std::move(result.value());
}

// ---------------------------------------------------------------------------
// Recipes. Each returns the components that one bench instantiates.

std::vector<Component> lite_target(const Params &p) {
  const unsigned width = std::stoul(param(p, "width", "32"));
  if (width != 32 && width != 64)
    throw FixtureError(2, "lite_target width must be 32 or 64");
  const unsigned cap = std::stoul(param(p, "max_outstanding", "1"));
  Axi4InitiatorProfile ip{"probe", width, 32, 4, 8};
  ip.start_address = true;
  ip.exclusive = true;
  ip.exclusive_windows = {{AddressWindow{0x1000, 0x1000}, 128, 0}};
  BurstOutletProfile profile{"probe", 4, 8, 0,
                             std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  profile.data_bits = width;
  profile.fixed_single = true;
  profile.timeout_cycles = std::stoul(param(p, "timeout_cycles", "1024"));
  profile.timeout_completion = param(p, "timeout_completion", "on") == "on";
  profile.exclusive = true;
  profile.exclusive_target = {ExclusiveAccess::Network, 2, 16};
  if (cap > 1) {
    // Capacity above one stands alone: no monitor and no timer; the front
    // keeps as many bursts in flight and every fragment carries a tag.
    const unsigned tag_bits = std::bit_width(cap - 1);
    profile.max_outstanding = cap;
    profile.timeout_cycles = 0;
    profile.timeout_completion = false;
    profile.exclusive_target = {ExclusiveAccess::None, 0, 0};
    profile.tag_bits = tag_bits;
    ip.max_outstanding = cap;
    ip.tag_bits = tag_bits;
    ip.destinations = {{AddressWindow{0x1000, 0x1000}}};
    ip.miss_destination = 0;
  }
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto target = take(lower_axi_lite_outlet(profile), "outlet");
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          {"target", std::move(target.ir), std::move(target.bindings)}};
}

// The APB4, APB3 or APB2 burst outlet behind a 32-bit AXI4 initiator front
// with these settings: window 0x1000..0x2000, dialect apb4/apb3/apb2, fixed_bursts single,
// back_to_back (SETUP right after a completion) and a timeout in target
// cycles (completion on). With back_to_back the read response queue and
// write buffer are a probe group (apb_queue).
std::vector<Component> apb_outlet(const Params &p) {
  Axi4InitiatorProfile ip{"probe", 32, 32, 4, 8};
  ip.start_address = true;
  BurstOutletProfile profile{"probe", 4, 8, 0,
                             std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  profile.fixed_single = param(p, "fixed_single", "0") == "1";
  profile.apb_back_to_back = param(p, "back_to_back", "0") == "1";
  profile.timeout_cycles = std::stoul(param(p, "timeout_cycles", "0"));
  profile.timeout_completion = profile.timeout_cycles != 0;
  const auto dialect = param(p, "dialect", "apb4");
  if (dialect == "apb3")
    profile.dialect = Protocol::Apb3;
  else if (dialect == "apb2")
    profile.dialect = Protocol::Apb2;
  else if (dialect != "apb4")
    throw FixtureError(2, "apb_outlet dialect must be apb4, apb3 or apb2");
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto target = take(lower_apb_burst_outlet(profile), "outlet");
  Json probes = Json::array();
  if (profile.apb_back_to_back) {
    Json nets = Json::array();
    for (const auto *field :
         {"tail_full=r.tail_full", "front_full=r.full", "push=r.push", "pop=r.pop", "skip=r.skip",
          "read_issue=bus.read_issue", "write_full=w.full", "write_flight=w.flight",
          "phase=bus.phase", "complete=bus.complete"}) {
      const std::string text = field;
      const auto equal = text.find('=');
      nets.push_back(Json::array({text.substr(0, equal), text.substr(equal + 1)}));
    }
    probes.push_back({{"name", "apb_queue"}, {"kind", "apb_queue"}, {"nets", nets}});
  }
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          {"target", std::move(target.ir), std::move(target.bindings), std::move(probes)}};
}

// The AHB outlet behind an AXI4 initiator front of the same width
// with these settings: window 0x1000..0x2000 (window=512:
// 0x1000..0x1200), data_bits 32..1024, fixed_single, complete (write bursts
// complete), blocks (no undefined-length INCR), hresp2 (2-bit AMBA 2 HRESP),
// timeout (4096 target cycles, longer than any wait of the scenario).
// overlap=1 builds the several-packets outlet (window
// 0x1000..0x11000, two ID bits, tags) behind a front that keeps eight bursts
// per direction in flight.
std::vector<Component> ahb_outlet(const Params &p) {
  const unsigned data = std::stoul(param(p, "data_bits", "32"));
  const bool overlap = param(p, "overlap", "0") == "1";
  Axi4InitiatorProfile ip{"probe", data, 32, 4, 8};
  ip.start_address = true;
  BurstOutletProfile profile{"probe", 4, 8, 0,
                             std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  if (overlap) {
    profile = BurstOutletProfile{"probe", 4, 8, 0,
                                 std::vector<nocloom::AddressMapping>{{0x1000, 0x10000}}};
    profile.tag_bits = 3;
    ip.max_outstanding = 8;
    ip.tag_bits = 3;
    ip.destinations = {AddressWindow{0x1000, 0x10000}};
    ip.miss_destination = 0;
  }
  profile.data_bits = data;
  profile.fixed_single = param(p, "fixed_single", "0") == "1";
  profile.complete_write_bursts = param(p, "complete", "0") == "1";
  profile.undefined_length_bursts = param(p, "blocks", "0") != "1";
  if (param(p, "window", "0") == "512")
    profile.mappings = {{0x1000, 0x200}};
  profile.hresp_width = param(p, "hresp2", "0") == "1" ? 2 : 1;
  profile.timeout_cycles = std::stoul(param(p, "timeout", "0"));
  profile.timeout_completion = profile.timeout_cycles != 0 && overlap;
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto target = take(lower_ahb_outlet(profile), "outlet");
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          {"target", std::move(target.ir), std::move(target.bindings)}};
}

// A test-side packet shim between an initiator front and the component
// behind it: every internal face passes through (nl_up_* faces the front,
// nl_down_* the component), except that a request may be marked with the
// Weft error flag and the source field may be set. The error is chosen by
// the burst's original address, bits 19:16: 1 marks the head (the whole
// burst is rejected), 2 the last write beat, 3 every write beat that is
// neither first nor last. The source field carries 2 * index on writes and
// 2 * index + 1 on reads; with source_bit the index is that bit of the
// burst's original address (two sources behind one front). It only drives
// the faces of the component under test as an upstream network stage would.
Component packet_shim(const HardwareIR &front, std::optional<unsigned> source, bool errors,
                      std::optional<unsigned> source_bit = std::nullopt) {
  Builder b;
  b.ir.name = "nocloom_nlv_packet_shim";
  std::map<std::string, ValueId> up;
  std::vector<const Port *> forward, backward;
  for (const auto &port : front.ports) {
    if (port.endpoint == "axi" || port.endpoint.starts_with("domain:"))
      continue;
    const auto suffix = port.name.substr(3); // after "nl_"
    // A front output travels down; a front input travels up.
    if (!port.input) {
      up[suffix] = b.input("nl_up_" + suffix, port.width, "up_" + port.endpoint, port.signal);
      forward.push_back(&port);
    } else {
      up[suffix] = b.input("nl_down_" + suffix, port.width, "down_" + port.endpoint, port.signal);
      backward.push_back(&port);
    }
  }
  auto field = [&](const std::string &face, const std::string &signal) {
    const auto it = up.find(face + "_" + signal);
    return it == up.end() ? std::optional<ValueId>{} : std::optional<ValueId>{it->second};
  };
  for (const auto *port : forward) {
    const auto suffix = port->name.substr(3);
    auto value = up.at(suffix);
    const auto face = port->endpoint;
    const unsigned read = face == "rreq" ? 1 : 0;
    if (port->signal == "source" && source_bit && field(face, "start_address"))
      value = b.select(b.slice(*field(face, "start_address"), *source_bit, 1),
                       b.constant(port->width, 2 + read), b.constant(port->width, read));
    else if (port->signal == "source" && source)
      value = b.constant(port->width, 2 * *source + read);
    if (port->signal == "error" && errors && field(face, "start_address")) {
      const auto start = *field(face, "start_address");
      const auto mode = b.slice(start, 16, 4);
      auto is = [&](unsigned m) { return b.binary(Op::Equal, mode, b.constant(4, m)); };
      auto first = *field(face, "first"), last = *field(face, "last");
      auto mark = b.binary(Op::BitAnd, is(1), first);
      if (face == "wreq") {
        mark = b.binary(Op::BitOr, mark, b.binary(Op::BitAnd, is(2), last));
        const auto middle =
            b.binary(Op::BitAnd, b.unary(Op::BitNot, first, 1), b.unary(Op::BitNot, last, 1));
        mark = b.binary(Op::BitOr, mark, b.binary(Op::BitAnd, is(3), middle));
      }
      value = b.binary(Op::BitOr, value, mark);
    }
    b.output("nl_down_" + suffix, value, "down_" + face, port->signal);
  }
  for (const auto *port : backward) {
    const auto suffix = port->name.substr(3);
    b.output("nl_up_" + suffix, up.at(suffix), "up_" + port->endpoint, port->signal);
  }
  return {"shim", std::move(b.ir), {}};
}

// The aligned-burst AXI4 outlet behind an AXI4 initiator front and the
// packet shim. multi: the several-bursts outlet reached by source index 1
// only.
std::vector<Component> axi4_outlet(const Params &p) {
  const unsigned data = std::stoul(param(p, "data_bits", "32"));
  const unsigned len = std::stoul(param(p, "len_bits", "8"));
  const unsigned ids = std::stoul(param(p, "id_bits", "4"));
  const unsigned sources = std::stoul(param(p, "source_bits", "3"));
  const bool multi = param(p, "multi", "0") == "1";
  Axi4InitiatorProfile ip{"probe", data, 32, ids, len};
  ip.start_address = true;
  ip.source_bits = sources;
  Axi4OutletProfile op;
  op.name = "probe";
  op.data_bits = data;
  op.len_bits = len;
  op.id_bits = ids;
  op.source_bits = sources;
  op.start_address = true;
  if (multi) {
    // Several bursts from the front too, so the outlet's overlap shows.
    op.max_outstanding = 2;
    op.id_sources = {1};
    ip.max_outstanding = 2;
    ip.destinations = {AddressWindow{0, 0x400000}};
    ip.admitted_windows = ip.destinations;
    ip.tag_bits = op.tag_bits = 1;
  }
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto target = take(lower_axi4_outlet(op), "outlet");
  std::optional<unsigned> source;
  if (sources)
    source = std::stoul(param(p, "source", "1"));
  auto shim = packet_shim(front.ir, source, true);
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          std::move(shim),
          {"target", std::move(target.ir), std::move(target.bindings)}};
}

// The target-side upsize merge behind a front of the target width (narrow
// bursts in the target layout, as the width adapters deliver them) and an
// outlet of that width. source: the index in the source field (the
// merge decodes writes 2 * index, reads 2 * index + 1). max_outstanding k > 1:
// the merge keeps k contexts per direction (merge_outstanding overrides it,
// for a fault variant); the front takes k + 1 of its one read/write pool
// (freed on downstream accept) and the outlet 2 * (k + 1) of its 16-entry
// read/write pool for k <= 7 (both directions fill at once), else k + 1 for
// k <= 15 (the case fills one direction at a time), so the merge stays the
// bound; two sources reach the merge (address bit 12 picks the index) and the outlet
// gives each its own target ID.
std::vector<Component> axi4_upsize(const Params &p) {
  const unsigned data = std::stoul(param(p, "data_bits", "128"));
  const unsigned capacity = std::stoul(param(p, "max_outstanding", "1"));
  const unsigned merge_capacity =
      std::stoul(param(p, "merge_outstanding", std::to_string(capacity)));
  Axi4InitiatorProfile ip{"probe", data, 32, 3, 8};
  ip.start_address = true;
  ip.source_bits = 2;
  Axi4UpsizeProfile up;
  up.name = "probe";
  up.id_bits = 3;
  up.data_bits = data;
  up.source_bits = 2;
  up.joined = param(p, "joined", "0") == "1";
  const unsigned source = std::stoul(param(p, "source", "1"));
  Axi4OutletProfile op;
  op.name = "probe";
  op.data_bits = data;
  op.len_bits = 8;
  op.id_bits = 3;
  op.source_bits = 2;
  op.start_address = true;
  op.interleave = op.merged = up.joined;
  std::optional<unsigned> source_bit;
  if (capacity > 1) {
    // Several bursts in flight through front, merge and outlet: the merge's
    // contexts fill. The outlet pools reads and
    // writes (16 entries at most): up to k = 7 it takes one burst more per
    // direction, above that one burst more of a single direction.
    if (capacity > 15 || merge_capacity > 16)
      throw FixtureError(2, "axi4_upsize contexts: max_outstanding 2..15");
    const unsigned pool = capacity > 7 ? capacity + 1 : 2 * (capacity + 1);
    unsigned tag = 1;
    while ((1U << tag) < pool)
      ++tag;
    ip.max_outstanding = capacity + 1;
    ip.destinations = {AddressWindow{0, 0x400000}};
    ip.admitted_windows = ip.destinations;
    op.max_outstanding = pool;
    up.max_outstanding = merge_capacity;
    ip.tag_bits = up.tag_bits = op.tag_bits = tag;
    op.id_sources = {0, 1};
    source_bit = 12;
  }
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto merge = take(lower_axi4_upsize(up), "upsize");
  if (up.joined) {
    Builder b;
    b.ir = std::move(merge.ir);
    const auto signal = [&](const std::string &name) {
      const auto port = std::ranges::find(b.ir.ports, name, &Port::name);
      const PortId id{static_cast<std::size_t>(port - b.ir.ports.begin())};
      if (port->input)
        return std::ranges::find_if(b.ir.values,
                                    [&](const auto &v) {
                                      return v.op == Op::Input && v.input_port.index == id.index;
                                    }) -
               b.ir.values.begin();
      return static_cast<std::ptrdiff_t>(std::ranges::find_if(b.ir.drivers, [&](const auto &d) {
                                           return d.port.index == id.index;
                                         })->value.index);
    };
    const auto valid = ValueId{static_cast<std::size_t>(signal("nl_up_rrsp_valid"))};
    const auto ready = ValueId{static_cast<std::size_t>(signal("nl_up_rrsp_ready"))};
    const auto last = ValueId{static_cast<std::size_t>(signal("nl_up_rrsp_last"))};
    const auto tail = b.binary(Op::BitAnd, b.binary(Op::BitAnd, valid, ready), last);
    b.output("nl_hint_read_tail", tail, "hint", "read_tail");
    merge.ir = std::move(b.ir);
  }
  auto target = take(lower_axi4_outlet(op), "outlet");
  auto shim = packet_shim(front.ir, source, false, source_bit);
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          std::move(shim),
          {"merge", std::move(merge.ir), std::move(merge.bindings)},
          {"target", std::move(target.ir), std::move(target.bindings)}};
}

std::vector<Component> axi4_width_cut(const Params &p) {
  const unsigned data_bits = std::stoul(param(p, "data_bits", "128"));
  Axi4InitiatorProfile ip{"probe", data_bits, 32, 4, 8};
  ip.start_address = true;
  Axi4WidthCutProfile profile;
  profile.name = "probe";
  profile.module_prefix = "nocloom_axi4_width_cut_";
  profile.id_bits = 4;
  profile.data_bits = data_bits;
  profile.down_bits = 32;
  profile.fixed_width = 2;
  if (param(p, "fixed_reject", "on") == "on")
    profile.fixed_reject = {{0, std::uint64_t{1} << 32}};
  auto front = take(lower_axi4_initiator(ip), "initiator");
  auto bridge = take(lower_axi4_width_cut(profile), "width cut");
  auto target = take(lower_axi4_outlet({"probe", 32, 4, 8, 0, true}), "outlet");
  return {{"front", std::move(front.ir), std::move(front.bindings)},
          {"bridge", std::move(bridge.ir), std::move(bridge.bindings)},
          {"target", std::move(target.ir), std::move(target.bindings)}};
}

// Public schema 10 networks: a definition as a configuration states it, planned once
// (build_network_plan) and composed as the product does. The main domain takes clk_main and
// reset_n; connectivity admits every pair.
Definition network(const std::string &name, ArbitrationPolicy policy) {
  Definition d;
  d.name = name;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  d.arbitration = policy;
  d.connectivity = Connectivity{.all = true};
  return d;
}
Initiator axi_source(const std::string &name, unsigned data, unsigned id, unsigned len) {
  Initiator i{name, "main"};
  i.protocol = Protocol::Axi4;
  i.data_width = data;
  i.id_width = id;
  i.len_width = len;
  return i;
}
Target axi_target(const std::string &name, AddressWindow window, unsigned data, unsigned len) {
  Target t{name, "main", {{window.base, window.size}}};
  t.protocol = Protocol::Axi4;
  t.data_width = data;
  t.len_width = len;
  t.attributes = TargetAttributes::amba();
  return t;
}
using Pairs = std::vector<std::pair<std::string, std::string>>;
// One direction: nodes, links (from, to) and attachments (endpoint, node).
ExchangeGraph graph(const std::vector<std::string> &nodes, const Pairs &links, const Pairs &entries,
                    const Pairs &exits) {
  ExchangeGraph g;
  for (const auto &name : nodes)
    g.nodes.push_back({name});
  for (const auto &[from, to] : links)
    g.links.push_back({from, to});
  for (const auto &[endpoint, node] : entries)
    g.entries.push_back({endpoint, node});
  for (const auto &[endpoint, node] : exits)
    g.exits.push_back({endpoint, node});
  return g;
}
// The register_reset parameter of every recipe (asynchronous or synchronous), as the CLI's
// output.register_reset: composed networks lower with it, and every component emits with it.
RegisterReset register_reset = RegisterReset::Asynchronous;
ExchangeHardware compose(const Definition &d, const std::string &what) {
  take(validate(d), what);
  return take(lower_axi4_exchange_composition(take(build_network_plan(d), what), register_reset),
              what);
}
// The generated identity of a configured switch of the request ("req_") or response ("rsp_")
// graph.
std::string hardware_node(const ExchangeHardware &h, const std::string &prefix,
                          const std::string &name) {
  const auto node = std::ranges::find_if(
      h.plan.nodes, [&](const NodePlan &n) { return n.name == name && n.id.starts_with(prefix); });
  if (node == h.plan.nodes.end())
    throw FixtureError(3, "no switch " + name);
  return node->id;
}

// Probe groups of an exchange: every arbitrated switch output (candidate
// valid, last, flow, first, level and eligible, output ready, winner, held,
// transfer and release) and every registered link stage, by binding name.
Json exchange_probes(const ExchangeHardware &h) {
  auto label = [&](const std::string &key) { return h.plan.label(key); };
  Json groups = Json::array();
  for (const auto &[key, a] : h.plan.arbiters) {
    Json candidates = Json::array();
    std::ranges::transform(a.candidates, std::back_inserter(candidates), label);
    Json g = {{"name", "arbiter/" + key},
              {"kind", "arbiter"},
              {"graph", a.response ? "response" : "request"},
              {"node", label("node/" + a.node).substr(5)},
              {"output", label(a.output)},
              {"policy", treadle::policy_name(a.spec.policy)},
              {"count", a.candidates.size()},
              {"rank", a.spec.rank},
              {"candidates", candidates},
              {"nets", Json::array()}};
    const auto base =
        std::string(a.response ? "graph/rsp/" : "graph/req/") + a.node + ">" + a.output;
    // Levels add each candidate's packet tail, flow, head marker and level (its pressure, a
    // thermometer), the eligible set and the output ready; without levels the tail of the
    // granted packet is the one the arbiter sees.
    const bool levels = h.plan.level_hint_bits() > 0;
    for (const auto *field : {"valid", "last", "flow", "first", "level", "eligible"})
      if (levels || std::string(field) == "valid")
        for (std::size_t i = 0; i < a.candidates.size(); ++i)
          g["nets"].push_back({field, base + "." + field + std::to_string(i)});
    for (const auto *field : {"ready", "winner", "held", "transfer", "tail", "release"})
      if (std::string(field) != (levels ? "tail" : "ready"))
        g["nets"].push_back({field, base + "." + field});
    groups.push_back(std::move(g));
  }
  std::set<std::string> pipes;
  for (const auto &link : h.plan.links)
    for (const auto &stage : link.stages)
      if (stage.kind == StageKind::Pipe)
        pipes.insert(stage.name);
  for (const auto &key : pipes) {
    Json g = {{"name", key}, {"kind", "pipeline"}, {"nets", Json::array()}};
    for (const auto *field : {"full", "spare", "push", "pop", "input.valid", "input.ready",
                              "output.valid", "output.ready"})
      g["nets"].push_back({field, key + "." + field});
    groups.push_back(std::move(g));
  }
  return groups;
}

// The fixed QoS funnel graph with bounded packets and registered links of
// cases/qos_outstanding.py.
std::vector<Component> qos_bounded(const Params &p) {
  auto d = network("arbitration_funnel_graph", ArbitrationPolicy::Fifo);
  // Reads and writes share the source capacity: eight keeps four of each.
  d.transport = TransportOptions{.max_outstanding = 8};
  for (unsigned i = 0; i < 2; ++i) {
    auto s = axi_source("s" + std::to_string(i), 32, 2, 8);
    s.narrow_bursts = NarrowBursts::SplitNonModifiable;
    s.qos.levels = i ? QosLevels{1, 7} : QosLevels{7, 1};
    d.initiators.push_back(s);
    auto t = axi_target("t" + std::to_string(i), {0x10000ULL * i, 0x10000}, 32, 8);
    t.max_outstanding = std::stoul(param(p, "target_outstanding", "4"));
    d.targets.push_back(t);
  }
  d.topology = ExchangeTopology{
      graph({"a", "b", "m"}, {{"a", "m"}, {"b", "m"}}, {{"s0", "a"}, {"s1", "b"}},
            {{"t0", "m"}, {"t1", "m"}}),
      graph({"n", "x"}, {{"x", "n"}}, {{"t0", "x"}, {"t1", "x"}}, {{"s0", "n"}, {"s1", "n"}})};
  for (auto *g : {&d.topology->request, &d.topology->response})
    for (auto &link : g->links)
      link.pipeline = {true, true};
  auto h = compose(d, "exchange");
  auto probes = exchange_probes(h);
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// A public configuration with arbiter observations in each arbiter's clock domain.
std::vector<Component> pressure_network(const Params &p) {
  std::ifstream file(param(p, "config", ""));
  std::stringstream text;
  text << file.rdbuf();
  const auto d = take(config::parse(text.str()), "definition").definition;
  auto h =
      take(lower_axi4_exchange_composition(take(build_network_plan(d), "plan"), register_reset),
           "composition");
  auto probes = exchange_probes(h);
  for (auto &group : probes) {
    if (group.at("kind") != "arbiter")
      continue;
    const auto key = group.at("name").get<std::string>().substr(8);
    const auto &arbiter = h.plan.arbiters.at(key);
    const auto base = std::string(arbiter.response ? "graph/rsp/" : "graph/req/") + arbiter.node +
                      ">" + arbiter.output;
    const auto held = h.bindings.at(base + ".held");
    auto clock = h.ir.states.at(h.ir.values.at(held.index).state.index).clock;
    while (h.ir.values.at(clock.index).op == Op::ClockGate)
      clock = h.ir.values.at(clock.index).operands[0];
    if (h.ir.values.at(clock.index).op != Op::Input)
      throw FixtureError(3, "arbiter clock has no port");
    group["clock"] = h.ir.ports.at(h.ir.values.at(clock.index).input_port.index).name;
  }
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// A regulated source and a fixed-level peer share two targets. Response pipes let a response
// head wait while the other target occupies the source's response output.
std::vector<Component> response_freeze(const Params &) {
  auto d = network("response_freeze", ArbitrationPolicy::RoundRobin);
  d.clock_domains.front().clock_port = "clk";
  for (unsigned i = 0; i < 3; ++i) {
    auto s = axi_source("s" + std::to_string(i), 32, 2, 8);
    s.max_outstanding = 4;
    if (i == 0)
      s.qos.shaper = QosShaper{512, 16}, s.qos.scheduler = QosScheduler{3, 0, 50};
    else if (i == 1)
      s.qos.levels = {2, 2};
    d.initiators.push_back(s);
  }
  for (unsigned i = 0; i < 2; ++i) {
    auto t = axi_target("t" + std::to_string(i), {0x10000ULL * i, 0x10000}, 32, 8);
    t.max_outstanding = 2;
    t.pipes.response = {.forward = true};
    d.targets.push_back(t);
  }
  auto h = compose(d, "exchange");
  auto probes = exchange_probes(h);
  Json response_probe = {
      {"name", "target/t0/response"}, {"kind", "response_level"}, {"nets", Json::array()}};
  response_probe["nets"].push_back({"pending", "pressure/target/t0/response.pending"});
  response_probe["nets"].push_back({"level", "pressure/target/t0/response.level"});
  probes.push_back(std::move(response_probe));
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// The networks of cases/arbitration.py under each arbitration policy: shape
// hub (three sources and targets, one switch per direction), funnel (two
// each, no topology: the generated crossbar) or funnel_graph (two-level
// graphs); policy auto, round_robin, fifo, rotating_priority or mixed
// (per-output overrides over the auto default); qos fixed (funnel_graph,
// fifo, fixed levels s0 1/7 and s1 7/1, one shared burst slot per source, a two-node
// response graph).
std::vector<Component> arbitration(const Params &p) {
  using P = ArbitrationPolicy;
  const auto shape = param(p, "shape", "funnel"), policy = param(p, "policy", "auto");
  const bool qos = param(p, "qos", "off") == "fixed";
  const std::map<std::string, P> globals{{"auto", P::None},
                                         {"mixed", P::None},
                                         {"round_robin", P::RoundRobin},
                                         {"fifo", P::Fifo},
                                         {"rotating_priority", P::RotatingPriority}};
  if ((shape != "hub" && shape != "funnel" && shape != "funnel_graph") || !globals.contains(policy))
    throw FixtureError(2, "arbitration: unknown shape or policy");
  if ((qos && (shape != "funnel_graph" || policy != "fifo")) ||
      (shape == "funnel" && policy == "mixed"))
    throw FixtureError(2, "arbitration: unsupported combination");
  auto d = network("arbitration_" + shape, globals.at(policy));
  for (unsigned i = 0; i < (shape == "hub" ? 3U : 2U); ++i) {
    d.initiators.push_back(axi_source("s" + std::to_string(i), 32, 2, 8));
    d.targets.push_back(axi_target("t" + std::to_string(i), {0x1000ULL * i, 0x1000}, 32, 8));
    if (qos) {
      d.initiators.back().qos.levels = i ? QosLevels{1, 7} : QosLevels{7, 1};
      d.targets.back().id_width = 2;
    } else {
      d.targets.back().max_outstanding = 2;
    }
  }
  d.transport = TransportOptions{.max_outstanding = qos ? 1U : 4U};
  // Every initiator or target attached at one node.
  const auto at = [](const auto &endpoints, const std::string &node) {
    Pairs out;
    std::ranges::transform(endpoints, std::back_inserter(out),
                           [&](const auto &e) { return std::pair{e.name, node}; });
    return out;
  };
  const auto &sources = d.initiators;
  const auto &targets = d.targets;
  using O = OutputArbitration;
  const bool mixed = policy == "mixed";
  if (shape == "hub") {
    d.topology = ExchangeTopology{graph({"hub"}, {}, at(sources, "hub"), at(targets, "hub")),
                                  graph({"back"}, {}, at(targets, "back"), at(sources, "back"))};
    if (mixed) {
      d.topology->request.nodes[0].arbitration = {
          O{"endpoint/t0", P::FixedPriority, {"endpoint/s2", "endpoint/s0", "endpoint/s1"}},
          O{"endpoint/t1", P::RotatingPriority, {}}};
      d.topology->response.nodes[0].arbitration = {
          O{"endpoint/s1", P::FixedPriority, {"endpoint/t2", "endpoint/t0", "endpoint/t1"}},
          O{"endpoint/s2", P::RotatingPriority, {}}};
    }
  } else if (shape == "funnel_graph") {
    d.topology =
        ExchangeTopology{graph({"a", "b", "m"}, {{"a", "m"}, {"b", "m"}},
                               {{"s0", "a"}, {"s1", "b"}}, at(targets, "m")),
                         qos ? graph({"n", "x"}, {{"x", "n"}}, at(targets, "x"), at(sources, "n"))
                             : graph({"x", "y", "n"}, {{"x", "n"}, {"y", "n"}},
                                     {{"t0", "x"}, {"t1", "y"}}, at(sources, "n"))};
    if (mixed)
      d.topology->request.nodes[2].arbitration = {
          O{"endpoint/t0", P::FixedPriority, {"node/b", "node/a"}},
          O{"endpoint/t1", P::RotatingPriority, {}}};
  }
  auto h = compose(d, "exchange");
  auto probes = exchange_probes(h);
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// The network of cases/target_bounded.py: two AXI4 sources (eight bursts
// each, narrow non-modifiable bursts split) and one AXI4 target keeping
// target_outstanding bursts of a shared pool; no topology (one crossbar
// switch per direction), round robin, error codes, id_bits 2 or 16.
std::vector<Component> target_bounded(const Params &p) {
  auto d = network("target_bounded", ArbitrationPolicy::RoundRobin);
  d.transport = TransportOptions{.response_codes = ResponseCodes::Distinct};
  for (const auto *name : {"s0", "s1"}) {
    auto s = axi_source(name, 32, std::stoul(param(p, "id_bits", "2")), 8);
    s.max_outstanding = 8;
    s.narrow_bursts = NarrowBursts::SplitNonModifiable;
    d.initiators.push_back(s);
  }
  d.targets = {axi_target("t0", {0x0, 0x10000}, 32, 8)};
  d.targets[0].max_outstanding = std::stoul(param(p, "target_outstanding", "4"));
  auto h = compose(d, "exchange");
  return {{"network", std::move(h.ir), std::move(h.bindings)}};
}

// The AXI4 burst exchange of two full AXI4 initiators and two AXI4 targets
// over two-level request and response graphs (unmapped bursts answer
// DECERR). ladder sets switch node words (node=bytes,...).
std::vector<Component> exchange(const Params &p) {
  auto d = network("axi4exchange", ArbitrationPolicy::RoundRobin);
  d.transport = TransportOptions{.response_codes = ResponseCodes::Distinct};
  const unsigned data = std::stoul(param(p, "data_bits", "32")),
                 ids = std::stoul(param(p, "id_bits", "4")),
                 len = std::stoul(param(p, "len_bits", "8"));
  for (unsigned i = 0; i < 2; ++i) {
    d.initiators.push_back(axi_source("s" + std::to_string(i), data, ids, len));
    d.targets.push_back(axi_target("t" + std::to_string(i), {65536ULL * i, 65536}, data, len));
    d.targets.back().id_width = ids;
  }
  d.topology = ExchangeTopology{graph({"a", "b", "m"}, {{"a", "m"}, {"b", "m"}},
                                      {{"s0", "a"}, {"s1", "b"}}, {{"t0", "m"}, {"t1", "m"}}),
                                graph({"x", "y", "n"}, {{"x", "n"}, {"y", "n"}},
                                      {{"t0", "x"}, {"t1", "y"}}, {{"s0", "n"}, {"s1", "n"}})};
  const auto ladder = param(p, "ladder", "");
  for (std::string_view rest = ladder; !rest.empty();) {
    const auto comma = rest.find(',');
    const auto entry = rest.substr(0, comma);
    const auto equal = entry.find('=');
    if (equal == std::string_view::npos)
      throw FixtureError(2, "exchange: ladder entries are node=bytes");
    for (auto *g : {&d.topology->request, &d.topology->response})
      for (auto &node : g->nodes)
        if (node.name == entry.substr(0, equal))
          node.transport_word_bytes = std::stoul(std::string(entry.substr(equal + 1)));
    rest = comma == std::string_view::npos ? std::string_view{} : rest.substr(comma + 1);
  }
  auto h = compose(d, "exchange");
  return {{"network", std::move(h.ir), std::move(h.bindings)}};
}

// Three AXI4 sources and targets over a graph whose switch nodes carry
// different transport words: request leaves (4 bytes) into merge (32), a
// 4-byte link to branch (16, t0) and tail (8, t1 and t2); responses through
// return_merge (8) and a 16-byte link to return_branch (32).
Definition graph_axi_definition() {
  auto d = network("graph_axi", ArbitrationPolicy::RoundRobin);
  d.transport = TransportOptions{.response_codes = ResponseCodes::Distinct};
  auto &t = d.topology.emplace();
  t.request.nodes = {{"merge", 32}, {"branch", 16}, {"tail", 8}};
  t.request.links = {{"merge", "branch", 4}, {"branch", "tail"}};
  t.response.nodes = {{"return_merge", 8}, {"return_branch", 32}};
  t.response.links = {{"return_merge", "return_branch", 16}};
  for (unsigned i = 0; i < 3; ++i) {
    const auto s = "s" + std::to_string(i), target = "t" + std::to_string(i),
               leaf = "leaf" + std::to_string(i);
    d.initiators.push_back(axi_source(s, 32, 4, 8));
    d.targets.push_back(axi_target(target, {65536ULL * i, 65536}, 32, 8));
    t.request.nodes.push_back({leaf, 4});
    t.request.links.push_back({leaf, "merge"});
    t.request.entries.push_back({s, leaf});
    t.request.exits.push_back({target, i == 0 ? "branch" : "tail"});
    t.response.entries.push_back({target, "return_merge"});
    t.response.exits.push_back({s, "return_branch"});
  }
  return d;
}
std::vector<Component> graph_axi(const Params &) {
  auto h = compose(graph_axi_definition(), "graph");
  return {{"network", std::move(h.ir), std::move(h.bindings)}};
}

// The three-source graph (graph_axi_definition) with mixed source widths
// (128, AXI4-Lite 32, 256 bits), selective pairs (each source reaches its
// own and the next target) and clock domains: mode internal (switch nodes
// in "middle"), endpoints (targets in "target" too) or twelve (every
// source, target set and switch node in a domain of its own). periods: one
// period in picoseconds per declared domain, in order (formula depths);
// without them the crossings take the fallback depth. Every domain is gated
// under the test port test (the default); pipes adds link pipes (below).
// Extra bindings name parts of the crossing FIFO of the request segment s0>leaf0 for faults
// (nlv/cut_*).
std::vector<Component> graph_clocked(const Params &p) {
  auto d = graph_axi_definition();
  d.name = "graph_clocked";
  d.initiators[0].data_width = 128;
  d.initiators[1].protocol = Protocol::Axi4Lite;
  d.initiators[1].id_width.reset();
  d.initiators[1].len_width.reset();
  d.initiators[2].data_width = 256;
  d.connectivity = Connectivity{};
  for (unsigned i = 0; i < 3; ++i)
    for (unsigned j = 0; j < 2; ++j)
      d.connectivity->pairs.push_back({"s" + std::to_string(i), "t" + std::to_string((i + j) % 3)});
  auto &request = d.topology->request.nodes, &response = d.topology->response.nodes;
  const auto domains = [&](const std::vector<std::string> &names) {
    d.clock_domains.clear();
    for (const auto &name : names)
      d.clock_domains.push_back({name, "clk_" + name, "reset_n", "test"});
  };
  // main -> middle and the response return take two synchronizer stages;
  // middle -> main takes four.
  domains({"main", "middle"});
  d.synchronizer_stages = 2;
  d.clock_crossings = {{"middle", "main", std::nullopt, 4}};
  for (auto *nodes : {&request, &response})
    for (auto &n : *nodes)
      n.clock_domain = n.name == "branch" || n.name == "return_branch" ? "main" : "middle";
  const auto mode = param(p, "mode", "internal");
  if (mode == "endpoints") {
    domains({"main", "middle", "target"});
    d.clock_crossings = {{"middle", "main", std::nullopt, 4},
                         {"middle", "target", std::nullopt, 3}};
    for (auto &t : d.targets)
      t.clock_domain = "target";
    for (auto *nodes : {&request, &response})
      for (auto &n : *nodes)
        n.clock_domain = "middle";
  } else if (mode == "twelve") {
    std::vector<std::string> names{"main", "source1", "source2", "target"};
    d.clock_crossings.clear();
    d.synchronizer_stages = 3;
    for (unsigned i = 1; i < d.initiators.size(); ++i)
      d.initiators[i].clock_domain = "source" + std::to_string(i);
    for (auto &t : d.targets)
      t.clock_domain = "target";
    for (auto *nodes : {&request, &response})
      for (auto &n : *nodes) {
        names.push_back("node" + std::to_string(names.size() - 4));
        n.clock_domain = names.back();
      }
    domains(names);
  } else if (mode != "internal") {
    throw FixtureError(2, "graph_clocked: unknown mode " + mode);
  }
  const auto periods = param(p, "periods", "");
  if (!periods.empty()) {
    std::stringstream list(periods);
    std::string period;
    for (auto &domain : d.clock_domains) {
      if (!std::getline(list, period, ','))
        throw FixtureError(2, "graph_clocked: one period per domain");
      domain.period_ps = std::stoull(period);
    }
  }
  // pipes=1: every link takes a sender and a receiver pipe and every attachment a pipe at its
  // switch, the modes (both, forward, backward) rotating along the lists.
  if (param(p, "pipes", "0") == "1") {
    const LinkPipeline modes[] = {{true, true}, {true, false}, {false, true}};
    unsigned k = 0;
    for (auto *g : {&d.topology->request, &d.topology->response}) {
      for (auto &link : g->links) {
        link.sender_pipeline = modes[k++ % 3];
        link.pipeline = modes[k % 3];
      }
      for (auto *attachments : {&g->entries, &g->exits})
        for (auto &a : *attachments)
          a.pipeline = modes[k++ % 3];
    }
  }
  auto h = compose(d, "clocked graph");
  const auto &cut = *std::ranges::find(h.plan.cuts, "req/s0>" + hardware_node(h, "req_", "leaf0"),
                                       &ClockCut::segment);
  const auto from = std::ranges::find(h.plan.domains, cut.from_domain, &DomainPlan::name);
  const auto to = std::ranges::find(h.plan.domains, cut.to_domain, &DomainPlan::name);
  const auto &options = std::get<AsyncClockCut>(cut.options);
  const auto fifo = take(
      lower_async_fifo(*from, *to, options.depth.depth, options.synchronizer_stages, cut.fields,
                       options.reset_handshake, h.plan.reset_coverage == ResetCoverage::All),
      "fifo");
  auto mapped = [&](ValueId id) {
    return h.bindings.at(cut.name + "/ir/" + std::to_string(id.index));
  };
  const auto data = std::ranges::find(fifo.fields, std::string("data0"), &FifoField::name);
  if (data == fifo.fields.end())
    throw FixtureError(3, "graph_clocked: no data0 field");
  h.bindings["nlv/cut_count"] = h.bindings.at(cut.name + "/write_count");
  h.bindings["nlv/cut_full_constant"] = mapped(fifo.ir.values.at(fifo.full.index).operands[1]);
  h.bindings["nlv/cut_received"] = mapped(data->received);
  h.bindings["nlv/cut_read"] = mapped(fifo.read);
  h.bindings["nlv/cut_nonempty"] = mapped(fifo.nonempty);
  h.bindings["nlv/cut_slot0"] = mapped(data->storage.front());
  h.bindings["nlv/cut_selected"] = mapped(data->selected);
  return {{"network", std::move(h.ir), std::move(h.bindings)}};
}

// Probe groups naming the valid and ready of every switch hop of both graphs.
Json hop_probes(const ExchangeHardware &h) {
  auto label = [&](const std::string &key) { return h.plan.label(key); };
  Json probes = Json::array();
  for (const bool response : {false, true}) {
    const auto &g = response ? h.plan.exchange.response : h.plan.exchange.request;
    const std::string prefix = response ? "graph/rsp/" : "graph/req/";
    Json group = {{"name", std::string("hops/") + (response ? "response" : "request")},
                  {"kind", "hops"},
                  {"graph", response ? "response" : "request"},
                  {"hops", Json::array()},
                  {"nets", Json::array()}};
    for (const auto &o : g.outputs)
      for (const auto &input : o.inputs) {
        const auto output = (o.endpoint ? "endpoint/" : "node/") + o.output;
        const auto base = prefix + o.node + ">" + output + ".hop/" + input;
        const auto k = std::to_string(group["hops"].size());
        group["hops"].push_back({label(input), label("node/" + o.node).substr(5), label(output)});
        group["nets"].push_back({"valid" + k, base + ".valid"});
        group["nets"].push_back({"ready" + k, base + ".ready"});
      }
    probes.push_back(std::move(group));
  }
  return probes;
}

// The network of cases/explicit_routes.py: AXI4 sources i0 and i1 and APB4 targets t0 and t1 over
// request nodes qa..qd (links both ways between qb and qc) and response nodes ra..rd, every pair
// on an explicit route. Shapes: merged attaches both sources to qm ahead of qa, so their request
// routes toward one target leave qa by different links; response_merged attaches both targets to
// rm ahead of ra, so their response routes toward one source leave ra by different links; repeat
// routes i1 to t0 through qb twice; flows gives i0 two flows (AxID classes:
// bit 0 clear, else) whose routes to each target differ in both graphs; target_paths
// adds two target flows each: t0 two APB port sets (windows 0x0 and 0x1000) and t1 an AXI4 target
// (windows 0x2000 and 0x3000, the flow in AxID bit 0), every initiator flow and target flow pair on
// its own route (swap_target_paths=1 swaps the flows of the windows); ahb_flows and
// lite_flows make t0 an AHB target, or an AXI4-Lite target of two packets in flight, of a port set
// per flow; conn_flows is target_paths with the connectivity matrix of
// cases/explicit_routes.py by flow pair (no route, and no link qb > qc, for the flow pairs it
// leaves out). Probe groups name every switch hop's valid and ready per graph.
std::vector<Component> explicit_routes(const Params &p) {
  const auto shape = param(p, "shape", "diamond");
  const bool target_paths = shape == "target_paths" || shape == "ahb_flows" ||
                            shape == "lite_flows" || shape == "conn_flows";
  if (shape != "diamond" && shape != "merged" && shape != "response_merged" && shape != "repeat" &&
      shape != "flows" && !target_paths)
    throw FixtureError(2, "shape is diamond, merged, response_merged, repeat, flows, "
                          "target_paths, ahb_flows, lite_flows or conn_flows");
  const bool merged = shape == "merged", response_merged = shape == "response_merged";
  auto d = network(shape == "diamond" ? std::string("explicit_routes") : "explicit_routes_" + shape,
                   ArbitrationPolicy::None);
  for (const auto *name : {"i0", "i1"})
    d.initiators.push_back(axi_source(name, 32, 2, 8));
  if (shape == "flows" || target_paths) {
    d.initiators.front().route_classes = {"even", "odd"};
    d.initiators.front().default_class = "odd";
    d.initiators.front().id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  }
  for (unsigned i = 0; i < 2; ++i) {
    Target t{"t" + std::to_string(i), "main", {{0x1000ULL * i, 0x1000}}};
    t.protocol = Protocol::Apb4;
    t.data_width = 32;
    t.address_width = 16;
    t.attributes = TargetAttributes::amba();
    d.targets.push_back(t);
  }
  if (target_paths) {
    auto &apb = d.targets[0], &axi = d.targets[1];
    apb.flows = axi.flows = 2;
    // swap_target_paths=1 gives each window the other flow (a negative control).
    const unsigned low = param(p, "swap_target_paths", "0") == "1" ? 1 : 0;
    apb.mappings = {{.base = 0x0, .size = 0x1000, .flow = low},
                    {.base = 0x1000, .size = 0x1000, .flow = 1 - low}};
    axi.mappings = {{.base = 0x2000, .size = 0x1000, .flow = low},
                    {.base = 0x3000, .size = 0x1000, .flow = 1 - low}};
    axi.protocol = Protocol::Axi4;
    axi.address_width.reset();
    axi.id_width = 3;
    axi.len_width = 8;
    if (shape == "ahb_flows")
      apb.protocol = Protocol::AhbV3;
    if (shape == "lite_flows") {
      apb.protocol = Protocol::Axi4Lite;
      apb.max_outstanding = 2;
    }
  }
  const std::string q = merged ? "qm" : "qa", r = response_merged ? "rm" : "ra";
  Pairs request_links{{"qa", "qb"}, {"qa", "qc"}, {"qb", "qc"},
                      {"qc", "qb"}, {"qb", "qd"}, {"qc", "qd"}};
  const bool connected = shape == "conn_flows";
  if (connected) {
    request_links.erase(request_links.begin() + 2);
    d.connectivity = Connectivity{.pairs = {{"i0", "t0", 0U, 0U},
                                            {"i0", "t0", 1U, std::nullopt},
                                            {"i0", "t1", 0U, std::nullopt},
                                            {"i0", "t1", 1U, 1U},
                                            {"i1", "t0", std::nullopt, 1U},
                                            {"i1", "t1"}}};
  }
  Pairs response_links{{"ra", "rb"}, {"ra", "rc"}, {"rb", "rd"}, {"rc", "rd"}, {"rc", "rb"}};
  std::vector<std::string> request_nodes{"qa", "qb", "qc", "qd"},
      response_nodes{"ra", "rb", "rc", "rd"};
  if (merged) {
    request_nodes.push_back("qm");
    request_links.emplace_back("qm", "qa");
  }
  if (response_merged) {
    response_nodes.push_back("rm");
    response_links.emplace_back("rm", "ra");
  }
  d.topology = ExchangeTopology{
      graph(request_nodes, request_links, {{"i0", q}, {"i1", q}}, {{"t0", "qd"}, {"t1", "qd"}}),
      graph(response_nodes, response_links, {{"t0", r}, {"t1", r}}, {{"i0", "rd"}, {"i1", "rd"}})};
  using Path = std::vector<std::string>;
  // (initiator, target, request, response, initiator flow, target flow)
  std::vector<std::tuple<std::string, std::string, Path, Path, unsigned, unsigned>> routes{
      {"i0", "t0", {"qa", "qb", "qd"}, {"ra", "rc", "rd"}, 0, 0},
      {"i0", "t1", {"qa", "qc", "qd"}, {"ra", "rb", "rd"}, 0, 0},
      {"i1", "t0", {"qa", "qc", "qb", "qd"}, {"ra", "rb", "rd"}, 0, 0},
      {"i1", "t1", {"qa", "qb", "qc", "qd"}, {"ra", "rc", "rb", "rd"}, 0, 0}};
  if (merged)
    for (auto &route : routes)
      std::get<2>(route).insert(std::get<2>(route).begin(), "qm");
  if (response_merged) {
    std::get<3>(routes[0]) = {"rm", "ra", "rc", "rd"};
    std::get<3>(routes[1]) = {"rm", "ra", "rb", "rd"};
    std::get<3>(routes[2]) = {"rm", "ra", "rb", "rd"};
    std::get<3>(routes[3]) = {"rm", "ra", "rc", "rb", "rd"};
  }
  if (shape == "repeat")
    std::get<2>(routes[2]) = {"qa", "qb", "qc", "qb", "qd"};
  if (shape == "flows")
    routes = {{"i0", "t0", {"qa", "qb", "qd"}, {"ra", "rb", "rd"}, 0, 0},
              {"i0", "t0", {"qa", "qc", "qd"}, {"ra", "rc", "rd"}, 1, 0},
              {"i0", "t1", {"qa", "qc", "qd"}, {"ra", "rc", "rd"}, 0, 0},
              {"i0", "t1", {"qa", "qb", "qd"}, {"ra", "rc", "rb", "rd"}, 1, 0},
              {"i1", "t0", {"qa", "qc", "qb", "qd"}, {"ra", "rc", "rb", "rd"}, 0, 0},
              {"i1", "t1", {"qa", "qb", "qc", "qd"}, {"ra", "rb", "rd"}, 0, 0}};
  if (target_paths)
    routes = {{"i0", "t0", {"qa", "qb", "qd"}, {"ra", "rb", "rd"}, 0, 0},
              {"i0", "t0", {"qa", "qc", "qd"}, {"ra", "rc", "rd"}, 0, 1},
              {"i0", "t0", {"qa", "qc", "qd"}, {"ra", "rc", "rd"}, 1, 0},
              {"i0", "t0", {"qa", "qb", "qd"}, {"ra", "rb", "rd"}, 1, 1},
              {"i0", "t1", {"qa", "qc", "qb", "qd"}, {"ra", "rc", "rb", "rd"}, 0, 0},
              {"i0", "t1", {"qa", "qb", "qd"}, {"ra", "rb", "rd"}, 0, 1},
              {"i0", "t1", {"qa", "qb", "qc", "qd"}, {"ra", "rb", "rd"}, 1, 0},
              {"i0", "t1", {"qa", "qc", "qd"}, {"ra", "rc", "rd"}, 1, 1},
              {"i1", "t0", {"qa", "qb", "qc", "qd"}, {"ra", "rc", "rd"}, 0, 0},
              {"i1", "t0", {"qa", "qc", "qb", "qd"}, {"ra", "rb", "rd"}, 0, 1},
              {"i1", "t1", {"qa", "qc", "qd"}, {"ra", "rb", "rd"}, 0, 0},
              {"i1", "t1", {"qa", "qb", "qd"}, {"ra", "rc", "rb", "rd"}, 0, 1}};
  if (connected) {
    // The flow pairs left out take no route; i0 flow 1 to t0 flow 1 takes qc.
    std::erase_if(routes, [](const auto &r) {
      const auto &[i, t, q, rp, f, g] = r;
      return (i == "i0" && t == "t0" && f == 0 && g == 1) ||
             (i == "i0" && t == "t1" && f == 1 && g == 0) ||
             (i == "i1" && t == "t0" && f == 0 && g == 0);
    });
    for (auto &route : routes)
      if (std::get<0>(route) == "i0" && std::get<1>(route) == "t0" && std::get<4>(route) == 1 &&
          std::get<5>(route) == 1)
        std::tie(std::get<2>(route), std::get<3>(route)) =
            std::pair{Path{"qa", "qc", "qd"}, Path{"ra", "rc", "rd"}};
  }
  for (const auto &[i, t, request, response, flow, target_path] : routes)
    d.topology->routes.push_back({i, t, request, response, flow, target_path});
  auto h = compose(d, "explicit_routes");
  auto probes = hop_probes(h);
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// A two-flow remap network with hop probes: an AXI4 source transactionSourcePort0 whose flows split
// by AxID classes (bit 0 clear, else), two APB4 targets that share a 4 KiB
// window by the Boot mode, and request and response graphs whose middle switch depends on the
// flow alone. packet_bytes, when given, bounds the packets of both pairs.
std::vector<Component> flow_remap(const Params &p) {
  Definition d;
  d.name = "c1";
  d.address_width = 32;
  d.reset_ports = {{"RstN"}};
  d.test_ports = {{"test"}};
  d.address_inputs = {{"Boot", 1, "main"}};
  d.address_selectors = {{"remapped", {{"Boot", 1}}}};
  d.clock_domains = {{"main", "Clk", "RstN", "test"}};
  d.connectivity = Connectivity{.all = true};
  if (const auto bytes = param(p, "packet_bytes", ""); !bytes.empty()) {
    d.transport = TransportOptions{};
    for (const auto *target : {"memoryTargetPort0", "memoryTargetPort1"})
      d.transport->packet_limits.push_back({"transactionSourcePort0", target, std::stoull(bytes)});
  }
  auto source = axi_source("transactionSourcePort0", 32, 2, 4);
  source.route_classes = {"even", "odd"};
  source.default_class = "odd";
  source.id_ranges = {{0, 0, "even"}, {2, 2, "even"}};
  d.initiators.push_back(source);
  for (unsigned i = 0; i < 2; ++i) {
    Target t{"memoryTargetPort" + std::to_string(i), "main", {{0, 0x1000}}};
    t.mappings.front().when = {{"remapped", i == 1}};
    t.protocol = Protocol::Apb4;
    t.data_width = 32;
    t.address_width = 16;
    t.attributes = TargetAttributes::amba();
    d.targets.push_back(t);
  }
  const std::string i0 = "transactionSourcePort0", t0 = "memoryTargetPort0",
                    t1 = "memoryTargetPort1";
  d.topology =
      ExchangeTopology{graph({"reqIngress", "reqSwitch", "reqSwitch1", "reqEgress"},
                             {{"reqIngress", "reqSwitch"},
                              {"reqIngress", "reqSwitch1"},
                              {"reqSwitch", "reqEgress"},
                              {"reqSwitch1", "reqEgress"}},
                             {{i0, "reqIngress"}}, {{t0, "reqEgress"}, {t1, "reqEgress"}}),
                       graph({"rspIngress", "rspSwitch", "rspSwitch1", "rspEgress"},
                             {{"rspIngress", "rspSwitch"},
                              {"rspIngress", "rspSwitch1"},
                              {"rspSwitch", "rspEgress"},
                              {"rspSwitch1", "rspEgress"}},
                             {{t0, "rspIngress"}, {t1, "rspIngress"}}, {{i0, "rspEgress"}})};
  for (const unsigned flow : {0U, 1U})
    for (const auto &t : {t0, t1}) {
      const std::string middle = flow ? "1" : "";
      d.topology->routes.push_back({i0,
                                    t,
                                    {"reqIngress", "reqSwitch" + middle, "reqEgress"},
                                    {"rspIngress", "rspSwitch" + middle, "rspEgress"},
                                    flow});
    }
  auto h = compose(d, "flow_remap");
  auto probes = hop_probes(h);
  return {{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)}};
}

// Register banks alone. The custom bank carries the field kinds at offsets of this test's choosing;
// the error logger uses its own table, named to the bench by the register_map probe group. Bus
// signals leave as observation ports of this fixture.
std::vector<Component> register_bank(const Params &p) {
  using A = RegisterAccess;
  RegisterBankPlan custom{"custom",
                          1,
                          {{"identity", 0x00, 0, 32, A::Constant, 0x12345678},
                           {"revision", 0x01, 0, 32, A::Constant, 0x00000100},
                           {"option_a", 0x02, 0, 1, A::Control},
                           {"option_b", 0x04, 0, 1, A::Control},
                           {"status16", 0x13, 8, 16, A::Status},
                           {"control32", 0x10, 0, 32, A::Control},
                           {"control8", 0x11, 0, 8, A::Control},
                           {"pulse4", 0x12, 0, 4, A::Pulse}}};
  // The wide record measures the read multiplexer; the bench drives the narrow one.
  const auto shape = param(p, "record", "narrow") == "wide" ? ErrorRecordShape{12, 46, 72, 8, 8, 3}
                                                            : ErrorRecordShape{3, 32, 0, 0, 2, 0};
  const auto observed = [](std::string label, const RegisterBankPlan &plan) {
    auto node = take(lower_register_bank(plan), "register bank");
    Builder b;
    b.ir = std::move(node.ir);
    for (const auto &[signal, binding] :
         std::map<std::string, std::string>{{"word_index", "access.word_index"},
                                            {"write_active", "access.write_active"},
                                            {"read_active", "access.read_active"},
                                            {"complete", "access.complete"},
                                            {"write_mask", "access.write_mask"},
                                            {"write_value", "access.write_value"},
                                            {"read_value", "access.read_value"}})
      b.output("nl_access_" + signal, node.bindings.at(binding), "access", signal);
    b.output("nl_bus_lanes", node.bindings.at("bus.lanes"), "bus", "lanes");
    Json words = Json::object();
    for (const auto &f : plan.fields)
      words[f.name] = {{"word", f.word}, {"lsb", f.lsb}, {"width", f.width}, {"value", f.value}};
    Json groups = Json::array();
    groups.push_back({{"name", "register_map"},
                      {"kind", "register_map"},
                      {"nets", Json::array()},
                      {"fields", words}});
    return Component{std::move(label), std::move(b.ir), std::move(node.bindings), groups};
  };
  std::vector<Component> out;
  out.push_back(observed("custom", custom));
  // revision=<n> gives the logger's revision register that value in place of the generator version.
  const auto revision = param(p, "revision", "");
  auto logger = error_logger_bank(
      "logger", 0, shape,
      revision.empty() ? std::nullopt
                       : std::optional(static_cast<std::uint32_t>(std::stoul(revision))));
  // A negative control: the user byte of the revision word made writable.
  if (param(p, "revision_reserved", "constant") == "writable")
    std::ranges::find(logger.fields, "revision_reserved", &RegisterField::name)->access =
        A::Control;
  out.push_back(observed("logger", logger));
  return out;
}

// The security flags of a network: flags, drivers per source and flag ("zero", "one", "privileged",
// "nonsecure", "instruction" or "user:<bit>", with a leading ~ to invert).
void read_security(const Json &spec, InternalNetwork &internal) {
  spec.at("flags").get_to(internal.security.flags);
  const auto driver = [](std::string text) {
    AttributeBit bit;
    if (text.starts_with('~')) {
      bit.invert = true;
      text.erase(0, 1);
    }
    if (text.starts_with("user:")) {
      bit.source = AttributeSource::User;
      bit.index = static_cast<unsigned>(std::stoul(text.substr(5)));
      return bit;
    }
    static const std::map<std::string, AttributeSource> named{
        {"zero", AttributeSource::Zero},
        {"one", AttributeSource::One},
        {"privileged", AttributeSource::Privileged},
        {"nonsecure", AttributeSource::Nonsecure},
        {"instruction", AttributeSource::Instruction}};
    bit.source = named.at(text);
    return bit;
  };
  for (const auto &[source, drivers] : spec.at("drivers").items())
    for (const auto &[flag, text] : drivers.items())
      internal.security.drivers[source][flag] = driver(text.get<std::string>());
}
// A logger's filters from its JSON: security_filter and user_filter (flag: 0 or 1).
void read_filters(const Json &spec, FaultMonitorRequest &observer) {
  const auto filter = [&](const char *key, std::map<std::string, bool> &into) {
    const auto wanted = spec.value(key, Json::object());
    for (const auto &[flag, value] : wanted.items())
      into[flag] = value.get<int>() != 0;
  };
  filter("security_filter", observer.security_filter);
  filter("user_filter", observer.user_filter);
}
// Several observers on the service target from a JSON file: sec (see read_security, or null) and
// observers, each with name, targets and loggers (base, route, ignored, forward, and read_filters).
void read_observers(const std::string &path, const std::string &service, bool keep,
                    InternalNetwork &internal) {
  std::ifstream file(path);
  const auto spec = Json::parse(file);
  if (const auto &sec = spec.at("sec"); !sec.is_null())
    read_security(sec, internal);
  for (const auto &entry : spec.at("observers")) {
    FaultMonitorRequest observer{entry.at("name").get<std::string>(), service, {}, {}};
    entry.at("targets").get_to(observer.targets);
    observer.keep_fields = keep;
    observer.clock_domain = entry.value("domain", std::string{});
    const auto &first = entry.at("loggers").at(0);
    first.at("ignored").get_to(observer.ignored_codes);
    read_filters(first, observer);
    observer.route = first.at("route").get<unsigned>();
    observer.base = first.at("base").get<std::uint64_t>();
    observer.forward = first.value("forward", 0) != 0;
    const auto &loggers = entry.at("loggers");
    for (std::size_t n = 1; n < loggers.size(); ++n) {
      const auto &logger = loggers.at(n);
      FaultMonitorRequest filters;
      read_filters(logger, filters);
      observer.chain.push_back(
          {logger.at("ignored").get<std::vector<unsigned>>(), std::move(filters.user_filter),
           std::move(filters.security_filter), logger.at("route").get<unsigned>(),
           logger.at("base").get<std::uint64_t>(), logger.value("forward", 0) != 0});
    }
    internal.fault_monitors.push_back(std::move(observer));
  }
}

// A network of a YAML definition (parameter config), one target of it (parameter service) serving
// the error logger's register bank. Definition validation holds as for any target.
std::vector<Component> service_network(const Params &p) {
  std::ifstream file(param(p, "config", ""));
  std::stringstream text;
  text << file.rdbuf();
  const auto d = take(config::parse(text.str()), "definition").definition;
  const ErrorRecordShape shape{3, 32, 0, 0, 2, 0};
  InternalNetwork internal;
  internal.service_targets[param(p, "service", "")] = error_logger_bank("logger", 0, shape);
  if (const auto seen = param(p, "observe", ""); !seen.empty()) {
    FaultMonitorRequest observer{
        param(p, "observer", "transactionFaultMonitor"), param(p, "service", ""), {}, {}};
    std::stringstream names(seen);
    for (std::string name; std::getline(names, name, ',');)
      observer.targets.push_back(name);
    std::stringstream codes(param(p, "ignored", ""));
    for (std::string code; std::getline(codes, code, ',');)
      observer.ignored_codes.push_back(static_cast<unsigned>(std::stoul(code)));
    observer.keep_fields = param(p, "keep", "") == "1";
    if (const auto security = param(p, "security", ""); !security.empty()) {
      std::ifstream spec_file(security);
      const auto spec = Json::parse(spec_file);
      read_security(spec, internal);
      read_filters(spec, observer);
    }
    internal.fault_monitors.push_back(std::move(observer));
  }
  if (const auto observers = param(p, "observers", ""); !observers.empty())
    read_observers(observers, param(p, "service", ""), param(p, "keep", "") == "1", internal);
  for (auto &observer : internal.fault_monitors)
    observer.serial_word_bits =
        static_cast<unsigned>(std::stoul(param(p, "serial_word_bits", "32")));
  // A clock domain that only a logger owns: the definition alone does not pass.
  if (std::ranges::all_of(internal.fault_monitors,
                          [](const auto &observer) { return observer.clock_domain.empty(); }))
    take(validate(d), "definition");
  const auto plan = take(build_network_plan(d, internal), "plan");
  // The product's artifacts of this plan, for a bench that reads its map.
  if (const auto dir = param(p, "artifacts", ""); !dir.empty()) {
    std::filesystem::create_directories(dir);
    for (const auto &artifact : take(emit_axi4_network(plan, register_reset), "artifacts"))
      std::ofstream(std::filesystem::path(dir) / artifact.path, std::ios::binary) << artifact.text;
  }
  auto h = take(lower_axi4_exchange_composition(plan, register_reset), "composition");
  take(verify_axi4_exchange_composition(h), "composition");
  // The serial words and answers at the bank, for a bench that compares them.
  const auto key = "burst_service/" + param(p, "service", "") + "/";
  Json nets = Json::array();
  for (const auto &[field, binding] :
       std::vector<std::pair<std::string, std::string>>{{"req_valid", "bus.serving"},
                                                        {"req_data", "bus.word"},
                                                        {"req_tail", "bus.at_last"},
                                                        {"req_ready", "bank/request.ready"},
                                                        {"rsp_valid", "bank/response.offer"},
                                                        {"rsp_data", "bank/response.data"},
                                                        {"rsp_head", "bank/response.head"},
                                                        {"rsp_tail", "bank/response.tail"}})
    nets.push_back(Json::array({field, key + binding}));
  // Past a crossing: the serial words where the bank takes them, each crossing's hand-offs, and the
  // answer leaving the target.
  const auto stem = "cut/service/" + param(p, "service", "");
  if (const auto cut = stem + (h.bindings.contains(stem + "/request/read") ? "" : "/branch0");
      h.bindings.contains(cut + "/request/read")) {
    nets = Json::array();
    const auto request = cut + "/request/", response = cut + "/response/";
    for (const auto &[field, binding] : std::vector<std::pair<std::string, std::string>>{
             {"req_valid", request + "output.valid"},
             {"req_data", request + "output.field0"},
             {"req_head", request + "output.field1"},
             {"req_tail", request + "output.field2"},
             {"req_ready", request + "output.ready"},
             {"rsp_valid", response + "input.valid"},
             {"rsp_data", response + "input.field0"},
             {"rsp_tail", response + "input.field1"},
             {"rsp_ready", response + "input.ready"},
             {"wr_en", key + "bank/access.write_active"},
             {"rd_en", key + "bank/access.read_active"},
             {"fifo_read", response + "read"},
             {"req_in_valid", request + "input.valid"},
             {"req_in_ready", request + "input.ready"},
             {"req_write_count", request + "write_count"},
             {"req_read_count", request + "read_count"},
             {"rsp_write_count", response + "write_count"},
             {"rsp_read_count", response + "read_count"},
             {"reply_valid", key + "reply.valid"},
             {"reply_ready", key + "reply.ready"}})
      nets.push_back(Json::array({field, binding}));
  }
  Json probes = Json::array();
  probes.push_back({{"name", "bank"}, {"kind", "bank"}, {"nets", nets}});
  if (h.bindings.contains("observe/errors/logger.valid"))
    probes.push_back(
        {{"name", "record"},
         {"kind", "record"},
         {"nets", Json::array({{"valid", "observe/errors/logger.valid"},
                               {"capture", "observe/errors/logger.capture"},
                               {"alarm", "burst_service/csr/bank/field.alarm_enable"},
                               {"clear", "burst_service/csr/bank/field.record_clear_valid"}})}});
  std::vector<Component> out;
  out.push_back(Component{"network", std::move(h.ir), std::move(h.bindings), std::move(probes)});
  return out;
}

std::vector<Component> replay_safety(const Params &p) {
  const auto kind = param(p, "kind", "merge");
  const unsigned capacity = std::stoul(param(p, "capacity", "2"));
  auto h = [&] {
    if (kind == "ahb") {
      BurstOutletProfile q{"safety", 2, 4};
      q.hresp_width = 2;
      q.fixed_single = true;
      return take(lower_ahb_outlet(q), "ahb");
    }
    if (kind == "outlet") {
      Axi4OutletProfile q;
      q.name = "safety";
      q.id_bits = 2;
      q.source_bits = 1;
      q.id_sources = {0};
      q.len_bits = 4;
      q.max_outstanding = capacity;
      q.start_address = q.interleave = q.align_wrap = true;
      return take(lower_axi4_outlet(q), "outlet");
    }
    Axi4UpsizeProfile q;
    q.name = "safety";
    q.data_bits = 32;
    q.id_bits = 2;
    q.len_bits = 4;
    q.max_outstanding = capacity;
    q.joined = true;
    return take(lower_axi4_upsize(q), "merge");
  }();
  for (const auto &[name, id] : h.bindings) {
    auto signal = "nl_probe_" + name;
    std::ranges::replace(signal, '.', '_');
    const auto width = h.ir.values.at(id.index).width;
    h.ir.ports.push_back({signal, false, width, "probe", name});
    h.ir.drivers.push_back({PortId{h.ir.ports.size() - 1}, id});
  }
  return {{"dut", std::move(h.ir), std::move(h.bindings)}};
}

std::vector<Component> build(const std::string &recipe, const Params &p) {
  static const std::map<std::string, std::function<std::vector<Component>(const Params &)>> recipes{
      {"lite_target", lite_target},
      {"axi4_width_cut", axi4_width_cut},
      {"qos_bounded", qos_bounded},
      {"pressure_network", pressure_network},
      {"response_freeze", response_freeze},
      {"arbitration", arbitration},
      {"exchange", exchange},
      {"graph_axi", graph_axi},
      {"graph_clocked", graph_clocked},
      {"axi4_outlet", axi4_outlet},
      {"axi4_upsize", axi4_upsize},
      {"replay_safety", replay_safety},
      {"target_bounded", target_bounded},
      {"apb_outlet", apb_outlet},
      {"ahb_outlet", ahb_outlet},
      {"explicit_routes", explicit_routes},
      {"flow_remap", flow_remap},
      {"register_bank", register_bank},
      {"service_network", service_network}};
  const auto it = recipes.find(recipe);
  if (it == recipes.end())
    throw FixtureError(2, "unknown recipe " + recipe);
  const auto reset = param(p, "register_reset", "asynchronous");
  if (reset != "asynchronous" && reset != "synchronous")
    throw FixtureError(2, "register_reset is asynchronous or synchronous");
  register_reset =
      reset == "synchronous" ? RegisterReset::Synchronous : RegisterReset::Asynchronous;
  auto components = it->second(p);
  for (auto &c : components)
    c.ir.synchronous_reset = register_reset == RegisterReset::Synchronous;
  return components;
}

// ---------------------------------------------------------------------------
// IR helpers.

// The core operand count: a clock gate has three (clock, enable, test).
unsigned arity(Op op) { return operand_count(op); }

const std::map<std::string, Op> &op_names() {
  static const std::map<std::string, Op> names{
      {"Input", Op::Input},           {"Constant", Op::Constant},
      {"ZeroExtend", Op::ZeroExtend}, {"UnsignedLess", Op::UnsignedLess},
      {"BitNot", Op::BitNot},         {"BitAnd", Op::BitAnd},
      {"BitOr", Op::BitOr},           {"Select", Op::Select},
      {"Equal", Op::Equal},           {"State", Op::State},
      {"Subtract", Op::Subtract},     {"Slice", Op::Slice},
      {"Concat", Op::Concat}};
  return names;
}

std::string op_name(Op op) {
  for (const auto &[name, value] : op_names())
    if (value == op)
      return name;
  return "?";
}

Op parse_op(const std::string &name) {
  const auto it = op_names().find(name);
  if (it == op_names().end())
    throw FixtureError(2, "unknown operator " + name);
  return it->second;
}

bool commutative(Op op) { return op == Op::BitAnd || op == Op::BitOr || op == Op::Equal; }

bool glob(const std::string &pattern, const std::string &text) {
  // '*' matches any run of characters; everything else is literal.
  std::size_t p = 0, t = 0, star = std::string::npos, mark = 0;
  while (t < text.size()) {
    if (p < pattern.size() && pattern[p] == '*') {
      star = p++;
      mark = t;
    } else if (p < pattern.size() && pattern[p] == text[t]) {
      ++p;
      ++t;
    } else if (star != std::string::npos) {
      p = star + 1;
      t = ++mark;
    } else
      return false;
  }
  while (p < pattern.size() && pattern[p] == '*')
    ++p;
  return p == pattern.size();
}

// Renumbers values so that operands precede users, as verify_structure
// requires. A combinational loop throws.
void renumber(Component &c) {
  auto &ir = c.ir;
  const auto n = ir.values.size();
  std::vector<int> mark(n, 0);
  std::vector<std::size_t> order;
  order.reserve(n);
  for (std::size_t root = 0; root < n; ++root) {
    if (mark[root])
      continue;
    std::vector<std::pair<std::size_t, unsigned>> stack{{root, 0}};
    mark[root] = 1;
    while (!stack.empty()) {
      auto &[v, next] = stack.back();
      const auto &value = ir.values[v];
      if (next < arity(value.op)) {
        const auto child = value.operands[next++].index;
        if (mark[child] == 1)
          throw FixtureError(4, "combinational loop through value of " + c.label);
        if (!mark[child]) {
          mark[child] = 1;
          stack.push_back({child, 0});
        }
        continue;
      }
      mark[v] = 2;
      order.push_back(v);
      stack.pop_back();
    }
  }
  std::vector<std::size_t> remap(n);
  for (std::size_t i = 0; i < n; ++i)
    remap[order[i]] = i;
  auto fix = [&](ValueId &id) { id.index = remap[id.index]; };
  std::vector<Value> values(n);
  for (std::size_t i = 0; i < n; ++i) {
    values[remap[i]] = ir.values[i];
    auto &v = values[remap[i]];
    for (unsigned j = 0; j < arity(v.op); ++j)
      fix(v.operands[j]);
  }
  ir.values = std::move(values);
  for (auto &d : ir.drivers)
    fix(d.value);
  for (auto &s : ir.states) {
    fix(s.output);
    fix(s.next);
    fix(s.clock);
    if (s.reset)
      fix(s.reset->signal);
  }
  for (auto &s : ir.synchronizer_stages)
    fix(s);
  for (auto &[name, id] : c.bindings)
    fix(id);
  // The permutation interleaves a canonical IR's emitted prefix with its
  // observation logic and the appended fault logic, so the faulted copy (a
  // test artifact) emits every value. The observation logic only reads the
  // hardware; the sel=0 event hash equal to the baseline confirms it.
  ir.emitted.reset();
}

// ---------------------------------------------------------------------------
// Fault engine.

struct Target {
  ValueId value;
  std::string binding; // the binding name when the selector named one
};

class Engine {
public:
  explicit Engine(Component &c) : c_(c) {
    b_.ir = std::move(c.ir);
    c.ir = HardwareIR{};
    sel_ = b_.input("nl_nlv_fault_sel", 8, "nlv", "fault_sel");
  }
  Engine(const Engine &) = delete;
  Engine &operator=(const Engine &) = delete;
  ~Engine() { c_.ir = std::move(b_.ir); }

  std::vector<Target> resolve(const Json &s) {
    auto &ir = b_.ir;
    if (s.contains("binding")) {
      const auto name = s.at("binding").get<std::string>();
      const auto it = c_.bindings.find(name);
      if (it == c_.bindings.end())
        throw FixtureError(3, "binding not found: " + name);
      return {{it->second, name}};
    }
    if (s.contains("glob")) {
      std::vector<Target> out;
      for (const auto &[name, id] : c_.bindings)
        if (glob(s.at("glob").get<std::string>(), name))
          out.push_back({id, name});
      // An optional structural pattern narrows the named bindings.
      if (s.contains("where"))
        std::erase_if(out, [&](const Target &t) { return !match(t.value, s.at("where")); });
      return out;
    }
    if (s.contains("driver")) {
      const auto port = s.at("driver").get<std::string>();
      const auto it = std::ranges::find_if(
          ir.drivers, [&](const Driver &d) { return ir.ports.at(d.port.index).name == port; });
      if (it == ir.drivers.end())
        throw FixtureError(3, "driven port not found: " + port);
      return {{it->value, {}}};
    }
    if (s.contains("input")) {
      for (std::size_t i = 0; i < ir.values.size(); ++i)
        if (ir.values[i].op == Op::Input &&
            ir.ports.at(ir.values[i].input_port.index).name == s.at("input").get<std::string>())
          return {{ValueId{i}, {}}};
      throw FixtureError(3, "input port not found: " + s.at("input").get<std::string>());
    }
    if (s.contains("state_next")) {
      auto base = resolve(s.at("state_next"));
      std::vector<Target> out;
      for (const auto &t : base) {
        const auto &v = ir.values.at(t.value.index);
        if (v.op != Op::State)
          throw FixtureError(3, "state_next of a non-state value");
        out.push_back({ir.states.at(v.state.index).next, t.binding});
      }
      return out;
    }
    if (s.contains("path")) {
      auto base = resolve(s.at("from"));
      std::vector<Target> out;
      for (auto t : base) {
        auto v = t.value;
        const auto &steps = s.at("path");
        for (std::size_t k = 0; k < steps.size(); ++k) {
          const auto &value = ir.values.at(v.index);
          const auto index = steps[k].get<unsigned>();
          if (index >= arity(value.op))
            throw FixtureError(3, "path step beyond operands of " + op_name(value.op));
          v = value.operands[index];
          if (s.contains("ops") && k < s.at("ops").size() &&
              ir.values.at(v.index).op != parse_op(s.at("ops")[k].get<std::string>()))
            throw FixtureError(3, "path step " + std::to_string(k) + " expected " +
                                      s.at("ops")[k].get<std::string>() + " found " +
                                      op_name(ir.values.at(v.index).op));
        }
        out.push_back({v, t.binding});
      }
      return out;
    }
    if (s.contains("match")) {
      std::vector<std::size_t> candidates;
      if (s.contains("within")) {
        std::set<std::size_t> seen;
        const auto roots = resolve(s.at("within"));
        std::vector<std::size_t> work(roots.size());
        std::ranges::transform(roots, work.begin(), [](const Target &t) { return t.value.index; });
        while (!work.empty()) {
          const auto v = work.back();
          work.pop_back();
          if (v >= limit() || !seen.insert(v).second)
            continue;
          const auto &value = ir.values[v];
          for (unsigned j = 0; j < arity(value.op); ++j)
            work.push_back(value.operands[j].index);
        }
        candidates.assign(seen.begin(), seen.end());
      } else
        for (std::size_t i = 0; i < limit(); ++i)
          candidates.push_back(i);
      std::vector<Target> out;
      for (auto i : candidates)
        if (match(ValueId{i}, s.at("match")))
          out.push_back({ValueId{i}, {}});
      return out;
    }
    throw FixtureError(2, "unknown selector " + s.dump());
  }

  bool match(ValueId id, const Json &p) const {
    if (p.is_string() && p.get<std::string>() == "*")
      return true;
    const auto &ir = b_.ir;
    const auto &v = ir.values.at(id.index);
    if (p.contains("binding")) {
      const auto it = c_.bindings.find(p.at("binding").get<std::string>());
      return it != c_.bindings.end() && it->second.index == id.index;
    }
    if (p.contains("const"))
      return v.op == Op::Constant && v.literal == p.at("const").get<std::uint64_t>();
    if (p.contains("input"))
      return v.op == Op::Input &&
             ir.ports.at(v.input_port.index).name == p.at("input").get<std::string>();
    if (p.contains("width") && v.width != p.at("width").get<unsigned>())
      return false;
    if (!p.contains("op"))
      return p.contains("width");
    if (v.op != parse_op(p.at("op").get<std::string>()))
      return false;
    if (!p.contains("args"))
      return true;
    const auto &args = p.at("args");
    if (args.size() != arity(v.op))
      return false;
    auto all = [&](bool swap) {
      for (unsigned j = 0; j < args.size(); ++j) {
        const unsigned k = swap && j < 2 ? 1 - j : j;
        if (!match(v.operands[k], args[j]))
          return false;
      }
      return true;
    };
    return all(false) || (commutative(v.op) && !p.value("ordered", false) && all(true));
  }

  // Builds the faulty value. self is the value being replaced; node is the
  // selected node (they differ only for set_operand). fit is the width a constant or a
  // selected value is fitted to; it is the width of self except for a Select condition.
  ValueId make(const Json &w, ValueId self, ValueId node, const std::string &binding,
               std::optional<unsigned> fit = std::nullopt) {
    auto &b = b_;
    const auto width = fit.value_or(b_.ir.values.at(self.index).width);
    if (w.contains("self"))
      return self;
    if (w.contains("const"))
      return b.constant(width, w.at("const").get<std::uint64_t>());
    if (w.contains("add")) {
      // The value plus a constant, modulo its width (an off-by-k).
      const auto mask = width >= 64 ? ~std::uint64_t{0} : (std::uint64_t{1} << width) - 1;
      return b.binary(Op::Subtract, self,
                      b.constant(width, (0 - w.at("add").get<std::uint64_t>()) & mask));
    }
    if (w.contains("operand"))
      return b_.ir.values.at(node.index).operands.at(w.at("operand").get<unsigned>());
    if (w.contains("binding_sub")) {
      auto name = binding;
      const auto from = w.at("binding_sub")[0].get<std::string>(),
                 to = w.at("binding_sub")[1].get<std::string>();
      const auto at = name.rfind(from);
      if (binding.empty() || at == std::string::npos)
        throw FixtureError(3, "binding_sub needs a named target containing " + from);
      name.replace(at, from.size(), to);
      const auto it = c_.bindings.find(name);
      if (it == c_.bindings.end())
        throw FixtureError(3, "binding not found: " + name);
      return it->second;
    }
    if (w.contains("select")) {
      const auto found = resolve(w.at("select"));
      if (found.size() != 1)
        throw FixtureError(3, "replacement selector found " + std::to_string(found.size()));
      // A replacement of another width is zero-extended or truncated to the driven width, so
      // a fault survives a narrower internal value (an address kept inside its arrival block).
      const auto value = found.front().value;
      const auto value_width = b_.ir.values.at(value.index).width;
      if (value_width < width)
        return b.unary(Op::ZeroExtend, value, width);
      return value_width > width ? b.slice(value, 0, width) : value;
    }
    if (w.contains("delay")) {
      // Added state: the value registered once on the component's clock,
      // reset to zero (a one-cycle-late copy).
      const auto inner = make(w.at("delay"), self, node, binding);
      if (b.ir.states.empty())
        throw FixtureError(3, "delay needs a clocked component");
      const auto clock = b.ir.states.front().clock;
      const auto reset = b.ir.states.front().reset;
      if (!reset)
        throw FixtureError(3, "delay needs a reset domain");
      const auto reg = b.state(b.ir.values.at(inner.index).width, clock, reset->signal);
      b.update(reg, inner);
      return reg;
    }
    if (w.contains("op")) {
      const auto op = parse_op(w.at("op").get<std::string>());
      // A Select condition is one bit wide whatever the width of the value it drives; the
      // other operands keep the width fitted to this operation.
      const auto &items = w.at("args");
      std::vector<ValueId> args(items.size());
      std::transform(items.begin(), items.end(), args.begin(), [&](const Json &a) {
        const auto condition = op == Op::Select && &a == &items.front();
        return make(a, self, node, binding, condition ? std::optional<unsigned>{1} : fit);
      });
      if (op == Op::BitNot)
        return b.unary(op, args.at(0), b_.ir.values.at(args.at(0).index).width);
      if (op == Op::Select)
        return b.select(args.at(0), args.at(1), args.at(2));
      if (args.size() != 2)
        throw FixtureError(2, "replacement operator needs two arguments");
      return b.binary(op, args[0], args[1]);
    }
    throw FixtureError(2, "unknown replacement " + w.dump());
  }

  // Applies one inline fault with selector index k; returns the number of hits.
  std::size_t apply(const Json &f, unsigned k) {
    auto &ir = b_.ir;
    const auto op = f.at("op").get<std::string>();
    std::vector<Target> targets;
    if (op == "set_driver") {
      targets = resolve(Json{{"driver", f.at("port")}});
    } else
      targets = resolve(f.at("at"));
    const auto expected = f.value("hits", std::size_t{1});
    if (targets.size() != expected)
      throw FixtureError(3, f.at("id").get<std::string>() + ": anchor hits " +
                                std::to_string(targets.size()) + " != " + std::to_string(expected));
    for (const auto &t : targets) {
      const auto mark = ir.values.size();
      auto chosen = [&](ValueId faulty, ValueId original) {
        auto &b = b_;
        const auto hit = b.binary(Op::Equal, sel_, b.constant(8, k));
        return b.select(hit, faulty, original);
      };
      if (op == "replace") {
        const auto faulty = make(f.at("with"), t.value, t.value, t.binding);
        const auto mux = chosen(faulty, t.value);
        // The replacement's own combinational cone keeps the original value
        // (a bypassed register still computes its next value from itself).
        std::set<std::size_t> cone;
        std::vector<std::size_t> work{faulty.index};
        while (!work.empty()) {
          const auto v = work.back();
          work.pop_back();
          if (!cone.insert(v).second)
            continue;
          const auto &value = ir.values[v];
          for (unsigned j = 0; j < arity(value.op); ++j)
            work.push_back(value.operands[j].index);
        }
        for (std::size_t i = 0; i < mark; ++i) {
          if (cone.contains(i))
            continue;
          auto &v = ir.values[i];
          for (unsigned j = 0; j < arity(v.op); ++j)
            if (v.operands[j].index == t.value.index && i != mux.index)
              v.operands[j] = mux;
        }
        for (auto &d : ir.drivers)
          if (d.value.index == t.value.index)
            d.value = mux;
        for (auto &s : ir.states)
          if (s.next.index == t.value.index && !cone.contains(s.output.index))
            s.next = mux;
      } else if (op == "remove_reset") {
        const auto &v = ir.values.at(t.value.index);
        if (v.op != Op::State)
          throw FixtureError(3, "remove_reset needs a state");
        ir.states.at(v.state.index).reset.reset();
      } else if (op == "set_next") {
        const auto &v = ir.values.at(t.value.index);
        if (v.op != Op::State)
          throw FixtureError(3, "set_next needs a state");
        const auto state = v.state.index;
        const auto original = ir.states.at(state).next;
        const auto faulty = make(f.at("with"), t.value, t.value, t.binding);
        ir.states.at(state).next = chosen(faulty, original);
      } else if (op == "set_driver") {
        for (auto &d : ir.drivers)
          if (ir.ports.at(d.port.index).name == f.at("port").get<std::string>()) {
            const auto original = d.value;
            const auto faulty = make(f.at("with"), original, original, {});
            const auto mux = chosen(faulty, original);
            for (auto &e : ir.drivers)
              if (ir.ports.at(e.port.index).name == f.at("port").get<std::string>())
                e.value = mux;
            break;
          }
      } else if (op == "set_operand") {
        const auto index = f.at("operand").get<unsigned>();
        if (index >= arity(ir.values.at(t.value.index).op))
          throw FixtureError(3, "set_operand beyond operands");
        const auto original = ir.values.at(t.value.index).operands[index];
        const auto faulty = make(f.at("with"), original, t.value, t.binding);
        const auto mux = chosen(faulty, original);
        ir.values.at(t.value.index).operands[index] = mux;
      } else
        throw FixtureError(2, "unknown fault operator " + op);
    }
    return targets.size();
  }

private:
  std::size_t limit() const { return b_.ir.values.size(); }
  Component &c_;
  Builder b_;
  ValueId sel_{};
};

Json ports_json(const std::vector<Component> &components) {
  Json out = Json::array();
  for (const auto &c : components) {
    Json ports = Json::array();
    std::ranges::transform(c.ir.ports, std::back_inserter(ports), [](const Port &p) {
      return Json{{"name", p.name},
                  {"width", p.width},
                  {"input", p.input},
                  {"endpoint", p.endpoint},
                  {"signal", p.signal}};
    });
    out.push_back({{"label", c.label}, {"module", c.ir.name}, {"ports", ports}});
  }
  return out;
}

// Probe groups with their nets resolved to emitted wire names and widths. A
// probe must name emitted logic: the observed netlist is the product's.
Json resolve_probes(const std::vector<Component> &components,
                    const std::set<std::string> &wrapped) {
  Json out = Json::object();
  for (const auto &c : components) {
    if (c.probes.empty())
      continue;
    const auto emitted = c.ir.emitted.value_or(c.ir.values.size());
    Json groups = Json::array();
    for (auto g : c.probes) {
      Json nets = Json::array();
      // A candidate head marker the network itself never reads is not emitted: the group then
      // goes without head markers.
      const auto unread = [&](const Json &net) {
        const auto it = c.bindings.find(net.at(1).get<std::string>());
        return net.at(0) == "first" && it != c.bindings.end() && it->second.index >= emitted;
      };
      const bool heads = std::ranges::none_of(g.at("nets"), unread);
      for (const auto &net : g.at("nets")) {
        if (net.at(0) == "first" && !heads)
          continue;
        const auto key = net.at(1).get<std::string>();
        const auto it = c.bindings.find(key);
        if (it == c.bindings.end())
          throw FixtureError(3, "probe binding not found: " + key);
        if (it->second.index >= emitted)
          throw FixtureError(3, "probe outside the emitted netlist: " + key);
        nets.push_back({net.at(0), "nl_internal_v" + std::to_string(it->second.index),
                        c.ir.values.at(it->second.index).width});
      }
      g["nets"] = std::move(nets);
      groups.push_back(std::move(g));
    }
    out[c.label] = {{"wrapped", wrapped.contains(c.label)}, {"groups", groups}};
  }
  return out;
}

void write(const std::filesystem::path &path, const std::string &text) {
  std::ofstream out(path, std::ios::binary);
  out << text;
  if (!out)
    throw FixtureError(1, "cannot write " + path.string());
}

int run(int argc, char **argv) {
  if (argc < 3)
    throw FixtureError(2, "usage: nlv_fixture <recipe> <out_dir> [--param k=v]... "
                          "[--faults spec.json] [--list-bindings]");
  const std::string recipe = argv[1];
  const std::filesystem::path out = argv[2];
  Params params;
  std::optional<std::filesystem::path> spec_path;
  bool list = false, probes = false;
  for (int i = 3; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--param" && i + 1 < argc) {
      const std::string kv = argv[++i];
      const auto eq = kv.find('=');
      if (eq == std::string::npos)
        throw FixtureError(2, "--param needs key=value");
      params[kv.substr(0, eq)] = kv.substr(eq + 1);
    } else if (arg == "--faults" && i + 1 < argc)
      spec_path = argv[++i];
    else if (arg == "--list-bindings")
      list = true;
    else if (arg == "--probes")
      probes = true;
    else
      throw FixtureError(2, "unknown argument " + arg);
  }
  auto components = build(recipe, params);
  std::filesystem::create_directories(out);
  std::string baseline;
  std::map<std::string, std::string> cells;
  for (const auto &c : components) {
    baseline += emit_verilog(c.ir);
    // The cell models, apart from the module so that fault copies leave them single.
    for (const auto &cell : cell_files(c.ir))
      cells[cell.path] = cell.text;
  }
  if (!cells.empty()) {
    std::string text;
    for (const auto &cell : cells)
      text += cell.second;
    write(out / "cells.v", text);
  }
  write(out / "network.v", baseline);
  write(out / "ports.json", ports_json(components).dump(1) + "\n");
  if (probes)
    write(out / "probes.json", resolve_probes(components, {}).dump(1) + "\n");
  if (list) {
    // Each binding with its operator and its users, to author structural
    // anchors: a user names its operator, operand position and any bindings
    // of its own; drivers and state updates are listed as port and next.
    Json bindings = Json::object();
    for (const auto &c : components) {
      std::map<std::size_t, std::vector<std::string>> named;
      for (const auto &[name, id] : c.bindings)
        named[id.index].push_back(name);
      std::map<std::size_t, Json> users;
      for (std::size_t i = 0; i < c.ir.values.size(); ++i) {
        const auto &v = c.ir.values[i];
        for (unsigned j = 0; j < arity(v.op); ++j) {
          Json user = {{"op", op_name(v.op)}, {"pos", j}};
          if (named.contains(i))
            user["names"] = named.at(i);
          users[v.operands[j].index].push_back(user);
        }
      }
      for (const auto &d : c.ir.drivers)
        users[d.value.index].push_back({{"port", c.ir.ports.at(d.port.index).name}});
      for (const auto &s : c.ir.states) {
        Json user = {{"next_of", named.contains(s.output.index) ? Json(named.at(s.output.index))
                                                                : Json(nullptr)}};
        users[s.next.index].push_back(user);
      }
      for (const auto &[name, id] : c.bindings) {
        const auto &v = c.ir.values.at(id.index);
        Json entry = {{"op", op_name(v.op)}, {"width", v.width}, {"index", id.index}};
        if (users.contains(id.index))
          entry["users"] = users.at(id.index);
        bindings[c.label][name] = entry;
      }
    }
    write(out / "bindings.json", bindings.dump(1) + "\n");
  }
  if (!spec_path)
    return 0;
  Json spec;
  {
    std::ifstream in(*spec_path);
    if (!in)
      throw FixtureError(2, "cannot read " + spec_path->string());
    spec = Json::parse(in);
  }
  // Fault k (1-based, in spec order) is active when nl_nlv_fault_sel == k.
  Json resolved = Json::array();
  std::map<std::string, std::vector<std::size_t>> inline_by_label;
  std::vector<std::size_t> variants;
  const auto &faults = spec.at("faults");
  if (faults.size() > 255)
    throw FixtureError(2, "at most 255 faults per build");
  std::set<std::string> ids;
  for (std::size_t i = 0; i < faults.size(); ++i) {
    const auto &f = faults[i];
    if (!ids.insert(f.at("id").get<std::string>()).second)
      throw FixtureError(2, "duplicate fault id " + f.at("id").get<std::string>());
    if (f.at("op") == "variant")
      variants.push_back(i);
    else
      inline_by_label[f.at("component").get<std::string>()].push_back(i);
  }
  std::string faulted;
  Json modules = Json::array();
  std::set<std::string> wrapped;
  for (auto &c : components) {
    const auto original = c.ir.name;
    const auto original_ports = c.ir.ports;
    std::vector<std::size_t> mine;
    if (inline_by_label.contains(c.label))
      mine = inline_by_label.at(c.label);
    std::vector<std::size_t> own_variants;
    std::ranges::copy_if(variants, std::back_inserter(own_variants), [&](std::size_t i) {
      return faults[i].at("component").get<std::string>() == c.label;
    });
    if (mine.empty() && own_variants.empty()) {
      faulted += emit_verilog(c.ir);
      continue;
    }
    Json entry = {{"label", c.label}, {"module", original}, {"core", original + "__nlv"}};
    wrapped.insert(c.label);
    if (!mine.empty()) {
      {
        Engine engine(c);
        for (auto i : mine) {
          const auto &f = faults[i];
          const auto hits = engine.apply(f, static_cast<unsigned>(i + 1));
          resolved.push_back({{"id", f.at("id")},
                              {"index", i + 1},
                              {"component", c.label},
                              {"kind", "inline"},
                              {"op", f.at("op")},
                              {"hits", hits}});
        }
      }
      renumber(c);
      entry["sel_port"] = "nl_nlv_fault_sel";
    }
    c.ir.name = original + "__nlv";
    if (auto status = verify_structure(c.ir); !status)
      throw FixtureError(4, "faulted " + c.label + " fails structure verification: " +
                                status.diagnostics().front().message);
    faulted += emit_verilog(c.ir);
    entry["variants"] = Json::array();
    for (auto i : own_variants) {
      const auto &f = faults[i];
      Params changed = params;
      for (const auto &[k, v] : f.at("params").items())
        changed[k] = v.get<std::string>();
      auto rebuilt = build(recipe, changed);
      auto it = std::ranges::find_if(rebuilt, [&](const auto &r) { return r.label == c.label; });
      if (it == rebuilt.end())
        throw FixtureError(3, "variant lost component " + c.label);
      if (it->ir.name != original)
        throw FixtureError(3, "variant renamed module " + it->ir.name);
      auto mismatch = it->ir.ports.size() != original_ports.size();
      for (std::size_t p = 0; !mismatch && p < it->ir.ports.size(); ++p)
        mismatch = it->ir.ports[p].name != original_ports[p].name ||
                   it->ir.ports[p].width != original_ports[p].width ||
                   it->ir.ports[p].input != original_ports[p].input;
      if (mismatch)
        throw FixtureError(3, f.at("id").get<std::string>() + ": variant changes the port list");
      const auto name = original + "__nlvf" + std::to_string(i + 1);
      it->ir.name = name;
      faulted += emit_verilog(it->ir);
      entry["variants"].push_back({{"index", i + 1}, {"module", name}});
      resolved.push_back({{"id", f.at("id")},
                          {"index", i + 1},
                          {"component", c.label},
                          {"kind", "variant"},
                          {"op", "variant"},
                          {"params", f.at("params")},
                          {"hits", 1}});
    }
    modules.push_back(entry);
  }
  std::sort(resolved.begin(), resolved.end(), [](const Json &a, const Json &b) {
    return a.at("index").get<std::size_t>() < b.at("index").get<std::size_t>();
  });
  write(out / "faulted.v", faulted);
  if (probes)
    write(out / "faulted_probes.json", resolve_probes(components, wrapped).dump(1) + "\n");
  write(out / "faults.json",
        Json{{"recipe", recipe}, {"faults", resolved}, {"modules", modules}}.dump(1) + "\n");
  return 0;
}
} // namespace

int main(int argc, char **argv) {
  try {
    return run(argc, argv);
  } catch (const FixtureError &e) {
    std::cerr << "nlv_fixture: " << e.what() << '\n';
    return e.code;
  } catch (const std::exception &e) {
    std::cerr << "nlv_fixture: " << e.what() << '\n';
    return 1;
  }
}
