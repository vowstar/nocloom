#pragma once
// Probes of the public generation result: the netlist text and the map JSON.
#include "graph_example_definition.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <cctype>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/generation.hpp>
#include <numeric>
#include <set>
#include <sstream>
#include <string>
#include <vector>

struct PublicArtifacts {
  std::string rtl;
  nlohmann::json map;
  // Declared width of every port and net, and the operands of every continuous assignment.
  std::map<std::string, unsigned> widths;
  std::set<std::string> inputs, outputs;
  std::map<std::string, std::vector<std::string>> operands;

  PublicArtifacts(std::string text, nlohmann::json json)
      : rtl(std::move(text)), map(std::move(json)) {
    const auto names = [](const std::string &s) {
      std::vector<std::string> out;
      for (std::size_t at = s.find("nl_"); at != std::string::npos; at = s.find("nl_", at)) {
        auto end = at;
        while (end < s.size() &&
               (std::isalnum(static_cast<unsigned char>(s[end])) || s[end] == '_'))
          ++end;
        out.push_back(s.substr(at, end - at));
        at = end;
      }
      return out;
    };
    std::istringstream lines(rtl);
    for (std::string line; std::getline(lines, line);) {
      if (line.starts_with("assign ")) {
        const auto equal = line.find(" = ");
        REQUIRE(equal != std::string::npos);
        operands[line.substr(7, equal - 7)] = names(line.substr(equal + 3));
        continue;
      }
      const bool input = line.starts_with("  input "), output = line.starts_with("  output ");
      if (!input && !output && !line.starts_with("wire ") && !line.starts_with("reg "))
        continue;
      const auto declared = names(line);
      if (declared.empty())
        continue;
      unsigned width = 1;
      if (const auto open = line.find('['); open != std::string::npos)
        width = std::stoul(line.substr(open + 1)) + 1;
      widths[declared.front()] = width;
      if (input)
        inputs.insert(declared.front());
      if (output)
        outputs.insert(declared.front());
    }
  }
  // The declared width of a port, or of the net a map value ("value/N") names.
  unsigned bits(const std::string &name) const {
    const auto net = name.starts_with("value/") ? "nl_internal_v" + name.substr(6) : name;
    const auto it = widths.find(net);
    if (it == widths.end())
      FAIL("no declaration of " << net);
    return it->second;
  }
  bool has_port(const std::string &name) const {
    return std::ranges::any_of(map["ports"], [&](const auto &p) { return p["name"] == name; });
  }
  unsigned port_width(const std::string &name) const {
    const auto &ports = map["ports"];
    const auto it = std::ranges::find_if(ports, [&](const auto &p) { return p["name"] == name; });
    if (it == ports.end())
      FAIL("no port " << name);
    return (*it)["width"].template get<unsigned>();
  }
  // The entry of network/<list> whose `key` is `name`; null when absent.
  const nlohmann::json *entry(const char *list, const char *key, const std::string &name) const {
    const auto &entries = map["network"][list];
    const auto it = std::ranges::find_if(entries, [&](const auto &e) { return e[key] == name; });
    return it == entries.end() ? nullptr : &*it;
  }
  // The map entry of a network resource, or of the storage bound to a resource.
  const nlohmann::json *resource(const std::string &name) const {
    return entry("resources", "name", name);
  }
  const nlohmann::json *storage(const std::string &name) const {
    return entry("storage", "resource", name);
  }
  // Total declared bits of a storage binding's "payload" or "control" values.
  unsigned storage_bits(const std::string &name, const char *part) const {
    const auto *s = storage(name);
    REQUIRE(s != nullptr);
    return std::accumulate((*s)[part].begin(), (*s)[part].end(), 0U,
                           [&](unsigned sum, const auto &value) {
                             return sum + bits(value.template get<std::string>());
                           });
  }
  // The widths a named field takes in the clock crossing FIFOs; empty when no cut carries it.
  std::set<unsigned> cut_field_widths(const std::string &field) const {
    std::set<unsigned> out;
    if (!map["network"].contains("cdc"))
      return out;
    for (const auto &cut : map["network"]["cdc"]["cuts"])
      for (const auto &f : cut["fields"])
        if (f["name"] == field)
          out.insert(f["width"].get<unsigned>());
    return out;
  }
  // The transport word bytes at which an endpoint attaches: the output of the last width adapter
  // on the segment into it (the link, or its receiver side <link>/link-out), or zero when no
  // adapter leads there.
  unsigned endpoint_bytes(const std::string &endpoint) const {
    const nlohmann::json *last = nullptr;
    for (const auto &a : map["network"]["adapters"])
      if (const auto segment = a["segment"].get<std::string>();
          segment.ends_with(">" + endpoint) || segment.ends_with(">" + endpoint + "/link-out"))
        last = &a; // the map lists a segment's hops in order
    if (!last)
      return 0;
    return ((*last)["kind"] == "serializer" ? (*last)["narrow_bytes"] : (*last)["wide_bytes"])
        .get<unsigned>();
  }
  // For every output port, the input ports that reach it through continuous assignments only
  // (registers cut every path).
  std::map<std::string, std::set<std::string>> combinational_inputs() const {
    std::map<std::string, std::set<std::string>> memo;
    std::set<std::string> visiting;
    const auto reach = [&](const auto &self, const std::string &net) -> std::set<std::string> {
      if (inputs.contains(net))
        return {net};
      if (const auto it = memo.find(net); it != memo.end())
        return it->second;
      const auto driver = operands.find(net);
      if (driver == operands.end())
        return {};
      REQUIRE(visiting.insert(net).second);
      std::set<std::string> found;
      for (const auto &operand : driver->second) {
        const auto sub = self(self, operand);
        found.insert(sub.begin(), sub.end());
      }
      visiting.erase(net);
      return memo[net] = std::move(found);
    };
    std::map<std::string, std::set<std::string>> out;
    for (const auto &port : outputs)
      out[port] = reach(reach, port);
    return out;
  }
};

inline std::string artifact_text(const nocloom::ArtifactSet &set, const std::string &suffix) {
  const auto it = std::ranges::find_if(set, [&](const auto &a) {
    return a.path.ends_with(suffix) && a.path != "nocloom_clock_gate.v";
  });
  REQUIRE(it != set.end());
  return it->text;
}

// Generates through the public API and requires success.
inline PublicArtifacts public_artifacts(const nocloom::Definition &d) {
  const auto generated = nocloom::generate(d);
  for (const auto &diagnostic : generated.diagnostics())
    UNSCOPED_INFO(diagnostic.code << " " << diagnostic.field << " " << diagnostic.message);
  REQUIRE(generated);
  return {artifact_text(generated.value(), ".v"),
          nlohmann::json::parse(artifact_text(generated.value(), ".map.json"))};
}

// True when the result failed with the given diagnostic code.
template <typename T> bool rejected_with(const nocloom::Result<T> &r, const std::string &code) {
  for (const auto &d : r.diagnostics())
    UNSCOPED_INFO(d.code << " " << d.field);
  return !r && std::ranges::any_of(r.diagnostics(), [&](const auto &d) { return d.code == code; });
}

// A copy whose first target runs in clock domain "io" while every switch stays in "main", so that
// the map lists the crossing FIFOs and the domain of each resource.
inline nocloom::Definition target_domain(nocloom::Definition d) {
  d.clock_domains.push_back({"io", "clk_io", "reset_n", "test"});
  d.targets[0].clock_domain = "io";
  for (auto *graph : {&d.topology->request, &d.topology->response})
    for (auto &node : graph->nodes)
      node.clock_domain = "main";
  return d;
}

// The public three-source graph (graph_example_definition.hpp), or the same graph cut down to s0
// and t0 without the other leaves and the tail.
inline nocloom::Definition graph_example_endpoints(unsigned count) {
  auto d = graph_example_definition();
  if (count == 3)
    return d;
  REQUIRE(count == 1);
  d.initiators.resize(1);
  d.targets.resize(1);
  const auto gone = [](const std::string &name) {
    return name == "leaf1" || name == "leaf2" || name == "tail";
  };
  auto &request = d.topology->request;
  std::erase_if(request.nodes, [&](const auto &n) { return gone(n.name); });
  std::erase_if(request.links, [&](const auto &l) { return gone(l.from) || gone(l.to); });
  for (auto *graph : {&d.topology->request, &d.topology->response}) {
    std::erase_if(graph->entries, [](const auto &a) { return !a.endpoint.ends_with('0'); });
    std::erase_if(graph->exits, [](const auto &a) { return !a.endpoint.ends_with('0'); });
  }
  return d;
}
