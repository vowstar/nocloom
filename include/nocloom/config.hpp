#pragma once
#include "nocloom/definition.hpp"
#include <string_view>

namespace nocloom::config {
inline constexpr std::size_t max_input_mib = 32;
inline constexpr std::size_t max_input_bytes = max_input_mib * 1024 * 1024;
inline constexpr std::size_t max_depth = 32;
inline constexpr std::size_t max_nodes = 4 * 1024 * 1024;
Result<ParsedDefinition> parse(std::string_view text, std::string_view source_label = {});
} // namespace nocloom::config
