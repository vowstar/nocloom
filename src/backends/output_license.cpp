#include "output_license.hpp"
#include <nlohmann/json.hpp>

namespace nocloom::detail {
namespace {
constexpr auto identifier = "LicenseRef-NocLoom-Output-1.0";
constexpr auto permission = "LICENSES/LicenseRef-NocLoom-Output-1.0.txt";
} // namespace
std::string output_license_comment(std::string_view prefix, std::string_view suffix) {
  return std::string(prefix) + " SPDX-License-Identifier: " + identifier + std::string(suffix) +
         "\n" + std::string(prefix) + " NocLoom supplied content only. See NocLoom " + permission +
         std::string(suffix) + "\n";
}
std::string output_license_json(std::string_view text) {
  auto json = nlohmann::json::parse(text);
  json["_license"] = {{"SPDX-License-Identifier", identifier},
                      {"scope", "NocLoom supplied content only"},
                      {"license_file", permission},
                      {"project_license_file", "LICENSES/Apache-2.0.txt"}};
  return json.dump(2) + "\n";
}
ArtifactSet license_output_json(ArtifactSet artifacts) {
  for (auto &artifact : artifacts)
    if (artifact.path.ends_with(".json"))
      artifact.text = output_license_json(artifact.text);
  return artifacts;
}
} // namespace nocloom::detail
