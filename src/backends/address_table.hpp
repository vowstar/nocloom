#pragma once
#include "backends/optional_outputs.hpp"

namespace nocloom::detail {
// Per-initiator routing windows, including mode, flow and address-width selection.
nlohmann::json address_table(const nlohmann::json &map);
Status process_address_table(const nlohmann::json &map, OutputFiles *output);
} // namespace nocloom::detail
