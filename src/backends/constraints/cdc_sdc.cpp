#include "backends/constraints/cdc_sdc.hpp"
#include "backends/clock_period_json.hpp"
#include "backends/output_license.hpp"
#include "core/diagnostic.hpp"
#include <algorithm>
#include <map>
#include <nlohmann/json.hpp>
#include <optional>
#include <set>
#include <stdexcept>

namespace nocloom::detail {
namespace {
using Json = nlohmann::json;

// One exception pair (set_max_delay and set_min_delay 0) from cells to the endpoints of a clock.
struct Exception {
  std::string label;
  std::vector<std::string> from;
  std::string to_domain;
  std::string bound;
};

std::string period(const std::string &domain) { return "$nocloom_period_" + domain; }
std::string nanoseconds(ClockPeriod ps) { return ps.decimal(1000, ps.denominator() == 1 ? 3 : 6); }

std::string max_delay(ClockPeriod ps) {
  constexpr std::uint64_t whole_ns_threshold_ps = (std::uint64_t{1} << 32) * 1000;
  return ps > ClockPeriod{whole_ns_threshold_ps} ? ps.decimal(1000, 0) : nanoseconds(ps);
}

class CdcConstraints {
public:
  CdcConstraints(const Json &map, std::string top) : map_(map), top_(std::move(top)) {
    for (const auto &r : cdc().at("registers"))
      registers_.emplace(r.at("value").get<std::string>(), &r);
    const auto &sync = map_.contains("cells") ? map_.at("cells").at("synchronizer") : Json();
    if (sync.contains("module"))
      bound_stages_ = sync.at("stages").get<unsigned>();
    for (const auto &domain : map_.at("definition").at("clock_domains"))
      domains_.emplace(domain.at("name").get<std::string>(), &domain);
  }
  const Json &cdc() const { return map_.at("network").at("cdc"); }

  std::string sdc() const {
    std::vector<std::string> body;
    std::set<std::string> crossing_domains;
    std::set<std::string> related_roots;
    std::set<std::pair<std::string, std::string>> async_pairs;
    bool related = false;
    for (const auto &cut : cdc().at("cuts")) {
      const auto from = cut.at("from_domain").get<std::string>();
      const auto to = cut.at("to_domain").get<std::string>();
      crossing_domains.insert({from, to});
      const auto kind = cut.value("kind", std::string("async"));
      if (kind != "async") {
        related = true;
        const auto root = domains_.at(from)->at("clock_root").get<std::string>();
        related_roots.insert(root);
        const auto spacing = edge_spacing(from, to);
        const auto input = physical_root_period(root);
        const auto bound =
            input ? max_delay(*input * spacing)
                  : "[expr {" + root_period(root) + " * " + std::to_string(spacing) + "}]";
        std::vector<Exception> list;
        if (kind == "sync") {
          list = {
              {"write count, related buffered return", cells(cut.at("write_count")), to, bound},
              {"read count, related buffered return", cells(cut.at("read_count")), from, bound}};
        } else {
          if (cut.contains("admission_open"))
            list.push_back({"EdgeAligned admission, related window",
                            cells(cut.at("admission_open")),
                            cut.at("slow_from_sender").get<bool>() ? from : to, bound});
        }
        Exception slots{"related payload, minimum distinct edge spacing", {}, to, bound};
        for (const auto &field : cut.at("fields"))
          for (const auto &slot : field.at("storage"))
            slots.from.push_back(register_cells(slot.get<std::string>()));
        list.push_back(std::move(slots));
        body.push_back("\n# " + cut.at("name").get<std::string>() + ": " + kind + " " + from +
                       " to " + to + "; related paths remain timed");
        for (const auto &e : list) {
          const auto source = e.from.empty() ? " -from [get_clocks {" + from + "}]"
                                             : " -from [get_cells {" + join(e.from) + "}]";
          const auto ends =
              " -ignore_clock_latency" + source + " -to [get_clocks {" + e.to_domain + "}]";
          body.push_back("# " + e.label);
          body.push_back("set_max_delay " + e.bound + ends);
          body.push_back("set_min_delay 0" + ends);
        }
        continue;
      }
      async_pairs.emplace(from, to);
      const auto stages = cut.at("synchronizer_stages").get<unsigned>();
      body.push_back("\n# " + cut.at("name").get<std::string>() + ": " + from + " to " + to + ", " +
                     std::to_string(stages) + " synchronizer stages");
      const auto sender = read_clock_period(*domains_.at(from));
      const auto receiver = read_clock_period(*domains_.at(to));
      const auto gray = sender && receiver
                            ? max_delay(std::min(*sender, *receiver))
                            : "[expr {min(" + period(from) + ", " + period(to) + ")}]";
      std::vector<Exception> list{{"write count, min(T_" + from + ", T_" + to + ")",
                                   cells(cut.at("write_count")), to, gray},
                                  {"read count, min(T_" + from + ", T_" + to + ")",
                                   cells(cut.at("read_count")), from, gray}};
      Exception slots{"payload slots, " + std::to_string(stages) + " * T_" + to,
                      {},
                      to,
                      receiver ? max_delay(*receiver * stages)
                               : "[expr {" + std::to_string(stages) + " * " + period(to) + "}]"};
      for (const auto &field : cut.at("fields"))
        for (const auto &slot : field.at("storage"))
          slots.from.push_back(register_cells(slot.get<std::string>()));
      list.push_back(std::move(slots));
      if (cut.value("reset_handshake", false))
        for (const auto &[side, own, peer, peer_period] :
             {std::tuple{"sender", from, to, receiver}, std::tuple{"receiver", to, from, sender}})
          for (const auto *chain : {"announce_sync", "acknowledge_sync"}) {
            const auto &first = registers_.at(cut.at("handshake").at(side).at(chain).at(0));
            if (first->at("synchronizer").at("stage") != 0)
              throw std::runtime_error("handshake chain does not start at stage 0");
            list.push_back({std::string(side) + " " + chain + ", T_" + peer,
                            cells(first->at("next")), own,
                            peer_period ? max_delay(*peer_period) : period(peer)});
          }
      for (const auto &e : list) {
        const auto ends = " -ignore_clock_latency -from [get_cells {" + join(e.from) +
                          "}] -to [get_clocks {" + e.to_domain + "}]";
        body.push_back("# " + e.label);
        body.push_back("set_max_delay " + e.bound + ends);
        body.push_back("set_min_delay 0" + ends);
      }
      for (const auto &o : cut.at("obligations"))
        if (o.at("kind") == "one_side_reset_hold")
          body.push_back("# Not a path: reset " + o.at("bound").get<std::string>() + ".");
    }
    std::string out = "# Generated by NocLoom\n# Clock-domain crossing timing exceptions of " +
                      top_ +
                      ".\n# Read after one clock per domain exists, named after the domain. Times "
                      "are in ns.\n# Register cells are named <register>_reg[<bit>]; keep the "
                      "cells of " +
                      top_ +
                      ".cdc_preserve.txt.\n# Reset synchronizer chains have no exception: check "
                      "recovery and removal.\nset sdc_version 2.1\n\n";
    std::string groups = "set_clock_groups -asynchronous -allow_paths";
    for (const auto &root : related_roots)
      out += root_period_setting(root);
    for (const auto &domain : map_.at("definition").at("clock_domains")) {
      const auto name = domain.at("name").get<std::string>();
      if (!crossing_domains.contains(name))
        continue;
      out += domain_period_setting(domain);
      if (!related)
        groups += " -group [get_clocks {" + name + "}]";
    }
    if (related) {
      groups = "# Related-clock paths are not in asynchronous clock groups.";
      for (const auto &[from, to] : async_pairs)
        groups += "\nset_clock_groups -asynchronous -allow_paths -group [get_clocks {" + from +
                  "}] -group [get_clocks {" + to + "}]";
    }
    out += "# Crossing paths stay timed; the exceptions below replace their clock relation.\n" +
           groups + "\n";
    for (const auto &line : body)
      out += line + "\n";
    return output_license_comment("#") + out;
  }

  std::string preserve() const {
    std::string out =
        "# Generated by NocLoom\n# Cells of " + top_ +
        " that synthesis must keep: no register merging, no removal and no\n# boundary "
        "optimization of these synchronizer instances and registers.\n";
    std::set<std::string> seen;
    for (const auto &r : cdc().at("registers"))
      if (r.contains("synchronizer") && r.at("synchronizer").at("stage") == 0)
        for (const auto &instance : r.at("synchronizer").at("instances"))
          if (seen.insert(instance.get<std::string>()).second)
            out += "instance " + instance.get<std::string>() + "\n";
    for (const auto &cut : cdc().at("cuts")) {
      if (cut.value("kind", std::string("async")) != "async")
        continue;
      std::vector<std::string> values{cut.at("write_count"), cut.at("read_count")};
      if (cut.value("reset_handshake", false))
        for (const auto *side : {"sender", "receiver"})
          values.push_back(cut.at("handshake").at(side).at("announce"));
      for (const auto &v : values)
        out += "register " + register_cells(v) + "\n";
    }
    return output_license_comment("#") + out;
  }

private:
  static std::string root_period(const std::string &root) { return "$nocloom_root_period_" + root; }

  std::vector<const Json *> clock_path(const std::string &name) const {
    std::vector<const Json *> path;
    for (auto at = name; !at.empty(); at = domains_.at(at)->value("edge_parent", std::string()))
      path.push_back(domains_.at(at));
    std::ranges::reverse(path);
    return path;
  }

  std::uint64_t edge_spacing(const std::string &from, const std::string &to) const {
    const auto a = clock_path(from), z = clock_path(to);
    const auto sync_reset = map_.at("definition").value("register_reset", "") == "synchronous";
    const auto variable = [&](const Json *d) {
      return d->contains("enable_port") ||
             (d->value("ratio", std::uint64_t{1}) > 1 &&
              (d->contains("test_port") || d->contains("ungate_port") || sync_reset));
    };
    std::uint64_t common = 1;
    std::size_t shared = 0;
    while (shared < a.size() && shared < z.size() && a[shared] == z[shared] &&
           !variable(a[shared])) {
      common *= a[shared]->value("ratio", std::uint64_t{1});
      ++shared;
    }
    // Differing release edges can shift the phase of otherwise fixed sibling dividers.
    std::set<std::pair<std::string, bool>> releases;
    const auto tail = [&](const auto &path) -> std::optional<std::uint64_t> {
      auto ratio = common;
      for (std::size_t i = shared; i < path.size(); ++i) {
        const auto *d = path[i];
        if (variable(d))
          return std::nullopt;
        const auto level = d->value("ratio", std::uint64_t{1});
        if (level > 1)
          releases.emplace(d->at("reset_port").template get<std::string>(),
                           d->value("reset_synchronizer", true));
        ratio *= level;
      }
      return ratio;
    };
    const auto a_ratio = tail(a), z_ratio = tail(z);
    return a_ratio && z_ratio && releases.size() <= 1 ? std::gcd(*a_ratio, *z_ratio) : common;
  }

  std::optional<ClockPeriod> physical_root_period(const std::string &name) const {
    const auto &definition = map_.at("definition");
    const auto roots = definition.find("clock_roots");
    if (roots == definition.end())
      return std::nullopt;
    const auto root =
        std::ranges::find_if(*roots, [&](const Json &record) { return record.at("name") == name; });
    return root == roots->end() ? std::nullopt : read_clock_period(*root);
  }

  std::string root_period_setting(const std::string &root) const {
    const auto variable = root_period(root).substr(1);
    if (const auto ps = physical_root_period(root))
      return "set " + variable + " " + nanoseconds(*ps) + "\n";
    return "# Set " + variable + " (physical root period in ns) before reading this file.\n";
  }

  std::string domain_period_setting(const Json &d) const {
    const auto name = d.at("name").get<std::string>();
    const auto variable = "nocloom_period_" + name;
    if (const auto ps = read_clock_period(d))
      return "set " + variable + " " + nanoseconds(*ps) + "\n";
    return "# " + name + " has no period_ps: set " + variable + " (ns) before reading this file.\n";
  }

  static std::string join(const std::vector<std::string> &names) {
    std::string out;
    for (const auto &n : names)
      out += (out.empty() ? "" : " ") + n;
    return out;
  }
  std::string register_cells(const std::string &value) const {
    return registers_.at(value)->at("rtl").get<std::string>() + "_reg*";
  }
  // The cells of one register: its bits, or its synchronizer stage in every bit's instance.
  std::vector<std::string> cells(const Json &value) const {
    const auto &record = *registers_.at(value.get<std::string>());
    if (!record.contains("synchronizer"))
      return {register_cells(value.get<std::string>())};
    const auto stage = std::to_string(record.at("synchronizer").at("stage").get<unsigned>());
    const auto cell = bound_stages_ == 0   ? "/stage_reg[" + stage + "]"
                      : bound_stages_ == 1 ? "/u_stage[" + stage + "].u_cell"
                                           : std::string("/u_cell");
    const auto &instances = record.at("synchronizer").at("instances");
    std::vector<std::string> out(instances.size());
    std::ranges::transform(instances, out.begin(), [&](const Json &instance) {
      return instance.get<std::string>() + cell;
    });
    return out;
  }

  const Json &map_;
  std::string top_;
  std::map<std::string, const Json *> registers_;
  std::map<std::string, const Json *> domains_;
  unsigned bound_stages_ = 0;
};

Diagnostic internal(const std::string &message) {
  return error("NCL_INTERNAL_MAP", "output.cdc_constraints", message);
}
} // namespace

Result<ArtifactSet> add_cdc_constraints(ArtifactSet artifacts) {
  auto bundle = std::ranges::find_if(
      artifacts, [](const Artifact &a) { return a.path.ends_with(".bundle.json"); });
  if (bundle == artifacts.end())
    return Result<ArtifactSet>::failure(internal("The artifacts have no bundle"));
  try {
    auto manifest = Json::parse(bundle->text);
    const auto top = manifest.at("top").get<std::string>();
    const auto mapping = std::ranges::find(artifacts, top + ".map.json", &Artifact::path);
    if (mapping == artifacts.end())
      return Result<ArtifactSet>::failure(internal("The artifacts have no map"));
    const auto map = Json::parse(mapping->text);
    const auto *network = map.contains("network") ? &map.at("network") : nullptr;
    if (!network || !network->contains("cdc") || network->at("cdc").at("cuts").empty())
      return Result<ArtifactSet>::failure(
          error("NCL_UNSUPPORTED_BACKEND", "output.cdc_constraints",
                "CDC constraints require a clocked network with at least one crossing"));
    CdcConstraints constraints(map, top);
    const std::string sdc = top + ".cdc.sdc", keep = top + ".cdc_preserve.txt";
    manifest.at("files").push_back({{"path", sdc}, {"role", "cdc_constraints"}});
    manifest.at("files").push_back({{"path", keep}, {"role", "cdc_preserve_list"}});
    bundle->text = manifest.dump(2) + "\n";
    artifacts.push_back({sdc, constraints.sdc()});
    artifacts.push_back({keep, constraints.preserve()});
  } catch (const std::exception &e) {
    return Result<ArtifactSet>::failure(
        internal(std::string("Malformed CDC records: ") + e.what()));
  }
  std::ranges::sort(artifacts, {}, &Artifact::path);
  return Result<ArtifactSet>::success(std::move(artifacts));
}
} // namespace nocloom::detail
