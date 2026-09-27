#include "backends/constraints/cdc_sdc.hpp"
#include "backends/clock_period_json.hpp"
#include "backends/output_license.hpp"
#include "core/diagnostic.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <map>
#include <nlohmann/json.hpp>
#include <numeric>
#include <optional>
#include <set>
#include <stdexcept>

namespace nocloom::detail {
namespace {
using Json = nlohmann::json;
constexpr ClockPeriod::Numerator fs_per_ns = 1000000;
constexpr std::uint64_t whole_ns_threshold_ps = (std::uint64_t{1} << 32) * 1000;
constexpr auto double_digits = std::numeric_limits<double>::digits;
static_assert(std::numeric_limits<double>::radix == 2 && double_digits == 53);

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
  if (ps <= ClockPeriod{whole_ns_threshold_ps})
    return nanoseconds(ps);
  const auto whole_ns = ps.whole() / 1000;
  auto rounded = static_cast<double>(whole_ns);
  if (static_cast<ClockPeriod::Numerator>(rounded) > whole_ns)
    rounded = std::nextafter(rounded, 0.0);
  return ClockPeriod{static_cast<ClockPeriod::Numerator>(rounded)}.text();
}

std::string bounded_period(ClockPeriod ps) {
  if (ps > ClockPeriod{whole_ns_threshold_ps})
    return max_delay(ps);
  const auto fs = (ps * 1000).whole();
  if (fs == 0)
    return nanoseconds(ps);
  const auto rounded = static_cast<double>(fs) / static_cast<double>(fs_per_ns);
  int exponent = 0;
  const auto fraction = std::frexp(rounded, &exponent);
  // Compare the reader's binary value with the truncated decimal in integer arithmetic.
  const auto significand = static_cast<std::uint64_t>(std::ldexp(fraction, double_digits));
  if (significand * fs_per_ns <= (fs << (double_digits - exponent)))
    return nanoseconds(ps);
  const auto lower = std::nextafter(rounded, 0.0);
  // At most 72 places for a nonzero femtosecond value. Print the binary value exactly.
  char decimal[80];
  const auto places = double_digits - 1 - std::ilogb(lower);
  const auto result =
      std::to_chars(decimal, decimal + sizeof(decimal), lower, std::chars_format::fixed, places);
  auto end = result.ptr;
  while (*(end - 1) == '0')
    --end;
  return std::string(decimal, end);
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
    std::set<std::string> mixed_periods;
    std::set<std::string> related_roots;
    std::set<std::pair<std::string, std::string>> async_pairs;
    bool related = false, related_log = false;
    for (const auto &cut : cdc().at("cuts")) {
      const auto from = cut.at("from_domain").get<std::string>();
      const auto to = cut.at("to_domain").get<std::string>();
      crossing_domains.insert({from, to});
      const auto kind = cut.value("kind", std::string("async"));
      if (kind != "async") {
        related = true;
        const auto root = domains_.at(from)->at("clock_root").get<std::string>();
        related_roots.insert(root);
        const auto input = physical_root_period(root);
        if (kind == "related_log") {
          // The probe uses every root edge. The target and log select from those
          // edges, including when their dividers open. Promises size storage.
          related_log = true;
          log_paths(body, cut, input ? bounded_period(*input) : root_period(root));
          continue;
        }
        const auto spacing = edge_spacing(from, to);
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
      const auto sender = timing_period(from);
      const auto receiver = timing_period(to);
      if (sender.has_value() != receiver.has_value())
        mixed_periods.insert(sender ? from : to);
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
      out += domain_period_setting(domain, mixed_periods.contains(name));
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
    if (related_log)
      out += "set nocloom_data_pins [all_registers -data_pins]\n"
             "set nocloom_async_pins [all_registers -async_pins]\n";
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
  void log_paths(std::vector<std::string> &body, const Json &cut, const std::string &bound) const {
    body.push_back("\n# " + cut.at("name").get<std::string>() + ": related log handover");
    for (const auto &path : cut.at("paths")) {
      std::vector<std::string> from, to;
      for (const auto &source : path.at("sources")) {
        const auto names = cells(source);
        from.insert(from.end(), names.begin(), names.end());
      }
      for (const auto &destination : path.at("destinations")) {
        const auto names = cells(destination);
        to.insert(to.end(), names.begin(), names.end());
      }
      if (to.empty())
        continue;
      const std::string pins =
          path.at("pin") == "async_reset" ? "$nocloom_async_pins" : "$nocloom_data_pins";
      const auto ends = " -to [remove_from_collection " + pins + " [remove_from_collection " +
                        pins + " [get_pins -of_objects [get_cells {" + join(to) + "}]]]]";
      const auto delay = [&](const std::string &start) {
        body.push_back("set_max_delay " + bound + " -ignore_clock_latency" + start + ends);
        body.push_back("set_min_delay 0 -ignore_clock_latency" + start + ends);
      };
      body.push_back("# " + path.at("pin").get<std::string>() + " pins");
      if (!from.empty())
        delay(" -from [get_cells {" + join(from) + "}]");
      if (path.contains("input_ports") && !path.at("input_ports").empty())
        delay(" -from [get_ports {" + join(path.at("input_ports").get<std::vector<std::string>>()) +
              "}]");
    }
  }
  static std::string root_period(const std::string &root) { return "$nocloom_root_period_" + root; }

  std::vector<const Json *> clock_path(const std::string &name) const {
    std::vector<const Json *> path;
    for (auto at = name; !at.empty(); at = domains_.at(at)->value("edge_parent", std::string()))
      path.push_back(domains_.at(at));
    std::ranges::reverse(path);
    return path;
  }

  bool variable_edges(const Json &d) const {
    return d.contains("enable_port") ||
           (d.value("ratio", std::uint64_t{1}) > 1 &&
            (d.contains("test_port") || d.contains("ungate_port") ||
             map_.at("definition").value("register_reset", "") == "synchronous"));
  }

  std::uint64_t edge_spacing(const std::string &from, const std::string &to) const {
    const auto a = clock_path(from), z = clock_path(to);
    std::uint64_t common = 1;
    std::size_t shared = 0;
    while (shared < a.size() && shared < z.size() && a[shared] == z[shared] &&
           !variable_edges(*a[shared])) {
      common *= a[shared]->value("ratio", std::uint64_t{1});
      ++shared;
    }
    // Differing release edges can shift the phase of otherwise fixed sibling dividers.
    std::set<std::pair<std::string, bool>> releases;
    const auto tail = [&](const auto &path) -> std::optional<std::uint64_t> {
      auto ratio = common;
      for (std::size_t i = shared; i < path.size(); ++i) {
        const auto *d = path[i];
        if (variable_edges(*d))
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

  std::optional<ClockPeriod> timing_period(const std::string &name) const {
    const auto path = clock_path(name);
    const auto &domain = *path.back();
    if (std::ranges::none_of(path, [&](const Json *d) { return variable_edges(*d); }))
      return read_clock_period(domain);
    const auto root_name = domain.value("clock_root", std::string{});
    if (root_name.empty() && !domain.contains("enable_port")) {
      // An independent divider's declaration gives its nominal output period.
      if (const auto nominal = read_clock_period(domain))
        return ClockPeriod{nominal->whole(), domain.at("ratio").get<std::uint64_t>()};
    }
    const auto root = physical_root_period(root_name);
    if (!root)
      return std::nullopt;
    const auto ratio = std::accumulate(
        path.begin(), path.end(), std::uint64_t{1}, [&](auto product, const Json *d) {
          return variable_edges(*d) ? product : product * d->value("ratio", std::uint64_t{1});
        });
    return *root * ratio;
  }

  std::string domain_period_setting(const Json &d, bool mixed) const {
    const auto name = d.at("name").get<std::string>();
    const auto variable = "nocloom_period_" + name;
    if (const auto ps = timing_period(name))
      return "set " + variable + " " + (mixed ? bounded_period(*ps) : nanoseconds(*ps)) + "\n";
    if (d.contains("period_ps"))
      return "# Set " + variable + " (minimum possible period in ns) before reading this file.\n";
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

Status process_cdc_constraints(const Json &map, OutputFiles *output) {
  try {
    const auto *network = map.contains("network") ? &map.at("network") : nullptr;
    if (!network || !network->contains("cdc") || network->at("cdc").at("cuts").empty())
      return Status::failure(
          error("NCL_UNSUPPORTED_BACKEND", "output.cdc_constraints",
                "CDC constraints require a clocked network with at least one crossing"));
    const auto top = map.at("module").get<std::string>();
    CdcConstraints constraints(map, top);
    const auto add = [&](std::string_view suffix, std::string_view role, std::string text) {
      if (output)
        output->add(top + std::string(suffix), role, std::move(text));
    };
    add(".cdc.sdc", "cdc_constraints", constraints.sdc());
    add(".cdc_preserve.txt", "cdc_preserve_list", constraints.preserve());
  } catch (const std::exception &e) {
    return Status::failure(internal(std::string("Malformed CDC records: ") + e.what()));
  }
  return Status::success({});
}
} // namespace nocloom::detail
