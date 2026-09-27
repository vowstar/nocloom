#include "diff.hpp"
#include "nocloom/config.hpp"
#include <algorithm>
#include <fstream>
#include <functional>
#include <optional>
#include <set>

namespace nocloom::cli {
namespace {
using json = nlohmann::json;
using Identity = std::function<std::optional<std::string>(const json &)>;

std::optional<std::string> text(const json &object, const char *key) {
  if (!object.is_object() || !object.contains(key) || !object[key].is_string())
    return std::nullopt;
  return object[key].get<std::string>();
}
Identity pair(const char *first, const char *second) {
  return [first, second](const json &e) -> std::optional<std::string> {
    const auto a = text(e, first), b = text(e, second);
    if (!a || !b)
      return std::nullopt;
    return *a + "->" + *b;
  };
}
Identity field(const char *key) {
  return [key](const json &e) { return text(e, key); };
}
// The ways a list element is named, tried in order; a list whose elements all
// have distinct names under one of them is compared by name, any other list
// by position.
const std::vector<Identity> &identities() {
  static const std::vector<Identity> all{field("name"),
                                         field("output"),
                                         field("resource"),
                                         field("switch"),
                                         pair("source", "target"),
                                         pair("from", "to"),
                                         pair("entry", "exit"),
                                         field("endpoint"),
                                         field("source"),
                                         [](const json &e) -> std::optional<std::string> {
                                           if (!e.is_primitive())
                                             return std::nullopt;
                                           return e.is_string() ? e.get<std::string>() : e.dump();
                                         }};
  return all;
}
std::optional<std::vector<std::string>> names(const json &list, const Identity &identity) {
  std::vector<std::string> result;
  std::set<std::string> seen;
  for (const auto &e : list) {
    auto id = identity(e);
    if (!id || !seen.insert(*id).second)
      return std::nullopt;
    result.push_back(std::move(*id));
  }
  return result;
}
std::string below(const std::string &path, const std::string &key) {
  return path.empty() ? key : path + "." + key;
}

void walk(const json &a, const json &b, const std::string &path, std::vector<std::string> &out) {
  if (a.is_object() && b.is_object()) {
    for (const auto &[key, value] : a.items())
      if (!b.contains(key))
        out.push_back("- " + below(path, key) + ": " + value.dump());
      else
        walk(value, b[key], below(path, key), out);
    for (const auto &[key, value] : b.items())
      if (!a.contains(key))
        out.push_back("+ " + below(path, key) + ": " + value.dump());
    return;
  }
  if (a.is_array() && b.is_array()) {
    for (const auto &identity : identities()) {
      const auto left = names(a, identity), right = names(b, identity);
      if (!left || !right)
        continue;
      std::vector<std::string> kept_left, kept_right;
      for (std::size_t i = 0; i < left->size(); ++i) {
        const auto j = std::find(right->begin(), right->end(), (*left)[i]);
        const auto at = path + "[" + (*left)[i] + "]";
        if (j == right->end()) {
          out.push_back("- " + at + ": " + a[i].dump());
          continue;
        }
        kept_left.push_back((*left)[i]);
        walk(a[i], b[static_cast<std::size_t>(j - right->begin())], at, out);
      }
      for (std::size_t j = 0; j < right->size(); ++j)
        if (std::find(left->begin(), left->end(), (*right)[j]) == left->end())
          out.push_back("+ " + path + "[" + (*right)[j] + "]: " + b[j].dump());
        else
          kept_right.push_back((*right)[j]);
      if (kept_left != kept_right)
        out.push_back("~ " + path + ": order");
      return;
    }
    for (std::size_t i = 0; i < std::max(a.size(), b.size()); ++i) {
      const auto at = path + "[" + std::to_string(i) + "]";
      if (i >= b.size())
        out.push_back("- " + at + ": " + a[i].dump());
      else if (i >= a.size())
        out.push_back("+ " + at + ": " + b[i].dump());
      else
        walk(a[i], b[i], at, out);
    }
    return;
  }
  if (a != b)
    out.push_back("~ " + path + ": " + a.dump() + " -> " + b.dump());
}

json scoped(const json &map, DiffScope scope) {
  json result = json::object();
  if (!map.is_object())
    return result;
  if (scope == DiffScope::Full) {
    result = map;
    result.erase("generator");
    return result;
  }
  for (const auto *key : {"format_version", "module", "definition", "ports"})
    if (map.contains(key))
      result[key] = map[key];
  if (map.contains("network") && map["network"].contains("resources")) {
    json resources = json::array();
    for (const auto &r : map["network"]["resources"])
      if (const auto name = text(r, "name"))
        resources.push_back(*name);
    result["network"]["resources"] = std::move(resources);
  }
  return result;
}
// The fields every map has; a bundle lacks network and ports.
constexpr const char *map_keys[] = {"format_version", "module", "definition", "network", "ports"};
Diagnostic failure(std::string code, std::string message) {
  return {std::move(code), "", std::move(message), {}, {}};
}
} // namespace

std::vector<std::string> map_diff(const json &before, const json &after, DiffScope scope) {
  std::vector<std::string> out;
  walk(scoped(before, scope), scoped(after, scope), "", out);
  return out;
}

Result<json> load_map(const std::filesystem::path &path) {
  const auto extension = path.extension();
  if (extension == ".yaml" || extension == ".yml") {
    auto parsed = io::load_definition(path);
    if (!parsed)
      return Result<json>::failure(parsed.diagnostics());
    const auto &input = parsed.value();
    auto artifacts = generate(input.definition, input.options, &input.sources);
    if (!artifacts)
      return Result<json>::failure(artifacts.diagnostics());
    const auto &set = artifacts.value();
    const auto map = std::find_if(set.begin(), set.end(),
                                  [](const Artifact &a) { return a.path.ends_with(".map.json"); });
    if (map == set.end())
      return Result<json>::failure(failure("NCL_DIFF_MAP", "Generation returned no map"));
    return Result<json>::success(json::parse(map->text));
  }
  if (extension != ".json")
    return Result<json>::failure(
        failure("NCL_INPUT_FORMAT", "Expected a .yaml, .yml or .json (map) file"));
  std::ifstream input(path, std::ios::binary);
  if (!input)
    return Result<json>::failure(
        failure("NCL_IO_READ", "Cannot open input file: " + path.string()));
  // The input limits of a YAML definition apply to a map: its size, and the nesting that the
  // comparison descends.
  std::string content(config::max_input_bytes + 1, '\0');
  input.read(content.data(), static_cast<std::streamsize>(content.size()));
  if (input.bad())
    return Result<json>::failure(
        failure("NCL_IO_READ", "Cannot read input file: " + path.string()));
  content.resize(static_cast<std::size_t>(input.gcount()));
  if (content.size() > config::max_input_bytes)
    return Result<json>::failure(
        failure("NCL_RESOURCE_LIMIT",
                "JSON input exceeds " + std::to_string(config::max_input_mib) + " MiB"));
  bool too_deep = false;
  const json::parser_callback_t within_depth = [&](int depth, json::parse_event_t event, json &) {
    const bool opens =
        event == json::parse_event_t::object_start || event == json::parse_event_t::array_start;
    too_deep = too_deep || (opens && depth >= static_cast<int>(config::max_depth));
    return !too_deep;
  };
  auto map = json::parse(content, within_depth, false);
  if (too_deep)
    return Result<json>::failure(failure("NCL_RESOURCE_LIMIT", "JSON nesting limit exceeded"));
  if (map.is_discarded() || !map.is_object() ||
      std::any_of(std::begin(map_keys), std::end(map_keys),
                  [&](const char *key) { return !map.contains(key); }))
    return Result<json>::failure(
        failure("NCL_DIFF_MAP", path.string() + " is not a NocLoom map (.map.json)"));
  return Result<json>::success(std::move(map));
}
} // namespace nocloom::cli
