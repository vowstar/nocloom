#include "capability.hpp"
#include "diagnostic.hpp"
#include "network_plan_profiles.hpp"
#include <algorithm>
#include <bit>
#include <iterator>
#include <set>
#include <tuple>

namespace nocloom::detail {
namespace {
using P = Protocol;
constexpr std::uint32_t narrow = 0xFF, wide = 0xFC; // 8 to 1024 bits, 32 to 1024 bits
// protocol, ingress, egress, their widths, timeout, attributes, fixed, apb, ahb, lock, max
constexpr ProtocolCaps caps[] = {
    {P::Apb4, true, true, narrow, narrow, true, true, true, true, false, false, 1},
    {P::Axi4Lite, true, true, wide, wide, true, true, true, false, false, false, 256},
    {P::Apb3, true, true, narrow, narrow, true, false, true, true, false, false, 1},
    {P::Apb2, false, true, wide, narrow, false, false, true, true, false, false, 1},
    {P::Axi4, true, true, wide, wide, true, true, false, false, false, true, 16},
    {P::AhbV3, true, true, wide, wide, true, true, true, false, true, false, 1},
    {P::Ahb5, true, true, wide, wide, true, true, true, false, true, false, 1},
    {P::Axi3, true, true, wide, wide, true, true, false, false, false, true, 16}};

std::optional<Finding> when(bool violated, std::string field, std::string message) {
  return violated ? std::optional<Finding>{{std::move(field), std::move(message)}} : std::nullopt;
}
// Whether a crossing FIFO leaves domain from (any when empty) for domain to (any when empty).
bool crosses(const NetworkPlan &plan, const std::string &from, const std::string &to) {
  return std::ranges::any_of(plan.cuts, [&](const ClockCut &cut) {
    return (from.empty() || cut.from_domain == from) && (to.empty() || cut.to_domain == to);
  });
}
std::string labels(const NetworkPlan &plan, const std::vector<std::string> &keys) {
  std::string out;
  for (const auto &key : keys)
    out += (out.empty() ? "" : ", ") + plan.label(key);
  return out;
}
// The arbitration key of a switch output: "<req|rsp>/<node>><node|endpoint>/<output>".
std::string output_key(bool response, const SwitchOutputPlan &o) {
  return std::string(response ? "rsp/" : "req/") + o.node + ">" +
         (o.endpoint ? "endpoint/" : "node/") + o.output;
}
// The switch output at v.at (request outputs, then response ones) with its arbitration entry.
struct Overridden {
  std::string output;
  const OutputArbiterRequest *request = nullptr;
  const SwitchOutputPlan *o = nullptr;
  bool fixed() const { return request && request->policy == treadle::Policy::FixedPriority; }
};
Overridden overridden(const RuleView &v) {
  const auto &request = v.plan.exchange.request.outputs;
  const bool response = v.at >= request.size();
  const auto &o =
      response ? v.plan.exchange.response.outputs[v.at - request.size()] : request[v.at];
  const auto it = v.plan.output_arbitration.find(output_key(response, o));
  return {(o.endpoint ? "endpoint/" : "node/") + o.output,
          it == v.plan.output_arbitration.end() ? nullptr : &it->second, &o};
}
// The packet limits of the connected pair at v.at with an AXI4 source.
std::optional<std::tuple<std::string, std::string, PacketLimits>> packet_pair(const RuleView &v) {
  const auto &p = v.plan;
  const auto &pair = p.pairs[v.at];
  if (p.sources[pair.source].protocol != P::Axi4)
    return std::nullopt;
  return std::tuple{p.sources[pair.source].name, p.targets[pair.target].name, pair_limits(p, pair)};
}
// The configuration path of a key of the definition target named name.
std::string target_field(const Definition &def, const std::string &name, const char *key) {
  const auto i = std::ranges::find(def.targets, name, &Target::name) - def.targets.begin();
  return "targets[" + std::to_string(i) + "]." + key;
}
// The planned domains; a same-domain network has its one domain.
std::vector<DomainPlan> domains(const NetworkPlan &plan) {
  return plan.clocked() ? plan.domains : std::vector{plan.domain};
}
// The stages of every synchronizer chain: each crossing's and each resynchronized reset release.
std::set<unsigned> chain_stages(const NetworkPlan &plan) {
  std::set<unsigned> stages;
  for (const auto &d : domains(plan)) {
    if (d.release_stages)
      stages.insert(d.release_stages);
  }
  for (const auto &cut : plan.cuts)
    if (const auto *async = std::get_if<AsyncClockCut>(&cut.options))
      stages.insert(async->synchronizer_stages);
  return stages;
}
bool ladder(unsigned bytes) { return bytes == 4 || bytes == 8 || bytes == 16 || bytes == 32; }

constexpr CapabilityRule rules[] = {
    {"ahb5.master_width", "NCL_AHB5_PROPERTIES", Scope::Pair, Stage::Plan,
     [](const RuleView &v) {
       const auto &pair = v.plan.pairs[v.at];
       const auto &s = v.plan.sources[pair.source];
       const auto &t = v.plan.targets[pair.target];
       return when(t.ahb5 && t.hmaster_bits && t.hmaster_bits < s.hmaster_bits,
                   target_field(v.def, t.name, "ahb5.hmaster_width"),
                   "An AHB5 target HMASTER port must hold every reaching source HMASTER");
     }},
    {"qos.shaper_protocol", "NCL_QOS_PROFILE", Scope::Network, Stage::Definition,
     [](const RuleView &v) {
       const auto &list = v.def.initiators;
       const auto i = std::ranges::find_if(list, [](const Initiator &x) {
         const bool apb = (x.protocol == P::Apb4 || x.protocol == P::Apb3) && x.data_width == 32U;
         return x.qos.shaper && !apb && !full_axi(x.protocol);
       });
       return when(i != list.end(),
                   "initiators[" + std::to_string(i - list.begin()) + "].qos.shaper",
                   "A shaper needs a 32-bit apb4 or apb3, an axi4 or an axi3 "
                   "initiator");
     }},
    {"qos.scheduler_protocol", "NCL_QOS_PROFILE", Scope::Network, Stage::Definition,
     [](const RuleView &v) {
       const auto &list = v.def.initiators;
       const auto i = std::ranges::find_if(list, [](const Initiator &x) {
         const bool apb = (x.protocol == P::Apb4 || x.protocol == P::Apb3) && x.data_width == 32U;
         return x.qos.scheduler && !apb && !full_axi(x.protocol);
       });
       return when(i != list.end(),
                   "initiators[" + std::to_string(i - list.begin()) + "].qos.scheduler",
                   "A scheduler needs a 32-bit APB initiator or an AXI4 or AXI3 initiator");
     }},
    // A limiter or regulator charges an AXI burst once, from its one network packet: no burst
    // of the source may be cut (its largest INCR, up to 4 KiB, fits every packet window; no
    // boundary below 4 KiB, WRAP cut, narrow split, exact packets or width conversion).
    {"qos.whole_bursts", "NCL_QOS_PROFILE", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       const auto &p = v.plan;
       const auto cut = [&](const SourcePlan &s) {
         if (!s.qos.shaper || s.apb)
           return false;
         if (s.clamp || s.split_narrow)
           return true;
         const auto profile = packet_profile(p, static_cast<unsigned>(&s - p.sources.data()));
         const std::uint64_t largest = std::min<std::uint64_t>(
             4096, (std::uint64_t{1} << s.len_bits.value_or(8)) * (s.engine_bits / 8));
         return profile.decompose_wrap_bursts ||
                std::ranges::any_of(profile.windows, [&](const auto &w) {
                  return w.max_bytes < largest || w.boundary_bytes < 4096 || w.exact;
                });
       };
       const auto i = std::ranges::find_if(p.sources, cut);
       return when(i != p.sources.end(), "exchange",
                   "A shaper charges whole bursts, so no burst of AXI source " +
                       (i != p.sources.end() ? i->name : std::string{}) +
                       " may be cut into packets: raise the packet limits or narrow len_width");
     }},
    // A regulated APB source holds one transfer and has no pipes of its own to compensate.
    {"qos.scheduler_pipes", "NCL_QOS_PROFILE", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       const auto &list = v.plan.sources;
       const auto reaches = [](const SourcePlan &s) {
         return s.qos.picks() && s.apb &&
                std::ranges::any_of(s.pipes, [](const EndpointPipe &pipe) {
                  return pipe.name.ends_with("/pipe/packets") || pipe.name.ends_with("/pipe/issue");
                });
       };
       const auto i = std::ranges::find_if(list, reaches);
       return when(i != list.end(), "initiators[" + std::to_string(i - list.begin()) + "].pipes",
                   "A regulated APB source takes no packets or issue pipe");
     }},
    // Sources of any width and transport words of any width carry levels: link adapters pass
    // the pressure on (docs/scheduling.md#scheduling-pressure-rules). A target wider than the
    // network merges narrow bursts and is not in the profile.
    {"qos.endpoints", "NCL_QOS_PROFILE", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       const auto &p = v.plan;
       const unsigned bits = p.burst_data_bits;
       const auto axi_source = [&](const SourcePlan &s) {
         return (full_axi(s.external_protocol) || ahb_protocol(s.external_protocol)) &&
                s.protocol == P::Axi4 && !s.read_only && !s.write_only;
       };
       const auto axi_target = [&](const TargetPlan &t) {
         return full_axi(t.external_protocol) && t.protocol == P::Axi4 && t.data_bits == bits &&
                !t.read_only && !t.write_only && !t.timeout &&
                t.exclusive.mode == ExclusiveAccess::None;
       };
       const auto apb_source = [](const SourcePlan &s) { return s.apb && s.apb_bits == 32; };
       const auto apb_target = [](const TargetPlan &t) {
         return (t.protocol == P::Apb4 || t.protocol == P::Apb3 || t.protocol == P::Apb2) &&
                t.apb_bits == 32 && !t.timeout;
       };
       const bool axi =
           std::ranges::all_of(p.sources, axi_source) && std::ranges::all_of(p.targets, axi_target);
       const bool apb = std::ranges::all_of(p.sources, apb_source) &&
                        std::ranges::all_of(p.targets, apb_target) && !p.laddered;
       const auto ahb = std::ranges::find_if(
           p.targets, [](const TargetPlan &t) { return ahb_protocol(t.external_protocol); });
       const auto field =
           ahb == p.targets.end() ? "exchange" : target_field(v.def, ahb->name, "protocol");
       return when(
           p.levels() && !(axi || apb), field,
           "Scheduling levels require AXI4, AXI3, AHB-Lite or AHB5 sources to full AXI4 or AXI3 "
           "targets, or 32-bit APB endpoints, without targets wider than the transport, "
           "exclusive access or timeout. AXI graphs also exclude one-direction endpoints");
     }},
    {"arbitration.single_candidate", "NCL_ARBITRATION_OUTPUT", Scope::Output, Stage::Plan,
     [](const RuleView &v) {
       const auto x = overridden(v);
       return when(x.request && x.o->inputs.size() < 2,
                   x.request ? x.request->field + ".output" : "",
                   "Output " + v.plan.label(x.output) + " has a single candidate (" +
                       labels(v.plan, x.o->inputs) + ") and no arbiter");
     }},
    {"arbitration.priority_order", "NCL_ARBITRATION_PRIORITY", Scope::Output, Stage::Plan,
     [](const RuleView &v) {
       const auto x = overridden(v);
       const auto &in = x.o->inputs;
       const bool sized = x.fixed() && in.size() >= 2;
       const auto &order = sized ? x.request->priority : in;
       return when(order.size() != in.size() ||
                       std::set(order.begin(), order.end()) != std::set(in.begin(), in.end()),
                   sized ? x.request->field + ".priority" : "",
                   "The priority list must name every candidate of output " +
                       v.plan.label(x.output) + " exactly once: " + labels(v.plan, in));
     }},
    {"arbitration.output_exists", "NCL_ARBITRATION_OUTPUT", Scope::Override, Stage::Plan,
     [](const RuleView &v) {
       const auto &[key, request] =
           *std::next(v.plan.output_arbitration.begin(), static_cast<std::ptrdiff_t>(v.at));
       const bool response = key.starts_with("rsp/");
       const auto &outputs =
           (response ? v.plan.exchange.response : v.plan.exchange.request).outputs;
       return when(
           std::ranges::none_of(
               outputs, [&, &k = key](const auto &o) { return k == output_key(response, o); }),
           request.field + ".output", "The node has no such output");
     }},
    {"word.switch_ladder", "NCL_AXI4_EXCHANGE_PROFILE", Scope::Node, Stage::Plan,
     [](const RuleView &v) {
       const auto &node = v.plan.nodes[v.at];
       return when(!ladder(node.word_bytes), "exchange",
                   "Transport word width must be a serialization ladder member (4, 8, 16 or 32 "
                   "bytes): " +
                       node.id);
     }},
    {"word.link_ladder", "NCL_AXI4_EXCHANGE_PROFILE", Scope::Link, Stage::Plan,
     [](const RuleView &v) {
       const auto &word = v.plan.links[v.at].word;
       return when(word && !ladder(*word), "exchange",
                   "Invalid internal link word width or identity");
     }},
    {"target.whole_id", "NCL_AXI4_EXCHANGE_ID_WIDTH", Scope::Target, Stage::Plan,
     [](const RuleView &v) {
       const auto &p = v.plan;
       const auto &t = p.targets[v.at];
       const auto least = t.len_bits ? least_target_id_bits(t) : 0U;
       return when(least && t.id_bits.value() < least, target_field(v.def, t.name, "id_width"),
                   "Target " + t.name + " needs " + std::to_string(least) +
                       " ID bits: the flow and the owner flags of its exclusive accesses");
     }},
    {"pair.packet_limit_range", "NCL_AXI4_PACKETS", Scope::Pair, Stage::Plan,
     [](const RuleView &v) {
       const auto pair = packet_pair(v);
       if (!pair || std::get<2>(*pair).error.empty())
         return when(false, {}, {});
       const auto &[source, target, limits] = *pair;
       return when(true, v.plan.pairs[v.at].limit_field,
                   "Packet limit " + limits.error + " for source " + source + " and target " +
                       target);
     }},
    {"pair.packet_alignment", "NCL_AXI4_PACKETS", Scope::Pair, Stage::Plan,
     [](const RuleView &v) {
       const auto pair = packet_pair(v);
       if (!pair)
         return when(false, {}, {});
       const auto &[source, target, limits] = *pair;
       return when(limits.error.empty() && limits.boundary_bytes < limits.bytes, "exchange",
                   "Target " + target + " window is smaller than the " +
                       std::to_string(limits.bytes) + "-byte packet alignment word of source " +
                       source);
     }},
    {"clock.domain_owner", "NCL_EXCHANGE_CLOCK_ASSIGNMENT", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       std::set<std::string> used;
       if (v.plan.placement)
         std::ranges::transform(v.plan.placement->objects, std::inserter(used, used.end()),
                                [](const auto &object) { return object.second; });
       for (const auto &t : v.plan.targets)
         if (t.service && !t.service->domain.empty())
           used.insert(t.service->domain);
       return when(v.plan.placement && used.size() != v.plan.placement->domains.size(),
                   "exchange.clocks", "Declared clock domains must own an endpoint or switch");
     }},
    {"cdc.period_pair", "NCL_CDC_PERIOD_PARTIAL", Scope::Cut, Stage::Plan,
     [](const RuleView &v) {
       const auto &cut = v.plan.cuts[v.at];
       const auto &domains = v.plan.placement->domains;
       const auto a = std::ranges::find(domains, cut.from_domain, &DomainPlan::name);
       const auto z = std::ranges::find(domains, cut.to_domain, &DomainPlan::name);
       const auto timed = a->period_ps ? a : z, untimed = a->period_ps ? z : a;
       return when(!std::holds_alternative<EdgeAlignedCut>(cut.options) &&
                       !v.plan.explicit_depths.contains({cut.from_domain, cut.to_domain}) &&
                       a->period_ps.has_value() != z->period_ps.has_value(),
                   "clock_domains[" + std::to_string(timed - domains.begin()) + "].period_ps",
                   "Domain " + timed->name + " sets period_ps but its crossing peer " +
                       untimed->name + " does not; set both, neither, or an explicit depth for " +
                       cut.from_domain + " to " + cut.to_domain);
     }},
    {"cdc.crossing_used", "NCL_CDC_CROSSING_UNUSED", Scope::Crossing, Stage::Plan,
     [](const RuleView &v) {
       const auto &c = v.plan.placement->crossings[v.at];
       return when(!crosses(v.plan, c.from_domain, c.to_domain),
                   "clock_crossings[" + std::to_string(v.at) + "]",
                   "No graph segment crosses from " + c.from_domain + " to " + c.to_domain);
     }},
    {"cdc.reset_handshake_ports", "NCL_RESET_PORT", Scope::Crossing, Stage::Plan,
     [](const RuleView &v) {
       const auto &crossing = v.plan.placement->crossings[v.at];
       const auto *async = std::get_if<AsyncClockCrossing>(&crossing.options);
       return when(async && async->reset_handshake.value_or(false) && !v.plan.reset_handshake,
                   "clock_crossings[" + std::to_string(v.at) + "].reset_handshake",
                   "A reset handshake needs two or more reset ports");
     }},
    {"cdc.edge_aligned_relation", "NCL_CDC_EDGE_ALIGNED_RELATION", Scope::Crossing, Stage::Plan,
     [](const RuleView &v) {
       const auto &crossing = v.plan.placement->crossings[v.at];
       if (!std::holds_alternative<EdgeAlignedCrossing>(crossing.options))
         return when(false, {}, {});
       const auto &domains = v.plan.domains;
       const auto &from = *std::ranges::find(domains, crossing.from_domain, &DomainPlan::name);
       const auto &to = *std::ranges::find(domains, crossing.to_domain, &DomainPlan::name);
       const bool from_slow =
           from.edge_ratio > to.edge_ratio ||
           (from.edge_ratio == to.edge_ratio && from.type == ClockType::InternalDivider &&
            to.type == ClockType::Direct);
       const auto &slow = from_slow ? from : to;
       const auto &fast = from_slow ? to : from;
       auto ancestor = slow.name;
       while (!ancestor.empty() && ancestor != fast.name)
         ancestor = std::ranges::find(domains, ancestor, &DomainPlan::name)->edge_parent;
       return when(ancestor != fast.name || slow.edge_ratio % fast.edge_ratio != 0,
                   "clock_crossings[" + std::to_string(v.at) + "]",
                   "EdgeAligned requires one domain to be a statically divided ancestor of "
                   "the other");
     }},
    {"cdc.default_stages_used", "NCL_CDC_CROSSING_UNUSED", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       return when(v.plan.placement && v.plan.placement->synchronizer_stages &&
                       std::ranges::all_of(
                           v.plan.cuts,
                           [&](const ClockCut &c) {
                             return !std::holds_alternative<AsyncClockCut>(c.options) ||
                                    v.plan.explicit_stages.contains({c.from_domain, c.to_domain});
                           }),
                   "synchronizer_stages", "No crossing takes the default synchronizer stages");
     }},
    {"cdc.period_used", "NCL_CDC_PERIOD_UNUSED", Scope::Domain, Stage::Plan,
     [](const RuleView &v) {
       const auto &d = v.plan.placement->domains[v.at];
       return when(d.period_ps && !crosses(v.plan, d.name, "") && !crosses(v.plan, "", d.name),
                   "clock_domains[" + std::to_string(v.at) + "].period_ps",
                   "Domain " + d.name + " has no crossing");
     }},
    // A bound cell serves at least one wrapper instance; an integrated synchronizer every chain.
    {"cell.gate_used", "NCL_DOMAIN_REFERENCE", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       const auto divided = [](const DomainPlan &d) {
         return d.type == ClockType::InternalDivider || d.type == ClockType::EdgeQualified;
       };
       return when(v.def.cells.clock_gate &&
                       (v.def.clock_gating == ClockGating::None ||
                        std::ranges::none_of(domains(v.plan), &DomainPlan::gated)) &&
                       std::ranges::none_of(domains(v.plan), divided),
                   "cells.clock_gate", "No clock domain is gated or divided");
     }},
    {"cell.sync_used", "NCL_DOMAIN_REFERENCE", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       return when(v.def.cells.synchronizer && chain_stages(v.plan).empty(), "cells.synchronizer",
                   "No synchronizer chain is generated");
     }},
    {"cell.sync_stages", "NCL_CDC_STAGES_UNSUPPORTED", Scope::Network, Stage::Plan,
     [](const RuleView &v) {
       const auto &cell = v.def.cells.synchronizer;
       const auto chains = chain_stages(v.plan);
       return when(cell && cell->stages > 1 && !chains.empty() && chains != std::set{cell->stages},
                   "cells.synchronizer.stages",
                   "An integrated synchronizer serves only chains of its own stages");
     }},
};

// The element count of each scope, in Scope order. Definition rules see the definition targets
// in their order and no other planned element.
std::size_t elements(Scope scope, const Definition &def, const NetworkPlan &p, Stage stage) {
  if (stage == Stage::Definition && scope == Scope::Target)
    return def.targets.size();
  const auto *placed = p.placement ? &*p.placement : nullptr;
  const std::size_t counts[] = {1,
                                placed ? placed->domains.size() : 0,
                                placed ? placed->crossings.size() : 0,
                                p.targets.size(),
                                p.pairs.size(),
                                p.exchange.request.outputs.size() +
                                    p.exchange.response.outputs.size(),
                                p.output_arbitration.size(),
                                p.nodes.size(),
                                p.links.size(),
                                placed ? p.cuts.size() : 0};
  return counts[static_cast<std::size_t>(scope)];
}
} // namespace

const ProtocolCaps &protocol_caps(Protocol protocol) {
  // A value outside the enumeration is neither an initiator nor a target protocol.
  static constexpr ProtocolCaps unknown{P::Axi4, false, false, wide,  wide,  true,
                                        true,    true,  false, false, false, 1};
  const auto *it = std::ranges::find(caps, protocol, &ProtocolCaps::protocol);
  return it == std::end(caps) ? unknown : *it;
}
std::span<const CapabilityRule> capability_rules() { return rules; }

std::vector<Diagnostic> check_capabilities(const Definition &def, const NetworkPlan *planned) {
  static const NetworkPlan unplanned;
  const auto &plan = planned ? *planned : unplanned;
  const auto stage = planned ? Stage::Plan : Stage::Definition;
  std::vector<Diagnostic> out;
  std::set<std::tuple<std::string, std::string, std::string>> seen;
  for (const auto &rule : rules)
    for (std::size_t at = 0; rule.stage == stage && at < elements(rule.scope, def, plan, stage);
         ++at)
      if (auto finding = rule.check({def, plan, at});
          finding && seen.emplace(rule.code, finding->field, finding->message).second)
        out.push_back(
            error(std::string(rule.code), std::move(finding->field), std::move(finding->message)));
  return out;
}
} // namespace nocloom::detail
