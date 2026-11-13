#include "async_fifo.hpp"
#include "capability.hpp"
#include "cone_walk.hpp"
#include "diagnostic.hpp"
#include "error_logger.hpp"
#include "exchange_composition.hpp"
#include "fifo_levels.hpp"
#include "network_plan_profiles.hpp"
#include "related_clock.hpp"
#include "width_adapter.hpp"
#include <algorithm>
#include <bit>
#include <limits>
#include <set>
#include <stdexcept>
#include <tuple>

namespace nocloom::detail {
namespace {
// Replaces the same-domain terminals of the obligations by one release per domain and
// expands each admitted route through the actual clock adapters. Each adapter and receive register
// waits for real downstream capacity; only the final reserved return write carries the endpoint
// reservation exception.
void route_through_crossings(const NetworkPlan &p, CrossGraph &graph) {
  const auto same_domain = "domain/" + p.domain.name;
  std::erase_if(graph.resources, [&](const auto &r) {
    return r.id == same_domain + "/init" || r.id == same_domain + "/clock";
  });
  std::erase_if(graph.waits, [&](const auto &w) { return w.from == same_domain + "/init"; });
  for (auto &r : graph.resources)
    r.domain = p.resource_domains.at(r.id);
  for (auto &w : graph.waits)
    if (w.kind == CrossWaitKind::Startup && w.to == same_domain + "/init")
      w.to = "domain/" + p.resource_domains.at(w.from) + "/init";
  auto resource = [&](const std::string &id, const std::string &domain, const std::string &role,
                      unsigned bits, unsigned capacity = 1) {
    graph.resources.push_back({id, id, domain, role, "local clocked transport",
                               "qualified local transfer", bits, capacity});
  };
  for (const auto &domain : p.domains) {
    const auto id = "domain/" + domain.name;
    resource(id + "/clock", domain.name, "environment", 0);
    resource(id + "/init", domain.name, "release_chain", 0);
    if (domain.type == ClockType::InternalDivider || domain.type == ClockType::EdgeQualified)
      resource(id + "/divider", domain.name, "clock_divider", 0);
    graph.waits.push_back({id + "/init", id + "/clock", "qualified local reset-release edges",
                           CrossWaitKind::Environment});
  }
  std::set<std::string> cuts;
  for (const auto &cut : p.cuts) {
    cuts.insert(cut.name);
    const auto *async = std::get_if<AsyncClockCut>(&cut.options);
    const auto *sync = std::get_if<SyncClockCut>(&cut.options);
    const auto *edge_aligned = std::get_if<EdgeAlignedCut>(&cut.options);
    const auto role = async ? "crossing_fifo" : sync ? "crossing_sync" : "crossing_edge_aligned";
    const auto capacity = async  ? async->depth.depth
                          : sync ? sync->depth.depth
                          : std::get<EdgeAlignedCut>(cut.options).slow_from_sender &&
                                  std::get<EdgeAlignedCut>(cut.options).slow_pipeline
                              ? 2U
                              : 0U;
    const auto receive_capacity = async  ? 1U
                                  : sync ? 0U
                                  : !std::get<EdgeAlignedCut>(cut.options).slow_from_sender &&
                                          std::get<EdgeAlignedCut>(cut.options).slow_pipeline
                                      ? 2U
                                      : 0U;
    resource(cut.name, cut.from_domain, role, cut.payload_bits, capacity);
    resource(cut.name + "/receive", cut.to_domain, "crossing_receive", cut.payload_bits,
             receive_capacity);
    graph.waits.push_back({cut.name, cut.name + "/receive",
                           edge_aligned
                               ? "the slow-side adapter releases the held word"
                               : "receive register loads the head entry; the freed slot becomes "
                                 "visible through the read count",
                           CrossWaitKind::Completion});
    graph.waits.push_back({cut.name, "domain/" + cut.from_domain + "/init",
                           "local release before write", CrossWaitKind::Startup});
    graph.waits.push_back({cut.name + "/receive", "domain/" + cut.to_domain + "/init",
                           "local release before read", CrossWaitKind::Startup});
    if (async && async->reset_handshake) {
      graph.waits.push_back({cut.name, "domain/" + cut.to_domain + "/init",
                             "reset handshake before write", CrossWaitKind::Startup});
      graph.waits.push_back({cut.name + "/receive", "domain/" + cut.from_domain + "/init",
                             "reset handshake before read", CrossWaitKind::Startup});
    }
  }
  for (bool response : {false, true}) {
    const auto &routes = response ? p.exchange.response.routes : p.exchange.request.routes;
    const std::string prefix = response ? "rsp/" : "req/";
    for (const auto &route : routes)
      for (const std::string operation : {"write", "read"}) {
        auto previous = response ? "burst_service/" + route.entry + "/" + operation
                                 : flow_id(route.entry, operation);
        std::vector<std::string> nodes{route.entry};
        nodes.insert(nodes.end(), route.nodes.begin(), route.nodes.end());
        nodes.push_back(route.exit);
        for (std::size_t i = 1; i < nodes.size(); ++i) {
          const auto cut = "cut/" + prefix + nodes[i - 1] + ">" + nodes[i];
          if (!cuts.contains(cut))
            continue;
          graph.waits.push_back({previous, cut, "admitted route requires a free FIFO entry",
                                 CrossWaitKind::Capacity});
          previous = cut + "/receive";
        }
        const auto destination = response ? flow_id(route.exit, operation) + "/return"
                                          : "burst_service/" + route.exit + "/" + operation;
        if (previous.ends_with("/receive"))
          graph.waits.push_back(
              {previous, destination, "addressed endpoint accepts the fragment",
               response ? CrossWaitKind::ReservedWrite : CrossWaitKind::Capacity});
      }
  }
  std::ranges::sort(graph.waits, [](const auto &a, const auto &b) {
    return std::tie(a.from, a.to, a.kind, a.condition) <
           std::tie(b.from, b.to, b.kind, b.condition);
  });
  graph.waits.erase(std::unique(graph.waits.begin(), graph.waits.end()), graph.waits.end());
  std::ranges::sort(graph.resources, {}, &CrossResource::id);
}
// The crossings of a service target's serial path and of its error loggers' records.
void plan_serial_cuts(NetworkPlan &p, const std::map<std::string, DomainPlan> &domains) {
  const auto owner = [&](const std::string &object) -> const std::string & {
    return p.object_domains.at(object);
  };
  const auto serial_cut = [&](const std::string &name, const std::string &sender,
                              const std::string &receiver,
                              std::vector<std::pair<std::string, unsigned>> fields) {
    ClockCut cut{"cut/" + name, name, sender, receiver, std::move(fields)};
    for (const auto &[field, width] : cut.fields)
      cut.payload_bits += width;
    AsyncClockCut options;
    options.synchronizer_stages = p.stages(sender, receiver);
    options.reset_handshake = false;
    const auto &a = domains.at(sender), &z = domains.at(receiver);
    auto depth = default_crossing_depth(options.synchronizer_stages, a.period_ps, z.period_ps);
    require(static_cast<bool>(depth), "The definition checks bound every crossing period");
    options.depth = depth.value();
    options.count_bits = async_fifo_count_bits(options.depth.depth);
    options.index_bits = async_fifo_index_bits(options.depth.depth);
    p.fifo_payload_bits += cut.payload_bits * (options.depth.depth + 1);
    p.fifo_control_bits += 2 * options.index_bits + 2 * options.count_bits + 1 +
                           options.count_bits * 2 * options.synchronizer_stages;
    cut.options = options;
    p.cuts.push_back(std::move(cut));
  };
  for (const auto &t : p.targets) {
    if (!t.service || t.service->domain.empty() || t.service->domain == owner("target/" + t.name))
      continue;
    for (unsigned branch = 0; branch <= t.service->banks.back().branch; ++branch) {
      const auto stem = "service/" + t.name + service_branch_suffix(*t.service, branch);
      serial_cut(stem + "/request", owner("target/" + t.name), t.service->domain,
                 {{"data", 10}, {"head", 1}, {"tail", 1}});
      serial_cut(stem + "/response", t.service->domain, owner("target/" + t.name),
                 {{"data", 10}, {"tail", 1}});
    }
  }
  for (const auto &observer : p.fault_monitors)
    if (observer.logger.crossed)
      for (const auto &observed : observer.observed) {
        const auto &name = p.targets[observed.target].name;
        serial_cut("service/observe/" + observer.logger.name + "/" + name, owner("target/" + name),
                   p.targets[observer.service].service->domain,
                   {{"word", error_record_word_bits(observer.logger)}, {"last", 1}});
      }
}
} // namespace

// The definition checks bound every crossing parameter and every domain a placement names; the
// capability rules judge unused parameters, partial periods and unowned domains.
void plan_clocking(NetworkPlan &p) {
  const auto &assignment = *p.placement;
  std::map<std::string, DomainPlan> domains;
  for (const auto &domain : assignment.domains)
    domains.emplace(domain.name, domain);
  p.synchronizer_stages = assignment.synchronizer_stages.value_or(async_fifo_default_stages);
  std::set<std::string> reset_ports;
  for (const auto &domain : assignment.domains)
    reset_ports.insert(domain.reset_port);
  p.reset_handshake = reset_ports.size() > 1;
  auto &explicit_depths = p.explicit_depths;
  for (const auto &c : assignment.crossings) {
    if (const auto *a = std::get_if<AsyncClockCrossing>(&c.options)) {
      if (a->depth)
        explicit_depths.emplace(std::pair{c.from_domain, c.to_domain}, *a->depth);
      if (a->synchronizer_stages)
        p.explicit_stages.emplace(std::pair{c.from_domain, c.to_domain}, *a->synchronizer_stages);
      if (a->reset_handshake)
        p.explicit_handshakes.emplace(std::pair{c.from_domain, c.to_domain}, *a->reset_handshake);
    } else if (const auto *s = std::get_if<SyncClockCrossing>(&c.options); s && s->depth) {
      explicit_depths.emplace(std::pair{c.from_domain, c.to_domain}, *s->depth);
    }
  }
  // Planning names every endpoint and switch once.
  require(assignment.objects.size() == p.sources.size() + p.targets.size() + p.nodes.size() &&
              std::ranges::all_of(assignment.objects,
                                  [&](const auto &o) { return domains.contains(o.second); }),
          "Every endpoint and directional switch takes one declared domain");
  p.object_domains = assignment.objects;
  for (auto &[name, domain] : domains) {
    std::uint64_t ratio = domain.ratio;
    auto parent = domain.edge_parent;
    while (!parent.empty()) {
      const auto &edge = domains.at(parent);
      require(ratio <= std::numeric_limits<std::uint64_t>::max() / edge.ratio,
              "Clock edge ratio exceeds the planning representation");
      ratio *= edge.ratio;
      parent = edge.edge_parent;
    }
    domain.edge_ratio = ratio;
    p.domains.push_back(domain);
  }
  auto owner = [&](const std::string &object) -> const std::string & {
    return p.object_domains.at(object);
  };
  // The crossing payload is the fragment layout of the composition, the
  // response steering tag included (several bursts in flight).
  const auto burst = transport_burst(p);
  // Every pipe and adapter stage in its domain; one typed clock adapter per crossing stage.
  for (const auto &link : p.links)
    for (const auto &stage : link.stages) {
      if (stage.kind != StageKind::Crossing) {
        p.resource_domains.emplace(stage.name, stage.domain);
        continue;
      }
      const auto &sender = link.from_domain, &receiver = link.to_domain;
      ClockCut cut{stage.name, link.name, sender, receiver,
                   fragment_fields(link.response, link.crossing_bytes(), p.source_bits, burst)};
      for (const auto &[field, width] : cut.fields)
        cut.payload_bits += width;
      const auto &a = domains.at(sender), &z = domains.at(receiver);
      const auto declaration = std::ranges::find_if(assignment.crossings, [&](const auto &c) {
        return c.from_domain == sender && c.to_domain == receiver;
      });
      if (declaration != assignment.crossings.end() &&
          !std::holds_alternative<AsyncClockCrossing>(declaration->options) &&
          p.level_hint_bits()) {
        cut.fields.emplace_back("pressure", p.level_hint_bits());
        cut.payload_bits += p.level_hint_bits();
      }
      const auto requested = explicit_depths.find({sender, receiver});
      const bool timed = a.period_ps && z.period_ps;
      if (declaration == assignment.crossings.end() || declaration->options.index() == 0) {
        AsyncClockCut options;
        options.synchronizer_stages = p.stages(sender, receiver);
        options.reset_handshake = p.handshake(sender, receiver);
        auto depth = resolve_crossing_depth(
            requested == explicit_depths.end() ? std::nullopt : std::optional{requested->second},
            options.synchronizer_stages, timed ? a.period_ps : std::nullopt,
            timed ? z.period_ps : std::nullopt);
        require(static_cast<bool>(depth), "The definition checks bound every crossing depth");
        options.depth = depth.value();
        options.count_bits = async_fifo_count_bits(options.depth.depth);
        options.index_bits = async_fifo_index_bits(options.depth.depth);
        p.fifo_payload_bits += cut.payload_bits * (options.depth.depth + 1);
        p.fifo_control_bits += 2 * options.index_bits + 2 * options.count_bits + 1 +
                               options.count_bits * 2 * options.synchronizer_stages;
        if (options.reset_handshake)
          p.fifo_control_bits +=
              2 * (2 + options.synchronizer_stages + 2 * options.synchronizer_stages);
        cut.options = options;
      } else if (const auto *sync = std::get_if<SyncClockCrossing>(&declaration->options)) {
        SyncClockCut options;
        options.fast = sync->fast;
        if (requested != explicit_depths.end()) {
          options.depth = {requested->second, CrossingDepthSource::Explicit};
        } else if (timed) {
          const auto slow = std::max(*a.period_ps, *z.period_ps);
          options.depth = {
              static_cast<unsigned>((2 * (*a.period_ps + *z.period_ps) + slow - 1) / slow),
              CrossingDepthSource::Formula};
        } else {
          options.depth = {3, CrossingDepthSource::Fallback};
        }
        options.count_bits = async_fifo_count_bits(options.depth.depth);
        options.index_bits = async_fifo_index_bits(options.depth.depth);
        p.fifo_payload_bits += cut.payload_bits * options.depth.depth;
        p.fifo_control_bits += 2 * options.index_bits + 2 * options.count_bits +
                               (options.fast ? 0 : 2 * options.count_bits);
        cut.options = options;
      } else {
        const auto &edge_aligned = std::get<EdgeAlignedCrossing>(declaration->options);
        const bool sender_static = a.type == ClockType::InternalDivider;
        const bool receiver_static = z.type == ClockType::InternalDivider;
        const bool slow_from_sender =
            a.edge_ratio > z.edge_ratio ||
            (a.edge_ratio == z.edge_ratio && sender_static && !receiver_static);
        cut.options = EdgeAlignedCut{edge_aligned.slow_pipeline, slow_from_sender};
        p.fifo_payload_bits += cut.payload_bits * (edge_aligned.slow_pipeline ? 2U : 0U);
        p.fifo_control_bits += 2U * edge_aligned.slow_pipeline +
                               (slow_from_sender || !edge_aligned.slow_pipeline ? 1U : 0U);
      }
      p.cuts.push_back(std::move(cut));
    }
  plan_serial_cuts(p, domains);
  for (bool response : {false, true})
    for (const auto &o : (response ? p.exchange.response : p.exchange.request).outputs)
      if (o.inputs.size() > 1)
        p.resource_domains.emplace(std::string(response ? "graph/rsp/" : "graph/req/") + o.node +
                                       ">" + (o.endpoint ? "endpoint/" : "node/") + o.output,
                                   owner((response ? "rsp/" : "req/") + o.node));
  for (const auto &source : names(p.sources))
    for (const auto *operation : {"write", "read"}) {
      const auto id = "source/" + source + "/" + operation;
      for (const auto *suffix : {"", "/return", "/consumer"})
        p.resource_domains.emplace(id + suffix, owner("source/" + source));
    }
  // A source's rate limiter or regulator, in its domain.
  for (const auto &s : p.sources)
    for (const auto &[on, unit] :
         {std::pair{s.qos.holds(), "/limiter"}, std::pair{s.qos.picks(), "/regulator"}})
      if (on)
        p.resource_domains.emplace("source/" + s.name + unit, owner("source/" + s.name));
  for (const auto &target : names(p.targets)) {
    const auto id = "burst_service/" + target;
    for (const auto *suffix : {"/write", "/read", "/target"})
      p.resource_domains.emplace(id + suffix, owner("target/" + target));
    if (dedicated_protocol(p.target(target).protocol))
      p.resource_domains.emplace(id + "/bus", owner("target/" + target));
  }
  // Each endpoint pipe (<endpoint>/pipe/<place>) in its endpoint's domain.
  auto pipes_in = [&](const std::vector<EndpointPipe> &pipes) {
    for (const auto &pipe : pipes)
      p.resource_domains.emplace(pipe.name, owner(endpoint_owner(pipe.name)));
  };
  for (const auto &s : p.sources)
    pipes_in(s.pipes);
  for (const auto &t : p.targets)
    pipes_in(t.pipes);
  // Each domain has one release resource. All other physical resources
  // require a local clock-domain owner.
  const auto placeholder = "domain/" + p.domain.name;
  std::set<std::string> physical_resources;
  for (const auto &resource : p.graph.resources)
    if (resource.id != placeholder + "/init" && resource.id != placeholder + "/clock")
      physical_resources.insert(resource.id);
  require(
      physical_resources.size() == p.resource_domains.size() &&
          std::ranges::all_of(p.resource_domains,
                              [&](const auto &r) { return physical_resources.contains(r.first); }),
      "Clock ownership covers exactly the composition resources");
  std::ranges::sort(p.cuts, {}, &ClockCut::name);
  route_through_crossings(p, p.graph);
}
CrossGraph clocked_obligations(const NetworkPlan &plan) {
  auto graph = build_axi4_exchange_obligations(plan);
  route_through_crossings(plan, graph);
  return graph;
}

namespace {
bool same(ValueId a, ValueId b) { return a.index == b.index; }
// The clock and crossing checks of composed hardware, in order: the domain clocks and resets,
// each clock adapter against a fresh lowering, the synchronizer attributes and the sampling domain
// of every register and output cone (verify_axi4_exchange_obligations checks the obligations). A
// violation throws std::logic_error with its message.
class ClockAudit {
public:
  explicit ClockAudit(const ExchangeHardware &hardware)
      : h(hardware), p(hardware.plan), ir(hardware.ir) {}
  void run() {
    domain_resets();
    for (const auto &cut : p.cuts)
      crossing(cut);
    std::set<std::size_t> annotated;
    // A synchronizer stage runs on its domain clock or its clock port (the reset
    // release chains), never on a region gate.
    for (auto id : ir.synchronizer_stages)
      if (!annotated.insert(id.index).second || !sync_clocks.contains(state(id).clock.index))
        throw std::logic_error("Repeated or region-gated synchronizer stage");
    if (annotated != expected_sync)
      throw std::logic_error("Synchronizer attributes do not match actual declared stages");
    cones();
  }

private:
  const ExchangeHardware &h;
  const NetworkPlan &p;
  const HardwareIR &ir;
  std::map<std::string, DomainPlan> domains;
  std::set<std::size_t> expected_sync, clocks, sync_clocks;
  // Each reset port and the clocks of the domains it resets directly: logic of those domains
  // may read it, as their state reads a release register.
  std::map<std::size_t, std::set<std::size_t>> resets;
  std::map<std::size_t, std::size_t> stage_successor;
  std::set<std::pair<std::size_t, std::size_t>> allowed_control_crossings, actual_control_crossings;
  // A verified confirmed-slot reduction is a receiver-domain boundary. Raw slot levels
  // remain forbidden in every other receiver cone.
  std::map<std::size_t, std::size_t> confirmed_levels;
  std::map<std::size_t, std::size_t> slot_crossings;
  std::map<std::size_t, ValueId> input_clocks;
  std::set<std::pair<std::size_t, std::size_t>> allowed_input_crossings;

  ValueId bound(const std::string &key) const {
    const auto id = h.bindings.at(key);
    if (id.index >= ir.values.size())
      throw std::logic_error("Undefined clock binding " + key);
    return id;
  }
  const State &state(ValueId id) const {
    const auto &v = ir.values.at(id.index);
    if (v.op != Op::State)
      throw std::logic_error("Clock component state binding is not a register");
    return ir.states.at(v.state.index);
  }
  // A gated state belongs to the domain of the clock its gate passes.
  ValueId domain_clock(ValueId id) const { return root_clock(ir, state(id).clock); }
  ValueId clock(const std::string &domain) const { return bound("domain/" + domain + ".clock"); }
  ValueId stage(const std::string &id, unsigned i) const {
    return bound(id + "/stage" + decimal(i));
  }
  // One synchronizer chain walk; the FIFO count chains start at the Gray count register itself.
  template <typename StageAt>
  void chain(const StageAt &stage_at, ValueId source, unsigned count, ValueId clk,
             const ValueId *reset, const std::string &id = {}) {
    for (unsigned i = 0; i < count; ++i) {
      const auto output = stage_at(i);
      if (reset) {
        const auto &q = state(output);
        if (!same(q.clock, clk) || !q.reset || !same(q.reset->signal, *reset) || q.reset->value ||
            ir.values[output.index].width != 1 || !same(q.next, source) ||
            !expected_sync.insert(output.index).second)
          throw std::logic_error("Incorrect reset/synchronizer chain " + id);
      } else if (!expected_sync.insert(output.index).second)
        throw std::logic_error("Count chains cannot share stages");
      if (i)
        stage_successor.emplace(source.index, output.index);
      if (ir.values[source.index].op == Op::State && !same(domain_clock(source), clk))
        allowed_control_crossings.emplace(source.index, output.index);
      source = output;
    }
  }
  template <typename Ids, typename Mapped>
  void chain(const Ids &ids, const Mapped &mapped, ValueId source, ValueId clk) {
    chain([&](unsigned i) { return mapped(ids[i]); }, source, ids.size(), root_clock(ir, clk),
          nullptr);
  }
  bool input_port(ValueId id, const std::string &name, const std::string &endpoint,
                  const std::string &signal) const {
    const auto &v = ir.values[id.index];
    return v.op == Op::Input && v.width == 1 && ir.ports[v.input_port.index].name == name &&
           ir.ports[v.input_port.index].endpoint == endpoint &&
           ir.ports[v.input_port.index].signal == signal;
  }
  bool domain_clock_port(ValueId id, const DomainPlan &domain) const {
    if (input_port(id, domain.clock_port, "domain:" + domain.name, "clock"))
      return true;
    if (domain.clock_root.empty() || ir.values[id.index].op != Op::Input)
      return false;
    const auto &port = ir.ports[ir.values[id.index].input_port.index];
    if (port.name != domain.clock_port || port.signal != "clock" ||
        !port.endpoint.starts_with("domain:"))
      return false;
    const auto owner = port.endpoint.substr(7);
    return std::ranges::any_of(p.domains, [&](const DomainPlan &candidate) {
      return candidate.name == owner && candidate.clock_root == domain.clock_root &&
             candidate.clock_port == domain.clock_port;
    });
  }
  // Domain edge selection: InternalDivider opens on zero count, EdgeQualified on enable_port.
  // The ungate input also opens the gate. The test input overrides it.
  // An absent test input is tied to zero.
  ValueId divided_clock(const DomainPlan &d, ValueId clk, ValueId input) {
    const auto &v = ir.values[clk.index];
    if (v.op != Op::ClockGate || !same(v.operands[0], input))
      throw std::logic_error("A divided domain clock is the gate of its clock port");
    const auto &t = ir.values[v.operands[2].index];
    if (!(input_port(v.operands[2], d.test_port, "test:" + d.test_port, "test") ||
          (d.test_port.empty() && t.op == Op::Constant && t.literal == 0)))
      throw std::logic_error("A divider gate takes the test port of its domain");
    auto en = v.operands[1];
    if (!d.ungate_port.empty()) {
      const auto &o = ir.values[en.index];
      if (o.op != Op::BitOr ||
          !input_port(o.operands[1], d.ungate_port, "ungate:" + d.ungate_port, "ungate"))
        throw std::logic_error("A divider gate takes the ungate port of its domain");
      en = o.operands[0];
    }
    if (ir.synchronous_reset) {
      const auto &o = ir.values[en.index];
      if (o.op != Op::BitOr || ir.values[o.operands[1].index].op != Op::BitNot ||
          !same(ir.values[o.operands[1].index].operands[0], bound("domain/" + d.name + ".reset")))
        throw std::logic_error("A divider gate opens while its domain is in synchronous reset");
      en = o.operands[0];
    }
    if (d.type == ClockType::EdgeQualified) {
      if (!input_port(en, d.enable_port, "enable:" + d.enable_port, "enable"))
        throw std::logic_error("An edge_qualified domain gates its clock by its enable port");
      return clk;
    }
    const auto &en_v = ir.values[en.index];
    if (en_v.op != Op::Equal)
      throw std::logic_error("An internal_divider domain gates its clock by its zero count");
    const auto count = en_v.operands[0];
    const auto &zero = ir.values[en_v.operands[1].index];
    if (zero.op != Op::Constant || zero.literal != 0 ||
        ir.values[count.index].width != std::max<unsigned>(1, std::bit_width(d.ratio - 1)))
      throw std::logic_error("A static divider compares its count with zero");
    const auto &q = state(count);
    if (!same(q.clock, input) || !q.reset ||
        !same(q.reset->signal, bound("domain/" + d.name + ".reset")) ||
        q.reset->value != d.ratio - 1)
      throw std::logic_error(
          "A static divider count runs on the clock port under the domain reset");
    const auto &nx = ir.values[q.next.index];
    const auto &reload = ir.values[nx.operands[1].index];
    const auto &dec = ir.values[nx.operands[2].index];
    if (nx.op != Op::Select || !same(nx.operands[0], en) || reload.op != Op::Constant ||
        reload.literal != d.ratio - 1 || dec.op != Op::Subtract || !same(dec.operands[0], count) ||
        ir.values[dec.operands[1].index].op != Op::Constant ||
        ir.values[dec.operands[1].index].literal != 1)
      throw std::logic_error("A static divider count reloads its ratio and decrements otherwise");
    return clk;
  }
  // Each domain resets from its reset port, directly or through its own release chain.
  void domain_resets() {
    for (const auto &d : p.domains) {
      const auto clk = clock(d.name), port = bound("domain/" + d.name + ".reset_port");
      auto input = clk;
      if (d.type == ClockType::InternalDivider || d.type == ClockType::EdgeQualified) {
        const auto &v = ir.values[clk.index];
        if (v.op != Op::ClockGate)
          throw std::logic_error("A divided domain clock is the gate of its clock port");
        input = v.operands[0];
      }
      if (!domains.emplace(d.name, d).second || !domain_clock_port(input, d) ||
          !input_port(port, d.reset_port, "reset:" + d.reset_port, "reset_n"))
        throw std::logic_error("Clock and reset ports must match their planned domains");
      clocks.insert(clk.index);
      clocks.insert(input.index);
      sync_clocks.insert(clk.index);
      sync_clocks.insert(input.index);
      if (d.type == ClockType::InternalDivider || d.type == ClockType::EdgeQualified)
        divided_clock(d, clk, input);
      if (!d.release_stages)
        resets[port.index].insert(input.index);
      else
        resets[port.index];
      const auto id = "domain/" + d.name + "/init";
      const auto local = d.release_stages ? stage(id, d.release_stages - 1) : port;
      if (!same(bound("domain/" + d.name + ".reset"), local))
        throw std::logic_error("A domain resets from its port or its release chain");
      if (!d.release_stages)
        continue;
      const auto first_next = state(stage(id, 0)).next;
      if (ir.values[first_next.index].op != Op::Constant ||
          ir.values[first_next.index].literal != 1 || ir.values[first_next.index].width != 1)
        throw std::logic_error("Release chain must shift constant one");
      chain([&](unsigned i) { return stage(id, i); }, first_next, d.release_stages, input, &port,
            id);
    }
  }
  bool wire(ValueId value, ValueId input) const {
    const auto &v = ir.values[value.index];
    return v.op == Op::BitOr && same(v.operands[0], input) &&
           ir.values[v.operands[1].index].op == Op::Constant &&
           ir.values[v.operands[1].index].literal == 0;
  }
  // The planned depth must be the one the declarations select: the explicit pair depth, else
  // the default from stages and periods.
  void crossing_shape(const ClockCut &cut) const {
    const auto &a = domains.at(cut.from_domain), &z = domains.at(cut.to_domain);
    const auto requested = p.explicit_depths.find({cut.from_domain, cut.to_domain});
    const auto stages = p.stages(cut.from_domain, cut.to_domain);
    auto depth =
        requested == p.explicit_depths.end()
            ? default_crossing_depth(stages, a.period_ps, z.period_ps)
            : Result<CrossingDepth>::success({requested->second, CrossingDepthSource::Explicit});
    const auto &options = std::get<AsyncClockCut>(cut.options);
    if (!depth || options.synchronizer_stages != stages || options.depth != depth.value() ||
        options.reset_handshake != p.handshake(cut.from_domain, cut.to_domain) ||
        options.count_bits != async_fifo_count_bits(options.depth.depth) ||
        options.index_bits != async_fifo_index_bits(options.depth.depth))
      throw std::logic_error("Crossing depth or count width differs from its declarations");
  }
  // The crossing FIFO of cut: its declared shape, its substituted clocks, resets and channel
  // handshake, each lowered value in place, and its count and handshake synchronizer chains.
  void crossing(const ClockCut &cut) {
    if (!std::holds_alternative<AsyncClockCut>(cut.options)) {
      related_crossing(cut);
      return;
    }
    crossing_shape(cut);
    const auto &options = std::get<AsyncClockCut>(cut.options);
    const auto &a = domains.at(cut.from_domain), &z = domains.at(cut.to_domain);
    const auto handshake = p.handshake(cut.from_domain, cut.to_domain);
    auto lowered = lower_async_fifo(a, z, options.depth.depth, options.synchronizer_stages,
                                    cut.fields, handshake, p.reset_coverage == ResetCoverage::All);
    if (!lowered)
      throw std::logic_error("Invalid planned crossing FIFO");
    const auto &t = lowered.value();
    auto mapped = [&](ValueId id) { return bound(cut.name + "/ir/" + decimal(id.index)); };
    std::set<std::size_t> replacements;
    for (const auto &d : t.domains) {
      replacements.insert(d.clock.index);
      replacements.insert(d.reset.index);
      if (!same(mapped(d.clock), clock(d.plan.name)) ||
          !same(mapped(d.reset), bound("domain/" + d.plan.name + ".reset")))
        throw std::logic_error("FIFO clock or reset substitution mismatch");
    }
    replacements.insert(t.valid.index);
    replacements.insert(t.out_ready.index);
    if (!same(mapped(t.valid), bound(cut.name + "/input.valid")) ||
        !same(mapped(t.out_ready), bound(cut.name + "/output.ready")) ||
        !wire(bound(cut.name + "/input.ready"), mapped(t.ready)) ||
        !wire(bound(cut.name + "/output.valid"), mapped(t.output_valid)))
      throw std::logic_error("FIFO channel handshake is disconnected");
    for (const auto &[key, value] :
         {std::pair{"/write", t.write}, std::pair{"/read", t.read},
          std::pair{"/write_count", t.write_count}, std::pair{"/read_count", t.read_count}})
      if (!same(bound(cut.name + key), mapped(value)))
        throw std::logic_error("FIFO map binding differs from the lowered FIFO");
    for (std::size_t i = 0; i < t.fields.size(); ++i) {
      const auto &f = t.fields[i];
      replacements.insert(f.input.index);
      if (!same(mapped(f.input), bound(cut.name + "/input.field" + decimal(i))) ||
          !wire(bound(cut.name + "/output.field" + decimal(i)), mapped(f.output)))
        throw std::logic_error("FIFO field is disconnected");
      // Payload slots cross only into the receive register.
      for (auto slot : f.storage)
        allowed_control_crossings.emplace(mapped(slot).index, mapped(f.received).index);
    }
    for (std::size_t i = 0; i < t.ir.values.size(); ++i)
      if (!replacements.contains(i))
        fifo_value(t.ir, ValueId{i}, mapped);
    chain(t.write_count_sync, mapped, mapped(t.write_count), clock(cut.to_domain));
    // The pressure of a network with levels: its synchronizer chain and its slot levels.
    if (const auto level_hint = "level_hint/" + cut.name + "/";
        h.bindings.contains(level_hint + "send")) {
      chain([&](unsigned i) { return bound(level_hint + "sync" + decimal(i)); },
            bound(level_hint + "send"), options.synchronizer_stages,
            root_clock(ir, clock(cut.to_domain)), nullptr);
      level_slots(cut, t, mapped, level_hint);
    }
    chain(t.read_count_sync, mapped, mapped(t.read_count), clock(cut.from_domain));
    for (unsigned d = 0; handshake && d < 2; ++d) {
      const auto &side = d ? cut.to_domain : cut.from_domain;
      const auto &other = t.handshake[1 - d];
      chain(t.handshake[d].peer_sync, mapped, mapped(other.announce), clock(side));
      chain(t.handshake[d].ack_sync, mapped, mapped(other.peer_sync.back()), clock(side));
    }
  }
  template <typename Mapped>
  void level_slots(const ClockCut &cut, const AsyncFifo &fifo, const Mapped &mapped,
                   const std::string &prefix) {
    Builder model;
    std::map<std::size_t, ValueId> leaves;
    const auto input = [&](ValueId actual) {
      const auto value = model.input("x", ir.values[actual.index].width, "", "");
      leaves.emplace(value.index, actual);
      return value;
    };
    const auto write_count = input(mapped(fifo.write_count_sync.back()));
    const auto read_count = input(mapped(fifo.read_count));
    const auto index = input(mapped(fifo.read_index));
    const auto enabled =
        fifo.reset_handshake ? input(mapped(fifo.handshake[1].enable)) : model.constant(1, 1);
    std::vector<ValueId> slots;
    for (unsigned i = 0; i < fifo.depth; ++i) {
      const auto slot = bound(prefix + "slot" + decimal(i));
      if (!same(domain_clock(slot), root_clock(ir, clock(cut.from_domain))))
        throw std::logic_error("FIFO level slot has the wrong clock");
      slots.push_back(input(slot));
    }
    const auto expected = lower_fifo_levels(model, write_count, read_count, index, enabled, slots);
    const auto match = [&](auto &self, ValueId want, ValueId got) -> void {
      if (const auto leaf = leaves.find(want.index); leaf != leaves.end()) {
        if (!same(leaf->second, got))
          throw std::logic_error("FIFO level reduction reads an unconfirmed slot or count");
        return;
      }
      const auto &a = model.ir.values.at(want.index), &z = ir.values.at(got.index);
      if (a.op != z.op || a.width != z.width || (a.op == Op::Constant && a.literal != z.literal))
        throw std::logic_error("FIFO confirmed level interval changed");
      for (unsigned i = 0; i < operand_count(a.op); ++i)
        self(self, a.operands[i], z.operands[i]);
    };
    const auto first = std::ranges::find(fifo.fields, "first", &FifoField::name);
    const auto level = std::ranges::find(fifo.fields, "level", &FifoField::name);
    if (first == fifo.fields.end() || level == fifo.fields.end())
      throw std::logic_error("FIFO level metadata needs fragment head fields");
    const auto write = input(mapped(fifo.write));
    const auto write_index = input(mapped(fifo.write_index));
    const auto head = input(mapped(first->input)), head_level = input(mapped(level->input));
    for (unsigned i = 0; i < fifo.depth; ++i) {
      const auto selected =
          model.binary(Op::BitAnd, write,
                       model.binary(Op::Equal, write_index, model.constant(fifo.index_bits, i)));
      const auto next =
          model.select(selected, model.select(head, head_level, model.constant(3, 0)), slots[i]);
      match(match, next, state(bound(prefix + "slot" + decimal(i))).next);
    }
    match(match, expected.count, bound("fifo_levels/" + cut.name + "/count"));
    for (unsigned i = 0; i < fifo.depth; ++i)
      match(match, expected.active[i], bound("fifo_levels/" + cut.name + "/active" + decimal(i)));
    match(match, expected.level, bound("fifo_levels/" + cut.name + "/queue_level"));
    confirmed_levels.emplace(bound("fifo_levels/" + cut.name + "/queue_level").index,
                             root_clock(ir, clock(cut.to_domain)).index);
  }
  void related_crossing(const ClockCut &cut) {
    const bool edge_aligned = std::holds_alternative<EdgeAlignedCut>(cut.options);
    const auto &a = domains.at(cut.from_domain), &z = domains.at(cut.to_domain);
    Result<RelatedAdapter> lowered =
        edge_aligned
            ? lower_edge_aligned(a, z, std::get<EdgeAlignedCut>(cut.options).slow_from_sender,
                                 std::get<EdgeAlignedCut>(cut.options).slow_pipeline, cut.fields,
                                 p.reset_coverage == ResetCoverage::All)
            : lower_related_sync(a, z, std::get<SyncClockCut>(cut.options).depth.depth,
                                 std::get<SyncClockCut>(cut.options).fast, cut.fields,
                                 p.reset_coverage == ResetCoverage::All);
    if (!lowered)
      throw std::logic_error("Invalid planned related-clock adapter");
    const auto &t = lowered.value();
    auto mapped = [&](ValueId id) { return bound(cut.name + "/ir/" + decimal(id.index)); };
    std::set<std::size_t> replacements;
    for (const auto &d : t.domains) {
      replacements.insert(d.clock.index);
      replacements.insert(d.reset.index);
      if (!same(mapped(d.clock), clock(d.plan.name)) ||
          !same(mapped(d.reset), bound("domain/" + d.plan.name + ".reset")))
        throw std::logic_error("Related adapter clock or reset substitution mismatch");
    }
    replacements.insert(t.valid.index);
    replacements.insert(t.out_ready.index);
    if (!same(mapped(t.valid), bound(cut.name + "/input.valid")) ||
        !same(mapped(t.out_ready), bound(cut.name + "/output.ready")) ||
        !wire(bound(cut.name + "/input.ready"), mapped(t.ready)) ||
        !wire(bound(cut.name + "/output.valid"), mapped(t.output_valid)))
      throw std::logic_error("Related adapter channel handshake is disconnected");
    for (std::size_t i = 0; i < t.fields.size(); ++i) {
      const auto &field = t.fields[i];
      replacements.insert(field.input.index);
      const bool sideband = field.name == "pressure" && i + 1 == t.fields.size();
      if (!same(mapped(field.input), bound(cut.name + "/input.field" + decimal(i))) ||
          !(sideband ? same(bound(cut.name + "/output.field" + decimal(i)), mapped(field.output))
                     : wire(bound(cut.name + "/output.field" + decimal(i)), mapped(field.output))))
        throw std::logic_error("Related adapter field is disconnected: " + cut.name + " field " +
                               decimal(i));
      for (auto slot : field.storage)
        slot_crossings.emplace(mapped(slot).index, root_clock(ir, clock(cut.to_domain)).index);
    }
    if (edge_aligned) {
      replacements.insert(t.slow_enable.index);
      const auto &slow = t.slow_from_sender ? a : z;
      const auto &fast = t.slow_from_sender ? z : a;
      if (slow.type == ClockType::InternalDivider) {
        const auto enable = mapped(t.slow_enable);
        const auto count = bound("domain/" + slow.name + "/divider.count");
        const auto &v = ir.values[enable.index];
        const auto equivalent = v.op == Op::Equal && same(v.operands[0], count) &&
                                ir.values[v.operands[1].index].op == Op::Constant &&
                                ir.values[v.operands[1].index].literal == 0;
        if (!same(enable, bound("domain/" + slow.name + "/divider.enable")) && !equivalent)
          throw std::logic_error("EdgeAligned uses the wrong slow-edge enable");
        slot_crossings.emplace(count.index, root_clock(ir, clock(fast.name)).index);
      } else if (ir.values[mapped(t.slow_enable).index].op != Op::Constant ||
                 ir.values[mapped(t.slow_enable).index].literal != 1) {
        throw std::logic_error("A root slow clock must have a constant edge enable");
      }
      allowed_input_crossings.emplace(bound(cut.name + "/input.valid").index,
                                      root_clock(ir, clock(cut.to_domain)).index);
      for (std::size_t i = 0; i < t.fields.size(); ++i)
        allowed_input_crossings.emplace(bound(cut.name + "/input.field" + decimal(i)).index,
                                        root_clock(ir, clock(cut.to_domain)).index);
      allowed_input_crossings.emplace(bound(cut.name + "/output.ready").index,
                                      root_clock(ir, clock(cut.from_domain)).index);
      if (t.admission_open.index != 0) {
        slot_crossings.emplace(
            mapped(t.admission_open).index,
            root_clock(ir, clock(t.slow_from_sender ? cut.from_domain : cut.to_domain)).index);
        if (!same(bound(cut.name + "/admission_open"), mapped(t.admission_open)))
          throw std::logic_error("EdgeAligned admission state is not mapped");
      }
    } else {
      for (const auto &[key, value] :
           {std::pair{"/write_count", t.write_count}, std::pair{"/read_count", t.read_count},
            std::pair{"/read_count_return", t.returned_read_count()},
            std::pair{"/write_count_return", t.returned_write_count()}})
        if (!same(bound(cut.name + key), mapped(value)))
          throw std::logic_error("Sync count binding differs from the lowered adapter");
      if (t.fast) {
        slot_crossings.emplace(mapped(t.read_count).index,
                               root_clock(ir, clock(cut.from_domain)).index);
        slot_crossings.emplace(mapped(t.write_count).index,
                               root_clock(ir, clock(cut.to_domain)).index);
      } else {
        allowed_control_crossings.emplace(mapped(t.read_count).index,
                                          mapped(t.read_count_sync.back()).index);
        allowed_control_crossings.emplace(mapped(t.write_count).index,
                                          mapped(t.write_count_sync.back()).index);
      }
    }
    for (std::size_t i = 0; i < t.ir.values.size(); ++i)
      if (!replacements.contains(i))
        fifo_value(t.ir, ValueId{i}, mapped);
  }
  // One value of the lowered FIFO template against its imported copy.
  template <typename Mapped>
  void fifo_value(const HardwareIR &fifo, ValueId id, const Mapped &mapped) const {
    const auto &original = fifo.values[id.index];
    const auto actual_id = mapped(id);
    const auto &actual = ir.values[actual_id.index];
    if (actual.op != original.op || actual.width != original.width)
      throw std::logic_error("FIFO expression type or width changed");
    if (original.op == Op::Constant) {
      if (actual.literal != original.literal)
        throw std::logic_error("FIFO constant changed");
    } else if (original.op == Op::State) {
      const auto &expected = fifo.states[original.state.index], &got = state(actual_id);
      if (!same(got.next, mapped(expected.next)) ||
          !same(root_clock(ir, got.clock), root_clock(ir, mapped(expected.clock))) ||
          got.reset.has_value() != expected.reset.has_value() ||
          (got.reset && (!same(got.reset->signal, mapped(expected.reset->signal)) ||
                         got.reset->value != expected.reset->value)))
        throw std::logic_error("FIFO storage update/reset/clock changed");
    } else if (original.op == Op::Slice) {
      // An import rebuilds the constant slice offset; compare its literal.
      const auto &offset = ir.values[actual.operands[1].index];
      if (!same(actual.operands[0], mapped(original.operands[0])) || offset.op != Op::Constant ||
          offset.literal != fifo.values[original.operands[1].index].literal)
        throw std::logic_error("FIFO count slice changed");
    } else {
      for (unsigned j = 0; j < operand_count(original.op); ++j)
        if (!same(actual.operands[j], mapped(original.operands[j])))
          throw std::logic_error("FIFO control expression changed");
    }
  }
  ValueId port_clock(const Port &port) const {
    const auto separator = port.endpoint.find(':');
    if (separator == std::string::npos)
      throw std::logic_error("Unbound physical port");
    const auto kind = port.endpoint.substr(0, separator);
    const auto name = port.endpoint.substr(separator + 1);
    // An address input belongs to the domain that samples it.
    if (kind == "address_input")
      return root_clock(
          ir, clock(std::ranges::find(p.address_inputs, name, &AddressInput::name)->clock_domain));
    if (kind == "error_log")
      return root_clock(ir, clock(p.observer_domain(name)));
    return root_clock(
        ir, clock(p.object_domains.at((kind == "initiator" ? "source/" : "target/") + name)));
  }
  // What the cone of each value reads, shared by every value reading the same: (0, clock) for a
  // plain register or an assigned input, (1, port) for a reset port, (2, register) for one a
  // synchronizer declaration names and (3, input) for an unassigned input.
  using Reads = std::vector<std::pair<unsigned, std::size_t>>;
  std::map<Reads, unsigned> read_ids;
  std::vector<const Reads *> reads;
  std::map<std::pair<unsigned, unsigned>, unsigned> merged;
  std::vector<unsigned> value_reads;
  unsigned intern(Reads r) {
    const auto [it, fresh] = read_ids.try_emplace(std::move(r), unsigned(reads.size()));
    if (fresh)
      reads.push_back(&it->first);
    return it->second;
  }
  unsigned merge(unsigned a, unsigned b) {
    const std::pair<unsigned, unsigned> key = std::minmax(a, b);
    if (const auto it = merged.find(key); a == b || it != merged.end())
      return a == b ? a : it->second;
    Reads r;
    std::ranges::set_union(*reads[a], *reads[b], std::back_inserter(r));
    return merged[key] = intern(std::move(r));
  }
  // The reads of every value in index order (operands precede their users), over the edges the
  // walk below follows: a register or input is a leaf, any other value merges its operands.
  void read_table() {
    std::set<std::size_t> named;
    for (const auto &[from, to] : stage_successor)
      named.insert(from);
    for (const auto &[from, to] : allowed_control_crossings)
      named.insert(from);
    for (const auto &[from, to] : slot_crossings)
      named.insert(from);
    for (std::size_t i = 0; i < ir.values.size(); ++i) {
      const auto &v = ir.values[i];
      Reads r;
      const auto confirmed = confirmed_levels.find(i);
      if (confirmed != confirmed_levels.end())
        r = {std::pair{0U, confirmed->second}};
      else if (v.op == Op::State) {
        const auto domain = domain_clock(ValueId{i}).index;
        r = {named.contains(i) ? std::pair{2U, i} : std::pair{0U, domain}};
      } else if (v.op == Op::Input)
        r = {resets.contains(i)         ? std::pair{1U, i}
             : input_clocks.contains(i) ? std::pair{0U, input_clocks.at(i).index}
                                        : std::pair{3U, i}};
      auto id = intern(std::move(r));
      if (confirmed == confirmed_levels.end() && v.op != Op::State && v.op != Op::Input &&
          v.op != Op::Constant)
        for (unsigned j = 0; j < operand_count(v.op); ++j)
          id = merge(id, value_reads[v.operands[j].index]);
      value_reads.push_back(id);
    }
  }
  // cone's checks on the reads of root, recording its crossings; false on a violation, which
  // the walk then reports in its own order.
  bool cone_reads(ValueId root, ValueId destination, ValueId destination_clock) {
    const auto &r = *reads[value_reads[root.index]];
    auto crosses = [&](std::size_t id) {
      return !same(domain_clock(ValueId{id}), destination_clock);
    };
    if (!std::ranges::all_of(r, [&](const auto &read) {
          const auto [kind, id] = read;
          const auto next = stage_successor.find(id);
          const auto slot = slot_crossings.find(id);
          return kind == 0   ? id == destination_clock.index
                 : kind == 1 ? resets.at(id).contains(destination_clock.index)
                 : kind == 2
                     ? (next == stage_successor.end() || next->second == destination.index) &&
                           (!crosses(id) ||
                            allowed_control_crossings.contains({id, destination.index}) ||
                            (slot != slot_crossings.end() &&
                             slot->second == destination_clock.index))
                     : (input_clocks.contains(id) &&
                        same(input_clocks.at(id), destination_clock)) ||
                           allowed_input_crossings.contains({id, destination_clock.index});
        }))
      return false;
    for (const auto &[kind, id] : r)
      if (kind == 2 && crosses(id) && !slot_crossings.contains(id))
        actual_control_crossings.emplace(id, destination.index);
    return true;
  }
  // The registers and inputs that the next value or output of destination reads, each from the
  // destination's clock or through a declared synchronizer.
  void cone(ValueId root, ValueId destination, ValueId destination_clock) {
    if (value_reads.size() == ir.values.size() && cone_reads(root, destination, destination_clock))
      return;
    walk_cone(
        ir, root,
        [&](ValueId id) {
          if (const auto next = stage_successor.find(id.index);
              next != stage_successor.end() && next->second != destination.index)
            throw std::logic_error("Intermediate synchronizer stage has a functional consumer");
          if (const auto slot = slot_crossings.find(id.index);
              slot != slot_crossings.end() && slot->second == destination_clock.index)
            return;
          if (!same(domain_clock(id), destination_clock)) {
            if (!allowed_control_crossings.contains({id.index, destination.index}))
              throw std::logic_error("Unqualified register crossing");
            actual_control_crossings.emplace(id.index, destination.index);
          }
        },
        [&](ValueId id) {
          if (allowed_input_crossings.contains({id.index, destination_clock.index}))
            return;
          if (resets.contains(id.index) ? !resets.at(id.index).contains(destination_clock.index)
                                        : !input_clocks.contains(id.index) ||
                                              !same(input_clocks.at(id.index), destination_clock))
            throw std::logic_error("Input sampled outside its assigned domain");
        });
  }
  void cones() {
    // A test port only drives clock gates; an enable or ungate port only feeds
    // gate enables (an ungate port through the or of the domain's divider or
    // region enables).
    for (std::size_t i = 0; i < ir.values.size(); ++i) {
      if (ir.values[i].op != Op::Input)
        continue;
      const auto &port = ir.ports[ir.values[i].input_port.index];
      if (port.signal == "enable" || port.signal == "ungate") {
        std::vector<ValueId> pending{ValueId{i}};
        while (!pending.empty()) {
          const auto at = pending.back();
          pending.pop_back();
          for (const auto &v : ir.values)
            for (unsigned j = 0; j < operand_count(v.op); ++j)
              if (v.operands[j].index == at.index) {
                if (v.op == Op::ClockGate && j == 1)
                  continue;
                if (v.op != Op::BitOr)
                  throw std::logic_error("An enable or ungate port only drives clock gates");
                pending.push_back(ValueId{static_cast<std::size_t>(&v - ir.values.data())});
              }
        }
        continue;
      }
      if (!resets.contains(i) && !clocks.contains(i) && port.signal != "test")
        input_clocks.emplace(i, port_clock(port));
    }
    try {
      read_table();
    } catch (const std::logic_error &) {
      value_reads.clear(); // the walks meet the fault in their own order
    }
    for (const auto &q : ir.states)
      cone(q.next, q.output, root_clock(ir, q.clock));
    for (const auto &driver : ir.drivers)
      cone(driver.value, ValueId{ir.values.size() + driver.port.index},
           port_clock(ir.ports[driver.port.index]));
    if (actual_control_crossings != allowed_control_crossings) {
      for (const auto &[from, to] : actual_control_crossings)
        if (!allowed_control_crossings.contains({from, to}))
          throw std::logic_error("Undeclared control crossing " + decimal(from) + " to " +
                                 decimal(to));
      for (const auto &[from, to] : allowed_control_crossings)
        if (!actual_control_crossings.contains({from, to}) &&
            !same(domain_clock(ValueId{from}), domain_clock(ValueId{to})))
          throw std::logic_error("Unused control crossing declaration " + decimal(from) + " to " +
                                 decimal(to));
    }
  }
};
} // namespace

Status verify_exchange_clock_hardware(const ExchangeHardware &h) {
  auto fail = [](const std::string &message) {
    return Status::failure(error("NCL_EXCHANGE_CLOCK_BINDING", "exchange.hardware", message));
  };
  if (!h.plan.clocked())
    return fail("Missing clock placement");
  if (auto status = verify_structure(h.ir); !status)
    return status;
  try {
    ClockAudit(h).run();
    return Status::success({});
  } catch (const std::logic_error &e) {
    return fail(e.what());
  }
}
} // namespace nocloom::detail
