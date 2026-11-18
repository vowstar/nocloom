#include "core/switch_graph.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <functional>
#include <nocloom/config.hpp>
#include <nocloom/generation.hpp>
#include <random>

// The route cycle report (docs/network.md#route-cycle-report). An exhaustive search over the
// routes of each fixture gives the expected report: every cycle, its name, its waits and the
// routes of each wait.
namespace {
using namespace nocloom;
using namespace nocloom::detail;
using Link = std::pair<std::string, std::string>;
// One route as the report names it, with its switches.
struct Trip {
  std::string name;
  std::vector<std::string> nodes;
};
std::string cycle_name(const std::vector<Link> &cycle) {
  std::string name;
  for (const auto &link : cycle)
    name += link.first + " -> ";
  return name + cycle.front().first;
}
// Every cycle of waits by exhaustive search, each from its lowest link, in report order.
std::vector<std::vector<Link>> all_cycles(const std::vector<Trip> &trips) {
  std::map<Link, std::set<Link>> waits;
  for (const auto &trip : trips)
    for (std::size_t i = 2; i < trip.nodes.size(); ++i)
      waits[{trip.nodes[i - 2], trip.nodes[i - 1]}].insert({trip.nodes[i - 1], trip.nodes[i]});
  std::vector<std::vector<Link>> cycles;
  for (const auto &[start, unused] : waits) {
    std::vector<Link> stack{start};
    const std::function<void()> extend = [&] {
      const auto at = waits.find(stack.back());
      if (at == waits.end())
        return;
      for (const auto &next : at->second) {
        if (next == start)
          cycles.push_back(stack);
        if (next > start && std::ranges::find(stack, next) == stack.end()) {
          stack.push_back(next);
          extend();
          stack.pop_back();
        }
      }
    };
    extend();
  }
  std::ranges::sort(cycles, [](const auto &a, const auto &b) {
    return std::pair{a.size(), cycle_name(a)} < std::pair{b.size(), cycle_name(b)};
  });
  return cycles;
}
std::vector<Diagnostic> expected_report(const std::vector<Trip> &trips,
                                        const std::string &direction,
                                        const ValidationOptions &limits = {}) {
  const auto cycles = all_cycles(trips);
  std::vector<Diagnostic> report;
  for (std::size_t c = 0; c < cycles.size() && c < limits.cycle_limit; ++c) {
    const auto &cycle = cycles[c];
    Diagnostic d{"NCL_SWITCH_ROUTE_CYCLE",
                 "switches.edges",
                 "Routes chain the links of the cycle " + cycle_name(cycle) +
                     "; no deadlock claim is made",
                 {},
                 {}};
    for (std::size_t i = 0; i < cycle.size(); ++i) {
      const auto &held = cycle[i], &next = cycle[(i + 1) % cycle.size()];
      std::set<std::string> names;
      for (const auto &trip : trips)
        for (std::size_t k = 2; k < trip.nodes.size(); ++k)
          if (trip.nodes[k - 2] == held.first && trip.nodes[k - 1] == held.second &&
              trip.nodes[k] == next.second)
            names.insert(trip.name);
      REQUIRE_FALSE(names.empty());
      std::string note = held.first + " -> " + held.second + " waits for " + next.first + " -> " +
                         next.second + " in " + std::to_string(names.size()) + " " + direction +
                         (names.size() == 1 ? " route" : " routes");
      std::size_t listed = 0;
      for (const auto &name : names)
        if (limits.contributor_limit == 0 || listed < limits.contributor_limit)
          note += (listed++ ? ", " : ": ") + name;
      if (listed < names.size())
        note += " and " + std::to_string(names.size() - listed) + " more";
      d.notes.push_back(note);
    }
    report.push_back(d);
  }
  if (cycles.size() > limits.cycle_limit)
    report.back().notes.push_back("more " + direction + " cycles are present; the cycle limit is " +
                                  std::to_string(limits.cycle_limit));
  return report;
}
void same_report(const std::vector<Diagnostic> &got, const std::vector<Diagnostic> &want) {
  REQUIRE(got.size() == want.size());
  for (std::size_t i = 0; i < got.size(); ++i) {
    CHECK(got[i].code == want[i].code);
    CHECK(got[i].field == want[i].field);
    CHECK(got[i].message == want[i].message);
    CHECK(got[i].notes == want[i].notes);
  }
}
std::string n(const char *prefix, unsigned i) { return prefix + std::to_string(i); }
// A one-way ring of switches n0, n1 and so on. Initiator s<i> and target t<i> attach to switch i,
// and connected(i, j) admits s<i> to t<j>. A response ring carries t<j> back to s<i>. Each
// connected pair takes the one path around the ring.
struct Ring {
  DirectedSwitchGraph graph;
  std::vector<Trip> trips;
};
Ring ring(unsigned size, const std::function<bool(unsigned, unsigned)> &connected,
          bool response = false) {
  Ring r;
  r.graph.response = response;
  r.graph.admitted_pairs.emplace();
  for (unsigned i = 0; i < size; ++i) {
    r.graph.nodes.push_back(n("n", i));
    r.graph.edges.push_back({n("n", i), n("n", (i + 1) % size)});
    (response ? r.graph.exits : r.graph.entries).push_back({n("s", i), n("n", i)});
    (response ? r.graph.entries : r.graph.exits).push_back({n("t", i), n("n", i)});
    for (unsigned j = 0; j < size; ++j) {
      if (!connected(i, j))
        continue;
      r.graph.admitted_pairs->push_back(response ? SwitchPair{n("t", j), n("s", i)}
                                                 : SwitchPair{n("s", i), n("t", j)});
      Trip trip{n("s", i) + " to " + n("t", j), {}};
      for (unsigned at = response ? j : i;; at = (at + 1) % size) {
        trip.nodes.push_back(n("n", at));
        if (at == (response ? i : j))
          break;
      }
      r.trips.push_back(trip);
    }
  }
  return r;
}
// A graph from explicit routes alone: route k runs between its own initiator s<k> and its own
// target t<k> over the given switches, from the target in a response graph.
Ring routed(const std::vector<std::vector<std::string>> &paths, bool response = false) {
  Ring r;
  r.graph.response = response;
  r.graph.admitted_pairs.emplace();
  std::set<std::string> nodes;
  std::set<Link> links;
  for (unsigned k = 0; k < paths.size(); ++k) {
    const auto &path = paths[k];
    nodes.insert(path.begin(), path.end());
    for (std::size_t i = 1; i < path.size(); ++i)
      links.emplace(path[i - 1], path[i]);
    const auto entry = n(response ? "t" : "s", k), exit = n(response ? "s" : "t", k);
    r.graph.entries.push_back({entry, path.front()});
    r.graph.exits.push_back({exit, path.back()});
    r.graph.admitted_pairs->push_back({entry, exit});
    r.graph.fixed_routes.push_back({entry, exit, path});
    r.trips.push_back({n("s", k) + " to " + n("t", k), path});
  }
  r.graph.nodes.assign(nodes.begin(), nodes.end());
  for (const auto &[from, to] : links)
    r.graph.edges.push_back({from, to});
  return r;
}
// A ring of stages. Stage i has a hub c<i> and two switches a<i> and b<i>, and each of them
// leads to the hub of the next stage. Routes chain every turn, so a cycle picks a or b at each
// stage: 2^stages cycles of one length. A cycle can also go around twice and take the other
// switch of each stage the second time: 2^(stages - 1) cycles of twice that length.
Ring ladder(unsigned stages, bool response = false) {
  std::vector<std::vector<std::string>> paths;
  char label[16];
  const auto name = [&](char kind, unsigned stage) {
    std::snprintf(label, sizeof label, "%c%02u", kind, stage % stages);
    return std::string(label);
  };
  for (unsigned i = 0; i < stages; ++i)
    for (const char x : {'a', 'b'})
      for (const char y : {'a', 'b'})
        paths.push_back({name('c', i), name(x, i), name('c', i + 1), name(y, i + 1)});
  return routed(paths, response);
}
std::vector<Diagnostic> report_of(const DirectedSwitchGraph &graph,
                                  const ValidationOptions &limits = {}) {
  const auto planned = plan_switch_graph(graph, limits);
  REQUIRE_FALSE(planned);
  return planned.diagnostics();
}
const auto every = [](unsigned, unsigned) { return true; };
} // namespace

TEST_CASE("A route cycle report names the routes of every wait", "[route-cycles]") {
  SECTION("one route for each wait of a request ring") {
    const auto r = ring(4, [](unsigned i, unsigned j) { return j == (i + 2) % 4; });
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 1);
    CHECK(report[0].message == "Routes chain the links of the cycle n0 -> n1 -> n2 -> n3 -> n0; "
                               "no deadlock claim is made");
    CHECK(report[0].notes ==
          std::vector<std::string>{"n0 -> n1 waits for n1 -> n2 in 1 request route: s0 to t2",
                                   "n1 -> n2 waits for n2 -> n3 in 1 request route: s1 to t3",
                                   "n2 -> n3 waits for n3 -> n0 in 1 request route: s2 to t0",
                                   "n3 -> n0 waits for n0 -> n1 in 1 request route: s3 to t1"});
    same_report(report, expected_report(r.trips, "request"));
  }
  SECTION("several routes for each wait, in the byte order of their names") {
    const auto r = ring(4, every);
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes.front() ==
          "n0 -> n1 waits for n1 -> n2 in 3 request routes: s0 to t2, s0 to t3, s3 to t2");
    same_report(report, expected_report(r.trips, "request"));
  }
  SECTION("a response ring writes the initiator first") {
    const auto r = ring(4, [](unsigned i, unsigned j) { return i == (j + 2) % 4; }, true);
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes ==
          std::vector<std::string>{"n0 -> n1 waits for n1 -> n2 in 1 response route: s2 to t0",
                                   "n1 -> n2 waits for n2 -> n3 in 1 response route: s3 to t1",
                                   "n2 -> n3 waits for n3 -> n0 in 1 response route: s0 to t2",
                                   "n3 -> n0 waits for n0 -> n1 in 1 response route: s1 to t3"});
    same_report(report, expected_report(r.trips, "response"));
  }
  SECTION("a route that takes a wait twice counts once") {
    const auto r = routed({{"n0", "n1", "n2", "n0", "n1", "n2"}});
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes ==
          std::vector<std::string>{"n0 -> n1 waits for n1 -> n2 in 1 request route: s0 to t0",
                                   "n1 -> n2 waits for n2 -> n0 in 1 request route: s0 to t0",
                                   "n2 -> n0 waits for n0 -> n1 in 1 request route: s0 to t0"});
  }
  SECTION("a link back into its switch keeps its message and has no note") {
    auto r = ring(4, every);
    r.graph.edges.push_back({"n1", "n1"});
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 1);
    CHECK(report[0].message == "The link of the cycle n1 -> n1 returns to its node: a route over "
                               "it passes the node twice");
    CHECK(report[0].notes.empty());
  }
}

TEST_CASE("A route cycle report names the flows of a route", "[route-cycles]") {
  // Two flows cross a -> b and c -> d in opposite orders: neither route has a cycle alone.
  DirectedSwitchGraph g{{"a", "b", "c", "d", "u", "v"},
                        {{"a", "b"},
                         {"b", "c"},
                         {"b", "v"},
                         {"c", "d"},
                         {"d", "a"},
                         {"d", "v"},
                         {"u", "a"},
                         {"u", "c"}},
                        {{"s0", "u"}},
                        {{"t0", "v"}}};
  const std::vector<std::string> first{"u", "a", "b", "c", "d", "v"},
      second{"u", "c", "d", "a", "b", "v"};
  SECTION("initiator flows") {
    g.flows = {{"s0", 2}};
    g.fixed_routes = {{"s0", "t0", first, {0}}, {"s0", "t0", second, {1}}};
    const auto report = report_of(g);
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes ==
          std::vector<std::string>{"a -> b waits for b -> c in 1 request route: s0 flow 0 to t0",
                                   "b -> c waits for c -> d in 1 request route: s0 flow 0 to t0",
                                   "c -> d waits for d -> a in 1 request route: s0 flow 1 to t0",
                                   "d -> a waits for a -> b in 1 request route: s0 flow 1 to t0"});
    // When the second flow leaves at d, no wait closes the cycle.
    g.fixed_routes[1].nodes = {"u", "c", "d", "v"};
    g.edges.erase(g.edges.begin() + 4);
    g.edges.erase(g.edges.begin() + 2);
    CHECK(plan_switch_graph(g));
  }
  SECTION("target flows") {
    g.target_paths = {{"t0", 2}};
    g.fixed_routes = {{"s0", "t0", first}, {"s0", "t0", second}};
    g.fixed_routes[0].target_path = 0;
    g.fixed_routes[1].target_path = 1;
    const auto report = report_of(g);
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes ==
          std::vector<std::string>{"a -> b waits for b -> c in 1 request route: s0 to t0 flow 0",
                                   "b -> c waits for c -> d in 1 request route: s0 to t0 flow 0",
                                   "c -> d waits for d -> a in 1 request route: s0 to t0 flow 1",
                                   "d -> a waits for a -> b in 1 request route: s0 to t0 flow 1"});
  }
  SECTION("each flow pair of one path is a route, in both directions") {
    for (const bool response : {false, true}) {
      // Request: s0 reaches t2 over n0 -> n1 -> n2. Response: t1 answers s0 over n1 -> n2 -> n0.
      auto r = ring(
          3, [&](unsigned i, unsigned j) { return response ? i == (j + 2) % 3 : j == (i + 2) % 3; },
          response);
      const std::string target = response ? "t1" : "t2";
      r.graph.flows = {{"s0", 2}};
      r.graph.target_paths = {{target, 2}};
      const auto report = report_of(r.graph);
      REQUIRE(report.size() == 1);
      REQUIRE(report[0].notes.size() == 3);
      CHECK(report[0].notes[response ? 1 : 0] ==
            (response ? "n1 -> n2 waits for n2 -> n0 in 4 response routes: "
                      : "n0 -> n1 waits for n1 -> n2 in 4 request routes: ") +
                ("s0 flow 0 to " + target + " flow 0, s0 flow 0 to " + target +
                 " flow 1, s0 flow 1 to " + target + " flow 0, s0 flow 1 to " + target +
                 " flow 1"));
      CHECK(report[0].notes[response ? 0 : 1] ==
            (response ? "n0 -> n1 waits for n1 -> n2 in 1 response route: s2 to t0"
                      : "n1 -> n2 waits for n2 -> n0 in 1 request route: s1 to t0"));
    }
  }
}

TEST_CASE("Removing contributors keeps or removes a wait", "[route-cycles]") {
  const std::set<std::pair<unsigned, unsigned>> first{{0, 2}, {0, 3}, {3, 2}};
  // One contributor less: the wait stays with the others.
  const auto less =
      ring(4, [](unsigned i, unsigned j) { return std::pair{i, j} != std::pair{0U, 2U}; });
  const auto report = report_of(less.graph);
  REQUIRE(report.size() == 1);
  CHECK(report[0].notes.front() ==
        "n0 -> n1 waits for n1 -> n2 in 2 request routes: s0 to t3, s3 to t2");
  same_report(report, expected_report(less.trips, "request"));
  // No contributor of the first wait: the only cycle is gone and the graph is accepted.
  const auto none = ring(4, [&](unsigned i, unsigned j) { return !first.contains({i, j}); });
  CHECK(all_cycles(none.trips).empty());
  CHECK(plan_switch_graph(none.graph));
}

TEST_CASE("A route cycle report orders cycles by length, then by name", "[route-cycles]") {
  SECTION("the shorter of two rings comes first, whatever its name") {
    const auto r = routed({{"a0", "a1", "a2"},
                           {"a1", "a2", "a3"},
                           {"a2", "a3", "a4"},
                           {"a3", "a4", "a0"},
                           {"a4", "a0", "a1"},
                           {"x0", "x1", "x2"},
                           {"x1", "x2", "x0"},
                           {"x2", "x0", "x1"}});
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 2);
    CHECK(report[0].message.find("cycle x0 -> x1 -> x2 -> x0;") != std::string::npos);
    CHECK(report[1].message.find("cycle a0 -> a1 -> a2 -> a3 -> a4 -> a0;") != std::string::npos);
    CHECK(report[0].notes.size() == 3);
    CHECK(report[1].notes.size() == 5);
    same_report(report, expected_report(r.trips, "request"));
  }
  SECTION("cycles of one length follow their names: n10 is before n2, and a before a_b") {
    const auto r = routed({{"n2", "n3", "n2", "n3"},
                           {"n10", "n1", "n10", "n1"},
                           {"a_b", "z", "a_b", "z"},
                           {"a", "z2", "a", "z2"}});
    const auto report = report_of(r.graph);
    REQUIRE(report.size() == 4);
    CHECK(report[0].message.find("cycle a -> z2 -> a;") != std::string::npos);
    CHECK(report[1].message.find("cycle a_b -> z -> a_b;") != std::string::npos);
    CHECK(report[2].message.find("cycle n1 -> n10 -> n1;") != std::string::npos);
    CHECK(report[3].message.find("cycle n2 -> n3 -> n2;") != std::string::npos);
    same_report(report, expected_report(r.trips, "request"));
  }
  SECTION("cycles that share links are each reported once") {
    // Two loops through hub -> x, and a longer one around both.
    const auto r = routed({{"hub", "x", "p", "hub", "x"},
                           {"hub", "x", "q", "hub", "x"},
                           {"x", "p", "q"},
                           {"p", "q", "hub"},
                           {"q", "p"},
                           {"x", "q", "p", "hub"}});
    for (const unsigned limit : {1U, 2U, 3U, 4U, 5U, 8U, 1024U}) {
      const ValidationOptions limits{limit, 8};
      same_report(report_of(r.graph, limits), expected_report(r.trips, "request", limits));
    }
    CHECK(all_cycles(r.trips).size() == 4);
  }
}

TEST_CASE("The cycle limit bounds a route cycle report", "[route-cycles]") {
  const auto small = ladder(3);
  REQUIRE(all_cycles(small.trips).size() == 12);
  SECTION("the default reports four cycles and a note without a count") {
    const auto report = report_of(small.graph);
    REQUIRE(report.size() == 4);
    for (std::size_t i = 0; i + 1 < report.size(); ++i)
      CHECK(report[i].notes.size() == 6);
    REQUIRE(report.back().notes.size() == 7);
    CHECK(report.back().notes.back() == "more request cycles are present; the cycle limit is 4");
    CHECK(report[0].message.find("cycle a00 -> c01 -> a01 -> c02 -> a02 -> c00 -> a00;") !=
          std::string::npos);
    CHECK(report[1].message.find("cycle a00 -> c01 -> a01 -> c02 -> b02 -> c00 -> a00;") !=
          std::string::npos);
    same_report(report, expected_report(small.trips, "request"));
  }
  SECTION("every limit reports the first cycles, and the note only when more are present") {
    for (const unsigned limit : {1U, 2U, 3U, 5U, 8U, 9U, 11U, 12U, 13U, 1024U}) {
      const ValidationOptions limits{limit, 8};
      const auto report = report_of(small.graph, limits);
      CHECK(report.size() == std::min(limit, 12U));
      CHECK(report.back().notes.size() == (limit <= 8 ? 6U : 12U) + (limit < 12 ? 1U : 0U));
      CHECK(report.back().notes.back().starts_with("more request cycles") == (limit < 12));
      same_report(report, expected_report(small.trips, "request", limits));
    }
  }
  SECTION("a response direction names itself in the note") {
    const auto back = ladder(3, true);
    const auto report = report_of(back.graph, {2, 8});
    REQUIRE(report.size() == 2);
    CHECK(report.back().notes.back() == "more response cycles are present; the cycle limit is 2");
    CHECK(report[0].notes.front() ==
          "a00 -> c01 waits for c01 -> a01 in 1 response route: s0 to t0");
    same_report(report, expected_report(back.trips, "response", {2, 8}));
  }
  SECTION("a graph of more than 2^40 cycles reports the first ones") {
    // The cycles cannot all be visited within the time limit of this test.
    const auto large = ladder(40);
    const auto report = report_of(large.graph);
    const auto many = report_of(large.graph, {1024, 8});
    REQUIRE(report.size() == 4);
    REQUIRE(many.size() == 1024);
    CHECK(many.back().notes.back() == "more request cycles are present; the cycle limit is 1024");
    // Every cycle has 80 links. The first takes a at every stage. The next ones take b at the
    // last stages, as a binary count does.
    for (unsigned k = 0; k < 1024; ++k) {
      std::string name;
      char label[16];
      for (unsigned stage = 0; stage < 40; ++stage) {
        const bool b = stage >= 30 && ((k >> (39 - stage)) & 1U);
        std::snprintf(label, sizeof label, "%c%02u -> c%02u -> ", b ? 'b' : 'a', stage,
                      (stage + 1) % 40);
        name += label;
      }
      CHECK(many[k].message ==
            "Routes chain the links of the cycle " + name + "a00; no deadlock claim is made");
      CHECK(many[k].notes.size() == (k == 1023 ? 81U : 80U));
      if (k < 4)
        CHECK(report[k].message == many[k].message);
    }
  }
}

TEST_CASE("The contributor limit bounds a note and keeps the total", "[route-cycles]") {
  const auto r = ring(16, every);
  const auto note = [&](unsigned limit) {
    const auto report = report_of(r.graph, {4, limit});
    REQUIRE(report.size() == 1);
    REQUIRE(report[0].notes.size() == 16);
    same_report(report, expected_report(r.trips, "request", {4, limit}));
    return report[0].notes.front();
  };
  const std::string head = "n0 -> n1 waits for n1 -> n2 in 105 request routes: ";
  CHECK(report_of(r.graph)[0].notes.front() ==
        head + "s0 to t10, s0 to t11, s0 to t12, s0 to t13, s0 to t14, s0 to t15, s0 to t2, "
               "s0 to t3 and 97 more");
  CHECK(note(8) == report_of(r.graph)[0].notes.front());
  CHECK(note(1) == head + "s0 to t10 and 104 more");
  CHECK(note(3) == head + "s0 to t10, s0 to t11, s0 to t12 and 102 more");
  CHECK(note(104).ends_with("s9 to t7 and 1 more"));
  CHECK(note(105).ends_with("s9 to t7, s9 to t8"));
  CHECK(note(106) == note(105));
  CHECK(note(0) == note(105));
  CHECK(std::ranges::count(note(0), ',') == 104);
}

TEST_CASE("A route cycle report does not depend on the declaration order", "[route-cycles]") {
  std::mt19937 random(20261117);
  const auto shuffled = [&](DirectedSwitchGraph g) {
    const auto mix = [&](auto &list) {
      for (std::size_t i = list.size(); i > 1; --i)
        std::swap(list[i - 1], list[random() % i]);
    };
    mix(g.nodes);
    mix(g.edges);
    mix(g.entries);
    mix(g.exits);
    mix(*g.admitted_pairs);
    mix(g.fixed_routes);
    return g;
  };
  SECTION("fixed fixtures") {
    for (const auto &r : {ring(4, every), ring(16, every), ladder(3), ladder(8)}) {
      const auto report = report_of(r.graph);
      for (unsigned round = 0; round < 4; ++round)
        same_report(report_of(shuffled(r.graph)), report);
    }
  }
  SECTION("random routes agree with the exhaustive search at every limit") {
    const std::vector<std::string> names{"a", "a_b", "n1", "n10", "n2", "z"};
    unsigned cyclic = 0;
    for (unsigned graph = 0; graph < 200; ++graph) {
      std::vector<std::vector<std::string>> paths(2 + random() % 5);
      for (auto &path : paths)
        for (unsigned hops = 2 + random() % 4; path.size() < hops;)
          if (const auto &next = names[random() % (3 + graph % 4)];
              path.empty() || path.back() != next)
            path.push_back(next);
      const auto r = routed(paths);
      const auto cycles = all_cycles(r.trips);
      const auto planned = plan_switch_graph(r.graph);
      REQUIRE(static_cast<bool>(planned) == cycles.empty());
      if (planned)
        continue;
      ++cyclic;
      for (const unsigned limit : {1U, 2U, 4U, 1024U})
        for (const unsigned routes : {0U, 1U, 8U}) {
          const ValidationOptions limits{limit, routes};
          const auto want = expected_report(r.trips, "request", limits);
          same_report(report_of(r.graph, limits), want);
          same_report(report_of(shuffled(r.graph), limits), want);
        }
    }
    CHECK(cyclic > 50);
  }
}

namespace {
// AXI4 initiators s<i> and targets t<i>. A direction that is a ring has one-way rings of size
// switches each, n0, n1 and so on, with endpoint i at switch i. Any other direction has one switch.
std::string ring_yaml(unsigned size, const std::string &connectivity, bool request_ring,
                      bool response_ring, unsigned rings = 1) {
  std::string yaml = "schema_version: 10\nname: rings\naddress_width: 32\n"
                     "reset_ports: [{name: reset_n}]\ntest_ports: [{name: test}]\n"
                     "clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, "
                     "test_port: test}]\ninitiators:\n";
  for (unsigned i = 0; i < size * rings; ++i)
    yaml += "  - {name: " + n("s", i) +
            ", clock_domain: main, data_width: 32, protocol: axi4, len_width: 8}\n";
  yaml += "targets:\n";
  for (unsigned i = 0; i < size * rings; ++i)
    yaml += "  - {name: " + n("t", i) +
            ", clock_domain: main, data_width: 32, protocol: axi4, len_width: 8, attributes: amba, "
            "mappings: [{base: '0x" +
            std::to_string(i) + "0000', size: '0x10000'}]}\n";
  yaml +=
      "transport: {max_outstanding: 1, transport_word_bytes: 4}\nconnectivity: " + connectivity +
      "\ntopology:\n";
  for (const bool response : {false, true}) {
    const bool is_ring = response ? response_ring : request_ring;
    std::string nodes, links, entries, exits;
    for (unsigned i = 0; i < size * rings; ++i) {
      const auto node = is_ring ? n("n", i) : "hub";
      if (is_ring || i == 0)
        nodes += (nodes.empty() ? "{name: " : ", {name: ") + node + "}";
      if (is_ring)
        links += (i ? ", {from: " : "{from: ") + node +
                 ", to: " + n("n", i - i % size + (i + 1) % size) + "}";
      const auto source =
                     (i ? ", {endpoint: " : "{endpoint: ") + n("s", i) + ", node: " + node + "}",
                 sink = (i ? ", {endpoint: " : "{endpoint: ") + n("t", i) + ", node: " + node + "}";
      (response ? exits : entries) += source;
      (response ? entries : exits) += sink;
    }
    yaml += std::string(response ? "  response:\n" : "  request:\n") + "    nodes: [" + nodes +
            "]\n    links: [" + links + "]\n    entries: [" + entries + "]\n    exits: [" + exits +
            "]\n";
  }
  return yaml + "output: {backend: verilog, language: verilog_2001}\n";
}
Definition parsed(const std::string &yaml) {
  auto result = config::parse(yaml);
  REQUIRE(result);
  return result.value().definition;
}
std::vector<Diagnostic> rejected(const Definition &d, const ValidationOptions &limits = {}) {
  GenerationOptions options;
  options.validation = limits;
  const auto status = validate(d, options);
  REQUIRE_FALSE(status);
  const auto plan = make_plan(d, nullptr, limits);
  REQUIRE_FALSE(plan);
  REQUIRE_FALSE(generate(d, options));
  same_report(plan.diagnostics(), status.diagnostics());
  same_report(generate(d, options).diagnostics(), status.diagnostics());
  return status.diagnostics();
}
const std::string forward = "{s0: [t2], s1: [t3], s2: [t0], s3: [t1]}";
} // namespace

TEST_CASE("Validation keeps the notes of a route cycle report", "[route-cycles]") {
  SECTION("a request ring and a response ring report in their own fields") {
    const auto report = rejected(parsed(ring_yaml(4, forward, true, true)));
    REQUIRE(report.size() == 2);
    CHECK(report[0].code == "NCL_SWITCH_ROUTE_CYCLE");
    CHECK(report[0].field == "topology.request.links");
    CHECK(report[0].message == "Routes chain the links of the cycle n0 -> n1 -> n2 -> n3 -> n0; "
                               "no deadlock claim is made");
    CHECK(report[0].notes ==
          std::vector<std::string>{"n0 -> n1 waits for n1 -> n2 in 1 request route: s0 to t2",
                                   "n1 -> n2 waits for n2 -> n3 in 1 request route: s1 to t3",
                                   "n2 -> n3 waits for n3 -> n0 in 1 request route: s2 to t0",
                                   "n3 -> n0 waits for n0 -> n1 in 1 request route: s3 to t1"});
    CHECK(report[1].field == "topology.response.links");
    CHECK(report[1].notes ==
          std::vector<std::string>{"n0 -> n1 waits for n1 -> n2 in 1 response route: s2 to t0",
                                   "n1 -> n2 waits for n2 -> n3 in 1 response route: s3 to t1",
                                   "n2 -> n3 waits for n3 -> n0 in 1 response route: s0 to t2",
                                   "n3 -> n0 waits for n0 -> n1 in 1 response route: s1 to t3"});
  }
  SECTION("the source location of a diagnostic stays on the links") {
    auto result = config::parse(ring_yaml(4, forward, true, false), "rings.yaml");
    REQUIRE(result);
    const auto status =
        validate(result.value().definition, result.value().options, &result.value().sources);
    REQUIRE_FALSE(status);
    REQUIRE(status.diagnostics().size() == 1);
    REQUIRE(status.diagnostics()[0].location);
    CHECK(status.diagnostics()[0].location->label == "rings.yaml");
    CHECK(status.diagnostics()[0].notes.size() == 4);
  }
  SECTION("removing one contributor keeps the wait, removing all accepts the definition") {
    auto report = rejected(parsed(ring_yaml(4, "all", true, false)));
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes.front() ==
          "n0 -> n1 waits for n1 -> n2 in 3 request routes: s0 to t2, s0 to t3, s3 to t2");
    report = rejected(parsed(ring_yaml(
        4, "{s0: [t0, t1, t3], s1: [t0, t1, t2, t3], s2: [t0, t1, t2, t3], s3: [t0, t1, t2, t3]}",
        true, false)));
    REQUIRE(report.size() == 1);
    CHECK(report[0].notes.front() ==
          "n0 -> n1 waits for n1 -> n2 in 2 request routes: s0 to t3, s3 to t2");
    const auto open = parsed(
        ring_yaml(4, "{s0: [t0, t1], s1: [t0, t1, t2, t3], s2: [t0, t1, t2, t3], s3: [t0, t1, t3]}",
                  true, false));
    CHECK(validate(open));
    CHECK(generate(open));
  }
  SECTION("declaration order does not change the report") {
    const auto base = parsed(ring_yaml(4, "all", true, true));
    auto reordered = base;
    std::ranges::reverse(reordered.initiators);
    std::ranges::reverse(reordered.targets);
    for (auto *graph : {&reordered.topology->request, &reordered.topology->response}) {
      std::ranges::reverse(graph->nodes);
      std::ranges::reverse(graph->links);
      std::ranges::reverse(graph->entries);
      std::ranges::reverse(graph->exits);
    }
    same_report(rejected(reordered), rejected(base));
    CHECK(rejected(base).size() == 2);
  }
}

TEST_CASE("The validation options set both limits of the report", "[route-cycles]") {
  CHECK(ValidationOptions{}.cycle_limit == 4);
  CHECK(ValidationOptions{}.contributor_limit == 8);
  CHECK(GenerationOptions{}.validation.cycle_limit == 4);
  CHECK(GenerationOptions{}.validation.contributor_limit == 8);
  const auto rings = parsed(ring_yaml(4, forward, true, true));
  const auto wide = parsed(ring_yaml(16, "all", true, false));
  SECTION("the cycle limit applies to each direction") {
    auto report = rejected(rings, {1, 8});
    REQUIRE(report.size() == 2);
    CHECK(report[0].notes.size() == 4);
    CHECK(report[1].notes.size() == 4);
    // Two request rings of three switches: both cycles at the default, one and the note at 1.
    const auto two = parsed(ring_yaml(
        3, "{s0: [t2], s1: [t0], s2: [t1], s3: [t5], s4: [t3], s5: [t4]}", true, false, 2));
    report = rejected(two);
    REQUIRE(report.size() == 2);
    CHECK(report[0].message.find("cycle n0 -> n1 -> n2 -> n0;") != std::string::npos);
    CHECK(report[1].message.find("cycle n3 -> n4 -> n5 -> n3;") != std::string::npos);
    CHECK(report[1].field == "topology.request.links");
    CHECK(report[1].notes ==
          std::vector<std::string>{"n3 -> n4 waits for n4 -> n5 in 1 request route: s3 to t5",
                                   "n4 -> n5 waits for n5 -> n3 in 1 request route: s4 to t3",
                                   "n5 -> n3 waits for n3 -> n4 in 1 request route: s5 to t4"});
    report = rejected(two, {1, 8});
    REQUIRE(report.size() == 1);
    REQUIRE(report[0].notes.size() == 4);
    CHECK(report[0].notes.back() == "more request cycles are present; the cycle limit is 1");
    CHECK(rejected(two, {2, 8}).back().notes.size() == 3);
  }
  SECTION("the contributor limit at its default, at another value and at 0") {
    const std::string head = "n0 -> n1 waits for n1 -> n2 in 105 request routes: ";
    CHECK(rejected(wide)[0].notes.front() ==
          head + "s0 to t10, s0 to t11, s0 to t12, s0 to t13, s0 to t14, s0 to t15, s0 to t2, "
                 "s0 to t3 and 97 more");
    CHECK(rejected(wide, {4, 2})[0].notes.front() == head + "s0 to t10, s0 to t11 and 103 more");
    const auto all = rejected(wide, {4, 0})[0].notes.front();
    CHECK(std::ranges::count(all, ',') == 104);
    CHECK(all.find(" more") == std::string::npos);
  }
  SECTION("a cycle limit outside 1 to 1024 is rejected, with or without a cycle") {
    const auto open =
        parsed(ring_yaml(4, "{s0: [t0], s1: [t1], s2: [t2], s3: [t3]}", false, false));
    REQUIRE(validate(open));
    for (const auto *d : {&rings, &open})
      for (const unsigned limit : {0U, 1025U, 4000000000U}) {
        const auto report = rejected(*d, {limit, 8});
        REQUIRE(report.size() == 1);
        CHECK(report[0].code == "NCL_VALIDATION_OPTION");
        CHECK(report[0].field == "validation.cycle_limit");
        CHECK(report[0].message == "The cycle limit must be 1 to 1024");
      }
    for (const unsigned limit : {1U, 1024U}) {
      GenerationOptions options;
      options.validation = {limit, 4000000000U};
      CHECK(validate(open, options));
      CHECK(rejected(rings, options.validation).size() == 2);
    }
  }
  SECTION("an accepted definition generates the same artifacts at every limit") {
    const auto open = parsed(
        ring_yaml(4, "{s0: [t0, t1], s1: [t0, t1, t2, t3], s2: [t0, t1, t2, t3], s3: [t0, t1, t3]}",
                  true, false));
    const auto base = generate(open);
    REQUIRE(base);
    for (const ValidationOptions &limits :
         {ValidationOptions{1, 0}, ValidationOptions{1024, 1}, ValidationOptions{7, 100}}) {
      GenerationOptions options;
      options.validation = limits;
      const auto other = generate(open, options);
      REQUIRE(other);
      CHECK(other.value() == base.value());
    }
  }
}
