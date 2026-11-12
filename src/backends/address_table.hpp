#pragma once
#include <nlohmann/json_fwd.hpp>

namespace nocloom::detail {
// Per-initiator routing windows, including mode, flow and address-width selection.
nlohmann::json address_table(const nlohmann::json &map);
} // namespace nocloom::detail
