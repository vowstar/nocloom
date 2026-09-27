#include "cone_walk.hpp"
#include "diagnostic.hpp"
#include "exchange_composition.hpp"
#include "ir_cone.hpp"
#include "network_plan_profiles.hpp"
#include <algorithm>
#include <set>

namespace nocloom::detail {
namespace {
// Binding suffix of a switch arbiter's state word: the grant owner and held
// flag, a round robin or rotating cursor (".pointer" in imported switches),
// or a FIFO order bit ".order<i>_<j>".
bool arbiter_state_binding(std::string_view suffix) {
  // A cascade group arbiter ".group<g>" belongs to its output.
  if (const auto dot = suffix.find('.', 1); suffix.starts_with(".group") && dot != suffix.npos)
    suffix.remove_prefix(dot);
  if (suffix == ".cursor" || suffix == ".owner" || suffix == ".held" || suffix == ".pointer")
    return true;
  const auto digits = [](std::string_view text) {
    return !text.empty() && text.find_first_not_of("0123456789") == std::string_view::npos;
  };
  // A level round robin's last winner of each level.
  if (suffix.starts_with(".level_winner"))
    return digits(suffix.substr(13));
  if (!suffix.starts_with(".order"))
    return false;
  suffix.remove_prefix(6);
  const auto split = suffix.find('_');
  return split != std::string_view::npos && digits(suffix.substr(0, split)) &&
         digits(suffix.substr(split + 1));
}
Diagnostic binding_error(std::string message) {
  return error("NCL_EXCHANGE_BINDING", "exchange.hardware", std::move(message));
}
// The target of each destination index of a source: a routing window in target order
// (initiator_profile), then local completion (target index targets.size()).
std::vector<unsigned> route_owners(const NetworkPlan &plan) {
  std::vector<unsigned> owner;
  for (unsigned i = 0; i < plan.targets.size(); ++i)
    owner.insert(owner.end(), plan.targets[i].routes().size(), i);
  owner.push_back(static_cast<unsigned>(plan.targets.size()));
  return owner;
}
// The target of a decoded destination index, by the owner of each index.
std::optional<unsigned> owning(const std::vector<unsigned> &owner,
                               std::optional<std::uint64_t> index) {
  return index && *index < owner.size() ? std::optional{owner[*index]} : std::nullopt;
}
// The network's decision for one address of a source's flow, from the target index its entry
// decodes (message.hpp point 16) and the switches' own logic on that index.
std::optional<unsigned> decision(const ExchangeHardware &h, ConeEvaluator &evaluate,
                                 const std::string &source, std::uint64_t address, unsigned flow) {
  const auto &g = h.plan.exchange.request;
  const auto entry = std::ranges::find(g.graph.entries, source, &SwitchAttachment::endpoint);
  if (entry == g.graph.entries.end())
    return std::nullopt;
  std::uint64_t route = 0;
  if (h.plan.target_bits) {
    const auto at = "source/" + source + "/write.route_";
    const auto t = h.bindings.find(at + "target"), a = h.bindings.find(at + "address");
    if (t == h.bindings.end() || a == h.bindings.end())
      return std::nullopt;
    const auto index = evaluate(t->second.index, {}, {}, {{a->second.index, address}});
    if (!index)
      return std::nullopt;
    route = *index;
  }
  std::string node = entry->node, key = "endpoint/" + source;
  for (std::size_t hop = 0; hop <= g.graph.nodes.size(); ++hop) {
    const SwitchOutputPlan *chosen = nullptr;
    unsigned selected = 0;
    for (const auto &o : g.outputs) {
      if (o.node != node || std::ranges::find(o.inputs, key) == o.inputs.end())
        continue;
      const auto at =
          "decode/graph/req/" + node + ">" + (o.endpoint ? "endpoint/" : "node/") + o.output;
      const auto s = h.bindings.find(at + ".select/" + key),
                 r = h.bindings.find(at + ".route/" + key),
                 from = h.bindings.find(at + ".source/" + key),
                 path = h.bindings.find(at + ".path/" + key);
      if (s == h.bindings.end() || r == h.bindings.end())
        return std::nullopt;
      // A switch input whose routes toward one target split also compares the source write flow
      // and, with a route path, the initiator flow.
      ConeEvaluator::Words pinned{{r->second.index, route}};
      if (from != h.bindings.end())
        pinned[from->second.index] =
            2 * static_cast<std::uint64_t>(
                    std::ranges::find(h.plan.sources, source, &SourcePlan::name) -
                    h.plan.sources.begin());
      if (path != h.bindings.end())
        pinned[path->second.index] = flow;
      const auto hit = evaluate(s->second.index, {}, {}, pinned);
      if (!hit)
        return std::nullopt;
      if (*hit) {
        chosen = &o;
        ++selected;
      }
    }
    if (selected != 1)
      return std::nullopt;
    if (chosen->endpoint)
      return h.plan.target_index(chosen->output);
    key = "node/" + node;
    node = chosen->output;
  }
  return std::nullopt;
}
// The target the request switches select for an address, alike for both directions and every
// flow of a mask (all of a source's flows outside a window: a miss takes one route); none when
// they differ.
struct Decisions {
  const ExchangeHardware &h;
  ConeEvaluator &select;
  const SourcePlan &sp;
  std::map<std::pair<std::uint64_t, std::uint64_t>, std::optional<unsigned>> decided = {};
  std::optional<unsigned> operator()(std::uint64_t address, std::uint64_t flows) {
    const auto [it, fresh] = decided.try_emplace({address, flows});
    if (!fresh)
      return it->second;
    std::optional<std::optional<unsigned>> out;
    for (unsigned flow = 0; flow < std::max<std::size_t>(1, sp.flows.size()); ++flow)
      if (flows >> flow & 1) {
        const auto here = decision(h, select, sp.name, address, flow);
        out = !out || *out == here ? here : std::nullopt;
      }
    return it->second = out.value_or(std::nullopt);
  }
};
// The windows a source reaches, each with its flows that reach it.
std::vector<std::pair<AddressWindow, std::uint64_t>> reached_windows(const NetworkPlan &plan,
                                                                     const SourcePlan &sp) {
  std::vector<std::pair<AddressWindow, std::uint64_t>> out;
  for (const auto &r : plan.exchange.request.routes)
    if (r.entry == sp.name) {
      const auto flows = reaching_flows(plan, sp.name, r.exit, r.target_path.value_or(0));
      std::ranges::transform(plan.target(r.exit).routes(r.target_path), std::back_inserter(out),
                             [&](const AddressWindow &w) { return std::pair{w, flows}; });
    }
  return out;
}
// Where a source's misses go: the default target when the switches take its miss window on the
// miss route, the local destination (targets) without one.
std::optional<unsigned> miss_destination(const NetworkPlan &plan, const SourcePlan &sp,
                                         Decisions &network) {
  const auto at = miss_route(plan, sp.name);
  if (!at)
    return static_cast<unsigned>(plan.targets.size());
  if (network(at->window.base, std::uint64_t{1} << at->source_path) == at->target)
    return at->target;
  return std::nullopt;
}
} // namespace

// Self-check over the composed hardware of every tracked source: for each
// probe start address, the source's destination decode (evaluated on its
// descriptor registers) equals the target the request switches select when
// their own selection logic is evaluated with that address on their route
// operand. Admission failures (exclusive, a 4 KiB crossing, an illegal
// WRAP) decode to the local destination and never reach a switch.
Status verify_network_decode(const ExchangeHardware &h) {
  const auto &plan = h.plan;
  auto fail = [](std::string message) {
    return Status::failure(
        error("NCL_EXCHANGE_DESTINATION_DECODE", "exchange.sources", std::move(message)));
  };
  const auto owner = route_owners(plan);
  ConeEvaluator evaluate(h.ir), select(h.ir);
  const std::uint64_t local_destination = owner.size() - 1;
  const std::uint64_t limit = std::uint64_t{1} << plan.burst_address_bits;
  std::set<std::uint64_t> probes{0, limit - 1};
  for (const auto &entry : plan.decode) {
    const auto &w = entry.route;
    probes.insert(w.base);
    probes.insert(w.base + w.size - 1);
    if (w.base)
      probes.insert(w.base - 1);
    if (w.base + w.size < limit)
      probes.insert(w.base + w.size);
  }
  auto bound = [&](const std::string &key) -> std::optional<std::size_t> {
    const auto it = h.bindings.find(key);
    return it == h.bindings.end() ? std::nullopt : std::optional{it->second.index};
  };
  auto state_of = [&](const std::string &key) -> std::optional<std::size_t> {
    const auto v = bound(key);
    if (!v || h.ir.values.at(*v).op != Op::State)
      return std::nullopt;
    return h.ir.values.at(*v).state.index;
  };
  for (const auto &sp : plan.sources) {
    const auto &source = sp.name;
    if (sp.outstanding < 2)
      continue;
    Decisions network{h, select, sp};
    const auto reached = reached_windows(plan, sp);
    // Flows differ in what they reach: the decode takes the flow of a pinned AxID.
    const bool by_flow =
        std::ranges::any_of(reached, [&](const auto &x) { return x.second != all_flows(sp); });
    std::optional<std::optional<unsigned>> missed;
    // A one-direction or AXI4-Lite (no AxLOCK) source issues no exclusive access.
    const auto windows = sp.read_only || sp.write_only || sp.protocol == Protocol::Axi4Lite
                             ? std::vector<ExclusiveWindow>{}
                             : exclusive_windows(plan, source);
    for (const bool write : {true, false}) {
      const std::string p = write ? "wr" : "rr";
      const auto base = "source/" + source + "/";
      const auto dest = bound(base + p + ".dest");
      const auto addr = state_of(base + p + ".desc.addr"),
                 burst = state_of(base + p + ".desc.burst");
      const auto len = state_of(base + p + ".desc.len"), lock = state_of(base + p + ".desc.lock");
      const auto size = state_of(base + p + ".desc.size"), engine = state_of(base + p + ".state");
      const auto id = state_of(base + p + ".desc.id");
      if (!dest || !addr || !burst || !len || !lock || !size || !engine || (by_flow && !id))
        return fail("Source " + source + ": missing the " + p + " destination decode anchors");
      // A mode-dependent decode takes the plane of the transport address (mode_planes).
      const auto plane = bound(base + p + ".plane_now");
      std::uint64_t ident = 0;
      auto decode = [&](std::uint64_t address, std::uint64_t length, std::uint64_t kind,
                        std::uint64_t locked) {
        ConeEvaluator::Words words{{*addr, address}, {*burst, kind}, {*len, length},
                                   {*lock, locked},  {*size, 2},     {*engine, 1}},
            pinned;
        if (by_flow)
          words[*id] = ident;
        if (plane) {
          pinned[*plane] = address >> plan.plane_shift;
          words[*addr] = address & ((std::uint64_t{1} << plan.plane_shift) - 1);
        }
        return evaluate(*dest, words, {}, pinned);
      };
      for (unsigned flow = 0; flow < (by_flow ? sp.flows.size() : 1); ++flow) {
        // An AxID of the flow.
        while (by_flow && id_flow(sp, ident) != flow)
          ++ident;
        for (const auto address : probes) {
          const auto value = decode(address, 0, 1, 0);
          const auto held = std::ranges::find_if(reached, [&](const auto &x) {
            return address >= x.first.base && address < x.first.base + x.first.size &&
                   (!by_flow || x.second >> flow & 1);
          });
          const bool admitted = held != reached.end();
          // An unlisted pair or a miss goes to the default target at its miss window, where the
          // switches must take it on its miss route; otherwise the switches decide.
          if (!admitted && !missed)
            missed = miss_destination(plan, sp, network);
          const auto want =
              admitted ? network(address, by_flow ? std::uint64_t{1} << flow : held->second)
                       : *missed;
          const auto got = owning(owner, value);
          if (!got || !want || *got != *want)
            return fail("Source " + source + ": the " + p + " destination decode of address " +
                        decimal(address) + " differs from the switches' selection");
          // Admission failures complete locally whatever the address: an INCR
          // crossing 4 KiB, an illegal WRAP length.
          const auto page_end = (address | 4095) - 3;
          for (const auto local :
               {decode(page_end, 1, 1, 0), decode(address & ~std::uint64_t{3}, 2, 2, 0)})
            if (!local || *local != local_destination)
              return fail("Source " + source + ": an admission failure at address " +
                          decimal(address) + " does not decode to local completion");
          // A FIXED exclusive access fails admission inside an exclusive
          // window; elsewhere AxLOCK is ignored and it decodes as a normal one.
          const bool exclusive = std::ranges::any_of(windows, [&](const ExclusiveWindow &w) {
            return address >= w.window.base && address < w.window.base + w.window.size;
          });
          const auto locked = decode(address, 0, 0, 1), plain = decode(address, 0, 0, 0);
          if (!locked || !plain || *locked != (exclusive ? local_destination : *plain))
            return fail("Source " + source + ": an exclusive access at address " +
                        decimal(address) + " does not decode as its window requires");
        }
      }
    }
  }
  return Status::success({});
}

namespace {
// A binding violation of composed hardware.
struct BindingFault {
  std::string message;
};
void fault(bool violated, std::string message) {
  if (violated)
    throw BindingFault{std::move(message)};
}
// The ownership checks of composed hardware, in order: domain clocks and resets, physical
// storage, packet grant control, lowered adapters, the owner and reset of every state word, and
// the lifecycle of each source's response readiness. A violation throws BindingFault.
class CompositionAudit {
public:
  explicit CompositionAudit(const ExchangeHardware &hardware) : h(hardware), ir(hardware.ir) {}
  void run() {
    domain_clocks();
    storage();
    packet_grants();
    for (const auto &a : h.plan.adapters())
      fault(!h.bindings.contains(a.name + (a.serializer ? "/ser.started" : "/des.have_low")),
            "Planned adapter was not lowered");
    state_owners = words;
    control_owners();
    arbitration_owners();
    for (const auto &q : ir.states) {
      const auto it = clocks.find(root_clock(ir, q.clock).index);
      fault(it == clocks.end() || (q.reset && !it->second.local.contains(q.reset->signal.index) &&
                                   !it->second.port.contains(q.reset->signal.index)),
            "All state must belong to a domain clock and its reset");
    }
    fault(state_owners.size() != ir.states.size(), "Unaccounted state or hidden capacity");
    response_faces();
  }

private:
  const ExchangeHardware &h;
  const HardwareIR &ir;
  // Each domain root clock with the resets of its state and reset release registers.
  struct DomainResets {
    std::set<std::size_t> local, port;
  };
  std::map<std::size_t, DomainResets> clocks;
  std::map<std::string, const CrossStorageBinding *> stores;
  std::set<std::size_t> words, state_owners;
  std::string claim_failure;

  void domain_clocks() {
    for (const auto &[key, id] : h.bindings)
      if (key.starts_with("domain/") && key.ends_with(".clock")) {
        const auto domain = key.substr(0, key.size() - 6);
        fault(!h.bindings.contains(domain + ".reset") ||
                  !h.bindings.contains(domain + ".reset_port"),
              "Missing domain reset binding");
        auto &resets = clocks[root_clock(ir, id).index];
        resets.local.insert(h.bindings.at(domain + ".reset").index);
        resets.port.insert(h.bindings.at(domain + ".reset_port").index);
      }
    fault(clocks.empty() || (!h.plan.clocked() &&
                             !h.bindings.contains("domain/" + h.plan.domain.name + ".clock")),
          "Missing domain clock binding");
  }
  // Each storage record owns its payload words, matches its planned resource and sits in the
  // resource's domain; every planned payload has a record.
  void storage() {
    for (const auto &s : h.storage) {
      fault(!stores.emplace(s.resource, &s).second, "Duplicate physical storage binding");
      unsigned bits = 0;
      for (auto q : s.payload) {
        fault(q.index >= ir.values.size() || ir.values[q.index].op != Op::State ||
                  !words.insert(q.index).second,
              "Payload storage must have one owner: " + s.resource + " value " + decimal(q.index) +
                  " previous " + previous_payload_owner(s, q) + " local copies " +
                  decimal(std::ranges::count_if(s.payload,
                                                [&](ValueId x) { return x.index == q.index; })) +
                  (q.index < ir.values.size()
                       ? " op " + decimal(static_cast<unsigned>(ir.values[q.index].op))
                       : ""));
        bits += ir.values[q.index].width;
      }
      auto resource = std::ranges::find_if(h.plan.graph.resources,
                                           [&](const auto &r) { return r.id == s.resource; });
      // A resource stores payload_bits per word for each of its capacity words.
      fault(resource == h.plan.graph.resources.end() ||
                bits != resource->payload_bits * resource->capacity,
            "Payload storage does not match the planned resource: " + s.resource + " has " +
                decimal(bits) + " bits, expected " +
                decimal(resource == h.plan.graph.resources.end()
                            ? 0
                            : resource->payload_bits * resource->capacity));
      const auto assigned_clock = h.bindings.find("domain/" + resource->domain + ".clock");
      // A resource sits on its domain clock. Reset release chains sit on its clock port.
      fault(assigned_clock == h.bindings.end() ||
                (s.clock.index != assigned_clock->second.index &&
                 s.clock.index != root_clock(ir, assigned_clock->second).index),
            "Storage clock does not match its resource domain");
      auto wrong_clock = [&](ValueId id) {
        return id.index >= ir.values.size() || ir.values[id.index].op != Op::State ||
               root_clock(ir, ir.states[ir.values[id.index].state.index].clock).index !=
                   root_clock(ir, s.clock).index;
      };
      fault(std::ranges::any_of(s.payload, wrong_clock) ||
                std::ranges::any_of(s.control, wrong_clock),
            "A storage member belongs to a different clock domain");
      if (resource->pipe && resource->pipe->ungated) {
        const auto gated = [&](ValueId id) {
          return ir.states.at(ir.values.at(id.index).state.index).clock.index != s.clock.index;
        };
        fault(std::ranges::any_of(s.payload, gated) || std::ranges::any_of(s.control, gated),
              "An ungated pipeline storage member belongs to a region clock");
      }
    }
    for (const auto &r : h.plan.graph.resources)
      fault(r.payload_bits && !stores.contains(r.id), "Missing physical payload storage");
  }
  // The control words of a packet grant are exactly the arbiter's bound state: owner and held,
  // plus the policy state (a cursor or the FIFO order bits).
  void packet_grants() const {
    for (const auto &resource : h.plan.graph.resources) {
      if (resource.role != "packet_grant")
        continue;
      const auto it = stores.find(resource.id);
      fault(it == stores.end() || !it->second->payload.empty(),
            "Missing packet grant ownership storage");
      const auto &store = *it->second;
      std::set<std::size_t> bound, control;
      for (const auto &[name, id] : h.bindings)
        if (name.starts_with(resource.id) &&
            arbiter_state_binding(std::string_view(name).substr(resource.id.size())))
          bound.insert(id.index);
      for (auto q : store.control)
        control.insert(q.index);
      fault(!h.bindings.contains(resource.id + ".owner") ||
                !h.bindings.contains(resource.id + ".held") || bound != control ||
                control.size() != store.control.size(),
            "Packet grant control binding mismatch");
      const auto release = h.bindings.find(resource.id + ".release");
      fault(release == h.bindings.end() || release->second.index != store.release.index,
            "Packet grant release binding mismatch");
    }
  }
  // Claims the state word id once, in a domain clock with that domain's reset (1: the reset port
  // of a release chain) at value. value is none for a state without reset.
  bool claim(ValueId id, unsigned release = 0, std::optional<std::uint64_t> value = 0,
             const std::string &domain_name = {}) {
    if (id.index >= ir.values.size() || ir.values[id.index].op != Op::State) {
      claim_failure = "not a state";
      return false;
    }
    const auto &q = ir.states[ir.values[id.index].state.index];
    if (q.reset.has_value() != value.has_value() ||
        !clocks.contains(root_clock(ir, q.clock).index)) {
      claim_failure = "reset presence or clock ownership";
      return false;
    }
    const auto &domain = clocks.at(root_clock(ir, q.clock).index);
    auto expected = release == 1 ? domain.port : domain.local;
    if (!domain_name.empty()) {
      const auto key = "domain/" + domain_name + (release == 1 ? ".reset_port" : ".reset");
      expected = {h.bindings.at(key).index};
    }
    if (q.reset && (!expected.contains(q.reset->signal.index) || q.reset->value != *value)) {
      claim_failure = "unexpected reset " + decimal(q.reset->signal.index) + " value " +
                      decimal(q.reset->value) + " expected set size " + decimal(expected.size()) +
                      (expected.empty() ? std::string{} : " first " + decimal(*expected.begin()));
      return false;
    }
    if (!state_owners.insert(id.index).second) {
      claim_failure = "duplicate claim";
      return false;
    }
    claim_failure.clear();
    return true;
  }
  // With the reset handshake a crossing count (control word 1) has no reset and the wait counter
  // (after the count chain, announce and on) resets to all ones. A division counter
  // restarts at ratio - 1.
  std::optional<std::uint64_t> reset_value(const std::string &store, std::size_t i) const {
    const auto cut_name = store.ends_with("/receive") ? store.substr(0, store.size() - 8) : store;
    if (const auto admission = h.bindings.find(cut_name + "/admission_open");
        admission != h.bindings.end() &&
        admission->second.index == stores.at(store)->control[i].index)
      return 1;
    if (store.ends_with("/divider")) {
      const auto name = store.substr(7, store.size() - 7 - 8);
      const auto &list = h.plan.clocked() ? h.plan.domains : std::vector{h.plan.domain};
      const auto domain = std::ranges::find(list, name, &DomainPlan::name);
      return domain == list.end() ? std::nullopt : std::optional<std::uint64_t>{domain->ratio - 1};
    }
    const auto cut = std::ranges::find_if(h.plan.cuts, [&](const auto &c) {
      const auto *async = std::get_if<AsyncClockCut>(&c.options);
      return async && async->reset_handshake && (store == c.name || store == c.name + "/receive");
    });
    if (cut == h.plan.cuts.end() || i == 1)
      return cut == h.plan.cuts.end() ? std::optional<std::uint64_t>{0} : std::nullopt;
    const auto n = std::get<AsyncClockCut>(cut->options).synchronizer_stages;
    return i == (store == cut->name ? 4 : 5) + n ? (1U << n) - 1 : 0;
  }
  void control_owners() {
    for (const auto &store : h.storage)
      for (std::size_t i = 0; i < store.control.size(); ++i) {
        const auto resource =
            std::ranges::find(h.plan.graph.resources, store.resource, &CrossResource::id);
        const auto domain = h.plan.clocked() ? resource->domain : h.plan.domain.name;
        const bool valid = claim(store.control[i], store.resource.ends_with("/init") ? 1 : 0,
                                 reset_value(store.resource, i), domain);
        fault(!valid, "Control state must have one owner and the reset of its domain: " +
                          store.resource + " index " + decimal(i) + " value " +
                          decimal(store.control[i].index) + " " + claim_failure);
      }
  }
  // The service face grant (svc.turn/busy/owner) is already owned by its storage record; the
  // switch grants are claimed here.
  void arbitration_owners() {
    for (const auto &[name, id] : h.bindings) {
      fault(id.index >= ir.values.size(), "Undefined object binding");
      // The open-edge deadline of a region's clock gate (clock_gating.hpp).
      if (name.starts_with("gate/") && name.ends_with(".remaining"))
        fault(ir.values[id.index].width != 2 || !claim(id, 0, 3),
              "A gate deadline must have two bits, one owner and its domain reset loading three");
      // The registers of the pressure sideband (message.hpp point 15).
      if (name.starts_with("level_hint/"))
        fault(!claim(id), "A pressure register must have one owner and the reset of its domain");
      // The registers of an error logger (error_logger.hpp).
      if (name.starts_with("observe/") && ir.values[id.index].op == Op::State)
        fault(!claim(id), "An error logger register needs one owner and its domain reset");
      // The registers of a bank of another clock domain than its target.
      if (name.starts_with("service/bank/") && ir.values[id.index].op == Op::State)
        fault(!claim(id), "A bank register must have one owner and the reset of its domain");
      if (const auto dot = name.rfind('.');
          dot != std::string::npos && arbiter_state_binding(std::string_view(name).substr(dot)) &&
          !state_owners.contains(id.index))
        fault(!claim(id), "Arbitration state must have one owner and the reset of its domain");
    }
  }
  std::vector<std::size_t> state_leaves_of(std::size_t root,
                                           std::span<const ValueId> stops = {}) const {
    std::vector<std::size_t> out;
    walk_cone(
        ir, ValueId{root}, [&](ValueId id) { out.push_back(id.index); }, [](ValueId) {},
        [&](ValueId id) {
          return std::ranges::any_of(stops, [&](ValueId s) { return s.index == id.index; });
        });
    return out;
  }
  std::string previous_payload_owner(const CrossStorageBinding &store, ValueId id) const {
    for (const auto &candidate : h.storage) {
      if (&candidate == &store)
        break;
      if (std::ranges::any_of(candidate.payload, [&](ValueId q) { return q.index == id.index; }))
        return candidate.resource;
    }
    return {};
  }
  std::string state_owner_of(std::size_t index) const {
    for (const auto &s : h.storage) {
      auto in = [&](const auto &v) {
        return std::ranges::any_of(v, [&](auto q) { return q.index == index; });
      };
      if (in(s.payload) || in(s.control))
        return s.resource;
    }
    return {};
  }
  // The lifecycle anchor behind the reserved return write: each source engine's response-face
  // readiness is a function of the flow's own storage and the environment, never of another
  // flow's state.
  void response_faces() const {
    for (const auto &source : names(h.plan.sources))
      for (const auto *operation : {"write", "read"}) {
        const auto id = flow_id(source, operation);
        const auto face = h.bindings.find(id + ".response_ready");
        fault(face == h.bindings.end(), "Missing the response-face readiness anchor for " + id);
        // A clamp gathering per tag reads the fragment in front of it: its tag names the read among
        // this flow's own, its address where that read's beat ends, its last where a held word
        // ends. A decode packet's word (message.hpp point 19) is told by its mark. The
        // transport holds the fragment until this readiness takes it, so neither may read this
        // readiness.
        std::vector<ValueId> head;
        for (const auto *field : {".response_tag", ".response_head_address", ".response_head_last",
                                  ".response_head_count", ".response_head_result",
                                  ".response_head_error", ".response_head_valid"})
          if (const auto at = h.bindings.find(id + field); at != h.bindings.end()) {
            head.push_back(at->second);
            walk_cone(
                ir, at->second, [](ValueId) {}, [](ValueId) {},
                [&](ValueId value) {
                  fault(value.index == face->second.index,
                        "The response head of " + id + " depends on its readiness");
                  return false;
                });
          }
        for (const auto leaf : state_leaves_of(face->second.index, head)) {
          const auto owner = state_owner_of(leaf);
          // The domain's reset release registers gate every handshake.
          const auto release = "domain/" +
                               (h.plan.clocked() ? h.plan.object_domains.at("source/" + source)
                                                 : h.plan.domain.name) +
                               "/init";
          fault(owner != id && owner != id + "/return" && owner != release &&
                    !owner.starts_with("source/" + source + "/pipe/"),
                "The response-face readiness of " + id +
                    " depends on state outside its own lifecycle");
        }
      }
  }
};
} // namespace

Status verify_axi4_exchange_composition(const ExchangeHardware &h) {
  if (auto status = verify_structure(h.ir); !status)
    return status;
  if (auto status = verify_axi4_exchange_obligations(h.plan, h.plan.clocked()); !status)
    return status;
  if (h.plan.clocked())
    if (auto status = verify_exchange_clock_hardware(h); !status)
      return status;
  try {
    CompositionAudit(h).run();
  } catch (const BindingFault &f) {
    return Status::failure(binding_error(f.message));
  }
  return Status::success({});
}
} // namespace nocloom::detail
