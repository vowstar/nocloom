#include "diagnostic.hpp"
#include "switch_graph.hpp"
#include <algorithm>
#include <iterator>
#include <limits>

namespace nocloom::detail {
namespace {
// The waits of one direction over link numbers. A link has its position in the links sorted by
// (from, to), so the order of the numbers is the byte order of the names.
struct WaitGraph {
  std::vector<std::set<unsigned>> next, into;
};
// A cycle of waits as its links in order, from its lowest link. Cycles of one length compare as
// their names do: a name is the nodes of these links in order.
using Cycle = std::vector<unsigned>;
// The first cycle of one part of the cycles that start at a link. The part has the cycles that
// start with the links up to fork and do not continue with a barred link.
struct Candidate {
  Cycle links;
  std::size_t fork = 0;
  std::set<unsigned> barred = {};
  bool operator<(const Candidate &other) const {
    return links.size() != other.links.size() ? links.size() < other.links.size()
                                              : links < other.links;
  }
};
constexpr auto unreached = std::numeric_limits<unsigned>::max();
// The shortest cycle that starts with the given links, passes only higher links than its first and
// does not continue with a barred link. Among the shortest it is the first by name: a walk back
// from the first link gives every link its distance, and each step takes the lowest link of the
// least distance.
std::optional<Cycle> first_cycle(const WaitGraph &g, Cycle links,
                                 const std::set<unsigned> &barred) {
  const unsigned root = links.front();
  std::vector<unsigned> distance(g.next.size(), unreached);
  std::vector<bool> held(g.next.size());
  for (const unsigned link : links)
    held[link] = true;
  distance[root] = 0;
  for (std::vector<unsigned> wave{root}; !wave.empty();) {
    std::vector<unsigned> before;
    for (const unsigned link : wave)
      for (const unsigned from : g.into[link])
        if (from > root && !held[from] && distance[from] == unreached) {
          distance[from] = distance[link] + 1;
          before.push_back(from);
        }
    wave = std::move(before);
  }
  for (bool fork = true;; fork = false) {
    std::optional<unsigned> step;
    for (const unsigned to : g.next[links.back()])
      if (distance[to] != unreached && !(fork && barred.contains(to)) &&
          (!step || distance[to] < distance[*step]))
        step = to;
    if (!step)
      return std::nullopt;
    if (*step == root)
      return links;
    links.push_back(*step);
  }
}
// The first cycles in report order, one more than the limit when the graph has more. Each found
// cycle splits its part into the parts that leave it after each of its links, so no cycle is
// found twice and only the first cycle of a part is computed. The open candidates never exceed
// the cycles still wanted. With L links, W waits, limit K and C links in the longest reported
// cycle, this takes at most L + (K + 1) * C walks of O(L + W) steps and O(K * L + W) memory.
std::vector<Cycle> first_cycles(const WaitGraph &g, unsigned limit) {
  std::set<Candidate> open;
  std::vector<Cycle> found;
  const auto add = [&](std::optional<Cycle> cycle, std::size_t fork, std::set<unsigned> barred) {
    if (cycle)
      open.insert({std::move(*cycle), fork, std::move(barred)});
    if (open.size() > limit + 1 - found.size())
      open.erase(std::prev(open.end()));
  };
  for (unsigned link = 0; link < g.next.size(); ++link)
    if (!g.next[link].empty() && !g.into[link].empty())
      add(first_cycle(g, {link}, {}), 0, {});
  while (!open.empty() && found.size() <= limit) {
    const auto top = std::move(open.extract(open.begin()).value());
    found.push_back(top.links);
    for (std::size_t i = top.fork; found.size() <= limit && i < top.links.size(); ++i) {
      auto barred = i == top.fork ? top.barred : std::set<unsigned>{};
      barred.insert(top.links[(i + 1) % top.links.size()]);
      const auto last = top.links.begin() + static_cast<std::ptrdiff_t>(i) + 1;
      add(first_cycle(g, {top.links.begin(), last}, barred), i, barred);
    }
  }
  return found;
}
// The names of the flow pairs that a route carries, initiator first in both directions.
std::vector<std::string> flow_pairs(const DirectedSwitchGraph &g, const SwitchRoute &route) {
  const auto &initiator = g.response ? route.exit : route.entry;
  const auto target = " to " + (g.response ? route.entry : route.exit) +
                      (route.target_path ? " flow " + std::to_string(*route.target_path) : "");
  if (route.flows.empty())
    return {initiator + target};
  std::vector<std::string> names;
  std::ranges::transform(route.flows, std::back_inserter(names), [&](unsigned flow) {
    return initiator + " flow " + std::to_string(flow) + target;
  });
  return names;
}
} // namespace

std::vector<Diagnostic> route_cycle_report(const SwitchGraphPlan &plan,
                                           const ValidationOptions &limits) {
  const auto &links = plan.graph.edges;
  const auto number = [&](const std::string &from, const std::string &to) {
    return static_cast<unsigned>(
        std::ranges::lower_bound(links, std::tie(from, to), {},
                                 [](const SwitchEdge &e) { return std::tie(e.from, e.to); }) -
        links.begin());
  };
  WaitGraph g{std::vector<std::set<unsigned>>(links.size()),
              std::vector<std::set<unsigned>>(links.size())};
  for (const auto &route : plan.routes)
    for (std::size_t i = 2; i < route.nodes.size(); ++i) {
      const auto held = number(route.nodes[i - 2], route.nodes[i - 1]);
      const auto next = number(route.nodes[i - 1], route.nodes[i]);
      g.next[held].insert(next);
      g.into[next].insert(held);
    }
  const unsigned limit = std::max(limits.cycle_limit, 1U);
  const auto cycles = first_cycles(g, limit);
  const auto reported = std::min<std::size_t>(cycles.size(), limit);
  // The contributors of each reported wait: the flow pairs whose route takes both links in turn.
  std::map<std::pair<unsigned, unsigned>, std::set<std::string>> contributors;
  for (std::size_t c = 0; c < reported; ++c)
    for (std::size_t i = 0; i < cycles[c].size(); ++i)
      contributors[{cycles[c][i], cycles[c][(i + 1) % cycles[c].size()]}];
  for (const auto &route : plan.routes)
    for (std::size_t i = 2; i < route.nodes.size(); ++i)
      if (const auto at = contributors.find({number(route.nodes[i - 2], route.nodes[i - 1]),
                                             number(route.nodes[i - 1], route.nodes[i])});
          at != contributors.end())
        std::ranges::copy(flow_pairs(plan.graph, route),
                          std::inserter(at->second, at->second.end()));
  const std::string direction = plan.graph.response ? "response" : "request";
  std::vector<Diagnostic> report;
  for (std::size_t c = 0; c < reported; ++c) {
    const auto &cycle = cycles[c];
    auto &diagnostic = report.emplace_back(error("NCL_SWITCH_ROUTE_CYCLE", "switches.edges", ""));
    std::string loop;
    for (std::size_t i = 0; i < cycle.size(); ++i) {
      const auto &held = links[cycle[i]], &next = links[cycle[(i + 1) % cycle.size()]];
      const auto &routes = contributors.at({cycle[i], cycle[(i + 1) % cycle.size()]});
      const auto listed = limits.contributor_limit == 0
                              ? routes.size()
                              : std::min<std::size_t>(routes.size(), limits.contributor_limit);
      // The text after "in" gives the cause of the wait; a route is the only cause.
      auto note = held.from + " -> " + held.to + " waits for " + next.from + " -> " + next.to +
                  " in " + std::to_string(routes.size()) + " " + direction +
                  (routes.size() == 1 ? " route" : " routes");
      auto name = routes.begin();
      for (std::size_t k = 0; k < listed; ++k)
        note += (k ? ", " : ": ") + *name++;
      if (listed < routes.size())
        note += " and " + std::to_string(routes.size() - listed) + " more";
      diagnostic.notes.push_back(std::move(note));
      loop += held.from + " -> ";
    }
    diagnostic.message = "Routes chain the links of the cycle " + loop + links[cycle[0]].from +
                         "; no deadlock claim is made";
  }
  if (cycles.size() > reported)
    report.back().notes.push_back("more " + direction + " cycles are present; the cycle limit is " +
                                  std::to_string(limit));
  return report;
}
} // namespace nocloom::detail
