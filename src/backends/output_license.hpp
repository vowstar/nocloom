#pragma once
#include "nocloom/generation.hpp"
#include <string>
#include <string_view>

namespace nocloom::detail {
std::string output_license_comment(std::string_view prefix, std::string_view suffix = "");
ArtifactSet license_output_json(ArtifactSet artifacts);
std::string output_license_json(std::string_view text);
} // namespace nocloom::detail
