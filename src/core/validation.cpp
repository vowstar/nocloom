#include "async_fifo.hpp"
#include "axi4_output.hpp"
#include "capability.hpp"
#include "diagnostic.hpp"
#include "message.hpp"
#include "nocloom/generation.hpp"
#include "switch_graph.hpp"
#include <algorithm>
#include <bit>
#include <set>
#include <tuple>

namespace nocloom::detail {
Diagnostic error(std::string code, std::string field, std::string message) {
  return {std::move(code), std::move(field), std::move(message), {}, {}};
}
std::vector<Diagnostic> locate(std::vector<Diagnostic> diagnostics, const SourceMap *sources) {
  for (auto &d : diagnostics)
    for (auto key = d.field; sources && !d.location && !key.empty();) {
      if (auto it = sources->find(key); it != sources->end())
        d.location = it->second;
      const auto pos = key.find_last_of(".[");
      key = pos == std::string::npos ? "" : key.substr(0, pos);
    }
  return diagnostics;
}

bool valid_name(std::string_view name) {
  const auto letter = [](char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); };
  if (name.empty() || name.size() > 128 || !letter(name.front()) || name.starts_with("nl_") ||
      name.starts_with("nocloom_"))
    return false;
  for (char c : name)
    if (!letter(c) && !(c >= '0' && c <= '9') && c != '_')
      return false;
  // The Verilog-2001 keywords.
  static const auto keywords = [] {
    std::set<std::string_view> set;
    constexpr std::string_view all =
        "always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos "
        "config deassign default defparam design disable edge else end endcase endconfig "
        "endfunction endgenerate endmodule endprimitive endspecify endtable endtask event "
        "for force forever fork function generate genvar highz0 highz1 if ifnone incdir "
        "include initial inout input instance integer join large liblist library "
        "localparam macromodule medium module nand negedge nmos nor noshowcancelled not "
        "notif0 notif1 or output parameter pmos posedge primitive pull0 pull1 pulldown "
        "pullup pulsestyle_onevent pulsestyle_ondetect rcmos real realtime reg release "
        "repeat rnmos rpmos rtran rtranif0 rtranif1 scalared showcancelled signed small "
        "specify specparam strong0 strong1 supply0 supply1 table task time tran tranif0 "
        "tranif1 tri tri0 tri1 triand trior trireg unsigned use vectored wait wand weak0 "
        "weak1 while wire wor xnor xor";
    for (std::size_t at = 0; at < all.size();) {
      const auto end = std::min(all.find(' ', at), all.size());
      set.insert(all.substr(at, end - at));
      at = end + 1;
    }
    return set;
  }();
  return !keywords.contains(name);
}
} // namespace nocloom::detail

namespace nocloom {
namespace {
using detail::apb_initiator;
using detail::connects;
using detail::exclusive_keys;
using detail::full_axi;
using detail::layout_mappings;
using detail::network_data_width;
using detail::source_data_width;
using detail::source_engine_width;
using detail::unconnected_flows;
// Schema 10 connectivity rules. Pair diagnostics name the source row and the position of the target
// in it, as the YAML map writes them.
template <typename Reject> void check_connectivity(const Definition &d, Reject &reject) {
  if (!d.connectivity) {
    reject("NCL_CONNECTIVITY_REQUIRED", "connectivity",
           "Every network needs a connectivity matrix: map each source to its targets, or "
           "write connectivity: all");
    return;
  }
  // A named default target is one the initiator reaches.
  for (std::size_t i = 0; i < d.initiators.size(); ++i)
    if (const auto &x = d.initiators[i];
        x.default_target &&
        (std::ranges::find(d.targets, *x.default_target, &Target::name) == d.targets.end() ||
         !connects(d, x.name, *x.default_target)))
      reject("NCL_CONNECTIVITY_REFERENCE", "initiators[" + std::to_string(i) + "].default_target",
             "Initiator " + x.name + " reaches no target " + *x.default_target);
  const auto &c = *d.connectivity;
  if (c.all) {
    if (!c.pairs.empty())
      reject("NCL_CONNECTIVITY_DUPLICATE", "connectivity",
             "connectivity all already connects every pair; it takes no pair list");
    return;
  }
  // The flow pairs each entry connects: one flow, or every flow of a side without one.
  std::set<std::tuple<std::string, std::string, unsigned, unsigned>> seen;
  std::set<std::string> used_sources, used_targets;
  std::map<std::string, unsigned> position;
  for (const auto &pair : c.pairs) {
    const auto row = "connectivity." + pair.source;
    const auto field = row + "[" + std::to_string(position[pair.source]++) + "]";
    const auto initiator = std::ranges::find(d.initiators, pair.source, &Initiator::name);
    const auto target = std::ranges::find(d.targets, pair.target, &Target::name);
    const bool source = initiator != d.initiators.end(), sink = target != d.targets.end();
    if (!source)
      reject("NCL_CONNECTIVITY_REFERENCE", row, "Unknown initiator " + pair.source);
    if (!sink)
      reject("NCL_CONNECTIVITY_REFERENCE", field, "Unknown target " + pair.target);
    const unsigned flows = source ? std::max<unsigned>(1, initiator->route_classes.size()) : 1,
                   target_paths = sink ? std::max(1U, target->flows) : 1;
    if (pair.source_path && *pair.source_path >= flows)
      reject("NCL_CONNECTIVITY_REFERENCE", field + ".source_path",
             pair.source + " has flows 0 to " + std::to_string(flows - 1));
    if (pair.target_path && *pair.target_path >= target_paths)
      reject("NCL_CONNECTIVITY_REFERENCE", field + ".target_path",
             pair.target + " has flows 0 to " + std::to_string(target_paths - 1));
    bool again = false;
    for (unsigned f = 0; f < flows; ++f)
      for (unsigned g = 0; g < target_paths; ++g)
        if (pair.source_path.value_or(f) == f && pair.target_path.value_or(g) == g)
          again = !seen.emplace(pair.source, pair.target, f, g).second || again;
    if (again)
      reject("NCL_CONNECTIVITY_DUPLICATE", field,
             "Target " + pair.target + " is listed twice for " + pair.source);
    if (source && sink) {
      used_sources.insert(pair.source);
      used_targets.insert(pair.target);
    }
  }
  for (const auto &initiator : d.initiators)
    if (!used_sources.contains(initiator.name))
      reject("NCL_CONNECTIVITY_UNUSED_SOURCE", "connectivity",
             "Initiator " + initiator.name + " reaches no target");
  for (const auto &t : d.targets)
    if (!used_targets.contains(t.name))
      reject("NCL_CONNECTIVITY_UNUSED_TARGET", "connectivity",
             "Target " + t.name + " is reached by no initiator");
}
// A connected flow pair needs a mapping below the initiator's address limit, a window's first block
// starting at its base, and every mapping a source that reaches it. Runs on a definition whose
// widths and mappings passed their own checks.
template <typename Reject> void check_connected_mappings(const Definition &d, Reject &reject) {
  const auto check = [&](const Initiator &i, const Target &t, std::optional<unsigned> flow,
                         const std::string &field) {
    const auto bits = i.address_width.value_or(d.address_width);
    for (unsigned g = 0; g < std::max(1U, t.flows); ++g)
      if (flow.value_or(g) == g && std::ranges::none_of(t.mappings, [&](const AddressMapping &m) {
            return m.flow == g && m.base >> bits == 0;
          })) {
        reject("NCL_CONNECTIVITY_REFERENCE", field,
               "Initiator " + i.name + " connects flow " + std::to_string(g) + " of target " +
                   t.name + ", which has no mapping below its address limit");
        return;
      }
  };
  // The widest address space connected to each target flow.
  std::map<std::pair<std::string, unsigned>, unsigned> widest;
  const auto connect = [&](const Initiator &i, const Target &t, std::optional<unsigned> flow) {
    for (unsigned g = 0; g < std::max(1U, t.flows); ++g)
      if (flow.value_or(g) == g) {
        auto &bits = widest[{t.name, g}];
        bits = std::max(bits, i.address_width.value_or(d.address_width));
      }
  };
  if (d.connectivity->all) {
    for (const auto &i : d.initiators)
      for (const auto &t : d.targets) {
        check(i, t, std::nullopt, "connectivity");
        connect(i, t, std::nullopt);
      }
  } else {
    std::map<std::string, unsigned> position;
    for (const auto &pair : d.connectivity->pairs) {
      const auto &i = *std::ranges::find(d.initiators, pair.source, &Initiator::name);
      const auto &t = *std::ranges::find(d.targets, pair.target, &Target::name);
      check(i, t, pair.target_path,
            "connectivity." + pair.source + "[" + std::to_string(position[pair.source]++) + "]");
      connect(i, t, pair.target_path);
    }
  }
  // Every aligned block of a mapping needs a connected source that addresses it.
  for (std::size_t t = 0; t < d.targets.size(); ++t)
    for (std::size_t m = 0; m < d.targets[t].mappings.size(); ++m) {
      const auto &mapping = d.targets[t].mappings[m];
      const auto at = widest.find({d.targets[t].name, mapping.flow});
      if (at == widest.end() ||
          detail::aligned_blocks(mapping.window()).back().base >> at->second != 0)
        reject("NCL_CONNECTIVITY_UNUSED_TARGET",
               "targets[" + std::to_string(t) + "].mappings[" + std::to_string(m) + "]",
               "No initiator connected to target " + d.targets[t].name +
                   " addresses all of this mapping");
    }
}
// An initiator's burst capacity (its own or the transport's) and the field that sets it.
std::pair<unsigned, std::string> capacity_at(const Definition &d, const Initiator &s,
                                             const std::string &field) {
  if (s.max_outstanding)
    return {*s.max_outstanding, field + ".max_outstanding"};
  return {d.transport.value_or(TransportOptions{}).max_outstanding, "transport.max_outstanding"};
}
// network_bits is the network data width.
template <typename Reject>
void check_apb_initiator(const Definition &d, unsigned network_bits, const Initiator &initiator,
                         const std::string &field, Reject &reject) {
  const auto unsupported = [&](bool set, const char *name, const char *why) {
    if (set)
      reject("NCL_APB_INITIATOR", field + "." + name, std::string("APB initiators ") + why);
  };
  const auto width = initiator.data_width.value_or(32);
  if (width != 8 && width != 16 && width != 32)
    reject("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
           "APB initiators support 8, 16 or 32 data bits");
  else if (network_bits != 32)
    reject("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
           "APB initiators need a 32-bit network; set transport.data_width to 32");
  if (initiator.address_width.value_or(d.address_width) > 32)
    reject("NCL_ENDPOINT_ADDRESS_WIDTH", field + ".address_width",
           "APB initiators have at most 32 address bits; set address_width");
  unsupported(initiator.id_width.has_value(), "id_width", "carry no ID");
  unsupported(initiator.len_width.has_value(), "len_width", "carry no burst length");
  unsupported(initiator.access.has_value(), "access",
              "are read-write: PWRITE selects the direction of each transfer");
  unsupported(initiator.narrow_bursts.has_value(), "narrow_bursts", "issue no bursts");
  // A capacity outside 1..64 has its own diagnostic.
  if (const auto [capacity, at] = capacity_at(d, initiator, field); capacity > 1 && capacity <= 64)
    reject("NCL_AXI4_CAPACITY_COMBINATION", at,
           "APB initiator " + initiator.name +
               " keeps one transfer in flight; set its max_outstanding to 1");
}
// An AHB initiator is an AHB-Lite subordinate face of a full AXI4 source of its width: no ID or
// length ports, read-write, one burst in flight.
template <typename Reject>
void check_ahb_initiator(const Definition &d, const Initiator &initiator, const std::string &field,
                         Reject &reject) {
  const auto unsupported = [&](bool set, const char *name, const char *why) {
    if (set)
      reject("NCL_AHB_INITIATOR", field + "." + name, std::string("AHB initiators ") + why);
  };
  unsupported(initiator.id_width.has_value(), "id_width", "carry no ID");
  unsupported(initiator.len_width.has_value(), "len_width",
              "carry no burst length port; HBURST gives at most 16 beats");
  unsupported(initiator.access.has_value(), "access",
              "are read-write: HWRITE selects the direction of each transfer");
  unsupported(initiator.narrow_bursts.has_value(), "narrow_bursts",
              "answer a narrow burst with ERROR; only SINGLE transfers may be narrow");
  if (const auto [capacity, at] = capacity_at(d, initiator, field); capacity > 1 && capacity <= 64)
    reject("NCL_AXI4_CAPACITY_COMBINATION", at,
           "AHB initiator " + initiator.name +
               " keeps one burst in flight; set its max_outstanding to 1");
}
// Schema 10 arbitration settings that need no graph planning: legal policies per scope, fixed
// priority only per output with a duplicate-free order, and one entry per output. Candidate sets
// (existence, count, the priority permutation and the FIFO size) are checked when the graph is
// planned.
template <typename Reject> void check_v10_arbitration(const Definition &d, Reject &reject) {
  using P = ArbitrationPolicy;
  const auto selectable = [](P p) {
    return p == P::RoundRobin || p == P::Fifo || p == P::RotatingPriority ||
           p == P::LevelRoundRobin;
  };
  if (d.arbitration == P::FixedPriority)
    reject("NCL_ARBITRATION_PRIORITY", "arbitration.policy",
           "fixed_priority needs the priority order of one output; set it per output under "
           "topology.<request|response>.nodes[].arbitration");
  else if (d.arbitration != P::None && d.arbitration != P::Auto && !selectable(d.arbitration))
    reject("NCL_UNSUPPORTED_ARBITRATION", "arbitration.policy",
           "Expected auto, round_robin, fifo, rotating_priority or level_round_robin");
  if (!d.topology)
    return;
  for (const auto &[graph, direction] :
       {std::pair{&d.topology->request, "request"}, std::pair{&d.topology->response, "response"}})
    for (std::size_t i = 0; i < graph->nodes.size(); ++i) {
      std::set<std::string> outputs;
      const auto &node = graph->nodes[i];
      for (std::size_t j = 0; j < node.arbitration.size(); ++j) {
        const auto &o = node.arbitration[j];
        const auto field = std::string("topology.") + direction + ".nodes[" + std::to_string(i) +
                           "].arbitration[" + std::to_string(j) + "]";
        if (!o.output.starts_with("node/") && !o.output.starts_with("endpoint/"))
          reject("NCL_ARBITRATION_OUTPUT", field + ".output",
                 "Expected node/<successor> or endpoint/<name>");
        else if (!outputs.insert(o.output).second)
          reject("NCL_ARBITRATION_OUTPUT", field + ".output",
                 "Each output takes at most one arbitration entry");
        if (o.policy != P::FixedPriority && !selectable(o.policy))
          reject(
              "NCL_UNSUPPORTED_ARBITRATION", field + ".policy",
              "Expected round_robin, fifo, rotating_priority, fixed_priority or level_round_robin");
        else if (o.policy == P::FixedPriority && o.priority.empty())
          reject("NCL_ARBITRATION_PRIORITY", field + ".priority",
                 "fixed_priority requires the priority list of every candidate");
        else if (o.policy != P::FixedPriority && !o.priority.empty())
          reject("NCL_ARBITRATION_PRIORITY", field + ".priority",
                 "Only fixed_priority takes a priority list");
        if (std::set<std::string>(o.priority.begin(), o.priority.end()).size() != o.priority.size())
          reject("NCL_ARBITRATION_PRIORITY", field + ".priority",
                 "Each candidate appears once in the priority list");
      }
    }
}
// Every endpoint states its protocol and data width; there is no global one.
template <typename Reject> void check_v10_shape(const Definition &d, Reject &reject) {
  check_v10_arbitration(d, reject);
  const auto endpoint = [&](const auto &list, const char *kind) {
    for (std::size_t i = 0; i < list.size(); ++i) {
      const auto field = std::string(kind) + "[" + std::to_string(i) + "]";
      if (!list[i].protocol)
        reject("NCL_SCHEMA_MISSING_FIELD", field + ".protocol",
               "Schema 10 endpoints state their protocol");
      if (!list[i].data_width)
        reject("NCL_SCHEMA_MISSING_FIELD", field + ".data_width",
               "Schema 10 endpoints state their data width");
      // Narrow APB faces attach to legal 32-bit internal engines.
      else if (const auto &caps = detail::protocol_caps(list[i].protocol.value_or(Protocol::Axi4));
               !detail::width_in(std::string_view(kind) == "targets" ? caps.egress_widths
                                                                     : caps.ingress_widths,
                                 *list[i].data_width))
        reject("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
               "Endpoint data width must be 32, 64, 128, 256, 512 or 1024");
    }
  };
  endpoint(d.initiators, "initiators");
  endpoint(d.targets, "targets");
  if (d.transport && d.transport->data_width && *d.transport->data_width != 32 &&
      *d.transport->data_width != 64)
    reject("NCL_UNSUPPORTED_WIDTH", "transport.data_width",
           "The network data width must be 32 or 64 bits");
}
// The planning faults of one direction's routes, named in configured node names (planning would
// name them in hardware names): an explicit route off the graph, a cycle of route waits (a packet
// holding one link waits for the next link of its route, over the routes of every flow) or a link
// back into its own switch. A cycle of links that no route chains is accepted.
template <typename Reject>
void check_routes(const Definition &d, const ExchangeGraph &graph, bool response,
                  const std::string &field, Reject &reject) {
  detail::DirectedSwitchGraph named;
  std::ranges::transform(graph.nodes, std::back_inserter(named.nodes), &ExchangeNode::name);
  for (const auto &link : graph.links)
    named.edges.push_back({link.from, link.to});
  for (const auto &a : graph.entries)
    named.entries.push_back({a.endpoint, a.node});
  for (const auto &a : graph.exits)
    named.exits.push_back({a.endpoint, a.node});
  named.response = response;
  for (const auto &i : d.initiators)
    if (i.route_classes.size() > 1)
      named.flows[i.name] = static_cast<unsigned>(i.route_classes.size());
  for (const auto &t : d.targets)
    if (t.flows > 1)
      named.target_paths[t.name] = t.flows;
  named.unconnected = unconnected_flows(d);
  auto &pairs = named.admitted_pairs.emplace();
  for (const auto &i : d.initiators)
    for (const auto &t : d.targets)
      if (connects(d, i.name, t.name))
        pairs.push_back(response ? detail::SwitchPair{t.name, i.name}
                                 : detail::SwitchPair{i.name, t.name});
  std::vector<std::size_t> rows;
  const auto &routes = d.topology->routes;
  for (std::size_t i = 0; i < routes.size(); ++i)
    if (const auto &path = response ? routes[i].response : routes[i].request; !path.empty()) {
      named.fixed_routes.push_back(
          response ? detail::SwitchRoute{routes[i].target, routes[i].initiator, path}
                   : detail::SwitchRoute{routes[i].initiator, routes[i].target, path});
      if (named.flows.contains(routes[i].initiator))
        named.fixed_routes.back().flows = {routes[i].source_path};
      if (named.target_paths.contains(routes[i].target))
        named.fixed_routes.back().target_path = routes[i].target_path;
      rows.push_back(i);
    }
  // Other planning faults are reported by planning itself.
  auto planned = detail::plan_switch_graph(named);
  if (planned)
    return;
  const auto &fault = planned.diagnostics().front();
  if (fault.code == "NCL_SWITCH_ROUTE_CYCLE")
    reject(fault.code, field + ".links", fault.message);
  else if (fault.field.starts_with("switches.routes["))
    reject(fault.code,
           "topology.routes[" + std::to_string(rows.at(std::stoul(fault.field.substr(16)))) +
               (response ? "].response" : "].request"),
           fault.message);
}
// Each explicit route names a flow pair of a connected pair once and gives a request or a response
// path.
template <typename Reject> void check_route_rows(const Definition &d, Reject &reject) {
  std::set<std::tuple<std::string, std::string, unsigned, unsigned>> seen;
  const auto &routes = d.topology->routes;
  for (std::size_t i = 0; i < routes.size(); ++i) {
    const auto &r = routes[i];
    const auto field = "topology.routes[" + std::to_string(i) + "]";
    auto fault = [&](std::string code, const std::string &at, std::string message) {
      reject(std::move(code), field + at, std::move(message));
    };
    const auto initiator = std::ranges::find(d.initiators, r.initiator, &Initiator::name);
    const bool source = initiator != d.initiators.end();
    const auto at = std::ranges::find(d.targets, r.target, &Target::name);
    const bool target = at != d.targets.end();
    if (!source)
      fault("NCL_ROUTE_REFERENCE", ".initiator", "Unknown initiator " + r.initiator);
    else if (const auto flows = std::max<std::size_t>(1, initiator->route_classes.size());
             r.source_path >= flows)
      fault("NCL_ROUTE_REFERENCE", ".source_path",
            r.initiator + " has flows 0 to " + std::to_string(flows - 1));
    if (!target)
      fault("NCL_ROUTE_REFERENCE", ".target", "Unknown target " + r.target);
    else if (const auto flows = std::max(1U, at->flows); r.target_path >= flows)
      fault("NCL_ROUTE_REFERENCE", ".target_path",
            r.target + " has flows 0 to " + std::to_string(flows - 1));
    if (source && target && !connects(d, r.initiator, r.source_path, r.target, r.target_path))
      fault("NCL_ROUTE_REFERENCE", "", r.initiator + " does not connect to " + r.target);
    if (!seen.emplace(r.initiator, r.target, r.source_path, r.target_path).second)
      fault("NCL_ROUTE_DUPLICATE", "", "Each flow pair of a connected pair takes one route entry");
    if (r.request.empty() && r.response.empty())
      fault("NCL_SCHEMA_MISSING_FIELD", "",
            "A route gives a request path, a response path or both");
  }
}
bool extended_pipe(const LinkPipeline &pipe) {
  return pipe.half_rate || !pipe.bypass_port.empty() || pipe.ungated;
}
template <typename Reject>
void check_link_pipe(const LinkPipeline &pipe, const std::string &field, Reject &reject) {
  if (pipe.half_rate && !(pipe.forward && pipe.backward))
    reject("NCL_AXI4_TOPOLOGY", field + ".half_rate", "half_rate requires forward and backward");
  if (!pipe.bypass_port.empty() && !pipe.forward)
    reject("NCL_AXI4_TOPOLOGY", field + ".bypass_port",
           "A static bypass requires a forward or half-rate pipe");
}
template <typename Reject>
void check_endpoint_pipe(const LinkPipeline &pipe, const std::string &field, Reject &reject) {
  if (extended_pipe(pipe))
    reject("NCL_ENDPOINT_PIPE", field,
           "half_rate, bypass_port and ungated apply only to link and attachment pipes");
}
// The switch graphs of an explicit topology and the crossing parameters of a clocked one, checked
// once the endpoints are valid: switch names and uses, switch domains, CDC value ranges.
template <typename Reject> void check_graphs(const Definition &d, Reject &reject) {
  const bool clocked = detail::clocked_graph(d);
  std::set<std::string> domains;
  std::ranges::transform(d.clock_domains, std::inserter(domains, domains.end()),
                         &ClockDomain::name);
  for (const bool response : {false, true}) {
    if (!d.topology)
      break;
    const auto *graph = response ? &d.topology->response : &d.topology->request;
    const auto field = std::string("topology.") + (response ? "response" : "request");
    std::set<std::string> nodes, faults;
    auto fault = [&](const std::string &message) {
      if (faults.insert(message).second)
        reject("NCL_AXI4_TOPOLOGY", field, message);
    };
    if (graph->nodes.empty() || graph->nodes.size() > 128)
      fault("Expected 1..128 switch nodes per direction");
    for (const auto &node : graph->nodes) {
      if (!detail::valid_name(node.name) || !nodes.insert(node.name).second)
        fault("Node names must be valid and unique within their direction");
      if (clocked && !node.clock_domain)
        fault("Every switch in a clocked graph requires clock_domain");
    }
    for (std::size_t i = 0; i < graph->nodes.size(); ++i)
      for (std::size_t j = 0; j < graph->nodes[i].arbitration.size(); ++j) {
        const auto &o = graph->nodes[i].arbitration[j];
        const auto at =
            field + ".nodes[" + std::to_string(i) + "].arbitration[" + std::to_string(j) + "]";
        auto absent = [&](const std::string &key) {
          return key.starts_with("node/") && !nodes.contains(key.substr(5));
        };
        if (absent(o.output))
          reject("NCL_ARBITRATION_OUTPUT", at + ".output",
                 "The output names no node of this graph");
        for (std::size_t k = 0; k < o.priority.size(); ++k)
          if (absent(o.priority[k]))
            reject("NCL_ARBITRATION_PRIORITY", at + ".priority[" + std::to_string(k) + "]",
                   "The candidate names no node of this graph");
      }
    for (std::size_t i = 0; i < graph->links.size(); ++i) {
      const auto &link = graph->links[i];
      const auto at = field + ".links[" + std::to_string(i) + "]";
      if (!nodes.contains(link.from) || !nodes.contains(link.to))
        fault("Link references an absent node");
      check_link_pipe(link.pipeline, at + ".pipeline", reject);
      check_link_pipe(link.sender_pipeline, at + ".sender_pipeline", reject);
    }
    for (const auto *attachments : {&graph->entries, &graph->exits})
      for (std::size_t i = 0; i < attachments->size(); ++i) {
        const auto &attachment = (*attachments)[i];
        if (!nodes.contains(attachment.node))
          fault("Attachment references an absent node");
        const auto key = attachments == &graph->entries ? ".entries[" : ".exits[";
        check_link_pipe(attachment.pipeline, field + key + std::to_string(i) + "].pipeline",
                        reject);
      }
    if (!faults.empty())
      continue;
    check_routes(d, *graph, response, field, reject);
    if (!clocked)
      continue;
    const auto identities = detail::switch_identities(d, *graph, response);
    for (const auto &node : graph->nodes)
      if (!domains.contains(*node.clock_domain))
        reject("NCL_EXCHANGE_CLOCK_ASSIGNMENT", "exchange.clocks",
               std::string("Unknown clock placement object or domain: ") +
                   (response ? "rsp/" : "req/") + identities.at(node.name));
  }
  if (!clocked)
    return;
  auto stages = [&](std::optional<unsigned> n, const std::string &at) {
    if (n && (*n < 2 || *n > 4))
      reject("NCL_CDC_STAGES_UNSUPPORTED", at, "Synchronizer stages must be 2 to 4");
  };
  for (std::size_t i = 0; i < d.clock_domains.size(); ++i)
    if (const auto period = d.clock_domains[i].period_ps;
        period &&
        (*period < detail::crossing_min_period_ps || *period > detail::crossing_max_period_ps))
      reject("NCL_CDC_PERIOD_RANGE", "clock_domains[" + std::to_string(i) + "].period_ps",
             "A clock period must be 100 to 1000000000 picoseconds");
  stages(d.synchronizer_stages, "synchronizer_stages");
  std::set<std::pair<std::string, std::string>> listed;
  for (std::size_t i = 0; i < d.clock_crossings.size(); ++i) {
    const auto &c = d.clock_crossings[i];
    const auto path = "clock_crossings[" + std::to_string(i) + "]";
    for (const auto &[name, field] :
         {std::pair{&c.from_domain, ".from_domain"}, std::pair{&c.to_domain, ".to_domain"}})
      if (!domains.contains(*name))
        reject("NCL_DOMAIN_REFERENCE", path + field, "Unknown clock domain");
    if (c.from_domain == c.to_domain)
      reject("NCL_CDC_CROSSING_SAME_DOMAIN", path, c.from_domain + " never crosses into itself");
    if (!listed.emplace(c.from_domain, c.to_domain).second)
      reject("NCL_CDC_CROSSING_DUPLICATE", path,
             "Each directed domain pair takes one crossing entry");
    if (const auto *a = std::get_if<AsyncClockCrossing>(&c.options)) {
      if (!a->depth && !a->synchronizer_stages && !a->reset_handshake)
        reject(
            "NCL_SCHEMA_MISSING_FIELD", path,
            "An asynchronous crossing sets depth, synchronizer_stages, reset_handshake or several");
      if (a->depth &&
          (*a->depth < detail::async_fifo_min_depth || *a->depth > detail::async_fifo_max_depth))
        reject("NCL_CDC_DEPTH_RANGE", path + ".depth", "A crossing depth must be 2 to 10 entries");
      stages(a->synchronizer_stages, path + ".synchronizer_stages");
    } else if (const auto *s = std::get_if<SyncClockCrossing>(&c.options)) {
      if (s->depth && (*s->depth < 2 || *s->depth > 4))
        reject("NCL_CDC_DEPTH_RANGE", path + ".depth",
               "A related Sync depth must be 2 to 4 entries");
      if (s->fast && s->depth.value_or(3) != 2)
        reject("NCL_CDC_LOW_LATENCY_CREDIT_DEPTH", path + ".fast", "FastCredit requires depth 2");
    }
    if (!std::holds_alternative<AsyncClockCrossing>(c.options)) {
      const auto from = std::ranges::find(d.clock_domains, c.from_domain, &ClockDomain::name);
      const auto to = std::ranges::find(d.clock_domains, c.to_domain, &ClockDomain::name);
      if (from == d.clock_domains.end() || to == d.clock_domains.end())
        continue;
      if (!from->clock_root || from->clock_root != to->clock_root)
        reject("NCL_CDC_RELATED_ROOT", path,
               "A related crossing requires both domains to name the same explicit clock root");
      if (from->reset_port != to->reset_port)
        reject("NCL_CDC_RELATED_RESET", path,
               "Related Sync and EdgeAligned crossings require one common reset port");
      if (from->clock_type != ClockType::Direct && from->clock_type != ClockType::InternalDivider)
        reject("NCL_CDC_RELATED_DYNAMIC", path + ".from_domain",
               "Related crossings support only direct and internal_divider domains");
      if (to->clock_type != ClockType::Direct && to->clock_type != ClockType::InternalDivider)
        reject("NCL_CDC_RELATED_DYNAMIC", path + ".to_domain",
               "Related crossings support only direct and internal_divider domains");
    }
  }
}
// The element checks of one definition. A check records each diagnostic in order; the network
// width, the source width default and the declared domains are set as the checks reach them.
struct Checks {
  explicit Checks(const Definition &definition) : d(definition) {}
  const Definition &d;
  std::vector<Diagnostic> errors;
  unsigned network_bits = 0, source_bits = 0;
  bool clocked = false;
  std::set<std::string> domains;
  void operator()(std::string code, std::string field, std::string message) {
    errors.push_back(detail::error(std::move(code), std::move(field), std::move(message)));
  }
  void name(const std::string &value, const std::string &field) {
    if (!detail::valid_name(value))
      (*this)("NCL_NAME_INVALID", field,
              "Expected a nonreserved identifier of 1 to 128 characters");
  }
};
// The derived network and source default widths (endpoint widths are checked before). The first
// violation alone is reported.
std::optional<Diagnostic> check_widths(const Checks &c) {
  for (auto [width, field] :
       {std::pair{c.source_bits, "initiators"}, std::pair{c.network_bits, "targets"}})
    if (width < 8 || !std::has_single_bit(width))
      return detail::error("NCL_DATA_WIDTH_DOMAIN", field,
                           "Data width must be a power of two of at least 8 bits");
  if (c.d.address_width == 0)
    return detail::error("NCL_ADDRESS_WIDTH", "address_width", "Address width must be positive");
  const auto source_bits = c.source_bits;
  const bool downsize = (source_bits == 64 || source_bits == 128 || source_bits == 256 ||
                         source_bits == 512 || source_bits == 1024) &&
                        c.network_bits == 32;
  if (source_bits != c.network_bits && !downsize)
    return detail::error(
        "NCL_WIDTH_CONVERSION", "targets",
        "Endpoint widths must match except schema 10 AXI64/128/256/512/1024 to AXI32");
  if (!downsize && source_bits != 32 && source_bits != 64)
    return detail::error("NCL_UNSUPPORTED_WIDTH", "initiators",
                         "Schema 10 supports equal 32/64 or AXI64/128/256/512/1024 to AXI32");
  return std::nullopt;
}
// Error logs name the window's target and data targets of this definition; the window serves at
// least one log, and is one window.
void check_error_logging(Checks &c) {
  const auto &d = c.d;
  const auto &logging = d.error_logging;
  const auto windows = std::ranges::count_if(d.targets, &Target::log_registers);
  if (windows > 1)
    c("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.registers", "Expected one window of registers");
  if (windows != 0 && logging.logs.empty())
    c("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.logs", "A window of registers serves a log");
  if (windows == 0 && (!logging.logs.empty() || !logging.audit_flags.empty() || logging.revision))
    c("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.registers", "Error logs need a window");
  if (logging.revision.value_or(0) > 0xFFFFFFU)
    c("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.revision", "Expected 0 to 16777215");
  const auto known_flag = [&](const std::string &flag) {
    const auto &user = d.transport.value_or(TransportOptions{}).user_flags;
    return std::ranges::find(logging.audit_flags, flag) != logging.audit_flags.end() ||
           std::ranges::find(user, flag) != user.end();
  };
  for (std::size_t i = 0; i < logging.logs.size(); ++i) {
    const auto &log = logging.logs[i];
    const auto at = "error_logging.logs[" + std::to_string(i) + "]";
    c.name(log.name, at + ".name");
    if (log.serial_word_bits != 8 && log.serial_word_bits != 16 && log.serial_word_bits != 32)
      c("NCL_UNSUPPORTED_TOPOLOGY", at + ".serial_word_bits", "Expected 8, 16 or 32 bits");
    if (log.watch.empty())
      c("NCL_UNSUPPORTED_TOPOLOGY", at + ".watch", "A log watches at least one target");
    for (const auto &name : log.watch) {
      const auto target = std::ranges::find(d.targets, name, &Target::name);
      if (target == d.targets.end() || target->log_registers)
        c("NCL_UNSUPPORTED_TOPOLOGY", at + ".watch", "Expected data targets of this definition");
    }
    if (log.clock_domain && std::ranges::find(d.clock_domains, *log.clock_domain,
                                              &ClockDomain::name) == d.clock_domains.end())
      c("NCL_UNSUPPORTED_TOPOLOGY", at + ".clock_domain", "No such clock domain");
    for (const auto &logger : log.loggers) {
      for (const auto &code : logger.skip)
        if (code != "target" && code != "decode")
          c("NCL_UNSUPPORTED_TOPOLOGY", at + ".loggers.skip", "Expected target or decode");
      for (const auto &[flag, value] : logger.match)
        if (!known_flag(flag))
          c("NCL_UNSUPPORTED_TOPOLOGY", at + ".loggers.match", "No such flag: " + flag);
    }
  }
}

// Full AXI4 burst options (capability gates; defaults applied when the block is absent).
void check_transport(Checks &c) {
  const auto &d = c.d;
  const auto options = d.transport.value_or(TransportOptions{});
  if (options.max_outstanding == 0)
    c("NCL_AXI4_CAPACITY", "transport.max_outstanding",
      "The shared outstanding capacity must be at least one");
  // Pair packet limits: connected pairs of an AXI4 source, each once, with powers of two (the
  // capability rules check each against the pair's range).
  std::set<std::pair<std::string, std::string>> limited;
  for (std::size_t i = 0; i < options.packet_limits.size(); ++i) {
    const auto &limit = options.packet_limits[i];
    const auto field = "transport.packet_limits[" + std::to_string(i) + "]";
    const auto source = std::ranges::find(d.initiators, limit.source, &Initiator::name);
    const bool target =
        std::ranges::any_of(d.targets, [&](const Target &t) { return t.name == limit.target; });
    auto power = [](std::uint64_t value) { return value && !(value & (value - 1)); };
    if (source == d.initiators.end() || !target)
      c("NCL_AXI4_PACKETS", field, "A packet limit names an unknown source or target");
    else if (!connects(d, limit.source, limit.target))
      c("NCL_AXI4_PACKETS", field,
        "A packet limit names a pair that the connectivity matrix does not carry");
    else if (detail::ahb_protocol(source->protocol))
      c("NCL_AXI4_PACKETS", field,
        "AHB initiators take the target packet limits; a pair limit needs an AXI4 initiator");
    else if (!full_axi(source->protocol))
      c("NCL_AXI4_PACKETS", field,
        "Only AXI4 and AXI3 initiators issue bursts; AXI4-Lite and APB initiators have no "
        "packets to limit");
    else if (!limited.emplace(limit.source, limit.target).second)
      c("NCL_AXI4_PACKETS", field, "A pair takes at most one packet limit");
    else if (!limit.max_bytes && !limit.boundary_bytes)
      c("NCL_AXI4_PACKETS", field, "A packet limit sets max_bytes, boundary_bytes or both");
    else if ((limit.max_bytes && !power(*limit.max_bytes)) ||
             (limit.boundary_bytes && !power(*limit.boundary_bytes)))
      c("NCL_AXI4_PACKETS", field, "Packet limits are powers of two");
  }
  if (options.max_outstanding > 64)
    c("NCL_AXI4_CAPACITY", "transport.max_outstanding",
      "The shared outstanding capacity must be 1 to 64");
  if (options.transport_word_bytes && *options.transport_word_bytes != 4 &&
      *options.transport_word_bytes != 8 && *options.transport_word_bytes != 16 &&
      *options.transport_word_bytes != 32)
    c("NCL_AXI4_TRANSPORT_WORD", "transport.transport_word_bytes",
      "The transport word must be a serialization ladder member (4, 8, 16 or 32 bytes)");
  if (const auto granule = options.exclusive_granule;
      granule && (!std::has_single_bit(*granule) || *granule > 128))
    c("NCL_TARGET_EXCLUSIVE", "transport.exclusive_granule",
      "exclusive_granule must be a power of two from 1 to 128 bytes");
  else if (granule && std::ranges::none_of(d.targets, [](const Target &t) {
             return t.exclusive == ExclusiveAccess::Network;
           }))
    c("NCL_TARGET_EXCLUSIVE", "transport.exclusive_granule",
      "exclusive_granule applies only with an exclusive: network target");
}
// Network-wide settings: crossing parameters without a clocked graph, widths, transport options,
// the address width and the domain and endpoint counts.
std::optional<Diagnostic> check_network(Checks &c) {
  const auto &d = c.d;
  // Crossing parameters need a clocked graph; check_graphs checks their values.
  if (!c.clocked) {
    if (!d.clock_roots.empty())
      c("NCL_CLOCK_ROOT", "clock_roots", "Clock roots require an explicitly clocked graph");
    if (d.synchronizer_stages)
      c("NCL_SCHEMA_FIELD_VERSION", "synchronizer_stages",
        "Synchronizer stages require an explicitly clocked graph");
    for (std::size_t i = 0; i < d.clock_domains.size(); ++i)
      if (d.clock_domains[i].period_ps)
        c("NCL_CDC_PERIOD_UNUSED", "clock_domains[" + std::to_string(i) + "].period_ps",
          "A clock period requires a crossing of an explicitly clocked graph");
    for (std::size_t i = 0; i < d.clock_crossings.size(); ++i)
      c("NCL_CDC_CROSSING_UNUSED", "clock_crossings[" + std::to_string(i) + "]",
        "A crossing requires an explicitly clocked graph");
  }
  if (auto fatal = check_widths(c))
    return fatal;
  check_transport(c);
  if (d.address_width < 1 || d.address_width > 48)
    c("NCL_ADDRESS_WIDTH", "address_width", "Address width must be 2 to 48");
  if (d.address_width < 2)
    c("NCL_ADDRESS_WIDTH", "address_width",
      "Full AXI4 endpoints require at least two address bits");
  if (c.clocked ? (d.clock_domains.empty() || d.clock_domains.size() > 64)
                : d.clock_domains.size() != 1U)
    c("NCL_UNSUPPORTED_TOPOLOGY", "clock_domains",
      c.clocked ? "Clocked networks require 1 to 64 clock domains"
                : "Without an explicitly clocked topology the network is single-domain; "
                  "several domains need a topology whose switches bind clock_domain");
  if (d.initiators.empty() || d.initiators.size() > detail::max_endpoints)
    c("NCL_UNSUPPORTED_TOPOLOGY", "initiators", "Schema 10 needs 1 to 256 initiators");
  if (d.targets.empty())
    c("NCL_UNSUPPORTED_TOPOLOGY", "targets", "At least one target is required");
  if (d.targets.empty() || d.targets.size() > detail::max_endpoints)
    c("NCL_UNSUPPORTED_TOPOLOGY", "targets", "Schema 10 needs 1 to 256 targets");
  c.name(d.name, "name");
  check_error_logging(c);
  return std::nullopt;
}
// Clock, reset, control and static bypass ports are top-level inputs named as written. A reset
// port serves one or more domains; one released on a clock edge serves only the domain of that
// clock. A test port serves the clock gates of the domains that name it; with gating on, every
// gated domain names one.
void check_ports(Checks &c) {
  const auto &d = c.d;
  std::set<std::string> port_names;
  auto check_port = [&](const std::string &name, const std::string &field) {
    c.name(name, field);
    if (!port_names.insert(name).second)
      c("NCL_NAME_COLLISION", field, "Duplicate clock or reset port name");
  };
  auto check_named = [&](const auto &ports, const char *stem, const char *field, auto member) {
    for (std::size_t i = 0; i < ports.size(); ++i)
      check_port(ports[i].*member, std::string(stem) + "[" + std::to_string(i) + "]." + field);
  };
  if (d.clock_roots.empty())
    check_named(d.clock_domains, "clock_domains", "clock_port", &ClockDomain::clock_port);
  else
    check_named(d.clock_roots, "clock_roots", "clock_port", &ClockRoot::clock_port);
  for (std::size_t i = 0; i < d.reset_ports.size(); ++i) {
    const auto &port = d.reset_ports[i];
    const auto path = "reset_ports[" + std::to_string(i) + "]";
    const bool edge = port.release == ResetRelease::OnClockEdge;
    check_port(port.name, path + ".name");
    if (edge != port.release_domain.has_value())
      c("NCL_RESET_PORT", path + ".release_domain",
        edge ? "on_clock_edge names its release_domain"
             : "release_domain applies only to on_clock_edge");
    bool used = false;
    for (const auto &domain : d.clock_domains) {
      if (domain.reset_port == port.name) {
        used = true;
        if (edge && port.release_domain && *port.release_domain != domain.name)
          c("NCL_RESET_PORT", path + ".release_domain",
            "Released on a clock edge of " + *port.release_domain + ", it cannot reset domain " +
                domain.name);
      }
    }
    if (!used)
      c("NCL_RESET_PORT", path, "Reset port " + port.name + " resets no clock domain");
  }
  check_named(d.test_ports, "test_ports", "name", &TestPort::name);
  check_named(d.enable_ports, "enable_ports", "name", &EnablePort::name);
  check_named(d.ungate_ports, "ungate_ports", "name", &UngatePort::name);
  check_named(d.address_inputs, "address_inputs", "name", &AddressInput::name);
  std::set<std::string> bypasses;
  auto bypass = [&](const LinkPipeline &pipe, const std::string &field) {
    if (!pipe.bypass_port.empty() && bypasses.insert(pipe.bypass_port).second)
      check_port(pipe.bypass_port, field);
  };
  if (d.topology)
    for (std::size_t g = 0; g < 2; ++g) {
      const auto &graph = g ? d.topology->response : d.topology->request;
      const auto stem = std::string("topology.") + (g ? "response" : "request");
      for (std::size_t i = 0; i < graph.links.size(); ++i) {
        const auto at = stem + ".links[" + std::to_string(i) + "]";
        bypass(graph.links[i].pipeline, at + ".pipeline.bypass_port");
        bypass(graph.links[i].sender_pipeline, at + ".sender_pipeline.bypass_port");
      }
      for (const auto *attachments : {&graph.entries, &graph.exits})
        for (std::size_t i = 0; i < attachments->size(); ++i)
          bypass((*attachments)[i].pipeline,
                 stem + (attachments == &graph.entries ? ".entries[" : ".exits[") +
                     std::to_string(i) + "].pipeline.bypass_port");
    }
}
// Address inputs, selectors and mapping conditions (docs/address-map.md#address-selectors).
// Overlapping mappings take one condition or conditions that exclude each other: in every mode the
// mappings that decode follow the overlap rules of one plane. Whether two mode conditions exclude
// each other: a selector takes both values, or the selectors that must hold need two values of one
// address input.
bool exclusive(const Definition &d, const std::map<std::string, bool> &a,
               const std::map<std::string, bool> &b) {
  std::map<std::string, std::uint32_t> ports;
  for (const auto &[name, value] : a)
    if (b.contains(name) && b.at(name) != value)
      return true;
  for (const auto *when : {&a, &b})
    for (const auto &[name, value] : *when)
      if (const auto f = std::ranges::find(d.address_selectors, name, &AddressSelector::name);
          value && f != d.address_selectors.end())
        for (const auto &[port, level] : f->equals)
          if (const auto [at, fresh] = ports.emplace(port, level); !fresh && at->second != level)
            return true;
  return false;
}
void check_modes(Checks &c) {
  const auto &d = c.d;
  std::map<std::string, const AddressInput *> ports;
  for (std::size_t i = 0; i < d.address_inputs.size(); ++i) {
    const auto &port = d.address_inputs[i];
    const auto path = "address_inputs[" + std::to_string(i) + "]";
    ports[port.name] = &port;
    if (port.width < 1 || port.width > 32)
      c("NCL_ADDRESS_INPUT", path + ".width", "An address input is 1 to 32 bits wide");
    if (!c.domains.contains(port.clock_domain))
      c("NCL_DOMAIN_REFERENCE", path + ".clock_domain", "Unknown clock domain");
    if (std::ranges::none_of(d.address_selectors,
                             [&](const auto &f) { return f.equals.contains(port.name); }))
      c("NCL_ADDRESS_INPUT", path, "Address input " + port.name + " is compared by no selector");
  }
  if (d.address_selectors.size() > 16)
    c("NCL_ADDRESS_SELECTOR", "address_selectors", "A network has at most 16 address selectors");
  std::map<std::string, const AddressSelector *> flags;
  for (std::size_t i = 0; i < d.address_selectors.size(); ++i) {
    const auto &flag = d.address_selectors[i];
    const auto path = "address_selectors[" + std::to_string(i) + "]";
    c.name(flag.name, path + ".name");
    if (!flags.emplace(flag.name, &flag).second)
      c("NCL_NAME_COLLISION", path + ".name", "Duplicate address selector name");
    if (flag.equals.empty())
      c("NCL_ADDRESS_SELECTOR", path + ".equals", "A selector compares at least one address input");
    for (const auto &[port, value] : flag.equals)
      if (!ports.contains(port) || (ports.at(port)->width < 32 && value >> ports.at(port)->width))
        c("NCL_ADDRESS_SELECTOR", path + ".equals." + port,
          "Expected an address input and a value that fits its width");
    if (std::ranges::none_of(d.targets, [&](const Target &t) {
          return std::ranges::any_of(t.mappings,
                                     [&](const auto &m) { return m.when.contains(flag.name); });
        }))
      c("NCL_ADDRESS_SELECTOR", path, "Address selector " + flag.name + " is named by no mapping");
  }
  std::vector<std::tuple<const AddressMapping *, std::string, const Target *>> all;
  for (const auto &t : d.targets)
    for (std::size_t k = 0; k < t.mappings.size(); ++k)
      all.emplace_back(&t.mappings[k],
                       "targets[" + std::to_string(&t - d.targets.data()) + "].mappings[" +
                           std::to_string(k) + "]",
                       &t);
  for (std::size_t i = 0; i < all.size(); ++i) {
    const auto &[m, field, t] = all[i];
    if (!m->when.empty() && (m->base % 4096 || m->size % 4096 || m->local_address % 4096))
      c("NCL_MODE_CONDITION", field + ".when", "A mode-dependent mapping is whole 4 KiB pages");
    if (exclusive(d, m->when, m->when))
      c("NCL_MODE_CONDITION", field + ".when", "The condition never holds");
    // Every initiator reaching it samples the address inputs in its own domain.
    for (const auto &[name, value] : m->when)
      if (!flags.contains(name))
        c("NCL_MODE_CONDITION", field + ".when." + name, "Unknown address selector " + name);
      else
        for (const auto &[port, _] : flags.at(name)->equals)
          for (const auto &s : d.initiators)
            if (ports.contains(port) && ports.at(port)->clock_domain != s.clock_domain &&
                connects(d, s.name, t->name))
              c("NCL_ADDRESS_INPUT", field + ".when." + name,
                "Initiator " + s.name + " samples address input " + port + " outside its domain");
    for (std::size_t j = 0; j < i; ++j)
      if (const auto *o = std::get<0>(all[j]); m->when != o->when && m->base < o->base + o->size &&
                                               o->base < m->base + m->size &&
                                               !exclusive(d, m->when, o->when))
        c("NCL_ADDRESS_OVERLAP", field,
          "Overlapping mappings take one mode condition, or conditions that exclude each other");
  }
  if (const auto bits = detail::plane_bits(d); bits && d.address_width + bits > 48)
    c("NCL_MODE_CONDITION", "address_width",
      "address_width and the " + std::to_string(bits) + " bits of the mode conditions exceed 48");
}
// Bound library cells: identifier module and pin names, each pin in one role or tie, and the
// synchronizer stages a flip-flop (1) or an integrated synchronizer (2..4) gives.
void check_cells(Checks &c) {
  const auto &cells = c.d.cells;
  auto check = [&](const std::string &path, const std::string &module,
                   std::vector<std::pair<const char *, std::string>> pins,
                   const std::map<std::string, bool> &tie) {
    c.name(module, path + ".module");
    for (const auto &[pin, value] : tie)
      pins.emplace_back("tie", pin);
    std::set<std::string> used;
    for (const auto &[role, pin] : pins) {
      const auto field = path + "." + role + (std::string_view(role) == "tie" ? "." + pin : "");
      c.name(pin, field);
      if (!used.insert(pin).second)
        c("NCL_NAME_COLLISION", field, "Pin " + pin + " has another role or tie");
    }
  };
  if (const auto &g = cells.clock_gate) {
    std::vector<std::pair<const char *, std::string>> pins{
        {"clock", g->clock}, {"enable", g->enable}, {"output", g->output}};
    if (g->test)
      pins.emplace_back("test", *g->test);
    check("cells.clock_gate", g->module, pins, g->tie);
  }
  if (const auto &s = cells.synchronizer) {
    check("cells.synchronizer", s->module,
          {{"clock", s->clock}, {"data", s->data}, {"reset", s->reset}, {"output", s->output}},
          s->tie);
    if (s->stages < 1 || s->stages > 4)
      c("NCL_CDC_STAGES_UNSUPPORTED", "cells.synchronizer.stages",
        "A synchronizer cell has 1 (a flip-flop) to 4 stages");
    if (cells.clock_gate && cells.clock_gate->module == s->module)
      c("NCL_NAME_COLLISION", "cells.synchronizer.module", "One module binds both cells");
  }
}
// Clock domains, their ports and the endpoint domains of a single-domain network.
void check_domains(Checks &c) {
  const auto &d = c.d;
  std::map<std::string, const ClockRoot *> roots;
  for (std::size_t i = 0; i < d.clock_roots.size(); ++i) {
    const auto &root = d.clock_roots[i];
    const auto path = "clock_roots[" + std::to_string(i) + "]";
    c.name(root.name, path + ".name");
    c.name(root.clock_port, path + ".clock_port");
    if (!roots.emplace(root.name, &root).second)
      c("NCL_NAME_COLLISION", path + ".name", "Duplicate clock root");
    if (root.period_ps && (*root.period_ps < detail::crossing_min_period_ps ||
                           *root.period_ps > detail::crossing_max_period_ps))
      c("NCL_CDC_PERIOD_RANGE", path + ".period_ps",
        "A clock period must be 100 to 1000000000 picoseconds");
  }
  for (std::size_t i = 0; i < d.clock_domains.size(); ++i) {
    const auto &domain = d.clock_domains[i];
    const auto path = "clock_domains[" + std::to_string(i) + "]";
    c.name(domain.name, path + ".name");
    if (!c.domains.insert(domain.name).second)
      c("NCL_NAME_COLLISION", path + ".name", "Duplicate clock domain");
    if (std::ranges::none_of(d.reset_ports,
                             [&](const auto &r) { return r.name == domain.reset_port; }))
      c("NCL_DOMAIN_REFERENCE", path + ".reset_port", "Unknown reset port");
    if (d.clock_roots.empty()) {
      if (domain.clock_root || domain.edge_parent)
        c("NCL_CLOCK_ROOT", path, "clock_root and edge_parent require clock_roots");
    } else {
      if (!domain.clock_root || !roots.contains(*domain.clock_root))
        c("NCL_CLOCK_ROOT", path + ".clock_root", "Unknown or missing clock root");
      else if (roots.at(*domain.clock_root)->clock_port != domain.clock_port)
        c("NCL_CLOCK_ROOT", path + ".clock_port",
          "A domain clock port must equal its declared root clock port");
    }
    const bool stat = domain.clock_type == ClockType::InternalDivider;
    const bool dynamic = domain.clock_type == ClockType::EdgeQualified;
    if (domain.ratio && (!stat || *domain.ratio < 1 || *domain.ratio > 64))
      c("NCL_CLOCK_DIVISION", path + ".ratio",
        stat ? "A division ratio must be 1 to 64"
             : "A division ratio applies only to clock_type: internal_divider");
    if (stat && !domain.ratio)
      c("NCL_CLOCK_DIVISION", path + ".ratio", "An internal_divider domain requires ratio");
    if (domain.enable_port.has_value() != dynamic)
      c("NCL_CLOCK_DIVISION", path + ".enable_port",
        dynamic ? "An edge_qualified domain needs its valid-edge enable port"
                : "An enable port applies only to clock_type: edge_qualified");
    const auto known_port = [&](const std::optional<std::string> &port, const auto &ports,
                                const char *field, const char *message) {
      if (port && std::ranges::none_of(ports, [&](const auto &p) { return p.name == *port; }))
        c("NCL_DOMAIN_REFERENCE", path + "." + field, message);
    };
    known_port(domain.enable_port, d.enable_ports, "enable_port", "Unknown enable port");
    known_port(domain.ungate_port, d.ungate_ports, "ungate_port", "Unknown ungate port");
    if (!d.clock_roots.empty()) {
      const bool divided = stat || dynamic;
      if (divided != domain.edge_parent.has_value())
        c("NCL_CLOCK_EDGE", path + ".edge_parent",
          divided ? "An internal_divider or edge_qualified domain requires an edge_parent"
                  : "edge_parent applies only to internal_divider or edge_qualified domains");
      if (domain.edge_parent) {
        const auto parent =
            std::ranges::find(d.clock_domains, *domain.edge_parent, &ClockDomain::name);
        if (parent == d.clock_domains.end() || parent->name == domain.name)
          c("NCL_CLOCK_EDGE", path + ".edge_parent", "Unknown or self-referential edge parent");
        else if (parent->clock_root != domain.clock_root)
          c("NCL_CLOCK_EDGE", path + ".edge_parent", "An edge parent must belong to the same root");
      }
    }
  }
  for (std::size_t i = 0; i < d.clock_domains.size(); ++i) {
    std::set<std::string> seen;
    auto at = std::optional<std::size_t>{i};
    while (at && d.clock_domains[*at].edge_parent) {
      if (!seen.insert(d.clock_domains[*at].name).second) {
        c("NCL_CLOCK_EDGE", "clock_domains[" + std::to_string(i) + "].edge_parent",
          "Clock edge parents contain a cycle");
        break;
      }
      const auto parent =
          std::ranges::find(d.clock_domains, *d.clock_domains[*at].edge_parent, &ClockDomain::name);
      at = parent == d.clock_domains.end()
               ? std::nullopt
               : std::optional{static_cast<std::size_t>(parent - d.clock_domains.begin())};
    }
  }
  if (std::ranges::any_of(d.clock_domains,
                          [](const auto &x) { return x.ungate_port.has_value(); }) &&
      std::ranges::any_of(d.clock_domains,
                          [](const auto &x) { return !x.ungate_port.has_value(); }))
    c("NCL_CLOCK_DIVISION", "clock_domains",
      "A domain naming an ungate port requires every domain to name one");
  for (const auto &port : d.enable_ports)
    if (std::ranges::none_of(d.clock_domains,
                             [&](const auto &x) { return x.enable_port == port.name; }))
      c("NCL_CLOCK_DIVISION", "enable_ports",
        "Enable port " + port.name + " enables no clock domain");
  check_ports(c);
  check_cells(c);
  const bool gating = d.clock_gating != ClockGating::None;
  for (std::size_t i = 0; i < d.clock_domains.size(); ++i)
    if (const auto &port = d.clock_domains[i].test_port;
        port ? std::ranges::find(d.test_ports, *port, &TestPort::name) == d.test_ports.end()
             : gating && d.clock_domains[i].gated)
      c("NCL_DOMAIN_REFERENCE", "clock_domains[" + std::to_string(i) + "].test_port",
        port ? "Unknown test port" : "A gated domain needs a test port, or gated: false");
  const auto &source_domain = d.initiators.front().clock_domain;
  const auto &target_domain = d.targets.front().clock_domain;
  if (!c.clocked && (!c.domains.contains(source_domain) || !c.domains.contains(target_domain) ||
                     source_domain != target_domain))
    c("NCL_DOMAIN_REFERENCE", "clock_domains",
      "Endpoint domains do not match the selected topology");
}
void check_ahb5(Checks &c, std::optional<Protocol> protocol,
                const std::optional<Ahb5Properties> &properties, const std::string &field) {
  if (!properties)
    return;
  const auto &p = *properties;
  if (protocol != Protocol::Ahb5)
    c("NCL_AHB5_PROPERTIES", field + ".ahb5", "The ahb5 object requires protocol: ahb5");
  else if ((p.hprot_width != 4 && p.hprot_width != 7) || p.hmaster_width > 32 || p.exclusive ||
           p.hauser_width || p.hwuser_width || p.hruser_width || p.hbuser_width)
    c("NCL_AHB5_PROPERTIES", field + ".ahb5",
      "AHB5 requires hprot_width 4 or 7 and hmaster_width 0..32, without exclusive or user "
      "signals");
}
// The protocol, data width and address width of an initiator.
void check_initiator_face(Checks &c, const Initiator &initiator, const std::string &field) {
  const auto &d = c.d;
  check_ahb5(c, initiator.protocol, initiator.ahb5, field);
  if (initiator.protocol) {
    if (*initiator.protocol == Protocol::Apb2)
      c("NCL_UNSUPPORTED_PROTOCOL", field + ".protocol",
        "APB2 initiators are not supported: an APB2 manager expects every transfer to complete in "
        "its first access cycle, which a network does not provide; use apb3 or apb4");
    else if (*initiator.protocol == Protocol::Apb5 || *initiator.protocol == Protocol::Apb4 ||
             *initiator.protocol == Protocol::Apb3)
      check_apb_initiator(d, c.network_bits, initiator, field, c);
    else if (detail::ahb_protocol(initiator.protocol))
      check_ahb_initiator(d, initiator, field, c);
    else if (initiator.data_width.value_or(c.source_bits) != c.network_bits &&
             !full_axi(initiator.protocol) &&
             !(*initiator.protocol == Protocol::Axi4Lite &&
               initiator.data_width.value_or(c.source_bits) == 64))
      c("NCL_UNSUPPORTED_PROTOCOL", field + ".protocol", "Wide ingress requires full AXI4");
    else if (!detail::protocol_caps(*initiator.protocol).ingress)
      c("NCL_UNSUPPORTED_PROTOCOL", field + ".protocol",
        "Schema 10 initiators support axi4, axi3, axi4_lite, apb5, apb4, apb3, ahb_v3 or ahb5");
  }
  // check_apb_initiator owns the width rules of an APB initiator.
  if (initiator.data_width && !apb_initiator(initiator)) {
    const auto bits = *initiator.data_width;
    if (bits != c.network_bits &&
        !(c.network_bits == 32 &&
          (bits == 64 || bits == 128 || bits == 256 || bits == 512 || bits == 1024)))
      c("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
        "Source width must match its target or use the admitted wide-to32 profile");
  }
  const unsigned source_address_min = apb_initiator(initiator)                   ? 1U
                                      : initiator.protocol == Protocol::Axi4Lite ? 2U
                                                                                 : 12U;
  if (!initiator.address_width && d.address_width < source_address_min)
    c("NCL_ENDPOINT_ADDRESS_WIDTH", field + ".address_width",
      "The source address width is below its protocol minimum; widen the global address or "
      "set address_width");
  if (initiator.address_width &&
      (*initiator.address_width < source_address_min || *initiator.address_width > d.address_width))
    c("NCL_ENDPOINT_ADDRESS_WIDTH", field + ".address_width",
      "Source addresses require at least 1 bit for APB, 2 for AXI4-Lite or 12 for AXI4 and AHB, "
      "and no more than the global width");
}
// The transaction ports, narrow bursts, capacity and ID table of an initiator. Returns whether
// it is a full AXI4 or AXI3 initiator with more than one outstanding burst.
bool check_initiator_bursts(Checks &c, const Initiator &initiator, const std::string &field) {
  const auto options = c.d.transport.value_or(TransportOptions{});
  const bool lite_source = initiator.protocol == Protocol::Axi4Lite;
  // check_apb_initiator and check_ahb_initiator own the port and burst rules of their faces.
  const bool face_source = apb_initiator(initiator) || detail::ahb_protocol(initiator.protocol);
  // An AXI3 AxLEN has four bits: bursts of at most 16 beats.
  const bool axi3 = initiator.protocol == Protocol::Axi3;
  for (const auto &[name, value, limit] :
       {std::tuple{"id_width", initiator.id_width, 16U},
        std::tuple{"len_width", initiator.len_width, axi3 ? 4U : 8U}}) {
    if (!value || face_source)
      continue;
    if (lite_source)
      c("NCL_ENDPOINT_TRANSACTION_WIDTH", field + "." + name,
        "AXI4-Lite sources carry no ID or length ports");
    else if (*value > limit)
      c("NCL_ENDPOINT_TRANSACTION_WIDTH", field + "." + name,
        std::string(axi3 ? "AXI3" : "AXI4") + " source " + name + " must be 0 to " +
            std::to_string(limit));
  }
  if (initiator.narrow_bursts && !face_source) {
    if (*initiator.narrow_bursts != NarrowBursts::Merge &&
        *initiator.narrow_bursts != NarrowBursts::SplitNonModifiable)
      c("NCL_AXI4_NARROW_BURSTS", field + ".narrow_bursts",
        "Expected merge or split_non_modifiable");
    else if (lite_source)
      c("NCL_AXI4_NARROW_BURSTS", field + ".narrow_bursts",
        *initiator.narrow_bursts == NarrowBursts::Merge
            ? "AXI4-Lite initiators issue single beats and have no narrow bursts to merge"
            : "AXI4-Lite initiators issue single beats and have no narrow bursts to split");
    else if (*initiator.narrow_bursts == NarrowBursts::SplitNonModifiable &&
             std::ranges::any_of(c.d.targets, [&](const Target &t) {
               return t.read_side_effects.value_or(false) && connects(c.d, initiator.name, t.name);
             }))
      c("NCL_AXI4_NARROW_BURSTS", field + ".narrow_bursts",
        "A target with read_side_effects already cuts Non-modifiable INCR bursts into aligned "
        "blocks; split_non_modifiable cannot reach it");
  }
  if (initiator.max_outstanding &&
      (*initiator.max_outstanding == 0 || *initiator.max_outstanding > 64))
    c("NCL_AXI4_CAPACITY", field + ".max_outstanding",
      "The shared outstanding capacity must be 1 to 64");
  // APB initiators keep one transfer (check_apb_initiator diagnoses it). A Lite source has one
  // ordering key per direction; its shared table can limit those two keys to one entry.
  const auto capacity = capacity_at(c.d, initiator, field).first;
  const bool multi = capacity > 1 && capacity <= 64 && !face_source;
  // The ID table: the {direction, AXI ID} keys a source has in flight at once, reads and writes
  // together. Lite has no AXI ID, so its key is just the direction.
  const auto ids =
      initiator.id_table_entries ? initiator.id_table_entries : options.id_table_entries;
  if (face_source && initiator.id_table_entries)
    c("NCL_AXI4_ID_CAPACITY", field + ".id_table_entries",
      "APB and AHB initiators have no source ID table");
  else if (ids && !face_source && (capacity > 1 || initiator.id_table_entries)) {
    const auto ids_at = initiator.id_table_entries ? field + ".id_table_entries"
                                                   : std::string("transport.id_table_entries");
    const unsigned width = initiator.id_width.value_or(4);
    const bool one_way =
        initiator.access == Access::ReadOnly || initiator.access == Access::WriteOnly;
    const unsigned keys = lite_source ? 1U : (1U << std::min(width, 6U));
    const unsigned bound = std::min(capacity, keys * (one_way ? 1U : 2U));
    if (!lite_source && initiator.id_table_entries && capacity < 2)
      c("NCL_AXI4_ID_CAPACITY", ids_at, "An ID table needs more than one outstanding burst");
    else if (*ids == 0 || *ids > bound)
      c("NCL_AXI4_ID_CAPACITY", ids_at,
        lite_source ? "The AXI4-Lite ID table holds 1 to min(max_outstanding, directions) = " +
                          std::to_string(bound) + " entries for this initiator"
                    : "The ID table holds 1 to min(max_outstanding, directions * 2^ID width) = " +
                          std::to_string(bound) + " entries for this initiator");
  }
  return multi;
}
// Validate classes before building their total, disjoint numeric partition.
void check_source_paths(Checks &c, const Initiator &initiator, const std::string &field) {
  const auto &classes = initiator.route_classes;
  const auto at = field + ".route_classes";
  const auto fail = [&](const std::string &where, const std::string &message) {
    c("NCL_SOURCE_PATH", where, message);
  };
  if (classes.empty()) {
    if (initiator.default_class || !initiator.id_ranges.empty())
      fail(at, "Declare route_classes before default_class or id_ranges");
    return;
  }
  const unsigned width = initiator.id_width.value_or(4);
  if (!full_axi(initiator.protocol) || width > 16 || classes.size() > std::min(32U, 1U << width)) {
    fail(at, "An AXI4 or AXI3 source has 1 to min(32, 2^id_width) route classes");
    return;
  }
  std::set<std::string> names;
  if (std::ranges::any_of(
          classes, [&](const auto &name) { return name.empty() || !names.insert(name).second; })) {
    fail(at, "Route class names must be nonempty and unique");
    return;
  }
  if (!initiator.default_class || !names.contains(*initiator.default_class)) {
    fail(field + ".default_class", "default_class must name a declared route class");
    return;
  }
  auto ranges = initiator.id_ranges;
  std::ranges::sort(ranges, {}, &IdRange::first);
  unsigned last = 0;
  std::string previous;
  for (const auto &r : ranges) {
    if (r.first > r.last || r.last >= (1U << width) || !names.contains(r.route_class)) {
      fail(field + ".id_ranges", "Each closed ID range must fit AxID and name a declared class");
      return;
    }
    if (!previous.empty() && r.first <= last && r.route_class != previous) {
      fail(field + ".id_ranges", "ID ranges of different classes must not overlap");
      return;
    }
    if (previous.empty() || r.first > last) {
      previous = r.route_class;
      last = r.last;
    } else {
      last = std::max(last, r.last);
    }
  }
  std::set<unsigned> reached;
  for (const auto &r : detail::class_partition(initiator))
    reached.insert(r.path);
  if (reached.size() != classes.size())
    fail(at, "Every declared route class must receive at least one AxID");
}
void check_initiators(Checks &c) {
  const auto &d = c.d;
  std::set<std::string> names;
  bool multi_sources = false;
  for (std::size_t i = 0; i < d.initiators.size(); ++i) {
    const auto &initiator = d.initiators[i];
    const auto field = "initiators[" + std::to_string(i) + "]";
    c.name(initiator.name, field + ".name");
    const auto &qos = initiator.qos;
    if (qos.levels.read > 7 || qos.levels.write > 7)
      c("NCL_QOS_LEVEL", field + ".qos.levels", "Fixed scheduling levels must be 0..7");
    if (const auto &l = qos.shaper;
        l && (l->bytes_per_1024_cycles > 65535 || l->burst_bytes > 65535))
      c("NCL_QOS_SHAPER", field + ".qos.shaper",
        "A shaper's bytes_per_1024_cycles and burst_bytes are each 0..65535");
    if (const auto &g = qos.scheduler;
        g && (g->under_level > 7 || g->over_level > 7 || g->threshold_percent > 100))
      c("NCL_QOS_SCHEDULER", field + ".qos.scheduler",
        "A scheduler's levels are 0..7 and its threshold_percent 0..100");
    else if (g && (!qos.shaper || qos.levels != QosLevels{}))
      c("NCL_QOS_SCHEDULER", field + ".qos.scheduler",
        "A scheduler needs a shaper and excludes fixed levels");
    if (const auto &m = initiator.qos.axqos_map; !m.empty()) {
      const auto at = field + ".qos.axqos_map";
      if (!full_axi(initiator.protocol))
        c("NCL_QOS_MAP", at, "An AxQOS map needs an axi4 or axi3 initiator");
      else if (m.size() > 16 || !std::has_single_bit(m.size()) ||
               std::ranges::any_of(m, [](unsigned v) { return v > 7; }) ||
               !std::ranges::is_sorted(m) || m.size() < 2)
        c("NCL_QOS_MAP", at, "An AxQOS map lists 2, 4, 8 or 16 levels of 0..7 that never decrease");
      else if (qos.shaper || qos.scheduler || qos.levels != QosLevels{})
        c("NCL_QOS_MAP", at,
          "An AxQOS map sets the source's levels and excludes fixed levels, a shaper and a "
          "scheduler");
    }
    for (const auto &[key, pipe] : {std::pair{"request", &initiator.pipes.request},
                                    std::pair{"packets", &initiator.pipes.packets},
                                    std::pair{"response", &initiator.pipes.response}})
      check_endpoint_pipe(*pipe, field + ".pipes." + key, c);
    if (initiator.pipes.request != InitiatorPipes{}.request && !full_axi(initiator.protocol))
      c("NCL_ENDPOINT_PIPE", field + ".pipes.request", "Port pipes need an axi4 or axi3 initiator");
    else if (initiator.pipes.request == LinkPipeline{})
      c("NCL_ENDPOINT_PIPE", field + ".pipes.request",
        "AW, W and AR need a forward or a backward stage at the port");
    if (initiator.pipes.response != InitiatorPipes{}.response && !full_axi(initiator.protocol))
      c("NCL_ENDPOINT_PIPE", field + ".pipes.response",
        "Port pipes need an axi4 or axi3 initiator");
    if (!initiator.pipes.response.forward)
      c("NCL_ENDPOINT_PIPE", field + ".pipes.response.forward",
        "The R and B forward stage is the initiator's own register; forward false is not "
        "supported");
    check_initiator_face(c, initiator, field);
    multi_sources |= check_initiator_bursts(c, initiator, field);
    check_source_paths(c, initiator, field);
    if (!names.insert(initiator.name).second)
      c("NCL_NAME_COLLISION", field + ".name", "Duplicate initiator name");
    if (c.clocked)
      c.name(initiator.clock_domain, field + ".clock_domain");
    if (c.clocked ? !c.domains.contains(initiator.clock_domain)
                  : initiator.clock_domain != d.initiators.front().clock_domain)
      c("NCL_DOMAIN_REFERENCE", field + ".clock_domain", "Unknown clock domain");
  }
  if (d.transport && d.transport->id_table_entries && !multi_sources)
    c("NCL_AXI4_ID_CAPACITY", "transport.id_table_entries",
      "An ID table needs an AXI4, AXI3 or AXI4-Lite initiator with more than one outstanding "
      "transaction");
}
// The protocol profile, data width and transaction ports of a target.
void check_target_face(Checks &c, const Target &t, const std::string &field) {
  const auto &d = c.d;
  const auto network_bits = c.network_bits;
  const auto protocol = t.protocol.value_or(Protocol::Axi4);
  const auto &caps = detail::protocol_caps(protocol);
  const bool narrow_address = t.address_width.value_or(d.address_width) <= 32;
  if (!caps.egress)
    c("NCL_UNSUPPORTED_PROTOCOL", field + ".protocol",
      "Expected axi4, axi3 or a dedicated ahb_v3, ahb5, apb5, apb4, apb3, apb2 or axi4_lite "
      "target");
  else if (detail::ahb_protocol(t.protocol) && (network_bits != 32 || !narrow_address))
    c("NCL_AHB_OUTLET_PROFILE", field + ".protocol",
      "Dedicated AHB targets require a 32-bit network data width (a wider AHB target sets its own "
      "data_width) and at most 32 local address bits; set address_width when the global width "
      "exceeds 32");
  else if (caps.apb && (network_bits != 32 || !narrow_address))
    c("NCL_APB_BURST_OUTLET_PROFILE", field + ".protocol",
      "Burst APB targets require a 32-bit network and at most 32 local address bits; set "
      "address_width when the global width exceeds 32");
  else if (t.protocol == Protocol::Axi4Lite && ((t.data_width.value_or(network_bits) != 32 &&
                                                 t.data_width.value_or(network_bits) != 64) ||
                                                !narrow_address))
    c("NCL_AXI_LITE_OUTLET_PROFILE", field + ".protocol",
      "Dedicated AXI4-Lite targets require 32- or 64-bit target data and at most 32 local "
      "address bits; set address_width when the global width exceeds 32");
  if (t.data_width) {
    const auto width = *t.data_width;
    // Only APB targets take 8 or 16 bits, a narrow face of a 32-bit engine.
    if (!detail::width_in(caps.egress_widths, width))
      c("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
        "Target data width must be 32, 64, 128, 256, 512 or 1024");
    else if (width != network_bits && width >= 32 && !full_axi(protocol) &&
             !detail::ahb_protocol(protocol) && !(protocol == Protocol::Axi4Lite && width <= 64))
      c("NCL_ENDPOINT_DATA_WIDTH", field + ".data_width",
        "A target data width different from the network data width needs an AXI4, AHB "
        "32/64-bit AXI4-Lite or 8/16-bit APB target");
  }
  // Ports of an AXI4 target; its AxLEN carries one beat of every source.
  const auto wide = std::ranges::find_if(d.initiators, [&](const Initiator &i) {
    return connects(d, i.name, t.name) &&
           source_engine_width(i, c.source_bits) > t.data_width.value_or(network_bits)
                                                       << t.len_width.value_or(4);
  });
  const bool axi = full_axi(protocol);
  const bool bad_id = !axi ? t.id_width.has_value() : t.id_width > 31U;
  if (bad_id || (!axi ? t.len_width.has_value() : t.len_width.value_or(4) - 4 > 4))
    c("NCL_ENDPOINT_TRANSACTION_WIDTH", field + (bad_id ? ".id_width" : ".len_width"),
      "Only AXI4 targets have ID ports, 0 to 31 bits, and length ports, 4 to 8 bits");
  // An AXI3 AxLEN has four bits.
  else if (protocol == Protocol::Axi3 && t.len_width.value_or(4) != 4)
    c("NCL_ENDPOINT_TRANSACTION_WIDTH", field + ".len_width",
      "AXI3 targets have 4-bit length ports");
  else if (axi && wide != d.initiators.end())
    c("NCL_ENDPOINT_TRANSACTION_WIDTH", field + ".len_width",
      "2^len_width target words must hold one beat of initiator " + wide->name);
}
// Attribute mapping of a target: signals the protocol lacks must hold a preset's bits, never a
// mapping that would be silently dropped.
void check_target_attributes(Checks &c, const Target &t, const std::string &field) {
  const auto protocol = t.protocol.value_or(Protocol::Axi4);
  const auto &caps = detail::protocol_caps(protocol);
  if (!t.attributes && caps.attributes)
    c("NCL_TARGET_ATTRIBUTES", field + ".attributes",
      "AXI4, AXI3, AXI4-Lite, APB5, APB4 and AHB targets need attributes, for example amba");
  if (!t.attributes)
    return;
  if (t.protocol == Protocol::Ahb5 && *t.attributes != TargetAttributes::amba())
    c("NCL_AHB5_PROPERTIES", field + ".attributes", "AHB5 targets require attributes: amba");
  const auto valid = [](const auto &bits) {
    return std::ranges::all_of(bits, [](const AttributeBit &bit) {
      return bit.source >= AttributeSource::Zero && bit.source <= AttributeSource::Instruction;
    });
  };
  const auto amba = TargetAttributes::amba(), zero = TargetAttributes::zero();
  const auto unused = [&](auto member) {
    return (*t.attributes).*member != amba.*member && (*t.attributes).*member != zero.*member;
  };
  const bool axi = full_axi(protocol), ahb = detail::ahb_protocol(protocol);
  if (!caps.attributes)
    c("NCL_TARGET_ATTRIBUTES", field + ".attributes", "APB3 and APB2 targets have no PPROT to map");
  else if (!valid(t.attributes->read_cache) || !valid(t.attributes->write_cache) ||
           !valid(t.attributes->prot) || !valid(t.attributes->hprot))
    c("NCL_TARGET_ATTRIBUTES", field + ".attributes", "Unknown attribute source");
  else if ((!axi &&
            (unused(&TargetAttributes::read_cache) || unused(&TargetAttributes::write_cache))) ||
           (ahb && unused(&TargetAttributes::prot)) || (!ahb && unused(&TargetAttributes::hprot)))
    c("NCL_TARGET_ATTRIBUTES", field + ".attributes",
      "A mapping for a signal this target protocol lacks would be ignored");
}
// Protocol options of a target: back-to-back APB access, FIXED bursts, AHB burst shaping and
// HRESP width, the timeout and the in-flight capacity. Burst shaping exists only on the AHB
// outlet: other targets have no HBURST, so even an explicit default is rejected there.
void check_target_options(Checks &c, const Target &t, const std::string &field) {
  check_ahb5(c, t.protocol, t.ahb5, field);
  if (t.protocol == Protocol::Ahb5 && t.hresp_width.value_or(1) != 1)
    c("NCL_TARGET_AHB_HRESP", field + ".hresp_width", "AHB5 requires one-bit HRESP");
  const auto &caps = detail::protocol_caps(t.protocol.value_or(Protocol::Axi4));
  for (const auto &[key, pipe] :
       {std::pair{"arrival", &t.pipes.arrival}, std::pair{"response", &t.pipes.response},
        std::pair{"request", &t.pipes.request}})
    check_endpoint_pipe(*pipe, field + ".pipes." + key, c);
  if (t.apb_back_to_back && !caps.apb)
    c("NCL_TARGET_APB_ACCESS", field + ".apb_back_to_back",
      "Back-to-back access requires an APB5, APB4, APB3 or APB2 target");
  // An outlet timer must see its target's handshakes.
  if (t.pipes.request != TargetPipes{}.request && (!full_axi(t.protocol) || t.timeout))
    c("NCL_ENDPOINT_PIPE", field + ".pipes.request",
      "Port pipes need an axi4 or axi3 target without timeout");
  if (!t.pipes.request.forward)
    c("NCL_ENDPOINT_PIPE", field + ".pipes.request.forward",
      "The AW, W and AR forward stage is the outlet's own register; forward false is not "
      "supported");
  if (t.fixed_bursts && *t.fixed_bursts != FixedBursts::Reject &&
      *t.fixed_bursts != FixedBursts::Single)
    c("NCL_TARGET_FIXED_BURSTS", field + ".fixed_bursts", "Expected reject or single");
  else if (t.fixed_bursts && !caps.fixed_single)
    c("NCL_TARGET_FIXED_BURSTS", field + ".fixed_bursts",
      "AXI4 targets receive FIXED bursts unchanged; fixed_bursts applies to dedicated apb5, apb4, "
      "apb3, apb2, axi4_lite and AHB targets");
  for (const auto &[present, key] :
       {std::pair{t.write_bursts.has_value(), "write_bursts"},
        std::pair{t.undefined_length_bursts.has_value(), "undefined_length_bursts"}})
    if (present && !caps.ahb_shape)
      c("NCL_TARGET_AHB_BURSTS", field + "." + key, "Burst shaping applies to AHB targets only");
  if (t.hresp_width && !caps.ahb_shape)
    c("NCL_TARGET_AHB_HRESP", field + ".hresp_width", "hresp_width applies to AHB targets only");
  else if (t.hresp_width && *t.hresp_width != 1 && *t.hresp_width != 2)
    c("NCL_TARGET_AHB_HRESP", field + ".hresp_width",
      "Expected 1 (AHB-Lite/AHB5) or 2 (AMBA 2 AHB)");
  if (t.write_bursts && *t.write_bursts != WriteBursts::Packet &&
      *t.write_bursts != WriteBursts::Complete)
    c("NCL_TARGET_AHB_BURSTS", field + ".write_bursts", "Expected packet or complete");
  if (t.timeout && !detail::valid_timeout_cycles(t.timeout->cycles))
    c("NCL_TARGET_TIMEOUT", field + ".timeout.cycles",
      "Timeout cycles must be a power of two from 16 to 1048576");
  else if (t.timeout && !caps.timeout)
    c("NCL_TARGET_TIMEOUT", field + ".timeout",
      "APB2 targets complete every access in a fixed number of cycles and cannot stall");
  // A dedicated AXI4-Lite target pipelines up to 256 packets of one direction; an AXI4 target
  // keeps 1..16 bursts in flight shared by both.
  const bool lite = t.protocol == Protocol::Axi4Lite;
  if (t.max_outstanding && (*t.max_outstanding == 0 || *t.max_outstanding > (lite ? 256 : 16)))
    c("NCL_AXI4_CAPACITY", field + ".max_outstanding",
      lite ? "An AXI4-Lite target keeps 1 to 256 packets of one direction in flight"
           : "The in-flight capacity of a target (reads and writes combined) must be 1 to 16");
  else if (t.max_outstanding > caps.max_outstanding)
    c("NCL_AXI4_CAPACITY_COMBINATION", field + ".max_outstanding",
      "Dedicated APB and AHB targets keep one burst in flight");
}
// The exclusive access mode and monitors of a target.
void check_target_exclusive(Checks &c, const Target &t, const std::string &field) {
  if (!t.exclusive && !t.exclusive_monitors && t.exclusive_id_flags.empty())
    return;
  const auto mode = t.exclusive.value_or(ExclusiveAccess::None);
  const auto at = field + (t.exclusive ? ".exclusive" : ".exclusive_monitors");
  const auto keys = exclusive_keys(c.d, t);
  if (mode != ExclusiveAccess::None && mode != ExclusiveAccess::Network &&
      mode != ExclusiveAccess::Target)
    c("NCL_TARGET_EXCLUSIVE", field + ".exclusive", "Expected none, network or target");
  else if (t.exclusive_monitors && mode != ExclusiveAccess::Network)
    c("NCL_TARGET_EXCLUSIVE", at, "exclusive_monitors applies to exclusive: network only");
  else if (mode == ExclusiveAccess::None && !t.exclusive_id_flags.empty())
    c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_id_flags",
      "exclusive_id_flags applies to exclusive: network or target only");
  else if (mode == ExclusiveAccess::None)
    ;
  else if (mode == ExclusiveAccess::Target &&
           !detail::protocol_caps(t.protocol.value_or(Protocol::Axi4)).lock)
    c("NCL_TARGET_EXCLUSIVE", at,
      "exclusive: target passes AxLOCK to an AXI4 or AXI3 target; APB, AHB and AXI4-Lite targets "
      "have "
      "no exclusive signal and use exclusive: network");
  else if (t.access == Access::ReadOnly)
    c("NCL_TARGET_EXCLUSIVE", at, "A read-only target supports no exclusive access (AXI A10.2.2)");
  else if (t.access == Access::WriteOnly)
    c("NCL_TARGET_EXCLUSIVE", at,
      "A write-only target supports no exclusive access: an exclusive write follows an exclusive "
      "read (AXI A7.2)");
  else if (t.exclusive_monitors && (*t.exclusive_monitors == 0 || *t.exclusive_monitors > 16))
    c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_monitors", "exclusive_monitors must be 1 to 16");
  else if (keys == 0)
    c("NCL_TARGET_EXCLUSIVE", at,
      "No read-write AXI4 or AXI3 initiator reaches this target, so no exclusive access can "
      "arrive");
  else if (t.exclusive_id_flags.empty())
    c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_id_flags",
      "Exclusive access needs exclusive_id_flags, the user flags naming its owner");
  else {
    const auto &flags = c.d.transport.value_or(TransportOptions{}).user_flags;
    for (std::size_t i = 0; i < t.exclusive_id_flags.size(); ++i) {
      const auto &name = t.exclusive_id_flags[i];
      if (std::ranges::find(flags, name) == flags.end())
        c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_id_flags[" + std::to_string(i) + "]",
          "Not a user flag of transport.user_flags");
      else if (std::ranges::count(t.exclusive_id_flags, name) > 1)
        c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_id_flags[" + std::to_string(i) + "]",
          "Duplicate user flag name");
    }
    const auto owners = t.exclusive_id_flags.size();
    if (t.exclusive_monitors && owners < 5 && *t.exclusive_monitors > 1U << owners)
      c("NCL_TARGET_EXCLUSIVE", field + ".exclusive_monitors",
        "exclusive_monitors exceeds the " + std::to_string(1U << owners) +
            " values of exclusive_id_flags");
  }
}
// Target mappings as (begin, field, end).
// One mapping of a target: inside the address space, whole pages or a power-of-two sub-page
// aligned to its size, its local interval within the local address port.
void check_mapping(Checks &c, const Target &t, const AddressMapping &m, const std::string &field) {
  const auto limit = std::uint64_t{1} << c.d.address_width;
  const bool sized = t.address_width >= 2U && t.address_width <= c.d.address_width;
  const auto local = std::uint64_t{1} << (sized ? *t.address_width : c.d.address_width);
  if (m.base >= limit || m.size == 0 || m.size > limit - m.base) {
    c("NCL_ADDRESS_RANGE", field, "Mapping exceeds the address space or is empty");
    return;
  }
  if (m.local_address >= local || m.size > local - m.local_address)
    c("NCL_ENDPOINT_ADDRESS_WIDTH", field,
      "Every local byte address of the mapping must fit the target address port");
  const bool page = m.base % 4096 == 0 && m.size % 4096 == 0 && m.local_address % 4096 == 0;
  if (!page && (m.size < 4 || m.size > 4096 || !std::has_single_bit(m.size) ||
                m.base % m.size != 0 || m.local_address % m.size != 0))
    c("NCL_ADDRESS_RANGE", field,
      "A mapping is whole 4 KiB pages, or a power-of-two sub-page of at least four bytes whose "
      "base and local address are aligned to its size");
  // A wide word decodes as one beat, and an AXI4 packet never crosses a mapping edge: the
  // mapping holds one word.
  else if ((detail::ahb_protocol(t.protocol) || t.protocol == Protocol::Axi4Lite ||
            t.protocol == Protocol::Axi4) &&
           m.size < t.data_width.value_or(c.network_bits) / 8)
    c("NCL_ADDRESS_RANGE", field,
      "An AHB, AXI4-Lite or AXI4 target mapping must hold at least one target data word");
}
// The local address width, name, domain and mappings of a target.
void check_target_place(Checks &c, const Target &t, const std::string &field,
                        std::set<std::string> &names) {
  if (t.address_width && (*t.address_width < 2 || *t.address_width > c.d.address_width))
    c("NCL_ENDPOINT_ADDRESS_WIDTH", field + ".address_width",
      "Target address width must be between two and the global address width");
  if (!names.insert(t.name).second)
    c("NCL_NAME_COLLISION", field + ".name", "Duplicate target name");
  if (c.clocked)
    c.name(t.clock_domain, field + ".clock_domain");
  if (c.clocked ? !c.domains.contains(t.clock_domain)
                : t.clock_domain != c.d.targets.front().clock_domain)
    c("NCL_DOMAIN_REFERENCE", field + ".clock_domain", "Unknown clock domain");
  if (t.mappings.empty())
    c("NCL_ADDRESS_RANGE", field + ".mappings", "A target needs at least one mapping");
  else
    for (std::size_t m = 0; m < t.mappings.size(); ++m)
      check_mapping(c, t, t.mappings[m], field + ".mappings[" + std::to_string(m) + "]");
}
// The flows of a target: 1..32, several only at an APB, AHB or AXI4-Lite target (one slave
// interface each) or an AXI4 or AXI3 target with an ID port (the flow number in AxID), every
// mapping naming a flow and every flow reached by a mapping.
// The shapes a read interleave size was verified on (Target::read_interleave_bytes).
bool interleave_size_target(const Target &t) {
  return t.len_width == 8U && t.id_width.value_or(0) <= 4 && t.max_outstanding.value_or(1) <= 4 &&
         t.access != Access::WriteOnly && t.flows == 1 && !t.exclusive && !t.timeout &&
         t.pipes == TargetPipes{} && !t.response_user_width && !t.data_user_width &&
         t.user.empty() && !t.read_side_effects.value_or(false) && t.mappings.size() == 1;
}
bool interleave_size_network(const Definition &d, const Target &t) {
  return !d.topology && !d.transport && d.clock_gating == ClockGating::PerSwitchAndEndpoint &&
         d.clock_domains.size() == 1 && d.initiators.size() <= 2 && d.targets.size() <= 2 &&
         std::ranges::all_of(d.targets, [&](const Target &other) {
           return other.protocol == t.protocol && other.data_width == t.data_width &&
                  other.max_outstanding == t.max_outstanding && other.access == t.access;
         });
}
bool interleave_size_source(const Definition &d, const Initiator &i, const Target &t,
                            unsigned width) {
  const auto &first = d.initiators.front();
  return connects(d, i.name, t.name) && i.protocol == Protocol::Axi4 &&
         (width == 32 || width == 64) && i.data_width == first.data_width && i.len_width == 8U &&
         i.id_width.value_or(0) <= 4 && i.max_outstanding.value_or(1) <= 4 &&
         i.max_outstanding == first.max_outstanding && !i.access && !i.narrow_bursts &&
         !i.id_table_entries && !i.default_target && !i.user_width && !i.response_user_width &&
         !i.data_user_width && i.qos == InitiatorQos{} && i.route_classes.empty() &&
         i.pipes == InitiatorPipes{};
}
void check_read_interleave(Checks &c, const Target &t, const std::string &field) {
  if (!t.read_interleave_bytes)
    return;
  const auto &d = c.d;
  const auto bytes = *t.read_interleave_bytes;
  const auto reject = [&](const std::string &message) {
    c("NCL_TARGET_READ_INTERLEAVE", field + ".read_interleave_bytes", message);
  };
  if (t.protocol.value_or(Protocol::Axi4) != Protocol::Axi4)
    return reject("Only AXI4 targets set a read interleave size");
  const auto word = t.data_width.value_or(c.network_bits) / 8;
  if (word != 4 && word != 8)
    return reject("A read interleave size needs a 32- or 64-bit target");
  if (!interleave_size_target(t))
    return reject("A read interleave size needs a target that reads, with len_width 8, at most 4 "
                  "ID bits and 4 bursts in flight, one flow and one mapping, and no exclusive "
                  "access, timeout, pipes, user fields or read side effects");
  if (!interleave_size_network(d, t))
    return reject("A read interleave size needs the automatic exchange with clock gating, one "
                  "clock domain, at most two sources and two targets of one protocol, width, "
                  "access and burst count, and no transport options");
  for (const auto &i : d.initiators) {
    const auto width = source_engine_width(i, c.source_bits);
    if (!interleave_size_source(d, i, t, width))
      return reject("A read interleave size needs every source to reach the target and be AXI4 "
                    "of 32 or 64 bits like the others, with len_width 8, at most 4 ID bits and 4 "
                    "bursts in flight, and no other option set: " +
                    i.name);
    if (bytes > word && width / 8 > bytes)
      return reject("A size above one word needs sources with words no wider: " + i.name);
  }
  if (bytes == 0)
    return;
  if (t.access == Access::ReadOnly && t.id_width.value_or(4) == 0)
    return reject("A read-only target with one flow and no ID has nothing to interleave: use 0");
  if (!std::has_single_bit(bytes) || bytes < word || bytes > 32)
    return reject("Use 0 or a power of two from one target word to 32 bytes");
}
void check_target_paths(Checks &c, const Target &t, const std::string &field) {
  if (t.flows < 1 || t.flows > 32) {
    c("NCL_TARGET_PATH", field + ".flows", "A target has 1 to 32 flows");
    return;
  }
  const auto protocol = t.protocol.value_or(Protocol::Axi4);
  if (t.flows > 1 && full_axi(protocol) && !t.id_width.value_or(4)) {
    c("NCL_TARGET_PATH", field + ".flows",
      "Several flows need an ID port at an axi4 or axi3 target (the flow in AxID); other "
      "targets have an interface per flow");
    return;
  }
  std::vector<bool> reached(t.flows);
  for (std::size_t m = 0; m < t.mappings.size(); ++m)
    if (t.mappings[m].flow >= t.flows)
      c("NCL_TARGET_PATH", field + ".mappings[" + std::to_string(m) + "].flow",
        t.name + " has flows 0 to " + std::to_string(t.flows - 1));
    else
      reached[t.mappings[m].flow] = true;
  if (const auto lost = std::ranges::find(reached, false); lost != reached.end())
    c("NCL_TARGET_PATH", field + ".flows",
      "Flow " + std::to_string(lost - reached.begin()) + " of " + t.name + " has no mapping");
}
// Overlapping mappings: the smaller decodes, so blocks of one size never overlap, an initiator
// reaching a mapping reaches every smaller one inside it, and what the smaller ones leave of an
// AHB, AXI4-Lite or AXI4 mapping holds whole target words.
void check_targets(Checks &c) {
  std::set<std::string> names;
  for (std::size_t i = 0; i < c.d.targets.size(); ++i) {
    const auto &t = c.d.targets[i];
    const auto field = "targets[" + std::to_string(i) + "]";
    c.name(t.name, field + ".name");
    check_target_face(c, t, field);
    check_target_attributes(c, t, field);
    check_target_options(c, t, field);
    check_target_exclusive(c, t, field);
    check_target_place(c, t, field, names);
    check_target_paths(c, t, field);
    check_read_interleave(c, t, field);
  }
  if (!c.errors.empty())
    return;
  const auto layout = layout_mappings(c.d);
  auto at = [](std::pair<unsigned, unsigned> m) {
    return "targets[" + std::to_string(m.first) + "].mappings[" + std::to_string(m.second) + "]";
  };
  for (const auto &[outer, inner, same] : layout.overlaps)
    if (same || std::ranges::any_of(c.d.initiators, [&](const Initiator &s) {
          return connects(c.d, s.name, c.d.targets[outer.first].name) &&
                 !connects(c.d, s.name, c.d.targets[inner.first].name);
        }))
      c("NCL_ADDRESS_OVERLAP", at(outer),
        same ? "Mappings overlap in an aligned block of the same size"
             : "An initiator reaching a mapping must reach every smaller mapping inside it");
  for (unsigned i = 0; i < c.d.targets.size(); ++i)
    for (unsigned m = 0; m < layout.mappings[i].size(); ++m)
      if (std::ranges::any_of(layout.mappings[i][m].first, [&](const AddressWindow &r) {
            const auto &t = c.d.targets[i];
            return !detail::protocol_caps(t.protocol.value_or(Protocol::Axi4)).apb &&
                   r.size < t.data_width.value_or(c.network_bits) / 8;
          }))
        c("NCL_ADDRESS_RANGE", at({i, m}),
          "Smaller mappings must leave whole target data words of an AHB, AXI4-Lite or AXI4 "
          "target mapping");
  // A source with several bursts in flight records each burst's destination routing window.
  std::size_t windows = 0;
  for (const auto &target : layout.mappings)
    for (const auto &[routes, _] : target)
      windows += routes.size();
  for (std::set<std::string> fields; const auto &s : c.d.initiators)
    if (const auto [n, field] =
            capacity_at(c.d, s, "initiators[" + std::to_string(&s - c.d.initiators.data()) + "]");
        windows > detail::max_destinations && n > 1 && n <= 64 && !apb_initiator(s) &&
        fields.insert(field).second)
      c("NCL_AXI4_CAPACITY_COMBINATION", field,
        "A multi-burst source decodes at most 256 routing windows, not " + std::to_string(windows));
}
void check_data_user(Checks &c) {
  const auto k = c.d.transport.value_or(TransportOptions{}).data_user_bits_per_byte;
  if (k > 4)
    c("NCL_APB5_DATA_USER_WIDTH", "transport.data_user_bits_per_byte",
      "Data user bits per byte must be 0 to 4");
  const auto check = [&](const auto &endpoints, const std::string &kind) {
    for (std::size_t i = 0; i < endpoints.size(); ++i) {
      const auto &e = endpoints[i];
      if (!e.data_user_width)
        continue;
      const auto bytes = e.data_width.value_or(c.network_bits) / 8;
      if (e.protocol != Protocol::Apb5 || !bytes || !k || k > 4 || e.data_user_width != k * bytes)
        c("NCL_APB5_DATA_USER_WIDTH", kind + "[" + std::to_string(i) + "].data_user_width",
          "A nonzero APB5 data user width must equal data bytes times "
          "transport.data_user_bits_per_byte, with 1 to 4 bits per byte");
    }
  };
  check(c.d.initiators, "initiators");
  check(c.d.targets, "targets");
}
// Network user flags: distinct names, a driver of every flag at every source (an AxUSER bit of an
// AXI4, AXI3 or AXI4-Lite source with a user port, an attribute flag or a constant) and AxUSER
// bits of AXI4, AXI3 and AXI4-Lite targets that name declared flags.
void check_user_flags(Checks &c) {
  check_data_user(c);
  const auto &flags = c.d.transport.value_or(TransportOptions{}).user_flags;
  if (c.d.transport && c.d.transport->response_user_bits > 256)
    c("NCL_USER_FLAGS", "transport.response_user_bits",
      "A response packet carries at most 256 user bits");
  if (flags.size() > 256)
    c("NCL_USER_FLAGS", "transport.user_flags", "A network carries at most 256 user flags");
  for (std::size_t i = 0; i < flags.size(); ++i) {
    c.name(flags[i], "transport.user_flags[" + std::to_string(i) + "]");
    if (std::ranges::count(flags, flags[i]) > 1)
      c("NCL_USER_FLAGS", "transport.user_flags[" + std::to_string(i) + "]",
        "Duplicate user flag name");
  }
  const auto axi = [](std::optional<Protocol> p) {
    return p.value_or(Protocol::Axi4) == Protocol::Axi4 || p == Protocol::Axi4Lite;
  };
  for (std::size_t i = 0; i < c.d.initiators.size(); ++i) {
    const auto &s = c.d.initiators[i];
    const auto field = "initiators[" + std::to_string(i) + "]";
    for (const auto &[width, key] : {std::pair{s.user_width, ".user_width"},
                                     std::pair{s.response_user_width, ".response_user_width"}})
      if (width && s.protocol == Protocol::Apb5) {
        const auto limit = key == std::string(".user_width") ? 128U : 16U;
        if (*width > limit)
          c("NCL_APB5_USER_WIDTH", field + key,
            "APB5 request user width is 0 to 128 and response user width is 0 to 16");
      } else if (width && ((!axi(s.protocol) && s.protocol != Protocol::Axi3) || *width == 0 ||
                           *width > 256))
        c("NCL_USER_FLAGS", field + key,
          "AXI4, AXI3 and AXI4-Lite initiator user widths are 1 to 256 bits. "
          "APB5 request widths are 0 to 128 and response widths are 0 to 16");
    for (const auto &[name, bit] : s.user_flags)
      if (std::ranges::find(flags, name) == flags.end())
        c("NCL_USER_FLAGS", field + ".user_flags." + name,
          "Not a user flag of transport.user_flags");
      else if (bit.source > AttributeSource::User ||
               (bit.source == AttributeSource::User && bit.index >= s.user_width.value_or(0)))
        c("NCL_USER_FLAGS", field + ".user_flags." + name,
          s.protocol == Protocol::Apb5
              ? "Expected a constant, an attribute flag or a bit of this initiator's PAUSER port"
              : "Expected a constant, an attribute flag or a bit of this initiator's AxUSER port");
    for (const auto &name : flags)
      if (!s.user_flags.contains(name))
        c("NCL_USER_FLAGS", field + ".user_flags",
          "Every initiator drives every user flag; " + name + " has no driver");
  }
  for (std::size_t i = 0; i < c.d.targets.size(); ++i) {
    const auto &t = c.d.targets[i];
    const auto field = "targets[" + std::to_string(i) + "].user";
    const bool apb5 = t.protocol == Protocol::Apb5;
    const bool ports = axi(t.protocol) || t.protocol == Protocol::Axi3;
    if (apb5 && (t.user.size() > 128 || t.response_user_width.value_or(0) > 16))
      c("NCL_APB5_USER_WIDTH", "targets[" + std::to_string(i) + "]",
        "APB5 request user width is 0 to 128 and response user width is 0 to 16");
    if (!apb5 && !t.user.empty() && (!ports || t.user.size() > 256))
      c("NCL_USER_FLAGS", field,
        "Only AXI4, AXI3 and AXI4-Lite targets have AxUSER ports, 1 to 256 bits");
    if (const auto width = t.response_user_width;
        !apb5 && width && (!ports || *width == 0 || *width > 256))
      c("NCL_USER_FLAGS", "targets[" + std::to_string(i) + "].response_user_width",
        "Only AXI4, AXI3 and AXI4-Lite targets have BUSER and RUSER ports, 1 to 256 bits");
    for (std::size_t b = 0; b < t.user.size(); ++b)
      if (const auto &bit = t.user[b];
          !bit.flag.empty() && (bit.value || std::ranges::find(flags, bit.flag) == flags.end()))
        c("NCL_USER_FLAGS", field + "[" + std::to_string(b) + "]",
          "Expected 0, 1 or a user flag of transport.user_flags");
  }
}
void check_endpoint_gating(Checks &c) {
  const auto check = [&](const auto &endpoints, const std::string &kind) {
    for (std::size_t i = 0; i < endpoints.size(); ++i) {
      const auto &e = endpoints[i];
      if (!e.clock_gating)
        continue;
      const auto domain = std::ranges::find(c.d.clock_domains, e.clock_domain, &ClockDomain::name);
      if (*e.clock_gating < EndpointClockGating::Separate ||
          *e.clock_gating > EndpointClockGating::TransportOnly ||
          c.d.clock_gating == ClockGating::None ||
          (domain != c.d.clock_domains.end() && !domain->gated))
        c("NCL_ENDPOINT_CLOCK_GATING", kind + "[" + std::to_string(i) + "].clock_gating",
          "An endpoint mode must be separate, shared or transport_only and requires a gated domain "
          "with per_switch_and_endpoint clock gating");
    }
  };
  check(c.d.initiators, "initiators");
  check(c.d.targets, "targets");
}
using PlanData = std::shared_ptr<const detail::NetworkPlan>;
// The definition checks, then the network plan with its capability rules. Diagnostics carry
// configuration paths; make_plan locates them.
Result<PlanData> plan_definition(const Definition &d) {
  if (d.schema_version != 10)
    return Result<PlanData>::failure(detail::error("NCL_SCHEMA_VERSION", "schema_version",
                                                   "Only schema version 10 is supported"));
  Checks c(d);
  check_v10_shape(d, c);
  check_connectivity(d, c);
  if (!c.errors.empty())
    return Result<PlanData>::failure(std::move(c.errors));
  // The network data width and the source width default, both derived from the endpoints (or
  // transport.data_width).
  c.network_bits = network_data_width(d);
  c.source_bits = source_data_width(d, c.network_bits);
  c.clocked = detail::clocked_graph(d);
  if (auto fatal = check_network(c))
    return Result<PlanData>::failure(std::move(*fatal));
  if (!c.errors.empty())
    return Result<PlanData>::failure(std::move(c.errors));
  check_domains(c);
  check_endpoint_gating(c);
  check_initiators(c);
  check_modes(c);
  check_targets(c);
  check_user_flags(c);
  if (c.errors.empty())
    check_connected_mappings(d, c);
  // Explicit routes name connected pairs before their paths are planned.
  if (c.errors.empty() && d.topology)
    check_route_rows(d, c);
  if (c.errors.empty()) {
    check_graphs(d, c);
    std::ranges::move(detail::check_capabilities(d, nullptr), std::back_inserter(c.errors));
  }
  if (!c.errors.empty())
    return Result<PlanData>::failure(std::move(c.errors));
  auto built = detail::build_network_plan(d);
  if (!built)
    return Result<PlanData>::failure(built.diagnostics());
  return Result<PlanData>::success(
      std::make_shared<const detail::NetworkPlan>(std::move(built.value())));
}
} // namespace
Result<Plan> make_plan(const Definition &input, const SourceMap *sources) {
  auto planned = plan_definition(input);
  if (planned)
    return Result<Plan>::success(Plan(std::move(planned.value())));
  auto diagnostics = planned.diagnostics();
  // A schema 10 network width that was derived, not written, can be the cause of a width error:
  // name the field that fixes it. Errors that a different network width can resolve; a dedicated
  // target profile error only when the derived width is not the 32 bits it needs.
  static const std::set<std::string> width_codes = {"NCL_WIDTH_CONVERSION", "NCL_UNSUPPORTED_WIDTH",
                                                    "NCL_ENDPOINT_DATA_WIDTH", "NCL_AXI4_LEN_WIDTH",
                                                    "NCL_UNSUPPORTED_PROTOCOL"};
  static const std::set<std::string> profile_codes = {
      "NCL_AHB_OUTLET_PROFILE", "NCL_APB_BURST_OUTLET_PROFILE", "NCL_AXI_LITE_OUTLET_PROFILE"};
  const auto derived = network_data_width(input);
  // An APB2 initiator and an APB initiator outside 8/16/32 bits stay invalid at every network
  // width.
  std::set<std::string> fixed;
  for (std::size_t i = 0; i < input.initiators.size(); ++i) {
    const auto &initiator = input.initiators[i];
    const auto at = "initiators[" + std::to_string(i) + "].";
    if (initiator.protocol == Protocol::Apb2)
      fixed.insert(at + "protocol");
    if (apb_initiator(initiator) && initiator.data_width.value_or(32) != 8 &&
        initiator.data_width.value_or(32) != 16 && initiator.data_width.value_or(32) != 32)
      fixed.insert(at + "data_width");
  }
  if (input.schema_version == 10 && !(input.transport && input.transport->data_width) &&
      (derived == 32 || derived == 64) &&
      std::ranges::any_of(diagnostics, [&](const Diagnostic &d) {
        return !fixed.contains(d.field) &&
               (width_codes.contains(d.code) || (derived != 32 && profile_codes.contains(d.code)));
      }))
    diagnostics.push_back(detail::error("NCL_NETWORK_DATA_WIDTH", "transport.data_width",
                                        "The network data width defaulted to " +
                                            std::to_string(derived) +
                                            " bits (the narrowest endpoint container, at most 64; "
                                            "APB uses 32); set transport.data_width "
                                            "to 32 or 64 if another network width is intended"));
  return Result<Plan>::failure(detail::locate(std::move(diagnostics), sources));
}

Status validate(const Definition &definition, const GenerationOptions &options,
                const SourceMap *sources) {
  if (options.backend != Backend::Verilog2001)
    return Status::failure(
        detail::locate({detail::error("NCL_UNSUPPORTED_BACKEND", "output.backend",
                                      "Only Verilog-2001 is supported")},
                       sources));
  auto plan = make_plan(definition, sources);
  if (!plan)
    return Status::failure(plan.diagnostics());
  return Status::success({});
}
} // namespace nocloom
