#pragma once
// nocloom diff: the semantic difference of two definitions, compared through
// the maps they generate. Names, not positions, identify list elements, so a
// reordered input that generates the same map has no difference.
#include "nocloom/io.hpp"
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace nocloom::cli {
enum class DiffScope {
  // definition, ports, module, format_version and the names of network.resources.
  Interface,
  // Every map field but generator.
  Full,
};
// One line per difference, in map order: "+ path: value" (only in after),
// "- path: value" (only in before), "~ path: old -> new" (changed) and
// "~ path: order" (the same elements in another order). Deterministic: the
// lines hold map content only.
std::vector<std::string> map_diff(const nlohmann::json &before, const nlohmann::json &after,
                                  DiffScope scope);
// The map of a YAML definition (generated in memory, nothing is written) or a
// map.json file.
Result<nlohmann::json> load_map(const std::filesystem::path &path);
} // namespace nocloom::cli
