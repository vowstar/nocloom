#include "dependency_graph.hpp"
#include "diagnostic.hpp"
#include "network_plan.hpp"
#include "network_plan_profiles.hpp"
#include <algorithm>
#include <array>
#include <deque>
#include <iterator>
#include <map>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>
#include <tuple>
#include <unordered_map>

namespace nocloom::detail {
namespace {
constexpr std::size_t finding_limit = 64;
constexpr std::size_t model_endpoint_pairs = 4096;
// Stated in the order of DependencyRule. Each wait reads the code it names.
constexpr std::array<DependencyRuleInfo, 20> rule_table{{
    // Endpoint level: a route's target blocks its source, and the response side blocks the request
    // side; the same waits as a route's endpoints in any wormhole network.
    {"S1", "target.blocks_source", "endpoint"},
    {"S2", "response.blocks_accept", "endpoint"},
    {"S3", "target.blocks_return", "endpoint"},
    {"S4", "request.path", "endpoint"},
    {"S5", "response.path", "endpoint"},
    {"S6", "external", "endpoint"},
    // treadle.cpp lower_output: a grant holds its output from win to tail; graph_merge's mux.
    {"M1", "grant.contention", "micro"},
    {"M2", "grant.downstream", "micro"},
    // A response grant releases at last or cut (graph_merge): without cut it needs the next word.
    {"M3", "grant.continuation", "micro"},
    // axi4_outlet.hpp interleave: one response register takes one word at a time.
    {"M4", "target.queue_order", "micro"},
    // axi4_outlet.cpp pool.read_ok, pool.write_ok: an entry returns as its last word leaves.
    {"M6", "target.pool", "micro"},
    {"M7", "source.write_data", "micro"},
    // exchange_composition.cpp rejoin_requests, pool_answers.
    {"M8", "source.unit", "micro"},
    // The staging of a source's engine and its width clamp's gather.
    {"M11", "source.return", "micro"},
    {"M14", "merge.order", "micro"},
    {"M15", "stage.occupancy", "micro"},
    {"M16", "environment", "micro"},
    // A descriptor retains its replay slot through the final accepted response.
    // The pool waits only when its descriptors are occupied. The shared return stage
    // and the outlet's RREADY and B wait while saved beats drain.
    {"M17", "merge.replay_capacity", "micro"},
    {"M18", "merge.stored_word", "micro"},
    {"E0", "escape", "micro"},
}};
// Where a kind's nodes are outside the network: no input of their own, an assumption of liveness.
constexpr std::array<std::pair<std::string_view, std::string_view>, 6> assumptions{{
    {"env.consumer", "F1"},
    {"env.source_data", "F2"},
    {"env.target_request", "F3"},
    {"env.target_response", "F4"},
    // A service target's serial path: its bank's bus is always done and one packet is on it.
    {"env.bank_done", "F5"},
    {"env.logger_hold", "F6"},
}};
std::string_view assumption_of(std::string_view kind) {
  const auto it = std::ranges::find(assumptions, kind, [](const auto &a) { return a.first; });
  return it == assumptions.end() ? std::string_view{} : it->second;
}
struct NodeHash {
  std::size_t operator()(const std::pair<std::string, std::string> &k) const {
    return std::hash<std::string>{}(k.first) ^ (std::hash<std::string>{}(k.second) * 31);
  }
};

class DependencyBuilder {
public:
  DependencyBuilder(const NetworkPlan &plan, const DependencyFacts &facts)
      : plan(plan), facts(facts) {
    for (const auto &link : plan.links)
      links.emplace(link.name, &link);
    for (const auto *g : {&plan.exchange.request, &plan.exchange.response})
      for (const auto &o : g->outputs)
        outputs.emplace(direction(g == &plan.exchange.response) + o.node + ">" + o.output, &o);
    for (unsigned si = 0; si < plan.sources.size(); ++si)
      sources.emplace(plan.sources[si].name, si);
    answering.resize(plan.sources.size());
    holds_anywhere = std::ranges::find(facts.segment_ends, false) != facts.segment_ends.end() &&
                     std::ranges::any_of(plan.targets, [&](const TargetPlan &t) {
                       return facts.interleaved[plan.target_index(t.name)] && t.outstanding > 1;
                     });
    for (const auto &route : plan.exchange.response.routes)
      answering[source_index(route.exit)].insert(route.entry);
  }
  DependencyGraph run() {
    endpoint_level();
    for (bool response : {false, true})
      for (const auto &route :
           response ? plan.exchange.response.routes : plan.exchange.request.routes)
        walk(route, response);
    target_order();
    stored_words();
    endpoint_sides();
    exclusions();
    model_tables();
    apply_facts();
    return finish();
  }

private:
  using Key = std::pair<std::string, std::string>;
  const NetworkPlan &plan;
  const DependencyFacts &facts;
  std::unordered_map<std::string, const LinkPlan *> links;
  std::unordered_map<std::string, const SwitchOutputPlan *> outputs;
  std::unordered_map<std::string, unsigned> sources;
  std::unordered_map<Key, std::uint32_t, NodeHash> index;
  std::set<std::string> walked;
  std::vector<std::set<std::string>> answering;
  std::vector<DependencyNode> nodes;
  std::vector<DependencyEdge> edges;
  std::vector<DependencyExclusion> excluded;
  std::uint32_t groups = 0;

  static std::string direction(bool response) { return response ? "rsp/" : "req/"; }
  unsigned source_index(const std::string &name) const { return sources.at(name); }
  std::uint32_t node(const std::string &kind, const std::string &key, std::string component = {},
                     std::string binding = {}) {
    const auto [at, added] = index.try_emplace({kind, key}, std::uint32_t(nodes.size()));
    if (added) {
      const auto placed = plan.object_domains.find(component);
      nodes.push_back({kind, key, std::move(component),
                       placed != plan.object_domains.end() ? placed->second : plan.domain.name,
                       std::move(binding)});
    }
    return at->second;
  }
  void edge(std::uint32_t from, std::uint32_t to, DependencyRule rule) {
    edges.push_back({from, to, rule});
  }
  // Keys of a target's words: an AXI4 target has one response channel, a target of several slave
  // interfaces one per flow.
  static std::string head_key(const SwitchRoute &route, bool per_flow) {
    return route.entry + (per_flow ? "/flow" + std::to_string(route.target_path.value_or(0)) : "") +
           "/" + route.exit;
  }
  static bool per_flow_target(const TargetPlan &t) {
    return t.protocol != Protocol::Axi4 && t.flows > 1;
  }
  // A source answered by at most one target at a time waits on no grant another target holds at
  // its own output (rule X1): one transfer, or one direction with one AXI ID.
  bool answered_by_two(unsigned si) const {
    const auto &s = plan.sources[si];
    return answering[si].size() > 1 && !s.apb && (s.outstanding > 1 || !s.pooled) &&
           (s.outstanding_ids > 1 || (!s.read_only && !s.write_only));
  }

  void endpoint_level() {
    for (const auto &route : plan.exchange.request.routes) {
      const auto s = route.entry, t = route.exit;
      const auto treq = node("endpoint.target.req", t, "target/" + t);
      const auto trsp = node("endpoint.target.rsp", t, "target/" + t);
      const auto sreq = node("endpoint.source.req", s, "source/" + s);
      const auto srsp = node("endpoint.source.rsp", s, "source/" + s);
      edge(treq, sreq, DependencyRule::TargetBlocksSource);
      edge(trsp, treq, DependencyRule::ResponseBlocksAccept);
      edge(srsp, trsp, DependencyRule::TargetBlocksReturn);
    }
    for (bool response : {false, true})
      for (const auto &route :
           response ? plan.exchange.response.routes : plan.exchange.request.routes)
        endpoint_path(route, response);
  }
  // Each step of a route blocks the step before it.
  void endpoint_path(const SwitchRoute &route, bool response) {
    const auto rule = response ? DependencyRule::ResponsePath : DependencyRule::RequestPath;
    const auto scope = direction(response);
    auto previous = response ? node("endpoint.target.rsp", route.entry, "target/" + route.entry)
                             : node("endpoint.source.req", route.entry, "source/" + route.entry);
    auto from = route.entry;
    for (std::size_t i = 0; i <= route.nodes.size(); ++i) {
      const auto &to = i < route.nodes.size() ? route.nodes[i] : route.exit;
      const auto link = node("endpoint.link", scope + from + ">" + to);
      edge(link, previous, rule);
      previous = link;
      if (i < route.nodes.size()) {
        const auto &next = i + 1 < route.nodes.size() ? route.nodes[i + 1] : route.exit;
        const auto step = node("endpoint.output", scope + to + ">" + next, scope + to);
        edge(step, previous, rule);
        previous = step;
      }
      from = to;
    }
    const auto end = response ? node("endpoint.source.rsp", route.exit, "source/" + route.exit)
                              : node("endpoint.target.req", route.exit, "target/" + route.exit);
    edge(end, previous, rule);
  }

  // The stages of a link in flow order, chained toward the word before them; the node a stall
  // enters at, which is `word` itself without stages.
  std::uint32_t link_stages(const LinkPlan &link, std::uint32_t word) {
    auto at = word;
    for (const auto &stage : link.stages) {
      const auto kind = stage.kind == StageKind::Pipe      ? "occupancy.pipe"
                        : stage.kind == StageKind::Adapter ? "occupancy.adapter"
                                                           : "occupancy.crossing";
      const auto next = node(kind, stage.name, link.name.substr(0, 4) + stage.domain, stage.name);
      nodes[next].domain = stage.domain;
      edge(next, at, DependencyRule::Occupancy);
      at = next;
    }
    return at;
  }
  const LinkPlan &link_named(const std::string &name) const {
    const auto it = links.find(name);
    if (it == links.end())
      throw std::logic_error("Unplanned link " + name);
    return *it->second;
  }
  // The grant a word takes at an output: grant.held, or output.wire where the output has one
  // candidate. What follows the output stalls the word and the grant apart: a word waiting across
  // a read holds its grant without being stalled itself (M2, M3).
  std::uint32_t grant_at(const std::string &key, const std::string &input) {
    const auto &o = *outputs.at(key);
    const auto component = key.substr(0, key.find('>'));
    if (o.inputs.size() < 2)
      return node("output.wire", key + "/" + input, component);
    const auto response = key.starts_with("rsp/");
    return node("grant.held", key + "/" + input, component,
                std::string("graph/") + direction(response) + o.node + ">" +
                    (o.endpoint ? "endpoint/" : "node/") + o.output);
  }
  // One route's stream through its stages and outputs, once per distinct path. A word stands in a
  // slot: the endpoint's stream, then the word of each link between outputs (link.word). A stall
  // of the next slot reaches both the word behind it and the grant it passed.
  void walk(const SwitchRoute &route, bool response) {
    auto path = direction(response) + route.entry + ">" + route.exit;
    for (const auto &n : route.nodes)
      path += "/" + n;
    if (response && plan.target(route.entry).flows > 1)
      path += "#" + std::to_string(route.target_path.value_or(0));
    if (!walked.insert(path).second)
      return;
    const auto scope = direction(response);
    auto slot = response ? response_start(route) : request_start(route);
    auto stall = response ? response_pipe(route, slot) : slot;
    auto input = "endpoint/" + route.entry;
    auto from = route.entry;
    auto slot_name = entry_slot(route, response);
    std::string previous;
    for (std::size_t i = 0; i <= route.nodes.size(); ++i) {
      const auto &to = i < route.nodes.size() ? route.nodes[i] : route.exit;
      const auto name = scope + from + ">" + to;
      if (i == 0)
        stall = link_stages(link_named(name), stall);
      if (i == route.nodes.size()) {
        const auto end = response ? response_end(route) : request_end(route);
        const auto &last = passes.back();
        edge(end, link_stages(link_named(name), last.held), DependencyRule::Downstream);
        edge(end, last.slot, DependencyRule::Downstream);
        edge(end, last.held, DependencyRule::Downstream);
        break;
      }
      const auto &next = i + 1 < route.nodes.size() ? route.nodes[i + 1] : route.exit;
      const auto key = scope + to + ">" + next;
      const auto held = grant_at(key, input);
      const auto single = response && i + 1 == route.nodes.size()
                              ? std::optional{source_index(route.exit)}
                              : std::nullopt;
      if (outputs.at(key)->inputs.size() > 1)
        contend(key, input, slot, stall, single);
      if (i > 0) {
        edge(slot, passes.back().slot, DependencyRule::Downstream);
        edge(slot, passes.back().held, DependencyRule::Downstream);
        next_slots[passes.back().key] = slot_name;
      }
      passes.push_back({route.entry, route.exit, key, slot_name, previous, slot, held, response,
                        path,
                        !response && per_flow_target(plan.target(route.exit))
                            ? "/flow" + std::to_string(route.target_path.value_or(0))
                            : std::string()});
      previous = slot_name;
      if (i + 1 < route.nodes.size()) {
        const auto linkname = scope + to + ">" + route.nodes[i + 1];
        slot = node("link.word", linkname, scope + to);
        slot_name = linkname;
        stall = link_stages(link_named(linkname), held);
        edge(slot, stall, DependencyRule::Occupancy);
        stall = slot;
      }
      input = "node/" + to;
      from = to;
    }
  }
  // Another input holds the output this word needs (M1), unless this source has one target
  // answering it at a time (exclusion X1) while some grant may be held across a read.
  void contend(const std::string &key, const std::string &input, std::uint32_t slot,
               std::uint32_t stall, std::optional<unsigned> single) {
    if (single && holds_anywhere && !answered_by_two(*single)) {
      ++suppressed[*single];
      return;
    }
    const auto other = node("grant.other", key + "/" + input, key.substr(0, key.find('>')));
    edge(other, slot, DependencyRule::Contention);
    if (stall != slot)
      edge(other, stall, DependencyRule::Contention);
  }
  std::map<unsigned, std::uint64_t> suppressed;
  struct Pass {
    std::string entry, exit, key, slot_name, previous;
    std::uint32_t slot = 0, held = 0;
    bool response = false;
    std::string path;
    std::string flow; // Of a per-flow target: which of its slave interfaces.
  };
  std::map<std::string, std::string> next_slots;
  // The name of the slot a route's words start in.
  std::string entry_slot(const SwitchRoute &route, bool response) const {
    if (!response)
      return "source/" + route.entry;
    const auto &t = plan.target(route.entry);
    return "target/" + t.name +
           (per_flow_target(t) ? "/flow" + std::to_string(route.target_path.value_or(0)) : "");
  }
  std::vector<Pass> passes;
  std::map<const Pass *, std::uint32_t> pass_dest;
  bool holds_anywhere = false;

  std::uint32_t request_start(const SwitchRoute &route) {
    return node("source.merge", route.entry, "source/" + route.entry);
  }
  std::uint32_t request_end(const SwitchRoute &route) {
    return node("target.pool", route.exit, "target/" + route.exit);
  }
  // The target's own word for this source.
  std::uint32_t response_start(const SwitchRoute &route) {
    const auto &t = plan.target(route.entry);
    const auto head = node("target.head", head_key(route, per_flow_target(t)), "target/" + t.name);
    answer_of(t, head, route.exit);
    return head;
  }
  // A word in the target's response pipe blocks the words behind it.
  std::uint32_t response_pipe(const SwitchRoute &route, std::uint32_t head) {
    const auto &t = plan.target(route.entry);
    const auto pipe = std::ranges::find_if(
        t.pipes, [](const auto &p) { return p.name.ends_with("/pipe/response"); });
    if (pipe == t.pipes.end())
      return head;
    const auto at = node("occupancy.endpoint_pipe", pipe->name, "target/" + t.name, pipe->name);
    edge(at, head, DependencyRule::Occupancy);
    return at;
  }
  // The stage a response ends at: the width clamp's gather, then the source's staging.
  std::uint32_t response_end(const SwitchRoute &route) {
    const auto si = source_index(route.exit);
    const auto &s = plan.sources[si];
    const auto component = "source/" + s.name;
    const auto staging = node("source.staging", s.name, component);
    if (!s.clamp)
      return staging;
    const auto kind = facts.reassembles[si] ? "gather.tag" : "gather.single";
    const auto gather = node(kind, s.name, component);
    edge(staging, gather, DependencyRule::ReturnStage);
    return gather;
  }

  // Words of an interleaving target wait behind its front word. That front word stalls a grant
  // only where the word's own destination differs from the held one (a word for the held output
  // passes): prefix and suffix chains keep "any other destination" linear.
  void target_order() {
    std::map<std::string, std::vector<const Pass *>> by_target;
    for (const auto &pass : passes)
      if (pass.response)
        by_target[pass.entry].push_back(&pass);
    for (unsigned i = 0; i < plan.targets.size(); ++i)
      if (const auto &t = plan.targets[i]; facts.interleaved[i] && t.outstanding > 1)
        if (const auto it = by_target.find(t.name); it != by_target.end())
          queue_order(t, it->second);
  }
  static std::vector<std::string> distinct_exits(const std::vector<const Pass *> &held) {
    std::set<std::string> exits;
    for (const auto *h : held)
      exits.insert(h->exit);
    return {exits.begin(), exits.end()};
  }
  void queue_order(const TargetPlan &t, const std::vector<const Pass *> &held) {
    if (std::ranges::none_of(held, [&](const Pass *h) { return holds_across(h->exit); }))
      return;
    const auto destinations = distinct_exits(held);
    const auto component = "target/" + t.name;
    std::vector<std::uint32_t> before, after;
    for (const auto &d : destinations) {
      const auto head = node("target.head", t.name + "/" + d, component);
      before.push_back(node("target.behind", t.name + "/before/" + d, component));
      after.push_back(node("target.behind", t.name + "/after/" + d, component));
      edge(head, before.back(), DependencyRule::QueueOrder);
      edge(head, after.back(), DependencyRule::QueueOrder);
    }
    for (std::size_t i = 1; i < destinations.size(); ++i) {
      edge(before[i - 1], before[i], DependencyRule::QueueOrder);
      edge(after[i], after[i - 1], DependencyRule::QueueOrder);
    }
    for (const auto *h : held)
      if (holds_across(h->exit)) {
        const auto at = static_cast<std::size_t>(std::ranges::lower_bound(destinations, h->exit) -
                                                 destinations.begin());
        if (at > 0)
          edge(before[at - 1], h->held, DependencyRule::Continuation);
        if (at + 1 < destinations.size())
          edge(after[at + 1], h->held, DependencyRule::Continuation);
      }
  }
  bool holds_across(const std::string &source) const {
    return !facts.segment_ends.at(source_index(source));
  }

  // A held output's other inputs: grant.other(o, i) stands for any input of o but i holding it.
  void contention(const std::string &key, const SwitchOutputPlan &o) {
    const auto component = key.substr(0, key.find('>'));
    std::vector<std::uint32_t> before, after;
    for (const auto &input : o.inputs) {
      before.push_back(node("grant.before", key + "/" + input, component));
      after.push_back(node("grant.after", key + "/" + input, component));
    }
    for (std::size_t i = 0; i < o.inputs.size(); ++i) {
      const auto held = index.find({"grant.held", key + "/" + o.inputs[i]});
      if (held != index.end()) {
        edge(held->second, before[i], DependencyRule::Contention);
        edge(held->second, after[i], DependencyRule::Contention);
      }
      if (i > 0)
        edge(before[i - 1], before[i], DependencyRule::Contention);
      if (i + 1 < o.inputs.size())
        edge(after[i + 1], after[i], DependencyRule::Contention);
      const auto other = index.find({"grant.other", key + "/" + o.inputs[i]});
      if (other == index.end())
        continue;
      if (i > 0)
        edge(before[i - 1], other->second, DependencyRule::Contention);
      if (i + 1 < o.inputs.size())
        edge(after[i + 1], other->second, DependencyRule::Contention);
    }
  }

  void source_side(unsigned si) {
    const auto &s = plan.sources[si];
    const auto component = "source/" + s.name;
    const auto merge = node("source.merge", s.name, component);
    const auto pool = node("source.pool", s.name, component);
    const auto returns = node("source.return", s.name, component);
    edge(pool, merge, DependencyRule::SourceUnit);
    edge(returns, pool, DependencyRule::SourceUnit);
    if (s.id_table != 0 && s.id_table < s.id_keys) {
      const auto table = node("source.id_table", s.name, component);
      edge(table, merge, DependencyRule::SourceUnit);
      edge(returns, table, DependencyRule::SourceUnit);
    }
    edge(node("env.source_data", s.name, component), merge, DependencyRule::Environment);
    edge(node("env.consumer", s.name, component), node("source.staging", s.name, component),
         DependencyRule::Environment);
    if (s.qos.picks())
      scheduler_waits(s, merge);
    for (const auto &pipe : s.pipes)
      edge(merge, node("occupancy.endpoint_pipe", pipe.name, component, pipe.name),
           DependencyRule::Occupancy);
  }
  void scheduler_waits(const SourcePlan &s, std::uint32_t merge) {
    const auto component = "source/" + s.name;
    const auto control =
        node("scheduler.control", s.name, component, component + "/regulator.control_valid");
    const auto packet =
        node("scheduler.packet", s.name, component, component + "/regulator.data_held");
    edge(control, merge, DependencyRule::SourceUnit);
    edge(packet, control, DependencyRule::Continuation);
    edge(node("env.source_data", s.name, component), packet, DependencyRule::SourceData);
    for (const auto &pass : passes)
      if (!pass.response && pass.entry == s.name) {
        edge(pass.held, control, DependencyRule::Downstream);
        edge(pass.held, packet, DependencyRule::Downstream);
      }
  }
  // The crossing named by a serial path, if the network has it: its occupancy between what fills it
  // and the node it stalls.
  std::optional<std::uint32_t> serial_crossing(const std::string &segment,
                                               const std::string &component) {
    const auto cut = std::ranges::find(plan.cuts, "cut/" + segment, &ClockCut::name);
    if (cut == plan.cuts.end())
      return std::nullopt;
    const auto at = node("occupancy.crossing", cut->name, component, cut->name);
    nodes[at].domain = cut->from_domain;
    return at;
  }
  // Whether a service target's banks are in another domain.
  bool serial_crossed(const TargetPlan &t) const {
    return std::ranges::find(plan.cuts,
                             "cut/service/" + t.name + service_branch_suffix(*t.service, 0) +
                                 "/request",
                             &ClockCut::name) != plan.cuts.end();
  }
  // The bank waits behind a crossing of a branch of the serial path, when its domain is another.
  bool bank_wait(const TargetPlan &t, unsigned branch, const char *way, std::uint32_t waiting) {
    const auto component = "target/" + t.name;
    const auto crossing = serial_crossing(
        "service/" + t.name + service_branch_suffix(*t.service, branch) + "/" + way, component);
    if (crossing) {
      edge(node("env.bank_done", t.name, component), *crossing, DependencyRule::Occupancy);
      edge(*crossing, waiting, DependencyRule::Occupancy);
    }
    return crossing.has_value();
  }
  void target_side(const TargetPlan &t) {
    const auto component = "target/" + t.name;
    const auto pool = node("target.pool", t.name, component);
    const bool crossed = t.service && serial_crossed(t);
    if (!crossed || t.service->banks.back().branch == 0) {
      if (!t.service || !crossed || !bank_wait(t, 0, "request", pool))
        edge(node(t.service ? "env.bank_done" : "env.target_request", t.name, component), pool,
             DependencyRule::Environment);
    }
    // The service switch's word register waits for its branch; one packet is on the path, so no
    // other input contends for it and the path leads on to nothing but the bank's answer.
    if (t.service && t.service->banks.back().branch > 0)
      for (const auto &bank : t.service->banks) {
        // One output per branch: its first bank names it.
        if (&bank != &t.service->banks.front() && bank.branch == (&bank - 1)->branch)
          continue;
        const auto output = node("service.output", bank.plan.name, component);
        if (!crossed || !bank_wait(t, bank.branch, "request", output))
          edge(node("env.bank_done", t.name, component), output, DependencyRule::Occupancy);
        edge(output, pool, DependencyRule::Occupancy);
      }
    for (const auto &pipe : t.pipes)
      if (!pipe.name.ends_with("/pipe/response"))
        edge(pool, node("occupancy.endpoint_pipe", pipe.name, component, pipe.name),
             DependencyRule::Occupancy);
  }
  // A word still at a target blocks its pool entry, and the source's unit behind it.
  void answer_of(const TargetPlan &t, std::uint32_t head, const std::string &source) {
    const auto component = "target/" + t.name;
    // A bank's bus is always done: its serial path answers one packet with no wait of its own.
    if (t.service && serial_crossed(t))
      for (unsigned branch = 0; branch <= t.service->banks.back().branch; ++branch)
        bank_wait(t, branch, "response", head);
    else
      edge(node(t.service ? "env.bank_done" : "env.target_response", t.name, component), head,
           DependencyRule::Environment);
    edge(head, node("target.pool", t.name, component), DependencyRule::PoolWait);
    edge(head, node("source.return", source, "source/" + source), DependencyRule::SourceUnit);
    if (t.upsize)
      edge(head, node("merge.single", t.name, component), DependencyRule::MergeOrder);
  }
  // Each read descriptor owns its saved word. Saved beats wait for their return path and keep
  // their descriptor occupied. While they drain, the shared return stage blocks other words
  // and B. No saved word waits for a new request or a second replay slot.
  void stored_words() {
    for (unsigned i = 0; i < plan.targets.size(); ++i) {
      const auto &t = plan.targets[i];
      if (t.upsize && t.outstanding > 1 && joined_target(plan, t))
        stored_words_of(t);
    }
  }
  void stored_words_of(const TargetPlan &t) {
    const auto component = "target/" + t.name;
    std::vector<const Pass *> first;
    for (const auto &pass : passes)
      if (pass.response && pass.previous.empty() && pass.entry == t.name)
        first.push_back(&pass);
    const auto destinations = distinct_exits(first);
    const auto pool = node("target.pool", t.name, component);
    std::vector<std::uint32_t> before, after;
    for (const auto &d : destinations) {
      const auto stored = node("merge.stored", t.name + "/" + d, component);
      edge(stored, pool, DependencyRule::StoredRequest);
      before.push_back(node("target.behind", t.name + "/stored_before/" + d, component));
      after.push_back(node("target.behind", t.name + "/stored_after/" + d, component));
      edge(stored, before.back(), DependencyRule::StoredWord);
      edge(stored, after.back(), DependencyRule::StoredWord);
    }
    for (std::size_t i = 1; i < destinations.size(); ++i) {
      edge(before[i - 1], before[i], DependencyRule::StoredWord);
      edge(after[i], after[i - 1], DependencyRule::StoredWord);
    }
    for (std::size_t k = 0; k < destinations.size(); ++k) {
      const auto head = node("target.head", t.name + "/" + destinations[k], component);
      if (k > 0)
        edge(before[k - 1], head, DependencyRule::StoredWord);
      if (k + 1 < destinations.size())
        edge(after[k + 1], head, DependencyRule::StoredWord);
    }
    for (const auto *pass : first)
      stored_waits(*pass, node("merge.stored", t.name + "/" + pass->exit, component));
  }
  void stored_waits(const Pass &pass, std::uint32_t stored) {
    const auto other = index.find({"grant.other", pass.key + "/endpoint/" + pass.entry});
    if (other != index.end())
      edge(other->second, stored, DependencyRule::Contention);
    const auto next = next_slots.find(pass.key);
    if (next != next_slots.end()) {
      edge(node("link.word", next->second,
                direction(true) + pass.key.substr(4, pass.key.find('>') - 4)),
           stored, DependencyRule::Downstream);
      return;
    }
    const auto source = "source/" + pass.exit;
    const auto si = source_index(pass.exit);
    const auto kind = !plan.sources[si].clamp ? "source.staging"
                      : facts.reassembles[si] ? "gather.tag"
                                              : "gather.single";
    edge(node(kind, pass.exit, source), stored, DependencyRule::Downstream);
  }
  void endpoint_sides() {
    for (unsigned si = 0; si < plan.sources.size(); ++si)
      source_side(si);
    for (const auto &t : plan.targets)
      target_side(t);
    // An error logger holds no packet: its slot waits on nothing of the network and nothing of the
    // network waits on it (error_logger.hpp). Its nodes stay apart from the data path.
    for (const auto &observer : plan.fault_monitors) {
      const auto component = "observe/" + observer.logger.name;
      const auto slot = node("logger.slot", observer.logger.name, component);
      edge(node("env.logger_hold", observer.logger.name, component), slot,
           DependencyRule::Environment);
      for (const auto &observed : observer.observed) {
        const auto &name = plan.targets[observed.target].name;
        const auto head = node("observe.head", name, component);
        if (const auto crossing = serial_crossing(
                "service/observe/" + observer.logger.name + "/" + name, component)) {
          const auto assembly = node("logger.assembly", observer.logger.name, component);
          edge(slot, assembly, DependencyRule::Occupancy);
          edge(assembly, *crossing, DependencyRule::Occupancy);
          edge(*crossing, head, DependencyRule::Occupancy);
        } else {
          edge(slot, head, DependencyRule::Occupancy);
        }
      }
      // A record not deleted waits for the next logger's room; a chain ends at its last slot.
      auto waiting = slot;
      for (std::size_t n = 0; n < observer.logger.chain.size(); ++n) {
        const auto next =
            node("logger.slot", observer.logger.name + "/" + std::to_string(n + 1), component);
        edge(next, waiting, DependencyRule::Occupancy);
        edge(node("env.logger_hold", observer.logger.name + "/" + std::to_string(n + 1), component),
             next, DependencyRule::Environment);
        waiting = next;
      }
    }
    for (const auto &[key, o] : outputs)
      if (o->inputs.size() > 1)
        pending_contention.emplace(key, o);
    for (const auto &[key, o] : pending_contention)
      contention(key, *o);
    for (const auto &pass : passes)
      if (!pass.response)
        edge(node("env.source_data", pass.entry, "source/" + pass.entry), pass.held,
             DependencyRule::SourceData);
  }
  std::map<std::string, const SwitchOutputPlan *> pending_contention;

  // Each exclusion names the liveness shape that checks it.
  void exclusions() {
    for (const auto &[si, count] : suppressed)
      excluded.push_back({"X1", "source/" + plan.sources[si].name, count, "L7_single_answer"});
    for (unsigned i = 0; i < plan.targets.size(); ++i) {
      const auto &t = plan.targets[i];
      const auto subject = "target/" + t.name;
      if (t.protocol != Protocol::Axi4)
        excluded.push_back({"X6", subject, 0, "L1_plain"});
      else if (t.outstanding == 1)
        excluded.push_back({"X5", subject, 0, "L3_one_burst"});
      else if (!facts.interleaved[i])
        excluded.push_back({"X4", subject, 0, "L8_merged_contexts"});
      if (!t.read_only && !t.write_only)
        excluded.push_back({"X2", subject, 0, "L2_read_write"});
    }
    std::ranges::transform(plan.sources, std::back_inserter(excluded), [](const auto &s) {
      return DependencyExclusion{"X3", "source/" + s.name, 0, "L2_read_write"};
    });
    if (plan.clock_gating != ClockGating::None)
      excluded.push_back({"X7", "network", 0, "L4_gated_merge"});
  }

  // The transport tables of formal/lean's model: slots, outputs and destinations by number, with
  // the same relations as the edges above. Its coverage reads direct edges or short paths, so a
  // network small enough for that takes direct edges too; a larger one has no tables.
  struct ModelBuild {
    DependencyModel m;
    std::map<std::string, std::uint32_t> slots, ports, dests;
    std::map<std::string, bool> cross;
    std::set<std::tuple<std::uint32_t, std::uint32_t>> holds, ahead;
    std::set<std::uint32_t> head_slots;
    std::map<std::pair<std::uint32_t, std::uint32_t>, std::uint32_t> head_nodes, grant_nodes, route;
    std::map<std::uint32_t, std::set<std::uint32_t>> dests_of;
    std::set<std::pair<std::string, std::string>> by_path;
    std::string omitted;
  };
  static std::uint32_t number(std::map<std::string, std::uint32_t> &table, const std::string &key) {
    return table.try_emplace(key, std::uint32_t(table.size())).first->second;
  }
  void model_tables() {
    ModelBuild b;
    // A destination is its exit, or one path where flows of a target leave a slot by several
    // outputs toward it.
    std::map<std::pair<std::string, std::string>, std::set<std::string>> exits;
    for (const auto &pass : passes)
      exits[{pass.slot_name, pass.exit}].insert(pass.key);
    for (const auto &pass : passes)
      if (exits[{pass.slot_name, pass.exit}].size() > 1)
        b.by_path.insert({pass.response ? "rsp" : "req", pass.exit});
    for (const auto &pass : passes)
      model_pass(b, pass);
    held_at_links(b);
    bound_sources(b);
    // Direct context edges per destination grow with sources squared times targets: too many.
    if (plan.sources.size() * plan.targets.size() > model_endpoint_pairs)
      b.omitted = "more endpoint pairs than the direct context edges carry";
    if (!b.omitted.empty()) {
      model.omitted = b.omitted;
      return;
    }
    finish_model(b);
  }
  // One route's step: its numbers, and whether its slot keeps a grant, ends segments or crosses.
  void model_pass(ModelBuild &b, const Pass &pass) {
    const auto at = number(b.slots, pass.slot_name), out = number(b.ports, pass.key);
    const auto dest =
        number(b.dests, b.by_path.contains({pass.response ? "rsp" : "req", pass.exit})
                            ? pass.path
                            : (pass.response ? "source/" : "target/") + pass.exit + pass.flow);
    pass_dest[&pass] = dest;
    const auto [placed, added] = b.route.try_emplace({at, dest}, out);
    if (placed->second != out)
      b.omitted = "a slot takes several outputs for one destination";
    if (added)
      b.m.routes.push_back({at, dest, out});
    b.head_nodes[{at, dest}] = pass.slot;
    b.grant_nodes[{out, at}] = pass.held;
    b.dests_of[at].insert(dest);
    bool crossing = false;
    if (pass.previous.empty()) {
      if (pass.response)
        crossing = target_slot(b, pass, at, dest);
      b.m.endpoints.push_back(at);
    } else {
      crossing = b.cross[pass.previous];
    }
    b.cross[pass.slot_name] = b.cross[pass.slot_name] || crossing;
    if (!b.ahead.contains({at, dest}) &&
        (!b.cross[pass.slot_name] || !pass.response || holds_across(pass.exit)))
      b.holds.insert({at, dest});
  }
  // An interleaving target ends a segment unless the next word is behind it, except toward a
  // source that keeps a grant across a read. One burst has no other packet to alternate with, yet
  // its pauses end segments all the same where the transport has them. Returns whether it crosses.
  bool target_slot(ModelBuild &b, const Pass &pass, std::uint32_t at, std::uint32_t dest) {
    const auto at_target = plan.target_index(pass.entry);
    const bool interleaves = facts.interleaved[at_target];
    const bool several = plan.targets[at_target].outstanding > 1;
    // Inside a block of several words the next word is the same read's and the grant waits for it,
    // as across a read of a target that does not interleave.
    const bool by_word = interleaves && interleave_words(plan.targets[at_target]) == 1;
    if (by_word && !holds_across(pass.exit) && (several || plan.response_segments)) {
      b.ahead.insert({at, dest});
      b.head_slots.insert(at);
    }
    return interleaves && several && holds_across(pass.exit);
  }
  // A grant may wait across a read at a slot whose words change destination: M3 at link words.
  void held_at_links(const ModelBuild &b) {
    std::map<std::string, std::set<std::string>> outputs_at;
    for (const auto &p : passes)
      if (p.response && !p.previous.empty() && b.cross.at(p.slot_name))
        outputs_at[p.slot_name].insert(p.key);
    // The word of a link is one node: it stalls every grant its slot may hold across a read.
    for (const auto &p : passes)
      if (p.response && !p.previous.empty() && b.cross.at(p.slot_name) && holds_across(p.exit) &&
          outputs_at[p.slot_name].size() > 1)
        edge(p.slot, p.held, DependencyRule::Continuation);
  }
  // A source with one target answering it at a time: a bound destination, one step away.
  void bound_sources(ModelBuild &b) {
    for (const auto &[si, count] : suppressed) {
      const auto &name = plan.sources[si].name;
      const auto it = b.dests.find("source/" + name);
      if (it == b.dests.end()) {
        b.omitted = "a bound source with several paths";
        continue;
      }
      b.m.bound.push_back(it->second);
      for (const auto &pass : passes)
        if (pass.response && pass.exit == name && next_slots.contains(pass.key))
          b.omitted = "a bound source more than one step away";
    }
    std::ranges::sort(b.m.bound);
  }
  void finish_model(ModelBuild &b) {
    auto &m = b.m;
    for (const auto &[key, target] : next_slots)
      m.nexts.push_back({number(b.ports, key), number(b.slots, target)});
    for (const auto &[name, crossing] : b.cross)
      if (crossing)
        m.cross.push_back(number(b.slots, name));
      else if (!b.head_slots.contains(number(b.slots, name)))
        m.contig.push_back(number(b.slots, name));
    m.ahead.assign(b.ahead.begin(), b.ahead.end());
    std::ranges::sort(m.cross);
    std::ranges::sort(m.contig);
    std::ranges::sort(m.endpoints);
    m.endpoints.erase(std::unique(m.endpoints.begin(), m.endpoints.end()), m.endpoints.end());
    m.may_hold.assign(b.holds.begin(), b.holds.end());
    direct_edges(m, b.slots, b.ports, b.head_nodes, b.grant_nodes, b.dests_of);
    for (const auto &[key, n] : b.head_nodes)
      m.heads.push_back({key.first, key.second, n});
    for (const auto &[key, n] : b.grant_nodes)
      m.grants.push_back({key.first, key.second, n});
    m.pool_cap = 1;
    for (const auto &t : plan.targets)
      m.pool_cap = std::max(m.pool_cap, t.outstanding);
    m.present = true;
    model = std::move(m);
    named_numbers(b);
    // The destinations that answer each source's slot.
    for (const auto &pass : passes)
      if (pass.response) {
        const auto s = b.slots.find("source/" + pass.exit);
        const auto d = b.dests.find(b.by_path.contains({"rsp", pass.exit}) ? pass.path
                                                                           : "source/" + pass.exit);
        if (s != b.slots.end() && d != b.dests.end())
          model.answers.push_back({s->second, d->second});
      }
    std::ranges::sort(model.answers);
    model.answers.erase(std::unique(model.answers.begin(), model.answers.end()),
                        model.answers.end());
  }
  // A name's place in the list is its number.
  void named_numbers(const ModelBuild &b) {
    const auto fill = [](auto &list, const std::map<std::string, std::uint32_t> &table) {
      for (const auto &[name, id] : table)
        list.push_back({id, name});
      std::ranges::sort(list, {}, [](const auto &row) { return row.first; });
    };
    fill(model.slots, b.slots);
    fill(model.outputs, b.ports);
    fill(model.destinations, b.dests);
  }
  void direct_edges(DependencyModel &m, std::map<std::string, std::uint32_t> &slots,
                    std::map<std::string, std::uint32_t> &ports,
                    const std::map<std::pair<std::uint32_t, std::uint32_t>, std::uint32_t> &heads,
                    const std::map<std::pair<std::uint32_t, std::uint32_t>, std::uint32_t> &grants,
                    const std::map<std::uint32_t, std::set<std::uint32_t>> &dests_of) {
    std::map<std::uint32_t, std::vector<std::uint32_t>> feeders;
    for (const auto &[key, n] : grants)
      feeders[key.first].push_back(key.second);
    // Contention goes through the helper nodes: each row states one input's helpers, and the
    // checker derives the pairs from the rows.
    std::map<std::uint32_t, std::uint32_t> slot_of_grant;
    std::set<std::uint32_t> shared;
    for (const auto &[key, n] : grants)
      if (!slot_of_grant.try_emplace(n, key.second).second)
        shared.insert(key.first); // Slots of one input: direct edges state these.
    std::set<std::uint32_t> rowed;
    for (const auto &[key, o] : pending_contention) {
      const auto out = ports.find(key);
      if (out == ports.end() || shared.contains(out->second))
        continue;
      std::uint32_t absent = 1000000;
      for (const auto &input : o->inputs) {
        const auto name = key + "/" + input;
        const auto before = index.find({"grant.before", name});
        const auto after = index.find({"grant.after", name});
        const auto other = index.find({"grant.other", name});
        const auto held = index.find({"grant.held", name});
        if (before == index.end() || after == index.end())
          continue;
        if (other == index.end()) {
          shared.insert(out->second); // No helper for this input: bound, or direct edges.
          break;
        }
        const auto holder =
            held == index.end() ? slot_of_grant.end() : slot_of_grant.find(held->second);
        const std::uint32_t at = holder == slot_of_grant.end() ? absent++ : holder->second;
        const std::uint32_t helper = other->second;
        m.contention.push_back({out->second, at, before->second, after->second, helper});
        rowed.insert(out->second);
      }
    }
    std::erase_if(m.contention, [&](const auto &r) { return shared.contains(r[0]); });
    for (const auto o : shared)
      rowed.erase(o);
    for (const auto &[at, dest, out] : m.routes)
      if (!rowed.contains(out) && !std::ranges::binary_search(m.bound, dest))
        for (const auto other : feeders[out])
          if (other != at)
            edge(grants.at({out, other}), heads.at({at, dest}), DependencyRule::Contention);
    // A request's last output takes a context of its target's response slot.
    std::set<std::uint32_t> feeding;
    for (const auto &[out, slot] : m.nexts)
      feeding.insert(out);
    for (const auto &pass : passes) {
      if (pass.response)
        continue;
      const auto out = ports.at(pass.key);
      if (feeding.contains(out))
        continue;
      const auto it = slots.find("target/" + pass.exit + pass.flow);
      if (it == slots.end())
        continue;
      m.pools.push_back({out, pass_dest.at(&pass), it->second});
      for (const auto d : dests_of.at(it->second)) {
        edge(heads.at({it->second, d}), pass.slot, DependencyRule::PoolWait);
        edge(heads.at({it->second, d}), pass.held, DependencyRule::PoolWait);
      }
    }
    std::ranges::sort(m.pools);
    m.pools.erase(std::unique(m.pools.begin(), m.pools.end()), m.pools.end());
    // The front word for another destination stalls a grant held across a read, directly.
    for (const auto &[at, dest, out] : m.routes)
      if (std::ranges::binary_search(m.cross, at) && mayhold(m, at, dest))
        for (const auto &[at2, dest2, out2] : m.routes)
          if (at2 == at && out2 != out)
            edge(heads.at({at, dest2}), grants.at({out, at}), DependencyRule::Continuation);
  }
  static bool mayhold(const DependencyModel &m, std::uint32_t at, std::uint32_t dest) {
    return std::ranges::binary_search(m.may_hold, std::tuple{at, dest});
  }
  DependencyModel model;

  std::uint32_t existing_node(const DependencyNodeRef &ref) const {
    const auto it = index.find({ref.kind, ref.key});
    if (it == index.end())
      throw std::logic_error("No dependency node " + ref.kind + " " + ref.key);
    return it->second;
  }
  void apply_facts() {
    for (const auto &raw : facts.edges) {
      const auto ensure = [&](const DependencyNodeRef &r) { return node(r.kind, r.key); };
      const auto from = ensure(raw.from);
      edge(from, ensure(raw.to), DependencyRule::External);
    }
    for (const auto &escape : facts.escapes) {
      const auto blocked = existing_node(escape.blocked), blocker = existing_node(escape.blocker);
      const auto group = ++groups;
      auto found = false;
      for (auto &e : edges)
        if (e.from == blocker && e.to == blocked) {
          e.group = group;
          found = true;
        }
      if (!found)
        throw std::logic_error("An escape needs an edge from " + escape.blocker.key);
      edges.push_back({existing_node(escape.alternative), blocked, DependencyRule::Escape, group});
    }
  }
  DependencyGraph finish() {
    std::vector<std::uint32_t> order(nodes.size());
    std::iota(order.begin(), order.end(), 0U);
    std::ranges::sort(order, [&](std::uint32_t x, std::uint32_t y) {
      return std::tie(nodes[x].kind, nodes[x].key) < std::tie(nodes[y].kind, nodes[y].key);
    });
    std::vector<std::uint32_t> position(nodes.size());
    DependencyGraph g;
    for (std::uint32_t i = 0; i < order.size(); ++i) {
      position[order[i]] = i;
      g.nodes.push_back(std::move(nodes[order[i]]));
    }
    for (auto &e : edges) {
      e.from = position[e.from];
      e.to = position[e.to];
    }
    const auto key = [](const DependencyEdge &e) {
      return std::tuple{e.from, e.to, static_cast<int>(e.rule), e.group};
    };
    std::ranges::sort(edges, {}, key);
    edges.erase(std::unique(edges.begin(), edges.end()), edges.end());
    g.edges = std::move(edges);
    g.excluded = std::move(excluded);
    for (auto *table : {&model.heads, &model.grants})
      for (auto &row : *table)
        std::get<2>(row) = position[std::get<2>(row)];
    for (auto &row : model.contention)
      for (std::size_t i = 2; i < row.size(); ++i)
        row[i] = position[row[i]];
    g.model = std::move(model);
    return g;
  }
};

// The strongly connected components of nodes not yet live, each of two or more nodes or with a
// self edge, ascending.
class Tarjan {
public:
  Tarjan(const std::vector<std::vector<std::uint32_t>> &next, const std::vector<char> &stuck)
      : next(next), stuck(stuck), number(next.size(), 0), low(next.size(), 0),
        on_stack(next.size(), 0), step(next.size(), 0) {}
  std::vector<std::vector<std::uint32_t>> run() {
    for (std::uint32_t r = 0; r < next.size(); ++r)
      if (stuck[r] && !number[r])
        visit(r);
    std::ranges::sort(out);
    return std::move(out);
  }

private:
  const std::vector<std::vector<std::uint32_t>> &next;
  const std::vector<char> &stuck;
  std::vector<std::uint32_t> number, low;
  std::vector<char> on_stack;
  std::vector<std::size_t> step;
  std::vector<std::uint32_t> stack;
  std::vector<std::vector<std::uint32_t>> out;
  std::uint32_t counter = 0;

  void enter(std::uint32_t u, std::vector<std::uint32_t> &calls) {
    number[u] = low[u] = ++counter;
    stack.push_back(u);
    on_stack[u] = 1;
    calls.push_back(u);
  }
  void visit(std::uint32_t root) {
    std::vector<std::uint32_t> calls;
    enter(root, calls);
    while (!calls.empty()) {
      const auto u = calls.back();
      if (step[u] < next[u].size()) {
        const auto w = next[u][step[u]++];
        if (!stuck[w])
          continue;
        if (!number[w])
          enter(w, calls);
        else if (on_stack[w])
          low[u] = std::min(low[u], number[w]);
        continue;
      }
      calls.pop_back();
      if (!calls.empty())
        low[calls.back()] = std::min(low[calls.back()], low[u]);
      if (low[u] == number[u])
        close(u);
    }
  }
  void close(std::uint32_t u) {
    std::vector<std::uint32_t> component;
    std::uint32_t w = 0;
    do {
      w = stack.back();
      stack.pop_back();
      on_stack[w] = 0;
      component.push_back(w);
    } while (w != u);
    if (component.size() > 1 || std::ranges::find(next[u], u) != next[u].end()) {
      std::ranges::sort(component);
      out.push_back(std::move(component));
    }
  }
};

// The shortest cycle through the lowest node of a component, by breadth first search in edge
// order; the edges kept are those between the component's nodes.
std::vector<std::uint32_t> shortest_cycle(const std::vector<std::vector<std::uint32_t>> &next,
                                          const std::vector<std::uint32_t> &component) {
  std::vector<std::uint32_t> inside(next.size(), 0);
  for (const auto u : component)
    inside[u] = 1;
  std::unordered_map<std::uint32_t, std::uint32_t> before;
  std::deque<std::uint32_t> todo{component.front()};
  while (!todo.empty()) {
    const auto u = todo.front();
    todo.pop_front();
    for (const auto w : next[u]) {
      if (!inside[w])
        continue;
      if (w == component.front()) {
        std::vector<std::uint32_t> cycle{u};
        while (cycle.back() != w)
          cycle.push_back(before.at(cycle.back()));
        std::ranges::reverse(cycle);
        return cycle;
      }
      if (before.try_emplace(w, u).second)
        todo.push_back(w);
    }
  }
  return component;
}

std::string escaped(std::string_view text) {
  std::string out;
  for (const char c : text)
    if (c == '"' || c == '\\')
      out.append({'\\', c});
    else
      out.push_back(c);
  return out;
}
std::string quoted(std::string_view text) { return "\"" + escaped(text) + "\""; }
} // namespace

const DependencyRuleInfo &dependency_rule(DependencyRule rule) {
  return rule_table.at(static_cast<std::size_t>(rule));
}

DependencyFacts dependency_facts(const NetworkPlan &plan) {
  DependencyFacts facts;
  facts.interleaved = interleaved_targets(plan);
  facts.reassembles = reassembling_sources(plan, facts.interleaved);
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    const auto &s = plan.sources[si];
    facts.segment_ends.push_back(!s.clamp || s.outstanding <= 1 || s.outstanding_ids <= 1 ||
                                 facts.reassembles[si]);
  }
  return facts;
}

DependencyGraph build_dependency_graph(const NetworkPlan &plan, const DependencyFacts &facts) {
  return DependencyBuilder(plan, facts).run();
}

DependencyReport check_dependency_graph(DependencyGraph &graph) {
  const auto n = graph.nodes.size();
  // needs[v]: the edges alone plus the groups of v still to be met.
  std::vector<std::uint32_t> needs(n, 0), rank(n, 0);
  std::vector<std::vector<std::uint32_t>> next(n);
  std::vector<char> group_met;
  for (const auto &e : graph.edges) {
    next[e.from].push_back(e.to);
    if (e.group >= group_met.size())
      group_met.resize(e.group + 1, 0);
    if (!e.group)
      ++needs[e.to];
  }
  std::vector<std::vector<std::pair<std::uint32_t, std::uint32_t>>> leaving(n); // to, group
  std::vector<std::vector<std::uint32_t>> group_members(group_met.size());
  for (const auto &e : graph.edges) {
    leaving[e.from].emplace_back(e.to, e.group);
    if (e.group && std::ranges::find(group_members[e.group], e.to) == group_members[e.group].end())
      group_members[e.group].push_back(e.to);
  }
  for (std::uint32_t g = 1; g < group_members.size(); ++g)
    for (const auto v : group_members[g])
      ++needs[v];
  std::vector<std::uint32_t> ready;
  std::vector<char> stuck(n, 1);
  for (std::uint32_t v = 0; v < n; ++v)
    if (!needs[v]) {
      ready.push_back(v);
      stuck[v] = 0;
    }
  for (std::size_t i = 0; i < ready.size(); ++i) {
    const auto u = ready[i];
    for (const auto &[to, group] : leaving[u]) {
      if (group && group_met[group])
        continue;
      if (group)
        group_met[group] = 1;
      rank[to] = std::max(rank[to], rank[u] + 1);
      if (--needs[to] == 0) {
        ready.push_back(to);
        stuck[to] = 0;
      }
    }
  }
  DependencyReport report;
  const auto components = Tarjan(next, stuck).run();
  report.cycle_sets = components.size();
  for (const auto &component : components) {
    if (report.findings.size() == finding_limit)
      break;
    DependencyFinding finding;
    finding.cycle = shortest_cycle(next, component);
    for (std::size_t i = 0; i < finding.cycle.size(); ++i) {
      const auto from = finding.cycle[i], to = finding.cycle[(i + 1) % finding.cycle.size()];
      const auto e =
          std::ranges::lower_bound(graph.edges, std::pair{from, to}, {},
                                   [](const auto &x) { return std::pair{x.from, x.to}; });
      finding.rules.push_back(e->rule);
    }
    finding.level = std::string(dependency_rule(finding.rules.front()).level);
    report.findings.push_back(std::move(finding));
  }
  for (std::uint32_t v = 0; v < n; ++v)
    graph.nodes[v].rank = report.findings.empty() ? rank[v] : 0;
  return report;
}

std::vector<std::string> dependency_explanation(const DependencyGraph &graph,
                                                const DependencyFinding &finding) {
  std::vector<std::string> lines;
  auto has = [&](DependencyRule rule) {
    return std::ranges::find(finding.rules, rule) != finding.rules.end();
  };
  for (std::size_t i = 0; i < finding.cycle.size(); ++i) {
    const auto &n = graph.nodes[finding.cycle[i]];
    if (n.kind.starts_with("target.behind") || n.kind == "grant.before" ||
        n.kind == "grant.after" || n.kind == "grant.other")
      continue;
    lines.push_back(n.kind + " " + n.key);
  }
  if (has(DependencyRule::Continuation))
    lines.push_back("Fix: a response output stays with a target between two words of a read toward "
                    "a source that gathers one read at a time, and that target's next word waits "
                    "behind a word for another output. Lower that source's max_outstanding to 1, "
                    "or reach those targets with max_outstanding 1.");
  else if (has(DependencyRule::External))
    lines.push_back("Fix: remove the dependency outside the network from this loop.");
  else
    lines.push_back("Fix: change the routes or the topology so these waits form no loop.");
  return lines;
}

std::string fnv1a64(std::string_view text) {
  std::uint64_t h = 0xcbf29ce484222325ULL;
  for (const unsigned char c : text) {
    h ^= c;
    h *= 0x100000001b3ULL;
  }
  std::string out = "fnv1a64:";
  for (int shift = 60; shift >= 0; shift -= 4)
    out.push_back("0123456789abcdef"[(h >> shift) & 15]);
  return out;
}

namespace {
void emit_rules(std::string &o) {
  o += " \"rules\": [\n";
  for (std::size_t i = 0; i < rule_table.size(); ++i)
    o += "  {\"id\": " + quoted(rule_table[i].id) + ", \"name\": " + quoted(rule_table[i].name) +
         ", \"level\": " + quoted(rule_table[i].level) + "}" +
         (i + 1 < rule_table.size() ? ",\n" : "\n");
  o += " ],\n";
}
void emit_nodes(std::string &o, const DependencyGraph &g) {
  o += " \"nodes\": [\n";
  for (std::size_t i = 0; i < g.nodes.size(); ++i) {
    const auto &n = g.nodes[i];
    o += "  {\"kind\": " + quoted(n.kind) + ", \"key\": " + quoted(n.key);
    if (!n.component.empty())
      o += ", \"component\": " + quoted(n.component);
    o += ", \"domain\": " + quoted(n.domain);
    if (!n.binding.empty())
      o += ", \"binding\": " + quoted(n.binding);
    o += ", \"rank\": " + std::to_string(n.rank) + "}" + (i + 1 < g.nodes.size() ? ",\n" : "\n");
  }
  o += " ],\n";
}
void emit_edges(std::string &o, const DependencyGraph &g) {
  o += " \"edges\": [\n";
  for (std::size_t i = 0; i < g.edges.size(); ++i) {
    const auto &e = g.edges[i];
    o += "  [" + std::to_string(e.from) + "," + std::to_string(e.to) + "," +
         quoted(dependency_rule(e.rule).id) + "," + std::to_string(e.group) + "]" +
         (i + 1 < g.edges.size() ? ",\n" : "\n");
  }
  o += " ],\n";
}
void emit_excluded(std::string &o, const DependencyGraph &g) {
  o += " \"excluded\": [\n";
  for (std::size_t i = 0; i < g.excluded.size(); ++i) {
    const auto &x = g.excluded[i];
    o += "  {\"rule\": " + quoted(x.rule) + ", \"subject\": " + quoted(x.subject) +
         ", \"edges\": " + std::to_string(x.edges) + ", \"liveness\": " + quoted(x.liveness) + "}" +
         (i + 1 < g.excluded.size() ? ",\n" : "\n");
  }
  o += " ],\n";
}
void emit_environment(std::string &o, const DependencyGraph &g) {
  o += " \"environment\": [\n";
  bool first = true;
  for (std::size_t i = 0; i < g.nodes.size(); ++i)
    if (const auto a = assumption_of(g.nodes[i].kind); !a.empty()) {
      o += std::string(first ? "" : ",\n") + "  {\"node\": " + std::to_string(i) +
           ", \"assumption\": " + quoted(a) + "}";
      first = false;
    }
  o += "\n ],\n";
}
template <class Rows, class Row>
void emit_rows(std::string &o, std::string_view name, const Rows &rows, Row row) {
  o += "  " + quoted(name) + ": [";
  bool first = true;
  for (const auto &r : rows) {
    o += (first ? "" : ",") + row(r);
    first = false;
  }
  o += "],\n";
}
void emit_model(std::string &o, const DependencyModel &m) {
  o += " \"model\": ";
  if (!m.present) {
    o += "null,\n \"model_omitted\": " + quoted(m.omitted) + ",\n";
    return;
  }
  const auto triple = [](const auto &t) {
    return "[" + std::to_string(std::get<0>(t)) + "," + std::to_string(std::get<1>(t)) + "," +
           std::to_string(std::get<2>(t)) + "]";
  };
  const auto pair = [](const auto &t) {
    return "[" + std::to_string(std::get<0>(t)) + "," + std::to_string(std::get<1>(t)) + "]";
  };
  const auto number = [](auto v) { return std::to_string(v); };
  const auto named = [](const auto &t) { return quoted(t.second); };
  o += "{\n  \"cap\": 1,\n  \"pool_cap\": " + std::to_string(m.pool_cap) + ",\n";
  emit_rows(o, "routes", m.routes, triple);
  emit_rows(o, "nexts", m.nexts, pair);
  emit_rows(o, "pools", m.pools, triple);
  emit_rows(o, "endpoints", m.endpoints, number);
  emit_rows(o, "may_hold", m.may_hold, pair);
  emit_rows(o, "contig", m.contig, number);
  emit_rows(o, "cross", m.cross, number);
  emit_rows(o, "bound", m.bound, number);
  emit_rows(o, "ahead", m.ahead, pair);
  emit_rows(o, "answers", m.answers, pair);
  emit_rows(o, "contention", m.contention, [](const auto &r) {
    std::string t = "[";
    for (std::size_t i = 0; i < r.size(); ++i)
      t += (i ? "," : "") + std::to_string(r[i]);
    return t + "]";
  });
  o += "  \"holds\": [";
  bool first = true;
  for (const auto &[a, b, n] : m.heads) {
    o += std::string(first ? "" : ",") + "{\"head\": [" + std::to_string(a) + "," +
         std::to_string(b) + "], \"node\": " + std::to_string(n) + "}";
    first = false;
  }
  for (const auto &[a, b, n] : m.grants) {
    o += std::string(first ? "" : ",") + "{\"grant\": [" + std::to_string(a) + "," +
         std::to_string(b) + "], \"node\": " + std::to_string(n) + "}";
    first = false;
  }
  o += "],\n  \"hints\": [],\n";
  emit_rows(o, "slots", m.slots, named);
  emit_rows(o, "outputs", m.outputs, named);
  emit_rows(o, "destinations", m.destinations, named);
  o.erase(o.size() - 2);
  o += "\n },\n";
}
void emit_result(std::string &o, const DependencyGraph &g, const DependencyReport &r) {
  o += " \"result\": {\n  \"status\": ";
  o += quoted(r.findings.empty() ? "no_cycle" : "cycle");
  o += ",\n  \"cycle_sets\": " + std::to_string(r.cycle_sets) + ",\n  \"findings\": [";
  for (std::size_t i = 0; i < r.findings.size(); ++i) {
    const auto &f = r.findings[i];
    o += std::string(i ? ",\n" : "\n") + "   {\"level\": " + quoted(f.level) + ", \"cycle\": [";
    for (std::size_t k = 0; k < f.cycle.size(); ++k)
      o += (k ? "," : "") + std::to_string(f.cycle[k]);
    o += "], \"rules\": [";
    for (std::size_t k = 0; k < f.rules.size(); ++k)
      o += (k ? "," : "") + quoted(dependency_rule(f.rules[k]).id);
    o += "], \"explanation\": [";
    const auto lines = dependency_explanation(g, f);
    for (std::size_t k = 0; k < lines.size(); ++k)
      o += (k ? "," : "") + quoted(lines[k]);
    o += "]}";
  }
  o += r.findings.empty() ? "]\n }\n" : "\n  ]\n }\n";
}
} // namespace

std::string dependency_graph_json(const DependencyGraph &graph, const DependencyReport &report,
                                  std::string_view module, std::string_view generator,
                                  std::string_view rtl_fingerprint,
                                  std::string_view plan_fingerprint) {
  std::string o = "{\n \"format\": \"nocloom.dependency\",\n \"format_version\": 1,\n";
  o += " \"generator\": " + quoted(generator) + ",\n \"module\": " + quoted(module) + ",\n";
  o += " \"orientation\": \"blocker_to_blocked\",\n";
  o += " \"fingerprints\": {\"rtl\": " + quoted(rtl_fingerprint) +
       ", \"plan\": " + quoted(plan_fingerprint) + "},\n";
  emit_rules(o);
  emit_nodes(o, graph);
  emit_edges(o, graph);
  emit_excluded(o, graph);
  emit_environment(o, graph);
  emit_model(o, graph.model);
  emit_result(o, graph, report);
  o += "}\n";
  return o;
}

Status dependency_gate(const DependencyGraph &graph, const DependencyReport &report,
                       bool rejecting) {
  if (!rejecting || report.findings.empty())
    return Status::success({});
  std::vector<Diagnostic> out;
  for (const auto &finding : report.findings) {
    auto diagnostic = error("NCL_DEPENDENCY_CYCLE", "network.dependency",
                            "The waits of the generated network form a cycle");
    diagnostic.notes = dependency_explanation(graph, finding);
    out.push_back(std::move(diagnostic));
  }
  return Status::failure(std::move(out));
}
} // namespace nocloom::detail
