// The address table: each initiator's routing windows, read back from the interface map.
#include "backends/address_table.hpp"
#include "backends/output_license.hpp"
#include "core/diagnostic.hpp"
#include "core/network_plan_profiles.hpp"
#include "nocloom/generation.hpp"
#include <algorithm>
#include <bit>
#include <charconv>
#include <cstdint>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <ranges>
#include <set>
#include <span>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace nocloom {
namespace {
using Json = nlohmann::json;

struct Mapping {
  std::string target;
  std::uint64_t base = 0, end = 0, local = 0;
  // The mode condition as written in the map ({flag: value}), empty for every mode.
  Json when = Json::object();
  // The target flow (docs/network.md#endpoint-flows), 0 for a target of one.
  unsigned flow = 0;
};
struct Window {
  std::uint64_t base = 0, end = 0;
  const Mapping *mapping = nullptr;
};

std::uint64_t number(const Json &value) {
  const auto text = value.get<std::string>();
  std::uint64_t out = 0;
  const auto [at, ec] = std::from_chars(text.data(), text.data() + text.size(), out);
  if (ec != std::errc{} || at != text.data() + text.size())
    throw std::invalid_argument("map address " + text);
  return out;
}
std::string hex(std::uint64_t value, unsigned digits) {
  std::string out(16, '0');
  auto [at, ec] = std::to_chars(out.data(), out.data() + out.size(), value, 16);
  out.resize(static_cast<std::size_t>(at - out.data()));
  if (out.size() < digits)
    out.insert(0, digits - out.size(), '0');
  return "0x" + out;
}
// The aligned power-of-two blocks of [base, end).
std::vector<std::pair<std::uint64_t, std::uint64_t>> blocks(std::uint64_t base, std::uint64_t end) {
  std::vector<std::pair<std::uint64_t, std::uint64_t>> out;
  for (std::uint64_t size = 0; base < end; base += size) {
    size = std::bit_floor(end - base);
    if (base)
      size = std::min(size, base & (~base + 1));
    out.emplace_back(base, size);
  }
  return out;
}
// The condition of a window as table text: each flag, "!" before one that must be false, joined
// by "&"; empty without one.
std::string condition(const Json &when) {
  std::string out;
  for (const auto &[flag, value] : when.items())
    out += (out.empty() ? "" : "&") + std::string(value.get<bool>() ? "" : "!") + flag;
  return out;
}
// Routing windows (docs/address-map.md#target-mappings): every mapping as its aligned blocks, each
// block less the smaller blocks it holds, contiguous pieces of one mapping merged, in base order.
// Mappings of different mode conditions never overlap where both decode
// (docs/address-map.md#address-selectors): each condition is laid out alone, and its windows follow
// the others of equal base.
std::vector<Window> routing_windows(const std::vector<Mapping> &mappings) {
  struct Block {
    std::uint64_t base, size;
    const Mapping *mapping;
  };
  std::vector<Block> all;
  for (const auto &m : mappings)
    for (const auto &[base, size] : blocks(m.base, m.end))
      all.push_back({base, size, &m});
  std::ranges::sort(all, {}, [](const Block &b) { return std::pair{b.size, b.base}; });
  std::vector<Window> pieces;
  for (std::size_t i = 0; i < all.size(); ++i) {
    const auto &w = all[i];
    std::vector<std::pair<std::uint64_t, std::uint64_t>> held;
    for (const auto &h : std::span(all).first(i))
      if (h.mapping->when == w.mapping->when && h.base >= w.base &&
          h.base + h.size <= w.base + w.size)
        held.emplace_back(h.base, h.base + h.size);
    std::ranges::sort(held);
    auto next = w.base;
    for (const auto &[base, end] : held) {
      if (base > next)
        pieces.push_back({next, base, w.mapping});
      next = std::max(next, end);
    }
    if (next < w.base + w.size)
      pieces.push_back({next, w.base + w.size, w.mapping});
  }
  std::ranges::stable_sort(
      pieces, {}, [](const Window &w) { return std::pair{w.base, condition(w.mapping->when)}; });
  std::vector<Window> out;
  for (const auto &p : pieces)
    if (!out.empty() && out.back().mapping == p.mapping && out.back().end == p.base)
      out.back().end = p.end;
    else
      out.push_back(p);
  return out;
}
// The value of a name-keyed map record, or the fallback.
template <typename T> T lookup(const Json &object, const std::string &key, T fallback) {
  if (object.is_object() && object.contains(key))
    return object.at(key).get<T>();
  return fallback;
}

struct Table {
  Json json;
  std::string csv;
};
// The targets a source reaches, each with the [initiator flow, target flow] pairs it connects when
// the map lists them (flow_pairs), else every pair.
using Reached = std::map<std::string, std::optional<std::set<std::pair<unsigned, unsigned>>>>;
// The initiator flows of a source that reach a mapping (all of a source of one flow: {0}).
std::vector<unsigned> reaching(const Reached &reached, const Mapping &m) {
  const auto at = reached.find(m.target);
  if (at == reached.end())
    return {};
  if (!at->second)
    return {0};
  std::vector<unsigned> out;
  for (const auto &[f, g] : *at->second)
    if (g == m.flow)
      out.push_back(f);
  return out;
}
std::string default_target(const Json &d, const std::string &source,
                           const std::vector<Mapping> &mappings, const Reached &reached,
                           unsigned address_bits) {
  if (const auto named = lookup<std::string>(d.value("default_targets", Json{}), source, "");
      !named.empty())
    return named;
  std::map<std::string, unsigned> target_indices;
  for (const auto &m : mappings)
    target_indices.emplace(m.target, 0);
  unsigned index = 0;
  for (auto &[name, value] : target_indices)
    value = index++;
  std::vector<detail::DecodeEntry> candidates;
  for (unsigned m = 0; m < mappings.size(); ++m)
    if (!reaching(reached, mappings[m]).empty())
      candidates.push_back({{mappings[m].base, mappings[m].end - mappings[m].base},
                            target_indices.at(mappings[m].target),
                            m});
  const auto best = detail::select_default_mapping(candidates, address_bits);
  return best ? mappings[best->mapping].target : "";
}
Table build(const Json &map) {
  const auto &d = map.at("definition");
  const auto width = d.at("address_width").get<unsigned>();
  const auto digits = (width + 3) / 4;
  std::vector<Mapping> mappings;
  std::ranges::transform(d.at("targets"), std::back_inserter(mappings), [](const Json &row) {
    return Mapping{row.at("name").get<std::string>(),
                   number(row.at("begin")),
                   number(row.at("end")),
                   row.contains("local") ? number(row.at("local")) : 0,
                   row.value("when", Json::object()),
                   row.value("flow", 0U)};
  });
  // A mode-dependent network lists each window's condition in a column of its own.
  const bool modes =
      std::ranges::any_of(mappings, [](const Mapping &m) { return !m.when.empty(); });
  // A network with target flows lists each window's flow in a column of its own.
  const bool flows = std::ranges::any_of(mappings, [](const Mapping &m) { return m.flow > 0; });
  const auto windows = routing_windows(mappings);
  std::map<std::string, Reached> reached;
  // A network whose matrix leaves out flow pairs lists the initiator flows reaching each window.
  bool restricted = false;
  for (const auto &pair : d.at("connectivity")) {
    const auto source = pair.at("source").get<std::string>(),
               target = pair.at("target").get<std::string>();
    reached[source][target];
    if (pair.contains("flow_pairs")) {
      restricted = true;
      auto &to = reached[source][target];
      to.emplace();
      for (const auto &fg : pair.at("flow_pairs"))
        to->emplace(fg.at(0).get<unsigned>(), fg.at(1).get<unsigned>());
    }
  }
  const auto transactions = d.value("endpoint_transactions", Json::object());
  std::set<std::string> read_only_targets, write_only_targets;
  for (const auto &name : transactions.value("read_only_targets", Json::array()))
    read_only_targets.insert(name.get<std::string>());
  for (const auto &name : transactions.value("write_only_targets", Json::array()))
    write_only_targets.insert(name.get<std::string>());
  const auto exclusive = d.value("exclusive_targets", Json::object());
  const auto protocols = d.value("endpoint_protocols", Json::object());
  const auto address_widths = d.value("endpoint_address_widths", Json::object());
  Table out;
  out.json = {{"format_version", 1},
              {"generator", "NocLoom"},
              {"module", map.at("module")},
              {"address_width", width},
              {"initiators", Json::array()}};
  out.csv = std::string("initiator,base,size,target,local_base,access,exclusive,default_target") +
            (modes ? ",when" : "") + (flows ? ",flow" : "") +
            (restricted ? ",source_paths\n" : "\n");
  for (const auto &item : d.at("initiators")) {
    const auto source = item.get<std::string>();
    const auto &targets = reached[source];
    const auto source_access = lookup<std::string>(
        lookup(transactions.value("initiators", Json::object()), source, Json::object()), "access",
        "");
    static const std::map<std::string, Protocol> protocol_values = {
        {"axi4", Protocol::Axi4},    {"axi3", Protocol::Axi3}, {"axi4_lite", Protocol::Axi4Lite},
        {"apb5", Protocol::Apb5},    {"apb4", Protocol::Apb4}, {"apb3", Protocol::Apb3},
        {"ahb_v3", Protocol::AhbV3}, {"ahb5", Protocol::Ahb5}};
    const bool full_axi = detail::full_axi(protocol_values.at(
        lookup<std::string>(protocols.value("initiators", Json::object()), source, "axi4")));
    // A narrower source zero-extends its address: it reaches only the low part of the space.
    const auto bits =
        lookup<unsigned>(address_widths.value("initiators", Json::object()), source, width);
    const auto limit = bits >= 64 ? ~std::uint64_t{0} : std::uint64_t{1} << bits;
    const auto fallback = default_target(d, source, mappings, targets, bits);
    Json entry = {{"name", source}, {"default_target", fallback}, {"windows", Json::array()}};
    for (const auto &w : windows) {
      const auto &t = w.mapping->target;
      const auto from = reaching(targets, *w.mapping);
      if (from.empty() || w.base >= limit)
        continue;
      const auto end = std::min(w.end, limit);
      const auto local = w.mapping->local + (w.base - w.mapping->base);
      // A read-only side keeps reads, a write-only side writes; both sides together keep none.
      const bool reads = source_access != "write_only" && !write_only_targets.contains(t);
      const bool writes = source_access != "read_only" && !read_only_targets.contains(t);
      const auto access = reads && writes ? "read_write"
                          : reads         ? "read_only"
                          : writes        ? "write_only"
                                          : "none";
      const auto mode =
          full_axi ? lookup<Json>(exclusive, t, Json::object()).value("mode", "none") : "none";
      const bool miss = t == fallback;
      Json row{{"base", hex(w.base, digits)},
               {"size", hex(end - w.base, digits)},
               {"target", t},
               {"local_base", hex(local, digits)},
               {"access", access},
               {"exclusive", mode},
               {"default_target", miss}};
      if (!w.mapping->when.empty())
        row["when"] = w.mapping->when;
      if (flows)
        row["flow"] = w.mapping->flow;
      std::string listed;
      for (const auto f : from)
        listed += (listed.empty() ? "" : " ") + std::to_string(f);
      if (restricted)
        row["source_paths"] = from;
      entry["windows"].push_back(std::move(row));
      out.csv += source + "," + hex(w.base, digits) + "," + hex(end - w.base, digits) + "," + t +
                 "," + hex(local, digits) + "," + access + "," + mode + "," +
                 (miss ? "true" : "false") + (modes ? "," + condition(w.mapping->when) : "") +
                 (flows ? "," + std::to_string(w.mapping->flow) : "") +
                 (restricted ? "," + listed : "") + "\n";
    }
    out.json["initiators"].push_back(std::move(entry));
  }
  return out;
}
Diagnostic input_error(const std::string &message) {
  return detail::error("NCL_ADDRESS_TABLE_INPUT", "artifacts", message);
}
} // namespace

namespace detail {
Json address_table(const Json &map) { return build(map).json; }
} // namespace detail

Result<ArtifactSet> add_address_table(ArtifactSet artifacts) {
  const auto with = [&](std::string_view suffix) {
    std::vector<Artifact *> out;
    for (auto &a : artifacts)
      if (a.path.ends_with(suffix))
        out.push_back(&a);
    return out;
  };
  const auto maps = with(".map.json"), bundles = with(".bundle.json");
  if (maps.size() != 1 || bundles.size() != 1 || !with(".address_table.json").empty() ||
      !with(".address_table.csv").empty())
    return Result<ArtifactSet>::failure(input_error(
        "An address table needs one generated map and bundle and no address table yet"));
  try {
    const auto table = build(Json::parse(maps.front()->text));
    const auto module = table.json.at("module").get<std::string>();
    auto bundle = Json::parse(bundles.front()->text);
    for (const auto *suffix : {".address_table.json", ".address_table.csv"})
      bundle.at("files").push_back({{"path", module + suffix}, {"role", "address_table"}});
    bundles.front()->text = bundle.dump(2) + "\n";
    artifacts.push_back(
        {module + ".address_table.json", detail::output_license_json(table.json.dump(2))});
    artifacts.push_back(
        {module + ".address_table.csv", detail::output_license_comment("#") + table.csv});
  } catch (const std::exception &e) {
    return Result<ArtifactSet>::failure(
        input_error(std::string("The map cannot be read as a NocLoom map: ") + e.what()));
  }
  std::ranges::sort(artifacts, {}, &Artifact::path);
  return Result<ArtifactSet>::success(std::move(artifacts));
}
} // namespace nocloom
