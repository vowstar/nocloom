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
unsigned exclusive_keys(const Definition &d, const Target &t) {
  unsigned keys = 0;
  for (const auto &initiator : d.initiators) {
    if (full_axi(initiator.protocol) &&
        initiator.access.value_or(Access::ReadWrite) == Access::ReadWrite &&
        connects(d, initiator.name, t.name)) {
      keys += 1U << std::min(4U, initiator.id_width.value_or(4));
      if (keys >= 16)
        return 16;
    }
  }
  return keys;
}
unsigned exclusive_reservation_limit(const Definition &d, const Target &t) {
  if (t.exclusive_owner_flags.empty())
    return exclusive_keys(d, t);
  return 1U << std::min<std::size_t>(4, t.exclusive_owner_flags.size());
}
// APB4 and APB3 initiators: single transfers carried as AXI4-Lite beats of the 32-bit network.
bool apb_initiator(const Initiator &initiator) {
  return initiator.protocol == Protocol::Apb4 || initiator.protocol == Protocol::Apb3;
}
bool full_axi(std::optional<Protocol> protocol) {
  return protocol.value_or(Protocol::Axi4) == Protocol::Axi4 || protocol == Protocol::Axi3;
}
bool axi_response_protocol(std::optional<Protocol> protocol) {
  return full_axi(protocol) || protocol == Protocol::Axi4Lite;
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

// Compare predicates in reverse input declaration order, then by their values.
static std::vector<const AddressSelector *> ordered_selectors(const Definition &d) {
  std::map<std::string, std::size_t> position;
  for (std::size_t i = 0; i < d.address_inputs.size(); ++i)
    position.emplace(d.address_inputs[d.address_inputs.size() - i - 1].name, i);
  const auto predicate = [&](const AddressSelector *selector) {
    std::vector<std::pair<std::size_t, std::uint32_t>> terms;
    for (const auto &[name, value] : selector->equals) {
      const auto input = position.find(name);
      // Layout checking can run before validation of the input names.
      terms.emplace_back(input == position.end() ? position.size() : input->second, value);
    }
    std::ranges::sort(terms);
    return terms;
  };
  std::vector<const AddressSelector *> selectors;
  std::ranges::transform(d.address_selectors, std::back_inserter(selectors),
                         [](const auto &selector) { return &selector; });
  std::ranges::stable_sort(selectors, {}, predicate);
  return selectors;
}

std::vector<std::map<std::string, bool>> mode_planes(const Definition &d) {
  std::set<std::map<std::string, bool>> all;
  for (const auto &t : d.targets)
    for (const auto &m : t.mappings)
      if (!m.when.empty())
        all.insert(m.when);
  if (all.size() < 2)
    return {all.begin(), all.end()};
  // A plane's number affects the number of address bits a target stores.
  const auto selectors = ordered_selectors(d);
  std::map<std::string, unsigned> position;
  for (unsigned i = 0; i < selectors.size(); ++i)
    position.emplace(selectors[i]->name, i);
  std::vector<std::map<std::string, bool>> planes(all.begin(), all.end());
  // Layout checking can precede validation of selector names.
  if (std::ranges::any_of(planes, [&](const auto &condition) {
        return std::ranges::any_of(
            condition, [&](const auto &term) { return !position.contains(term.first); });
      }))
    return planes;
  const auto key = [&](const std::map<std::string, bool> &condition) {
    std::vector<std::pair<unsigned, bool>> terms;
    for (const auto &[name, enabled] : condition)
      terms.emplace_back(position.at(name), enabled);
    std::ranges::sort(terms);
    return terms;
  };
  std::ranges::sort(planes, {}, key);
  return planes;
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
      // A conditional mapping lies in its plane above the global address space.
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
  if (mode.slice != PipeSlice::None)
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
                    {pipes.response.backward_stage() ? PipeSlice::Backward : PipeSlice::None});
  if (s.qos.picks() && pipes.packets.slice != PipeSlice::None) {
    endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/packets", pipes.packets);
  } else {
    for (const auto *direction : {"write", "read"})
      endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/packets/" + direction, pipes.packets);
  }
  endpoint_pipe(s.pipes, "source/" + s.name + "/pipe/issue",
                {pipes.issue ? PipeSlice::Forward : PipeSlice::None});
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
    s.slverr_only = initiator.slverr_only.value_or(false);
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
    s.ahb = initiator.protocol == Protocol::AhbLite;
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
    s.split_narrow = initiator.narrow_bursts == NarrowBursts::MergeModifiable;
    s.pooled = !apb;
    plan_source_pipes(s, initiator);
    s.outstanding = initiator.max_outstanding.value_or(options.max_outstanding);
    s.engine_slots = s.outstanding;
    if (s.qos.picks() && s.pooled &&
        (initiator.pipes.packets.slice != PipeSlice::None || initiator.pipes.issue)) {
      const unsigned buffers = initiator.pipes.packets.slot_count();
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
    endpoint_pipe(x.pipes, stem + "dispatch", {t.pipes.dispatch ? PipeSlice::Forward : PipeSlice::None});
    // The forward stage of AW, W and AR is the outlet's own address and write register.
    for (const auto *channel : {"aw", "w", "ar"})
      if (one_way_keeps(x.read_only, x.write_only, channel))
        endpoint_pipe(x.pipes, stem + channel, {t.pipes.request.backward_stage() ? PipeSlice::Backward : PipeSlice::None});
    endpoint_pipe(x.pipes, stem + "response", t.pipes.response);
    x.fixed_single = t.fixed_bursts == FixedBursts::Single;
    x.timeout = t.timeout;
    x.fixed_length = t.undefined_length_bursts == false;
    x.hresp_bits = t.hresp_width == 2U ? 2 : 1;
    // A narrow APB target keeps the network word as its engine width.
    x.data_bits = plan.burst_data_bits;
    if (protocol_caps(x.protocol).apb && t.data_width && *t.data_width < 32)
      x.apb_bits = *t.data_width;
    else
      x.data_bits = t.data_width.value_or(plan.burst_data_bits);
    if (t.read_interleaving_disabled.value_or(false))
      x.read_interleave_size = 0U;
    else if (t.read_interleaving_disabled || t.read_block_words)
      x.read_interleave_size = t.read_block_words.value_or(1) * (x.data_bits / 8);
    plan.per_target = plan.per_target || x.data_bits != plan.burst_data_bits;
    plan.per_endpoint = plan.per_endpoint || plan.per_target;
    x.outstanding = std::max(t.max_outstanding.value_or(1), 1U);
    // With several bursts in flight a write-only outlet's BREADY looks for the slot of BID; a
    // backward pipe keeps it registered (the others park a B instead, Axi4OutletProfile).
    if (x.protocol == Protocol::Axi4 && x.outstanding > 1 && x.write_only)
      endpoint_pipe(x.pipes, stem + "b", {PipeSlice::Backward});
    if (const auto mode = t.exclusive.value_or(ExclusiveAccess::None);
        mode != ExclusiveAccess::None) {
      x.exclusive.mode = mode;
      auto owners = t.exclusive_owner_flags;
      std::ranges::sort(owners);
      std::ranges::transform(
          owners, std::back_inserter(x.exclusive.id_flags), [&](const std::string &flag) {
            return static_cast<unsigned>(std::ranges::find(options.user_flags, flag) -
                                         options.user_flags.begin());
          });
      if (mode == ExclusiveAccess::Network) {
        const auto monitor = t.exclusive_monitor.value_or(ExclusiveMonitorOptions{});
        x.exclusive.monitors = monitor.reservations.value_or(exclusive_reservation_limit(d, t));
        x.exclusive.granule = monitor.granule_bytes;
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
      if (mode.slice != PipeSlice::None) {
        planned = mode;
        static constexpr std::array names{"", "forward", "backward", "full", "half"};
        record[key] = {{"slice", names[static_cast<unsigned>(mode.slice)]}};
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

// A log's targets share a clock domain and a network has at most 64 domains, so 64 logs watch
// every target. A logger's place in its chain fills the frame header beside the register index.
static constexpr std::size_t log_limit = 64, logger_chain_limit = 1U << register_key_bits;
// A window spans at most 1 MiB, with its banks anywhere in it. Every bank of the most logs with
// the longest chains fills 128 KiB of it.
static constexpr std::uint64_t window_limit = 1ULL << 20;
// A chain is checked before its banks, whose keys hold a place in the chain.
static Status check_log_chains(const NetworkPlan &plan) {
  const auto longer = std::ranges::find_if(plan.error_logs, [](const ErrorLogPlan &log) {
    return log.logger.chain.size() >= logger_chain_limit;
  });
  if (longer != plan.error_logs.end())
    return Status::failure(error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + longer->logger.name,
                                 "A log has at most 32 loggers"));
  return Status::success({});
}
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
  if (plan.burst_data_bits != 32 || plan.mapped() || plan.has_scheduler())
    return bad("network", "A window needs a 32-bit network without scheduling levels");
  for (const auto &t : plan.targets) {
    if (!t.service)
      continue;
    if (t.service->banks.back().branch >= log_limit)
      return bad(t.name, "A window has at most 64 logs");
    std::set<std::uint64_t> blocks;
    for (const auto &bank : t.service->banks) {
      if (auto checked = check_register_bank(bank.plan); !checked)
        return checked;
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
    const auto outside = [size](const ServiceBank &bank) {
      const auto bytes = std::uint64_t{1} << bank.plan.address_bits;
      return size < bytes || bank.base > size - bytes;
    };
    if (size > window_limit || t.mappings.front().window.base % size != 0 ||
        std::ranges::any_of(t.service->banks, outside))
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
      return d.type == DomainEdges::Direct && d.edge_parent.empty();
    };
    if (!plain(*a) || !plain(*z) || a->clock_port == z->clock_port ||
        a->reset_port != z->reset_port)
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
  for (const auto &observer : plan.error_logs) {
    if (!observer.logger.crossed)
      continue;
    const auto &domain = plan.targets[observer.service].service->domain;
    if (observer.whole_record)
      return bad(observer.logger.name, "A crossed log does not keep fields");
    // A crossed log needs both effective periods to size its crossings.
    const auto period_of = [&](const std::string &name) {
      const auto &all = plan.placement->domains;
      const auto at = std::ranges::find(all, name, &DomainPlan::name);
      return at == all.end() ? std::nullopt : at->effective_period_ps;
    };
    if (!period_of(domain))
      return bad(observer.logger.name, "A crossed log's clocks need known periods");
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
        return bad(t.name, "A crossed log's clocks need known periods");
      if (auto status = apart(t.name, own, domain); !status)
        return status;
    }
  }
  return Status::success({});
}

// Logs watch 32-bit APB4 or AXI4 targets reached by 32-bit sources without link pipes.
// Each target admits one burst. Audit flags fit in one record word.
static constexpr std::size_t audit_flags_limit = 32;
// Sets a network's audit flags, in their order, and each source's drivers of them.
static Status plan_audit_flags(NetworkPlan &plan, const InternalNetwork &internal) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(
        error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging.audit_flags." + field, message));
  };
  const auto &request = internal.audit;
  if (request.flags.empty() && request.drivers.empty())
    return Status::success({});
  if (internal.error_logs.empty())
    return bad("flags", "Audit flags serve an error log");
  const auto &flags = request.flags;
  const std::set<std::string> distinct(flags.begin(), flags.end());
  if (flags.empty() || flags.size() > audit_flags_limit || distinct.size() != flags.size() ||
      !std::ranges::all_of(flags, valid_name))
    return bad("flags", "One to 32 distinct audit flags with valid names");
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
      source.audit_flags.push_back(bit->second);
    }
  }
  if (std::ranges::any_of(plan.targets, [](const TargetPlan &t) { return t.upsize.has_value(); }))
    return bad("targets", "A network with audit flags has no target wider than its transport");
  plan.audit_flags = flags;
  plan.burst_audit_bits = static_cast<unsigned>(plan.audit_flags.size());
  return Status::success({});
}
// One observed target of an error logger, and the sources that reach it.
static Status check_observed_target(const NetworkPlan &plan, const ErrorLogPlan &observer,
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
  const bool flags_alone = !observer.whole_record && plan.burst_user_bits != 0;
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
    if (observer.whole_record && (axi4 ? !plain_axi : !plain_apb))
      return bad("A target that keeps a record's fields has sources of its protocol");
  }
  return Status::success({});
}
static Status check_error_logs(const NetworkPlan &plan) {
  const auto bad = [](const std::string &field, const std::string &message) {
    return Status::failure(error("NCL_UNSUPPORTED_TOPOLOGY", "error_logging." + field, message));
  };
  if (plan.error_logs.empty())
    return Status::success({});
  if (plan.burst_record_bits && std::ranges::any_of(plan.sources, [](const SourcePlan &source) {
        return source.ahb || (source.protocol == Protocol::Axi4Lite && !source.apb);
      }))
    return bad("sources", "A logger of varying lengths needs APB and AXI4 sources");
  if (plan.plane_bits != 0)
    return bad("targets", "Networks with error logs have no conditional mappings");
  std::set<unsigned> seen;
  std::set<std::string> observers;
  for (const auto &observer : plan.error_logs) {
    if (!observers.insert(observer.logger.name).second)
      return bad(observer.logger.name, "Error logs have distinct names");
    if (observer.service != plan.error_logs.front().service)
      return bad(observer.logger.name, "Error logs share one window");
    if (std::ranges::any_of(observer.logger.skipped_classes,
                            [](unsigned one) { return one != ClassTarget && one != ClassDecode; }))
      return bad("skip", "A logger skips target or decode");
    if (observer.observed.empty())
      return bad("targets", "A log watches at least one target");
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

namespace {
constexpr unsigned log_bank_bytes = 1U << error_log_bank_address_bits;
constexpr unsigned log_bank_words = log_bank_bytes / 4;
constexpr unsigned all_bank_words = (1U << log_bank_words) - 1;
constexpr unsigned control_words = 1U | (1U << error_log_state_word);

// The same cut routes that the initiator decodes, translated back to target local addresses.
struct LogWindow {
  const MappingPlan &mapping;
  std::vector<const SourcePlan *> sources;

  unsigned visible(std::uint64_t local, std::uint64_t limit) const {
    unsigned result = 0;
    for (unsigned word = 0; word < log_bank_words; ++word) {
      const auto at = local + 4 * word;
      if (at < mapping.local_base || at - mapping.local_base >= mapping.window.size)
        continue;
      const auto address = mapping.window.base + (at - mapping.local_base);
      if (address + 4 > limit)
        continue;
      const auto after =
          std::ranges::upper_bound(mapping.routes, address, {}, &AddressWindow::base);
      if (after != mapping.routes.begin()) {
        const auto &route = *std::prev(after);
        if (address + 4 <= route.base + route.size)
          result |= 1U << word;
      }
    }
    return result;
  }

  bool covered(std::uint64_t limit) const {
    auto next = mapping.window.base;
    const auto end = std::min(limit, next + mapping.window.size);
    for (const auto &route : mapping.routes) {
      if (next >= end)
        return false;
      if (route.base > next)
        return true;
      next = route.base + route.size;
    }
    return next < end;
  }

  void describe(ServiceBank &bank) const {
    const auto narrowest = std::ranges::min_element(sources, {}, &SourcePlan::address_bits);
    if (narrowest != sources.end() &&
        visible(bank.base, 1ULL << (*narrowest)->address_bits) == all_bank_words)
      return;
    std::map<std::string, std::vector<unsigned>> missing;
    bank.state_reachable = false;
    for (const auto *source : sources) {
      const auto words = visible(bank.base, 1ULL << source->address_bits);
      bank.state_reachable |= (words & (1U << error_log_state_word)) != 0;
      auto &offsets = missing[source->name];
      for (unsigned word = 0; word < log_bank_words; ++word)
        if (!(words & (1U << word)))
          offsets.push_back(4 * word);
    }
    bank.unreachable_words = std::move(missing);
  }
};

LogWindow log_window(const NetworkPlan &plan, const TargetPlan &target) {
  LogWindow window{target.mappings.front(), {}};
  for (const auto index : reaching_sources(plan, target.name))
    window.sources.push_back(&plan.sources[index]);
  return window;
}

void place_log_banks(const NetworkPlan &plan, InternalNetwork &internal, const TargetPlan &target) {
  // check_service_targets rejects both an invalid mapping count and an oversized window.
  if (target.mappings.size() != 1 || target.mappings.front().window.size > window_limit)
    return;
  const auto window = log_window(plan, target);
  if (window.sources.empty())
    return;

  // The routes are shared. Address width alone limits how much of them each source can reach.
  const auto [narrowest, widest] =
      std::ranges::minmax_element(window.sources, {}, &SourcePlan::address_bits);
  const auto first_limit = 1ULL << (*narrowest)->address_bits;
  const auto last_limit = 1ULL << (*widest)->address_bits;
  if (!window.covered(last_limit))
    return;

  std::vector<std::uint64_t *> automatic;
  std::set<std::uint64_t> reserved;
  const auto collect = [&](auto &logger) {
    if (logger.automatic_base)
      automatic.push_back(&logger.base);
    else
      reserved.insert(logger.base);
  };
  for (auto &request : internal.error_logs)
    if (request.service == target.name) {
      collect(request);
      for (auto &logger : request.chain)
        collect(logger);
    }

  if (automatic.empty())
    return;
  std::vector<std::uint64_t> complete, partial;
  for (std::uint64_t at = 0; at + log_bank_bytes <= window.mapping.window.size;
       at += log_bank_bytes) {
    if (reserved.contains(at))
      continue;
    if (window.visible(at, first_limit) == all_bank_words) {
      complete.push_back(at);
      if (complete.size() == automatic.size())
        break;
    } else if ((window.visible(at, last_limit) & control_words) == control_words &&
               partial.size() < automatic.size()) {
      partial.push_back(at);
    }
  }

  // Leave the original placement intact unless the whole group fits.
  if (complete.size() + partial.size() < automatic.size())
    return;
  complete.insert(complete.end(), partial.begin(),
                  partial.begin() +
                      static_cast<std::ptrdiff_t>(automatic.size() - complete.size()));
  std::ranges::sort(complete);
  for (std::size_t i = 0; i < automatic.size(); ++i)
    *automatic[i] = complete[i];
}
} // namespace

// A definition's error logging as the plan's request: bank places follow the logs.
static InternalNetwork with_error_logging(const Definition &d, InternalNetwork internal) {
  const auto registers = std::ranges::find_if(d.targets, &Target::log_registers);
  if (registers == d.targets.end())
    return internal;
  internal.service_targets.emplace(registers->name,
                                   error_logger_bank(registers->name, {1, 32, 0, 0, 2, 0},
                                                     {.given_revision = d.error_logging.revision}));
  const auto &logging = d.error_logging;
  internal.audit.flags = logging.audit_flags;
  if (!logging.audit_flags.empty())
    for (const auto &i : d.initiators)
      internal.audit.drivers[i.name] = i.audit_flags;
  const auto class_of = [](const std::string &name) {
    return name == "decode" ? unsigned{ClassDecode} : unsigned{ClassTarget};
  };
  std::uint64_t position = 0;
  const auto next_base = [&](const ErrorLogger &logger) {
    const auto base = logger.offset.value_or(position);
    position = base + log_bank_bytes;
    return base;
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
    std::ranges::transform(logger.skip, std::back_inserter(out), class_of);
    return out;
  };
  for (const auto &log : logging.logs) {
    ErrorLogRequest request{log.name, registers->name, log.watch, {}};
    request.whole_record = log.whole_record;
    request.capture_class = logging.capture_class;
    request.serial_word_bits = log.serial_word_bits;
    request.clock_domain = log.clock_domain.value_or(registers->clock_domain);
    if (request.clock_domain == registers->clock_domain)
      request.clock_domain.clear();
    const auto loggers = log.loggers.empty() ? std::vector<ErrorLogger>(1) : log.loggers;
    const auto &first = loggers.front();
    request.skipped_classes = codes(first);
    split(first, request.user_match, request.audit_match);
    request.base = next_base(first);
    request.automatic_base = !first.offset;
    request.pass_always = first.pass_on == ErrorLogger::PassOn::Always;
    for (std::size_t n = 1; n < loggers.size(); ++n) {
      const auto &next = loggers[n];
      ErrorLogRequest::Chained chained;
      chained.skipped_classes = codes(next);
      split(next, chained.user_match, chained.audit_match);
      chained.base = next_base(next);
      chained.automatic_base = !next.offset;
      chained.pass_always = next.pass_on == ErrorLogger::PassOn::Always;
      request.chain.push_back(std::move(chained));
    }
    internal.error_logs.push_back(std::move(request));
  }
  return internal;
}

static void resolve_clock_roots(ClockPlacement &placement, const Definition &d) {
  auto &domains = placement.domains;
  for (auto &domain : domains) {
    domain.edge_ratio = domain.ratio;
    domain.qualified = domain.type == DomainEdges::Enabled;
    for (auto parent = domain.edge_parent; !parent.empty();) {
      const auto edge = std::ranges::find(domains, parent, &DomainPlan::name);
      domain.edge_ratio *= edge->ratio;
      domain.qualified |= edge->type == DomainEdges::Enabled;
      parent = edge->edge_parent;
    }
  }
  for (auto &root : placement.roots) {
    if (root.period_ps)
      continue;
    // A fixed chain with the smallest declared ratio supplies an untimed root.
    std::optional<std::size_t> first;
    for (std::size_t i = 0; i < domains.size(); ++i)
      if (domains[i].clock_root == root.name && !domains[i].qualified &&
          d.clock_domains[i].period_ps &&
          (!first || domains[i].edge_ratio < domains[*first].edge_ratio))
        first = i;
    if (!first)
      continue;
    root.period_ps = ClockPeriod{*d.clock_domains[*first].period_ps, domains[*first].edge_ratio};
  }
}

static Status resolve_clock_periods(ClockPlacement &placement, const Definition &d) {
  resolve_clock_roots(placement, d);
  auto &domains = placement.domains;
  std::vector<Diagnostic> errors;
  for (std::size_t i = 0; i < domains.size(); ++i) {
    auto &domain = domains[i];
    const std::optional<ClockPeriod> declared{d.clock_domains[i].period_ps};
    domain.period_declared = declared.has_value();
    domain.effective_period_ps = declared;
    const auto field = "clock_domains[" + std::to_string(i) + "].period_ps";
    const auto root = std::ranges::find(placement.roots, domain.clock_root, &ClockRootPlan::name);
    std::optional<ClockPeriod> minimum;
    if (root != placement.roots.end() && root->period_ps)
      minimum = *root->period_ps * domain.edge_ratio;
    for (auto edge = &domain; domain.qualified && !edge->edge_parent.empty();) {
      const auto parent = std::ranges::find(domains, edge->edge_parent, &DomainPlan::name);
      if (parent->qualified) {
        const auto &promise = d.clock_domains[parent - domains.begin()].period_ps;
        if (promise) {
          const auto bound = ClockPeriod{*promise} * (domain.edge_ratio / parent->edge_ratio);
          minimum = minimum ? std::max(*minimum, bound) : bound;
        }
      }
      edge = &*parent;
    }
    if (!minimum)
      continue;
    if (declared && (domain.qualified ? *declared < *minimum : *declared != *minimum))
      errors.push_back(error("NCL_CDC_PERIOD_RELATION", field,
                             "Declared clock period " + declared->text() + " ps " +
                                 (domain.qualified ? "is below minimum period "
                                                   : "differs from root-derived period ") +
                                 minimum->text() + " ps"));
    else
      domain.effective_period_ps = declared ? declared : minimum;
  }
  if (!errors.empty())
    return Status::failure(std::move(errors));
  return Status::success({});
}

Result<NetworkPlan> build_network_plan(const Definition &d, const InternalNetwork &given) {
  auto internal = with_error_logging(d, given);
  const auto options = d.transport.value_or(TransportOptions{});
  if (options.compact_failure_responses) {
    const auto source = std::ranges::find_if(d.initiators, [](const Initiator &s) {
      return axi_response_protocol(s.protocol) && !s.slverr_only.value_or(false);
    });
    if (source != d.initiators.end())
      return Result<NetworkPlan>::failure(
          error("NCL_UNSUPPORTED_TOPOLOGY", "transport.compact_failure_responses",
                "AXI initiator " + source->name + " distinguishes DECERR from SLVERR"));
    const auto log = std::ranges::find_if(internal.error_logs, &ErrorLogRequest::capture_class);
    if (log != internal.error_logs.end())
      return Result<NetworkPlan>::failure(
          error("NCL_UNSUPPORTED_TOPOLOGY", "transport.compact_failure_responses",
                "Error log " + log->name + " records failure classes"));
  }
  NetworkPlan plan;
  plan.name = d.name;
  plan.address_bits = d.address_width;
  plan.failure_classes = !options.compact_failure_responses;
  plan.max_outstanding = options.max_outstanding;
  plan.id_table_entries = options.id_table_entries;
  plan.burst_data_bits = network_data_width(d);
  plan.burst_source_data_bits = source_data_width(d, plan.burst_data_bits);
  plan.transport_word_bytes =
      options.transport_word_bytes.value_or(std::min(plan.burst_data_bits / 8, 32U));
  plan.arbitration.policy = selector(d.arbitration);
  if (clocked_graph(d)) {
    plan.placement.emplace();
    plan.placement->crossings = d.clock_crossings;
    plan.placement->synchronizer_stages = d.synchronizer_stages;
    for (const auto &root : d.clock_roots)
      plan.placement->roots.push_back(
          {root.name, root.clock_port, std::optional<ClockPeriod>{root.period_ps}});
  }
  for (const auto &domain : d.clock_domains) {
    DomainPlan dp{domain.name,
                  domain.clock_port,
                  domain.reset_port,
                  domain.reset_synchronizer ? reset_release_stages : 0,
                  domain.test_port.value_or(""),
                  domain.gated};
    dp.type = domain.ratio         ? DomainEdges::Divided
              : domain.enable_port ? DomainEdges::Enabled
                                   : DomainEdges::Direct;
    dp.ratio = domain.ratio.value_or(1);
    dp.enable_port = domain.enable_port.value_or("");
    dp.ungate_port = domain.ungate_port.value_or("");
    dp.clock_root = domain.clock_root.value_or("");
    dp.edge_parent = domain.edge_parent.value_or("");
    (plan.placement ? plan.placement->domains.emplace_back() : plan.domain) = dp;
  }
  if (plan.placement)
    if (auto periods = resolve_clock_periods(*plan.placement, d); !periods)
      return Result<NetworkPlan>::failure(periods.diagnostics());
  plan.reset_ports = d.reset_ports;
  plan.test_ports = d.test_ports;
  plan.enable_ports = d.enable_ports;
  plan.ungate_ports = d.ungate_ports;
  plan.address_inputs = d.address_inputs;
  std::ranges::reverse(plan.address_inputs);
  for (const auto *selector : ordered_selectors(d))
    plan.address_selectors.push_back(*selector);
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
    plan.observes_miss_address = !internal.error_logs.empty();
    if (plan.observes_miss_address && record_last_offset_bits(plan) > 2)
      plan.burst_record_bits = record_last_offset_bits(plan) + 1;
    auto status = plan_audit_flags(plan, internal);
    if (!status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    status = plan_composition(plan);
    if (!status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    for (auto &request : internal.error_logs) {
      auto observer = plan_error_log(plan, request);
      if (!observer)
        return Result<NetworkPlan>::failure(observer.diagnostics());
      auto &target = plan.targets[observer.value().service];
      auto &service = target.service;
      if (!service)
        return Result<NetworkPlan>::failure(error("NCL_INTERNAL_PLAN",
                                                  "error_logging." + request.name,
                                                  "The logger's bank needs a window"));
      const auto first = plan.error_logs.empty();
      // Service validation allows only one register window for all logs.
      if (first)
        place_log_banks(plan, internal, target);
      // The first logger takes the target's bank; each further one is a branch of its own.
      auto &banks = service->banks;
      const auto name = first ? banks.front().plan.name : request.name;
      const auto &shape = observer.value().logger.shape;
      // A log is a branch of the window's switch, a logger a place in the log's chain.
      const auto log = static_cast<unsigned>(plan.error_logs.size());
      const auto place = [&](std::size_t logger) {
        return ErrorLogPlace{log, static_cast<unsigned>(logger), logger < request.chain.size(),
                             request.whole_record, d.error_logging.revision};
      };
      auto placed = ServiceBank{error_logger_bank(name, shape, place(0)), request.base, log};
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
      for (std::size_t n = 0; n < request.chain.size(); ++n)
        banks.push_back(ServiceBank{
            error_logger_bank(request.name + "_" + std::to_string(n + 1), shape, place(n + 1)),
            request.chain[n].base, log});
      plan.error_logs.push_back(std::move(observer).value());
    }
    if (status = check_log_chains(plan); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    if (status = check_service_targets(plan); !status)
      return Result<NetworkPlan>::failure(status.diagnostics());
    if (!plan.error_logs.empty()) {
      auto &target = plan.targets[plan.error_logs.front().service];
      const auto window = log_window(plan, target);
      for (auto &bank : target.service->banks)
        window.describe(bank);
    }
    if (status = check_error_logs(plan); !status)
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
