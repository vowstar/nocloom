#include "exchange_composition.hpp"
#include "ahb_outlet.hpp"
#include "axi4_initiator.hpp"
#include "axi4_outlet.hpp"
#include "beat_stage.hpp"
#include "capability.hpp"
#include "clock_gating.hpp"
#include "diagnostic.hpp"
#include "exclusive_monitor.hpp"
#include "fifo_levels.hpp"
#include "ir_component.hpp"
#include "link_pipeline.hpp"
#include "network_plan_profiles.hpp"
#include "piece_count.hpp"
#include "qos_pipeline.hpp"
#include "rate_limiter.hpp"
#include "related_clock.hpp"
#include "treadle.hpp"
#include "width_adapter.hpp"
#include <algorithm>
#include <bit>
#include <map>
#include <memory>
#include <numeric>
#include <optional>
#include <ranges>
#include <set>
#include <span>
#include <stdexcept>
#include <tuple>

namespace nocloom::detail {
namespace {
// The stages of one link in data flow order: the sender pipe, the 2:1 adapter chain to the
// crossing word (explicit, else edge_word) in the sender domain, the crossing FIFO between two
// domains, then the chain from the crossing word and the receiver pipe in the receiver domain.
// Equal widths derive no adapter; a word off the ladder is a capability violation and derives
// none either.
void link_stages(LinkPlan &link) {
  auto &stages = link.stages;
  auto pipe = [&](LinkPipeline mode, const std::string &name, const std::string &domain,
                  unsigned bytes) {
    if (mode.forward || mode.backward)
      stages.push_back(
          {StageKind::Pipe, name, domain, {}, link.response, false, bytes, bytes, mode});
  };
  auto chain = [&](const std::string &segment, const std::string &domain, unsigned width,
                   unsigned to) {
    // A response word widens in one step (response assembler, message.hpp point 18).
    if (link.response && width < to && std::has_single_bit(width) && std::has_single_bit(to)) {
      stages.push_back({StageKind::Adapter, "adapter/" + segment + "/0", domain, segment, true,
                        false, to, width});
      return;
    }
    for (unsigned hop = 0; std::has_single_bit(width) && std::has_single_bit(to) && width != to;) {
      const bool shrink = width > to;
      const unsigned next = shrink ? width / 2 : width * 2;
      stages.push_back({StageKind::Adapter, "adapter/" + segment + "/" + std::to_string(hop++),
                        domain, segment, link.response, shrink, std::max(width, next),
                        std::min(width, next)});
      width = next;
    }
  };
  stages.clear();
  pipe(link.out_pipe, "pipeline/" + link.name + "/out", link.from_domain, link.from_bytes);
  chain(link.name + (link.word ? "/link-in" : ""), link.from_domain, link.from_bytes,
        link.crossing_bytes());
  if (link.from_domain != link.to_domain)
    stages.push_back({StageKind::Crossing, "cut/" + link.name, link.to_domain, {}});
  chain(link.name + "/link-out", link.to_domain, link.crossing_bytes(), link.to_bytes);
  pipe(link.in_pipe, "pipeline/" + link.name, link.to_domain, link.to_bytes);
}
// Every link of both directions in graph order (entry attachments, switch edges, exit
// attachments), each switch edge with the word and pipe of its configuration, and its stages.
void plan_links(NetworkPlan &plan) {
  auto edges = std::exchange(plan.links, {});
  // A placed object: its name, its word and its domain.
  auto object = [&](const std::string &kind, const std::string &name) {
    const bool node = kind == "req/" || kind == "rsp/";
    return std::tuple{kind + name, node ? plan.node(name).word_bytes : endpoint_word(plan, name),
                      plan.placement ? plan.placement->objects.at(kind + name) : plan.domain.name};
  };
  // The period of a placed domain; the single domain of an unplaced network takes none.
  auto period = [&](const std::string &domain) {
    return plan.placement
               ? std::ranges::find(plan.placement->domains, domain, &DomainPlan::name)->period_ps
               : std::nullopt;
  };
  for (bool response : {false, true}) {
    const auto &g = (response ? plan.exchange.response : plan.exchange.request).graph;
    const std::string prefix = response ? "rsp/" : "req/";
    const std::string entry = response ? "target/" : "source/";
    const std::string exit = response ? "source/" : "target/";
    auto add = [&](const std::string &from_kind, const std::string &from,
                   const std::string &to_kind, const std::string &to) {
      const auto name = prefix + from + ">" + to;
      const auto edge = std::ranges::find(edges, name, &LinkPlan::name);
      auto &link = edge == edges.end() ? plan.links.emplace_back() : plan.links.emplace_back(*edge);
      link.name = name;
      link.response = response;
      std::tie(link.from, link.from_bytes, link.from_domain) = object(from_kind, from);
      std::tie(link.to, link.to_bytes, link.to_domain) = object(to_kind, to);
      link.edge_bytes = edge_word(link.from_bytes, link.to_bytes, period(link.from_domain),
                                  period(link.to_domain));
      link_stages(link);
    };
    for (const auto &a : g.entries)
      add(entry, a.endpoint, prefix, a.node);
    for (const auto &e : g.edges)
      add(prefix, e.from, prefix, e.to);
    for (const auto &a : g.exits)
      add(prefix, a.node, exit, a.endpoint);
  }
}
// A component the plan admits; a failed lowering is a composition fault.
NodeHardware admitted(Result<NodeHardware> lowered) {
  if (!lowered)
    throw std::logic_error(lowered.diagnostics().front().message);
  return std::move(lowered).value();
}
// The field ports of an AXI channel (aw, w, ar, r or b) of a component, in port order.
std::vector<Port> channel_fields(const NodeHardware &component, std::string_view channel) {
  std::vector<Port> fields;
  for (const auto &port : component.ir.ports)
    if (const std::string_view rest = port.signal;
        port.endpoint == "axi" && rest.starts_with(channel) && !rest.ends_with("valid") &&
        !rest.ends_with("ready"))
      fields.push_back(port);
  return fields;
}
// The place of an endpoint pipe: the part of its name after /pipe/.
std::string_view pipe_place(const EndpointPipe &pipe) {
  return std::string_view(pipe.name).substr(pipe.name.rfind("/pipe/") + 6);
}
bool socket_channel(std::string_view place) {
  return place == "aw" || place == "w" || place == "ar" || place == "r" || place == "b";
}
// The slot payload of every endpoint pipe: the fields of an AXI channel at the port (none when
// the lowering fails; the composition reports it), or a transport fragment at the endpoint word
// with the burst-final marker on a direction before the merge.
void plan_endpoint_pipes(NetworkPlan &plan) {
  const auto burst = transport_burst(plan);
  auto plan_pipes = [&](std::vector<EndpointPipe> &pipes, const std::string &endpoint, auto lower) {
    std::optional<Result<NodeHardware>> face;
    for (auto &pipe : pipes) {
      if (const auto place = pipe_place(pipe); socket_channel(place)) {
        if (!face)
          face.emplace(lower());
        for (const auto &port : *face ? channel_fields(face->value(), place) : std::vector<Port>{})
          pipe.bits += port.width;
      } else {
        for (const auto &[name, width] : fragment_fields(
                 place == "response", endpoint_word(plan, endpoint), plan.source_bits, burst))
          pipe.bits += width;
        pipe.bits += place.starts_with("packets/");
      }
    }
  };
  for (unsigned i = 0; i < plan.sources.size(); ++i)
    plan_pipes(plan.sources[i].pipes, plan.sources[i].name,
               [&] { return lower_axi4_initiator(initiator_profile(plan, i)); });
  for (unsigned i = 0; i < plan.targets.size(); ++i)
    plan_pipes(plan.targets[i].pipes, plan.targets[i].name,
               [&] { return lower_axi4_outlet(axi4_outlet_profile(plan, i)); });
}
struct Channel {
  ValueId valid, ready;
  std::vector<ValueId> data;
  // The pressure sideband (message.hpp point 15) of a channel of a network with levels.
  std::optional<ValueId> level_hint = std::nullopt;
};
// The outputs that routes toward one destination leave a switch input by, each with the entry and
// the route path (message.hpp point 14: a request's flow, a response's return path) of those
// routes.
using Leaving = std::map<std::string, std::set<std::pair<std::string, unsigned>>>;
class Compose {
public:
  Builder b;
  ExchangeHardware out;
  ValueId clock{}, reset{};
  std::set<std::size_t> driven_wires;
  // Per domain: its clock and the reset of its flip-flops.
  std::map<std::string, std::pair<ValueId, ValueId>> domains;
  // Clock gating: the region of every switch and endpoint zone of a gated domain, each domain's
  // test input and the object whose region produces each channel valid.
  std::map<std::string, GateRegion> regions;
  std::map<std::string, ValueId> tests;
  std::map<std::string, ValueId> ungates;
  std::map<std::string, ValueId> bypasses;
  std::map<std::size_t, std::string> producers;

  // The region of an object of a gated domain: a switch (req/<id>, rsp/<id>), an endpoint's
  // transport zone (source/<name>, target/<name>) or its protocol zone (<endpoint>/protocol).
  GateRegion *region(const std::string &object) {
    const auto &p = out.plan;
    if (p.clock_gating == ClockGating::None || object.empty())
      return nullptr;
    const auto owner = gate_owner(object);
    const auto domain =
        p.clocked() ? *std::ranges::find(p.domains, p.object_domains.at(owner), &DomainPlan::name)
                    : p.domain;
    if (!domain.gated ||
        (object != owner && p.endpoint_gating(owner) == EndpointClockGating::TransportOnly))
      return nullptr;
    auto &r = regions[object];
    r.domain = domain.name;
    return &r;
  }
  // A region component reads a channel: a valid from outside the region wakes it.
  void consume(GateRegion *r, const std::string &object, ValueId valid) {
    const auto from = producers.find(valid.index);
    if (r && (from == producers.end() || from->second != object) &&
        std::ranges::none_of(r->wake, [&](ValueId w) { return w.index == valid.index; }))
      r->wake.push_back(valid);
  }
  // Each gated region's gate, once the network is complete.
  void gate_regions() {
    for (auto it = regions.begin(); it != regions.end();) {
      const auto owner = gate_owner(it->first);
      if (owner == it->first || out.plan.endpoint_gating(owner) != EndpointClockGating::Shared) {
        ++it;
        continue;
      }
      auto &combined = regions[owner];
      combined.domain = it->second.domain;
      combined.states.insert(combined.states.end(), it->second.states.begin(),
                             it->second.states.end());
      combined.idle.insert(combined.idle.end(), it->second.idle.begin(), it->second.idle.end());
      combined.wake.insert(combined.wake.end(), it->second.wake.begin(), it->second.wake.end());
      it = regions.erase(it);
    }
    for (const auto &[object, r] : regions) {
      if (r.states.empty())
        continue;
      const auto [clk, rst] = domains.at(r.domain);
      const auto ungate = ungates.find(r.domain);
      const auto g =
          lower_region_gate(b, clk, rst, tests.at(r.domain),
                            ungate != ungates.end() ? std::optional{ungate->second} : std::nullopt,
                            r, b.ir.synchronous_reset);
      const auto id = "gate/" + object;
      for (const auto &[key, value] : {std::pair{".clock", g.clock}, std::pair{".enable", g.enable},
                                       std::pair{".idle", g.idle}, std::pair{".wake", g.wake},
                                       std::pair{".remaining", g.remaining}})
        bind(id + key, value);
    }
  }

  void select_object(const std::string &object) {
    if (out.plan.clocked())
      std::tie(clock, reset) = domains.at(out.plan.object_domains.at(object));
  }
  std::vector<ValueId> sync_chain(const std::string &id, ValueId source, unsigned count,
                                  ValueId target_clock, std::optional<ValueId> with_reset = {}) {
    const auto cleared = with_reset.value_or(reset);
    std::vector<ValueId> chain;
    for (unsigned i = 0; i < count; ++i) {
      auto q = b.state(1, target_clock, cleared);
      b.update(q, source);
      b.ir.synchronizer_stages.push_back(q);
      bind(id + "/stage" + decimal(i), q);
      chain.push_back(q);
      source = q;
    }
    out.storage.push_back({id, target_clock, {}, chain, bit(0), chain.back()});
    return chain;
  }
  // The domain's clock port and its reset: the reset port itself, or the port released through
  // release_stages registers on the domain clock port that shift in one. Domains share a reset
  // port. Test and ungate ports no domain names follow as inputs without load.
  void add_domains(const std::vector<DomainPlan> &plans) {
    std::map<std::string, ValueId> ports, root_inputs;
    auto input_of = [&](const std::string &name, const std::string &endpoint,
                        const std::string &signal) {
      auto [port, fresh] = ports.try_emplace(endpoint, ValueId{});
      if (fresh)
        port->second = b.input(name, 1, endpoint, signal);
      return port->second;
    };
    for (const auto &d : plans) {
      const auto id = "domain/" + d.name;
      auto input = ValueId{};
      if (d.clock_root.empty()) {
        input = b.input(d.clock_port, 1, "domain:" + d.name, "clock");
      } else {
        auto [root, fresh] = root_inputs.try_emplace(d.clock_root, ValueId{});
        if (fresh)
          root->second = b.input(d.clock_port, 1, "domain:" + d.name, "clock");
        input = root->second;
      }
      reset = input_of(d.reset_port, "reset:" + d.reset_port, "reset_n");
      // The test port drives the domain's clock gates, the ungate port holds them open.
      if (!d.test_port.empty())
        tests[d.name] = input_of(d.test_port, "test:" + d.test_port, "test");
      if (!d.ungate_port.empty())
        ungates[d.name] = input_of(d.ungate_port, "ungate:" + d.ungate_port, "ungate");
      bind(id + ".reset_port", reset);
      if (d.release_stages)
        reset = sync_chain(id + "/init", bit(1), d.release_stages, input).back();
      bind(id + ".reset", reset);
      // A divided clock: the divider gate opens for one edge of the port every ratio edges
      // (the internal divider's counter) or where the enable port marks a valid edge.
      clock = input;
      if (d.type == ClockType::InternalDivider || d.type == ClockType::EdgeQualified) {
        const auto test = d.test_port.empty() ? bit(0) : tests.at(d.name);
        ValueId open;
        if (d.type == ClockType::InternalDivider) {
          const unsigned width = std::max<unsigned>(1, std::bit_width(d.ratio - 1));
          const auto count = b.state(width, input, reset, d.ratio - 1);
          open = eq(count, 0);
          b.update(count, b.select(open, b.constant(width, d.ratio - 1),
                                   b.binary(Op::Subtract, count, b.constant(width, 1))));
          bind(id + "/divider.count", count);
          bind(id + "/divider.enable", open);
          out.storage.push_back({id + "/divider", input, {}, {count}, bit(0), bit(0)});
        } else {
          open = input_of(d.enable_port, "enable:" + d.enable_port, "enable");
        }
        // With synchronous register reset the domain reset low also opens it (see
        // lower_region_gate).
        if (b.ir.synchronous_reset)
          open = or_(open, b.unary(Op::BitNot, reset, 1));
        const auto ungate = ungates.find(d.name);
        clock =
            b.clock_gate(input, ungate != ungates.end() ? or_(open, ungate->second) : open, test);
      }
      bind(id + ".clock", clock);
      domains[d.name] = {clock, reset};
    }
    for (const auto &t : out.plan.test_ports)
      input_of(t.name, "test:" + t.name, "test");
    for (const auto &t : out.plan.ungate_ports)
      input_of(t.name, "ungate:" + t.name, "ungate");
  }
  // The clock adapter cut/<link> from a channel of the given word, between the link's ends.
  Channel cross_fragment(const std::string &name, unsigned bytes, const Channel &input,
                         const std::array<std::string, 2> &ends) {
    const auto &placement = out.plan;
    const auto cut = std::ranges::find(placement.cuts, name, &ClockCut::name);
    if (cut == placement.cuts.end())
      throw std::logic_error("Missing planned crossing " + name);
    if (!std::holds_alternative<AsyncClockCut>(cut->options))
      return cross_related(*cut, bytes, input, ends);
    return cross_async(*cut, burst_fragment_widths(cut->segment.starts_with("rsp/"), bytes), input,
                       ends);
  }
  // The clock adapter of an asynchronous cut, its output of the given field widths.
  Channel cross_async(const ClockCut &planned, const std::vector<unsigned> &widths,
                      const Channel &input, const std::array<std::string, 2> &ends) {
    const auto *cut = &planned;
    const auto &placement = out.plan;
    const auto &options = std::get<AsyncClockCut>(cut->options);
    const auto from = std::ranges::find(placement.domains, cut->from_domain, &DomainPlan::name);
    const auto to = std::ranges::find(placement.domains, cut->to_domain, &DomainPlan::name);
    auto built =
        lower_async_fifo(*from, *to, options.depth.depth, options.synchronizer_stages, cut->fields,
                         options.reset_handshake, placement.reset_coverage == ResetCoverage::All);
    if (!built)
      throw std::logic_error("Cannot lower planned crossing FIFO");
    const auto &f = built.value();
    IrComponent imported(b, f.ir);
    for (const auto &domain : f.domains) {
      imported.replace(domain.clock, domains.at(domain.plan.name).first);
      imported.replace(domain.reset, domains.at(domain.plan.name).second);
    }
    auto output = channel(widths);
    imported.replace(f.valid, input.valid);
    imported.replace(f.out_ready, output.ready);
    for (std::size_t i = 0; i < f.fields.size(); ++i)
      imported.replace(f.fields[i].input, input.data.at(i));
    // Preserve the complete semantic import map so the verifier can compare
    // the actual lowered FIFO against the independently rebuilt component.
    for (std::size_t i = 0; i < f.ir.values.size(); ++i)
      bind(cut->name + "/ir/" + decimal(i), imported.get(ValueId{i}));
    imported.finish();
    drive(input.ready, imported.get(f.ready));
    drive(output.valid, imported.get(f.output_valid));
    std::vector<ValueId> slots, received;
    std::vector<ValueId> sender{imported.get(f.write_index), imported.get(f.write_count)};
    std::vector<ValueId> receiver{imported.get(f.read_index), imported.get(f.read_count),
                                  imported.get(f.out_valid)};
    for (std::size_t i = 0; i < f.fields.size(); ++i) {
      const auto &field = f.fields[i];
      drive(output.data[i], imported.get(field.output));
      std::ranges::transform(field.storage, std::back_inserter(slots),
                             [&](ValueId slot) { return imported.get(slot); });
      received.push_back(imported.get(field.received));
    }
    for (auto q : f.read_count_sync) {
      sender.push_back(imported.get(q));
      b.ir.synchronizer_stages.push_back(imported.get(q));
    }
    for (auto q : f.write_count_sync) {
      receiver.push_back(imported.get(q));
      b.ir.synchronizer_stages.push_back(imported.get(q));
    }
    // Handshake registers follow: announce, on, wait, then both chains.
    for (unsigned d = 0; f.reset_handshake && d < 2; ++d) {
      const auto &h = f.handshake[d];
      auto &side = d ? receiver : sender;
      for (auto q : {h.announce, h.on, h.wait})
        side.push_back(imported.get(q));
      for (const auto *chain : {&h.peer_sync, &h.ack_sync})
        for (auto q : *chain) {
          side.push_back(imported.get(q));
          b.ir.synchronizer_stages.push_back(imported.get(q));
        }
    }
    // Slots are acquired by a write and released by the receiving load; the
    // receive register is acquired by that load and released when drained.
    const auto read = imported.get(f.read);
    const auto drained = and_(imported.get(f.out_valid), output.ready);
    out.storage.push_back(
        {cut->name, domains.at(from->name).first, slots, sender, imported.get(f.write), read});
    out.storage.push_back(
        {cut->name + "/receive", domains.at(to->name).first, received, receiver, read, drained});
    for (const auto &[key, value] :
         {std::pair{"/write", f.write}, std::pair{"/read", f.read},
          std::pair{"/write_count", f.write_count}, std::pair{"/read_count", f.read_count}})
      bind(cut->name + key, imported.get(value));
    gate_crossing(f, imported, input.valid, ends);
    if (input.level_hint)
      output.level_hint = cross_level_hint(*cut, f, imported, input, output, ends);
    producers[output.valid.index] = ends[1];
    bind(cut->name + "/input.valid", input.valid);
    bind(cut->name + "/input.ready", input.ready);
    bind(cut->name + "/output.valid", output.valid);
    bind(cut->name + "/output.ready", output.ready);
    for (std::size_t i = 0; i < input.data.size(); ++i) {
      bind(cut->name + "/input.field" + decimal(i), input.data[i]);
      bind(cut->name + "/output.field" + decimal(i), output.data[i]);
    }
    return output;
  }

  Channel cross_related(const ClockCut &cut, unsigned bytes, const Channel &input,
                        const std::array<std::string, 2> &ends) {
    const auto from = std::ranges::find(out.plan.domains, cut.from_domain, &DomainPlan::name);
    const auto to = std::ranges::find(out.plan.domains, cut.to_domain, &DomainPlan::name);
    const bool edge_aligned = std::holds_alternative<EdgeAlignedCut>(cut.options);
    Result<RelatedAdapter> built =
        edge_aligned
            ? lower_edge_aligned(*from, *to, std::get<EdgeAlignedCut>(cut.options).slow_from_sender,
                                 std::get<EdgeAlignedCut>(cut.options).slow_pipeline, cut.fields,
                                 out.plan.reset_coverage == ResetCoverage::All)
            : lower_related_sync(*from, *to, std::get<SyncClockCut>(cut.options).depth.depth,
                                 std::get<SyncClockCut>(cut.options).fast, cut.fields,
                                 out.plan.reset_coverage == ResetCoverage::All);
    if (!built)
      throw std::logic_error("Cannot lower planned related-clock adapter");
    const auto &f = built.value();
    IrComponent imported(b, f.ir);
    for (const auto &domain : f.domains) {
      imported.replace(domain.clock, domains.at(domain.plan.name).first);
      imported.replace(domain.reset, domains.at(domain.plan.name).second);
    }
    if (edge_aligned) {
      const auto &slow = f.slow_from_sender ? *from : *to;
      imported.replace(f.slow_enable,
                       slow.type == ClockType::InternalDivider
                           ? eq(out.bindings.at("domain/" + slow.name + "/divider.count"), 0)
                           : bit(1));
    }
    auto output = channel(burst_fragment_widths(cut.segment.starts_with("rsp/"), bytes));
    imported.replace(f.valid, input.valid);
    imported.replace(f.out_ready, output.ready);
    auto input_data = input.data;
    if (f.fields.size() == input_data.size() + 1) {
      const auto head = head_pressure(input.valid, input.data, cut.segment.starts_with("rsp/"));
      input_data.push_back(input.level_hint ? or_(*input.level_hint, head) : head);
    }
    if (input_data.size() != f.fields.size())
      throw std::logic_error("Related-clock field bundle differs from its channel");
    for (std::size_t i = 0; i < f.fields.size(); ++i)
      imported.replace(f.fields[i].input, input_data[i]);
    for (std::size_t i = 0; i < f.ir.values.size(); ++i)
      bind(cut.name + "/ir/" + decimal(i), imported.get(ValueId{i}));
    imported.finish();
    drive(input.ready, imported.get(f.ready));
    drive(output.valid, imported.get(f.output_valid));
    for (std::size_t i = 0; i < output.data.size(); ++i)
      drive(output.data[i], imported.get(f.fields[i].output));
    if (input.level_hint)
      output.level_hint = imported.get(f.fields.back().output);
    std::vector<ValueId> slots, received;
    const auto append = [&](const std::vector<ValueId> &source, std::vector<ValueId> &destination) {
      std::ranges::transform(source, std::back_inserter(destination),
                             [&](ValueId id) { return imported.get(id); });
    };
    for (const auto &field : f.fields)
      append(field.storage, slots);
    std::vector<ValueId> sender, receiver;
    if (edge_aligned) {
      if (f.slow_from_sender)
        receiver.push_back(imported.get(f.admission_open));
      else if (!f.slow_pipeline)
        sender.push_back(imported.get(f.admission_open));
      if (f.slow_from_sender) {
        append(f.pipeline_payload, slots);
        append(f.pipeline_control, sender);
      } else {
        append(f.pipeline_payload, received);
        append(f.pipeline_control, receiver);
      }
      if (f.admission_open.index != 0)
        bind(cut.name + "/admission_open", imported.get(f.admission_open));
    } else {
      sender = {imported.get(f.write_index), imported.get(f.write_count)};
      receiver = {imported.get(f.read_index), imported.get(f.read_count)};
      append(f.read_count_sync, sender);
      append(f.write_count_sync, receiver);
      for (const auto &[key, value] :
           {std::pair{"/write_count", f.write_count}, std::pair{"/read_count", f.read_count},
            std::pair{"/read_count_return", f.returned_read_count()},
            std::pair{"/write_count_return", f.returned_write_count()}})
        bind(cut.name + key, imported.get(value));
    }
    const auto write = imported.get(f.write), read = imported.get(f.read);
    out.storage.push_back({cut.name, domains.at(from->name).first, slots, sender, write, read});
    out.storage.push_back(
        {cut.name + "/receive", domains.at(to->name).first, received, receiver, read, read});
    bind(cut.name + "/write", write);
    bind(cut.name + "/read", read);
    if (auto *r = region(ends[0]))
      consume(r, ends[0], input.valid);
    if (auto *r = region(ends[1]))
      r->wake.push_back(imported.get(f.output_valid));
    producers[output.valid.index] = ends[1];
    bind(cut.name + "/input.valid", input.valid);
    bind(cut.name + "/input.ready", input.ready);
    bind(cut.name + "/output.valid", output.valid);
    bind(cut.name + "/output.ready", output.ready);
    for (std::size_t i = 0; i < input_data.size(); ++i) {
      bind(cut.name + "/input.field" + decimal(i), input_data[i]);
      bind(cut.name + "/output.field" + decimal(i),
           i < output.data.size() ? output.data[i] : imported.get(f.fields[i].output));
    }
    return output;
  }

  // Slot levels are visible only inside the confirmed FIFO interval.
  ValueId cross_level_hint(const ClockCut &cut, const AsyncFifo &f, IrComponent &imported,
                           const Channel &input, const Channel &output,
                           const std::array<std::string, 2> &ends) {
    const bool response = cut.segment.starts_with("rsp/");
    const auto [sender, sender_reset] = domains.at(cut.from_domain);
    const auto [receiver, receiver_reset] = domains.at(cut.to_domain);
    const auto id = "level_hint/" + cut.name + "/";
    const auto owned = [&](const std::string &name, unsigned width, ValueId clk, ValueId rst) {
      const auto q = b.state(width, clk, rst);
      bind(id + name, q);
      return q;
    };
    const auto word = fragment_word(input.data, response);
    const auto level = [&](const std::vector<ValueId> &data) {
      return data[burst_field_index(response, word, "level")];
    };
    const auto first = [&](const std::vector<ValueId> &data) {
      return data[burst_field_index(response, word, "first")];
    };
    const auto at = [&](ValueId x, unsigned i) { return eq(x, i); };
    // Sender: the pressure register and the slot levels.
    const std::size_t sent_first = b.ir.states.size();
    const auto pressed = owned("send", level_hint_width(), sender, sender_reset);
    const PressurePath path{b, level_hint_width()};
    b.update(pressed, path.join({{*input.level_hint, bit(1)},
                                 {head_pressure(input.valid, input.data, response), bit(1)}}));
    const auto write = imported.get(f.write), write_index = imported.get(f.write_index);
    std::vector<ValueId> slots;
    for (unsigned i = 0; i < f.depth; ++i) {
      slots.push_back(owned("slot" + decimal(i), 3, sender, sender_reset));
      b.update(slots[i], b.select(and_(write, at(write_index, i)),
                                  b.select(first(input.data), level(input.data), b.constant(3, 0)),
                                  slots[i]));
    }
    const std::size_t sent_last = b.ir.states.size();
    // Receiver: the synchronized pressure as a thermometer.
    auto synced = pressed;
    for (unsigned k = 0; k < std::get<AsyncClockCut>(cut.options).synchronizer_stages; ++k) {
      const auto q = owned("sync" + decimal(k), level_hint_width(), receiver, receiver_reset);
      b.update(q, synced);
      b.ir.synchronizer_stages.push_back(q);
      synced = q;
    }
    const auto thermometer = path.encode(path.rank(synced));
    const std::size_t received_first = b.ir.states.size();
    const auto enabled = f.reset_handshake ? imported.get(f.handshake[1].enable) : bit(1);
    const auto levels =
        lower_fifo_levels(b, imported.get(f.write_count_sync.back()), imported.get(f.read_count),
                          imported.get(f.read_index), enabled, slots);
    const auto interval = "fifo_levels/" + cut.name + "/";
    bind(interval + "count", levels.count);
    bind(interval + "queue_level", levels.level);
    for (unsigned i = 0; i < f.depth; ++i)
      bind(interval + "active" + decimal(i), levels.active[i]);
    const auto queue =
        path.join({{thermometer, imported.get(f.nonempty)}, {path.encode(levels.level), bit(1)}});
    // The forward register: a forward pipe stage behind the queue head.
    const auto out_valid = imported.get(f.out_valid);
    const auto loads = or_(not_(out_valid), output.ready);
    const auto stalled = owned("forward", level_hint_width(), receiver, receiver_reset);
    b.update(stalled, path.mask(path.join({{stalled, bit(1)}, {queue, bit(1)}}), not_(loads)));
    const std::size_t received_last = b.ir.states.size();
    // Each side's registers but the synchronizer stages join its gate region.
    for (unsigned d = 0; d < 2; ++d)
      if (auto *r = region(ends[d]))
        for (auto i = d ? received_first : sent_first; i < (d ? received_last : sent_last); ++i)
          r->states.push_back(i);
    return path.mask(path.join({{queue, bit(1)},
                                {stalled, bit(1)},
                                {head_pressure(output.valid, output.data, response), bit(1)}}),
                     output.valid);
  }
  // Each crossing side owns its index, count and word registers. The receiver holds a
  // forward word and wakes while the FIFO is nonempty or its reset handshake clears.
  void gate_crossing(const AsyncFifo &f, IrComponent &imported, ValueId valid,
                     const std::array<std::string, 2> &ends) {
    for (unsigned d = 0; d < 2; ++d) {
      auto *r = region(ends[d]);
      if (!r)
        continue;
      auto owned = d ? std::vector{f.read_index, f.read_count, f.out_valid}
                     : std::vector{f.write_index, f.write_count};
      for (const auto &field : f.fields)
        if (d)
          owned.push_back(field.received);
        else
          owned.insert(owned.end(), field.storage.begin(), field.storage.end());
      for (auto q : owned)
        r->states.push_back(b.ir.values.at(imported.get(q).index).state.index);
      if (d) {
        r->idle.push_back(not_(imported.get(f.out_valid)));
        r->wake.push_back(imported.get(f.nonempty));
      } else {
        consume(r, ends[0], valid);
      }
      if (f.reset_handshake)
        r->wake.push_back(not_(imported.get(f.handshake[d].enable)));
    }
  }
  ValueId bit(unsigned n) { return b.constant(1, n); }
  ValueId and_(ValueId a, ValueId c) { return b.binary(Op::BitAnd, a, c); }
  ValueId or_(ValueId a, ValueId c) { return b.binary(Op::BitOr, a, c); }
  ValueId not_(ValueId a) { return b.unary(Op::BitNot, a, 1); }
  ValueId eq(ValueId a, std::uint64_t v) {
    return b.binary(Op::Equal, a, b.constant(b.ir.values.at(a.index).width, v));
  }
  ValueId wire(unsigned width) { return b.constant(width, 0); }
  // Wires are single-drive placeholders; overwriting is a composition fault.
  void drive(ValueId to, ValueId from) {
    if (!driven_wires.insert(to.index).second)
      throw std::logic_error("A composition wire was driven twice");
    const auto width = b.ir.values.at(to.index).width;
    auto zero = b.constant(width, 0);
    b.ir.values.at(to.index) = {width, Op::BitOr, {from, zero, {}}};
  }
  void bind(const std::string &id, ValueId v) { out.bindings.emplace(id, v); }
  Channel channel(const std::vector<unsigned> &widths) {
    Channel c{wire(1), wire(1), {}};
    for (auto width : widths)
      c.data.push_back(wire(width));
    return c;
  }
  ValueId transfer(const Channel &c) { return and_(c.valid, c.ready); }
  // Pressure is a thermometer: bit k marks a waiting packet above level k (message.hpp point 15).
  unsigned level_hint_width() const { return out.plan.level_hint_bits(); }
  ValueId spread(ValueId on) {
    return b.select(on,
                    b.constant(level_hint_width(), (std::uint64_t{1} << level_hint_width()) - 1),
                    b.constant(level_hint_width(), 0));
  }
  ValueId masked(ValueId level_hint, ValueId on) { return and_(level_hint, spread(on)); }
  ValueId pressure(ValueId level) { return PressurePath{b, level_hint_width()}.encode(level); }
  unsigned fragment_word(const std::vector<ValueId> &data, bool response) {
    std::vector<unsigned> widths(data.size());
    std::ranges::transform(data, widths.begin(),
                           [&](const ValueId &x) { return b.ir.values.at(x.index).width; });
    for (unsigned word = 1; word <= 128; word *= 2)
      if (burst_fragment_widths(response, word) == widths)
        return word;
    throw std::logic_error("A pressed channel has no burst fragment layout");
  }
  ValueId head_pressure(ValueId valid, const std::vector<ValueId> &data, bool response) {
    const auto word = fragment_word(data, response);
    return masked(pressure(data[burst_field_index(response, word, "level")]),
                  and_(valid, data[burst_field_index(response, word, "first")]));
  }
  ValueId level_hint_owned(const std::string &id, unsigned width) {
    const auto q = b.state(width, clock, reset);
    bind("level_hint/" + id, q);
    return q;
  }
  ValueId level_hint_state(const std::string &id, ValueId next) {
    const auto q = level_hint_owned(id, level_hint_width());
    b.update(q, next);
    return q;
  }
  Channel pipeline(const std::string &id, const Channel &input, LinkPipeline mode,
                   bool response = false) {
    const auto ready = wire(1);
    std::optional<ValueId> bypass;
    if (!mode.bypass_port.empty()) {
      auto [port, fresh] = bypasses.try_emplace(mode.bypass_port);
      if (fresh)
        port->second = b.input(mode.bypass_port, 1, "bypass:" + mode.bypass_port, "bypass");
      bypass = port->second;
    }
    const PipeDemand demand =
        input.level_hint
            ? [this, response](const auto &data) { return head_pressure(bit(1), data, response); }
            : PipeDemand{};
    const auto p =
        lower_link_pipeline(b, clock, reset, input.valid, input.data, ready, mode,
                            out.plan.reset_coverage == ResetCoverage::All, bypass, demand);
    drive(input.ready, p.ready);
    if (mode.forward)
      bind(id + ".full", p.control.front());
    if (mode.backward && !mode.half_rate)
      bind(id + ".spare", p.control.back());
    bind(id + ".push", p.push);
    bind(id + ".pop", p.pop);
    bind(id + ".input.valid", input.valid);
    bind(id + ".input.ready", p.ready);
    bind(id + ".output.valid", p.valid);
    bind(id + ".output.ready", ready);
    for (std::size_t i = 0; i < p.data.size(); ++i) {
      bind(id + ".input.field" + decimal(i), input.data[i]);
      bind(id + ".output.field" + decimal(i), p.data[i]);
    }
    out.storage.push_back({id, clock, p.payload, p.control, p.push, p.pop});
    Channel piped{p.valid, ready, p.data};
    if (input.level_hint)
      piped.level_hint = PressurePath{b, level_hint_width()}.follow(
          *input.level_hint, p.ready, p.positions,
          [&](ValueId next) { return level_hint_state(id, next); });
    return piped;
  }
  // A gate region stays open while a slot of a pipe stored since record `from` is occupied.
  void hold_open(GateRegion *r, std::size_t from) {
    for (auto i = from; r && i < out.storage.size(); ++i)
      for (auto q : out.storage[i].control)
        r->idle.push_back(not_(q));
  }
  // Output multiplexer of one arbiter: forwards the selected candidate's
  // fields and returns readiness to that candidate only. At most one
  // candidate is selected, so each field is an AND-OR of the candidates'.
  treadle::Datapath multiplexer(std::vector<Channel> inputs, const Channel &sink) {
    auto terms = std::make_shared<std::vector<std::vector<ValueId>>>(sink.data.size());
    treadle::Datapath d;
    d.open = [] {};
    d.candidate = [this, terms, inputs = std::move(inputs), sink](unsigned i, ValueId selected) {
      drive(inputs[i].ready, and_(selected, sink.ready));
      for (std::size_t f = 0; f < terms->size(); ++f) {
        const auto width = b.ir.values.at(sink.data[f].index).width;
        (*terms)[f].push_back(b.select(selected, inputs[i].data[f], b.constant(width, 0)));
      }
    };
    d.close = [this, terms, sink](ValueId valid) {
      drive(sink.valid, valid);
      for (std::size_t f = 0; f < terms->size(); ++f)
        drive(sink.data[f], treadle::join(b, (*terms)[f]));
      return std::pair{sink.valid, sink.ready};
    };
    return d;
  }
  // One physical graph output arbitrates its actual candidate set. There is
  // no payload buffering: the owner survives stalls and inter-fragment gaps. With levels each
  // candidate's level is its pressure (message.hpp point 15), and the sink presses with the OR of
  // the candidates' while it is valid.
  void graph_merge(const std::string &id, const std::vector<Channel> &inputs, Channel &sink,
                   unsigned last, const treadle::Spec &spec, bool response) {
    if (inputs.empty())
      throw std::logic_error("Graph output has no candidates");
    if (level_hint_width()) {
      std::vector<ValueId> presses;
      std::ranges::transform(inputs, std::back_inserter(presses),
                             [](const Channel &in) { return in.level_hint.value(); });
      sink.level_hint = masked(treadle::join(b, presses), sink.valid);
    }
    if (inputs.size() == 1) {
      drive(sink.valid, inputs[0].valid);
      drive(inputs[0].ready, sink.ready);
      for (std::size_t f = 0; f < sink.data.size(); ++f)
        drive(sink.data[f], inputs[0].data[f]);
      return;
    }
    std::vector<treadle::Candidate> candidates;
    // A response grant also ends at a segment's end (message.hpp point 17).
    const bool segmented = response && out.plan.response_segments;
    for (std::size_t i = 0; i < inputs.size(); ++i) {
      const auto tail =
          segmented ? or_(inputs[i].data[last], inputs[i].data[last + 1]) : inputs[i].data[last];
      candidates.push_back({inputs[i].valid, tail});
      bind(id + ".valid" + std::to_string(i), inputs[i].valid);
      bind(id + ".last" + std::to_string(i), tail);
      if (!level_hint_width())
        continue;
      candidates.back().level = inputs[i].level_hint;
      bind(id + ".level" + std::to_string(i), *inputs[i].level_hint);
    }
    // A cascaded output names its group arbiters "<id>.group<g>" and the top one id; eligible
    // is each candidate's own arbiter filter.
    auto levelled = spec;
    levelled.thermometer = level_hint_width() > 0;
    if (spec.policy == treadle::Policy::LevelRoundRobin)
      levelled.levels = out.plan.level_count();
    const auto arbiters =
        treadle::lower_output(b, clock, reset, levelled, candidates, multiplexer(inputs, sink));
    for (std::size_t g = 0, i = 0; g + (arbiters.size() > 1) < arbiters.size(); ++g)
      for (const auto e : arbiters[g].eligible)
        bind(id + ".eligible" + std::to_string(i++), e);
    std::vector<ValueId> registers; // the output is one resource
    for (std::size_t g = 0; g < arbiters.size(); ++g) {
      const auto &a = arbiters[g];
      const auto at = g + 1 < arbiters.size() ? id + ".group" + std::to_string(g) : id;
      // Grant state, and observation points of the grant (benches replay the policy model).
      auto named = a.state;
      named.insert(named.end(), {{".owner", a.owner},
                                 {".held", a.held},
                                 {".winner", a.winner},
                                 {".release", a.release},
                                 {".acquire", a.engage},
                                 {".transfer", a.transfer},
                                 {".tail", *a.tail}});
      for (const auto &[suffix, q] : named)
        bind(at + suffix, q);
      registers.insert(registers.end(), a.registers.begin(), a.registers.end());
    }
    out.storage.push_back(
        {id, clock, {}, registers, arbiters.back().engage, arbiters.back().release});
    bind(id + ".ready", sink.ready);
  }
  Channel like(const Channel &c) {
    Channel copy{wire(1), wire(1), {}};
    for (const auto f : c.data)
      copy.data.push_back(wire(b.ir.values.at(f.index).width));
    return copy;
  }
  // Steers a channel to two by a bit (set: the second): a closed side sees no valid, the head's
  // side alone decides readiness. Each side's direction (the write bit, the flow's low bit) is
  // constant, and side unloaded carries header packets: one fragment, no data lanes.
  std::array<Channel, 2> steer(const Channel &in, ValueId second, std::array<ValueId, 2> open,
                               unsigned unloaded, bool response, unsigned word_bytes) {
    const auto fields =
        fragment_fields(response, word_bytes, out.plan.source_bits, burst_profile());
    std::array<Channel, 2> sides{like(in), like(in)};
    for (unsigned k = 0; k < 2; ++k) {
      const auto here = and_(k ? second : not_(second), open[k]);
      drive(sides[k].valid, and_(in.valid, here));
      for (std::size_t f = 0; f < in.data.size(); ++f) {
        const auto &[name, width] = fields[f];
        auto value = in.data[f];
        const bool lane = name.starts_with("data") || name.starts_with("be");
        if (k == unloaded && (lane || name == "first" || name == "last"))
          value = b.constant(width, lane ? 0 : 1);
        else if (name == "write")
          value = bit(1 - k);
        else if (name == "source")
          value = width == 1 ? bit(k) : b.concat(b.slice(value, 1, width - 1), bit(k));
        drive(sides[k].data[f], value);
      }
      open[k] = and_(here, sides[k].ready);
    }
    drive(in.ready, or_(open[0], open[1]));
    return sides;
  }
  // Request merger of one source: a grant holds from a burst's first packet to the tail of its
  // final one (final marks it), so neither direction is granted while the other holds. AXI4
  // serves [read, write] round robin, AXI4-Lite and APB [write, read] by turns.
  //
  // With `pool` (the source's capacity n), reads and writes share n units in flight: a burst
  // takes a unit as its first packet transfers; every piece of a narrow-split burst (a
  // splitting source's narrow non-modifiable INCR goes out as single-beat packets) takes one
  // as the piece transfers, its head waiting for a free unit. A unit frees as its answer
  // finishes at the source: every response packet tail of a narrow-split burst frees one, any
  // other burst's last unanswered response packet tail does. pool_answers drives that side.
  struct PoolUnit {
    ValueId packets;       // the burst's packets sent and not yet answered
    ValueId whole, narrow; // the burst's final packet was sent; the burst splits into pieces
    ValueId rq, bs, fs;    // a packet tail entered; the burst starts; that tail is its final one
  };
  struct Pool {
    ValueId count, incr;                  // units in flight; a unit entered this cycle
    ValueId narrow_held, piece_wait;      // the held burst splits; its next piece starts a unit
    std::optional<ValueId> head_held;     // a burst has passed the merger's stop and not yet ended
    std::optional<ValueId> admitted_head; // the same admission state for regulated sources too
    std::array<std::vector<PoolUnit>, 2> units; // by write bit, then tag
    // The ID table, when it can fill: per entry its units in flight (zero: free) and its key
    // {write bit, AXI ID}; the unit key of the starting unit.
    std::vector<ValueId> id_count, id_key;
    ValueId start_key{};
    std::optional<ValueId> pending;
    unsigned id_bits = 0;
    // A request waits this cycle: the chosen head for its ID's destination or an ID table entry
    // (head_waits), or any request for a unit or a table entry (lacks_unit).
    ValueId head_waits{}, lacks_unit{};
  };
  struct Rejoined {
    Channel sink;
    std::optional<Pool> pool;
    std::optional<ValueId> source_level;
    std::vector<ValueId> qos_words;
  };
  ValueId regulator_empty(const RegulatorFront &front, ValueId valid) {
    auto used = front.used[0];
    for (std::size_t i = 1; i < front.used.size(); ++i)
      used = or_(used, front.used[i]);
    return not_(or_(valid, used));
  }
  // The merger's chosen request stops here while the requests past it reach the source's limit
  // (SourcePlan::request_limit): counted in as a burst's last fragment passes, out as its last
  // response arrives.
  Channel limit_requests(const Channel &chosen, unsigned last, const RegulatorFront &front,
                         Rejoined &merged) {
    const auto bits = static_cast<unsigned>(std::bit_width(front.request_limit));
    const auto count = b.state(bits, clock, reset);
    const auto open_for = not_(eq(count, front.request_limit));
    Channel passed{and_(chosen.valid, open_for), wire(1), chosen.data};
    drive(chosen.ready, and_(passed.ready, open_for));
    const auto entered = and_(transfer(passed), passed.data[last]);
    const auto plus = b.binary(Op::Subtract, count, b.constant(bits, (1ULL << bits) - 1));
    const auto minus = b.binary(Op::Subtract, count, b.constant(bits, 1));
    b.update(count, b.select(and_(entered, not_(*front.answered)), plus,
                             b.select(and_(*front.answered, not_(entered)), minus, count)));
    merged.qos_words.push_back(count);
    return passed;
  }
  // The packets pipe of a regulated source. Its requests take their level as they enter and wait
  // for their context and ID binding behind it; a burst that waits in the engine for a local
  // completion stays out of it. A regulated burst is one packet (qos.whole_bursts).
  Channel qos_packet_pipe(const std::string &id, Channel raw, ValueId head_waits, unsigned word,
                          const LinkPipeline &mode, const RegulatorFront *front, Rejoined &merged) {
    drive(front->presented, raw.valid);
    const auto actual = and_(raw.valid, not_(head_waits));
    const auto command = and_(actual, raw.data[burst_field_index(false, word, "first")]);
    const auto request = masked(pressure(front->request_level), command);
    const auto level_hint = pressure(front->now);
    Channel pipe_input{wire(1), wire(1), raw.data};
    pipe_input.data.resize(merged.sink.data.size());
    pipe_input.data[burst_field_index(false, word, "level")] = front->request_level;
    drive(pipe_input.valid, actual);
    auto head = pipeline(id.substr(0, id.rfind("/request")) + "/pipe/packets", pipe_input, mode);
    drive(raw.ready, and_(pipe_input.ready, not_(head_waits)));
    drive(front->taken, and_(actual, raw.ready));
    const auto qp =
        lower_qos_level_hint(b, clock, reset, raw.valid, request, level_hint, head.ready, mode);
    const auto qh = lower_qos_level(b, clock, reset, unsigned(mode.forward) + mode.backward,
                                    front->now, regulator_empty(*front, or_(raw.valid, head.valid)),
                                    raw.valid, raw.ready, head.valid, head.ready);
    head.level_hint = qp.value;
    merged.source_level = qh.value;
    merged.qos_words.insert(merged.qos_words.end(), qp.words.begin(), qp.words.end());
    merged.qos_words.insert(merged.qos_words.end(), qh.words.begin(), qh.words.end());
    return head;
  }
  ValueId rejoin_id_fits(const std::string &id, Pool &p, const Channel &head, unsigned word,
                         unsigned id_entries, unsigned id_bits, unsigned pool) {
    if (!id_entries)
      return bit(1);
    p.id_bits = id_bits;
    const unsigned key_bits = 1 + id_bits;
    const auto count_bits = static_cast<unsigned>(std::bit_width(pool));
    for (unsigned e = 0; e < id_entries; ++e) {
      p.id_count.push_back(b.state(count_bits, clock, reset));
      p.id_key.push_back(b.state(key_bits, clock, reset));
      bind(id + ".ids.e" + std::to_string(e) + ".count", p.id_count.back());
      bind(id + ".ids.e" + std::to_string(e) + ".key", p.id_key.back());
    }
    p.start_key = unit_key(head, word, id_bits);
    auto known = bit(0), vacant = bit(0);
    for (unsigned e = 0; e < id_entries; ++e) {
      const auto used = not_(eq(p.id_count[e], 0));
      known = or_(known, and_(used, b.binary(Op::Equal, p.id_key[e], p.start_key)));
      vacant = or_(vacant, not_(used));
    }
    return and_(bit(1), or_(known, vacant));
  }
  void rejoin_waits(const std::array<Channel, 2> &faces,
                    const std::optional<std::array<ValueId, 2>> &waiting,
                    const RegulatorFront *front, bool qos_packets, const Channel &head,
                    ValueId waits, ValueId blocked, ValueId room, ValueId held, unsigned word,
                    Pool &p, Rejoined &merged) {
    auto asking = or_(faces[0].valid, faces[1].valid);
    if (waiting)
      asking = or_(asking, or_((*waiting)[0], (*waiting)[1]));
    if (front)
      asking = head.valid;
    const auto reasons = PressurePath{b, 2}.join(
        {{b.constant(2, 1), and_(head.valid, waits)},
         {b.constant(2, 2), and_(asking, not_(or_(room, held)))},
         {b.select(head.valid, b.constant(2, 3), b.constant(2, 2)), blocked}});
    p.head_waits = b.slice(reasons, 0, 1);
    p.lacks_unit = b.slice(reasons, 1, 1);
    if (qos_packets) {
      const PressurePath path{b, level_hint_width()};
      merged.sink.level_hint =
          path.join({{*head.level_hint, bit(1)},
                     {path.encode(head.data[burst_field_index(false, word, "level")]),
                      and_(head.valid, or_(p.head_waits, p.lacks_unit))}});
    }
  }
  void rejoin_pool_units(Pool &p, unsigned pool, unsigned width, ValueId dir, ValueId tag,
                         ValueId sent, ValueId starting, ValueId final_w, ValueId narrow) {
    for (unsigned d = 0; d < 2; ++d)
      for (unsigned t = 0; t < pool; ++t) {
        const auto here = and_(eq(dir, d), eq(tag, t));
        const auto &u = p.units[d].emplace_back(
            PoolUnit{b.state(std::max(piece_count::kBits, width), clock, reset),
                     b.state(1, clock, reset), b.state(1, clock, reset), and_(sent, here),
                     and_(starting, here), and_(sent, and_(here, final_w))});
        b.update(u.narrow, b.select(u.bs, narrow, u.narrow));
      }
  }
  // A face whose engine holds a burst waiting for its ID competes as if it had a packet (the extra
  // data bit marks such a candidate); chosen, it keeps the merger until it comes.
  void rejoin_offers(const std::array<Channel, 2> &faces,
                     const std::optional<std::array<ValueId, 2>> &waiting, unsigned first,
                     bool gate, bool stops, std::array<Channel, 2> &offered,
                     std::array<ValueId, 2> &open) {
    if (!gate && !waiting)
      return;
    for (unsigned i = 0; i < 2; ++i) {
      const auto &face = faces[i == 0 ? first : 1 - first];
      open[i] = gate && !stops ? wire(1) : bit(1);
      Channel ch = like(face);
      auto shown = face.valid;
      if (waiting) {
        const auto wait = and_((*waiting)[i == 0 ? first : 1 - first], not_(face.valid));
        shown = or_(shown, wait);
        ch.data.push_back(wire(1));
        drive(ch.data.back(), wait);
      }
      drive(ch.valid, stops ? shown : and_(shown, open[i]));
      for (std::size_t f = 0; f < face.data.size(); ++f)
        drive(ch.data[f], face.data[f]);
      drive(face.ready, stops ? ch.ready : and_(ch.ready, open[i]));
      offered[i] = ch;
    }
  }
  //
  // With id_entries (the source's ID table size, smaller than the keys it can have in flight), a
  // unit also needs an entry of the ID table, shared by reads and writes: a unit whose
  // {direction, AXI ID} has an entry joins it, one with a new key takes a free entry, and its head
  // waits while every entry is taken. An entry frees as its last unit frees.
  Rejoined rejoin_requests(const std::string &id, const std::array<Channel, 2> &faces,
                           const std::array<ValueId, 2> &final, bool lite, unsigned last,
                           std::optional<unsigned> pool, bool split_narrow, unsigned word,
                           unsigned narrow_below, unsigned id_entries = 0, unsigned id_bits = 0,
                           std::optional<std::array<ValueId, 2>> waiting = std::nullopt,
                           const RegulatorFront *front = nullptr, const LinkPipeline &qos_mode = {},
                           unsigned slots = 0) {
    if (!slots)
      slots = pool.value_or(0);
    if (!pool)
      waiting.reset();
    Rejoined merged{like(faces[0]), {}, {}, {}};
    // Lite checks resource eligibility before rotating write then read offers.
    // Full AXI selects read then write offers and holds its choice through resource waits.
    const unsigned write_first = 0, read_first = 1;
    const unsigned first = lite ? write_first : read_first;
    std::array<Channel, 2> offered{faces[first], faces[1 - first]};
    std::array<ValueId, 2> open{};
    const bool gate = pool.has_value();
    const bool qos_packets = front && (qos_mode.forward || qos_mode.backward);
    // A source's pool stops the chosen head, not the merger's arbitration: the head stays chosen
    // while a unit is lacking.
    const bool stops = pool && !lite;
    const bool qos_stops = front && pool;
    rejoin_offers(faces, waiting, first, gate, stops, offered, open);
    // With an ID table or waiting faces the arbiter's choice (head) passes on only once it may: a
    // chosen head whose unit does not fit, or that waits for its ID, keeps the merger, so the
    // other direction waits behind it.
    const bool holds = pool && (id_entries || waiting);
    Channel head = holds || qos_packets || stops ? like(faces[0]) : merged.sink;
    ValueId head_waits = bit(0);
    if (waiting) {
      head.data.push_back(wire(1));
      head_waits = head.data.back();
    }
    std::vector<treadle::Candidate> candidates;
    for (unsigned i = 0; i < 2; ++i)
      candidates.push_back(
          {offered[i].valid, and_(offered[i].data[last], final[i == 0 ? first : 1 - first])});
    const auto merge_policy =
        lite ? treadle::Policy::RotatingPriority : treadle::Policy::RoundRobin;
    const auto a = treadle::lower_arbiter(b, clock, reset, {merge_policy, {}, 1, false, true},
                                          candidates, multiplexer({offered[0], offered[1]}, head));
    for (const auto &[suffix, q] : a.state)
      bind(id + suffix, q);
    bind(id + ".owner", a.owner);
    bind(id + ".held", a.held);
    if (qos_stops && front->answered)
      head = limit_requests(head, last, *front, merged);
    if (qos_packets) {
      head = qos_packet_pipe(id, head, head_waits, word, qos_mode, front, merged);
      head_waits = bit(0);
    }
    if (pool) {
      auto &p = merged.pool.emplace(Pool{});
      const auto width = static_cast<unsigned>(std::bit_width(*pool));
      p.count = b.state(width, clock, reset);
      const auto room = b.binary(Op::UnsignedLess, p.count, b.constant(width, *pool));
      const auto field = [&](const char *name) {
        return head.data[burst_field_index(false, word, name)];
      };
      p.narrow_held = b.state(1, clock, reset), p.piece_wait = b.state(1, clock, reset);
      const auto blocked = holds ? wire(1) : bit(0);
      const auto stop = stops ? wire(1) : or_(blocked, head_waits);
      if (holds || stops) {
        drive(merged.sink.valid, and_(head.valid, not_(stop)));
        for (std::size_t f = 0; f < merged.sink.data.size(); ++f)
          drive(merged.sink.data[f], head.data[f]);
        drive(head.ready, and_(merged.sink.ready, not_(stop)));
      }
      const auto sent = and_(transfer(merged.sink), merged.sink.data[last]);
      const auto final_w =
          qos_stops ? bit(1) : b.select(eq(a.winner, 0), final[first], final[1 - first]);
      // The merger's hold covers a head still stopped: the head's own covers what has passed.
      const auto held = stops ? b.state(1, clock, reset) : a.held;
      if (stops) {
        p.admitted_head = held;
        b.update(held, b.select(transfer(merged.sink), not_(and_(merged.sink.data[last], final_w)),
                                held));
        if (qos_stops)
          merged.qos_words.push_back(held);
        else
          p.head_held = held;
      }
      // A burst starts as its first fragment passes on with no burst held, or held while it waits
      // for its unit (pending). The winner reads combinationally.
      const bool checks = id_entries != 0;
      const auto pending = checks ? b.state(1, clock, reset) : bit(0);
      const auto need = or_(not_(held), pending);
      const auto waits = qos_packets && front->conflict
                             ? or_(head_waits, and_(need, *front->conflict))
                             : head_waits;
      if (stops)
        drive(stop,
              or_(or_(blocked, waits), and_(or_(need, and_(held, p.piece_wait)), not_(room))));
      // Admission counts a transferred head when stops is set, otherwise the eligible choice.
      // A one-fragment burst can start and release in the same cycle.
      const auto starting = stops   ? and_(transfer(merged.sink), need)
                            : holds ? and_(head.valid, and_(need, not_(checks ? stop : blocked)))
                                    : and_(or_(a.engage, a.release), not_(a.held));
      ValueId narrow = bit(0);
      if (split_narrow) {
        narrow = and_(eq(field("burst"), 1),
                      and_(b.binary(Op::UnsignedLess, field("size"), b.constant(3, narrow_below)),
                           and_(not_(b.slice(field("cache"), 1, 1)), not_(field("error")))));
        narrow = and_(narrow, and_(eq(field("length"), 0), not_(final_w)));
        if (out.plan.exclusive)
          narrow = and_(narrow, not_(field("lock")));
      }
      // piece_wait: the held burst is narrow and a piece of it has left, so its next packet
      // head starts a unit and waits for room. The starting burst's own narrow marks it (a
      // single-fragment first piece transfers in its starting cycle, before narrow_held loads).
      b.update(p.narrow_held, b.select(starting, narrow, p.narrow_held));
      const auto narrow_now = b.select(starting, narrow, p.narrow_held);
      b.update(p.piece_wait, b.select(sent, and_(narrow_now, not_(final_w)), p.piece_wait));
      // The head needs its key's ID table entry or a free entry.
      const auto fits = rejoin_id_fits(id, p, head, word, id_entries, id_bits, *pool);
      const auto piece = and_(held, and_(p.piece_wait, not_(pending)));
      if (holds)
        drive(blocked, checks ? and_(head.valid, and_(or_(need, piece), not_(fits))) : bit(0));
      rejoin_waits(faces, waiting, front, qos_packets, head, waits, blocked, room, held, word, p,
                   merged);
      if (checks)
        b.update(pending, and_(head.valid, and_(need, stop)));
      if (gate && !stops)
        for (unsigned i = 0; i < 2; ++i) {
          drive(open[i], or_(room, and_(a.held, and_(eq(a.owner, i), not_(p.piece_wait)))));
          bind(id + ".open" + std::to_string(i), open[i]);
        }
      p.incr = or_(starting, and_(sent, and_(held, and_(p.narrow_held, p.piece_wait))));
      const auto dir = field("write");
      const auto tag = out.plan.burst_tag_bits ? field("tag") : bit(0);
      rejoin_pool_units(p, slots, width, dir, tag, sent, starting, final_w, narrow);
      if (checks)
        p.pending = pending;
      bind(id + ".pool", p.count);
    }
    return merged;
  }
  // The ID table key of a request packet: its write bit above the low id_bits of its AXI ID.
  ValueId unit_key(const Channel &ch, unsigned word, unsigned id_bits) {
    const auto write = ch.data[burst_field_index(false, word, "write")];
    if (!id_bits)
      return write;
    const auto axi_id = ch.data[burst_field_index(false, word, "id")];
    return b.concat(write, b.slice(axi_id, 0, id_bits));
  }
  // Count `up` minus `down` on a state word, never below zero (a reset of this side can leave a
  // tail with nothing in flight).
  void step_counter(ValueId count, ValueId up, ValueId down) {
    const auto width = b.ir.values.at(count.index).width;
    down = and_(down, not_(eq(count, 0)));
    b.update(count, b.select(and_(up, not_(down)),
                             b.binary(Op::Subtract, count, b.constant(width, (1U << width) - 1)),
                             b.select(and_(down, not_(up)),
                                      b.binary(Op::Subtract, count, b.constant(width, 1)), count)));
  }
  // The response side of one source's pool (rejoin_requests): a response packet tail arriving
  // at the source frees its burst's unit when the burst splits into pieces (every piece's
  // tail), or when it is the burst's last unanswered packet after its final packet was sent.
  void pool_answers(const Pool &p, const Channel &arriving, unsigned word) {
    const auto tail =
        and_(transfer(arriving), arriving.data[burst_field_index(true, word, "last")]);
    const auto source = arriving.data[burst_field_index(true, word, "source")];
    const auto dir = out.plan.source_bits > 1 ? b.slice(source, 0, 1) : source;
    const auto tag =
        out.plan.burst_tag_bits ? arriving.data[burst_field_index(true, word, "tag")] : bit(0);
    ValueId decr = bit(0);
    // The AXI ID of the unit freed, carried by its response tail.
    ValueId freed_id = bit(0);
    if (p.id_bits) {
      freed_id = arriving.data[burst_field_index(true, word, "id")];
      if (b.ir.values.at(freed_id.index).width > p.id_bits)
        freed_id = b.slice(freed_id, 0, p.id_bits);
    }
    for (unsigned d = 0; d < 2; ++d)
      for (const auto &u : p.units[d]) {
        const auto t = static_cast<unsigned>(&u - p.units[d].data());
        const auto rs = and_(tail, and_(eq(dir, d ^ 1), eq(tag, t)));
        const auto done = and_(rs, and_(u.whole, eq(u.packets, 1)));
        decr = or_(decr, and_(rs, or_(u.narrow, done)));
        step_counter(u.packets, u.rq, rs);
        b.update(u.whole, b.select(done, bit(0), or_(u.whole, u.fs)));
      }
    step_counter(p.count, p.incr, decr);
    if (p.id_count.empty())
      return;
    // The ID table: the starting unit joins its key's entry or takes the lowest free one; the
    // freed unit leaves its key's entry.
    const auto write = not_(dir);
    const auto freed_key = p.id_bits ? b.concat(write, freed_id) : write;
    auto known = bit(0);
    for (std::size_t e = 0; e < p.id_count.size(); ++e)
      known = or_(known,
                  and_(not_(eq(p.id_count[e], 0)), b.binary(Op::Equal, p.id_key[e], p.start_key)));
    auto earlier = bit(0);
    for (std::size_t e = 0; e < p.id_count.size(); ++e) {
      const auto used = not_(eq(p.id_count[e], 0));
      const auto take = and_(not_(known), and_(not_(used), not_(earlier)));
      earlier = or_(earlier, not_(used));
      const auto up =
          and_(p.incr, or_(and_(used, b.binary(Op::Equal, p.id_key[e], p.start_key)), take));
      const auto down = and_(decr, and_(used, b.binary(Op::Equal, p.id_key[e], freed_key)));
      step_counter(p.id_count[e], up, down);
      b.update(p.id_key[e], b.select(and_(p.incr, take), p.start_key, p.id_key[e]));
    }
  }
  Channel rejoin_responses(const std::array<Channel, 2> &faces, unsigned first, unsigned last,
                           std::vector<ValueId> &states, std::array<ValueId, 2> *picked) {
    using enum beat_stage::ResponsePhase;
    const auto &[w, r] = faces;
    const auto sink = like(w);
    const auto phase = b.state(3, clock, reset);
    states.push_back(phase);
    const auto free_read =
        and_(r.valid, not_(and_(eq(phase, ReadBoundary), and_(w.valid, r.data[first]))));
    const auto read = b.select(eq(phase, WriteResponse), bit(0),
                               or_(beat_stage::read_owned(b, phase), free_read));
    drive(sink.valid, b.select(read, r.valid, w.valid));
    for (std::size_t f = 0; f < sink.data.size(); ++f)
      drive(sink.data[f], b.select(read, r.data[f], w.data[f]));
    drive(r.ready, and_(read, sink.ready));
    drive(w.ready, and_(not_(read), sink.ready));
    if (picked)
      *picked = {and_(not_(read), w.valid), and_(read, r.valid)};
    const auto r_moves = transfer(r);
    const auto code = [&](beat_stage::ResponsePhase p) { return b.constant(3, p); };
    const auto reading = b.select(
        r_moves, b.select(r.data[last], code(ReadBoundary), code(ReadTransfer)), code(ReadPacket));
    const auto writing =
        b.select(w.valid, b.select(sink.ready, code(Available), code(WriteResponse)),
                 b.select(eq(phase, WriteResponse), phase, code(Available)));
    b.update(phase, b.select(read, reading, writing));
    return sink;
  }
  // A joined outlet offers at most one response face. Both faces see the network's ready.
  Channel joined_responses(const std::array<Channel, 2> &faces, bool read_first = false) {
    const auto &[w, r] = faces;
    const auto sink = like(w);
    drive(sink.valid, or_(w.valid, r.valid));
    for (std::size_t f = 0; f < sink.data.size(); ++f)
      drive(sink.data[f], b.select(r.valid, r.data[f], w.data[f]));
    drive(r.ready, sink.ready);
    // Behind a merge with several reads a stored word's beats may leave while the outlet shows a B.
    drive(w.ready, read_first ? and_(sink.ready, not_(r.valid)) : sink.ready);
    return sink;
  }
  struct ImportedEndpoint {
    std::map<std::string, ValueId> states; // component binding key -> imported state
    std::map<std::string, ValueId> axi;    // axi signal -> composition port value
    std::map<std::string, ValueId> bound;  // component binding key -> imported value
  };
  // Imports one component: its clock and reset inputs take the current domain's, every other
  // input reads input(port), output(port, value) places every driver, and the component bindings
  // surface under prefix.
  template <typename Input, typename Output>
  void import_component(const NodeHardware &component, const std::string &prefix,
                        ImportedEndpoint &result, Input input, Output output) {
    IrComponent imported(b, component.ir);
    for (std::size_t i = 0; i < component.ir.values.size(); ++i) {
      const auto &v = component.ir.values[i];
      if (v.op != Op::Input)
        continue;
      const auto &port = component.ir.ports[v.input_port.index];
      imported.replace(ValueId{i}, port.signal == "clock"     ? clock
                                   : port.signal == "reset_n" ? reset
                                                              : input(port));
    }
    for (const auto &driver : component.ir.drivers)
      output(component.ir.ports[driver.port.index], imported.get(driver.value));
    for (const auto &[key, id] : component.bindings) {
      const auto value = imported.get(id);
      bind(prefix + key, value);
      result.bound[key] = value;
      if (component.ir.values.at(id.index).op == Op::State)
        result.states[key] = value;
    }
    imported.finish();
  }
  // Import one planned adapter instance and splice its wide/narrow fragment
  // channels. The upstream channel is the wide side of a serializer and the
  // narrow side of a deserializer.
  // Whether a request run of whole beats may join at a deserializer (width_adapter.hpp): its
  // destination takes runs of several beats, a Lite INCR write fills its word, or the beats are
  // as wide as the destination's word. None when every destination takes them.
  std::optional<ValueId> merge_gate(const Channel &narrow, unsigned word) {
    const auto merged = upsize_targets(out.plan);
    std::vector<std::tuple<unsigned, unsigned, bool>> limited; // index, word bytes, Lite
    unsigned index = 0;
    for (const auto &t : out.plan.targets)
      for (unsigned f = 0; f < t.flows; ++f, ++index)
        if (const auto bytes = endpoint_word(out.plan, t.name);
            bytes > 4 && !merged.contains(t.name))
          limited.emplace_back(index, bytes, t.protocol == Protocol::Axi4Lite);
    if (limited.empty())
      return std::nullopt;
    const auto size = narrow.data.at(burst_field_index(false, word, "size"));
    const bool routed = out.plan.target_bits > 0;
    auto gate = bit(1);
    for (const auto &[d, bytes, lite] : limited) {
      // Beats as wide as the word: size >= log2(bytes).
      auto wide = not_(b.binary(Op::UnsignedLess, size,
                                b.constant(3, static_cast<unsigned>(std::countr_zero(bytes)))));
      if (lite)
        wide = or_(wide, and_(narrow.data.at(burst_field_index(false, word, "write")),
                              eq(narrow.data.at(burst_field_index(false, word, "burst")), 1)));
      auto here = routed ? eq(narrow.data.at(burst_field_index(false, word, "target")), d) : bit(1);
      gate = and_(gate, or_(not_(here), wide));
    }
    return gate;
  }
  void import_adapter(const StagePlan &spec, const Channel &up, const Channel &down) {
    const bool response = spec.response;
    WidthAdapterParameters parameters{spec.wide_bytes, spec.narrow_bytes, response,
                                      out.plan.source_bits, burst_profile()};
    std::optional<ValueId> gate;
    if (!response && !spec.serializer)
      gate = merge_gate(up, spec.narrow_bytes);
    parameters.merge_gate = gate.has_value();
    // A wide or narrow face signal: valid, ready or a fragment field at that side's word.
    auto face = [&](const Port &port) -> const ValueId & {
      const bool wide = port.endpoint == "wide";
      const Channel &ch = wide == spec.serializer ? up : down;
      if (port.signal == "merge")
        return gate.value();
      if (port.signal == "valid")
        return ch.valid;
      if (port.signal == "ready")
        return ch.ready;
      return ch.data.at(
          burst_field_index(response, wide ? spec.wide_bytes : spec.narrow_bytes, port.signal));
    };
    ImportedEndpoint imported;
    import_component(
        admitted(spec.serializer ? lower_width_serializer(parameters)
                                 : lower_width_deserializer(parameters)),
        spec.name + "/", imported,
        [&](const Port &port) {
          if (port.endpoint != "wide" && port.endpoint != "narrow")
            throw std::logic_error("Unconnected adapter input " + port.name);
          return face(port);
        },
        [&](const Port &port, ValueId value) { drive(face(port), value); });
    CrossStorageBinding record{spec.name, clock, {}, {}, {}, {}};
    for (const auto &[key, id] : imported.states)
      (key.find(".control.") != std::string::npos || key.find(".lane_") != std::string::npos
           ? record.payload
           : record.control)
          .push_back(id);
    record.acquire =
        out.bindings.at(spec.name + (spec.serializer ? "/ser.accept" : "/des.low_accept"));
    record.release =
        out.bindings.at(spec.name + (spec.serializer ? "/ser.wide_accept" : "/des.wide_accept"));
    out.storage.push_back(std::move(record));
  }
  // One link: its stages in data flow order, each on its own domain's clock; a link without
  // stages passes the channel through unchanged. A stage is in the sender's gate region, or the
  // receiver's from the crossing on and for the receiver chain and pipe; a pipe holds a word
  // while a slot is full. An ungated pipe stays outside that region and wakes it at its output.
  Channel lower_link(const LinkPlan &link, Channel current) {
    bool received = false;
    for (const auto &stage : link.stages) {
      if (stage.kind == StageKind::Crossing) {
        current = cross_fragment(stage.name, link.crossing_bytes(), current, {link.from, link.to});
        received = true;
        continue;
      }
      received =
          received || stage.segment.ends_with("/link-out") || stage.name == "pipeline/" + link.name;
      const auto &owner = received ? link.to : link.from;
      auto *r = region(owner);
      const auto first = b.ir.states.size();
      const bool ungated = stage.kind == StageKind::Pipe && stage.pipe.ungated;
      if (!ungated)
        consume(r, owner, current.valid);
      std::tie(clock, reset) = domains.at(stage.domain);
      if (stage.kind == StageKind::Pipe) {
        current = pipeline(stage.name, current, stage.pipe, stage.response);
        if (ungated)
          consume(r, owner, current.valid);
        else
          for (auto q : r ? out.storage.back().control : std::vector<ValueId>{})
            r->idle.push_back(not_(q));
      } else {
        Channel next = channel(burst_fragment_widths(
            stage.response, stage.serializer ? stage.narrow_bytes : stage.wide_bytes));
        import_adapter(stage, current, next);
        // A serializer passes its input's pressure on; a deserializer presses while its output
        // is valid, with its input's pressure and its output head's level.
        if (current.level_hint)
          next.level_hint = stage.serializer
                                ? *current.level_hint
                                : or_(masked(*current.level_hint, next.valid),
                                      head_pressure(next.valid, next.data, stage.response));
        current = next;
      }
      if (r && !ungated)
        r->claim(b, first);
      producers[current.valid.index] = owner;
    }
    return current;
  }

  // ---------------- AXI4 burst attachment ----------------
  // The burst profile's transport fragment layout (the burst control bundle
  // rides the adapter replication discipline; packet transport keys occupation
  // on the last marker, found by name).
  BurstProfile burst_profile() const { return transport_burst(out.plan); }
  std::vector<unsigned> burst_fragment_widths(bool response, unsigned word_bytes) {
    std::vector<unsigned> widths;
    for (const auto &[name, width] :
         fragment_fields(response, word_bytes, out.plan.source_bits, burst_profile()))
      widths.push_back(width);
    return widths;
  }
  unsigned burst_field_index(bool response, unsigned word_bytes, const std::string &name) {
    const auto fields =
        fragment_fields(response, word_bytes, out.plan.source_bits, burst_profile());
    const auto it = std::ranges::find(fields, name, &std::pair<std::string, unsigned>::first);
    if (it == fields.end())
      throw std::logic_error("A burst fragment layout lacks the field " + name);
    return static_cast<unsigned>(it - fields.begin());
  }
  // Import one initiator/target endpoint component. The AXI face becomes composition
  // ports named <port_prefix><signal>; the Weft faces splice into the given
  // endpoint-layout channels (valid/data driven by the request-producing side,
  // ready by the consuming side). Component bindings surface under
  // <prefix><key> and imported state words are collected for the caller's
  // storage records.
  ImportedEndpoint import_endpoint(const NodeHardware &component, const std::string &prefix,
                                   const std::string &port_prefix, const std::string &port_endpoint,
                                   std::map<std::string, Channel *> faces,
                                   const std::map<std::string, unsigned> &port_widths = {},
                                   const std::map<std::string, ValueId> &hints = {},
                                   const std::vector<EndpointPipe> &pipes = {}) {
    // External protocol ports may be narrower than the component's: inputs
    // zero-extend, outputs truncate, and width zero removes the port (inputs
    // then read zero).
    const auto narrowed = [&](const Port &port) -> std::optional<unsigned> {
      const auto it = port_widths.find(port.signal);
      if (it == port_widths.end() || it->second >= port.width)
        return std::nullopt;
      return it->second;
    };
    auto field_of = [&](const std::string &endpoint, const std::string &signal) -> ValueId & {
      Channel *ch = faces.at(endpoint);
      if (signal == "valid")
        return ch->valid;
      if (signal == "ready")
        return ch->ready;
      return ch->data.at(
          endpoint_field_index(!endpoint.ends_with("wreq") && !endpoint.ends_with("rreq"), signal));
    };
    const auto external = [](const Port &port) {
      return port.endpoint == "axi" || port.endpoint == "ahb" || port.endpoint == "apb" ||
             port.endpoint == "axil";
    };
    ImportedEndpoint result;
    // Pipes on the AXI channels aw, w, ar, r and b between the ports and the component (into
    // an initiator, out of an outlet), fields in the component's port order. Per piped signal:
    // the pipe side its source (the port of a component input, else the component) drives, and
    // the side its sink reads.
    std::map<std::string, std::pair<ValueId, ValueId>> through;
    for (const auto &pipe : pipes)
      if (const auto place = pipe_place(pipe); socket_channel(place)) {
        const auto fields = channel_fields(component, place);
        std::vector<unsigned> widths;
        std::ranges::transform(fields, std::back_inserter(widths), &Port::width);
        const auto up = channel(widths), down = pipeline(pipe.name, up, pipe.mode);
        for (std::size_t k = 0; k < fields.size(); ++k)
          through[fields[k].signal] = {up.data[k], down.data[k]};
        through[std::string(place) + "valid"] = {up.valid, down.valid};
        through[std::string(place) + "ready"] = {down.ready, up.ready};
      }
    auto input = [&](const Port &port) {
      if (external(port)) {
        ValueId value;
        if (const auto width = narrowed(port)) {
          value = b.constant(port.width, 0);
          if (*width)
            value = b.unary(Op::ZeroExtend,
                            b.input(port_prefix + port.signal, *width, port_endpoint, port.signal),
                            port.width);
        } else
          value = b.input(port_prefix + port.signal, port.width, port_endpoint, port.signal);
        result.axi[port.signal] = value;
        if (const auto it = through.find(port.signal); it != through.end()) {
          drive(it->second.first, value);
          return it->second.second;
        }
        return value;
      }
      // Sideband signals of a neighbouring component (the bounded packet
      // splitter reads the initiator's descriptor AxLOCK).
      if (port.endpoint == "hint")
        return hints.at(port.signal);
      if (faces.contains(port.endpoint))
        return field_of(port.endpoint, port.signal);
      throw std::logic_error("Unconnected endpoint input " + port.name);
    };
    auto output = [&](const Port &port, ValueId value) {
      if (external(port)) {
        if (const auto it = through.find(port.signal); it != through.end()) {
          drive(it->second.first, value);
          value = it->second.second;
        }
        // A removed output stays available internally for bindings.
        result.axi[port.signal] = value;
        if (const auto width = narrowed(port); !width)
          b.output(port_prefix + port.signal, value, port_endpoint, port.signal);
        else if (*width)
          b.output(port_prefix + port.signal, b.slice(value, 0, *width), port_endpoint,
                   port.signal);
      } else if (port.endpoint == "hint") {
        // A producer-owned request sideband is consumed only by its neighbour.
        result.bound[port.signal] = value;
        bind(prefix + "hint." + port.signal, value);
      } else if (port.endpoint == "status") {
        // Status outputs leave the composition as target ports.
        b.output(port_prefix + port.signal, value, port_endpoint, port.signal);
      } else if (faces.contains(port.endpoint)) {
        drive(field_of(port.endpoint, port.signal), value);
      } else
        throw std::logic_error("Unconnected endpoint output " + port.name);
    };
    import_component(component, prefix, result, input, output);
    return result;
  }
  // Endpoint-layout field tables for the initiator/target components' Weft faces.
  std::vector<std::pair<std::string, unsigned>> endpoint_request_fields;
  std::vector<std::pair<std::string, unsigned>> endpoint_response_fields;
  unsigned endpoint_field_index(bool response, const std::string &name) {
    const auto &fields = response ? endpoint_response_fields : endpoint_request_fields;
    const auto it = std::ranges::find(fields, name, &std::pair<std::string, unsigned>::first);
    if (it == fields.end())
      throw std::logic_error("Unknown endpoint field " + name);
    return static_cast<unsigned>(it - fields.begin());
  }
  // A request run's start in its word of `word_bytes` (width_adapter.hpp request runs), `bits`
  // wide: the address modulo the word.
  ValueId run_start(ValueId address, unsigned word_bytes, unsigned bits) {
    const auto low = b.slice(address, 0, static_cast<unsigned>(std::countr_zero(word_bytes)));
    return bits == b.ir.values.at(low.index).width ? low : b.unary(Op::ZeroExtend, low, bits);
  }
  // Bitwise difference (exclusive or) of two equal-width values.
  ValueId differ(ValueId a, ValueId c) {
    const auto w = b.ir.values.at(a.index).width;
    return and_(or_(a, c), b.unary(Op::BitNot, and_(a, c), w));
  }
  // The last byte of a run's first beat in a word of 2^bits bytes: address | 2^size - 1.
  ValueId first_beat_last(ValueId address, ValueId size, unsigned bits) {
    // Bit i: the address bit, or set below the beat size; bit 0 first, each next one above.
    auto last = or_(b.slice(address, 0, 1), b.binary(Op::UnsignedLess, b.constant(3, 0), size));
    for (unsigned i = 1; i < bits; ++i)
      last = b.concat(
          or_(b.slice(address, i, 1), b.binary(Op::UnsignedLess, b.constant(3, i), size)), last);
    return last;
  }
  // A run's wrap_end (width_adapter.hpp request runs): the source's mark (message.hpp point 4),
  // whose address bits reach bit 14, while the joins read the transport's `address_bits` (at least
  // 12): a set length bit j whose address bit size + j lies above them clears it.
  ValueId wrap_end(const Channel &ep, unsigned address_bits) {
    auto mark = ep.data[endpoint_field_index(false, "wrap_end")];
    const auto length = ep.data[endpoint_field_index(false, "length")];
    const auto size = ep.data[endpoint_field_index(false, "size")];
    for (unsigned j = address_bits - 7; j < b.ir.values.at(length.index).width; ++j) {
      const auto above =
          j >= address_bits
              ? bit(1)
              : not_(b.binary(Op::UnsignedLess, size, b.constant(3, address_bits - j)));
      mark = and_(mark, not_(and_(b.slice(length, j, 1), above)));
    }
    return mark;
  }
  // Frame one endpoint-layout channel into the transport layout at the
  // endpoint word: field-for-field aliases, the source identity stamped with
  // the flow constant on requests, the wide payload split into 32-bit lanes.
  // The address keeps its low transport bits, the raw start its page offset, and a request
  // takes the target index `target` (message.hpp point 16).
  Channel frame_burst(bool response, const Channel &ep, std::optional<ValueId> flow,
                      std::optional<unsigned> word_bytes = std::nullopt,
                      std::optional<ValueId> path = std::nullopt,
                      std::optional<ValueId> target = std::nullopt, unsigned beat_floor = 0,
                      std::optional<ValueId> level = std::nullopt,
                      std::optional<ValueId> cut = std::nullopt,
                      std::optional<ValueId> run_end_mark = std::nullopt) {
    const auto word = word_bytes.value_or(out.plan.burst_data_bits / 8);
    Channel tr = channel(burst_fragment_widths(response, word));
    drive(tr.valid, ep.valid);
    drive(ep.ready, tr.ready);
    const auto fields = fragment_fields(response, word, out.plan.source_bits, burst_profile());
    ValueId run_end{};
    for (std::size_t i = 0; i < fields.size(); ++i) {
      const auto &[name, width] = fields[i];
      if (name == "source" && flow) {
        drive(tr.data[i], *flow);
        continue;
      }
      // The route path is stamped at the network entry (message.hpp point 14).
      if (name == "path") {
        if (!path)
          throw std::logic_error("A route path field without its stamp");
        drive(tr.data[i], *path);
        continue;
      }
      if (name == "target") {
        drive(tr.data[i], target.value());
        continue;
      }
      // A response segment's end (message.hpp point 17): an interleaving target's, else none.
      if (name == "cut") {
        drive(tr.data[i], cut.value_or(bit(0)));
        continue;
      }
      if (name == "reach") {
        // The run's last byte in the word (message.hpp point 18): one beat of 2^size bytes.
        const auto ep_count = ep.data[endpoint_field_index(response, "count")];
        const auto wide = b.ir.values.at(ep_count.index).width <= width
                              ? b.unary(Op::ZeroExtend, ep_count, width + 1)
                              : b.slice(ep_count, 0, width + 1);
        const auto low = b.slice(ep.data[endpoint_field_index(response, "address")], 0, width);
        drive(tr.data[i],
              or_(low, b.slice(b.binary(Op::Subtract, wide, b.constant(width + 1, 1)), 0, width)));
        continue;
      }
      if (name == "wrap_end" && response) {
        drive(tr.data[i], run_end_mark.value_or(bit(1)));
        continue;
      }
      // The transport level (message.hpp point 15): the stamp, else the endpoint's (a mapped
      // source's), else zero; a packet leaves its source unmarked as a raise packet.
      const auto &ep_fields = response ? endpoint_response_fields : endpoint_request_fields;
      const auto on_ep = [&] {
        return std::ranges::find(ep_fields, name, &std::pair<std::string, unsigned>::first) !=
               ep_fields.end();
      };
      if ((name == "level" && (level || !on_ep())) || name == "raise") {
        drive(tr.data[i], name == "level" && level ? *level : b.constant(width, 0));
        continue;
      }
      if (name == "address" || name == "start_offset") {
        const auto whole =
            ep.data[endpoint_field_index(response, name == "address" ? name : "start_address")];
        drive(tr.data[i],
              b.ir.values.at(whole.index).width == width ? whole : b.slice(whole, 0, width));
        continue;
      }
      // A payload or strobe lane, sliced at the same lane index in each field.
      if (const bool data = name.starts_with("data"); data || name.starts_with("be")) {
        const bool user = name.starts_with("data_user");
        const unsigned lane = std::stoul(name.substr(user ? 9 : data ? 4 : 2));
        auto whole = ep.data[endpoint_field_index(response, user   ? "data_user"
                                                            : data ? "data"
                                                                   : "be")];
        drive(tr.data[i], width == b.ir.values.at(whole.index).width
                              ? whole
                              : b.slice(whole, lane * width, width));
        continue;
      }
      if (name == "count") {
        // A narrower endpoint occupancy widens to the composition's.
        auto ep_count = ep.data[endpoint_field_index(response, "count")];
        drive(tr.data[i], b.ir.values.at(ep_count.index).width == width
                              ? ep_count
                              : b.unary(Op::ZeroExtend, ep_count, width));
        continue;
      }
      if (name == "extent") {
        // A request run's last byte in the word (width_adapter.hpp request runs): the last byte of
        // its first beat for single beats (message.hpp points 3 and 4: an aligned beat of 2^size
        // bytes; extent zero), else its start plus its bytes less one, modulo the word (a run
        // never leaves its word). Its extent has no bit below `beat_floor`: every run is a single
        // beat or whole beats of at least 2^beat_floor bytes.
        const auto first = first_beat_last(ep.data[endpoint_field_index(response, "address")],
                                           ep.data[endpoint_field_index(response, "size")], width);
        run_end = first;
        if (beat_floor >= width) {
          drive(tr.data[i], b.constant(width, 0));
          continue;
        }
        const auto count = b.slice(ep.data[endpoint_field_index(response, "count")], 0, width);
        const auto start =
            run_start(ep.data[endpoint_field_index(response, "address")], word, width);
        const auto less_one = b.binary(Op::Subtract, count, b.constant(width, 1));
        run_end = b.unary(
            Op::BitNot, b.binary(Op::Subtract, b.unary(Op::BitNot, start, width), less_one), width);
        const auto extent = differ(first, run_end);
        drive(tr.data[i], beat_floor ? b.concat(b.slice(extent, beat_floor, width - beat_floor),
                                                b.constant(beat_floor, 0))
                                     : extent);
        continue;
      }
      if (name == "ends") {
        // Whether the run ends at the word's last byte ("extent" precedes it).
        drive(tr.data[i], eq(run_end, static_cast<unsigned>(word - 1)));
        continue;
      }
      if (name == "wrap_end") {
        drive(tr.data[i], wrap_end(ep, b.ir.values.at(tr.data[0].index).width));
        continue;
      }
      drive(tr.data[i], ep.data[endpoint_field_index(response, name)]);
    }
    return tr;
  }
  // Deframe one transport channel into an existing endpoint-layout channel:
  // the lanes join into the wide payload, the remaining fields alias. Deframing
  // happens only at the endpoint word boundary (after the adapter chain). The
  // address takes the global bits above the transport's from `span` (zero
  // without), and the raw start joins its low offset to the address above it.
  void deframe_burst_into(bool response, const Channel &tr, const Channel &ep, unsigned word_bytes,
                          std::uint64_t span = 0) {
    if (word_bytes * 8 !=
        b.ir.values.at(ep.data[endpoint_field_index(response, "data")].index).width)
      throw std::logic_error("Burst deframing happens at the endpoint word");
    drive(ep.valid, tr.valid);
    drive(tr.ready, ep.ready);
    auto tr_field = [&](const std::string &name) {
      return tr.data.at(burst_field_index(response, word_bytes, name));
    };
    const unsigned lanes = word_bytes / 4;
    const auto &ep_fields = response ? endpoint_response_fields : endpoint_request_fields;
    const auto address = tr_field("address");
    const auto low = b.ir.values.at(address.index).width;
    const auto global =
        b.ir.values.at(ep.data[endpoint_field_index(response, "address")].index).width;
    const auto whole =
        low == global ? address : b.concat(b.constant(global - low, span >> low), address);
    for (std::size_t i = 0; i < ep_fields.size(); ++i) {
      const auto &name = ep_fields[i].first;
      if (name == "address") {
        drive(ep.data[i], whole);
        continue;
      }
      if (name == "start_address") {
        const auto offset = tr_field("start_offset");
        const auto bits = b.ir.values.at(offset.index).width;
        drive(ep.data[i], b.concat(b.slice(whole, bits, global - bits), offset));
        continue;
      }
      if (name == "data" || name == "data_user" || name == "be") {
        ValueId joined = tr_field(name + "0");
        for (unsigned lane = 1; lane < lanes; ++lane)
          joined = b.concat(tr_field(name + decimal(lane)), joined);
        drive(ep.data[i], joined);
        continue;
      }
      if (name == "count") {
        // The endpoint word bounds the occupancy, so the dropped upper bits
        // are zero: a burst reaching a target is never wider than its word
        // (the source width clamp narrows AxSIZE to every narrower target
        // first; dedicated outlets see beats no wider than their word), and a
        // response reaching a source carries that source's own AxSIZE (the
        // target upsize answers merged bursts in the source's terms). The
        // wide benches check the delivered AxSIZE, strobes and data per beat.
        // A request run's bytes are its reach (its first beat's last byte and its extent) less its
        // start, plus one.
        // A response's count field holds its reach (message.hpp point 18).
        auto count = response ? tr_field("reach") : tr_field("extent");
        if (!response) {
          const auto width = b.ir.values.at(count.index).width + 1;
          const auto reach = differ(first_beat_last(address, tr_field("size"), width - 1), count);
          const auto end = b.unary(Op::ZeroExtend, reach, width);
          const auto all = b.constant(width, (std::uint64_t{1} << width) - 1); // minus one
          count = b.binary(Op::Subtract, b.binary(Op::Subtract, end, all),
                           run_start(address, word_bytes, width));
        }
        const auto bits = b.ir.values.at(ep.data[i].index).width,
                   have = b.ir.values.at(count.index).width;
        drive(ep.data[i], have == bits  ? count
                          : have > bits ? b.slice(count, 0, bits)
                                        : b.unary(Op::ZeroExtend, count, bits));
        continue;
      }
      drive(ep.data[i], tr_field(name));
    }
  }
};
// Resolve composition wires into the canonical order (see canonicalize): the
// numbering follows the structure, not the order the composition ran in. The
// storage records (in resource order) are hardware: the plan accounts their
// words as resource capacity. The bindings (in key order) only keep their
// logic for verification, witnesses and benches, except the crossing faces
// and admission anchors the artifact map publishes, which are hardware too.
Status order_exchange_ir(ExchangeHardware &h) {
  std::vector<ValueId> kept, observed;
  std::vector<const CrossStorageBinding *> stores;
  std::ranges::transform(h.storage, std::back_inserter(stores),
                         [](const CrossStorageBinding &s) { return &s; });
  std::ranges::stable_sort(stores, {}, &CrossStorageBinding::resource);
  for (const auto *s : stores) {
    kept.push_back(s->clock);
    kept.insert(kept.end(), s->payload.begin(), s->payload.end());
    kept.insert(kept.end(), s->control.begin(), s->control.end());
    kept.push_back(s->acquire);
    kept.push_back(s->release);
  }
  for (const auto &cut : h.plan.cuts)
    for (const auto *face : {"/input.", "/output."})
      for (auto it = h.bindings.lower_bound(cut.name + face);
           it != h.bindings.end() && it->first.starts_with(cut.name + face); ++it)
        kept.push_back(it->second);
  for (const auto &source : names(h.plan.sources))
    for (const auto *anchor :
         {"/wr.route_mapped", "/rr.route_mapped", "/write.route_target", "/read.route_target"})
      if (const auto it = h.bindings.find("source/" + source + anchor); it != h.bindings.end())
        kept.push_back(it->second);
  for (const auto &[key, id] : h.bindings)
    observed.push_back(id);
  const auto mapped = canonicalize(h.ir, kept, observed);
  if (mapped.empty())
    return Status::failure(error("NCL_EXCHANGE_COMBINATIONAL_CYCLE", "exchange.hardware",
                                 "Composition has a combinational cycle"));
  auto remap = [&](ValueId &id) { id.index = mapped[id.index]; };
  for (auto &[key, id] : h.bindings)
    remap(id);
  for (auto &s : h.storage) {
    remap(s.clock);
    remap(s.acquire);
    remap(s.release);
    for (auto &q : s.payload)
      remap(q);
    for (auto &q : s.control)
      remap(q);
  }
  return Status::success({});
}
} // namespace

namespace {
// Resolves the arbiter of every physical switch output with two or more candidates: a per-output
// override, else the common default policy. Candidates take
// hardware indices, here and in the graph outputs, in the order of the configured names of the
// endpoints and nodes they come from, a node before an endpoint of the same name. The capability
// rules judge the overrides.
void plan_arbiters(NetworkPlan &plan) {
  for (bool response : {false, true})
    for (auto &o : (response ? plan.exchange.response : plan.exchange.request).outputs) {
      std::ranges::sort(o.inputs, {}, [&](const std::string &key) {
        const auto name = plan.label(key);
        return std::pair{name.substr(name.find('/') + 1), key.starts_with("endpoint/")};
      });
      if (o.inputs.size() < 2)
        continue;
      const auto output = (o.endpoint ? "endpoint/" : "node/") + o.output;
      const auto key = std::string(response ? "rsp/" : "req/") + o.node + ">" + output;
      const auto request = plan.output_arbitration.find(key);
      ArbiterPlan a{response, o.node, output,
                    o.inputs, {},     request != plan.output_arbitration.end()};
      a.spec.policy = a.overridden
                          ? request->second.policy
                          : plan.arbitration.choose(static_cast<unsigned>(o.inputs.size()));
      if (a.overridden && a.spec.policy == treadle::Policy::FixedPriority)
        for (const auto &order = request->second.priority; const auto &candidate : o.inputs)
          a.spec.rank.push_back(
              static_cast<unsigned>(std::ranges::find(order, candidate) - order.begin()));
      plan.arbiters.emplace(key, std::move(a));
    }
}
} // namespace

// The definition checks and the capability rules judge the input; planning only derives and
// asserts its invariants.
Status plan_composition(NetworkPlan &plan) {
  // One row per routing window (MappingPlan).
  for (unsigned i = 0; i < plan.targets.size(); ++i)
    for (unsigned m = 0; m < plan.targets[i].mappings.size(); ++m)
      for (const auto &route : plan.targets[i].mappings[m].routes)
        plan.decode.push_back({route, i, m});
  std::ranges::sort(plan.decode, {},
                    [](const DecodeEntry &e) { return std::pair{e.route.base, e.target}; });
  auto exchange = plan_exchange(plan.exchange.request.graph, plan.exchange.response.graph);
  if (!exchange)
    return Status::failure(exchange.diagnostics());
  plan.exchange = std::move(exchange.value());
  build_route_index(plan);
  plan_arbiters(plan);
  // The route path (message.hpp point 14): request flows and response return paths, where some
  // switch input needs them.
  if (exit_split(plan.exchange.response) || flow_split(plan.exchange.request)) {
    auto widest = std::accumulate(
        plan.sources.begin(), plan.sources.end(), std::size_t{2},
        [](std::size_t most, const SourcePlan &s) { return std::max(most, s.flows.size()); });
    widest = std::accumulate(
        plan.exchange.response.routes.begin(), plan.exchange.response.routes.end(), widest,
        [&](std::size_t most, const SwitchRoute &route) {
          return std::max<std::size_t>(most, return_path(plan.exchange.response, route) + 1);
        });
    plan.burst_path_bits = static_cast<unsigned>(std::bit_width(widest - 1));
  }
  // The request scheduling level (message.hpp point 15) of sources that map their AxQOS.
  plan.burst_level_bits = plan.mapped() ? 3 : 0;
  plan.source_bits = std::max(1U, static_cast<unsigned>(std::bit_width(
                                      static_cast<unsigned>(2 * plan.sources.size() - 1))));
  // The network ID field takes the widest source ID.
  for (const auto &source : plan.sources)
    plan.burst_id_bits = std::max(plan.burst_id_bits, source.id_bits.value_or(0));
  plan.burst_address_bits = std::max(32U, plan.address_bits + plan.plane_bits);
  // The transport layout (message.hpp point 14): the widest target span and the target index.
  for (const auto &s : plan.sources)
    if (const auto d = default_target(plan, s.name))
      plan.targets[*d].fallback = true;
  plan.transport_address_bits = 12;
  for (const auto &t : plan.targets) {
    const auto span = transport_span(plan, t);
    plan.transport_address_bits =
        std::max(plan.transport_address_bits, span.keep ? span.keep : plan.burst_address_bits);
  }
  plan.transport_address_bits = std::min(plan.transport_address_bits, plan.burst_address_bits);
  if (plan.observes_miss_address)
    plan.transport_address_bits = plan.burst_address_bits;
  // The request destination index: each target, each flow of a target of several.
  const auto destinations =
      std::accumulate(plan.targets.begin(), plan.targets.end(), 0U,
                      [](unsigned n, const TargetPlan &t) { return n + t.flows; });
  plan.target_bits = static_cast<unsigned>(std::bit_width(destinations - 1));
  plan.exclusive = std::ranges::any_of(
      plan.targets, [](const auto &t) { return t.exclusive.mode != ExclusiveAccess::None; });
  // The response steering tag numbers the bursts in flight of the source with
  // the largest capacity; every capacity one omits it (message.hpp point 12).
  // Known before the width converters, whose merge state holds it.
  const auto widest_capacity = std::accumulate(
      plan.sources.begin(), plan.sources.end(), 1U,
      [](unsigned widest, const SourcePlan &s) { return std::max(widest, s.engine_slots); });
  plan.burst_tag_bits =
      widest_capacity > 1 ? static_cast<unsigned>(std::bit_width(widest_capacity - 1)) : 0U;
  if (plan.per_endpoint) {
    // Per-endpoint widths: every endpoint attaches at its own width and the
    // adapter ladder places each beat by address. A source wider than a
    // target it reaches clamps its bursts to that target's width, including a
    // single-beat AXI4-Lite source.
    for (const auto &pair : plan.pairs)
      if (auto &source = plan.sources[pair.source];
          source.engine_bits > plan.targets[pair.target].data_bits)
        source.clamp.emplace();
    // 64- and 128-byte endpoint words widen the ladder-global occupancy.
    for (const auto &source : plan.sources)
      plan.occupancy_bits = std::max(plan.occupancy_bits, burst_count_bits(source.engine_bits));
    for (const auto &target : plan.targets)
      plan.occupancy_bits = std::max(plan.occupancy_bits, burst_count_bits(target.data_bits));
    plan.laddered = true;
  }
  plan.burst_len_bits = network_len_bits(plan);
  require(plan.burst_len_bits, "A target AxLEN carries one beat of every source it serves");
  // The default width of a target ID at a target with several bursts in flight or its own exclusive
  // monitor: the widest source ID, the bits to count the sources, at least the flow and owner bits.
  // Ahead of what reads interleaved_target: a target may interleave through the ID given here.
  for (auto &target : plan.targets)
    if (target.len_bits && !target.id_bits)
      target.id_bits = std::max(whole_target_id_bits(plan, target), least_target_id_bits(target));
  // Targets wider than a source they serve merge its narrow beats.
  for (const auto &target : upsize_targets(plan)) {
    const auto index = plan.target_index(target);
    const auto bits = axi4_upsize_state_bits(upsize_profile(plan, index));
    require(bits.first && bits.second, "Every upsize merge has state");
    plan.targets[index].upsize = bits;
  }
  const unsigned ep_word = plan.burst_data_bits / 8;
  plan.laddered =
      plan.laddered ||
      std::ranges::any_of(plan.nodes, [&](const NodePlan &n) { return n.word_bytes != ep_word; }) ||
      std::ranges::any_of(plan.links,
                          [&](const LinkPlan &l) { return l.word.value_or(ep_word) != ep_word; });
  plan_links(plan);
  plan.response_runs = std::ranges::any_of(plan.links, [](const LinkPlan &link) {
    return link.response && std::ranges::any_of(link.stages, [](const StagePlan &s) {
             return s.kind == StageKind::Adapter && !s.serializer;
           });
  });
  // A run ends at a segment's end, so an interleaving target needs the marker on them too.
  plan.response_segments =
      std::ranges::any_of(plan.targets,
                          [&](const TargetPlan &t) { return interleaved_target(plan, t); }) &&
      (plan.response_runs ||
       std::ranges::any_of(plan.arbiters, [](const auto &a) { return a.second.response; }));
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    // AXI4-Lite sources issue single beats: nothing to cut. A packet table below its alignment
    // word is a capability violation and has no splitter.
    const auto profile = packet_profile(plan, si);
    if (plan.sources[si].protocol != Protocol::Axi4Lite &&
        std::ranges::none_of(profile.windows,
                             [](const auto &w) { return w.boundary_bytes < w.align_bytes; }))
      plan.sources[si].packets = axi4_packetizer_state_bits(profile);
  }
  // Outstanding and packet tables are known only once source capacities and packets resolve.
  for (unsigned si = 0; si < plan.sources.size(); ++si)
    if (plan.sources[si].clamp) {
      const auto bits = axi4_width_clamp_state_bits(clamp_profile(plan, si));
      require(bits.first && bits.second, "Every width clamp has state");
      plan.sources[si].clamp = bits;
    }
  plan_endpoint_pipes(plan);
  plan.graph = build_axi4_exchange_obligations(plan);
  return Status::success({});
}

namespace {
using Imported = Compose::ImportedEndpoint;
// Storage word names of one engine: payload, then control.
using Names = std::pair<std::vector<std::string>, std::vector<std::string>>;
// The mode plane a source engine direction holds for its burst in service, with its hold flag.
void plane_words(const std::map<std::string, ValueId> &states, const std::string &dir,
                 Names &words) {
  if (states.contains(dir + ".plane")) {
    words.first.push_back(dir + ".plane");
    words.second.push_back(dir + ".plane_held");
  }
}
// Register one endpoint engine's storage records and event aliases; every
// imported state word lands in exactly one record (checked by exhaustion).
void consume_states(const Imported &ep, const Names &words, std::set<std::string> &assigned,
                    std::vector<ValueId> &payload_ids, std::vector<ValueId> &control_ids) {
  auto take = [&](const std::vector<std::string> &list, std::vector<ValueId> &ids) {
    for (const auto &name : list) {
      if (!assigned.insert(name).second || !ep.states.contains(name))
        throw std::logic_error("Endpoint state assignment fault for " + name);
      ids.push_back(ep.states.at(name));
    }
  };
  take(words.first, payload_ids);
  take(words.second, control_ids);
}
// A read-only endpoint exposes only AR/R, a write-only one only AW/W/B; the engine of the other
// direction answers locally.
void remove_one_way_ports(const NodeHardware &component, std::map<std::string, unsigned> &ports,
                          bool read_only, bool write_only) {
  for (const auto &port : component.ir.ports) {
    const auto &s = port.signal;
    if (port.endpoint == "axi" &&
        ((read_only && (s.starts_with("aw") || s.starts_with("w") || s.starts_with("b"))) ||
         (write_only && (s.starts_with("ar") || s.starts_with("r")))))
      ports[s] = 0;
  }
}
// The descriptor words under a stem: address, length, size, burst, lock, cache, protection and
// the ID, the user flags, the AxQOS map's level and the security flags when there are some.
void add_descriptor(std::vector<std::string> &words, const std::string &stem, bool id, bool user,
                    bool level = false, bool security = false) {
  for (const auto *field : {"addr", "len", "size", "burst", "lock", "cache", "prot"})
    words.push_back(stem + field);
  if (id)
    words.push_back(stem + "id");
  if (user)
    words.push_back(stem + "user");
  if (level)
    words.push_back(stem + "level");
  if (security)
    words.push_back(stem + "security");
}
// A source engine's words besides its descriptor. An APB face holds the transfer: the engine keeps
// its progress and no payload.
void add_source_progress(Names &words, bool read, bool apb, bool lite, bool tracked) {
  auto &[fields, controls] = words;
  if (apb) {
    controls =
        read ? Names::second_type{"rr.state"} : Names::second_type{"wr.state", "wr.beat_full"};
  } else if (!read) {
    fields.push_back("wr.beat_addr");
    controls = {"wr.state", "wr.remaining", "wr.poisoned"};
    // A Lite engine's beat buffer; a full AXI4 engine passes W beats through.
    if (lite) {
      fields.insert(fields.end(), {"wr.beat_data", "wr.beat_be"});
      controls.push_back("wr.beat_full");
    }
  } else if (tracked)
    controls = {"rr.state", "rr.beat", "rr.tag", "rr.local_burst", "rr.offered"};
  else
    controls = {"rr.state", "rr.remaining"};
}
// The ID and source words of a stem, where the network carries them.
void add_id_source(std::vector<std::string> &words, const std::string &stem, unsigned id_bits,
                   unsigned source_bits) {
  if (id_bits)
    words.push_back(stem + "id");
  if (source_bits)
    words.push_back(stem + "source");
}
// The AXI4 outlet tables: flags, counts and pointers are control; descriptors, staging and slot
// fields are payload.
bool axi4_outlet_control(const std::string &name) {
  static const std::set<std::string> controls{
      "w.busy",         "w.full",           "w.done",          "w.aw_pending", "w.reject",
      "w.bad",          "w.remaining",      "w.response_full", "w.miss",       "r.count",
      "r.unissued",     "r.head",           "r.tail",          "r.issue",      "r.full",
      "r.beat",         "r.response_first", "r.response_last", "pool.count",   "w.head_pending",
      "w.head_beat_in", "pool.read_first"};
  // The W, B and R stage flags and the read receive pointer.
  static const std::set<std::string> stage_controls{
      "w.spare",           "w.beat_last",      "w.beat_drop", "w.spare_last",  "w.spare_drop",
      "r.spare",           "r.receive",        "r.received",  "r.spare_first", "r.spare_last",
      "r.spare_exclusive", "w.response_spare", "w.b_parked"};
  // The read entries' receive state and the delayed pool releases.
  static const std::set<std::string> entry_controls{
      "r.current", "r.response_index", "r.spare_index", "pool.read_release", "pool.write_release"};
  // The joined response stage of an interleaving outlet: its flags and the merge state.
  static const std::set<std::string> join_controls{"r.out_write",     "r.join_mid",
                                                   "r.join_index",    "r.merge_phase",
                                                   "r.segment_phase", "r.join_block"};
  return controls.contains(name) || stage_controls.contains(name) ||
         entry_controls.contains(name) || join_controls.contains(name) || name.ends_with(".beat") ||
         name.ends_with(".decode") || name.starts_with("r.age") || name.ends_with(".valid") ||
         name.ends_with(".local") || name.ends_with(".bad") || name.ends_with(".ahead") ||
         name.ends_with(".reject") || name.ends_with(".miss") || name.ends_with(".lock") ||
         name.ends_with(".response_exclusive") || name.ends_with(".tag") ||
         name.ends_with(".exclusive_fail") || name.ends_with(".issued") ||
         name.ends_with(".received") || name == "r.local_error" || name == "r.spare_local_error" ||
         name == "w.fixed_shown" || name.ends_with(".saved_valid");
}

// A full AXI4 or AXI3 source with one burst in flight and no width conversion holds a request at
// its engine while the request merger cannot take it.
bool holds_requests(const SourcePlan &sp, bool convert) {
  return sp.outstanding == 1 && !sp.ahb && !convert &&
         (sp.protocol == Protocol::Axi4 || sp.protocol == Protocol::Axi3);
}

// Lowers one planned network in stages: each source with its splitter and converter, the request
// transport, each target with its outlet, then the response transport back to the sources.
class Lowering {
public:
  Lowering(const NetworkPlan &network, RegisterReset register_reset) : plan(network), b(c.b) {
    b.ir.name = "nocloom_" + plan.name;
    b.ir.synchronous_reset = register_reset == RegisterReset::Synchronous;
    c.out.plan = plan;
    c.add_domains(plan.clocked() ? plan.domains : std::vector<DomainPlan>{plan.domain});
    add_address_selectors();
    c.endpoint_request_fields = burst_request_fields(plan.burst_data_bits, burst, plan.source_bits);
    c.endpoint_response_fields =
        burst_response_fields(plan.burst_data_bits, burst, plan.source_bits);
  }
  Result<ExchangeHardware> run() {
    for (unsigned si = 0; si < plan.sources.size(); ++si)
      attach_source(si);
    Channels entries, responses;
    for (unsigned si = 0; si < plan.sources.size(); ++si)
      entries.emplace(plan.sources[si].name, sides[si].req_tr);
    const auto arrivals = compose_transport(false, entries);
    // Per target: split the request lane by the write field, attach the
    // burst service engine behind the window shim, and merge its responses.
    for (unsigned o = 0; o < plan.targets.size(); ++o)
      responses.emplace(plan.targets[o].name, attach_target(o, arrivals.at(plan.targets[o].name)));
    attach_fault_monitors();
    return_responses(compose_transport(true, responses));
    c.gate_regions();
    c.out.ir = std::move(b.ir);
    if (auto status = order_exchange_ir(c.out); !status)
      return Result<ExchangeHardware>::failure(status.diagnostics());
    if (auto status = verify_network_decode(c.out); !status)
      return Result<ExchangeHardware>::failure(status.diagnostics());
    if (auto status = verify_axi4_exchange_composition(c.out); !status)
      return Result<ExchangeHardware>::failure(status.diagnostics());
    return Result<ExchangeHardware>::success(std::move(c.out));
  }

private:
  using Channels = std::map<std::string, Channel>;
  struct SourceSide {
    Imported ep;
    Channel req_tr;           // both directions, transport layout at the endpoint word
    Channel wrsp_ep, rrsp_ep; // endpoint layout, driven by the response split
    // The shared pool of units in flight (rejoin_requests); pool_answers drives its response
    // side from the returning response stream.
    std::optional<Compose::Pool> pool;
    // A mode-dependent source: whether the request merger holds the other direction's burst
    // (write, read), which keeps this direction's next request in its engine.
    std::optional<std::array<ValueId, 2>> merge_held;
    // A source with AXI port pipes: its read request's final packet enters the request merger,
    // which lets the AR leave the port pipe (axi4_initiator.hpp port_pipes).
    std::optional<ValueId> read_taken;
    // The network entry drops a write decode miss packet's fragments after its head (join_source).
    std::optional<ValueId> miss_dropping, miss_entered, miss_drop_state;
    // The request merger holds a packet into another target than its burst's previous one
    // (Axi4PacketizerProfile::merger_holds); the target of the last packet head it passed.
    bool merger_holds = false;
    std::vector<ValueId> last_target;
    // A local miss descriptor's one word being answered (expand_read).
    std::vector<ValueId> read_expand;
  };
  // One target between the network and its outlet: the endpoint-layout faces, the response
  // merger's registers, the packet records and, on a shared bus, the arrival order.
  struct TargetSide {
    std::string key;
    unsigned word = 0, bits_ep = 0, count_bits = 3;
    bool shared_bus = false, upsize = false;
    ValueId count{}, turn{};
    // A default target's decode miss head seen and a miss being answered (attach_target), with
    // count and turn as bus control.
    std::optional<ValueId> miss_seen, miss_answer, miss_replied, empty;
    // An AXI4-Lite target of several flows: the flow of the packets in flight (attach_target).
    std::optional<ValueId> flow;
    Channel wreq{}, rreq{}, wrsp{}, rrsp{};
    std::vector<ValueId> merger;
    // An interleaving outlet's segment end (hint output cut), where the transport carries it.
    std::optional<ValueId> cut;
    // Where the target's responses end a run (hint output wrap_end, message.hpp point 18).
    std::optional<ValueId> wrap_end;
    // The response merger's output carries the (write, read) face
    // (Axi4OutletProfile::merger_hints).
    std::optional<std::array<ValueId, 2>> picked;
    Imported merge;
    std::array<ValueId, 2> acquired{}, released{};
    // An AXI4 outlet admitting heads ahead of it: whether a (write, read) head may enter its
    // dispatch stage, and the heads entering it.
    std::optional<std::array<ValueId, 2>> admit_ok, admits;
  };
  const NetworkPlan &plan;
  Compose c;
  Builder &b;
  const BurstProfile burst = endpoint_burst(plan);
  std::vector<SourceSide> sides;
  // Error loggers: the bank's fields and controls, the events and context of observed targets.
  std::map<std::string, ValueId> logger_fields, logger_controls;
  std::map<std::string, std::map<std::string, ValueId>> observed_events;
  // An observed AXI4 target's RVALID and RREADY, for its events.
  std::map<unsigned, std::pair<ValueId, ValueId>> observed_reads;
  // The targets that answer some source's decode misses, found once for all targets.
  std::optional<std::set<unsigned>> fallbacks;
  // Per target, whether the response leaving it answers a decode miss (attach_target): it returns
  // over its source's miss route.
  std::map<std::string, ValueId> miss_answers;

  // Endpoint-layout field widths at one endpoint's data width; field order
  // does not depend on the width.
  std::vector<unsigned> ep_widths(bool response, unsigned bits = 0) const {
    std::vector<unsigned> widths;
    for (const auto &[name, width] :
         bits ? (response ? burst_response_fields(bits, burst, plan.source_bits)
                          : burst_request_fields(bits, burst, plan.source_bits))
              : (response ? c.endpoint_response_fields : c.endpoint_request_fields))
      widths.push_back(width);
    return widths;
  }
  // The four endpoint-layout faces of an attachment at its word: the write and
  // read request faces, then the write and read response faces.
  std::tuple<Channel, Channel, Channel, Channel> ep_faces(unsigned bits_ep) {
    return {c.channel(ep_widths(false, bits_ep)), c.channel(ep_widths(false, bits_ep)),
            c.channel(ep_widths(true, bits_ep)), c.channel(ep_widths(true, bits_ep))};
  }
  unsigned word_at(const std::string &endpoint) const { return endpoint_word(plan, endpoint); }
  unsigned word_of(const std::string &node) const { return plan.node(node).word_bytes; }
  const LinkPlan &link(const std::string &name) const {
    const auto it = std::ranges::find(plan.links, name, &LinkPlan::name);
    if (it == plan.links.end())
      throw std::logic_error("Unplanned link " + name);
    return *it;
  }

  // The planned pipe of an endpoint at place on a channel, in the gate region object.
  Channel endpoint_pipe(const std::vector<EndpointPipe> &pipes, std::string_view place,
                        const std::string &object, const Channel &in, bool response = false) {
    const auto it = std::ranges::find(pipes, place, pipe_place);
    if (it == pipes.end())
      return in;
    auto piped = c.pipeline(it->name, in, it->mode, response);
    c.hold_open(c.region(object), c.out.storage.size() - 1);
    return piped;
  }

  // Selector k is bit k in name order, set when all listed address inputs match.
  std::optional<ValueId> address_selectors;
  void add_address_selectors() {
    std::map<std::string, ValueId> ports;
    for (const auto &port : plan.address_inputs)
      ports[port.name] =
          b.input(port.name, port.width, "address_input:" + port.name, "address_input");
    for (const auto &flag : plan.address_selectors) {
      auto all = c.bit(1);
      for (const auto &[name, value] : flag.equals)
        all = c.and_(all, b.binary(Op::Equal, ports.at(name),
                                   b.constant(b.ir.values.at(ports.at(name).index).width, value)));
      c.bind("mode/" + flag.name, all);
      address_selectors = address_selectors ? b.concat(all, *address_selectors) : all;
    }
  }

  // A source's network entry passes a write decode miss packet's head as a whole packet and takes
  // the fragments after it without passing them (dropping, from the head to the packet's tail), as
  // the default target takes the head and answers it while the beats are still coming.
  Channel drop_miss_beats(const std::string &id, const Channel &in, SourceSide &side,
                          unsigned word) {
    const auto field = [&](const char *name) {
      return in.data[c.burst_field_index(false, word, name)];
    };
    const auto state = *(side.miss_drop_state = b.state(1, c.clock, c.reset));
    c.drive(*side.miss_dropping, state);
    Channel out{c.wire(1), c.wire(1), in.data};
    const auto last = field("last");
    // A write into a target that refuses writes (its packet one AxLEN 0 head) drops likewise.
    auto refused = field("error");
    unsigned index = 0;
    for (const auto &t : plan.targets)
      for (unsigned f = 0; f < t.flows; ++f, ++index)
        if (t.read_only)
          refused = c.or_(refused, plan.target_bits ? c.eq(field("target"), index) : c.bit(1));
    const auto head = c.and_(c.and_(field("first"), refused), c.and_(field("write"), c.not_(last)));
    c.drive(out.valid, c.and_(in.valid, c.not_(state)));
    out.data[c.burst_field_index(false, word, "last")] = c.or_(last, head);
    c.drive(in.ready, c.or_(state, out.ready));
    const auto start = c.and_(c.transfer(out), head), end = c.and_(state, c.and_(in.valid, last));
    b.update(state, b.select(state, c.not_(end), start));
    if (side.miss_entered)
      c.drive(*side.miss_entered, start);
    if (auto *zone = c.region(id.substr(0, id.rfind('/'))))
      zone->idle.push_back(c.not_(state));
    return out;
  }

  // The producer side of a channel that passes it only while blocked is low.
  Channel gate(const Channel &down, ValueId blocked) {
    auto up = c.like(down);
    c.drive(down.valid, c.and_(up.valid, c.not_(blocked)));
    for (std::size_t f = 0; f < down.data.size(); ++f)
      c.drive(down.data[f], up.data[f]);
    c.drive(up.ready, c.and_(down.ready, c.not_(blocked)));
    return up;
  }
  // A request waiting at its port competes for the merger: its zones stay open for it.
  static void add_waiting_wakes(const SourceSide &side, GateRegion &zone) {
    for (const auto *hint : {"write_port", "read_port"})
      if (const auto it = side.ep.bound.find(hint); it != side.ep.bound.end())
        zone.wake.push_back(it->second);
  }
  void wake_source_face(const SourcePlan &sp, const Imported &ep, GateRegion &zone) {
    for (const auto *signal : {"awvalid", "wvalid", "arvalid", "psel"})
      if (ep.axi.contains(signal))
        zone.wake.push_back(ep.axi.at(signal));
    if (sp.ahb)
      zone.wake.push_back(c.and_(ep.axi.at("hsel"), c.not_(c.eq(ep.axi.at("htrans"), 0))));
  }
  // One source: the initiator, its packet splitter and its width converter, from the AXI face to
  // the endpoint-layout request and response faces at the source's attachment word.
  void attach_source(unsigned si) {
    const auto &sp = plan.sources[si];
    const auto &source = sp.name;
    c.select_object("source/" + source);
    const auto source_data = sp.engine_bits;
    const auto key = "source/" + source;
    SourceSide side;
    // Per-endpoint widths attach the source at its own width, no converter.
    const unsigned source_bits_ep = plan.per_endpoint ? word_at(source) * 8 : 0;
    Channel wreq_ep{}, rreq_ep{};
    std::tie(wreq_ep, rreq_ep, side.wrsp_ep, side.rrsp_ep) = ep_faces(source_bits_ep);
    auto wide_widths = [&](bool response) { return ep_widths(response, source_data); };
    // A clamped source keeps its own layout on both converter faces.
    const bool convert = sp.clamp.has_value();
    auto [front_wreq, front_rreq, front_wrsp, front_rrsp] =
        convert ? ep_faces(source_data) : std::tuple{wreq_ep, rreq_ep, side.wrsp_ep, side.rrsp_ep};
    // Bounded packets: the splitter sits between the initiator and the
    // width conversion, on the source layout.
    const bool packetize = sp.packets.has_value();
    const bool reads_by_granule = reads_granule_target(plan, si);
    // A read-only source's write faces bypass the splitter.
    const bool packetize_write = packetize && !sp.read_only;
    Channel taken_wrsp = held_response(side, packetize_write);
    if (!convert)
      front_wrsp = taken_wrsp;
    Channel init_wreq = packetize_write ? c.channel(wide_widths(false)) : front_wreq;
    Channel init_rreq = packetize ? c.channel(wide_widths(false)) : front_rreq;
    Channel init_wrsp = packetize_write ? c.channel(wide_widths(true)) : front_wrsp;
    // A source with several beats answers a local miss descriptor's one word (decode_packet.hpp)
    // here.
    Channel answered_rrsp = answered_channel(sp, front_rrsp);
    Channel init_rrsp = packetize ? c.channel(wide_widths(true)) : answered_rrsp;
    Imported converter, splitter;
    // Behind a clamp a decode packet's word decides late whether it ends a beat: the protocol zone
    // wakes on the clamp's fragment in front instead (r.up_wake, a splitter's held word).
    const auto read_wake = convert && expands_reads(sp) ? c.wire(1) : init_rrsp.valid;
    const auto profile = initiator_profile(plan, si);
    const auto initiator = admitted(lower_axi4_initiator(profile));
    // Narrower ID/length ports and one-direction sources change only the
    // external face; the endpoint keeps the network-wide widths.
    std::map<std::string, unsigned> source_ports;
    if (sp.id_bits)
      for (const auto *signal : {"awid", "arid", "bid", "rid"})
        source_ports[signal] = *sp.id_bits;
    if (sp.id_bits && sp.axi3)
      source_ports["wid"] = *sp.id_bits;
    if (sp.len_bits)
      for (const auto *signal : {"awlen", "arlen"})
        source_ports[signal] = *sp.len_bits;
    remove_one_way_ports(initiator, source_ports, sp.read_only, sp.write_only);
    auto first = b.ir.states.size();
    const auto stores = c.out.storage.size();
    std::map<std::string, ValueId> modes;
    if (address_selectors)
      modes["address_selectors"] = *address_selectors;
    if (profile.port_pipes)
      modes["read_taken"] = *(side.read_taken = c.wire(1));
    // Mode-dependent requests wait before packetization for capacity and the other direction.
    // A full AXI4 or AXI3 source with one burst also holds its AW or AR in the port pipe.
    auto engine_wreq = init_wreq, engine_rreq = init_rreq;
    if (plan.decodes_modes(si) || holds_requests(sp, convert)) {
      side.merge_held = {c.wire(1), c.wire(1)};
      engine_wreq = gate(init_wreq, (*side.merge_held)[0]);
      engine_rreq = gate(init_rreq, (*side.merge_held)[1]);
    }
    side.ep = c.import_endpoint(initiator, key + "/", "nl_i_" + source + "_", "initiator:" + source,
                                {{"wreq", &engine_wreq},
                                 {"wrsp", &init_wrsp},
                                 {"rreq", &engine_rreq},
                                 {"rrsp", &init_rrsp}},
                                source_ports, modes, sp.pipes);
    // The protocol zone wakes on a valid of the source face (an APB PSEL) or a response.
    if (auto *zone = c.region(key + "/protocol")) {
      zone->claim(b, first);
      c.hold_open(zone, stores);
      zone->idle.push_back(side.ep.states.contains("rr.offered")
                               ? at_rest(b, side.ep.states, initiator_multi_rest)
                               : at_rest(b, side.ep.states, initiator_rest));
      wake_source_face(sp, side.ep, *zone);
      zone->wake.insert(zone->wake.end(), {init_wrsp.valid, read_wake});
      add_waiting_wakes(side, *zone);
    }
    first = b.ir.states.size();
    expand_read(sp, side, front_rrsp, answered_rrsp);
    if (packetize) {
      std::map<std::string, Channel *> faces{{"up_rreq", &init_rreq},
                                             {"up_rrsp", &init_rrsp},
                                             {"down_rreq", &front_rreq},
                                             {"down_rrsp", &answered_rrsp}};
      if (packetize_write)
        faces.insert({{"up_wreq", &init_wreq},
                      {"up_wrsp", &init_wrsp},
                      {"down_wreq", &front_wreq},
                      {"down_wrsp", &front_wrsp}});
      // AxLOCK follows the producer's current request, independently of
      // the Weft lock (which admission clears for ordinary targets); the producer's own addresses
      // feed the splitter's window decode.
      const auto packets = packet_profile(plan, si);
      side.merger_holds = packets.merger_holds;
      std::map<std::string, ValueId> hints;
      if (sp.split_narrow || exact_packets(plan, si))
        hints = {{"write_lock", side.ep.bound.at("write_lock")},
                 {"read_lock", side.ep.bound.at("read_lock")}};
      if (packets.source_miss)
        for (const auto *signal : {"write_start", "write_address", "write_length", "read_start"})
          if (packets.write || std::string_view(signal) == "read_start")
            hints[signal] = side.ep.bound.at(signal);
      if (packets.miss_drop)
        hints["write_dropping"] = *(side.miss_dropping = c.wire(1));
      if (packets.miss_lag)
        hints["write_miss_entered"] = *(side.miss_entered = c.wire(1));
      splitter = c.import_endpoint(admitted(lower_axi4_packetizer(packets)), key + "/packets/", "",
                                   "", faces, {}, hints);
    }
    if (convert) {
      auto lowered_converter = admitted(lower_axi4_width_clamp(clamp_profile(plan, si, true)));
      // Behind the splitter the converter takes each packet's burst-final marker.
      std::map<std::string, ValueId> finals;
      if (packetize)
        finals = {
            {"write_final", packetize_write ? splitter.bound.at("w.burst_final_packet") : c.bit(1)},
            {"read_final", splitter.bound.at("r.burst_final_packet")}};
      if (packetize && reads_by_granule)
        finals["read_taken_packet"] = splitter.bound.at("r.burst_taken_packet");
      // The clamp plans each packet from the splitter's handed start and run end.
      for (const std::string direction : {"write", "read"})
        if (packetize && (packetize_write || direction == "read"))
          for (const std::string field :
               {"start", "end", "size", "beat", "length", "key", "moved", "address"})
            if (direction == "write" || field != "address")
              finals[direction + "_packet_" + field] =
                  splitter.bound.at(direction + "_packet_" + field);
      converter = c.import_endpoint(lowered_converter, key + "/conversion/", "", "",
                                    {{"up_wreq", &front_wreq},
                                     {"up_wrsp", &front_wrsp},
                                     {"up_rreq", &front_rreq},
                                     {"up_rrsp", &front_rrsp},
                                     {"down_wreq", &wreq_ep},
                                     {"down_wrsp", &taken_wrsp},
                                     {"down_rreq", &rreq_ep},
                                     {"down_rrsp", &side.rrsp_ep}},
                                    {}, finals);
      drive_read_wake(sp, read_wake, converter, splitter);
    }
    if (packetize_write)
      release_response(side, taken_wrsp, splitter);
    join_source(si, side, {wreq_ep, rreq_ep}, convert, converter, splitter);
    // The transport zone: the splitter, the converter and the request merger; it wakes on a
    // request of the protocol zone or an arriving response (return_responses).
    if (auto *zone = c.region(key)) {
      zone->claim(b, first);
      if (!side.read_expand.empty())
        zone->idle.push_back(c.not_(side.read_expand.front()));
      zone->idle.insert(zone->idle.end(), {at_rest(b, splitter.states, packetizer_rest),
                                           at_rest(b, converter.states, width_cut_rest)});
      zone->wake.insert(zone->wake.end(), {init_wreq.valid, init_rreq.valid});
      add_waiting_wakes(side, *zone);
      c.producers[side.req_tr.valid.index] = key;
    }
    sides.push_back(std::move(side));
  }

  // With error codes a read's dropped words reach the response valid: a gate's wake reads the
  // occupancy it implies instead, off the first beat's result and the ID's decode. Clock stays on a
  // little longer; no state differs.
  ValueId tail_wake(const TargetSide &t, ValueId tail) {
    const auto busy = t.merge.states.find("r.busy");
    return plan.error_codes && busy != t.merge.states.end() ? busy->second : tail;
  }
  ValueId read_face_wake(const TargetSide &t, const Imported &ep, ValueId valid) {
    const auto full = ep.states.find("r.full");
    return plan.error_codes && t.upsize && full != ep.states.end() ? full->second : valid;
  }
  bool expands_reads(const SourcePlan &sp) const {
    return plan.error_codes && !sp.write_only && !sp.apb &&
           (sp.protocol != Protocol::Axi4Lite || sp.clamp.has_value());
  }
  Channel answered_channel(const SourcePlan &sp, const Channel &model) {
    if (!expands_reads(sp))
      return model;
    std::vector<unsigned> widths;
    std::ranges::transform(model.data, std::back_inserter(widths),
                           [&](ValueId value) { return b.ir.values.at(value.index).width; });
    return c.channel(widths);
  }
  void drive_read_wake(const SourcePlan &sp, ValueId wake, const Imported &converter,
                       const Imported &splitter) {
    if (!expands_reads(sp))
      return;
    const auto held = splitter.states.find("r.word_full");
    const auto clamp_wake = converter.bound.at("r.up_wake");
    c.drive(wake, held == splitter.states.end() ? clamp_wake : c.or_(clamp_wake, held->second));
  }
  // Answers a local miss descriptor's one word (decode_packet.hpp) as its beats, one a cycle: the
  // word waits on the down face, its data read as zero, until the last one leaves.
  void expand_read(const SourcePlan &sp, SourceSide &side, const Channel &down, const Channel &up) {
    if (!expands_reads(sp))
      return;
    const bool plan_flag = decode_flag(plan);
    const auto at = [&](const char *name) { return c.endpoint_field_index(true, name); };
    const auto field = [&](const char *name) { return down.data[at(name)]; };
    const auto data = field("data");
    const auto data_bits = b.ir.values.at(data.index).width, bits = plan.burst_len_bits;
    auto length = b.slice(data, 0, bits);
    for (unsigned g = 32; g < data_bits; g += 32)
      length = c.or_(length, b.slice(data, g, bits));
    const auto more = b.state(1, c.clock, c.reset), left = b.state(bits, c.clock, c.reset);
    side.read_expand = {more, left};
    const auto packet = plan_flag ? field("error") : c.eq(field("result"), 3);
    const auto ending = c.eq(left, 1), spent = c.eq(left, 0);
    // A word of length zero (a clamp's word ending one beat of several) answered its beat already:
    // the state drops it in a cycle of its own, since readiness never reads a word's data.
    const auto dropped = c.and_(more, spent);
    const auto fire = c.and_(down.valid, c.or_(dropped, up.ready));
    const auto step = b.binary(Op::Subtract, left, b.constant(bits, 1));
    b.update(left, b.select(fire, b.select(more, b.select(spent, left, step), length), left));
    b.update(more,
             b.select(fire, b.select(more, c.and_(c.not_(ending), c.not_(spent)), packet), more));
    c.drive(up.valid, c.and_(down.valid, c.not_(dropped)));
    c.drive(down.ready, c.or_(dropped, c.and_(up.ready, b.select(more, ending, c.not_(packet)))));
    for (unsigned i = 0; i < down.data.size(); ++i)
      if (i != at("first") && i != at("last") && i != at("data") && i != at("error") &&
          i != at("result"))
        c.drive(up.data[i], down.data[i]);
    c.drive(up.data[at("error")], plan_flag ? b.constant(1, 0) : field("error"));
    c.drive(up.data[at("result")],
            plan_flag ? field("result") : b.select(packet, b.constant(2, 1), field("result")));
    c.drive(up.data[at("first")], c.and_(field("first"), c.not_(more)));
    // A packet's first beat is its word's last only with length zero.
    c.drive(up.data[at("last")],
            c.and_(field("last"), b.select(more, ending, c.or_(c.not_(packet), c.eq(length, 0)))));
    c.drive(up.data[at("data")], b.select(c.or_(more, packet), b.constant(data_bits, 0), data));
  }
  // The write response the conversion (else the splitter) takes from the network: with a splitter
  // it is held while the network entry drops a decode miss packet's beats (release_response).
  Channel held_response(const SourceSide &side, bool packetize_write) {
    if (!packetize_write)
      return side.wrsp_ep;
    return {c.wire(1), c.wire(1), side.wrsp_ep.data};
  }
  void release_response(const SourceSide &side, const Channel &taken, const Imported &splitter) {
    const auto it = splitter.bound.find("w.miss_hold");
    const auto hold = it == splitter.bound.end() ? c.bit(0) : it->second;
    c.drive(taken.valid, c.and_(side.wrsp_ep.valid, c.not_(hold)));
    c.drive(side.wrsp_ep.ready, c.and_(taken.ready, c.not_(hold)));
  }
  // A later packet of a burst whose target differs from the previous packet's (the target of the
  // last packet head the merger passed) waits while any earlier packet of its burst is unanswered
  // (the burst's pool unit, by direction and tag), so responses keep packet order. The merger
  // holds a burst from its first packet to its final one, so one register serves both directions.
  void hold_packets(unsigned si, SourceSide &side, const std::array<Channel, 2> &framed,
                    const Compose::Rejoined &rejoined, const std::array<ValueId, 2> &held_back,
                    unsigned word) {
    const auto &sp = plan.sources[si];
    if (!plan.target_bits || !rejoined.pool)
      throw std::logic_error("Packets into several targets need the target field and the pool");
    const auto key = "source/" + sp.name;
    const auto &bound = c.out.bindings;
    const auto held = bound.at(key + "/request.held"), owner = bound.at(key + "/request.owner");
    const auto field = [&](const Channel &ch, const char *name) {
      return ch.data[c.burst_field_index(false, word, name)];
    };
    const auto &sink = rejoined.sink;
    const auto last = side.last_target.emplace_back(b.state(plan.target_bits, c.clock, c.reset));
    b.update(last,
             b.select(c.and_(c.transfer(sink), field(sink, "first")), field(sink, "target"), last));
    // The merger's candidate of each direction: a full AXI4 source's read face is candidate 0,
    // an AXI4-Lite source's write face.
    const unsigned read = sp.protocol == Protocol::Axi4Lite ? 1 : 0;
    for (unsigned d = 0; d < 2; ++d) {
      const auto &face = framed[d];
      const unsigned at = d ? read : 1 - read;
      // The pool's units by write bit: a write's are units[1].
      const auto &units = rejoined.pool->units[d ? 0 : 1];
      const auto tag = plan.burst_tag_bits ? field(face, "tag") : c.bit(0);
      auto pending = c.bit(0);
      for (unsigned t = 0; t < units.size(); ++t) {
        const auto open = c.not_(c.eq(units[t].packets, 0));
        pending = c.or_(pending, units.size() > 1 ? c.and_(c.eq(tag, t), open) : open);
      }
      const auto later = c.and_(held, c.eq(owner, at));
      const auto apart = c.not_(b.binary(Op::Equal, field(face, "target"), last));
      c.drive(held_back[d], c.and_(c.and_(field(face, "first"), later), c.and_(apart, pending)));
    }
  }

  struct RegulatorSource {
    LinkPipeline packets_mode, issue_mode;
    std::optional<RegulatorFront> front;
  };
  RegulatorSource regulator_source(unsigned si) {
    const auto &sp = plan.sources[si];
    RegulatorSource result;
    for (const auto &pipe : sp.pipes) {
      if (pipe.name.ends_with("/pipe/packets") || pipe.name.ends_with("/pipe/packets/write"))
        result.packets_mode = pipe.mode;
      if (pipe.name.ends_with("/pipe/issue"))
        result.issue_mode = pipe.mode;
    }
    if (sp.qos.picks() &&
        (result.packets_mode.forward || result.packets_mode.backward || result.issue_mode.forward))
      result.front = lower_rate_regulator_front(b, c.clock, c.reset, c.wire(1), c.wire(1),
                                                budget_levels(*sp.qos.shaper, *sp.qos.scheduler),
                                                sp.outstanding);
    if (result.front && initiator_profile(plan, si).late_id_binding)
      result.front->conflict = c.wire(1);
    if (result.front && sp.request_limit) {
      result.front->answered = c.wire(1);
      result.front->request_limit = sp.request_limit;
    }
    return result;
  }
  void finish_source_entry(unsigned si, SourceSide &side, unsigned word,
                           Compose::Rejoined &rejoined,
                           std::optional<RegulatorFront> &regulator_front,
                           const LinkPipeline &packets_mode, const LinkPipeline &issue_mode) {
    const auto &sp = plan.sources[si];
    const auto key = "source/" + sp.name;
    auto entry = rejoined.sink;
    if (sp.qos.holds())
      entry = limit(key + "/limiter", entry, *sp.qos.shaper, word, sp);
    if (regulator_front && !packets_mode.forward && !packets_mode.backward)
      c.drive(regulator_front->presented,
              c.or_(entry.valid, rejoined.pool
                                     ? c.or_(rejoined.pool->head_waits, rejoined.pool->lacks_unit)
                                     : c.bit(0)));
    if (sp.qos.picks())
      entry = regulate(si, entry, side, word, rejoined.pool,
                       regulator_front ? &*regulator_front : nullptr, packets_mode, issue_mode,
                       rejoined.source_level, rejoined.qos_words);
    side.req_tr = regulator_front && issue_mode.forward
                      ? entry
                      : endpoint_pipe(sp.pipes, "issue", key, entry);
    if (side.miss_dropping)
      side.req_tr = drop_miss_beats(key + "/miss_drop", side.req_tr, side, word);
    // The entry presses with its packet head's level, a regulator with its own compensated level.
    if (c.level_hint_width())
      side.req_tr.level_hint = sp.qos.picks()
                                   ? c.masked(*entry.level_hint, side.req_tr.valid)
                                   : c.head_pressure(side.req_tr.valid, side.req_tr.data, false);
  }
  // Frames a source's two request faces into the transport layout and merges them into its one
  // network entry.
  void join_source(unsigned si, SourceSide &side, const std::array<Channel, 2> &requests,
                   bool convert, const Imported &converter, const Imported &splitter) {
    const auto &sp = plan.sources[si];
    const auto word = word_at(sp.name);
    const auto key = "source/" + sp.name;
    // Frame into the transport layout; the flow identity is stamped here
    // (the endpoint drives a reserved zero field by contract).
    auto stamp_w = b.constant(plan.source_bits, 2 * si);
    auto stamp_r = b.constant(plan.source_bits, 2 * si + 1);
    // A decode miss (an error request) takes the flow pair of the source's miss route.
    std::array<std::optional<ValueId>, 2> paths;
    if (plan.burst_path_bits)
      for (unsigned d = 0; d < 2; ++d) {
        paths[d] = flow_of(si, requests[d].data[c.endpoint_field_index(false, "id")]);
        if (const auto missed = miss_route(plan, sp.name); missed && sp.flows.size() > 1)
          paths[d] = b.select(requests[d].data[c.endpoint_field_index(false, "error")],
                              b.constant(plan.burst_path_bits, missed->source_path), *paths[d]);
      }
    std::array<std::optional<ValueId>, 2> targets;
    if (plan.target_bits)
      for (unsigned d = 0; d < 2; ++d) {
        const auto address = requests[d].data[c.endpoint_field_index(false, "address")];
        const auto at = key + (d ? "/read" : "/write");
        targets[d] = route_target(si, address);
        c.bind(at + ".route_target", *targets[d]);
        c.bind(at + ".route_address", address);
      }
    // Its runs (width_adapter.hpp request runs): single beats without a converter; whole beats of
    // a target word at least behind the clamp (its narrowest window width, clamp_profile).
    const unsigned clamp_floor = std::accumulate(
        plan.targets.begin(), plan.targets.end(),
        static_cast<unsigned>(std::bit_width(sp.engine_bits / 8) - 1),
        [&](unsigned least, const auto &t) {
          return std::min(least,
                          static_cast<unsigned>(std::bit_width(endpoint_word(plan, t.name)) - 1));
        });
    const unsigned beat_floor = convert ? clamp_floor : 64;
    // A fixed level is stamped per direction (message.hpp point 15).
    std::array<std::optional<ValueId>, 2> levels;
    if (plan.levels() && (sp.qos.levels.write || sp.qos.levels.read))
      levels = {b.constant(3, sp.qos.levels.write), b.constant(3, sp.qos.levels.read)};
    std::array<Channel, 2> framed{c.frame_burst(false, requests[0], stamp_w, word, paths[0],
                                                targets[0], beat_floor, levels[0]),
                                  c.frame_burst(false, requests[1], stamp_r, word, paths[1],
                                                targets[1], beat_floor, levels[1])};
    // Formal-observation anchors: the write request packet before the
    // read/write merge (write-engine-local view).
    c.bind(key + "/request.pre_merge_write_valid", framed[0].valid);
    c.bind(key + "/request.pre_merge_write_error",
           framed[0].data[c.burst_field_index(false, word, "error")]);
    // A burst cut into packets or pieces holds the merger to its final packet: the
    // converter's marker when there is one, else the splitter's; a burst without
    // either is one packet.
    std::array<ValueId, 2> final{c.bit(1), c.bit(1)};
    const bool reads_by_granule = reads_granule_target(plan, si);
    auto read_taken = final[1];
    if (convert) {
      final = {converter.bound.at("w.burst_final_packet"),
               converter.bound.at("r.burst_final_packet")};
      if (reads_by_granule)
        read_taken = converter.bound.at("r.burst_taken_packet");
    } else if (sp.packets) {
      if (!sp.read_only)
        final[0] = splitter.bound.at("w.burst_final_packet");
      final[1] = splitter.bound.at("r.burst_final_packet");
      if (reads_by_granule)
        read_taken = splitter.bound.at("r.burst_taken_packet");
    }
    // Each direction's pipe carries its final marker with the fragment.
    for (unsigned d = 0; d < 2; ++d) {
      auto with = framed[d];
      with.data.push_back(final[d]);
      if (d == 1 && reads_by_granule)
        with.data.push_back(read_taken);
      with = endpoint_pipe(sp.pipes, d ? "packets/read" : "packets/write", key, with);
      if (d == 1 && reads_by_granule) {
        read_taken = with.data.back();
        with.data.pop_back();
      }
      final[d] = with.data.back();
      with.data.pop_back();
      framed[d] = with;
    }
    // A packet into another target than its burst's previous packet waits before the request
    // merger until every earlier packet of the burst is answered (hold_packets).
    std::array<ValueId, 2> held_back{};
    if (side.merger_holds)
      for (unsigned d = 0; d < 2; ++d) {
        held_back[d] = c.wire(1);
        Channel out{c.wire(1), c.wire(1), framed[d].data};
        c.drive(out.valid, c.and_(framed[d].valid, c.not_(held_back[d])));
        c.drive(framed[d].ready, c.and_(out.ready, c.not_(held_back[d])));
        framed[d] = out;
      }
    // Formal-observation anchor: the read request offered to the merge (a held engine's burst
    // stays in its port pipe until the merge takes it).
    c.bind(key + "/request.pre_merge_read_valid", framed[1].valid);
    // A read answered by the converter itself (a burst it rejects) is taken there.
    if (side.read_taken)
      c.drive(*side.read_taken,
              c.or_(c.and_(c.transfer(framed[1]), reads_by_granule ? read_taken : final[1]),
                    convert ? converter.bound.at("r.local_take") : c.bit(0)));
    auto regulator = regulator_source(si);
    auto &regulator_front = regulator.front;
    const auto packets_mode = regulator.packets_mode, issue_mode = regulator.issue_mode;
    // Reads and writes share one pool of `outstanding` units in flight (SourcePlan::pooled).
    auto rejoined = c.rejoin_requests(
        key + "/request", framed, final, sp.protocol == Protocol::Axi4Lite,
        c.burst_field_index(false, word, "last"),
        sp.pooled ? std::optional<unsigned>{sp.outstanding} : std::nullopt, sp.split_narrow, word,
        std::countr_zero(sp.engine_bits / 8), sp.id_table < sp.id_keys ? sp.id_table : 0U,
        sp.id_table < sp.id_keys ? sp.id_bits.value_or(0) : 0U,
        // A source width converter keeps converted packets of its own between the engine and the
        // merger, so a burst waiting in the engine is not the merger's next head there.
        !convert &&
                (side.ep.bound.contains("write_waiting") || side.ep.bound.contains("read_waiting"))
            ? std::optional<std::array<ValueId, 2>>{std::array{
                  side.ep.bound.contains("write_waiting") ? side.ep.bound.at("write_waiting")
                                                          : c.bit(0),
                  side.ep.bound.contains("read_waiting") ? side.ep.bound.at("read_waiting")
                                                         : c.bit(0)}}
            : std::nullopt,
        regulator_front ? &*regulator_front : nullptr, packets_mode, sp.engine_slots);
    if (side.merge_held) {
      // The merger's candidate 0 is the read face of a full AXI4 source, the write face of Lite;
      // a direction also waits while the shared pool has no room for it.
      const auto &bound = c.out.bindings;
      const auto held = bound.at(key + "/request.held"), owner = bound.at(key + "/request.owner");
      const unsigned read = sp.protocol == Protocol::Axi4Lite ? 1 : 0;
      for (const unsigned d : {0U, 1U}) {
        const unsigned at = d ? read : 1 - read;
        auto blocked = c.and_(held, c.eq(owner, 1 - at));
        if (const auto &pool = rejoined.pool; plan.decodes_modes(si) && pool && pool->admitted_head)
          blocked = c.or_(blocked,
                          c.and_(c.eq(pool->count, sp.outstanding), c.not_(*pool->admitted_head)));
        else if (const auto open = bound.find(key + "/request.open" + std::to_string(at));
                 open != bound.end())
          blocked = c.or_(blocked, c.not_(open->second));
        c.drive((*side.merge_held)[d], blocked);
      }
    }
    if (side.merger_holds)
      hold_packets(si, side, framed, rejoined, held_back, word);
    finish_source_entry(si, side, word, rejoined, regulator_front, packets_mode, issue_mode);
    side.pool = std::move(rejoined.pool);
    // Map observation anchors name the merged request packet at the network entry.
    c.bind(key + "/request.packet_valid", side.req_tr.valid);
    c.bind(key + "/request.packet_ready", side.req_tr.ready);
    c.bind(key + "/request.packet_write",
           side.req_tr.data[c.burst_field_index(false, word, "write")]);
    c.bind(key + "/request.packet_error",
           side.req_tr.data[c.burst_field_index(false, word, "error")]);
    if (!sp.qos.axqos_map.empty())
      c.bind(key + "/request.packet_level",
             side.req_tr.data[c.burst_field_index(false, word, "level")]);
    c.bind(key + "/write.source_stamp", stamp_w);
    c.bind(key + "/read.source_stamp", stamp_r);
    register_source_storage(si, side, converter, splitter);
    // The drop state counts with the write engine's controls (its B readiness reads it).
    if (side.miss_drop_state)
      std::ranges::find(c.out.storage, key + "/write", &CrossStorageBinding::resource)
          ->control.push_back(*side.miss_drop_state);
    // The response-face readiness anchors cited by the reserved return
    // write justification (verify_axi4_exchange_composition).
    c.bind(key + "/write.response_ready", side.wrsp_ep.ready);
    c.bind(key + "/read.response_ready", side.rrsp_ep.ready);
    if (convert)
      bind_response_head(si, key, side);
    bind_decode_head(key, side, convert);
  }

  // A decode packet's word (decode_packet.hpp) is marked by the fragment in front; a clamp's word
  // in front of the source's entry looks at its tag and valid (response_faces).
  void bind_decode_head(const std::string &key, const SourceSide &side, bool convert) {
    if (!plan.error_codes)
      return;
    const bool flag = decode_flag(plan);
    c.bind(key + (flag ? "/read.response_head_error" : "/read.response_head_result"),
           side.rrsp_ep.data[c.endpoint_field_index(true, flag ? "error" : "result")]);
    if (!convert)
      return;
    c.bind(key + "/read.response_head_valid", side.rrsp_ep.valid);
    if (plan.burst_tag_bits)
      c.bind(key + "/read.response_tag", side.rrsp_ep.data[c.endpoint_field_index(true, "tag")]);
  }

  // What a clamp's response readiness reads of the fragment in front of it (response_faces).
  void bind_response_head(unsigned si, const std::string &key, const SourceSide &side) {
    const auto profile = clamp_profile(plan, si);
    if (!profile.reassembly && !profile.response_runs)
      return;
    const auto field = [&](const char *name) {
      return side.rrsp_ep.data[c.endpoint_field_index(true, name)];
    };
    if (plan.burst_tag_bits)
      c.bind(key + "/read.response_tag", field("tag"));
    c.bind(key + "/read.response_head_address", field("address"));
    c.bind(key + "/read.response_head_last", field("last"));
    if (profile.response_runs)
      c.bind(key + "/read.response_head_count", field("count"));
  }

  // 2^bits minus the charge of the burst whose request packet is in: its byte count rounded up
  // to 16 bytes, in 1/1024-byte units. A full AXI burst counts ((length + 1) << size) bytes less
  // the start address offset within its size (one packet per burst, capability rule
  // qos.whole_bursts); any other source's transfer counts its word.
  ValueId minus_charge(const SourcePlan &sp, const Channel &in, unsigned word, unsigned bits) {
    if (sp.apb || sp.protocol != Protocol::Axi4 || sp.ahb)
      return b.constant(bits, (1ULL << bits) - (std::uint64_t{(word + 15) / 16} << budget_unit_at));
    const auto field = [&](const char *name) {
      return in.data[c.burst_field_index(false, word, name)];
    };
    const unsigned w = plan.burst_len_bits + 8;
    const auto minus = [&](ValueId x, ValueId y) { return b.binary(Op::Subtract, x, y); };
    const auto beats =
        minus(b.unary(Op::ZeroExtend, field("length"), w), b.constant(w, (1ULL << w) - 1));
    auto bytes = beats;
    for (unsigned k = 1; k < 8; ++k) {
      const auto spanned = minus(b.slice(b.concat(beats, b.constant(k, 0)), 0, w),
                                 b.unary(Op::ZeroExtend, b.slice(field("address"), 0, k), w));
      bytes = b.select(c.eq(field("size"), k), spanned, bytes);
    }
    // floor((bytes - 1) / 16) + 1 whole 16-byte units.
    const auto units = minus(b.slice(minus(bytes, b.constant(w, 1)), 4, w - 4),
                             b.constant(w - 4, (1ULL << (w - 4)) - 1));
    const auto charge = b.concat(units, b.constant(budget_unit_at, 0));
    const auto fit = w - 4 + budget_unit_at >= bits ? b.slice(charge, 0, bits)
                                                    : b.unary(Op::ZeroExtend, charge, bits);
    return minus(b.constant(bits, 0), fit);
  }
  // A source's rate limiter on its merged request packets; each transfer charges its bytes
  // rounded up to 16 (minus_charge). Its zone rests once the budget is drained.
  Channel limit(const std::string &id, const Channel &in, const QosShaper &shaper, unsigned word,
                const SourcePlan &sp) {
    Channel out{c.wire(1), c.wire(1), in.data};
    const auto r = lower_rate_limiter(b, c.clock, c.reset, in.valid,
                                      in.data[c.burst_field_index(false, word, "last")], out.ready,
                                      shaper, minus_charge(sp, in, word, budget_bits));
    c.drive(out.valid, r.valid);
    c.drive(in.ready, r.ready);
    c.out.storage.push_back({id, c.clock, {}, {r.counter, r.inside, r.held}, r.charge, r.charge});
    if (auto *zone = c.region(id.substr(0, id.rfind('/'))))
      zone->idle.push_back(c.and_(c.eq(r.counter, 0), c.not_(r.held)));
    return out;
  }

  // A source's bandwidth regulator on its merged request packets: a transfer is charged its bytes
  // rounded up to 16 (minus_charge) after its last response transfer; a raise packet is the
  // transfer's address with head and tail markers and the read flow, its other fields those of
  // the input. Its zone
  // rests once the budget is drained and no transfer or raise packet is in flight.
  Channel regulate_piped(unsigned si, const Channel &in, const SourceSide &side, unsigned word,
                         const std::optional<Compose::Pool> &pool, RegulatorFront *front,
                         const LinkPipeline &packets_mode, const LinkPipeline &issue_mode,
                         std::optional<ValueId> source_level, std::vector<ValueId> qos_words) {
    const auto key = "source/" + plan.sources[si].name;
    const auto field = [&](const char *name) { return c.burst_field_index(false, word, name); };
    const auto finished = [&](const Channel &ch) {
      return c.and_(c.and_(ch.valid, ch.ready), ch.data[c.endpoint_field_index(true, "last")]);
    };
    const auto &sp = plan.sources[si];
    Channel allocated = in;
    if (!packets_mode.forward && !packets_mode.backward)
      allocated.data[field("level")] = front->request_level;
    if (!allocated.level_hint) {
      const auto waiting = pool ? c.or_(pool->head_waits, pool->lacks_unit) : c.bit(0);
      const auto pressing =
          b.select(waiting,
                   b.select(b.binary(Op::UnsignedLess, front->now, front->request_level),
                            front->request_level, front->now),
                   front->now);
      allocated.level_hint = c.pressure(pressing);
    }
    Channel normal = allocated;
    RegulatorPipe pipe;
    pipe.front = front;
    pipe.issue = issue_mode.forward;
    pipe.delayed = packets_mode.forward;
    pipe.level_hint = *allocated.level_hint;
    pipe.network_level_hint = *allocated.level_hint;
    pipe.head_level = allocated.data[field("level")];
    pipe.source_level = source_level.value_or(front->now);
    std::vector<ValueId> allocation;
    if (pipe.issue) {
      for (unsigned i = 0; i < sp.outstanding; ++i)
        allocation.push_back(c.wire(1));
      normal.data.insert(normal.data.end(), allocation.begin(), allocation.end());
      const auto head = c.masked(c.pressure(allocated.data[field("level")]),
                                 c.and_(allocated.valid, allocated.data[field("first")]));
      normal.level_hint.reset();
      normal = c.pipeline(key + "/pipe/issue", normal, {true, false});
      auto &stored = c.out.storage.back();
      stored.control.insert(stored.control.end(),
                            stored.payload.end() - static_cast<long>(sp.outstanding),
                            stored.payload.end());
      stored.payload.resize(stored.payload.size() - sp.outstanding);
      const auto qp = lower_qos_level_hint(b, c.clock, c.reset, allocated.valid, head,
                                           *allocated.level_hint, normal.ready, {true, false});
      const auto qh =
          lower_qos_level(b, c.clock, c.reset, 1, source_level.value_or(front->now),
                          c.regulator_empty(*front, c.or_(allocated.valid, normal.valid)),
                          allocated.valid, allocated.ready, normal.valid, normal.ready);
      normal.level_hint = qp.value;
      pipe.network_level_hint = qp.value;
      pipe.source_level = qh.value;
      pipe.head_level = normal.data[field("level")];
      pipe.alloc_valid = allocated.valid;
      pipe.alloc_first = allocated.data[field("first")];
      pipe.alloc_ready = allocated.ready;
      pipe.offered_context.assign(normal.data.end() - static_cast<long>(sp.outstanding),
                                  normal.data.end());
      qos_words.insert(qos_words.end(), qp.words.begin(), qp.words.end());
      qos_words.insert(qos_words.end(), qh.words.begin(), qh.words.end());
      if (!packets_mode.forward && !packets_mode.backward)
        c.drive(front->taken, c.and_(allocated.valid, allocated.ready));
    }
    Channel out{c.wire(1), c.wire(1), normal.data};
    if (pipe.issue)
      out.data.resize(in.data.size());
    const auto address = normal.data[field("address")];
    const auto route = plan.target_bits ? b.concat(normal.data[field("target")], address) : address;
    const auto done_w = finished(side.wrsp_ep), done_r = finished(side.rrsp_ep);
    if (pipe.issue) {
      const auto alloc_address = allocated.data[field("address")];
      pipe.alloc_route = plan.target_bits ? b.concat(allocated.data[field("target")], alloc_address)
                                          : alloc_address;
      pipe.alloc_write = allocated.data[field("write")];
      pipe.alloc_tag = plan.burst_tag_bits ? allocated.data[field("tag")] : c.bit(0);
      pipe.alloc_charge = minus_charge(sp, allocated, word, budget_bits + 1);
    }
    const auto tag = plan.burst_tag_bits ? normal.data[field("tag")] : c.bit(0);
    const auto done_tag =
        plan.burst_tag_bits
            ? b.select(done_r, side.rrsp_ep.data[c.endpoint_field_index(true, "tag")],
                       side.wrsp_ep.data[c.endpoint_field_index(true, "tag")])
            : c.bit(0);
    std::optional<ValueId> wait_id;
    if (plan.burst_id_bits)
      wait_id = allocated.data[field("id")];
    auto r = lower_rate_regulator_contexts(
        b, c.clock, c.reset, normal.valid, normal.data[field("last")], route, plan.target_bits,
        normal.data[field("write")], tag,
        {sp.outstanding > 1 && pool ? pool->head_waits : c.bit(0),
         sp.outstanding > 1 && pool ? pool->lacks_unit : c.bit(0), wait_id},
        out.ready, c.or_(done_w, done_r), c.not_(done_r), done_tag,
        budget_levels(*sp.qos.shaper, *sp.qos.scheduler),
        minus_charge(sp, normal, word, budget_bits + 1), sp.outstanding, &pipe);
    if (!pipe.issue && !packets_mode.forward && !packets_mode.backward)
      c.drive(front->taken, c.and_(normal.valid, r.ready));
    if (front->conflict)
      c.drive(*front->conflict, r.conflict);
    if (front->answered)
      c.drive(*front->answered, c.or_(done_w, done_r));
    if (pipe.issue)
      for (unsigned i = 0; i < sp.outstanding; ++i)
        c.drive(allocation[i], r.allocation[i]);
    r.words.insert(r.words.end(), qos_words.begin(), qos_words.end());
    c.drive(out.valid, r.valid);
    c.drive(normal.ready, r.ready);
    const auto low = plan.transport_address_bits;
    out.data[field("address")] = b.select(r.raise, b.slice(r.route, 0, low), address);
    if (plan.target_bits)
      out.data[field("target")] =
          b.select(r.raise, b.slice(r.route, low, plan.target_bits), normal.data[field("target")]);
    out.data[field("last")] = c.or_(r.raise, normal.data[field("last")]);
    if (!sp.apb)
      out.data[field("first")] = c.or_(r.raise, normal.data[field("first")]);
    out.data[field("source")] =
        b.select(r.raise, b.constant(plan.source_bits, 2 * si + 1), normal.data[field("source")]);
    out.data[field("level")] = r.head_level;
    out.data[field("raise")] = r.raise;
    if (c.level_hint_width())
      out.level_hint = r.pressure;
    c.out.storage.push_back({key + "/regulator", c.clock,
                             r.routes.empty() ? std::vector{r.route} : r.routes, r.words, r.charged,
                             r.charged});
    for (const auto &[name, value] : r.bindings)
      c.bind(key + "/regulator." + name, value);
    c.bind(key + "/request.packet_raise", r.raise);
    if (auto *zone = c.region(key))
      zone->idle.push_back(r.idle);
    return out;
  }

  Channel regulate(unsigned si, const Channel &in, const SourceSide &side, unsigned word,
                   const std::optional<Compose::Pool> &pool, RegulatorFront *front,
                   const LinkPipeline &packets_mode, const LinkPipeline &issue_mode,
                   std::optional<ValueId> source_level, std::vector<ValueId> qos_words) {
    if (front)
      return regulate_piped(si, in, side, word, pool, front, packets_mode, issue_mode, source_level,
                            std::move(qos_words));
    const auto key = "source/" + plan.sources[si].name;
    const auto field = [&](const char *name) { return c.burst_field_index(false, word, name); };
    const auto finished = [&](const Channel &ch) {
      return c.and_(c.and_(ch.valid, ch.ready), ch.data[c.endpoint_field_index(true, "last")]);
    };
    Channel out{c.wire(1), c.wire(1), in.data};
    // A raise packet goes where the transfer went: its route holds the target index too.
    const auto address = in.data[field("address")];
    const auto route = plan.target_bits ? b.concat(in.data[field("target")], address) : address;
    const auto &sp = plan.sources[si];
    const auto done_w = finished(side.wrsp_ep), done_r = finished(side.rrsp_ep);
    // A source with several transfers in flight keeps a context per transfer, found again by the
    // response's direction and slot (tag).
    const auto r =
        sp.outstanding > 1 && !sp.apb
            ? lower_rate_regulator_contexts(
                  b, c.clock, c.reset, in.valid, in.data[field("last")], route, plan.target_bits,
                  in.data[field("write")], in.data[field("tag")],
                  {pool ? pool->head_waits : c.bit(0), pool ? pool->lacks_unit : c.bit(0),
                   plan.burst_id_bits ? std::optional{in.data[field("id")]} : std::nullopt},
                  out.ready, c.or_(done_w, done_r), c.not_(done_r),
                  b.select(done_r, side.rrsp_ep.data[c.endpoint_field_index(true, "tag")],
                           side.wrsp_ep.data[c.endpoint_field_index(true, "tag")]),
                  budget_levels(*sp.qos.shaper, *sp.qos.scheduler),
                  minus_charge(sp, in, word, budget_bits + 1), sp.outstanding)
            : lower_rate_regulator(b, c.clock, c.reset, in.valid, in.data[field("last")], route,
                                   out.ready, c.or_(done_w, done_r),
                                   budget_levels(*sp.qos.shaper, *sp.qos.scheduler),
                                   minus_charge(sp, in, word, budget_bits + 1));
    c.drive(out.valid, r.valid);
    c.drive(in.ready, r.ready);
    const auto low = plan.transport_address_bits;
    out.data[field("address")] = b.select(r.raise, b.slice(r.route, 0, low), address);
    if (plan.target_bits)
      out.data[field("target")] =
          b.select(r.raise, b.slice(r.route, low, plan.target_bits), in.data[field("target")]);
    out.data[field("last")] = c.or_(r.raise, in.data[field("last")]);
    if (!plan.sources[si].apb)
      out.data[field("first")] = c.or_(r.raise, in.data[field("first")]);
    out.data[field("source")] =
        b.select(r.raise, b.constant(plan.source_bits, 2 * si + 1), in.data[field("source")]);
    // The packet carries its level and the raise mark; the output presses with the regulator's
    // arbitration level.
    out.data[field("level")] = r.head_level;
    out.data[field("raise")] = r.raise;
    if (c.level_hint_width())
      out.level_hint = c.pressure(r.level);
    c.out.storage.push_back({key + "/regulator", c.clock,
                             r.routes.empty() ? std::vector{r.route} : r.routes, r.words, r.charged,
                             r.charged});
    for (const auto &[name, value] : r.bindings)
      c.bind(key + "/regulator." + name, value);
    c.bind(key + "/request.packet_raise", r.raise);
    if (auto *zone = c.region(key))
      zone->idle.push_back(r.idle);
    return out;
  }

  // The storage records of a source's write and read engines (axi4_initiator.cpp): descriptor,
  // slot and queued-descriptor words with the converter's and splitter's words of the direction,
  // then the return staging (the B response register, the one-beat R skid).
  static void ahb5_source_names(std::vector<std::string> &fields, const SourcePlan &sp) {
    if (sp.ahb5) {
      if (sp.hnonsec)
        fields.push_back("ahb.nonsecure");
      if (sp.hmaster_bits)
        fields.push_back("ahb.master");
    }
  }
  void register_source_storage(unsigned si, const SourceSide &side, const Imported &converter,
                               const Imported &splitter) {
    const auto &sp = plan.sources[si];
    const auto key = "source/" + sp.name;
    const unsigned outstanding = sp.engine_slots;
    const bool lite = sp.protocol == Protocol::Axi4Lite, id = plan.burst_id_bits != 0;
    const auto &bound = side.ep.bound;
    const auto &states = side.ep.states;
    std::set<std::string> assigned;
    // A tracked engine's slot flags.
    auto slots_taken = [&](const std::string &prefix) {
      auto taken = states.at(prefix + ".s0.valid");
      for (unsigned k = 1; k < outstanding; ++k)
        taken = c.or_(taken, states.at(prefix + ".s" + std::to_string(k) + ".valid"));
      return taken;
    };
    const bool staged_one = outstanding == 1 && states.contains("wr.b_full");
    for (const bool read : {false, true}) {
      const std::string dir = read ? "rr" : "wr";
      const auto way = read ? "read" : "write";
      const auto state = states.at(dir + ".state");
      ValueId capture, consume, full;
      if (!read) {
        consume = sp.apb || sp.ahb ? bound.at("wr.b_take")
                                   : c.and_(side.ep.axi.at("bvalid"), side.ep.axi.at("bready"));
        capture = bound.at("wr.aw_fire");
        if (lite)
          capture = c.or_(c.and_(capture, c.not_(states.at("wr.beat_full"))),
                          c.and_(bound.at("wr.accept"), c.eq(state, 0)));
        full = c.not_(c.eq(state, 0));
        if (lite)
          full = c.or_(full, states.at("wr.beat_full"));
        // A tracked engine holds bursts while any slot is taken.
        if (outstanding > 1)
          full = c.or_(full, slots_taken("wr"));
      } else {
        // A tracked engine completes a burst when the face accepts its last
        // beat (rr.pop).
        capture = bound.at("rr.ar_fire");
        consume = outstanding > 1 ? bound.at("rr.pop")
                  : sp.apb        ? bound.at("rr.accept")
                                  : c.and_(bound.at("rr.accept"), bound.at("rr.rlast"));
        full = outstanding > 1 ? c.or_(c.not_(c.eq(state, 0)), slots_taken("rr"))
                               : c.not_(c.eq(state, 0));
      }
      // An engine of one burst holds it until its staged response leaves.
      if (staged_one)
        full = c.or_(full, states.at(read ? "rr.rsp_full" : "wr.b_full"));
      c.bind(key + "/" + way + ".capture", capture);
      c.bind(key + "/" + way + ".transfer", bound.at(read ? "rr.emit_fire" : "wr.transfer"));
      c.bind(key + "/" + way + ".deliver", bound.at(read ? "rr.rsp_load" : "wr.wrsp_fire"));
      c.bind(key + "/" + way + ".consume", consume);
      c.bind(key + "/" + way + ".full", full);
      Names words;
      auto &[fields, controls] = words;
      const bool mapped = !sp.qos.axqos_map.empty();
      if (!sp.apb)
        add_descriptor(fields, dir + ".desc.", id, plan.burst_user_bits, mapped,
                       plan.burst_security_bits != 0);
      add_source_progress(words, read, sp.apb.has_value(), lite, outstanding > 1);
      plane_words(side.ep.states, dir, words);
      // The AHB face's data phase and its open piece count with the read engine: its R readiness
      // reads them, and its B readiness is constant.
      if (read && sp.ahb) {
        fields.insert(fields.end(), {"ahb.addr", "ahb.write", "ahb.size", "ahb.burst", "ahb.prot",
                                     "ahb.lock", "ahb.first"});
        ahb5_source_names(fields, sp);
        controls.insert(controls.end(), {"ahb.phase", "ahb.sent", "ahb.done", "ahb.err", "ahb.end",
                                         "ahb.left", "ahb.flight", "ahb.open_write"});
      }
      if (outstanding > 1) {
        // A slot's write poison or read prefix is payload. Slot flags, the ID table, the tags,
        // the read's local beat and the B staging flag are control.
        for (unsigned k = 0; k < outstanding; ++k) {
          const auto at = dir + ".s" + std::to_string(k) + ".";
          const auto payload = at + std::array{"poison", "prefix"}[read];
          if (states.contains(payload))
            fields.push_back(payload);
          controls.push_back(at + "valid");
        }
        for (unsigned j = 0; j < sp.outstanding_ids; ++j) {
          const auto at = dir + ".i" + std::to_string(j) + ".";
          controls.push_back(at + "count");
          if (id)
            controls.push_back(at + "id");
          controls.push_back(at + "dest");
        }
        if (!read)
          controls.insert(controls.end(), {"wr.tag", "wr.b_full"});
      }
      std::vector<ValueId> payload, control;
      consume_states(side.ep, words, assigned, payload, control);
      for (const auto *part : {&converter, &splitter})
        for (const auto &[name, q] : part->states)
          if (name.starts_with(read ? "r." : "w."))
            payload.push_back(q);
      if (read)
        control.insert(control.end(), side.read_expand.begin(), side.read_expand.end());
      // The shared pool and its per-tag bookkeeping count with the read engine's controls, with
      // the target of the last packet head the merger passed (hold_packets).
      if (read && side.pool) {
        control.insert(control.end(), side.last_target.begin(), side.last_target.end());
        control.push_back(side.pool->count);
        control.insert(control.end(), {side.pool->narrow_held, side.pool->piece_wait});
        if (side.pool->head_held)
          control.push_back(*side.pool->head_held);
        if (side.pool->pending)
          control.push_back(*side.pool->pending);
        for (const auto &units : side.pool->units)
          for (const auto &u : units)
            control.insert(control.end(), {u.packets, u.whole, u.narrow});
        control.insert(control.end(), side.pool->id_count.begin(), side.pool->id_count.end());
        control.insert(control.end(), side.pool->id_key.begin(), side.pool->id_key.end());
      }
      c.out.storage.push_back({key + "/" + way, c.clock, payload, control, capture, consume});
      Names staging;
      if (!read) {
        staging.first = {"wr.bresp"};
        if (plan.burst_response_user_bits)
          staging.first.push_back("wr.b_user");
        if (staged_one)
          staging.second.push_back("wr.b_full");
      } else {
        staging.first = {"rr.rsp_data", "rr.rsp_result", "rr.rsp_error", "rr.rsp_last"};
        if (sp.data_user_bits)
          staging.first.push_back("rr.rsp_data_user");
        if (plan.exclusive)
          staging.first.push_back("rr.rsp_exclusive_okay");
        if (plan.burst_response_user_bits)
          staging.first.push_back("rr.rsp_user");
        staging.second = {"rr.rsp_full"};
      }
      for (const auto &name : {dir + ".rsp_id", dir + ".pslverr", dir + ".prdata_read",
                               dir + ".prdata_lane", dir + ".pruser_lane"})
        if (side.ep.states.contains(name))
          staging.second.push_back(name);
      std::vector<ValueId> staged, staged_control;
      consume_states(side.ep, staging, assigned, staged, staged_control);
      c.out.storage.push_back({key + "/" + way + "/return", c.clock, staged, staged_control,
                               bound.at(read ? "rr.rsp_load" : "wr.wrsp_fire"),
                               read ? bound.at("rr.accept") : consume});
    }
    if (assigned.size() != side.ep.states.size())
      throw std::logic_error("Unowned initiator state word at " + key);
  }

  // A class is a pure function of the transaction's captured ID on both directions.
  ValueId flow_of(unsigned si, ValueId id) {
    const auto &sp = plan.sources[si];
    if (sp.flows.size() < 2)
      return b.constant(plan.burst_path_bits, 0);
    std::vector<ValueId> values;
    for (unsigned k = 0; k < sp.flows.size(); ++k)
      values.push_back(b.constant(plan.burst_path_bits, k));
    return class_value(b, id, sp.id_ranges, values);
  }
  // The return path of a target's response (message.hpp point 14) from its source and AxID: of
  // the target's response route to that source and flow.
  ValueId return_of(const std::string &target, const Channel &response) {
    const auto &g = plan.exchange.response;
    const auto source = response.data[c.endpoint_field_index(true, "source")];
    auto path = b.constant(plan.burst_path_bits, 0);
    std::map<unsigned, ValueId> flows;
    std::optional<ValueId> target_path;
    for (const auto &route : g.routes) {
      const auto value = return_path(g, route);
      if (route.entry != target || !value)
        continue;
      // A target of several flows: the flow of the mapping holding the response address.
      if (route.target_path && !target_path)
        target_path =
            flow_at(plan.target(target), response.data[c.endpoint_field_index(true, "address")]);
      const auto si = static_cast<unsigned>(
          std::ranges::find(plan.sources, route.exit, &SourcePlan::name) - plan.sources.begin());
      auto hit = c.or_(c.eq(source, 2 * si), c.eq(source, 2 * si + 1));
      if (!route.flows.empty() && route.flows.size() < plan.sources[si].flows.size()) {
        if (!flows.contains(si))
          flows[si] = flow_of(si, response.data[c.endpoint_field_index(true, "id")]);
        hit = c.and_(hit, std::accumulate(route.flows.begin(), route.flows.end(), c.bit(0),
                                          [&](ValueId any, unsigned k) {
                                            return c.or_(any, c.eq(flows.at(si), k));
                                          }));
      }
      if (route.target_path)
        hit = c.and_(hit, c.eq(*target_path, *route.target_path));
      path = b.select(hit, b.constant(plan.burst_path_bits, value), path);
    }
    // A miss answer, and a response of a flow pair the matrix leaves out (only a miss has one),
    // returns over the source's miss route.
    for (unsigned si = 0; si < plan.sources.size(); ++si) {
      const auto &sp = plan.sources[si];
      const auto missed = miss_route(plan, sp.name);
      if (!missed || plan.targets[missed->target].name != target)
        continue;
      const auto route = std::ranges::find_if(g.routes, [&](const SwitchRoute &r) {
        return r.entry == target && r.exit == sp.name &&
               r.target_path.value_or(0) == missed->target_path &&
               (r.flows.empty() ||
                std::ranges::find(r.flows, missed->source_path) != r.flows.end());
      });
      const bool marked = miss_answers.contains(target) && sp.flows.size() > 1;
      auto other = marked ? miss_answers.at(target) : c.bit(0);
      bool any = marked;
      const auto &tp = plan.target(target);
      for (unsigned f = 0; f < std::max<std::size_t>(1, sp.flows.size()); ++f)
        for (unsigned k = 0; k < tp.flows; ++k)
          if (!(reaching_flows(plan, sp.name, target, k) >> f & 1)) {
            any = true;
            auto pair = c.bit(1);
            if (sp.flows.size() > 1) {
              if (!flows.contains(si))
                flows[si] = flow_of(si, response.data[c.endpoint_field_index(true, "id")]);
              pair = c.eq(flows.at(si), f);
            }
            if (tp.flows > 1) {
              if (!target_path)
                target_path = flow_at(tp, response.data[c.endpoint_field_index(true, "address")]);
              pair = c.and_(pair, c.eq(*target_path, k));
            }
            other = c.or_(other, pair);
          }
      if (route == g.routes.end() || !any)
        continue;
      path = b.select(c.and_(c.or_(c.eq(source, 2 * si), c.eq(source, 2 * si + 1)), other),
                      b.constant(plan.burst_path_bits, return_path(g, *route)), path);
    }
    return path;
  }

  // The target flow of a transport address: that of the routing window holding it, else of the
  // last window in base order, as the outlet decodes it (mapping_decode::flow).
  ValueId flow_at(const TargetPlan &tp, ValueId address) {
    std::vector<std::pair<AddressWindow, unsigned>> rows;
    for (const auto &m : tp.mappings)
      std::ranges::transform(m.routes, std::back_inserter(rows),
                             [&](const AddressWindow &r) { return std::pair{r, m.flow}; });
    std::ranges::sort(rows, {}, [](const auto &x) { return x.first.base; });
    const auto bits = b.ir.values.at(address.index).width, width = tp.flow_bits();
    auto flow = b.constant(width, rows.back().second);
    for (auto k = rows.size() - 1; k-- > 0;) {
      const auto &[w, f] = rows[k];
      const auto offset = b.binary(Op::Subtract, address, b.constant(bits, w.base));
      flow = b.select(b.binary(Op::UnsignedLess, offset, b.constant(bits, w.size)),
                      b.constant(width, f), flow);
    }
    return flow;
  }
  // A request destination: a target, or one flow of a target of several.
  static std::string destination(const std::string &target, std::optional<unsigned> flow) {
    return flow ? target + "/" + std::to_string(*flow) : target;
  }
  // The request destinations (message.hpp point 16): each target, each flow of a target of
  // several, with its routing windows; a request's target field holds its index here.
  std::vector<std::pair<std::string, std::vector<AddressWindow>>> request_destinations() const {
    std::vector<std::pair<std::string, std::vector<AddressWindow>>> out;
    for (const auto &t : plan.targets)
      for (unsigned f = 0; f < t.flows; ++f) {
        const auto flow = t.flows > 1 ? std::optional{f} : std::nullopt;
        out.emplace_back(destination(t.name, flow), t.routes(flow));
      }
    return out;
  }
  // A decision over a field's bits that is 1 on the blocks marked on, 0 on those marked off and
  // free elsewhere. A block is the aligned [base, base + 2^bits); the blocks are disjoint. The
  // decision tests a bit only where the two halves below it decide differently, and alike
  // decisions are one node (decision_nodes), so a field the blocks follow bit for bit decides by
  // that bit alone.
  struct DecisionBlock {
    std::uint64_t base;
    unsigned bits;
    bool on;
  };
  static constexpr int decision_free = -1, decision_zero = -2, decision_one = -3;
  struct DecisionNode {
    unsigned bit;
    int high, low;
    auto operator<=>(const DecisionNode &) const = default;
  };
  std::vector<DecisionNode> decision_nodes;
  std::map<DecisionNode, int> decision_index;
  // The decision of the blocks inside one aligned span of 2^bits values (bases relative to it).
  int decision(std::span<const DecisionBlock> blocks, unsigned bits) {
    if (blocks.empty())
      return decision_free;
    if (std::ranges::all_of(blocks, [&](const auto &k) { return k.on == blocks.front().on; }))
      return blocks.front().on ? decision_one : decision_zero;
    if (std::ranges::any_of(blocks, [&](const auto &k) { return k.bits >= bits; }))
      throw std::logic_error("Decision blocks overlap");
    const auto half = std::uint64_t{1} << (bits - 1);
    std::vector<DecisionBlock> low, high;
    for (const auto &k : blocks)
      (k.base < half ? low : high).push_back({k.base & (half - 1), k.bits, k.on});
    const auto h = decision(high, bits - 1), l = decision(low, bits - 1);
    if (h == decision_free || h == l)
      return l;
    if (l == decision_free)
      return h;
    const DecisionNode node{bits - 1, h, l};
    const auto [at, fresh] =
        decision_index.try_emplace(node, static_cast<int>(decision_nodes.size()));
    if (fresh)
      decision_nodes.push_back(node);
    return at->second;
  }
  // The decisions over one value: each node, bit and inverted bit made once.
  struct Decided {
    ValueId value{};
    std::map<int, ValueId> nodes = {};
    std::map<unsigned, ValueId> bits = {}, inverted = {};
  };
  ValueId decide(Decided &d, int root) {
    if (root < 0)
      return c.bit(root == decision_one ? 1 : 0);
    if (const auto at = d.nodes.find(root); at != d.nodes.end())
      return at->second;
    const auto node = decision_nodes[static_cast<std::size_t>(root)];
    auto test = [&] {
      const auto [at, fresh] = d.bits.try_emplace(node.bit);
      if (fresh)
        at->second =
            b.ir.values.at(d.value.index).width == 1 ? d.value : b.slice(d.value, node.bit, 1);
      return at->second;
    };
    auto inverted = [&] {
      const auto [at, fresh] = d.inverted.try_emplace(node.bit);
      if (fresh)
        at->second = c.not_(test());
      return at->second;
    };
    const auto high = node.high, low = node.low;
    ValueId out;
    if (high == decision_one && low == decision_zero)
      out = test();
    else if (high == decision_zero && low == decision_one)
      out = inverted();
    else if (high == decision_one)
      out = c.or_(test(), decide(d, low));
    else if (high == decision_zero)
      out = c.and_(inverted(), decide(d, low));
    else if (low == decision_zero)
      out = c.and_(test(), decide(d, high));
    else if (low == decision_one)
      out = c.or_(inverted(), decide(d, high));
    else
      out = b.select(test(), decide(d, high), decide(d, low));
    return d.nodes[root] = out;
  }
  // The address windows of request destinations as decision blocks: each window split into
  // aligned power-of-two blocks, marked on for the destinations `here`.
  static void window_blocks(std::vector<DecisionBlock> &blocks,
                            const std::vector<AddressWindow> &windows, bool on) {
    for (const auto &w : windows)
      for (auto at = w.base; at < w.base + w.size;) {
        auto size = at ? at & ~(at - 1) : std::uint64_t{1} << 63;
        while (at + size > w.base + w.size)
          size /= 2;
        blocks.push_back({at, static_cast<unsigned>(std::countr_zero(size)), on});
        at += size;
      }
  }
  // The decisions of each target index bit over the global address, per set of reached
  // destinations (alike for every source that reaches the same ones).
  std::map<std::pair<unsigned, std::set<unsigned>>, std::vector<int>> index_decisions;
  // The destination index a source's request carries into the network (message.hpp point 16),
  // decoded from its global address over the destinations the source reaches: every request it
  // sends lies in one of their windows (a decode miss moves into its default target's), each
  // index bit telling the reached destinations with it set from those without.
  ValueId route_target(unsigned si, ValueId address) {
    const auto all = request_destinations();
    std::set<unsigned> reached;
    for (const auto &route : plan.exchange.request.routes)
      if (route.entry == plan.sources[si].name)
        reached.insert(static_cast<unsigned>(
            std::ranges::find(all, destination(route.exit, route.target_path),
                              &std::pair<std::string, std::vector<AddressWindow>>::first) -
            all.begin()));
    const auto width = b.ir.values.at(address.index).width;
    auto [roots, fresh] = index_decisions.try_emplace({width, reached});
    if (fresh) {
      for (unsigned k = 0; k < plan.target_bits; ++k) {
        std::vector<DecisionBlock> blocks;
        for (const auto t : reached)
          window_blocks(blocks, all[t].second, (t >> k & 1) != 0);
        std::ranges::sort(blocks, {}, &DecisionBlock::base);
        const auto root = decision(blocks, width);
        roots->second.push_back(root == decision_free ? decision_zero : root);
      }
    }
    Decided decided{address};
    std::vector<ValueId> bits;
    std::ranges::transform(roots->second, std::back_inserter(bits),
                           [&](int root) { return decide(decided, root); });
    return std::accumulate(std::next(bits.begin()), bits.end(), bits.front(),
                           [&](ValueId low, ValueId bit) { return b.concat(bit, low); });
  }
  // The destinations a switch input carries that leave by this output: the selection of one
  // candidate. A response route is the source flow (2i writes, 2i + 1 reads), a request route the
  // target index. carried maps each destination to the outputs its routes leave by and the entries
  // of each; a request destination split over several outputs also compares the source flow.
  ValueId switch_select(bool response, const std::string &node, const std::string &input,
                        const SwitchOutputPlan &output, ValueId route, ValueId source,
                        std::optional<ValueId> path,
                        const std::map<std::string, Leaving> &carried) {
    const auto key = (output.endpoint ? "endpoint/" : "node/") + output.output;
    // Request destinations are targets, each flow of a target of several (destination()).
    std::vector<std::string> destinations;
    if (response)
      destinations = names(plan.sources);
    else
      std::ranges::transform(request_destinations(), std::back_inserter(destinations),
                             [](const auto &d) { return d.first; });
    auto hit = [&](ValueId flow, unsigned i) {
      return c.or_(c.eq(flow, 2 * i), c.eq(flow, 2 * i + 1));
    };
    auto routed = [&](unsigned i) { return response ? hit(route, i) : c.eq(route, i); };
    // Of the destinations this input carries, the output takes
    // `here` and the other outputs `elsewhere`. With nothing
    // elsewhere the selection is constant; otherwise it compares
    // the shorter list (inverted when it is `elsewhere`).
    std::vector<unsigned> here, elsewhere, split;
    for (unsigned i = 0; i < destinations.size(); ++i)
      if (const auto at = carried.find(destinations[i]); at != carried.end()) {
        if (at->second.size() > 1 && at->second.contains(key))
          split.push_back(i);
        (at->second.size() == 1 && at->second.contains(key) ? here : elsewhere).push_back(i);
      }
    if (!split.empty() && response && !path)
      throw std::logic_error("Response routes split at " + node + " without a route path");
    // Of a split destination, the route paths that leave by this output.
    auto paths = [&](unsigned i, auto keep) {
      auto any = c.bit(0);
      std::set<unsigned> seen;
      for (const auto &[entry, value] : carried.at(destinations[i]).at(key))
        if (keep(entry) && seen.insert(value).second)
          any = c.or_(any, c.eq(*path, value));
      return any;
    };
    const auto &compared = here.size() <= elsewhere.size() ? here : elsewhere;
    auto selected = c.bit(elsewhere.empty() ? 1 : 0);
    if (response || elsewhere.empty()) {
      selected = std::accumulate(compared.begin(), compared.end(), selected,
                                 [&](ValueId any, unsigned i) { return c.or_(any, routed(i)); });
      if (!elsewhere.empty() && here.size() > elsewhere.size())
        selected = c.not_(selected);
    } else {
      // A request decides on the target index bits that tell `here` from `elsewhere`.
      std::vector<DecisionBlock> blocks;
      for (const auto *set : {&here, &elsewhere})
        std::ranges::transform(*set, std::back_inserter(blocks),
                               [&](unsigned i) { return DecisionBlock{i, 0, set == &here}; });
      std::ranges::sort(blocks, {}, &DecisionBlock::base);
      Decided decided{route};
      selected = decide(decided, decision(blocks, plan.target_bits));
    }
    // A split destination: its return paths, or the sources (and with a route path, their flows)
    // whose routes toward it leave by this output.
    const auto sources = names(plan.sources);
    for (const auto i : split) {
      if (response) {
        selected =
            c.or_(selected, c.and_(routed(i), paths(i, [](const std::string &) { return true; })));
        continue;
      }
      std::set<std::string> entries;
      for (const auto &leaving : carried.at(destinations[i]).at(key))
        entries.insert(leaving.first);
      const auto from = std::accumulate(
          entries.begin(), entries.end(), c.bit(0), [&](ValueId any, const std::string &entry) {
            const auto si =
                static_cast<unsigned>(std::ranges::find(sources, entry) - sources.begin());
            auto term = hit(source, si);
            if (path && plan.sources[si].flows.size() > 1)
              term =
                  c.and_(term, paths(i, [&](const std::string &other) { return other == entry; }));
            return c.or_(any, term);
          });
      selected = c.or_(selected, c.and_(routed(i), from));
    }
    // Queued sources: the selection and its route operand, for the
    // destination self-check over this switch's own logic.
    if (!response &&
        std::ranges::any_of(plan.sources, [](const auto &s) { return s.outstanding > 1; })) {
      const auto at = std::string("decode/graph/req/") + node + ">" +
                      (output.endpoint ? "endpoint/" : "node/") + output.output;
      c.bind(at + ".select/" + input, selected);
      c.bind(at + ".route/" + input, route);
      if (!split.empty())
        c.bind(at + ".source/" + input, source);
      if (!split.empty() && path)
        c.bind(at + ".path/" + input, *path);
    }
    return selected;
  }

  // An arbitrated candidate's flow and head marker, named for inspection with its level.
  void name_candidate(const std::string &at, const SwitchOutputPlan &output, std::size_t k,
                      const Channel &in, bool response, unsigned word) {
    if (!c.level_hint_width() || !plan.source_bits || output.inputs.size() < 2)
      return;
    const auto field = [&](const char *name) {
      return in.data[c.burst_field_index(response, word, name)];
    };
    c.bind(at + ".flow" + std::to_string(k), field("source"));
    c.bind(at + ".first" + std::to_string(k), field("first"));
  }
  // One direction's transport graph from its entries to its exits: each switch output arbitrates
  // the packets its inputs select for it (reads and writes together), each link takes its planned
  // stages.
  Channels compose_transport(bool response, const Channels &entries) {
    Channels exits;
    const auto &g = response ? plan.exchange.response : plan.exchange.request;
    const std::string prefix = response ? "rsp/" : "req/";
    // The destinations a switch input can carry, each with the output its routes leave by: those
    // of the admitted routes that enter the switch through it. Every packet on the input is bound
    // for one of them (the source completes every other access locally), so its decode compares
    // only these windows or sources.
    std::map<std::pair<std::string, std::string>, std::map<std::string, Leaving>> carried;
    for (const auto &route : g.routes) {
      // A response's return path, else the request flows of the route (flow 0 for one).
      const auto tags = response              ? std::vector<unsigned>{return_path(g, route)}
                        : route.flows.empty() ? std::vector<unsigned>{0}
                                              : route.flows;
      const auto to = response ? route.exit : destination(route.exit, route.target_path);
      for (std::size_t i = 0; i < route.nodes.size(); ++i)
        for (const auto tag : tags)
          carried[{route.nodes[i], i ? "node/" + route.nodes[i - 1] : "endpoint/" + route.entry}]
                 [to][i + 1 < route.nodes.size() ? "node/" + route.nodes[i + 1]
                                                 : "endpoint/" + route.exit]
                     .insert({route.entry, tag});
    }
    std::map<std::pair<std::string, std::string>, Channel> incoming;
    for (const auto &a : g.graph.entries)
      incoming.emplace(
          std::pair{a.node, "endpoint/" + a.endpoint},
          c.lower_link(link(prefix + a.endpoint + ">" + a.node), entries.at(a.endpoint)));
    // Each output once its inputs exist: one pass in topological order, or more over a cyclic
    // graph (planning admits one only when no route chains its cycle).
    std::map<std::pair<std::string, std::string>, ValueId> readies;
    std::set<const SwitchOutputPlan *> done;
    for (std::size_t pass = 0; done.size() < g.outputs.size(); ++pass)
      for (const auto &node : g.topological_order) {
        if (pass > g.outputs.size())
          throw std::logic_error("No switch output order in " + prefix + node);
        std::vector<const SwitchOutputPlan *> ready;
        for (const auto &output : g.outputs)
          if (output.node == node && !done.contains(&output) &&
              std::ranges::all_of(output.inputs,
                                  [&](const auto &key) { return incoming.contains({node, key}); }))
            ready.push_back(&output);
        if (ready.empty())
          continue;
        const auto widths = c.burst_fragment_widths(response, word_of(node));
        // A node's packet tail marker and route field (requests the target, responses the flow);
        // a single target takes no request route field.
        const auto last = c.burst_field_index(response, word_of(node), "last");
        const auto field = response ? "source" : "target";
        const bool routed = response || plan.target_bits;
        const auto route = routed ? c.burst_field_index(response, word_of(node), field) : 0;
        for (const auto &[key, ch] : incoming)
          if (key.first == node && !readies.contains(key))
            readies.emplace(key, c.bit(0));
        for (const auto *at_output : ready) {
          const auto &output = *at_output;
          done.insert(at_output);
          c.select_object(prefix + node);
          // An arbiter's state moves only while a candidate is valid.
          auto *gated = output.inputs.size() > 1 ? c.region(prefix + node) : nullptr;
          const auto first = b.ir.states.size();
          std::vector<Channel> candidates;
          for (const auto &key : output.inputs) {
            const auto &in = incoming.at({node, key});
            c.consume(gated, prefix + node, in.valid);
            const auto &leaving = carried.at({node, key});
            // The source flow field, compared only where routes toward one target split.
            const bool split = std::ranges::any_of(
                leaving, [](const auto &destination) { return destination.second.size() > 1; });
            const auto selected = switch_select(
                response, node, key, output, routed ? in.data[route] : c.bit(0),
                split ? in.data[c.burst_field_index(false, word_of(node), "source")] : c.bit(0),
                split && plan.burst_path_bits
                    ? std::optional{in.data[c.burst_field_index(response, word_of(node), "path")]}
                    : std::nullopt,
                leaving);
            auto filtered = c.channel(widths);
            c.drive(filtered.valid, c.and_(in.valid, selected));
            for (std::size_t f = 0; f < widths.size(); ++f)
              c.drive(filtered.data[f], in.data[f]);
            // An input presses only the output its packet takes.
            if (in.level_hint)
              filtered.level_hint = c.masked(*in.level_hint, selected);
            readies.at({node, key}) =
                c.or_(readies.at({node, key}), c.and_(selected, filtered.ready));
            // The hop of this input through this output and its selection, named for inspection.
            const auto hop = "graph/" + prefix + node + ">" +
                             (output.endpoint ? "endpoint/" : "node/") + output.output + ".hop/" +
                             key;
            c.bind(hop + ".valid", filtered.valid);
            c.bind(hop + ".ready", filtered.ready);
            c.bind(hop + ".select", selected);
            name_candidate(hop.substr(0, hop.find(".hop/")), output, candidates.size(), in,
                           response, word_of(node));
            candidates.push_back(filtered);
          }
          auto sink = c.channel(widths);
          const auto at =
              prefix + node + ">" + (output.endpoint ? "endpoint/" : "node/") + output.output;
          const auto id = "graph/" + at;
          const auto arbiter = plan.arbiters.find(at);
          if ((arbiter == plan.arbiters.end()) != (candidates.size() < 2))
            throw std::logic_error("Unplanned arbiter at " + id);
          c.graph_merge(id, candidates, sink, last,
                        arbiter == plan.arbiters.end() ? treadle::Spec{} : arbiter->second.spec,
                        response);
          if (gated)
            gated->claim(b, first);
          // A single candidate passes through: the sink keeps its producer.
          const auto from = c.producers[incoming.at({node, output.inputs.front()}).valid.index];
          c.producers[sink.valid.index] = output.inputs.size() > 1 ? prefix + node : from;
          // A source takes its responses without arbitration: their pressure ends at the exit.
          if (response && output.endpoint)
            sink.level_hint.reset();
          const auto tail = c.lower_link(link(prefix + node + ">" + output.output), sink);
          if (output.endpoint)
            exits.emplace(output.output, tail);
          else
            incoming.emplace(std::pair{output.output, "node/" + node}, tail);
        }
        if (std::ranges::none_of(
                g.outputs, [&](const auto &o) { return o.node == node && !done.contains(&o); }))
          for (const auto &[key, ready_bit] : readies)
            if (key.first == node)
              c.drive(incoming.at(key).ready, ready_bit);
      }
    return exits;
  }

  // A raise packet (message.hpp point 15) leaves the request path after the dispatch stage: it is
  // taken at once and passed no further.
  Channel discard_raises(const Channel &in, unsigned word) {
    if (!plan.regulated())
      return in;
    const auto raise = c.and_(in.data[c.burst_field_index(false, word, "raise")], in.valid);
    Channel out{c.wire(1), c.wire(1), in.data};
    c.drive(out.valid, c.and_(in.valid, c.not_(raise)));
    c.drive(in.ready, c.or_(out.ready, raise));
    return out;
  }
  // Each in-flight request owns a context until its response tail transfers.
  // Raises update all lower-level contexts of that source. The response path combines
  // active contexts with sampled admission and output congestion demands.
  struct TargetLevelHint {
    ValueId level, level_hint;
  };
  TargetLevelHint target_level_hint(const TargetSide &t, const TargetPlan &tp,
                                    const Channel &offered, const Channel &arrived,
                                    const Channel &response, ValueId stall) {
    const auto id = "target/" + tp.name + "/";
    const auto field = [&](const Channel &ch, bool rsp, const char *name) {
      return ch.data[rsp ? c.endpoint_field_index(true, name)
                         : c.burst_field_index(false, t.word, name)];
    };
    const auto raised = plan.regulated() ? field(arrived, false, "raise") : c.bit(0);
    // The request pressure at the context allocation, through the arrival pipe.
    const PressurePath arrival_model{b, c.level_hint_width()};
    const PressureSlot packet{arrival_model.encode(field(offered, false, "level")),
                              field(offered, false, "first")};
    const auto it = std::ranges::find(tp.pipes, "arrival", pipe_place);
    const auto piped = lower_qos_level_hint(
        b, c.clock, c.reset, offered.valid, arrival_model.join({packet}), *offered.level_hint,
        arrived.ready, it == tp.pipes.end() ? LinkPipeline{} : it->mode);
    const std::array bound{"arrival.occupancy", "arrival.kept", "arrival"};
    for (std::size_t i = 0; i < piped.words.size(); ++i)
      c.bind("level_hint/" + id + bound[bound.size() - piped.words.size() + i], piped.words[i]);
    const auto inside = c.level_hint_owned(id + "inside", 1);
    b.update(inside, b.select(c.transfer(arrived), c.not_(field(arrived, false, "last")), inside));
    const auto head = c.not_(inside);
    const auto flow = field(arrived, false, "source");
    const auto flow_bits = b.ir.values.at(flow.index).width;
    const auto tagged = plan.burst_tag_bits > 0;
    struct Context {
      ValueId active, flow, level;
      std::optional<ValueId> tag;
    };
    std::vector<Context> contexts;
    for (unsigned i = 0; i < std::max(tp.outstanding, 1U); ++i) {
      const auto at = id + "context" + decimal(i) + ".";
      contexts.push_back(
          {c.level_hint_owned(at + "used", 1), c.level_hint_owned(at + "flow", flow_bits),
           c.level_hint_owned(at + "level", 3),
           tagged ? std::optional{c.level_hint_owned(at + "tag", plan.burst_tag_bits)}
                  : std::nullopt});
    }
    const auto level = field(arrived, false, "level");
    const auto start =
        c.and_(c.and_(c.transfer(arrived), field(arrived, false, "first")), c.not_(raised));
    const auto lift = c.and_(c.and_(arrived.valid, field(arrived, false, "first")), raised);
    // A source's flows differ in their low (direction) bit only.
    const auto source_of = [&](ValueId x) {
      return flow_bits > 1 ? b.slice(x, 1, flow_bits - 1) : c.bit(0);
    };
    const auto rsp_flow = field(response, true, "source");
    const auto done = c.and_(c.transfer(response), field(response, true, "last"));
    auto taken = c.bit(0), found = c.bit(0), all = c.bit(1);
    auto own = b.constant(3, 0);
    const PressurePath path{b, c.level_hint_width()};
    std::vector<PressureSlot> demands;
    for (const auto &context : contexts) {
      const auto take = c.and_(c.and_(start, c.not_(context.active)), c.not_(taken));
      taken = c.or_(taken, c.not_(context.active));
      auto match = c.and_(context.active, b.binary(Op::Equal, context.flow, rsp_flow));
      if (tagged)
        match = c.and_(match, b.binary(Op::Equal, *context.tag, field(response, true, "tag")));
      const auto release = c.and_(c.and_(done, match), c.not_(found));
      own = b.select(c.and_(match, c.not_(found)), context.level, own);
      found = c.or_(found, match);
      all = c.and_(all, context.active);
      const auto up = c.and_(c.and_(c.and_(lift, context.active),
                                    b.binary(Op::Equal, source_of(context.flow), source_of(flow))),
                             b.binary(Op::UnsignedLess, context.level, level));
      b.update(context.active, c.or_(take, c.and_(context.active, c.not_(release))));
      b.update(context.flow, b.select(take, flow, context.flow));
      if (tagged)
        b.update(*context.tag, b.select(take, field(arrived, false, "tag"), *context.tag));
      b.update(context.level, b.select(c.or_(take, up), level, context.level));
      demands.push_back({path.encode(context.level), context.active});
    }
    c.bind(t.key + "/context_overflow", c.and_(start, all));
    // A free context is offered a cycle after it is free, and not in the cycle after one is
    // taken; a request head waits for the offer (held back).
    const auto offer = c.level_hint_owned(id + "offered", 1);
    b.update(offer, c.and_(c.not_(all), c.not_(start)));
    struct WaitSample {
      ValueId demand, applies;
      const char *name = nullptr;
    };
    const std::array waits{
        WaitSample{piped.value, c.and_(c.and_(head, c.not_(offer)), c.not_(raised)), "admission"},
        WaitSample{
            path.join({{piped.value, c.bit(1)}, {path.encode(level), c.and_(head, arrived.valid)}}),
            stall, "response"}};
    for (const auto &wait : waits) {
      const auto sampled = c.level_hint_state(id + wait.name, path.mask(wait.demand, wait.applies));
      demands.push_back({sampled, c.bit(1)});
    }
    return {own, path.mask(path.join(demands), response.valid)};
  }
  // An interleaving target's segment end toward the transport (message.hpp point 17); none where
  // the transport has no marker. A clamp a target can cut for gathers per read (clamp_reassembles).
  std::optional<ValueId> response_segment(TargetSide &t) {
    if (!plan.response_segments)
      return std::nullopt;
    t.cut = c.wire(1);
    return t.cut;
  }
  Channel attach_target(unsigned o, const Channel &offered) {
    const auto &tp = plan.targets[o];
    const auto &target = tp.name;
    c.select_object("target/" + target);
    // The pressure ends at the target (target_level_hint reads it from offered).
    auto arriving = offered;
    arriving.level_hint.reset();
    TargetSide t;
    const auto first = b.ir.states.size();
    t.key = "burst_service/" + target;
    t.word = word_at(target);
    t.bits_ep = plan.per_endpoint ? t.word * 8 : 0;
    t.upsize = tp.upsize.has_value();
    // A shared-bus target serves in arrival order: a packet waits while packets of the other
    // direction (turn) are between acquisition and release (count, at most four).
    t.shared_bus =
        ahb_target(plan, target) || apb_target(plan, target) || lite_target(plan, target);
    // The arrival count holds the packets of one direction between
    // acquisition and release: a pipelined AXI4-Lite outlet holds up to its
    // packet capacity.
    if (lite_target(plan, target))
      t.count_bits = std::max(3U, static_cast<unsigned>(std::bit_width(tp.outstanding)));
    t.count = t.shared_bus ? b.state(t.count_bits, c.clock, c.reset) : c.bit(0);
    t.turn = t.shared_bus ? b.state(1, c.clock, c.reset) : c.bit(0);
    // Formal-observation anchors: the request packet as it leaves the network
    // toward this target, and the response packet as it re-enters it.
    const auto write = arriving.data[c.burst_field_index(false, t.word, "write")];
    c.bind(t.key + "/offer_valid", arriving.valid);
    c.bind(t.key + "/offer_write", write);
    c.bind(t.key + "/offer_error", arriving.data[c.burst_field_index(false, t.word, "error")]);
    const auto object = "target/" + target;
    auto arrived = endpoint_pipe(tp.pipes, "arrival", object, arriving);
    const auto arrival = arrived;
    // An AXI4 outlet's dispatch stage sits after admission: a head enters it only while the outlet
    // pool has room for it, and counts there (Axi4OutletProfile::admitted).
    if (!t.shared_bus && !t.upsize && tp.protocol == Protocol::Axi4 &&
        std::ranges::any_of(tp.pipes,
                            [](const EndpointPipe &e) { return pipe_place(e) == "dispatch"; })) {
      t.admit_ok = {c.wire(1), c.wire(1)};
      const auto starts = arrived.data[c.burst_field_index(false, t.word, "first")];
      const auto writes = arrived.data[c.burst_field_index(false, t.word, "write")];
      // A raise packet takes no room in the pool.
      const auto raise =
          plan.regulated() ? arrived.data[c.burst_field_index(false, t.word, "raise")] : c.bit(0);
      const auto allowed =
          c.or_(c.or_(c.not_(starts), raise), b.select(writes, (*t.admit_ok)[0], (*t.admit_ok)[1]));
      auto gated = c.like(arrived);
      c.drive(gated.valid, c.and_(arrived.valid, allowed));
      for (std::size_t f = 0; f < arrived.data.size(); ++f)
        c.drive(gated.data[f], arrived.data[f]);
      c.drive(arrived.ready, c.and_(gated.ready, allowed));
      const auto head = c.and_(c.and_(c.transfer(gated), starts), c.not_(raise));
      t.admits = {c.and_(head, writes), c.and_(head, c.not_(writes))};
      arrived = gated;
    }
    const auto taken = discard_raises(endpoint_pipe(tp.pipes, "dispatch", object, arrived), t.word);
    std::array<ValueId, 2> open{c.or_(c.eq(t.count, 0), c.not_(t.turn)),
                                c.or_(c.eq(t.count, 0), t.turn)};
    // An AXI4-Lite target of several flows lets a packet of another flow in only when none is in
    // flight: the packets in flight share one flow, that of the last packet let in.
    if (lite_target(plan, target) && tp.flows > 1) {
      t.flow = b.state(tp.flow_bits(), c.clock, c.reset);
      // The packet's flow: its target index (message.hpp point 15) less that of the first flow.
      const auto all = request_destinations();
      const auto flow0 = static_cast<std::uint64_t>(
          std::ranges::find(all, destination(target, 0U),
                            &std::pair<std::string, std::vector<AddressWindow>>::first) -
          all.begin());
      const auto flow =
          b.slice(b.binary(Op::Subtract, taken.data[c.burst_field_index(false, t.word, "target")],
                           b.constant(plan.target_bits, flow0)),
                  0, tp.flow_bits());
      const auto same = c.or_(c.eq(t.count, 0), b.binary(Op::Equal, flow, *t.flow));
      open = {c.and_(open[0], same), c.and_(open[1], same)};
      b.update(*t.flow, b.select(c.and_(c.transfer(taken),
                                        taken.data[c.burst_field_index(false, t.word, "first")]),
                                 flow, *t.flow));
    }
    // An AHB target holds one packet of either direction from its head to its response tail: a
    // packet head waits until no packet is in flight (one transaction context, as the bus serves
    // one transfer sequence at a time).
    if (ahb_target(plan, target) || tp.service) {
      const auto one =
          c.or_(c.not_(taken.data[c.burst_field_index(false, t.word, "first")]), c.eq(t.count, 0));
      open = {c.and_(open[0], one), c.and_(open[1], one)};
    }
    // A default target answers one decode miss at a time. It takes a miss head once it is empty
    // (a shared-bus target sees it one cycle after it arrives, so its local answer leaves as late
    // as at an AXI4 target) (count: no packet of either direction between
    // acquisition and release; at an AXI4 target its outlet's in-flight pool). From then on
    // it takes no packet head until it is empty again and the miss's response tail has left
    // (miss_answer; miss_replied: the tail has left while it is not empty yet).
    ValueId pending{};
    if (!fallbacks) {
      fallbacks.emplace();
      for (const auto &s : plan.sources)
        if (const auto d = default_target(plan, s.name))
          fallbacks->insert(*d);
    }
    if (fallbacks->contains(o)) {
      t.empty = t.shared_bus ? c.eq(t.count, 0) : c.wire(1);
      const auto starts = taken.data[c.burst_field_index(false, t.word, "first")];
      const auto error_bit = taken.data[c.burst_field_index(false, t.word, "error")];
      t.miss_seen = b.state(1, c.clock, c.reset);
      t.miss_answer = b.state(1, c.clock, c.reset);
      t.miss_replied = b.state(1, c.clock, c.reset);
      const auto seen =
          t.shared_bus ? *t.miss_seen
                       : c.or_(*t.miss_seen,
                               c.not_(taken.data[c.burst_field_index(false, t.word, "write")]));
      const auto empty = c.and_(seen, *t.empty);
      const auto quiet = c.not_(*t.miss_answer);
      const auto pass =
          c.or_(c.not_(starts), c.or_(c.and_(quiet, c.not_(error_bit)), c.and_(quiet, empty)));
      open = {c.and_(open[0], pass), c.and_(open[1], pass)};
      pending = c.and_(taken.valid, c.and_(starts, error_bit));
    }
    const auto faces =
        c.steer(taken, c.not_(taken.data[c.burst_field_index(false, t.word, "write")]), open, 1,
                false, t.word);
    std::tie(t.wreq, t.rreq, t.wrsp, t.rrsp) = ep_faces(t.bits_ep);
    // The global address bits above the transport's are those of the target's span.
    const auto span = transport_span(plan, tp).base;
    c.deframe_burst_into(false, faces[0], t.wreq, t.word, span);
    c.deframe_burst_into(false, faces[1], t.rreq, t.word, span);
    const bool joined = !t.shared_bus && joined_target(plan, tp);
    if (!t.shared_bus && tp.protocol == Protocol::Axi4 && tp.outstanding > 1 && !tp.write_only &&
        !joined)
      t.picked.emplace();
    const auto rejoined =
        joined ? c.joined_responses({t.wrsp, t.rrsp}, t.upsize && tp.outstanding > 1)
               : c.rejoin_responses({t.wrsp, t.rrsp}, c.endpoint_field_index(true, "first"),
                                    c.endpoint_field_index(true, "last"), t.merger,
                                    t.picked ? &*t.picked : nullptr);
    const auto segment = joined ? response_segment(t) : std::nullopt;
    if (plan.response_runs)
      t.wrap_end = c.wire(1);
    // A default target takes a miss only when empty and nothing else until its answer has left:
    // a response leaving while a miss is open and unanswered answers it (return_of).
    if (t.miss_answer)
      miss_answers[target] = c.and_(*t.miss_answer, c.not_(*t.miss_replied));
    // The feedback word holds the blocked output and the level of an offered response.
    std::optional<TargetLevelHint> pressed;
    std::optional<ValueId> feedback, next_offer, level;
    if (c.level_hint_width()) {
      feedback = c.level_hint_owned("target/" + target + "/feedback", 5);
      const auto response_pending = b.slice(*feedback, 1, 1), kept = b.slice(*feedback, 2, 3);
      c.bind("pressure/target/" + target + "/response.pending", response_pending);
      c.bind("pressure/target/" + target + "/response.level", kept);
      pressed = target_level_hint(t, tp, offered, arrival, rejoined, b.slice(*feedback, 0, 1));
      level = b.select(c.and_(rejoined.valid, c.not_(response_pending)), pressed->level, kept);
      next_offer =
          b.concat(*level, c.and_(c.not_(rejoined.ready), c.or_(response_pending, rejoined.valid)));
    }
    auto framed = c.frame_burst(true, rejoined, std::nullopt, t.word,
                                plan.burst_path_bits ? std::optional{return_of(target, rejoined)}
                                                     : std::nullopt,
                                std::nullopt, 0, level, segment, t.wrap_end);
    if (pressed)
      framed.level_hint = pressed->level_hint;
    const auto sent = endpoint_pipe(tp.pipes, "response", object, framed, true);
    if (feedback)
      b.update(*feedback, b.concat(*next_offer, b.select(sent.valid, c.not_(sent.ready),
                                                         b.slice(*feedback, 0, 1))));
    if (t.miss_answer) {
      const auto tail = c.and_(c.and_(sent.valid, sent.ready),
                               sent.data[c.burst_field_index(true, t.word, "last")]);
      const auto replied = c.or_(tail, *t.miss_replied), done = c.and_(*t.empty, replied);
      b.update(*t.miss_seen, c.and_(c.and_(pending, c.not_(taken.ready)), c.not_(*t.miss_answer)));
      b.update(*t.miss_replied, c.and_(c.and_(*t.miss_answer, c.not_(done)), replied));
      b.update(*t.miss_answer,
               c.or_(c.and_(pending, taken.ready), c.and_(*t.miss_answer, c.not_(done))));
    }
    observe_target(o, t, rejoined);
    c.bind(t.key + "/emit_valid", sent.valid);
    c.bind(t.key + "/emit_result", sent.data[c.burst_field_index(true, t.word, "result")]);
    c.bind(t.key + "/emit_route", sent.data[c.burst_field_index(true, t.word, "source")]);
    // The transport zone: the arrival order, the response merger (resting once it has seen no
    // R transfer for a cycle) and the upsize merge (import_outlet); it wakes on an arriving
    // request or a response of the protocol zone.
    if (auto *zone = c.region("target/" + target)) {
      zone->claim(b, first);
      if (!t.merger.empty())
        zone->idle.push_back(c.not_(b.slice(t.merger.front(), 0, 1)));
      c.consume(zone, "target/" + target, arriving.valid);
      c.producers[sent.valid.index] = "target/" + target;
    }
    attach_engine(o, t, sent);
    return sent;
  }
  // The engine on a target's faces, by what the target is.
  void attach_engine(unsigned o, TargetSide &t, const Channel &sent) {
    if (plan.targets[o].service && crossed_bank(o)) {
      c.bind(t.key + "/reply.valid", sent.valid);
      c.bind(t.key + "/reply.ready", sent.ready);
    }
    if (plan.targets[o].service)
      attach_target_service(o, t);
    else if (t.shared_bus)
      attach_target_bus(o, t);
    else
      attach_target_axi4(o, t);
  }

  // The outlet on the target's faces; a target wider than a source it serves merges the narrow
  // beats between the network and its outlet, on the target layout.
  Imported import_outlet(unsigned o, TargetSide &t, const NodeHardware &outlet,
                         const std::map<std::string, unsigned> &ports,
                         std::map<std::string, ValueId> hints = {}) {
    const auto &target = plan.targets[o].name;
    auto [out_wreq, out_rreq, out_wrsp, out_rrsp] =
        t.upsize ? ep_faces(t.bits_ep) : std::tuple{t.wreq, t.rreq, t.wrsp, t.rrsp};
    auto first = b.ir.states.size();
    const auto stores = c.out.storage.size();
    // An AXI4 outlet with several reads behind the merge frees a read's entry after the merge's
    // last word of it.
    const bool tails = t.upsize && plan.targets[o].protocol == Protocol::Axi4 &&
                       (plan.targets[o].outstanding > 1 || joined_target(plan, plan.targets[o]));
    if (tails)
      hints["read_tail"] = c.wire(1);
    const bool stored =
        t.upsize && plan.targets[o].outstanding > 1 && joined_target(plan, plan.targets[o]);
    if (stored)
      hints["read_stored"] = c.wire(1);
    if (t.admits)
      hints.insert({{"write_admit", (*t.admits)[0]}, {"read_admit", (*t.admits)[1]}});
    if (t.picked)
      hints.insert({{"write_picked", (*t.picked)[0]}, {"read_picked", (*t.picked)[1]}});
    auto ep = c.import_endpoint(
        outlet, t.key + "/", "nl_t_" + target + "_", "target:" + target,
        {{"wreq", &out_wreq}, {"wrsp", &out_wrsp}, {"rreq", &out_rreq}, {"rrsp", &out_rrsp}}, ports,
        hints, plan.targets[o].pipes);
    c.bind(t.key + "/outlet.read_valid", out_rrsp.valid);
    if (t.cut && !t.upsize)
      c.drive(*t.cut, ep.bound.at("cut"));
    if (t.wrap_end && !t.upsize)
      c.drive(*t.wrap_end, ep.bound.at("wrap_end"));
    // The protocol zone wakes on a request or a response valid of the target face; the transport
    // zone on the outlet's responses.
    auto *transport = c.region("target/" + target);
    if (auto *zone = c.region("target/" + target + "/protocol")) {
      zone->claim(b, first, ep.states, outlet_free);
      zone->idle.push_back(at_rest(b, ep.states, outlet_rest));
      c.hold_open(zone, stores);
      for (const auto *signal : {"bvalid", "rvalid"})
        if (ep.axi.contains(signal))
          zone->wake.push_back(ep.axi.at(signal));
      zone->wake.insert(zone->wake.end(), {out_wreq.valid, out_rreq.valid});
      // A head admitted ahead (into the dispatch stage) counts in the outlet's pool at once.
      if (t.admits)
        zone->wake.insert(zone->wake.end(), {(*t.admits)[0], (*t.admits)[1]});
    }
    if (transport)
      transport->wake.insert(transport->wake.end(),
                             {out_wrsp.valid, read_face_wake(t, ep, out_rrsp.valid)});
    first = b.ir.states.size();
    if (t.upsize) {
      t.merge = c.import_endpoint(admitted(lower_axi4_upsize(upsize_profile(plan, o))),
                                  t.key + "/merge/", "", "",
                                  {{"up_wreq", &t.wreq},
                                   {"up_wrsp", &t.wrsp},
                                   {"up_rreq", &t.rreq},
                                   {"up_rrsp", &t.rrsp},
                                   {"down_wreq", &out_wreq},
                                   {"down_wrsp", &out_wrsp},
                                   {"down_rreq", &out_rreq},
                                   {"down_rrsp", &out_rrsp}});
      if (transport) {
        transport->claim(b, first);
        transport->idle.push_back(at_rest(b, t.merge.states, upsize_rest));
      }
      if (t.cut)
        c.drive(*t.cut, c.and_(ep.bound.at("cut"), t.merge.bound.at("word_end")));
      if (t.wrap_end)
        c.drive(*t.wrap_end, t.merge.bound.at("wrap_end"));
      if (stored)
        c.drive(hints.at("read_stored"), t.merge.bound.at("read_stored"));
      if (tails) {
        c.drive(hints.at("read_tail"),
                c.and_(c.transfer(t.rrsp), t.rrsp.data[c.endpoint_field_index(true, "last")]));
        c.bind(t.key + "/outlet.read_tail", hints.at("read_tail"));
        // The merge's last beat of a read may leave while the outlet's protocol zone rests.
        if (auto *zone = c.region("target/" + target + "/protocol"))
          zone->wake.push_back(tail_wake(t, hints.at("read_tail")));
      }
    }
    return ep;
  }

  // One direction's packet record (write, then read). With a merge in front, a packet's
  // lifecycle spans the merge and the outlet, from its first source beat to its B or last R.
  void record(TargetSide &t, bool read, ValueId acquire, ValueId release,
              std::vector<ValueId> payload, std::vector<ValueId> control) {
    const auto &request = read ? t.rreq : t.wreq;
    const auto &response = read ? t.rrsp : t.wrsp;
    if (t.upsize) {
      acquire = c.and_(c.transfer(request), request.data[c.endpoint_field_index(false, "first")]);
      release = c.transfer(response);
      if (read)
        release = c.and_(release, response.data[c.endpoint_field_index(true, "last")]);
    }
    const auto resource = t.key + (read ? "/read" : "/write");
    c.bind(resource + ".acquire", acquire);
    c.bind(resource + ".release", release);
    t.acquired[read] = acquire;
    t.released[read] = release;
    if (t.shared_bus && read) {
      // Count minus (out - in); one direction acquires at a time.
      const auto in = c.or_(t.acquired[0], t.acquired[1]),
                 out = c.or_(t.released[0], t.released[1]);
      const auto step = b.select(out, b.constant(t.count_bits, 1), b.constant(t.count_bits, 0));
      b.update(
          t.count,
          b.binary(Op::Subtract, t.count,
                   b.select(in, b.binary(Op::Subtract, step, b.constant(t.count_bits, 1)), step)));
      b.update(t.turn, b.select(in, t.acquired[1], t.turn));
    } else if (t.miss_answer && read) {
      control.insert(control.end(), {*t.miss_seen, *t.miss_answer, *t.miss_replied});
    }
    for (const auto &[name, q] : t.merge.states)
      if (name.starts_with(read ? "r." : "w."))
        payload.push_back(q);
    if (read)
      control.insert(control.end(), t.merger.begin(), t.merger.end());
    c.out.storage.push_back({resource, c.clock, payload, control, acquire, release});
  }

  // A service target: the outlet and its bank on one byte-serial path. The bank's fields answer
  // from its own registers; a status field reads zero until something drives it.
  void attach_target_service(unsigned o, TargetSide &t) {
    const auto &tp = plan.targets[o];
    const auto &banks = tp.service->banks;
    const auto ready = c.wire(1), out_valid = c.wire(1), out_tail = c.wire(1);
    const auto out_data = c.wire(10);
    const auto outlet =
        import_outlet(o, t, admitted(lower_service_outlet(service_outlet_profile(plan, o))), {},
                      {{"bank_ready", ready},
                       {"bank_out_valid", out_valid},
                       {"bank_out_data", out_data},
                       {"bank_out_tail", out_tail}});
    if (t.wrap_end)
      c.drive(*t.wrap_end, outlet.bound.at("wrap_end"));
    // Read data arrives with no word on a face; the bank itself keeps the domain's clock.
    if (auto *zone = c.region("target/" + tp.name + "/protocol"))
      zone->wake.push_back(out_valid);
    std::vector<Imported> imported_banks;
    std::map<std::string, ValueId> switch_states;
    if (banks.back().branch == 0) {
      const BankRequest face{
          [&](const std::string &signal) { return outlet.bound.at("bank_" + signal); },
          outlet.bound.at("bank_out_ready")};
      const BankOutputs answer{ready, out_valid, out_data, out_tail};
      if (crossed_bank(o))
        import_crossed_branch(o, t, 0, face, answer, imported_banks);
      else
        import_branch(o, t, 0, face, answer, imported_banks);
    } else {
      // The word register stands between the outlet and the branches.
      const auto branch_count = banks.back().branch + 1;
      const auto hardware = admitted(lower_service_switch("t" + std::to_string(o), branch_count));
      std::vector<BankOutputs> branches;
      std::ranges::generate_n(std::back_inserter(branches), branch_count, [&] {
        return BankOutputs{c.wire(1), c.wire(1), c.wire(10), c.wire(1)};
      });
      Imported fabric;
      std::map<std::string, ValueId> faces;
      c.import_component(
          hardware, t.key + "/switch/", fabric,
          [&](const Port &port) -> ValueId {
            const auto &signal = port.signal;
            if (signal.starts_with("in_"))
              return outlet.bound.at("bank_" + signal.substr(3));
            const auto &branch =
                branches.at(std::stoul(signal.substr(signal.find_first_of("0123456789"))));
            if (signal.ends_with("_ready"))
              return branch.ready;
            return signal.ends_with("_valid")  ? branch.valid
                   : signal.ends_with("_data") ? branch.data
                                               : branch.tail;
          },
          [&](const Port &port, ValueId value) {
            if (port.signal == "in_ready")
              c.drive(ready, value);
            else if (port.signal == "back_valid")
              c.drive(out_valid, value);
            else if (port.signal == "back_data")
              c.drive(out_data, value);
            else if (port.signal == "back_tail")
              c.drive(out_tail, value);
            else
              faces[port.signal] = value;
          });
      for (unsigned k = 0; k < branch_count; ++k) {
        const auto out = "out" + std::to_string(k) + "_";
        const BankRequest face{[&](const std::string &signal) { return faces.at(out + signal); },
                               outlet.bound.at("bank_out_ready")};
        if (crossed_bank(o))
          import_crossed_branch(o, t, k, face, branches[k], imported_banks);
        else
          import_branch(o, t, k, face, branches[k], imported_banks);
      }
      if (!crossed_bank(o))
        imported_banks.push_back(std::move(fabric));
      else
        switch_states = fabric.states;
    }
    if (crossed_bank(o))
      own_crossed_banks(o, imported_banks);
    Names words[2];
    words[1].first = {"r.gather"};
    std::set<std::string> assigned;
    for (bool read : {false, true}) {
      std::vector<ValueId> payload, control;
      consume_states(outlet, words[read], assigned, payload, control);
      const auto &request = read ? t.rreq : t.wreq;
      record(t, read, c.transfer(request), c.transfer(read ? t.rrsp : t.wrsp), payload, control);
    }
    std::vector<ValueId> control{t.count, t.turn};
    if (t.miss_seen)
      control.insert(control.end(), {*t.miss_seen, *t.miss_answer, *t.miss_replied});
    for (const auto &[name, value] : outlet.states)
      if (!assigned.contains(name))
        control.push_back(value);
    if (!crossed_bank(o))
      for (const auto &bank : imported_banks)
        for (const auto &[name, value] : bank.states)
          control.push_back(value);
    for (const auto &[name, value] : switch_states)
      control.push_back(value);
    const auto taken = outlet.bound.at("bus.taken");
    c.bind(t.key + "/bus.acquire", taken);
    c.bind(t.key + "/bus.release", taken);
    c.out.storage.push_back({t.key + "/bus", c.clock, {}, control, taken, taken});
  }

  struct BankRequest {
    std::function<ValueId(const std::string &)> signal;
    ValueId answer_ready;
  };
  struct BankOutputs {
    ValueId ready, valid, data, tail;
    std::optional<ValueId> head{};
  };
  bool crossed_bank(unsigned o) const {
    const auto &tp = plan.targets[o];
    return plan.clocked() && !tp.service->domain.empty() &&
           tp.service->domain != plan.object_domains.at("target/" + tp.name);
  }
  // The clock adapter named by a serial path.
  const ClockCut &serial_cut(const std::string &segment) const {
    return *std::ranges::find(plan.cuts, "cut/" + segment, &ClockCut::name);
  }
  // A branch of banks in another clock domain than its target's outlet: the serial words go through
  // one crossing, the answer through another. The banks' registers answer to their own domain.
  void import_crossed_branch(unsigned o, const TargetSide &t, unsigned branch,
                             const BankRequest &from, const BankOutputs &to,
                             std::vector<Imported> &imported) {
    const auto &tp = plan.targets[o];
    const auto &service = *tp.service;
    const auto stem = "service/" + tp.name + service_branch_suffix(service, branch);
    const std::array<std::string, 2> ends{};
    const Channel sent{from.signal("valid"),
                       to.ready,
                       {from.signal("data"), from.signal("head"), from.signal("tail")}};
    const auto request = c.cross_async(serial_cut(stem + "/request"), {10, 1, 1}, sent, ends);
    const auto answer = c.channel({10, 1});
    const auto response = c.cross_async(serial_cut(stem + "/response"), {10, 1}, answer, ends);
    c.drive(to.valid, response.valid);
    c.drive(to.data, response.data[0]);
    c.drive(to.tail, response.data[1]);
    c.drive(response.ready, from.answer_ready);
    const auto kept = std::pair{c.clock, c.reset};
    std::tie(c.clock, c.reset) = c.domains.at(service.domain);
    import_branch(o, t, branch,
                  {[&request](const std::string &signal) {
                     return signal == "valid"  ? request.valid
                            : signal == "data" ? request.data[0]
                            : signal == "head" ? request.data[1]
                                               : request.data[2];
                   },
                   answer.ready},
                  {request.ready, answer.valid, answer.data[0], answer.data[1]}, imported);
    std::tie(c.clock, c.reset) = kept;
  }
  // Names the registers of banks in another domain than the target's.
  void own_crossed_banks(unsigned o, const std::vector<Imported> &imported) {
    for (std::size_t k = 0; k < imported.size(); ++k)
      for (const auto &[name, value] : imported[k].states)
        c.bind("service/bank/" + plan.targets[o].name + "/" + std::to_string(k) + "/" + name,
               value);
  }

  // One bank of a service target. Its request comes as named and takes the answer's ready; the
  // wires it drives are given, and its logger's fields and controls go by the bank's place.
  // A branch's banks in series. A word not for a bank passes it, and so does the answer of a bank
  // before it: one cycle each.
  void import_branch(unsigned o, const TargetSide &t, unsigned branch, const BankRequest &request,
                     const BankOutputs &drives, std::vector<Imported> &imported) {
    const auto &banks = plan.targets[o].service->banks;
    std::vector<unsigned> members;
    for (unsigned k = 0; k < banks.size(); ++k)
      if (banks[k].branch == branch)
        members.push_back(k);
    std::vector<BankOutputs> links;
    std::ranges::generate_n(std::back_inserter(links), members.size() - 1, [&] {
      return BankOutputs{c.wire(1), c.wire(1), c.wire(10), c.wire(1), c.wire(1)};
    });
    for (std::size_t n = 0; n < members.size(); ++n) {
      const auto before = n ? &links[n - 1] : nullptr;
      const auto after = n + 1 < members.size() ? &links[n] : nullptr;
      BankRequest asked{request.signal, after ? after->ready : request.answer_ready};
      if (before)
        asked.signal = [before](const std::string &signal) {
          return signal == "valid"  ? before->valid
                 : signal == "data" ? before->data
                 : signal == "tail" ? before->tail
                                    : *before->head;
        };
      auto outputs = after ? *after : drives;
      outputs.ready = before ? before->ready : drives.ready;
      imported.push_back(import_bank(o, t, members[n], asked, outputs));
    }
  }
  static std::string bank_scope(unsigned k) { return k ? "bank" + std::to_string(k) + "/" : ""; }
  Imported import_bank(unsigned o, const TargetSide &t, unsigned k, const BankRequest &request,
                       const BankOutputs &drives) {
    const auto &service = *plan.targets[o].service;
    const auto bank_hardware = admitted(lower_register_bank(service.banks[k].plan));
    const auto scope = bank_scope(k);
    Imported bank;
    c.import_component(
        bank_hardware, t.key + "/" + (k ? "bank" + std::to_string(k) : std::string("bank")) + "/",
        bank,
        [&](const Port &port) -> ValueId {
          if (port.endpoint == "field") {
            if (!plan.fault_monitors.empty())
              return logger_fields.emplace(scope + port.signal, c.wire(port.width)).first->second;
            return b.constant(port.width, 0);
          }
          if (port.endpoint == "response")
            return request.answer_ready;
          return request.signal(port.signal);
        },
        [&](const Port &port, ValueId value) {
          if (port.endpoint == "field")
            logger_controls[scope + port.signal] = value;
          else if (port.endpoint == "request")
            c.drive(drives.ready, value);
          else if (port.signal == "valid")
            c.drive(drives.valid, value);
          else if (port.signal == "data")
            c.drive(drives.data, value);
          else if (port.signal == "tail")
            c.drive(drives.tail, value);
          else if (port.signal == "head" && drives.head)
            c.drive(*drives.head, value);
        });
    return bank;
  }

  // The error events of an observed target. A request head fixes what its packet's record says; a
  // response word moved makes the event (error_logger.hpp).
  void observe_target(unsigned o, const TargetSide &t, const Channel &rejoined) {
    for (const auto &observer : plan.fault_monitors)
      for (std::size_t k = 0; k < observer.observed.size(); ++k)
        if (observer.observed[k].target == o)
          observed_events[observer.logger.name + "/" + std::to_string(k)] =
              target_events(observer, observer.observed[k], t, rejoined);
  }
  std::map<std::string, ValueId> target_events(const FaultMonitorPlan &observer,
                                               const ObservedTarget &seen, const TargetSide &t,
                                               const Channel &rejoined) {
    const auto &shape = observer.logger.shape;
    const auto id = "observe/" + plan.targets[seen.target].name + "/";
    const auto top_bit = std::uint64_t{1} << (shape.address_bits - 1);
    const auto all_ones = top_bit | (top_bit - 1);
    const auto low_bits = std::min(8U, shape.address_bits - 1);
    const auto field_of = [&](const Channel &request, const char *name) {
      return request.data[c.endpoint_field_index(false, name)];
    };
    const auto fit = [&](ValueId value, unsigned bits) {
      const auto have = b.ir.values.at(value.index).width;
      return have == bits  ? value
             : have < bits ? b.unary(Op::ZeroExtend, value, bits)
                           : b.slice(value, 0, bits);
    };
    // A target of one burst holds one packet, whichever its direction: its head fixes the record.
    const auto request_head = [&](const Channel &request) {
      return c.and_(c.transfer(request), field_of(request, "first"));
    };
    const auto write_head = request_head(t.wreq), read_head = request_head(t.rreq);
    const auto of_request = [&](const char *name) {
      return b.select(write_head, field_of(t.wreq, name), field_of(t.rreq, name));
    };
    const auto start = b.slice(of_request("start_address"), 0, shape.address_bits);
    const auto source = of_request("source");
    const auto miss = of_request("error");
    const auto burst_kind = of_request("burst");
    const auto head = c.or_(write_head, read_head);
    // Every row is one AND-OR term; a request hits one.
    auto route_now = b.constant(shape.route_bits, 0);
    auto kept = b.constant(shape.address_bits, 0);
    for (const auto &row : seen.sources) {
      // A source code is 2 * index + direction.
      const auto from = plan.source_bits > 1
                            ? c.eq(b.slice(source, 1, plan.source_bits - 1), row.source)
                            : b.constant(1, 1);
      const auto low_word = [&](unsigned range) -> ValueId {
        auto word = b.constant(shape.route_bits,
                               row.route | (std::uint64_t{range} << observer.range_shift));
        if (!row.keeps_id)
          return word;
        const auto &ids = plan.route_index.target_ids;
        for (const auto &field : ids.fields(true))
          if (field.bits && (field.name == "sequence" || field.name == "direction")) {
            auto value = fit(field.name == "sequence" ? of_request("id") : write_head, field.bits);
            if (field.lsb)
              value = b.concat(value, b.constant(field.lsb, 0));
            word = b.binary(Op::BitOr, word, fit(value, shape.route_bits));
          }
        return word;
      };
      const auto term = [&](ValueId when, unsigned range) {
        route_now = b.binary(
            Op::BitOr, route_now,
            b.select(c.and_(from, when), low_word(range), b.constant(shape.route_bits, 0)));
      };
      for (const auto &block : row.blocks) {
        const auto bits = static_cast<unsigned>(std::countr_zero(block.size));
        const auto whole = of_request("address");
        const auto top = b.ir.values.at(whole.index).width;
        const auto hit = bits >= top ? b.constant(1, 1)
                                     : b.binary(Op::Equal, b.slice(whole, bits, top - bits),
                                                b.constant(top - bits, block.base >> bits));
        term(c.and_(c.not_(miss), hit), block.range);
      }
      term(miss, row.miss_range);
      kept = b.binary(Op::BitOr, kept,
                      b.select(from, b.constant(shape.address_bits, row.miss_mask & all_ones),
                               b.constant(shape.address_bits, 0)));
    }
    const auto whole_low = observer.keep_fields ? all_ones : (std::uint64_t{1} << low_bits) - 1;
    const auto hit_record = b.binary(Op::BitAnd, start, b.constant(shape.address_bits, whole_low));
    const auto kept_address = b.select(miss, b.binary(Op::BitAnd, start, kept), hit_record);
    // The fields a request head fixes for its response's record.
    std::map<std::string, ValueId> fields{{"route", route_now},
                                          {"address", kept_address},
                                          {"write", write_head},
                                          {"wrap", c.eq(burst_kind, 2)},
                                          {"miss", miss}};
    // The source fixed a burst's length and a miss's lock; an APB transfer's length is its word's.
    if (!observer.logger.fixed_length) {
      const auto fixed_there = of_request("record");
      fields["length"] = b.slice(fixed_there, 0, shape.length_bits);
      fields["lock"] = b.slice(fixed_there, shape.length_bits, 1);
    }
    if (shape.user_bits) {
      const auto ordered = of_request("user");
      // Without kept fields a target's error records no flags; a miss's still does.
      fields["user"] =
          observer.keep_fields ? ordered : b.select(miss, ordered, b.constant(shape.user_bits, 0));
    }
    if (shape.security_bits)
      fields["security"] = of_request("security");
    const bool one = plan.targets[seen.target].outstanding == 1;
    const auto write_moved = c.transfer(t.wrsp), read_moved = c.transfer(t.rrsp);
    const auto response = [&](const char *name) {
      const auto at = c.endpoint_field_index(true, name);
      return one ? b.select(write_moved, t.wrsp.data[at], t.rrsp.data[at]) : rejoined.data[at];
    };
    const auto moved = one ? c.or_(write_moved, read_moved) : c.transfer(rejoined);
    const auto is_first = response("first"), is_last = response("last");
    // A read word that came after two cycles or more without one at the target's port heads a
    // packet of its own, when it is not its burst's first.
    auto cut = b.constant(1, 0);
    if (plan.targets[seen.target].protocol == Protocol::Axi4) {
      const auto port_valid = c.wire(1), port_ready = c.wire(1);
      observed_reads.emplace(seen.target, std::pair{port_valid, port_ready});
      const auto quiet = b.state(2, c.clock, c.reset), marked = b.state(1, c.clock, c.reset);
      c.bind(id + "quiet", quiet);
      c.bind(id + "marked", marked);
      const auto idle_long = c.eq(quiet, 2);
      b.update(quiet, b.select(port_valid, b.constant(2, 0),
                               b.select(idle_long, quiet,
                                        b.binary(Op::Subtract, quiet, b.constant(2, 3)))));
      const auto taken = c.and_(port_valid, port_ready);
      const auto arrived = c.and_(taken, idle_long);
      b.update(marked, b.select(read_moved, b.constant(1, 0), c.or_(marked, arrived)));
      cut = c.and_(read_moved, c.and_(c.not_(is_first), c.or_(marked, arrived)));
    }
    std::map<std::string, ValueId> kept_fields;
    if (one) {
      for (const auto &[name, value] : fields)
        kept_fields[name] = latched(id + name, value, head);
    } else {
      const auto with_id = plan.burst_id_bits != 0;
      const auto key_of = [&](ValueId from_source, const auto &id_field) {
        return with_id ? b.concat(from_source, id_field()) : from_source;
      };
      const auto response_key = key_of(response("source"), [&] { return response("id"); });
      kept_fields = held_contexts(id, plan.targets[seen.target].outstanding, fields, head,
                                  key_of(source, [&] { return of_request("id"); }), response_key,
                                  c.and_(moved, is_last));
      // So does one after a word of another burst, a write response included.
      const auto last_key = latched(id + "last_key", response_key, moved);
      cut = c.or_(cut,
                  c.and_(read_moved, c.and_(c.not_(is_first),
                                            c.not_(b.binary(Op::Equal, last_key, response_key)))));
    }
    const auto result = response("result");
    const auto failed = c.or_(
        response("error"), b.unary(Op::BitNot, b.binary(Op::Equal, result, b.constant(2, 0)), 1));
    // A packet that a cut started records the low address bits of its first failed word. That
    // word heads the record in its turn.
    if (plan.targets[seen.target].protocol == Protocol::Axi4) {
      const auto cut_started = b.state(1, c.clock, c.reset);
      c.bind(id + "cut_started", cut_started);
      b.update(cut_started,
               b.select(read_moved,
                        c.and_(c.not_(is_last), c.or_(cut, c.and_(c.not_(is_first), cut_started))),
                        cut_started));
      cut = c.or_(cut, c.and_(c.and_(read_moved, failed), c.and_(cut_started, c.not_(is_first))));
    }
    // A local length descriptor is tagged separately from a native target DECERR.
    const auto decode_word = !plan.error_codes   ? b.constant(1, 0)
                             : decode_flag(plan) ? response("error")
                                                 : c.eq(result, 3);
    const auto first_moved = c.and_(moved, is_first);
    const auto decode_held = latched(id + "decode_packet", decode_word, first_moved);
    const auto decode_packet = b.select(is_first, decode_word, decode_held);
    std::map<std::string, ValueId> event{
        {"moved", moved},
        // A decode miss has one event, at its last word.
        {"head",
         c.or_(b.select(c.or_(kept_fields.at("miss"), decode_packet), is_last, is_first), cut)},
        {"cut", cut},
        {"tail", is_last},
        {"failed", failed},
    };
    for (const auto &[name, value] : kept_fields)
      event[name] = value;
    // Such a packet's record has only its word's low address bits, and says so.
    const auto word = b.binary(Op::BitAnd, b.slice(response("address"), 0, shape.address_bits),
                               b.constant(shape.address_bits, (std::uint64_t{1} << low_bits) - 1));
    event["address"] = b.select(cut, word, event.at("address"));
    const auto low_only = observer.keep_fields ? b.constant(1, 0) : c.not_(kept_fields.at("miss"));
    event["partial"] = c.or_(cut, low_only);
    if (shape.code_bits)
      event["code"] = b.select(c.or_(decode_word, c.eq(result, 1)), b.constant(3, CodeDecode),
                               b.constant(3, CodeTarget));
    return event;
  }
  // A target of several bursts holds a request's fields from its head until its response tail, a
  // response finding them as the oldest of its source and ID. Returns the found fields.
  std::map<std::string, ValueId> held_contexts(const std::string &id, unsigned contexts,
                                               const std::map<std::string, ValueId> &fields,
                                               ValueId head, ValueId request_key,
                                               ValueId response_key, ValueId tail) {
    const auto name = [&](unsigned k) { return id + "context" + std::to_string(k) + "."; };
    std::vector<ValueId> valid, stored_key, matches, selected, releases, allocates;
    std::map<std::pair<unsigned, unsigned>, ValueId> older;
    for (unsigned k = 0; k < contexts; ++k) {
      valid.push_back(b.state(1, c.clock, c.reset));
      stored_key.push_back(b.state(b.ir.values.at(request_key.index).width, c.clock, c.reset));
      c.bind(name(k) + "valid", valid[k]);
      c.bind(name(k) + "key", stored_key[k]);
      matches.push_back(c.and_(valid[k], b.binary(Op::Equal, stored_key[k], response_key)));
      for (unsigned j = k + 1; j < contexts; ++j) {
        older[{k, j}] = b.state(1, c.clock, c.reset);
        c.bind(name(k) + "older" + std::to_string(j), older[{k, j}]);
      }
    }
    for (unsigned k = 0; k < contexts; ++k) {
      auto pick = matches[k];
      for (unsigned j = 0; j < contexts; ++j)
        if (j != k) {
          const auto j_older = j < k ? older.at({j, k}) : c.not_(older.at({k, j}));
          pick = c.and_(pick, c.not_(c.and_(matches[j], j_older)));
        }
      selected.push_back(pick);
      releases.push_back(c.and_(pick, tail));
    }
    auto earlier_free = b.constant(1, 0);
    for (unsigned k = 0; k < contexts; ++k) {
      const auto free = c.or_(c.not_(valid[k]), releases[k]);
      allocates.push_back(c.and_(head, c.and_(free, c.not_(earlier_free))));
      earlier_free = c.or_(earlier_free, free);
    }
    for (unsigned k = 0; k < contexts; ++k) {
      b.update(valid[k], c.or_(allocates[k], c.and_(valid[k], c.not_(releases[k]))));
      b.update(stored_key[k], b.select(allocates[k], request_key, stored_key[k]));
      for (unsigned j = k + 1; j < contexts; ++j) {
        const auto bit = older.at({k, j});
        b.update(bit, b.select(allocates[j], b.constant(1, 1),
                               b.select(allocates[k], b.constant(1, 0), bit)));
      }
    }
    std::map<std::string, ValueId> found;
    for (const auto &[field, value] : fields) {
      const auto width = b.ir.values.at(value.index).width;
      auto joined = b.constant(width, 0);
      for (unsigned k = 0; k < contexts; ++k) {
        const auto stored = b.state(width, c.clock, c.reset);
        b.update(stored, b.select(allocates[k], value, stored));
        c.bind(name(k) + field, stored);
        joined = b.binary(Op::BitOr, joined, b.select(selected[k], stored, b.constant(width, 0)));
      }
      found[field] = joined;
    }
    return found;
  }
  ValueId latched(const std::string &name, ValueId value, ValueId enable) {
    const auto state = b.state(b.ir.values.at(value.index).width, c.clock, c.reset);
    b.update(state, b.select(enable, value, state));
    c.bind(name, state);
    return state;
  }
  void attach_fault_monitors() {
    for (const auto &observer : plan.fault_monitors) {
      const auto &name = observer.logger.name;
      // A face's name ends in its logger's place in the chain: none for the first.
      const auto scope = [&](const std::string &endpoint, std::size_t stem) {
        const auto place = endpoint.substr(stem);
        return bank_scope(observer.bank +
                          (place.empty() ? 0U : static_cast<unsigned>(std::stoul(place))));
      };
      // A logger apart from its targets: each target's sender and its crossing.
      std::vector<Channel> words;
      if (observer.logger.crossed)
        for (std::size_t k = 0; k < observer.observed.size(); ++k)
          words.push_back(import_sender(observer, static_cast<unsigned>(k)));
      const auto kept = std::pair{c.clock, c.reset};
      if (observer.logger.crossed)
        std::tie(c.clock, c.reset) = c.domains.at(plan.observer_domain(name));
      Imported imported;
      c.import_component(
          admitted(lower_error_logger(observer.logger)), "observe/" + name + "/", imported,
          [&](const Port &port) -> ValueId {
            if (port.endpoint.starts_with("control"))
              return logger_controls.at(scope(port.endpoint, 7) + port.signal);
            if (port.endpoint.starts_with("word")) {
              const auto &word = words.at(std::stoul(port.endpoint.substr(4)));
              return port.signal == "valid" ? word.valid : word.data[port.signal == "last" ? 1 : 0];
            }
            return observed_events.at(name + "/" + port.endpoint.substr(5)).at(port.signal);
          },
          [&](const Port &port, ValueId value) {
            if (port.endpoint.starts_with("word")) {
              c.drive(words.at(std::stoul(port.endpoint.substr(4))).ready, value);
            } else if (port.endpoint.starts_with("alarm")) {
              const auto suffix = port.endpoint.substr(5);
              b.output("nl_o_" + name + "_alarm" + suffix, value, "error_log:" + name,
                       "alarm" + suffix);
            } else if (const auto at = logger_fields.find(scope(port.endpoint, 5) + port.signal);
                       at != logger_fields.end()) {
              c.drive(at->second, value);
            }
          });
      std::tie(c.clock, c.reset) = kept;
    }
  }
  // Target k's sender of a crossed error logger, in the target's domain, and the crossing's output.
  Channel import_sender(const FaultMonitorPlan &observer, unsigned k) {
    const auto &name = observer.logger.name;
    const auto &target = plan.targets[observer.observed[k].target];
    const auto kept = std::pair{c.clock, c.reset};
    c.select_object("target/" + target.name);
    Channel word{ValueId{}, c.wire(1), {}};
    Imported sender;
    c.import_component(
        admitted(lower_error_sender(observer.logger, k)),
        "observe/" + name + "/sender" + std::to_string(k) + "/", sender,
        [&](const Port &port) -> ValueId {
          if (port.endpoint == "word")
            return word.ready;
          return observed_events.at(name + "/" + std::to_string(k)).at(port.signal);
        },
        [&](const Port &port, ValueId value) {
          if (port.signal == "valid")
            word.valid = value;
          else
            word.data.push_back(value);
        });
    std::tie(c.clock, c.reset) = kept;
    return c.cross_async(serial_cut("service/observe/" + name + "/" + target.name),
                         {error_record_word_bits(observer.logger), 1}, word, {});
  }

  // A shared-bus target: the APB, AXI4-Lite or AHB outlet engine.
  void attach_target_bus(unsigned o, TargetSide &t) {
    const auto &target = plan.targets[o].name;
    const bool apb = apb_target(plan, target), lite = lite_target(plan, target);
    const auto profile = outlet_profile(plan, o);
    auto lowered = apb    ? lower_apb_burst_outlet(profile)
                   : lite ? lower_axi_lite_outlet(profile)
                          : lower_ahb_outlet(profile);
    const auto ep = import_outlet(o, t, admitted(std::move(lowered)), {});
    // The arrival count holds the packets of one direction between
    // acquisition and release: one in an APB engine, up to the configured
    // packet capacity in an AXI4-Lite engine; in an AHB engine one per
    // write context, or one per read response entry plus the packet still
    // issuing.
    if (ahb_target(plan, target)) {
      auto entries = [&](const std::string &stem, const std::string &field) {
        unsigned n = 0;
        while (ep.states.contains(stem + decimal(n) + "." + field))
          ++n;
        return n;
      };
      const auto writes = entries("w.context", "valid"), reads = entries("r.metadata", "first");
      if (!writes || !reads || std::max(writes, reads + 1) > 7)
        throw std::logic_error("The arrival count of " + target + " cannot hold its packets");
    }
    register_bus_storage(o, t, profile, ep);
  }

  // The APB engine's storage words of one direction.
  Names apb_names(const BurstOutletProfile &profile, const Imported &ep, bool read) const {
    const bool monitored = profile.exclusive_target.mode == ExclusiveAccess::Network;
    const std::string dir = read ? "r" : "w";
    Names words;
    auto &[fields, controls] = words;
    const auto form = apb_outlet_form(profile);
    // Descriptor words, the stem's optional ID and source after its listed fields.
    auto described = [&](const std::string &stem, std::initializer_list<const char *> listed) {
      std::ranges::transform(listed, std::back_inserter(fields),
                             [&](const char *field) { return stem + field; });
      add_id_source(fields, stem, plan.burst_id_bits, plan.source_bits);
    };
    // Only what a serial outlet does not find on its faces or in its response entry.
    if (read && form.single) {
      fields.insert(fields.end(), {"r.descriptor.length", "r.descriptor.burst"});
      if (!form.packet_coded)
        fields.insert(fields.end(), {"r.descriptor.cache", "r.descriptor.protection"});
    } else if (read || !form.write_in_entry)
      described(dir + ".descriptor.",
                {"address", "length", "size", "burst", "cache", "protection"});
    if (!profile.user.empty() && profile.user_bits)
      fields.push_back(dir + ".request_user");
    fields.push_back(dir + ".data");
    if (profile.data_user_bits_per_byte && read) {
      fields.insert(fields.end(), {"r.data_user", "r.tail_data_user"});
      if (profile.data_user_port_bits && profile.apb_data_bits < 32)
        fields.push_back("r.gather_user");
    } else if (profile.data_user_port_bits)
      fields.push_back("w.data_user");
    if (!form.at_once)
      fields.insert(fields.end(), {dir + ".address", dir + ".mask"});
    else if (read)
      fields.push_back("r.address");
    if (!read || !form.packet_coded)
      fields.push_back(dir + ".result");
    // The offered read response apart from the current word, and its second slot; both carry the
    // packet's descriptor fields since the next packet may be accepted while the response still
    // waits. Writes keep their two response entries likewise.
    if (read) {
      fields.push_back("r.initial_result");
      described("r.response_", {"size"});
      described("r.tail_meta_", {"size"});
      fields.insert(fields.end(), {"r.response_address", "r.response_result", "r.tail_address",
                                   "r.tail_data", "r.tail_result"});
      if (profile.apb_data_bits < 32)
        fields.push_back("r.gather");
      controls = {"r.busy",  "r.full",           "r.remaining",
                  "r.first", "r.response_first", "r.response_last"};
      if (profile.response_run_bits)
        controls.insert(controls.end(), {"r.response_run_end", "r.tail_meta_run_end"});
      if (plan.burst_tag_bits)
        controls.insert(controls.end(), {"r.response_tag", "r.tail_meta_tag"});
      if (monitored)
        controls.insert(controls.end(), {"r.response_exclusive", "r.tail_meta_exclusive"});
      if (ep.states.contains("r.error_request"))
        controls.push_back("r.error_request");
      controls.insert(controls.end(), {"r.tail_full", "r.tail_first", "r.tail_last"});
    } else {
      for (const auto *stem : {"w.response.", "w.response_tail."}) {
        if (form.write_in_entry)
          described(stem, {"address", "size"});
        else
          described(stem, {"address", "result", "size"});
        if (monitored)
          fields.insert(fields.end(), {stem + std::string("length"), stem + std::string("burst")});
      }
      controls = {"w.busy", "w.response_full", "w.last", "w.response_tail_full"};
      if (!form.at_once)
        controls.push_back("w.full");
      if (!form.single)
        controls.push_back("w.flight");
      if (plan.burst_tag_bits)
        controls.insert(controls.end(), {"w.response.tag", "w.response_tail.tag"});
      if (monitored)
        controls.insert(controls.end(), {"w.response.fail", "w.response.exclusive_okay",
                                         "w.response_tail.fail", "w.response_tail.exclusive_okay"});
    }
    if (profile.dialect == Protocol::Apb5 && profile.response_user_bits &&
        profile.response_user_port_bits) {
      if (read) {
        fields.insert(fields.end(), {"r.packet_user", "r.response_user", "r.tail_meta_user"});
        controls.push_back("r.user_seen");
      } else {
        fields.insert(fields.end(), {"w.packet_user", "w.response.user", "w.response_tail.user"});
        controls.push_back("w.user_seen");
      }
    }
    // A serial outlet without a timer keeps one response entry per direction.
    if (!profile.apb_back_to_back && !profile.timeout_cycles)
      for (auto *list : {&fields, &controls})
        std::erase_if(*list, [](const std::string &n) {
          return n.starts_with("r.tail") || n.starts_with("w.response_tail");
        });
    // The response steering tag of the packet in service.
    if (plan.burst_tag_bits && !(read ? form.single : form.write_in_entry))
      controls.push_back(dir + ".descriptor.tag");
    // The packet's exclusive flags (network monitor).
    if (monitored && read)
      controls.push_back("r.exclusive");
    else if (monitored)
      controls.insert(controls.end(), {"w.exclusive", "w.exclusive_fail"});
    return words;
  }

  // The pipelined AXI4-Lite engine's storage words of one direction: per-direction packet tables,
  // the write packets' completion tallies, one-entry response stages.
  Names lite_names(const BurstOutletProfile &profile, bool read) const {
    const bool monitored = profile.exclusive_target.mode == ExclusiveAccess::Network;
    const std::string dir = read ? "r" : "w";
    Names words;
    auto &[fields, controls] = words;
    const unsigned capacity = profile.max_outstanding;
    // The control states of a one-tag packet_context table.
    const auto table_controls = [&](const std::string &stem, unsigned n) {
      controls.push_back(stem + ".t0.count");
      if (n > 1)
        controls.push_back(stem + ".t0.head");
    };
    // A write packet keeps its length and burst for the exclusive monitor only.
    std::vector<std::string> pkt_fields{"address", "size"};
    if (read || monitored)
      pkt_fields.insert(pkt_fields.end(), {"length", "burst"});
    if (read)
      pkt_fields.insert(pkt_fields.end(), {"cache", "protection", "error"});
    if (read && !profile.user.empty())
      pkt_fields.emplace_back("user");
    add_id_source(pkt_fields, "", plan.burst_id_bits, plan.source_bits);
    for (unsigned i = 0; i < capacity; ++i) {
      const auto prefix = dir + ".packet.t0.s" + std::to_string(i) + ".";
      std::ranges::transform(pkt_fields, std::back_inserter(fields),
                             [&](const auto &f) { return prefix + f; });
      if (plan.burst_tag_bits)
        controls.push_back(prefix + "tag");
      if (read && profile.response_run_bits)
        controls.push_back(prefix + "run_end");
      if (monitored) {
        controls.push_back(prefix + "exclusive");
        if (!read)
          controls.push_back(prefix + "fail");
      }
    }
    table_controls(dir + ".packet", capacity);
    if (!read) {
      // The tally's local answer is payload; its counts are control.
      for (unsigned i = 0; i < capacity; ++i) {
        const auto prefix = "w.tally.s" + std::to_string(i) + ".";
        fields.push_back(prefix + "code");
        controls.push_back(prefix + "reals");
        if (profile.error_codes)
          controls.push_back(prefix + "after");
      }
      fields.push_back("w.bus_error");
      if (plan.burst_response_user_bits && profile.response_user_port_bits)
        fields.push_back("w.user");
    }
    {
      const auto prefix = dir + ".rsp0.";
      for (const auto *f : {"address", "size", "result"})
        fields.push_back(prefix + f);
      if (read)
        fields.push_back(prefix + "data");
      if (plan.burst_response_user_bits && profile.response_user_port_bits) {
        fields.push_back(prefix + "response_user");
        if (read)
          fields.push_back("r.user_head");
      }
      add_id_source(fields, prefix, plan.burst_id_bits, plan.source_bits);
      if (monitored && !read)
        fields.insert(fields.end(), {prefix + "length", prefix + "burst"});
      if (plan.burst_tag_bits)
        controls.push_back(prefix + "tag");
      if (read)
        controls.insert(controls.end(), {prefix + "first", prefix + "last"});
      if (monitored)
        controls.push_back(prefix + (read ? "exclusive_okay" : "fail"));
      if (monitored && !read)
        controls.push_back(prefix + "exclusive_okay");
    }
    controls.push_back(dir + ".rsp.full");
    if (read) {
      fields.insert(fields.end(), {"r.issue_address", "r.address"});
      controls.insert(controls.end(), {"r.packet.issue", "r.packet.unissued", "r.issuing",
                                       "r.issue_left", "r.on", "r.left", "r.first", "r.ar_done"});
      if (profile.response_run_bits)
        controls.insert(controls.end(), {"r.rsp0.run_end", "r.run_end"});
    } else {
      controls.insert(controls.end(), {"w.open", "w.retired", "w.bus_done"});
      if (monitored)
        controls.push_back("w.fail_tail");
    }
    return words;
  }

  Names bus_names(unsigned o, const BurstOutletProfile &profile, const Imported &ep) const {
    const auto &target = plan.targets[o].name;
    const bool apb = apb_target(plan, target), lite = lite_target(plan, target);
    const bool ahb = ahb_target(plan, target);
    const auto protocol = plan.targets[o].protocol;
    const bool strobeless = protocol == Protocol::Apb3 || protocol == Protocol::Apb2;
    Names bus{{"bus.address"}, {"bus.phase", "bus.write"}};
    auto &[bus_fields, bus_controls] = bus;
    if (!apb && !lite)
      bus_fields.push_back("bus.size");
    if (!ahb)
      bus_fields.push_back("bus.mask");
    if (!ahb && (!apb || profile.apb_back_to_back || profile.timeout_cycles))
      bus_fields.push_back("bus.data");
    if (!strobeless)
      bus_fields.push_back("bus.protection");
    if (lite && !profile.user.empty())
      bus_fields.push_back("bus.user");
    if (apb && !profile.user.empty())
      bus_fields.push_back("bus.request_user");
    if (apb && profile.data_user_port_bits)
      bus_fields.push_back("bus.data_user");
    if (profile.flows > 1)
      bus_fields.push_back("bus.flow");
    if (lite)
      // The pipelined engine: the write word on AW/W and its handshake
      // tracking; reads run on their own channel state above.
      bus_controls = {"bus.open", "bus.aw_done", "bus.w_done"};
    if (ahb) {
      // The pipelined address and data phases replace the phase counter.
      bus_fields.push_back("bus.hwdata");
      if (profile.ahb5) {
        if (profile.hnonsec)
          bus_fields.push_back("bus.nonsecure");
        if (profile.hmaster_bits)
          bus_fields.push_back("bus.master");
      }
      bus_controls = {"bus.address_phase", "bus.data_phase",     "bus.data_write", "bus.write",
                      "bus.open",          "bus.open_write",     "bus.hburst",     "bus.htrans",
                      "bus.release",       "bus.address_packet", "bus.data_packet"};
      if (profile.hresp_width == 2)
        bus_controls.push_back("bus.abort_pending");
      if (profile.flows > 1)
        bus_controls.push_back("bus.response_select");
      bus_controls.push_back("bus.burst");
    }
    // The timer, the failed flag and the orphan read copy are bus control.
    for (const auto &[name, value] : ep.states)
      if (name.starts_with("timeout."))
        bus_controls.push_back(name);
    // The exclusive monitor's reservations observe the bus; they hold no
    // transaction.
    if (profile.exclusive_target.mode == ExclusiveAccess::Network)
      std::ranges::copy(
          ExclusiveMonitor::state_names(
              "exclusive", {profile.exclusive_target.monitors, profile.exclusive_target.granule,
                            plan.burst_address_bits, profile.exclusive_target.id_flags}),
          std::back_inserter(bus_controls));
    return bus;
  }

  // The shared-bus engine's packet records per direction and its bus record.
  void register_bus_storage(unsigned o, TargetSide &t, const BurstOutletProfile &profile,
                            const Imported &ep) {
    const auto &target = plan.targets[o].name;
    const bool apb = apb_target(plan, target), lite = lite_target(plan, target);
    const bool ahb = ahb_target(plan, target);
    std::set<std::string> assigned;
    for (bool read : {false, true}) {
      const std::string dir = read ? "r" : "w";
      Names words;
      if (apb)
        words = apb_names(profile, ep, read);
      else if (lite)
        words = lite_names(profile, read);
      else {
        // AHB packet contexts and reserved response metadata have their
        // own lifecycle; assign every word once by its explicit role.
        static const std::set<std::string> data_names{
            "address",   "length",    "size",          "burst",         "cache",  "protection",
            "id",        "source",    "data",          "mask",          "result", "next_address",
            "next_data", "next_mask", "issue_address", "initial_result"};
        for (const auto &[name, value] : ep.states)
          if (name.starts_with(dir + "."))
            ((data_names.contains(name.substr(name.find_last_of('.') + 1)) ||
              (profile.ahb5 && name.ends_with(".user")))
                 ? words.first
                 : words.second)
                .push_back(name);
      }
      // A denied direction holds each packet's first fragment one cycle.
      if ((apb || lite) && (read ? profile.write_only : profile.read_only))
        words.second.push_back(dir + ".denied_held");
      std::vector<ValueId> payload, control;
      consume_states(ep, words, assigned, payload, control);
      auto release = c.transfer(read ? t.rrsp : t.wrsp);
      if (read)
        release = c.and_(release, apb    ? ep.states.at("r.response_last")
                                  : lite ? t.rrsp.data[c.endpoint_field_index(true, "last")]
                                         : ep.bound.at("r.last"));
      const auto &request = read ? t.rreq : t.wreq;
      record(t, read,
             ahb ? ep.bound.at(read ? "r.accept" : "w.head")
             : lite
                 ? c.and_(c.transfer(request), request.data[c.endpoint_field_index(false, "first")])
                 : c.and_(c.transfer(request), c.not_(ep.states.at(dir + ".busy"))),
             release, payload, control);
    }
    std::vector<ValueId> payload, control;
    const auto bus = bus_names(o, profile, ep);
    consume_states(ep, bus, assigned, payload, control);
    control.insert(control.end(), {t.count, t.turn});
    if (t.flow)
      control.push_back(*t.flow);
    if (t.miss_seen)
      control.insert(control.end(), {*t.miss_seen, *t.miss_answer, *t.miss_replied});
    c.bind(t.key + "/bus.acquire", ep.bound.at("bus.issue"));
    c.bind(t.key + "/bus.release", ep.bound.at("bus.complete"));
    c.out.storage.push_back({t.key + "/bus", c.clock, payload, control, ep.bound.at("bus.issue"),
                             ep.bound.at("bus.complete")});
    for (const auto &[name, value] : ep.states)
      if (!assigned.contains(name))
        throw std::logic_error("Unowned dedicated outlet state " + name + " at " + t.key);
  }

  // An AXI4 target: the burst outlet and its packet records.
  void attach_target_axi4(unsigned o, TargetSide &t) {
    const auto profile = axi4_outlet_profile(plan, o);
    const auto outlet = admitted(lower_axi4_outlet(profile));
    std::map<std::string, unsigned> target_ports;
    remove_one_way_ports(outlet, target_ports, profile.read_only, profile.write_only);
    const auto ep = import_outlet(o, t, outlet, target_ports);
    if (const auto seen = observed_reads.find(o); seen != observed_reads.end()) {
      c.drive(seen->second.first, ep.axi.at("rvalid"));
      c.drive(seen->second.second, ep.axi.at("rready"));
    }
    if (t.admit_ok) {
      c.drive((*t.admit_ok)[0], ep.bound.at("pool.write_ok"));
      c.drive((*t.admit_ok)[1], ep.bound.at("pool.read_ok"));
    }
    if (t.empty)
      c.drive(*t.empty, ep.bound.at("pool.empty"));
    // The timer state of each direction (frozen address channel, held valids, count), the failed
    // flag (write side unless read-only) and the network monitor's reservations, which observe the
    // write channel, hold no transaction.
    std::set<std::string> assigned;
    for (bool read : {false, true}) {
      const std::string dir = read ? "r" : "w";
      Names words;
      for (const auto &[name, q] : ep.states) {
        (void)q;
        if (name.starts_with("timeout.") || name.starts_with("exclusive.")) {
          if ((name.starts_with("timeout.ar") || (name == "timeout.failed" && profile.read_only)) ==
              read)
            words.second.push_back(name);
        } else if (name.starts_with(dir + ".") || (!read && name.starts_with("pool."))) {
          // The shared in-flight pool states ride with the write engine.
          (axi4_outlet_control(name) ? words.second : words.first).push_back(name);
        }
      }
      std::vector<ValueId> payload, control;
      consume_states(ep, words, assigned, payload, control);
      // The read queue takes every accepted request; the write issue
      // stage every packet head (w.head).
      record(t, read, read ? c.transfer(t.rreq) : ep.bound.at("w.head"),
             read ? c.and_(c.transfer(t.rrsp), ep.states.contains("r.response_last")
                                                   ? ep.states.at("r.response_last")
                                                   : ep.bound.at("r.response_last"))
                  : c.transfer(t.wrsp),
             payload, control);
    }
    if (assigned.size() != ep.states.size())
      throw std::logic_error("Unowned AXI outlet state word at " + t.key);
  }

  // The response transport's exits split by direction into each source's response faces.
  void return_responses(const Channels &returns) {
    for (unsigned si = 0; si < plan.sources.size(); ++si) {
      const auto &source = plan.sources[si].name;
      const auto &side = sides[si];
      const auto word = word_at(source);
      // The source flow names the direction (2i + 1 reads): the head packet's
      // side alone decides readiness, so an R the source does not take holds a B.
      const auto &arriving = returns.at(source);
      c.consume(c.region("source/" + source), "source/" + source, arriving.valid);
      const auto faces = c.steer(
          arriving, c.eq(arriving.data[c.burst_field_index(true, word, "source")], 2 * si + 1),
          {c.bit(1), c.bit(1)}, 0, true, word);
      c.deframe_burst_into(true, faces[0], side.wrsp_ep, word);
      c.deframe_burst_into(true, faces[1], side.rrsp_ep, word);
      // Formal-observation anchors (emitted into the map): the response packet as it reaches
      // this source's engines.
      for (const auto *dir : {"write", "read"}) {
        const auto &ep = *dir == 'w' ? side.wrsp_ep : side.rrsp_ep;
        const auto at = "source/" + source + "/" + dir;
        c.bind(at + ".return_valid", ep.valid);
        c.bind(at + ".return_result", ep.data[c.endpoint_field_index(true, "result")]);
        c.bind(at + ".return_error", ep.data[c.endpoint_field_index(true, "error")]);
      }
      // A response packet tail at the source answers its burst's unit of the pool.
      if (side.pool)
        c.pool_answers(*side.pool, arriving, word);
    }
  }
};
} // namespace

Result<ExchangeHardware> lower_axi4_exchange_composition(const NetworkPlan &plan,
                                                         RegisterReset register_reset) {
  try {
    return Lowering(plan, register_reset).run();
  } catch (const std::logic_error &e) {
    return Result<ExchangeHardware>::failure(
        error("NCL_EXCHANGE_COMPOSE", "exchange.hardware", e.what()));
  }
}

} // namespace nocloom::detail
