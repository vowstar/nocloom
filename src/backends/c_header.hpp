#pragma once
#include "backends/optional_outputs.hpp"

namespace nocloom::detail {
Status process_c_headers(const nlohmann::json &map, OutputFiles *output);
} // namespace nocloom::detail
