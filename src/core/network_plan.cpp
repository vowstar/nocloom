#include "capability.hpp"
#include "diagnostic.hpp"
#include "exchange_composition.hpp"
#include "network_plan_profiles.hpp"
#include <algorithm>
#include <bit>
#include <iterator>
#include <nlohmann/json.hpp>
#include <numeric>
#include <ranges>
#include <span>
#include <tuple>

namespace nocloom::detail {
// Whether the connectivity matrix connects a source to a target, some flow pair of them. all
// connects every pair (an absent matrix is rejected first).
bool connects(const Definition &d, const std::string &source, const std::string &target) {
  return !d.connectivity || d.connectivity->all ||
         std::ranges::any_of(d.connectivity->pairs, [&](const ConnectivityEntry &e) {
           return e.source == source && e.target == target;
         });
}
bool connects(const Definition &d, const std::string &source, unsigned source_path,
              const std::string &target, unsigned target_path) {
  return !d.connectivity || d.connectivity->all ||
         std::ranges::any_of(d.connectivity->pairs, [&](const ConnectivityEntry &e) {
           return e.source == source && e.target == target &&
                  e.source_path.value_or(source_path) == source_path &&
                  e.target_path.value_or(target_path) == target_path;
         });
}
std::set<std::tuple<std::string, std::string, unsigned, unsigned>>
unconnected_flows(const Definition &d) {
  std::set<std::tuple<std::string, std::string, unsigned, unsigned>> out;
  for (const auto &i : d.initiators)
    for (const auto &t : d.targets)
      if (connects(d, i.name, t.name))
        for (unsigned f = 0; f < std::max<std::size_t>(1, i.route_classes.size()); ++f)
          for (unsigned g = 0; g < std::max(1U, t.flows); ++g)
            if (!connects(d, i.name, f, t.name, g))
              out.emplace(i.name, t.name, f, g);
  return out;
}
// One key per AxID value of every read-write AXI4 or AXI3 source connected to the target. AXI4-Lite
// and one-direction sources issue no exclusive access.
std::uint64_t exclusive_keys(const Definition &d, const Target &t) {
  return std::accumulate(
      d.initiators.begin(), d.initiators.end(), std::uint64_t{0},
      [&](std::uint64_t keys, const Initiator &initiator) {
        const bool exclusive = full_axi(initiator.protocol) &&
                               initiator.access.value_or(Access::ReadWrite) == Access::ReadWrite &&
                               connects(d, initiator.name, t.name);
        return exclusive ? keys + (std::uint64_t{1} << initiator.id_width.value_or(4)) : keys;
      });
}
// APB4 and APB3 initiators: single transfers carried as AXI4-Lite beats of the 32-bit network.
bool apb_initiator(const Initiator &initiator) {
  return initiator.protocol == Protocol::Apb4 || initiator.protocol == Protocol::Apb3;
}
bool full_axi(std::optional<Protocol> protocol) {
  return protocol.value_or(Protocol::Axi4) == Protocol::Axi4 || protocol == Protocol::Axi3;
}
unsigned source_engine_width(const Initiator &initiator, unsigned fallback) {
  return apb_initiator(initiator) ? 32U : initiator.data_width.value_or(fallback);
}
// APB containers count as 32 bits.
unsigned network_data_width(const Definition &d) {
  if (d.transport && d.transport->data_width)
    return *d.transport->data_width;
  const auto narrowest = [](unsigned w, const auto &endpoint) {
    const bool apb = protocol_caps(endpoint.protocol.value_or(Protocol::Axi4)).apb;
    const auto width = endpoint.data_width.value_or(64);
    return std::min(w, apb && width < 32 ? 32U : width);
  };
  return std::accumulate(d.targets.begin(), d.targets.end(),
                         std::accumulate(d.initiators.begin(), d.initiators.end(), 64U,
                                         [](unsigned w, const Initiator &i) {
                                           return std::min(w, source_engine_width(i, 64));
                                         }),
                         narrowest);
}
// The common source engine width, or else the network data width. Endpoint widths stay as
// written.
unsigned source_data_width(const Definition &d, unsigned network_bits) {
  const bool uniform =
      !d.initiators.empty() && std::ranges::all_of(d.initiators, [&](const Initiator &i) {
        return source_engine_width(i, network_bits) ==
               source_engine_width(d.initiators.front(), network_bits);
      });
  return uniform ? source_engine_width(d.initiators.front(), network_bits) : network_bits;
}
std::map<std::string, std::string> switch_identities(const Definition &d,
                                                     const ExchangeGraph &graph, bool response) {
  std::set<std::string> endpoints;
  std::ranges::transform(d.initiators, std::inserter(endpoints, endpoints.end()), &Initiator::name);
  std::ranges::transform(d.targets, std::inserter(endpoints, endpoints.end()), &Target::name);
  std::vector<std::string> ordered;
  std::ranges::transform(graph.nodes, std::back_inserter(ordered), &ExchangeNode::name);
  std::ranges::sort(ordered);
  std::map<std::string, std::string> identities;
  unsigned index = 0;
  for (const auto &name : ordered) {
    std::string id;
    do {
      id = (response ? "rsp_node" : "req_node") + std::to_string(index++);
    } while (endpoints.contains(id));
    identities.emplace(name, id);
  }
  return identities;
}
bool clocked_graph(const Definition &d) {
  const auto placed = [](const ExchangeGraph &graph) {
    return std::ranges::any_of(graph.nodes,
                               [](const auto &node) { return node.clock_domain.has_value(); });
  };
  return d.topology && (d.clock_domains.size() != 1 || placed(d.topology->request) ||
                        placed(d.topology->response));
}

std::vector<std::map<std::string, bool>> mode_planes(const Definition &d) {
  std::set<std::map<std::string, bool>> all;
  for (const auto &t : d.targets)
    for (const auto &m : t.mappings)
      if (!m.when.empty())
        all.insert(m.when);
  return {all.begin(), all.end()};
}
unsigned plane_bits(const Definition &d) {
  return static_cast<unsigned>(std::bit_width(mode_planes(d).size()));
}
unsigned mapping_plane(const Definition &d, const AddressMapping &m) {
  if (m.when.empty())
    return 0;
  const auto planes = mode_planes(d);
  return static_cast<unsigned>(std::ranges::find(planes, m.when) - planes.begin()) + 1;
}
MappingLayout layout_mappings(const Definition &d) {
  MappingLayout out;
  std::vector<std::pair<AddressWindow, std::pair<unsigned, unsigned>>> blocks;
  auto align = [](std::uint64_t a) { return a & (~a + 1); };
  // [base, end) as aligned power-of-two blocks, with pages as one run of whole pages.
  auto cut = [&](std::uint64_t base, std::uint64_t end, bool pages, auto take) {
    for (std::uint64_t s = 0; base < end; base += s) {
      s = base ? std::min(std::bit_floor(end - base), align(base)) : std::bit_floor(end - base);
      if (pages && s >= 4096)
        s = (end - base) / 4096 * 4096;
      take(AddressWindow{base, s});
    }
  };
  for (unsigned t = 0; t < d.targets.size(); ++t) {
    out.mappings.emplace_back(d.targets[t].mappings.size());
    for (unsigned m = 0; m < d.targets[t].mappings.size(); ++m) {
      const auto &x = d.targets[t].mappings[m];
      // A mode-dependent mapping lies in its plane above the global address space.
      const auto base = x.base + (std::uint64_t{mapping_plane(d, x)} << d.address_width);
      cut(base, base + x.size, false, [&](AddressWindow w) { blocks.push_back({w, {t, m}}); });
    }
  }
  std::ranges::sort(blocks, {},
                    [](const auto &b) { return std::pair{b.first.size, b.first.base}; });
  for (std::size_t i = 0; i < blocks.size(); ++i) {
    const auto &[w, at] = blocks[i];
    std::vector<AddressWindow> held;
    // Blocks of targets no initiator shares never meet in a decode: neither takes from the other.
    const auto shared = [&](unsigned a, unsigned b) {
      return a == b || std::ranges::any_of(d.initiators, [&](const Initiator &s) {
               return connects(d, s.name, d.targets[a].name) &&
                      connects(d, s.name, d.targets[b].name);
             });
    };
    for (const auto &[h, other] : std::span(blocks).first(i))
      if (h.base >= w.base && h.base + h.size <= w.base + w.size &&
          (h == w || shared(at.first, other.first))) {
        held.push_back(h);
        out.overlaps.emplace(at, other, h == w);
      }
    std::ranges::sort(held, {}, &AddressWindow::base);
    auto &[routes, boundaries] = out.mappings[at.first][at.second];
    auto next = w.base;
    // The boundary of a block holding others keeps its packets out of them, inside that block
    // only.
    std::uint64_t boundary = 0;
    for (const auto &h : held) {
      boundary = std::min(boundary ? boundary : w.size, h.base > next ? align(h.base) : w.size);
      if (h.base > next)
        routes.push_back({next, h.base - next});
      next = std::max(next, h.base + h.size);
    }
    if (boundary)
      boundaries.emplace_back(w, boundary);
    if (next < w.base + w.size)
      routes.push_back({next, w.base + w.size - next});
  }
  for (auto &target : out.mappings)
    for (auto &[routes, _] : target) {
      std::ranges::sort(routes, {}, &AddressWindow::base);
      std::vector<AddressWindow> merged;
      for (const auto &r : routes)
        if (!merged.empty() && merged.back().base + merged.back().size == r.base)
          merged.back().size += r.size;
        else
          merged.push_back(r);
      routes.clear();
      for (const auto &r : merged)
        cut(r.base, r.base + r.size, true, [&](AddressWindow w) { routes.push_back(w); });
    }
  return out;
}
namespace {
// Validation admits only these five policies wherever one is selected.
treadle::Policy selector(ArbitrationPolicy policy) {
  switch (policy) {
  case ArbitrationPolicy::LevelRoundRobin:
    return treadle::Policy::LevelRoundRobin;
  case ArbitrationPolicy::Fifo:
    return treadle::Policy::Fifo;
  case ArbitrationPolicy::RotatingPriority:
    return treadle::Policy::RotatingPriority;
  case ArbitrationPolicy::FixedPriority:
    return treadle::Policy::FixedPriority;
  default:
    return treadle::Policy::RoundRobin;
  }
}
// Whether each mapping has a sub-page routing window in a page another target's, or another flow
// of its target's, shares.
void plan_routes(NetworkPlan &plan) {
  std::map<std::uint64_t, std::set<std::pair<const TargetPlan *, unsigned>>> owners;
  auto sub = [](const AddressWindow &r) { return r.size % 4096 != 0; };
  for (const auto &t : plan.targets)
    for (const auto &m : t.mappings)
      for (const auto &r : m.routes | std::views::filter(sub))
        owners[r.base / 4096].insert({&t, m.flow});
  for (auto &t : plan.targets)
    for (auto &m : t.mappings)
      m.shared = std::ranges::any_of(m.routes | std::views::filter(sub), [&](const auto &r) {
        return owners.at(r.base / 4096).size() > 1;
      });
}
// A configured endpoint pipe (any mode set) under its storage name; payload bits come with the
// composition plan.
void endpoint_pipe(std::vector<EndpointPipe> &pipes, std::string name, LinkPipeline mode) {
  if (mode.forward || mode.backward)
    pipes.push_back({std::move(name), mode});
}
// Whether a one-direction endpoint has the AXI channel: AR and R only with reads, AW, W and B
// only with writes.
bool one_way_keeps(bool read_only, bool write_only, std::string_view channel) {
  return channel == "ar" || channel == "r" ? !write_only : !read_only;
}
void plan_source_pipes(SourcePlan &s, const Initiator &initiator) {
  const auto &pipes = initiator.pipes;
  // An AHB face has no AXI channels to pipe.
  for (const auto *channel : {"aw", "w", "ar"})
    if (s.protocol == Protocol::Axi4 && !s.ahb && one_way_keeps(s.read_only, s.write_only, channel))
      endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/" + channel, pipes.request);
  // The forward stage of R and B is the engine's own response register.
  for (const auto *channel : {"r", "b"})
    if (s.protocol == Protocol::Axi4 && !s.ahb && one_way_keeps(s.read_only, s.write_only, channel))
      endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/" + channel,
                    {false, pipes.response.backward});
  if (s.qos.picks() && (pipes.packets.forward || pipes.packets.backward)) {
    endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/packets", pipes.packets);
  } else {
    for (const auto *direction : {"write", "read"})
      endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/packets/" + direction, pipes.packets);
  }
  endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/issue", {pipes.issue, false});
}
// The endpoint settings of the definition, in name order.
void plan_endpoints(NetworkPlan &plan, const Definition &d) {
  const auto options = d.transport.value_or(TransportOptions{});
  const auto layout = layout_mappings(d);
  plan.user_flags = options.user_flags;
  plan.burst_user_bits = static_cast<unsigned>(options.user_flags.size());
  plan.burst_response_user_bits = options.response_user_bits;
  for (const auto &initiator : d.initiators) {
    auto &s = plan.sources.emplace_back();
    s.name = initiator.name;
    s.external_protocol = initiator.protocol.value_or(Protocol::Axi4);
    s.qos = initiator.qos;
    if (plan.placement)
      plan.placement->objects["source/" + s.name] = initiator.clock_domain;
    s.address_bits = initiator.address_width.value_or(d.address_width);
    // The network carries an APB initiator's transfers as AXI4-Lite single beats; only its port
    // face differs.
    const bool apb = apb_initiator(initiator);
    s.protocol = apb ? Protocol::Axi4Lite : initiator.protocol.value_or(Protocol::Axi4);
    if (apb) {
      s.apb = *initiator.protocol;
      s.apb_bits = initiator.data_width.value_or(32);
    }
    // An AHB burst holds at most 16 beats; the face drives AxID zero.
    s.ahb = initiator.protocol == Protocol::AhbV3;
    if (s.ahb) {
      s.protocol = Protocol::Axi4;
      s.len_bits = 4;
    }
    s.axi3 = initiator.protocol == Protocol::Axi3;
    if (s.axi3)
      s.protocol = Protocol::Axi4;
    s.engine_bits = source_engine_width(initiator, plan.burst_source_data_bits);
    // A source wider than the network word attaches at its own width: its packets carry whole
    // source beats, and the transport adapters on its link step them down to the network word.
    plan.per_endpoint = plan.per_endpoint || s.engine_bits != plan.burst_data_bits;
    if (full_axi(initiator.protocol)) {
      s.id_bits = initiator.id_width.value_or(4);
      s.len_bits = initiator.len_width.value_or(4);
    }
    if (!initiator.route_classes.empty()) {
      s.flows = initiator.route_classes;
      s.default_class = *initiator.default_class;
      s.id_ranges = class_partition(initiator);
    }
    s.read_only = initiator.access == Access::ReadOnly;
    s.write_only = initiator.access == Access::WriteOnly;
    s.default_target = initiator.default_target;
    s.user_bits = initiator.user_width.value_or(0);
    s.response_user_bits = initiator.response_user_width.value_or(0);
    for (const auto &flag : options.user_flags)
      s.user_flags.push_back(initiator.user_flags.at(flag));
    s.split_narrow = initiator.narrow_bursts == NarrowBursts::SplitNonModifiable;
    s.pooled = !apb;
    plan_source_pipes(s, initiator);
    s.outstanding = initiator.max_outstanding.value_or(options.max_outstanding);
    s.engine_slots = s.outstanding;
    if (s.qos.picks() && s.pooled &&
        (initiator.pipes.packets.forward || initiator.pipes.packets.backward ||
         initiator.pipes.issue)) {
      const unsigned buffers =
          unsigned(initiator.pipes.packets.forward) + unsigned(initiator.pipes.packets.backward);
      const unsigned headroom = std::bit_ceil(s.outstanding + 1) - 1 - s.outstanding;
      const unsigned extra = std::min(buffers, headroom);
      if (buffers >= headroom)
        s.request_limit = s.outstanding + headroom;
      if (s.outstanding > 1)
        s.engine_slots += extra;
    }
    if (s.outstanding > 1) {
      const unsigned per_direction = 1U << std::min(initiator.id_width.value_or(4), 6U);
      s.outstanding_ids =
          initiator.protocol == Protocol::Axi4Lite ? 1U : std::min(s.outstanding, per_direction);
      const unsigned keys_per_direction =
          initiator.protocol == Protocol::Axi4Lite ? 1U : per_direction;
      s.id_keys =
          std::min(s.outstanding, keys_per_direction * (s.read_only || s.write_only ? 1U : 2U));
      s.id_table =
          initiator.id_table_entries.value_or(options.id_table_entries.value_or(s.id_keys));
    }
  }
  std::ranges::sort(plan.sources, {}, &SourcePlan::name);
  for (const auto &t : d.targets) {
    auto &x = plan.targets.emplace_back();
    x.name = t.name;
    const auto index = static_cast<std::size_t>(&t - d.targets.data());
    for (std::size_t m = 0; m < t.mappings.size(); ++m) {
      const auto &y = t.mappings[m];
      const auto plane = mapping_plane(d, y);
      x.mappings.push_back({{y.base + (std::uint64_t{plane} << d.address_width), y.size},
                            y.local_address,
                            layout.mappings[index][m].first,
                            layout.mappings[index][m].second,
                            false,
                            y.when,
                            plane,
                            y.flow});
    }
    x.flows = t.flows;
    std::ranges::sort(x.mappings, {}, [](const MappingPlan &m) { return m.window.base; });
    if (plan.placement)
      plan.placement->objects["target/" + t.name] = t.clock_domain;
    x.local_bits = t.address_width.value_or(d.address_width);
    x.external_protocol = t.protocol.value_or(Protocol::Axi4);
    x.axi3 = t.protocol == Protocol::Axi3;
    x.protocol = x.axi3 ? Protocol::Axi4 : t.protocol.value_or(Protocol::Axi4);
    // Absent id_width: the default ID width for several bursts or a target monitor.
    if (x.protocol == Protocol::Axi4) {
      if (t.id_width ||
          (t.max_outstanding.value_or(1) < 2 && t.exclusive != ExclusiveAccess::Target))
        x.id_bits = t.id_width.value_or(4);
      x.len_bits = t.len_width.value_or(4);
      x.read_interleave_bytes = t.read_interleave_bytes;
    }
    x.attributes = t.attributes;
    x.response_user_bits = t.response_user_width.value_or(0);
    for (const auto &bit : t.user)
      x.user.push_back(
          bit.flag.empty()
              ? AttributeBit{bit.value ? AttributeSource::One : AttributeSource::Zero, bit.invert}
              : AttributeBit{AttributeSource::User, bit.invert,
                             static_cast<unsigned>(std::ranges::find(options.user_flags, bit.flag) -
                                                   options.user_flags.begin())});
    x.read_only = t.access == Access::ReadOnly;
    x.write_only = t.access == Access::WriteOnly;
    x.back_to_back = t.apb_back_to_back.value_or(false);
    x.read_side_effects = t.read_side_effects.value_or(false);
    const auto stem = "target/" + t.name + "/pipe/";
    endpoint_pipe(x.pipes, stem + "arrival", t.pipes.arrival);
    endpoint_pipe(x.pipes, stem + "dispatch", {t.pipes.dispatch, false});
    // The forward stage of AW, W and AR is the outlet's own address and write register.
    for (const auto *channel : {"aw", "w", "ar"})
      if (one_way_keeps(x.read_only, x.write_only, channel))
        endpoint_pipe(x.pipes, stem + channel, {false, t.pipes.request.backward});
    endpoint_pipe(x.pipes, stem + "response", t.pipes.response);
    x.fixed_single = t.fixed_bursts == FixedBursts::Single;
    x.timeout = t.timeout;
    x.complete_writes = t.write_bursts == WriteBursts::Complete;
    x.fixed_length = t.undefined_length_bursts == false;
    x.hresp_bits = t.hresp_width == 2U ? 2 : 1;
    // A narrow APB target keeps the network word as its engine width.
    x.data_bits = plan.burst_data_bits;
    if (protocol_caps(x.protocol).apb && t.data_width && *t.data_width < 32)
      x.apb_bits = *t.data_width;
    else
      x.data_bits = t.data_width.value_or(plan.burst_data_bits);
    plan.per_target = plan.per_target || x.data_bits != plan.burst_data_bits;
    plan.per_endpoint = plan.per_endpoint || plan.per_target;
    x.outstanding = std::max(t.max_outstanding.value_or(1), 1U);
    // With several bursts in flight a write-only outlet's BREADY looks for the slot of BID; a
    // backward pipe keeps it registered (the others park a B instead, Axi4OutletProfile).
    if (x.protocol == Protocol::Axi4 && x.outstanding > 1 && x.write_only)
      endpoint_pipe(x.pipes, stem + "b", {false, true});
    if (const auto mode = t.exclusive.value_or(ExclusiveAccess::None);
        mode != ExclusiveAccess::None) {
      x.exclusive.mode = mode;
      auto owners = t.exclusive_id_flags;
      std::ranges::sort(owners);
      std::ranges::transform(
          owners, std::back_inserter(x.exclusive.id_flags), [&](const std::string &flag) {
            return static_cast<unsigned>(std::ranges::find(options.user_flags, flag) -
                                         options.user_flags.begin());
          });
      if (mode == ExclusiveAccess::Network) {
        x.exclusive.monitors =
            t.exclusive_monitors.value_or(owners.size() >= 4 ? 16U : 1U << owners.size());
        x.exclusive.granule = options.exclusive_granule.value_or(1);
      }
    }
  }
  std::ranges::sort(plan.targets, {}, &TargetPlan::name);
  plan_routes(plan);
}
// The switch graphs: switches numbered per direction in name order (skipping endpoint names),
// per-output arbitration, link words and stages, and the map's topology record. Without an
// explicit topology the network is one switch per direction with every endpoint attached: each
// switch output takes only the sources connected to it, a sparse crossbar. The definition checks
// guarantee every switch name and every switch a link or attachment names.
void plan_graphs(NetworkPlan &plan, const Definition &d) {
  const auto topology = d.topology ? *d.topology : [&] {
    ExchangeTopology generated;
    generated.request.nodes = generated.response.nodes = {{"crossbar"}};
    for (const auto &source : plan.sources) {
      generated.request.entries.push_back({source.name, "crossbar"});
      generated.response.exits.push_back({source.name, "crossbar"});
    }
    for (const auto &target : plan.targets) {
      generated.request.exits.push_back({target.name, "crossbar"});
      generated.response.entries.push_back({target.name, "crossbar"});
    }
    return generated;
  }();
  nlohmann::json topology_record;
  if (!d.topology)
    topology_record["generated"] = "one_switch_per_direction";
  auto convert = [&](ExchangeGraph graph, const std::string &direction,
                     DirectedSwitchGraph &result) {
    const bool response = direction == "response";
    const auto scope = response ? "rsp/" : "req/";
    const auto identities = switch_identities(d, graph, response);
    std::ranges::sort(graph.nodes, {}, &ExchangeNode::name);
    std::ranges::sort(graph.links, [](const auto &x, const auto &y) {
      return std::tie(x.from, x.to) < std::tie(y.from, y.to);
    });
    for (auto *attachments : {&graph.entries, &graph.exits})
      std::ranges::sort(*attachments, [](const auto &x, const auto &y) {
        return std::tie(x.endpoint, x.node) < std::tie(y.endpoint, y.node);
      });
    auto &record = topology_record[direction];
    for (const auto *key : {"nodes", "links", "entries", "exits"})
      record[key] = nlohmann::json::array();
    for (const auto &node : graph.nodes) {
      const auto &id = identities.at(node.name);
      result.nodes.push_back(id);
      const auto word = node.transport_word_bytes.value_or(plan.transport_word_bytes);
      plan.nodes.push_back({id, node.name, word});
      record["nodes"].push_back(
          {{"name", node.name}, {"hardware_node", id}, {"transport_word_bytes", word}});
      if (plan.placement) {
        plan.placement->objects[scope + id] = node.clock_domain.value();
        record["nodes"].back()["clock_domain"] = *node.clock_domain;
      }
      // Per-output arbitration, keyed by generated node identities. Field paths keep the
      // configured node order.
      const auto &configured = response ? topology.response.nodes : topology.request.nodes;
      const auto at =
          std::ranges::find(configured, node.name, &ExchangeNode::name) - configured.begin();
      for (std::size_t j = 0; j < node.arbitration.size(); ++j) {
        const auto &o = node.arbitration[j];
        auto key = [&](const std::string &name) {
          return name.starts_with("node/") ? "node/" + identities.at(name.substr(5)) : name;
        };
        OutputArbiterRequest r{selector(o.policy),
                               {},
                               "topology." + direction + ".nodes[" + std::to_string(at) +
                                   "].arbitration[" + std::to_string(j) + "]"};
        std::ranges::transform(o.priority, std::back_inserter(r.priority), key);
        plan.output_arbitration[scope + id + ">" + key(o.output)] = std::move(r);
      }
    }
    // A configured pipe: kept on the link draft and recorded under key.
    auto pipe = [](const LinkPipeline &mode, LinkPipeline &planned, nlohmann::json &record,
                   const char *key) {
      if (mode.forward || mode.backward || mode.ungated) {
        planned = mode;
        record[key] = {{"forward", mode.forward}, {"backward", mode.backward}};
        if (mode.half_rate)
          record[key]["half_rate"] = true;
        if (!mode.bypass_port.empty())
          record[key]["bypass_port"] = mode.bypass_port;
        if (mode.ungated)
          record[key]["ungated"] = true;
      }
    };
    for (const auto &edge : graph.links) {
      const auto &from = identities.at(edge.from), &to = identities.at(edge.to);
      result.edges.push_back({from, to});
      nlohmann::json link = {{"from", edge.from}, {"to", edge.to}};
      auto &planned = plan.links.emplace_back();
      planned.name = scope + from + ">" + to;
      if ((planned.word = edge.transport_word_bytes))
        link["transport_word_bytes"] = *edge.transport_word_bytes;
      pipe(edge.pipeline, planned.in_pipe, link, "pipeline");
      pipe(edge.sender_pipeline, planned.out_pipe, link, "sender_pipeline");
      record["links"].push_back(std::move(link));
    }
    // An attachment's pipe is at its switch: the receiver of an entry, the sender of an exit.
    for (const auto *key : {"entries", "exits"}) {
      const bool entry = std::string_view(key) == "entries";
      for (const auto &attachment : entry ? graph.entries : graph.exits) {
        const auto &node = identities.at(attachment.node);
        (entry ? result.entries : result.exits).push_back({attachment.endpoint, node});
        nlohmann::json item = {{"endpoint", attachment.endpoint}, {"node", attachment.node}};
        LinkPlan planned;
        planned.name =
            scope + (entry ? attachment.endpoint + ">" + node : node + ">" + attachment.endpoint);
        pipe(attachment.pipeline, entry ? planned.in_pipe : planned.out_pipe, item, "pipeline");
        if (item.contains("pipeline"))
          plan.links.push_back(std::move(planned));
        record[key].push_back(std::move(item));
      }
    }
    // The flows of every initiator with several, and the explicit routes of this direction in
    // generated node identities, each of its flow.
    result.response = response;
    for (const auto &i : d.initiators)
      if (i.route_classes.size() > 1)
        result.flows[i.name] = static_cast<unsigned>(i.route_classes.size());
    for (const auto &t : d.targets)
      if (t.flows > 1)
        result.target_paths[t.name] = t.flows;
    result.unconnected = unconnected_flows(d);
    for (const auto &route : topology.routes)
      if (const auto &path = response ? route.response : route.request; !path.empty()) {
        auto &fixed = result.fixed_routes.emplace_back(
            response ? SwitchRoute{route.target, route.initiator, {}}
                     : SwitchRoute{route.initiator, route.target, {}});
        std::ranges::transform(path, std::back_inserter(fixed.nodes),
                               [&](const std::string &n) { return identities.at(n); });
        if (result.flows.contains(route.initiator))
          fixed.flows = {route.source_path};
        if (result.target_paths.contains(route.target))
          fixed.target_path = route.target_path;
      }
  };
  auto &request = plan.exchange.request.graph, &response = plan.exchange.response.graph;
  convert(topology.request, "request", request);
  convert(topology.response, "response", response);
  std::ranges::sort(plan.nodes, {}, &NodePlan::id);
  // The explicit routes as written, by pair and flow pair.
  auto routes = topology.routes;
  std::ranges::sort(routes, {}, [](const ExchangeRoute &r) {
    return std::tie(r.initiator, r.target, r.source_path, r.target_path);
  });
  for (const auto &route : routes) {
    nlohmann::json row = {{"initiator", route.initiator}, {"target", route.target}};
    if (route.source_path)
      row["source_path"] = route.source_path;
    if (route.target_path)
      row["target_path"] = route.target_path;
    for (const auto &[key, path] :
         {std::pair{"request", &route.request}, {"response", &route.response}})
      if (!path->empty())
        row[key] = *path;
    topology_record["routes"].push_back(std::move(row));
  }
  plan.topology_record = topology_record.dump();
  // The connectivity matrix is the admission list of both directions.
  request.admitted_pairs.emplace();
  response.admitted_pairs.emplace();
  const auto limits = d.transport.value_or(TransportOptions{}).packet_limits;
  for (unsigned si = 0; si < plan.sources.size(); ++si)
    for (unsigned ti = 0; ti < plan.targets.size(); ++ti)
      if (const auto &s = plan.sources[si].name, &t = plan.targets[ti].name; connects(d, s, t)) {
        request.admitted_pairs->push_back({s, t});
        response.admitted_pairs->push_back({t, s});
        const auto limit = std::ranges::find(limits, std::pair{s, t}, [](const PacketLimit &l) {
          return std::pair{l.source, l.target};
        });
        plan.pairs.push_back(limit == limits.end()
                                 ? PairPlan{si, ti}
                                 : PairPlan{si, ti, limit->max_bytes.value_or(0),
                                            limit->boundary_bytes.value_or(0),
                                            "transport.packet_limits[" +
                                                std::to_string(limit - limits.begin()) + "]"});
      }
}
} // namespace

unsigned id_flow(const SourcePlan &source, std::uint64_t id) {
  const auto range =
      std::ranges::find_if(source.id_ranges, [&](const auto &r) { return id <= r.last; });
  return range == source.id_ranges.end() ? 0 : range->path;
}

// Register routing supports four branches per switch and eight loggers per monitor.
static constexpr std::size_t service_branch_limit = 4, logger_chain_limit = 8;
// A register target serves one packet at a time from APB4 sources in one domain.
static Status check_service_targets(const NetworkPlan &plan) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(
        error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.registers." + field, message));
  };
  const auto served =
      std::ranges::count_if(plan.targets, [](const auto &t) { return t.service.has_value(); });
  if (served == 0)
    return Status::success({});
  if (served > 1)
    return bad("targets", "Expected one window of registers");
  if (plan.burst_data_bits != 32 || plan.mapped() || plan.regulated())
    return bad("network", "A window needs a 32-bit network without scheduling levels");
  for (const auto &t : plan.targets) {
    if (!t.service)
      continue;
    if (t.service->banks.back().branch >= service_branch_limit)
      return bad(t.name, "A window has at most four logs");
    std::set<unsigned> routes;
    std::set<std::uint64_t> blocks;
    for (const auto &bank : t.service->banks) {
      if (auto checked = check_register_bank(bank.plan); !checked)
        return checked;
      if (!routes.insert(bank.plan.route).second)
        return bad(t.name, "Two banks of a window share route " + std::to_string(bank.plan.route));
      const auto size = std::uint64_t{1} << bank.plan.address_bits;
      if (bank.base % size != 0 || !blocks.insert(bank.base).second)
        return bad(t.name, "The banks of a window are aligned and apart");
    }
    // The bank serves one packet at a time, whatever K would allow.
    if (t.protocol != Protocol::Apb4 || t.outstanding != 1 || t.flows != 1 || t.back_to_back ||
        t.read_only || t.write_only || t.timeout || !t.pipes.empty() ||
        t.exclusive.mode != ExclusiveAccess::None || t.apb_bits != 32)
      return bad(t.name, "A window has one packet, one flow and no options");
    if (t.mappings.size() != 1 || !t.mappings.front().when.empty())
      return bad(t.name, "A window has one unconditional mapping");
    const auto size = t.mappings.front().window.size;
    const auto reach = [&](const ServiceBank &bank) {
      return bank.base + (1ULL << bank.plan.address_bits);
    };
    if (size > (1ULL << 20) || t.mappings.front().window.base % size != 0 ||
        std::ranges::any_of(t.service->banks,
                            [&](const ServiceBank &bank) { return reach(bank) > size; }))
      return bad(t.name, "A window holds its banks in an aligned block of at most 1 MiB");
    if (std::ranges::any_of(plan.sources, [&](const SourcePlan &source) {
          return reaches(plan, source.name, t.name) &&
                 (source.apb != Protocol::Apb4 || source.apb_bits != 32);
        }))
      return bad(t.name, "Only 32-bit APB4 sources reach a window");
  }
  return Status::success({});
}

// The clock domains a service network keeps to: the path to the banks has no crossing but its own.
// A clocked network has no link crossing; a target's banks and loggers take one domain, and each
// crossing to it joins two plain clocks of one reset that declare no crossing of their own. A
// crossed error logger watches APB4 targets of one domain and does not keep fields.
static Status check_service_domains(const NetworkPlan &plan) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(
        error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.registers." + field, message));
  };
  const auto apart = [&](const std::string &field, const std::string &near,
                         const std::string &far) {
    const auto &all = plan.placement->domains;
    const auto a = std::ranges::find(all, near, &DomainPlan::name);
    const auto z = std::ranges::find(all, far, &DomainPlan::name);
    if (z == all.end())
      return bad(field, "No clock domain " + far);
    const auto plain = [](const DomainPlan &d) {
      return d.type == ClockType::Direct && d.clock_root.empty() && d.edge_parent.empty();
    };
    if (!plain(*a) || !plain(*z) || a->reset_port != z->reset_port)
      return bad(field, "A serial crossing joins clocks of their own and of one reset");
    if (std::ranges::any_of(plan.placement->crossings, [&](const auto &c) {
          return (c.from_domain == near && c.to_domain == far) ||
                 (c.from_domain == far && c.to_domain == near);
        }))
      return bad(field, "A serial crossing takes no declaration");
    return Status::success({});
  };
  for (const auto &t : plan.targets) {
    if (!t.service)
      continue;
    const auto &domain = t.service->domain;
    if (!plan.placement) {
      if (!domain.empty())
        return bad(t.name, "A bank of another clock domain needs a clocked network");
      continue;
    }
    if (std::ranges::any_of(
            plan.links, [](const LinkPlan &link) { return link.from_domain != link.to_domain; }))
      return bad(t.name, "A clocked network with a window has no link crossing");
    const auto &own = plan.placement->objects.at("target/" + t.name);
    if (domain.empty() || domain == own)
      continue;
    if (auto status = apart(t.name, own, domain); !status)
      return status;
  }
  for (const auto &observer : plan.fault_monitors) {
    if (!observer.logger.crossed)
      continue;
    const auto &domain = plan.targets[observer.service].service->domain;
    if (observer.keep_fields)
      return bad(observer.logger.name, "A crossed log does not keep fields");
    // Which side's clock is the faster fixes where a record's serial words run.
    const auto period_of = [&](const std::string &name) {
      const auto &all = plan.placement->domains;
      const auto at = std::ranges::find(all, name, &DomainPlan::name);
      return at == all.end() ? std::nullopt : at->period_ps;
    };
    if (!period_of(domain))
      return bad(observer.logger.name, "A crossed log's clocks declare their periods");
    const auto &first =
        plan.placement->objects.at("target/" + plan.targets[observer.observed[0].target].name);
    for (const auto &observed : observer.observed) {
      const auto &t = plan.targets[observed.target];
      const auto &own = plan.placement->objects.at("target/" + t.name);
      if (own == domain || own != first)
        return bad(t.name, "The targets of a crossed log share a domain apart from it");
      if (t.protocol != Protocol::Apb4)
        return bad(t.name, "A crossed log watches APB4 targets");
      if (!period_of(own))
        return bad(t.name, "A crossed log's clocks declare their periods");
      if (auto status = apart(t.name, own, domain); !status)
        return status;
    }
  }
  return Status::success({});
}

// Logs watch 32-bit APB4 or AXI4 targets reached by 32-bit sources without link pipes.
// Each target admits one burst. Audit flags fit in one record word.
static constexpr std::size_t security_flags_limit = 8;
// Sets a network's security flags, in their order, and each source's drivers of them.
static Status plan_security_flags(NetworkPlan &plan, const InternalNetwork &internal) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(
        error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.audit_flags." + field, message));
  };
  const auto &request = internal.security;
  if (request.flags.empty() && request.drivers.empty())
    return Status::success({});
  if (internal.fault_monitors.empty())
    return bad("flags", "Audit flags serve an error log");
  const auto &flags = request.flags;
  const std::set<std::string> distinct(flags.begin(), flags.end());
  if (flags.empty() || flags.size() > security_flags_limit || distinct.size() != flags.size() ||
      !std::ranges::all_of(flags, valid_name))
    return bad("flags", "One to eight distinct audit flags with valid names");
  for (const auto &[source, drivers] : request.drivers)
    if (std::ranges::find(plan.sources, source, &SourcePlan::name) == plan.sources.end())
      return bad(source, "No such source");
  for (auto &source : plan.sources) {
    const auto of = request.drivers.find(source.name);
    if (of == request.drivers.end() || of->second.size() != flags.size())
      return bad(source.name, "Every source drives every audit flag");
    const bool apb4 = source.apb == Protocol::Apb4;
    const bool axi4 =
        !source.apb && !source.ahb && !source.axi3 && source.protocol == Protocol::Axi4;
    if (!apb4 && !axi4)
      return bad(source.name, "Only APB4 and AXI4 sources drive audit flags");
    if (source.clamp)
      return bad(source.name, "A source of audit flags is no wider than its targets");
    for (const auto &name : flags) {
      const auto bit = of->second.find(name);
      if (bit == of->second.end())
        return bad(source.name, "Every source drives every audit flag");
      using S = AttributeSource;
      const auto kind = bit->second.source;
      const bool constant = kind == S::Zero || kind == S::One;
      const bool protection =
          kind == S::Privileged || kind == S::Nonsecure || kind == S::Instruction;
      const bool user = kind == S::User && bit->second.index < source.user_bits;
      if (!constant && !protection && !user)
        return bad(source.name + "." + name,
                   "An audit flag takes a constant, an AxPROT flag or a bit of the AxUSER port");
      source.security_flags.push_back(bit->second);
    }
  }
  if (std::ranges::any_of(plan.targets, [](const TargetPlan &t) { return t.upsize.has_value(); }))
    return bad("targets", "A network with audit flags has no target wider than its transport");
  plan.security_flags = flags;
  plan.burst_security_bits = static_cast<unsigned>(plan.security_flags.size());
  return Status::success({});
}
// One observed target of an error logger, and the sources that reach it.
static Status check_observed_target(const NetworkPlan &plan, const FaultMonitorPlan &observer,
                                    const ObservedTarget &observed) {
  const auto &t = plan.targets[observed.target];
  const auto bad = [&](const std::string &message) {
    return Status::failure(error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + t.name, message));
  };
  const bool apb4 = t.protocol == Protocol::Apb4 && t.apb_bits == 32 && !t.back_to_back;
  const bool axi4 = t.protocol == Protocol::Axi4 && !t.axi3 && t.data_bits == 32;
  if ((!apb4 && !axi4) || (apb4 && t.outstanding != 1) || t.flows != 1 || t.read_only ||
      t.write_only || t.timeout || !t.pipes.empty() || t.exclusive.mode != ExclusiveAccess::None ||
      t.upsize)
    return bad("Only a plain 32-bit APB4 target of one burst, or a plain 32-bit AXI4 target, is "
               "observed");
  // A kept record rests on what a request carries; flags without it only on an AXI4 path.
  const bool flags_alone = !observer.keep_fields && plan.burst_user_bits != 0;
  if (flags_alone && !axi4)
    return bad("Only AXI4 targets keep a record's fields");
  for (const auto &row : observed.sources) {
    const auto &source = plan.sources[row.source];
    const bool plain_apb = source.apb == Protocol::Apb4 && source.apb_bits == 32;
    const bool plain_axi = !source.apb && source.protocol == Protocol::Axi4 && !source.ahb &&
                           !source.axi3 && source.engine_bits == 32;
    if ((!plain_apb && !plain_axi) || source.flows.size() > 1 || source.default_target)
      return bad("Only plain 32-bit APB4 and AXI4 sources reach an observed target");
    if (flags_alone && !plain_axi)
      return bad("Only AXI4 sources reach a target whose records keep their fields");
    if (observer.keep_fields && (axi4 ? !plain_axi : !plain_apb))
      return bad("A target that keeps a record's fields has sources of its protocol");
  }
  return Status::success({});
}
static Status check_fault_monitors(const NetworkPlan &plan) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + field, message));
  };
  if (plan.fault_monitors.empty())
    return Status::success({});
  if (plan.burst_record_bits && std::ranges::any_of(plan.sources, [](const SourcePlan &source) {
        return source.ahb || (source.protocol == Protocol::Axi4Lite && !source.apb);
      }))
    return bad("sources", "A logger of varying lengths needs APB and AXI4 sources");
  if (plan.plane_bits != 0)
    return bad("targets", "Networks with error logs have no mode-dependent mappings");
  std::set<unsigned> seen;
  std::set<std::string> observers;
  for (const auto &observer : plan.fault_monitors) {
    if (!observers.insert(observer.logger.name).second)
      return bad(observer.logger.name, "Error logs have distinct names");
    if (observer.service != plan.fault_monitors.front().service)
      return bad(observer.logger.name, "Error logs share one window");
    if (std::ranges::any_of(observer.logger.ignored_codes, [&](unsigned code) {
          return !plan.error_codes || (code != CodeTarget && code != CodeDecode);
        }))
      return bad("ignored_codes", "Only target and decode codes of a network with error codes");
    if (observer.observed.empty())
      return bad("targets", "A log watches at least one target");
    if (observer.logger.chain.size() >= logger_chain_limit)
      return bad(observer.logger.name, "A log has at most eight loggers");
    for (const auto &observed : observer.observed) {
      const auto &t = plan.targets[observed.target];
      if (!seen.insert(observed.target).second || t.service)
        return bad(t.name, "A log watches each data target once");
      if (auto checked = check_observed_target(plan, observer, observed); !checked)
        return checked;
    }
  }
  if (std::ranges::any_of(plan.links, [](const LinkPlan &link) { return !link.stages.empty(); }))
    return bad("links", "A network with error logs has no link pipes");
  return Status::success({});
}

// A definition's error logging as the plan's request: bank places follow the logs, and each bank
// takes the next route.
static InternalNetwork with_error_logging(const Definition &d, InternalNetwork internal) {
  const auto registers = std::ranges::find_if(d.targets, &Target::log_registers);
  if (registers == d.targets.end())
    return internal;
  internal.service_targets.emplace(
      registers->name,
      error_logger_bank(registers->name, 0, {1, 32, 0, 0, 2, 0}, d.error_logging.revision));
  const auto &logging = d.error_logging;
  internal.security.flags = logging.audit_flags;
  if (!logging.audit_flags.empty())
    for (const auto &i : d.initiators)
      internal.security.drivers[i.name] = i.audit_flags;
  const auto code_of = [](const std::string &name) {
    return name == "decode" ? unsigned{CodeDecode} : unsigned{CodeTarget};
  };
  unsigned route = 0;
  std::uint64_t place = 0;
  const auto next_place = [&](const ErrorLogger &logger) {
    const auto at = logger.offset.value_or(place);
    place = at + (1U << error_log_bank_address_bits);
    return at;
  };
  const auto split = [&](const ErrorLogger &logger, std::map<std::string, bool> &users,
                         std::map<std::string, bool> &audits) {
    for (const auto &[flag, value] : logger.match)
      if (std::ranges::find(logging.audit_flags, flag) != logging.audit_flags.end())
        audits[flag] = value;
      else
        users[flag] = value;
  };
  const auto codes = [&](const ErrorLogger &logger) {
    std::vector<unsigned> out;
    std::ranges::transform(logger.skip, std::back_inserter(out), code_of);
    return out;
  };
  for (const auto &log : logging.logs) {
    FaultMonitorRequest request{log.name, registers->name, log.watch, {}};
    request.keep_fields = log.whole_record;
    request.serial_word_bits = log.serial_word_bits;
    request.clock_domain = log.clock_domain.value_or(registers->clock_domain);
    if (request.clock_domain == registers->clock_domain)
      request.clock_domain.clear();
    const auto loggers = log.loggers.empty() ? std::vector<ErrorLogger>(1) : log.loggers;
    const auto &first = loggers.front();
    request.ignored_codes = codes(first);
    split(first, request.user_filter, request.security_filter);
    request.route = route++;
    request.base = next_place(first);
    request.forward = first.pass_on == ErrorLogger::PassOn::Always;
    for (std::size_t n = 1; n < loggers.size(); ++n) {
      const auto &next = loggers[n];
      FaultMonitorRequest::Chained chained;
      chained.ignored_codes = codes(next);
      split(next, chained.user_filter, chained.security_filter);
      chained.route = route++;
      chained.base = next_place(next);
      chained.forward = next.pass_on == ErrorLogger::PassOn::Always;
      request.chain.push_back(std::move(chained));
    }
    internal.fault_monitors.push_back(std::move(request));
  }
  return internal;
}

Result<NetworkPlan> build_network_plan(const Definition &d, const InternalNetwork &given) {
  const auto internal = with_error_logging(d, given);
  const auto options = d.transport.value_or(TransportOptions{});
  NetworkPlan plan;
  plan.name = d.name;
  plan.address_bits = d.address_width;
  plan.error_codes = options.response_codes == ResponseCodes::Distinct;
  plan.max_outstanding = options.max_outstanding;
  plan.id_table_entries = options.id_table_entries;
  plan.burst_data_bits = network_data_width(d);
  plan.burst_source_data_bits = source_data_width(d, plan.burst_data_bits);
  plan.transport_word_bytes =
      options.transport_word_bytes.value_or(std::min(plan.burst_data_bits / 8, 32U));
  plan.arbitration.policy =
      d.arbitration == ArbitrationPolicy::None || d.arbitration == ArbitrationPolicy::Auto
          ? treadle::Policy::RoundRobin
          : selector(d.arbitration);
  if (clocked_graph(d)) {
    plan.placement.emplace();
    plan.placement->crossings = d.clock_crossings;
    plan.placement->synchronizer_stages = d.synchronizer_stages;
    plan.placement->roots = d.clock_roots;
  }
  for (const auto &domain : d.clock_domains) {
    const auto reset = std::ranges::find(d.reset_ports, domain.reset_port, &ResetPort::name);
    const bool resynchronized =
        reset == d.reset_ports.end() || reset->release == ResetRelease::Resynchronized;
    DomainPlan dp{domain.name,       domain.clock_port,
                  domain.reset_port, resynchronized ? reset_release_stages : 0,
                  domain.period_ps,  domain.test_port.value_or(""),
                  domain.gated};
    dp.type = domain.clock_type;
    dp.ratio = domain.ratio.value_or(1);
    dp.enable_port = domain.enable_port.value_or("");
    dp.ungate_port = domain.ungate_port.value_or("");
    dp.clock_root = domain.clock_root.value_or("");
    dp.edge_parent = domain.edge_parent.value_or("");
    (plan.placement ? plan.placement->domains.emplace_back() : plan.domain) = dp;
  }
  plan.reset_ports = d.reset_ports;
  plan.test_ports = d.test_ports;
  plan.enable_ports = d.enable_ports;
  plan.ungate_ports = d.ungate_ports;
  plan.address_inputs = d.address_inputs;
  std::ranges::sort(plan.address_inputs, {}, &AddressInput::name);
  plan.address_selectors = d.address_selectors;
  std::ranges::sort(plan.address_selectors, {}, &AddressSelector::name);
  plan.plane_shift = d.address_width;
  plan.plane_bits = plane_bits(d);
  plan.clock_gating = d.clock_gating;
  plan.reset_coverage = d.reset_coverage;
  plan.cells = d.cells;
  // Past the definition checks only the switch graph routing and the capability rules reject;
  // anything else is a planning defect.
  try {
    plan_endpoints(plan, d);
    plan_graphs(plan, d);
    for (const auto &[name, bank] : internal.service_targets) {
      const auto served = std::ranges::find(plan.targets, name, &TargetPlan::name);
      if (served == plan.targets.end())
        return Result<NetworkPlan>::failure(
            error("NCL_INTERNAL_PLAN", "service." + name, "No such target"));
      served->service = ServicePlan{{ServiceBank{bank}}};
    }
    plan.observes_miss_address = !internal.fault_monitors.empty();
    if (plan.observes_miss_address && record_bytes_minus_one_bits(plan) > 2)
      plan.burst_record_bits = record_bytes_minus_one_bits(plan) + 1;
    auto status = plan_security_flags(plan, internal);
    if (!status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    status = plan_composition(plan);
    if (!status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    for (const auto &request : internal.fault_monitors) {
      auto observer = plan_fault_monitor(plan, request);
      if (!observer)
        return Result<NetworkPlan>::failure(observer.diagnostics());
      auto &service = plan.targets[observer.value().service].service;
      if (!service)
        return Result<NetworkPlan>::failure(error("NCL_INTERNAL_PLAN",
                                                  "error_logging." + request.name,
                                                  "The logger's bank needs a window"));
      // The first logger takes the target's bank; each further one is a branch of its own.
      auto &banks = service->banks;
      const auto first = plan.fault_monitors.empty();
      const auto name = first ? banks.front().plan.name : request.name;
      const auto route = request.route.value_or(banks.front().plan.route);
      const auto &shape = observer.value().logger.shape;
      auto placed = ServiceBank{error_logger_bank(name, route, shape, d.error_logging.revision),
                                request.base, static_cast<unsigned>(plan.fault_monitors.size())};
      if (first)
        banks.front() = std::move(placed);
      else
        banks.push_back(std::move(placed));
      observer.value().bank = static_cast<unsigned>(banks.size() - 1);
      if (!first && service->domain != request.clock_domain)
        return Result<NetworkPlan>::failure(error("NCL_UNSUPPORTED_TOPOLOGY",
                                                  "error_logging." + request.name,
                                                  "The loggers of a window share a domain"));
      service->domain = request.clock_domain;
      if (plan.placement && !request.clock_domain.empty()) {
        auto &logged = observer.value().logger;
        for (const auto &observed : observer.value().observed) {
          const auto &sender =
              plan.placement->objects.at("target/" + plan.targets[observed.target].name);
          if (sender == request.clock_domain)
            continue;
          logged.crossed = true;
        }
      }
      const auto branch = banks.back().branch;
      for (std::size_t n = 0; n < request.chain.size(); ++n)
        banks.push_back(
            ServiceBank{error_logger_bank(request.name + "_" + std::to_string(n + 1),
                                          request.chain[n].route, shape, d.error_logging.revision),
                        request.chain[n].base, branch});
      plan.fault_monitors.push_back(std::move(observer).value());
    }
    if (status = check_service_targets(plan); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    if (status = check_fault_monitors(plan); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    if (status = check_service_domains(plan); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    if (plan.placement)
      plan_clocking(plan);
    if (auto violations = check_capabilities(d, &plan); !violations.empty())
      return Result<NetworkPlan>::failure(std::move(violations));
    if (status = verify_axi4_exchange_obligations(plan, plan.clocked()); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    plan.dependency = build_dependency_graph(plan, dependency_facts(plan));
    plan.dependency_report = check_dependency_graph(plan.dependency);
    if (status = dependency_gate(plan.dependency, plan.dependency_report, dependency_rejects);
        !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
  } catch (const std::exception &e) {
    return Result<NetworkPlan>::failure(error("NCL_INTERNAL_PLAN", "plan", e.what()));
  }
  return Result<NetworkPlan>::success(std::move(plan));
}
} // namespace nocloom::detail
